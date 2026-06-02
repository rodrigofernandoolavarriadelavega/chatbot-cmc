"""Tests de goal-setting + runtime. DB y data dir aislados.

Verifica: propuesta de metas desde alertas del cerebro, creación, medición de
progreso contra el Ledger, y que el runtime pondere el broker.
Corre: `python3 tests/test_alma_agents_goals.py`
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
_TMP = tempfile.mkdtemp(prefix="goals_test_")
os.environ["ALMA_AGENTS_DATA_DIR"] = _TMP
import session  # noqa: E402
session.DB_PATH = Path(_TMP) / "sessions.db"

from alma_agents import goals, ledger, runtime, broker  # noqa: E402
from alma_agents.base import AgentAction  # noqa: E402

_OK = 0; _FAIL = 0
def check(c, l):
    global _OK, _FAIL
    if c: _OK += 1; print(f"OK  {l}")
    else: _FAIL += 1; print(f"XX  FALLA: {l}")


def _seed_cita(agent, n, ts):
    ledger.ensure_table()
    conn = session._conn()
    with conn:
        for _ in range(n):
            conn.execute(
                """INSERT INTO agent_action_ledger
                   (agent_id, action_kind, target_phone, acted_at, window_days, outcome)
                   VALUES (?,?,?,?,7,'cita')""",
                (agent, "c", "569", ts))
    conn.close()


def main():
    # 1) Propuesta desde alertas del cerebro
    world = {"alerts": [
        {"domain": "agenda", "severity": "critico", "message": "agenda vacía"},
        {"domain": "demanda", "severity": "oportunidad", "message": "gastro alta"},
        {"domain": "agenda", "severity": "warn", "message": "dup"},  # mismo dominio, no duplica
    ]}
    props = goals.propose_goals(world)
    doms = [p["titulo"] for p in props]
    check(len(props) == 2, f"2 metas propuestas (agenda+demanda, sin duplicar) (got {len(props)})")
    check(props[0]["severidad"] == "critico", "la meta crítica va primero")
    check("yield_agenda" in props[0]["agents"], "meta de agenda asigna yield_agenda")

    # 2) Crear meta y medir progreso
    gid = goals.create_goal("Llenar agenda", "citas", 5, ["yield_agenda"], budget_contacts=20)
    check(gid is not None, "meta creada con id")
    # sembrar citas DENTRO de la ventana de la meta (en su created_at)
    ts_meta = goals.list_goals(with_progress=False)[0]["created_at"][:19]
    _seed_cita("yield_agenda", 3, ts_meta)  # 3 de 5
    lst = goals.list_goals()
    g = lst[0]
    check(g["progreso"]["done"] == 3, f"progreso 3 citas (got {g['progreso']['done']})")
    check(g["progreso"]["pct"] == 60, f"progreso 60% (got {g['progreso']['pct']})")
    check(g["progreso"]["cumplida"] is False, "meta aún no cumplida")
    _seed_cita("yield_agenda", 2, ts_meta)  # +2 = 5
    g2 = goals.list_goals()[0]
    check(g2["progreso"]["cumplida"] is True, "meta cumplida al llegar al target")

    # 3) Runtime pondera el broker
    runtime.reset()
    base_ev = broker.expected_value("yield_agenda", AgentAction(kind="c", summary="x",
                                    risk="alto", target="569", requires_contact=True))
    runtime.apply_overrides({"agent_weights": {"yield_agenda": 2.0}, "focus": ["agenda"]})
    boosted_ev = broker.expected_value("yield_agenda", AgentAction(kind="c", summary="x",
                                       risk="alto", target="569", requires_contact=True))
    check(boosted_ev == round(base_ev * 2.0, 2), f"prioridad 2.0 duplica el EV (got {base_ev}→{boosted_ev})")
    st = runtime.get_state()
    check(st["focus"] == ["agenda"], "foco persistido en runtime")

    print(f"\n{_OK} OK · {_FAIL} FALLAS")
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()
