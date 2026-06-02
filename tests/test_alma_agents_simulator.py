"""Tests del simulador de flota — agregación y detección de spam agregado.

Verifica lo que ningún dry-run por agente puede ver: que el simulador detecte a
un paciente que VARIOS agentes contactarían en la misma corrida (riesgo agregado)
y que reporte el agregado por riesgo / Medilink / ejecutaría-vs-bloquearía.
Corre: `python3 tests/test_alma_agents_simulator.py`
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

# Flota apagada (defaults) → con execute off, authorize bloquea todo.
os.environ.setdefault("ALMA_AGENTS_ENABLED", "false")
os.environ.setdefault("ALMA_AGENTS_EXECUTE", "false")
os.environ.setdefault("ALMA_AGENTS_CONTACT_MAX_WEEK", "2")

from alma_agents.base import Agent, AgentAction  # noqa: E402
from alma_agents import simulator  # noqa: E402

_OK = 0; _FAIL = 0


def check(cond, label):
    global _OK, _FAIL
    if cond: _OK += 1; print(f"OK  {label}")
    else: _FAIL += 1; print(f"XX  FALLA: {label}")


class FakeAgent(Agent):
    def __init__(self, id, actions):
        super().__init__(id=id, name=id, descr="fake", risk="medio", flag=f"FLAG_{id}")
        self._actions = actions
    async def perceive(self): return {}
    async def decide(self, ctx): return self._actions


def C(target, risk="medio", staff=False, medilink=False):
    return AgentAction(kind="contacto", summary=f"->{target}", risk=risk,
                       target=target, requires_contact=True, is_staff=staff,
                       requires_medilink_write=medilink)


def main():
    P1, P2, P3 = "56911110001", "56911110002", "56911110003"
    fleet = {
        "x": FakeAgent("x", [C(P1), C(P2)]),
        "y": FakeAgent("y", [C(P1), C(P3), AgentAction(kind="write", summary="medilink",
                              risk="alto", target=P3, requires_medilink_write=True)]),
        "z": FakeAgent("z", [C(P1), C("rodrigo", staff=True),
                             AgentAction(kind="cobro", summary="cobranza", risk="extremo",
                                         target=P2, requires_contact=True)]),
    }
    rep = asyncio.run(simulator.simulate_fleet(agents=fleet))

    tot = rep["totales"]
    check(tot["agentes"] == 3, f"3 agentes simulados (got {tot['agentes']})")
    # contactos a pacientes: x:2, y:2, z:2(P1 + extremo P2 contacto) = 6 ; staff aparte
    check(tot["contactos_staff"] == 1, f"1 contacto a staff (got {tot['contactos_staff']})")
    check(tot["escrituras_medilink"] == 1, f"1 escritura Medilink (got {tot['escrituras_medilink']})")
    check(tot["por_riesgo"].get("extremo") == 1, f"1 acción extremo (got {tot['por_riesgo'].get('extremo')})")
    check(tot["pacientes_unicos"] == 3, f"3 pacientes únicos P1/P2/P3 (got {tot['pacientes_unicos']})")

    # Con execute off, NADA se ejecutaría
    check(tot["se_ejecutaria"] == 0, f"0 se ejecutaría con flota off (got {tot['se_ejecutaria']})")
    check(tot["se_bloquearia"] == tot["acciones"], "todo bloqueado con execute off")

    # RIESGO AGREGADO: P1 lo contactan x,y,z = 3 agentes
    agg = rep["riesgo_agregado"]
    multi = {o["paciente"]: o["n_agentes"] for o in agg["pacientes_multi_agente"]}
    check(any(n == 3 for n in multi.values()), f"un paciente contactado por 3 agentes (got {multi})")
    # 3 > presupuesto(2) → aparece en sobre_presupuesto
    check(len(agg["pacientes_sobre_presupuesto"]) == 1,
          f"1 paciente sobre presupuesto (got {len(agg['pacientes_sobre_presupuesto'])})")
    check("…0001" in multi, f"el paciente solapado es P1 enmascarado (got {list(multi)})")

    # por_agente ordenado y completo
    check(len(rep["por_agente"]) == 3, "por_agente lista los 3")
    check(all("n_actions" in a for a in rep["por_agente"]), "cada agente trae n_actions")

    print(f"\n{_OK} OK · {_FAIL} FALLAS")
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()
