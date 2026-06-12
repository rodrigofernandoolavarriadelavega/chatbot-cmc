"""Tests de learned_skills — graduación, estabilidad y decaimiento.

No tocan BI ni red: alimentan `observe()` con recs sintéticas (la forma exacta que
emite optimizer._rec para márgenes) y verifican la máquina de estados.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from autopilot import learned_skills as ls  # noqa: E402


def _rec(esp, val, conf="alta", action="bajar"):
    """Replica la forma de una rec de margen del optimizer."""
    return {"policy": f"margen[{esp}]", "especialidad": esp, "proposed_clp": val,
            "current": "$40,000", "confidence": conf, "action": action,
            "evidence": f"n atenciones · margen real ${val}", "analyzer": "Margen por especialidad"}


def test_no_promote_before_min_confirmations():
    skills = {}
    for _ in range(ls.MIN_CONFIRMATIONS - 1):
        skills = ls.observe([_rec("ecografía", 16000)], skills)
    s = skills["margen:ecografía"]
    assert s["status"] == "observing", "no debe graduar antes del mínimo de confirmaciones"
    assert s["confirmations"] == ls.MIN_CONFIRMATIONS - 1


def test_promotes_after_stable_confirmations():
    skills = {}
    for _ in range(ls.MIN_CONFIRMATIONS):
        skills = ls.observe([_rec("ecografía", 16000)], skills)
    s = skills["margen:ecografía"]
    assert s["status"] == "active", "debe graduar tras confirmaciones estables"
    assert s["effect"]["value_clp"] == 16000


def test_low_confidence_never_promotes():
    skills = {}
    for _ in range(ls.MIN_CONFIRMATIONS + 3):
        skills = ls.observe([_rec("kinesiología", 8000, conf="baja")], skills)
    assert skills["margen:kinesiología"]["status"] == "observing"


def test_flipflop_retires():
    """Una skill que OSCILA (ruido, no cambio de mundo) acumula reversiones y se retira."""
    skills = {}
    for _ in range(ls.MIN_CONFIRMATIONS):
        skills = ls.observe([_rec("endodoncia", 60000)], skills)
    assert skills["margen:endodoncia"]["status"] == "active"
    # Flip-flop: alterna valores muy distintos → cada cambio es una reversión.
    for val in (20000, 60000, 20000, 60000)[:ls.RETIRE_AFTER_REVERSALS]:
        skills = ls.observe([_rec("endodoncia", val)], skills)
    assert skills["margen:endodoncia"]["status"] == "retired"


def test_sustained_world_change_is_adopted_not_retired():
    """Un cambio de mundo SOSTENIDO (margen real bajó y se quedó) se adopta y re-gradúa."""
    skills = {}
    for _ in range(ls.MIN_CONFIRMATIONS):
        skills = ls.observe([_rec("endodoncia", 60000)], skills)
    assert skills["margen:endodoncia"]["status"] == "active"
    # Baja a 20000 y se queda ahí muchas corridas → debe re-graduar en 20000, no retirarse.
    for _ in range(ls.MIN_CONFIRMATIONS + 1):
        skills = ls.observe([_rec("endodoncia", 20000)], skills)
    s = skills["margen:endodoncia"]
    assert s["status"] == "active"
    assert s["effect"]["value_clp"] == 20000


def test_absence_does_not_decay_active():
    """Una skill activa que YA no aparece (porque el override la alineó) NO decae."""
    skills = {}
    for _ in range(ls.MIN_CONFIRMATIONS):
        skills = ls.observe([_rec("ecografía", 16000)], skills)
    assert skills["margen:ecografía"]["status"] == "active"
    before = dict(skills["margen:ecografía"])
    # corrida sin recs (el optimizer ya no flaggea esta especialidad)
    skills = ls.observe([], skills)
    after = skills["margen:ecografía"]
    assert after["status"] == "active"
    assert after["reversals"] == before["reversals"]


def test_value_smoothing_within_tolerance_counts_as_confirmation():
    skills = {}
    skills = ls.observe([_rec("cardiología", 12000)], skills)
    # 13000 está dentro de ±15% de 12000 → confirma, no reversa
    skills = ls.observe([_rec("cardiología", 13000)], skills)
    s = skills["margen:cardiología"]
    assert s["confirmations"] == 2
    assert s["reversals"] == 0
    assert 12000 <= s["effect"]["value_clp"] <= 13000  # EMA entre ambos


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        try:
            fn()
            print(f"  ✓ {fn.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  ✗ {fn.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ {fn.__name__}: ERROR {e}")
    print(f"\n{passed}/{len(fns)} tests verdes")
    sys.exit(0 if passed == len(fns) else 1)
