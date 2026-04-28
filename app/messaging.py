"""Messaging utilities — WhatsApp, Instagram, Facebook Messenger, Whisper."""
import asyncio
import logging

import httpx

from config import (META_ACCESS_TOKEN, META_PHONE_NUMBER_ID,
                    META_PAGE_ACCESS_TOKEN, META_MESSENGER_TOKEN,
                    INSTAGRAM_USER_ID, META_PAGE_ID,
                    OPENAI_API_KEY)

log = logging.getLogger("bot")

def _normalize_markdown_for_chat(body: str) -> str:
    """Convierte **bold** → *bold* para WhatsApp/IG/FB (Meta renderer)."""
    import re as _re_nm
    return _re_nm.sub(r"\*\*([^*]+)\*\*", r"*\1*", body or "")



META_API_URL = f"https://graph.facebook.com/v22.0/{META_PHONE_NUMBER_ID}/messages"


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
            async with httpx.AsyncClient(timeout=10) as client:
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


def _final_phone_guard(text: str) -> str:
    """Última defensa antes de enviar al canal. Si por algún path el número
    personal se filtró sin pasar por _scrub_telefonos, lo capturamos acá y
    loggeamos warning para detectar regresiones."""
    if not text:
        return text
    if _RX_PERSONAL_LEAK.search(text):
        log.warning("PHONE_LEAK_GUARD personal_number_caught snippet=%r",
                    text[:160])
        text = _RX_PERSONAL_LEAK.sub(_TEL_CMC_WA_GUARD, text)
    return text


async def send_whatsapp(to: str, body: str) -> str | None:
    """Envía mensaje de texto vía Meta Cloud API. Retorna wamid o None si falla.
    Si el mismo body fue enviado a `to` en los últimos 2 min, skip (dedupe)."""
    if not body or not body.strip():
        return None
    body = _final_phone_guard(body)
    body = _normalize_markdown_for_chat(body)
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
        async with httpx.AsyncClient(timeout=10) as client:
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


async def send_whatsapp_template(to: str, template_name: str,
                                  body_params: list[str] | None = None,
                                  button_payloads: list[str] | None = None,
                                  language: str = "es"):
    """Envía un Message Template aprobado por Meta.

    Usar para TODOS los mensajes proactivos (fuera de ventana 24h):
    recordatorios, fidelización, lista de espera, alertas.

    Args:
        to: teléfono destino (sin +)
        template_name: nombre del template registrado en Meta
        body_params: lista de valores para {{1}}, {{2}}, etc.
        button_payloads: payloads para botones QUICK_REPLY (índice 0, 1, 2)
        language: código de idioma (default "es")
    """
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
        async with httpx.AsyncClient(timeout=20) as client:
            # Paso 1: obtener URL firmada del media
            meta = await client.get(
                f"https://graph.facebook.com/v22.0/{media_id}",
                headers={"Authorization": f"Bearer {META_ACCESS_TOKEN}"},
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
        async with httpx.AsyncClient(timeout=10) as client:
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
                async with httpx.AsyncClient(timeout=10) as client:
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
                async with httpx.AsyncClient(timeout=10) as client:
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
