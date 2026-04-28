"""
Google Places API wrapper para rating + reseñas del CMC.

Usa Places API (New) v1 con `Place Details (Essentials)` que devuelve:
rating + userRatingCount + reviews[] (texto, autor, fecha, estrellas).

Cache en memoria por 6 horas para no exceder cuota gratuita
(Google da $200 USD/mes, ~28k requests de Place Details).

Requiere en .env:
    GOOGLE_PLACES_API_KEY=...
    GOOGLE_PLACE_ID=ChIJfwqzraTvaZYRBlt0l4W85JE   # opcional, fallback abajo

Uso:
    from google_rating import fetch_rating, get_review_link
    data = await fetch_rating()    # {"rating", "review_count", "reviews", ...}
    link = get_review_link()       # https://search.google.com/local/writereview?placeid=...
"""
import logging
import os
import time
from typing import Any

import httpx

log = logging.getLogger(__name__)

PLACE_ID = os.getenv("GOOGLE_PLACE_ID", "ChIJfwqzraTvaZYRBlt0l4W85JE")
API_KEY  = os.getenv("GOOGLE_PLACES_API_KEY", "").strip()
CACHE_TTL_SECONDS = 6 * 3600

_CACHE: dict[str, Any] = {"data": None, "fetched_at": 0.0}


async def fetch_rating(force: bool = False) -> dict[str, Any]:
    """Devuelve {rating, review_count, reviews[], updated_at, source}.
    Cache de 6h. Si la API falla, devuelve último valor cacheado o defaults."""
    now = time.time()
    if (not force
        and _CACHE["data"]
        and (now - _CACHE["fetched_at"] < CACHE_TTL_SECONDS)):
        return _CACHE["data"]

    if not API_KEY:
        fallback = _CACHE["data"] or {
            "rating": None, "review_count": None, "reviews": [],
            "updated_at": int(now), "source": "no_api_key",
        }
        return fallback

    url = f"https://places.googleapis.com/v1/places/{PLACE_ID}"
    headers = {
        "X-Goog-Api-Key": API_KEY,
        "X-Goog-FieldMask": (
            "rating,userRatingCount,"
            "reviews.text,reviews.rating,"
            "reviews.authorAttribution.displayName,"
            "reviews.publishTime,reviews.relativePublishTimeDescription"
        ),
    }
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(url, headers=headers)
        if r.status_code != 200:
            log.warning("google_rating http %s: %s", r.status_code, r.text[:160])
            return _CACHE["data"] or {
                "rating": None, "review_count": None, "reviews": [],
                "updated_at": int(now), "source": f"http_{r.status_code}",
            }
        d = r.json()
        result = {
            "rating": d.get("rating"),
            "review_count": d.get("userRatingCount"),
            "reviews": [_normalize_review(rv) for rv in (d.get("reviews") or [])[:6]],
            "updated_at": int(now),
            "source": "google_places_v1",
        }
        _CACHE["data"] = result
        _CACHE["fetched_at"] = now
        log.info("google_rating refreshed: %s ★ · %s reseñas",
                 result["rating"], result["review_count"])
        return result
    except (httpx.TimeoutException, httpx.NetworkError, httpx.RequestError) as e:
        log.warning("google_rating network error: %s", e)
        return _CACHE["data"] or {
            "rating": None, "review_count": None, "reviews": [],
            "updated_at": int(now), "source": f"net_error",
        }


def _normalize_review(rv: dict) -> dict:
    """Saca campos planos del schema anidado de Google Places v1."""
    text = (rv.get("text") or {}).get("text", "") or ""
    return {
        "author":        (rv.get("authorAttribution") or {}).get("displayName", "Anónimo"),
        "rating":        rv.get("rating"),
        "text":          text.strip(),
        "relative_time": rv.get("relativePublishTimeDescription", ""),
        "publish_time":  rv.get("publishTime", ""),
    }


def get_review_link() -> str:
    """Link directo a 'Escribir reseña' en Google Maps para el CMC.
    Pegar este link al paciente que dijo 'mejor' en el seguimiento."""
    return f"https://search.google.com/local/writereview?placeid={PLACE_ID}"


def initials(name: str) -> str:
    """Para avatares de testimonios. 'María Catalán P.' → 'MC'."""
    parts = [p for p in (name or "").split() if p and p[0].isalpha()]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[1][0]).upper()
