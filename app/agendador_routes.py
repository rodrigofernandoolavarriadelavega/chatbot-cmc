"""
Agendador público online — cara pública premium del agendamiento del CMC.

Página pública SIN login en `/agendar` (servida desde main.py) + API JSON aquí.
Crea citas REALES en Medilink reutilizando el mismo motor que el bot y el módulo
de agenda de recepción (`agenda_routes`): slots en vivo, alta de paciente,
crear_cita, registro de origen para Pagos, CAPI Schedule y registro en citas_bot
(para que disparen los recordatorios automáticos del cron).

Gating: todo OFF por defecto (`AGENDADOR_PUBLICO_ENABLED`). Mientras está OFF se
puede previsualizar con `?preview=ADMIN_TOKEN`. Salvaguardas para uso público:
  - Validación de RUT chileno (módulo 11) y de teléfono móvil.
  - Rate-limit por IP y por RUT (en memoria, sliding window).
  - Bloqueo de duplicados per-RUT (mismo profesional + mismo día) — misma regla
    que el bot (ver fix 3a86983/d7dbfdd).
  - Re-chequeo de cupo en vivo antes de crear (reduce la carrera de "cupo tomado").
  - Consentimiento Ley 21.719 obligatorio, registrado en privacy_consents.

OTP por WhatsApp = fast-follow (requiere template de autenticación aprobado).
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from datetime import datetime, date

from fastapi import APIRouter, HTTPException, Request, Query
from fastapi.responses import JSONResponse

import config
from session import (
    save_privacy_consent, save_cita_bot, log_event,
)

import logging
log = logging.getLogger("agendador")

router = APIRouter(prefix="/agendar/api", tags=["agendador"])


# ── Gating ───────────────────────────────────────────────────────────────────
def _preview_ok(request: Request, preview: str | None) -> bool:
    tok = preview or request.query_params.get("preview") or request.headers.get("X-Preview-Token")
    return bool(tok) and tok == config.ADMIN_TOKEN


def _gate(request: Request, preview: str | None = None):
    """Permite el paso si el agendador está prendido o si hay token de preview."""
    if config.AGENDADOR_PUBLICO_ENABLED or _preview_ok(request, preview):
        return
    raise HTTPException(404, "No encontrado")


# ── Rate limiter en memoria (sliding window) ─────────────────────────────────
_buckets: dict[str, deque] = {}

def _rate_ok(key: str, limit: int, window_s: int) -> bool:
    now = time.time()
    dq = _buckets.get(key)
    if dq is None:
        dq = deque()
        _buckets[key] = dq
    while dq and dq[0] <= now - window_s:
        dq.popleft()
    if len(dq) >= limit:
        return False
    dq.append(now)
    return True

def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# ── Helpers de validación / normalización ────────────────────────────────────
def _clean_rut(rut: str) -> str:
    return (rut or "").replace(".", "").replace(" ", "").strip().upper()

def _norm_phone(tel: str) -> str | None:
    """Normaliza un teléfono chileno a 569XXXXXXXX (formato que usa el bot)."""
    d = "".join(c for c in (tel or "") if c.isdigit())
    if d.startswith("56"):
        d = d
    elif d.startswith("9") and len(d) == 9:
        d = "56" + d
    elif len(d) == 8:                 # 8 dígitos → asumimos móvil sin el 9
        d = "569" + d
    if len(d) == 11 and d.startswith("569"):
        return d
    return None

def _valid_fecha_futura(fecha: str) -> bool:
    try:
        f = datetime.strptime(fecha, "%Y-%m-%d").date()
    except ValueError:
        return False
    return f >= date.today()


# ── Catálogo: especialidades agrupadas + profesionales + precios reales ──────
# Orden y agrupamiento curado. Precio se deriva de PRECIOS_SLOT (fuente del bot).
_GRUPOS = [
    ("Médicas",              ["Medicina General", "Medicina Familiar", "Cardiología",
                              "Gastroenterología", "Otorrinolaringología"]),
    ("Salud de la mujer",    ["Ginecología", "Matrona"]),
    ("Terapias y bienestar", ["Kinesiología", "Nutrición", "Psicología Adulto",
                              "Fonoaudiología", "Podología", "Masoterapia"]),
    ("Imágenes",             ["Ecografía"]),
    ("Dentales",             ["Odontología General", "Ortodoncia", "Endodoncia",
                              "Implantología", "Estética Facial"]),
]
# Override de precio por profesional (cuando difiere del de la especialidad).
_PRECIO_PROF_OVERRIDE = {13: ("ambas", 7880, None, 30000)}  # Dr. Márquez particular $30.000

def _fmt_clp(n: int) -> str:
    return "$" + f"{int(n):,}".replace(",", ".")

def _precio_label(esp: str, id_prof: int | None = None) -> str:
    from flows import PRECIOS_SLOT
    entry = _PRECIO_PROF_OVERRIDE.get(id_prof) if id_prof else None
    entry = entry or PRECIOS_SLOT.get(esp)
    if not entry:
        return "Según evaluación"
    modo = entry[0]
    if modo == "ambas":
        _, fon, _x, part = entry
        return f"Fonasa {_fmt_clp(fon)} · Particular {_fmt_clp(part)}"
    if modo == "fonasa":
        return f"Fonasa {_fmt_clp(entry[1])}"
    # particular
    suf = entry[2] if len(entry) > 2 and entry[2] else None
    base = _fmt_clp(entry[1])
    if suf == "desde":
        return f"Desde {base}"
    if suf:
        return f"{base} ({suf})"
    return f"Particular {base}"

def _es_dental(esp: str) -> bool:
    return esp in ("Odontología General", "Ortodoncia", "Endodoncia",
                   "Implantología", "Estética Facial")

def _build_catalogo() -> list[dict]:
    from medilink import PROFESIONALES
    # especialidad -> [ (id, nombre) ... ]
    por_esp: dict[str, list] = {}
    for pid, info in PROFESIONALES.items():
        por_esp.setdefault(info["especialidad"], []).append((pid, info["nombre"], info.get("intervalo")))
    grupos = []
    for nombre_grupo, esps in _GRUPOS:
        items = []
        for esp in esps:
            profs = por_esp.get(esp)
            if not profs:
                continue
            items.append({
                "especialidad": esp,
                "precio": _precio_label(esp, profs[0][0] if len(profs) == 1 else None),
                "dental": _es_dental(esp),
                "metodos_pago": (["Efectivo", "Transferencia", "Débito", "Crédito"]
                                 if _es_dental(esp) else ["Efectivo", "Transferencia"]),
                "profesionales": [
                    {"id": pid, "nombre": nom,
                     "precio": _precio_label(esp, pid)}
                    for (pid, nom, _iv) in sorted(profs)
                ],
            })
        if items:
            grupos.append({"grupo": nombre_grupo, "especialidades": items})
    return grupos


# ── Descubrir primer día con cupo para un profesional ────────────────────────
async def _primer_dia_prof(id_prof: int, max_dias: int = 21) -> dict:
    from medilink import buscar_slots_dia_por_ids
    from datetime import timedelta
    hoy = date.today()
    for i in range(max_dias):
        f = hoy + timedelta(days=i)
        if f.weekday() == 6:          # domingo: cerrado
            continue
        fecha = f.isoformat()
        try:
            _smart, todos = await buscar_slots_dia_por_ids([id_prof], fecha)
        except Exception:
            todos = []
        if todos:
            return {"fecha": fecha, "slots": todos}
    return {"fecha": None, "slots": []}


# ════════════════════════════════════════════════════════════════════════════
# Endpoints
# ════════════════════════════════════════════════════════════════════════════

@router.get("/catalogo")
async def catalogo(request: Request, preview: str | None = Query(None)):
    _gate(request, preview)
    if not _rate_ok(f"cat:{_client_ip(request)}", 120, 60):
        raise HTTPException(429, "Demasiadas solicitudes")
    return {"grupos": _build_catalogo()}


@router.get("/slots")
async def slots(request: Request, id_prof: int = Query(...),
                fecha: str | None = Query(None), preview: str | None = Query(None)):
    """Slots de UN profesional. Sin `fecha` → primer día disponible."""
    _gate(request, preview)
    if not _rate_ok(f"slots:{_client_ip(request)}", 120, 60):
        raise HTTPException(429, "Demasiadas solicitudes")
    from medilink import PROFESIONALES, buscar_slots_dia_por_ids
    if id_prof not in PROFESIONALES:
        raise HTTPException(400, "Profesional no reconocido")
    if fecha is None:
        data = await _primer_dia_prof(id_prof)
        return {"id_prof": id_prof, "fecha": data["fecha"], "slots": data["slots"]}
    if not _valid_fecha_futura(fecha):
        raise HTTPException(400, "Fecha inválida o pasada")
    try:
        _smart, todos = await buscar_slots_dia_por_ids([id_prof], fecha)
    except Exception as e:
        log.error("slots prof=%s fecha=%s: %s", id_prof, fecha, e)
        raise HTTPException(503, "No pudimos consultar las horas. Intente nuevamente.")
    return {"id_prof": id_prof, "fecha": fecha, "slots": todos}


@router.post("/identificar")
async def identificar(request: Request, preview: str | None = Query(None)):
    """Busca al paciente por RUT en Medilink. No revela datos sensibles de más."""
    _gate(request, preview)
    ip = _client_ip(request)
    if not _rate_ok(f"ident:{ip}", 40, 300):
        raise HTTPException(429, "Demasiados intentos. Espere un momento.")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Body inválido")
    from medilink import valid_rut, buscar_paciente
    rut = _clean_rut(body.get("rut", ""))
    if not valid_rut(rut):
        raise HTTPException(400, "RUT inválido. Revise el dígito verificador.")
    try:
        pac = await buscar_paciente(rut)
    except Exception as e:
        log.error("identificar: %s", e)
        raise HTTPException(503, "No pudimos validar el RUT. Intente nuevamente.")
    if not pac:
        return {"encontrado": False, "rut": rut}
    nombre = pac.get("nombre", "")
    return {
        "encontrado": True,
        "id": pac.get("id"),
        "rut": rut,
        "nombre": nombre,
        "nombre_corto": (nombre or "").split(" ")[0].title(),
        "telefono_parcial": _mask_phone(pac.get("telefono", "") or pac.get("celular", "")),
    }


def _mask_phone(tel: str) -> str:
    d = "".join(c for c in (tel or "") if c.isdigit())
    if len(d) >= 4:
        return "•••• " + d[-4:]
    return ""


@router.post("/reservar")
async def reservar(request: Request, preview: str | None = Query(None)):
    """Crea la cita REAL en Medilink. Todas las salvaguardas viven acá."""
    _gate(request, preview)
    ip = _client_ip(request)
    if not _rate_ok(f"resv-ip:{ip}", 8, 3600):
        raise HTTPException(429, "Demasiadas reservas desde esta conexión. Intente más tarde.")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Body inválido")

    from medilink import (PROFESIONALES, valid_rut, buscar_paciente, crear_paciente,
                          crear_cita, listar_citas_paciente, buscar_slots_dia_por_ids)

    # 1) Consentimiento obligatorio (Ley 21.719)
    if not body.get("consent"):
        raise HTTPException(400, "Debe aceptar el tratamiento de sus datos para agendar.")

    # 2) Validaciones base
    rut = _clean_rut(body.get("rut", ""))
    if not valid_rut(rut):
        raise HTTPException(400, "RUT inválido.")
    phone = _norm_phone(body.get("telefono", ""))
    if not phone:
        raise HTTPException(400, "Ingrese un número de celular chileno válido.")
    if not _rate_ok(f"resv-rut:{rut}", 4, 86400):
        raise HTTPException(429, "Ya registró varias reservas hoy con este RUT. Contáctenos por WhatsApp.")

    try:
        id_profesional = int(body.get("id_profesional"))
    except (TypeError, ValueError):
        raise HTTPException(400, "Profesional inválido.")
    if id_profesional not in PROFESIONALES:
        raise HTTPException(400, "Profesional no reconocido.")
    prof = PROFESIONALES[id_profesional]
    fecha = (body.get("fecha") or "").strip()
    hora_inicio = (body.get("hora_inicio") or "").strip()[:5]
    hora_fin = (body.get("hora_fin") or "").strip()[:5]
    if not _valid_fecha_futura(fecha):
        raise HTTPException(400, "Fecha inválida o pasada.")
    try:
        datetime.strptime(hora_inicio, "%H:%M")
        datetime.strptime(hora_fin, "%H:%M")
    except ValueError:
        raise HTTPException(400, "Hora inválida.")

    es_tercero = bool(body.get("es_tercero"))

    # 3) Buscar / crear paciente
    try:
        pac = await buscar_paciente(rut)
    except Exception as e:
        log.error("reservar buscar_paciente: %s", e)
        raise HTTPException(503, "No pudimos validar el RUT. Intente nuevamente.")

    if pac:
        id_paciente = pac.get("id")
        paciente_nombre = pac.get("nombre", "")
    else:
        nombre = (body.get("nombre") or "").strip()
        apellidos = (body.get("apellidos") or "").strip()
        if not nombre:
            raise HTTPException(400, "Falta el nombre del paciente.")
        kwargs = {"telefono": phone, "celular": phone}
        for k in ("fecha_nacimiento", "sexo", "comuna", "email"):
            v = (body.get(k) or "").strip()
            if v:
                kwargs[k] = v
        try:
            pac = await crear_paciente(rut, nombre, apellidos, **kwargs)
        except Exception as e:
            log.error("reservar crear_paciente: %s", e)
            raise HTTPException(503, "No pudimos registrar al paciente. Intente nuevamente.")
        if not pac:
            raise HTTPException(502, "No pudimos registrar al paciente en el sistema.")
        id_paciente = pac.get("id")
        paciente_nombre = f"{nombre} {apellidos}".strip()

    if not id_paciente:
        raise HTTPException(502, "No se pudo obtener el paciente.")

    # 4) Bloqueo de duplicados per-RUT (mismo prof + mismo día) — misma regla del bot
    try:
        existentes = await listar_citas_paciente(id_paciente, rut=rut)
    except Exception:
        existentes = []
    dup = next((c for c in (existentes or [])
                if str(c.get("id_profesional", "")) == str(id_profesional)
                and c.get("fecha") == fecha), None)
    if dup:
        raise HTTPException(409, {
            "error": "duplicado",
            "mensaje": f"Ya tiene una hora con {prof['nombre']} ese día a las "
                       f"{(dup.get('hora_inicio','') or '')[:5]}. No agendamos dos horas con "
                       f"el mismo profesional el mismo día.",
        })

    # 5) Re-chequeo de cupo en vivo (reduce carrera)
    try:
        _smart, todos = await buscar_slots_dia_por_ids([id_profesional], fecha)
        libres = {s["hora_inicio"][:5] for s in (todos or [])}
    except Exception:
        libres = None
    if libres is not None and hora_inicio not in libres:
        raise HTTPException(409, {
            "error": "cupo_tomado",
            "mensaje": "Esa hora se acaba de ocupar. Por favor elija otra disponible.",
        })

    # 6) Crear la cita REAL
    try:
        resultado = await crear_cita(
            id_paciente=int(id_paciente), id_profesional=id_profesional,
            fecha=fecha, hora_inicio=hora_inicio, hora_fin=hora_fin,
        )
    except Exception as e:
        log.error("reservar crear_cita: %s", e)
        raise HTTPException(503, "No pudimos crear la cita en el sistema. Intente nuevamente.")
    if not resultado:
        raise HTTPException(502, "El sistema rechazó la cita. Es posible que la hora ya no esté disponible.")

    id_cita = resultado.get("id")
    esp = prof["especialidad"]

    # 7) Efectos secundarios (no deben tumbar la reserva ya creada)
    try:
        save_privacy_consent(phone, "accepted", "portal")
    except Exception as e:
        log.warning("reservar consent: %s", e)
    try:
        save_cita_bot(phone, str(id_cita), esp, prof["nombre"], fecha, hora_inicio,
                      "PRESENCIAL", paciente_nombre=paciente_nombre,
                      es_tercero=es_tercero, id_paciente_medilink=int(id_paciente))
    except Exception as e:
        log.warning("reservar save_cita_bot: %s", e)
    try:
        from pagos_routes import registrar_origen_cita
        registrar_origen_cita(str(id_cita), "web", "agendador")
    except Exception as e:
        log.warning("reservar origen: %s", e)
    # CAPI Schedule (fire-and-forget)
    asyncio.create_task(_capi_schedule(phone, rut, id_profesional, prof, fecha, hora_inicio, id_cita))
    # Confirmación inmediata por WhatsApp (gated; requiere template aprobado)
    if config.AGENDADOR_WA_CONFIRM:
        asyncio.create_task(_wa_confirm(phone, prof, esp, fecha, hora_inicio))

    log_event(phone, "agendador_web_reserva", {
        "id_cita": str(id_cita), "id_profesional": id_profesional,
        "especialidad": esp, "fecha": fecha, "hora": hora_inicio,
        "paciente_nuevo": not bool(pac.get("id") if pac else None) is False,
    })

    return {
        "ok": True,
        "id_cita": id_cita,
        "especialidad": esp,
        "profesional": prof["nombre"],
        "fecha": fecha,
        "hora_inicio": hora_inicio,
        "hora_fin": hora_fin,
        "nombre": paciente_nombre,
        "metodos_pago": (["Efectivo", "Transferencia", "Débito", "Crédito"]
                         if _es_dental(esp) else ["Efectivo", "Transferencia"]),
    }


@router.post("/mis-horas")
async def mis_horas(request: Request, preview: str | None = Query(None)):
    """Lista las próximas horas del paciente (por RUT)."""
    _gate(request, preview)
    if not _rate_ok(f"mish:{_client_ip(request)}", 40, 300):
        raise HTTPException(429, "Demasiados intentos. Espere un momento.")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Body inválido")
    from medilink import valid_rut, buscar_paciente, listar_citas_paciente
    rut = _clean_rut(body.get("rut", ""))
    if not valid_rut(rut):
        raise HTTPException(400, "RUT inválido.")
    try:
        pac = await buscar_paciente(rut)
    except Exception as e:
        log.error("mis_horas: %s", e)
        raise HTTPException(503, "No pudimos consultar sus horas. Intente nuevamente.")
    if not pac:
        return {"encontrado": False, "citas": []}
    try:
        citas = await listar_citas_paciente(pac.get("id"), rut=rut)
    except Exception:
        citas = []
    hoy = date.today().isoformat()
    futuras = [c for c in (citas or []) if (c.get("fecha") or "") >= hoy]
    futuras.sort(key=lambda c: (c.get("fecha", ""), c.get("hora_inicio", "")))
    return {
        "encontrado": True,
        "nombre_corto": (pac.get("nombre", "") or "").split(" ")[0].title(),
        "citas": [{
            "id_cita": c.get("id") or c.get("id_cita"),
            "especialidad": c.get("especialidad", ""),
            "profesional": c.get("profesional", ""),
            "fecha": c.get("fecha", ""),
            "hora_inicio": (c.get("hora_inicio", "") or "")[:5],
        } for c in futuras],
    }


@router.post("/cancelar")
async def cancelar(request: Request, preview: str | None = Query(None)):
    """Cancela una cita, verificando que pertenezca al RUT informado."""
    _gate(request, preview)
    ip = _client_ip(request)
    if not _rate_ok(f"canc:{ip}", 20, 3600):
        raise HTTPException(429, "Demasiadas solicitudes. Intente más tarde.")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Body inválido")
    from medilink import valid_rut, buscar_paciente, listar_citas_paciente, cancelar_cita
    rut = _clean_rut(body.get("rut", ""))
    id_cita = str(body.get("id_cita", "")).strip()
    if not valid_rut(rut) or not id_cita:
        raise HTTPException(400, "Datos inválidos.")
    try:
        pac = await buscar_paciente(rut)
        citas = await listar_citas_paciente(pac.get("id"), rut=rut) if pac else []
    except Exception as e:
        log.error("cancelar lookup: %s", e)
        raise HTTPException(503, "No pudimos procesar la cancelación.")
    ids = {str(c.get("id") or c.get("id_cita")) for c in (citas or [])}
    if id_cita not in ids:
        raise HTTPException(403, "Esa hora no corresponde al RUT indicado.")
    try:
        ok = await cancelar_cita(int(id_cita))
    except Exception as e:
        log.error("cancelar: %s", e)
        raise HTTPException(503, "No pudimos cancelar la hora. Intente nuevamente.")
    if not ok:
        raise HTTPException(502, "El sistema no pudo cancelar la hora.")
    log_event(_norm_phone(body.get("telefono", "")) or rut, "agendador_web_cancela", {"id_cita": id_cita})
    return {"ok": True, "id_cita": id_cita}


# ── Efectos secundarios ──────────────────────────────────────────────────────
async def _capi_schedule(phone, rut, id_profesional, prof, fecha, hora_inicio, id_cita):
    try:
        from meta_capi import send_event, _normalize_phone
        ph = _normalize_phone(phone) if phone else None
        if not ph:
            return
        await send_event(
            "Schedule", ph, rut=rut or None, value=0.0, currency="CLP",
            custom_data={
                "origen": "web", "especialidad": prof.get("especialidad", ""),
                "profesional": prof.get("nombre", ""), "fecha_cita": fecha,
                "hora_cita": hora_inicio, "id_cita": str(id_cita) if id_cita else "",
                "id_profesional": id_profesional,
                "content_name": prof.get("especialidad", ""),
            },
        )
    except Exception as e:
        log.warning("_capi_schedule agendador: %s", e)


async def _wa_confirm(phone, prof, esp, fecha, hora_inicio):
    """Confirmación inmediata por WhatsApp. Requiere template aprobado; gated."""
    try:
        from messaging import send_whatsapp_template
        # TODO: cablear al template real cuando esté aprobado en Meta.
        await send_whatsapp_template(phone, "recordatorio_cita", params=[
            prof.get("nombre", ""), f"{fecha} {hora_inicio}",
        ])
    except Exception as e:
        log.warning("_wa_confirm agendador: %s", e)
