"""
Recordatorios automáticos:
- 24h antes (mensaje interactivo con 3 botones): diario a las 9:00 CLT
- 2h antes (mensaje texto corto, fresh-in-mind): cron interval 15 min
"""
import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from session import (
    get_citas_bot_pendientes,
    get_citas_bot_para_2h_reminder,
    mark_reminder_sent,
    mark_reminder_2h_sent,
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


def _interactive_recordatorio(cita: dict) -> dict:
    """Construye un mensaje interactivo con 3 botones: confirmo / cambiar hora / no podré ir.
    Los IDs de botón incluyen el id_cita Medilink para poder resolver la cita en la respuesta,
    incluso si el paciente no tiene una sesión activa."""
    fecha_display = _fmt_fecha_display(cita["fecha"])
    hora = _fmt_hora(cita["hora"])
    esp = cita["especialidad"]
    prof = cita["profesional"]
    modalidad = (cita.get("modalidad") or "particular").capitalize()
    id_cita = cita["id_cita"]
    body = (
        f"Hola 👋 Te recordamos tu cita en el *Centro Médico Carampangue*:\n\n"
        f"🏥 *{esp}* — {prof}\n"
        f"📅 *{fecha_display}* a las *{hora}*\n"
        f"💳 {modalidad}\n\n"
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


async def enviar_recordatorios(send_text_fn, send_interactive_fn=None):
    """
    Busca citas para mañana y envía recordatorio por WhatsApp.

    - send_text_fn: fallback send_whatsapp(to, body) — usado si no hay send_interactive_fn.
    - send_interactive_fn: send_whatsapp_interactive(to, interactive_dict) — si se pasa,
      el recordatorio es un mensaje con 3 botones (confirmo / cambiar hora / no podré ir).
    """
    manana = (date.today() + timedelta(days=1)).isoformat()
    citas = get_citas_bot_pendientes(manana)

    if not citas:
        log.info("Recordatorios: sin citas para %s", manana)
        return

    log.info("Recordatorios: enviando %d recordatorio(s) para %s", len(citas), manana)
    for cita in citas:
        try:
            if send_interactive_fn:
                await send_interactive_fn(cita["phone"], _interactive_recordatorio(cita))
            else:
                # Fallback texto plano
                fecha_display = _fmt_fecha_display(cita["fecha"])
                hora = _fmt_hora(cita["hora"])
                await send_text_fn(
                    cita["phone"],
                    f"Hola 👋 Te recordamos tu cita en el *Centro Médico Carampangue*:\n\n"
                    f"🏥 *{cita['especialidad']}* — {cita['profesional']}\n"
                    f"📅 *{fecha_display}* a las *{hora}*\n\n"
                    "Recuerda llegar *15 minutos antes* con tu cédula de identidad.\n\n"
                    "¿Confirmas tu asistencia? Responde *SÍ* o *NO*."
                )
            mark_reminder_sent(cita["id"])
            log.info("Recordatorio enviado → %s cita_id=%s", cita["phone"], cita["id_cita"])
        except Exception as e:
            log.error("Error enviando recordatorio cita_id=%s: %s", cita.get("id"), e)


async def enviar_recordatorios_2h(send_text_fn):
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
            await send_text_fn(
                cita["phone"],
                f"⏰ *En 2 horas* tienes tu cita en el *Centro Médico Carampangue*:\n\n"
                f"🏥 *{cita['especialidad']}* — {cita['profesional']}\n"
                f"🕐 Hoy a las *{hora}*\n"
                f"📍 Monsalve 102, frente a la antigua estación de trenes\n\n"
                "Recuerda llegar *15 minutos antes* con tu cédula de identidad."
            )
            mark_reminder_2h_sent(cita["id"])
            log.info("Recordatorio 2h enviado → %s cita_id=%s", cita["phone"], cita["id_cita"])
        except Exception as e:
            log.error("Error enviando recordatorio 2h cita_id=%s: %s", cita.get("id"), e)
