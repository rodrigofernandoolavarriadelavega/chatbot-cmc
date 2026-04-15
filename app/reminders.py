"""
Recordatorios automáticos:
- 24h antes (mensaje interactivo con 3 botones): diario a las 9:00 CLT
- 2h antes (mensaje texto corto, fresh-in-mind): cron interval 15 min
"""
import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from config import USE_TEMPLATES
from session import (
    get_citas_bot_pendientes,
    get_citas_bot_para_2h_reminder,
    mark_reminder_sent,
    mark_reminder_2h_sent,
    log_message,
)

log = logging.getLogger("bot.reminders")
_TZ_CL = ZoneInfo("America/Santiago")


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
    citas = get_citas_bot_pendientes(manana)

    if not citas:
        log.info("Recordatorios: sin citas para %s", manana)
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

    citas = get_citas_bot_para_2h_reminder(fecha_hoy, hora_min, hora_max)
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

            if USE_TEMPLATES and send_template_fn:
                await send_template_fn(
                    cita["phone"],
                    "recordatorio_cita_2h",
                    body_params=[nombre_pac, cita["especialidad"],
                                 cita["profesional"], hora],
                )
            else:
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
