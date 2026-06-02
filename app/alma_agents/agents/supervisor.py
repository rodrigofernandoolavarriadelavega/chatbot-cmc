"""Agente Supervisor — el meta-agente que vigila a la flota.

Riesgo BAJO: solo le reporta a Rodrigo (staff). Lee el último run de cada agente,
resume la actividad de la flota, y detecta CONFLICTOS — sobre todo el riesgo
agregado: un mismo paciente que aparece como objetivo de varios agentes (señal de
sobre-contacto que el presupuesto individual podría no atrapar si corre en orden).

No apaga agentes solo (eso sigue siendo decisión de Rodrigo vía flags), pero
levanta la mano cuando la flota se comporta raro.
"""
import os

from ..base import Agent, AgentAction
from .. import store


class SupervisorAgent(Agent):
    async def perceive(self) -> dict:
        # Lazy: NO usar registry a nivel de módulo (se importa durante el discover).
        from .. import registry
        runs = {}
        for aid in registry.all_agents():
            if aid == self.id:
                continue
            last = store.load_last_run(aid)
            if last:
                runs[aid] = last
        return {"runs": runs}

    async def decide(self, ctx: dict) -> list[AgentAction]:
        admin = os.getenv("ADMIN_ALERT_PHONE", "")
        runs = ctx.get("runs", {})
        if not admin or not runs:
            return []

        # Conteo de actividad + detección de objetivos repetidos entre agentes.
        targets: dict[str, list[str]] = {}
        total_exec = 0
        for aid, r in runs.items():
            total_exec += len(r.get("actions_executed", []))
            for a in r.get("actions_executed", []) + r.get("actions_proposed", []):
                t = a.get("target")
                if t and not a.get("is_staff"):
                    targets.setdefault(t, [])
                    if aid not in targets[t]:
                        targets[t].append(aid)
        conflictos = {t: ags for t, ags in targets.items() if len(ags) >= 2}

        # Solo reporta si hay conflictos o actividad ejecutada (señal real).
        if not conflictos and total_exec == 0:
            return []

        lines = ["*Supervisor de la flota Alma*", ""]
        lines.append(f"Agentes con run reciente: {len(runs)} · acciones ejecutadas: {total_exec}")
        if conflictos:
            lines.append("")
            lines.append("*Posible sobre-contacto (mismo paciente, varios agentes):*")
            for t, ags in list(conflictos.items())[:8]:
                lines.append(f"• {t}: {', '.join(ags)}")
            lines.append("")
            lines.append("Revisar — el presupuesto anti-spam corta, pero conviene mirar.")
        return [AgentAction(
            kind="supervisor_reporte",
            summary="Reporte de salud de la flota a Rodrigo",
            risk="bajo", target=admin, is_staff=True, requires_contact=True,
            params={"texto": "\n".join(lines), "conflictos": len(conflictos)},
        )]

    async def execute_one(self, action: AgentAction) -> dict:
        from messaging import send_whatsapp_proactive
        wamid = await send_whatsapp_proactive(action.target, action.params["texto"])
        return {"sent": bool(wamid), "wamid": wamid}


AGENT = SupervisorAgent(
    id="supervisor",
    name="Supervisor de la flota",
    descr="Vigila a los demás agentes, detecta sobre-contacto a un mismo paciente y reporta a Rodrigo.",
    risk="bajo",
    flag="ALMA_AGENT_SUPERVISOR",
    category="operaciones",
    schedule={"hour": 20, "minute": 0},
)
