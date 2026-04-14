"""
Alertas personales para el Dr. Olavarría:
  1. Resumen del paciente 10 min antes de cada cita
  2. Reporte de progreso a las 08:00, 12:00, 16:00 y 20:00
"""
import logging
from datetime import datetime, date
from zoneinfo import ZoneInfo

from medilink import obtener_agenda_dia

log = logging.getLogger("bot.doctor_alerts")
_CHILE_TZ = ZoneInfo("America/Santiago")

# ── Exámenes preventivos por edad/sexo (para el doctor) ─────────────────────
_PREVENTIVOS = [
    (25, 64, "F", "PAP cada 3 años"),
    (40, 54, "F", "Mamografía (evaluar FR)"),
    (50, 69, "F", "Mamografía GES cada 2 años"),
    (50, 75, "M", "PSA + TR próstata"),
    (40, 99, None, "Perfil lipídico + glicemia"),
    (45, 99, None, "Control PA"),
    (50, 99, None, "TSOH / colonoscopía"),
    (65, 99, None, "EMPAM anual"),
    (65, 99, None, "Vacuna influenza + neumococo"),
    (18, 39, None, "EMP (examen preventivo)"),
    # Pediátricos
    (0, 1,   None, "RN: BCG + Hepatitis B"),
    (0, 0,   None, "Screening neonatal (TSH, PKU)"),
    (1, 6,   None, "PNI según calendario"),
    (6, 15,  None, "PNI escolar (ver calendario)"),
]


def _get_preventivos_doctor(edad_str: str, sexo: str) -> list[str]:
    """Retorna lista de exámenes preventivos según edad/sexo."""
    if not edad_str:
        return []
    try:
        edad = int(edad_str.split()[0])
    except (ValueError, IndexError):
        return []
    sexo_upper = (sexo or "").upper()[:1]
    result = []
    for e_min, e_max, e_sexo, msg in _PREVENTIVOS:
        if e_min <= edad <= e_max:
            if e_sexo is None or e_sexo == sexo_upper:
                result.append(msg)
    return result

# Dr. Olavarría — ID 1 en Medilink
DOCTOR_PROF_ID = 1
DOCTOR_NOMBRE = "Rodrigo"

# Track de resúmenes ya enviados (por id_cita) para no repetir
_resumenes_enviados: set[int] = set()


def _ahora_cl() -> datetime:
    return datetime.now(_CHILE_TZ)


async def enviar_resumen_precita(send_fn, doctor_phone: str):
    """
    Llamar cada 5 min. Si hay un paciente con cita en los próximos 10 min,
    envía un resumen al WhatsApp del doctor.
    """
    ahora = _ahora_cl()
    fecha_hoy = ahora.strftime("%Y-%m-%d")
    hora_actual = ahora.hour * 60 + ahora.minute

    try:
        agenda = await obtener_agenda_dia(DOCTOR_PROF_ID, fecha_hoy)
    except Exception as e:
        log.error("Error obteniendo agenda para resumen pre-cita: %s", e)
        return

    for cita in agenda:
        cid = cita["id_cita"]
        if cid in _resumenes_enviados:
            continue

        # Parsear hora de la cita
        try:
            h, m = map(int, cita["hora"].split(":"))
            hora_cita = h * 60 + m
        except (ValueError, AttributeError):
            continue

        # Enviar si faltan entre 0 y 10 minutos
        diff = hora_cita - hora_actual
        if 0 <= diff <= 10:
            pac = cita["paciente"] or "Sin nombre"
            rut = cita["rut"] or "Sin RUT"
            edad = cita["edad"] or "—"
            sexo_txt = {"M": "Masculino", "F": "Femenino"}.get(cita.get("sexo", ""), "—")

            msg = (
                f"📋 *Próximo paciente — {cita['hora']}*\n\n"
                f"👤 *{pac}*\n"
                f"🪪 RUT: {rut}\n"
                f"🎂 Edad: {edad}\n"
                f"⚧ Sexo: {sexo_txt}\n"
            )

            # Exámenes preventivos por edad/sexo
            preventivos = _get_preventivos_doctor(edad, cita.get("sexo", ""))
            if preventivos:
                msg += "\n🩺 *Preventivo pendiente:*\n"
                for p in preventivos:
                    msg += f"  • {p}\n"

            _resumenes_enviados.add(cid)
            try:
                await send_fn(doctor_phone, msg)
                log.info("Resumen pre-cita enviado: cita=%d pac=%s hora=%s",
                         cid, pac, cita["hora"])
            except Exception as e:
                log.error("Error enviando resumen pre-cita: %s", e)


async def enviar_reporte_progreso(send_fn, doctor_phone: str):
    """
    Llamar a las 08:00, 12:00, 16:00 y 20:00.
    Envía cuántos pacientes tiene agendados, cuántos ya pasaron, cuántos faltan.
    """
    ahora = _ahora_cl()
    fecha_hoy = ahora.strftime("%Y-%m-%d")
    hora_actual = ahora.hour * 60 + ahora.minute

    try:
        agenda = await obtener_agenda_dia(DOCTOR_PROF_ID, fecha_hoy)
    except Exception as e:
        log.error("Error obteniendo agenda para reporte progreso: %s", e)
        return

    total = len(agenda)

    if total == 0:
        msg = (
            f"📊 *Reporte {ahora.strftime('%H:%M')}*\n\n"
            "No tienes pacientes agendados para hoy 🎉"
        )
        await send_fn(doctor_phone, msg)
        return

    vistos = 0
    pendientes = 0
    proximos = []

    for cita in agenda:
        try:
            h, m = map(int, cita["hora"].split(":"))
            hora_cita = h * 60 + m
        except (ValueError, AttributeError):
            pendientes += 1
            continue

        if hora_cita < hora_actual:
            vistos += 1
        else:
            pendientes += 1
            if len(proximos) < 3:
                pac = cita["paciente"] or "Sin nombre"
                proximos.append(f"  • {cita['hora']} — {pac}")

    msg = (
        f"📊 *Reporte {ahora.strftime('%H:%M')}*\n\n"
        f"📅 Agendados hoy: *{total}*\n"
        f"✅ Atendidos: *{vistos}*\n"
        f"⏳ Pendientes: *{pendientes}*\n"
    )

    if proximos:
        msg += f"\n*Próximos:*\n" + "\n".join(proximos)

    if pendientes == 0:
        msg += "\n\n🎉 *¡Terminaste tu agenda de hoy!*"

    try:
        await send_fn(doctor_phone, msg)
        log.info("Reporte progreso enviado: total=%d vistos=%d pendientes=%d",
                 total, vistos, pendientes)
    except Exception as e:
        log.error("Error enviando reporte progreso: %s", e)


def reset_resumenes_diarios():
    """Llamar a medianoche para limpiar el set de resúmenes enviados."""
    _resumenes_enviados.clear()
