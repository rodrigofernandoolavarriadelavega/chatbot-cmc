"""Cerebro Alma — grafo de conocimiento interactivo del organismo CMC.

Módulo del shell Alma (key "grafo" en ALMA_MODULE_REGISTRY). El HTML es
autocontenido (datos embebidos, sin fetch), así que la ruta solo gatea acceso
con la misma auth que los demás módulos (token admin o cookie de sesión).
"""

from pathlib import Path
from fastapi import HTTPException, Query, Cookie
from fastapi.responses import HTMLResponse, RedirectResponse

_TEMPLATE_DIR = Path(__file__).parent.parent / "templates"
_GRAFO_HTML = (_TEMPLATE_DIR / "alma_grafo.html").read_text(encoding="utf-8") \
    if (_TEMPLATE_DIR / "alma_grafo.html").exists() else ""


def register_grafo_routes(app):
    """Registra /alma/grafo. Llamar desde main.py:
    import grafo_routes; grafo_routes.register_grafo_routes(app)"""

    @app.get("/alma/grafo", response_class=HTMLResponse, tags=["alma"])
    def alma_grafo_page(token: str | None = Query(None),
                        cmc_session: str | None = Cookie(None)):
        from admin_routes import _verify_cookie, _is_admin_token
        if not _GRAFO_HTML:
            raise HTTPException(404, "Cerebro Alma no disponible")
        if token and _is_admin_token(token):
            return HTMLResponse(_GRAFO_HTML)
        if cmc_session and _verify_cookie(cmc_session) == "admin":
            return HTMLResponse(_GRAFO_HTML)
        return RedirectResponse(url="/admin/login", status_code=302)
