"""
Chatbot WhatsApp — Centro Médico Carampangue
Webhook de Meta Cloud API → FastAPI → Claude + Medilink
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

import logging
import logging.config
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from time import monotonic

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, Request, Response, Query, HTTPException, Cookie
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from config import (META_VERIFY_TOKEN, CMC_TELEFONO, ADMIN_TOKEN,
                    MEDILINK_TOKEN)
from flows import handle_message
from messaging import (send_whatsapp, send_whatsapp_interactive,
                       send_whatsapp_location,
                       react_whatsapp, unreact_whatsapp,
                       download_whatsapp_media, transcribe_audio,
                       extract_text_from_pdf, extract_text_from_docx)
from session import (get_session, is_duplicate, reset_session, save_session,
                     get_metricas, log_message, log_event,
                     intent_queue_depth, waitlist_depth, purge_old_data,
                     upsert_message_status, upsert_bsuid,
                     get_profile, save_profile)
from resilience import is_medilink_down
from jobs import (_enviar_reenganche, _sync_citas_hoy,
                  _job_recordatorios, _job_recordatorios_2h,
                  _job_postconsulta, _job_reactivacion,
                  _job_adherencia_kine, _job_control_especialidad,
                  _job_crosssell_kine, _job_medilink_watchdog,
                  _job_waitlist_check,
                  _job_doctor_resumen_precita, _job_doctor_reporte_progreso,
                  _job_doctor_reset_diario,
                  _job_cumpleanos, _job_winback)
import admin_routes
import portal_routes

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

HEADERS_MEDILINK = {"Authorization": f"Token {MEDILINK_TOKEN}"}


# ── Rate limiter en memoria (sliding window por teléfono) ────────────────────
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


# ── Lifespan & scheduler ─────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    _CLT = "America/Santiago"
    # Recordatorios 24h: todos los días a las 9:00 AM CLT
    scheduler.add_job(
        _job_recordatorios,
        CronTrigger(hour=9, minute=0, timezone=_CLT),
        id="recordatorios_diarios",
        replace_existing=True,
    )
    # Recordatorios 2h: cada 15 min entre 7:30 y 21:30 CLT
    scheduler.add_job(
        _job_recordatorios_2h,
        CronTrigger(hour="7-21", minute="0,15,30,45", timezone=_CLT),
        id="recordatorios_2h",
        replace_existing=True,
    )
    # Reenganche: cada 5 minutos revisa sesiones abandonadas
    scheduler.add_job(
        _enviar_reenganche,
        "interval", minutes=5,
        id="reenganche",
        replace_existing=True,
    )
    # Post-consulta: todos los días a las 10:00 AM CLT
    scheduler.add_job(
        _job_postconsulta,
        CronTrigger(hour=10, minute=0, timezone=_CLT),
        id="seguimiento_postconsulta",
        replace_existing=True,
    )
    # Reactivación: todos los lunes a las 10:30 AM CLT
    scheduler.add_job(
        _job_reactivacion,
        CronTrigger(day_of_week="mon", hour=10, minute=30, timezone=_CLT),
        id="reactivacion_pacientes",
        replace_existing=True,
    )
    # Adherencia kine: diario a las 11:00 AM CLT
    scheduler.add_job(
        _job_adherencia_kine,
        CronTrigger(hour=11, minute=0, timezone=_CLT),
        id="adherencia_kine",
        replace_existing=True,
    )
    # Control por especialidad: diario a las 11:30 AM CLT
    scheduler.add_job(
        _job_control_especialidad,
        CronTrigger(hour=11, minute=30, timezone=_CLT),
        id="control_especialidad",
        replace_existing=True,
    )
    # Cross-sell kine: miércoles a las 10:30 AM CLT
    scheduler.add_job(
        _job_crosssell_kine,
        CronTrigger(day_of_week="wed", hour=10, minute=30, timezone=_CLT),
        id="crosssell_kine",
        replace_existing=True,
    )
    # Cumpleaños: diario a las 10:00 CLT
    scheduler.add_job(
        _job_cumpleanos,
        CronTrigger(hour=10, minute=0, timezone=_CLT),
        id="cumpleanos_diario",
        replace_existing=True,
    )
    # Win-back >90 días: primer lunes de cada mes a las 10:00 CLT
    scheduler.add_job(
        _job_winback,
        CronTrigger(day_of_week="mon", day="1-7", hour=10, minute=0, timezone=_CLT),
        id="winback_mensual",
        replace_existing=True,
    )
    # Sync caché de citas: diario a las 23:50 CLT
    scheduler.add_job(
        _sync_citas_hoy,
        CronTrigger(hour=23, minute=50, timezone=_CLT),
        id="sync_citas_cache",
        replace_existing=True,
    )
    # Retención desactivada: mensajes y eventos se mantienen indefinidamente.
    # El crecimiento es ~90 MB/año para el volumen del CMC, manejable en SQLite.
    # Para purgar manualmente: purge_old_data(msgs_days=N, events_days=N)
    # Watchdog Medilink: cada minuto chequea si se recuperó
    scheduler.add_job(
        _job_medilink_watchdog,
        "interval", minutes=1,
        id="medilink_watchdog",
        replace_existing=True,
    )
    # Lista de espera: diario a las 07:00 CLT
    scheduler.add_job(
        _job_waitlist_check,
        CronTrigger(hour=7, minute=0, timezone=_CLT),
        id="waitlist_check",
        replace_existing=True,
    )
    # Doctor alerts: resumen pre-cita cada 5 min (lun-sáb 07:30-21:30 CLT)
    scheduler.add_job(
        _job_doctor_resumen_precita,
        CronTrigger(minute="*/5", hour="7-21", day_of_week="mon-sat", timezone=_CLT),
        id="doctor_resumen_precita",
        replace_existing=True,
    )
    # Doctor alerts: reporte progreso 09:00, 12:00, 16:00, 20:00 CLT
    for h in (9, 12, 16, 20):
        scheduler.add_job(
            _job_doctor_reporte_progreso,
            CronTrigger(hour=h, minute=0, timezone=_CLT),
            id=f"doctor_reporte_{h}",
            replace_existing=True,
        )
    # Doctor alerts: reset diario a medianoche CLT
    scheduler.add_job(
        _job_doctor_reset_diario,
        CronTrigger(hour=0, minute=0, timezone=_CLT),
        id="doctor_reset_diario",
        replace_existing=True,
    )
    scheduler.start()
    log.info(
        "Scheduler iniciado — recordatorios 09:00 · recordatorios 2h cada 15min · cumpleaños 10:00 · "
        "post-consulta 10:00 · reactivación lun 10:30 · adherencia kine 11:00 · "
        "control 11:30 · cross-sell kine mié 10:30 · winback 1er lun mes 10:00 · sync caché 23:50 · "
        "watchdog medilink 1min · doctor alerts cada 5min + reportes 09/12/16/20"
    )
    yield
    scheduler.shutdown()


# ── FastAPI app ──────────────────────────────────────────────────────────────

app = FastAPI(title="CMC WhatsApp Bot", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent.parent / "static")), name="static")

# CORS restrictivo
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

# Registrar rutas admin + portal
app.include_router(admin_routes.router)
app.include_router(portal_routes.router)

# Cargar HTML del panel admin y portal paciente
_TEMPLATE_DIR = Path(__file__).parent.parent / "templates"
_ADMIN_HTML = (_TEMPLATE_DIR / "admin.html").read_text(encoding="utf-8")
_PORTAL_HTML = (_TEMPLATE_DIR / "portal.html").read_text(encoding="utf-8")
_ECOSISTEMA_HTML = (_TEMPLATE_DIR / "ecosistema.html").read_text(encoding="utf-8")
_DASHBOARD_HTML = (_TEMPLATE_DIR / "dashboard.html").read_text(encoding="utf-8")
_MEULEN_ECOSISTEMA_HTML = (_TEMPLATE_DIR / "meulen_ecosistema.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "meulen_ecosistema.html").exists() else ""
_MEULEN_DASHBOARD_HTML = (_TEMPLATE_DIR / "meulen_dashboard.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "meulen_dashboard.html").exists() else ""
_LANDING_HTML = (_TEMPLATE_DIR / "landing.html").read_text(encoding="utf-8")
_SITIO_V3_HTML = (_TEMPLATE_DIR / "sitio-v3.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "sitio-v3.html").exists() else ""
_HEATMAP_COMUNAS_HTML = (_TEMPLATE_DIR / "heatmap_comunas.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "heatmap_comunas.html").exists() else ""
_HEATMAP_DIRECCIONES_HTML = (_TEMPLATE_DIR / "heatmap_direcciones.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "heatmap_direcciones.html").exists() else ""


# ── Endpoints ────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    """Healthcheck básico + ping a Medilink con timeout corto."""
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
    from session import get_bsuid_stats
    bsuid = get_bsuid_stats()
    return {
        "status":      "ok",
        "medilink":    "ok" if medilink_ok else "degraded",
        "medilink_ms": medilink_ms,
        "medilink_state":   "down" if is_medilink_down() else "up",
        "intent_queue_depth": intent_queue_depth(),
        "waitlist_depth":     waitlist_depth(),
        "bsuid_mapped": bsuid["total"],
    }


@app.get("/landing", response_class=HTMLResponse)
def landing():
    """Landing page SEO del Centro Médico Carampangue."""
    return _LANDING_HTML


@app.get("/sitio", response_class=HTMLResponse)
def sitio_v3():
    """Prototipo v3 del sitio web — público para revisión."""
    return _SITIO_V3_HTML


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


@app.get("/admin", response_class=HTMLResponse)
def admin_panel(token: str | None = Query(None),
                cmc_session: str | None = Cookie(None)):
    """Panel admin. Acepta auth via query param ?token= O cookie de sesión.
    Si no hay auth válida, redirige a /admin/login."""
    from admin_routes import _verify_cookie
    # 1. Query param (backwards compat — also sets a cookie for subsequent loads)
    if token and token == ADMIN_TOKEN:
        return _ADMIN_HTML.replace("__TOKEN__", token)
    # 2. Cookie
    if cmc_session:
        role = _verify_cookie(cmc_session)
        if role in ("admin", "ortodoncia"):
            # Authed via cookie — inject empty TOKEN so JS uses cookie-only path
            return _ADMIN_HTML.replace("__TOKEN__", "")
    # 3. No auth → redirect to login
    return RedirectResponse(url="/admin/login", status_code=302)


@app.get("/admin/dashboard", response_class=HTMLResponse)
def admin_dashboard(token: str | None = Query(None),
                    cmc_session: str | None = Cookie(None)):
    """Dashboard de KPIs. Misma auth que /admin."""
    from admin_routes import _verify_cookie
    if token and token == ADMIN_TOKEN:
        return _DASHBOARD_HTML.replace("__TOKEN__", token)
    if cmc_session:
        role = _verify_cookie(cmc_session)
        if role in ("admin", "ortodoncia"):
            return _DASHBOARD_HTML.replace("__TOKEN__", "")
    return RedirectResponse(url="/admin/login", status_code=302)


@app.get("/admin/mapa-comunas", response_class=HTMLResponse)
def admin_mapa_comunas(token: str | None = Query(None),
                       cmc_session: str | None = Cookie(None)):
    """Mapa de calor por comunas/localidades. Misma auth que /admin."""
    from admin_routes import _verify_cookie
    if not _HEATMAP_COMUNAS_HTML:
        raise HTTPException(404, "Mapa no generado aún. Ejecutar: python scripts/heatmap_comunas.py map")
    if token and token == ADMIN_TOKEN:
        return _HEATMAP_COMUNAS_HTML
    if cmc_session:
        role = _verify_cookie(cmc_session)
        if role in ("admin", "ortodoncia"):
            return _HEATMAP_COMUNAS_HTML
    return RedirectResponse(url="/admin/login", status_code=302)


@app.get("/admin/mapa-direcciones", response_class=HTMLResponse)
def admin_mapa_direcciones(token: str | None = Query(None),
                           cmc_session: str | None = Cookie(None)):
    """Mapa de direcciones exactas geocodificadas. Misma auth que /admin."""
    from admin_routes import _verify_cookie
    if not _HEATMAP_DIRECCIONES_HTML:
        raise HTTPException(404, "Mapa no generado aún. Ejecutar: python scripts/geocode_direcciones.py")
    if token and token == ADMIN_TOKEN:
        return _HEATMAP_DIRECCIONES_HTML
    if cmc_session:
        role = _verify_cookie(cmc_session)
        if role in ("admin", "ortodoncia"):
            return _HEATMAP_DIRECCIONES_HTML
    return RedirectResponse(url="/admin/login", status_code=302)


@app.get("/portal", response_class=HTMLResponse)
def portal_page():
    """Portal del paciente — webapp pública (auth se maneja client-side con OTP)."""
    return _PORTAL_HTML


@app.get("/ecosistema", response_class=HTMLResponse)
def ecosistema_page():
    """Dashboard visual del ecosistema digital CMC."""
    return _ECOSISTEMA_HTML


@app.get("/meulen/ecosistemameulen", response_class=HTMLResponse)
def meulen_ecosistema_page():
    """Visualización del ecosistema digital de Supermercado Meulen."""
    return _MEULEN_ECOSISTEMA_HTML


@app.get("/meulen/dashboardplanificacion", response_class=HTMLResponse)
def meulen_dashboard_page():
    """Dashboard de planificación del MVP Meulen."""
    return _MEULEN_DASHBOARD_HTML


# ── Webhooks ─────────────────────────────────────────────────────────────────

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

    # ── Helper: convertir mensaje interactivo WA a texto plano ──────────────
    _SOCIAL_PROMO = (
        "\n\n✨ *Nutricionista bono Fonasa $4.680*\n"
        "😁 *Ortodoncia:* completa $120.000 / controles $30.000"
    )

    def _interactive_to_text(resp: dict) -> str:
        """Convierte un mensaje interactivo de WhatsApp a texto plano para IG/FB."""
        inter = resp.get("interactive", {})
        itype = inter.get("type", "")
        body = inter.get("body", {}).get("text", "")
        if itype == "button":
            btns = inter.get("action", {}).get("buttons", [])
            opts = "\n".join(f"  → {b['reply']['title']}" for b in btns)
            return f"{body}\n\n{opts}" if opts else body
        elif itype == "list":
            sections = inter.get("action", {}).get("sections", [])
            opts = []
            for sec in sections:
                for row in sec.get("rows", []):
                    desc = f" — {row['description']}" if row.get("description") else ""
                    opts.append(f"  • {row['title']}{desc}")
            items = "\n".join(opts)
            # Agregar promo al menú principal
            is_menu = "¿Qué necesitas hoy?" in body
            promo = _SOCIAL_PROMO if is_menu else ""
            return f"{body}{promo}\n\n{items}" if items else body + promo
        return body

    # ── Helper: procesar mensaje de IG/FB con el chatbot ─────────────────────
    async def _process_social(phone: str, sender_id: str, texto: str,
                              canal: str, send_fn):
        """Procesa un mensaje de IG/FB usando handle_message y responde."""
        session = get_session(phone)
        state_before = session.get("state", "IDLE")
        log_message(phone, "in", texto, state_before, canal=canal)
        try:
            respuesta = await handle_message(phone, texto, session)
        except Exception as e:
            log.error("Error procesando %s msg from=%s: %s", canal, phone, e, exc_info=True)
            reset_session(phone)
            respuesta = (
                "Tuve un problema técnico 😕\n\n"
                "Por favor intenta de nuevo o llama a recepción:\n"
                f"📞 {CMC_TELEFONO}"
            )
        state_after = get_session(phone).get("state", "IDLE")
        if isinstance(respuesta, dict) and respuesta.get("type") == "interactive":
            resp_text = _interactive_to_text(respuesta)
        else:
            resp_text = str(respuesta) if respuesta else ""
        if resp_text:
            await send_fn(sender_id, resp_text)
            log_message(phone, "out", resp_text, state_after, canal=canal)
            log.info("BOT %s to=%s state=%s reply=%r", canal.upper(), phone, state_after, resp_text[:80])

    # ── Helper: obtener nombre de usuario IG/FB ─────────────────────────────
    async def _fetch_social_name(sender_id: str, phone: str, platform: str):
        """Obtiene nombre/username de IG o FB via Graph API y lo guarda en contact_profiles."""
        existing = get_profile(phone)
        if existing:
            n = existing.get("nombre", "")
            if not (n.startswith("ig_") or n.startswith("fb_")):
                return  # ya tenemos un nombre real
        from config import META_ACCESS_TOKEN, META_PAGE_ACCESS_TOKEN
        # Para Messenger: intentar con system user token y page token
        tokens = [META_ACCESS_TOKEN]
        if META_PAGE_ACCESS_TOKEN and META_PAGE_ACCESS_TOKEN != META_ACCESS_TOKEN:
            tokens.append(META_PAGE_ACCESS_TOKEN)
        try:
            import httpx
            fields = "name,username" if platform == "instagram" else "name,first_name,last_name"
            async with httpx.AsyncClient(timeout=5) as client:
                for token in tokens:
                    if not token:
                        continue
                    r = await client.get(
                        f"https://graph.facebook.com/v22.0/{sender_id}",
                        params={"fields": fields, "access_token": token},
                    )
                    if r.status_code == 200:
                        info = r.json()
                        if info.get("error"):
                            continue
                        if platform == "instagram":
                            nombre = info.get("username") or info.get("name", "")
                        else:
                            nombre = info.get("name") or f"{info.get('first_name', '')} {info.get('last_name', '')}".strip()
                        if nombre and nombre != sender_id:
                            save_profile(phone, "", nombre)
                            log.info("%s perfil guardado: %s → %s", platform.upper(), phone, nombre)
                            return
                    else:
                        log.debug("_fetch_social_name %s token attempt %s: %s",
                                  platform, r.status_code, r.text[:120])
        except Exception as e:
            log.debug("No se pudo obtener perfil %s %s: %s", platform, sender_id, e)

    # ── Instagram DMs ────────────────────────────────────────────────────────
    if obj == "instagram":
        try:
            for entry in data.get("entry", []):
                for ev in entry.get("messaging", []):
                    sender_id = ev.get("sender", {}).get("id", "")
                    sender_name = ev.get("sender", {}).get("username", "") or ev.get("sender", {}).get("name", "")
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
                    log.info("INSTAGRAM from=%s name=%r text=%r sender=%s",
                             phone, sender_name, texto[:80], ev.get("sender", {}))
                    # Guardar perfil si viene en el webhook
                    if sender_name and not get_profile(phone):
                        save_profile(phone, "", sender_name)
                    elif not sender_name:
                        await _fetch_social_name(sender_id, phone, "instagram")
                    # Procesar con el chatbot completo
                    from messaging import send_instagram
                    await _process_social(phone, sender_id, texto, "instagram", send_instagram)
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
                    log.info("MESSENGER from=%s sender=%s text=%r",
                             phone, ev.get("sender", {}), texto[:80])
                    # Guardar nombre si viene en el webhook
                    sender_obj = ev.get("sender", {})
                    sender_name = sender_obj.get("name", "") or sender_obj.get("first_name", "")
                    if sender_name and not get_profile(phone):
                        save_profile(phone, "", sender_name)
                    elif not sender_name:
                        await _fetch_social_name(sender_id, phone, "facebook")
                    from messaging import send_messenger
                    await _process_social(phone, sender_id, texto, "messenger", send_messenger)
        except Exception as e:
            log.warning("Error procesando Messenger webhook: %s", e)
        return Response(status_code=200)

    # ── WhatsApp ─────────────────────────────────────────────────────────────
    try:
        entry = data["entry"][0]
        change = entry["changes"][0]["value"]

        # ── Message delivery statuses (sent/delivered/read/failed) ────────
        if "statuses" in change:
            for st in change["statuses"]:
                wamid = st.get("id", "")
                recipient = st.get("recipient_id", "").lstrip("+")
                status = st.get("status", "")  # sent, delivered, read, failed
                err = st.get("errors", [{}])[0] if st.get("errors") else {}
                if wamid and recipient and status:
                    upsert_message_status(
                        wamid, recipient, status,
                        error_code=str(err.get("code", "")) if err else None,
                        error_title=err.get("title", "") if err else None,
                    )
                    if status == "failed":
                        log.warning("MSG FAILED wamid=%s to=%s code=%s: %s",
                                    wamid, recipient, err.get("code"), err.get("title"))

        if "messages" not in change:
            return Response(status_code=200)

        msg = change["messages"][0]
        msg_type = msg.get("type")

        phone = msg["from"].lstrip("+")  # normalizar: siempre sin +

        # Capture BSUID for future phone-number-hidden support (June 2026)
        contacts = change.get("contacts", [])
        if contacts:
            contact = contacts[0]
            bsuid = contact.get("user_id", "")
            wa_id = contact.get("wa_id", "")
            if bsuid:
                upsert_bsuid(bsuid, phone or wa_id or None)

        msg_id = msg.get("id", "")
        is_audio = False

        # De-dup temprano
        if msg_id and is_duplicate(msg_id):
            log.info("MSG duplicado ignorado id=%s from=%s", msg_id, phone)
            return Response(status_code=200)

        if _rate_limited(phone):
            log.warning("Rate limit excedido WA phone=%s type=%s", phone, msg_type)
            return Response(status_code=200)

        # Extraer texto de mensajes de texto, respuestas interactivas o audio
        if msg_type == "text":
            texto = msg["text"]["body"].strip()
            if not texto:
                return Response(status_code=200)
        elif msg_type == "interactive":
            interactive = msg.get("interactive", {})
            itype = interactive.get("type", "")
            if itype == "button_reply":
                texto = interactive["button_reply"]["id"]
            elif itype == "list_reply":
                texto = interactive["list_reply"]["id"]
            else:
                return Response(status_code=200)
        elif msg_type == "audio":
            media_id = msg.get("audio", {}).get("id", "")
            log.info("AUDIO recibido from=%s media_id=%s — transcribiendo...", phone, media_id)
            media = await download_whatsapp_media(media_id)
            if not media:
                await send_whatsapp(
                    phone,
                    "No pude descargar tu audio 😕\nIntenta escribir el mensaje o grabar de nuevo."
                )
                return Response(status_code=200)
            audio_bytes, mime = media
            transcripcion = await transcribe_audio(audio_bytes, mime)
            if not transcripcion:
                await send_whatsapp(
                    phone,
                    "No logré entender el audio 😕\n¿Puedes escribirlo o grabarlo de nuevo un poco más claro?"
                )
                return Response(status_code=200)
            texto = transcripcion
            is_audio = True
            log.info("AUDIO transcrito from=%s text=%r", phone, texto[:120])
        elif msg_type == "reaction":
            # Reacciones (emoji a un mensaje) — ignorar silenciosamente
            return Response(status_code=200)
        elif msg_type in ("image", "video", "document"):
            # Archivos: descargar, almacenar. PDF/Word → extraer texto como audio.
            log.info("MEDIA recibido from=%s type=%s", phone, msg_type)
            _MEDIA_LABELS = {"image": "imagen 📷", "video": "video 🎥", "document": "documento 📄"}
            label = _MEDIA_LABELS[msg_type]
            caption = ""
            media_id = ""
            orig_filename = ""
            if msg_type == "image":
                caption = msg.get("image", {}).get("caption", "")
                media_id = msg.get("image", {}).get("id", "")
            elif msg_type == "video":
                caption = msg.get("video", {}).get("caption", "")
                media_id = msg.get("video", {}).get("id", "")
            elif msg_type == "document":
                orig_filename = msg.get("document", {}).get("filename", "")
                caption = orig_filename
                media_id = msg.get("document", {}).get("id", "")
            # Descargar y guardar archivo
            saved_filename = ""
            blob = None
            mime = ""
            if media_id:
                try:
                    result = await download_whatsapp_media(media_id)
                    if result:
                        blob, mime = result
                        from session import save_patient_file
                        _UPLOAD_DIR = Path(__file__).parent.parent / "data" / "uploads" / phone
                        _UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
                        _MIME_EXT = {
                            "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
                            "video/mp4": ".mp4", "video/3gpp": ".3gp",
                            "application/pdf": ".pdf",
                            "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
                            "application/msword": ".doc",
                            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
                        }
                        ext = _MIME_EXT.get(mime, ".bin")
                        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                        saved_filename = orig_filename or f"{msg_type}_{ts}{ext}"
                        file_path = _UPLOAD_DIR / saved_filename
                        if file_path.exists():
                            saved_filename = f"{ts}_{saved_filename}"
                            file_path = _UPLOAD_DIR / saved_filename
                        file_path.write_bytes(blob)
                        rel_path = f"data/uploads/{phone}/{saved_filename}"
                        save_patient_file(phone, saved_filename, msg_type, mime,
                                          rel_path, len(blob), caption[:200])
                        log.info("MEDIA guardado from=%s path=%s size=%d", phone, rel_path, len(blob))
                except Exception as e:
                    log.error("Error descargando/guardando media from=%s: %s", phone, e)

            # PDF/Word → extraer texto y procesar como mensaje (igual que audio)
            if blob and mime in ("application/pdf",
                                  "application/vnd.openxmlformats-officedocument.wordprocessingml.document"):
                extracted = ""
                if "pdf" in mime:
                    extracted = extract_text_from_pdf(blob)
                else:
                    extracted = extract_text_from_docx(blob)
                if extracted:
                    # Truncar a 2000 chars para no exceder límites
                    if len(extracted) > 2000:
                        extracted = extracted[:2000] + "…"
                    texto = extracted
                    log.info("📄 Texto extraído from=%s (%d chars): %s", phone, len(extracted), extracted[:120])
                    state_before = get_session(phone).get("state", "IDLE")
                    log_text = f"[{msg_type}:{saved_filename}]"
                    log_message(phone, "in", log_text, state_before, canal="whatsapp")
                    # Feedback al paciente (como con audio)
                    preview = extracted[:300] + ("…" if len(extracted) > 300 else "")
                    confirm_msg = f"📄 *Tu documento dice:*\n_{preview}_"
                    await send_whatsapp(phone, confirm_msg)
                    log_message(phone, "out", confirm_msg, state_before, canal="whatsapp")
                    # Procesar el texto extraído por el pipeline normal
                    session = get_session(phone)
                    respuesta = await handle_message(phone, texto, session)
                    if respuesta:
                        if isinstance(respuesta, dict):
                            await send_whatsapp_interactive(phone, respuesta)
                            body = respuesta.get("interactive", {}).get("body", {}).get("text", "")
                            log_message(phone, "out", body, get_session(phone).get("state", "IDLE"), canal="whatsapp")
                        else:
                            await send_whatsapp(phone, respuesta)
                            log_message(phone, "out", respuesta, get_session(phone).get("state", "IDLE"), canal="whatsapp")
                    return Response(status_code=200)

            # Imágenes y otros → guardar + derivar a recepción (sin extracción)
            log_text = f"[{msg_type}]" + (f" {caption}" if caption else "")
            if saved_filename:
                log_text = f"[{msg_type}:{saved_filename}]" + (f" {caption}" if caption and caption != saved_filename else "")
            state_before = get_session(phone).get("state", "IDLE")
            log_message(phone, "in", log_text, state_before, canal="whatsapp")
            save_session(phone, "HUMAN_TAKEOVER", {
                "hold_sent": True,
                "handoff_reason": f"media:{msg_type}",
                "media_caption": caption,
            })
            log_event(phone, "media_recibido", {"tipo": msg_type, "caption": caption[:200],
                                                 "filename": saved_filename})
            reply = (
                f"Recibí tu {label}, gracias.\n\n"
                "Lo guardé en tu ficha y una recepcionista lo va a revisar 🙏\n"
                "Si es urgente, puedes llamar al 📞 (41) 296 5226"
            )
            await send_whatsapp(phone, reply)
            log_message(phone, "out", reply, "HUMAN_TAKEOVER", canal="whatsapp")
            return Response(status_code=200)
        elif msg_type in ("sticker", "location", "contacts"):
            # Tipos livianos: responder amable sin derivar a recepción
            log.info("MSG no soportado from=%s type=%s", phone, msg_type)
            _LIGHT_REPLIES = {
                "sticker": (
                    "😄 ¡Gracias por el sticker!\n"
                    "¿En qué puedo ayudarte? Escribe *menu* para ver las opciones."
                ),
                "contacts": (
                    "Recibí el contacto 👤 pero no puedo procesarlo.\n"
                    "¿En qué puedo ayudarte? Escribe *menu* para ver las opciones."
                ),
            }
            if msg_type == "location":
                # Enviar ubicación del CMC como mapa nativo + link de ruta
                log.info("LOCATION recibido from=%s", phone)
                loc = msg.get("location", {})
                lat = loc.get("latitude")
                lng = loc.get("longitude")
                CMC_LAT, CMC_LNG = -37.2548769, -73.2355041
                log_message(phone, "in", "[ubicación]", get_session(phone).get("state", "IDLE"), canal="whatsapp")
                # 1) Enviar pin del CMC como mensaje de ubicación nativo
                await send_whatsapp_location(
                    phone, CMC_LAT, CMC_LNG,
                    name="Centro Médico Carampangue",
                    address="Monsalve 102 esq. República, Carampangue",
                )
                # 2) Enviar link de ruta como texto
                if lat and lng:
                    maps_url = f"https://www.google.com/maps/dir/{lat},{lng}/{CMC_LAT},{CMC_LNG}"
                    reply = (
                        f"🗺️ *Cómo llegar desde tu ubicación:*\n{maps_url}\n\n"
                        "¿Necesitas agendar una hora? Escribe *menu*"
                    )
                else:
                    maps_url = f"https://www.google.com/maps/dir//{CMC_LAT},{CMC_LNG}"
                    reply = (
                        f"🗺️ *Ver en Google Maps:*\n{maps_url}\n\n"
                        "¿Necesitas agendar una hora? Escribe *menu*"
                    )
                await send_whatsapp(phone, reply)
                log_message(phone, "out", f"[ubicación CMC] + {reply}", get_session(phone).get("state", "IDLE"), canal="whatsapp")
                return Response(status_code=200)
            reply = _LIGHT_REPLIES[msg_type]
            log_message(phone, "in", f"[{msg_type}]", get_session(phone).get("state", "IDLE"), canal="whatsapp")
            await send_whatsapp(phone, reply)
            log_message(phone, "out", reply, get_session(phone).get("state", "IDLE"), canal="whatsapp")
            return Response(status_code=200)
        else:
            log.info("MSG tipo desconocido from=%s type=%s — ignorado", phone, msg_type)
            return Response(status_code=200)

        log.info("MSG from=%s id=%s type=%s text=%r", phone, msg_id, msg_type, texto[:100])

        session = get_session(phone)
        state_before = session.get("state", "IDLE")
        log_text = f"🎤 {texto}" if is_audio else texto
        log_message(phone, "in", log_text, state_before, canal="whatsapp")

        # Indicador de "pensando" — reacción ⏳ al mensaje del paciente
        await react_whatsapp(phone, msg_id)

        # Confirmar al paciente lo que se entendió del audio
        if is_audio:
            await send_whatsapp(phone, f"🎤 Entendí: _{texto}_")

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

        # Quitar indicador de "pensando"
        await unreact_whatsapp(phone, msg_id)

        state_after = get_session(phone).get("state", "IDLE")

        if isinstance(respuesta, dict) and respuesta.get("type") == "interactive":
            resp_text = respuesta["interactive"].get("body", {}).get("text", "[mensaje interactivo]")
        else:
            resp_text = str(respuesta) if respuesta else ""

        if resp_text:
            log_message(phone, "out", resp_text, state_after, canal="whatsapp")
        log.info("BOT to=%s state=%s reply=%r", phone, state_after, resp_text[:80])

        if not respuesta:
            pass  # silencio intencional (HUMAN_TAKEOVER)
        elif isinstance(respuesta, dict) and respuesta.get("type") == "interactive":
            await send_whatsapp_interactive(phone, respuesta["interactive"])
        else:
            await send_whatsapp(phone, respuesta)

        # Enviar pin del mapa solo en respuestas de ubicación o confirmación de cita
        # (NO en el saludo que también menciona la dirección)
        _location_ctx = resp_text and "Monsalve 102" in resp_text and (
            "ubicado" in resp_text.lower()
            or "recuerda llegar" in resp_text.lower()
            or "tiempos de llegada" in resp_text.lower()
        )
        if _location_ctx:
            await send_whatsapp_location(
                phone, -37.2548769, -73.2355041,
                name="Centro Médico Carampangue",
                address="Monsalve 102 esq. República, Carampangue",
            )

        # C4 fix: process remaining messages in batch (Meta can send 2+ per payload)
        for _xm in change["messages"][1:]:
            try:
                _xphone = _xm["from"].lstrip("+")
                _xid = _xm.get("id", "")
                if _xid and is_duplicate(_xid):
                    continue
                if _rate_limited(_xphone):
                    continue
                _xtype = _xm.get("type", "")
                _xtxt = ""
                if _xtype == "text":
                    _xtxt = _xm.get("text", {}).get("body", "").strip()
                elif _xtype == "interactive":
                    _xi = _xm.get("interactive", {})
                    _xit = _xi.get("type", "")
                    if _xit == "button_reply":
                        _xtxt = _xi["button_reply"]["id"]
                    elif _xit == "list_reply":
                        _xtxt = _xi["list_reply"]["id"]
                if not _xtxt:
                    log.info("MSG extra en batch ignorado from=%s type=%s", _xphone, _xtype)
                    continue
                log.info("MSG extra en batch from=%s type=%s text=%r", _xphone, _xtype, _xtxt[:80])
                _xs = get_session(_xphone)
                _xstate = _xs.get("state", "IDLE")
                log_message(_xphone, "in", _xtxt, _xstate, canal="whatsapp")
                _xresp = await handle_message(_xphone, _xtxt, _xs)
                _xstate_after = get_session(_xphone).get("state", "IDLE")
                if _xresp:
                    if isinstance(_xresp, dict) and _xresp.get("type") == "interactive":
                        await send_whatsapp_interactive(_xphone, _xresp["interactive"])
                        _xrt = _xresp["interactive"].get("body", {}).get("text", "")
                    else:
                        await send_whatsapp(_xphone, str(_xresp))
                        _xrt = str(_xresp)
                    log_message(_xphone, "out", _xrt, _xstate_after, canal="whatsapp")
            except Exception as _xe:
                log.warning("Error procesando msg extra en batch WA: %s", _xe)

    except (KeyError, IndexError) as e:
        log.warning("Payload inesperado: %s | data=%s", e, str(data)[:200])

    return Response(status_code=200)
