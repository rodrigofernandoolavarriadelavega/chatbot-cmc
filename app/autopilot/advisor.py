"""Fase 1 — Advisor de IA para casos ambiguos.

Claude SOLO opina sobre las acciones que las reglas marcaron `ambiguous`.
Nunca ve ni cambia las decisiones que las reglas resolvieron con confianza.
Y nunca ejecuta: devuelve hipótesis + recomendación que el engine vuelve a
pasar por los límites duros antes de considerarlas. IA propone, reglas mandan.
"""
import asyncio
import json
import logging
import os

from config import ANTHROPIC_API_KEY
from .policy import ProposedAction, ActionType, HardLimits
from .world_state import WorldState

log = logging.getLogger("bot")

# Modelo para el análisis de casos ambiguos. Sonnet por defecto (mejor razonamiento
# que Haiku para juicios económicos); override con AUTOPILOT_MODEL en .env.
_MODEL = os.getenv("AUTOPILOT_MODEL", "claude-sonnet-4-6")

# Timeout duro de la llamada a Claude. Sin esto, una llamada colgada congela el
# run completo del autopilot (corre cada 6h dentro del proceso del bot).
_TIMEOUT_S = float(os.getenv("AUTOPILOT_ADVISOR_TIMEOUT_S", "25"))

try:
    from anthropic import AsyncAnthropic
    _client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None
except ImportError:
    _client = None


_SYSTEM = """Eres analista de marketing de performance de un centro médico chileno (CMC).
Te paso campañas de Meta Ads que el motor de reglas NO pudo juzgar con confianza
(poco gasto, pocos resultados, señales contradictorias, o atribución dudosa).

Tu trabajo: para CADA campaña ambigua, dar una hipótesis breve de qué está pasando
y UNA recomendación entre: increase_budget, decrease_budget, pause, keep, alert.

Reglas duras que NO puedes violar:
- Si los datos son insuficientes para decidir, recomienda "keep" o "alert", nunca inventes.
- Nunca recomiendes subir más de un paso conservador; el motor aplicará los topes.
- Piensa en CAC por paciente atendido y en si la caja real respalda lo que dice Meta.

Responde SOLO un JSON array, sin texto extra. Cada item:
{"campaign_id": "...", "action": "keep|increase_budget|decrease_budget|pause|alert",
 "hypothesis": "...", "recommendation": "...", "confidence": 0.0-1.0}"""


def _context_blob(ws: WorldState, ambiguous: list[ProposedAction]) -> str:
    """Compacta el contexto que Claude necesita (sin PII, solo métricas)."""
    ctx = {
        "ventana_dias": ws.window_days,
        "gasto_total": round(ws.total_spend),
        "ingreso_real_caja_BI": round(ws.bi_revenue_real) if ws.bi_revenue_real else None,
        "ratio_atribucion_BI_vs_Meta": round(ws.attribution_ratio, 2) if ws.attribution_ratio else None,
        "campanas_ambiguas": [
            {
                "campaign_id": a.campaign_id,
                "nombre": a.campaign_name,
                "presupuesto_diario": a.current_budget_clp,
                "metricas": a.metrics,
                "motivo_ambiguedad": a.reason,
            }
            for a in ambiguous
        ],
    }
    return json.dumps(ctx, ensure_ascii=False, indent=2)


async def advise(ws: WorldState, actions: list[ProposedAction],
                 limits: HardLimits | None = None) -> list[ProposedAction]:
    """Enriquece las acciones ambiguas con la opinión de Claude.

    Degrada con gracia: si no hay API key o falla, deja las acciones como están
    (marcadas ambiguas → irán al reporte para revisión humana).
    """
    ambiguous = [a for a in actions if a.ambiguous]
    if not ambiguous:
        return actions
    if _client is None:
        log.info("autopilot advisor: sin ANTHROPIC_API_KEY; %d ambiguas quedan para humano",
                 len(ambiguous))
        return actions

    limits = limits or HardLimits.from_env()
    try:
        resp = await asyncio.wait_for(
            _client.messages.create(
                model=_MODEL,
                max_tokens=2000,
                system=_SYSTEM,
                messages=[{"role": "user", "content": _context_blob(ws, ambiguous)}],
            ),
            timeout=_TIMEOUT_S,
        )
        try:  # INSTRUMENTACIÓN gasto Sonnet — quitar tras diagnóstico
            _u = getattr(resp, "usage", None)
            if _u is not None:
                log.info("CLAUDE_CALL fn=advisor model=%s in=%s cache_r=%s cache_w=%s out=%s",
                         _MODEL, getattr(_u, "input_tokens", "?"),
                         getattr(_u, "cache_read_input_tokens", 0),
                         getattr(_u, "cache_creation_input_tokens", 0),
                         getattr(_u, "output_tokens", "?"))
        except Exception:
            pass
        text = resp.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("```")[1].lstrip("json").strip()
        recs = {r["campaign_id"]: r for r in json.loads(text)}
    except Exception as e:  # noqa: BLE001 — nunca romper el bot por el advisor
        log.warning("autopilot advisor: falló (%s); ambiguas quedan para humano", e)
        return actions

    by_id = {a.campaign_id: a for a in actions}
    for cid, rec in recs.items():
        act = by_id.get(cid)
        if not act:
            continue
        try:
            act.action = ActionType(rec.get("action", "alert"))
        except ValueError:
            act.action = ActionType.ALERT
        act.reason = (f"[IA] {rec.get('hypothesis', '')} → {rec.get('recommendation', '')}")
        act.confidence = float(rec.get("confidence", 0.5))
        # La IA NUNCA salta los límites: cualquier acción que mueva plata pide aprobación.
        act.needs_approval = act.action in (ActionType.INCREASE, ActionType.DECREASE,
                                            ActionType.PAUSE)
        act.ambiguous = False
        act.metrics = {**act.metrics, "fuente_decision": "ia_advisor"}

    log.info("autopilot advisor: %d campañas ambiguas evaluadas por IA", len(recs))
    return actions
