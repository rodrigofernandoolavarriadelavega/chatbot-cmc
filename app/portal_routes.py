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
                     get_profile_full, update_profile_fields,
                     add_family_link, list_family_links, revoke_family_link,
                     is_family_link, log_event)
from medilink import buscar_paciente, listar_citas_paciente, listar_historial_paciente, valid_rut

log = logging.getLogger("bot.portal")

router = APIRouter(tags=["portal"])

_COOKIE_NAME = "portal_session"
_ACTIVE_COOKIE_NAME = "portal_active"

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
        {"id": 900, "id_profesional": 73, "profesional": "Dr. Andrés Abarca",
         "especialidad": "Medicina General",
         "fecha": ymd(hoy),
         "fecha_display": fmt_es(hoy),
         "hora_inicio": "17:30", "estado": "Confirmada"},
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


# Familia ficticia del modo demo: permite recorrer la estructura familiar completa
# (switch real entre perfiles con datos inventados, nada toca Medilink).
DEMO_FAMILY = {
    "50000001-5": {"nombre": "Tomás Ejemplo Demo", "relation": "hijo/a",
                   "sexo": "M", "fnac": "2018-03-10", "rut_fmt": "50.000.001-5"},
    "50000002-3": {"nombre": "Pedro Ejemplo Demo", "relation": "cónyuge",
                   "sexo": "M", "fnac": "1981-01-20", "rut_fmt": "50.000.002-3"},
    "50000003-1": {"nombre": "Rosa Ejemplo Demo", "relation": "madre/padre",
                   "sexo": "F", "fnac": "1958-04-02", "rut_fmt": "50.000.003-1"},
    "50000004-K": {"nombre": "Carmen Ejemplo Demo", "relation": "vecina",
                   "sexo": "F", "fnac": "1952-09-18", "rut_fmt": "50.000.004-K"},
}


def _demo_member_data(rut: str) -> dict:
    """Datos ficticios completos para un familiar del modo demo."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    hoy = datetime.now(ZoneInfo("America/Santiago")).date()
    meta = DEMO_FAMILY[rut]

    def ymd(d):
        return d.strftime("%Y-%m-%d")

    def fmt_es(d):
        dias = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]
        meses = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
                 "agosto", "septiembre", "octubre", "noviembre", "diciembre"]
        return f"{dias[d.weekday()]} {d.day} de {meses[d.month-1]}"

    def cita(idx, prof_id, prof, esp, delta, hora):
        d = hoy + timedelta(days=delta)
        return {"id": idx, "id_profesional": prof_id, "profesional": prof,
                "especialidad": esp, "fecha": ymd(d), "fecha_display": fmt_es(d),
                "hora_inicio": hora, "estado": "Confirmada"}

    def aten(idx, prof, esp, hace_dias, hora):
        d = hoy - timedelta(days=hace_dias)
        return {"id": idx, "profesional": prof, "especialidad": esp,
                "fecha": ymd(d), "fecha_display": fmt_es(d), "hora_inicio": hora}

    perfiles = {
        "50000001-5": {  # Tomás, 8 años
            "citas_futuras": [cita(911, 55, "Dra. Javiera Burgos", "Odontología General", 5, "16:30")],
            "historial": [
                aten(811, "Dr. Andrés Abarca", "Medicina General", 75, "11:30"),
                aten(812, "Dra. Javiera Burgos", "Odontología General", 210, "16:00"),
                aten(813, "Juana Arratia", "Fonoaudiología", 300, "15:00"),
            ],
            "diagnosticos": ["Asma"],
        },
        "50000002-3": {  # Pedro, 45 años — sin citas futuras (cross-sell)
            "citas_futuras": [],
            "historial": [
                aten(821, "Luis Armijo", "Kinesiología", 120, "17:20"),
                aten(822, "Dr. Claudio Barraza", "Traumatología", 150, "12:20"),
                aten(823, "Dr. Rodrigo Olavarría", "Medicina General", 400, "10:15"),
            ],
            "diagnosticos": ["HTA"],
        },
        "50000004-K": {  # Carmen, 74 años — vecina sin citas (le gestionan las horas)
            "citas_futuras": [],
            "historial": [
                aten(841, "Dr. Andrés Abarca", "Medicina General", 60, "10:30"),
                aten(842, "David Pardo", "Ecografía", 270, "12:45"),
            ],
            "diagnosticos": ["HTA"],
        },
        "50000003-1": {  # Rosa, 68 años
            "citas_futuras": [cita(931, 77, "Luis Armijo", "Kinesiología", 8, "10:40")],
            "historial": [
                aten(831, "Dr. Andrés Abarca", "Medicina General", 30, "09:00"),
                aten(832, "Dr. Miguel Millán", "Cardiología", 240, "11:20"),
                aten(833, "Gisela Pinto", "Nutrición", 100, "15:45"),
            ],
            "diagnosticos": ["DM2", "Artrosis"],
        },
    }
    p = perfiles[rut]
    return {
        "nombre": meta["nombre"],
        "rut": meta["rut_fmt"],
        "fecha_nacimiento": meta["fnac"],
        "sexo": meta["sexo"],
        "citas_futuras": p["citas_futuras"],
        "historial": p["historial"],
        "diagnosticos": p["diagnosticos"],
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


# ── Paciente activo (owner + dependientes familiares) ────────────────────────

def _sign_active_cookie(owner_rut: str, active_rut: str) -> str:
    expires = int(time.time()) + _COOKIE_MAX_AGE
    payload = f"{owner_rut}:{active_rut}:{expires}"
    sig = hmac.new(_portal_key(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}:{sig}"


def _verify_active_cookie(value: str, owner_rut: str) -> str | None:
    """Verifica la cookie de paciente activo. Retorna active_rut válido si
    coincide con el owner_rut de la sesión actual."""
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
    cookie_owner, active_rut, expires_str = segments
    if cookie_owner != owner_rut:
        return None
    try:
        if time.time() > int(expires_str):
            return None
    except ValueError:
        return None
    return active_rut


def _resolve_context(portal_session: str | None,
                     portal_active: str | None) -> tuple[str, str, str, str]:
    """Dependency interna: retorna (owner_rut, owner_phone, active_rut, active_phone).
    Si no hay cookie activa o es inválida, active = owner.
    Si active != owner, valida que sea un familiar vinculado."""
    result = _verify_portal_cookie(portal_session)
    if not result:
        raise HTTPException(status_code=401, detail="Sesión expirada")
    owner_rut, owner_phone = result
    active_rut = owner_rut
    if portal_active:
        candidate = _verify_active_cookie(portal_active, owner_rut)
        if candidate and candidate != owner_rut:
            if (candidate == DEMO_RUT
                    or (owner_rut == DEMO_RUT and candidate in DEMO_FAMILY)
                    or is_family_link(owner_rut, candidate)):
                active_rut = candidate
    if active_rut == owner_rut:
        active_phone = owner_phone
    else:
        active_phone = get_phone_by_rut(active_rut) or owner_phone
    return owner_rut, owner_phone, active_rut, active_phone


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
        raise HTTPException(status_code=429, detail="Demasiados intentos. Espere unos minutos.")

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
                detail="Para activar su portal, escríbanos primero al WhatsApp: +56 9 6661 0737"
            )
        raise HTTPException(status_code=404, detail="RUT no encontrado")

    # Generar código de 6 dígitos
    code = f"{secrets.randbelow(1000000):06d}"
    save_portal_otp(rut_norm, phone, code)

    # Enviar por WhatsApp
    await send_whatsapp(
        phone,
        f"🔐 Su código de acceso al Portal del Paciente es: *{code}*\n\n"
        "Vence en 10 minutos.\n"
        "Si usted no pidió este código, ignore este mensaje."
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
    # Limpiar cookie de paciente activo de sesiones previas
    response.delete_cookie(key=_ACTIVE_COOKIE_NAME, path="/")
    log.info("Portal login OK rut=%s", rut_norm)
    return response


@router.get("/portal/api/datos")
async def portal_datos(portal_session: str | None = Cookie(None),
                       portal_active: str | None = Cookie(None)):
    """Retorna los datos del paciente activo (owner o familiar vinculado)."""
    owner_rut, owner_phone, active_rut, active_phone = _resolve_context(portal_session, portal_active)

    # Modo demo: datos ficticios cuando el activo es el RUT demo
    if active_rut == DEMO_RUT or (owner_rut == DEMO_RUT and active_rut in DEMO_FAMILY):
        data = _demo_data() if active_rut == DEMO_RUT else _demo_member_data(active_rut)
        data["owner_rut"] = owner_rut
        data["is_dependent"] = (active_rut != owner_rut)
        return data

    paciente = await buscar_paciente(active_rut)
    if not paciente:
        raise HTTPException(status_code=404, detail="Paciente no encontrado en el sistema")

    id_pac = paciente["id"]
    nombre = paciente["nombre"]

    import asyncio
    rut_medilink = paciente.get("rut") or ""
    citas_futuras, historial = await asyncio.gather(
        listar_citas_paciente(id_pac, rut=rut_medilink),
        listar_historial_paciente(id_pac, meses=12, rut=rut_medilink),
    )

    # Los tags de dx están ligados al teléfono; para dependientes usamos el del dependiente si lo hay
    diagnosticos = get_dx_tags(active_phone) if active_phone else []

    return {
        "nombre": nombre,
        "rut": active_rut,
        "fecha_nacimiento": paciente.get("fecha_nacimiento", ""),
        "sexo": paciente.get("sexo", ""),
        "citas_futuras": citas_futuras,
        "historial": historial,
        "diagnosticos": diagnosticos,
        "whatsapp_url": "https://wa.me/56966610737?text=Hola%2C%20quiero%20agendar%20una%20cita",
        "owner_rut": owner_rut,
        "is_dependent": (active_rut != owner_rut),
    }


@router.post("/portal/api/logout")
async def portal_logout():
    """Cierra la sesión del portal."""
    response = JSONResponse({"ok": True})
    response.delete_cookie(key=_COOKIE_NAME, path="/")
    response.delete_cookie(key=_ACTIVE_COOKIE_NAME, path="/")
    return response


# ══ Registros personales del paciente (auto-monitoreo) ═══════════════════
_VITAL_TIPOS_OK = {"presion", "glicemia", "peso", "temperatura"}


@router.post("/portal/api/vitals")
async def portal_add_vital(request: Request,
                           portal_session: str | None = Cookie(None),
                           portal_active: str | None = Cookie(None)):
    """Añade un registro (presión, glicemia, peso, temperatura) al paciente activo."""
    _owner_rut, _owner_phone, rut, _active_phone = _resolve_context(portal_session, portal_active)
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
                             portal_session: str | None = Cookie(None),
                             portal_active: str | None = Cookie(None)):
    """Lista registros del paciente activo."""
    _owner_rut, _owner_phone, rut, _active_phone = _resolve_context(portal_session, portal_active)
    if tipo and tipo not in _VITAL_TIPOS_OK:
        raise HTTPException(status_code=400, detail="Tipo inválido")
    vitals = list_vitals(rut, tipo=tipo, dias=dias, limit=max(1, min(500, limit)))
    return {"ok": True, "vitals": vitals}


@router.get("/portal/api/perfil")
async def portal_get_perfil(portal_session: str | None = Cookie(None),
                            portal_active: str | None = Cookie(None)):
    """Devuelve los campos editables del perfil del paciente activo."""
    _owner_rut, _owner_phone, rut, phone = _resolve_context(portal_session, portal_active)
    if rut == DEMO_RUT:
        return {
            "ok": True, "demo": True,
            "profile": {
                "nombre": "María Ejemplo Demo",
                "fecha_nacimiento": "1975-06-15",
                "sexo": "F",
                "phone": DEMO_PHONE,
                "email": "demo@cmc.cl",
                "comuna": "Arauco",
                "direccion": "Calle Ficticia 123",
                "prevision": "Fonasa C",
                "contacto_emerg_nombre": "Juan Ejemplo",
                "contacto_emerg_telefono": "+56 9 8765 4321",
            },
        }
    if rut in DEMO_FAMILY:
        meta = DEMO_FAMILY[rut]
        return {"ok": True, "demo": True, "profile": {
            "nombre": meta["nombre"], "fecha_nacimiento": meta["fnac"],
            "sexo": meta["sexo"], "phone": DEMO_PHONE, "email": "", "comuna": "Arauco",
            "direccion": "Calle Ficticia 123", "prevision": "Fonasa B",
            "contacto_emerg_nombre": "María Ejemplo Demo",
            "contacto_emerg_telefono": "+56 9 8765 4321"}}
    prof = get_profile_full(phone)
    # El teléfono de la sesión siempre disponible para mostrar en "Mis datos"
    # (fix del campo que quedaba vacío para pacientes reales).
    if not prof.get("phone"):
        prof["phone"] = phone
    return {"ok": True, "profile": prof}


@router.post("/portal/api/perfil")
async def portal_update_perfil(request: Request,
                                portal_session: str | None = Cookie(None),
                                portal_active: str | None = Cookie(None)):
    """Actualiza campos editables del perfil del paciente activo."""
    _owner_rut, _owner_phone, rut, phone = _resolve_context(portal_session, portal_active)
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
    if rut == DEMO_RUT or rut in DEMO_FAMILY:
        return {"ok": True, "demo": True}  # no persistir demo
    update_profile_fields(phone, rut, data)
    return {"ok": True}


@router.delete("/portal/api/vitals/{vital_id}")
async def portal_delete_vital(vital_id: int,
                              portal_session: str | None = Cookie(None),
                              portal_active: str | None = Cookie(None)):
    """Elimina un registro del paciente activo."""
    _owner_rut, _owner_phone, rut, _active_phone = _resolve_context(portal_session, portal_active)
    ok = delete_vital(rut, vital_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Registro no encontrado")
    return {"ok": True}


# ═══ Familiares vinculados ═══════════════════════════════════════════════

def _normalize_rut(rut_raw: str) -> str:
    """Normaliza RUT a formato 'NNNNNNNN-K' (sin puntos, con guión)."""
    clean = (rut_raw or "").replace(".", "").replace("-", "").strip().upper()
    if len(clean) < 2:
        return ""
    return clean[:-1] + "-" + clean[-1]


def _age_years(fecha_nac: str) -> int | None:
    """Edad en años a partir de YYYY-MM-DD o DD/MM/YYYY. None si no parsea."""
    if not fecha_nac:
        return None
    from datetime import date
    s = fecha_nac.strip()
    parsed = None
    for sep, fmt in (("-", "%Y-%m-%d"), ("/", "%d/%m/%Y")):
        if sep in s:
            try:
                from datetime import datetime as _dt
                parsed = _dt.strptime(s[:10], fmt).date()
                break
            except ValueError:
                continue
    if not parsed:
        return None
    today = date.today()
    years = today.year - parsed.year - ((today.month, today.day) < (parsed.month, parsed.day))
    return years


@router.get("/portal/api/family")
async def portal_family_list(portal_session: str | None = Cookie(None)):
    """Lista los familiares vinculados al titular."""
    owner_rut, _owner_phone = _require_portal(portal_session)
    links = list_family_links(owner_rut)
    return {"ok": True, "owner_rut": owner_rut, "links": links}


@router.get("/portal/api/family/overview")
async def portal_family_overview(portal_session: str | None = Cookie(None)):
    """Vista familiar: titular + dependientes, cada uno con su próxima cita.
    Caso de uso central: quien agenda para toda la familia (típicamente la mamá)
    abre SU portal y ve de un vistazo las horas de todos, sin cambiar de perfil."""
    owner_rut, _owner_phone = _require_portal(portal_session)

    # Modo demo: familia ficticia completa (cada miembro navegable con switch)
    if owner_rut == DEMO_RUT:
        members = []
        d0 = _demo_data()
        members.append({"rut": DEMO_RUT, "nombre": d0["nombre"], "relation": "titular",
                        "sexo": d0["sexo"], "edad": _age_years(d0["fecha_nacimiento"]),
                        "dx": d0.get("diagnosticos") or [],
                        "ultima": ({"especialidad": d0["historial"][0]["especialidad"],
                                    "fecha": d0["historial"][0]["fecha"]} if d0["historial"] else None),
                        "n_at": len(d0["historial"]),
                        "proxima": d0["citas_futuras"][0] if d0["citas_futuras"] else None})
        for fr, meta in DEMO_FAMILY.items():
            dm = _demo_member_data(fr)
            members.append({"rut": fr, "nombre": meta["nombre"], "relation": meta["relation"],
                            "sexo": meta["sexo"], "edad": _age_years(meta["fnac"]),
                            "dx": dm.get("diagnosticos") or [],
                            "ultima": ({"especialidad": dm["historial"][0]["especialidad"],
                                        "fecha": dm["historial"][0]["fecha"]} if dm["historial"] else None),
                            "n_at": len(dm["historial"]),
                            "proxima": dm["citas_futuras"][0] if dm["citas_futuras"] else None})
        return {"ok": True, "demo": True, "members": members}

    import asyncio

    links = list_family_links(owner_rut)[:6]
    members_meta = [{"rut": owner_rut, "nombre": "", "relation": "titular"}] + [
        {"rut": l["dependent_rut"], "nombre": l.get("dependent_nombre") or "",
         "relation": l.get("relation") or "familiar"}
        for l in links
    ]

    async def fetch_member(m):
        # Fail-safe: cualquier error de Medilink degrada a "sin próxima cita",
        # nunca rompe la vista familiar completa.
        try:
            pac = await buscar_paciente(m["rut"])
            if not pac:
                return {**m, "proxima": None}
            rut_ml = pac.get("rut") or ""
            citas, hist = await asyncio.gather(
                listar_citas_paciente(pac["id"], rut=rut_ml),
                listar_historial_paciente(pac["id"], meses=12, rut=rut_ml),
            )
            out = {**m, "nombre": pac.get("nombre") or m["nombre"], "proxima": None,
                   "ultima": ({"especialidad": hist[0].get("especialidad", ""),
                               "fecha": hist[0].get("fecha", "")} if hist else None),
                   "n_at": len(hist or []),
                   "sexo": pac.get("sexo") or "", "edad": _age_years(pac.get("fecha_nacimiento") or "")}
            try:
                _ph = get_phone_by_rut(m["rut"])
                out["dx"] = get_dx_tags(_ph) if _ph else []
            except Exception:
                out["dx"] = []
            if citas:
                prox = citas[0]
                out["proxima"] = {"especialidad": prox.get("especialidad", ""),
                                  "fecha": prox.get("fecha", ""),
                                  "hora_inicio": prox.get("hora_inicio", ""),
                                  "profesional": prox.get("profesional", "")}
            return out
        except Exception:
            return {**m, "proxima": None}

    members = await asyncio.gather(*(fetch_member(m) for m in members_meta))
    return {"ok": True, "demo": False, "members": list(members)}


@router.post("/portal/api/family/switch")
async def portal_family_switch(request: Request,
                               portal_session: str | None = Cookie(None)):
    """Cambia el paciente activo. Body: {rut}. Setea cookie portal_active."""
    owner_rut, _owner_phone = _require_portal(portal_session)
    body = await request.json()
    target_raw = (body.get("rut") or "").strip()
    target = _normalize_rut(target_raw) if target_raw else owner_rut

    if target != owner_rut and target != DEMO_RUT:
        es_demo_fam = (owner_rut == DEMO_RUT and target in DEMO_FAMILY)
        if not es_demo_fam and not is_family_link(owner_rut, target):
            raise HTTPException(status_code=403, detail="Familiar no vinculado")

    is_https = (request.url.scheme == "https"
                or request.headers.get("x-forwarded-proto") == "https")
    response = JSONResponse({"ok": True, "active_rut": target})
    if target == owner_rut:
        response.delete_cookie(key=_ACTIVE_COOKIE_NAME, path="/")
    else:
        response.set_cookie(
            key=_ACTIVE_COOKIE_NAME,
            value=_sign_active_cookie(owner_rut, target),
            max_age=_COOKIE_MAX_AGE,
            httponly=True,
            samesite="lax",
            secure=is_https,
            path="/",
        )
    log_event(_owner_phone, "portal_family_switch", {"from": owner_rut, "to": target})
    return response


@router.post("/portal/api/family/add-minor")
async def portal_family_add_minor(request: Request,
                                  portal_session: str | None = Cookie(None)):
    """Vincula un familiar menor de edad mediante declaración de tutor.
    Body: {rut, relation, tutor_declaration: true}."""
    owner_rut, owner_phone = _require_portal(portal_session)
    body = await request.json()
    dep_raw = (body.get("rut") or "").strip()
    relation = (body.get("relation") or "").strip().lower() or "hijo"
    tutor_ok = bool(body.get("tutor_declaration"))

    if not tutor_ok:
        raise HTTPException(status_code=400, detail="Debes aceptar la declaración de tutor")
    if not dep_raw or not valid_rut(dep_raw):
        raise HTTPException(status_code=400, detail="RUT inválido")

    dep_rut = _normalize_rut(dep_raw)
    if dep_rut == owner_rut:
        raise HTTPException(status_code=400, detail="No puedes vincularte a ti mismo")
    if relation not in {"hijo", "hija", "tutelado", "tutelada", "nieto", "nieta"}:
        raise HTTPException(status_code=400, detail="Relación inválida para menor")

    paciente = await buscar_paciente(dep_raw)
    if not paciente:
        raise HTTPException(status_code=404, detail="Paciente no encontrado en Medilink")

    edad = _age_years(paciente.get("fecha_nacimiento", ""))
    if edad is None:
        raise HTTPException(status_code=400,
                            detail="El paciente no tiene fecha de nacimiento registrada. "
                                   "Para mayores usa la vinculación con código de verificación.")
    if edad >= 18:
        raise HTTPException(status_code=400,
                            detail=f"El paciente tiene {edad} años. "
                                   "Los mayores requieren verificación con código al WhatsApp del titular.")

    add_family_link(owner_rut, dep_rut, paciente.get("nombre", ""),
                    relation, "tutor_declaration")
    log_event(owner_phone, "portal_family_add_minor",
              {"owner": owner_rut, "dependent": dep_rut, "edad": edad})
    return {"ok": True, "dependent_rut": dep_rut, "nombre": paciente.get("nombre", ""), "edad": edad}


@router.post("/portal/api/family/request-otp")
async def portal_family_request_otp(request: Request,
                                    portal_session: str | None = Cookie(None)):
    """Envía OTP al WhatsApp del familiar adulto para autorizar la vinculación.
    Body: {rut}."""
    owner_rut, owner_phone = _require_portal(portal_session)
    body = await request.json()
    dep_raw = (body.get("rut") or "").strip()
    if not dep_raw or not valid_rut(dep_raw):
        raise HTTPException(status_code=400, detail="RUT inválido")

    dep_rut = _normalize_rut(dep_raw)
    if dep_rut == owner_rut:
        raise HTTPException(status_code=400, detail="No puedes vincularte a ti mismo")

    # Rate limit por RUT familiar
    if count_portal_otps(dep_rut) >= 3:
        raise HTTPException(status_code=429, detail="Demasiados intentos. Espere unos minutos.")

    paciente = await buscar_paciente(dep_raw)
    if not paciente:
        raise HTTPException(status_code=404, detail="Paciente no encontrado en Medilink")

    edad = _age_years(paciente.get("fecha_nacimiento", ""))
    if edad is not None and edad < 18:
        raise HTTPException(status_code=400,
                            detail="El paciente es menor de edad. Usa la opción 'Agregar menor'.")

    dep_phone = get_phone_by_rut(dep_rut)
    if not dep_phone:
        raise HTTPException(status_code=404,
                            detail="Este familiar no tiene WhatsApp registrado en el CMC. "
                                   "Pídele que escriba primero al +56 9 6661 0737.")

    code = f"{secrets.randbelow(1000000):06d}"
    save_portal_otp(dep_rut, dep_phone, code)

    await send_whatsapp(
        dep_phone,
        f"🔐 *Vinculación familiar — Portal del Paciente CMC*\n\n"
        f"Alguien está solicitando gestionar tus citas en el portal.\n\n"
        f"Código de autorización: *{code}*\n\n"
        f"Compártelo solo con quien confíes. Expira en 5 minutos.\n"
        f"Si no reconoces esta solicitud, ignora este mensaje."
    )
    log_event(owner_phone, "portal_family_request_otp",
              {"owner": owner_rut, "dependent": dep_rut})

    masked = dep_phone[:6] + "***" + dep_phone[-2:]
    return {"ok": True, "phone_masked": masked}


@router.post("/portal/api/family/verify-otp")
async def portal_family_verify_otp(request: Request,
                                   portal_session: str | None = Cookie(None)):
    """Verifica el OTP del familiar adulto y crea la vinculación.
    Body: {rut, code, relation}."""
    owner_rut, owner_phone = _require_portal(portal_session)
    body = await request.json()
    dep_raw = (body.get("rut") or "").strip()
    code = (body.get("code") or "").strip()
    relation = (body.get("relation") or "").strip().lower() or "familiar"

    if not dep_raw or not code:
        raise HTTPException(status_code=400, detail="RUT y código requeridos")
    if relation not in {"padre", "madre", "conyuge", "pareja", "hermano", "hermana",
                        "hijo", "hija", "abuelo", "abuela", "otro", "familiar",
                        "amigo", "amiga", "vecino", "vecina", "cuidador", "cuidadora"}:
        relation = "familiar"

    dep_rut = _normalize_rut(dep_raw)
    phone = verify_portal_otp(dep_rut, code)
    if not phone:
        raise HTTPException(status_code=401, detail="Código incorrecto o expirado")

    paciente = await buscar_paciente(dep_raw)
    nombre = paciente.get("nombre", "") if paciente else ""

    add_family_link(owner_rut, dep_rut, nombre, relation, "otp")
    log_event(owner_phone, "portal_family_add_adult",
              {"owner": owner_rut, "dependent": dep_rut, "relation": relation})
    return {"ok": True, "dependent_rut": dep_rut, "nombre": nombre}


@router.delete("/portal/api/family/{dependent_rut}")
async def portal_family_remove(dependent_rut: str,
                               portal_session: str | None = Cookie(None)):
    """Revoca una vinculación familiar."""
    owner_rut, owner_phone = _require_portal(portal_session)
    dep_rut = _normalize_rut(dependent_rut)
    ok = revoke_family_link(owner_rut, dep_rut)
    if not ok:
        raise HTTPException(status_code=404, detail="Vínculo no encontrado")
    log_event(owner_phone, "portal_family_revoke",
              {"owner": owner_rut, "dependent": dep_rut})
    return {"ok": True}


# ═══ Magic Link — /mis-citas ══════════════════════════════════════════════════
# Token firmado con HMAC-SHA256, payload: phone:exp, válido 24h.
# NO usa JWT para evitar dependencia externa. Mismo patrón que portal cookies.

_MAGIC_LINK_TTL = 86400  # 24h en segundos
_BOT_WA = "+56966610737"


def _magic_key() -> bytes:
    """Derivar clave dedicada para magic links."""
    secret = PORTAL_SESSION_SECRET or ADMIN_TOKEN
    return hashlib.sha256(f"cmc-magic-link:{secret}".encode()).digest()


def generar_magic_token(phone: str) -> str:
    """Genera token HMAC-SHA256 con payload phone:exp."""
    exp = int(time.time()) + _MAGIC_LINK_TTL
    payload = f"{phone}:{exp}"
    sig = hmac.new(_magic_key(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}:{sig}"


def verificar_magic_token(token: str) -> str | None:
    """Verifica token. Retorna phone si válido, None si no."""
    try:
        parts = token.rsplit(":", 1)
        if len(parts) != 2:
            return None
        payload, sig = parts
        expected = hmac.new(_magic_key(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        segs = payload.split(":")
        if len(segs) != 2:
            return None
        phone_tok, exp_str = segs
        if time.time() > int(exp_str):
            return None
        return phone_tok
    except Exception:
        return None


async def enviar_magic_link(phone: str) -> bool:
    """Genera token y envía link /mis-citas por WhatsApp al phone dado.

    Retorna True si el envío fue exitoso.
    """
    try:
        token = generar_magic_token(phone)
        import urllib.parse
        token_enc = urllib.parse.quote(token, safe="")
        url = f"https://agentecmc.cl/mis-citas?token={token_enc}"
        msg = (
            f"Aquí tienes el link para ver tus citas en el Centro Médico Carampangue:\n\n"
            f"{url}\n\n"
            f"El link es válido por 24 horas. Si necesitas ayuda escríbenos aquí."
        )
        await send_whatsapp(phone, msg)
        log_event(phone, "magic_link_enviado", {"url": url[:120]})
        return True
    except Exception as e:
        log.warning("enviar_magic_link falló phone=%s: %s", phone, e)
        return False


# ═══ Portal v5 — rediseño rural mobile-first ══════════════════════════════════
# La ruta vive aquí (y no en main.py) para no mezclar este cambio con trabajo
# en vuelo de otras sesiones sobre main.py. Usa el mismo _serve_portal de main
# (import perezoso: a la hora del request main ya está cargado).

from pathlib import Path as _Path
from fastapi.responses import HTMLResponse as _HTMLResponse

_TEMPLATES_DIR = _Path(__file__).resolve().parent.parent / "templates"
_PORTAL_V5_HTML = (
    (_TEMPLATES_DIR / "portal_v5.html").read_text(encoding="utf-8")
    if (_TEMPLATES_DIR / "portal_v5.html").exists() else ""
)


@router.get("/portal/v5", response_class=_HTMLResponse)
def portal_page_v5(request: Request, demo: str = ""):
    """Portal v5 — rediseño UX rural (tipografía 16px+, targets 48px, offline,
    usted, lenguaje claro). ?demo=1 -> modo demo sin clave (PORTAL_DEMO_OPEN)."""
    import sys
    # Reusar el módulo main ya cargado (app.main bajo uvicorn) en vez de
    # re-importarlo como "main": evita re-ejecutar side effects de módulo.
    main = sys.modules.get("app.main") or sys.modules.get("main")
    if main is None:  # pragma: no cover — sólo si se sirve sin app cargada
        import main  # noqa: F401
        main = sys.modules["main"]
    return main._serve_portal(
        _PORTAL_V5_HTML or main._PORTAL_V4_HTML or main._PORTAL_V3_HTML
        or main._PORTAL_V2_HTML or main._PORTAL_HTML,
        request, demo,
    )


# ═══ Magic link de LOGIN — alternativa al OTP (WCAG 3.3.8) ════════════════════
# El OTP transcrito es la barrera #1 para adultos mayores. Este link firmado
# entra directo: HMAC dedicado, TTL 30 min, mismo cookie de sesión que el OTP.
# (El magic link de /mis-citas es OTRO token, con otra clave y otro payload.)

_LOGIN_LINK_TTL = 1800  # 30 minutos

# Rate limit en memoria (mismo patrón sliding-window del agendador)
from collections import deque as _deque
import time as _time
_link_buckets: dict[str, "_deque"] = {}


def _link_rate_ok(key: str, limit: int, window_s: int) -> bool:
    now = _time.time()
    dq = _link_buckets.setdefault(key, _deque())
    while dq and dq[0] < now - window_s:
        dq.popleft()
    if len(dq) >= limit:
        return False
    dq.append(now)
    return True


def _login_key() -> bytes:
    secret = PORTAL_SESSION_SECRET or ADMIN_TOKEN
    return hashlib.sha256(f"cmc-portal-login:{secret}".encode()).digest()


def generar_login_token(rut: str, phone: str) -> str:
    exp = int(time.time()) + _LOGIN_LINK_TTL
    payload = f"{rut}|{phone}|{exp}"
    sig = hmac.new(_login_key(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}|{sig}"


def verificar_login_token(token: str) -> tuple[str, str] | None:
    """Retorna (rut, phone) si el token es válido y vigente; None si no."""
    try:
        parts = token.split("|")
        if len(parts) != 4:
            return None
        rut, phone, exp_str, sig = parts
        payload = f"{rut}|{phone}|{exp_str}"
        expected = hmac.new(_login_key(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        if time.time() > int(exp_str):
            return None
        return rut, phone
    except Exception:
        return None


@router.post("/portal/api/request-link")
async def portal_request_link(request: Request):
    """Envía por WhatsApp un link que entra directo al portal (sin código)."""
    body = await request.json()
    rut = (body.get("rut") or "").strip()

    if is_demo_rut(rut):
        return {"ok": True, "demo": True,
                "hint": f"En modo demo use el código {DEMO_CODE} (el link es solo para pacientes reales)."}

    if not rut or not valid_rut(rut):
        raise HTTPException(status_code=400, detail="RUT inválido")

    rut_clean = rut.replace(".", "").replace("-", "").strip().upper()
    rut_norm = rut_clean[:-1] + "-" + rut_clean[-1]

    ip = (request.headers.get("x-real-ip")
          or (request.client.host if request.client else "?"))
    if not _link_rate_ok(f"lnk-rut:{rut_norm}", 3, 3600) or not _link_rate_ok(f"lnk-ip:{ip}", 10, 3600):
        raise HTTPException(status_code=429, detail="Demasiados intentos. Espere unos minutos.")

    phone = get_phone_by_rut(rut_norm) or get_phone_by_rut(rut_clean)
    if not phone:
        pac = await buscar_paciente(rut)
        if pac:
            raise HTTPException(
                status_code=404,
                detail="Para activar su portal, escríbanos primero al WhatsApp: +56 9 6661 0737")
        raise HTTPException(status_code=404, detail="RUT no encontrado")

    import urllib.parse
    token = generar_login_token(rut_norm, phone)
    url = f"https://agentecmc.cl/portal/entrar?t={urllib.parse.quote(token, safe='')}"
    await send_whatsapp(
        phone,
        "🔐 Toque este link para entrar a su Portal del Paciente:\n\n"
        f"{url}\n\n"
        "Vence en 30 minutos. Si usted no lo pidió, ignore este mensaje.")
    log_event(phone, "portal_login_link_enviado", {"rut": rut_norm})
    rut_masked = rut_norm[:2] + ".***." + rut_norm[-3:]
    return {"ok": True, "rut_masked": rut_masked}


@router.get("/portal/entrar", response_class=_HTMLResponse)
def portal_entrar(request: Request, t: str = ""):
    """Login por magic link: valida el token y entra directo a /portal/v5."""
    from fastapi.responses import RedirectResponse
    res = verificar_login_token(t)
    if not res:
        return _HTMLResponse(
            """<!DOCTYPE html><html lang="es"><head><meta charset="UTF-8">
            <meta name="viewport" content="width=device-width,initial-scale=1">
            <title>Link vencido — CMC</title></head>
            <body style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#f3f6f9;
                         display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0">
            <div style="background:#fff;border-radius:16px;padding:30px 24px;max-width:380px;text-align:center;
                        box-shadow:0 6px 18px rgba(15,63,104,.08)">
              <div style="font-size:40px">⏰</div>
              <h2 style="color:#0F3F68;font-size:21px;margin:10px 0 6px">Este link ya venció</h2>
              <p style="color:#546776;font-size:16px;line-height:1.5;margin:0 0 18px">
                Por su seguridad, los links duran 30 minutos. Pida uno nuevo desde la página de entrada.</p>
              <a href="/portal/v5" style="display:block;background:#1172AB;color:#fff;border-radius:12px;
                 padding:15px;font-size:17px;font-weight:700;text-decoration:none">Ir a la página de entrada</a>
            </div></body></html>""",
            status_code=401)
    rut_norm, phone = res
    is_https = (request.url.scheme == "https"
                or request.headers.get("x-forwarded-proto") == "https")
    resp = RedirectResponse(url="/portal/v5", status_code=302)
    resp.set_cookie(
        key=_COOKIE_NAME,
        value=_sign_portal_cookie(rut_norm, phone),
        max_age=_COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=is_https,
        path="/",
    )
    resp.delete_cookie(key=_ACTIVE_COOKIE_NAME, path="/")
    log_event(phone, "portal_login_magic_link", {"rut": rut_norm})
    log.info("Portal login por magic link rut=%s", rut_norm)
    return resp


# ═══ Exámenes con semáforo (Fase 3) ═══════════════════════════════════════════
# Patrón basado en evidencia: barra de color + valor + palabra + conclusión en
# lenguaje simple. Hoy los resultados reales NO están integrados al portal
# (se entregan en el centro / WhatsApp): para pacientes reales la API es honesta
# (disponible=False) y la UI muestra ese estado. La demo trae 3 ejemplos para
# validar el diseño con pacientes.

def _demo_examenes() -> list[dict]:
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    hoy = datetime.now(ZoneInfo("America/Santiago")).date()
    f = (hoy - timedelta(days=12)).strftime("%Y-%m-%d")
    return [
        {"id": 1, "nombre": "Azúcar en la sangre (glicemia en ayunas)",
         "fecha": f, "valor": 118, "unidad": "mg/dL",
         "rango_min": 70, "rango_max": 100, "escala_min": 40, "escala_max": 200,
         "nivel": "atencion", "etiqueta": "Algo alta",
         "conclusion": "Su azúcar está un poco sobre lo normal (lo normal en ayunas es 70 a 100).",
         "que_hacer": "No es una urgencia. Muéstrele este resultado a su médico en el próximo control."},
        {"id": 2, "nombre": "Colesterol total",
         "fecha": f, "valor": 232, "unidad": "mg/dL",
         "rango_min": 0, "rango_max": 200, "escala_min": 100, "escala_max": 320,
         "nivel": "alto", "etiqueta": "Alto",
         "conclusion": "Su colesterol está sobre el nivel recomendado (menos de 200).",
         "que_hacer": "Pida una hora con su médico este mes para revisar este resultado."},
        {"id": 3, "nombre": "Hemoglobina (sangre)",
         "fecha": f, "valor": 13.8, "unidad": "g/dL",
         "rango_min": 12, "rango_max": 16, "escala_min": 8, "escala_max": 20,
         "nivel": "normal", "etiqueta": "Normal",
         "conclusion": "Su hemoglobina está dentro de lo normal (12 a 16). Sin señales de anemia.",
         "que_hacer": "Nada que hacer: siga con sus controles habituales."},
    ]


@router.get("/portal/api/examenes")
async def portal_examenes(portal_session: str | None = Cookie(None),
                          portal_active: str | None = Cookie(None)):
    """Resultados de exámenes del paciente activo (demo: ejemplos de diseño)."""
    _o, _op, rut, _phone = _resolve_context(portal_session, portal_active)
    if rut == DEMO_RUT:
        return {"ok": True, "demo": True, "disponible": True, "examenes": _demo_examenes()}
    # Pacientes reales: los resultados aún no están integrados al portal.
    return {"ok": True, "disponible": False, "examenes": []}


# ═══ Check-in del día ("voy en camino") ═══════════════════════════════════════
@router.post("/portal/api/checkin")
async def portal_checkin(request: Request,
                         portal_session: str | None = Cookie(None),
                         portal_active: str | None = Cookie(None)):
    """El paciente confirma que viene a su hora de HOY. Queda registrado como
    evento (visible en la línea de tiempo de recepción); no toca Medilink."""
    owner_rut, owner_phone, rut, _phone = _resolve_context(portal_session, portal_active)
    body = await request.json()
    if rut == DEMO_RUT or rut in DEMO_FAMILY:
        return {"ok": True, "demo": True}
    log_event(owner_phone, "portal_checkin_confirmado", {
        "rut": rut,
        "id_cita": str(body.get("id_cita") or ""),
        "especialidad": (body.get("especialidad") or "")[:60],
        "hora": (body.get("hora") or "")[:5],
    })
    return {"ok": True}


# ═══ Telemetría mínima del portal (embudo de uso) ═════════════════════════════
# El cliente manda eventos de un catálogo cerrado; quedan como log_event y se
# consultan con SQL sobre conversation_events (patrón "medir → decidir").
_EVENTOS_PORTAL = {
    "wiz_abre", "wiz_esp", "wiz_slot", "wiz_exito", "wiz_error",
    "cita_cambia_inicia", "cita_cambia_exito", "cita_anula",
    "checkin", "fz", "examenes_ver", "offline", "magic_link_pide",
}


@router.post("/portal/api/evento")
async def portal_evento(request: Request,
                        portal_session: str | None = Cookie(None),
                        portal_active: str | None = Cookie(None)):
    """Registra un evento de uso del portal (whitelist, payload acotado)."""
    owner_rut, owner_phone, rut, _phone = _resolve_context(portal_session, portal_active)
    if not _link_rate_ok(f"evt:{owner_phone}", 60, 300):
        return {"ok": True}  # silencioso: la telemetría jamás molesta al usuario
    try:
        body = await request.json()
    except Exception:
        return {"ok": True}
    e = str(body.get("e") or "")
    if e not in _EVENTOS_PORTAL:
        return {"ok": True}
    d = body.get("d") or {}
    datos = {"rut": rut, "demo": rut == DEMO_RUT or rut in DEMO_FAMILY}
    if isinstance(d, dict):
        for k, v in list(d.items())[:5]:
            if isinstance(v, (str, int, float, bool)):
                datos[str(k)[:24]] = str(v)[:80]
    log_event(owner_phone, f"portal_{e}", datos)
    return {"ok": True}
