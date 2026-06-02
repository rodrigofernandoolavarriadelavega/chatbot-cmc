"""Agente Adherencia Kine — nudge a pacientes de kine en riesgo de abandono.

Riesgo ALTO: contacta PACIENTES. Detecta pacientes de kinesiología que llevan
4+ días sin sesión y sin cita futura: cohorte "en riesgo". Reusa directamente
get_kine_candidatos_adherencia() de session.py (misma fuente que
enviar_adherencia_kine() de fidelizacion.py) y _msg_adherencia_kine() de
fidelizacion.py para el texto del nudge.

La distinción con enviar_adherencia_kine() de fidelizacion.py es que este
agente corre bajo el framework Alma Agents (guardrails, consent, dry-run,
panel de control), mientras que el job de fidelizacion.py corre sin ese
envelope. No duplicar el check de consent: guardrails.authorize() lo hace.
"""
import logging

from ..base import Agent, AgentAction

log = logging.getLogger("bot")


class AdherenciaKineAgent(Agent):

    async def perceive(self) -> dict:
        try:
            candidatos = _get_candidatos()
        except Exception as e:  # noqa: BLE001
            log.error("alma_agents[adherencia_kine]: perceive falló: %s", e)
            return {"candidatos": [], "error": str(e)}
        return {"candidatos": candidatos}

    async def decide(self, ctx: dict) -> list[AgentAction]:
        if ctx.get("error"):
            return []
        acciones = []
        for p in ctx.get("candidatos", []):
            phone = p.get("phone", "")
            if not phone:
                continue
            texto = _render_nudge(p)
            acciones.append(AgentAction(
                kind="adherencia_kine_nudge",
                summary=f"Nudge adherencia kine a {phone} (última: {p.get('ultima_fecha', '?')})",
                risk="alto",
                target=phone,
                requires_contact=True,
                params={"texto": texto, "profesional": p.get("profesional", "")},
            ))
        return acciones

    async def execute_one(self, action: AgentAction) -> dict:
        try:
            from messaging import send_whatsapp_proactive
            from session import save_fidelizacion_msg, log_message
            wamid = await send_whatsapp_proactive(action.target, action.params["texto"])
            if wamid:
                save_fidelizacion_msg(action.target, "adherencia_kine")
                log_message(action.target, "out", action.params["texto"][:200], "IDLE")
            return {"sent": bool(wamid), "wamid": wamid}
        except Exception as e:  # noqa: BLE001
            log.error("alma_agents[adherencia_kine]: execute_one falló phone=%s: %s",
                      action.target, e)
            return {"sent": False, "error": str(e)}


# ── Helpers de percepción ────────────────────────────────────────────────────

def _get_candidatos() -> list[dict]:
    """Reutiliza get_kine_candidatos_adherencia de session.py.
    Gap configurable por env var ALMA_KINE_GAP_DIAS (default 4).
    Degrada a [] si session.db no disponible.
    """
    import os
    gap = int(os.getenv("ALMA_KINE_GAP_DIAS", "4"))
    try:
        from session import get_kine_candidatos_adherencia
        return get_kine_candidatos_adherencia(gap_dias=gap)
    except Exception as e:  # noqa: BLE001
        log.warning("alma_agents[adherencia_kine]: get_kine_candidatos_adherencia falló: %s", e)
        return []


def _render_nudge(p: dict) -> str:
    """Texto del nudge de adherencia. Texto plano (send_whatsapp_proactive acepta str).
    Calca el tono de _msg_adherencia_kine de fidelizacion.py sin armar dict interactive
    (el agente usa texto plano para simplificar; el interactivo con botones lo maneja
    el job de fidelizacion cuando corre por su canal propio).
    """
    nombre = (p.get("nombre") or "").strip()
    saludo = f"Hola *{nombre}* " if nombre else "Hola "
    prof = (p.get("profesional") or "").strip()
    prof_label = f" con *{prof}*" if prof else ""
    ultima = p.get("ultima_fecha", "")
    desde_label = f" desde el {ultima}" if ultima else ""

    return (
        f"{saludo}del *Centro Médico Carampangue*.\n\n"
        f"Notamos que no has retomado tu plan de kinesiología{prof_label}"
        f"{desde_label}. "
        "La continuidad en las sesiones es clave para recuperarte.\n\n"
        "Si quieres agendar tu próxima sesión, responde este mensaje y te ayudamos."
    )


AGENT = AdherenciaKineAgent(
    id="adherencia_kine",
    name="Adherencia kinesiología",
    descr=(
        "Detecta pacientes de kine con 4+ días sin sesión y sin cita futura "
        "(cohorte en riesgo) y les envía un nudge de adherencia."
    ),
    risk="alto",
    flag="ALMA_AGENT_ADHERENCIA_KINE",
    category="clinico",
    schedule={"hour": 11, "minute": 30},
)
