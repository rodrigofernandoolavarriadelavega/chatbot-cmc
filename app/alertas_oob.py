"""alertas_oob.py — Canal de alerta fuera de banda (Telegram) + dead-man's switch.

Dos funciones públicas:

  alerta_oob(texto)         → envía por Telegram si TELEGRAM_ALERT_TOKEN +
                               TELEGRAM_ALERT_CHAT_ID están en env.
                               No-op (retorna False) si faltan las vars.
                               NO reemplaza WhatsApp; es canal adicional.

  ping_deadman(nombre)      → GET a HEALTHCHECKS_{NOMBRE}_URL si existe.
                               Fire-and-forget; no-op si falta la URL.
                               Cablear al FINAL exitoso de crons críticos.

Ambas funciones son seguras de llamar aunque fallen: capturan todas las
excepciones y retornan bool. Nunca tiran hacia arriba.

ACTIVAR (solo hay que pegar las llaves en .env):
  TELEGRAM_ALERT_TOKEN=<token del bot de Telegram>
  TELEGRAM_ALERT_CHAT_ID=<chat_id del grupo/canal>

  HEALTHCHECKS_RECORDATORIOS_URL=https://hc-ping.com/<uuid>
  HEALTHCHECKS_WAITLIST_URL=https://hc-ping.com/<uuid>
  (un UUID distinto por cada cron que quieras monitorear)
"""
from __future__ import annotations

import logging
import os

import httpx

log = logging.getLogger("bot")

# ── Telegram ──────────────────────────────────────────────────────────────────

_TELEGRAM_SEND_URL = "https://api.telegram.org/bot{token}/sendMessage"
_TELEGRAM_TIMEOUT  = 8.0   # segundos


async def alerta_oob(texto: str) -> bool:
    """Envía texto a Telegram si el env está configurado.

    Retorna True si el envío fue exitoso, False en cualquier otro caso
    (env no configurado, error de red, error API).
    """
    token   = os.getenv("TELEGRAM_ALERT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_ALERT_CHAT_ID", "").strip()

    if not token or not chat_id:
        return False   # no-op cuando el dueño aún no configuró las cuentas

    url = _TELEGRAM_SEND_URL.format(token=token)
    try:
        async with httpx.AsyncClient(timeout=_TELEGRAM_TIMEOUT) as client:
            resp = await client.post(url, json={
                "chat_id": chat_id,
                "text": texto,
                "parse_mode": "Markdown",
            })
            if resp.status_code == 200:
                return True
            log.warning("alerta_oob: Telegram respondió %s — %s", resp.status_code, resp.text[:200])
            return False
    except Exception as exc:  # noqa: BLE001
        log.warning("alerta_oob: error enviando a Telegram: %s", exc)
        return False


async def enviar_telegram(texto: str, header: str | None = None) -> bool:
    """Envío robusto a Telegram con formato. Intenta Markdown (negritas *…*,
    itálicas _…_ — mismo marcado que WhatsApp); si la API rechaza el parseo
    (entidad mal cerrada), reintenta como texto plano para no perder el aviso.
    Usado para espejar a Telegram los avisos que el bot manda al dueño."""
    token   = os.getenv("TELEGRAM_ALERT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_ALERT_CHAT_ID", "").strip()
    if not token or not chat_id:
        return False
    cuerpo = (f"{header}\n\n{texto}" if header else texto)[:4000]
    url = _TELEGRAM_SEND_URL.format(token=token)
    base = {"chat_id": chat_id, "text": cuerpo, "disable_web_page_preview": True}
    try:
        async with httpx.AsyncClient(timeout=_TELEGRAM_TIMEOUT) as client:
            resp = await client.post(url, json={**base, "parse_mode": "Markdown"})
            if resp.status_code == 200:
                return True
            # Markdown roto → reintento en texto plano (mejor llega feo que no llega)
            resp = await client.post(url, json=base)
            return resp.status_code == 200
    except Exception as exc:  # noqa: BLE001
        log.warning("enviar_telegram: error enviando a Telegram: %s", exc)
        return False


# ── Dead-man's switch (healthchecks.io) ──────────────────────────────────────

_DEADMAN_TIMEOUT = 5.0   # segundos


async def ping_deadman(nombre: str) -> bool:
    """Hace GET a HEALTHCHECKS_{NOMBRE}_URL si está configurado.

    Llamar al FINAL exitoso del cron cuyo nombre se pasa (ej. "RECORDATORIOS").
    Si el cron deja de correr, healthchecks.io envía alerta por correo/Slack.
    No-op si la URL no está en env. Fire-and-forget seguro (no lanza excepciones).

    nombre: string MAYUSCULA que se usa como sufijo de la var de entorno,
            ej "RECORDATORIOS" → lee HEALTHCHECKS_RECORDATORIOS_URL.
    """
    env_key = f"HEALTHCHECKS_{nombre.upper()}_URL"
    url = os.getenv(env_key, "").strip()
    if not url:
        return False

    try:
        async with httpx.AsyncClient(timeout=_DEADMAN_TIMEOUT) as client:
            resp = await client.get(url)
            if resp.status_code == 200:
                return True
            log.debug("ping_deadman[%s]: respuesta inesperada %s", nombre, resp.status_code)
            return False
    except Exception as exc:  # noqa: BLE001
        log.warning("ping_deadman[%s]: error: %s", nombre, exc)
        return False


async def enviar_telegram_foto(imagen: bytes, caption: str = "") -> bool:
    """Envía una FOTO a Telegram (sendPhoto, multipart). Caption máx 1024
    chars — el texto largo va aparte con enviar_telegram(). Usado para
    reenviar al médico los resultados de exámenes que llegan por WhatsApp
    (ver docs_clinicos / main.py). Retorna False si no hay credenciales."""
    token   = os.getenv("TELEGRAM_ALERT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_ALERT_CHAT_ID", "").strip()
    if not token or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                url,
                data={"chat_id": chat_id, "caption": caption[:1024]},
                files={"photo": ("documento.jpg", imagen, "image/jpeg")},
            )
            if resp.status_code == 200:
                return True
            log.warning("enviar_telegram_foto: %s — %s",
                        resp.status_code, resp.text[:200])
            return False
    except Exception as exc:  # noqa: BLE001
        log.warning("enviar_telegram_foto error: %s", exc)
        return False
