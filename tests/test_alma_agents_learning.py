"""Tests del loop de aprendizaje (bandit UCB sobre el Ledger). DB temporal.

Verifica: explotación de la variante con mejor conversión, exploración de una
variante nueva (cold-start), y el prior conservador de expected_conversion.
Corre: `python3 tests/test_alma_agents_learning.py`
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
_TMP = tempfile.mkdtemp(prefix="learning_test_")
import session  # noqa: E402
session.DB_PATH = Path(_TMP) / "sessions.db"

from alma_agents import ledger, learning  # noqa: E402

_OK = 0; _FAIL = 0
def check(c, l):
    global _OK, _FAIL
    if c: _OK += 1; print(f"OK  {l}")
    else: _FAIL += 1; print(f"XX  FALLA: {l}")


def _seed(agent, variant, outcome, n):
    ledger.ensure_table()
    conn = session._conn()
    with conn:
        for _ in range(n):
            conn.execute(
                """INSERT INTO agent_action_ledger
                   (agent_id, action_kind, target_phone, acted_at, window_days, variant, outcome)
                   VALUES (?,?,?,?,7,?,?)""",
                (agent, "contacto", "569000", "2026-05-01 12:00:00", variant, outcome))
    conn.close()


def main():
    # winback: variante 'mensaje_corto' convierte bien; 'mensaje_largo' mal.
    _seed("winback", "mensaje_corto", "cita", 3)
    _seed("winback", "mensaje_corto", "sin_respuesta", 1)   # corto: 3/4 = 0.75
    _seed("winback", "mensaje_largo", "cita", 0)
    _seed("winback", "mensaje_largo", "sin_respuesta", 4)   # largo: 0/4 = 0.0

    stats = learning.variant_stats("winback")
    check(stats["mensaje_corto"]["valor"] == 0.75, f"corto valor 0.75 (got {stats['mensaje_corto']['valor']})")
    check(stats["mensaje_largo"]["valor"] == 0.0, f"largo valor 0.0 (got {stats['mensaje_largo']['valor']})")

    # Sin candidatas nuevas → explota la mejor (corto)
    rec = learning.recommend("winback")
    check(rec["variant"] == "mensaje_corto", f"recomienda la mejor variante (got {rec['variant']})")
    check("explotación" in rec["reason"], "razona como explotación")

    # Con una candidata nueva → la explora (cold-start gana)
    rec2 = learning.recommend("winback", candidates=["mensaje_corto", "mensaje_largo", "mensaje_emoji"])
    check(rec2["variant"] == "mensaje_emoji", f"explora la variante nueva (got {rec2['variant']})")
    check("exploración" in rec2["reason"], "razona como exploración")

    # expected_conversion para el broker
    ev_corto = learning.expected_conversion("winback", "mensaje_corto")
    check(ev_corto == 0.75, f"EV corto 0.75 (got {ev_corto})")
    ev_nuevo = learning.expected_conversion("winback", "mensaje_nuevo")
    check(ev_nuevo == 0.33, f"EV variante sin datos = prior 0.33 (got {ev_nuevo})")
    ev_agente = learning.expected_conversion("otro_agente")
    check(ev_agente == 0.33, f"EV agente sin datos = prior 0.33 (got {ev_agente})")

    # learning_summary
    summ = learning.learning_summary()
    wb = [a for a in summ["per_agent"] if a["agent_id"] == "winback"]
    check(wb and wb[0]["recomendada"] == "mensaje_corto", "summary recomienda corto para winback")

    print(f"\n{_OK} OK · {_FAIL} FALLAS")
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()
