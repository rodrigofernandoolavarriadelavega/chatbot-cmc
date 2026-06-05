"""Cliente Marketing API (Graph v21.0) — lectura de performance y estructura.

Read-only por diseño. Las funciones de escritura (`set_adset_budget`,
`pause_adset`, ...) existen pero abortan salvo que `AUTOPILOT_EXECUTE=true`,
y aun así respetan los límites duros que les pasa el motor. En Fase 0/1
NUNCA se llaman.

Señal primaria del autopilot: como el CAPI Purchase ya envía `value` por
paciente, Meta calcula `purchase_roas` y `cost_per_action_type` por campaña
en su servidor. Aquí solo los leemos.
"""
import logging
import os
from .flags import flag_on
from typing import Any

import httpx

log = logging.getLogger("bot")

GRAPH = "https://graph.facebook.com/v21.0"

# Cuenta publicitaria CMC (la misma que usan meta-ads-builder / custom_audiences_sync).
from .tenants import active as _active_tenant
# La cuenta Meta sale del inquilino activo (CMC por defecto → mismo valor de antes).
AD_ACCOUNT = _active_tenant().ad_account or os.getenv("META_AD_ACCOUNT_ID", "act_220608142267129")


def _token() -> str:
    """Token de la Marketing API. Reusa el de config (mismo de CAPI/WhatsApp)."""
    try:
        from config import META_ACCESS_TOKEN
        if META_ACCESS_TOKEN:
            return META_ACCESS_TOKEN
    except ImportError:
        pass
    return os.getenv("META_ACCESS_TOKEN", "")


def _execute_enabled() -> bool:
    """Master kill-switch de escritura. False en Fase 0/1."""
    return flag_on("AUTOPILOT_EXECUTE")


# ── Lectura ──────────────────────────────────────────────────────────────────

async def _get(client: httpx.AsyncClient, path: str, params: dict) -> dict:
    params = {**params, "access_token": _token()}
    r = await client.get(f"{GRAPH}/{path}", params=params, timeout=30)
    data = r.json()
    if "error" in data:
        err = data["error"]
        log.warning("meta_ads GET %s → error %s: %s",
                    path, err.get("code"), err.get("message"))
        raise RuntimeError(f"Marketing API error {err.get('code')}: {err.get('message')}")
    return data


async def list_campaigns(client: httpx.AsyncClient, only_active: bool = True) -> list[dict]:
    """Campañas de la cuenta con su estado y presupuesto (si es CBO)."""
    params = {
        "fields": "id,name,status,effective_status,daily_budget,lifetime_budget,objective",
        "limit": 200,
    }
    data = await _get(client, f"{AD_ACCOUNT}/campaigns", params)
    camps = data.get("data", [])
    if only_active:
        camps = [c for c in camps if c.get("effective_status") == "ACTIVE"]
    return camps


async def account_insights_by_campaign(
    client: httpx.AsyncClient, date_preset: str = "last_7d"
) -> dict[str, dict]:
    """Insights de TODA la cuenta en UNA llamada, agrupados por campaña.

    Esto evita el problema N+1 (una llamada de insights por campaña) que dispara
    el error 17 'User request limit reached' de Meta. Además, al filtrar por
    spend>0, descartamos las decenas de publicaciones impulsadas históricas que
    siguen 'ACTIVE' pero no gastan — solo nos quedan las campañas que importan.

    Devuelve: {campaign_id: insights_normalizados}.
    """
    params = {
        "level": "campaign",
        "fields": "campaign_id,campaign_name,spend,impressions,clicks,ctr,cpm,"
                  "actions,action_values,cost_per_action_type,purchase_roas,"
                  "inline_link_clicks",
        "date_preset": date_preset,
        "limit": 500,
        # Solo campañas con gasto real en la ventana.
        "filtering": '[{"field":"spend","operator":"GREATER_THAN","value":0}]',
    }
    out: dict[str, dict] = {}
    next_url = f"{GRAPH}/{AD_ACCOUNT}/insights"
    page_params = {**params, "access_token": _token()}
    for _ in range(10):  # tope de páginas defensivo
        r = await client.get(next_url, params=page_params, timeout=45)
        data = r.json()
        if "error" in data:
            err = data["error"]
            log.warning("account_insights → error %s: %s", err.get("code"), err.get("message"))
            raise RuntimeError(f"Marketing API error {err.get('code')}: {err.get('message')}")
        for row in data.get("data", []):
            cid = row.get("campaign_id")
            if cid:
                norm = _normalize_insights(row)
                norm["campaign_name"] = row.get("campaign_name", "")
                out[cid] = norm
        nxt = (data.get("paging") or {}).get("next")
        if not nxt:
            break
        next_url, page_params = nxt, {}  # next ya trae todos los params + token
    return out


async def list_adsets(client: httpx.AsyncClient, campaign_id: str) -> list[dict]:
    """Ad sets de una campaña con presupuesto diario y targeting resumido."""
    params = {
        "fields": "id,name,status,effective_status,daily_budget,lifetime_budget,"
                  "optimization_goal,targeting{age_min,age_max,genders,geo_locations}",
        "limit": 200,
    }
    data = await _get(client, f"{campaign_id}/adsets", params)
    return data.get("data", [])


async def account_insights_by_ad(
    client: httpx.AsyncClient, date_preset: str = "last_30d"
) -> list[dict]:
    """Insights a nivel ANUNCIO (creatividad) de toda la cuenta, en pocas llamadas.

    Cierra el loop de medición de creatividades: mide qué anuncio individual
    convierte mejor, no solo qué campaña. Devuelve una lista de dicts con
    {ad_id, ad_name, campaign_name, + métricas normalizadas}. Filtra spend>0.
    """
    params = {
        "level": "ad",
        "fields": "ad_id,ad_name,campaign_name,adset_name,spend,impressions,clicks,"
                  "ctr,cpm,actions,action_values,cost_per_action_type,purchase_roas",
        "date_preset": date_preset,
        "limit": 500,
        "filtering": '[{"field":"spend","operator":"GREATER_THAN","value":0}]',
    }
    out: list[dict] = []
    next_url = f"{GRAPH}/{AD_ACCOUNT}/insights"
    page_params = {**params, "access_token": _token()}
    for _ in range(10):
        r = await client.get(next_url, params=page_params, timeout=45)
        data = r.json()
        if "error" in data:
            err = data["error"]
            log.warning("account_insights_by_ad → error %s: %s", err.get("code"), err.get("message"))
            raise RuntimeError(f"Marketing API error {err.get('code')}: {err.get('message')}")
        for row in data.get("data", []):
            aid = row.get("ad_id")
            if not aid:
                continue
            norm = _normalize_insights(row)
            norm["ad_id"] = aid
            norm["ad_name"] = row.get("ad_name", "")
            norm["campaign_name"] = row.get("campaign_name", "")
            norm["adset_name"] = row.get("adset_name", "")
            out.append(norm)
        nxt = (data.get("paging") or {}).get("next")
        if not nxt:
            break
        next_url, page_params = nxt, {}
    return out


async def insights(
    client: httpx.AsyncClient,
    object_id: str,
    date_preset: str = "last_7d",
    level: str = "campaign",
) -> dict | None:
    """Insights agregados de un objeto (campaign/adset/ad).

    Devuelve un dict normalizado con las métricas que el motor necesita.
    `purchase` y `purchase_value` vienen del CAPI Purchase (con value),
    así que `roas` aquí es ROAS real de pacientes atendidos según Meta.
    """
    params = {
        "fields": "spend,impressions,clicks,ctr,cpm,actions,action_values,"
                  "cost_per_action_type,purchase_roas",
        "date_preset": date_preset,
        "level": level,
    }
    data = await _get(client, f"{object_id}/insights", params)
    rows = data.get("data", [])
    if not rows:
        return None
    return _normalize_insights(rows[0])


def _normalize_insights(row: dict) -> dict:
    """Aplana la estructura `actions`/`action_values` de Meta a métricas planas."""
    def _action(arr: list[dict] | None, key: str) -> float:
        for a in (arr or []):
            if a.get("action_type") == key:
                try:
                    return float(a.get("value", 0))
                except (TypeError, ValueError):
                    return 0.0
        return 0.0

    def _action_family(arr: list[dict] | None, *keys: str) -> float:
        """Máximo entre variantes equivalentes de un mismo evento. Meta nombra la
        conversión de CAPI de varias formas según canal: `purchase`, `omni_purchase`,
        `offsite_conversion.fct.purchase`. Tomamos el MÁXIMO (no la suma) para no
        duplicar cuando Meta reporta la variante omni Y la de canal a la vez."""
        best = 0.0
        for a in (arr or []):
            at = a.get("action_type", "")
            if at in keys or any(at.endswith("." + k) for k in keys):
                try:
                    best = max(best, float(a.get("value", 0)))
                except (TypeError, ValueError):
                    pass
        return best

    actions = row.get("actions")
    values = row.get("action_values")
    cpa = row.get("cost_per_action_type")
    roas_arr = row.get("purchase_roas")

    spend = float(row.get("spend", 0) or 0)
    # En CMC los eventos relevantes son Lead (calificado), Schedule (cita) y Purchase (atendido).
    # Match por FAMILIA — Meta los reporta con prefijos distintos según canal
    # (onsite_conversion.lead, omni_purchase, offsite_conversion.fct.purchase, etc.).
    leads = _action_family(actions, "lead", "omni_lead")
    schedules = _action_family(actions, "schedule", "omni_schedule")
    purchases = _action_family(actions, "purchase", "omni_purchase")
    purchase_value = _action_family(values, "purchase", "omni_purchase")

    # Eventos de mensajería WhatsApp (Click-to-WhatsApp) y de tráfico. Meta los nombra
    # de varias formas según el tipo de campaña; sumamos las variantes conocidas.
    messages = (
        _action(actions, "onsite_conversion.messaging_conversation_started_7d")
        + _action(actions, "onsite_conversion.total_messaging_connection")
        + _action(actions, "messaging_conversation_started_7d")
    )
    link_clicks = _action(actions, "link_click")
    landing_views = _action(actions, "landing_page_view")
    post_engagement = _action(actions, "post_engagement")

    roas = 0.0
    for r in (roas_arr or []):
        if r.get("action_type") in (None, "purchase", "omni_purchase"):
            try:
                roas = float(r.get("value", 0))
            except (TypeError, ValueError):
                roas = 0.0
            break

    return {
        "spend": spend,
        "impressions": int(float(row.get("impressions", 0) or 0)),
        "clicks": int(float(row.get("clicks", 0) or 0)),
        "ctr": float(row.get("ctr", 0) or 0),
        "cpm": float(row.get("cpm", 0) or 0),
        "leads": leads,
        "schedules": schedules,
        "purchases": purchases,
        "purchase_value": purchase_value,
        "messages": messages,
        "link_clicks": link_clicks,
        "landing_views": landing_views,
        "post_engagement": post_engagement,
        # CAC en 3 capas (según Meta; world_state lo cruza con fact_pagos):
        "cac_lead": (spend / leads) if leads else None,
        "cac_schedule": (spend / schedules) if schedules else None,
        "cac_purchase": (spend / purchases) if purchases else None,
        # Costos de eventos de embudo más temprano (medibles aunque no haya Purchase):
        "cost_per_message": (spend / messages) if messages else None,
        "cost_per_link_click": (spend / link_clicks) if link_clicks else None,
        "roas_meta": roas,
        "_cpa_raw": cpa,  # se conserva para auditoría
    }


# ── Escritura (guardada — NO se usa en Fase 0/1) ──────────────────────────────

async def set_adset_budget(
    client: httpx.AsyncClient,
    adset_id: str,
    daily_budget_clp: int,
    *,
    reason: str = "",
) -> dict:
    """Cambia el presupuesto diario de un ad set. ABORTA si AUTOPILOT_EXECUTE != true.

    El motor SOLO debe llamar esto tras validar límites duros. Meta espera el
    presupuesto en la unidad menor de la moneda (CLP no tiene decimales → CLP entero).
    """
    if not _execute_enabled():
        log.info("[autopilot dry-run] set_adset_budget(%s → $%s) bloqueado (motivo: %s)",
                 adset_id, daily_budget_clp, reason)
        return {"dry_run": True, "adset_id": adset_id, "daily_budget": daily_budget_clp}

    params = {"daily_budget": int(daily_budget_clp), "access_token": _token()}
    r = await client.post(f"{GRAPH}/{adset_id}", data=params, timeout=30)
    out = r.json()
    log.info("[autopilot] set_adset_budget(%s → $%s) motivo=%s → %s",
             adset_id, daily_budget_clp, reason, out)
    return out


async def set_campaign_budget(
    client: httpx.AsyncClient,
    campaign_id: str,
    daily_budget_clp: int,
    *,
    reason: str = "",
) -> dict:
    """Cambia el presupuesto diario de una campaña con CBO. ABORTA si AUTOPILOT_EXECUTE != true.

    Solo aplica a campañas con Campaign Budget Optimization (presupuesto a nivel
    campaña). Para presupuesto a nivel ad set, usar set_adset_budget.
    """
    if not _execute_enabled():
        log.info("[autopilot dry-run] set_campaign_budget(%s → $%s) bloqueado (motivo: %s)",
                 campaign_id, daily_budget_clp, reason)
        return {"dry_run": True, "campaign_id": campaign_id, "daily_budget": daily_budget_clp}

    params = {"daily_budget": int(daily_budget_clp), "access_token": _token()}
    r = await client.post(f"{GRAPH}/{campaign_id}", data=params, timeout=30)
    out = r.json()
    log.info("[autopilot] set_campaign_budget(%s → $%s) motivo=%s → %s",
             campaign_id, daily_budget_clp, reason, out)
    return out


async def pause_adset(client: httpx.AsyncClient, adset_id: str, *, reason: str = "") -> dict:
    """Pausa un ad set. ABORTA si AUTOPILOT_EXECUTE != true."""
    if not _execute_enabled():
        log.info("[autopilot dry-run] pause_adset(%s) bloqueado (motivo: %s)", adset_id, reason)
        return {"dry_run": True, "adset_id": adset_id, "status": "PAUSED"}

    params = {"status": "PAUSED", "access_token": _token()}
    r = await client.post(f"{GRAPH}/{adset_id}", data=params, timeout=30)
    out = r.json()
    log.info("[autopilot] pause_adset(%s) motivo=%s → %s", adset_id, reason, out)
    return out
