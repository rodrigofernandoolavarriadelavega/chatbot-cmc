"""Agente Control Crónico — detecta pacientes con patología crónica cuyo control venció.

Riesgo ALTO: contacta PACIENTES. Lee tags dx:* en sessions.db (contact_tags) y
cruzando con citas_bot detecta a quienes superaron el intervalo de control
recomendado sin volver. Reusa get_control_candidatos() de session.py para las
especialidades con regla fija, y complementa con una query directa a dx:* tags
para capturar casos no cubiertos por especialidad (ej. DM2 controlado por MG).

Patrón de referencia: yield_agenda.py (contacto a PACIENTES) +
_msg_control / enviar_recordatorio_control de fidelizacion.py.
"""
import logging
import os

from ..base import Agent, AgentAction

log = logging.getLogger("bot")

# Días máximos sin control por patología crónica (etiqueta dx:*)
_DIAS_CONTROL_DX: dict[str, int] = {
    "dm2":       90,
    "hta":       90,
    "asma":      90,
    "epoc":      90,
    "hipotiroidismo": 90,
    "obesidad":  90,
    "depresion": 60,
    "ansiedad":  60,
    "dislipidemia": 90,
    "fibromialgia":  60,
}

# Especialidades con regla de control ya cubierta por fidelizacion.py;
# las reutilizamos también aquí para no duplicar contactos.
_ESP_CONTROL = [
    ("Nutrición",         30),
    ("Psicología Adulto", 30),
    ("Cardiología",       90),
    ("Ginecología",       180),
    ("Traumatología",     60),
    ("Medicina General",  90),
    ("Medicina Familiar", 90),
]


class ControlCronicoAgent(Agent):

    async def perceive(self) -> dict:
        try:
            candidatos = _get_candidatos_cronicos()
        except Exception as e:  # noqa: BLE001
            log.error("alma_agents[control_cronico]: perceive falló: %s", e)
            return {"candidatos": [], "error": str(e)}
        return {"candidatos": candidatos}

    async def decide(self, ctx: dict) -> list[AgentAction]:
        if ctx.get("error"):
            return []
        acciones = []
        for c in ctx.get("candidatos", []):
            phone = c.get("phone", "")
            if not phone:
                continue
            nombre = c.get("nombre") or ""
            saludo = f"Hola *{nombre}* " if nombre else "Hola "
            dx_label = c.get("dx_label", "")
            dias = c.get("dias_sin_cita", 0)
            esp = c.get("especialidad", "")

            if dx_label:
                cuerpo = (
                    f"{saludo}del *Centro Médico Carampangue*.\n\n"
                    f"Notamos que llevas *{dias} días* sin un control "
                    f"para tu *{dx_label.upper()}*. "
                    "Mantener el seguimiento regular es clave para tu bienestar.\n\n"
                    "Tenemos horas disponibles esta semana. ¿Quieres agendar?"
                )
            else:
                esp_label = esp or "tu especialidad"
                cuerpo = (
                    f"{saludo}del *Centro Médico Carampangue*.\n\n"
                    f"Tu control de *{esp_label}* ya corresponde hacerlo "
                    f"(han pasado *{dias} días* desde tu última visita).\n\n"
                    "¿Quieres que te ayudemos a agendar tu próximo control?"
                )

            acciones.append(AgentAction(
                kind="control_cronico_outreach",
                summary=f"Recordar control crónico a {phone} (dx={dx_label or esp}, {dias}d)",
                risk="alto",
                target=phone,
                requires_contact=True,
                params={"texto": cuerpo, "dx_label": dx_label, "especialidad": esp},
            ))
        return acciones

    async def execute_one(self, action: AgentAction) -> dict:
        try:
            from messaging import send_whatsapp_proactive
            from session import save_fidelizacion_msg, log_message
            wamid = await send_whatsapp_proactive(action.target, action.params["texto"])
            if wamid:
                save_fidelizacion_msg(action.target, "control_cronico")
                log_message(action.target, "out", action.params["texto"][:200], "IDLE")
            return {"sent": bool(wamid), "wamid": wamid}
        except Exception as e:  # noqa: BLE001
            log.error("alma_agents[control_cronico]: execute_one falló phone=%s: %s",
                      action.target, e)
            return {"sent": False, "error": str(e)}


# ── Helpers de percepción (Medilink-free) ────────────────────────────────────

def _get_candidatos_cronicos(limite: int = 30) -> list[dict]:
    """Combina dos fuentes:
    1. Pacientes con tags dx:* cuya última cita supera el umbral de la patología.
    2. Pacientes de especialidades con regla fija (get_control_candidatos).

    Degrada a [] si session.db no está disponible.
    """
    try:
        from session import _conn, get_control_candidatos
    except Exception:  # noqa: BLE001
        return []

    candidatos: list[dict] = []
    vistos: set[str] = set()

    # Fuente 1 — tags dx:*
    try:
        candidatos_dx = _get_candidatos_por_dx(_conn, _DIAS_CONTROL_DX)
        for c in candidatos_dx:
            if c["phone"] not in vistos:
                vistos.add(c["phone"])
                candidatos.append(c)
    except Exception as e:  # noqa: BLE001
        log.warning("alma_agents[control_cronico]: fuente dx:* falló: %s", e)

    # Fuente 2 — especialidades con regla (reutiliza get_control_candidatos)
    try:
        for esp, dias in _ESP_CONTROL:
            if len(candidatos) >= limite:
                break
            try:
                filas = get_control_candidatos(esp, dias)
            except Exception:  # noqa: BLE001
                continue
            for r in filas:
                phone = r.get("phone", "")
                if phone and phone not in vistos:
                    vistos.add(phone)
                    candidatos.append({
                        "phone": phone,
                        "nombre": r.get("nombre"),
                        "especialidad": esp,
                        "dias_sin_cita": dias,
                        "dx_label": "",
                    })
    except Exception as e:  # noqa: BLE001
        log.warning("alma_agents[control_cronico]: fuente especialidad falló: %s", e)

    return candidatos[:limite]


def _get_candidatos_por_dx(conn_fn, dias_por_dx: dict) -> list[dict]:
    """Pacientes con tag dx:* sin cita reciente y sin recordatorio de control_cronico
    enviado en los últimos 15 días. conn_fn es session._conn (callable)."""
    resultado = []
    try:
        conn = conn_fn()
    except Exception:  # noqa: BLE001
        return resultado
    try:
        for dx_tag, dias_umbral in dias_por_dx.items():
            try:
                rows = conn.execute(
                    """
                    SELECT ct.phone, MAX(cb.fecha) AS ultima_cita, p.nombre
                    FROM contact_tags ct
                    LEFT JOIN citas_bot cb ON cb.phone = ct.phone
                    LEFT JOIN contact_profiles p ON p.phone = ct.phone
                    WHERE ct.tag = ?
                      AND NOT EXISTS (
                          SELECT 1 FROM fidelizacion_msgs f
                          WHERE f.phone = ct.phone AND f.tipo = 'control_cronico'
                            AND f.enviado_en >= datetime('now', '-15 days')
                      )
                    GROUP BY ct.phone
                    HAVING ultima_cita IS NULL
                       OR  ultima_cita <= date('now', ?)
                    """,
                    (f"dx:{dx_tag}", f"-{dias_umbral} days"),
                ).fetchall()
                for r in rows:
                    resultado.append({
                        "phone": r[0],
                        "nombre": r[2],
                        "especialidad": "",
                        "dias_sin_cita": dias_umbral,
                        "dx_label": dx_tag,
                    })
            except Exception:  # noqa: BLE001
                continue
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    return resultado


AGENT = ControlCronicoAgent(
    id="control_cronico",
    name="Control crónico",
    descr=(
        "Detecta pacientes con patologías crónicas (tags dx:*) o especialidades "
        "con seguimiento periódico cuyo control venció, y les recuerda agendar."
    ),
    risk="alto",
    flag="ALMA_AGENT_CONTROL_CRONICO",
    category="clinico",
    schedule={"hour": 11, "minute": 0},
)
