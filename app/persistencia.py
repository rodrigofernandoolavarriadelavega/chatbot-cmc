"""persistencia.py — Carril de persistencia del agendamiento (2026-07-13).

Objetivo del dueño: "que ningún paciente que pregunta se quede sin agendar".

TODO GATED OFF por defecto (`PERSISTENCIA_ACTIVE=false`). El dueño lo enciende
cuando lo revise — este módulo no cambia ningún comportamiento hasta entonces.

## Diagnóstico que motiva este diseño (ver docs/PERSISTENCIA_EMBUDO_2026-07.md)

1. La medición de intención estaba rota (evento `intent_agendar` solo se
   logueaba en 1 de ~84 puntos de entrada al agendamiento). Se corrigió en
   `flows.py::_iniciar_agendar` (chokepoint único) con el evento
   `funnel_intent_agendar` + un `_funnel_id` que viaja en `data` y se propaga
   a `funnel_especialidad`/`funnel_slot_ofrecido`/`funnel_slot_elegido`/
   `funnel_confirmacion`/`cita_creada`. Esa instrumentación es la base de este
   módulo (`sync_consultas` usa `funnel_intent_agendar` como apertura).
2. El embudo real reconstruido desde `messages.state` (30 días, prod):
   1051 entraron → 960 vieron slots → 579 llegaron a confirmar → 582 citas.
   El agujero más grande NO es "no hay hora hoy" (eso son solo 45 casos/30d):
   es que 394 personas vieron un horario y nunca lo confirmaron, y de esas,
   230 se quedaron en silencio total (nunca volvieron a escribir).
3. BUG DE RAÍZ encontrado y corregido en `session.py::phone_tiene_solo_citas_canceladas`:
   el guard que evita reenganchar a alguien "cuya cita ya se canceló" también
   atrapaba (por diseño, "o ninguna") a cualquier paciente que JAMÁS tuvo una
   cita — que es exactamente el 100% de la gente que abandona en WAIT_SLOT.
   Resultado medido: 214 de los 230 silencios puros (93%) caían en esa rama y
   jamás recibían el reenganche que el cron de 5 min ya está diseñado para
   mandarles. Con el fix, el reenganche EXISTENTE (`jobs._enviar_reenganche`)
   debería empezar a cubrir la enorme mayoría de este agujero SOLO.

## Qué agrega este módulo (por eso sigue habiendo un carril nuevo)

El reenganche existente da UN toque entre 10 y 90 minutos, dentro de la
ventana de 24h (texto libre). Este módulo agrega lo que falta y el dueño
pidió explícitamente:

  - Una máquina de estados POR CONSULTA (no por sesión), con cierre
    garantizado: ABIERTA → CONTACTADA → AGENDADA | NO_EXPLICITO | EXPIRADA.
    Ninguna consulta queda abierta para siempre (ver `sync_consultas`).
  - Un SEGUNDO toque, deliberadamente tardío (>=2h desde que se abrió la
    consulta) y único (máximo 1 por consulta), para quien el primer toque
    (reenganche o followup_info) no rescató. Comparte el presupuesto de
    contacto UNIFICADO (`contact_budget.py` — no crea uno nuevo) con todos
    los demás rieles proactivos del bot.
  - Detección explícita de "no" / "no me escriban" → cierre inmediato como
    NO_EXPLICITO (éxito del carril, no fracaso) y nunca más se contacta esa
    consulta.
  - Instrumentación del "no hay hora para HOY": si el paciente pidió hoy y
    no había, se le ofrece anotarse en la lista de espera same-day (reusa
    `add_to_waitlist` + el cron de waitlist ya existente, `lista_espera_cupo`
    template ya aprobado) en vez de dejarlo solo con el disclaimer.

## Lo que NO hace (deliberado)

  - NO manda un tercer toque. Dos toques (reenganche/followup_info + este) y
    se acabó — silencio se convierte en EXPIRADA, no en más mensajes.
  - NO usa una plantilla MARKETING para esto: es seguimiento TRANSACCIONAL de
    algo que el paciente pidió (Ley 21.719 — no requiere opt-in de marketing).
    Si la ventana de 24h está cerrada y no hay plantilla UTILITY aprobada
    para este caso específico, NO envía nada — solo lo deja registrado
    (`persistencia_template_pendiente`) para que el dueño suba la plantilla
    `seguimiento_consulta_pendiente` (borrador en
    `templates/whatsapp_templates/seguimiento_consulta_pendiente.DRAFT.json`)
    a Meta. Enviar por fuera del catálogo aprobado arriesga la calidad GREEN
    del número — no se hace.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

log = logging.getLogger("persistencia")

_ESTADOS = ("abierta", "contactada", "agendada", "no_explicito", "expirada")

# Palabras de stop explícito — mismo criterio que usa el resto del bot
# (reenganche_optout, waitlist "No, gracias", etc.) para no reinventar reglas.
_STOP_KW = (
    "no gracias", "no, gracias", "no me interesa", "no quiero", "no por ahora",
    "ahora no", "no me escriban", "no me escribas", "dejen de escribir",
    "stop", "no molestar", "ya no", "no necesito", "cancela", "olvidalo",
    "olvídalo", "no me llames", "no me contacten",
)


def _active() -> bool:
    try:
        from autopilot import flags  # type: ignore
        if flags.flag_on("PERSISTENCIA_ACTIVE"):
            return True
    except Exception:  # noqa: BLE001
        pass
    return os.getenv("PERSISTENCIA_ACTIVE", "false").strip().lower() in ("1", "true", "yes", "on")


def _ensure_table(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS consultas_persistencia (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            funnel_id       TEXT UNIQUE,
            phone           TEXT NOT NULL,
            especialidad    TEXT,
            canal           TEXT DEFAULT 'wa',
            opened_at       TEXT NOT NULL,
            estado          TEXT NOT NULL DEFAULT 'abierta',
            intentos        INTEGER NOT NULL DEFAULT 0,
            ultimo_intento_at TEXT,
            motivo_cierre   TEXT,
            closed_at       TEXT,
            updated_at      TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_consultas_persistencia_estado "
                 "ON consultas_persistencia(estado)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_consultas_persistencia_phone "
                 "ON consultas_persistencia(phone)")
    conn.commit()


def _canal_de(phone: str) -> str:
    if phone.startswith("ig_"):
        return "ig"
    if phone.startswith("fb_"):
        return "fb"
    return "wa"


# ── Bookkeeping puro (cero contacto) ───────────────────────────────────────

def sync_consultas(dias: int = 3) -> dict:
    """Abre/cierra registros de `consultas_persistencia` a partir de eventos
    ya existentes. NO envía ningún mensaje — es solo contabilidad. Seguro de
    correr siempre que el flag maestro esté ON, independiente del contacto.
    """
    from session import db

    abiertas = cerradas_agendada = cerradas_no_explicito = cerradas_expirada = 0
    with db() as conn:
        _ensure_table(conn)

        # 1) Abrir nuevas desde funnel_intent_agendar (instrumentación nueva).
        rows = conn.execute(
            "SELECT phone, meta, ts FROM conversation_events "
            "WHERE event='funnel_intent_agendar' AND ts >= datetime('now', ?)",
            (f"-{int(dias)} day",),
        ).fetchall()
        for phone, meta, ts in rows:
            try:
                d = json.loads(meta) if meta else {}
            except Exception:
                d = {}
            fid = d.get("funnel_id")
            if not fid:
                continue
            existe = conn.execute(
                "SELECT 1 FROM consultas_persistencia WHERE funnel_id=?", (fid,)
            ).fetchone()
            if existe:
                continue
            conn.execute(
                "INSERT INTO consultas_persistencia "
                "(funnel_id, phone, especialidad, canal, opened_at, estado) "
                "VALUES (?, ?, ?, ?, ?, 'abierta')",
                (fid, phone, d.get("especialidad_pedida", ""), _canal_de(phone), ts),
            )
            abiertas += 1
        conn.commit()

        # 2) Cerrar como AGENDADA: cita_creada con el mismo funnel_id, o (red
        #    de seguridad) cualquier cita del mismo phone creada después de
        #    abierta la consulta — cubre funnel_id que no se propagó por
        #    algún camino de agendamiento no contemplado.
        abiertas_rows = conn.execute(
            "SELECT id, funnel_id, phone, opened_at FROM consultas_persistencia "
            "WHERE estado IN ('abierta','contactada')"
        ).fetchall()
        for cid, fid, phone, opened_at in abiertas_rows:
            cita = conn.execute(
                "SELECT 1 FROM conversation_events WHERE event='cita_creada' "
                "AND phone=? AND meta LIKE ? LIMIT 1",
                (phone, f'%"funnel_id": "{fid}"%'),
            ).fetchone()
            if not cita:
                cita = conn.execute(
                    "SELECT 1 FROM citas_bot WHERE phone=? AND created_at >= ? LIMIT 1",
                    (phone, opened_at),
                ).fetchone()
            if cita:
                conn.execute(
                    "UPDATE consultas_persistencia SET estado='agendada', "
                    "motivo_cierre='cita_creada', closed_at=datetime('now'), "
                    "updated_at=datetime('now') WHERE id=?", (cid,))
                cerradas_agendada += 1
        conn.commit()

        # 3) Cerrar como NO_EXPLICITO: mensaje entrante con stop-keyword
        #    DESPUÉS de abierta la consulta. Es un ÉXITO del carril, no un
        #    fracaso — se respeta y no se vuelve a contactar por este motivo.
        pendientes = conn.execute(
            "SELECT id, phone, opened_at FROM consultas_persistencia "
            "WHERE estado IN ('abierta','contactada')"
        ).fetchall()
        for cid, phone, opened_at in pendientes:
            msgs = conn.execute(
                "SELECT text FROM messages WHERE phone=? AND direction='in' "
                "AND ts >= ? ORDER BY ts ASC", (phone, opened_at)
            ).fetchall()
            for (txt,) in msgs:
                tl = (txt or "").strip().lower()
                if any(k in tl for k in _STOP_KW):
                    conn.execute(
                        "UPDATE consultas_persistencia SET estado='no_explicito', "
                        "motivo_cierre='paciente_dijo_no', closed_at=datetime('now'), "
                        "updated_at=datetime('now') WHERE id=?", (cid,))
                    cerradas_no_explicito += 1
                    break
        conn.commit()

        # 4) Expirar lo que lleva demasiado tiempo abierto/contactado sin
        #    resolución — cierra el lazo, nunca queda "para siempre".
        max_dias_abierta = int(os.getenv("PERSISTENCIA_MAX_DIAS_ABIERTA", "3"))
        cur = conn.execute(
            "UPDATE consultas_persistencia SET estado='expirada', "
            "motivo_cierre='timeout', closed_at=datetime('now'), "
            "updated_at=datetime('now') "
            "WHERE estado IN ('abierta','contactada') "
            "AND opened_at < datetime('now', ?)",
            (f"-{max_dias_abierta} day",),
        )
        cerradas_expirada = cur.rowcount or 0
        conn.commit()

    return {
        "abiertas_nuevas": abiertas,
        "cerradas_agendada": cerradas_agendada,
        "cerradas_no_explicito": cerradas_no_explicito,
        "cerradas_expirada": cerradas_expirada,
    }


# ── Contacto (el único código de este módulo que envía mensajes) ──────────

async def job_persistencia_contacto() -> dict:
    """Segundo toque, único, para consultas 'abierta' que ya pasaron la
    ventana del reenganche existente (>=2h) sin resolverse. Gated OFF por
    `PERSISTENCIA_ACTIVE`. Respeta contact_budget, phones_with_open_offers,
    horas de silencio y HUMAN_TAKEOVER.
    """
    if not _active():
        return {"active": False}

    sync_result = sync_consultas()

    from session import db, phones_with_open_offers, get_session
    from contact_budget import can_contact, record_contact, in_quiet_hours
    import contact_budget

    if in_quiet_hours():
        return {"active": True, "skipped": "horas_silencio", **sync_result}

    min_horas = float(os.getenv("PERSISTENCIA_TOQUE2_MIN_HORAS", "2"))
    max_horas = float(os.getenv("PERSISTENCIA_TOQUE2_MAX_HORAS", "26"))

    n_contactados = n_skip_budget = n_skip_offer = n_skip_takeover = n_skip_window = 0

    with db() as conn:
        _ensure_table(conn)
        candidatas = conn.execute(
            "SELECT id, funnel_id, phone, especialidad, opened_at FROM consultas_persistencia "
            "WHERE estado='abierta' AND intentos=0 "
            "AND opened_at <= datetime('now', ?) AND opened_at >= datetime('now', ?)",
            (f"-{min_horas} hours", f"-{max_horas} hours"),
        ).fetchall()

    ocupados = phones_with_open_offers()

    for cid, fid, phone, especialidad, opened_at in candidatas:
        if phone in ocupados:
            n_skip_offer += 1
            continue
        sesion_actual = get_session(phone)
        if sesion_actual.get("state") == "HUMAN_TAKEOVER":
            n_skip_takeover += 1
            continue
        # Si ya avanzó a otro estado de agendamiento (retomó solo), no insistir.
        if sesion_actual.get("state") not in ("IDLE", "WAIT_SLOT", "WAIT_ESPECIALIDAD"):
            continue

        allow, motivo = can_contact(phone, rail="persistencia")
        if not allow:
            n_skip_budget += 1
            continue

        # Regla del dueño (2026-08-05): mirar el HIS antes del toque 2 — si el
        # paciente ya agendó por teléfono con recepción (no pasa por el bot),
        # "¿sigues necesitando la hora?" lo confunde y puede hacerlo agendar
        # DE NUEVO (caso Alexander: terminó con cita duplicada el viernes).
        from jobs import verificar_cita_externa
        _cita_ext = await verificar_cita_externa(phone)
        if _cita_ext == "tiene":
            from session import log_event as _le_ext
            _le_ext(phone, "persistencia_skip_cita_externa", {"funnel_id": fid})
            with db() as conn:
                conn.execute(
                    "UPDATE consultas_persistencia SET estado='agendada', "
                    "motivo_cierre='cita_externa_medilink', closed_at=datetime('now'), "
                    "updated_at=datetime('now') WHERE id=?", (cid,))
                conn.commit()
            continue
        if _cita_ext == "error":
            continue  # Medilink no respondió — reintenta el próximo ciclo

        from session import is_window_open, log_event, log_message, save_session

        esp_txt = f" de *{especialidad}*" if especialidad else ""
        nombre = ""
        try:
            from session import get_profile
            perfil = get_profile(phone)
            if perfil and perfil.get("nombre"):
                nombre = str(perfil["nombre"]).split()[0]
        except Exception:
            pass
        saludo = f"*{nombre}*, " if nombre else ""

        enviado = False
        if is_window_open(phone):
            from flows import _btn_msg
            from messaging import send_whatsapp_interactive
            msg = (
                f"Hola {saludo}👋 Hace un rato estabas viendo horas{esp_txt} "
                "en el Centro Médico Carampangue. ¿Sigues necesitando la hora?"
            )
            try:
                bt = _btn_msg(msg, [
                    {"id": "menu", "title": "Sí, ver horas"},
                    {"id": "no_gracias_reeng", "title": "No, gracias"},
                ])
                await send_whatsapp_interactive(phone, bt["interactive"])
                log_message(phone, "out", msg, sesion_actual.get("state", "IDLE"))
                enviado = True
            except Exception as e:  # noqa: BLE001
                log.warning("persistencia: envío texto libre falló phone=%s: %s", phone, e)
        else:
            # Ventana cerrada: se requiere plantilla UTILITY aprobada. Todavía
            # no existe (`seguimiento_consulta_pendiente`, borrador en
            # templates/whatsapp_templates/). NO se envía nada — solo se
            # registra para que el dueño la suba a Meta y active USE_TEMPLATES.
            log_event(phone, "persistencia_template_pendiente",
                      {"funnel_id": fid, "especialidad": especialidad})
            n_skip_window += 1
            with db() as conn:
                conn.execute(
                    "UPDATE consultas_persistencia SET intentos=intentos+1, "
                    "ultimo_intento_at=datetime('now'), updated_at=datetime('now') "
                    "WHERE id=?", (cid,))
                conn.commit()
            continue

        if enviado:
            record_contact(phone, "persistencia", {"funnel_id": fid, "especialidad": especialidad})
            log_event(phone, "persistencia_toque2_enviado",
                      {"funnel_id": fid, "especialidad": especialidad})
            with db() as conn:
                conn.execute(
                    "UPDATE consultas_persistencia SET estado='contactada', intentos=intentos+1, "
                    "ultimo_intento_at=datetime('now'), updated_at=datetime('now') "
                    "WHERE id=?", (cid,))
                conn.commit()
            n_contactados += 1

    return {
        "active": True,
        "contactados": n_contactados,
        "skip_budget": n_skip_budget,
        "skip_oferta_viva": n_skip_offer,
        "skip_takeover": n_skip_takeover,
        "skip_ventana_cerrada_sin_template": n_skip_window,
        **sync_result,
    }


# ── Métricas para el panel del dueño (reusa el patrón de mg_abandono_routes) ─

def medir_funnel_persistencia(dias: int = 30) -> dict:
    """Números duros: cuántas consultas se abrieron, en qué terminaron, y el
    % de resolución. Pensado para exponerse en /api/persistencia (mismo
    patrón de auth que /api/mg-abandono) y embeberse en /admin/v2."""
    from session import db

    with db() as conn:
        _ensure_table(conn)
        total = conn.execute(
            "SELECT COUNT(*) FROM consultas_persistencia WHERE opened_at >= datetime('now', ?)",
            (f"-{int(dias)} day",)).fetchone()[0]
        por_estado = dict(conn.execute(
            "SELECT estado, COUNT(*) FROM consultas_persistencia "
            "WHERE opened_at >= datetime('now', ?) GROUP BY estado",
            (f"-{int(dias)} day",)).fetchall())
        abiertas_sin_resolver = por_estado.get("abierta", 0) + por_estado.get("contactada", 0)
        agendadas = por_estado.get("agendada", 0)
        pct_resuelto = (agendadas / total * 100) if total else 0.0
        return {
            "dias": dias,
            "total_consultas": total,
            "por_estado": por_estado,
            "abiertas_sin_resolver": abiertas_sin_resolver,
            "pct_terminaron_agendadas": round(pct_resuelto, 1),
            "active": _active(),
        }
