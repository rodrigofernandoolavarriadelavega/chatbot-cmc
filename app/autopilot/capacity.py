"""Capacidad de agenda por especialidad — para gasto consciente de oferta.

El Autopilot, por defecto, escala lo que convierte. Pero si la agenda de una
especialidad ya está saturada (la próxima hora libre está a 2 semanas), escalar
ads de ESA especialidad quema plata generando demanda que no se puede atender y
frustra pacientes. Este módulo mide, barato y fail-safe, cuántos días faltan para
la próxima hora libre de cada especialidad (vía Medilink /proxima, cacheado).

Regla que habilita en policy: no escalar una especialidad saturada; ese presupuesto
rinde más en una especialidad con boxes ociosos. La saturación además es señal de
CONTRATACIÓN (más demanda que capacidad).

Fail-safe: si no se puede determinar la fecha (Medilink caído, especialidad sin
mapeo), se marca `known=False` y NO se bloquea gasto (no frenar por dato incierto).
"""
import json
import logging
import time
from datetime import date
from pathlib import Path

log = logging.getLogger("bot")

_SNAPSHOT = Path(__file__).parent.parent.parent / "data" / "capacity_snapshot.json"
_TTL = 12 * 3600  # recomputar a lo sumo 2×/día (no martillar Medilink)


def _today() -> date:
    try:
        from zoneinfo import ZoneInfo
        from datetime import datetime
        return datetime.now(ZoneInfo("America/Santiago")).date()
    except Exception:  # noqa: BLE001
        from datetime import datetime
        return datetime.utcnow().date()


async def capacity_for(specialties, *, saturated_days: int = 10) -> dict:
    """{especialidad: {days_to_next, saturated, known}}. Lee snapshot si está fresco."""
    specialties = sorted({(s or "").strip().lower() for s in specialties if s})
    if not specialties:
        return {}

    cached = _load()
    now = int(time.time())
    if cached and (now - cached.get("ts", 0)) < _TTL:
        have = cached.get("data", {})
        if all(s in have for s in specialties):
            return {s: have[s] for s in specialties}

    import medilink
    today = _today()
    out: dict = {}
    for esp in specialties:
        # Señal Medilink = SOLO INFORMATIVA. El CMC hace SOBRECUPOS, así que el horario
        # formal sub-cuenta la capacidad real (eco: /proxima dice 11 días pero hay 25
        # cupos vía sobrecupo). Por eso NUNCA gatea sola; la autoridad es el override.
        try:
            fecha = await medilink.proxima_fecha_especialidad(esp)
        except Exception as e:  # noqa: BLE001
            log.warning("capacity: %s falló: %s", esp, e)
            fecha = None
        days = None
        if fecha:
            try:
                days = max(0, (date.fromisoformat(fecha) - today).days)
            except Exception:  # noqa: BLE001
                days = None
        out[esp] = {"days_to_next_formal": days, "known": days is not None}

    # Override del dueño = AUTORIDAD. Default = SEGUIR LLENANDO (no gatea).
    # capacity_override[esp] = {"status": "fill"|"lleno", "target": int|None}
    try:
        from . import settings as _s
        override = _s.get("capacity_override") or {}
    except Exception:  # noqa: BLE001
        override = {}
    for esp, v in out.items():
        ov = override.get(esp) or {}
        status = ov.get("status", "fill")          # default: llenar
        v["status"] = status
        v["target"] = ov.get("target")
        v["gate"] = (status == "lleno")            # solo frena si el dueño lo marcó lleno

    merged = dict((cached or {}).get("data", {}))
    merged.update(out)
    _save({"ts": now, "data": merged})
    return out


def _load() -> dict | None:
    if not _SNAPSHOT.exists():
        return None
    try:
        return json.loads(_SNAPSHOT.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def _save(d: dict) -> None:
    try:
        _SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        _SNAPSHOT.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        log.warning("capacity: no se pudo guardar snapshot: %s", e)


def load_snapshot() -> dict | None:
    return _load()
