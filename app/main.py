"""
Chatbot WhatsApp — Centro Médico Carampangue
Webhook de Meta Cloud API → FastAPI → Claude + Medilink
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

import asyncio
import logging
import logging.config
from contextlib import asynccontextmanager

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, Request, Response, Query
from fastapi.responses import HTMLResponse
from config import META_ACCESS_TOKEN, META_PHONE_NUMBER_ID, META_VERIFY_TOKEN, CMC_TELEFONO
from flows import handle_message
from reminders import enviar_recordatorios
from session import get_session, is_duplicate, reset_session, get_metricas

logging.config.dictConfig({
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "default": {"format": "%(asctime)s %(levelname)-8s %(name)s — %(message)s",
                    "datefmt": "%Y-%m-%d %H:%M:%S"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "default"},
    },
    "root": {"level": "INFO", "handlers": ["console"]},
    "loggers": {
        "medilink": {"level": "INFO"},
        "claude":   {"level": "INFO"},
        "bot":      {"level": "INFO"},
    },
})
log = logging.getLogger("bot")

scheduler = AsyncIOScheduler(timezone="America/Santiago")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Recordatorios: todos los días a las 9:00 AM hora Chile
    scheduler.add_job(
        lambda: asyncio.create_task(enviar_recordatorios(send_whatsapp)),
        CronTrigger(hour=9, minute=0),
        id="recordatorios_diarios",
        replace_existing=True,
    )
    scheduler.start()
    log.info("Scheduler iniciado — recordatorios diarios a las 09:00 CLT")
    yield
    scheduler.shutdown()


app = FastAPI(title="CMC WhatsApp Bot", lifespan=lifespan)

META_API_URL = f"https://graph.facebook.com/v19.0/{META_PHONE_NUMBER_ID}/messages"


async def send_whatsapp(to: str, body: str):
    """Envía mensaje de texto vía Meta Cloud API. Reintenta 1 vez ante fallos."""
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": body},
    }
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
            log.error("send_whatsapp to=%s intento %d → %s: %s",
                      to, attempt + 1, r.status_code, r.text[:200])
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            log.error("send_whatsapp to=%s intento %d error red: %s", to, attempt + 1, e)
        if attempt == 0:
            await asyncio.sleep(2)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/privacidad", response_class=HTMLResponse)
def privacidad():
    return """<!DOCTYPE html>
<html lang="es">
<head><meta charset="UTF-8"><title>Política de Privacidad — Centro Médico Carampangue</title>
<style>body{font-family:Arial,sans-serif;max-width:800px;margin:40px auto;padding:0 20px;color:#333;line-height:1.6}h1{color:#1a5276}h2{color:#2874a6;margin-top:30px}</style>
</head>
<body>
<h1>Política de Privacidad</h1>
<p><strong>Centro Médico Carampangue</strong> — Monsalve 102 esq. República, Carampangue, Chile<br>
Última actualización: marzo 2026</p>

<h2>1. Información que recopilamos</h2>
<p>A través de nuestro asistente de WhatsApp recopilamos: número de teléfono, RUT, nombre completo y los datos necesarios para agendar, modificar o cancelar citas médicas.</p>

<h2>2. Uso de la información</h2>
<p>Los datos recopilados se usan exclusivamente para gestionar citas médicas en el Centro Médico Carampangue. No compartimos su información con terceros salvo lo estrictamente necesario para prestar el servicio (sistema de agendamiento Medilink/HealthAtom).</p>

<h2>3. Retención de datos</h2>
<p>Los datos de sesión se conservan por 30 minutos de inactividad. Los registros de citas se mantienen según la normativa sanitaria chilena vigente.</p>

<h2>4. Seguridad</h2>
<p>Las comunicaciones a través de WhatsApp están cifradas de extremo a extremo por Meta. Los datos almacenados en nuestros servidores se protegen con controles de acceso adecuados.</p>

<h2>5. Derechos del usuario</h2>
<p>Puede solicitar acceso, rectificación o eliminación de sus datos contactándonos en: <strong>+56 9 8783 4148</strong> o en nuestra dirección física.</p>

<h2>6. Contacto</h2>
<p>Centro Médico Carampangue<br>
Monsalve 102 esq. República, Carampangue, Región del Biobío, Chile<br>
Teléfono: (41) 296 5226<br>
WhatsApp: +56 9 8783 4148</p>
</body>
</html>"""


@app.get("/metrics")
def metrics(dias: int = Query(30, ge=1, le=365)):
    """Métricas de conversación de los últimos N días."""
    return get_metricas(dias)


@app.get("/webhook")
def verify_webhook(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
):
    """Verificación del webhook por Meta."""
    if hub_mode == "subscribe" and hub_verify_token == META_VERIFY_TOKEN:
        return Response(content=hub_challenge, media_type="text/plain")
    return Response(status_code=403)


@app.post("/webhook")
async def webhook(request: Request):
    """Recibe mensajes de Meta Cloud API."""
    data = await request.json()

    try:
        entry = data["entry"][0]
        change = entry["changes"][0]["value"]

        if "messages" not in change:
            return Response(status_code=200)

        msg = change["messages"][0]

        if msg.get("type") != "text":
            return Response(status_code=200)

        phone = msg["from"]
        texto = msg["text"]["body"].strip()
        msg_id = msg.get("id", "")

        if msg_id and is_duplicate(msg_id):
            log.info("MSG duplicado ignorado id=%s from=%s", msg_id, phone)
            return Response(status_code=200)

        log.info("MSG from=%s id=%s text=%r", phone, msg_id, texto[:100])

        session = get_session(phone)
        try:
            respuesta = await handle_message(phone, texto, session)
        except Exception as e:
            log.error("Error inesperado procesando msg from=%s: %s", phone, e, exc_info=True)
            reset_session(phone)
            respuesta = (
                "Tuve un problema técnico 😕\n\n"
                "Por favor intenta de nuevo o llama a recepción:\n"
                f"📞 {CMC_TELEFONO}"
            )

        log.info("BOT to=%s state=%s reply=%r", phone, session.get("state", "?"), respuesta[:80])

        await send_whatsapp(phone, respuesta)

    except (KeyError, IndexError) as e:
        log.warning("Payload inesperado: %s | data=%s", e, str(data)[:200])

    return Response(status_code=200)
