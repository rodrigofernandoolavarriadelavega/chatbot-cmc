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
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

_TZ_CHILE = ZoneInfo("America/Santiago")

from config import USE_TEMPLATES
from session import (get_citas_para_seguimiento, get_pacientes_inactivos,
                     save_fidelizacion_msg, puede_enviar_campana,
                     get_kine_candidatos_adherencia, get_control_candidatos,
                     get_crosssell_kine_candidatos, get_profile, log_message,
                     get_cumpleanos_hoy, get_pacientes_winback, get_tags,
                     save_campana_envio, puede_enviar_campana_estacional,
                     get_crosssell_orl_fono_candidatos,
                     get_crosssell_odonto_estetica_candidatos,
                     get_crosssell_mg_chequeo_candidatos)
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
    """Mensaje interactivo con escala 1-5 — pide valoración de la experiencia
    siendo atendido en CMC (más amplio que solo 'cómo te sientes').
    Ofrece NPS-style scoring: detractores (1-2), neutros (3), promotores (4-5).
    """
    nombre = _nombre_corto(cita.get("nombre"))
    saludo = f"Hola *{nombre}* 😊 " if nombre else "Hola 😊 "
    prof = cita.get("profesional", "el profesional")
    esp = cita.get("especialidad", "tu consulta")
    body = (
        f"{saludo}*¿Cómo te sentiste siendo atendido/a en el Centro Médico Carampangue?*\n\n"
        f"Hoy fuiste por *{esp}* con *{prof}*. Tu opinión es muy importante para mejorar 🙏\n\n"
        "_Califica de 1 a 5._"
    )
    return {
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": body},
            "action": {
                "button": "Calificar",
                "sections": [{
                    "title": "Tu calificación",
                    "rows": [
                        {"id": "seg_5", "title": "5 ⭐⭐⭐⭐⭐", "description": "Excelente"},
                        {"id": "seg_4", "title": "4 ⭐⭐⭐⭐",   "description": "Muy buena"},
                        {"id": "seg_3", "title": "3 ⭐⭐⭐",     "description": "Regular"},
                        {"id": "seg_2", "title": "2 ⭐⭐",       "description": "Mala"},
                        {"id": "seg_1", "title": "1 ⭐",         "description": "Muy mala"},
                    ],
                }],
            },
        },
    }


def _msg_reactivacion(paciente: dict) -> dict:
    """Mensaje interactivo para reactivar un paciente inactivo — tono prescriptivo."""
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
                    f"{saludo}Del *Centro Médico Carampangue*.\n\n"
                    f"Para mantener tu tratamiento{esp_txt} al día, es importante "
                    "no dejar pasar mucho tiempo entre controles.\n\n"
                    "Tenemos horas disponibles esta semana. ¿Te reservo una?"
                )
            },
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": "reac_si",    "title": "📅 Sí, reservar"}},
                    {"type": "reply", "reply": {"id": "reac_luego", "title": "Más adelante"}},
                ]
            }
        }
    }


async def enviar_seguimiento_postconsulta(send_fn, send_template_fn=None,
                                          send_text_fn=None, buscar_paciente_fn=None):
    """
    Ejecutar diariamente a las 22:00 CLT — mismo día de la consulta, dentro de la
    ventana 24h de WhatsApp, lo que permite mandar lista interactiva 1-5 estrellas
    sin depender de un template Meta (que solo soporta 3 botones).
    send_fn: envía mensaje interactivo (dict con botones)
    send_text_fn: envía mensaje de texto plano (para tips)
    buscar_paciente_fn: async fn(rut) → dict con fecha_nacimiento, sexo, etc.
    """
    # FIX 2026-05-02: usar fecha en zona horaria CLT, no UTC del servidor.
    # A las 22:00 CLT = 02:00 UTC siguiente día → date.today() del servidor devuelve
    # la fecha de mañana → mandaba postconsulta de citas que aún NO han ocurrido.
    # Caso real: David Valenzuela reservó 1-may para 2-may 11:15; el bot le mandó
    # "¿cómo te fue?" el 1-may a las 22:00, antes de la atención.
    ahora_clt = datetime.now(_TZ_CHILE)
    hoy = ahora_clt.date().isoformat()
    hora_actual = ahora_clt.strftime("%H:%M")
    citas = get_citas_para_seguimiento(hoy, hora_actual)

    if not citas:
        log.info("Post-consulta: sin citas de %s ya ocurridas (corte %s) para seguimiento",
                 hoy, hora_actual)
        return

    log.info("Post-consulta: enviando %d seguimiento(s) de %s", len(citas), hoy)
    for cita in citas:
        try:
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
                # Guardar tips en session para enviar cuando paciente
                # responda algun boton (seg_mejor/igual/peor) — ahi la
                # ventana 24h esta abierta. Antes se enviaba inmediato
                # tras el template y fallaba con 131047 (ventana cerrada).
                try:
                    from session import get_session, save_session
                    _sess = get_session(cita["phone"])
                    _data = _sess.get("data", {})
                    _data["pending_tips"] = tips
                    save_session(cita["phone"], _sess.get("state", "IDLE"), _data)
                except Exception as _e_tips:
                    log.warning("No se pudo guardar pending_tips: %s", _e_tips)

            save_fidelizacion_msg(cita["phone"], "postconsulta", str(cita.get("id_cita", "")))
            log.info("Seguimiento enviado → %s (%s)", cita["phone"], cita.get("especialidad"))
            # ── Meta CAPI: evento Purchase — cita ocurrida (proxy más confiable) ─
            # El post-consulta se manda solo cuando la cita ya pasó (filtro hora_actual).
            # Es el momento más cercano a "compra confirmada" sin integración de caja.
            try:
                import asyncio as _asyncio_fidel
                import meta_capi as _mc_purch
                from session import get_profile as _gp_fidel
                _prof_fidel = _gp_fidel(cita["phone"]) or {}
                _nom_fidel = (cita.get("nombre") or _prof_fidel.get("nombre") or "").split()
                _asyncio_fidel.create_task(_mc_purch.send_event(
                    "Purchase",
                    phone=cita["phone"],
                    rut=_prof_fidel.get("rut") or None,
                    first_name=_nom_fidel[0] if _nom_fidel else None,
                    last_name=_nom_fidel[-1] if len(_nom_fidel) > 1 else None,
                    custom_data={
                        "content_name": cita.get("especialidad") or "",
                        "content_category": "medical_appointment",
                        "currency": "CLP",
                    },
                ))
            except Exception as _capi_purch_err:
                log.debug("CAPI Purchase create_task falló: %s", _capi_purch_err)
            # ── fin CAPI Purchase ────────────────────────────────────────────
        except Exception as e:
            log.error("Error seguimiento phone=%s: %s", cita.get("phone"), e)


async def enviar_reactivacion_pacientes(send_fn, send_template_fn=None):
    """
    Ejecutar semanalmente (lunes 10:30 AM).
    Envía mensaje de reactivación a pacientes inactivos 60–120 días.
    Umbral subido 30→60 días para evitar "mosca en la sopa" con pacientes recientes
    y reducir volumen de templates ~40%.
    """
    pacientes = get_pacientes_inactivos(dias_min=60, dias_max=120)

    if not pacientes:
        log.info("Reactivación: sin pacientes inactivos en rango 60–120 días")
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
                    f"{saludo}Tu kinesiólogo necesita verte esta semana para "
                    "que el tratamiento avance bien.\n\n"
                    "Si dejas pasar más días, se pierde el progreso de las sesiones anteriores.\n\n"
                    "¿Agendamos tu próxima sesión?"
                )
            },
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": "kine_adh_si",  "title": "📅 Agendar ahora"}},
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
                    f"{saludo}Tu control de *{especialidad}* ya corresponde hacerlo.\n\n"
                    "Detectar cambios a tiempo puede evitar complicaciones.\n\n"
                    "Tenemos horas disponibles. ¿Te reservo una?"
                )
            },
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": "ctrl_si", "title": "📅 Reservar"}},
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
                    f"{saludo}Para que tu tratamiento funcione, "
                    "necesitas complementar con *kinesiología*.\n\n"
                    "Es lo que más ayuda a la recuperación. "
                    "Tenemos horas esta semana. ¿Te reservo?"
                )
            },
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": "xkine_si", "title": "📅 Reservar kine"}},
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
        if not puede_enviar_campana(p.get("phone",""), "crosssell_kine", dias_cooldown=90):
            continue
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
# 5b. Cross-sell ORL ↔ Fonoaudiología
# ─────────────────────────────────────────────────────────────────────────────

def _msg_crosssell_orl_fono(p: dict) -> dict:
    nombre = _nombre_corto(p.get("nombre"))
    saludo = f"Hola *{nombre}* 😊 " if nombre else "Hola 😊 "
    destino = p.get("destino", "Fonoaudiología")
    if destino == "Fonoaudiología":
        cuerpo = (
            f"{saludo}Después de tu consulta con el otorrino, una evaluación de "
            f"*fonoaudiología* suele complementar el tratamiento — especialmente "
            f"para audición, lenguaje o problemas de voz.\n\n"
            f"¿Te agendo una hora?"
        )
    else:
        cuerpo = (
            f"{saludo}Además de la fonoaudiología, muchos pacientes se benefician "
            f"de una evaluación con *otorrino* (oído, nariz, garganta) para un "
            f"diagnóstico más completo.\n\n¿Te interesa agendar?"
        )
    return {
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": cuerpo},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": "xorlfono_si", "title": "📅 Sí, agendar"}},
                    {"type": "reply", "reply": {"id": "xorlfono_no", "title": "No por ahora"}},
                ]
            }
        }
    }


async def enviar_crosssell_orl_fono(send_fn, send_template_fn=None):
    """Cross-sell bidireccional ORL ↔ Fono. Cron semanal (jueves 11:00)."""
    candidatos = get_crosssell_orl_fono_candidatos()
    if not candidatos:
        log.info("Cross-sell ORL↔Fono: sin candidatos")
        return
    log.info("Cross-sell ORL↔Fono: enviando %d mensaje(s)", len(candidatos))
    for p in candidatos:
        if not puede_enviar_campana(p.get("phone",""), "crosssell_orl_fono", dias_cooldown=90):
            continue
        try:
            msg = _msg_crosssell_orl_fono(p)
            await send_fn(p["phone"], msg)
            body = msg.get("interactive", {}).get("body", {}).get("text", "[Cross-sell ORL↔Fono]")
            log_message(p["phone"], "out", body, "IDLE")
            origen = (p.get("origen") or "").lower()
            tipo = "crosssell_orl_fono" if "otorrin" in origen else "crosssell_fono_orl"
            save_fidelizacion_msg(p["phone"], tipo)
        except Exception as e:
            log.error("Error cross-sell ORL↔Fono phone=%s: %s", p.get("phone"), e)


# ─────────────────────────────────────────────────────────────────────────────
# 5c. Cross-sell Odontología → Estética Facial
# ─────────────────────────────────────────────────────────────────────────────

def _msg_crosssell_odonto_estetica(p: dict) -> dict:
    nombre = _nombre_corto(p.get("nombre"))
    saludo = f"Hola *{nombre}* 😊 " if nombre else "Hola 😊 "
    return {
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {
                "text": (
                    f"{saludo}Vimos que has venido a tus controles dentales. "
                    f"En el CMC también hacemos *estética facial* con la Dra. "
                    f"Valentina Fuentealba: toxina botulínica, bioestimuladores, "
                    f"hilos y limpiezas faciales.\n\n¿Te interesa evaluar?"
                )
            },
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": "xestetica_si", "title": "📅 Ver horas"}},
                    {"type": "reply", "reply": {"id": "xestetica_info", "title": "💬 Más info"}},
                    {"type": "reply", "reply": {"id": "xestetica_no", "title": "No por ahora"}},
                ]
            }
        }
    }


async def enviar_crosssell_odonto_estetica(send_fn, send_template_fn=None):
    """Cross-sell odontología frecuente → estética facial. Cron bi-semanal."""
    candidatos = get_crosssell_odonto_estetica_candidatos()
    if not candidatos:
        log.info("Cross-sell Odonto→Estética: sin candidatos")
        return
    log.info("Cross-sell Odonto→Estética: enviando %d mensaje(s)", len(candidatos))
    for p in candidatos:
        if not puede_enviar_campana(p.get("phone",""), "crosssell_odonto_estetica", dias_cooldown=90):
            continue
        try:
            msg = _msg_crosssell_odonto_estetica(p)
            await send_fn(p["phone"], msg)
            body = msg.get("interactive", {}).get("body", {}).get("text", "[Cross-sell Odonto→Estética]")
            log_message(p["phone"], "out", body, "IDLE")
            save_fidelizacion_msg(p["phone"], "crosssell_odonto_estetica")
        except Exception as e:
            log.error("Error cross-sell odonto-estetica phone=%s: %s", p.get("phone"), e)


# ─────────────────────────────────────────────────────────────────────────────
# 5d. Cross-sell Medicina General → Chequeo preventivo
# ─────────────────────────────────────────────────────────────────────────────

def _msg_crosssell_mg_chequeo(p: dict) -> dict:
    nombre = _nombre_corto(p.get("nombre"))
    saludo = f"Hola *{nombre}* 😊 " if nombre else "Hola 😊 "
    # Si es >=40 ajustamos el mensaje a chequeo preventivo con EMPAM
    edad = None
    try:
        fn = p.get("fecha_nacimiento")
        if fn:
            edad = _calcular_edad(fn)
    except Exception:
        pass
    if edad and edad >= 40:
        texto = (
            f"{saludo}Pasaron unos meses desde tu consulta. "
            f"A partir de los 40, se recomienda un *chequeo preventivo anual*: "
            f"presión, glicemia, colesterol y EMPAM (Fonasa).\n\n"
            f"¿Te agendo una hora de control?"
        )
    else:
        texto = (
            f"{saludo}Pasaron unos meses desde tu última consulta. "
            f"Si quieres, puedes agendar un *control médico general* para "
            f"revisar cómo estás.\n\n¿Te reservo hora?"
        )
    return {
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": texto},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": "xchequeo_si", "title": "📅 Agendar control"}},
                    {"type": "reply", "reply": {"id": "xchequeo_no", "title": "No por ahora"}},
                ]
            }
        }
    }


async def enviar_crosssell_mg_chequeo(send_fn, send_template_fn=None):
    """Cross-sell paciente MG inactivo 30-180d → chequeo preventivo. Cron mensual."""
    candidatos = get_crosssell_mg_chequeo_candidatos()
    if not candidatos:
        log.info("Cross-sell MG→Chequeo: sin candidatos")
        return
    log.info("Cross-sell MG→Chequeo: enviando %d mensaje(s)", len(candidatos))
    for p in candidatos:
        if not puede_enviar_campana(p.get("phone",""), "crosssell_mg_chequeo", dias_cooldown=180):
            continue
        try:
            msg = _msg_crosssell_mg_chequeo(p)
            await send_fn(p["phone"], msg)
            body = msg.get("interactive", {}).get("body", {}).get("text", "[Cross-sell MG→Chequeo]")
            log_message(p["phone"], "out", body, "IDLE")
            save_fidelizacion_msg(p["phone"], "crosssell_mg_chequeo")
        except Exception as e:
            log.error("Error cross-sell mg-chequeo phone=%s: %s", p.get("phone"), e)


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
        if not puede_enviar_campana(p.get("phone",""), "cumpleanos", dias_cooldown=330):
            continue
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
                f"¡Feliz cumpleaños{(", " + saludo) if saludo else ""}! 🎂🎉{edad_txt}\n\n"
                "Todo el equipo del *Centro Médico Carampangue* te desea un excelente día.\n\n"
                f"Tu salud es lo más importante.{tip}\n\n"
                "¿Aprovechas de agendar tu chequeo preventivo?"
            )
            try:
                from messaging import send_whatsapp_interactive
                from flows import _btn_msg as _btn_msg_f
                _bmf = _btn_msg_f(msg, [
                    {"id": "reac_si", "title": "📅 Sí, agendar"},
                    {"id": "reac_luego", "title": "Más adelante"},
                ])
                await send_whatsapp_interactive(p["phone"], _bmf["interactive"])
            except Exception:
                await send_fn(p["phone"], msg + "\n\n_Escribe *menu* para agendar._")
            log_message(p["phone"], "out", msg, "IDLE")
            save_fidelizacion_msg(p["phone"], "cumpleanos")
            log.info("Cumpleaños enviado → %s (%s)%s", p["phone"], nombre, edad_txt)
        except Exception as e:
            log.error("Error cumpleaños phone=%s: %s", p.get("phone"), e)


# ─────────────────────────────────────────────────────────────────────────────
# 7. Win-back (pacientes >90 días sin cita)
# ─────────────────────────────────────────────────────────────────────────────

def _msg_winback(paciente: dict) -> dict:
    """Mensaje interactivo para recuperar pacientes inactivos >90 días — tono directo."""
    nombre = _nombre_corto(paciente.get("nombre"))
    saludo = f"Hola *{nombre}* 👋 " if nombre else "Hola 👋 "

    # Revisar si tiene patología crónica para personalizar
    tags = get_tags(paciente["phone"])
    dx_tags = [t.replace("dx:", "") for t in tags if t.startswith("dx:")]

    if dx_tags:
        patologia = dx_tags[0].upper()
        body = (
            f"{saludo}Del *Centro Médico Carampangue*.\n\n"
            f"Con *{patologia}*, dejar pasar más de 3 meses sin control "
            "aumenta el riesgo de complicaciones.\n\n"
            "Te recomendamos agendar tu chequeo esta semana. "
            "Tenemos horas disponibles."
        )
    else:
        body = (
            f"{saludo}Del *Centro Médico Carampangue*.\n\n"
            "Tu última visita fue hace más de 3 meses. "
            "Un chequeo preventivo puede detectar problemas a tiempo.\n\n"
            "También se recomienda *limpieza dental* cada 6 meses 🦷\n\n"
            "Tenemos horas disponibles esta semana."
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
