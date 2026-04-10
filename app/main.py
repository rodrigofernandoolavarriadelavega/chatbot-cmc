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
from pathlib import Path

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, Request, Response, Query, HTTPException, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from config import (META_ACCESS_TOKEN, META_PHONE_NUMBER_ID, META_VERIFY_TOKEN,
                    META_PAGE_ACCESS_TOKEN, INSTAGRAM_USER_ID, META_PAGE_ID,
                    CMC_TELEFONO, ADMIN_TOKEN, ORTODONCIA_TOKEN)
from flows import handle_message
from reminders import enviar_recordatorios
from fidelizacion import (enviar_seguimiento_postconsulta, enviar_reactivacion_pacientes,
                          enviar_adherencia_kine, enviar_recordatorio_control,
                          enviar_crosssell_kine)
from medilink import (buscar_paciente, crear_paciente, buscar_primer_dia,
                      buscar_slots_dia, crear_cita, listar_citas_paciente,
                      cancelar_cita, get_citas_seguimiento_mes, sync_citas_dia,
                      sync_ortodoncia_rango,
                      SEGUIMIENTO_ESPECIALIDADES, PROFESIONALES, ESPECIALIDADES_MAP)
from session import (get_session, is_duplicate, reset_session, save_session, get_metricas,
                     log_message, get_messages, get_conversations, log_event,
                     get_sesiones_abandonadas, get_tags, save_tag, delete_tag, search_messages,
                     get_kine_tracking_all, save_kine_tracking,
                     get_ortodoncia_pacientes, set_ortodoncia_tipo, get_ortodoncia_sync_max_fecha,
                     purge_old_data)

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


# ── Rate limiter en memoria (sliding window por teléfono) ────────────────────
from collections import deque
from time import monotonic

_RATE_WINDOW_SEC = 60
_RATE_MAX_MSGS   = 30  # mensajes por minuto por número
_rate_buckets: dict[str, deque] = {}


def _rate_limited(phone: str) -> bool:
    """True si el número superó _RATE_MAX_MSGS mensajes en la última ventana."""
    now = monotonic()
    bucket = _rate_buckets.get(phone)
    if bucket is None:
        bucket = deque()
        _rate_buckets[phone] = bucket
    # Descartar entradas fuera de la ventana
    while bucket and now - bucket[0] > _RATE_WINDOW_SEC:
        bucket.popleft()
    if len(bucket) >= _RATE_MAX_MSGS:
        return True
    bucket.append(now)
    # Limpieza oportunista: si el dict crece demasiado, purgar buckets vacíos
    if len(_rate_buckets) > 5000:
        for k in list(_rate_buckets.keys()):
            b = _rate_buckets[k]
            while b and now - b[0] > _RATE_WINDOW_SEC:
                b.popleft()
            if not b:
                _rate_buckets.pop(k, None)
    return False


async def _enviar_reenganche():
    """Reenganche a pacientes que abandonaron un flujo activo hace 10-60 minutos."""
    sesiones = get_sesiones_abandonadas()
    for s in sesiones:
        phone = s["phone"]
        state = s["state"]
        data  = s["data"]
        especialidad = data.get("especialidad", "")
        if state == "WAIT_SLOT":
            msg = (
                f"Hola 😊 ¿Seguimos con tu hora{' de *' + especialidad + '*' if especialidad else ''}?\n\n"
                "Escribe *menu* para retomar desde el inicio."
            )
        else:
            msg = (
                "Hola 😊 Quedaste a punto de confirmar tu hora.\n\n"
                "Escribe *menu* para retomar cuando quieras."
            )
        await send_whatsapp(phone, msg)
        data["reenganche_sent"] = True
        save_session(phone, state, data)
        log.info("Reenganche enviado → %s (estado: %s)", phone, state)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Recordatorios: todos los días a las 9:00 AM hora Chile
    scheduler.add_job(
        lambda: asyncio.create_task(enviar_recordatorios(send_whatsapp)),
        CronTrigger(hour=9, minute=0),
        id="recordatorios_diarios",
        replace_existing=True,
    )
    # Reenganche: cada 5 minutos revisa sesiones abandonadas
    scheduler.add_job(
        lambda: asyncio.create_task(_enviar_reenganche()),
        "interval", minutes=5,
        id="reenganche",
        replace_existing=True,
    )
    # Post-consulta: todos los días a las 10:00 AM (citas de ayer que fueron atendidas)
    scheduler.add_job(
        lambda: asyncio.create_task(enviar_seguimiento_postconsulta(send_whatsapp)),
        CronTrigger(hour=10, minute=0),
        id="seguimiento_postconsulta",
        replace_existing=True,
    )
    # Reactivación: todos los lunes a las 10:30 AM (pacientes sin volver en 30–90 días)
    scheduler.add_job(
        lambda: asyncio.create_task(enviar_reactivacion_pacientes(send_whatsapp)),
        CronTrigger(day_of_week="mon", hour=10, minute=30),
        id="reactivacion_pacientes",
        replace_existing=True,
    )
    # Adherencia kine: diario a las 11:00 AM (kine pacientes con gap de 4+ días)
    scheduler.add_job(
        lambda: asyncio.create_task(enviar_adherencia_kine(send_whatsapp)),
        CronTrigger(hour=11, minute=0),
        id="adherencia_kine",
        replace_existing=True,
    )
    # Control por especialidad: diario a las 11:30 AM (nutrición, psicología, cardiología, etc.)
    scheduler.add_job(
        lambda: asyncio.create_task(enviar_recordatorio_control(send_whatsapp)),
        CronTrigger(hour=11, minute=30),
        id="control_especialidad",
        replace_existing=True,
    )
    # Cross-sell kine: miércoles a las 10:30 AM (medicina/traumatología → kine)
    scheduler.add_job(
        lambda: asyncio.create_task(enviar_crosssell_kine(send_whatsapp)),
        CronTrigger(day_of_week="wed", hour=10, minute=30),
        id="crosssell_kine",
        replace_existing=True,
    )
    # Sync caché de citas: diario a las 23:50 CLT (sync del día en curso)
    def _sync_hoy():
        from datetime import date
        from zoneinfo import ZoneInfo
        hoy = date.today().strftime("%Y-%m-%d")  # servidor puede estar en UTC, sync igual
        ids_todos = list({i for cfg in SEGUIMIENTO_ESPECIALIDADES.values() for i in cfg["ids"]})
        asyncio.create_task(sync_citas_dia(hoy, ids_todos))
    scheduler.add_job(
        _sync_hoy,
        CronTrigger(hour=2, minute=50),  # 23:50 CLT = 02:50 UTC
        id="sync_citas_cache",
        replace_existing=True,
    )
    # Retención: domingos 04:00 CLT borra mensajes > 90 días y eventos > 180 días
    scheduler.add_job(
        purge_old_data,
        CronTrigger(day_of_week="sun", hour=4, minute=0),
        id="purge_old_data",
        replace_existing=True,
    )
    scheduler.start()
    log.info(
        "Scheduler iniciado — recordatorios 09:00 · post-consulta 10:00 · "
        "reactivación lun 10:30 · adherencia kine 11:00 · control 11:30 · "
        "cross-sell kine mié 10:30 · sync caché 23:50 · purge dom 04:00"
    )
    yield
    scheduler.shutdown()


app = FastAPI(title="CMC WhatsApp Bot", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent.parent / "static")), name="static")

# CORS restrictivo: solo el propio dominio del panel y preview local
_ALLOWED_ORIGINS = [
    "https://agentecmc.cl",
    "http://agentecmc.cl",
    "http://157.245.13.107:8001",
    "http://localhost:8001",
    "http://127.0.0.1:8001",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

META_API_URL = f"https://graph.facebook.com/v22.0/{META_PHONE_NUMBER_ID}/messages"

# Reexportamos headers para healthcheck (evita import circular en runtime)
from config import MEDILINK_TOKEN as _MEDILINK_TOKEN
HEADERS_MEDILINK = {"Authorization": f"Token {_MEDILINK_TOKEN}"}


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


_ADMIN_HTML = r'''<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<title>Panel Recepción — CMC</title>
<style>
:root {
  --bg: #f1f5f9;
  --surface: #ffffff;
  --border: #e2e8f0;
  --text: #1e293b;
  --text-2: #475569;
  --text-3: #94a3b8;
  --primary: #1172AB;
  --red: #ef4444;
  --red-soft: #fef2f2;
  --amber: #f59e0b;
  --amber-soft: #fffbeb;
  --green: #10b981;
  --green-soft: #f0fdf4;
  --blue: #3b82f6;
  --blue-soft: #eff6ff;
  --purple: #8b5cf6;
  --purple-soft: #f5f3ff;
  --slate: #94a3b8;
  --radius: 10px;
  --shadow: 0 1px 3px rgba(0,0,0,.08),0 1px 2px rgba(0,0,0,.06);
  --shadow-md: 0 4px 6px rgba(0,0,0,.07),0 2px 4px rgba(0,0,0,.06);
}
* { margin:0; padding:0; box-sizing:border-box; }
body {
  background: var(--bg); color: var(--text);
  font-family: "Inter", system-ui, sans-serif;
  height: 100vh; overflow: hidden; font-size: 13px;
}
/* TOPBAR */
.topbar {
  height: 54px; background: var(--surface);
  border-bottom: 1px solid var(--border);
  display: flex; align-items: center; padding: 0 20px; gap: 14px;
  box-shadow: var(--shadow); z-index: 10; position: relative;
}
.brand { display: flex; align-items: center; gap: 10px; }
.brand-icon {
  width: 34px; height: 34px; border-radius: 9px;
  background: var(--primary); display: flex; align-items: center; justify-content: center;
  flex-shrink: 0;
}
.brand-text h1 { font-size: 13px; font-weight: 700; color: var(--text); }
.brand-text p { font-size: 11px; color: var(--text-3); }
.topbar-pills { display: flex; gap: 8px; margin-left: auto; align-items: center; }
.pill {
  display: flex; align-items: center; gap: 6px;
  padding: 5px 12px; border-radius: 20px;
  font-size: 12px; font-weight: 500;
  border: 1px solid var(--border); background: var(--bg); color: var(--text-2);
}
.pill strong { color: var(--text); font-weight: 700; }
.pill.red { background: var(--red-soft); border-color: #fca5a5; color: var(--red); }
.pill.red strong { color: var(--red); }
.pill.amber { background: var(--amber-soft); border-color: #fcd34d; color: #92400e; }
.pill.amber strong { color: #92400e; }
.live-dot { width:7px; height:7px; border-radius:50%; background:var(--green); animation:blink 2s infinite; }
@keyframes blink { 0%,100%{opacity:1} 50%{opacity:.2} }
/* LAYOUT 3 columnas */
.layout { display: flex; height: calc(100vh - 54px - var(--alert-h, 0px)); }
.progress-step {
  display: flex; align-items: center; gap: 10px;
  padding: 7px 10px; border-radius: 8px; margin-bottom: 4px;
  font-size: 12px; font-weight: 500;
}
.progress-step.done { background: #f0fdf4; color: #15803d; }
.progress-step.pending { background: #f8fafc; color: #94a3b8; }
.progress-step-icon {
  width: 22px; height: 22px; border-radius: 50%;
  display: flex; align-items: center; justify-content: center;
  font-size: 11px; font-weight: 700; flex-shrink: 0;
}
.progress-step.done .progress-step-icon { background: #dcfce7; color: #15803d; }
.progress-step.pending .progress-step-icon { background: #e2e8f0; color: #94a3b8; }
/* COL 1: FILTROS */
.col-filters {
  width: 218px; min-width: 218px; background: var(--surface);
  border-right: 1px solid var(--border);
  display: flex; flex-direction: column; overflow-y: auto;
}
.filter-search { padding: 12px 12px 10px; border-bottom: 1px solid var(--border); }
.search-box {
  display: flex; align-items: center; gap: 6px;
  background: var(--bg); border: 1px solid var(--border);
  border-radius: 8px; padding: 7px 10px;
}
.search-box input {
  border: none; background: transparent; font-family: inherit;
  font-size: 12px; color: var(--text); width: 100%; outline: none;
}
.search-box input::placeholder { color: var(--text-3); }
.filter-section { padding: 14px 12px 10px; }
.filter-section + .filter-section { border-top: 1px solid var(--border); }
.filter-label {
  font-size: 10px; font-weight: 700; letter-spacing: .8px;
  color: var(--text-3); text-transform: uppercase; margin-bottom: 8px;
}
.state-btn {
  display: flex; align-items: center; gap: 9px; width: 100%;
  padding: 9px 10px; border-radius: 8px; border: none;
  background: transparent; cursor: pointer; font-family: inherit;
  font-size: 12px; font-weight: 500; color: var(--text-2);
  margin-bottom: 2px; transition: background .15s; text-align: left;
}
.state-btn:hover { background: var(--bg); }
.state-btn.active { background: var(--bg); color: var(--text); font-weight: 600; }
.sdot { width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0; }
.scount {
  margin-left: auto; font-size: 11px; font-weight: 700;
  padding: 1px 7px; border-radius: 10px;
  background: rgba(0,0,0,.06); color: var(--text-3);
}
.state-btn.active .scount { color: var(--text); }
.esp-btn {
  display: flex; align-items: center; gap: 7px; width: 100%;
  padding: 7px 10px; border-radius: 8px;
  border: 1px solid transparent; background: transparent;
  cursor: pointer; font-family: inherit; font-size: 12px;
  font-weight: 500; color: var(--text-2); margin-bottom: 2px;
  transition: all .15s; text-align: left;
}
.esp-btn:hover { background: var(--bg); }
.esp-btn.active { background: #f0fdf4; border-color: #86efac; color: #15803d; font-weight: 600; }
.esp-btn-count { margin-left: auto; font-size: 11px; font-weight: 700; color: var(--text-3); }
/* COL 2: CONVERSACIONES */
.col-convs {
  width: 288px; min-width: 288px; background: var(--bg);
  border-right: 1px solid var(--border);
  display: flex; flex-direction: column; overflow: hidden;
}
.col-convs-hdr {
  padding: 12px 14px 8px; background: var(--surface);
  border-bottom: 1px solid var(--border);
  font-size: 13px; font-weight: 700; color: var(--text);
}
.conv-scroll { flex: 1; overflow-y: auto; padding: 8px 10px 16px; }
.group-label {
  font-size: 10px; font-weight: 700; letter-spacing: .6px;
  text-transform: uppercase; padding: 10px 4px 5px; margin-top: 2px;
}
.conv-card {
  background: var(--surface); border-radius: var(--radius);
  border: 1px solid var(--border); border-left-width: 4px;
  padding: 11px 12px; margin-bottom: 6px; cursor: pointer;
  transition: all .15s; box-shadow: var(--shadow);
}
.conv-card:hover { box-shadow: var(--shadow-md); transform: translateY(-1px); }
.conv-card.selected { border-color: var(--primary) !important; box-shadow: 0 0 0 2px rgba(17,114,171,.15); }
.card-top { display: flex; align-items: center; gap: 8px; margin-bottom: 4px; }
.cdot { width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0; }
.cname { font-size: 13px; font-weight: 600; color: var(--text); flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.ctime { font-size: 11px; color: var(--text-3); flex-shrink: 0; }
.cstate { font-size: 11px; font-weight: 600; margin-bottom: 2px; }
.cesp { font-size: 11px; color: #16a34a; font-weight: 500; margin-bottom: 3px; }
.cpreview { font-size: 11px; color: var(--text-3); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; margin-bottom: 5px; }
.cbadges { display: flex; gap: 5px; flex-wrap: wrap; align-items: center; }
.badge { font-size: 10px; font-weight: 700; padding: 2px 7px; border-radius: 6px; border: 1px solid transparent; }
.badge-unread { background: var(--red); color: #fff; border-radius: 10px; min-width: 18px; text-align: center; }
.badge-urgent { background: var(--red-soft); color: var(--red); border-color: #fca5a5; }
.badge-warn { background: var(--amber-soft); color: #92400e; border-color: #fcd34d; }
.badge-prob { background: var(--green-soft); color: #15803d; border-color: #86efac; }
.no-results { text-align: center; color: var(--text-3); font-size: 12px; padding: 30px 0; }
/* COL 3: CHAT + CONTEXTO */
.col-main { flex: 1; display: flex; overflow: hidden; }
.chat-panel { flex: 1; display: flex; flex-direction: column; background: var(--surface); overflow: hidden; border-right: 1px solid var(--border); }
.chat-empty {
  flex: 1; display: flex; flex-direction: column;
  align-items: center; justify-content: center;
  color: var(--text-3); gap: 10px;
}
.chat-empty-icon { font-size: 48px; opacity: .4; }
.chat-empty h3 { font-size: 15px; font-weight: 600; color: var(--text-2); }
.chat-empty p { font-size: 12px; }
.chat-active { flex: 1; display: flex; flex-direction: column; min-height: 0; overflow: hidden; }
.chat-header {
  padding: 11px 16px; border-bottom: 1px solid var(--border);
  display: flex; align-items: center; gap: 10px; flex-shrink: 0;
}
.ch-avatar {
  width: 36px; height: 36px; border-radius: 50%;
  background: var(--primary); color: #fff;
  display: flex; align-items: center; justify-content: center;
  font-size: 13px; font-weight: 700; flex-shrink: 0;
}
.ch-info { flex: 1; }
.ch-name { font-size: 14px; font-weight: 700; color: var(--text); }
.ch-sub { font-size: 11px; color: var(--text-3); }
.ch-actions { display: flex; gap: 7px; align-items: center; flex-shrink: 0; }
.state-tag {
  display: inline-flex; align-items: center; font-size: 11px;
  font-weight: 600; padding: 3px 9px; border-radius: 6px;
  border: 1px solid transparent;
}
.tag-red { background: var(--red-soft); color: var(--red); border-color: #fca5a5; }
.tag-amber { background: var(--amber-soft); color: #92400e; border-color: #fcd34d; }
.tag-green { background: var(--green-soft); color: #15803d; border-color: #86efac; }
.tag-slate { background: #f8fafc; color: var(--text-3); border-color: var(--border); }
.tag-blue { background: var(--blue-soft); color: #1d4ed8; border-color: #bfdbfe; }
.tag-purple { background: var(--purple-soft); color: #6d28d9; border-color: #ddd6fe; }
.btn {
  display: inline-flex; align-items: center; gap: 5px;
  padding: 6px 12px; border-radius: 7px; border: 1px solid transparent;
  font-family: inherit; font-size: 12px; font-weight: 600;
  cursor: pointer; transition: all .15s;
}
.btn-primary { background: var(--primary); color: #fff; border-color: var(--primary); }
.btn-primary:hover { background: #0d5f8e; }
.btn-danger { background: var(--red); color: #fff; border-color: var(--red); }
.btn-danger:hover { background: #dc2626; }
.btn-outline { background: transparent; border-color: var(--border); color: var(--text-2); }
.btn-outline:hover { background: var(--bg); }
.btn-full { width: 100%; justify-content: center; }
.takeover-banner {
  margin: 10px 16px 0; padding: 9px 14px;
  background: #fff3cd; border: 1px solid #ffc107;
  border-radius: 8px; font-size: 12px;
  display: flex; align-items: center; gap: 8px; color: #664d03; flex-shrink: 0;
}
.takeover-banner.hidden { display: none; }
.quick-replies { display: flex; flex-wrap: wrap; gap: 5px; padding: 8px 14px 0; flex-shrink: 0; }
.quick-replies.hidden { display: none; }
.qr-btn {
  font-size: 11px; padding: 4px 10px; border-radius: 6px;
  border: 1px solid var(--border); background: var(--bg);
  color: var(--text-2); cursor: pointer; font-family: inherit;
  font-weight: 500; transition: all .15s;
}
.qr-btn:hover { background: var(--primary); color: #fff; border-color: var(--primary); }
.chat-messages {
  flex: 1; overflow-y: auto; padding: 16px;
  display: flex; flex-direction: column; gap: 6px;
  background: var(--bg);
}
.msg-row { display: flex; }
.msg-row.in { justify-content: flex-start; }
.msg-row.out { justify-content: flex-end; }
.msg-bubble {
  max-width: 75%; padding: 9px 13px; border-radius: 12px;
  font-size: 12px; line-height: 1.5; word-break: break-word;
}
.msg-row.in .msg-bubble { background: var(--surface); color: var(--text); border-bottom-left-radius: 3px; border: 1px solid var(--border); }
.msg-row.out .msg-bubble { background: #dcf8c6; color: #1a3a1a; border-bottom-right-radius: 3px; }
.msg-row.out.recep .msg-bubble { background: #fff3cd; color: #664d03; }
.msg-meta { font-size: 10px; color: var(--text-3); margin-top: 3px; padding: 0 4px; }
.msg-row.out .msg-meta { text-align: right; }
.state-sep { text-align: center; margin: 6px 0; }
.state-pill { font-size: 10px; padding: 2px 10px; border-radius: 20px; background: var(--surface); border: 1px solid var(--border); color: var(--text-3); }
.reply-bar { padding: 10px 14px; border-top: 1px solid var(--border); background: var(--surface); flex-shrink: 0; }
.reply-bar.hidden { display: none; }
.reply-textarea {
  width: 100%; resize: none; border: 1px solid var(--border);
  border-radius: 8px; padding: 8px 10px; font-family: inherit;
  font-size: 12px; color: var(--text); outline: none;
  background: var(--bg); max-height: 80px;
}
.reply-textarea:focus { border-color: var(--primary); background: var(--surface); }
.reply-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 8px; }
/* PANEL CONTEXTO */
.ctx-panel {
  width: 238px; min-width: 238px; background: var(--surface);
  display: flex; flex-direction: column; overflow-y: auto;
}
.ctx-empty {
  flex: 1; display: flex; flex-direction: column;
  align-items: center; justify-content: center;
  gap: 8px; color: var(--text-3); padding: 20px; text-align: center;
}
.ctx-content { display: none; }
.ctx-sec { padding: 14px; border-bottom: 1px solid var(--border); }
.ctx-sec:last-child { border-bottom: none; }
.ctx-sec-title { font-size: 10px; font-weight: 700; letter-spacing: .6px; text-transform: uppercase; color: var(--text-3); margin-bottom: 10px; }
.ctx-avatar {
  width: 52px; height: 52px; border-radius: 50%;
  background: linear-gradient(135deg, var(--primary), #4FBECE);
  color: #fff; font-size: 18px; font-weight: 700;
  display: flex; align-items: center; justify-content: center;
  margin: 0 auto 10px;
}
.ctx-name { font-size: 15px; font-weight: 700; text-align: center; color: var(--text); margin-bottom: 3px; }
.ctx-sub { font-size: 11px; color: var(--text-3); text-align: center; margin-bottom: 10px; }
.ctx-state-wrap { text-align: center; }
.flow-row {
  display: flex; align-items: flex-start; gap: 8px;
  padding: 8px; background: var(--bg); border-radius: 8px; margin-bottom: 6px;
}
.flow-row-icon { font-size: 14px; flex-shrink: 0; }
.flow-row-label { font-size: 10px; color: var(--text-3); font-weight: 500; margin-bottom: 1px; }
.flow-row-value { font-size: 12px; font-weight: 600; color: var(--text); }
.ctx-notes {
  width: 100%; resize: none; border: 1px solid var(--border);
  border-radius: 7px; padding: 7px 9px; font-family: inherit;
  font-size: 11px; color: var(--text); outline: none;
  min-height: 70px; background: var(--bg);
}
.ctx-notes:focus { border-color: var(--primary); background: var(--surface); }
.ctx-actions { display: flex; flex-direction: column; gap: 6px; }
/* Scrollbar */
::-webkit-scrollbar { width: 4px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: var(--border); border-radius: 2px; }
</style>
</head>
<body>

<!-- TOPBAR -->
<div class="topbar">
  <div class="brand">
    <img src="/static/logo.png" alt="Centro Médico Carampangue" style="height:36px;object-fit:contain;">
    <div class="brand-text" style="margin-left:4px;">
      <p>Panel de Recepción · WhatsApp Bot</p>
    </div>
  </div>
  <div class="topbar-pills">
    <div class="pill"><strong id="s-total">—</strong>&nbsp;conversaciones</div>
    <div class="pill amber"><strong id="s-flujo">—</strong>&nbsp;en flujo</div>
    <div class="pill" id="pill-esperando"><strong id="s-takeover">—</strong>&nbsp;esperando atención</div>
    <div class="pill"><div class="live-dot"></div>&nbsp;Actualizado <span id="last-refresh">—</span></div>
    <button class="btn btn-primary" onclick="abrirModalAgendar()" style="margin-left:8px;font-size:12px;padding:6px 14px;border-radius:8px;">+ Nueva Cita</button>
    <button class="btn" onclick="abrirBusquedaGlobal()" style="margin-left:6px;font-size:12px;padding:6px 14px;border-radius:8px;background:#f0f9ff;color:#0369a1;border:1px solid #bae6fd;">🔍 Buscar</button>
    <button class="btn" onclick="abrirModalAnular()" style="margin-left:6px;font-size:12px;padding:6px 14px;border-radius:8px;background:#fee2e2;color:#dc2626;border:1px solid #fca5a5;">✕ Anular Hora</button>
    <button class="btn" onclick="abrirModalKine()" style="margin-left:6px;font-size:12px;padding:6px 14px;border-radius:8px;background:#f0fdf4;color:#15803d;border:1px solid #86efac;">📋 Pacientes en Control</button>
    <button class="btn" onclick="abrirModalOrtodoncia()" style="margin-left:6px;font-size:12px;padding:6px 14px;border-radius:8px;background:#fdf4ff;color:#7e22ce;border:1px solid #d8b4fe;">🦷 Ortodoncia</button>
  </div>
</div>

<!-- MODAL KINESIOLOGÍA -->
<div id="modal-kine" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:1000;align-items:flex-start;justify-content:center;padding-top:40px;padding-bottom:40px;overflow-y:auto;">
  <div style="background:#fff;border-radius:14px;width:900px;max-width:96vw;box-shadow:0 20px 60px rgba(0,0,0,.2);position:relative;margin:auto;">
    <div style="padding:20px 24px 14px;border-bottom:1px solid #e2e8f0;display:flex;align-items:center;gap:12px;flex-wrap:wrap;">
      <span style="font-size:16px;font-weight:700;color:#15803d;">📋 Pacientes en Tratamiento</span>
      <select id="kine-esp-select" onchange="kineEspActual=this.value;cargarKine()" style="border:1px solid #e2e8f0;border-radius:8px;padding:5px 10px;font-size:13px;color:#334155;">
        <option value="kinesiologia">🦵 Kinesiología</option>
        <option value="ortodoncia">🦷 Ortodoncia</option>
        <option value="psicologia">🧠 Psicología</option>
        <option value="nutricion">🥗 Nutrición</option>
      </select>
      <div style="display:flex;align-items:center;gap:4px;margin-left:auto;">
        <div id="kine-nav-mes" style="display:flex;align-items:center;gap:4px;">
          <button onclick="kineNavMes(-1)" style="background:#f1f5f9;border:none;border-radius:6px;padding:5px 10px;cursor:pointer;font-size:14px;">◀</button>
          <span id="kine-mes-label" style="font-size:13px;font-weight:600;color:#334155;min-width:100px;text-align:center;"></span>
          <button onclick="kineNavMes(1)" style="background:#f1f5f9;border:none;border-radius:6px;padding:5px 10px;cursor:pointer;font-size:14px;">▶</button>
        </div>
        <div style="display:flex;gap:2px;margin-left:8px;">
          <button id="kine-btn-mes" onclick="kineSetModo('mes')" style="font-size:11px;padding:4px 8px;border-radius:6px 0 0 6px;border:1px solid #e2e8f0;cursor:pointer;background:#7c3aed;color:#fff;">Mes</button>
          <button id="kine-btn-anio" onclick="kineSetModo('anio')" style="font-size:11px;padding:4px 8px;border:1px solid #e2e8f0;border-left:none;cursor:pointer;background:#f1f5f9;color:#475569;">Año</button>
          <button id="kine-btn-todos" onclick="kineSetModo('todos')" style="font-size:11px;padding:4px 8px;border-radius:0 6px 6px 0;border:1px solid #e2e8f0;border-left:none;cursor:pointer;background:#f1f5f9;color:#475569;">Todos</button>
        </div>
      </div>
      <button onclick="cerrarModalKine()" style="background:none;border:none;font-size:20px;cursor:pointer;color:#94a3b8;margin-left:8px;">✕</button>
    </div>
    <!-- Resumen -->
    <div id="kine-resumen" style="padding:12px 24px;background:#f8fafc;border-bottom:1px solid #e2e8f0;display:flex;gap:24px;flex-wrap:wrap;font-size:12px;color:#475569;"></div>
    <!-- Tabla -->
    <div style="overflow-x:auto;max-height:60vh;overflow-y:auto;">
      <table style="width:100%;border-collapse:collapse;font-size:13px;">
        <thead style="position:sticky;top:0;background:#f8fafc;z-index:1;">
          <tr>
            <th style="text-align:left;padding:10px 12px;font-weight:600;color:#475569;border-bottom:2px solid #e2e8f0;">Paciente</th>
            <th style="text-align:left;padding:10px 12px;font-weight:600;color:#475569;border-bottom:2px solid #e2e8f0;">Kinesiólogo</th>
            <th style="text-align:center;padding:10px 12px;font-weight:600;color:#475569;border-bottom:2px solid #e2e8f0;">Sesiones mes</th>
            <th style="text-align:center;padding:10px 12px;font-weight:600;color:#475569;border-bottom:2px solid #e2e8f0;">Total prescritas</th>
            <th style="text-align:center;padding:10px 12px;font-weight:600;color:#475569;border-bottom:2px solid #e2e8f0;">Progreso</th>
            <th style="text-align:center;padding:10px 12px;font-weight:600;color:#475569;border-bottom:2px solid #e2e8f0;">Modalidad</th>
            <th style="text-align:left;padding:10px 12px;font-weight:600;color:#475569;border-bottom:2px solid #e2e8f0;">Notas</th>
            <th style="padding:10px 8px;border-bottom:2px solid #e2e8f0;"></th>
          </tr>
        </thead>
        <tbody id="kine-tbody"></tbody>
      </table>
      <div id="kine-empty" style="display:none;text-align:center;padding:40px;color:#94a3b8;font-size:14px;">Sin citas de kinesiología en este período</div>
      <div id="kine-loading" style="text-align:center;padding:40px;color:#94a3b8;font-size:14px;">Cargando...</div>
    </div>
    <div style="padding:12px 24px;border-top:1px solid #e2e8f0;font-size:11px;color:#94a3b8;">
      💡 "Sesiones mes" se obtiene de Medilink. "Total prescritas" y "Notas" se guardan manualmente.
    </div>
  </div>
</div>

<!-- MODAL ORTODONCIA -->
<div id="modal-ort" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:1000;align-items:flex-start;justify-content:center;padding-top:30px;padding-bottom:30px;overflow-y:auto;">
  <div style="background:#fff;border-radius:14px;width:1100px;max-width:98vw;box-shadow:0 20px 60px rgba(0,0,0,.2);position:relative;margin:auto;">
    <!-- Header -->
    <div style="padding:16px 20px 12px;border-bottom:1px solid #e2e8f0;display:flex;align-items:center;gap:10px;position:sticky;top:0;background:#fff;z-index:2;border-radius:14px 14px 0 0;flex-wrap:wrap;gap:8px;">
      <span style="font-size:20px;">🦷</span>
      <div style="flex:1;min-width:160px;">
        <div style="font-size:15px;font-weight:700;color:#1e293b;">Ortodoncia — Dra. Daniela Castillo</div>
        <div style="font-size:11px;color:#94a3b8;" id="ort-ultima-sync"></div>
      </div>
      <!-- Filtro período -->
      <div style="display:flex;align-items:center;gap:3px;">
        <div id="ort-nav-mes" style="display:flex;align-items:center;gap:3px;">
          <button onclick="ortNavPeriodo(-1)" style="background:#f1f5f9;border:none;border-radius:6px;padding:4px 9px;cursor:pointer;font-size:13px;">◀</button>
          <span id="ort-periodo-label" style="font-size:12px;font-weight:600;color:#334155;min-width:80px;text-align:center;"></span>
          <button onclick="ortNavPeriodo(1)" style="background:#f1f5f9;border:none;border-radius:6px;padding:4px 9px;cursor:pointer;font-size:13px;">▶</button>
        </div>
        <div style="display:flex;gap:1px;margin-left:6px;">
          <button id="ort-btn-mes"   onclick="ortSetModo('mes')"   style="font-size:11px;padding:4px 8px;border-radius:6px 0 0 6px;border:1px solid #e2e8f0;cursor:pointer;background:#7e22ce;color:#fff;">Mes</button>
          <button id="ort-btn-anio"  onclick="ortSetModo('anio')"  style="font-size:11px;padding:4px 8px;border:1px solid #e2e8f0;border-left:none;cursor:pointer;background:#f1f5f9;color:#475569;">Año</button>
          <button id="ort-btn-todos" onclick="ortSetModo('todos')" style="font-size:11px;padding:4px 8px;border-radius:0 6px 6px 0;border:1px solid #e2e8f0;border-left:none;cursor:pointer;background:#f1f5f9;color:#475569;">Todos</button>
        </div>
      </div>
      <!-- Toggle vista -->
      <div style="display:flex;gap:1px;margin-left:4px;">
        <button id="ort-vista-cards"  onclick="ortSetVista('cards')"  title="Vista tarjetas" style="font-size:13px;padding:4px 9px;border-radius:6px 0 0 6px;border:1px solid #e2e8f0;cursor:pointer;background:#f1f5f9;color:#475569;">▦</button>
        <button id="ort-vista-matriz" onclick="ortSetVista('matriz')" title="Vista matriz"   style="font-size:13px;padding:4px 9px;border-radius:0 6px 6px 0;border:1px solid #e2e8f0;border-left:none;cursor:pointer;background:#7e22ce;color:#fff;">⊞</button>
      </div>
      <button onclick="cerrarModalOrtodoncia()" style="background:none;border:none;font-size:20px;cursor:pointer;color:#94a3b8;">✕</button>
    </div>
    <!-- Stats -->
    <div id="ort-resumen" style="padding:10px 20px;background:#fdf4ff;border-bottom:1px solid #e9d5ff;display:flex;gap:20px;flex-wrap:wrap;font-size:12px;color:#6b21a8;"></div>
    <!-- Leyenda -->
    <div style="padding:8px 20px;background:#f8fafc;border-bottom:1px solid #e2e8f0;display:flex;gap:16px;font-size:11px;color:#475569;align-items:center;">
      <span><span style="display:inline-block;width:12px;height:12px;border-radius:50%;background:#7e22ce;vertical-align:middle;margin-right:4px;"></span>Instalación $120.000</span>
      <span><span style="display:inline-block;width:12px;height:12px;border-radius:50%;background:#0369a1;vertical-align:middle;margin-right:4px;"></span>Control $30.000</span>
      <span><span style="display:inline-block;width:12px;height:12px;border-radius:50%;background:#94a3b8;vertical-align:middle;margin-right:4px;"></span>Sin clasificar</span>
      <span style="margin-left:auto;color:#94a3b8;">Clic en visita para cambiar tipo</span>
    </div>
    <div id="ort-loading" style="padding:40px;text-align:center;color:#94a3b8;">Cargando pacientes...</div>
    <!-- Vista cards -->
    <div id="ort-body-cards" style="padding:16px 20px;display:grid;grid-template-columns:repeat(auto-fill,minmax(270px,1fr));gap:14px;"></div>
    <!-- Vista matriz -->
    <div id="ort-body-matriz" style="display:none;overflow-x:auto;max-height:65vh;overflow-y:auto;">
      <table id="ort-tabla-matriz" style="border-collapse:collapse;font-size:12px;width:100%;"></table>
    </div>
    <div style="padding:10px 20px;border-top:1px solid #e2e8f0;font-size:11px;color:#94a3b8;">
      💡 Clasificación automática por monto desde Medilink · Los cambios manuales se guardan permanentemente.
    </div>
  </div>
</div>

<!-- MODAL BÚSQUEDA GLOBAL -->
<div id="modal-busqueda" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:1000;align-items:flex-start;justify-content:center;padding-top:80px;">
  <div style="background:#fff;border-radius:14px;width:560px;max-width:95vw;max-height:70vh;display:flex;flex-direction:column;box-shadow:0 20px 60px rgba(0,0,0,.2);position:relative;">
    <div style="padding:20px 20px 12px;border-bottom:1px solid var(--border);">
      <div style="display:flex;gap:8px;align-items:center;">
        <span style="font-size:18px;">🔍</span>
        <input id="global-search-input" type="text" placeholder="Buscar en todas las conversaciones..."
          style="flex:1;padding:9px 12px;border:1.5px solid var(--border);border-radius:8px;font-size:14px;"
          oninput="buscarGlobal()" onkeydown="if(event.key==='Escape') cerrarBusquedaGlobal()">
        <button onclick="cerrarBusquedaGlobal()" style="background:none;border:none;font-size:20px;cursor:pointer;color:#94a3b8;">×</button>
      </div>
      <div id="global-search-meta" style="font-size:11px;color:#94a3b8;margin-top:8px;min-height:14px;"></div>
    </div>
    <div id="global-search-results" style="overflow-y:auto;padding:8px 0;flex:1;"></div>
  </div>
</div>

<!-- MODAL ANULAR HORA -->
<div id="modal-anular" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:1000;align-items:center;justify-content:center;">
  <div style="background:#fff;border-radius:14px;width:440px;max-width:95vw;padding:28px 28px 24px;box-shadow:0 20px 60px rgba(0,0,0,.2);position:relative;">
    <button onclick="cerrarModalAnular()" style="position:absolute;top:14px;right:16px;background:none;border:none;font-size:20px;cursor:pointer;color:#94a3b8;">×</button>
    <h2 style="font-size:16px;font-weight:700;margin-bottom:20px;color:#dc2626;">✕ Anular Hora</h2>

    <!-- PASO 1: BUSCAR RUT -->
    <div id="an-paso1">
      <label style="font-size:13px;font-weight:600;color:#374151;">RUT del paciente</label>
      <div style="display:flex;gap:8px;margin-top:6px;">
        <input id="an-rut" type="text" placeholder="12345678-9" style="flex:1;padding:9px 12px;border:1.5px solid #e2e8f0;border-radius:8px;font-size:13px;"
          onkeydown="if(event.key==='Enter') buscarCitasPaciente()">
        <button onclick="buscarCitasPaciente()" style="background:#1172AB;color:#fff;border:none;border-radius:8px;padding:9px 16px;font-size:13px;font-weight:600;cursor:pointer;">Buscar</button>
      </div>
      <div id="an-paciente-info" style="margin-top:8px;min-height:18px;"></div>
    </div>

    <!-- PASO 2: LISTA DE CITAS -->
    <div id="an-paso2" style="display:none;">
      <div id="an-lista-citas" style="max-height:260px;overflow-y:auto;"></div>
      <div id="an-resultado" style="margin-top:4px;"></div>
      <button onclick="document.getElementById('an-paso1').style.display='block';document.getElementById('an-paso2').style.display='none';"
        style="margin-top:14px;background:none;border:none;color:#64748b;font-size:12px;cursor:pointer;text-decoration:underline;">← Buscar otro RUT</button>
    </div>
  </div>
</div>

<!-- MODAL NUEVA CITA -->
<div id="modal-agendar" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:1000;align-items:center;justify-content:center;">
  <div style="background:#fff;border-radius:14px;width:480px;max-width:95vw;padding:28px 28px 24px;box-shadow:0 20px 60px rgba(0,0,0,.2);position:relative;">
    <button onclick="cerrarModalAgendar()" style="position:absolute;top:14px;right:16px;background:none;border:none;font-size:20px;cursor:pointer;color:#94a3b8;">×</button>
    <h2 style="font-size:16px;font-weight:700;margin-bottom:20px;">📅 Nueva Cita</h2>

    <!-- PASO 1: RUT -->
    <div id="paso1">
      <label style="font-size:12px;font-weight:600;color:#475569;">RUT del paciente</label>
      <div style="display:flex;gap:8px;margin-top:6px;">
        <input id="m-rut" placeholder="12345678-9" style="flex:1;padding:9px 12px;border:1px solid #e2e8f0;border-radius:8px;font-size:13px;outline:none;" onkeydown="if(event.key==='Enter')buscarPaciente()">
        <button class="btn btn-primary" onclick="buscarPaciente()" style="padding:9px 16px;border-radius:8px;font-size:12px;">Buscar</button>
      </div>
      <div id="m-paciente-info" style="margin-top:10px;font-size:12px;"></div>
    </div>

    <!-- PASO 2: ESPECIALIDAD Y SLOTS -->
    <div id="paso2" style="display:none;margin-top:16px;">
      <label style="font-size:12px;font-weight:600;color:#475569;">Especialidad</label>
      <select id="m-especialidad" style="width:100%;margin-top:6px;padding:9px 12px;border:1px solid #e2e8f0;border-radius:8px;font-size:13px;outline:none;" onchange="buscarSlots()">
        <option value="">— Selecciona —</option>
      </select>
      <div id="m-slots" style="margin-top:12px;"></div>
    </div>

    <!-- PASO 3: CONFIRMAR -->
    <div id="paso3" style="display:none;margin-top:16px;">
      <div id="m-resumen" style="background:#f0fdf4;border:1px solid #bbf7d0;border-radius:8px;padding:12px;font-size:13px;"></div>
      <div style="display:flex;gap:8px;margin-top:14px;">
        <button class="btn btn-outline" onclick="volverPaso2()" style="flex:1;padding:9px;border-radius:8px;font-size:12px;">← Atrás</button>
        <button class="btn btn-primary" onclick="confirmarCita()" style="flex:2;padding:9px;border-radius:8px;font-size:12px;font-weight:600;">✅ Confirmar Cita</button>
      </div>
      <div id="m-resultado" style="margin-top:10px;font-size:12px;text-align:center;"></div>
    </div>
  </div>
</div>

<!-- BANNER DE ALERTAS -->
<div id="alert-bar" style="display:none; background:#fef2f2; border-bottom:1px solid #fca5a5; padding:7px 20px; display:flex; align-items:center; gap:16px; font-size:12px; font-weight:500; color:#991b1b;">
  <span style="font-size:14px;">🚨</span>
  <span id="alert-text"></span>
  <span style="margin-left:auto; font-size:11px; color:#b91c1c; opacity:.7;">Se actualiza automáticamente</span>
</div>

<!-- LAYOUT -->
<div class="layout">

  <!-- COL 1: FILTROS -->
  <div class="col-filters">
    <div class="filter-search">
      <div class="search-box">
        <span style="font-size:13px;">🔍</span>
        <input type="text" id="search" placeholder="Buscar nombre o teléfono..." oninput="renderList()">
      </div>
    </div>
    <div class="filter-section">
      <div class="filter-label">Estado</div>
      <button class="state-btn active" id="btn-all" onclick="setFilter('all',this)">
        <span class="sdot" style="background:#94a3b8;"></span>
        <span style="flex:1;">Todos</span>
        <span class="scount" id="cnt-all">—</span>
      </button>
      <div id="state-buttons"></div>
    </div>
    <div class="filter-section">
      <div class="filter-label">Especialidad</div>
      <div id="esp-buttons"></div>
      <div id="esp-empty" style="font-size:11px;color:var(--text-3);display:none;">Sin especialidad registrada</div>
    </div>
  </div>

  <!-- COL 2: CONVERSACIONES -->
  <div class="col-convs">
    <div class="col-convs-hdr">Conversaciones</div>
    <div class="conv-scroll" id="conv-list"></div>
  </div>

  <!-- COL 3: CHAT + CONTEXTO -->
  <div class="col-main">

    <!-- CHAT -->
    <div class="chat-panel">
      <div id="chat-empty" class="chat-empty">
        <div class="chat-empty-icon">💬</div>
        <h3>Selecciona una conversación</h3>
        <p>Haz clic en un contacto para ver el historial</p>
      </div>
      <div id="chat-active" class="chat-active" style="display:none;">
        <div class="chat-header">
          <div class="ch-avatar" id="chat-avatar-hdr">?</div>
          <div class="ch-info">
            <div class="ch-name" id="chat-name">—</div>
            <div class="ch-sub" id="chat-sub">—</div>
          </div>
          <div class="ch-actions">
            <button onclick="toggleBusquedaChat()" title="Buscar en conversación" style="background:none;border:none;cursor:pointer;font-size:16px;padding:4px 6px;color:#64748b;border-radius:6px;" id="btn-chat-search">🔍</button>
            <span class="state-tag tag-slate" id="chat-state-tag">—</span>
            <button class="btn btn-danger" id="btn-takeover" onclick="doTakeover()">🎯 Tomar control</button>
            <button class="btn btn-outline" id="btn-resume" style="display:none;" onclick="doResume()">🤖 Devolver al bot</button>
          </div>
        </div>
        <div id="takeover-banner" class="takeover-banner hidden">
          🙋 Estás respondiendo como recepcionista — el bot está pausado para este paciente
        </div>
        <div class="quick-replies hidden" id="quick-replies"></div>
        <div id="chat-search-bar" style="display:none;padding:6px 12px;border-bottom:1px solid var(--border);background:#f8fafc;gap:6px;align-items:center;flex-shrink:0;">
          <input id="chat-search-input" type="text" placeholder="Buscar en esta conversación..."
            style="flex:1;padding:5px 10px;border:1.5px solid var(--border);border-radius:7px;font-size:12px;width:100%;"
            oninput="filtrarMensajesChat()" onkeydown="if(event.key==='Escape') cerrarBusquedaChat()">
          <span id="chat-search-count" style="font-size:11px;color:#64748b;white-space:nowrap;margin-left:6px;"></span>
          <button onclick="cerrarBusquedaChat()" style="background:none;border:none;font-size:16px;cursor:pointer;color:#94a3b8;margin-left:4px;">×</button>
        </div>
        <div class="chat-messages" id="chat-messages"></div>
        <div class="reply-bar hidden" id="reply-bar">
          <textarea class="reply-textarea" id="reply-input" placeholder="Escribe tu respuesta..." rows="2"
            onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();sendReply();}"></textarea>
          <div class="reply-actions">
            <button class="btn btn-outline" onclick="document.getElementById('reply-input').value=''">Limpiar</button>
            <button class="btn btn-primary btn-send" onclick="sendReply()">Enviar →</button>
          </div>
        </div>
      </div>
    </div>

    <!-- CONTEXTO -->
    <div class="ctx-panel">
      <div id="ctx-empty" class="ctx-empty">
        <div style="font-size:32px;opacity:.3;">👤</div>
        <p style="font-size:12px;">Selecciona una<br>conversación</p>
      </div>
      <div id="ctx-content" class="ctx-content">
        <div class="ctx-sec">
          <div class="ctx-avatar" id="ctx-avatar">?</div>
          <div class="ctx-name" id="ctx-nombre">—</div>
          <div class="ctx-sub" id="ctx-sub-info">—</div>
          <div class="ctx-state-wrap"><span class="state-tag tag-slate" id="ctx-state-badge">—</span></div>
        </div>
        <div class="ctx-sec" id="ctx-flow-section" style="display:none;">
          <div class="ctx-sec-title">Flujo actual</div>
          <div id="ctx-esp-wrap" class="flow-row" style="display:none;">
            <span class="flow-row-icon">🩺</span>
            <div><div class="flow-row-label">Especialidad</div><div class="flow-row-value" id="ctx-especialidad">—</div></div>
          </div>
          <div id="ctx-prof-wrap" class="flow-row" style="display:none;">
            <span class="flow-row-icon">👨‍⚕️</span>
            <div><div class="flow-row-label">Profesional</div><div class="flow-row-value" id="ctx-profesional">—</div></div>
          </div>
          <div id="ctx-slot-wrap" class="flow-row" style="display:none;">
            <span class="flow-row-icon">📅</span>
            <div><div class="flow-row-label">Horario elegido</div><div class="flow-row-value" id="ctx-horario">—</div></div>
          </div>
        </div>
        <div class="ctx-sec" id="ctx-progress-section" style="display:none;">
          <div class="ctx-sec-title">Progreso del flujo</div>
          <div id="ctx-progress-steps"></div>
        </div>
        <div class="ctx-sec">
          <div class="ctx-sec-title">Acciones</div>
          <div class="ctx-actions">
            <button class="btn btn-danger btn-full" id="ctx-btn-takeover" onclick="doTakeover()">🎯 Tomar control</button>
            <button class="btn btn-outline btn-full" id="ctx-btn-resume" style="display:none;" onclick="doResume()">🤖 Devolver al bot</button>
          </div>
        </div>
        <div class="ctx-sec">
          <div class="ctx-sec-title">Etiquetas</div>
          <div id="ctx-tags" style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:8px;min-height:24px;"></div>
          <div style="display:flex;gap:6px;">
            <input id="ctx-tag-input" type="text" placeholder="Nueva etiqueta..." maxlength="30"
              style="flex:1;padding:6px 10px;border:1.5px solid var(--border);border-radius:7px;font-size:12px;"
              onkeydown="if(event.key==='Enter'){event.preventDefault();addTag();}">
            <button onclick="addTag()" style="background:var(--primary);color:#fff;border:none;border-radius:7px;padding:6px 12px;font-size:13px;cursor:pointer;font-weight:600;">+</button>
          </div>
        </div>
        <div class="ctx-sec">
          <div class="ctx-sec-title">Notas internas</div>
          <textarea class="ctx-notes" id="ctx-notes" placeholder="Notas sobre este paciente..."></textarea>
        </div>
      </div>
    </div>

  </div>
</div>

<script>
const TOKEN = "__TOKEN__";
let convs = [];
let selectedPhone = null;
let currentFilter = "all";
let currentEsp = "all";
let localNotes = {};

const ESP_DISPLAY = {
  "medicina general":"Medicina General","medicina familiar":"Medicina Familiar",
  "kinesiología":"Kinesiología","masoterapia":"Masoterapia","masaje":"Masoterapia",
  "etcheverry":"Kinesiología · Etcheverry","armijo":"Kinesiología · Luis Armijo",
  "paola":"Masoterapia · Paola Acosta","paola acosta":"Masoterapia · Paola Acosta",
  "abarca":"Medicina General · Dr. Abarca","olavarría":"Medicina General · Dr. Olavarría",
  "otorrinolaringología":"ORL · Dr. Borrego","cardiología":"Cardiología",
  "traumatología":"Traumatología · Dr. Barraza","ginecología":"Ginecología · Dr. Rejón",
  "gastroenterología":"Gastroenterología · Dr. Quijano","odontología general":"Odontología General",
  "ortodoncia":"Ortodoncia","endodoncia":"Endodoncia","implantología":"Implantología",
  "estética facial":"Estética Facial","nutrición":"Nutrición","psicología":"Psicología",
  "psicología adulto":"Psicología Adulto","fonoaudiología":"Fonoaudiología",
  "matrona":"Matrona","podología":"Podología","ecografía":"Ecografía",
};
function espLabel(key) {
  if (!key) return '';
  return ESP_DISPLAY[key.toLowerCase()] || (key.charAt(0).toUpperCase() + key.slice(1));
}

const ACTIVE_STATES = ["WAIT_ESPECIALIDAD","WAIT_SLOT","WAIT_MODALIDAD","WAIT_RUT_AGENDAR",
  "WAIT_NOMBRE_NUEVO","CONFIRMING_CITA","WAIT_RUT_CANCELAR","WAIT_CITA_CANCELAR",
  "CONFIRMING_CANCEL","WAIT_RUT_VER"];

const STATE_GROUPS = [
  { id:"takeover",  label:"Esperando atención",    dot:"#ef4444", tag:"tag-red",
    states:["HUMAN_TAKEOVER"] },
  { id:"agendando", label:"Agendando",              dot:"#f59e0b", tag:"tag-amber",
    states:["WAIT_ESPECIALIDAD","WAIT_SLOT","WAIT_MODALIDAD","WAIT_RUT_AGENDAR","WAIT_NOMBRE_NUEVO","CONFIRMING_CITA"] },
  { id:"cancelando",label:"Cancelando",             dot:"#8b5cf6", tag:"tag-purple",
    states:["WAIT_RUT_CANCELAR","WAIT_CITA_CANCELAR","CONFIRMING_CANCEL"] },
  { id:"reservas",  label:"Consultando reservas",   dot:"#3b82f6", tag:"tag-blue",
    states:["WAIT_RUT_VER"] },
  { id:"idle",      label:"Sin actividad",          dot:"#cbd5e1", tag:"tag-slate",
    states:["IDLE"] },
];

const STATE_LABELS = {
  IDLE:"Sin actividad", WAIT_ESPECIALIDAD:"Eligiendo especialidad",
  WAIT_SLOT:"Eligiendo horario", WAIT_MODALIDAD:"Fonasa / Particular",
  WAIT_RUT_AGENDAR:"Ingresando RUT", WAIT_NOMBRE_NUEVO:"Registro paciente nuevo",
  CONFIRMING_CITA:"Confirmando cita", WAIT_RUT_CANCELAR:"Cancelando — ingresa RUT",
  WAIT_CITA_CANCELAR:"Elige cita a cancelar", CONFIRMING_CANCEL:"Confirmando cancelación",
  WAIT_RUT_VER:"Consultando reservas", HUMAN_TAKEOVER:"Esperando recepcionista"
};

function stateLabel(s) { return STATE_LABELS[s] || s; }
function getGroup(state) { return STATE_GROUPS.find(g => g.states.includes(state)) || STATE_GROUPS[STATE_GROUPS.length-1]; }
function dotColor(state) {
  if (state === "HUMAN_TAKEOVER") return "#ef4444";
  if (ACTIVE_STATES.includes(state)) return "#f59e0b";
  return "#cbd5e1";
}
function stateTagClass(s) { return getGroup(s).tag; }
function initials(name) {
  if (!name) return "?";
  const p = name.trim().split(/\s+/);
  return p.length >= 2 ? (p[0][0]+p[1][0]).toUpperCase() : name.substring(0,2).toUpperCase();
}
function relTime(ts) {
  if (!ts) return "";
  const d = new Date(ts.replace(" ","T")+"Z");
  const now = new Date();
  const diff = Math.floor((Date.now() - d) / 1000);
  if (diff < 60) return "ahora";
  if (diff < 3600) return Math.floor(diff/60)+"m";
  // Si es hoy: mostrar hora
  if (d.toDateString()===now.toDateString()) return d.toLocaleTimeString("es-CL",{hour:"2-digit",minute:"2-digit",timeZone:"America/Santiago"});
  // Si es este año: mostrar día y mes
  if (d.getFullYear()===now.getFullYear()) return d.toLocaleDateString("es-CL",{day:"numeric",month:"short",timeZone:"America/Santiago"});
  // Otro año: fecha completa
  return d.toLocaleDateString("es-CL",{day:"numeric",month:"short",year:"numeric",timeZone:"America/Santiago"});
}
function waitMinutes(ts) {
  if (!ts) return 0;
  return Math.floor((Date.now() - new Date(ts.replace(" ","T")+"Z")) / 60000);
}
function waitLabel(mins) {
  if (mins < 60) return `${mins} min`;
  if (mins < 1440) { // menos de 24h
    const h = Math.floor(mins / 60), m = mins % 60;
    const s = `${h} hora${h > 1 ? "s" : ""}`;
    return m === 0 ? s : `${s} y ${m} min`;
  }
  if (mins < 10080) { // menos de 7 días
    const d = Math.floor(mins / 1440), h = Math.floor((mins % 1440) / 60);
    const s = `${d} día${d > 1 ? "s" : ""}`;
    return h === 0 ? s : `${s} y ${h} hora${h > 1 ? "s" : ""}`;
  }
  // 7 días o más
  const w = Math.floor(mins / 10080), d = Math.floor((mins % 10080) / 1440);
  const s = `${w} semana${w > 1 ? "s" : ""}`;
  return d === 0 ? s : `${s} y ${d} día${d > 1 ? "s" : ""}`;
}

/* ── FILTROS ── */
function setFilter(id, el) {
  currentFilter = id; currentEsp = "all";
  document.querySelectorAll(".state-btn").forEach(b => b.classList.remove("active"));
  el.classList.add("active");
  document.querySelectorAll(".esp-btn").forEach(b => b.classList.remove("active"));
  renderList();
}
function setEspFilter(esp) {
  currentEsp = (currentEsp === esp) ? "all" : esp;
  document.querySelectorAll(".esp-btn").forEach(b => b.classList.toggle("active", currentEsp!=="all" && b.dataset.esp === currentEsp));
  renderList();
}
function renderStateButtons() {
  const el = document.getElementById("state-buttons");
  document.getElementById("cnt-all").textContent = convs.length;
  let html = "";
  STATE_GROUPS.forEach(g => {
    const cnt = convs.filter(c => g.states.includes(c.state)).length;
    if (!cnt) return;
    html += `<button class="state-btn${currentFilter===g.id?" active":""}" onclick="setFilter('${g.id}',this)">
      <span class="sdot" style="background:${g.dot};"></span>
      <span style="flex:1;">${g.label}</span>
      <span class="scount">${cnt}</span>
    </button>`;
  });
  el.innerHTML = html;
}
function renderEspButtons() {
  const el = document.getElementById("esp-buttons");
  const emptyEl = document.getElementById("esp-empty");
  const esps = {};
  convs.forEach(c => { const e = c.flow_data?.especialidad; if (e) esps[e]=(esps[e]||0)+1; });
  const entries = Object.entries(esps).sort((a,b)=>b[1]-a[1]);
  if (!entries.length) { el.innerHTML=""; emptyEl.style.display="block"; return; }
  emptyEl.style.display="none";
  el.innerHTML = entries.map(([esp,cnt]) =>
    `<button class="esp-btn${currentEsp===esp?" active":""}" data-esp="${esp.replace(/"/g,"&quot;")}" onclick="setEspFilter(this.dataset.esp)">
      🩺 ${espLabel(esp)} <span class="esp-btn-count">${cnt}</span>
    </button>`
  ).join("");
}

/* ── LISTA ── */
function sortedConvs(list) {
  return list.slice().sort((a,b) => {
    const gA = STATE_GROUPS.findIndex(g=>g.states.includes(a.state));
    const gB = STATE_GROUPS.findIndex(g=>g.states.includes(b.state));
    if (gA!==gB) return gA-gB;
    // Dentro del mismo grupo: más reciente primero
    return waitMinutes(a.last_ts||a.updated_at) - waitMinutes(b.last_ts||b.updated_at);
  });
}
function filtered() {
  const q = document.getElementById("search").value.toLowerCase();
  const group = STATE_GROUPS.find(g=>g.id===currentFilter);
  return convs.filter(c => {
    if (q && !(c.phone||"").includes(q) && !(c.nombre||"").toLowerCase().includes(q)) return false;
    if (group && !group.states.includes(c.state)) return false;
    if (currentEsp!=="all" && (c.flow_data?.especialidad||"")!==currentEsp) return false;
    return true;
  });
}
function renderList() {
  const list = sortedConvs(filtered());
  const el = document.getElementById("conv-list");
  if (!list.length) { el.innerHTML=`<div class="no-results">Sin resultados</div>`; return; }
  let html=""; let lastGid=null;
  list.forEach(c => {
    const g = getGroup(c.state);
    if (lastGid!==g.id) { html+=`<div class="group-label" style="color:${g.dot};">${g.label.toUpperCase()}</div>`; lastGid=g.id; }
    html+=convCard(c,g);
  });
  el.innerHTML=html;
}
function canalIcon(canal) {
  if (canal==="instagram") return `<span title="Instagram" style="font-size:14px;">📷</span>`;
  if (canal==="messenger") return `<span title="Facebook Messenger" style="font-size:14px;">💬</span>`;
  return `<span title="WhatsApp" style="font-size:14px;">📱</span>`;
}
function canalLabel(canal) {
  if (canal==="instagram") return "Instagram";
  if (canal==="messenger") return "Messenger";
  return "WhatsApp";
}
function convCard(c,g) {
  const name = c.nombre||(c.phone.startsWith("ig_")?"Instagram #"+c.phone.slice(3):c.phone.startsWith("fb_")?"Messenger #"+c.phone.slice(3):c.phone);
  const preview = c.last_text ? c.last_text.substring(0,60) : "Sin mensajes";
  const dir = c.last_dir==="in" ? "" : "← ";
  const fd = c.flow_data||{};
  const mins = waitMinutes(c.last_ts||c.updated_at);
  const canal = c.canal||"whatsapp";
  let badges="";
  if (c.msgs_sin_respuesta>0) badges+=`<span class="badge badge-unread">${c.msgs_sin_respuesta}</span>`;
  if (mins>=15) badges+=`<span class="badge badge-urgent">⏰ ${waitLabel(mins)} sin respuesta</span>`;
  else if (mins>=5) badges+=`<span class="badge badge-warn">⏱ ${waitLabel(mins)} esperando</span>`;
  else if (mins>=1&&c.state!=="IDLE") badges+=`<span class="badge" style="background:#f8fafc;color:#94a3b8;border-color:#e2e8f0;">⏱ ${waitLabel(mins)}</span>`;
  if (fd.fecha_display&&fd.hora_inicio) badges+=`<span class="badge badge-prob">✅ Lista para agendar</span>`;
  return `<div class="conv-card${selectedPhone===c.phone?" selected":""}" style="border-left-color:${g.dot};" onclick="selectConv('${c.phone}')">
    <div class="card-top">
      <span class="cdot" style="background:${dotColor(c.state)};"></span>
      <span class="cname">${name.replace(/</g,"&lt;")}</span>
      <span style="margin-left:4px;">${canalIcon(canal)}</span>
      <span class="ctime">${relTime(c.last_ts||c.updated_at)}</span>
    </div>
    <div class="cstate" style="color:${g.dot};">${stateLabel(c.state)}</div>
    ${fd.especialidad?`<div class="cesp">🩺 ${espLabel(fd.especialidad)}</div>`:""}
    <div class="cpreview">${dir}${preview.replace(/</g,"&lt;")}</div>
    ${badges?`<div class="cbadges">${badges}</div>`:""}
  </div>`;
}

/* ── SELECCIÓN ── */
async function selectConv(phone) {
  selectedPhone=phone; renderList();
  const conv=convs.find(c=>c.phone===phone);
  document.getElementById("chat-empty").style.display="none";
  document.getElementById("chat-active").style.display="flex";
  const name=conv?.nombre||phone;
  document.getElementById("chat-avatar-hdr").textContent=initials(name);
  document.getElementById("chat-name").textContent=name;
  const canal = conv?.canal||"whatsapp";
  const canalTxt = canalLabel(canal);
  document.getElementById("chat-sub").textContent=(conv?.rut?`RUT ${conv.rut} · `:"")+`${phone} · ${canalTxt}`;
  updateChatControls(conv?.state||"IDLE");
  updateContextPanel(conv);
  await Promise.all([loadMessages(phone), loadTags(phone)]);
}
function updateChatControls(state) {
  const isTakeover=state==="HUMAN_TAKEOVER";
  const tag=document.getElementById("chat-state-tag");
  tag.textContent=stateLabel(state); tag.className="state-tag "+stateTagClass(state);
  document.getElementById("btn-takeover").style.display=isTakeover?"none":"inline-flex";
  document.getElementById("btn-resume").style.display=isTakeover?"inline-flex":"none";
  document.getElementById("ctx-btn-takeover").style.display=isTakeover?"none":"block";
  document.getElementById("ctx-btn-resume").style.display=isTakeover?"block":"none";
  document.getElementById("takeover-banner").classList.toggle("hidden",!isTakeover);
  document.getElementById("reply-bar").classList.toggle("hidden",!isTakeover);
  document.getElementById("quick-replies").classList.toggle("hidden",!isTakeover);
}
function updateContextPanel(conv) {
  if (!conv) return;
  document.getElementById("ctx-empty").style.display="none";
  document.getElementById("ctx-content").style.display="block";
  const name=conv.nombre||conv.phone;
  document.getElementById("ctx-avatar").textContent=initials(name);
  document.getElementById("ctx-nombre").textContent=name;
  document.getElementById("ctx-sub-info").textContent=(conv.rut?`RUT ${conv.rut} · `:"")+conv.phone;
  const sb=document.getElementById("ctx-state-badge");
  sb.textContent=stateLabel(conv.state); sb.className="state-tag "+stateTagClass(conv.state);
  const notesEl=document.getElementById("ctx-notes");
  notesEl.value=localNotes[conv.phone]||"";
  notesEl.oninput=()=>{localNotes[conv.phone]=notesEl.value;};
  const fd=conv.flow_data||{};
  const hasFlow=fd.especialidad||fd.profesional||fd.fecha_display;
  document.getElementById("ctx-flow-section").style.display=hasFlow?"block":"none";
  const ew=document.getElementById("ctx-esp-wrap"); const pw=document.getElementById("ctx-prof-wrap"); const sw=document.getElementById("ctx-slot-wrap");
  if(fd.especialidad){ew.style.display="flex";document.getElementById("ctx-especialidad").textContent=espLabel(fd.especialidad);}else ew.style.display="none";
  if(fd.profesional){pw.style.display="flex";document.getElementById("ctx-profesional").textContent=fd.profesional;}else pw.style.display="none";
  if(fd.fecha_display&&fd.hora_inicio){sw.style.display="flex";document.getElementById("ctx-horario").textContent=fd.fecha_display+" · "+fd.hora_inicio.substring(0,5);}else sw.style.display="none";
  // Checklist de progreso
  const isAgendar = ["WAIT_ESPECIALIDAD","WAIT_SLOT","WAIT_MODALIDAD","WAIT_RUT_AGENDAR","WAIT_NOMBRE_NUEVO","CONFIRMING_CITA"].includes(conv.state);
  const progSec = document.getElementById("ctx-progress-section");
  if(isAgendar){
    progSec.style.display="block";
    const steps=[
      {label:"Especialidad elegida", done:!!fd.especialidad},
      {label:"Horario seleccionado", done:!!(fd.fecha_display&&fd.hora_inicio)},
      {label:"Previsión / RUT", done:!!conv.rut},
      {label:"Confirmación pendiente", done:conv.state==="CONFIRMING_CITA"},
    ];
    document.getElementById("ctx-progress-steps").innerHTML=steps.map(s=>`
      <div class="progress-step ${s.done?"done":"pending"}">
        <span class="progress-step-icon">${s.done?"✓":"·"}</span>
        <span>${s.label}</span>
      </div>`).join("");
  } else { progSec.style.display="none"; }
  renderQuickReplies(conv);
}
function renderQuickReplies(conv) {
  const fd=conv.flow_data||{};
  const replies=["En un momento te atiendo 😊"];
  if(fd.especialidad) replies.push(`Te busco disponibilidad para ${fd.especialidad} 👍`);
  else replies.push("¿En qué especialidad necesitas hora?");
  if(!conv.rut) replies.push("¿Cuál es tu RUT para buscarte en el sistema?");
  if(fd.fecha_display&&fd.hora_inicio) replies.push(`Tu hora el ${fd.fecha_display} a las ${fd.hora_inicio.substring(0,5)} está confirmada ✅`);
  replies.push("Tu consulta fue registrada, te llamamos pronto.");
  replies.push("Para más información llama al (41) 296 5226");
  document.getElementById("quick-replies").innerHTML=replies.map(r=>
    `<button class="qr-btn" onclick="insertQR(${JSON.stringify(r)})">${r.replace(/</g,"&lt;")}</button>`
  ).join("");
}

/* ── MENSAJES ── */
async function loadMessages(phone, preserveScroll=false) {
  try {
    const r=await fetch(`/admin/api/conversations/${encodeURIComponent(phone)}?token=${TOKEN}`);
    renderMessages(await r.json(), preserveScroll);
  } catch(e){console.error(e);}
}
function renderMessages(msgs, preserveScroll=false) {
  const el=document.getElementById("chat-messages");
  const prevScroll=el.scrollTop;
  if (msgs.length) allMsgsCache = msgs;
  if(!msgs.length){el.innerHTML=`<div style="text-align:center;color:var(--text-3);font-size:12px;padding:20px;">Sin mensajes registrados aún</div>`;return;}
  let html=""; let lastState=null;
  [...msgs].reverse().forEach(m=>{
    if(m.state&&m.state!==lastState){html+=`<div class="state-sep"><span class="state-pill">${stateLabel(m.state)}</span></div>`;lastState=m.state;}
    const isRecep=m.direction==="out"&&(m.text||"").startsWith("[Recepcionista]");
    const text=(m.text||"").replace(/^\[Recepcionista\] /,"").replace(/^\[.*?\] /,"")
      .replace(/</g,"&lt;").replace(/\\n/g,"<br>").replace(/\*(.*?)\*/g,"<strong>$1</strong>");
    const ts=m.ts?new Date(m.ts.replace(" ","T")+"Z").toLocaleTimeString("es-CL",{hour:"2-digit",minute:"2-digit"}):"";
    const mCanal=m.canal||"whatsapp";
    const chanIco=m.direction==="in"?canalIcon(mCanal):"";
    const who=m.direction==="in"?`${chanIco} Paciente`:isRecep?"🙋 Recepcionista":"🤖 Bot";
    html+=`<div class="msg-row ${m.direction}${isRecep?" recep":""}"><div><div class="msg-bubble">${text}</div><div class="msg-meta">${who} · ${ts}</div></div></div>`;
  });
  el.innerHTML=html; el.scrollTop=preserveScroll ? prevScroll : 0;
}
function insertQR(text){document.getElementById("reply-input").value=text;document.getElementById("reply-input").focus();}

/* ── ACCIONES ── */
async function doTakeover(){
  if(!selectedPhone) return;
  if(!confirm("¿Tomar esta conversación? El bot se pausará.")) return;
  const r=await fetch(`/admin/api/takeover/${encodeURIComponent(selectedPhone)}?token=${TOKEN}`,{method:"POST"});
  if(r.ok){updateChatControls("HUMAN_TAKEOVER");await loadConversations();}
  else alert("Error al tomar la conversación");
}
async function doResume(){
  if(!selectedPhone) return;
  if(!confirm("¿Devolver el control al bot?")) return;
  const r=await fetch(`/admin/api/resume/${encodeURIComponent(selectedPhone)}?token=${TOKEN}`,{method:"POST"});
  if(r.ok){updateChatControls("IDLE");await loadConversations();await loadMessages(selectedPhone);}
  else alert("Error al reanudar el bot");
}
async function sendReply(){
  if(!selectedPhone) return;
  const input=document.getElementById("reply-input");
  const msg=input.value.trim(); if(!msg) return;
  const btn=document.querySelector("#reply-bar .btn-send");
  btn.disabled=true; btn.textContent="...";
  try{
    const r=await fetch(`/admin/api/reply?token=${TOKEN}`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({phone:selectedPhone,message:msg})});
    if(r.ok){input.value="";await loadMessages(selectedPhone);}
    else alert("Error al enviar el mensaje");
  }finally{btn.disabled=false;btn.textContent="Enviar →";}
}

/* ── DATOS ── */
async function loadConversations(){
  try{
    const r=await fetch(`/admin/api/conversations?token=${TOKEN}`);
    convs=await r.json();
    renderStateButtons(); renderEspButtons(); renderList(); updateStats();
    if(selectedPhone){
      const still=convs.find(c=>c.phone===selectedPhone);
      if(still){updateChatControls(still.state);updateContextPanel(still);await loadMessages(selectedPhone,true);}
    }
  }catch(e){console.error(e);}
  document.getElementById("last-refresh").textContent=
    new Date().toLocaleTimeString("es-CL",{hour:"2-digit",minute:"2-digit",second:"2-digit"});
}
async function loadMetrics(){
  try{const r=await fetch(`/admin/api/metrics?token=${TOKEN}`);await r.json();}catch(e){}
}
function updateStats(){
  document.getElementById("s-total").textContent=convs.length;
  document.getElementById("s-flujo").textContent=convs.filter(c=>ACTIVE_STATES.includes(c.state)).length;
  const tk=convs.filter(c=>c.state==="HUMAN_TAKEOVER").length;
  document.getElementById("s-takeover").textContent=tk;
  document.getElementById("pill-esperando").className="pill"+(tk>0?" red":"");
  // Banner de alertas
  const alertBar=document.getElementById("alert-bar");
  const esperando5=convs.filter(c=>c.state!=="IDLE"&&waitMinutes(c.last_ts||c.updated_at)>=5).length;
  const listos=convs.filter(c=>(c.flow_data?.fecha_display)&&(c.flow_data?.hora_inicio)).length;
  const partes=[];
  if(tk>0) partes.push(`${tk} paciente${tk>1?"s":""} esperando atención humana`);
  if(esperando5>0) partes.push(`${esperando5} sin respuesta hace más de 5 min`);
  if(listos>0) partes.push(`${listos} listo${listos>1?"s":""} para confirmar hora`);
  if(partes.length){
    alertBar.style.display="flex";
    document.getElementById("alert-text").textContent=partes.join("  ·  ");
    document.documentElement.style.setProperty("--alert-h","37px");
  } else {
    alertBar.style.display="none";
    document.documentElement.style.setProperty("--alert-h","0px");
  }
}

loadConversations();
loadMetrics();
setInterval(loadConversations,10000);
setInterval(loadMetrics,60000);

// ── BÚSQUEDA EN CHAT ─────────────────────────────────────────────────────────
let allMsgsCache = [];

function toggleBusquedaChat() {
  const bar = document.getElementById('chat-search-bar');
  const visible = bar.style.display === 'flex';
  if (visible) { cerrarBusquedaChat(); }
  else { bar.style.display='flex'; document.getElementById('chat-search-input').focus(); }
}
function cerrarBusquedaChat() {
  document.getElementById('chat-search-bar').style.display='none';
  document.getElementById('chat-search-input').value='';
  document.getElementById('chat-search-count').textContent='';
  // Restaurar mensajes sin highlight
  if (allMsgsCache.length) renderMessages(allMsgsCache, true);
}
function filtrarMensajesChat() {
  const q = document.getElementById('chat-search-input').value.trim().toLowerCase();
  const el = document.getElementById('chat-messages');
  const countEl = document.getElementById('chat-search-count');
  if (!q) { if (allMsgsCache.length) renderMessages(allMsgsCache, true); countEl.textContent=''; return; }
  // Highlight en el HTML existente
  let count = 0;
  el.querySelectorAll('.msg-bubble').forEach(b => {
    const orig = b.getAttribute('data-orig') || b.innerHTML;
    b.setAttribute('data-orig', orig);
    const plain = b.textContent.toLowerCase();
    if (plain.includes(q)) {
      count++;
      b.innerHTML = orig.replace(new RegExp(q.replace(/[.*+?^${}()|[\]\\]/g,'\\$&'), 'gi'),
        m => `<mark style="background:#fef08a;border-radius:2px;padding:0 2px;">${m}</mark>`);
      b.closest('.msg-row')?.scrollIntoView({block:'nearest'});
    } else {
      b.innerHTML = orig;
    }
  });
  countEl.textContent = count ? `${count} resultado${count>1?'s':''}` : 'Sin resultados';
}

// ── BÚSQUEDA GLOBAL ───────────────────────────────────────────────────────────
let globalSearchTimer = null;

function abrirBusquedaGlobal() {
  document.getElementById('modal-busqueda').style.display='flex';
  setTimeout(()=>document.getElementById('global-search-input').focus(), 50);
}
function cerrarBusquedaGlobal() {
  document.getElementById('modal-busqueda').style.display='none';
  document.getElementById('global-search-input').value='';
  document.getElementById('global-search-results').innerHTML='';
  document.getElementById('global-search-meta').textContent='';
}
function buscarGlobal() {
  clearTimeout(globalSearchTimer);
  const q = document.getElementById('global-search-input').value.trim();
  if (q.length < 2) { document.getElementById('global-search-results').innerHTML=''; document.getElementById('global-search-meta').textContent=''; return; }
  document.getElementById('global-search-meta').textContent='Buscando...';
  globalSearchTimer = setTimeout(async ()=>{
    try {
      const r = await fetch(`/admin/api/search?q=${encodeURIComponent(q)}&token=${TOKEN}`);
      const d = await r.json();
      const results = d.results || [];
      document.getElementById('global-search-meta').textContent = results.length ? `${results.length} resultado${results.length>1?'s':''}` : 'Sin resultados';
      if (!results.length) { document.getElementById('global-search-results').innerHTML='<p style="text-align:center;color:#94a3b8;font-size:13px;padding:24px;">Sin resultados para "'+q.replace(/</g,'&lt;')+'"</p>'; return; }
      const re = new RegExp(q.replace(/[.*+?^${}()|[\]\\]/g,'\\$&'), 'gi');
      document.getElementById('global-search-results').innerHTML = results.map(m => {
        const snippet = (m.text||'').replace(/</g,'&lt;').replace(re, match=>`<mark style="background:#fef08a;border-radius:2px;padding:0 1px;">${match.replace(/</g,'&lt;')}</mark>`);
        const who = m.direction==='in'?'👤':'🤖';
        const nombre = m.nombre ? `<strong>${m.nombre.replace(/</g,'&lt;')}</strong> · ` : '';
        const ts = m.ts ? new Date(m.ts.replace(' ','T')+'Z').toLocaleString('es-CL',{day:'numeric',month:'short',hour:'2-digit',minute:'2-digit',timeZone:'America/Santiago'}) : '';
        return `<div onclick="seleccionarYCerrar('${m.phone}')" style="padding:10px 20px;border-bottom:1px solid var(--border);cursor:pointer;transition:background .1s;" onmouseover="this.style.background='#f8fafc'" onmouseout="this.style.background=''">
          <div style="font-size:11px;color:#64748b;margin-bottom:3px;">${nombre}${m.phone} · ${ts} ${who}</div>
          <div style="font-size:13px;line-height:1.4;">${snippet}</div>
        </div>`;
      }).join('');
    } catch(e) { document.getElementById('global-search-meta').textContent='Error al buscar'; }
  }, 350);
}
async function seleccionarYCerrar(phone) {
  cerrarBusquedaGlobal();
  const conv = convs.find(c=>c.phone===phone);
  if (conv) { selectConv(conv.phone); }
  else {
    // Conversación no está en la lista visible — cargarla igual
    selectedPhone = phone;
    document.getElementById('chat-empty').style.display='none';
    document.getElementById('chat-active').style.display='flex';
    await loadMessages(phone);
  }
}

// ── ETIQUETAS ────────────────────────────────────────────────────────────────
const TAG_COLORS = ["#dbeafe","#dcfce7","#fef9c3","#fce7f3","#ede9fe","#ffedd5","#e0f2fe","#f1f5f9"];
const TAG_TEXT   = ["#1d4ed8","#15803d","#854d0e","#9d174d","#6d28d9","#9a3412","#0369a1","#475569"];

function renderTags(tags) {
  const el = document.getElementById("ctx-tags");
  if (!el) return;
  if (!tags.length) { el.innerHTML = '<span style="font-size:11px;color:#94a3b8;">Sin etiquetas</span>'; return; }
  el.innerHTML = tags.map((t,i) => {
    const bg  = TAG_COLORS[i % TAG_COLORS.length];
    const txt = TAG_TEXT[i % TAG_TEXT.length];
    const enc = encodeURIComponent(t);
    return `<span style="display:inline-flex;align-items:center;gap:4px;background:${bg};color:${txt};border-radius:20px;padding:3px 10px 3px 10px;font-size:11px;font-weight:600;">
      ${t.replace(/</g,"&lt;")}
      <span onclick="removeTag('${enc}')" style="cursor:pointer;opacity:.6;font-size:13px;line-height:1;margin-left:2px;" title="Eliminar">×</span>
    </span>`;
  }).join("");
}

async function loadTags(phone) {
  try {
    const r = await fetch(`/admin/api/tags/${encodeURIComponent(phone)}?token=${TOKEN}`);
    const d = await r.json();
    renderTags(d.tags);
  } catch(e) { console.error(e); }
}

async function addTag() {
  if (!selectedPhone) return;
  const input = document.getElementById("ctx-tag-input");
  const tag = input.value.trim();
  if (!tag) return;
  const r = await fetch(`/admin/api/tags/${encodeURIComponent(selectedPhone)}?token=${TOKEN}`,
    {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({tag})});
  if (r.ok) { const d=await r.json(); renderTags(d.tags); input.value=""; }
}

async function removeTag(encodedTag) {
  if (!selectedPhone) return;
  const r = await fetch(`/admin/api/tags/${encodeURIComponent(selectedPhone)}/${encodedTag}?token=${TOKEN}`,
    {method:"DELETE"});
  if (r.ok) { const d=await r.json(); renderTags(d.tags); }
}

// ── MODAL NUEVA CITA ─────────────────────────────────────────────────────────
let slotSeleccionado = null;
let pacienteActual = null;

async function abrirModalAgendar(){
  document.getElementById('modal-agendar').style.display='flex';
  document.getElementById('paso1').style.display='block';
  document.getElementById('paso2').style.display='none';
  document.getElementById('paso3').style.display='none';
  document.getElementById('m-rut').value='';
  document.getElementById('m-paciente-info').innerHTML='';
  document.getElementById('m-resultado').innerHTML='';
  slotSeleccionado=null; pacienteActual=null;
  // Cargar especialidades
  const r=await fetch(`/admin/api/especialidades?token=${TOKEN}`);
  const d=await r.json();
  const sel=document.getElementById('m-especialidad');
  sel.innerHTML='<option value="">— Selecciona —</option>';
  d.especialidades.forEach(e=>{ const o=document.createElement('option'); o.value=e; o.textContent=e; sel.appendChild(o); });
}

function cerrarModalAgendar(){
  document.getElementById('modal-agendar').style.display='none';
}

async function buscarPaciente(){
  const rut=document.getElementById('m-rut').value.trim();
  if(!rut) return;
  const info=document.getElementById('m-paciente-info');
  info.innerHTML='<span style="color:#94a3b8;">Buscando...</span>';
  const r=await fetch(`/admin/api/paciente?rut=${encodeURIComponent(rut)}&token=${TOKEN}`);
  if(r.ok){
    pacienteActual=await r.json();
    const nombre=pacienteActual.nombre+' '+(pacienteActual.apellido||pacienteActual.apellidos||'');
    info.innerHTML=`<span style="color:#15803d;font-weight:600;">✅ ${nombre.trim()}</span>`;
    document.getElementById('paso2').style.display='block';
  } else if(r.status===404){
    info.innerHTML=`<span style="color:#dc2626;">Paciente no encontrado. </span><span style="color:#2563eb;cursor:pointer;text-decoration:underline;" onclick="mostrarFormNuevoPaciente()">Registrar nuevo</span>`;
    pacienteActual={rut:rut, nuevo:true};
    document.getElementById('paso2').style.display='block';
  } else {
    info.innerHTML='<span style="color:#dc2626;">Error al buscar paciente.</span>';
  }
}

function mostrarFormNuevoPaciente(){
  const info=document.getElementById('m-paciente-info');
  info.innerHTML=`
    <div style="margin-top:8px;display:flex;flex-direction:column;gap:6px;">
      <input id="m-nombre" placeholder="Nombre" style="padding:7px 10px;border:1px solid #e2e8f0;border-radius:6px;font-size:12px;">
      <input id="m-apellidos" placeholder="Apellidos" style="padding:7px 10px;border:1px solid #e2e8f0;border-radius:6px;font-size:12px;">
      <button class="btn btn-primary" onclick="registrarNuevoPaciente()" style="padding:7px;border-radius:6px;font-size:12px;">Registrar</button>
    </div>`;
}

async function registrarNuevoPaciente(){
  const nombre=document.getElementById('m-nombre').value.trim();
  const apellidos=document.getElementById('m-apellidos').value.trim();
  if(!nombre||!apellidos) return;
  pacienteActual={...pacienteActual, nombre, apellidos};
  document.getElementById('m-paciente-info').innerHTML=`<span style="color:#15803d;font-weight:600;">✅ ${nombre} ${apellidos} (nuevo)</span>`;
}

async function buscarSlots(){
  const esp=document.getElementById('m-especialidad').value;
  if(!esp) return;
  const cont=document.getElementById('m-slots');
  cont.innerHTML='<span style="color:#94a3b8;font-size:12px;">Buscando disponibilidad...</span>';
  slotSeleccionado=null;
  const r=await fetch(`/admin/api/slots?especialidad=${encodeURIComponent(esp)}&token=${TOKEN}`);
  if(!r.ok){ cont.innerHTML='<span style="color:#dc2626;font-size:12px;">Sin disponibilidad.</span>'; return; }
  const d=await r.json();
  const fecha=d.fecha;
  cont.innerHTML=`<div style="font-size:11px;color:#64748b;margin-bottom:6px;">📅 ${fecha}</div>`;
  d.slots.forEach(s=>{
    const btn=document.createElement('button');
    btn.style.cssText='display:block;width:100%;text-align:left;padding:8px 12px;margin-bottom:4px;border:1px solid #e2e8f0;border-radius:8px;background:#f8fafc;cursor:pointer;font-size:12px;';
    btn.innerHTML=`<strong>${s.hora_inicio.slice(0,5)}</strong> — ${s.profesional_nombre}`;
    btn.onclick=()=>{ slotSeleccionado={...s, fecha}; mostrarConfirmacion(); };
    cont.appendChild(btn);
  });
}

function mostrarConfirmacion(){
  document.getElementById('paso3').style.display='block';
  const nombre = pacienteActual.nombre ? pacienteActual.nombre+' '+(pacienteActual.apellidos||'') : '(nuevo paciente)';
  document.getElementById('m-resumen').innerHTML=`
    <div style="font-weight:600;margin-bottom:6px;">Resumen de la cita</div>
    <div>👤 <strong>${nombre.trim()}</strong></div>
    <div>🩺 ${slotSeleccionado.profesional_nombre}</div>
    <div>📅 ${slotSeleccionado.fecha} a las ${slotSeleccionado.hora_inicio.slice(0,5)}</div>`;
}

function volverPaso2(){ document.getElementById('paso3').style.display='none'; }

async function confirmarCita(){
  const res=document.getElementById('m-resultado');
  res.innerHTML='<span style="color:#94a3b8;">Creando cita...</span>';
  const duracion=(()=>{
    const [h1,m1]=slotSeleccionado.hora_inicio.split(':').map(Number);
    const [h2,m2]=slotSeleccionado.hora_fin.split(':').map(Number);
    return (h2*60+m2)-(h1*60+m1);
  })();
  const body={
    rut: pacienteActual.rut||document.getElementById('m-rut').value,
    nombre: pacienteActual.nombre||'',
    apellidos: pacienteActual.apellidos||'',
    id_profesional: slotSeleccionado.id_profesional,
    fecha: slotSeleccionado.fecha,
    hora_inicio: slotSeleccionado.hora_inicio,
    hora_fin: slotSeleccionado.hora_fin,
    duracion,
  };
  const r=await fetch(`/admin/api/agendar?token=${TOKEN}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  if(r.ok){
    res.innerHTML='<span style="color:#15803d;font-weight:600;">✅ Cita creada exitosamente</span>';
    setTimeout(cerrarModalAgendar, 2000);
  } else {
    const e=await r.json();
    res.innerHTML=`<span style="color:#dc2626;">Error: ${e.detail||'No se pudo crear la cita'}</span>`;
  }
}

// ── MODAL ANULAR HORA ─────────────────────────────────────────────────────────
let citaAAnular = null;

async function abrirModalAnular(){
  document.getElementById('modal-anular').style.display='flex';
  document.getElementById('an-paso1').style.display='block';
  document.getElementById('an-paso2').style.display='none';
  document.getElementById('an-rut').value='';
  document.getElementById('an-paciente-info').innerHTML='';
  document.getElementById('an-resultado').innerHTML='';
  citaAAnular=null;
}

function cerrarModalAnular(){
  document.getElementById('modal-anular').style.display='none';
}

async function buscarCitasPaciente(){
  const rut=document.getElementById('an-rut').value.trim();
  if(!rut) return;
  document.getElementById('an-paciente-info').innerHTML='<span style="color:#64748b;font-size:13px;">Buscando...</span>';
  const r=await fetch(`/admin/api/citas-paciente?rut=${encodeURIComponent(rut)}&token=${TOKEN}`);
  if(!r.ok){
    document.getElementById('an-paciente-info').innerHTML='<span style="color:#dc2626;font-size:13px;">Paciente no encontrado</span>';
    return;
  }
  const d=await r.json();
  document.getElementById('an-paciente-info').innerHTML=`<span style="font-size:13px;color:#15803d;">✓ ${d.paciente.nombre}</span>`;
  const lista=document.getElementById('an-lista-citas');
  if(!d.citas.length){
    lista.innerHTML='<p style="color:#64748b;font-size:13px;text-align:center;padding:16px 0;">Sin citas próximas registradas</p>';
    document.getElementById('an-paso1').style.display='none';
    document.getElementById('an-paso2').style.display='block';
    return;
  }
  lista.innerHTML=d.citas.map(c=>`
    <div onclick="seleccionarCita(${c.id},'${c.profesional}','${c.fecha_display}','${c.hora_inicio}')"
      style="border:1.5px solid #e2e8f0;border-radius:8px;padding:10px 14px;cursor:pointer;margin-bottom:8px;transition:border-color .15s;"
      onmouseover="this.style.borderColor='#ef4444'" onmouseout="this.style.borderColor='#e2e8f0'"
      id="cita-row-${c.id}">
      <div style="font-weight:600;font-size:13px;">${c.profesional}</div>
      <div style="font-size:12px;color:#64748b;">${c.fecha_display} — ${c.hora_inicio}</div>
    </div>`).join('');
  document.getElementById('an-paso1').style.display='none';
  document.getElementById('an-paso2').style.display='block';
}

function seleccionarCita(id, prof, fecha, hora){
  citaAAnular={id, prof, fecha, hora};
  // Resaltar seleccionada
  document.querySelectorAll('[id^="cita-row-"]').forEach(el=>el.style.background='');
  const el=document.getElementById(`cita-row-${id}`);
  if(el){ el.style.background='#fff1f2'; el.style.borderColor='#ef4444'; }
  document.getElementById('an-resultado').innerHTML=`
    <div style="background:#fff1f2;border:1px solid #fca5a5;border-radius:8px;padding:12px 14px;margin-top:12px;">
      <div style="font-size:13px;font-weight:600;color:#dc2626;">¿Confirmar anulación?</div>
      <div style="font-size:12px;color:#64748b;margin-top:4px;">${prof} — ${fecha} ${hora}</div>
      <button onclick="confirmarAnular()" style="margin-top:10px;background:#dc2626;color:#fff;border:none;border-radius:7px;padding:7px 18px;font-size:13px;font-weight:600;cursor:pointer;">Anular esta hora</button>
    </div>`;
}

async function confirmarAnular(){
  if(!citaAAnular) return;
  const res=document.getElementById('an-resultado');
  res.innerHTML='<span style="color:#64748b;font-size:13px;">Anulando...</span>';
  const r=await fetch(`/admin/api/anular?token=${TOKEN}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id_cita:citaAAnular.id})});
  if(r.ok){
    res.innerHTML='<span style="color:#15803d;font-weight:600;">✅ Hora anulada correctamente</span>';
    setTimeout(cerrarModalAnular, 2000);
  } else {
    const e=await r.json();
    res.innerHTML=`<span style="color:#dc2626;">Error: ${e.detail||'No se pudo anular la hora'}</span>`;
  }
}

// ── MODAL ORTODONCIA ──────────────────────────────────────────────────────────
let ortTodosPacientes = [];
let ortModo = 'todos';
let ortVista = 'matriz';
let ortPeriodo = new Date();

function abrirModalOrtodoncia() {
  ortModo = 'todos'; ortVista = 'matriz'; ortPeriodo = new Date();
  document.getElementById('modal-ort').style.display = 'flex';
  cargarOrtodoncia();
}
function cerrarModalOrtodoncia() {
  document.getElementById('modal-ort').style.display = 'none';
}
function ortSetModo(modo) {
  ortModo = modo;
  ['mes','anio','todos'].forEach(k => {
    const el = document.getElementById('ort-btn-'+k);
    el.style.background = k===modo ? '#7e22ce' : '#f1f5f9';
    el.style.color = k===modo ? '#fff' : '#475569';
  });
  document.getElementById('ort-nav-mes').style.display = modo==='todos' ? 'none' : 'flex';
  renderOrtodoncia();
}
function ortSetVista(vista) {
  ortVista = vista;
  document.getElementById('ort-vista-cards').style.background  = vista==='cards'  ? '#7e22ce' : '#f1f5f9';
  document.getElementById('ort-vista-cards').style.color       = vista==='cards'  ? '#fff'    : '#475569';
  document.getElementById('ort-vista-matriz').style.background = vista==='matriz' ? '#7e22ce' : '#f1f5f9';
  document.getElementById('ort-vista-matriz').style.color      = vista==='matriz' ? '#fff'    : '#475569';
  renderOrtodoncia();
}
function ortNavPeriodo(delta) {
  if (ortModo === 'mes') ortPeriodo.setMonth(ortPeriodo.getMonth() + delta);
  else ortPeriodo.setFullYear(ortPeriodo.getFullYear() + delta);
  renderOrtodoncia();
}

async function cargarOrtodoncia() {
  document.getElementById('ort-loading').style.display = 'block';
  document.getElementById('ort-body').innerHTML = '';
  const r = await fetch(`/admin/api/ortodoncia?token=${TOKEN}`);
  const d = await r.json();
  document.getElementById('ort-loading').style.display = 'none';
  if (d.ultima_sync) {
    document.getElementById('ort-ultima-sync').textContent = `Última sync: ${d.ultima_sync}`;
  }
  ortTodosPacientes = d.pacientes || [];
  document.getElementById('ort-loading').style.display = 'none';
  ortSetModo('todos');
  ortSetVista('matriz');
}

function ortFiltrarPacientes() {
  const y = ortPeriodo.getFullYear();
  const m = ortPeriodo.getMonth();
  let label, pacientes;
  if (ortModo === 'todos') {
    label = 'Histórico completo';
    pacientes = ortTodosPacientes;
  } else if (ortModo === 'anio') {
    label = `Año ${y}`;
    pacientes = ortTodosPacientes.map(p => ({
      ...p, visitas: p.visitas.filter(v => v.fecha && v.fecha.startsWith(String(y)))
    })).filter(p => p.visitas.length > 0);
  } else {
    const mes = String(m+1).padStart(2,'0');
    label = `${MESES_ES[m]} ${y}`;
    pacientes = ortTodosPacientes.map(p => ({
      ...p, visitas: p.visitas.filter(v => v.fecha && v.fecha.startsWith(`${y}-${mes}`))
    })).filter(p => p.visitas.length > 0);
  }
  document.getElementById('ort-periodo-label').textContent = label;
  return pacientes;
}

function renderOrtodoncia() {
  const pacientes = ortFiltrarPacientes();
  const conInstalacion = ortTodosPacientes.filter(p => p.visitas.some(v => v.tipo === 'instalacion')).length;
  const totalControles = pacientes.reduce((s,p) => s + p.visitas.filter(v => v.tipo==='control').length, 0);
  const pendientes = pacientes.reduce((s,p) => s + p.visitas.filter(v => v.tipo==='pendiente').length, 0);
  document.getElementById('ort-resumen').innerHTML = `
    <span>🦷 <strong>${pacientes.length}</strong> pacientes${ortModo!=='todos'?' en período':''}</span>
    <span>📦 <strong>${conInstalacion}</strong> con instalación (total histórico)</span>
    <span>⚙️ <strong>${totalControles}</strong> controles en período</span>
    ${pendientes > 0 ? `<span style="color:#dc2626;">⚠️ <strong>${pendientes}</strong> sin clasificar</span>` : ''}
  `;
  // Ordenar: instalación primero, luego por última visita desc
  const sorted = [...pacientes].sort((a,b) => {
    const aI = a.visitas.find(v=>v.tipo==='instalacion');
    const bI = b.visitas.find(v=>v.tipo==='instalacion');
    if (aI && !bI) return -1; if (!aI && bI) return 1;
    return (b.visitas[b.visitas.length-1]?.fecha||'').localeCompare(a.visitas[a.visitas.length-1]?.fecha||'');
  });

  document.getElementById('ort-body-cards').style.display  = ortVista==='cards'  ? 'grid' : 'none';
  document.getElementById('ort-body-matriz').style.display = ortVista==='matriz' ? 'block' : 'none';

  if (!pacientes.length) {
    document.getElementById('ort-body-cards').innerHTML = '<p style="color:#94a3b8;text-align:center;padding:40px;grid-column:1/-1;">Sin visitas en este período.</p>';
    document.getElementById('ort-tabla-matriz').innerHTML = '';
    return;
  }
  if (ortVista === 'cards') renderOrtCards(sorted);
  else renderOrtMatriz(sorted);
}

function renderOrtCards(pacientes) {
  document.getElementById('ort-body-cards').innerHTML = pacientes.map(p => renderTarjetaPaciente(p)).join('');
}

function renderOrtMatriz(pacientes) {
  // Recopilar todas las fechas únicas ordenadas
  const fechasSet = new Set();
  pacientes.forEach(p => p.visitas.forEach(v => fechasSet.add(v.fecha)));
  const fechas = [...fechasSet].sort();

  // Construir tabla
  let html = '<thead><tr style="position:sticky;top:0;z-index:3;background:#f8fafc;">';
  html += '<th style="text-align:left;padding:8px 12px;font-size:12px;font-weight:600;color:#475569;border-bottom:2px solid #e2e8f0;border-right:2px solid #e2e8f0;position:sticky;left:0;background:#f8fafc;z-index:4;min-width:160px;">Paciente</th>';
  fechas.forEach(f => {
    const [yy,mm,dd] = f.split('-');
    html += `<th style="padding:6px 4px;font-size:10px;color:#64748b;border-bottom:2px solid #e2e8f0;text-align:center;min-width:38px;font-weight:500;">${dd}/${mm}<br><span style="color:#94a3b8;">${yy}</span></th>`;
  });
  html += '<th style="padding:8px;font-size:11px;font-weight:700;color:#475569;border-bottom:2px solid #e2e8f0;border-left:2px solid #e2e8f0;text-align:center;position:sticky;right:0;background:#f8fafc;">Total</th>';
  html += '</tr></thead><tbody>';

  pacientes.forEach((p, pi) => {
    const visitaMap = {};
    p.visitas.forEach(v => { visitaMap[v.fecha] = v; });
    const totalVisitas = p.visitas.length;
    const bgRow = pi % 2 === 0 ? '#fff' : '#fafafa';
    html += `<tr style="background:${bgRow};">`;
    html += `<td style="padding:7px 12px;font-size:12px;font-weight:500;color:#1e293b;border-right:2px solid #e2e8f0;position:sticky;left:0;background:${bgRow};white-space:nowrap;max-width:200px;overflow:hidden;text-overflow:ellipsis;" title="${p.nombre}">${p.nombre}</td>`;
    fechas.forEach(f => {
      const v = visitaMap[f];
      if (!v) {
        html += '<td style="text-align:center;padding:4px;border-bottom:1px solid #f1f5f9;"></td>';
      } else {
        const color = v.tipo==='instalacion' ? '#7e22ce' : v.tipo==='control' ? '#0369a1' : '#94a3b8';
        const bg    = v.tipo==='instalacion' ? '#f3e8ff' : v.tipo==='control' ? '#e0f2fe' : '#f1f5f9';
        const title = v.tipo==='instalacion' ? 'Instalación' : v.tipo==='control' ? 'Control' : 'Sin clasificar';
        html += `<td style="text-align:center;padding:4px;border-bottom:1px solid #f1f5f9;">
          <div onclick="toggleOrtTipo(${v.id_atencion},'${v.tipo}',this)" title="${title} - ${fmtFecha(f)}\nClic para cambiar"
            style="width:24px;height:24px;border-radius:50%;background:${bg};border:2px solid ${color};cursor:pointer;margin:0 auto;display:flex;align-items:center;justify-content:center;font-size:9px;color:${color};font-weight:700;">
            ${v.tipo==='instalacion'?'I':v.tipo==='control'?'C':'?'}
          </div>
        </td>`;
      }
    });
    const insColor = p.visitas.some(v=>v.tipo==='instalacion') ? '#7e22ce' : '#94a3b8';
    html += `<td style="text-align:center;padding:7px 10px;border-left:2px solid #e2e8f0;border-bottom:1px solid #f1f5f9;font-weight:700;font-size:13px;color:${insColor};position:sticky;right:0;background:${bgRow};">${totalVisitas}</td>`;
    html += '</tr>';
  });
  html += '</tbody>';
  document.getElementById('ort-tabla-matriz').innerHTML = html;
}

function fmtFecha(f) {
  if (!f) return '';
  const [y,m,d] = f.split('-');
  return `${d}/${m}/${y}`;
}

function renderTarjetaPaciente(p) {
  const instalacion = p.visitas.find(v => v.tipo === 'instalacion');
  const controles   = p.visitas.filter(v => v.tipo === 'control');
  const pendientes  = p.visitas.filter(v => v.tipo === 'pendiente');
  const ultimaVisita = p.visitas[p.visitas.length-1];

  const visitasHtml = p.visitas.map((v,i) => {
    const isIns = v.tipo === 'instalacion';
    const isCtrl = v.tipo === 'control';
    const ctrlNum = controles.indexOf(v) + 1;
    const color = isIns ? '#7e22ce' : isCtrl ? '#0369a1' : '#94a3b8';
    const bg    = isIns ? '#fdf4ff' : isCtrl ? '#f0f9ff' : '#f8fafc';
    const label = isIns ? '📦 Instalación' : isCtrl ? `⚙️ Control ${ctrlNum}` : '❓ Sin clasificar';
    const monto = v.total ? ` · $${v.total.toLocaleString('es-CL')}` : '';
    return `<div style="display:flex;align-items:center;gap:6px;padding:4px 8px;border-radius:6px;background:${bg};margin-bottom:4px;cursor:pointer;"
                 onclick="toggleOrtTipo(${v.id_atencion}, '${v.tipo}', this)"
                 title="Clic para cambiar tipo">
      <span style="font-size:11px;color:${color};font-weight:600;min-width:100px;">${label}</span>
      <span style="font-size:11px;color:#475569;">${fmtFecha(v.fecha)}${monto}</span>
      <span style="font-size:10px;color:#cbd5e1;margin-left:auto;">✏️</span>
    </div>`;
  }).join('');

  const bordeColor = instalacion ? '#d8b4fe' : '#e2e8f0';
  return `<div style="border:1px solid ${bordeColor};border-radius:10px;padding:14px;background:#fff;">
    <div style="font-weight:600;font-size:13px;color:#1e293b;margin-bottom:2px;">${p.nombre}</div>
    <div style="font-size:11px;color:#94a3b8;margin-bottom:10px;">
      ${instalacion ? `Instalación: ${fmtFecha(instalacion.fecha)}` : '<span style="color:#f59e0b;">Sin instalación</span>'}
      · ${controles.length} control${controles.length!==1?'es':''}
      · Última visita: ${fmtFecha(ultimaVisita?.fecha)}
    </div>
    ${visitasHtml}
  </div>`;
}

async function toggleOrtTipo(idAtencion, tipoActual, el) {
  const orden = ['instalacion', 'control', 'pendiente'];
  const idx = orden.indexOf(tipoActual);
  const nuevoTipo = orden[(idx + 1) % orden.length];
  el.style.opacity = '0.4';
  await fetch(`/admin/api/ortodoncia/${idAtencion}?token=${TOKEN}`, {
    method: 'PUT',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({tipo: nuevoTipo})
  });
  // Actualizar localmente sin recargar todo
  for (const p of ortTodosPacientes) {
    const v = p.visitas.find(v => v.id_atencion === idAtencion);
    if (v) { v.tipo = nuevoTipo; v.tipo_manual = 1; break; }
  }
  renderOrtodoncia();
}

// ── MODAL PACIENTES EN TRATAMIENTO ───────────────────────────────────────────
const MESES_ES = ["Enero","Febrero","Marzo","Abril","Mayo","Junio",
                  "Julio","Agosto","Septiembre","Octubre","Noviembre","Diciembre"];

let kineMesActual = new Date();
let kineDatos = [];
let kineEspActual = "kinesiologia";
let kineEspLabel = "Kinesiología";
let kinePrecioFonasa = 7830;
let kinePrecioParticular = 20000;
let kineModo = 'mes'; // 'mes' | 'anio' | 'todos'

function abrirModalKine() {
  kineMesActual = new Date();
  kineEspActual = "kinesiologia";
  kineModo = 'mes';
  document.getElementById('kine-esp-select').value = 'kinesiologia';
  document.getElementById('modal-kine').style.display = 'flex';
  kineSetModo('mes');
}
function cerrarModalKine() {
  document.getElementById('modal-kine').style.display = 'none';
}
function kineNavMes(delta) {
  if (kineModo === 'mes') kineMesActual.setMonth(kineMesActual.getMonth() + delta);
  else if (kineModo === 'anio') kineMesActual.setFullYear(kineMesActual.getFullYear() + delta);
  cargarKine();
}
function kineSetModo(modo) {
  kineModo = modo;
  const btns = {mes:'kine-btn-mes', anio:'kine-btn-anio', todos:'kine-btn-todos'};
  Object.entries(btns).forEach(([k,id]) => {
    const el = document.getElementById(id);
    el.style.background = k===modo ? '#7c3aed' : '#f1f5f9';
    el.style.color = k===modo ? '#fff' : '#475569';
  });
  const nav = document.getElementById('kine-nav-mes');
  nav.style.display = modo === 'todos' ? 'none' : 'flex';
  cargarKine();
}
async function cargarKine() {
  const y = kineMesActual.getFullYear();
  const m = String(kineMesActual.getMonth()+1).padStart(2,'0');
  let mesParam, label;
  if (kineModo === 'todos') { mesParam = 'todos'; label = 'Histórico completo'; }
  else if (kineModo === 'anio') { mesParam = String(y); label = `Año ${y}`; }
  else { mesParam = `${y}-${m}`; label = `${MESES_ES[kineMesActual.getMonth()]} ${y}`; }
  document.getElementById('kine-mes-label').textContent = label;
  document.getElementById('kine-loading').style.display = 'block';
  document.getElementById('kine-empty').style.display = 'none';
  document.getElementById('kine-tbody').innerHTML = '';
  document.getElementById('kine-resumen').innerHTML = '';
  try {
    const r = await fetch(`/admin/api/kine?mes=${mesParam}&especialidad=${kineEspActual}&token=${TOKEN}`);
    const d = await r.json();
    kineDatos = d.pacientes || [];
    kineEspLabel = d.especialidad_label || kineEspActual;
    kinePrecioFonasa = (d.pacientes[0]?.precio_fonasa) || 0;
    kinePrecioParticular = (d.pacientes[0]?.precio_particular) || 0;
    renderKine();
  } catch(e) {
    document.getElementById('kine-loading').style.display = 'none';
    document.getElementById('kine-empty').style.display = 'block';
    document.getElementById('kine-empty').textContent = 'Error al cargar datos';
  }
}
function renderKine() {
  document.getElementById('kine-loading').style.display = 'none';
  const tbody = document.getElementById('kine-tbody');
  if (!kineDatos.length) {
    document.getElementById('kine-empty').style.display = 'block';
    document.getElementById('kine-resumen').innerHTML = '';
    return;
  }
  document.getElementById('kine-empty').style.display = 'none';

  // Resumen
  const totalSesiones = kineDatos.reduce((s,p)=>s+p.sesiones_mes,0);
  const totalPacientes = kineDatos.length;
  const ingFonasa = kineDatos.filter(p=>p.modalidad==='fonasa').reduce((s,p)=>s+p.sesiones_mes*(p.precio_fonasa||kinePrecioFonasa),0);
  const ingPart   = kineDatos.filter(p=>p.modalidad!=='fonasa').reduce((s,p)=>s+p.sesiones_mes*(p.precio_particular||kinePrecioParticular),0);
  const ingTotal  = ingFonasa + ingPart;
  document.getElementById('kine-resumen').innerHTML = `
    <span>👤 <strong>${totalPacientes}</strong> pacientes</span>
    <span>📋 <strong>${totalSesiones}</strong> sesiones realizadas</span>
    <span>💰 Ingresos estimados: <strong>$${ingTotal.toLocaleString('es-CL')}</strong></span>
    <span style="color:#64748b;">(Fonasa $${ingFonasa.toLocaleString('es-CL')} · Particular $${ingPart.toLocaleString('es-CL')})</span>
  `;

  tbody.innerHTML = kineDatos.map((p,i) => {
    const total = p.total_sesiones || 0;
    const hechas = p.sesiones_mes;
    const pct = total ? Math.min(100, Math.round(hechas/total*100)) : 0;
    const barColor = pct >= 100 ? '#16a34a' : pct >= 60 ? '#f59e0b' : '#3b82f6';
    const progBar = total ? `
      <div style="display:flex;align-items:center;gap:6px;">
        <div style="flex:1;background:#e2e8f0;border-radius:99px;height:8px;min-width:60px;">
          <div style="width:${pct}%;background:${barColor};border-radius:99px;height:8px;transition:width .3s;"></div>
        </div>
        <span style="font-size:11px;color:#475569;white-space:nowrap;">${hechas}/${total}</span>
      </div>` : `<span style="font-size:11px;color:#94a3b8;">—</span>`;

    const profNombre = p.prof_nombre || '—';
    // Nombre corto: tomar primera palabra que no sea Dr/Dra
    const profParts = profNombre.replace(/^Dr\.?\s*|^Dra\.?\s*/i,'').split(' ');
    const profShort = profParts[profParts.length-1]; // apellido
    const profColors = ['#dbeafe','#dcfce7','#fef9c3','#ede9fe','#ffedd5'];
    const profTextColors = ['#1d4ed8','#15803d','#854d0e','#6d28d9','#9a3412'];
    const profColorIdx = p.id_prof % profColors.length;
    const rut = p.rut || '—';
    const nombre = p.paciente_nombre || rut;

    return `<tr style="border-bottom:1px solid #f1f5f9;${i%2===1?'background:#fafafa':''}" id="kine-row-${i}">
      <td style="padding:10px 12px;">
        <div style="font-weight:600;color:#1e293b;font-size:13px;">${nombre}</div>
        <div style="font-size:11px;color:#94a3b8;">${rut}</div>
      </td>
      <td style="padding:10px 12px;">
        <span style="background:${profColors[profColorIdx]};color:${profTextColors[profColorIdx]};border-radius:20px;padding:2px 10px;font-size:11px;font-weight:600;">${profShort}</span>
      </td>
      <td style="padding:10px 12px;text-align:center;">
        <span style="font-size:16px;font-weight:700;color:#1172AB;">${hechas}</span>
        <div style="font-size:10px;color:#94a3b8;">sesiones</div>
      </td>
      <td style="padding:10px 12px;text-align:center;">
        <input type="number" min="0" max="50" value="${total}"
          style="width:56px;text-align:center;border:1px solid #e2e8f0;border-radius:6px;padding:4px;font-size:13px;"
          onchange="kineDatos[${i}].total_sesiones=parseInt(this.value)||0;guardarKine(${i})">
      </td>
      <td style="padding:10px 12px;">${progBar}</td>
      <td style="padding:10px 12px;text-align:center;">
        <select style="border:1px solid #e2e8f0;border-radius:6px;padding:4px 6px;font-size:12px;"
          onchange="kineDatos[${i}].modalidad=this.value;guardarKine(${i});renderKine()">
          <option value="fonasa" ${p.modalidad==='fonasa'?'selected':''}>Fonasa</option>
          <option value="particular" ${p.modalidad==='particular'?'selected':''}>Particular</option>
        </select>
      </td>
      <td style="padding:10px 12px;">
        <input type="text" placeholder="Notas..." value="${(p.notas||'').replace(/"/g,'&quot;')}"
          style="width:100%;border:1px solid #e2e8f0;border-radius:6px;padding:4px 8px;font-size:12px;"
          onchange="kineDatos[${i}].notas=this.value;guardarKine(${i})">
      </td>
      <td style="padding:10px 8px;text-align:center;">
        <span id="kine-saved-${i}" style="font-size:11px;color:#16a34a;display:none;">✓</span>
      </td>
    </tr>`;
  }).join('');
}
async function guardarKine(i) {
  const p = kineDatos[i];
  await fetch(`/admin/api/kine/${p.id_paciente}/${p.id_prof}?token=${TOKEN}`, {
    method: 'PUT',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({total_sesiones: p.total_sesiones, modalidad: p.modalidad, notas: p.notas})
  });
  const el = document.getElementById(`kine-saved-${i}`);
  if (el) { el.style.display='inline'; setTimeout(()=>el.style.display='none', 2000); }
}
</script>
</body>
</html>'''


@app.get("/health")
async def health():
    """Healthcheck básico + ping a Medilink con timeout corto.
    Responde 200 siempre que el proceso esté vivo; reporta el estado de dependencias."""
    from config import MEDILINK_BASE_URL
    medilink_ok = False
    medilink_ms = None
    try:
        t0 = monotonic()
        async with httpx.AsyncClient(timeout=3) as client:
            r = await client.get(f"{MEDILINK_BASE_URL}/sucursales", headers=HEADERS_MEDILINK)
        medilink_ms = int((monotonic() - t0) * 1000)
        medilink_ok = r.status_code < 500
    except (httpx.TimeoutException, httpx.NetworkError, httpx.RequestError):
        medilink_ok = False
    return {
        "status":   "ok",
        "medilink": "ok" if medilink_ok else "degraded",
        "medilink_ms": medilink_ms,
    }


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


# ── Panel de recepcionistas ────────────────────────────────────────────────────

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


@app.get("/admin/api/conversations")
def admin_conversations(_: str = Depends(require_admin)):
    return get_conversations()


@app.get("/admin/api/conversations/{phone}")
def admin_conversation_detail(phone: str, _: str = Depends(require_admin)):
    return get_messages(phone)


@app.get("/admin/api/metrics")
def admin_metrics(_: str = Depends(require_admin)):
    return get_metricas(dias=30)


@app.post("/admin/api/takeover/{phone}")
async def admin_takeover(phone: str, _: str = Depends(require_admin)):
    """Recepcionista toma control manual de una conversación."""
    session = get_session(phone)
    save_session(phone, "HUMAN_TAKEOVER", {"hold_sent": True, "msgs_sin_respuesta": 0,
                                            "handoff_reason": "manual (recepcionista)"})
    log_event(phone, "derivado_humano", {"razon": "takeover manual desde panel"})
    await send_whatsapp(phone,
        "Hola 👋 Te está atendiendo una recepcionista del Centro Médico Carampangue.\n"
        "¿En qué te podemos ayudar?")
    log_message(phone, "out", "[Recepcionista tomó la conversación]", "HUMAN_TAKEOVER")
    return {"ok": True}


@app.post("/admin/api/reply")
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


@app.post("/admin/api/resume/{phone}")
async def admin_resume(phone: str, _: str = Depends(require_admin)):
    """Devuelve el control al bot y notifica al paciente."""
    reset_session(phone)
    log_event(phone, "bot_reanudado")
    await send_whatsapp(phone,
        "Continuamos con el asistente automático 😊\n"
        "Escribe *menu* cuando quieras.")
    log_message(phone, "out", "[Bot reanudado por recepcionista]", "IDLE")
    return {"ok": True}


@app.get("/admin/api/paciente")
async def admin_buscar_paciente(rut: str, _: str = Depends(require_admin)):
    """Busca un paciente en Medilink por RUT."""
    paciente = await buscar_paciente(rut)
    if not paciente:
        raise HTTPException(status_code=404, detail="Paciente no encontrado")
    return paciente


@app.get("/admin/api/slots")
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


@app.post("/admin/api/agendar")
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


@app.get("/admin/api/especialidades")
def admin_especialidades(_: str = Depends(require_admin)):
    """Retorna la lista de especialidades únicas disponibles."""
    from medilink import PROFESIONALES
    esp = sorted({v["especialidad"] for v in PROFESIONALES.values()})
    return {"especialidades": esp}


@app.get("/admin/api/tags/{phone}")
def admin_get_tags(phone: str, _: str = Depends(require_admin)):
    return {"tags": get_tags(phone)}


@app.post("/admin/api/tags/{phone}")
async def admin_add_tag(phone: str, request: Request, _: str = Depends(require_admin)):
    body = await request.json()
    tag = body.get("tag", "").strip()
    if not tag:
        raise HTTPException(status_code=400, detail="tag requerido")
    save_tag(phone, tag)
    return {"tags": get_tags(phone)}


@app.delete("/admin/api/tags/{phone}/{tag}")
def admin_delete_tag(phone: str, tag: str, _: str = Depends(require_admin)):
    delete_tag(phone, tag)
    return {"tags": get_tags(phone)}


@app.get("/admin/api/search")
def admin_search_messages(q: str, _: str = Depends(require_admin)):
    """Busca texto en todos los mensajes de todas las conversaciones."""
    if len(q.strip()) < 2:
        raise HTTPException(status_code=400, detail="Mínimo 2 caracteres")
    results = search_messages(q.strip())
    return {"q": q, "results": results}


@app.get("/admin/api/citas-paciente")
async def admin_citas_paciente(rut: str, _: str = Depends(require_admin)):
    """Retorna las citas futuras de un paciente buscado por RUT."""
    paciente = await buscar_paciente(rut)
    if not paciente:
        raise HTTPException(status_code=404, detail="Paciente no encontrado")
    citas = await listar_citas_paciente(paciente["id"])
    return {"paciente": paciente, "citas": citas}


@app.post("/admin/api/anular")
async def admin_anular_cita(request: Request, _: str = Depends(require_admin)):
    """Anula una cita por su ID de Medilink."""
    body = await request.json()
    id_cita = int(body.get("id_cita"))
    ok = await cancelar_cita(id_cita)
    if not ok:
        raise HTTPException(status_code=400, detail="No se pudo anular la cita en Medilink")
    log_event("admin", "cita_anulada_panel", {"id_cita": id_cita})
    return {"ok": True}


@app.get("/admin/api/kine")
async def admin_kine(mes: str = None, especialidad: str = "kinesiologia",
                     _: str = Depends(require_admin)):
    """Retorna citas de una especialidad recurrente.
    mes=YYYY-MM → mes específico | mes=YYYY → año completo | mes=todos → todo el histórico"""
    from datetime import date
    from session import get_citas_cache_todos
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
        from collections import defaultdict
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
        from collections import defaultdict
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
            fechas = sorted({f for i in items for f in i.get("fechas", [i.get("primera_fecha","")])} )
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


@app.get("/admin/api/kine/especialidades")
def admin_kine_especialidades(_: str = Depends(require_admin)):
    return {"especialidades": [
        {"id": k, "label": v["label"]} for k, v in SEGUIMIENTO_ESPECIALIDADES.items()
    ]}


@app.post("/admin/api/kine/sync")
async def admin_kine_sync(fecha: str = None, _: str = Depends(require_admin)):
    """Fuerza sincronización del caché de citas para una fecha (default: hoy)."""
    from datetime import date
    if not fecha:
        fecha = date.today().strftime("%Y-%m-%d")
    ids_todos = list({i for cfg in SEGUIMIENTO_ESPECIALIDADES.values() for i in cfg["ids"]})
    await sync_citas_dia(fecha, ids_todos)
    return {"ok": True, "fecha": fecha, "ids": ids_todos}


@app.put("/admin/api/kine/{id_paciente}/{id_prof}")
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


# ─── Ortodoncia ──────────────────────────────────────────────────────────────

@app.get("/admin/api/ortodoncia")
def admin_ortodoncia_pacientes(_: str = Depends(require_ortodoncia)):
    pacientes = get_ortodoncia_pacientes()
    ultima_sync = get_ortodoncia_sync_max_fecha()
    return {"pacientes": pacientes, "ultima_sync": ultima_sync}


@app.put("/admin/api/ortodoncia/{id_atencion}")
async def admin_ortodoncia_tipo(id_atencion: int, request: Request,
                                _: str = Depends(require_ortodoncia)):
    body = await request.json()
    tipo = body.get("tipo")
    if tipo not in ("instalacion", "control", "pendiente"):
        raise HTTPException(status_code=400, detail="tipo debe ser instalacion, control o pendiente")
    set_ortodoncia_tipo(id_atencion, tipo)
    return {"ok": True}


@app.post("/admin/api/ortodoncia/sync")
async def admin_ortodoncia_sync(desde: str = "2025-01-01", hasta: str = None,
                                _: str = Depends(require_ortodoncia)):
    from datetime import date
    fin = hasta or date.today().isoformat()
    asyncio.create_task(sync_ortodoncia_rango(desde, fin))
    return {"ok": True, "desde": desde, "hasta": fin}


@app.get("/admin", response_class=HTMLResponse)
def admin_panel(token: str = Query(ADMIN_TOKEN)):
    # El panel HTML requiere token por query param porque necesita inyectarlo
    # en el JS embebido; las llamadas API del panel sí pueden usar Bearer header.
    if token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Token inválido")
    return _ADMIN_HTML.replace("__TOKEN__", token)


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
    """Recibe mensajes de Meta Cloud API (WhatsApp, Instagram, Messenger)."""
    data = await request.json()
    obj = data.get("object", "")

    # ── Instagram DMs ────────────────────────────────────────────────────────
    if obj == "instagram":
        try:
            for entry in data.get("entry", []):
                for ev in entry.get("messaging", []):
                    sender_id = ev.get("sender", {}).get("id", "")
                    msg = ev.get("message", {})
                    if not sender_id or not msg or msg.get("is_echo"):
                        continue
                    texto = msg.get("text", "")
                    if not texto:
                        continue
                    msg_id = msg.get("mid", "")
                    if msg_id and is_duplicate(msg_id):
                        continue
                    phone = f"ig_{sender_id}"
                    if _rate_limited(phone):
                        log.warning("Rate limit excedido IG phone=%s", phone)
                        continue
                    log.info("INSTAGRAM from=%s text=%r", phone, texto[:80])
                    save_session(phone, "HUMAN_TAKEOVER", {})
                    log_message(phone, "in", texto, "HUMAN_TAKEOVER", canal="instagram")
        except Exception as e:
            log.warning("Error procesando Instagram webhook: %s", e)
        return Response(status_code=200)

    # ── Facebook Messenger ───────────────────────────────────────────────────
    if obj == "page":
        try:
            for entry in data.get("entry", []):
                for ev in entry.get("messaging", []):
                    sender_id = ev.get("sender", {}).get("id", "")
                    msg = ev.get("message", {})
                    if not sender_id or not msg or msg.get("is_echo"):
                        continue
                    texto = msg.get("text", "")
                    if not texto:
                        continue
                    msg_id = msg.get("mid", "")
                    if msg_id and is_duplicate(msg_id):
                        continue
                    phone = f"fb_{sender_id}"
                    if _rate_limited(phone):
                        log.warning("Rate limit excedido FB phone=%s", phone)
                        continue
                    log.info("MESSENGER from=%s text=%r", phone, texto[:80])
                    save_session(phone, "HUMAN_TAKEOVER", {})
                    log_message(phone, "in", texto, "HUMAN_TAKEOVER", canal="messenger")
        except Exception as e:
            log.warning("Error procesando Messenger webhook: %s", e)
        return Response(status_code=200)

    # ── WhatsApp ─────────────────────────────────────────────────────────────
    try:
        entry = data["entry"][0]
        change = entry["changes"][0]["value"]

        if "messages" not in change:
            return Response(status_code=200)

        msg = change["messages"][0]
        msg_type = msg.get("type")

        # Extraer texto de mensajes de texto o respuestas interactivas
        if msg_type == "text":
            texto = msg["text"]["body"].strip()
        elif msg_type == "interactive":
            interactive = msg.get("interactive", {})
            itype = interactive.get("type", "")
            if itype == "button_reply":
                texto = interactive["button_reply"]["id"]
            elif itype == "list_reply":
                texto = interactive["list_reply"]["id"]
            else:
                return Response(status_code=200)
        else:
            return Response(status_code=200)

        phone = msg["from"].lstrip("+")  # normalizar: siempre sin + (ej: "56987834148")
        msg_id = msg.get("id", "")

        if msg_id and is_duplicate(msg_id):
            log.info("MSG duplicado ignorado id=%s from=%s", msg_id, phone)
            return Response(status_code=200)

        if _rate_limited(phone):
            log.warning("Rate limit excedido WA phone=%s text=%r", phone, texto[:80])
            return Response(status_code=200)

        log.info("MSG from=%s id=%s text=%r", phone, msg_id, texto[:100])

        session = get_session(phone)
        state_before = session.get("state", "IDLE")
        log_message(phone, "in", texto, state_before, canal="whatsapp")

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

        state_after = get_session(phone).get("state", "IDLE")

        # Guardar respuesta del bot (texto plano o resumen del interactivo)
        if isinstance(respuesta, dict) and respuesta.get("type") == "interactive":
            resp_text = respuesta["interactive"].get("body", {}).get("text", "[mensaje interactivo]")
        else:
            resp_text = str(respuesta) if respuesta else ""

        if resp_text:
            log_message(phone, "out", resp_text, state_after, canal="whatsapp")
        log.info("BOT to=%s state=%s reply=%r", phone, state_after, resp_text[:80])

        if not respuesta:
            pass  # silencio intencional (HUMAN_TAKEOVER sin respuesta automática)
        elif isinstance(respuesta, dict) and respuesta.get("type") == "interactive":
            await send_whatsapp_interactive(phone, respuesta["interactive"])
        else:
            await send_whatsapp(phone, respuesta)

    except (KeyError, IndexError) as e:
        log.warning("Payload inesperado: %s | data=%s", e, str(data)[:200])

    return Response(status_code=200)
