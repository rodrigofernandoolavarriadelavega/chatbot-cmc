"""Portal del Paciente — rutas API para autenticación y datos."""
import hashlib
import hmac
import logging
import secrets
import time

from fastapi import APIRouter, Request, HTTPException, Cookie
from fastapi.responses import JSONResponse

from config import PORTAL_SESSION_SECRET, ADMIN_TOKEN
from messaging import send_whatsapp
from session import (get_phone_by_rut, save_portal_otp, verify_portal_otp,
                     count_portal_otps, get_dx_tags, get_profile)
from medilink import buscar_paciente, listar_citas_paciente, listar_historial_paciente, valid_rut

log = logging.getLogger("bot.portal")

router = APIRouter(tags=["portal"])

_COOKIE_NAME = "portal_session"
_COOKIE_MAX_AGE = 24 * 3600  # 24 hours


# ── Cookie signing ───────────────────────────────────────────────────────────

def _portal_key() -> bytes:
    secret = PORTAL_SESSION_SECRET or ADMIN_TOKEN
    return hashlib.sha256(f"cmc-portal-sign:{secret}".encode()).digest()


def _sign_portal_cookie(rut: str, phone: str) -> str:
    expires = int(time.time()) + _COOKIE_MAX_AGE
    payload = f"{rut}:{phone}:{expires}"
    sig = hmac.new(_portal_key(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}:{sig}"


def _verify_portal_cookie(value: str) -> tuple[str, str] | None:
    """Verifica cookie del portal. Retorna (rut, phone) o None."""
    if not value:
        return None
    parts = value.rsplit(":", 1)
    if len(parts) != 2:
        return None
    payload, sig = parts
    expected = hmac.new(_portal_key(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    segments = payload.split(":")
    if len(segments) != 3:
        return None
    rut, phone, expires_str = segments
    try:
        if time.time() > int(expires_str):
            return None
    except ValueError:
        return None
    return (rut, phone)


def _require_portal(portal_session: str | None = Cookie(None)) -> tuple[str, str]:
    """Dependency: valida cookie del portal, retorna (rut, phone)."""
    result = _verify_portal_cookie(portal_session)
    if not result:
        raise HTTPException(status_code=401, detail="Sesión expirada")
    return result


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.post("/portal/api/request-code")
async def portal_request_code(request: Request):
    """Envía código OTP al WhatsApp del paciente."""
    body = await request.json()
    rut = body.get("rut", "").strip()

    if not rut or not valid_rut(rut):
        raise HTTPException(status_code=400, detail="RUT inválido")

    # Normalizar RUT
    rut_clean = rut.replace(".", "").replace("-", "").strip().upper()
    if len(rut_clean) > 1:
        rut_norm = rut_clean[:-1] + "-" + rut_clean[-1]
    else:
        raise HTTPException(status_code=400, detail="RUT inválido")

    # Rate limit: max 3 OTPs por hora
    if count_portal_otps(rut_norm) >= 3:
        raise HTTPException(status_code=429, detail="Demasiados intentos. Espera unos minutos.")

    # Buscar teléfono en contact_profiles
    phone = get_phone_by_rut(rut_norm)
    if not phone:
        # Intentar sin guión
        phone = get_phone_by_rut(rut_clean)

    if not phone:
        # Verificar que el paciente existe en Medilink (aunque no tenga WhatsApp registrado)
        pac = await buscar_paciente(rut)
        if pac:
            raise HTTPException(
                status_code=404,
                detail="Para activar tu portal, escríbenos primero al WhatsApp: +56 9 4588 6628"
            )
        raise HTTPException(status_code=404, detail="RUT no encontrado")

    # Generar código de 6 dígitos
    code = f"{secrets.randbelow(1000000):06d}"
    save_portal_otp(rut_norm, phone, code)

    # Enviar por WhatsApp
    await send_whatsapp(
        phone,
        f"🔐 Tu código de acceso al Portal del Paciente es: *{code}*\n\n"
        "Expira en 5 minutos.\n"
        "Si no solicitaste este código, ignora este mensaje."
    )

    # Enmascarar RUT para respuesta
    rut_masked = rut_norm[:2] + "." + "***" + "." + rut_norm[-3:]
    log.info("Portal OTP enviado rut=%s phone=%s", rut_norm, phone[:6] + "***")

    return {"ok": True, "rut_masked": rut_masked}


@router.post("/portal/api/verify-code")
async def portal_verify_code(request: Request):
    """Verifica el código OTP y crea sesión."""
    body = await request.json()
    rut = body.get("rut", "").strip()
    code = body.get("code", "").strip()

    if not rut or not code:
        raise HTTPException(status_code=400, detail="RUT y código requeridos")

    rut_clean = rut.replace(".", "").replace("-", "").strip().upper()
    if len(rut_clean) > 1:
        rut_norm = rut_clean[:-1] + "-" + rut_clean[-1]
    else:
        raise HTTPException(status_code=400, detail="RUT inválido")

    phone = verify_portal_otp(rut_norm, code)
    if not phone:
        raise HTTPException(status_code=401, detail="Código incorrecto o expirado")

    # Crear cookie de sesión
    is_https = (request.url.scheme == "https"
                or request.headers.get("x-forwarded-proto") == "https")

    response = JSONResponse({"ok": True})
    response.set_cookie(
        key=_COOKIE_NAME,
        value=_sign_portal_cookie(rut_norm, phone),
        max_age=_COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=is_https,
        path="/",
    )
    log.info("Portal login OK rut=%s", rut_norm)
    return response


@router.get("/portal/api/datos")
async def portal_datos(portal_session: str | None = Cookie(None)):
    """Retorna los datos del paciente autenticado."""
    result = _verify_portal_cookie(portal_session)
    if not result:
        raise HTTPException(status_code=401, detail="Sesión expirada")
    rut, phone = result

    # Buscar paciente en Medilink
    paciente = await buscar_paciente(rut)
    if not paciente:
        raise HTTPException(status_code=404, detail="Paciente no encontrado en el sistema")

    id_pac = paciente["id"]
    nombre = paciente["nombre"]

    # Fetch paralelo: citas futuras + historial
    import asyncio
    citas_futuras, historial = await asyncio.gather(
        listar_citas_paciente(id_pac),
        listar_historial_paciente(id_pac, meses=12),
    )

    # Tags de diagnóstico
    diagnosticos = get_dx_tags(phone)

    # Datos del perfil local
    perfil = get_profile(phone)

    return {
        "nombre": nombre,
        "rut": rut,
        "fecha_nacimiento": paciente.get("fecha_nacimiento", ""),
        "sexo": paciente.get("sexo", ""),
        "citas_futuras": citas_futuras,
        "historial": historial,
        "diagnosticos": diagnosticos,
        "whatsapp_url": "https://wa.me/56966610737?text=Hola%2C%20quiero%20agendar%20una%20cita",
    }


@router.post("/portal/api/logout")
async def portal_logout():
    """Cierra la sesión del portal."""
    response = JSONResponse({"ok": True})
    response.delete_cookie(key=_COOKIE_NAME, path="/")
    return response
