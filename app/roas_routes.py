"""ROAS por campaña Meta Ads × ingreso real (bi_pagos_caja).

Fuente única de verdad para el ROAS del CMC: el MISMO `compute_roas()` alimenta
el dashboard `/alma/roas`, el snapshot diario del cron y la alerta. Así el número
del panel, el del mail y el del WhatsApp nunca divergen.

Criterio de plata: SIEMPRE caja real (`bi_pagos_caja`), nunca `bi_atenciones`
(que incluye deuda no cobrada). Cobertura ~60-70%: solo captura pacientes que
agendaron por el bot Y tienen id_paciente_medilink — el ROAS es PISO, no techo.

Atribución (3 capas):
  meta_referrals.source_id (= ad_id) → phone → citas_bot.id_paciente_medilink
  → bi_pagos_caja.id_paciente (= ingreso real).

Auth: token admin/olacore o cookie cmc_session.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from fastapi import Query, Cookie, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

import config

log = logging.getLogger("roas")
_CLT = ZoneInfo("America/Santiago")
_TPL = Path(__file__).parent.parent / "templates" / "alma_roas.html"

# ── Umbrales de semáforo (decisión del dueño 2026-06-26) ──────────────────
ROAS_ROJO = float(os.getenv("ROAS_ROJO", "1.0"))   # < 1 → pierde plata (rojo)
ROAS_VERDE = float(os.getenv("ROAS_VERDE", "3.0"))  # > 3 → escalar (verde)

# ── Mapa ad_id → campaign_id (al 2026-06-26). Si se crean campañas nuevas,
#    actualizar acá o resolver dinámicamente vía /campaign/ads. ─────────────
AD_TO_CAMPAIGN = {
    "120237287467320245": "120237287464660245",  # ECO
    "120238959671070245": "120238959665850245",  # MG-B
    "120247321009090245": "120247320993400245",  # SaludMental
    "120245734215650245": "120245734045940245",  # ORTO-WA
    "120242338785460245": "120242338784340245",  # MG-A
    "120242288313920245": "120242288312510245",  # MG-C
}

# ── Etiqueta legible por campaign_id (lo que ve el dueño en el panel) ──────
CAMPAIGN_LABEL = {
    "120242338784340245": ("MG-A", "Problemas de salud · variante A"),
    "120238959665850245": ("MG-B", "Problemas de salud · variante B"),
    "120242288312510245": ("MG-C", "Problemas de salud · variante C"),
    "120237287464660245": ("ECO", "Ecotomografía"),
    "120247320993400245": ("SaludMental", "Tu salud mental también merece atención"),
    "120245734045940245": ("ORTO-WA", "Orto Battle · WA Edition"),
}


# ═══════════════════════════ AUTH ════════════════════════════════════════
def _auth(token, cmc_session):
    from admin_routes import _verify_cookie, _is_admin_token
    if (token and _is_admin_token(token)) or (cmc_session and _verify_cookie(cmc_session)):
        return
    raise HTTPException(403, "No autorizado")


# ═══════════════════════════ META SPEND ══════════════════════════════════
async def _meta_campaign_spend(since: str, until: str) -> dict[str, dict]:
    """Spend por campaign_id en [since, until] (YYYY-MM-DD). Solo spend > 0."""
    acct = getattr(config, "META_AD_ACCOUNT_ID", "") or "act_220608142267129"
    if not acct.startswith("act_"):
        acct = f"act_{acct}"
    token = config.META_ACCESS_TOKEN
    out: dict[str, dict] = {}
    if not token:
        log.warning("ROAS: META_ACCESS_TOKEN vacío — spend = 0")
        return out
    params = {
        "fields": "campaign_id,campaign_name,spend,actions",
        "level": "campaign",
        "time_range": json.dumps({"since": since, "until": until}),
        "limit": 100,
        "access_token": token,
    }
    url = f"https://graph.facebook.com/v21.0/{acct}/insights"
    try:
        async with httpx.AsyncClient(timeout=20) as cli:
            r = await cli.get(url, params=params)
        data = r.json()
    except Exception as e:  # noqa: BLE001
        log.error("ROAS: fallo Meta insights: %s", e)
        return out
    if isinstance(data, dict) and data.get("error"):
        log.error("ROAS: Meta API error: %s", data["error"])
        return out
    for c in data.get("data", []):
        spend = float(c.get("spend", 0) or 0)
        if spend <= 0:
            continue  # nunca confiar en effective_status — filtrar por spend real
        convs = 0
        for a in (c.get("actions") or []):
            if a.get("action_type") in (
                "onsite_conversion.messaging_conversation_started_7d",
                "messaging_conversation_started",
            ):
                convs = max(convs, int(float(a.get("value", 0))))
        out[c["campaign_id"]] = {
            "spend": spend,
            "name": c.get("campaign_name", ""),
            "convs": convs,
        }
    return out


# ═══════════════════════════ ATRIBUCIÓN ══════════════════════════════════
def _attribution(ts_inicio: int, fecha_inicio: str, ad_ids: list[str]) -> dict[str, dict]:
    """ad_id → {phones, pacientes(set medilink), ingreso, atendidos}.

    Agregación en Python para evitar el fan-out del triple JOIN (un teléfono con
    varias citas/pagos duplicaría el ingreso). Tres queries simples:
      1) referrals en período → ad_id → set(phones)
      2) phone → id_paciente_medilink (citas_bot, id>0)
      3) id_paciente → SUM(monto) (bi_pagos_caja, fecha>=inicio)
    """
    from session import db

    res: dict[str, dict] = {aid: {"phones": set(), "pac": set()} for aid in ad_ids}
    if not ad_ids:
        return res
    ph = ",".join("?" for _ in ad_ids)

    with db() as c:
        # 1) referrals → ad_id → phones
        rows = c.execute(
            f"SELECT source_id, phone FROM meta_referrals "
            f"WHERE ts >= ? AND source_id IN ({ph})",
            (ts_inicio, *ad_ids),
        ).fetchall()
        all_phones: set[str] = set()
        for source_id, phone in rows:
            if source_id in res:
                res[source_id]["phones"].add(phone)
                all_phones.add(phone)

        # 2) phone → id_paciente_medilink (solo válidos)
        phone_to_pac: dict[str, int] = {}
        if all_phones:
            pph = ",".join("?" for _ in all_phones)
            crows = c.execute(
                f"SELECT phone, id_paciente_medilink FROM citas_bot "
                f"WHERE id_paciente_medilink > 0 AND phone IN ({pph})",
                tuple(all_phones),
            ).fetchall()
            for phone, pac in crows:
                phone_to_pac[phone] = int(pac)

        # 3) id_paciente → ingreso real (caja)
        pac_ids = set(phone_to_pac.values())
        pago_por_pac: dict[int, int] = {}
        if pac_ids:
            pph = ",".join("?" for _ in pac_ids)
            prows = c.execute(
                f"SELECT id_paciente, COALESCE(SUM(monto),0) FROM bi_pagos_caja "
                f"WHERE fecha >= ? AND id_paciente IN ({pph}) "
                f"GROUP BY id_paciente",
                (fecha_inicio, *pac_ids),
            ).fetchall()
            for pid, monto in prows:
                pago_por_pac[int(pid)] = int(monto or 0)

    # ── armar por ad_id: pacientes atribuidos, ingreso, atendidos (con pago) ──
    for aid, d in res.items():
        pacientes = {phone_to_pac[p] for p in d["phones"] if p in phone_to_pac}
        d["pac"] = pacientes
        d["ingreso"] = sum(pago_por_pac.get(pid, 0) for pid in pacientes)
        d["atendidos"] = sum(1 for pid in pacientes if pago_por_pac.get(pid, 0) > 0)
        d["phones_n"] = len(d["phones"])
        d["citas_n"] = len(pacientes)
    return res


# ═══════════════════════════ CÁLCULO ROAS ════════════════════════════════
def _roas(spend: float, ingreso: float):
    return round(ingreso / spend, 2) if spend > 0 else None


def _cac(spend: float, atendidos: int):
    return round(spend / atendidos) if atendidos > 0 else None


def _semaforo(roas, atendidos):
    if roas is None:
        return "pausada"
    if roas < ROAS_ROJO:
        return "rojo"
    if roas > ROAS_VERDE:
        return "verde"
    return "neutro"


async def compute_roas(dias: int = 30) -> dict:
    """Fuente única: arma el ROAS por campaña + totales + alertas."""
    hoy = datetime.now(_CLT).date()
    inicio = hoy - timedelta(days=dias)
    fecha_inicio = inicio.isoformat()
    until = hoy.isoformat()
    ts_inicio = int(datetime(inicio.year, inicio.month, inicio.day, tzinfo=_CLT).timestamp())

    spend_map = await _meta_campaign_spend(fecha_inicio, until)
    attr = _attribution(ts_inicio, fecha_inicio, list(AD_TO_CAMPAIGN.keys()))

    # ── agregar ad → campaña ──
    by_camp: dict[str, dict] = {}
    for aid, d in attr.items():
        camp = AD_TO_CAMPAIGN.get(aid)
        if not camp:
            continue
        b = by_camp.setdefault(camp, {"phones": 0, "citas": 0, "ingreso": 0, "atendidos": 0})
        b["phones"] += d.get("phones_n", 0)
        b["citas"] += d.get("citas_n", 0)
        b["ingreso"] += d.get("ingreso", 0)
        b["atendidos"] += d.get("atendidos", 0)

    campañas, alertas = [], []
    tot_spend = tot_ingreso = tot_atendidos = 0
    # incluir TODAS las campañas conocidas (aunque spend=0 → pausada)
    for camp in CAMPAIGN_LABEL:
        sp = spend_map.get(camp, {})
        spend = float(sp.get("spend", 0))
        b = by_camp.get(camp, {"phones": 0, "citas": 0, "ingreso": 0, "atendidos": 0})
        roas = _roas(spend, b["ingreso"])
        cac = _cac(spend, b["atendidos"])
        estado = _semaforo(roas, b["atendidos"])
        # citas agendadas sin pago aún → pendiente (no es ROAS=0)
        pendiente = b["citas"] > 0 and b["atendidos"] == 0
        label, sub = CAMPAIGN_LABEL[camp]
        fila = {
            "campaign_id": camp,
            "label": label,
            "sub": sub,
            "spend": round(spend),
            "phones": b["phones"],
            "citas": b["citas"],
            "atendidos": b["atendidos"],
            "ingreso": b["ingreso"],
            "roas": roas,
            "cac": cac,
            "estado": estado,
            "pendiente": pendiente,
        }
        campañas.append(fila)
        tot_spend += spend
        tot_ingreso += b["ingreso"]
        tot_atendidos += b["atendidos"]
        if roas is not None and roas < ROAS_ROJO:
            alertas.append({"label": label, "roas": roas, "spend": round(spend),
                            "ingreso": b["ingreso"]})

    # ordenar: rojas primero (peor ROAS), luego por ROAS desc, pausadas al final
    def _key(f):
        r = f["roas"]
        return (0 if r is None else 1, r if r is not None else 0)
    campañas.sort(key=_key)
    campañas.sort(key=lambda f: (f["roas"] is None, -(f["roas"] or 0)))

    return {
        "generado": datetime.now(_CLT).isoformat(timespec="seconds"),
        "periodo": {"desde": fecha_inicio, "hasta": until, "dias": dias},
        "campanas": campañas,
        "totales": {
            "spend": round(tot_spend),
            "ingreso": tot_ingreso,
            "atendidos": tot_atendidos,
            "roas": _roas(tot_spend, tot_ingreso),
            "cac": _cac(tot_spend, tot_atendidos),
        },
        "alertas": alertas,
        "umbrales": {"rojo": ROAS_ROJO, "verde": ROAS_VERDE},
        "cobertura_nota": "Ingreso vía caja real (bi_pagos_caja). Captura solo "
                          "pacientes agendados por el bot con id Medilink (~60-70%). "
                          "El ROAS es PISO, no techo.",
    }


# ═══════════════════════════ SNAPSHOTS (historial) ═══════════════════════
def _ensure_snapshot_table(c):
    c.execute("""
        CREATE TABLE IF NOT EXISTS roas_snapshots (
            fecha    TEXT PRIMARY KEY,   -- YYYY-MM-DD (1 por día)
            payload  TEXT NOT NULL,      -- JSON completo de compute_roas
            spend    INTEGER,
            ingreso  INTEGER,
            roas     REAL,
            n_rojas  INTEGER,
            created_at TEXT
        )
    """)


def _save_snapshot(payload: dict):
    from session import db
    hoy = datetime.now(_CLT).date().isoformat()
    t = payload.get("totales", {})
    with db() as c:
        _ensure_snapshot_table(c)
        c.execute(
            "INSERT INTO roas_snapshots (fecha, payload, spend, ingreso, roas, n_rojas, created_at) "
            "VALUES (?,?,?,?,?,?, datetime('now')) "
            "ON CONFLICT(fecha) DO UPDATE SET payload=excluded.payload, spend=excluded.spend, "
            "ingreso=excluded.ingreso, roas=excluded.roas, n_rojas=excluded.n_rojas, "
            "created_at=excluded.created_at",
            (hoy, json.dumps(payload), t.get("spend"), t.get("ingreso"),
             t.get("roas"), len(payload.get("alertas", []))),
        )


def _history(limit: int = 60) -> list[dict]:
    from session import db
    out = []
    with db() as c:
        _ensure_snapshot_table(c)
        for fecha, spend, ingreso, roas, n_rojas in c.execute(
            "SELECT fecha, spend, ingreso, roas, n_rojas FROM roas_snapshots "
            "ORDER BY fecha DESC LIMIT ?", (limit,)
        ).fetchall():
            out.append({"fecha": fecha, "spend": spend, "ingreso": ingreso,
                        "roas": roas, "n_rojas": n_rojas})
    out.reverse()
    return out


# ═══════════════════════════ CRON DIARIO + ALERTAS ═══════════════════════
async def roas_daily_job():
    """Recalcula ROAS 30d, persiste snapshot y alerta si hay campañas en rojo.

    Alertas (las 3, decisión 2026-06-26):
      · WhatsApp al dueño — SOLO si la ventana de 24h está abierta (send_whatsapp
        entrega solo dentro de la ventana; fuera, Meta lo descarta → no spamea).
      · Email diario — si Resend/SMTP está configurado.
      · Dashboard — el banner rojo lo arma el front al leer payload["alertas"].
    """
    try:
        payload = await compute_roas(30)
        _save_snapshot(payload)
    except Exception as e:  # noqa: BLE001
        log.error("ROAS cron: fallo compute/snapshot: %s", e)
        return
    rojas = payload.get("alertas", [])
    if not rojas:
        log.info("ROAS cron: sin campañas en rojo, no se alerta")
        return

    lineas = [f"⚠️ ROAS CMC — {len(rojas)} campaña(s) perdiendo plata (últimos 30d):", ""]
    for a in rojas:
        lineas.append(f"• {a['label']}: ROAS {a['roas']} — gasto ${a['spend']:,} / "
                      f"ingreso ${a['ingreso']:,}".replace(",", "."))
    t = payload["totales"]
    lineas += ["", f"Global: ROAS {t['roas']} · gasto ${t['spend']:,} · "
               f"ingreso ${t['ingreso']:,}".replace(",", ".")]
    lineas.append("Panel: agentecmc.cl/alma/roas")
    texto = "\n".join(lineas)

    # ── WhatsApp (solo ventana abierta) ──
    wa = os.getenv("ROAS_ALERT_WA", "")
    if wa:
        try:
            from messaging import send_whatsapp
            await send_whatsapp(wa, texto)
            log.info("ROAS cron: WhatsApp enviado a %s (si ventana abierta)", wa[-4:])
        except Exception as e:  # noqa: BLE001
            log.warning("ROAS cron: WhatsApp falló (ventana cerrada?): %s", e)

    # ── Email diario ──
    em = os.getenv("ROAS_ALERT_EMAIL", "")
    if em:
        try:
            from autopilot.email_render import send_email, sending_status
            if sending_status().get("enabled"):
                html = "<h2>ROAS CMC — campañas en rojo</h2><pre style='font:14px monospace'>" \
                       + texto.replace("<", "&lt;") + "</pre>"
                await send_email(em, f"⚠️ ROAS CMC: {len(rojas)} campaña(s) en rojo", html)
                log.info("ROAS cron: email enviado a %s", em)
        except Exception as e:  # noqa: BLE001
            log.warning("ROAS cron: email falló: %s", e)


# ═══════════════════════════ RUTAS ═══════════════════════════════════════
def register_roas_routes(app):
    @app.get("/alma/roas", response_class=HTMLResponse, include_in_schema=False)
    def roas_page(token: str | None = Query(None), cmc_session: str | None = Cookie(None)):
        _auth(token, cmc_session)
        if not _TPL.exists():
            return HTMLResponse("<h1>Falta templates/alma_roas.html</h1>", status_code=500)
        html = _TPL.read_text(encoding="utf-8").replace("__TOKEN__", token or "")
        return HTMLResponse(html)

    @app.get("/alma/api/roas", tags=["roas"], include_in_schema=False)
    async def roas_data(token: str | None = Query(None), cmc_session: str | None = Cookie(None),
                        dias: int = Query(30, ge=1, le=120)):
        _auth(token, cmc_session)
        return JSONResponse(await compute_roas(dias))

    @app.get("/alma/api/roas/history", tags=["roas"], include_in_schema=False)
    def roas_history(token: str | None = Query(None), cmc_session: str | None = Cookie(None),
                     limit: int = Query(60, ge=1, le=365)):
        _auth(token, cmc_session)
        return JSONResponse({"serie": _history(limit)})
