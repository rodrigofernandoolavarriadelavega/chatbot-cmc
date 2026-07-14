"""agenda_ticker_routes.py — Panel/API del Monitor de Agendamientos en vivo.

Mismo patrón de auth que roas_routes.py / persistencia_routes.py (token
admin/olacore o cookie de sesión). Página standalone en /alma/agenda-en-vivo,
registrada como módulo del shell Alma (ver ALMA_MODULE_REGISTRY en config.py).

El feed que ve el navegador viene SIEMPRE de la tabla local `agenda_ticker`
(sessions.db) — nunca de Medilink en vivo. El único que habla con Medilink es
el job del scheduler (agenda_ticker.poll_agenda_ticker, cada ~45s) y el
barrido diario de atenciones sin cerrar. Así el auto-refresh del navegador
(cada pocos segundos) no agrega presión sobre el rate limit de Medilink.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import Query, Cookie, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

_TPL = Path(__file__).resolve().parent.parent / "templates" / "alma_agenda_ticker.html"


def _auth(token, cmc_session):
    from admin_routes import _verify_cookie, _is_admin_token
    if not ((token and _is_admin_token(token)) or (cmc_session and _verify_cookie(cmc_session))):
        raise HTTPException(403, "No autorizado")


def _auth_dueno(token, cmc_session):
    """Gate más estricto para el disparo manual del barrido de 'sin cerrar'
    (varias decenas de requests a Medilink) — solo el token del dueño, no
    cualquier token admin de recepción."""
    from admin_routes import _verify_cookie
    from config import OLACORE_TOKEN
    if token and OLACORE_TOKEN and token == OLACORE_TOKEN:
        return
    if cmc_session and _verify_cookie(cmc_session) == "admin":
        # cookie de sesión no distingue dueño/recepción hoy — se deja pasar
        # igual que el resto del panel admin (mismo nivel que _auth).
        return
    raise HTTPException(403, "No autorizado")


def register_agenda_ticker_routes(app):
    @app.get("/alma/agenda-en-vivo", response_class=HTMLResponse, include_in_schema=False)
    def agenda_ticker_page(token: str | None = Query(None), cmc_session: str | None = Cookie(None)):
        _auth(token, cmc_session)
        if not _TPL.exists():
            return HTMLResponse("<h1>Falta templates/alma_agenda_ticker.html</h1>", status_code=500)
        html = _TPL.read_text(encoding="utf-8").replace("__TOKEN__", token or "")
        return HTMLResponse(html, headers={"Cache-Control": "no-store, no-cache, must-revalidate"})

    @app.get("/api/agenda-ticker/feed", tags=["agenda-ticker"], include_in_schema=False)
    def agenda_ticker_feed(token: str | None = Query(None), cmc_session: str | None = Cookie(None),
                           limit: int = Query(60, ge=1, le=200)):
        _auth(token, cmc_session)
        from agenda_ticker import get_feed, get_contadores_hoy, get_aviso_sin_cerrar
        return JSONResponse({
            "feed": get_feed(limit),
            "contadores_hoy": get_contadores_hoy(),
            "aviso_sin_cerrar": get_aviso_sin_cerrar(),
        })

    @app.post("/api/agenda-ticker/sync", tags=["agenda-ticker"], include_in_schema=False)
    async def agenda_ticker_sync_manual(token: str | None = Query(None), cmc_session: str | None = Cookie(None)):
        """Corre una pasada del poll a demanda (útil para probar sin esperar
        el ciclo del scheduler). Sigue siendo UNA sola consulta base."""
        _auth(token, cmc_session)
        from agenda_ticker import poll_agenda_ticker
        return await poll_agenda_ticker()

    @app.post("/api/agenda-ticker/sync-sin-cerrar", tags=["agenda-ticker"], include_in_schema=False)
    async def agenda_ticker_sync_sin_cerrar_manual(token: str | None = Query(None), cmc_session: str | None = Cookie(None),
                                                    dias: int = Query(30, ge=1, le=90)):
        """Dispara el barrido de atenciones sin cerrar a demanda. Gateado a
        dueño — son varias decenas de requests a Medilink en una sola
        llamada (paginado, con pausa entre páginas)."""
        _auth_dueno(token, cmc_session)
        from agenda_ticker import sync_atenciones_sin_cerrar
        return await sync_atenciones_sin_cerrar(dias=dias)
