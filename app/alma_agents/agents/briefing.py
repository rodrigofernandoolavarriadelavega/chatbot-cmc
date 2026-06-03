"""Agente Briefing — el analista que le cuenta a Rodrigo cómo va el negocio.

Riesgo BAJO: solo contacta a Rodrigo (staff), no a pacientes. Lee el world-state
del cerebro de Alma y arma un resumen ejecutivo diario por WhatsApp.

Patrón de referencia para: contacto a STAFF + reuso del alma_brain.
"""
import os

from ..base import Agent, AgentAction


class BriefingAgent(Agent):
    async def perceive(self) -> dict:
        try:
            from alma_brain.state import load_state, build_state
            st = load_state() or build_state(7)
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)}
        return st

    async def decide(self, ctx: dict) -> list[AgentAction]:
        admin = os.getenv("ADMIN_ALERT_PHONE", "")
        if not admin or ctx.get("error"):
            return []
        msg = self._render(ctx)
        return [AgentAction(
            kind="briefing_diario",
            summary="Enviar briefing ejecutivo del día a Rodrigo",
            risk="bajo", target=admin, is_staff=True, requires_contact=True,
            params={"texto": msg},
        )]

    def _render(self, st: dict) -> str:
        lines = ["*Briefing Alma — estado del negocio*", ""]
        dom = st.get("domains", {})
        caja = dom.get("caja", {})
        if caja.get("available"):
            k = caja["kpis"]
            lines.append(f"Caja mes: ${k.get('ingreso_mes_clp', 0):,.0f} ({k.get('pagos_mes', 0)} pagos)")
        agenda = dom.get("agenda", {})
        if agenda.get("available"):
            k = agenda["kpis"]
            d = k.get("delta_pct")
            tend = f" ({d:+.0f}% vs semana previa)" if d is not None else ""
            lines.append(f"Citas (7d): {k.get('citas_ventana', 0)}{tend}")
        alerts = st.get("alerts", [])
        if alerts:
            lines.append("")
            lines.append("*Alertas:*")
            for a in alerts[:5]:
                lines.append(f"• {a.get('message', '')}")
        else:
            lines.append("")
            lines.append("Sin alertas relevantes.")
        return "\n".join(lines)

    async def execute_one(self, action: AgentAction) -> dict:
        from messaging import send_whatsapp_proactive
        wamid = await send_whatsapp_proactive(action.target, action.params["texto"])
        return {"sent": bool(wamid), "wamid": wamid}


AGENT = BriefingAgent(
    id="briefing",
    name="Briefing diario",
    descr="Resumen ejecutivo del negocio a Rodrigo cada mañana (lee el cerebro de Alma).",
    risk="bajo",
    flag="ALMA_AGENT_BRIEFING",
    category="inteligencia",
    schedule={"hour": 7, "minute": 30},
)
