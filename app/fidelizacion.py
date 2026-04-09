"""
Flujos de fidelización:
  1. Post-consulta automática (24h después de la cita)
  2. Reactivación de pacientes inactivos (30–90 días sin volver)
  3. Adherencia kinesiología (gap de 4+ días sin sesión)
  4. Recordatorio de control por especialidad
  5. Cross-sell kinesiología (tras medicina/traumatología)
"""
import logging
from datetime import date, timedelta

from session import (get_citas_para_seguimiento, get_pacientes_inactivos,
                     save_fidelizacion_msg, puede_enviar_campana,
                     get_kine_candidatos_adherencia, get_control_candidatos,
                     get_crosssell_kine_candidatos)

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


async def enviar_seguimiento_postconsulta(send_fn):
    """
    Ejecutar diariamente a las 10:00 AM.
    Busca citas de ayer y envía seguimiento post-consulta.
    """
    ayer = (date.today() - timedelta(days=1)).isoformat()
    citas = get_citas_para_seguimiento(ayer)

    if not citas:
        log.info("Post-consulta: sin citas de %s para hacer seguimiento", ayer)
        return

    log.info("Post-consulta: enviando %d seguimiento(s) de %s", len(citas), ayer)
    for cita in citas:
        try:
            msg = _msg_postconsulta(cita)
            await send_fn(cita["phone"], msg)
            save_fidelizacion_msg(cita["phone"], "postconsulta", str(cita.get("id_cita", "")))
            log.info("Seguimiento enviado → %s (%s)", cita["phone"], cita.get("especialidad"))
        except Exception as e:
            log.error("Error seguimiento phone=%s: %s", cita.get("phone"), e)


async def enviar_reactivacion_pacientes(send_fn):
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
            msg = _msg_reactivacion(p)
            await send_fn(p["phone"], msg)
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


async def enviar_adherencia_kine(send_fn):
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
            msg = _msg_adherencia_kine(p)
            await send_fn(p["phone"], msg)
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


async def enviar_recordatorio_control(send_fn):
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
                msg = _msg_control(p, especialidad)
                await send_fn(p["phone"], msg)
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


async def enviar_crosssell_kine(send_fn):
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
            msg = _msg_crosssell_kine(p)
            await send_fn(p["phone"], msg)
            save_fidelizacion_msg(p["phone"], "crosssell_kine")
            log.info("Cross-sell kine enviado → %s (%s)", p["phone"], p.get("especialidad"))
        except Exception as e:
            log.error("Error cross-sell kine phone=%s: %s", p.get("phone"), e)
