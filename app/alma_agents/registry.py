"""Registry de la flota — descubre todos los agentes del paquete `agents`.

Cada módulo en alma_agents/agents/ que exponga un objeto `AGENT` (instancia de
Agent) se registra automáticamente. Así sumar un agente = soltar un archivo.
"""
import importlib
import logging
import pkgutil

log = logging.getLogger("bot")

_REGISTRY: dict | None = None


def _discover() -> dict:
    from . import agents as agents_pkg
    found = {}
    for mod in pkgutil.iter_modules(agents_pkg.__path__):
        if mod.name.startswith("_"):
            continue
        try:
            m = importlib.import_module(f"{agents_pkg.__name__}.{mod.name}")
            agent = getattr(m, "AGENT", None)
            if agent is not None:
                found[agent.id] = agent
        except Exception as e:  # noqa: BLE001 — un agente roto no rompe la flota
            log.error("alma_agents registry: no pude cargar %s (%s)", mod.name, e)
    return found


def all_agents() -> dict:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = _discover()
        log.info("alma_agents: %d agentes descubiertos: %s",
                 len(_REGISTRY), ", ".join(sorted(_REGISTRY)))
    return _REGISTRY


def get(agent_id: str):
    return all_agents().get(agent_id)
