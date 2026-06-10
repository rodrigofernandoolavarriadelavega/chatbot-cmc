"""Fase 0 — Estado unificado del mundo publicitario por campaña.

Combina dos fuentes de verdad:
  1. Meta Insights (señal primaria, rápida): spend, purchases, ROAS por campaña.
  2. bi.fact_pagos (auditor independiente, ground-truth): ingreso real cobrado.

El auditor existe para detectar cuando Meta sobre/sub-atribuye. Si Meta dice
ROAS 5x pero la caja real no se movió, el motor debe desconfiar de la señal Meta.
"""
import logging
from dataclasses import dataclass, field, asdict
from datetime import date, timedelta
from typing import Any

import httpx

from . import meta_ads

log = logging.getLogger("bot")


@dataclass
class CampaignState:
    """Foto de una campaña en una ventana temporal."""
    id: str
    name: str
    objective: str
    effective_status: str
    daily_budget_clp: int | None
    adsets: list[dict] = field(default_factory=list)
    # métricas Meta (insights normalizados):
    spend: float = 0.0
    leads: float = 0.0
    schedules: float = 0.0
    purchases: float = 0.0
    purchase_value: float = 0.0
    cac_purchase: float | None = None
    # Atribución LOCAL (source_id → pagó, sin depender del CAPI de Meta):
    atendidos_local: int = 0
    cac_real_local: float | None = None
    roas_meta: float = 0.0
    ctr: float = 0.0
    cpm: float = 0.0
    # Eventos de embudo temprano (medibles sin Purchase atribuido):
    messages: float = 0.0
    cost_per_message: float | None = None
    link_clicks: float = 0.0
    cost_per_link_click: float | None = None
    insights_raw: dict = field(default_factory=dict)


@dataclass
class WorldState:
    """Snapshot completo: todas las campañas + auditor BI + meta-datos de la corrida."""
    window_days: int
    date_from: str
    date_to: str
    campaigns: list[CampaignState] = field(default_factory=list)
    # auditor BI (ground-truth de toda la ventana, no por campaña):
    bi_revenue_real: float | None = None
    bi_pagos_count: int | None = None
    bi_available: bool = False
    # agregados derivados:
    total_spend: float = 0.0
    total_purchase_value_meta: float = 0.0
    attribution_ratio: float | None = None  # ingreso_real_BI / valor_atribuido_Meta
    # Capacidad de agenda por especialidad (gasto consciente de oferta):
    # {especialidad: {days_to_next, saturated, known}}.
    capacity: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


# ── Auditor BI ────────────────────────────────────────────────────────────────

def _bi_window_totals(date_from: str, date_to: str) -> tuple[float | None, int | None]:
    """Ingreso real cobrado y nº de pagos en la ventana, desde bi.fact_pagos.

    Devuelve (None, None) si BI no está configurado/accesible — el autopilot
    debe degradar con gracia y operar solo con la señal Meta, marcándolo.
    """
    # FIX 2026-06-09: importaba `_connect_bi` de bi_sync, función que ya no
    # existe — el except lo tragaba y el auditor BI llevaba semanas muerto en
    # silencio ("bi_sync no importable" en el log). Se usa winback.bi_conn(),
    # el pool canónico con rollback al devolver (root-fix 2026-05-29).
    try:
        from winback import bi_conn
    except ImportError:
        log.warning("autopilot: winback.bi_conn no importable; sin auditor BI")
        return None, None

    try:
        with bi_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COALESCE(SUM(monto), 0)::float, COUNT(*)::int
                    FROM bi.fact_pagos
                    WHERE fecha >= %s AND fecha <= %s
                    """,
                    (date_from, date_to),
                )
                monto, n = cur.fetchone()
                return float(monto or 0), int(n or 0)
    except Exception as e:  # noqa: BLE001 — degradar con gracia, no romper el bot
        log.warning("autopilot: auditor BI falló (%s); sigo solo con Meta", e)
        return None, None


# ── Construcción del estado ─────────────────────────────────────────────────

def _date_preset(window_days: int) -> str:
    return {7: "last_7d", 14: "last_14d", 30: "last_30d"}.get(window_days, "last_7d")


async def build_world_state(window_days: int = 7) -> WorldState:
    """Arma el snapshot completo. Read-only: no toca ninguna campaña."""
    today = date.today()
    date_to = today.isoformat()
    date_from = (today - timedelta(days=window_days)).isoformat()
    preset = _date_preset(window_days)

    ws = WorldState(window_days=window_days, date_from=date_from, date_to=date_to)

    async with httpx.AsyncClient() as client:
        # 1 sola llamada a nivel cuenta (evita N+1 → error 17 de Meta) y filtra
        # las publicaciones impulsadas sin gasto: solo quedan campañas con spend>0.
        try:
            by_campaign = await meta_ads.account_insights_by_campaign(client, date_preset=preset)
        except Exception as e:  # noqa: BLE001
            log.error("autopilot: account insights falló: %s", e)
            by_campaign = {}

        # Estructura/presupuesto de las campañas con gasto (segunda llamada, acotada).
        meta_map = {}
        try:
            for c in await meta_ads.list_campaigns(client, only_active=False):
                meta_map[c["id"]] = c
        except Exception as e:  # noqa: BLE001
            log.warning("autopilot: list_campaigns falló: %s", e)

        for cid, ins in by_campaign.items():
            c = meta_map.get(cid, {})
            cs = CampaignState(
                id=cid,
                name=ins.get("campaign_name") or c.get("name", ""),
                objective=c.get("objective", ""),
                effective_status=c.get("effective_status", ""),
                daily_budget_clp=int(c["daily_budget"]) if c.get("daily_budget") else None,
                adsets=[],
                spend=ins.get("spend", 0.0),
                leads=ins.get("leads", 0.0),
                schedules=ins.get("schedules", 0.0),
                purchases=ins.get("purchases", 0.0),
                purchase_value=ins.get("purchase_value", 0.0),
                cac_purchase=ins.get("cac_purchase"),
                roas_meta=ins.get("roas_meta", 0.0),
                ctr=ins.get("ctr", 0.0),
                cpm=ins.get("cpm", 0.0),
                messages=ins.get("messages", 0.0),
                cost_per_message=ins.get("cost_per_message"),
                link_clicks=ins.get("link_clicks", 0.0),
                cost_per_link_click=ins.get("cost_per_link_click"),
                insights_raw=ins,
            )
            ws.campaigns.append(cs)

    # Agregados Meta
    ws.total_spend = sum(c.spend for c in ws.campaigns)
    ws.total_purchase_value_meta = sum(c.purchase_value for c in ws.campaigns)

    # Auditor BI
    rev, n = _bi_window_totals(date_from, date_to)
    if rev is not None:
        ws.bi_available = True
        ws.bi_revenue_real = rev
        ws.bi_pagos_count = n
        # ratio < 1 → Meta atribuye MÁS ingreso del que la caja real registra (inflado)
        # ratio > 1 → hay ingreso real que Meta no captura (sub-atribución / otros canales)
        if ws.total_purchase_value_meta > 0:
            ws.attribution_ratio = rev / ws.total_purchase_value_meta

    log.info(
        "autopilot world_state: %d campañas activas · spend=$%.0f · purchases=%.0f · "
        "valor_meta=$%.0f · ingreso_BI=%s · ratio=%s",
        len(ws.campaigns), ws.total_spend,
        sum(c.purchases for c in ws.campaigns),
        ws.total_purchase_value_meta,
        f"${rev:.0f}" if rev is not None else "n/d",
        f"{ws.attribution_ratio:.2f}" if ws.attribution_ratio else "n/d",
    )

    # Capacidad de agenda SOLO para las especialidades que tienen campañas activas
    # (infiere del nombre). Fail-safe: si falla, capacity queda {} y no se gatea nada.
    try:
        from .policy import _infer_especialidad
        from . import capacity as _cap
        esps = {e for e in (_infer_especialidad(c.name) for c in ws.campaigns) if e}
        if esps:
            ws.capacity = await _cap.capacity_for(esps)
            _sat = [e for e, v in ws.capacity.items() if v.get("saturated")]
            if _sat:
                log.info("autopilot capacidad: saturadas → %s (no se escalan)", ", ".join(_sat))
    except Exception as e:  # noqa: BLE001
        log.warning("autopilot: capacidad no disponible (%s); sigo sin gating de agenda", e)

    # Atribución LOCAL real por campaña (source_id → pagó), independiente del CAPI de
    # Meta. Adjunta cac_real_local + atendidos_local a cada campaña (match por nombre).
    try:
        from . import cac_local
        _cl = await cac_local.cac_by_campaign(window_days=max(window_days, 30))
        _byc = _cl.get("by_campaign") or {}
        _matched = 0
        for c in ws.campaigns:
            info = _byc.get((c.name or "").strip())
            if info:
                c.atendidos_local = int(info.get("atendidos") or 0)
                c.cac_real_local = info.get("cac_real")
                _matched += 1
        if _matched:
            log.info("autopilot: CAC local real adjuntado a %d campañas (modo %s)",
                     _matched, _cl.get("modo_atendido"))
    except Exception as e:  # noqa: BLE001
        log.warning("autopilot: CAC local no disponible (%s); sigo con señal Meta", e)

    return ws


# ── Persistencia de snapshot (para que el dashboard no golpee Meta) ───────────

import json
import os

SNAPSHOT_PATH = os.getenv("AUTOPILOT_SNAPSHOT_PATH", "data/autopilot_snapshot.json")


def save_snapshot(payload: dict) -> None:
    """Guarda el último run (world_state + acciones) como JSON para el dashboard."""
    try:
        os.makedirs(os.path.dirname(SNAPSHOT_PATH) or ".", exist_ok=True)
        tmp = SNAPSHOT_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, default=str)
        os.replace(tmp, SNAPSHOT_PATH)  # escritura atómica
    except Exception as e:  # noqa: BLE001
        log.warning("autopilot: no pude guardar snapshot: %s", e)


def load_snapshot() -> dict | None:
    """Lee el último snapshot. None si no existe todavía."""
    try:
        with open(SNAPSHOT_PATH, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:  # noqa: BLE001
        log.warning("autopilot: no pude leer snapshot: %s", e)
        return None
