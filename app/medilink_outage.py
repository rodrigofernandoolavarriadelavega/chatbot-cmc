"""medilink_outage.py — Modo caída de PLATAFORMA Medilink (403 "no se
encuentra activa") + recontacto contextual automático al recuperarse.

Distinto de resilience.py, que cubre saturación (429) y caídas de red
genéricas (5xx/timeout): esto es específico de la suspensión de la
plataforma Healthatom, que devuelve el mismo 403 a TODO endpoint. Incidente
real 2026-08-12 12:00-13:22 UTC (82 min): 8 pacientes rebotados con "Tuve un
problema técnico" y ninguno quedó en cola de aviso — recepción los recontactó
a mano con las horas exactas que pedían y convirtió 7/8.

Diseño (aprobado por Rodrigo 2026-08-12, reemplaza la idea original de "cola
por-error"):
  1. MODO CAÍDA — flag persistido en `system_state` (sobrevive restart).
     Se ABRE con 2 fallos MedilinkInactiva CONSECUTIVOS (evita falsa alarma
     con un 403 aislado). Se CIERRA con 2 sondeos OK consecutivos del watcher
     (jobs.py). Tope duro: 24 h desde la apertura (ventana de sesión de
     WhatsApp — pasado eso, avisar exige un template pagado; no vale la pena).
  2. CAPTURA — mientras el modo está abierto, TODO mensaje entrante (no solo
     los que revientan contra Medilink) se registra/actualiza en
     `medilink_outage_context`: hasta 5 últimos textos, especialidad/
     profesional/fecha si la sesión los tiene, rut si está, y el estado en
     que quedó (incluye HUMAN_TAKEOVER — el bot no responde pero SÍ captura).
  3. RECONTACTO — al recuperarse (jobs.py._job_medilink_outage_watcher):
     regla barata primero (especialidad/profesional ya en el contexto);
     Claude (claude_helper) solo para los ambiguos. Guardrails: skip si el
     rut ya tiene cita futura, skip si state_al_fallar=HUMAN_TAKEOVER (queda
     para recepción), skip fuera de la ventana de 24 h.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from session import db, system_state_get, system_state_set

log = logging.getLogger("medilink_outage")

# ── Claves en system_state ───────────────────────────────────────────────────
_KEY_MODE = "medilink_outage_mode"              # "open" | "closed"
_KEY_OPENED_AT = "medilink_outage_opened_at"    # ISO ts de la apertura
_KEY_FAIL_STREAK = "medilink_outage_fail_streak"  # fallos MedilinkInactiva consecutivos
_KEY_OK_STREAK = "medilink_outage_ok_streak"      # sondeos OK consecutivos del watcher

FAILS_TO_OPEN = 2
OKS_TO_CLOSE = 2
WINDOW_HOURS = 24


# ── Tabla local ───────────────────────────────────────────────────────────────

def _ensure_table() -> None:
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS medilink_outage_context (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                phone           TEXT NOT NULL UNIQUE,
                first_ts        TEXT DEFAULT (datetime('now')),
                last_ts         TEXT DEFAULT (datetime('now')),
                textos          TEXT DEFAULT '[]',
                especialidad    TEXT DEFAULT '',
                id_profesional  INTEGER,
                fecha_preferida TEXT,
                rut             TEXT DEFAULT '',
                state_al_fallar TEXT DEFAULT '',
                avisado         INTEGER DEFAULT 0,
                avisado_ts      TEXT,
                resultado       TEXT DEFAULT ''
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_outage_ctx_avisado "
            "ON medilink_outage_context(avisado)"
        )
        conn.commit()


# ── Estado del modo caída ─────────────────────────────────────────────────────

def note_fallo_inactiva() -> bool:
    """Registra un fallo MedilinkInactiva (llamado desde medilink.py en cada
    403 de plataforma suspendida, sin importar qué caller lo disparó).
    Retorna True si esto ABRE el modo caída recién ahora (2º fallo seguido)."""
    _ensure_table()
    streak = int(system_state_get(_KEY_FAIL_STREAK) or 0) + 1
    system_state_set(_KEY_FAIL_STREAK, str(streak))
    if streak >= FAILS_TO_OPEN and not is_open():
        system_state_set(_KEY_MODE, "open")
        system_state_set(_KEY_OPENED_AT, datetime.now(timezone.utc).isoformat())
        log.error("MEDILINK_OUTAGE modo caída ABIERTO (%d fallos consecutivos)", streak)
        return True
    return False


def note_exito() -> None:
    """Cualquier respuesta Medilink que NO sea el 403 de plataforma inactiva
    rompe la racha de fallos — evita que 403 aislados y separados en el
    tiempo se sumen como si fueran consecutivos."""
    if system_state_get(_KEY_FAIL_STREAK) not in (None, "0"):
        system_state_set(_KEY_FAIL_STREAK, "0")


def is_open() -> bool:
    """True si el modo caída está abierto Y dentro de la ventana de 24 h."""
    if system_state_get(_KEY_MODE) != "open":
        return False
    opened = system_state_get(_KEY_OPENED_AT)
    if opened:
        try:
            ts = datetime.fromisoformat(opened)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - ts) > timedelta(hours=WINDOW_HOURS):
                return False
        except (ValueError, TypeError):
            pass
    return True


def opened_at() -> str | None:
    return system_state_get(_KEY_OPENED_AT)


def note_sondeo_ok() -> bool:
    """Sondeo exitoso del watcher (jobs.py). Retorna True si esto CIERRA el
    modo caída recién ahora (2º sondeo OK seguido)."""
    streak = int(system_state_get(_KEY_OK_STREAK) or 0) + 1
    system_state_set(_KEY_OK_STREAK, str(streak))
    if streak >= OKS_TO_CLOSE:
        system_state_set(_KEY_MODE, "closed")
        system_state_set(_KEY_OK_STREAK, "0")
        system_state_set(_KEY_FAIL_STREAK, "0")
        log.warning("MEDILINK_OUTAGE modo caída CERRADO (%d sondeos OK)", streak)
        return True
    return False


def note_sondeo_fail() -> None:
    """Sondeo fallido del watcher: sigue caído, corta la racha de OKs."""
    system_state_set(_KEY_OK_STREAK, "0")


# ── Captura de contexto por paciente ─────────────────────────────────────────

def capturar_mensaje(phone: str, texto: str, session: dict, force: bool = False) -> None:
    """Registra/actualiza el contexto de este paciente mientras el modo caída
    está abierto. `force=True` ignora el gate de is_open() — se usa desde el
    handler de MedilinkInactiva en main.py para garantizar que el mensaje que
    disparó el fallo (incluido el que recién abre el modo, antes de que
    is_open() sea True) siempre quede capturado.

    No hace nada si el modo está cerrado y force=False (evita escribir en
    cada mensaje normal del bot, la inmensa mayoría del tráfico).
    """
    if not force and not is_open():
        return
    _ensure_table()
    data = session.get("data") if session else {}
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (ValueError, TypeError):
            data = {}
    data = data or {}
    state = (session or {}).get("state") or "IDLE"
    especialidad = (data.get("especialidad") or data.get("quick_esp") or "").strip()
    id_prof = data.get("prof_sugerido_id") or data.get("id_profesional")
    fecha_pref = data.get("fecha_preferida") or ""
    rut = (data.get("rut") or "").strip()
    if not rut:
        try:
            from session import get_profile
            perfil = get_profile(phone)
            if perfil:
                rut = (perfil.get("rut") or "").strip()
        except Exception:
            pass
    txt = (texto or "").strip()[:300]

    with db() as conn:
        row = conn.execute(
            "SELECT textos FROM medilink_outage_context WHERE phone=?", (phone,)
        ).fetchone()
        textos = []
        if row and row["textos"]:
            try:
                textos = json.loads(row["textos"])
            except (ValueError, TypeError):
                textos = []
        if txt and (not textos or textos[-1] != txt):
            textos.append(txt)
        textos = textos[-5:]
        conn.execute("""
            INSERT INTO medilink_outage_context
                (phone, first_ts, last_ts, textos, especialidad, id_profesional,
                 fecha_preferida, rut, state_al_fallar, avisado)
            VALUES (?, datetime('now'), datetime('now'), ?, ?, ?, ?, ?, ?, 0)
            ON CONFLICT(phone) DO UPDATE SET
                last_ts = datetime('now'),
                textos = excluded.textos,
                especialidad = CASE WHEN excluded.especialidad != '' THEN excluded.especialidad
                                     ELSE medilink_outage_context.especialidad END,
                id_profesional = COALESCE(excluded.id_profesional, medilink_outage_context.id_profesional),
                fecha_preferida = CASE WHEN excluded.fecha_preferida != '' THEN excluded.fecha_preferida
                                        ELSE medilink_outage_context.fecha_preferida END,
                rut = CASE WHEN excluded.rut != '' THEN excluded.rut ELSE medilink_outage_context.rut END,
                state_al_fallar = excluded.state_al_fallar
        """, (phone, json.dumps(textos), especialidad, id_prof, fecha_pref, rut, state))
        conn.commit()


def hay_pendientes() -> bool:
    _ensure_table()
    with db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM medilink_outage_context WHERE avisado = 0"
        ).fetchone()
        return bool(row and row["n"])


def list_pendientes() -> list[dict]:
    """Contextos capturados durante la caída, aún sin avisar (más antiguos primero)."""
    _ensure_table()
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM medilink_outage_context WHERE avisado = 0 ORDER BY first_ts ASC"
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["textos"] = json.loads(d.get("textos") or "[]")
        except (ValueError, TypeError):
            d["textos"] = []
        out.append(d)
    return out


def marcar_resultado(phone: str, resultado: str) -> None:
    """Cierra el pendiente de este paciente (enviado / skip / expirado...)."""
    _ensure_table()
    with db() as conn:
        conn.execute("""
            UPDATE medilink_outage_context
            SET avisado = 1, avisado_ts = datetime('now'), resultado = ?
            WHERE phone = ?
        """, (resultado, phone))
        conn.commit()


def expirar_pendientes() -> int:
    """Marca 'expirado' los contextos sin avisar de más de WINDOW_HOURS desde
    su primera captura — fuera de la ventana de sesión de WhatsApp, avisar
    exigiría un template pagado y ya perdió el momento. Retorna cuántos."""
    _ensure_table()
    with db() as conn:
        cur = conn.execute("""
            UPDATE medilink_outage_context
            SET avisado = 1, avisado_ts = datetime('now'), resultado = 'expirado'
            WHERE avisado = 0 AND first_ts < datetime('now', ?)
        """, (f"-{WINDOW_HOURS} hours",))
        conn.commit()
        return cur.rowcount
