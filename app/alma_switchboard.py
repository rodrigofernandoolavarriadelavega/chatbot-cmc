"""Switchboard de Alma — única fuente de verdad para los overrides de runtime.

Las dos capas agénticas (alma_agents y alma_brain) resuelven todos sus
kill-switches a través de un único `_flag(name)` leído en cada llamada. Este
módulo intercepta esa resolución: si el dueño dejó un *override* explícito para
un flag desde el panel /alma/control, manda el override; si no, manda el valor
del entorno (que por defecto es false → apagado).

Diseño:
  - Default OFF se preserva: sin override → manda el env → apagado.
  - El switchboard solo enciende algo si el dueño lo toca explícitamente.
  - Persistente (sobrevive reinicio) en data/alma_switchboard.json.
  - Sin dependencias de las capas alma → no crea ciclos de import.

NO gobierna el piso legal (consent Ley 21.719, escritura Medilink, riesgo
extremo, horas de silencio, presupuesto anti-spam). Esos candados siguen
siendo solo-env y no se exponen como interruptores casuales.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path

log = logging.getLogger("bot")

_PATH = Path(os.getenv("ALMA_SWITCHBOARD_PATH", "data/alma_switchboard.json"))
_AUDIT_PATH = Path(os.getenv("ALMA_SWITCHBOARD_AUDIT", "data/alma_switchboard_audit.jsonl"))
_LOCK = threading.Lock()
_CACHE: dict | None = None


def _load() -> dict:
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    try:
        _CACHE = json.loads(_PATH.read_text(encoding="utf-8")) if _PATH.exists() else {}
    except Exception as e:  # archivo corrupto → arrancar limpio, nunca tumbar el gating
        log.warning("alma_switchboard: no se pudo leer %s (%s) → vacío", _PATH, e)
        _CACHE = {}
    if not isinstance(_CACHE, dict):
        _CACHE = {}
    return _CACHE


def _save(data: dict) -> None:
    global _CACHE
    _CACHE = data
    try:
        _PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, _PATH)  # atómico
    except Exception as e:
        log.error("alma_switchboard: no se pudo escribir %s: %s", _PATH, e)


def _audit(flag: str, value, actor: str) -> None:
    """Anota un cambio en el log append-only (quién, qué, cuándo). Fail-safe."""
    try:
        import json as _json
        from datetime import datetime
        from zoneinfo import ZoneInfo
        ts = datetime.now(ZoneInfo("America/Santiago")).isoformat(timespec="seconds")
        rec = {"ts": ts, "flag": flag, "value": value, "actor": actor or "?"}
        _AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _AUDIT_PATH.open("a", encoding="utf-8") as f:
            f.write(_json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        log.warning("alma_switchboard: no se pudo auditar %s: %s", flag, e)


def get_audit(limit: int = 50) -> list[dict]:
    """Últimas N entradas del log de auditoría, más recientes primero."""
    try:
        import json as _json
        if not _AUDIT_PATH.exists():
            return []
        lines = _AUDIT_PATH.read_text(encoding="utf-8").splitlines()
        out = []
        for ln in lines[-limit:]:
            try:
                out.append(_json.loads(ln))
            except Exception:
                continue
        return list(reversed(out))
    except Exception:
        return []


def effective(name: str, env_value: bool) -> bool:
    """Valor efectivo de un flag: override del dueño si existe, si no el env.

    Esta es la función que consultan los `_flag()` de cada capa. Es fail-safe:
    cualquier error cae al valor del entorno (el comportamiento previo).
    """
    try:
        ov = _load()
        if name in ov:
            return bool(ov[name])
    except Exception:
        pass
    return env_value


def get_overrides() -> dict:
    """Copia de los overrides actuales {flag: bool}."""
    return dict(_load())


def set_flag(name: str, value: bool | None, actor: str = "?") -> dict:
    """Fija (True/False) o limpia (None → vuelve al env) un override.

    Devuelve el dict de overrides resultante.
    """
    with _LOCK:
        data = dict(_load())
        if value is None:
            data.pop(name, None)
        else:
            data[name] = bool(value)
        _save(data)
        log.info("alma_switchboard: %s := %s (por %s)", name, value, actor)
    _audit(name, value, actor)
    return data


def reset(actor: str = "?") -> dict:
    """Limpia TODOS los overrides → todo vuelve al entorno (apagado por defecto)."""
    with _LOCK:
        _save({})
        log.info("alma_switchboard: reset total (por %s)", actor)
    _audit("*reset*", None, actor)
    return {}
