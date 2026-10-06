"""recuperacion_routes.py — Página y API de "Recuperar pacientes" (recepción).

Auth del patrón Alma de recepción: token ADMIN_TOKEN/OLACORE_TOKEN (query o
Bearer) o cookie de sesión `admin`. Sin credencial 401; credencial inválida 403.
La cookie `ortodoncia` NO entra: el perfil dental no ve esta cola.

La respuesta nunca incluye gasto, campañas ni anuncios (ver recuperacion.py).
"""
from __future__ import annotations

from pathlib import Path

from fastapi import Cookie, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

_TPL = Path(__file__).resolve().parent.parent / "templates" / "alma_recuperar.html"


def _auth(request: Request, token: str | None, cmc_session: str | None) -> None:
    from admin_routes import _verify_cookie, _is_admin_token
    auth = request.headers.get("authorization", "") or ""
    tk = auth.split(None, 1)[1].strip() if auth.lower().startswith("bearer ") else token
    if tk:
        if _is_admin_token(tk):
            return
        raise HTTPException(403, "No autorizado")
    if cmc_session:
        if _verify_cookie(cmc_session) == "admin":
            return
        raise HTTPException(403, "No autorizado")
    raise HTTPException(401, "Falta el token")


def register_recuperacion_routes(app):
    @app.get("/alma/recuperar", response_class=HTMLResponse, include_in_schema=False)
    def recuperar_page(token: str | None = Query(None), cmc_session: str | None = Cookie(None)):
        import alma_scope
        if not _TPL.exists():
            raise HTTPException(404, "Recuperar pacientes no disponible")
        tk = alma_scope.page_token(token, cmc_session, "recuperar")
        if not tk:
            if token or cmc_session:
                raise HTTPException(403, "Este módulo no está disponible para tu perfil")
            return RedirectResponse(url="/admin/login", status_code=302)
        return HTMLResponse(_TPL.read_text(encoding="utf-8").replace("__TOKEN__", tk),
                            headers={"Cache-Control": "no-store"})

    @app.get("/api/recuperar/lista", tags=["recuperar"], include_in_schema=False)
    async def recuperar_lista(request: Request, dias: int = Query(30), gestion: str | None = Query(None),
                              token: str | None = Query(None), cmc_session: str | None = Cookie(None)):
        _auth(request, token, cmc_session)
        import asyncio
        import recuperacion as rec
        d = await asyncio.to_thread(rec.lista, dias, None, gestion or None)
        est = await rec.estado_plantillas()
        d = rec.publica(d)
        d["plantillas"] = {n: {"estado": est.get(n, "NO_VERIFICADA"), "aprobada": est.get(n) == "APPROVED"}
                           for n in sorted(set(rec.PLANTILLAS.values()))}
        return JSONResponse(d, headers={"Cache-Control": "no-store"})

    @app.post("/api/recuperar/{clave}/enviar", tags=["recuperar"], include_in_schema=False)
    async def recuperar_enviar(clave: str, request: Request, token: str | None = Query(None),
                               cmc_session: str | None = Cookie(None)):
        """Envía UN recordatorio a UNA persona (nunca en lote). Cuerpo:
        {modo: 'texto'|'plantilla', mensaje, confirmar: true}."""
        _auth(request, token, cmc_session)
        import campanas_meta_routes as cm
        import recuperacion as rec
        clave = cm._clave_valida(clave)
        try:
            b = await request.json()
        except ValueError:
            raise HTTPException(400, "Cuerpo inválido")
        if not isinstance(b, dict):
            raise HTTPException(400, "Cuerpo inválido")
        try:
            return await rec.enviar(clave, str(b.get("modo") or ""), b.get("mensaje"), b.get("confirmar") is True)
        except rec.NoEnviable as e:
            raise HTTPException(e.status, str(e))
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001 — red/Meta caída: recepción debe saber que NO salió
            rec.log.warning("recuperacion: envío falló para %s: %s", clave, e)
            raise HTTPException(502, "No se pudo enviar el mensaje por WhatsApp. Intenta de nuevo en unos minutos "
                                     "o llama a la persona.")

    @app.post("/api/recuperar/{clave}/gestion", tags=["recuperar"], include_in_schema=False)
    async def recuperar_gestion(clave: str, request: Request, token: str | None = Query(None),
                                cmc_session: str | None = Cookie(None)):
        """Estado de gestión (misma tabla que la ficha del dueño en Campañas Meta)."""
        _auth(request, token, cmc_session)
        import campanas_meta_routes as cm
        try:
            b = await request.json()
        except ValueError:
            raise HTTPException(400, "Cuerpo inválido")
        if not isinstance(b, dict):
            raise HTTPException(400, "Cuerpo inválido")
        return cm.guardar_seguimiento(clave, str(b.get("estado") or ""), b.get("nota"), b.get("proximo"),
                                      origen="recepción")
