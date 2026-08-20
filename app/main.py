"""
Chatbot WhatsApp — Centro Médico Carampangue
Webhook de Meta Cloud API → FastAPI → Claude + Medilink
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

import asyncio
import logging
import logging.config
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
import re
from time import monotonic

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, Request, Response, Query, HTTPException, Cookie
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from config import (META_VERIFY_TOKEN, CMC_TELEFONO, CMC_TELEFONO_FIJO, ADMIN_TOKEN,
                    OLACORE_TOKEN, ALMA_PROFILES, ALMA_MODULE_REGISTRY,
                    MEDILINK_TOKEN, META_AD_ACCOUNT_ID as _CFG_META_ACCOUNT_ID,
                    ASISTENTE_EXAMENES_ENABLED, STAFF_PHONES,
                    MEULEN_ASSISTANT_ACTIVE, MEULEN_ASSISTANT_PHONES,
                    MEULEN_ASSISTANT_URL, MEULEN_ASSISTANT_SECRET,
                    ADKUN_ASSISTANT_ACTIVE, ADKUN_ASSISTANT_PHONES)
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
from resilience import is_medilink_down, is_claude_down, claude_down_reason
from medilink import MedilinkRateLimited, MedilinkInactiva
import medilink_outage
from jobs import (_enviar_reenganche, _sync_citas_hoy, _job_learned_skills,
                  _job_verificar_intervalos, _job_agenda_dias_sync,
                  _job_recordatorios, _job_recordatorios_2h, _job_recordatorios_48h,
                  _job_postconsulta, _job_postconsulta_morning,
                  _job_enrolar_atendidos_dia,
                  _job_detectar_cancelaciones,
                  _job_monitor_anomalias,
                  _job_reactivacion, _job_abarca_sync, _job_olavarria_sync,
                  _job_bi_sync_diario, _job_bi_sync_intradia, _job_pagos_prellenar_intradia,
                  _job_cac_snapshot, _job_repasada_historica,
                  _job_adherencia_kine, _job_control_especialidad,
                  _job_crosssell_kine, _job_crosssell_orl_fono,
                  _job_crosssell_odonto_estetica, _job_crosssell_mg_chequeo,
                  _job_medilink_watchdog, _job_claude_watchdog, _job_cierre_caja_diario,
                  _job_medilink_outage_watcher,
                  _job_agenda_dia, _job_admin_status_report,
                  _job_cleanup_stuck_sessions,
                  _job_waitlist_check,
                  _job_doctor_resumen_precita, _job_doctor_reporte_progreso,
                  _job_doctor_reset_diario,
                  _job_cumpleanos, _job_winback,
                  _job_takeover_ttl, _job_takeover_media_ttl,
                  _job_regenerate_heatmap_cache,
                  _job_enviar_dashboards_semanales,
                  _job_horas_vacias_dia_siguiente,
                  _job_telemedicina_recordatorios,
                  _job_resumen_diario_profesionales,
                  _job_resumen_semanal_profesionales,
                  _job_no_show_check,
                  _job_crosssell_dx,
                  _job_winback_bi,
                  _job_custom_audiences_sync,
                  _job_marketing_consent_blast,
                  _job_consent_agendados,
                  _job_takeover_pendiente_alert,
                  _job_health_report,
                  _job_caja_report,
                  _job_watchdog_blast,
                  _job_winback_daily_report,
                  _job_dental_promo_report,
                  _job_dental_consent_blast,
                  _job_dental_winback,
                  _job_crosssell_post_dental_ortodoncia,
                  _job_sync_citas_recepcion,
                  _job_demanda_semanal,
                  _job_watchdog_entrega)
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
        # Perf 2026-05-26: APScheduler emite INFO por cada job tick. Eran ~113k
        # líneas/log con watchdogs cada 1-5 min. Causa I/O contention en sync
        # writes a /var/log. Quedan WARNING/ERROR para alertas reales.
        "apscheduler.scheduler":          {"level": "WARNING"},
        "apscheduler.executors.default": {"level": "WARNING"},
    },
})
log = logging.getLogger("bot")

# ── Redactar tokens en access log de uvicorn ─────────────────────────────────
# uvicorn.access emite record.args = (client_addr, method, full_path, http_version, status_code)
# El query string (con token=...) va en full_path (índice 2). El filtro lo redacta
# antes de que el formatter lo procese. Confirmado en uvicorn AccessFormatter.formatMessage.
_TOKEN_RE = re.compile(r"(token=)[^&\s\"']+")

class _RedactTokenFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if record.args and isinstance(record.args, tuple) and len(record.args) >= 3:
                args = list(record.args)
                if isinstance(args[2], str):
                    args[2] = _TOKEN_RE.sub(r"\1REDACTED", args[2])
                record.args = tuple(args)
            if isinstance(record.msg, str):
                record.msg = _TOKEN_RE.sub(r"\1REDACTED", record.msg)
        except Exception:
            pass
        return True

logging.getLogger("uvicorn.access").addFilter(_RedactTokenFilter())

# ── Background task helper (FIX-7) ──────────────────────────────────────────
# asyncio.create_task() sin guardar referencia permite que el GC elimine la
# tarea y cualquier excepción queda silenciada ("Task exception was never
# retrieved"). _spawn_bg mantiene referencia fuerte en _BG_TASKS y loguea
# errores explícitamente.
_BG_TASKS: set[asyncio.Task] = set()

def _spawn_bg(coro, name: str = "bg") -> asyncio.Task:
    task = asyncio.create_task(coro, name=name)
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)
    def _on_done(t: asyncio.Task) -> None:
        if t.cancelled():
            return
        exc = t.exception()
        if exc:
            log.error("BG_TASK_FAIL name=%s exc=%s", t.get_name(), exc, exc_info=exc)
    task.add_done_callback(_on_done)
    return task

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
def _dt_now_iso() -> str:
    """Timestamp en el mismo formato que graba `log_message` en `messages.ts`,
    para poder comparar con SQL sin conversiones."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


async def lifespan(app: FastAPI):
    _CLT = "America/Santiago"
    # Recordatorios 24h: todos los días a las 9:00 AM CLT
    scheduler.add_job(
        _job_recordatorios,
        CronTrigger(hour=9, minute=0, timezone=_CLT),
        id="recordatorios_diarios",
        replace_existing=True,
        misfire_grace_time=3600,  # F046: si el proceso arrancó tarde, corre igual (hasta 1h después)
        coalesce=True,            # F046: si se acumularon disparos perdidos, corre solo 1 vez
    )
    # Centinela diario 07:30 CLT — barre fallas SILENCIOSAS (500 del webhook,
    # Meta 400, excepciones, abonos con plata llegada sin conciliar, citas
    # duplicadas) y manda UN resumen WhatsApp al dueño. Solo lee; apagable
    # con CENTINELA_ACTIVE=false. Ver docstring de app/centinela.py.
    from centinela import job_centinela_diario
    scheduler.add_job(
        job_centinela_diario,
        CronTrigger(hour=7, minute=30, timezone=_CLT),
        id="centinela_diario",
        replace_existing=True,
        misfire_grace_time=3600,
        coalesce=True,
    )
    # Agenda por día 04:40 CLT — cachea citas reales por profesional/día
    # (agenda_dias_cache) para que el calendario de /profesional/{id} distinga
    # "día con agenda" de "día solo con fichas a distancia" (bi_atenciones miente).
    scheduler.add_job(
        _job_agenda_dias_sync,
        CronTrigger(hour=4, minute=40, timezone=_CLT),
        id="agenda_dias_sync",
        replace_existing=True,
        misfire_grace_time=3600,
        coalesce=True,
    )
    # ROAS diario 05:30 CLT — recalcula ROAS 30d, persiste snapshot y alerta
    # (WhatsApp ventana-abierta + email) si alguna campaña cae bajo ROAS 1.
    # Corre post bi_sync de madrugada (bi_pagos_caja ya actualizada).
    from roas_routes import roas_daily_job
    scheduler.add_job(
        roas_daily_job,
        CronTrigger(hour=5, minute=30, timezone=_CLT),
        id="roas_diario",
        replace_existing=True,
        misfire_grace_time=3600,
        coalesce=True,
    )
    # Consent en caliente: barrido HORARIO de citas Medilink → consent_marketing_v2
    # a agendados por teléfono/presencial (no del bot). Gated CONSENT_AGENDADOS_ACTIVE.
    # El job se auto-limita a L-V 09-20 CLT aunque el cron dispare cada hora.
    scheduler.add_job(
        _job_consent_agendados,
        CronTrigger(minute=25, timezone=_CLT),
        id="consent_agendados_horario",
        replace_existing=True,
        misfire_grace_time=1800,  # F046: hasta 30 min de gracia (corre cada hora, 30 min es razonable)
        coalesce=True,
    )
    # Promo post-consent: consent_marketing aceptado + ATENCIÓN REALIZADA (pago
    # en caja, /pagos Medilink en vivo) → promo dental segmentada en la corrida
    # siguiente. HORARIO al :48 L-V (minuto libre del escalonado 429); el job
    # se auto-limita a 09-21 CLT. Gated PROMO_POSTCONSENT_ACTIVE (default OFF)
    # + override switchboard.
    from promo_postconsent import job_promo_postconsent as _job_promo_postconsent
    scheduler.add_job(
        _job_promo_postconsent,
        CronTrigger(day_of_week="mon-fri", minute=48, timezone=_CLT),
        id="promo_postconsent_horario",
        replace_existing=True,
        misfire_grace_time=600,
    )
    # Preparación pre-examen eco: barrido horario de citas de David Pardo en
    # ventana hoy..+2 días → template UTILITY con la preparación por tipo.
    # HORARIO al :11 (minuto libre del escalonado 429); el job se auto-limita
    # a 08-21 CLT. Gated ECO_PREP_ACTIVE (default OFF) + template APPROVED.
    from eco_prep import job_eco_prep as _job_eco_prep
    scheduler.add_job(
        _job_eco_prep,
        CronTrigger(minute=11, timezone=_CLT),
        id="eco_prep_horario",
        replace_existing=True,
        misfire_grace_time=600,
    )
    # Reconciliación Pagos × Medilink cada 30 min (solo particular/fonasa MG/MF):
    # refresca la caja Medilink de hoy/ayer y marca cada fila ok/difiere/falta.
    from pagos_recon import job_reconciliar_pagos as _job_recon_pagos
    scheduler.add_job(
        _job_recon_pagos,
        CronTrigger(minute="0,30", timezone=_CLT),
        id="recon_pagos_30min",
        replace_existing=True,
        misfire_grace_time=600,
    )
    # Autopilot marketing (Fase 1, dry-run diario 08:30 CLT).
    # Inerte salvo AUTOPILOT_ENABLED=true. NO ejecuta cambios en Meta (solo reporta).
    async def _job_autopilot_dryrun():
        import os, logging
        if os.getenv("AUTOPILOT_ENABLED", "false").lower() != "true":
            return
        try:
            from autopilot.engine import run_dry_run
            run = await run_dry_run(window_days=7)
            logging.getLogger("bot").info(
                "[autopilot] dry-run OK — %d acciones propuestas", len(run.actions))
        except Exception as e:  # noqa: BLE001 — nunca tumbar el scheduler por el autopilot
            logging.getLogger("bot").error("[autopilot] dry-run falló: %s", e)
    scheduler.add_job(
        _job_autopilot_dryrun,
        CronTrigger(hour=8, minute=30, timezone=_CLT),
        id="autopilot_dryrun",
        replace_existing=True,
        # misfire_grace_time: APScheduler descarta el disparo si el proceso
        # estaba ocupado/reiniciando en ese segundo exacto — sin log ni error.
        # El bot reinicia en cada deploy (16 veces en 7 días de ago-2026) y por
        # eso el cierre de caja NUNCA corrió desde que se creó el 30-jun.
        misfire_grace_time=3600,
        coalesce=True,
    )
    # Cerebro de Alma — snapshot del world-state global (Fase 1). Read-only y
    # Medilink-free: lee BI + sessions.db + snapshots ya persistidos. Corre
    # SIEMPRE (es solo lectura, no contacta a nadie); 06:00 CLT, tras el cierre
    # diario de BI (23:59) para tener data fresca del día anterior.
    async def _job_alma_brain_snapshot():
        try:
            from alma_brain.state import build_and_save
            st = await asyncio.to_thread(build_and_save, 7)
            logging.getLogger("bot").info(
                "[alma_brain] snapshot OK — %d dominios · %d alertas",
                len(st.get("domains_available", [])), len(st.get("alerts", [])))
        except Exception as e:  # noqa: BLE001 — nunca tumbar el scheduler por el cerebro
            logging.getLogger("bot").error("[alma_brain] snapshot falló: %s", e)
    scheduler.add_job(
        _job_alma_brain_snapshot,
        CronTrigger(hour=6, minute=0, timezone=_CLT),
        id="alma_brain_snapshot",
        replace_existing=True,
        # misfire_grace_time: APScheduler descarta el disparo si el proceso
        # estaba ocupado/reiniciando en ese segundo exacto — sin log ni error.
        # El bot reinicia en cada deploy (16 veces en 7 días de ago-2026) y por
        # eso el cierre de caja NUNCA corrió desde que se creó el 30-jun.
        misfire_grace_time=3600,
        coalesce=True,
    )
    # Flota de agentes autónomos (Alma Agents). INERTE por defecto: register_agent_jobs
    # no agrega NI UN job si ALMA_AGENTS_ENABLED=false (default). Encender el maestro
    # recién agenda los agentes, y aún así cada run() vuelve a chequear su propio flag
    # + execute. Nunca tumba el scheduler (errores aislados por agente).
    try:
        from alma_agents.scheduler_hook import register_agent_jobs
        register_agent_jobs(scheduler, _CLT)
    except Exception as e:  # noqa: BLE001
        logging.getLogger("bot").error("[alma_agents] registro de flota falló: %s", e)
    # Effectiveness Ledger: mide si los contactos de la flota convirtieron (cita/
    # respuesta) dentro de su ventana. Solo LEE sessions.db y escribe su propia
    # tabla — no contacta a nadie → corre SIEMPRE, independiente del maestro.
    async def _job_alma_agents_ledger():
        try:
            from alma_agents import ledger
            res = await asyncio.to_thread(ledger.measure_outcomes)
            logging.getLogger("bot").info(
                "[alma_agents] ledger medido — %d citas · %d respuestas · %d sin respuesta",
                res.get("cita", 0), res.get("respuesta", 0), res.get("sin_respuesta", 0))
        except Exception as e:  # noqa: BLE001
            logging.getLogger("bot").error("[alma_agents] ledger falló: %s", e)
    scheduler.add_job(
        _job_alma_agents_ledger,
        CronTrigger(hour=5, minute=30, timezone=_CLT),
        id="alma_agents_ledger",
        replace_existing=True,
        # misfire_grace_time: APScheduler descarta el disparo si el proceso
        # estaba ocupado/reiniciando en ese segundo exacto — sin log ni error.
        # El bot reinicia en cada deploy (16 veces en 7 días de ago-2026) y por
        # eso el cierre de caja NUNCA corrió desde que se creó el 30-jun.
        misfire_grace_time=3600,
        coalesce=True,
    )
    # Capstone: latido diario que une cerebro + flota (intención simulada) +
    # ledger en un digest unificado. No re-ejecuta agentes ni contacta a nadie;
    # solo sensa/mide/reporta → corre SIEMPRE. 06:30 CLT (tras snapshot 06:00).
    async def _job_alma_agents_capstone():
        try:
            from alma_agents import capstone
            d = await capstone.run_cycle()
            logging.getLogger("bot").info("[alma_agents] capstone — %s", d.get("headline"))
        except Exception as e:  # noqa: BLE001
            logging.getLogger("bot").error("[alma_agents] capstone falló: %s", e)
    scheduler.add_job(
        _job_alma_agents_capstone,
        CronTrigger(hour=6, minute=30, timezone=_CLT),
        id="alma_agents_capstone",
        replace_existing=True,
        # misfire_grace_time: APScheduler descarta el disparo si el proceso
        # estaba ocupado/reiniciando en ese segundo exacto — sin log ni error.
        # El bot reinicia en cada deploy (16 veces en 7 días de ago-2026) y por
        # eso el cierre de caja NUNCA corrió desde que se creó el 30-jun.
        misfire_grace_time=3600,
        coalesce=True,
    )
    # Cola de publicación orgánica (segmento IG·FB·WhatsApp): publica las piezas
    # aprobadas que ya vencieron su hora. La escritura real a Meta está bloqueada
    # salvo ORGANIC_PUBLISH_EXECUTE=true (kill-switch). Cada 5 min.
    async def _job_organic_publish_queue():
        try:
            from autopilot.publishing import run_due_queue
            await run_due_queue()
        except Exception as e:  # noqa: BLE001 — nunca tumbar el scheduler
            logging.getLogger("bot").error("[publish] cola falló: %s", e)
    scheduler.add_job(
        _job_organic_publish_queue,
        CronTrigger(minute="*/5", timezone=_CLT),
        id="organic_publish_queue",
        replace_existing=True,
    )
    # Recordatorios 2h: cada 15 min entre 7:30 y 21:30 CLT
    scheduler.add_job(
        _job_recordatorios_2h,
        CronTrigger(hour="7-21", minute="0,15,30,45", timezone=_CLT),
        id="recordatorios_2h",
        replace_existing=True,
        misfire_grace_time=600,   # F046: 10 min de gracia (corre cada 15 min — gracia del 66%)
        coalesce=True,
    )
    # Recordatorios 48h anti no-show: diario 10:00 CLT.
    # Solo envía a pacientes con historial de no-show o cita en peak 16-19h.
    # Columna reminder_48h_sent se agrega inline en session.py si no existe.
    scheduler.add_job(
        _job_recordatorios_48h,
        CronTrigger(hour=10, minute=0, timezone=_CLT),
        id="recordatorios_48h",
        replace_existing=True,
        misfire_grace_time=3600,
        coalesce=True,
    )
    # Piloto recordatorios recepción (Márquez id=13): 08:00 CLT, antes de recordatorios 09:00.
    # Con RECORDATORIOS_RECEPCION_ENABLED=false (default) termina inmediatamente.
    scheduler.add_job(
        _job_sync_citas_recepcion,
        CronTrigger(hour=5, minute=30, timezone=_CLT),
        id="sync_citas_recepcion",
        replace_existing=True,
        misfire_grace_time=3600,
        coalesce=True,
    )
    # Reenganche: cada 5 minutos revisa sesiones abandonadas
    scheduler.add_job(
        _enviar_reenganche,
        "interval", minutes=5,
        id="reenganche",
        replace_existing=True,
    )
    # Skills aprendidas (nivel 6): lunes 09:35 CLT (hueco entre 9:12 y 10:00 para
    # no engrosar clusters → 429). No-op si LEARNED_SKILLS_ACTIVE=false (default).
    scheduler.add_job(
        _job_learned_skills,
        CronTrigger(day_of_week="mon", hour=9, minute=35, timezone=_CLT),
        id="learned_skills_semanal",
        replace_existing=True,
        # misfire_grace_time: APScheduler descarta el disparo si el proceso
        # estaba ocupado/reiniciando en ese segundo exacto — sin log ni error.
        # El bot reinicia en cada deploy (16 veces en 7 días de ago-2026) y por
        # eso el cierre de caja NUNCA corrió desde que se creó el 30-jun.
        misfire_grace_time=3600,
        coalesce=True,
    )
    # Guardrail intervalos: 06:20 CLT, antes de que abra el centro y fuera de
    # los clusters de la mañana (429). Compara la duración de cita del bot
    # contra el intervalo real de cada profesional en Medilink y alerta si
    # dejó de ser múltiplo — ese desajuste hace fallar la reserva en el ÚLTIMO
    # paso y costó 46 citas de psiquiatría entre junio y julio de 2026.
    scheduler.add_job(
        _job_verificar_intervalos,
        CronTrigger(hour=6, minute=20, timezone=_CLT),
        id="verificar_intervalos_diario",
        replace_existing=True,
        misfire_grace_time=3600,
        coalesce=True,
    )
    # Post-consulta: 18:00 CLT (Item 33 — dentro de ventana 09:30-20:00 para evitar
    # mensajes de madrugada; 44% sin respuesta cuando corría a las 22:00).
    # 22:00 CLT por decisión del dueño: la clínica atiende hasta las 21:00,
    # así el envío del día alcanza a TODOS los atendidos. Citas posteriores
    # las recoge _job_postconsulta_morning (09:00 CLT).
    scheduler.add_job(
        _job_postconsulta,
        CronTrigger(hour=22, minute=0, timezone=_CLT),
        id="seguimiento_postconsulta",
        replace_existing=True,
        misfire_grace_time=3600,
        coalesce=True,
    )
    # Postconsulta morning: cubre citas tardías (>22:00) del día anterior
    # que el cron de las 22:00 no alcanzó (la cita aún no había ocurrido).
    scheduler.add_job(
        _job_postconsulta_morning,
        CronTrigger(hour=9, minute=12, timezone=_CLT),  # 9:12 (era 9:00; cluster de 6 jobs → 429)
        id="seguimiento_postconsulta_morning",
        replace_existing=True,
        misfire_grace_time=3600,
        coalesce=True,
    )
    # Enrolar atendidos offline (recepción/presencial) a citas_bot para que
    # el cron postconsulta de las 22:00 los alcance. Corre 21:30 CLT, antes.
    # Pacientes con perfil bot existente (Tier B) se insertan automático;
    # los sin opt-in WhatsApp (Tier C) van a tabla pacientes_sin_optin para
    # que recepción los enrole manualmente respetando Ley 19.628.
    scheduler.add_job(
        _job_enrolar_atendidos_dia,
        CronTrigger(hour=21, minute=30, timezone=_CLT),
        id="enrolar_atendidos_dia",
        replace_existing=True,
        misfire_grace_time=3600,
        coalesce=True,
    )
    # Detectar cancelaciones hechas en Medilink: cada hora barre citas futuras
    # (hoy + 14 días), valida contra Medilink, marca canceladas y reagenda
    # automáticamente las próximas (≤48h). Implementado tras caso 2026-05-03
    # (cita 54874 anulada hace 20 días seguía generando recordatorios).
    scheduler.add_job(
        _job_detectar_cancelaciones,
        CronTrigger(minute=24, timezone=_CLT),  # cada hora :24 (era :15; chocaba con recordatorios_2h+telemedicina → 995×429 en :15)
        id="detectar_cancelaciones",
        replace_existing=True,
    )
    # Monitor de anomalías: cada 15 min escanea bugs sospechosos y manda
    # resumen al WhatsApp del dueño (ADMIN_ALERT_PHONE). El dueño se entera
    # antes que el paciente lo viva. Anti-spam interno (4h por hash de alerta).
    scheduler.add_job(
        _job_monitor_anomalias,
        "interval", minutes=15,
        id="monitor_anomalias",
        replace_existing=True,
    )
    # Sync atenciones Dr. Abarca: cierre del día a las 23:55 CLT
    scheduler.add_job(
        _job_abarca_sync,
        CronTrigger(hour=23, minute=55, timezone=_CLT),
        id="abarca_sync_diario",
        replace_existing=True,
        # misfire_grace_time: APScheduler descarta el disparo si el proceso
        # estaba ocupado/reiniciando en ese segundo exacto — sin log ni error.
        # El bot reinicia en cada deploy (16 veces en 7 días de ago-2026) y por
        # eso el cierre de caja NUNCA corrió desde que se creó el 30-jun.
        misfire_grace_time=3600,
        coalesce=True,
    )
    # Sync atenciones Dr. Olavarría: cierre del día a las 23:57 CLT
    scheduler.add_job(
        _job_olavarria_sync,
        CronTrigger(hour=23, minute=57, timezone=_CLT),
        id="olavarria_sync_diario",
        replace_existing=True,
        # misfire_grace_time: APScheduler descarta el disparo si el proceso
        # estaba ocupado/reiniciando en ese segundo exacto — sin log ni error.
        # El bot reinicia en cada deploy (16 veces en 7 días de ago-2026) y por
        # eso el cierre de caja NUNCA corrió desde que se creó el 30-jun.
        misfire_grace_time=3600,
        coalesce=True,
    )
    # BI v2: sync diario de TODOS los profesionales 23:59 CLT
    scheduler.add_job(
        _job_bi_sync_diario,
        CronTrigger(hour=23, minute=59, timezone=_CLT),
        id="bi_sync_v2_diario",
        replace_existing=True,
        misfire_grace_time=3600,
        coalesce=True,
    )
    # Panel del Día: cache de CAPACIDAD REAL por profesional (Medilink /citas,
    # secuencial+throttle) 04:10 CLT — horario libre, off-peak. Alimenta el
    # potencial/ocupación reales del N1 sin fan-out en vivo.
    from panel_dia_jobs import refrescar_cap_cache
    scheduler.add_job(
        refrescar_cap_cache,
        CronTrigger(hour=4, minute=10, timezone=_CLT),
        id="panel_cap_cache_diario",
        replace_existing=True,
        misfire_grace_time=3600,
        coalesce=True,
        max_instances=1,
    )
    # …y un refresco intradía SÓLO de hoy. El barrido de las 04:10 mide la
    # agenda antes de que abra el centro, así que a media tarde ya no refleja
    # lo que se agendó durante el día: en Boxes salía "24 citas de 22 cupos",
    # una capacidad menor que las citas reales. Es una fecha sola (~24 llamadas
    # secuenciales throttleadas), no el fan-out que tumbó /admin/api/agenda-dia.
    def _refrescar_cap_hoy():
        from datetime import date as _d
        return refrescar_cap_cache(solo_fecha=_d.today().isoformat(), full=True)
    scheduler.add_job(
        _refrescar_cap_hoy,
        CronTrigger(hour="10,13,16,19", minute=25, timezone=_CLT),
        id="panel_cap_cache_hoy",
        replace_existing=True,
        misfire_grace_time=1800,
        coalesce=True,
        max_instances=1,
    )
    # Ausentismo: recolección nocturna de citas (Medilink /citas paginado,
    # carril batch) → tabla local `ausentismo_citas`. 04:50 CLT, off-peak,
    # después del cap cache y antes del barrido de sin-cerrar (06:20). La
    # primera noche hace backfill profundo (hasta 12 meses) automáticamente.
    from ausentismo import job_ausentismo_nocturno
    scheduler.add_job(
        job_ausentismo_nocturno,
        CronTrigger(hour=4, minute=50, timezone=_CLT),
        id="ausentismo_nocturno",
        replace_existing=True,
        misfire_grace_time=3600,
        coalesce=True,
        max_instances=1,
    )
    # BI v2: sync intradía LIGERO (solo pagos del día) 14:00 y 19:00 CLT → el
    # dashboard /cmc/mensual refleja el día en curso sin esperar a las 23:59.
    scheduler.add_job(
        _job_bi_sync_intradia,
        CronTrigger(hour="14,19", minute=0, timezone=_CLT),
        id="bi_sync_v2_intradia",
        replace_existing=True,
        misfire_grace_time=3600,
        coalesce=True,
    )
    # Panel Pagos: mantener pagos_cmc del día SIEMPRE completo (todas las citas +
    # RUT) sin depender del botón manual. Idempotente, carril batch (semáforo
    # angosto, sus 429 no tocan el breaker del paciente); max_instances=1 evita
    # solapes si una corrida se alarga.
    #
    # 2026-07-27: era cada 30 min y ARRANCABA EN EL MINUTO 0, justo encima del
    # resto de los crons de la hora en punto. El pico de concurrencia contra
    # Medilink devolvía 105-114 429/hora y tumbaba el agendamiento. Ahora corre
    # 1 vez por hora y en el minuto 45, lejos del amontonamiento; el barrido
    # pesado real ya lo hace el sync nocturno.
    scheduler.add_job(
        _job_pagos_prellenar_intradia,
        CronTrigger(minute="45", hour="8-21", timezone=_CLT),
        id="pagos_prellenar_intradia",
        replace_existing=True,
        misfire_grace_time=600,
        coalesce=True,
        max_instances=1,
    )

    # Repasada histórica completa (semanal): domingo 03:30 CLT, caza errores viejos
    scheduler.add_job(
        _job_repasada_historica,
        CronTrigger(day_of_week="sun", hour=3, minute=30, timezone=_CLT),
        id="repasada_historica_semanal",
        replace_existing=True,
        # misfire_grace_time: APScheduler descarta el disparo si el proceso
        # estaba ocupado/reiniciando en ese segundo exacto — sin log ni error.
        # El bot reinicia en cada deploy (16 veces en 7 días de ago-2026) y por
        # eso el cierre de caja NUNCA corrió desde que se creó el 30-jun.
        misfire_grace_time=3600,
        coalesce=True,
    )
    # CAC snapshot: 00:20 CLT (post bi_sync → usa pagos frescos). Alimenta la
    # pestaña Atribución de Autopilot (data/cac_snapshot.json).
    scheduler.add_job(
        _job_cac_snapshot,
        CronTrigger(hour=0, minute=20, timezone=_CLT),
        id="cac_snapshot_diario",
        replace_existing=True,
        # misfire_grace_time: APScheduler descarta el disparo si el proceso
        # estaba ocupado/reiniciando en ese segundo exacto — sin log ni error.
        # El bot reinicia en cada deploy (16 veces en 7 días de ago-2026) y por
        # eso el cierre de caja NUNCA corrió desde que se creó el 30-jun.
        misfire_grace_time=3600,
        coalesce=True,
    )
    # Reporte semanal de demanda no capturada (Items 31/32/35): lunes 09:00 CLT.
    # Lee conversation_events (sin_disponibilidad + demanda_no_disponible) + waitlist.
    # Solo lectura — NO contacta pacientes. Envía resumen rankeado al dueño.
    scheduler.add_job(
        _job_demanda_semanal,
        CronTrigger(day_of_week="mon", hour=9, minute=6, timezone=_CLT),  # 9:06 (era 9:00)
        id="demanda_semanal",
        replace_existing=True,
        # misfire_grace_time: APScheduler descarta el disparo si el proceso
        # estaba ocupado/reiniciando en ese segundo exacto — sin log ni error.
        # El bot reinicia en cada deploy (16 veces en 7 días de ago-2026) y por
        # eso el cierre de caja NUNCA corrió desde que se creó el 30-jun.
        misfire_grace_time=3600,
        coalesce=True,
    )
    # Reactivación: todos los lunes a las 10:30 AM CLT
    scheduler.add_job(
        _job_reactivacion,
        CronTrigger(day_of_week="mon", hour=10, minute=30, timezone=_CLT),
        id="reactivacion_pacientes",
        replace_existing=True,
        misfire_grace_time=3600,
        coalesce=True,
    )
    # Adherencia kine: L/M/V a las 11:00 AM CLT (antes diario — bajamos 7→3/sem para reducir costo templates)
    scheduler.add_job(
        _job_adherencia_kine,
        CronTrigger(day_of_week="mon,wed,fri", hour=11, minute=0, timezone=_CLT),
        id="adherencia_kine",
        replace_existing=True,
        # misfire_grace_time: APScheduler descarta el disparo si el proceso
        # estaba ocupado/reiniciando en ese segundo exacto — sin log ni error.
        # El bot reinicia en cada deploy (16 veces en 7 días de ago-2026) y por
        # eso el cierre de caja NUNCA corrió desde que se creó el 30-jun.
        misfire_grace_time=3600,
        coalesce=True,
    )
    # Control por especialidad: diario a las 11:30 AM CLT
    scheduler.add_job(
        _job_control_especialidad,
        CronTrigger(hour=11, minute=30, timezone=_CLT),
        id="control_especialidad",
        replace_existing=True,
        # misfire_grace_time: APScheduler descarta el disparo si el proceso
        # estaba ocupado/reiniciando en ese segundo exacto — sin log ni error.
        # El bot reinicia en cada deploy (16 veces en 7 días de ago-2026) y por
        # eso el cierre de caja NUNCA corrió desde que se creó el 30-jun.
        misfire_grace_time=3600,
        coalesce=True,
    )
    # Cross-sell kine: miércoles a las 10:30 AM CLT
    scheduler.add_job(
        _job_crosssell_kine,
        CronTrigger(day_of_week="wed", hour=10, minute=30, timezone=_CLT),
        id="crosssell_kine",
        replace_existing=True,
        # misfire_grace_time: APScheduler descarta el disparo si el proceso
        # estaba ocupado/reiniciando en ese segundo exacto — sin log ni error.
        # El bot reinicia en cada deploy (16 veces en 7 días de ago-2026) y por
        # eso el cierre de caja NUNCA corrió desde que se creó el 30-jun.
        misfire_grace_time=3600,
        coalesce=True,
    )
    # Cross-sell ORL↔Fono: jueves 11:00 CLT
    scheduler.add_job(
        _job_crosssell_orl_fono,
        CronTrigger(day_of_week="thu", hour=11, minute=24, timezone=_CLT),  # 11:24 (era 11:00)
        id="crosssell_orl_fono",
        replace_existing=True,
        # misfire_grace_time: APScheduler descarta el disparo si el proceso
        # estaba ocupado/reiniciando en ese segundo exacto — sin log ni error.
        # El bot reinicia en cada deploy (16 veces en 7 días de ago-2026) y por
        # eso el cierre de caja NUNCA corrió desde que se creó el 30-jun.
        misfire_grace_time=3600,
        coalesce=True,
    )
    # Cross-sell odontología → estética: 1º y 15 del mes 10:30 CLT
    scheduler.add_job(
        _job_crosssell_odonto_estetica,
        CronTrigger(day="1,15", hour=10, minute=30, timezone=_CLT),
        id="crosssell_odonto_estetica",
        replace_existing=True,
        # misfire_grace_time: APScheduler descarta el disparo si el proceso
        # estaba ocupado/reiniciando en ese segundo exacto — sin log ni error.
        # El bot reinicia en cada deploy (16 veces en 7 días de ago-2026) y por
        # eso el cierre de caja NUNCA corrió desde que se creó el 30-jun.
        misfire_grace_time=3600,
        coalesce=True,
    )
    # Cross-sell MG→chequeo preventivo: primer martes del mes 09:30 CLT
    scheduler.add_job(
        _job_crosssell_mg_chequeo,
        CronTrigger(day_of_week="tue", day="1-7", hour=9, minute=30, timezone=_CLT),
        id="crosssell_mg_chequeo",
        replace_existing=True,
        # misfire_grace_time: APScheduler descarta el disparo si el proceso
        # estaba ocupado/reiniciando en ese segundo exacto — sin log ni error.
        # El bot reinicia en cada deploy (16 veces en 7 días de ago-2026) y por
        # eso el cierre de caja NUNCA corrió desde que se creó el 30-jun.
        misfire_grace_time=3600,
        coalesce=True,
    )
    # Cross-sell post-dental -> ortodoncia: L-V 11:00 CLT (Patron 5, 2026-05-19)
    # Pacientes con cita dental atendida hace 48-72h sin cita futura con Castillo.
    # Template pendiente de aprobacion Meta: crosssell_ortodoncia_post_dental_v1.
    scheduler.add_job(
        _job_crosssell_post_dental_ortodoncia,
        CronTrigger(day_of_week="mon-fri", hour=11, minute=6, timezone=_CLT),  # 11:06 (era 11:00; cluster de 5 jobs)
        id="crosssell_post_dental_ortodoncia",
        replace_existing=True,
        # misfire_grace_time: APScheduler descarta el disparo si el proceso
        # estaba ocupado/reiniciando en ese segundo exacto — sin log ni error.
        # El bot reinicia en cada deploy (16 veces en 7 días de ago-2026) y por
        # eso el cierre de caja NUNCA corrió desde que se creó el 30-jun.
        misfire_grace_time=3600,
        coalesce=True,
    )
    # Cumpleaños: diario a las 10:00 CLT
    scheduler.add_job(
        _job_cumpleanos,
        CronTrigger(hour=10, minute=18, timezone=_CLT),  # 10:18 (era 10:00; chocaba con recordatorios_48h)
        id="cumpleanos_diario",
        replace_existing=True,
        misfire_grace_time=3600,
        coalesce=True,
    )
    # Win-back >90 días: primer lunes de cada mes a las 10:00 CLT
    scheduler.add_job(
        _job_winback,
        CronTrigger(day_of_week="mon", day="1-7", hour=10, minute=42, timezone=_CLT),  # 10:42 (era 10:00; libre entre dental 10:35 y blast 11:xx)
        id="winback_mensual",
        replace_existing=True,
        # misfire_grace_time: APScheduler descarta el disparo si el proceso
        # estaba ocupado/reiniciando en ese segundo exacto — sin log ni error.
        # El bot reinicia en cada deploy (16 veces en 7 días de ago-2026) y por
        # eso el cierre de caja NUNCA corrió desde que se creó el 30-jun.
        misfire_grace_time=3600,
        coalesce=True,
    )
    # Sync caché de citas: diario a las 23:50 CLT
    scheduler.add_job(
        _sync_citas_hoy,
        CronTrigger(hour=23, minute=50, timezone=_CLT),
        id="sync_citas_cache",
        replace_existing=True,
        # misfire_grace_time: APScheduler descarta el disparo si el proceso
        # estaba ocupado/reiniciando en ese segundo exacto — sin log ni error.
        # El bot reinicia en cada deploy (16 veces en 7 días de ago-2026) y por
        # eso el cierre de caja NUNCA corrió desde que se creó el 30-jun.
        misfire_grace_time=3600,
        coalesce=True,
    )
    # Monitor de Agendamientos en vivo: poll cada 45s, UNA sola consulta base
    # (id_sucursal + cursor por id, sin filtro de profesional — ver
    # docs/medilink_gotchas.md §6, antecedente del fan-out de agenda-dia).
    from agenda_ticker import poll_agenda_ticker, sync_atenciones_sin_cerrar
    scheduler.add_job(
        poll_agenda_ticker,
        "interval", seconds=45,
        id="agenda_ticker_poll",
        replace_existing=True,
        misfire_grace_time=30,
        coalesce=True,
        max_instances=1,
    )
    # Barrido diario (horario valle) de citas pasadas sin cerrar en Medilink —
    # alimenta el aviso pasivo del monitor. Baja frecuencia a propósito:
    # pagina varias decenas de veces (30 días de agenda completa).
    scheduler.add_job(
        sync_atenciones_sin_cerrar,
        CronTrigger(hour=6, minute=20, timezone=_CLT),
        id="agenda_ticker_sin_cerrar_diario",
        replace_existing=True,
        misfire_grace_time=3600,
        coalesce=True,
    )
    # Lector de correos de Medilink (IMAP, solo lectura): cada 60s revisa el
    # Gmail del centro por notificaciones nuevas de agendamiento/anulación/
    # reagendamiento y alimenta agenda_ticker con la hora EXACTA de creación
    # (la API de Medilink no la entrega). Ver app/email_ticker.py. Degrada
    # con gracia si GMAIL_CMC_USER/GMAIL_CMC_APP_PASSWORD no están seteados.
    from email_ticker import poll_email_ticker
    scheduler.add_job(
        poll_email_ticker,
        "interval", seconds=60,
        id="email_ticker_poll",
        replace_existing=True,
        misfire_grace_time=30,
        coalesce=True,
        max_instances=1,
    )
    # Conciliación de transferencias (IMAP, solo lectura): cada 10 min revisa
    # el Gmail del centro por avisos NUEVOS de transferencia bancaria (12
    # bancos, ver app/transferencias_email_parser.py) y los guarda en
    # transferencias_banco (tabla COMPARTIDA con abono_transferencia.py — ver
    # docstring de conciliacion_transferencias.ensure_conciliacion_tables).
    # Cursor propio (transferencias_banco_last_uid), independiente del de
    # email_ticker/abono_transferencia. Degrada con gracia si las
    # credenciales no están seteadas o Gmail no responde.
    # GATEADO por CONCILIACION_TRANSFERENCIAS_ACTIVE (2026-07-28). Antes se
    # registraba siempre: deployar el bloque bastaba para empezar a leer el
    # Gmail del centro cada 10 min, sin decisión y sin apagado que no fuera
    # otro deploy. Con el flag en false ni siquiera se importa el módulo.
    # Pre-carga de resultados de exámenes al Copiloto de Ficha ~15 min antes
    # de la cita (pedido del Dr. 2026-08-01). Cada 4 min revisa citas próximas
    # de los profesionales configurados y crea la ficha con las transcripciones
    # ya puestas. Idempotente (marca cargado). Ver app/copiloto_bridge.py.
    from config import COPILOTO_PRELOAD_ACTIVE as _COPI_ACTIVE
    if _COPI_ACTIVE:
        from copiloto_bridge import precargar_para_citas
        scheduler.add_job(
            precargar_para_citas,
            "interval", minutes=4,
            id="copiloto_preload",
            replace_existing=True,
            misfire_grace_time=60,
            coalesce=True,
            max_instances=1,
        )

    from config import CONCILIACION_TRANSFERENCIAS_ACTIVE as _CONCIL_ACTIVE
    if _CONCIL_ACTIVE:
        from conciliacion_transferencias import poll_conciliacion_transferencias
        # 60 s, no 10 min (2026-07-28, decisión del dueño). Con 10 minutos la
        # sugerencia llegaba tarde para el único momento en que sirve: el
        # paciente transfiere en el mesón y recepción cobra al tiro. Si la
        # sugerencia aparece después de que ya cobró, no aporta nada — el
        # sistema solo propone sobre filas SIN cobrar.
        # No cuesta más: `email_ticker_poll` lleva meses consultando este mismo
        # buzón cada 60 s. Son conexiones IMAP cortas y secuenciales
        # (max_instances=1, nunca se solapan), Gmail no cobra por request, y
        # este carril no toca ni Medilink ni la IA.
        scheduler.add_job(
            poll_conciliacion_transferencias,
            "interval", seconds=60,
            id="conciliacion_transferencias_poll",
            replace_existing=True,
            misfire_grace_time=45,
            coalesce=True,
            max_instances=1,
        )
        log.info("conciliación de transferencias ACTIVA (poll cada 60 s)")
    else:
        log.info("conciliación de transferencias apagada "
                 "(CONCILIACION_TRANSFERENCIAS_ACTIVE=false) — sin IMAP")
    # Confirmación automática de abonos por transferencia (Psiquiatría, $60.000,
    # única prestación con abono hoy). GATEADO por ABONO_AUTO_ACTIVE: mientras
    # esté en false ni siquiera se registra el cron (cero conexiones IMAP
    # nuevas) — el flujo actual de foto del comprobante sigue intacto. Ver
    # app/abono_transferencia.py para el diseño completo.
    from config import ABONO_AUTO_ACTIVE as _ABONO_AUTO_ACTIVE
    if _ABONO_AUTO_ACTIVE:
        from abono_transferencia import poll_abonos_transferencia, job_nudge_foto_fallback
        scheduler.add_job(
            poll_abonos_transferencia,
            "interval", seconds=60,
            id="abono_transferencia_poll",
            replace_existing=True,
            misfire_grace_time=30,
            coalesce=True,
            max_instances=1,
        )
        scheduler.add_job(
            job_nudge_foto_fallback,
            "interval", minutes=2,
            id="abono_transferencia_nudge_foto",
            replace_existing=True,
            misfire_grace_time=60,
            coalesce=True,
            max_instances=1,
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
    # Watchdog IA (Claude): cada 2 min alerta al dueño si la IA cae (saldo/API)
    scheduler.add_job(
        _job_claude_watchdog,
        "interval", minutes=2,
        id="claude_watchdog",
        replace_existing=True,
    )
    # Watchdog modo caída Medilink (403 "plataforma no activa"): cada 3 min,
    # solo actúa si hay contexto pendiente de avisar. Ver medilink_outage.py.
    scheduler.add_job(
        _job_medilink_outage_watcher,
        "interval", minutes=3,
        id="medilink_outage_watcher",
        replace_existing=True,
    )
    # Cierre de caja diario: 09:05 CLT empuja al dueño el cierre del día anterior
    scheduler.add_job(
        _job_cierre_caja_diario,
        CronTrigger(hour=9, minute=5, timezone=_CLT),
        id="cierre_caja_diario",
        replace_existing=True,
        # misfire_grace_time: APScheduler descarta el disparo si el proceso
        # estaba ocupado/reiniciando en ese segundo exacto — sin log ni error.
        # El bot reinicia en cada deploy (16 veces en 7 días de ago-2026) y por
        # eso el cierre de caja NUNCA corrió desde que se creó el 30-jun.
        misfire_grace_time=3600,
        coalesce=True,
    )
    # Agenda del día: 07:45 CLT empuja cupos/ocupados/libres por profesional
    scheduler.add_job(
        _job_agenda_dia,
        CronTrigger(hour=7, minute=45, timezone=_CLT),
        id="agenda_dia",
        replace_existing=True,
        # misfire_grace_time: APScheduler descarta el disparo si el proceso
        # estaba ocupado/reiniciando en ese segundo exacto — sin log ni error.
        # El bot reinicia en cada deploy (16 veces en 7 días de ago-2026) y por
        # eso el cierre de caja NUNCA corrió desde que se creó el 30-jun.
        misfire_grace_time=3600,
        coalesce=True,
    )
    # Lista de espera: diario a las 07:00 CLT
    scheduler.add_job(
        _job_waitlist_check,
        CronTrigger(hour=7, minute=0, timezone=_CLT),
        id="waitlist_check",
        replace_existing=True,
        misfire_grace_time=3600,  # F046
        coalesce=True,            # F046
    )
    # Doctor alerts: resumen pre-cita cada 5 min (lun-sáb 07:30-21:30 CLT)
    # Desfasado a :03/:08/.../:58 para no caer en :00/:15/:30/:45 junto a
    # recordatorios_2h (anti-429 Medilink).
    scheduler.add_job(
        _job_doctor_resumen_precita,
        CronTrigger(minute="3-58/5", hour="7-21", day_of_week="mon-sat", timezone=_CLT),
        id="doctor_resumen_precita",
        replace_existing=True,
        # misfire_grace_time: APScheduler descarta el disparo si el proceso
        # estaba ocupado/reiniciando en ese segundo exacto — sin log ni error.
        # El bot reinicia en cada deploy (16 veces en 7 días de ago-2026) y por
        # eso el cierre de caja NUNCA corrió desde que se creó el 30-jun.
        misfire_grace_time=3600,
        coalesce=True,
    )
    # Doctor alerts: reporte progreso 09:05, 12:05, 16:05, 20:05 CLT
    # (+5 min para no chocar con recordatorios y no-show que parten a :00/:02)
    for h in (9, 12, 16, 20):
        scheduler.add_job(
            _job_doctor_reporte_progreso,
            CronTrigger(hour=h, minute=5, timezone=_CLT),
            id=f"doctor_reporte_{h}",
            replace_existing=True,
        )
    # Doctor alerts: reset diario a medianoche CLT
    scheduler.add_job(
        _job_doctor_reset_diario,
        CronTrigger(hour=0, minute=0, timezone=_CLT),
        id="doctor_reset_diario",
        replace_existing=True,
        # misfire_grace_time: APScheduler descarta el disparo si el proceso
        # estaba ocupado/reiniciando en ese segundo exacto — sin log ni error.
        # El bot reinicia en cada deploy (16 veces en 7 días de ago-2026) y por
        # eso el cierre de caja NUNCA corrió desde que se creó el 30-jun.
        misfire_grace_time=3600,
        coalesce=True,
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
    # Alerta takeover pendiente: cada 30 min, avisa si hay sesiones sin respuesta humana
    # >2h (horario hábil) o >12h (fuera de horario).
    scheduler.add_job(
        _job_takeover_pendiente_alert,
        CronTrigger(minute="10,40", timezone=_CLT),
        id="takeover_pendiente_alert",
        replace_existing=True,
    )
    # Reporte periódico de estado al admin cada 30 min
    # (+8 min para separar del no-show_check (:02/:32) y reporte progreso (:05)
    scheduler.add_job(
        _job_admin_status_report,
        CronTrigger(minute="8,38", timezone=_CLT),
        id="admin_status_report",
        replace_existing=True,
    )
    # Limpieza de sesiones stuck en WAIT_* cada hora
    scheduler.add_job(
        _job_cleanup_stuck_sessions,
        CronTrigger(hour="7-22", minute="15", timezone=_CLT),
        id="cleanup_stuck_sessions",
        replace_existing=True,
        # misfire_grace_time: APScheduler descarta el disparo si el proceso
        # estaba ocupado/reiniciando en ese segundo exacto — sin log ni error.
        # El bot reinicia en cada deploy (16 veces en 7 días de ago-2026) y por
        # eso el cierre de caja NUNCA corrió desde que se creó el 30-jun.
        misfire_grace_time=3600,
        coalesce=True,
    )
    # Regenerar heatmap_cache.json cada 6h (00:05, 06:05, 12:05, 18:05 CLT)
    scheduler.add_job(
        _job_regenerate_heatmap_cache,
        CronTrigger(hour="*/6", minute=5, timezone=_CLT),
        id="regenerate_heatmap_cache",
        replace_existing=True,
        # misfire_grace_time: APScheduler descarta el disparo si el proceso
        # estaba ocupado/reiniciando en ese segundo exacto — sin log ni error.
        # El bot reinicia en cada deploy (16 veces en 7 días de ago-2026) y por
        # eso el cierre de caja NUNCA corrió desde que se creó el 30-jun.
        misfire_grace_time=3600,
        coalesce=True,
    )
    # Dashboards semanales a profesionales: lunes 09:00 CLT
    scheduler.add_job(
        _job_enviar_dashboards_semanales,
        CronTrigger(day_of_week="mon", hour=9, minute=18, timezone=_CLT),  # 9:18 (era 9:00)
        id="dashboards_semanales_profesionales",
        replace_existing=True,
        # misfire_grace_time: APScheduler descarta el disparo si el proceso
        # estaba ocupado/reiniciando en ese segundo exacto — sin log ni error.
        # El bot reinicia en cada deploy (16 veces en 7 días de ago-2026) y por
        # eso el cierre de caja NUNCA corrió desde que se creó el 30-jun.
        misfire_grace_time=3600,
        coalesce=True,
    )
    # Resumen diario a profesionales: lun-sáb 07:00 CLT (permiso resumen_diario_07)
    scheduler.add_job(
        _job_resumen_diario_profesionales,
        CronTrigger(day_of_week="mon-sat", hour=7, minute=12, timezone=_CLT),  # 7:12 (era 7:00; chocaba con waitlist_check, ambos Medilink-heavy)
        id="resumen_diario_profesionales",
        replace_existing=True,
        # misfire_grace_time: APScheduler descarta el disparo si el proceso
        # estaba ocupado/reiniciando en ese segundo exacto — sin log ni error.
        # El bot reinicia en cada deploy (16 veces en 7 días de ago-2026) y por
        # eso el cierre de caja NUNCA corrió desde que se creó el 30-jun.
        misfire_grace_time=3600,
        coalesce=True,
    )
    # Resumen semanal a profesionales: domingo 19:00 CLT (permiso resumen_semanal_dom)
    scheduler.add_job(
        _job_resumen_semanal_profesionales,
        CronTrigger(day_of_week="sun", hour=19, minute=0, timezone=_CLT),
        id="resumen_semanal_profesionales",
        replace_existing=True,
        # misfire_grace_time: APScheduler descarta el disparo si el proceso
        # estaba ocupado/reiniciando en ese segundo exacto — sin log ni error.
        # El bot reinicia en cada deploy (16 veces en 7 días de ago-2026) y por
        # eso el cierre de caja NUNCA corrió desde que se creó el 30-jun.
        misfire_grace_time=3600,
        coalesce=True,
    )
    # No-show check: cada 30 min entre 09:00 y 21:00 CLT (permiso notif_no_show)
    # +2 min para escalonar respecto a recordatorios que parten a :00
    scheduler.add_job(
        _job_no_show_check,
        CronTrigger(hour="9-21", minute="2,32", timezone=_CLT),
        id="no_show_check_profesionales",
        replace_existing=True,
        # misfire_grace_time: APScheduler descarta el disparo si el proceso
        # estaba ocupado/reiniciando en ese segundo exacto — sin log ni error.
        # El bot reinicia en cada deploy (16 veces en 7 días de ago-2026) y por
        # eso el cierre de caja NUNCA corrió desde que se creó el 30-jun.
        misfire_grace_time=3600,
        coalesce=True,
    )
    # Horas vacías D+1: diariamente a las 14:00 CLT
    # Detecta slots libres del día siguiente y notifica proactivamente a pacientes elegibles.
    scheduler.add_job(
        _job_horas_vacias_dia_siguiente,
        CronTrigger(hour=14, minute=0, timezone=_CLT),
        id="horas_vacias_dia_siguiente",
        replace_existing=True,
        # misfire_grace_time: APScheduler descarta el disparo si el proceso
        # estaba ocupado/reiniciando en ese segundo exacto — sin log ni error.
        # El bot reinicia en cada deploy (16 veces en 7 días de ago-2026) y por
        # eso el cierre de caja NUNCA corrió desde que se creó el 30-jun.
        misfire_grace_time=3600,
        coalesce=True,
    )
    # Telemedicina recordatorios: cada 15 min entre 7 y 22 CLT
    # Desfasado a :07/:22/:37/:52 — antes corría en :00/:15/:30/:45, la misma
    # grilla que recordatorios_2h → ráfagas simultáneas a Medilink = 429
    # (1.640 errores en minuto :00, peor burst 290 el 2026-06-09 16:00 CLT).
    scheduler.add_job(
        _job_telemedicina_recordatorios,
        CronTrigger(minute="7,22,37,52", hour="7-22", timezone=_CLT),
        id="telemedicina_recordatorios",
        replace_existing=True,
        # misfire_grace_time: APScheduler descarta el disparo si el proceso
        # estaba ocupado/reiniciando en ese segundo exacto — sin log ni error.
        # El bot reinicia en cada deploy (16 veces en 7 días de ago-2026) y por
        # eso el cierre de caja NUNCA corrió desde que se creó el 30-jun.
        misfire_grace_time=3600,
        coalesce=True,
    )
    # Cross-sell por dx tags: diario 11:00 CLT.
    # INACTIVO por defecto (CROSS_SELL_ACTIVE=false).
    # Activar solo después de piloto N=5 y confirmación de Rodrigo.
    scheduler.add_job(
        _job_crosssell_dx,
        CronTrigger(hour=11, minute=18, timezone=_CLT),  # 11:18 (era 11:00)
        id="crosssell_dx_diario",
        replace_existing=True,
        # misfire_grace_time: APScheduler descarta el disparo si el proceso
        # estaba ocupado/reiniciando en ese segundo exacto — sin log ni error.
        # El bot reinicia en cada deploy (16 veces en 7 días de ago-2026) y por
        # eso el cierre de caja NUNCA corrió desde que se creó el 30-jun.
        misfire_grace_time=3600,
        coalesce=True,
    )
    # Custom Audiences Meta: diario 04:00 CLT
    scheduler.add_job(
        _job_custom_audiences_sync,
        CronTrigger(hour=4, minute=0, timezone=_CLT),
        id="custom_audiences_sync_diario",
        replace_existing=True,
        # misfire_grace_time: APScheduler descarta el disparo si el proceso
        # estaba ocupado/reiniciando en ese segundo exacto — sin log ni error.
        # El bot reinicia en cada deploy (16 veces en 7 días de ago-2026) y por
        # eso el cierre de caja NUNCA corrió desde que se creó el 30-jun.
        misfire_grace_time=3600,
        coalesce=True,
    )
    # Winback BI: L-V 10:05 CLT — usa bi.v_winback_cohortes_contactables
    # INACTIVO por defecto (WINBACK_ACTIVE=false en .env).
    # Activar solo después de confirmar aprobación de templates en Meta.
    scheduler.add_job(
        _job_winback_bi,
        CronTrigger(day_of_week="mon-fri", hour=10, minute=5, timezone=_CLT),
        id="winback_bi_diario",
        replace_existing=True,
        misfire_grace_time=600,
    )
    # Marketing consent blast: L-V 11:00 CLT — envía consent_marketing_v1 (UTILITY)
    # a phones en v_winback_cohortes_contactables sin registro en marketing_consent.
    # Corre DESPUÉS del dental (10:30) porque dental tiene mayor margen — sus
    # candidatos ya están excluidos del pool general vía v_winback_cohortes_contactables.
    scheduler.add_job(
        _job_marketing_consent_blast,
        CronTrigger(day_of_week="mon-fri", hour=11, minute=12, timezone=_CLT),  # 11:12 (era 11:00; sigue corriendo después del dental 10:30)
        id="marketing_consent_blast",
        replace_existing=True,
        misfire_grace_time=600,
    )
    # Watchdog auto-pausa/reactivación blast: cada 4h a los :15 (03:15, 07:15, 11:15, 15:15, 19:15, 23:15)
    scheduler.add_job(
        _job_watchdog_blast,
        CronTrigger(hour="*/4", minute=15, timezone=_CLT),
        id="watchdog_blast",
        replace_existing=True,
        # misfire_grace_time: APScheduler descarta el disparo si el proceso
        # estaba ocupado/reiniciando en ese segundo exacto — sin log ni error.
        # El bot reinicia en cada deploy (16 veces en 7 días de ago-2026) y por
        # eso el cierre de caja NUNCA corrió desde que se creó el 30-jun.
        misfire_grace_time=3600,
        coalesce=True,
    )
    # Watchdog entrega real de templates: cada 30 min a los :20/:50
    # Detecta apagones de facturación Meta (error 131042). Canal email primario
    # (no depende de templates WA). Histéresis simétrica — alerta solo al cambiar estado.
    scheduler.add_job(
        _job_watchdog_entrega,
        CronTrigger(minute="20,50", timezone=_CLT),
        id="watchdog_entrega",
        replace_existing=True,
    )
    # Dental win-back: L-V 10:35 CLT — campanas dentales focalizadas.
    # Cohortes por profesional: ortodoncia → endo/implanto → odonto general → estética.
    # INACTIVO por defecto (DENTAL_WINBACK_ACTIVE=false en .env).
    scheduler.add_job(
        _job_dental_winback,
        CronTrigger(day_of_week="mon-fri", hour=10, minute=35, timezone=_CLT),
        id="dental_winback_diario",
        replace_existing=True,
        misfire_grace_time=600,
    )
    # Dental consent blast: L-V 10:30 CLT — envía consent_dental_v1 (UTILITY).
    # PRIORIDAD sobre el general (corre antes) porque dental tiene mayor margen.
    # General arranca a las 11:00 sobre pool ya filtrado (sin dental candidatos).
    scheduler.add_job(
        _job_dental_consent_blast,
        CronTrigger(day_of_week="mon-fri", hour=10, minute=30, timezone=_CLT),
        id="dental_consent_blast",
        replace_existing=True,
        misfire_grace_time=600,
    )
    # Reporte diario win-back a Rodrigo: L-V 19:00 CLT
    scheduler.add_job(
        _job_winback_daily_report,
        CronTrigger(day_of_week="mon-fri", hour=19, minute=0, timezone=_CLT),
        id="winback_daily_report",
        replace_existing=True,
        # misfire_grace_time: APScheduler descarta el disparo si el proceso
        # estaba ocupado/reiniciando en ese segundo exacto — sin log ni error.
        # El bot reinicia en cada deploy (16 veces en 7 días de ago-2026) y por
        # eso el cierre de caja NUNCA corrió desde que se creó el 30-jun.
        misfire_grace_time=3600,
        coalesce=True,
    )
    # Reporte conversión Promo Dental Junio: diario 09:00 CLT (solo si ventana 24h abierta; no-op tras junio)
    scheduler.add_job(
        _job_dental_promo_report,
        CronTrigger(hour=9, minute=24, timezone=_CLT),  # 9:24 (era 9:00)
        id="dental_promo_report",
        replace_existing=True,
        # misfire_grace_time: APScheduler descarta el disparo si el proceso
        # estaba ocupado/reiniciando en ese segundo exacto — sin log ni error.
        # El bot reinicia en cada deploy (16 veces en 7 días de ago-2026) y por
        # eso el cierre de caja NUNCA corrió desde que se creó el 30-jun.
        misfire_grace_time=3600,
        coalesce=True,
    )
    # Reporte semanal de salud del bot: lunes 09:00 CLT
    scheduler.add_job(
        _job_health_report,
        CronTrigger(day_of_week="mon", hour=9, minute=36, timezone=_CLT),  # 9:36 (era 9:00)
        id="health_report_semanal",
        replace_existing=True,
        # misfire_grace_time: APScheduler descarta el disparo si el proceso
        # estaba ocupado/reiniciando en ese segundo exacto — sin log ni error.
        # El bot reinicia en cada deploy (16 veces en 7 días de ago-2026) y por
        # eso el cierre de caja NUNCA corrió desde que se creó el 30-jun.
        misfire_grace_time=3600,
        coalesce=True,
    )
    # Reporte de caja al dueño: cada mañana 08:45 CLT (cuadre de ayer + efectivo en caja)
    scheduler.add_job(
        _job_caja_report,
        CronTrigger(hour=8, minute=45, timezone=_CLT),
        id="caja_report_diario",
        replace_existing=True,
        # misfire_grace_time: APScheduler descarta el disparo si el proceso
        # estaba ocupado/reiniciando en ese segundo exacto — sin log ni error.
        # El bot reinicia en cada deploy (16 veces en 7 días de ago-2026) y por
        # eso el cierre de caja NUNCA corrió desde que se creó el 30-jun.
        misfire_grace_time=3600,
        coalesce=True,
    )
    # M4: digest semanal a recepción de pacientes >14 días en waitlist (lunes 09:30 CLT)
    from jobs import _job_waitlist_digest_semanal, _job_followup_info
    from persistencia import job_persistencia_contacto as _job_persistencia_contacto
    scheduler.add_job(
        _job_waitlist_digest_semanal,
        CronTrigger(day_of_week="mon", hour=9, minute=30, timezone=_CLT),
        id="waitlist_digest_semanal",
        replace_existing=True,
        # misfire_grace_time: APScheduler descarta el disparo si el proceso
        # estaba ocupado/reiniciando en ese segundo exacto — sin log ni error.
        # El bot reinicia en cada deploy (16 veces en 7 días de ago-2026) y por
        # eso el cierre de caja NUNCA corrió desde que se creó el 30-jun.
        misfire_grace_time=3600,
        coalesce=True,
    )
    # M5: follow-up proactivo a intent=info sin cita (cada 5 min, gated FOLLOWUP_INFO_ENABLED)
    scheduler.add_job(
        _job_followup_info,
        CronTrigger(minute="*/5", timezone=_CLT),
        id="followup_info",
        replace_existing=True,
    )
    # Carril de persistencia (2026-07-13): segundo toque a consultas de
    # agendamiento abiertas que el reenganche existente no rescató. GATED OFF
    # por defecto (PERSISTENCIA_ACTIVE) — la función retorna de inmediato si
    # el flag no está encendido, así que registrar el job es inerte/seguro.
    scheduler.add_job(
        _job_persistencia_contacto,
        CronTrigger(minute="*/15", timezone=_CLT),
        id="persistencia_contacto",
        replace_existing=True,
    )
    # B6: Synthetic check del agendamiento — ejercita buscar_primer_dia("Medicina General")
    # READ-ONLY (sin crear citas). Gateado por SYNTHETIC_CHECK_ENABLED (default true).
    # Alertas via alerta_oob al 3er fallo consecutivo; silencio al recuperarse.
    _synthetic_fails: list[int] = [0]   # contador mutable compartido en el cierre

    async def _job_synthetic_check_agendar():
        from config import SYNTHETIC_CHECK_ENABLED as _sc_enabled
        if not _sc_enabled:
            return
        import asyncio as _aio
        try:
            from medilink import buscar_primer_dia as _bpd
            # Timeout estricto: si tarda > 15s, es anomalía
            _, slots = await _aio.wait_for(_bpd("Medicina General", dias_adelante=7), timeout=15)
            if slots:
                # Recuperación: si había fallos acumulados, avisar y resetear
                if _synthetic_fails[0] >= 3:
                    try:
                        from alertas_oob import alerta_oob as _aob
                        await _aob(
                            f"*Synthetic check CMC recuperado* (Medicina General)\n"
                            f"Volvio a devolver slots tras {_synthetic_fails[0]} fallo(s)"
                        )
                    except Exception:  # noqa: BLE001
                        pass
                _synthetic_fails[0] = 0
                log.debug("synthetic_check: OK — %d slots disponibles", len(slots))
            else:
                # Slots vacíos puede ser normal (fin de semana largo, agenda llena)
                # No es un fallo de infraestructura por sí solo; solo loguear
                log.info("synthetic_check: buscar_primer_dia devolvio 0 slots — no anomalia")
                _synthetic_fails[0] = 0
        except Exception as exc:  # noqa: BLE001
            _synthetic_fails[0] += 1
            log.warning("synthetic_check: fallo %d — %s", _synthetic_fails[0], exc)
            if _synthetic_fails[0] >= 3:
                try:
                    from alertas_oob import alerta_oob as _aob
                    await _aob(
                        f"*Synthetic agendar FALLA* (CMC)\n"
                        f"buscar_primer_dia(Medicina General) falla {_synthetic_fails[0]} veces seguidas\n"
                        f"Error: {str(exc)[:200]}"
                    )
                except Exception:  # noqa: BLE001
                    pass

    scheduler.add_job(
        _job_synthetic_check_agendar,
        CronTrigger(minute="*/15", timezone=_CLT),
        id="synthetic_check_agendar",
        replace_existing=True,
        misfire_grace_time=600,
        coalesce=True,
    )
    # Primera generación al arrancar (sin await — no bloquear startup)
    import asyncio as _asyncio_startup
    _asyncio_startup.get_event_loop().create_task(_job_regenerate_heatmap_cache())
    # Check one-shot post-arranque: reservas que murieron en vuelo en el proceso
    # anterior (reserva_en_vuelo sin reserva_resultado, incidente Matías
    # 2026-06-05) → marca y alerta a recepción. 10s de gracia para no competir
    # con el arranque.
    async def _startup_reservas_huerfanas():
        await _asyncio_startup.sleep(10)
        try:
            from jobs import startup_reservas_huerfanas_check
            await startup_reservas_huerfanas_check()
        except Exception as e:  # noqa: BLE001 — nunca tumbar el startup por esto
            log.error("reservas_huerfanas: check de arranque falló: %s", e)
        try:
            from jobs import startup_mensajes_huerfanos_check
            await startup_mensajes_huerfanos_check()
        except Exception as e:  # noqa: BLE001
            log.error("mensajes_huerfanos: check de arranque falló: %s", e)
    _asyncio_startup.get_event_loop().create_task(_startup_reservas_huerfanas())
    scheduler.start()
    log.info(
        "Scheduler iniciado — recordatorios 09:00 · recordatorios 2h cada 15min · cumpleaños 10:00 · "
        "post-consulta 10:00 · reactivación lun 10:30 · adherencia kine 11:00 · "
        "control 11:30 · cross-sell kine mié 10:30 · winback-bi L-V 10:05 (ACTIVE=%s) · "
        "sync caché 23:50 · watchdog medilink 1min · doctor alerts cada 5min + reportes 09/12/16/20 · "
        "watchdog blast cada 4h · reporte diario winback L-V 19:00 · health report lunes 09:00",
        os.getenv('WINBACK_ACTIVE', 'false'),
    )
    yield
    scheduler.shutdown()


# ── FastAPI app ──────────────────────────────────────────────────────────────

app = FastAPI(title="CMC WhatsApp Bot", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent.parent / "static")), name="static")

# Landing Adkun "Digital Products" — sitio React/Tailwind servido como directorio estático
# (usa rutas relativas assets/ + js/, por eso se monta en vez de retornar HTML suelto).
_ADKUN_GPT_DIR = Path(__file__).parent.parent / "sites" / "adkun-gpt"
if _ADKUN_GPT_DIR.exists():
    app.mount("/adkun/gpt", StaticFiles(directory=str(_ADKUN_GPT_DIR), html=True), name="adkun_gpt")

# CORS restrictivo
_ALLOWED_ORIGINS = [
    "https://agentecmc.cl",
    # MED-5: orígenes HTTP públicos eliminados (http://agentecmc.cl y
    # http://157.245.13.107:8001) — con allow_credentials=True un MitM en HTTP
    # podía interceptar cookies de sesión. Prod sirve solo HTTPS.
    # La home pública vive en WordPress (centromedicocarampangue.cl) y consume
    # /api/google-rating del lado cliente para mostrar reseñas reales de Google.
    "https://centromedicocarampangue.cl",
    "https://www.centromedicocarampangue.cl",
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
from autopilot.routes import router as autopilot_router  # noqa: E402
app.include_router(autopilot_router)
app.include_router(portal_routes.router)
from alma_brain.routes import router as alma_brain_router  # noqa: E402
app.include_router(alma_brain_router)
from alma_agents.routes import router as alma_agents_router  # noqa: E402
app.include_router(alma_agents_router)
import alma_control_routes  # noqa: E402
app.include_router(alma_control_routes.router)

import vuelos_routes
app.include_router(vuelos_routes.router)

import agenda_routes
app.include_router(agenda_routes.router)

import agendador_routes
app.include_router(agendador_routes.router)  # agendador público (cada endpoint gateado)
import checkin_routes; app.include_router(checkin_routes.router); checkin_routes.ensure_checkin_table()
import comparador_routes; comparador_routes.register_comparador_routes(app)  # Comparador BI
import ebitda_routes; ebitda_routes.register_ebitda_routes(app)  # EBITDA / Resultado  # Check-in QR (gateado CHECKIN_ENABLED)
import panel_dia_routes; panel_dia_routes.register_panel_dia_routes(app)  # Panel del Día — datos reales (N1/N2/N3)
import remuneraciones_routes; remuneraciones_routes.register_remuneraciones_routes(app)  # Panel del Día N4 — remuneraciones
import captacion_routes; captacion_routes.register_captacion_routes(app)  # Dashboard captación (cómo nos conociste)
import mg_abandono_routes; mg_abandono_routes.register_mg_abandono_routes(app)  # métrica abandono Medicina General
import persistencia_routes; persistencia_routes.register_persistencia_routes(app)  # carril de persistencia (GATED PERSISTENCIA_ACTIVE)
import marketing_routes; marketing_routes.register_marketing_routes(app)  # Estudio de Marketing (panel publicidad/contenido)
import roas_routes; roas_routes.register_roas_routes(app)  # ROAS por campaña Meta × caja real (/alma/roas)
import agenda_ticker_routes; agenda_ticker_routes.register_agenda_ticker_routes(app)  # Monitor de agendamientos en vivo (/alma/agenda-en-vivo)
import ausentismo_routes; ausentismo_routes.register_ausentismo_routes(app)  # Ausentismo — ranking pacientes que no asisten (/alma/ausentismo)
import numero_equivocado; numero_equivocado.register_numero_equivocado_routes(app)  # Números reciclados: limpieza 4 capas desde panel v2
import direccion_routes; direccion_routes.register_direccion_routes(app)  # Plan de Dirección (tracker formación dueño)
import conciliacion_transferencias_routes; conciliacion_transferencias_routes.register_conciliacion_transferencias_routes(app)  # Conciliación transferencias × correos banco + sugerencias de pago (/alma/conciliacion-transferencias)
import mapa_centro_routes; mapa_centro_routes.register_mapa_centro_routes(app)  # Mapa del Centro: inventario de TODO lo que hay en marcha, con sondas en vivo (/alma/mapa)

import pagos_routes
app.include_router(pagos_routes.router)
import comprobantes_routes; app.include_router(comprobantes_routes.router)  # Cola comprobantes WhatsApp (/alma/comprobantes)
import recepcion_kanban_routes
app.include_router(recepcion_kanban_routes.router)
import caja_diaria_routes; app.include_router(caja_diaria_routes.router); caja_diaria_routes.ensure_caja_diaria_table()  # Caja Diaria (libro de caja + depósitos)
pagos_routes.ensure_pagos_table()  # DDL idempotente al arrancar

import abonos_routes
app.include_router(abonos_routes.router)

import pagos_medilink_routes
app.include_router(pagos_medilink_routes.router)
abonos_routes.ensure_abonos_table()  # DDL idempotente al arrancar

import envios_routes
app.include_router(envios_routes.router)

import conciliacion_routes
app.include_router(conciliacion_routes.router)

import inventario_routes
app.include_router(inventario_routes.router)
inventario_routes.seed_if_empty()  # DDL + siembra catálogo MayorDent al arrancar
import proveedores_routes; app.include_router(proveedores_routes.router); proveedores_routes.seed_if_empty()

# ── Módulos Profesionales (analítica clínica BI-driven por especialidad) ──
import ortodoncia_routes; app.include_router(ortodoncia_routes.router); ortodoncia_routes.ensure_ortodoncia_plan_table()  # Seguimiento Ortodoncia
import kine_routes; app.include_router(kine_routes.router); kine_routes.ensure_kine_plan_table()  # Programa Kinesiología
import programas; app.include_router(programas.router); programas.ensure_programa_plan_table()  # Motor de Programas Clínicos por especialidad

# ── Módulos clínicos integrales (pacientes, interconsultas, esterilización, finanzas, equipo, documentos, habilitación) ──
import pacientes_routes; app.include_router(pacientes_routes.router)
import interconsultas_routes; app.include_router(interconsultas_routes.router)
import esterilizacion_routes; app.include_router(esterilizacion_routes.router)
import finanzas_routes; app.include_router(finanzas_routes.router)
import equipo_routes; app.include_router(equipo_routes.router); equipo_routes.seed_if_empty()
import documentos_routes; app.include_router(documentos_routes.router); documentos_routes.seed_if_empty()
import habilitacion_routes; app.include_router(habilitacion_routes.router); habilitacion_routes.seed_if_empty()
import mantencion_routes; app.include_router(mantencion_routes.router); mantencion_routes.seed_if_empty()
import calidad_routes; app.include_router(calidad_routes.router)
import examenes_routes; app.include_router(examenes_routes.router)
import tareas_routes; app.include_router(tareas_routes.router)
import checklist_routes; app.include_router(checklist_routes.router); checklist_routes.seed_if_empty()
import liquidaciones_routes; app.include_router(liquidaciones_routes.router)
import tablero_routes; app.include_router(tablero_routes.router)
import patient_source_routes; app.include_router(patient_source_routes.router)  # Canal declarado ("¿cómo nos conoció?")
import promo_postconsent; app.include_router(promo_postconsent.router)  # Riel consent aceptado → promo dental diferida (gated OFF)
import eco_prep; app.include_router(eco_prep.router)  # Preparación pre-examen eco (gated OFF, template pendiente Meta)
import rieles_pnl; app.include_router(rieles_pnl.router)  # P&L unificado de rieles (Sala de Máquinas + Autopilot + Director)
import grafo_routes; grafo_routes.register_grafo_routes(app)  # Cerebro Alma (grafo del organismo)

import audit_routes  # vista /admin/auditoria — hallazgos del enjambre horario
app.include_router(audit_routes.router)

import print_routes; app.include_router(print_routes.router)  # Impresion remota → Alma Print

import abono_pago_routes; app.include_router(abono_pago_routes.router)  # Página pública /abono/{token} — confirmación auto de abonos por transferencia (gated ABONO_AUTO_ACTIVE, ver abono_transferencia.py)

# Cargar HTML del panel admin y portal paciente
_TEMPLATE_DIR = Path(__file__).parent.parent / "templates"
_ADMIN_HTML = (_TEMPLATE_DIR / "admin.html").read_text(encoding="utf-8")
_ADMIN_V2_HTML = (_TEMPLATE_DIR / "admin_v2.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "admin_v2.html").exists() else ""
_ADMIN_V3_HTML = (_TEMPLATE_DIR / "admin_v3.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "admin_v3.html").exists() else ""
_PORTAL_HTML = (_TEMPLATE_DIR / "portal.html").read_text(encoding="utf-8")
_PORTAL_V2_HTML = (_TEMPLATE_DIR / "portal_v2.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "portal_v2.html").exists() else ""
_PORTAL_V3_HTML = (_TEMPLATE_DIR / "portal_v3.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "portal_v3.html").exists() else ""
_PORTAL_V4_HTML = (_TEMPLATE_DIR / "portal_v4.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "portal_v4.html").exists() else ""
_PORTAL_INFORME_HTML = (_TEMPLATE_DIR / "portal_informe.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "portal_informe.html").exists() else ""
_ECOSISTEMA_HTML = (_TEMPLATE_DIR / "ecosistema.html").read_text(encoding="utf-8")
_DASHBOARD_HTML = (_TEMPLATE_DIR / "dashboard.html").read_text(encoding="utf-8")
_MEULEN_ECOSISTEMA_HTML = (_TEMPLATE_DIR / "meulen_ecosistema.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "meulen_ecosistema.html").exists() else ""
_MEULEN_DASHBOARD_HTML = (_TEMPLATE_DIR / "meulen_dashboard.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "meulen_dashboard.html").exists() else ""
_MEULEN_KPIS_HTML = (_TEMPLATE_DIR / "meulen_kpis.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "meulen_kpis.html").exists() else ""
_MENU_HTML = (_TEMPLATE_DIR / "menu.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "menu.html").exists() else ""
_CHEQUEOS_HTML = (_TEMPLATE_DIR / "chequeos.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "chequeos.html").exists() else ""
_EMPRESAS_HTML = (_TEMPLATE_DIR / "empresas.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "empresas.html").exists() else ""
_PROYECTOS2026_HTML = (_TEMPLATE_DIR / "proyectos2026.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "proyectos2026.html").exists() else ""
_LANDING_HTML = (_TEMPLATE_DIR / "landing.html").read_text(encoding="utf-8")
_SALA_ESPERA_HTML = (_TEMPLATE_DIR / "sala_espera.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "sala_espera.html").exists() else ""
_ADMIN_FUNCIONES_HTML = (_TEMPLATE_DIR / "admin_cmc_funciones.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "admin_cmc_funciones.html").exists() else ""
_SITIO_V3_HTML = (_TEMPLATE_DIR / "sitio-v3.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "sitio-v3.html").exists() else ""
_SITIO_V2_HTML = (_TEMPLATE_DIR / "sitio-v2.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "sitio-v2.html").exists() else ""
_SITIO_FLAGSHIP_HTML = (_TEMPLATE_DIR / "sitio-flagship.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "sitio-flagship.html").exists() else ""
_SITIO_V4_HTML = (_TEMPLATE_DIR / "sitio-v4.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "sitio-v4.html").exists() else ""
_SITIO_V5_HTML = (_TEMPLATE_DIR / "sitio-v5.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "sitio-v5.html").exists() else ""
_SITIO_V6_HTML = (_TEMPLATE_DIR / "sitio-v6.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "sitio-v6.html").exists() else ""
_SITIO_V7_HTML = (_TEMPLATE_DIR / "sitio-v7.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "sitio-v7.html").exists() else ""
_SITIO_HTML = (_TEMPLATE_DIR / "sitio.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "sitio.html").exists() else ""
_RIOMONTE_HTML = (_TEMPLATE_DIR / "riomonte.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "riomonte.html").exists() else ""
_RIOMONTE_PROFORMA_HTML = (_TEMPLATE_DIR / "riomonte_proforma.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "riomonte_proforma.html").exists() else ""
_ARQUETIX_MEMO_HTML = (_TEMPLATE_DIR / "arquetix_memo.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "arquetix_memo.html").exists() else ""
_ARQUETIX_PITCH_HTML = (_TEMPLATE_DIR / "arquetix_pitch.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "arquetix_pitch.html").exists() else ""
_BLOG_DIR = _TEMPLATE_DIR / "blog"
_HEATMAP_COMUNAS_HTML = (_TEMPLATE_DIR / "heatmap_comunas.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "heatmap_comunas.html").exists() else ""
# Mapa de direcciones: es un ARTEFACTO GENERADO (scripts/geocode_direcciones.py,
# refresh diario 05:00), no un template. Vive en data/ (gitignored) para no dejar
# el árbol de prod sucio, y se lee EN CADA REQUEST — antes se leía acá al importar,
# así que el refresh nocturno no se veía hasta reiniciar el servicio.
_HEATMAP_DIRECCIONES_FILE = _TEMPLATE_DIR.parent / "data" / "heatmap_direcciones.html"


def _leer_heatmap_direcciones() -> str:
    """HTML del mapa de direcciones, fresco del disco. '' si aún no se generó."""
    try:
        return _HEATMAP_DIRECCIONES_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except OSError as e:
        log.warning("No se pudo leer el mapa de direcciones: %s", e)
        return ""
_SEO_DASHBOARD_HTML = (_TEMPLATE_DIR / "seo_dashboard.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "seo_dashboard.html").exists() else ""
_CRECIMIENTO_PERSONAL_HTML = (_TEMPLATE_DIR / "crecimiento_personal.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "crecimiento_personal.html").exists() else ""
_RUTA_PERSONAL_HTML = (_TEMPLATE_DIR / "ruta_personal.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "ruta_personal.html").exists() else ""
_BRUJULA_HTML = (_TEMPLATE_DIR / "brujula_personal.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "brujula_personal.html").exists() else ""
_CAMINOS_HTML = (_TEMPLATE_DIR / "caminos_personal.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "caminos_personal.html").exists() else ""
_PERSONAL_HUB_HTML = (_TEMPLATE_DIR / "personal_hub.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "personal_hub.html").exists() else ""
_META_DASHBOARD_HTML = (_TEMPLATE_DIR / "meta_dashboard.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "meta_dashboard.html").exists() else ""
_HORIZONTE_DASHBOARD_HTML = (_TEMPLATE_DIR / "horizonte_dashboard.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "horizonte_dashboard.html").exists() else ""
_CAMINO_50M_HTML = (_TEMPLATE_DIR / "camino_50m.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "camino_50m.html").exists() else ""
_PRIVACIDAD_HTML = (_TEMPLATE_DIR / "privacidad.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "privacidad.html").exists() else ""
_PROFESIONALES_CMC_HTML = (_TEMPLATE_DIR / "profesionales_cmc.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "profesionales_cmc.html").exists() else ""
_TRAUMATOLOGO_CURANILAHUE_HTML = (_TEMPLATE_DIR / "traumatologo-curanilahue.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "traumatologo-curanilahue.html").exists() else ""
_OTORRINO_CURANILAHUE_HTML = (_TEMPLATE_DIR / "otorrino-curanilahue.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "otorrino-curanilahue.html").exists() else ""
_GINECOLOGO_CURANILAHUE_HTML = (_TEMPLATE_DIR / "ginecologo-curanilahue.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "ginecologo-curanilahue.html").exists() else ""
_DENTISTA_CURANILAHUE_HTML = (_TEMPLATE_DIR / "dentista-curanilahue.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "dentista-curanilahue.html").exists() else ""
_LANDING_ORTODONCIA_HTML = (_TEMPLATE_DIR / "landing_ortodoncia.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "landing_ortodoncia.html").exists() else ""
_ADKUN_COMPANY_HTML = (_TEMPLATE_DIR / "adkun_company_board.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "adkun_company_board.html").exists() else ""
_ADKUN_LANDING_HTML = (_TEMPLATE_DIR / "adkun_landing.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "adkun_landing.html").exists() else ""
_ALMA_PRODUCT_HTML = (_TEMPLATE_DIR / "alma_product_board.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "alma_product_board.html").exists() else ""


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
        "claude_state":     "down" if is_claude_down() else "up",
        "claude_reason":    claude_down_reason() if is_claude_down() else None,
        "intent_queue_depth": intent_queue_depth(),
        "waitlist_depth":     waitlist_depth(),
        "bsuid_mapped": bsuid["total"],
    }


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    """Recibe updates del bot de Telegram (consola de dueño). Valida el secret
    de Telegram (header) y procesa en background para responder rápido (Telegram
    reintenta si el webhook tarda)."""
    expected = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not expected or secret != expected:
        return Response(status_code=403)
    try:
        update = await request.json()
    except Exception:
        return Response(status_code=400)
    from telegram_console import handle_update
    _spawn_bg(handle_update(update), name="telegram_update")
    return {"ok": True}


@app.get("/landing", response_class=HTMLResponse)
def landing():
    """Landing page SEO del Centro Médico Carampangue."""
    return _LANDING_HTML


@app.get("/sala-espera", response_class=HTMLResponse)
@app.get("/sala", response_class=HTMLResponse)
def sala_espera():
    """Pantalla ambiental para la TV de la sala de espera (cálida, cercana)."""
    return _SALA_ESPERA_HTML


@app.get("/administradora", response_class=HTMLResponse)
def administradora_funciones():
    """Mapa del cargo Administradora del CMC × malla UTP Adm. de Empresas (formación)."""
    return _ADMIN_FUNCIONES_HTML


@app.get("/traumatologo-curanilahue", response_class=HTMLResponse)
def traumatologo_curanilahue():
    """Landing SEO — Traumatólogo en Curanilahue."""
    return _TRAUMATOLOGO_CURANILAHUE_HTML


@app.get("/otorrino-curanilahue", response_class=HTMLResponse)
def otorrino_curanilahue():
    """Landing SEO — Otorrinolaringólogo en Curanilahue."""
    return _OTORRINO_CURANILAHUE_HTML


@app.get("/ginecologo-curanilahue", response_class=HTMLResponse)
def ginecologo_curanilahue():
    """Landing SEO — Ginecólogo en Curanilahue."""
    return _GINECOLOGO_CURANILAHUE_HTML


@app.get("/dentista-curanilahue", response_class=HTMLResponse)
def dentista_curanilahue():
    """Landing SEO — Dentista en Curanilahue."""
    return _DENTISTA_CURANILAHUE_HTML


@app.get("/ortodoncia", response_class=HTMLResponse)
def landing_ortodoncia():
    """Landing SEO — Ortodoncia en Arauco y Carampangue."""
    return _LANDING_ORTODONCIA_HTML


@app.get("/sitio", response_class=HTMLResponse)
def sitio_v3():
    """Prototipo v3 del sitio web — público para revisión."""
    return _SITIO_V3_HTML


@app.get("/riomonte", response_class=HTMLResponse)
def riomonte_landing():
    """Landing page Ríomonte Clínica Dental — Puerto Montt (vertical en evaluación)."""
    return _RIOMONTE_HTML


@app.get("/riomonte/proforma", response_class=HTMLResponse)
def riomonte_proforma():
    """Proforma financiera Ríomonte 2.0 — análisis interno (Rodrigo · Sebastián · Juan)."""
    return _RIOMONTE_PROFORMA_HTML


@app.get("/arquetix-memo", response_class=HTMLResponse)
def arquetix_memo():
    """Deal memo Arquetix Health — co-founder + PO vertical clínicas (Rodrigo)."""
    return _ARQUETIX_MEMO_HTML


@app.get("/arquetix-pitch", response_class=HTMLResponse)
def arquetix_pitch():
    """Pitch deck 20 slides Arquetix Health — para reunión con equipo Arquetix."""
    return _ARQUETIX_PITCH_HTML


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
    return _render_sitio_dynamic(_SITIO_HTML, rating_data)


@app.get("/blog", response_class=HTMLResponse)
@app.get("/blog/", response_class=HTMLResponse)
async def blog_index():
    """Índice del blog: lista las 22 especialidades."""
    p = _TEMPLATE_DIR / "blog_index.html"
    if p.exists():
        return p.read_text(encoding="utf-8")
    return HTMLResponse("<h1>Blog</h1>", status_code=200)


# Comuna hubs — landing por localidad con todas las especialidades
_COMUNA_HUB_TPL = (_TEMPLATE_DIR / "comuna_hub.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "comuna_hub.html").exists() else ""

_COMUNA_SPECIALTIES = [
    ("medicina-general", "Medicina General", "Bono Fonasa $7.880", "Dr. Olavarría · Dr. Abarca", "Medicina"),
    ("cardiologia", "Cardiología", "Particular $40.000", "Dr. Miguel Millán", "Especialidades"),
    ("otorrinolaringologia", "Otorrinolaringología", "Particular $35.000", "Dr. Manuel Borrego", "Especialidades"),
    ("ginecologia", "Ginecología", "Particular $30.000", "Dr. Tirso Rejón", "Especialidades"),
    ("gastroenterologia", "Gastroenterología", "Particular $35.000", "Dr. Nicolás Quijano", "Especialidades"),
    ("kinesiologia", "Kinesiología", "Bono Fonasa $7.830", "Luis Armijo · Leo Etcheverry", "Rehabilitación"),
    ("nutricion", "Nutrición", "Bono Fonasa $4.770", "Gisela Pinto", "Bienestar"),
    ("psicologia-adulto", "Psicología Adulto", "Bono Fonasa $14.420", "J. Montalba · J.P. Rodríguez", "Salud Mental"),
    ("psicologia-infantil", "Psicología Infantil", "Bono Fonasa $14.420", "Jorge Montalba", "Salud Mental"),
    ("fonoaudiologia", "Fonoaudiología", "Particular $25.000", "Juana Arratia", "Rehabilitación"),
    ("matrona", "Matrona", "Tarifa Fonasa $16.000", "Sarai Gómez", "Salud Mujer"),
    ("podologia", "Podología", "$20.000–$35.000", "Andrea Guevara", "Bienestar"),
    ("ecografia", "Ecografía", "$35.000", "Dr. David Pardo", "Diagnóstico"),
    ("neurologia", "Neurología", "Particular $65.000", "Dra. Franca González", "Especialidades"),
    ("psiquiatria", "Psiquiatría", "Particular $60.000", "Dra. Cecilia Unibazo", "Salud Mental"),
    ("oftalmologia", "Oftalmología", "$15.000 (todos)", "TM Ana Celedón", "Diagnóstico"),
    ("masoterapia", "Masoterapia", "$17.990 (20 min)", "Paola Acosta", "Bienestar"),
    ("odontologia-general", "Odontología General", "Limpieza desde $30.000", "Dra. Burgos · Dr. Jiménez", "Dental"),
    ("ortodoncia", "Ortodoncia", "Brackets metálicos/estéticos", "Dra. Daniela Castillo", "Dental"),
    ("endodoncia", "Endodoncia", "Tratamiento conducto", "Dr. Fernando Fredes", "Dental"),
    ("implantologia", "Implantología", "Implante + corona desde $650.000", "Dra. Aurora Valdés", "Dental"),
    ("estetica-facial", "Estética Facial", "Evaluación $15.000", "Dra. Valentina Fuentealba", "Estética"),
]


@app.get("/comuna/{slug}", response_class=HTMLResponse)
@app.get("/comuna/{slug}/", response_class=HTMLResponse)
async def comuna_hub(slug: str):
    """Hub landing por comuna — agrupa todas las especialidades para esa localidad."""
    if slug not in COMUNAS_ARAUCO or not _COMUNA_HUB_TPL:
        return HTMLResponse("Not found", status_code=404)
    c = COMUNAS_ARAUCO[slug]
    nombre = c["nombre"]
    km = c.get("km", 0)
    minutos = c.get("min", 0)
    ruta = c.get("ruta", "")

    # Title con descriptor de distancia
    if km == 0:
        title = f"Médico y Dentista en {nombre} · Centro Médico Carampangue"
        km_txt = "en el centro de la localidad"
        min_txt = ""
        lead = (f"Centro Médico Carampangue está físicamente en {nombre}, en República 102. "
                f"23 profesionales y 22 especialidades médicas y dentales. Bono Fonasa MLE en sucursal con huella biométrica.")
    else:
        title = f"Médico y Dentista en {nombre} · CMC a {km} km ({minutos} min)"
        km_txt = f"a {km} km"
        min_txt = f" · {minutos} min" if minutos else ""
        lead = (f"Atendemos pacientes desde {nombre} ({c['tipo'] if 'tipo' in c else 'Provincia de Arauco'}). "
                f"Centro Médico Carampangue está a {km} km vía {ruta}. "
                f"23 profesionales · 22 especialidades · Bono Fonasa MLE · Agenda WhatsApp 24/7.")

    description = (f"Médico y dentista para pacientes de {nombre} (Provincia de Arauco). "
                   f"23 profesionales, 22 especialidades · Bono Fonasa MLE · "
                   f"a {km} km del centro" if km > 0 else
                   f"Médico y dentista en {nombre}: 23 profesionales, 22 especialidades. Bono Fonasa MLE.")

    wa_text = f"quiero%20agendar%20una%20hora%20desde%20{nombre.replace(' ', '%20')}"

    # Render specialty cards
    cards = []
    for sp_slug, sp_name, sp_price, sp_pro, sp_cat in _COMUNA_SPECIALTIES:
        url = f"/blog/{sp_slug}-{slug}" if km > 0 else f"/blog/{sp_slug}"
        cards.append(f'''<a class="spec-card" href="{url}">
        <span class="pill">{sp_cat}</span>
        <h3>{sp_name}</h3>
        <p>{sp_pro}</p>
        <div class="price">{sp_price}</div>
        <span class="read">Leer más
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><line x1="5" y1="12" x2="19" y2="12"/><polyline points="12 5 19 12 12 19"/></svg>
        </span>
      </a>''')
    cards_html = "\n      ".join(cards)

    # ItemList JSON for SEO
    import json as _json
    itemlist = {
        "@context": "https://schema.org",
        "@type": "ItemList",
        "name": f"Especialidades disponibles para pacientes de {nombre}",
        "numberOfItems": len(_COMUNA_SPECIALTIES),
        "itemListElement": [
            {
                "@type": "ListItem",
                "position": i + 1,
                "url": f"https://centromedicocarampangue.cl/blog/{sp[0]}-{slug}" if km > 0 else f"https://centromedicocarampangue.cl/blog/{sp[0]}",
                "name": f"{sp[1]} en {nombre}"
            } for i, sp in enumerate(_COMUNA_SPECIALTIES)
        ]
    }
    itemlist_json = _json.dumps(itemlist, ensure_ascii=False, indent=2)

    # Sección de contenido único por comuna (locomocion + contexto + razón hub)
    local_info = ""
    if slug in COMUNA_LOCAL_DATA:
        d = COMUNA_LOCAL_DATA[slug]
        razon_hub = d["razon"].replace("{esp}", "atención médica y dental")
        if slug == "carampangue":
            h2_li = "Atención médica y dental en Carampangue"
        else:
            h2_li = f"Atención médica y dental desde {nombre}"
        local_info = f"""
<section class="local-info" style="padding:56px 0;background:#f9fafb;border-top:1px solid #e5e7eb;border-bottom:1px solid #e5e7eb;">
  <div class="container">
    <h2 style="font-size:28px;line-height:1.2;margin:0 0 28px 0;color:#0f3f68;">{h2_li}</h2>
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:28px;">
      <article>
        <h3 style="font-size:17px;margin:0 0 8px 0;color:#1172ab;">Cómo llegar al CMC</h3>
        <p style="line-height:1.65;color:#374151;margin:0;">{d["locomocion"]}</p>
      </article>
      <article>
        <h3 style="font-size:17px;margin:0 0 8px 0;color:#1172ab;">{nombre} en la Provincia de Arauco</h3>
        <p style="line-height:1.65;color:#374151;margin:0;">{d["contexto"]}</p>
      </article>
      <article>
        <h3 style="font-size:17px;margin:0 0 8px 0;color:#1172ab;">¿Por qué venir desde {nombre}?</h3>
        <p style="line-height:1.65;color:#374151;margin:0;">{razon_hub}</p>
      </article>
    </div>
  </div>
</section>
"""

    html = (_COMUNA_HUB_TPL
            .replace("{{TITLE}}", title)
            .replace("{{DESCRIPTION}}", description)
            .replace("{{COMUNA_NOMBRE}}", nombre)
            .replace("{{COMUNA_SLUG}}", slug)
            .replace("{{KM_TXT}}", km_txt)
            .replace("{{MIN_TXT}}", min_txt)
            .replace("{{RUTA}}", ruta or "varias rutas")
            .replace("{{LEAD_TEXT}}", lead)
            .replace("{{WA_TEXT}}", wa_text)
            .replace("{{SPECIALTY_CARDS}}", cards_html)
            .replace("{{ITEMLIST_JSON}}", itemlist_json)
            .replace("{{LOCAL_INFO_SECTION}}", local_info))
    return html


@app.get("/comuna", response_class=HTMLResponse)
@app.get("/comuna/", response_class=HTMLResponse)
async def comuna_index():
    """Índice de comunas — landing rica con cards detalladas + JSON-LD."""
    import json as _json
    sede_html = ""
    cercanas = []
    arauco_g = []
    for slug, c in COMUNAS_ARAUCO.items():
        nombre = c["nombre"]
        km = c.get("km", 0)
        minutos = c.get("min", 0)
        ruta = c.get("ruta", "")
        if km == 0:
            distancia = "Sede principal"
            badge = "★ Sede"
            tipo_grupo = "sede"
        elif km <= 10:
            distancia = f"a {km} km · {minutos} min"
            badge = "Cercana"
            tipo_grupo = "cercana"
        else:
            distancia = f"a {km} km · {minutos} min"
            badge = "Provincia de Arauco"
            tipo_grupo = "provincia"
        card = f'''<a class="c-card" href="/comuna/{slug}">
        <span class="c-badge">{badge}</span>
        <h3>{nombre}</h3>
        <p class="c-dist">{distancia}</p>
        <p class="c-ruta">{ruta if ruta != "—" else "Carampangue"}</p>
        <span class="c-arrow">Ver especialidades →</span>
      </a>'''
        if tipo_grupo == "sede":
            sede_html = card
        elif tipo_grupo == "cercana":
            cercanas.append(card)
        else:
            arauco_g.append(card)

    itemlist = {
        "@context": "https://schema.org",
        "@type": "ItemList",
        "name": "Comunas de la Provincia de Arauco atendidas por el CMC",
        "numberOfItems": len(COMUNAS_ARAUCO),
        "itemListElement": [
            {"@type": "ListItem", "position": i + 1,
             "url": f"https://centromedicocarampangue.cl/comuna/{s}",
             "name": COMUNAS_ARAUCO[s]["nombre"]}
            for i, s in enumerate(COMUNAS_ARAUCO.keys())
        ],
    }
    breadcrumb = {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type":"ListItem","position":1,"name":"Inicio","item":"https://centromedicocarampangue.cl/"},
            {"@type":"ListItem","position":2,"name":"Servicios por comuna","item":"https://centromedicocarampangue.cl/comuna/"},
        ],
    }
    place = {
        "@context": "https://schema.org",
        "@type": "MedicalClinic",
        "@id": "https://centromedicocarampangue.cl/#clinic",
        "name": "Centro Médico Carampangue",
        "url": "https://centromedicocarampangue.cl/",
        "areaServed": [
            {"@type": "Place", "name": COMUNAS_ARAUCO[s]["nombre"]} for s in COMUNAS_ARAUCO
        ],
    }
    faq = {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": [
            {"@type":"Question","name":"¿A qué comunas atiende el Centro Médico Carampangue?","acceptedAnswer":{"@type":"Answer","text":"Atendemos pacientes de toda la Provincia de Arauco: Carampangue (sede), Laraquete, Ramadilla, Arauco, Curanilahue, Los Álamos, Lebu, Cañete, Contulmo y Tirúa. Recibimos también pacientes de Concepción, Coronel y comunas vecinas que prefieren evitar esperas del sistema público."}},
            {"@type":"Question","name":"¿Necesito ser de la zona para agendar?","acceptedAnswer":{"@type":"Answer","text":"No. Cualquier persona puede agendar una hora con cualquiera de nuestros 23 profesionales. La cercanía geográfica solo influye en el tiempo de viaje, no en la disponibilidad de hora."}},
            {"@type":"Question","name":"¿Cómo llego desde mi comuna?","acceptedAnswer":{"@type":"Answer","text":"Selecciona tu comuna en la lista para ver rutas exactas, kilómetros, tiempo de viaje y opciones de transporte público desde tu localidad hasta Carampangue."}},
            {"@type":"Question","name":"¿Aceptan Bono Fonasa para pacientes de otras comunas?","acceptedAnswer":{"@type":"Answer","text":"Sí. El Bono Fonasa MLE se emite en la sucursal con huella biométrica para Medicina General, Medicina Familiar, Kinesiología, Nutrición y Psicología (adulto e infantil), independiente de la comuna del paciente. Matrona tiene tarifa preferencial Fonasa."}},
        ],
    }

    cercanas_html = "\n      ".join(cercanas)
    arauco_html = "\n      ".join(arauco_g)
    itemlist_j = _json.dumps(itemlist, ensure_ascii=False)
    breadcrumb_j = _json.dumps(breadcrumb, ensure_ascii=False)
    place_j = _json.dumps(place, ensure_ascii=False)
    faq_j = _json.dumps(faq, ensure_ascii=False)

    return f"""<!DOCTYPE html>
<html lang="es-CL">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Servicios médicos por comuna · Provincia de Arauco | CMC</title>
<meta name="description" content="Centro Médico Carampangue atiende pacientes de toda la Provincia de Arauco: Carampangue, Arauco, Lebu, Cañete, Curanilahue, Los Álamos, Tirúa, Contulmo, Laraquete, Ramadilla. 23 profesionales · 22 especialidades · Bono Fonasa MLE.">
<meta name="keywords" content="centro médico Carampangue, médico Arauco, dentista Provincia Arauco, kinesiología Lebu, ginecología Curanilahue, ortodoncia Cañete, ecografía Los Álamos, agenda WhatsApp">
<meta name="robots" content="index, follow, max-image-preview:large">
<link rel="canonical" href="https://centromedicocarampangue.cl/comuna/">
<meta property="og:type" content="website">
<meta property="og:title" content="Servicios médicos por comuna · Provincia de Arauco | CMC">
<meta property="og:description" content="23 profesionales y 22 especialidades médicas y dentales para toda la Provincia de Arauco.">
<meta property="og:url" content="https://centromedicocarampangue.cl/comuna/">
<meta property="og:image" content="https://agentecmc.cl/static/og-image.png">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta property="og:site_name" content="Centro Médico Carampangue">
<meta property="og:locale" content="es_CL">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="Servicios médicos por comuna · Provincia de Arauco">
<meta name="twitter:description" content="23 profesionales · 22 especialidades · Bono Fonasa MLE · Agenda WhatsApp 24/7">
<meta name="twitter:image" content="https://agentecmc.cl/static/og-image.png">
<meta name="twitter:site" content="@CMCarampangue">
<script type="application/ld+json">
{itemlist_j}
</script>
<script type="application/ld+json">
{breadcrumb_j}
</script>
<script type="application/ld+json">
{place_j}
</script>
<script type="application/ld+json">
{faq_j}
</script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,700;9..144,800&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
*,*::before,*::after{{box-sizing:border-box}}
body{{font-family:Inter,-apple-system,Segoe UI,sans-serif;margin:0;color:#0a1a28;background:#FAF8F5;line-height:1.6}}
header{{padding:18px 24px;border-bottom:1px solid #e5e7eb;background:#fff}}
header a{{color:#1F7E8C;text-decoration:none;font-weight:600;font-size:14px}}
.hero{{padding:56px 24px 40px;text-align:center;max-width:840px;margin:0 auto}}
.hero .eyebrow{{font-size:12px;letter-spacing:1.5px;color:#1172ab;text-transform:uppercase;font-weight:700;margin-bottom:12px;display:block}}
h1{{font-family:Fraunces,Georgia,serif;font-weight:800;color:#0F3F68;font-size:40px;line-height:1.1;margin:0 0 16px 0}}
h1 em{{font-style:normal;color:#1F7E8C}}
.lead{{font-size:18px;color:#374151;max-width:680px;margin:0 auto}}
.section{{max-width:1080px;margin:0 auto;padding:32px 24px}}
.section h2{{font-family:Fraunces,Georgia,serif;font-weight:700;color:#0F3F68;font-size:26px;margin:32px 0 18px 0}}
.section .sub{{color:#5e7183;margin:0 0 22px 0}}
.c-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:14px}}
.c-card{{background:#fff;border:1px solid #e5e7eb;border-radius:16px;padding:20px;text-decoration:none;color:inherit;display:block;transition:.18s;position:relative}}
.c-card:hover{{border-color:#4FBECE;transform:translateY(-3px);box-shadow:0 12px 24px -10px rgba(15,63,104,.18)}}
.c-card h3{{font-family:Fraunces,Georgia,serif;color:#0F3F68;font-size:22px;margin:8px 0 4px 0;font-weight:700}}
.c-badge{{font-size:11px;letter-spacing:.6px;color:#1172ab;background:#e6f0fa;padding:4px 9px;border-radius:30px;font-weight:600;display:inline-block}}
.c-dist{{font-size:14px;color:#0F3F68;margin:6px 0;font-weight:600}}
.c-ruta{{font-size:13px;color:#5e7183;margin:4px 0 12px 0}}
.c-arrow{{font-size:13px;color:#1F7E8C;font-weight:600}}
.cta-band{{background:linear-gradient(135deg,#0F3F68,#1172ab);color:#fff;padding:48px 24px;text-align:center;border-radius:18px;margin:40px 0}}
.cta-band h2{{color:#fff;margin:0 0 14px 0;font-size:26px}}
.cta-band p{{color:#cfe5f4;margin:0 0 24px 0}}
.cta-band a{{display:inline-block;background:#25D366;color:#fff;padding:14px 28px;border-radius:30px;font-weight:700;text-decoration:none;font-size:15px}}
.faq details{{background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:16px 20px;margin:10px 0;cursor:pointer}}
.faq summary{{font-weight:600;color:#0F3F68;font-size:15px}}
.faq .ans{{margin-top:12px;color:#374151;font-size:14px;line-height:1.65}}
footer{{text-align:center;padding:24px;font-size:13px;color:#5e7183;border-top:1px solid #e5e7eb;margin-top:40px;background:#fff}}
@media (max-width:600px){{h1{{font-size:30px}}.hero{{padding:40px 20px 32px}}}}
</style>
</head>
<body>
<header><a href="/">← Volver al inicio</a></header>
<section class="hero">
  <span class="eyebrow">Servicios médicos por comuna</span>
  <h1>Atendemos toda la <em>Provincia de Arauco</em></h1>
  <p class="lead">23 profesionales y 22 especialidades médicas y dentales para pacientes de Carampangue, Arauco, Curanilahue, Lebu, Cañete, Los Álamos, Tirúa, Contulmo y comunas cercanas. Bono Fonasa MLE en sucursal con huella biométrica.</p>
</section>
<section class="section">
  <h2>Sede principal</h2>
  <p class="sub">El Centro Médico Carampangue está físicamente en República 102, Carampangue (comuna de Arauco), Región del Biobío.</p>
  <div class="c-grid">
      {sede_html}
  </div>
  <h2>Localidades cercanas</h2>
  <p class="sub">A menos de 10 km del centro — viaje de ida y vuelta cómodo en una mañana.</p>
  <div class="c-grid">
      {cercanas_html}
  </div>
  <h2>Provincia de Arauco</h2>
  <p class="sub">El resto de la provincia: información de rutas, distancias y especialidades disponibles.</p>
  <div class="c-grid">
      {arauco_html}
  </div>

  <div class="cta-band">
    <h2>Agenda desde tu comuna en 30 segundos</h2>
    <p>El asistente automático revisa disponibilidad real al instante, 24/7.</p>
    <a href="https://wa.me/56966610737?text=Hola%2C%20quiero%20agendar%20una%20hora&utm_source=comuna_index&utm_medium=organic&utm_campaign=cta_general" target="_blank" rel="noopener">Agendar por WhatsApp</a>
  </div>

  <h2>Preguntas frecuentes</h2>
  <div class="faq">
    <details><summary>¿A qué comunas atiende el Centro Médico Carampangue?</summary><p class="ans">Atendemos pacientes de toda la Provincia de Arauco: Carampangue (sede), Laraquete, Ramadilla, Arauco, Curanilahue, Los Álamos, Lebu, Cañete, Contulmo y Tirúa. Recibimos también pacientes de Concepción, Coronel y comunas vecinas que prefieren evitar esperas del sistema público.</p></details>
    <details><summary>¿Necesito ser de la zona para agendar?</summary><p class="ans">No. Cualquier persona puede agendar una hora con cualquiera de nuestros 23 profesionales. La cercanía geográfica solo influye en el tiempo de viaje, no en la disponibilidad de hora.</p></details>
    <details><summary>¿Cómo llego desde mi comuna?</summary><p class="ans">Selecciona tu comuna en las cards de arriba para ver rutas exactas, kilómetros, tiempo de viaje y opciones de transporte público desde tu localidad hasta Carampangue.</p></details>
    <details><summary>¿Aceptan Bono Fonasa para pacientes de otras comunas?</summary><p class="ans">Sí. El Bono Fonasa MLE se emite en la sucursal con huella biométrica para Medicina General, Medicina Familiar, Kinesiología, Nutrición y Psicología (adulto e infantil), independiente de la comuna del paciente. Matrona tiene tarifa preferencial Fonasa ($16.000).</p></details>
  </div>
</section>
<footer>Centro Médico Carampangue · República 102, Carampangue · Provincia de Arauco · (44) 296 5226 · WhatsApp +56 9 6661 0737</footer>
</body>
</html>"""


@app.get("/blog/{slug}", response_class=HTMLResponse)
async def blog_post(slug: str):
    """Blogs por especialidad. Si el slug termina con sufijo de comuna
    de Arauco (ej. 'medicina-general-arauco'), genera versión localizada SEO."""
    import re as _re
    if not _re.fullmatch(r"[a-z0-9-]{1,80}", slug):
        return HTMLResponse("<h1>404</h1>", status_code=404)

    # Detectar localización por sufijo conocido
    for comuna_slug in COMUNAS_ARAUCO:
        suffix = "-" + comuna_slug
        if slug.endswith(suffix):
            base_slug = slug[:-len(suffix)]
            base_path = _BLOG_DIR / f"{base_slug}.html"
            if base_path.exists():
                html = base_path.read_text(encoding="utf-8")
                return _localize_blog(html, base_slug, comuna_slug)

    # Sin localización: blog base — inyectar bloque de enlaces a variantes por comuna
    blog_path = _BLOG_DIR / f"{slug}.html"
    if not blog_path.exists():
        return HTMLResponse("<h1>404 — Artículo no encontrado</h1>", status_code=404)
    html = blog_path.read_text(encoding="utf-8")
    html = _inject_comunas_block_in_base(html, slug)
    return html


# ============================================================
# COMUNAS DE LA PROVINCIA DE ARAUCO (SEO local)
# ============================================================
COMUNAS_ARAUCO = {
    # Localidades fuertes (mayor concentración de pacientes)
    "carampangue": {"nombre": "Carampangue", "km": 0,   "min": 0,   "ruta": "—",              "tipo": "local"},
    "laraquete":   {"nombre": "Laraquete",   "km": 8,   "min": 10,  "ruta": "Ruta 160 norte", "tipo": "cercana"},
    "ramadilla":   {"nombre": "Ramadilla",   "km": 6,   "min": 10,  "ruta": "ruta rural",     "tipo": "cercana"},
    # Comunas Provincia de Arauco
    "arauco":      {"nombre": "Arauco",      "km": 15,  "min": 20,  "ruta": "Ruta P-22"},
    "lebu":        {"nombre": "Lebu",        "km": 50,  "min": 60,  "ruta": "Ruta P-40"},
    "canete":      {"nombre": "Cañete",      "km": 70,  "min": 80,  "ruta": "Ruta P-72"},
    "tirua":       {"nombre": "Tirúa",       "km": 110, "min": 120, "ruta": "Ruta P-72 sur"},
    "curanilahue": {"nombre": "Curanilahue", "km": 25,  "min": 30,  "ruta": "Ruta 160"},
    "los-alamos":  {"nombre": "Los Álamos",  "km": 35,  "min": 40,  "ruta": "Ruta 160"},
    "contulmo":    {"nombre": "Contulmo",    "km": 90,  "min": 100, "ruta": "Ruta P-72 + P-60"},
}

# Contenido único por comuna — se inyecta en cada blog comuna-especialidad
# para que Google los trate como páginas distintas (no template duplicado).
# Datos verificables (rutas, hospitales públicos, distancias) — sin inventar números.
COMUNA_LOCAL_DATA = {
    "carampangue": {
        "locomocion": "El centro está en República 102, en pleno casco urbano de Carampangue, a 5 minutos a pie desde la plaza y frente a Banco Estado. Hay estacionamiento libre en la calle. Si vienes en colectivo o taxi, cualquier conductor de la zona conoce la ubicación.",
        "contexto": "Carampangue es la localidad más poblada de la comuna de Arauco fuera del centro urbano de Arauco mismo. Cuenta con CESFAM Carampangue para atención primaria, escuelas y comercio, pero las especialidades médicas y dentales requieren derivación al hospital de Arauco o Concepción.",
        "razon": "El CMC nació en Carampangue justamente para acercar {esp} sin que tengas que viajar a Arauco o Concepción. Tu hora puede ser el mismo día o al día siguiente, sin las listas de espera del sistema público.",
    },
    "laraquete": {
        "locomocion": "Desde Laraquete son 8 km por la Ruta 160 hacia el sur, 10 minutos en auto. Los buses interurbanos del tramo Concepción–Arauco–Lebu paran en Carampangue durante todo el día. También hay taxis colectivos locales que cubren Laraquete–Carampangue.",
        "contexto": "Laraquete es la entrada norte a la Provincia de Arauco, balneario y comunidad pesquera. Cuenta con CESFAM y posta rural, pero las especialidades se derivan a Carampangue, Arauco o Concepción.",
        "razon": "Para vecinos de Laraquete, el CMC es la opción más cercana de {esp} sin tener que cruzar el puente del Bío Bío hasta Concepción (1 hora). Vuelta el mismo día, sin perder la jornada completa.",
    },
    "ramadilla": {
        "locomocion": "Ramadilla está a 6 km de Carampangue por camino rural, 10 minutos en auto. La ruta es directa; sin auto, lo más práctico es taxi compartido o coordinar con un familiar. La señal de celular y datos cubre bien el tramo.",
        "contexto": "Ramadilla es localidad rural de la comuna de Arauco con población dispersa. La posta rural cubre atención básica pero todo lo especializado se deriva.",
        "razon": "Por cercanía geográfica, los vecinos de Ramadilla suelen preferir el CMC para {esp} antes que viajar al hospital de Arauco — menos tiempo de viaje y horario más amplio (hasta las 21:00 entre semana).",
    },
    "arauco": {
        "locomocion": "Desde Arauco son 15 km por la Ruta P-22, 20 minutos en auto. Hay buses Estuario Reloncaví y otras líneas que cubren Arauco–Carampangue durante el día. También sirve cualquier bus interurbano que vaya hacia Concepción y para en Carampangue.",
        "contexto": "Arauco es la cabecera comunal y centro político-administrativo. Cuenta con Hospital Dr. Rafael Avaria, CESFAM Arauco y CECOSF. El CMC es la principal alternativa privada de la comuna para especialidades médicas y dentales.",
        "razon": "Pacientes de Arauco acuden al CMC para {esp} cuando necesitan acortar listas de espera del sistema público o cuando buscan tratamientos dentales (ortodoncia, implantología, estética) que la red pública no cubre con prontitud.",
    },
    "curanilahue": {
        "locomocion": "Desde Curanilahue son 25 km por la Ruta 160, 30 minutos en auto. Buses Lit Sur, Estuario Reloncaví y otras líneas cubren el tramo Curanilahue–Carampangue varias veces al día. Es viaje de ida y vuelta cómodo en una mañana.",
        "contexto": "Curanilahue tiene Hospital Comunitario Dr. Rafael Avaria y CESFAM. Históricamente comuna minera del carbón, hoy en transición. Los pacientes locales conocen bien la red Carampangue–Arauco.",
        "razon": "Curanilahue es, después de Arauco, la comuna fuera de la sede del CMC con más pacientes recurrentes — particularmente en odontología, ortodoncia y kinesiología. Muchas familias vienen mensualmente para controles ortodónticos y aprovechan el viaje para otras especialidades como {esp}.",
    },
    "los-alamos": {
        "locomocion": "Desde Los Álamos son 35 km por la Ruta 160, 40 minutos en auto. Los buses interurbanos que conectan Concepción con Lebu pasan por Los Álamos y Carampangue, lo que facilita la conexión sin transbordos.",
        "contexto": "Los Álamos cuenta con Hospital Comunitario Los Álamos y CESFAM. Es comuna intermedia entre Curanilahue y Lebu, con economía diversificada (forestal, comercio, servicios).",
        "razon": "Para pacientes de Los Álamos, el CMC es opción cuando necesitan {esp} y prefieren no viajar a Concepción (1h 30 min) ni esperar la red pública. La distancia hace que el viaje sea razonable de ida y vuelta el mismo día.",
    },
    "lebu": {
        "locomocion": "Desde Lebu son 50 km combinando Ruta P-40 y Ruta 160, alrededor de 1 hora en auto. Hay servicios diarios de buses Lebu–Concepción que paran en Carampangue, lo que evita transbordos. El viaje completo (ida + atención + vuelta) cabe holgadamente en una jornada.",
        "contexto": "Lebu es la capital de la Provincia de Arauco, con Hospital Provincial Santa Isabel de Lebu. Pero la red pública trabaja con tiempos de espera largos para varias especialidades, especialmente las dentales.",
        "razon": "Pacientes de Lebu vienen al CMC para {esp} principalmente para acortar tiempos de espera o para tratamientos dentales privados (ortodoncia, implantología). Es habitual programar la cita temprano para volver en la tarde.",
    },
    "canete": {
        "locomocion": "Desde Cañete son 70 km vía Ruta P-72 y luego Ruta 160 hacia el norte, alrededor de 1h 20 min en auto. Hay servicios de buses Cañete–Concepción que paran en Carampangue, sin necesidad de transbordo en Arauco.",
        "contexto": "Cañete es la comuna mapuche-lafquenche más grande de la Provincia de Arauco. Cuenta con Hospital Intercultural Kallvu Llanka, que combina medicina occidental con medicina mapuche.",
        "razon": "Pacientes de Cañete vienen al CMC para {esp} cuando necesitan especialidades privadas o quieren agilizar tiempos. Muchos combinan el viaje con compras o trámites en Carampangue/Arauco para optimizar el día.",
    },
    "contulmo": {
        "locomocion": "Desde Contulmo son 90 km vía Ruta P-72 y P-60, alrededor de 1h 40 min en auto. Es trayecto largo pero la única alternativa razonable sin pasar por Concepción (que sería un rodeo de más de 3 horas).",
        "contexto": "Contulmo es comuna cordillerana pequeña, junto al Lago Lanalhue, con paisajes selváticos y patrimonio arquitectónico colono. Cuenta con CESFAM Contulmo y postas rurales, pero las especialidades se derivan fuera de la comuna.",
        "razon": "Pacientes de Contulmo eligen el CMC para {esp} cuando necesitan especialidades que en su comuna no existen y prefieren no hacer el viaje a Concepción. Recomendamos agendar varias atenciones en un mismo día para optimizar el viaje.",
    },
    "tirua": {
        "locomocion": "Desde Tirúa son 110 km vía Ruta P-72 sur, alrededor de 2 horas en auto. Es la comuna más distante de la Provincia de Arauco, por lo que recomendamos agendar el viaje con tiempo y combinar varias atenciones en una sola venida.",
        "contexto": "Tirúa es comuna mapuche-lafquenche del extremo sur de la Provincia, costera. Cuenta con CESFAM Tirúa y postas rurales. La conectividad con la red pública especializada implica viajar a Cañete o a Concepción.",
        "razon": "Pacientes de Tirúa que viajan al CMC suelen agendar varias atenciones en un mismo día (médica + dental + ecografía cuando aplica). Para {esp} en particular, conviene coordinar con anticipación por WhatsApp para que la cita calce con disponibilidad.",
    },
}


def _specialty_label(base_slug: str) -> str:
    """Mapea slug base a label legible para usar en contenido localizado."""
    return {
        "cardiologia": "cardiología",
        "medicina-general": "medicina general",
        "ortodoncia": "ortodoncia",
        "ecografia": "ecografía",
        "estetica-facial": "estética facial",
        "kinesiologia": "kinesiología",
        "odontologia-general": "odontología general",
        "otorrinolaringologia": "otorrinolaringología",
        "ginecologia": "ginecología",
        "gastroenterologia": "gastroenterología",
        "endodoncia": "endodoncia",
        "implantologia": "implantología",
        "masoterapia": "masoterapia",
        "nutricion": "nutrición",
        "psicologia-adulto": "psicología adulto",
        "psicologia-infantil": "psicología infantil",
        "fonoaudiologia": "fonoaudiología",
        "matrona": "matrona",
        "podologia": "podología",
        "neurologia": "neurología",
        "psiquiatria": "psiquiatría",
        "oftalmologia": "oftalmología",
    }.get(base_slug, base_slug.replace("-", " "))


def _build_local_info_section(base_slug: str, comuna_slug: str) -> str:
    """HTML de la sección 'Atención desde {comuna}' con 3 párrafos únicos."""
    if comuna_slug not in COMUNA_LOCAL_DATA:
        return ""
    data = COMUNA_LOCAL_DATA[comuna_slug]
    nombre = COMUNAS_ARAUCO[comuna_slug]["nombre"]
    esp = _specialty_label(base_slug)
    locomocion = data["locomocion"]
    contexto = data["contexto"]
    razon = data["razon"].replace("{esp}", esp)
    if comuna_slug == "carampangue":
        h2 = f"Atención médica y dental en Carampangue"
    else:
        h2 = f"Atención de {esp} desde {nombre}"
    return f"""
<section class="local-info" style="padding:56px 0;background:#f9fafb;border-top:1px solid #e5e7eb;border-bottom:1px solid #e5e7eb;">
  <div class="container">
    <h2 style="font-size:28px;line-height:1.2;margin:0 0 28px 0;color:#0f3f68;">{h2}</h2>
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:28px;">
      <article>
        <h3 style="font-size:17px;margin:0 0 8px 0;color:#1172ab;">Cómo llegar al CMC</h3>
        <p style="line-height:1.65;color:#374151;margin:0;">{locomocion}</p>
      </article>
      <article>
        <h3 style="font-size:17px;margin:0 0 8px 0;color:#1172ab;">{nombre} en la Provincia de Arauco</h3>
        <p style="line-height:1.65;color:#374151;margin:0;">{contexto}</p>
      </article>
      <article>
        <h3 style="font-size:17px;margin:0 0 8px 0;color:#1172ab;">¿Por qué venir desde {nombre}?</h3>
        <p style="line-height:1.65;color:#374151;margin:0;">{razon}</p>
      </article>
    </div>
  </div>
</section>
"""


def _build_comunas_footer_block(base_slug: str) -> str:
    """Bloque 'Disponible también en:' que se inyecta al pie de cada blog base.
    190 enlaces internos nuevos que conectan blogs base ↔ variantes por comuna.
    Esto saca a las 190 páginas localizadas de su huerfanidad de PageRank."""
    esp = _specialty_label(base_slug)
    esp_cap = esp.capitalize()
    links_html = "\n".join(
        f'      <li><a href="https://centromedicocarampangue.cl/blog/{base_slug}-{slug}">'
        f'{esp_cap} en {c["nombre"]}</a></li>'
        for slug, c in COMUNAS_ARAUCO.items()
    )
    return f"""
<section class="comunas-disponibles" style="padding:48px 0 40px;background:#f0f4f8;border-top:2px solid #e2e8f0;">
  <div class="container">
    <h2 style="font-size:20px;font-weight:700;color:#0f3f68;margin:0 0 16px 0;">
      {esp_cap} disponible tambi\u00e9n en estas comunas
    </h2>
    <p style="font-size:14px;color:#4b5563;margin:0 0 20px 0;">
      Atendemos pacientes de toda la Provincia de Arauco. Selecciona tu comuna para ver
      informaci\u00f3n espec\u00edfica de cómo llegar y tiempos de viaje al Centro M\u00e9dico Carampangue.
    </p>
    <ul style="list-style:none;padding:0;margin:0;display:flex;flex-wrap:wrap;gap:10px;">
{links_html}
    </ul>
  </div>
</section>
"""


def _inject_comunas_block_in_base(html: str, base_slug: str) -> str:
    """Inyecta el bloque de comunas antes de </body> en un blog base."""
    block = _build_comunas_footer_block(base_slug)
    if "</body>" in html:
        return html.replace("</body>", block + "\n</body>", 1)
    return html + block


def _localize_blog(html: str, base_slug: str, comuna_slug: str) -> str:
    """Genera versión localizada del blog para una comuna de Arauco.
    Cambia title/meta/h1/lead/canonical y agrega referencia de la comuna."""
    import re as _re
    c = COMUNAS_ARAUCO[comuna_slug]
    nombre = c["nombre"]
    km = c["km"]
    minutos = c["min"]

    # Title con sufijo de comuna
    html = _re.sub(
        r'(<title>[^<]*?)(\s*\|\s*CMC</title>)',
        rf'\1 desde {nombre}\2', html, count=1
    )

    # Meta description con localidad (Carampangue es la sede, no decir "desde")
    if comuna_slug == "carampangue":
        meta_extra = f' Atención en Carampangue, Provincia de Arauco.'
    else:
        meta_extra = f' Atendemos pacientes desde {nombre} ({km} km · {minutos} min). Provincia de Arauco.'
    html = _re.sub(
        r'(<meta name="description" content="[^"]*?)(\s*"\s*/>)',
        rf'\1{meta_extra}"\2',
        html, count=1
    )

    # og:title
    html = _re.sub(
        r'(<meta property="og:title" content="[^"]*?)(\s*"\s*/>)',
        rf'\1 desde {nombre}\2', html, count=1
    )

    # Canonical apunta a versión localizada en centromedicocarampangue.cl
    # BUG FIX 2026-05-18: el template tiene canonical en centromedicocarampangue.cl
    # (no agentecmc.cl), entonces la regex vieja nunca matcheaba y las 190 páginas
    # comuna-especialidad quedaban con canonical → blog base, indexación 0.
    local_url = f"https://centromedicocarampangue.cl/blog/{base_slug}-{comuna_slug}"
    base_url_cmc = f"https://centromedicocarampangue.cl/blog/{base_slug}"
    base_url_ag = f"https://agentecmc.cl/blog/{base_slug}"

    # Reescribe canonical (tolera ambos hosts y con/sin espacio antes de />)
    html = _re.sub(
        rf'<link\s+rel="canonical"\s+href="https://(?:centromedicocarampangue\.cl|agentecmc\.cl)/blog/{base_slug}"\s*/?>',
        f'<link rel="canonical" href="{local_url}" />',
        html
    )

    # Schema URLs y og:url: apuntan a versión localizada
    html = html.replace(f'"{base_url_cmc}"', f'"{local_url}"')
    html = html.replace(f'"{base_url_ag}"', f'"{local_url}"')
    html = html.replace(
        f'content="{base_url_cmc}"', f'content="{local_url}"'
    )
    html = html.replace(
        f'content="{base_url_ag}"', f'content="{local_url}"'
    )

    # H1: agregar " · {nombre}" al final
    html = _re.sub(
        r'(<h1 class="blog-h1">[^<]*?)(</h1>)',
        rf'\1 · {nombre}\2', html, count=1
    )

    # Lead: prefix con localidad. Carampangue tiene caso especial (es la sede)
    if comuna_slug == "carampangue":
        lead_prefix = f'<strong>Atendemos a la comunidad de Carampangue.</strong> '
    else:
        lead_prefix = f'<strong>Pacientes desde {nombre}</strong> ({km} km · {minutos} min en auto). '
    html = _re.sub(
        r'(<p class="blog-lead">\s*)',
        rf'\1{lead_prefix}',
        html, count=1
    )

    # Breadcrumb visible
    html = _re.sub(
        r'(<span class="current">)([^<]+)(</span>)',
        rf'\1\2 · {nombre}\3', html, count=1
    )

    # Inyectar sección de contenido local único (locomoción + contexto + razón)
    # antes del body del blog. Esto diferencia cada página comuna-especialidad
    # para que Google no la trate como contenido duplicado.
    local_section = _build_local_info_section(base_slug, comuna_slug)
    if local_section:
        html = html.replace(
            '<section class="blog-body">',
            local_section + '\n<section class="blog-body">',
            1,
        )

    return html


@app.get("/sitemap.xml")
async def sitemap_xml():
    """Sitemap dinámico con todas las URLs (home + 22 especialidades + localidades + topic blogs + /blog index)."""
    from fastapi.responses import Response
    from datetime import datetime
    BLOGS_BASE = ["cardiologia", "medicina-general", "ortodoncia", "ecografia",
                  "estetica-facial", "kinesiologia", "odontologia-general",
                  "otorrinolaringologia", "ginecologia",
                  "gastroenterologia", "endodoncia", "implantologia",
                  "masoterapia", "nutricion", "psicologia-adulto",
                  "psicologia-infantil", "fonoaudiologia", "matrona", "podologia",
                  "neurologia", "psiquiatria", "oftalmologia"]
    BLOGS_TOPICS = [
        "cefalea-tipos-tratamiento", "diabetes-tipo-2-control",
        "dolor-lumbar-cuando-consultar", "embarazo-controles-mensuales",
        "hipertension-arterial-control", "nutricion-baja-peso-saludable",
        "precio-implante-dental-arauco", "precio-ortodoncia-arauco",
        "psicologia-infantil-cuando-consultar", "rinoplastia-funcional-tabique",
        "vacunas-pni-calendario-2026", "bono-fonasa-mle-arauco",
        "limpieza-dental-precio-arauco", "ecografia-precio-arauco",
    ]
    base_url = "https://centromedicocarampangue.cl"
    today = datetime.now().strftime("%Y-%m-%d")
    urls = [
        (f"{base_url}/", "1.0", "weekly"),
        (f"{base_url}/blog", "0.95", "weekly"),
        (f"{base_url}/comuna/", "0.85", "monthly"),
        (f"{base_url}/privacidad", "0.3", "yearly"),
    ]
    # Landings directas (servidas vía bridge WP Snippet 8 bajo el dominio canónico)
    for direct_slug in ("lebu", "empresas", "los-alamos", "canete", "chequeos", "curanilahue", "ortodoncia"):
        urls.append((f"{base_url}/{direct_slug}", "0.85", "monthly"))
    # Comuna hubs
    for comuna_slug in COMUNAS_ARAUCO:
        urls.append((f"{base_url}/comuna/{comuna_slug}", "0.85", "monthly"))
    for slug in BLOGS_BASE:
        urls.append((f"{base_url}/blog/{slug}", "0.9", "monthly"))
        for comuna_slug in COMUNAS_ARAUCO:
            urls.append((f"{base_url}/blog/{slug}-{comuna_slug}", "0.7", "monthly"))
    for slug in BLOGS_TOPICS:
        urls.append((f"{base_url}/blog/{slug}", "0.85", "monthly"))
    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for url, priority, freq in urls:
        parts.append(
            f'  <url><loc>{url}</loc><lastmod>{today}</lastmod>'
            f'<changefreq>{freq}</changefreq><priority>{priority}</priority></url>'
        )
    parts.append('</urlset>')
    return Response(content="\n".join(parts), media_type="application/xml")


@app.get("/sitemap_blogs.xml")
async def sitemap_blogs_xml():
    """Sitemap de blogs — sirve el archivo estático si existe; fallback dinámico leyendo disco."""
    from fastapi.responses import Response
    from pathlib import Path as _P
    from datetime import datetime as _dt
    _f = _P(__file__).parent.parent / "static" / "sitemap_blogs.xml"
    if _f.exists():
        return Response(content=_f.read_text(encoding="utf-8"), media_type="application/xml")
    # fallback dinámico: enumera todos los archivos reales en templates/blog/
    _blog_dir = _P(__file__).parent.parent / "templates" / "blog"
    slugs = sorted(p.stem for p in _blog_dir.glob("*.html")) if _blog_dir.exists() else []
    base_url = "https://centromedicocarampangue.cl"
    today = _dt.now().strftime("%Y-%m-%d")
    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for slug in slugs:
        parts.append(
            f'  <url><loc>{base_url}/blog/{slug}</loc>'
            f'<lastmod>{today}</lastmod>'
            f'<changefreq>monthly</changefreq><priority>0.8</priority></url>'
        )
    parts.append('</urlset>')
    return Response(content="\n".join(parts), media_type="application/xml")


@app.get("/sitemap_index.xml")
async def sitemap_index_xml():
    """Sitemap index que referencia el sitemap principal + el de blogs + imágenes."""
    from fastapi.responses import Response
    from datetime import datetime as _dt
    today = _dt.now().strftime("%Y-%m-%d")
    content = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f'  <sitemap><loc>https://centromedicocarampangue.cl/sitemap.xml</loc>'
        f'<lastmod>{today}</lastmod></sitemap>\n'
        f'  <sitemap><loc>https://centromedicocarampangue.cl/sitemap_blogs.xml</loc>'
        f'<lastmod>{today}</lastmod></sitemap>\n'
        f'  <sitemap><loc>https://centromedicocarampangue.cl/sitemap_images.xml</loc>'
        f'<lastmod>{today}</lastmod></sitemap>\n'
        '</sitemapindex>\n'
    )
    return Response(content=content, media_type="application/xml")


@app.get("/feed.xml")
@app.get("/rss")
@app.get("/blog/feed")
async def blog_rss_feed():
    """RSS 2.0 feed con los 30 artículos del blog. Habilita lectores RSS y signal SEO."""
    from fastapi.responses import Response
    from datetime import datetime
    base = "https://centromedicocarampangue.cl"
    now = datetime.now().strftime("%a, %d %b %Y %H:%M:%S %z") or datetime.now().strftime("%a, %d %b %Y %H:%M:%S +0000")
    items = [
        ("medicina-general", "Medicina General · Tu médico de cabecera cerca de casa", "Dr. Andrés Abarca y Dr. Rodrigo Olavarría · Bono Fonasa $7.880 · Particular $25.000"),
        ("cardiologia", "Cardiología · Cuándo consultar y exámenes preventivos", "Dr. Miguel Millán · Evaluación cardiovascular, hipertensión, ECG"),
        ("ortodoncia", "Ortodoncia · Brackets para niños y adultos", "Dra. Daniela Castillo · Brackets metálicos y estéticos, controles cada 3-4 semanas"),
        ("kinesiologia", "Kinesiología · Lumbago, contracturas y rehabilitación", "Luis Armijo y Leonardo Etcheverry · Bono Fonasa MLE disponible"),
        ("odontologia-general", "Odontología General · Limpiezas, restauraciones y urgencia", "Dra. Javiera Burgos y Dr. Carlos Jiménez · Adulto y pediátrico"),
        ("ecografia", "Ecografía · Abdominal, renal, partes blandas, mamaria", "Dr. David Pardo · Diagnóstico por imagen no invasivo"),
        ("estetica-facial", "Estética Facial · Botox, hialurónico, hilos, peelings", "Dra. Valentina Fuentealba · Procedimientos no quirúrgicos"),
        ("ginecologia", "Ginecología · Controles, ecografía y obstetricia", "Dr. Tirso Rejón · PAP, anticoncepción, climaterio"),
        ("otorrinolaringologia", "Otorrinolaringología · Patología ORL adulta y pediátrica", "Dr. Manuel Borrego · Otitis, sinusitis, vértigo, lavado de oídos"),
        ("gastroenterologia", "Gastroenterología · Reflujo, colon irritable, dolor abdominal", "Dr. Nicolás Quijano · Helicobacter pylori, hígado graso"),
        ("nutricion", "Nutrición · Plan personalizado para baja de peso", "Gisela Pinto · Bono Fonasa MLE disponible"),
        ("psicologia-adulto", "Psicología Adulto · Ansiedad, depresión, duelo", "Jorge Montalba y Juan Pablo Rodríguez · Bono Fonasa $14.420"),
        ("psicologia-infantil", "Psicología Infantil · Trastornos conductuales y aprendizaje", "Jorge Montalba · Atención a niños y adolescentes"),
        ("fonoaudiologia", "Fonoaudiología · Lenguaje, voz, audiometría", "Juana Arratia · Pediátrica y adulta"),
        ("matrona", "Matrona · Control prenatal, PAP, anticoncepción", "Sarai Gómez · Tarifa preferencial Fonasa $16.000"),
        ("podologia", "Podología · Uña encarnada, callos, podología diabética", "Andrea Guevara"),
        ("masoterapia", "Masoterapia · Masaje descontracturante 20 o 40 min", "Paola Acosta · Espalda, cuello, lumbar"),
        ("endodoncia", "Endodoncia · Tratamiento de conducto", "Dr. Fernando Fredes · Rescate de dientes con dolor"),
        ("implantologia", "Implantología · Implantes dentales y rehabilitación oral", "Dra. Aurora Valdés · Implante + corona desde $650.000"),
        ("diabetes-tipo-2-control", "Diabetes Tipo 2 · Control y prevención de complicaciones", "Guía completa para diabéticos en Arauco"),
        ("hipertension-arterial-control", "Hipertensión Arterial · Cómo controlarla", "Dr. Miguel Millán · Antihipertensivos, dieta, controles"),
        ("cefalea-tipos-tratamiento", "Cefalea: migraña, tensional y cluster", "Tipos, síntomas y cuándo consultar"),
        ("dolor-lumbar-cuando-consultar", "Dolor lumbar: cuándo consultar al kine", "Lumbago crónico, ciática, hernia discal"),
        ("embarazo-controles-mensuales", "Embarazo: controles mensuales y ecografías", "Sarai Gómez · Control prenatal completo"),
        ("nutricion-baja-peso-saludable", "Bajar de peso de forma saludable", "Gisela Pinto · Plan nutricional personalizado"),
        ("precio-implante-dental-arauco", "Precio implante dental Arauco 2026", "Costo implante + corona, alternativas, financiamiento"),
        ("precio-ortodoncia-arauco", "Precio ortodoncia Arauco 2026", "Brackets metálicos vs estéticos, controles, duración"),
        ("psicologia-infantil-cuando-consultar", "Psicología infantil: señales de alerta", "Cuándo necesita un niño apoyo psicológico"),
        ("rinoplastia-funcional-tabique", "Rinoplastia funcional vs tabique desviado", "Dr. Manuel Borrego · Cuándo se opera"),
        ("vacunas-pni-calendario-2026", "Calendario PNI 2026 — vacunas pediátricas en Chile", "Programa Nacional de Inmunización completo"),
        ("bono-fonasa-mle-arauco", "Bono Fonasa MLE en Arauco · Cómo usarlo en el CMC", "Modalidad Libre Elección con huella biométrica · Arauco, Curanilahue, Lebu y alrededores"),
        ("ecografia-precio-arauco", "Precio ecografía en Arauco 2026 · CMC Carampangue", "Ecografía abdominal, renal, partes blandas y mamaria · Dr. David Pardo"),
        ("limpieza-dental-precio-arauco", "Precio limpieza dental en Arauco 2026 · CMC Carampangue", "Profilaxis, detartrado y pulido · Dra. Javiera Burgos · Dr. Carlos Jiménez"),
    ]

    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom" xmlns:content="http://purl.org/rss/1.0/modules/content/">',
             '<channel>',
             '<title>Blog Centro Médico Carampangue</title>',
             f'<link>{base}/blog</link>',
             f'<atom:link href="{base}/feed.xml" rel="self" type="application/rss+xml" />',
             '<description>Artículos médicos y dentales del Centro Médico Carampangue. 22 especialidades en la Provincia de Arauco.</description>',
             '<language>es-CL</language>',
             f'<lastBuildDate>{now}</lastBuildDate>',
             '<copyright>Centro Médico Carampangue</copyright>',
             f'<image><url>https://agentecmc.cl/static/og-image.png</url><title>Centro Médico Carampangue</title><link>{base}/blog</link></image>']
    for slug, title, desc in items:
        parts.append(f'<item>'
                     f'<title>{title}</title>'
                     f'<link>{base}/blog/{slug}</link>'
                     f'<guid>{base}/blog/{slug}</guid>'
                     f'<description><![CDATA[{desc}]]></description>'
                     f'<pubDate>{now}</pubDate>'
                     f'</item>')
    parts.append('</channel></rss>')
    return Response(content="\n".join(parts), media_type="application/rss+xml")


@app.get("/sitemap_images.xml")
async def sitemap_images_xml():
    """Image sitemap — declara imágenes del centro para Google Images."""
    from fastapi.responses import Response
    base = "https://centromedicocarampangue.cl"
    img_base = "https://agentecmc.cl/static/images/centro"
    photos = [
        ("fachada-centro-medico-carampangue.jpg", "Fachada del Centro Médico y Dental Carampangue con su letrero, en República 102 esquina Monsalve, Arauco"),
        ("recepcion.jpg", "Recepción del Centro Médico Carampangue con mostrador de madera y zona de espera"),
        ("sala-espera.jpg", "Sala de espera con sillones y vista a la calle desde ventanal grande"),
        ("box-medico.jpg", "Box de atención médica con camilla, escritorio y lavamanos"),
        ("box-luz-natural.jpg", "Box médico con camilla, escritorio y ventanal con luz natural"),
    ]
    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"',
             '        xmlns:image="http://www.google.com/schemas/sitemap-image/1.1">',
             f'  <url><loc>{base}/</loc>']
    for fname, alt in photos:
        parts.append(f'    <image:image><image:loc>{img_base}/{fname}</image:loc>'
                     f'<image:caption>{alt}</image:caption>'
                     f'<image:title>{alt[:80]}</image:title></image:image>')
    parts.append('  </url>')
    # Logo + og-image on home
    parts.append(f'  <url><loc>{base}/</loc>')
    parts.append(f'    <image:image><image:loc>https://agentecmc.cl/static/og-image.png</image:loc>'
                 f'<image:caption>Centro Médico Carampangue — Médico y Dentista en Arauco</image:caption></image:image>')
    parts.append('  </url>')
    parts.append('</urlset>')
    return Response(content="\n".join(parts), media_type="application/xml")


@app.get("/robots.txt")
async def robots_txt():
    """robots.txt apuntando al sitemap index y sitemaps dinámicos."""
    from fastapi.responses import PlainTextResponse
    body = (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /admin\n"
        "Disallow: /api/\n"
        "Disallow: /sitio\n"
        "Disallow: /sitio/\n"
        "Disallow: /portal\n"
        "Disallow: /portal/\n"
        "Disallow: /metrics\n"
        "Disallow: /mapa\n"
        "Disallow: /menu\n"
        "Disallow: /agentes\n"
        "Disallow: /dashboards\n"
        "Disallow: /bi/\n"
        "Disallow: /meulen/\n"
        "Disallow: /riomonte\n"
        "Disallow: /riomonte/\n"
        "Disallow: /arquetix-memo\n"
        "Disallow: /arquetix-pitch\n"
        "Disallow: /ecosistema\n"
        "Disallow: /suplementos\n"
        "Disallow: /farmacia\n"
        "Disallow: /farmacia/\n"
        "Disallow: /ideas\n"
        "Disallow: /ideas-revision\n"
        "\n"
        "User-agent: GPTBot\n"
        "Allow: /\n"
        "Disallow: /admin\n"
        "Disallow: /api/\n"
        "\n"
        "User-agent: ClaudeBot\n"
        "Allow: /\n"
        "Disallow: /admin\n"
        "Disallow: /api/\n"
        "\n"
        "User-agent: Google-Extended\n"
        "Allow: /\n"
        "Disallow: /admin\n"
        "Disallow: /api/\n"
        "\n"
        "Sitemap: https://centromedicocarampangue.cl/sitemap_index.xml\n"
        "Sitemap: https://centromedicocarampangue.cl/sitemap.xml\n"
        "Sitemap: https://centromedicocarampangue.cl/sitemap_blogs.xml\n"
    )
    return PlainTextResponse(body)


# NOTA: este catch-all /{key}.txt debe quedar DESPUÉS de /robots.txt.
# Si se registra antes, secuestra /robots.txt (key="robots" → 404) porque
# Starlette evalúa rutas en orden de registro, no por especificidad.
@app.get("/{key}.txt", include_in_schema=False)
async def indexnow_key_file(key: str):
    """IndexNow key verification — sirve {key}.txt desde root si existe en static/."""
    from fastapi.responses import Response, PlainTextResponse
    from pathlib import Path as _P
    if not (len(key) == 32 and all(c in "0123456789abcdef" for c in key)):
        return Response(status_code=404)
    f = _P(__file__).parent.parent / "static" / f"{key}.txt"
    if not f.exists():
        return Response(status_code=404)
    return PlainTextResponse(content=f.read_text(encoding="utf-8").strip(), media_type="text/plain")


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
            '<i class="fas fa-house-medical" style="color:var(--c-blue)"></i>'
            '<span class="rn">Centro Médico y Dental</span>'
            '<span class="rt">· Provincia de Arauco</span>'
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
        from google_rating import initials, PLACE_ID
        # Formato v4/v5: clases .testi / .testi-text / .testi-author
        cards_v45 = []
        # Formato v7: clases .test-card / .test-quote / .test-author / .verif (SVG inline, sin fontawesome)
        cards_v7 = []
        star_svg = '<svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/></svg>'
        # Filtrar reseñas con texto, ordenar por fecha de publicación DESC (más recientes primero)
        # y mostrar hasta 7 (deja espacio para los 2 CTAs en el grid → max 9 cards = 3 filas de 3)
        reviews_with_text = [r for r in reviews if (r.get("text") or "").strip()]
        reviews_with_text.sort(key=lambda r: r.get("publish_time") or "", reverse=True)
        for rv in reviews_with_text[:7]:
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
        # CTAs siempre visibles al final del grid: "Ver todas en Google" + "Dejar tu reseña"
        google_g_svg = '<svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M21.35 11.1H12v3.83h5.51c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09 0-.78-.07-1.53-.2-2.25z"/><path d="M12 22c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 19.98 7.7 22 12 22z" opacity=".75"/><path d="M5.84 13.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V6.07H2.18C1.43 7.55 1 9.22 1 11s.43 3.45 1.18 4.93l3.66-2.84z" opacity=".5"/><path d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.02 2.18 6.07l3.66 2.84c.87-2.6 3.3-3.53 6.16-3.53z" opacity=".3"/></svg>'
        rt = rating_data.get("rating") or "4.8"
        review_count = rating_data.get("review_count") or 8
        cards_v7.append(
            '<a class="test-card test-cta-card" '
            f'href="https://search.google.com/local/reviews?placeid={PLACE_ID}" '
            'target="_blank" rel="noopener" '
            'style="display: flex; flex-direction: column; background: rgba(79,190,206,0.10); border-style: dashed; border-color: var(--brand-teal); text-decoration: none;">\n'
            f'  <div style="color: var(--brand-teal); margin-bottom: 12px;">{google_g_svg}</div>\n'
            f'  <p class="test-quote" style="margin-bottom: 14px; font-size: 15px;">Mira las <strong style="color: var(--brand-teal);">{review_count} reseñas</strong> de pacientes en Google · <strong>{rt}★</strong></p>\n'
            '  <div style="display: inline-flex; align-items: center; gap: 6px; margin-top: auto; padding: 11px 16px; background: white; color: var(--brand-navy); border-radius: var(--radius-pill); font-weight: 700; font-size: 13px; font-family: var(--font-display); align-self: flex-start;">\n'
            '    Ver todas en Google →\n'
            '  </div>\n'
            '</a>'
        )
        cards_v7.append(
            '<a class="test-card test-cta-card" '
            f'href="https://search.google.com/local/writereview?placeid={PLACE_ID}" '
            'target="_blank" rel="noopener" '
            'style="display: flex; flex-direction: column; background: rgba(37,211,102,0.08); border-style: dashed; border-color: #25d366; text-decoration: none;">\n'
            '  <div style="color: #25d366; margin-bottom: 12px;">'
            '<svg width="32" height="32" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M11.4 2.6c.3-.6 1-.6 1.3 0l2.4 5 5.5.8c.7.1 1 .9.5 1.4l-4 3.9.9 5.5c.1.7-.6 1.2-1.2.9L12 17.5l-4.9 2.6c-.6.3-1.3-.2-1.2-.9l.9-5.5-4-3.9c-.5-.5-.2-1.3.5-1.4l5.5-.8 2.6-5z"/></svg>'
            '</div>\n'
            '  <p class="test-quote" style="margin-bottom: 14px;">¿Te atendiste con nosotros? <strong style="color: #25d366;">Tu opinión nos ayuda a seguir mejorando.</strong></p>\n'
            '  <div style="display: inline-flex; align-items: center; gap: 6px; margin-top: auto; padding: 11px 16px; background: white; color: var(--brand-navy); border-radius: var(--radius-pill); font-weight: 700; font-size: 13px; font-family: var(--font-display); align-self: flex-start;">\n'
            '    Dejar tu reseña →\n'
            '  </div>\n'
            '</a>'
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
    from admin_routes import _verify_cookie, _is_admin_token
    # no-store: el panel se itera seguido; sin esto el navegador (y el iframe del
    # shell Alma) sirve una versión vieja en caché y los cambios de UI no se ven.
    _NOCACHE = {"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"}
    # Pill "Pendientes" (checklist+mural) en la barra: solo dueño por ahora.
    # Inyectamos un booleano, NUNCA el token del dueño (no se filtra a recepción).
    if token and _is_admin_token(token):
        _pill = "true" if (OLACORE_TOKEN and token == OLACORE_TOKEN) else "false"
        return HTMLResponse(_ADMIN_V2_HTML.replace("__TOKEN__", token).replace("__CHECKLIST_PILL__", _pill), headers=_NOCACHE)
    if cmc_session:
        role = _verify_cookie(cmc_session)
        if role in ("admin", "ortodoncia"):
            return HTMLResponse(_ADMIN_V2_HTML.replace("__TOKEN__", "").replace("__CHECKLIST_PILL__", "false"), headers=_NOCACHE)
    return RedirectResponse(url="/admin/login", status_code=302)


@app.get("/admin/v3", response_class=HTMLResponse)
def admin_panel_v3(token: str | None = Query(None),
                   cmc_session: str | None = Cookie(None)):
    """Panel de recepción v3 (beta, rediseño premium Alma). Misma auth que /admin."""
    from admin_routes import _verify_cookie, _is_admin_token
    if not _ADMIN_V3_HTML:
        raise HTTPException(404, "Panel v3 no disponible")
    if token and _is_admin_token(token):
        return _ADMIN_V3_HTML.replace("__TOKEN__", token)
    if cmc_session:
        role = _verify_cookie(cmc_session)
        if role in ("admin", "ortodoncia"):
            return _ADMIN_V3_HTML.replace("__TOKEN__", "")
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


@app.get("/sitio-v8", response_class=HTMLResponse)
def sitio_v8_preview():
    """Preview pública del sitio V8 «Costero Editorial» (borrador, no indexar)."""
    html = (_TEMPLATE_DIR / "sitio-v8.html").read_text(encoding="utf-8")
    return HTMLResponse(html, headers={"Cache-Control": "no-store, max-age=0",
                                       "X-Robots-Tag": "noindex, nofollow"})


@app.get("/sitio-v9", response_class=HTMLResponse)
def sitio_v9_preview():
    """Preview pública del sitio V9 «MAREA» (borrador, no indexar)."""
    html = (_TEMPLATE_DIR / "sitio-v9.html").read_text(encoding="utf-8")
    return HTMLResponse(html, headers={"Cache-Control": "no-store, max-age=0",
                                       "X-Robots-Tag": "noindex, nofollow"})


@app.get("/vecino", response_class=HTMLResponse)
def vecino_meulen_page():
    """Mi Vecino Meulen — demo navegable del portal del vecino (datos ficticios).
    Mismo ADN que el portal del paciente CMC, identidad Supermercado Meulen."""
    if not _VECINO_MEULEN_HTML:
        raise HTTPException(404, "No encontrado")
    return HTMLResponse(_VECINO_MEULEN_HTML,
                        headers={"Cache-Control": "no-store, max-age=0"})


@app.get("/vecino/sw.js", include_in_schema=False)
def vecino_service_worker():
    return FileResponse(
        str(Path(__file__).parent.parent / "static" / "pwa" / "portal-sw.js"),
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/vecino"},
    )


@app.get("/favicon.ico", include_in_schema=False)
def favicon_global():
    # Antes 404 en TODO el sitio (visto en consola 2026-06-11) — el isotipo
    # oficial como favicon global; las páginas pueden sobreescribir con <link>.
    return FileResponse(
        str(Path(__file__).parent.parent / "static" / "isotipo.png"),
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/agendar/sw.js", include_in_schema=False)
def agendar_service_worker():
    return FileResponse(
        str(Path(__file__).parent.parent / "static" / "pwa" / "agendar-sw.js"),
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/agendar"},
    )


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
    # Autorizar ANTES de leer del disco: son direcciones de pacientes.
    _autorizado = bool(token and token == ADMIN_TOKEN)
    if not _autorizado and cmc_session:
        _autorizado = _verify_cookie(cmc_session) in ("admin", "ortodoncia")
    if not _autorizado:
        return RedirectResponse(url="/admin/login", status_code=302)

    _html = _leer_heatmap_direcciones()
    if not _html:
        raise HTTPException(404, "Mapa no generado aún. Ejecutar: bash scripts/heatmap_refresh.sh")
    return _html


def _serve_portal(html: str, request: Request, demo: str):
    """Sirve una versión del portal. Con ?demo=1 y PORTAL_DEMO_OPEN entra
    directo en modo demo (datos ficticios), sin pedir RUT ni código — para
    que cualquiera pueda VER el portal sin clave."""
    # no-store: el navegador siempre trae la última versión (el caché viejo
    # hacía que el dueño viera comportamientos ya corregidos: "sigue igual")
    resp = HTMLResponse(html, headers={"Cache-Control": "no-store, max-age=0"})
    from config import PORTAL_DEMO_OPEN
    if demo and PORTAL_DEMO_OPEN:
        from portal_routes import (
            _sign_portal_cookie, _COOKIE_NAME, _ACTIVE_COOKIE_NAME,
            _COOKIE_MAX_AGE, DEMO_RUT, DEMO_PHONE,
        )
        is_https = (request.url.scheme == "https"
                    or request.headers.get("x-forwarded-proto") == "https")
        resp.set_cookie(key=_COOKIE_NAME,
                        value=_sign_portal_cookie(DEMO_RUT, DEMO_PHONE),
                        max_age=_COOKIE_MAX_AGE, httponly=True,
                        samesite="lax", secure=is_https, path="/")
        resp.delete_cookie(key=_ACTIVE_COOKIE_NAME, path="/")
    return resp


@app.get("/portal", response_class=HTMLResponse)
def portal_page(request: Request, demo: str = ""):
    """Portal del paciente (auth client-side OTP). ?demo=1 -> modo demo sin clave."""
    return _serve_portal(_PORTAL_HTML, request, demo)


@app.get("/portal/v2", response_class=HTMLResponse)
def portal_page_v2(request: Request, demo: str = ""):
    """Portal v2 — tabs/sidebar. ?demo=1 -> modo demo sin clave."""
    return _serve_portal(_PORTAL_V2_HTML or _PORTAL_HTML, request, demo)


@app.get("/portal/v3", response_class=HTMLResponse)
def portal_page_v3(request: Request, demo: str = ""):
    """Portal v3 — V2 + Banderas rojas. ?demo=1 -> modo demo sin clave."""
    return _serve_portal(_PORTAL_V3_HTML or _PORTAL_V2_HTML or _PORTAL_HTML, request, demo)


@app.get("/portal/v4", response_class=HTMLResponse)
def portal_page_v4(request: Request, demo: str = ""):
    """Portal v4 — copia editable de v3 ("demo 2"). v3 queda congelada como demo 1."""
    return _serve_portal(_PORTAL_V4_HTML or _PORTAL_V3_HTML or _PORTAL_V2_HTML or _PORTAL_HTML, request, demo)


@app.get("/portal/informe", response_class=HTMLResponse)
def portal_informe():
    """Informe imprimible de registros del paciente (HTML print-friendly)."""
    return _PORTAL_INFORME_HTML


@app.get("/portal/demo", response_class=HTMLResponse)
def portal_demo_page(request: Request):
    """Puerta de demo: entra directo al portal con la sesion demo (datos
    ficticios), sin pedir telefono ni codigo. Gateada por PORTAL_DEMO_OPEN;
    con el flag apagado responde 404 y el login OTP de /portal queda intacto."""
    from config import PORTAL_DEMO_OPEN
    if not PORTAL_DEMO_OPEN:
        raise HTTPException(status_code=404, detail="Not found")
    from portal_routes import (
        _sign_portal_cookie, _COOKIE_NAME, _ACTIVE_COOKIE_NAME,
        _COOKIE_MAX_AGE, DEMO_RUT, DEMO_PHONE,
    )
    is_https = (request.url.scheme == "https"
                or request.headers.get("x-forwarded-proto") == "https")
    resp = HTMLResponse(_PORTAL_V2_HTML or _PORTAL_HTML)
    resp.set_cookie(
        key=_COOKIE_NAME,
        value=_sign_portal_cookie(DEMO_RUT, DEMO_PHONE),
        max_age=_COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=is_https,
        path="/",
    )
    resp.delete_cookie(key=_ACTIVE_COOKIE_NAME, path="/")
    return resp


@app.get("/mis-citas", response_class=HTMLResponse)
async def mis_citas_page(token: str = ""):
    """Portal ligero de citas por magic link.

    Token firmado HMAC-SHA256 con payload phone:exp, generado por
    portal_routes.generar_magic_token y enviado por WhatsApp al paciente.
    Válido 24h. Sin login: el link es el auth.
    """
    from portal_routes import verificar_magic_token
    from medilink import buscar_paciente, listar_citas_paciente
    from zoneinfo import ZoneInfo
    from datetime import datetime as _dt_mc, timedelta as _td_mc
    from session import get_profile, log_event as _le_mc

    _CHILE_TZ_MC = ZoneInfo("America/Santiago")
    _DIAS_ES = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
    _MESES_ES_MC = ["enero", "febrero", "marzo", "abril", "mayo", "junio",
                    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"]

    def _fecha_display(iso: str) -> str:
        try:
            d = _dt_mc.strptime(iso, "%Y-%m-%d").date()
            return f"{_DIAS_ES[d.weekday()]} {d.day} de {_MESES_ES_MC[d.month - 1]}"
        except Exception:
            return iso

    def _render_error() -> str:
        return (
            """<!DOCTYPE html><html lang="es"><head><meta charset="UTF-8">"""
            """<meta name="viewport" content="width=device-width,initial-scale=1">"""
            """<title>Link inválido — CMC</title>"""
            """<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap" rel="stylesheet">"""
            """<style>body{font-family:Inter,sans-serif;background:#F8FAFC;display:flex;"""
            """align-items:center;justify-content:center;min-height:100vh;padding:1rem}"""
            """.box{background:#fff;border:1px solid #CBD5E1;border-radius:16px;padding:2.5rem 2rem;"""
            """max-width:420px;text-align:center}h2{margin-bottom:.75rem;font-size:1.2rem}"""
            """p{color:#475569;font-size:.9rem;margin-bottom:1.5rem}"""
            """.btn{display:inline-block;background:#1B7A4A;color:#fff;padding:.65rem 1.5rem;"""
            """border-radius:8px;text-decoration:none;font-weight:600;font-size:.9rem}</style>"""
            """</head><body><div class="box"><div style="font-size:2.5rem;margin-bottom:.75rem">🔗</div>"""
            """<h2>Link inválido o expirado</h2>"""
            """<p>Los links de "Mis citas" son válidos por 24 horas.</p>"""
            """<a class="btn" href="https://wa.me/56966610737?text=Quiero+ver+mis+citas">"""
            """Pedir nuevo link por WhatsApp</a></div></body></html>"""
        )

    if not token:
        return HTMLResponse(_render_error(), status_code=401)

    phone = verificar_magic_token(token)
    if not phone:
        return HTMLResponse(_render_error(), status_code=401)

    # Obtener datos del paciente
    nombre = "Paciente"
    citas = []
    try:
        perfil = get_profile(phone)
        rut = (perfil or {}).get("rut", "")
        if rut:
            paciente = await buscar_paciente(rut)
            if paciente:
                nombre_raw = paciente.get("nombre", "")
                nombre = nombre_raw.split()[0].capitalize() if nombre_raw else "Paciente"
                hoy = _dt_mc.now(_CHILE_TZ_MC).date()
                hasta_iso = (hoy + _td_mc(days=90)).strftime("%Y-%m-%d")
                citas_raw = await listar_citas_paciente(
                    paciente["id"],
                    rut=rut,
                )
                # Filtrar localmente a los próximos 90 días
                citas_raw = [c for c in (citas_raw or []) if (c.get("fecha") or "") <= hasta_iso]
                for c in (citas_raw or []):
                    c["fecha_display"] = _fecha_display(c.get("fecha", ""))
                citas = citas_raw or []
        _le_mc(phone, "magic_link_visto", {"citas_count": len(citas)})
    except Exception as _e_mc:
        log.warning("mis_citas_page error: %s", _e_mc)

    # Construir HTML de citas
    def _card(c: dict) -> str:
        esp = c.get("especialidad", "—")
        prof = c.get("profesional", "—")
        fecha = c.get("fecha_display", c.get("fecha", "—"))
        hora = (c.get("hora_inicio") or "")[:5] or "—"
        modalidad = c.get("modalidad") or ""
        estado = c.get("estado") or "Confirmada"
        import urllib.parse
        wa_reagendar = f"https://wa.me/56966610737?text={urllib.parse.quote('Quiero reagendar mi cita de ' + esp + ' del ' + fecha)}"
        wa_cancelar  = f"https://wa.me/56966610737?text={urllib.parse.quote('Quiero cancelar mi cita de ' + esp + ' del ' + fecha)}"
        modal_row = f"<strong>Modalidad:</strong> {modalidad}<br>" if modalidad else ""
        return f"""
        <div style="background:#fff;border:1px solid #CBD5E1;border-radius:12px;padding:1.1rem 1.25rem;margin-bottom:1rem">
          <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:.6rem">
            <span style="font-weight:700;font-size:1rem">{esp}</span>
            <span style="font-size:.72rem;font-weight:600;background:#E8F5EE;color:#1B7A4A;padding:.2rem .55rem;border-radius:999px">{estado}</span>
          </div>
          <div style="font-size:.85rem;color:#475569;line-height:1.6">
            <strong style="color:#0F172A">Profesional:</strong> {prof}<br>
            <strong style="color:#0F172A">Fecha:</strong> {fecha}<br>
            <strong style="color:#0F172A">Hora:</strong> {hora}<br>
            {modal_row}
          </div>
          <div style="display:flex;gap:.6rem;margin-top:.9rem;flex-wrap:wrap">
            <a href="{wa_reagendar}" target="_blank"
               style="background:#F1F5F9;color:#0F172A;border:1px solid #CBD5E1;padding:.5rem 1rem;border-radius:8px;font-size:.82rem;font-weight:600;text-decoration:none">
              Reagendar
            </a>
            <a href="{wa_cancelar}" target="_blank"
               style="background:#FEF2F2;color:#DC2626;border:1px solid #FECACA;padding:.5rem 1rem;border-radius:8px;font-size:.82rem;font-weight:600;text-decoration:none">
              Cancelar
            </a>
          </div>
        </div>"""

    if citas:
        cards_html = "".join(_card(c) for c in citas)
        body_html = f"""<div class="greeting">Hola, {nombre}.</div>{cards_html}"""
    else:
        body_html = f"""
        <div class="greeting">Hola, {nombre}.</div>
        <div style="background:#fff;border:1px solid #CBD5E1;border-radius:12px;padding:2rem 1.5rem;text-align:center;color:#475569">
          <strong style="display:block;font-size:1rem;margin-bottom:.5rem">No tienes citas próximas</strong>
          <p>Puedes agendar una hora directamente por WhatsApp.</p><br>
          <a style="background:#1B7A4A;color:#fff;padding:.65rem 1.5rem;border-radius:8px;font-weight:600;font-size:.9rem;text-decoration:none"
             href="https://wa.me/56966610737?text=Quiero+agendar+una+hora">Agendar por WhatsApp</a>
        </div>"""

    html = f"""<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1.0">
  <title>Mis citas — Centro Médico Carampangue</title>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
  <style>
    *,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
    body{{font-family:'Inter',sans-serif;background:#F8FAFC;color:#0F172A;min-height:100vh}}
    header{{background:#1B7A4A;color:#fff;padding:1rem 1.5rem}}
    header h1{{font-size:1.1rem;font-weight:600}}
    header p{{font-size:.78rem;opacity:.8}}
    .container{{max-width:640px;margin:0 auto;padding:1.5rem 1rem}}
    .greeting{{font-size:1.05rem;font-weight:600;margin-bottom:1.25rem}}
    footer{{text-align:center;color:#475569;font-size:.75rem;padding:2rem 1rem 1.5rem}}
  </style>
</head>
<body>
<header>
  <h1>Centro Médico Carampangue</h1>
  <p>Mis próximas citas</p>
</header>
<div class="container">
  {body_html}
</div>
<footer>Centro Médico Carampangue · (44) 296 5226 ·
  <a href="https://wa.me/56966610737" style="color:inherit">WhatsApp</a>
</footer>
</body>
</html>"""

    return HTMLResponse(html)


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


@app.get("/alma/panel-dia", response_class=HTMLResponse)
def alma_panel_dia_demo(token: str | None = Query(None),
                        cmc_session: str | None = Cookie(None)):
    """Panel del Día (v1) — GATEADO con token de Alma (datos reales conectándose).

    Antes era público (mock para amigos). Ahora exige auth porque el Nivel 3
    (financiero) y, a futuro, el Nivel 2 (chat) consumen datos reales. Sin auth
    el frontend cae a mock; con token/cookie válidos inyectamos el token y el
    frontend llama /api/cmc/ebitda (real). Se re-lee de disco (no-cache).
    """
    from admin_routes import _verify_cookie, _is_admin_token
    authed_token = token if (token and _is_admin_token(token)) else None
    if not (authed_token or (cmc_session and _verify_cookie(cmc_session))):
        return RedirectResponse(url="/admin/login", status_code=302)
    tpl_path = _TEMPLATE_DIR / "alma_panel_dia_v1.html"
    if not tpl_path.exists():
        raise HTTPException(404, "Panel del Día no disponible")
    html = tpl_path.read_text(encoding="utf-8").replace("__PANEL_TOKEN__", authed_token or "")
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


def _read_template(name: str) -> str:
    p = _TEMPLATE_DIR / name
    return p.read_text(encoding="utf-8") if p.exists() else ""


@app.get("/suplementos", response_class=HTMLResponse)
def suplementos_page():
    """Dashboard MVP de inventario, ventas y ganancias para línea Farmacia/Suplementos."""
    html = _read_template("suplementos.html")
    if not html:
        raise HTTPException(404, "Suplementos MVP no disponible")
    return html


@app.get("/bi/mensual", response_class=HTMLResponse)
@app.get("/bi/dashboard-mensual", response_class=HTMLResponse)
def bi_dashboard_mensual_page():
    """Dashboard mensual Health BI (CMC): facturación por profesional/área, simulador honorarios."""
    html = _read_template("bi_dashboard_mensual.html")
    if not html:
        raise HTTPException(404, "Dashboard mensual no disponible")
    return html


@app.get("/bi/dia", response_class=HTMLResponse)
@app.get("/bi/dashboard-dia", response_class=HTMLResponse)
def bi_dashboard_dia_page():
    """Dashboard diario Health BI (CMC): caja del día, conciliación pagos."""
    html = _read_template("bi_dashboard_dia.html")
    if not html:
        raise HTTPException(404, "Dashboard diario no disponible")
    return html


@app.get("/bi/proyecto", response_class=HTMLResponse)
def bi_proyecto_page():
    """Mapa del proyecto Health BI: arquitectura, archivos clave, flujo ETL."""
    html = _read_template("bi_dashboard_proyecto.html")
    if not html:
        raise HTTPException(404, "Dashboard proyecto BI no disponible")
    return html


@app.get("/bi/farmacia-ideas", response_class=HTMLResponse)
def bi_farmacia_ideas_page():
    """Brainstorm farmacia/suplementos: ideas de catálogo, márgenes, plan."""
    html = _read_template("bi_farmacia_ideas.html")
    if not html:
        raise HTTPException(404, "Dashboard farmacia ideas no disponible")
    return html


# ── Farmacia CMC (sitio público sub-marca) ─────────────────────────────────

@app.get("/farmacia", response_class=HTMLResponse)
def farmacia_home():
    """Página madre de la Farmacia CMC — sub-marca del CMC bajo OLACORE."""
    html = _read_template("farmacia.html")
    if not html:
        raise HTTPException(404, "Farmacia no disponible")
    return html


_FARMACIA_PAGES = {
    "medicamentos": "Medicamentos · CENABAST y SNRE",
    "dermocosmetica": "Dermocosmética · Marcas curadas",
    "recetario-magistral": "Recetario magistral",
    "servicios-clinicos": "Servicios clínicos",
    "dental-supply": "Dental Supply CMC B2B",
}


@app.get("/farmacia/{page}", response_class=HTMLResponse)
def farmacia_subpage(page: str):
    """Sub-páginas verticales de la Farmacia CMC."""
    if page not in _FARMACIA_PAGES:
        return HTMLResponse("<h1>404 — página no encontrada</h1>", status_code=404)
    p = _TEMPLATE_DIR / "farmacia" / f"{page}.html"
    if not p.exists():
        raise HTTPException(404, f"Farmacia/{page} no disponible")
    return p.read_text(encoding="utf-8")


@app.get("/bi/meulen-roadmap", response_class=HTMLResponse)
def bi_meulen_roadmap_page():
    """Roadmap estratégico Meulen: fases, módulos, hitos."""
    html = _read_template("bi_meulen_roadmap.html")
    if not html:
        raise HTTPException(404, "Dashboard meulen roadmap no disponible")
    return html


@app.get("/bi/meulen-operaciones", response_class=HTMLResponse)
def bi_meulen_operaciones_page():
    """Dashboard operaciones internas Meulen: orden interno, procesos, métricas."""
    html = _read_template("bi_meulen_operaciones.html")
    if not html:
        raise HTTPException(404, "Dashboard meulen operaciones no disponible")
    return html


@app.get("/agentes", response_class=HTMLResponse)
@app.get("/agentes/dashboard", response_class=HTMLResponse)
def agentes_dashboard_page():
    """Mapa de subagentes Claude Code + automatizaciones del ecosistema OLACORE."""
    html = _read_template("agentes_dashboard.html")
    if not html:
        raise HTTPException(404, "Dashboard de agentes no disponible")
    return html


@app.get("/dashboards", response_class=HTMLResponse)
@app.get("/dashboards/", response_class=HTMLResponse)
@app.get("/mapa", response_class=HTMLResponse)
def dashboards_overview_page():
    """Mapa esquemático de todos los dashboards del ecosistema OLACORE (CMC, Meulen, Farmacia, BI)."""
    html = _read_template("dashboards_overview.html")
    if not html:
        raise HTTPException(404, "Mapa de dashboards no disponible")
    return html


@app.get("/menu", response_class=HTMLResponse)
def menu_page():
    """Landing esquemático con todas las rutas desplegadas en agentecmc.cl."""
    return _MENU_HTML


@app.get("/chequeos", response_class=HTMLResponse)
@app.get("/chequeo", response_class=HTMLResponse)
@app.get("/chequeos-preventivos", response_class=HTMLResponse)
def chequeos_page():
    """Landing pública de paquetes preventivos: Mujer 30+, Hombre 40+, Escolar, Deportivo."""
    return _CHEQUEOS_HTML


@app.get("/empresas", response_class=HTMLResponse)
@app.get("/empresa", response_class=HTMLResponse)
@app.get("/medicina-laboral", response_class=HTMLResponse)
@app.get("/convenio-empresas", response_class=HTMLResponse)
def empresas_page():
    """Landing convenios medicina laboral + tarifario imprimible (impresión print-only)."""
    return _EMPRESAS_HTML


_IDEAS_REVISION_HTML = (_TEMPLATE_DIR / "ideas_revision.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "ideas_revision.html").exists() else ""


@app.get("/ideas", response_class=HTMLResponse)
@app.get("/ideas-revision", response_class=HTMLResponse)
@app.get("/ideas/revision", response_class=HTMLResponse)
def ideas_revision_page():
    """Dashboard interno: features pausadas con feature flag — tabla de pendientes para Rodrigo."""
    return _IDEAS_REVISION_HTML



_COMUNA_TEMPLATE_HTML = (_TEMPLATE_DIR / "comuna_template.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "comuna_template.html").exists() else ""

_COMUNAS_DATA = {
    "curanilahue": {
        "name": "Curanilahue",
        "title": "Médicos en Curanilahue · Centro Médico Carampangue",
        "description": "Atención médica completa para pacientes de Curanilahue. 22 especialidades médicas y dentales a 25 minutos del centro. Bono Fonasa, agendamiento por WhatsApp.",
        "hero_lead": "Si vives en Curanilahue, el CMC está a 25 minutos. 22 especialidades médicas y dentales: medicina general, kinesiología, ginecología, pediatría, odontología, psicología, ecografías y más. Bono Fonasa MLE en consultas elegibles.",
        "km": "25", "time": "25 minutos", "bus": "Buses regulares Curanilahue–Arauco pasan por Carampangue",
        "transport": "Toma cualquier bus que vaya a Arauco o que pase por la Ruta 160 — todos hacen parada en Carampangue. Tiempo estimado en transporte público: 35-45 minutos.",
        "kine_note": "Ya atendemos pacientes recurrentes desde Curanilahue.",
    },
    "los-alamos": {
        "name": "Los Álamos",
        "title": "Médicos cerca de Los Álamos · Centro Médico Carampangue",
        "description": "Atención médica integral para pacientes de Los Álamos. CMC a 35 km, 22 especialidades, agendamiento por WhatsApp.",
        "hero_lead": "Si estás en Los Álamos, el Centro Médico Carampangue es la opción más cercana fuera de tu comuna. 22 especialidades médicas y dentales con tarifa Fonasa donde aplica.",
        "km": "35", "time": "40 minutos", "bus": "Buses Los Álamos–Concepción pasan cerca de Carampangue",
        "transport": "Buses Los Álamos a Concepción/Talcahuano vía Arauco pasan cerca del centro. También accesible en auto vía Ruta 160.",
        "kine_note": "Bono Fonasa MLE en kinesiología: 10 sesiones por $83.360.",
    },
    "canete": {
        "name": "Cañete",
        "title": "Médicos cerca de Cañete · Centro Médico Carampangue",
        "description": "Atención médica integral para pacientes de Cañete. 22 especialidades a 45 km, agendamiento por WhatsApp, Fonasa y particular.",
        "hero_lead": "Atendemos pacientes desde Cañete y comunas cercanas (Tirúa, Contulmo). 22 especialidades médicas y dentales. Bono Fonasa MLE disponible. Si necesitas algo que no encontraste en tu comuna, te esperamos.",
        "km": "45", "time": "55 minutos", "bus": "Buses Cañete–Concepción pasan por la zona",
        "transport": "Buses interregionales (Cañete a Concepción) hacen parada en Arauco, desde ahí 10 minutos a Carampangue. En auto, vía Ruta 160.",
        "kine_note": "Tratamientos extensos disponibles: kinesiología, psicología, ortodoncia.",
    },
    "lebu": {
        "name": "Lebu",
        "title": "Médicos cerca de Lebu · Centro Médico Carampangue",
        "description": "Atención médica integral para pacientes de Lebu. CMC en provincia de Arauco, 22 especialidades, agendamiento por WhatsApp.",
        "hero_lead": "Si estás en Lebu, capital de la provincia de Arauco, el CMC en Carampangue ofrece 22 especialidades médicas y dentales que pueden no estar disponibles en tu comuna. Bono Fonasa MLE en consultas elegibles.",
        "km": "55", "time": "1 hora 10 minutos", "bus": "Buses Lebu–Concepción vía Cañete y Arauco",
        "transport": "Buses Lebu a Concepción pasan por Cañete y Arauco. Desde Arauco son 10 minutos a Carampangue.",
        "kine_note": "22 especialidades disponibles en una sola visita.",
    },
}


async def _render_comuna_html(slug: str, *, for_wp: bool = False) -> str | None:
    """Render comuna landing con reviews dinámicas + schema.
    for_wp=True → indexable. for_wp=False → noindex (agentecmc.cl)."""
    slug = (slug or "").lower().replace("ñ", "n")
    data = _COMUNAS_DATA.get(slug)
    if not data:
        return None

    from google_rating import fetch_rating
    try:
        rating = await fetch_rating()
    except Exception:
        rating = {"rating": 4.8, "review_count": 14, "reviews": []}

    rv = float(rating.get("rating") or 4.8)
    rc = int(rating.get("review_count") or 14)
    reviews = rating.get("reviews") or []

    import json as _json
    reviews_schema = []
    reviews_html_parts = []
    for r in reviews[:3]:
        author = (r.get("author") or "Paciente CMC").replace('"', "\u0027")
        text = (r.get("text") or "").replace('"', "\u0027")[:400]
        stars = int(r.get("rating") or 5)
        when = r.get("relative_time") or ""
        publish = (r.get("publish_time") or "")[:10]
        reviews_schema.append(_json.dumps({
            "@type": "Review",
            "author": {"@type": "Person", "name": author},
            "reviewRating": {"@type": "Rating", "ratingValue": stars, "bestRating": 5},
            "datePublished": publish,
            "reviewBody": text,
        }, ensure_ascii=False))
        reviews_html_parts.append(
            f'<div class="review-card">'
            f'<div class="stars">{"★" * stars}{"☆" * (5 - stars)}</div>'
            f'<p class="text">"{text}"</p>'
            f'<div class="author">{author}</div>'
            f'<div class="date">{when}</div>'
            f'</div>'
        )
    if not reviews_html_parts:
        reviews_html_parts.append(
            '<div class="review-card"><div class="stars">★★★★★</div>'
            '<p class="text">"Excelente atención, médicos empáticos y secretaría rápida."</p>'
            '<div class="author">Paciente CMC</div></div>'
        )

    wa_text = f"Hola%2C%20vivo%20en%20{data['name'].replace(' ', '%20')}%20y%20quiero%20agendar"
    html = _COMUNA_TEMPLATE_HTML
    replacements = {
        "{{TITLE}}": data["title"],
        "{{DESCRIPTION}}": data["description"],
        "{{SLUG}}": slug,
        "{{COMUNA_NAME}}": data["name"],
        "{{HERO_LEAD}}": data["hero_lead"],
        "{{KM_DIST}}": data["km"],
        "{{TIME_DIST}}": data["time"],
        "{{BUS_DIST}}": data["bus"],
        "{{TRANSPORT_DESC}}": data["transport"],
        "{{KINE_NOTE}}": data["kine_note"],
        "{{WA_LINK}}": f"https://wa.me/56966610737?text={wa_text}",
        "{{RATING_VALUE}}": f"{rv:.1f}",
        "{{RATING_COUNT}}": str(rc),
        "{{REVIEWS_SCHEMA}}": ",".join(reviews_schema),
        "{{REVIEWS_HTML}}": "".join(reviews_html_parts),
        "{{ROBOTS}}": "index,follow" if for_wp else "noindex,nofollow",
    }
    for k, v in replacements.items():
        html = html.replace(k, v)
    return html


@app.get("/curanilahue", response_class=HTMLResponse)
@app.get("/los-alamos", response_class=HTMLResponse)
@app.get("/losalamos", response_class=HTMLResponse)
@app.get("/canete", response_class=HTMLResponse)
@app.get("/cañete", response_class=HTMLResponse)
@app.get("/lebu", response_class=HTMLResponse)
async def comuna_page(request: Request):
    """Landing por comuna. Default agentecmc.cl: noindex,nofollow.
    Si ?for_wp=1 (usado por Snippet 8 desde WP): indexable."""
    url_path = request.url.path.lstrip("/").rstrip("/").lower()
    if url_path.startswith("comuna/"):
        slug = url_path.split("/", 1)[1]
    else:
        slug = url_path
    slug = slug.replace("ñ", "n")
    if slug == "losalamos": slug = "los-alamos"
    for_wp = request.query_params.get("for_wp") in ("1","true","yes")
    html = await _render_comuna_html(slug, for_wp=for_wp)
    if html is None:
        return HTMLResponse("<h1>404</h1><p>Comuna no encontrada</p>", status_code=404)
    return HTMLResponse(html)


def _seo_api_auth(token: str, cmc_session: str | None) -> None:
    """Acepta auth via ?token=... o cookie cmc_session admin. 401 si no."""
    if token == ADMIN_TOKEN:
        return
    from admin_routes import _verify_cookie
    if _verify_cookie(cmc_session or "") == "admin":
        return
    raise HTTPException(401, "unauthorized")


@app.get("/seo", response_class=HTMLResponse)
@app.get("/seo/dashboard", response_class=HTMLResponse)
@app.get("/seo-dashboard", response_class=HTMLResponse)
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


@app.get("/crecimiento", response_class=HTMLResponse)
@app.get("/crecimientopersonal", response_class=HTMLResponse)
@app.get("/crecimiento-personal", response_class=HTMLResponse)
def crecimiento_personal_page():
    """Roadmap personal de aprendizaje del Dr. Olavarría.
    Sin auth: es plan personal, no contiene datos sensibles del CMC."""
    if not _CRECIMIENTO_PERSONAL_HTML:
        raise HTTPException(404, "Dashboard Crecimiento Personal no disponible")
    return _CRECIMIENTO_PERSONAL_HTML



@app.get("/personal", response_class=HTMLResponse)
@app.get("/tableros", response_class=HTMLResponse)
def personal_hub_page():
    if not _PERSONAL_HUB_HTML:
        raise HTTPException(404, "Hub no disponible")
    return _PERSONAL_HUB_HTML


@app.get("/ruta", response_class=HTMLResponse)
@app.get("/rutapersonal", response_class=HTMLResponse)
@app.get("/ruta-personal", response_class=HTMLResponse)
@app.get("/maparuta", response_class=HTMLResponse)
def ruta_personal_page():
    if not _RUTA_PERSONAL_HTML:
        raise HTTPException(404, "Dashboard Ruta Personal no disponible")
    return _RUTA_PERSONAL_HTML


@app.get("/brujula", response_class=HTMLResponse)
def brujula_personal_page():
    if not _BRUJULA_HTML:
        raise HTTPException(404, "Dashboard Brújula no disponible")
    return _BRUJULA_HTML


@app.get("/caminos", response_class=HTMLResponse)
@app.get("/jugadas", response_class=HTMLResponse)
def caminos_page():
    if not _CAMINOS_HTML:
        raise HTTPException(404, "Dashboard Caminos no disponible")
    return _CAMINOS_HTML


@app.get("/adkun", response_class=HTMLResponse)
def adkun_landing():
    """Landing pública Adkun — empresa de software."""
    if not _ADKUN_LANDING_HTML:
        raise HTTPException(404, "Landing Adkun no disponible")
    return _ADKUN_LANDING_HTML


@app.get("/adkun/brand", response_class=HTMLResponse)
def adkun_company_board():
    """Brand board Adkun — empresa."""
    if not _ADKUN_COMPANY_HTML:
        raise HTTPException(404, "Brand board Adkun no disponible")
    return _ADKUN_COMPANY_HTML


@app.get("/alma/branding", response_class=HTMLResponse)
def alma_product_board(token: str | None = Query(None),
                       cmc_session: str | None = Cookie(None)):
    """Brand board Alma — producto. Misma auth que el resto de módulos Alma."""
    from admin_routes import _verify_cookie, _is_admin_token
    if not _ALMA_PRODUCT_HTML:
        raise HTTPException(404, "Brand board Alma no disponible")
    if token and _is_admin_token(token):
        return _ALMA_PRODUCT_HTML
    if cmc_session and _verify_cookie(cmc_session) in ("admin", "ortodoncia"):
        return _ALMA_PRODUCT_HTML
    return RedirectResponse(url="/admin/login", status_code=302)


@app.get("/meta", response_class=HTMLResponse)
@app.get("/meta/dashboard", response_class=HTMLResponse)
@app.get("/meta-dashboard", response_class=HTMLResponse)
def meta_dashboard_page():
    """Dashboard dedicado de Meta Ads — el mayor canal de inversión y captación.
    Sin auth de cookie: usa el token del .env via /api/seo/meta-ads."""
    if not _META_DASHBOARD_HTML:
        raise HTTPException(404, "Dashboard Meta no disponible")
    return _META_DASHBOARD_HTML


@app.get("/camino-50m", response_class=HTMLResponse)
@app.get("/camino/50m", response_class=HTMLResponse)
@app.get("/50m", response_class=HTMLResponse)
def camino_50m_page():
    """Dashboard Camino a 50M — 8 palancas de crecimiento CMC hacia 50M/mes."""
    if not _CAMINO_50M_HTML:
        raise HTTPException(404, "Dashboard Camino 50M no disponible")
    return _CAMINO_50M_HTML


@app.get("/horizonte", response_class=HTMLResponse)
@app.get("/horizonte/dashboard", response_class=HTMLResponse)
def horizonte_dashboard_page():
    """Roadmap estratégico de largo plazo del CMC — escenarios A/B/C, pipeline contratación, KPIs."""
    if not _HORIZONTE_DASHBOARD_HTML:
        raise HTTPException(404, "Dashboard Horizonte no disponible")
    return _HORIZONTE_DASHBOARD_HTML


_SEGMENTACION_CMC_HTML = (_TEMPLATE_DIR / "segmentacion_cmc.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "segmentacion_cmc.html").exists() else ""

@app.get("/segmentacioncmc", response_class=HTMLResponse)
@app.get("/segmentacioncmc/", response_class=HTMLResponse)
def segmentacioncmc_page():
    """Dashboard de segmentación de pacientes CMC (snapshot estático del BI local)."""
    if not _SEGMENTACION_CMC_HTML:
        raise HTTPException(404, "Dashboard Segmentación CMC no disponible")
    return _SEGMENTACION_CMC_HTML


_QR_OPTIN_HTML = (_TEMPLATE_DIR / "qr_optin.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "qr_optin.html").exists() else ""

@app.get("/qr-optin", response_class=HTMLResponse)
@app.get("/qr-optin/", response_class=HTMLResponse)
@app.get("/activar-whatsapp", response_class=HTMLResponse)
def qr_optin_page():
    """Página pública con QR para activar WhatsApp en recepción. El paciente
    atendido offline escanea, WhatsApp se abre con texto pre-llenado, el bot
    captura el opt-in formal con botones (Ley 19.628)."""
    if not _QR_OPTIN_HTML:
        raise HTTPException(404, "Página de opt-in no disponible")
    return _QR_OPTIN_HTML


_CMC_SNAPSHOT_DIR = Path("/opt/chatbot-cmc/data/cmc_snapshot")

@app.get("/segmentacioncmc-data/{path:path}", include_in_schema=False)
def segmentacioncmc_data(path: str):
    """Sirve los JSONs del snapshot del BI local. Path traversal protegido."""
    # Sanitización: rechazar paths con .. o absolutos
    if ".." in path or path.startswith("/"):
        raise HTTPException(400, "Path inválido")
    fpath = (_CMC_SNAPSHOT_DIR / path).resolve()
    try:
        fpath.relative_to(_CMC_SNAPSHOT_DIR.resolve())
    except ValueError:
        raise HTTPException(400, "Path fuera del directorio permitido")
    if not fpath.is_file():
        raise HTTPException(404, "Archivo no encontrado")
    media = "application/json" if fpath.suffix == ".json" else "text/plain"
    return FileResponse(fpath, media_type=media,
                        headers={"Cache-Control": "public, max-age=300"})


_ATRIBUCION_DASHBOARD_HTML = (_TEMPLATE_DIR / "atribucion_dashboard.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "atribucion_dashboard.html").exists() else ""
_ARQUITECTURA_SAAS_HTML = (_TEMPLATE_DIR / "arquitectura_saas.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "arquitectura_saas.html").exists() else ""
_PORTADA_OLACORE_HTML = (_TEMPLATE_DIR / "portada_olacore.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "portada_olacore.html").exists() else ""
_ABARCA_DASHBOARD_HTML = (_TEMPLATE_DIR / "abarca_dashboard.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "abarca_dashboard.html").exists() else ""
_REEMPLAZO_INGRESO_DASHBOARD_HTML = (_TEMPLATE_DIR / "reemplazo_ingreso_dashboard.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "reemplazo_ingreso_dashboard.html").exists() else ""
_OLAVARRIA_DASHBOARD_HTML = (_TEMPLATE_DIR / "olavarria_dashboard.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "olavarria_dashboard.html").exists() else ""
_PROF_DASHBOARD_HTML = (_TEMPLATE_DIR / "profesional_dashboard.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "profesional_dashboard.html").exists() else ""
# Genérico por id (BI v2, sin token) — VIVE APARTE del de token a propósito:
# fc7db14 sobrescribió profesional_dashboard.html (que era este genérico, creado
# en b246e84) con el dashboard semanal HMAC, y /profesional/{id} quedó sirviendo
# el front equivocado → pantalla en blanco. Restaurado con nombre propio.
_PROF_BI_DASHBOARD_HTML = (_TEMPLATE_DIR / "profesional_bi_dashboard.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "profesional_bi_dashboard.html").exists() else ""
_WINBACK_DASHBOARD_HTML = (_TEMPLATE_DIR / "winback_dashboard.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "winback_dashboard.html").exists() else ""
_WINBACK_DENTAL_DASHBOARD_HTML = (_TEMPLATE_DIR / "winback_dental_dashboard.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "winback_dental_dashboard.html").exists() else ""
_BOXES_DASHBOARD_HTML = (_TEMPLATE_DIR / "boxes_dashboard.html").read_text(encoding="utf-8")
_ALMA_HTML = (_TEMPLATE_DIR / "alma.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "alma.html").exists() else ""
_KINTU_HTML = (_TEMPLATE_DIR / "kintu.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "kintu.html").exists() else ""
_ALMA_AGENDA_HTML = (_TEMPLATE_DIR / "alma_agenda.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "alma_agenda.html").exists() else ""
_AGENDADOR_HTML = (_TEMPLATE_DIR / "agendador.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "agendador.html").exists() else ""
_AGENDADOR_PORTAL_HTML = (_TEMPLATE_DIR / "agendador_portal.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "agendador_portal.html").exists() else ""
_AGENDADOR_V2_HTML = (_TEMPLATE_DIR / "agendador_v2.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "agendador_v2.html").exists() else ""
_VECINO_MEULEN_HTML = (_TEMPLATE_DIR / "vecino_meulen.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "vecino_meulen.html").exists() else ""
_ALMA_PAGOS_HTML  = (_TEMPLATE_DIR / "alma_pagos.html").read_text(encoding="utf-8")  if (_TEMPLATE_DIR / "alma_pagos.html").exists()  else ""
_ALMA_PAGOS_SIMPLE_HTML = (_TEMPLATE_DIR / "alma_pagos_simple.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "alma_pagos_simple.html").exists() else ""
_ALMA_ABONOS_HTML = (_TEMPLATE_DIR / "alma_abonos.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "alma_abonos.html").exists() else ""
_ALMA_CAJA_DIARIA_HTML = (_TEMPLATE_DIR / "alma_caja_diaria.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "alma_caja_diaria.html").exists() else ""
_ALMA_CAJA_DIARIA_SIMPLE_HTML = (_TEMPLATE_DIR / "alma_caja_diaria_simple.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "alma_caja_diaria_simple.html").exists() else ""
_CHECKIN_HTML = (_TEMPLATE_DIR / "checkin.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "checkin.html").exists() else ""
_ALMA_SALA_HTML = (_TEMPLATE_DIR / "alma_sala.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "alma_sala.html").exists() else ""
_ALMA_ENVIOS_HTML = (_TEMPLATE_DIR / "alma_envios.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "alma_envios.html").exists() else ""
_ALMA_PAGOS_MEDILINK_HTML = (_TEMPLATE_DIR / "alma_pagos_medilink.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "alma_pagos_medilink.html").exists() else ""
_ALMA_CONCILIACION_HTML = (_TEMPLATE_DIR / "alma_conciliacion.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "alma_conciliacion.html").exists() else ""
_ALMA_INVENTARIO_HTML = (_TEMPLATE_DIR / "alma_inventario.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "alma_inventario.html").exists() else ""
_ALMA_RECEPCION_KANBAN_HTML = (_TEMPLATE_DIR / "alma_recepcion_kanban.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "alma_recepcion_kanban.html").exists() else ""
_ALMA_RECEPCION_KANBAN_V2_HTML = (_TEMPLATE_DIR / "alma_recepcion_kanban_v2.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "alma_recepcion_kanban_v2.html").exists() else ""
_ALMA_RECEPCION_KANBAN_V3_HTML = (_TEMPLATE_DIR / "alma_recepcion_kanban_v3.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "alma_recepcion_kanban_v3.html").exists() else ""
_ALMA_ORTODONCIA_HTML = (_TEMPLATE_DIR / "alma_ortodoncia.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "alma_ortodoncia.html").exists() else ""
_ALMA_KINE_HTML = (_TEMPLATE_DIR / "alma_kine.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "alma_kine.html").exists() else ""
_ALMA_PROGRAMAS_HTML = (_TEMPLATE_DIR / "alma_programas.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "alma_programas.html").exists() else ""
_ALMA_DASHBOARDS_HTML = (_TEMPLATE_DIR / "alma_dashboards.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "alma_dashboards.html").exists() else ""
_OLACORE_ESTRUCTURA_HTML = (_TEMPLATE_DIR / "olacore_estructura.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "olacore_estructura.html").exists() else ""
_OLACORE_HOLDING_HTML = (_TEMPLATE_DIR / "olacore_holding.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "olacore_holding.html").exists() else ""
_OLACORE_REUNION_HTML = (_TEMPLATE_DIR / "olacore_reunion.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "olacore_reunion.html").exists() else ""
_OLACORE_PORTAL_HTML = (_TEMPLATE_DIR / "olacore_portal.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "olacore_portal.html").exists() else ""
# Token dedicado para compartir SOLO los documentos del holding (no da acceso al resto de Alma).
OLACORE_HOLDING_TOKEN = "olacore_holding_2026"
_ALMA_PACIENTES_HTML = (_TEMPLATE_DIR / "alma_pacientes.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "alma_pacientes.html").exists() else ""
_ALMA_INTERCONSULTAS_HTML = (_TEMPLATE_DIR / "alma_interconsultas.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "alma_interconsultas.html").exists() else ""
_ALMA_ESTERILIZACION_HTML = (_TEMPLATE_DIR / "alma_esterilizacion.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "alma_esterilizacion.html").exists() else ""
_ALMA_FINANZAS_HTML = (_TEMPLATE_DIR / "alma_finanzas.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "alma_finanzas.html").exists() else ""
_ALMA_EQUIPO_HTML = (_TEMPLATE_DIR / "alma_equipo.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "alma_equipo.html").exists() else ""
_ALMA_DOCUMENTOS_HTML = (_TEMPLATE_DIR / "alma_documentos.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "alma_documentos.html").exists() else ""
_ALMA_HABILITACION_HTML = (_TEMPLATE_DIR / "alma_habilitacion.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "alma_habilitacion.html").exists() else ""
_ALMA_MANTENCION_HTML = (_TEMPLATE_DIR / "alma_mantencion.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "alma_mantencion.html").exists() else ""
_ALMA_CALIDAD_HTML = (_TEMPLATE_DIR / "alma_calidad.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "alma_calidad.html").exists() else ""
_ALMA_EXAMENES_HTML = (_TEMPLATE_DIR / "alma_examenes.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "alma_examenes.html").exists() else ""
_ALMA_TAREAS_HTML = (_TEMPLATE_DIR / "alma_tareas.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "alma_tareas.html").exists() else ""
_ALMA_CHECKLIST_HTML = (_TEMPLATE_DIR / "alma_checklist.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "alma_checklist.html").exists() else ""
_ALMA_LIQUIDACIONES_HTML = (_TEMPLATE_DIR / "alma_liquidaciones.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "alma_liquidaciones.html").exists() else ""
_ALMA_INICIO_HTML = (_TEMPLATE_DIR / "alma_inicio.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "alma_inicio.html").exists() else ""
_ALMA_PROVEEDORES_HTML = (_TEMPLATE_DIR / "alma_proveedores.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "alma_proveedores.html").exists() else ""
_ALMA_MEJORAS_HTML = (_TEMPLATE_DIR / "alma_mejoras.html").read_text(encoding="utf-8") if (_TEMPLATE_DIR / "alma_mejoras.html").exists() else ""

def _make_alma_page(_html, _label):
    """Factory de páginas Alma simples (template con __TOKEN__, misma auth que el shell)."""
    def _page(token: str | None = Query(None), cmc_session: str | None = Cookie(None)):
        from admin_routes import _verify_cookie, _is_admin_token
        if not _html:
            raise HTTPException(404, f"{_label} no disponible")
        if token and _is_admin_token(token):
            return HTMLResponse(_html.replace("__TOKEN__", token))
        if cmc_session:
            role = _verify_cookie(cmc_session)
            if role in ("admin", "ortodoncia"):
                return HTMLResponse(_html.replace("__TOKEN__", ADMIN_TOKEN))
        return RedirectResponse(url="/admin/login", status_code=302)
    return _page

for _ap, _ah, _al in [
    ("/alma/pacientes", _ALMA_PACIENTES_HTML, "Pacientes"),
    ("/alma/interconsultas", _ALMA_INTERCONSULTAS_HTML, "Interconsultas"),
    ("/alma/esterilizacion", _ALMA_ESTERILIZACION_HTML, "Esterilizacion"),
    ("/alma/finanzas", _ALMA_FINANZAS_HTML, "Finanzas"),
    ("/alma/equipo", _ALMA_EQUIPO_HTML, "Equipo"),
    ("/alma/documentos", _ALMA_DOCUMENTOS_HTML, "Documentos"),
    ("/alma/habilitacion", _ALMA_HABILITACION_HTML, "Habilitacion"),
    ("/alma/mantencion", _ALMA_MANTENCION_HTML, "Mantencion"),
    ("/alma/calidad", _ALMA_CALIDAD_HTML, "Calidad"),
    ("/alma/examenes", _ALMA_EXAMENES_HTML, "Examenes"),
    ("/alma/tareas", _ALMA_TAREAS_HTML, "Tareas"),
    ("/alma/checklist", _ALMA_CHECKLIST_HTML, "Checklist"),
    ("/alma/liquidaciones", _ALMA_LIQUIDACIONES_HTML, "Liquidaciones"),
    ("/alma/inicio", _ALMA_INICIO_HTML, "Inicio"),
    ("/alma/proveedores", _ALMA_PROVEEDORES_HTML, "Proveedores"),
    ("/alma/mejoras", _ALMA_MEJORAS_HTML, "Mejoras"),
]:
    app.add_api_route(_ap, _make_alma_page(_ah, _al), methods=["GET"], response_class=HTMLResponse, include_in_schema=False)

# ── Pool de conexiones BI para endpoints de boxes ────────────────────────────
# Máximo 8 conexiones compartidas entre boxes-state, boxes-config y boxes-config-put.
# Esto acota el peor caso: nunca más de 8 conexiones PG abiertas desde este proceso
# para este subsistema, independientemente de cuántos clientes polleen el dashboard.
# Importante: la conexión DEBE retornarse al pool (putconn) en finally.
import threading as _threading_boxes

_BI_POOL: "psycopg2.pool.ThreadedConnectionPool | None" = None  # type: ignore[name-defined]
_BI_POOL_LOCK = _threading_boxes.Lock()

def _bi_pool() -> "psycopg2.pool.ThreadedConnectionPool":  # type: ignore[name-defined]
    """Devuelve el pool compartido (inicialización lazy, thread-safe)."""
    global _BI_POOL
    if _BI_POOL is None:
        with _BI_POOL_LOCK:
            if _BI_POOL is None:
                import psycopg2.pool as _pg_pool
                import os as _osp
                _BI_POOL = _pg_pool.ThreadedConnectionPool(
                    minconn=1,
                    maxconn=8,
                    host=_osp.getenv("BI_DB_HOST", "127.0.0.1"),
                    port=int(_osp.getenv("BI_DB_PORT", "5432")),
                    dbname=_osp.getenv("BI_DB_NAME", "health_bi"),
                    user=_osp.getenv("BI_DB_USER", "health_user"),
                    password=_osp.getenv("BI_DB_PASSWORD", "password123"),
                    connect_timeout=5,
                )
    return _BI_POOL

# ── Boxes CMC — gemelo digital ────────────────────────────────────────────────
# uso_autorizado: para qué está autorizada cada sala. El expediente de
# habilitación SEREMI declara un uso por recinto y el gemelo ya sabe qué pasa de
# verdad en cada uno: con este campo el módulo pasa a ser evidencia de uso real
# para la tramitación, y puede avisar si una sala se está ocupando para algo
# distinto de lo declarado.
#
# Config inicial: 8 boxes en 2 pisos. Pool dinámico vs profesional fijo.
# "pool" = cualquier profesional listado en default_profs entra al box; el primero
# con cita activa ocupa este box, el segundo el siguiente del mismo tipo.
# "fijo" = solo este profesional ocupa este box (kine 1 = Armijo, kine 2 = Etcheverry).
# Estados que devuelve Medilink en `estado_cita`, medidos sobre citas reales
# (2026-07-31, muestra de 50): "No confirmado" 70%, "Atendido" 16%, "Anulado"
# 10%, "No asiste" 2%, "Cambio de fecha" 2%.
#
# OJO: son en ESPAÑOL. El filtro viejo buscaba ("agendada","atendida",
# "confirmada","en_curso") y NINGUNO calza. Eso no se notaba porque el campo se
# leía como `estado` —que Medilink no devuelve— y todo caía al default
# "agendada", que sí estaba en la lista. Arreglar sólo la lectura habría hecho
# desaparecer TODAS las citas del gemelo.
#
# Ocupan la sala: la agendada que aún no se confirma y la ya atendida.
# NO ocupan: anulada, no asiste (justamente el no-show que antes pintaba la
# sala llena) y cambio de fecha.
# Se define por EXCLUSIÓN, no por lista blanca. Medilink tiene más variantes de
# las que se ven en una muestra chica: además de "No confirmado" y "Atendido"
# aparecen "Confirmado por email", "Confirmado por teléfono" y "Notificado via
# email". Con lista blanca, cada variante nueva hacía desaparecer citas en
# silencio — que es justo el modo de fallar que hay que evitar en un tablero.
# Acá la cita OCUPA la sala salvo que su estado diga explícitamente que no.
_NO_OCUPAN_FRAGMENTOS = ("anulad", "no asiste", "no asistio", "no asistió",
                         "cambio de fecha", "cancelad", "reagendad")

def estado_ocupa_sala(estado: str | None) -> bool:
    e = (estado or "").strip().lower()
    if not e:
        return True                      # sin dato → se muestra, no se esconde
    return not any(f in e for f in _NO_OCUPAN_FRAGMENTOS)

def estado_es_no_show(estado: str | None) -> bool:
    e = (estado or "").strip().lower()
    return any(f in e for f in ("no asiste", "no asistio", "no asistió"))

BOXES_CONFIG = [
    # ⚠️ LOS IDS NO COINCIDEN CON LOS NOMBRES — y es a propósito.
    #
    # `box3` se llama "Box 4" y `box4` se llama "Box 3". Se ve mal, pero los ids
    # son la llave con la que `bi.boxes_asignacion_log` guarda meses de historial
    # de lo que marcó recepción. Renombrarlos reasignaría en silencio todos los
    # registros pasados a la sala equivocada. El id es interno; el nombre es lo
    # que ve la gente, y el nombre es el correcto. No los "arregles".
    #
    # Planta confirmada con el dueño el 2026-08-06.
    #
    # default_profs  = quién ocupa la sala (asignación física).
    # revenue_profs  = de quién es la plata. Particiona sin solape ni hueco:
    #                  cada profesional cuenta en UNA sola sala.

    # ── Piso 1 ────────────────────────────────────────────────────────────
    # Box 1 y Box 2 comparten grupo: si uno se llena, se desborda al otro.
    # Box 2 es "de" Márquez y Quijano, pero no rechaza a nadie estando libre
    # (decisión del dueño: la única sala verdaderamente excluyente es la dental).
    {"id": "box1", "piso": 1, "orden": 1, "nombre": "Box 1", "tipo": "general",
     "modo": "pool", "pool_group": "general",
     "default_profs": [1, 73, 23, 60, 61, 68], "revenue_profs": [1, 73, 23, 60, 61, 68],
     "uso_autorizado": "consulta médica y ecografía"},
    {"id": "box2", "piso": 1, "orden": 2, "nombre": "Box 2", "tipo": "general",
     "modo": "pool", "pool_group": "general",
     "default_profs": [13, 65], "revenue_profs": [13, 65],
     "uso_autorizado": "consulta médica (Dr. Márquez y Dr. Quijano)"},
    # Las dos salas de kine son un grupo: los kinesiólogos pueden ocupar las dos
    # a la vez cuando coinciden, aunque por lo general les basta una.
    {"id": "kine1", "piso": 1, "orden": 3, "nombre": "Kinesiología 1", "tipo": "kinesiología",
     "modo": "pool", "pool_group": "kine",
     "default_profs": [21, 77], "revenue_profs": [21, 77],
     "uso_autorizado": "kinesiología"},
    {"id": "kine2", "piso": 1, "orden": 4, "nombre": "Kinesiología 2", "tipo": "kinesiología",
     "modo": "pool", "pool_group": "kine",
     "default_profs": [59, 70, 80], "revenue_profs": [59, 70, 80],
     "uso_autorizado": "masoterapia, fonoaudiología y oftalmología"},

    # ── Piso 2 ────────────────────────────────────────────────────────────
    {"id": "box4", "piso": 2, "orden": 1, "nombre": "Box 3", "tipo": "psicología",
     "modo": "pool", "pool_group": "psico",
     "default_profs": [74, 49], "revenue_profs": [74, 49],
     "uso_autorizado": "consulta psicológica (multiuso)"},
    {"id": "box3", "piso": 2, "orden": 2, "nombre": "Box 4", "tipo": "procedimientos",
     "modo": "pool", "pool_group": "proced",
     "default_profs": [67, 56], "revenue_profs": [67, 56],
     "uso_autorizado": "matrona y podología (multiuso)"},
    {"id": "box5", "piso": 2, "orden": 3, "nombre": "Box 5", "tipo": "nutrición",
     "modo": "pool", "pool_group": "nutri",
     "default_profs": [52], "revenue_profs": [52],
     "uso_autorizado": "nutrición (multiuso)"},
    # Única sala EXCLUYENTE del centro: tiene sillón y nadie más la ocupa.
    {"id": "boxdental", "piso": 2, "orden": 4, "nombre": "Box Dental", "tipo": "dental",
     "modo": "pool", "pool_group": "dental",
     "default_profs": [55, 72, 66, 75, 69, 76], "revenue_profs": [55, 72, 66, 75, 69, 76],
     "uso_autorizado": "odontología"},

    # ── Sin sala ──────────────────────────────────────────────────────────
    # Ocupan AGENDA pero no metros cuadrados. Van en su propio carril para no
    # ensuciar el cálculo de cuánto se puede crecer sin construir. Se pueblan
    # desde la ficha del profesional (`profesionales_telemedicina`).
    {"id": "telemed", "piso": 0, "orden": 1, "nombre": "Telemedicina", "tipo": "telemedicina",
     "modo": "pool", "pool_group": "telemed",
     "default_profs": [], "revenue_profs": [], "virtual": True,
     "uso_autorizado": "teleconsulta (sin sala física)"},
]

# Profesionales CAUTIVOS: sólo pueden atender en ciertas salas y nunca se
# reparten al resto del pool. Es distinto de la prioridad: el cautivo no desplaza
# a nadie, simplemente no puede estar en otro lado.
#
# `excepcion` son salas permitidas fuera de su set habitual, para casos puntuales
# (no entran al reparto automático; sirven para no marcar un choque falso cuando
# de verdad ocurre).
#
# Datos confirmados con el dueño 2026-08-06.
CAUTIVOS = {
    77: {"salas": ["kine1", "kine2"], "motivo": "kinesiología"},
    21: {"salas": ["kine1", "kine2"], "motivo": "kinesiología"},
    59: {"salas": ["kine1", "kine2"], "motivo": "masoterapia; su sala es Kine 2"},
    # Dental: todos al Box Dental. Javiera además puede usar un box del piso 1
    # cuando el paciente no puede subir al segundo — es excepción, no rutina.
    55: {"salas": ["boxdental"], "excepcion": ["box1", "box2"],
         "motivo": "dental; usa piso 1 si el paciente no puede subir"},
    72: {"salas": ["boxdental"], "motivo": "dental"},
    66: {"salas": ["boxdental"], "motivo": "dental"},
    75: {"salas": ["boxdental"], "motivo": "dental"},
    69: {"salas": ["boxdental"], "motivo": "dental"},
    76: {"salas": ["boxdental"], "motivo": "dental"},
}


def profesionales_telemedicina() -> list[int]:
    """Quién atiende por videollamada, según la ficha del profesional.

    FUENTE ÚNICA: `PROFESIONALES[id]["telemedicina"]` en medilink.py — la misma
    marca que usa el bot para avisarle al paciente que su hora es teleconsulta.

    Antes el carril de Telemedicina del mapa tenía su propia lista escrita a
    mano, así que había dos verdades: se podía sacar a alguien del carril y el
    bot seguía diciendo "videollamada", o al revés. Reasignar ahora es cambiar
    la marca en la ficha y las dos cosas se mueven juntas.
    """
    try:
        from medilink import PROFESIONALES
        return sorted(pid for pid, d in PROFESIONALES.items() if d.get("telemedicina"))
    except Exception as e:  # noqa: BLE001
        log.warning("boxes: no se pudo leer la marca de telemedicina (%s)", e)
        return []


def salas_permitidas(prof_id: int, incluir_excepciones: bool = False) -> list | None:
    """Salas donde este profesional PUEDE estar, o None si no está restringido."""
    c = CAUTIVOS.get(prof_id)
    if not c:
        return None
    salas = list(c["salas"])
    if incluir_excepciones:
        salas += [x for x in (c.get("excepcion") or []) if x not in salas]
    return salas


# Salas EXCLUYENTES: sólo admiten a sus propios profesionales, aunque estén
# vacías.
#
# OJO — esto es un eje DISTINTO de `CAUTIVOS`, y confundirlos fue un error mío
# que el dueño corrigió el 2026-08-06:
#
#   · CAUTIVOS  restringe al PROFESIONAL: el kinesiólogo sólo trabaja en salas
#     de kine porque ahí está la camilla y el equipo.
#   · EXCLUYENTES restringe la SALA: quién más puede entrar cuando está libre.
#
# No son lo mismo. Un kinesiólogo no puede salir de kine, pero **un médico sí
# puede usar una sala de kine si está desocupada** — la sala no es exclusiva,
# el profesional es el limitado. La única sala verdaderamente excluyente del
# centro es la dental: tiene sillón, y nadie más la ocupa. Todas las demás
# (Box 1 a 5 y las de kine) son multiuso y se pueden usar indistintamente.
SALAS_EXCLUYENTES = {"boxdental"}


def sala_acepta(box_id: str, prof_id: int) -> bool:
    """¿Este profesional puede ocupar esta sala? Cruza los DOS ejes de arriba."""
    perm = salas_permitidas(prof_id, incluir_excepciones=True)
    if perm is not None and box_id not in perm:
        return False                      # el profesional no puede salir de lo suyo
    if box_id in SALAS_EXCLUYENTES:
        # Sala exclusiva: sólo entra quien la tiene asignada de origen.
        for b in BOXES_CONFIG:
            if b["id"] == box_id:
                return prof_id in (b.get("default_profs") or [])
    return True


# Reglas de PRIORIDAD entre profesionales por sala.
#
# El modelo de boxes tiene dos modos: "pool" (el primero que llega toma la sala)
# y "fijo" (siempre el mismo). Ninguno representa lo que pasa de verdad en el
# centro: hay salas donde una prestación MANDA sobre otra y desplaza a quien
# esté ahí.
#
# Regla real (confirmada con el dueño 2026-08-06): el Box 1 es de Abarca, SALVO
# que ese día atienda ecografía. Cuando David Pardo atiende, él toma el Box 1 y
# Abarca se corre al Box 4; si el 4 está ocupado, al 3.
#
# Se evalúa POR FRANJA, no por día: si Pardo sólo atiende en la mañana, Abarca
# vuelve al Box 1 en la tarde. Sale gratis porque la asignación ya se calcula
# momento a momento.
#
# `alternativas` va en orden de preferencia. Si ninguna tiene cupo, el
# desplazado cae al reparto normal del pool.
REGLAS_SALA = [
    {
        "cuando_atiende": 68,                    # David Pardo — Ecografía
        "toma": "box1",
        "desplaza": {73: ["box4", "box3"]},      # Andrés Abarca — Medicina General
        "motivo": "ecografía tiene prioridad en Box 1",
    },
]


def boxes_config_efectiva(layout_guardado) -> list[dict]:
    """Fusiona la planta que editó el usuario con la semántica del código.

    Ninguna de las dos fuentes basta sola:

    · El layout guardado en bi.boxes_state_global lo escribe el dashboard y trae
      lo que el usuario controla —nombre, piso, orden, tipo, quién puede estar—
      pero NO trae la semántica interna: `revenue_profs` (la partición contable
      que evita el doble conteo de box1/box2), `virtual` ni `uso_autorizado`.
      El layout que hay hoy es del 31-may-2026 y es anterior a todo eso.

    · BOXES_CONFIG tiene la semántica al día, pero ignora que el dueño movió
      salas, las renombró o cambió de piso. Hasta ahora el backend usaba SOLO
      esto: editar la planta no cambiaba ni Revenue ni Eficiencia, así que media
      pantalla mostraba la planta nueva y la otra media la de junio.

    Se toma el código como base y se le aplican encima los campos que el usuario
    edita. Los boxes que el usuario creó y no existen en el código entran igual.
    `revenue_profs` se intersecta con los `default_profs` efectivos: nunca se
    atribuye plata a alguien que ya no está en esa sala.
    """
    # El carril virtual se puebla desde la ficha del profesional, no desde una
    # lista escrita a mano: una sola fuente para el mapa y para lo que el bot le
    # dice al paciente. Va ANTES del atajo de abajo — si no, sin layout guardado
    # el carril salía vacío y esos profesionales quedaban sin sala.
    _tele = profesionales_telemedicina()
    base = {}
    for b in BOXES_CONFIG:
        b = dict(b)
        if b.get("virtual") and _tele:
            b["default_profs"] = list(_tele)
            b["revenue_profs"] = list(_tele)
        base[b["id"]] = b
    if not layout_guardado:
        return [dict(b) for b in base.values()]

    EDITABLES = ("nombre", "piso", "orden", "tipo", "modo", "pool_group", "default_profs")
    salida, vistos = [], set()
    for guardado in layout_guardado:
        bid = guardado.get("id")
        if not bid:
            continue
        vistos.add(bid)
        box = base.get(bid, {}).copy()
        nuevo = not box
        _profs_codigo = list(box.get("default_profs") or [])
        box.update({k: guardado[k] for k in EDITABLES if k in guardado})
        box.setdefault("id", bid)
        box.setdefault("default_profs", [])
        # La planta guardada puede ser vieja y no traer a los profesionales que
        # se agregaron después en el código (la del 31-may no tiene a Celedón en
        # box3). Se suman los del código que falten, conservando el orden del
        # usuario: quitar a alguien de una sala tiene que ser una edición
        # deliberada en el dashboard, no un efecto de tener el layout desfasado.
        for _pid in _profs_codigo:
            if _pid not in box["default_profs"]:
                box["default_profs"].append(_pid)

        # …pero si el CÓDIGO sacó a alguien de esta sala —está en otra sala del
        # código y ya no en ésta— esa remoción sí manda. Sin esto, mover a un
        # profesional en el código no servía de nada: el layout guardado (que es
        # de mayo) lo resucitaba en su sala vieja y quedaba en dos partes.
        # Sólo aplica a quien el código ubica explícitamente en otro lado; a
        # quien el código no menciona, se respeta lo que hayas puesto tú.
        _mudados = {pid for pid in list(box["default_profs"])
                    if pid not in _profs_codigo
                    and any(pid in (o.get("default_profs") or [])
                            for oid, o in base.items() if oid != bid)
                    and any(pid in (o.get("default_profs") or [])
                            for oid, o in base.items() if oid == bid) is False}
        if _mudados:
            box["default_profs"] = [p for p in box["default_profs"] if p not in _mudados]
            log.info("boxes: %s — el código movió a %s fuera de esta sala",
                     bid, sorted(_mudados))
        box.setdefault("modo", "pool")
        box.setdefault("pool_group", None)
        rp = box.get("revenue_profs")
        if rp:
            rp_ok = [x for x in rp if x in box["default_profs"]]
            if len(rp_ok) != len(rp):
                log.warning("boxes: %s tenía revenue_profs fuera de su sala (%s) — se recortan",
                            bid, sorted(set(rp) - set(rp_ok)))
            box["revenue_profs"] = rp_ok
        if nuevo:
            log.info("boxes: %s existe solo en la planta guardada, sin semántica en código", bid)
        salida.append(box)

    # Boxes que el código define y la planta guardada no menciona (p. ej. el
    # carril de telemedicina, creado después del último guardado): se agregan,
    # si no desaparecerían sus citas y su plata.
    for bid, box in base.items():
        if bid not in vistos:
            salida.append(dict(box))
    salida.sort(key=lambda b: (b.get("piso", 9), b.get("orden", 99)))
    return salida


@app.get("/atribucion")
@app.get("/atribucion/dashboard")
def atribucion_dashboard_page(token: str | None = Query(None)):
    """Atribución se rediseñó como pestaña dentro de Autopilot Ads (2026-05-31).
    Mantenemos la ruta como redirect para links/bookmarks viejos."""
    dest = "/autopilot" + (f"?token={token}" if token else "") + "#atribucion"
    return RedirectResponse(url=dest, status_code=307)


@app.get("/demanda", response_class=HTMLResponse)
@app.get("/demanda/dashboard", response_class=HTMLResponse)
def demanda_dashboard_page():
    """Demanda capturada por el bot: qué piden los pacientes que no resolvimos.

    Se lee fresco del disco en cada request (igual que /cmc/mensual) para poder
    iterar el HTML sin reiniciar el servicio.
    """
    p = _TEMPLATE_DIR / "demanda_dashboard.html"
    if not p.exists():
        raise HTTPException(404, "Dashboard Demanda no disponible")
    return p.read_text(encoding="utf-8")


# Apellidos de profesionales del CMC — para separar dentro de los eventos
# `sin_disponibilidad` "pidieron un servicio que no tenía cupo" de "pidieron a
# un doctor puntual" (agenda saturada). Son decisiones distintas.
_PROF_SURNAMES = {
    "olavarría", "olavarria", "abarca", "márquez", "marquez", "borrego",
    "millán", "millan", "barraza", "rejón", "rejon", "quijano", "burgos",
    "jiménez", "jimenez", "castillo", "fredes", "valdés", "valdes",
    "fuentealba", "acosta", "armijo", "etcheverry", "pinto", "montalba",
    "rodríguez", "rodriguez", "arratia", "gómez", "gomez", "guevara", "pardo",
}


@app.get("/api/demanda/data")
def api_demanda_data(dias: int = 90):
    """Señales de demanda capturadas en conversación, para decidir qué ofrecer/promover.

    Fuentes (sessions.db):
    - `sin_disponibilidad`: pidió algo sin cupo → separado en servicio vs profesional
    - `intent_agendar`: intención de agendar por especialidad (volumen de interés)
    - `demanda_no_disponible` (+ eventos): lo que el CMC no ofrece (gap de catálogo)

    Aporta el lado "qué promover" del loop de Alma; /atribucion mide "qué llegó".
    """
    import sys as _sys_dem
    from pathlib import Path as _P_dem
    _sys_dem.path.insert(0, str(_P_dem(__file__).parent))
    from session import _conn as _conn_dem

    dias = max(1, min(int(dias or 90), 365))
    win = f"-{dias} days"
    conn = _conn_dem()
    c = conn.cursor()

    def _is_prof(term: str) -> bool:
        t = (term or "").lower()
        return any(s in t for s in _PROF_SURNAMES)

    # 1) sin_disponibilidad → ranking (lower() para no duplicar por capitalización)
    c.execute("""SELECT lower(json_extract(meta,'$.especialidad')) esp, COUNT(*) n,
                        COUNT(DISTINCT phone) personas, MAX(date(ts)) ultimo
                 FROM conversation_events
                 WHERE event='sin_disponibilidad'
                   AND json_extract(meta,'$.especialidad') IS NOT NULL
                   AND ts >= datetime('now', ?)
                 GROUP BY esp ORDER BY n DESC""", (win,))
    servicios: list[dict] = []
    profesionales: list[dict] = []
    for r in c.fetchall():
        item = {"nombre": r["esp"], "solicitudes": r["n"],
                "personas": r["personas"], "ultimo": r["ultimo"]}
        (profesionales if _is_prof(r["esp"]) else servicios).append(item)

    # 2) intent_agendar → interés por especialidad (volumen de demanda).
    #    Excluimos intents sin especialidad: son ruido para "qué promover".
    c.execute("""SELECT lower(json_extract(meta,'$.especialidad')) esp,
                        COUNT(DISTINCT phone) personas, COUNT(*) n
                 FROM conversation_events
                 WHERE event='intent_agendar'
                   AND json_extract(meta,'$.especialidad') IS NOT NULL
                   AND ts >= datetime('now', ?)
                 GROUP BY esp ORDER BY personas DESC""", (win,))
    interes = [{"nombre": r["esp"], "personas": r["personas"], "solicitudes": r["n"]}
               for r in c.fetchall()]

    # 3) demanda_no_disponible → gap de catálogo (lo que NO ofrecemos)
    c.execute("""SELECT lower(solicitud) sol, tipo, COUNT(*) n, MAX(date(created_at)) ultimo
                 FROM demanda_no_disponible
                 WHERE created_at >= datetime('now', ?)
                 GROUP BY sol, tipo ORDER BY n DESC""", (win,))
    no_ofrecemos = [{"nombre": r["sol"], "tipo": r["tipo"], "solicitudes": r["n"], "ultimo": r["ultimo"]}
                    for r in c.fetchall()]
    _seen = {x["nombre"] for x in no_ofrecemos}
    c.execute("""SELECT lower(COALESCE(json_extract(meta,'$.solicitud'),
                                       json_extract(meta,'$.especialidad'))) sol,
                        COUNT(*) n, MAX(date(ts)) ultimo
                 FROM conversation_events
                 WHERE event IN ('demanda_no_disponible','demanda_no_disponible_faq')
                   AND ts >= datetime('now', ?)
                 GROUP BY sol ORDER BY n DESC""", (win,))
    for r in c.fetchall():
        if r["sol"] and r["sol"] not in _seen:
            no_ofrecemos.append({"nombre": r["sol"], "tipo": "consulta",
                                 "solicitudes": r["n"], "ultimo": r["ultimo"]})
    no_ofrecemos.sort(key=lambda x: x["solicitudes"], reverse=True)

    conn.close()

    return {
        "dias": dias,
        "kpis": {
            "servicios_sin_cupo": sum(s["solicitudes"] for s in servicios),
            "profesionales_pedidos": sum(p["solicitudes"] for p in profesionales),
            "intencion_total": sum(i["personas"] for i in interes),
            "gaps_catalogo": len(no_ofrecemos),
        },
        "servicios_sin_cupo": servicios,
        "profesionales_sin_cupo": profesionales,
        "interes_por_especialidad": interes,
        "no_ofrecemos": no_ofrecemos,
    }


@app.get("/arquitectura", response_class=HTMLResponse)
@app.get("/saas", response_class=HTMLResponse)
def arquitectura_saas_page():
    """Esquema estilo n8n de la plataforma CMC, segmentado por packs SaaS (Básico/Avanzado/Pro). Material de presentación."""
    if not _ARQUITECTURA_SAAS_HTML:
        raise HTTPException(404, "Esquema de arquitectura no disponible")
    return _ARQUITECTURA_SAAS_HTML


@app.get("/portada", response_class=HTMLResponse)
@app.get("/inicio", response_class=HTMLResponse)
def portada_olacore_page():
    """Portada/cover de la presentación OLACORE Tech con la fachada del CMC. Lleva a /arquitectura."""
    if not _PORTADA_OLACORE_HTML:
        raise HTTPException(404, "Portada no disponible")
    return _PORTADA_OLACORE_HTML


@app.get("/winback", response_class=HTMLResponse)
@app.get("/winback/dashboard", response_class=HTMLResponse)
def winback_dashboard_page(token: str | None = Query(None)):
    """Dashboard winback — pool, funnel consent, KPIs campana."""
    if token != ADMIN_TOKEN:
        raise HTTPException(401, "No autorizado")
    if not _WINBACK_DASHBOARD_HTML:
        raise HTTPException(404, "Dashboard Winback no disponible")
    return _WINBACK_DASHBOARD_HTML


@app.get("/winback-dental", response_class=HTMLResponse)
@app.get("/winback-dental/dashboard", response_class=HTMLResponse)
def winback_dental_dashboard_page(token: str | None = Query(None)):
    """Dashboard winback dental — pool por sub-cohorte, funnel consent, KPIs campana dental."""
    if token != ADMIN_TOKEN:
        raise HTTPException(401, "No autorizado")
    if not _WINBACK_DENTAL_DASHBOARD_HTML:
        raise HTTPException(404, "Dashboard Winback Dental no disponible")
    return _WINBACK_DENTAL_DASHBOARD_HTML


@app.get("/admin/api/winback-dental-status")
def api_winback_dental_status(token: str | None = Query(None)):
    """KPIs winback dental en tiempo real desde BI Postgres."""
    if token != ADMIN_TOKEN:
        raise HTTPException(401, "No autorizado")

    from datetime import datetime
    from zoneinfo import ZoneInfo
    import os as _oswd

    now_cl = datetime.now(ZoneInfo("America/Santiago"))

    flags = {
        "DENTAL_CONSENT_BLAST_ACTIVE": _oswd.getenv("DENTAL_CONSENT_BLAST_ACTIVE", "false"),
        "DENTAL_WINBACK_ACTIVE": _oswd.getenv("DENTAL_WINBACK_ACTIVE", "false"),
    }

    kpis: dict = {}
    try:
        from winback import bi_conn as _bi_conn_wd

        with _bi_conn_wd() as conn:
            cur = conn.cursor()

            # Pool total por sub-cohorte
            cur.execute(
                "SELECT subcohorte, COUNT(*) FROM bi.v_dental_cohortes_contactables "
                "GROUP BY subcohorte ORDER BY subcohorte"
            )
            kpis["pool_por_subcohorte"] = {r[0]: r[1] for r in cur.fetchall()}

            # Consent dental: totales por estado
            cur.execute(
                "SELECT status, COUNT(*) FROM bi.dental_consent GROUP BY status"
            )
            kpis["consent_por_estado"] = {r[0]: r[1] for r in cur.fetchall()}

            # Consent blast hoy
            cur.execute(
                "SELECT COUNT(*) FROM bi.dental_consent "
                "WHERE DATE(consent_sent_at AT TIME ZONE 'America/Santiago') = CURRENT_DATE"
            )
            kpis["consent_enviados_hoy"] = cur.fetchone()[0]

            # Winback enviados hoy
            cur.execute(
                "SELECT COUNT(*) FROM bi.dental_winback_envios "
                "WHERE DATE(enviado_at) = CURRENT_DATE"
            )
            kpis["winback_enviados_hoy"] = cur.fetchone()[0]

            # Winback total + respuestas últimos 30 días
            cur.execute(
                "SELECT COUNT(*) FROM bi.dental_winback_envios "
                "WHERE enviado_at > NOW() - INTERVAL '30 days'"
            )
            kpis["winback_30d"] = cur.fetchone()[0]

            cur.execute(
                "SELECT response_type, COUNT(*) FROM bi.dental_winback_envios "
                "WHERE enviado_at > NOW() - INTERVAL '30 days' AND response_type IS NOT NULL "
                "GROUP BY response_type"
            )
            kpis["respuestas_30d"] = {r[0]: r[1] for r in cur.fetchall()}

            # Opt-outs dental total
            cur.execute("SELECT COUNT(*) FROM bi.dental_opt_outs")
            kpis["opt_outs_total"] = cur.fetchone()[0]

            # Últimos 7 días de envíos (para sparkline)
            cur.execute(
                "SELECT DATE(enviado_at) AS dia, COUNT(*) "
                "FROM bi.dental_winback_envios "
                "WHERE enviado_at > NOW() - INTERVAL '7 days' "
                "GROUP BY dia ORDER BY dia"
            )
            kpis["ultimos_7_dias"] = [{"dia": str(r[0]), "enviados": r[1]} for r in cur.fetchall()]

            cur.close()

    except Exception as _e:
        import logging as _lg
        _lg.getLogger("winback_dental_dashboard").warning("winback-dental-status BI error: %s", _e)
        kpis = {"error": str(_e)}

    return {
        "now_cl": now_cl.strftime("%Y-%m-%d %H:%M"),
        "flags": flags,
        "kpis": kpis,
    }


@app.get("/admin/api/winback-status")
def api_winback_status(token: str | None = Query(None)):
    """KPIs winback en tiempo real desde BI Postgres."""
    if token != ADMIN_TOKEN:
        raise HTTPException(401, "No autorizado")

    import os as _osw
    import psycopg2
    from datetime import datetime, timedelta
    import zoneinfo as _zi

    tz_cl = _zi.ZoneInfo("America/Santiago")
    now_cl = datetime.now(tz_cl)

    # Flags desde env
    flags = {
        "MARKETING_CONSENT_BLAST_ACTIVE": _osw.getenv("MARKETING_CONSENT_BLAST_ACTIVE", "false").lower() in ("true", "1", "yes"),
        "WINBACK_ACTIVE":                 _osw.getenv("WINBACK_ACTIVE", "false").lower() in ("true", "1", "yes"),
        "CROSS_SELL_ACTIVE":              _osw.getenv("CROSS_SELL_ACTIVE", "false").lower() in ("true", "1", "yes"),
    }

    # Conexion BI
    bi_host = _osw.getenv("BI_DB_HOST", "127.0.0.1")
    bi_port = int(_osw.getenv("BI_DB_PORT", "5432"))
    bi_name = _osw.getenv("BI_DB_NAME", "health_bi")
    bi_user = _osw.getenv("BI_DB_USER", "health_user")
    bi_pass = _osw.getenv("BI_DB_PASSWORD", "password123")

    kpis: dict = {}
    funnel: list = []
    ultimos_dias: list = []
    errores_meta_24h: dict = {"131042": 0, "132000": 0, "otros_4xx": 0}

    # Leer errores Meta de las últimas 5000 líneas del log
    try:
        from jobs import _tail_lines as _tl_winback
        _log_tail_wb = _tl_winback()
        for _line in _log_tail_wb.splitlines():
            if "131042" in _line and ("MSG FAILED" in _line or "error_code" in _line):
                errores_meta_24h["131042"] += 1
            elif "132000" in _line and ("MSG FAILED" in _line or "error_code" in _line):
                errores_meta_24h["132000"] += 1
            elif ("MSG FAILED" in _line or "error_code" in _line) and any(
                c in _line for c in ("131", "132", "133", "100", "200")
            ):
                errores_meta_24h["otros_4xx"] += 1
    except Exception:
        pass

    try:
        conn = psycopg2.connect(
            host=bi_host, port=bi_port, dbname=bi_name,
            user=bi_user, password=bi_pass, connect_timeout=5
        )
        cur = conn.cursor()

        # Pool total y pendiente
        try:
            cur.execute("SELECT COUNT(*) FROM bi.v_winback_cohortes_contactables")
            pool_total = cur.fetchone()[0]
        except Exception:
            pool_total = 0

        # Consent stats
        try:
            cur.execute("""
                SELECT
                    COUNT(*) FILTER (WHERE status = 'pending')   AS pendiente,
                    COUNT(*) FILTER (WHERE status = 'accepted')  AS accepted,
                    COUNT(*) FILTER (WHERE status = 'declined')  AS declined,
                    COUNT(*)                                       AS total_enviados
                FROM bi.marketing_consent
            """)
            row = cur.fetchone()
            consent_pending, consent_accepted, consent_declined, consent_enviados = (row or (0, 0, 0, 0))
        except Exception:
            consent_pending = consent_accepted = consent_declined = consent_enviados = 0

        acceptance_rate = round(
            (consent_accepted / (consent_accepted + consent_declined) * 100)
            if (consent_accepted + consent_declined) > 0 else 0.0, 1
        )

        # Pool pendiente = total - ya tiene consent aceptado
        try:
            cur.execute("SELECT COUNT(*) FROM bi.marketing_consent WHERE status = 'accepted'")
            ya_aceptados = cur.fetchone()[0]
        except Exception:
            ya_aceptados = 0
        pool_pendiente = max(0, pool_total - ya_aceptados)

        # Winbacks enviados
        try:
            cur.execute("SELECT COUNT(*) FROM bi.winback_envios")
            winbacks_enviados = cur.fetchone()[0]
        except Exception:
            winbacks_enviados = 0

        # Citas atribuidas y revenue (tabla winback_envios con campos opcionales)
        try:
            cur.execute("""
                SELECT
                    COUNT(*) FILTER (WHERE cita_id IS NOT NULL OR cita_atribuida_id IS NOT NULL) AS citas,
                    COALESCE(SUM(value_clp) FILTER (WHERE cita_id IS NOT NULL OR cita_atribuida_id IS NOT NULL), 0) AS revenue
                FROM bi.winback_envios
            """)
            row2 = cur.fetchone()
            citas_atribuidas = row2[0] if row2 else 0
            revenue_atribuido = int(row2[1]) if row2 else 0
        except Exception:
            citas_atribuidas = 0
            revenue_atribuido = 0

        # Costo Meta acumulado (si existe tabla)
        try:
            cur.execute("SELECT COALESCE(SUM(spend_clp), 0) FROM bi.meta_spend_winback")
            costo_meta = int(cur.fetchone()[0])
        except Exception:
            costo_meta = 0

        kpis = {
            "pool_total":              pool_total,
            "pool_pendiente":          pool_pendiente,
            "consent_enviados":        consent_enviados,
            "consent_accepted":        consent_accepted,
            "consent_declined":        consent_declined,
            "consent_pending":         consent_pending,
            "acceptance_rate_pct":     acceptance_rate,
            "winbacks_enviados":       winbacks_enviados,
            "citas_atribuidas":        citas_atribuidas,
            "revenue_atribuido_clp":   revenue_atribuido,
            "costo_meta_acumulado_clp": costo_meta,
        }

        funnel = [
            {"step": "Pool total",      "n": pool_total},
            {"step": "Consent enviado", "n": consent_enviados},
            {"step": "Respondieron",    "n": consent_accepted + consent_declined},
            {"step": "Aceptaron",       "n": consent_accepted},
            {"step": "Winback enviado", "n": winbacks_enviados},
            {"step": "Cita creada",     "n": citas_atribuidas},
        ]

        # Ultimos 7 dias
        try:
            cur.execute("""
                SELECT
                    d::date AS fecha,
                    COALESCE(c.consent, 0) AS consent,
                    COALESCE(c.accepted, 0) AS accepted,
                    COALESCE(w.winbacks, 0) AS winbacks,
                    COALESCE(w.citas, 0) AS citas
                FROM generate_series(
                    CURRENT_DATE - INTERVAL '13 days',
                    CURRENT_DATE,
                    '1 day'::interval
                ) AS d
                LEFT JOIN (
                    SELECT
                        DATE(consent_sent_at) AS fecha,
                        COUNT(*) AS consent,
                        COUNT(*) FILTER (WHERE status = 'accepted') AS accepted
                    FROM bi.marketing_consent
                    GROUP BY 1
                ) c ON c.fecha = d::date
                LEFT JOIN (
                    SELECT DATE(enviado_at) AS fecha, COUNT(*) AS winbacks,
                           COUNT(*) FILTER (WHERE cita_id IS NOT NULL OR cita_atribuida_id IS NOT NULL) AS citas
                    FROM bi.winback_envios
                    GROUP BY 1
                ) w ON w.fecha = d::date
                ORDER BY d::date DESC
            """)
            rows = cur.fetchall()
            ultimos_dias = [
                {"fecha": str(r[0]), "consent": r[1], "accepted": r[2], "winbacks": r[3], "citas": r[4]}
                for r in rows
            ]
        except Exception:
            ultimos_dias = []

        cur.close()
        conn.close()

    except Exception as _e:
        import logging as _lg
        _lg.getLogger("winback_dashboard").warning("winback-status BI error: %s", _e)
        kpis = {"error": str(_e)}

    return {
        "now_cl": now_cl.strftime("%Y-%m-%d %H:%M"),
        "flags": flags,
        "kpis": kpis,
        "funnel": funnel,
        "errores_meta_24h": errores_meta_24h,
        "ultimos_dias": ultimos_dias,
    }


@app.get("/boxes", response_class=HTMLResponse)
@app.get("/boxes/dashboard", response_class=HTMLResponse)
def boxes_dashboard_page(token: str | None = Query(None)):
    """Gemelo digital de boxes CMC — tiempo casi-real (datos desde BI)."""
    from admin_routes import _is_admin_token
    if not (token and _is_admin_token(token)):
        raise HTTPException(401, "No autorizado")
    if not _BOXES_DASHBOARD_HTML:
        raise HTTPException(404, "Dashboard Boxes no disponible")
    return _BOXES_DASHBOARD_HTML


@app.get("/kintu", response_class=HTMLResponse)
@app.get("/kintu/dashboard", response_class=HTMLResponse)
def kintu_shell(token: str | None = Query(None),
                cmc_session: str | None = Cookie(None)):
    """Kintu — shell del producto Growth (espejo de /alma con brand Kintu). Embebe los
    módulos reales de growth (/autopilot, /seo) + Resumen (Kintu Global) + Marcas + Canales.
    Cada iframe recibe su token correcto: autopilot=token dueño, seo=ADMIN_TOKEN."""
    from admin_routes import _verify_cookie, _is_admin_token
    if not _KINTU_HTML:
        raise HTTPException(404, "Kintu no disponible")
    active = token if (token and _is_admin_token(token)) else None
    if not active and _verify_cookie(cmc_session or "") == "admin":
        active = token if (token and _is_admin_token(token)) else OLACORE_TOKEN
    if not active:
        raise HTTPException(403, "no autorizado")
    return HTMLResponse(_KINTU_HTML
                        .replace("__TOKEN__", active)
                        .replace("__SEO_TOKEN__", ADMIN_TOKEN)
                        .replace("__ADKUN_TOKEN__", ""))


@app.get("/alma", response_class=HTMLResponse)
@app.get("/alma/dashboard", response_class=HTMLResponse)
def alma_shell(token: str | None = Query(None),
               cmc_session: str | None = Cookie(None)):
    """Alma — plataforma interna unificada. Embebe Panel Recepción v2 y Boxes
    en una sola página con navegación lateral. Misma auth que /admin.

    Los módulos embebidos (en especial /boxes) sólo aceptan el token por query,
    por eso, cuando la sesión entra por cookie, inyectamos el ADMIN_TOKEN real
    para que los iframes carguen. El token queda en el DOM de los iframes — es
    el mismo modelo que ya usa /boxes hoy.

    El perfil ALMA_PROFILES resuelve la variante (3ª línea del lockup) según
    el token activo. La 2ª línea "CARAMPANGUE" es fija en alma.html.
    Si variante es "" o None, la 3ª línea no se renderiza.
    """
    from admin_routes import _verify_cookie, _is_admin_token
    if not _ALMA_HTML:
        raise HTTPException(404, "Alma no disponible")

    def _render(active_token: str) -> str:
        import json as _json_alma
        profile = ALMA_PROFILES.get(active_token, {})
        variante = profile.get("variante", "") or ""
        variante_line = (
            f'<div class="alma-variante">{variante}</div>'
            if variante else ""
        )
        # Construir lista de módulos visibles para este perfil.
        # modulos=None → todos los del registry (acceso total).
        allowed_keys = profile.get("modulos")
        if allowed_keys is None:
            modules_list = [dict(id=k, **v) for k, v in ALMA_MODULE_REGISTRY.items()]
        else:
            modules_list = [
                dict(id=k, **ALMA_MODULE_REGISTRY[k])
                for k in allowed_keys
                if k in ALMA_MODULE_REGISTRY
            ]
        # Módulos gateados por feature flag: no mostrar módulos inertes.
        if os.getenv("CHECKIN_ENABLED", "false").lower() != "true":
            modules_list = [m for m in modules_list if m["id"] != "sala"]
        profile_modules_json = _json_alma.dumps(modules_list, ensure_ascii=False)
        panel_profesional = "true" if profile.get("panel_profesional", True) else "false"
        return (_ALMA_HTML
                .replace("__TOKEN__", active_token)
                .replace("__ALMA_VARIANTE_LINE__", variante_line)
                .replace("__PROFILE_MODULES__", profile_modules_json)
                .replace("__PANEL_PROFESIONAL__", panel_profesional))

    from alma_scope import page_token as _alma_page_token
    eff = _alma_page_token(token, cmc_session, None)  # shell: solo exige login válido
    if eff:
        return _render(eff)
    return RedirectResponse(url="/admin/login", status_code=302)


@app.get("/agendar", response_class=HTMLResponse)
def agendador_publico_page(request: Request, preview: str | None = Query(None)):
    """Agendador público premium. Gateado: 404 salvo que esté prendido o haya
    ?preview=ADMIN_TOKEN. La página inyecta el preview para que la API lo herede."""
    import config as _cfg
    enabled = _cfg.AGENDADOR_PUBLICO_ENABLED
    is_preview = bool(preview) and preview == ADMIN_TOKEN
    if not (enabled or is_preview):
        raise HTTPException(404, "No encontrado")
    if not _AGENDADOR_HTML:
        raise HTTPException(404, "Agendador no disponible")
    # __PREVIEW__ → token (en preview) o "" (en producción pública)
    html = _AGENDADOR_HTML.replace("__PREVIEW__", preview if is_preview else "")
    # no-store: en preview/iteración el navegador siempre trae la última versión
    return HTMLResponse(html, headers={"Cache-Control": "no-store, max-age=0"})


@app.get("/agendar/v2", response_class=HTMLResponse)
def agendador_v2_page(request: Request, preview: str | None = Query(None)):
    """Agendador v2 (rediseño con evidencia de conversión, 2026-07-14).
    Misma API que /agendar, pero llave propia (AGENDADOR_V2_ENABLED) para que
    el dueño decida cuándo exponerlo. OJO: la API exige además que
    AGENDADOR_PUBLICO_ENABLED esté prendido (o preview) — como ya lo está en
    prod, encender la v2 es solo agregar su flag."""
    import config as _cfg
    enabled = _cfg.AGENDADOR_V2_ENABLED and _cfg.AGENDADOR_PUBLICO_ENABLED
    is_preview = bool(preview) and preview == ADMIN_TOKEN
    if not (enabled or is_preview):
        raise HTTPException(404, "No encontrado")
    if not _AGENDADOR_V2_HTML:
        raise HTTPException(404, "Agendador no disponible")
    html = _AGENDADOR_V2_HTML.replace("__PREVIEW__", preview if is_preview else "")
    return HTMLResponse(html, headers={"Cache-Control": "no-store, max-age=0"})


@app.get("/agendar/portal", response_class=HTMLResponse)
def agendador_portal_page(request: Request, preview: str | None = Query(None)):
    """Agendador compacto para el Portal del Paciente: una pantalla, sin pasos.
    Mismo gate y misma API que /agendar (las salvaguardas viven en la API)."""
    import config as _cfg
    enabled = _cfg.AGENDADOR_PUBLICO_ENABLED
    is_preview = bool(preview) and preview == ADMIN_TOKEN
    if not (enabled or is_preview):
        raise HTTPException(404, "No encontrado")
    if not _AGENDADOR_PORTAL_HTML:
        raise HTTPException(404, "Agendador no disponible")
    return HTMLResponse(_AGENDADOR_PORTAL_HTML,
                        headers={"Cache-Control": "no-store, max-age=0"})


@app.get("/alma/agenda", response_class=HTMLResponse)
def alma_agenda_page(token: str | None = Query(None),
                     cmc_session: str | None = Cookie(None)):
    """Modulo nativo Agenda — ver citas del dia y agendar desde Alma."""
    from alma_scope import page_token as _alma_page_token
    if not _ALMA_AGENDA_HTML:
        raise HTTPException(404, "Agenda no disponible")
    eff = _alma_page_token(token, cmc_session, "agenda")
    if eff:
        return _ALMA_AGENDA_HTML.replace("__TOKEN__", eff)
    return RedirectResponse(url="/admin/login", status_code=302)


@app.get("/alma/pagos", response_class=HTMLResponse)
def alma_pagos_page(token: str | None = Query(None),
                    cmc_session: str | None = Cookie(None)):
    """Modulo nativo Pagos OLACORE — registro completo con Caja/Cierre del dia."""
    from admin_routes import _verify_cookie, _is_admin_token
    if not _ALMA_PAGOS_HTML:
        raise HTTPException(404, "Pagos no disponible")
    if token and _is_admin_token(token):
        return _ALMA_PAGOS_HTML.replace("__TOKEN__", token)
    if cmc_session:
        role = _verify_cookie(cmc_session)
        if role in ("admin", "ortodoncia"):
            return _ALMA_PAGOS_HTML.replace("__TOKEN__", ADMIN_TOKEN)
    return RedirectResponse(url="/admin/login", status_code=302)


@app.get("/alma/pagos-simple", response_class=HTMLResponse)
def alma_pagos_simple_page(token: str | None = Query(None),
                            cmc_session: str | None = Cookie(None)):
    """Modulo Pagos simple — version liviana sin Caja/Cierre. Misma tabla pagos_cmc."""
    from admin_routes import _verify_cookie, _is_admin_token
    from alma_scope import page_token as _pt, is_readonly as _ro
    if not _ALMA_PAGOS_SIMPLE_HTML:
        raise HTTPException(404, "Pagos simple no disponible")
    def _ren_pagos(tok: str, readonly: bool) -> str:
        return (_ALMA_PAGOS_SIMPLE_HTML
                .replace("__TOKEN__", tok)
                .replace("__PAGOS_READONLY__", "true" if readonly else "false"))
    if token and _is_admin_token(token):
        return _ren_pagos(token, False)
    # Perfil de profesional por token explícito → prioridad sobre la cookie admin del navegador
    if token:
        eff = _pt(token, None, "pagos")
        if eff:
            return _ren_pagos(eff, _ro(eff))  # SOLO LECTURA, filtrado a su especialidad
    if cmc_session:
        role = _verify_cookie(cmc_session)
        if role in ("admin", "ortodoncia"):
            return _ren_pagos(ADMIN_TOKEN, False)
    return RedirectResponse(url="/admin/login", status_code=302)


@app.get("/alma/abonos", response_class=HTMLResponse)
def alma_abonos_page(token: str | None = Query(None),
                     cmc_session: str | None = Cookie(None)):
    """Modulo Abonos — pagos anticipados (psiquiatria, fono, nutricion)."""
    from admin_routes import _verify_cookie, _is_admin_token
    from alma_scope import page_token as _pt, is_readonly as _ro
    if not _ALMA_ABONOS_HTML:
        raise HTTPException(404, "Abonos no disponible")
    def _ren_abonos(tok: str, readonly: bool) -> str:
        return (_ALMA_ABONOS_HTML
                .replace("__TOKEN__", tok)
                .replace("__ABONOS_READONLY__", "true" if readonly else "false"))
    if token and _is_admin_token(token):
        return _ren_abonos(token, False)
    # Perfil de profesional por token explícito → prioridad sobre la cookie admin del navegador
    if token:
        eff = _pt(token, None, "abonos")
        if eff:
            return _ren_abonos(eff, _ro(eff))  # SOLO LECTURA, filtrado a su especialidad
    if cmc_session:
        role = _verify_cookie(cmc_session)
        if role in ("admin", "ortodoncia"):
            return _ren_abonos(ADMIN_TOKEN, False)
    return RedirectResponse(url="/admin/login", status_code=302)


@app.get("/alma/caja-diaria", response_class=HTMLResponse)
def alma_caja_diaria_page(token: str | None = Query(None),
                          cmc_session: str | None = Cookie(None)):
    """Módulo Caja Diaria — libro de caja (efectivo del día + depósitos al banco).
    Caja global del centro: solo dueño/recepción (no scopeado por profesional)."""
    from admin_routes import _verify_cookie, _is_admin_token
    if not _ALMA_CAJA_DIARIA_HTML:
        raise HTTPException(404, "Caja Diaria no disponible")
    if token and _is_admin_token(token):
        return _ALMA_CAJA_DIARIA_HTML.replace("__TOKEN__", token)
    if cmc_session:
        role = _verify_cookie(cmc_session)
        if role in ("admin", "ortodoncia"):
            return _ALMA_CAJA_DIARIA_HTML.replace("__TOKEN__", ADMIN_TOKEN)
    return RedirectResponse(url="/admin/login", status_code=302)


@app.get("/checkin", response_class=HTMLResponse)
def checkin_page():
    """Página pública de check-in (paciente escanea QR → pone RUT). Gateada por
    CHECKIN_ENABLED en los endpoints; la página se sirve siempre."""
    import os as _os
    if not _CHECKIN_HTML:
        raise HTTPException(404, "Check-in no disponible")
    if _os.getenv("CHECKIN_ENABLED", "false").lower() != "true":
        raise HTTPException(404, "Check-in no disponible")
    return _CHECKIN_HTML


@app.get("/alma/sala", response_class=HTMLResponse)
def alma_sala_page(token: str | None = Query(None),
                   cmc_session: str | None = Cookie(None)):
    """Panel 'En sala' — pacientes que hicieron check-in. Recepción/profesional."""
    from admin_routes import _verify_cookie, _is_admin_token
    if not _ALMA_SALA_HTML:
        raise HTTPException(404, "Sala no disponible")
    if token and _is_admin_token(token):
        return _ALMA_SALA_HTML.replace("__TOKEN__", token)
    if cmc_session and _verify_cookie(cmc_session) in ("admin", "ortodoncia"):
        return _ALMA_SALA_HTML.replace("__TOKEN__", ADMIN_TOKEN)
    return RedirectResponse(url="/admin/login", status_code=302)


@app.get("/alma/caja-diaria-simple", response_class=HTMLResponse)
def alma_caja_diaria_simple_page(token: str | None = Query(None),
                                 cmc_session: str | None = Cookie(None)):
    """Caja Diaria recepción — SOLO el efectivo que hay en el cajón ahora + registrar
    depósito. El libro completo (depósitos, saldo, historia) vive en Caja Diaria OLACORE."""
    from admin_routes import _verify_cookie, _is_admin_token
    if not _ALMA_CAJA_DIARIA_SIMPLE_HTML:
        raise HTTPException(404, "Caja Diaria no disponible")
    if token and _is_admin_token(token):
        return _ALMA_CAJA_DIARIA_SIMPLE_HTML.replace("__TOKEN__", token)
    if cmc_session:
        role = _verify_cookie(cmc_session)
        if role in ("admin", "ortodoncia"):
            return _ALMA_CAJA_DIARIA_SIMPLE_HTML.replace("__TOKEN__", ADMIN_TOKEN)
    return RedirectResponse(url="/admin/login", status_code=302)


@app.get("/alma/pagos-medilink", response_class=HTMLResponse)
def alma_pagos_medilink_page(token: str | None = Query(None),
                             cmc_session: str | None = Cookie(None)):
    """Módulo Pagos Medilink — espejo SOLO LECTURA de la caja real (bi_pagos_caja),
    misma fuente que DB Mensual. Dueño/recepción ve todo; perfil de profesional, lo suyo."""
    from admin_routes import _verify_cookie, _is_admin_token
    from alma_scope import page_token as _pt
    if not _ALMA_PAGOS_MEDILINK_HTML:
        raise HTTPException(404, "Pagos Medilink no disponible")
    def _ren_pm(tok: str, is_dueno: bool) -> str:
        return (_ALMA_PAGOS_MEDILINK_HTML
                .replace("__TOKEN__", tok)
                .replace("__IS_DUENO__", "true" if is_dueno else "false"))
    if token and _is_admin_token(token):
        return _ren_pm(token, True)
    # Perfil de profesional por token explícito → prioridad sobre la cookie admin del navegador
    if token:
        eff = _pt(token, None, "pagos_medilink")
        if eff:
            return _ren_pm(eff, False)  # profesional: sin informe (vista acotada)
    if cmc_session:
        role = _verify_cookie(cmc_session)
        if role in ("admin", "ortodoncia"):
            return _ren_pm(ADMIN_TOKEN, True)
    return RedirectResponse(url="/admin/login", status_code=302)


@app.get("/alma/envios", response_class=HTMLResponse)
def alma_envios_page(token: str | None = Query(None),
                     cmc_session: str | None = Cookie(None)):
    """Modulo Envios/Campanas — auditoria de templates e imagenes enviadas."""
    from admin_routes import _verify_cookie, _is_admin_token
    if not _ALMA_ENVIOS_HTML:
        raise HTTPException(404, "Envios no disponible")
    if token and _is_admin_token(token):
        return _ALMA_ENVIOS_HTML.replace("__TOKEN__", token)
    if cmc_session:
        role = _verify_cookie(cmc_session)
        if role in ("admin", "ortodoncia"):
            return _ALMA_ENVIOS_HTML.replace("__TOKEN__", ADMIN_TOKEN)
    return RedirectResponse(url="/admin/login", status_code=302)


@app.get("/alma/conciliacion", response_class=HTMLResponse)
def alma_conciliacion_page(token: str | None = Query(None),
                           cmc_session: str | None = Cookie(None)):
    """Modulo nativo Conciliacion — cruce financiero multi-fuente con capa Imed."""
    from admin_routes import _verify_cookie, _is_admin_token
    if not _ALMA_CONCILIACION_HTML:
        raise HTTPException(404, "Conciliacion no disponible")
    if token and _is_admin_token(token):
        return _ALMA_CONCILIACION_HTML.replace("__TOKEN__", token)
    if cmc_session:
        role = _verify_cookie(cmc_session)
        if role in ("admin", "ortodoncia"):
            return _ALMA_CONCILIACION_HTML.replace("__TOKEN__", ADMIN_TOKEN)
    return RedirectResponse(url="/admin/login", status_code=302)


@app.get("/alma/inventario", response_class=HTMLResponse)
def alma_inventario_page(token: str | None = Query(None),
                         cmc_session: str | None = Cookie(None)):
    """Modulo nativo Inventario Dental — catalogo MayorDent + stock + orden de compra."""
    from admin_routes import _verify_cookie, _is_admin_token
    if not _ALMA_INVENTARIO_HTML:
        raise HTTPException(404, "Inventario no disponible")
    if token and _is_admin_token(token):
        return _ALMA_INVENTARIO_HTML.replace("__TOKEN__", token)
    if cmc_session:
        role = _verify_cookie(cmc_session)
        if role in ("admin", "ortodoncia"):
            return _ALMA_INVENTARIO_HTML.replace("__TOKEN__", ADMIN_TOKEN)
    return RedirectResponse(url="/admin/login", status_code=302)


@app.get("/alma/recepcion-kanban", response_class=HTMLResponse)
def alma_recepcion_kanban_page(token: str | None = Query(None),
                               v: str | None = Query(None),
                               cmc_session: str | None = Cookie(None)):
    """Cola de Recepción (v2) — a quién le toca responder ahora y por qué.

    Sirve la v2 por defecto. `?v=1` devuelve el tablero original, que se conserva
    para poder comparar: la v2 pasa de 220 tarjetas a ~34 aplicando política de
    salida y clases de servicio (ver app/recepcion_kanban_v2.py).
    """
    from admin_routes import _verify_cookie, _is_admin_token
    # v3 por defecto; ?v=1 y ?v=2 conservan las anteriores para comparar.
    _HTML = (_ALMA_RECEPCION_KANBAN_HTML if v == "1"
             else _ALMA_RECEPCION_KANBAN_V2_HTML if v == "2"
             else (_ALMA_RECEPCION_KANBAN_V3_HTML or _ALMA_RECEPCION_KANBAN_V2_HTML
                   or _ALMA_RECEPCION_KANBAN_HTML))
    if not _HTML:
        raise HTTPException(404, "Recepción Kanban no disponible")
    if token and _is_admin_token(token):
        return _HTML.replace("__TOKEN__", token)
    if cmc_session:
        role = _verify_cookie(cmc_session)
        if role in ("admin", "ortodoncia"):
            return _HTML.replace("__TOKEN__", ADMIN_TOKEN)
    return RedirectResponse(url="/admin/login", status_code=302)


@app.get("/alma/ortodoncia", response_class=HTMLResponse)
def alma_ortodoncia_page(token: str | None = Query(None),
                         cmc_session: str | None = Cookie(None)):
    """Modulo Profesional: Seguimiento Ortodoncia — controles vencidos, avance, plan de pago."""
    from admin_routes import _verify_cookie, _is_admin_token
    if not _ALMA_ORTODONCIA_HTML:
        raise HTTPException(404, "Ortodoncia no disponible")
    if token and _is_admin_token(token):
        return _ALMA_ORTODONCIA_HTML.replace("__TOKEN__", token)
    if cmc_session:
        role = _verify_cookie(cmc_session)
        if role in ("admin", "ortodoncia"):
            return _ALMA_ORTODONCIA_HTML.replace("__TOKEN__", ADMIN_TOKEN)
    return RedirectResponse(url="/admin/login", status_code=302)


@app.get("/alma/kine", response_class=HTMLResponse)
def alma_kine_page(token: str | None = Query(None),
                   cmc_session: str | None = Cookie(None)):
    """Modulo Profesional: Programa Kinesiología — adherencia, riesgo de abandono, plan de sesiones."""
    from alma_scope import (page_token as _alma_page_token, shows_money as _shows_money,
                            profesional_id_of as _prof_id_of)
    if not _ALMA_KINE_HTML:
        raise HTTPException(404, "Kine no disponible")
    eff = _alma_page_token(token, cmc_session, "kine")
    if eff:
        financiero = "true" if _shows_money(eff) else "false"
        # Si es perfil de profesional, deep-link a su panel (Tu calendario).
        pid = _prof_id_of(eff)
        dash_url = f"/profesional/dashboard?token={_make_prof_token(pid)}" if pid else ""
        return (_ALMA_KINE_HTML
                .replace("__TOKEN__", eff)
                .replace("__KINE_FINANCIERO__", financiero)
                .replace("__PROF_DASHBOARD_URL__", dash_url))
    return RedirectResponse(url="/admin/login", status_code=302)


@app.get("/alma/programas", response_class=HTMLResponse)
def alma_programas_page(token: str | None = Query(None),
                        cmc_session: str | None = Cookie(None)):
    """Modulo Profesional: Motor de Programas Clínicos por especialidad (adherencia + control/recall)."""
    from alma_scope import page_token as _alma_page_token, profesional_id_of as _prof_id_of, ver_ingreso_of as _ver_ing
    if not _ALMA_PROGRAMAS_HTML:
        raise HTTPException(404, "Programas no disponible")
    eff = _alma_page_token(token, cmc_session, "programas")
    if eff:
        pid = _prof_id_of(eff)
        scoped = "true" if pid else "false"  # perfil de profesional → vista acotada
        ver_ingreso = "true" if _ver_ing(eff) else "false"  # flag por perfil: ve su ingreso
        dash_url = f"/profesional/dashboard?token={_make_prof_token(pid)}" if pid else ""
        return (_ALMA_PROGRAMAS_HTML
                .replace("__TOKEN__", eff)
                .replace("__PROG_SCOPED__", scoped)
                .replace("__PROG_VER_INGRESO__", ver_ingreso)
                .replace("__PROF_DASHBOARD_URL__", dash_url))
    return RedirectResponse(url="/admin/login", status_code=302)


def _build_alma_accesos() -> list[dict]:
    """Lista de accesos (persona → token + link de entrada a Alma).

    Fuente canónica: ALMA_PROFILES en vivo (token→perfil) + algunos tokens extra
    de config. Los VALORES de token se leen del entorno en tiempo de request —
    NUNCA se hardcodean en el repo (la rotación los sacó de git a propósito)."""
    import config as _cfg
    base = os.getenv("ALMA_PUBLIC_BASE", "https://agentecmc.cl").rstrip("/")
    out: list[dict] = []
    for tk, prof in ALMA_PROFILES.items():
        if not tk:
            continue
        variante = (prof.get("variante") or "").strip()
        mods = prof.get("modulos")
        if mods is None:
            grupo, rol = "Dueño", "Acceso total"
            nombre = variante or "Adkun · Dueño"
            mod_label = "Todos los módulos"
        elif tk == ADMIN_TOKEN:
            grupo, rol = "Recepción", "Recepción"
            nombre = variante or "Recepción"
            mod_label = f"{len(mods)} módulos"
        else:
            grupo, rol = "Profesionales", "Profesional"
            nombre = variante or "Profesional"
            mod_label = f"{len(mods)} módulos"
        out.append({
            "grupo": grupo, "rol": rol, "nombre": nombre,
            "token": tk, "url": f"{base}/alma?token={tk}", "modulos": mod_label,
        })
    # Accesos extra: links reales de entrada que no son perfiles de Alma.
    extra = [
        (OLACORE_HOLDING_TOKEN, "Documentos holding",
         "Estructura tributaria / Holding (compartible con contador)",
         f"{base}/olacore/holding?token={OLACORE_HOLDING_TOKEN}"),
    ]
    if getattr(_cfg, "ORTODONCIA_TOKEN", ""):
        extra.append((_cfg.ORTODONCIA_TOKEN, "Ortodoncia",
                      "Módulo Ortodoncia (panel admin)",
                      f"{base}/admin?token={_cfg.ORTODONCIA_TOKEN}"))
    if getattr(_cfg, "MARKETING_TOKEN", ""):
        extra.append((_cfg.MARKETING_TOKEN, "Marketing",
                      "Estudio de Marketing (recepción)",
                      f"{base}/marketing?token={_cfg.MARKETING_TOKEN}"))
    for tk, rol, nombre, url in extra:
        if tk:
            out.append({"grupo": "Otros accesos", "rol": rol,
                        "nombre": nombre, "token": tk, "url": url, "modulos": "—"})
    return out


@app.get("/alma/dashboards", response_class=HTMLResponse)
def alma_dashboards_page(token: str | None = Query(None),
                         cmc_session: str | None = Cookie(None)):
    """Módulo Accesos — todos los tokens y links de entrada a Alma, por persona.

    SOLO el dueño (OLACORE_TOKEN): lista incluye el token de recepción y el suyo,
    así que es el módulo más sensible. 404 (no redirect) para no revelar que
    existe a quien no lo porta. Los valores de token se inyectan en vivo."""
    import hmac as _hm, json as _json_acc
    is_owner = bool(token) and bool(OLACORE_TOKEN) and _hm.compare_digest(token, OLACORE_TOKEN)
    if not is_owner:
        raise HTTPException(404, "No encontrado")
    if not _ALMA_DASHBOARDS_HTML:
        raise HTTPException(404, "Accesos no disponible")
    accesos = _build_alma_accesos()
    html = (_ALMA_DASHBOARDS_HTML
            .replace("__TOKEN__", token)
            .replace("__ACCESOS_JSON__", _json_acc.dumps(accesos, ensure_ascii=False)))
    return HTMLResponse(html, headers={"Cache-Control": "no-store, max-age=0"})


def _olacore_holding_ok(token: str | None) -> bool:
    """Acceso a los documentos del holding: dueño (OLACORE_TOKEN) o el token
    dedicado de solo-documentos (compartible con la familia/contador sin dar
    acceso al resto de Alma)."""
    import hmac as _hm
    return bool(token) and (_hm.compare_digest(token, OLACORE_TOKEN)
                            or _hm.compare_digest(token, OLACORE_HOLDING_TOKEN))


@app.get("/olacore/estructura", response_class=HTMLResponse)
def olacore_estructura_page(token: str | None = Query(None)):
    """Documento de estudio: estructura tributaria legal + roadmap (gateado)."""
    if not _olacore_holding_ok(token):
        raise HTTPException(401, "No autorizado")
    if not _OLACORE_ESTRUCTURA_HTML:
        raise HTTPException(404, "Documento no disponible")
    return HTMLResponse(_OLACORE_ESTRUCTURA_HTML, headers={"Cache-Control": "no-store"})


@app.get("/olacore/holding", response_class=HTMLResponse)
def olacore_holding_page(token: str | None = Query(None)):
    """Dashboard financiero del holding OLACORE (gateado)."""
    if not _olacore_holding_ok(token):
        raise HTTPException(401, "No autorizado")
    if not _OLACORE_HOLDING_HTML:
        raise HTTPException(404, "Dashboard no disponible")
    return HTMLResponse(_OLACORE_HOLDING_HTML, headers={"Cache-Control": "no-store"})


@app.get("/olacore/reunion", response_class=HTMLResponse)
def olacore_reunion_page(token: str | None = Query(None)):
    """Hoja de 1 página para la reunión con el contador (gateado)."""
    if not _olacore_holding_ok(token):
        raise HTTPException(401, "No autorizado")
    if not _OLACORE_REUNION_HTML:
        raise HTTPException(404, "Documento no disponible")
    return HTMLResponse(_OLACORE_REUNION_HTML, headers={"Cache-Control": "no-store"})


@app.get("/olacore", response_class=HTMLResponse)
@app.get("/olacore/", response_class=HTMLResponse)
def olacore_portal_page(token: str | None = Query(None)):
    """Portal del holding: enlaza los documentos + diccionario (gateado).
    Inyecta el token en los links para que el visitante navegue sin re-tipearlo."""
    if not _olacore_holding_ok(token):
        raise HTTPException(401, "No autorizado")
    if not _OLACORE_PORTAL_HTML:
        raise HTTPException(404, "Portal no disponible")
    return HTMLResponse(_OLACORE_PORTAL_HTML.replace("__TOKEN__", token or ""),
                        headers={"Cache-Control": "no-store"})


@app.get("/anima", include_in_schema=False)
@app.get("/anima/dashboard", include_in_schema=False)
def anima_redirect(token: str | None = Query(None)):
    """Redirect 301 de /anima → /alma (ruta canónica de la plataforma)."""
    target = f"/alma?token={token}" if token else "/alma"
    return RedirectResponse(url=target, status_code=301)


@app.get("/admin/api/boxes-config")
def api_boxes_config_get(token: str | None = Query(None)):
    """Devuelve la configuración persistente de boxes (layout, pisos, overrides, schedules)."""
    # Mismo criterio que /admin/api/boxes-state: cualquier token Alma válido
    # entra. La diferencia entre recepción y dueño NO es el acceso, es
    # `boxes_financiero` (ver ALMA_PROFILES) — el dueño ve los montos y
    # recepción no. Cuando estos endpoints exigían ADMIN_TOKEN exacto, entrar
    # con el token de dueño daba 401 en boxes-config: el dashboard se lo tragaba
    # en el catch de fetchConfig() y quedaba sin layout guardado NI escenarios,
    # sin ningún error visible.
    from admin_routes import _is_admin_token
    if not (token and _is_admin_token(token)):
        raise HTTPException(401, "No autorizado")
    pool = _bi_pool()
    conn = None
    try:
        try:
            conn = pool.getconn()
        except Exception as _pe:
            raise HTTPException(503, "BI pool ocupado, reintenta en unos segundos")
        with conn.cursor() as cur:
            _boxes_escenarios_ensure(cur)
            conn.commit()
            cur.execute("SELECT layout, pisos, manual_overrides, schedules, weekly_template, "
                        "escenarios, updated_at FROM bi.boxes_state_global WHERE id=1")
            row = cur.fetchone()
            if not row:
                return {"layout": [], "pisos": [], "manual_overrides": {}, "schedules": {},
                        "weekly_template": {}, "escenarios": {}, "updated_at": None}
            layout, pisos, overrides, schedules, weekly, escenarios, updated = row
            return {
                "layout": layout or [],
                "pisos": pisos or [],
                "manual_overrides": overrides or {},
                "schedules": schedules or {},
                "weekly_template": weekly or {},
                "escenarios": escenarios or {},
                "updated_at": updated.isoformat() if updated else None,
            }
    except HTTPException:
        raise
    except Exception:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        raise
    finally:
        if conn is not None:
            pool.putconn(conn)


def _boxes_escenarios_ensure(cur) -> None:
    """Columna de escenarios de simulación. Idempotente.

    Los escenarios viven en la MISMA fila que el resto de la config de boxes
    (`bi.boxes_state_global`) en vez de en una tabla propia: se leen y se
    escriben siempre junto con la planta, y el PUT ya hace merge por clave, así
    que guardar un escenario no puede pisar el layout ni el patrón semanal.
    """
    cur.execute("ALTER TABLE bi.boxes_state_global "
                "ADD COLUMN IF NOT EXISTS escenarios jsonb DEFAULT '{}'::jsonb")


def _boxes_log_ensure(cur) -> None:
    """Bitácora de asignaciones REALES de sala. Idempotente.

    El override manual del dashboard vive en `manual_overrides` (jsonb) indexado
    POR BOX, así que cada asignación pisa la anterior y `getManualOverrides` sólo
    devuelve las de HOY: el trabajo de recepción se borra cada noche.

    Acá se guarda cada asignación como un HECHO con su fecha. Eso convierte el
    arreglo diario —"hoy Ana está en Kinesiología 2"— en evidencia acumulada de
    cómo se usa el centro de verdad, que es lo que después permite fijar el
    patrón semanal sobre datos y no sobre memoria.
    """
    cur.execute("""
        CREATE TABLE IF NOT EXISTS bi.boxes_asignacion_log (
            id          SERIAL PRIMARY KEY,
            fecha       DATE        NOT NULL,
            dow         SMALLINT    NOT NULL,   -- 0=lunes … 6=domingo
            box_id      TEXT        NOT NULL,
            prof_id     INTEGER,
            prof_nombre TEXT,
            origen      TEXT        NOT NULL DEFAULT 'manual',
            set_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_bal_fecha ON bi.boxes_asignacion_log (fecha)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_bal_box_dow ON bi.boxes_asignacion_log (box_id, dow)")


@app.post("/admin/api/boxes-asignacion")
async def api_boxes_asignacion_log(request: Request, token: str | None = Query(None)):
    """Registra que alguien asignó un profesional a una sala. No borra nada."""
    # Mismo criterio que /admin/api/boxes-state: cualquier token Alma válido
    # entra. La diferencia entre recepción y dueño NO es el acceso, es
    # `boxes_financiero` (ver ALMA_PROFILES) — el dueño ve los montos y
    # recepción no. Cuando estos endpoints exigían ADMIN_TOKEN exacto, entrar
    # con el token de dueño daba 401 en boxes-config: el dashboard se lo tragaba
    # en el catch de fetchConfig() y quedaba sin layout guardado NI escenarios,
    # sin ningún error visible.
    from admin_routes import _is_admin_token
    if not (token and _is_admin_token(token)):
        raise HTTPException(401, "No autorizado")
    body = await request.json()
    box_id = (body.get("box_id") or "").strip()
    if not box_id:
        raise HTTPException(400, "falta box_id")
    # `datetime` NO está importado a nivel de módulo en main.py — el resto de las
    # funciones lo importan localmente. Sin esto: NameError en cada registro.
    from datetime import date as _d, datetime as _dt
    import zoneinfo as _zi
    hoy = _dt.now(_zi.ZoneInfo("America/Santiago")).date()
    try:
        f = _d.fromisoformat(body["fecha"]) if body.get("fecha") else hoy
    except Exception:
        f = hoy
    pool = _bi_pool()
    conn = None
    try:
        conn = pool.getconn()
        with conn.cursor() as cur:
            _boxes_log_ensure(cur)
            cur.execute("""INSERT INTO bi.boxes_asignacion_log
                           (fecha, dow, box_id, prof_id, prof_nombre, origen)
                           VALUES (%s,%s,%s,%s,%s,%s)""",
                        (f, f.weekday(), box_id,
                         body.get("prof_id"), (body.get("prof_nombre") or "")[:120],
                         (body.get("origen") or "manual")[:20]))
        conn.commit()
        return {"ok": True}
    except Exception as e:
        if conn is not None:
            try: conn.rollback()
            except Exception: pass
        log.warning("boxes-asignacion: no se pudo registrar (%s)", e)
        # Nunca romper la interacción de recepción por un fallo de bitácora.
        return {"ok": False, "error": str(e)[:120]}
    finally:
        if conn is not None:
            pool.putconn(conn)


@app.get("/admin/api/boxes-patrones")
def api_boxes_patrones(token: str | None = Query(None), dias: int = Query(60)):
    """Qué patrón viene marcando recepción, agregado por sala y día de la semana.

    Responde la pregunta que hoy nadie puede responder: "Ana quedó en
    Kinesiología 2 los miércoles ¿cuántas veces?". Con eso se decide qué fijar.
    """
    # Mismo criterio que /admin/api/boxes-state: cualquier token Alma válido
    # entra. La diferencia entre recepción y dueño NO es el acceso, es
    # `boxes_financiero` (ver ALMA_PROFILES) — el dueño ve los montos y
    # recepción no. Cuando estos endpoints exigían ADMIN_TOKEN exacto, entrar
    # con el token de dueño daba 401 en boxes-config: el dashboard se lo tragaba
    # en el catch de fetchConfig() y quedaba sin layout guardado NI escenarios,
    # sin ningún error visible.
    from admin_routes import _is_admin_token
    if not (token and _is_admin_token(token)):
        raise HTTPException(401, "No autorizado")
    pool = _bi_pool()
    conn = None
    try:
        conn = pool.getconn()
        with conn.cursor() as cur:
            _boxes_log_ensure(cur)
            cur.execute("""
                SELECT box_id, dow, prof_id, MAX(prof_nombre) AS prof,
                       COUNT(*) AS veces, MAX(fecha) AS ultima,
                       COUNT(DISTINCT fecha) AS dias_distintos
                  FROM bi.boxes_asignacion_log
                 WHERE fecha >= CURRENT_DATE - %s::int
                   AND prof_id IS NOT NULL
                 GROUP BY box_id, dow, prof_id
                 ORDER BY box_id, dow, veces DESC
            """, (dias,))
            filas = [{"box_id": r[0], "dow": r[1], "prof_id": r[2], "profesional": r[3],
                      "veces": r[4], "ultima": r[5].isoformat() if r[5] else None,
                      "dias_distintos": r[6]} for r in cur.fetchall()]
        DOW = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
        for f in filas:
            f["dia"] = DOW[f["dow"]] if 0 <= f["dow"] < 7 else "?"
            # Se sugiere fijar cuando el mismo profesional aparece en la misma
            # sala el mismo día de la semana 3 veces o más: deja de ser excepción.
            f["sugerir_fijar"] = f["dias_distintos"] >= 3
        return {"dias": dias, "patrones": filas,
                "sugeridos": sum(1 for f in filas if f["sugerir_fijar"])}
    except Exception as e:
        raise HTTPException(500, f"No se pudo leer la bitácora: {str(e)[:120]}")
    finally:
        if conn is not None:
            pool.putconn(conn)


@app.post("/admin/api/boxes-simular")
async def api_boxes_simular(request: Request, token: str | None = Query(None)):
    """Simula mover profesionales de sala SIN tocar nada.

    Responde la pregunta que hoy se decide a ojo: "quiero mover a Ana de
    Kinesiología 2 a Box 3 — ¿cabe? ¿se topa con alguien?". Toma las citas
    reales de una ventana de días, reasigna según el escenario y cuenta los
    choques: dos profesionales con horarios superpuestos en la misma sala.

    Body: {"mover": [{"prof_id": 80, "a_box": "box3"}], "dias": 30}
    No escribe: es solo lectura sobre bi.fact_citas.
    """
    # Mismo criterio que /admin/api/boxes-state: cualquier token Alma válido
    # entra. La diferencia entre recepción y dueño NO es el acceso, es
    # `boxes_financiero` (ver ALMA_PROFILES) — el dueño ve los montos y
    # recepción no. Cuando estos endpoints exigían ADMIN_TOKEN exacto, entrar
    # con el token de dueño daba 401 en boxes-config: el dashboard se lo tragaba
    # en el catch de fetchConfig() y quedaba sin layout guardado NI escenarios,
    # sin ningún error visible.
    from admin_routes import _is_admin_token
    if not (token and _is_admin_token(token)):
        raise HTTPException(401, "No autorizado")
    body = await request.json()
    mover = {int(m["prof_id"]): m["a_box"] for m in (body.get("mover") or []) if m.get("prof_id")}
    dias = max(1, min(int(body.get("dias") or 30), 120))
    if not mover:
        raise HTTPException(400, "falta 'mover'")

    # IMPOSIBLE es sólo lo que la sala rechaza por sí misma: la dental tiene
    # sillón y nadie más la ocupa. Ahí el conteo de choques mentiría.
    #
    # Que un profesional esté hoy restringido a ciertas salas (`CAUTIVOS`) es un
    # supuesto de configuración, NO una ley física — y la gracia de simular es
    # preguntar "¿y si lo levantamos?". La primera versión de este guard también
    # bloqueaba eso y convertía al simulador en un validador de lo que ya
    # existe: pedir mover a la masoterapeuta fuera de kine devolvía "imposible"
    # en vez de responder si cabía. Ahora se simula igual y se advierte.
    _imposibles = [{"prof_id": pid, "a_box": bx,
                    "motivo": "la sala dental sólo admite odontología"}
                   for pid, bx in mover.items()
                   if bx in SALAS_EXCLUYENTES and not sala_acepta(bx, pid)]
    if _imposibles:
        return {"ok": False, "imposibles": _imposibles,
                "mensaje": "El traslado no es posible; no se simuló."}

    _advertencias = [
        {"prof_id": pid, "a_box": bx, "restringido_a": salas_permitidas(pid),
         "motivo": (CAUTIVOS.get(pid) or {}).get("motivo", ""),
         "nota": "hoy está restringido a esas salas; la simulación asume que se levanta"}
        for pid, bx in mover.items()
        if (salas_permitidas(pid) or []) and bx not in (salas_permitidas(pid) or [])
    ]

    try:
        _cfg = api_boxes_config_get(token=token) or {}
        BOXES = boxes_config_efectiva(_cfg.get("layout") or [])
    except Exception:
        BOXES = [dict(b) for b in BOXES_CONFIG]
    por_id = {b["id"]: b for b in BOXES}
    for destino in set(mover.values()):
        if destino not in por_id:
            raise HTTPException(400, f"la sala '{destino}' no existe")

    # Réplica de la asignación REAL, no una aproximación. La versión anterior
    # tomaba el primer box cuyo default_profs contuviera al profesional, así que
    # amontonaba a todos los generales en Box 1 y contaba cada solapamiento como
    # choque — cuando el centro permite DOS profesionales por sala a propósito.
    # El número absoluto salía inflado (838 en 30 días) y no significaba nada.
    #
    # Ahora se reparte igual que en vivo: cautivos a sus salas, luego el pool con
    # tope de dos, y sólo se cuenta choque cuando ya no queda cupo.
    CUPO_POR_SALA = 2
    por_grupo: dict = {}
    for b in BOXES:
        if b.get("modo") == "pool" and not b.get("virtual"):
            por_grupo.setdefault(b.get("pool_group"), []).append(b["id"])

    _virtuales = {pid for b in BOXES if b.get("virtual")
                  for pid in (b.get("default_profs") or [])}

    def _salas_candidatas(pid: int, escenario: bool) -> list:
        if escenario and pid in mover:
            return [mover[pid]]
        if pid in _virtuales:
            return []          # telemedicina: no necesita sala
        perm = salas_permitidas(pid)
        if perm:
            return [x for x in perm if x in por_id]
        for b in BOXES:
            if pid in (b.get("default_profs") or []):
                if b.get("virtual"):
                    return []          # telemedicina no disputa metros cuadrados
                if b.get("modo") == "fijo":
                    return [b["id"]]
                return por_grupo.get(b.get("pool_group"), [b["id"]])
        return []

    pool = _bi_pool()
    conn = None
    try:
        conn = pool.getconn()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT fecha, profesional_id, hora_inicio, hora_fin
                  FROM bi.fact_citas
                 WHERE fecha >= CURRENT_DATE - %s::int AND fecha <= CURRENT_DATE
                   AND hora_inicio IS NOT NULL
            """, (dias,))
            citas = cur.fetchall()
    except Exception as e:
        raise HTTPException(500, f"No se pudieron leer las citas: {str(e)[:120]}")
    finally:
        if conn is not None:
            pool.putconn(conn)

    def _choques(escenario: bool):
        """Reparte las citas como en vivo y cuenta las que se quedan SIN sala."""
        total, detalle = 0, []
        # Por día: se recorren las citas en orden de inicio y se ocupa la primera
        # sala candidata con cupo en esa franja. Un choque es una cita que no
        # encontró dónde ir — no dos personas compartiendo sala, que es legítimo.
        por_dia: dict = {}
        for f, pid, hi, hf in citas:
            por_dia.setdefault(f, []).append((pid, hi, hf))
        for f, items in por_dia.items():
            items.sort(key=lambda x: x[1])
            ocupacion: dict = {}      # sala → [(inicio, fin, prof)]
            for pid, hi, hf in items:
                fin = hf or hi
                colocado = False
                for sala in _salas_candidatas(pid, escenario):
                    activos = [x for x in ocupacion.get(sala, []) if x[1] > hi]
                    # el mismo profesional en su sala no consume un cupo extra
                    if any(x[2] == pid for x in activos) or len(activos) < CUPO_POR_SALA:
                        ocupacion.setdefault(sala, []).append((hi, fin, pid))
                        colocado = True
                        break
                # Telemedicina no compite por sala: no encontrar box no es un
                # choque, es que no lo necesita. Antes inflaba el conteo.
                if not colocado and pid in _virtuales:
                    continue
                if not colocado:
                    total += 1
                    if len(detalle) < 25:
                        detalle.append({"fecha": f.isoformat(), "prof": pid,
                                        "hora": str(hi)[:5],
                                        "salas_intentadas": _salas_candidatas(pid, escenario)})
        return total, detalle

    hoy_n, hoy_det = _choques(False)
    sim_n, sim_det = _choques(True)
    # Los choques que YA existen hoy no son culpa del traslado. Se restan para
    # que "agrega N" signifique de verdad N nuevos, y se devuelven aparte: la
    # primera corrida mostró 3 topones preexistentes en Box 1/Box 2 que se
    # leían como si los causara el movimiento simulado.
    _ya = {(d["fecha"], d["prof"], d["hora"]) for d in hoy_det}
    _nuevos = [d for d in sim_det if (d["fecha"], d["prof"], d["hora"]) not in _ya]
    _nom = {}
    try:
        from medilink import PROFESIONALES as _PF
        _nom = {k: v.get("nombre", str(k)) for k, v in _PF.items()}
    except Exception:
        pass
    for d in (_nuevos + hoy_det):
        d["profesional"] = _nom.get(d.get("prof"), str(d.get("prof")))
    return {
        "ok": True,
        "dias": dias,
        "mover": [{"prof_id": k, "a_box": v, "profesional": _nom.get(k, str(k)),
                   "salas_actuales": _salas_candidatas(k, False)} for k, v in mover.items()],
        "advertencias": _advertencias,
        "choques_hoy": hoy_n,
        "choques_simulado": sim_n,
        "diferencia": sim_n - hoy_n,
        "veredicto": ("cabe sin topones" if sim_n <= hoy_n else
                      f"agrega {sim_n - hoy_n} choque(s) respecto de hoy"),
        "choques_nuevos": _nuevos,
        "choques_preexistentes": hoy_det[:25],
        "ejemplos": sim_det,
    }


@app.post("/admin/api/simular-horas")
async def api_simular_horas(request: Request, token: str | None = Query(None)):
    """¿Cuánto cambia el ingreso si muevo horas entre profesionales?

    La otra mitad de /admin/api/boxes-simular: ese responde "¿cabe en las
    salas?", éste responde "¿cuánto me cuesta?". Sólo lectura.

    Body: {"cambios":[{"prof_id":73,"horas_dia":8,"ocupacion":0.65,"comision":0.2}],
           "origen_id":1, "dias":90, "sensibilidad":true}
    """
    from admin_routes import _is_admin_token
    if not (token and _is_admin_token(token)):
        raise HTTPException(401, "No autorizado")
    # Es plata: quien no ve montos en Boxes tampoco puede verlos por acá.
    if not ALMA_PROFILES.get(token, {}).get("boxes_financiero", True):
        raise HTTPException(403, "Este módulo muestra montos")

    body = await request.json()
    cambios = [c for c in (body.get("cambios") or []) if c.get("prof_id")]
    if not cambios:
        raise HTTPException(400, "falta 'cambios'")
    origen_id = body.get("origen_id")
    dias = max(30, min(int(body.get("dias") or 90), 365))

    from simulador_horas import medir, simular, sensibilidad
    ids = [int(c["prof_id"]) for c in cambios]
    if origen_id:
        ids.append(int(origen_id))
    try:
        base = medir(sorted(set(ids)), dias=dias)
    except Exception as e:
        raise HTTPException(500, f"No se pudo medir la agenda: {str(e)[:140]}")

    res = simular(base, cambios, int(origen_id) if origen_id else None)
    res["base"] = base
    res["dias_medidos"] = dias
    if body.get("sensibilidad") and len(cambios) == 1:
        c0 = cambios[0]
        res["sensibilidad"] = sensibilidad(
            base, int(c0["prof_id"]),
            float(c0.get("horas_dia") or base[int(c0["prof_id"])]["horas_dia"]),
            origen_id=int(origen_id) if origen_id else None)
    return res


@app.put("/admin/api/boxes-config")
async def api_boxes_config_put(request: Request, token: str | None = Query(None)):
    """Guarda la configuración persistente de boxes."""
    # Mismo criterio que /admin/api/boxes-state: cualquier token Alma válido
    # entra. La diferencia entre recepción y dueño NO es el acceso, es
    # `boxes_financiero` (ver ALMA_PROFILES) — el dueño ve los montos y
    # recepción no. Cuando estos endpoints exigían ADMIN_TOKEN exacto, entrar
    # con el token de dueño daba 401 en boxes-config: el dashboard se lo tragaba
    # en el catch de fetchConfig() y quedaba sin layout guardado NI escenarios,
    # sin ningún error visible.
    from admin_routes import _is_admin_token
    if not (token and _is_admin_token(token)):
        raise HTTPException(401, "No autorizado")
    body = await request.json()
    # MERGE por clave, no reemplazo total. Antes cualquier cliente que mandara un
    # body parcial borraba lo que no incluía: el dashboard nunca enviaba
    # `weekly_template`, así que CADA guardado de layout/override/horario dejaba
    # la columna del patrón semanal en {}. Ahora una clave ausente NO se toca;
    # para borrarla hay que mandarla explícitamente vacía.
    _actual = {}
    try:
        _actual = api_boxes_config_get(token=token) or {}
    except Exception as _e_merge:
        # Si no se pudo leer lo guardado, es preferible NO borrar nada: se
        # escribe solo lo que vino en el body y el resto queda como esté.
        log.warning("boxes-config: no se pudo leer para merge (%s)", _e_merge)

    def _campo(nombre, vacio):
        return body[nombre] if nombre in body else (_actual.get(nombre) or vacio)

    layout = _campo("layout", [])
    pisos = _campo("pisos", [])
    overrides = _campo("manual_overrides", {})
    schedules = _campo("schedules", {})
    weekly = _campo("weekly_template", {})
    escenarios = _campo("escenarios", {})
    import json as _js
    pool = _bi_pool()
    conn = None
    try:
        try:
            conn = pool.getconn()
        except Exception as _pe:
            raise HTTPException(503, "BI pool ocupado, reintenta en unos segundos")
        with conn.cursor() as cur:
            _boxes_escenarios_ensure(cur)
            cur.execute("""
                INSERT INTO bi.boxes_state_global (id, layout, pisos, manual_overrides, schedules,
                                                   weekly_template, escenarios, updated_at)
                VALUES (1, %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb, NOW())
                ON CONFLICT (id) DO UPDATE SET
                  layout = EXCLUDED.layout,
                  pisos = EXCLUDED.pisos,
                  manual_overrides = EXCLUDED.manual_overrides,
                  schedules = EXCLUDED.schedules,
                  weekly_template = EXCLUDED.weekly_template,
                  escenarios = EXCLUDED.escenarios,
                  updated_at = NOW()
            """, (_js.dumps(layout), _js.dumps(pisos), _js.dumps(overrides), _js.dumps(schedules),
                  _js.dumps(weekly), _js.dumps(escenarios)))
            conn.commit()
        return {"ok": True}
    except HTTPException:
        raise
    except Exception:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        raise
    finally:
        if conn is not None:
            pool.putconn(conn)


@app.get("/boxes/manifest.webmanifest", include_in_schema=False)
def boxes_manifest(token: str | None = Query(None)):
    """PWA manifest para instalación como app. Usa los mismos íconos que /admin/v2."""
    if token != ADMIN_TOKEN:
        raise HTTPException(401, "No autorizado")
    return JSONResponse({
        "name": "Boxes CMC",
        "short_name": "Boxes CMC",
        "description": "Gemelo digital de boxes del Centro Médico Carampangue",
        "start_url": f"/boxes?token={token}",  # token ya validado == ADMIN_TOKEN; no emitir el literal del config
        "scope": "/boxes",
        "display": "standalone",
        "orientation": "any",
        "background_color": "#FFFFFF",
        "theme_color": "#1172AB",
        "lang": "es-CL",
        "dir": "ltr",
        "categories": ["medical", "productivity", "business"],
        "icons": [
            {"src": "/static/pwa/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": "/static/pwa/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
            {"src": "/static/pwa/icon-192-maskable.png", "sizes": "192x192", "type": "image/png", "purpose": "maskable"},
            {"src": "/static/pwa/icon-512-maskable.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        ],
    }, media_type="application/manifest+json")


@app.get("/boxes/sw.js", include_in_schema=False)
def boxes_service_worker():
    """Service worker mínimo para PWA + offline cache de assets."""
    sw = """
const CACHE = 'boxes-cmc-v1';
const ASSETS = ['/static/isotipo.png', 'https://cdn.tailwindcss.com', 'https://cdn.jsdelivr.net/npm/sortablejs@1.15.2/Sortable.min.js'];
self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(ASSETS.map(u => new Request(u, {mode:'no-cors'})))));
  self.skipWaiting();
});
self.addEventListener('activate', e => { e.waitUntil(self.clients.claim()); });
self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  // No cachear endpoints API (siempre live)
  if (url.pathname.startsWith('/admin/api/')) return;
  e.respondWith(
    caches.match(e.request).then(r => r || fetch(e.request).then(resp => {
      // Cachear assets estáticos
      if (e.request.method === 'GET' && (url.pathname.startsWith('/static/') || url.host !== location.host)) {
        const clone = resp.clone();
        caches.open(CACHE).then(c => c.put(e.request, clone));
      }
      return resp;
    }).catch(() => caches.match('/boxes')))
  );
});
"""
    from fastapi.responses import Response
    return Response(content=sw, media_type="application/javascript")


@app.get("/alma/manifest.webmanifest", include_in_schema=False)
@app.get("/anima/manifest.webmanifest", include_in_schema=False)  # alias legacy (PWAs instaladas) — borrar tras ventana de gracia
def alma_manifest(token: str | None = Query(None)):
    """PWA manifest de Alma para instalación como app. Identidad CMC."""
    start = f"/alma?token={token}" if token else "/alma"
    return JSONResponse({
        "name": "Alma — Centro Médico Carampangue",
        "short_name": "Alma",
        "description": "Plataforma interna unificada del Centro Médico Carampangue",
        "start_url": start,
        "scope": "/",
        "display": "standalone",
        "orientation": "any",
        "background_color": "#0F3F68",
        "theme_color": "#0F3F68",
        "lang": "es-CL",
        "dir": "ltr",
        "categories": ["medical", "productivity", "business"],
        "icons": [
            {"src": "/static/pwa/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": "/static/pwa/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
            {"src": "/static/pwa/icon-192-maskable.png", "sizes": "192x192", "type": "image/png", "purpose": "maskable"},
            {"src": "/static/pwa/icon-512-maskable.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        ],
    }, media_type="application/manifest+json")


@app.get("/alma/sw.js", include_in_schema=False)
@app.get("/anima/sw.js", include_in_schema=False)  # alias legacy (PWAs instaladas) — borrar tras ventana de gracia
def alma_service_worker():
    """Service worker mínimo para habilitar instalación PWA de Alma."""
    sw = (
        "self.addEventListener('install', e => self.skipWaiting());\n"
        "self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));\n"
        "self.addEventListener('fetch', e => {\n"
        "  e.respondWith(fetch(e.request).catch(() => caches.match(e.request)));\n"
        "});\n"
    )
    from fastapi.responses import Response
    return Response(content=sw, media_type="application/javascript")


@app.get("/admin/api/boxes-state")
async def api_boxes_state(token: str | None = Query(None), fecha: str | None = Query(None)):
    """Estado de los boxes CMC para una fecha (default: hoy).
    Si fecha != hoy → modo histórico, sin "estado en curso", solo agregados.

    Diseño de conexiones PG:
    - La conexión BI se toma del pool, se usa solo para queries rápidas, y se DEVUELVE
      al pool ANTES de cualquier llamada a Medilink.
    - Las llamadas a Medilink (potencialmente lentas por backoff 429) ocurren con la
      conexión PG ya liberada, evitando acumulación de conexiones abiertas bajo carga.
    - Si el path Medilink requiere datos de BI (prof_map), se obtienen en una primera
      apertura corta antes de llamar a Medilink. Las queries pesadas (historial 30d,
      fallback fact_citas, etc.) van en una segunda apertura, también corta.
    """
    from admin_routes import _is_admin_token
    if not (token and _is_admin_token(token)):
        raise HTTPException(401, "No autorizado")
    _boxes_financiero = ALMA_PROFILES.get(token, {}).get("boxes_financiero", True)

    # Planta EFECTIVA: la que editó el dueño, fusionada con la semántica del
    # código. Hasta ahora todo este endpoint calculaba contra BOXES_CONFIG fijo,
    # así que mover o renombrar una sala en el dashboard no cambiaba ni Revenue
    # ni Eficiencia: media pantalla mostraba la planta nueva y la otra la vieja.
    try:
        _cfg_guardada = api_boxes_config_get(token=token) or {}
        BOXES = boxes_config_efectiva(_cfg_guardada.get("layout") or [])
    except Exception as _e_pl:
        log.warning("boxes: no se pudo leer la planta guardada (%s) — se usa la del código", _e_pl)
        BOXES = [dict(b) for b in BOXES_CONFIG]

    from datetime import datetime, timedelta, time as _dtime, date as _date
    import zoneinfo as _zib

    tz = _zib.ZoneInfo("America/Santiago")
    now_cl = datetime.now(tz)
    today_real = now_cl.date()
    if fecha:
        try:
            today = _date.fromisoformat(fecha)
        except Exception:
            today = today_real
    else:
        today = today_real
    is_today = (today == today_real)
    now_t = now_cl.time() if is_today else _dtime(23, 59)  # histórico: ya pasó todo

    def _parse_h(s):
        if not s: return None
        try:
            hh, mm = str(s).split(":")[:2]
            return _dtime(int(hh), int(mm))
        except Exception:
            return None

    pool = _bi_pool()

    # ── FASE 1: obtener prof_map de BI (conexión corta, milisegundos) ─────────
    # Solo se necesita si vamos al path Medilink live (hoy + circuit breaker ok).
    # Para el path histórico/fallback la obtendremos en la Fase 2 junto al resto.
    prof_map: dict = {}
    if is_today and not is_medilink_down():
        conn1 = None
        try:
            try:
                conn1 = pool.getconn()
            except Exception:
                raise HTTPException(503, "BI pool ocupado, reintenta en unos segundos")
            with conn1.cursor() as cur1:
                cur1.execute("""
                    SELECT dp.profesional_id, dp.nombre, COALESCE(de.nombre,'')
                    FROM bi.dim_profesional dp
                    LEFT JOIN bi.dim_especialidad de ON de.especialidad_id = dp.especialidad_id
                """)
                prof_map = {r[0]: (r[1], r[2]) for r in cur1.fetchall()}
        except HTTPException:
            raise
        except Exception:
            if conn1 is not None:
                try:
                    conn1.rollback()
                except Exception:
                    pass
            raise
        finally:
            if conn1 is not None:
                pool.putconn(conn1)  # liberada ANTES de llamar a Medilink
                conn1 = None

    # ── Llamadas a Medilink (fuera del scope PG) ──────────────────────────────
    citas_hoy = []
    medilink_live_ok = False

    if is_today and not is_medilink_down():
        try:
            from medilink import _get_shared_client, _get, _q, MEDILINK_BASE_URL, MEDILINK_SUCURSAL, HEADERS
            client = _get_shared_client()
            params_ml = {
                "id_sucursal": {"eq": MEDILINK_SUCURSAL},
                "fecha": {"eq": today.isoformat()},
                "estado_anulacion": {"eq": 0},
            }
            r = await _get(client, f"{MEDILINK_BASE_URL}/citas",
                           params={"q": _q(params_ml)}, headers=HEADERS)
            if r.status_code == 200:
                for cita in r.json().get("data", []):
                    pid = cita.get("id_profesional")
                    prof_info = prof_map.get(pid, ("—", ""))
                    pac_obj = cita.get("paciente") or {}
                    pac_nombre = " ".join([pac_obj.get("nombre","") or "", pac_obj.get("apellido","") or ""]).strip()
                    citas_hoy.append({
                        "cita_id": cita.get("id"),
                        "profesional_id": pid,
                        "paciente_id": cita.get("id_paciente") or pac_obj.get("id"),
                        "hora_inicio": _parse_h(cita.get("hora") or cita.get("hora_inicio")),
                        "hora_fin": _parse_h(cita.get("hora_fin")),
                        # Medilink devuelve este campo como `estado_cita` (ver
                        # medilink.py:1667, 1804, 1837). Leyendo "estado" siempre
                        # salía None y TODA cita caía al default "agendada": el
                        # gemelo no distinguía quién llegó de quién no, y un
                        # no-show pintaba la sala ocupada.
                        "estado": (cita.get("estado_cita") or cita.get("estado") or "agendada").lower(),
                        # Medilink marca la anulación en un campo aparte del estado.
                        "anulada": str(cita.get("estado_anulacion") or "0") not in ("0", "None", ""),
                        "profesional": prof_info[0],
                        "especialidad": prof_info[1],
                        "paciente": pac_nombre or None,
                    })
                medilink_live_ok = True
        except Exception as _me:
            logging.getLogger("boxes").warning("Medilink live error, fallback a BI: %s", _me)

    # Pagos por paciente (también fuera del scope PG):
    # - Si HOY: Medilink live /pagos
    # - Si HISTÓRICO o Medilink falló: SQLite/BI en Fase 2
    pagos_por_pac: dict = {}
    if is_today and medilink_live_ok:
        try:
            from medilink import _get_shared_client as _gsc_p, _get as _get_p, _q as _q_p, MEDILINK_BASE_URL as _MBU_p, HEADERS as _H_p
            _cli_p = _gsc_p()
            _pp = {"fecha_recepcion": {"eq": today.isoformat()}}
            _rp = await _get_p(_cli_p, f"{_MBU_p}/pagos", params={"q": _q_p(_pp)}, headers=_H_p)
            if _rp.status_code == 200:
                for _pago in _rp.json().get("data", []):
                    _pid = _pago.get("id_paciente") or (_pago.get("paciente") or {}).get("id")
                    _monto = int(_pago.get("monto_pago") or _pago.get("monto") or 0)
                    if _pid:
                        pagos_por_pac[_pid] = pagos_por_pac.get(_pid, 0) + _monto
        except Exception as _pe:
            logging.getLogger("boxes").warning("Medilink /pagos live error: %s", _pe)

    # ── FASE 2: queries BI pesadas (conexión corta, milisegundos) ────────────
    # A este punto Medilink ya respondió (o falló). La conexión PG vive solo mientras
    # ejecutan las queries, no durante el backoff de Medilink.
    conn2 = None
    cur = None
    try:
        try:
            conn2 = pool.getconn()
        except Exception:
            raise HTTPException(503, "BI pool ocupado, reintenta en unos segundos")
        cur = conn2.cursor()

        # Fallback fact_citas: histórico o si Medilink falló
        if not citas_hoy:
            # Si no tenemos prof_map todavía (path histórico), ya viene de fact_citas JOIN
            cur.execute("""
                SELECT fc.cita_id, fc.profesional_id, fc.paciente_id, fc.hora_inicio,
                       fc.hora_fin, fc.estado,
                       COALESCE(dp.nombre, '—') AS profesional,
                       COALESCE(de.nombre, '') AS especialidad,
                       TRIM(CONCAT(COALESCE(pa.nombre,''),' ',COALESCE(pa.apellido,''))) AS paciente
                FROM bi.fact_citas fc
                LEFT JOIN bi.dim_profesional dp ON dp.profesional_id = fc.profesional_id
                LEFT JOIN bi.dim_especialidad de ON de.especialidad_id = fc.especialidad_id
                LEFT JOIN bi.dim_paciente pa ON pa.paciente_id = fc.paciente_id
                WHERE fc.fecha = %s
                  AND fc.estado IN ('agendada', 'atendida', 'confirmada', 'en_curso')
                ORDER BY fc.hora_inicio
            """, (today,))
            for row in cur.fetchall():
                cita_id, prof_id, pac_id, h_ini, h_fin, estado, prof, esp, pac = row
                citas_hoy.append({
                    "cita_id": cita_id, "profesional_id": prof_id, "paciente_id": pac_id,
                    "hora_inicio": _parse_h(h_ini), "hora_fin": _parse_h(h_fin),
                    "estado": estado, "profesional": prof, "especialidad": esp,
                    "paciente": (pac or "").strip() or None,
                })

        # Fallback pagos: SQLite o BI Postgres si Medilink no los trajo
        if not pagos_por_pac:
            try:
                from session import db as _sqc
                with _sqc() as _sq:
                    _rows_pc = _sq.execute(
                        "SELECT id_paciente, SUM(monto) AS monto FROM bi_pagos_caja "
                        "WHERE fecha = ? AND id_paciente IS NOT NULL GROUP BY id_paciente",
                        (today.isoformat(),)
                    ).fetchall()
                    pagos_por_pac = {r["id_paciente"]: int(r["monto"] or 0) for r in _rows_pc}
            except Exception:
                # Último fallback: fact_pagos BI Postgres (ya tenemos la conexión abierta)
                cur.execute("SELECT paciente_id, SUM(monto)::int FROM bi.fact_pagos WHERE fecha = %s GROUP BY paciente_id", (today,))
                pagos_por_pac = {r[0]: r[1] for r in cur.fetchall()}

        # Atribuir revenue: cada cita del día → suma del pago del paciente ese día,
        # repartido si el paciente tuvo múltiples citas (proporcional).
        pac_cita_count = {}
        for c in citas_hoy:
            pac_cita_count[c["paciente_id"]] = pac_cita_count.get(c["paciente_id"], 0) + 1

        # Helper para encontrar a qué box va cada profesional con cita activa.
        # 1) pasada por boxes fijos primero (kine1, kine2, box5) — asignan al prof default.
        # 2) pasada por pools: dental, general, proced, psiconut → ordenan por hora_inicio.
        box_assignments = {b["id"]: {"profesionales_activos": [], "proximo": None,
                                      "citas_hoy_ids": set(), "revenue_dia": 0,
                                      "citas_dia_count": 0} for b in BOXES}

        # Filter active citas: in progress now
        def _is_active(c):
            if not c["hora_inicio"]:
                return False
            if c["hora_fin"] and now_t > c["hora_fin"]:
                return False
            if now_t < c["hora_inicio"]:
                return False
            # Una cita anulada no ocupa la sala aunque su hora esté en curso.
            if c.get("anulada"):
                return False
            return estado_ocupa_sala(c["estado"])

        def _is_proximo(c):
            if not c["hora_inicio"] or now_t >= c["hora_inicio"]:
                return False
            delta = (datetime.combine(today, c["hora_inicio"]) - now_cl.replace(tzinfo=None)).total_seconds() / 60.0
            return 0 < delta <= 60

        activas = [c for c in citas_hoy if _is_active(c)]
        proximas = [c for c in citas_hoy if _is_proximo(c)]

        # Asignación: por cada cita activa, encontrar el box que la admita.
        # Boxes fijos primero (modo='fijo' y prof_id en default_profs)
        citas_asignadas = set()
        for box in BOXES:
            if box["modo"] != "fijo":
                continue
            for c in activas:
                if c["cita_id"] in citas_asignadas:
                    continue
                if c["profesional_id"] in box["default_profs"]:
                    elapsed = int((datetime.combine(today, now_t) - datetime.combine(today, c["hora_inicio"])).total_seconds() / 60)
                    box_assignments[box["id"]]["profesionales_activos"].append({
                        "profesional": c["profesional"],
                        "especialidad": c["especialidad"],
                        "paciente": _initials_pac(c["paciente"]) if c["paciente"] else None,
                        "elapsed_min": max(0, elapsed),
                        "cita_id": c["cita_id"],
                        "paciente_id": c["paciente_id"],
                    })
                    box_assignments[box["id"]]["citas_hoy_ids"].add(c["cita_id"])
                    citas_asignadas.add(c["cita_id"])

        # Pools: por cada cita activa restante, asignar al primer box libre del pool
        pool_boxes = {}  # group → [box_ids in order]
        for box in BOXES:
            if box["modo"] == "pool":
                pool_boxes.setdefault(box["pool_group"], []).append(box["id"])

        # ── PRIORIDAD entre profesionales (REGLAS_SALA) ─────────────────────
        # Antes del reparto normal: si el profesional prioritario está atendiendo
        # AHORA, toma su sala y el desplazado se va a su alternativa. Como esto
        # corre sobre `activas` —las citas en curso en este momento— la regla
        # aplica por franja: si el de ecografía sólo atiende en la mañana, el
        # desplazado vuelve a su sala en la tarde sin que nadie haga nada.
        desplazamientos = []
        _cap = lambda bid: len(box_assignments[bid]["profesionales_activos"]) < 2

        def _colocar(c, bid):
            elapsed = int((datetime.combine(today, now_t) - datetime.combine(today, c["hora_inicio"])).total_seconds() / 60)
            box_assignments[bid]["profesionales_activos"].append({
                "profesional": c["profesional"], "especialidad": c["especialidad"],
                "paciente": _initials_pac(c["paciente"]) if c["paciente"] else None,
                "elapsed_min": max(0, elapsed),
                "cita_id": c["cita_id"], "paciente_id": c["paciente_id"],
            })
            box_assignments[bid]["citas_hoy_ids"].add(c["cita_id"])
            citas_asignadas.add(c["cita_id"])

        for regla in REGLAS_SALA:
            pid_manda = regla["cuando_atiende"]
            destino = regla["toma"]
            if destino not in box_assignments:
                continue
            citas_manda = [c for c in activas
                           if c["profesional_id"] == pid_manda and c["cita_id"] not in citas_asignadas]
            if not citas_manda:
                continue          # no atiende ahora → nadie se mueve

            # 1) el prioritario toma su sala
            for c in citas_manda:
                if _cap(destino):
                    _colocar(c, destino)

            # 2) los desplazados salen de esa sala y van a su alternativa
            for pid_fuera, alternativas in (regla.get("desplaza") or {}).items():
                citas_fuera = [c for c in activas
                               if c["profesional_id"] == pid_fuera and c["cita_id"] not in citas_asignadas]
                _perm_fuera = salas_permitidas(pid_fuera)
                for c in citas_fuera:
                    ubicado = None
                    for alt in alternativas:
                        if _perm_fuera is not None and alt not in _perm_fuera:
                            continue      # no se desplaza a un cautivo fuera de sus salas
                        if alt in box_assignments and _cap(alt):
                            _colocar(c, alt); ubicado = alt; break
                    desplazamientos.append({
                        "profesional": c["profesional"], "desde": destino,
                        "hacia": ubicado, "motivo": regla.get("motivo", ""),
                        # Si ninguna alternativa tenía cupo, cae al reparto normal
                        # más abajo — pero queda registrado que no se pudo ubicar.
                        "sin_cupo": ubicado is None,
                    })
            if desplazamientos:
                log.info("boxes: regla '%s' aplicada — %s", regla.get("motivo"),
                         [f'{d["profesional"]} → {d["hacia"] or "sin cupo"}' for d in desplazamientos])

        for c in activas:
            if c["cita_id"] in citas_asignadas:
                continue
            # Encontrar grupo del profesional
            target_group = None
            for box in BOXES:
                if box["modo"] == "pool" and c["profesional_id"] in box["default_profs"]:
                    target_group = box["pool_group"]
                    break
            if target_group is None:
                continue
            # Un cautivo no entra al reparto libre: sólo puede caer en sus salas.
            _permitidas = salas_permitidas(c["profesional_id"])
            _candidatos = pool_boxes.get(target_group, [])
            if _permitidas is not None:
                _candidatos = [b for b in _candidatos if b in _permitidas] or _permitidas
            # Buscar primer box con cupo (max 2 profesionales simultáneos por box)
            for box_id in _candidatos:
                if box_id not in box_assignments:
                    continue
                if len(box_assignments[box_id]["profesionales_activos"]) < 2:
                    elapsed = int((datetime.combine(today, now_t) - datetime.combine(today, c["hora_inicio"])).total_seconds() / 60)
                    box_assignments[box_id]["profesionales_activos"].append({
                        "profesional": c["profesional"],
                        "especialidad": c["especialidad"],
                        "paciente": _initials_pac(c["paciente"]) if c["paciente"] else None,
                        "elapsed_min": max(0, elapsed),
                        "cita_id": c["cita_id"],
                        "paciente_id": c["paciente_id"],
                    })
                    box_assignments[box_id]["citas_hoy_ids"].add(c["cita_id"])
                    citas_asignadas.add(c["cita_id"])
                    break

        # CHOQUES: citas activas que no encontraron sala. Ocurre cuando todos los
        # boxes de su grupo están al tope (2 profesionales simultáneos) o cuando
        # el profesional no pertenece a ningún pool. Antes el `continue` las
        # descartaba sin dejar rastro — o sea el gemelo tiraba a la basura justo
        # el evento que debería gritar: dos personas peleando la misma sala.
        choques = []
        for c in activas:
            if c["cita_id"] in citas_asignadas:
                continue
            _grupo = next((b["pool_group"] for b in BOXES
                           if b["modo"] == "pool" and c["profesional_id"] in b["default_profs"]), None)
            choques.append({
                "profesional": c["profesional"],
                "especialidad": c["especialidad"],
                "hora_inicio": c["hora_inicio"].strftime("%H:%M") if c["hora_inicio"] else None,
                "motivo": "sin cupo en el grupo" if _grupo else "profesional sin pool asignado",
                "grupo": _grupo,
            })
        if choques:
            log.warning("boxes: %d cita(s) activa(s) sin sala — %s", len(choques),
                        [f'{x["profesional"]} ({x["motivo"]})' for x in choques])

        # Próximas: el primer prof "próximo" se asigna como preview al box correspondiente
        for c in proximas:
            target_box_id = None
            for box in BOXES:
                if c["profesional_id"] in box["default_profs"]:
                    target_box_id = box["id"]
                    break
            if not target_box_id:
                continue
            if box_assignments[target_box_id]["proximo"] is None and not box_assignments[target_box_id]["profesionales_activos"]:
                starts_in = int((datetime.combine(today, c["hora_inicio"]) - datetime.combine(today, now_t)).total_seconds() / 60)
                box_assignments[target_box_id]["proximo"] = {
                    "profesional": c["profesional"],
                    "especialidad": c["especialidad"],
                    "starts_in_min": starts_in,
                }

        # Revenue del día por box: sumar todos los pagos del día de los pacientes
        # cuyas citas (activas o no) hayan caído en este box.
        # Para esto, primero asignar TODAS las citas del día a su box predeterminado.
        citas_del_box = {b["id"]: [] for b in BOXES}
        # Bucket de huérfanas: citas cuyo profesional no está en NINGÚN box.
        # Antes el `break` sin `else` las descartaba en silencio, así que su plata
        # no entraba en "Revenue del día" pero sí en "Citas hoy" — los dos KPI
        # nunca cuadraban y nadie explicaba la diferencia. Ahora se muestran.
        citas_sin_sala = []
        for c in citas_hoy:
            # box destino contable: primer box donde el prof está en revenue_profs
            # (o default_profs si el box no reparte el pool). Evita doble conteo box1/box2.
            for box in BOXES:
                if c["profesional_id"] in box.get("revenue_profs", box["default_profs"]):
                    citas_del_box[box["id"]].append(c)
                    break
            else:
                citas_sin_sala.append(c)
        if citas_sin_sala:
            _profs_hf = sorted({c["profesional_id"] for c in citas_sin_sala})
            log.warning("boxes: %d cita(s) sin sala asignada — profesionales %s "
                        "(agregarlos a la planta o crearles carril)",
                        len(citas_sin_sala), _profs_hf)

        for box_id, ctas in citas_del_box.items():
            rev = 0
            for c in ctas:
                ncitas_pac = pac_cita_count.get(c["paciente_id"], 1)
                rev += (pagos_por_pac.get(c["paciente_id"], 0) / max(1, ncitas_pac))
            box_assignments[box_id]["revenue_dia"] = int(rev)
            box_assignments[box_id]["citas_dia_count"] = len(ctas)

        # Estado del box
        boxes_out = []
        for box in BOXES:
            bid = box["id"]
            asign = box_assignments[bid]
            if asign["profesionales_activos"]:
                estado_box = "ocupado"
            elif asign["proximo"]:
                estado_box = "proximo"
            else:
                estado_box = "libre"
            boxes_out.append({
                "id": bid,
                "nombre": box["nombre"],
                "piso": box["piso"],
                "orden": box["orden"],
                "tipo": box["tipo"],
                "estado": estado_box,
                "profesionales_activos": asign["profesionales_activos"],
                "proximo": asign["proximo"],
                "revenue_dia": asign["revenue_dia"],
                "citas_dia": asign["citas_dia_count"],
            })

        # Historial por box en 4 períodos (hoy / semana / mes / 30d corridos)
        # + ventanas de comparación para variación %:
        #   - hoy   vs mismo día de la semana anterior (today - 7)
        #   - semana vs misma cantidad de días de la semana pasada
        #   - mes   vs mes anterior a la misma fecha (month-to-date)
        historial = []
        desde_30 = today - timedelta(days=30)
        week_start = today - timedelta(days=today.weekday())          # lunes de esta semana
        month_start = today.replace(day=1)
        days_into_month = (today - month_start).days
        # comparación
        hoy_prev = today - timedelta(days=7)
        week_prev_start = week_start - timedelta(days=7)
        week_prev_end = today - timedelta(days=7)
        if month_start.month == 1:
            prev_month_start = month_start.replace(year=month_start.year - 1, month=12)
        else:
            prev_month_start = month_start.replace(month=month_start.month - 1)
        prev_month_mtd_end = prev_month_start + timedelta(days=days_into_month)
        desde_90 = today - timedelta(days=90)          # ventana para ticket promedio
        ayer = today - timedelta(days=1)               # no-show proxy: solo días ya cerrados
        lower = min(desde_30, week_prev_start, prev_month_start)

        params = {
            "lower": lower, "today": today,
            "hoy_prev": hoy_prev,
            "week_start": week_start,
            "week_prev_start": week_prev_start, "week_prev_end": week_prev_end,
            "month_start": month_start,
            "prev_month_start": prev_month_start, "prev_month_mtd_end": prev_month_mtd_end,
            "desde_30": desde_30, "desde_90": desde_90, "ayer": ayer,
        }
        cur.execute("""
            SELECT fa.profesional_id, COALESCE(dp.nombre,'—') AS prof,
              SUM(CASE WHEN fa.fecha = %(today)s THEN 1 ELSE 0 END) AS n_hoy,
              COALESCE(SUM(CASE WHEN fa.fecha = %(today)s THEN monto_pac.monto ELSE 0 END),0)::int AS rev_hoy,
              COALESCE(SUM(CASE WHEN fa.fecha = %(hoy_prev)s THEN monto_pac.monto ELSE 0 END),0)::int AS rev_hoy_prev,
              SUM(CASE WHEN fa.fecha BETWEEN %(week_start)s AND %(today)s THEN 1 ELSE 0 END) AS n_sem,
              COALESCE(SUM(CASE WHEN fa.fecha BETWEEN %(week_start)s AND %(today)s THEN monto_pac.monto ELSE 0 END),0)::int AS rev_sem,
              COALESCE(SUM(CASE WHEN fa.fecha BETWEEN %(week_prev_start)s AND %(week_prev_end)s THEN monto_pac.monto ELSE 0 END),0)::int AS rev_sem_prev,
              SUM(CASE WHEN fa.fecha BETWEEN %(month_start)s AND %(today)s THEN 1 ELSE 0 END) AS n_mes,
              COALESCE(SUM(CASE WHEN fa.fecha BETWEEN %(month_start)s AND %(today)s THEN monto_pac.monto ELSE 0 END),0)::int AS rev_mes,
              COALESCE(SUM(CASE WHEN fa.fecha BETWEEN %(prev_month_start)s AND %(prev_month_mtd_end)s THEN monto_pac.monto ELSE 0 END),0)::int AS rev_mes_prev,
              SUM(CASE WHEN fa.fecha BETWEEN %(desde_30)s AND %(today)s THEN 1 ELSE 0 END) AS n_30,
              COALESCE(SUM(CASE WHEN fa.fecha BETWEEN %(desde_30)s AND %(today)s THEN monto_pac.monto ELSE 0 END),0)::int AS rev_30
            FROM bi.fact_atenciones fa
            LEFT JOIN bi.dim_profesional dp ON dp.profesional_id = fa.profesional_id
            LEFT JOIN LATERAL (
              SELECT SUM(p.monto) AS monto FROM bi.fact_pagos p
              WHERE p.paciente_id = fa.paciente_id AND p.fecha = fa.fecha
            ) monto_pac ON true
            WHERE fa.fecha BETWEEN %(lower)s AND %(today)s
            GROUP BY fa.profesional_id, dp.nombre
        """, params)
        cols = ["prof", "n_hoy", "rev_hoy", "rev_hoy_prev", "n_sem", "rev_sem",
                "rev_sem_prev", "n_mes", "rev_mes", "rev_mes_prev", "n_30", "rev_30"]
        prof_stats_30d = {r[0]: dict(zip(cols, r[1:])) for r in cur.fetchall()}

        # ── Capa de decisión: horas ocupadas, ociosidad, ticket, no-show proxy ──
        # Intervalo nominal del slot por profesional (min). Horas ocupadas = nº
        # atenciones × intervalo → modela "ocupación agendada" (la sala se bloquea
        # el slot completo), coherente con el conteo que genera el revenue.
        try:
            from medilink import PROFESIONALES as _PROFS
        except Exception:
            _PROFS = {}
        def _intervalo(pid):
            return int((_PROFS.get(pid) or {}).get("intervalo", 20) or 20)

        # ►► VENTANA OPERATIVA DIARIA del box, en minutos. Define el denominador de
        #    utilización: un box "ocioso" lo está respecto de esta franja. 720 = 12h
        #    (08:00–20:00), igual que el load-bar del dashboard. AJUSTA si tus boxes
        #    operan otra franja real (ej. 600 = 10h, 540 = 9h).
        VENTANA_DIA_MIN = 720

        # Días activos por profesional (distinct fechas con atención, 30d) → para
        # agregar a nivel box (un día cuenta una vez aunque varios profs atiendan).
        cur.execute("""
            SELECT DISTINCT fecha, profesional_id
            FROM bi.fact_atenciones
            WHERE fecha BETWEEN %(desde_30)s AND %(today)s
        """, params)
        dias_por_prof: dict = {}
        for f, pid in cur.fetchall():
            dias_por_prof.setdefault(pid, set()).add(f)

        # Días con el centro abierto en la ventana. Base: lun–sáb (domingo cerrado).
        # Es el denominador honesto de la capacidad — contra los días que la sala
        # se usó, la ociosidad es invisible por construcción.
        #
        # PERO hay atenciones en domingo (medido: 3 domingos con 26 atenciones en
        # los últimos 30 días). Si el numerador las incluye y el denominador no,
        # la utilización "contra capacidad" sale MAYOR que la de días usados, que
        # es imposible. Por eso el denominador es la unión: los días de calendario
        # más cualquier día en que de hecho se atendió.
        _cal_abiertos = {desde_30 + timedelta(days=_i)
                         for _i in range((today - desde_30).days + 1)
                         if (desde_30 + timedelta(days=_i)).weekday() < 6}
        _con_atencion = set()
        for _s in dias_por_prof.values():
            _con_atencion |= _s
        dias_abiertos_30 = len(_cal_abiertos | _con_atencion) or 1

        # Última atención (ociosidad) + ticket promedio 90d (forecast), por prof.
        cur.execute("""
            SELECT fa.profesional_id, MAX(fa.fecha) AS ultima,
              ROUND(AVG(NULLIF(mp.monto,0)))::int AS ticket
            FROM bi.fact_atenciones fa
            LEFT JOIN LATERAL (
              SELECT SUM(p.monto) AS monto FROM bi.fact_pagos p
              WHERE p.paciente_id = fa.paciente_id AND p.fecha = fa.fecha
            ) mp ON true
            WHERE fa.fecha BETWEEN %(desde_90)s AND %(today)s
            GROUP BY fa.profesional_id
        """, params)
        prof_extra = {r[0]: {"ultima": r[1], "ticket": r[2] or 0} for r in cur.fetchall()}

        # No-show proxy (30d, solo días ya cerrados ≤ ayer): citas cuyo paciente NO
        # registró pago ese día. Incluye falsos positivos legítimos (bonos/controles
        # gratis), por eso el front lo rotula "sin registro de pago", no "no-show".
        cur.execute("""
            SELECT fc.profesional_id,
              COUNT(*) AS total_pasadas,
              COUNT(*) FILTER (WHERE pg.paciente_id IS NULL) AS sin_pago
            FROM bi.fact_citas fc
            LEFT JOIN (SELECT DISTINCT paciente_id, fecha FROM bi.fact_pagos) pg
              ON pg.paciente_id = fc.paciente_id AND pg.fecha = fc.fecha
            WHERE fc.fecha BETWEEN %(desde_30)s AND %(ayer)s
            GROUP BY fc.profesional_id
        """, params)
        prof_nopago = {r[0]: {"total": r[1], "sin_pago": r[2]} for r in cur.fetchall()}

        def _pct(cur_v, prev_v):
            # variación %; None si no hay base de comparación
            if prev_v and prev_v > 0:
                return round((cur_v - prev_v) / prev_v * 100)
            return None

        for box in BOXES:
            acct_profs = box.get("revenue_profs", box["default_profs"])
            stats = [prof_stats_30d[pid] for pid in acct_profs if pid in prof_stats_30d]
            stats.sort(key=lambda x: x["n_30"], reverse=True)
            def _sum(k): return sum(s[k] for s in stats)
            rev_hoy, rev_sem, rev_mes = _sum("rev_hoy"), _sum("rev_sem"), _sum("rev_mes")
            rev_30 = _sum("rev_30")

            # Horas ocupadas 30d = Σ atenciones_prof × intervalo_prof.
            occ_min = sum(prof_stats_30d[pid]["n_30"] * _intervalo(pid)
                          for pid in acct_profs if pid in prof_stats_30d)
            occ_h = occ_min / 60.0
            # Días activos del box: distinct fechas entre sus profs (sin doble conteo).
            dias_set = set()
            for pid in acct_profs:
                dias_set |= dias_por_prof.get(pid, set())
            dias_activos = len(dias_set)
            avail_min = dias_activos * VENTANA_DIA_MIN
            # Intensidad: qué tan lleno estuvo LOS DÍAS QUE SE USÓ. Sirve para ver
            # si el día que trabaja rinde, pero NO revela ociosidad: una sala
            # usada un solo día del mes, ese día lleno, da 100%.
            utilizacion = round(occ_min / avail_min * 100) if avail_min else None

            # Utilización contra CAPACIDAD: los mismos minutos ocupados, pero
            # divididos por todos los días que el centro estuvo abierto en la
            # ventana (lun–sáb; domingo cerrado). Ésta sí muestra el hueco, que es
            # la pregunta del dueño: cuánto se puede crecer sin construir.
            cap_min = dias_abiertos_30 * VENTANA_DIA_MIN
            utilizacion_cap = round(occ_min / cap_min * 100) if cap_min else None
            libres_min = max(0, cap_min - occ_min)

            # Cupos que caben en ese hueco, al intervalo típico de esta sala, y
            # cuánto valdrían al ticket promedio de sus profesionales.
            _intervalos = [_intervalo(pid) for pid in acct_profs] or [30]
            interv_box = max(5, int(sum(_intervalos) / len(_intervalos)))
            _tickets = [prof_extra[pid]["ticket"] for pid in acct_profs
                        if pid in prof_extra and prof_extra[pid]["ticket"]]
            ticket_box = int(sum(_tickets) / len(_tickets)) if _tickets else 0
            cupos_libres = int(libres_min // interv_box)
            # Sin ticket conocido, el hueco es DESCONOCIDO, no cero: mostrar
            # "970 cupos · $0" invita a leer que no vale nada llenarlos.
            plata_hueco = cupos_libres * ticket_box if ticket_box else None
            # Una sala VIRTUAL (telemedicina) no tiene metros cuadrados: su hueco
            # no es capacidad instalada que se pueda llenar mudando pacientes, así
            # que no suma al total físico. Se informa su utilización igual, pero
            # con el hueco en cero para no inflar el "cuánto cabe sin construir".
            if box.get("virtual"):
                cupos_libres = 0
                plata_hueco = 0
            yield_hora = round(rev_30 / occ_h) if occ_h else None
            # Ociosidad: días desde la última atención de cualquier prof del box.
            ultimas = [prof_extra[pid]["ultima"] for pid in acct_profs
                       if pid in prof_extra and prof_extra[pid]["ultima"]]
            dias_sin_citas = (today - max(ultimas)).days if ultimas else None
            # No-show proxy (sin registro de pago).
            np_total = sum(prof_nopago.get(pid, {}).get("total", 0) for pid in acct_profs)
            np_sin = sum(prof_nopago.get(pid, {}).get("sin_pago", 0) for pid in acct_profs)
            np_pct = round(np_sin / np_total * 100) if np_total else None

            # Forecast del día (solo hoy): realizado (pacientes que ya pagaron) +
            # pendiente estimado. Valor esperado por cita pendiente = revenue
            # realizado por atención del box (rev_30/citas_30), que YA incorpora la
            # tasa de no-pago del box. Proyectar al ticket-de-pagadores sobreestima
            # porque ignora que ~50% de las citas no registran pago.
            fc_real = fc_pend_n = 0
            n30 = _sum("n_30")
            rev_x_cita = (rev_30 / n30) if n30 else 0
            if is_today:
                pac_pagados = set()
                for c in citas_del_box.get(box["id"], []):
                    pac = c.get("paciente_id")
                    if pac and pagos_por_pac.get(pac, 0) > 0:
                        pac_pagados.add(pac)
                    else:
                        fc_pend_n += 1
                fc_real = sum(pagos_por_pac.get(p, 0) for p in pac_pagados)
            fc_pend_monto = round(fc_pend_n * rev_x_cita)
            fc_proy = fc_real + fc_pend_monto

            historial.append({
                "nombre": box["nombre"],
                "profesionales_top": [s["prof"] for s in stats[:3]],
                "revenue_hoy": rev_hoy, "delta_hoy": _pct(rev_hoy, _sum("rev_hoy_prev")),
                "revenue_sem": rev_sem, "delta_sem": _pct(rev_sem, _sum("rev_sem_prev")),
                "revenue_mes": rev_mes, "delta_mes": _pct(rev_mes, _sum("rev_mes_prev")),
                "revenue_30d": rev_30,
                "citas_30d": _sum("n_30"),
                # Capa de decisión:
                "horas_ocup_30d": round(occ_h, 1),
                "dias_activos_30d": dias_activos,
                "utilizacion_30d": utilizacion,      # % horas ocupadas vs franja disponible
                "utilizacion_cap_30d": utilizacion_cap,  # % contra TODOS los días abiertos
                "dias_abiertos_30d": dias_abiertos_30,
                "cupos_libres_30d": cupos_libres,        # cuántos pacientes más caben
                "plata_hueco_30d": plata_hueco,          # cuánto valdría llenarlos
                "intervalo_box_min": interv_box,
                "ticket_box": ticket_box,
                "yield_hora": yield_hora,            # $ por hora ocupada
                "dias_sin_citas": dias_sin_citas,    # ociosidad
                "sin_pago_30d": np_sin,              # nº citas sin registro de pago
                "sin_pago_pct": np_pct,              # % sobre citas pasadas del box
                # Forecast del día:
                "fc_realizado": fc_real,             # $ ya cobrado hoy
                "fc_pendiente_n": fc_pend_n,         # citas sin pago aún
                "fc_proyectado": fc_proy,            # realizado + pendiente estimado
            })

        # Totales
        rev_sin_sala = 0
        for c in citas_sin_sala:
            _n = pac_cita_count.get(c["paciente_id"], 1)
            rev_sin_sala += (pagos_por_pac.get(c["paciente_id"], 0) / max(1, _n))
        total_revenue = sum(b["revenue_dia"] for b in boxes_out)
        total_ocupados = sum(1 for b in boxes_out if b["estado"] == "ocupado")
        total_profs_activos = sum(len(b["profesionales_activos"]) for b in boxes_out)
        total_citas = len(citas_hoy)

        # Lista de TODOS los profesionales activos del CMC (para multi-select del editor)
        cur.execute("""
            SELECT dp.profesional_id, dp.nombre, COALESCE(de.nombre, '') AS especialidad
            FROM bi.dim_profesional dp
            LEFT JOIN bi.dim_especialidad de ON de.especialidad_id = dp.especialidad_id
            WHERE dp.es_activo = true
            ORDER BY dp.nombre
        """)
        profesionales_all = [
            {"id": r[0], "nombre": r[1], "especialidad": r[2]} for r in cur.fetchall()
        ]

        # Citas del día completas (todas) para el editor de horarios hover
        def _fmt_t(t):
            if not t: return None
            try: return f"{t.hour:02d}:{t.minute:02d}"
            except Exception: return str(t)[:5]

        citas_dia_full = []
        for c in citas_hoy:
            ncitas_pac = pac_cita_count.get(c["paciente_id"], 1)
            monto_atrib = int(pagos_por_pac.get(c["paciente_id"], 0) / max(1, ncitas_pac))
            citas_dia_full.append({
                "cita_id": c["cita_id"],
                "profesional_id": c["profesional_id"],
                "profesional": c["profesional"],
                "especialidad": c["especialidad"],
                "paciente": _initials_pac(c["paciente"]) if c["paciente"] else None,
                "hora_inicio": _fmt_t(c["hora_inicio"]),
                "hora_fin": _fmt_t(c["hora_fin"]),
                "estado": c["estado"],
                "monto_atrib": monto_atrib,
                "day_of_week": today.weekday(),
            })

        # Citas activas / próximas RAW (sin asignar a box) — para que el frontend
        # con layout custom pueda re-asignar dinámicamente.
        citas_raw = []
        for c in citas_hoy:
            if not (_is_active(c) or _is_proximo(c)):
                continue
            elapsed = None
            starts_in = None
            if _is_active(c):
                elapsed = int((datetime.combine(today, now_t) - datetime.combine(today, c["hora_inicio"])).total_seconds() / 60)
            else:
                starts_in = int((datetime.combine(today, c["hora_inicio"]) - datetime.combine(today, now_t)).total_seconds() / 60)
            citas_raw.append({
                "cita_id": c["cita_id"],
                "profesional_id": c["profesional_id"],
                "profesional": c["profesional"],
                "especialidad": c["especialidad"],
                "paciente": _initials_pac(c["paciente"]) if c["paciente"] else None,
                "elapsed_min": elapsed,
                "starts_in_min": starts_in,
                "is_active": _is_active(c),
            })

        # Revenue por profesional:
        # - HISTÓRICO: tomar directo desde bi_pagos_caja (id_profesional ya resuelto via cruce)
        # - HOY: cruzar pagos Medilink × citas_hoy por paciente_id (fallback)
        rev_por_prof = {}
        citas_por_prof = {}
        if not is_today:
            try:
                from session import db as _sqr
                with _sqr() as _sq2:
                    _rows_r = _sq2.execute(
                        "SELECT id_profesional, SUM(monto) AS m, COUNT(*) AS n FROM bi_pagos_caja "
                        "WHERE fecha = ? AND id_profesional IS NOT NULL GROUP BY id_profesional",
                        (today.isoformat(),)
                    ).fetchall()
                    for r in _rows_r:
                        rev_por_prof[r["id_profesional"]] = int(r["m"] or 0)
                        citas_por_prof[r["id_profesional"]] = int(r["n"] or 0)
            except Exception:
                pass
        if not rev_por_prof:
            # Cruce paciente → profesional vía citas del día
            for c in citas_hoy:
                pid = c["profesional_id"]
                ncitas_pac = pac_cita_count.get(c["paciente_id"], 1)
                rev_por_prof[pid] = rev_por_prof.get(pid, 0) + (pagos_por_pac.get(c["paciente_id"], 0) / max(1, ncitas_pac))
                citas_por_prof[pid] = citas_por_prof.get(pid, 0) + 1

        cur.close()
        cur = None

        # ── Gateo financiero: omitir campos monetarios para tokens sin acceso financiero ──
        if not _boxes_financiero:
            _OMIT = {"revenue_dia", "revenue_hoy", "revenue_sem", "revenue_mes",
                     "revenue_30d", "delta_hoy", "delta_sem", "delta_mes",
                     "yield_hora", "fc_realizado", "fc_pendiente_n", "fc_proyectado",
                     "monto_atrib"}
            for _b in boxes_out:
                _b.pop("revenue_dia", None)
            historial = [{k: v for k, v in h.items() if k not in _OMIT}
                         for h in historial]
            citas_dia_full = [{k: v for k, v in c.items() if k not in _OMIT}
                              for c in citas_dia_full]
            rev_por_prof = {}

        # ── Cupos y potencial de HOY, por sala y por profesional ─────────
        # Fuente: `panel_cap_cache`, el barrido nocturno que mide la agenda
        # REAL contra Medilink. Se prefiere a cualquier proxy de pagos porque
        # `bi_pagos_caja` sólo ve atenciones PAGADAS: las Fonasa y los bonos no
        # dejan registro de pago y harían ver la agenda más chica de lo que es.
        cap_hoy = {}
        try:
            from session import db as _db_cap
            with _db_cap() as _c:
                for _r in _c.execute(
                        "SELECT id_profesional, cap, n_citas FROM panel_cap_cache WHERE fecha = ?",
                        (today.isoformat(),)).fetchall():
                    cap_hoy[_r[0]] = {"cupos": _r[1] or 0, "citas": _r[2] or 0}
            with _db_cap() as _c:
                _u = _c.execute("SELECT MAX(updated_at) FROM panel_cap_cache WHERE fecha = ?",
                                (today.isoformat(),)).fetchone()
                cap_actualizado = _u[0] if _u else None
        except Exception as _e_cap:
            cap_actualizado = None
            log.warning("boxes: sin panel_cap_cache para hoy (%s)", _e_cap)

        _nom_prof = {p["id"]: p["nombre"] for p in profesionales_all}
        _citas_vivas_prof = {}
        for _c in citas_hoy:
            _citas_vivas_prof[_c["profesional_id"]] = _citas_vivas_prof.get(_c["profesional_id"], 0) + 1
        # `por_id` NO existe en esta función (vive en el simulador). El mapa se
        # arma acá desde BOXES, que sí es local.
        _por_id_box = {b["id"]: b for b in BOXES}
        for _b in boxes_out:
            _cfg_b = _por_id_box.get(_b["id"]) or {}
            # La partición contable (`revenue_profs`) primero: en box1/box2 el
            # `default_profs` es la misma lista larga y contaría los cupos dos
            # veces, una por sala.
            _acct = _cfg_b.get("revenue_profs") or _cfg_b.get("default_profs") or []
            _det, _cup, _pot = [], 0, 0
            for _pid in _acct:
                _cc = cap_hoy.get(_pid) or {}
                # Las CITAS salen siempre de lo vivo; del caché se usa sólo la
                # CAPACIDAD. Mezclar las dos épocas en la misma tarjeta mostraba
                # "24 citas de 22 cupos" —imposible— porque el barrido es de la
                # mañana y no ve lo que se agendó durante el día.
                _ct = _citas_vivas_prof.get(_pid, 0)
                _cupos = max(_cc.get("cupos") or 0, _ct)
                if not _cupos:
                    continue          # no trabaja hoy: no aporta cupos ni potencial
                _tk = (prof_extra.get(_pid) or {}).get("ticket") or 0
                _cup += _cupos
                _pot += _cupos * _tk
                _det.append({"prof_id": _pid, "profesional": _nom_prof.get(_pid, f"#{_pid}"),
                             "cupos": _cupos, "citas": _ct,
                             "libres": max(0, _cupos - _ct),
                             "ticket": _tk})
            _det.sort(key=lambda x: -x["cupos"])
            _b["cupos_hoy"] = _cup
            _b["cupos_por_prof"] = _det
            # Sin ticket conocido el potencial es DESCONOCIDO, no cero (misma
            # regla que `plata_hueco`): un "$0" se lee como "no vale nada".
            _b["potencial_hoy"] = _pot if _pot else None

        _tot_cupos = sum(_b.get("cupos_hoy") or 0 for _b in boxes_out)
        _tot_potencial = sum(_b.get("potencial_hoy") or 0 for _b in boxes_out)
        _det_global = {}
        for _b in boxes_out:
            for _d in _b.get("cupos_por_prof") or []:
                _g = _det_global.setdefault(_d["prof_id"], {**_d, "salas": []})
                if _b["nombre"] not in _g["salas"]:
                    _g["salas"].append(_b["nombre"])
        _det_global = sorted(_det_global.values(), key=lambda x: -x["cupos"])

        # El gateo financiero de más arriba ya corrió, así que estos campos —que
        # nacen acá— hay que taparlos de nuevo. Los CUPOS sí los ve recepción
        # (son agenda, no plata); el potencial y el ticket no.
        if not _boxes_financiero:
            _tot_potencial = 0
            for _b in boxes_out:
                _b.pop("potencial_hoy", None)
            for _d in [d for _b in boxes_out for d in (_b.get("cupos_por_prof") or [])] + _det_global:
                _d.pop("ticket", None)

        return {
            "now_cl": now_cl.strftime("%Y-%m-%d %H:%M:%S"),
            "totales": {
                "cupos_hoy": _tot_cupos,
                "cupos_actualizado": cap_actualizado,
                "potencial_hoy": _tot_potencial or None,
                "cupos_por_prof": _det_global,
                "boxes_totales": len(BOXES),
                "boxes_ocupados": total_ocupados,
                "profesionales_activos": total_profs_activos,
                "citas_dia": total_citas,
                # Cuántas de esas citas no tienen sala. Si es > 0, la suma de las
                # tarjetas NO va a cuadrar con "Citas hoy" y el tablero tiene que
                # decir por qué en vez de dejar la diferencia sin explicar.
                "citas_sin_sala": len(citas_sin_sala),
                "choques": len(choques),
                "desplazamientos": len(desplazamientos),
                # Capacidad ociosa del CENTRO en 30 días, sumando solo salas
                # físicas. Es la respuesta a "cuánto puedo crecer sin construir".
                "cupos_libres_30d": sum(h.get("cupos_libres_30d") or 0 for h in historial),
                "plata_hueco_30d": sum(h.get("plata_hueco_30d") or 0 for h in historial),
                **({} if not _boxes_financiero else {
                    "revenue_dia": total_revenue,
                    "revenue_sin_sala": int(rev_sin_sala),
                    "fc_realizado": sum(h["fc_realizado"] for h in historial),
                    "fc_pendiente_n": sum(h["fc_pendiente_n"] for h in historial),
                    "fc_proyectado": sum(h["fc_proyectado"] for h in historial),
                }),
            },
            "choques_detalle": choques,
            "desplazamientos_detalle": desplazamientos,
            "boxes": boxes_out,
            "boxes_config_default": BOXES,
            "profesionales_all": profesionales_all,
            "citas_raw": citas_raw,
            "citas_dia_full": citas_dia_full,
            "rev_por_prof": {str(k): int(v) for k, v in rev_por_prof.items()},
            "citas_por_prof": {str(k): int(v) for k, v in citas_por_prof.items()},
            "historial": historial,
        }
    except HTTPException:
        raise
    except Exception:
        if conn2 is not None:
            try:
                conn2.rollback()
            except Exception:
                pass
        raise
    finally:
        if conn2 is not None:
            if cur is not None:
                try:
                    cur.close()  # cierra cursor si la excepción ocurrió antes del cur.close() explícito
                except Exception:
                    pass
            pool.putconn(conn2)


def _initials_pac(nombre: str | None) -> str:
    """Devuelve iniciales del paciente para privacidad (Ej: 'Juan Perez' -> 'J.P.')."""
    if not nombre:
        return ""
    parts = [p for p in nombre.strip().split() if p]
    if not parts:
        return ""
    return ".".join(p[0].upper() for p in parts[:2]) + "."


# ── Boxes en vivo — feed PÚBLICO y ANÓNIMO para el gemelo digital ────────────
# agentecmc.cl/gemelo es HTML estático servido por nginx: no puede llevar el
# token admin embebido en el bundle. Este endpoint no pide token, pero solo
# expone {id, nombre visible, estado, especialidad} — nunca paciente, RUT,
# teléfono, nombre de profesional, plata ni horario detallado.
_BOXES_EN_VIVO_CACHE: dict = {"ts": 0.0, "data": None}
_BOXES_EN_VIVO_TTL = 30  # segundos — evita que N visitantes del gemelo golpeen el pool BI (8 conexiones)


def _anonimizar_boxes_state(raw: dict) -> list[dict]:
    """Reduce el payload interno de /admin/api/boxes-state a lo mínimo que
    necesita el gemelo. "proximo" acá exige <15 min (no los 60 min que usa el
    dashboard interno): el gemelo solo debe insinuar que alguien está por
    llegar, no filtrar la agenda con minutos exactos.
    """
    out = []
    for box in raw.get("boxes", []):
        activos = box.get("profesionales_activos") or []
        proximo = box.get("proximo") or {}
        if activos:
            estado = "ocupado"
            especialidad = activos[0].get("especialidad") or None
        elif proximo and isinstance(proximo.get("starts_in_min"), int) and proximo["starts_in_min"] <= 15:
            estado = "proximo"
            especialidad = proximo.get("especialidad") or None
        else:
            estado = "libre"
            especialidad = None
        out.append({
            "id": box.get("id"),
            "nombre_visible": box.get("nombre"),
            "estado": estado,
            "especialidad_actual": especialidad,
        })
    return out


@app.get("/api/boxes-en-vivo")
async def api_boxes_en_vivo():
    """Estado anónimo de los boxes del CMC para el gemelo digital 3D.

    Sin token: es un endpoint público consumido por HTML estático. Reutiliza
    toda la lógica de asignación de /admin/api/boxes-state (pools, prioridades,
    cautivos) llamándolo internamente con el token de servicio, así el pool BI
    de 8 conexiones no se duplica. La única diferencia es el filtro de salida:
    ver `_anonimizar_boxes_state`.

    Cache en memoria 30s: cache-aside simple, sin invalidación activa. Un
    visitante dispara el cálculo real; el resto lee la copia hasta que expire.
    """
    now = monotonic()
    if _BOXES_EN_VIVO_CACHE["data"] is not None and (now - _BOXES_EN_VIVO_CACHE["ts"]) < _BOXES_EN_VIVO_TTL:
        return _BOXES_EN_VIVO_CACHE["data"]

    try:
        raw = await api_boxes_state(token=ADMIN_TOKEN, fecha=None)
        payload = {"boxes": _anonimizar_boxes_state(raw)}
    except Exception as _e:
        log.warning("boxes-en-vivo: fallo calculando estado (%s)", _e)
        if _BOXES_EN_VIVO_CACHE["data"] is not None:
            return _BOXES_EN_VIVO_CACHE["data"]  # stale-but-served: mejor que un 500 al gemelo
        raise HTTPException(503, "Estado de boxes no disponible, reintenta en unos segundos")

    _BOXES_EN_VIVO_CACHE["data"] = payload
    _BOXES_EN_VIVO_CACHE["ts"] = now
    return payload


@app.get("/admin/api/winback-conversations")
def api_winback_conversations(token: str | None = Query(None), limit: int = 20):
    """Ultimas N conversaciones winback: consent + winback_envios + dim_paciente, enmascaradas."""
    if token != ADMIN_TOKEN:
        raise HTTPException(401, "No autorizado")

    import os as _osw2
    import psycopg2

    bi_host = _osw2.getenv("BI_DB_HOST", "127.0.0.1")
    bi_port = int(_osw2.getenv("BI_DB_PORT", "5432"))
    bi_name = _osw2.getenv("BI_DB_NAME", "health_bi")
    bi_user = _osw2.getenv("BI_DB_USER", "health_user")
    bi_pass = _osw2.getenv("BI_DB_PASSWORD", "password123")

    try:
        conn = psycopg2.connect(
            host=bi_host, port=bi_port, dbname=bi_name,
            user=bi_user, password=bi_pass, connect_timeout=5
        )
        cur = conn.cursor()
        cur.execute("""
            SELECT
                mc.phone                                  AS phone_full,
                COALESCE(NULLIF(TRIM(CONCAT_WS(' ', p.nombre, p.apellido)), ''), NULL) AS nombre,
                mc.status                                 AS cohorte,
                NULL::text                                AS especialidad,
                (mc.status IS NOT NULL)                   AS consent_enviado,
                (mc.status IN ('accepted','declined'))    AS respondio,
                (we.id IS NOT NULL)                       AS winback_enviado,
                (we.cita_id IS NOT NULL OR we.cita_atribuida_id IS NOT NULL) AS cita_atribuida,
                COALESCE(we.respondio_at, mc.consent_sent_at) AS ts_orden
            FROM bi.marketing_consent mc
            LEFT JOIN bi.dim_paciente p
                   ON RIGHT(REGEXP_REPLACE(p.telefono, '[^0-9]', '', 'g'), 9)
                    = RIGHT(REGEXP_REPLACE(mc.phone,   '[^0-9]', '', 'g'), 9)
            LEFT JOIN bi.winback_envios we
                   ON RIGHT(REGEXP_REPLACE(we.telefono, '[^0-9]', '', 'g'), 9)
                    = RIGHT(REGEXP_REPLACE(mc.phone,    '[^0-9]', '', 'g'), 9)
            ORDER BY ts_orden DESC NULLS LAST
            LIMIT %s
        """, (min(limit, 100),))
        rows = cur.fetchall()
        cur.close()
        conn.close()
        result = []
        for r in rows:
            result.append({
                "phone_full":      str(r[0]) if r[0] else None,
                "nombre":          str(r[1]) if r[1] else None,
                "cohorte":         str(r[2]) if r[2] else None,
                "especialidad":    str(r[3]) if r[3] else None,
                "consent_enviado": bool(r[4]),
                "respondio":       bool(r[5]),
                "winback_enviado": bool(r[6]),
                "cita_atribuida":  bool(r[7]),
            })
        return result
    except Exception as _e:
        import logging as _lg2
        _lg2.getLogger("winback_conversations").warning("winback-conversations error: %s", _e)
        return []


@app.get("/admin/api/winback-donuts")
def api_winback_donuts(token: str | None = Query(None)):
    """Distribucion pool por cohorte y winbacks por template Meta para los donuts del dashboard."""
    if token != ADMIN_TOKEN:
        raise HTTPException(401, "No autorizado")

    import os as _osw3
    import psycopg2

    bi_host = _osw3.getenv("BI_DB_HOST", "127.0.0.1")
    bi_port = int(_osw3.getenv("BI_DB_PORT", "5432"))
    bi_name = _osw3.getenv("BI_DB_NAME", "health_bi")
    bi_user = _osw3.getenv("BI_DB_USER", "health_user")
    bi_pass = _osw3.getenv("BI_DB_PASSWORD", "password123")

    cohortes: list = []
    templates: list = []

    try:
        conn = psycopg2.connect(
            host=bi_host, port=bi_port, dbname=bi_name,
            user=bi_user, password=bi_pass, connect_timeout=5
        )
        cur = conn.cursor()

        # Donut izquierdo: pool por cohorte (candidatos sin consent aceptado)
        try:
            cur.execute("""
                SELECT
                    vc.cohorte,
                    COUNT(*) AS candidatos
                FROM bi.v_winback_cohortes_contactables vc
                LEFT JOIN bi.marketing_consent mc
                    ON RIGHT(REGEXP_REPLACE(mc.phone, '[^0-9]', '', 'g'), 9)
                     = RIGHT(REGEXP_REPLACE(vc.telefono, '[^0-9]', '', 'g'), 9)
                   AND mc.status = 'accepted'
                WHERE mc.phone IS NULL
                GROUP BY vc.cohorte
                ORDER BY vc.cohorte
            """)
            cohortes = [{"cohorte": str(r[0]), "candidatos": int(r[1])} for r in cur.fetchall()]
        except Exception as _ec:
            import logging as _lg3
            _lg3.getLogger("winback_donuts").warning("cohortes donut error: %s", _ec)

        # Donut derecho: winbacks por template
        try:
            cur.execute("""
                SELECT
                    COALESCE(template_meta, 'sin_template') AS template,
                    COUNT(*)                                  AS enviados,
                    COUNT(*) FILTER (WHERE cita_id IS NOT NULL OR cita_atribuida_id IS NOT NULL) AS citas
                FROM bi.winback_envios
                GROUP BY template_meta
                ORDER BY enviados DESC
            """)
            templates = [
                {"template": str(r[0]), "enviados": int(r[1]), "citas": int(r[2])}
                for r in cur.fetchall()
            ]
        except Exception as _et:
            import logging as _lg4
            _lg4.getLogger("winback_donuts").warning("templates donut error: %s", _et)

        cur.close()
        conn.close()
    except Exception as _e:
        import logging as _lg5
        _lg5.getLogger("winback_donuts").warning("winback-donuts DB error: %s", _e)

    return {"cohortes": cohortes, "templates": templates}


@app.get("/abarca", response_class=HTMLResponse)
@app.get("/abarca/dashboard", response_class=HTMLResponse)
@app.get("/abarca/2026", response_class=HTMLResponse)
def abarca_dashboard_page():
    """Análisis de carga del Dr. Abarca. /abarca = histórico total · /abarca/2026 = solo 2026."""
    if not _ABARCA_DASHBOARD_HTML:
        raise HTTPException(404, "Dashboard Abarca no disponible")
    return _ABARCA_DASHBOARD_HTML


@app.get("/reemplazo-ingreso", response_class=HTMLResponse)
@app.get("/reemplazo-ingreso/dashboard", response_class=HTMLResponse)
def reemplazo_ingreso_dashboard_page():
    """Reemplazo de ingreso — cuánto del honorario clínico personal de Rodrigo
    ($5,03M/mes) puede reemplazar el CMC creciendo, sin que él atienda."""
    if not _REEMPLAZO_INGRESO_DASHBOARD_HTML:
        raise HTTPException(404, "Dashboard Reemplazo de ingreso no disponible")
    return _REEMPLAZO_INGRESO_DASHBOARD_HTML


async def _fetch_abarca_dia(cli: httpx.AsyncClient, fecha_iso: str) -> list[dict] | None:
    """Fetch atenciones del Dr. Abarca para una fecha. Retorna None si el fetch
    falla (preserva cache existente); [] o lista poblada si tuvo éxito."""
    import json as _json_ab
    import asyncio as _aio_ab
    from config import MEDILINK_BASE_URL as _MB
    params = {"id_sucursal": {"eq": 1}, "id_profesional": {"eq": 73},
              "fecha": {"eq": fecha_iso}}
    pq = {"q": _json_ab.dumps(params, separators=(",", ":"))}
    for attempt in range(6):
        try:
            resp = await cli.get(f"{_MB}/citas", params=pq, headers=HEADERS_MEDILINK)
        except Exception as e:
            log.warning("abarca fetch %s attempt=%d excepción: %s", fecha_iso, attempt, e)
            await _aio_ab.sleep(1.5 + attempt * 1.5)
            continue
        if resp.status_code == 200:
            return resp.json().get("data", []) or []
        if resp.status_code == 429:
            await _aio_ab.sleep(1.5 + attempt * 1.5)
            continue
        log.warning("abarca fetch %s HTTP %s — preservo cache", fecha_iso, resp.status_code)
        return None
    log.warning("abarca fetch %s: 6 intentos fallidos — preservo cache", fecha_iso)
    return None


_ABARCA_SYNC_LOCK = asyncio.Lock()


async def sync_abarca_atenciones(desde: str = "2025-05-01", solo_hoy: bool = False) -> dict:
    """Sincroniza atenciones del Dr. Abarca desde Medilink hacia abarca_atenciones_cache.

    `solo_hoy=True`: solo refresca el día actual (uso típico del cron diario).
    `solo_hoy=False`: barre desde `desde` hasta hoy, completando solo los días
    faltantes en cache (NO re-sincroniza días ya guardados — evita el barrido
    de 313 días que saturaba Medilink con 429s y disparaba SIGBUS por OOM
    con admin polling concurrente, visto 2026-04-30).

    Mutex global: si ya hay un sync corriendo, este await espera a que termine
    y retorna sin re-ejecutar (defense-in-depth contra dispatchers concurrentes
    desde /api/abarca/data + cron + /abarca dashboard hits paralelos).
    """
    from datetime import date as _date_s, timedelta as _td_s
    import asyncio as _aio_s
    from session import (upsert_abarca_atenciones, delete_abarca_atenciones_fecha,
                         get_abarca_fechas_existentes)

    if _ABARCA_SYNC_LOCK.locked():
        log.info("abarca sync ya en curso — esperando lock antes de retornar")
        async with _ABARCA_SYNC_LOCK:
            return {"total": 0, "dias": 0, "skipped": "in_progress"}

    async with _ABARCA_SYNC_LOCK:
        hoy = _date_s.today()
        if solo_hoy:
            fechas = [hoy.isoformat()]
        else:
            try:
                d = _date_s.fromisoformat(desde)
            except ValueError:
                d = _date_s(2025, 5, 1)
            todas: list[str] = []
            while d <= hoy:
                if d.weekday() != 6:  # skip domingos
                    todas.append(d.isoformat())
                d += _td_s(days=1)
            existentes = get_abarca_fechas_existentes()
            # Siempre re-sync HOY (puede tener atenciones nuevas) + faltantes históricos.
            fechas = [f for f in todas if f not in existentes or f == hoy.isoformat()]

        total = 0
        skipped_fail = 0
        async with httpx.AsyncClient(timeout=30) as cli:
            for f in fechas:
                citas = await _fetch_abarca_dia(cli, f)
                if citas is None:
                    skipped_fail += 1
                    if not solo_hoy:
                        await _aio_s.sleep(0.5)
                    continue
                delete_abarca_atenciones_fecha(f)
                n = upsert_abarca_atenciones(citas)
                total += n
                if not solo_hoy:
                    await _aio_s.sleep(0.5)  # 0.15→0.5: menos 429s
        log.info("abarca sync done: %d atenciones, %d días, %d failed (solo_hoy=%s)",
                 total, len(fechas), skipped_fail, solo_hoy)
        return {"total": total, "dias": len(fechas), "failed": skipped_fail}


@app.get("/api/abarca/data")
async def api_abarca_data(refresh: int = 0, desde: str = "2025-05-01"):
    """Atenciones del Dr. Abarca (id=73). Lee de abarca_atenciones_cache (sessions.db).

    `?desde=YYYY-MM-DD` filtra agregaciones desde esa fecha (default 2025-05-01).
    `?refresh=1` dispara sync delta de hoy desde Medilink antes de devolver.
    """
    from datetime import datetime as _dt_ab, date as _date_ab
    from collections import defaultdict as _dd_ab, Counter as _ct_ab
    from session import get_abarca_atenciones, abarca_cache_count

    # Si la tabla está vacía, hacer un seed completo (solo pasa la primera vez)
    if abarca_cache_count() == 0:
        log.info("abarca cache vacío — seed completo desde Medilink")
        await sync_abarca_atenciones(desde="2025-05-01", solo_hoy=False)
    elif refresh:
        await sync_abarca_atenciones(solo_hoy=True)
    else:
        # Delta liviano: refrescar hoy si la última sync de hoy es vieja (>30 min)
        from session import db as _conn_ab
        from datetime import date as _date_chk
        hoy_iso = _date_chk.today().isoformat()
        with _conn_ab() as _c:
            row = _c.execute(
                "SELECT MAX(synced_at) FROM abarca_atenciones_cache WHERE fecha=?",
                (hoy_iso,)
            ).fetchone()
        last_sync = row[0] if row else None
        needs_refresh = True
        if last_sync:
            try:
                age = (_dt_ab.utcnow() - _dt_ab.fromisoformat(last_sync)).total_seconds()
                needs_refresh = age > 1800  # 30 min
            except Exception:
                needs_refresh = True
        if needs_refresh:
            try:
                await sync_abarca_atenciones(solo_hoy=True)
            except Exception as e:
                log.warning("abarca delta hoy falló: %s", e)

    raw = get_abarca_atenciones(desde=desde)

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
        "fuente_cache": "sqlite (sync diario 23:55 CLT + delta hoy on-read)",
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


@app.get("/olavarria", response_class=HTMLResponse)
@app.get("/olavarria/dashboard", response_class=HTMLResponse)
@app.get("/olavarria/2026", response_class=HTMLResponse)
def olavarria_dashboard_page():
    """Análisis de carga e ingreso del Dr. Olavarría (id 1, dueño-doctor CMC)."""
    if not _OLAVARRIA_DASHBOARD_HTML:
        raise HTTPException(404, "Dashboard Olavarría no disponible")
    return _OLAVARRIA_DASHBOARD_HTML


async def _fetch_olavarria_dia(cli: httpx.AsyncClient, fecha_iso: str) -> list[dict] | None:
    """Fetch atenciones del Dr. Olavarría (id 1) para una fecha. Retorna None si
    el fetch falla (preserva cache existente); [] o lista poblada si tuvo éxito."""
    import json as _json_ol
    import asyncio as _aio_ol
    from config import MEDILINK_BASE_URL as _MB
    params = {"id_sucursal": {"eq": 1}, "id_profesional": {"eq": 1},
              "fecha": {"eq": fecha_iso}}
    pq = {"q": _json_ol.dumps(params, separators=(",", ":"))}
    for attempt in range(6):
        try:
            resp = await cli.get(f"{_MB}/citas", params=pq, headers=HEADERS_MEDILINK)
        except Exception as e:
            log.warning("olavarria fetch %s attempt=%d excepción: %s", fecha_iso, attempt, e)
            await _aio_ol.sleep(1.5 + attempt * 1.5)
            continue
        if resp.status_code == 200:
            return resp.json().get("data", []) or []
        if resp.status_code == 429:
            await _aio_ol.sleep(1.5 + attempt * 1.5)
            continue
        log.warning("olavarria fetch %s HTTP %s — preservo cache", fecha_iso, resp.status_code)
        return None
    log.warning("olavarria fetch %s: 6 intentos fallidos — preservo cache", fecha_iso)
    return None


_OLAVARRIA_SYNC_LOCK = asyncio.Lock()


async def sync_olavarria_atenciones(desde: str = "2024-01-01", solo_hoy: bool = False) -> dict:
    """Sincroniza atenciones del Dr. Olavarría (id 1) hacia olavarria_atenciones_cache.
    Mismo patrón que sync_abarca_atenciones."""
    from datetime import date as _date_s, timedelta as _td_s
    import asyncio as _aio_s
    from session import (upsert_olavarria_atenciones, delete_olavarria_atenciones_fecha,
                         get_olavarria_fechas_existentes)

    if _OLAVARRIA_SYNC_LOCK.locked():
        log.info("olavarria sync ya en curso — esperando lock antes de retornar")
        async with _OLAVARRIA_SYNC_LOCK:
            return {"total": 0, "dias": 0, "skipped": "in_progress"}

    async with _OLAVARRIA_SYNC_LOCK:
        hoy = _date_s.today()
        if solo_hoy:
            fechas = [hoy.isoformat()]
        else:
            try:
                d = _date_s.fromisoformat(desde)
            except ValueError:
                d = _date_s(2024, 1, 1)
            todas: list[str] = []
            while d <= hoy:
                if d.weekday() != 6:
                    todas.append(d.isoformat())
                d += _td_s(days=1)
            existentes = get_olavarria_fechas_existentes()
            fechas = [f for f in todas if f not in existentes or f == hoy.isoformat()]

        total = 0
        skipped_fail = 0
        async with httpx.AsyncClient(timeout=30) as cli:
            for f in fechas:
                citas = await _fetch_olavarria_dia(cli, f)
                if citas is None:
                    skipped_fail += 1
                    if not solo_hoy:
                        await _aio_s.sleep(0.5)
                    continue
                delete_olavarria_atenciones_fecha(f)
                n = upsert_olavarria_atenciones(citas)
                total += n
                if not solo_hoy:
                    await _aio_s.sleep(0.5)
        log.info("olavarria sync done: %d atenciones, %d días, %d failed (solo_hoy=%s)",
                 total, len(fechas), skipped_fail, solo_hoy)
        return {"total": total, "dias": len(fechas), "failed": skipped_fail}


def _api_olavarria_data_from_bi(desde: str = "2024-01-01"):
    """Lee desde olavarria_bi_ingresos (tabla cargada desde BI Postgres) y arma
    la misma estructura que devolvía el endpoint anterior. Tarifa real = avg(monto_bruto)."""
    from datetime import datetime as _dt_b, date as _date_b, timedelta as _td_b
    from collections import defaultdict as _dd_b
    from session import db as _conn_b

    with _conn_b() as _c:
        rows = _c.execute(
            "SELECT atencion_id, fecha, paciente_id, monto_bruto "
            "FROM olavarria_bi_ingresos WHERE fecha >= ? ORDER BY fecha",
            (desde,)
        ).fetchall()

    por_dia: dict = {}
    por_mes: dict = _dd_b(lambda: {"atend": 0, "monto": 0})
    por_dow: dict = _dd_b(list)
    pacientes_dia: dict = _dd_b(set)

    for r in rows:
        f = (r["fecha"] or "")[:10]
        m = f[:7]
        monto = int(r["monto_bruto"] or 0)
        por_mes[m]["atend"] += 1
        por_mes[m]["monto"] += monto
        por_dia[f] = por_dia.get(f, 0) + 1
        pacientes_dia[f].add(r["paciente_id"])

    # Backfill días vacíos
    try:
        start = _date_b.fromisoformat(desde)
    except ValueError:
        start = _date_b(2024, 1, 1)
    end = _date_b.today()
    d = start
    while d <= end:
        f = d.isoformat()
        por_dia.setdefault(f, 0)
        d += _td_b(days=1)

    for f, n in por_dia.items():
        if n > 0:
            dt = _date_b.fromisoformat(f)
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
                "min": vals[0], "max": vals[-1],
                "p90": vals[p90_idx], "n": n_v,
            }

    total_atend = sum(v["atend"] for v in por_mes.values())
    total_facturado = sum(v["monto"] for v in por_mes.values())
    n_meses = max(1, len(por_mes))
    atend_avg_mes = total_atend / n_meses
    tarifa_real = total_facturado / total_atend if total_atend else 0
    ing_avg_mes = total_facturado / n_meses
    dias_trab = sum(1 for v in por_dia.values() if v > 0)

    # FACTOR DE CORRECCIÓN BI → CAJA REAL
    # Cruce 15-may-2024: BI=$479.560, Caja real Medilink=$354.670 → factor 0.74
    # Cruce mayo-2024 completo: BI=$8.42M, Caja=$7.17M → factor 0.851
    # El BI sobrestima ~15% por atenciones registradas pero no cobradas (bug ETL)
    # NO es split caja vs bono — el usuario carga TODO como efectivo en caja, así
    # que la Caja real ya incluye bonos. La diferencia con BI son ingresos-fantasma.
    FACTOR_REAL = 0.85

    # Proyección lineal últimos 6 meses con datos
    meses_ord = sorted(por_mes.keys())
    ult6 = meses_ord[-6:] if len(meses_ord) >= 6 else meses_ord
    proyeccion = {}
    if len(ult6) >= 2:
        ys = [por_mes[m]["atend"] for m in ult6]
        xs = list(range(len(ys)))
        n_x = len(xs)
        mean_x = sum(xs) / n_x
        mean_y = sum(ys) / n_x
        num = sum((xs[i] - mean_x) * (ys[i] - mean_y) for i in range(n_x))
        den = sum((xs[i] - mean_x) ** 2 for i in range(n_x))
        slope = num / den if den else 0
        intercept = mean_y - slope * mean_x
        last_m = _date_b.fromisoformat(meses_ord[-1] + "-01") if meses_ord else _date_b.today()
        for k in range(1, 7):
            yr = last_m.year + ((last_m.month + k - 1) // 12)
            mo = ((last_m.month + k - 1) % 12) + 1
            key = f"{yr}-{mo:02d}"
            est = max(0, round(intercept + slope * (n_x - 1 + k)))
            proyeccion[key] = {"atend": est, "ingreso": round(est * tarifa_real)}

    # Aplicar factor de corrección a montos por mes y proyección (real ≈ BI × 0.85)
    por_mes_corr = {}
    for m, v in por_mes.items():
        ing_real = round(v["monto"] * FACTOR_REAL)
        por_mes_corr[m] = {"atend": v["atend"], "anul": 0, "no_asiste": 0, "otros": 0,
                            "total": v["atend"], "monto_bi": v["monto"], "monto_real": ing_real,
                            "monto_real_n": v["atend"]}
    proy_corr = {}
    for m, v in proyeccion.items():
        proy_corr[m] = {"atend": v["atend"], "ingreso": round(v["ingreso"] * FACTOR_REAL)}

    ing_avg_real = round(ing_avg_mes * FACTOR_REAL)
    total_real_historico = round(total_facturado * FACTOR_REAL)
    tarifa_real_corr = round(tarifa_real * FACTOR_REAL)

    return {
        "fecha_actualizacion": _dt_b.now().strftime("%Y-%m-%d %H:%M"),
        "fuente_cache": f"BI Postgres × {FACTOR_REAL} (cruzado con Caja Medilink real)",
        "tarifa": tarifa_real_corr,
        "tarifa_real_promedio": tarifa_real_corr,
        "factor_real": FACTOR_REAL,
        "por_dia": por_dia,
        "por_mes": por_mes_corr,
        "por_dow": dow_stats,
        "por_hora": {},
        "estados": {"atendido": total_atend},
        "proyeccion": proy_corr,
        "kpis": {
            "total_atend": total_atend,
            "dias_con_atencion": dias_trab,
            "atend_avg_mes": round(atend_avg_mes, 1),
            "ing_avg_mes": ing_avg_real,
            "ing_avg_mes_bi": round(ing_avg_mes),
            "n_meses": n_meses,
            "monto_real_total": total_real_historico,
            "monto_real_n_atend": total_atend,
            "cobertura_real_pct": 100.0,
            "tarifa_real_promedio": tarifa_real_corr,
            "ing_total_historico": total_real_historico,
            "ing_total_bi": total_facturado,
            "factor_aplicado": FACTOR_REAL,
        },
    }


@app.get("/api/olavarria/data")
async def api_olavarria_data(refresh: int = 0, desde: str = "2024-01-01", tarifa: int = 15100):
    """
    FUENTE PRIMARIA: olavarria_bi_ingresos (importada del BI Postgres health-bi-project,
    refleja /atenciones de Medilink con monto_bruto real). Más confiable que el cache
    propio del bot, que filtraba por estado_cita='atendido' en /citas y subestimaba ~22%.
    Si la tabla BI está vacía cae al cache antiguo (degradación graceful).
    """
    from datetime import datetime as _dt_b, date as _date_b, timedelta as _td_b
    from collections import defaultdict as _dd_b, Counter as _ct_b
    from session import db as _conn_b
    with _conn_b() as _c:
        bi_count = _c.execute("SELECT COUNT(*) FROM olavarria_bi_ingresos WHERE fecha >= ?", (desde,)).fetchone()[0]
    if bi_count > 0:
        return _api_olavarria_data_from_bi(desde=desde)
    # Fallback al cache antiguo:
    """Atenciones del Dr. Olavarría (id 1) con agregaciones para proyección de ingreso.

    `?desde=YYYY-MM-DD` filtra agregaciones desde esa fecha (default 2024-01-01).
    `?refresh=1` dispara sync delta de hoy desde Medilink antes de devolver.
    `?tarifa=N` tarifa por atención en CLP (default 30.000, ajustable desde el UI).
    """
    from datetime import datetime as _dt_ol, date as _date_ol
    from collections import defaultdict as _dd_ol, Counter as _ct_ol
    from session import get_olavarria_atenciones, olavarria_cache_count

    if olavarria_cache_count() == 0:
        log.info("olavarria cache vacío — kickoff seed completo en background")
        _spawn_bg(sync_olavarria_atenciones(desde=desde, solo_hoy=False), name="seed_olavarria")
    else:
        # Detectar cache incompleto: si la fecha máxima en cache es más vieja
        # que hace 7 días, retomar seed completo. Si no, solo delta de hoy.
        from session import db as _conn_ol
        hoy = _date_ol.today()
        with _conn_ol() as _c:
            row = _c.execute(
                "SELECT MAX(fecha) FROM olavarria_atenciones_cache"
            ).fetchone()
        max_fecha = row[0] if row else None
        cache_incompleto = False
        if max_fecha:
            try:
                gap_dias = (hoy - _date_ol.fromisoformat(max_fecha)).days
                cache_incompleto = gap_dias > 7
            except Exception:
                pass
        if cache_incompleto:
            log.info("olavarria cache incompleto (max=%s, gap>7d) — retomando seed", max_fecha)
            _spawn_bg(sync_olavarria_atenciones(desde=desde, solo_hoy=False), name="seed_olavarria_resumido")
        elif refresh:
            _spawn_bg(sync_olavarria_atenciones(solo_hoy=True), name="refresh_olavarria_hoy")
        else:
            with _conn_ol() as _c:
                row = _c.execute(
                    "SELECT MAX(synced_at) FROM olavarria_atenciones_cache WHERE fecha=?",
                    (hoy.isoformat(),)
                ).fetchone()
            last_sync = row[0] if row else None
            needs_refresh = True
            if last_sync:
                try:
                    age = (_dt_ol.utcnow() - _dt_ol.fromisoformat(last_sync)).total_seconds()
                    needs_refresh = age > 1800
                except Exception:
                    needs_refresh = True
            if needs_refresh:
                _spawn_bg(sync_olavarria_atenciones(solo_hoy=True), name="delta_olavarria_hoy")

    raw = get_olavarria_atenciones(desde=desde)

    por_dia: dict = {}
    por_mes: dict = _dd_ol(lambda: {"atend": 0, "anul": 0, "no_asiste": 0, "otros": 0, "total": 0,
                                     "monto_real": 0, "monto_real_n": 0})
    por_dow: dict = _dd_ol(list)
    por_hora: dict = _dd_ol(int)
    estados: dict = _ct_ol()

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
            # Monto facturado real (si ya se sincronizó desde /atenciones)
            mr = c.get("monto_facturado")
            if mr is not None:
                por_mes[m]["monto_real"] += int(mr)
                por_mes[m]["monto_real_n"] += 1
        elif st == "anulado" or "anulad" in st:
            por_mes[m]["anul"] += 1
        elif "asiste" in st:
            por_mes[m]["no_asiste"] += 1
        else:
            por_mes[m]["otros"] += 1

    from datetime import timedelta as _td_ol
    try:
        start = _date_ol.fromisoformat(desde)
    except ValueError:
        start = _date_ol(2024, 1, 1)
    end = _date_ol.today()
    d = start
    while d <= end:
        f = d.isoformat()
        por_dia.setdefault(f, 0)
        d += _td_ol(days=1)

    for f, n in por_dia.items():
        if n > 0:
            dt = _date_ol.fromisoformat(f)
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

    total_atend = sum(v for v in por_dia.values())
    dias_trab = sum(1 for v in por_dia.values() if v > 0)
    n_meses = max(1, len(por_mes))
    atend_avg_mes = total_atend / n_meses
    ing_avg_mes = atend_avg_mes * tarifa
    # Monto real Medilink (suma de los meses donde hay datos sincronizados)
    monto_real_total = sum(v["monto_real"] for v in por_mes.values())
    monto_real_n_atend = sum(v["monto_real_n"] for v in por_mes.values())
    cobertura_real_pct = (monto_real_n_atend / total_atend * 100) if total_atend else 0
    tarifa_real_promedio = (monto_real_total / monto_real_n_atend) if monto_real_n_atend else 0

    # Proyección lineal: regresión simple sobre los últimos 6 meses con datos
    meses_ord = sorted(por_mes.keys())
    ult6 = meses_ord[-6:] if len(meses_ord) >= 6 else meses_ord
    proyeccion = {}
    if len(ult6) >= 2:
        ys = [por_mes[m]["atend"] for m in ult6]
        xs = list(range(len(ys)))
        n_x = len(xs)
        mean_x = sum(xs) / n_x
        mean_y = sum(ys) / n_x
        num = sum((xs[i] - mean_x) * (ys[i] - mean_y) for i in range(n_x))
        den = sum((xs[i] - mean_x) ** 2 for i in range(n_x))
        slope = num / den if den else 0
        intercept = mean_y - slope * mean_x
        # Próximos 6 meses
        from datetime import date as _d_p
        last_m = _d_p.fromisoformat(meses_ord[-1] + "-01") if meses_ord else _d_p.today()
        for k in range(1, 7):
            yr = last_m.year + ((last_m.month + k - 1) // 12)
            mo = ((last_m.month + k - 1) % 12) + 1
            key = f"{yr}-{mo:02d}"
            est = max(0, round(intercept + slope * (n_x - 1 + k)))
            proyeccion[key] = {"atend": est, "ingreso": est * tarifa}

    return {
        "fecha_actualizacion": _dt_ol.now().strftime("%Y-%m-%d %H:%M"),
        "fuente_cache": "sqlite (sync diario 23:55 CLT + delta hoy on-read)",
        "tarifa": tarifa,
        "por_dia": por_dia,
        "por_mes": dict(por_mes),
        "por_dow": dow_stats,
        "por_hora": dict(por_hora),
        "estados": dict(estados),
        "proyeccion": proyeccion,
        "kpis": {
            "total_atend": total_atend,
            "dias_con_atencion": dias_trab,
            "atend_avg_mes": round(atend_avg_mes, 1),
            "ing_avg_mes": round(ing_avg_mes),
            "n_meses": n_meses,
            "monto_real_total": monto_real_total,
            "monto_real_n_atend": monto_real_n_atend,
            "cobertura_real_pct": round(cobertura_real_pct, 1),
            "tarifa_real_promedio": round(tarifa_real_promedio),
        },
    }


async def sync_olavarria_montos(limite: int = 0, delay: float = 0.5) -> dict:
    """Rellena monto_facturado consultando /atenciones/{id} por cada cita atendida
    sin monto. NO sobreescribe los ya cargados. `limite=0` procesa todos.

    Este relleno era la principal fuente de 429 contra Medilink (~1.000/día).
    Se auto-alimentaba: si los reintentos daban 429 no se escribía nada, la
    atención seguía NULL y volvía a pedirse en la pasada siguiente. Medido el
    18-ago-2026: **29.132 llamadas a /atenciones/{id} para solo 951 IDs
    distintos** — 30 por ID, uno llegó a 221. Tres cambios lo cortan:

      1. `use_batch_lane()` — deja de competir con los pacientes en vivo por
         el rate limit (guardrail: todo cron que pegue a Medilink lo llama).
      2. Cliente compartido en vez de httpx.AsyncClient propio, que se saltaba
         el throttling de medilink.py.
      3. Los fallos suman `intentos_monto`; a los 3 la atención sale de la cola
         (ver `_MAX_INTENTOS_MONTO` en session.py). Lo que Medilink no entregó
         en 3 pasadas no lo va a entregar por insistir.
    """
    import asyncio as _aio_m
    from session import (get_olavarria_atenciones_sin_monto, update_olavarria_monto,
                         marcar_intento_monto)
    from config import MEDILINK_BASE_URL as _MB
    from medilink import use_batch_lane, _get_shared_client

    use_batch_lane()   # guardrail 429: este relleno NO es prioritario
    pendientes = get_olavarria_atenciones_sin_monto()
    if limite > 0:
        pendientes = pendientes[:limite]

    ok = 0; fail = 0; sin_id = 0; agotados = 0
    cli = _get_shared_client()
    for row in pendientes:
        id_aten = row.get("id_atencion")
        id_cita = row.get("id_cita")
        if not id_aten:
            sin_id += 1; continue
        resuelto = False
        # 3 intentos (no 5): con el contador persistido, insistir dentro de la
        # misma pasada aporta poco y multiplica la presión sobre el HIS.
        for attempt in range(3):
            try:
                r = await cli.get(f"{_MB}/atenciones/{id_aten}", headers=HEADERS_MEDILINK)
            except Exception:
                await _aio_m.sleep(1 + attempt)
                continue
            if r.status_code == 200:
                total = (r.json().get("data") or {}).get("total", 0) or 0
                update_olavarria_monto(id_cita, int(total))
                ok += 1; resuelto = True
                break
            if r.status_code == 429:
                await _aio_m.sleep(1.5 + attempt * 1.5)
                continue
            fail += 1
            break
        if not resuelto:
            # Persistir el fallo: a los 3 sale de la cola y deja de martillar.
            marcar_intento_monto(id_cita)
            agotados += 1
        await _aio_m.sleep(delay)

    log.info("olavarria montos sync: ok=%d fail=%d sin_id=%d sin_resolver=%d (de %d pendientes)",
             ok, fail, sin_id, agotados, len(pendientes))
    return {"ok": ok, "fail": fail, "sin_id": sin_id,
            "sin_resolver": agotados, "pendientes": len(pendientes)}


@app.post("/api/olavarria/sync-montos")
async def api_olavarria_sync_montos(limite: int = 0):
    """Dispara el llenado de monto_facturado desde Medilink. Background."""
    _spawn_bg(sync_olavarria_montos(limite=limite), name="sync_olavarria_montos")
    return {"started": True, "limite": limite or "todos"}


# ── BI v2: dashboard genérico por profesional ───────────────────────────────

@app.get("/profesional/dashboard", response_class=HTMLResponse)
def profesional_dashboard_token_page():
    """Dashboard personal del profesional — auth por token en query string.
    DEBE declararse antes que /profesional/{id_prof} para que FastAPI no
    intente parsear 'dashboard' como int."""
    if not _PROF_DASHBOARD_HTML:
        raise HTTPException(404, "Dashboard profesional no disponible")
    return _PROF_DASHBOARD_HTML


@app.get("/centro", response_class=HTMLResponse)
def centro_dashboard_page():
    """Consolidado de TODO el centro — mismo dashboard genérico con id 0."""
    if not _PROF_BI_DASHBOARD_HTML:
        raise HTTPException(404, "Dashboard profesional no disponible")
    # no-store: sin esto el navegador cachea heurísticamente el HTML y el
    # dueño no ve los cambios tras un deploy (pasó el 2026-08-04).
    return HTMLResponse(_PROF_BI_DASHBOARD_HTML,
                        headers={"Cache-Control": "no-store"})


@app.get("/profesional/{id_prof}", response_class=HTMLResponse)
def profesional_dashboard_page(id_prof: int):
    """Dashboard genérico por profesional (BI v2). Come de
    /api/profesional/{id}/data y resuelve el id desde el pathname.
    Sirve `profesional_bi_dashboard.html`, NO el de token."""
    if not _PROF_BI_DASHBOARD_HTML:
        raise HTTPException(404, "Dashboard profesional no disponible")
    return HTMLResponse(_PROF_BI_DASHBOARD_HTML,
                        headers={"Cache-Control": "no-store"})


@app.get("/api/profesional/{id_prof}/data")
async def api_profesional_data(id_prof: int, desde: str = "2024-01-01",
                                refresh: int = 0):
    """KPIs por profesional. Mezcla:
    - bi_atenciones (volumen + facturado total)
    - bi_pagos_caja (CAJA REAL — fuente primaria de ingreso, igual a Medilink Cajas)
    """
    from bi_sync import sync_profesional, stats_profesional, stats_profesional_caja
    from session import db as _c_p
    # id_prof=0 → CONSOLIDADO del centro. No hay "profesional 0" que sincronizar:
    # se lee lo que ya cargaron los syncs individuales + el cron diario.
    if id_prof != 0:
        with _c_p() as c:
            n_rows = c.execute(
                "SELECT COUNT(*) FROM bi_atenciones WHERE id_profesional=?", (id_prof,)
            ).fetchone()[0]
        if n_rows == 0:
            log.info("BI v2: prof=%d cache vacío → kickoff seed en background", id_prof)
            _spawn_bg(sync_profesional(id_prof, desde=desde), name=f"seed_prof_{id_prof}")
        elif refresh:
            _spawn_bg(sync_profesional(id_prof, desde=desde, force=False), name=f"refresh_prof_{id_prof}")

    base = stats_profesional(id_prof, desde=desde)
    caja = stats_profesional_caja(id_prof, desde=desde)

    # Inyectar caja real por mes en por_mes
    for m, v in base["por_mes"].items():
        c = caja["por_mes"].get(m, {})
        v["caja_real"] = c.get("caja", 0)
        v["n_pagos"] = c.get("n_pagos", 0)

    # KPIs caja real
    n_meses = base["kpis"]["n_meses"]
    base["kpis"]["caja_real_total"] = caja["total_caja"]
    base["kpis"]["caja_real_avg_mes"] = round(caja["total_caja"] / n_meses) if n_meses else 0
    base["kpis"]["n_pagos_total"] = caja["total_pagos"]
    # Cobertura caja/facturado
    fac = base["kpis"]["total_facturado"]
    base["kpis"]["cobertura_caja_pct"] = round(100 * caja["total_caja"] / fac, 1) if fac else 0
    # Agenda real por día (citas Medilink cacheadas) — para que el calendario
    # distinga "día con agenda" de "día solo con fichas a distancia".
    if id_prof != 0:
        from session import get_agenda_dias
        base["agenda_dias"] = get_agenda_dias(id_prof, desde=desde)
    else:
        base["agenda_dias"] = {}
    base["fuente"] = "bi_atenciones (volumen) + bi_pagos_caja (CAJA REAL)"
    return base


@app.get("/api/profesional/{id_prof}/dia")
async def api_profesional_dia(id_prof: int, fecha: str,
                              token: str | None = Query(None),
                              cmc_session: str | None = Cookie(None)):
    """Detalle del día para el calendario del dashboard: citas reales de Medilink
    con horarios. Los NOMBRES de pacientes solo van con auth admin (token query
    o cookie) — la página /profesional/{id} es pública y esto es dato sensible."""
    import re as _re
    if not _re.fullmatch(r"\d{4}-\d{2}-\d{2}", fecha):
        raise HTTPException(400, "fecha inválida (YYYY-MM-DD)")
    from admin_routes import _verify_cookie, _is_admin_token
    autorizado = bool(token and _is_admin_token(token)) or bool(
        cmc_session and _verify_cookie(cmc_session) in ("admin", "ortodoncia"))
    from medilink import citas_dia_lista
    citas = await citas_dia_lista(id_prof, fecha)
    if citas is None:
        raise HTTPException(503, "Medilink no respondió — intenta de nuevo")
    out = []
    for c in citas:
        out.append({
            "ini": (c.get("hora_inicio") or "")[:5],
            "fin": (c.get("hora_fin") or "")[:5],
            "paciente": (c.get("nombre_paciente") or "").strip() if autorizado else None,
            "estado": c.get("estado_cita"),
            "anulada": bool(c.get("estado_anulacion")),
        })
    out.sort(key=lambda x: x["ini"])
    return {"fecha": fecha, "autorizado": autorizado, "citas": out}


@app.post("/api/profesional/{id_prof}/sync")
async def api_profesional_sync(id_prof: int, desde: str = "2024-01-01",
                                force: int = 0):
    """Dispara sync manual de atenciones en background."""
    from bi_sync import sync_profesional
    _spawn_bg(sync_profesional(id_prof, desde=desde, force=bool(force)), name=f"manual_sync_prof_{id_prof}")
    return {"started": True, "id_profesional": id_prof, "desde": desde, "force": bool(force)}


# ── Dashboard personal por profesional (token-auth) ──────────────────────────

# Tabla de tokens: id_profesional → token (HMAC-SHA256 de "prof:{id}:{secret}")
# Generados una vez, almacenados como config estática. Nunca expiran (30d reservado para futuro).

def _make_prof_token(id_prof: int) -> str:
    """Genera token determinístico para un profesional usando ADMIN_TOKEN como secreto."""
    import hashlib as _hl, hmac as _hm
    raw = f"prof:{id_prof}:{ADMIN_TOKEN}"
    return _hm.new(ADMIN_TOKEN.encode(), raw.encode(), _hl.sha256).hexdigest()[:32]


@app.get("/api/alma/profesionales")
@app.get("/api/anima/profesionales")  # alias legacy — borrar tras ventana de gracia
def api_alma_profesionales(token: str | None = Query(None),
                            cmc_session: str | None = Cookie(None)):
    """Roster de profesionales agrupado por especialidad, con el token de cada uno,
    para construir el navegador desplegable del módulo 'Panel del Profesional' en Alma.
    Auth admin (token query o cookie de sesión)."""
    from admin_routes import _verify_cookie, _is_admin_token
    from alma_scope import profesional_id_of as _prof_id_of
    is_admin = (token and _is_admin_token(token)) or (
        cmc_session and _verify_cookie(cmc_session) in ("admin", "ortodoncia"))
    scope_pid = _prof_id_of(token)  # perfil de profesional → solo se ve a sí mismo
    if not is_admin and scope_pid is None:
        raise HTTPException(401, "No autorizado")
    from medilink import PROFESIONALES
    grupos: dict[str, list] = {}
    for pid, info in sorted(PROFESIONALES.items(), key=lambda kv: (kv[1].get("especialidad", ""), kv[1].get("nombre", ""))):
        if scope_pid is not None and pid != scope_pid:
            continue
        esp = (info.get("especialidad") or "Otros").split(" / ")[0].strip()
        grupos.setdefault(esp, []).append({
            "id": pid,
            "nombre": info.get("nombre", f"Prof {pid}"),
            "url": f"/profesional/dashboard?token={_make_prof_token(pid)}",
        })
    return {"grupos": [{"especialidad": esp, "profesionales": profs}
                       for esp, profs in sorted(grupos.items())]}

@app.get("/api/profesional/dashboard")
async def api_profesional_dashboard_data(token: str = ""):
    """KPIs del mes actual + tendencia + NPS + ranking + acciones sugeridas.
    Autenticado por token individual firmado HMAC. Sin admin token requerido."""
    import hmac as _hm, hashlib as _hl
    from datetime import date as _date
    from medilink import PROFESIONALES
    from session import get_nps_por_profesional
    from bi_sync import stats_profesional, stats_profesional_caja

    # Verificar token: buscar qué profesional corresponde
    id_prof = None
    for pid in PROFESIONALES:
        expected = _make_prof_token(pid)
        if _hm.compare_digest(expected, (token or "")[:32]):
            id_prof = pid
            break
    if id_prof is None:
        raise HTTPException(401, "Token inválido")

    prof_info = PROFESIONALES[id_prof]
    hoy = _date.today()
    mes_actual = hoy.strftime("%Y-%m")
    mes_anterior_anio = f"{hoy.year-1}-{hoy.month:02d}"
    desde_anio = f"{hoy.year-1}-01-01"

    # Datos BI
    try:
        base = stats_profesional(id_prof, desde=desde_anio)
        caja = stats_profesional_caja(id_prof, desde=desde_anio)
    except Exception as _e:
        log.warning("api_profesional_dashboard stats error prof=%d: %s", id_prof, _e)
        base = {"por_mes": {}, "kpis": {}, "por_dia": {}, "proyeccion": {}}
        caja = {"por_mes": {}, "total_caja": 0, "total_pagos": 0}

    pm = base.get("por_mes", {})
    pd = base.get("por_dia", {})

    # KPIs del mes actual
    mes_data = pm.get(mes_actual, {})
    mes_ant_data = pm.get(mes_anterior_anio, {})
    atend_mes = mes_data.get("atend") or mes_data.get("atendidos_total") or 0
    atend_ant = mes_ant_data.get("atend") or mes_ant_data.get("atendidos_total") or None

    # Ingreso mes actual desde caja real
    caja_mes = caja.get("por_mes", {}).get(mes_actual, {})
    ingreso_mes = caja_mes.get("caja") or None

    # No-shows y utilización: TODO — Medilink no expone este campo directo en /citas BI.
    # Se necesita cruzar /citas?estado_anulacion=0 con /citas?id_estado=1 por mes y profesional.
    # Por ahora se devuelven como null para que el frontend muestre "—".
    noshows = None
    citados_mes = None
    slots_ocupados = None
    slots_total = None

    # NPS desde fidelizacion_msgs
    try:
        nps_data = get_nps_por_profesional(dias=90)
        nps_prof = next((p for p in nps_data.get("por_profesional", [])
                         if p.get("profesional") == prof_info["nombre"]), {})
    except Exception:
        nps_prof = {}

    # Ranking dentro de la especialidad (atenciones mes actual)
    especialidad = prof_info["especialidad"]
    pares = [pid for pid, p in PROFESIONALES.items() if p["especialidad"] == especialidad]
    atend_pares = {}
    for pid in pares:
        try:
            pd2 = stats_profesional(pid, desde=f"{hoy.year}-01-01")
            atend_pares[pid] = pd2.get("por_mes", {}).get(mes_actual, {}).get("atend") or 0
        except Exception:
            atend_pares[pid] = 0

    sorted_pares = sorted(atend_pares.items(), key=lambda x: -x[1])
    pos = next((i+1 for i, (pid, _) in enumerate(sorted_pares) if pid == id_prof), None)
    ranking = {
        "posicion": pos,
        "total": len(pares),
        "pct_ile": round(100*(len(pares)-pos+1)/len(pares)) if pos else None,
    }

    # Tendencia últimos 12 meses vs año anterior
    from datetime import date as _d2
    meses_tend = []
    actual_vals = []
    anterior_vals = []
    for i in range(11, -1, -1):
        import calendar as _cal
        base_d = _date(hoy.year, hoy.month, 1)
        # retroceder i meses
        y, m = base_d.year, base_d.month - i
        while m <= 0: m += 12; y -= 1
        mk = f"{y}-{m:02d}"
        mk_ant = f"{y-1}-{m:02d}"
        meses_tend.append(mk)
        actual_vals.append(pm.get(mk, {}).get("atend") or 0)
        anterior_vals.append(pm.get(mk_ant, {}).get("atend") or 0)

    # Promedio especialidad (atend mes actual)
    prom_esp = round(sum(atend_pares.values()) / len(pares)) if pares else None

    # Dias trabajados del mes (para heatmap)
    dias_mes = {f: pd.get(f, 0) for f in pd if f.startswith(mes_actual)}

    # Avg diario para referencia del heatmap
    dias_vals = [v for v in dias_mes.values() if v > 0]
    avg_dia = round(sum(dias_vals)/len(dias_vals)) if dias_vals else 10

    # Acciones sugeridas (heurísticas simples)
    acciones = _generar_acciones(id_prof, atend_mes, atend_ant, nps_prof, ranking, hoy)

    nombres_meses = ["","Enero","Febrero","Marzo","Abril","Mayo","Junio","Julio","Agosto",
                     "Septiembre","Octubre","Noviembre","Diciembre"]
    mes_label = f"{nombres_meses[hoy.month]} {hoy.year}"

    return {
        "id_profesional": id_prof,
        "nombre": prof_info["nombre"],
        "especialidad": especialidad,
        "mes_label": mes_label,
        "kpis": {
            "atend_mes": atend_mes,
            "atend_mes_anio_anterior": atend_ant,
            "prom_especialidad": prom_esp,
            "noshows": noshows,
            "citados_mes": citados_mes,
            "slots_ocupados": slots_ocupados,
            "slots_total": slots_total,
            "ingreso_mes": ingreso_mes,
            "dias_trabajados": len([v for v in dias_mes.values() if v > 0]),
            "atend_avg_dia": avg_dia,
        },
        "tendencia": {
            "meses": meses_tend,
            "actual": actual_vals,
            "anterior": anterior_vals,
        },
        "nps": {
            "nps": nps_prof.get("nps"),
            "total": nps_prof.get("total", 0),
            "mejor": nps_prof.get("mejor", 0),
            "igual": nps_prof.get("igual", 0),
            "peor": nps_prof.get("peor", 0),
        },
        "ranking": ranking,
        "por_dia": dias_mes,
        "acciones": acciones,
    }

def _generar_acciones(id_prof: int, atend_mes: int, atend_ant, nps_prof: dict,
                      ranking: dict, hoy) -> list[dict]:
    """Genera hasta 3 acciones sugeridas basadas en datos reales."""
    from medilink import PROFESIONALES
    acciones = []

    # Accion 1: comparativa año anterior
    if atend_ant is not None:
        delta = atend_mes - atend_ant
        if delta < 0:
            acciones.append({
                "titulo": f"Recuperar {-delta} atenciones vs el año pasado",
                "descripcion": (f"Este mes vas con {atend_mes} atenciones; el mismo mes del año pasado "
                                f"tuviste {atend_ant}. Revisa si hay horas libres esta semana."),
                "tipo": "urgente" if delta < -5 else "normal",
            })
        elif delta > 0:
            acciones.append({
                "titulo": f"Vas {delta} atenciones arriba vs el año pasado",
                "descripcion": (f"{atend_mes} atenciones este mes vs {atend_ant} el año anterior. "
                                f"Buen ritmo — mantenerlo es clave para el cierre del mes."),
                "tipo": "normal",
            })

    # Accion 2: NPS
    nps_val = nps_prof.get("nps")
    nps_total = nps_prof.get("total", 0)
    if nps_total >= 3 and nps_val is not None:
        if nps_val < 30:
            acciones.append({
                "titulo": "Revisar feedbacks negativos recientes",
                "descripcion": (f"Tu NPS de los últimos 90 días está en {nps_val:+.0f}. "
                                f"Hay {nps_prof.get('peor', 0)} respuestas 'Peor'. "
                                f"Coordina con recepción para revisar esas conversaciones."),
                "tipo": "urgente",
            })
        elif nps_val >= 70:
            acciones.append({
                "titulo": "Pide a tus pacientes satisfechos que recomienden el CMC",
                "descripcion": (f"Tu NPS es {nps_val:+.0f} — en el top del centro. "
                                f"Es el momento ideal para activar referidos: un paciente contento "
                                f"trae entre 1 y 2 pacientes nuevos en promedio."),
                "tipo": "normal",
            })

    # Accion 3: ranking
    pos = ranking.get("posicion")
    total = ranking.get("total")
    if pos and total and total > 1:
        if pos == 1:
            acciones.append({
                "titulo": "Primer lugar en tu especialidad este mes",
                "descripcion": f"Lideras el ranking de {PROFESIONALES[id_prof]['especialidad']} con {atend_mes} atenciones. Compartir agenda con recepcion para mantener la ocupacion.",
                "tipo": "normal",
            })
        elif pos == total:
            acciones.append({
                "titulo": "Hay espacio para subir en el ranking esta semana",
                "descripcion": (f"Vas en la posicion {pos} de {total} en {PROFESIONALES[id_prof]['especialidad']}. "
                                f"Coordina con recepcion: ¿hay horas sin confirmar que se puedan abrir?"),
                "tipo": "urgente",
            })

    # Limitar a 3
    return acciones[:3]

@app.get("/admin/enviar-dashboard-semanal")
async def admin_enviar_dashboard_semanal(forzar: int = 0, token: str | None = Query(None)):
    """Dispara envío manual del dashboard semanal a todos los profesionales (requiere auth admin)."""
    import hmac as _hm2
    if not token or not _hm2.compare_digest(token, ADMIN_TOKEN):
        raise HTTPException(403, "Forbidden")
    from jobs import _job_enviar_dashboards_semanales
    _spawn_bg(_job_enviar_dashboards_semanales(forzar=bool(forzar)), name="dashboard_semanal_manual")
    return {"started": True, "nota": "Enviando en background. Ver logs para estado."}



@app.post("/api/bi/sync-atenciones")
async def api_bi_sync_atenciones(desde: str = "2024-01-01", hasta: str | None = None,
                                   force: int = 0):
    """Dispara sync de /atenciones a bi_atenciones (refresca total/abonado/deuda).

    Necesario para que el matcher de pagos cruce por monto exacto: las
    atenciones recién creadas vienen con total=$0 hasta que se cobran. Re-
    sincronizar después actualiza los campos.
    """
    from bi_sync import sync_rango
    _spawn_bg(sync_rango(desde=desde, hasta=hasta, force=bool(force)), name="sync_rango")
    return {"started": True, "desde": desde, "hasta": hasta or "today", "force": bool(force)}


@app.post("/api/bi/rematch-pagos")
def api_bi_rematch_pagos(desde: str = "2026-01-01", hasta: str | None = None):
    """Re-aplica el matcher heurístico sobre pagos del rango, respetando
    overrides manuales (nivel 0). Útil después de un sync de atenciones que
    haya actualizado campos total/abonado."""
    from session import db as _conn
    from bi_sync import _resolver_profesional_pago
    from datetime import date as _date
    if not hasta:
        hasta = _date.today().isoformat()
    cambios = 0
    with _conn() as c:
        cur = c.cursor()
        cur.execute(
            "SELECT pago_id, fecha, monto, id_paciente, id_profesional "
            "FROM bi_pagos_caja WHERE fecha>=? AND fecha<=?",
            (desde, hasta)
        )
        for r in cur.fetchall():
            pago_dict = {"id": r[0], "fecha_recepcion": r[1],
                          "monto_pago": r[2], "id_paciente": r[3]}
            id_prof_nuevo, id_aten_nuevo = _resolver_profesional_pago(c, pago_dict)
            if id_prof_nuevo and id_prof_nuevo != r[4]:
                cur.execute(
                    "UPDATE bi_pagos_caja SET id_profesional=?, atencion_id=? WHERE pago_id=?",
                    (id_prof_nuevo, id_aten_nuevo, r[0])
                )
                cambios += 1
    return {"ok": True, "desde": desde, "hasta": hasta, "cambios": cambios}


@app.post("/api/bi/sync-pagos")
async def api_bi_sync_pagos(desde: str = "2024-01-01", hasta: str | None = None,
                              force: int = 0):
    """Dispara sync de /pagos a bi_pagos_caja (fuente primaria caja real)."""
    from bi_sync import sync_pagos_rango
    _spawn_bg(sync_pagos_rango(desde=desde, hasta=hasta, force=bool(force)), name="sync_pagos_rango")
    return {"started": True, "desde": desde, "hasta": hasta or "today", "force": bool(force)}


# ─── /api/state: dashboard mensual simulación (editor de datos) ───────────────
# El dashboard /cmc/mensual y /bi/mensual usa este endpoint para persistir
# profesionales / áreas / sim_params / custom_charts / box_data editados
# manualmente desde la UI. Se guarda en sessions.db tabla cmc_dashboard_state
# (singleton, id=1). Antes este endpoint vivía solo en health-bi-project (local
# Mac, Postgres) → los edits en producción se perdían silenciosamente.

def _ensure_state_table(c):
    c.execute("""
        CREATE TABLE IF NOT EXISTS cmc_dashboard_state (
            id          INTEGER PRIMARY KEY,
            state_json  TEXT NOT NULL DEFAULT '{}',
            updated_at  TEXT
        )
    """)
    c.execute(
        "INSERT OR IGNORE INTO cmc_dashboard_state (id, state_json, updated_at) "
        "VALUES (1, '{}', datetime('now'))"
    )


@app.get("/api/state")
def api_state_get():
    """Carga el state del dashboard mensual (singleton row id=1)."""
    import json as _json_st
    from session import db as _c_st
    with _c_st() as c:
        _ensure_state_table(c)
        row = c.execute(
            "SELECT state_json FROM cmc_dashboard_state WHERE id=1"
        ).fetchone()
    try:
        data = _json_st.loads(row[0] if row else "{}")
    except Exception:
        data = {}
    return {
        "profesionales":     data.get("profesionales", []),
        "areas":             data.get("areas", []),
        "sim_params":        data.get("sim_params", []),
        "global_dias":       int(data.get("global_dias", 22)),
        "custom_charts":     data.get("custom_charts", []) or [],
        "sim_custom_charts": data.get("sim_custom_charts", []) or [],
        "box_data":          data.get("box_data", {}) or {},
        "sheets_hash":       data.get("sheets_hash", ""),
        "sheets_pushed_at":  data.get("sheets_pushed_at"),
    }


@app.put("/api/state")
def api_state_put(body: dict):
    """Persiste el state. Solo guarda campos conocidos (whitelist)."""
    import json as _json_st
    from session import db as _c_st
    payload = {
        "profesionales":     body.get("profesionales", []),
        "areas":             body.get("areas", []),
        "sim_params":        body.get("sim_params", []),
        "global_dias":       int(body.get("global_dias", 22) or 22),
        "custom_charts":     body.get("custom_charts", []) or [],
        "sim_custom_charts": body.get("sim_custom_charts", []) or [],
        "box_data":          body.get("box_data", {}) or {},
    }
    with _c_st() as c:
        _ensure_state_table(c)
        c.execute(
            "REPLACE INTO cmc_dashboard_state (id, state_json, updated_at) "
            "VALUES (1, ?, datetime('now'))",
            (_json_st.dumps(payload, ensure_ascii=False),)
        )
    return {"ok": True}


@app.get("/api/cmc/mensual")
def api_cmc_mensual(mes: str | None = None):
    """Agrega bi_pagos_caja por profesional y por área para un mes (YYYY-MM).
    Si mes es None, usa el mes actual."""
    from session import db as _c_m
    from medilink import PROFESIONALES
    from datetime import date as _d_m

    AREA_MAP = {
        1: "med", 73: "med", 13: "med", 23: "med", 60: "med", 61: "med",
        65: "med", 64: "med", 79: "med",
        68: "tecmed", 80: "tecmed",
        55: "dent", 72: "dent", 66: "dent", 75: "dent", 69: "dent", 76: "dent",
        59: "maso", 77: "kine", 21: "kine",
        52: "nutri", 74: "psico", 49: "psico", 70: "fono",
        67: "matrona", 56: "podo",
    }
    AREA_LABELS = {
        "med": "Medicina", "dent": "Dental", "kine": "Kinesiología",
        "maso": "Masoterapia", "nutri": "Nutrición", "psico": "Psicología",
        "fono": "Fonoaudiología", "matrona": "Matrona", "podo": "Podología",
        "tecmed": "Ecografía", "otros": "Otros",
    }

    if not mes:
        mes = _d_m.today().strftime("%Y-%m")
    inicio = f"{mes}-01"
    yr, mo = int(mes[:4]), int(mes[5:7])
    fin_y = yr + (mo // 12); fin_mo = (mo % 12) + 1
    fin = f"{fin_y}-{fin_mo:02d}-01"

    with _c_m() as c:
        rows = c.execute(
            "SELECT id_profesional, COUNT(*) AS n, SUM(monto) AS total, "
            "       COUNT(DISTINCT id_paciente) AS pacientes "
            "FROM bi_pagos_caja WHERE fecha>=? AND fecha<? "
            "AND id_profesional IS NOT NULL "
            "GROUP BY id_profesional ORDER BY 3 DESC",
            (inicio, fin)
        ).fetchall()
        # Pagos sin profesional resuelto (huérfanos) — se agrupan como "Sin asignar"
        huerfanos = c.execute(
            "SELECT COUNT(*) AS n, SUM(monto) AS total, "
            "       COUNT(DISTINCT id_paciente) AS pacientes "
            "FROM bi_pagos_caja WHERE fecha>=? AND fecha<? "
            "AND id_profesional IS NULL",
            (inicio, fin)
        ).fetchone()
        dia_count = c.execute(
            "SELECT COUNT(DISTINCT fecha) FROM bi_pagos_caja "
            "WHERE fecha>=? AND fecha<?", (inicio, fin)
        ).fetchone()[0] or 0
        rows_dia = c.execute(
            "SELECT fecha, COUNT(DISTINCT id_paciente) AS n, SUM(monto) AS total "
            "FROM bi_pagos_caja WHERE fecha>=? AND fecha<? "
            "GROUP BY fecha ORDER BY fecha", (inicio, fin)
        ).fetchall()
        # Lista de meses disponibles
        meses_rows = c.execute(
            "SELECT DISTINCT substr(fecha,1,7) AS m FROM bi_pagos_caja "
            "WHERE fecha>='2024-01-01' ORDER BY m DESC"
        ).fetchall()

    profs = []
    por_area: dict = {}
    total_mes = 0
    n_pagos_total = 0
    for r in rows:
        pid = r["id_profesional"]
        info = PROFESIONALES.get(pid, {})
        nombre = info.get("nombre") or f"Prof {pid}"
        area = AREA_MAP.get(pid, "otros")
        total = int(r["total"] or 0)
        profs.append({
            "id": pid, "nombre": nombre,
            "especialidad": info.get("especialidad", ""),
            "area": area, "area_label": AREA_LABELS.get(area, area),
            "total": total, "n_pagos": r["n"], "pacientes": r["pacientes"],
        })
        por_area.setdefault(area, {"label": AREA_LABELS.get(area, area), "total": 0, "n_pagos": 0})
        por_area[area]["total"] += total
        por_area[area]["n_pagos"] += r["n"]
        total_mes += total
        n_pagos_total += r["n"]

    # Pagos sin profesional cruzado: aparecen como "Sin asignar" para que cuadre el total
    huer_n = huerfanos["n"] or 0
    huer_total = int(huerfanos["total"] or 0)
    if huer_n > 0:
        profs.append({
            "id": None, "nombre": "Sin asignar",
            "especialidad": "(pagos sin cruce a atención)",
            "area": "sin_asignar", "area_label": "Sin asignar",
            "total": huer_total, "n_pagos": huer_n,
            "pacientes": huerfanos["pacientes"] or 0,
        })
        por_area["sin_asignar"] = {
            "label": "Sin asignar", "total": huer_total, "n_pagos": huer_n,
        }
        total_mes += huer_total
        n_pagos_total += huer_n

    from datetime import datetime as _dt_cm
    return {
        "mes": mes,
        "fecha_actualizacion": _dt_cm.now().strftime("%Y-%m-%d %H:%M"),
        "total_mes": total_mes,
        "n_profesionales_activos": sum(1 for p in profs if p.get("id") is not None),
        "n_pagos_total": n_pagos_total,
        "dias_con_actividad": dia_count,
        "profesionales": profs,
        "areas": [{"key": k, **v} for k, v in sorted(por_area.items(), key=lambda x: -x[1]["total"])],
        "por_dia": [{"fecha": r["fecha"], "pacientes": r["n"], "total": int(r["total"] or 0)} for r in rows_dia],
        "meses_disponibles": [r["m"] for r in meses_rows],
    }


@app.get("/cmc/mensual", response_class=HTMLResponse)
def cmc_mensual_page():
    """Dashboard mensual v2 — leído de bi_pagos_caja (CSV oficial Medilink)."""
    p = _TEMPLATE_DIR / "cmc_mensual.html"
    if not p.exists():
        raise HTTPException(404, "Dashboard mensual no disponible")
    return p.read_text(encoding="utf-8")


@app.get("/api/profesionales")
def api_profesionales_list():
    """Lista de profesionales del CMC con sus IDs Medilink."""
    from medilink import PROFESIONALES
    return {
        "profesionales": [
            {"id": pid, "nombre": info.get("nombre"),
             "especialidad": info.get("especialidad"),
             "intervalo": info.get("intervalo"),
             "dashboard": f"/profesional/{pid}"}
            for pid, info in sorted(PROFESIONALES.items())
        ]
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
    try:
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

        # Origen web (MEDIDO): marcador "(web)" / "(web: slug)" del link wa.me del sitio.
        # Distinto de los tags referido:* (DECLARADO por el paciente post-cita).
        # Cada visita web guarda 'referral_source:web' + opcional 'referral_source:web_<slug>'
        # (home/blog), así que el total se cuenta con DISTINCT phone para no duplicar.
        def _web_por_fuente(_window_sql):
            c.execute(f"""SELECT tag, COUNT(DISTINCT phone) FROM contact_tags
                          WHERE tag LIKE 'referral_source:web_%' AND {_window_sql}
                          GROUP BY tag ORDER BY 2 DESC""")
            return {r[0].split("referral_source:web_", 1)[1]: r[1] for r in c.fetchall()}

        c.execute("""SELECT COUNT(DISTINCT phone) FROM contact_tags
                     WHERE tag LIKE 'referral_source:web%' AND date(ts)=date('now')""")
        _web_total_hoy = c.fetchone()[0]
        c.execute("""SELECT COUNT(DISTINCT phone) FROM contact_tags
                     WHERE tag LIKE 'referral_source:web%' AND ts > datetime('now','-30 days')""")
        _web_total_30d = c.fetchone()[0]
        out["origen_web"] = {
            "total_hoy": _web_total_hoy,
            "total_30d": _web_total_30d,
            "por_fuente_hoy": _web_por_fuente("date(ts)=date('now')"),
            "por_fuente_30d": _web_por_fuente("ts > datetime('now','-30 days')"),
        }

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

        # Meta Ads del día — agregado + por campaña
        import os as _os_atr, urllib.request as _ur_atr, urllib.parse as _up_atr
        token = (_os_atr.getenv("META_ACCESS_TOKEN") or "").strip()
        acct = "act_220608142267129"
        if token:
            try:
                # Aggregate
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

                # Per-campaign breakdown
                params_camp = {
                    "fields": "campaign_id,campaign_name,objective,spend,impressions,reach,clicks,frequency,actions",
                    "level": "campaign",
                    "time_range": _json_atr.dumps({"since": today, "until": today}),
                    "limit": 50,
                    "access_token": token,
                }
                url_camp = f"https://graph.facebook.com/v19.0/{acct}/insights?" + _up_atr.urlencode(params_camp)
                with _ur_atr.urlopen(url_camp, timeout=10) as resp:
                    dc = _json_atr.loads(resp.read())
                    campaigns = []
                    for r in dc.get("data", []):
                        actions = r.get("actions") or []
                        convs = sum(int(float(a.get("value", 0))) for a in actions
                                    if a.get("action_type") in ("onsite_conversion.messaging_conversation_started_7d",
                                                                  "onsite_conversion.total_messaging_connection"))
                        link_clicks = next((int(float(a.get("value", 0))) for a in actions if a.get("action_type") == "link_click"), 0)
                        spend = float(r.get("spend", 0))
                        impressions = int(r.get("impressions", 0))
                        reach = int(r.get("reach", 0))
                        clicks = int(r.get("clicks", 0))
                        campaigns.append({
                            "id": r.get("campaign_id"),
                            "name": r.get("campaign_name"),
                            "objective": r.get("objective"),
                            "spend_clp": spend,
                            "impressions": impressions,
                            "reach": reach,
                            "clicks": clicks,
                            "link_clicks": link_clicks,
                            "frequency": float(r.get("frequency", 0)),
                            "conversaciones": convs,
                            "ctr_pct": round(100.0 * clicks / impressions, 2) if impressions else 0,
                            "cpm_clp": round(spend * 1000 / impressions, 0) if impressions else 0,
                            "cpc_clp": round(spend / clicks, 0) if clicks else 0,
                            "costo_x_conversacion_clp": round(spend / convs, 0) if convs else 0,
                        })
                    campaigns.sort(key=lambda x: -x["spend_clp"])
                    out["meta"]["campaigns"] = campaigns
            except Exception as e:
                out["meta"]["error"] = str(e)[:200]

        # Google Ads — placeholder hasta que la cuenta esté creada
        # Cuando esté: pull via Google Ads API con search_term_view + campaign report
        out["google_ads"] = {"status": "no_configurado", "campaigns": []}

        # Comparación cross-channel
        meta_spend = (out.get("meta", {}) or {}).get("spend_clp", 0)
        meta_convs = (out.get("meta", {}) or {}).get("conversaciones_iniciadas", 0)
        google_spend = sum(c.get("spend_clp", 0) for c in (out.get("google_ads", {}).get("campaigns") or []))
        google_convs = sum(c.get("conversaciones", 0) for c in (out.get("google_ads", {}).get("campaigns") or []))
        organic_phones = out["bot"].get("phones_nuevos", 0) - meta_convs - google_convs
        out["comparacion"] = {
            "meta": {
                "spend_clp": meta_spend,
                "conversaciones": meta_convs,
                "costo_x_conv_clp": round(meta_spend / meta_convs, 0) if meta_convs else None,
            },
            "google_ads": {
                "spend_clp": google_spend,
                "conversaciones": google_convs,
                "costo_x_conv_clp": round(google_spend / google_convs, 0) if google_convs else None,
            },
            "organico": {
                "spend_clp": 0,
                "phones_atribuibles": max(0, organic_phones),
            },
            "total_spend_clp": meta_spend + google_spend,
            "total_phones_nuevos": out["bot"].get("phones_nuevos", 0),
            "citas_creadas_total": out["bot"].get("citas_creadas", 0),
        }

        return out
    finally:
        conn.close()


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
# account_id: leído desde config (META_AD_ACCOUNT_ID env var, default act_220608142267129)
# Override por query param: /api/seo/meta-ads?account_id=act_XXX

# Cliente httpx compartido (singleton) — reutiliza conexiones HTTP/2 con graph.facebook.com
_META_HTTP: httpx.AsyncClient | None = None

def _get_meta_client() -> httpx.AsyncClient:
    global _META_HTTP
    if _META_HTTP is None or _META_HTTP.is_closed:
        _META_HTTP = httpx.AsyncClient(
            base_url="https://graph.facebook.com/v19.0",
            timeout=10.0,
            http2=False,  # graph.facebook.com no siempre negocia h2 limpiamente
        )
    return _META_HTTP


async def _meta_get(path: str, params: dict | None = None) -> dict:
    """Async helper para Marketing API. Token en Authorization header (no en URL).
    Retry automático: max 3 intentos con backoff 0.5/1/2s en 429/5xx.
    En 4xx (salvo 429) no reintenta.
    """
    token = os.getenv("META_ACCESS_TOKEN", "")
    if not token:
        return {"error": "no META_ACCESS_TOKEN"}
    client = _get_meta_client()
    p = dict(params or {})
    delays = [0.5, 1.0, 2.0]
    last_err: str = "unknown"
    for attempt, delay in enumerate(delays, 1):
        try:
            resp = await client.get(
                path,
                params=p,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
            if resp.status_code == 200:
                return resp.json()
            last_err = f"HTTP {resp.status_code}"
            # 4xx (salvo 429): no reintentar
            if 400 <= resp.status_code < 500 and resp.status_code != 429:
                return {"error": last_err, "body": resp.text[:300]}
            # 429 / 5xx: reintentar con backoff
            if attempt < len(delays):
                await asyncio.sleep(delay)
        except Exception as e:
            last_err = str(e)
            if attempt < len(delays):
                await asyncio.sleep(delay)
    return {"error": last_err}


def _sum_conv(actions: list) -> int:
    """Suma conversaciones iniciadas (los 2 action types relevantes)."""
    if not actions:
        return 0
    types = ("onsite_conversion.messaging_conversation_started_7d",
             "onsite_conversion.messaging_first_reply")
    return sum(int(a.get("value", 0)) for a in actions if a.get("action_type") in types)


# Mapa de periodo (query param) → date_preset de Graph API
_PERIODO_MAP: dict[str, str] = {
    "last_7d":  "last_7d",
    "last_30d": "last_30d",
    "last_90d": "last_90d",
    "maximum":  "maximum",
}


@app.get("/api/seo/meta-ads")
async def seo_meta_ads_api(
    periodo: str = "maximum",
    account_id: str | None = None,
    token: str = "",
    cmc_session: str | None = Cookie(None),
):
    """Análisis de Meta Ads (FB+IG → WhatsApp) cruzado con citas del chatbot.
    periodo: last_7d | last_30d | last_90d | maximum (default)
    account_id: override del account (fallback a META_AD_ACCOUNT_ID en config/.env)
    """
    _seo_api_auth(token, cmc_session)

    acct = account_id or _CFG_META_ACCOUNT_ID
    preset = _PERIODO_MAP.get(periodo, "maximum")

    # Nota: breakdowns (demografía, placement, horario) no siempre aceptan
    # date_preset con todos los valores en combination — si falla, la llamada
    # retorna {"error": ...} y el gather captura la excepción sin romper el resto.
    # hourly_stats solo está disponible con rango ≤ 90 días; si el preset es
    # "maximum" lo limitamos a "last_90d" para ese breakdown.
    hourly_preset = preset if preset != "maximum" else "last_90d"

    # Las 7 llamadas en paralelo; return_exceptions=True para resiliencia parcial
    (lifetime, monthly, campaigns, placement, demo, hourly) = await asyncio.gather(
        _meta_get(f"{acct}/insights",
                  {"fields": "spend,impressions,reach,clicks,actions",
                   "date_preset": preset}),
        _meta_get(f"{acct}/insights",
                  {"fields": "spend,impressions,reach,clicks,frequency,actions",
                   "time_increment": "monthly", "date_preset": preset}),
        _meta_get(f"{acct}/insights",
                  {"fields": "campaign_name,spend,impressions,clicks,frequency,actions",
                   "level": "campaign", "date_preset": preset, "limit": 50}),
        _meta_get(f"{acct}/insights",
                  {"fields": "spend,impressions,clicks,actions",
                   "breakdowns": "publisher_platform,platform_position",
                   "date_preset": preset}),
        _meta_get(f"{acct}/insights",
                  {"fields": "spend,impressions,clicks,actions",
                   "breakdowns": "age,gender", "date_preset": preset}),
        _meta_get(f"{acct}/insights",
                  {"fields": "spend,clicks,actions",
                   "breakdowns": "hourly_stats_aggregated_by_advertiser_time_zone",
                   "date_preset": hourly_preset}),
        return_exceptions=True,
    )

    # Normalizar excepciones a dicts de error
    def _safe(r):
        return r if isinstance(r, dict) else {"error": str(r)}
    lifetime, monthly, campaigns, placement, demo, hourly = (
        _safe(lifetime), _safe(monthly), _safe(campaigns),
        _safe(placement), _safe(demo), _safe(hourly)
    )

    # 7. Cruce con chatbot: pacientes nuevos por mes (filtrado por periodo)
    import sqlite3
    from pathlib import Path as _Path
    db_path = _Path(__file__).parent.parent / "data" / "heatmap_cache.db"
    nuevos_mes = []
    if db_path.exists():
        # Calcular fecha_desde según periodo para filtrar citas_heatmap
        from datetime import date, timedelta
        _hoy = date.today()
        _dias = {"last_7d": 7, "last_30d": 30, "last_90d": 90}
        _desde = (_hoy - timedelta(days=_dias[preset])).isoformat() if preset in _dias else None
        conn = sqlite3.connect(str(db_path))
        try:
            if _desde:
                rows = conn.execute("""
                    WITH primera AS (
                        SELECT id_paciente, MIN(fecha) AS f
                        FROM citas_heatmap WHERE id_paciente IS NOT NULL AND fecha >= ?
                        GROUP BY id_paciente)
                    SELECT substr(f,1,7) AS mes, COUNT(*) FROM primera
                    GROUP BY mes ORDER BY mes
                """, (_desde,)).fetchall()
            else:
                rows = conn.execute("""
                    WITH primera AS (
                        SELECT id_paciente, MIN(fecha) AS f
                        FROM citas_heatmap WHERE id_paciente IS NOT NULL
                        GROUP BY id_paciente)
                    SELECT substr(f,1,7) AS mes, COUNT(*) FROM primera
                    GROUP BY mes ORDER BY mes
                """).fetchall()
            for mes, n in rows:
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

    result = {
        "fuente": "meta_marketing_api",
        "ad_account_id": acct,
        "periodo": preset,
        "lifetime": proc_lifetime(lifetime),
        "monthly": proc_monthly(monthly),
        "top_campaigns": proc_campaigns(campaigns)[:20],
        "placement": proc_placement(placement),
        "demografia": proc_demo(demo),
        "hourly": proc_hourly(hourly),
        "pacientes_nuevos_chatbot": nuevos_mes,
    }
    # Incluir errores parciales para diagnóstico
    errs = {k: v["error"] for k, v in [
        ("lifetime", lifetime), ("monthly", monthly), ("campaigns", campaigns),
        ("placement", placement), ("demografia", demo), ("hourly", hourly),
    ] if isinstance(v, dict) and "error" in v}
    if errs:
        result["partial_errors"] = errs
    return result


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


# Snapshot de localidades de Arauco — fuente: bi.dim_paciente.localidad × Censo INE 2017.
# Se actualiza manualmente cuando los valores cambian significativamente.
# Última actualización: 2026-05-10 desde container BI Postgres (health_bi_api).
# Buckets internos del script heatmap_comunas.py que NO son localidades reales
_LOCALIDAD_DESCARTADA = {"ARAUCO (OTRO)", "ARAUCO (SIN DETALLE)"}


@app.get("/api/seo/localidades-arauco")
def seo_localidades_arauco():
    """Pacientes únicos por localidad urbana de Arauco × Censo INE 2017.
    Lee del último data/heatmap_*.json regenerado por scripts/heatmap_comunas.py
    (single source of truth con el resto del dashboard)."""
    import json, glob, os
    from datetime import datetime as _dt
    # Solo snapshots mensuales (heatmap_<mes>_<año>.json), NO heatmap_cache.json
    # que tiene otro propósito y no trae localidades_arauco.
    all_files = glob.glob(str(Path(__file__).parent.parent / "data" / "heatmap_*.json"))
    files = sorted(
        [f for f in all_files if not f.endswith("heatmap_cache.json")],
        key=os.path.getmtime, reverse=True,
    )
    if not files:
        raise HTTPException(503, "Sin snapshot de heatmap")
    raw = json.loads(Path(files[0]).read_text(encoding="utf-8"))
    locs = []
    for item in raw.get("localidades_arauco", []):
        nombre_raw = (item.get("localidad") or "").strip().upper()
        if nombre_raw in _LOCALIDAD_DESCARTADA:
            continue
        display = item.get("localidad", "").strip().title()
        if display == "Arauco Urbano":
            display = "Arauco urbano"
        locs.append({
            "localidad":   display,
            "pacientes":   item.get("pacientes", 0),
            "atenciones":  item.get("citas", 0),
            "poblacion":   item.get("poblacion", 0),
            "pct_captura": item.get("pct_captura", 0.0) or 0.0,
        })
    locs.sort(key=lambda x: x["pacientes"], reverse=True)
    mtime = os.path.getmtime(files[0])
    return {
        "fecha_snapshot": _dt.fromtimestamp(mtime).strftime("%Y-%m-%d"),
        "fuente":         "scripts/heatmap_comunas.py × INE Censo 2017",
        "arauco":         locs,
    }


@app.get("/api/seo/paginas")
def seo_paginas_list(token: str = "", cmc_session: str | None = Cookie(None)):
    """Páginas SEO especialidad×comuna generadas + oportunidades pendientes."""
    _seo_api_auth(token, cmc_session)
    import seo_pages
    return {"paginas": seo_pages.listar(), "oportunidades": seo_pages.oportunidades()}


@app.post("/api/seo/generar-pagina")
async def seo_generar_pagina(request: Request, token: str = "",
                             cmc_session: str | None = Cookie(None)):
    """Crea una landing SEO para una celda especialidad×comuna (contenido único via
    Claude + schema fijo). Devuelve la meta + el HTML para previsualizar embebido."""
    _seo_api_auth(token, cmc_session)
    import seo_pages
    body = await request.json()
    esp_slug = (body.get("esp_slug") or "").strip()
    com_slug = (body.get("com_slug") or "").strip()
    publicar = bool(body.get("publicar"))
    if not esp_slug or not com_slug:
        raise HTTPException(400, "esp_slug y com_slug requeridos")
    try:
        res = await seo_pages.generar(esp_slug, com_slug, publicar=publicar)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return res


@app.get("/seo/p/{slug}", response_class=HTMLResponse)
def seo_pagina_serve(slug: str):
    """Sirve una página SEO generada (pública — para previsualizar embebida y, si se
    publica, para que Google la indexe)."""
    import seo_pages
    html = seo_pages.get_html(slug)
    if not html:
        raise HTTPException(404, "página no encontrada")
    return HTMLResponse(html)


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
    "Matrona":                   20000,
    "Podología":                 25000,
    "Ecografía":                 40000,
    "Traumatología":             35000,
}


def _periodo_to_fecha_desde(periodo: str) -> str | None:
    """Convierte un periodo label en una fecha mínima YYYY-MM-DD (None = todos)."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
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


@app.get("/api/seo/meta-creatives")
async def seo_meta_creatives_api(
    account_id: str | None = None,
    token: str = "",
    cmc_session: str | None = Cookie(None),
):
    """Creatives activos en Meta Ads — últimos 30 días.
    Llama a Marketing API ads?fields=name,creative{thumbnail_url,...},insights{...}
    Devuelve array de creatives con gasto, impresiones, CTR, conversaciones y frecuencia.
    Si la API falla o no hay creatives, devuelve {"creatives": []}.
    """
    _seo_api_auth(token, cmc_session)

    acct = account_id or _CFG_META_ACCOUNT_ID

    resp = await _meta_get(
        f"{acct}/ads",
        {
            "fields": (
                "name,"
                "creative{thumbnail_url,object_story_spec,effective_object_story_id},"
                "insights{spend,impressions,clicks,ctr,frequency,actions}"
            ),
            "limit": 50,
            "date_preset": "last_30d",
        }
    )

    if "error" in resp:
        return {"creatives": [], "error": resp["error"]}

    creatives = []
    for ad in resp.get("data", []):
        ins = (ad.get("insights") or {})
        ins_data = ins.get("data", [{}])
        d = ins_data[0] if ins_data else {}
        gasto = float(d.get("spend", 0))
        if gasto < 10:
            continue  # filtrar ads sin gasto significativo

        cre = ad.get("creative") or {}
        thumb = cre.get("thumbnail_url") or None

        actions = d.get("actions") or []
        conversaciones = _sum_conv(actions)
        frecuencia = float(d.get("frequency", 0))

        creatives.append({
            "nombre":         (ad.get("name") or "")[:80],
            "thumbnail_url":  thumb,
            "gasto":          gasto,
            "impresiones":    int(d.get("impressions", 0)),
            "clicks":         int(d.get("clicks", 0)),
            "ctr":            d.get("ctr"),
            "conversaciones": conversaciones,
            "frecuencia":     frecuencia,
        })

    creatives.sort(key=lambda x: -x["gasto"])
    return {"creatives": creatives, "ad_account_id": acct, "periodo": "last_30d"}


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


@app.get("/api/watchdog/entrega-status")
def api_watchdog_entrega_status(
    token: str | None = Query(None),
    cmc_session: str | None = Cookie(None),
):
    """Estado actual del watchdog de entrega real de templates WhatsApp.

    Lee /var/log/cmc-entrega-watchdog-state.json (escrito por _job_watchdog_entrega
    cada 30 min). Gateado por OLACORE_TOKEN — solo el dueño.

    Respuesta JSON:
      is_bad (bool): hay apagón activo
      total, delivered, delivered_pct, failed, err_131042: métricas de la última ventana
      ventana_h: horas de la ventana de análisis
      ts: ISO timestamp de la última evaluación
      last_alert_ts, last_recovery_ts: floats Unix de los últimos eventos
    """
    import hmac as _hm_we
    import json as _json_we
    from pathlib import Path
    eff = token or cmc_session or ""
    if not (eff and OLACORE_TOKEN and _hm_we.compare_digest(eff, OLACORE_TOKEN)):
        raise HTTPException(404, "No encontrado")
    _state_file = Path("/var/log/cmc-entrega-watchdog-state.json")
    if not _state_file.exists():
        # Aún no corrió el primer ciclo
        return {
            "is_bad": False,
            "total": 0,
            "delivered": 0,
            "delivered_pct": 100.0,
            "failed": 0,
            "err_131042": 0,
            "ventana_h": 6,
            "ts": None,
            "last_alert_ts": 0.0,
            "last_recovery_ts": 0.0,
            "no_data": True,
        }
    try:
        return _json_we.loads(_state_file.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("api_watchdog_entrega_status: error leyendo state file: %s", e)
        raise HTTPException(500, "Error leyendo estado del watchdog")


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

def _es_intent_rescate_takeover(texto: str) -> bool:
    """¿El mensaje es una intención de AUTOSERVICIO inequívoca que justifica
    sacar al paciente de HUMAN_TAKEOVER (solo si ningún humano respondió aún)?

    Cubre: elegir especialidad (motivo_*), elegir profesional (agendar_prof_*),
    agendar, volver al menú, ver/cambiar citas. NO incluye saludos sueltos
    ("hola") ni pedir recepción de nuevo — esos se quedan en silencio."""
    t = (texto or "").strip().lower()
    if not t:
        return False
    if t.startswith("motivo_") or t.startswith("agendar_prof_"):
        return True
    _RESCATE = {
        "agendar", "accion_agendar", "agendar_sugerido", "quick_book",
        "menu", "menu_volver", "inicio",
        "accion_otro", "accion_mis_citas", "accion_cambiar", "accion_waitlist",
        "mis horas", "mis citas", "cat_medico", "cat_dental",
    }
    return t in _RESCATE


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
    # Fail-CLOSED: sin secret configurado NO se puede verificar la firma de Meta,
    # así que rechazamos en vez de aceptar payloads sin autenticar (un webhook sin
    # firma permitiría inyectar mensajes WhatsApp falsos que agendan/cancelan citas
    # reales). En prod META_APP_SECRET está seteado; este guard evita la regresión
    # si alguna vez falta en el .env.
    if not _MAS:
        log.error("webhook RECHAZADO: META_APP_SECRET ausente, no se puede validar firma")
        return Response(status_code=503)
    sig_header = request.headers.get("x-hub-signature-256", "")
    def _wh_diag():
        try:
            import json as _jd
            return (_jd.loads(body_bytes.decode() or "{}") or {}).get("object")
        except Exception:
            return "?"
    if not sig_header.startswith("sha256="):
        log.warning("webhook firma faltante/malformada obj=%s", _wh_diag())
        return Response(status_code=403)
    import hmac as _hmac_w, hashlib as _hl_w
    from config import INSTAGRAM_APP_SECRET as _IGS
    def _sig_match(_secret):
        if not _secret:
            return False
        _exp = "sha256=" + _hmac_w.new(_secret.encode(), body_bytes, _hl_w.sha256).hexdigest()
        return _hmac_w.compare_digest(sig_header, _exp)
    # WhatsApp/Messenger(page) firman con el Facebook App Secret (META_APP_SECRET);
    # los webhooks del objeto `instagram` (IG con Instagram Login) firman con el
    # Instagram App Secret. Aceptamos si CUALQUIERA valida — ambos son secretos de
    # Meta, así que sigue siendo imposible forjar un webhook sin uno de ellos.
    if not (_sig_match(_MAS) or _sig_match(_IGS)):
        log.warning("webhook firma inválida obj=%s", _wh_diag())
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
        "\n\n✨ *Nutricionista bono Fonasa $4.770*\n"
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
            # Marca temporal del entrante: la usa el reintento por saturación
            # para saber si el paciente escribió de nuevo mientras esperaba.
            _ts_entrante = _dt_now_iso()
            # Modo caída Medilink: captura contexto de TODO mensaje entrante
            # mientras esté abierto (incluidos los que quedan en HUMAN_TAKEOVER
            # más abajo) — ver medilink_outage.py.
            try:
                medilink_outage.capturar_mensaje(phone, texto, session)
            except Exception:
                pass
            # Guard HUMAN_TAKEOVER para IG/FB: silenciar respuesta automática
            # y no ejecutar autocapture con texto del operador.
            if state_before == "HUMAN_TAKEOVER":
                log.info("HUMAN_TAKEOVER activo from=%s canal=%s — silenciado", phone, canal)
                return
            # autocapture_profile: solo fuera de HUMAN_TAKEOVER
            try:
                from session import try_autocapture_rut_name
                try_autocapture_rut_name(phone, texto)
            except Exception:
                pass
            try:
                respuesta = await handle_message(phone, texto, session)
            except MedilinkInactiva as e:
                # Plataforma Medilink suspendida (403), no saturada (2026-08-12).
                # NO resetear la sesión: cuando vuelva, el watcher (jobs.py) le
                # escribe con las horas reales de lo que estaba pidiendo.
                log.error("Medilink INACTIVA atendiendo %s from=%s: %s", canal, phone, e)
                log_event(phone, "respuesta_medilink_inactiva", {})
                try:
                    medilink_outage.capturar_mensaje(phone, texto, session, force=True)
                except Exception:
                    pass
                respuesta = (
                    "El sistema de horas está con un problema técnico en este "
                    "momento 😕\n\n"
                    "No perdí tu mensaje: apenas se recupere te escribo con las "
                    "horas disponibles.\n\n"
                    f"Si prefieres, llámanos: 📞 {CMC_TELEFONO}"
                )
            except MedilinkRateLimited as e:
                # Medilink SATURADO, no caído (2026-07-27). NO resetear la sesión:
                # el paciente sigue donde estaba y con un "hola" retoma. Y sobre
                # todo NO ofrecerle lista de espera — su hora probablemente existe,
                # solo no alcanzamos a leerla. Ver medilink._agotado().
                log.warning("Medilink saturado atendiendo %s from=%s: %s", canal, phone, e)
                log_event(phone, "respuesta_medilink_saturado", {})
                # El bot reintenta SOLO en ~45 s. Antes el texto le pedía al
                # paciente que reescribiera y no volvía: caso 56926854672
                # (18-ago) — tocó "Sí, agendar", leyó esto, nunca respondió, y
                # recepción estuvo 10 min tomándole los datos a mano.
                try:
                    import reintento_saturado
                    from resilience import spawn_task
                    spawn_task(
                        reintento_saturado.programar(
                            phone=phone, texto=texto, canal=canal,
                            sender_id=sender_id, send_fn=send_fn,
                            desde_ts=_ts_entrante,
                        ),
                        name=f"reintento_saturado:{phone}",
                    )
                except Exception as _e_re:  # noqa: BLE001
                    log.warning("no se pudo programar reintento por saturación: %s", _e_re)
                respuesta = (
                    "Dame un momento — la agenda está muy pedida y no alcancé "
                    "a leerla 😅\n\n"
                    "*Te escribo con las horas apenas las tenga*, no necesitas "
                    "hacer nada.\n"
                    f"Si prefieres, llámanos: 📞 {CMC_TELEFONO}"
                )
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

    # ── Helper: ¿el perfil aún necesita un nombre real? ─────────────────────
    def _profile_needs_name(phone: str) -> bool:
        """True si NO tenemos un nombre real (no existe, está vacío, o es el
        placeholder ig_/fb_). Un nombre vacío "" cuenta como faltante — antes
        el guard lo trataba como nombre real y nunca reintentaba la captura."""
        p = get_profile(phone)
        if not p:
            return True
        n = (p.get("nombre") or "").strip()
        return (not n) or n.startswith("ig_") or n.startswith("fb_")

    # ── Helper: Page Access Token de la página FB (derivado del system-user) ──
    _FB_PAGE_TOKEN_CACHE = {}
    async def _get_fb_page_token():
        """El User Profile API de Messenger SOLO acepta el Page Access Token de
        la página que recibió el mensaje. META_MESSENGER_TOKEN en .env estaba
        expirado; lo derivamos del system-user token (permanente) vía
        /me/accounts y lo cacheamos en memoria."""
        if _FB_PAGE_TOKEN_CACHE.get("token"):
            return _FB_PAGE_TOKEN_CACHE["token"]
        from config import META_ACCESS_TOKEN, META_PAGE_ID
        if not META_ACCESS_TOKEN:
            return None
        try:
            import httpx
            async with httpx.AsyncClient(timeout=5) as client:
                r = await client.get(
                    "https://graph.facebook.com/v22.0/me/accounts",
                    params={"fields": "id,name,access_token"},
                    headers={"Authorization": f"Bearer {META_ACCESS_TOKEN}"},
                )
            if r.status_code == 200:
                for p in r.json().get("data", []):
                    if not META_PAGE_ID or p.get("id") == META_PAGE_ID:
                        tok = p.get("access_token")
                        if tok:
                            _FB_PAGE_TOKEN_CACHE["token"] = tok
                            return tok
        except Exception as e:
            log.debug("no se pudo derivar page token FB: %s", e)
        return None

    # ── Helper: obtener nombre de usuario IG/FB ─────────────────────────────
    async def _fetch_social_name(sender_id: str, phone: str, platform: str):
        """Obtiene nombre/username de IG o FB via Graph API y lo guarda en contact_profiles."""
        if not _profile_needs_name(phone):
            return  # ya tenemos un nombre real
        from config import META_ACCESS_TOKEN, META_PAGE_ACCESS_TOKEN, META_MESSENGER_TOKEN
        # Instagram (Instagram Login API): el perfil del usuario se consulta en
        # graph.instagram.com con el token de PÁGINA — el mismo host/token que usa
        # el ENVÍO de IG (send_instagram). Pegarle a graph.facebook.com con el
        # system-user token daba 400/401 → el bot no capturaba el nombre (name='').
        # Messenger/FB sí va por graph.facebook.com (system-user y/o page token).
        if platform == "instagram":
            host = "https://graph.instagram.com/v22.0"
            fields = "name,username"
            tokens = [t for t in (META_PAGE_ACCESS_TOKEN, META_ACCESS_TOKEN) if t]
        else:
            host = "https://graph.facebook.com/v22.0"
            fields = "name,first_name,last_name"
            # Messenger: el User Profile API SOLO acepta el Page Access Token de
            # la página. Lo derivamos del system-user (META_MESSENGER_TOKEN en
            # .env estaba expirado → 400). El system-user da #3 "no capability"
            # y el page-token de IG da "cannot parse" en graph.facebook.
            # NOTA: requiere el permiso `pages_user_profile` (Advanced Access)
            # aprobado en la app de Meta; sin él Graph responde 400 igual.
            _fb_page_tok = await _get_fb_page_token()
            tokens = [t for t in (_fb_page_tok, META_MESSENGER_TOKEN,
                                  META_ACCESS_TOKEN) if t]
        try:
            import httpx
            async with httpx.AsyncClient(timeout=5) as client:
                for token in tokens:
                    if not token:
                        continue
                    # Pasar token por Authorization header evita que httpx lo logee
                    # en la URL (seguridad: antes se filtraba en /var/log/cmc-bot.log)
                    r = await client.get(
                        f"{host}/{sender_id}",
                        params={"fields": fields},
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    if r.status_code == 200:
                        info = r.json()
                        if info.get("error"):
                            continue
                        if platform == "instagram":
                            # Preferimos el nombre real ("María A") sobre el
                            # username ("maria.marita.m") — más legible para recepción.
                            nombre = info.get("name") or info.get("username", "")
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
                    # Guardar/recuperar nombre: reintenta aunque ya exista un
                    # perfil con nombre vacío (antes solo capturaba en el 1er msg).
                    if _profile_needs_name(phone):
                        if sender_name:
                            save_profile(phone, (get_profile(phone) or {}).get("rut", "") or "", sender_name)
                        else:
                            await _fetch_social_name(sender_id, phone, "instagram")
                    # Capturar referral Meta (anuncio Click-to-Instagram DM)
                    _ig_referral = ev.get("referral") or {}
                    if not _ig_referral:
                        # IG también puede traerlo en postback.referral
                        _ig_referral = ev.get("postback", {}).get("referral") or {}
                    if _ig_referral:
                        try:
                            from session import save_meta_referral as _smr
                            _smr(phone, _ig_referral, canal="instagram")
                            log.info("META_REFERRAL IG capturado phone=%s headline=%r",
                                     phone, _ig_referral.get("headline", "")[:60])
                        except Exception as _ref_err:
                            log.debug("meta_referral IG error: %s", _ref_err)
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
                    if _profile_needs_name(phone):
                        if sender_name:
                            save_profile(phone, (get_profile(phone) or {}).get("rut", "") or "", sender_name)
                        else:
                            await _fetch_social_name(sender_id, phone, "facebook")
                    # Capturar referral Meta (anuncio Click-to-Messenger)
                    _fb_referral = ev.get("referral") or {}
                    if _fb_referral:
                        try:
                            from session import save_meta_referral as _smr
                            _smr(phone, _fb_referral, canal="messenger")
                            log.info("META_REFERRAL FB capturado phone=%s headline=%r",
                                     phone, _fb_referral.get("headline", "")[:60])
                        except Exception as _ref_err:
                            log.debug("meta_referral FB error: %s", _ref_err)
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
        # Título legible del botón/lista que tocó el paciente (para el panel de
        # recepción). El routing usa el id crudo (`texto`); el log usa el title
        # para que la conversación se lea como la ve el paciente.
        _interactive_title = None

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

        # BUG-B: en algunos dispositivos/versiones de WA, los button payloads
        # llegan como msg_type="text" en vez de "interactive". El set cubre todos
        # los payloads conocidos del bot. Si el texto coincide exactamente, se
        # procesa como si fuera un button_reply — evita "no te entendí" o Claude.
        _BUTTON_PAYLOADS_KNOWN = {
            "menu", "menu_volver", "agendar_sugerido", "ver_otros", "ver_todos",
            "otro_dia", "otro_día", "otro_prof", "confirmar_sugerido",
            "no_gracias_reeng", "accion_recepcion", "accion_cambiar",
            "accion_agendar", "accion_mis_citas", "accion_otro", "accion_waitlist",
            "quick_other", "quick_book", "quick_yes", "quick_no",
            "quick_cancel", "waitlist_si", "waitlist_no", "reac_si", "reac_luego",
            "ped_continuar", "ped_no", "no_pediatra", "no_agendar",
            "menor_confirma_menor", "menor_confirma_adulto",
            "menor_es_adulto", "menor_es_menor",
            "wb_agendar", "wb_info",
            "ig_recepcion", "fb_recepcion", "humano",
            "seg_1", "seg_2", "seg_3", "seg_4", "seg_5",
            "seg_mejor", "seg_igual", "seg_peor",
            "tele_mg", "tele_psico", "tele_nutri", "tele_otro",
            "cita_confirm", "cita_reagendar", "cita_cancelar",
            "ref_amigo", "ref_rrss", "ref_recurrente", "ref_google",
            "maso_20", "maso_40",
            "medfam_fallback_si", "medfam_fallback_no",
            "waitlist_confirmar", "waitlist_cancelar",
            "cat_medico", "cat_dental",
            # motivo_ / accion_ / agendar_prof_ son prefijos — chequear abajo
        }
        # Prefijos de payloads dinámicos (el valor completo varía pero el prefijo es fijo)
        _BUTTON_PAYLOAD_PREFIXES = (
            "motivo_", "accion_", "agendar_prof_", "cat_", "menu_",
            "cita_confirm:", "cita_cancelar:", "cita_reagendar:",
            "slot_", "ref_",
        )

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
            # BUG-B: payload de botón llegó como texto (algunos dispositivos WA)
            _txt_lower = texto.strip().lower()
            _is_known_payload = (
                _txt_lower in _BUTTON_PAYLOADS_KNOWN
                or any(_txt_lower.startswith(p) for p in _BUTTON_PAYLOAD_PREFIXES)
            )
            if _is_known_payload:
                log.info("button_payload_as_text from=%s payload=%r", phone, texto)
                try:
                    from session import log_event as _log_ev_bb
                    _log_ev_bb(phone, "button_payload_as_text", {"payload": texto})
                except Exception:
                    pass
                # Tratar como si viniera del canal interactivo (ya normalizado)
                texto = _txt_lower
        elif msg_type == "interactive":
            interactive = msg.get("interactive", {})
            itype = interactive.get("type", "")
            if itype == "button_reply":
                texto = interactive["button_reply"]["id"]
                _interactive_title = interactive["button_reply"].get("title")
            elif itype == "list_reply":
                texto = interactive["list_reply"]["id"]
                _interactive_title = interactive["list_reply"].get("title")
            else:
                return Response(status_code=200)
        elif msg_type == "button":
            # Quick Reply de template MARKETING/UTILITY (consent_marketing_v1, winback, etc.)
            # Meta envia type=button con button.text y button.payload — distinto de
            # type=interactive (botones de sesion propios del bot).
            _btn_text = msg.get("button", {}).get("text", "")
            _btn_payload = msg.get("button", {}).get("payload", "")
            texto = _btn_text or _btn_payload or ""
            log.info("MSG from=%s id=%s type=button text=%r payload=%r", phone, msg_id, _btn_text, _btn_payload)
            if not texto:
                return Response(status_code=200)
        elif msg_type == "audio":
            media_id = msg.get("audio", {}).get("id", "")
            log.info("AUDIO recibido from=%s media_id=%s — transcribiendo...", phone, media_id)
            media = await download_whatsapp_media(media_id)
            if not media:
                _m1_msg = "No pude descargar tu audio 😕\nIntenta escribir el mensaje o grabar de nuevo."
                await send_whatsapp(phone, _m1_msg)
                log_message(phone, "out", _m1_msg, get_session(phone).get("state", "IDLE"))
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
                _m2_msg = (
                    "Tu audio es muy cortito y no se entiende bien 😅\n"
                    "¿Puedes escribirlo o grabar uno un poco más largo?"
                )
                await send_whatsapp(phone, _m2_msg)
                log_message(phone, "out", _m2_msg, get_session(phone).get("state", "IDLE"))
                return Response(status_code=200)
            transcripcion = await transcribe_audio(audio_bytes, mime)
            if not transcripcion:
                _m3_msg = "No logré entender el audio 😕\n¿Puedes escribirlo o grabarlo de nuevo un poco más claro?"
                await send_whatsapp(phone, _m3_msg)
                log_message(phone, "out", _m3_msg, get_session(phone).get("state", "IDLE"))
                return Response(status_code=200)
            texto = transcripcion
            is_audio = True
            log.info("AUDIO transcrito from=%s text=%r", phone, texto[:120])
        elif msg_type == "reaction":
            # Reacciones (emoji a un mensaje) — ignorar silenciosamente
            return Response(status_code=200)
        elif msg_type == "image" and ASISTENTE_EXAMENES_ENABLED and phone in STAFF_PHONES:
            # MODALIDAD ASISTENTE: un número de staff manda la foto de un examen →
            # la transcribimos y le devolvemos el texto para pegar en la ficha de
            # Medilink. Solo staff (gate arriba); el paciente nunca entra acá.
            media_id = msg.get("image", {}).get("id", "")
            log.info("ASISTENTE_EXAMEN foto staff from=%s media_id=%s", phone, media_id)
            log_message(phone, "in", "[examen: foto staff]",
                        get_session(phone).get("state", "IDLE"), canal="whatsapp")
            media = await download_whatsapp_media(media_id)
            if not media:
                _ex_err = "No pude descargar la foto del examen 😕 Reenvíala, porfa."
                await send_whatsapp(phone, _ex_err)
                log_message(phone, "out", _ex_err, "ASISTENTE", canal="whatsapp")
                return Response(status_code=200)
            _img_bytes, _img_mime = media
            from examenes_transcribe import transcribir_examen
            _texto_examen = await transcribir_examen(_img_bytes, _img_mime)
            if not _texto_examen:
                _ex_fail = ("No logré leer el examen 😕 Intenta una foto más nítida, "
                            "derecha y con buena luz.")
                await send_whatsapp(phone, _ex_fail)
                log_message(phone, "out", _ex_fail, "ASISTENTE", canal="whatsapp")
                return Response(status_code=200)
            _ex_out = ("📋 *Transcripción del examen* — revísala antes de guardar en la ficha:\n\n"
                       + _texto_examen)
            await send_whatsapp(phone, _ex_out)
            log_message(phone, "out", _ex_out, "ASISTENTE", canal="whatsapp")
            try:
                log_event(phone, "asistente_examen_transcrito", {"chars": len(_texto_examen)})
            except Exception:
                pass
            return Response(status_code=200)
        elif msg_type in ("image", "video", "document"):
            # ── Abono-Gate: capturar imagen de comprobante ─────────────────
            # Si el paciente está esperando enviar un comprobante de abono
            # (estado WAIT_ABONO_COMPROBANTE) y manda una imagen, la procesamos
            # con Claude vision ANTES del pipeline normal de media (que la
            # enviaría a HUMAN_TAKEOVER y perdería el contexto del gate).
            if msg_type == "image":
                _ag_sess = get_session(phone)
                _ag_gate = (_ag_sess or {}).get("state") == "WAIT_ABONO_COMPROBANTE"
                if not _ag_gate:
                    # La sesión pudo vencer mientras el paciente iba al banco.
                    # El hecho que manda es que tenga un abono PENDIENTE, no en
                    # qué estado conversacional quedó. Sin esto, su comprobante
                    # cae al pipeline genérico y se archiva como documento
                    # clínico en su ficha (pasó de verdad el 29-jul).
                    try:
                        from abono_transferencia import get_abono_pendiente_activo_por_phone
                        _ag_ap = get_abono_pendiente_activo_por_phone(phone)
                        _ag_gate = bool(_ag_ap and _ag_ap.get("estado") == "pendiente")
                    except Exception as _ag_e:
                        log.warning("abono-gate: no se pudo consultar abono vivo: %s", _ag_e)
                if _ag_gate:
                    _ag_media_id = msg.get("image", {}).get("id", "")
                    _ag_caption  = msg.get("image", {}).get("caption", "")
                    log_text_ag  = "[imagen comprobante abono]" + (f" {_ag_caption}" if _ag_caption else "")
                    log_message(phone, "in", log_text_ag, "WAIT_ABONO_COMPROBANTE", canal="whatsapp")
                    _ag_blob = None
                    _ag_mime = "image/jpeg"
                    if _ag_media_id:
                        try:
                            _ag_res = await download_whatsapp_media(_ag_media_id)
                            if _ag_res:
                                _ag_blob, _ag_mime = _ag_res
                        except Exception as _ag_dl_err:
                            log.warning("abono-gate: error descargando imagen: %s", _ag_dl_err)
                    # Guardar el comprobante en la ficha ANTES de intentar
                    # leerlo. Si la visión falla, si Medilink está caído o si el
                    # handler devuelve vacío, la imagen ya está guardada y
                    # recepción puede verificar el pago a mano. Antes se
                    # procesaba en memoria y se descartaba: cuando algo fallaba
                    # no quedaba NADA — ni para verificar ni para reintentar
                    # (pasó el 29-jul con un paciente y no se pudo reproducir).
                    if _ag_blob:
                        try:
                            # OJO: save_patient_file NO está importado a nivel de
                            # módulo; el pipeline genérico lo importa localmente
                            # más abajo (línea ~9800). Sin este import acá esto
                            # revienta con NameError en el primer comprobante.
                            from session import save_patient_file
                            _ab_dir = Path(__file__).parent.parent / "data" / "uploads" / phone
                            _ab_dir.mkdir(parents=True, exist_ok=True)
                            _ab_ext = {"image/jpeg": ".jpg", "image/png": ".png",
                                       "image/webp": ".webp"}.get(_ag_mime, ".jpg")
                            from datetime import datetime as _dt_ab
                            from zoneinfo import ZoneInfo as _ZI_ab
                            _ab_ts = _dt_ab.now(_ZI_ab("America/Santiago")).strftime("%Y%m%d_%H%M%S")
                            _ab_name = f"comprobante_abono_{_ab_ts}{_ab_ext}"
                            (_ab_dir / _ab_name).write_bytes(_ag_blob)
                            save_patient_file(
                                phone, _ab_name, "image", _ag_mime,
                                f"data/uploads/{phone}/{_ab_name}", len(_ag_blob),
                                (_ag_caption or "Comprobante de abono Psiquiatría")[:200])
                            log.info("ABONO comprobante guardado from=%s file=%s size=%d",
                                     phone, _ab_name, len(_ag_blob))
                            log_event(phone, "abono_comprobante_guardado",
                                      {"archivo": _ab_name, "bytes": len(_ag_blob)})
                        except Exception as _ab_e:
                            log.error("ABONO: no se pudo guardar el comprobante de %s: %s", phone, _ab_e)

                    if _ag_blob:
                        from flows import procesar_imagen_abono
                        # OJO: get_phone_lock se importa LOCAL más abajo en esta
                        # misma función (webhook) → Python lo marca variable
                        # local para TODO el scope y acá aún no existe. Sin este
                        # import: UnboundLocalError → 500 en CADA comprobante
                        # (el camino de visión nunca corrió; caso Bryan 04-08).
                        from resilience import get_phone_lock
                        try:
                            async with get_phone_lock(phone):
                                _ag_respuesta = await procesar_imagen_abono(phone, _ag_blob, _ag_mime)
                        except Exception as _ag_proc_e:
                            # El comprobante ya quedó guardado arriba; una falla
                            # acá jamás debe responder 500 (Meta reintenta y el
                            # dedupe se traga el retry → paciente sin respuesta).
                            log.exception("ABONO: procesar_imagen_abono lanzó para %s: %s",
                                          phone, _ag_proc_e)
                            _ag_respuesta = ""
                        if not _ag_respuesta:
                            # Falla silenciosa: el handler devolvió vacío y el
                            # paciente quedaba sin respuesta y sin cita, creyendo
                            # que pagó y quedó. Ahora se le contesta y se avisa.
                            log.error("ABONO: procesar_imagen_abono devolvió vacío para %s", phone)
                            log_event(phone, "abono_comprobante_sin_respuesta", {})
                            _ag_respuesta = (
                                "Recibí tu comprobante ✅ y quedó guardado.\n\n"
                                "No pude confirmarlo automáticamente, así que recepción "
                                "lo va a revisar y te confirma la hora a la brevedad.")
                        if _ag_respuesta:
                            await send_whatsapp(phone, _ag_respuesta)
                            log_message(phone, "out", _ag_respuesta,
                                        get_session(phone).get("state", "IDLE"), canal="whatsapp")
                    else:
                        _ag_err = "No pude descargar la imagen. Reenvíala, por favor."
                        await send_whatsapp(phone, _ag_err)
                        log_message(phone, "out", _ag_err, "WAIT_ABONO_COMPROBANTE", canal="whatsapp")
                    return Response(status_code=200)
            # ── fin abono-gate imagen ──────────────────────────────────────

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
                    # OJO: clasificar sobre el texto COMPLETO, no sobre el
                    # truncado. Las señales de un examen —"Toma Muestra", el
                    # nombre del laboratorio, los valores de referencia— casi
                    # nunca caben en los primeros 200 caracteres: el encabezado
                    # se los come. Antes se clasificaba después de truncar y por
                    # eso un examen de laboratorio pasaba de largo.
                    _texto_full = extracted

                    # ── ¿es un examen de laboratorio? ──────────────────────
                    from examenes_lab import parece_examen, nombre_en_examen
                    _es_examen, _senales = parece_examen(_texto_full)
                    if _es_examen:
                        _nom_pac = nombre_en_examen(_texto_full)
                        log.info("Examen de laboratorio detectado from=%s señales=%s", phone, _senales)
                        state_before = get_session(phone).get("state", "IDLE")
                        log_message(phone, "in", f"[{msg_type}:{saved_filename}]",
                                    state_before, canal="whatsapp")
                        from session import log_event as _le_ex, save_session as _ss_ex
                        _le_ex(phone, "examen_recibido", {
                            "filename": saved_filename[:120], "senales": _senales[:6],
                            "paciente_en_examen": (_nom_pac or "")[:80],
                        })
                        # Se deriva a recepción para que nadie quede esperando
                        # respuesta del bot sobre un resultado clínico.
                        _ss_ex(phone, "HUMAN_TAKEOVER", {
                            "hold_sent": True, "handoff_reason": "examen_recibido",
                        })
                        _ex_resp = (
                            "Recibí tu examen 🧪 y quedó guardado en tu ficha.\n\n"
                            "Lo va a revisar el profesional que te atiende. Si necesitas "
                            "una hora para verlo, escribe *menu* y te ayudo a agendarla."
                        )
                        await send_whatsapp(phone, _ex_resp)
                        log_message(phone, "out", _ex_resp, "HUMAN_TAKEOVER", canal="whatsapp")
                        # Aviso a recepción con lo mínimo para actuar.
                        # El aviso va dentro de try/except a propósito: si la
                        # ventana de 24 h del número admin está cerrada, este
                        # send falla — y eso no puede tumbar el webhook ni
                        # perder el examen que ya quedó guardado. Misma lección
                        # que el bug de visión del 2026-08-04.
                        if ADMIN_ALERT_PHONE:
                            _av = (f"🧪 *Examen recibido por el bot*\n"
                                   f"Paciente: {_nom_pac or 'sin nombre en el documento'}\n"
                                   f"WhatsApp: {phone}\n"
                                   f"Archivo: {saved_filename or 'sin nombre'}\n"
                                   f"Está en su ficha. Falta que lo vea el profesional.")
                            try:
                                await send_whatsapp(ADMIN_ALERT_PHONE, _av)
                                log_message(ADMIN_ALERT_PHONE, "out", _av,
                                            "HUMAN_TAKEOVER", canal="whatsapp")
                            except Exception as _e_av:
                                log.warning("Aviso de examen no enviado a admin: %s", _e_av)
                        return Response(status_code=200)

                    # Truncar a 200 chars antes de pasar al pipeline (Bug-3)
                    if len(extracted) > 200:
                        extracted = extracted[:200] + "…"
                    # Detectar documentos clínicos: ficha, consentimiento, formulario, entrevista
                    # (se mantiene sobre el texto YA truncado, como estaba: sus
                    # keywords son genéricas —"formulario" aparece en cualquier
                    # PDF— y ampliarle el alcance dispararía takeovers de más.)
                    _CLINICAL_DOC_KEYS = ("ficha", "entrevista psicol", "formulario", "consentimiento")
                    _extracted_lower = extracted.lower()
                    _es_doc_clinico = any(k in _extracted_lower for k in _CLINICAL_DOC_KEYS)
                    if _es_doc_clinico:
                        log.info("Documento clínico detectado from=%s, derivando a humano", phone)
                        state_before = get_session(phone).get("state", "IDLE")
                        log_text = f"[{msg_type}:{saved_filename}]"
                        log_message(phone, "in", log_text, state_before, canal="whatsapp")
                        from session import log_event as _le_doc
                        _le_doc(phone, "documento_clinico_recibido", {"filename": saved_filename[:120]})
                        from session import save_session as _ss_doc
                        _ss_doc(phone, "HUMAN_TAKEOVER", {
                            "hold_sent": True,
                            "handoff_reason": "documento_clinico_recibido",
                        })
                        _doc_resp = (
                            "Recibí tu documento. Lo dejé guardado para que una recepcionista "
                            "lo revise y te contacte. Si es urgente, llama al "
                            f"*{CMC_TELEFONO_FIJO}*."
                        )
                        await send_whatsapp(phone, _doc_resp)
                        log_message(phone, "out", _doc_resp, "HUMAN_TAKEOVER", canal="whatsapp")
                        return Response(status_code=200)
                    # Documento sin clasificar (ni examen, ni ficha/consentimiento/
                    # formulario conocido): puede ser CUALQUIER COSA — liquidación de
                    # sueldo, cédula, contrato, boleta de otro rubro. Antes se citaba
                    # un preview del texto extraído de vuelta al paciente ("Esto es lo
                    # que dice: ...") Y se inyectaba el documento completo al pipeline
                    # de agendamiento (detect_intent la procesaba como si fuera un
                    # mensaje del paciente). Caso real: liquidación de sueldo con razón
                    # social, RUT de la empresa y RUT del trabajador transcrita en el
                    # chat (auditoría 2026-08-19, #11). Nunca transcribir ni interpretar
                    # el contenido — mismo patrón que el documento clínico de arriba.
                    log.info("Documento sin clasificar from=%s (%d chars extraídos, no se transcribe)",
                             phone, len(extracted))
                    state_before = get_session(phone).get("state", "IDLE")
                    log_text = f"[{msg_type}:{saved_filename}]"
                    log_message(phone, "in", log_text, state_before, canal="whatsapp")
                    from session import log_event as _le_doc2, save_session as _ss_doc2
                    _le_doc2(phone, "documento_sin_clasificar_recibido", {"filename": saved_filename[:120]})
                    _ss_doc2(phone, "HUMAN_TAKEOVER", {
                        "hold_sent": True,
                        "handoff_reason": "documento_sin_clasificar",
                    })
                    confirm_msg = "Recibí tu documento, una recepcionista lo revisará."
                    await send_whatsapp(phone, confirm_msg)
                    log_message(phone, "out", confirm_msg, "HUMAN_TAKEOVER", canal="whatsapp")
                    return Response(status_code=200)

            # Imágenes y otros → guardar + derivar a recepción (sin extracción)
            log_text = f"[{msg_type}]" + (f" {caption}" if caption else "")
            if saved_filename:
                log_text = f"[{msg_type}:{saved_filename}]" + (f" {caption}" if caption and caption != saved_filename else "")
            _sess_before_media = get_session(phone)
            state_before = _sess_before_media.get("state", "IDLE")
            # ¿Estaba el bot esperando el tipo de ecografía? En Arauco la
            # respuesta natural es mandar la FOTO de la orden médica (corpus
            # 2026-08-01). Conservar ese contexto para recepción en vez de
            # perderlo en el takeover genérico.
            _data_before_media = _sess_before_media.get("data") or {}
            if isinstance(_data_before_media, str):
                import json as _json_media
                try:
                    _data_before_media = _json_media.loads(_data_before_media)
                except Exception:
                    _data_before_media = {}
            _media_es_orden_eco = bool(_data_before_media.get("wait_eco_tipo")) \
                and msg_type in ("image", "document")
            # Logging de entrada SIEMPRE: la recepcionista debe ver que llegó
            # una imagen/documento independiente del estado de la sesión.
            log_message(phone, "in", log_text, state_before, canal="whatsapp")

            # ── Guard HUMAN_TAKEOVER para media ────────────────────────────
            # Si ya hay un operador humano atendiendo, NO enviar respuesta
            # automática ni sobrescribir el estado de sesión. El log de entrada
            # ya se registró arriba para que la recepcionista lo vea en el panel.
            if state_before == "HUMAN_TAKEOVER":
                log.info("HUMAN_TAKEOVER activo from=%s type=%s (media) — respuesta automática suprimida",
                         phone, msg_type)
                return Response(status_code=200)
            # ── fin guard media HUMAN_TAKEOVER ─────────────────────────────

            # ── OCR de imágenes entrantes (gated ECO_ORDEN_OCR_ACTIVE) ─────
            # GATILLO AMPLIADO (2026-08-01): TODA imagen entrante se clasifica
            # con Claude visión (antes: solo con wait_eco_tipo activo, y en la
            # práctica no leía casi ninguna — las fotos llegan ANTES de que el
            # bot pregunte). Orden de eco única → ofrecer la hora (el paciente
            # siempre confirma). Obstétrica → responder que no se realiza.
            # Todo lo demás (comprobantes, órdenes no-eco, memes, ilegible) →
            # flujo actual de recepción, con handoff_reason enriquecido.
            # Validado con 19 imágenes reales: 0 falsos positivos.
            # Costo medido: ~$0.007 USD/imagen · ~400 img/mes ≈ $3 USD/mes.
            _ocr_tipo_doc = None
            if msg_type == "image" and blob:
                from config import ECO_ORDEN_OCR_ACTIVE
                if ECO_ORDEN_OCR_ACTIVE:
                    try:
                        from eco_orden_ocr import (leer_orden_medica, decidir_accion,
                                                   msg_oferta, MSG_OBSTETRICA)
                        # Dedupe: si hace <3 min ya ofrecimos agenda por otra
                        # foto (paciente manda la misma orden 2 veces, caso real
                        # ...3079), no re-ofrecer ni pisar la oferta pendiente.
                        import time as _t_ocr
                        try:
                            _prev_oferta_ts = float(
                                _data_before_media.get("_ocr_oferta_ts") or 0)
                        except Exception:  # noqa: BLE001
                            _prev_oferta_ts = 0
                        if _t_ocr.time() - _prev_oferta_ts < 180:
                            log_event(phone, "eco_orden_ocr", {
                                "decision": "skip_oferta_reciente",
                                "filename": saved_filename,
                            })
                            return Response(status_code=200)
                        _ocr_ext = await leer_orden_medica(blob, mime)
                        _ocr_dec = decidir_accion(_ocr_ext)
                        _ocr_tipo_doc = (_ocr_ext or {}).get("tipo_documento")
                        # Identidad del paciente leída de la orden (si trae).
                        # El RUT solo se arrastra si valida módulo 11 — un RUT
                        # mal leído por visión NUNCA entra al flujo.
                        _ocr_identidad = {}
                        _pac_ocr = (_ocr_ext or {}).get("paciente") or {}
                        if _pac_ocr.get("nombre") or _pac_ocr.get("rut"):
                            from eco_orden_ocr import rut_normalizado as _rutn_fn
                            import time as _t_ident
                            _rutn = _rutn_fn(_pac_ocr.get("rut") or "")
                            _nom_ocr = (_pac_ocr.get("nombre") or "").strip()[:80]
                            # Conciliar contra los nombres CONOCIDOS del número:
                            # la letra manuscrita produce lecturas imperfectas
                            # ("Anyie Ruby" por "Anguie Rondoy", caso real) — si
                            # se parece a alguien conocido, usar el nombre bueno;
                            # si no, probablemente es la orden de un tercero.
                            if _nom_ocr:
                                try:
                                    from docs_clinicos import nombre_mas_probable
                                    _nom_fin, _nom_fuente = nombre_mas_probable(
                                        phone, _nom_ocr)
                                    if _nom_fuente == "conocido" and _nom_fin != _nom_ocr:
                                        log_event(phone, "ocr_nombre_conciliado", {
                                            "leido": _nom_ocr[:60],
                                            "conocido": _nom_fin[:60]})
                                    _nom_ocr = _nom_fin
                                except Exception:  # noqa: BLE001
                                    pass
                            _ocr_identidad = {
                                "ocr_paciente": {
                                    "nombre": _nom_ocr,
                                    "rut": _rutn or "",
                                    "fecha_nacimiento":
                                        (_pac_ocr.get("fecha_nacimiento") or "").strip()[:12],
                                    "sexo": (_pac_ocr.get("sexo") or "").strip().upper()[:1],
                                },
                                "ocr_ident_ts": _t_ident.time(),
                            }
                        log_event(phone, "eco_orden_ocr", {
                            "decision": _ocr_dec.get("accion"),
                            "motivo": _ocr_dec.get("motivo", ""),
                            "tipo_documento": _ocr_tipo_doc or "",
                            "examenes": (_ocr_ext or {}).get("examenes_solicitados", [])[:4],
                            "confianza": (_ocr_ext or {}).get("confianza", ""),
                            "filename": saved_filename,
                        })
                        # ── Docs clínicos (gated DOCS_CLINICOS_ACTIVE) ─────
                        # dx escrito → tags crónicos · exámenes que no hacemos
                        # → demanda estructurada · resultado/receta → oferta de
                        # control. El bot JAMÁS interpreta valores clínicos.
                        from config import DOCS_CLINICOS_ACTIVE
                        if (DOCS_CLINICOS_ACTIVE and _ocr_tipo_doc in
                                ("orden_medica", "receta_medicamentos",
                                 "resultado_examen")):
                            try:
                                from docs_clinicos import (
                                    detectar_dx_tags, clasificar_examen_externo,
                                    registrar_receta, registrar_demanda_examen)
                                from session import save_tag as _save_tag_dc
                                _exs_dc = (_ocr_ext or {}).get(
                                    "examenes_solicitados") or []
                                _meds_dc = (_ocr_ext or {}).get("medicamentos") or []
                                _texto_dc = " ".join(
                                    [str(x) for x in list(_exs_dc) + list(_meds_dc)]
                                    + [(_ocr_ext or {}).get("diagnostico") or ""])
                                _tags_dc = detectar_dx_tags(_texto_dc)
                                for _t_dc in _tags_dc:
                                    _save_tag_dc(phone, f"dx:{_t_dc}")
                                if _tags_dc:
                                    log_event(phone, "ocr_dx_tags",
                                              {"tags": _tags_dc})
                                if _ocr_tipo_doc == "orden_medica":
                                    for _ex_dc in _exs_dc:
                                        _cat_dc = clasificar_examen_externo(str(_ex_dc))
                                        if _cat_dc:
                                            registrar_demanda_examen(
                                                phone, str(_ex_dc), _cat_dc)
                                            log_event(phone, "demanda_examen_externo",
                                                      {"examen": str(_ex_dc)[:100],
                                                       "categoria": _cat_dc})
                                _ofrecer_control_dc = None
                                if _ocr_tipo_doc == "resultado_examen":
                                    _titulo_dc = ((_ocr_ext or {}).get("titulo_examen")
                                                  or "tu examen")
                                    # ── Reenvío al MÉDICO (Telegram): foto +
                                    # transcripción completa lista para copiar
                                    # a la ficha. Canal profesional — el
                                    # paciente NUNCA recibe estos valores.
                                    try:
                                        _blob_dr = blob
                                        _cont_dr = ((_ocr_ext or {}).get(
                                            "contenido_texto") or "").strip()
                                        _nom_dr = ((_ocr_identidad.get("ocr_paciente")
                                                    or {}).get("nombre") or "")
                                        if not _nom_dr:
                                            try:
                                                from session import get_profile as _gp_dr
                                                _p_dr = _gp_dr(phone)
                                                _nom_dr = (_p_dr or {}).get("nombre", "")
                                            except Exception:  # noqa: BLE001
                                                pass
                                        _rut_dr = ((_ocr_identidad.get("ocr_paciente")
                                                    or {}).get("rut") or "")
                                        _cab_dr = (
                                            f"📄 Resultado recibido por WhatsApp\n"
                                            f"👤 {_nom_dr or '(sin nombre)'}"
                                            f"{' · ' + _rut_dr if _rut_dr else ''}"
                                            f" · +{phone}\n"
                                            f"🧪 {_titulo_dc[:80]}"
                                        )
                                        _txt_dr = (
                                            _cab_dr + "\n\n" + (_cont_dr or
                                            "(sin transcripción — ver foto)") +
                                            "\n\n_Transcripción automática para "
                                            "copiar — verificar contra la imagen._"
                                        )
                                        from alertas_oob import (
                                            enviar_telegram, enviar_telegram_foto)
                                        _edad_dr = ((_ocr_identidad.get("ocr_paciente")
                                                     or {}).get("fecha_nacimiento") or "")
                                        _sexo_dr = ((_ocr_identidad.get("ocr_paciente")
                                                     or {}).get("sexo") or "")

                                        async def _enviar_dr(b=_blob_dr, t=_txt_dr,
                                                             c=_cab_dr,
                                                             ti=_titulo_dc,
                                                             co=_cont_dr,
                                                             ed=_edad_dr,
                                                             sx=_sexo_dr):
                                            await enviar_telegram_foto(b, c)
                                            await enviar_telegram(t)
                                            # Bloque GES para el médico (2ª
                                            # llamada, solo canal profesional)
                                            try:
                                                from docs_clinicos import sugerencias_ges
                                                _ges = await sugerencias_ges(
                                                    ti, co, ed, sx)
                                                if _ges:
                                                    await enviar_telegram(_ges)
                                            except Exception:  # noqa: BLE001
                                                pass

                                        import asyncio as _aio_dr
                                        _aio_dr.create_task(_enviar_dr())
                                        log_event(phone, "resultado_enviado_doctor",
                                                  {"titulo": _titulo_dc[:80],
                                                   "chars": len(_cont_dr)})
                                        # Persistir para la pre-carga al
                                        # Copiloto 15 min antes de la cita
                                        # (ver app/copiloto_bridge.py).
                                        from docs_clinicos import registrar_resultado
                                        registrar_resultado(
                                            phone, _nom_dr, _rut_dr,
                                            _titulo_dc, _cont_dr, saved_filename)
                                    except Exception as _e_dr:  # noqa: BLE001
                                        log.warning("reenvio doctor fallo: %s",
                                                    str(_e_dr)[:150])
                                    _ofrecer_control_dc = (
                                        f"Recibí tu resultado 📄 (*{_titulo_dc[:60]}*). "
                                        "Quedó guardado en tu ficha.\n\n"
                                        "Para que el médico lo revise contigo, "
                                        "¿te agendo una hora de control?"
                                    )
                                elif _ocr_tipo_doc == "receta_medicamentos":
                                    registrar_receta(phone, list(_meds_dc),
                                                     _tags_dc, saved_filename)
                                    _ofrecer_control_dc = (
                                        "Recibí tu receta 💊 Quedó guardada en "
                                        "tu ficha.\n\n"
                                        "Las recetas de tratamiento permanente "
                                        "suelen durar 30 días — ¿te agendo un "
                                        "control médico para renovarla a tiempo?"
                                    )
                                if _ofrecer_control_dc:
                                    from datetime import datetime as _dt_dc, \
                                        timezone as _tz_dc
                                    save_session(phone, "IDLE", {
                                        "especialidad_sugerida": "medicina general",
                                        "especialidad_sugerida_ts":
                                            _dt_dc.now(_tz_dc.utc).isoformat(),
                                        **_ocr_identidad,
                                    })
                                    _msg_dc = {
                                        "type": "interactive",
                                        "interactive": {
                                            "type": "button",
                                            "body": {"text": _ofrecer_control_dc},
                                            "action": {"buttons": [
                                                {"type": "reply", "reply": {
                                                    "id": "agendar_sugerido",
                                                    "title": "✅ Agendar control"}},
                                                {"type": "reply", "reply": {
                                                    "id": "no_agendar",
                                                    "title": "Por ahora no"}},
                                            ]},
                                        },
                                    }
                                    await send_whatsapp(phone, _msg_dc)
                                    log_message(phone, "out", _ofrecer_control_dc,
                                                "IDLE", canal="whatsapp")
                                    log_event(phone, "docs_clinicos_oferta_control",
                                              {"tipo": _ocr_tipo_doc})
                                    return Response(status_code=200)
                            except Exception as _e_dc:  # noqa: BLE001
                                log.warning("docs_clinicos fallo from=%s: %s",
                                            phone, str(_e_dc)[:200])
                        # Orden de kine con N sesiones → agendar DIRECTO la
                        # primera sesión (cero fricción — decisión del dueño
                        # 2026-08-01); la oferta de la serie completa llega
                        # como 2º mensaje DESPUÉS de confirmarse esa cita.
                        # serie_kine_max viaja en data hasta el hook de flows.
                        if (_ocr_tipo_doc == "orden_medica"
                                and _ocr_dec.get("accion") == "recepcion"):
                            from config import SERIE_KINE_ACTIVE
                            if SERIE_KINE_ACTIVE:
                                from serie_kine import detectar_sesiones_kine
                                _sk_det = detectar_sesiones_kine(
                                    (_ocr_ext or {}).get("examenes_solicitados") or [])
                                if _sk_det:
                                    _sk_carry = {
                                        "serie_kine_max": _sk_det["n"],
                                        "serie_kine_texto": _sk_det["texto"][:120],
                                        **_ocr_identidad,
                                    }
                                    try:
                                        from session import get_profile as _gp_sk
                                        _perf_sk = _gp_sk(phone)
                                        if _perf_sk:
                                            # Fallback: la identidad de la ORDEN
                                            # (si trae RUT válido) pisa a este
                                            # perfil en _iniciar_agendar.
                                            _sk_carry.setdefault(
                                                "rut_conocido", _perf_sk.get("rut", ""))
                                            _sk_carry.setdefault(
                                                "nombre_conocido", _perf_sk.get("nombre", ""))
                                    except Exception:  # noqa: BLE001
                                        pass
                                    _nom_ocr_sk = (_ocr_identidad.get("ocr_paciente")
                                                   or {}).get("nombre", "")
                                    _intro_sk = (
                                        f"Leí tu orden 📄: *{_sk_det['texto'][:90]}*"
                                        + (f"\n👤 A nombre de *{_nom_ocr_sk}*"
                                           if _nom_ocr_sk else "")
                                        + "\n\nTe busco la primera hora disponible "
                                          "de kinesiología 👇"
                                    )
                                    await send_whatsapp(phone, _intro_sk)
                                    log_message(phone, "out", _intro_sk, "IDLE",
                                                canal="whatsapp")
                                    from flows import _iniciar_agendar as _ini_ag_sk
                                    _resp_sk = await _ini_ag_sk(
                                        phone, _sk_carry, "kinesiología")
                                    if isinstance(_resp_sk, dict):
                                        await send_whatsapp_interactive(
                                            phone, _resp_sk["interactive"])
                                        _log_sk = _interactive_to_text(
                                            _resp_sk, include_promo=False)
                                    else:
                                        await send_whatsapp(phone, _resp_sk)
                                        _log_sk = _resp_sk or ""
                                    log_message(phone, "out", _log_sk,
                                                get_session(phone).get("state", "IDLE"),
                                                canal="whatsapp")
                                    log_event(phone, "serie_kine_detectada", {
                                        "n": _sk_det["n"],
                                        "texto": _sk_det["texto"][:120]})
                                    return Response(status_code=200)
                        # Comprobante de transferencia → encolar en el panel
                        # de pagos con validaciones pre-cruzadas (gated).
                        if _ocr_tipo_doc == "comprobante_pago":
                            from config import COMPROBANTES_WHATSAPP_ACTIVE
                            if COMPROBANTES_WHATSAPP_ACTIVE:
                                try:
                                    from comprobantes_pagos import registrar_comprobante
                                    _fila_comp = registrar_comprobante(
                                        phone,
                                        (_ocr_ext or {}).get("comprobante") or {},
                                        saved_filename,
                                        confianza=(_ocr_ext or {}).get("confianza", ""),
                                    )
                                    log_event(phone, "comprobante_whatsapp_encolado",
                                              _fila_comp)
                                except Exception as _e_comp:  # noqa: BLE001
                                    log.warning("comprobante encolar fallo from=%s: %s",
                                                phone, str(_e_comp)[:200])
                        # ── Taxonomía ampliada (cédula / foto clínica /
                        # captura de cita) — gated DOCS_CLINICOS_ACTIVE ─────
                        from config import DOCS_CLINICOS_ACTIVE as _DC_ACT_TAX
                        if _DC_ACT_TAX and _ocr_tipo_doc == "cedula_identidad":
                            # Identidad ya validada módulo 11 en _ocr_identidad.
                            # Guardarla (TTL 15 min) y ofrecer agendar — el
                            # botón cae al flujo normal, que prellenará RUT.
                            save_session(phone, "IDLE", {**_ocr_identidad})
                            _nom_ced = (_ocr_identidad.get("ocr_paciente")
                                        or {}).get("nombre", "")
                            _msg_ced = {
                                "type": "interactive",
                                "interactive": {
                                    "type": "button",
                                    "body": {"text": (
                                        "Recibí tu cédula ✅"
                                        + (f" Gracias, *{_nom_ced.split()[0]}*."
                                           if _nom_ced else "")
                                        + "\n\nGuardé tus datos para agendarte "
                                          "más rápido. ¿Te busco una hora?"
                                    )},
                                    "action": {"buttons": [
                                        {"type": "reply", "reply": {
                                            "id": "agendar_sugerido",
                                            "title": "✅ Agendar hora"}},
                                        {"type": "reply", "reply": {
                                            "id": "no_agendar",
                                            "title": "Por ahora no"}},
                                    ]},
                                },
                            }
                            await send_whatsapp(phone, _msg_ced)
                            log_message(phone, "out",
                                        _msg_ced["interactive"]["body"]["text"],
                                        "IDLE", canal="whatsapp")
                            log_event(phone, "cedula_recibida_prefill", {})
                            return Response(status_code=200)
                        if _DC_ACT_TAX and _ocr_tipo_doc == "foto_clinica":
                            # REGLA DURA: el bot JAMÁS comenta lo que se ve en
                            # la foto — solo enruta a evaluación profesional.
                            # SIN takeover: el bot sigue disponible.
                            from datetime import datetime as _dt_fc, \
                                timezone as _tz_fc
                            save_session(phone, "IDLE", {
                                "especialidad_sugerida": "medicina general",
                                "especialidad_sugerida_ts":
                                    _dt_fc.now(_tz_fc.utc).isoformat(),
                                **_ocr_identidad,
                            })
                            _msg_fc = {
                                "type": "interactive",
                                "interactive": {
                                    "type": "button",
                                    "body": {"text": (
                                        "Recibí tu foto 📷 Quedó guardada en tu "
                                        "ficha.\n\nNo puedo evaluar imágenes — "
                                        "eso debe hacerlo un profesional en "
                                        "consulta. ¿Te agendo una hora de "
                                        "*Medicina General* para que la revisen?"
                                        "\n\n⚠️ Si es una urgencia, llama al "
                                        "*SAMU 131*.\n_Si prefieres hablar con "
                                        "una persona, escribe *recepción*._"
                                    )},
                                    "action": {"buttons": [
                                        {"type": "reply", "reply": {
                                            "id": "agendar_sugerido",
                                            "title": "✅ Sí, agendar"}},
                                        {"type": "reply", "reply": {
                                            "id": "no_agendar",
                                            "title": "Por ahora no"}},
                                    ]},
                                },
                            }
                            await send_whatsapp(phone, _msg_fc)
                            log_message(phone, "out",
                                        _msg_fc["interactive"]["body"]["text"],
                                        "IDLE", canal="whatsapp")
                            log_event(phone, "foto_clinica_recibida", {})
                            return Response(status_code=200)
                        if _DC_ACT_TAX and _ocr_tipo_doc == "captura_cita":
                            # Pantallazo de una cita → responder con sus
                            # reservas REALES (flujo ver-reservas existente,
                            # que prellena el RUT si lo conocemos).
                            _intro_cc = ("Veo que es una captura de una cita 🗓️ "
                                         "Te reviso tus reservas 👇")
                            await send_whatsapp(phone, _intro_cc)
                            log_message(phone, "out", _intro_cc, "IDLE",
                                        canal="whatsapp")
                            from flows import _iniciar_ver as _ini_ver_cc
                            _resp_cc = await _ini_ver_cc(phone, {})
                            if isinstance(_resp_cc, dict):
                                await send_whatsapp_interactive(
                                    phone, _resp_cc["interactive"])
                                _log_cc = _interactive_to_text(
                                    _resp_cc, include_promo=False)
                            else:
                                await send_whatsapp(phone, _resp_cc)
                                _log_cc = _resp_cc or ""
                            log_message(phone, "out", _log_cc,
                                        get_session(phone).get("state", "IDLE"),
                                        canal="whatsapp")
                            log_event(phone, "captura_cita_recibida", {})
                            return Response(status_code=200)
                        if _ocr_dec["accion"] == "ofrecer_agenda":
                            from datetime import datetime as _dt_ocr, timezone as _tz_ocr
                            save_session(phone, "IDLE", {
                                "especialidad_sugerida": "ecografía",
                                "especialidad_sugerida_ts":
                                    _dt_ocr.now(_tz_ocr.utc).isoformat(),
                                "eco_tipo_text": _ocr_dec["tipo_texto"],
                                "_ocr_oferta_ts": _t_ocr.time(),
                                **_ocr_identidad,
                            })
                            _msg_ocr = msg_oferta(_ocr_dec["tipo_texto"],
                                                  _ocr_dec["routing"])
                            await send_whatsapp(phone, _msg_ocr)
                            log_message(phone, "out",
                                        _msg_ocr["interactive"]["body"]["text"],
                                        "IDLE", canal="whatsapp")
                            return Response(status_code=200)
                        if _ocr_dec["accion"] == "obstetrica":
                            save_session(phone, "IDLE", {})
                            await send_whatsapp(phone, MSG_OBSTETRICA)
                            log_message(phone, "out", MSG_OBSTETRICA, "IDLE",
                                        canal="whatsapp")
                            return Response(status_code=200)
                        # accion == "recepcion" → sigue el flujo actual de abajo
                    except Exception as _e_ocr:  # noqa: BLE001
                        log.warning("eco_orden_ocr fallo from=%s: %s",
                                    phone, str(_e_ocr)[:200])
            # ── fin OCR imágenes ───────────────────────────────────────────

            if _media_es_orden_eco:
                _handoff_media = "media:orden_eco"
            elif _ocr_tipo_doc in ("comprobante_pago", "orden_medica",
                                   "receta_medicamentos"):
                # El clasificador leyó la imagen: recepción ve QUÉ llegó
                _handoff_media = f"media:{_ocr_tipo_doc}"
            else:
                _handoff_media = f"media:{msg_type}"
            save_session(phone, "HUMAN_TAKEOVER", {
                "hold_sent": True,
                "handoff_reason": _handoff_media,
                "media_caption": caption,
            })
            log_event(phone, "media_recibido", {"tipo": msg_type, "caption": caption[:200],
                                                 "filename": saved_filename})
            if _media_es_orden_eco:
                log_event(phone, "eco_orden_foto_recepcion", {
                    "tipo": msg_type, "filename": saved_filename,
                })
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
                if _media_es_orden_eco:
                    reply = (
                        "Recibí la foto, gracias 📄\n\n"
                        "Si es tu orden médica, una recepcionista la va a revisar "
                        "y te escribirá para agendar la ecografía que corresponde.\n"
                        "Si necesitas algo más rápido, puedes llamar al 📞 (44) 296 5226"
                    )
                elif _ocr_tipo_doc == "orden_medica":
                    # Orden leída pero no auto-agendable (no-eco, varias, ilegible)
                    reply = (
                        "Recibí tu orden médica 📄, gracias.\n\n"
                        "Una recepcionista la va a revisar y te escribirá para "
                        "coordinar la hora o el examen que indica.\n"
                        "Si es urgente, puedes llamar al 📞 (44) 296 5226"
                    )
                elif _ocr_tipo_doc == "comprobante_pago":
                    reply = (
                        "Recibí tu comprobante, gracias 🙏\n\n"
                        "Una recepcionista lo va a verificar y te confirmará "
                        "en este mismo chat."
                    )
                else:
                    reply = (
                        f"Recibí tu {label}, gracias.\n\n"
                        "Lo guardé en tu ficha y una recepcionista lo va a revisar 🙏\n"
                        "Si es urgente, puedes llamar al 📞 (44) 296 5226"
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

        from session import _scrub_pii as _sp
        log.info("MSG from=%s id=%s type=%s text=%r", phone, msg_id, msg_type, _sp(texto[:100]))

        from resilience import get_phone_lock
        async with get_phone_lock(phone):
            session = get_session(phone)
            state_before = session.get("state", "IDLE")
            log_text = f"🎤 {texto}" if is_audio else (_interactive_title or texto)
            log_message(phone, "in", log_text, state_before, canal="whatsapp")
            # Marca temporal del entrante: la usa el reintento por saturación
            # para saber si el paciente escribió de nuevo mientras esperaba.
            _ts_entrante = _dt_now_iso()

            # Modo caída Medilink: captura contexto de TODO mensaje entrante
            # mientras esté abierto (incluidos los que quedan en HUMAN_TAKEOVER
            # más abajo) — ver medilink_outage.py.
            try:
                medilink_outage.capturar_mensaje(phone, texto, session)
            except Exception:
                pass

            # ── Captura fbclid desde primer mensaje (una sola vez por sesión) ──
            # Meta puede precargar mensajes de ad con "Hola [fbclid:XXX]".
            # Guardamos en session data para mandarlo con eventos CAPI.
            try:
                import re as _re_fbclid
                import time as _time_fb
                _fbclid_re = _re_fbclid.compile(r"fbclid[=:]([A-Za-z0-9_-]+)", _re_fbclid.IGNORECASE)
                _fbclid_m = _fbclid_re.search(texto or "")
                if _fbclid_m:
                    # P28: reusar session ya leida al inicio del lock — evita apertura SQLCipher extra
                    _data_fb = session.get("data") or {}
                    if not _data_fb.get("fbclid"):
                        _data_fb["fbclid"] = _fbclid_m.group(1)
                        _data_fb["fbclid_ts"] = int(_time_fb.time())
                        save_session(phone, session.get("state", "IDLE"), _data_fb)
                        log_event(phone, "fbclid_captured", {"fbclid": _fbclid_m.group(1)[:20]})
            except Exception as _fbclid_err:
                log.debug("fbclid capture error: %s", _fbclid_err)
            # ── fin captura fbclid ──────────────────────────────────────────

            # ── Captura referral Meta (Click-to-WhatsApp desde anuncio) ──────
            # WhatsApp Cloud API incluye `messages[0].referral` cuando el usuario
            # hizo clic en un anuncio "Send Message" de Meta para abrir la conversación.
            # Solo procesamos el primer mensaje de la sesión (cuando aún no hay
            # meta_referral guardado) para no sobreescribir si el paciente responde
            # múltiples veces desde el mismo anuncio.
            # Cada clic en un ad (aunque el phone ya haya hablado antes) es una
            # atribución válida → guardar SIEMPRE que llegue referral. El guard
            # `if not _existing_ref` previo bloqueaba phones con sesión existente
            # → ~288 conv/mes sin atribuir (fuga CAC mayo 2026). save_meta_referral
            # hace INSERT con ts (no sobreescribe); dedup msg_id evita reprocesar.
            try:
                _wa_referral = msg.get("referral") or {}
                if _wa_referral:
                    from session import save_meta_referral as _smr_wa
                    _smr_wa(phone, _wa_referral, canal="whatsapp")
                    log.info("META_REFERRAL WA capturado phone=%s headline=%r",
                             phone, _wa_referral.get("headline", "")[:60])
            except Exception as _wa_ref_err:
                log.debug("meta_referral WA error: %s", _wa_ref_err)
            # ── fin captura referral ────────────────────────────────────────

            # ── Guard HUMAN_TAKEOVER centralizado ──────────────────────────
            # Aplica a TODOS los tipos de mensaje (texto, audio transcrito,
            # documento, botón interactivo). Antes este guard solo existía en
            # el path de texto (comentario "silencio intencional" al final), lo
            # que permitía que audios y documentos igual llamaran handle_message
            # y generaran respuestas mientras un operador humano atendía.
            # Bug confirmado: 39 interrupciones en 48h por audios de pacientes.
            if state_before == "HUMAN_TAKEOVER":
                # Rescate seguro: si el paciente acaba de pedir recepción (o cayó
                # en takeover) y NINGÚN humano respondió aún (human_replied=False),
                # y toca un botón/intent de autoservicio inequívoco (elegir
                # especialidad, agendar, menú, mis citas...), lo sacamos del
                # takeover y lo atiende el bot. Caso real: paciente toca "Hablar
                # con recepción" e inmediatamente "🦷 Revisión dental" → antes el
                # bot lo dejaba mudo esperando a un humano que ni había entrado.
                # Si la recepcionista YA habló, se respeta el silencio (no la
                # interrumpe el bot).
                _tk_data = session.get("data") or {}
                if (not _tk_data.get("human_replied")
                        and _es_intent_rescate_takeover(texto)):
                    log.info("HUMAN_TAKEOVER rescate por intención clara from=%s txt=%r",
                             phone, (texto or "")[:40])
                    try:
                        log_event(phone, "takeover_rescate_intent", {"texto": (texto or "")[:60]})
                    except Exception:
                        pass
                    save_session(phone, "IDLE", {})
                    session = get_session(phone)
                    state_before = "IDLE"
                else:
                    log.info("HUMAN_TAKEOVER activo from=%s type=%s — silenciado", phone, msg_type)
                    return Response(status_code=200)
            # ── fin guard HUMAN_TAKEOVER ────────────────────────────────────

            # ── Puente asistente Meulen ─────────────────────────────────────
            # Si escribe el número autorizado (el papá), su mensaje va al bot de
            # Meulen (modo supermercado) en vez del flujo de pacientes. Gateado:
            # cualquier otro número sigue el flujo clínico normal.
            if MEULEN_ASSISTANT_ACTIVE and phone in MEULEN_ASSISTANT_PHONES:
                _mln_reply = ""
                try:
                    import httpx as _httpx_mln
                    async with _httpx_mln.AsyncClient(timeout=45) as _cmln:
                        _rmln = await _cmln.post(MEULEN_ASSISTANT_URL, json={
                            "phone": phone, "text": texto, "secret": MEULEN_ASSISTANT_SECRET})
                        _rmln.raise_for_status()
                        _mln_reply = (_rmln.json() or {}).get("reply", "")
                except Exception as _emln:
                    log.error("Puente Meulen error from=%s: %s", phone, _emln)
                    _mln_reply = "Uy, tuve un problema con el sistema de Meulen. Probá de nuevo en un ratito 🙏"
                if not _mln_reply:
                    _mln_reply = "No te entendí 😅 ¿me lo repetís?"
                await send_whatsapp(phone, _mln_reply)
                log_message(phone, "out", _mln_reply[:600], "MEULEN", canal="whatsapp")
                return Response(status_code=200)

            # ── Asistente Adkun (dueño): capa agéntica por WhatsApp ──────────
            # Si escribe el dueño desde un número autorizado, recibe reportes
            # (P&L, win-back, Director, Autopilot, Optimizador) en vez del flujo
            # de pacientes. READ-ONLY. Gateado: otro número sigue el flujo normal.
            if ADKUN_ASSISTANT_ACTIVE and phone in ADKUN_ASSISTANT_PHONES:
                try:
                    from adkun_assistant import route as _adkun_route
                    _handled, _adk_reply = _adkun_route(phone, texto)
                except Exception as _eadk:
                    log.error("Asistente Adkun error from=%s: %s", phone, _eadk)
                    _handled, _adk_reply = True, "Tuve un problema con el asistente Adkun 🙏. Probá *menu*."
                if _handled:
                    await send_whatsapp(phone, _adk_reply or "Probá *menu*.")
                    log_message(phone, "out", (_adk_reply or "")[:600], "ADKUN", canal="whatsapp")
                    return Response(status_code=200)
                # modo paciente: cae al flujo clínico normal (no interrumpe)

            # autocapture_profile: solo corre fuera de HUMAN_TAKEOVER para evitar
            # capturar texto dictado por el operador como si fuera datos del paciente.
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
                log_message(phone, "out", f"🎤 Entendí: _{texto}_", state_before)

            try:
                respuesta = await handle_message(phone, texto, session)
            except MedilinkInactiva as e:
                # Plataforma Medilink suspendida (403), no saturada (2026-08-12).
                # Mismo criterio que el otro webhook (ver arriba). NO resetear
                # la sesión: cuando vuelva, el watcher (jobs.py) le escribe con
                # las horas reales de lo que estaba pidiendo.
                log.error("Medilink INACTIVA procesando msg from=%s: %s", phone, e)
                log_event(phone, "respuesta_medilink_inactiva", {})
                try:
                    medilink_outage.capturar_mensaje(phone, texto, session, force=True)
                except Exception:
                    pass
                respuesta = (
                    "El sistema de horas está con un problema técnico en este "
                    "momento 😕\n\n"
                    "No perdí tu mensaje: apenas se recupere te escribo con las "
                    "horas disponibles.\n\n"
                    f"Si prefieres, llámanos: 📞 {CMC_TELEFONO}"
                )
            except MedilinkRateLimited as e:
                # Saturado ≠ caído: mismo criterio que el otro webhook (ver arriba).
                # Sin reset de sesión y sin lista de espera.
                log.warning("Medilink saturado procesando msg from=%s: %s", phone, e)
                log_event(phone, "respuesta_medilink_saturado", {})
                # El bot reintenta SOLO en ~45 s. Antes el texto le pedía al
                # paciente que reescribiera y no volvía: caso 56926854672
                # (18-ago) — tocó "Sí, agendar", leyó esto, nunca respondió, y
                # recepción estuvo 10 min tomándole los datos a mano.
                try:
                    import reintento_saturado
                    from resilience import spawn_task
                    spawn_task(
                        reintento_saturado.programar(
                            phone=phone, texto=texto, canal=canal,
                            sender_id=sender_id, send_fn=send_fn,
                            desde_ts=_ts_entrante,
                        ),
                        name=f"reintento_saturado:{phone}",
                    )
                except Exception as _e_re:  # noqa: BLE001
                    log.warning("no se pudo programar reintento por saturación: %s", _e_re)
                respuesta = (
                    "Dame un momento — la agenda está muy pedida y no alcancé "
                    "a leerla 😅\n\n"
                    "*Te escribo con las horas apenas las tenga*, no necesitas "
                    "hacer nada.\n"
                    f"Si prefieres, llámanos: 📞 {CMC_TELEFONO}"
                )
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
