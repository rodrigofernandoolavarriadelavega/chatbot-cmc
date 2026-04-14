"""
Máquina de estados para los flujos de conversación.
Opción C: Claude detecta intención → sistema guía el flujo → Medilink ejecuta.
"""
import asyncio
import logging
import re
import time
from datetime import datetime, timedelta

from claude_helper import detect_intent, respuesta_faq, clasificar_respuesta_seguimiento, consulta_clinica_doctor
from medilink import (buscar_primer_dia, buscar_slots_dia, buscar_slots_dia_por_ids,
                      buscar_paciente, buscar_paciente_por_nombre, crear_paciente, crear_cita,
                      listar_citas_paciente, cancelar_cita, obtener_agenda_dia,
                      valid_rut, clean_rut, especialidades_disponibles,
                      consultar_proxima_fecha)
from session import (save_session, reset_session, save_tag, delete_tag, get_tags,
                     save_cita_bot, log_event,
                     save_profile, get_profile, save_fidelizacion_respuesta, get_ultimo_seguimiento,
                     enqueue_intent, add_to_waitlist, cancel_waitlist,
                     get_cita_bot_by_id_cita, mark_cita_confirmation, get_phone_by_rut)
from resilience import is_medilink_down
from triage_ges import triage_sintomas, normalizar_texto_paciente
from pni import get_vaccine_reminder
from config import CMC_TELEFONO, CMC_TELEFONO_FIJO
from messaging import send_whatsapp

log = logging.getLogger("bot.flows")

# Teléfono del médico director para alertas clínicas (Dr. Olavarría)
ADMIN_ALERT_PHONE = "56987834148"

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

EMERGENCIAS  = {
    # generales
    "emergencia", "urgencia", "dolor muy fuerte", "no puedo respirar",
    "estoy grave", "me estoy muriendo", "perdí el conocimiento", "perdi el conocimiento",
    "mucho dolor", "accidente", "desmayo", "convulsion", "convulsión",
    # respiratorio severo
    "me ahogo", "no me entra aire", "ahogo fuerte",
    # cardiovascular severo
    "dolor de pecho fuerte", "dolor fuerte en el pecho", "dolor en el pecho fuerte",
    "me duele mucho el pecho", "infarto", "me da un infarto",
    # sangrado
    "sangre en deposiciones", "vómito con sangre", "vomito con sangre",
    "hemorragia", "sangrado abundante", "mucho sangrado",
    # trauma
    "me golpeé la cabeza", "me golpee la cabeza", "caída fuerte", "caida fuerte",
    "fractura", "hueso expuesto", "accidente grave",
    # quemaduras / araña
    "quemadura grave", "me quemé mucho", "me queme mucho",
    "araña de rincón", "arana de rincon", "araña rincón", "arana rincon",
    "mordedura de araña", "mordedura de arana", "loxosceles", "picó araña", "pico araña",
    # intoxicaciones
    "intoxicación por mariscos", "intoxicacion por mariscos", "marea roja",
    # neurológico grave
    "no despierta", "no reacciona", "perdida de conciencia",
    # ocular urgente
    "no veo", "perdí la vista", "perdi la vista", "ceguera súbita",
}

# Patrones regex para emergencias con redacción flexible
# Capturan variantes como "dolor fuerte en el pecho", "mucho sangrado", "me sangra mucho", etc.
EMERGENCIAS_PATRONES = [
    re.compile(r"dolor.{0,20}fuerte.{0,20}pecho"),
    re.compile(r"pecho.{0,20}dolor.{0,20}fuerte"),
    re.compile(r"duele.{0,20}pecho.{0,20}(fuerte|mucho|harto|arto|insoport|tanto)"),
    re.compile(r"(fuerte|mucho|harto).{0,10}(me\s+)?duele.{0,20}pecho"),
    re.compile(r"pecho.{0,15}(me\s+)?duele.{0,15}(fuerte|mucho|harto)"),
    re.compile(r"duele.{0,10}(fuerte|mucho|harto|arto).{0,15}pecho"),
    re.compile(r"mucho.{0,10}sangr"),
    re.compile(r"sangr\w*.{0,15}mucho"),
    re.compile(r"sangr\w*.{0,15}no\s+para"),
    re.compile(r"no\s+para.{0,15}sangr"),
    re.compile(r"hemorragia"),
]

# Patrones de amenaza vital física con lookahead negativo para excluir
# colloquialismos chilenos como "me muero de hambre/risa/sed/calor/sueño/frío".
# Estos patrones tienen que ser regex (no substrings) porque el set EMERGENCIAS
# hace match por substring y "me muero" estaría dentro de "me muero de hambre".
_COLLOQ_MUERO = r"(hambre|sed|risa|calor|sueno|sueño|frio|frío|ganas|amor|pena|aburri|cansanci|nervios|ansi|susto|verguenza|vergüenza|pica|celos|rabia|emocion|emoción|alegria|alegría)"
EMERGENCIAS_VITAL_PATRONES = [
    re.compile(rf"\bme\s+muero(?!\s+de\s+{_COLLOQ_MUERO})"),
    re.compile(rf"\bme\s+voy\s+a\s+morir(?!\s+de\s+{_COLLOQ_MUERO})"),
    re.compile(rf"\bvoy\s+a\s+morir(?!\s+de\s+{_COLLOQ_MUERO})"),
    re.compile(r"\bcreo\s+que\s+me\s+(muero|voy\s+a\s+morir)"),
    re.compile(r"\bme\s+siento\s+morir"),
    re.compile(rf"\bme\s+estoy\s+muriendo(?!\s+de\s+{_COLLOQ_MUERO})"),
    re.compile(r"\bestoy\s+muri[eé]ndome"),
    re.compile(r"\bme\s+estoy\s+por\s+morir"),
]

# Crisis de salud mental / ideación suicida — respuesta diferenciada con
# Salud Responde 600 360 7777 además del SAMU. Tono de contención.
# "me quiero morir" va acá, NO a amenaza vital física (merece otro mensaje).
SALUD_MENTAL_CRISIS = {
    "me quiero matar", "me quiero suicidar", "quiero suicidarme",
    "quiero matarme", "voy a suicidarme", "voy a matarme",
    "no quiero vivir", "no quiero seguir viviendo",
    "pensamientos suicidas", "ideacion suicida", "ideación suicida",
    "quiero acabar con todo", "quiero acabar con mi vida",
    "no aguanto mas vivir", "no aguanto más vivir",
}

SALUD_MENTAL_PATRONES = [
    re.compile(r"\b(me\s+quiero|quiero)\s+(morir|matar|suicidar)"),
    re.compile(r"\b(me\s+voy\s+a|voy\s+a)\s+(matar|suicidar)(?:me)?"),
    re.compile(r"\bno\s+quiero\s+(vivir|seguir\s+viviendo|estar\s+vivo)"),
    re.compile(r"\bpensamientos?\s+suicida"),
    re.compile(r"\bquiero\s+acabar\s+con\s+(todo|mi\s+vida)"),
]

DISCLAIMER = "_Recuerda que soy un asistente virtual, no un médico. Para consultas clínicas, habla siempre con un profesional de salud._"

# Señales léxicas de síntoma — si el texto del paciente las contiene pero el
# motor de triage NO produce match, vale la pena loggear el texto para revisar
# los gaps de recall del corpus GES semanalmente.
_SENALES_SINTOMA = re.compile(
    r"\b(me\s+duele|me\s+siento|siento|dolor|molest|ardor|nause|mareo|"
    r"fiebre|tos|flem|diarrea|vomit|sangr|picaz|inflam|hincha|"
    r"no\s+puedo|no\s+me\s+puedo|no\s+para|hace\s+\w+\s+que|"
    r"desde\s+hace|tengo\s+un|tengo\s+una|tengo\s+mucho)",
    re.IGNORECASE,
)


# ── Precios para mostrar en la oferta de slot ─────────────────────────────────
# Se muestran en el mismo mensaje donde el bot ofrece horarios, para matar la
# pregunta "¿cuánto cuesta?" antes de que el paciente la haga. La mayoría de
# los pacientes CMC son Fonasa MLE N3 → cuando hay bono, mostramos el precio
# Fonasa; cuando es solo particular, mostramos el precio particular. Los
# pacientes particulares pueden preguntar por el valor particular.
# Clave = valor exacto de PROFESIONALES[id]["especialidad"] en medilink.py
# Valor = (modalidad, precio, sufijo_opcional)
PRECIOS_SLOT = {
    "Medicina General":       ("fonasa",     7880),
    "Kinesiología":           ("fonasa",     7830),
    "Psicología Adulto":      ("fonasa",    14420),
    "Psicología Infantil":    ("fonasa",    14420),
    "Nutrición":              ("fonasa",     4770),
    "Matrona":                ("fonasa",    16000),
    "Fonoaudiología":         ("particular", 25000),
    "Podología":              ("particular", 20000, "desde"),
    "Cardiología":            ("particular", 40000),
    "Ginecología":            ("particular", 30000),
    "Traumatología":          ("particular", 35000),
    "Otorrinolaringología":   ("particular", 35000),
    "Gastroenterología":      ("particular", 35000),
    "Ecografía":              ("particular", 40000),
    "Odontología General":    ("particular", 15000, "evaluación"),
    "Ortodoncia":             ("particular", 30000, "control"),
    "Endodoncia":             ("particular",110000, "desde"),
    "Implantología":          ("particular",650000, "desde"),
    "Estética Facial":        ("particular", 15000, "evaluación"),
    # Masoterapia se resuelve dinámicamente según la duración real del slot.
}


# ── Cross-reference entre especialidades complementarias ─────────────────────
# Tras confirmar una cita, el bot sugiere la especialidad complementaria.
# Clave = valor exacto de PROFESIONALES[id]["especialidad"] en medilink.py.
# Valor = mensaje de cross-reference. Extensible: agregar más pares aquí.
CROSS_REFERENCE: dict[str, str] = {
    "Otorrinolaringología": (
        "\n\n💡 *¿Sabías que tenemos Fonoaudióloga?*\n"
        "Juana Arratia atiende en el CMC y realiza:\n"
        "• Audiometría ($25.000)\n"
        "• Audiometría + Impedanciometría ($45.000)\n"
        "• Impedanciometría ($20.000)\n"
        "• Evaluación + Maniobra VPPB ($50.000)\n"
        "• Octavo Par ($50.000)\n"
        "• Evaluación infantil/adulto ($30.000)\n"
        "• Sesión de terapia infantil/adulto ($25.000)\n"
        "• Terapia vestibular / Terapia tinnitus ($25.000)\n"
        "• Prueba y calibración de audífonos\n\n"
        "Muchas atenciones de ORL se complementan con fonoaudiología. "
        "Si te interesa, escribe *menu* y agenda con ella 😊"
    ),
    "Fonoaudiología": (
        "\n\n💡 *¿Sabías que tenemos Otorrinolaringólogo?*\n"
        "Dr. Manuel Borrego atiende en el CMC y puede ayudarte con:\n"
        "• Evaluación de oído, nariz y garganta\n"
        "• Sinusitis, rinitis, amigdalitis\n"
        "• Problemas de audición\n"
        "• Vértigo y mareos\n\n"
        "Muchas atenciones de fonoaudiología se complementan con ORL. "
        "Si te interesa, escribe *menu* y agenda con él 😊"
    ),
}

# Cross-sell inteligente post-consulta: cuando el paciente responde "Mejor",
# le sugerimos un servicio complementario en vez de un control genérico.
# Clave = especialidad (lowercase), Valor = (mensaje, especialidad_destino)
UPSELL_POSTCONSULTA: dict[str, tuple[str, str]] = {
    "traumatología": (
        "Para consolidar tu recuperación, la kinesiología puede marcar la diferencia 💪\n\n"
        "¿Quieres agendar con nuestros kinesiólogos?",
        "kinesiología",
    ),
    "medicina general": (
        "Ya que estás bien, ¿qué tal un chequeo preventivo anual? 🩺\n"
        "Incluye evaluación cardiovascular, metabólica y según tu edad.\n\n"
        "¿Te gustaría agendarlo?",
        "medicina general",
    ),
    "odontología general": (
        "Ahora que estás bien, ¿te gustaría mejorar la estética de tu sonrisa? ✨\n"
        "Tenemos blanqueamiento y estética dental.\n\n"
        "¿Te interesa agendar una evaluación?",
        "odontología general",
    ),
    "kinesiología": (
        "Para complementar tu mejoría, una sesión de masoterapia puede ayudarte "
        "a mantener los resultados 🙌\n\n"
        "¿Te interesa agendar con nuestra masoterapeuta?",
        "masoterapia",
    ),
    "otorrinolaringología": (
        "Muchas atenciones de ORL se complementan con fonoaudiología 🗣️\n"
        "Tenemos audiometría, terapia vestibular y más.\n\n"
        "¿Te gustaría agendar con nuestra fonoaudióloga?",
        "fonoaudiología",
    ),
    "fonoaudiología": (
        "Si necesitas evaluación de oído o garganta, nuestro otorrinolaringólogo "
        "puede complementar tu atención 👂\n\n"
        "¿Te interesa agendar?",
        "otorrinolaringología",
    ),
}


_MESES_ES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
    "ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6,
    "jul": 7, "ago": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dic": 12,
}


def _parsear_fecha_nacimiento(texto: str):
    """Parsea fecha de nacimiento en múltiples formatos comunes de WhatsApp.
    Retorna datetime.date o None si no puede parsear.
    Formatos soportados:
      - DD/MM/YYYY, DD-MM-YYYY, DD.MM.YYYY (con o sin ceros)
      - DDMMYYYY (8 dígitos pegados)
      - DD de mes YYYY, DD mes YYYY, DD-mes-YYYY
      - DD/MM/YY (año corto)
      - YYYY-MM-DD (ISO)
    """
    from datetime import date as _date
    txt = texto.strip().lower().replace("del", "de").replace(",", " ").replace("  ", " ")

    # 1) DD/MM/YYYY o DD-MM-YYYY o DD.MM.YYYY (separador / - .)
    m = re.match(r"^(\d{1,2})[/\-.\s](\d{1,2})[/\-.\s](\d{4})$", txt)
    if m:
        try:
            return _date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            return None

    # 2) DD/MM/YY (año corto: 00-30 → 2000s, 31-99 → 1900s)
    m = re.match(r"^(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2})$", txt)
    if m:
        try:
            yy = int(m.group(3))
            anio = 2000 + yy if yy <= 30 else 1900 + yy
            return _date(anio, int(m.group(2)), int(m.group(1)))
        except ValueError:
            return None

    # 3) YYYY-MM-DD (ISO)
    m = re.match(r"^(\d{4})[/\-.](\d{1,2})[/\-.](\d{1,2})$", txt)
    if m:
        try:
            return _date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None

    # 4) 8 dígitos pegados: DDMMYYYY
    m = re.match(r"^(\d{2})(\d{2})(\d{4})$", txt)
    if m:
        try:
            return _date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            return None

    # 5) "15 de marzo de 1990", "15 marzo 1990", "15-marzo-1990"
    m = re.match(r"^(\d{1,2})[\s\-/]+(?:de\s+)?([a-záéíóúñ]+)[\s\-/]+(?:de\s+)?(\d{4})$", txt)
    if m:
        dia, mes_str, anio = m.group(1), m.group(2), m.group(3)
        mes_num = _MESES_ES.get(mes_str)
        if mes_num:
            try:
                return _date(int(anio), mes_num, int(dia))
            except ValueError:
                return None

    # 6) "marzo 15 1990" (mes primero en texto)
    m = re.match(r"^([a-záéíóúñ]+)[\s\-/]+(\d{1,2})[\s,\-/]+(\d{4})$", txt)
    if m:
        mes_str, dia, anio = m.group(1), m.group(2), m.group(3)
        mes_num = _MESES_ES.get(mes_str)
        if mes_num:
            try:
                return _date(int(anio), mes_num, int(dia))
            except ValueError:
                return None

    return None


def _cross_reference_msg(especialidad: str) -> str:
    """Retorna el mensaje de cross-reference para la especialidad, o string vacío."""
    if not especialidad:
        return ""
    return CROSS_REFERENCE.get(especialidad.strip(), "")


def _precio_line(especialidad: str, slot: dict | None = None) -> str:
    """Línea de precio para insertar en la oferta de slot.
    Retorna string vacío si la especialidad no tiene precio registrado."""
    if not especialidad:
        return ""
    esp = especialidad.strip()
    # Masoterapia: el precio depende de la duración real del slot (20 o 40 min)
    if esp.lower() == "masoterapia":
        if not slot:
            return ""
        try:
            hi = slot["hora_inicio"]
            hf = slot["hora_fin"]
            mins = (int(hf[:2]) * 60 + int(hf[3:5])) - (int(hi[:2]) * 60 + int(hi[3:5]))
        except (KeyError, ValueError, IndexError):
            return ""
        if mins >= 35:
            return "💰 Sesión 40 min: $26.990"
        return "💰 Sesión 20 min: $17.990"
    entry = PRECIOS_SLOT.get(esp)
    if not entry:
        return ""
    modalidad = entry[0]
    precio = entry[1]
    sufijo = entry[2] if len(entry) > 2 else None
    precio_str = f"${precio:,}".replace(",", ".")
    if modalidad == "fonasa":
        return f"💰 Fonasa: {precio_str}"
    # modalidad == particular
    if sufijo == "desde":
        return f"💰 Consulta: desde {precio_str}"
    if sufijo == "evaluación":
        return f"💰 Evaluación: {precio_str}"
    if sufijo == "control":
        return f"💰 Control: {precio_str}"
    return f"💰 Consulta: {precio_str}"


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
            "¿Qué necesitas hoy?"
        ),
        button_label="Ver opciones",
        sections=[
            {
                "title": "Motivos rápidos",
                "rows": [
                    {"id": "motivo_resfrio",  "title": "🤒 Resfrío o malestar"},
                    {"id": "motivo_kine",     "title": "🦴 Dolor muscular/espalda"},
                    {"id": "motivo_hta",      "title": "🫀 Control HTA/diabetes"},
                    {"id": "motivo_dental",   "title": "🦷 Revisión dental"},
                    {"id": "motivo_mg_otra",  "title": "🩺 Otra consulta médica"},
                    {"id": "motivo_otra_esp", "title": "➕ Otra especialidad"},
                ],
            },
            {
                "title": "Otras opciones",
                "rows": [
                    {"id": "accion_cambiar",   "title": "🔄 Cambiar/cancelar hora"},
                    {"id": "accion_mis_citas", "title": "📅 Mis citas / espera"},
                    {"id": "accion_recepcion", "title": "💬 Hablar con recepción"},
                ],
            },
        ],
    )


# ── Patologías válidas para comandos dx ──────────────────────────────────────
_DX_VALIDOS = {
    "dm2": "Diabetes Mellitus 2",
    "dm1": "Diabetes Mellitus 1",
    "hta": "Hipertensión Arterial",
    "asma": "Asma",
    "epoc": "EPOC",
    "hipotiroidismo": "Hipotiroidismo",
    "hipertiroidismo": "Hipertiroidismo",
    "dislipidemia": "Dislipidemia",
    "depresion": "Depresión",
    "epilepsia": "Epilepsia",
    "artrosis": "Artrosis",
    "irc": "Insuficiencia Renal Crónica",
    "erc": "Enfermedad Renal Crónica",
    "ic": "Insuficiencia Cardíaca",
    "fa": "Fibrilación Auricular",
    "gota": "Gota",
    "lupus": "Lupus",
    "ar": "Artritis Reumatoide",
    "obesidad": "Obesidad",
    "tabaquismo": "Tabaquismo",
    "oh": "OH Crónico",
    "anemia": "Anemia",
    "rinitis": "Rinitis Alérgica",
}


def _handle_doctor_dx(phone: str, txt: str) -> str:
    """Comando: dx <RUT> [patología1 patología2 ...]
    Sin patologías: muestra tags actuales. Con patologías: las agrega."""
    partes = txt.strip().split()
    if len(partes) < 2:
        return (
            "📋 *Comando dx*\n\n"
            "• `dx 12345678-9` → ver diagnósticos\n"
            "• `dx 12345678-9 dm2 hta asma` → agregar\n"
            "• `dxborrar 12345678-9 dm2` → eliminar\n\n"
            f"*Códigos válidos:*\n" +
            "\n".join(f"  `{k}` = {v}" for k, v in sorted(_DX_VALIDOS.items()))
        )

    rut = partes[1].strip().upper()
    phone_pac = get_phone_by_rut(rut)

    if not phone_pac:
        return f"❌ No encontré un paciente con RUT *{rut}* en el sistema."

    # Sin patologías → mostrar tags actuales
    if len(partes) == 2:
        tags = get_tags(phone_pac)
        dx_tags = [t for t in tags if t.startswith("dx:")]
        if not dx_tags:
            return f"ℹ️ *{rut}* no tiene diagnósticos registrados."
        lista = "\n".join(f"  • {t.replace('dx:', '').upper()}" for t in dx_tags)
        return f"📋 *Diagnósticos de {rut}:*\n{lista}"

    # Con patologías → agregar
    nuevos = []
    invalidos = []
    for dx in partes[2:]:
        dx_lower = dx.lower().strip()
        if dx_lower in _DX_VALIDOS:
            save_tag(phone_pac, f"dx:{dx_lower}")
            nuevos.append(dx_lower.upper())
        else:
            invalidos.append(dx)

    msg = ""
    if nuevos:
        msg += f"✅ Agregados a *{rut}*: {', '.join(nuevos)}"
    if invalidos:
        msg += f"\n⚠️ No reconocidos: {', '.join(invalidos)}\nEscribe `dx` para ver códigos válidos."
    return msg.strip()


def _handle_doctor_dxborrar(phone: str, txt: str) -> str:
    """Comando: dxborrar <RUT> <patología>"""
    partes = txt.strip().split()
    if len(partes) < 3:
        return "Uso: `dxborrar 12345678-9 dm2`"

    rut = partes[1].strip().upper()
    phone_pac = get_phone_by_rut(rut)
    if not phone_pac:
        return f"❌ No encontré un paciente con RUT *{rut}* en el sistema."

    eliminados = []
    for dx in partes[2:]:
        dx_lower = dx.lower().strip()
        delete_tag(phone_pac, f"dx:{dx_lower}")
        eliminados.append(dx_lower.upper())

    return f"🗑️ Eliminados de *{rut}*: {', '.join(eliminados)}"


async def _handle_doctor_paciente(rut_raw: str) -> str:
    """Comando: paciente <RUT> — ficha rápida del paciente."""
    pac = await buscar_paciente(rut_raw)
    if not pac:
        return f"❌ No encontré paciente con RUT *{rut_raw}*"

    nombre = pac["nombre"]
    rut = pac.get("rut", rut_raw)
    edad = ""
    sexo = ""
    if pac.get("fecha_nacimiento"):
        try:
            from zoneinfo import ZoneInfo
            fn = datetime.strptime(pac["fecha_nacimiento"][:10], "%Y-%m-%d").date()
            hoy = datetime.now(ZoneInfo("America/Santiago")).date()
            edad_n = hoy.year - fn.year - ((hoy.month, hoy.day) < (fn.month, fn.day))
            edad = f"{edad_n} años"
        except (ValueError, KeyError):
            pass
    if pac.get("sexo"):
        sexo = {"M": "Masculino", "F": "Femenino"}.get(pac["sexo"], pac["sexo"])

    msg = f"👤 *{nombre}*\n🪪 RUT: {rut}\n"
    if edad:
        msg += f"🎂 {edad}\n"
    if sexo:
        msg += f"⚧ {sexo}\n"

    # Tags dx
    phone_pac = get_phone_by_rut(rut)
    if phone_pac:
        tags = get_tags(phone_pac)
        dx_tags = [t for t in tags if t.startswith("dx:")]
        if dx_tags:
            msg += "\n🏷️ *Diagnósticos:*\n"
            for t in dx_tags:
                msg += f"  • {t.replace('dx:', '').upper()}\n"

    # Citas futuras
    citas = await listar_citas_paciente(pac["id"])
    if citas:
        msg += f"\n📅 *Próximas citas ({len(citas)}):*\n"
        for c in citas[:3]:
            msg += f"  • {c['fecha_display']} {c['hora_inicio']} — {c['profesional']}\n"
    else:
        msg += "\n📅 Sin citas futuras"

    return msg


async def _handle_doctor_agenda(fecha_label: str = "hoy") -> str:
    """Comando: agenda [mañana] — agenda del doctor."""
    from zoneinfo import ZoneInfo
    ahora = datetime.now(ZoneInfo("America/Santiago"))
    if fecha_label == "mañana":
        fecha = (ahora + timedelta(days=1)).strftime("%Y-%m-%d")
        titulo = f"📋 *Agenda mañana* ({(ahora + timedelta(days=1)).strftime('%d/%m')})"
    else:
        fecha = ahora.strftime("%Y-%m-%d")
        titulo = f"📋 *Agenda hoy* ({ahora.strftime('%d/%m')})"

    # Dr. Olavarría = ID 1
    agenda = await obtener_agenda_dia(1, fecha)
    if not agenda:
        return f"{titulo}\n\nSin pacientes agendados 🎉"

    msg = f"{titulo}\n{len(agenda)} pacientes\n"
    for cita in agenda:
        pac = cita["paciente"] or "Sin nombre"
        edad = f" ({cita['edad']})" if cita.get("edad") else ""
        msg += f"\n🕐 *{cita['hora']}* — {pac}{edad}"

        # Tags dx si hay
        if cita.get("rut"):
            phone_pac = get_phone_by_rut(cita["rut"])
            if phone_pac:
                tags = get_tags(phone_pac)
                dx_tags = [t.replace("dx:", "").upper() for t in tags if t.startswith("dx:")]
                if dx_tags:
                    msg += f" 🏷️{','.join(dx_tags)}"

    return msg


async def _handle_doctor_buscar(nombre: str) -> str:
    """Comando: buscar <nombre> — busca paciente por nombre."""
    if len(nombre) < 2:
        return "Escribe al menos 2 caracteres. Ej: `buscar maría gonzález`"

    resultados = await buscar_paciente_por_nombre(nombre)
    if not resultados:
        return f"❌ No encontré pacientes con *{nombre}*"

    msg = f"🔍 *Resultados para \"{nombre}\"* ({len(resultados)}):\n"
    for r in resultados:
        msg += f"\n  • *{r['nombre']}* — RUT: {r['rut']}"
    msg += "\n\nUsa `paciente <RUT>` para ver la ficha completa."
    return msg


def _doctor_mode_menu() -> dict:
    """Menú de modo para el doctor: Agente CMC (probar flujo) o Asistente Clínico."""
    return {
        "type": "button",
        "body": {"text": "Hola Rodrigo 👋 ¿Qué necesitas?"},
        "action": {
            "buttons": [
                {"type": "reply", "reply": {"id": "doc_modo_agente", "title": "🤖 Agente CMC"}},
                {"type": "reply", "reply": {"id": "doc_modo_asistente", "title": "👨‍⚕️ Asistente Clínico"}},
            ]
        },
    }


async def _handle_doctor_command(phone: str, txt: str, tl: str, data: dict, state: str) -> str | None:
    """Procesa comandos del doctor. Retorna respuesta, dict interactivo, o None para pasar al flujo normal."""

    # ── Selección de modo ──────────────────────────────────────────────────
    if tl == "doc_modo_agente":
        data["doctor_mode"] = "agente"
        save_session(phone, "IDLE", data)
        return "🤖 *Modo Agente CMC* activado. Estás en el flujo de pacientes para probar.\nEscribe *menu* para volver a elegir modo."

    if tl == "doc_modo_asistente":
        data["doctor_mode"] = "asistente"
        save_session(phone, "IDLE", data)
        return (
            "👨‍⚕️ *Asistente Clínico* activado.\n\n"
            "📋 `agenda` — tu agenda de hoy\n"
            "📋 `agenda mañana` — agenda de mañana\n"
            "👤 `paciente 12345678-9` — ficha del paciente\n"
            "🔍 `buscar María González` — buscar por nombre\n"
            "🏷️ `dx RUT dm2 hta` — agregar diagnósticos\n"
            "🗑️ `dxborrar RUT dm2` — eliminar diagnóstico\n"
            "💬 Cualquier otra cosa → pregunta clínica IA\n\n"
            "Escribe *menu* para volver a elegir modo."
        )

    # ── Si no tiene modo elegido y está en IDLE → mostrar menú de modo ────
    doctor_mode = data.get("doctor_mode")
    if not doctor_mode and state == "IDLE":
        return _doctor_mode_menu()

    # ── Modo Agente CMC → pasar al flujo normal de pacientes ──────────────
    if doctor_mode == "agente":
        return None  # None = seguir con el flujo normal de handle_message

    # ── Modo Asistente Clínico ────────────────────────────────────────────
    # dx / dxborrar
    if tl.startswith("dx ") or tl == "dx":
        return _handle_doctor_dx(phone, txt)
    if tl.startswith("dxborrar "):
        return _handle_doctor_dxborrar(phone, txt)

    # paciente <RUT>
    if tl.startswith("paciente "):
        rut_raw = txt.strip().split(maxsplit=1)[1].strip()
        return await _handle_doctor_paciente(rut_raw)

    # agenda / agenda mañana
    if tl in ("agenda", "mi agenda", "agenda hoy"):
        return await _handle_doctor_agenda("hoy")
    if tl in ("agenda mañana", "agenda manana", "mañana"):
        return await _handle_doctor_agenda("mañana")

    # buscar <nombre>
    if tl.startswith("buscar "):
        nombre = txt.strip().split(maxsplit=1)[1].strip()
        return await _handle_doctor_buscar(nombre)

    # ayuda
    if tl in ("ayuda", "help", "comandos"):
        return (
            "🩺 *Comandos disponibles:*\n\n"
            "📋 `agenda` — tu agenda de hoy\n"
            "📋 `agenda mañana` — agenda de mañana\n"
            "👤 `paciente 12345678-9` — ficha del paciente\n"
            "🔍 `buscar María González` — buscar por nombre\n"
            "🏷️ `dx 12345678-9 dm2 hta` — agregar diagnósticos\n"
            "🗑️ `dxborrar 12345678-9 dm2` — eliminar diagnóstico\n"
            "💬 Cualquier otra cosa → asistente clínico IA\n\n"
            "Escribe *menu* para volver a elegir modo."
        )

    # Cualquier otro texto → asistente clínico con Haiku
    return await consulta_clinica_doctor(txt)


async def _handle_confirmacion_precita(phone: str, tl: str, data: dict) -> str:
    """Procesa la respuesta del paciente a los botones del recordatorio de 09:00.
    IDs: cita_confirm:<id_cita> / cita_reagendar:<id_cita> / cita_cancelar:<id_cita>"""
    try:
        accion, id_cita = tl.split(":", 1)
    except ValueError:
        return "No pude procesar tu respuesta 😕 Escribe *menu* para volver al inicio."

    cita_bot = get_cita_bot_by_id_cita(id_cita, phone=phone)
    if not cita_bot:
        log_event(phone, "confirmacion_precita_notfound", {"id_cita": id_cita, "accion": accion})
        return (
            "No encontré esa cita en nuestros registros 😕\n"
            f"Llama a recepción para ayudarte: 📞 *{CMC_TELEFONO}*"
        )

    fecha = cita_bot.get("fecha", "")
    hora = (cita_bot.get("hora") or "")[:5]
    esp = cita_bot.get("especialidad", "")
    prof = cita_bot.get("profesional", "")

    # ── Confirma asistencia ───────────────────────────────────────────────────
    if accion == "cita_confirm":
        mark_cita_confirmation(id_cita, phone, "confirmed")
        log_event(phone, "cita_confirmada", {"id_cita": id_cita, "especialidad": esp})
        reset_session(phone)
        return (
            f"¡Perfecto! Tu asistencia quedó confirmada ✅\n\n"
            f"🏥 *{esp}* — {prof}\n"
            f"🕐 *{hora}*\n\n"
            "Te esperamos *15 minutos antes* con tu cédula de identidad.\n\n"
            f"📍 Monsalve 102, Carampangue\n\n"
            "_Si cambian tus planes, escríbenos para reagendar._"
        )

    # ── Quiere cambiar la hora (reagendar) ────────────────────────────────────
    if accion == "cita_reagendar":
        mark_cita_confirmation(id_cita, phone, "reagendar")
        log_event(phone, "cita_reagendar_solicitado", {"id_cita": id_cita, "especialidad": esp})
        esp_lower = (esp or "").lower()
        if not esp_lower:
            return (
                "No pude identificar la especialidad de esa cita 😕\n"
                f"Llama a recepción: 📞 *{CMC_TELEFONO}*"
            )
        # Construir la cita "vieja" mínima para reagendar sin pedir RUT
        cita_old = {
            "id": id_cita,
            "especialidad": esp,
            "profesional": prof,
            "fecha": fecha,
            "fecha_display": fecha,
            "hora_inicio": hora,
        }
        data = dict(data or {})
        data["cita_old"] = cita_old
        data["reagendar_mode"] = True
        perfil = get_profile(phone)
        if perfil:
            data["rut_conocido"] = perfil["rut"]
            data["nombre_conocido"] = perfil["nombre"]
        return await _iniciar_agendar(phone, data, esp_lower)

    # ── No podrá ir (cancela) ─────────────────────────────────────────────────
    if accion == "cita_cancelar":
        mark_cita_confirmation(id_cita, phone, "cancelar")
        log_event(phone, "cita_cancelar_solicitado", {"id_cita": id_cita, "especialidad": esp})
        # Carga la cita directamente en CONFIRMING_CANCEL (sin pedir RUT)
        cita_cancelar = {
            "id": id_cita,
            "especialidad": esp,
            "profesional": prof,
            "fecha": fecha,
            "fecha_display": fecha,
            "hora_inicio": hora,
        }
        data = dict(data or {})
        data["cita_cancelar"] = cita_cancelar
        save_session(phone, "CONFIRMING_CANCEL", data)
        return _btn_msg(
            f"Entendido 😕 Vamos a cancelar esta hora:\n\n"
            f"🏥 {prof}\n"
            f"📅 {fecha}\n"
            f"🕐 {hora}\n\n"
            "¿Confirmas la cancelación?",
            [
                {"id": "si", "title": "✅ Sí, cancelar"},
                {"id": "no", "title": "❌ No, mantener"},
            ]
        )

    return "No pude procesar tu respuesta 😕 Escribe *menu* para volver al inicio."


async def handle_message(phone: str, texto: str, session: dict) -> str:
    state = session["state"]
    data  = session["data"]
    txt   = texto.strip()
    tl    = txt.lower()
    # tl_norm = texto del paciente normalizado léxicamente (sin tildes,
    # abreviaciones WhatsApp expandidas, typos frecuentes corregidos,
    # participios rurales arreglados). Lo usamos en los matches hard-coded
    # (emergencias, comandos globales, afirmaciones, negaciones, arauco) para
    # ganar recall con mensajes como "tngo dlor d pcho" o "sangrao mucho".
    # OJO: mantenemos `tl`/`txt` para parseos estrictos (RUT, números, IDs de
    # botón `cat_medico`/`cita_confirm:*`, selección de slot, captura de
    # nombre) y para pasarle a `detect_intent` el texto original.
    tl_norm = normalizar_texto_paciente(txt)

    # ── Confirmación pre-cita (respuesta al recordatorio de 09:00) ────────────
    # Los botones del recordatorio llegan con ID "cita_confirm:<id>", etc.
    # Debe ir ANTES de emergencias y comandos globales para que siempre se procese.
    if tl.startswith(("cita_confirm:", "cita_reagendar:", "cita_cancelar:")):
        return await _handle_confirmacion_precita(phone, tl, data)

    # ── Comandos del doctor (solo desde su número) ──────────────────────────
    _doctor_phone = CMC_TELEFONO.replace("+", "").replace(" ", "")
    if phone == _doctor_phone:
        resp = await _handle_doctor_command(phone, txt, tl, data, state)
        if resp is not None:
            return resp

    # ── Crisis de salud mental (prioridad 1) ─────────────────────────────────
    # Ideación suicida merece un mensaje diferenciado con tono de contención
    # + Salud Responde 600 360 7777 además de SAMU 131. Va ANTES que
    # emergencias físicas porque "me quiero morir" y "me quiero matar" no son
    # amenaza vital física sino crisis de salud mental.
    if (any(p in tl_norm for p in SALUD_MENTAL_CRISIS)
            or any(pat.search(tl_norm) for pat in SALUD_MENTAL_PATRONES)
            or any(p in tl for p in SALUD_MENTAL_CRISIS)
            or any(pat.search(tl) for pat in SALUD_MENTAL_PATRONES)):
        save_tag(phone, "crisis-salud-mental")
        log_event(phone, "crisis_salud_mental", {"texto": txt[:240]})
        reset_session(phone)
        return (
            "Lamento mucho lo que estás sintiendo 💙 Lo que me cuentas es muy "
            "importante y no estás solo/a.\n\n"
            "Por favor, habla ahora con alguien que pueda ayudarte:\n\n"
            "🆘 *Salud Responde*: 600 360 7777 (24 h, atención en crisis)\n"
            "🚑 *SAMU*: 131 (emergencias)\n"
            f"📞 *CMC*: {CMC_TELEFONO}\n\n"
            "Si puedes, acércate a un familiar, vecino o persona de confianza "
            "mientras llamas. Buscar ayuda es un acto de valentía 💙"
        )

    # ── Emergencias físicas (prioridad 2) ─────────────────────────────────────
    # Usamos tl_norm para capturar variantes abreviadas ("dlor fuerte d pcho"),
    # y tl como fallback por si la normalización rompe algún match existente.
    # `EMERGENCIAS_VITAL_PATRONES` tiene lookahead negativo para excluir
    # colloquialismos como "me muero de hambre/risa/sed".
    if (any(p in tl_norm for p in EMERGENCIAS)
            or any(pat.search(tl_norm) for pat in EMERGENCIAS_PATRONES)
            or any(pat.search(tl_norm) for pat in EMERGENCIAS_VITAL_PATRONES)
            or any(p in tl for p in EMERGENCIAS)
            or any(pat.search(tl) for pat in EMERGENCIAS_PATRONES)
            or any(pat.search(tl) for pat in EMERGENCIAS_VITAL_PATRONES)):
        log_event(phone, "emergencia_detectada", {"texto": txt[:240]})
        reset_session(phone)
        return (
            "⚠️ Esto suena como una urgencia.\n\n"
            "Llama al *SAMU 131* o acude al servicio de urgencias más cercano ahora mismo.\n\n"
            f"También puedes contactarnos:\n📞 *{CMC_TELEFONO}*\n☎️ *{CMC_TELEFONO_FIJO}*\n\n"
            "Si necesitas algo más, escribe *menú*."
        )

    # ── Comandos globales ─────────────────────────────────────────────────────
    _COMANDOS_GLOBALES = ("menu", "menú", "inicio", "reiniciar", "volver", "hola")
    if tl in _COMANDOS_GLOBALES or tl_norm in _COMANDOS_GLOBALES:
        reset_session(phone)
        # Doctor: "menu" lo lleva al selector de modo (no al menú de pacientes)
        if phone == _doctor_phone:
            return _doctor_mode_menu()
        return _menu_msg()

    # ── Detección pasiva de Arauco (guarda tag sin interrumpir el flujo) ──────
    if "arauco" in tl_norm:
        save_tag(phone, "arauco")

    # ── Detección pasiva de patologías crónicas ────────────────────────────────
    _PATOLOGIAS_KEYWORDS = {
        "dm2":  ["diabete", "diabetico", "diabetica", "diabetes", "insulina", "glicemia alta", "azucar alta", "azucar en la sangre"],
        "hta":  ["hipertens", "presion alta", "presión alta", "hipertenso", "hipertensa", "antihipertensivo"],
        "asma": ["asma", "asmatico", "asmatica", "inhalador", "salbutamol", "broncodilatador"],
        "epoc": ["epoc", "enfisema", "bronquitis cronica"],
        "hipotiroidismo": ["hipotiroid", "levotiroxina", "eutirox", "tiroides baja"],
        "dislipidemia": ["colesterol alto", "trigliceridos alto", "dislipidemia", "estatina", "atorvastatina"],
        "depresion": ["depresion", "antidepresivo", "sertralina", "fluoxetina", "escitalopram"],
        "epilepsia": ["epilepsia", "epileptico", "convulsion", "anticonvulsivante"],
        "artrosis": ["artrosis", "desgaste articular", "osteoartrosis"],
        "irc": ["insuficiencia renal", "dialisis", "hemodialisis"],
    }
    for tag, keywords in _PATOLOGIAS_KEYWORDS.items():
        if any(kw in tl_norm for kw in keywords):
            save_tag(phone, f"dx:{tag}")

    # ── IDLE: detectar intención ──────────────────────────────────────────────
    if state == "IDLE":
        # ── Seguimiento de FAQ con sugerencia de agendar ──────────────────────
        # Debe ir ANTES de los atajos numéricos (1..4) porque aquí interpretamos
        # "1"/"sí"/botón como "agendar la especialidad ya sugerida en el FAQ".
        esp_sug_prev = data.get("especialidad_sugerida")
        if esp_sug_prev:
            if tl == "no_agendar" or tl in NEGACIONES or tl_norm in NEGACIONES:
                data.pop("especialidad_sugerida", None)
                save_session(phone, "IDLE", data)
                log_event(phone, "faq_agendar_rechazo", {"esp": esp_sug_prev})
                return (
                    "Sin problema 😊 Cuando lo necesites, estamos acá.\n"
                    "_Escribe *menu* para ver todas las opciones._"
                )
            if tl == "agendar_sugerido" or txt == "1" or tl in AFIRMACIONES or tl_norm in AFIRMACIONES:
                data.pop("especialidad_sugerida", None)
                log_event(phone, "faq_agendar_acepto", {"esp": esp_sug_prev})
                perfil = get_profile(phone)
                if perfil:
                    data["rut_conocido"] = perfil["rut"]
                    data["nombre_conocido"] = perfil["nombre"]
                return await _iniciar_agendar(phone, data, esp_sug_prev)
            # Cualquier otro mensaje: limpiamos la sugerencia y seguimos el flujo
            # normal para no atrapar al paciente.
            data.pop("especialidad_sugerida", None)
            save_session(phone, "IDLE", data)

        # Atajos numéricos del menú (compatibilidad + sub-menús "Cambiar/cancelar"
        # y "Mis citas / espera" que devuelven botones con estos IDs)
        if txt == "1": return await _iniciar_agendar(phone, data, None)
        if txt == "2": return await _iniciar_reagendar(phone, data)
        if txt == "3": return await _iniciar_cancelar(phone, data)
        if txt == "4": return await _iniciar_ver(phone, data)
        if txt == "5": return await _iniciar_waitlist(phone, data, None)
        if txt == "6": return _derivar_humano(phone=phone, contexto="menú opción 6")

        # ── Motivos rápidos del menú ──────────────────────────────────────────
        # Cada motivo → ruta directa a _iniciar_agendar con la especialidad
        # preseleccionada + saludo prefix ("pausa" estilo 5A: una línea de
        # reconocimiento antes de mostrar el slot, todo en un solo mensaje).
        # HTA/diabetes rutea a MG por ahora (la priorización de slots matinales
        # para crónicos es un feature aparte — palanca 1 del plan estratégico).
        _MOTIVOS = {
            "motivo_resfrio":  ("medicina general", "🤒", "Medicina General"),
            "motivo_kine":     ("kinesiología",     "🦴", "Kinesiología"),
            "motivo_hta":      ("medicina general", "🫀", "Medicina General"),
            "motivo_dental":   ("odontología",      "🦷", "Odontología"),
            "motivo_mg_otra":  ("medicina general", "🩺", "Medicina General"),
        }
        if tl in _MOTIVOS:
            esp, emoji, label = _MOTIVOS[tl]
            prefix = f"{emoji} *Perfecto, te agendo con {label}*\n\n"
            log_event(phone, "motivo_seleccionado", {"motivo": tl, "especialidad": esp})
            return await _iniciar_agendar(phone, data, esp, saludo_prefix=prefix)
        if tl == "motivo_otra_esp":
            log_event(phone, "motivo_seleccionado", {"motivo": "otra_esp"})
            return await _iniciar_agendar(phone, data, None)

        # ── Sub-menús de "Otras opciones" ─────────────────────────────────────
        # Los botones del sub-menú usan los mismos IDs numéricos que los atajos
        # (txt == "2"/"3"/"4"/"5") — arriba ya están enrutados, acá solo
        # mostramos el sub-menú al tocar la entrada agrupada.
        if tl == "accion_cambiar":
            return _btn_msg(
                "¿Qué necesitas hacer con tu hora?",
                [
                    {"id": "2", "title": "🔄 Reagendar"},
                    {"id": "3", "title": "❌ Cancelar"},
                ]
            )
        if tl == "accion_mis_citas":
            return _btn_msg(
                "¿Qué quieres ver?",
                [
                    {"id": "4", "title": "📅 Mis reservas"},
                    {"id": "5", "title": "⏰ Lista de espera"},
                ]
            )
        if tl == "accion_recepcion":
            return _derivar_humano(phone=phone, contexto="menú recepción")

        # ── Respuestas de fidelización ────────────────────────────────────────
        if tl == "seg_mejor":
            # IMPORTANTE: obtener seguimiento ANTES de guardar respuesta
            # (get_ultimo_seguimiento busca respuesta IS NULL)
            seg = get_ultimo_seguimiento(phone)
            save_fidelizacion_respuesta(phone, "postconsulta", "mejor")
            esp = seg.get("especialidad", "") if seg else ""
            log_event(phone, "seguimiento_mejor", {"especialidad": esp})
            # Cross-sell inteligente según especialidad
            upsell = UPSELL_POSTCONSULTA.get(esp.lower()) if esp else None
            if upsell:
                upsell_msg, upsell_esp = upsell
                data["upsell_especialidad"] = upsell_esp
                save_session(phone, "IDLE", data)
                log_event(phone, "upsell_postconsulta_ofrecido",
                          {"especialidad_origen": esp, "especialidad_destino": upsell_esp})
                return _btn_msg(
                    f"Qué bueno saberlo 😊 Nos alegra que te sientas mejor.\n\n{upsell_msg}",
                    [{"id": "upsell_si", "title": "Sí, me interesa"},
                     {"id": "no_control", "title": "No por ahora"}]
                )
            return _btn_msg(
                "Qué bueno saberlo 😊 Nos alegra que te sientas mejor.\n\n"
                "¿Quieres agendar tu control de seguimiento?",
                [{"id": "1", "title": "Sí, agendar control"},
                 {"id": "no_control", "title": "Por ahora no"}]
            )
        if tl in ("seg_igual", "seg_peor"):
            seg = get_ultimo_seguimiento(phone)
            save_fidelizacion_respuesta(phone, "postconsulta", tl.replace("seg_", ""))
            esp = seg.get("especialidad", "") if seg else ""
            prof = seg.get("profesional", "") if seg else ""
            log_event(phone, "seguimiento_negativo", {"respuesta": tl, "especialidad": esp})
            # Si responde PEOR, alertar al doctor
            if tl == "seg_peor" and ADMIN_ALERT_PHONE:
                perfil = get_profile(phone)
                nombre_pac = perfil["nombre"] if perfil else phone
                alerta = (
                    f"⚠️ *Alerta seguimiento*\n\n"
                    f"Paciente *{nombre_pac}* ({phone}) reporta sentirse *PEOR* "
                    f"después de {esp} con {prof}.\n"
                    f"Revisar situación clínica."
                )
                log_event(phone, "seguimiento_alerta_peor",
                          {"especialidad": esp, "profesional": prof})
                try:
                    asyncio.create_task(send_whatsapp(ADMIN_ALERT_PHONE, alerta))
                except Exception:
                    log.warning("No se pudo enviar alerta peor a %s", ADMIN_ALERT_PHONE)
            return _btn_msg(
                "Lamentamos escuchar eso 😟\n\n"
                f"¿Quieres reagendar una consulta{' con ' + prof if prof else ''}?",
                [{"id": "2", "title": "Sí, reagendar"},
                 {"id": "no_control", "title": "No por ahora"}]
            )
        if tl == "upsell_si":
            upsell_esp = data.pop("upsell_especialidad", None)
            log_event(phone, "upsell_postconsulta_acepto", {"especialidad": upsell_esp})
            perfil = get_profile(phone)
            if perfil:
                data["rut_conocido"] = perfil["rut"]
                data["nombre_conocido"] = perfil["nombre"]
            return await _iniciar_agendar(phone, data, upsell_esp)
        if tl == "no_control":
            data.pop("upsell_especialidad", None)
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
                    upsell = UPSELL_POSTCONSULTA.get(esp.lower()) if esp else None
                    if upsell:
                        upsell_msg, upsell_esp = upsell
                        data["upsell_especialidad"] = upsell_esp
                        save_session(phone, "IDLE", data)
                        log_event(phone, "upsell_postconsulta_ofrecido",
                                  {"especialidad_origen": esp, "especialidad_destino": upsell_esp,
                                   "fuente": "texto_libre"})
                        return _btn_msg(
                            f"Qué bueno saberlo 😊 Nos alegra que te sientas mejor.\n\n{upsell_msg}",
                            [{"id": "upsell_si", "title": "Sí, me interesa"},
                             {"id": "no_control", "title": "No por ahora"}]
                        )
                    return _btn_msg(
                        "Qué bueno saberlo 😊 Nos alegra que te sientas mejor.\n\n"
                        "¿Quieres agendar tu control de seguimiento?",
                        [{"id": "1", "title": "Sí, agendar control"},
                         {"id": "no_control", "title": "Por ahora no"}]
                    )
                else:  # igual o peor
                    log_event(phone, "seguimiento_negativo",
                              {"respuesta": clasificacion, "especialidad": esp, "fuente": "texto_libre"})
                    if clasificacion == "peor" and ADMIN_ALERT_PHONE:
                        perfil = get_profile(phone)
                        nombre_pac = perfil["nombre"] if perfil else phone
                        alerta = (
                            f"⚠️ *Alerta seguimiento*\n\n"
                            f"Paciente *{nombre_pac}* ({phone}) reporta sentirse *PEOR* "
                            f"después de {esp} con {prof}.\n"
                            f"Revisar situación clínica."
                        )
                        log_event(phone, "seguimiento_alerta_peor",
                                  {"especialidad": esp, "profesional": prof, "fuente": "texto_libre"})
                        try:
                            asyncio.create_task(send_whatsapp(ADMIN_ALERT_PHONE, alerta))
                        except Exception:
                            log.warning("No se pudo enviar alerta peor a %s", ADMIN_ALERT_PHONE)
                    return _btn_msg(
                        "Lamentamos escuchar eso 😟\n\n"
                        f"¿Quieres reagendar una consulta{' con ' + prof if prof else ''}?",
                        [{"id": "2", "title": "Sí, reagendar"},
                         {"id": "no_control", "title": "No por ahora"}]
                    )

        # ── Pre-triage por síntomas (GES Clinical Assistant) ─────────────────
        # Orden de prioridad en handle_message (NO mover sin coordinar con equipo
        # clínico del CMC):
        #   1. Emergencias hard-coded (EMERGENCIAS + regex) — síntomas obvios
        #      que no dependen del motor GES, siempre ganan.
        #   2. Comandos globales (menu/hola/...) — el paciente quiere reiniciar.
        #   3. Pre-triage GES (este bloque) — consulta motor clínico y puede
        #      derivar a SAMU, HOSPITAL o agendar según hipótesis.
        #   4. detect_intent() con Claude — fallback general.
        #
        # Responsabilidad clínica: los mensajes al paciente NO nombran la
        # patología sospechada (ej. "posible IAM") — eso es territorio médico
        # y puede alarmar sin información diagnóstica real. La patología se
        # registra en log_event para auditoría interna y revisión posterior.
        if len(txt) >= 10 and not txt.isdigit():
            _t0 = time.monotonic()
            triage = await triage_sintomas(txt)
            _elapsed_ms = int((time.monotonic() - _t0) * 1000)
            if triage:
                log_event(phone, "triage_ges_match", {
                    "top": triage.get("top_pathology"),
                    "score": triage.get("top_score"),
                    "especialidad": triage.get("especialidad"),
                    "urgency": triage.get("needs_urgency"),
                    "elapsed_ms": _elapsed_ms,
                })
                # Urgencia tiempo-dependiente → derivar a SAMU inmediatamente.
                # NO nombramos la patología al paciente (responsabilidad clínica).
                if triage.get("needs_urgency"):
                    save_tag(phone, "triage-urgencia")
                    return (
                        "⚠️ Lo que describes puede requerir atención médica urgente.\n\n"
                        "Por favor, llama al *SAMU 131* o acude al servicio de "
                        "urgencias más cercano ahora mismo.\n\n"
                        f"También puedes contactarnos:\n📞 *{CMC_TELEFONO}*\n"
                        f"☎️ *{CMC_TELEFONO_FIJO}*\n\n"
                        + DISCLAIMER
                    )
                # Patología derivada a hospital → no se atiende en el CMC.
                # Tampoco nombramos la patología; decimos "atención de mayor
                # complejidad" para no alarmar ni dar un diagnóstico indirecto.
                if triage.get("ges_specialty_raw") == "HOSPITAL":
                    save_tag(phone, "triage-hospital")
                    return (
                        "Lo que describes podría requerir atención de mayor "
                        "complejidad que no realizamos en el Centro Médico "
                        "Carampangue.\n\n"
                        "Te recomiendo acudir a tu consultorio de referencia o al "
                        "hospital base para una evaluación.\n\n"
                        f"Si necesitas orientación, llama a recepción:\n📞 *{CMC_TELEFONO}*\n\n"
                        + DISCLAIMER
                    )
                # Especialidad agendable → iniciar flujo de agendar directo.
                especialidad_triage = triage.get("especialidad")
                if especialidad_triage:
                    perfil = get_profile(phone)
                    if perfil:
                        data["rut_conocido"] = perfil["rut"]
                        data["nombre_conocido"] = perfil["nombre"]
                    data["triage_motivo"] = triage.get("top_pathology")
                    return await _iniciar_agendar(phone, data, especialidad_triage)
            else:
                # Log de gaps de recall — sólo si el texto parece clínico. Así
                # evitamos llenar el event stream con "hola, cómo están" y
                # mantenemos un corpus limpio para revisar semanalmente qué
                # frases sintomáticas no están capturadas por el motor GES.
                if _SENALES_SINTOMA.search(txt):
                    log_event(phone, "triage_ges_nomatch", {
                        "texto": txt[:240],
                        "elapsed_ms": _elapsed_ms,
                    })

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

        if intent == "reagendar":
            return await _iniciar_reagendar(phone, data)

        if intent == "cancelar":
            return await _iniciar_cancelar(phone, data)

        if intent == "ver_reservas":
            return await _iniciar_ver(phone, data)

        if intent == "waitlist":
            especialidad = result.get("especialidad")
            return await _iniciar_waitlist(phone, data, especialidad)

        if intent == "humano":
            # Override defensivo: Claude Haiku ocasionalmente clasifica
            # frases con carga clínica/vital como "humano" cuando deberían ser
            # emergencia. Las capas anteriores (SALUD_MENTAL_CRISIS + EMERGENCIAS)
            # ya filtran lo obvio, pero si por alguna combinación rara algo se
            # coló hasta acá, reroutear antes de mandar al paciente a recepción.
            _DANGER_KW = (
                "morir", "muero", "muerte", "super mal", "súper mal",
                "muy mal", "muy grave", "estoy grave", "desmay",
                "convuls", "ahogo", "no puedo respir", "sangre",
                "dolor fuerte", "dolor muy fuerte",
            )
            if any(kw in tl_norm for kw in _DANGER_KW) or any(kw in tl for kw in _DANGER_KW):
                log_event(phone, "humano_override_emergencia", {"texto": txt[:240]})
                return (
                    "⚠️ Lo que describes puede requerir atención urgente.\n\n"
                    "Por favor, llama al *SAMU 131* o acude al servicio de "
                    "urgencias más cercano ahora mismo.\n\n"
                    f"También puedes contactarnos:\n📞 *{CMC_TELEFONO}*\n"
                    f"☎️ *{CMC_TELEFONO_FIJO}*"
                )
            return _derivar_humano(phone=phone, contexto=txt)

        if intent == "disponibilidad":
            if is_medilink_down():
                return _modo_degradado(phone, "disponibilidad", result.get("especialidad") or "")
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
            esp_sug = (result.get("especialidad") or "").strip()
            # Si Claude infirió una especialidad, intentamos mostrar el próximo slot
            # inline + botón para agendar directo.
            if esp_sug and not is_medilink_down():
                try:
                    esp_lower = esp_sug.lower()
                    if esp_lower in _ESP_MED_GENERAL:
                        _smart, _todos = await buscar_primer_dia(esp_lower, solo_ids=_MED_AO_IDS)
                        mejor = _todos[0] if _todos else None
                    elif esp_lower in ("masoterapia", "masaje", "masajes"):
                        # Masoterapia requiere preguntar duración: no pre-lookup.
                        mejor = None
                    else:
                        _smart, _todos = await buscar_primer_dia(esp_lower)
                        mejor = (_smart[0] if _smart else (_todos[0] if _todos else None))
                except Exception as e:
                    log_event(phone, "faq_slot_lookup_error", {"esp": esp_sug, "error": str(e)[:200]})
                    mejor = None

                if mejor:
                    data["especialidad_sugerida"] = esp_lower
                    save_session(phone, "IDLE", data)
                    preview = (
                        f"📅 *{mejor['fecha_display']}* · "
                        f"🕐 *{mejor['hora_inicio'][:5]}* · "
                        f"{mejor['profesional']}"
                    )
                    return _btn_msg(
                        f"{resp}\n\n"
                        f"Próxima hora disponible en *{esp_sug}*:\n{preview}\n\n"
                        f"{DISCLAIMER}\n\n"
                        "¿Quieres agendar?",
                        [
                            {"id": "agendar_sugerido", "title": "✅ Agendar ahora"},
                            {"id": "no_agendar",      "title": "No por ahora"},
                        ]
                    )
                # Fallback: guardamos la especialidad igual para que "sí" funcione
                if esp_lower:
                    data["especialidad_sugerida"] = esp_lower
                    save_session(phone, "IDLE", data)
                    return _btn_msg(
                        f"{resp}\n\n{DISCLAIMER}\n\n¿Quieres agendar en *{esp_sug}*?",
                        [
                            {"id": "agendar_sugerido", "title": "✅ Sí, agendar"},
                            {"id": "no_agendar",      "title": "No por ahora"},
                        ]
                    )
            return (
                f"{resp}\n\n"
                f"{DISCLAIMER}\n\n"
                "¿Quieres agendar una hora? Escribe *1* o *menu* para volver."
            )

        # intent "otro" — si Claude produjo una respuesta útil (p.ej. una
        # emergencia que se filtró del detector léxico), la mostramos con
        # el disclaimer y NO derivamos a recepción como si fuera un trámite.
        resp_otro = (result.get("respuesta_directa") or "").strip()
        if resp_otro:
            return f"{resp_otro}\n\n{DISCLAIMER}"
        # Fallback final (saludo o input incomprensible) → mostrar menú
        return _menu_msg()

    # ── WAIT_DURACION_MASOTERAPIA ──────────────────────────────────────────────
    if state == "WAIT_DURACION_MASOTERAPIA":
        # Matchear palabra exacta, no substring (evita que "200" o "2040" entren)
        num = re.findall(r"\b(20|40)\b", txt)
        if tl == "maso_20" or (num and num[0] == "20"):
            duracion_maso = 20
        elif tl == "maso_40" or (num and num[0] == "40"):
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
        mejor = smart[0]
        prof_sugerido_id = mejor.get("id_profesional")
        data.update({"especialidad": "masoterapia", "slots": smart,
                     "todos_slots": todos, "fechas_vistas": [fecha],
                     "expansion_stage": 0, "prof_sugerido_id": prof_sugerido_id})
        save_session(phone, "WAIT_SLOT", data)
        precio_linea = _precio_line("Masoterapia", mejor)
        precio_bloque = f"{precio_linea}\n" if precio_linea else ""
        return _btn_msg(
            f"Encontré disponibilidad ✨\n\n"
            f"🏥 *Masoterapia* — {mejor['profesional']}\n"
            f"📅 *{mejor['fecha_display']}*\n"
            f"🕐 *{mejor['hora_inicio'][:5]}* ({duracion_maso} min) ⭐\n"
            f"{precio_bloque}\n"
            "¿La agendo?",
            [
                {"id": "confirmar_sugerido", "title": "✅ Sí, esa hora"},
                {"id": "ver_otros",          "title": "📋 Otros horarios"},
                {"id": "otro_dia",           "title": "📅 Otro día"},
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
        # Si venimos del flujo de lista de espera, redirigir al confirming
        if data.pop("from_waitlist", False):
            return await _iniciar_waitlist(phone, data, especialidad_candidata)
        return await _iniciar_agendar(phone, data, especialidad_candidata)

    # ── WAIT_SLOT ─────────────────────────────────────────────────────────────
    if state == "WAIT_SLOT":
        slots_mostrados = data.get("slots", [])          # los que ve el paciente ahora
        todos_slots     = data.get("todos_slots", slots_mostrados)  # todos del día
        fechas_vistas   = data.get("fechas_vistas", [])
        especialidad    = data.get("especialidad", "")
        fecha_actual    = todos_slots[0]["fecha"] if todos_slots else None

        # Respuesta al sugerido proactivo (botón o texto libre "si"/"sí"/"confirmo"/...)
        if (tl == "confirmar_sugerido" or tl in AFIRMACIONES or tl_norm in AFIRMACIONES) and slots_mostrados:
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

        # "Otro profesional" → muestra slots del/los otro(s) doctor(es) de la especialidad
        if tl == "otro_prof":
            from medilink import _ids_para_especialidad
            prof_sugerido_id = data.get("prof_sugerido_id")
            ids_esp = _ids_para_especialidad(especialidad)
            if especialidad in _ESP_MED_GENERAL:
                ids_esp = list(_MED_GENERAL_IDS)
            otros_ids = [i for i in ids_esp if i != prof_sugerido_id]
            if not otros_ids:
                return "Solo hay un profesional disponible para esta especialidad 😊"

            # 1) Intentar con los slots que ya tenemos del mismo día (todos_slots)
            slots_otros_mismo_dia = [s for s in todos_slots if s.get("id_profesional") in otros_ids]
            if slots_otros_mismo_dia:
                data["slots"] = slots_otros_mismo_dia
                # Marcar al primer "otro" como el nuevo sugerido para seguir navegando
                nuevo_sugerido_id = slots_otros_mismo_dia[0].get("id_profesional")
                data["prof_sugerido_id"] = nuevo_sugerido_id
                save_session(phone, "WAIT_SLOT", data)
                return _format_slots(slots_otros_mismo_dia, mostrar_todos=True)

            # 2) No hay cupo de los otros en ese día → buscar su próximo día disponible
            _maso_override = {59: data["maso_duracion"]} if especialidad == "masoterapia" and data.get("maso_duracion") else None
            smart_nuevo, todos_nuevo = await buscar_primer_dia(
                especialidad, excluir=fechas_vistas,
                solo_ids=otros_ids, intervalo_override=_maso_override)
            if not todos_nuevo:
                return (
                    "No encontré disponibilidad con otros profesionales en los próximos días 😕\n\n"
                    "Escribe *otro día* para seguir buscando con el mismo doctor, "
                    f"o llama a recepción: 📞 *{CMC_TELEFONO}*"
                )
            nueva_fecha = todos_nuevo[0]["fecha"]
            if nueva_fecha not in fechas_vistas:
                fechas_vistas = fechas_vistas + [nueva_fecha]
            nuevo_sugerido_id = todos_nuevo[0].get("id_profesional")
            smart_nuevo_filtrado = [s for s in smart_nuevo if s.get("id_profesional") == nuevo_sugerido_id] or smart_nuevo
            data.update({"slots": smart_nuevo_filtrado, "todos_slots": todos_nuevo,
                         "fechas_vistas": fechas_vistas, "expansion_stage": 0,
                         "prof_sugerido_id": nuevo_sugerido_id})
            save_session(phone, "WAIT_SLOT", data)
            return _format_slots(smart_nuevo_filtrado)

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
            if especialidad in _ESP_MED_GENERAL:
                smart_nuevo, todos_nuevo = await buscar_primer_dia(
                    especialidad, excluir=fechas_vistas, solo_ids=_MED_AO_IDS)
                if not todos_nuevo:  # overflow a Márquez
                    smart_nuevo, todos_nuevo = await buscar_primer_dia(
                        especialidad, excluir=fechas_vistas, solo_ids=[_MED_OVERFLOW_ID])
            else:
                smart_nuevo, todos_nuevo = await buscar_primer_dia(
                    especialidad, excluir=fechas_vistas, intervalo_override=_maso_override)
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
                    # Refrescar sesión para mantener el flujo vivo y que el panel
                    # muestre esta conversación como "activa"
                    save_session(phone, "WAIT_SLOT", data)
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
        if tl in FONASA or tl_norm in FONASA:
            data["modalidad"] = "fonasa"
        elif tl in PARTICULAR or tl_norm in PARTICULAR:
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
        _SET_CONTINUAR = AFIRMACIONES | {"si", "sí", "ok", "mismo", "el mismo"}
        if rut_conocido and (tl in _SET_CONTINUAR or tl_norm in _SET_CONTINUAR) and tl != "rut_nuevo":
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
        if tl in AFIRMACIONES or tl_norm in AFIRMACIONES:
            slot    = data["slot_elegido"]
            paciente = data["paciente"]
            reagendar = bool(data.get("reagendar_mode"))
            cita_old = data.get("cita_old") or {}
            resultado = await crear_cita(
                id_paciente=paciente["id"],
                id_profesional=slot["id_profesional"],
                fecha=slot["fecha"],
                hora_inicio=slot["hora_inicio"],
                hora_fin=slot["hora_fin"],
                id_recurso=slot.get("id_recurso", 1),
            )
            # Si estamos en reagendar, cancelamos la anterior SOLO si la nueva
            # se creó bien. Si falla la nueva, la vieja queda intacta.
            cancel_ok = False
            if resultado and reagendar and cita_old.get("id"):
                cancel_ok = await cancelar_cita(cita_old["id"])
                if not cancel_ok:
                    log_event(phone, "reagendar_cancel_old_fail",
                              {"id_cita_old": cita_old.get("id"),
                               "id_cita_new": resultado.get("id")})
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
                log_event(phone, "cita_reagendada" if reagendar else "cita_creada", {
                    "especialidad": esp,
                    "profesional": slot["profesional"],
                    "fecha": slot["fecha"],
                    "modalidad": data.get("modalidad", "particular"),
                    "id_cita_old": cita_old.get("id") if reagendar else None,
                })
                cross_ref = _cross_reference_msg(esp)
                # Recordatorio PNI para pacientes pediátricos
                fecha_nac = (data.get("reg_fecha_nacimiento")
                             or paciente.get("fecha_nacimiento", ""))
                pni_msg = ""
                if fecha_nac:
                    _pni = get_vaccine_reminder(fecha_nac, paciente["nombre"])
                    if _pni:
                        pni_msg = f"\n\n{_pni}"
                if reagendar:
                    extra = ""
                    if not cancel_ok:
                        extra = (
                            "\n\n⚠️ _Tuvimos un inconveniente cancelando la hora anterior; "
                            "recepción la anulará de forma manual. No hay problema._"
                        )
                    return (
                        f"🔄 *¡Listo, {nombre_corto}! Tu hora fue reagendada.*\n\n"
                        f"👤 {paciente['nombre']}\n"
                        f"🏥 {slot['especialidad']} — {slot['profesional']}\n"
                        f"📅 {slot['fecha_display']}\n"
                        f"🕐 {slot['hora_inicio'][:5]}\n\n"
                        "Recuerda llegar *15 minutos antes* con tu cédula de identidad.\n\n"
                        "📍 *Monsalve 102 esq. República, Carampangue*"
                        f"{extra}"
                        f"{cross_ref}"
                        f"{pni_msg}\n\n"
                        "_Escribe *menu* si necesitas algo más._"
                    )
                return (
                    f"✅ *¡Listo, {nombre_corto}! Tu hora quedó reservada.*\n\n"
                    f"👤 {paciente['nombre']}\n"
                    f"🏥 {slot['especialidad']} — {slot['profesional']}\n"
                    f"📅 {slot['fecha_display']}\n"
                    f"🕐 {slot['hora_inicio'][:5]}\n"
                    f"💳 {modalidad}\n\n"
                    "Recuerda llegar *15 minutos antes* con tu cédula de identidad.\n\n"
                    "📍 *Monsalve 102 esq. República, Carampangue*\n\n"
                    f"¡Te esperamos! 😊{cross_ref}"
                    f"{pni_msg}\n\n"
                    "_Escribe *menu* si necesitas algo más._"
                )
            else:
                return (
                    "Hubo un problema al reservar la hora 😕\n"
                    f"Llama a recepción: 📞 *{CMC_TELEFONO}*"
                )

        if tl in NEGACIONES or tl_norm in NEGACIONES:
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
        _SET_SALIR = {"menu", "menú", "salir", "atras", "atrás"}
        if (tl in NEGACIONES or tl_norm in NEGACIONES
                or tl in _SET_SALIR or tl_norm in _SET_SALIR):
            reset_session(phone)
            return "Perfecto, no cancelamos nada 😊\n_Escribe *menu* si necesitas algo más._"
        try:
            idx = int(txt) - 1
            if not (0 <= idx < len(citas)):
                raise ValueError("fuera de rango")
        except (ValueError, TypeError):
            retries = data.get("cancel_retries", 0) + 1
            if retries >= 3:
                save_session(phone, "HUMAN_TAKEOVER", {"hold_sent": True, "handoff_reason": "cancel_retries"})
                return (
                    "No logro entender tu selección 😕\n"
                    f"Te comunico con recepción para ayudarte.\n📞 *{CMC_TELEFONO}*"
                )
            data["cancel_retries"] = retries
            save_session(phone, "WAIT_CITA_CANCELAR", data)
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
        if tl in AFIRMACIONES or tl_norm in AFIRMACIONES:
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

        if tl in NEGACIONES or tl_norm in NEGACIONES:
            reset_session(phone)
            return "Perfecto, tu cita se mantiene 😊\n_Escribe *menu* si necesitas algo más._"

        return "Responde *SÍ* para cancelar o *NO* para mantener la cita."

    # ── WAIT_RUT_REAGENDAR ────────────────────────────────────────────────────
    if state == "WAIT_RUT_REAGENDAR":
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

        data.update({"paciente": paciente, "citas": citas, "rut": rut})
        save_session(phone, "WAIT_CITA_REAGENDAR", data)
        return _format_citas_reagendar(citas, paciente["nombre"])

    # ── WAIT_CITA_REAGENDAR ───────────────────────────────────────────────────
    if state == "WAIT_CITA_REAGENDAR":
        citas = data.get("citas", [])
        _SET_SALIR = {"menu", "menú", "salir", "atras", "atrás"}
        if (tl in NEGACIONES or tl_norm in NEGACIONES
                or tl in _SET_SALIR or tl_norm in _SET_SALIR):
            reset_session(phone)
            return "Perfecto, dejamos tu cita como está 😊\n_Escribe *menu* si necesitas algo más._"
        try:
            idx = int(txt) - 1
            if not (0 <= idx < len(citas)):
                raise ValueError("fuera de rango")
        except (ValueError, TypeError):
            retries = data.get("reagendar_retries", 0) + 1
            if retries >= 3:
                save_session(phone, "HUMAN_TAKEOVER", {"hold_sent": True, "handoff_reason": "reagendar_retries"})
                return (
                    "No logro entender tu selección 😕\n"
                    f"Te comunico con recepción para ayudarte.\n📞 *{CMC_TELEFONO}*"
                )
            data["reagendar_retries"] = retries
            save_session(phone, "WAIT_CITA_REAGENDAR", data)
            return f"Elige un número entre 1 y {len(citas)} 😊"

        cita_old = citas[idx]
        esp_lower = (cita_old.get("especialidad") or "").lower()
        if not esp_lower:
            reset_session(phone)
            return (
                "No pude identificar la especialidad de esa cita 😕\n"
                f"Llama a recepción: 📞 *{CMC_TELEFONO}*"
            )
        data["cita_old"] = cita_old
        data["reagendar_mode"] = True
        # Pre-fill perfil para no volver a pedir RUT en el confirming
        data["rut_conocido"] = data.get("rut", "")
        data["nombre_conocido"] = data["paciente"]["nombre"]
        log_event(phone, "reagendar_elegida_cita",
                  {"id_cita": cita_old["id"], "especialidad": esp_lower})
        return await _iniciar_agendar(phone, data, esp_lower)

    # ── WAIT_WAITLIST_CONFIRM ─────────────────────────────────────────────────
    if state == "WAIT_WAITLIST_CONFIRM":
        if tl == "waitlist_si" or tl in AFIRMACIONES or tl_norm in AFIRMACIONES:
            perfil = get_profile(phone)
            if perfil:
                data["rut"] = perfil["rut"]
                data["paciente_nombre"] = perfil["nombre"]
                return _inscribir_waitlist_y_responder(phone, data)
            save_session(phone, "WAIT_WAITLIST_RUT", data)
            return (
                "Perfecto 👍 Para inscribirte necesito tu RUT:\n"
                "(ej: *12.345.678-9*)"
            )
        if tl == "waitlist_no" or tl in NEGACIONES or tl_norm in NEGACIONES:
            reset_session(phone)
            return (
                "Sin problema 😊 Cuando lo necesites, escríbenos.\n"
                f"_Llama a recepción: 📞 *{CMC_TELEFONO}* · ☎️ *{CMC_TELEFONO_FIJO}*_"
            )
        return "Responde *SÍ* para inscribirte o *NO* si prefieres llamar a recepción."

    # ── WAIT_WAITLIST_RUT ─────────────────────────────────────────────────────
    if state == "WAIT_WAITLIST_RUT":
        rut = clean_rut(txt)
        if not valid_rut(rut):
            return (
                "Ese RUT no quedó bien 😕\n"
                "Escríbelo así: *12.345.678-9*"
            )
        data["rut"] = rut
        # Buscar paciente en Medilink para traer el nombre
        paciente = await buscar_paciente(rut)
        if paciente:
            data["paciente_nombre"] = paciente["nombre"]
            save_profile(phone, rut, paciente["nombre"])
            return _inscribir_waitlist_y_responder(phone, data)
        # Paciente no existe: pedir nombre
        save_session(phone, "WAIT_WAITLIST_NOMBRE", data)
        return (
            "No encontré ese RUT en el sistema, pero igual te inscribo en la lista 😊\n\n"
            "Escríbeme tu *nombre completo* (ej: *María González López*)"
        )

    # ── WAIT_WAITLIST_NOMBRE ──────────────────────────────────────────────────
    if state == "WAIT_WAITLIST_NOMBRE":
        partes = txt.strip().split()
        if len(partes) < 2:
            return "Escribe tu nombre completo con nombre y apellido (ej: *María González*)."
        nombre = " ".join(p.capitalize() for p in partes)
        data["paciente_nombre"] = nombre
        save_profile(phone, data.get("rut", ""), nombre)
        return _inscribir_waitlist_y_responder(phone, data)

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
        data["reg_nombre"] = nombre
        data["reg_apellidos"] = apellidos
        # Auto-rellenar celular desde el número de WhatsApp
        cel = phone.lstrip("+")
        if cel.startswith("56") and len(cel) >= 10:
            data["reg_celular"] = cel[2:]  # 9 dígitos sin código país (ej: 912345678)
        log_event(phone, "registro_inicio", {"rut": data.get("rut", ""), "step": "nombre"})
        save_session(phone, "WAIT_FECHA_NAC", data)
        return (
            f"Gracias, *{nombre}* 😊\n\n"
            "Para completar tu ficha, necesito algunos datos más.\n"
            "Si no quieres responder alguno, escribe *saltar*.\n\n"
            "📅 *¿Cuál es tu fecha de nacimiento?*\n"
            "(ej: *15/03/1990* o *15-03-1990*)"
        )

    # ── WAIT_FECHA_NAC ─────────────────────────────────────────────────────
    if state == "WAIT_FECHA_NAC":
        if tl in ("saltar", "no", "no tengo", "skip", "paso"):
            log_event(phone, "registro_skip", {"step": "fecha_nacimiento"})
        else:
            fecha_nac = _parsear_fecha_nacimiento(txt.strip())
            if not fecha_nac:
                return (
                    "No entendí la fecha 😕\n"
                    "Escríbela así: *15/03/1990* o *15 marzo 1990*\n"
                    "(o escribe *saltar*)"
                )
            from datetime import date as _date
            if fecha_nac.year < 1920 or fecha_nac > datetime.now().date():
                return "Esa fecha no parece correcta 🤔 Intenta de nuevo (ej: *15/03/1990*)"
            data["reg_fecha_nacimiento"] = fecha_nac.strftime("%Y-%m-%d")
        save_session(phone, "WAIT_SEXO", data)
        return _btn_msg(
            "👤 *¿Cuál es tu sexo?*",
            [
                {"id": "sexo_m", "title": "Masculino"},
                {"id": "sexo_f", "title": "Femenino"},
                {"id": "sexo_skip", "title": "Saltar"},
            ]
        )

    # ── WAIT_SEXO ──────────────────────────────────────────────────────────
    if state == "WAIT_SEXO":
        if tl in ("saltar", "no", "skip", "paso", "sexo_skip"):
            log_event(phone, "registro_skip", {"step": "sexo"})
        elif tl in ("m", "masculino", "hombre", "sexo_m"):
            data["reg_sexo"] = "M"
        elif tl in ("f", "femenino", "mujer", "sexo_f"):
            data["reg_sexo"] = "F"
        else:
            return _btn_msg(
                "No entendí. Selecciona una opción:",
                [
                    {"id": "sexo_m", "title": "Masculino"},
                    {"id": "sexo_f", "title": "Femenino"},
                    {"id": "sexo_skip", "title": "Saltar"},
                ]
            )
        save_session(phone, "WAIT_COMUNA", data)
        return "🏘️ *¿De qué comuna eres?*\n(ej: *Arauco*, *Curanilahue*, *Cañete*. O escribe *saltar*)"

    # ── WAIT_COMUNA ────────────────────────────────────────────────────────
    if state == "WAIT_COMUNA":
        if tl in ("saltar", "no", "skip", "paso", "no tengo"):
            log_event(phone, "registro_skip", {"step": "comuna"})
        else:
            data["reg_comuna"] = txt.strip().title()
        save_session(phone, "WAIT_EMAIL", data)
        return "📧 *¿Cuál es tu correo electrónico?*\n(ej: *maria@gmail.com*. O escribe *saltar*)"

    # ── WAIT_EMAIL ─────────────────────────────────────────────────────────
    if state == "WAIT_EMAIL":
        if tl in ("saltar", "no", "skip", "paso", "no tengo", "no se", "no sé"):
            log_event(phone, "registro_skip", {"step": "email"})
        else:
            email = txt.strip().lower()
            if "@" in email and "." in email:
                data["reg_email"] = email
            else:
                # No parece correo válido, lo ignoramos y seguimos
                log_event(phone, "registro_skip", {"step": "email", "raw": email[:60]})
        save_session(phone, "WAIT_REFERRAL", data)
        return _list_msg(
            "📢 *Última pregunta:* ¿Cómo nos conociste?\n(Esto nos ayuda a mejorar nuestro servicio)",
            "Elegir",
            [{"title": "Opciones", "rows": [
                {"id": "ref_amigo",      "title": "Amigo o familiar"},
                {"id": "ref_google",     "title": "Google / internet"},
                {"id": "ref_rrss",       "title": "Redes sociales"},
                {"id": "ref_recurrente", "title": "Ya me atendí antes"},
                {"id": "ref_saltar",     "title": "Prefiero no decir"},
            ]}]
        )

    # ── WAIT_REFERRAL ─────────────────────────────────────────────────────
    if state == "WAIT_REFERRAL":
        _REF_MAP = {
            "ref_amigo": "amigo", "ref_google": "google",
            "ref_rrss": "rrss", "ref_recurrente": "recurrente",
        }
        ref_source = _REF_MAP.get(tl)
        if not ref_source and tl in ("saltar", "skip", "paso", "no", "ref_saltar"):
            log_event(phone, "registro_skip", {"step": "referral"})
        elif ref_source:
            save_tag(phone, f"referido:{ref_source}")
            log_event(phone, "registro_referral", {"source": ref_source})
        else:
            # Texto libre: intentar mapear
            tl_ref = tl
            if any(w in tl_ref for w in ("amig", "famili", "conoci", "vecin")):
                save_tag(phone, "referido:amigo")
                log_event(phone, "registro_referral", {"source": "amigo", "raw": txt[:60]})
            elif any(w in tl_ref for w in ("google", "internet", "busq", "web")):
                save_tag(phone, "referido:google")
                log_event(phone, "registro_referral", {"source": "google", "raw": txt[:60]})
            elif any(w in tl_ref for w in ("instagram", "facebook", "tiktok", "red")):
                save_tag(phone, "referido:rrss")
                log_event(phone, "registro_referral", {"source": "rrss", "raw": txt[:60]})
            elif any(w in tl_ref for w in ("antes", "siempre", "años", "venia", "venía")):
                save_tag(phone, "referido:recurrente")
                log_event(phone, "registro_referral", {"source": "recurrente", "raw": txt[:60]})
            else:
                log_event(phone, "registro_skip", {"step": "referral", "raw": txt[:60]})
        # Crear paciente con todos los datos recopilados
        rut = data.get("rut", "")
        nombre = data.get("reg_nombre", "")
        apellidos = data.get("reg_apellidos", "")
        extra = {}
        if data.get("reg_fecha_nacimiento"):
            extra["fecha_nacimiento"] = data["reg_fecha_nacimiento"]
        if data.get("reg_sexo"):
            extra["sexo"] = data["reg_sexo"]
        if data.get("reg_celular"):
            extra["celular"] = data["reg_celular"]
            extra["telefono"] = data["reg_celular"]
        if data.get("reg_comuna"):
            extra["comuna"] = data["reg_comuna"]
        if data.get("reg_email"):
            extra["email"] = data["reg_email"]
        log_event(phone, "registro_completo", {
            "rut": rut, "campos_extra": list(extra.keys()),
            "total_campos": len(extra),
        })
        paciente = await crear_paciente(rut, nombre, apellidos, **extra)
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
        # Auto-escape: si el paciente cambió de tema y su mensaje tiene una
        # intención accionable (agendar, cancelar, precio, info, etc.), NO lo
        # dejamos atrapado esperando a la recepcionista — reseteamos la sesión
        # y re-procesamos el mismo texto desde IDLE. Así el bot "entiende solo"
        # cuando el paciente retoma el flujo, sin obligarlo a escribir "hola".
        # Los botones de confirmación pre-cita ya se manejan arriba, por eso
        # acá solo consultamos Claude si es texto libre de cierta longitud.
        _es_boton_precita = tl.startswith(("cita_confirm:", "cita_reagendar:", "cita_cancelar:"))
        if not _es_boton_precita and len(txt) >= 3:
            try:
                _result = await detect_intent(txt)
                _intent = _result.get("intent", "otro")
            except Exception:
                _intent = "otro"
            _ACCIONABLES = {
                "agendar", "reagendar", "cancelar", "ver_reservas",
                "waitlist", "disponibilidad", "precio", "info",
            }
            if _intent in _ACCIONABLES:
                log_event(phone, "human_takeover_exit", {"intent": _intent, "texto": txt[:120]})
                reset_session(phone)
                fresh_session = {"state": "IDLE", "data": {}}
                return await handle_message(phone, texto, fresh_session)

        # Mensaje quedó guardado en el historial — recepcionista responde desde el panel
        # Solo respondemos si el paciente sigue enviando mensajes para que no sienta silencio
        msgs_sin_respuesta = data.get("msgs_sin_respuesta", 0) + 1
        data["msgs_sin_respuesta"] = msgs_sin_respuesta
        save_session(phone, "HUMAN_TAKEOVER", data)

        # Si el paciente escribe un mensaje con señal clínica (síntoma o
        # palabra clave de patología) mientras está en HUMAN_TAKEOVER, no
        # respondamos con "Recibido 🙏" porque se siente desatendido. Le
        # damos un mensaje más específico que reconoce el contenido clínico
        # y refuerza el canal de urgencia.
        _CLINICAL_KWS = (
            "diabet", "hipert", "presion", "presión", "azucar", "azúcar",
            "colesterol", "tiroid", "asma", "epilep", "cancer", "cáncer",
            "embaraz", "operac", "cirug", "medicament", "pastilla",
            "remedio", "receta", "examen", "análisis", "analisis",
            "control", "chequeo", "tratamient", "diagnost", "diagnóstic",
        )
        texto_clinico = (
            _SENALES_SINTOMA.search(tl)
            or any(kw in tl_norm for kw in _CLINICAL_KWS)
            or any(kw in tl for kw in _CLINICAL_KWS)
        )
        if texto_clinico:
            log_event(phone, "human_takeover_clinico", {"texto": txt[:240]})
            return (
                "Gracias por contarnos 🙏 Ya registré tu mensaje para que una "
                "recepcionista te responda en este chat.\n\n"
                f"*Si es urgente o empeora, llama ahora:*\n📞 *{CMC_TELEFONO}*\n"
                "🚑 *SAMU*: 131"
            )

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
_MED_AO_IDS      = [73, 1]      # Primarios: Abarca (08-16) + Olavarría (16-21)
_MED_OVERFLOW_ID = 13            # Márquez: overflow cuando Abarca+Olavarría no tienen cupo
_ESP_MED_GENERAL = {"medicina general", "medicina familiar"}


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
    """Expande progresivamente los horarios de Medicina General.
    Stage 0→1: muestra slots del doctor sugerido (ya cargados).
    Stage 1→2: busca el OTRO doctor primario (Abarca↔Olavarría) y muestra ambos.
    Stage 2→3: muestra los 3 (Abarca + Olavarría + Márquez) con todos los horarios."""
    next_stage = stage + 1
    prof_sugerido_id = data.get("prof_sugerido_id")

    if next_stage == 1:
        # Mostrar los slots del doctor sugerido (ya guardados en data["slots"])
        data["expansion_stage"] = 1
        save_session(phone, "WAIT_SLOT", data)
        return _format_slots(data["slots"])

    elif next_stage == 2:
        # Buscar el OTRO doctor primario (el que NO fue sugerido)
        slots_sugerido = data.get("slots", [])
        otros_primarios = [i for i in _MED_AO_IDS if i != prof_sugerido_id]
        smart_otro, todos_otro = (await buscar_slots_dia_por_ids(otros_primarios, fecha)) if (fecha and otros_primarios) else ([], [])

        show_sug = slots_sugerido[:4]
        show_otro = smart_otro[:4]
        combined = show_sug + show_otro

        data["expansion_stage"] = 2
        data["slots"] = combined
        data["todos_slots"] = todos_slots + todos_otro
        save_session(phone, "WAIT_SLOT", data)

        groups = []
        if show_sug:
            groups.append({"slots": show_sug})
        if show_otro:
            groups.append({"slots": show_otro})
        return _format_slots_expansion(groups, show_ver_mas=True) if groups else _format_slots(todos_slots, mostrar_todos=True)

    else:
        # Todos los horarios de los 3 profesionales (cada uno en su próximo día disponible)
        all_groups = []
        todos_all = []
        for pid in _MED_GENERAL_IDS:
            _, slots_pid = (await buscar_slots_dia_por_ids([pid], fecha)) if fecha else ([], [])
            if not slots_pid and pid == _MED_OVERFLOW_ID:
                _, slots_pid = await buscar_primer_dia("medicina familiar")
            if slots_pid:
                all_groups.append({"slots": slots_pid})
                todos_all.extend(slots_pid)

        data["expansion_stage"] = 3
        data["slots"] = todos_all
        data["todos_slots"] = todos_all
        save_session(phone, "WAIT_SLOT", data)

        return _format_slots_expansion(all_groups) if all_groups else "No hay más horarios disponibles."


def _modo_degradado(phone: str, intent: str, state_snap: str = "") -> str:
    """Respuesta cuando Medilink está caído. Encola la intención y avisa al paciente.
    Devuelve un mensaje graceful que el bot enviará por WhatsApp."""
    enqueue_intent(phone, intent, state_snap)
    log_event(phone, "modo_degradado", {"intent": intent})
    reset_session(phone)
    return (
        "Nuestro sistema de citas está con un problema técnico en este momento 😕\n\n"
        "Guardé tu mensaje y te avisaré apenas vuelva a estar operativo. "
        "Mientras tanto puedes llamarnos:\n"
        f"📞 *{CMC_TELEFONO}*\n"
        f"☎️ *{CMC_TELEFONO_FIJO}*\n\n"
        "_Gracias por tu paciencia._"
    )


async def _iniciar_agendar(phone: str, data: dict, especialidad: str | None,
                            saludo_prefix: str | None = None) -> str:
    if is_medilink_down():
        return _modo_degradado(phone, "agendar", especialidad or "")
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
    # Medicina general: stage 0 = slot más próximo entre Abarca (08-16) y Olavarría (16-21).
    # Márquez (15-20) solo aparece como overflow si Abarca+Olavarría no tienen cupo.
    if especialidad_lower in _ESP_MED_GENERAL:
        smart, todos = await buscar_primer_dia(especialidad_lower, solo_ids=_MED_AO_IDS)
        if todos:
            mejor = todos[0]  # más próximo entre ambos doctores
        else:
            # Abarca + Olavarría sin disponibilidad → Márquez como overflow
            smart, todos = await buscar_primer_dia(especialidad_lower, solo_ids=[_MED_OVERFLOW_ID])
            mejor = todos[0] if todos else None
    else:
        smart, todos = await buscar_primer_dia(especialidad_lower)
        mejor = smart[0] if smart else (todos[0] if todos else None)

    if not todos or not mejor:
        log_event(phone, "sin_disponibilidad", {"especialidad": especialidad})
        save_tag(phone, "sin-disponibilidad")
        # Ofrecer lista de espera en lugar de terminar la conversación
        # Si la especialidad resuelve a un único profesional (ej. "olavarria",
        # "castillo"), lo guardamos como preferencia → el cron buscará solo a ese.
        from medilink import _ids_para_especialidad
        ids_resueltos = _ids_para_especialidad(especialidad_lower)
        id_prof_pref = int(ids_resueltos[0]) if len(ids_resueltos) == 1 else None
        data["waitlist_especialidad"] = especialidad_lower
        data["waitlist_id_prof_pref"] = id_prof_pref
        save_session(phone, "WAIT_WAITLIST_CONFIRM", data)
        return _btn_msg(
            f"No encontré horas disponibles para *{especialidad}* en los próximos días 😕\n\n"
            "¿Quieres que te avise apenas se libere un cupo?\n"
            "Te inscribo en nuestra lista de espera y te escribo por WhatsApp.",
            [
                {"id": "waitlist_si", "title": "📝 Sí, inscribirme"},
                {"id": "waitlist_no", "title": "No, gracias"},
            ]
        )
    fecha = mejor["fecha"]
    # Al tocar "Ver más horarios" mostramos los del MISMO doctor del sugerido.
    # smart_select del combinado puede sesgar hacia un doctor con más adyacencias;
    # reconstruimos el smart usando solo los slots del doctor sugerido.
    prof_sugerido_id = mejor.get("id_profesional")
    slots_sugerido_todos = [s for s in todos if s.get("id_profesional") == prof_sugerido_id]
    smart_sugerido = slots_sugerido_todos[:5] if slots_sugerido_todos else smart
    data.update({"especialidad": especialidad_lower, "slots": smart_sugerido,
                 "todos_slots": todos, "fechas_vistas": [fecha],
                 "expansion_stage": 0, "prof_sugerido_id": prof_sugerido_id})
    save_session(phone, "WAIT_SLOT", data)
    nombre_conocido = data.get("nombre_conocido", "")
    nombre_corto = nombre_conocido.split()[0] if nombre_conocido else ""
    # Si viene con saludo_prefix (ej. desde un motivo del menú), el prefix
    # actúa como header y se omite el "¡Hola de nuevo!" para no duplicar saludos.
    if saludo_prefix:
        header = saludo_prefix
    else:
        header = f"¡Hola de nuevo, *{nombre_corto}*! " if nombre_corto else ""
    # Tercer botón: "Otro profesional" si hay >1 doctor; si no, "Otro día"
    from medilink import _ids_para_especialidad
    ids_esp = _ids_para_especialidad(especialidad_lower)
    if especialidad_lower in _ESP_MED_GENERAL:
        ids_esp = list(_MED_GENERAL_IDS)  # Abarca, Olavarría, Márquez
    hay_otros = len([i for i in ids_esp if i != prof_sugerido_id]) > 0

    botones = [
        {"id": "confirmar_sugerido", "title": "✅ Sí, esa hora"},
        {"id": "ver_otros",          "title": "📋 Otros horarios"},
    ]
    if hay_otros:
        botones.append({"id": "otro_prof", "title": "👤 Otro profesional"})
    else:
        botones.append({"id": "otro_dia", "title": "📅 Otro día"})

    precio_linea = _precio_line(mejor.get("especialidad", ""), mejor)
    precio_bloque = f"{precio_linea}\n" if precio_linea else ""
    return _btn_msg(
        f"{header}Encontré disponibilidad ✨\n\n"
        f"🏥 *{mejor['especialidad']}* — {mejor['profesional']}\n"
        f"📅 *{mejor['fecha_display']}*\n"
        f"🕐 *{mejor['hora_inicio'][:5]}* ⭐\n"
        f"{precio_bloque}\n"
        "¿La agendo?",
        botones
    )


async def _iniciar_cancelar(phone: str, data: dict) -> str:
    if is_medilink_down():
        return _modo_degradado(phone, "cancelar")
    save_session(phone, "WAIT_RUT_CANCELAR", data)
    return (
        "Claro, te ayudo a cancelar una hora.\n\n"
        "Necesito tu RUT para buscarte:\n"
        "(ej: *12.345.678-9*)"
    )


async def _iniciar_ver(phone: str, data: dict) -> str:
    if is_medilink_down():
        return _modo_degradado(phone, "ver_reservas")
    save_session(phone, "WAIT_RUT_VER", data)
    return (
        "Claro, te muestro tus reservas.\n\n"
        "Necesito tu RUT:\n"
        "(ej: *12.345.678-9*)"
    )


async def _iniciar_reagendar(phone: str, data: dict) -> str:
    """Flujo de reagendar en un paso: lista tus citas, eliges una, buscamos
    un nuevo slot para la misma especialidad y la reemplazamos (crea primero
    la nueva, cancela la anterior solo si la nueva se creó con éxito)."""
    if is_medilink_down():
        return _modo_degradado(phone, "reagendar")
    # Si ya conocemos el perfil, saltamos directo a mostrar sus citas
    perfil = get_profile(phone)
    if perfil and perfil.get("rut"):
        paciente = await buscar_paciente(perfil["rut"])
        if paciente:
            citas = await listar_citas_paciente(paciente["id"])
            if not citas:
                reset_session(phone)
                return (
                    f"No encontré citas futuras para *{paciente['nombre'].split()[0]}* 📋\n\n"
                    "¿Quieres agendar una nueva hora? Escribe *1* o *menu*."
                )
            data.update({"paciente": paciente, "citas": citas, "rut": perfil["rut"]})
            save_session(phone, "WAIT_CITA_REAGENDAR", data)
            return _format_citas_reagendar(citas, paciente["nombre"])
    save_session(phone, "WAIT_RUT_REAGENDAR", data)
    return (
        "Claro, te ayudo a reagendar tu hora 🔄\n\n"
        "Necesito tu RUT para buscar tus citas:\n"
        "(ej: *12.345.678-9*)"
    )


async def _iniciar_waitlist(phone: str, data: dict, especialidad: str | None) -> str:
    """Flujo de lista de espera: si ya sabemos la especialidad, preguntamos
    confirmación; si no, pedimos que elija una del menú de agendar."""
    if not especialidad:
        # Reutilizamos el menú de elegir especialidad pero cambiamos la data
        # con un flag para que al terminar vaya a WAIT_WAITLIST_CONFIRM.
        data["from_waitlist"] = True
        save_session(phone, "WAIT_ESPECIALIDAD", data)
        return (
            "Claro, te ayudo a inscribirte en la lista de espera 📝\n\n"
            f"¿Para qué especialidad?\n\n{_ESPECIALIDADES_TEXTO}"
        )
    esp_lower = especialidad.lower()
    data["waitlist_especialidad"] = esp_lower
    data["waitlist_id_prof_pref"] = None
    save_session(phone, "WAIT_WAITLIST_CONFIRM", data)
    return _btn_msg(
        f"Te voy a inscribir en la lista de espera de *{esp_lower}* 📝\n\n"
        "Cuando se libere un cupo te aviso al tiro por aquí.\n\n"
        "¿Confirmas?",
        [
            {"id": "waitlist_si", "title": "✅ Sí, inscribirme"},
            {"id": "waitlist_no", "title": "No, gracias"},
        ]
    )


def _inscribir_waitlist_y_responder(phone: str, data: dict) -> str:
    """Inscribe al paciente en la tabla waitlist y responde con confirmación."""
    esp = data.get("waitlist_especialidad", "")
    rut = data.get("rut", "") or data.get("rut_conocido", "")
    nombre = data.get("paciente_nombre", "") or data.get("nombre_conocido", "")
    id_prof_pref = data.get("waitlist_id_prof_pref")
    wid = add_to_waitlist(phone, rut, nombre, esp, id_prof_pref)
    save_tag(phone, f"waitlist-{esp}")
    log_event(phone, "waitlist_inscrito",
              {"id": wid, "especialidad": esp, "id_prof_pref": id_prof_pref})
    reset_session(phone)
    nombre_corto = nombre.split()[0] if nombre else ""
    saludo = f"*{nombre_corto}*, " if nombre_corto else ""
    return (
        f"✅ Listo {saludo}quedaste inscrito/a en la lista de espera de *{esp}*.\n\n"
        "Apenas se libere un cupo te aviso por este mismo chat 📱\n\n"
        "_Escribe *menu* si necesitas algo más._"
    )


def _format_citas_reagendar(citas: list, nombre_paciente: str) -> dict:
    """Muestra las citas del paciente para que elija cuál reagendar."""
    nombre = nombre_paciente.split()[0]
    rows = []
    for i, c in enumerate(citas, 1):
        fecha_short = c.get("fecha_display", "")[:10]
        hora = c.get("hora_inicio", "")[:5]
        prof = c.get("profesional", "").split()[-1] if c.get("profesional") else ""
        title = f"{fecha_short} {hora} {prof}"[:24]
        rows.append({"id": str(i), "title": title})
    return _list_msg(
        body_text=f"¿Cuál cita quieres reagendar, *{nombre}*?",
        button_label="Elegir cita",
        sections=[{"title": "Tus citas", "rows": rows}],
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
    precio_linea = _precio_line(slots[0].get("especialidad", ""), slots[0])

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
        body_text = f"Te encontré estas opciones 👇\n\n*{fecha}* — {prof}"
        if precio_linea:
            body_text += f"\n{precio_linea}"
        return _list_msg(
            body_text=body_text,
            button_label="Ver horarios",
            sections=sections,
        )

    # Fallback texto para listas muy largas
    lineas = [f"📅 *{fecha}* — {prof}"]
    if precio_linea:
        lineas.append(precio_linea)
    lineas.append("")  # línea en blanco antes de los slots
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
