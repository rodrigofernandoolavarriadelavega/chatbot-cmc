"""Effectiveness Ledger de la flota — ¿cada agente realmente funciona?

El decision-log dice qué hizo cada agente. Este ledger dice si **sirvió**: toma
cada acción EJECUTADA que contactó a un paciente y la cruza, dentro de una
ventana, contra señales reales en `sessions.db` (cita agendada/confirmada/
reagendada o, como mínimo, respuesta del paciente). Así la flota deja de ser un
acto de fe: cada agente tiene conversión real, tasa de respuesta e ingreso
recuperable estimado.

Diseño:
  - Read-only sobre sessions.db salvo su propia tabla `agent_action_ledger`
    (vive en la MISMA sessions.db → queda encriptada SQLCipher + respaldada).
  - `record_result(run)` se llama desde `base.run()` tras ejecutar: inserta una
    fila por acción de contacto a paciente (outcome='pendiente').
  - `measure_outcomes()` resuelve las filas pendientes cuya ventana ya venció,
    marcando outcome = cita | respuesta | sin_respuesta.
  - `effectiveness_summary()` agrega por agente para el panel/KPI.

Atribución honesta (sin sobre-atribuir): una acción se acredita un resultado solo
si ocurrió DESPUÉS del contacto y DENTRO de la ventana. Es last-touch por agente;
si dos agentes contactan al mismo paciente en la ventana, ambos registran el
mismo resultado (se anota el solapamiento, no se infla un total global).
"""
import json
import logging
import re
from datetime import datetime, timedelta, timezone

log = logging.getLogger("bot")

# Ventana por defecto para atribuir un resultado a un contacto.
DEFAULT_WINDOW_DAYS = 7
# Ticket promedio conservador (CLP) para estimar ingreso recuperable por cita.
# Es una ESTIMACIÓN — el valor real depende de especialidad; ver nota en summary.
AVG_TICKET_CLP = 25_000

# Eventos de conversation_events que cuentan como "cita lograda".
_CITA_EVENTS = (
    "cita_confirmada", "cita_confirmada_texto_libre", "cita_creada_panel",
    "cita_reagendada", "agendado_manual",
)

# sessions.db guarda timestamps como `datetime('now')` → "YYYY-MM-DD HH:MM:SS"
# (UTC, con espacio, sin offset). TODO timestamp del ledger se normaliza a este
# formato para que las comparaciones de string contra events/messages/citas_bot
# sean correctas (mezclar ISO-T con formato-espacio rompe el orden lexicográfico).
_DB_TS = "%Y-%m-%d %H:%M:%S"


def _now_db() -> str:
    return datetime.now(timezone.utc).strftime(_DB_TS)


def _to_db_ts(value: str | None) -> str:
    """Convierte cualquier timestamp (ISO-T con/ sin offset, o ya formato-espacio)
    al formato canónico de sessions.db en UTC. Devuelve '' si no parsea."""
    if not value:
        return ""
    s = str(value).strip()
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt.strftime(_DB_TS)
    except Exception:  # noqa: BLE001
        # ¿ya viene en formato-espacio? validarlo
        try:
            return datetime.strptime(s[:19], _DB_TS).strftime(_DB_TS)
        except Exception:  # noqa: BLE001
            return ""


def _conn():
    """Reusa la conexión de session.py (maneja SQLCipher + DDL guard)."""
    from session import _conn as _session_conn  # import diferido: evita ciclos
    return _session_conn()


def _digits(s: str | None) -> str:
    """Normaliza a solo dígitos para comparar teléfonos (events.phone va sin +)."""
    return re.sub(r"\D", "", s or "")


def ensure_table() -> None:
    """DDL idempotente de la tabla del ledger (en sessions.db)."""
    try:
        conn = _conn()
        with conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS agent_action_ledger (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    agent_id     TEXT NOT NULL,
                    action_kind  TEXT,
                    summary      TEXT,
                    risk         TEXT,
                    target_phone TEXT,          -- dígitos canónicos
                    target_raw   TEXT,          -- target original (phone o rut)
                    acted_at     TEXT NOT NULL,  -- ISO UTC del contacto
                    window_days  INTEGER DEFAULT 7,
                    outcome      TEXT DEFAULT 'pendiente',  -- pendiente|cita|respuesta|sin_respuesta
                    outcome_event TEXT,
                    outcome_ts   TEXT,
                    measured_at  TEXT
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ledger_agent ON agent_action_ledger(agent_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ledger_outcome ON agent_action_ledger(outcome)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ledger_acted ON agent_action_ledger(acted_at)")
        conn.close()
    except Exception as e:  # noqa: BLE001 — el ledger nunca debe tumbar nada
        log.warning("ledger.ensure_table falló: %s", e)


def _resolve_phone(target_raw: str | None) -> str:
    """De un target (teléfono o RUT) saca el teléfono en dígitos canónicos."""
    if not target_raw:
        return ""
    raw = str(target_raw).strip()
    # ¿parece RUT? (tiene guión o K, o <8 dígitos puros largos) → resolver vía perfil
    looks_rut = bool(re.search(r"[kK]$", raw)) or "-" in raw
    if looks_rut:
        try:
            from session import get_phone_by_rut
            ph = get_phone_by_rut(raw)
            if ph:
                return _digits(ph)
        except Exception:  # noqa: BLE001
            pass
        return ""
    return _digits(raw)


def record_result(run: dict, window_days: int = DEFAULT_WINDOW_DAYS) -> int:
    """Registra en el ledger cada acción EJECUTADA de contacto a paciente.

    Se llama desde base.run() tras ejecutar. Ignora acciones a staff, sin target
    o que no contactan. Devuelve cuántas filas insertó. Nunca lanza.
    """
    actions = run.get("actions_executed", [])
    if not actions:
        return 0
    rows = []
    acted_at = _to_db_ts(run.get("ran_at")) or _now_db()
    for a in actions:
        if a.get("is_staff") or not a.get("requires_contact"):
            continue
        phone = _resolve_phone(a.get("target"))
        if not phone:
            continue
        rows.append((
            run.get("agent_id", "?"), a.get("kind"), a.get("summary"),
            a.get("risk"), phone, str(a.get("target") or ""),
            acted_at, window_days,
        ))
    if not rows:
        return 0
    try:
        ensure_table()
        conn = _conn()
        with conn:
            conn.executemany(
                """INSERT INTO agent_action_ledger
                   (agent_id, action_kind, summary, risk, target_phone, target_raw,
                    acted_at, window_days, outcome)
                   VALUES (?,?,?,?,?,?,?,?, 'pendiente')""",
                rows)
        conn.close()
        return len(rows)
    except Exception as e:  # noqa: BLE001
        log.warning("ledger.record_result falló: %s", e)
        return 0


def _first_positive(conn, phone: str, start_iso: str, end_iso: str) -> tuple[str, str] | None:
    """Primera señal positiva del paciente en (start, end]. Devuelve (tipo, ts)."""
    # 1) Cita lograda vía conversation_events
    qmarks = ",".join("?" * len(_CITA_EVENTS))
    cur = conn.execute(
        f"""SELECT event, ts FROM conversation_events
            WHERE phone = ? AND event IN ({qmarks})
              AND ts > ? AND ts <= ?
            ORDER BY ts ASC LIMIT 1""",
        (phone, *_CITA_EVENTS, start_iso, end_iso))
    r = cur.fetchone()
    if r:
        return ("cita", r[1])
    # 2) Cita real registrada por el bot (citas_bot.created_at)
    cur = conn.execute(
        """SELECT created_at FROM citas_bot
           WHERE phone = ? AND created_at > ? AND created_at <= ?
           ORDER BY created_at ASC LIMIT 1""",
        (phone, start_iso, end_iso))
    r = cur.fetchone()
    if r:
        return ("cita", r[0])
    # 3) Al menos respondió (mensaje entrante)
    cur = conn.execute(
        """SELECT ts FROM messages
           WHERE phone = ? AND direction = 'in' AND ts > ? AND ts <= ?
           ORDER BY ts ASC LIMIT 1""",
        (phone, start_iso, end_iso))
    r = cur.fetchone()
    if r:
        return ("respuesta", r[0])
    return None


def measure_outcomes(now_iso: str | None = None) -> dict:
    """Resuelve filas 'pendiente' cuya ventana ya venció. Idempotente.

    `now_iso` permite testear con tiempo fijo (Date.now no disponible en algunos
    entornos). Por defecto usa el reloj real.
    """
    now = _to_db_ts(now_iso) or _now_db()
    resolved = {"cita": 0, "respuesta": 0, "sin_respuesta": 0, "revisadas": 0}
    try:
        ensure_table()
        conn = _conn()
        cur = conn.execute(
            "SELECT id, target_phone, acted_at, window_days FROM agent_action_ledger WHERE outcome = 'pendiente'")
        pend = cur.fetchall()
        updates = []
        for rid, phone, acted_at, wdays in pend:
            try:
                start = datetime.strptime(acted_at[:19], _DB_TS)
            except Exception:  # noqa: BLE001
                continue
            end = start + timedelta(days=wdays or DEFAULT_WINDOW_DAYS)
            end_iso = end.strftime(_DB_TS)
            hit = _first_positive(conn, phone, acted_at, min(end_iso, now))
            if hit:
                updates.append((hit[0], hit[0], hit[1], now, rid))
                resolved[hit[0]] += 1
                resolved["revisadas"] += 1
            elif now >= end_iso:
                # ventana vencida sin señal → miss definitivo
                updates.append(("sin_respuesta", None, None, now, rid))
                resolved["sin_respuesta"] += 1
                resolved["revisadas"] += 1
            # si la ventana sigue abierta y no hubo señal: queda pendiente
        if updates:
            with conn:
                conn.executemany(
                    """UPDATE agent_action_ledger
                       SET outcome=?, outcome_event=?, outcome_ts=?, measured_at=?
                       WHERE id=?""",
                    updates)
        conn.close()
    except Exception as e:  # noqa: BLE001
        log.warning("ledger.measure_outcomes falló: %s", e)
    return resolved


def effectiveness_summary(avg_ticket: int = AVG_TICKET_CLP) -> dict:
    """Agrega por agente: contactos, citas, respuestas, conversión, ingreso recup.

    El ingreso recuperable es una ESTIMACIÓN (citas × ticket promedio); no es caja
    real — para caja usar bi_pagos_caja. Sirve para rankear agentes entre sí.
    """
    out = {"avg_ticket_clp": avg_ticket, "per_agent": [], "totales": {}}
    try:
        ensure_table()
        conn = _conn()
        cur = conn.execute(
            """SELECT agent_id,
                      COUNT(*)                                       AS contactos,
                      SUM(outcome='cita')                            AS citas,
                      SUM(outcome='respuesta')                       AS respuestas,
                      SUM(outcome='sin_respuesta')                   AS sin_resp,
                      SUM(outcome='pendiente')                       AS pendientes
               FROM agent_action_ledger
               GROUP BY agent_id
               ORDER BY citas DESC, respuestas DESC""")
        tot_c = tot_cita = tot_resp = 0
        for row in cur.fetchall():
            agent_id, contactos, citas, resp, sinr, pend = row
            citas = citas or 0; resp = resp or 0
            medidos = contactos - (pend or 0)
            conv = round(citas / medidos, 3) if medidos else 0.0
            reply = round((citas + resp) / medidos, 3) if medidos else 0.0
            out["per_agent"].append({
                "agent_id": agent_id,
                "contactos": contactos,
                "citas": citas,
                "respuestas": resp,
                "sin_respuesta": sinr or 0,
                "pendientes": pend or 0,
                "conversion": conv,           # citas / contactos medidos
                "tasa_respuesta": reply,      # (citas+respuestas) / medidos
                "ingreso_recuperable_clp": citas * avg_ticket,
            })
            tot_c += contactos; tot_cita += citas; tot_resp += resp
        conn.close()
        out["totales"] = {
            "contactos": tot_c, "citas": tot_cita, "respuestas": tot_resp,
            "ingreso_recuperable_clp": tot_cita * avg_ticket,
            "conversion_global": round(tot_cita / tot_c, 3) if tot_c else 0.0,
        }
    except Exception as e:  # noqa: BLE001
        log.warning("ledger.effectiveness_summary falló: %s", e)
    return out
