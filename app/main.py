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
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, JSONResponse
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
                  _job_crosssell_kine, _job_crosssell_orl_fono,
                  _job_crosssell_odonto_estetica, _job_crosssell_mg_chequeo,
                  _job_medilink_watchdog, _job_admin_status_report,
                  _job_cleanup_stuck_sessions,
                  _job_waitlist_check,
                  _job_doctor_resumen_precita, _job_doctor_reporte_progreso,
                  _job_doctor_reset_diario,
                  _job_cumpleanos, _job_winback,
                  _job_takeover_ttl, _job_takeover_media_ttl)
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


def _rate_limited(*keys: str) -> bool:
    """True si CUALQUIER clave superó _RATE_MAX_MSGS mensajes en la última ventana.

    Acepta múltiples claves (e.g. phone y rut:XXXXX) para evitar que un atacante
    bypassee el límite rotando números con un mismo RUT. Solo se incrementan los
    buckets si ninguno excedió, para no "castigar" claves secundarias cuando otra
    ya bloqueó.
    """
    now = monotonic()
    keys = tuple(k for k in keys if k)
    if not keys:
        return False
    # Primera pasada: comprobar si alguna clave excede
    for key in keys:
        bucket = _rate_buckets.get(key)
        if bucket is None:
            bucket = deque()
            _rate_buckets[key] = bucket
        while bucket and now - bucket[0] > _RATE_WINDOW_SEC:
            bucket.popleft()
        if len(bucket) >= _RATE_MAX_MSGS:
            return True
    # Ninguna excedió: registrar timestamp en todas
    for key in keys:
        _rate_buckets[key].append(now)
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
    # Adherencia kine: L/M/V a las 11:00 AM CLT (antes diario — bajamos 7→3/sem para reducir costo templates)
    scheduler.add_job(
        _job_adherencia_kine,
        CronTrigger(day_of_week="mon,wed,fri", hour=11, minute=0, timezone=_CLT),
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
    # Cross-sell ORL↔Fono: jueves 11:00 CLT
    scheduler.add_job(
        _job_crosssell_orl_fono,
        CronTrigger(day_of_week="thu", hour=11, minute=0, timezone=_CLT),
        id="crosssell_orl_fono",
        replace_existing=True,
    )
    # Cross-sell odontología → estética: 1º y 15 del mes 10:30 CLT
    scheduler.add_job(
        _job_crosssell_odonto_estetica,
        CronTrigger(day="1,15", hour=10, minute=30, timezone=_CLT),
        id="crosssell_odonto_estetica",
        replace_existing=True,
    )
    # Cross-sell MG→chequeo preventivo: primer martes del mes 09:30 CLT
    scheduler.add_job(
        _job_crosssell_mg_chequeo,
        CronTrigger(day_of_week="tue", day="1-7", hour=9, minute=30, timezone=_CLT),
        id="crosssell_mg_chequeo",
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
    # TTL HUMAN_TAKEOVER: reanudar bot si recepción no devolvió el control en 24h.
    # Cron cada hora a los :15. Evita 107+ sesiones bloqueadas (auditoría 2026-04-28).
    scheduler.add_job(
        _job_takeover_ttl,
        CronTrigger(minute=15, timezone=_CLT),
        id="takeover_ttl",
        replace_existing=True,
    )
    # TTL más corto (6h) para HUMAN_TAKEOVER iniciados por imagen/PDF: solo
    # requieren ack/archivado. Cron cada hora a los :45.
    scheduler.add_job(
        _job_takeover_media_ttl,
        CronTrigger(minute=45, timezone=_CLT),
        id="takeover_media_ttl",
        replace_existing=True,
    )
    # Reporte periódico de estado al admin cada 30 min
    scheduler.add_job(
        _job_admin_status_report,
        CronTrigger(minute="0,30", timezone=_CLT),
        id="admin_status_report",
        replace_existing=True,
    )
    # Limpieza de sesiones stuck en WAIT_* cada hora
    scheduler.add_job(
        _job_cleanup_stuck_sessions,
        CronTrigger(hour="7-22", minute="15", timezone=_CLT),
        id="cleanup_stuck_sessions",
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

import vuelos_routes
app.include_router(vuelos_routes.router)

# Cargar HTML del panel admin y portal paciente
_TEMPLATE_DIR = Path(__file__).parent.parent / "templates"
_ADMIN_HTML = (_TEMPLATE_DIR / "admin.html").read_text(encoding="utf-8")
_ADMIN_V2_HTML = (_TEMPLATE_DIR / "admin_v2.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "admin_v2.html").exists() else ""
_PORTAL_HTML = (_TEMPLATE_DIR / "portal.html").read_text(encoding="utf-8")
_PORTAL_V2_HTML = (_TEMPLATE_DIR / "portal_v2.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "portal_v2.html").exists() else ""
_PORTAL_INFORME_HTML = (_TEMPLATE_DIR / "portal_informe.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "portal_informe.html").exists() else ""
_ECOSISTEMA_HTML = (_TEMPLATE_DIR / "ecosistema.html").read_text(encoding="utf-8")
_DASHBOARD_HTML = (_TEMPLATE_DIR / "dashboard.html").read_text(encoding="utf-8")
_MEULEN_ECOSISTEMA_HTML = (_TEMPLATE_DIR / "meulen_ecosistema.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "meulen_ecosistema.html").exists() else ""
_MEULEN_DASHBOARD_HTML = (_TEMPLATE_DIR / "meulen_dashboard.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "meulen_dashboard.html").exists() else ""
_MEULEN_KPIS_HTML = (_TEMPLATE_DIR / "meulen_kpis.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "meulen_kpis.html").exists() else ""
_MENU_HTML = (_TEMPLATE_DIR / "menu.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "menu.html").exists() else ""
_PROYECTOS2026_HTML = (_TEMPLATE_DIR / "proyectos2026.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "proyectos2026.html").exists() else ""
_LANDING_HTML = (_TEMPLATE_DIR / "landing.html").read_text(encoding="utf-8")
_SITIO_V3_HTML = (_TEMPLATE_DIR / "sitio-v3.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "sitio-v3.html").exists() else ""
_SITIO_V2_HTML = (_TEMPLATE_DIR / "sitio-v2.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "sitio-v2.html").exists() else ""
_SITIO_FLAGSHIP_HTML = (_TEMPLATE_DIR / "sitio-flagship.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "sitio-flagship.html").exists() else ""
_SITIO_V4_HTML = (_TEMPLATE_DIR / "sitio-v4.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "sitio-v4.html").exists() else ""
_SITIO_V5_HTML = (_TEMPLATE_DIR / "sitio-v5.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "sitio-v5.html").exists() else ""
_SITIO_V6_HTML = (_TEMPLATE_DIR / "sitio-v6.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "sitio-v6.html").exists() else ""
_SITIO_V7_HTML = (_TEMPLATE_DIR / "sitio-v7.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "sitio-v7.html").exists() else ""
_SITIO_V7_1_HTML = (_TEMPLATE_DIR / "sitio-v7-1.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "sitio-v7-1.html").exists() else ""
_HEATMAP_COMUNAS_HTML = (_TEMPLATE_DIR / "heatmap_comunas.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "heatmap_comunas.html").exists() else ""
_HEATMAP_DIRECCIONES_HTML = (_TEMPLATE_DIR / "heatmap_direcciones.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "heatmap_direcciones.html").exists() else ""
_SEO_DASHBOARD_HTML = (_TEMPLATE_DIR / "seo_dashboard.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "seo_dashboard.html").exists() else ""
_CRECIMIENTO_PERSONAL_HTML = (_TEMPLATE_DIR / "crecimiento_personal.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "crecimiento_personal.html").exists() else ""
_META_DASHBOARD_HTML = (_TEMPLATE_DIR / "meta_dashboard.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "meta_dashboard.html").exists() else ""
_HORIZONTE_DASHBOARD_HTML = (_TEMPLATE_DIR / "horizonte_dashboard.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "horizonte_dashboard.html").exists() else ""
_PRIVACIDAD_HTML = (_TEMPLATE_DIR / "privacidad.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "privacidad.html").exists() else ""
_PROFESIONALES_CMC_HTML = (_TEMPLATE_DIR / "profesionales_cmc.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "profesionales_cmc.html").exists() else ""


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


@app.get("/sitio/v2", response_class=HTMLResponse)
def sitio_v2():
    """Sitio web v2 — diseño handoff Claude Design (azul deep + turquesa)."""
    return _SITIO_V2_HTML


@app.get("/sitio/v3", response_class=HTMLResponse)
def sitio_v3_flagship():
    """Sitio web v3 — flagship: HTML estático server-rendered, schema enriquecido,
    booking widget integrado, equipo con SVG ilustrado, FAQ ampliada, lead magnet."""
    return _SITIO_FLAGSHIP_HTML


@app.get("/sitio/v4", response_class=HTMLResponse)
async def sitio_v4():
    """Sitio web v4 — híbrido OLACORE-aligned con rating real de Google Places.
    El HTML base usa placeholders <!--CMC_*--> que se reemplazan en cada request
    con los datos del caché de google_rating (TTL 6h, ~4 calls/día)."""
    from google_rating import fetch_rating
    rating_data = await fetch_rating()
    return _render_sitio_v4(rating_data)


@app.get("/sitio/v5", response_class=HTMLResponse)
async def sitio_v5():
    """Sitio web v5 — toma v4 y restaura lo mejor de v3 flagship: trust strip
    con aseguradoras, floating chip de disponibilidad, stats animados, lead magnet."""
    from google_rating import fetch_rating
    rating_data = await fetch_rating()
    return _render_sitio_dynamic(_SITIO_V5_HTML, rating_data)


@app.get("/sitio/v6", response_class=HTMLResponse)
async def sitio_v6():
    """Sitio web v6 — base v3 flagship + lo mejor de v4: rating dinámico Google
    Places, insurance bar (formas de pago) y sección horarios por especialidad."""
    from google_rating import fetch_rating
    rating_data = await fetch_rating()
    return _render_sitio_dynamic(_SITIO_V6_HTML, rating_data)


@app.get("/sitio/v7", response_class=HTMLResponse)
async def sitio_v7():
    """Sitio web v7 — versión consolidada inicial (preview/staging, noindex).
    Base v6 con SEO técnico endurecido, Schema Physician (EEAT). Reemplazada
    por v7-1 que incluye correcciones de auditoría senior (H1 SEO, cards
    transaccionales, copy regulatorio, claims honestos)."""
    from google_rating import fetch_rating
    rating_data = await fetch_rating()
    return _render_sitio_dynamic(_SITIO_V7_HTML, rating_data)


@app.get("/sitio/v7-1", response_class=HTMLResponse)
async def sitio_v7_1():
    """Sitio web v7.1 — versión FINAL en producción. Sobre v7 aplica auditoría
    senior: H1 con keyword local "Centro médico en Carampangue", cards
    transaccionales con price-row honesta y CTA "Agendar", copy regulatorio
    correcto ("Profesionales habilitados" en vez de "Acreditados"),
    claim de disponibilidad sin número fabricado, reseñas dinámicas Google
    Places con fallback honesto al perfil de Google Maps."""
    from google_rating import fetch_rating
    rating_data = await fetch_rating()
    return _render_sitio_dynamic(_SITIO_V7_1_HTML, rating_data)


@app.get("/api/google-rating")
async def api_google_rating():
    """Rating + reseñas de Google Places para el CMC (cache 6h)."""
    from google_rating import fetch_rating
    return await fetch_rating()


def _render_sitio_v4(rating_data: dict) -> str:
    return _render_sitio_dynamic(_SITIO_V4_HTML, rating_data)


def _render_sitio_dynamic(html: str, rating_data: dict) -> str:
    """Reemplaza placeholders del template con rating real de Google.
    Si no hay API key o falla, deja la pill genérica y omite aggregateRating
    (cumple Google guidelines: no fabricar reviews). Usado por v4 y v5."""
    import html as _html
    rating  = rating_data.get("rating")
    count   = rating_data.get("review_count")
    reviews = rating_data.get("reviews") or []

    if rating and count:
        rt = f"{rating:.1f}".replace(".", ",")
        pill = (
            '<span class="stars" style="color:var(--c-warm);font-size:.82rem;letter-spacing:1px">★★★★★</span>'
            f'<span class="rn">{rt}</span>'
            f'<span class="rt">· {count} reseñas en Google</span>'
        )
    else:
        pill = (
            '<i class="fas fa-shield-halved" style="color:var(--c-blue)"></i>'
            '<span class="rn">Acreditados</span>'
            '<span class="rt">· Superintendencia de Salud</span>'
        )
    html = html.replace("<!--CMC_RATING_PILL-->", pill)

    if rating and count:
        agg = (
            ',\n        "aggregateRating": {\n'
            '          "@type": "AggregateRating",\n'
            f'          "ratingValue": "{rating:.1f}",\n'
            f'          "reviewCount": "{count}",\n'
            '          "bestRating": "5",\n'
            '          "worstRating": "1"\n'
            '        }'
        )
    else:
        agg = ""
    html = html.replace("<!--CMC_AGGREGATE_RATING-->", agg)

    # Placeholders v6/v7 — rating-card del bloque testimonios (formato grande)
    # v6 fallback: "4.8" + "247 reseñas en Google" (estático, viola guidelines si la API falla)
    # v7 fallback: "Reseñas reales" + "Verificadas en Google" (honesto sin número fabricado)
    if rating and count:
        rt = f"{rating:.1f}".replace(".", ",")
        html = html.replace("<!--CMC_RATING_BIG-->4.8", f"<!--CMC_RATING_BIG-->{rt}")
        html = html.replace("<!--CMC_RATING_BIG-->Reseñas reales", f"<!--CMC_RATING_BIG-->{rt}")
        html = html.replace("<!--CMC_RATING_DESC-->247 reseñas en Google", f"<!--CMC_RATING_DESC-->{count} reseñas en Google")
        html = html.replace("<!--CMC_RATING_DESC-->Verificadas en Google", f"<!--CMC_RATING_DESC-->{count} reseñas en Google")

    if reviews:
        from google_rating import initials
        # Formato v4/v5: clases .testi / .testi-text / .testi-author
        cards_v45 = []
        # Formato v7: clases .test-card / .test-quote / .test-author / .verif (SVG inline, sin fontawesome)
        cards_v7 = []
        star_svg = '<svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/></svg>'
        for rv in reviews[:3]:
            txt = (rv.get("text") or "").strip()
            if len(txt) < 25:
                continue
            txt_short = txt[:240] + ("…" if len(txt) > 240 else "")
            author = rv.get("author") or "Anónimo"
            n_stars = int(rv.get("rating") or 5)
            when    = rv.get("relative_time") or ""
            cards_v45.append(
                '<article class="testi reveal">\n'
                f'  <div class="testi-stars">{"★" * n_stars}</div>\n'
                f'  <p class="testi-text">"{_html.escape(txt_short)}"</p>\n'
                '  <div class="testi-author">\n'
                f'    <div class="testi-avatar">{_html.escape(initials(author))}</div>\n'
                '    <div>\n'
                f'      <div class="testi-name">{_html.escape(author)}</div>\n'
                f'      <div class="testi-role">Reseña Google · {_html.escape(when)}</div>\n'
                '    </div>\n'
                '    <div class="testi-verified">Verificado</div>\n'
                '  </div>\n'
                '</article>'
            )
            cards_v7.append(
                '<div class="test-card">\n'
                f'  <div class="test-stars">{star_svg * n_stars}</div>\n'
                f'  <p class="test-quote">"{_html.escape(txt_short)}"</p>\n'
                '  <div class="test-author">\n'
                f'    <div class="avatar">{_html.escape(initials(author))}</div>\n'
                '    <div>\n'
                f'      <div class="name">{_html.escape(author)}</div>\n'
                f'      <div class="loc">Reseña Google · {_html.escape(when)}</div>\n'
                '    </div>\n'
                '    <div class="verif"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg> Verificado</div>\n'
                '  </div>\n'
                '</div>'
            )
        if cards_v45:
            html = html.replace("<!--CMC_TESTIMONIOS_REALES-->", "\n".join(cards_v45))
        if cards_v7:
            # En v7, los placeholders START/END delimitan el bloque a reemplazar
            # cuando hay reviews reales (cae el fallback "Leer reseñas en Google").
            import re as _re
            html = _re.sub(
                r'<!--CMC_TESTIMONIOS_V7_START-->.*?<!--CMC_TESTIMONIOS_V7_END-->',
                '\n      ' + '\n      '.join(cards_v7),
                html,
                count=1,
                flags=_re.DOTALL
            )

    return html


@app.get("/privacidad", response_class=HTMLResponse)
def privacidad():
    """Política de Privacidad v1.0 — Ley 19.628 (Chile). Referenciada desde el
    prompt de consent del bot y desde el footer del sitio web."""
    return _PRIVACIDAD_HTML


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


@app.get("/admin/v2", response_class=HTMLResponse)
def admin_panel_v2(token: str | None = Query(None),
                   cmc_session: str | None = Cookie(None)):
    """Panel de recepción v2 (chat-first). Misma auth que /admin."""
    from admin_routes import _verify_cookie
    if token and token == ADMIN_TOKEN:
        return _ADMIN_V2_HTML.replace("__TOKEN__", token)
    if cmc_session:
        role = _verify_cookie(cmc_session)
        if role in ("admin", "ortodoncia"):
            return _ADMIN_V2_HTML.replace("__TOKEN__", "")
    return RedirectResponse(url="/admin/login", status_code=302)


@app.get("/admin/sw.js", include_in_schema=False)
def admin_service_worker():
    return FileResponse(
        str(Path(__file__).parent.parent / "static" / "pwa" / "admin-sw.js"),
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/admin/"},
    )


@app.get("/admin/v2/manifest.webmanifest", include_in_schema=False)
def admin_manifest(token: str | None = Query(None),
                   cmc_session: str | None = Cookie(None)):
    """Manifest dinámico: embebe token en start_url si el requester está autenticado."""
    import json as _json
    from admin_routes import _verify_cookie
    base = _json.loads((Path(__file__).parent.parent / "static" / "pwa" / "admin-manifest.webmanifest").read_text(encoding="utf-8"))
    if token and token == ADMIN_TOKEN:
        base["start_url"] = f"/admin/v2?token={token}"
    elif cmc_session and _verify_cookie(cmc_session) in ("admin", "ortodoncia"):
        base["start_url"] = "/admin/v2"  # cookie sigue válida en próximos launches
    return JSONResponse(base, media_type="application/manifest+json")


@app.get("/portal/sw.js", include_in_schema=False)
def portal_service_worker():
    return FileResponse(
        str(Path(__file__).parent.parent / "static" / "pwa" / "portal-sw.js"),
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/portal/"},
    )


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


@app.get("/portal/v2", response_class=HTMLResponse)
def portal_page_v2():
    """Portal del paciente v2 — IA modernizada (tabs, sidebar, best practices MyChart/MiSalud)."""
    return _PORTAL_V2_HTML or _PORTAL_HTML


@app.get("/portal/informe", response_class=HTMLResponse)
def portal_informe():
    """Informe imprimible de registros del paciente (HTML print-friendly)."""
    return _PORTAL_INFORME_HTML


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
    """Dashboard de planificación del MVP Meulen.

    Se re-lee el template desde disco en cada request y se envían headers de
    no-cache para que los cambios hechos vía `git pull` se reflejen sin
    requerir restart del servicio.
    """
    tpl_path = _TEMPLATE_DIR / "meulen_dashboard.html"
    html = tpl_path.read_text(encoding="utf-8") if tpl_path.exists() else _MEULEN_DASHBOARD_HTML
    return HTMLResponse(
        content=html,
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.get("/meulen/kpis", response_class=HTMLResponse)
def meulen_kpis_page():
    """Dashboard de KPIs del MVP Meulen — avance fases, módulos, tests, riesgos."""
    if not _MEULEN_KPIS_HTML:
        raise HTTPException(404, "Dashboard KPIs Meulen no disponible")
    return _MEULEN_KPIS_HTML


@app.get("/menu", response_class=HTMLResponse)
def menu_page():
    """Landing esquemático con todas las rutas desplegadas en agentecmc.cl."""
    return _MENU_HTML


def _seo_api_auth(token: str, cmc_session: str | None) -> None:
    """Acepta auth via ?token=... o cookie cmc_session admin. 401 si no."""
    if token == ADMIN_TOKEN:
        return
    from admin_routes import _verify_cookie
    if _verify_cookie(cmc_session or "") == "admin":
        return
    raise HTTPException(401, "unauthorized")


@app.get("/seo/dashboard", response_class=HTMLResponse)
def seo_dashboard_page(request: Request, token: str = "",
                       cmc_session: str | None = Cookie(None)):
    """Dashboard SEO. Acepta auth via ?token=... o cookie cmc_session
    (la misma del panel /admin). Si entrás con token query, se setea la
    cookie para que las próximas visitas funcionen sin token en URL."""
    from admin_routes import _verify_cookie, _set_session_cookie
    if not _SEO_DASHBOARD_HTML:
        raise HTTPException(404, "Dashboard SEO no disponible")

    has_query_token = token == ADMIN_TOKEN
    has_cookie = _verify_cookie(cmc_session or "") == "admin"

    if not (has_query_token or has_cookie):
        msg = (
            '<!DOCTYPE html><html lang="es"><head><meta charset="UTF-8">'
            '<title>Acceso requerido</title>'
            '<style>body{font-family:system-ui;background:#0f172a;color:#e2e8f0;'
            'display:flex;min-height:100vh;align-items:center;justify-content:center;'
            'margin:0;padding:20px}div{max-width:520px;text-align:center;'
            'background:#1e293b;padding:32px;border-radius:12px}h1{color:#38bdf8;'
            'margin:0 0 12px;font-size:1.4rem}p{color:#cbd5e1;line-height:1.5}'
            'code{background:#334155;padding:2px 8px;border-radius:4px;'
            'font-size:0.85em;color:#fbbf24}a{color:#38bdf8}</style></head><body>'
            '<div><h1>🔒 Dashboard SEO — acceso restringido</h1>'
            '<p>Este dashboard requiere autenticación. Andá primero a '
            '<a href="/admin?token=…">/admin?token=…</a> para iniciar sesión, '
            'o accedé directo con <code>?token=…</code> en la URL.</p></div>'
            '</body></html>'
        )
        return HTMLResponse(msg, status_code=401)

    response = HTMLResponse(
        _SEO_DASHBOARD_HTML.replace("__ADMIN_TOKEN_PLACEHOLDER__", ADMIN_TOKEN)
    )
    # Si autenticó con ?token=..., refrescamos la cookie para futuras visitas
    if has_query_token and not has_cookie:
        is_https = request.url.scheme == "https"
        _set_session_cookie(response, "admin", is_https)
    return response


@app.get("/crecimientopersonal", response_class=HTMLResponse)
@app.get("/crecimiento-personal", response_class=HTMLResponse)
def crecimiento_personal_page():
    """Roadmap personal de aprendizaje del Dr. Olavarría.
    Sin auth: es plan personal, no contiene datos sensibles del CMC."""
    if not _CRECIMIENTO_PERSONAL_HTML:
        raise HTTPException(404, "Dashboard Crecimiento Personal no disponible")
    return _CRECIMIENTO_PERSONAL_HTML


@app.get("/meta/dashboard", response_class=HTMLResponse)
@app.get("/meta-dashboard", response_class=HTMLResponse)
def meta_dashboard_page():
    """Dashboard dedicado de Meta Ads — el mayor canal de inversión y captación.
    Sin auth de cookie: usa el token del .env via /api/seo/meta-ads."""
    if not _META_DASHBOARD_HTML:
        raise HTTPException(404, "Dashboard Meta no disponible")
    return _META_DASHBOARD_HTML


@app.get("/horizonte", response_class=HTMLResponse)
@app.get("/horizonte/dashboard", response_class=HTMLResponse)
def horizonte_dashboard_page():
    """Roadmap estratégico de largo plazo del CMC — escenarios A/B/C, pipeline contratación, KPIs."""
    if not _HORIZONTE_DASHBOARD_HTML:
        raise HTTPException(404, "Dashboard Horizonte no disponible")
    return _HORIZONTE_DASHBOARD_HTML


_ATRIBUCION_DASHBOARD_HTML = (_TEMPLATE_DIR / "atribucion_dashboard.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "atribucion_dashboard.html").exists() else ""
_ABARCA_DASHBOARD_HTML = (_TEMPLATE_DIR / "abarca_dashboard.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "abarca_dashboard.html").exists() else ""

@app.get("/atribucion", response_class=HTMLResponse)
@app.get("/atribucion/dashboard", response_class=HTMLResponse)
def atribucion_dashboard_page():
    """Cruce diario Meta Ads × Bot × Pacientes nuevos × Referidos."""
    if not _ATRIBUCION_DASHBOARD_HTML:
        raise HTTPException(404, "Dashboard Atribución no disponible")
    return _ATRIBUCION_DASHBOARD_HTML


@app.get("/abarca", response_class=HTMLResponse)
@app.get("/abarca/dashboard", response_class=HTMLResponse)
@app.get("/abarca/2026", response_class=HTMLResponse)
def abarca_dashboard_page():
    """Análisis de carga del Dr. Abarca. /abarca = histórico total · /abarca/2026 = solo 2026."""
    if not _ABARCA_DASHBOARD_HTML:
        raise HTTPException(404, "Dashboard Abarca no disponible")
    return _ABARCA_DASHBOARD_HTML


_ABARCA_CACHE_PATH = Path(__file__).parent.parent / "data" / "abarca_cache.json"
_ABARCA_CACHE_TTL_SEC = 86400  # 24h (refresh manual con ?refresh=1)


@app.get("/api/abarca/data")
async def api_abarca_data(refresh: int = 0, desde: str = "2025-05-01"):
    """Atenciones del Dr. Abarca (id=73). El cache crudo va desde 2025-05-01.

    `?desde=YYYY-MM-DD` filtra agregaciones desde esa fecha (default 2025-05-01).
    `?refresh=1` fuerza recarga desde Medilink.
    """
    import json as _json_ab
    import time as _time_ab
    from datetime import datetime as _dt_ab, date as _date_ab
    from collections import defaultdict as _dd_ab, Counter as _ct_ab
    from config import MEDILINK_BASE_URL as _MB

    raw = None
    cache_age = None
    if not refresh and _ABARCA_CACHE_PATH.exists():
        cache_age = _time_ab.time() - _ABARCA_CACHE_PATH.stat().st_mtime
        if cache_age < _ABARCA_CACHE_TTL_SEC:
            try:
                raw = _json_ab.loads(_ABARCA_CACHE_PATH.read_text(encoding="utf-8"))
            except Exception:
                raw = None

    if raw is None:
        # Fetch día a día (eq por fecha = sin problema de paginación, secuencial = sin 429)
        from datetime import date as _date_ab2, timedelta as _td_ab2
        import asyncio as _aio_ab
        raw = []
        seen_ids = set()
        d = _date_ab2(2025, 5, 1)
        hoy = _date_ab2.today()
        async with httpx.AsyncClient(timeout=30) as cli:
            while d <= hoy:
                if d.weekday() == 6:  # domingo, sin atenciones
                    d += _td_ab2(days=1); continue
                params = {"id_sucursal": {"eq": 1}, "id_profesional": {"eq": 73},
                          "fecha": {"eq": d.isoformat()}}
                pq = {"q": _json_ab.dumps(params, separators=(",", ":"))}
                resp = None
                for attempt in range(6):
                    resp = await cli.get(f"{_MB}/citas", params=pq, headers=HEADERS_MEDILINK)
                    if resp.status_code == 200:
                        break
                    if resp.status_code == 429:
                        await _aio_ab.sleep(1.5 + attempt * 1.5)
                        continue
                    break
                if resp is not None and resp.status_code == 200:
                    for c in resp.json().get("data", []):
                        cid = c.get("id")
                        if cid in seen_ids:
                            continue
                        seen_ids.add(cid)
                        raw.append(c)
                d += _td_ab2(days=1)
                await _aio_ab.sleep(0.15)
        try:
            _ABARCA_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            _ABARCA_CACHE_PATH.write_text(_json_ab.dumps(raw, ensure_ascii=False, default=str), encoding="utf-8")
        except Exception:
            pass

    # ── Delta refresh: siempre traer las atenciones de HOY ────────────────────
    # El cache vive 24h, pero el día actual cambia constantemente. Una llamada
    # extra a Medilink mantiene la métrica de hoy fresca sin esperar refresh
    # completo de 7 min.
    from datetime import date as _date_dr
    from config import MEDILINK_BASE_URL as _MB_dr
    hoy_iso = _date_dr.today().isoformat()
    try:
        params_hoy = {"id_sucursal": {"eq": 1}, "id_profesional": {"eq": 73},
                      "fecha": {"eq": hoy_iso}}
        async with httpx.AsyncClient(timeout=10) as cli_dr:
            resp_dr = await cli_dr.get(
                f"{_MB_dr}/citas",
                params={"q": _json_ab.dumps(params_hoy, separators=(",", ":"))},
                headers=HEADERS_MEDILINK,
            )
        if resp_dr.status_code == 200:
            citas_hoy = resp_dr.json().get("data", [])
            raw = [c for c in raw if (c.get("fecha") or "")[:10] != hoy_iso] + citas_hoy
            log.info("abarca delta refresh hoy=%s: %d citas", hoy_iso, len(citas_hoy))
        else:
            log.warning("abarca delta refresh hoy=%s HTTP %s", hoy_iso, resp_dr.status_code)
    except Exception as _e_dr:
        log.warning("abarca delta refresh hoy falló: %s", _e_dr)

    # ── Agregaciones ──
    por_dia: dict = {}
    por_mes: dict = _dd_ab(lambda: {"atend": 0, "anul": 0, "no_asiste": 0, "otros": 0, "total": 0})
    por_dow: dict = _dd_ab(list)   # weekday → [atendidos por día trabajado]
    por_hora: dict = _dd_ab(int)    # hora → atenciones
    estados: dict = _ct_ab()

    for c in raw:
        f = (c.get("fecha") or "")[:10]
        if not f or f < desde:
            continue
        st = (c.get("estado_cita") or "").lower()
        estados[c.get("estado_cita") or "?"] += 1
        m = f[:7]
        por_mes[m]["total"] += 1
        if st == "atendido":
            por_mes[m]["atend"] += 1
            por_dia[f] = por_dia.get(f, 0) + 1
            h = (c.get("hora_inicio") or "")[:2]
            if h.isdigit():
                por_hora[int(h)] += 1
        elif st == "anulado" or "anulad" in st:
            por_mes[m]["anul"] += 1
        elif "asiste" in st:
            por_mes[m]["no_asiste"] += 1
        else:
            por_mes[m]["otros"] += 1

    # Asegurar todos los días del rango aparezcan (con 0)
    from datetime import timedelta as _td_ab
    try:
        start = _date_ab.fromisoformat(desde)
    except ValueError:
        start = _date_ab(2025, 5, 1)
    end = _date_ab.today()
    d = start
    while d <= end:
        f = d.isoformat()
        por_dia.setdefault(f, 0)
        d += _td_ab(days=1)

    # por_dow stats
    for f, n in por_dia.items():
        if n > 0:
            dt = _date_ab.fromisoformat(f)
            por_dow[dt.weekday()].append(n)

    dow_stats = {}
    for w in range(7):
        vals = sorted(por_dow.get(w, []))
        if not vals:
            dow_stats[w] = {"avg": 0, "median": 0, "min": 0, "max": 0, "p90": 0, "n": 0}
        else:
            n_v = len(vals)
            p90_idx = max(0, int(n_v * 0.9) - 1) if n_v >= 10 else n_v - 1
            dow_stats[w] = {
                "avg": round(sum(vals) / n_v, 2),
                "median": vals[n_v // 2],
                "min": vals[0],
                "max": vals[-1],
                "p90": vals[p90_idx],
                "n": n_v,
            }

    # KPIs
    total_atend = sum(v for v in por_dia.values())
    dias_trab = sum(1 for v in por_dia.values() if v > 0)
    n_meses = max(1, len(por_mes))
    atend_avg_mes = total_atend / n_meses
    ing_avg_mes = atend_avg_mes * 15100
    delta_avg_mes = ing_avg_mes - 3414000

    return {
        "fecha_actualizacion": _dt_ab.now().strftime("%Y-%m-%d %H:%M"),
        "fuente_cache": "json local (TTL 24h)" if cache_age and cache_age < _ABARCA_CACHE_TTL_SEC else "Medilink fresh",
        "por_dia": por_dia,
        "por_mes": dict(por_mes),
        "por_dow": dow_stats,
        "por_hora": dict(por_hora),
        "estados": dict(estados),
        "kpis": {
            "total_atend": total_atend,
            "dias_con_atencion": dias_trab,
            "atend_avg_mes": round(atend_avg_mes, 1),
            "ing_avg_mes": round(ing_avg_mes),
            "delta_avg_mes": round(delta_avg_mes),
            "n_meses": n_meses,
        },
    }


@app.get("/api/atribucion/today")
async def api_atribucion_today():
    """Cruce de datos para el dashboard /atribucion. Devuelve día actual.

    Combina:
    - Meta Ads (Marketing API): spend, impresiones, clicks, conversaciones
    - Bot: mensajes, phones nuevos, citas creadas, registros completos
    - Tags de referido: distribución por canal (amigo/rrss/google/recurrente)
    """
    import json as _json_atr
    from datetime import datetime as _dt_atr
    from pathlib import Path as _P_atr
    import sys as _sys_atr
    _sys_atr.path.insert(0, str(_P_atr(__file__).parent))
    from session import _conn as _conn_atr

    today = _dt_atr.now().strftime("%Y-%m-%d")
    out: dict = {"fecha": today, "meta": {}, "bot": {}, "atribucion": {}, "funnel": {}}

    conn = _conn_atr()
    c = conn.cursor()

    # Bot: actividad del día
    c.execute("SELECT COUNT(*) FROM messages WHERE date(ts)=date('now')")
    out["bot"]["mensajes_total"] = c.fetchone()[0]

    c.execute("""SELECT COUNT(DISTINCT phone) FROM messages WHERE date(ts)=date('now')
                 AND phone NOT IN (SELECT DISTINCT phone FROM messages WHERE date(ts) < date('now'))""")
    out["bot"]["phones_nuevos"] = c.fetchone()[0]

    c.execute("""SELECT COUNT(*) FROM conversation_events
                 WHERE event='cita_creada' AND date(ts)=date('now')""")
    out["bot"]["citas_creadas"] = c.fetchone()[0]

    c.execute("""SELECT COUNT(*) FROM conversation_events
                 WHERE event='registro_completo' AND date(ts)=date('now')""")
    out["bot"]["registros_completos"] = c.fetchone()[0]

    c.execute("""SELECT COUNT(*) FROM conversation_events
                 WHERE event='cita_bloqueada_mismo_profesional' AND date(ts)=date('now')""")
    out["bot"]["bloqueos_mismo_prof"] = c.fetchone()[0]

    c.execute("""SELECT COUNT(*) FROM conversation_events
                 WHERE event='derivado_humano' AND date(ts)=date('now')""")
    out["bot"]["derivados_humano"] = c.fetchone()[0]

    # Atribución por tags de referido
    c.execute("""SELECT tag, COUNT(*) FROM contact_tags
                 WHERE tag LIKE 'referido:%' AND date(ts)=date('now')
                 GROUP BY tag ORDER BY 2 DESC""")
    refs = {r[0].split(":", 1)[1]: r[1] for r in c.fetchall()}
    out["atribucion"]["por_canal_hoy"] = refs
    out["atribucion"]["respondieron_post"] = sum(refs.values())

    c.execute("""SELECT tag, COUNT(*) FROM contact_tags
                 WHERE tag LIKE 'referido:%' AND ts > datetime('now','-30 days')
                 GROUP BY tag ORDER BY 2 DESC""")
    out["atribucion"]["por_canal_30d"] = {r[0].split(":", 1)[1]: r[1] for r in c.fetchall()}

    # Funnel del día: phones nuevos → cita
    c.execute("""SELECT COUNT(DISTINCT ce.phone) FROM conversation_events ce
                 WHERE ce.event='cita_creada' AND date(ce.ts)=date('now')
                   AND ce.phone IN (
                     SELECT phone FROM messages WHERE date(ts)=date('now')
                       AND phone NOT IN (SELECT DISTINCT phone FROM messages WHERE date(ts) < date('now'))
                   )""")
    nuevos_con_cita = c.fetchone()[0]
    out["funnel"]["phones_nuevos_con_cita"] = nuevos_con_cita
    if out["bot"]["phones_nuevos"]:
        out["funnel"]["conversion_pct"] = round(100.0 * nuevos_con_cita / out["bot"]["phones_nuevos"], 1)
    else:
        out["funnel"]["conversion_pct"] = 0

    # Meta Ads del día
    import os as _os_atr, urllib.request as _ur_atr, urllib.parse as _up_atr
    token = (_os_atr.getenv("META_ACCESS_TOKEN") or "").strip()
    acct = "act_220608142267129"
    if token:
        try:
            params = {
                "fields": "spend,impressions,reach,clicks,actions",
                "time_range": _json_atr.dumps({"since": today, "until": today}),
                "access_token": token,
            }
            url = f"https://graph.facebook.com/v19.0/{acct}/insights?" + _up_atr.urlencode(params)
            with _ur_atr.urlopen(url, timeout=10) as resp:
                d = _json_atr.loads(resp.read())
                rows = d.get("data", [])
                if rows:
                    r = rows[0]
                    out["meta"] = {
                        "spend_clp": float(r.get("spend", 0)),
                        "impresiones": int(r.get("impressions", 0)),
                        "reach": int(r.get("reach", 0)),
                        "clicks": int(r.get("clicks", 0)),
                    }
                    for a in (r.get("actions") or []):
                        if a.get("action_type") == "link_click":
                            out["meta"]["link_clicks"] = int(float(a.get("value", 0)))
                        elif a.get("action_type") == "onsite_conversion.messaging_conversation_started_7d":
                            out["meta"]["conversaciones_iniciadas"] = int(float(a.get("value", 0)))
                else:
                    out["meta"] = {"spend_clp": 0, "impresiones": 0, "reach": 0, "clicks": 0}
        except Exception as e:
            out["meta"] = {"error": str(e)[:120]}

    return out


# ─────────────────────────────────────────────────────────────────────────
# Pipeline de Contratación — tracking de búsquedas activas y candidatos
# ─────────────────────────────────────────────────────────────────────────
import sqlite3 as _sqlite3_hiring
from datetime import datetime as _dt_hiring

def _hiring_db():
    db_path = Path(__file__).parent.parent / "data" / "heatmap_cache.db"
    conn = _sqlite3_hiring.connect(str(db_path))
    conn.row_factory = _sqlite3_hiring.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hiring_pipeline (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            especialidad TEXT NOT NULL,
            prioridad TEXT NOT NULL DEFAULT 'media',
            estado TEXT NOT NULL DEFAULT 'busqueda',
            candidato_nombre TEXT,
            candidato_contacto TEXT,
            fuente TEXT,
            fecha_inicio TEXT,
            fecha_proxima_accion TEXT,
            notas TEXT,
            escenario TEXT,
            jornada TEXT,
            sueldo_estimado INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    return conn


@app.get("/api/hiring/pipeline")
def hiring_pipeline_list():
    """Lista todas las búsquedas activas del pipeline de contratación."""
    conn = _hiring_db()
    rows = conn.execute(
        "SELECT * FROM hiring_pipeline ORDER BY "
        "CASE prioridad WHEN 'critica' THEN 0 WHEN 'alta' THEN 1 WHEN 'media' THEN 2 ELSE 3 END, "
        "CASE estado WHEN 'contratado' THEN 9 WHEN 'descartado' THEN 8 ELSE 0 END, "
        "id DESC"
    ).fetchall()
    conn.close()
    items = [dict(r) for r in rows]
    by_estado = {}
    for it in items:
        by_estado[it["estado"]] = by_estado.get(it["estado"], 0) + 1
    return {
        "items": items,
        "total": len(items),
        "by_estado": by_estado,
        "activos": sum(1 for it in items if it["estado"] not in ("contratado", "descartado")),
    }


@app.post("/api/hiring/pipeline")
async def hiring_pipeline_create(request: Request):
    body = await request.json()
    if not body.get("especialidad"):
        raise HTTPException(400, "especialidad requerida")
    conn = _hiring_db()
    cur = conn.execute(
        """INSERT INTO hiring_pipeline
        (especialidad, prioridad, estado, candidato_nombre, candidato_contacto,
         fuente, fecha_inicio, fecha_proxima_accion, notas, escenario, jornada, sueldo_estimado)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            body.get("especialidad"),
            body.get("prioridad", "media"),
            body.get("estado", "busqueda"),
            body.get("candidato_nombre"),
            body.get("candidato_contacto"),
            body.get("fuente"),
            body.get("fecha_inicio") or _dt_hiring.now().strftime("%Y-%m-%d"),
            body.get("fecha_proxima_accion"),
            body.get("notas"),
            body.get("escenario"),
            body.get("jornada"),
            body.get("sueldo_estimado"),
        ),
    )
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return {"id": new_id, "ok": True}


@app.put("/api/hiring/pipeline/{item_id}")
async def hiring_pipeline_update(item_id: int, request: Request):
    body = await request.json()
    allowed = {"especialidad", "prioridad", "estado", "candidato_nombre", "candidato_contacto",
               "fuente", "fecha_inicio", "fecha_proxima_accion", "notas", "escenario",
               "jornada", "sueldo_estimado"}
    fields = {k: v for k, v in body.items() if k in allowed}
    if not fields:
        raise HTTPException(400, "Sin campos a actualizar")
    set_clause = ", ".join([f"{k}=?" for k in fields.keys()]) + ", updated_at=CURRENT_TIMESTAMP"
    conn = _hiring_db()
    conn.execute(
        f"UPDATE hiring_pipeline SET {set_clause} WHERE id=?",
        (*fields.values(), item_id),
    )
    conn.commit()
    conn.close()
    return {"ok": True}


@app.delete("/api/hiring/pipeline/{item_id}")
def hiring_pipeline_delete(item_id: int):
    conn = _hiring_db()
    conn.execute("DELETE FROM hiring_pipeline WHERE id=?", (item_id,))
    conn.commit()
    conn.close()
    return {"ok": True}


# ─────────────────────────────────────────────────────────────────────────
# Meta Ads (Marketing API) — análisis y cruce con citas del chatbot
# ─────────────────────────────────────────────────────────────────────────

META_AD_ACCOUNT_ID = "act_220608142267129"


def _meta_get(path: str, params: dict | None = None) -> dict:
    """Helper para llamar Marketing API con el token del .env."""
    import os, json, urllib.request, urllib.parse
    token = os.getenv("META_ACCESS_TOKEN", "")
    if not token:
        return {"error": "no META_ACCESS_TOKEN"}
    base = "https://graph.facebook.com/v19.0"
    p = dict(params or {})
    p["access_token"] = token
    url = f"{base}/{path}?" + urllib.parse.urlencode(p)
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"error": str(e)}


def _sum_conv(actions: list) -> int:
    """Suma conversaciones iniciadas (los 2 action types relevantes)."""
    if not actions:
        return 0
    types = ("onsite_conversion.messaging_conversation_started_7d",
             "onsite_conversion.messaging_first_reply")
    return sum(int(a.get("value", 0)) for a in actions if a.get("action_type") in types)


@app.get("/api/seo/meta-ads")
def seo_meta_ads_api(periodo: str = "last_90d", token: str = "",
                    cmc_session: str | None = Cookie(None)):
    """Análisis de Meta Ads (FB+IG → WhatsApp) cruzado con citas del chatbot."""
    _seo_api_auth(token, cmc_session)

    # 1. Lifetime totales
    lifetime = _meta_get(f"{META_AD_ACCOUNT_ID}/insights",
                         {"fields": "spend,impressions,reach,clicks,actions",
                          "date_preset": "maximum"})

    # 2. Serie mensual (todos los meses)
    monthly = _meta_get(f"{META_AD_ACCOUNT_ID}/insights",
                        {"fields": "spend,impressions,reach,clicks,frequency,actions",
                         "time_increment": "monthly", "date_preset": "maximum"})

    # 3. Top campañas (lifetime)
    campaigns = _meta_get(f"{META_AD_ACCOUNT_ID}/insights",
                          {"fields": "campaign_name,spend,impressions,clicks,frequency,actions",
                           "level": "campaign", "date_preset": "maximum", "limit": 50})

    # 4. Por placement
    placement = _meta_get(f"{META_AD_ACCOUNT_ID}/insights",
                          {"fields": "spend,impressions,clicks,actions",
                           "breakdowns": "publisher_platform,platform_position",
                           "date_preset": "maximum"})

    # 5. Demografía
    demo = _meta_get(f"{META_AD_ACCOUNT_ID}/insights",
                     {"fields": "spend,impressions,clicks,actions",
                      "breakdowns": "age,gender", "date_preset": "maximum"})

    # 6. Por hora (últimos 90d para no sobrepasar)
    hourly = _meta_get(f"{META_AD_ACCOUNT_ID}/insights",
                       {"fields": "spend,clicks,actions",
                        "breakdowns": "hourly_stats_aggregated_by_advertiser_time_zone",
                        "date_preset": "last_90d"})

    # 7. Cruce con chatbot: pacientes nuevos por mes (correlación)
    import sqlite3
    from pathlib import Path as _Path
    db_path = _Path(__file__).parent.parent / "data" / "heatmap_cache.db"
    nuevos_mes = []
    if db_path.exists():
        conn = sqlite3.connect(str(db_path))
        try:
            for mes, n in conn.execute("""
                WITH primera AS (
                    SELECT id_paciente, MIN(fecha) AS f
                    FROM citas_heatmap WHERE id_paciente IS NOT NULL
                    GROUP BY id_paciente)
                SELECT substr(f,1,7) AS mes, COUNT(*) FROM primera
                GROUP BY mes ORDER BY mes
            """).fetchall():
                nuevos_mes.append({"mes": mes, "pacientes_nuevos": n})
        finally:
            conn.close()

    # Procesar respuestas
    def proc_lifetime(resp):
        if not resp.get("data"): return {}
        r = resp["data"][0]
        return {
            "spend": float(r.get("spend", 0)),
            "impresiones": int(r.get("impressions", 0)),
            "reach": int(r.get("reach", 0)),
            "clicks": int(r.get("clicks", 0)),
            "conversaciones": _sum_conv(r.get("actions", [])),
            "link_clicks": next((int(a["value"]) for a in r.get("actions", []) if a["action_type"] == "link_click"), 0),
        }

    def proc_monthly(resp):
        out = []
        for r in resp.get("data", []):
            spend = float(r.get("spend", 0))
            convs = _sum_conv(r.get("actions", []))
            out.append({
                "mes": r.get("date_start", "")[:7],
                "spend": spend,
                "impresiones": int(r.get("impressions", 0)),
                "clicks": int(r.get("clicks", 0)),
                "frecuencia": float(r.get("frequency", 0)),
                "conversaciones": convs,
                "cpa": round(spend / convs, 0) if convs else None,
            })
        return out

    def proc_campaigns(resp):
        out = []
        for r in resp.get("data", []):
            spend = float(r.get("spend", 0))
            convs = _sum_conv(r.get("actions", []))
            out.append({
                "nombre": r.get("campaign_name", "")[:80],
                "spend": spend,
                "impresiones": int(r.get("impressions", 0)),
                "clicks": int(r.get("clicks", 0)),
                "frecuencia": float(r.get("frequency", 0)),
                "conversaciones": convs,
                "cpa": round(spend / convs, 0) if convs else None,
                "saturacion": "🔴" if r.get("frequency", 0) and float(r["frequency"]) > 8 else
                              "🟠" if r.get("frequency", 0) and float(r["frequency"]) > 4 else "🟢",
            })
        return sorted(out, key=lambda x: -x["spend"])

    def proc_placement(resp):
        out = []
        for r in resp.get("data", []):
            spend = float(r.get("spend", 0))
            if spend < 100: continue  # filtrar ruido
            out.append({
                "plataforma": r.get("publisher_platform", ""),
                "posicion": r.get("platform_position", ""),
                "spend": spend,
                "impresiones": int(r.get("impressions", 0)),
                "clicks": int(r.get("clicks", 0)),
            })
        return sorted(out, key=lambda x: -x["spend"])

    def proc_demo(resp):
        out = []
        total = sum(float(r.get("spend", 0)) for r in resp.get("data", []))
        for r in resp.get("data", []):
            spend = float(r.get("spend", 0))
            if spend < 100: continue
            out.append({
                "edad": r.get("age", ""),
                "genero": r.get("gender", ""),
                "spend": spend,
                "pct": round(spend / total * 100, 1) if total else 0,
                "impresiones": int(r.get("impressions", 0)),
                "clicks": int(r.get("clicks", 0)),
            })
        return sorted(out, key=lambda x: -x["spend"])

    def proc_hourly(resp):
        out = []
        for r in resp.get("data", []):
            h = r.get("hourly_stats_aggregated_by_advertiser_time_zone", "")
            hora = int(h.split(":")[0]) if h else 0
            spend = float(r.get("spend", 0))
            convs = _sum_conv(r.get("actions", []))
            out.append({
                "hora": hora,
                "spend": spend,
                "clicks": int(r.get("clicks", 0)),
                "conversaciones": convs,
                "cpa": round(spend / convs, 0) if convs else None,
            })
        return sorted(out, key=lambda x: x["hora"])

    return {
        "fuente": "meta_marketing_api",
        "ad_account_id": META_AD_ACCOUNT_ID,
        "lifetime": proc_lifetime(lifetime),
        "monthly": proc_monthly(monthly),
        "top_campaigns": proc_campaigns(campaigns)[:20],
        "placement": proc_placement(placement),
        "demografia": proc_demo(demo),
        "hourly": proc_hourly(hourly),
        "pacientes_nuevos_chatbot": nuevos_mes,
    }


# Población oficial INE (Censo 2017 / proyección 2024). Provincia de Arauco
# y vecinas del Gran Concepción. Sirve para calcular % de población captada.
POBLACION_COMUNA = {
    "ARAUCO":              37000,   # comuna completa
    "Arauco":              16000,   # solo zona urbana
    "Carampangue":          5000,
    "Laraquete":            4000,
    "Ramadillas":           1500,
    "Tubul":                1500,
    "Llico":                 800,
    "Colico":                500,
    "CURANILAHUE":         32000,
    "LOS ÁLAMOS":          21000,
    "CAÑETE":              32000,
    "LEBU":                26000,
    "TIRÚA":               11000,
    "CONTULMO":             6000,
    "LOTA":                43000,
    "CORONEL":            116000,
    "CONCEPCIÓN":         230000,
    "SAN PEDRO DE LA PAZ":142000,
    "TALCAHUANO":         154000,
}


def _enriquecer_comunas(comunas: list[dict]) -> list[dict]:
    """Agrega poblacion_total y pct_captado (penetración) a cada fila."""
    for c in comunas:
        pob = POBLACION_COMUNA.get(c["comuna"])
        if pob:
            c["poblacion_total"] = pob
            c["pct_captado"] = round(c["pacientes"] / pob * 100, 2)
    return comunas


@app.get("/api/seo/geo")
def seo_geo_api(periodo: str = "todos", desde: str | None = None,
                hasta: str | None = None, profesional: str = "",
                token: str = "",
                cmc_session: str | None = Cookie(None)):
    _seo_api_auth(token, cmc_session)
    """Sirve el cruce comunas/atenciones para el dashboard SEO.

    Lee data/heatmap_*.json (snapshot del periodo completo) cuando no hay
    filtro de fechas. Si se pasa `periodo`/`desde`/`hasta`, recalcula los
    conteos contra el SQLite (`data/heatmap_cache.db`) restringido al
    rango pedido — fuente de verdad temporal.
    """
    import json, re, glob, os
    from pathlib import Path

    # Tomar el heatmap más reciente
    files = sorted(glob.glob(str(Path(__file__).parent.parent / "data" / "heatmap_*.json")),
                   key=os.path.getmtime, reverse=True)
    if not files:
        return {"error": "no heatmap data"}
    raw = json.loads(Path(files[0]).read_text(encoding="utf-8"))

    # Normalizar variantes con typos: agrupar por palabra base
    NORMALIZE = {
        r"^CURAN[IM]?L?A?H?U?E?\.?$": "CURANILAHUE",
        r"^LO[SA]?\s*A?L?[AÁ]?M?O?S?\.?$": "LOS ÁLAMOS",
        r"^ARAU[CU]+O?\s*-?$": "ARAUCO",
        r"^CONCEPCI[OÓ]N$": "CONCEPCIÓN",
        r"^SAN\s+JOS[EÉ]\s+(DE\s+)?C[OÓ]LICO$": "SAN JOSÉ DE CÓLICO",
    }
    grouped = {}
    for c in raw.get("comunas", []):
        nombre = c["comuna"].strip().upper()
        canonical = nombre
        for pattern, target in NORMALIZE.items():
            if re.match(pattern, nombre):
                canonical = target
                break
        if canonical in grouped:
            grouped[canonical]["pacientes"] += c["pacientes"]
            grouped[canonical]["citas"] += c["citas"]
        else:
            grouped[canonical] = {"comuna": canonical, "pacientes": c["pacientes"], "citas": c["citas"]}

    # Expandir ARAUCO en sus localidades reales (si el JSON las trae)
    if "ARAUCO" in grouped and raw.get("localidades_arauco"):
        del grouped["ARAUCO"]
        # Mapeo de nombres internos → nombres reconocibles para el público
        DISPLAY_NAME = {
            "ARAUCO URBANO": "Arauco",  # la gente busca "arauco", no "arauco urbano"
            "ARAUCO (OTRO)": None,       # descartar agregados sin detalle
            "ARAUCO (SIN DETALLE)": None,
        }
        for loc in raw["localidades_arauco"]:
            nombre = loc["localidad"].strip().upper()
            display = DISPLAY_NAME.get(nombre, loc["localidad"].strip().title())
            if display is None:
                continue
            # Sumar si ya existe (caso edge: dos buckets que mapean al mismo display)
            if display in grouped:
                grouped[display]["pacientes"] += loc["pacientes"]
                grouped[display]["citas"] += loc.get("citas", 0)
            else:
                grouped[display] = {
                    "comuna": display,
                    "pacientes": loc["pacientes"],
                    "citas": loc.get("citas", 0),
                    "es_localidad_arauco": True,
                }

    total_pac = sum(g["pacientes"] for g in grouped.values())
    total_cit = sum(g["citas"] for g in grouped.values())
    comunas = sorted(grouped.values(), key=lambda x: x["pacientes"], reverse=True)
    for c in comunas:
        c["pct"] = round(c["pacientes"] / total_pac * 100, 1) if total_pac else 0
        c["pct_citas"] = round(c["citas"] / total_cit * 100, 1) if total_cit else 0

    # Leer rango real + serie mensual del SQLite cache (fuente única de verdad)
    import sqlite3
    db_path = Path(__file__).parent.parent / "data" / "heatmap_cache.db"
    rango = None
    serie_mensual = []
    if db_path.exists():
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT MIN(fecha), MAX(fecha) FROM citas_heatmap"
            ).fetchone()
            if row and row[0]:
                rango = {"desde": row[0], "hasta": row[1]}
            # CTE: para cada paciente, mes de su PRIMERA cita (= mes en que es "nuevo")
            serie_rows = conn.execute("""
                WITH primera AS (
                    SELECT id_paciente, MIN(fecha) AS f_primera
                    FROM citas_heatmap
                    WHERE id_paciente IS NOT NULL
                    GROUP BY id_paciente
                ),
                mensual AS (
                    SELECT substr(fecha,1,7) AS mes,
                           COUNT(*) AS citas,
                           COUNT(DISTINCT id_paciente) AS pac_unicos
                    FROM citas_heatmap
                    GROUP BY mes
                ),
                nuevos AS (
                    SELECT substr(f_primera,1,7) AS mes, COUNT(*) AS pac_nuevos
                    FROM primera GROUP BY mes
                )
                SELECT m.mes, m.citas, m.pac_unicos, COALESCE(n.pac_nuevos, 0) AS pac_nuevos
                FROM mensual m
                LEFT JOIN nuevos n ON n.mes = m.mes
                ORDER BY m.mes
            """).fetchall()
            acumulado = 0
            for mes, citas, unicos, nuevos in serie_rows:
                acumulado += nuevos
                serie_mensual.append({
                    "mes": mes,
                    "citas": citas,
                    "pacientes_unicos": unicos,
                    "pacientes_nuevos": nuevos,
                    "pacientes_acumulado": acumulado,
                })
            # Totales recalculados del SQLite (fuente live, no del JSON snapshot)
            tot_row = conn.execute("""
                SELECT COUNT(*) AS citas, COUNT(DISTINCT id_paciente) AS unicos
                FROM citas_heatmap
            """).fetchone()
            sqlite_total_citas, sqlite_total_pac = (tot_row[0], tot_row[1]) if tot_row else (0, 0)
            con_com_row = conn.execute("""
                SELECT COUNT(DISTINCT c.id_paciente)
                FROM citas_heatmap c
                INNER JOIN pacientes_heatmap p ON p.id = c.id_paciente
                WHERE TRIM(COALESCE(p.comuna,'')) != ''
            """).fetchone()
            sqlite_con_comuna = con_com_row[0] if con_com_row else 0
        finally:
            conn.close()

    # Si hay filtro de fechas, recalcular las comunas contra el SQLite
    # restringido al rango. Pierde el detalle de localidades dentro de Arauco
    # (eso lo provee el script que escribe el JSON snapshot), pero responde
    # con conteos exactos por comuna en el periodo solicitado.
    fecha_desde, fecha_hasta = _resolver_rango(periodo, desde, hasta)
    if fecha_desde or fecha_hasta or profesional:
        if not db_path.exists():
            return {"error": "no cache for date range"}
        clause, params = "", ()
        if fecha_desde and fecha_hasta:
            clause = " AND c.fecha BETWEEN ? AND ?"
            params = (fecha_desde, fecha_hasta)
        elif fecha_desde:
            clause = " AND c.fecha >= ?"
            params = (fecha_desde,)
        elif fecha_hasta:
            clause = " AND c.fecha <= ?"
            params = (fecha_hasta,)
        if profesional:
            clause += " AND c.nombre_profesional = ?"
            params = params + (profesional,)

        conn = sqlite3.connect(str(db_path))
        try:
            tot_cit = conn.execute(
                f"SELECT COUNT(*) FROM citas_heatmap c WHERE 1=1{clause}",
                params,
            ).fetchone()[0]
            tot_pac = conn.execute(
                f"SELECT COUNT(DISTINCT c.id_paciente) FROM citas_heatmap c "
                f"WHERE c.id_paciente IS NOT NULL{clause}",
                params,
            ).fetchone()[0]
            rows = conn.execute(
                f"""
                SELECT TRIM(COALESCE(p.comuna, '')) AS comuna,
                       COUNT(DISTINCT c.id_paciente) AS pacientes,
                       COUNT(*) AS citas
                FROM citas_heatmap c
                INNER JOIN pacientes_heatmap p ON p.id = c.id_paciente
                WHERE c.id_paciente IS NOT NULL{clause}
                GROUP BY comuna
                """,
                params,
            ).fetchall()
        finally:
            conn.close()

        # Aplicar la misma normalización de typos que en el branch sin filtro
        grouped_f: dict[str, dict] = {}
        sin_com = 0
        ARAUCO_PATTERN = r"^ARAU[CU]+O?\s*-?$"
        # Detectar pacientes ARAUCO para expandir por localidad luego
        arauco_buckets: list[tuple[str, int, int]] = []  # (raw_name, pac, cit)
        for nombre_raw, pac, cit in rows:
            nombre = (nombre_raw or "").strip().upper()
            if (not nombre or nombre.isdigit() or len(nombre) < 3
                    or any(x in nombre for x in ("VOLCAN", "CALLE", "PASAJE", "#"))):
                sin_com += pac
                continue
            # Localidades dentro de la comuna de Arauco que se digitan en el campo
            # `comuna` por convención local — marcarlas para expansión posterior.
            if nombre in ("ARAUCO", "LARAQUETE", "RAMADILLAS", "CARAMPANGUE",
                          "TUBUL", "LLICO", "COLICO", "CÓLICO",
                          "PUNTA LAVAPIE", "PUNTA LAVAPIÉ", "ARAUUCO", "ARA"):
                arauco_buckets.append((nombre, pac, cit))
                continue
            if re.match(ARAUCO_PATTERN, nombre):
                arauco_buckets.append((nombre, pac, cit))
                continue
            canonical = nombre
            for pattern, target in NORMALIZE.items():
                if re.match(pattern, nombre):
                    canonical = target
                    break
            if canonical in grouped_f:
                grouped_f[canonical]["pacientes"] += pac
                grouped_f[canonical]["citas"] += cit
            else:
                grouped_f[canonical] = {
                    "comuna": canonical, "pacientes": pac, "citas": cit,
                }

        # Expandir ARAUCO en localidades reales mirando p.direccion
        if arauco_buckets:
            conn_a = sqlite3.connect(str(db_path))
            try:
                # Si el campo comuna ya dice "CARAMPANGUE" / "LARAQUETE" / etc.,
                # respetarlo; si dice "ARAUCO" o variante, mirar p.direccion.
                where_arauco = (
                    "(UPPER(TRIM(p.comuna)) IN ('ARAUCO','ARAUUCO','ARA') "
                    "OR UPPER(TRIM(p.comuna)) LIKE 'ARAUCO%')"
                )
                arauco_rows = conn_a.execute(
                    f"""SELECT c.id_paciente AS pid,
                              UPPER(TRIM(p.comuna)) AS com,
                              LOWER(COALESCE(p.direccion,'')) AS dir,
                              COUNT(*) AS citas
                       FROM citas_heatmap c
                       INNER JOIN pacientes_heatmap p ON p.id = c.id_paciente
                       WHERE c.id_paciente IS NOT NULL{clause}
                         AND {where_arauco}
                       GROUP BY c.id_paciente
                    """,
                    params,
                ).fetchall()
            finally:
                conn_a.close()
            local_pac: dict[str, set] = {}
            local_cit: dict[str, int] = {}
            for pid, com, direccion, citas in arauco_rows:
                # Si el campo comuna ya es una localidad, usarla directo
                if com in ("CARAMPANGUE",): loc = "Carampangue"
                elif com in ("LARAQUETE",): loc = "Laraquete"
                elif com in ("RAMADILLAS",): loc = "Ramadillas"
                elif com in ("TUBUL",): loc = "Tubul"
                elif com in ("LLICO",): loc = "Llico"
                elif com in ("COLICO", "CÓLICO"): loc = "Colico"
                else:
                    # Comuna = ARAUCO: deducir por dirección
                    d = direccion or ""
                    if "carampangue" in d or "conumo" in d or "horcones" in d or "pichilo" in d:
                        loc = "Carampangue"
                    elif "laraquete" in d or "el bosque" in d:
                        loc = "Laraquete"
                    elif "ramadillas" in d or "ramadilla" in d:
                        loc = "Ramadillas"
                    elif "tubul" in d:
                        loc = "Tubul"
                    elif "llico" in d:
                        loc = "Llico"
                    elif "colico" in d or "cólico" in d:
                        loc = "Colico"
                    else:
                        loc = "Arauco"  # urbano por defecto
                local_pac.setdefault(loc, set()).add(pid)
                local_cit[loc] = local_cit.get(loc, 0) + citas
            for loc, pids in local_pac.items():
                grouped_f[loc] = {
                    "comuna": loc,
                    "pacientes": len(pids),
                    "citas": local_cit.get(loc, 0),
                    "es_localidad_arauco": True,
                }

        total_pac_g = sum(g["pacientes"] for g in grouped_f.values()) or 1
        total_cit_g = sum(g["citas"] for g in grouped_f.values()) or 1
        comunas_f = sorted(grouped_f.values(), key=lambda x: x["pacientes"], reverse=True)
        for c in comunas_f:
            c["pct"] = round(c["pacientes"] / total_pac_g * 100, 1)
            c["pct_citas"] = round(c["citas"] / total_cit_g * 100, 1)

        # Lista de profesionales (independiente del filtro, para popular el select)
        conn2 = sqlite3.connect(str(db_path))
        try:
            prof_list = [
                {"nombre": r[0].strip(), "citas": r[1]}
                for r in conn2.execute("""
                    SELECT nombre_profesional, COUNT(*) AS n
                    FROM citas_heatmap
                    WHERE nombre_profesional IS NOT NULL AND nombre_profesional != ''
                    GROUP BY nombre_profesional ORDER BY n DESC
                """).fetchall() if r[0]
            ]
            # Serie mensual FILTRADA (con cláusula de fechas + profesional)
            serie_mensual_f = []
            serie_q = f"""
                WITH primera AS (
                    SELECT id_paciente, MIN(fecha) AS f_primera
                    FROM citas_heatmap c
                    WHERE id_paciente IS NOT NULL{clause}
                    GROUP BY id_paciente
                ),
                mensual AS (
                    SELECT substr(c.fecha,1,7) AS mes,
                           COUNT(*) AS citas,
                           COUNT(DISTINCT c.id_paciente) AS pac_unicos
                    FROM citas_heatmap c
                    WHERE 1=1{clause}
                    GROUP BY mes
                ),
                nuevos AS (
                    SELECT substr(f_primera,1,7) AS mes, COUNT(*) AS pac_nuevos
                    FROM primera GROUP BY mes
                )
                SELECT m.mes, m.citas, m.pac_unicos, COALESCE(n.pac_nuevos, 0)
                FROM mensual m LEFT JOIN nuevos n ON n.mes = m.mes
                ORDER BY m.mes
            """
            # clause aparece 2 veces en serie_q → params duplicados
            serie_params = params + params
            acum = 0
            for mes, cit, uni, nuv in conn2.execute(serie_q, serie_params).fetchall():
                acum += nuv
                serie_mensual_f.append({
                    "mes": mes, "citas": cit, "pacientes_unicos": uni,
                    "pacientes_nuevos": nuv, "pacientes_acumulado": acum,
                })
        finally:
            conn2.close()

        return {
            "fuente": "heatmap_sqlite_filtrado",
            "actualizado": Path(files[0]).stat().st_mtime if files else 0,
            "rango": {"desde": fecha_desde, "hasta": fecha_hasta} if (fecha_desde or fecha_hasta) else None,
            "fecha_desde": fecha_desde,
            "fecha_hasta": fecha_hasta,
            "periodo": periodo if not (desde or hasta) else None,
            "filtro_profesional": profesional or None,
            "profesionales": prof_list,
            "serie_mensual": serie_mensual_f if serie_mensual_f else serie_mensual,
            "total_citas": tot_cit,
            "pacientes_unicos": tot_pac,
            "con_comuna": tot_pac - sin_com,
            "sin_comuna": sin_com,
            "comunas": _enriquecer_comunas(comunas_f[:12]),
            "filtrado": True,
        }

    # Lista de profesionales (para popular dropdown)
    prof_list = []
    # Comunas calculadas del SQLite histórico (no del JSON snapshot del último mes)
    sqlite_comunas: list[dict] = []
    if db_path.exists():
        conn3 = sqlite3.connect(str(db_path))
        try:
            prof_list = [
                {"nombre": r[0].strip(), "citas": r[1]}
                for r in conn3.execute("""
                    SELECT nombre_profesional, COUNT(*) AS n
                    FROM citas_heatmap
                    WHERE nombre_profesional IS NOT NULL AND nombre_profesional != ''
                    GROUP BY nombre_profesional ORDER BY n DESC
                """).fetchall() if r[0]
            ]
            # Comunas (sin filtro) desde SQLite + expansión Arauco por dirección
            comunas_rows = conn3.execute("""
                SELECT TRIM(COALESCE(p.comuna, '')) AS comuna,
                       COUNT(DISTINCT c.id_paciente) AS pacientes,
                       COUNT(*) AS citas
                FROM citas_heatmap c
                INNER JOIN pacientes_heatmap p ON p.id = c.id_paciente
                WHERE c.id_paciente IS NOT NULL
                GROUP BY comuna
            """).fetchall()
            grouped_full: dict[str, dict] = {}
            sin_com_full = 0
            arauco_buckets = []
            for nombre_raw, pac, cit in comunas_rows:
                nombre = (nombre_raw or "").strip().upper()
                if (not nombre or nombre.isdigit() or len(nombre) < 3
                        or any(x in nombre for x in ("VOLCAN", "CALLE", "PASAJE", "#"))):
                    sin_com_full += pac
                    continue
                if nombre in ("ARAUCO", "LARAQUETE", "RAMADILLAS", "CARAMPANGUE",
                              "TUBUL", "LLICO", "COLICO", "CÓLICO",
                              "PUNTA LAVAPIE", "PUNTA LAVAPIÉ", "ARAUUCO", "ARA"):
                    arauco_buckets.append((nombre, pac, cit))
                    continue
                if re.match(r"^ARAU[CU]+O?\s*-?$", nombre):
                    arauco_buckets.append((nombre, pac, cit))
                    continue
                canonical = nombre
                for pat, target in NORMALIZE.items():
                    if re.match(pat, nombre):
                        canonical = target
                        break
                if canonical in grouped_full:
                    grouped_full[canonical]["pacientes"] += pac
                    grouped_full[canonical]["citas"] += cit
                else:
                    grouped_full[canonical] = {"comuna": canonical, "pacientes": pac, "citas": cit}
            # Expandir Arauco en localidades por dirección
            if arauco_buckets:
                arauco_rows = conn3.execute("""
                    SELECT c.id_paciente AS pid,
                           UPPER(TRIM(p.comuna)) AS com,
                           LOWER(COALESCE(p.direccion,'')) AS dir,
                           COUNT(*) AS citas
                    FROM citas_heatmap c
                    INNER JOIN pacientes_heatmap p ON p.id = c.id_paciente
                    WHERE c.id_paciente IS NOT NULL
                      AND (UPPER(TRIM(p.comuna)) IN ('ARAUCO','ARAUUCO','ARA','LARAQUETE','RAMADILLAS','CARAMPANGUE','TUBUL','LLICO','COLICO','CÓLICO')
                           OR UPPER(TRIM(p.comuna)) LIKE 'ARAUCO%')
                    GROUP BY c.id_paciente
                """).fetchall()
                local_pac: dict[str, set] = {}
                local_cit: dict[str, int] = {}
                for pid, com, direccion, citas in arauco_rows:
                    if com == "CARAMPANGUE": loc = "Carampangue"
                    elif com == "LARAQUETE": loc = "Laraquete"
                    elif com == "RAMADILLAS": loc = "Ramadillas"
                    elif com == "TUBUL": loc = "Tubul"
                    elif com == "LLICO": loc = "Llico"
                    elif com in ("COLICO", "CÓLICO"): loc = "Colico"
                    else:
                        d = direccion or ""
                        if "carampangue" in d or "conumo" in d or "horcones" in d or "pichilo" in d:
                            loc = "Carampangue"
                        elif "laraquete" in d or "el bosque" in d:
                            loc = "Laraquete"
                        elif "ramadillas" in d or "ramadilla" in d:
                            loc = "Ramadillas"
                        elif "tubul" in d:
                            loc = "Tubul"
                        elif "llico" in d:
                            loc = "Llico"
                        elif "colico" in d or "cólico" in d:
                            loc = "Colico"
                        else:
                            loc = "Arauco"
                    local_pac.setdefault(loc, set()).add(pid)
                    local_cit[loc] = local_cit.get(loc, 0) + citas
                for loc, pids in local_pac.items():
                    grouped_full[loc] = {"comuna": loc, "pacientes": len(pids),
                                         "citas": local_cit.get(loc, 0),
                                         "es_localidad_arauco": True}
            tot_pac_full = sum(g["pacientes"] for g in grouped_full.values()) or 1
            tot_cit_full = sum(g["citas"] for g in grouped_full.values()) or 1
            sqlite_comunas = sorted(grouped_full.values(), key=lambda x: x["pacientes"], reverse=True)
            for c in sqlite_comunas:
                c["pct"] = round(c["pacientes"] / tot_pac_full * 100, 1)
                c["pct_citas"] = round(c["citas"] / tot_cit_full * 100, 1)
        finally:
            conn3.close()

    return {
        "fuente": "heatmap_sqlite_live",
        "actualizado": Path(files[0]).stat().st_mtime,
        "archivo": Path(files[0]).name,
        "periodo_label": raw.get("periodo"),
        "rango": rango,
        "filtro_profesional": None,
        "profesionales": prof_list,
        "serie_mensual": serie_mensual,
        # Totales LIVE del SQLite (no del JSON snapshot que se queda viejo)
        "total_citas": sqlite_total_citas if 'sqlite_total_citas' in dir() else raw.get("total_citas"),
        "pacientes_unicos": sqlite_total_pac if 'sqlite_total_pac' in dir() else raw.get("pacientes_unicos"),
        "con_comuna": sqlite_con_comuna if 'sqlite_con_comuna' in dir() else raw.get("con_comuna"),
        "sin_comuna": (sqlite_total_pac - sqlite_con_comuna) if 'sqlite_total_pac' in dir() else raw.get("sin_comuna"),
        "comunas": _enriquecer_comunas(sqlite_comunas[:12] if sqlite_comunas else comunas[:12]),
    }


# ── Cross-sell helpers ───────────────────────────────────────────────────
HIST_PROFESIONALES = {
    64: {"nombre": "Dr. Claudio Barraza", "especialidad": "Traumatología"},
}

# Precio promedio particular por especialidad (CLP). Se usa para estimar
# el ingreso generado por un paciente. Fuente: SYSTEM_PROMPT del chatbot.
PRECIOS_ESPECIALIDAD = {
    "Medicina General":          25000,
    "Medicina Familiar":         25000,
    "Otorrinolaringología":      35000,
    "Cardiología":               40000,
    "Ginecología":               30000,
    "Gastroenterología":         40000,
    "Odontología General":       35000,
    "Ortodoncia":                30000,
    "Endodoncia":               150000,
    "Implantología":            650000,
    "Estética Facial":           80000,
    "Masoterapia":               20000,
    "Kinesiología":              20000,
    "Nutrición":                 20000,
    "Psicología Adulto":         20000,
    "Psicología Infantil":       20000,
    "Fonoaudiología":            35000,
    "Matrona":                   30000,
    "Podología":                 25000,
    "Ecografía":                 40000,
    "Traumatología":             35000,
}


def _periodo_to_fecha_desde(periodo: str) -> str | None:
    """Convierte un periodo label en una fecha mínima YYYY-MM-DD (None = todos)."""
    from datetime import datetime, timedelta
    hoy = datetime.now(ZoneInfo("America/Santiago")).date()
    if periodo == "hoy":
        return hoy.isoformat()
    if periodo == "semana":
        return (hoy - timedelta(days=7)).isoformat()
    if periodo == "mes":
        return (hoy - timedelta(days=30)).isoformat()
    if periodo == "año" or periodo == "anio" or periodo == "year":
        return (hoy - timedelta(days=365)).isoformat()
    return None  # todos


def _resolver_rango(periodo: str | None, desde: str | None, hasta: str | None) -> tuple[str | None, str | None]:
    """Devuelve (fecha_desde, fecha_hasta). Rango explícito gana sobre preset."""
    import re
    valido = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    d = desde if desde and valido.match(desde) else None
    h = hasta if hasta and valido.match(hasta) else None
    if d or h:
        return d, h
    return _periodo_to_fecha_desde(periodo or "todos"), None


@app.get("/api/seo/cruces")
def seo_cruces_api(periodo: str = "todos", desde: str | None = None,
                   hasta: str | None = None, token: str = "",
                   cmc_session: str | None = Cookie(None)):
    _seo_api_auth(token, cmc_session)
    """Cruce de pacientes entre profesionales.

    Para cada profesional A, lista los profesionales B con los que comparte
    pacientes, ordenado por # pacientes en común. Sirve al tab "Cruces" del
    dashboard SEO para detectar oportunidades de cross-sell.

    `periodo` ∈ {hoy, semana, mes, año, todos}. Si se pasan `desde`/`hasta`
    en YYYY-MM-DD, anulan el preset.
    """
    import sqlite3
    from medilink import PROFESIONALES as _PROFS_BOOKING
    from pathlib import Path

    PROFESIONALES = {**HIST_PROFESIONALES, **_PROFS_BOOKING}
    fecha_desde, fecha_hasta = _resolver_rango(periodo, desde, hasta)

    db_path = Path(__file__).parent.parent / "data" / "heatmap_cache.db"
    if not db_path.exists():
        return {"error": "no cache"}

    # Construye filtro de fecha y parámetros como strings/binds
    fecha_clause = ""
    params: tuple = ()
    if fecha_desde and fecha_hasta:
        fecha_clause = " AND fecha BETWEEN ? AND ?"
        params = (fecha_desde, fecha_hasta)
    elif fecha_desde:
        fecha_clause = " AND fecha >= ?"
        params = (fecha_desde,)
    elif fecha_hasta:
        fecha_clause = " AND fecha <= ?"
        params = (fecha_hasta,)

    conn = sqlite3.connect(str(db_path))
    try:
        # Pacientes y atenciones por profesional (en el periodo)
        pac_por_prof: dict[int, int] = {}
        cit_por_prof: dict[int, int] = {}
        for pid, pac, cit in conn.execute(
            f"SELECT id_profesional, COUNT(DISTINCT id_paciente), COUNT(*) "
            f"FROM citas_heatmap WHERE id_profesional IS NOT NULL "
            f"AND id_paciente IS NOT NULL{fecha_clause} GROUP BY id_profesional",
            params,
        ).fetchall():
            pac_por_prof[pid] = pac
            cit_por_prof[pid] = cit

        # Cruces direccionales: (A, B, # pacientes que se atienden con ambos)
        cruces_raw = conn.execute(f"""
            SELECT a.id_profesional, b.id_profesional, COUNT(DISTINCT a.id_paciente)
            FROM citas_heatmap a
            JOIN citas_heatmap b
              ON a.id_paciente = b.id_paciente
             AND a.id_profesional != b.id_profesional
            WHERE a.id_profesional IS NOT NULL
              AND b.id_profesional IS NOT NULL
              AND a.id_paciente IS NOT NULL
              {fecha_clause.replace('fecha', 'a.fecha')}
              {fecha_clause.replace('fecha', 'b.fecha')}
            GROUP BY a.id_profesional, b.id_profesional
        """, params + params).fetchall()

        # Pacientes con >1 profesional distinto + atenciones de esos pacientes
        row = conn.execute(f"""
            SELECT COUNT(*), COALESCE(SUM(citas), 0)
            FROM (
                SELECT id_paciente, COUNT(*) AS citas
                FROM citas_heatmap
                WHERE id_paciente IS NOT NULL AND id_profesional IS NOT NULL{fecha_clause}
                GROUP BY id_paciente
                HAVING COUNT(DISTINCT id_profesional) > 1
            )
        """, params).fetchone()
        pac_multi = row[0] if row else 0
        atenciones_multi = row[1] if row else 0

        # Pacientes con >1 ESPECIALIDAD distinta (cross-sell verdadero)
        prof_especs_rows = conn.execute(
            f"SELECT id_paciente, id_profesional FROM citas_heatmap "
            f"WHERE id_paciente IS NOT NULL AND id_profesional IS NOT NULL{fecha_clause}",
            params,
        ).fetchall()

        total_pac = conn.execute(
            f"SELECT COUNT(DISTINCT id_paciente) FROM citas_heatmap "
            f"WHERE id_paciente IS NOT NULL{fecha_clause}",
            params,
        ).fetchone()[0]
        total_citas = conn.execute(
            f"SELECT COUNT(*) FROM citas_heatmap "
            f"WHERE id_paciente IS NOT NULL AND id_profesional IS NOT NULL{fecha_clause}",
            params,
        ).fetchone()[0]
    finally:
        conn.close()

    # Índices paciente↔profesional usados por todos los KPIs siguientes
    pac_profs_set: dict[int, set] = {}
    pac_prof_citas: dict[tuple, int] = {}
    prof_to_pacs: dict[int, set] = {}
    for pid, prof in prof_especs_rows:
        if prof not in PROFESIONALES:
            continue
        pac_profs_set.setdefault(pid, set()).add(prof)
        pac_prof_citas[(pid, prof)] = pac_prof_citas.get((pid, prof), 0) + 1
        prof_to_pacs.setdefault(prof, set()).add(pid)

    # Profesionales activos (con al menos 1 paciente en el periodo)
    profesionales = []
    for pid, info in PROFESIONALES.items():
        n = pac_por_prof.get(pid, 0)
        if n == 0:
            continue
        cit = cit_por_prof.get(pid, 0)
        precio = PRECIOS_ESPECIALIDAD.get(info["especialidad"], 25000)
        profesionales.append({
            "id": pid,
            "nombre": info["nombre"],
            "especialidad": info["especialidad"],
            "pacientes": n,
            "atenciones": cit,
            "monto_estimado": cit * precio,
        })
    profesionales.sort(key=lambda x: x["atenciones"], reverse=True)

    # Cruces agrupados por profesional A
    cruces: dict[str, list] = {}
    for prof_a, prof_b, comunes in cruces_raw:
        if prof_a not in PROFESIONALES or prof_b not in PROFESIONALES:
            continue
        n_a = pac_por_prof.get(prof_a, 0)
        if n_a == 0:
            continue
        # Pacientes que comparten A y B
        comunes_pids = prof_to_pacs.get(prof_a, set()) & prof_to_pacs.get(prof_b, set())
        # Atenciones que el cruzado (B) generó con esos pacientes
        atenciones_b_cross = sum(pac_prof_citas.get((pid, prof_b), 0) for pid in comunes_pids)
        atenciones_a_cross = sum(pac_prof_citas.get((pid, prof_a), 0) for pid in comunes_pids)
        precio_b = PRECIOS_ESPECIALIDAD.get(PROFESIONALES[prof_b]["especialidad"], 25000)
        precio_a = PRECIOS_ESPECIALIDAD.get(PROFESIONALES[prof_a]["especialidad"], 25000)
        cruces.setdefault(str(prof_a), []).append({
            "id": prof_b,
            "nombre": PROFESIONALES[prof_b]["nombre"],
            "especialidad": PROFESIONALES[prof_b]["especialidad"],
            "comunes": comunes,
            "pct": round(comunes / n_a * 100, 1),
            "atenciones_cruzado": atenciones_b_cross,
            "monto_cruzado": atenciones_b_cross * precio_b,
            "atenciones_derivador": atenciones_a_cross,
            "monto_derivador": atenciones_a_cross * precio_a,
        })
    for lista in cruces.values():
        lista.sort(key=lambda x: x["monto_cruzado"], reverse=True)

    # Top pares globales (sin duplicar A↔B)
    seen = set()
    top_pares = []
    for prof_a, prof_b, comunes in sorted(cruces_raw, key=lambda x: x[2], reverse=True):
        if prof_a not in PROFESIONALES or prof_b not in PROFESIONALES:
            continue
        key = tuple(sorted([prof_a, prof_b]))
        if key in seen:
            continue
        seen.add(key)
        top_pares.append({
            "a_id": key[0],
            "a": PROFESIONALES[key[0]]["nombre"],
            "esp_a": PROFESIONALES[key[0]]["especialidad"],
            "b_id": key[1],
            "b": PROFESIONALES[key[1]]["nombre"],
            "esp_b": PROFESIONALES[key[1]]["especialidad"],
            "comunes": comunes,
            "misma_esp": PROFESIONALES[key[0]]["especialidad"] == PROFESIONALES[key[1]]["especialidad"],
        })
        if len(top_pares) >= 30:
            break

    # ── KPIs cross-sell por especialidad ─────────────────────────────────
    # Mapeo paciente → set(especialidades) y citas por (paciente, especialidad)
    pac_esps: dict[int, set] = {}
    pac_citas: dict[int, int] = {}
    pares_esp_count: dict[tuple, int] = {}
    citas_por_esp: dict[str, int] = {}
    pac_por_esp: dict[str, set] = {}
    for pid, prof in prof_especs_rows:
        if prof not in PROFESIONALES:
            continue
        esp = PROFESIONALES[prof]["especialidad"]
        pac_esps.setdefault(pid, set()).add(esp)
        pac_citas[pid] = pac_citas.get(pid, 0) + 1
        citas_por_esp[esp] = citas_por_esp.get(esp, 0) + 1
        pac_por_esp.setdefault(esp, set()).add(pid)

    pac_multi_esp = sum(1 for s in pac_esps.values() if len(s) > 1)
    atenciones_multi_esp = sum(c for pid, c in pac_citas.items() if len(pac_esps.get(pid, set())) > 1)

    # Cross-sell INTRA-especialidad: paciente con ≥2 profesionales de la misma esp
    # (ej. paciente que ve a Olavarría Y a Márquez — ambos Medicina General)
    pac_intra = 0
    for pid, profs in pac_profs_set.items():
        esps_counts: dict[str, int] = {}
        for prof in profs:
            esp = PROFESIONALES[prof]["especialidad"]
            esps_counts[esp] = esps_counts.get(esp, 0) + 1
        if any(n > 1 for n in esps_counts.values()):
            pac_intra += 1
    pct_intra = round(pac_intra / total_pac * 100, 1) if total_pac else 0

    # Top pares intra-especialidad — recorre cruces_raw completo (no solo top 30)
    seen_intra = set()
    pares_intra = []
    for prof_a, prof_b, comunes in sorted(cruces_raw, key=lambda x: x[2], reverse=True):
        if prof_a not in PROFESIONALES or prof_b not in PROFESIONALES:
            continue
        if PROFESIONALES[prof_a]["especialidad"] != PROFESIONALES[prof_b]["especialidad"]:
            continue
        key = tuple(sorted([prof_a, prof_b]))
        if key in seen_intra:
            continue
        seen_intra.add(key)
        pares_intra.append({
            "a": PROFESIONALES[key[0]]["nombre"],
            "b": PROFESIONALES[key[1]]["nombre"],
            "especialidad": PROFESIONALES[key[0]]["especialidad"],
            "comunes": comunes,
        })
        if len(pares_intra) >= 15:
            break

    # Pares de especialidades (no profesionales) — cross-sell real
    for pid, esps in pac_esps.items():
        if len(esps) < 2:
            continue
        esp_list = sorted(esps)
        for i in range(len(esp_list)):
            for j in range(i + 1, len(esp_list)):
                key = (esp_list[i], esp_list[j])
                pares_esp_count[key] = pares_esp_count.get(key, 0) + 1

    top_pares_esp = sorted(
        [{"esp_a": k[0], "esp_b": k[1], "pacientes": v} for k, v in pares_esp_count.items()],
        key=lambda x: x["pacientes"], reverse=True
    )[:15]

    # Cross-sell ratio por especialidad: % de pacientes de esp X que también consumen otra especialidad
    cross_sell_esp = []
    for esp, pacs in pac_por_esp.items():
        n = len(pacs)
        cruzaron = sum(1 for pid in pacs if len(pac_esps.get(pid, set())) > 1)
        cross_sell_esp.append({
            "especialidad": esp,
            "pacientes": n,
            "cruzaron": cruzaron,
            "pct_cross": round(cruzaron / n * 100, 1) if n else 0,
        })
    cross_sell_esp.sort(key=lambda x: x["pacientes"], reverse=True)

    promedio_profs = round(
        sum(len(pac_esps.get(pid, set())) for pid in pac_esps) / len(pac_esps), 2
    ) if pac_esps else 0

    return {
        "periodo": periodo,
        "fecha_desde": fecha_desde,
        "fecha_hasta": fecha_hasta,
        "total_pacientes": total_pac,
        "total_atenciones": total_citas,
        "pacientes_multi_profesional": pac_multi,
        "pct_multi": round(pac_multi / total_pac * 100, 1) if total_pac else 0,
        "atenciones_multi_profesional": atenciones_multi,
        "pct_atenciones_cross": round(atenciones_multi / total_citas * 100, 1) if total_citas else 0,
        "pacientes_multi_especialidad": pac_multi_esp,
        "pct_multi_esp": round(pac_multi_esp / total_pac * 100, 1) if total_pac else 0,
        "atenciones_multi_especialidad": atenciones_multi_esp,
        "pct_atenciones_cross_esp": round(atenciones_multi_esp / total_citas * 100, 1) if total_citas else 0,
        "pacientes_intra_especialidad": pac_intra,
        "pct_intra_esp": pct_intra,
        "promedio_especialidades_por_paciente": promedio_profs,
        "cross_sell_por_especialidad": cross_sell_esp,
        "top_pares_especialidad": top_pares_esp,
        "top_pares_intra_especialidad": pares_intra,
        "profesionales": profesionales,
        "cruces": cruces,
        "top_pares": top_pares,
    }


@app.get("/api/seo/meta")
def seo_meta_api(dias: int = 30, token: str = "",
                 cmc_session: str | None = Cookie(None)):
    _seo_api_auth(token, cmc_session)
    """KPIs estilo Meta Business Suite calculados sobre los datos locales del bot.

    Incluye volumen de conversaciones, captación de pacientes, conversión a citas,
    distribución por canal (WA/IG/FB), calidad de entrega y engagement de
    templates de fidelización. Ventana configurable por query param `dias`.
    """
    from session import _conn
    from datetime import datetime, timedelta

    dias = max(1, min(int(dias), 365))
    desde_dt = datetime.now() - timedelta(days=dias)
    desde = desde_dt.strftime("%Y-%m-%d %H:%M:%S")

    conn = _conn()
    try:
        # ── Volumen + captación ──────────────────────────────────────────
        msg_in = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE direction='in' AND ts >= ?", (desde,)
        ).fetchone()[0]
        msg_out = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE direction='out' AND ts >= ?", (desde,)
        ).fetchone()[0]
        convers_unicas = conn.execute(
            "SELECT COUNT(DISTINCT phone) FROM messages WHERE direction='in' AND ts >= ?", (desde,)
        ).fetchone()[0]
        # Pacientes nuevos: primer mensaje 'in' cae dentro del período
        pacientes_nuevos = conn.execute(
            "SELECT COUNT(*) FROM (SELECT phone, MIN(ts) AS first FROM messages "
            "WHERE direction='in' GROUP BY phone HAVING first >= ?)", (desde,)
        ).fetchone()[0]

        # ── Por canal ────────────────────────────────────────────────────
        canal_rows = conn.execute("""
            SELECT COALESCE(canal,'whatsapp') AS canal,
                   COUNT(*) FILTER (WHERE direction='in')  AS msg_in,
                   COUNT(*) FILTER (WHERE direction='out') AS msg_out,
                   COUNT(DISTINCT phone) AS phones
            FROM messages WHERE ts >= ?
            GROUP BY COALESCE(canal,'whatsapp')
        """, (desde,)).fetchall()
        por_canal = [
            {"canal": r["canal"], "msg_in": r["msg_in"], "msg_out": r["msg_out"], "phones": r["phones"]}
            for r in canal_rows
        ]

        # ── Conversión: citas agendadas por el bot en el período ─────────
        citas_agend = conn.execute(
            "SELECT COUNT(*) FROM citas_bot WHERE created_at >= ?", (desde,)
        ).fetchone()[0]
        citas_por_canal = conn.execute("""
            SELECT COALESCE(m.canal,'whatsapp') AS canal, COUNT(DISTINCT cb.id) AS citas
            FROM citas_bot cb
            LEFT JOIN messages m ON m.phone = cb.phone
            WHERE cb.created_at >= ?
            GROUP BY COALESCE(m.canal,'whatsapp')
        """, (desde,)).fetchall()
        citas_canal_map = {r["canal"]: r["citas"] for r in citas_por_canal}
        for c in por_canal:
            c["citas"] = citas_canal_map.get(c["canal"], 0)
            c["pct_conv"] = round(c["citas"] / c["phones"] * 100, 1) if c["phones"] else 0

        citas_top_esp = conn.execute("""
            SELECT especialidad, COUNT(*) AS n FROM citas_bot
            WHERE created_at >= ? AND especialidad IS NOT NULL AND especialidad != ''
            GROUP BY especialidad ORDER BY n DESC LIMIT 8
        """, (desde,)).fetchall()
        top_especialidades = [{"especialidad": r["especialidad"], "n": r["n"]} for r in citas_top_esp]

        # ── Funnel de agendamiento ───────────────────────────────────────
        # Estado al final de cada conversación es difícil; aproximamos contando
        # estados visitados al menos una vez en messages (cada msg trae state)
        funnel_rows = conn.execute("""
            SELECT state, COUNT(DISTINCT phone) AS phones
            FROM messages WHERE ts >= ? AND state IS NOT NULL
            GROUP BY state
        """, (desde,)).fetchall()
        funnel_map = {r["state"]: r["phones"] for r in funnel_rows}
        funnel = [
            {"etapa": "Conversación iniciada", "phones": convers_unicas},
            {"etapa": "Eligió especialidad",   "phones": funnel_map.get("WAIT_SLOT", 0) + funnel_map.get("WAIT_MODALIDAD", 0)},
            {"etapa": "Eligió slot",           "phones": funnel_map.get("WAIT_MODALIDAD", 0) + funnel_map.get("CONFIRMING_CITA", 0)},
            {"etapa": "Confirmando cita",      "phones": funnel_map.get("CONFIRMING_CITA", 0)},
            {"etapa": "Cita reservada",        "phones": citas_agend},
        ]

        # ── Calidad de entrega (message_statuses) ────────────────────────
        ms_rows = conn.execute("""
            SELECT status, COUNT(*) AS n FROM message_statuses
            WHERE ts >= ? GROUP BY status
        """, (desde,)).fetchall()
        statuses = {r["status"]: r["n"] for r in ms_rows}
        total_status = sum(statuses.values()) or 1
        delivery = {
            "sent":      statuses.get("sent", 0),
            "delivered": statuses.get("delivered", 0),
            "read":      statuses.get("read", 0),
            "failed":    statuses.get("failed", 0),
            "total":     sum(statuses.values()),
            "pct_delivered": round(statuses.get("delivered", 0) / total_status * 100, 1),
            "pct_read":      round(statuses.get("read", 0)      / total_status * 100, 1),
            "pct_failed":    round(statuses.get("failed", 0)    / total_status * 100, 1),
        }

        # ── Engagement de templates de fidelización ──────────────────────
        tpl_rows = conn.execute("""
            SELECT tipo, COUNT(*) AS enviados,
                   SUM(CASE WHEN respuesta IS NOT NULL AND respuesta != '' THEN 1 ELSE 0 END) AS respondidos
            FROM fidelizacion_msgs
            WHERE enviado_en >= ?
            GROUP BY tipo ORDER BY enviados DESC
        """, (desde,)).fetchall()
        templates = []
        for r in tpl_rows:
            tasa = round(r["respondidos"] / r["enviados"] * 100, 1) if r["enviados"] else 0
            templates.append({"tipo": r["tipo"], "enviados": r["enviados"],
                              "respondidos": r["respondidos"], "pct_respuesta": tasa})

        # ── Serie temporal diaria ────────────────────────────────────────
        serie_rows = conn.execute("""
            SELECT substr(ts, 1, 10) AS dia,
                   COUNT(*) FILTER (WHERE direction='in')  AS msg_in,
                   COUNT(*) FILTER (WHERE direction='out') AS msg_out,
                   COUNT(DISTINCT phone) AS phones
            FROM messages WHERE ts >= ?
            GROUP BY dia ORDER BY dia
        """, (desde,)).fetchall()
        serie = [{"dia": r["dia"], "msg_in": r["msg_in"], "msg_out": r["msg_out"], "phones": r["phones"]}
                 for r in serie_rows]

        # Tasa de toma de control humana (HUMAN_TAKEOVER en eventos)
        try:
            human = conn.execute(
                "SELECT COUNT(DISTINCT phone) FROM conversation_events "
                "WHERE event LIKE '%takeover%' AND ts >= ?", (desde,)
            ).fetchone()[0]
        except Exception:
            human = 0

    finally:
        conn.close()

    pct_conv = round(citas_agend / convers_unicas * 100, 1) if convers_unicas else 0
    pct_humano = round(human / convers_unicas * 100, 1) if convers_unicas else 0

    return {
        "ventana_dias": dias,
        "desde": desde_dt.isoformat(),
        "msg_in": msg_in,
        "msg_out": msg_out,
        "conversaciones_unicas": convers_unicas,
        "pacientes_nuevos": pacientes_nuevos,
        "citas_agendadas": citas_agend,
        "pct_conversion": pct_conv,
        "tomas_humano": human,
        "pct_humano": pct_humano,
        "por_canal": sorted(por_canal, key=lambda x: x["phones"], reverse=True),
        "top_especialidades": top_especialidades,
        "funnel": funnel,
        "delivery": delivery,
        "templates": templates,
        "serie": serie,
    }


@app.get("/api/seo/cruce-pacientes")
def seo_cruce_pacientes_api(prof_a: int, prof_b: int, periodo: str = "todos",
                             desde: str | None = None, hasta: str | None = None,
                             token: str = "",
                             cmc_session: str | None = Cookie(None)):
    _seo_api_auth(token, cmc_session)
    """Lista de pacientes que se atienden con prof_a Y prof_b en el periodo.

    Devuelve nombre, RUT, # citas con cada profesional, $ estimado por
    cada uno y total. Usado para drill-down del tab Cruces.
    """
    import sqlite3
    from medilink import PROFESIONALES as _PROFS_BOOKING
    from pathlib import Path

    PROFESIONALES = {**HIST_PROFESIONALES, **_PROFS_BOOKING}
    fecha_desde, fecha_hasta = _resolver_rango(periodo, desde, hasta)
    if prof_a not in PROFESIONALES or prof_b not in PROFESIONALES:
        return {"error": "profesional no reconocido"}

    info_a = PROFESIONALES[prof_a]
    info_b = PROFESIONALES[prof_b]
    precio_a = PRECIOS_ESPECIALIDAD.get(info_a["especialidad"], 25000)
    precio_b = PRECIOS_ESPECIALIDAD.get(info_b["especialidad"], 25000)

    db_path = Path(__file__).parent.parent / "data" / "heatmap_cache.db"
    if not db_path.exists():
        return {"error": "no cache"}

    if fecha_desde and fecha_hasta:
        fecha_clause = " AND fecha BETWEEN ? AND ?"
        base_params: list = [fecha_desde, fecha_hasta]
    elif fecha_desde:
        fecha_clause = " AND fecha >= ?"
        base_params = [fecha_desde]
    elif fecha_hasta:
        fecha_clause = " AND fecha <= ?"
        base_params = [fecha_hasta]
    else:
        fecha_clause = ""
        base_params = []

    conn = sqlite3.connect(str(db_path))
    try:
        # Pacientes que tienen ≥1 cita con A Y ≥1 cita con B
        rows = conn.execute(
            f"""
            SELECT p.id, COALESCE(p.nombre,'') || ' ' || COALESCE(p.apellidos,'') AS nombre,
                   p.rut, p.comuna, p.celular,
                   (SELECT COUNT(*) FROM citas_heatmap c
                    WHERE c.id_paciente = p.id AND c.id_profesional = ?{fecha_clause}) AS cit_a,
                   (SELECT COUNT(*) FROM citas_heatmap c
                    WHERE c.id_paciente = p.id AND c.id_profesional = ?{fecha_clause}) AS cit_b,
                   (SELECT MAX(fecha) FROM citas_heatmap c
                    WHERE c.id_paciente = p.id AND c.id_profesional IN (?, ?){fecha_clause}) AS ultima
            FROM pacientes_heatmap p
            WHERE p.id IN (
                SELECT id_paciente FROM citas_heatmap
                WHERE id_profesional = ?{fecha_clause}
            )
            AND p.id IN (
                SELECT id_paciente FROM citas_heatmap
                WHERE id_profesional = ?{fecha_clause}
            )
            ORDER BY (cit_a + cit_b) DESC
            """,
            [prof_a] + base_params  # cit_a subquery
            + [prof_b] + base_params  # cit_b subquery
            + [prof_a, prof_b] + base_params  # ultima subquery
            + [prof_a] + base_params  # outer A
            + [prof_b] + base_params,  # outer B
        ).fetchall()

        pacientes = []
        for pid, nombre, rut, comuna, celular, cit_a, cit_b, ultima in rows:
            monto_a = cit_a * precio_a
            monto_b = cit_b * precio_b
            pacientes.append({
                "id": pid,
                "nombre": nombre.strip() or "(sin nombre)",
                "rut": rut or "—",
                "comuna": comuna or "—",
                "celular": celular or "—",
                "citas_a": cit_a,
                "citas_b": cit_b,
                "monto_a": monto_a,
                "monto_b": monto_b,
                "monto_total": monto_a + monto_b,
                "ultima_cita": ultima or "—",
            })
    finally:
        conn.close()

    total_monto = sum(p["monto_total"] for p in pacientes)
    total_citas = sum(p["citas_a"] + p["citas_b"] for p in pacientes)

    return {
        "periodo": periodo,
        "fecha_desde": fecha_desde,
        "fecha_hasta": fecha_hasta,
        "prof_a": {"id": prof_a, "nombre": info_a["nombre"], "especialidad": info_a["especialidad"], "precio": precio_a},
        "prof_b": {"id": prof_b, "nombre": info_b["nombre"], "especialidad": info_b["especialidad"], "precio": precio_b},
        "pacientes": pacientes,
        "total_pacientes": len(pacientes),
        "total_atenciones": total_citas,
        "monto_total_estimado": total_monto,
    }


@app.get("/proyectos2026", response_class=HTMLResponse)
def proyectos2026_page():
    """Visualización Canvas 2D de CMC y Meulen como proyectos hermanos."""
    return _PROYECTOS2026_HTML


@app.get("/profesionalescmc", response_class=HTMLResponse)
def profesionales_cmc_page():
    """Dashboard de permisos del bot profesional CMC por profesional."""
    return _PROFESIONALES_CMC_HTML


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




def _sanitize_upload_filename(orig: str, fallback: str = "file") -> str:
    """Path traversal guard: solo basename, alfanumeric/dot/dash, max 120 chars."""
    import os, re
    base = os.path.basename(orig or "")
    safe = re.sub(r"[^\w.\-]", "_", base)[:120]
    return safe or fallback

@app.post("/webhook")
async def webhook(request: Request):
    """Recibe mensajes de Meta Cloud API (WhatsApp, Instagram, Messenger).

    Si META_APP_SECRET está configurado, valida la firma X-Hub-Signature-256
    para evitar que un atacante envíe payloads falsos al endpoint público.
    Sin APP_SECRET, modo legacy (acepta todo) — recomendado configurarlo.
    """
    # Leer body raw primero para poder validar firma (json.loads consume el stream)
    body_bytes = await request.body()
    from config import META_APP_SECRET as _MAS
    if _MAS:
        sig_header = request.headers.get("x-hub-signature-256", "")
        if not sig_header.startswith("sha256="):
            log.warning("webhook firma faltante o malformada")
            return Response(status_code=403)
        import hmac as _hmac_w, hashlib as _hl_w
        expected = "sha256=" + _hmac_w.new(_MAS.encode(), body_bytes, _hl_w.sha256).hexdigest()
        if not _hmac_w.compare_digest(sig_header, expected):
            log.warning("webhook firma inválida")
            return Response(status_code=403)
    try:
        import json as _json_w
        data = _json_w.loads(body_bytes.decode() or "{}")
    except Exception:
        return Response(status_code=200)
    if not isinstance(data, dict):
        return Response(status_code=200)
    obj = data.get("object", "")

    # ── Helper: convertir mensaje interactivo WA a texto plano ──────────────
    _SOCIAL_PROMO = (
        "\n\n✨ *Nutricionista bono Fonasa $4.680*\n"
        "😁 *Ortodoncia:* completa $120.000 / controles $30.000"
    )

    def _interactive_to_text(resp: dict, include_promo: bool = False) -> str:
        """Convierte un mensaje interactivo a texto plano.
        Se usa para:
         - IG/FB outbound (WhatsApp interactive no aplica) → include_promo=True
         - Logging de mensajes WA en messages.text → include_promo=False (la
           recepcionista necesita ver header + opciones tal como las ve el paciente)
        """
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
            is_menu = "¿Qué necesitas hoy?" in body
            promo = _SOCIAL_PROMO if (is_menu and include_promo) else ""
            return f"{body}{promo}\n\n{items}" if items else body + promo
        return body

    # ── Helper: procesar mensaje de IG/FB con el chatbot ─────────────────────
    async def _process_social(phone: str, sender_id: str, texto: str,
                              canal: str, send_fn):
        """Procesa un mensaje de IG/FB usando handle_message y responde."""
        from resilience import get_phone_lock
        async with get_phone_lock(phone):
            session = get_session(phone)
            state_before = session.get("state", "IDLE")
            log_message(phone, "in", texto, state_before, canal=canal)
            try:
                from session import try_autocapture_rut_name
                try_autocapture_rut_name(phone, texto)
            except Exception:
                pass
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
                resp_text = _interactive_to_text(respuesta, include_promo=True)
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
                    # Pasar token por Authorization header evita que httpx lo logee
                    # en la URL (seguridad: antes se filtraba en /var/log/cmc-bot.log)
                    r = await client.get(
                        f"https://graph.facebook.com/v22.0/{sender_id}",
                        params={"fields": fields},
                        headers={"Authorization": f"Bearer {token}"},
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
                        # 131047/51/52 = ventana 24h cerrada (esperado, no error).
                        # Admin personal tampoco es customer-facing issue.
                        _err_code = err.get("code") if err else None
                        try:
                            from config import ADMIN_ALERT_PHONE as _ADM
                        except Exception:
                            _ADM = ""
                        if _err_code in (131047, 131051, 131052) or recipient == _ADM:
                            log.info("MSG undelivered wamid=%s to=%s code=%s: %s",
                                     wamid, recipient, _err_code, err.get("title") if err else "")
                        else:
                            log.warning("MSG FAILED wamid=%s to=%s code=%s: %s",
                                        wamid, recipient, _err_code, err.get("title") if err else "")

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

        # Rate limit por phone Y por RUT (si lo conocemos): evita bypass rotando números
        _profile = get_profile(phone) or {}
        _rut = (_profile.get("rut") or "").strip()
        _rate_keys = (phone, f"rut:{_rut}" if _rut else "")
        if _rate_limited(*_rate_keys):
            # Ley 21.719: NO loguear RUT en claro en /var/log. Hash truncado para diagnóstico.
            import hashlib as _hl_rut
            _rut_log = _hl_rut.sha256(_rut.encode()).hexdigest()[:8] if _rut else "-"
            log.warning("Rate limit excedido WA phone=%s rut_hash=%s type=%s", phone, _rut_log, msg_type)
            return Response(status_code=200)

        # Extraer texto de mensajes de texto, respuestas interactivas o audio
        if msg_type == "text":
            texto = msg["text"]["body"].strip()
            if not texto:
                return Response(status_code=200)
            # Mensajes-ruido: solo signos de puntuación ("?", "??", "...", "!") o
            # emojis sueltos. No deben activar detect_intent ni generar un saludo
            # largo. Ignoramos silenciosamente.
            import re as _re_noise
            if _re_noise.fullmatch(r"[^\w\s]{1,10}", texto):
                log.info("noise msg ignored from=%s txt=%r", phone, texto)
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
            # Skip audios muy cortos (<~2s en opus ~20 kbps) — ruido, "hmm", respiraciones.
            # Evita pagar Whisper por audios sin contenido util.
            if len(audio_bytes) < 5000:
                log.info("AUDIO omitido (demasiado corto, %d bytes) from=%s", len(audio_bytes), phone)
                try:
                    log_event(phone, "savings:skip_whisper_short_audio", {"bytes": len(audio_bytes)})
                except Exception:
                    pass
                await send_whatsapp(
                    phone,
                    "Tu audio es muy cortito y no se entiende bien 😅\n"
                    "¿Puedes escribirlo o grabar uno un poco más largo?"
                )
                return Response(status_code=200)
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
                        from datetime import datetime as _dt
                        from zoneinfo import ZoneInfo as _ZI
                        ts = _dt.now(_ZI("America/Santiago")).strftime("%Y%m%d_%H%M%S")
                        _fallback_name = f"{msg_type}_{ts}{ext}"
                        saved_filename = _sanitize_upload_filename(orig_filename, fallback=_fallback_name)
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
                    # Procesar el texto extraído por el pipeline normal.
                    # Lock por phone: serializa procesamiento si llegan mensajes
                    # simultáneos del mismo paciente (evita doble respuesta en WAIT_SLOT).
                    from resilience import get_phone_lock
                    async with get_phone_lock(phone):
                        session = get_session(phone)
                        respuesta = await handle_message(phone, texto, session)
                        if respuesta:
                            if isinstance(respuesta, dict):
                                await send_whatsapp_interactive(phone, respuesta["interactive"])
                                # Log con texto completo (body + opciones) para que la
                                # recepcionista en /admin vea las mismas opciones que el paciente.
                                log_text = _interactive_to_text(respuesta, include_promo=False)
                                log_message(phone, "out", log_text, get_session(phone).get("state", "IDLE"), canal="whatsapp")
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
            # Dedupe: si el paciente manda varias imágenes/PDFs en ráfaga (ej. 3 fotos
            # seguidas), solo responder al PRIMERO dentro de una ventana de 60s.
            # Evita el spam "Recibí tu imagen × 3".
            import time as _time
            _now = _time.time()
            _last_ack_ts = (get_session(phone).get("data") or {}).get("_last_media_ack_ts", 0)
            try:
                _last_ack_ts = float(_last_ack_ts or 0)
            except Exception:
                _last_ack_ts = 0
            if _now - _last_ack_ts < 60:
                # Ya mandamos ack reciente — actualizar timestamp y no responder de nuevo
                _sess_curr = get_session(phone)
                _data_curr = _sess_curr.get("data") or {}
                if isinstance(_data_curr, str):
                    import json as _json
                    try: _data_curr = _json.loads(_data_curr)
                    except Exception: _data_curr = {}
                _data_curr["_last_media_ack_ts"] = _now
                save_session(phone, _sess_curr.get("state") or "HUMAN_TAKEOVER", _data_curr)
            else:
                reply = (
                    f"Recibí tu {label}, gracias.\n\n"
                    "Lo guardé en tu ficha y una recepcionista lo va a revisar 🙏\n"
                    "Si es urgente, puedes llamar al 📞 (41) 296 5226"
                )
                await send_whatsapp(phone, reply)
                log_message(phone, "out", reply, "HUMAN_TAKEOVER", canal="whatsapp")
                # Guardar timestamp del ack en session data
                _sess_curr = get_session(phone)
                _data_curr = _sess_curr.get("data") or {}
                if isinstance(_data_curr, str):
                    import json as _json
                    try: _data_curr = _json.loads(_data_curr)
                    except Exception: _data_curr = {}
                _data_curr["_last_media_ack_ts"] = _now
                save_session(phone, "HUMAN_TAKEOVER", _data_curr)
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

        from resilience import get_phone_lock
        async with get_phone_lock(phone):
            session = get_session(phone)
            state_before = session.get("state", "IDLE")
            log_text = f"🎤 {texto}" if is_audio else texto
            log_message(phone, "in", log_text, state_before, canal="whatsapp")
            try:
                from session import try_autocapture_rut_name
                try_autocapture_rut_name(phone, log_text)
            except Exception:
                pass

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
                # Log con texto completo (body + opciones) → la recepción ve lo mismo que el paciente.
                resp_text = _interactive_to_text(respuesta, include_promo=False)
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
                # Lock por phone: el mensaje principal ya liberó su lock al retornar,
                # pero si hay otro handler en vuelo del mismo paciente queremos serializar.
                from resilience import get_phone_lock
                async with get_phone_lock(_xphone):
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
