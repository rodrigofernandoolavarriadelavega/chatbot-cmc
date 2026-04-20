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
                     add_vital, list_vitals, delete_vital,
                     count_portal_otps, get_dx_tags, get_profile,
                     get_profile_full, update_profile_fields)
from medilink import buscar_paciente, listar_citas_paciente, listar_historial_paciente, valid_rut

log = logging.getLogger("bot.portal")

router = APIRouter(tags=["portal"])

_COOKIE_NAME = "portal_session"

# ═══ Modo demo ═══════════════════════════════════════════════════════════
# RUT ficticio (50.000.000-X) para compartir la demo con socios sin exponer
# datos reales. Código fijo, OTP skipped.
DEMO_RUT = "50000000-7"
DEMO_CODE = "123456"
DEMO_PHONE = "56900000000"


def is_demo_rut(rut_raw: str) -> bool:
    clean = (rut_raw or "").replace(".", "").replace("-", "").upper().strip()
    return clean.startswith("50000000")


def _demo_data() -> dict:
    """Data ficticia para modo demo. Fechas relativas al día actual."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    hoy = datetime.now(ZoneInfo("America/Santiago")).date()

    def ymd(d):
        return d.strftime("%Y-%m-%d")

    def fmt_es(d):
        dias = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]
        meses = ["enero","febrero","marzo","abril","mayo","junio","julio","agosto","septiembre","octubre","noviembre","diciembre"]
        return f"{dias[d.weekday()]} {d.day} de {meses[d.month-1]}"

    citas_futuras = [
        {"id": 901, "id_profesional": 60, "profesional": "Dr. Miguel Millán",
         "especialidad": "Cardiología",
         "fecha": ymd(hoy + timedelta(days=3)),
         "fecha_display": fmt_es(hoy + timedelta(days=3)),
         "hora_inicio": "11:00", "estado": "Confirmada"},
        {"id": 902, "id_profesional": 52, "profesional": "Gisela Pinto",
         "especialidad": "Nutrición",
         "fecha": ymd(hoy + timedelta(days=10)),
         "fecha_display": fmt_es(hoy + timedelta(days=10)),
         "hora_inicio": "15:30", "estado": "Confirmada"},
    ]
    historial = [
        {"id": 801, "profesional": "Dr. Rodrigo Olavarría", "especialidad": "Medicina General",
         "fecha": ymd(hoy - timedelta(days=14)), "fecha_display": fmt_es(hoy - timedelta(days=14)),
         "hora_inicio": "10:00"},
        {"id": 802, "profesional": "Dr. Andrés Abarca", "especialidad": "Medicina General",
         "fecha": ymd(hoy - timedelta(days=45)), "fecha_display": fmt_es(hoy - timedelta(days=45)),
         "hora_inicio": "09:30"},
        {"id": 803, "profesional": "Luis Armijo", "especialidad": "Kinesiología",
         "fecha": ymd(hoy - timedelta(days=60)), "fecha_display": fmt_es(hoy - timedelta(days=60)),
         "hora_inicio": "16:00"},
        {"id": 804, "profesional": "Dr. Claudio Barraza", "especialidad": "Traumatología",
         "fecha": ymd(hoy - timedelta(days=90)), "fecha_display": fmt_es(hoy - timedelta(days=90)),
         "hora_inicio": "12:00"},
        {"id": 805, "profesional": "Dra. Javiera Burgos", "especialidad": "Odontología General",
         "fecha": ymd(hoy - timedelta(days=150)), "fecha_display": fmt_es(hoy - timedelta(days=150)),
         "hora_inicio": "17:00"},
    ]
    return {
        "nombre": "María Ejemplo Demo",
        "rut": "50.000.000-7",
        "fecha_nacimiento": "1975-06-15",
        "sexo": "F",
        "citas_futuras": citas_futuras,
        "historial": historial,
        "diagnosticos": ["HTA", "DM2"],
        "whatsapp_url": "https://wa.me/56966610737?text=Hola%2C%20quiero%20agendar%20una%20cita",
        "demo": True,
    }
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

    # Modo demo: RUT 50.000.000-X → salta OTP por WhatsApp
    if is_demo_rut(rut):
        return {"ok": True, "rut_masked": "50.***.0-0", "demo": True, "hint": f"Código demo: {DEMO_CODE}"}

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

    # Modo demo
    if is_demo_rut(rut):
        if code != DEMO_CODE:
            raise HTTPException(status_code=401, detail=f"Código demo: {DEMO_CODE}")
        rut_norm = DEMO_RUT
        phone = DEMO_PHONE
    else:
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

    # Modo demo: devolver datos ficticios
    if rut == DEMO_RUT:
        return _demo_data()

    # Buscar paciente en Medilink
    paciente = await buscar_paciente(rut)
    if not paciente:
        raise HTTPException(status_code=404, detail="Paciente no encontrado en el sistema")

    id_pac = paciente["id"]
    nombre = paciente["nombre"]

    # Fetch paralelo: citas futuras + historial
    import asyncio
    rut_medilink = paciente.get("rut") or ""
    citas_futuras, historial = await asyncio.gather(
        listar_citas_paciente(id_pac, rut=rut_medilink),
        listar_historial_paciente(id_pac, meses=12, rut=rut_medilink),
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


# ══ Registros personales del paciente (auto-monitoreo) ═══════════════════
_VITAL_TIPOS_OK = {"presion", "glicemia", "peso", "temperatura"}


@router.post("/portal/api/vitals")
async def portal_add_vital(request: Request,
                           portal_session: str | None = Cookie(None)):
    """Añade un registro (presión, glicemia, peso, temperatura)."""
    result = _verify_portal_cookie(portal_session)
    if not result:
        raise HTTPException(status_code=401, detail="Sesión expirada")
    rut, _ = result
    body = await request.json()
    tipo = (body.get("tipo") or "").strip().lower()
    if tipo not in _VITAL_TIPOS_OK:
        raise HTTPException(status_code=400, detail="Tipo inválido")
    try:
        valor = float(body.get("valor"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Valor inválido")
    valor2 = body.get("valor2")
    if valor2 is not None and valor2 != "":
        try:
            valor2 = float(valor2)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Valor2 inválido")
    else:
        valor2 = None
    contexto = (body.get("contexto") or "").strip() or None
    nota = (body.get("nota") or "").strip() or None
    ts = (body.get("ts") or "").strip() or None
    # Validaciones por tipo
    if tipo == "presion":
        if not (50 <= valor <= 260) or valor2 is None or not (30 <= valor2 <= 180):
            raise HTTPException(status_code=400, detail="Presión fuera de rango")
    elif tipo == "glicemia":
        if not (20 <= valor <= 600):
            raise HTTPException(status_code=400, detail="Glicemia fuera de rango")
    elif tipo == "peso":
        if not (20 <= valor <= 300):
            raise HTTPException(status_code=400, detail="Peso fuera de rango")
    elif tipo == "temperatura":
        if not (30 <= valor <= 43):
            raise HTTPException(status_code=400, detail="Temperatura fuera de rango")
    vid = add_vital(rut, tipo, valor, valor2, contexto, nota, ts)
    return {"ok": True, "id": vid}


@router.get("/portal/api/vitals")
async def portal_list_vitals(tipo: str | None = None, dias: int | None = None,
                             limit: int = 200,
                             portal_session: str | None = Cookie(None)):
    """Lista registros del paciente."""
    result = _verify_portal_cookie(portal_session)
    if not result:
        raise HTTPException(status_code=401, detail="Sesión expirada")
    rut, _ = result
    if tipo and tipo not in _VITAL_TIPOS_OK:
        raise HTTPException(status_code=400, detail="Tipo inválido")
    vitals = list_vitals(rut, tipo=tipo, dias=dias, limit=max(1, min(500, limit)))
    return {"ok": True, "vitals": vitals}


@router.get("/portal/api/perfil")
async def portal_get_perfil(portal_session: str | None = Cookie(None)):
    """Devuelve los campos editables del perfil del paciente."""
    result = _verify_portal_cookie(portal_session)
    if not result:
        raise HTTPException(status_code=401, detail="Sesión expirada")
    rut, phone = result
    if rut == DEMO_RUT:
        return {
            "ok": True, "demo": True,
            "profile": {
                "nombre": "María Ejemplo Demo",
                "fecha_nacimiento": "1975-06-15",
                "sexo": "F",
                "email": "demo@cmc.cl",
                "comuna": "Arauco",
                "direccion": "Calle Ficticia 123",
                "prevision": "Fonasa C",
                "contacto_emerg_nombre": "Juan Ejemplo",
                "contacto_emerg_telefono": "+56 9 8765 4321",
            },
        }
    prof = get_profile_full(phone)
    return {"ok": True, "profile": prof}


@router.post("/portal/api/perfil")
async def portal_update_perfil(request: Request,
                                portal_session: str | None = Cookie(None)):
    """Actualiza campos editables del perfil."""
    result = _verify_portal_cookie(portal_session)
    if not result:
        raise HTTPException(status_code=401, detail="Sesión expirada")
    rut, phone = result
    body = await request.json()
    # Validaciones ligeras
    campos = ("nombre", "fecha_nacimiento", "sexo", "email", "comuna",
              "direccion", "prevision", "contacto_emerg_nombre",
              "contacto_emerg_telefono")
    data = {k: (body.get(k) or "").strip() or None for k in campos if k in body}
    # Email básico
    if data.get("email") and "@" not in data["email"]:
        raise HTTPException(status_code=400, detail="Email inválido")
    # Sexo
    if data.get("sexo") and data["sexo"] not in ("M", "F", "O"):
        raise HTTPException(status_code=400, detail="Sexo inválido")
    if rut == DEMO_RUT:
        return {"ok": True, "demo": True}  # no persistir demo
    update_profile_fields(phone, rut, data)
    return {"ok": True}


@router.delete("/portal/api/vitals/{vital_id}")
async def portal_delete_vital(vital_id: int,
                              portal_session: str | None = Cookie(None)):
    """Elimina un registro del paciente."""
    result = _verify_portal_cookie(portal_session)
    if not result:
        raise HTTPException(status_code=401, detail="Sesión expirada")
    rut, _ = result
    ok = delete_vital(rut, vital_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Registro no encontrado")
    return {"ok": True}
