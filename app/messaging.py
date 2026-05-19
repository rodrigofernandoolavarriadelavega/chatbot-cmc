"""Messaging utilities — WhatsApp, Instagram, Facebook Messenger, Whisper."""
import asyncio
import logging
import os
import re

import httpx

import time

from config import (META_ACCESS_TOKEN, META_PHONE_NUMBER_ID,
                    META_PAGE_ACCESS_TOKEN, META_MESSENGER_TOKEN,
                    INSTAGRAM_USER_ID, META_PAGE_ID,
                    META_WABA_ID,
                    OPENAI_API_KEY)

log = logging.getLogger("bot")

def _normalize_markdown_for_chat(body: str) -> str:
    """Convierte **bold** → *bold* para WhatsApp/IG/FB (Meta renderer)."""
    import re as _re_nm
    return _re_nm.sub(r"\*\*([^*]+)\*\*", r"*\1*", body or "")



META_API_URL = f"https://graph.facebook.com/v22.0/{META_PHONE_NUMBER_ID}/messages"

# P-3: cliente httpx compartido para Meta Cloud API — evita crear/cerrar el pool
# TCP+SSL en cada mensaje saliente (~74ms de overhead por envío).
_META_CLIENT: httpx.AsyncClient | None = None


def _get_meta_client() -> httpx.AsyncClient:
    """Retorna el cliente httpx compartido para Meta, creándolo si está cerrado."""
    global _META_CLIENT
    if _META_CLIENT is None or _META_CLIENT.is_closed:
        _META_CLIENT = httpx.AsyncClient(
            timeout=10,
            limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
        )
    return _META_CLIENT


async def _post_meta(payload: dict) -> str | None:
    """POST a Meta Cloud API con retry selectivo.

    - Ventana 24h cerrada (codes 131047/131052/131051/131045/131042/131030 o
      mensaje con "re-engagement" / "24 hour") → INFO, no es fallo operacional.
    - 4xx (excepto 429): payload irreversible → no reintenta.
    - 5xx / 429 / timeout / NetworkError: transitorio → backoff exponencial
      (2s, 4s), 3 intentos totales.
    """
    WINDOW_CLOSED_CODES = {131047, 131052, 131051, 131045, 131042, 131030}
    backoffs = [2, 4]
    for attempt in range(3):
        try:
            client = _get_meta_client()
            r = await client.post(
                META_API_URL,
                headers={"Authorization": f"Bearer {META_ACCESS_TOKEN}"},
                json=payload,
            )
            if r.status_code == 200:
                try:
                    data = r.json()
                    messages = data.get("messages", [])
                    if messages:
                        return messages[0].get("id")
                except Exception:
                    pass
                return None
            err_code = None
            err_msg = ""
            try:
                err = r.json().get("error", {}) or {}
                err_code = err.get("code")
                err_msg = (err.get("message") or err.get("error_user_msg") or "")[:200]
            except Exception:
                err_msg = r.text[:200]
            msg_lower = err_msg.lower()
            is_window_closed = (
                err_code in WINDOW_CLOSED_CODES
                or "re-engagement" in msg_lower
                or "outside the allowed window" in msg_lower
                or "24 hour" in msg_lower
                or "24-hour" in msg_lower
            )
            if is_window_closed:
                log.info("Meta API: ventana 24h cerrada para %s (code=%s) — mensaje omitido",
                         payload.get("to", "?"), err_code)
                return None
            if 400 <= r.status_code < 500 and r.status_code != 429:
                _to_val = payload.get("to", "?")
                try:
                    from config import ADMIN_ALERT_PHONE as _AAP
                except Exception:
                    _AAP = ""
                if _to_val == _AAP:
                    log.info("Meta API %s to=%s (admin, sin WA): %s", r.status_code, _to_val, err_msg)
                else:
                    log.error("Meta API %s (no-retry) to=%s: %s", r.status_code, _to_val, err_msg)
                return None
            log.warning("Meta API intento %d → %s (transitorio): %s",
                        attempt + 1, r.status_code, err_msg)
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            log.warning("Meta API intento %d error red: %s", attempt + 1, e)
        if attempt < len(backoffs):
            await asyncio.sleep(backoffs[attempt])
    log.error("Meta API: 3 intentos fallidos, abandono (to=%s)", payload.get("to"))
    return None


async def react_whatsapp(to: str, message_id: str, emoji: str = "⏳"):
    """Reacciona a un mensaje con un emoji (indicador de 'pensando')."""
    await _post_meta({
        "messaging_product": "whatsapp",
        "to": to,
        "type": "reaction",
        "reaction": {"message_id": message_id, "emoji": emoji},
    })


async def unreact_whatsapp(to: str, message_id: str):
    """Quita la reacción de un mensaje (emoji vacío)."""
    await _post_meta({
        "messaging_product": "whatsapp",
        "to": to,
        "type": "reaction",
        "reaction": {"message_id": message_id, "emoji": ""},
    })


# Dedupe: evita enviar el mismo mensaje idéntico al mismo teléfono en una
# ventana corta. Causa común: el paciente escribe "Hola" dos veces y el bot
# manda el mismo saludo largo dos veces; o errores que generan el mismo
# "Tuve un problema técnico" repetido. Guardamos hash(body) por phone con TTL.
import time as _t
_DEDUPE_WINDOW_S = 120  # 2 min
_DEDUPE_CACHE: dict[str, tuple[int, float]] = {}  # phone → (hash, ts)


def _is_dupe_outbound(to: str, body: str) -> bool:
    """True si estamos a punto de enviar el mismo body a `to` dentro de la ventana."""
    if not body or not to:
        return False
    now = _t.time()
    h = hash(body)
    prev = _DEDUPE_CACHE.get(to)
    if prev and prev[0] == h and (now - prev[1]) < _DEDUPE_WINDOW_S:
        return True
    _DEDUPE_CACHE[to] = (h, now)
    # GC barato cuando el cache crece
    if len(_DEDUPE_CACHE) > 500:
        for k in list(_DEDUPE_CACHE.keys()):
            if now - _DEDUPE_CACHE[k][1] > _DEDUPE_WINDOW_S * 4:
                _DEDUPE_CACHE.pop(k, None)
    return False


_PERSONAL_PHONE_DR = "56987834148"  # número personal Dr. Olavarría — NUNCA customer-facing
_RX_PERSONAL_LEAK = re.compile(r"\+?\s*56[\s\-]*9[\s\-]*8783[\s\-]*4148")
_TEL_CMC_WA_GUARD = "+56966610737"


_RX_FIJO_44 = re.compile(r"\(\s*44\s*\)\s*\d{3}[\s\-]*\d{4}")
_FIJO_CMC_CANONICO = "(41) 296 5226"


def _final_phone_guard(text: str) -> str:
    """Última defensa antes de enviar al canal. Si por algún path el número
    personal o el código de área incorrecto se filtraron sin pasar por
    _scrub_telefonos, los capturamos acá y loggeamos warning para detectar
    regresiones. Auditoría 2026-04-28: 74 casos en 7d con código (44) en
    respuestas — Claude Haiku alucinaba o el sitio web v3 (que tiene "(44)"
    hardcoded) lo metía en contexto."""
    if not text:
        return text
    if _RX_PERSONAL_LEAK.search(text):
        log.warning("PHONE_LEAK_GUARD personal_number_caught snippet=%r",
                    text[:160])
        text = _RX_PERSONAL_LEAK.sub(_TEL_CMC_WA_GUARD, text)
    if _RX_FIJO_44.search(text):
        log.warning("PHONE_LEAK_GUARD codigo_area_44 snippet=%r", text[:160])
        text = _RX_FIJO_44.sub(_FIJO_CMC_CANONICO, text)
    return text


# ── Guard centralizado para mensajes proactivos (jobs/crons) ─────────────────
# Evita enviar mensajes outbound proactivos a teléfonos de staff/admin que nunca
# tienen ventana 24h abierta desde el bot → genera bucle de errores 131047.
# Bug confirmado 2026-05-16: 39/43 errores 131047 del día al número personal Dr.
# Solución sistémica: un único guard en messaging.py, aplicado por send_whatsapp_proactive.

_PROACTIVE_BLOCKLIST: set[str] = set()


def _refresh_proactive_blocklist() -> None:
    global _PROACTIVE_BLOCKLIST
    admin_raw = os.getenv("ADMIN_ALERT_PHONE", "").strip()
    admin_phones = {admin_raw} if admin_raw else set()
    # STAFF_PHONES puede ser JSON {"56912345678": "Nombre"} o CSV "56912345678,56987654321"
    staff_raw = os.getenv("STAFF_PHONES", "").strip()
    staff: set[str] = set()
    if staff_raw:
        try:
            import json as _j
            parsed = _j.loads(staff_raw)
            if isinstance(parsed, dict):
                staff = set(parsed.keys())
            elif isinstance(parsed, list):
                staff = set(parsed)
        except Exception:
            staff = {p.strip() for p in staff_raw.split(",") if p.strip()}
    _PROACTIVE_BLOCKLIST = (admin_phones | staff) - {""}


def _normalize_phone_for_block(phone: str) -> str:
    """Normaliza a dígitos puros sin leading zeros para comparación."""
    return phone.replace("+", "").lstrip("0")


def is_proactive_blocked(phone: str) -> bool:
    """True si NO se debe enviar mensajes proactivos (jobs, crons) a este phone.

    Los teléfonos de la blocklist son de staff/admin que nunca tienen ventana
    24h abierta desde el bot — enviarles texto libre genera error 131047 en bucle.

    Uso: llamar antes de cualquier send en jobs/crons (send_whatsapp_proactive
    lo hace automáticamente). Para respuestas inbound (dentro de ventana 24h)
    usar send_whatsapp directamente.
    """
    if not _PROACTIVE_BLOCKLIST:
        _refresh_proactive_blocklist()
    phone_norm = _normalize_phone_for_block(phone)
    blocklist_norm = {_normalize_phone_for_block(p) for p in _PROACTIVE_BLOCKLIST}
    result = phone_norm in blocklist_norm
    if result:
        log.info("PROACTIVE_BLOCK matched: phone=%s norm=%s blocklist=%s",
                 phone, phone_norm, blocklist_norm)
    return result


async def send_whatsapp_proactive(to: str, body, **kwargs) -> str | None:
    """Wrapper de send_whatsapp para mensajes proactivos (jobs, crons, fidelizacion).

    Verifica is_proactive_blocked antes de enviar. Si el phone está en la blocklist
    (ADMIN_ALERT_PHONE o STAFF_PHONES), logea proactive_skip_blocklist y retorna sin enviar.

    Para respuestas a mensajes inbound del paciente (ventana 24h garantizada),
    usar send_whatsapp directamente.
    """
    if is_proactive_blocked(to):
        log.info(
            "proactive_skip_blocklist: phone=%s body_snippet=%r",
            to, (str(body) if not isinstance(body, dict) else "[interactive]")[:80],
        )
        try:
            from session import log_event as _le
            _le(to, "proactive_skip_blocklist", {
                "body_snippet": (str(body) if not isinstance(body, dict) else "[interactive]")[:120],
            })
        except Exception:
            pass
        return None
    return await send_whatsapp(to, body)


async def send_whatsapp(to: str, body) -> str | None:
    """Envía mensaje de texto (o interactivo) vía Meta Cloud API.
    Retorna wamid o None si falla.

    Si recibe un dict con `type=interactive`, auto-routea a send_whatsapp_interactive.
    Caso real 2026-04-30: jobs de cross-sell (ORL↔Fono, Odonto-Estética, MG-Chequeo)
    crashearon con `'dict' has no attribute 'strip'` porque sus _msg_*() arman dicts
    interactive y los pasan directo a send_fn=send_whatsapp. Antes: 6+ pacientes
    fallidos cada vez que corría el cron.

    Si el mismo body fue enviado a `to` en los últimos 2 min, skip (dedupe).

    Guard defensivo movido a send_whatsapp_proactive (P-4): inspect.stack() eliminado
    del hot path — era 0.4ms median, 1.6ms P99 por mensaje. Los crons usan
    send_whatsapp_proactive que ya aplica is_proactive_blocked() sin stack inspection.
    """
    if isinstance(body, dict):
        if body.get("type") == "interactive" and "interactive" in body:
            return await send_whatsapp_interactive(to, body["interactive"])
        # Otros payloads dict no soportados acá → log + None
        log.warning("send_whatsapp recibió dict sin type=interactive: keys=%s", list(body.keys()))
        return None
    if not body or not str(body).strip():
        return None
    body = _final_phone_guard(body)
    # BUG-C guard: si después de _final_phone_guard aún queda ** sin normalizar,
    # loggear warning (regresión detectable) y normalizar como defensa final.
    if isinstance(body, str) and "**" in body:
        log.warning("MARKDOWN_GUARD send_whatsapp ** sin normalizar snippet=%r", body[:120])
    body = _normalize_markdown_for_chat(body)
    # FIX-8: WhatsApp rechaza mensajes >4096 chars. Si es largo, dividir en chunks
    # como IG/FB (usando _split_long_msg). Evita error 131009 silencioso de Meta.
    if isinstance(body, str) and len(body) > 4000:
        log.warning("send_whatsapp: mensaje largo %d chars → dividiendo en chunks", len(body))
        wamid = None
        for chunk in _split_long_msg(body, limit=4000):
            chunk = chunk.strip()
            if not chunk:
                continue
            if _is_dupe_outbound(to, chunk):
                continue
            wamid = await _post_meta({
                "messaging_product": "whatsapp",
                "to": to,
                "type": "text",
                "text": {"body": chunk},
            })
            import asyncio as _asyncio
            await _asyncio.sleep(0.5)
        return wamid
    if _is_dupe_outbound(to, body):
        log.info("dedupe outbound skipped to=%s len=%d", to, len(body))
        return None
    return await _post_meta({
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": body},
    })


async def edit_whatsapp_message(to: str, wamid: str, new_body: str) -> tuple[bool, str | None]:
    """Edita un mensaje de texto ya enviado vía Meta Cloud API.

    Limitaciones de Meta: sólo texto, ventana de 15 min desde envío original.
    Retorna (ok, error_message). Si ok=True, error_message es None.
    """
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "text",
        "text": {"body": new_body},
        "message_id": wamid,
    }
    try:
        client = _get_meta_client()
        r = await client.post(
            META_API_URL,
            headers={"Authorization": f"Bearer {META_ACCESS_TOKEN}"},
            json=payload,
        )
        if r.status_code == 200:
            return True, None
        try:
            err = r.json().get("error", {})
            msg = err.get("message") or err.get("error_user_msg") or r.text[:300]
        except Exception:
            msg = r.text[:300]
        log.error("edit_whatsapp_message falló %s: %s", r.status_code, msg)
        return False, msg
    except (httpx.TimeoutException, httpx.NetworkError) as e:
        log.error("edit_whatsapp_message error de red: %s", e)
        return False, f"Error de red: {e}"


async def send_whatsapp_location(to: str, latitude: float, longitude: float,
                                  name: str = "", address: str = ""):
    """Envía mensaje de ubicación nativo vía Meta Cloud API."""
    location = {"latitude": latitude, "longitude": longitude}
    if name:
        location["name"] = name
    if address:
        location["address"] = address
    await _post_meta({
        "messaging_product": "whatsapp",
        "to": to,
        "type": "location",
        "location": location,
    })


async def send_whatsapp_interactive(to: str, interactive: dict):
    """Envía mensaje interactivo (botones o lista) vía Meta Cloud API."""
    await _post_meta({
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": interactive,
    })


async def send_whatsapp_document(to: str, media_url: str, filename: str = "",
                                  caption: str = "") -> str | None:
    """Envía un documento (PDF, etc.) vía Meta Cloud API usando URL pública."""
    doc = {"link": media_url}
    if filename:
        doc["filename"] = filename
    if caption:
        doc["caption"] = caption
    return await _post_meta({
        "messaging_product": "whatsapp",
        "to": to,
        "type": "document",
        "document": doc,
    })


async def send_whatsapp_image(to: str, media_url: str,
                               caption: str = "") -> str | None:
    """Envía una imagen vía Meta Cloud API usando URL pública."""
    img = {"link": media_url}
    if caption:
        img["caption"] = caption
    return await _post_meta({
        "messaging_product": "whatsapp",
        "to": to,
        "type": "image",
        "image": img,
    })


async def upload_media_to_whatsapp(file_bytes: bytes, mime_type: str,
                                    filename: str = "file") -> str | None:
    """Sube un archivo a Meta Cloud API y retorna el media_id.
    Luego se puede enviar con send_whatsapp_document_by_id()."""
    if not META_ACCESS_TOKEN or not META_PHONE_NUMBER_ID:
        return None
    url = f"https://graph.facebook.com/v22.0/{META_PHONE_NUMBER_ID}/media"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                url,
                headers={"Authorization": f"Bearer {META_ACCESS_TOKEN}"},
                data={"messaging_product": "whatsapp", "type": mime_type},
                files={"file": (filename, file_bytes, mime_type)},
            )
        if r.status_code == 200:
            return r.json().get("id")
        log.error("Upload media %s: %s", r.status_code, r.text[:200])
        return None
    except Exception as e:
        log.error("Error uploading media: %s", e)
        return None


async def send_whatsapp_document_by_id(to: str, media_id: str,
                                        filename: str = "",
                                        caption: str = "") -> str | None:
    """Envía un documento usando un media_id ya subido a Meta."""
    doc = {"id": media_id}
    if filename:
        doc["filename"] = filename
    if caption:
        doc["caption"] = caption
    return await _post_meta({
        "messaging_product": "whatsapp",
        "to": to,
        "type": "document",
        "document": doc,
    })


async def send_whatsapp_image_by_id(to: str, media_id: str,
                                     caption: str = "") -> str | None:
    """Envía una imagen usando un media_id ya subido a Meta."""
    img = {"id": media_id}
    if caption:
        img["caption"] = caption
    return await _post_meta({
        "messaging_product": "whatsapp",
        "to": to,
        "type": "image",
        "image": img,
    })


# ── Caché de language code por template (TTL 1 hora) ─────────────────────────
# Meta devuelve el language real del template aprobado (ej: "es_CL").
# Nunca hardcodear — el default "es" causaba HTTP 404 en todos los templates
# aprobados en es_CL (bug detectado 2026-05-13).
_template_language_cache: dict[str, tuple[str, float]] = {}
_TEMPLATE_LANGUAGE_TTL = 3600  # segundos


async def _get_template_language(template_name: str) -> str:
    """Retorna el language code del template aprobado en Meta (ej: "es_CL").

    Consulta la Graph API una vez por TTL=1h y cachea el resultado.
    Fallback: "es_CL" (todos los templates CMC están aprobados en ese locale).
    """
    _FALLBACK = "es_CL"
    now = time.monotonic()
    cached = _template_language_cache.get(template_name)
    if cached and (now - cached[1]) < _TEMPLATE_LANGUAGE_TTL:
        return cached[0]

    waba_id = META_WABA_ID
    if not waba_id or not META_ACCESS_TOKEN:
        log.warning("messaging: _get_template_language sin WABA_ID o token — usando %s", _FALLBACK)
        return _FALLBACK

    try:
        client = _get_meta_client()
        r = await client.get(
            f"https://graph.facebook.com/v22.0/{waba_id}/message_templates",
            params={
                "name": template_name,
                "fields": "name,language,status",
                "access_token": META_ACCESS_TOKEN,
            },
            timeout=8,
        )
        if r.status_code == 200:
            templates = r.json().get("data", [])
            # Buscar primero APPROVED, luego cualquiera con ese nombre
            approved = next(
                (t for t in templates
                 if t.get("name") == template_name and t.get("status") == "APPROVED"),
                None,
            )
            tpl = approved or next(
                (t for t in templates if t.get("name") == template_name), None
            )
            if tpl and tpl.get("language"):
                lang = tpl["language"]
                _template_language_cache[template_name] = (lang, now)
                log.debug("messaging: template=%s language=%s (from Meta API)", template_name, lang)
                return lang
        log.warning(
            "messaging: no se pudo obtener language para template=%s (status=%s) — usando %s",
            template_name, r.status_code, _FALLBACK,
        )
    except Exception as e:
        log.warning("messaging: error consultando language de template=%s: %s — usando %s",
                    template_name, e, _FALLBACK)

    _template_language_cache[template_name] = (_FALLBACK, now)
    return _FALLBACK



async def send_whatsapp_template(to: str, template_name: str,
                                  body_params: list[str] | None = None,
                                  button_payloads: list[str] | None = None,
                                  language: str | None = None):
    """Envía un Message Template aprobado por Meta.

    Usar para TODOS los mensajes proactivos (fuera de ventana 24h):
    recordatorios, fidelización, lista de espera, alertas.

    Args:
        to: teléfono destino (sin +)
        template_name: nombre del template registrado en Meta
        body_params: lista de valores para {{1}}, {{2}}, etc.
        button_payloads: payloads para botones QUICK_REPLY (índice 0, 1, 2)
        language: código de idioma. Si es None (default), se consulta desde
                  Meta API con caché TTL 1h. Pasar explícitamente solo en tests.
    """
    # Resolver language desde Meta API si no se especificó explícitamente.
    # Root cause del bug: antes era language="es" hardcodeado; todos los templates
    # CMC están aprobados en "es_CL" → Meta retornaba 404.
    if language is None:
        language = await _get_template_language(template_name)

    components = []

    # Variables del body
    if body_params:
        components.append({
            "type": "body",
            "parameters": [{"type": "text", "text": p} for p in body_params],
        })

    # Payloads de botones QUICK_REPLY (dinámicos al enviar)
    if button_payloads:
        for idx, payload in enumerate(button_payloads):
            components.append({
                "type": "button",
                "sub_type": "quick_reply",
                "index": str(idx),
                "parameters": [{"type": "payload", "payload": payload}],
            })

    await _post_meta({
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": language},
            "components": components,
        },
    })


# ── Multimodal: descarga de media + transcripción Whisper ───────────────────
async def download_whatsapp_media(media_id: str) -> tuple[bytes, str] | None:
    """Descarga un archivo de WhatsApp (audio/imagen/doc) por media_id.

    Returns: (contenido_bytes, mime_type) o None si falla.
    """
    if not media_id:
        return None
    try:
        client = _get_meta_client()
        # Paso 1: obtener URL firmada del media
        meta = await client.get(
            f"https://graph.facebook.com/v22.0/{media_id}",
            headers={"Authorization": f"Bearer {META_ACCESS_TOKEN}"},
            timeout=20,
        )
        if meta.status_code != 200:
            log.error("Whisper media meta %s: %s", meta.status_code, meta.text[:200])
            return None
        info = meta.json()
        url = info.get("url", "")
        mime = info.get("mime_type", "audio/ogg")
        if not url:
            return None
        # Paso 2: descargar el binario (requiere Authorization también)
        blob = await client.get(
            url,
            headers={"Authorization": f"Bearer {META_ACCESS_TOKEN}"},
            timeout=20,
        )
        if blob.status_code != 200:
            log.error("Whisper media blob %s", blob.status_code)
            return None
        return blob.content, mime
    except Exception as e:
        log.error("Error descargando media %s: %s", media_id, e)
        return None


async def transcribe_audio(audio_bytes: bytes, mime: str = "audio/ogg") -> str:
    """Transcribe un audio a texto usando OpenAI Whisper.

    WhatsApp envía notas de voz como audio/ogg (codec opus).
    Devuelve "" si falla.
    """
    if not OPENAI_API_KEY:
        log.error("OPENAI_API_KEY no configurado — no se puede transcribir audio")
        return ""
    try:
        # Extensión según mime (Whisper la usa para elegir decoder)
        ext = "ogg"
        if "mp3" in mime or "mpeg" in mime:
            ext = "mp3"
        elif "wav" in mime:
            ext = "wav"
        elif "m4a" in mime or "mp4" in mime:
            ext = "m4a"
        elif "webm" in mime:
            ext = "webm"

        # Llamada HTTP directa (evita dependencia del SDK async del cliente openai)
        async with httpx.AsyncClient(timeout=60) as client:
            files = {
                "file": (f"audio.{ext}", audio_bytes, mime or "application/octet-stream"),
                "model": (None, "whisper-1"),
                "language": (None, "es"),
                "response_format": (None, "text"),
            }
            r = await client.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
                files=files,
            )
        if r.status_code != 200:
            log.error("Whisper API %s: %s", r.status_code, r.text[:300])
            return ""
        # response_format=text devuelve texto plano
        return r.text.strip()
    except Exception as e:
        log.error("Error transcribiendo audio: %s", e)
        return ""


def extract_text_from_pdf(pdf_bytes: bytes) -> str:
    """Extrae texto de un PDF usando PyMuPDF. Retorna "" si falla."""
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        text_parts = []
        for page in doc:
            text_parts.append(page.get_text())
        doc.close()
        return "\n".join(text_parts).strip()
    except Exception as e:
        log.error("Error extrayendo texto de PDF: %s", e)
        return ""


def extract_text_from_docx(docx_bytes: bytes) -> str:
    """Extrae texto de un archivo Word (.docx) usando python-docx. Retorna "" si falla."""
    try:
        from docx import Document
        from io import BytesIO
        doc = Document(BytesIO(docx_bytes))
        text_parts = [p.text for p in doc.paragraphs if p.text.strip()]
        return "\n".join(text_parts).strip()
    except Exception as e:
        log.error("Error extrayendo texto de DOCX: %s", e)
        return ""


async def get_whatsapp_quality_rating() -> dict | None:
    """Fetch quality rating and messaging limits from Meta API.
    Returns dict with quality_rating, messaging_limit, etc. or None on error."""
    if not META_PHONE_NUMBER_ID or not META_ACCESS_TOKEN:
        return None
    try:
        client = _get_meta_client()
        r = await client.get(
            f"https://graph.facebook.com/v22.0/{META_PHONE_NUMBER_ID}"
            "?fields=quality_rating,messaging_limit_tier,verified_name,code_verification_status,status",
            headers={"Authorization": f"Bearer {META_ACCESS_TOKEN}"},
        )
        if r.status_code == 200:
            return r.json()
        log.error("Quality rating API %s: %s", r.status_code, r.text[:200])
        return None
    except (httpx.TimeoutException, httpx.NetworkError) as e:
        log.error("Quality rating check error: %s", e)
        return None


def _split_long_msg(body: str, limit: int = 900) -> list[str]:
    """Divide un mensaje largo en chunks <= limit chars respetando líneas.
    IG y Messenger rechazan > 1000 chars; WA acepta 4096 pero conviene
    dividir para legibilidad. Intenta cortar en saltos de línea primero,
    luego en espacios, como fallback en el char exacto."""
    if not body or len(body) <= limit:
        return [body] if body else []
    chunks: list[str] = []
    resto = body
    while len(resto) > limit:
        corte = resto.rfind("\n\n", 0, limit)
        if corte < limit * 0.5:
            corte = resto.rfind("\n", 0, limit)
        if corte < limit * 0.5:
            corte = resto.rfind(" ", 0, limit)
        if corte < limit * 0.5:
            corte = limit  # cortar en el char exacto como fallback
        chunks.append(resto[:corte].rstrip())
        resto = resto[corte:].lstrip()
    if resto:
        chunks.append(resto)
    return chunks


async def send_instagram(igsid: str, body: str):
    """Envía mensaje de texto a un usuario de Instagram vía Graph API.
    IG rechaza mensajes > 1000 chars: divide en chunks automáticamente.
    Dedupe: skip si el mismo body se envió a este igsid en los últimos 2 min."""
    body = _final_phone_guard(body)
    body = _normalize_markdown_for_chat(body)
    if _is_dupe_outbound(f"ig_{igsid}", body):
        log.info("dedupe outbound skipped ig=%s len=%d", igsid, len(body))
        return
    if not INSTAGRAM_USER_ID:
        log.error("INSTAGRAM_USER_ID no configurado en .env")
        return
    url = f"https://graph.instagram.com/v22.0/{INSTAGRAM_USER_ID}/messages"
    for chunk in _split_long_msg(body, limit=900):
        for attempt in range(2):
            try:
                client = _get_meta_client()
                r = await client.post(
                    url,
                    headers={"Authorization": f"Bearer {META_PAGE_ACCESS_TOKEN}"},
                    json={"recipient": {"id": igsid}, "message": {"text": chunk}},
                )
                if r.status_code == 200:
                    break
                log.error("Instagram API intento %d → %s: %s", attempt + 1, r.status_code, r.text[:200])
            except (httpx.TimeoutException, httpx.NetworkError) as e:
                log.error("Instagram API intento %d error: %s", attempt + 1, e)


# ─── Render de templates para logging legible ─────────────────────────────────
# Bodies de los templates aprobados por Meta, con placeholders {{1}}, {{2}}...
# Se usa SOLO para guardar en messages.body un texto humano legible. El envío
# real a WhatsApp lo hace send_whatsapp_template con la estructura template.
_TEMPLATE_BODIES = {
    "postconsulta_seguimiento": (
        "Hola {1} 😊 ¿Cómo te sientes después de tu consulta de *{2}* con *{3}*?\n\n"
        "Tu opinión nos ayuda a mejorar 🙏\n\n"
        "[Mejor 😊] [Igual 😐] [Peor 😟]"
    ),
    "recordatorio_cita": (
        "Hola {1} 👋 Te recordamos tu cita de *{2}* con *{3}* el *{4}* a las *{5}*.\n\n"
        "📍 Monsalve 102, Carampangue.\n\n"
        "[Confirmar ✅] [Cancelar ❌]"
    ),
    "recordatorio_cita_2h": (
        "Hola {1} 👋 Tu cita de *{2}* con *{3}* es en 2 horas (*{4}*).\n\n"
        "📍 Monsalve 102, Carampangue."
    ),
    "lista_espera_cupo": (
        "Hola {1} 👋 Se liberó un cupo de *{2}* con *{3}* el *{4}* a las *{5}*. "
        "¿Te interesa agendar?\n\n[Sí, agendar ✅] [No, gracias ❌]"
    ),
    "informe_listo": (
        "Hola {1} 👋 Tu informe de *{2}* ya está listo para retirar.\n\n"
        "📍 Recepción CMC · Monsalve 102, Carampangue."
    ),
    "seguimiento_medico": (
        "Hola {1} 👋 El Dr/Dra *{2}* quiere hacer seguimiento de tu última consulta. "
        "¿Cómo te has sentido?\n\n[Mejor 😊] [Igual 😐] [Peor 😟]"
    ),
    "reactivacion_paciente": (
        "Hola {1} 👋 Hace tiempo no te vemos por el Centro Médico Carampangue. "
        "¿Te gustaría agendar una consulta?"
    ),
    "sistema_recuperado": (
        "Hola 👋 Ya tenemos el sistema de agendamiento funcionando otra vez. "
        "Disculpa la espera. ¿Te ayudo a agendar?"
    ),
    "cumpleanos": (
        "🎂 ¡Feliz cumpleaños, {1}! 🎉 El equipo del Centro Médico Carampangue "
        "te desea un excelente año por delante."
    ),
    "consent_marketing_v1": (
        "Hola {1} 👋 ¿Quieres recibir tips de salud, recordatorios y promociones del CMC?\n\n"
        "[Sí, acepto ✅] [No, gracias ❌]"
    ),
}


def render_template_body(name: str, params: list | tuple | None = None) -> str:
    """Renderiza body del template interpolando {1},{2}... (dict) o {{1}},{{2}}... (JSON).

    Busca primero en _TEMPLATE_BODIES (dict local). Si no está, intenta leer el
    JSON de templates/whatsapp_templates/{name}.json para cubrir winback y templates
    dinámicos no incluidos en el dict.
    Retorna "[template: name]\n{body renderizado}" para que el panel admin muestre
    el contenido real del mensaje junto al badge del nombre del template.
    """
    import json as _json
    from pathlib import Path as _Path
    params = list(params or [])
    body = _TEMPLATE_BODIES.get(name)
    placeholder_fmt = "dict"  # {1}, {2}...

    if not body:
        # Intentar leer desde JSON en disco
        try:
            _tpl_path = (
                _Path(__file__).parent.parent
                / "templates" / "whatsapp_templates" / f"{name}.json"
            )
            if _tpl_path.exists():
                _tpl = _json.loads(_tpl_path.read_text())
                body = next(
                    (c["text"] for c in _tpl.get("components", []) if c.get("type") == "BODY"),
                    None,
                )
                placeholder_fmt = "json"  # {{1}}, {{2}}...
        except Exception as _e:
            log.warning("render_template_body: error leyendo JSON template=%s: %s", name, _e)

    if not body:
        if params:
            return f"[template: {name}] {' · '.join(str(p) for p in params)}"
        return f"[template: {name}]"

    out = body
    for i, p in enumerate(params, start=1):
        if placeholder_fmt == "json":
            out = out.replace("{{" + str(i) + "}}", str(p))
        else:
            out = out.replace("{" + str(i) + "}", str(p))
    return f"[template: {name}]\n{out}"


async def send_messenger(psid: str, body: str):
    """Envía mensaje de texto a un usuario de Facebook Messenger vía Graph API.
    Messenger rechaza mensajes > 1000 chars: divide en chunks automáticamente.
    Dedupe: skip si el mismo body se envió a este psid en los últimos 2 min."""
    body = _final_phone_guard(body)
    body = _normalize_markdown_for_chat(body)
    if _is_dupe_outbound(f"fb_{psid}", body):
        log.info("dedupe outbound skipped fb=%s len=%d", psid, len(body))
        return
    page_id = META_PAGE_ID or "me"
    url = f"https://graph.facebook.com/v22.0/{page_id}/messages"
    token = META_MESSENGER_TOKEN or META_ACCESS_TOKEN or META_PAGE_ACCESS_TOKEN
    for chunk in _split_long_msg(body, limit=900):
        for attempt in range(2):
            try:
                client = _get_meta_client()
                r = await client.post(
                    url,
                    headers={"Authorization": f"Bearer {token}"},
                    json={"recipient": {"id": psid}, "message": {"text": chunk}},
                )
                if r.status_code == 200:
                    break
                log.error("Messenger API intento %d → %s: %s", attempt + 1, r.status_code, r.text[:200])
            except (httpx.TimeoutException, httpx.NetworkError) as e:
                log.error("Messenger API intento %d error: %s", attempt + 1, e)
