"""
Recordatorios automáticos 24h antes de la cita.
Se ejecuta diariamente a las 9:00 AM.
"""
import logging
from datetime import date, timedelta

from session import get_citas_bot_pendientes, mark_reminder_sent

log = logging.getLogger("bot.reminders")


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


def _mensaje_recordatorio(cita: dict) -> str:
    nombre_corto = (cita.get("phone") or "")  # no tenemos nombre guardado aún
    fecha_display = _fmt_fecha_display(cita["fecha"])
    hora = _fmt_hora(cita["hora"])
    esp = cita["especialidad"]
    prof = cita["profesional"]
    modalidad = (cita.get("modalidad") or "particular").capitalize()

    return (
        f"Hola 👋 Te recordamos tu cita en el *Centro Médico Carampangue*:\n\n"
        f"🏥 *{esp}* — {prof}\n"
        f"📅 *{fecha_display}* a las *{hora}*\n"
        f"💳 {modalidad}\n\n"
        "Recuerda llegar *15 minutos antes* con tu cédula de identidad.\n\n"
        "¿Confirmas tu asistencia? Responde *SÍ* o *NO*."
    )


async def enviar_recordatorios(send_fn):
    """
    Busca citas para mañana y envía recordatorio por WhatsApp.
    send_fn debe ser la función send_whatsapp(to, body) de main.py.
    """
    manana = (date.today() + timedelta(days=1)).isoformat()
    citas = get_citas_bot_pendientes(manana)

    if not citas:
        log.info("Recordatorios: sin citas para %s", manana)
        return

    log.info("Recordatorios: enviando %d recordatorio(s) para %s", len(citas), manana)
    for cita in citas:
        try:
            msg = _mensaje_recordatorio(cita)
            await send_fn(cita["phone"], msg)
            mark_reminder_sent(cita["id"])
            log.info("Recordatorio enviado → %s cita_id=%s", cita["phone"], cita["id_cita"])
        except Exception as e:
            log.error("Error enviando recordatorio cita_id=%s: %s", cita.get("id"), e)
