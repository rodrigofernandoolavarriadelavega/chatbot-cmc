"""Tests del motor de reglas del Autopilot (policy.py).

Primera suite del módulo (hasta 2026-06-10 las 7.9k líneas del autopilot
corrían SIN tests, con EXECUTE=on en prod). Pinea:
  • la jerarquía de señal (CAC local > Purchase > mensaje > clic),
  • la asimetría honesta (CAC local alto NUNCA es loser/pausa),
  • los límites duros (paso ±20%, piso/techo, techo total de cuenta),
  • el gating de aprobación humana (pause SIEMPRE pide OK),
  • la inferencia de especialidad y el freno por capacidad "lleno".

Correr:  python3 -m pytest tests/test_autopilot_policy.py -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "app"))

from autopilot.policy import (  # noqa: E402
    ActionType, EconVerdict, HardLimits, _econ_thresholds, _infer_especialidad,
    _step_budget, decide, evaluate_economics,
)
from autopilot.world_state import CampaignState, WorldState  # noqa: E402

LIMITS = HardLimits()  # defaults: paso 20%, piso 2000, techo 30000, total 80000


def _camp(**kw) -> CampaignState:
    base = dict(id="c1", name="Campaña genérica", objective="OUTCOME_ENGAGEMENT",
                effective_status="ACTIVE", daily_budget_clp=10000, spend=50000.0)
    base.update(kw)
    return CampaignState(**base)


def _ws(campaigns, **kw) -> WorldState:
    base = dict(window_days=7, date_from="2026-06-03", date_to="2026-06-10",
                campaigns=campaigns)
    base.update(kw)
    return WorldState(**base)


# ── Umbrales derivados (no números mágicos en los tests) ─────────────────────
T = _econ_thresholds(None)            # margen global
T_ECO = _econ_thresholds("ecografía")  # override real $16.000


# ── Jerarquía nivel 0: CAC REAL LOCAL ────────────────────────────────────────

def test_cac_local_bueno_es_winner():
    c = _camp(atendidos_local=3, cac_real_local=T["cac_bueno"] - 100)
    v, conf, _ = evaluate_economics(c, LIMITS)
    assert v == EconVerdict.WINNER and conf == 0.9


def test_cac_local_tolerable_es_ok():
    c = _camp(atendidos_local=3, cac_real_local=T["cac_tolerable"] - 100)
    v, _, _ = evaluate_economics(c, LIMITS)
    assert v == EconVerdict.OK


def test_cac_local_alto_es_marginal_nunca_loser():
    """Asimetría honesta: el CAC local SUBESTIMA atenciones → es un techo.
    Por muy alto que sea, como mucho MARGINAL (jamás loser/pausa por esta señal)."""
    c = _camp(atendidos_local=3, cac_real_local=T["cac_tolerable"] * 10)
    v, _, reason = evaluate_economics(c, LIMITS)
    assert v == EconVerdict.MARGINAL
    assert "techo" in reason


def test_cac_local_pisa_a_purchase():
    """Nivel 0 manda: aunque Meta diga maravilla, el CAC local decide."""
    c = _camp(atendidos_local=3, cac_real_local=T["cac_tolerable"] * 3,
              purchases=10, cac_purchase=1000.0, roas_meta=9.9)
    v, _, _ = evaluate_economics(c, LIMITS)
    assert v == EconVerdict.MARGINAL


# ── Nivel 1: Purchase de Meta ────────────────────────────────────────────────

def test_purchase_winner_requiere_cac_y_roas():
    c = _camp(purchases=5, cac_purchase=float(T["cac_bueno"] - 100), roas_meta=3.0)
    assert evaluate_economics(c, LIMITS)[0] == EconVerdict.WINNER
    # mismo CAC pero ROAS bajo → no escala
    c2 = _camp(purchases=5, cac_purchase=float(T["cac_bueno"] - 100), roas_meta=1.0)
    assert evaluate_economics(c2, LIMITS)[0] == EconVerdict.OK


def test_purchase_loser_sobre_150pct_del_tope():
    c = _camp(purchases=5, cac_purchase=T["cac_tolerable"] * 1.6, roas_meta=0.5)
    assert evaluate_economics(c, LIMITS)[0] == EconVerdict.LOSER


def test_pocas_purchases_no_se_confia():
    """Bajo min_purchases_to_trust (2) cae al siguiente nivel de señal."""
    c = _camp(purchases=1, cac_purchase=999999.0)
    assert evaluate_economics(c, LIMITS)[0] == EconVerdict.UNKNOWN


# ── Niveles 2-3 y guards ─────────────────────────────────────────────────────

def test_mensajes_baratos_winner():
    c = _camp(messages=20, cost_per_message=float(T["msg_bueno"] - 50))
    assert evaluate_economics(c, LIMITS)[0] == EconVerdict.WINNER


def test_solo_clics_nunca_winner():
    """Clic = señal débil: lo mejor que da es OK (no se escala por clics)."""
    c = _camp(link_clicks=100, cost_per_link_click=100.0)
    v, conf, _ = evaluate_economics(c, LIMITS)
    assert v == EconVerdict.OK and conf <= 0.5


def test_gasto_minimo_no_se_juzga():
    c = _camp(spend=LIMITS.min_spend_to_judge_clp - 1)
    v, conf, _ = evaluate_economics(c, LIMITS)
    assert v == EconVerdict.UNKNOWN and conf == 0.0


def test_sin_senal_unknown():
    assert evaluate_economics(_camp(), LIMITS)[0] == EconVerdict.UNKNOWN


# ── Umbrales por especialidad ────────────────────────────────────────────────

def test_infer_especialidad():
    assert _infer_especialidad("Ecotomografía en CMC") == "ecografía"
    assert _infer_especialidad("CMC ORTO BATTLE") == "ortodoncia"
    assert _infer_especialidad("promo sin pistas") is None


def test_override_margen_ecografia():
    """Ecografía: margen REAL $16.000 (la presta un tercero, CMC retiene 30%)."""
    assert T_ECO["cac_tolerable"] == 16000


# ── Límites duros ────────────────────────────────────────────────────────────

def test_step_budget_capea_el_paso():
    assert _step_budget(10000, 1.5, LIMITS) == 12000   # 1.5 → cap +20%
    assert _step_budget(10000, 0.1, LIMITS) == 8000    # 0.1 → cap −20%


def test_step_budget_respeta_piso_y_techo():
    assert _step_budget(2400, 0.8, LIMITS) == LIMITS.min_daily_budget_clp
    assert _step_budget(28000, 1.2, LIMITS) == LIMITS.max_daily_budget_clp
    assert _step_budget(None, 1.2, LIMITS) is None


# ── decide(): acciones + gating de aprobación ────────────────────────────────

def test_marginal_propone_decrease_con_aprobacion():
    c = _camp(atendidos_local=3, cac_real_local=T["cac_tolerable"] * 2)
    acts = decide(_ws([c]), LIMITS)
    a = acts[0]
    assert a.action == ActionType.DECREASE
    assert a.proposed_budget_clp == 8000          # −20% de 10000
    assert a.needs_approval is True                # cambio 20% ≥ umbral


def test_loser_que_quemo_fuerte_propone_pause_con_ok_humano():
    c = _camp(purchases=5, cac_purchase=T["cac_tolerable"] * 5.0, roas_meta=0.1,
              spend=4 * LIMITS.min_spend_to_judge_clp + 1)
    a = decide(_ws([c]), LIMITS)[0]
    assert a.action == ActionType.PAUSE
    assert a.needs_approval is True                # pausar SIEMPRE pide OK
    assert a.proposed_budget_clp == 0


def test_unknown_va_al_advisor():
    a = decide(_ws([_camp()]), LIMITS)[0]
    assert a.action == ActionType.ALERT and a.ambiguous is True


def test_techo_total_cancela_escaladas():
    """3 ganadoras de $25k → propuestas $30k c/u = $90k > techo $80k → KEEP."""
    camps = [_camp(id=f"c{i}", daily_budget_clp=25000, atendidos_local=3,
                   cac_real_local=T["cac_bueno"] - 100) for i in range(3)]
    acts = decide(_ws(camps), LIMITS)
    assert all(a.action == ActionType.KEEP for a in acts)
    assert all("techo" in a.reason for a in acts)


def test_capacity_lleno_frena_ganadora():
    c = _camp(name="Ecotomografía CMC", atendidos_local=3,
              cac_real_local=T_ECO["cac_bueno"] - 100)
    ws = _ws([c], capacity={"ecografía": {"gate": True}})
    a = decide(ws, LIMITS)[0]
    assert a.action == ActionType.KEEP
    assert "LLENA" in a.reason


def test_ratio_atribucion_bajo_reduce_confianza():
    c = _camp(atendidos_local=3, cac_real_local=T["cac_bueno"] - 100)
    conf_normal = decide(_ws([c]), LIMITS)[0].confidence
    conf_dudosa = decide(_ws([c], attribution_ratio=0.3), LIMITS)[0].confidence
    assert conf_dudosa < conf_normal


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
