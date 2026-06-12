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
    # nginx setea X-Real-IP = $remote_addr (lo SOBRESCRIBE, el cliente no puede
    # falsificarlo por la vía del proxy). NUNCA usar el primer valor de
    # X-Forwarded-For: con `$proxy_add_x_forwarded_for` el primero lo controla el
    # cliente y el hop confiable es el ÚLTIMO (la IP real que agregó nginx).
    xri = (request.headers.get("x-real-ip") or "").strip()
    if xri:
        return xri
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[-1].strip()
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
                              "Psicología Infantil", "Fonoaudiología", "Podología",
                              "Masoterapia"]),
    ("Imágenes",             ["Ecografía"]),
    ("Dentales",             ["Odontología General", "Ortodoncia", "Endodoncia",
                              "Implantología", "Estética Facial"]),
]
# Override de precio por profesional (cuando difiere del de la especialidad).
_PRECIO_PROF_OVERRIDE = {13: ("ambas", 7880, None, 30000)}  # Dr. Márquez particular $30.000

# Especialidades que NO tienen profesional propio en PROFESIONALES pero SÍ se
# atienden (el mismo profesional figura bajo otra etiqueta). Pedido dueño
# 2026-06-12: Márquez en Medicina General Y Medicina Familiar; Montalba en
# Psicología Adulto Y Psicología Infantil. PRECIOS_SLOT ya tiene ambas claves.
# Etiqueta de precio especial cuando una especialidad agrupa servicios con
# valores distintos (fuente: glosario de precios del bot en claude_helper.py).
_PRECIO_ESP_LABEL = {
    "Ginecología": "Consulta $30.000 · Eco ginecológica $35.000",
}

# Prestaciones con valor propio, mostradas al desplegar la especialidad
# (pedido dueño 2026-06-12 para Fonoaudiología). Fuente: glosario del bot.
_PRESTACIONES: dict[str, list[dict]] = {
    "Fonoaudiología": [
        {"n": "Evaluación infantil/adulto", "p": "$30.000"},
        {"n": "Sesión de terapia", "p": "$25.000"},
        {"n": "Audiometría", "p": "$25.000"},
        {"n": "Audiometría + impedanciometría", "p": "$45.000"},
        {"n": "Impedanciometría", "p": "$20.000"},
        {"n": "Octavo par (audición y equilibrio)", "p": "$50.000"},
        {"n": "Evaluación + maniobra VPPB (vértigo)", "p": "$50.000"},
        {"n": "Terapia vestibular", "p": "$25.000"},
        {"n": "Terapia tinnitus (zumbido)", "p": "$25.000"},
        {"n": "Calibración de audífonos", "p": "$10.000"},
        {"n": "Revisión de exámenes", "p": "$10.000"},
    ],
    "Ginecología": [
        {"n": "Consulta ginecológica", "p": "$30.000"},
        {"n": "Ecografía ginecológica / transvaginal / pélvica", "p": "$35.000"},
        {"n": "PAP", "p": "$20.000"},
    ],
    "Matrona": [
        {"n": "Consulta Fonasa preferencial", "p": "$16.000"},
        {"n": "Consulta particular", "p": "$20.000"},
        {"n": "Consulta + PAP (Fonasa preferencial)", "p": "$25.000"},
        {"n": "Consulta + PAP (particular)", "p": "$30.000"},
        {"n": "PAP / Papanicolau solo", "p": "$20.000"},
        {"n": "Revisión de exámenes", "p": "$10.000"},
    ],
    "Estética Facial": [
        {"n": "Evaluación / armonización (plan personalizado)", "p": "$15.000"},
        {"n": "Toxina botulínica (3 zonas)", "p": "$159.990"},
        {"n": "Ácido hialurónico (labios, pómulos, ojeras)", "p": "$159.990"},
        {"n": "Mesoterapia facial (1 sesión)", "p": "$80.000"},
        {"n": "Mesoterapia facial (3 sesiones)", "p": "$179.990"},
        {"n": "Hilos tensores", "p": "$129.990"},
        {"n": "Lipopapada (3 sesiones)", "p": "$139.990"},
        {"n": "Exosomas (regeneración)", "p": "$349.900"},
        {"n": "Bioestimulador de colágeno", "p": "$450.000"},
    ],
    "Nutrición": [
        {"n": "Consulta nutricional", "p": "Fonasa $4.770 · Particular $20.000"},
        {"n": "Bioimpedanciometría (composición corporal)", "p": "$20.000"},
    ],
    "Odontología General": [
        {"n": "Evaluación dental (diagnóstico + plan)", "p": "$15.000"},
        {"n": "Restauración de resina (tapadura)", "p": "desde $35.000"},
        {"n": "Destartraje + profilaxis (limpieza)", "p": "$30.000"},
        {"n": "Exodoncia simple", "p": "$40.000"},
        {"n": "Exodoncia compleja", "p": "$60.000"},
        {"n": "Blanqueamiento dental", "p": "$75.000"},
        {"n": "Carillas de resina", "p": "desde $50.000"},
    ],
    "Ortodoncia": [
        {"n": "Evaluación dental previa (gratis si inicias tratamiento ese día)", "p": "$15.000"},
        {"n": "Instalación brackets boca completa", "p": "$120.000"},
        {"n": "Instalación brackets 1 arcada", "p": "$60.000"},
        {"n": "Control mensual ortodoncia", "p": "$30.000"},
        {"n": "Control ortopedia (niños/adolescentes)", "p": "$20.000"},
        {"n": "Retiro brackets + contención", "p": "$120.000"},
    ],
}

# Especialidades que NO se agendan directo: el flujo clínico parte por otra
# (ortodoncia: evaluación con dentista general que luego deriva — regla CMC).
_AGENDAR_VIA = {
    "Ortodoncia": {
        "via": "Odontología General",
        "nota": ("El tratamiento de ortodoncia parte con una evaluación dental "
                 "con nuestro equipo de Odontología General ($15.000 — gratis si "
                 "inicias un tratamiento ese mismo día). La dentista evalúa tu caso, "
                 "solicita radiografías y gestiona tu derivación con la ortodoncista "
                 "Dra. Daniela Castillo."),
    },
}

_ESP_EXTRA: dict[str, list[int]] = {
    "Medicina Familiar":   [13],   # Dr. Alonso Márquez
    "Psicología Infantil": [74],   # Jorge Montalba
}

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
            if not profs and esp in _ESP_EXTRA:
                profs = [(pid, PROFESIONALES[pid]["nombre"], PROFESIONALES[pid].get("intervalo"))
                         for pid in _ESP_EXTRA[esp] if pid in PROFESIONALES]
            if not profs:
                continue
            items.append({
                "especialidad": esp,
                "precio": _PRECIO_ESP_LABEL.get(esp) or _precio_label(esp, profs[0][0] if len(profs) == 1 else None),
                "prestaciones": _PRESTACIONES.get(esp, []),
                **(_AGENDAR_VIA.get(esp) or {}),
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

@router.get("/status")
async def status():
    """Sin gate: el portal lo consulta para decidir si embebe el agendador o
    cae al fallback de WhatsApp. No expone nada sensible."""
    return {"enabled": bool(config.AGENDADOR_PUBLICO_ENABLED)}


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
        # Prestación elegida en el agendador → observación visible en la agenda
        # Medilink (recepción y el profesional saben a qué viene el paciente).
        import re as _re_pr
        _prest = _re_pr.sub(r"[^\w\sáéíóúñÁÉÍÓÚÑ+/().$·-]", "", str(body.get("prestacion") or ""))[:80].strip()
        resultado = await crear_cita(
            id_paciente=int(id_paciente), id_profesional=id_profesional,
            fecha=fecha, hora_inicio=hora_inicio, hora_fin=hora_fin,
            observaciones_extra=(f"[{_prest}] " if _prest else ""),
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
    """Confirmación inmediata por WhatsApp. Requiere template aprobado; gated.
    FIX F064: usar body_params= (no params=) — kwarg correcto de send_whatsapp_template.
    Template recordatorio_cita tiene 6 placeholders:
      {{1}}=nombre {{2}}=especialidad {{3}}=profesional {{4}}=fecha {{5}}=hora {{6}}=prevision
    """
    try:
        from messaging import send_whatsapp_template
        await send_whatsapp_template(phone, "recordatorio_cita", body_params=[
            prof.get("nombre", ""),
            esp or prof.get("especialidad", ""),
            prof.get("nombre", ""),
            fecha,
            hora_inicio,
            "Particular",
        ])
    except Exception as e:
        log.warning("_wa_confirm agendador: %s", e)
