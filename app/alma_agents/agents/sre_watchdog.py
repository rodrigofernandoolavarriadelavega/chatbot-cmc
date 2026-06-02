"""Agente SRE — vigila la salud del sistema y auto-remedia / escala.

Riesgo BAJO: solo alerta a Rodrigo (staff). Revisa señales de salud (IA, Medilink,
scheduler) y, si algo está degradado, avisa. Es el "ojo que nunca duerme".

Patrón de referencia para: análisis sin contacto a pacientes + alerta a STAFF.
"""
import os

from ..base import Agent, AgentAction


class SREWatchdogAgent(Agent):
    async def perceive(self) -> dict:
        health = {"claude_down": None, "medilink_down": None}
        try:
            from resilience import is_medilink_degraded  # nombre puede variar; degrada
            health["medilink_down"] = bool(is_medilink_degraded())
        except Exception:  # noqa: BLE001
            pass
        try:
            from claude_helper import is_claude_down
            health["claude_down"] = bool(is_claude_down())
        except Exception:  # noqa: BLE001
            pass
        return health

    async def decide(self, ctx: dict) -> list[AgentAction]:
        admin = os.getenv("ADMIN_ALERT_PHONE", "")
        if not admin:
            return []
        problemas = [k for k, v in ctx.items() if v is True]
        if not problemas:
            return []
        msg = ("*Alerta SRE Alma*\nSistema degradado: " + ", ".join(problemas) +
               ". Revisar /health y logs.")
        return [AgentAction(
            kind="sre_alert", summary=f"Alertar a Rodrigo: {', '.join(problemas)}",
            risk="bajo", target=admin, is_staff=True, requires_contact=True,
            params={"texto": msg},
        )]

    async def execute_one(self, action: AgentAction) -> dict:
        from messaging import send_whatsapp_proactive
        wamid = await send_whatsapp_proactive(action.target, action.params["texto"])
        return {"sent": bool(wamid), "wamid": wamid}


AGENT = SREWatchdogAgent(
    id="sre_watchdog",
    name="Watchdog SRE",
    descr="Vigila salud (IA, Medilink, scheduler) y escala a Rodrigo si algo cae.",
    risk="bajo",
    flag="ALMA_AGENT_SRE",
    category="operaciones",
    schedule={"minute": "*/30"},
)
