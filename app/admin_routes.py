"""Admin panel API routes — all /admin/api/* endpoints."""
import asyncio
import logging
from datetime import date, timedelta
from collections import defaultdict

from fastapi import APIRouter, Request, Query, HTTPException, Header, Depends
from fastapi.responses import HTMLResponse

from config import ADMIN_TOKEN, ORTODONCIA_TOKEN
from messaging import send_whatsapp, send_instagram, send_messenger
from session import (get_session, reset_session, save_session, get_metricas,
                     log_message, get_messages, get_conversations, log_event,
                     get_tags, save_tag, delete_tag, search_messages,
                     get_kine_tracking_all, save_kine_tracking,
                     get_ortodoncia_pacientes, set_ortodoncia_tipo, get_ortodoncia_sync_max_fecha,
                     get_waitlist_all, cancel_waitlist,
                     get_confirmaciones_dia, get_citas_cache_todos)
from medilink import (buscar_paciente, crear_paciente, buscar_primer_dia,
                      buscar_slots_dia, crear_cita, listar_citas_paciente,
                      cancelar_cita, get_citas_seguimiento_mes, sync_citas_dia,
                      sync_ortodoncia_rango,
                      SEGUIMIENTO_ESPECIALIDADES, PROFESIONALES)

log = logging.getLogger("bot")

router = APIRouter(tags=["admin"])


# ── Auth helpers ─────────────────────────────────────────────────────────────

def _extract_token(query_token: str | None, auth_header: str | None) -> str:
    """Obtiene el token desde Authorization: Bearer ... o, como fallback,
    desde el query param ?token=... (para mantener compatibilidad con el panel HTML).
    """
    if auth_header and auth_header.lower().startswith("bearer "):
        return auth_header.split(None, 1)[1].strip()
    return query_token or ""


def require_admin(token: str | None = Query(None),
                  authorization: str | None = Header(None)) -> str:
    """Dependency FastAPI que valida token admin (header Bearer o query).
    Retorna el token validado para quien lo necesite."""
    tk = _extract_token(token, authorization)
    if tk != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Token inválido")
    return tk


def require_ortodoncia(token: str | None = Query(None),
                       authorization: str | None = Header(None)) -> str:
    """Dependency FastAPI que valida token de ortodoncia o admin."""
    tk = _extract_token(token, authorization)
    if tk not in (ORTODONCIA_TOKEN, ADMIN_TOKEN):
        raise HTTPException(status_code=403, detail="Acceso denegado")
    return tk


# ── Conversations & metrics ──────────────────────────────────────────────────

@router.get("/admin/api/conversations")
def admin_conversations(_: str = Depends(require_admin)):
    return get_conversations()


@router.get("/admin/api/conversations/{phone}")
def admin_conversation_detail(phone: str, _: str = Depends(require_admin)):
    return get_messages(phone)


@router.get("/admin/api/metrics")
def admin_metrics(_: str = Depends(require_admin)):
    return get_metricas(dias=30)


# ── Waitlist ─────────────────────────────────────────────────────────────────

@router.get("/admin/api/waitlist")
def admin_waitlist(_: str = Depends(require_admin)):
    """Lista de espera completa (activas + notificadas + canceladas)."""
    return get_waitlist_all()


@router.post("/admin/api/waitlist/{wl_id}/cancel")
def admin_waitlist_cancel(wl_id: int, _: str = Depends(require_admin)):
    """Marca una entrada de waitlist como cancelada (por recepción)."""
    cancel_waitlist(wl_id)
    return {"ok": True}


# ── Confirmaciones ───────────────────────────────────────────────────────────

@router.get("/admin/api/confirmaciones")
def admin_confirmaciones(fecha: str = None, _: str = Depends(require_admin)):
    """Estado de confirmación de las citas del bot para una fecha (default: mañana)."""
    if not fecha:
        fecha = (date.today() + timedelta(days=1)).isoformat()
    filas = get_confirmaciones_dia(fecha)
    resumen = {"confirmed": 0, "reagendar": 0, "cancelar": 0, "pendiente": 0}
    for f in filas:
        estado = f.get("confirmation_status") or "pendiente"
        if estado in resumen:
            resumen[estado] += 1
    return {"fecha": fecha, "total": len(filas), "resumen": resumen, "citas": filas}


# ── Takeover, reply, resume ──────────────────────────────────────────────────

@router.post("/admin/api/takeover/{phone}")
async def admin_takeover(phone: str, _: str = Depends(require_admin)):
    """Recepcionista toma control manual de una conversación."""
    save_session(phone, "HUMAN_TAKEOVER", {"hold_sent": True, "msgs_sin_respuesta": 0,
                                            "handoff_reason": "manual (recepcionista)"})
    log_event(phone, "derivado_humano", {"razon": "takeover manual desde panel"})
    await send_whatsapp(phone,
        "Hola 👋 Te está atendiendo una recepcionista del Centro Médico Carampangue.\n"
        "¿En qué te podemos ayudar?")
    log_message(phone, "out", "[Recepcionista tomó la conversación]", "HUMAN_TAKEOVER")
    return {"ok": True}


@router.post("/admin/api/reply")
async def admin_reply(request: Request, _: str = Depends(require_admin)):
    """Recepcionista envía un mensaje al paciente desde el panel (WhatsApp, Instagram o Messenger)."""
    body = await request.json()
    phone = body.get("phone", "").strip()
    message = body.get("message", "").strip()
    if not phone or not message:
        raise HTTPException(status_code=400, detail="phone y message son requeridos")

    if phone.startswith("ig_"):
        igsid = phone[3:]
        await send_instagram(igsid, message)
        canal = "instagram"
    elif phone.startswith("fb_"):
        psid = phone[3:]
        await send_messenger(psid, message)
        canal = "messenger"
    else:
        await send_whatsapp(phone, message)
        canal = "whatsapp"

    state = get_session(phone).get("state", "HUMAN_TAKEOVER")
    log_message(phone, "out", f"[Recepcionista] {message}", state, canal=canal)
    log_event(phone, "recepcionista_respondio", {"mensaje": message[:200]})
    return {"ok": True}


@router.post("/admin/api/resume/{phone}")
async def admin_resume(phone: str, _: str = Depends(require_admin)):
    """Devuelve el control al bot y notifica al paciente."""
    reset_session(phone)
    log_event(phone, "bot_reanudado")
    await send_whatsapp(phone,
        "Continuamos con el asistente automático 😊\n"
        "Escribe *menu* cuando quieras.")
    log_message(phone, "out", "[Bot reanudado por recepcionista]", "IDLE")
    return {"ok": True}


# ── Paciente & citas ─────────────────────────────────────────────────────────

@router.get("/admin/api/paciente")
async def admin_buscar_paciente(rut: str, _: str = Depends(require_admin)):
    """Busca un paciente en Medilink por RUT."""
    paciente = await buscar_paciente(rut)
    if not paciente:
        raise HTTPException(status_code=404, detail="Paciente no encontrado")
    return paciente


@router.get("/admin/api/slots")
async def admin_slots(especialidad: str, _: str = Depends(require_admin)):
    """Retorna la próxima fecha disponible y sus slots para una especialidad."""
    fecha = await buscar_primer_dia(especialidad)
    if not fecha:
        raise HTTPException(status_code=404, detail="Sin disponibilidad")
    slots, _ = await buscar_slots_dia(especialidad, fecha)
    # Incluir nombre del profesional en cada slot
    for s in slots:
        pid = s.get("id_profesional")
        s["profesional_nombre"] = PROFESIONALES.get(pid, {}).get("nombre", f"Prof. {pid}")
    return {"fecha": fecha, "slots": slots}


@router.post("/admin/api/agendar")
async def admin_agendar(request: Request, _: str = Depends(require_admin)):
    """Crea una cita desde el panel de recepción."""
    body = await request.json()
    rut        = body.get("rut", "").strip()
    nombre     = body.get("nombre", "").strip()
    apellidos  = body.get("apellidos", "").strip()
    id_prof    = int(body.get("id_profesional"))
    fecha      = body.get("fecha", "").strip()
    hora_ini   = body.get("hora_inicio", "").strip()
    hora_fin   = body.get("hora_fin", "").strip()
    duracion   = int(body.get("duracion", 30))

    # Buscar o crear paciente
    paciente = await buscar_paciente(rut)
    if not paciente:
        paciente = await crear_paciente(rut, nombre, apellidos)
        if not paciente:
            raise HTTPException(status_code=400, detail="No se pudo crear el paciente")

    cita = await crear_cita(paciente["id"], id_prof, fecha, hora_ini, hora_fin, duracion)
    if not cita:
        raise HTTPException(status_code=400, detail="No se pudo crear la cita en Medilink")

    log_event("admin", "cita_creada_panel", {
        "rut": rut, "id_profesional": id_prof,
        "fecha": fecha, "hora": hora_ini
    })
    return {"ok": True, "cita": cita}


@router.get("/admin/api/especialidades")
def admin_especialidades(_: str = Depends(require_admin)):
    """Retorna la lista de especialidades únicas disponibles."""
    esp = sorted({v["especialidad"] for v in PROFESIONALES.values()})
    return {"especialidades": esp}


# ── Tags ─────────────────────────────────────────────────────────────────────

@router.get("/admin/api/tags/{phone}")
def admin_get_tags(phone: str, _: str = Depends(require_admin)):
    return {"tags": get_tags(phone)}


@router.post("/admin/api/tags/{phone}")
async def admin_add_tag(phone: str, request: Request, _: str = Depends(require_admin)):
    body = await request.json()
    tag = body.get("tag", "").strip()
    if not tag:
        raise HTTPException(status_code=400, detail="tag requerido")
    save_tag(phone, tag)
    return {"tags": get_tags(phone)}


@router.delete("/admin/api/tags/{phone}/{tag}")
def admin_delete_tag(phone: str, tag: str, _: str = Depends(require_admin)):
    delete_tag(phone, tag)
    return {"tags": get_tags(phone)}


# ── Search ───────────────────────────────────────────────────────────────────

@router.get("/admin/api/search")
def admin_search_messages(q: str, _: str = Depends(require_admin)):
    """Busca texto en todos los mensajes de todas las conversaciones."""
    if len(q.strip()) < 2:
        raise HTTPException(status_code=400, detail="Mínimo 2 caracteres")
    results = search_messages(q.strip())
    return {"q": q, "results": results}


# ── Citas paciente & anular ──────────────────────────────────────────────────

@router.get("/admin/api/citas-paciente")
async def admin_citas_paciente(rut: str, _: str = Depends(require_admin)):
    """Retorna las citas futuras de un paciente buscado por RUT."""
    paciente = await buscar_paciente(rut)
    if not paciente:
        raise HTTPException(status_code=404, detail="Paciente no encontrado")
    citas = await listar_citas_paciente(paciente["id"])
    return {"paciente": paciente, "citas": citas}


@router.post("/admin/api/anular")
async def admin_anular_cita(request: Request, _: str = Depends(require_admin)):
    """Anula una cita por su ID de Medilink."""
    body = await request.json()
    id_cita = int(body.get("id_cita"))
    ok = await cancelar_cita(id_cita)
    if not ok:
        raise HTTPException(status_code=400, detail="No se pudo anular la cita en Medilink")
    log_event("admin", "cita_anulada_panel", {"id_cita": id_cita})
    return {"ok": True}


# ── Kinesiología / Pacientes en Control ──────────────────────────────────────

@router.get("/admin/api/kine")
async def admin_kine(mes: str = None, especialidad: str = "kinesiologia",
                     _: str = Depends(require_admin)):
    """Retorna citas de una especialidad recurrente.
    mes=YYYY-MM → mes específico | mes=YYYY → año completo | mes=todos → todo el histórico"""
    import calendar as cal_mod

    cfg = SEGUIMIENTO_ESPECIALIDADES.get(especialidad, {})
    tracking = {(t["id_paciente"], t["id_prof"]): t for t in get_kine_tracking_all()}

    def _enrich(citas):
        for p in citas:
            t = tracking.get((p["id_paciente"], p["id_prof"]), {})
            p["total_sesiones"]    = t.get("total_sesiones", 0)
            p["modalidad"]         = t.get("modalidad", "fonasa")
            p["notas"]             = t.get("notas", "")
            p["precio_fonasa"]     = cfg.get("precio_fonasa")
            p["precio_particular"] = cfg.get("precio_particular")
        return citas

    # Modo "todos" — histórico completo desde caché
    if mes == "todos":
        ids_prof = cfg.get("ids", [])
        raw = get_citas_cache_todos(ids_prof)
        grupos: dict = defaultdict(list)
        for c in raw:
            key = (c["id_paciente"], c["id_prof"])
            grupos[key].append(c)
        citas = []
        for (id_pac, id_prof), items in grupos.items():
            items_sorted = sorted(items, key=lambda x: x["fecha"])
            citas.append({
                "id_paciente":     id_pac,
                "id_prof":         id_prof,
                "prof_nombre":     cfg.get("ids") and PROFESIONALES.get(id_prof, {}).get("nombre", ""),
                "paciente_nombre": items_sorted[0]["paciente_nombre"],
                "sesiones_mes":    len(items_sorted),
                "fechas":          [c["fecha"] for c in items_sorted],
                "primera_fecha":   items_sorted[0]["fecha"],
                "ultima_fecha":    items_sorted[-1]["fecha"],
            })
        citas = sorted(citas, key=lambda x: x["primera_fecha"])
        return {"mode": "todos", "especialidad": especialidad,
                "especialidad_label": cfg.get("label", especialidad),
                "pacientes": _enrich(citas)}

    # Modo "año" — YYYY sin mes
    if mes and len(mes) == 4 and mes.isdigit():
        year = int(mes)
        all_citas = []
        for month in range(1, 13):
            mc = await get_citas_seguimiento_mes(year, month, especialidad)
            all_citas.extend(mc)
        # Reagrupar por paciente+prof sumando sesiones
        grupos: dict = defaultdict(list)
        for c in all_citas:
            key = (c["id_paciente"], c["id_prof"])
            grupos[key].append(c)
        citas = []
        for (id_pac, id_prof), items in grupos.items():
            fechas = sorted({f for i in items for f in i.get("fechas", [i.get("primera_fecha","")])})
            citas.append({
                "id_paciente":     id_pac, "id_prof": id_prof,
                "prof_nombre":     items[0].get("prof_nombre",""),
                "paciente_nombre": items[0]["paciente_nombre"],
                "sesiones_mes":    len(fechas),
                "fechas":          fechas,
                "primera_fecha":   fechas[0] if fechas else "",
                "ultima_fecha":    fechas[-1] if fechas else "",
            })
        citas = sorted(citas, key=lambda x: x["primera_fecha"])
        return {"mode": "anio", "year": year, "especialidad": especialidad,
                "especialidad_label": cfg.get("label", especialidad),
                "pacientes": _enrich(citas)}

    # Modo mes (default)
    if mes:
        try:
            year, month = int(mes.split("-")[0]), int(mes.split("-")[1])
        except (ValueError, IndexError):
            raise HTTPException(status_code=400, detail="mes debe ser YYYY-MM, YYYY, o 'todos'")
    else:
        hoy = date.today()
        year, month = hoy.year, hoy.month
    citas = await get_citas_seguimiento_mes(year, month, especialidad)
    return {"year": year, "month": month, "mode": "mes", "especialidad": especialidad,
            "especialidad_label": cfg.get("label", especialidad), "pacientes": _enrich(citas)}


@router.get("/admin/api/kine/especialidades")
def admin_kine_especialidades(_: str = Depends(require_admin)):
    return {"especialidades": [
        {"id": k, "label": v["label"]} for k, v in SEGUIMIENTO_ESPECIALIDADES.items()
    ]}


@router.post("/admin/api/kine/sync")
async def admin_kine_sync(fecha: str = None, _: str = Depends(require_admin)):
    """Fuerza sincronización del caché de citas para una fecha (default: hoy)."""
    if not fecha:
        fecha = date.today().strftime("%Y-%m-%d")
    ids_todos = list({i for cfg in SEGUIMIENTO_ESPECIALIDADES.values() for i in cfg["ids"]})
    await sync_citas_dia(fecha, ids_todos)
    return {"ok": True, "fecha": fecha, "ids": ids_todos}


@router.put("/admin/api/kine/{id_paciente}/{id_prof}")
async def admin_kine_update(id_paciente: int, id_prof: int, request: Request,
                            _: str = Depends(require_admin)):
    """Actualiza el tracking de sesiones de un paciente en control."""
    body = await request.json()
    save_kine_tracking(
        id_paciente, id_prof,
        int(body.get("total_sesiones", 0)),
        body.get("modalidad", "fonasa"),
        body.get("notas", ""),
    )
    return {"ok": True}


# ── Ortodoncia ───────────────────────────────────────────────────────────────

@router.get("/admin/api/ortodoncia")
def admin_ortodoncia_pacientes(_: str = Depends(require_ortodoncia)):
    pacientes = get_ortodoncia_pacientes()
    ultima_sync = get_ortodoncia_sync_max_fecha()
    return {"pacientes": pacientes, "ultima_sync": ultima_sync}


@router.put("/admin/api/ortodoncia/{id_atencion}")
async def admin_ortodoncia_tipo(id_atencion: int, request: Request,
                                _: str = Depends(require_ortodoncia)):
    body = await request.json()
    tipo = body.get("tipo")
    if tipo not in ("instalacion", "control", "pendiente"):
        raise HTTPException(status_code=400, detail="tipo debe ser instalacion, control o pendiente")
    set_ortodoncia_tipo(id_atencion, tipo)
    return {"ok": True}


@router.post("/admin/api/ortodoncia/sync")
async def admin_ortodoncia_sync(desde: str = "2025-01-01", hasta: str = None,
                                _: str = Depends(require_ortodoncia)):
    fin = hasta or date.today().isoformat()
    asyncio.create_task(sync_ortodoncia_rango(desde, fin))
    return {"ok": True, "desde": desde, "hasta": fin}
