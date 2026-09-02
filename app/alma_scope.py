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

from config import ALMA_PROFILES, ADMIN_TOKEN, OLACORE_TOKEN, ORTODONCIA_TOKEN

_ADMIN_TOKENS: tuple[str, ...] = (ADMIN_TOKEN, OLACORE_TOKEN)


def _is_admin(tk: str) -> bool:
    return any(hmac.compare_digest(tk, t) for t in _ADMIN_TOKENS)


def profesional_id_of(token: str | None) -> int | None:
    """profesional_id del perfil (para deep-link a su panel), o None si el token
    no es un perfil de profesional (admin/recepción)."""
    prof = ALMA_PROFILES.get(token or "")
    return prof.get("profesional_id") if prof else None


def _bearer(request) -> str | None:
    if request is None:
        return None
    auth = request.headers.get("authorization", "") or ""
    if auth.lower().startswith("bearer "):
        return auth.split(None, 1)[1].strip()
    return None


def shows_money(token: str | None) -> bool:
    """¿Este token ve cifras de INGRESO? Solo el dueño (OLACORE_TOKEN). Recepción
    y perfiles de profesional NO. Recibe el token efectivo (el de page_token)."""
    return bool(token) and hmac.compare_digest(token, OLACORE_TOKEN)


def ver_ingreso_of(token: str | None) -> bool:
    """¿Este perfil de profesional puede ver su 'Ingreso recuperable' en Programas?
    Se habilita por perfil con el flag `ver_ingreso` (pedido del dueño, p.ej. Gisela)."""
    p = ALMA_PROFILES.get(token or "")
    return bool(p and p.get("ver_ingreso"))


def is_readonly(token: str | None) -> bool:
    """¿Este perfil ve los módulos financieros (Pagos/Abonos) en SOLO LECTURA?
    Flag `pagos_readonly`. Un token así no puede crear/editar/borrar (403 en endpoints)."""
    p = ALMA_PROFILES.get(token or "")
    return bool(p and p.get("pagos_readonly"))


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
        rol = _verify_cookie(cmc_session)
        if rol == "ortodoncia":
            # La cookie dental NO se promueve a admin: se resuelve como su
            # propio perfil, con la allowlist de ALMA_PROFILES. Antes devolvia
            # (ADMIN_TOKEN, None) y con eso veia EBITDA y liquidaciones enteras.
            return resolve(request, ORTODONCIA_TOKEN, None, module_key)
        if rol == "admin":
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
        rol = _verify_cookie(cmc_session)
        if rol == "ortodoncia":
            return page_token(ORTODONCIA_TOKEN, None, module_key)
        if rol == "admin":
            return ADMIN_TOKEN
    return None
