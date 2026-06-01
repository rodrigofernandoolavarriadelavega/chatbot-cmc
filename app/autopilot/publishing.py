"""Publicación orgánica IG / Facebook / WhatsApp para el Autopilot de Marketing.

Esta es la "capa de publicación" del segmento **Instagram · Facebook · WhatsApp**
del módulo Diseños: toma una pieza de la galería (`designs.py`) y la lleva a los
canales orgánicos vía Graph API, con el mismo blindaje que el resto del autopilot.

Modelo de operación (decidido con el dueño, 2026-05-31): **cola con aprobación**.
  1. Una pieza entra a la cola (`enqueue`) en estado `cola`.
  2. El dueño la aprueba con 1 clic (`approve`) → estado `aprobado` + `scheduled_at`.
  3. El job `_job_organic_publish_queue` (cron cada 5 min) publica las que ya
     vencieron su hora. La escritura real a Meta está **bloqueada** salvo que
     `ORGANIC_PUBLISH_EXECUTE=true` en el `.env` del server (kill-switch global,
     mismo patrón que `AUTOPILOT_EXECUTE`).

Canales:
  - `instagram_feed`  → container + media_publish
  - `instagram_story` → container media_type=STORIES + media_publish
  - `facebook_feed`   → POST /{page}/photos (publicado)
  - `facebook_story`  → upload no publicado + POST /{page}/photo_stories
  - `whatsapp_status` → NO se automatiza (la API de WhatsApp no publica Estados);
                        el segmento solo ofrece descargar la pieza para subirla a mano.
  - `whatsapp_broadcast` → GATEADO por Ley 21.719 (consent). No envía aquí; queda
                        marcado y se delega al sprint Win-back cuando haya base opted-in.

Storage: JSON atómico en `data/publish_queue.json`, idéntico patrón a `designs.py`.
La cola nunca debe tumbar el dashboard: todo lo de lectura captura y degrada.
"""
import json
import logging
import os
from datetime import datetime, timezone

import httpx

log = logging.getLogger("bot")

GRAPH = "https://graph.facebook.com/v22.0"
QUEUE_PATH = os.getenv("PUBLISH_QUEUE_PATH", "data/publish_queue.json")
PUBLIC_BASE = os.getenv("PUBLIC_BASE_URL", "https://agentecmc.cl").rstrip("/")

# Canales que SÍ publican por código (los demás son manuales o gateados).
AUTO_CHANNELS = ("instagram_feed", "instagram_story", "facebook_feed", "facebook_story")
ALL_CHANNELS = AUTO_CHANNELS + ("whatsapp_status", "whatsapp_broadcast")

CHANNEL_LABEL = {
    "instagram_feed":   "Instagram · Feed",
    "instagram_story":  "Instagram · Historia",
    "facebook_feed":    "Facebook · Página",
    "facebook_story":   "Facebook · Historia",
    "whatsapp_status":  "WhatsApp · Estado (manual)",
    "whatsapp_broadcast": "WhatsApp · Difusión (gateado)",
}

VALID_STATUS = ("cola", "aprobado", "publicado", "error", "parcial")


# ── Config / conexión ────────────────────────────────────────────────────────

def _execute_enabled() -> bool:
    """Kill-switch maestro de escritura. False por defecto (Fase 0)."""
    return os.getenv("ORGANIC_PUBLISH_EXECUTE", "false").lower() == "true"


def _cfg() -> dict:
    """IDs y tokens necesarios. Reusa la config del bot (no duplica secrets)."""
    try:
        from config import (META_ACCESS_TOKEN, META_PAGE_ACCESS_TOKEN,
                            INSTAGRAM_USER_ID, META_PAGE_ID, META_PHONE_NUMBER_ID)
    except ImportError:  # pragma: no cover
        META_ACCESS_TOKEN = os.getenv("META_ACCESS_TOKEN", "")
        META_PAGE_ACCESS_TOKEN = os.getenv("META_PAGE_ACCESS_TOKEN", "") or META_ACCESS_TOKEN
        INSTAGRAM_USER_ID = os.getenv("INSTAGRAM_USER_ID", "")
        META_PAGE_ID = os.getenv("META_PAGE_ID", "")
        META_PHONE_NUMBER_ID = os.getenv("META_PHONE_NUMBER_ID", "")
    return {
        "ig_user": INSTAGRAM_USER_ID,
        "page_id": META_PAGE_ID,
        "page_token": META_PAGE_ACCESS_TOKEN or META_ACCESS_TOKEN,
        "user_token": META_ACCESS_TOKEN,
        "wa_phone": META_PHONE_NUMBER_ID,
    }


def connection_status() -> dict:
    """Qué canales están listos para publicar. Lo consume el dashboard para
    mostrar 'conectado' vs 'conecta tu cuenta' sin exponer tokens."""
    c = _cfg()
    return {
        "execute_enabled": _execute_enabled(),
        "channels": {
            "instagram_feed":   bool(c["ig_user"] and c["user_token"]),
            "instagram_story":  bool(c["ig_user"] and c["user_token"]),
            "facebook_feed":    bool(c["page_id"] and c["page_token"]),
            "facebook_story":   bool(c["page_id"] and c["page_token"]),
            "whatsapp_status":  True,  # siempre disponible (manual: descargar y subir)
            "whatsapp_broadcast": False,  # gateado por consent — se habilita en Win-back
        },
        "labels": CHANNEL_LABEL,
    }


def _public_url(image_url: str) -> str:
    """Convierte una ruta relativa (/static/..) en URL absoluta pública que Meta
    pueda descargar. Si ya es absoluta, la deja."""
    if not image_url:
        return ""
    if image_url.startswith("http://") or image_url.startswith("https://"):
        return image_url
    return PUBLIC_BASE + "/" + image_url.lstrip("/")


# ── Publicación a Meta (escritura — gateada) ─────────────────────────────────

async def _publish_instagram(image_url: str, caption: str, *, story: bool) -> dict:
    c = _cfg()
    if not (c["ig_user"] and c["user_token"]):
        return {"ok": False, "error": "Instagram no conectado (falta INSTAGRAM_USER_ID)"}
    img = _public_url(image_url)
    container = {"image_url": img, "access_token": c["user_token"]}
    if story:
        container["media_type"] = "STORIES"
    else:
        container["caption"] = caption or ""
    async with httpx.AsyncClient(timeout=60) as client:
        r1 = await client.post(f"{GRAPH}/{c['ig_user']}/media", data=container)
        if r1.status_code != 200:
            return {"ok": False, "error": f"container {r1.status_code}: {r1.text[:200]}"}
        creation_id = r1.json().get("id")
        if not creation_id:
            return {"ok": False, "error": "sin creation_id"}
        r2 = await client.post(f"{GRAPH}/{c['ig_user']}/media_publish",
                               data={"creation_id": creation_id, "access_token": c["user_token"]})
        if r2.status_code != 200:
            return {"ok": False, "error": f"publish {r2.status_code}: {r2.text[:200]}"}
        return {"ok": True, "id": r2.json().get("id")}


async def _publish_facebook(image_url: str, caption: str, *, story: bool) -> dict:
    c = _cfg()
    if not (c["page_id"] and c["page_token"]):
        return {"ok": False, "error": "Facebook no conectado (falta META_PAGE_ID)"}
    img = _public_url(image_url)
    async with httpx.AsyncClient(timeout=60) as client:
        if not story:
            r = await client.post(f"{GRAPH}/{c['page_id']}/photos",
                                  data={"url": img, "caption": caption or "",
                                        "published": "true", "access_token": c["page_token"]})
            if r.status_code != 200:
                return {"ok": False, "error": f"photo {r.status_code}: {r.text[:200]}"}
            return {"ok": True, "id": r.json().get("post_id") or r.json().get("id")}
        # Historia de Página: subir foto NO publicada y luego crear la historia.
        r1 = await client.post(f"{GRAPH}/{c['page_id']}/photos",
                               data={"url": img, "published": "false", "access_token": c["page_token"]})
        if r1.status_code != 200:
            return {"ok": False, "error": f"upload {r1.status_code}: {r1.text[:200]}"}
        photo_id = r1.json().get("id")
        r2 = await client.post(f"{GRAPH}/{c['page_id']}/photo_stories",
                               data={"photo_id": photo_id, "access_token": c["page_token"]})
        if r2.status_code != 200:
            return {"ok": False, "error": f"story {r2.status_code}: {r2.text[:200]}"}
        return {"ok": True, "id": r2.json().get("post_id") or photo_id}


async def _publish_channel(channel: str, image_url: str, caption: str) -> dict:
    """Despacha un canal. Respeta el kill-switch y los canales no automatizables."""
    if channel in ("whatsapp_status",):
        return {"ok": False, "skipped": True, "reason": "manual",
                "error": "WhatsApp Estado se sube a mano (la API no lo permite)"}
    if channel in ("whatsapp_broadcast",):
        return {"ok": False, "skipped": True, "reason": "gated",
                "error": "Difusión WhatsApp gateada por Ley 21.719 (requiere consent)"}
    if not _execute_enabled():
        return {"ok": False, "skipped": True, "reason": "kill-switch",
                "error": "Escritura bloqueada (ORGANIC_PUBLISH_EXECUTE=false)"}
    try:
        if channel == "instagram_feed":
            return await _publish_instagram(image_url, caption, story=False)
        if channel == "instagram_story":
            return await _publish_instagram(image_url, caption, story=True)
        if channel == "facebook_feed":
            return await _publish_facebook(image_url, caption, story=False)
        if channel == "facebook_story":
            return await _publish_facebook(image_url, caption, story=True)
        return {"ok": False, "error": f"canal desconocido: {channel}"}
    except Exception as e:  # noqa: BLE001 — un canal caído no debe tumbar la cola
        log.error("publish %s falló: %s", channel, e)
        return {"ok": False, "error": str(e)[:200]}


# ── Cola (JSON atómico) ──────────────────────────────────────────────────────

def load_queue() -> list[dict]:
    try:
        with open(QUEUE_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("items", []) if isinstance(data, dict) else data
    except FileNotFoundError:
        return []
    except Exception as e:  # noqa: BLE001
        log.error("load_queue falló: %s", e)
        return []


def _save_all(items: list[dict]) -> None:
    os.makedirs(os.path.dirname(QUEUE_PATH) or ".", exist_ok=True)
    tmp = QUEUE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"items": items, "updated_at": datetime.now(timezone.utc).isoformat()},
                  f, ensure_ascii=False, indent=2, default=str)
    os.replace(tmp, QUEUE_PATH)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def enqueue(record: dict) -> dict:
    """Agrega una pieza a la cola en estado `cola`. Requiere image_url + channels."""
    items = load_queue()
    rid = str(record.get("id") or f"pub{int(datetime.now(timezone.utc).timestamp())}")
    channels = [ch for ch in (record.get("channels") or []) if ch in ALL_CHANNELS]
    item = {
        "id": rid,
        "design_id": record.get("design_id"),
        "title": record.get("title") or "",
        "image_url": record.get("image_url") or "",
        "caption": record.get("caption") or "",
        "channels": channels,
        "scheduled_at": record.get("scheduled_at") or None,
        "status": "cola",
        "results": {},
        "created_at": _now(),
        "approved_at": None,
        "published_at": None,
    }
    items = [i for i in items if str(i.get("id")) != rid]
    items.insert(0, item)
    _save_all(items)
    return item


def approve(rid: str, scheduled_at: str | None = None) -> dict | None:
    """Pasa una pieza a `aprobado`. Si no hay hora, se publica en la próxima
    corrida del job (ASAP)."""
    items = load_queue()
    for i in items:
        if str(i.get("id")) == str(rid):
            i["status"] = "aprobado"
            i["approved_at"] = _now()
            if scheduled_at:
                i["scheduled_at"] = scheduled_at
            _save_all(items)
            return i
    return None


def delete(rid: str) -> bool:
    items = load_queue()
    kept = [i for i in items if str(i.get("id")) != str(rid)]
    if len(kept) == len(items):
        return False
    _save_all(kept)
    return True


def _is_due(item: dict) -> bool:
    """¿Le toca publicarse? Aprobada y (sin hora o ya vencida)."""
    if item.get("status") != "aprobado":
        return False
    sched = item.get("scheduled_at")
    if not sched:
        return True
    try:
        dt = datetime.fromisoformat(str(sched).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) >= dt
    except Exception:  # noqa: BLE001 — hora corrupta → publica ya, no se traba
        return True


async def publish_item(rid: str) -> dict | None:
    """Publica una pieza por todos sus canales y guarda el resultado por canal."""
    items = load_queue()
    item = next((i for i in items if str(i.get("id")) == str(rid)), None)
    if not item:
        return None
    results = dict(item.get("results") or {})
    for ch in item.get("channels", []):
        res = await _publish_channel(ch, item.get("image_url", ""), item.get("caption", ""))
        res["at"] = _now()
        results[ch] = res
    ok = [c for c, r in results.items() if r.get("ok")]
    failed = [c for c, r in results.items()
              if not r.get("ok") and not r.get("skipped")]
    if ok and not failed:
        item["status"] = "publicado"
    elif ok and failed:
        item["status"] = "parcial"
    elif failed:
        item["status"] = "error"
    else:
        # Todo skipped (kill-switch / manual / gated): se mantiene aprobado para reintentar.
        item["status"] = "aprobado"
    item["results"] = results
    item["published_at"] = _now() if ok else item.get("published_at")
    _save_all(items)
    return item


async def run_due_queue() -> dict:
    """Job del scheduler: publica todas las piezas aprobadas que ya vencieron.
    Devuelve un resumen para el log. Nunca lanza (degrada por ítem)."""
    items = load_queue()
    due = [i for i in items if _is_due(i)]
    published = 0
    for i in due:
        try:
            r = await publish_item(i["id"])
            if r and r.get("status") in ("publicado", "parcial"):
                published += 1
        except Exception as e:  # noqa: BLE001
            log.error("run_due_queue ítem %s falló: %s", i.get("id"), e)
    if due:
        log.info("[publish] cola: %d vencidas, %d publicadas (execute=%s)",
                 len(due), published, _execute_enabled())
    return {"due": len(due), "published": published, "execute": _execute_enabled()}
