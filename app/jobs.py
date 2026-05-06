"""Scheduler job functions — reenganche, watchdog, waitlist, fidelización wrappers."""
import logging

import httpx

from config import MEDILINK_BASE_URL, MEDILINK_TOKEN, ADMIN_ALERT_PHONE, USE_TEMPLATES
from messaging import (send_whatsapp, send_whatsapp_interactive, send_instagram, send_messenger,
                       send_whatsapp_template)
from reminders import enviar_recordatorios, enviar_recordatorios_2h
from fidelizacion import (enviar_seguimiento_postconsulta,
                          enviar_seguimiento_postconsulta_dia_anterior,
                          enviar_reactivacion_pacientes,
                          enviar_adherencia_kine, enviar_recordatorio_control,
                          enviar_crosssell_kine, enviar_cumpleanos, enviar_winback,
                          enviar_crosssell_orl_fono, enviar_crosssell_odonto_estetica,
                          enviar_crosssell_mg_chequeo)
from medilink import (buscar_primer_dia, buscar_paciente, sync_citas_dia,
                      SEGUIMIENTO_ESPECIALIDADES, PROFESIONALES, get_slots_libres,
                      listar_citas_paciente)
from session import (get_sesiones_abandonadas, save_session, log_event,
                     get_pending_intent_queue, mark_intent_notified, intent_queue_depth,
                     get_waitlist_pending, mark_waitlist_notified,
                     get_cita_bot_by_id_for_rebook, mark_cita_cancel_detected,
                     get_profile,
                     get_candidatos_horas_vacias, log_horas_vacias_envio,
                     get_horas_vacias_envios_hoy)
from resilience import (is_medilink_down, mark_medilink_up, medilink_down_since,
                        should_notify_reception, mark_reception_notified,
                        should_notify_recovery, mark_recovery_notified)
from doctor_alerts import (enviar_resumen_precita, enviar_reporte_progreso,
                           reset_resumenes_diarios)
from config import CMC_TELEFONO

log = logging.getLogger("bot")

HEADERS_MEDILINK = {"Authorization": f"Token {MEDILINK_TOKEN}"}


def _canal_de_phone(phone: str) -> str:
    """Devuelve 'wa', 'ig', 'fb' o 'unknown' según el prefijo del id de sesión."""
    p = str(phone or "")
    if p.startswith("ig_"):
        return "ig"
    if p.startswith("fb_"):
        return "fb"
    if p.startswith("TEST_"):
        return "unknown"
    if p.isdigit() and len(p) >= 10:
        return "wa"
    return "unknown"


async def _enviar_reenganche():
    """Reenganche agresivo: slot real + urgencia + botón directo.

    Cubre WhatsApp, Instagram y Messenger. Antes el filtro `phone.isdigit()`
    excluía silenciosamente todas las sesiones de IG/FB (fix 2026-04-24
    eliminó el envío erróneo a Meta API por canal equivocado pero también
    cortó el reenganche a esos pacientes). Ahora se rutea al canal correcto.
    """
    sesiones = get_sesiones_abandonadas()
    # Sólo phones con canal conocido. TEST_* y otros raros se descartan.
    sesiones = [s for s in sesiones if _canal_de_phone(s.get("phone", "")) != "unknown"]
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

        canal = _canal_de_phone(phone)
        try:
            if canal == "wa":
                from flows import _btn_msg as _btn_msg_j
                _bt_msg = _btn_msg_j(msg, [
                    {"id": "menu", "title": "✅ Sí, continuar"},
                    {"id": "no_gracias_reeng", "title": "No por ahora"},
                ])
                await send_whatsapp_interactive(phone, _bt_msg["interactive"])
            elif canal == "ig":
                igsid = phone[3:]  # strip "ig_"
                await send_instagram(igsid, msg + "\n\nEscribe *menu* para continuar o *no* si ya no te interesa.")
            elif canal == "fb":
                psid = phone[3:]  # strip "fb_"
                await send_messenger(psid, msg + "\n\nEscribe *menu* para continuar o *no* si ya no te interesa.")
        except Exception:
            if canal == "wa":
                try:
                    await send_whatsapp(phone, msg + "\n\nEscribe *menu* para continuar.")
                except Exception:
                    log.exception("Reenganche fallback wa falló phone=%s", phone)
                    continue
            else:
                log.exception("Reenganche %s falló phone=%s", canal, phone)
                continue
        data["reenganche_sent"] = True
        save_session(phone, state, data)
        log.info("Reenganche enviado → %s (estado: %s, canal: %s)", phone, state, canal)


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
    try:
        await enviar_seguimiento_postconsulta(
            send_whatsapp, send_template_fn=_tpl,
            send_text_fn=send_whatsapp, buscar_paciente_fn=buscar_paciente,
        )
    except Exception as e:
        log.error("_job_postconsulta falló (BUG-07): %s", e)


async def _job_postconsulta_morning():
    """Recoge postconsulta de citas tardías (>22:00) del día anterior.
    Corre 09:00 CLT. Complementa _job_postconsulta de las 22:00."""
    try:
        await enviar_seguimiento_postconsulta_dia_anterior(
            send_whatsapp, send_template_fn=_tpl,
            send_text_fn=send_whatsapp, buscar_paciente_fn=buscar_paciente,
        )
    except Exception as e:
        log.exception("Postconsulta morning falló: %s", e)


async def _job_detectar_cancelaciones():
    """Cada hora: barrer citas futuras (hoy + 14 días) y detectar cancelaciones
    hechas directamente en Medilink (cuando un doctor o recepción anula sin pasar
    por el bot). Marca cancel_detected_at en citas_bot y, si la cita es próxima
    (≤48h), dispara reagendamiento automático con 3 slots alternativos.

    Caso real 2026-05-03: cita 54874 (Quijano lunes 4-may) anulada hace 20 días
    en Medilink seguía generando recordatorios. La pre-validación en
    enviar_recordatorios resuelve el síntoma; este job es la solución preventiva
    (detecta antes del recordatorio y reagenda al paciente con tiempo).

    Rate-limit-aware: pausa 200ms entre requests para no saturar Medilink.
    """
    import asyncio
    from session import get_citas_bot_para_validar, mark_cita_cancel_detected
    from medilink import get_cita
    from datetime import datetime, timezone, timedelta
    from zoneinfo import ZoneInfo
    _CL = ZoneInfo("America/Santiago")

    if is_medilink_down():
        log.info("Detect cancelaciones: Medilink down, skip")
        return

    citas = get_citas_bot_para_validar(dias_adelante=14)
    if not citas:
        log.info("Detect cancelaciones: 0 citas a validar")
        return

    log.info("Detect cancelaciones: validando %d citas futuras", len(citas))
    ahora = datetime.now(_CL)
    canceladas_proximas = []
    canceladas_lejanas = 0
    errores = 0

    for c in citas:
        id_cita = c.get("id_cita")
        try:
            cita_ml = await get_cita(int(id_cita))
        except (TypeError, ValueError):
            continue
        except Exception as e:
            errores += 1
            log.debug("get_cita falló id=%s: %s", id_cita, e)
            await asyncio.sleep(0.5)
            continue
        await asyncio.sleep(0.2)  # rate-limit
        if cita_ml is None:
            continue

        anulada = (cita_ml.get("id_estado") == 1
                   or cita_ml.get("estado_anulacion") == 1)
        # Slot reasignado a otro paciente (también es "cancelación" para el original)
        id_pac_local = c.get("id_paciente_medilink")
        id_pac_ml = cita_ml.get("id_paciente")
        reasignada = (id_pac_local and id_pac_ml
                      and str(id_pac_local) != str(id_pac_ml))

        if not (anulada or reasignada):
            continue

        mark_cita_cancel_detected(str(id_cita))
        log_event(c.get("phone", ""), "cita_cancelada_detectada",
                  {"id_cita": id_cita, "fecha": c.get("fecha"),
                   "hora": c.get("hora"), "tipo": "anulada" if anulada else "reasignada"})

        # ¿Cita próxima? — calcular delta horas
        try:
            fh = datetime.strptime(
                f"{c['fecha']} {c['hora'][:5]}", "%Y-%m-%d %H:%M"
            ).replace(tzinfo=_CL)
            horas_hasta = (fh - ahora).total_seconds() / 3600
        except (ValueError, KeyError):
            horas_hasta = 9999

        if 0 < horas_hasta <= 48:
            canceladas_proximas.append({"id_cita": id_cita, "horas": horas_hasta,
                                        "phone": c.get("phone")})
        else:
            canceladas_lejanas += 1

    log.info("Detect cancelaciones: %d próximas (≤48h) · %d lejanas · %d errores",
             len(canceladas_proximas), canceladas_lejanas, errores)

    # Disparar reagendamiento automático para las próximas
    for cp in canceladas_proximas:
        try:
            res = await enviar_reagendar_por_cancelacion(
                str(cp["id_cita"]), motivo="medilink_cancel_detected"
            )
            log.info("Reagendar auto id=%s phone=%s: %s",
                     cp["id_cita"], cp["phone"], res)
        except Exception as e:
            log.exception("Reagendar auto falló id=%s: %s", cp["id_cita"], e)


async def _job_monitor_anomalias():
    """Cada 15 min: escanea anomalías y manda resumen al WhatsApp del dueño.

    Detectores: postconsulta prematuro, RUT rechazado repetido, cancelar
    con keywords de pago, fallback loop, menú repetido, leak +56987834148,
    recordatorio a cita anulada, reenganche caído.

    Anti-spam interno: cada alerta tiene hash y TTL 4h en monitor_alerts_seen.
    """
    try:
        from monitor import enviar_resumen_anomalias
        n = await enviar_resumen_anomalias(send_whatsapp)
        if n:
            log.info("Monitor: %d alertas enviadas al admin", n)
    except Exception as e:
        log.exception("Monitor anomalías falló: %s", e)


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
    try:
        await enviar_reactivacion_pacientes(send_whatsapp, send_template_fn=_tpl)
    except Exception as e:
        log.error("_job_reactivacion falló (BUG-07): %s", e)

async def _job_adherencia_kine():
    try:
        await enviar_adherencia_kine(send_whatsapp, send_template_fn=_tpl)
    except Exception as e:
        log.error("_job_adherencia_kine falló (BUG-07): %s", e)

async def _job_control_especialidad():
    try:
        await enviar_recordatorio_control(send_whatsapp, send_template_fn=_tpl)
    except Exception as e:
        log.error("_job_control_especialidad falló (BUG-07): %s", e)

async def _job_crosssell_kine():
    try:
        await enviar_crosssell_kine(send_whatsapp, send_template_fn=_tpl)
    except Exception as e:
        log.error("_job_crosssell_kine falló (BUG-07): %s", e)

async def _job_crosssell_orl_fono():
    try:
        await enviar_crosssell_orl_fono(send_whatsapp, send_template_fn=_tpl)
    except Exception as e:
        log.error("_job_crosssell_orl_fono falló (BUG-07): %s", e)

async def _job_crosssell_odonto_estetica():
    try:
        await enviar_crosssell_odonto_estetica(send_whatsapp, send_template_fn=_tpl)
    except Exception as e:
        log.error("_job_crosssell_odonto_estetica falló (BUG-07): %s", e)

async def _job_crosssell_mg_chequeo():
    try:
        await enviar_crosssell_mg_chequeo(send_whatsapp, send_template_fn=_tpl)
    except Exception as e:
        log.error("_job_crosssell_mg_chequeo falló (BUG-07): %s", e)

async def _job_cumpleanos():
    try:
        await enviar_cumpleanos(send_whatsapp)
    except Exception as e:
        log.error("_job_cumpleanos falló (BUG-07): %s", e)

async def _job_winback():
    try:
        await enviar_winback(send_whatsapp)
    except Exception as e:
        log.error("_job_winback falló (BUG-07): %s", e)

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
    try:
        await enviar_resumen_precita(send_whatsapp, _doctor_phone)
    except Exception as e:
        log.error("_job_doctor_resumen_precita falló (BUG-07): %s", e)

async def _job_doctor_reporte_progreso():
    if not _admin_window_open():
        return
    try:
        await enviar_reporte_progreso(send_whatsapp, _doctor_phone)
    except Exception as e:
        log.error("_job_doctor_reporte_progreso falló (BUG-07): %s", e)

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
    BUG-07: envuelto en try/except amplio para que un crash no SIGKILL al servicio.
    """
    try:
        await _job_medilink_watchdog_inner()
    except Exception as e:
        log.error("_job_medilink_watchdog falló inesperadamente (BUG-07): %s", e)


async def _job_medilink_watchdog_inner():
    if not is_medilink_down():
        return

    # Ping rápido a /sucursales (endpoint liviano y estable)
    ok = False
    try:
        async with httpx.AsyncClient(timeout=8) as client:
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


_WAITLIST_ESP_KEYWORDS = (
    ("ecograf", "ecografia"),
    ("cardiolog", "cardiologia"),
    ("gastroenter", "gastroenterologia"),
    ("ginecolog", "ginecologia"),
    ("traumatol", "traumatologia"),
    ("endodon", "endodoncia"),
    ("ortodon", "ortodoncia"),
    ("implantol", "implantologia"),
    ("estetic", "estetica facial"),
    ("kinesiolog", "kinesiologia"),
    ("fonoaud", "fonoaudiologia"),
    ("otorrin", "otorrinolaringologia"),
    ("psicolog", "psicologia"),
    ("nutricion", "nutricion"),
    ("matron", "matrona"),
    ("podolog", "podologia"),
    ("masoterap", "masoterapia"),
    ("odontolog", "odontologia"),
    ("medicina familiar", "medicina familiar"),
    ("medicina general", "medicina general"),
)


def _waitlist_esp_canonical(s: str) -> str:
    """Normaliza una especialidad (de waitlist o de Medilink) a una raíz comparable.
    Captura variantes con/sin tildes, texto libre del paciente ("para ecografía
    intravajinal" → "ecografia") y sinónimos."""
    import unicodedata
    s = (s or "").strip().lower()
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    for needle, canon in _WAITLIST_ESP_KEYWORDS:
        if needle in s:
            return canon
    return s


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
        rut_p = (row.get("rut") or "").strip()

        # Skip si el paciente ya tiene una cita futura en esta especialidad
        # (recepcionista pudo haberla agendado a mano fuera del bot).
        if rut_p:
            try:
                citas_existentes = await listar_citas_paciente(0, rut=rut_p) or []
                esp_canon = _waitlist_esp_canonical(esp)
                ya_agendada = next(
                    (c for c in citas_existentes
                     if _waitlist_esp_canonical(c.get("especialidad", "")) == esp_canon),
                    None,
                )
                if ya_agendada:
                    mark_waitlist_notified(wl_id)
                    log_event(phone_p, "waitlist_skip_ya_tiene_cita", {
                        "waitlist_id": wl_id,
                        "especialidad": esp,
                        "cita_fecha": ya_agendada.get("fecha"),
                        "cita_hora": ya_agendada.get("hora_inicio"),
                        "cita_esp": ya_agendada.get("especialidad"),
                    })
                    log.info(
                        "waitlist_check: skip wl_id=%d (ya tiene cita %s %s en %s)",
                        wl_id, ya_agendada.get("fecha"), ya_agendada.get("hora_inicio"),
                        ya_agendada.get("especialidad"),
                    )
                    continue
            except Exception as e:
                log.warning(
                    "waitlist_check: fallo verificando citas existentes wl_id=%d: %s",
                    wl_id, e,
                )

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


async def _job_regenerate_heatmap_cache():
    """Cada 6h: regenera heatmap_cache.json con conteos de comunas desde sessions.db.

    Lee conversations + citas_cache, agrupa pacientes por comuna/región
    y guarda el resultado en data/heatmap_cache.json para que /api/seo/geo
    lo sirva sin recalcular en cada request.
    """
    try:
        import json as _json
        import sqlite3 as _sqlite3
        from pathlib import Path as _Path
        from collections import defaultdict as _dd
        from session import _conn as _sc_heatmap

        _db_heatmap  = _Path(__file__).parent.parent / "data" / "heatmap_cache.db"
        _out_file    = _Path(__file__).parent.parent / "data" / "heatmap_cache.json"

        # Leer comunas desde contact_profiles (sessions.db) via _conn (SQLCipher-aware)
        comunas: dict = _dd(lambda: {"pacientes": 0, "citas": 0})

        with _sc_heatmap() as conn:
            # Comunas registradas en perfiles de contacto
            rows = conn.execute(
                "SELECT UPPER(TRIM(comuna)) AS c, COUNT(DISTINCT phone) AS n "
                "FROM contact_profiles WHERE comuna IS NOT NULL AND comuna != '' "
                "GROUP BY UPPER(TRIM(comuna))"
            ).fetchall()
            for r in rows:
                comunas[r["c"]]["pacientes"] += r["n"]
            # Tags de arauco como fallback
            arauco_phones = conn.execute(
                "SELECT COUNT(DISTINCT phone) FROM tags WHERE tag='arauco'"
            ).fetchone()[0]
            if arauco_phones and "ARAUCO" not in comunas:
                comunas["ARAUCO"]["pacientes"] += arauco_phones

        # Sumar citas desde heatmap_cache.db si existe
        if _db_heatmap.exists():
            conn2 = _sqlite3.connect(str(_db_heatmap))
            conn2.row_factory = _sqlite3.Row
            try:
                rows2 = conn2.execute(
                    "SELECT UPPER(TRIM(comuna)) AS c, COUNT(*) AS n "
                    "FROM citas_heatmap WHERE comuna IS NOT NULL AND comuna != '' "
                    "GROUP BY UPPER(TRIM(comuna))"
                ).fetchall()
                for r in rows2:
                    comunas[r["c"]]["citas"] += r["n"]
            except Exception:
                pass
            finally:
                conn2.close()

        result = {
            "generado_en": __import__("datetime").datetime.utcnow().isoformat() + "Z",
            "fuente": "sessions.db + heatmap_cache.db",
            "comunas": [
                {"comuna": k, "pacientes": v["pacientes"], "citas": v["citas"]}
                for k, v in sorted(comunas.items(), key=lambda x: -x[1]["pacientes"])
            ]
        }
        _out_file.parent.mkdir(parents=True, exist_ok=True)
        _out_file.write_text(_json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("heatmap_cache regenerado: %d comunas", len(comunas))
    except Exception as e:
        log.error("_job_regenerate_heatmap_cache fallo: %s", e)


async def _job_enviar_dashboards_semanales(forzar: bool = False):
    """Lunes 09:00 CLT: envía por WhatsApp a cada profesional su link de dashboard semanal.

    El link es /profesional/dashboard?token=<HMAC32>  — no expira (revocable generando nuevo token).
    Profesionales sin número de WA definido en PROF_PHONES son saltados silenciosamente.
    """
    try:
        import hmac as _hm, hashlib as _hl
        from datetime import date as _date
        from config import ADMIN_TOKEN as _AT
        from medilink import PROFESIONALES

        # TODO: mover a config.py o a una tabla en sessions.db cuando haya mas profesionales.
        # Formato: id_profesional → numero WA sin '+' (ej. "56912345678")
        PROF_PHONES: dict[int, str] = {
            # 1: "56987834148",   # Dr. Olavarría — número personal, NO habilitar
            # 73: "569XXXXXXXX",  # Dr. Abarca
            # Agregar el WA de cada profesional aqui antes de activar.
        }

        if not PROF_PHONES and not forzar:
            log.info("dashboards_semanales: sin números de WA configurados — agrega PROF_PHONES en jobs.py")
            return

        hoy = _date.today()
        mes_nombres = ["","Enero","Febrero","Marzo","Abril","Mayo","Junio","Julio",
                       "Agosto","Septiembre","Octubre","Noviembre","Diciembre"]
        mes_label = f"{mes_nombres[hoy.month]} {hoy.year}"

        enviados = 0
        for id_prof, wa_phone in PROF_PHONES.items():
            if id_prof not in PROFESIONALES:
                log.warning("dashboards_semanales: prof %d no en PROFESIONALES, saltando", id_prof)
                continue
            nombre = PROFESIONALES[id_prof]["nombre"]
            raw = f"prof:{id_prof}:{_AT}"
            token = _hm.new(_AT.encode(), raw.encode(), _hl.sha256).hexdigest()[:32]
            link = f"https://agentecmc.cl/profesional/dashboard?token={token}"
            texto = (
                f"Hola {nombre.split()[1] if len(nombre.split())>1 else nombre}, "
                f"aqui tienes tu resumen de {mes_label} en el CMC:\n\n"
                f"{link}\n\n"
                f"El link es personal — incluye tus atenciones, NPS de tus pacientes y acciones sugeridas para la semana."
            )
            try:
                await send_whatsapp(wa_phone, texto)
                log.info("dashboards_semanales: enviado a prof=%d phone=%s", id_prof, wa_phone[:6]+"***")
                enviados += 1
            except Exception as _e:
                log.error("dashboards_semanales: error enviando a prof=%d: %s", id_prof, _e)

        log.info("dashboards_semanales: %d links enviados (%s)", enviados, hoy.isoformat())
    except Exception as e:
        log.error("_job_enviar_dashboards_semanales fallo: %s", e)


# ── Horas vacías día siguiente ────────────────────────────────────────────────

# Especialidades con demanda suficiente para justificar notificaciones proactivas.
# Orden de prioridad (mayor demanda histórica primero, según pill de demanda del panel).
_ESPECIALIDADES_HORAS_VACIAS = [
    ("Medicina General",     [73, 1, 13]),
    ("Ginecología",          [61]),
    ("Otorrinolaringología", [23]),
    ("Kinesiología",         [77, 21]),
    ("Cardiología",          [60]),
    ("Gastroenterología",    [65]),
    ("Odontología General",  [72, 55]),
    ("Psicología Adulto",    [74, 49]),
    ("Nutrición",            [52]),
    ("Podología",            [56]),
    ("Ecografía",            [68]),
    ("Matrona",              [67]),
    ("Fonoaudiología",       [70]),
]

_HV_MAX_POR_ESPECIALIDAD = 30   # tope de envíos diarios por especialidad
_HV_SLOTS_MINIMOS       = 3    # umbral de "agenda holgada"


async def _job_horas_vacias_dia_siguiente():
    """14:00 CLT — detecta slots libres D+1 y notifica proactivamente a candidatos.

    Lógica:
    1. Para cada especialidad principal, suma slots libres del día siguiente
       entre todos los profesionales activos de esa especialidad.
    2. Si la suma >= _HV_SLOTS_MINIMOS → hay holgura.
    3. Identifica candidatos: phones con opt-in que preguntaron por esa especialidad
       en los últimos 30 días sin agendar, o recibieron sin_disponibilidad.
    4. Envía push de texto (sin template Meta) con slots disponibles + instrucción.
    5. Rate limit: máximo _HV_MAX_POR_ESPECIALIDAD envíos/día por especialidad.
    6. Cooldown: un phone no recibe más de 1 push cada 14 días para la misma especialidad.
    7. Excluye: sin consent, HUMAN_TAKEOVER, blacklist (marketing_opt_out).
    8. NO envía fines de semana después de las 13:00.

    Nota sobre template Meta UTILITY:
        El template aprobado lleva variables {{1}}=nombre, {{2}}=especialidad,
        {{3}}=fecha, {{4}}=hora. Mientras el template no esté aprobado, el job
        envía un mensaje de texto libre dentro de la ventana 24h (si el paciente
        escribió recientemente) para no bloquearse. Cuando el template esté
        disponible, reemplazar send_whatsapp() por send_whatsapp_template().
    """
    from datetime import datetime as _dt, timedelta as _td
    from zoneinfo import ZoneInfo as _ZI
    import asyncio as _asyncio

    _CLT = _ZI("America/Santiago")
    ahora = _dt.now(_CLT)
    dow = ahora.weekday()   # 0=Lun … 6=Dom

    # No enviar fines de semana después de las 13:00
    if dow in (5, 6) and ahora.hour >= 13:
        log.info("horas_vacias: fuera de ventana (fin de semana ≥13:00) — skipped")
        return

    # Fecha del día siguiente en CLT
    manana = (ahora + _td(days=1)).date()
    manana_str = manana.strftime("%Y-%m-%d")
    manana_display = manana.strftime("%-d/%-m/%Y")  # ej. "4/5/2026"

    log.info("horas_vacias: revisando slots para %s", manana_str)
    total_enviados = 0

    for especialidad_label, prof_ids in _ESPECIALIDADES_HORAS_VACIAS:
        esp_key = especialidad_label.lower()

        # Chequear tope diario
        ya_enviados = get_horas_vacias_envios_hoy(esp_key)
        if ya_enviados >= _HV_MAX_POR_ESPECIALIDAD:
            log.info("horas_vacias: %s → tope diario alcanzado (%d)", especialidad_label, ya_enviados)
            continue

        # Recolectar slots libres D+1 de todos los profesionales de esta especialidad
        slots_por_prof: dict[int, list] = {}
        for pid in prof_ids:
            try:
                slots = await get_slots_libres(pid, manana_str)
                if slots:
                    slots_por_prof[pid] = slots
            except Exception as e:
                log.error("horas_vacias: error slots prof=%d esp=%s: %s", pid, especialidad_label, e)
            # Pequeña pausa para no saturar rate limit Medilink (20 req/min)
            await _asyncio.sleep(1.5)

        total_slots = sum(len(v) for v in slots_por_prof.values())
        if total_slots < _HV_SLOTS_MINIMOS:
            log.info("horas_vacias: %s → solo %d slots libres D+1 — no notificar",
                     especialidad_label, total_slots)
            continue

        # Elegir el primer slot disponible de la mañana para mostrar en el mensaje
        todos_slots = sorted(
            [s for sl in slots_por_prof.values() for s in sl],
            key=lambda x: x["hora_inicio"]
        )
        slot_ejemplo = todos_slots[0]
        hora_ejemplo = slot_ejemplo["hora_inicio"]

        log.info("horas_vacias: %s → %d slots libres D+1 — buscando candidatos",
                 especialidad_label, total_slots)

        # Obtener candidatos con opt-in y sin cooldown
        candidatos = get_candidatos_horas_vacias(esp_key, dias=30)
        if not candidatos:
            log.info("horas_vacias: %s → 0 candidatos elegibles", especialidad_label)
            continue

        log.info("horas_vacias: %s → %d candidatos elegibles", especialidad_label, len(candidatos))

        enviados_esp = 0
        for phone in candidatos:
            if ya_enviados + enviados_esp >= _HV_MAX_POR_ESPECIALIDAD:
                log.info("horas_vacias: %s → tope diario alcanzado mid-loop", especialidad_label)
                break
            if _canal_de_phone(phone) not in ("wa",):
                # Solo WhatsApp por ahora (IG/FB no tienen templates UTILITY aprobados)
                continue

            texto = (
                f"Hola, te avisamos que se liberaron horas para {especialidad_label} "
                f"manana {manana_display}. Disponible desde las {hora_ejemplo} hrs.\n\n"
                f"Si te interesa agendar, responde SI y te ayudo.\n\n"
                f"Si no quieres recibir mas avisos, responde BAJA."
            )

            try:
                await send_whatsapp(phone, texto)
                # Usar el primer prof con slots como referencia para el registro
                pid_ref = next(iter(slots_por_prof))
                log_horas_vacias_envio(phone, esp_key, pid_ref, manana_str, hora_ejemplo)
                log_event(phone, "horas_vacias_enviado", {
                    "especialidad": esp_key,
                    "fecha_slot": manana_str,
                    "hora_slot": hora_ejemplo,
                    "total_slots": total_slots,
                })
                enviados_esp += 1
                total_enviados += 1
                # Pausa mínima entre envíos para no saturar Meta API
                await _asyncio.sleep(0.3)
            except Exception as e:
                log.error("horas_vacias: error enviando a %s: %s", phone[:6] + "***", e)

        log.info("horas_vacias: %s → %d envíos realizados", especialidad_label, enviados_esp)

    log.info("horas_vacias: total_enviados=%d para D+1=%s", total_enviados, manana_str)


# ── Telemedicina: recordatorios 24h y 30min antes ─────────────────────────
async def _job_telemedicina_recordatorios():
    """Envía recordatorios de telemedicina con el link de videollamada.

    - 24h antes: mensaje con link + instrucciones
    - 30min antes: mensaje corto con link y hora exacta

    Corre cada 15 minutos entre 7:00 y 22:00 CLT (mismo trigger que recordatorios_2h).
    """
    from session import (get_telemedicina_pendientes_24h,
                         get_telemedicina_pendientes_30min,
                         mark_telemedicina_recordatorio)
    import asyncio as _asyncio

    async def _enviar(row: dict, tipo: str):
        phone = row["phone"]
        link = row["link_videollamada"] or "(link no disponible)"
        fecha_hora = row["fecha_hora"] or ""
        hora = fecha_hora[11:16] if len(fecha_hora) >= 16 else ""
        fecha = fecha_hora[:10] if len(fecha_hora) >= 10 else ""
        if tipo == "24h":
            msg = (
                f"Recuerda que mañana tienes una consulta por *videollamada* en el CMC.\n\n"
                f"📅 {fecha} · 🕐 {hora}\n\n"
                f"*Tu link:* {link}\n\n"
                "Necesitas:\n"
                "✓ Internet estable\n"
                "✓ Cámara y audio funcionando\n"
                "✓ Lugar tranquilo y privado\n"
                "✓ Exámenes o recetas a mano\n\n"
                "Si aún no has pagado, hazlo por transferencia y envía el comprobante a este chat."
            )
        else:
            msg = (
                f"Tu consulta online comienza en *30 minutos* (🕐 {hora}).\n\n"
                f"*Ingresa aquí:* {link}\n\n"
                "Asegúrate de tener buena conexión y cámara activa. ¡Te esperamos!"
            )
        try:
            canal = _canal_de_phone(phone)
            if canal == "wa":
                await send_whatsapp(phone, msg)
            elif canal == "ig":
                await send_instagram(phone, msg)
            elif canal == "fb":
                await send_messenger(phone, msg)
            else:
                log.warning("telemedicina_recordatorio: canal desconocido phone=%s", phone[:8])
                return
            mark_telemedicina_recordatorio(row["id"], tipo)
            log_event(phone, f"telemedicina_recordatorio_{tipo}", {
                "cita_id": row["medilink_cita_id"],
                "link": link[:60],
            })
            log.info("telemedicina_recordatorio_%s enviado a %s", tipo, phone[:8] + "***")
        except Exception as e:
            log.error("telemedicina_recordatorio_%s error phone=%s: %s", tipo, phone[:8], e)

    try:
        pendientes_24h = get_telemedicina_pendientes_24h()
        for row in pendientes_24h:
            await _enviar(row, "24h")
            await _asyncio.sleep(0.3)

        pendientes_30min = get_telemedicina_pendientes_30min()
        for row in pendientes_30min:
            await _enviar(row, "30min")
            await _asyncio.sleep(0.3)

        if pendientes_24h or pendientes_30min:
            log.info("telemedicina_recordatorios: 24h=%d 30min=%d",
                     len(pendientes_24h), len(pendientes_30min))
    except Exception as e:
        log.error("_job_telemedicina_recordatorios fallo: %s", e)
