"""Scheduler job functions — reenganche, watchdog, waitlist, fidelización wrappers."""
import logging

import httpx

from config import MEDILINK_BASE_URL, MEDILINK_TOKEN, ADMIN_ALERT_PHONE, USE_TEMPLATES
from messaging import (send_whatsapp, send_whatsapp_interactive, send_instagram, send_messenger,
                       send_whatsapp_template, send_whatsapp_proactive, is_proactive_blocked)
from reminders import enviar_recordatorios, enviar_recordatorios_2h, enviar_recordatorios_48h
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
                     get_waitlist_pending, mark_waitlist_notified,
                     get_cita_bot_by_id_for_rebook, mark_cita_cancel_detected,
                     get_profile,
                     get_candidatos_horas_vacias, log_horas_vacias_envio,
                     get_horas_vacias_envios_hoy,
                     phone_tiene_solo_citas_canceladas,
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
        if phone_tiene_solo_citas_canceladas(phone):
            log_event(phone, "reenganche_skip_cita_cancelada", {"state": state})
            log.info("Reenganche skip (cita cancelada) → %s", phone)
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
        save_session(phone, state, data)
        log_event(phone, "reenganche_enviado", {"state": state, "canal": canal})
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

async def _job_recordatorios_2h():
    await enviar_recordatorios_2h(send_whatsapp_proactive, send_template_fn=_tpl)

async def _job_postconsulta():
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
    from session import _conn

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
                                log.debug("enrolar: atribuir_cita_a_winback error: %s", _wb_job_err)
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
                from messaging import render_template_body as _rtb_sr
                log_message(phone_p, "out", _rtb_sr("sistema_recuperado"), "IDLE")
            else:
                _sr_msg = (
                    "✅ ¡Buenas noticias! Nuestro sistema de citas ya está operativo de nuevo 🎉\n\n"
                    "Si quieres retomar lo que estabas haciendo, escribe *menu* y te ayudo al tiro.\n\n"
                    "_Gracias por tu paciencia._"
                )
                await send_whatsapp(phone_p, _sr_msg)
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
    # Bug fix 2026-05-28: antes el job notificaba a TODAS las personas en cola
    # el mismo primer slot disponible (caso eco lun 1-jun 10:00 → 5 personas
    # recibieron el mismo aviso). Ahora cada slot único (fecha+hora) se asigna
    # a UNA sola persona por corrida. Las que no alcancen quedan pendientes
    # para la próxima ejecución del cron diario.
    _slots_consumidos_run = {}
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
                await send_instagram(phone, msg)
                log_message(phone, "out", msg, "IDLE")
            elif canal == "fb":
                await send_messenger(phone, msg)
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


async def _job_crosssell_dx():
    """Cross-sell contextual por dx tags (dm2, hta, gineco/PAP).
    CROSS_SELL_ACTIVE=false hasta piloto N=5 confirmado por Rodrigo.
    """
    try:
        await enviar_crosssell_dx(send_whatsapp_proactive, send_template_fn=_tpl)
    except Exception as e:
        log.error("_job_crosssell_dx falló: %s", e)


async def _job_marketing_consent_blast():
    """Blast diario L-V 10:30 CLT: envía consent_marketing_v1 (UTILITY)
    a phones en v_winback_cohortes_contactables sin registro en marketing_consent.

    MARKETING_CONSENT_BLAST_ACTIVE=false hasta que Rodrigo confirme
    que consent_marketing_v1 está APPROVED en Meta.
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
        if not await is_template_approved("consent_marketing_v1"):
            log.warning("consent_template_not_approved: consent_marketing_v1 no está APPROVED en Meta — skip")
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

        # Candidatos: en cohortes_contactables pero sin registro consent
        # Seleccionar también nombre para el {{1}} del template consent_marketing_v1
        with bi_conn() as conn2:
            with conn2.cursor() as cur:
                cur.execute(
                    "SELECT wc.telefono, wc.nombre FROM bi.v_winback_cohortes_contactables wc "
                    "WHERE wc.telefono NOT IN (SELECT phone FROM bi.marketing_consent) "
                    f"LIMIT {LIMITE_DIA - enviados_hoy}"
                )
                candidates = [(r[0], r[1] or "Paciente") for r in cur.fetchall()]

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
                    "consent_marketing_v1",
                    body_params=[nombre],
                )
                from messaging import render_template_body as _rtb_cm
                log_message(phone, "out", _rtb_cm("consent_marketing_v1", [nombre]), "IDLE")
                registrar_consent_enviado(phone)
                enviados += 1
                log.info("consent_blast enviado → %s (%d/%d)", phone, enviados, len(candidates))
            except Exception as e:
                log.error("consent_blast error phone=%s: %s", phone, e)
            await _asyncio_mc.sleep(SLEEP_ENTRE)

        log.info("consent_blast: sesión completada, enviados=%d", enviados)
    except Exception as e:
        log.error("_job_marketing_consent_blast falló: %s", e)


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
    debe_reactivar = (
        not flag_actual
        and fue_auto_set
        and errores_total <= 1
        and quality_rating == "GREEN"
        and not (tasa_rechazo > 40.0 and muestra_rechazo >= 30)
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
            f"Dashboard: agentecmc.cl/winback?token=cmc_admin_2026\n"
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
        f"Dashboard: agentecmc.cl/winback?token=cmc_admin_2026"
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
