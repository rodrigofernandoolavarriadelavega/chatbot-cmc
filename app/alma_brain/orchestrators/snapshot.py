"""Snapshot dry-run de los orquestadores.

Corre `run_all(mode="dry")` una vez y persiste el resultado a un JSON atómico.
El panel lee el snapshot al instante (sin re-tocar BI/Medilink en cada carga), y
un cron puede refrescarlo 1x/día. Mismo patrón que alma_brain/state.py.

100% read-only: dry-run no persiste propuestas ni contacta a nadie.
"""
import json
import logging
import os
from datetime import datetime, timezone

log = logging.getLogger("bot")

SNAPSHOT_PATH = os.getenv("ALMA_ORQ_SNAPSHOT_PATH", "data/alma_orq_snapshot.json")


async def build_snapshot() -> dict:
    """Corre todos los orquestadores en dry y arma el snapshot (no persiste propuestas)."""
    from . import run_all, catalog
    resultados = await run_all(mode="dry")
    total = sum(len(r.get("proposals", [])) for r in resultados)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "catalogo": catalog(),
        "resultados": resultados,
        "total_propuestas_potenciales": total,
        "n_orquestadores": len(resultados),
    }


def save_snapshot(snap: dict) -> None:
    try:
        os.makedirs(os.path.dirname(SNAPSHOT_PATH) or ".", exist_ok=True)
        tmp = SNAPSHOT_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False, default=str)
        os.replace(tmp, SNAPSHOT_PATH)
    except Exception as e:  # noqa: BLE001
        log.warning("orq snapshot: no pude guardar (%s)", e)


def load_snapshot() -> dict | None:
    try:
        with open(SNAPSHOT_PATH, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:  # noqa: BLE001
        log.warning("orq snapshot: no pude leer (%s)", e)
        return None


async def build_and_save() -> dict:
    """Atajo para el cron: corre dry-run de todos y persiste."""
    snap = await build_snapshot()
    save_snapshot(snap)
    log.info("orq snapshot: %d orquestadores · %d propuestas potenciales",
             snap["n_orquestadores"], snap["total_propuestas_potenciales"])
    return snap
