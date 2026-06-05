"""Digest del Autopilot — resumen de los últimos N días para el dueño.

Cierra el loop de visibilidad: en vez de tener que abrir el panel, el dueño ve de un
vistazo qué hizo el autopilot (qué aplicó solo, qué aprobó/rechazó, qué espera su OK),
cuánto se gastó y qué creatividad rinde. Read-only: no toca Meta ni mueve plata.
"""
import logging
import time

log = logging.getLogger("bot")


def build_digest(days: int = 7) -> dict:
    from . import approvals
    cutoff = int(time.time()) - days * 86400

    recent = [r for r in approvals.list_recent(200) if (r.get("created_ts") or 0) >= cutoff]
    by_status: dict[str, int] = {}
    applied_delta = 0          # suma de cambios de presupuesto efectivamente aplicados
    for r in recent:
        st = r.get("status") or "?"
        by_status[st] = by_status.get(st, 0) + 1
        if st in ("approved", "auto_applied"):
            cur = r.get("current_budget") or 0
            prop = r.get("proposed_budget") or 0
            applied_delta += (prop - cur)

    pendientes = approvals.list_pending()

    # Snapshot de campañas (gasto/atribución) y de creatividades (recomendación).
    spend = n_camp = None
    try:
        from .world_state import load_snapshot as _ws_load
        ws = _ws_load() or {}
        spend = ws.get("total_spend")
        n_camp = len(ws.get("campaigns") or []) or ws.get("n_campaigns")
    except Exception:  # noqa: BLE001
        pass

    creative_rec = best_creative = None
    try:
        from . import creatives
        cs = creatives.load_snapshot() or {}
        recs = (cs.get("angles") or {}).get("recommendations") or []
        creative_rec = recs[0]["text"] if recs else None
        if cs.get("best"):
            b = cs["best"]
            best_creative = f"{b.get('ad_name') or b.get('campaign_name')} — ${(b.get('cost_per_message') or 0):,}/msg"
    except Exception:  # noqa: BLE001
        pass

    return {
        "generated_ts": int(time.time()),
        "days": days,
        "pendientes": len(pendientes),
        "por_estado": by_status,
        "auto_aplicadas": by_status.get("auto_applied", 0),
        "aprobadas": by_status.get("approved", 0),
        "rechazadas": by_status.get("rejected", 0),
        "delta_presupuesto_aplicado": applied_delta,
        "gasto_total": spend,
        "campanas": n_camp,
        "creativo_mejor": best_creative,
        "creativo_recomendacion": creative_rec,
    }


def render_text(d: dict) -> str:
    """Versión WhatsApp-friendly del digest."""
    L = [f"📊 *Autopilot — resumen {d['days']}d*", ""]
    L.append(f"• ⏳ Esperando tu OK: *{d['pendientes']}*")
    if d["auto_aplicadas"]:
        L.append(f"• 🤖 Aplicadas solas: {d['auto_aplicadas']}")
    if d["aprobadas"]:
        L.append(f"• ✅ Aprobadas por ti: {d['aprobadas']}")
    if d["rechazadas"]:
        L.append(f"• ❌ Rechazadas: {d['rechazadas']}")
    dlt = d.get("delta_presupuesto_aplicado") or 0
    if dlt:
        signo = "+" if dlt > 0 else ""
        L.append(f"• 💰 Cambio neto de presupuesto aplicado: {signo}${dlt:,}/día")
    if d.get("gasto_total"):
        L.append(f"• 📈 Gasto en ventana: ${d['gasto_total']:,.0f}")
    if d.get("creativo_recomendacion"):
        L += ["", f"🎨 {d['creativo_recomendacion']}"]
    return "\n".join(L)
