"""Promo post-consent — riel: paciente acepta consent_marketing → (delay) → promo dental.

Cadena completa (idea del dueño 2026-06-10):
  1. Paciente agenda por recepción → barrido consent_agendados le manda el utility.
  2. Paciente responde "Sí, acepto" → queda accepted en bi.marketing_consent.
  3. ESTE módulo, al día siguiente (delay configurable), le manda la promo dental
     (flyer MARKETING aprobado) — SOLO si no es ya paciente dental.

Diferencias con el auto-trigger dental de flows.py (consent_dental_v1 → flyer
inmediato): acá el pool es el consent de marketing GENERAL, el envío es diferido
(no se siente trampa "acepto → ad instantáneo") y va segmentado (quien ya se
atiende en dental no recibe promo de limpieza: ya viene).

Guardrails:
  - Gated PROMO_POSTCONSENT_ACTIVE (default false) + override en vivo vía
    alma_switchboard (Sala de Máquinas / set_flag).
  - Solo entra al riel quien aceptó DESPUÉS de PROMO_POSTCONSENT_SINCE
    (los 200+ aceptados históricos NO se blastean).
  - 1 promo por teléfono y por template, para siempre (tabla promo_postconsent_sends).
  - Excluidos: opt-out marketing, pool dental (ya tienen su riel), pacientes
    con atención dental reciente en BI.
  - Cap por corrida (default 15) + 30s entre envíos + ventana L-V horario clínica.

P&L: report() cruza envíos contra messages (respondió), citas_bot (agendó) y
bi_pagos_caja (plata real post-envío), mismo criterio que el win-back.
"""
from __future__ import annotations

import asyncio
import logging
import os

from fastapi import APIRouter, Query, Request, HTTPException
from fastapi.responses import JSONResponse

log = logging.getLogger("bot")

router = APIRouter(tags=["promo-postconsent"])

# Especialidades dentales en bi.dim_especialidad (verificado 2026-06-10):
# 9=Odontología General, 19=Ortodoncista, 20=Implantología.
_DENTAL_ESP_IDS = (9, 19, 20)

_EVENT_ENVIADA = "promo_postconsent_enviada"


# ── gating ───────────────────────────────────────────────────────────────────

def _active() -> bool:
    """Flag efectivo: override del switchboard (Sala de Máquinas) > env > OFF."""
    env_val = os.getenv("PROMO_POSTCONSENT_ACTIVE", "false").lower() in ("true", "1", "yes")
    try:
        from alma_switchboard import effective
        return effective("PROMO_POSTCONSENT_ACTIVE", env_val)
    except Exception:
        return env_val


def _cfg():
    return {
        "cap": int(os.getenv("PROMO_POSTCONSENT_CAP", "15")),
        "delay_hours": int(os.getenv("PROMO_POSTCONSENT_DELAY_HOURS", "18")),
        # Solo aceptados desde esta fecha entran al riel (no blastear histórico).
        "since": os.getenv("PROMO_POSTCONSENT_SINCE", "2026-06-10"),
    }


# ── tracking (SQLite sessions.db) ────────────────────────────────────────────

def _ensure_table() -> None:
    from session import db
    with db() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS promo_postconsent_sends (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone TEXT NOT NULL,
                template TEXT NOT NULL,
                segment TEXT,
                sent_at TEXT DEFAULT (datetime('now')),
                UNIQUE(phone, template)
            )"""
        )
        conn.commit()


def _ya_enviados(template: str) -> set[str]:
    from session import db
    _ensure_table()
    with db() as conn:
        rows = conn.execute(
            "SELECT phone FROM promo_postconsent_sends WHERE template = ?", (template,)
        ).fetchall()
    return {r[0] for r in rows}


def _registrar_envio(phone: str, template: str, segment: str) -> None:
    from session import db
    _ensure_table()
    with db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO promo_postconsent_sends (phone, template, segment) VALUES (?, ?, ?)",
            (phone, template, segment),
        )
        conn.commit()


# ── candidatos ───────────────────────────────────────────────────────────────

def _candidatos_bi(cfg: dict) -> list[str]:
    """Teléfonos accepted en bi.marketing_consent que entran al riel, ya
    excluyendo (en un solo query, set-based): opt-outs, pool dental propio y
    pacientes con atención dental reciente (180d)."""
    from winback import bi_conn

    sql = """
        SELECT mc.phone
        FROM bi.marketing_consent mc
        WHERE mc.status = 'accepted'
          AND mc.response_at >= %s::timestamp
          AND mc.response_at <= NOW() - (%s || ' hours')::interval
          AND NOT EXISTS (
              SELECT 1 FROM bi.opt_outs_marketing oo
              WHERE RIGHT(regexp_replace(oo.phone, '[^0-9]', '', 'g'), 9)
                  = RIGHT(regexp_replace(mc.phone, '[^0-9]', '', 'g'), 9))
          AND NOT EXISTS (
              SELECT 1 FROM bi.dental_consent dc
              WHERE RIGHT(regexp_replace(dc.phone, '[^0-9]', '', 'g'), 9)
                  = RIGHT(regexp_replace(mc.phone, '[^0-9]', '', 'g'), 9))
          AND NOT EXISTS (
              SELECT 1
              FROM bi.fact_atenciones fa
              JOIN bi.dim_profesional pr ON pr.profesional_id = fa.profesional_id
              JOIN bi.dim_paciente dp ON dp.paciente_id = fa.paciente_id
              WHERE pr.especialidad_id = ANY(%s)
                AND fa.fecha >= CURRENT_DATE - 180
                AND RIGHT(regexp_replace(dp.telefono, '[^0-9]', '', 'g'), 9)
                  = RIGHT(regexp_replace(mc.phone, '[^0-9]', '', 'g'), 9))
        ORDER BY mc.response_at ASC
    """
    with bi_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (cfg["since"], str(cfg["delay_hours"]), list(_DENTAL_ESP_IDS)))
            return [r[0] for r in cur.fetchall() if r[0]]


# ── job principal ────────────────────────────────────────────────────────────

async def job_promo_postconsent(dry_run: bool = False) -> dict:
    """Corrida diaria (L-V mediodía). dry_run=True → solo lista candidatos."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    if not dry_run and not _active():
        log.debug("promo_postconsent: PROMO_POSTCONSENT_ACTIVE=false — skip")
        return {"status": "inactive"}

    now_cl = datetime.now(ZoneInfo("America/Santiago"))
    if not dry_run and (now_cl.weekday() >= 5 or not (9 <= now_cl.hour < 20)):
        return {"status": "fuera_horario"}

    cfg = _cfg()
    from config import DENTAL_PROMO_FLYER_TEMPLATE, DENTAL_PROMO_FLYER_IMG
    template = DENTAL_PROMO_FLYER_TEMPLATE

    try:
        from winback import is_template_approved, phone_in_opt_out
        from session import normalize_wa_id, log_message, log_event

        if not dry_run and not await is_template_approved(template):
            log.warning("promo_postconsent: template %s no APPROVED — skip", template)
            return {"status": "template_not_approved"}

        crudos = _candidatos_bi(cfg)
        enviados_antes = _ya_enviados(template)
        candidatos: list[str] = []
        seen: set[str] = set()
        for tel in crudos:
            teln = normalize_wa_id(tel)
            if not teln or len(teln) < 11 or teln in seen:
                continue
            if teln in enviados_antes:
                continue  # 1 promo por teléfono+template, para siempre
            if phone_in_opt_out(teln):
                continue  # cinturón extra al SQL
            seen.add(teln)
            candidatos.append(teln)
            if len(candidatos) >= cfg["cap"]:
                break

        if dry_run:
            return {
                "candidatos": len(candidatos),
                "muestra": [t[:5] + "***" + t[-2:] for t in candidatos[:25]],
                "pool_bruto_bi": len(crudos),
                "ya_enviados": len(enviados_antes),
                "template": template,
                "config": cfg,
                "active": _active(),
            }

        from messaging import send_whatsapp_template, render_template_body

        enviados = 0
        for teln in candidatos:
            try:
                await send_whatsapp_template(
                    teln, template, header_image_url=DENTAL_PROMO_FLYER_IMG,
                )
                # Copy real + URL para que el panel muestre la miniatura.
                log_message(teln, "out",
                            render_template_body(template) + "\n" + DENTAL_PROMO_FLYER_IMG,
                            "IDLE")
                log_event(teln, _EVENT_ENVIADA, {"template": template, "segment": "no_dental"})
                _registrar_envio(teln, template, "no_dental")
                enviados += 1
                log.info("promo_postconsent: enviado → ...%s (%d/%d)",
                         teln[-4:], enviados, len(candidatos))
            except Exception as e:
                log.error("promo_postconsent: error ...%s: %s", teln[-4:], e)
            await asyncio.sleep(30)

        log.info("job_promo_postconsent: enviados=%d de %d candidatos", enviados, len(candidatos))
        return {"enviados": enviados, "candidatos": len(candidatos)}
    except Exception as e:
        log.error("job_promo_postconsent falló: %s", e)
        return {"status": "error", "error": str(e)}


# ── P&L ──────────────────────────────────────────────────────────────────────

def report() -> dict:
    """Conversión + plata real del riel. Mismo criterio que win-back:
    el ingreso se mide contra bi_pagos_caja (caja real), no contra estimados.
    Robusto: nunca lanza."""
    out = {"enviados": 0, "respondieron": 0, "agendaron": 0,
           "ingreso_clp": 0, "pacientes_con_pago": 0, "ok": True}
    try:
        from session import db, normalize_wa_id as _norm
        _ensure_table()
        with db() as s:
            sends = s.execute(
                "SELECT phone, MIN(sent_at) AS sent_at FROM promo_postconsent_sends GROUP BY phone"
            ).fetchall()
        if not sends:
            return out
        sent_at_por_phone = {_norm(r[0]): r[1] for r in sends}
        out["enviados"] = len(sent_at_por_phone)

        with db() as s:
            for teln, sent_at in sent_at_por_phone.items():
                resp = s.execute(
                    "SELECT 1 FROM messages WHERE phone = ? AND direction = 'in' AND ts >= ? LIMIT 1",
                    (teln, sent_at)).fetchone()
                if resp:
                    out["respondieron"] += 1
                cita = s.execute(
                    "SELECT id_paciente_medilink FROM citas_bot WHERE phone = ? AND created_at >= ? LIMIT 1",
                    (teln, sent_at)).fetchone()
                if cita:
                    out["agendaron"] += 1
                    id_pac = cita[0]
                    if id_pac:
                        try:
                            pago = s.execute(
                                "SELECT COALESCE(SUM(monto), 0) FROM bi_pagos_caja "
                                "WHERE id_paciente = ? AND fecha >= date(?)",
                                (id_pac, sent_at)).fetchone()
                            if pago and pago[0]:
                                out["ingreso_clp"] += int(pago[0])
                                out["pacientes_con_pago"] += 1
                        except Exception as e_pg:
                            log.debug("promo_postconsent report pagos: %s", e_pg)
    except Exception as e:
        log.warning("promo_postconsent report: %s", e)
        out["ok"] = False
    return out


# ── endpoints (solo dueño / admin) ───────────────────────────────────────────

def _check_token(token: str | None, request: Request | None) -> None:
    from config import ADMIN_TOKEN, OLACORE_TOKEN
    eff = token or (request.cookies.get("admin_token") if request else None)
    if eff not in (t for t in (ADMIN_TOKEN, OLACORE_TOKEN) if t):
        raise HTTPException(status_code=403, detail="token inválido")


@router.get("/alma/api/promo-postconsent/dryrun")
async def api_dryrun(request: Request, token: str | None = Query(default=None)):
    """Preview: a quién le saldría la promo HOY, sin tocar a nadie."""
    _check_token(token, request)
    return JSONResponse(await job_promo_postconsent(dry_run=True))


@router.get("/alma/api/promo-postconsent/report")
async def api_report(request: Request, token: str | None = Query(default=None)):
    """P&L del riel: enviados / respondieron / agendaron / ingreso real."""
    _check_token(token, request)
    return JSONResponse(report())
