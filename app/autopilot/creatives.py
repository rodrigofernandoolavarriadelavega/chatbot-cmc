"""Loop de medición de creatividades — ranking de anuncios por rendimiento real.

Cierra el bucle producción→optimización de la capa creativa: no basta con generar
imágenes y publicarlas, hay que medir CUÁL convierte mejor y retroalimentar. Toma
los insights a nivel ANUNCIO (no campaña), los juzga con la MISMA lógica económica
por especialidad de policy.evaluate_economics, y los ordena de mejor a peor.

Resultado: el dueño (y eventualmente el motor) ve qué creatividad rinde y cuál
quemar — la señal que faltaba para que generar más arte sea optimización, no solo
producción. Read-only: nunca toca Meta ni mueve plata.
"""
import json
import logging
import os
import time
from pathlib import Path

log = logging.getLogger("bot")

_SNAPSHOT = Path(__file__).parent.parent.parent / "data" / "creatives_snapshot.json"

# Orden de los veredictos para rankear (mejor primero).
_VERDICT_RANK = {"winner": 0, "ok": 1, "marginal": 2, "loser": 3, "unknown": 4}


def _state_from_ad(ad: dict):
    """Construye un CampaignState ligero por anuncio para reusar evaluate_economics.

    El nombre combina campaña + anuncio para que la inferencia de especialidad
    (policy._infer_especialidad) capte la keyword venga de donde venga.
    """
    from .world_state import CampaignState
    name = f"{ad.get('campaign_name','')} {ad.get('ad_name','')}".strip()
    c = CampaignState(
        id=ad.get("ad_id", ""), name=name, objective="", effective_status="",
        daily_budget_clp=None,
    )
    for k in ("spend", "leads", "schedules", "purchases", "purchase_value",
              "cac_purchase", "roas_meta", "ctr", "cpm", "messages",
              "cost_per_message", "link_clicks", "cost_per_link_click"):
        if ad.get(k) is not None:
            setattr(c, k, ad[k])
    return c


def _sort_key(item: dict):
    vr = _VERDICT_RANK.get(item["verdict"], 5)
    # Dentro del mismo veredicto: el más barato por mensaje primero; si no hay
    # mensajes, el de mayor CTR. Spend como desempate (priorizar lo que ya escala).
    cpm = item.get("cost_per_message")
    cpm = cpm if cpm is not None else 1e12
    ctr = -(item.get("ctr") or 0)
    return (vr, cpm, ctr, -(item.get("spend") or 0))


async def rank_creatives(window_days: int = 30) -> dict:
    """Trae insights por anuncio, los juzga y devuelve el ranking + agregados."""
    import httpx
    from .policy import evaluate_economics, HardLimits
    limits = HardLimits.from_env()
    preset = {7: "last_7d", 14: "last_14d", 30: "last_30d", 90: "last_90d"}.get(
        window_days, "last_30d")

    async with httpx.AsyncClient() as client:
        from . import meta_ads
        ads = await meta_ads.account_insights_by_ad(client, date_preset=preset)

    ranked: list[dict] = []
    for ad in ads:
        st = _state_from_ad(ad)
        verdict, conf, expl = evaluate_economics(st, limits)
        ranked.append({
            "ad_id": ad.get("ad_id"),
            "ad_name": ad.get("ad_name") or "(sin nombre)",
            "campaign_name": ad.get("campaign_name") or "",
            "adset_name": ad.get("adset_name") or "",
            "spend": round(ad.get("spend") or 0),
            "messages": int(ad.get("messages") or 0),
            "purchases": int(ad.get("purchases") or 0),
            "link_clicks": int(ad.get("link_clicks") or 0),
            "ctr": round(ad.get("ctr") or 0, 2),
            "cost_per_message": (round(ad["cost_per_message"])
                                 if ad.get("cost_per_message") is not None else None),
            "cac_purchase": (round(ad["cac_purchase"])
                             if ad.get("cac_purchase") is not None else None),
            "verdict": getattr(verdict, "value", verdict),
            "confidence": round(conf, 2),
            "explain": expl,
        })

    ranked.sort(key=_sort_key)
    total_spend = sum(r["spend"] for r in ranked)
    total_msgs = sum(r["messages"] for r in ranked)
    out = {
        "generated_ts": int(time.time()),
        "window_days": window_days,
        "n_ads": len(ranked),
        "total_spend": total_spend,
        "total_messages": total_msgs,
        "avg_cost_per_message": round(total_spend / total_msgs) if total_msgs else None,
        "best": ranked[0] if ranked else None,
        "worst": ranked[-1] if ranked and len(ranked) > 1 else None,
        "creatives": ranked,
    }
    return out


def save_snapshot(data: dict) -> None:
    try:
        _SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        _SNAPSHOT.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        log.warning("creatives: no se pudo guardar snapshot: %s", e)


def load_snapshot() -> dict | None:
    if not _SNAPSHOT.exists():
        return None
    try:
        return json.loads(_SNAPSHOT.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
