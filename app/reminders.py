"""
Recordatorios automáticos:
- 24h antes (mensaje interactivo con 3 botones): diario a las 9:00 CLT
- 2h antes (mensaje texto corto, fresh-in-mind): cron interval 15 min
"""
import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from config import USE_TEMPLATES, ADMIN_ALERT_PHONE, RECORDATORIOS_RECEPCION_ENABLED
from messaging import render_template_body
from session import (
    get_citas_bot_pendientes,
    get_citas_bot_para_2h_reminder,
    get_citas_bot_para_48h_reminder,
    get_last_inbound_ts,
    mark_reminder_sent,
    mark_reminder_2h_sent,
    mark_reminder_48h_sent,
    tiene_historial_noshow,
    get_waitlist_by_especialidad,
    mark_waitlist_notified,
    log_message,
    log_event,
    get_citas_recepcion_pendientes_24h,
    get_citas_recepcion_pendientes_2h,
    get_citas_recepcion_pendientes_48h,
    mark_recepcion_reminder_24h_sent,
    mark_recepcion_reminder_2h_sent,
    mark_recepcion_reminder_48h_sent,
)
from medilink import estado_cita_actual, PROFESIONALES

log = logging.getLogger("bot.reminders")
_TZ_CL = ZoneInfo("America/Santiago")

# Reverso nombre→id de PROFESIONALES. citas_bot no guarda id_profesional (solo
# el nombre en texto), pero para pedirle a Medilink el estado de TODAS las
# citas de un profesional/día en una sola llamada (en vez de una por cita)
# necesitamos el ID. Se construye una sola vez al importar el módulo.
_PROF_NOMBRE_A_ID = {info["nombre"]: pid for pid, info in PROFESIONALES.items()}


async def _estado_pre_envio(id_cita, id_prof: int | None, fecha: str) -> dict | None:
    """Consulta el estado ACTUAL de una cita en Medilink justo antes de enviar
    un recordatorio (la recepción confirma/anula por teléfono durante el día,
    y eso puede pasar en cualquier momento entre el sync y el envío).

    Devuelve `{"anulada", "confirmada", "estado_cita", "id_estado", "id_paciente"}`.
    Si `id_cita` no es un ID numérico de Medilink (reservas manuales tipo
    "manual-PHONE-TS" del panel admin, sin registro en Medilink) devuelve un
    estado neutro — no hay nada que anular/confirmar, se envía igual.
    Si la consulta a Medilink falla, devuelve `None` (fail-safe: el caller NO
    debe enviar ante duda)."""
    if not id_cita:
        return None
    id_cita_str = str(id_cita)
    if not id_cita_str.isdigit():
        return {"anulada": False, "confirmada": False, "estado_cita": "", "id_estado": None, "id_paciente": None}
    try:
        return await estado_cita_actual(int(id_cita_str), id_prof=id_prof, fecha=fecha)
    except Exception as e:
        log.warning("Pre-envío: consulta Medilink id_cita=%s falló: %s", id_cita, e)
        return None


def _dedup_citas(citas: list[dict]) -> list[dict]:
    """Deduplica citas por (phone, fecha, hora) para evitar enviar el mismo
    recordatorio dos veces cuando citas_bot tiene duplicados (ej. cita manual
    + cita sincronizada de Medilink para la misma hora). Prefiere la de menor
    id (normalmente la primera creada)."""
    seen: dict[tuple, dict] = {}
    for c in citas:
        key = (c.get("phone"), c.get("fecha"), (c.get("hora") or "")[:5])
        if key not in seen or (c.get("id") or 0) < (seen[key].get("id") or 0):
            seen[key] = c
    return list(seen.values())


def _fmt_hora(hora: str) -> str:
    """'10:30:00' → '10:30'"""
    return hora[:5]


def _fmt_fecha_display(fecha: str) -> str:
    """'2026-04-04' → 'Viernes 4 de abril'"""
    DIAS = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
    MESES = ["", "enero", "febrero", "marzo", "abril", "mayo", "junio",
             "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"]
    try:
        d = date.fromisoformat(fecha)
        return f"{DIAS[d.weekday()]} {d.day} de {MESES[d.month]}"
    except Exception:
        return fecha


# Ver mismo set en fidelizacion.py — nombres centinela que no deben mostrarse
# tal cual al paciente (placeholders de flujos incompletos). Auditoría 2026-08-19 (#5).
_NOMBRE_CENTINELA = {"paciente", "otra", "otro", "none", "null", "desconocido", "-"}


def _nombre_corto(nombre: str | None) -> str:
    """'Sergio Carrasco Cordero' → 'Sergio'"""
    if not nombre:
        return ""
    primera = nombre.strip().split()[0]
    if primera.lower() in _NOMBRE_CENTINELA:
        return ""
    return primera.capitalize()


def _interactive_recordatorio(cita: dict) -> dict:
    """Construye un mensaje interactivo con 3 botones: confirmo / cambiar hora / no podré ir.
    Los IDs de botón incluyen el id_cita Medilink para poder resolver la cita en la respuesta,
    incluso si el paciente no tiene una sesión activa.
    Si es_tercero=1, el recordatorio va dirigido al dueño del celular (phone_owner)
    mencionando al paciente por nombre."""
    fecha_display = _fmt_fecha_display(cita["fecha"])
    hora = _fmt_hora(cita["hora"])
    esp = cita["especialidad"]
    prof = cita["profesional"]
    modalidad = (cita.get("modalidad") or "particular").capitalize()
    id_cita = cita["id_cita"]
    es_tercero = cita.get("es_tercero", 0)
    nombre_paciente = _nombre_corto(cita.get("paciente_nombre"))
    nombre_owner = _nombre_corto(cita.get("phone_owner"))
    if es_tercero and nombre_owner and nombre_paciente:
        saludo = f"Hola {nombre_owner} 👋"
        intro = f"Recuerda que *{nombre_paciente}* tiene cita"
    else:
        saludo = f"Hola {nombre_paciente} 👋" if nombre_paciente else "Hola 👋"
        intro = "Te recordamos tu cita"
    body = (
        f"{saludo} {intro} en el *Centro Médico Carampangue*:\n\n"
        f"🏥 *{esp}* — {prof}\n"
        f"📅 *{fecha_display}* a las *{hora}*\n"
        f"💳 {modalidad}\n"
        "📍 Monsalve esquina República, Carampangue\n\n"
        "Recuerda llegar *15 minutos antes* con tu cédula de identidad.\n\n"
        "¿Nos confirmas tu asistencia?"
    )
    return {
        "type": "button",
        "body": {"text": body},
        "action": {
            "buttons": [
                {"type": "reply", "reply": {"id": f"cita_confirm:{id_cita}", "title": "✅ Confirmo"}},
                {"type": "reply", "reply": {"id": f"cita_reagendar:{id_cita}", "title": "🔄 Cambiar hora"}},
                {"type": "reply", "reply": {"id": f"cita_cancelar:{id_cita}", "title": "❌ No podré ir"}},
            ]
        },
    }


async def enviar_recordatorios(send_text_fn, send_interactive_fn=None,
                               send_template_fn=None):
    """
    Busca citas para mañana y envía recordatorio por WhatsApp.

    - send_text_fn: fallback send_whatsapp(to, body) — usado si no hay send_interactive_fn.
    - send_interactive_fn: send_whatsapp_interactive(to, interactive_dict) — si se pasa,
      el recordatorio es un mensaje con 3 botones (confirmo / cambiar hora / no podré ir).
    - send_template_fn: send_whatsapp_template — usado cuando USE_TEMPLATES=True.
    """
    manana = (datetime.now(ZoneInfo("America/Santiago")).date() + timedelta(days=1)).isoformat()
    citas = _dedup_citas(get_citas_bot_pendientes(manana))

    if not citas:
        log.info("Recordatorios: sin citas para %s", manana)
        return

    # ── PRE-VALIDACIÓN contra Medilink ────────────────────────────────────────
    # Antes de mandar cada recordatorio, verificar el estado ACTUAL de la cita
    # en Medilink (no el que tenía al sincronizar) — la recepción confirma o
    # anula por teléfono durante el día. Caso real 2026-05-03: Sebastian
    # recibió recordatorio de cita 54874 (Quijano lunes 4-may 10:00) que en
    # Medilink figura estado_anulacion=1 hace 20 días con otro paciente
    # reasignado. El bot no detectaba la cancelación y mandaba el recordatorio
    # igual.
    #
    # Regla (2026-07-23): ANULADA → no se envía NINGÚN recordatorio (24h/48h/2h).
    # CONFIRMADA por teléfono → no se envía el de 24h ni el de 48h (ya sabemos
    # que va a venir), pero SÍ el de 2h (recordatorio del día, siempre útil).
    try:
        from session import mark_cita_cancel_detected
        citas_validas = []
        for c in citas:
            id_cita = c.get("id_cita")
            if not id_cita:
                continue
            id_prof = _PROF_NOMBRE_A_ID.get(c.get("profesional"))
            estado = await _estado_pre_envio(id_cita, id_prof, c.get("fecha"))
            await asyncio.sleep(0.7)  # throttle Medilink (evita 429-tormenta)
            if estado is None:
                log.warning("Recordatorio: cita %s no encontrada/consulta falló en Medilink, salto", id_cita)
                continue
            if estado["anulada"]:
                log.warning("Recordatorio: cita %s ANULADA en Medilink, no se envía y se marca local",
                            id_cita)
                mark_cita_cancel_detected(str(id_cita))
                continue
            if estado["confirmada"]:
                log.info("Recordatorio 24h: cita %s CONFIRMADA por recepción, skip (queda 2h)", id_cita)
                log_event(c["phone"], "recordatorio_skip_confirmada", {
                    "id_cita": id_cita, "riel": "bot", "ventana": "24h",
                })
                mark_reminder_sent(c["id"])
                continue
            # Verificar que el paciente siga siendo el mismo (slot reasignado)
            id_pac_local = c.get("id_paciente_medilink")
            id_pac_ml = estado.get("id_paciente")
            if id_pac_local and id_pac_ml and str(id_pac_local) != str(id_pac_ml):
                log.warning("Recordatorio: cita %s reasignada en Medilink (paciente %s→%s), salto",
                            id_cita, id_pac_local, id_pac_ml)
                mark_cita_cancel_detected(str(id_cita))
                continue
            citas_validas.append(c)
        if len(citas_validas) < len(citas):
            log.info("Recordatorios: %d/%d válidas tras validación Medilink",
                     len(citas_validas), len(citas))
        citas = citas_validas
    except Exception as e:
        log.exception("Error en pre-validación de recordatorios: %s", e)
        # Si falla la validación entera, mejor NO mandar nada que mandar falsos positivos
        return

    if not citas:
        log.info("Recordatorios: sin citas válidas tras validación Medilink")
        return

    # Filtrar citas cuyo phone es el número personal del admin (ADMIN_ALERT_PHONE).
    # Ese número nunca tiene ventana 24h abierta desde el bot → 131047 garantizado.
    # Las citas del Dr. deben registrarse con el número del paciente real, no el suyo.
    _admin_phone_clean = (ADMIN_ALERT_PHONE or "").lstrip("+")
    if _admin_phone_clean:
        before = len(citas)
        citas = [c for c in citas if (c.get("phone") or "").lstrip("+") != _admin_phone_clean]
        if len(citas) < before:
            log.info("Recordatorios: omitidas %d cita(s) con phone=ADMIN (%s)",
                     before - len(citas), _admin_phone_clean)

    if not citas:
        log.info("Recordatorios: sin citas tras filtro ADMIN para %s", manana)
        return

    log.info("Recordatorios: enviando %d recordatorio(s) para %s", len(citas), manana)
    for cita in citas:
        try:
            # No enviar recordatorios automáticos mientras una recepcionista
            # está atendiendo esta conversación (HUMAN_TAKEOVER) — el paciente
            # puede confundirse recibiendo mensajes cruzados del bot y de una
            # persona real al mismo tiempo. Auditoría 2026-08-19 (#4).
            try:
                from session import get_session as _get_sess_rec
                if (_get_sess_rec(cita["phone"]) or {}).get("state") == "HUMAN_TAKEOVER":
                    log.info("Recordatorio 24h: phone=%s en HUMAN_TAKEOVER, no se envía",
                             cita["phone"])
                    log_event(cita["phone"], "recordatorio_skip_takeover", {"id_cita": cita.get("id_cita")})
                    continue
            except Exception:
                pass
            if USE_TEMPLATES and send_template_fn:
                # Template: recordatorio_cita
                # body_params: [nombre, especialidad, profesional, fecha_display, hora, modalidad]
                # button_payloads: ["cita_confirm:{id}", "cita_reagendar:{id}", "cita_cancelar:{id}"]
                nombre = _nombre_corto(cita.get("paciente_nombre")) or "paciente"
                fecha_display = _fmt_fecha_display(cita["fecha"])
                hora = _fmt_hora(cita["hora"])
                modalidad = (cita.get("modalidad") or "particular").capitalize()
                id_cita = cita["id_cita"]
                _tpl_params = [nombre, cita["especialidad"], cita["profesional"],
                               fecha_display, hora, modalidad]
                _wamid = await send_template_fn(
                    cita["phone"],
                    "recordatorio_cita",
                    body_params=_tpl_params,
                    button_payloads=[f"cita_confirm:{id_cita}",
                                     f"cita_reagendar:{id_cita}",
                                     f"cita_cancelar:{id_cita}"],
                )
                if _wamid is None:
                    log.error("Recordatorio (template) FALLÓ (sin wamid) → %s cita_id=%s; "
                              "NO se marca reminder_sent", cita["phone"], id_cita)
                    log_event(cita["phone"], "template_fallido", {
                        "template": "recordatorio_cita",
                        "id_cita": id_cita,
                    })
                    continue
                log_event(cita["phone"], "template_enviado", {
                    "template": "recordatorio_cita",
                    "id_cita": id_cita,
                })
                log_message(cita["phone"], "out",
                            render_template_body("recordatorio_cita", _tpl_params),
                            "IDLE")
                mark_reminder_sent(cita["id"])
                log.info("Recordatorio (template) enviado → %s cita_id=%s",
                         cita["phone"], id_cita)
                continue
            elif send_interactive_fn:
                await send_interactive_fn(cita["phone"], _interactive_recordatorio(cita))
            else:
                # Fallback texto plano
                fecha_display = _fmt_fecha_display(cita["fecha"])
                hora = _fmt_hora(cita["hora"])
                nombre_pac = _nombre_corto(cita.get("paciente_nombre"))
                nombre_own = _nombre_corto(cita.get("phone_owner"))
                es_terc = cita.get("es_tercero", 0)
                if es_terc and nombre_own and nombre_pac:
                    saludo = f"Hola {nombre_own} 👋"
                    intro = f"Recuerda que *{nombre_pac}* tiene cita"
                else:
                    saludo = f"Hola {nombre_pac} 👋" if nombre_pac else "Hola 👋"
                    intro = "Te recordamos tu cita"
                await send_text_fn(
                    cita["phone"],
                    f"{saludo} {intro} en el *Centro Médico Carampangue*:\n\n"
                    f"🏥 *{cita['especialidad']}* — {cita['profesional']}\n"
                    f"📅 *{fecha_display}* a las *{hora}*\n"
                    "📍 Monsalve esquina República, Carampangue\n\n"
                    "Recuerda llegar *15 minutos antes* con tu cédula de identidad.\n\n"
                    "¿Confirmas tu asistencia? Responde *SÍ* o *NO*."
                )
            fecha_d = _fmt_fecha_display(cita["fecha"])
            hora_d = _fmt_hora(cita["hora"])
            log_message(cita["phone"], "out",
                        f"[Recordatorio] {cita['especialidad']} con {cita['profesional']} — {fecha_d} a las {hora_d}",
                        "IDLE")
            mark_reminder_sent(cita["id"])
            log.info("Recordatorio enviado → %s cita_id=%s", cita["phone"], cita["id_cita"])
        except Exception as e:
            log.error("Error enviando recordatorio cita_id=%s: %s", cita.get("id"), e)


async def enviar_recordatorios_2h(send_text_fn, send_template_fn=None):
    """Busca citas del día en la ventana [1h45, 2h15] desde ahora (CLT) y envía
    un recordatorio corto de "en 2 horas" al paciente. Se ejecuta cada 15 min.

    Usa la marca `reminder_2h_sent` para evitar duplicados cuando la ventana
    abarca varios ticks del cron. Saltea citas ya canceladas vía botón.
    """
    ahora = datetime.now(_TZ_CL)
    fecha_hoy = ahora.date().isoformat()
    # Ventana: entre 1h45 y 2h15 desde ahora → horas a buscar
    hora_min_dt = ahora + timedelta(minutes=105)
    hora_max_dt = ahora + timedelta(minutes=135)
    # Solo el mismo día (si la ventana cruza medianoche salteamos — raro en CMC)
    if hora_min_dt.date() != ahora.date() or hora_max_dt.date() != ahora.date():
        return
    hora_min = hora_min_dt.strftime("%H:%M:%S")
    hora_max = hora_max_dt.strftime("%H:%M:%S")

    citas = _dedup_citas(get_citas_bot_para_2h_reminder(fecha_hoy, hora_min, hora_max))
    if not citas:
        return

    # Pre-validación contra Medilink (igual que recordatorio diario).
    # Si la cita está anulada o reasignada, no manda y marca local. A
    # diferencia del recordatorio de 24h/48h, aquí NO se saltea por estar
    # "confirmada" — el recordatorio de 2h se envía siempre que la cita siga
    # vigente, incluso si la recepción ya la confirmó por teléfono (regla
    # 2026-07-23: el de 2h es el único que se manda pase lo que pase, salvo
    # anulación).
    try:
        from session import mark_cita_cancel_detected
        validas = []
        for c in citas:
            id_c = c.get("id_cita")
            if not id_c:
                continue
            id_prof = _PROF_NOMBRE_A_ID.get(c.get("profesional"))
            estado = await _estado_pre_envio(id_c, id_prof, c.get("fecha"))
            await asyncio.sleep(0.7)  # throttle Medilink (evita 429-tormenta)
            if estado is None:
                continue
            if estado["anulada"]:
                log.warning("Recordatorio 2h: cita %s anulada, marca local", id_c)
                mark_cita_cancel_detected(str(id_c))
                continue
            id_pac_local = c.get("id_paciente_medilink")
            id_pac_ml = estado.get("id_paciente")
            if id_pac_local and id_pac_ml and str(id_pac_local) != str(id_pac_ml):
                log.warning("Recordatorio 2h: cita %s reasignada (%s→%s), marca local",
                            id_c, id_pac_local, id_pac_ml)
                mark_cita_cancel_detected(str(id_c))
                continue
            validas.append(c)
        citas = validas
    except Exception as e:
        log.exception("Pre-validación 2h falló: %s", e)
        return

    if not citas:
        return

    # Filtrar phone del admin (igual que en enviar_recordatorios)
    _admin_phone_clean = (ADMIN_ALERT_PHONE or "").lstrip("+")
    if _admin_phone_clean:
        before = len(citas)
        citas = [c for c in citas if (c.get("phone") or "").lstrip("+") != _admin_phone_clean]
        if len(citas) < before:
            log.info("Recordatorios 2h: omitidas %d cita(s) con phone=ADMIN", before - len(citas))
    if not citas:
        return

    log.info("Recordatorios 2h: enviando %d recordatorio(s) ventana [%s-%s]",
             len(citas), hora_min, hora_max)
    for cita in citas:
        try:
            # Re-validación de hora justo antes de enviar: `citas` se calculó
            # contra la ventana [1h45, 2h15] al INICIO del job, pero la validación
            # Medilink de arriba (una llamada por cita, con throttle 0.7s + reintentos
            # si Medilink está lento/caído) puede demorar minutos u horas en un
            # backlog grande. Sin este guard, un recordatorio "en 2 horas" puede
            # salir cuando la cita YA PASÓ. Caso real 56985831922/56994853413
            # (auditoría 2026-08-19): recordatorio a las 17:45 para cita de las 16:xx.
            try:
                _cita_dt_chk = datetime.strptime(
                    f"{cita['fecha']} {cita['hora']}", "%Y-%m-%d %H:%M:%S"
                ).replace(tzinfo=_TZ_CL)
            except ValueError:
                _cita_dt_chk = datetime.strptime(
                    f"{cita['fecha']} {cita['hora']}", "%Y-%m-%d %H:%M"
                ).replace(tzinfo=_TZ_CL)
            if _cita_dt_chk <= datetime.now(_TZ_CL):
                log.warning("Recordatorio 2h: cita %s ya pasó (%s), no se envía",
                            cita.get("id_cita"), _cita_dt_chk)
                log_event(cita["phone"], "recordatorio_2h_skip_hora_pasada",
                          {"id_cita": cita.get("id_cita"), "fecha_hora": str(_cita_dt_chk)})
                continue
            try:
                from session import get_session as _get_sess_rec2
                if (_get_sess_rec2(cita["phone"]) or {}).get("state") == "HUMAN_TAKEOVER":
                    log.info("Recordatorio 2h: phone=%s en HUMAN_TAKEOVER, no se envía", cita["phone"])
                    log_event(cita["phone"], "recordatorio_skip_takeover", {"id_cita": cita.get("id_cita")})
                    continue
            except Exception:
                pass
            hora = _fmt_hora(cita["hora"])
            nombre_pac = _nombre_corto(cita.get("paciente_nombre")) or "paciente"
            nombre_own = _nombre_corto(cita.get("phone_owner"))
            es_terc = cita.get("es_tercero", 0)
            id_cita_medilink = cita.get("id_cita")

            # Decide si usar template (pagado) o texto plano (gratis si hay service window)
            # Meta abre service window de 24h cuando el paciente envia un mensaje.
            # Dentro de esa ventana, texto plano va sin costo (no se usa template).
            last_in = get_last_inbound_ts(cita["phone"])
            en_service_window = False
            if last_in is not None:
                ahora_utc = datetime.now(timezone.utc)
                en_service_window = (ahora_utc - last_in) < timedelta(hours=24)

            if en_service_window:
                if es_terc and nombre_own and nombre_pac:
                    saludo = f"Hola {nombre_own}"
                    intro = f"⏰ *En 2 horas* *{nombre_pac}* tiene cita"
                else:
                    saludo = f"Hola {nombre_pac}" if nombre_pac != "paciente" else "Hola"
                    intro = "⏰ *En 2 horas* tienes tu cita"
                await send_text_fn(
                    cita["phone"],
                    f"{saludo} {intro} en el *Centro Médico Carampangue*:\n\n"
                    f"🏥 *{cita['especialidad']}* — {cita['profesional']}\n"
                    f"🕐 Hoy a las *{hora}*\n"
                    f"📍 Monsalve esquina República, Carampangue\n\n"
                    "Recuerda llegar *15 minutos antes* con tu cédula de identidad."
                )
                log.info("Recordatorio 2h (service window, free) → %s cita_id=%s",
                         cita["phone"], id_cita_medilink)
                try:
                    log_event(cita["phone"], "savings:skip_reminder_2h_service_window",
                              {"id_cita": id_cita_medilink})
                except Exception:
                    pass
            elif USE_TEMPLATES and send_template_fn:
                _tpl_params_2h = [nombre_pac, cita["especialidad"],
                                  cita["profesional"], hora]
                _wamid_2h = await send_template_fn(
                    cita["phone"],
                    "recordatorio_cita_2h",
                    body_params=_tpl_params_2h,
                )
                if _wamid_2h is None:
                    log.error("Recordatorio 2h (template) FALLÓ (sin wamid) → %s cita_id=%s; "
                              "NO se marca reminder_2h_sent", cita["phone"], id_cita_medilink)
                    log_event(cita["phone"], "template_fallido", {
                        "template": "recordatorio_cita_2h",
                        "id_cita": id_cita_medilink,
                    })
                    continue
                log_event(cita["phone"], "template_enviado", {
                    "template": "recordatorio_cita_2h",
                    "id_cita": id_cita_medilink,
                })
                log.info("Recordatorio 2h (template, paid) → %s cita_id=%s",
                         cita["phone"], id_cita_medilink)
            else:
                # Fallback: sin templates y sin service window → intenta texto plano igual
                # (WhatsApp rechazara si no hay ventana; fallo se loguea)
                if es_terc and nombre_own and nombre_pac:
                    saludo = f"Hola {nombre_own}"
                    intro = f"⏰ *En 2 horas* *{nombre_pac}* tiene cita"
                else:
                    saludo = f"Hola {nombre_pac}" if nombre_pac != "paciente" else "Hola"
                    intro = "⏰ *En 2 horas* tienes tu cita"
                await send_text_fn(
                    cita["phone"],
                    f"{saludo} {intro} en el *Centro Médico Carampangue*:\n\n"
                    f"🏥 *{cita['especialidad']}* — {cita['profesional']}\n"
                    f"🕐 Hoy a las *{hora}*\n"
                    f"📍 Monsalve esquina República, Carampangue\n\n"
                    "Recuerda llegar *15 minutos antes* con tu cédula de identidad."
                )

            # log_message: si fue por template usamos el body renderizado;
            # si fue por service window o fallback, texto genérico.
            _tpl_was_used = USE_TEMPLATES and send_template_fn and not en_service_window
            if _tpl_was_used:
                log_message(cita["phone"], "out",
                            render_template_body("recordatorio_cita_2h",
                                                 [nombre_pac, cita["especialidad"],
                                                  cita["profesional"], hora]),
                            "IDLE")
            else:
                log_message(cita["phone"], "out",
                            f"[Recordatorio 2h] {cita['especialidad']} con {cita['profesional']} — hoy a las {hora}",
                            "IDLE")
            mark_reminder_2h_sent(cita["id"])

            # ── Aviso de liberación de slot (anti no-show) ────────────────────
            # Si la cita es en peak horario (16-19h) o el paciente tiene historial
            # de no-show Y no ha confirmado todavía → enviar aviso de liberación.
            _cita_confirmation = cita.get("confirmation_status")
            _cita_no_confirmada = (
                _cita_confirmation is None
                or _cita_confirmation not in ("confirmed",)
            )
            if _cita_no_confirmada:
                try:
                    _hora_int = int((cita.get("hora") or "00")[:2])
                    _es_peak = 16 <= _hora_int < 19
                except (ValueError, TypeError):
                    _es_peak = False
                _tiene_noshow = tiene_historial_noshow(cita["phone"])
                if _es_peak or _tiene_noshow:
                    try:
                        await send_text_fn(
                            cita["phone"],
                            "Si no puedes asistir, por favor avísanos respondiendo *NO*.\n\n"
                            "Tu hora queda disponible en *30 minutos* si no recibimos confirmación, "
                            "para que otro paciente pueda ocuparla."
                        )
                        log_event(cita["phone"], "aviso_liberacion_slot", {
                            "id_cita": cita.get("id_cita"),
                            "motivo": ("peak" if _es_peak else "")
                                      + ("+noshow" if _tiene_noshow else ""),
                        })
                        # Notificar lista de espera para esta especialidad
                        try:
                            _waitlist = get_waitlist_by_especialidad(cita["especialidad"])
                            if _waitlist:
                                _primer = _waitlist[0]
                                _wp = _primer.get("phone", "")
                                if _wp:
                                    await send_text_fn(
                                        _wp,
                                        f"Hay una posible hora disponible en "
                                        f"*{cita['especialidad']}* hoy a las *{hora}*.\n\n"
                                        "Escribe *agendar* si te interesa tomarla."
                                    )
                                    # FIX F045: este es un aviso especulativo (el slot aún
                                    # no se liberó realmente). NO marcar notified_at aquí
                                    # para que el paciente siga en waitlist y reciba la
                                    # oferta real cuando la operativa confirme la cancelación.
                                    # Solo loggear el aviso enviado.
                                    log_event(_wp, "waitlist_notif_slot_liberado", {
                                        "especialidad": cita["especialidad"],
                                        "hora": hora,
                                        "especulativo": True,
                                    })
                        except Exception as _ew:
                            log.warning("Error notificando waitlist slot liberado: %s", _ew)
                    except Exception as _ea:
                        log.warning("Error aviso liberación slot cita_id=%s: %s",
                                    cita.get("id_cita"), _ea)

            log.info("Recordatorio 2h enviado → %s cita_id=%s", cita["phone"], cita["id_cita"])
        except Exception as e:
            log.error("Error enviando recordatorio 2h cita_id=%s: %s", cita.get("id"), e)


# ── Recordatorio 48h anti no-show ─────────────────────────────────────────────

async def enviar_recordatorios_48h(send_text_fn, send_interactive_fn=None):
    """Recordatorio 48h antes para pacientes con riesgo de no-show.

    Se envía a:
    1. Pacientes con >= 1 cita anterior sin confirmación (historial de no-show).
    2. Cualquier cita en franja 16:00-19:00 (peak no-show).

    Incluye botones de confirmación interactivos.
    Se ejecuta diariamente a las 10:00 CLT con fecha pasado mañana.
    """
    pasado_manana = (
        datetime.now(ZoneInfo("America/Santiago")).date() + timedelta(days=2)
    ).isoformat()

    citas = _dedup_citas(get_citas_bot_para_48h_reminder(pasado_manana))
    if not citas:
        log.info("Recordatorios 48h: sin citas para %s", pasado_manana)
        return

    # Validar contra Medilink. Regla 2026-07-23: ANULADA → no se envía nada;
    # CONFIRMADA por recepción → no se envía el 48h (queda igual el de 2h).
    try:
        from session import mark_cita_cancel_detected as _mcc
        validas = []
        for c in citas:
            id_c = c.get("id_cita")
            if not id_c:
                continue
            id_prof = _PROF_NOMBRE_A_ID.get(c.get("profesional"))
            estado = await _estado_pre_envio(id_c, id_prof, c.get("fecha"))
            await asyncio.sleep(0.7)  # throttle Medilink (evita 429-tormenta)
            if estado is None:
                continue
            if estado["anulada"]:
                _mcc(str(id_c))
                continue
            if estado["confirmada"]:
                log.info("Recordatorio 48h: cita %s CONFIRMADA por recepción, skip", id_c)
                log_event(c["phone"], "recordatorio_skip_confirmada", {
                    "id_cita": id_c, "riel": "bot", "ventana": "48h",
                })
                mark_reminder_48h_sent(c["id"])
                continue
            # Verificar que el paciente siga siendo el mismo (slot reasignado)
            id_pac_local = c.get("id_paciente_medilink")
            id_pac_ml = estado.get("id_paciente")
            if id_pac_local and id_pac_ml and str(id_pac_local) != str(id_pac_ml):
                log.warning("Recordatorio 48h: cita %s reasignada en Medilink "
                            "(paciente %s→%s), salto", id_c, id_pac_local, id_pac_ml)
                _mcc(str(id_c))
                continue
            validas.append(c)
        citas = validas
    except Exception as e:
        log.exception("Pre-validación 48h falló: %s", e)
        return

    if not citas:
        return

    # Especialidades con ticket alto que siempre reciben reminder_48h,
    # independiente de historial de no-show y horario.
    # $48K odonto, $34K eco, $28K gineco, $24K ORL, estética y dental en gral.
    _ESPECIALIDADES_TICKET_ALTO = {
        "odontología general",
        "ortodoncia",
        "endodoncia",
        "implantología",
        "estética facial",
        "ecografía",
        "ginecología",
        "otorrinolaringología",
    }

    enviados = 0
    for cita in citas:
        phone = cita["phone"]
        hora  = _fmt_hora(cita["hora"])
        esp   = cita["especialidad"]
        prof  = cita["profesional"]

        try:
            _hora_int = int(hora[:2])
            _aplica_peak = 16 <= _hora_int < 19
        except (ValueError, TypeError):
            _aplica_peak = False
        _aplica_noshow = tiene_historial_noshow(phone)
        _aplica_ticket_alto = (esp or "").lower() in _ESPECIALIDADES_TICKET_ALTO

        if not (_aplica_noshow or _aplica_peak or _aplica_ticket_alto):
            continue

        fecha_display = _fmt_fecha_display(cita["fecha"])
        nombre_pac    = _nombre_corto(cita.get("paciente_nombre")) or "paciente"
        id_cita       = cita["id_cita"]
        _motivos = []
        if _aplica_peak:        _motivos.append("peak")
        if _aplica_noshow:      _motivos.append("noshow")
        if _aplica_ticket_alto: _motivos.append("ticket_alto")
        motivo_log = "+".join(_motivos) if _motivos else "ticket_alto"

        try:
            if send_interactive_fn:
                body_txt = (
                    f"Hola {nombre_pac} te recordamos tu cita con anticipación:\n\n"
                    f"*{esp}* — {prof}\n"
                    f"*{fecha_display}* a las *{hora}*\n"
                    "Monsalve esquina República, Carampangue\n\n"
                    "¿Puedes confirmar tu asistencia?"
                )
                await send_interactive_fn(phone, {
                    "type": "button",
                    "body": {"text": body_txt},
                    "action": {
                        "buttons": [
                            {"type": "reply", "reply": {
                                "id": f"cita_confirm:{id_cita}", "title": "Confirmo"}},
                            {"type": "reply", "reply": {
                                "id": f"cita_reagendar:{id_cita}", "title": "Cambiar hora"}},
                            {"type": "reply", "reply": {
                                "id": f"cita_cancelar:{id_cita}", "title": "No podre ir"}},
                        ]
                    },
                })
            else:
                await send_text_fn(
                    phone,
                    f"Hola {nombre_pac}, te recordamos tu cita:\n\n"
                    f"*{esp}* — {prof}\n"
                    f"*{fecha_display}* a las *{hora}*\n"
                    "Monsalve esquina República, Carampangue\n\n"
                    "Por favor confirma tu asistencia respondiendo *SÍ* o *NO*."
                )

            log_message(phone, "out",
                        f"[Recordatorio 48h] {esp} con {prof} — {fecha_display} a las {hora}",
                        "IDLE")
            mark_reminder_48h_sent(cita["id"])
            log_event(phone, "recordatorio_48h_enviado", {
                "id_cita": id_cita, "motivo": motivo_log, "hora": hora,
            })
            enviados += 1
            log.info("Recordatorio 48h enviado → %s cita_id=%s motivo=%s",
                     phone, id_cita, motivo_log)
        except Exception as e:
            log.error("Error recordatorio 48h cita_id=%s: %s", cita.get("id"), e)

    if enviados:
        log.info("Recordatorios 48h: %d enviados", enviados)


# ── Recordatorios para citas agendadas por recepción (piloto Márquez id=13) ──

def _guard_recepcion(fn_name: str) -> bool:
    if not RECORDATORIOS_RECEPCION_ENABLED:
        log.debug("%s: RECORDATORIOS_RECEPCION_ENABLED=false → skip", fn_name)
        return False
    return True


async def enviar_recordatorios_recepcion_24h(
    send_text_fn,
    send_interactive_fn=None,
    send_template_fn=None,
) -> int:
    """Recordatorio 24h para citas de recepción. Gate: RECORDATORIOS_RECEPCION_ENABLED."""
    if not _guard_recepcion("enviar_recordatorios_recepcion_24h"):
        return 0

    manana = (datetime.now(_TZ_CL).date() + timedelta(days=1)).isoformat()
    citas = get_citas_recepcion_pendientes_24h(manana)
    if not citas:
        log.info("Recordatorios recepción 24h: sin citas para %s", manana)
        return 0

    enviados = 0
    for cita in citas:
        phone   = cita.get("phone")
        if not phone:
            continue
        id_cita = cita["id_cita_medilink"]
        estado = await _estado_pre_envio(id_cita, cita.get("id_profesional"), cita["fecha"])
        await asyncio.sleep(0.7)  # throttle Medilink (evita 429-tormenta)
        if estado is None:
            log.warning("Recepción 24h: pre-validación id=%s falló/no encontrada — skip (sin marcar, reintenta)", id_cita)
            continue
        if estado["anulada"]:
            log.info("Recepción 24h: cita %s anulada en Medilink, skip", id_cita)
            mark_recepcion_reminder_24h_sent(cita["id"])
            continue
        if estado["confirmada"]:
            log.info("Recepción 24h: cita %s CONFIRMADA por recepción, skip (queda 2h)", id_cita)
            log_event(phone, "recordatorio_skip_confirmada", {
                "id_cita": id_cita, "riel": "recepcion", "ventana": "24h",
            })
            mark_recepcion_reminder_24h_sent(cita["id"])
            continue

        nombre = _nombre_corto(cita.get("paciente_nombre")) or "paciente"
        esp    = cita.get("especialidad", "")
        prof   = cita.get("profesional", "")
        fecha_display = _fmt_fecha_display(cita["fecha"])
        hora   = _fmt_hora(cita["hora"])

        try:
            last_in = get_last_inbound_ts(phone)
            _now_utc = datetime.now(timezone.utc)
            _window_open = (
                last_in is not None
                and (_now_utc - last_in).total_seconds() < 86400
            )
            if _window_open and send_interactive_fn:
                _rec = {
                    "id_cita": id_cita, "especialidad": esp, "profesional": prof,
                    "fecha": cita["fecha"], "hora": cita["hora"], "modalidad": "particular",
                    "paciente_nombre": nombre, "phone_owner": None, "es_tercero": 0,
                }
                await send_interactive_fn(phone, _interactive_recordatorio(_rec))
                log.info("Recepción 24h (service_window) → %s id_cita=%s", phone[:8] + "***", id_cita)
            elif USE_TEMPLATES and send_template_fn:
                _wamid_r24 = await send_template_fn(
                    phone, "recordatorio_cita",
                    body_params=[nombre, esp, prof, fecha_display, hora, "Particular"],
                    button_payloads=[f"cita_confirm:{id_cita}",
                                     f"cita_reagendar:{id_cita}",
                                     f"cita_cancelar:{id_cita}"],
                )
                if _wamid_r24 is None:
                    log.error("Recepción 24h (template) FALLÓ (sin wamid) → %s id_cita=%s; "
                              "NO se marca reminder_24h_sent", phone[:8] + "***", id_cita)
                    log_event(phone, "template_fallido", {
                        "template": "recordatorio_cita", "id_cita": id_cita, "origen": "recepcion",
                    })
                    continue
                log_event(phone, "template_enviado", {
                    "template": "recordatorio_cita", "id_cita": id_cita, "origen": "recepcion",
                })
                log.info("Recepción 24h (template) → %s id_cita=%s", phone[:8] + "***", id_cita)
            elif send_interactive_fn:
                _rec = {
                    "id_cita": id_cita, "especialidad": esp, "profesional": prof,
                    "fecha": cita["fecha"], "hora": cita["hora"], "modalidad": "particular",
                    "paciente_nombre": nombre, "phone_owner": None, "es_tercero": 0,
                }
                await send_interactive_fn(phone, _interactive_recordatorio(_rec))
                log.info("Recepción 24h (interactive) → %s id_cita=%s", phone[:8] + "***", id_cita)
            else:
                await send_text_fn(
                    phone,
                    f"Hola {nombre} te recordamos tu cita en el *Centro Médico Carampangue*:\n\n"
                    f"*{esp}* — {prof}\n"
                    f"*{fecha_display}* a las *{hora}*\n"
                    "📍 Monsalve esquina República, Carampangue\n\n"
                    "Recuerda llegar 15 minutos antes con tu cédula de identidad.\n\n"
                    "¿Confirmas tu asistencia? Responde *SÍ* o *NO*.",
                )
                log.info("Recepción 24h (texto) → %s id_cita=%s", phone[:8] + "***", id_cita)

            log_message(phone, "out",
                        f"[Recordatorio recepción 24h] {esp} con {prof} — {fecha_display} a las {hora}",
                        "IDLE")
            mark_recepcion_reminder_24h_sent(cita["id"])
            log_event(phone, "recordatorio_recepcion_24h_enviado", {
                "id_cita": id_cita, "id_profesional": cita["id_profesional"],
            })
            enviados += 1
        except Exception as e:
            log.error("Error recordatorio recepción 24h id_cita=%s: %s", id_cita, e)

    log.info("Recordatorios recepción 24h: %d enviados para %s", enviados, manana)
    return enviados


async def enviar_recordatorios_recepcion_2h(send_text_fn, send_template_fn=None) -> int:
    """Recordatorio 2h para citas de recepción. Gate: RECORDATORIOS_RECEPCION_ENABLED."""
    if not _guard_recepcion("enviar_recordatorios_recepcion_2h"):
        return 0

    now_cl    = datetime.now(_TZ_CL)
    fecha_hoy = now_cl.date().isoformat()
    # Ventana "2h antes" real: [1h45, 2h15] desde ahora — espejo de
    # enviar_recordatorios_2h (versión bot). Antes era [ahora-15, ahora+15]
    # y el recordatorio llegaba a la hora exacta de la cita (bug F006).
    hora_min_dt = now_cl + timedelta(minutes=105)
    hora_max_dt = now_cl + timedelta(minutes=135)
    # Solo el mismo día (si la ventana cruza medianoche salteamos — raro en CMC)
    if hora_min_dt.date() != now_cl.date() or hora_max_dt.date() != now_cl.date():
        return 0
    # OJO: hora en citas_recepcion_reminders es "HH:MM" (hora_inicio[:5]),
    # NO "HH:MM:SS" como en citas_bot — mantener %H:%M.
    hora_min = hora_min_dt.strftime("%H:%M")
    hora_max = hora_max_dt.strftime("%H:%M")

    citas = get_citas_recepcion_pendientes_2h(fecha_hoy, hora_min, hora_max)
    if not citas:
        return 0

    enviados = 0
    for cita in citas:
        phone   = cita.get("phone")
        if not phone:
            continue
        id_cita = cita["id_cita_medilink"]
        nombre  = _nombre_corto(cita.get("paciente_nombre")) or "paciente"
        esp     = cita.get("especialidad", "")
        prof    = cita.get("profesional", "")
        hora    = _fmt_hora(cita["hora"])

        # Pre-envío: si la recepción anuló la cita durante el día, no se manda
        # (a diferencia del 24h/48h, aquí NO se saltea por "confirmada" — el
        # recordatorio de 2h siempre se envía si la cita sigue vigente).
        estado = await _estado_pre_envio(id_cita, cita.get("id_profesional"), cita["fecha"])
        await asyncio.sleep(0.7)  # throttle Medilink (evita 429-tormenta)
        if estado is None:
            log.warning("Recepción 2h: pre-validación id=%s falló/no encontrada — skip envío", id_cita)
            continue
        if estado["anulada"]:
            log.info("Recepción 2h: cita %s anulada en Medilink, skip", id_cita)
            log_event(phone, "recordatorio_skip_anulada", {
                "id_cita": id_cita, "riel": "recepcion", "ventana": "2h",
            })
            continue

        try:
            last_in = get_last_inbound_ts(phone)
            _now_utc = datetime.now(timezone.utc)
            _window_open = (
                last_in is not None and (_now_utc - last_in).total_seconds() < 86400
            )
            if _window_open:
                await send_text_fn(
                    phone,
                    f"Recuerda {nombre}, tienes una cita *hoy a las {hora}* con "
                    f"*{prof}* ({esp}) en el Centro Médico Carampangue.\n"
                    "📍 Monsalve esquina República. ¡Te esperamos!",
                )
            elif USE_TEMPLATES and send_template_fn:
                # FIX F041: template recordatorio_cita_2h tiene 4 placeholders
                # {{1}}=nombre {{2}}=especialidad {{3}}=profesional {{4}}=hora
                # (fecha_display era el 5to parámetro que causaba error Meta 132000)
                _wamid_r2h = await send_template_fn(phone, "recordatorio_cita_2h",
                                       body_params=[nombre, esp, prof, hora])
                if _wamid_r2h is None:
                    log.error("Recepción 2h (template) FALLÓ (sin wamid) → %s id_cita=%s; "
                              "NO se marca reminder_2h_sent", phone[:8] + "***", id_cita)
                    log_event(phone, "template_fallido", {
                        "template": "recordatorio_cita_2h", "id_cita": id_cita, "origen": "recepcion",
                    })
                    continue
                log_event(phone, "template_enviado", {
                    "template": "recordatorio_cita_2h", "id_cita": id_cita, "origen": "recepcion",
                })
            else:
                await send_text_fn(
                    phone,
                    f"Recuerda {nombre}, tienes una cita *hoy a las {hora}* con "
                    f"*{prof}* ({esp}) en el Centro Médico Carampangue.\n"
                    "📍 Monsalve esquina República. ¡Te esperamos!",
                )

            log_message(phone, "out",
                        f"[Recordatorio recepción 2h] {esp} — hoy a las {hora}", "IDLE")
            mark_recepcion_reminder_2h_sent(cita["id"])
            log_event(phone, "recordatorio_recepcion_2h_enviado", {
                "id_cita": id_cita, "id_profesional": cita["id_profesional"],
            })
            enviados += 1
            log.info("Recepción 2h enviado → %s id_cita=%s", phone[:8] + "***", id_cita)
        except Exception as e:
            log.error("Error recordatorio recepción 2h id_cita=%s: %s", id_cita, e)

    if enviados:
        log.info("Recordatorios recepción 2h: %d enviados", enviados)
    return enviados


async def enviar_recordatorios_recepcion_48h(send_text_fn, send_interactive_fn=None) -> int:
    """Recordatorio 48h para citas de recepción. Gate: RECORDATORIOS_RECEPCION_ENABLED."""
    if not _guard_recepcion("enviar_recordatorios_recepcion_48h"):
        return 0

    pasado_manana = (datetime.now(_TZ_CL).date() + timedelta(days=2)).isoformat()
    citas = get_citas_recepcion_pendientes_48h(pasado_manana)
    if not citas:
        return 0

    enviados = 0
    for cita in citas:
        phone   = cita.get("phone")
        if not phone:
            continue
        id_cita = cita["id_cita_medilink"]
        nombre  = _nombre_corto(cita.get("paciente_nombre")) or "paciente"
        esp     = cita.get("especialidad", "")
        prof    = cita.get("profesional", "")
        hora    = _fmt_hora(cita["hora"])
        fecha_display = _fmt_fecha_display(cita["fecha"])

        # Pre-envío: ANULADA → no se manda nada; CONFIRMADA por recepción →
        # no se manda el 48h (queda el de 2h más adelante).
        estado = await _estado_pre_envio(id_cita, cita.get("id_profesional"), cita["fecha"])
        await asyncio.sleep(0.7)  # throttle Medilink (evita 429-tormenta)
        if estado is None:
            log.warning("Recepción 48h: pre-validación id=%s falló/no encontrada — skip (sin marcar, reintenta)", id_cita)
            continue
        if estado["anulada"]:
            log.info("Recepción 48h: cita %s anulada en Medilink, skip", id_cita)
            mark_recepcion_reminder_48h_sent(cita["id"])
            continue
        if estado["confirmada"]:
            log.info("Recepción 48h: cita %s CONFIRMADA por recepción, skip", id_cita)
            log_event(phone, "recordatorio_skip_confirmada", {
                "id_cita": id_cita, "riel": "recepcion", "ventana": "48h",
            })
            mark_recepcion_reminder_48h_sent(cita["id"])
            continue

        # Piloto Márquez: enviamos 48h a todos (el Dr. quiere cobertura máxima)
        try:
            if send_interactive_fn:
                body_txt = (
                    f"Hola {nombre}, te recordamos con anticipación tu cita:\n\n"
                    f"*{esp}* — {prof}\n"
                    f"*{fecha_display}* a las *{hora}*\n"
                    "Monsalve esquina República, Carampangue\n\n"
                    "¿Puedes confirmar tu asistencia?"
                )
                await send_interactive_fn(phone, {
                    "type": "button",
                    "body": {"text": body_txt},
                    "action": {"buttons": [
                        {"type": "reply", "reply": {"id": f"cita_confirm:{id_cita}", "title": "Confirmo"}},
                        {"type": "reply", "reply": {"id": f"cita_reagendar:{id_cita}", "title": "Cambiar hora"}},
                        {"type": "reply", "reply": {"id": f"cita_cancelar:{id_cita}", "title": "No podre ir"}},
                    ]},
                })
            else:
                await send_text_fn(
                    phone,
                    f"Hola {nombre}, te recordamos tu próxima cita:\n\n"
                    f"*{esp}* — {prof}\n"
                    f"*{fecha_display}* a las *{hora}*\n"
                    "Monsalve esquina República, Carampangue\n\n"
                    "Por favor confirma tu asistencia respondiendo *SÍ* o *NO*.",
                )

            log_message(phone, "out",
                        f"[Recordatorio recepción 48h] {esp} con {prof} — {fecha_display} a las {hora}",
                        "IDLE")
            mark_recepcion_reminder_48h_sent(cita["id"])
            log_event(phone, "recordatorio_recepcion_48h_enviado", {
                "id_cita": id_cita, "id_profesional": cita["id_profesional"],
            })
            enviados += 1
            log.info("Recepción 48h enviado → %s id_cita=%s", phone[:8] + "***", id_cita)
        except Exception as e:
            log.error("Error recordatorio recepción 48h id_cita=%s: %s", id_cita, e)

    if enviados:
        log.info("Recordatorios recepción 48h: %d enviados", enviados)
    return enviados
