"""Scheduler job functions — reenganche, watchdog, waitlist, fidelización wrappers."""
import logging

import httpx

from config import MEDILINK_BASE_URL, MEDILINK_TOKEN, ADMIN_ALERT_PHONE, USE_TEMPLATES
from messaging import (send_whatsapp, send_whatsapp_interactive,
                       send_whatsapp_template)
from reminders import enviar_recordatorios, enviar_recordatorios_2h
from fidelizacion import (enviar_seguimiento_postconsulta, enviar_reactivacion_pacientes,
                          enviar_adherencia_kine, enviar_recordatorio_control,
                          enviar_crosssell_kine)
from medilink import (buscar_primer_dia, buscar_paciente, sync_citas_dia,
                      SEGUIMIENTO_ESPECIALIDADES, PROFESIONALES)
from session import (get_sesiones_abandonadas, save_session, log_event,
                     get_pending_intent_queue, mark_intent_notified, intent_queue_depth,
                     get_waitlist_pending, mark_waitlist_notified)
from resilience import (is_medilink_down, mark_medilink_up, medilink_down_since,
                        should_notify_reception, mark_reception_notified)

log = logging.getLogger("bot")

HEADERS_MEDILINK = {"Authorization": f"Token {MEDILINK_TOKEN}"}


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

async def _job_reactivacion():
    await enviar_reactivacion_pacientes(send_whatsapp, send_template_fn=_tpl)

async def _job_adherencia_kine():
    await enviar_adherencia_kine(send_whatsapp, send_template_fn=_tpl)

async def _job_control_especialidad():
    await enviar_recordatorio_control(send_whatsapp, send_template_fn=_tpl)

async def _job_crosssell_kine():
    await enviar_crosssell_kine(send_whatsapp, send_template_fn=_tpl)


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

    # Medilink respondió OK → recuperación
    mark_medilink_up()
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

        nombre_corto = nombre.split()[0] if nombre else ""
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
