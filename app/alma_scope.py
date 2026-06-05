"""
Auth + scoping de perfiles Alma — fuente única de verdad.

Resuelve "¿quién entra y qué datos puede ver?" para todos los módulos Alma.
Hay dos clases de identidad:

  • Admin (cmc_admin_2026 / cmc_admin_olacore o cookie de sesión admin):
    acceso total. scope_profesional = None → las queries NO filtran.

  • Perfil de profesional (entrada en ALMA_PROFILES con `profesional_id`):
    acceso acotado. scope_profesional = ese id → las queries filtran por
    fa.profesional_id = id, y la Agenda se fuerza a su propio id_prof.
    Además solo puede entrar a los módulos listados en su `modulos`.

El `profesional_id` del perfil es el MISMO id en Medilink y en la BI
(bi.dim_profesional.profesional_id = id de Medilink — lo fija el ETL en
transform.py), así que un solo número sirve para filtrar ambas fuentes.
"""
import hmac

from fastapi import HTTPException

from config import ALMA_PROFILES, ADMIN_TOKEN, OLACORE_TOKEN

_ADMIN_TOKENS: tuple[str, ...] = (ADMIN_TOKEN, OLACORE_TOKEN)


def _is_admin(tk: str) -> bool:
    return any(hmac.compare_digest(tk, t) for t in _ADMIN_TOKENS)


def _bearer(request) -> str | None:
    if request is None:
        return None
    auth = request.headers.get("authorization", "") or ""
    if auth.lower().startswith("bearer "):
        return auth.split(None, 1)[1].strip()
    return None


def resolve(request, token: str | None, cmc_session: str | None,
            module_key: str) -> tuple[str, int | None]:
    """Auth para endpoints de API. Devuelve (token_efectivo, scope_profesional).

    scope_profesional: None = ve todo (admin) · int = filtra a ese profesional.
    Lanza 401 si el token es inválido, 403 si el perfil no incluye `module_key`.
    """
    tk = _bearer(request) or token
    if tk:
        if _is_admin(tk):
            return tk, None
        prof = ALMA_PROFILES.get(tk)
        if prof is not None:
            mods = prof.get("modulos")
            if mods is not None and module_key not in mods:
                raise HTTPException(403, "Módulo no permitido para este perfil")
            return tk, prof.get("profesional_id")
        raise HTTPException(401, "Token inválido")
    if cmc_session:
        from admin_routes import _verify_cookie
        if _verify_cookie(cmc_session) in ("admin", "ortodoncia"):
            return ADMIN_TOKEN, None
    raise HTTPException(401, "Token inválido")


def page_token(token: str | None, cmc_session: str | None,
               module_key: str | None = None) -> str | None:
    """Auth para page-handlers HTML (sin objeto Request).

    Devuelve el token a embeber en la página, o None si debe ir a /admin/login.
    `module_key=None` = solo exige login válido (p.ej. el shell contenedor).
    """
    if token:
        if _is_admin(token):
            return token
        prof = ALMA_PROFILES.get(token)
        if prof is not None:
            mods = prof.get("modulos")
            if module_key is None or mods is None or module_key in mods:
                return token
            return None  # perfil válido pero sin acceso a este módulo
        return None
    if cmc_session:
        from admin_routes import _verify_cookie
        if _verify_cookie(cmc_session) in ("admin", "ortodoncia"):
            return ADMIN_TOKEN
    return None
