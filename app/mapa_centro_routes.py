"""mapa_centro_routes.py — Panel "Mapa del Centro" (/alma/mapa).

Mismo patrón de auth que el resto de los paneles de Alma (token admin o cookie
de sesión). Solo lectura: no escribe nada, no toca Medilink, no llama a la IA.
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import Cookie, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse

_TPL = Path(__file__).resolve().parent.parent / "templates" / "alma_mapa_centro.html"
log = logging.getLogger("mapa_centro_routes")


def _auth(token, cmc_session):
    from admin_routes import _is_admin_token, _verify_cookie
    if not ((token and _is_admin_token(token)) or (cmc_session and _verify_cookie(cmc_session))):
        raise HTTPException(403, "No autorizado")


def register_mapa_centro_routes(app):
    @app.get("/alma/mapa", response_class=HTMLResponse, include_in_schema=False)
    def mapa_page(token: str | None = Query(None), cmc_session: str | None = Cookie(None)):
        _auth(token, cmc_session)
        if not _TPL.exists():
            return HTMLResponse("<h1>Falta templates/alma_mapa_centro.html</h1>", status_code=500)
        html = _TPL.read_text(encoding="utf-8").replace("__TOKEN__", token or "")
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    @app.get("/api/mapa-centro", include_in_schema=False)
    def mapa_data(token: str | None = Query(None), cmc_session: str | None = Cookie(None)):
        _auth(token, cmc_session)
        import mapa_centro
        return JSONResponse(mapa_centro.estado())
