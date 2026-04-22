"""Flight search routes — public /vuelos/* endpoints.

Proxies Google Flights via SerpAPI with two modes:
  1. Direct: single-ticket results from origin->destination (round-trip or one-way).
  2. Self-transfer: builds itineraries by combining atomic one-way legs
     origin->SCL + SCL->destination (and the mirror for the return), which often
     unlocks combinations Google Flights hides because they span two tickets.

Public endpoint (no auth), unlike admin_routes.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from itertools import product
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates

log = logging.getLogger("bot")

router = APIRouter(tags=["vuelos"])

# ── Templates ───────────────────────────────────────────────────────────────

_TEMPLATE_DIR = Path(__file__).parent.parent / "templates"
_templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))


# ── Config ──────────────────────────────────────────────────────────────────

SERPAPI_URL = "https://serpapi.com/search.json"
HUB = "SCL"                  # self-transfer hub (Santiago)
MIN_LAYOVER_MIN = 90         # minimum layover for self-transfer
HTTP_TIMEOUT = 35.0          # seconds per SerpAPI call
MAX_RESULTS = 20             # top-N self-transfer itineraries returned


def _serpapi_key() -> str:
    key = os.getenv("SERPAPI_KEY", "").strip()
    if not key:
        raise HTTPException(
            status_code=500,
            detail="SERPAPI_KEY no configurada en .env",
        )
    return key


# ── SerpAPI helpers ─────────────────────────────────────────────────────────

def _base_params(
    origin: str,
    destination: str,
    outbound_date: str,
    return_date: str | None,
    adults: int,
    api_key: str,
) -> dict[str, str]:
    """Build the common query params. Caller appends `type=2` for one-way."""
    p: dict[str, str] = {
        "engine": "google_flights",
        "departure_id": origin,
        "arrival_id": destination,
        "outbound_date": outbound_date,
        "currency": "CLP",
        "hl": "es",
        "gl": "cl",
        "stops": "0",          # "0" = any number of stops (SerpAPI convention)
        "adults": str(max(1, int(adults))),
        "api_key": api_key,
    }
    if return_date:
        p["return_date"] = return_date
    else:
        p["type"] = "2"        # one-way
    return p


async def _serpapi_get(
    client: httpx.AsyncClient, params: dict[str, str]
) -> dict[str, Any]:
    """Call SerpAPI. Raises HTTPException(502) on failure."""
    try:
        r = await client.get(SERPAPI_URL, params=params, timeout=HTTP_TIMEOUT)
    except (httpx.TimeoutException, httpx.RequestError) as e:
        log.warning("SerpAPI network error: %s", e)
        raise HTTPException(
            status_code=502,
            detail={"error": "upstream", "detail": f"SerpAPI unreachable: {e}"},
        )
    if r.status_code != 200:
        log.warning("SerpAPI HTTP %s: %s", r.status_code, r.text[:200])
        raise HTTPException(
            status_code=502,
            detail={"error": "upstream", "detail": f"SerpAPI HTTP {r.status_code}"},
        )
    try:
        data = r.json()
    except ValueError as e:
        raise HTTPException(
            status_code=502,
            detail={"error": "upstream", "detail": f"SerpAPI bad JSON: {e}"},
        )
    # SerpAPI reports search errors inside the body.
    meta_status = (data.get("search_metadata") or {}).get("status")
    if meta_status and meta_status not in ("Success", "Cached"):
        err = data.get("error") or f"search_metadata.status={meta_status}"
        raise HTTPException(
            status_code=502,
            detail={"error": "upstream", "detail": str(err)},
        )
    return data


# ── Parsers ─────────────────────────────────────────────────────────────────

def _parse_dt(s: str | None) -> datetime | None:
    """SerpAPI returns 'YYYY-MM-DD HH:MM'."""
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M")
    except ValueError:
        return None


def _total_layover_min(option: dict[str, Any]) -> int:
    """Sum of layover durations (min) in a multi-leg Google Flights option."""
    layovers = option.get("layovers") or []
    return sum(int(lay.get("duration") or 0) for lay in layovers)


def _filter_direct(options: list[dict[str, Any]], max_layover_min: int) -> list[dict[str, Any]]:
    """Keep only options whose total layover time is within budget."""
    out: list[dict[str, Any]] = []
    for opt in options or []:
        if _total_layover_min(opt) <= max_layover_min:
            out.append(opt)
    return out


def _atomic_legs(options: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return only single-segment flights with a price — usable as atomic legs.

    SerpAPI sometimes omits `price` for specific options; those can't be
    combined into an itinerary with a total price, so we skip them.
    """
    legs: list[dict[str, Any]] = []
    for opt in options or []:
        flights = opt.get("flights") or []
        if len(flights) != 1:
            continue
        price = opt.get("price")
        if not isinstance(price, (int, float)) or price <= 0:
            continue
        legs.append(opt)
    return legs


def _combine_legs(
    leg_a: dict[str, Any],
    leg_b: dict[str, Any],
    min_layover: int,
    max_layover: int,
) -> dict[str, Any] | None:
    """Combine two consecutive atomic legs through the HUB into an itinerary."""
    fa = (leg_a.get("flights") or [{}])[0]
    fb = (leg_b.get("flights") or [{}])[0]
    arr_a = _parse_dt((fa.get("arrival_airport") or {}).get("time"))
    dep_b = _parse_dt((fb.get("departure_airport") or {}).get("time"))
    if not arr_a or not dep_b:
        return None
    if arr_a >= dep_b:
        return None
    layover_min = int((dep_b - arr_a).total_seconds() // 60)
    if layover_min < min_layover or layover_min > max_layover:
        return None
    dur_a = int(leg_a.get("total_duration") or fa.get("duration") or 0)
    dur_b = int(leg_b.get("total_duration") or fb.get("duration") or 0)
    price = int(leg_a.get("price", 0)) + int(leg_b.get("price", 0))
    return {
        "legs": [
            {
                "from": (fa.get("departure_airport") or {}).get("id"),
                "to": (fa.get("arrival_airport") or {}).get("id"),
                "departure": (fa.get("departure_airport") or {}).get("time"),
                "arrival": (fa.get("arrival_airport") or {}).get("time"),
                "airline": fa.get("airline"),
                "airline_logo": fa.get("airline_logo"),
                "flight_number": fa.get("flight_number"),
                "duration_min": dur_a,
                "price": leg_a.get("price"),
                "booking_token": leg_a.get("booking_token"),
            },
            {
                "from": (fb.get("departure_airport") or {}).get("id"),
                "to": (fb.get("arrival_airport") or {}).get("id"),
                "departure": (fb.get("departure_airport") or {}).get("time"),
                "arrival": (fb.get("arrival_airport") or {}).get("time"),
                "airline": fb.get("airline"),
                "airline_logo": fb.get("airline_logo"),
                "flight_number": fb.get("flight_number"),
                "duration_min": dur_b,
                "price": leg_b.get("price"),
                "booking_token": leg_b.get("booking_token"),
            },
        ],
        "hub": HUB,
        "layover_min": layover_min,
        "total_price": price,
        "total_duration_min": dur_a + layover_min + dur_b,
        "self_transfer": True,
    }


def _pair_legs(
    legs_a: list[dict[str, Any]],
    legs_b: list[dict[str, Any]],
    min_layover: int,
    max_layover: int,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for a, b in product(legs_a, legs_b):
        combo = _combine_legs(a, b, min_layover, max_layover)
        if combo:
            out.append(combo)
    return out


# ── Public: page ────────────────────────────────────────────────────────────

@router.get("/vuelos")
async def vuelos_page(request: Request):
    """Serve the flight-search UI (template rendered by a parallel agent)."""
    return _templates.TemplateResponse("vuelos.html", {"request": request})


@router.get("/vuelosv2")
async def vuelos_page_v2(request: Request):
    """Alternate v2 design for the flight-search landing."""
    return _templates.TemplateResponse("vuelosv2.html", {"request": request})


# ── Public: search API ──────────────────────────────────────────────────────

@router.get("/vuelos/api/search")
async def vuelos_search(
    origin: str = Query(..., min_length=3, max_length=3, description="IATA origen"),
    destination: str = Query(..., min_length=3, max_length=3, description="IATA destino"),
    outbound_date: str = Query(..., description="YYYY-MM-DD"),
    return_date: str | None = Query(None, description="YYYY-MM-DD o vacío"),
    max_layover_min: int = Query(300, ge=0, le=1440),
    allow_self_transfer: bool = Query(True),
    adults: int = Query(1, ge=1, le=9),
):
    """Flight search. See module docstring for the combined self-transfer logic."""
    origin = origin.upper()
    destination = destination.upper()
    return_date = (return_date or "").strip() or None

    # Validate dates early — SerpAPI will reject bad ones, but we want a clear 400.
    for label, value in (("outbound_date", outbound_date), ("return_date", return_date)):
        if value:
            try:
                datetime.strptime(value, "%Y-%m-%d")
            except ValueError:
                raise HTTPException(
                    status_code=400,
                    detail=f"{label} inválida (se espera YYYY-MM-DD)",
                )

    api_key = _serpapi_key()

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        # 1. Direct search.
        direct_params = _base_params(
            origin, destination, outbound_date, return_date, adults, api_key
        )
        try:
            direct_data = await _serpapi_get(client, direct_params)
        except HTTPException as e:
            # Bubble upstream errors as 502 JSON (FastAPI wraps HTTPException already).
            raise e

        direct_best = direct_data.get("best_flights") or []
        direct_other = direct_data.get("other_flights") or []
        direct_all = direct_best + direct_other
        direct_filtered = _filter_direct(direct_all, max_layover_min)

        # 2. Self-transfer search (parallel leg queries).
        self_transfer: list[dict[str, Any]] = []
        if allow_self_transfer and origin != HUB and destination != HUB:
            tasks = {
                "out_a": _serpapi_get(
                    client,
                    _base_params(origin, HUB, outbound_date, None, adults, api_key),
                ),
                "out_b": _serpapi_get(
                    client,
                    _base_params(HUB, destination, outbound_date, None, adults, api_key),
                ),
            }
            if return_date:
                tasks["ret_a"] = _serpapi_get(
                    client,
                    _base_params(destination, HUB, return_date, None, adults, api_key),
                )
                tasks["ret_b"] = _serpapi_get(
                    client,
                    _base_params(HUB, origin, return_date, None, adults, api_key),
                )

            keys = list(tasks.keys())
            try:
                results = await asyncio.gather(
                    *(tasks[k] for k in keys), return_exceptions=True
                )
            except Exception as e:
                log.warning("self-transfer gather failed: %s", e)
                results = []

            leg_data: dict[str, list[dict[str, Any]]] = {}
            for k, res in zip(keys, results):
                if isinstance(res, Exception):
                    log.warning("self-transfer leg %s failed: %s", k, res)
                    leg_data[k] = []
                    continue
                best = res.get("best_flights") or []
                other = res.get("other_flights") or []
                leg_data[k] = _atomic_legs(best + other)

            out_combos = _pair_legs(
                leg_data.get("out_a", []),
                leg_data.get("out_b", []),
                MIN_LAYOVER_MIN,
                max_layover_min,
            )
            if return_date:
                ret_combos = _pair_legs(
                    leg_data.get("ret_a", []),
                    leg_data.get("ret_b", []),
                    MIN_LAYOVER_MIN,
                    max_layover_min,
                )
                # Cartesian product outbound × return
                for o, r in product(out_combos, ret_combos):
                    self_transfer.append({
                        "outbound": o,
                        "return": r,
                        "total_price": o["total_price"] + r["total_price"],
                        "total_duration_min": o["total_duration_min"] + r["total_duration_min"],
                    })
                self_transfer.sort(key=lambda x: x["total_price"])
            else:
                self_transfer = [
                    {
                        "outbound": o,
                        "return": None,
                        "total_price": o["total_price"],
                        "total_duration_min": o["total_duration_min"],
                    }
                    for o in out_combos
                ]
                self_transfer.sort(key=lambda x: x["total_price"])

            self_transfer = self_transfer[:MAX_RESULTS]

    return JSONResponse({
        "direct": direct_filtered,
        "self_transfer": self_transfer,
        "price_insights": direct_data.get("price_insights") or {},
        "currency": "CLP",
        "meta": {
            "origin": origin,
            "destination": destination,
            "outbound_date": outbound_date,
            "return_date": return_date,
            "adults": adults,
            "max_layover_min": max_layover_min,
            "hub": HUB if allow_self_transfer else None,
            "direct_count_raw": len(direct_all),
            "direct_count_filtered": len(direct_filtered),
            "self_transfer_count": len(self_transfer),
        },
    })
