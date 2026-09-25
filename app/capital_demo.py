"""
capital_demo.py — Desvío temporal, acotado a UNA demo de venta.

Capital Travel (agencia de trekking/alta montaña de Santiago, cliente piloto
de Alma) recibe UNA plantilla de marketing desde la línea del CMC, dirigida a
UN solo número (el dueño de la agencia). Este módulo intercepta esa conversación
puntual y la atiende con un asistente propio de Capital Travel — nunca con el
flujo del CMC.

Activación SOLO por lista blanca:
  - CAPITAL_DEMO_PHONES: números separados por coma, solo dígitos (ej "56912345678")
  - CAPITAL_DEMO_HASTA: fecha límite AAAA-MM-DD (America/Santiago); pasada esa
    fecha el desvío se apaga solo.
Sin AMBAS variables seteadas el módulo no hace nada — ningún paciente del CMC
puede verse afectado. Default en prod: apagado.

Otras env opcionales:
  - CAPITAL_API_BASE: base de la API pública de Alma Capital (default
    http://127.0.0.1:8210, mismo servidor).
  - CAPITAL_DEMO_SRC: parámetro `src` del link de reserva (ej "dif-3").

Palabra de escape: un mensaje que sea EXACTAMENTE "CMC" (case-insensitive)
hace que el mensaje siga el flujo normal del bot del CMC.

Enganche: app/main.py, webhook POST /webhook, justo después de deduplicar por
message id y antes de rate-limit/sesión/estado/Medilink/consent/autocaptura
RUT/handle_message.
"""
import json
import logging
import os
import time
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import anthropic
import httpx

from config import ANTHROPIC_API_KEY

log = logging.getLogger("capital_demo")

_CL = ZoneInfo("America/Santiago")

client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
_MODEL = "claude-haiku-4-5-20251001"

# Default de WHATSAPP en alma-capital/app/routers/public.py — último recurso
# si ni siquiera se pudo consultar /api/public/info.
_WHATSAPP_FALLBACK = "56969651724"

_ESCAPE_WORD = "CMC"

# ── Historial de conversación en memoria (NO sessions.db — esa es de pacientes) ──
_HIST_TTL_S = 24 * 3600
_HIST_MAX_TURNOS = 12
_historial: dict[str, list[dict]] = {}
_historial_ts: dict[str, float] = {}

# ── Cache de contexto (tours/salidas/condiciones/info) ──────────────────────
_CACHE_TTL_S = 300
_cache_ctx = {"ts": 0.0, "texto": None, "whatsapp": _WHATSAPP_FALLBACK}


# ── Activación ────────────────────────────────────────────────────────────
def _phones_lista() -> set:
    raw = os.getenv("CAPITAL_DEMO_PHONES", "")
    return {p.strip().lstrip("+") for p in raw.split(",") if p.strip()}


def _hasta() -> date | None:
    raw = (os.getenv("CAPITAL_DEMO_HASTA", "") or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        log.warning("CAPITAL_DEMO_HASTA inválida (se espera AAAA-MM-DD): %r", raw)
        return None


def activo(phone: str) -> bool:
    """True solo si el teléfono está en la whitelist Y la fecha límite no pasó.

    Fail-closed: sin CAPITAL_DEMO_PHONES o sin CAPITAL_DEMO_HASTA, siempre False.
    """
    phone = (phone or "").lstrip("+")
    if not phone:
        return False
    if phone not in _phones_lista():
        return False
    hasta = _hasta()
    if hasta is None:
        return False
    hoy = datetime.now(_CL).date()
    return hoy <= hasta


# ── Extracción de texto del mensaje entrante ─────────────────────────────
def texto_de_mensaje(msg: dict, msg_type: str):
    """Devuelve el texto del mensaje si es texto o botón; None para audio,
    imagen, video, documento, sticker, reacción, etc."""
    if msg_type == "text":
        return (msg.get("text", {}).get("body") or "").strip() or None
    if msg_type == "button":
        b = msg.get("button", {}) or {}
        return (b.get("text") or b.get("payload") or "").strip() or None
    if msg_type == "interactive":
        inter = msg.get("interactive", {}) or {}
        itype = inter.get("type", "")
        if itype == "button_reply":
            br = inter.get("button_reply", {}) or {}
            return (br.get("title") or br.get("id") or "").strip() or None
        if itype == "list_reply":
            lr = inter.get("list_reply", {}) or {}
            return (lr.get("title") or lr.get("id") or "").strip() or None
    return None


# ── Historial por teléfono ───────────────────────────────────────────────
def _historial_get(phone: str) -> list:
    ts = _historial_ts.get(phone, 0.0)
    if time.time() - ts > _HIST_TTL_S:
        _historial.pop(phone, None)
        _historial_ts.pop(phone, None)
        return []
    return list(_historial.get(phone, []))


def _historial_push(phone: str, rol: str, texto: str):
    h = _historial_get(phone)
    h.append({"role": rol, "content": texto})
    _historial[phone] = h[-_HIST_MAX_TURNOS:]
    _historial_ts[phone] = time.time()


# ── Primer contacto: flyer + botón "PLOMO" (una sola vez por teléfono) ──────
# Persistencia doble: memoria (rápido, cierra la carrera del mismo proceso) +
# JSON en disco (sobrevive un reinicio del servicio). Archivo chico, sin PII
# más allá del teléfono — mismo criterio que otros archivos de estado del bot.
_ENVIADOS_PATH = Path(__file__).resolve().parent.parent / "data" / "capital_demo_enviados.json"
_enviados_mem = None  # set[str] | None — lazy, se carga la primera vez que se usa

_FLYER_IMG_URL = "https://capital.agentecmc.cl/static/img/flyer-plomo-2026.jpg"
_FLYER_TOUR_ID = 7
_FLYER_FOOTER = "Responde BAJA para no recibir más salidas"
_FLYER_CTA_TEXT = "Reservar mi cupo"
_BOTON_PLOMO_BODY = "¿Te cuento los requisitos y cómo prepararte? Toca PLOMO o escríbeme tu pregunta."

# Nombres por teléfono cuando no llega el nombre de perfil de WhatsApp (Meta
# no siempre lo entrega). Solo cubre los teléfonos de la demo — no es una
# libreta general de contactos.
_NOMBRES_FALLBACK = {
    "56983129274": "Juan Carlos",
    "56987834148": "Rodrigo",
}

_COPY_FLYER_RESTO = (
    " 🏔️ ALGÚN DÍA VAS A CONTAR ESTA HISTORIA.\n\n"
    "El frío en la cara. El sonido de tus pasos sobre la montaña. La cordillera "
    "extendiéndose hasta donde alcanza la mirada… y tú, ahí, viviendo eso que "
    "tantas veces imaginaste.\n\n"
    "Cerro El Plomo. 5.424 metros de un desafío que empieza mucho antes de la "
    "cumbre: cuando decides ir por él.\n\n"
    "Prepárate para días intensos, paisajes inmensos y compañeros con quienes "
    "compartir cada paso. Hay experiencias que se quedan contigo mucho después "
    "de volver a casa. 🔥\n\n"
    "⚡ CIERRE DE PREVENTA | ÚLTIMOS CUPOS\n\n"
    "📅 19–20–21 de diciembre: 3 cupos\n"
    "📅 26–27–28 de diciembre: 3 cupos\n"
    "📅 02–03–04 de enero: 4 cupos\n\n"
    "Capital Travel | Tu próxima gran historia empieza con un paso."
)


def _cargar_enviados():
    """Devuelve el set de teléfonos que ya recibieron el flyer, cacheado en
    memoria tras la primera lectura del archivo."""
    global _enviados_mem
    if _enviados_mem is not None:
        return _enviados_mem
    try:
        if _ENVIADOS_PATH.exists():
            data = json.loads(_ENVIADOS_PATH.read_text(encoding="utf-8"))
            _enviados_mem = set(data) if isinstance(data, list) else set()
        else:
            _enviados_mem = set()
    except Exception as e:  # noqa: BLE001
        log.warning("capital_demo: no se pudo leer %s: %s", _ENVIADOS_PATH, e)
        _enviados_mem = set()
    return _enviados_mem


def _es_primera_vez(phone: str) -> bool:
    return phone not in _cargar_enviados()


def _marcar_enviado(phone: str):
    """Marca el teléfono ANTES de enviar (memoria primero, cierra la carrera
    dentro del mismo proceso) y persiste a disco para sobrevivir un reinicio."""
    enviados = _cargar_enviados()
    if phone in enviados:
        return
    enviados.add(phone)
    try:
        _ENVIADOS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _ENVIADOS_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(sorted(enviados), ensure_ascii=False), encoding="utf-8")
        tmp.replace(_ENVIADOS_PATH)
    except Exception as e:  # noqa: BLE001
        log.error("capital_demo: no se pudo persistir %s: %s", _ENVIADOS_PATH, e)


def _primer_nombre(nombre_perfil: str) -> str:
    n = (nombre_perfil or "").strip()
    return n.split()[0] if n else ""


def _nombre_para(phone: str, wa_profile_name: str) -> str:
    pn = _primer_nombre(wa_profile_name)
    if pn:
        return pn
    return _NOMBRES_FALLBACK.get((phone or "").lstrip("+"), "")


def _render_copy_flyer(nombre: str) -> str:
    saludo = f"¡Hola {nombre}!" if nombre else "¡Hola!"
    return saludo + _COPY_FLYER_RESTO


def _link_reserva_plomo() -> str:
    url = f"https://capital.agentecmc.cl/agendar?tour={_FLYER_TOUR_ID}"
    src = os.getenv("CAPITAL_DEMO_SRC", "").strip()
    if src:
        url += f"&src={src}"
    return url


def _msg_flyer_plomo(nombre: str) -> dict:
    return {
        "type": "cta_url",
        "header": {"type": "image", "image": {"link": _FLYER_IMG_URL}},
        "body": {"text": _render_copy_flyer(nombre)},
        "footer": {"text": _FLYER_FOOTER},
        "action": {
            "name": "cta_url",
            "parameters": {"display_text": _FLYER_CTA_TEXT, "url": _link_reserva_plomo()},
        },
    }


def _msg_boton_plomo() -> dict:
    return {
        "type": "button",
        "body": {"text": _BOTON_PLOMO_BODY},
        "action": {
            "buttons": [{"type": "reply", "reply": {"id": "PLOMO", "title": "PLOMO"}}],
        },
    }


def _es_pregunta_concreta(texto: str) -> bool:
    """True si el primer mensaje merece respuesta del asistente además del
    flyer: trae signo de pregunta, o es largo (más de ~6 palabras)."""
    if "?" in texto or "¿" in texto:
        return True
    return len(texto.split()) > 6


# ── Contexto en vivo desde la API pública de Alma Capital ───────────────
def _capital_api_base() -> str:
    return os.getenv("CAPITAL_API_BASE", "http://127.0.0.1:8210")


def _render_contexto(tours: list, salidas_por_tour: dict, condiciones: list) -> str:
    bloques = []
    for t in tours:
        sal = salidas_por_tour.get(t.get("id"), [])
        if sal:
            fechas = "; ".join(
                f"{s.get('fecha')} {s.get('hora', '')} "
                f"({s.get('disponibles', 0)} cupos de {s.get('cupo', 0)})".strip()
                for s in sal
            )
        else:
            fechas = "sin salida programada actualmente"
        dur = t.get("duracion_dias") or t.get("duracion_horas")
        dur_u = "días" if t.get("duracion_dias") else "horas"
        precio = t.get("precio_persona") or 0
        bloques.append(
            f"- [id {t.get('id')}] {t.get('nombre')} ({t.get('categoria', '')}) — "
            f"dificultad: {t.get('dificultad', 's/i')}, altitud: {t.get('altitud_m', 's/i')} m, "
            f"duración: {dur} {dur_u}, destino: {t.get('destino', '')}, "
            f"precio: ${precio:,.0f} CLP por persona. Salidas: {fechas}"
            .replace(",", ".")
        )
    tours_txt = "\n".join(bloques) or "No hay tours cargados en este momento."

    cond_txt = "\n".join(
        f"- {c.get('titulo', '')}: {c.get('mensaje', '')} (estado: {c.get('estado', '')})"
        for c in condiciones
    ) or "Sin novedades reportadas."

    return f"TOURS DISPONIBLES:\n{tours_txt}\n\nCONDICIONES DE LA CORDILLERA:\n{cond_txt}"


async def _fetch_contexto():
    """Devuelve (texto_contexto, whatsapp_agencia), cacheado 5 minutos.
    Lanza excepción si la API no responde — el caller decide el fallback."""
    ahora = time.time()
    if _cache_ctx["texto"] is not None and (ahora - _cache_ctx["ts"]) < _CACHE_TTL_S:
        return _cache_ctx["texto"], _cache_ctx["whatsapp"]

    async with httpx.AsyncClient(base_url=_capital_api_base(), timeout=5.0) as cli:
        r_info = await cli.get("/api/public/info")
        r_info.raise_for_status()
        info = r_info.json() or {}
        whatsapp = info.get("whatsapp") or _WHATSAPP_FALLBACK

        r_tours = await cli.get("/api/public/tours", params={"lang": "es"})
        r_tours.raise_for_status()
        tours = r_tours.json() or []

        r_cond = await cli.get("/api/public/condiciones", params={"lang": "es"})
        r_cond.raise_for_status()
        condiciones = r_cond.json() or []

        salidas_por_tour = {}
        for t in tours:
            tid = t.get("id")
            try:
                r_sal = await cli.get("/api/public/salidas-tour", params={"tour_id": tid})
                r_sal.raise_for_status()
                salidas_por_tour[tid] = r_sal.json() or []
            except Exception as e:  # noqa: BLE001 — un tour caído no tumba el resto
                log.warning("capital_demo: salidas-tour falló para tour_id=%s: %s", tid, e)
                salidas_por_tour[tid] = []

    texto = _render_contexto(tours, salidas_por_tour, condiciones)
    _cache_ctx.update(ts=ahora, texto=texto, whatsapp=whatsapp)
    return texto, whatsapp


# ── Prompt + llamada a Claude ────────────────────────────────────────────
def _system_prompt(contexto: str, whatsapp: str) -> str:
    src = os.getenv("CAPITAL_DEMO_SRC", "").strip()
    src_txt = f"&src={src}" if src else ""
    return f"""Eres el asistente de WhatsApp de Capital Travel, agencia de trekking y alta montaña en Santiago (Cajón del Maipo, Farellones, Valle Nevado), con guías y traslado desde Santiago.

Tu voz es la del flyer que ya le llegó a esta persona: motivadora, aventurera, cercana — la de alguien que ya subió esas montañas y quiere que el otro también viva "esa historia que algún día va a contar". Invita al desafío, no lo vendas frío. Un par de emojis de montaña/fuego/mochila cuando calce de forma natural (🏔️🔥🎒), NUNCA en cada línea ni forzados. No prometas la cumbre ni exageres — la seguridad, los guías y la preparación física se cuentan como PARTE de la aventura ("vas bien acompañado", "te preparamos para que lo disfrutes"), nunca como letra chica o advertencia aparte.

Responde sobre tours, precios, dificultad, altura, duración, qué llevar, preparación física, condiciones de la cordillera y cómo reservar, usando SOLO los datos del contexto de abajo. Jamás inventes fechas, precios, cupos ni guías que no estén ahí. Si un tour no tiene salida programada, dilo con franqueza y ofrece coordinar la fecha directamente con la agencia.

Para reservar, entrega este link con el id del tour correspondiente: https://capital.agentecmc.cl/agendar?tour=<id>{src_txt}

Para hablar con una persona de la agencia, entrega este WhatsApp: {whatsapp}

Sobre altura: puedes mencionar aclimatación y soroche de forma general sobre los 4.000 m como parte de la preparación, pero nunca des indicaciones médicas personales — para eso, que consulten a su médico.

Nunca menciones al Centro Médico Carampangue ni temas de salud de esa clínica.

Español de Chile, natural y cercano. Tuteo estándar: tienes, quieres, puedes, sabes, dime, cuéntame, escríbeme, mira. PROHIBIDO el voseo argentino: nunca "tenés", "querés", "podés", "sabés", "decime", "contame", "escribime", "avisame", "mirá", "fijate", "dale", "che", "re" (como "re lindo") ni "vos". Con energía y calidez, nunca en tono de venta corporativa. Mensajes cortos, estilo WhatsApp, máximo ~700 caracteres. Sin markdown salvo *negrita* de WhatsApp (asteriscos simples).

CONTEXTO EN VIVO:
{contexto}"""


_APOLOGIA = (
    "Uy, se me cortó el hilo por un problema técnico 😅 pero tu aventura no se detiene aquí.\n"
    "Escríbenos directo al WhatsApp de la agencia: {whatsapp}"
)


async def _responder_asistente(phone: str, texto: str) -> str:
    try:
        contexto, whatsapp = await _fetch_contexto()
    except Exception as e:  # noqa: BLE001
        log.error("capital_demo: contexto Capital API no disponible: %s", e)
        return _APOLOGIA.format(whatsapp=_WHATSAPP_FALLBACK)

    historial = _historial_get(phone)
    mensajes = historial + [{"role": "user", "content": texto}]

    try:
        resp = await client.messages.create(
            model=_MODEL,
            max_tokens=400,
            system=_system_prompt(contexto, whatsapp),
            messages=mensajes,
        )
        respuesta = (resp.content[0].text or "").strip()
        if not respuesta:
            raise ValueError("respuesta vacía de Claude")
    except Exception as e:  # noqa: BLE001
        log.error("capital_demo: Claude falló: %s", e)
        return _APOLOGIA.format(whatsapp=whatsapp)

    _historial_push(phone, "user", texto)
    _historial_push(phone, "assistant", respuesta)
    return respuesta


# ── Envío + logging (mismo canal de mensajería del bot, log marcado aparte) ──
async def _log(phone: str, direction: str, texto: str):
    try:
        from session import log_message
        log_message(phone, direction, texto, "CAPITAL_DEMO", canal="capital_demo")
    except Exception as e:  # noqa: BLE001 — nunca debe tumbar la demo
        log.warning("capital_demo: no se pudo loguear mensaje %s: %s", direction, e)


async def _enviar(phone: str, texto: str):
    from messaging import send_whatsapp
    await send_whatsapp(phone, texto)
    await _log(phone, "out", texto)


async def _enviar_interactivo(phone: str, interactive: dict, log_text: str):
    from messaging import send_whatsapp_interactive
    await send_whatsapp_interactive(phone, interactive)
    await _log(phone, "out", log_text)


# ── Entrypoint del webhook ───────────────────────────────────────────────
_AVISO_SOLO_TEXTO = (
    "🎒 Por ahora solo puedo leer mensajes de texto — cuéntame por escrito qué aventura tienes en mente."
)

# Quick-reply de la plantilla de marketing (botón "PLOMO"): el texto/payload
# que llega es literal el nombre del botón, no una pregunta — se expande a
# una consulta completa antes de pasarla al asistente para que la respuesta
# sea siempre específica (requisitos, preparación, fechas, cupos, link),
# sin depender de que Claude interprete bien una sola palabra suelta.
_QUICK_REPLIES = {
    "PLOMO": (
        "Quiero información sobre la Expedición al Cerro El Plomo: "
        "requisitos, preparación física, fechas disponibles, cupos y cómo reservar."
    ),
}


async def manejar_webhook_wa(phone: str, msg: dict, msg_type: str,
                              wa_profile_name: str = "") -> bool:
    """Punto de entrada único desde el webhook de WhatsApp.

    Devuelve True si el mensaje fue atendido por la demo de Capital Travel
    (el caller debe responder 200 sin seguir el flujo del CMC). Devuelve
    False si no aplica (teléfono fuera de whitelist, fecha vencida, o el
    paciente escribió la palabra de escape "CMC") — el caller sigue su
    flujo normal.

    `wa_profile_name`: nombre de perfil de WhatsApp del contacto (si Meta lo
    entrega) — se usa como {nombre} del flyer de primer contacto.
    """
    if not activo(phone):
        return False

    texto = texto_de_mensaje(msg, msg_type)

    if texto is None:
        await _log(phone, "in", f"[{msg_type}]")
        await _enviar(phone, _AVISO_SOLO_TEXTO)
        return True

    texto_norm = texto.strip()
    if texto_norm.upper() == _ESCAPE_WORD:
        return False

    await _log(phone, "in", texto_norm)

    # Primer contacto: flyer con imagen + link de reserva, y botón único
    # "PLOMO", ANTES que cualquier respuesta del asistente. Se marca de
    # inmediato (memoria + disco) para que un reinicio o una carrera de
    # mensajes casi simultáneos no lo repita.
    primera_vez = _es_primera_vez(phone)
    if primera_vez:
        _marcar_enviado(phone)
        nombre = _nombre_para(phone, wa_profile_name)
        await _enviar_interactivo(phone, _msg_flyer_plomo(nombre), _render_copy_flyer(nombre))
        await _enviar_interactivo(phone, _msg_boton_plomo(), _BOTON_PLOMO_BODY)
        if not _es_pregunta_concreta(texto_norm):
            return True

    texto_asistente = _QUICK_REPLIES.get(texto_norm.upper(), texto_norm)
    respuesta = await _responder_asistente(phone, texto_asistente)
    await _enviar(phone, respuesta)
    return True
