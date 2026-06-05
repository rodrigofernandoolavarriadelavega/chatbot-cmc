"""CAC real por anuncio — atribución LOCAL, sin depender del CAPI de Meta.

Idea (de Rodrigo): cada mensaje de un ad Click-to-WhatsApp llega con el `source_id`
(= ad_id de Meta) guardado en meta_referrals. Cruzando eso con quién agendó/pagó
(citas_bot / bi_pagos_caja) y con el gasto por anuncio (account_insights_by_ad),
calculamos el CAC REAL por anuncio acá, sin esperar a que Meta acredite el Purchase.

  CAC_real(ad) = spend(ad) / pacientes atendidos que vinieron de ese ad

Read-only. Degrada con gracia (si falta BI, usa citas_bot como proxy de "agendó").
"""
import logging
import time

log = logging.getLogger("bot")


def _attended_phones_window(cutoff_ts: int) -> dict:
    """{phone: {'booked': bool, 'rut': str}} para teléfonos que vinieron de un ad y
    agendaron (citas_bot) dentro de la ventana. Base local, sin Medilink."""
    from session import _conn
    out: dict = {}
    with _conn() as conn:
        # Pacientes con referral de ad (source_id) en la ventana.
        refs = conn.execute(
            "SELECT phone, source_id, ts FROM meta_referrals "
            "WHERE source_id IS NOT NULL AND source_id!='' AND ts>=?",
            (cutoff_ts,),
        ).fetchall()
        ref_by_phone: dict = {}
        for r in refs:
            # quedarse con el ad más reciente por teléfono
            ref_by_phone[r["phone"]] = r["source_id"]
        if not ref_by_phone:
            return {}
        # RUT por teléfono.
        profs = {row["phone"]: (row["rut"] or "") for row in conn.execute(
            "SELECT phone, rut FROM contact_profiles").fetchall()}
        # ¿Agendó? (citas_bot futuras/recientes)
        booked = {row["phone"] for row in conn.execute(
            "SELECT DISTINCT phone FROM citas_bot").fetchall()}
    for phone, ad in ref_by_phone.items():
        out[phone] = {"ad": ad, "rut": profs.get(phone, ""), "booked": phone in booked}
    return out


def _paid_ruts_window(cutoff_date: str) -> set:
    """RUTs con PAGO REAL (atendido de verdad) desde cutoff_date, del BI Postgres:
    bi.fact_pagos × bi.dim_paciente (paciente_id↔rut). Set vacío si el BI no responde
    → la capa que lo usa cae al proxy 'agendó' (citas_bot)."""
    try:
        from bi_helper import bi_query
        rows, status = bi_query(
            "SELECT DISTINCT dp.rut AS rut "
            "FROM bi.fact_pagos fp "
            "JOIN bi.dim_paciente dp ON dp.paciente_id = fp.paciente_id "
            "WHERE fp.fecha >= %s AND dp.rut IS NOT NULL",
            (cutoff_date,),
        )
        if status != "ok":
            return set()
        return {_norm_rut(r.get("rut")) for r in rows if r.get("rut")}
    except Exception:  # noqa: BLE001 — BI no disponible → proxy booked
        return set()


def _norm_rut(rut: str) -> str:
    return (rut or "").replace(".", "").replace("-", "").lower().strip()


async def cac_by_ad(window_days: int = 30) -> dict:
    """CAC real por anuncio. Cruza spend (Meta por ad) × pacientes de ese ad que
    agendaron/pagaron (local). Devuelve lista ordenada por CAC ascendente."""
    import httpx
    now = int(time.time())
    cutoff_ts = now - window_days * 86400
    cutoff_date = time.strftime("%Y-%m-%d", time.gmtime(cutoff_ts))

    # 1) Pacientes por ad (local).
    attended = _attended_phones_window(cutoff_ts)
    paid_ruts = _paid_ruts_window(cutoff_date)

    # 2) Spend por ad (Meta).
    preset = {7: "last_7d", 14: "last_14d", 30: "last_30d", 90: "last_90d"}.get(window_days, "last_30d")
    spend_by_ad: dict = {}
    try:
        from . import meta_ads
        async with httpx.AsyncClient() as client:
            for row in await meta_ads.account_insights_by_ad(client, date_preset=preset):
                spend_by_ad[row["ad_id"]] = {
                    "spend": row.get("spend") or 0,
                    "ad_name": row.get("ad_name") or "",
                    "campaign_name": row.get("campaign_name") or "",
                    "messages": int(row.get("messages") or 0),
                }
    except Exception as e:  # noqa: BLE001
        log.warning("cac_local: insights por ad falló: %s", e)

    # 3) Agregar por ad.
    agg: dict = {}
    for phone, info in attended.items():
        ad = info["ad"]
        a = agg.setdefault(ad, {"leads": 0, "booked": 0, "paid": 0})
        a["leads"] += 1
        if info["booked"]:
            a["booked"] += 1
        if paid_ruts and _norm_rut(info["rut"]) in paid_ruts:
            a["paid"] += 1

    out = []
    for ad, a in agg.items():
        s = spend_by_ad.get(ad, {})
        spend = s.get("spend") or 0
        # "atendido" = pagó si hay BI; si no, proxy = agendó.
        atendidos = a["paid"] if paid_ruts else a["booked"]
        out.append({
            "ad_id": ad,
            "ad_name": s.get("ad_name") or "(sin gasto en ventana)",
            "campaign_name": s.get("campaign_name") or "",
            "spend": round(spend),
            "leads_wa": a["leads"],          # llegaron por el ad y escribieron
            "agendaron": a["booked"],
            "pagaron": a["paid"],
            "atendidos": atendidos,
            "cac_real": round(spend / atendidos) if atendidos else None,
            "cac_por_lead": round(spend / a["leads"]) if (spend and a["leads"]) else None,
        })
    # CAC real ascendente (mejores primero); los sin atendidos al final.
    out.sort(key=lambda x: (x["cac_real"] is None, x["cac_real"] or 1e12))
    total_spend = sum(o["spend"] for o in out)
    total_atendidos = sum(o["atendidos"] for o in out)
    return {
        "generated_ts": now,
        "window_days": window_days,
        "modo_atendido": "pago_real_BI" if paid_ruts else "agendó (proxy local)",
        "n_ads": len(out),
        "total_spend": total_spend,
        "total_atendidos": total_atendidos,
        "cac_promedio": round(total_spend / total_atendidos) if total_atendidos else None,
        "ads": out,
    }
