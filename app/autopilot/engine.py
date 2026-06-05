"""Fase 1 — Orquestador del bucle (modo dry-run).

Une las piezas: medir (world_state) → decidir (policy) → opinar lo ambiguo
(advisor) → reportar. En Fase 1 NO ejecuta ninguna acción: produce un reporte
de lo que haría y lo deja en log + (opcional) WhatsApp para que Rodrigo apruebe.

La ejecución real (Fase 2) reutilizará `actions` filtrando por
needs_approval / confianza y aplicándolas vía meta_ads (con AUTOPILOT_EXECUTE).
"""
import logging
import os
from dataclasses import dataclass, field

from .world_state import build_world_state, WorldState
from .policy import decide, HardLimits, ProposedAction, ActionType
from . import advisor

log = logging.getLogger("bot")


@dataclass
class AutopilotRun:
    ws: WorldState
    actions: list[ProposedAction]
    limits: HardLimits
    report_md: str = ""


async def run_dry_run(window_days: int = 7) -> AutopilotRun:
    """Corrida completa sin ejecutar. Devuelve el run con el reporte listo."""
    limits = HardLimits.from_env()
    ws = await build_world_state(window_days=window_days)
    actions = decide(ws, limits)
    actions = await advisor.advise(ws, actions, limits)
    report = render_report(ws, actions, limits)

    _append_decision_log(report)
    _save_snapshot(ws, actions, limits)
    log.info("autopilot dry-run completo: %d campañas, %d acciones que mueven plata",
             len(ws.campaigns),
             sum(1 for a in actions if a.action in (ActionType.INCREASE, ActionType.DECREASE,
                                                    ActionType.PAUSE)))

    # Fase 2 — encolar las acciones que mueven plata y avisar al dueño para su OK.
    # Inerte salvo AUTOPILOT_ENABLED=true (enqueue/notify chequean el flag adentro).
    # NO ejecuta nada: solo deja pendientes que el dueño aprueba desde el dashboard.
    try:
        from . import approvals
        new_ids = approvals.enqueue(actions)
        if new_ids:
            approvals.notify_owner(new_ids)
    except Exception as e:  # noqa: BLE001 — nunca tumbar el run por la cola de aprobación
        log.error("[autopilot] enqueue/notify falló: %s", e)

    return AutopilotRun(ws=ws, actions=actions, limits=limits, report_md=report)


_ICON = {
    ActionType.INCREASE: "⬆️",
    ActionType.DECREASE: "⬇️",
    ActionType.PAUSE: "⏸️",
    ActionType.KEEP: "▶️",
    ActionType.ALERT: "⚠️",
}


def render_report(ws: WorldState, actions: list[ProposedAction], limits: HardLimits) -> str:
    """Reporte legible de la corrida (markdown)."""
    lines = []
    lines.append(f"# Autopilot CMC — dry-run ({ws.date_from} → {ws.date_to}, {ws.window_days}d)")
    lines.append("")
    lines.append(f"- Campañas activas: **{len(ws.campaigns)}**")
    lines.append(f"- Gasto total: **${ws.total_spend:,.0f}**")
    if ws.bi_available:
        lines.append(f"- Ingreso real (caja BI): **${ws.bi_revenue_real:,.0f}** "
                     f"({ws.bi_pagos_count} pagos)")
        if ws.attribution_ratio is not None:
            estado = ("Meta inflado" if ws.attribution_ratio < 0.7
                      else "sub-atribución/otros canales" if ws.attribution_ratio > 1.3
                      else "consistente")
            lines.append(f"- Ratio atribución BI/Meta: **{ws.attribution_ratio:.2f}** ({estado})")
    else:
        lines.append("- Auditor BI: _no disponible_ (decidiendo solo con señal Meta)")
    lines.append("")
    lines.append("## Acciones propuestas")
    lines.append("")

    # Ordenar: primero lo que mueve plata, luego alertas, luego keep.
    order = {ActionType.INCREASE: 0, ActionType.DECREASE: 1, ActionType.PAUSE: 2,
             ActionType.ALERT: 3, ActionType.KEEP: 4}
    for a in sorted(actions, key=lambda x: order.get(x.action, 9)):
        icon = _ICON.get(a.action, "•")
        tag = " 🔒 requiere aprobación" if a.needs_approval else ""
        bud = ""
        if a.proposed_budget_clp is not None and a.current_budget_clp is not None \
                and a.proposed_budget_clp != a.current_budget_clp:
            bud = f" · ${a.current_budget_clp:,.0f} → ${a.proposed_budget_clp:,.0f}/día"
        lines.append(f"### {icon} {a.campaign_name}{tag}")
        lines.append(f"- {a.reason}{bud}")
        m = a.metrics
        lines.append(f"- spend ${m.get('spend', 0):,.0f} · purchases {m.get('purchases', 0):.0f} "
                     f"· CAC {_fmt_cac(m.get('cac_purchase'))} · ROAS {m.get('roas_meta', 0):.1f}x "
                     f"· confianza {a.confidence:.0%}")
        lines.append("")

    lines.append("---")
    lines.append(f"_Límites duros: paso ±{limits.max_step_pct:.0%} · "
                 f"presupuesto ${limits.min_daily_budget_clp:,}–${limits.max_daily_budget_clp:,}/ad set · "
                 f"techo total ${limits.max_total_daily_clp:,}/día. "
                 f"Modo: **dry-run** (no se ejecutó ningún cambio)._")
    return "\n".join(lines)


def _fmt_cac(v) -> str:
    return f"${v:,.0f}" if v else "n/d"


def _save_snapshot(ws: WorldState, actions: list[ProposedAction], limits: HardLimits) -> None:
    """Persiste el run como JSON estructurado para el dashboard (sin golpear Meta)."""
    from .world_state import save_snapshot
    import os
    payload = {
        "generated_at": ws.date_to,  # fecha del run (sin Date.now por reproducibilidad)
        "window_days": ws.window_days,
        "date_from": ws.date_from,
        "date_to": ws.date_to,
        "mode": "ejecución" if os.getenv("AUTOPILOT_EXECUTE", "false").lower() == "true" else "dry-run",
        "enabled": os.getenv("AUTOPILOT_ENABLED", "false").lower() == "true",
        "kpis": {
            "total_spend": ws.total_spend,
            "total_purchase_value_meta": ws.total_purchase_value_meta,
            "bi_revenue_real": ws.bi_revenue_real,
            "bi_pagos_count": ws.bi_pagos_count,
            "bi_available": ws.bi_available,
            "attribution_ratio": ws.attribution_ratio,
            "n_campaigns": len(ws.campaigns),
            "total_purchases": sum(c.purchases for c in ws.campaigns),
        },
        "limits": {
            "max_step_pct": limits.max_step_pct,
            "min_daily_budget_clp": limits.min_daily_budget_clp,
            "max_daily_budget_clp": limits.max_daily_budget_clp,
            "max_total_daily_clp": limits.max_total_daily_clp,
            "approval_threshold_pct": limits.approval_threshold_pct,
        },
        "actions": [
            {
                "campaign_id": a.campaign_id,
                "campaign_name": a.campaign_name,
                "action": a.action.value,
                "reason": a.reason,
                "current_budget_clp": a.current_budget_clp,
                "proposed_budget_clp": a.proposed_budget_clp,
                "confidence": a.confidence,
                "needs_approval": a.needs_approval,
                "metrics": a.metrics,
            }
            for a in actions
        ],
    }
    save_snapshot(payload)


def _append_decision_log(report: str) -> None:
    """Guarda el reporte en el log de decisiones para trazabilidad/auditoría."""
    try:
        from config import DECISION_LOG_PATH
        path = DECISION_LOG_PATH
    except ImportError:
        path = os.getenv("DECISION_LOG_PATH", "data/decisions.log")
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n\n" + "=" * 60 + "\n")
            f.write(report + "\n")
    except Exception as e:  # noqa: BLE001
        log.warning("autopilot: no pude escribir decision log: %s", e)
