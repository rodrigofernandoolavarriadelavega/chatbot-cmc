"""
Máquina de estados para los flujos de conversación.
Opción C: Claude detecta intención → sistema guía el flujo → Medilink ejecuta.
"""
import re
from datetime import datetime, timedelta

from claude_helper import detect_intent, respuesta_faq
from medilink import (buscar_primer_dia, buscar_slots_dia,
                      buscar_paciente, crear_paciente, crear_cita,
                      listar_citas_paciente, cancelar_cita,
                      valid_rut, clean_rut, especialidades_disponibles,
                      consultar_proxima_fecha)
from session import save_session, reset_session, save_tag, save_cita_bot, log_event, save_profile, get_profile
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

MENU = (
    "¡Hola! 👋 Bienvenido al *Centro Médico Carampangue*.\n\n"
    "Estamos en Carampangue, en el acceso a la Provincia de Arauco — un punto cómodo y fácil de llegar.\n"
    "Nos encuentras en *Monsalve 102, frente a la antigua estación de trenes*.\n\n"
    "¿En qué puedo ayudarte hoy?\n\n"
    "1️⃣ Agendar una hora\n"
    "2️⃣ Cancelar una hora\n"
    "3️⃣ Ver mis reservas\n"
    "4️⃣ Hablar con recepción\n\n"
    "También puedes escribirme con tus propias palabras 😊\n\n"
    "_Este chat es seguro y privado._"
)

AFIRMACIONES = {"si", "sí", "yes", "ok", "confirmo", "confirmar", "dale", "ya", "claro", "bueno"}
NEGACIONES   = {"no", "nop", "nope", "cancelar", "cancel", "no gracias"}

EMERGENCIAS  = {"emergencia", "urgencia", "dolor muy fuerte", "no puedo respirar",
                "estoy grave", "me estoy muriendo", "perdí el conocimiento",
                "mucho dolor", "accidente", "desmayo", "convulsion", "convulsión"}

DISCLAIMER = "_Recuerda que soy un asistente virtual, no un médico. Para consultas clínicas, habla siempre con un profesional de salud._"


async def handle_message(phone: str, texto: str, session: dict) -> str:
    state = session["state"]
    data  = session["data"]
    txt   = texto.strip()
    tl    = txt.lower()

    # ── Emergencias ───────────────────────────────────────────────────────────
    if any(p in tl for p in EMERGENCIAS):
        return (
            "⚠️ Eso suena como una situación urgente.\n\n"
            "Por favor llama al *SAMU: 131* o ve al servicio de urgencias más cercano de inmediato.\n\n"
            f"También puedes llamar a nuestra recepción: 📞 *{CMC_TELEFONO}*\n\n"
            "_Tu salud es lo primero._"
        )

    # ── Comandos globales ─────────────────────────────────────────────────────
    if tl in ("menu", "menú", "inicio", "reiniciar", "volver", "hola"):
        reset_session(phone)
        return MENU

    # ── IDLE: detectar intención ──────────────────────────────────────────────
    if state == "IDLE":
        # Atajos numéricos del menú
        if txt == "1": return await _iniciar_agendar(phone, data, None)
        if txt == "2": return await _iniciar_cancelar(phone, data)
        if txt == "3": return await _iniciar_ver(phone, data)
        if txt == "4": return _derivar_humano(contexto="menú opción 4")

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
            log_event(phone, "derivado_humano")
            return _derivar_humano(contexto=txt)

        if intent == "disponibilidad":
            especialidad = result.get("especialidad")
            if especialidad:
                fecha = await consultar_proxima_fecha(especialidad)
                if fecha:
                    return (
                        f"Para *{especialidad}* hay hora disponible el *{fecha}*. 📅\n\n"
                        "¿La agendamos ahora? Solo toma un par de minutos 😊\n\n"
                        "Escribe *1* para agendar o *menu* si necesitas algo más."
                    )
            return (
                f"Para consultar disponibilidad puedes llamar a recepción: 📞 *{CMC_TELEFONO}*\n\n"
                "_Escribe *menu* si necesitas algo más._"
            )

        if intent in ("precio", "info"):
            resp = result.get("respuesta_directa") or await respuesta_faq(txt)
            return resp + f"\n\n{DISCLAIMER}\n\n_Escribe *menu* si necesitas algo más._"

        # intent "otro" o "menu" (fallback de Claude) → mostrar menú
        return MENU

    # ── WAIT_ESPECIALIDAD ─────────────────────────────────────────────────────
    if state == "WAIT_ESPECIALIDAD":
        from medilink import _ids_para_especialidad
        # Si el texto no mapea directamente, usar Claude para extraer la especialidad
        especialidad_candidata = tl
        if not _ids_para_especialidad(tl):
            result = await detect_intent(txt)
            especialidad_candidata = result.get("especialidad") or tl
        return await _iniciar_agendar(phone, data, especialidad_candidata)

    # ── WAIT_SLOT ─────────────────────────────────────────────────────────────
    if state == "WAIT_SLOT":
        slots_mostrados = data.get("slots", [])          # los que ve el paciente ahora
        todos_slots     = data.get("todos_slots", slots_mostrados)  # todos del día
        fechas_vistas   = data.get("fechas_vistas", [])
        especialidad    = data.get("especialidad", "")

        # "ver todos" → mostrar todos los slots del día actual
        VER_TODOS = {"ver todos", "todos", "ver todo", "todos los horarios", "mostrar todos",
                     "ver horarios", "quiero ver los horarios", "ver todos los horarios",
                     "mostrar horarios", "quiero ver horarios", "ver mas", "ver más"}
        if tl in VER_TODOS or any(p in tl for p in ["ver todos", "todos los horarios", "ver horarios", "ver mas", "ver más"]):
            data["slots"] = todos_slots
            save_session(phone, "WAIT_SLOT", data)
            return _format_slots(todos_slots, mostrar_todos=True)

        # Día específico → "para el viernes", "hay para el martes", etc.
        dia_pedido = next((wd for nombre, wd in _DIAS_SEMANA.items() if nombre in tl), None)
        if dia_pedido is not None:
            fecha_dia = _proxima_fecha_dia(dia_pedido)
            if fecha_dia:
                smart_dia, todos_dia = await buscar_slots_dia(especialidad, fecha_dia)
                if todos_dia:
                    if fecha_dia not in fechas_vistas:
                        fechas_vistas = fechas_vistas + [fecha_dia]
                    data.update({"slots": smart_dia, "todos_slots": todos_dia, "fechas_vistas": fechas_vistas})
                    save_session(phone, "WAIT_SLOT", data)
                    return _format_slots(smart_dia)
            return f"No encontré disponibilidad para ese día 😕 Escribe *otro día* para buscar el siguiente disponible."

        # "otro día" → primeras 5 del siguiente día disponible
        OTRO_DIA = {"otro dia", "otro día", "otro", "no puedo", "no me sirve",
                    "no me acomoda", "cambiar dia", "cambiar día", "siguiente"}
        if tl in OTRO_DIA or any(p in tl for p in ["otro dia", "otro día", "no puedo"]):
            smart_nuevo, todos_nuevo = await buscar_primer_dia(especialidad, excluir=fechas_vistas)
            if not todos_nuevo:
                reset_session(phone)
                return f"No encontré más disponibilidad 😕 Llama a recepción: 📞 {CMC_TELEFONO}"
            nueva_fecha = todos_nuevo[0]["fecha"]
            fechas_vistas = fechas_vistas + [nueva_fecha]
            data.update({"slots": smart_nuevo, "todos_slots": todos_nuevo, "fechas_vistas": fechas_vistas})
            save_session(phone, "WAIT_SLOT", data)
            return _format_slots(smart_nuevo)

        idx = _parse_slot_selection(txt, slots_mostrados)
        if idx is None:
            if len(txt) > 2:
                result = await detect_intent(txt)
                intent = result.get("intent", "otro")
                if intent == "agendar" and result.get("especialidad"):
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
                    consulta = f"{txt} (especialidad: {esp_display})" if esp_display else txt
                    resp = result.get("respuesta_directa") or await respuesta_faq(consulta)
                    return resp + f"\n\n{DISCLAIMER}\n\n_Cuando quieras, elige un número para continuar con tu reserva o escribe *menu* para volver al inicio._"
            # Frustration detector
            data["intentos_fallidos"] = data.get("intentos_fallidos", 0) + 1
            if data["intentos_fallidos"] >= 3:
                reset_session(phone)
                log_event(phone, "derivado_humano", {"razon": "frustración", "estado": "WAIT_SLOT"})
                return (
                    "Parece que estás teniendo dificultades 😕\n\n"
                    "Te conecto con recepción para que te ayuden:\n"
                    f"📞 *{CMC_TELEFONO}*\n\n"
                    "_Escribe *menu* si quieres intentarlo de nuevo._"
                )
            save_session(phone, "WAIT_SLOT", data)
            return "Elige un número, escribe *ver todos* para ver todos los horarios del día, u *otro día* si no te acomoda."

        slot = slots_mostrados[idx]
        data["slot_elegido"] = slot
        save_session(phone, "WAIT_MODALIDAD", data)
        return (
            f"¡Excelente elección! 👍\n\n"
            f"🏥 *{slot['especialidad']}* — {slot['profesional']}\n"
            f"📅 *{slot['fecha_display']}* a las *{slot['hora_inicio'][:5]}*\n\n"
            "¿Tu atención será:\n\n"
            "1️⃣ Fonasa\n"
            "2️⃣ Particular"
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
                reset_session(phone)
                log_event(phone, "derivado_humano", {"razon": "frustración", "estado": "WAIT_MODALIDAD"})
                return (
                    "Parece que estás teniendo dificultades 😕\n\n"
                    "Te conecto con recepción:\n"
                    f"📞 *{CMC_TELEFONO}*"
                )
            save_session(phone, "WAIT_MODALIDAD", data)
            return "Por favor responde *1* para Fonasa o *2* para Particular."

        save_session(phone, "WAIT_RUT_AGENDAR", data)
        modalidad_str = data["modalidad"]
        # Si ya conocemos al paciente, mostrar su nombre y preguntar solo confirmación
        rut_conocido  = data.get("rut_conocido")
        nombre_conocido = data.get("nombre_conocido")
        if rut_conocido and nombre_conocido:
            nombre_corto = nombre_conocido.split()[0]
            return (
                f"Perfecto, atención *{modalidad_str}*.\n\n"
                f"¿Confirmo con tus datos anteriores, *{nombre_corto}*? Responde *sí* o escribe tu RUT si cambiaron."
            )
        return (
            f"Perfecto, atención *{modalidad_str}*.\n\n"
            "Para confirmar tu hora necesito tu RUT. Tus datos se usan solo para gestionar esta cita y se tratan con total confidencialidad.\n\n"
            "¿Cuál es tu RUT? (ej: 12.345.678-9)"
        )

    # ── WAIT_RUT_AGENDAR ──────────────────────────────────────────────────────
    if state == "WAIT_RUT_AGENDAR":
        # Si el paciente ya agendó antes y confirma con sí/ok, usar su RUT guardado
        rut_conocido = data.get("rut_conocido")
        if rut_conocido and tl in AFIRMACIONES | {"si", "sí", "ok", "mismo", "el mismo"}:
            rut = rut_conocido
        else:
            rut = clean_rut(txt)
        if not valid_rut(rut):
            data["intentos_fallidos"] = data.get("intentos_fallidos", 0) + 1
            if data["intentos_fallidos"] >= 3:
                reset_session(phone)
                log_event(phone, "derivado_humano", {"razon": "frustración", "estado": "WAIT_RUT_AGENDAR"})
                return (
                    "Parece que estás teniendo dificultades con el RUT 😕\n\n"
                    "Te conecto con recepción:\n"
                    f"📞 *{CMC_TELEFONO}*\n\n"
                    "_Escribe *menu* para intentarlo de nuevo._"
                )
            save_session(phone, "WAIT_RUT_AGENDAR", data)
            return "RUT inválido ❌ Por favor ingresa tu RUT con dígito verificador (ej: *12.345.678-9*)"

        paciente = await buscar_paciente(rut)
        if not paciente:
            data["rut"] = rut
            save_session(phone, "WAIT_NOMBRE_NUEVO", data)
            return (
                "No encontré ese RUT en nuestro sistema 🔍\n\n"
                "¡No te preocupes, te registro ahora mismo! 😊\n\n"
                "¿Cuál es tu nombre completo? (ej: *María González López*)"
            )

        data.update({"paciente": paciente, "rut": rut})
        save_session(phone, "CONFIRMING_CITA", data)

        slot = data["slot_elegido"]
        modalidad = data.get("modalidad", "particular").capitalize()
        return (
            f"¿Confirmas esta reserva? 📋\n\n"
            f"👤 *{paciente['nombre']}*\n"
            f"🏥 *{slot['especialidad']}* — {slot['profesional']}\n"
            f"📅 *{slot['fecha_display']}*\n"
            f"🕐 *{slot['hora_inicio'][:5]} – {slot['hora_fin'][:5]}*\n"
            f"💳 *{modalidad}*\n\n"
            "Responde *SÍ* para confirmar o *NO* para cancelar."
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
                    "📍 *Monsalve 102 esq. República, Carampangue* — frente a la antigua estación de trenes.\n\n"
                    "¡Te esperamos! 😊\n\n"
                    "_Escribe *menu* si necesitas algo más._"
                )
            else:
                return (
                    "Ocurrió un problema al crear la cita 😕\n"
                    f"Por favor llama a recepción: 📞 {CMC_TELEFONO}"
                )

        if tl in NEGACIONES:
            reset_session(phone)
            return "Entendido, no hay problema. Escribe *menu* cuando quieras intentar de nuevo 😊"

        return "Responde *SÍ* para confirmar o *NO* para cancelar."

    # ── WAIT_RUT_CANCELAR ─────────────────────────────────────────────────────
    if state == "WAIT_RUT_CANCELAR":
        rut = clean_rut(txt)
        if not valid_rut(rut):
            return "RUT inválido ❌ Ingresa tu RUT con dígito verificador (ej: *12.345.678-9*)"

        paciente = await buscar_paciente(rut)
        if not paciente:
            reset_session(phone)
            return (
                "No encontré ese RUT en nuestro sistema 🔍\n"
                f"Llama a recepción: 📞 {CMC_TELEFONO}\n\n"
                "_Escribe *menu* para volver al inicio._"
            )

        citas = await listar_citas_paciente(paciente["id"])
        if not citas:
            reset_session(phone)
            return (
                f"No encontré citas futuras para *{paciente['nombre']}* 📋\n\n"
                "_Escribe *menu* para volver al inicio._"
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
            return f"Por favor elige un número entre 1 y {len(citas)}."

        cita = citas[idx]
        data["cita_cancelar"] = cita
        save_session(phone, "CONFIRMING_CANCEL", data)
        return (
            f"¿Confirmas la cancelación? ❌\n\n"
            f"🏥 *{cita['profesional']}*\n"
            f"📅 *{cita['fecha_display']}*\n"
            f"🕐 *{cita['hora_inicio']}*\n\n"
            "Responde *SÍ* para cancelar o *NO* para mantener la cita."
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
                    "_Escribe *menu* si necesitas algo más._"
                )
            return f"Hubo un problema al cancelar 😕 Llama a recepción: 📞 {CMC_TELEFONO}"

        if tl in NEGACIONES:
            reset_session(phone)
            return "Entendido, la cita se mantiene. ¡Hasta pronto! 😊"

        return "Responde *SÍ* para cancelar la cita o *NO* para mantenerla."

    # ── WAIT_RUT_VER ──────────────────────────────────────────────────────────
    if state == "WAIT_RUT_VER":
        rut = clean_rut(txt)
        if not valid_rut(rut):
            return "RUT inválido ❌ Ingresa tu RUT con dígito verificador (ej: *12.345.678-9*)"

        paciente = await buscar_paciente(rut)
        if not paciente:
            reset_session(phone)
            return "No encontré ese RUT en nuestro sistema 🔍\n_Escribe *menu* para volver al inicio._"

        citas = await listar_citas_paciente(paciente["id"])
        reset_session(phone)
        if not citas:
            return f"No tienes citas futuras agendadas, *{paciente['nombre']}* 📋\n_Escribe *menu* para volver al inicio._"

        lineas = [f"📋 *Tus próximas citas, {paciente['nombre'].split()[0]}:*\n"]
        for c in citas:
            lineas.append(f"• {c['fecha_display']} {c['hora_inicio']} — {c['profesional']}")
        lineas.append("\n_Escribe *menu* para volver al inicio._")
        return "\n".join(lineas)

    # ── WAIT_NOMBRE_NUEVO ─────────────────────────────────────────────────────
    if state == "WAIT_NOMBRE_NUEVO":
        partes = txt.strip().split()
        if len(partes) < 2:
            return "Por favor escribe tu nombre completo con al menos nombre y apellido (ej: *María González*)."
        nombre   = partes[0].capitalize()
        apellidos = " ".join(p.capitalize() for p in partes[1:])
        rut = data.get("rut", "")
        paciente = await crear_paciente(rut, nombre, apellidos)
        if not paciente:
            reset_session(phone)
            return (
                "Hubo un problema al registrarte 😕\n"
                f"Por favor llama a recepción: 📞 *{CMC_TELEFONO}*"
            )
        data.update({"paciente": paciente, "rut": rut})
        save_session(phone, "CONFIRMING_CITA", data)
        slot = data["slot_elegido"]
        return (
            f"¡Bienvenido/a, *{nombre}*! Tu registro quedó listo 🎉\n\n"
            f"¿Confirmas esta reserva?\n\n"
            f"👤 *{paciente['nombre']}*\n"
            f"🏥 *{slot['especialidad']}* — {slot['profesional']}\n"
            f"📅 *{slot['fecha_display']}*\n"
            f"🕐 *{slot['hora_inicio'][:5]} – {slot['hora_fin'][:5]}*\n\n"
            "Responde *SÍ* para confirmar o *NO* para cancelar."
        )

    # Fallback
    reset_session(phone)
    return MENU


# ── Helpers de flujo ──────────────────────────────────────────────────────────

async def _iniciar_agendar(phone: str, data: dict, especialidad: str | None) -> str:
    if not especialidad:
        save_session(phone, "WAIT_ESPECIALIDAD", data)
        return (
            "Con gusto te ayudo a agendar 📅\n\n"
            "¿Qué especialidad necesitas?\n\n"
            f"{especialidades_disponibles()}"
        )
    especialidad_lower = especialidad.lower()
    smart, todos = await buscar_primer_dia(especialidad_lower)
    if not todos:
        reset_session(phone)
        log_event(phone, "sin_disponibilidad", {"especialidad": especialidad})
        save_tag(phone, "sin-disponibilidad")
        return (
            f"No encontré disponibilidad para *{especialidad}* en los próximos días 😕\n\n"
            "Te recomiendo llamar a recepción para más opciones:\n"
            f"📞 {CMC_TELEFONO}\n\n"
            "_Escribe *menu* para volver al inicio._"
        )
    fecha = todos[0]["fecha"]
    data.update({"especialidad": especialidad_lower, "slots": smart,
                 "todos_slots": todos, "fechas_vistas": [fecha]})
    save_session(phone, "WAIT_SLOT", data)
    slots_msg = _format_slots(smart)
    # Si tenemos RUT guardado, anticipar el paso siguiente
    rut_conocido = data.get("rut_conocido")
    nombre_conocido = data.get("nombre_conocido")
    if rut_conocido and nombre_conocido:
        nombre_corto = nombre_conocido.split()[0]
        slots_msg += f"\n\n_Cuando elijas hora, usaré tus datos anteriores, {nombre_corto}_ 😊"
    return slots_msg


async def _iniciar_cancelar(phone: str, data: dict) -> str:
    save_session(phone, "WAIT_RUT_CANCELAR", data)
    return "Para cancelar una hora necesito tu RUT ¿Cuál es? (ej: 12.345.678-9)"


async def _iniciar_ver(phone: str, data: dict) -> str:
    save_session(phone, "WAIT_RUT_VER", data)
    return "Para ver tus reservas necesito tu RUT ¿Cuál es? (ej: 12.345.678-9)"


def _derivar_humano(contexto: str = "") -> str:
    msg = (
        "¡Claro, con mucho gusto te conecto! 🙋\n\n"
        "Puedes llamar directamente a recepción:\n"
        f"📞 Fijo: *{CMC_TELEFONO_FIJO}*\n"
        f"📱 WhatsApp: *{CMC_TELEFONO}*\n\n"
        "Nuestro equipo te atenderá de lunes a sábado. ¡Gracias por confiar en nosotros! 😊\n\n"
        "_Escribe *menu* si necesitas algo más._"
    )
    # Nota interna para trazabilidad (no visible al paciente, queda en el log)
    if contexto:
        log_event("_sistema", "handoff_contexto", {"mensaje_original": contexto})
    return msg


def _format_slots(slots: list, mostrar_todos: bool = False) -> str:
    fecha = slots[0]["fecha_display"] if slots else ""
    prof  = slots[0]["profesional"] if slots else ""
    lineas = [f"📅 *{fecha}* — {prof}\n"]
    for i, s in enumerate(slots, 1):
        lineas.append(f"*{i}.* {s['hora_inicio'][:5]}")
    if mostrar_todos:
        lineas.append("\nElige un número o escribe *otro día* si no te acomoda.")
    else:
        lineas.append("\nElige un número, escribe *ver todos* para ver todos los horarios del día, u *otro día* si no te acomoda.")
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


def _format_citas_cancelar(citas: list, nombre_paciente: str) -> str:
    nombre = nombre_paciente.split()[0]
    lineas = [f"*{nombre}*, estas son tus próximas citas:\n"]
    for i, c in enumerate(citas, 1):
        lineas.append(f"*{i}.* {c['fecha_display']} · {c['hora_inicio']} · {c['profesional']}")
    lineas.append("\n¿Cuál quieres cancelar? Responde con el número.")
    return "\n".join(lineas)
