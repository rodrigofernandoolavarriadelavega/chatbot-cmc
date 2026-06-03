"""Tests offline de la flota de agentes — sin API ni red externa.

Fijan el contrato de los guardrails (el piso de seguridad de la flota) y que el
registry descubra agentes bien formados. Corre: `python3 tests/test_alma_agents.py`.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))


def _ok(cond, msg):
    print(("OK  " if cond else "FALLO ") + msg)
    assert cond, msg


def _reset_env():
    for k in ("ALMA_AGENTS_ENABLED", "ALMA_AGENTS_EXECUTE", "ALMA_AGENTS_ALLOW_EXTREME",
              "ALMA_BRAIN_ALLOW_MEDILINK_WRITES"):
        os.environ.pop(k, None)
    os.environ["ALMA_AGENTS_REQUIRE_CONSENT"] = "true"


def test_registry_descubre_flota():
    from alma_agents import registry
    ags = registry.all_agents()
    _ok(len(ags) >= 15, f"registry descubre la flota completa ({len(ags)} agentes)")
    for aid, a in ags.items():
        _ok(bool(a.flag and a.flag.startswith("ALMA_AGENT")), f"{aid} tiene flag propio ({a.flag})")
        _ok(a.risk in ("bajo", "medio", "alto", "extremo"), f"{aid} riesgo válido")


def test_execute_off_bloquea_todo():
    _reset_env()
    from alma_agents import guardrails
    from alma_agents.base import AgentAction
    a = AgentAction(kind="x", summary="y", risk="bajo", target="569", requires_contact=True, is_staff=True)
    allow, reason = guardrails.authorize(a)
    _ok(not allow and "dry-run" in reason, "con execute off, ninguna acción se ejecuta")


def test_extremo_requiere_flag():
    _reset_env()
    os.environ["ALMA_AGENTS_EXECUTE"] = "true"
    from importlib import reload
    from alma_agents import guardrails
    from alma_agents.base import AgentAction
    a = AgentAction(kind="cobro", summary="cobrar", risk="extremo", target="569", is_staff=True)
    allow, reason = guardrails.authorize(a)
    _ok(not allow and "EXTREMO" in reason, "riesgo extremo bloqueado sin ALMA_AGENTS_ALLOW_EXTREME")


def test_medilink_write_requiere_flag():
    _reset_env()
    os.environ["ALMA_AGENTS_EXECUTE"] = "true"
    from alma_agents import guardrails
    from alma_agents.base import AgentAction
    a = AgentAction(kind="agendar", summary="crear cita", risk="alto",
                    requires_medilink_write=True, is_staff=True)
    allow, reason = guardrails.authorize(a)
    _ok(not allow and "Medilink" in reason, "escritura Medilink bloqueada sin flag")


def test_contacto_paciente_exige_consent():
    _reset_env()
    os.environ["ALMA_AGENTS_EXECUTE"] = "true"
    os.environ["ALMA_AGENTS_QUIET_START"] = "23"  # evitar que horas de silencio enmascare
    os.environ["ALMA_AGENTS_QUIET_END"] = "0"
    from alma_agents import guardrails
    from alma_agents.base import AgentAction
    # paciente (no staff), sin consent en DB de test → debe bloquear por consent o budget
    a = AgentAction(kind="msg", summary="contacto", risk="medio",
                    target="56900000000", requires_contact=True, is_staff=False)
    allow, reason = guardrails.authorize(a)
    _ok(not allow, f"contacto a paciente sin consent/budget se bloquea ({reason})")


def test_dry_run_loop_no_ejecuta():
    _reset_env()
    os.environ["ALMA_AGENTS_ENABLED"] = "true"
    os.environ["ALMA_AGENT_SRE"] = "true"
    # execute OFF → dry-run
    from alma_agents import registry
    a = registry.get("sre_watchdog")

    async def go():
        return await a.run()
    r = asyncio.run(go())
    _ok(r["dry_run"] is True, "sre_watchdog corre en dry-run con execute off")
    _ok(len(r["actions_executed"]) == 0, "dry-run no ejecuta ninguna acción")


def test_agente_apagado_noop():
    _reset_env()
    os.environ["ALMA_AGENTS_ENABLED"] = "true"
    # su flag queda OFF
    from alma_agents import registry
    a = registry.get("briefing")

    async def go():
        return await a.run()
    r = asyncio.run(go())
    _ok("apagado" in r["notes"], "agente con su flag off hace no-op")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
    print(f"\n{len([f for f in fns])} grupos de test OK")
