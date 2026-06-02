"""Tests del broker de contacto — competencia por valor esperado. DB temporal.

Verifica que, ante dos agentes que quieren contactar al mismo paciente, gane el
de mayor EV (P(conversión) histórica × ticket) y se suprima al otro; que las
acciones a staff/no-contacto pasen sin competir.
Corre: `python3 tests/test_alma_agents_broker.py`
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
_TMP = tempfile.mkdtemp(prefix="broker_test_")
import session  # noqa: E402
session.DB_PATH = Path(_TMP) / "sessions.db"

from alma_agents import ledger, broker  # noqa: E402
from alma_agents.base import AgentAction  # noqa: E402

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
                (agent, "c", "569", "2026-05-01 12:00:00", variant, outcome))
    conn.close()


def C(target, variant=None, staff=False):
    return AgentAction(kind="contacto", summary="x", risk="alto", target=target,
                       requires_contact=True, is_staff=staff, variant=variant)


def main():
    P1 = "56922220001"
    # 'winback' tiene historial bueno; 'reputacion' malo → winback debe ganar P1.
    _seed("winback", "v1", "cita", 4)         # 1.0
    _seed("reputacion", "v1", "sin_respuesta", 4)  # 0.0

    proposals = [
        {"agent_id": "winback", "action": C(P1, "v1")},
        {"agent_id": "reputacion", "action": C(P1, "v1")},
        {"agent_id": "yield_agenda", "action": C("56922220002", "v1")},  # paciente solo
        {"agent_id": "briefing", "action": AgentAction(kind="aviso", summary="staff",
                                  risk="bajo", target="rodrigo", requires_contact=True, is_staff=True)},
    ]
    r = broker.resolve(proposals)

    check(r["n_pacientes"] == 2, f"2 pacientes en disputa/solo (got {r['n_pacientes']})")
    # P1: winback gana, reputacion suprimido
    disp = [d for d in r["by_patient"] if d["n_contendientes"] == 2]
    check(len(disp) == 1 and disp[0]["ganador"] == "winback",
          f"winback gana el paciente disputado (got {disp[0]['ganador'] if disp else None})")
    check(disp and disp[0]["suprimidos"][0]["agent_id"] == "reputacion", "reputacion suprimido")
    check(disp and disp[0]["ev_ganador"] > 0, f"EV del ganador > 0 (got {disp[0]['ev_ganador'] if disp else None})")

    check(r["n_suprimidos"] == 1, f"1 acción suprimida por valor (got {r['n_suprimidos']})")
    # staff pasa sin competir
    check(any(p["agent_id"] == "briefing" for p in r["passthrough"]), "contacto a staff pasa (passthrough)")
    # el paciente solo es winner (no suprimido)
    check(any(w["agent_id"] == "yield_agenda" for w in r["winners"]), "paciente sin disputa = winner")

    # EV anotado en la acción
    ev = broker.expected_value("winback", C(P1, "v1"))
    check(ev == round(1.0 * ledger.AVG_TICKET_CLP, 2), f"EV = P×ticket (got {ev})")

    print(f"\n{_OK} OK · {_FAIL} FALLAS")
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()
