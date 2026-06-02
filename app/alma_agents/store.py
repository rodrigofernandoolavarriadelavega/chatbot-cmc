"""Persistencia de la flota: último resultado por agente + decision log append.

Archivos JSON atómicos en data/alma_agents/. Gitignored (data/ ya lo está).
"""
import json
import logging
import os
from datetime import datetime, timezone

log = logging.getLogger("bot")

_DIR = os.getenv("ALMA_AGENTS_DATA_DIR", "data/alma_agents")
_DECISION_LOG = os.path.join(_DIR, "decisions.log")


def _ensure_dir() -> None:
    os.makedirs(_DIR, exist_ok=True)


def record_run(result: dict) -> None:
    """Guarda el último run del agente (sobreescribe) + traza en el decision log."""
    try:
        _ensure_dir()
        path = os.path.join(_DIR, f"{result['agent_id']}.json")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, default=str)
        os.replace(tmp, path)
    except Exception as e:  # noqa: BLE001
        log.warning("alma_agents store: no pude guardar run de %s (%s)",
                    result.get("agent_id"), e)
    # Decision log: una línea por acción ejecutada o bloqueada (auditoría).
    try:
        _ensure_dir()
        ts = datetime.now(timezone.utc).isoformat()
        with open(_DECISION_LOG, "a", encoding="utf-8") as f:
            for a in result.get("actions_executed", []):
                f.write(f"{ts}\t{result['agent_id']}\tEJECUTADA\t{a.get('summary','')}\t"
                        f"{json.dumps(a, ensure_ascii=False, default=str)}\n")
            for a in result.get("actions_blocked", []):
                f.write(f"{ts}\t{result['agent_id']}\tBLOQUEADA\t{a.get('summary','')}\t"
                        f"{a.get('reason','')}\n")
    except Exception as e:  # noqa: BLE001
        log.warning("alma_agents store: no pude escribir decision log (%s)", e)


def load_last_run(agent_id: str) -> dict | None:
    try:
        with open(os.path.join(_DIR, f"{agent_id}.json"), encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:  # noqa: BLE001
        log.warning("alma_agents store: no pude leer run de %s (%s)", agent_id, e)
        return None
