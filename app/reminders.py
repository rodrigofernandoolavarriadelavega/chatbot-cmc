"""
Recordatorios automáticos:
- 24h antes (mensaje interactivo con 3 botones): diario a las 9:00 CLT
- 2h antes (mensaje texto corto, fresh-in-mind): cron interval 15 min
"""
import logging
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from config import USE_TEMPLATES
from session import (
    get_citas_bot_pendientes,
    get_citas_bot_para_2h_reminder,
    get_last_inbound_ts,
    mark_reminder_sent,
    mark_reminder_2h_sent,
    log_message,
    log_event,
)
from medilink import get_cita, cita_esta_confirmada

log = logging.getLogger("bot.reminders")
_TZ_CL = ZoneInfo("America/Santiago")


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


def _nombre_corto(nombre: str | None) -> str:
    """'Sergio Carrasco Cordero' → 'Sergio'"""
    if not nombre:
        return ""
    return nombre.strip().split()[0].capitalize()


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
    # Antes de mandar cada recordatorio, verificar que la cita siga activa
    # en Medilink. Caso real 2026-05-03: Sebastian recibió recordatorio de
    # cita 54874 (Quijano lunes 4-may 10:00) que en Medilink figura
    # estado_anulacion=1 hace 20 días con otro paciente reasignado.
    # El bot no detectaba la cancelación y mandaba el recordatorio igual.
    try:
        from session import mark_cita_cancel_detected
        citas_validas = []
        for c in citas:
            id_cita = c.get("id_cita")
            if not id_cita:
                continue
            try:
                cita_ml = await get_cita(int(id_cita))
            except Exception as e:
                log.warning("Recordatorio pre-validación falló id_cita=%s: %s", id_cita, e)
                # Ante fallo de Medilink, NO mandamos (fail-safe).
                continue
            if cita_ml is None:
                log.warning("Recordatorio: cita %s no encontrada en Medilink, salto", id_cita)
                continue
            if cita_ml.get("id_estado") == 1 or cita_ml.get("estado_anulacion") == 1:
                log.warning("Recordatorio: cita %s ANULADA en Medilink, no se envía y se marca local",
                            id_cita)
                mark_cita_cancel_detected(str(id_cita))
                continue
            # Verificar que el paciente siga siendo el mismo (slot reasignado)
            id_pac_local = c.get("id_paciente_medilink")
            id_pac_ml = cita_ml.get("id_paciente")
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

    log.info("Recordatorios: enviando %d recordatorio(s) para %s", len(citas), manana)
    for cita in citas:
        try:
            if USE_TEMPLATES and send_template_fn:
                # Template: recordatorio_cita
                # body_params: [nombre, especialidad, profesional, fecha_display, hora, modalidad]
                # button_payloads: ["cita_confirm:{id}", "cita_reagendar:{id}", "cita_cancelar:{id}"]
                nombre = _nombre_corto(cita.get("paciente_nombre")) or "paciente"
                fecha_display = _fmt_fecha_display(cita["fecha"])
                hora = _fmt_hora(cita["hora"])
                modalidad = (cita.get("modalidad") or "particular").capitalize()
                id_cita = cita["id_cita"]
                await send_template_fn(
                    cita["phone"],
                    "recordatorio_cita",
                    body_params=[nombre, cita["especialidad"], cita["profesional"],
                                 fecha_display, hora, modalidad],
                    button_payloads=[f"cita_confirm:{id_cita}",
                                     f"cita_reagendar:{id_cita}",
                                     f"cita_cancelar:{id_cita}"],
                )
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
    # Si la cita está anulada o reasignada, no manda y marca local.
    try:
        from session import mark_cita_cancel_detected
        validas = []
        for c in citas:
            id_c = c.get("id_cita")
            if not id_c:
                continue
            try:
                _ml = await get_cita(int(id_c))
            except (TypeError, ValueError, Exception):
                continue
            if _ml is None:
                continue
            if _ml.get("id_estado") == 1 or _ml.get("estado_anulacion") == 1:
                log.warning("Recordatorio 2h: cita %s anulada, marca local", id_c)
                mark_cita_cancel_detected(str(id_c))
                continue
            id_pac_local = c.get("id_paciente_medilink")
            id_pac_ml = _ml.get("id_paciente")
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

    log.info("Recordatorios 2h: enviando %d recordatorio(s) ventana [%s-%s]",
             len(citas), hora_min, hora_max)
    for cita in citas:
        try:
            hora = _fmt_hora(cita["hora"])
            nombre_pac = _nombre_corto(cita.get("paciente_nombre")) or "paciente"
            nombre_own = _nombre_corto(cita.get("phone_owner"))
            es_terc = cita.get("es_tercero", 0)

            # Skip si la cita ya fue confirmada manualmente en Medilink
            # (recepcion envio confirmacion por el WA Business prepago y marco estado)
            id_cita_medilink = cita.get("id_cita")
            # Citas manuales (admin_routes) usan id tipo "manual-PHONE-TS", no int.
            _id_es_num = (
                isinstance(id_cita_medilink, int)
                or (isinstance(id_cita_medilink, str) and id_cita_medilink.isdigit())
            )
            if id_cita_medilink and _id_es_num:
                cita_md = await get_cita(int(id_cita_medilink))
                if cita_esta_confirmada(cita_md):
                    mark_reminder_2h_sent(cita["id"])
                    log.info("Recordatorio 2h omitido (ya confirmada en Medilink) → %s cita_id=%s",
                             cita["phone"], id_cita_medilink)
                    try:
                        log_event(cita["phone"], "savings:skip_reminder_2h_medilink_confirmed",
                                  {"id_cita": id_cita_medilink})
                    except Exception:
                        pass
                    continue

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
                await send_template_fn(
                    cita["phone"],
                    "recordatorio_cita_2h",
                    body_params=[nombre_pac, cita["especialidad"],
                                 cita["profesional"], hora],
                )
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

            log_message(cita["phone"], "out",
                        f"[Recordatorio 2h] {cita['especialidad']} con {cita['profesional']} — hoy a las {hora}",
                        "IDLE")
            mark_reminder_2h_sent(cita["id"])
            log.info("Recordatorio 2h enviado → %s cita_id=%s", cita["phone"], cita["id_cita"])
        except Exception as e:
            log.error("Error enviando recordatorio 2h cita_id=%s: %s", cita.get("id"), e)
