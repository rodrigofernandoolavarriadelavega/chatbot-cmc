"""
Flujos de fidelización:
  1. Post-consulta automática (24h después de la cita)
  2. Reactivación de pacientes inactivos (30–90 días sin volver)
  3. Adherencia kinesiología (gap de 4+ días sin sesión)
  4. Recordatorio de control por especialidad
  5. Cross-sell kinesiología (tras medicina/traumatología)
  6. Cumpleaños (saludo + chequeo preventivo)
  7. Win-back (pacientes >90 días sin cita)
"""
import logging
from datetime import date, timedelta

from config import USE_TEMPLATES
from session import (get_citas_para_seguimiento, get_pacientes_inactivos,
                     save_fidelizacion_msg, puede_enviar_campana,
                     get_kine_candidatos_adherencia, get_control_candidatos,
                     get_crosssell_kine_candidatos, get_profile, log_message,
                     get_cumpleanos_hoy, get_pacientes_winback, get_tags,
                     save_campana_envio, puede_enviar_campana_estacional)
from autocuidado import get_tips_autocuidado

log = logging.getLogger("bot.fidelizacion")

# Días de seguimiento por especialidad (para el mensaje de control futuro)
_DIAS_CONTROL = {
    "kinesiología":      3,
    "nutrición":         30,
    "psicología adulto": 30,
    "medicina general":  90,
    "medicina familiar": 90,
    "cardiología":       90,
    "ginecología":       180,
    "traumatología":     60,
}

_DIAS_CONTROL_DEFAULT = 60


def _dias_para_control(especialidad: str) -> int:
    return _DIAS_CONTROL.get(especialidad.lower(), _DIAS_CONTROL_DEFAULT)


def _nombre_corto(nombre: str | None) -> str:
    if not nombre:
        return ""
    return nombre.strip().split()[0].capitalize()


def _msg_postconsulta(cita: dict) -> dict:
    """Mensaje interactivo con 3 botones: Mejor / Igual / Peor."""
    nombre = _nombre_corto(cita.get("nombre"))
    saludo = f"Hola *{nombre}* 😊 " if nombre else "Hola 😊 "
    prof = cita.get("profesional", "el profesional")
    esp = cita.get("especialidad", "tu consulta")

    return {
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {
                "text": (
                    f"{saludo}¿Cómo te sientes después de tu consulta de *{esp}* con *{prof}*?\n\n"
                    "Tu opinión nos ayuda a mejorar 🙏"
                )
            },
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": "seg_mejor", "title": "Mejor 😊"}},
                    {"type": "reply", "reply": {"id": "seg_igual", "title": "Igual 😐"}},
                    {"type": "reply", "reply": {"id": "seg_peor",  "title": "Peor 😟"}},
                ]
            }
        }
    }


def _msg_reactivacion(paciente: dict) -> dict:
    """Mensaje interactivo para reactivar un paciente inactivo."""
    nombre = _nombre_corto(paciente.get("nombre"))
    saludo = f"Hola *{nombre}* 👋 " if nombre else "Hola 👋 "
    esp = paciente.get("especialidad", "")
    esp_txt = f" de *{esp}*" if esp else ""

    return {
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {
                "text": (
                    f"{saludo}Hace un tiempo no te vemos en el *Centro Médico Carampangue* 🏥\n\n"
                    f"¿Quieres retomar tu atención{esp_txt}? Puedo ayudarte a agendar ahora mismo."
                )
            },
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": "reac_si",    "title": "Sí, agendar"}},
                    {"type": "reply", "reply": {"id": "reac_luego", "title": "Más adelante"}},
                ]
            }
        }
    }


async def enviar_seguimiento_postconsulta(send_fn, send_template_fn=None,
                                          send_text_fn=None, buscar_paciente_fn=None):
    """
    Ejecutar diariamente a las 10:00 AM.
    Busca citas de ayer y envía seguimiento post-consulta + tips de autocuidado.
    send_fn: envía mensaje interactivo (dict con botones)
    send_text_fn: envía mensaje de texto plano (para tips)
    buscar_paciente_fn: async fn(rut) → dict con fecha_nacimiento, sexo, etc.
    """
    ayer = (date.today() - timedelta(days=1)).isoformat()
    citas = get_citas_para_seguimiento(ayer)

    if not citas:
        log.info("Post-consulta: sin citas de %s para hacer seguimiento", ayer)
        return

    log.info("Post-consulta: enviando %d seguimiento(s) de %s", len(citas), ayer)
    for cita in citas:
        try:
            if USE_TEMPLATES and send_template_fn:
                nombre = _nombre_corto(cita.get("nombre")) or "paciente"
                esp = cita.get("especialidad", "tu consulta")
                prof = cita.get("profesional", "el profesional")
                await send_template_fn(
                    cita["phone"],
                    "postconsulta_seguimiento",
                    body_params=[nombre, esp, prof],
                    button_payloads=["seg_mejor", "seg_igual", "seg_peor"],
                )
                log_message(cita["phone"], "out",
                            f"[Post-consulta] ¿Cómo te sientes después de tu consulta de {esp} con {prof}?",
                            "IDLE")
            else:
                msg = _msg_postconsulta(cita)
                await send_fn(cita["phone"], msg)
                body = msg.get("interactive", {}).get("body", {}).get("text", "[Post-consulta]")
                log_message(cita["phone"], "out", body, "IDLE")

            # Tips de autocuidado personalizados (segundo mensaje)
            if send_text_fn:
                fecha_nac = None
                sexo = None
                # Intentar obtener datos del paciente desde Medilink
                profile = get_profile(cita["phone"])
                if profile and profile.get("rut") and buscar_paciente_fn:
                    try:
                        pac = await buscar_paciente_fn(profile["rut"])
                        if pac:
                            fecha_nac = pac.get("fecha_nacimiento")
                            sexo = pac.get("sexo")
                    except Exception as e:
                        log.debug("No se pudo obtener datos Medilink para tips: %s", e)
                tips = get_tips_autocuidado(
                    fecha_nacimiento=fecha_nac,
                    sexo=sexo,
                    especialidad=cita.get("especialidad"),
                    nombre=cita.get("nombre"),
                )
                await send_text_fn(cita["phone"], tips)
                log_message(cita["phone"], "out", tips, "IDLE")

            save_fidelizacion_msg(cita["phone"], "postconsulta", str(cita.get("id_cita", "")))
            log.info("Seguimiento enviado → %s (%s)", cita["phone"], cita.get("especialidad"))
        except Exception as e:
            log.error("Error seguimiento phone=%s: %s", cita.get("phone"), e)


async def enviar_reactivacion_pacientes(send_fn, send_template_fn=None):
    """
    Ejecutar semanalmente (lunes 10:30 AM).
    Envía mensaje de reactivación a pacientes inactivos 30–90 días.
    """
    pacientes = get_pacientes_inactivos(dias_min=30, dias_max=90)

    if not pacientes:
        log.info("Reactivación: sin pacientes inactivos en rango 30–90 días")
        return

    log.info("Reactivación: enviando %d mensaje(s)", len(pacientes))
    for p in pacientes:
        try:
            if USE_TEMPLATES and send_template_fn:
                nombre = _nombre_corto(p.get("nombre")) or "paciente"
                esp = p.get("especialidad", "")
                await send_template_fn(
                    p["phone"],
                    "reactivacion_paciente",
                    body_params=[nombre, esp],
                    button_payloads=["reac_si", "reac_luego"],
                )
                log_message(p["phone"], "out",
                            f"[Reactivación] Hace un tiempo no te vemos. ¿Quieres retomar tu atención de {esp}?",
                            "IDLE")
            else:
                msg = _msg_reactivacion(p)
                await send_fn(p["phone"], msg)
                body = msg.get("interactive", {}).get("body", {}).get("text", "[Reactivación]")
                log_message(p["phone"], "out", body, "IDLE")
            save_fidelizacion_msg(p["phone"], "reactivacion")
            log.info("Reactivación enviada → %s (%s)", p["phone"], p.get("especialidad"))
        except Exception as e:
            log.error("Error reactivación phone=%s: %s", p.get("phone"), e)


# ─────────────────────────────────────────────────────────────────────────────
# 3. Adherencia kinesiología
# ─────────────────────────────────────────────────────────────────────────────

def _msg_adherencia_kine(paciente: dict) -> dict:
    nombre = _nombre_corto(paciente.get("nombre"))
    saludo = f"Hola *{nombre}* 💪 " if nombre else "Hola 💪 "
    return {
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {
                "text": (
                    f"{saludo}Para que tu tratamiento de kinesiología funcione bien, "
                    "es importante mantener continuidad en las sesiones.\n\n"
                    "¿Quieres que te ayude a agendar la próxima?"
                )
            },
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": "kine_adh_si",  "title": "Sí, agendar"}},
                    {"type": "reply", "reply": {"id": "kine_adh_no",  "title": "Más adelante"}},
                ]
            }
        }
    }


async def enviar_adherencia_kine(send_fn, send_template_fn=None):
    """
    Ejecutar diariamente a las 11:00 AM.
    Escribe a pacientes de kine que llevan 4+ días sin sesión y sin cita futura.
    """
    candidatos = get_kine_candidatos_adherencia(gap_dias=4)

    if not candidatos:
        log.info("Adherencia kine: sin candidatos hoy")
        return

    log.info("Adherencia kine: enviando %d mensaje(s)", len(candidatos))
    for p in candidatos:
        try:
            if USE_TEMPLATES and send_template_fn:
                nombre = _nombre_corto(p.get("nombre")) or "paciente"
                await send_template_fn(
                    p["phone"],
                    "adherencia_kine",
                    body_params=[nombre],
                    button_payloads=["kine_adh_si", "kine_adh_no"],
                )
                log_message(p["phone"], "out",
                            "[Adherencia kine] Es importante mantener continuidad en las sesiones. ¿Agendamos la próxima?",
                            "IDLE")
            else:
                msg = _msg_adherencia_kine(p)
                await send_fn(p["phone"], msg)
                body = msg.get("interactive", {}).get("body", {}).get("text", "[Adherencia kine]")
                log_message(p["phone"], "out", body, "IDLE")
            save_fidelizacion_msg(p["phone"], "adherencia_kine")
            log.info("Adherencia kine enviada → %s", p["phone"])
        except Exception as e:
            log.error("Error adherencia kine phone=%s: %s", p.get("phone"), e)


# ─────────────────────────────────────────────────────────────────────────────
# 4. Recordatorio de control por especialidad
# ─────────────────────────────────────────────────────────────────────────────

# Especialidades con control periódico: (nombre_en_citas_bot, dias_para_control)
_CONTROL_REGLAS = [
    ("Nutrición",        30),
    ("Psicología Adulto", 30),
    ("Cardiología",      90),
    ("Ginecología",      180),
    ("Traumatología",    60),
]


def _msg_control(paciente: dict, especialidad: str) -> dict:
    nombre = _nombre_corto(paciente.get("nombre"))
    saludo = f"Hola *{nombre}* 😊 " if nombre else "Hola 😊 "
    return {
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {
                "text": (
                    f"{saludo}Ya va correspondiendo tu control de *{especialidad}* 📅\n\n"
                    "Hacer el seguimiento a tiempo hace la diferencia. "
                    "¿Quieres ver horarios disponibles?"
                )
            },
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": "ctrl_si", "title": "Sí, ver horarios"}},
                    {"type": "reply", "reply": {"id": "ctrl_no", "title": "No por ahora"}},
                ]
            }
        }
    }


async def enviar_recordatorio_control(send_fn, send_template_fn=None):
    """
    Ejecutar diariamente a las 11:30 AM.
    Envía recordatorio de control por especialidad según sus días recomendados.
    """
    for especialidad, dias in _CONTROL_REGLAS:
        candidatos = get_control_candidatos(especialidad, dias)
        if not candidatos:
            continue
        tipo_fidel = f"control_{especialidad.lower().replace(' ', '_')}"
        log.info("Control %s: enviando %d mensaje(s)", especialidad, len(candidatos))
        for p in candidatos:
            try:
                if USE_TEMPLATES and send_template_fn:
                    nombre = _nombre_corto(p.get("nombre")) or "paciente"
                    await send_template_fn(
                        p["phone"],
                        "control_especialidad",
                        body_params=[nombre, especialidad],
                        button_payloads=["ctrl_si", "ctrl_no"],
                    )
                    log_message(p["phone"], "out",
                                f"[Control {especialidad}] Ya corresponde tu control. ¿Quieres ver horarios?",
                                "IDLE")
                else:
                    msg = _msg_control(p, especialidad)
                    await send_fn(p["phone"], msg)
                    body = msg.get("interactive", {}).get("body", {}).get("text", f"[Control {especialidad}]")
                    log_message(p["phone"], "out", body, "IDLE")
                save_fidelizacion_msg(p["phone"], tipo_fidel)
                log.info("Control %s enviado → %s", especialidad, p["phone"])
            except Exception as e:
                log.error("Error control %s phone=%s: %s", especialidad, p.get("phone"), e)


# ─────────────────────────────────────────────────────────────────────────────
# 5. Cross-sell kinesiología
# ─────────────────────────────────────────────────────────────────────────────

def _msg_crosssell_kine(paciente: dict) -> dict:
    nombre = _nombre_corto(paciente.get("nombre"))
    saludo = f"Hola *{nombre}* 😊 " if nombre else "Hola 😊 "
    return {
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {
                "text": (
                    f"{saludo}Muchas veces, tras una consulta de medicina o traumatología "
                    "se recomienda continuar con kinesiología para avanzar mejor.\n\n"
                    "¿Te gustaría agendar con nuestros kinesiólogos?"
                )
            },
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": "xkine_si", "title": "Sí, me interesa"}},
                    {"type": "reply", "reply": {"id": "xkine_no", "title": "No por ahora"}},
                ]
            }
        }
    }


async def enviar_crosssell_kine(send_fn, send_template_fn=None):
    """
    Ejecutar miércoles a las 10:30 AM.
    Sugiere kinesiología a pacientes de medicina/traumatología recientes.
    """
    candidatos = get_crosssell_kine_candidatos()

    if not candidatos:
        log.info("Cross-sell kine: sin candidatos esta semana")
        return

    log.info("Cross-sell kine: enviando %d mensaje(s)", len(candidatos))
    for p in candidatos:
        try:
            if USE_TEMPLATES and send_template_fn:
                nombre = _nombre_corto(p.get("nombre")) or "paciente"
                await send_template_fn(
                    p["phone"],
                    "crosssell_kine",
                    body_params=[nombre],
                    button_payloads=["xkine_si", "xkine_no"],
                )
                log_message(p["phone"], "out",
                            "[Cross-sell kine] Tras tu consulta, ¿te gustaría agendar con nuestros kinesiólogos?",
                            "IDLE")
            else:
                msg = _msg_crosssell_kine(p)
                await send_fn(p["phone"], msg)
                body = msg.get("interactive", {}).get("body", {}).get("text", "[Cross-sell kine]")
                log_message(p["phone"], "out", body, "IDLE")
            save_fidelizacion_msg(p["phone"], "crosssell_kine")
            log.info("Cross-sell kine enviado → %s (%s)", p["phone"], p.get("especialidad"))
        except Exception as e:
            log.error("Error cross-sell kine phone=%s: %s", p.get("phone"), e)


# ─────────────────────────────────────────────────────────────────────────────
# 6. Cumpleaños
# ─────────────────────────────────────────────────────────────────────────────

def _calcular_edad(fecha_nacimiento: str) -> int | None:
    """Calcula la edad a partir de una fecha YYYY-MM-DD."""
    try:
        parts = fecha_nacimiento.split("-")
        year, month, day = int(parts[0]), int(parts[1]), int(parts[2])
        hoy = date.today()
        return hoy.year - year - ((hoy.month, hoy.day) < (month, day))
    except (ValueError, IndexError):
        return None


async def enviar_cumpleanos(send_fn):
    """
    Ejecutar diariamente a las 08:00 AM CLT.
    Envía saludo de cumpleaños a pacientes cuya fecha coincide con hoy.
    """
    cumpleaneros = get_cumpleanos_hoy()

    if not cumpleaneros:
        log.info("Cumpleaños: nadie cumple años hoy")
        return

    log.info("Cumpleaños: enviando %d saludo(s)", len(cumpleaneros))
    for p in cumpleaneros:
        try:
            nombre = _nombre_corto(p.get("nombre"))
            saludo = f"*{nombre}*" if nombre else ""
            edad = _calcular_edad(p.get("fecha_nacimiento", ""))
            edad_txt = f" ({edad} años)" if edad else ""

            # Tip preventivo según edad
            tip = ""
            if edad and edad >= 65:
                tip = "\n\nRecuerda que a tu edad es importante el EMPAM anual y la vacuna de influenza."
            elif edad and edad >= 50:
                tip = "\n\nAproxímate para tu chequeo preventivo anual: exámenes de sangre, presión y más."
            elif edad and edad >= 40:
                tip = "\n\nEs un buen momento para agendar tu chequeo preventivo anual."

            msg = (
                f"¡Feliz cumpleaños, {saludo}! 🎂🎉{edad_txt}\n\n"
                "Todo el equipo del *Centro Médico Carampangue* te desea un excelente día.\n\n"
                f"Tu salud es lo más importante. ¿Te gustaría agendar un chequeo?{tip}\n\n"
                "_Escribe *menu* cuando quieras agendar._"
            )
            await send_fn(p["phone"], msg)
            log_message(p["phone"], "out", msg, "IDLE")
            save_fidelizacion_msg(p["phone"], "cumpleanos")
            log.info("Cumpleaños enviado → %s (%s)%s", p["phone"], nombre, edad_txt)
        except Exception as e:
            log.error("Error cumpleaños phone=%s: %s", p.get("phone"), e)


# ─────────────────────────────────────────────────────────────────────────────
# 7. Win-back (pacientes >90 días sin cita)
# ─────────────────────────────────────────────────────────────────────────────

def _msg_winback(paciente: dict) -> dict:
    """Mensaje interactivo para recuperar pacientes inactivos >90 días."""
    nombre = _nombre_corto(paciente.get("nombre"))
    saludo = f"Hola *{nombre}* 👋 " if nombre else "Hola 👋 "

    # Revisar si tiene patología crónica para personalizar
    tags = get_tags(paciente["phone"])
    dx_tags = [t.replace("dx:", "") for t in tags if t.startswith("dx:")]

    if dx_tags:
        # Mensaje personalizado para crónicos
        patologia = dx_tags[0].upper()
        body = (
            f"{saludo}Hace bastante que no vienes al *Centro Médico Carampangue*.\n\n"
            f"Como paciente con *{patologia}*, es importante mantener tus controles al día "
            "para cuidar tu salud a largo plazo.\n\n"
            "¿Te ayudo a agendar un control?"
        )
    else:
        body = (
            f"{saludo}Hace un buen tiempo que no te vemos en el *Centro Médico Carampangue*.\n\n"
            "Queremos saber cómo estás 😊 Recuerda que puedes agendar una hora "
            "por aquí mismo, a cualquier hora del día.\n\n"
            "¿Te agendamos?"
        )

    return {
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": "reac_si",    "title": "Sí, agendar"}},
                    {"type": "reply", "reply": {"id": "reac_luego", "title": "Más adelante"}},
                ]
            }
        }
    }


async def enviar_winback(send_fn):
    """
    Ejecutar mensualmente (primer lunes del mes, 10:00 AM CLT).
    Recupera pacientes con 91-365 días sin cita.
    """
    pacientes = get_pacientes_winback(dias_min=91, dias_max=365)

    if not pacientes:
        log.info("Win-back: sin pacientes inactivos >90 días")
        return

    log.info("Win-back: enviando %d mensaje(s)", len(pacientes))
    for p in pacientes:
        try:
            msg = _msg_winback(p)
            await send_fn(p["phone"], msg)
            body = msg.get("interactive", {}).get("body", {}).get("text", "[Win-back]")
            log_message(p["phone"], "out", body, "IDLE")
            save_fidelizacion_msg(p["phone"], "winback")
            log.info("Win-back enviado → %s (%s, última: %s)",
                     p["phone"], p.get("especialidad"), p.get("ultima_cita"))
        except Exception as e:
            log.error("Error win-back phone=%s: %s", p.get("phone"), e)


# ─────────────────────────────────────────────────────────────────────────────
# 8. Campañas estacionales
# ─────────────────────────────────────────────────────────────────────────────

CAMPANAS_ESTACIONALES = {
    "invierno_influenza": {
        "nombre": "Vacuna Influenza",
        "temporada": "Invierno",
        "icono": "\U0001f927",
        "meses_sugeridos": [4, 5, 6],
        "mensaje": (
            "{saludo}Se acerca la temporada de invierno y es muy importante "
            "vacunarse contra la *influenza* \U0001f489\n\n"
            "En el *Centro Medico Carampangue* contamos con stock de vacunas.\n\n"
            "Escribe *menu* y te ayudamos a agendar."
        ),
        "segmento": {},
        "descripcion": "Vacuna influenza para grupos de riesgo",
    },
    "invierno_respiratorio": {
        "nombre": "Chequeo Respiratorio",
        "temporada": "Invierno",
        "icono": "\U0001fac1",
        "meses_sugeridos": [5, 6, 7],
        "mensaje": (
            "{saludo}En invierno aumentan los cuadros respiratorios.\n\n"
            "Si tienes *tos persistente*, *dificultad para respirar* o "
            "*silbidos al pecho*, te recomendamos un control medico.\n\n"
            "Escribe *menu* para agendar tu hora."
        ),
        "segmento": {"tags": ["dx:asma", "dx:epoc"]},
        "descripcion": "Control respiratorio para pacientes cronicos en invierno",
    },
    "vuelta_clases": {
        "nombre": "Control Vuelta a Clases",
        "temporada": "Verano",
        "icono": "\U0001f392",
        "meses_sugeridos": [2, 3],
        "mensaje": (
            "{saludo}Se acerca la vuelta a clases.\n\n"
            "Es buen momento para un *control pediatrico* y asegurarse "
            "de que las vacunas esten al dia.\n\n"
            "Escribe *menu* para agendar una hora."
        ),
        "segmento": {},
        "descripcion": "Control pediatrico y vacunas previo al inicio de clases",
    },
    "mes_corazon": {
        "nombre": "Mes del Corazon",
        "temporada": "Agosto",
        "icono": "\u2764\ufe0f",
        "meses_sugeridos": [8],
        "mensaje": (
            "{saludo}*Agosto es el Mes del Corazon*\n\n"
            "Un control cardiologico puede prevenir enfermedades silenciosas.\n\n"
            "Tenemos *cardiologia* y *electrocardiogramas*.\n\n"
            "Escribe *menu* para agendar tu chequeo."
        ),
        "segmento": {"tags": ["dx:hta", "dx:dislipidemia"]},
        "descripcion": "Chequeo cardiologico preventivo",
    },
    "diabetes_noviembre": {
        "nombre": "Dia Mundial de la Diabetes",
        "temporada": "Noviembre",
        "icono": "\U0001fa78",
        "meses_sugeridos": [11],
        "mensaje": (
            "{saludo}*Noviembre es el mes de la Diabetes*\n\n"
            "Si tienes diabetes o factores de riesgo, es importante "
            "hacer tu control periodico.\n\n"
            "Escribe *menu* para agendar con nuestro equipo medico."
        ),
        "segmento": {"tags": ["dx:dm2"]},
        "descripcion": "Control para pacientes diabeticos",
    },
    "salud_mental": {
        "nombre": "Mes de la Salud Mental",
        "temporada": "Octubre",
        "icono": "\U0001f9e0",
        "meses_sugeridos": [10],
        "mensaje": (
            "{saludo}*Octubre es el Mes de la Salud Mental*\n\n"
            "Cuidar tu mente es tan importante como cuidar tu cuerpo.\n\n"
            "Contamos con *psicologia* para adultos e infantil.\n\n"
            "Escribe *menu* si quieres agendar."
        ),
        "segmento": {"tags": ["dx:depresion"]},
        "descripcion": "Atencion psicologica — mes de la salud mental",
    },
    "dental_marzo": {
        "nombre": "Mes de la Salud Dental",
        "temporada": "Marzo",
        "icono": "\U0001f9b7",
        "meses_sugeridos": [3],
        "mensaje": (
            "{saludo}*Marzo es el Mes de la Salud Dental*\n\n"
            "Lo ideal es hacerse una limpieza cada 6 meses.\n\n"
            "Tenemos *odontologia*, *ortodoncia*, *endodoncia* e "
            "*implantologia*.\n\n"
            "Escribe *menu* para agendar."
        ),
        "segmento": {},
        "descripcion": "Chequeo dental preventivo y limpiezas",
    },
    "mujer_octubre": {
        "nombre": "Mes de la Mujer (Cancer de Mama)",
        "temporada": "Octubre",
        "icono": "\U0001f49c",
        "meses_sugeridos": [10],
        "mensaje": (
            "{saludo}*Octubre es el Mes de la Prevencion del Cancer de Mama*\n\n"
            "Si tienes mas de 40 anos, es importante la mamografia anual.\n\n"
            "Agenda tu control ginecologico.\n\n"
            "Escribe *menu* para agendar."
        ),
        "segmento": {},
        "descripcion": "Prevencion cancer de mama — mamografia y control ginecologico",
    },
}


async def enviar_campana_estacional(campana_id: str, pacientes: list[dict],
                                    send_fn):
    """Envia una campana estacional a una lista de pacientes.

    pacientes: list[{phone, nombre}]
    Retorna (enviados, errores).
    """
    campana = CAMPANAS_ESTACIONALES.get(campana_id)
    if not campana:
        log.error("Campana no encontrada: %s", campana_id)
        return 0, 0

    enviados = 0
    errores = 0
    template_msg = campana["mensaje"]

    for p in pacientes:
        phone = p["phone"]
        if not puede_enviar_campana_estacional(phone, campana_id,
                                               dias_cooldown=30):
            continue
        try:
            nombre = _nombre_corto(p.get("nombre"))
            saludo = f"Hola *{nombre}* \U0001f44b " if nombre else "Hola \U0001f44b "
            msg = template_msg.format(saludo=saludo)
            await send_fn(phone, msg)
            save_campana_envio(phone, campana_id)
            log_message(phone, "out",
                        f"[Campana: {campana['nombre']}] {msg[:120]}...",
                        "IDLE")
            enviados += 1
        except Exception as e:
            log.error("Error campana %s phone=%s: %s", campana_id, phone, e)
            errores += 1

    log.info("Campana %s: enviados=%d, errores=%d, audiencia=%d",
             campana_id, enviados, errores, len(pacientes))
    return enviados, errores
