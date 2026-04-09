"""
Máquina de estados para los flujos de conversación.
Opción C: Claude detecta intención → sistema guía el flujo → Medilink ejecuta.
"""
import re
from datetime import datetime, timedelta

from claude_helper import detect_intent, respuesta_faq, clasificar_respuesta_seguimiento
from medilink import (buscar_primer_dia, buscar_slots_dia, buscar_slots_dia_por_ids,
                      buscar_paciente, crear_paciente, crear_cita,
                      listar_citas_paciente, cancelar_cita,
                      valid_rut, clean_rut, especialidades_disponibles,
                      consultar_proxima_fecha)
from session import (save_session, reset_session, save_tag, save_cita_bot, log_event,
                     save_profile, get_profile, save_fidelizacion_respuesta, get_ultimo_seguimiento)
from config import CMC_TELEFONO, CMC_TELEFONO_FIJO

# Mapa de nombres de día en español → Python weekday (0=Lun..6=Dom)
_DIAS_SEMANA = {
    "lunes": 0, "martes": 1, "miercoles": 2, "miércoles": 2,
    "jueves": 3, "viernes": 4, "sabado": 5, "sábado": 5,
}


def _proxima_fecha_dia(weekday: int) -> str:
    """Retorna la fecha (YYYY-MM-DD) del próximo día de la semana dado (hoy + 1 en adelante)."""
    hoy = datetime.now().date()
    for delta in range(1, 8):
        candidato = hoy + timedelta(days=delta)
        if candidato.weekday() == weekday:
            return candidato.strftime("%Y-%m-%d")
    return None

AFIRMACIONES = {"si", "sí", "yes", "ok", "confirmo", "confirmar", "dale", "ya", "claro", "bueno"}
NEGACIONES   = {"no", "nop", "nope", "cancelar", "cancel", "no gracias"}

EMERGENCIAS  = {"emergencia", "urgencia", "dolor muy fuerte", "no puedo respirar",
                "estoy grave", "me estoy muriendo", "perdí el conocimiento",
                "mucho dolor", "accidente", "desmayo", "convulsion", "convulsión"}

DISCLAIMER = "_Recuerda que soy un asistente virtual, no un médico. Para consultas clínicas, habla siempre con un profesional de salud._"


# ── Helpers de mensajes interactivos ──────────────────────────────────────────

def _list_msg(body_text: str, button_label: str, sections: list) -> dict:
    """Construye un mensaje de lista interactivo de WhatsApp."""
    return {
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": body_text},
            "action": {
                "button": button_label[:20],
                "sections": sections,
            }
        }
    }


def _btn_msg(body_text: str, buttons: list) -> dict:
    """Construye un mensaje con botones de respuesta (máx 3)."""
    return {
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body_text},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": b["id"], "title": b["title"][:20]}}
                    for b in buttons
                ]
            }
        }
    }


def _menu_msg() -> dict:
    return _list_msg(
        body_text=(
            "Hola 👋 Soy el asistente del *Centro Médico Carampangue*.\n\n"
            "📍 *Monsalve 102, frente a la antigua estación de trenes*, Carampangue.\n\n"
            "¿En qué te ayudo hoy?"
        ),
        button_label="Ver opciones",
        sections=[{
            "title": "¿En qué te ayudamos?",
            "rows": [
                {"id": "1", "title": "Agendar una hora"},
                {"id": "2", "title": "Cancelar una hora"},
                {"id": "3", "title": "Ver mis reservas"},
                {"id": "4", "title": "Hablar con recepción"},
            ]
        }]
    )


async def handle_message(phone: str, texto: str, session: dict) -> str:
    state = session["state"]
    data  = session["data"]
    txt   = texto.strip()
    tl    = txt.lower()

    # ── Emergencias ───────────────────────────────────────────────────────────
    if any(p in tl for p in EMERGENCIAS):
        return (
            "⚠️ Esto suena como una urgencia.\n\n"
            "Llama al *SAMU 131* o acude al servicio de urgencias más cercano ahora mismo.\n\n"
            f"También puedes contactarnos:\n📞 *{CMC_TELEFONO}*\n☎️ *{CMC_TELEFONO_FIJO}*"
        )

    # ── Comandos globales ─────────────────────────────────────────────────────
    if tl in ("menu", "menú", "inicio", "reiniciar", "volver", "hola"):
        reset_session(phone)
        return _menu_msg()

    # ── Detección pasiva de Arauco (guarda tag sin interrumpir el flujo) ──────
    if "arauco" in tl:
        save_tag(phone, "arauco")

    # ── IDLE: detectar intención ──────────────────────────────────────────────
    if state == "IDLE":
        # Atajos numéricos del menú
        if txt == "1": return await _iniciar_agendar(phone, data, None)
        if txt == "2": return await _iniciar_cancelar(phone, data)
        if txt == "3": return await _iniciar_ver(phone, data)
        if txt == "4": return _derivar_humano(phone=phone, contexto="menú opción 4")

        # ── Respuestas de fidelización ────────────────────────────────────────
        if tl == "seg_mejor":
            save_fidelizacion_respuesta(phone, "postconsulta", "mejor")
            seg = get_ultimo_seguimiento(phone)
            esp = seg.get("especialidad", "") if seg else ""
            log_event(phone, "seguimiento_mejor", {"especialidad": esp})
            return _btn_msg(
                "Qué bueno saberlo 😊 Nos alegra que te sientas mejor.\n\n"
                "¿Quieres agendar tu control de seguimiento?",
                [{"id": "1", "title": "Sí, agendar control"},
                 {"id": "no_control", "title": "Por ahora no"}]
            )
        if tl in ("seg_igual", "seg_peor"):
            save_fidelizacion_respuesta(phone, "postconsulta", tl.replace("seg_", ""))
            seg = get_ultimo_seguimiento(phone)
            esp = seg.get("especialidad", "") if seg else ""
            prof = seg.get("profesional", "") if seg else ""
            log_event(phone, "seguimiento_negativo", {"respuesta": tl, "especialidad": esp})
            return _btn_msg(
                "Lamentamos escuchar eso 😟\n\n"
                f"¿Quieres reagendar una consulta{' con ' + prof if prof else ''}?",
                [{"id": "1", "title": "Sí, reagendar"},
                 {"id": "no_control", "title": "No por ahora"}]
            )
        if tl == "no_control":
            return (
                "Entendido 😊 Cuando lo necesites, estamos acá.\n"
                "_Escribe *menu* para volver al inicio._"
            )
        if tl == "reac_si":
            log_event(phone, "reactivacion_acepto", {})
            return await _iniciar_agendar(phone, data, None)
        if tl == "reac_luego":
            log_event(phone, "reactivacion_rechazo", {})
            return (
                "Sin problema 😊 Cuando lo necesites escríbenos.\n"
                "_Escribe *menu* para ver todas las opciones._"
            )

        # ── Adherencia kinesiología ───────────────────────────────────────────
        if tl == "kine_adh_si":
            log_event(phone, "adherencia_kine_acepto", {})
            perfil = get_profile(phone)
            if perfil:
                data["rut_conocido"] = perfil["rut"]
                data["nombre_conocido"] = perfil["nombre"]
            return await _iniciar_agendar(phone, data, "kinesiología")
        if tl == "kine_adh_no":
            log_event(phone, "adherencia_kine_rechazo", {})
            return (
                "Entendido 😊 Cuando estés listo/a, escríbenos.\n"
                "_Escribe *menu* para volver al inicio._"
            )

        # ── Cross-sell kinesiología ───────────────────────────────────────────
        if tl == "xkine_si":
            log_event(phone, "crosssell_kine_acepto", {})
            perfil = get_profile(phone)
            if perfil:
                data["rut_conocido"] = perfil["rut"]
                data["nombre_conocido"] = perfil["nombre"]
            return await _iniciar_agendar(phone, data, "kinesiología")
        if tl == "xkine_no":
            log_event(phone, "crosssell_kine_rechazo", {})
            return (
                "Sin problema 😊 Cuando lo necesites, estamos acá.\n"
                "_Escribe *menu* para ver todas las opciones._"
            )

        # ── Recordatorio de control ───────────────────────────────────────────
        if tl == "ctrl_si":
            log_event(phone, "control_recordatorio_acepto", {})
            perfil = get_profile(phone)
            if perfil:
                data["rut_conocido"] = perfil["rut"]
                data["nombre_conocido"] = perfil["nombre"]
            return await _iniciar_agendar(phone, data, None)
        if tl == "ctrl_no":
            log_event(phone, "control_recordatorio_rechazo", {})
            return (
                "Entendido 😊 Cuando lo necesites, estamos acá.\n"
                "_Escribe *menu* para volver al inicio._"
            )

        # ── Respuesta libre al seguimiento post-consulta ──────────────────────
        seg_pendiente = get_ultimo_seguimiento(phone)
        if seg_pendiente:
            clasificacion = await clasificar_respuesta_seguimiento(txt)
            if clasificacion:
                esp  = seg_pendiente.get("especialidad", "")
                prof = seg_pendiente.get("profesional", "")
                save_fidelizacion_respuesta(phone, "postconsulta", clasificacion)
                if clasificacion == "mejor":
                    log_event(phone, "seguimiento_mejor", {"especialidad": esp, "fuente": "texto_libre"})
                    return _btn_msg(
                        "Qué bueno saberlo 😊 Nos alegra que te sientas mejor.\n\n"
                        "¿Quieres agendar tu control de seguimiento?",
                        [{"id": "1", "title": "Sí, agendar control"},
                         {"id": "no_control", "title": "Por ahora no"}]
                    )
                else:  # igual o peor
                    log_event(phone, "seguimiento_negativo",
                              {"respuesta": clasificacion, "especialidad": esp, "fuente": "texto_libre"})
                    return _btn_msg(
                        "Lamentamos escuchar eso 😟\n\n"
                        f"¿Quieres reagendar una consulta{' con ' + prof if prof else ''}?",
                        [{"id": "1", "title": "Sí, reagendar"},
                         {"id": "no_control", "title": "No por ahora"}]
                    )

        result = await detect_intent(txt)
        intent = result.get("intent", "otro")
        log_event(phone, "intent_detectado", {"intent": intent, "esp": result.get("especialidad")})

        if intent == "agendar":
            especialidad = result.get("especialidad")
            log_event(phone, "intent_agendar", {"especialidad": especialidad})
            # Pre-fill RUT si el paciente ya agendó antes
            perfil = get_profile(phone)
            if perfil:
                data["rut_conocido"] = perfil["rut"]
                data["nombre_conocido"] = perfil["nombre"]
            return await _iniciar_agendar(phone, data, especialidad)

        if intent == "cancelar":
            return await _iniciar_cancelar(phone, data)

        if intent == "ver_reservas":
            return await _iniciar_ver(phone, data)

        if intent == "humano":
            return _derivar_humano(phone=phone, contexto=txt)

        if intent == "disponibilidad":
            especialidad = result.get("especialidad")
            if especialidad:
                fecha = await consultar_proxima_fecha(especialidad)
                if fecha:
                    return (
                        f"Sí, para *{especialidad}* hay hora disponible el *{fecha}* 📅\n\n"
                        "¿La agendamos ahora?\n"
                        "Escribe *1* para continuar o *menu* si necesitas algo más."
                    )
            return (
                "Para consultar disponibilidad, dime qué especialidad necesitas 😊\n\n"
                f"O llama a recepción: 📞 *{CMC_TELEFONO}*"
            )

        if intent in ("precio", "info"):
            resp = result.get("respuesta_directa") or await respuesta_faq(txt)
            return (
                f"{resp}\n\n"
                f"{DISCLAIMER}\n\n"
                "¿Quieres agendar una hora? Escribe *1* o *menu* para volver."
            )

        # intent "otro" o "menu" (fallback de Claude) → mostrar menú
        return _menu_msg()

    # ── WAIT_DURACION_MASOTERAPIA ──────────────────────────────────────────────
    if state == "WAIT_DURACION_MASOTERAPIA":
        if tl in ("maso_20",) or "20" in txt:
            duracion_maso = 20
        elif tl in ("maso_40",) or "40" in txt:
            duracion_maso = 40
        else:
            save_session(phone, "WAIT_DURACION_MASOTERAPIA", data)
            return _btn_msg(
                "Por favor elige la duración de tu sesión:",
                [
                    {"id": "maso_20", "title": "20 minutos"},
                    {"id": "maso_40", "title": "40 minutos"},
                ]
            )
        data["maso_duracion"] = duracion_maso
        smart, todos = await buscar_primer_dia("masoterapia", intervalo_override={59: duracion_maso})
        if not todos:
            reset_session(phone)
            log_event(phone, "sin_disponibilidad", {"especialidad": "masoterapia"})
            save_tag(phone, "sin-disponibilidad")
            return (
                f"No encontré disponibilidad para masoterapia en los próximos días 😕\n\n"
                f"Llama a recepción:\n📞 *{CMC_TELEFONO}*\n\n"
                "_Escribe *menu* para volver._"
            )
        fecha = todos[0]["fecha"]
        data.update({"especialidad": "masoterapia", "slots": smart,
                     "todos_slots": todos, "fechas_vistas": [fecha], "expansion_stage": 0})
        save_session(phone, "WAIT_SLOT", data)
        mejor = smart[0]
        return _btn_msg(
            f"Encontré disponibilidad ✨\n\n"
            f"🏥 *Masoterapia* — {mejor['profesional']}\n"
            f"📅 *{mejor['fecha_display']}*\n"
            f"🕐 *{mejor['hora_inicio'][:5]}* ({duracion_maso} min) ⭐\n\n"
            "¿La agendo?",
            [
                {"id": "confirmar_sugerido", "title": "✅ Sí, esa hora"},
                {"id": "ver_otros",          "title": "📋 Ver más opciones"},
            ]
        )

    # ── WAIT_ESPECIALIDAD ─────────────────────────────────────────────────────
    if state == "WAIT_ESPECIALIDAD":
        # Selección de categoría (paso intermedio)
        if tl == "cat_medico":
            save_session(phone, "WAIT_ESPECIALIDAD", data)
            return _especialidades_medico_msg()
        if tl == "cat_dental":
            save_session(phone, "WAIT_ESPECIALIDAD", data)
            return _especialidades_dental_msg()

        from medilink import _ids_para_especialidad
        # Traducir ID de lista interactiva al nombre real de especialidad
        especialidad_candidata = _ESP_ID_MAP.get(tl, tl)
        if not _ids_para_especialidad(especialidad_candidata):
            result = await detect_intent(txt)
            especialidad_candidata = result.get("especialidad") or especialidad_candidata
        return await _iniciar_agendar(phone, data, especialidad_candidata)

    # ── WAIT_SLOT ─────────────────────────────────────────────────────────────
    if state == "WAIT_SLOT":
        slots_mostrados = data.get("slots", [])          # los que ve el paciente ahora
        todos_slots     = data.get("todos_slots", slots_mostrados)  # todos del día
        fechas_vistas   = data.get("fechas_vistas", [])
        especialidad    = data.get("especialidad", "")
        fecha_actual    = todos_slots[0]["fecha"] if todos_slots else None

        # Respuesta al sugerido proactivo
        if tl == "confirmar_sugerido":
            slot = slots_mostrados[0]
            data["slot_elegido"] = slot
            save_session(phone, "WAIT_MODALIDAD", data)
            return _btn_msg(
                f"Perfecto 🙌\n\n"
                f"🏥 *{slot['especialidad']}* — {slot['profesional']}\n"
                f"📅 *{slot['fecha_display']}*\n"
                f"🕐 *{slot['hora_inicio'][:5]}*\n\n"
                "¿Tu atención será Fonasa o Particular?",
                [{"id": "1", "title": "Fonasa"}, {"id": "2", "title": "Particular"}]
            )
        if tl == "ver_otros":
            if especialidad in _ESPECIALIDADES_EXPANSION:
                return await _handle_expansion(phone, data, slots_mostrados, todos_slots,
                                               data.get("expansion_stage", 0), fecha_actual)
            return _format_slots(slots_mostrados)

        # "ver todos" / "ver más" → expansión progresiva para med general, o todos del día para el resto
        VER_TODOS = {"ver todos", "todos", "ver todo", "todos los horarios", "mostrar todos",
                     "ver horarios", "quiero ver los horarios", "ver todos los horarios",
                     "mostrar horarios", "quiero ver horarios", "ver mas", "ver más", "ver_todos"}
        if tl in VER_TODOS or any(p in tl for p in ["ver todos", "todos los horarios", "ver horarios", "ver mas", "ver más"]):
            if especialidad in _ESPECIALIDADES_EXPANSION:
                return await _handle_expansion(phone, data, slots_mostrados, todos_slots,
                                               data.get("expansion_stage", 0), fecha_actual)
            data["slots"] = todos_slots
            save_session(phone, "WAIT_SLOT", data)
            return _format_slots(todos_slots, mostrar_todos=True)

        # Día específico → "para el viernes", "hay para el martes", etc.
        _maso_override = {59: data["maso_duracion"]} if especialidad == "masoterapia" and data.get("maso_duracion") else None
        dia_pedido = next((wd for nombre, wd in _DIAS_SEMANA.items() if nombre in tl), None)
        if dia_pedido is not None:
            fecha_dia = _proxima_fecha_dia(dia_pedido)
            if fecha_dia:
                smart_dia, todos_dia = await buscar_slots_dia(especialidad, fecha_dia, intervalo_override=_maso_override)
                if todos_dia:
                    if fecha_dia not in fechas_vistas:
                        fechas_vistas = fechas_vistas + [fecha_dia]
                    data.update({"slots": smart_dia, "todos_slots": todos_dia,
                                 "fechas_vistas": fechas_vistas, "expansion_stage": 1})
                    save_session(phone, "WAIT_SLOT", data)
                    return _format_slots(smart_dia)
            return "Sin horarios disponibles para ese día.\n\nEscribe *otro día* para buscar el siguiente 😊"

        # "otro día" → primeras 5 del siguiente día disponible
        OTRO_DIA = {"otro dia", "otro día", "otro", "no puedo", "no me sirve",
                    "no me acomoda", "cambiar dia", "cambiar día", "siguiente", "otro_dia"}
        if tl in OTRO_DIA or any(p in tl for p in ["otro dia", "otro día", "no puedo"]):
            smart_nuevo, todos_nuevo = await buscar_primer_dia(especialidad, excluir=fechas_vistas,
                                                               intervalo_override=_maso_override)
            if not todos_nuevo:
                reset_session(phone)
                return (
                    "No encontré más disponibilidad en los próximos días 😕\n\n"
                    f"Llama a recepción para más opciones:\n📞 *{CMC_TELEFONO}*"
                )
            nueva_fecha = todos_nuevo[0]["fecha"]
            fechas_vistas = fechas_vistas + [nueva_fecha]
            data.update({"slots": smart_nuevo, "todos_slots": todos_nuevo,
                         "fechas_vistas": fechas_vistas, "expansion_stage": 0})
            save_session(phone, "WAIT_SLOT", data)
            return _format_slots(smart_nuevo)

        idx = _parse_slot_selection(txt, slots_mostrados)
        if idx is None:
            if len(txt) > 2:
                result = await detect_intent(txt)
                intent = result.get("intent", "otro")
                if intent == "agendar" and result.get("especialidad"):
                    from medilink import _ids_para_especialidad
                    ids_nuevos = set(_ids_para_especialidad(result.get("especialidad", "")))
                    ids_actuales = {s.get("id_profesional") for s in todos_slots}
                    # Si el paciente menciona el mismo doctor/especialidad que ya está en pantalla,
                    # no resetear — solo recordarle que elija un número
                    if ids_nuevos and ids_nuevos & ids_actuales:
                        save_session(phone, "WAIT_SLOT", data)
                        return "Elige un número del listado, escribe *ver todos* para más horarios, u *otro día* si no te acomoda."
                    reset_session(phone)
                    return await _iniciar_agendar(phone, {}, result.get("especialidad"))
                if intent == "cancelar":
                    reset_session(phone)
                    return await _iniciar_cancelar(phone, {})
                if intent == "ver_reservas":
                    reset_session(phone)
                    return await _iniciar_ver(phone, {})
                if intent in ("precio", "info"):
                    esp_display = todos_slots[0]["especialidad"] if todos_slots else especialidad
                    # Siempre usar respuesta_faq con contexto de especialidad (ignorar respuesta_directa genérica)
                    consulta = f"¿Cuánto cuesta una consulta de {esp_display}?" if esp_display else txt
                    resp = await respuesta_faq(consulta)
                    return (
                        f"{resp}\n\n"
                        f"{DISCLAIMER}\n\n"
                        "_Elige un número para continuar con tu reserva o escribe *menu* para volver._"
                    )
            # Frustration detector — escalada en 3 niveles
            data["intentos_fallidos"] = data.get("intentos_fallidos", 0) + 1
            intentos = data["intentos_fallidos"]
            if intentos >= 3:
                return _derivar_humano(phone=phone, contexto="frustración WAIT_SLOT")
            save_session(phone, "WAIT_SLOT", data)
            if intentos == 2:
                return (
                    "Todavía no logro entenderte 😕\n\n"
                    "Escribe el *número* del horario que prefieres, *otro día* para cambiar de día, o *menu* para reiniciar."
                )
            return (
                "No te entendí bien 😅\n\n"
                "Puedes:\n"
                "• Escribir el *número* del horario\n"
                "• Escribir *otro día*\n"
                "• Escribir *ver todos* para más horarios"
            )

        slot = slots_mostrados[idx]
        data["slot_elegido"] = slot
        save_session(phone, "WAIT_MODALIDAD", data)
        return _btn_msg(
            f"Perfecto 🙌\n\n"
            f"🏥 *{slot['especialidad']}* — {slot['profesional']}\n"
            f"📅 *{slot['fecha_display']}*\n"
            f"🕐 *{slot['hora_inicio'][:5]}*\n\n"
            "¿Tu atención será Fonasa o Particular?",
            [
                {"id": "1", "title": "Fonasa"},
                {"id": "2", "title": "Particular"},
            ]
        )

    # ── WAIT_MODALIDAD ────────────────────────────────────────────────────────
    if state == "WAIT_MODALIDAD":
        FONASA     = {"1", "fonasa", "fona"}
        PARTICULAR = {"2", "particular", "privado", "privada"}
        if tl in FONASA:
            data["modalidad"] = "fonasa"
        elif tl in PARTICULAR:
            data["modalidad"] = "particular"
        else:
            data["intentos_fallidos"] = data.get("intentos_fallidos", 0) + 1
            if data["intentos_fallidos"] >= 3:
                return _derivar_humano(phone=phone, contexto="frustración WAIT_MODALIDAD")
            save_session(phone, "WAIT_MODALIDAD", data)
            return "Responde *Fonasa* o *Particular* 😊"

        save_session(phone, "WAIT_RUT_AGENDAR", data)
        modalidad_str = data["modalidad"].capitalize()
        # Si ya conocemos al paciente, mostrar su nombre y preguntar solo confirmación
        rut_conocido  = data.get("rut_conocido")
        nombre_conocido = data.get("nombre_conocido")
        if rut_conocido and nombre_conocido:
            nombre_corto = nombre_conocido.split()[0]
            return _btn_msg(
                f"Perfecto, atención *{modalidad_str}*.\n\n"
                f"¿Agendo con tus datos anteriores, *{nombre_corto}*?",
                [
                    {"id": "si", "title": "Sí, continuar"},
                    {"id": "rut_nuevo", "title": "Ingresar otro RUT"},
                ]
            )
        return (
            f"Perfecto, atención *{modalidad_str}* 😊\n\n"
            "Para confirmar necesito tu RUT:\n"
            "(ej: *12.345.678-9*)"
        )

    # ── WAIT_RUT_AGENDAR ──────────────────────────────────────────────────────
    if state == "WAIT_RUT_AGENDAR":
        # Si el paciente ya agendó antes y confirma con sí/ok, usar su RUT guardado
        rut_conocido = data.get("rut_conocido")
        if rut_conocido and tl in AFIRMACIONES | {"si", "sí", "ok", "mismo", "el mismo"} and tl != "rut_nuevo":
            rut = rut_conocido
        else:
            rut = clean_rut(txt)
        if not valid_rut(rut):
            data["intentos_fallidos"] = data.get("intentos_fallidos", 0) + 1
            if data["intentos_fallidos"] >= 3:
                return _derivar_humano(phone=phone, contexto="frustración WAIT_RUT_AGENDAR")
            save_session(phone, "WAIT_RUT_AGENDAR", data)
            return (
                "Ese RUT no quedó bien 😕\n"
                "Escríbelo con dígito verificador, por ejemplo: *12.345.678-9*"
            )

        paciente = await buscar_paciente(rut)
        if not paciente:
            data["rut"] = rut
            save_session(phone, "WAIT_NOMBRE_NUEVO", data)
            return (
                "No encontré ese RUT en el sistema 🔎\n\n"
                "No te preocupes, te registro ahora mismo.\n"
                "¿Cuál es tu nombre completo?\n"
                "(ej: *María González López*)"
            )

        data.update({"paciente": paciente, "rut": rut})
        save_session(phone, "CONFIRMING_CITA", data)

        slot = data["slot_elegido"]
        modalidad = data.get("modalidad", "particular").capitalize()
        return _btn_msg(
            f"Estás a un paso de confirmar tu hora 👇\n\n"
            f"👤 {paciente['nombre']}\n"
            f"🏥 {slot['especialidad']} — {slot['profesional']}\n"
            f"📅 {slot['fecha_display']}\n"
            f"🕐 {slot['hora_inicio'][:5]}–{slot['hora_fin'][:5]}\n"
            f"💳 {modalidad}\n\n"
            "¿La confirmo?",
            [
                {"id": "si", "title": "✅ Confirmar"},
                {"id": "no", "title": "❌ Cambiar"},
            ]
        )

    # ── CONFIRMING_CITA ───────────────────────────────────────────────────────
    if state == "CONFIRMING_CITA":
        if tl in AFIRMACIONES:
            slot    = data["slot_elegido"]
            paciente = data["paciente"]
            resultado = await crear_cita(
                id_paciente=paciente["id"],
                id_profesional=slot["id_profesional"],
                fecha=slot["fecha"],
                hora_inicio=slot["hora_inicio"],
                hora_fin=slot["hora_fin"],
                id_recurso=slot.get("id_recurso", 1),
            )
            reset_session(phone)
            nombre_corto = paciente['nombre'].split()[0]
            modalidad = data.get("modalidad", "particular").capitalize()
            if resultado:
                # Guardar perfil para no volver a pedir el RUT
                save_profile(phone, data.get("rut", ""), paciente["nombre"])
                # Registrar tag y cita para tracking/recordatorios
                esp = slot["especialidad"]
                save_tag(phone, f"cita-{esp.lower()}")
                save_tag(phone, f"modalidad-{data.get('modalidad','particular')}")
                id_cita = str(resultado.get("id", "")) if isinstance(resultado, dict) else ""
                save_cita_bot(
                    phone=phone,
                    id_cita=id_cita,
                    especialidad=esp,
                    profesional=slot["profesional"],
                    fecha=slot["fecha"],
                    hora=slot["hora_inicio"],
                    modalidad=data.get("modalidad", "particular"),
                )
                log_event(phone, "cita_creada", {
                    "especialidad": esp,
                    "profesional": slot["profesional"],
                    "fecha": slot["fecha"],
                    "modalidad": data.get("modalidad", "particular"),
                })
                return (
                    f"✅ *¡Listo, {nombre_corto}! Tu hora quedó reservada.*\n\n"
                    f"👤 {paciente['nombre']}\n"
                    f"🏥 {slot['especialidad']} — {slot['profesional']}\n"
                    f"📅 {slot['fecha_display']}\n"
                    f"🕐 {slot['hora_inicio'][:5]}\n"
                    f"💳 {modalidad}\n\n"
                    "Recuerda llegar *15 minutos antes* con tu cédula de identidad.\n\n"
                    "📍 *Monsalve 102 esq. República, Carampangue*\n\n"
                    "¡Te esperamos! 😊\n"
                    "_Escribe *menu* si necesitas algo más._"
                )
            else:
                return (
                    "Hubo un problema al reservar la hora 😕\n"
                    f"Llama a recepción: 📞 *{CMC_TELEFONO}*"
                )

        if tl in NEGACIONES:
            reset_session(phone)
            return (
                "No hay problema 😊\n\n"
                "• Escribe *otro día* para ver otros horarios\n"
                "• Escribe *menu* para volver al inicio"
            )

        return "Responde *SÍ* para confirmar o *NO* para cambiar."

    # ── WAIT_RUT_CANCELAR ─────────────────────────────────────────────────────
    if state == "WAIT_RUT_CANCELAR":
        rut = clean_rut(txt)
        if not valid_rut(rut):
            return (
                "Ese RUT no quedó bien 😕\n"
                "Escríbelo así: *12.345.678-9*"
            )

        paciente = await buscar_paciente(rut)
        if not paciente:
            reset_session(phone)
            return (
                "No encontré ese RUT en el sistema 🔎\n\n"
                f"Llama a recepción si necesitas ayuda:\n📞 *{CMC_TELEFONO}*\n\n"
                "_Escribe *menu* para volver._"
            )

        citas = await listar_citas_paciente(paciente["id"])
        if not citas:
            reset_session(phone)
            return (
                f"No encontré citas futuras para *{paciente['nombre'].split()[0]}* 📋\n\n"
                "¿Quieres agendar una nueva hora? Escribe *1* o *menu*."
            )

        data.update({"paciente": paciente, "citas": citas})
        save_session(phone, "WAIT_CITA_CANCELAR", data)
        return _format_citas_cancelar(citas, paciente["nombre"])

    # ── WAIT_CITA_CANCELAR ────────────────────────────────────────────────────
    if state == "WAIT_CITA_CANCELAR":
        citas = data.get("citas", [])
        try:
            idx = int(txt) - 1
            assert 0 <= idx < len(citas)
        except Exception:
            return f"Elige un número entre 1 y {len(citas)} 😊"

        cita = citas[idx]
        data["cita_cancelar"] = cita
        save_session(phone, "CONFIRMING_CANCEL", data)
        return _btn_msg(
            f"Vas a cancelar esta hora:\n\n"
            f"🏥 {cita['profesional']}\n"
            f"📅 {cita['fecha_display']}\n"
            f"🕐 {cita['hora_inicio']}\n\n"
            "¿Confirmas la cancelación?",
            [
                {"id": "si", "title": "✅ Sí, cancelar"},
                {"id": "no", "title": "❌ No, mantener"},
            ]
        )

    # ── CONFIRMING_CANCEL ─────────────────────────────────────────────────────
    if state == "CONFIRMING_CANCEL":
        if tl in AFIRMACIONES:
            cita = data["cita_cancelar"]
            ok = await cancelar_cita(cita["id"])
            reset_session(phone)
            if ok:
                log_event(phone, "cita_cancelada", {"id_cita": cita["id"], "profesional": cita.get("profesional")})
                save_tag(phone, "canceló")
                return (
                    f"✅ Cita cancelada correctamente.\n\n"
                    f"_{cita['profesional']} · {cita['fecha_display']} · {cita['hora_inicio']}_\n\n"
                    "¿Quieres agendar otra hora? Escribe *1* o *menu* para volver."
                )
            return f"Hubo un problema al cancelar 😕\nLlama a recepción: 📞 *{CMC_TELEFONO}*"

        if tl in NEGACIONES:
            reset_session(phone)
            return "Perfecto, tu cita se mantiene 😊\n_Escribe *menu* si necesitas algo más._"

        return "Responde *SÍ* para cancelar o *NO* para mantener la cita."

    # ── WAIT_RUT_VER ──────────────────────────────────────────────────────────
    if state == "WAIT_RUT_VER":
        rut = clean_rut(txt)
        if not valid_rut(rut):
            return (
                "Ese RUT no quedó bien 😕\n"
                "Escríbelo así: *12.345.678-9*"
            )

        paciente = await buscar_paciente(rut)
        if not paciente:
            reset_session(phone)
            return "No encontré ese RUT 🔎\nEscribe *menu* para volver o intenta de nuevo."

        citas = await listar_citas_paciente(paciente["id"])
        reset_session(phone)
        nombre_corto = paciente['nombre'].split()[0]
        if not citas:
            return (
                f"No tienes citas futuras agendadas, *{nombre_corto}* 📋\n\n"
                "¿Quieres agendar una ahora? Escribe *1* o *menu*."
            )

        lineas = [f"📋 *Tus próximas citas, {nombre_corto}:*\n"]
        for c in citas:
            lineas.append(f"• {c['fecha_display']} {c['hora_inicio']} — {c['profesional']}")
        lineas.append("\n_Escribe *menu* si necesitas algo más._")
        return "\n".join(lineas)

    # ── WAIT_NOMBRE_NUEVO ─────────────────────────────────────────────────────
    if state == "WAIT_NOMBRE_NUEVO":
        partes = txt.strip().split()
        if len(partes) < 2:
            return "Escribe tu nombre completo con nombre y apellido (ej: *María González*)."
        nombre   = partes[0].capitalize()
        apellidos = " ".join(p.capitalize() for p in partes[1:])
        rut = data.get("rut", "")
        paciente = await crear_paciente(rut, nombre, apellidos)
        if not paciente:
            reset_session(phone)
            return (
                "Hubo un problema al registrarte 😕\n"
                f"Llama a recepción: 📞 *{CMC_TELEFONO}*"
            )
        data.update({"paciente": paciente, "rut": rut})
        save_session(phone, "CONFIRMING_CITA", data)
        slot = data["slot_elegido"]
        modalidad = data.get("modalidad", "particular").capitalize()
        return _btn_msg(
            f"¡Listo, *{nombre}*! Ya quedaste registrado/a 🙌\n\n"
            f"¿Confirmas esta hora?\n\n"
            f"👤 *{paciente['nombre']}*\n"
            f"🏥 *{slot['especialidad']}* — {slot['profesional']}\n"
            f"📅 *{slot['fecha_display']}*\n"
            f"🕐 *{slot['hora_inicio'][:5]}*\n"
            f"💳 *{modalidad}*",
            [
                {"id": "si", "title": "✅ Confirmar"},
                {"id": "no", "title": "❌ Cambiar"},
            ]
        )

    # ── HUMAN_TAKEOVER ────────────────────────────────────────────────────────
    if state == "HUMAN_TAKEOVER":
        # Mensaje quedó guardado en el historial — recepcionista responde desde el panel
        # Solo respondemos si el paciente sigue enviando mensajes para que no sienta silencio
        msgs_sin_respuesta = data.get("msgs_sin_respuesta", 0) + 1
        data["msgs_sin_respuesta"] = msgs_sin_respuesta
        save_session(phone, "HUMAN_TAKEOVER", data)
        if msgs_sin_respuesta == 1:
            return "Recibido 🙏 Una recepcionista te responderá en este chat en breve."
        if msgs_sin_respuesta % 3 == 0:
            return f"Seguimos atentos 😊 Mientras esperas también puedes llamar: 📞 *{CMC_TELEFONO}*"
        return ""  # silencio — no spamear

    # Fallback
    reset_session(phone)
    return _menu_msg()


# ── Helpers de flujo ──────────────────────────────────────────────────────────

# Mapa de IDs ASCII (usados en listas WhatsApp) → nombre real de especialidad
_ESP_ID_MAP = {
    "esp_medgen":  "medicina general",
    "esp_medfam":  "medicina familiar",
    "esp_orl":     "otorrinolaringología",
    "esp_cardio":  "cardiología",
    "esp_trauma":  "traumatología",
    "esp_gineco":  "ginecología",
    "esp_gastro":  "gastroenterología",
    "esp_psico":   "psicología",
    "esp_fono":    "fonoaudiología",
    "esp_matrona": "matrona",
    "esp_odonto":  "odontología",
    "esp_orto":    "ortodoncia",
    "esp_endo":    "endodoncia",
    "esp_implant": "implantología",
    "esp_estetica":"estética facial",
    "esp_kine":    "kinesiología",
    "esp_nutri":   "nutrición",
    "esp_podo":    "podología",
    "esp_eco":     "ecografía",
}


def _especialidades_list_msg() -> dict:
    """Paso 1: elige categoría (WhatsApp permite máx 10 filas en total)."""
    return _btn_msg(
        "Claro, te ayudo a agendar 😊\n\n¿Qué área necesitas?",
        [
            {"id": "cat_medico", "title": "Médico y salud"},
            {"id": "cat_dental", "title": "Dental y kine"},
        ],
    )


def _especialidades_medico_msg() -> dict:
    return _list_msg(
        body_text="¿Qué especialidad médica necesitas?",
        button_label="Ver especialidades",
        sections=[{
            "title": "Médico y salud",
            "rows": [
                {"id": "esp_medgen",  "title": "Medicina General"},
                {"id": "esp_medfam",  "title": "Medicina Familiar"},
                {"id": "esp_orl",     "title": "Otorrinolaringología"},
                {"id": "esp_cardio",  "title": "Cardiología"},
                {"id": "esp_trauma",  "title": "Traumatología"},
                {"id": "esp_gineco",  "title": "Ginecología"},
                {"id": "esp_gastro",  "title": "Gastroenterología"},
                {"id": "esp_psico",   "title": "Psicología"},
                {"id": "esp_fono",    "title": "Fonoaudiología"},
                {"id": "esp_matrona", "title": "Matrona"},
            ],
        }],
    )


def _especialidades_dental_msg() -> dict:
    return _list_msg(
        body_text="¿Qué especialidad necesitas?",
        button_label="Ver especialidades",
        sections=[{
            "title": "Dental, kine y otros",
            "rows": [
                {"id": "esp_odonto",   "title": "Odontología General"},
                {"id": "esp_orto",     "title": "Ortodoncia"},
                {"id": "esp_endo",     "title": "Endodoncia"},
                {"id": "esp_implant",  "title": "Implantología"},
                {"id": "esp_estetica", "title": "Estética Facial"},
                {"id": "esp_kine",     "title": "Kinesiología"},
                {"id": "esp_nutri",    "title": "Nutrición"},
                {"id": "esp_podo",     "title": "Podología"},
                {"id": "esp_eco",      "title": "Ecografía"},
            ],
        }],
    )


# Especialidades con expansión progresiva por profesional
_ESPECIALIDADES_EXPANSION = {"medicina general"}
# IDs de profesionales de Medicina General, en orden de prioridad
_MED_GENERAL_IDS = [73, 1, 13]  # Abarca, Olavarría, Márquez


_ESPECIALIDADES_TEXTO = (
    "• Medicina General\n"
    "• Medicina Familiar\n"
    "• Otorrinolaringología\n"
    "• Cardiología\n"
    "• Traumatología\n"
    "• Ginecología\n"
    "• Gastroenterología\n"
    "• Odontología General\n"
    "• Ortodoncia\n"
    "• Endodoncia\n"
    "• Implantología\n"
    "• Estética Facial\n"
    "• Kinesiología\n"
    "• Nutrición\n"
    "• Psicología\n"
    "• Fonoaudiología\n"
    "• Matrona\n"
    "• Podología\n"
    "• Ecografía"
)


def _format_slots_expansion(groups: list, show_ver_mas: bool = False) -> str | dict:
    """Formatea slots agrupados por profesional. groups = [{"slots": [...]}].
    show_ver_mas=True agrega botón 'Ver más profesionales' (id=ver_todos)."""
    groups = [g for g in groups if g.get("slots")]
    if not groups:
        return "No hay más horarios disponibles."

    flat_slots = []
    for g in groups:
        flat_slots.extend(g["slots"])

    fecha_display = flat_slots[0]["fecha_display"]

    nav_rows = []
    if show_ver_mas:
        nav_rows.append({"id": "ver_todos", "title": "Ver más profesionales"})
    nav_rows.append({"id": "otro_dia", "title": "Buscar otro día"})

    total_rows = len(flat_slots) + len(nav_rows)

    if total_rows <= 10:
        sections = []
        offset = 0
        for g in groups:
            prof = g["slots"][0]["profesional"]
            rows = [{"id": str(offset + i + 1), "title": s["hora_inicio"][:5]}
                    for i, s in enumerate(g["slots"])]
            offset += len(g["slots"])
            sections.append({"title": prof[:24], "rows": rows})
        sections.append({"title": "Más opciones", "rows": nav_rows})
        return _list_msg(
            body_text=f"Horarios disponibles — *{fecha_display}* 👇",
            button_label="Ver horarios",
            sections=sections,
        )

    # Fallback texto para listas largas
    lineas = [f"📅 *{fecha_display}*\n"]
    idx = 1
    for g in groups:
        prof = g["slots"][0]["profesional"]
        lineas.append(f"\n*{prof}*")
        for s in g["slots"]:
            lineas.append(f"*{idx}.* {s['hora_inicio'][:5]}")
            idx += 1
    if show_ver_mas:
        lineas.append("\nElige un número, escribe *ver más* para ver más profesionales, u *otro día* para cambiar de día.")
    else:
        lineas.append("\nElige un número o escribe *otro día* para cambiar de día.")
    return "\n".join(lineas)


async def _handle_expansion(phone: str, data: dict, slots_mostrados: list,
                             todos_slots: list, stage: int, fecha: str | None) -> str | dict:
    """Expande progresivamente los horarios de Medicina General."""
    next_stage = stage + 1

    if next_stage == 1:
        # Mostrar smart de Abarca (ya guardado en data["slots"])
        data["expansion_stage"] = 1
        save_session(phone, "WAIT_SLOT", data)
        return _format_slots(data["slots"])

    elif next_stage == 2:
        # Smart de Abarca + smart de Olavarría
        smart_abarca = data.get("slots", [])
        smart_ola, todos_ola = (await buscar_slots_dia_por_ids([1],  fecha)) if fecha else ([], [])

        show_a = smart_abarca[:4]
        show_o = smart_ola[:4]
        combined = show_a + show_o

        data["expansion_stage"] = 2
        data["slots"] = combined
        data["todos_slots"] = todos_slots + todos_ola
        save_session(phone, "WAIT_SLOT", data)

        groups = []
        if show_a:
            groups.append({"slots": show_a})
        if show_o:
            groups.append({"slots": show_o})
        return _format_slots_expansion(groups, show_ver_mas=True) if groups else _format_slots(todos_slots, mostrar_todos=True)

    else:
        # Todos los horarios de los 3 profesionales (cada uno en su próximo día disponible)
        _, todos_a = (await buscar_slots_dia_por_ids([73], fecha)) if fecha else ([], [])
        _, todos_o = (await buscar_slots_dia_por_ids([1],  fecha)) if fecha else ([], [])
        # Márquez: siempre buscar su propio próximo día (puede no trabajar el mismo día que Abarca/Olavarría)
        _, todos_m = (await buscar_slots_dia_por_ids([13], fecha)) if fecha else ([], [])
        if not todos_m:
            _, todos_m = await buscar_primer_dia("medicina familiar")
        todos_all = todos_a + todos_o + todos_m

        data["expansion_stage"] = 3
        data["slots"] = todos_all
        data["todos_slots"] = todos_all
        save_session(phone, "WAIT_SLOT", data)

        groups = []
        if todos_a: groups.append({"slots": todos_a})
        if todos_o: groups.append({"slots": todos_o})
        if todos_m: groups.append({"slots": todos_m})
        return _format_slots_expansion(groups) if groups else "No hay más horarios disponibles."


async def _iniciar_agendar(phone: str, data: dict, especialidad: str | None) -> str:
    if not especialidad:
        save_session(phone, "WAIT_ESPECIALIDAD", data)
        return f"Claro, te ayudo a agendar 😊\n\n¿Qué especialidad necesitas?\n\n{_ESPECIALIDADES_TEXTO}"
    especialidad_lower = especialidad.lower()
    # Masoterapia tiene duración variable — preguntar antes de buscar slots
    if especialidad_lower in ("masoterapia", "masaje", "masajes"):
        data["especialidad"] = "masoterapia"
        save_session(phone, "WAIT_DURACION_MASOTERAPIA", data)
        return _btn_msg(
            "¿Cuánto tiempo necesitas para tu sesión de masoterapia?",
            [
                {"id": "maso_20", "title": "20 minutos"},
                {"id": "maso_40", "title": "40 minutos"},
            ]
        )
    # Para medicina general: stage 0 siempre muestra el slot más próximo de Abarca (73),
    # independiente de si Olavarría o Márquez tienen slots antes ese mismo día.
    _esp_med_general = {"medicina general", "medicina familiar"}
    if especialidad_lower in _esp_med_general:
        smart_abarca, todos_abarca = await buscar_primer_dia(especialidad_lower, solo_ids=[73])
        if todos_abarca:
            # Cargar todos los doctores del día de Abarca para la expansión "Ver más"
            fecha_abarca = todos_abarca[0]["fecha"]
            smart_todos, todos_todos = await buscar_slots_dia_por_ids(
                _MED_GENERAL_IDS, fecha_abarca
            )
            smart  = smart_abarca
            todos  = todos_todos if todos_todos else todos_abarca
            mejor  = todos_abarca[0]
        else:
            # Abarca sin disponibilidad en 60 días → fallback general
            smart, todos = await buscar_primer_dia(especialidad_lower)
            mejor = todos[0] if todos else None
    else:
        smart, todos = await buscar_primer_dia(especialidad_lower)
        mejor = smart[0] if smart else (todos[0] if todos else None)

    if not todos or not mejor:
        reset_session(phone)
        log_event(phone, "sin_disponibilidad", {"especialidad": especialidad})
        save_tag(phone, "sin-disponibilidad")
        return (
            f"No encontré disponibilidad para *{especialidad}* en los próximos días 😕\n\n"
            f"Llama a recepción para revisar más opciones:\n📞 *{CMC_TELEFONO}*\n\n"
            "_Escribe *menu* para volver._"
        )
    fecha = mejor["fecha"]
    data.update({"especialidad": especialidad_lower, "slots": smart,
                 "todos_slots": todos, "fechas_vistas": [fecha],
                 "expansion_stage": 0})
    save_session(phone, "WAIT_SLOT", data)
    nombre_conocido = data.get("nombre_conocido", "")
    nombre_corto = nombre_conocido.split()[0] if nombre_conocido else ""
    saludo = f"¡Hola de nuevo, *{nombre_corto}*! " if nombre_corto else ""
    return _btn_msg(
        f"{saludo}Encontré disponibilidad ✨\n\n"
        f"🏥 *{mejor['especialidad']}* — {mejor['profesional']}\n"
        f"📅 *{mejor['fecha_display']}*\n"
        f"🕐 *{mejor['hora_inicio'][:5]}* ⭐\n\n"
        "¿La agendo?",
        [
            {"id": "confirmar_sugerido", "title": "✅ Sí, esa hora"},
            {"id": "ver_otros",          "title": "📋 Ver más opciones"},
        ]
    )


async def _iniciar_cancelar(phone: str, data: dict) -> str:
    save_session(phone, "WAIT_RUT_CANCELAR", data)
    return (
        "Claro, te ayudo a cancelar una hora.\n\n"
        "Necesito tu RUT para buscarte:\n"
        "(ej: *12.345.678-9*)"
    )


async def _iniciar_ver(phone: str, data: dict) -> str:
    save_session(phone, "WAIT_RUT_VER", data)
    return (
        "Claro, te muestro tus reservas.\n\n"
        "Necesito tu RUT:\n"
        "(ej: *12.345.678-9*)"
    )


def _derivar_humano(phone: str = None, contexto: str = "") -> str:
    if phone:
        save_session(phone, "HUMAN_TAKEOVER", {"hold_sent": True, "handoff_reason": contexto[:200]})
        log_event(phone, "derivado_humano", {"razon": contexto[:200]})
    msg = (
        "Claro, te conecto con recepción 🙋\n\n"
        "Una recepcionista te responderá en este mismo chat en breve.\n\n"
        f"Si prefieres llamar: 📞 *{CMC_TELEFONO}* · ☎️ *{CMC_TELEFONO_FIJO}*\n\n"
        "_Atendemos de lunes a sábado._"
    )
    return msg


def _format_slots(slots: list, mostrar_todos: bool = False):
    if not slots:
        return "No hay horarios disponibles."
    fecha = slots[0]["fecha_display"]
    prof  = slots[0]["profesional"]

    # Usar lista interactiva cuando caben en el límite de 10 filas total
    nav_rows = []
    if not mostrar_todos:
        nav_rows.append({"id": "ver_todos", "title": "Ver todos los horarios"})
    nav_rows.append({"id": "otro_dia", "title": "Buscar otro día"})

    max_slots = 10 - len(nav_rows)
    if len(slots) <= max_slots:
        slot_rows = []
        for i, s in enumerate(slots, 1):
            hora = s["hora_inicio"][:5]
            title = f"⭐ {hora} (recomendado)" if i == 1 and not mostrar_todos else hora
            slot_rows.append({"id": str(i), "title": title[:24]})
        sections = [{"title": fecha[:24], "rows": slot_rows}]
        if nav_rows:
            sections.append({"title": "Más opciones", "rows": nav_rows})
        return _list_msg(
            body_text=f"Te encontré estas opciones 👇\n\n*{fecha}* — {prof}",
            button_label="Ver horarios",
            sections=sections,
        )

    # Fallback texto para listas muy largas
    lineas = [f"📅 *{fecha}* — {prof}\n"]
    for i, s in enumerate(slots, 1):
        hora = s['hora_inicio'][:5]
        prefix = f"*{i}.* ⭐ {hora} (recomendado)" if i == 1 and not mostrar_todos else f"*{i}.* {hora}"
        lineas.append(prefix)
    if mostrar_todos:
        lineas.append("\nElige un número o escribe *otro día* si no te acomoda.")
    else:
        lineas.append("\nElige un número, escribe *ver todos* para ver todos los horarios, u *otro día* para cambiar de día.")
    return "\n".join(lineas)


def _parse_slot_selection(txt: str, slots: list) -> int | None:
    """Interpreta texto libre como selección de slot. Retorna índice (0-based) o None."""
    if not slots:
        return None
    tl = txt.strip().lower()

    # Número directo: "1", "2", ...
    try:
        idx = int(txt.strip()) - 1
        if 0 <= idx < len(slots):
            return idx
    except ValueError:
        pass

    # Número dentro del texto: "el 1", "opción 2", "quiero el 3"
    m = re.search(r'\b([1-9])\b', tl)
    if m:
        idx = int(m.group(1)) - 1
        if 0 <= idx < len(slots):
            return idx

    # Hora en el texto: "las 10", "a las 10:20", "10:40", "las 11"
    m = re.search(r'\b(\d{1,2})(?::(\d{2}))?\b', tl)
    if m:
        h = m.group(1).zfill(2)
        mins = m.group(2) or None
        for i, s in enumerate(slots):
            hora = s["hora_inicio"][:5]  # "HH:MM"
            if mins:
                if hora == f"{h}:{mins}":
                    return i
            else:
                if hora.startswith(f"{h}:"):
                    return i

    return None


def _format_citas_cancelar(citas: list, nombre_paciente: str):
    nombre = nombre_paciente.split()[0]
    rows = []
    for i, c in enumerate(citas, 1):
        fecha_short = f"{c['fecha'][8:10]}/{c['fecha'][5:7]}" if c.get("fecha") else c.get("fecha_display", "")[:5]
        rows.append({
            "id": str(i),
            "title": f"{fecha_short} {c['hora_inicio']}"[:24],
            "description": c["profesional"][:72],
        })
    if len(rows) <= 10:
        return _list_msg(
            body_text=f"*{nombre}*, encontré estas reservas 👇\n¿Cuál quieres cancelar?",
            button_label="Ver citas",
            sections=[{"title": "Selecciona una cita", "rows": rows}],
        )
    # Fallback texto
    lineas = [f"*{nombre}*, estas son tus próximas citas:\n"]
    for i, c in enumerate(citas, 1):
        lineas.append(f"*{i}.* {c['fecha_display']} · {c['hora_inicio']} · {c['profesional']}")
    lineas.append("\n¿Cuál quieres cancelar? Responde con el número.")
    return "\n".join(lineas)
