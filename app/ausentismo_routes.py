"""ausentismo_routes.py — Página/API del módulo Ausentismo (shell Alma).

Mismo patrón de auth que agenda_ticker_routes.py (token admin/olacore o cookie
de sesión). La página y la API leen SIEMPRE la tabla local `ausentismo_citas`
— el único que habla con Medilink es el job nocturno (04:50 CLT) o el barrido
manual, que queda gateado a dueño porque son cientos de requests paginados.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import Query, Cookie, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

_TPL = Path(__file__).resolve().parent.parent / "templates" / "alma_ausentismo.html"


def _auth(token, cmc_session):
    from admin_routes import _verify_cookie, _is_admin_token
    if not ((token and _is_admin_token(token)) or (cmc_session and _verify_cookie(cmc_session))):
        raise HTTPException(403, "No autorizado")


def _auth_dueno(token, cmc_session):
    """El barrido manual pagina cientos de requests a Medilink — solo dueño."""
    from admin_routes import _verify_cookie
    from config import OLACORE_TOKEN
    if token and OLACORE_TOKEN and token == OLACORE_TOKEN:
        return
    if cmc_session and _verify_cookie(cmc_session) == "admin":
        return
    raise HTTPException(403, "No autorizado")


def register_ausentismo_routes(app):
    from ausentismo import ensure_ausentismo_table
    ensure_ausentismo_table()

    @app.get("/alma/ausentismo", response_class=HTMLResponse, include_in_schema=False)
    def ausentismo_page(token: str | None = Query(None), cmc_session: str | None = Cookie(None)):
        _auth(token, cmc_session)
        if not _TPL.exists():
            return HTMLResponse("<h1>Falta templates/alma_ausentismo.html</h1>", status_code=500)
        html = _TPL.read_text(encoding="utf-8").replace("__TOKEN__", token or "")
        return HTMLResponse(html, headers={"Cache-Control": "no-store, no-cache, must-revalidate"})

    @app.get("/api/ausentismo/ranking", tags=["ausentismo"], include_in_schema=False)
    def ausentismo_ranking(token: str | None = Query(None), cmc_session: str | None = Cookie(None),
                           dias: int = Query(180, ge=7, le=365),
                           prof: int | None = Query(None),
                           minimo: int = Query(1, ge=1, le=20)):
        _auth(token, cmc_session)
        from ausentismo import analizar
        return JSONResponse(analizar(dias=dias, id_prof=prof, min_no_shows=minimo))

    @app.get("/api/ausentismo/paciente/{id_paciente}", tags=["ausentismo"], include_in_schema=False)
    def ausentismo_paciente(id_paciente: int, token: str | None = Query(None),
                            cmc_session: str | None = Cookie(None),
                            dias: int = Query(365, ge=30, le=730)):
        _auth(token, cmc_session)
        from ausentismo import historial_paciente
        return JSONResponse(historial_paciente(id_paciente, dias=dias))

    @app.post("/api/ausentismo/recolectar", tags=["ausentismo"], include_in_schema=False)
    async def ausentismo_recolectar_manual(token: str | None = Query(None),
                                           cmc_session: str | None = Cookie(None),
                                           dias: int = Query(365, ge=1, le=365)):
        """Lanza el barrido en segundo plano (no bloquea la respuesta); el
        estado del avance queda en system_state y la UI lo muestra."""
        _auth_dueno(token, cmc_session)
        from ausentismo import recolectar
        asyncio.create_task(recolectar(dias_atras=dias))
        return JSONResponse({"lanzado": True, "dias": dias})
