"""Plan de Dirección — tracker de formación del dueño en su transición de
operador-fundador a director de portafolio (holding OLACORE).

Auth: token admin/olacore o cookie. El progreso se persiste en `direccion_kv`
(sincroniza entre dispositivos: lo marca en el celular y lo ve en el computador).
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from fastapi import Query, Cookie, HTTPException, Request
from fastapi.responses import HTMLResponse


def _auth(token, cmc_session):
    from admin_routes import _verify_cookie, _is_admin_token
    if not ((token and _is_admin_token(token)) or (cmc_session and _verify_cookie(cmc_session))):
        raise HTTPException(403, "No autorizado")


_TPL = Path(__file__).parent.parent / "templates" / "direccion_plan.html"


def _ensure(c):
    c.execute("CREATE TABLE IF NOT EXISTS direccion_kv (k TEXT PRIMARY KEY, v TEXT, updated_at TEXT)")


def register_direccion_routes(app):

    @app.get("/direccion", response_class=HTMLResponse, include_in_schema=False)
    def direccion_page(token: str | None = Query(None), cmc_session: str | None = Cookie(None)):
        _auth(token, cmc_session)
        html = _TPL.read_text(encoding="utf-8") if _TPL.exists() else "<h1>Falta el template</h1>"
        html = html.replace("__DIR_TOKEN__", token or "")
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    @app.get("/api/direccion/progress", tags=["direccion"], include_in_schema=False)
    def direccion_get(token: str | None = Query(None), cmc_session: str | None = Cookie(None)):
        _auth(token, cmc_session)
        from session import db
        with db() as c:
            _ensure(c)
            r = c.execute("SELECT v FROM direccion_kv WHERE k='progress'").fetchone()
        try:
            return json.loads(r[0]) if r else {}
        except Exception:
            return {}

    @app.post("/api/direccion/progress", tags=["direccion"], include_in_schema=False)
    async def direccion_set(request: Request,
                            token: str | None = Query(None), cmc_session: str | None = Cookie(None)):
        _auth(token, cmc_session)
        body = await request.json()
        from session import db
        with db() as c:
            _ensure(c)
            c.execute(
                "INSERT INTO direccion_kv (k, v, updated_at) VALUES ('progress', ?, ?) "
                "ON CONFLICT(k) DO UPDATE SET v=excluded.v, updated_at=excluded.updated_at",
                (json.dumps(body, ensure_ascii=False), datetime.now().isoformat(timespec="seconds")))
        return {"ok": True}
