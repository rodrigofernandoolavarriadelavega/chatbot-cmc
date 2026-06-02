"""
Helpers compartidos de los routers de módulos Alma (pacientes, interconsultas,
esterilización, finanzas, equipo, documentos).

Centraliza la auth (misma que pagos_routes/agenda_routes) para no duplicarla en
cada router. Se llama explícitamente al inicio de cada endpoint, igual que el
patrón existente: `require_admin(request, token=token, cmc_session=cmc_session)`.
"""
import hmac as _hmac
from fastapi import HTTPException, Request, Query, Cookie


def require_admin(request: Request,
                  token: str | None = Query(None),
                  cmc_session: str | None = Cookie(None)) -> str:
    from config import ADMIN_TOKEN
    from admin_routes import _verify_cookie

    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        tk = auth_header.split(None, 1)[1].strip()
        if _hmac.compare_digest(tk, ADMIN_TOKEN):
            return tk
    if cmc_session:
        role = _verify_cookie(cmc_session)
        if role in ("admin", "ortodoncia"):
            return ADMIN_TOKEN
    if token and _hmac.compare_digest(token, ADMIN_TOKEN):
        return token
    raise HTTPException(status_code=401, detail="Token inválido")
