"""
Router /alma/api/agenda — Módulo nativo de Agenda en la plataforma Alma.

Flujo 1: Ver agenda del día → lee citas_cache LOCAL (un profesional + fecha).
         NO hace fan-out de todos los profesionales. Una query SQLite.

Flujo 2: Slots disponibles → buscar_slots_dia_por_ids en vivo a Medilink
         (un solo profesional a la vez). Retorna lista de slots.

Flujo 3: Buscar paciente por RUT → buscar_paciente wrapper existente.

Flujo 4: Crear cita → confirmación explícita en el frontend; backend solo
         crea cuando se llama el endpoint POST /alma/api/agenda/citas.

Auth: require_admin de admin_routes (Bearer / cookie / query token).
"""
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request, Query, Cookie
from fastapi.responses import JSONResponse

log = logging.getLogger("agenda_routes")
_CHILE_TZ = ZoneInfo("America/Santiago")

router = APIRouter(prefix="/alma/api/agenda", tags=["agenda"])


def _require_admin_dep(request: Request,
                       token: str | None = Query(None),
                       authorization: str | None = None,
                       cmc_session: str | None = Cookie(None)) -> str:
    """Inline auth compatible con require_admin de admin_routes."""
    import hmac as _hmac
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


# ── Flujo 1: Agenda del día desde cache local ─────────────────────────────────

@router.get("/dia")
async def get_agenda_dia(
    id_prof: int = Query(..., description="ID del profesional"),
    fecha: str = Query(..., description="Fecha YYYY-MM-DD"),
    token: str | None = Query(None),
    cmc_session: str | None = Cookie(None),
    request: Request = None,
):
    """
    Retorna las citas del dia para un profesional desde citas_cache (SQLite local).
    NO consulta Medilink — evita 429. Solo un profesional por request.
    """
    _require_admin_dep(request, token=token, cmc_session=cmc_session)

    from medilink import PROFESIONALES
    if id_prof not in PROFESIONALES:
        raise HTTPException(400, f"Profesional {id_prof} no reconocido")

    # Validar formato fecha
    try:
        datetime.strptime(fecha, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(400, "Fecha debe ser YYYY-MM-DD")

    from session import get_citas_cache_dia
    citas = get_citas_cache_dia(id_prof, fecha)

    prof_info = PROFESIONALES[id_prof]
    return {
        "profesional": {
            "id": id_prof,
            "nombre": prof_info["nombre"],
            "especialidad": prof_info["especialidad"],
            "intervalo": prof_info["intervalo"],
        },
        "fecha": fecha,
        "citas": citas,
        "total": len(citas),
        "fuente": "cache",
    }


# ── Flujo 2a: Slots disponibles (en vivo, UN profesional) ────────────────────

@router.get("/slots")
async def get_slots_disponibles(
    id_prof: int = Query(..., description="ID del profesional"),
    fecha: str = Query(..., description="Fecha YYYY-MM-DD"),
    token: str | None = Query(None),
    cmc_session: str | None = Cookie(None),
    request: Request = None,
):
    """
    Retorna slots disponibles para UN profesional en una fecha, en vivo a Medilink.
    Usa buscar_slots_dia_por_ids con intervalo del dict PROFESIONALES.
    """
    _require_admin_dep(request, token=token, cmc_session=cmc_session)

    from medilink import PROFESIONALES, buscar_slots_dia_por_ids
    if id_prof not in PROFESIONALES:
        raise HTTPException(400, f"Profesional {id_prof} no reconocido")

    try:
        datetime.strptime(fecha, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(400, "Fecha debe ser YYYY-MM-DD")

    try:
        smart_5, todos = await buscar_slots_dia_por_ids([id_prof], fecha)
    except Exception as e:
        log.error("get_slots_disponibles prof=%d fecha=%s: %s", id_prof, fecha, e)
        raise HTTPException(503, "Error consultando Medilink")

    return {
        "profesional_id": id_prof,
        "fecha": fecha,
        "slots": todos,
        "smart": smart_5,
        "total": len(todos),
    }


# ── Flujo 2b: Buscar paciente por RUT ────────────────────────────────────────

@router.get("/paciente")
async def get_paciente_rut(
    rut: str = Query(..., description="RUT sin puntos, con guion"),
    token: str | None = Query(None),
    cmc_session: str | None = Cookie(None),
    request: Request = None,
):
    """Busca un paciente en Medilink por RUT. Retorna datos del paciente o 404."""
    _require_admin_dep(request, token=token, cmc_session=cmc_session)

    if not rut or len(rut) < 3:
        raise HTTPException(400, "RUT inválido")

    from medilink import buscar_paciente
    try:
        paciente = await buscar_paciente(rut.strip())
    except Exception as e:
        log.error("get_paciente_rut rut=*** : %s", e)
        raise HTTPException(503, "Error consultando Medilink")

    if not paciente:
        raise HTTPException(404, "Paciente no encontrado")

    return {
        "id": paciente.get("id"),
        "nombre": paciente.get("nombre", ""),
        "apellidos": paciente.get("apellidos", ""),
        "nombre_completo": f"{paciente.get('nombre', '')} {paciente.get('apellidos', '')}".strip(),
        "rut": paciente.get("rut", ""),
        "email": paciente.get("email", ""),
        "telefono": paciente.get("telefono", "") or paciente.get("celular", ""),
    }


# ── Flujo 2c: Crear cita (requiere confirmación previa en frontend) ───────────

@router.post("/citas")
async def post_crear_cita(
    request: Request,
    token: str | None = Query(None),
    cmc_session: str | None = Cookie(None),
):
    """
    Crea una cita real en Medilink. El frontend DEBE mostrar confirmación
    explícita antes de llamar este endpoint.

    Body JSON:
      id_paciente: int
      id_profesional: int
      fecha: str  (YYYY-MM-DD)
      hora_inicio: str  (HH:MM)
      hora_fin: str  (HH:MM)
      confirmado: bool  (debe ser true — campo de seguridad anti-accidental)
    """
    _require_admin_dep(request, token=token, cmc_session=cmc_session)

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Body JSON inválido")

    # Campo de seguridad — el frontend debe enviar confirmado=true
    if not body.get("confirmado"):
        raise HTTPException(400, "Se requiere confirmado=true para crear la cita")

    id_paciente = body.get("id_paciente")
    id_profesional = body.get("id_profesional")
    fecha = body.get("fecha")
    hora_inicio = body.get("hora_inicio")
    hora_fin = body.get("hora_fin")

    # Validaciones mínimas
    if not all([id_paciente, id_profesional, fecha, hora_inicio, hora_fin]):
        raise HTTPException(400, "Faltan campos: id_paciente, id_profesional, fecha, hora_inicio, hora_fin")

    from medilink import PROFESIONALES, crear_cita
    if id_profesional not in PROFESIONALES:
        raise HTTPException(400, f"Profesional {id_profesional} no reconocido")

    try:
        datetime.strptime(fecha, "%Y-%m-%d")
        datetime.strptime(hora_inicio, "%H:%M")
        datetime.strptime(hora_fin, "%H:%M")
    except ValueError as e:
        raise HTTPException(400, f"Formato de fecha/hora inválido: {e}")

    try:
        resultado = await crear_cita(
            id_paciente=int(id_paciente),
            id_profesional=int(id_profesional),
            fecha=fecha,
            hora_inicio=hora_inicio,
            hora_fin=hora_fin,
        )
    except Exception as e:
        log.error("post_crear_cita: %s", e)
        raise HTTPException(503, "Error al crear cita en Medilink")

    if not resultado:
        raise HTTPException(502, "Medilink rechazó la cita — verifica horario y disponibilidad")

    prof_info = PROFESIONALES[id_profesional]
    log.info(
        "agenda_routes.crear_cita: prof=%d (%s) fecha=%s %s-%s paciente=%d → id=%s",
        id_profesional, prof_info["nombre"], fecha, hora_inicio, hora_fin,
        id_paciente, resultado.get("id"),
    )

    return {
        "ok": True,
        "id_cita": resultado.get("id"),
        "profesional": prof_info["nombre"],
        "fecha": fecha,
        "hora_inicio": hora_inicio,
        "hora_fin": hora_fin,
    }


# ── Utilidades: lista de profesionales para el selector ──────────────────────

@router.get("/profesionales")
async def get_profesionales_lista(
    token: str | None = Query(None),
    cmc_session: str | None = Cookie(None),
    request: Request = None,
):
    """Lista todos los profesionales habilitados (para el selector de la UI)."""
    _require_admin_dep(request, token=token, cmc_session=cmc_session)

    from medilink import PROFESIONALES
    profs = [
        {
            "id": id_p,
            "nombre": info["nombre"],
            "especialidad": info["especialidad"],
            "intervalo": info["intervalo"],
        }
        for id_p, info in sorted(PROFESIONALES.items(), key=lambda x: x[1]["nombre"])
    ]
    return {"profesionales": profs}
