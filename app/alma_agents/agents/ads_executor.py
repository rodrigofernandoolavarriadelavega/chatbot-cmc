"""Agente Ads Executor — ejecuta las decisiones de presupuesto del Autopilot.

Riesgo ALTO: mueve plata en Meta Ads. NO contacta pacientes. Lee el snapshot
del Autopilot (ya calculado) y aplica los ajustes de presupuesto que no
requieren aprobacion humana y estan dentro de los HardLimits.

Fuentes reusadas:
  - autopilot.world_state.load_snapshot     → ultimo snapshot guardado
  - autopilot.engine.run_dry_run            → refresh fresco si no hay snapshot
  - autopilot.policy.HardLimits.from_env   → mismos topes del autopilot
  - autopilot.meta_ads.set_adset_budget     → cambia presupuesto via Marketing API
  - autopilot.meta_ads.pause_adset          → pausa campaña via Marketing API
"""
import logging
import os

import httpx

from ..base import Agent, AgentAction

log = logging.getLogger("bot")

# Solo ejecuta acciones por debajo de este porcentaje sin pedir aprobacion.
# Coincide con HardLimits.approval_threshold_pct (0.20 por defecto).
_AUTO_THRESHOLD = float(os.getenv("AUTOPILOT_APPROVAL_PCT", "0.20"))


class AdsExecutorAgent(Agent):

    async def perceive(self) -> dict:
        """Lee el snapshot del Autopilot. Si es del dia de hoy, lo reutiliza.
        Si esta desactualizado o no existe, corre un dry-run fresco."""
        try:
            from autopilot.world_state import load_snapshot
            snap = load_snapshot()
        except Exception as e:  # noqa: BLE001
            log.warning("ads_executor perceive: load_snapshot fallo (%s)", e)
            snap = None

        if snap and snap.get("world_state") and snap.get("actions"):
            return {"snapshot": snap, "source": "cache"}

        # Snapshot no existe o esta incompleto → refresh dry-run
        try:
            from autopilot.engine import run_dry_run
            run = await run_dry_run(window_days=7)
            return {
                "snapshot": {
                    "world_state": run.ws.to_dict(),
                    "actions": [
                        {
                            "campaign_id": a.campaign_id,
                            "campaign_name": a.campaign_name,
                            "action": a.action,
                            "reason": a.reason,
                            "current_budget_clp": a.current_budget_clp,
                            "proposed_budget_clp": a.proposed_budget_clp,
                            "confidence": a.confidence,
                            "needs_approval": a.needs_approval,
                        }
                        for a in run.actions
                    ],
                    "limits": {
                        "max_step_pct": run.limits.max_step_pct,
                        "max_daily_budget_clp": run.limits.max_daily_budget_clp,
                        "min_daily_budget_clp": run.limits.min_daily_budget_clp,
                    },
                },
                "source": "fresh_dry_run",
            }
        except Exception as e:  # noqa: BLE001
            log.warning("ads_executor perceive: run_dry_run fallo (%s)", e)
            return {}

    async def decide(self, ctx: dict) -> list[AgentAction]:
        snap = ctx.get("snapshot")
        if not snap:
            return []
        actions_raw = snap.get("actions", [])
        limits_raw = snap.get("limits", {})
        max_step = limits_raw.get("max_step_pct", _AUTO_THRESHOLD)

        out = []
        for a in actions_raw:
            action_type = a.get("action", "")
            needs_approval = a.get("needs_approval", True)
            confidence = a.get("confidence", 0.0)
            cid = a.get("campaign_id", "")
            name = a.get("campaign_name", cid)
            current = a.get("current_budget_clp")
            proposed = a.get("proposed_budget_clp")

            # Solo procesa acciones que mueven plata y no necesitan aprobacion
            if action_type not in ("increase_budget", "decrease_budget", "pause"):
                continue
            if needs_approval:
                continue
            if confidence < 0.5:
                continue

            if action_type == "pause":
                out.append(AgentAction(
                    kind="ads_budget_change",
                    summary=f"Pausar campana {name} (motivo: {a.get('reason', '')})",
                    risk="alto",
                    target=cid,
                    params={
                        "campaign_id": cid,
                        "campaign_name": name,
                        "direction": "pause",
                        "step_pct": 0,
                        "current_budget_clp": current,
                        "proposed_budget_clp": None,
                        "reason": a.get("reason", ""),
                    },
                    requires_contact=False,
                    requires_medilink_write=False,
                ))
            elif action_type in ("increase_budget", "decrease_budget") and current and proposed:
                step_pct = abs(proposed - current) / current if current else 0
                if step_pct > max_step:
                    continue  # excede limite duro → esperar aprobacion
                direction = "increase" if action_type == "increase_budget" else "decrease"
                out.append(AgentAction(
                    kind="ads_budget_change",
                    summary=(
                        f"{direction.capitalize()} presupuesto {name}: "
                        f"${current:,.0f} → ${proposed:,.0f} ({step_pct*100:.0f}%)"
                    ),
                    risk="alto",
                    target=cid,
                    params={
                        "campaign_id": cid,
                        "campaign_name": name,
                        "direction": direction,
                        "step_pct": round(step_pct, 4),
                        "current_budget_clp": current,
                        "proposed_budget_clp": proposed,
                        "reason": a.get("reason", ""),
                    },
                    requires_contact=False,
                    requires_medilink_write=False,
                ))
        return out

    async def execute_one(self, action: AgentAction) -> dict:
        p = action.params
        direction = p.get("direction")
        cid = p.get("campaign_id", "")
        reason = p.get("reason", "ads_executor")

        try:
            from autopilot import meta_ads
            async with httpx.AsyncClient() as client:
                if direction == "pause":
                    result = await meta_ads.pause_adset(client, cid, reason=reason)
                elif direction in ("increase", "decrease"):
                    proposed = int(p.get("proposed_budget_clp", 0))
                    if not proposed:
                        return {"error": "proposed_budget_clp vacio"}
                    result = await meta_ads.set_adset_budget(
                        client, cid, proposed, reason=reason
                    )
                else:
                    return {"error": f"direction desconocida: {direction}"}
            log.info("ads_executor: %s %s → %s", direction, cid, result)
            return {"ok": True, "meta_response": result, "direction": direction, "campaign_id": cid}
        except Exception as e:  # noqa: BLE001
            log.error("ads_executor execute_one fallo: %s", e)
            return {"ok": False, "error": str(e)}


AGENT = AdsExecutorAgent(
    id="ads_executor",
    name="Executor de Ads",
    descr=(
        "Aplica los ajustes de presupuesto que el Autopilot ya propuso "
        "y que no necesitan aprobacion humana. No contacta pacientes."
    ),
    risk="alto",
    flag="ALMA_AGENT_ADS_EXECUTOR",
    category="marketing",
    schedule={"hour": 9, "minute": 0},
)
