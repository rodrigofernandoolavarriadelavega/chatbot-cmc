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
        "angles": angle_insights(ranked),
    }
    return out


# Tokens que NO son ángulos creativos (códigos de especialidad, canal, edición…).
_ANGLE_STOP = {
    "ECO", "ORTO", "CMC", "WA", "LOCAL", "EDITION", "BATTLE", "CLAUDE",
    "PUBLICACION", "PUBLICACIÓN", "POST", "VIDEO", "IMAGEN", "COPY", "TEST",
    "FB", "IG", "ADS", "AD", "V1", "V2", "V3", "FINAL", "NUEVO", "NUEVA",
    # Marca / boilerplate (no son ángulos creativos):
    "CENTRO", "MEDICO", "MÉDICO", "MEDICA", "MÉDICA", "CARAMPANGUE",
    "SALUD", "HOLA", "PARA", "DEJES", "PROBLEMAS",
}


def _angle_tokens(name: str) -> list[str]:
    """Extrae los tokens de 'ángulo' de un nombre de anuncio (ej. ECO-C2-FUNCIONAL
    → ['FUNCIONAL']). Descarta códigos (A1/C2/B3), especialidad y canal."""
    import re
    raw = re.split(r"[-_\s]+", (name or "").upper())
    out = []
    for t in raw:
        t = t.strip()
        if len(t) < 4 or not t.isalpha():   # descarta C2, A3, WA, y separadores
            continue
        if t in _ANGLE_STOP:
            continue
        out.append(t)
    return out


def angle_insights(ranked: list[dict]) -> dict:
    """Agrupa las creatividades por 'ángulo' (palabra del nombre: FUNCIONAL, DIRECTO,
    PADRES…) y rankea cada ángulo por costo/mensaje. Cierra el loop: dice qué ángulo
    DUPLICAR y cuál DESCARTAR para la próxima tanda de creatividades.
    """
    agg: dict[str, dict] = {}
    for r in ranked:
        name = f"{r.get('ad_name','')} {r.get('campaign_name','')}"
        for tok in set(_angle_tokens(name)):
            a = agg.setdefault(tok, {"angle": tok, "ads": 0, "spend": 0,
                                     "messages": 0, "winners": 0})
            a["ads"] += 1
            a["spend"] += r.get("spend") or 0
            a["messages"] += r.get("messages") or 0
            if r.get("verdict") == "winner":
                a["winners"] += 1
    angles = []
    for a in agg.values():
        a["cost_per_message"] = round(a["spend"] / a["messages"]) if a["messages"] else None
        angles.append(a)

    # Con tracción medible: tienen mensajes. El mejor = menor costo/mensaje.
    with_traction = [a for a in angles if a["messages"] >= 10]
    with_traction.sort(key=lambda a: a["cost_per_message"])
    # Probados con gasto pero sin tracción (≥$3.000 y <3 mensajes) = candidatos a descartar.
    duds = [a for a in angles if a["spend"] >= 3000 and a["messages"] < 3]
    duds.sort(key=lambda a: -a["spend"])

    recs = []
    if with_traction:
        best = with_traction[0]
        recs.append({
            "kind": "scale_angle", "angle": best["angle"],
            "text": (f"Duplicá el ángulo «{best['angle']}»: ${best['cost_per_message']:,}/msg "
                     f"en {best['ads']} anuncio(s). Generá más variantes de ese ángulo."),
        })
    for d in duds[:2]:
        recs.append({
            "kind": "drop_angle", "angle": d["angle"],
            "text": (f"Descartá el ángulo «{d['angle']}»: gastó ${d['spend']:,} y casi no "
                     f"generó conversaciones. No insistas con ese mensaje."),
        })
    return {
        "ranking": with_traction,
        "recommendations": recs,
    }


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


# ── #5 — Generación automática de variantes del ángulo ganador ───────────────

async def autogen_winner(snapshot: dict | None = None, *, n: int = 1) -> list[dict]:
    """Genera n variante(s) del ÁNGULO GANADOR y las deja como borrador en la galería.

    Cierra el bucle producción→optimización: el ranking ya dice qué ángulo rinde
    (recommendations[0]); acá se generan piezas nuevas de ese ángulo, listas para que
    el dueño las revise/publique. Usa el MISMO pipeline que la generación manual
    (build_prompt → generate_png → add_design). Cuesta ~$75 CLP/imagen (gpt-image),
    por eso vive detrás del toggle `autogen` (OFF por defecto). Read-only sobre Meta.
    """
    snap = snapshot or load_snapshot() or {}
    recs = (snap.get("angles") or {}).get("recommendations") or []
    scale = next((r for r in recs if r.get("kind") == "scale_angle"), None)
    if not scale:
        log.info("autogen: sin ángulo ganador claro, no genero")
        return []
    angle = scale.get("angle", "")
    best = snap.get("best") or {}
    # Especialidad del mejor anuncio (para que la pieza sea del tema correcto).
    from .policy import _infer_especialidad
    esp = _infer_especialidad(f"{best.get('ad_name','')} {best.get('campaign_name','')}") or ""

    from .ad_formats import FORMAT_BY_KEY
    from .image_gen import build_prompt, generate_png, gpt_size_for, OPENAI_IMAGE_MODEL
    from .designs import add_design
    import time as _t
    from pathlib import Path as _P

    fmt = FORMAT_BY_KEY.get("instagram_story") or next(iter(FORMAT_BY_KEY.values()))
    title = (esp.title() or "Centro Médico Carampangue")
    brief = (f"Variante NUEVA del ángulo ganador «{angle}» (el que mejor convierte en "
             f"WhatsApp). Mantené ese enfoque y tono, cambiá la composición.")
    static_dir = _P(__file__).parent.parent.parent / "static" / "ad_designs"

    created: list[dict] = []
    for i in range(max(1, n)):
        try:
            prompt = build_prompt(fmt, title=title, subtitle="", specialty=esp, brief=brief)
            png = await generate_png(prompt, size=gpt_size_for(fmt))
            rid = f"autogen-{angle.lower()}-{int(_t.time())}-{i}"
            static_dir.mkdir(parents=True, exist_ok=True)
            (static_dir / f"{rid}.png").write_bytes(png)
            rec = add_design({
                "id": rid, "title": f"{title} · {angle}", "specialty": esp,
                "format": fmt["key"], "image_url": f"/static/ad_designs/{rid}.png",
                "status": "borrador", "source": f"autogen:{OPENAI_IMAGE_MODEL}",
                "angle": angle,
            })
            created.append(rec)
            log.info("autogen: generada variante de «%s» (%s)", angle, rid)
        except Exception as e:  # noqa: BLE001
            log.error("autogen: falló generación de «%s»: %s", angle, e)
            break
    return created
