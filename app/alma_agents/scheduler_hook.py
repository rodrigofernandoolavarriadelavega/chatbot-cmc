"""Enganche al scheduler de main.py — UNA línea de integración.

Diseño clave: si el flag maestro ALMA_AGENTS_ENABLED está OFF, NO se registra ni
un solo job. La flota es entonces totalmente inerte (existe como código, corre
solo a mano desde el panel en dry-run). Encender el maestro recién agenda los
agentes, y aún así cada run() vuelve a chequear su propio flag + execute.
"""
import logging

from apscheduler.triggers.cron import CronTrigger

from . import guardrails, registry

log = logging.getLogger("bot")


def register_agent_jobs(scheduler, timezone) -> int:
    """Registra los jobs de la flota. Devuelve cuántos registró (0 si maestro off)."""
    if not guardrails.master_enabled():
        log.info("alma_agents: flota apagada (ALMA_AGENTS_ENABLED=false) → 0 jobs")
        return 0

    n = 0
    for aid, agent in registry.all_agents().items():
        if not agent.schedule:
            continue

        async def _job(_agent=agent):
            try:
                await _agent.run()
            except Exception as e:  # noqa: BLE001 — nunca tumbar el scheduler
                log.error("alma_agents[%s] job falló: %s", _agent.id, e)

        try:
            scheduler.add_job(
                _job,
                CronTrigger(timezone=timezone, **agent.schedule),
                id=f"alma_agent_{aid}",
                replace_existing=True,
            )
            n += 1
        except Exception as e:  # noqa: BLE001
            log.error("alma_agents: no pude agendar %s (%s)", aid, e)
    log.info("alma_agents: %d jobs de la flota registrados", n)
    return n
