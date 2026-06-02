"""Runtime config de la agencia — el dial de prioridades que tú controlas.

Estado liviano (JSON) que la flota lee para saber en qué enfocarse esta semana,
qué agentes priorizar y con qué presupuesto. Lo escribe el plano de control en
lenguaje natural (control_plane.py) o la UI; lo leen el broker (pondera EV por
prioridad) y los agentes/objetivos.

NO son flags de encendido (eso vive en .env, piso duro). Esto es el "hacia dónde"
cuando la flota ya está encendida. Default = sin foco, prioridad 1.0 para todos.
"""
import json
import logging
import os
from datetime import datetime, timezone

log = logging.getLogger("bot")

_DIR = os.getenv("ALMA_AGENTS_DATA_DIR", "data/alma_agents")
_PATH = os.path.join(_DIR, "runtime.json")

_DEFAULT = {
    "focus": [],            # ej. ["kinesiologia", "ortodoncia"] o objetivos libres
    "agent_weights": {},    # agent_id -> peso (1.0 neutro; >1 prioriza, <1 baja)
    "notes": "",            # qué pidió el dueño, en sus palabras
    "updated_at": None,
}


def get_state() -> dict:
    try:
        with open(_PATH, encoding="utf-8") as f:
            st = json.load(f)
        return {**_DEFAULT, **st}
    except FileNotFoundError:
        return dict(_DEFAULT)
    except Exception as e:  # noqa: BLE001
        log.warning("runtime.get_state falló: %s", e)
        return dict(_DEFAULT)


def _save(st: dict) -> None:
    try:
        os.makedirs(_DIR, exist_ok=True)
        st["updated_at"] = datetime.now(timezone.utc).isoformat()
        tmp = _PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, default=str)
        os.replace(tmp, _PATH)
    except Exception as e:  # noqa: BLE001
        log.warning("runtime._save falló: %s", e)


def apply_overrides(overrides: dict) -> dict:
    """Mezcla overrides (focus / agent_weights / notes) sobre el estado actual."""
    st = get_state()
    if "focus" in overrides and overrides["focus"] is not None:
        st["focus"] = list(overrides["focus"])
    if "agent_weights" in overrides and overrides["agent_weights"]:
        st["agent_weights"] = {**st.get("agent_weights", {}), **overrides["agent_weights"]}
    if overrides.get("notes"):
        st["notes"] = overrides["notes"]
    _save(st)
    return st


def agent_weight(agent_id: str) -> float:
    """Peso de prioridad del agente (1.0 si no está seteado)."""
    try:
        return float(get_state().get("agent_weights", {}).get(agent_id, 1.0))
    except Exception:  # noqa: BLE001
        return 1.0


def reset() -> dict:
    _save(dict(_DEFAULT))
    return get_state()
