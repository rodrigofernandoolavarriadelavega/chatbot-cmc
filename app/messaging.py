"""Messaging utilities — WhatsApp, Instagram, Facebook Messenger, Whisper."""
import asyncio
import logging

import httpx

from config import (META_ACCESS_TOKEN, META_PHONE_NUMBER_ID,
                    META_PAGE_ACCESS_TOKEN, INSTAGRAM_USER_ID, META_PAGE_ID,
                    OPENAI_API_KEY)

log = logging.getLogger("bot")

META_API_URL = f"https://graph.facebook.com/v22.0/{META_PHONE_NUMBER_ID}/messages"


async def _post_meta(payload: dict):
    """POST a Meta Cloud API con 1 reintento."""
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.post(
                    META_API_URL,
                    headers={"Authorization": f"Bearer {META_ACCESS_TOKEN}"},
                    json=payload,
                )
            if r.status_code == 200:
                return
            log.error("Meta API intento %d → %s: %s", attempt + 1, r.status_code, r.text[:200])
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            log.error("Meta API intento %d error red: %s", attempt + 1, e)
        if attempt == 0:
            await asyncio.sleep(2)


async def send_whatsapp(to: str, body: str):
    """Envía mensaje de texto vía Meta Cloud API."""
    await _post_meta({
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": body},
    })


async def send_whatsapp_interactive(to: str, interactive: dict):
    """Envía mensaje interactivo (botones o lista) vía Meta Cloud API."""
    await _post_meta({
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": interactive,
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


async def send_instagram(igsid: str, body: str):
    """Envía mensaje de texto a un usuario de Instagram vía Graph API."""
    if not INSTAGRAM_USER_ID:
        log.error("INSTAGRAM_USER_ID no configurado en .env")
        return
    url = f"https://graph.facebook.com/v22.0/{INSTAGRAM_USER_ID}/messages"
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.post(
                    url,
                    headers={"Authorization": f"Bearer {META_PAGE_ACCESS_TOKEN}"},
                    json={"recipient": {"id": igsid}, "message": {"text": body}},
                )
            if r.status_code == 200:
                return
            log.error("Instagram API intento %d → %s: %s", attempt + 1, r.status_code, r.text[:200])
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            log.error("Instagram API intento %d error: %s", attempt + 1, e)


async def send_messenger(psid: str, body: str):
    """Envía mensaje de texto a un usuario de Facebook Messenger vía Graph API."""
    page_id = META_PAGE_ID or "me"
    url = f"https://graph.facebook.com/v22.0/{page_id}/messages"
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.post(
                    url,
                    headers={"Authorization": f"Bearer {META_PAGE_ACCESS_TOKEN}"},
                    json={"recipient": {"id": psid}, "message": {"text": body}},
                )
            if r.status_code == 200:
                return
            log.error("Messenger API intento %d → %s: %s", attempt + 1, r.status_code, r.text[:200])
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            log.error("Messenger API intento %d error: %s", attempt + 1, e)
