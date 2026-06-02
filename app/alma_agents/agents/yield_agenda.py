"""Agente Yield — llena la agenda contactando demanda reprimida.

Riesgo ALTO: contacta PACIENTES. Detecta especialidades con demanda no
satisfecha reciente (pacientes que pidieron hora y no había cupo) y les avisa
proactivamente que pueden agendar. Cada contacto pasa por el piso completo de
guardrails (consent, horas, presupuesto anti-spam).

Patrón de referencia para: contacto a PACIENTES + candidatos desde sessions.db.
"""
import os

from ..base import Agent, AgentAction


class YieldAgendaAgent(Agent):
    async def perceive(self) -> dict:
        cand = _candidatos_demanda_reprimida(
            dias=int(os.getenv("ALMA_AGENT_YIELD_DIAS", "30")),
            limite=int(os.getenv("ALMA_AGENT_YIELD_MAX", "10")),
        )
        return {"candidatos": cand}

    async def decide(self, ctx: dict) -> list[AgentAction]:
        out = []
        for c in ctx.get("candidatos", []):
            esp = c["especialidad"]
            msg = (f"Hola, te escribimos del Centro Médico Carampangue. Hace poco buscaste "
                   f"hora para {esp} y no había cupo disponible. Ya tenemos nuevas horas "
                   f"abiertas. ¿Quieres que te agendemos? Responde y te ayudamos.")
            out.append(AgentAction(
                kind="yield_outreach",
                summary=f"Avisar a {c['phone']} que hay cupo de {esp}",
                risk="alto", target=c["phone"], requires_contact=True,
                params={"texto": msg, "especialidad": esp},
            ))
        return out

    async def execute_one(self, action: AgentAction) -> dict:
        from messaging import send_whatsapp_proactive
        wamid = await send_whatsapp_proactive(action.target, action.params["texto"])
        return {"sent": bool(wamid), "wamid": wamid, "especialidad": action.params.get("especialidad")}


def _candidatos_demanda_reprimida(dias: int, limite: int) -> list[dict]:
    """Teléfonos que pidieron una especialidad y no encontraron cupo, recientes.
    Lee sessions.db (Medilink-free). Degrada a [] si falla."""
    try:
        from session import _conn
        conn = _conn()
    except Exception:  # noqa: BLE001
        return []
    try:
        cur = conn.execute(
            """
            SELECT phone, lower(json_extract(meta,'$.especialidad')) esp, MAX(ts) ult
            FROM conversation_events
            WHERE event='sin_disponibilidad' AND ts >= datetime('now', ?)
              AND phone IS NOT NULL AND phone != ''
            GROUP BY phone, esp
            ORDER BY ult DESC
            LIMIT ?
            """,
            (f"-{dias} days", limite),
        )
        return [{"phone": r[0], "especialidad": r[1] or "tu especialidad"} for r in cur.fetchall()]
    except Exception:  # noqa: BLE001
        return []
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


AGENT = YieldAgendaAgent(
    id="yield_agenda",
    name="Llenado de agenda",
    descr="Contacta a pacientes con demanda reprimida cuando se abre cupo. Contacta pacientes.",
    risk="alto",
    flag="ALMA_AGENT_YIELD",
    category="ingresos",
    schedule={"hour": 14, "minute": 0},
)
