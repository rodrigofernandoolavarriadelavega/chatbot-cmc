"""P&L unificado de los rieles de mensajería — fuente única para:
  - Sala de Máquinas (mini-línea de costo/beneficio bajo cada switch),
  - pestaña "Rieles" del Autopilot (vista profunda),
  - el Director (gabinete) para recomendar mantener/subir/apagar.

Criterio de plata: SIEMPRE caja real (bi_pagos_caja), nunca estimados —
mismo estándar que el win-back. Robusto: cada riel se calcula aislado;
si uno falla, los demás salen igual.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Query, Request, HTTPException
from fastapi.responses import JSONResponse

log = logging.getLogger("bot")

router = APIRouter(tags=["rieles-pnl"])

# Fecha de nacimiento de los rieles nuevos (no mirar histórico anterior).
SINCE = "2026-06-10"


def _consent_caliente() -> dict:
    """Pool de consent: enviados desde SINCE, aceptados/rechazados/pendientes."""
    from winback import bi_conn
    out = {"enviados": 0, "aceptados": 0, "rechazados": 0, "pendientes": 0,
           "tasa_aceptacion": None}
    with bi_conn() as c:
        with c.cursor() as cur:
            cur.execute(
                """SELECT status, COUNT(*) FROM bi.marketing_consent
                   WHERE consent_sent_at >= %s::timestamp GROUP BY status""",
                (SINCE,))
            for status, n in cur.fetchall():
                if status == "accepted":
                    out["aceptados"] = n
                elif status == "declined":
                    out["rechazados"] = n
                elif status == "pending":
                    out["pendientes"] = n
    out["enviados"] = out["aceptados"] + out["rechazados"] + out["pendientes"]
    respondieron = out["aceptados"] + out["rechazados"]
    if respondieron:
        out["tasa_aceptacion"] = round(100 * out["aceptados"] / respondieron)
    return out


def _promo() -> dict:
    from promo_postconsent import report
    return report()  # enviados / respondieron / agendaron / ingreso_clp / pacientes_con_pago


def _eco_prep() -> dict:
    from session import db
    out = {"enviados": 0, "examenes_futuros_cubiertos": 0}
    with db() as s:
        try:
            out["enviados"] = int(s.execute(
                "SELECT COUNT(*) FROM eco_prep_sends").fetchone()[0] or 0)
            out["examenes_futuros_cubiertos"] = int(s.execute(
                "SELECT COUNT(*) FROM eco_prep_sends WHERE fecha_cita >= date('now')"
            ).fetchone()[0] or 0)
        except Exception:
            pass  # tabla aún no creada (cero envíos) → ceros
    return out


def _operativa() -> dict:
    """Cupos liberados: ofertas por estado + ingreso real de las confirmadas
    (pagos en caja del paciente desde la fecha de SU cita rellenada)."""
    from session import db
    out = {"ofertas_enviadas": 0, "apartadas_recepcion": 0, "confirmadas": 0,
           "perdidas": 0, "ingreso_clp": 0}
    with db() as s:
        try:
            for estado, n in s.execute(
                    "SELECT estado, COUNT(*) FROM waitlist_offers GROUP BY estado"):
                if estado == "enviada":
                    out["ofertas_enviadas"] += n
                elif estado == "recepcion":
                    out["apartadas_recepcion"] = n
                elif estado == "confirmada":
                    out["confirmadas"] = n
                elif estado == "perdida":
                    out["perdidas"] = n
            out["ofertas_enviadas"] += (out["apartadas_recepcion"]
                                        + out["confirmadas"] + out["perdidas"])
            # Ingreso real: pagos del paciente confirmado desde la fecha de su cupo.
            rows = s.execute(
                "SELECT rut, fecha FROM waitlist_offers WHERE estado='confirmada'"
            ).fetchall()
            for rut, fecha in rows:
                if not rut:
                    continue
                try:
                    from caja_helper import pagos_por_rut_desde  # si existe helper
                    out["ingreso_clp"] += pagos_por_rut_desde(rut, fecha)
                except Exception:
                    break  # sin helper de rut→pagos: dejamos solo conteos
        except Exception as e:
            log.debug("rieles_pnl operativa: %s", e)
    return out


def pnl() -> dict:
    """Snapshot completo. Nunca lanza; cada riel aislado."""
    rieles = []
    for key, flag, label, fn, resumen_fn in [
        ("consent_caliente", "CONSENT_AGENDADOS_ACTIVE", "Consent en caliente",
         _consent_caliente,
         lambda s: f"{s['enviados']} enviados · {s['aceptados']} sí"
                   + (f" · {s['tasa_aceptacion']}% acepta" if s["tasa_aceptacion"] is not None else "")),
        ("promo_postconsent", "PROMO_POSTCONSENT_ACTIVE", "Promo post-atención",
         _promo,
         lambda s: f"{s['enviados']} enviadas · {s['agendaron']} agendaron · ${s['ingreso_clp']:,} caja".replace(",", ".")),
        ("eco_prep", "ECO_PREP_ACTIVE", "Preparación de ecografía",
         _eco_prep,
         lambda s: f"{s['enviados']} enviadas · {s['examenes_futuros_cubiertos']} exámenes por venir cubiertos"),
        ("operativa", "ALMA_OPERATIVA_ENABLED", "Relleno de cupos",
         _operativa,
         lambda s: f"{s['ofertas_enviadas']} ofertas · {s['confirmadas']} confirmadas · {s['apartadas_recepcion']} en recepción"),
    ]:
        item = {"key": key, "flag": flag, "label": label, "ok": True}
        try:
            stats = fn()
            item["stats"] = stats
            item["resumen"] = resumen_fn(stats)
        except Exception as e:
            log.warning("rieles_pnl %s: %s", key, e)
            item.update({"ok": False, "stats": {}, "resumen": "sin datos"})
        rieles.append(item)
    return {"since": SINCE, "rieles": rieles}


def _check_token(token: str | None, request: Request | None) -> None:
    from admin_routes import _is_admin_token
    eff = token or (request.cookies.get("admin_token") if request else None)
    if not eff or not _is_admin_token(eff):
        raise HTTPException(status_code=403, detail="token inválido")


@router.get("/alma/api/rieles/pnl")
async def api_rieles_pnl(request: Request, token: str | None = Query(default=None)):
    """P&L de todos los rieles — consumido por Sala de Máquinas, Autopilot y Director."""
    _check_token(token, request)
    return JSONResponse(pnl())
