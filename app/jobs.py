"""Scheduler job functions — reenganche, watchdog, waitlist, fidelización wrappers."""
import logging

import httpx

from config import (MEDILINK_BASE_URL, MEDILINK_TOKEN, ADMIN_ALERT_PHONE, USE_TEMPLATES,
                    RECORDATORIOS_RECEPCION_ENABLED, RECORDATORIOS_RECEPCION_PROF_IDS)
from messaging import (send_whatsapp, send_whatsapp_interactive, send_instagram, send_messenger,
                       send_whatsapp_template, send_whatsapp_proactive, is_proactive_blocked)
from reminders import (enviar_recordatorios, enviar_recordatorios_2h, enviar_recordatorios_48h,
                       enviar_recordatorios_recepcion_24h, enviar_recordatorios_recepcion_2h,
                       enviar_recordatorios_recepcion_48h)
from fidelizacion import (enviar_seguimiento_postconsulta,
                          enviar_seguimiento_postconsulta_dia_anterior,
                          enviar_reactivacion_pacientes,
                          enviar_adherencia_kine, enviar_recordatorio_control,
                          enviar_crosssell_kine, enviar_cumpleanos, enviar_winback,
                          enviar_crosssell_orl_fono, enviar_crosssell_odonto_estetica,
                          enviar_crosssell_mg_chequeo, enviar_crosssell_dx,
                          enviar_crosssell_post_dental_ortodoncia)
from medilink import (buscar_primer_dia, buscar_paciente, sync_citas_dia,
                      SEGUIMIENTO_ESPECIALIDADES, PROFESIONALES, get_slots_libres,
                      listar_citas_paciente)
from session import (get_sesiones_abandonadas, save_session, log_event, log_message,
                     get_pending_intent_queue, mark_intent_notified, intent_queue_depth,
                     get_waitlist_pending, mark_waitlist_notified, cancel_waitlist,
                     get_cita_bot_by_id_for_rebook, mark_cita_cancel_detected,
                     get_profile,
                     get_candidatos_horas_vacias, log_horas_vacias_envio,
                     get_horas_vacias_envios_hoy,
                     phone_tiene_solo_citas_canceladas,
                     upsert_citas_recepcion,
                     _conn as _session_conn)
from resilience import (is_medilink_down, mark_medilink_up, medilink_down_since,
                        should_notify_reception, mark_reception_notified,
                        should_notify_recovery, mark_recovery_notified)
from doctor_alerts import (enviar_resumen_precita, enviar_reporte_progreso,
                           reset_resumenes_diarios)
from config import CMC_TELEFONO

log = logging.getLogger("bot")

_BOT_LOG_PATH = "/var/log/cmc-bot.log"


def _tail_lines(path: str = _BOT_LOG_PATH, n: int = 5000) -> str:
    """Lee las últimas n líneas del log sin subprocess (PATH seguro en systemd)."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            block = min(size, n * 200)
            f.seek(-block, 2)
            return f.read().decode(errors="ignore")
    except FileNotFoundError:
        return ""
    except Exception:
        return ""

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
    # Fix J: timeout duro para sesiones WAIT_* sin actividad > 2h.
    # Estas sesiones no entran a get_sesiones_abandonadas (filtro 10-90 min)
    # y quedan en loop indefinido acumulando eventos reenganche_skip_cita_cancelada.
    # Raíz: el skip no resetea, solo loggea — la sesión permanece en WAIT_ESPECIALIDAD
    # hasta que el paciente vuelve a escribir o expira el timeout de get_session (4h).
    # Solución: resetear proactivamente a IDLE con log_event para trazabilidad.
    try:
        import json as _json_j
        with _session_conn() as _c_j:
            _stale = _c_j.execute("""
                SELECT phone, state FROM sessions
                WHERE state LIKE 'WAIT_%'
                AND updated_at < datetime('now', '-2 hours')
            """).fetchall()
        for _row_j in (_stale or []):
            _ph_j, _st_j = _row_j["phone"], _row_j["state"]
            # WAIT_ABONO_COMPROBANTE tiene TTL propio de 90 min (menor que 2h):
            # el cron de abono-gate ya lo maneja → no forzar reset aquí.
            if _st_j == "WAIT_ABONO_COMPROBANTE":
                continue
            log_event(_ph_j, "reenganche_force_reset", {"estado_previo": _st_j})
            save_session(_ph_j, "IDLE", {})
            log.info("Reenganche force-reset phone=%s estado=%s", _ph_j, _st_j)
    except Exception as _e_j:
        log.warning("Reenganche force-reset error: %s", _e_j)

    sesiones = get_sesiones_abandonadas()
    # Sólo phones con canal conocido. TEST_* y otros raros se descartan.
    sesiones = [s for s in sesiones if _canal_de_phone(s.get("phone", "")) != "unknown"]
    # Excluir ADMIN_ALERT_PHONE: nunca tiene ventana 24h abierta desde el bot → Meta 131047.
    if ADMIN_ALERT_PHONE:
        sesiones = [s for s in sesiones if s.get("phone") != ADMIN_ALERT_PHONE]
    for s in sesiones:
        phone = s["phone"]
        state = s["state"]
        data  = s["data"]
        especialidad = data.get("especialidad", "")
        nombre = (data.get("nombre_conocido") or data.get("reg_nombre") or "").split()
        saludo = f"*{nombre[0]}*" if nombre else ""

        # No reengancharse si todas las citas relevantes del paciente están canceladas.
        # Evita el mensaje "tienes una reserva pendiente" para una cita que ya no existe.
        # Raíz del bug: el skip anterior solo hacía `continue` sin tocar la sesión, por lo
        # que la sesión permanecía en WAIT_* y volvía en cada ciclo de 5 min indefinidamente
        # (1839 eventos en 7 días, un phone disparado 47 veces). Fix: reset a IDLE inmediato.
        #
        # Refinamiento: si la cancelación es reciente (<48h) y el paciente NO tiene otra
        # cita activa para la misma especialidad, ofrecer UNA re-invitación antes de resetear.
        # Lógica: cancelación puede haber sido por conflicto de horario, no por desinterés.
        # Todo en SQLite local (citas_bot) — sin consulta a Medilink para no añadir latencia.
        if phone_tiene_solo_citas_canceladas(phone):
            # Verificar si aplica re-invitación o skip silencioso.
            _reinvitar = False
            if not data.get("reenganche_optout") and not data.get("reenganche_cancelada_sent"):
                try:
                    with _session_conn() as _cc:
                        # ¿Cancelación reciente (<48h) en citas_bot para este phone?
                        _fila_reciente = _cc.execute(
                            "SELECT especialidad FROM citas_bot "
                            "WHERE phone=? AND cancel_detected_at IS NOT NULL "
                            "AND cancel_detected_at >= datetime('now', '-48 hours') "
                            "ORDER BY cancel_detected_at DESC LIMIT 1",
                            (phone,),
                        ).fetchone()
                        if _fila_reciente:
                            _esp_cancelada = _fila_reciente[0] or especialidad or ""
                            # ¿Ya tiene otra cita activa para la misma especialidad?
                            _otra_activa = _cc.execute(
                                "SELECT 1 FROM citas_bot "
                                "WHERE phone=? AND especialidad=? "
                                "AND cancel_detected_at IS NULL "
                                "AND fecha >= date('now', '-1 day') "
                                "LIMIT 1",
                                (phone, _esp_cancelada),
                            ).fetchone()
                            if not _otra_activa:
                                _reinvitar = True
                except Exception as _e_reinv:
                    log.warning("Reenganche reinvitar check error phone=%s: %s", phone, _e_reinv)

            if _reinvitar:
                # Enviar re-invitación: slot real si está disponible, sino solo el CTA.
                _slot_txt_reinv = ""
                if especialidad and not is_medilink_down():
                    try:
                        _, _todos_reinv = await buscar_primer_dia(especialidad, dias_adelante=7)
                        if _todos_reinv:
                            _s0r = _todos_reinv[0]
                            _slot_txt_reinv = (
                                f"\n\n📅 *{_s0r.get('fecha_display', '')}* a las "
                                f"*{_s0r.get('hora_inicio', '')[:5]}* con "
                                f"*{_s0r.get('profesional', '')}*"
                            )
                    except Exception:
                        pass
                _nombre_reinv = (data.get("nombre_conocido") or data.get("reg_nombre") or "").split()
                _saludo_reinv = f"*{_nombre_reinv[0]}*" if _nombre_reinv else ""
                _msg_reinv = (
                    f"Hola {_saludo_reinv} — vimos que tu hora"
                    f"{' de *' + especialidad + '*' if especialidad else ''} fue cancelada."
                    f"{_slot_txt_reinv}\n\n"
                    "Si quieres reagendar, escribe *menu* y te ayudamos en un momento."
                )
                canal = _canal_de_phone(phone)
                try:
                    if canal == "wa":
                        from flows import _btn_msg as _btn_msg_reinv
                        _bt_reinv = _btn_msg_reinv(_msg_reinv, [
                            {"id": "menu",          "title": "Reagendar"},
                            {"id": "no_gracias_reeng", "title": "No por ahora"},
                        ])
                        await send_whatsapp_interactive(phone, _bt_reinv["interactive"])
                        log_message(phone, "out", _msg_reinv, state)
                    elif canal == "ig":
                        await send_instagram(phone[3:], _msg_reinv)
                        log_message(phone, "out", _msg_reinv, state)
                    elif canal == "fb":
                        await send_messenger(phone[3:], _msg_reinv)
                        log_message(phone, "out", _msg_reinv, state)
                    data["reenganche_cancelada_sent"] = True
                    save_session(phone, "IDLE", data)
                    log_event(phone, "reenganche_reinvitar_enviado", {"state": state, "canal": canal})
                    log.info("Reenganche reinvitar enviado phone=%s", phone)
                except Exception as _e_send_reinv:
                    log.warning("Reenganche reinvitar send error phone=%s: %s", phone, _e_send_reinv)
                    # Fallback: reset silencioso si el envío falla
                    log_event(phone, "reenganche_skip_cita_cancelada", {"state": state, "reinvitar_error": True})
                    save_session(phone, "IDLE", {})
            else:
                log_event(phone, "reenganche_skip_cita_cancelada", {"state": state})
                log.info("Reenganche skip (cita cancelada) → reset IDLE phone=%s", phone)
                save_session(phone, "IDLE", {})
            continue

        # ── Guardas de no-interferencia (FIX 2026-06-09) ─────────────────────
        #
        # (a) Último mensaje ENTRANTE más reciente que último SALIENTE → el paciente
        #     respondió pero el bot aún no procesó (latencia/bug): NO es abandono.
        # (b) Estado CONFIRMING_CANCEL o CONFIRMING_CITA → decisión terminal en curso.
        #     Un reenganche encima pisa la decisión (caso real: cita quedó activa = no-show).
        # (c) updated_at < 10 min → la sesión sigue activa (cubierta en get_sesiones_abandonadas
        #     pero se defiende aquí también por si el filtro upstream cambia).
        #
        # Loggea reenganche_skip con motivo para auditoría.
        try:
            with _session_conn() as _c_guard:
                _last_in_row = _c_guard.execute(
                    "SELECT ts FROM messages WHERE phone=? AND direction='in' "
                    "ORDER BY ts DESC LIMIT 1",
                    (phone,),
                ).fetchone()
                _last_out_row = _c_guard.execute(
                    "SELECT ts FROM messages WHERE phone=? AND direction='out' "
                    "ORDER BY ts DESC LIMIT 1",
                    (phone,),
                ).fetchone()
            _last_in_ts  = _last_in_row["ts"]  if _last_in_row  else None
            _last_out_ts = _last_out_row["ts"] if _last_out_row else None
            if _last_in_ts and _last_out_ts and _last_in_ts > _last_out_ts:
                log_event(phone, "reenganche_skip",
                          {"motivo": "mensaje_entrante_sin_responder",
                           "last_in": _last_in_ts, "last_out": _last_out_ts, "state": state})
                log.info("Reenganche skip (inbound sin responder) phone=%s state=%s", phone, state)
                continue
        except Exception as _e_guard:
            log.warning("Reenganche guard inbound-check error phone=%s: %s", phone, _e_guard)

        if state in ("CONFIRMING_CANCEL", "CONFIRMING_CITA"):
            log_event(phone, "reenganche_skip",
                      {"motivo": "estado_confirmacion_terminal", "state": state})
            log.info("Reenganche skip (estado terminal) phone=%s state=%s", phone, state)
            continue

        # (d.1) Abono-Gate Psiquiatría: tiene TTL propio de 90 min. El reenganche
        #       no debe mandar "tienes una reserva pendiente" — el paciente está
        #       en proceso de pagar (o venció el plazo). Verificar timeout acá:
        #       si expiró → reset + mensaje especial. Si no → skip sin mensaje
        #       (el próximo mensaje del paciente activa el handler de timeout).
        if state == "WAIT_ABONO_COMPROBANTE":
            _ab_ts_str = data.get("abono_gate_ts", "")
            _ab_expirado = False
            if _ab_ts_str:
                try:
                    from datetime import datetime as _dt_abj
                    from zoneinfo import ZoneInfo as _ZI_abj
                    _ab_dt = _dt_abj.fromisoformat(_ab_ts_str)
                    if _ab_dt.tzinfo is None:
                        _ab_dt = _ab_dt.replace(tzinfo=_ZI_abj("America/Santiago"))
                    _ab_expirado = (_dt_abj.now(_ZI_abj("America/Santiago")) - _ab_dt).total_seconds() > 5400  # 90 min
                except Exception:
                    pass
            if _ab_expirado:
                log_event(phone, "abono_gate_timeout_reenganche", {"gate_ts": _ab_ts_str})
                log.info("Abono-gate expirado → reset IDLE phone=%s", phone)
                save_session(phone, "IDLE", {})
                # Aviso al paciente solo si tiene canal abierto (ventana 24h)
                try:
                    _canal_ab = _canal_de_phone(phone)
                    if _canal_ab != "unknown":
                        _msg_ab = (
                            "El tiempo para enviar el comprobante de tu hora de Psiquiatría venció "
                            "y el aparte fue liberado.\n\n"
                            "Escribe *menu* si quieres volver a buscar una hora."
                        )
                        from messaging import send_whatsapp as _sw_abj
                        import asyncio as _aio_abj
                        _aio_abj.create_task(_sw_abj(phone, _msg_ab))
                        from session import log_message as _lm_abj
                        _lm_abj(phone, "out", _msg_ab, "IDLE")
                except Exception as _e_ab_notif:
                    log.debug("Abono-gate timeout notif error: %s", _e_ab_notif)
            else:
                log_event(phone, "reenganche_skip",
                          {"motivo": "abono_gate_esperando_comprobante", "state": state})
                log.info("Reenganche skip (abono-gate activo) phone=%s", phone)
            continue

        # (d) Estados de OFERTA OPCIONAL post-acción (cross-sell tras reservar,
        #     pregunta de referidos): no hay nada "pendiente" — la reserva ya
        #     quedó hecha. Mandar "tienes una reserva pendiente" acá confunde
        #     (caso real María 2026-06-11: reservó a las 10:02 y a las 10:16 el
        #     bot le dijo que tenía una reserva pendiente). Ignorar la oferta ES
        #     una respuesta válida → reset suave a IDLE, sin mensaje.
        if state in ("WAIT_CROSS_SELL", "WAIT_REFERRAL_POST"):
            log_event(phone, "reenganche_skip",
                      {"motivo": "oferta_opcional_post_accion", "state": state})
            log.info("Reenganche skip (oferta opcional) → reset IDLE phone=%s state=%s", phone, state)
            save_session(phone, "IDLE", data)
            continue

        # Límite de reintentos genérico: máximo 3 skips o TTL 2h desde el primer skip.
        # Cubre condiciones de skip presentes o futuras que no sean cita cancelada.
        # Persistencia: session.data["reenganche_skip_count"] y ["reenganche_first_skip_ts"].
        import time as _time_reeng
        _skip_count = data.get("reenganche_skip_count", 0)
        _first_skip_ts = data.get("reenganche_first_skip_ts")
        _now_ts = _time_reeng.time()
        _ttl_exceeded = _first_skip_ts and (_now_ts - _first_skip_ts) > 7200  # 2h
        if _skip_count >= 3 or _ttl_exceeded:
            log_event(phone, "reenganche_exhausted", {
                "state": state, "skip_count": _skip_count,
                "ttl_exceeded": bool(_ttl_exceeded),
            })
            log.info("Reenganche exhausted → reset IDLE phone=%s skips=%d", phone, _skip_count)
            save_session(phone, "IDLE", {})
            continue

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
        elif state == "WAIT_DURACION_MASOTERAPIA":
            # Aún elige duración (20/40 min): NO hay reserva. No mostrar slot ni
            # decir "reserva pendiente" — confunde al paciente (hallazgo auditoría).
            msg = (
                f"Hola {saludo} 👋 Te quedaste eligiendo la duración de tu "
                "*masoterapia* (20 o 40 min). ¿Seguimos para reservar tu hora?"
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
                log_message(phone, "out", msg, state)
            elif canal == "ig":
                igsid = phone[3:]  # strip "ig_"
                await send_instagram(igsid, msg + "\n\nEscribe *menu* para continuar o *no* si ya no te interesa.")
                log_message(phone, "out", msg, state)
            elif canal == "fb":
                psid = phone[3:]  # strip "fb_"
                await send_messenger(psid, msg + "\n\nEscribe *menu* para continuar o *no* si ya no te interesa.")
                log_message(phone, "out", msg, state)
        except Exception:
            if canal == "wa":
                try:
                    await send_whatsapp_proactive(phone, msg + "\n\nEscribe *menu* para continuar.")
                    log_message(phone, "out", msg, state)
                except Exception:
                    log.exception("Reenganche fallback wa falló phone=%s", phone)
                    continue
            else:
                log.exception("Reenganche %s falló phone=%s", canal, phone)
                continue
        data["reenganche_sent"] = True
        from resilience import get_phone_lock as _gpl_re
        async with _gpl_re(phone):
            save_session(phone, state, data)
        log_event(phone, "reenganche_enviado", {"state": state, "canal": canal})
        log.info("Reenganche enviado → %s (estado: %s, canal: %s)", phone, state, canal)


async def enviar_reagendar_por_cancelacion(id_cita: str, motivo: str = "doctor_cancel",
                                           force: bool = False) -> dict:
    """Envía al paciente 3 slots alternativos tras cancelación del doctor.

    Flujo 1-click: pre-carga los slots en session.data con estado WAIT_SLOT. El
    paciente responde un número y entra directo al flujo existente de confirmación.

    force=True salta el guard de idempotencia sobre cancel_detected_at: lo usa
    _job_detectar_cancelaciones, que marca la cita como detectada ANTES de
    llamar acá (esa marca significa "detectada", no "ya notificada"). Sin
    force, la pre-marca hacía retornar 'ya_notificado' sin enviar nada.

    Retorna: {"ok": bool, "reason": str, "phone": str, "slots_enviados": int}.
    """
    cita = get_cita_bot_by_id_for_rebook(id_cita)
    if not cita:
        return {"ok": False, "reason": "cita_no_encontrada"}
    if cita.get("cancel_detected_at") and not force:
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
        _cancel_no_slots_msg = (
            f"⚠️ Tu hora del {cita.get('fecha','')} {cita.get('hora','')} con "
            f"{cita.get('profesional','')} fue cancelada por el profesional.\n\n"
            f"Por ahora no tenemos horas disponibles en *{esp}*. "
            f"Llámanos para coordinar: 📞 *{CMC_TELEFONO}*"
        )
        await send_whatsapp(phone, _cancel_no_slots_msg)
        log_message(phone, "out", _cancel_no_slots_msg, "IDLE")
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
    # Usar el lock por phone para no pisar sesiones en vuelo del paciente
    from resilience import get_phone_lock as _gpl
    async with _gpl(phone):
        save_session(phone, "WAIT_SLOT", data)

    _cancel_hdr = (
        f"⚠️ *Aviso importante*\n\nTu hora del *{cita.get('fecha','')}* a las "
        f"*{cita.get('hora','')}* con *{cita.get('profesional','')}* fue cancelada "
        f"por el profesional 😔\n\nTe dejo 3 alternativas para reagendar en 1 toque:"
    )
    await send_whatsapp(phone, _cancel_hdr)
    log_message(phone, "out", _cancel_hdr, "WAIT_SLOT")
    from flows import _format_slots
    body = _format_slots(alt_slots)
    if isinstance(body, dict):
        await send_whatsapp_interactive(phone, body)
        log_message(phone, "out", "[interactive: slots alternativos cancelación doctor]", "WAIT_SLOT")
    else:
        await send_whatsapp(phone, body)
        log_message(phone, "out", body, "WAIT_SLOT")

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
    await enviar_recordatorios(send_whatsapp_proactive, send_whatsapp_interactive, send_template_fn=_tpl)
    # Piloto recepción: citas no-bot (si flag activo)
    try:
        await enviar_recordatorios_recepcion_24h(
            send_whatsapp_proactive,
            send_interactive_fn=send_whatsapp_interactive,
            send_template_fn=_tpl,
        )
    except Exception as e:
        log.error("_job_recordatorios_recepcion_24h falló: %s", e)
    # B7: dead-man's switch — si este cron deja de correr, healthchecks.io avisa
    try:
        from alertas_oob import ping_deadman as _ping_dm
        await _ping_dm("RECORDATORIOS")
    except Exception:  # noqa: BLE001
        pass

async def _job_recordatorios_2h():
    await enviar_recordatorios_2h(send_whatsapp_proactive, send_template_fn=_tpl)
    # Piloto recepción: citas no-bot (si flag activo)
    try:
        await enviar_recordatorios_recepcion_2h(send_whatsapp_proactive, send_template_fn=_tpl)
    except Exception as e:
        log.error("_job_recordatorios_recepcion_2h falló: %s", e)

async def _job_postconsulta():
    """Post-consulta diario (cron 22:00 CLT).

    Corre 22:00 CLT (decisión del dueño 2026-06-10): la clínica atiende hasta las
    21:00, así el envío del día alcanza a todos los atendidos. La guardia de ventana
    09:30-22:30 CLT solo protege contra disparos de madrugada (deferred/restarts):
    fuera de ventana programa un one-shot para las 09:30 del día siguiente y termina
    sin enviar — ningún paciente se pierde, solo se difiere. Las citas posteriores a
    las 22:00 quedan para _job_postconsulta_morning (09:00 CLT).
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo
    _TZ_CHILE = ZoneInfo("America/Santiago")
    ahora_clt = datetime.now(_TZ_CHILE)
    hora_mins = ahora_clt.hour * 60 + ahora_clt.minute
    _VENTANA_INI = 9 * 60 + 30   # 09:30
    _VENTANA_FIN = 22 * 60 + 30  # 22:30 — el cron de las 22:00 debe pasar

    if not (_VENTANA_INI <= hora_mins <= _VENTANA_FIN):
        log.info(
            "_job_postconsulta: hora %s fuera de ventana 09:30-22:30 CLT — "
            "difiriendo a las 09:30 de manana (no se pierde ninguna cita)",
            ahora_clt.strftime("%H:%M"),
        )
        try:
            import sys as _sys
            from datetime import timedelta
            _main_mod = _sys.modules.get("app.main") or _sys.modules.get("main")
            _sched = getattr(_main_mod, "scheduler", None)
            if _sched and _sched.running:
                _tomorrow_930 = (ahora_clt + timedelta(days=1)).replace(
                    hour=9, minute=30, second=0, microsecond=0,
                )
                _sched.add_job(
                    _job_postconsulta,
                    "date",
                    run_date=_tomorrow_930,
                    id="postconsulta_deferred",
                    replace_existing=True,
                    timezone=str(_TZ_CHILE),
                )
                log.info("_job_postconsulta: diferido para %s CLT", _tomorrow_930.strftime("%Y-%m-%d %H:%M"))
        except Exception as _e_defer:
            log.warning("_job_postconsulta: no se pudo diferir one-shot: %s", _e_defer)
        return

    try:
        await enviar_seguimiento_postconsulta(
            send_whatsapp_proactive, send_template_fn=_tpl,
            send_text_fn=send_whatsapp_proactive, buscar_paciente_fn=buscar_paciente,
        )
    except Exception as e:
        log.error("_job_postconsulta falló (BUG-07): %s", e)


async def _job_postconsulta_morning():
    """Recoge postconsulta de citas tardías (>22:00) del día anterior.
    Corre 09:00 CLT. Complementa _job_postconsulta de las 22:00."""
    try:
        await enviar_seguimiento_postconsulta_dia_anterior(
            send_whatsapp_proactive, send_template_fn=_tpl,
            send_text_fn=send_whatsapp_proactive, buscar_paciente_fn=buscar_paciente,
        )
    except Exception as e:
        log.exception("Postconsulta morning falló: %s", e)


async def _job_enrolar_atendidos_dia():
    """21:30 CLT — antes del cron postconsulta de las 22:00.

    Tira de Medilink TODAS las atenciones efectivas del día (id_estado 2 o 14)
    y para cada paciente con perfil ya conocido en el bot, inserta una fila en
    citas_bot. Así el cron postconsulta de las 22:00 también alcanza a quienes
    agendaron por recepción/presencial y no pasaron por el bot.

    Pacientes SIN perfil bot (sin opt-in WhatsApp) se loguean a tabla
    `pacientes_sin_optin` para que recepción los enrole con consentimiento
    (Ley 19.628). NO se les manda mensaje automático.
    """
    import asyncio, sqlite3
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from medilink import _q, MEDILINK_SUCURSAL
    from session import db as _conn

    _TZ_CHILE = ZoneInfo("America/Santiago")

    if is_medilink_down():
        log.info("enrolar_atendidos: Medilink down, skip")
        return

    hoy = datetime.now(_TZ_CHILE).date().isoformat()

    # 1) Pull citas atendidas hoy desde Medilink (por profesional, rate-safe)
    atendidos = []
    async with httpx.AsyncClient(timeout=30) as client:
        for id_prof in PROFESIONALES.keys():
            try:
                params = {
                    "id_sucursal":    {"eq": int(MEDILINK_SUCURSAL)},
                    "id_profesional": {"eq": id_prof},
                    "fecha":          {"eq": hoy},
                }
                r = await client.get(
                    f"{MEDILINK_BASE_URL}/citas",
                    params={"q": _q(params)},
                    headers=HEADERS_MEDILINK,
                )
                if r.status_code == 200:
                    for c in r.json().get("data", []):
                        if c.get("id_estado") in (2, 14):
                            atendidos.append(c)
            except Exception as e:
                log.warning("enrolar_atendidos prof=%s: %s", id_prof, e)
            await asyncio.sleep(0.35)

    if not atendidos:
        log.info("enrolar_atendidos %s: sin atenciones", hoy)
        return

    log.info("enrolar_atendidos %s: %d atenciones encontradas", hoy, len(atendidos))

    # 2) Procesar en batches de 50 con commit intermedio para evitar
    # "database is locked" bajo concurrencia (BUG-3 fix 2026-05-18).
    # La transacción larga previa (1 with _conn() para todo el loop) bloqueaba
    # writers del webhook por varios segundos durante el cron de las 21:30.
    _BATCH_SIZE = 50
    _MAX_RETRIES = 3
    nuevos_enrolados = 0
    ya_enrolados = 0
    sin_optin = 0
    sin_celular = 0

    # DDL: asegurar tabla pacientes_sin_optin antes del loop (transacción corta)
    with _conn() as _ddl_conn:
        _ddl_conn.execute("""
            CREATE TABLE IF NOT EXISTS pacientes_sin_optin (
                id_paciente_medilink INTEGER PRIMARY KEY,
                rut                  TEXT,
                nombre               TEXT,
                celular              TEXT,
                primera_atencion     TEXT,
                ultima_atencion      TEXT,
                profesional          TEXT,
                contactado_at        TEXT,
                created_at           TEXT DEFAULT (datetime('now'))
            )
        """)
        _ddl_conn.commit()

    # heatmap_cache.db tiene rut + celular por id_paciente Medilink
    import contextlib as _contextlib
    try:
        heat = sqlite3.connect("/opt/chatbot-cmc/data/heatmap_cache.db")
    except Exception as e:
        log.warning("enrolar_atendidos: no se pudo abrir heatmap_cache: %s", e)
        heat = None

    # Iterar en batches, cada batch abre y cierra su propia conexión.
    # log_event se acumula fuera del with _conn() para evitar "database is locked":
    # log_event abre su propia conexión y compite con la transacción larga.
    for batch_start in range(0, len(atendidos), _BATCH_SIZE):
        batch = atendidos[batch_start:batch_start + _BATCH_SIZE]
        _pending_log_events: list[tuple[str, str, dict]] = []  # (phone, event, payload)
        for _attempt in range(_MAX_RETRIES):
            try:
                with _conn() as conn:
                    for cita in batch:
                        pid_med = cita.get("id_paciente")
                        cita_id = str(cita.get("id"))
                        if not pid_med or not cita_id:
                            continue

                        # Tier A: ¿ya está esta cita en citas_bot?
                        if conn.execute("SELECT 1 FROM citas_bot WHERE id_cita = ? LIMIT 1",
                                        (cita_id,)).fetchone():
                            ya_enrolados += 1
                            continue

                        # Buscar phone conocido por id_paciente_medilink en citas_bot previas
                        row = conn.execute(
                            "SELECT phone FROM citas_bot "
                            "WHERE id_paciente_medilink = ? "
                            "ORDER BY created_at DESC LIMIT 1",
                            (pid_med,)).fetchone()
                        phone = row[0] if row else None

                        # Si no, buscar en heatmap_cache por RUT/celular y cruzar con contact_profiles
                        rut_clean = None
                        celular_med = None
                        if not phone and heat:
                            try:
                                h = heat.execute(
                                    "SELECT rut, celular FROM pacientes_heatmap WHERE id = ?",
                                    (pid_med,)).fetchone()
                            except Exception:
                                h = None
                            if h:
                                rut_h, cel_h = h
                                rut_clean = (rut_h or "").replace(".", "").replace("-", "").upper() or None
                                celular_med = (cel_h or "").strip() or None
                                if rut_clean:
                                    pr = conn.execute(
                                        "SELECT phone FROM contact_profiles "
                                        "WHERE REPLACE(REPLACE(UPPER(rut),'.',''),'-','') = ?",
                                        (rut_clean,)).fetchone()
                                    if pr:
                                        phone = pr[0]

                        if phone:
                            # Tier B: tiene perfil bot → enrolar la cita
                            # Resolver especialidad: Medilink no siempre devuelve
                            # nombre_especialidad legible, así que usamos el dict
                            # PROFESIONALES como fuente de verdad canónica.
                            _id_prof_enrol = cita.get("id_profesional")
                            _esp_enrol = (
                                PROFESIONALES.get(_id_prof_enrol, {}).get("especialidad")
                                or cita.get("nombre_especialidad", "")
                                or ""
                            )
                            _prof_enrol = (
                                PROFESIONALES.get(_id_prof_enrol, {}).get("nombre")
                                or cita.get("nombre_profesional", "")
                                or ""
                            )
                            conn.execute("""
                                INSERT INTO citas_bot
                                    (phone, id_cita, especialidad, profesional, fecha, hora,
                                     paciente_nombre, id_paciente_medilink, modalidad, created_at)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'presencial_enrolado',
                                        datetime('now'))
                            """, (
                                phone, cita_id,
                                _esp_enrol,
                                _prof_enrol,
                                cita.get("fecha", hoy),
                                cita.get("hora_inicio", ""),
                                cita.get("nombre_paciente", ""),
                                pid_med,
                            ))
                            nuevos_enrolados += 1
                            # Acumular fuera del with para no abrir segunda conexión concurrente
                            _pending_log_events.append(
                                (phone, "enrolar_postconsulta_offline",
                                 {"cita_id": cita_id, "fecha": hoy})
                            )
                            # Atribución winback: si phone tiene envío reciente sin cita, marcar.
                            # Se ejecuta fuera del lock SQLite (usa Postgres BI).
                            try:
                                from winback import atribuir_cita_a_winback as _wb_attr_job
                                _wb_attr_job(phone, cita_id)
                            except Exception as _wb_job_err:
                                log.warning("enrolar: atribuir_cita_a_winback error cita=%s phone=%s: %s",
                                            cita_id, (phone or "")[:8], _wb_job_err)
                                log_event(phone or "", "atribucion_winback_error",
                                          {"cita_id": cita_id, "error": str(_wb_job_err)[:200]})
                        else:
                            # Tier C: sin opt-in WhatsApp → tabla pacientes_sin_optin
                            if celular_med:
                                conn.execute("""
                                    INSERT OR REPLACE INTO pacientes_sin_optin
                                        (id_paciente_medilink, rut, nombre, celular,
                                         primera_atencion, ultima_atencion, profesional)
                                    VALUES (?, ?, ?, ?,
                                            COALESCE((SELECT primera_atencion FROM pacientes_sin_optin
                                                      WHERE id_paciente_medilink = ?), ?),
                                            ?, ?)
                                """, (
                                    pid_med, rut_clean,
                                    cita.get("nombre_paciente", ""),
                                    celular_med,
                                    pid_med, hoy,
                                    hoy,
                                    cita.get("nombre_profesional", ""),
                                ))
                                sin_optin += 1
                            else:
                                sin_celular += 1
                    conn.commit()
                # Emitir log_events DESPUÉS de cerrar _conn para no competir con la transacción
                for _le_phone, _le_event, _le_payload in _pending_log_events:
                    try:
                        log_event(_le_phone, _le_event, _le_payload)
                    except Exception as _le_err:
                        log.warning("enrolar_atendidos: log_event falló phone=%s: %s", _le_phone, _le_err)
                _pending_log_events.clear()
                break  # batch procesado OK
            except Exception as _db_err:
                _is_locked = "database is locked" in str(_db_err).lower()
                if _is_locked and _attempt < _MAX_RETRIES - 1:
                    import time as _time
                    _backoff = 0.5 * (2 ** _attempt)
                    log.warning(
                        "enrolar_atendidos: database is locked (intento %d/%d), "
                        "retry en %.1fs batch=%d-%d",
                        _attempt + 1, _MAX_RETRIES, _backoff,
                        batch_start, batch_start + len(batch),
                    )
                    _time.sleep(_backoff)
                else:
                    log.error(
                        "enrolar_atendidos: error en batch %d-%d (intento %d): %s",
                        batch_start, batch_start + len(batch), _attempt + 1, _db_err,
                    )
                    break

    if heat:
        try:
            heat.close()
        except Exception:
            pass

    log.info(
        "enrolar_atendidos %s: nuevos=%d ya_en_citas_bot=%d sin_optin=%d sin_celular=%d",
        hoy, nuevos_enrolados, ya_enrolados, sin_optin, sin_celular,
    )


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

    # Housekeeping Fase 4: vencer holds blandos cuyo TTL pasó sin confirmar.
    # Barato, idempotente y sin red; aprovechamos que este job corre seguido.
    try:
        from session import expire_stale_offers
        _venc = expire_stale_offers()
        if _venc:
            log.info("operativa: %d hold(s) vencido(s) por TTL", _venc)
    except Exception as e:
        log.warning("operativa: expire_stale_offers falló: %s", e)

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

        # Fase 4 (Alma operativa): si el cupo quedó REALMENTE libre (anulada, no
        # reasignada), ofrecerlo a la lista de espera. Gateado internamente por
        # ALMA_OPERATIVA_ENABLED — con el flag apagado esto es un no-op seguro.
        # Nunca dejamos que un fallo acá frene la detección de cancelaciones.
        if anulada:
            try:
                from alma_brain import operativa
                id_prof_slot = cita_ml.get("id_profesional")
                if not id_prof_slot:
                    # Fallback: mapear el nombre del profesional (citas_bot) a su id.
                    _pn = (c.get("profesional") or "").strip().lower()
                    id_prof_slot = next(
                        (pid for pid, p in PROFESIONALES.items()
                         if p.get("nombre", "").strip().lower() == _pn), None)
                await operativa.fill_freed_slot({
                    "especialidad": c.get("especialidad", ""),
                    "id_prof": id_prof_slot,
                    "fecha": c.get("fecha", ""),
                    "hora": c.get("hora", ""),
                    "phone_cancelador": c.get("phone", ""),
                })
            except Exception as e:
                log.warning("operativa: fill_freed_slot falló id=%s: %s", id_cita, e)
            # Bus de eventos (Alma Agents): notifica la cancelación a los agentes
            # reactivos (ej. yield_agenda contacta demanda reprimida de esa esp).
            # No-op seguro si la flota está apagada. Nunca frena la detección.
            try:
                from alma_agents import events
                await events.emit("cita_cancelada", {
                    "especialidad": c.get("especialidad", ""),
                    "fecha": c.get("fecha", ""), "hora": c.get("hora", ""),
                })
            except Exception as e:  # noqa: BLE001
                log.warning("alma_agents: emit cita_cancelada falló id=%s: %s", id_cita, e)

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

    # Disparar reagendamiento automático para las próximas.
    # force=True: el loop de detección de arriba ya marcó cancel_detected_at
    # (esa marca significa "detectada", no "ya notificada"); sin force el guard
    # de enviar_reagendar_por_cancelacion devolvía 'ya_notificado' y el
    # paciente nunca recibía los 3 slots alternativos (bug F005).
    for cp in canceladas_proximas:
        try:
            res = await enviar_reagendar_por_cancelacion(
                str(cp["id_cita"]), motivo="medilink_cancel_detected", force=True
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
    from session import db as _c_b
    from datetime import date, timedelta
    try:
        r1 = await sync_diario()
        log.info("bi_sync_diario atenciones: %s", r1)
    except Exception as e:
        log.warning("bi_sync_diario atenciones fallo: %s", e)
    try:
        # Ventana de 7 días (no solo ayer/hoy): auto-cura huecos de noches en
        # que el sync no corrió (deadlock 2026-06-10, divergencia prod 2026-06-07
        # dejaron 7 y 9-jun sin sincronizar → bi_pagos_caja quedó ~$1.1M corta vs
        # Medilink). force=True hace upsert idempotente por pago_id.
        desde = (date.today() - timedelta(days=7)).isoformat()
        hoy = date.today().isoformat()
        r2 = await sync_pagos_rango(desde=desde, hasta=hoy, force=True)
        log.info("bi_sync_diario pagos: %s", r2)
    except Exception as e:
        log.warning("bi_sync_diario pagos fallo: %s", e)
    # Re-cross COMPLETO últimos 14 días (no solo huérfanos): después del sync
    # de atenciones, los campos total/abonado de atenciones recientes pueden
    # haberse actualizado (Medilink las marca como cobradas), y por tanto el
    # matcher puede reasignar pagos por monto exacto. Respeta overrides
    # manuales por NIVEL 0 del matcher.
    try:
        with _c_b() as c:
            rows = c.execute(
                "SELECT pago_id, fecha, id_paciente, monto, id_profesional "
                "FROM bi_pagos_caja WHERE fecha >= ?",
                ((date.today() - timedelta(days=14)).isoformat(),)
            ).fetchall()
            changed = 0
            recovered = 0
            for r in rows:
                p = {"id": r["pago_id"], "id_paciente": r["id_paciente"],
                     "fecha_recepcion": r["fecha"], "monto_pago": r["monto"]}
                id_prof, aid = _resolver_profesional_pago(c, p)
                if id_prof is None:
                    continue
                if r["id_profesional"] is None:
                    recovered += 1
                elif id_prof != r["id_profesional"]:
                    changed += 1
                else:
                    continue
                c.execute("UPDATE bi_pagos_caja SET id_profesional=?, atencion_id=? "
                          "WHERE pago_id=?", (id_prof, aid, r["pago_id"]))
            log.info("bi_sync_diario rematch: %d reasignados, %d huérfanos recuperados (de %d pagos 14d)",
                     changed, recovered, len(rows))
    except Exception as e:
        log.warning("bi_sync_diario re-cross fallo: %s", e)

    # Repasada: corrige la atribución cruzando con el módulo Pagos (recepción, verificado).
    # Persiste overrides en bi_pagos_caja → corrige también DB Mensual. Conservador.
    try:
        from pagos_medilink_routes import aplicar_repasada
        from datetime import date as _d, timedelta as _td
        rr = aplicar_repasada((_d.today() - _td(days=120)).isoformat(), _d.today().isoformat())
        log.info("bi_sync_diario repasada: revisados=%s con_match=%s corregidos=%s",
                 rr.get("revisados"), rr.get("con_match_manual"), rr.get("corregidos"))
    except Exception as e:
        log.warning("bi_sync_diario repasada fallo: %s", e)


async def _job_bi_sync_intradia():
    """Sync intradía LIGERO: solo pagos del día actual (1 día, sin atenciones ni
    repasada pesada). Corre a las 14:00 y 19:00 CLT para que /cmc/mensual refleje
    el día en curso sin esperar al sync nocturno de 23:59. force=True → upsert
    idempotente por pago_id."""
    from bi_sync import sync_pagos_rango
    from datetime import date
    try:
        hoy = date.today().isoformat()
        r = await sync_pagos_rango(desde=hoy, hasta=hoy, force=True)
        log.info("bi_sync_intradia pagos hoy: %s", r)
    except Exception as e:
        log.warning("bi_sync_intradia fallo: %s", e)


async def _job_pagos_prellenar_intradia():
    """Mantiene la tabla pagos_cmc (panel /alma#pagos) SIEMPRE completa: trae todas
    las citas del día desde Medilink (paginadas) y rellena RUT/prestación/monto.
    Así cualquier recarga del panel ya muestra todos los agendados con su RUT, sin
    depender de que alguien apriete 'Actualizar desde Medilink'. Idempotente:
    nunca pisa filas con cobro/bloqueadas (solo crea faltantes y rellena huecos)."""
    from pagos_routes import prellenar_pagos
    from config import ADMIN_TOKEN
    try:
        # request=None: _require_admin_dep acepta el token directo (guard server-side).
        r = await prellenar_pagos(fecha=None, token=ADMIN_TOKEN, cmc_session=None, request=None)
        log.info("pagos_prellenar_intradia: %s", r)
    except Exception as e:
        log.warning("pagos_prellenar_intradia fallo: %s", e)


async def _job_repasada_historica():
    """Barrido HISTÓRICO completo (semanal): corre la repasada sobre toda la caja
    (desde 2024) para cazar errores de atribución viejos que quedan fuera de la
    ventana de 120 días del job nocturno. Conservador, respeta overrides manuales."""
    try:
        from pagos_medilink_routes import aplicar_repasada
        rr = aplicar_repasada("2024-01-01", _date_today_iso())
        log.info("repasada_historica: revisados=%s con_match=%s corregidos=%s tasa=%s%%",
                 rr.get("revisados"), rr.get("con_match_manual"), rr.get("corregidos"), rr.get("tasa_error_pct"))
    except Exception as e:
        log.warning("repasada_historica fallo: %s", e)


def _date_today_iso():
    from datetime import date as _d
    return _d.today().isoformat()


async def _job_cac_snapshot():
    """Genera el snapshot JSON de atribución/CAC (data/cac_snapshot.json) que
    consume la pestaña Atribución de Autopilot. Reusa scripts/cac_report.py via
    subprocess (aislado del event loop). Llama a Meta Marketing API (~60s), por
    eso es cron y no se calcula en vivo por request. Corre post bi_sync (pagos
    frescos del día)."""
    import asyncio
    import sys as _sys
    from pathlib import Path as _P
    root = _P(__file__).resolve().parent.parent
    out = root / "data" / "cac_snapshot.json"
    script = root / "scripts" / "cac_report.py"
    try:
        proc = await asyncio.create_subprocess_exec(
            _sys.executable, str(script), "--mode", "rolling", "--json", str(out),
            cwd=str(root),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=240)
        except asyncio.TimeoutError:
            proc.kill()
            log.warning("cac_snapshot: timeout (>240s), cancelado")
            return
        output_str = (stdout or b"").decode("utf-8", "replace")
        if proc.returncode == 0:
            # Extraer línea de auditoría de spend (impresa por meta_spend())
            spend_line = next(
                (l for l in output_str.splitlines() if l.startswith("[meta_spend]")),
                None,
            )
            if spend_line:
                log.info("cac_snapshot: generado en %s | %s", out, spend_line)
            else:
                # Sin línea de spend = meta_spend retornó {} (token/config issue)
                # Buscar mensaje de error en output para diagnosarlo
                err_lines = [l for l in output_str.splitlines() if l.startswith("[!]")]
                if err_lines:
                    log.warning("cac_snapshot: generado con spend=0 — %s", "; ".join(err_lines[:3]))
                else:
                    log.info("cac_snapshot: generado en %s (sin datos Meta Ads)", out)
        else:
            tail = output_str[-400:]
            log.warning("cac_snapshot: rc=%s out=%s", proc.returncode, tail)
    except Exception as e:
        log.warning("cac_snapshot: fallo %s", e)


async def _job_reactivacion():
    try:
        await enviar_reactivacion_pacientes(send_whatsapp_proactive, send_template_fn=_tpl)
    except Exception as e:
        log.error("_job_reactivacion falló (BUG-07): %s", e)

async def _job_adherencia_kine():
    try:
        await enviar_adherencia_kine(send_whatsapp_proactive, send_template_fn=_tpl)
    except Exception as e:
        log.error("_job_adherencia_kine falló (BUG-07): %s", e)

async def _job_control_especialidad():
    try:
        await enviar_recordatorio_control(send_whatsapp_proactive, send_template_fn=_tpl)
    except Exception as e:
        log.error("_job_control_especialidad falló (BUG-07): %s", e)

async def _job_crosssell_kine():
    try:
        await enviar_crosssell_kine(send_whatsapp_proactive, send_template_fn=_tpl)
    except Exception as e:
        log.error("_job_crosssell_kine falló (BUG-07): %s", e)

async def _job_crosssell_orl_fono():
    try:
        await enviar_crosssell_orl_fono(send_whatsapp_proactive, send_template_fn=_tpl)
    except Exception as e:
        log.error("_job_crosssell_orl_fono falló (BUG-07): %s", e)

async def _job_crosssell_odonto_estetica():
    try:
        await enviar_crosssell_odonto_estetica(send_whatsapp_proactive, send_template_fn=_tpl)
    except Exception as e:
        log.error("_job_crosssell_odonto_estetica falló (BUG-07): %s", e)

async def _job_crosssell_mg_chequeo():
    try:
        await enviar_crosssell_mg_chequeo(send_whatsapp_proactive, send_template_fn=_tpl)
    except Exception as e:
        log.error("_job_crosssell_mg_chequeo falló (BUG-07): %s", e)

async def _job_crosssell_post_dental_ortodoncia():
    # Patron 5 (2026-05-19): cross-sell ortodoncia 48h despues de cita dental.
    # Cron L-V 11:00 CLT. Template pendiente: crosssell_ortodoncia_post_dental_v1.
    try:
        await enviar_crosssell_post_dental_ortodoncia(send_whatsapp_proactive, send_template_fn=_tpl)
    except Exception as e:
        log.error("_job_crosssell_post_dental_ortodoncia fallo: %s", e)

async def _job_cumpleanos():
    try:
        await enviar_cumpleanos(send_whatsapp_proactive)
    except Exception as e:
        log.error("_job_cumpleanos falló (BUG-07): %s", e)

async def _job_winback():
    try:
        await enviar_winback(send_whatsapp_proactive)
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


async def _job_takeover_pendiente_alert():
    """Alerta al admin cuando hay sesiones HUMAN_TAKEOVER sin respuesta humana
    en tiempo excesivo: >2h en horario hábil (09-20 CLT lun-vie) o >12h fuera.
    Evita que pacientes queden silenciados sin que nadie lo note.
    Se ejecuta cada 30 min (registrado en main.py).
    """
    from zoneinfo import ZoneInfo as _ZI
    from datetime import datetime as _dt, timezone as _tz
    import json as _json

    try:
        from session import _conn as _s_conn
        conn = _s_conn()
        rows = conn.execute(
            "SELECT phone, data, updated_at FROM sessions WHERE state='HUMAN_TAKEOVER'",
        ).fetchall()
        conn.close()
    except Exception as e:
        log.error("takeover_pendiente_alert: error leyendo DB: %s", e)
        return

    now_utc = _dt.now(_tz.utc)
    now_clt = now_utc.astimezone(_ZI("America/Santiago"))
    hora_clt = now_clt.hour
    dow = now_clt.weekday()  # 0=lunes, 6=domingo
    horario_habil = (dow <= 4) and (9 <= hora_clt < 20)  # lun-vie 09-20
    umbral_horas = 2 if horario_habil else 12

    alertas = []
    for row in rows:
        try:
            updated_raw = row["updated_at"] if isinstance(row, dict) else row[2]
            data_raw = row["data"] if isinstance(row, dict) else row[1]
            phone = row["phone"] if isinstance(row, dict) else row[0]
            updated = _dt.fromisoformat(updated_raw)
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=_tz.utc)
            horas_bloqueado = (now_utc - updated).total_seconds() / 3600
            if horas_bloqueado < umbral_horas:
                continue
            try:
                data = _json.loads(data_raw) if isinstance(data_raw, str) else (data_raw or {})
            except Exception:
                data = {}
            human_replied = data.get("human_replied", False)
            if human_replied:
                # Recepcionista ya respondió → no es una sesión abandonada
                continue
            takeover_reason = data.get("takeover_reason") or data.get("handoff_reason")
            if not takeover_reason:
                takeover_reason = f"inactividad >{round(horas_bloqueado, 1)}h"
            takeover_reason = str(takeover_reason)[:80]
            alertas.append({
                "phone": phone,
                "horas": round(horas_bloqueado, 1),
                "razon": takeover_reason,
            })
        except Exception:
            continue

    if not alertas:
        return

    if not ADMIN_ALERT_PHONE:
        log.warning("takeover_pendiente_alert: %d sesiones varadas pero ADMIN_ALERT_PHONE no configurado", len(alertas))
        return

    # Guard ventana 24h: si el Dr. no escribió al bot recientemente, la ventana
    # está cerrada y texto libre genera 131047 en bucle (causa confirmada 2026-05-16).
    if not _admin_window_open():
        log.info("takeover_pendiente_alert: ventana 24h cerrada para ADMIN_ALERT_PHONE — skip envío (%d sesiones varadas)", len(alertas))
        return

    # Enviar alerta consolidada al admin (máx 5 casos en el mensaje)
    lineas = [f"• {a['phone'][-4:]}... · {a['horas']}h · {a['razon']}" for a in alertas[:5]]
    if len(alertas) > 5:
        lineas.append(f"... y {len(alertas) - 5} más")
    cuerpo = (
        f"*Alerta: {len(alertas)} paciente(s) sin respuesta en HUMAN_TAKEOVER "
        f"(>{umbral_horas}h)*\n\n"
        + "\n".join(lineas)
        + "\n\nRevisa el panel para responder."
    )
    try:
        await send_whatsapp(ADMIN_ALERT_PHONE, cuerpo)
        log.info("takeover_pendiente_alert: alerta enviada para %d sesiones", len(alertas))
        from session import log_event as _le
        for a in alertas:
            _le(a["phone"], "takeover_pendiente_alerta", {
                "horas": a["horas"], "razon": a["razon"],
            })
    except Exception as e:
        log.error("takeover_pendiente_alert: no se pudo enviar alerta: %s", e)


async def startup_reservas_huerfanas_check(window_min: int = 60):
    """Corre UNA vez al arrancar el bot (lifespan en main.py): busca eventos
    `reserva_en_vuelo` recientes sin su par `reserva_resultado` — la firma de
    una reserva que murió con el proceso (restart/SIGKILL con el booking a
    Medilink en vuelo, incidente Matías 2026-06-05) — y alerta a recepción.
    NO reintenta la reserva (el slot pudo cambiar y el paciente no recibió
    confirmación): el humano decide. Cada caso se marca con
    `reserva_huerfana_detectada` para no re-alertar en el próximo restart.
    """
    import json as _json
    from session import _conn as _s_conn, log_event as _le

    try:
        conn = _s_conn()
        vuelo = conn.execute(
            "SELECT phone, meta, ts FROM conversation_events "
            "WHERE event='reserva_en_vuelo' AND ts > datetime('now', ?) "
            "ORDER BY ts",
            (f"-{int(window_min)} minutes",),
        ).fetchall()
        cierres = conn.execute(
            "SELECT phone, meta, ts FROM conversation_events "
            "WHERE event IN ('reserva_resultado','reserva_huerfana_detectada') "
            "AND ts > datetime('now', ?)",
            (f"-{int(window_min) + 5} minutes",),
        ).fetchall()
        conn.close()
    except Exception as e:
        log.error("reservas_huerfanas: error leyendo DB: %s", e)
        return

    def _key(row):
        try:
            m = _json.loads(row[1] or "{}")
        except Exception:
            m = {}
        return (row[0], m.get("fecha"), m.get("hora"))

    # Huérfana = en_vuelo sin cierre posterior para el mismo (phone, fecha, hora).
    # ts es 'YYYY-MM-DD HH:MM:SS' → comparación lexicográfica válida.
    huerfanas = {}
    for row in vuelo:
        k = _key(row)
        cerrada = any(_key(c) == k and c[2] >= row[2] for c in cierres)
        if not cerrada:
            huerfanas[k] = row[2]

    if not huerfanas:
        log.info("reservas_huerfanas: 0 huérfanas en los últimos %d min", window_min)
        return

    casos = []
    for (phone, fecha, hora), ts in sorted(huerfanas.items(), key=lambda kv: kv[1]):
        log.warning("reservas_huerfanas: reserva muerta en vuelo phone=…%s %s %s (en_vuelo ts=%s)",
                    str(phone)[-4:], fecha, hora, ts)
        _le(phone, "reserva_huerfana_detectada",
            {"fecha": fecha, "hora": hora, "en_vuelo_ts": ts})
        casos.append({"phone": phone, "fecha": fecha, "hora": hora})

    if not ADMIN_ALERT_PHONE:
        log.warning("reservas_huerfanas: %d casos pero ADMIN_ALERT_PHONE no configurado", len(casos))
        return
    # Guard ventana 24h (mismo patrón que takeover_pendiente_alert): texto libre
    # con ventana cerrada genera 131047 en bucle. Los casos quedan en eventos
    # (reserva_huerfana_detectada) y en los logs igual.
    if not _admin_window_open():
        log.warning("reservas_huerfanas: ventana 24h cerrada — %d casos quedan solo en eventos/logs", len(casos))
        return

    lineas = [f"• …{str(c['phone'])[-4:]} · {c['fecha']} {str(c['hora'] or '')[:5]}" for c in casos[:5]]
    if len(casos) > 5:
        lineas.append(f"… y {len(casos) - 5} más")
    cuerpo = (
        f"🚨 *{len(casos)} reserva(s) perdida(s) en reinicio del bot*\n\n"
        "El paciente confirmó una hora pero el sistema se reinició antes de "
        "crearla en Medilink. El bot NO le respondió:\n\n"
        + "\n".join(lineas)
        + "\n\nContáctalo desde el panel o agéndalo manual."
    )
    try:
        await send_whatsapp(ADMIN_ALERT_PHONE, cuerpo)
        log.info("reservas_huerfanas: alerta enviada a recepción (%d casos)", len(casos))
    except Exception as e:
        log.error("reservas_huerfanas: no se pudo enviar alerta: %s", e)


async def startup_mensajes_huerfanos_check(window_min: int = 30, max_avisos: int = 5):
    """Corre UNA vez al arrancar (encadenado tras startup_reservas_huerfanas_check
    en el lifespan): mensajes del inbox durable que quedaron en 'processing' =
    el proceso murió mientras los procesaba (restart/SIGKILL/crash) y el
    paciente quedó sin respuesta. Le pide al PACIENTE repetir su mensaje (su
    ventana 24h está abierta: acaba de escribir hace <window_min minutos) y
    deja eventos `mensaje_huerfano` para auditoría + alerta a recepción.

    Salvaguardas anti-spam: máx `max_avisos` pacientes por arranque, 1 aviso
    por phone, y la parte saliente se puede apagar con INBOX_RECOVERY_NOTIFY=false
    (los eventos y la alerta a recepción se registran igual).
    """
    import os
    from session import (inbox_stuck, inbox_mark_recovered,
                         log_event as _le, log_message as _lm, get_session as _gs)

    rows = inbox_stuck(window_min)
    if not rows:
        log.info("mensajes_huerfanos: 0 mensajes perdidos en vuelo en los últimos %d min", window_min)
        return

    notify_on = os.getenv("INBOX_RECOVERY_NOTIFY", "true").lower() == "true"
    avisados: set = set()
    casos = []
    for row in rows:
        wamid, phone, texto, ts = row[0], row[1], row[2], row[3]
        log.warning("mensajes_huerfanos: msg muerto en vuelo phone=…%s ts=%s texto=%r",
                    str(phone)[-4:], ts, (texto or "")[:60])
        _le(phone, "mensaje_huerfano", {
            "wamid": wamid, "ts_received": ts, "texto": (texto or "")[:120],
        })
        inbox_mark_recovered(wamid)
        casos.append({"phone": phone, "ts": ts})
        if notify_on and phone not in avisados and len(avisados) < max_avisos:
            try:
                cuerpo = (
                    "Disculpa 🙏 — tuve un reinicio justo cuando me escribiste y tu "
                    "último mensaje quedó sin responder.\n\n"
                    "¿Me lo repites? Seguimos desde donde quedamos."
                )
                await send_whatsapp(phone, cuerpo)
                _lm(phone, "out", cuerpo, _gs(phone).get("state", "IDLE"), canal="whatsapp")
                avisados.add(phone)
            except Exception as e:
                log.error("mensajes_huerfanos: no se pudo avisar a %s: %s", str(phone)[-4:], e)

    # Alerta consolidada a recepción (mismo guard de ventana 24h que
    # reservas_huerfanas / takeover_pendiente_alert).
    if ADMIN_ALERT_PHONE and _admin_window_open():
        lineas = [f"• …{str(c['phone'])[-4:]} · {c['ts']}" for c in casos[:5]]
        if len(casos) > 5:
            lineas.append(f"… y {len(casos) - 5} más")
        try:
            await send_whatsapp(
                ADMIN_ALERT_PHONE,
                f"⚙️ *{len(casos)} mensaje(s) de paciente(s) perdidos en reinicio del bot*\n\n"
                + "\n".join(lineas)
                + f"\n\nA {len(avisados)} ya se les pidió repetir su mensaje. Revisa el panel."
            )
        except Exception as e:
            log.error("mensajes_huerfanos: no se pudo alertar a recepción: %s", e)
    log.info("mensajes_huerfanos: %d casos · %d pacientes avisados", len(casos), len(avisados))


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


async def _job_claude_watchdog():
    """Cada 2 min: si la IA del bot (Claude) está caída y aún no avisamos por esta
    racha, alerta al dueño. Canal primario = Telegram OOB (no depende de WhatsApp
    NI de Anthropic → llega aunque ambos estén caídos); WhatsApp como secundario.
    Cierra el apagón silencioso de saldo (caso 2026-06-29: ~10h sin cerebro)."""
    try:
        from resilience import (should_alert_claude_down, mark_claude_down_alerted,
                                 claude_down_reason, claude_down_since)
        if not should_alert_claude_down():
            return
        reason = claude_down_reason() or "desconocido"
        since = claude_down_since() or "?"
        depth = intent_queue_depth()
        msg = ("🔴 *IA del bot CMC caída*\n"
               f"Causa: {reason}\n"
               f"Desde: {since} UTC · Conversaciones en cola: {depth}\n\n"
               "El bot está respondiendo con menú genérico (no detecta intención).\n"
               "Si es saldo: recargar en console.anthropic.com/settings/billing")
        delivered = False
        # Canal OOB (Telegram) — el más confiable cuando está configurado (no
        # depende de WhatsApp ni Anthropic). Hoy es no-op si faltan los env vars.
        try:
            from alertas_oob import alerta_oob as _oob
            delivered = await _oob(msg)
        except Exception as e:
            log.error("claude watchdog: OOB falló: %s", e)
        if ADMIN_ALERT_PHONE:
            # Template: entrega garantizada fuera de la ventana 24h (igual que el
            # watchdog de Medilink). Reusa alerta_tecnica_admin [hora, cola].
            if USE_TEMPLATES:
                try:
                    await send_whatsapp_template(
                        ADMIN_ALERT_PHONE, "alerta_tecnica_admin",
                        body_params=[since, str(depth)])
                    delivered = True
                except Exception as e:
                    log.error("claude watchdog: template falló: %s", e)
            # Free-text con el detalle (solo llega si hay ventana 24h abierta)
            try:
                await send_whatsapp(ADMIN_ALERT_PHONE, msg)
                delivered = True
            except Exception as e:
                log.error("claude watchdog: WhatsApp texto falló: %s", e)
        # Solo marcamos si al menos un canal entregó → si ambos fallan, reintenta
        if delivered:
            mark_claude_down_alerted()
            log.warning("claude watchdog: dueño alertado — IA caída (%s), cola=%d", reason, depth)
    except Exception as e:
        log.error("_job_claude_watchdog falló inesperadamente: %s", e)


async def _job_cierre_caja_diario():
    """Cada mañana (09:05 CLT) empuja al dueño el cierre de caja del día anterior.
    Pasa por send_whatsapp(ADMIN_ALERT_PHONE) → se espeja solo a Telegram. Datos:
    bi_pagos_caja (ya cuadrada por el sync de las 23:59)."""
    try:
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        import bi_sync
        ayer = datetime.now(ZoneInfo("America/Santiago")).date() - timedelta(days=1)
        d1 = ayer.isoformat(); d2 = (ayer + timedelta(days=1)).isoformat()
        c = bi_sync._bi_conn()
        tot, n = c.execute("SELECT COALESCE(SUM(monto),0), COUNT(*) FROM bi_pagos_caja "
                           "WHERE fecha>=? AND fecha<?", (d1, d2)).fetchone()
        if not n:
            return  # sin pagos ese día → no molestar
        nom = {r[0]: r[1] for r in c.execute("SELECT id_medilink, nombre FROM equipo_cmc").fetchall()}
        top = c.execute("SELECT id_profesional, SUM(monto), COUNT(*) FROM bi_pagos_caja "
                        "WHERE fecha>=? AND fecha<? GROUP BY id_profesional ORDER BY 2 DESC LIMIT 3",
                        (d1, d2)).fetchall()
        clp = lambda x: "$" + format(int(x or 0), ",d").replace(",", ".")
        lines = ["💰 *Cierre de caja* — %s" % ayer.strftime("%d/%m"),
                 "", "Total: *%s*" % clp(tot), "Pagos: *%d*" % int(n), "", "*Top 3 del día*"]
        for idp, s, cnt in top:
            lines.append("• %s — %s (%d)" % (nom.get(idp, "id %s" % idp), clp(s), int(cnt)))
        if ADMIN_ALERT_PHONE:
            await send_whatsapp(ADMIN_ALERT_PHONE, "\n".join(lines))
    except Exception as e:
        log.error("_job_cierre_caja_diario falló: %s", e)


async def _job_agenda_dia():
    """Cada mañana (07:45 CLT) empuja al dueño la agenda del día: cupos totales,
    ocupados y libres por profesional. Se espeja a Telegram vía send_whatsapp.
    Reusa telegram_console.reporte_agenda (rate-limit-safe)."""
    try:
        from telegram_console import reporte_agenda
        txt, _btns = await reporte_agenda()
        if txt and ADMIN_ALERT_PHONE:
            await send_whatsapp(ADMIN_ALERT_PHONE, txt)
    except Exception as e:
        log.error("_job_agenda_dia falló: %s", e)


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
        # B5: canal adicional OOB (Telegram) — si WhatsApp también está caído, igual llega
        if should_notify_reception():
            try:
                from alertas_oob import alerta_oob as _alerta_oob
                depth = intent_queue_depth()
                since = medilink_down_since() or "?"
                await _alerta_oob(
                    f"*Medilink caido* (watchdog CMC)\n"
                    f"Desde: {since} UTC · Cola: {depth} pacientes"
                )
            except Exception:  # noqa: BLE001
                pass
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
                _ok_sr = await send_whatsapp_template(phone_p, "sistema_recuperado")
                if _ok_sr is None:
                    log.error("watchdog: notificación sistema_recuperado FALLÓ (sin wamid) "
                              "→ %s; queda en cola para el próximo ciclo", phone_p)
                    continue
                from messaging import render_template_body as _rtb_sr
                log_message(phone_p, "out", _rtb_sr("sistema_recuperado"), "IDLE")
            else:
                _sr_msg = (
                    "✅ ¡Buenas noticias! Nuestro sistema de citas ya está operativo de nuevo 🎉\n\n"
                    "Si quieres retomar lo que estabas haciendo, escribe *menu* y te ayudo al tiro.\n\n"
                    "_Gracias por tu paciencia._"
                )
                _ok_sr = await send_whatsapp(phone_p, _sr_msg)
                if _ok_sr is None:
                    log.error("watchdog: notificación sistema_recuperado (texto) FALLÓ "
                              "→ %s; queda en cola para el próximo ciclo", phone_p)
                    continue
                log_message(phone_p, "out", _sr_msg, "IDLE")
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
    # Trauma SÍ acumula inscripciones (por si pronto hay traumatólogo); el job NO
    # notifica cupos de trauma (no hay profesional) — ver skip en _job_waitlist_check.
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
    errores_medilink = 0  # Contador de fallos httpx (429 agotado u otro error de red)
    # Bug fix 2026-05-28: antes el job notificaba a TODAS las personas en cola
    # el mismo primer slot disponible (caso eco lun 1-jun 10:00 → 5 personas
    # recibieron el mismo aviso). Ahora cada slot único (fecha+hora) se asigna
    # a UNA sola persona por corrida. Las que no alcancen quedan pendientes
    # para la próxima ejecución del cron diario.
    _slots_consumidos_run = {}
    # Candado anti-doble-mensaje (cron ↔ Fase 4): no le ofrezcas un cupo a quien ya
    # tiene una oferta de cupo VIVA pendiente de respuesta (invitada/apartada/en
    # recepción). Sin esto, una invitación de Fase 4 sin aceptar haría que este cron
    # le mande OTRO cupo al día siguiente. expire_stale_offers() (job de cancelaciones)
    # garantiza que una oferta ignorada no lo bloquee para siempre. Se lee UNA vez.
    try:
        from session import phones_with_open_offers
        _con_oferta_viva = phones_with_open_offers()
    except Exception as _e_oo:
        log.warning("waitlist_check: phones_with_open_offers falló (sigo sin candado): %s", _e_oo)
        _con_oferta_viva = set()
    for row in pendientes:
        wl_id = row["id"]
        phone_p = row["phone"]
        esp = row["especialidad"]
        id_prof_pref = row.get("id_prof_pref")
        nombre = row.get("nombre") or ""
        rut_p = (row.get("rut") or "").strip()

        # Candado anti-doble-mensaje: si ya tiene una oferta de cupo viva, no lo
        # contactamos de nuevo (lo cubre Fase 4 hasta que responda o expire).
        if phone_p in _con_oferta_viva:
            log_event(phone_p, "waitlist_skip_oferta_viva",
                      {"waitlist_id": wl_id, "especialidad": esp})
            log.info("waitlist_check: skip wl_id=%d (ya tiene oferta de cupo viva)", wl_id)
            continue

        # Traumatología: SÍ se acumulan pacientes en la lista (por si pronto hay
        # traumatólogo), pero NO se notifica "se liberó un cupo" — hoy no hay
        # profesional y los slots serían de Medicina General (id 10) mal etiquetados.
        # Mantener la inscripción, solo no notificar en esta corrida.
        if any(k in (esp or "").lower() for k in ("traumatol", "trauma")):
            continue

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
        except httpx.RequestError as e:
            # httpx.RequestError = 429 agotó reintentos u otro fallo de red.
            # Contamos para decidir si reprogramar el job completo.
            errores_medilink += 1
            log.error("waitlist_check: error Medilink buscando slots para %s (%s): %s", phone_p, esp, e)
            continue
        except Exception as e:
            log.error("waitlist_check: error buscando slots para %s (%s): %s", phone_p, esp, e)
            continue

        if not todos:
            continue

        # Buscar primer slot que NO haya sido asignado a otra persona en ESTA corrida.
        # Si todos los slots disponibles ya fueron asignados, esta persona espera
        # a la próxima ejecución del cron.
        primero = None
        for _slot_t in todos:
            _key_slot = (_slot_t.get("fecha"), _slot_t.get("hora_inicio"))
            if _key_slot not in _slots_consumidos_run:
                primero = _slot_t
                _slots_consumidos_run[_key_slot] = True
                break
        if primero is None:
            log.info("waitlist_check: wl_id=%d sin slots libres en esta corrida (otros ya asignados)", wl_id)
            continue

        # Hay slots disponibles → notificar y marcar
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
                from messaging import render_template_body as _rtb_le
                log_message(phone_p, "out",
                            _rtb_le("lista_espera_cupo",
                                    [nombre_corto or "paciente", esp.title(), fecha, hora]),
                            "IDLE")
            else:
                saludo = f"Hola{' ' + nombre_corto if nombre_corto else ''} 👋"
                prof_txt = f" con *{prof_nombre}*" if prof_nombre else ""
                _wl_cupo_msg = (
                    f"{saludo}\n\n"
                    f"¡Buenas noticias! Se liberó un cupo para *{esp.title()}*{prof_txt}.\n\n"
                    f"📅 Primera hora disponible: *{fecha} a las {hora}*\n\n"
                    "Si quieres agendarla escribe *menu* y te ayudo al tiro. "
                    "También puedo buscarte otro horario si ese no te sirve 😊\n\n"
                    "_Te escribimos porque estás en nuestra lista de espera. "
                    "Si ya no la necesitas, ignora este mensaje._"
                )
                await send_whatsapp(phone_p, _wl_cupo_msg)
                log_message(phone_p, "out", _wl_cupo_msg, "IDLE")
            mark_waitlist_notified(wl_id)
            log_event(phone_p, "waitlist_notificado", {
                "waitlist_id": wl_id, "especialidad": esp,
                "fecha": fecha, "hora": hora, "id_prof_pref": id_prof_pref,
            })
            notificados += 1
        except Exception as e:
            log.error("waitlist_check: fallo notificando %s: %s", phone_p, e)

    log.info("waitlist_check: notificados %d/%d pacientes", notificados, len(pendientes))
    # B7: dead-man's switch — si este cron deja de correr, healthchecks.io avisa
    try:
        from alertas_oob import ping_deadman as _ping_dm
        await _ping_dm("WAITLIST")
    except Exception:  # noqa: BLE001
        pass

    # Si Medilink falló en TODOS los intentos de búsqueda de slots (429 agotado
    # u otro error de red), reprogramar el job en 30 minutos para no perder
    # el ciclo diario completo. Solo reintenta una vez (evita loop infinito).
    if errores_medilink > 0 and errores_medilink >= len(pendientes) and notificados == 0:
        try:
            from datetime import datetime as _dt, timedelta as _td
            import sys as _sys
            _main_mod = _sys.modules.get("app.main") or _sys.modules.get("main")
            _sched = getattr(_main_mod, "scheduler", None)
            if _sched and _sched.running:
                _retry_at = _dt.now() + _td(minutes=30)
                _sched.add_job(
                    _job_waitlist_check,
                    "date",
                    run_date=_retry_at,
                    id="waitlist_check_retry",
                    replace_existing=True,
                )
                log.warning(
                    "waitlist_check: todos los pendientes fallaron por Medilink 429 "
                    "(%d/%d) — reprogramando retry en 30 min (%s)",
                    errores_medilink, len(pendientes), _retry_at.strftime("%H:%M"),
                )
        except Exception as _e_retry:
            log.warning("waitlist_check: no se pudo reprogramar retry: %s", _e_retry)


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
        from resilience import is_medilink_down, is_claude_down, claude_down_reason
        from session import db as _conn

        ahora = datetime.now(ZoneInfo("America/Santiago")).strftime("%H:%M")
        stats = get_stats_429()
        total_429 = stats.get("total", 0)
        delta_429 = total_429 - _admin_report_state["last_429_total"]
        _admin_report_state["last_429_total"] = total_429

        medilink_down = is_medilink_down()
        claude_down = is_claude_down()
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

        # Semáforo — Claude caído es 🔴: la IA es dependencia crítica por mensaje.
        ok = (not medilink_down and not claude_down and sched_running
              and sched_jobs > 0 and delta_429 < 5)
        icono = "🟢" if ok else ("🔴" if (medilink_down or claude_down) else "🟡")
        med_line = "DOWN" if medilink_down else "ok"
        if claude_down:
            ia_line = f"🔴 *DOWN — {claude_down_reason()}*"
        else:
            ia_line = "ok"
        alert = "" if ok else "\n⚠️ *Revisar*"

        body = (
            f"{icono} *CMC bot · {ahora}*\n\n"
            f"IA (Claude): {ia_line}\n"
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
        from session import db as _sc_heatmap

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
                "SELECT COUNT(DISTINCT phone) FROM contact_tags WHERE tag='arauco'"
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
    # Gobernable desde la Sala de Máquinas (flag agregado 2026-06-11; antes
    # corría sin flag, invisible). Default ON para preservar el comportamiento.
    import os as _os_hv
    _env_hv = _os_hv.getenv("HORAS_VACIAS_ACTIVE", "true").lower() in ("true", "1", "yes")
    try:
        from alma_switchboard import effective as _sb_eff_hv
        if not _sb_eff_hv("HORAS_VACIAS_ACTIVE", _env_hv):
            log.debug("horas_vacias: HORAS_VACIAS_ACTIVE=false — skip")
            return
    except Exception:
        if not _env_hv:
            return
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

    # Exclusión "ya se atendió" (pedido dueño 2026-06-11): si la persona tuvo una
    # atención REAL en los últimos 30 días (aunque haya agendado por recepción,
    # que el bot no ve), su necesidad probablemente ya se resolvió — no avisarle.
    # Fuente: bi.fact_atenciones (Postgres), match por últimos 9 dígitos.
    _atendidos_r9: set = set()
    try:
        from winback import bi_conn as _bi_hv
        with _bi_hv() as _cbi_hv:
            with _cbi_hv.cursor() as _cur_hv:
                _cur_hv.execute(
                    "SELECT DISTINCT RIGHT(regexp_replace(dp.telefono, '[^0-9]', '', 'g'), 9) "
                    "FROM bi.fact_atenciones fa "
                    "JOIN bi.dim_paciente dp ON dp.paciente_id = fa.paciente_id "
                    "WHERE fa.fecha >= CURRENT_DATE - 30 AND dp.telefono IS NOT NULL")
                _atendidos_r9 = {r[0] for r in _cur_hv.fetchall() if r[0]}
        log.info("horas_vacias: %d teléfonos con atención <30d (excluidos)", len(_atendidos_r9))
    except Exception as _e_hv_at:
        log.warning("horas_vacias: filtro atendidos no disponible (%s) — sigo sin él", _e_hv_at)

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
        # Anti-encimoso (caso Nataly 2026-06-11): si el bot le OFRECIÓ un slot en
        # las últimas 24h y lo declinó, no mandarle este aviso el mismo día.
        try:
            from session import _conn as _conn_hv
            with _conn_hv() as _c_hv:
                _recien_ofrecidos = {r[0] for r in _c_hv.execute(
                    "SELECT DISTINCT phone FROM conversation_events "
                    "WHERE event='funnel_slot_ofrecido' AND ts >= datetime('now','-1 day')"
                ).fetchall()}
            _n_antes = len(candidatos)
            candidatos = [p for p in candidatos if p not in _recien_ofrecidos
                          and p[-9:] not in _atendidos_r9]
            if _n_antes != len(candidatos):
                log.info("horas_vacias: %s → %d candidatos excluidos (slot ofrecido <24h o atendido <30d)",
                         especialidad_label, _n_antes - len(candidatos))
        except Exception as _e_hv_excl:
            log.warning("horas_vacias: filtro slot-reciente falló: %s", _e_hv_excl)
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
                f"mañana {manana_display}. La primera disponible es a las {hora_ejemplo}.\n\n"
                f"Si te interesa agendar, responde *Sí* y te ayudo.\n\n"
                f"Si no quieres recibir más avisos de este tipo, responde *No avisar*."
            )

            try:
                await send_whatsapp_proactive(phone, texto)
                log_message(phone, "out", texto, "IDLE")
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
                await send_whatsapp_proactive(phone, msg)
                log_message(phone, "out", msg, "IDLE")
            elif canal == "ig":
                # strip "ig_" — send_instagram recibe el igsid sin prefijo
                await send_instagram(phone[3:], msg)
                log_message(phone, "out", msg, "IDLE")
            elif canal == "fb":
                # strip "fb_" — send_messenger recibe el psid sin prefijo
                await send_messenger(phone[3:], msg)
                log_message(phone, "out", msg, "IDLE")
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


# ── Notificaciones automáticas a profesionales (best practices 2026-05) ───────

async def _job_resumen_diario_profesionales():
    """Lun-Sáb 07:00 CLT: para cada profesional con permiso `resumen_diario_07`
    activo y dentro de ventana 24h, envía el listado de pacientes del día.

    Best practice: el profesional planifica su mañana sabiendo a quién verá,
    cuál es nuevo vs control, y si hay confirmaciones pendientes. Resuelve
    el dolor de Jorge Montalba (2026-05-11) — paciente que apareció en agenda
    de un día para otro sin aviso.
    """
    try:
        import prof_notifications as pn
        from medilink import obtener_agenda_dia, PROFESIONALES
        from datetime import date as _date
        from resilience import spawn_task

        hoy = _date.today().strftime("%Y-%m-%d")
        dias = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
        hoy_label = dias[_date.today().weekday()]

        for id_prof, wa_phone in pn.PROF_ID_TO_PHONE.items():
            if not pn._tiene_permiso(wa_phone, "resumen_diario_07"):
                continue
            if not pn._dentro_ventana_24h(wa_phone):
                pn.log_event(wa_phone, "prof_notif_skipped", {
                    "event_type": "resumen_diario", "razon": "fuera_ventana_24h",
                    "feature": "resumen_diario_07", "id_prof": id_prof,
                })
                continue

            try:
                agenda = await obtener_agenda_dia(id_prof, hoy)
            except Exception as e:
                log.warning("resumen_diario: error agenda prof=%d: %s", id_prof, e)
                continue

            prof_info = PROFESIONALES.get(id_prof, {})
            nombre_corto = pn._primer_nombre(prof_info.get("nombre", ""))

            if not agenda:
                texto = (
                    f"☀️ *Buenos días, {nombre_corto}*\n\n"
                    f"Hoy {hoy_label} no tienes pacientes agendados.\n\n"
                    f"_Día libre — buena oportunidad para revisar tu dashboard "
                    f"o atender lista de espera._"
                )
            else:
                lineas = [f"☀️ *Buenos días, {nombre_corto}*\n",
                          f"Tu agenda del {hoy_label}:"]
                for c in agenda:
                    hora = c.get("hora", c.get("hora_inicio", ""))[:5]
                    pac = c.get("paciente", {})
                    pac_nombre = pac.get("nombre", "—") if isinstance(pac, dict) else str(pac)
                    edad = pac.get("edad", "") if isinstance(pac, dict) else ""
                    edad_str = f" ({edad}a)" if edad else ""
                    lineas.append(f"   🕐 {hora}  ·  {pac_nombre}{edad_str}")
                lineas.append("")
                lineas.append(f"Total: *{len(agenda)} pacientes*")
                lineas.append("\n_Responde *agenda* en cualquier momento para ver tu día actualizado._")
                texto = "\n".join(lineas)

            try:
                from messaging import send_whatsapp as _swa
                await _swa(wa_phone, texto)
                pn.log_event(wa_phone, "prof_notif_sent", {
                    "event_type": "resumen_diario",
                    "feature": "resumen_diario_07",
                    "id_prof": id_prof,
                    "n_pacientes": len(agenda),
                })
                log.info("resumen_diario enviado a prof=%d (%d pacientes)",
                         id_prof, len(agenda))
            except Exception as e:
                log.error("resumen_diario send error prof=%d: %s", id_prof, e)
    except Exception as e:
        log.error("_job_resumen_diario_profesionales fallo: %s", e)


async def _job_resumen_semanal_profesionales():
    """Domingo 19:00 CLT: para cada profesional con permiso `resumen_semanal_dom`
    activo y dentro de ventana 24h, envía resumen de pacientes confirmados
    para la semana que empieza al día siguiente (lun-sáb).

    Útil especialmente para psicología/kine/nutrición donde el profesional
    planifica con visión semanal.
    """
    try:
        import prof_notifications as pn
        from medilink import obtener_agenda_dia, PROFESIONALES
        from datetime import date as _date, timedelta as _td

        # Domingo a la noche → lunes próximo
        lunes = _date.today() + _td(days=1)
        dias_es = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado"]

        for id_prof, wa_phone in pn.PROF_ID_TO_PHONE.items():
            if not pn._tiene_permiso(wa_phone, "resumen_semanal_dom"):
                continue
            if not pn._dentro_ventana_24h(wa_phone):
                pn.log_event(wa_phone, "prof_notif_skipped", {
                    "event_type": "resumen_semanal", "razon": "fuera_ventana_24h",
                    "feature": "resumen_semanal_dom", "id_prof": id_prof,
                })
                continue

            prof_info = PROFESIONALES.get(id_prof, {})
            nombre_corto = pn._primer_nombre(prof_info.get("nombre", ""))

            lineas_dias = []
            total_semana = 0
            for offset in range(6):  # lun..sáb
                fecha = lunes + _td(days=offset)
                fecha_str = fecha.strftime("%Y-%m-%d")
                try:
                    agenda = await obtener_agenda_dia(id_prof, fecha_str)
                except Exception as e:
                    log.warning("resumen_semanal: error agenda prof=%d %s: %s",
                                id_prof, fecha_str, e)
                    agenda = []
                if not agenda:
                    lineas_dias.append(
                        f"   *{dias_es[offset].capitalize()} {fecha.day}*  ·  sin pacientes"
                    )
                    continue
                total_semana += len(agenda)
                lineas_dias.append(
                    f"   *{dias_es[offset].capitalize()} {fecha.day}*  ·  {len(agenda)} paciente{'s' if len(agenda)!=1 else ''}"
                )
                for c in agenda[:8]:  # máx 8 por día en el resumen
                    hora = c.get("hora", c.get("hora_inicio", ""))[:5]
                    pac = c.get("paciente", {})
                    pac_nombre = pac.get("nombre", "—") if isinstance(pac, dict) else str(pac)
                    lineas_dias.append(f"      🕐 {hora}  ·  {pac_nombre}")
                if len(agenda) > 8:
                    lineas_dias.append(f"      _… y {len(agenda)-8} más_")

            texto = (
                f"📋 *Tu semana, {nombre_corto}*\n\n"
                f"Semana del {lunes.day}/{lunes.month}:\n"
                + "\n".join(lineas_dias)
                + f"\n\nTotal semana: *{total_semana} pacientes*"
                + "\n\n_Responde *agenda* cualquier día para ver detalle actualizado._"
            )
            try:
                from messaging import send_whatsapp as _swa
                await _swa(wa_phone, texto)
                pn.log_event(wa_phone, "prof_notif_sent", {
                    "event_type": "resumen_semanal",
                    "feature": "resumen_semanal_dom",
                    "id_prof": id_prof,
                    "total_pacientes": total_semana,
                })
                log.info("resumen_semanal enviado a prof=%d (%d pacientes)",
                         id_prof, total_semana)
            except Exception as e:
                log.error("resumen_semanal send error prof=%d: %s", id_prof, e)
    except Exception as e:
        log.error("_job_resumen_semanal_profesionales fallo: %s", e)


async def _job_no_show_check():
    """Cada 30 min, 09:00-21:00 CLT: detecta citas que ya pasaron hora_inicio + 30 min
    sin marcar como atendidas → notif_no_show al profesional con permiso activo.

    Best practice: el profesional decide si insistir al paciente o liberar el
    cupo a lista de espera. Mejor que descubrirlo al día siguiente.
    """
    try:
        import asyncio
        import prof_notifications as pn
        from medilink import obtener_agenda_dia, PROFESIONALES
        from datetime import datetime as _dt, date as _date
        from zoneinfo import ZoneInfo as _ZI
        from resilience import spawn_task

        ahora_cl = _dt.now(_ZI("America/Santiago"))
        hoy_str = ahora_cl.date().strftime("%Y-%m-%d")
        ahora_h = ahora_cl.hour
        ahora_m = ahora_cl.minute

        for id_prof, wa_phone in pn.PROF_ID_TO_PHONE.items():
            if not pn._tiene_permiso(wa_phone, "notif_no_show"):
                continue
            try:
                agenda = await obtener_agenda_dia(id_prof, hoy_str)
            except Exception:
                await asyncio.sleep(0.3)
                continue
            prof_info = PROFESIONALES.get(id_prof, {})
            prof_nombre = prof_info.get("nombre", "")
            for c in agenda:
                hora_str = c.get("hora", c.get("hora_inicio", ""))[:5]
                if not hora_str or ":" not in hora_str:
                    continue
                try:
                    hh, mm = map(int, hora_str.split(":"))
                except Exception:
                    continue
                # 30 min después de la hora_inicio
                pasaron_min = (ahora_h - hh) * 60 + (ahora_m - mm)
                if pasaron_min < 30 or pasaron_min > 90:
                    # ventana de detección: entre 30 y 90 min después de la hora.
                    continue
                # Solo si Medilink NO marca como atendido. Heurística: campo
                # estado != 'atendido'/'finalizado'. Si Medilink no expone esto
                # aún, igual notificamos — el profesional sabe si llegó o no.
                estado = (c.get("estado") or "").lower()
                if any(k in estado for k in ("atend", "final", "complet")):
                    continue
                pac = c.get("paciente", {})
                pac_nombre = pac.get("nombre", "") if isinstance(pac, dict) else ""
                id_cita = str(c.get("id", ""))
                spawn_task(pn.notify_no_show(
                    id_prof=id_prof,
                    profesional_nombre=prof_nombre,
                    paciente_nombre=pac_nombre,
                    hora=hora_str,
                    id_cita=id_cita,
                ), name=f"prof_notif_no_show_{id_prof}_{id_cita}")
            # Throttle: 0.3s entre profesionales para no saturar Medilink con
            # hasta ~24 requests simultáneos (290 errores 429 en 1min, 2026-06-09)
            await asyncio.sleep(0.3)
    except Exception as e:
        log.error("_job_no_show_check fallo: %s", e)


async def _job_recordatorios_48h():
    """Recordatorio 48h anti no-show: diario 10:00 CLT.
    Solo envía a pacientes con historial de no-show o cita en peak 16-19h.
    """
    try:
        await enviar_recordatorios_48h(send_whatsapp_proactive, send_interactive_fn=send_whatsapp_interactive)
    except Exception as e:
        log.error("_job_recordatorios_48h falló: %s", e)
    # Piloto recepción: enviar también a citas no-bot (si flag activo)
    try:
        await enviar_recordatorios_recepcion_48h(
            send_whatsapp_proactive, send_interactive_fn=send_whatsapp_interactive
        )
    except Exception as e:
        log.error("_job_recordatorios_recepcion_48h falló: %s", e)


async def _job_sync_citas_recepcion():
    """Descarga citas futuras de Medilink para RECORDATORIOS_RECEPCION_PROF_IDS,
    resuelve el celular del paciente y puebla citas_recepcion_reminders.
    Con RECORDATORIOS_RECEPCION_ENABLED=false termina inmediatamente — cero efecto.
    """
    if not RECORDATORIOS_RECEPCION_ENABLED:
        log.debug("_job_sync_citas_recepcion: flag OFF → skip")
        return

    import asyncio
    from datetime import date, timedelta as _td
    from medilink import _get   # wrapper con semáforo global + reintentos 429

    hoy = date.today()
    # Semana completa por delante (+1..+7): la "base de datos de la agenda de la
    # semana" que se refresca cada noche. range(1,4) cubría solo 24h/48h; con 7
    # días la tabla queda lista para toda la semana (y da redundancia si una noche
    # de sync falla). Corre off-peak (05:30 CLT) para no competir con el bot.
    fechas = [(hoy + _td(days=d)).isoformat() for d in range(1, 8)]

    # Caché de celular por id_paciente dentro de esta corrida: con 7 días un mismo
    # paciente aparece en varias citas → evita re-pedir su ficha cada vez.
    _phone_cache: dict[int, tuple[str | None, str]] = {}

    _HEADERS = {
        "Authorization": f"Token {MEDILINK_TOKEN}",
        "Content-Type":  "application/json",
        "Accept":        "application/json",
    }

    import json as _json

    def _q(params: dict) -> str:
        # Medilink rechaza JSON con espacios — separadores compactos (igual que medilink._q).
        return _json.dumps(params, separators=(",", ":"))

    from config import MEDILINK_SUCURSAL

    async with httpx.AsyncClient(timeout=15) as client:
        for id_prof in RECORDATORIOS_RECEPCION_PROF_IDS:
            prof_info    = PROFESIONALES.get(id_prof, {})
            prof_nombre  = prof_info.get("nombre", f"Prof {id_prof}")
            especialidad = prof_info.get("especialidad", "Medicina General")

            for fecha in fechas:
                # Throttle entre llamadas /citas: con muchos profesionales el sync
                # hacía requests back-to-back y 429-tormentaba Medilink en hora peak
                # (08:00), degradando al bot en vivo. 0.7s deja ~72 llamadas en ~50s.
                await asyncio.sleep(0.7)
                params = {
                    "id_sucursal":      {"eq": MEDILINK_SUCURSAL},
                    "id_profesional":   {"eq": id_prof},
                    "fecha":            {"eq": fecha},
                    "estado_anulacion": {"eq": 0},
                }
                try:
                    # _get: semáforo global + reintentos 429 (3/6/12s). Antes era
                    # client.get crudo que ante 429 SALTABA la fecha sin reintentar
                    # → pacientes sin recordatorio en silencio.
                    r = await _get(
                        client,
                        f"{MEDILINK_BASE_URL}/citas",
                        params={"q": _q(params)},
                        headers=_HEADERS,
                    )
                except Exception as e:
                    log.warning("sync_recepcion prof=%d fecha=%s: %s", id_prof, fecha, e)
                    continue

                if r.status_code != 200:
                    log.warning("sync_recepcion prof=%d fecha=%s: HTTP %d", id_prof, fecha, r.status_code)
                    continue

                citas_raw = r.json().get("data", [])
                citas_a_insertar = []
                for c in citas_raw:
                    id_pac = c.get("id_paciente")
                    if not id_pac:
                        continue

                    # Resolver celular del paciente vía /pacientes/{id} (cacheado
                    # por corrida: el mismo paciente puede tener varias citas en la
                    # semana → una sola consulta de ficha).
                    if id_pac in _phone_cache:
                        phone_resuelto, phone_source = _phone_cache[id_pac]
                        citas_a_insertar.append({
                            "id_cita_medilink": c["id"],
                            "id_profesional":   id_prof,
                            "id_paciente":      id_pac,
                            "paciente_nombre":  (c.get("nombre_paciente") or "").strip(),
                            "especialidad":     especialidad,
                            "profesional":      prof_nombre,
                            "fecha":            fecha,
                            "hora":             (c.get("hora_inicio") or "")[:5],
                            "phone":            phone_resuelto,
                            "phone_source":     phone_source,
                        })
                        continue
                    phone_resuelto = None
                    phone_source   = "sin_celular"
                    try:
                        rp = await _get(
                            client,
                            f"{MEDILINK_BASE_URL}/pacientes/{id_pac}",
                            headers=_HEADERS,
                        )
                        if rp.status_code == 200:
                            p = rp.json().get("data", {})
                            if isinstance(p, list):
                                p = p[0] if p else {}
                            # Prioridad: celular > telefono_movil > telefono
                            cel_raw = (
                                p.get("celular")
                                or p.get("telefono_movil")
                                or p.get("telefono")
                                or ""
                            )
                            cel_raw = str(cel_raw).strip().replace(" ", "").replace("-", "")
                            if cel_raw:
                                if cel_raw.startswith("+"):
                                    cel_raw = cel_raw[1:]
                                if cel_raw.startswith("9") and len(cel_raw) == 9:
                                    cel_raw = "56" + cel_raw
                                elif cel_raw.startswith("56") and len(cel_raw) == 11:
                                    pass
                                elif len(cel_raw) == 9:
                                    cel_raw = "56" + cel_raw
                                if cel_raw.startswith("56") and len(cel_raw) == 11:
                                    phone_resuelto = cel_raw
                                    phone_source   = "medilink_celular"
                    except httpx.RequestError as e:
                        log.debug("sync_recepcion pac_id=%d celular error: %s", id_pac, e)

                    await asyncio.sleep(0.5)  # anti-429 entre llamadas a /pacientes

                    _phone_cache[id_pac] = (phone_resuelto, phone_source)
                    citas_a_insertar.append({
                        "id_cita_medilink": c["id"],
                        "id_profesional":   id_prof,
                        "id_paciente":      id_pac,
                        "paciente_nombre":  (c.get("nombre_paciente") or "").strip(),
                        "especialidad":     especialidad,
                        "profesional":      prof_nombre,
                        "fecha":            fecha,
                        "hora":             (c.get("hora_inicio") or "")[:5],
                        "phone":            phone_resuelto,
                        "phone_source":     phone_source,
                    })

                if citas_a_insertar:
                    n = upsert_citas_recepcion(citas_a_insertar)
                    log.info(
                        "sync_recepcion prof=%d fecha=%s → %d citas, %d upserted",
                        id_prof, fecha, len(citas_a_insertar), n,
                    )
                else:
                    log.debug("sync_recepcion prof=%d fecha=%s → sin citas", id_prof, fecha)


async def _job_crosssell_dx():
    """Cross-sell contextual por dx tags (dm2, hta, gineco/PAP).
    CROSS_SELL_ACTIVE=false hasta piloto N=5 confirmado por Rodrigo.
    """
    try:
        await enviar_crosssell_dx(send_whatsapp_proactive, send_template_fn=_tpl)
    except Exception as e:
        log.error("_job_crosssell_dx falló: %s", e)


async def _job_marketing_consent_blast():
    """Blast diario L-V 10:30 CLT: envía consent_marketing_v2 (UTILITY)
    a phones en v_winback_cohortes_contactables sin registro en marketing_consent.

    MARKETING_CONSENT_BLAST_ACTIVE=false hasta que Rodrigo confirme
    que consent_marketing_v2 está APPROVED en Meta.
    """
    import os as _osc
    if not _osc.getenv("MARKETING_CONSENT_BLAST_ACTIVE", "false").lower() in ("true", "1", "yes"):
        log.debug("_job_marketing_consent_blast: MARKETING_CONSENT_BLAST_ACTIVE=false — skip")
        return
    try:
        from winback import (
            is_template_approved,
            registrar_consent_enviado,
            bi_conn,
        )
        import asyncio as _asyncio_mc
        from datetime import datetime as _dt_mc

        # Verificar template aprobado
        if not await is_template_approved("consent_marketing_v2"):
            log.warning("consent_template_not_approved: consent_marketing_v2 no está APPROVED en Meta — skip")
            return

        # Ventana horaria L-V 10:30-19:00
        _now_cl = _dt_mc.now(__import__("zoneinfo").ZoneInfo("America/Santiago"))
        if _now_cl.weekday() >= 5:
            log.debug("_job_marketing_consent_blast: fin de semana — skip")
            return
        if not (10 <= _now_cl.hour < 19):
            log.debug("_job_marketing_consent_blast: fuera de ventana horaria — skip")
            return

        LIMITE_DIA = 200
        SLEEP_ENTRE = 30

        # Contar enviados hoy
        with bi_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM bi.marketing_consent "
                    "WHERE DATE(consent_sent_at AT TIME ZONE 'America/Santiago') = CURRENT_DATE"
                )
                enviados_hoy = cur.fetchone()[0]

        if enviados_hoy >= LIMITE_DIA:
            log.info("consent_blast: límite diario %d alcanzado", LIMITE_DIA)
            return

        # Candidatos: en cohortes_contactables pero sin registro consent.
        # La dedup se hace en Python sobre el teléfono NORMALIZADO, no con
        # NOT IN en SQL: el formato guardado varía ('+56...', '56...', '9...')
        # y un NOT IN crudo re-enviaría a quien ya recibió el consent (y antes
        # tampoco matcheaba la respuesta). Comparar canónico vs canónico es
        # robusto incluso a las filas históricas sin backfill.
        from session import normalize_wa_id as _norm_ph
        with bi_conn() as conn2:
            with conn2.cursor() as cur:
                cur.execute("SELECT phone FROM bi.marketing_consent")
                ya_enviados = {_norm_ph(r[0]) for r in cur.fetchall()}
                cur.execute(
                    "SELECT wc.telefono, wc.nombre FROM bi.v_winback_cohortes_contactables wc"
                )
                candidates = []
                cupo = LIMITE_DIA - enviados_hoy
                for _tel, _nombre in cur.fetchall():
                    _tel_norm = _norm_ph(_tel)
                    if _tel_norm in ya_enviados:
                        continue
                    candidates.append((_tel_norm, _nombre or "Paciente"))
                    if len(candidates) >= cupo:
                        break

        log.info("consent_blast: %d candidatos a enviar hoy", len(candidates))
        enviados = 0
        for phone, nombre in candidates:
            # Re-verificar ventana en cada iteración
            _now_loop = _dt_mc.now(__import__("zoneinfo").ZoneInfo("America/Santiago"))
            if not (10 <= _now_loop.hour < 19) or _now_loop.weekday() >= 5:
                log.info("consent_blast: ventana cerrada — detenido en %d enviados", enviados)
                break
            try:
                await send_whatsapp_template(
                    phone,
                    "consent_marketing_v2",
                    body_params=[nombre],
                )
                from messaging import render_template_body as _rtb_cm
                log_message(phone, "out", _rtb_cm("consent_marketing_v2", [nombre]), "IDLE")
                registrar_consent_enviado(phone)
                enviados += 1
                log.info("consent_blast enviado → %s (%d/%d)", phone, enviados, len(candidates))
            except Exception as e:
                log.error("consent_blast error phone=%s: %s", phone, e)
            await _asyncio_mc.sleep(SLEEP_ENTRE)

        log.info("consent_blast: sesión completada, enviados=%d", enviados)
    except Exception as e:
        log.error("_job_marketing_consent_blast falló: %s", e)


async def _job_consent_agendados(dry_run: bool = False) -> dict:
    """Barrido HORARIO: manda consent_marketing_v2 a pacientes agendados por
    TELÉFONO/PRESENCIAL (NO por el bot) sin consent ni opt-out — captura consent
    EN CALIENTE del que recién reservó por recepción.

    'Del bot' = id_cita en citas_bot (exacto) o teléfono ya en citas_bot del día
    (cubre el caso niño agendado por el papá: el número del papá ya está).
    Gated CONSENT_AGENDADOS_ACTIVE. Cap CONSENT_AGENDADOS_CAP (default 30/run)."""
    import os as _os
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo as _ZI
    _env_consent_ag = _os.getenv("CONSENT_AGENDADOS_ACTIVE", "false").lower() in ("true", "1", "yes")
    try:
        from alma_switchboard import effective as _sb_eff
        _consent_ag_on = _sb_eff("CONSENT_AGENDADOS_ACTIVE", _env_consent_ag)
    except Exception:
        _consent_ag_on = _env_consent_ag
    if not dry_run and not _consent_ag_on:
        log.debug("_job_consent_agendados: CONSENT_AGENDADOS_ACTIVE=false — skip")
        return {"status": "inactive"}
    now_cl = _dt.now(_ZI("America/Santiago"))
    if not dry_run and (now_cl.weekday() >= 5 or not (9 <= now_cl.hour < 20)):
        return {"status": "fuera_horario"}
    fecha = now_cl.strftime("%Y-%m-%d")
    CAP = int(_os.getenv("CONSENT_AGENDADOS_CAP", "30"))
    try:
        from medilink import _get_shared_client, _q, _safe_json, HEADERS
        from config import MEDILINK_BASE_URL
        from session import normalize_wa_id, log_message
        from session import db as _conn
        from winback import (bi_conn, marketing_consent_status, phone_in_opt_out,
                             registrar_consent_enviado)
        from messaging import send_whatsapp_template, render_template_body

        client = _get_shared_client()
        r = await client.get(
            f"{MEDILINK_BASE_URL}/citas",
            params={"q": _q({"fecha": {"eq": fecha}, "estado_anulacion": {"eq": 0}})},
            headers=HEADERS, timeout=15)
        citas = _safe_json(r).get("data", []) if r.status_code == 200 else []

        with _conn() as cdb:
            rows = cdb.execute(
                "SELECT id_cita, phone FROM citas_bot WHERE created_at >= datetime('now','-2 days')"
            ).fetchall()
        bot_cita_ids = {str(x[0]) for x in rows if x[0]}
        bot_phones = {normalize_wa_id(x[1]) for x in rows if x[1]}

        candidatos = []
        pac_cache = {}
        seen_phones = set()
        for cita in citas:
            if len(candidatos) >= CAP:
                break
            id_cita = str(cita.get("id_cita") or cita.get("id") or "")
            if id_cita and id_cita in bot_cita_ids:
                continue  # agendada por el bot → el bot le pregunta
            id_pac = cita.get("id_paciente")
            if not id_pac:
                continue
            if id_pac in pac_cache:
                pac = pac_cache[id_pac]
            else:
                try:
                    rp = await client.get(f"{MEDILINK_BASE_URL}/pacientes/{id_pac}", headers=HEADERS, timeout=12)
                    pac = (_safe_json(rp).get("data") or {}) if rp.status_code == 200 else {}
                    if isinstance(pac, list):
                        pac = pac[0] if pac else {}
                except Exception:
                    pac = {}
                pac_cache[id_pac] = pac
                # Throttle: evitar ráfaga de GETs Medilink sin pausa.
                # Este job corre en :25 (1 min después de detectar_cancelaciones en :24).
                # Sin sleep, 30 citas = 30 GETs consecutivos → 429 en cascada.
                import asyncio as _ai_cag
                await _ai_cag.sleep(0.35)
            tel = (pac.get("celular") or pac.get("telefono") or "").strip()
            teln = normalize_wa_id(tel) if tel else ""
            if not teln or len(teln) < 11:
                continue
            if teln in bot_phones:
                continue  # número ya del bot (caso papá agenda al niño)
            if teln in seen_phones:
                continue  # mismo número en 2 citas (papá con 2 hijos) → 1 solo mensaje
            if marketing_consent_status(teln) is not None:
                continue  # ya está en el sistema de consent
            if phone_in_opt_out(teln):
                continue
            nombre = (pac.get("nombre") or cita.get("paciente_nombre") or "Paciente").split(" ")[0]
            seen_phones.add(teln)
            candidatos.append((teln, nombre))

        if dry_run:
            return {"candidatos": len(candidatos),
                    "muestra": [(t[:5] + "***" + t[-2:], n) for t, n in candidatos[:25]],
                    "citas_dia": len(citas), "bot_excluidos": len(bot_cita_ids)}

        import asyncio as _ai
        enviados = 0
        for teln, nombre in candidatos:
            try:
                await send_whatsapp_template(teln, "consent_marketing_v2", body_params=[nombre])
                log_message(teln, "out", render_template_body("consent_marketing_v2", [nombre]), "IDLE")
                registrar_consent_enviado(teln)
                enviados += 1
                log.info("consent_agendados: enviado → ...%s (%d/%d)", teln[-4:], enviados, len(candidatos))
            except Exception as _e:
                log.error("consent_agendados: error %s: %s", teln[-4:], _e)
            await _ai.sleep(30)
        log.info("_job_consent_agendados: enviados=%d de %d candidatos", enviados, len(candidatos))
        return {"enviados": enviados, "candidatos": len(candidatos)}
    except Exception as e:
        log.error("_job_consent_agendados falló: %s", e)
        return {"status": "error", "error": str(e)}


# ── Winback BI ────────────────────────────────────────────────────────────────

async def _job_custom_audiences_sync() -> None:
    """Job diario 04:00 CLT: sincroniza Custom Audiences con Meta Marketing API."""
    try:
        from custom_audiences_sync import job_custom_audiences_diario
        await job_custom_audiences_diario()
    except Exception as e:
        log.error("_job_custom_audiences_sync fallo: %s", e)


async def _job_winback_bi() -> None:
    """Job diario L-V 10:05 CLT: campanas winback desde BI Postgres.

    Guarda con WINBACK_ACTIVE=false en .env hasta confirmar aprobación
    de templates en Meta Business Manager.
    """
    try:
        from winback import job_winback_diario, WINBACK_ACTIVE
        if not WINBACK_ACTIVE:
            log.debug("_job_winback_bi: WINBACK_ACTIVE=false — skip")
            return
        stats = await job_winback_diario()
        log.info("_job_winback_bi: %s", stats)
    except Exception as e:
        log.error("_job_winback_bi fallo: %s", e)


# ── Win-back DENTAL ───────────────────────────────────────────────────────────

async def _job_dental_consent_blast() -> None:
    """Blast diario L-V 11:00 CLT: envía consent_dental_v1 (UTILITY) a candidatos
    dentales sin registro en bi.dental_consent.

    Se programa 1 hora después del blast de consent general para no competir
    por rate limit Meta. Máx 100/día, 30s entre envíos.

    DENTAL_CONSENT_BLAST_ACTIVE=false hasta que Rodrigo confirme que
    consent_dental_v1 está APPROVED en Meta Business Manager.
    """
    try:
        from dental_winback import run_dental_consent_blast, DENTAL_CONSENT_BLAST_ACTIVE
        if not DENTAL_CONSENT_BLAST_ACTIVE:
            log.debug("_job_dental_consent_blast: DENTAL_CONSENT_BLAST_ACTIVE=false — skip")
            return
        stats = await run_dental_consent_blast()
        log.info("_job_dental_consent_blast: %s", stats)
    except Exception as e:
        log.error("_job_dental_consent_blast fallo: %s", e)


async def _job_dental_winback() -> None:
    """Batch diario L-V 10:35 CLT: winback dental desde BI Postgres.

    Se programa 30 min después del winback general (10:05) para no competir
    por rate limit Meta. Sub-cohortes en orden de prioridad:
    ortodoncia → endo/implanto → odonto general 180d → 365d → estética.

    DENTAL_WINBACK_ACTIVE=false hasta que Rodrigo confirme y templates
    estén APPROVED en Meta Business Manager.
    """
    try:
        from dental_winback import job_dental_winback_diario, DENTAL_WINBACK_ACTIVE
        if not DENTAL_WINBACK_ACTIVE:
            log.debug("_job_dental_winback: DENTAL_WINBACK_ACTIVE=false — skip")
            return
        await job_dental_winback_diario()
    except Exception as e:
        log.error("_job_dental_winback fallo: %s", e)


# ── Reporte semanal de salud del bot ─────────────────────────────────────────

async def _job_dental_promo_report() -> None:
    """Diario 09:00 CLT (junio): reporte de conversión de la Promo Dental al dueño.
    SOLO envía si la ventana de 24h del dueño está abierta (no rebota). Tras junio, no-op."""
    try:
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo
        _now = _dt.now(ZoneInfo("America/Santiago"))
        if (_now.year, _now.month) != (2026, 6):
            return  # promo solo junio 2026
        if not ADMIN_ALERT_PHONE:
            return
        from session import is_window_open
        if not is_window_open(ADMIN_ALERT_PHONE):
            log.info("_job_dental_promo_report: ventana 24h cerrada — skip (no rebota)")
            return
        from messaging import send_whatsapp
        from session import log_message
        from dental_promo_report import texto_whatsapp
        txt = texto_whatsapp()
        await send_whatsapp(ADMIN_ALERT_PHONE, txt)
        log_message(ADMIN_ALERT_PHONE, "out", txt, "IDLE")
        log.info("_job_dental_promo_report: enviado al dueño")
    except Exception as e:
        log.warning("_job_dental_promo_report fallo: %s", e)


async def _job_health_report() -> None:
    """Lunes 09:00 CLT: envía al admin el reporte semanal de salud del bot.

    Estrategia de entrega (en orden):
    1. Template `reporte_semanal_salud_bot` si está aprobado en Meta.
    2. send_whatsapp directo si la ventana 24h del admin está abierta.
       (NO se usa send_whatsapp_proactive porque el bloqueo de blocklist
       aplica exactamente a ADMIN_ALERT_PHONE; la ventana del admin se
       abre cuando él escribe primero al bot.)
    3. Fallback: guardar en data/reportes_salud/{fecha}.md y loggear stderr.
    """
    import os
    import sys
    from pathlib import Path
    from datetime import datetime
    from zoneinfo import ZoneInfo

    if not ADMIN_ALERT_PHONE:
        log.warning("_job_health_report: ADMIN_ALERT_PHONE no configurado — skip")
        return

    try:
        from health_report import build_weekly_health_report
        reporte = build_weekly_health_report()
    except Exception as e:
        log.error("_job_health_report: error generando reporte: %s", e)
        return

    admin_phone = ADMIN_ALERT_PHONE.lstrip("+")
    now_stgo = datetime.now(ZoneInfo("America/Santiago"))
    fecha_str = now_stgo.strftime("%Y-%m-%d")

    # ── Intento 1: template aprobado ──────────────────────────────────────
    if USE_TEMPLATES:
        _tmpl_name = "reporte_semanal_salud_bot"
        try:
            from session import get_approved_templates as _get_tmpl
            aprobados = _get_tmpl() or []
        except (ImportError, AttributeError, Exception):
            aprobados = []
        if _tmpl_name in aprobados:
            try:
                wamid = await send_whatsapp_template(
                    admin_phone,
                    _tmpl_name,
                    body_params=[reporte[:1024]],
                )
                if wamid:
                    log.info("_job_health_report: enviado via template → wamid=%s", wamid)
                    return
            except Exception as e:
                log.warning("_job_health_report: template falló: %s", e)

    # ── Intento 2: send_whatsapp directo si ventana 24h abierta ──────────
    from session import is_window_open as _is_win
    if _is_win(admin_phone):
        try:
            wamid = await send_whatsapp(admin_phone, reporte)
            if wamid:
                log.info("_job_health_report: enviado via send_whatsapp (ventana abierta)")
                return
        except Exception as e:
            log.warning("_job_health_report: send_whatsapp falló: %s", e)

    # ── Fallback: archivo + journalctl ────────────────────────────────────
    try:
        reports_dir = Path(__file__).parent.parent / "data" / "reportes_salud"
        reports_dir.mkdir(parents=True, exist_ok=True)
        dest = reports_dir / f"{fecha_str}.md"
        dest.write_text(reporte, encoding="utf-8")
        log.info("_job_health_report: ventana cerrada y sin template aprobado — guardado en %s", dest)
        print(
            f"[health_report] {fecha_str} — reporte guardado en {dest} "
            "(ventana admin cerrada, template no aprobado)",
            file=sys.stderr,
        )
    except Exception as e:
        log.error("_job_health_report: fallback archivo también falló: %s", e)


async def _job_caja_report() -> None:
    """Cada mañana (08:45 CLT): cuadre de ayer + efectivo en caja + qué falta registrar,
    al WhatsApp del dueño. Entrega: template `reporte_caja_diaria` → send_whatsapp si la
    ventana 24h del admin está abierta → fallback archivo. Lo confiable es el comando
    *caja* del asistente Adkun (el dueño escribe y lo recibe al instante)."""
    import sys
    from pathlib import Path
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo as _ZI

    if not ADMIN_ALERT_PHONE:
        log.warning("_job_caja_report: ADMIN_ALERT_PHONE no configurado — skip")
        return
    try:
        from cuadre_caja import texto_cuadre
        ayer = (datetime.now(_ZI("America/Santiago")) - timedelta(days=1)).strftime("%Y-%m-%d")
        reporte = texto_cuadre(ayer)
    except Exception as e:
        log.error("_job_caja_report: error generando reporte: %s", e)
        return

    admin_phone = ADMIN_ALERT_PHONE.lstrip("+")

    if USE_TEMPLATES:
        try:
            from session import get_approved_templates as _get_tmpl
            aprobados = _get_tmpl() or []
        except Exception:
            aprobados = []
        if "reporte_caja_diaria" in aprobados:
            try:
                wamid = await send_whatsapp_template(admin_phone, "reporte_caja_diaria",
                                                     body_params=[reporte[:1024]])
                if wamid:
                    log.info("_job_caja_report: enviado via template → %s", wamid)
                    return
            except Exception as e:
                log.warning("_job_caja_report: template falló: %s", e)

    from session import is_window_open as _is_win
    if _is_win(admin_phone):
        try:
            if await send_whatsapp(admin_phone, reporte):
                log.info("_job_caja_report: enviado via send_whatsapp (ventana abierta)")
                return
        except Exception as e:
            log.warning("_job_caja_report: send_whatsapp falló: %s", e)

    try:
        d = Path(__file__).parent.parent / "data" / "reportes_caja"
        d.mkdir(parents=True, exist_ok=True)
        dest = d / f"{datetime.now(_ZI('America/Santiago')).strftime('%Y-%m-%d')}.md"
        dest.write_text(reporte, encoding="utf-8")
        log.info("_job_caja_report: sin canal disponible — guardado en %s", dest)
        print(f"[caja_report] guardado en {dest} (ventana cerrada / sin template)", file=sys.stderr)
    except Exception as e:
        log.error("_job_caja_report: fallback archivo falló: %s", e)


# ── Watchdog auto-pausa/auto-reactivación del blast ──────────────────────────

async def _job_watchdog_blast() -> None:
    """Cada 4h (minuto 15): evalúa salud del blast y pausa/reactiva automáticamente.

    Criterios de AUTOPAUSAR (cualquiera de estos):
      - errores Meta (131042 + 132000 + MSG FAILED) > 10 en últimas 24h
      - quality_rating del número WA != GREEN
      - tasa de rechazo consent > 40% con muestra >= 30

    Criterios de AUTORREACTIVAR (todos simultáneamente):
      - errores <= 1 en últimas 24h
      - quality_rating == GREEN
      - tasa rechazo <= 40%
      - flag actualmente en false Y fue seteado por el watchdog (comentario # auto-set)

    Cuando cambia el estado edita /opt/chatbot-cmc/.env y alerta a ADMIN_ALERT_PHONE.
    Si el flag fue seteado manualmente (sin comentario # auto-set) NO lo toca.
    """
    import os as _os_wb
    import re as _re_wb
    from datetime import datetime
    from pathlib import Path
    from zoneinfo import ZoneInfo
    from config import OLACORE_TOKEN as _olacore_tok

    log.info("_job_watchdog_blast: iniciando evaluación")

    if not ADMIN_ALERT_PHONE:
        log.warning("_job_watchdog_blast: ADMIN_ALERT_PHONE no configurado — skip")
        return

    _ENV_PATH = Path("/opt/chatbot-cmc/.env")
    _ALERT_LOG = Path("/var/log/cmc-watchdog-alerts.log")
    _NOW_CL = datetime.now(ZoneInfo("America/Santiago"))
    _ts = _NOW_CL.strftime("%Y-%m-%d %H:%M CLT")

    # ── 1. Leer flag actual y detectar si fue seteado manualmente ────────
    flag_actual: bool = _os_wb.getenv("MARKETING_CONSENT_BLAST_ACTIVE", "false").lower() in ("true", "1", "yes")
    fue_auto_set: bool = False

    if _ENV_PATH.exists():
        env_text = _ENV_PATH.read_text(encoding="utf-8")
        # Buscar si la línea del flag tiene comentario # auto-set encima o inline
        for line in env_text.splitlines():
            if "MARKETING_CONSENT_BLAST_ACTIVE" in line and "# auto-set" in line:
                fue_auto_set = True
                break
        # También buscar comentario en línea previa
        lines = env_text.splitlines()
        for i, line in enumerate(lines):
            if "MARKETING_CONSENT_BLAST_ACTIVE" in line and i > 0:
                if "# auto-set" in lines[i - 1]:
                    fue_auto_set = True
                    break
    else:
        log.warning("_job_watchdog_blast: .env no encontrado en %s — modo solo-lectura", _ENV_PATH)

    # ── 2. Contar errores Meta últimas 24h en el log ──────────────────────
    errores_total = 0
    try:
        # grep sobre las últimas 24h. El log tiene timestamp ISO al inicio de cada línea.
        # Usamos las últimas 5000 líneas como proxy (más rápido que filtrar por fecha en bash).
        log_tail = _tail_lines()
        err_131042 = log_tail.count("131042")
        err_132000 = log_tail.count("132000")
        err_4xx     = len(_re_wb.findall(r"MSG FAILED.*code=", log_tail))
        errores_total = err_131042 + err_132000 + err_4xx
        log.info("_job_watchdog_blast: errores 24h — 131042=%d 132000=%d 4xx=%d total=%d",
                 err_131042, err_132000, err_4xx, errores_total)
    except Exception as e:
        log.warning("_job_watchdog_blast: no pudo leer log: %s", e)
        errores_total = 0  # conservador: no pausar por fallo de lectura

    # ── 3. Quality rating del número WhatsApp ────────────────────────────
    quality_rating = "GREEN"  # default seguro
    try:
        from config import META_PHONE_NUMBER_ID, META_ACCESS_TOKEN
        if META_PHONE_NUMBER_ID and META_ACCESS_TOKEN:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"https://graph.facebook.com/v19.0/{META_PHONE_NUMBER_ID}",
                    params={"fields": "quality_rating,name_status"},
                    headers={"Authorization": f"Bearer {META_ACCESS_TOKEN}"},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    quality_rating = (data.get("quality_rating") or "GREEN").upper()
                    log.info("_job_watchdog_blast: quality_rating=%s name_status=%s",
                             quality_rating, data.get("name_status"))
                else:
                    log.warning("_job_watchdog_blast: Meta API quality status=%d", resp.status_code)
        else:
            log.warning("_job_watchdog_blast: META_PHONE_NUMBER_ID o META_ACCESS_TOKEN no configurados")
    except Exception as e:
        log.warning("_job_watchdog_blast: error consultando quality rating: %s", e)

    # ── 4. Tasa de rechazo consent últimos 7 días ────────────────────────
    tasa_rechazo: float = 0.0
    muestra_rechazo: int = 0
    try:
        from winback import bi_conn as _bi_conn_wb
        with _bi_conn_wb() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT
                        COUNT(*) FILTER (WHERE status = 'declined')  AS declinados,
                        COUNT(*) FILTER (WHERE status IN ('accepted','declined')) AS respondidos
                    FROM bi.marketing_consent
                    WHERE consent_sent_at >= now() - interval '7 days'
                """)
                row = cur.fetchone()
                declinados, respondidos = (row or (0, 0))
                muestra_rechazo = respondidos or 0
                tasa_rechazo = round((declinados / respondidos * 100) if respondidos > 0 else 0.0, 1)
        log.info("_job_watchdog_blast: tasa_rechazo=%.1f%% muestra=%d", tasa_rechazo, muestra_rechazo)
    except Exception as e:
        log.warning("_job_watchdog_blast: error consultando tasa rechazo: %s", e)

    # ── 5. Decisión ──────────────────────────────────────────────────────
    debe_pausar = (
        errores_total > 10
        or quality_rating not in ("GREEN",)
        or (tasa_rechazo > 40.0 and muestra_rechazo >= 30)
    )
    # Reactivar es SIMÉTRICO a pausar: apenas no haya razón para pausar, vuelve.
    # (Bug histórico 2026-05-28→06-08: reactivar exigía errores<=1 mientras pausar
    #  exigía errores>10 → con errores en 2-10 quedaba TRABADO en OFF 11 días sin
    #  loguear nada. La zona muerta entre umbrales mató el blast UTILITY general.)
    debe_reactivar = (
        not flag_actual
        and fue_auto_set
        and not debe_pausar
    )

    razones: list[str] = []
    if errores_total > 10:
        razones.append(f"errores Meta 24h={errores_total}")
    if quality_rating not in ("GREEN",):
        razones.append(f"quality_rating={quality_rating}")
    if tasa_rechazo > 40.0 and muestra_rechazo >= 30:
        razones.append(f"tasa_rechazo={tasa_rechazo}% (n={muestra_rechazo})")

    log.info(
        "_job_watchdog_blast: flag_actual=%s fue_auto_set=%s debe_pausar=%s debe_reactivar=%s razones=%s",
        flag_actual, fue_auto_set, debe_pausar, debe_reactivar, razones,
    )

    # ── 6. Aplicar cambio si corresponde ─────────────────────────────────
    cambio_realizado: str | None = None

    def _editar_env(nuevo_valor: str) -> bool:
        """Edita MARKETING_CONSENT_BLAST_ACTIVE en .env. Retorna True si exitoso."""
        try:
            if not _ENV_PATH.exists():
                return False
            texto = _ENV_PATH.read_text(encoding="utf-8")
            lineas = texto.splitlines(keepends=True)
            nuevas: list[str] = []
            encontrado = False
            for linea in lineas:
                # Saltar comentarios auto-set previos
                if "# auto-set by watchdog" in linea:
                    continue
                if _re_wb.match(r"\s*MARKETING_CONSENT_BLAST_ACTIVE\s*=", linea):
                    nuevas.append(f"# auto-set by watchdog @ {_ts}\n")
                    nuevas.append(f"MARKETING_CONSENT_BLAST_ACTIVE={nuevo_valor}  # auto-set\n")
                    encontrado = True
                else:
                    nuevas.append(linea)
            if not encontrado:
                nuevas.append(f"\n# auto-set by watchdog @ {_ts}\n")
                nuevas.append(f"MARKETING_CONSENT_BLAST_ACTIVE={nuevo_valor}  # auto-set\n")
            _ENV_PATH.write_text("".join(nuevas), encoding="utf-8")
            return True
        except Exception as ex:
            log.error("_job_watchdog_blast: error editando .env: %s", ex)
            return False

    if flag_actual and debe_pausar:
        if _editar_env("false"):
            # Actualizar os.environ en memoria para que el proceso vivo tome efecto inmediato
            # sin esperar restart (escribir solo .env no modifica el entorno del proceso).
            _os_wb.environ["MARKETING_CONSENT_BLAST_ACTIVE"] = "false"
            cambio_realizado = "PAUSADO"
            log.warning("_job_watchdog_blast: BLAST AUTOPAUSADO — razones: %s", razones)
    elif debe_reactivar:
        if _editar_env("true"):
            _os_wb.environ["MARKETING_CONSENT_BLAST_ACTIVE"] = "true"
            cambio_realizado = "REACTIVADO"
            log.info("_job_watchdog_blast: BLAST AUTOREACTIVADO — todos los indicadores OK")

    # ── 7. Alerta a Rodrigo si hubo cambio ───────────────────────────────
    if cambio_realizado:
        estado_txt = "PAUSADO (blast detenido)" if cambio_realizado == "PAUSADO" else "ACTIVO (blast reanudado)"
        razones_txt = ", ".join(razones) if razones else "todos los indicadores OK"
        msg = (
            f"Sistema CMC — Blast {cambio_realizado}\n\n"
            f"Estado actual: {estado_txt}\n"
            f"Razon: {razones_txt}\n"
            f"Errores 24h: {errores_total} | Quality: {quality_rating} | "
            f"Rechazo 7d: {tasa_rechazo}% (n={muestra_rechazo})\n\n"
            f"Dashboard: agentecmc.cl/winback?token={_olacore_tok}\n"
            f"{_ts}"
        )

        # Intentar WA (ventana 24h)
        from session import is_window_open as _is_win_wb
        admin_phone = ADMIN_ALERT_PHONE.lstrip("+")
        enviado_wa = False
        if _is_win_wb(admin_phone):
            try:
                wamid = await send_whatsapp(admin_phone, msg)
                if wamid:
                    log.info("_job_watchdog_blast: alerta enviada por WA → wamid=%s", wamid)
                    enviado_wa = True
            except Exception as e:
                log.warning("_job_watchdog_blast: send_whatsapp falló: %s", e)

        # Fallback: archivo de log
        try:
            _ALERT_LOG.parent.mkdir(parents=True, exist_ok=True)
            with open(_ALERT_LOG, "a", encoding="utf-8") as f:
                f.write(f"[{_ts}] BLAST {cambio_realizado}: {razones_txt} | "
                        f"errores={errores_total} quality={quality_rating} "
                        f"rechazo={tasa_rechazo}% n={muestra_rechazo} "
                        f"wa_enviado={enviado_wa}\n")
        except Exception as e:
            log.warning("_job_watchdog_blast: no pudo escribir alert log: %s", e)


# ─────────────────────────────────────────────────────────────────────────────
# Watchdog de entrega real de templates WhatsApp
# ─────────────────────────────────────────────────────────────────────────────

def _render_watchdog_email(subject: str, body_text: str, is_alert: bool) -> str:
    """HTML mínimo para alerta operacional del watchdog de entrega WA.
    No usa CDN — estilos inline completos."""
    _NAVY = "#0F3F68"
    _AQUA = "#4FBECE"
    _BG = "#f4f7fa"
    _INK = "#13202e"
    _MUTED = "#64798c"
    _header_bg = "#dc2626" if is_alert else "#16a34a"
    _header_label = "ALERTA OPERACIONAL" if is_alert else "SISTEMA NORMALIZADO"
    import html as _html_mod
    body_html = "".join(
        f'<p style="margin:0 0 14px;font-size:14px;line-height:1.65;color:{_INK}">'
        f'{_html_mod.escape(para.strip())}</p>'
        for para in body_text.split("\n\n") if para.strip()
    )
    return f"""<!DOCTYPE html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_html_mod.escape(subject)}</title></head>
<body style="margin:0;padding:0;background:{_BG}">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"
       style="background:{_BG}">
<tr><td align="center" style="padding:24px 12px">
  <table role="presentation" width="560" cellpadding="0" cellspacing="0"
         style="width:560px;max-width:100%;background:#fff;border-radius:14px;
                overflow:hidden;font-family:Arial,Helvetica,sans-serif;
                box-shadow:0 4px 18px rgba(15,63,104,.08)">
    <tr><td style="background:{_NAVY};padding:18px 24px 14px">
      <div style="color:{_AQUA};font-size:11px;font-weight:bold;letter-spacing:.08em;
                  text-transform:uppercase;margin-bottom:3px">Centro Médico Carampangue</div>
      <div style="display:inline-block;background:{_header_bg};color:#fff;
                  font-size:11px;font-weight:bold;letter-spacing:.06em;
                  text-transform:uppercase;padding:3px 9px;border-radius:5px;
                  margin-bottom:8px">{_header_label}</div>
      <div style="color:#fff;font-size:17px;font-weight:bold;line-height:1.3">
        {_html_mod.escape(subject)}</div>
    </td></tr>
    <tr><td style="padding:24px 24px 10px">{body_html}</td></tr>
    <tr><td style="padding:16px 24px;background:#f0f5f9;border-top:1px solid #e6edf3">
      <p style="margin:0;font-size:11px;line-height:1.5;color:{_MUTED}">
        Este es un correo operacional automático del sistema CMC — no responder.<br>
        Centro Médico Carampangue · agentecmc.cl · WhatsApp +56 9 6661 0737
      </p>
    </td></tr>
  </table>
</td></tr></table>
</body></html>"""


async def _job_watchdog_entrega() -> None:
    """Cada 30 min: vigila la tasa de entrega real de templates via message_statuses.

    Detecta apagones de facturación Meta (error 131042 = Business eligibility
    payment issue): Meta acepta el POST (HTTP 200) pero luego marca la entrega
    como 'failed' con code=131042 vía webhook asíncrono. Sin este watchdog el
    apagón pasa desapercibido indefinidamente (el bot solo mide el 200 inicial).

    Umbrales (ventana 6h, mínimo 8 mensajes):
      - Alerta si delivered_pct < 50% O errores 131042 >= 5
    Histéresis simétrica (sin zona muerta):
      - Alerta UNA vez al entrar en estado malo (transición bueno→malo)
      - Alerta UNA vez al recuperarse (transición malo→bueno)
      - Estado persistido en /var/log/cmc-entrega-watchdog-state.json

    Canales (en orden, sin depender de templates WA — justo cuando hay que
    alertar, los templates pueden estar caídos):
      1. Email vía Resend/SMTP directo (bypass del gate EMAIL_SENDING_ENABLED)
         Destinatario: variable ALERT_EMAIL en .env
      2. Banner: endpoint GET /api/watchdog/entrega-status lee el state file
      3. Log CRITICAL / INFO con texto claro
      4. WA free-form al owner SOLO si ventana 24h abierta (best-effort)
    """
    import os as _os_wd
    import json as _json_wd
    from datetime import datetime, timedelta
    from pathlib import Path
    from zoneinfo import ZoneInfo

    _STATE_FILE = Path("/var/log/cmc-entrega-watchdog-state.json")
    _VENTANA_H = 6
    _MIN_MUESTRA = 8
    _UMBRAL_DELIVERED_PCT = 50.0
    _UMBRAL_131042 = 5

    _NOW = datetime.now(ZoneInfo("America/Santiago"))
    _ts_label = _NOW.strftime("%Y-%m-%d %H:%M CLT")

    # ── 1. Leer estado previo (histéresis) ───────────────────────────────────
    prev_state: dict = {"is_bad": False, "last_alert_ts": 0.0, "last_recovery_ts": 0.0}
    try:
        if _STATE_FILE.exists():
            prev_state = _json_wd.loads(_STATE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("_job_watchdog_entrega: no pudo leer state file: %s", e)

    fue_bad = bool(prev_state.get("is_bad", False))

    # ── 2. Consultar message_statuses últimas N horas ─────────────────────────
    # ts se almacena como TEXT en formato SQLite datetime (UTC). Usamos
    # datetime('now', '-Xhours') para la comparación (mismo patrón que
    # get_message_status_summary en session.py).
    total = delivered = failed = err_131042 = 0
    try:
        from session import db as _db_wd
        with _db_wd() as conn:
            rows = conn.execute(
                """
                SELECT status, error_code, COUNT(*) AS cnt
                FROM message_statuses
                WHERE ts >= datetime('now', ?)
                GROUP BY status, error_code
                """,
                (f"-{_VENTANA_H} hours",),
            ).fetchall()
        for row in rows:
            cnt = int(row["cnt"])
            status = (row["status"] or "").lower()
            err_code = str(row["error_code"] or "")
            total += cnt
            if status in ("delivered", "read"):
                delivered += cnt
            elif status == "failed":
                failed += cnt
                if err_code == "131042":
                    err_131042 += cnt
    except Exception as e:
        log.error("_job_watchdog_entrega: error consultando message_statuses: %s", e)
        return

    delivered_pct = round(delivered / total * 100, 1) if total > 0 else 100.0

    log.info(
        "_job_watchdog_entrega: ventana=%dh total=%d delivered=%d(%.1f%%) "
        "failed=%d err_131042=%d",
        _VENTANA_H, total, delivered, delivered_pct, failed, err_131042,
    )

    # ── 3. Evaluar condición de apagón ───────────────────────────────────────
    muestra_suficiente = total >= _MIN_MUESTRA
    is_bad = muestra_suficiente and (
        delivered_pct < _UMBRAL_DELIVERED_PCT
        or err_131042 >= _UMBRAL_131042
    )

    # ── 4. Persistir estado (endpoint /api/watchdog/entrega-status lo lee) ───
    new_state: dict = {
        "is_bad": is_bad,
        "total": total,
        "delivered": delivered,
        "delivered_pct": delivered_pct,
        "failed": failed,
        "err_131042": err_131042,
        "ventana_h": _VENTANA_H,
        "ts": _NOW.isoformat(),
        "last_alert_ts": prev_state.get("last_alert_ts", 0.0),
        "last_recovery_ts": prev_state.get("last_recovery_ts", 0.0),
    }

    # ── 5. Histéresis simétrica: actuar solo en transición de estado ─────────
    # (Replica el patrón corregido de _job_watchdog_blast: umbral de entrada ==
    # umbral de salida → sin zona muerta. Bug histórico 2026-05-28→06-08 documentado.)
    transicion_a_malo = is_bad and not fue_bad
    transicion_a_bueno = (not is_bad) and fue_bad

    if not transicion_a_malo and not transicion_a_bueno:
        # Sin cambio de estado — solo persistir métricas actualizadas
        try:
            _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            _STATE_FILE.write_text(_json_wd.dumps(new_state, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
        except Exception as e:
            log.warning("_job_watchdog_entrega: no pudo escribir state file: %s", e)
        return

    # ── 6. Construir mensaje según transición ────────────────────────────────
    if transicion_a_malo:
        razones: list[str] = []
        if err_131042 >= _UMBRAL_131042:
            razones.append(
                f"{err_131042} fallas con error 131042 "
                f"(problema de pago/elegibilidad de la cuenta Meta)"
            )
        if delivered_pct < _UMBRAL_DELIVERED_PCT:
            razones.append(
                f"tasa de entrega {delivered_pct}% (umbral: {_UMBRAL_DELIVERED_PCT}%)"
            )
        razones_txt = " | ".join(razones) if razones else "parámetros de umbral superados"
        asunto = (
            f"ALERTA CMC: WhatsApp NO entrega templates "
            f"({err_131042} errores 131042)"
        )
        cuerpo = (
            f"WhatsApp NO está entregando templates: {razones_txt}.\n\n"
            f"Estadísticas (últimas {_VENTANA_H}h): "
            f"Total={total} | Delivered={delivered} ({delivered_pct}%) | "
            f"Failed={failed} | Errores 131042={err_131042}\n\n"
            f"Acción requerida: revisa el método de pago en "
            f"business.facebook.com/billing → WhatsApp Business → método de pago.\n"
            f"Tarjeta en uso: MASTERCARD *9176 (vence 8/2028)\n\n"
            f"Timestamp: {_ts_label}"
        )
        log.critical(
            "_job_watchdog_entrega: APAGON DETECTADO — %s "
            "(total=%d delivered=%.1f%% failed=%d err_131042=%d)",
            razones_txt, total, delivered_pct, failed, err_131042,
        )
        new_state["last_alert_ts"] = _NOW.timestamp()
    else:  # transicion_a_bueno
        asunto = "CMC: Entrega de templates WhatsApp normalizada"
        cuerpo = (
            f"La entrega de templates WhatsApp se normalizó.\n\n"
            f"Estadísticas (últimas {_VENTANA_H}h): "
            f"Total={total} | Delivered={delivered} ({delivered_pct}%) | "
            f"Failed={failed} | Errores 131042={err_131042}\n\n"
            f"Timestamp: {_ts_label}"
        )
        log.info(
            "_job_watchdog_entrega: RECUPERACION — entrega normalizada "
            "(total=%d delivered=%.1f%% err_131042=%d)",
            total, delivered_pct, err_131042,
        )
        new_state["last_recovery_ts"] = _NOW.timestamp()

    # ── 7. Persistir estado actualizado ──────────────────────────────────────
    try:
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _STATE_FILE.write_text(_json_wd.dumps(new_state, ensure_ascii=False, indent=2),
                               encoding="utf-8")
    except Exception as e:
        log.warning("_job_watchdog_entrega: no pudo escribir state file: %s", e)

    # ── 8. Canal 1: Email (primario — independiente de templates WA) ─────────
    # Bypass del gate EMAIL_SENDING_ENABLED: ese gate es para marketing;
    # las alertas operacionales deben salir siempre que haya proveedor configurado.
    _alert_email = _os_wd.getenv("ALERT_EMAIL", "").strip()
    _email_sent = False
    if _alert_email:
        try:
            from autopilot.email_render import (
                EMAIL_PROVIDER as _ep_wd,
                EMAIL_FROM_ADDRESS as _efrom_wd,
            )
            _html_body = _render_watchdog_email(asunto, cuerpo, transicion_a_malo)

            if _ep_wd == "resend" and _os_wd.getenv("RESEND_API_KEY"):
                async with httpx.AsyncClient(timeout=30) as _hx_em:
                    _r = await _hx_em.post(
                        "https://api.resend.com/emails",
                        json={
                            "from": f"Centro Médico Carampangue <{_efrom_wd}>",
                            "to": [_alert_email],
                            "subject": asunto,
                            "html": _html_body,
                        },
                        headers={
                            "Authorization": f"Bearer {_os_wd.getenv('RESEND_API_KEY')}",
                            "Content-Type": "application/json",
                        },
                    )
                if _r.status_code in (200, 201):
                    _email_sent = True
                    log.info("_job_watchdog_entrega: email enviado via Resend → %s",
                             _alert_email)
                else:
                    log.warning("_job_watchdog_entrega: Resend %d: %s",
                                _r.status_code, _r.text[:150])

            elif _ep_wd == "smtp" and _os_wd.getenv("SMTP_HOST"):
                import smtplib as _smtplib_wd
                from email.mime.multipart import MIMEMultipart as _MIMEMulti_wd
                from email.mime.text import MIMEText as _MIMEText_wd
                _msg = _MIMEMulti_wd("alternative")
                _msg["Subject"] = asunto
                _msg["From"] = f"Centro Médico Carampangue <{_efrom_wd}>"
                _msg["To"] = _alert_email
                _msg.attach(_MIMEText_wd(_html_body, "html", "utf-8"))
                with _smtplib_wd.SMTP(
                    _os_wd.getenv("SMTP_HOST"),
                    int(_os_wd.getenv("SMTP_PORT", "587")),
                    timeout=30,
                ) as _s_wd:
                    _s_wd.starttls()
                    if _os_wd.getenv("SMTP_USER"):
                        _s_wd.login(
                            _os_wd.getenv("SMTP_USER"),
                            _os_wd.getenv("SMTP_PASSWORD", ""),
                        )
                    _s_wd.sendmail(_efrom_wd, [_alert_email], _msg.as_string())
                _email_sent = True
                log.info("_job_watchdog_entrega: email enviado via SMTP → %s",
                         _alert_email)

            else:
                log.warning(
                    "_job_watchdog_entrega: ALERT_EMAIL configurado pero sin "
                    "proveedor activo (EMAIL_PROVIDER=%s, RESEND_KEY=%s, SMTP_HOST=%s). "
                    "Configura RESEND_API_KEY o SMTP_HOST en .env",
                    _ep_wd,
                    bool(_os_wd.getenv("RESEND_API_KEY")),
                    bool(_os_wd.getenv("SMTP_HOST")),
                )
        except Exception as e:
            log.warning("_job_watchdog_entrega: email falló: %s", e)
    else:
        log.warning(
            "_job_watchdog_entrega: ALERT_EMAIL no configurado en .env — "
            "agrega ALERT_EMAIL=tu@correo.cl para recibir alertas por email"
        )

    # Canal 2: Banner persistente (estado ya en state file — endpoint lo expone)

    # ── 9. Canal 3: WA free-form al owner (best-effort, solo si ventana abierta) ──
    if ADMIN_ALERT_PHONE:
        try:
            from session import is_window_open as _is_win_wd
            _admin_ph = ADMIN_ALERT_PHONE.lstrip("+")
            if _is_win_wd(_admin_ph):
                _wamid = await send_whatsapp(_admin_ph, f"*{asunto}*\n\n{cuerpo}")
                if _wamid:
                    log.info("_job_watchdog_entrega: WA free-form enviado → wamid=%s",
                             _wamid)
        except Exception as e:
            log.warning("_job_watchdog_entrega: WA free-form falló: %s", e)

    log.info(
        "_job_watchdog_entrega: ciclo completo — email_sent=%s estado=%s",
        _email_sent,
        "APAGON" if transicion_a_malo else "RECUPERACION",
    )


# ── Reporte diario win-back a Rodrigo ─────────────────────────────────────────

async def _job_winback_daily_report() -> None:
    """L-V 19:00 CLT: envía a ADMIN_ALERT_PHONE el resumen del día del sprint win-back.

    Datos: queries a BI Postgres (mismo patrón que /admin/api/winback-status).
    Entrega: send_whatsapp si ventana 24h abierta, sino guarda en
    /var/log/cmc-watchdog-alerts.log para revisión manual.
    """
    import os as _os_dr
    from datetime import datetime
    from pathlib import Path
    from zoneinfo import ZoneInfo
    from config import OLACORE_TOKEN as _olacore_tok_dr

    if not ADMIN_ALERT_PHONE:
        log.warning("_job_winback_daily_report: ADMIN_ALERT_PHONE no configurado — skip")
        return

    _ALERT_LOG = Path("/var/log/cmc-watchdog-alerts.log")
    _NOW_CL = datetime.now(ZoneInfo("America/Santiago"))
    fecha_str = _NOW_CL.strftime("%d/%m/%Y")

    # ── 1. Queries BI ────────────────────────────────────────────────────
    utility_hoy = 0
    aceptaron_hoy = 0
    declinaron_hoy = 0
    winbacks_hoy = 0
    citas_hoy = 0
    costo_hoy = 0
    errores_24h = 0
    tasa_acepto_pct: float = 0.0

    try:
        from winback import bi_conn as _bi_conn_dr
        with _bi_conn_dr() as conn:
            with conn.cursor() as cur:
                # Consent enviados hoy
                cur.execute("""
                    SELECT
                        COUNT(*) FILTER (WHERE DATE(consent_sent_at AT TIME ZONE 'America/Santiago') = CURRENT_DATE) AS enviados_hoy,
                        COUNT(*) FILTER (WHERE DATE(response_at AT TIME ZONE 'America/Santiago') = CURRENT_DATE AND status = 'accepted') AS aceptaron_hoy,
                        COUNT(*) FILTER (WHERE DATE(response_at AT TIME ZONE 'America/Santiago') = CURRENT_DATE AND status = 'declined') AS declinaron_hoy
                    FROM bi.marketing_consent
                """)
                row = cur.fetchone()
                utility_hoy, aceptaron_hoy, declinaron_hoy = (row or (0, 0, 0))

                respondieron_hoy = aceptaron_hoy + declinaron_hoy
                tasa_acepto_pct = round(
                    (aceptaron_hoy / respondieron_hoy * 100) if respondieron_hoy > 0 else 0.0, 1
                )

                # Winbacks enviados hoy
                cur.execute("""
                    SELECT
                        COUNT(*) AS winbacks_hoy,
                        COUNT(*) FILTER (WHERE cita_id IS NOT NULL OR cita_atribuida_id IS NOT NULL) AS citas_hoy
                    FROM bi.winback_envios
                    WHERE DATE(enviado_at AT TIME ZONE 'America/Santiago') = CURRENT_DATE
                """)
                row2 = cur.fetchone()
                winbacks_hoy = row2[0] if row2 else 0
                citas_hoy = row2[1] if row2 else 0

                # Costo Meta hoy (tabla opcional)
                try:
                    cur.execute("""
                        SELECT COALESCE(SUM(spend_clp), 0)
                        FROM bi.meta_spend_winback
                        WHERE fecha = CURRENT_DATE
                    """)
                    costo_hoy = int(cur.fetchone()[0])
                except Exception:
                    costo_hoy = 0

    except Exception as e:
        log.warning("_job_winback_daily_report: error queries BI: %s", e)

    # ── 2. Errores Meta últimas 24h (misma lógica que watchdog) ─────────
    try:
        import re as _re_dr
        log_tail = _tail_lines()
        errores_24h = (
            log_tail.count("131042")
            + log_tail.count("132000")
            + len(_re_dr.findall(r"MSG FAILED.*code=", log_tail))
        )
    except Exception as e:
        log.warning("_job_winback_daily_report: error leyendo log: %s", e)

    # ── 3. Estado actual del flag ─────────────────────────────────────────
    flag_blast = _os_dr.getenv("MARKETING_CONSENT_BLAST_ACTIVE", "false").lower() in ("true", "1", "yes")
    estado_flag = "activo" if flag_blast else "pausado"

    # ── 4. Armar mensaje ──────────────────────────────────────────────────
    msg = (
        f"Resumen Win-back CMC — {fecha_str}\n\n"
        f"UTILITY enviados hoy: {utility_hoy}\n"
        f"Aceptaron: {aceptaron_hoy} ({tasa_acepto_pct}%)\n"
        f"Declinaron: {declinaron_hoy}\n"
        f"Winbacks delivery OK: {winbacks_hoy}\n"
        f"Citas creadas desde winback: {citas_hoy}\n"
        f"Costo Meta hoy: ${costo_hoy:,} CLP\n"
        f"Errores 24h: {errores_24h}\n\n"
        f"Estado blast: {estado_flag}\n"
        f"Dashboard: agentecmc.cl/winback?token={_olacore_tok_dr}"
    )

    # ── 5. Enviar ─────────────────────────────────────────────────────────
    from session import is_window_open as _is_win_dr
    admin_phone = ADMIN_ALERT_PHONE.lstrip("+")
    enviado_wa = False

    if _is_win_dr(admin_phone):
        try:
            wamid = await send_whatsapp(admin_phone, msg)
            if wamid:
                log.info("_job_winback_daily_report: enviado WA → wamid=%s", wamid)
                enviado_wa = True
        except Exception as e:
            log.warning("_job_winback_daily_report: send_whatsapp falló: %s", e)

    if not enviado_wa:
        # Fallback: log de alertas (Rodrigo puede hacer: tail /var/log/cmc-watchdog-alerts.log)
        try:
            _ALERT_LOG.parent.mkdir(parents=True, exist_ok=True)
            with open(_ALERT_LOG, "a", encoding="utf-8") as f:
                f.write(f"\n--- REPORTE DIARIO {fecha_str} ---\n{msg}\n")
            log.info("_job_winback_daily_report: ventana cerrada — guardado en %s", _ALERT_LOG)
        except Exception as e:
            log.error("_job_winback_daily_report: fallback log también falló: %s", e)


# ─────────────────────────────────────────────────────────────────────────────
# M4: Digest semanal de pacientes con >14 días en waitlist
# Gateado por WAITLIST_DIGEST_ENABLED (default true — notificación solo interna)
# ─────────────────────────────────────────────────────────────────────────────
def _get_waitlist_digest_enabled() -> bool:
    import os as _os_wd2
    return _os_wd2.getenv("WAITLIST_DIGEST_ENABLED", "true").lower() in ("true", "1", "yes")


async def _job_waitlist_digest_semanal():
    """Lunes 09:30 CLT: arma digest de pacientes con >14 días en waitlist y
    lo envía a recepción via ADMIN_ALERT_PHONE (igual que otras alertas).
    NO contacta a los pacientes — es solo notificación interna.
    Gateado por env WAITLIST_DIGEST_ENABLED (default true).
    """
    if not _get_waitlist_digest_enabled():
        log.info("waitlist_digest: desactivado (WAITLIST_DIGEST_ENABLED=false)")
        return

    if not ADMIN_ALERT_PHONE:
        log.warning("waitlist_digest: ADMIN_ALERT_PHONE no configurado, abortando")
        return

    from datetime import datetime as _dt_wd, timezone as _tz_wd
    from session import log_event as _le_wd
    from session import db as _conn_wd

    try:
        with _conn_wd() as _c_wd:
            rows_wd = _c_wd.execute("""
                SELECT id, phone, nombre, especialidad, created_at
                FROM waitlist
                WHERE notified_at IS NULL AND canceled_at IS NULL
                  AND created_at <= datetime('now', '-14 days')
                ORDER BY created_at ASC
            """).fetchall()
    except Exception as _e_wd:
        log.error("waitlist_digest: error leyendo DB: %s", _e_wd)
        return

    if not rows_wd:
        log.info("waitlist_digest: no hay pacientes con >14 dias en waitlist")
        return

    # FIX F043: sqlite3.Row no tiene .get() — acceder por clave directo (devuelve None si ausente)
    lineas_wd = []
    for row_wd in rows_wd:
        _nombre_raw = row_wd["nombre"]
        _nombre_wd = (_nombre_raw or "").split()[0] if _nombre_raw else "Paciente"
        _esp_wd = row_wd["especialidad"] or "?"
        _phone_wd = row_wd["phone"] or ""
        _phone_fmt = _phone_wd[-8:] if len(_phone_wd) >= 8 else _phone_wd
        try:
            _ts_wd = _dt_wd.fromisoformat(row_wd["created_at"])
            if _ts_wd.tzinfo is None:
                from zoneinfo import ZoneInfo as _ZI_wd
                _ts_wd = _ts_wd.replace(tzinfo=_ZI_wd("America/Santiago"))
            _dias_wd = (_dt_wd.now(_ts_wd.tzinfo) - _ts_wd).days
        except Exception:
            _dias_wd = "?"
        lineas_wd.append(f"• {_nombre_wd} — {_esp_wd} — +56{_phone_fmt} ({_dias_wd} dias)")

    n_wd = len(rows_wd)
    cuerpo_wd = (
        f"*Lista de espera — pacientes con >14 dias sin cupo* ({n_wd} total)\n\n"
        + "\n".join(lineas_wd[:20])
        + ("\n\n... y mas" if n_wd > 20 else "")
        + "\n\nRevisa disponibilidad en Medilink y contáctalos por recepción."
    )

    try:
        from session import is_window_open as _is_win_wd
        _admin_bare = ADMIN_ALERT_PHONE.lstrip("+")
        if _is_win_wd(_admin_bare):
            await send_whatsapp(_admin_bare, cuerpo_wd)
            log.info("waitlist_digest: enviado a %s (%d pacientes)", ADMIN_ALERT_PHONE, n_wd)
        else:
            # Ventana cerrada: loggear como alerta de texto
            log.warning("waitlist_digest: ventana 24h cerrada — digest guardado en log")
            try:
                from pathlib import Path as _Path_wd
                _ALERT_LOG_WD = _Path_wd("/var/log/cmc-watchdog-alerts.log")
                _ALERT_LOG_WD.parent.mkdir(parents=True, exist_ok=True)
                with open(_ALERT_LOG_WD, "a", encoding="utf-8") as _f_wd:
                    _f_wd.write(f"\n--- WAITLIST DIGEST {_dt_wd.now().strftime('%Y-%m-%d')} ---\n{cuerpo_wd}\n")
            except Exception:
                pass
        _le_wd(ADMIN_ALERT_PHONE, "waitlist_digest_semanal", {
            "n_pacientes": n_wd,
            "especialidades": list({r["especialidad"] for r in rows_wd}),
        })
    except Exception as _e_send_wd:
        log.error("waitlist_digest: no se pudo enviar alerta: %s", _e_send_wd)


# ─────────────────────────────────────────────────────────────────────────────
# M5: Follow-up proactivo para intent=info sin cita creada en 10 min
# ─────────────────────────────────────────────────────────────────────────────
def _get_followup_info_enabled() -> bool:
    import os as _os_fi2
    return _os_fi2.getenv("FOLLOWUP_INFO_ENABLED", "true").lower() in ("true", "1", "yes")


async def _job_followup_info():
    """Cada 5 min (cron del scheduler): busca sesiones IDLE que tuvieron intent=info
    en los ultimos 10-20 min, sin cita creada posterior, y envia UNA vez el mensaje
    de reenganche. Guards estrictos: max 1 por sesion, no HUMAN_TAKEOVER,
    ventana 24h Meta, respeta has_recent_event followup_info_enviado (7 dias).
    """
    if not _get_followup_info_enabled():
        return

    import json as _json_fi
    from datetime import datetime as _dt_fi, timezone as _tz_fi
    from session import log_event as _le_fi, has_recent_event as _hre_fi
    from session import db as _conn_fi

    try:
        with _conn_fi() as _c_fi:
            rows_fi = _c_fi.execute("""
                SELECT phone, state, data, updated_at FROM sessions
                WHERE state = 'IDLE'
                  AND updated_at >= datetime('now', '-25 minutes')
                  AND updated_at <= datetime('now', '-10 minutes')
            """).fetchall()
    except Exception as _e_fi:
        log.error("followup_info: error leyendo DB: %s", _e_fi)
        return

    for row_fi in (rows_fi or []):
        _phone_fi = row_fi["phone"] if isinstance(row_fi, dict) else row_fi[0]
        _state_fi = row_fi["state"] if isinstance(row_fi, dict) else row_fi[1]
        _data_raw = row_fi["data"] if isinstance(row_fi, dict) else row_fi[2]
        try:
            _data_fi = _json_fi.loads(_data_raw) if isinstance(_data_raw, str) else (_data_raw or {})
        except Exception:
            continue

        # Guards
        if _data_fi.get("followup_info_sent"):
            continue
        if not _data_fi.get("followup_info_ts"):
            continue
        if _data_fi.get("followup_info_sent") is not False:
            # Solo procesar sesiones explicitamente marcadas (flag=False)
            continue

        # Verificar que no haya cita creada despues del intent info
        _info_ts_str = _data_fi.get("followup_info_ts", "")
        try:
            _info_ts = _dt_fi.fromisoformat(_info_ts_str)
            if _info_ts.tzinfo is None:
                _info_ts = _info_ts.replace(tzinfo=_tz_fi.utc)
            _age_min = (_dt_fi.now(_tz_fi.utc) - _info_ts).total_seconds() / 60
            if _age_min < 10 or _age_min > 25:
                continue
        except Exception:
            continue

        # Anti-spam: max 1 followup_info_enviado por 7 dias
        if _hre_fi(_phone_fi, "followup_info_enviado", days=7):
            continue

        # No enviar a ADMIN_ALERT_PHONE (nunca tiene ventana 24h)
        if _phone_fi == (ADMIN_ALERT_PHONE or "").lstrip("+"):
            continue
        if _phone_fi == ADMIN_ALERT_PHONE:
            continue

        # Verificar ventana 24h abierta
        try:
            from session import is_window_open as _is_win_fi
            if not _is_win_fi(_phone_fi):
                continue
        except Exception:
            continue

        # Verificar que no haya cita creada recientemente en la DB de citas
        try:
            with _conn_fi() as _c_cita:
                _cita_reciente = _c_cita.execute(
                    "SELECT 1 FROM citas_bot WHERE phone=? AND created_at >= ? LIMIT 1",
                    (_phone_fi, _info_ts_str),
                ).fetchone()
            if _cita_reciente:
                # Ya agendó — limpiar flags y saltar
                _data_fi["followup_info_sent"] = True
                try:
                    from session import save_session as _ss_fi
                    _ss_fi(_phone_fi, "IDLE", _data_fi)
                except Exception:
                    pass
                continue
        except Exception:
            pass

        # Armar mensaje de follow-up
        _esp_fi = (_data_fi.get("followup_info_esp") or "").strip()
        _nombre_fi = (_data_fi.get("nombre_conocido") or _data_fi.get("reg_nombre") or "").split()
        _saludo_fi = f"*{_nombre_fi[0]}*, " if _nombre_fi else ""
        if _esp_fi:
            _msg_fi = (
                f"Hola {_saludo_fi}¿te gustaría que te ayude a agendar una hora "
                f"en *{_esp_fi}*? 😊"
            )
        else:
            _msg_fi = f"Hola {_saludo_fi}¿te gustaría que te ayude a agendar una hora? 😊"

        canal_fi = _canal_de_phone(_phone_fi)
        if canal_fi == "unknown":
            continue

        try:
            from flows import _btn_msg as _btn_fi
            _interactive_fi = _btn_fi(
                _msg_fi,
                [
                    {"id": "1",             "title": "Si, agendar"},
                    {"id": "no_gracias_fi", "title": "No, gracias"},
                ],
            )
            if canal_fi == "wa":
                await send_whatsapp_interactive(_phone_fi, _interactive_fi["interactive"])
                log_message(_phone_fi, "out", _msg_fi, "IDLE")
            elif canal_fi == "ig":
                await send_instagram(_phone_fi[3:], _msg_fi)
                log_message(_phone_fi, "out", _msg_fi, "IDLE")
            elif canal_fi == "fb":
                await send_messenger(_phone_fi[3:], _msg_fi)
                log_message(_phone_fi, "out", _msg_fi, "IDLE")
            else:
                continue

            _data_fi["followup_info_sent"] = True
            try:
                from session import save_session as _ss_fi2
                _ss_fi2(_phone_fi, "IDLE", _data_fi)
            except Exception:
                pass
            _le_fi(_phone_fi, "followup_info_enviado", {
                "esp": _esp_fi,
                "age_min": round(_age_min, 1),
            })
            log.info("followup_info: enviado a %s (esp=%s, age=%.1f min)", _phone_fi, _esp_fi, _age_min)
        except Exception as _e_send_fi:
            log.warning("followup_info: error enviando a %s: %s", _phone_fi, _e_send_fi)


# ── Reporte semanal de demanda no capturada (Items 31/32/35) ─────────────────

# Precios de consulta por especialidad (para estimar $ perdidos).
# Fuente: PRECIOS_SLOT en flows.py — mismo origen que los mensajes al paciente.
# Para especialidades con Fonasa+Particular usamos precio particular (conservador).
_PRECIO_ESP: dict[str, int] = {
    "medicina general":       25000,
    "medicina familiar":      30000,
    "otorrinolaringología":   35000,
    "cardiología":            40000,
    "gastroenterología":      35000,
    "ginecología":            30000,
    "traumatología":          30000,
    "kinesiología":           20000,
    "psicología adulto":      20000,
    "psicología infantil":    20000,
    "nutrición":              20000,
    "fonoaudiología":         25000,
    "podología":              20000,
    "ecografía":              40000,
    "odontología general":    15000,
    "ortodoncia":             30000,
    "endodoncia":            110000,
    "implantología":         650000,
    "estética facial":        15000,
    "matrona":                20000,
    "psiquiatría":            60000,
    "neurología":             65000,
    "masoterapia":            22000,
}


async def _job_demanda_semanal():
    """Lunes 09:00 CLT — reporte de demanda no capturada de los últimos 7 días.

    Agrega eventos sin_disponibilidad + demanda_no_disponible + demanda_no_disponible_faq
    de conversation_events, los rankea por phones únicos y estima revenue perdido.
    Incluye cuántos waitlist_notificados del período convirtieron en cita ≤72h.
    Solo lectura — NO contacta pacientes.
    """
    if not ADMIN_ALERT_PHONE:
        log.info("demanda_semanal: ADMIN_ALERT_PHONE no configurado — skip")
        return
    if not _admin_window_open():
        log.info("demanda_semanal: ventana 24h cerrada para ADMIN_ALERT_PHONE — skip")
        return

    try:
        from datetime import datetime, timedelta, timezone
        from zoneinfo import ZoneInfo
        import json

        _TZ_CHILE = ZoneInfo("America/Santiago")
        ahora_clt = datetime.now(_TZ_CHILE)
        hace_7d = (ahora_clt - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")

        with _session_conn() as conn:
            # ── 1. Demanda no capturada por especialidad ──────────────────────
            # Eventos: sin_disponibilidad, demanda_no_disponible, demanda_no_disponible_faq
            rows_demanda = conn.execute("""
                SELECT
                    lower(COALESCE(
                        json_extract(meta, '$.especialidad'),
                        json_extract(meta, '$.solicitud'),
                        'desconocida'
                    )) AS especialidad,
                    COUNT(DISTINCT phone) AS phones_unicos,
                    COUNT(*) AS total_eventos
                FROM conversation_events
                WHERE event IN (
                    'sin_disponibilidad',
                    'demanda_no_disponible',
                    'demanda_no_disponible_faq'
                )
                  AND ts >= ?
                GROUP BY especialidad
                ORDER BY phones_unicos DESC
            """, (hace_7d,)).fetchall()

            # ── 2. Waitlist notificados en el período ─────────────────────────
            rows_wl_total = conn.execute("""
                SELECT COUNT(DISTINCT phone) AS phones_notificados
                FROM conversation_events
                WHERE event = 'waitlist_notificado'
                  AND ts >= ?
            """, (hace_7d,)).fetchone()
            notificados_total = (rows_wl_total["phones_notificados"] if rows_wl_total else 0) or 0

            # ── 3. Conversión waitlist→cita ≤72h ─────────────────────────────
            # Join: phone que recibió waitlist_notificado en el período
            # y luego tiene una cita en citas_bot creada ≤72h después.
            rows_wl_conv = conn.execute("""
                SELECT COUNT(DISTINCT e.phone) AS convertidos
                FROM conversation_events e
                JOIN citas_bot c ON c.phone = e.phone
                WHERE e.event = 'waitlist_notificado'
                  AND e.ts >= ?
                  AND c.created_at >= e.ts
                  AND (
                      julianday(c.created_at) - julianday(e.ts)
                  ) * 86400 <= 259200  -- 72h en segundos
            """, (hace_7d,)).fetchone()
            convertidos = (rows_wl_conv["convertidos"] if rows_wl_conv else 0) or 0

            # ── 4. Waitlist activa por especialidad ───────────────────────────
            rows_wl_activa = conn.execute("""
                SELECT lower(especialidad) AS especialidad, COUNT(*) AS inscriptos
                FROM waitlist
                WHERE canceled_at IS NULL AND notified_at IS NULL
                GROUP BY especialidad
                ORDER BY inscriptos DESC
            """).fetchall()

        # ── Construir resumen ─────────────────────────────────────────────────
        _fecha_inicio = (ahora_clt - timedelta(days=7)).strftime("%d/%m")
        _fecha_fin = ahora_clt.strftime("%d/%m")

        lineas_demanda = []
        total_perdido = 0
        for row in rows_demanda[:10]:  # top 10
            esp = row["especialidad"] or "desconocida"
            phones = row["phones_unicos"] or 0
            precio = _PRECIO_ESP.get(esp, 25000)
            perdido = phones * precio
            total_perdido += perdido
            perdido_fmt = f"${perdido:,}".replace(",", ".")
            lineas_demanda.append(f"• {esp.title()}: {phones} personas ({perdido_fmt})")

        lineas_waitlist = []
        wl_dict = {r["especialidad"]: r["inscriptos"] for r in rows_wl_activa}
        for row in rows_wl_activa[:5]:
            esp = row["especialidad"] or "?"
            lineas_waitlist.append(f"• {esp.title()}: {row['inscriptos']} en lista")

        total_perdido_fmt = f"${total_perdido:,}".replace(",", ".")

        tasa_conv = 0
        if notificados_total > 0:
            tasa_conv = round(convertidos / notificados_total * 100)

        msg_parts = [
            f"*Demanda no capturada — {_fecha_inicio} al {_fecha_fin}*",
            "",
        ]
        if lineas_demanda:
            msg_parts.append("*Por especialidad (phones únicos · $ est.):*")
            msg_parts.extend(lineas_demanda)
            msg_parts.append("")
            msg_parts.append(f"*Total estimado: {total_perdido_fmt}*")
        else:
            msg_parts.append("Sin eventos de demanda no capturada esta semana.")

        if lineas_waitlist:
            msg_parts.append("")
            msg_parts.append("*Lista de espera activa:*")
            msg_parts.extend(lineas_waitlist)

        msg_parts.append("")
        if notificados_total > 0:
            msg_parts.append(
                f"*Conversión waitlist→cita (≤72h):* "
                f"{convertidos}/{notificados_total} ({tasa_conv}%)"
            )
        else:
            msg_parts.append("Sin notificaciones de waitlist esta semana.")

        msg = "\n".join(msg_parts)

        try:
            await send_whatsapp(ADMIN_ALERT_PHONE, msg)
            log.info(
                "demanda_semanal: reporte enviado — %d especialidades · %s perdido · "
                "%d/%d conv waitlist",
                len(rows_demanda), total_perdido_fmt, convertidos, notificados_total,
            )
        except Exception as e:
            log.error("demanda_semanal: fallo enviando reporte WA: %s", e)

    except Exception as e:
        log.error("_job_demanda_semanal falló: %s", e, exc_info=True)


# ─────────────────────────────────────────────────────────────────────────────
# Skills aprendidas (nivel 6) — destila reglas durables del optimizer
# ─────────────────────────────────────────────────────────────────────────────
async def _job_learned_skills():
    """Pasada semanal: corre el optimizer, observa/gradúa/decae skills y persiste.
    No-op si LEARNED_SKILLS_ACTIVE está OFF. Corre la parte síncrona (query al BI)
    en un thread para no bloquear el event loop."""
    try:
        import asyncio
        from autopilot import learned_skills
        rep = await asyncio.to_thread(learned_skills.run)
        if rep.get("active"):
            grad = rep.get("graduated", [])
            log.info("learned_skills: pasada semanal OK — %d skills, %d graduadas%s",
                     len(rep.get("skills", [])), len(grad),
                     (": " + ", ".join(grad)) if grad else "")
        else:
            log.debug("learned_skills: pasada semanal no-op (flag off)")
    except Exception as e:  # noqa: BLE001
        log.error("_job_learned_skills falló: %s", e, exc_info=True)
