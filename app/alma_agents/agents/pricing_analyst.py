"""Agente Pricing — analiza la caja real y propone ajustes de arancel.

Riesgo MEDIO: solo le propone a Rodrigo (staff), no cambia precios solos (precio
es decisión del dueño). Cruza ingreso por especialidad con los aranceles de
referencia y marca dónde el ticket promedio se aleja del arancel (posible fuga
de copago, exceso de descuento, o espacio para subir).
"""
import os

from ..base import Agent, AgentAction


class PricingAnalystAgent(Agent):
    async def perceive(self) -> dict:
        ctx = {"por_prof": [], "aranceles": {}}
        try:
            from alma_brain.sensors import sense_caja
            caja = sense_caja(30)
            if caja.get("available"):
                ctx["por_prof"] = caja.get("por_profesional_mes", [])
        except Exception:  # noqa: BLE001
            pass
        try:
            from config import ARANCELES_CLP
            ctx["aranceles"] = dict(ARANCELES_CLP)
        except Exception:  # noqa: BLE001
            pass
        return ctx

    async def decide(self, ctx: dict) -> list[AgentAction]:
        admin = os.getenv("ADMIN_ALERT_PHONE", "")
        por_prof = ctx.get("por_prof", [])
        if not admin or not por_prof:
            return []
        # Heurística simple: profesionales con ticket promedio muy bajo (posible
        # fuga de copago / exceso de gratuidad) merecen una mirada.
        hallazgos = []
        for p in por_prof:
            n = p.get("pagos", 0)
            ingreso = p.get("ingreso_clp", 0)
            if n >= 5:
                ticket = ingreso / n
                if ticket < int(os.getenv("ALMA_AGENT_PRICING_TICKET_MIN", "8000")):
                    hallazgos.append((p.get("profesional"), round(ticket), n))
        if not hallazgos:
            return []
        lines = ["*Pricing — ticket promedio bajo (mes)*", "",
                 "Revisar si hay fuga de copago o exceso de descuento:"]
        for prof, ticket, n in hallazgos[:8]:
            lines.append(f"• {prof}: ticket ${ticket:,.0f} en {n} pagos.")
        lines.append("")
        lines.append("Ajuste de precio = tu decisión. Solo te marco el dato.")
        return [AgentAction(
            kind="pricing_alerta",
            summary="Profesionales con ticket promedio bajo → revisar pricing/copago",
            risk="medio", target=admin, is_staff=True, requires_contact=True,
            params={"texto": "\n".join(lines)},
        )]

    async def execute_one(self, action: AgentAction) -> dict:
        from messaging import send_whatsapp_proactive
        wamid = await send_whatsapp_proactive(action.target, action.params["texto"])
        return {"sent": bool(wamid), "wamid": wamid}


AGENT = PricingAnalystAgent(
    id="pricing_analyst",
    name="Analista de pricing",
    descr="Detecta fugas de copago / ticket bajo por profesional. Propone a Rodrigo, no cambia precios.",
    risk="medio",
    flag="ALMA_AGENT_PRICING",
    category="inteligencia",
    schedule={"day_of_week": "fri", "hour": 18, "minute": 0},
)
