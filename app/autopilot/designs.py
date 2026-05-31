"""Galería de diseños publicitarios (Canva) para el Autopilot de Marketing.

Storage simple en JSON — mismo patrón que `world_state.save_snapshot`. Cada
registro es una pieza de marketing generada con Canva que se renderiza
**embebida** en el dashboard `/autopilot` (pestaña "Diseños"), visible desde Ánima.

Flujo de datos: la generación con Canva ocurre vía MCP (lado asistente), no en
este backend. Los diseños entran por `POST /autopilot/api/designs` (o sembrando
`data/ad_designs.json`). La imagen se auto-hospeda en `/static/ad_designs/` para
que el dashboard no dependa de URLs efímeras de Canva (los thumbnails de Canva
caducan; un PNG en /static no).

Esquema de un registro:
  {
    "id":          "ecografia-1",          # único; sirve de nombre de archivo
    "title":       "Ecografía en Carampangue",
    "specialty":   "Ecografía",            # categoría/etiqueta libre
    "format":      "instagram_post",       # tipo de pieza Canva
    "image_url":   "/static/ad_designs/ecografia-1.png",  # imagen auto-hospedada
    "edit_url":    "https://www.canva.com/d/...",          # editar en Canva
    "thumbnail_url": "https://design.canva.ai/...",        # fallback si no hay PNG local
    "status":      "borrador",             # borrador | aprobado | publicado
    "created_at":  "2026-05-31T..."        # ISO; se autocompleta
  }
"""
import json
import logging
import os
from datetime import datetime, timezone

log = logging.getLogger("bot")

DESIGNS_PATH = os.getenv("AD_DESIGNS_PATH", "data/ad_designs.json")


def load_designs() -> list[dict]:
    """Lee la galería completa (más reciente primero). Devuelve [] si no existe."""
    try:
        with open(DESIGNS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        # Aceptamos tanto {"designs": [...]} como una lista cruda.
        return data.get("designs", []) if isinstance(data, dict) else data
    except FileNotFoundError:
        return []
    except Exception as e:  # noqa: BLE001 — la galería nunca debe tumbar el dashboard
        log.error("load_designs falló: %s", e)
        return []


def _save_all(designs: list[dict]) -> None:
    """Escritura atómica (tmp + replace) como el resto del autopilot."""
    os.makedirs(os.path.dirname(DESIGNS_PATH) or ".", exist_ok=True)
    tmp = DESIGNS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(
            {"designs": designs, "updated_at": datetime.now(timezone.utc).isoformat()},
            f, ensure_ascii=False, indent=2, default=str,
        )
    os.replace(tmp, DESIGNS_PATH)


def add_design(record: dict) -> dict:
    """Agrega un diseño (o lo reemplaza si el `id` ya existe). Lo deja primero."""
    designs = load_designs()
    rid = str(record.get("id") or f"d{int(datetime.now(timezone.utc).timestamp())}")
    record["id"] = rid
    record.setdefault("status", "borrador")
    record.setdefault("created_at", datetime.now(timezone.utc).isoformat())
    designs = [d for d in designs if str(d.get("id")) != rid]
    designs.insert(0, record)  # más reciente primero
    _save_all(designs)
    return record


VALID_STATUS = ("borrador", "aprobado", "publicado")


def set_status(rid: str, status: str) -> bool:
    """Cambia el estado de un diseño (flujo de aprobación). True si existía."""
    designs = load_designs()
    for d in designs:
        if str(d.get("id")) == str(rid):
            d["status"] = status
            _save_all(designs)
            return True
    return False


def delete_design(rid: str) -> bool:
    """Elimina por id. Devuelve True si había algo que borrar."""
    designs = load_designs()
    kept = [d for d in designs if str(d.get("id")) != str(rid)]
    if len(kept) == len(designs):
        return False
    _save_all(kept)
    return True
