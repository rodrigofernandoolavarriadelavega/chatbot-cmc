"""Ajustes runtime del Autopilot toggleables desde la UI (sin reiniciar).

A diferencia de los flags de entorno (AUTOPILOT_ENABLED/EXECUTE/AUTOAPPLY, que son
barandas duras y se setean en .env), estos son interruptores que el dueño prende y
apaga en vivo desde el panel. Hoy: `autogen` (generación automática de variantes del
creativo ganador). Persisten en data/autopilot_settings.json.
"""
import json
import logging
from pathlib import Path

log = logging.getLogger("bot")

_FILE = Path(__file__).parent.parent.parent / "data" / "autopilot_settings.json"

_DEFAULTS = {
    "autogen": False,   # #5 — generar solo variantes del ángulo ganador (OFF por defecto)
    # #1 — capacidad por especialidad. Default = seguir llenando (sobrecupos).
    # {especialidad: {"status": "fill"|"lleno", "target": int|None}}. Solo "lleno" frena.
    "capacity_override": {},
}


def set_capacity(especialidad: str, status: str = "fill", target=None) -> dict:
    """Marca una especialidad 'fill' (seguir llenando, default) o 'lleno' (no escalar
    más). target opcional (nº de cupos objetivo, informativo)."""
    esp = (especialidad or "").strip().lower()
    if not esp:
        return get_settings()
    ov = dict(get("capacity_override") or {})
    if status == "fill" and not target:
        ov.pop(esp, None)            # volver al default = quitar la marca
    else:
        ov[esp] = {"status": status if status in ("fill", "lleno") else "fill",
                   "target": int(target) if target else None}
    return set_setting("capacity_override", ov)


def get_settings() -> dict:
    out = dict(_DEFAULTS)
    try:
        if _FILE.exists():
            out.update(json.loads(_FILE.read_text(encoding="utf-8")) or {})
    except Exception as e:  # noqa: BLE001
        log.warning("autopilot settings: no se pudo leer: %s", e)
    return out


def get(key: str, default=None):
    return get_settings().get(key, default if default is not None else _DEFAULTS.get(key))


def set_setting(key: str, value) -> dict:
    s = get_settings()
    s[key] = value
    try:
        _FILE.parent.mkdir(parents=True, exist_ok=True)
        _FILE.write_text(json.dumps(s, ensure_ascii=False), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        log.warning("autopilot settings: no se pudo guardar: %s", e)
    return s
