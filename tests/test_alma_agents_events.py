"""Tests del bus de eventos — suscripción, dispatch reactivo y gating. Sin red.

Verifica: un evento despierta solo a los agentes suscritos; con el maestro OFF
emit() es no-op; con maestro ON + execute OFF las acciones se proponen pero se
bloquean (dry). Corre: `python3 tests/test_alma_agents_events.py`
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from alma_agents import events, registry  # noqa: E402
from alma_agents.base import Agent, AgentAction  # noqa: E402

_OK = 0; _FAIL = 0
def check(c, l):
    global _OK, _FAIL
    if c: _OK += 1; print(f"OK  {l}")
    else: _FAIL += 1; print(f"XX  FALLA: {l}")


class ReactorAgent(Agent):
    async def react(self, event_type, payload):
        return [AgentAction(kind="reaccion", summary=f"reacciono a {event_type}",
                            risk="bajo", target="rodrigo", requires_contact=True, is_staff=True)]


def main():
    # Inyectar una flota de prueba en el registry.
    a_react = ReactorAgent(id="reactor", name="Reactor", descr="x", risk="bajo",
                           flag="ALMA_AGENT_REACTOR", triggers=["cita_cancelada"])
    a_idle = ReactorAgent(id="idle", name="Idle", descr="x", risk="bajo",
                          flag="ALMA_AGENT_IDLE", triggers=["otro_evento"])
    registry._REGISTRY = {"reactor": a_react, "idle": a_idle}

    # Suscripción correcta
    subs = events.subscribers("cita_cancelada")
    check(len(subs) == 1 and subs[0].id == "reactor", f"solo 'reactor' suscrito a cita_cancelada (got {[s.id for s in subs]})")

    # Maestro OFF → no-op
    os.environ["ALMA_AGENTS_ENABLED"] = "false"
    r_off = asyncio.run(events.emit("cita_cancelada", {}))
    check(r_off["skipped"] is not None and not r_off["reacciones"], "maestro OFF → emit no-op")

    # Maestro ON, execute OFF → reacciona pero bloquea (dry)
    os.environ["ALMA_AGENTS_ENABLED"] = "true"
    os.environ["ALMA_AGENTS_EXECUTE"] = "false"
    os.environ["ALMA_AGENT_REACTOR"] = "true"
    r_on = asyncio.run(events.emit("cita_cancelada", {"especialidad": "kine"}))
    check(r_on["skipped"] is None, "maestro ON → emit corre")
    check(len(r_on["reacciones"]) == 1, "1 agente reaccionó")
    rr = r_on["reacciones"][0]
    check(rr["propuestas"] == 1, f"propuso 1 acción (got {rr['propuestas']})")
    check(rr["ejecutadas"] == 0, f"0 ejecutadas (execute off → dry) (got {rr['ejecutadas']})")
    check(rr["bloqueadas"] == 1, f"1 bloqueada por dry (got {rr['bloqueadas']})")

    # Evento sin suscritos → no reacciones
    r_none = asyncio.run(events.emit("evento_fantasma", {}))
    check(not r_none["reacciones"], "evento sin suscritos → 0 reacciones")

    # limpiar para no contaminar otros tests del proceso
    os.environ["ALMA_AGENTS_ENABLED"] = "false"
    registry._REGISTRY = None

    print(f"\n{_OK} OK · {_FAIL} FALLAS")
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()
