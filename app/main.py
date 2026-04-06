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
from fastapi import FastAPI, Request, Response, Query, HTTPException
from fastapi.responses import HTMLResponse
from config import META_ACCESS_TOKEN, META_PHONE_NUMBER_ID, META_VERIFY_TOKEN, CMC_TELEFONO, ADMIN_TOKEN
from flows import handle_message
from reminders import enviar_recordatorios
from session import get_session, is_duplicate, reset_session, save_session, get_metricas, log_message, get_messages, get_conversations, log_event, get_sesiones_abandonadas

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
    scheduler.start()
    log.info("Scheduler iniciado — recordatorios 09:00 CLT · reenganche cada 5 min")
    yield
    scheduler.shutdown()


app = FastAPI(title="CMC WhatsApp Bot", lifespan=lifespan)

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


_ADMIN_HTML = '''<!DOCTYPE html>
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
    <div class="brand-icon">
      <svg width="20" height="20" viewBox="0 0 20 20" fill="none">
        <path d="M10 3v14M3 10h14" stroke="white" stroke-width="2.5" stroke-linecap="round"/>
      </svg>
    </div>
    <div class="brand-text">
      <h1>Centro Médico Carampangue</h1>
      <p>Panel de Recepción · WhatsApp Bot</p>
    </div>
  </div>
  <div class="topbar-pills">
    <div class="pill"><strong id="s-total">—</strong>&nbsp;conversaciones</div>
    <div class="pill amber"><strong id="s-flujo">—</strong>&nbsp;en flujo</div>
    <div class="pill" id="pill-esperando"><strong id="s-takeover">—</strong>&nbsp;esperando atención</div>
    <div class="pill"><div class="live-dot"></div>&nbsp;Actualizado <span id="last-refresh">—</span></div>
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
      <button class="state-btn active" id="btn-all" onclick="setFilter(\'all\',this)">
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
            <span class="state-tag tag-slate" id="chat-state-tag">—</span>
            <button class="btn btn-danger" id="btn-takeover" onclick="doTakeover()">🎯 Tomar control</button>
            <button class="btn btn-outline" id="btn-resume" style="display:none;" onclick="doResume()">🤖 Devolver al bot</button>
          </div>
        </div>
        <div id="takeover-banner" class="takeover-banner hidden">
          🙋 Estás respondiendo como recepcionista — el bot está pausado para este paciente
        </div>
        <div class="quick-replies hidden" id="quick-replies"></div>
        <div class="chat-messages" id="chat-messages"></div>
        <div class="reply-bar hidden" id="reply-bar">
          <textarea class="reply-textarea" id="reply-input" placeholder="Escribe tu respuesta..." rows="2"
            onkeydown="if(event.key===\'Enter\'&&!event.shiftKey){event.preventDefault();sendReply();}"></textarea>
          <div class="reply-actions">
            <button class="btn btn-outline" onclick="document.getElementById(\'reply-input\').value=\'\'">Limpiar</button>
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
  const diff = Math.floor((Date.now() - new Date(ts.replace(" ","T")+"Z")) / 1000);
  if (diff < 60) return "ahora";
  if (diff < 3600) return Math.floor(diff/60)+"m";
  if (diff < 86400) return Math.floor(diff/3600)+"h";
  return Math.floor(diff/86400)+"d";
}
function waitMinutes(ts) {
  if (!ts) return 0;
  return Math.floor((Date.now() - new Date(ts.replace(" ","T")+"Z")) / 60000);
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
    html += `<button class="state-btn${currentFilter===g.id?" active":""}" onclick="setFilter(\'${g.id}\',this)">
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
      🩺 ${esp} <span class="esp-btn-count">${cnt}</span>
    </button>`
  ).join("");
}

/* ── LISTA ── */
function sortedConvs(list) {
  return list.slice().sort((a,b) => {
    const gA = STATE_GROUPS.findIndex(g=>g.states.includes(a.state));
    const gB = STATE_GROUPS.findIndex(g=>g.states.includes(b.state));
    if (gA!==gB) return gA-gB;
    // Dentro del mismo grupo: mayor tiempo de espera primero
    return waitMinutes(b.last_ts||b.updated_at) - waitMinutes(a.last_ts||a.updated_at);
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
function convCard(c,g) {
  const name = c.nombre||c.phone;
  const preview = c.last_text ? c.last_text.substring(0,60) : "Sin mensajes";
  const dir = c.last_dir==="in" ? "" : "← ";
  const isTakeover = c.state==="HUMAN_TAKEOVER";
  const fd = c.flow_data||{};
  const mins = waitMinutes(c.last_ts||c.updated_at);
  let badges="";
  if (c.msgs_sin_respuesta>0) badges+=`<span class="badge badge-unread">${c.msgs_sin_respuesta}</span>`;
  if (mins>=15) badges+=`<span class="badge badge-urgent">⏰ ${mins}m sin respuesta</span>`;
  else if (mins>=5) badges+=`<span class="badge badge-warn">⏱ ${mins}m esperando</span>`;
  else if (mins>=1&&c.state!=="IDLE") badges+=`<span class="badge" style="background:#f8fafc;color:#94a3b8;border-color:#e2e8f0;">⏱ ${mins}m</span>`;
  if (fd.fecha_display&&fd.hora_inicio) badges+=`<span class="badge badge-prob">✅ Lista para agendar</span>`;
  return `<div class="conv-card${selectedPhone===c.phone?" selected":""}" style="border-left-color:${g.dot};" onclick="selectConv(\'${c.phone}\')">
    <div class="card-top">
      <span class="cdot" style="background:${dotColor(c.state)};"></span>
      <span class="cname">${name.replace(/</g,"&lt;")}</span>
      <span class="ctime">${relTime(c.last_ts||c.updated_at)}</span>
    </div>
    <div class="cstate" style="color:${g.dot};">${stateLabel(c.state)}</div>
    ${fd.especialidad?`<div class="cesp">🩺 ${fd.especialidad}</div>`:""}
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
  document.getElementById("chat-sub").textContent=conv?.rut?`RUT ${conv.rut} · ${phone}`:phone;
  updateChatControls(conv?.state||"IDLE");
  updateContextPanel(conv);
  await loadMessages(phone);
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
  if(fd.especialidad){ew.style.display="flex";document.getElementById("ctx-especialidad").textContent=fd.especialidad;}else ew.style.display="none";
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
async function loadMessages(phone) {
  try {
    const r=await fetch(`/admin/api/conversations/${encodeURIComponent(phone)}?token=${TOKEN}`);
    renderMessages(await r.json());
  } catch(e){console.error(e);}
}
function renderMessages(msgs) {
  const el=document.getElementById("chat-messages");
  if(!msgs.length){el.innerHTML=`<div style="text-align:center;color:var(--text-3);font-size:12px;padding:20px;">Sin mensajes registrados aún</div>`;return;}
  let html=""; let lastState=null;
  msgs.forEach(m=>{
    if(m.state&&m.state!==lastState){html+=`<div class="state-sep"><span class="state-pill">${stateLabel(m.state)}</span></div>`;lastState=m.state;}
    const isRecep=m.direction==="out"&&(m.text||"").startsWith("[Recepcionista]");
    const text=(m.text||"").replace(/^\[Recepcionista\] /,"").replace(/^\[.*?\] /,"")
      .replace(/</g,"&lt;").replace(/\\n/g,"<br>").replace(/\*(.*?)\*/g,"<strong>$1</strong>");
    const ts=m.ts?new Date(m.ts.replace(" ","T")+"Z").toLocaleTimeString("es-CL",{hour:"2-digit",minute:"2-digit"}):"";
    const who=m.direction==="in"?"👤 Paciente":isRecep?"🙋 Recepcionista":"🤖 Bot";
    html+=`<div class="msg-row ${m.direction}${isRecep?" recep":""}"><div><div class="msg-bubble">${text}</div><div class="msg-meta">${who} · ${ts}</div></div></div>`;
  });
  el.innerHTML=html; el.scrollTop=el.scrollHeight;
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
      if(still){updateChatControls(still.state);updateContextPanel(still);await loadMessages(selectedPhone);}
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
</script>
</body>
</html>'''


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


# ── Panel de recepcionistas ────────────────────────────────────────────────────

def _check_token(token: str):
    if token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Token inválido")


@app.get("/admin/api/conversations")
def admin_conversations(token: str = Query(...)):
    _check_token(token)
    return get_conversations()


@app.get("/admin/api/conversations/{phone}")
def admin_conversation_detail(phone: str, token: str = Query(...)):
    _check_token(token)
    return get_messages(phone)


@app.get("/admin/api/metrics")
def admin_metrics(token: str = Query(...)):
    _check_token(token)
    return get_metricas(dias=30)


@app.post("/admin/api/takeover/{phone}")
async def admin_takeover(phone: str, token: str = Query(...)):
    """Recepcionista toma control manual de una conversación."""
    _check_token(token)
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
async def admin_reply(request: Request, token: str = Query(...)):
    """Recepcionista envía un mensaje al paciente desde el panel."""
    _check_token(token)
    body = await request.json()
    phone = body.get("phone", "").strip()
    message = body.get("message", "").strip()
    if not phone or not message:
        raise HTTPException(status_code=400, detail="phone y message son requeridos")
    await send_whatsapp(phone, message)
    state = get_session(phone).get("state", "HUMAN_TAKEOVER")
    log_message(phone, "out", f"[Recepcionista] {message}", state)
    log_event(phone, "recepcionista_respondio", {"mensaje": message[:200]})
    return {"ok": True}


@app.post("/admin/api/resume/{phone}")
async def admin_resume(phone: str, token: str = Query(...)):
    """Devuelve el control al bot y notifica al paciente."""
    _check_token(token)
    reset_session(phone)
    log_event(phone, "bot_reanudado")
    await send_whatsapp(phone,
        "Continuamos con el asistente automático 😊\n"
        "Escribe *menu* cuando quieras.")
    log_message(phone, "out", "[Bot reanudado por recepcionista]", "IDLE")
    return {"ok": True}


@app.get("/admin", response_class=HTMLResponse)
def admin_panel(token: str = Query(ADMIN_TOKEN)):
    _check_token(token)
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
    """Recibe mensajes de Meta Cloud API."""
    data = await request.json()

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

        phone = msg["from"]
        msg_id = msg.get("id", "")

        if msg_id and is_duplicate(msg_id):
            log.info("MSG duplicado ignorado id=%s from=%s", msg_id, phone)
            return Response(status_code=200)

        log.info("MSG from=%s id=%s text=%r", phone, msg_id, texto[:100])

        session = get_session(phone)
        state_before = session.get("state", "IDLE")
        log_message(phone, "in", texto, state_before)

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
            log_message(phone, "out", resp_text, state_after)
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
