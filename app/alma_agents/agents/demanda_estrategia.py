"""Agente Estrategia de Demanda — recomienda contratación / nuevas especialidades.

Riesgo BAJO: solo le propone a Rodrigo (staff). Cruza demanda no satisfecha con
la oferta actual y recomienda decisiones estructurales (abrir especialidad,
contratar, ampliar días). No ejecuta nada operativo: es el estratega.
"""
import os

from ..base import Agent, AgentAction


class DemandaEstrategiaAgent(Agent):
    async def perceive(self) -> dict:
        try:
            from alma_brain.sensors import sense_demanda
            return {"demanda": sense_demanda(90)}
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)}

    async def decide(self, ctx: dict) -> list[AgentAction]:
        admin = os.getenv("ADMIN_ALERT_PHONE", "")
        dem = ctx.get("demanda", {})
        if not admin or not dem.get("available"):
            return []
        sin_cupo = dem.get("servicios_sin_cupo", [])
        no_ofrecemos = dem.get("no_ofrecemos", [])
        umbral = int(os.getenv("ALMA_AGENT_DEMANDA_UMBRAL", "8"))
        oportunidades = [s for s in sin_cupo if s.get("solicitudes", 0) >= umbral]
        nuevas = [n for n in no_ofrecemos if n.get("solicitudes", 0) >= max(3, umbral // 2)]
        if not oportunidades and not nuevas:
            return []
        msg = self._render(oportunidades, nuevas)
        return [AgentAction(
            kind="estrategia_demanda",
            summary="Recomendación de contratación / especialidades a Rodrigo",
            risk="bajo", target=admin, is_staff=True, requires_contact=True,
            params={"texto": msg},
        )]

    def _render(self, opp: list, nuevas: list) -> str:
        lines = ["*Estrategia de demanda (90d)*", ""]
        if opp:
            lines.append("*Reforzar oferta (demanda reprimida):*")
            for s in opp[:5]:
                lines.append(f"• {s.get('especialidad')}: {s.get('solicitudes')} solicitudes "
                             f"de {s.get('personas','?')} personas → evaluar más cupos/días.")
        if nuevas:
            lines.append("")
            lines.append("*Especialidades que no ofrecemos y piden:*")
            for n in nuevas[:5]:
                lines.append(f"• {n.get('especialidad') or n.get('nombre')}: {n.get('solicitudes')} solicitudes.")
        lines.append("")
        lines.append("Decisión estructural — la dejo a tu criterio. Datos de sessions.db.")
        return "\n".join(lines)

    async def execute_one(self, action: AgentAction) -> dict:
        from messaging import send_whatsapp_proactive
        wamid = await send_whatsapp_proactive(action.target, action.params["texto"])
        return {"sent": bool(wamid), "wamid": wamid}


AGENT = DemandaEstrategiaAgent(
    id="demanda_estrategia",
    name="Estrategia de demanda",
    descr="Recomienda contratación y nuevas especialidades según demanda reprimida. Propone a Rodrigo.",
    risk="bajo",
    flag="ALMA_AGENT_DEMANDA_ESTRATEGIA",
    category="inteligencia",
    schedule={"day_of_week": "mon", "hour": 9, "minute": 0},
)
