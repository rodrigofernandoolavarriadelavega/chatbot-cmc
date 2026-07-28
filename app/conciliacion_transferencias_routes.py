"""conciliacion_transferencias_routes.py — Panel + API de conciliación de
transferencias bancarias y sugerencias de pago para recepción.

Mismo patrón de auth que `roas_routes.py` / `agenda_ticker_routes.py` (token
admin/olacore o cookie de sesión). Dos superficies en una sola página:

  1. "Sugerencias de pago de hoy" — lo que `pagos_transferencia_sugeridos.py`
     detectó automáticamente (correo de banco de HOY + paciente con atención
     hoy sin cobrar). Recepción confirma o descarta con un clic. NUNCA se
     aplica solo.
  2. "Conciliación histórica" — el motor de `conciliacion_transferencias.py`
     por rango de fechas: qué calzó, qué se registró sin respaldo bancario,
     qué entró al banco sin quedar registrado (el número más importante) y
     qué quedó ambiguo.

No toca `app/pagos_routes.py` ni `templates/alma_pagos.html` (otra ventana
del dueño trabaja ahí) — la escritura a `pagos_cmc` para confirmar una
sugerencia vive en `pagos_transferencia_sugeridos.confirmar_sugerencia`,
como UPDATE directo a la tabla, sin pasar por ese módulo.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import Body, Cookie, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse

_TPL = Path(__file__).resolve().parent.parent / "templates" / "alma_conciliacion_transferencias.html"
_CLT = ZoneInfo("America/Santiago")
log = logging.getLogger("conciliacion_routes")


def _auth(token, cmc_session):
    from admin_routes import _verify_cookie, _is_admin_token
    if not ((token and _is_admin_token(token)) or (cmc_session and _verify_cookie(cmc_session))):
        raise HTTPException(403, "No autorizado")


def _quien(token, cmc_session) -> str:
    if token:
        return f"token:{token[-6:]}"
    return "recepcion"


def _requiere_flag():
    """Corta los caminos que SALEN a Gmail cuando la conciliación está apagada.

    El flag CONCILIACION_TRANSFERENCIAS_ACTIVE controla el cron, pero el backfill
    se dispara por HTTP: sin este guard, el bloque desplegado "inerte" igual podía
    ponerse a leer el buzón entero con un POST. Los endpoints de LECTURA no pasan
    por acá a propósito — solo consultan tablas locales y sirven para revisar lo
    ya conciliado aunque el sistema esté apagado.
    """
    from config import CONCILIACION_TRANSFERENCIAS_ACTIVE
    if not CONCILIACION_TRANSFERENCIAS_ACTIVE:
        raise HTTPException(
            503,
            "Conciliación de transferencias apagada "
            "(CONCILIACION_TRANSFERENCIAS_ACTIVE=false). No se abre el correo del centro."
        )


# Referencia viva de la task de backfill. Sin esto, asyncio.create_task devuelve
# una task que el recolector puede llevarse a media ejecución, y cualquier
# excepción muere en silencio ("Task exception was never retrieved").
_BACKFILL_TASK: "object | None" = None


def register_conciliacion_transferencias_routes(app):
    @app.get("/alma/conciliacion-transferencias", response_class=HTMLResponse, include_in_schema=False)
    def conciliacion_page(token: str | None = Query(None), cmc_session: str | None = Cookie(None)):
        _auth(token, cmc_session)
        if not _TPL.exists():
            return HTMLResponse("<h1>Falta templates/alma_conciliacion_transferencias.html</h1>", status_code=500)
        html = _TPL.read_text(encoding="utf-8").replace("__TOKEN__", token or "")
        return HTMLResponse(html, headers={"Cache-Control": "no-store, no-cache, must-revalidate"})

    # ── Conciliación por rango ──────────────────────────────────────────
    @app.get("/api/conciliacion-transferencias/data", tags=["conciliacion"], include_in_schema=False)
    def conciliacion_data(token: str | None = Query(None), cmc_session: str | None = Cookie(None),
                          desde: str | None = Query(None), hasta: str | None = Query(None)):
        _auth(token, cmc_session)
        import conciliacion_transferencias as ct
        hoy = datetime.now(_CLT).date()
        if not hasta:
            hasta = hoy.isoformat()
        if not desde:
            desde = (hoy - timedelta(days=30)).isoformat()
        try:
            datetime.strptime(desde, "%Y-%m-%d")
            datetime.strptime(hasta, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(400, "desde/hasta deben ser YYYY-MM-DD")
        try:
            resultado = ct.conciliar(desde, hasta)
        except Exception as e:
            raise HTTPException(500, f"error conciliando: {e}")
        return JSONResponse(resultado)

    @app.get("/api/conciliacion-transferencias/estado", tags=["conciliacion"], include_in_schema=False)
    def conciliacion_estado(token: str | None = Query(None), cmc_session: str | None = Cookie(None)):
        _auth(token, cmc_session)
        import conciliacion_transferencias as ct
        return JSONResponse(ct.estado_backfill())

    @app.post("/api/conciliacion-transferencias/backfill", tags=["conciliacion"], include_in_schema=False)
    async def conciliacion_backfill(token: str | None = Query(None), cmc_session: str | None = Cookie(None)):
        """Dispara el backfill histórico completo. Solo dueño/admin — recorre
        TODO el buzón (puede tomar varios minutos). Se ejecuta en background;
        seguir progreso con GET .../estado."""
        global _BACKFILL_TASK
        _auth(token, cmc_session)
        _requiere_flag()
        import asyncio
        import conciliacion_transferencias as ct

        # Un solo backfill a la vez. `backfill_transferencias_banco` marca
        # running/idle en system_state pero NO comprueba si ya hay uno en curso:
        # dos clics seguidos en el panel lanzaban dos barridos del buzón completo
        # compitiendo por la misma conexión IMAP y la misma tabla.
        if _BACKFILL_TASK is not None and not _BACKFILL_TASK.done():
            raise HTTPException(409, "Ya hay un backfill en curso. Sigue el avance en .../estado")
        if ct.estado_backfill().get("running"):
            raise HTTPException(409, "Hay un backfill marcado como en curso en otro proceso")

        _BACKFILL_TASK = asyncio.create_task(ct.backfill_transferencias_banco())

        def _al_terminar(t):
            exc = t.exception() if not t.cancelled() else None
            if exc:
                log.error("backfill de transferencias falló: %s", exc, exc_info=exc)
            else:
                log.info("backfill de transferencias terminado: %s", t.result())

        _BACKFILL_TASK.add_done_callback(_al_terminar)
        return JSONResponse({"ok": True, "mensaje": "backfill lanzado en segundo plano"})

    # ── Sugerencias de pago (hoy) ────────────────────────────────────────
    @app.get("/api/pagos-sugeridos", tags=["conciliacion"], include_in_schema=False)
    def pagos_sugeridos_listar(token: str | None = Query(None), cmc_session: str | None = Cookie(None)):
        _auth(token, cmc_session)
        import pagos_transferencia_sugeridos as pts
        return JSONResponse({"pendientes": pts.listar_pendientes()})

    @app.post("/api/pagos-sugeridos/{sugerencia_id}/confirmar", tags=["conciliacion"], include_in_schema=False)
    async def pagos_sugeridos_confirmar(sugerencia_id: int,
                                        token: str | None = Query(None), cmc_session: str | None = Cookie(None),
                                        body: dict = Body(...)):
        _auth(token, cmc_session)
        import pagos_transferencia_sugeridos as pts
        pago_cmc_id = body.get("pago_cmc_id")
        if not pago_cmc_id:
            raise HTTPException(400, "falta pago_cmc_id")
        r = pts.confirmar_sugerencia(sugerencia_id, int(pago_cmc_id), _quien(token, cmc_session))
        if not r["ok"]:
            raise HTTPException(409, r["error"])
        return JSONResponse(r)

    @app.post("/api/pagos-sugeridos/{sugerencia_id}/descartar", tags=["conciliacion"], include_in_schema=False)
    async def pagos_sugeridos_descartar(sugerencia_id: int,
                                        token: str | None = Query(None), cmc_session: str | None = Cookie(None)):
        _auth(token, cmc_session)
        import pagos_transferencia_sugeridos as pts
        r = pts.descartar_sugerencia(sugerencia_id, _quien(token, cmc_session))
        return JSONResponse(r)

    @app.get("/api/pagos-sugeridos/cobertura-historica", tags=["conciliacion"], include_in_schema=False)
    def pagos_sugeridos_cobertura(token: str | None = Query(None), cmc_session: str | None = Cookie(None),
                                  dias: int = Query(60, ge=1, le=365)):
        _auth(token, cmc_session)
        import pagos_transferencia_sugeridos as pts
        return JSONResponse(pts.medir_cobertura_historica(dias))
