"""Scheduler job functions — reenganche, watchdog, waitlist, fidelización wrappers."""
import logging

import httpx

from config import MEDILINK_BASE_URL, MEDILINK_TOKEN, ADMIN_ALERT_PHONE, USE_TEMPLATES
from messaging import (send_whatsapp, send_whatsapp_interactive,
                       send_whatsapp_template)
from reminders import enviar_recordatorios, enviar_recordatorios_2h
from fidelizacion import (enviar_seguimiento_postconsulta, enviar_reactivacion_pacientes,
                          enviar_adherencia_kine, enviar_recordatorio_control,
                          enviar_crosssell_kine, enviar_cumpleanos, enviar_winback,
                          enviar_crosssell_orl_fono, enviar_crosssell_odonto_estetica,
                          enviar_crosssell_mg_chequeo)
from medilink import (buscar_primer_dia, buscar_paciente, sync_citas_dia,
                      SEGUIMIENTO_ESPECIALIDADES, PROFESIONALES)
from session import (get_sesiones_abandonadas, save_session, log_event,
                     get_pending_intent_queue, mark_intent_notified, intent_queue_depth,
                     get_waitlist_pending, mark_waitlist_notified,
                     get_cita_bot_by_id_for_rebook, mark_cita_cancel_detected,
                     get_profile)
from resilience import (is_medilink_down, mark_medilink_up, medilink_down_since,
                        should_notify_reception, mark_reception_notified,
                        should_notify_recovery, mark_recovery_notified)
from doctor_alerts import (enviar_resumen_precita, enviar_reporte_progreso,
                           reset_resumenes_diarios)
from config import CMC_TELEFONO

log = logging.getLogger("bot")

HEADERS_MEDILINK = {"Authorization": f"Token {MEDILINK_TOKEN}"}


async def _enviar_reenganche():
    """Reenganche agresivo: slot real + urgencia + botón directo."""
    sesiones = get_sesiones_abandonadas()
    # Filtrar phones no-WhatsApp (fb_*, ig_*, TEST_*, IDs numericos raros)
    sesiones = [s for s in sesiones if str(s.get("phone", "")).isdigit() and len(str(s["phone"])) >= 10]
    for s in sesiones:
        phone = s["phone"]
        state = s["state"]
        data  = s["data"]
        especialidad = data.get("especialidad", "")
        nombre = (data.get("nombre_conocido") or data.get("reg_nombre") or "").split()
        saludo = f"*{nombre[0]}*" if nombre else ""

        # Intentar obtener próximo slot real para la especialidad
        slot_txt = ""
        if especialidad and not is_medilink_down():
            try:
                _, todos = await buscar_primer_dia(especialidad, dias_adelante=7)
                if todos:
                    s0 = todos[0]
                    n_slots = len(todos)
                    escasez = "⚡ _Última hora disponible_ " if n_slots <= 2 else (
                        f"⚡ _Quedan solo {n_slots} horas_ " if n_slots <= 4 else "")
                    slot_txt = (
                        f"\n\n{escasez}📅 *{s0.get('fecha_display', '')}* a las *{s0.get('hora_inicio', '')[:5]}*"
                        f" con *{s0.get('profesional', '')}*"
                    )
            except Exception:
                pass

        if state == "WAIT_SLOT":
            msg = (
                f"Hola {saludo} 👋 Te quedaste a punto de elegir tu hora"
                f"{' de *' + especialidad + '*' if especialidad else ''}."
                f"{slot_txt}\n\n"
                "Las horas se van llenando rápido, ¿la reservo?"
            )
        elif state in ("CONFIRMING_CITA", "WAIT_RUT_AGENDAR", "WAIT_DATOS_NUEVO", "WAIT_NOMBRE_NUEVO"):
            msg = (
                f"Hola {saludo} 👋 Quedaste a un paso de confirmar tu hora"
                f"{' de *' + especialidad + '*' if especialidad else ''}."
                f"{slot_txt}\n\n"
                "Solo falta un dato para reservarla. ¿Seguimos?"
            )
        else:
            msg = (
                f"Hola {saludo} 👋 Tienes una reserva pendiente"
                f"{' de *' + especialidad + '*' if especialidad else ''}."
                f"{slot_txt}\n\n"
                "¿Te la reservo antes de que se llene?"
            )

        try:
            from flows import _btn_msg as _btn_msg_j
            _bt_msg = _btn_msg_j(msg, [
                {"id": "menu", "title": "✅ Sí, continuar"},
                {"id": "no_gracias_reeng", "title": "No por ahora"},
            ])
            await send_whatsapp_interactive(phone, _bt_msg["interactive"])
        except Exception:
            await send_whatsapp(phone, msg + "\n\nEscribe *menu* para continuar.")
        data["reenganche_sent"] = True
        save_session(phone, state, data)
        log.info("Reenganche enviado → %s (estado: %s)", phone, state)


async def enviar_reagendar_por_cancelacion(id_cita: str, motivo: str = "doctor_cancel") -> dict:
    """Envía al paciente 3 slots alternativos tras cancelación del doctor.

    Flujo 1-click: pre-carga los slots en session.data con estado WAIT_SLOT. El
    paciente responde un número y entra directo al flujo existente de confirmación.

    Retorna: {"ok": bool, "reason": str, "phone": str, "slots_enviados": int}.
    """
    cita = get_cita_bot_by_id_for_rebook(id_cita)
    if not cita:
        return {"ok": False, "reason": "cita_no_encontrada"}
    if cita.get("cancel_detected_at"):
        return {"ok": False, "reason": "ya_notificado"}
    phone = cita["phone"]
    esp = (cita.get("especialidad") or "").strip()
    if not esp:
        return {"ok": False, "reason": "sin_especialidad"}
    if is_medilink_down():
        return {"ok": False, "reason": "medilink_down"}

    try:
        smart, todos = await buscar_primer_dia(esp)
    except Exception as e:
        log.exception("Error buscando slots alternos id_cita=%s: %s", id_cita, e)
        return {"ok": False, "reason": "error_buscar_slots"}
    if not todos:
        await send_whatsapp(
            phone,
            f"⚠️ Tu hora del {cita.get('fecha','')} {cita.get('hora','')} con "
            f"{cita.get('profesional','')} fue cancelada por el profesional.\n\n"
            f"Por ahora no tenemos horas disponibles en *{esp}*. "
            f"Llámanos para coordinar: 📞 *{CMC_TELEFONO}*"
        )
        mark_cita_cancel_detected(id_cita)
        log_event(phone, "cancel_doctor_notified", {"id_cita": id_cita, "slots": 0})
        return {"ok": True, "reason": "sin_disponibilidad", "phone": phone, "slots_enviados": 0}

    alt_slots = smart[:3] if smart else todos[:3]
    perfil = get_profile(phone) or {}
    data = {
        "especialidad": esp,
        "slots": alt_slots,
        "todos_slots": todos,
        "fechas_vistas": list({s.get("fecha") for s in alt_slots if s.get("fecha")}),
        "rut_conocido": perfil.get("rut"),
        "nombre_conocido": perfil.get("nombre"),
        "expansion_stage": 0,
        "prof_sugerido_id": alt_slots[0].get("id_profesional") if alt_slots else None,
        "from_cancel": True,
    }
    save_session(phone, "WAIT_SLOT", data)

    await send_whatsapp(
        phone,
        f"⚠️ *Aviso importante*\n\nTu hora del *{cita.get('fecha','')}* a las "
        f"*{cita.get('hora','')}* con *{cita.get('profesional','')}* fue cancelada "
        f"por el profesional 😔\n\nTe dejo 3 alternativas para reagendar en 1 toque:"
    )
    from flows import _format_slots
    body = _format_slots(alt_slots)
    if isinstance(body, dict):
        await send_whatsapp_interactive(phone, body)
    else:
        await send_whatsapp(phone, body)

    mark_cita_cancel_detected(id_cita)
    log_event(phone, "cancel_doctor_notified", {
        "id_cita": id_cita, "slots": len(alt_slots), "motivo": motivo
    })
    return {"ok": True, "reason": "notificado", "phone": phone,
            "slots_enviados": len(alt_slots)}


async def _sync_citas_hoy():
    """Sync diario del caché de citas del día actual (job del scheduler)."""
    from datetime import date
    hoy = date.today().strftime("%Y-%m-%d")
    ids_todos = list({i for cfg in SEGUIMIENTO_ESPECIALIDADES.values() for i in cfg["ids"]})
    await sync_citas_dia(hoy, ids_todos)


# ── Wrappers de fidelización (pasan send_whatsapp + send_whatsapp_template como callback) ──
# Cuando USE_TEMPLATES=True, cada función interna usa send_whatsapp_template en vez de
# mensajes free-form. El flag se evalúa dentro de cada función, no aquí.
_tpl = send_whatsapp_template  # alias corto para los wrappers

async def _job_recordatorios():
    await enviar_recordatorios(send_whatsapp, send_whatsapp_interactive, send_template_fn=_tpl)

async def _job_recordatorios_2h():
    await enviar_recordatorios_2h(send_whatsapp, send_template_fn=_tpl)

async def _job_postconsulta():
    await enviar_seguimiento_postconsulta(
        send_whatsapp, send_template_fn=_tpl,
        send_text_fn=send_whatsapp, buscar_paciente_fn=buscar_paciente,
    )

async def _job_abarca_sync():
    """Sync diario de atenciones del Dr. Abarca. Solo trae el día actual (delta).
    Si la tabla está vacía hace seed completo automáticamente."""
    from main import sync_abarca_atenciones
    from session import abarca_cache_count
    if abarca_cache_count() == 0:
        await sync_abarca_atenciones(desde="2025-05-01", solo_hoy=False)
    else:
        await sync_abarca_atenciones(solo_hoy=True)


async def _job_olavarria_sync():
    """Sync diario de atenciones del Dr. Olavarría (id 1). Mismo patrón que Abarca."""
    from main import sync_olavarria_atenciones
    from session import olavarria_cache_count
    if olavarria_cache_count() == 0:
        await sync_olavarria_atenciones(desde="2024-01-01", solo_hoy=False)
    else:
        await sync_olavarria_atenciones(solo_hoy=True)


async def _job_bi_sync_diario():
    """BI v2: sincroniza atenciones + pagos del día anterior y hoy. Después
    re-cruza pagos huérfanos por si alguna atención llegó tarde."""
    from bi_sync import sync_diario, sync_pagos_rango, _resolver_profesional_pago
    from session import _conn as _c_b
    from datetime import date, timedelta
    try:
        r1 = await sync_diario()
        log.info("bi_sync_diario atenciones: %s", r1)
    except Exception as e:
        log.warning("bi_sync_diario atenciones fallo: %s", e)
    try:
        ayer = (date.today() - timedelta(days=1)).isoformat()
        hoy = date.today().isoformat()
        r2 = await sync_pagos_rango(desde=ayer, hasta=hoy, force=True)
        log.info("bi_sync_diario pagos: %s", r2)
    except Exception as e:
        log.warning("bi_sync_diario pagos fallo: %s", e)
    # Re-cross pagos huérfanos (por si la sync de atenciones llenó gaps)
    try:
        with _c_b() as c:
            rows = c.execute(
                "SELECT pago_id, fecha, id_paciente, monto FROM bi_pagos_caja "
                "WHERE id_profesional IS NULL AND fecha >= ?",
                ((date.today() - timedelta(days=14)).isoformat(),)
            ).fetchall()
            recovered = 0
            for r in rows:
                p = {"id_paciente": r["id_paciente"], "fecha_recepcion": r["fecha"],
                     "monto_pago": r["monto"]}
                id_prof, aid = _resolver_profesional_pago(c, p)
                if id_prof is not None:
                    c.execute("UPDATE bi_pagos_caja SET id_profesional=?, atencion_id=? "
                              "WHERE pago_id=?", (id_prof, aid, r["pago_id"]))
                    recovered += 1
            log.info("bi_sync_diario re-cross: %d/%d pagos recuperados",
                     recovered, len(rows))
    except Exception as e:
        log.warning("bi_sync_diario re-cross fallo: %s", e)

async def _job_reactivacion():
    await enviar_reactivacion_pacientes(send_whatsapp, send_template_fn=_tpl)

async def _job_adherencia_kine():
    await enviar_adherencia_kine(send_whatsapp, send_template_fn=_tpl)

async def _job_control_especialidad():
    await enviar_recordatorio_control(send_whatsapp, send_template_fn=_tpl)

async def _job_crosssell_kine():
    await enviar_crosssell_kine(send_whatsapp, send_template_fn=_tpl)

async def _job_crosssell_orl_fono():
    await enviar_crosssell_orl_fono(send_whatsapp, send_template_fn=_tpl)

async def _job_crosssell_odonto_estetica():
    await enviar_crosssell_odonto_estetica(send_whatsapp, send_template_fn=_tpl)

async def _job_crosssell_mg_chequeo():
    await enviar_crosssell_mg_chequeo(send_whatsapp, send_template_fn=_tpl)

async def _job_cumpleanos():
    await enviar_cumpleanos(send_whatsapp)

async def _job_winback():
    await enviar_winback(send_whatsapp)

# ── Doctor alerts ────────────────────────────────────────────────────────────
# Usar ADMIN_ALERT_PHONE (celular del Dr. Olavarria), no CMC_TELEFONO (bot).
# Caso real 2026-04-23: el job enviaba mensajes al numero del bot → Meta API
# 400 Invalid parameter 6x al dia. Bug heredado del _doctor_phone de flows.py
# (ya arreglado en commit a2b19f4).

def _admin_window_open(threshold_hours: int = 23) -> bool:
    """True si ADMIN_ALERT_PHONE escribió al bot en las últimas N horas.
    Evita enviar texto libre cuando la ventana 24h de Meta está cerrada
    (que devuelve 131047 o 400 #100)."""
    if not ADMIN_ALERT_PHONE:
        return False
    try:
        from session import get_last_inbound_ts
        from datetime import datetime, timedelta, timezone
        ts = get_last_inbound_ts(ADMIN_ALERT_PHONE)
        if not ts:
            return False
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts) < timedelta(hours=threshold_hours)
    except Exception:
        return False


_doctor_phone = ADMIN_ALERT_PHONE

async def _job_doctor_resumen_precita():
    if not _admin_window_open():
        return  # ventana 24h cerrada; evita Meta 400 (#100) Invalid parameter
    await enviar_resumen_precita(send_whatsapp, _doctor_phone)

async def _job_doctor_reporte_progreso():
    if not _admin_window_open():
        return
    await enviar_reporte_progreso(send_whatsapp, _doctor_phone)

async def _job_doctor_reset_diario():
    reset_resumenes_diarios()


async def _job_takeover_ttl():
    """TTL automático para HUMAN_TAKEOVER: reanuda al bot si recepción no
    devolvió el control en 24h. Evita que mensajes del paciente queden
    silenciados indefinidamente cuando recepcionista cierra el chat sin
    clickear "Devolver al bot". Auditoría 2026-04-28: 107 sesiones HUMAN_TAKEOVER
    con +48h sin reanude, 29 con +7 días.
    """
    try:
        from session import reanudar_takeovers_expirados
        phones = reanudar_takeovers_expirados(horas_max=24)
        if phones:
            log.info("takeover_ttl: reanudados %d phones (mostrando primeros 10): %s",
                     len(phones), phones[:10])
    except Exception as e:
        log.exception("takeover_ttl falló: %s", e)


async def _job_takeover_media_ttl():
    """TTL más agresivo (6h) para HUMAN_TAKEOVER iniciados por imagen/PDF/doc.
    Esos handoffs solo requieren ack/archivo de la recepción, no conversación —
    no tiene sentido bloquear al paciente 24h. Auditoría 28-abr-2026: 9 sesiones
    varadas con +8h por media sin acción de recepción.
    """
    try:
        from session import reanudar_takeovers_expirados
        phones = reanudar_takeovers_expirados(horas_max=6, solo_media=True)
        if phones:
            log.info("takeover_media_ttl: reanudados %d phones por media (primeros 10): %s",
                     len(phones), phones[:10])
    except Exception as e:
        log.exception("takeover_media_ttl falló: %s", e)


async def _job_medilink_watchdog():
    """Cada minuto: si Medilink está marcado como caído, prueba un ping.
    - Si se recuperó: marca up, notifica a los pacientes encolados y avisa a recepción.
    - Si sigue caído: notifica a recepción (como máximo 1 vez cada 30 min).
    """
    if not is_medilink_down():
        return

    # Ping rápido a /sucursales (endpoint liviano y estable)
    ok = False
    try:
        async with httpx.AsyncClient(timeout=4) as client:
            r = await client.get(f"{MEDILINK_BASE_URL}/sucursales", headers=HEADERS_MEDILINK)
        ok = r.status_code < 500
    except (httpx.TimeoutException, httpx.NetworkError, httpx.RequestError):
        ok = False

    if not ok:
        # Sigue caído → alerta a recepción con throttle
        if ADMIN_ALERT_PHONE and should_notify_reception():
            depth = intent_queue_depth()
            since = medilink_down_since() or "?"
            try:
                if USE_TEMPLATES:
                    # Template: alerta_tecnica_admin
                    # body_params: [hora_caida, cantidad_cola]
                    await send_whatsapp_template(
                        ADMIN_ALERT_PHONE,
                        "alerta_tecnica_admin",
                        body_params=[since, str(depth)],
                    )
                else:
                    await send_whatsapp(
                        ADMIN_ALERT_PHONE,
                        "⚠️ *Alerta técnica CMC bot*\n\n"
                        f"Medilink no responde desde las {since} UTC.\n"
                        f"Pacientes esperando: *{depth}*\n\n"
                        "El bot avisó a cada paciente que guardó su solicitud y les "
                        "pedirá volver a escribir cuando el sistema esté operativo."
                    )
                mark_reception_notified()
                log.warning("watchdog: recepción notificada — Medilink sigue caído, cola=%d", depth)
            except Exception as e:
                log.error("watchdog: no se pudo notificar a recepción: %s", e)
        return

    # Medilink respondió OK → recuperación.
    # Siempre marcamos como up (estado del sistema), pero las NOTIFICACIONES
    # están gateadas por should_notify_recovery() para evitar spam cuando
    # Medilink oscila (ej. 429 intermitente cada pocos minutos).
    mark_medilink_up()
    if not should_notify_recovery():
        log.info("watchdog: Medilink recuperado pero notif throttled "
                 "(oscilación reciente o notif ya enviada en los últimos 30 min)")
        return
    mark_recovery_notified()
    pendientes = get_pending_intent_queue()
    log.info("watchdog: Medilink OPERATIVO de nuevo — notificando %d pacientes en cola", len(pendientes))
    for row in pendientes:
        phone_p = row["phone"]
        try:
            if USE_TEMPLATES:
                # Template: sistema_recuperado — no params
                await send_whatsapp_template(phone_p, "sistema_recuperado")
            else:
                await send_whatsapp(
                    phone_p,
                    "✅ ¡Buenas noticias! Nuestro sistema de citas ya está operativo de nuevo 🎉\n\n"
                    "Si quieres retomar lo que estabas haciendo, escribe *menu* y te ayudo al tiro.\n\n"
                    "_Gracias por tu paciencia._"
                )
            mark_intent_notified(row["id"])
        except Exception as e:
            log.error("watchdog: fallo notificando paciente %s: %s", phone_p, e)

    # Avisar a recepción que se recuperó
    if ADMIN_ALERT_PHONE:
        try:
            if USE_TEMPLATES:
                # Template: sistema_recuperado_admin
                # body_params: [cantidad_notificados]
                await send_whatsapp_template(
                    ADMIN_ALERT_PHONE,
                    "sistema_recuperado_admin",
                    body_params=[str(len(pendientes))],
                )
            else:
                await send_whatsapp(
                    ADMIN_ALERT_PHONE,
                    "✅ *Medilink recuperado*\n\n"
                    f"El bot ya está operativo de nuevo. Avisé a {len(pendientes)} paciente(s) "
                    "que estaban esperando."
                )
        except Exception:
            pass


async def _job_waitlist_check():
    """Cron diario 07:00 CLT: escanea inscripciones activas en la lista de espera
    y notifica al paciente apenas se libera un cupo en los próximos 14 días.
    Si la inscripción especifica un profesional (id_prof_pref), la búsqueda se
    restringe solo a ese profesional. FIFO (más antiguas primero)."""
    if is_medilink_down():
        log.info("waitlist_check: Medilink caído, saltando ejecución")
        return

    pendientes = get_waitlist_pending()
    if not pendientes:
        return

    log.info("waitlist_check: %d inscripciones activas por revisar", len(pendientes))
    notificados = 0
    for row in pendientes:
        wl_id = row["id"]
        phone_p = row["phone"]
        esp = row["especialidad"]
        id_prof_pref = row.get("id_prof_pref")
        nombre = row.get("nombre") or ""

        try:
            solo_ids = [int(id_prof_pref)] if id_prof_pref else None
            _, todos = await buscar_primer_dia(esp, dias_adelante=14, solo_ids=solo_ids)
        except Exception as e:
            log.error("waitlist_check: error buscando slots para %s (%s): %s", phone_p, esp, e)
            continue

        if not todos:
            continue

        # Hay slots disponibles → notificar y marcar
        primero = todos[0]
        fecha = primero.get("fecha", "")
        hora  = primero.get("hora_inicio", "")
        prof_nombre = primero.get("profesional") or (
            PROFESIONALES.get(int(id_prof_pref), {}).get("nombre", "") if id_prof_pref else ""
        )

        nombre_corto = ((nombre or "").split() or [""])[0] if nombre else ""
        try:
            if USE_TEMPLATES:
                # Template: lista_espera_cupo
                # body_params: [nombre, especialidad, fecha, hora]
                await send_whatsapp_template(
                    phone_p,
                    "lista_espera_cupo",
                    body_params=[nombre_corto or "paciente",
                                 esp.title(), fecha, hora],
                )
            else:
                saludo = f"Hola{' ' + nombre_corto if nombre_corto else ''} 👋"
                prof_txt = f" con *{prof_nombre}*" if prof_nombre else ""
                await send_whatsapp(
                    phone_p,
                    f"{saludo}\n\n"
                    f"¡Buenas noticias! Se liberó un cupo para *{esp.title()}*{prof_txt}.\n\n"
                    f"📅 Primera hora disponible: *{fecha} a las {hora}*\n\n"
                    "Si quieres agendarla escribe *menu* y te ayudo al tiro. "
                    "También puedo buscarte otro horario si ese no te sirve 😊\n\n"
                    "_Te escribimos porque estás en nuestra lista de espera. "
                    "Si ya no la necesitas, ignora este mensaje._"
                )
            mark_waitlist_notified(wl_id)
            log_event(phone_p, "waitlist_notificado", {
                "waitlist_id": wl_id, "especialidad": esp,
                "fecha": fecha, "hora": hora, "id_prof_pref": id_prof_pref,
            })
            notificados += 1
        except Exception as e:
            log.error("waitlist_check: fallo notificando %s: %s", phone_p, e)

    log.info("waitlist_check: notificados %d/%d pacientes", notificados, len(pendientes))


# ── Reporte periódico al admin por WhatsApp ──────────────────────────────────

# Contador previo de 429s para calcular delta entre ejecuciones
_admin_report_state = {"last_429_total": 0}


async def _job_admin_status_report():
    """Cada 30 min envía un resumen de salud al ADMIN_ALERT_PHONE por WhatsApp.
    No consume calls extra a Medilink (solo lee contadores en memoria y DB local).
    Skip si la ventana 24h del admin esta cerrada (evita Meta 131047 spam).
    """
    if not ADMIN_ALERT_PHONE or not _admin_window_open():
        return
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from medilink import get_stats_429, _proxima_cache
        from resilience import is_medilink_down
        from session import _conn

        ahora = datetime.now(ZoneInfo("America/Santiago")).strftime("%H:%M")
        stats = get_stats_429()
        total_429 = stats.get("total", 0)
        delta_429 = total_429 - _admin_report_state["last_429_total"]
        _admin_report_state["last_429_total"] = total_429

        medilink_down = is_medilink_down()
        cache_n = len(_proxima_cache)

        # Jobs del scheduler
        import sys
        _mod = sys.modules.get("app.main") or sys.modules.get("main")
        scheduler = getattr(_mod, "scheduler", None) if _mod else None
        sched_running = bool(scheduler and scheduler.running)
        sched_jobs = len(scheduler.get_jobs()) if scheduler else 0

        # Mensajes últimos 30 min
        try:
            with _conn() as c:
                r = c.execute("""
                    SELECT
                      SUM(CASE WHEN direction='in' THEN 1 ELSE 0 END) AS ins,
                      SUM(CASE WHEN direction='out' THEN 1 ELSE 0 END) AS outs
                    FROM messages
                    WHERE ts >= datetime('now','-30 minutes')
                """).fetchone()
                msgs_in = r["ins"] or 0
                msgs_out = r["outs"] or 0
        except Exception:
            msgs_in = msgs_out = "?"

        # Semáforo
        ok = not medilink_down and sched_running and sched_jobs > 0 and delta_429 < 5
        icono = "🟢" if ok else ("🟡" if not medilink_down else "🔴")
        med_line = "DOWN" if medilink_down else "ok"
        alert = "" if ok else "\n⚠️ *Revisar*"

        body = (
            f"{icono} *CMC bot · {ahora}*\n\n"
            f"Medilink: {med_line}\n"
            f"429 totales: {total_429} (últ 30min: {delta_429})\n"
            f"Cache próxima: {cache_n} entradas\n"
            f"Scheduler: {sched_jobs} jobs · running={sched_running}\n"
            f"Mensajes 30min: in={msgs_in} · out={msgs_out}"
            f"{alert}"
        )

        try:
            from messaging import send_whatsapp
            await send_whatsapp(ADMIN_ALERT_PHONE, body)
        except Exception as e:
            log.error("admin_status_report: fallo enviando a admin: %s", e)
    except Exception as e:
        log.error("admin_status_report: %s", e)


async def _job_cleanup_stuck_sessions():
    """Cada hora: resetea sesiones stuck en WAIT_*/CONFIRMING_* > 4h."""
    try:
        from session import cleanup_stuck_sessions
        n = cleanup_stuck_sessions(hours=4)
        if n:
            log.info("cleanup_stuck_sessions: %d sesiones reseteadas", n)
    except Exception as e:
        log.error("cleanup_stuck_sessions fallo: %s", e)
