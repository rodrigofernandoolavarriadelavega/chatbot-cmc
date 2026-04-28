"""
Máquina de estados para los flujos de conversación.
Opción C: Claude detecta intención → sistema guía el flujo → Medilink ejecuta.
"""
import asyncio
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
_CHILE_TZ = ZoneInfo("America/Santiago")

from claude_helper import (detect_intent, respuesta_faq, clasificar_respuesta_seguimiento,
                           consulta_clinica_doctor, classify_with_context)
from medilink import (buscar_primer_dia, buscar_slots_dia, buscar_slots_dia_por_ids,
                      buscar_paciente, buscar_paciente_por_nombre, crear_paciente, crear_cita,
                      listar_citas_paciente, cancelar_cita, obtener_agenda_dia,
                      valid_rut, clean_rut, especialidades_disponibles,
                      consultar_proxima_fecha, verificar_slot_disponible)
from session import (save_session, reset_session, save_tag, delete_tag, get_tags,
                     save_cita_bot, log_event, has_recent_event,
                     save_profile, get_profile, save_fidelizacion_respuesta, get_ultimo_seguimiento,
                     enqueue_intent, add_to_waitlist, cancel_waitlist,
                     get_cita_bot_by_id_cita, mark_cita_confirmation, get_phone_by_rut,
                     save_demanda_no_disponible, get_waitlist_by_especialidad,
                     mark_waitlist_notified, get_ultima_cita_paciente,
                     has_privacy_consent, save_privacy_consent, revoke_privacy_consent)
from resilience import is_medilink_down
from triage_ges import triage_sintomas, normalizar_texto_paciente
from pni import get_vaccine_reminder
from hitos_desarrollo import get_milestones_reminder
from config import CMC_TELEFONO, CMC_TELEFONO_FIJO, ADMIN_ALERT_PHONE
from messaging import send_whatsapp

log = logging.getLogger("bot.flows")

# Mapa de nombres de día en español → Python weekday (0=Lun..6=Dom)
_DIAS_SEMANA = {
    "lunes": 0, "martes": 1, "miercoles": 2, "miércoles": 2,
    "jueves": 3, "viernes": 4, "sabado": 5, "sábado": 5,
}


def _detectar_fecha_pedida_idle(txt: str) -> str | None:
    """Detecta fecha relativa pedida en un mensaje libre (IDLE → agendar).
    Retorna YYYY-MM-DD o None. Solo se usa para PROPAGAR la preferencia al
    flujo de agendar, no como filtro estricto. Si el paciente menciona
    "en la mañana" / "por la mañana" sin "para mañana", no es día — es franja.
    """
    if not txt:
        return None
    t = txt.lower()
    franjas = ("en la mañana", "en la manana", "por la mañana", "por la manana")
    es_franja = any(p in t for p in franjas)
    hoy = datetime.now(_CHILE_TZ).date()
    if t.strip() in ("hoy", "hoy mismo", "hoy dia", "hoy día"):
        return hoy.strftime("%Y-%m-%d")
    if any(p in t for p in (" para hoy", "para hoy", "hoy mismo", "hoy dia", "hoy día")):
        return hoy.strftime("%Y-%m-%d")
    if " hoy " in f" {t} " and "manana" not in t and "mañana" not in t:
        return hoy.strftime("%Y-%m-%d")
    if "pasado mañana" in t or "pasado manana" in t:
        return (hoy + timedelta(days=2)).strftime("%Y-%m-%d")
    if ("para mañana" in t or "para manana" in t
        or t.strip() in ("mañana", "manana")):
        return (hoy + timedelta(days=1)).strftime("%Y-%m-%d")
    if (("mañana" in t or "manana" in t) and not es_franja):
        return (hoy + timedelta(days=1)).strftime("%Y-%m-%d")
    return None


def _first_name(nombre) -> str:
    """Primer token de un nombre, seguro ante None/vacío/solo-espacios."""
    parts = (nombre or "").split()
    return parts[0] if parts else "paciente"


def _proxima_fecha_dia(weekday: int) -> str:
    """Retorna la fecha (YYYY-MM-DD) del próximo día de la semana dado (hoy + 1 en adelante)."""
    hoy = datetime.now(_CHILE_TZ).date()
    for delta in range(1, 8):
        candidato = hoy + timedelta(days=delta)
        if candidato.weekday() == weekday:
            return candidato.strftime("%Y-%m-%d")
    return None

AFIRMACIONES = {
    "si", "sí", "yes", "ok", "confirmo", "confirmar", "dale", "ya", "claro", "bueno",
    "perfecto", "listo", "tomo", "tomar", "esa", "ese", "esa hora", "ese horario",
    "me sirve", "sirve", "genial", "buenisimo", "buenísimo", "vale", "acepto", "acepta",
    "reservar", "reservalo", "resérvalo", "reservala", "resérvala", "agenda", "agendala",
    "agéndala", "agendar", "confirma", "confírmalo", "confirmalo", "de acuerdo",
}
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
    "perdí la vista", "perdi la vista", "ceguera súbita",
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

DISCLAIMER = "_Soy tu asistente del CMC, no reemplazo la evaluación médica presencial._"

# 200+ variaciones de saludo (chileno, coloquial, typos, WhatsApp).
# Cualquiera de estos → resetea sesión y muestra menú principal.
_SALUDOS_SET = frozenset({
    # ── "Hola" con typos, repeticiones y teclado ──
    "hola", "hol", "holaa", "holaaa", "holaaaa", "holaaaaa", "holas", "holaz",
    "holla", "hila", "hoka", "hoal", "hloa", "holq", "jola", "gola", "hiola",
    "hoola", "hpla", "hols", "hoia", "hla", "hkla", "hopa", "hala", "hela",
    "hula", "holo", "hoña", "hol a", "holahola", "hola hola",
    # con puntuación
    "hola!", "hola!!", "hola!!!", "hola!!!!", "hola.", "hola..", "hola...",
    "hola,", "hola?", "jola!", "ola!", "ola!!", "ola.", "ola..", "ola...", "ola?",
    # ── "Ola" (sin H, muy frecuente en WhatsApp chileno) ──
    "ola", "olaa", "olaaa", "olaaaa", "ols", "ole",
    # ── Variantes informales / juveniles ──
    "holi", "holii", "holiii", "holis", "holiss", "holip", "holap", "holiwi",
    "holiwis", "holu", "jelou", "jelouuu", "hello", "hellou", "hi", "hai",
    "hey", "hey!", "ey", "ei", "eii",
    # ── Chileno "wena/wenas" ──
    "wena", "wenas", "wenaa", "wenaaa", "wenaaaa", "wenass", "weena", "weenas",
    "wenis", "weno", "guena", "güena", "güenas", "guenas", "wenah", "wen",
    "wena!", "wena!!", "wena po", "wenaa po", "wenah po", "wena ahi",
    "wena ahí", "wenas tardes", "wenas noches", "wenas doc",
    "wena doc", "wena doctor",
    # ── "Buenas" solo ──
    "buenas", "buena", "bnas", "bns", "buenaa", "buenass",
    # ── "Buenas tardes" y variantes ──
    "buenas tardes", "buenas tarde", "buena tardes", "buena tarde",
    "buenas tards", "buenas tardess", "buenas tardes!", "buenas tardes!!",
    "bnas tardes", "bnas tards", "bnas tds", "bns tardes", "bns tards",
    "bns tds", "bn tarde", "bn tardes",
    "bueas tardes", "bueas tarde", "buenaa tardes",
    # ── "Buenos días" y variantes ──
    "buenos dias", "buenos días", "buenos dia", "buen dia", "buen día",
    "buens dias", "buens días", "bunos dias", "buemos dias", "beunos dias",
    "bienos dias", "benos dias", "buenos díaz",
    "bns dias", "bns días", "bn dia", "bn dias",
    "buenos dias!", "buenos días!", "buen dia!", "buen día!",
    # ── "Buenas noches" y variantes ──
    "buenas noches", "buenas noche", "buena noches", "buena noche",
    "buenas noch", "bnas noches", "bns noches", "bns nch", "bn noche", "bn noches",
    "bueas noches", "buenas noches!",
    # ── Con "doc/doctor/doctora" ──
    "hola doc", "hola doctor", "hola doctora", "ola doc", "ola doctor",
    "hola señorita", "hola srta", "hola seño", "hola sr", "ola seño",
    "buen dia doc", "buen día doc", "buenos dias doc", "buenos días doc",
    "buenas tardes doc", "buenas tardes doctor", "buenas tardes doctora",
    "buenas noches doc", "buenas noches doctor",
    "bnas tds doc", "bnas doc", "buenas doc",
    # ── Con "centro médico" ──
    "hola centro medico", "hola centro", "hola cmc", "ola cmc",
    "hola consultorio", "hola clinica", "hola clínica",
    # ── Combinaciones ──
    "hola buenas", "hola buenas tardes", "hola buenas noches",
    "hola buenos dias", "hola buenos días", "hola buen dia", "hola buen día",
    "hola que tal", "hola como estan", "hola como están",
    "hola wena", "hola wenas", "ola buenas", "ola buenas tardes",
    "ola buenos dias", "ola wena", "hola buenas buenas", "buenas buenas",
    # ── "Cómo estai" (chileno) ──
    "como estai", "como estái", "como andai", "como andái", "como vai",
    "como estay", "como estás", "como estas", "como esta", "como le va",
    "como les va", "como anda", "kmo estai", "kmo andai", "kmo vai",
    "kmo estas", "kmo andan", "como andan",
    # ── "Qué tal" ──
    "que tal", "qué tal", "que tal?", "qué tal?", "ke tal", "q tal", "qtal", "k tal",
    # ── "Aló" (teléfono/WhatsApp) ──
    "aló", "alo", "alo?", "aló?", "alo buenas", "aló buenas",
    # ── Formales ──
    "saludos", "slds", "un saludo", "salu2", "saludo",
    # ── Oiga / atención (solos, sin mensaje adicional) ──
    "oie", "oie hola", "oiga hola", "hola oiga", "oye",
    # ── Extras coloquiales ──
    "good", "gd", "bn", "bkn", "ta bueno",
})

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
    # "Traumatología" — temporalmente deshabilitada (Dr. Barraza no disponible)
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
    "Odontología General": (
        "\n\n✨ *¿Sabías que hacemos Blanqueamiento Dental?*\n"
        "Dra. Javiera Burgos y Dr. Carlos Jiménez realizan:\n"
        "• Blanqueamiento dental ($120.000)\n"
        "• Carillas de resina (desde $50.000)\n\n"
        "Aprovecha tu visita y mejora tu sonrisa. "
        "Escribe *menu* para agendar 😊"
    ),
    "Ortodoncia": (
        "\n\n✨ *¿Sabías que tenemos Estética Facial?*\n"
        "Dra. Valentina Fuentealba atiende en el CMC:\n"
        "• Armonización facial (eval $15.000)\n"
        "• Hilos tensores ($129.990)\n"
        "• Bioestimuladores ($450.000)\n"
        "• Peeling químico ($50.000)\n\n"
        "Complementa tu nueva sonrisa con estética facial. "
        "Escribe *menu* para agendar 😊"
    ),
    "Endodoncia": (
        "\n\n💡 *Después de una endodoncia se recomienda proteger el diente*\n"
        "Consulta con nuestros odontólogos sobre coronas y restauraciones "
        "para que tu diente quede fuerte y estético.\n\n"
        "Escribe *menu* para agendar un control 😊"
    ),
    "Implantología": (
        "\n\n✨ *¿Sabías que tenemos Estética Facial?*\n"
        "Complementa tu implante con una sonrisa completa. "
        "Dra. Valentina Fuentealba realiza blanqueamiento, "
        "armonización facial y más.\n\n"
        "Escribe *menu* para agendar 😊"
    ),
    "Ginecología": (
        "\n\n💡 *¿Sabías que tenemos Matrona?*\n"
        "Sarai Gómez atiende en el CMC y realiza:\n"
        "• Control ginecológico\n"
        "• PAP\n"
        "• Control prenatal\n"
        "• Planificación familiar\n\n"
        "Complementa tu atención ginecológica. "
        "Escribe *menu* para agendar con ella 😊"
    ),
    "Matrona": (
        "\n\n💡 *¿Sabías que tenemos Ginecólogo?*\n"
        "Dr. Tirso Rejón atiende en el CMC y puede ayudarte con:\n"
        "• Evaluación ginecológica especializada\n"
        "• Ecografía ginecológica\n"
        "• Patología cervical\n"
        "• Control de embarazo de alto riesgo\n\n"
        "Si necesitas atención más especializada, "
        "escribe *menu* para agendar con él 😊"
    ),
}

# Cross-sell inteligente post-consulta: cuando el paciente responde "Mejor",
# le sugerimos un servicio complementario en vez de un control genérico.
# Clave = especialidad (lowercase), Valor = (mensaje, especialidad_destino)
UPSELL_POSTCONSULTA: dict[str, tuple[str, str]] = {
    # traumatología → redirigida a medicina general (Dr. Barraza no disponible)
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
    "ortodoncia": (
        "Ahora que tu sonrisa está mejor, ¿te gustaría complementarla con estética facial? ✨\n"
        "Tenemos armonización facial, hilos tensores, peeling y más.\n\n"
        "¿Te interesa agendar una evaluación?",
        "estética facial",
    ),
    "endodoncia": (
        "Después de una endodoncia es importante proteger el diente 🦷\n"
        "¿Te gustaría agendar un control para evaluar si necesitas corona?\n\n"
        "¿Te agendo?",
        "odontología general",
    ),
    "implantología": (
        "Ahora que tienes tu implante, ¿qué tal mejorar el resto de tu sonrisa? ✨\n"
        "Tenemos blanqueamiento dental y estética facial.\n\n"
        "¿Te interesa?",
        "odontología general",
    ),
    "ginecología": (
        "¿Sabías que nuestra matrona Sarai Gómez complementa la atención ginecológica? 👩‍⚕️\n"
        "Realiza controles, PAP, ecografías y más.\n\n"
        "¿Te gustaría agendar con ella?",
        "matrona",
    ),
    "matrona": (
        "Si necesitas una evaluación más especializada, nuestro ginecólogo "
        "Dr. Tirso Rejón puede ayudarte 🩺\n\n"
        "¿Te interesa agendar?",
        "ginecología",
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


def _precio_line(especialidad: str, slot: dict | None = None, modalidad_override: str | None = None) -> str:
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
    # Si el paciente preguntó por una modalidad distinta a la default, ser
    # explícito: para MG/Kine/Psico/etc el único precio es Fonasa.
    # Bug real 2026-04-25 (56942757630, 17:55): pidió "particular" en MG y
    # bot respondió "Fonasa $7.880" sin advertir que no hay particular.
    if modalidad_override and modalidad_override != modalidad:
        if modalidad == "fonasa":
            return (
                f"💰 Fonasa: {precio_str}\n"
                f"_{esp} se atiende solo con valor Fonasa en el CMC._"
            )
        # default es particular y piden fonasa
        return (
            f"💰 Particular: {precio_str}\n"
            f"_{esp} no tiene convenio Fonasa en el CMC._"
        )
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


# Especialidades con opción Fonasa — las demás son solo particular y se salta
# la pregunta Fonasa/Particular para reducir un paso en el flujo.
_FONASA_SPECIALTIES = frozenset({
    "Medicina General", "Kinesiología", "Psicología Adulto",
    "Psicología Infantil", "Nutrición", "Matrona",
})


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


# ── Consent Ley 19.628 (reforma 2024) ─────────────────────────────────────────

# ── Consent inline al pedir datos personales (Ley 19.628) ─────────────────
# En vez de bloquear al inicio (asusta a los pacientes), incluimos una nota
# breve cuando pedimos el RUT y registramos consent al recibirlo.
_PRIVACY_NOTE = "\n\n_Tus datos se usan solo para tu atención médica · agentecmc.cl/privacidad_"


def _ensure_consent(phone: str) -> None:
    """Auto-registra consent cuando el paciente comparte datos personales (RUT)."""
    if not has_privacy_consent(phone):
        save_privacy_consent(phone, "accepted", method="rut_provided")
        log_event(phone, "privacy_consent_accepted", {"method": "rut_provided"})


async def _buscar_paciente_safe(rut: str) -> tuple[dict | None, bool]:
    """Wrapper de buscar_paciente que distingue 'RUT no existe' de 'error transitorio'.

    Devuelve (paciente, transient_error). Si transient_error es True, el caller
    NO debe asumir que el RUT no existe — Medilink falló (429/timeout/red) y se
    debe derivar a humano para evitar registrar como paciente nuevo a alguien
    que ya está en sistema. Causa raíz del bug donde RUT 16649550-4 (existente)
    se reportó como no encontrado por 429 silenciado.
    """
    paciente = await buscar_paciente(rut)
    if paciente is None and is_medilink_down():
        return None, True
    return paciente, False


def _msg_medilink_transient(extra: str = "") -> str:
    """Mensaje estándar cuando Medilink tira 429/timeout durante búsqueda de RUT."""
    base = (
        "🤔 No pude verificar tu RUT en este momento porque el sistema está lento.\n\n"
        "Una recepcionista te ayudará en breve."
    )
    if extra:
        base += "\n\n" + extra
    base += f"\n\nMientras esperas también puedes llamar:\n📞 *{CMC_TELEFONO}*"
    return base


async def _slot_confirmed(phone: str, data: dict, slot: dict) -> str | dict:
    """Llamada cuando el paciente confirma un slot.

    Fast-track para pacientes recurrentes: si ya tenemos su perfil (RUT + nombre),
    saltamos Fonasa/Particular + Para ti/otra persona + confirmar RUT y vamos
    directo a CONFIRMING_CITA. Reduce de 6 a 3 pasos para el 90%+ de los casos.

    Si no hay perfil o el paciente está agendando para un tercero, sigue el
    flujo normal por WAIT_MODALIDAD.
    """
    # Defensa sistémica: revalidar que el slot no esté en el pasado al momento
    # de confirmar. Cubre el caso donde la conversación quedó abierta horas
    # (sesión vigente) y el paciente confirma una hora que ya pasó. Sin este
    # check, Medilink crearía la cita "para el pasado" o fallaría con error
    # confuso. Detectado 2026-04-28: bot ofreció martes 28 11:40 a paciente que
    # confirmó después de las 19:00 del mismo día.
    try:
        from datetime import datetime as _dtv
        from zoneinfo import ZoneInfo as _Zv
        _hora_str = (slot.get("hora_inicio") or "")[:5]  # "HH:MM"
        _fecha_str = slot.get("fecha") or ""
        if _fecha_str and _hora_str:
            _slot_dt = _dtv.strptime(f"{_fecha_str} {_hora_str}", "%Y-%m-%d %H:%M")
            _slot_dt = _slot_dt.replace(tzinfo=_Zv("America/Santiago"))
            _ahora = _dtv.now(_Zv("America/Santiago"))
            if _slot_dt < _ahora:
                log_event(phone, "slot_expirado_al_confirmar", {
                    "slot": f"{_fecha_str} {_hora_str}",
                    "esp": slot.get("especialidad"),
                    "phone": phone,
                })
                esp_obs = slot.get("especialidad", "") or data.get("especialidad", "")
                reset_session(phone)
                return await _iniciar_agendar(
                    phone, {}, esp_obs or None,
                    saludo_prefix=(
                        f"⚠️ Esa hora (*{_hora_str}* del *{slot.get('fecha_display','')}*) "
                        f"ya pasó.\n\nTe busco la siguiente disponible:\n\n"
                    ),
                )
    except Exception as _e_slot_val:
        log.warning("slot revalidation failed: %s", _e_slot_val)

    data["slot_elegido"] = slot

    # No fast-track si ya sabemos que es para otra persona
    if data.get("booking_for_other"):
        save_session(phone, "WAIT_MODALIDAD", data)
        return _btn_msg(
            f"Perfecto 🙌\n\n"
            f"🏥 *{slot['especialidad']}* — {slot['profesional']}\n"
            f"📅 *{slot['fecha_display']}*\n"
            f"🕐 *{slot['hora_inicio'][:5]}*\n\n"
            "¿Tu atención será Fonasa o Particular?",
            [{"id": "1", "title": "Fonasa"}, {"id": "2", "title": "Particular"}]
        )

    # Fast track: paciente recurrente con perfil completo
    perfil = get_profile(phone)
    if perfil and perfil.get("rut"):
        paciente = await buscar_paciente(perfil["rut"])
        if paciente:
            _ensure_consent(phone)
            # Reutilizar última modalidad conocida (tag modalidad-fonasa/particular)
            tags = get_tags(phone)
            last_modalidad = "fonasa"  # default chileno
            for t in tags:
                if t.startswith("modalidad-"):
                    last_modalidad = t.replace("modalidad-", "")
                    break
            data.update({
                "paciente": paciente,
                "rut": perfil["rut"],
                "modalidad": last_modalidad,
                "booking_for_other": False,
            })
            save_session(phone, "CONFIRMING_CITA", data)
            nombre_corto = _first_name(paciente.get("nombre"))
            modalidad_str = last_modalidad.capitalize()
            return _btn_msg(
                f"*{nombre_corto}*, te reservo esta hora 👇\n\n"
                f"👤 {paciente['nombre']}\n"
                f"🏥 {slot['especialidad']} — {slot['profesional']}\n"
                f"📅 {slot['fecha_display']}\n"
                f"🕐 {slot['hora_inicio'][:5]}\n"
                f"💳 {modalidad_str}\n\n"
                "¿La confirmo?",
                [
                    {"id": "si", "title": "✅ Sí, reservar"},
                    {"id": "cambiar_datos", "title": "✏️ Cambiar algo"},
                ]
            )

    # Flujo para pacientes nuevos (sin perfil aún)
    esp = slot.get("especialidad", "")
    slot_resumen = (
        f"🏥 *{esp}* — {slot['profesional']}\n"
        f"📅 *{slot['fecha_display']}*\n"
        f"🕐 *{slot['hora_inicio'][:5]}*"
    )
    if esp not in _FONASA_SPECIALTIES:
        # Solo particular → saltar pregunta modalidad, ir directo al RUT
        data["modalidad"] = "particular"
        data["booking_for_other"] = False
        # Si ya conocemos al paciente (ej. reagendar), ofrecer atajo
        rut_c = data.get("rut_conocido")
        nombre_c = data.get("nombre_conocido")
        if rut_c and nombre_c:
            save_session(phone, "WAIT_RUT_AGENDAR", data)
            return _btn_msg(
                f"Perfecto 🙌\n\n{slot_resumen}\n\n"
                f"¿Agendo con tus datos, *{_first_name(nombre_c)}*?",
                [
                    {"id": "si", "title": "✅ Sí, continuar"},
                    {"id": "rut_nuevo", "title": "Ingresar otro RUT"},
                ]
            )
        save_session(phone, "WAIT_RUT_AGENDAR", data)
        return (
            f"Perfecto 🙌\n\n{slot_resumen}\n\n"
            "Para reservar necesito tu *RUT* 😊\n"
            "(ej: *12.345.678-9*)\n\n"
            "_Si es para otra persona, escribe *otra persona*._"
            + _PRIVACY_NOTE
        )
    # Fonasa disponible → preguntar modalidad
    save_session(phone, "WAIT_MODALIDAD", data)
    return _btn_msg(
        f"Perfecto 🙌\n\n{slot_resumen}\n\n"
        "¿Tu atención será Fonasa o Particular?",
        [{"id": "1", "title": "Fonasa"}, {"id": "2", "title": "Particular"}]
    )


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
    citas = await listar_citas_paciente(pac["id"], rut=pac.get("rut"))
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
    return _btn_msg(
        "Hola Rodrigo 👋 ¿Qué necesitas?",
        [
            {"id": "doc_modo_agente", "title": "🤖 Agente CMC"},
            {"id": "doc_modo_asistente", "title": "👨‍⚕️ Asistente"},
        ]
    )


def _get_doctor_mode(phone: str) -> str | None:
    """Lee el modo del doctor desde tags (persistente, sobrevive resets)."""
    tags = get_tags(phone)
    for t in tags:
        if t.startswith("doctor_mode:"):
            return t.split(":", 1)[1]
    return None


def _set_doctor_mode(phone: str, mode: str):
    """Guarda el modo del doctor como tag (reemplaza el anterior)."""
    # Borrar modo anterior
    tags = get_tags(phone)
    for t in tags:
        if t.startswith("doctor_mode:"):
            delete_tag(phone, t)
    save_tag(phone, f"doctor_mode:{mode}")


def _clear_doctor_mode(phone: str):
    """Elimina el tag de modo del doctor."""
    tags = get_tags(phone)
    for t in tags:
        if t.startswith("doctor_mode:"):
            delete_tag(phone, t)


async def _handle_doctor_command(phone: str, txt: str, tl: str, data: dict, state: str) -> str | None:
    """Procesa comandos del doctor. Retorna respuesta, dict interactivo, o None para pasar al flujo normal."""

    # ── Selección de modo (botones interactivos) ─────────────────────────
    if tl == "doc_modo_agente":
        _set_doctor_mode(phone, "agente")
        return "🤖 *Modo Agente CMC* activado. Estás en el flujo de pacientes para probar.\nEscribe *modo* para cambiar."

    if tl == "doc_modo_asistente":
        _set_doctor_mode(phone, "asistente")
        return (
            "👨‍⚕️ *Asistente Clínico* activado.\n\n"
            "📋 `agenda` — tu agenda de hoy\n"
            "📋 `agenda mañana` — agenda de mañana\n"
            "👤 `paciente 12345678-9` — ficha del paciente\n"
            "🔍 `buscar María González` — buscar por nombre\n"
            "🏷️ `dx RUT dm2 hta` — agregar diagnósticos\n"
            "🗑️ `dxborrar RUT dm2` — eliminar diagnóstico\n"
            "💬 Cualquier otra cosa → pregunta clínica IA\n\n"
            "Escribe *modo* para cambiar."
        )

    # ── Cambiar modo: ÚNICA forma de volver al selector ──────────────────
    # Matchea variantes naturales porque el doctor no se acuerda del comando exacto.
    _MODO_RESET_FRASES = (
        "modo", "cambiar", "cambiar modo", "cambiar_modo",
        "cambio de modo", "cambiar de modo", "cambiar mode",
        "otro modo", "volver al menu", "volver menu", "menu doctor",
        "menu dr", "menú dr", "salir modo", "salir del modo",
    )
    if tl in _MODO_RESET_FRASES or "cambio de modo" in tl or "cambiar de modo" in tl:
        _clear_doctor_mode(phone)
        reset_session(phone)
        return _doctor_mode_menu()

    # ── Leer modo desde tag (persistente) ────────────────────────────────
    doctor_mode = _get_doctor_mode(phone)
    if not doctor_mode and state == "IDLE":
        return _doctor_mode_menu()

    # ── Modo Agente CMC → pasar al flujo normal de pacientes ──────────────
    # Si viene un saludo simple ("hola", "buenos días") en IDLE, asumir que
    # el doctor olvidó que estaba en modo agente y volver al menú doctor.
    if doctor_mode == "agente":
        _saludos_naturales = {"hola", "hi", "buenos dias", "buenos días",
                              "buenas tardes", "buenas noches", "buen dia",
                              "buen día", "ola", "hey"}
        if tl in _saludos_naturales and state == "IDLE":
            _clear_doctor_mode(phone)
            reset_session(phone)
            return _doctor_mode_menu()
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
            "Escribe *modo* para cambiar de modo."
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
        # Propagar es_tercero desde citas_bot para no pisar el perfil del dueño del celular
        cita_bot_row = get_cita_bot_by_id_cita(str(id_cita), phone)
        if cita_bot_row and cita_bot_row.get("es_tercero"):
            data["booking_for_other"] = True
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


# ─────────────────────────────────────────────────────────────────────────────
# Pre-router universal para estados WAIT_*
# Detecta cambios de tema, preguntas paralelas y escape intents usando Claude.
# ─────────────────────────────────────────────────────────────────────────────
import re as _re_pre

_FAST_PATH_BUTTONS = {
    # botones universales
    "si", "sí", "no", "confirmar", "cancelar", "rechazar",
    "confirmar_sugerido", "ver_otros", "ver_todos", "otro_dia", "otro_día",
    "otro_prof", "otro_profesional", "menu", "menu_volver", "cambiar_datos",
    "accion_recepcion", "accion_cambiar", "accion_agendar",
    "accion_mis_citas", "accion_otro", "accion_waitlist",
    # afirmaciones frecuentes
    "ok", "dale", "listo", "vale", "perfecto", "bueno",
    # respuestas modalidad — bug 2026-04-25 (56942757630): el classifier
    # interpretaba "Fonasa" como preguntar_info y devolvía la dirección,
    # ignorando 5 mensajes consecutivos. Fast-path corta el classifier.
    "fonasa", "fona", "particular", "privado", "privada",
    "no_gracias_reeng",
}

_FAST_PATH_PREFIXES = (
    "cita_confirm:", "cita_cancelar:", "cita_reagendar:",
    "motivo_", "cat_", "menu_", "accion_", "slot_", "cita_",
)

def _es_respuesta_obvia_al_prompt(txt: str, tl: str, state: str, data: dict) -> bool:
    """
    Determina si el texto es una respuesta OBVIA al prompt del estado actual.
    Si devuelve True, el pre-router se salta y el handler continúa normal
    (evita costo/latencia de Claude).
    """
    if not txt:
        return True
    if tl in _FAST_PATH_BUTTONS:
        return True
    if any(tl.startswith(p) for p in _FAST_PATH_PREFIXES):
        return True
    # Números cortos (selección de lista 1-20)
    if _re_pre.fullmatch(r"\d{1,2}", tl):
        return True
    # RUT-like
    stripped = tl.replace(".", "").replace(" ", "").replace("-", "")
    if _re_pre.fullmatch(r"\d{7,9}[\dkK]", stripped):
        return True
    # Hora suelta
    if _re_pre.fullmatch(r"\d{1,2}:?\d{0,2}", tl):
        return True
    if _re_pre.fullmatch(r"\d{1,2}\s?(am|pm|hrs?)", tl):
        return True
    # WAIT_MODALIDAD: respuestas obvias
    if state == "WAIT_MODALIDAD":
        if tl in {"fonasa", "fona", "f", "particular", "privado", "privada", "p", "1", "2", "isapre"}:
            return True
    # WAIT_SLOT: frases muy cortas de navegación
    if state == "WAIT_SLOT":
        if tl in ("otro dia", "otro día", "ver todos", "todos", "ver mas",
                  "ver más", "mañana", "manana", "hoy", "pasado mañana",
                  "pasado manana"):
            return True
    # Estados con RUT: cualquier cosa con formato numérico larga ya la filtramos arriba
    return False


def _format_horario_prof(horario: dict) -> str:
    """Formatea un horario Medilink (con dias + horario_dia por weekday) en
    texto legible: "lunes 16:00-20:00, martes 16:00-20:00, miércoles ...".
    Agrupa días con mismo rango.
    """
    DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
    dias = sorted(horario.get("dias", []))
    horario_dia = horario.get("horario_dia", {}) or {}
    if not dias:
        return "según agenda"
    bloques = []
    for d in dias:
        if d not in range(7):
            continue
        rango = horario_dia.get(d)
        if rango and len(rango) >= 2:
            hi, hf = rango[0][:5], rango[1][:5]
            bloques.append((DIAS[d], f"{hi}-{hf}"))
        else:
            bloques.append((DIAS[d], None))
    # Agrupar consecutivos con mismo horario
    grupos: list[tuple[list[str], str | None]] = []
    for nombre, rango in bloques:
        if grupos and grupos[-1][1] == rango:
            grupos[-1][0].append(nombre)
        else:
            grupos.append(([nombre], rango))
    partes = []
    for nombres, rango in grupos:
        if len(nombres) == 1:
            d_str = nombres[0]
        elif len(nombres) == 2:
            d_str = f"{nombres[0]} y {nombres[1]}"
        else:
            d_str = f"{nombres[0]} a {nombres[-1]}"
        if rango:
            partes.append(f"{d_str} {rango}")
        else:
            partes.append(d_str)
    return ", ".join(partes)


async def _send_review_request_if_due(phone: str, especialidad: str = "") -> None:
    """Pide al paciente que dejó 'mejor' que califique en Google.
    Anti-spam: máx 1 vez cada 90 días por teléfono.
    Se dispara con spawn_task tras la respuesta de upsell/control para no
    competir con el cross-sell ni bloquear la conversación."""
    import asyncio
    if has_recent_event(phone, "review_request_sent", days=90):
        return
    await asyncio.sleep(4)  # respiro tras el msg de upsell/control
    try:
        from google_rating import get_review_link
        link = get_review_link()
    except Exception:
        link = "https://search.google.com/local/writereview?placeid=ChIJfwqzraTvaZYRBlt0l4W85JE"
    msg = (
        "Una última cosa 🌟\n\n"
        "Si te tomas 30 segundos, ¿podrías dejarnos una reseña en Google? "
        "Tu opinión ayuda a otras familias de Arauco a encontrarnos.\n\n"
        f"👉 {link}\n\n"
        "_Solo te pedimos esto una vez al año. ¡Gracias!_"
    )
    try:
        await send_whatsapp(phone, msg)
        log_event(phone, "review_request_sent", {"especialidad": especialidad})
    except Exception as e:
        log.warning("review_request fallo phone=%s: %s", phone, e)


async def _responder_horario_por_especialidad(especialidad: str) -> str | None:
    """Responde días+horarios reales (de Medilink) de los profesionales de una
    especialidad. Devuelve None si no hay match. Esto corta el path donde
    Claude Haiku improvisaba horarios genéricos del CMC para profesionales
    específicos. Caso real 2026-04-28 (56958462692): paciente preguntó días
    del otorrino y bot respondió "lunes a viernes 08:00–21:00 + sábado
    09:00–14:00" cuando el Dr. Borrego atiende lunes a miércoles 16:00–20:00.
    """
    if not especialidad:
        return None
    try:
        import httpx as _httpx
        from medilink import _ids_para_especialidad, _get_horario, PROFESIONALES
        ids = _ids_para_especialidad(especialidad.lower())
        if not ids:
            return None
        async with _httpx.AsyncClient(timeout=10) as _c:
            horarios = []
            for pid in ids:
                try:
                    h = await _get_horario(_c, int(pid))
                    horarios.append((pid, h))
                except Exception:
                    continue
        if not horarios:
            return None
        partes = []
        for pid, h in horarios:
            nombre = PROFESIONALES.get(int(pid), {}).get("nombre", "")
            partes.append(f"👨‍⚕️ *{nombre}*: {_format_horario_prof(h)}")
        esp_display = especialidad.lower()
        return (
            f"Horarios de atención de *{esp_display}* en el CMC:\n\n"
            + "\n".join(partes)
            + "\n\n¿Te agendo una hora? Responde *sí* o escribe el día que prefieres."
        )
    except Exception as e:
        log.warning("_responder_horario_por_especialidad falló: %s", e)
        return None


async def _responder_pregunta_horario(phone: str, state: str, data: dict, txt: str = "") -> str:
    """Responde orgánicamente los días de atención del profesional del flujo,
    O del profesional que el paciente mencione en el mensaje (si distinto).

    Caso real 2026-04-22 (56932644508): en WAIT_SLOT con Abarca, paciente pregunta
    "¿el dr Márquez aún trabaja ahí?" — debe responder con días de Márquez,
    no de Abarca.
    """
    prof_id = data.get("prof_sugerido_id")

    # Override: si el texto menciona a otro profesional distinto al sugerido,
    # cambiar a mostrar slots de ESE profesional en lugar de solo días.
    prof_mencionado_id = None
    if txt:
        key_mencionado = _detectar_apellido_profesional(txt)
        if key_mencionado:
            from medilink import _ids_para_especialidad as _ids_chk
            ids_mencionados = _ids_chk(key_mencionado)
            if ids_mencionados and len(ids_mencionados) == 1:
                prof_mencionado_id = ids_mencionados[0]
                if prof_id != prof_mencionado_id:
                    # Paciente pide otro doctor → switch al que pide
                    prof_id = prof_mencionado_id
                    # Intentar cargar slots del nuevo doctor para ofrecerlos
                    try:
                        from medilink import PROFESIONALES, buscar_primer_dia
                        esp_prof = PROFESIONALES.get(int(prof_id), {}).get("especialidad", "").lower()
                        if esp_prof:
                            smart, todos = await buscar_primer_dia(esp_prof, solo_ids=[int(prof_id)])
                            if todos:
                                data["slots"] = (smart or todos)[:5]
                                data["todos_slots"] = todos
                                data["prof_sugerido_id"] = int(prof_id)
                                data["especialidad"] = esp_prof
                                save_session(phone, "WAIT_SLOT", data)
                                prof_nombre_sw = PROFESIONALES.get(int(prof_id), {}).get("nombre", "")
                                # _format_slots puede devolver dict (interactive list)
                                # con <=8 slots → no concatenar, mandar header como msg separado.
                                _slot_resp = _format_slots((smart or todos)[:5])
                                if isinstance(_slot_resp, dict):
                                    await send_whatsapp(phone, f"Cambié a *{prof_nombre_sw}* 👨‍⚕️")
                                    return _slot_resp
                                return f"Cambié a *{prof_nombre_sw}* 👨‍⚕️{chr(10)}{chr(10)}" + _slot_resp
                    except Exception as _e_sw:
                        log.warning("switch prof en preguntar_horario falló: %s", _e_sw)

    if not prof_id:
        return (
            "Los días de atención varían según el profesional. "
            "Si quieres te muestro horarios disponibles por día — "
            "escribe el día que prefieres (ej: *lunes*, *mañana*, *próximo martes*)."
        )
    try:
        import httpx as _httpx
        from medilink import _get_horario, PROFESIONALES
        async with _httpx.AsyncClient(timeout=10) as _c:
            horario = await _get_horario(_c, int(prof_id))
        prof_nombre = PROFESIONALES.get(int(prof_id), {}).get("nombre", "El profesional")
        especialidad = PROFESIONALES.get(int(prof_id), {}).get("especialidad", "")
        esp_sufijo = f" de *{especialidad}*" if especialidad else ""
        horario_str = _format_horario_prof(horario)
        # Marcar prof pedido explícitamente para que confirmar_sugerido no
        # reserve con otro. Caso 56988694763: pidió Márquez, bot mostró días
        # de atención pero no slots; al confirmar reservó con Olavarría.
        if prof_mencionado_id:
            data["prof_pedido_explicito"] = int(prof_mencionado_id)
            save_session(phone, state, data)
        return f"📅 *{prof_nombre}*{esp_sufijo} atiende: {horario_str}"
    except Exception as e:
        log.warning("pregunta_horario falló: %s", e)
        return "Los días de atención dependen del profesional. Te puedo mostrar horarios disponibles."


_ESP_DENTALES = {
    "odontología", "odontologia", "ortodoncia", "endodoncia",
    "implantología", "implantologia", "estética facial", "estetica facial",
    "estética dental", "estetica dental",
}

def _preguntar_pago_respuesta(data: dict | None = None, txt: str = "") -> str:
    """Responde sobre pago. Si hay especialidad activa, muestra SOLO los métodos
    aplicables (médica vs dental) para no confundir al paciente. Sin contexto,
    muestra ambos.
    """
    precio_block = ""
    esp_low = ""
    if data:
        slot = data.get("slot_elegido") or {}
        esp = (slot.get("especialidad") or data.get("especialidad") or "").strip()
        esp_low = esp.lower()
        if esp:
            # Si el paciente menciona "particular" o "fonasa" en el texto, forzar esa columna
            _modalidad_pedida = None
            _txt_low = (txt or "").lower()
            if "particular" in _txt_low or "privado" in _txt_low:
                _modalidad_pedida = "particular"
            elif "fonasa" in _txt_low:
                _modalidad_pedida = "fonasa"
            linea = _precio_line(esp, slot if slot else None, modalidad_override=_modalidad_pedida)
            if linea:
                precio_block = f"{linea}\n\n"
    # Filtrar la línea de pago según el tipo de especialidad
    if esp_low and any(d in esp_low for d in _ESP_DENTALES):
        metodos = "• Efectivo, transferencia, débito o crédito\n"
    elif esp_low:
        metodos = "• Efectivo o transferencia\n"
    else:
        metodos = (
            "• *Atenciones médicas:* efectivo o transferencia\n"
            "• *Atenciones dentales:* efectivo, transferencia, débito o crédito\n"
        )
    return (
        f"{precio_block}"
        "💳 *Pago:* se cancela al momento de la atención.\n"
        f"{metodos}"
        "No se cobra al agendar la hora."
    )


def _preguntar_info_respuesta() -> str:
    return (
        f"📍 Monsalve 102, Carampangue\n"
        f"📞 *{CMC_TELEFONO}* · ☎️ *{CMC_TELEFONO_FIJO}*\n"
        f"🕐 Lun-Vie 8-21h · Sáb 9-14h"
    )


def _recordatorio_prompt(state: str, data: dict) -> str:
    """Texto que recuerda al paciente qué estaba pidiendo el bot."""
    if state == "WAIT_SLOT":
        return "_¿Te sirve alguno de los horarios que te mostré, o prefieres otro día?_"
    if state == "WAIT_WAITLIST_CONFIRM":
        return "_Responde *Sí* para inscribirte en lista de espera o *No* si prefieres llamar._"
    if state in ("WAIT_RUT_AGENDAR", "WAIT_RUT_CANCELAR", "WAIT_RUT_REAGENDAR", "WAIT_RUT_VER",
                 "WAIT_WAITLIST_RUT"):
        return "_Necesito tu RUT para continuar (ej: 12.345.678-9)._"
    if state == "WAIT_MODALIDAD":
        return "_Indica si tu atención es *Fonasa* o *Particular*._"
    if state == "WAIT_BOOKING_FOR":
        return "_¿La hora es para *ti* o para *otra persona*?_"
    if state == "CONFIRMING_CITA":
        return "_¿Confirmo la reserva? Responde *Sí* o *No*._"
    return ""


async def _pre_router_wait(phone: str, txt: str, tl: str, state: str, data: dict):
    """
    Pre-router universal para estados WAIT_*.
    Retorna str (respuesta final) si tomó control; None si el handler normal debe continuar.
    """
    # Fast path — evita Claude cuando la respuesta es obvia
    if _es_respuesta_obvia_al_prompt(txt, tl, state, data):
        return None

    try:
        intent = await classify_with_context(txt, state, data)
    except Exception as e:
        log.warning("pre-router classify falló: %s — fallback a handler normal", e)
        return None

    action = intent.get("action")
    tag    = intent.get("intent")
    args   = intent.get("args", {}) or {}

    if action == "continue":
        return None

    # ── Preguntas paralelas: responder y recordar prompt ──
    if action == "answer_and_continue":
        if tag == "preguntar_horario":
            resp = await _responder_pregunta_horario(phone, state, data, txt=txt)
        elif tag == "preguntar_pago":
            resp = _preguntar_pago_respuesta(data, txt=txt)
        elif tag == "preguntar_info":
            # Intentar FAQ específico primero (telemedicina, radiografía, etc).
            from claude_helper import _local_faq_fallback as _faq_fb
            resp = _faq_fb(txt) or _preguntar_info_respuesta()
        else:
            return None
        recordatorio = _recordatorio_prompt(state, data)
        save_session(phone, state, data)
        return f"{resp}\n\n{recordatorio}" if recordatorio else resp

    # ── Escape: cambio de tema ──
    if action == "escape":
        if tag == "confirmar_slot":
            # Paciente acepta el horario mostrado con lenguaje natural
            # ("perfecto tomo la hora", "sí me sirve", "esa está bien").
            slots_mostrados = data.get("slots", [])
            if state == "WAIT_SLOT" and slots_mostrados:
                return await _slot_confirmed(phone, data, slots_mostrados[0])
            return None

        if tag == "cancelar_cita_real":
            # Si el paciente está en flujo de AGENDAR, no cancelar cita existente.
            # Caso 56988694763 22:17: No alcanzo q llegar en CONFIRMING_CITA →
            # significaba rechazar el slot, no anular cita previa.
            if state in ("WAIT_SLOT", "CONFIRMING_CITA", "WAIT_MODALIDAD",
                         "WAIT_RUT_AGENDAR", "WAIT_BOOKING_FOR"):
                save_session(phone, "WAIT_SLOT", data)
                return (
                    "Sin problema 😊 Escribe *otro día* para ver más opciones, "
                    "un número del listado o *menu* para volver al inicio."
                )
            reset_session(phone)
            return await handle_message(phone, "accion_cambiar", {"state": "IDLE", "data": {}})

        if tag == "cambiar_especialidad":
            nueva_esp = (args.get("especialidad") or "").strip().lower()
            reset_session(phone)
            return await _iniciar_agendar(phone, {}, nueva_esp or None)

        if tag == "pedir_hora_nuevo":
            nueva_esp = (args.get("especialidad") or "").strip().lower()
            # Si el paciente NO especificó una especialidad nueva pero antes
            # vio una sugerencia (ej: "¿Te agendo en ecografía?") y ahora
            # dice "quisiera agendar porfavor" → respetar esa sugerencia
            # en vez de resetear y volver a preguntar especialidad de cero.
            esp_sug = data.get("especialidad_sugerida")
            esp_final = nueva_esp or esp_sug or None
            data_carry = {}
            if esp_final:
                # Pasamos perfil conocido si está, así no re-pregunta RUT
                perfil = get_profile(phone)
                if perfil:
                    data_carry["rut_conocido"] = perfil["rut"]
                    data_carry["nombre_conocido"] = perfil["nombre"]
                log_event(phone, "pedir_hora_carry_sugerencia", {
                    "esp_final": esp_final, "tenia_sugerencia": bool(esp_sug),
                    "explicita": bool(nueva_esp),
                })
            reset_session(phone)
            return await _iniciar_agendar(phone, data_carry, esp_final)

        if tag == "cambiar_profesional":
            if state == "WAIT_SLOT":
                # Re-dispatch al handler "otro_prof" del WAIT_SLOT
                return None  # Dejar que el handler con tl="otro_prof" no aplica aquí
                             # (simplemente devolvemos None y el siguiente mensaje podrá escoger)
            # Si está en otro estado, reset y mostrar opciones
            reset_session(phone)
            return await _iniciar_agendar(phone, {}, data.get("especialidad") or None)

        if tag == "llamar_recepcion":
            save_session(phone, state, data)
            return (
                f"Claro, te dejo el contacto:\n\n"
                f"📞 *{CMC_TELEFONO}*\n"
                f"☎️ *{CMC_TELEFONO_FIJO}*\n"
                f"🕐 Lun-Vie 8-21h · Sáb 9-14h\n\n"
                "_Si prefieres, sigo ayudándote por acá 😊_"
            )

        if tag == "buscar_fecha":
            # Delegar a WAIT_SLOT si corresponde; si no, re-abrir flujo
            preferencia = args.get("preferencia_horaria")
            fecha_desde = args.get("fecha_desde")
            if state != "WAIT_SLOT":
                return None
            # Si el texto incluye hora explícita ("a las 20", "20 horas",
            # "20:00", "8 pm") cedemos al handler de WAIT_SLOT que tiene
            # parser de hora exacta — devolver None acá hace fall-through.
            try:
                from time_parser import parse_hora as _ph
                if _ph(txt) is not None:
                    return None
            except Exception:
                pass
            # En WAIT_SLOT: si hay fecha_desde, buscar ese día; si hay preferencia,
            # filtrar slots por periodo horario.
            esp = data.get("especialidad") or ""
            if fecha_desde:
                try:
                    smart_dia, todos_dia = await buscar_slots_dia(esp, fecha_desde)
                    if todos_dia:
                        fv = data.get("fechas_vistas", [])
                        if fecha_desde not in fv:
                            fv = fv + [fecha_desde]
                        data.update({"slots": (smart_dia or todos_dia)[:5],
                                     "todos_slots": todos_dia,
                                     "fechas_vistas": fv, "expansion_stage": 1})
                        save_session(phone, "WAIT_SLOT", data)
                        return _format_slots((smart_dia or todos_dia)[:5])
                except Exception as e:
                    log.warning("buscar_fecha falló: %s", e)
            if preferencia:
                todos_slots = data.get("todos_slots", [])
                def _hora_in(sl, franja):
                    # Slots usan "hora_inicio" — el código original leía "hora"
                    # (siempre None) → todos los slots quedaban filtrados out
                    # y el filtro nunca aplicaba.
                    raw = sl.get("hora_inicio") or sl.get("hora") or "00:00"
                    try:
                        h = int(raw.split(":")[0])
                    except (ValueError, AttributeError):
                        return False
                    if franja == "mañana":
                        return 7 <= h < 13
                    if franja == "tarde":
                        return 13 <= h < 19
                    if franja == "noche":
                        return h >= 19
                    if franja == "tarde-noche":
                        return h >= 13
                    return True
                filtrados = [s for s in todos_slots if _hora_in(s, preferencia)]
                if filtrados:
                    data["slots"] = filtrados[:5]
                    save_session(phone, "WAIT_SLOT", data)
                    return _format_slots(filtrados[:5])
                # Sin slots en la franja pedida → mensaje específico, no caer al menú
                save_session(phone, "WAIT_SLOT", data)
                franja_label = {"mañana": "la mañana", "tarde": "la tarde",
                                "noche": "la noche", "tarde-noche": "la tarde-noche"}.get(preferencia, preferencia)
                return (
                    f"No tengo horas en *{franja_label}* este día 😕\n\n"
                    "Escribe *otro día* para cambiar de fecha, *ver todos* "
                    "para ver los horarios disponibles, o el *número* del horario que prefieras."
                )
            return None

        if tag == "fuera_de_alcance":
            # REGLA SISTÉMICA: el clasificador de intent NUNCA sobreescribe
            # un handler de estado activo. Si el paciente está en CUALQUIER
            # WAIT_*, el handler del estado tiene el contexto correcto para
            # decidir si el mensaje es válido en ese paso. Solo cuando estamos
            # en IDLE (sin flujo activo) tiene sentido derivar a recepción.
            #
            # Excepción operativa: en WAIT_RUT_* permitimos que cierres
            # cordiales ("gracias", "saludos") respondan amablemente sin
            # cerrar el flujo (el paciente está pensando, no abandonó).
            if state.startswith("WAIT_") or state == "CONFIRMING_CITA" or state == "CONFIRMING_CANCEL":
                _tl_fda = txt.lower().strip()
                _CIERRES_CORDIALES = {"gracias", "muchas gracias", "bendiciones",
                                       "saludos", "que tenga buen dia",
                                       "que tengan buen dia", "perfecto"}
                if state.startswith("WAIT_RUT_") and _tl_fda in _CIERRES_CORDIALES:
                    save_session(phone, state, data)
                    return "🙏 Cuando tengas el RUT me lo envías para continuar."
                return None  # ← handler del estado decide
            # IDLE / HUMAN_TAKEOVER → derivar a recepción
            save_session(phone, state, data)
            return (
                f"Para ese tema prefiero que hables directamente con recepción:\n\n"
                f"📞 *{CMC_TELEFONO}*\n"
                f"☎️ *{CMC_TELEFONO_FIJO}*"
            )

    return None


async def handle_message(phone: str, texto: str, session: dict) -> str:
    state = session["state"]
    data  = session["data"]
    txt   = texto.strip()
    tl    = txt.lower()

    # ── Comando admin: /status (y sinónimos) desde el celular del admin ───
    # Abre la ventana 24h de WhatsApp y devuelve el reporte EN VIVO. Útil
    # cuando el job periódico no llegó por "Re-engagement message" (131047).
    if phone == ADMIN_ALERT_PHONE and tl in ("/status", "status", "ping",
                                             "reporte", "/reporte", "health",
                                             "/health", "estado", "/estado"):
        return await _admin_status_report_live()
    # tl_norm = texto del paciente normalizado léxicamente (sin tildes,
    # abreviaciones WhatsApp expandidas, typos frecuentes corregidos,
    # participios rurales arreglados). Lo usamos en los matches hard-coded
    # (emergencias, comandos globales, afirmaciones, negaciones, arauco) para
    # ganar recall con mensajes como "tngo dlor d pcho" o "sangrao mucho".
    # OJO: mantenemos `tl`/`txt` para parseos estrictos (RUT, números, IDs de
    # botón `cat_medico`/`cita_confirm:*`, selección de slot, captura de
    # nombre) y para pasarle a `detect_intent` el texto original.
    tl_norm = normalizar_texto_paciente(txt)

    # Flush pending_tips: si hay tips guardados del ultimo post-consulta
    # (bug #5 fidelizacion), enviarlos ahora que el paciente escribio y
    # la ventana 24h esta abierta.
    _pending_tips = data.get("pending_tips") if isinstance(data, dict) else None
    if _pending_tips:
        try:
            await send_whatsapp(phone, _pending_tips)
            data.pop("pending_tips", None)
            save_session(phone, state, data)
            log_event(phone, "pending_tips_enviados", {"len": len(_pending_tips)})
        except Exception as _e_pt:
            log.warning("Error enviando pending_tips: %s", _e_pt)

    # ── Paciente envía solo una URL: no lo procesemos con Claude ──
    # Caso real 2026-04-23 (56931400124): paciente mandó link a boleta;
    # Claude respondió "el CMC no es una imprenta" (alucinación).
    # URL sola → escalar a recepcionista directamente.
    import re as _re_url
    _URL_SOLA_RE = _re_url.compile(
        r"^(https?://\S+|www\.\S+)$", _re_url.IGNORECASE
    )
    if _URL_SOLA_RE.match(txt.strip()) and state != "HUMAN_TAKEOVER":
        save_session(phone, "HUMAN_TAKEOVER", data)
        log_event(phone, "url_sola_a_humano", {"url": txt.strip()[:200]})
        return (
            "Recibí tu link 🔗 Una recepcionista lo revisará y te responderá"
            " en breve por acá.\n\n"
            f"_Si es urgente: 📞 *{CMC_TELEFONO}*_"
        )

    # ── Mapeo de títulos de botón/lista → IDs (crítico para IG/FB) ─────────────
    # En WhatsApp los clicks de botones llegan con `id`; en Instagram/Messenger
    # el click manda el texto literal del título. Normalizamos aquí antes de
    # que el dispatcher falle al no matchear el id esperado.
    _TITLE_TO_ID = {
        "hablar con recepcion": "accion_recepcion",
        "hablar con recepción": "accion_recepcion",
        "cambiar/cancelar hora": "accion_cambiar",
        "cambiar cancelar hora": "accion_cambiar",
        "cambiar hora": "accion_cambiar",
        "cancelar hora": "accion_cambiar",
        "cancelar mi hora": "accion_cambiar",
        "agendar hora": "accion_agendar",
        "agendar una hora": "accion_agendar",
        "pedir hora": "accion_agendar",
        "ver mis citas": "accion_mis_citas",
        "mis citas": "accion_mis_citas",
        "ver mis reservas": "accion_mis_citas",
        "lista de espera": "accion_waitlist",
        "otro profesional": "otro_prof",
        "👤 otro profesional": "otro_prof",
        "otro dia": "otro_dia",
        "otro día": "otro_dia",
        "ver todos": "ver_todos",
        "ver más": "ver_otros",
        "ver mas": "ver_otros",
    }
    _tl_map_key = tl_norm.lstrip("🔄💬📅📋👤⚡🏥❌✅🔎📊📷 ").strip()
    if _tl_map_key in _TITLE_TO_ID:
        tl = _TITLE_TO_ID[_tl_map_key]
    elif tl_norm in _TITLE_TO_ID:
        tl = _TITLE_TO_ID[tl_norm]

    # ── Confirmación pre-cita (respuesta al recordatorio de 09:00) ────────────
    # Los botones del recordatorio llegan con ID "cita_confirm:<id>", etc.
    # Debe ir ANTES de emergencias y comandos globales para que siempre se procese.
    if tl.startswith(("cita_confirm:", "cita_reagendar:", "cita_cancelar:")):
        return await _handle_confirmacion_precita(phone, tl, data)

    # ── Respuesta al reenganche "No por ahora" ────────────────────────────────
    # Bug 2026-04-25 (56933748605, 15:32): el botón de jobs.py mandaba
    # "no_gracias_reeng" pero no había handler → caía en HUMAN_TAKEOVER y el
    # bot decía "Una recepcionista te responderá", asustando al paciente que
    # solo dijo "no por ahora". Cerramos amable sin escalar.
    if tl == "no_gracias_reeng":
        log_event(phone, "reenganche_rechazado", {"state": state})
        # Limpiar flag para permitir reenganche futuro y conservar la sesión
        # para que retome cuando quiera (no resetear estado completo).
        data.pop("reenganche_sent", None)
        save_session(phone, state, data)
        return (
            "Sin problema 😊 Cuando quieras retomar, escribe *menu* y te ayudo.\n\n"
            f"_📞 *{CMC_TELEFONO}* si lo prefieres por teléfono._"
        )

    # ── Comandos del profesional (doctor_mode) ──────────────────────────
    # Gate via dashboard /profesionalescmc → permiso "wa_access".
    # Fallback legacy: ADMIN_ALERT_PHONE siempre tiene acceso (primer arranque
    # del dashboard sin data aún).
    _doctor_phone = ADMIN_ALERT_PHONE  # bypass STOP legacy (ver linea 1551)
    _tiene_wa_prof = False
    try:
        from admin_routes import get_permiso as _get_permiso_wa
        _tiene_wa_prof = _get_permiso_wa(phone, "wa_access", default=False)
    except Exception:
        pass
    if phone == ADMIN_ALERT_PHONE or _tiene_wa_prof:
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
    # IMPORTANTE: emergencias pasan por encima del opt-in de privacidad
    # (Ley 19.628 art. 21 — base legal "interés vital del titular").
    # Solo registramos el evento (no el texto crudo) para minimizar PII.
    if (any(p in tl_norm for p in EMERGENCIAS)
            or any(pat.search(tl_norm) for pat in EMERGENCIAS_PATRONES)
            or any(pat.search(tl_norm) for pat in EMERGENCIAS_VITAL_PATRONES)
            or any(p in tl for p in EMERGENCIAS)
            or any(pat.search(tl) for pat in EMERGENCIAS_PATRONES)
            or any(pat.search(tl) for pat in EMERGENCIAS_VITAL_PATRONES)):
        _consented_now = has_privacy_consent(phone)
        log_event(phone, "emergencia_detectada",
                  {"consented": _consented_now, "texto": txt[:240] if _consented_now else "[redacted]"})
        reset_session(phone)
        return (
            "⚠️ Esto suena como una urgencia.\n\n"
            "Llama al *SAMU 131* o acude al servicio de urgencias más cercano ahora mismo.\n\n"
            f"También puedes contactarnos:\n📞 *{CMC_TELEFONO}*\n☎️ *{CMC_TELEFONO_FIJO}*\n\n"
            "Si necesitas algo más, escribe *menú*."
        )

    # ── Urgencias soft (no-SAMU) por especialidad ─────────────────────────────
    # Situaciones clínicas no vitales pero que requieren atención rápida: la
    # dentista general / el flujo normal de agendamiento no resuelve a tiempo.
    # Derivamos directo a recepción con contexto para que coordine.
    # Formato: (señal, contexto). El match requiere QUE AMBOS aparezcan en tl.
    _URGENCIAS_SOFT = (
        # Ortodoncia
        (("alambre", "me pinchó", "me pincho", "me clavó", "me clavo",
          "me saca sangre", "me sangra", "suelto", "sueltos", "se safó", "se zafo"),
         ("bracket", "brácket", "brackets", "bráckets", "ortodoncia",
          "frenillos", "aparato dental", "aparato de los dientes"),
         "ortodoncia"),
        # Dental — diente/muela fracturado, prótesis rota, absceso
        (("se me partió", "se me partio", "se rompió", "se rompio", "fractur",
          "se me cayó un trozo", "se me salió", "se me salio", "no puedo comer",
          "absceso", "infla", "me revento"),
         ("muela", "diente", "dientes", "colmillo", "incisivo", "molar",
          "prótesis", "protesis", "placa dental", "corona"),
         "dental"),
    )
    if state != "HUMAN_TAKEOVER":
        for kws_sig, kws_ctx, etiqueta in _URGENCIAS_SOFT:
            if any(k in tl for k in kws_sig) and any(c in tl for c in kws_ctx):
                log_event(phone, "urgencia_soft", {"tipo": etiqueta, "texto": txt[:200]})
                return _derivar_humano(
                    phone=phone,
                    contexto=f"urgencia {etiqueta}: {txt[:160]}"
                )

    # ── Consent inline (Ley 19.628) ──────────────────────────────────────────
    # El consentimiento se registra cuando el paciente proporciona su RUT
    # (consentimiento tácito al compartir datos personales). NO bloqueamos al
    # inicio para evitar asustar a los pacientes (ver feedback de campo).

    # ── Revocación post-consent + derecho al olvido ───────────────────────────
    # Paciente ya consintió pero ahora escribe STOP / "borrar mis datos". Son
    # dos cosas distintas:
    #   - STOP / revocar      → revoca consent; deja de enviar marketing pero
    #                           los datos clínicos quedan (pueden ser necesarios).
    #   - "borrar mis datos"  → derecho al olvido (art. 12). Emite alerta al
    #                           admin para ejecutar DELETE /admin/api/patient.
    if phone != _doctor_phone:
        if tl in ("stop", "detener", "baja") or tl_norm in ("stop", "detener", "baja"):
            revoke_privacy_consent(phone)
            save_tag(phone, "marketing_opt_out")
            log_event(phone, "privacy_consent_revoked")
            reset_session(phone)
            return (
                "Listo 👍 No recibirás más mensajes de seguimiento ni campañas.\n\n"
                "Si quieres que borremos *todos* tus datos, escribe "
                "*borrar mis datos*.\n\n"
                "Para volver a recibir mensajes escribe *aceptar*."
            )
        if ("borrar mis datos" in tl_norm or "borrar mis datos" in tl
                or "derecho al olvido" in tl_norm):
            log_event(phone, "gdpr_deletion_requested", {"texto": txt[:240]})
            # Alerta al admin/doctor para ejecución manual (validación identidad)
            try:
                from resilience import spawn_task
                spawn_task(send_whatsapp(
                    ADMIN_ALERT_PHONE,
                    f"🔐 *Solicitud borrado de datos*\n\n"
                    f"📱 Paciente: {phone}\n"
                    f"📝 Texto: {txt[:200]}\n\n"
                    f"Valida identidad y ejecuta:\n"
                    f"`DELETE /admin/api/patient/{{rut}}`"
                ))
            except Exception as _e:
                log.warning("No pude notificar borrado al admin: %s", _e)
            return (
                "Recibida tu solicitud de borrado 🔐\n\n"
                "Para proteger tus datos vamos a *validar tu identidad* antes de "
                "ejecutarla. Un miembro del equipo se contactará contigo dentro "
                "de las próximas 48 horas (plazo legal: 30 días).\n\n"
                "Mientras tanto hemos pausado el envío de mensajes."
            )

    # ── Comandos globales ─────────────────────────────────────────────────────
    _COMANDOS_GLOBALES = ("menu", "menú", "inicio", "reiniciar", "volver", "hola", "menu_volver")
    # Si la recepcionista tomó la conversación, NO resetear por saludos/menu —
    # dejar que el handler de HUMAN_TAKEOVER registre el mensaje.
    _es_comando_reset = (tl in _COMANDOS_GLOBALES or tl_norm in _COMANDOS_GLOBALES
                        or tl in _SALUDOS_SET or tl_norm in _SALUDOS_SET)
    # Si el paciente está en flujo activo y escribe un saludo (no un comando
    # explícito como 'menu'/'reiniciar'), ofrecer retomar antes de resetear.
    _es_saludo_puro = (tl in _SALUDOS_SET or tl_norm in _SALUDOS_SET) and tl not in (
        "menu", "menú", "inicio", "reiniciar", "volver", "menu_volver"
    )
    _FLUJO_RETOMABLE = {
        "WAIT_SLOT", "WAIT_MODALIDAD", "WAIT_BOOKING_FOR",
        "WAIT_RUT_AGENDAR", "CONFIRMING_CITA",
        "WAIT_RUT_CANCELAR", "WAIT_CITA_CANCELAR", "CONFIRMING_CANCEL",
        "WAIT_RUT_REAGENDAR", "WAIT_CITA_REAGENDAR",
    }
    if _es_saludo_puro and state in _FLUJO_RETOMABLE and not data.get("_retomar_ofrecido"):
        data["_retomar_ofrecido"] = True
        save_session(phone, state, data)
        esp_retomar = data.get("especialidad") or data.get("quick_esp") or "tu cita"
        log_event(phone, "retomar_ofrecido", {"state": state, "esp": esp_retomar})
        return _btn_msg(
            f"¡Hola de nuevo! 👋\n\nTenías un trámite pendiente de *{esp_retomar}*. "
            "¿Retomamos donde quedaste o prefieres empezar de cero?",
            [
                {"id": "retomar_si", "title": "✅ Retomar"},
                {"id": "retomar_no", "title": "🔄 Empezar de cero"},
                {"id": "retomar_menu", "title": "📋 Ver menú"},
            ]
        )
    # Handler de los botones de retomar (llega antes del reset_session general)
    if tl in ("retomar_si",):
        data.pop("_retomar_ofrecido", None)
        save_session(phone, state, data)
        log_event(phone, "retomado", {"state": state})
        # Según el estado, reemitir el prompt específico
        if state in ("WAIT_SLOT", "WAIT_MODALIDAD"):
            esp_r = data.get("especialidad") or ""
            return f"Perfecto, seguimos agendando *{esp_r}*. Escribe el *número* del horario o *otro día* para cambiar de día."
        if state == "CONFIRMING_CITA":
            return "Retomamos tu confirmación. Escribe *sí* para confirmar la hora, o *no* para buscar otra."
        if state in ("WAIT_RUT_AGENDAR", "WAIT_RUT_CANCELAR", "WAIT_RUT_REAGENDAR"):
            return "Necesito tu *RUT* para continuar (ej: *12.345.678-9*)"
        if state in ("WAIT_CITA_CANCELAR", "WAIT_CITA_REAGENDAR"):
            return "Escribe el *número* de la cita que quieres cambiar/cancelar."
        return "Sigamos donde quedamos 👌 Escribe lo que necesitas."
    if tl in ("retomar_no", "retomar_menu"):
        log_event(phone, "retomar_rechazado", {"state": state})
        reset_session(phone)
        return _menu_msg()
    if _es_comando_reset and state != "HUMAN_TAKEOVER":
        reset_session(phone)
        if phone == _doctor_phone:
            # El modo se lee del tag, no de la sesión — sobrevive el reset
            doc_mode = _get_doctor_mode(phone)
            if doc_mode == "agente":
                return _menu_msg()
            if doc_mode == "asistente":
                return (
                    "👨‍⚕️ *Asistente Clínico* listo.\n"
                    "Escribe *modo* para cambiar."
                )
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

    # ── IDLE + hora suelta + snapshot reciente → reabrir WAIT_SLOT ──
    # Si el paciente vio una lista de horarios hace <60 min y ahora escribe
    # "10:30" (o cualquier variante), restauramos WAIT_SLOT con esos slots
    # para que el bloque de WAIT_SLOT encuentre la hora exacta.
    if state == "IDLE" and data.get("last_slots") and data.get("last_slots_ts"):
        try:
            from time_parser import parse_hora as _parse_hora_idle
            _ls = data["last_slots"]
            _ls_valido = (
                isinstance(_ls, list)
                and _ls
                and all(isinstance(s, dict) and s.get("hora_inicio") for s in _ls)
            )
            if _ls_valido and _parse_hora_idle(txt):
                _ts_snap = datetime.fromisoformat(data["last_slots_ts"])
                _edad = datetime.now(timezone.utc) - _ts_snap
                if _edad < timedelta(minutes=60):
                    data["todos_slots"] = _ls
                    data["slots"] = _ls[:5]
                    if data.get("last_especialidad"):
                        data["especialidad"] = data["last_especialidad"]
                    data.setdefault("fechas_vistas", [])
                    state = "WAIT_SLOT"
                    save_session(phone, "WAIT_SLOT", data)
                    log_event(phone, "hora_idle_recuperada", {"edad_min": int(_edad.total_seconds() / 60)})
        except Exception:
            pass

    # ── PRE-ROUTER UNIVERSAL para estados WAIT_* / CONFIRMING_* ──
    # Detecta cambios de tema y preguntas paralelas antes de que el handler
    # rígido del estado falle por no matchear patterns. Solo corre si el texto
    # no es una respuesta "obvia" al prompt actual (evita latencia y costo).
    if state.startswith("WAIT_") or state.startswith("CONFIRMING_"):
        try:
            _pre_resp = await _pre_router_wait(phone, txt, tl, state, data)
            if _pre_resp is not None:
                return _pre_resp
        except Exception as _e_pre:
            log.warning("pre-router excepción en state=%s: %s — fallback", state, _e_pre)

    # ── IDLE: detectar intención ──────────────────────────────────────────────
    if state == "IDLE":
        # ── Botones residuales de WAIT_SLOT que llegaron tarde (sesión expiró,
        # usuario volvió al menú pero el mensaje tardó en llegar). En vez de
        # devolver el menú genérico, relanzar el flujo de agendar. ──
        if tl in ("ver_otros", "ver_todos", "otro_dia", "otro_día",
                  "otro_prof", "confirmar_sugerido"):
            return await _iniciar_agendar(phone, data, None)

        # ── Closings conversacionales (no re-mostrar menú) ────────────────────
        # "gracias", "ok", "dale", "chao" tras un flujo completado — el paciente
        # está cerrando la conversación, no iniciando otra. Evita saludarlo de
        # cero con el menú cuando solo dice "ok".
        _CLOSINGS = {
            "gracias", "muchas gracias", "muchas grasias", "grasias",
            "gracia", "graciass", "graciasss", "thanks", "thx",
            "ok", "okey", "okay", "okey gracias", "ok gracias",
            "vale", "dale", "bueno", "perfecto", "listo", "listop",
            "super", "súper", "bacan", "bakan", "bacán", "genial",
            "ya", "ya ok", "ya gracias", "ya po", "ya listo",
            "chao", "chaito", "chau", "adios", "adiós", "bye",
            "hasta luego", "hasta pronto", "nos vemos",
            "no gracias", "no grasias", "pero no gracias",
            "muy amable", "muy amables", "excelente", "ta bien",
            "tá bien", "ta bueno", "tá bueno", "gracias igual",
        }
        # Strip de puntuación al final ("gracias!!", "ok.", "dale!") para que
        # match aún cuando el paciente cierra con énfasis. Sin esto, el bot
        # respondía con menú completo a "Gracias!!" después de takeover.
        _tl_clean = tl.rstrip("!.?,;:🙏✨💙🙌👋😊")
        _tl_norm_clean = tl_norm.rstrip("!.?,;:🙏✨💙🙌👋😊")
        if (tl_norm in _CLOSINGS or tl in _CLOSINGS
                or _tl_clean in _CLOSINGS or _tl_norm_clean in _CLOSINGS):
            log_event(phone, "idle_closing", {"txt": txt[:80]})
            return "¡Que estés muy bien! 👋"

        # ── Corrección de titular tras cita recién confirmada ─────────────────
        # Bug 2026-04-25 (56981328760, 13:29): la paciente confirmó hora
        # con su RUT y luego dijo "Per la hora es para mi hija". El bot la
        # llevó a quick_book (oferta nueva agenda) en vez de detectar que
        # quería corregir el TITULAR de la cita recién creada → terminó con
        # doble reserva. Detectar el patrón y derivar a humano.
        try:
            from datetime import datetime as _dt_titular, timezone as _tz_titular
            _last_book_ts = data.get("last_booking_ts")
            _es_post_confirm = False
            if _last_book_ts:
                try:
                    _delta = (_dt_titular.now(_tz_titular.utc)
                              - _dt_titular.fromisoformat(_last_book_ts))
                    _es_post_confirm = _delta.total_seconds() < 1800  # 30 min
                except Exception:
                    pass
            _CORRECCION_TITULAR_RE = re.compile(
                r"\b(la hora|esa hora|esta hora|la cita) (es )?para "
                r"(mi |un |una )?(hij[oa]|esposo|esposa|mam[aá]|pap[aá]|"
                r"hermano|hermana|nieto|nieta|pareja|pololo|polola|"
                r"abuelo|abuela|familiar|amig[oa])\b",
                re.IGNORECASE,
            )
            if _es_post_confirm and _CORRECCION_TITULAR_RE.search(txt):
                log_event(phone, "correccion_titular_post_confirm", {"txt": txt[:160]})
                save_session(phone, "HUMAN_TAKEOVER", data)
                return (
                    "Entendido 🙏 Una recepcionista corregirá los datos de "
                    "la hora que recién agendaste y te confirmará por acá.\n\n"
                    f"_Si es urgente: 📞 *{CMC_TELEFONO}*_"
                )
        except Exception:
            pass

        # ── Seguimiento de FAQ con sugerencia de agendar ──────────────────────
        # Debe ir ANTES de los atajos numéricos (1..4) porque aquí interpretamos
        # "1"/"sí"/botón como "agendar la especialidad ya sugerida en el FAQ".
        esp_sug_prev = data.get("especialidad_sugerida")
        # ── Defensa sistémica: payload de botón viejo sin contexto ────────────
        # El paciente clickeó un botón "Sí, agendar" / "Otros horarios" / etc.
        # pero la sesión expiró (timeout 30 min) o nunca se guardó el contexto.
        # Sin este handler caía al menú genérico y mostraba un saludo de
        # bienvenida confuso. Caso real 2026-04-28 (56931330787): paciente
        # clickeó "agendar_sugerido" tras un mensaje del bot y recibió 2 saludos
        # genéricos en lugar de retomar el agendamiento.
        if not esp_sug_prev and not data.get("slots"):
            _BOT_PAYLOADS_HUERFANOS = {
                "agendar_sugerido": "Esa opción de agendar ya no está activa 😔\n\n¿Qué *especialidad* necesitas? O escribe *menu* para ver las opciones.",
                "confirmar_sugerido": "La hora que te ofrecí ya no está disponible 😔\n\nEscribe la *especialidad* que necesitas y te busco hora nueva.",
                "ver_otros": "Las opciones que mostré ya no están activas. Escribe la *especialidad* que necesitas para empezar de nuevo 😊",
                "ver_todos": "Las opciones que mostré ya no están activas. Escribe la *especialidad* que necesitas para empezar de nuevo 😊",
                "otro_dia": "Tu búsqueda anterior expiró. Dime qué *especialidad* necesitas y te busco horas 😊",
                "otro_día": "Tu búsqueda anterior expiró. Dime qué *especialidad* necesitas y te busco horas 😊",
                "otro_prof": "Tu búsqueda anterior expiró. Dime qué *especialidad* necesitas y te busco horas 😊",
            }
            if tl in _BOT_PAYLOADS_HUERFANOS:
                log_event(phone, "payload_huerfano", {"payload": tl})
                save_session(phone, "IDLE", data)
                return _BOT_PAYLOADS_HUERFANOS[tl]
            # "no_agendar" sin contexto: silencio no, mejor cerrar amable
            if tl == "no_agendar":
                save_session(phone, "IDLE", data)
                return "Sin problema 😊 Si necesitas algo, escribe *menu*."
        if esp_sug_prev:
            if tl == "no_agendar" or tl in NEGACIONES or tl_norm in NEGACIONES:
                data.pop("especialidad_sugerida", None)
                save_session(phone, "IDLE", data)
                log_event(phone, "faq_agendar_rechazo", {"esp": esp_sug_prev})
                return (
                    "Sin problema 😊 Cuando lo necesites, estamos acá.\n"
                    "_Escribe *menu* para ver todas las opciones._"
                )
            # Aceptación implícita: paciente expresa intent de agendar (texto libre)
            # cuando hay especialidad sugerida → procesar como sí.
            # Cubre casos como "quisiera agendar porfavor", "agendame", "reservar hora",
            # "quiero la hora", etc. — frecuente cuando el paciente vuelve después de
            # ver la sugerencia y no responde con "sí" puro.
            _AGENDAR_KWS = (
                "agendar", "agenda", "agéndame", "agendame", "agendarme",
                "reserva", "reservar", "reservame", "resérvame",
                "quiero hora", "quiero la hora", "quiero una hora",
                "tomar hora", "tomar la hora", "darme hora",
            )
            _es_intent_agendar = any(kw in tl_norm for kw in _AGENDAR_KWS)
            if (tl == "agendar_sugerido" or txt == "1"
                or tl in AFIRMACIONES or tl_norm in AFIRMACIONES
                or _es_intent_agendar):
                data.pop("especialidad_sugerida", None)
                log_event(phone, "faq_agendar_acepto", {"esp": esp_sug_prev,
                                                          "via": "implicit" if _es_intent_agendar else "explicit"})
                perfil = get_profile(phone)
                if perfil:
                    data["rut_conocido"] = perfil["rut"]
                    data["nombre_conocido"] = perfil["nombre"]
                return await _iniciar_agendar(phone, data, esp_sug_prev)
            # Si pregunta por más opciones/temprano/otra hora, iniciar flujo completo
            # de agendar (WAIT_SLOT) para que vea múltiples horarios y pueda filtrar
            # por período ("temprano", "tarde", etc.). Antes caía al fallback genérico.
            _MAS_OPCIONES_KWS = (
                "mas temprano", "más temprano", "mas tarde", "más tarde",
                "mas tempranito", "más tempranito",
                "otra hora", "otras horas", "otro horario", "otros horarios",
                "mas opciones", "más opciones", "mas horas", "más horas",
                "mas horarios", "más horarios", "hay otra", "hay otro",
                "no habra hora", "no habrá hora", "no habran", "no habrán",
                "en la mañana", "en la manana", "por la mañana", "por la manana",
                "en la tarde", "por la tarde", "en la noche", "por la noche",
                "tendrá otra", "tendra otra", "tendrás otra", "tendras otra",
                "ver mas", "ver más", "ver todas", "ver todos",
            )
            if any(kw in tl_norm for kw in _MAS_OPCIONES_KWS):
                log_event(phone, "faq_agendar_mas_opciones", {"esp": esp_sug_prev, "txt": txt[:100]})
                data.pop("especialidad_sugerida", None)
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
            # Pide reseña Google (anti-spam: máx 1/90d)
            try:
                from resilience import spawn_task
                spawn_task(_send_review_request_if_due(phone, esp))
            except Exception:
                pass
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
                    from resilience import spawn_task
                    spawn_task(send_whatsapp(ADMIN_ALERT_PHONE, alerta))
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

        # ── Cross-sell ORL ↔ Fonoaudiología ────────────────────────────────
        if tl in ("xorlfono_si",):
            perfil = get_profile(phone)
            if perfil:
                data["rut_conocido"] = perfil["rut"]
                data["nombre_conocido"] = perfil["nombre"]
            # Determinar destino: si última cita es ORL → fono, si es fono → ORL
            ultima = get_ultima_cita_paciente(phone)
            esp_prev = (ultima or {}).get("especialidad", "").lower()
            destino = "fonoaudiología" if "otorrin" in esp_prev else "otorrinolaringología"
            log_event(phone, "crosssell_orl_fono_acepto", {"destino": destino})
            return await _iniciar_agendar(phone, data, destino)
        if tl == "xorlfono_no":
            log_event(phone, "crosssell_orl_fono_rechazo", {})
            return "Sin problema 😊 Cuando quieras, avísame.\n_Escribe *menu* para ver opciones._"

        # ── Cross-sell Odontología → Estética Facial ──────────────────────
        if tl == "xestetica_si":
            log_event(phone, "crosssell_odonto_estetica_acepto", {})
            perfil = get_profile(phone)
            if perfil:
                data["rut_conocido"] = perfil["rut"]
                data["nombre_conocido"] = perfil["nombre"]
            return await _iniciar_agendar(phone, data, "estética facial")
        if tl == "xestetica_info":
            log_event(phone, "crosssell_odonto_estetica_info", {})
            try:
                info = await respuesta_faq("¿qué procedimientos de estética facial hacen?")
            except Exception:
                info = None
            return (
                (info or
                 "En *estética facial* con la Dra. Valentina Fuentealba ofrecemos: "
                 "toxina botulínica, bioestimuladores, hilos tensores, "
                 "armonización facial y limpiezas profundas.")
                + "\n\n_Escribe *agendar estética* si quieres reservar hora._"
            )
        if tl == "xestetica_no":
            log_event(phone, "crosssell_odonto_estetica_rechazo", {})
            return "Entendido 😊 _Escribe *menu* cuando quieras volver._"

        # ── Cross-sell Medicina General → Chequeo preventivo ──────────────
        if tl == "xchequeo_si":
            log_event(phone, "crosssell_mg_chequeo_acepto", {})
            perfil = get_profile(phone)
            if perfil:
                data["rut_conocido"] = perfil["rut"]
                data["nombre_conocido"] = perfil["nombre"]
            return await _iniciar_agendar(phone, data, "medicina general")
        if tl == "xchequeo_no":
            log_event(phone, "crosssell_mg_chequeo_rechazo", {})
            return "Sin problema 😊 Cuando te haga sentido, avísame.\n_Escribe *menu* para ver opciones._"

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
                    try:
                        from resilience import spawn_task
                        spawn_task(_send_review_request_if_due(phone, esp))
                    except Exception:
                        pass
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
                            from resilience import spawn_task
                            spawn_task(send_whatsapp(ADMIN_ALERT_PHONE, alerta))
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
        # Skip triage si el texto menciona gestión de cita existente o
        # especialidad específica — esas intenciones deben ir a Claude.
        _TRIAGE_SKIP_KWS = (
            "dentista", "odontol", "ortodonci", "endodonc", "implante",
            "cancel", "anular", "reagend", "cambiar hora", "cambiar mi hora",
            "no puedo ir", "no puedo asistir", "no alcanzo", "cambio de hora",
            "otorrino", "kinesio", "kine", "psicolog", "nutricion",
            "matrona", "ecograf", "ginecolog", "cardiolog", "podolog",
            "fonoaud", "gastro",
            # Frases de gestión de cita — agregadas 2026-04-28 tras auditoría
            # (7 nomatch/7d con intención clara: "tengo hora hoy", "no podré
            # asistir", "horita hoy con el dr X", etc.).
            "tengo hora", "tengo una hora", "tengo cita", "tengo una cita",
            "una horita", "una hora", "mi hora", "mi cita",
            "no podre", "no podré", "no creo que", "no asistir", "no asistire",
            "no asistiré", "no llegaré", "no llegare", "no voy a poder",
            "no podre asistir", "no podré asistir",
            "esa hora", "esa cita", "agendado", "agendada",
            "voy a llegar tarde", "atrasado", "atrasada",
            "verificar mi hora", "confirmar mi", "confirmar hora",
            # Apellidos de profesionales (mención = gestión de cita, no síntoma)
            "abarca", "olavarria", "olavarría", "marquez", "márquez",
            "borrego", "millan", "millán", "barraza", "rejon", "rejón",
            "quijano", "burgos", "jimenez", "jiménez", "castillo",
            "fredes", "valdes", "valdés", "fuentealba", "armijo",
            "etcheverry", "pinto", "montalba", "rodriguez", "rodríguez",
            "arratia", "saraí", "sarai", "guevara", "pardo",
            # "horita" usado como diminutivo de "hora" (cita), muy común en CMC
            "horita",
        )
        _skip_triage = any(k in tl for k in _TRIAGE_SKIP_KWS)
        if len(txt) >= 10 and not txt.isdigit() and not _skip_triage:
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
                # Especialidad agendable → iniciar flujo de agendar con urgencia empática.
                especialidad_triage = triage.get("especialidad")
                if especialidad_triage:
                    perfil = get_profile(phone)
                    if perfil:
                        data["rut_conocido"] = perfil["rut"]
                        data["nombre_conocido"] = perfil["nombre"]
                    data["triage_motivo"] = triage.get("top_pathology")
                    # Mensaje de urgencia empática ANTES de iniciar agendamiento
                    await send_whatsapp(
                        phone,
                        f"Por lo que me cuentas, es importante que te evalúe "
                        f"un especialista en *{especialidad_triage}* pronto.\n\n"
                        "Te busco la hora más cercana disponible ahora mismo."
                    )
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

        # ── Shortcut local: mención a un profesional → agendar sin Claude ──
        # Cubre tres formas:
        #   A) Texto corto que es PRINCIPALMENTE el apellido del prof
        #      "Dr Márquez", "Dra Javiera", "con Olavarría", "la doctora Burgos"
        #   B) Apellido + verbo de acción explícito
        #      "Necesito hora con el doctor Olavarría", "agendar con Abarca"
        #   C) Apellido implicando "quiero con X"
        #      "me equivoqué quiero con el dr Márquez"
        # No dispara si el mensaje parece una pregunta sobre el profesional
        # ("quién es", "dónde atiende", "es bueno", etc.).
        _apellido_idle = _detectar_apellido_profesional(txt)
        if _apellido_idle:
            _PREGUNTAS_INFO_PROF = (
                "quien es", "quién es", "quien atiende", "quién atiende",
                "a que hora atiende", "a qué hora atiende",
                "donde atiende", "dónde atiende",
                "que dias atiende", "qué días atiende",
                "que dia atiende", "qué día atiende",
                "es buen", "es bueno", "es buena",
                "sabe de", "especialidad de", "que especialidad",
                "qué especialidad",
            )
            # Contra-señal: el paciente se presenta con su propio nombre.
            # Ej: "Soy Luis", "me llamo Daniela", "mi nombre es Rodrigo"
            # (coincide con nombres de pila de profesionales).
            _SELF_INTRO = (
                "soy ", "me llamo", "mi nombre es", "yo soy", "yo me llamo",
                "habla ", "le habla",
            )
            _es_pregunta_info = any(kw in tl for kw in _PREGUNTAS_INFO_PROF)
            _es_self_intro = any(kw in tl for kw in _SELF_INTRO)
            _tiene_verbo_accion = any(
                k in tl for k in (
                    "necesito", "quiero", "hora", "agendar", "me equivoque",
                    "me equivoqué", "reservar", "con el", "con la", "mejor con",
                    "tendra", "tendrá", "tiene", "disponible", "disponibilidad",
                    "atencion", "atención", "atiende",
                )
            )
            # Texto corto: pocas palabras significativas (típicamente
            # "dr marquez", "la javiera", "con olavarría", "doctor abarca")
            _palabras_utiles = [w for w in tl.split()
                                if len(w) >= 2 and w not in {"dr", "dra", "doctor",
                                                             "doctora", "con", "el",
                                                             "la", "los", "las", "y"}]
            _es_texto_corto = len(_palabras_utiles) <= 3
            if not _es_pregunta_info and not _es_self_intro and (_tiene_verbo_accion or _es_texto_corto):
                log_event(phone, "intent_detectado_apellido", {
                    "apellido": _apellido_idle,
                    "modo": "verbo" if _tiene_verbo_accion else "texto_corto",
                })
                return await _iniciar_agendar(phone, data, _apellido_idle)

        # ── Shortcut: frase de especialidad ("hora medico general") + intent
        # implícito → agendar sin Claude cuando la frase es inequívoca. ──
        _esp_idle = _detectar_especialidad_en_texto(txt)
        _ES_PREGUNTA_INFO = any(k in tl for k in (
            "realizan", "realiza", "hacen", "hace ",
            "tienen", "tiene ", "ofrecen", "ofrece",
            "cuanto", "cuánto", "precio", "valor", "vale", "bono",
            "cuesta", "costo",
        ))
        if _esp_idle and any(
            k in tl for k in (
                "hora", "agendar", "reservar", "necesito", "quiero",
                "tiene alguna", "tendra", "tendrá", "tendrán",
            )
        ) and not _ES_PREGUNTA_INFO:
            log_event(phone, "intent_detectado_local", {"esp": _esp_idle})
            return await _iniciar_agendar(phone, data, _esp_idle)
        # Pregunta "¿realizan X?" (existencia del servicio) con especialidad →
        # FAQ local antes de Claude. Robusto ante outages.
        # NO interceptar preguntas de precio — dejamos que Claude responda con
        # el arancel específico y Fonasa/particular.
        _PREGUNTA_EXISTENCIA = any(k in tl for k in (
            "realizan", "realiza", "hacen", "hace ",
            "tienen", "tiene ", "ofrecen", "ofrece",
        ))
        if _esp_idle and _PREGUNTA_EXISTENCIA:
            from claude_helper import _local_faq_fallback
            _faq_fb = _local_faq_fallback(txt)
            if _faq_fb:
                log_event(phone, "faq_local_hit", {"esp": _esp_idle})
                data["especialidad_sugerida"] = _esp_idle
                save_session(phone, "IDLE", data)
                return _btn_msg(
                    f"{_faq_fb}\n\n¿Te agendo en *{_esp_idle}*?",
                    [
                        {"id": "agendar_sugerido", "title": "✅ Sí, agendar"},
                        {"id": "no_agendar",      "title": "No por ahora"},
                    ]
                )

        # ── RUT suelto en IDLE (sin flujo activo): el paciente responde con
        # sólo su RUT esperando continuar. Ofrecerle las 3 opciones principales. ──
        _txt_stripped = txt.strip()
        if len(_txt_stripped) <= 15 and valid_rut(clean_rut(_txt_stripped)):
            data["rut_conocido"] = clean_rut(_txt_stripped)
            save_session(phone, "IDLE", data)
            return _btn_msg(
                "Recibí tu *RUT* 👌 ¿Qué necesitas hacer?",
                [
                    {"id": "1", "title": "Agendar hora"},
                    {"id": "3", "title": "Ver mis citas"},
                    {"id": "2", "title": "Cancelar cita"},
                ]
            )

        # ── Datos de paciente no solicitados: RUT + nombre o fecha en el mismo
        # mensaje → el paciente está enviando todo de una. Asumimos que quiere
        # agendar y arrancamos el flujo. Se basa en patrón de RUT chileno. ──
        _txt_multiline = "\n" in txt or ";" in txt or txt.count(",") >= 2
        if _txt_multiline and len(txt) > 30:
            import re as _re_rut
            _m_rut = _re_rut.search(r"\b(\d{1,2}[.]?\d{3}[.]?\d{3}[-]?[0-9kK])\b", txt)
            _tiene_nombre = bool(_re_rut.search(r"\b[A-ZÁÉÍÓÚÑ][a-záéíóúñ]+\s+[A-ZÁÉÍÓÚÑ][a-záéíóúñ]+", txt))
            _tiene_fecha = bool(_re_rut.search(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b|\bde \d{4}\b|\bde enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|octubre|noviembre|diciembre\b", txt, _re_rut.IGNORECASE))
            if _m_rut and (_tiene_nombre or _tiene_fecha):
                log_event(phone, "datos_paciente_no_solicitados", {"len": len(txt)})
                rut_hallado = clean_rut(_m_rut.group(1))
                if valid_rut(rut_hallado):
                    data["rut_sugerido"] = rut_hallado
                return (
                    "¡Gracias por enviarme tus datos! 🙌\n\n"
                    "Para agendar necesito saber *qué especialidad* quieres. "
                    "Elige una opción:\n\n"
                    "• Escribe *1* para agendar\n"
                    "• Escribe *menu* para ver todas las opciones\n\n"
                    "_Te derivaré con la recepcionista si prefieres registro manual._"
                )

        # ── Context pivot: si preguntó por info de una esp hace <5min y ahora
        # pregunta por disponibilidad/cupos/hora, re-enrutar a agendar. ──
        _ctx_esp = data.get("last_esp_context")
        _ctx_ts = data.get("last_esp_context_ts")
        if _ctx_esp and _ctx_ts:
            try:
                from datetime import datetime as _dt_ctx2
                _edad = _dt_ctx2.now(timezone.utc) - _dt_ctx2.fromisoformat(_ctx_ts)
                if _edad < timedelta(minutes=5):
                    _pivot_kw = ("cupo", "cupos", "disponib", "hora dispon",
                                 "horario", "cuando hay", "cuándo hay",
                                 "hay hora", "para cuando", "para cuándo",
                                 "en que horario", "en qué horario")
                    if any(k in tl for k in _pivot_kw):
                        data.pop("last_esp_context", None)
                        data.pop("last_esp_context_ts", None)
                        log_event(phone, "ctx_pivot_agendar", {"esp": _ctx_esp})
                        return await _iniciar_agendar(phone, data, _ctx_esp)
            except Exception:
                pass

        # Detección previa: frases sobre cita EXISTENTE deben ir a
        # reagendar/cancelar, no a agendar nueva. Claude a veces clasifica
        # tenia una hora con la dentista el sabado como intent=agendar.
        # Caso 56975811662 2026-04-23 22:18.
        _CITA_EXISTENTE_RE = re.compile(
            r"\b(tenia|tenía) (una )?hora\b|"
            r"\bmi hora (de|del|para)\b|"
            r"\bmi cita (de|del|para)\b|"
            r"\btengo (una )?hora (el|para el)\b|"
            r"\bagend[eé] (una )?hora\b",
            re.IGNORECASE,
        )
        if _CITA_EXISTENTE_RE.search(txt) and not any(p in tl for p in ("agendar", "quiero agendar", "quiero una hora nueva")):
            log_event(phone, "intent_cita_existente_detectado", {"texto": txt[:120]})
            return await _iniciar_reagendar(phone, data)

        # ── Pregunta de días/horarios de atención por especialidad ──────────
        # Caso real 2026-04-28 (56958462692): paciente preguntó "Que día
        # atiende el otorrino?" y bot respondió con horario genérico del CMC
        # (lunes-viernes 08-21) inventado por Claude Haiku, en lugar del
        # horario REAL del Dr. Borrego (lun-mié 16-20). Fix sistémico: cortar
        # ANTES de Claude, consultar Medilink directo.
        _PREGUNTA_HORARIO_RE = (
            "que dia atiende", "qué día atiende", "que dias atiende", "qué días atiende",
            "cuando atiende", "cuándo atiende",
            "que dia trabaja", "qué día trabaja", "que dias trabaja", "qué días trabaja",
            "cuando viene", "cuándo viene",
            "que dia viene", "qué día viene", "que dias viene", "qué días viene",
            "horario del", "horario de la", "horario de los",
            "que horario tiene", "qué horario tiene",
            "a que hora atiende", "a qué hora atiende",
            "atiende los", "atiende el día", "atiende el dia",
        )
        if any(p in tl for p in _PREGUNTA_HORARIO_RE):
            # Detectar especialidad o apellido del profesional
            _esp_h = _detectar_especialidad_en_texto(txt) or _detectar_apellido_profesional(txt)
            if _esp_h:
                _resp_h = await _responder_horario_por_especialidad(_esp_h)
                if _resp_h:
                    log_event(phone, "horario_consultado", {"esp": _esp_h, "fuente": "medilink"})
                    return _resp_h

        result = await detect_intent(txt)
        intent = result.get("intent", "otro")
        log_event(phone, "intent_detectado", {"intent": intent, "esp": result.get("especialidad")})

        # ── Defensa sistémica: fallback loop counter ─────────────────────────
        # Si el bot devuelve N veces seguidas intent="otro" / "menu" sin avanzar
        # el flow, escalar a HUMAN_TAKEOVER. Caso real 2026-04-28 (56971038302):
        # bot mandó 4 menús distintos en 26 segundos sin entender al paciente.
        if intent in ("otro", "menu"):
            cnt_otro = int(data.get("fallback_otro_count", 0)) + 1
            data["fallback_otro_count"] = cnt_otro
            if cnt_otro >= 3:
                log_event(phone, "fallback_loop_escalado", {"count": cnt_otro})
                data["handoff_reason"] = "fallback_loop"
                data["fallback_otro_count"] = 0
                save_session(phone, "HUMAN_TAKEOVER", data)
                return (
                    "Disculpa, no estoy entendiendo bien tu consulta 😔\n\n"
                    "Te conecto con una recepcionista para que te ayude personalmente.\n"
                    f"📞 *{CMC_TELEFONO}* · ☎️ *{CMC_TELEFONO_FIJO}*"
                )
            save_session(phone, state, data)
        else:
            # Avanzó del fallback — limpiar contador
            if data.get("fallback_otro_count"):
                data["fallback_otro_count"] = 0
                save_session(phone, state, data)

        # ── Saludo / menu → devolver menú corto con botones (sin preguntas largas) ──
        if intent == "menu":
            return _menu_msg()

        if intent == "agendar":
            especialidad = result.get("especialidad")
            # Validar que la "especialidad" no sea un APELLIDO de profesional
            # alucinado por Claude. Si Claude retornó "jimenez"/"abarca"/etc.
            # pero el texto NO menciona el apellido, descartar y usar
            # detector local. Caso real 2026-04-28 (56993584481): paciente
            # dijo "Tiene hora para médico mañana?", Claude retornó "jimenez"
            # → bot ofreció odontología en vez de medicina general.
            if especialidad:
                esp_norm = especialidad.lower().strip()
                if esp_norm in _APELLIDOS_INDIVIDUALES_KEYS:
                    txt_norm_apellido = _normalizar_para_apellido(txt) or ""
                    if esp_norm not in txt_norm_apellido:
                        # Apellido no está en el texto — Claude alucinó. Fallback.
                        log_event(phone, "esp_apellido_alucinada", {"esp_claude": esp_norm, "txt": txt[:120]})
                        especialidad_fb = _detectar_especialidad_en_texto(txt)
                        if especialidad_fb and especialidad_fb.lower() not in _APELLIDOS_INDIVIDUALES_KEYS:
                            especialidad = especialidad_fb
                        else:
                            especialidad = None
            log_event(phone, "intent_agendar", {"especialidad": especialidad})
            # Detectar preferencia de fecha en el mensaje ("mañana", "pasado mañana",
            # "viernes", etc.) y guardar en data para que _iniciar_agendar la use.
            # Caso real 2026-04-23: Una horita para mañana con el Dr. Olavarria →
            # bot ignoraba "mañana" y daba slot de HOY.
            from datetime import datetime as _dt_fp, timedelta as _td_fp
            _hoy_cl = _dt_fp.now(_CHILE_TZ).date()
            _fp_tl = txt.lower()
            if "pasado mañana" in _fp_tl or "pasado manana" in _fp_tl:
                data["fecha_preferida"] = (_hoy_cl + _td_fp(days=2)).strftime("%Y-%m-%d")
            elif ("para mañana" in _fp_tl or "para manana" in _fp_tl
                  or " mañana" in _fp_tl or " manana" in _fp_tl):
                # "en la mañana" / "por la mañana" son franja, no fecha
                if not any(fr in _fp_tl for fr in ("en la mañana", "en la manana",
                                                    "por la mañana", "por la manana")):
                    data["fecha_preferida"] = (_hoy_cl + _td_fp(days=1)).strftime("%Y-%m-%d")
            # Pre-fill RUT si el paciente ya agendó antes
            perfil = get_profile(phone)
            if perfil:
                data["rut_conocido"] = perfil["rut"]
                data["nombre_conocido"] = perfil["nombre"]
            # Quick-book: paciente conocido → ofrecer "¿agendo otra hora como
            # la última vez?" antes del flujo estándar. Dispara en 2 casos:
            #   a) No hay especialidad explícita (Claude no la extrajo)
            #   b) Especialidad coincide con la última cita → proponemos slot
            #      inmediato (mismo doctor) para reducir 4-6 pasos a 2.
            # Antes el bug: solo (a), pero Claude casi siempre infiere esp →
            # el quick-book nunca disparaba (0 ofertas en 14 días).
            if perfil:
                ultima = get_ultima_cita_paciente(phone)
                esp_ultima = (ultima or {}).get("especialidad", "")
                esp_norm = (especialidad or "").lower().strip()
                esp_ultima_norm = esp_ultima.lower().strip()
                _esp_match = (
                    not especialidad
                    or esp_norm == esp_ultima_norm
                    or (esp_norm and esp_ultima_norm and
                        (esp_norm in esp_ultima_norm or esp_ultima_norm in esp_norm))
                )
                if esp_ultima and _esp_match:
                    prof_ultima = (ultima or {}).get("profesional", "") or ""
                    data["quick_esp"] = esp_ultima
                    data["quick_prof"] = prof_ultima
                    save_session(phone, "WAIT_QUICK_BOOK", data)
                    log_event(phone, "quick_book_offered", {
                        "especialidad": esp_ultima,
                        "esp_claude": especialidad or None,
                    })
                    nombre_corto = _first_name(perfil.get("nombre"))
                    saludo = f"¡Hola de nuevo, *{nombre_corto}*! ⚡\n\n" if nombre_corto else "⚡ "
                    con_prof = f" con *{prof_ultima}*" if prof_ultima else ""
                    return _btn_msg(
                        f"{saludo}Vi que tu última visita fue de *{esp_ultima}*{con_prof}.\n\n"
                        f"¿Te agendo otra hora de lo mismo?",
                        [
                            {"id": "quick_yes", "title": "⚡ Sí, agendar"},
                            {"id": "quick_other", "title": "🔄 Otra especialidad"},
                            {"id": "quick_cancel", "title": "✋ Ahora no"},
                        ]
                    )
            # Detectar "para hoy/mañana" en el mensaje y propagar al agendar.
            # Si paciente dice "una hora con kine para hoy" y no hay slots hoy,
            # _iniciar_agendar debe avisarle explícitamente en vez de mostrar
            # mañana sin contexto. Caso real 2026-04-28 (Norma Muñoz) +
            # CLAUDE.md pendiente #1 (caso María 56968621918).
            _fp = _detectar_fecha_pedida_idle(txt)
            if _fp:
                data["fecha_pedida_idle"] = _fp
            return await _iniciar_agendar(phone, data, especialidad)

        if intent == "reagendar":
            return await _iniciar_reagendar(phone, data)

        if intent == "cancelar":
            return await _iniciar_cancelar(phone, data, txt=txt)

        if intent == "ver_reservas":
            return await _iniciar_ver(phone, data)

        # Atajo conversacional: paciente pregunta si su cita de HOY sigue en pie
        _tl_confirm = txt.lower()
        _CONFIRM_HOY = ("se confirma", "sigue en pie", "confirman", "confirma hoy",
                        "mi hora para hoy", "mi hora de hoy", "mi cita de hoy",
                        "mi hora sigue", "mi cita sigue")
        if any(p in _tl_confirm for p in _CONFIRM_HOY):
            perfil_c = get_profile(phone)
            if perfil_c and perfil_c.get("rut") and not is_medilink_down():
                try:
                    pac_c = await buscar_paciente(perfil_c["rut"])
                except Exception:
                    pac_c = None
                if pac_c:
                    try:
                        citas_c = await listar_citas_paciente(pac_c["id"], rut=pac_c.get("rut")) or []
                    except Exception:
                        citas_c = []
                    hoy_str = datetime.now(_CHILE_TZ).date().strftime("%Y-%m-%d")
                    citas_hoy = [c for c in citas_c if c.get("fecha") == hoy_str]
                    if citas_hoy:
                        c0 = citas_hoy[0]
                        return (
                            f"Sí, tu hora de hoy está confirmada ✅\n\n"
                            f"🏥 *{c0.get('especialidad','')}* — {c0.get('profesional','')}\n"
                            f"🕐 *{c0.get('hora_inicio','')[:5]}*\n\n"
                            f"📍 Monsalve 102, Carampangue.\n"
                            f"_Llega 15 min antes con tu cédula._"
                        )
                    return (
                        "No veo una cita tuya para hoy 🤔\n\n"
                        "¿Quieres que te muestre tus próximas citas? Escribe *ver mis citas*."
                    )

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
            # Override: si Claude no detectó especialidad, buscarla en el texto crudo
            # (detecta apellidos de profesionales y términos como "médico familiar")
            # Apellido explícito tiene prioridad sobre especialidad genérica de Claude
            _ap_explicito_disp = _detectar_apellido_profesional(txt)
            especialidad = _ap_explicito_disp or result.get("especialidad") or _detectar_especialidad_en_texto(txt)
            # Si tenemos especialidad pero consultar_proxima_fecha falla, redirigir
            # al flujo completo de agendar (que busca día por día) en vez de caer
            # al fallback feo 'dime qué especialidad'.
            if especialidad:
                try:
                    _fecha_prox = await consultar_proxima_fecha(especialidad)
                except Exception:
                    _fecha_prox = None
                if not _fecha_prox:
                    # Sin fecha inmediata → lanzar flujo completo de agendar
                    return await _iniciar_agendar(phone, data, especialidad)
            if especialidad:
                fecha = await consultar_proxima_fecha(especialidad)
                if fecha:
                    data["especialidad_sugerida"] = especialidad.lower()
                    save_session(phone, "IDLE", data)
                    return _btn_msg(
                        f"Sí, para *{especialidad}* hay hora disponible el *{fecha}* 📅\n\n"
                        "¿Te la reservo?",
                        [
                            {"id": "agendar_sugerido", "title": "✅ Sí, agendar"},
                            {"id": "no_agendar",      "title": "No por ahora"},
                        ]
                    )
            # Fallback: apellido fuzzy (caso 56964044338: labarria pñ -> olavarria)
            _ap_fb = _detectar_apellido_profesional(txt)
            if _ap_fb:
                return await _iniciar_agendar(phone, data, _ap_fb)
            return (
                "Para consultar disponibilidad, dime qué especialidad necesitas 😊\n\n"
                f"O llama a recepción: 📞 *{CMC_TELEFONO}*"
            )

        if intent in ("precio", "info"):
            # Guardar especialidad mencionada en contexto con TTL 5min.
            # Permite que "¿Y cuando hay cupos?" en el siguiente turno sepa
            # que hablamos de eso. Caso 56937785271 2026-04-23 18:52.
            _esp_ctx = result.get("especialidad")
            if _esp_ctx:
                from datetime import datetime as _dt_ctx
                data["last_esp_context"] = _esp_ctx
                data["last_esp_context_ts"] = _dt_ctx.now(timezone.utc).isoformat()
                save_session(phone, "IDLE", data)
            resp = result.get("respuesta_directa") or await respuesta_faq(txt)
            esp_sug = (result.get("especialidad") or "").strip()
            # Si Claude infirió una especialidad, intentamos mostrar el próximo slot
            # inline + botón para agendar directo.
            if esp_sug and not is_medilink_down():
                try:
                    esp_lower = esp_sug.lower()
                    # Detectar si la especialidad no existe en nuestro catálogo
                    from medilink import _ids_para_especialidad as _ids_chk
                    if not _ids_chk(esp_lower):
                        save_demanda_no_disponible(phone, esp_sug, "especialidad")
                        log_event(phone, "demanda_no_disponible",
                                  {"solicitud": esp_sug, "tipo": "info"})
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
                        "¿Te la reservo?",
                        [
                            {"id": "agendar_sugerido", "title": "✅ Sí, agendar"},
                            {"id": "no_agendar",      "title": "No por ahora"},
                        ]
                    )
                # Fallback: guardamos la especialidad igual para que "sí" funcione
                if esp_lower:
                    data["especialidad_sugerida"] = esp_lower
                    save_session(phone, "IDLE", data)
                    return _btn_msg(
                        f"{resp}\n\n¿Te agendo en *{esp_sug}*?",
                        [
                            {"id": "agendar_sugerido", "title": "✅ Sí, agendar"},
                            {"id": "no_agendar",      "title": "No por ahora"},
                        ]
                    )
            return _btn_msg(
                f"{resp}\n\n{DISCLAIMER}",
                [
                    {"id": "1", "title": "📅 Agendar hora"},
                    {"id": "menu_volver", "title": "Ver menú"},
                ]
            )

        # intent "otro" — si Claude produjo una respuesta útil (p.ej. una
        # emergencia que se filtró del detector léxico), la mostramos con
        # el disclaimer y NO derivamos a recepción como si fuera un trámite.
        resp_otro = (result.get("respuesta_directa") or "").strip()
        if resp_otro:
            return f"{resp_otro}\n\n{DISCLAIMER}"
        # Override fallback: antes de caer al menú, intentar rescatar la
        # intención del paciente.
        if len(txt) >= 10:
            # 1) ¿Menciona apellido/especialidad específica? → flujo agendar
            esp_hint = _detectar_apellido_profesional(txt) or _detectar_especialidad_en_texto(txt)
            if esp_hint:
                log_event(phone, "fallback_esp_detectada", {"esp": esp_hint, "txt": txt[:120]})
                return await _iniciar_agendar(phone, data, esp_hint)
            # 1b) Intención clara de agendar sin especialidad → iniciar flujo
            # agendar que pregunta especialidad. Ej: "Necesito una hora para
            # mi hijo", "quiero agendar hora", "quiero pedir hora"
            _tl_book = txt.lower()
            _VERBO_AGENDAR = (
                "agendar", "reservar", "tomar hora", "pedir hora",
            )
            _HORA_NOUN_BOOK = any(k in _tl_book for k in (
                "hora medica", "hora médica", "hora para",
                "una hora", "reservar una", "agendar una",
                "agendar hora", "pedir una hora",
            ))
            if any(v in _tl_book for v in _VERBO_AGENDAR) or _HORA_NOUN_BOOK:
                log_event(phone, "fallback_agendar_sin_esp", {"txt": txt[:120]})
                return await _iniciar_agendar(phone, data, None)
            # 1c) Intención explícita de hablar con recepción → derivar humano
            if any(k in _tl_book for k in (
                "hablar con recepcion", "hablar con recepción",
                "hablar con alguien", "hablar con humano",
                "hablar con persona", "atencion humana", "atención humana",
            )):
                log_event(phone, "fallback_humano", {"txt": txt[:120]})
                return _derivar_humano(phone=phone, contexto=txt)
            # 1d) Reagendar / cancelar por texto libre
            if any(k in _tl_book for k in (
                "cambiar hora", "cambiar cita", "cambiar mi hora",
                "mover hora", "mover cita", "reagendar",
                "modificar hora", "modificar cita", "modificar la hora",
                "cambiar de hora", "cambiar horario",
            )):
                log_event(phone, "fallback_reagendar", {"txt": txt[:120]})
                return await _iniciar_reagendar(phone, data)
            if any(k in _tl_book for k in (
                "cancelar mi hora", "cancelar hora", "cancelar cita",
                "anular hora", "anular cita",
            )):
                log_event(phone, "fallback_cancelar", {"txt": txt[:120]})
                return await _iniciar_cancelar(phone, data, txt=txt)
            # 2) Si NO hay palabra de acción CLARA de reserva, probar FAQ.
            #    "consulta" es ambiguo (noun/verb) — no bloquea FAQ.
            #    Si hay acción clara, el paciente ya está en flujo conocido →
            #    dejar que caiga al menú (muestra las especialidades).
            _tl_fb = txt.lower()
            _ACCION_KW = ("agendar", "reservar", "reagendar", "cancelar", "mover",
                          "cambiar", "quiero hora", "quiero cita",
                          "pedir hora", "tomar hora")
            _es_accion = any(k in _tl_fb for k in _ACCION_KW)
            if not _es_accion:
                # Primero intentar FAQ local (sin red) → robusto ante outages
                try:
                    from claude_helper import _local_faq_fallback
                    _local_fb = _local_faq_fallback(txt)
                    if _local_fb:
                        log_event(phone, "fallback_faq_local", {"txt": txt[:120]})
                        return f"{_local_fb}\n\n_Escribe *menu* si prefieres ver las opciones._"
                except Exception:
                    pass
                # Si no matchea local, llamar Claude FAQ
                try:
                    faq_resp = await respuesta_faq(txt)
                    if faq_resp and len(faq_resp) > 20:
                        log_event(phone, "fallback_faq", {"txt": txt[:120]})
                        return f"{faq_resp}\n\n_Escribe *menu* si prefieres ver las opciones._"
                except Exception:
                    pass
        # Fallback final (saludo o input muy corto) → mostrar menú
        return _menu_msg()

    # ── WAIT_DURACION_MASOTERAPIA ──────────────────────────────────────────────
    if state == "WAIT_DURACION_MASOTERAPIA":
        # Matchear número exacto o texto escrito
        num = re.findall(r"\b(20|40)\b", txt)
        _es_20 = tl == "maso_20" or (num and num[0] == "20") or "veinte" in tl
        _es_40 = tl == "maso_40" or (num and num[0] == "40") or "cuarenta" in tl
        if _es_20:
            duracion_maso = 20
        elif _es_40:
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
            f"Te encontré hora ✨\n\n"
            f"🏥 *Masoterapia* — {mejor['profesional']}\n"
            f"📅 *{mejor['fecha_display']}*\n"
            f"🕐 *{mejor['hora_inicio'][:5]}* ({duracion_maso} min) ⭐\n"
            f"{precio_bloque}\n"
            "¿Te la reservo?",
            [
                {"id": "confirmar_sugerido", "title": "✅ Sí, esa hora"},
                {"id": "ver_otros",          "title": "📋 Otros horarios"},
                {"id": "otro_dia",           "title": "📅 Otro día"},
            ]
        )

    # ── WAIT_QUICK_BOOK ───────────────────────────────────────────────────────
    # Oferta "agendar otra hora como la última vez" para pacientes conocidos.
    # 3 botones: sí / otra especialidad / ahora no. Cualquier otro texto cae al
    # detector de intent general (permite "cancelar", "ver reservas", etc.).
    if state == "WAIT_QUICK_BOOK":
        tl = txt.strip().lower()
        if tl in ("quick_yes", "si", "sí", "1", "agendar", "ok", "dale"):
            esp = data.get("quick_esp", "")
            log_event(phone, "quick_book_accepted", {"especialidad": esp})
            # Limpiar flags del quick-book antes de pasar al flujo estándar
            data.pop("quick_esp", None)
            data.pop("quick_prof", None)
            return await _iniciar_agendar(phone, data, esp or None)
        if tl in ("quick_other", "otra", "otra especialidad", "2", "cambiar"):
            log_event(phone, "quick_book_other")
            data.pop("quick_esp", None)
            data.pop("quick_prof", None)
            return await _iniciar_agendar(phone, data, None)
        if tl in ("quick_cancel", "ahora no", "no", "3", "cancelar", "menu"):
            log_event(phone, "quick_book_declined")
            reset_session(phone)
            return "Sin problema 😊 Escribe *menu* cuando quieras retomar."
        # Texto libre → re-detectar intent (permite decir "quiero ver mis reservas")
        result = await detect_intent(txt)
        intent = result.get("intent", "otro")
        if intent == "agendar":
            esp_nuevo = result.get("especialidad") or data.get("quick_esp")
            data.pop("quick_esp", None)
            data.pop("quick_prof", None)
            return await _iniciar_agendar(phone, data, esp_nuevo)
        if intent == "cancelar":
            reset_session(phone)
            return await _iniciar_cancelar(phone, {})
        if intent == "ver_reservas":
            reset_session(phone)
            return await _iniciar_ver(phone, {})
        # Si no entendimos, reiterar las opciones
        save_session(phone, "WAIT_QUICK_BOOK", data)
        return _btn_msg(
            "Elige una opción 👇",
            [
                {"id": "quick_yes", "title": "⚡ Sí, agendar"},
                {"id": "quick_other", "title": "🔄 Otra especialidad"},
                {"id": "quick_cancel", "title": "✋ Ahora no"},
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
            # 1) fallback local por apellido o frase conocida (ahorra Claude call)
            apellido_loc = _detectar_apellido_profesional(txt)
            if apellido_loc:
                especialidad_candidata = apellido_loc
            else:
                esp_frase = _detectar_especialidad_en_texto(txt)
                if esp_frase:
                    especialidad_candidata = esp_frase
                else:
                    # 2) último recurso: Claude
                    result = await detect_intent(txt)
                    especialidad_candidata = result.get("especialidad") or especialidad_candidata
        # Si venimos del flujo de lista de espera, redirigir al confirming
        if data.pop("from_waitlist", False):
            return await _iniciar_waitlist(phone, data, especialidad_candidata)
        return await _iniciar_agendar(phone, data, especialidad_candidata)

    # ── WAIT_SLOT ─────────────────────────────────────────────────────────────
    if state == "WAIT_SLOT":
        # Escape universal: botón motivo_* del menú inicial llega aquí
        # (paciente se devolvió al menú y tocó un botón). Reset + re-dispatch.
        if txt.startswith("motivo_"):
            reset_session(phone)
            return await handle_message(phone, txt, {"state": "IDLE", "data": {}})
        slots_mostrados = data.get("slots", [])          # los que ve el paciente ahora
        todos_slots     = data.get("todos_slots", slots_mostrados)  # todos del día
        fechas_vistas   = data.get("fechas_vistas", [])
        especialidad    = data.get("especialidad", "")
        fecha_actual    = todos_slots[0]["fecha"] if todos_slots else None
        # tl_norm_slot: normalizado usado por todo el handler. Definido al inicio
        # porque bloques tempranos (mes/fecha/semana) lo referencian antes del
        # punto donde históricamente se asignaba (~línea 3140). Causaba NameError
        # crashes. Caso real 2026-04-23: 15 crashes en pacientes que escribieron
        # "20hrs", botón "otro_dia", "6", frases con mes antes de llegar a 3140.
        tl_norm_slot = txt.lower().strip()

        # Respuesta al sugerido proactivo (botón o texto libre "si"/"sí"/"confirmo"/...)
        # Afirmación libre: "puedo reservar?", "sí reservalo", "reserva esa hora",
        # "agenda esa", "tomo esa hora", etc. Caso real 2026-04-28
        # (fb_27066996906237198): bot ofreció Podología 14:00, paciente preguntó
        # "¿Puedo reservar una cita?" como confirmación implícita y el bot
        # reseteó el flow con "Claro, te ayudo a agendar 😊".
        _afirm_libre = (
            ("reserv" in tl or "agenda" in tl or "tomo" in tl or "tomar" in tl
             or "confirm" in tl or "esa hora" in tl or "esa hora me sirve" in tl)
            and not any(neg in tl for neg in (
                "no reserv", "no quiero reserv", "no agenda",
                "no la reserv", "no me sirve", "no gracias",
            ))
        )
        if (tl == "confirmar_sugerido" or tl in AFIRMACIONES or tl_norm in AFIRMACIONES or _afirm_libre) and slots_mostrados:
            # Si el paciente pidió explicitamente otro profesional antes y los
            # slots mostrados NO son de él, preferir uno que sí lo sea.
            _pedido = data.get("prof_pedido_explicito")
            if _pedido:
                _slot_pedido = next((s for s in slots_mostrados if s.get("id_profesional") == _pedido), None)
                if _slot_pedido:
                    data.pop("prof_pedido_explicito", None)
                    return await _slot_confirmed(phone, data, _slot_pedido)
                # No hay slot del doctor pedido → avisar antes de confirmar
                from medilink import PROFESIONALES as _PROFS_EX
                nombre_p = _PROFS_EX.get(int(_pedido), {}).get("nombre", "ese doctor")
                nombre_s = slots_mostrados[0].get("profesional", "otro doctor")
                data.pop("prof_pedido_explicito", None)
                save_session(phone, "WAIT_SLOT", data)
                return (
                    f"No encontré cupo con *{nombre_p}* en los próximos días 😕{chr(92)}n{chr(92)}n"
                    f"¿Te sirve con *{nombre_s}* (mismo día y hora)? Responde *sí* o escribe *otro día*."
                )
            slot = slots_mostrados[0]
            return await _slot_confirmed(phone, data, slot)

        # Pregunta-afirmación implícita cuando hay 1 solo slot mostrado:
        # Tendra hora disponible? / hay cupos? / tiene hora? → paciente está
        # confirmando implícitamente el slot único ofrecido. Caso 56966283335.
        if len(slots_mostrados) == 1:
            _CONFIRM_IMPLICITO = (
                "tendra", "tendrá", "tiene hora", "hay cupo", "hay cupos",
                "esta disponible", "está disponible", "hay disponible",
                "alguna horita disponible", "tendra alguna", "tendrá alguna",
                "hay hora",
            )
            if any(k in tl for k in _CONFIRM_IMPLICITO):
                return await _slot_confirmed(phone, data, slots_mostrados[0])
        # Payload del botón "Sí, esa hora" llegó pero se perdieron los slots de sesión
        # (sesión expiró, mensaje demorado, etc.) → re-buscar en vez de ignorar.
        if tl == "confirmar_sugerido" and not slots_mostrados:
            reset_session(phone)
            return await _iniciar_agendar(phone, {}, especialidad or None)
        if tl == "ver_otros":
            if especialidad in _ESPECIALIDADES_EXPANSION:
                return await _handle_expansion(phone, data, slots_mostrados, todos_slots,
                                               data.get("expansion_stage", 0), fecha_actual)
            # Defensa sistémica: si solo hay 1 slot total (ya mostrado), no
            # tiene sentido "ver_otros" del mismo día — debemos expandir a otro
            # día o profesional. Caso real 2026-04-28 (56934363158): bot ofreció
            # Dr. Abarca 08:00, paciente clickeó "ver_otros", bot mostró el
            # mismo slot duplicado.
            if len(todos_slots or []) <= 1:
                # Buscar slots de OTROS días para esta especialidad
                from medilink import buscar_primer_dia
                fechas_vistas = data.get("fechas_vistas") or []
                if not isinstance(fechas_vistas, list):
                    fechas_vistas = list(fechas_vistas)
                try:
                    smart_x, todos_x = await buscar_primer_dia(
                        especialidad,
                        excluir=fechas_vistas,
                    )
                except Exception:
                    smart_x, todos_x = [], []
                if todos_x:
                    nueva_fecha = todos_x[0].get("fecha")
                    if nueva_fecha and nueva_fecha not in fechas_vistas:
                        fechas_vistas.append(nueva_fecha)
                    data["slots"] = (smart_x or todos_x)[:5]
                    data["todos_slots"] = todos_x
                    data["fechas_vistas"] = fechas_vistas
                    save_session(phone, "WAIT_SLOT", data)
                    return _format_slots((smart_x or todos_x)[:5], mostrar_todos=False)
                return (
                    "Esta era la única hora que tenía disponible para esta especialidad 😕\n\n"
                    "Escribe *otro día* o *llamar recepción* para más opciones."
                )
            # Para especialidades sin expansion-stages: mostrar TODOS los slots del día
            # (no los mismos 5 ya vistos — eso era el bug que dejaba el botón inútil).
            data["slots"] = todos_slots
            save_session(phone, "WAIT_SLOT", data)
            return _format_slots(todos_slots, mostrar_todos=True)

        # "Otro profesional" → muestra slots del/los otro(s) doctor(es) de la especialidad
        if tl == "otro_prof":
            from medilink import _ids_para_especialidad
            prof_sugerido_id = data.get("prof_sugerido_id")
            ids_esp = _ids_para_especialidad(especialidad)
            if especialidad in _ESP_MED_GENERAL:
                ids_esp = list(_MED_GENERAL_IDS)  # [73, 1, 13] = Abarca, Olavarría, Márquez
            # Tracking de profesionales vistos — evita loops entre los mismos 2
            profs_vistos = set(data.get("profs_vistos", []))
            if prof_sugerido_id:
                profs_vistos.add(prof_sugerido_id)
            otros_ids = [i for i in ids_esp if i not in profs_vistos]
            # Si ya vio a todos los "primarios" pero aún hay profesionales adicionales
            # no cargados (caso MG: Márquez como overflow), incluirlos explícitamente.
            if not otros_ids and especialidad in _ESP_MED_GENERAL:
                otros_ids = [_MED_OVERFLOW_ID] if _MED_OVERFLOW_ID not in profs_vistos else []
            if not otros_ids:
                return "Ya viste a todos los profesionales disponibles para esta especialidad 😊\n\nEscribe *otro día* para cambiar de día o elige un número del listado."

            # 1) Intentar con los slots que ya tenemos del mismo día (todos_slots)
            slots_otros_mismo_dia = [s for s in todos_slots if s.get("id_profesional") in otros_ids]
            if slots_otros_mismo_dia:
                data["slots"] = slots_otros_mismo_dia
                nuevo_sugerido_id = slots_otros_mismo_dia[0].get("id_profesional")
                data["prof_sugerido_id"] = nuevo_sugerido_id
                data["profs_vistos"] = list(profs_vistos)
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
                         "prof_sugerido_id": nuevo_sugerido_id,
                         "profs_vistos": list(profs_vistos)})
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
        # ── Salto directo a fecha específica ("para mayo", "el 15/05", "próxima semana") ──
        # Antes el paciente debía spamear "otro día" 6+ veces para llegar a mayo.
        _fecha_objetivo: str | None = None
        # 1) Mes mencionado: "para mayo", "en mayo", "mayo", "para junio"
        for _mes_nombre, _mes_num in _MESES_ES.items():
            if len(_mes_nombre) < 3:
                continue
            if (f" {_mes_nombre}" in f" {tl_norm_slot}"
                    or tl_norm_slot.startswith(_mes_nombre)
                    or tl_norm_slot.endswith(_mes_nombre)):
                _hoy_dt = datetime.now(_CHILE_TZ).date()
                _anio = _hoy_dt.year
                if _mes_num < _hoy_dt.month or (_mes_num == _hoy_dt.month and _hoy_dt.day > 25):
                    _anio += 1
                _fecha_objetivo = f"{_anio:04d}-{_mes_num:02d}-01"
                break
        # 2) Fecha DD/MM o DD-MM
        if not _fecha_objetivo:
            _m = re.search(r"\b(\d{1,2})[/\-\.](\d{1,2})(?:[/\-\.](\d{2,4}))?\b", tl_norm_slot)
            if _m:
                try:
                    _d, _mm = int(_m.group(1)), int(_m.group(2))
                    _yy = _m.group(3)
                    _hoy_dt = datetime.now(_CHILE_TZ).date()
                    if _yy:
                        _yy_int = int(_yy)
                        if _yy_int < 100:
                            _yy_int += 2000
                        _anio = _yy_int
                    else:
                        _anio = _hoy_dt.year
                        if (_mm, _d) < (_hoy_dt.month, _hoy_dt.day):
                            _anio += 1
                    if 1 <= _d <= 31 and 1 <= _mm <= 12:
                        _fecha_objetivo = f"{_anio:04d}-{_mm:02d}-{_d:02d}"
                except (ValueError, IndexError):
                    pass
        # 3) "próxima semana" / "la otra semana" / "en X semanas"
        if not _fecha_objetivo:
            if any(k in tl_norm_slot for k in ("proxima semana", "próxima semana",
                                               "la otra semana", "otra semana",
                                               "semana que viene", "semana entrante")):
                _hoy_dt = datetime.now(_CHILE_TZ).date()
                _dias_lunes = (7 - _hoy_dt.weekday()) % 7 or 7
                _fecha_objetivo = (_hoy_dt + timedelta(days=_dias_lunes)).strftime("%Y-%m-%d")
            else:
                _m_sem = re.search(r"\ben\s+(\d{1,2})\s+semanas?\b", tl_norm_slot)
                if _m_sem:
                    _hoy_dt = datetime.now(_CHILE_TZ).date()
                    _fecha_objetivo = (_hoy_dt + timedelta(days=int(_m_sem.group(1))*7)).strftime("%Y-%m-%d")
        if _fecha_objetivo:
            _maso_override = {59: data["maso_duracion"]} if especialidad == "masoterapia" and data.get("maso_duracion") else None
            try:
                smart_dia, todos_dia = await buscar_slots_dia(
                    especialidad, _fecha_objetivo, intervalo_override=_maso_override)
            except Exception as e:
                log.warning("buscar_slots_dia salto fecha falló: %s", e)
                smart_dia, todos_dia = [], []
            if todos_dia:
                fechas_vistas_nuevas = (data.get("fechas_vistas") or []) + [_fecha_objetivo]
                data.update({"slots": (smart_dia or todos_dia)[:5],
                             "todos_slots": todos_dia,
                             "fechas_vistas": fechas_vistas_nuevas,
                             "expansion_stage": 0})
                save_session(phone, "WAIT_SLOT", data)
                log_event(phone, "salto_fecha_directo", {"fecha": _fecha_objetivo})
                return _format_slots((smart_dia or todos_dia)[:5])
            # No hay slots ese día — buscar próximos 14 días desde la fecha pedida
            try:
                _start_dt = datetime.strptime(_fecha_objetivo, "%Y-%m-%d").date()
                for _delta in range(1, 15):
                    _fecha_try = (_start_dt + timedelta(days=_delta)).strftime("%Y-%m-%d")
                    smart_post, todos_post = await buscar_slots_dia(
                        especialidad, _fecha_try, intervalo_override=_maso_override)
                    if todos_post:
                        fechas_vistas_nuevas = (data.get("fechas_vistas") or []) + [_fecha_try]
                        data.update({"slots": (smart_post or todos_post)[:5],
                                     "todos_slots": todos_post,
                                     "fechas_vistas": fechas_vistas_nuevas,
                                     "expansion_stage": 0})
                        save_session(phone, "WAIT_SLOT", data)
                        return _format_slots((smart_post or todos_post)[:5])
            except (ValueError, Exception) as e:
                log.warning("salto fecha follow-up falló: %s", e)
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

        # ── Motivos del menú que cayeron en WAIT_SLOT (usuario volvió a menú) ──
        # Manejo directo — evita redispatch que puede fallar por preambles (crisis,
        # emergencias, consent, doctor_mode). Cada motivo_* dispara _iniciar_agendar
        # con la especialidad correspondiente.
        _MOTIVOS_ESP = {
            "motivo_resfrio":  ("medicina general", "🤒", "Medicina General"),
            "motivo_kine":     ("kinesiología",     "🦴", "Kinesiología"),
            "motivo_hta":      ("medicina general", "🫀", "Medicina General"),
            "motivo_dental":   ("odontología",      "🦷", "Odontología"),
            "motivo_mg_otra":  ("medicina general", "🩺", "Medicina General"),
        }
        if tl in _MOTIVOS_ESP:
            esp, emoji, label = _MOTIVOS_ESP[tl]
            prefix = f"{emoji} *Perfecto, te agendo con {label}*\n\n"
            log_event(phone, "motivo_seleccionado", {"motivo": tl, "especialidad": esp})
            reset_session(phone)
            return await _iniciar_agendar(phone, {}, esp, saludo_prefix=prefix)
        if tl == "motivo_otra_esp":
            log_event(phone, "motivo_seleccionado", {"motivo": "otra_esp"})
            reset_session(phone)
            return await _iniciar_agendar(phone, {}, None)
        if txt == "cambiar_datos":
            # Botón "✏️ Cambiar algo" viene con sesión stale en WAIT_SLOT.
            # Reprocesar `cambiar_datos` como texto en IDLE no matchea nada
            # y cae en intent detection (resultados erráticos: FAQ, estética).
            # Fix: arrancar flujo de agendar desde cero.
            reset_session(phone)
            return await _iniciar_agendar(phone, {}, None)
        if txt in (
            "accion_cambiar", "accion_mis_citas", "accion_otro",
            "menu_volver"
        ):
            reset_session(phone)
            return await handle_message(phone, txt, {"state": "IDLE", "data": {}})

        # ── "No" suelto en WAIT_SLOT → ofrecer alternativas (no confundir con negación real) ──
        _tl_slot = txt.strip().lower()
        if _tl_slot in ("no", "no gracias", "nel", "nop", "negativo", "no me sirve", "ninguna"):
            return (
                "Sin problema 😊 Puedo mostrarte:\n\n"
                "• *Otros horarios* del mismo día (escribe *ver todos*)\n"
                "• *Otro día* para cambiar de fecha\n"
                "• *Otro profesional* (si hay disponible)\n\n"
                "¿Qué prefieres?"
            )

        # ── Pregunta por contacto / teléfono / dirección / ubicación ──
        if any(k in _tl_slot for k in (
            "contacto telef", "contacto telefonico", "contacto telefónico",
            "numero de contacto", "número de contacto",
            "telefono de contacto", "teléfono de contacto",
            "numero para llamar", "número para llamar",
            "llamar por telefono", "llamar por teléfono",
            "telefono del centro", "teléfono del centro",
        )):
            save_session(phone, "WAIT_SLOT", data)
            return (
                f"📞 *{CMC_TELEFONO}* · ☎️ *{CMC_TELEFONO_FIJO}*\n"
                f"📍 Monsalve 102, Carampangue\n\n"
                "_Seguimos con tu reserva: elige un número del listado o escribe *otro día*._"
            )

        # ── Pregunta por teléfono/dirección en WAIT_SLOT ──
        _INFO_CONTACTO = ("numero de contacto", "número de contacto", "telefono de contacto",
                          "teléfono de contacto", "a que numero", "a qué número",
                          "direccion del centro", "dirección del centro",
                          "donde queda", "dónde queda", "como llego", "cómo llego")
        if any(p in tl_norm_slot for p in _INFO_CONTACTO):
            return (
                f"📞 *{CMC_TELEFONO}* o ☎️ *(41) 296 5226*\n"
                f"📍 Monsalve 102, Carampangue (frente a la antigua estación de trenes).\n\n"
                "_Elige un número del listado, *ver todos* para más horarios, u *otro día*._"
            )

        # ── Apellido específico mencionado ("con el dr marquez", "quiero con abarca") ──
        # PRIORIDAD MÁXIMA: si el paciente pide un doctor por nombre, filtramos
        # slots actuales a ese profesional o lanzamos búsqueda fresca con él.
        # Evita loop donde el paciente pedía Márquez y el bot ofrecía Olavarría.
        _apellido_slot = _detectar_apellido_profesional(txt) if tl != "otro_prof" else None
        if _apellido_slot:
            from medilink import _ids_para_especialidad
            ids_apellido = set(_ids_para_especialidad(_apellido_slot))
            if ids_apellido:
                slots_de_ese = [s for s in todos_slots if s.get("id_profesional") in ids_apellido]
                if slots_de_ese:
                    data["slots"] = slots_de_ese[:10]
                    data["prof_sugerido_id"] = slots_de_ese[0].get("id_profesional")
                    _pv = set(data.get("profs_vistos", []))
                    _pv.update(ids_apellido)
                    data["profs_vistos"] = list(_pv)
                    save_session(phone, "WAIT_SLOT", data)
                    return _format_slots(slots_de_ese[:10], mostrar_todos=True)
                # Sin slots de ese profesional en el día actual → búsqueda fresca
                reset_session(phone)
                return await _iniciar_agendar(phone, {}, _apellido_slot)

        # ── Intento de cambio de profesional por lenguaje natural ──
        # "no quiero ese profesional", "con otro doctor", "no me gusta", etc.
        _OTRO_PROF_PHRASES = (
            "no quiero ese", "no me gusta", "otro doctor", "otro profesional",
            "otra doctora", "otro médico", "otro medico", "con otro",
            "con otra", "cambiar doctor", "cambiar profesional",
            "no ese", "no ese doctor", "prefiero otro",
        )
        if any(p in tl_norm_slot for p in _OTRO_PROF_PHRASES):
            tl = "otro_prof"  # re-dispatch al handler ya existente

        # ── Día relativo ("mañana", "pasado mañana", "hoy") — PRIORITARIO ──
        # Va antes del filtro por período para que "Para mañana" = día siguiente,
        # no "en la mañana" (período horario).
        _DIA_RELATIVO = None
        _hoy = datetime.now(_CHILE_TZ).date()
        # Strip puntuación final del paciente ("O mañana ??", "mañana?", "hoy.")
        _tns_clean = tl_norm_slot.rstrip("!?.,;:¿¡ ").strip()
        # Eliminar prefijos triviales que rompían el match exacto:
        # "o mañana" → "mañana"; "y mañana" → "mañana"; "para hoy" → "hoy"
        _tns_short = _tns_clean
        for _pref in ("o ", "y ", "para ", "el "):
            if _tns_short.startswith(_pref):
                _tns_short = _tns_short[len(_pref):]
                break
        if "pasado mañana" in tl_norm_slot or "pasado manana" in tl_norm_slot:
            _DIA_RELATIVO = (_hoy + timedelta(days=2)).strftime("%Y-%m-%d")
        elif ("para mañana" in tl_norm_slot or "para manana" in tl_norm_slot
              or _tns_clean in ("mañana", "manana", "o mañana", "o manana", "y mañana", "y manana")
              or _tns_short in ("mañana", "manana")):
            # Confirmar que NO es "en la mañana" / "por la mañana" (ahí es franja horaria)
            if not any(p in tl_norm_slot for p in ("en la mañana", "en la manana",
                                                    "por la mañana", "por la manana")):
                _DIA_RELATIVO = (_hoy + timedelta(days=1)).strftime("%Y-%m-%d")
        elif _tns_clean in ("hoy", "hoy mismo", "hoy dia", "hoy día") or _tns_short == "hoy":
            _DIA_RELATIVO = _hoy.strftime("%Y-%m-%d")
        if _DIA_RELATIVO:
            _maso_override = {59: data["maso_duracion"]} if especialidad == "masoterapia" and data.get("maso_duracion") else None
            smart_dia, todos_dia = await buscar_slots_dia(
                especialidad, _DIA_RELATIVO, intervalo_override=_maso_override)
            # Filtro estricto: Medilink a veces devuelve slots del día siguiente
            # cuando no hay disponibilidad en el día pedido. Aseguramos que solo
            # mostramos slots con fecha == _DIA_RELATIVO.
            todos_dia = [s for s in (todos_dia or []) if s.get("fecha") == _DIA_RELATIVO]
            smart_dia = [s for s in (smart_dia or []) if s.get("fecha") == _DIA_RELATIVO]
            if todos_dia:
                if _DIA_RELATIVO not in fechas_vistas:
                    fechas_vistas = fechas_vistas + [_DIA_RELATIVO]
                data.update({"slots": smart_dia or todos_dia[:5],
                             "todos_slots": todos_dia,
                             "fechas_vistas": fechas_vistas, "expansion_stage": 1})
                save_session(phone, "WAIT_SLOT", data)
                return _format_slots(smart_dia or todos_dia[:5])
            # Convertir fecha a label legible para el mensaje
            from datetime import datetime as _dtx
            try:
                _d = _dtx.strptime(_DIA_RELATIVO, "%Y-%m-%d")
                _DIAS_LBL = ["lunes","martes","miércoles","jueves","viernes","sábado","domingo"]
                _MESES_LBL = ["enero","febrero","marzo","abril","mayo","junio","julio","agosto","septiembre","octubre","noviembre","diciembre"]
                _lbl = f"{_DIAS_LBL[_d.weekday()]} {_d.day} de {_MESES_LBL[_d.month - 1]}"
            except Exception:
                _lbl = _DIA_RELATIVO
            return (
                f"No tengo horarios disponibles para *{_lbl}* 😕\n\n"
                f"Escribe *otro día* para buscar el siguiente disponible, o llama a recepción."
            )

        # ── Filtro por período horario (mañana/tarde/noche) ──
        # NOTA: "mañana" suelto ya se manejó arriba como día relativo.
        _PERIODOS = {
            # Más específico primero (orden importa para el primer match)
            "tarde noche":    (17, 24), "tarde-noche": (17, 24),
            "tardecita":      (17, 22),
            "mas tarde":      (17, 24), "más tarde": (17, 24),
            "mas tardecito":  (17, 22), "más tardecito": (17, 22),
            "mas temprano":   (0, 11),  "más temprano": (0, 11),
            "mas tempranito": (0, 10),  "más tempranito": (0, 10),
            "en la mañana":   (0, 12),  "en la manana": (0, 12),
            "por la mañana":  (0, 12),  "por la manana": (0, 12),
            "temprano":       (0, 12),
            "mediodía":       (12, 14), "mediodia": (12, 14), "al mediodia": (12, 14),
            "en la tarde":    (14, 19), "por la tarde": (14, 19),
            "tarde":          (14, 19),
            "en la noche":    (19, 24), "por la noche": (19, 24),
            "noche":          (19, 24),
            # 'mañana' solo NO se incluye aquí — el día relativo (línea 3079+)
            # ya lo interpreta como "tomorrow". Solo "en la mañana" / "por la
            # mañana" caen como franja horaria.
        }
        periodo = None
        for kw, rango in _PERIODOS.items():
            if kw in tl_norm_slot and tl != "otro_prof":
                periodo = (kw, rango)
                break
        if periodo:
            kw, (h_min, h_max) = periodo
            slots_filtrados = [
                s for s in todos_slots
                if h_min <= int(s.get("hora_inicio", "99:00")[:2]) < h_max
            ]
            if slots_filtrados:
                data["slots"] = slots_filtrados[:10]
                save_session(phone, "WAIT_SLOT", data)
                return _format_slots(slots_filtrados[:10], mostrar_todos=True)
            # No hay slots en ese período → responder con los disponibles
            horas_disp = sorted({s.get("hora_inicio", "")[:5] for s in todos_slots if s.get("hora_inicio")})
            # Mapa kw → label gramaticalmente correcto (evita "en la en la mañana",
            # "en la mediodía", "en la mas tarde", etc.)
            _PERIODO_LABEL = {
                "tarde noche": "la tarde-noche", "tarde-noche": "la tarde-noche",
                "tardecita": "la tardecita",
                "mas tarde": "el horario más tarde", "más tarde": "el horario más tarde",
                "mas tardecito": "el horario más tardecito", "más tardecito": "el horario más tardecito",
                "mas temprano": "la mañana temprano", "más temprano": "la mañana temprano",
                "mas tempranito": "la mañana tempranito", "más tempranito": "la mañana tempranito",
                "en la mañana": "la mañana", "en la manana": "la mañana",
                "por la mañana": "la mañana", "por la manana": "la mañana",
                "temprano": "la mañana temprano",
                "mediodía": "el mediodía", "mediodia": "el mediodía", "al mediodia": "el mediodía",
                "en la tarde": "la tarde", "por la tarde": "la tarde",
                "tarde": "la tarde",
                "en la noche": "la noche", "por la noche": "la noche",
                "noche": "la noche",
            }
            _label = _PERIODO_LABEL.get(kw, kw)
            return (
                f"No tengo horas en {_label} para este profesional 😕\n\n"
                f"Horarios disponibles:\n{', '.join(horas_disp[:12])}"
                f"\n\nElige uno, escribe *otro día* o *otro profesional*."
            )

        # ── Hora exacta mencionada ("10:00", "diez y media", "a las 5") ──
        # Delegamos el parseo a time_parser.parse_hora (cubre ~100 formatos:
        # numérico, AM/PM, palabras, prefijos, sufijos, expresiones de resta).
        from time_parser import parse_hora as _parse_hora
        _hora_tuple = _parse_hora(tl_norm_slot)
        def _slot_hora_close(slots, h_target, m_target):
            def _mins(hm):
                try:
                    hh, mm = hm.split(":")
                    return int(hh) * 60 + int(mm)
                except Exception:
                    return 9999
            target = h_target * 60 + m_target
            best = None
            best_d = 999
            for s in slots:
                hi = s.get("hora_inicio", "")[:5]
                if not hi:
                    continue
                d = abs(_mins(hi) - target)
                if d < best_d:
                    best_d = d
                    best = s
            return best, best_d
        _hora_match_valida = False
        _h_pedida = _m_pedida = 0
        if _hora_tuple is not None:
            _h_pedida, _m_pedida = _hora_tuple
            # "10" solo → selección por número, no hora (lo maneja _parse_slot_selection)
            _es_numero_puro = tl_norm_slot.strip().isdigit() and len(tl_norm_slot.strip()) <= 2
            _hora_match_valida = (
                not _es_numero_puro
                and bool(todos_slots)
                and tl != "otro_prof"
            )
        if _hora_match_valida:
            best_slot, delta = _slot_hora_close(todos_slots, _h_pedida, _m_pedida)
            if best_slot and delta <= 30:
                data["slots"] = [best_slot]
                save_session(phone, "WAIT_SLOT", data)
                return _format_slots([best_slot])
            cercanos = []
            for s in todos_slots:
                hi = s.get("hora_inicio", "")[:5]
                try:
                    hh = int(hi.split(":")[0])
                    if abs(hh - _h_pedida) <= 2:
                        cercanos.append(s)
                except Exception:
                    pass
            _slot_resp_c = None
            if cercanos:
                data["slots"] = cercanos[:10]
                save_session(phone, "WAIT_SLOT", data)
                _slot_resp_c = _format_slots(cercanos[:10], mostrar_todos=True)
            _hdr = f"No tengo exactamente a las {_h_pedida:02d}:{_m_pedida:02d} 😕\n"
            _hdr += "Te muestro los más cercanos:"
            if _slot_resp_c is None:
                return _hdr + "\n\n_No hay otros horarios disponibles ese día._"
            if isinstance(_slot_resp_c, dict):
                await send_whatsapp(phone, _hdr)
                return _slot_resp_c
            return _hdr + "\n\n" + _slot_resp_c

        # ── Ventana horaria "desde las N" / "después de las N" / "antes de las N" ──
        # Usuario escribe "desde las 15", "después de las 5", "antes de las 12"
        import re as _re_vh
        _m_desde = _re_vh.search(
            r'(?:desde|despues de|después de|a partir de|despues d las|después d las)\s+(?:las\s+)?(\d{1,2})',
            tl_norm_slot,
        )
        _m_antes = _re_vh.search(
            r'(?:antes de|hasta|máximo|maximo)\s+(?:las\s+)?(\d{1,2})',
            tl_norm_slot,
        )
        if (_m_desde or _m_antes) and todos_slots:
            def _h_int(s):
                try:
                    return int(s.get("hora_inicio", "00:00")[:2])
                except Exception:
                    return 0
            if _m_desde:
                h_min = int(_m_desde.group(1))
                # Asumir PM si <8 (pedir "después de las 5" = 17:00)
                if h_min < 8:
                    h_min += 12
                slots_vh = [s for s in todos_slots if _h_int(s) >= h_min]
                etiqueta = f"desde las {h_min:02d}:00"
            else:
                h_max = int(_m_antes.group(1))
                if h_max < 8:
                    h_max += 12
                slots_vh = [s for s in todos_slots if _h_int(s) < h_max]
                etiqueta = f"antes de las {h_max:02d}:00"
            if slots_vh:
                data["slots"] = slots_vh[:10]
                save_session(phone, "WAIT_SLOT", data)
                return _format_slots(slots_vh[:10], mostrar_todos=True)
            horas_disp_vh = sorted({s.get("hora_inicio", "")[:5] for s in todos_slots if s.get("hora_inicio")})
            return (
                f"No tengo horas {etiqueta} para este profesional 😕\n\n"
                f"Horarios disponibles:\n{', '.join(horas_disp_vh[:12])}"
                f"\n\nElige uno, escribe *otro día* o *otro profesional*."
            )

        idx = _parse_slot_selection(txt, slots_mostrados)

        # ── Fallback 1: HH:MM contra TODOS los slots del día, no solo los 5 mostrados ──
        # Usuario escribe "10:00", "las 16:45", "1030" y ese horario está en todos_slots
        # aunque no esté entre los 5 sugeridos → promocionar al primer puesto y re-mostrar.
        if idx is None and todos_slots and len(todos_slots) > len(slots_mostrados):
            idx_all = _parse_slot_selection(txt, todos_slots)
            if idx_all is not None:
                slot_elegido = todos_slots[idx_all]
                hora_eleg = slot_elegido.get("hora_inicio", "")[:5]
                # Poner el slot elegido primero, llenar resto con los ya mostrados
                otros = [s for s in slots_mostrados if s.get("hora_inicio", "")[:5] != hora_eleg]
                data["slots"] = [slot_elegido] + otros[:4]
                save_session(phone, "WAIT_SLOT", data)
                return _format_slots(data["slots"])

        # ── Fallback 2: apellido de profesional en texto libre (sin llamar a Claude) ──
        # Usuario escribe "Con Olavarria", "el dr marquez", "necesito con Abarca".
        # Shortcut sin Claude para ahorrar tokens y latencia.
        if idx is None:
            apellido_key = _detectar_apellido_profesional(txt)
            if apellido_key:
                from medilink import _ids_para_especialidad
                ids_nuevos = set(_ids_para_especialidad(apellido_key))
                slots_prof = [s for s in todos_slots if s.get("id_profesional") in ids_nuevos]
                if slots_prof:
                    data["slots"] = slots_prof[:5]
                    data["prof_sugerido_id"] = slots_prof[0].get("id_profesional")
                    save_session(phone, "WAIT_SLOT", data)
                    return _format_slots(slots_prof[:5], mostrar_todos=True)
                # No hay slots de ese profesional en el pool actual — re-buscar fresh
                reset_session(phone)
                return await _iniciar_agendar(phone, {}, apellido_key)

        if idx is None:
            # Si el texto parece una hora pero no coincide con slots, mostrar opciones
            import re as _re
            _hora_match = _re.search(r"\b(\d{1,2})[:.]?(\d{2})?\b", tl_norm_slot)
            if _hora_match and len(tl_norm_slot) <= 10:
                h_pedida = _hora_match.group(1).zfill(2)
                m_pedida = _hora_match.group(2) or ""
                hora_str = f"{h_pedida}:{m_pedida}" if m_pedida else f"{h_pedida}:00"
                horas_disp = sorted({s.get("hora_inicio", "")[:5] for s in todos_slots if s.get("hora_inicio")})
                if horas_disp and hora_str not in horas_disp:
                    return (
                        f"La hora *{hora_str}* no está disponible para este profesional 😕\n\n"
                        f"Horarios disponibles:\n{', '.join(horas_disp[:12])}"
                        f"\n\nElige una o escribe *otro día*."
                    )
            if len(txt) > 2:
                result = await detect_intent(txt)
                intent = result.get("intent", "otro")
                esp_override = _detectar_apellido_profesional(txt)
                # Si detectamos apellido de profesional, tratarlo como intent agendar
                # aunque Claude haya devuelto otro (info/precio/otro). El paciente
                # claramente está pidiendo al doctor por nombre.
                if esp_override and intent not in ("cancelar", "reagendar", "ver_reservas"):
                    intent = "agendar"
                if intent == "agendar" and (result.get("especialidad") or esp_override):
                    from medilink import _ids_para_especialidad
                    # Override: si el texto crudo menciona un apellido de profesional,
                    # priorizar ese match sobre la clasificación genérica de Claude.
                    esp_pedida = esp_override or result.get("especialidad", "")
                    ids_nuevos = set(_ids_para_especialidad(esp_pedida))
                    ids_actuales = {s.get("id_profesional") for s in todos_slots}
                    # Si el paciente pide un doctor/especialidad que ya está en el pool
                    # actual, filtrar a ese profesional. Si no hay en pool o filtro
                    # sale vacío, resetear y buscar fresh — el paciente nombró a un
                    # profesional específico y merece ver SUS horarios, no un menú genérico.
                    if ids_nuevos and ids_nuevos & ids_actuales:
                        slots_filtrados = [s for s in todos_slots if s.get("id_profesional") in ids_nuevos]
                        if slots_filtrados:
                            data["slots"] = slots_filtrados
                            data["prof_sugerido_id"] = slots_filtrados[0].get("id_profesional")
                            save_session(phone, "WAIT_SLOT", data)
                            return _format_slots(slots_filtrados, mostrar_todos=True)
                    # Fallback robusto: cualquier mención de profesional específico →
                    # buscar slots frescos de ese profesional (incluye caso sin pool match).
                    reset_session(phone)
                    return await _iniciar_agendar(phone, {}, esp_pedida)
                if intent == "cancelar":
                    reset_session(phone)
                    return await _iniciar_cancelar(phone, {})
                if intent == "ver_reservas":
                    reset_session(phone)
                    return await _iniciar_ver(phone, {})
                if intent in ("precio", "info"):
                    esp_display = todos_slots[0]["especialidad"] if todos_slots else especialidad
                    # Heredar contexto SOLO si la pregunta es corta y no menciona otra
                    # especialidad. Si el texto menciona una especialidad/tratamiento
                    # distinto ("procedimientos estéticos", "endodoncia", "botox"),
                    # respetar el texto original y no contaminar con la especialidad
                    # del WAIT_SLOT actual.
                    tl = txt.lower().strip()
                    OTRAS_ESPS_KW = (
                        "odontolog", "dental", "diente", "muela", "tapadura",
                        "endodoncia", "conducto", "ortodoncia", "brackets",
                        "implante", "implantolog", "estét", "estetica",
                        "botox", "peeling", "hilos", "bioestim", "lipopapada",
                        "kinesio", "kine", "lumbago", "espalda",
                        "cardio", "corazon", "corazón", "gastro",
                        "gine", "matrona", "embarazo", "otorrino", "garganta", "oido", "oído",
                        "fono", "psico", "ansiedad", "nutri", "dieta",
                        "podo", "uña", "ecograf", "maso", "masaje",
                    )
                    menciona_otra = any(k in tl for k in OTRAS_ESPS_KW)
                    ambiguas = {"precio", "precios", "cuanto", "cuánto",
                                "cuanto cuesta", "cuánto cuesta", "cuanto sale",
                                "cuánto sale", "cuanto vale", "cuánto vale",
                                "valor", "vale"}
                    es_ambigua_corta = (
                        not menciona_otra
                        and len(tl) <= 20
                        and any(p in tl for p in ambiguas)
                    )
                    if es_ambigua_corta and esp_display:
                        consulta = f"¿Cuánto cuesta una consulta de {esp_display}?"
                    else:
                        consulta = txt
                    resp = await respuesta_faq(consulta)
                    # Refrescar sesión para mantener el flujo vivo y que el panel
                    # muestre esta conversación como "activa"
                    save_session(phone, "WAIT_SLOT", data)
                    return (
                        f"{resp}\n\n"
                        "_Elige un número para continuar con tu reserva o escribe *menu* para volver._"
                    )
            # Fallback sistémico: antes de dar el mensaje genérico, re-correr
            # detect_intent. Si el paciente pivotó a otra acción clara (cancelar,
            # reagendar, cambiar de especialidad, ver reservas), procesamos ese
            # intent nuevo en vez de insistir con el "no te entendí".
            if len(txt) >= 3 and not txt.isdigit():
                try:
                    _pivot = await detect_intent(txt)
                    _pintent = _pivot.get("intent", "otro")
                except Exception:
                    _pintent = "otro"
                if _pintent in ("cancelar", "reagendar", "ver_reservas"):
                    log_event(phone, "wait_slot_pivot", {"intent": _pintent, "texto": txt[:120]})
                    reset_session(phone)
                    return await handle_message(phone, texto, {"state": "IDLE", "data": {}})
                if _pintent == "agendar" and _pivot.get("especialidad"):
                    nueva_esp = (_pivot.get("especialidad") or "").lower()
                    if nueva_esp and nueva_esp != (data.get("especialidad") or "").lower():
                        log_event(phone, "wait_slot_cambio_esp",
                                  {"de": data.get("especialidad"), "a": nueva_esp})
                        reset_session(phone)
                        return await _iniciar_agendar(phone, {}, nueva_esp)
            # Mensajes con clara intención de hablar con el doctor o dejar
            # consulta libre → escalar directo (no insistir con número).
            # Bug 2026-04-25 (56923649471, 14:05): "Fui en la semana y necesito
            # hacerle una consulta" cayó en "No te entendí bien" tres veces.
            _DERIVAR_FRASES = (
                "necesito hacerle", "necesito hablar",
                "necesito consultar", "necesito preguntar",
                "hacerle una consulta", "consultarle",
                "dejarle un mensaje", "decirle al doctor",
                "decirle al dr", "decirle a la dra",
                "le pregunto al doctor", "modificar la receta",
                "modificarla", "me dio una receta",
            )
            if any(f in tl for f in _DERIVAR_FRASES):
                log_event(phone, "wait_slot_consulta_libre", {"texto": txt[:120]})
                return _derivar_humano(phone=phone, contexto="consulta libre WAIT_SLOT")
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
        return await _slot_confirmed(phone, data, slot)

    # ── WAIT_MODALIDAD ────────────────────────────────────────────────────────
    if state == "WAIT_MODALIDAD":
        FONASA     = {"1", "fonasa", "fona", "con fonasa", "por fonasa"}
        PARTICULAR = {"2", "particular", "privado", "privada", "particulares", "con particular"}
        ISAPRE     = {"isapre", "consalud", "colmena", "banmedica", "cruz blanca", "vida tres"}
        if tl in FONASA or tl_norm in FONASA:
            data["modalidad"] = "fonasa"
        elif tl in PARTICULAR or tl_norm in PARTICULAR:
            data["modalidad"] = "particular"
        elif tl in ISAPRE or any(k in tl for k in ISAPRE):
            # Isapre no está integrado → atender como particular con nota
            data["modalidad"] = "particular"
        else:
            # Escape: usuario se equivocó / quiere reiniciar
            if txt.startswith("motivo_") or tl in ("menu", "menú", "inicio", "hola", "volver"):
                reset_session(phone)
                return await handle_message(phone, txt, {"state": "IDLE", "data": {}})
            # Escape: menciona "otra persona" → saltar a flujo de terceros
            # Regex con word-boundary evita matchear "para otro DÍA" o
            # "para otra CITA". Caso real 2026-04-21 (56982709417): "necesito
            # una hora para otro día" → bot decía "Entendido, es para otra persona".
            _OTRA_PERSONA_RE = re.compile(
                r"\b(otra persona|otr[oa] familiar|mi esposo|mi esposa|"
                r"mi hijo|mi hija|mi mam[aá]|mi pap[aá]|mi hermano|mi hermana|"
                r"mi abuelo|mi abuela|mi pololo|mi polola|mi pareja|mi nieto|"
                r"mi nieta|un familiar|para un amigo|para una amiga|"
                r"para mi (?:hijo|hija|mam[aá]|pap[aá]|hermano|hermana|"
                r"abuelo|abuela|esposo|esposa|pareja|nieto|nieta))\b"
            )
            if _OTRA_PERSONA_RE.search(tl):
                data["booking_for_other"] = True
                save_session(phone, "WAIT_MODALIDAD", data)
                return _btn_msg(
                    "Entendido, es para otra persona 😊\n\n¿Atención *Fonasa* o *Particular*?",
                    [{"id": "1", "title": "Fonasa"},
                     {"id": "2", "title": "Particular"}]
                )
            # Escape: apellido profesional → reiniciar agendar con ese doctor
            apellido_esc = _detectar_apellido_profesional(txt)
            if apellido_esc:
                reset_session(phone)
                return await _iniciar_agendar(phone, {}, apellido_esc)
            # Escape: payload de otro día / ver otros (quedaron en buffer)
            if tl in ("otro_dia", "otro_día", "ver_otros", "ver_todos"):
                save_session(phone, "WAIT_MODALIDAD", data)
                return "Primero dime si la atención es *Fonasa* o *Particular* 😊\n\nDespués elegimos otro horario."
            data["intentos_fallidos"] = data.get("intentos_fallidos", 0) + 1
            if data["intentos_fallidos"] >= 3:
                return _derivar_humano(phone=phone, contexto="frustración WAIT_MODALIDAD")
            save_session(phone, "WAIT_MODALIDAD", data)
            return _btn_msg(
                "¿La atención será *Fonasa* o *Particular*?",
                [{"id": "1", "title": "Fonasa"},
                 {"id": "2", "title": "Particular"}]
            )

        modalidad_str = data["modalidad"].capitalize()
        # Saltar WAIT_BOOKING_FOR → ir directo al RUT (si quiere para otro, escribe "otra persona")
        data["booking_for_other"] = False

        # Atajo para pacientes conocidos
        rut_c = data.get("rut_conocido")
        nombre_c = data.get("nombre_conocido")
        if rut_c and nombre_c:
            save_session(phone, "WAIT_RUT_AGENDAR", data)
            return _btn_msg(
                f"Perfecto, atención *{modalidad_str}* 😊\n\n"
                f"¿Agendo con tus datos, *{_first_name(nombre_c)}*?",
                [{"id": "si", "title": "✅ Sí, continuar"},
                 {"id": "rut_nuevo", "title": "Ingresar otro RUT"}]
            )

        save_session(phone, "WAIT_RUT_AGENDAR", data)
        return (
            f"Perfecto, atención *{modalidad_str}* 😊\n\n"
            "Para reservar necesito tu *RUT*:\n"
            "(ej: *12.345.678-9*)\n\n"
            "_Si es para otra persona, escribe *otra persona*._"
            + _PRIVACY_NOTE
        )

    # ── WAIT_BOOKING_FOR ───────────────────────────────────────────────────────
    if state == "WAIT_BOOKING_FOR":
        _SELF = {"booking_self", "para mi", "para mí", "yo", "mio", "mía", "mia"}
        _OTHER = {"booking_other", "otra persona", "otro", "otra", "familiar",
                  "hijo", "hija", "papa", "papá", "mama", "mamá", "hermano", "hermana",
                  "esposo", "esposa", "abuelo", "abuela"}
        if tl in _SELF or tl_norm in _SELF:
            data["booking_for_other"] = False
            save_session(phone, "WAIT_RUT_AGENDAR", data)
            rut_conocido = data.get("rut_conocido")
            nombre_conocido = data.get("nombre_conocido")
            if rut_conocido and nombre_conocido:
                nombre_corto = _first_name(nombre_conocido)
                return _btn_msg(
                    f"¿Agendo con tus datos anteriores, *{nombre_corto}*?",
                    [
                        {"id": "si", "title": "Sí, continuar"},
                        {"id": "rut_nuevo", "title": "Ingresar otro RUT"},
                    ]
                )
            return (
                "Para confirmar necesito tu RUT:\n"
                "(ej: *12.345.678-9*)"
                + _PRIVACY_NOTE
            )
        if tl in _OTHER or tl_norm in _OTHER:
            data["booking_for_other"] = True
            # Limpiar RUT/nombre conocido para pedir datos del paciente real
            data.pop("rut_conocido", None)
            data.pop("nombre_conocido", None)
            # Verificar si ya conocemos el nombre del dueño del celular
            perfil_owner = get_profile(phone)
            if perfil_owner and perfil_owner.get("nombre"):
                save_session(phone, "WAIT_RUT_AGENDAR", data)
                return (
                    "Sin problema 😊 Necesito el RUT de la persona que se va a atender:\n"
                    "(ej: *12.345.678-9*)"
                )
            # No conocemos al dueño del celular — pero no preguntemos su nombre
            # ahora (genera fricción). Saltamos directo al RUT del paciente a
            # atender. Al final preguntamos si el RUT es suyo o es para tercero.
            save_session(phone, "WAIT_RUT_AGENDAR", data)
            return (
                "Sin problema 😊 Necesito el *RUT* de la persona que se va a atender:\n"
                "(ej: *12.345.678-9*)"
            )
        save_session(phone, "WAIT_BOOKING_FOR", data)
        return _btn_msg(
            "Responde *Para mí* o *Para otra persona* 😊",
            [
                {"id": "booking_self", "title": "Para mí"},
                {"id": "booking_other", "title": "Para otra persona"},
            ]
        )

    # ── WAIT_PHONE_OWNER_NAME ────────────────────────────────────────────────
    if state == "WAIT_PHONE_OWNER_NAME":
        nombre_owner = txt.strip()
        if len(nombre_owner) < 2 or nombre_owner.isdigit():
            save_session(phone, "WAIT_PHONE_OWNER_NAME", data)
            return "¿Cuál es tu nombre? (el de quien nos escribe, para enviarte los recordatorios)"
        # Guardar el nombre del dueño del celular (sin RUT, no es el paciente)
        save_profile(phone, "", nombre_owner)
        save_session(phone, "WAIT_RUT_AGENDAR", data)
        nombre_corto = _first_name(nombre_owner).capitalize()
        return (
            f"Gracias {nombre_corto} 😊 Ahora necesito el RUT de la persona que se va a atender:\n"
            "(ej: *12.345.678-9*)"
            + _PRIVACY_NOTE
        )

    # ── WAIT_RUT_AGENDAR ──────────────────────────────────────────────────────
    # Helper: detectar intent humano/escape antes de validar RUT. Si el paciente
    # pide hablar con alguien, no le insistamos con "RUT inválido".
    _HUMAN_PHRASES_RUT = (
        "hablar con", "hablar persona", "hablar secretaria",
        "con la secretaria", "con una persona", "con alguien",
        "recepcionista", "recepción", "recepcion",
        "no puedo ahora", "no tengo mi rut", "no recuerdo mi rut",
        "luego vuelvo", "llámame", "llamame", "llamen",
        "directo", "necesito ayuda", "ayudame", "ayúdame",
        "humano", "persona real",
    )
    if state in ("WAIT_RUT_AGENDAR", "WAIT_RUT_CANCELAR", "WAIT_RUT_REAGENDAR", "WAIT_RUT_VER"):
        _tl_rut = txt.lower().strip()
        if any(p in _tl_rut for p in _HUMAN_PHRASES_RUT) and len(_tl_rut) > 5:
            return _derivar_humano(phone=phone, contexto=f"paciente pidió humano en {state}")
        # Audios largos en WAIT_RUT_* = paciente está contando historia compleja,
        # no dándonos RUT. Derivar a humano con el texto transcrito como contexto.
        # Mismo para mensajes de texto MUY largos (>80 chars) sin formato de RUT.
        if (txt.startswith("🎤") and len(txt) > 30) or \
           (len(txt) > 80 and not any(ch.isdigit() for ch in txt[:15])):
            return _derivar_humano(
                phone=phone,
                contexto=f"audio/texto largo en {state}: {txt[:240]}",
            )

    if state == "WAIT_RUT_AGENDAR":
        # Botón "Ingresar otro RUT" (rut_nuevo) — paciente rechazó el RUT conocido
        if tl == "rut_nuevo":
            data.pop("rut_conocido", None)
            data.pop("nombre_conocido", None)
            save_session(phone, "WAIT_RUT_AGENDAR", data)
            return (
                "Perfecto 😊 Ingresa el *RUT* con el que se va a atender:\n"
                "(ej: *12.345.678-9*)"
            )
        # Si menciona otro profesional/especialidad (paciente se arrepintió del slot)
        # → reset + reiniciar agendar con esa especialidad
        _esp_override_rut = _detectar_apellido_profesional(txt) or _detectar_especialidad_en_texto(txt)
        _tl_rut_check = txt.lower().strip()
        _frases_cambio = ("me equivoque", "me equivoqué", "mejor con", "mejor el",
                          "cambiar a", "en realidad", "quise decir", "no quiero este")
        if _esp_override_rut and (any(p in _tl_rut_check for p in _frases_cambio) or len(txt) > 25):
            log_event(phone, "rut_to_agendar_redirect", {
                "texto": txt[:120], "esp": _esp_override_rut
            })
            reset_session(phone)
            return await _iniciar_agendar(phone, {}, _esp_override_rut)
        # Escape: "otra persona" → flujo de terceros
        _OTHER_PHRASES = {"otra persona", "otro", "otra", "para otra persona",
                          "para otro", "booking_other", "familiar", "hijo", "hija",
                          "papa", "papá", "mama", "mamá", "esposo", "esposa",
                          "hermano", "hermana", "abuelo", "abuela"}
        if tl in _OTHER_PHRASES or tl_norm in _OTHER_PHRASES:
            data["booking_for_other"] = True
            data.pop("rut_conocido", None)
            data.pop("nombre_conocido", None)
            perfil_owner = get_profile(phone)
            if perfil_owner and perfil_owner.get("nombre"):
                save_session(phone, "WAIT_RUT_AGENDAR", data)
                return (
                    "Sin problema 😊 Necesito el RUT de la persona que se va a atender:\n"
                    "(ej: *12.345.678-9*)"
                    + _PRIVACY_NOTE
                )
            # Ir directo al RUT del paciente. El nombre del dueño del cel lo
            # preguntamos al final (si la cita es para tercero).
            save_session(phone, "WAIT_RUT_AGENDAR", data)
            return (
                "Sin problema 😊 Necesito el *RUT* de la persona que se va a atender:\n"
                "(ej: *12.345.678-9*)"
                + _PRIVACY_NOTE
            )

        # Si el paciente ya agendó antes y confirma con sí/ok, usar su RUT guardado
        rut_conocido = data.get("rut_conocido")
        _SET_CONTINUAR = AFIRMACIONES | {"si", "sí", "ok", "mismo", "el mismo"}
        if rut_conocido and (tl in _SET_CONTINUAR or tl_norm in _SET_CONTINUAR) and tl != "rut_nuevo":
            rut = rut_conocido
        else:
            rut = clean_rut(txt)
        if not valid_rut(rut):
            # Escape: el usuario pide cambiar de profesional ("me equivoqué necesito con abarca")
            apellido_esc = _detectar_apellido_profesional(txt)
            if apellido_esc and any(
                k in tl for k in ("necesito", "quiero", "equivoque", "equivoqué",
                                  "con el", "con la", "mejor con")
            ):
                reset_session(phone)
                return await _iniciar_agendar(phone, {}, apellido_esc)
            # Escape: pregunta de precio/info en medio del flujo — responder sin romper
            if any(k in tl for k in ("cuanto", "cuánto", "precio", "valor", "vale", "sale", "bono")):
                try:
                    resp_faq = await respuesta_faq(txt)
                except Exception:
                    resp_faq = "Para más información, comunícate con recepción 😊"
                save_session(phone, "WAIT_RUT_AGENDAR", data)
                return (
                    f"{resp_faq}\n\n"
                    "_Cuando quieras continuar con tu reserva, envíame tu RUT 😊_"
                )
            data["intentos_fallidos"] = data.get("intentos_fallidos", 0) + 1
            if data["intentos_fallidos"] >= 3:
                return _derivar_humano(phone=phone, contexto="frustración WAIT_RUT_AGENDAR")
            save_session(phone, "WAIT_RUT_AGENDAR", data)
            return (
                "Hmm, no reconozco ese RUT 🤔\n"
                "Escríbelo con dígito verificador, por ejemplo: *12.345.678-9*"
            )

        _ensure_consent(phone)
        paciente, transient = await _buscar_paciente_safe(rut)
        if transient:
            data["rut"] = rut
            save_session(phone, "HUMAN_TAKEOVER", data)
            return _msg_medilink_transient()
        if not paciente:
            data["rut"] = rut
            is_social = phone.startswith("ig_") or phone.startswith("fb_")
            save_session(phone, "WAIT_DATOS_NUEVO", data)
            if is_social:
                return (
                    "¡Bienvenido/a! Es tu primera vez con nosotros 🙌\n\n"
                    "Escríbeme en *un solo mensaje*:\n\n"
                    "👤 Nombre completo\n"
                    "⚤ Sexo (M o F)\n"
                    "📅 Fecha de nacimiento\n"
                    "📱 Celular _(opcional, para recordarte la cita)_\n\n"
                    "_Ejemplo: María González López, F, 15/03/1990_\n"
                    "_Si quieres agregar celular al final: …, 912345678_"
                )
            return (
                "¡Bienvenido/a! Es tu primera vez con nosotros 🙌\n\n"
                "Escríbeme en *un solo mensaje*:\n\n"
                "👤 Nombre completo\n"
                "⚤ Sexo (M o F)\n"
                "📅 Fecha de nacimiento\n\n"
                "_Ejemplo: *María González López, F, 15/03/1990*_"
            )

        data.update({"paciente": paciente, "rut": rut})
        save_session(phone, "CONFIRMING_CITA", data)

        slot = data["slot_elegido"]
        modalidad = data.get("modalidad", "particular").capitalize()
        nombre_corto_conf = _first_name(paciente.get('nombre'))
        return _btn_msg(
            f"*{nombre_corto_conf}*, te reservo esta hora 👇\n\n"
            f"👤 {paciente['nombre']}\n"
            f"🏥 {slot['especialidad']} — {slot['profesional']}\n"
            f"📅 {slot['fecha_display']}\n"
            f"🕐 {slot['hora_inicio'][:5]}\n"
            f"💳 {modalidad}\n\n"
            "¿La confirmo?",
            [
                {"id": "si", "title": "✅ Sí, reservar"},
                {"id": "no", "title": "❌ Cambiar"},
            ]
        )

    # ── CONFIRMING_CITA ───────────────────────────────────────────────────────
    if state == "CONFIRMING_CITA":
        # Detección sistémica de re-envío de datos del paciente.
        # Caso real (Paula Alejandra, 28-abr): el paciente reenvió "Nombre, F, fecha"
        # 3 veces para corregir el año, pero el bot insistía en pedir SÍ/NO.
        # Si detectamos el patrón "Nombre, M/F, DD/MM/YYYY", redirigimos a
        # WAIT_MODALIDAD igual que si hubiera presionado "cambiar_datos".
        import re as _re_corr
        _RE_DATOS_RECITADOS = _re_corr.compile(
            r"^[A-Za-zÁÉÍÓÚáéíóúñÑ ]{3,},\s*[MFmf]\s*,\s*\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}\s*$"
        )
        if _RE_DATOS_RECITADOS.match(txt.strip()):
            log_event(phone, "confirming_recibe_datos_correccion", {"raw": txt[:80]})
            data.pop("paciente", None)
            data.pop("rut", None)
            data["datos_corregidos_pending"] = txt.strip()  # para WAIT_DATOS_NUEVO
            perfil = get_profile(phone)
            if perfil and perfil.get("rut"):
                data["rut_conocido"] = perfil["rut"]
                data["nombre_conocido"] = perfil.get("nombre", "")
            slot = data.get("slot_elegido", {})
            save_session(phone, "WAIT_MODALIDAD", data)
            return _btn_msg(
                f"Veo que querías corregir tus datos 😊\n\n"
                f"Tu hora sigue apartada:\n"
                f"🏥 *{slot.get('especialidad', '')}* — {slot.get('profesional', '')}\n"
                f"📅 *{slot.get('fecha_display', '')}*\n"
                f"🕐 *{slot.get('hora_inicio', '')[:5]}*\n\n"
                "¿Tu atención será Fonasa o Particular?",
                [{"id": "1", "title": "Fonasa"}, {"id": "2", "title": "Particular"}]
            )
        # Paciente fast-track quiere cambiar datos → flujo completo desde WAIT_MODALIDAD
        if tl == "cambiar_datos":
            data.pop("paciente", None)
            data.pop("rut", None)
            # Preservar rut_conocido del perfil para el atajo en WAIT_RUT_AGENDAR
            perfil = get_profile(phone)
            if perfil and perfil.get("rut"):
                data["rut_conocido"] = perfil["rut"]
                data["nombre_conocido"] = perfil.get("nombre", "")
            slot = data.get("slot_elegido", {})
            save_session(phone, "WAIT_MODALIDAD", data)
            return _btn_msg(
                f"Sin problema 😊 Tu hora sigue apartada:\n\n"
                f"🏥 *{slot.get('especialidad', '')}* — {slot.get('profesional', '')}\n"
                f"📅 *{slot.get('fecha_display', '')}*\n"
                f"🕐 *{slot.get('hora_inicio', '')[:5]}*\n\n"
                "¿Tu atención será Fonasa o Particular?",
                [{"id": "1", "title": "Fonasa"}, {"id": "2", "title": "Particular"}]
            )
        # ── Paciente declinó la confirmación de cita duplicada ──
        if data.get("dup_pending") and (tl in NEGACIONES or tl_norm in NEGACIONES):
            data.pop("dup_pending", None)
            reset_session(phone)
            log_event(phone, "cita_duplicada_rechazada", {})
            return (
                "Sin problema 😊 Mantienes solo la hora original.\n\n"
                "_Escribe *menu* si necesitas algo más._"
            )
        if tl in AFIRMACIONES or tl_norm in AFIRMACIONES:
            slot    = data.get("slot_elegido")
            paciente = data.get("paciente")
            if not slot or not paciente:
                # Sesión sin datos clave (limpieza parcial, race, admin_resume manual).
                reset_session(phone)
                return (
                    "Perdimos el hilo de tu reserva 😅 "
                    "Escribe *menu* para empezar de nuevo o *agendar* directamente."
                )
            cita_old = data.get("cita_old") or {}
            # Defensa sistémica: si hay cita_old con id, tratar como reagendar
            # incluso si el flag se perdió. Auditoría 2026-04-28: 2 casos con
            # id_cita_old=null en cita_creada y sin cita_cancelada — el flag
            # reagendar_mode se perdía en algún save_session intermedio.
            reagendar = bool(data.get("reagendar_mode")) or bool(cita_old.get("id"))
            # ── Validación: paciente ya tiene cita activa con el MISMO profesional ──
            # Bloqueo duro — no permitimos múltiples horas activas con el mismo
            # profesional para evitar auto-agendamientos en cascada (caso real:
            # paciente reservaba 19:20, 19:40 y 20:00 con el mismo doctor).
            # En reagendar la antigua se cancela igual; ahí saltamos el check.
            if not reagendar:
                try:
                    existing_citas = await listar_citas_paciente(
                        paciente["id"], rut=paciente.get("rut")
                    )
                except Exception as e:
                    log.warning("dup-check listar_citas falló: %s", e)
                    existing_citas = []

                # 1) Bloqueo duro: misma profesional ya reservada
                same_prof = next(
                    (c for c in (existing_citas or [])
                     if str(c.get("id_profesional", "")) == str(slot.get("id_profesional", ""))),
                    None,
                )
                if same_prof:
                    log_event(phone, "cita_bloqueada_mismo_profesional", {
                        "id_profesional": slot.get("id_profesional"),
                        "fecha_existente": same_prof.get("fecha"),
                        "hora_existente": same_prof.get("hora_inicio", "")[:5],
                    })
                    reset_session(phone)
                    return (
                        f"📋 *Ya tienes una hora reservada con {same_prof.get('profesional','este profesional')}.*\n\n"
                        f"📅 {same_prof.get('fecha','')} a las "
                        f"*{(same_prof.get('hora_inicio','') or '')[:5]}*\n\n"
                        f"Solo permitimos *una hora activa por profesional*. "
                        f"Si quieres cambiar el horario, escribe *reagendar*. "
                        f"Si necesitas una segunda atención, escribe *recepción* "
                        f"para hablar con una secretaria."
                    )

                # 2) Soft-warn: misma fecha + especialidad (puede ser válido en algunos casos)
                if not data.get("dup_ok"):
                    if data.get("dup_pending"):
                        data["dup_ok"] = True
                        data.pop("dup_pending", None)
                    else:
                        _slot_esp = (slot.get("especialidad") or "").strip().lower()
                        dup = next(
                            (c for c in (existing_citas or [])
                             if c.get("fecha") == slot.get("fecha")
                             and (c.get("especialidad") or "").strip().lower() == _slot_esp),
                            None,
                        )
                        if dup:
                            data["dup_pending"] = True
                            save_session(phone, "CONFIRMING_CITA", data)
                            log_event(phone, "cita_duplicada_detectada", {
                                "fecha": slot.get("fecha"),
                                "especialidad": slot.get("especialidad"),
                                "existing_hora": dup.get("hora_inicio", "")[:5],
                            })
                            return _btn_msg(
                                f"⚠️ *Ya tienes una hora ese día*\n\n"
                                f"📋 Tienes *{dup.get('especialidad','')}* el "
                                f"{slot.get('fecha_display','')} a las "
                                f"*{dup.get('hora_inicio','')}* con "
                                f"{dup.get('profesional','')}.\n\n"
                                f"¿Igual quieres agendar esta segunda hora a las "
                                f"*{slot['hora_inicio'][:5]}*?",
                                [{"id": "si", "title": "✅ Sí, agendar igual"},
                                 {"id": "no", "title": "❌ Cancelar"}]
                            )
            # ── Doble-check: verificar que el slot sigue libre ──
            slot_libre = await verificar_slot_disponible(
                slot["id_profesional"], slot["fecha"],
                slot["hora_inicio"], slot["hora_fin"],
            )
            if not slot_libre:
                log.warning("Slot %s %s ya no está disponible para prof %s",
                            slot["fecha"], slot["hora_inicio"], slot["id_profesional"])
                log_event(phone, "slot_ya_ocupado", {
                    "fecha": slot["fecha"], "hora": slot["hora_inicio"],
                    "profesional": slot.get("profesional", ""),
                })
                # Re-buscar y ofrecer nueva hora
                esp = data.get("especialidad", slot.get("especialidad", ""))
                smart, todos = await buscar_primer_dia(esp)
                if smart:
                    new_slot = smart[0]
                    data["slot_elegido"] = new_slot
                    save_session(phone, "CONFIRMING_CITA", data)
                    return _btn_msg(
                        f"⚠️ Esa hora ya fue tomada. Te encontré otra:\n\n"
                        f"🏥 *{new_slot['especialidad']}* — {new_slot['profesional']}\n"
                        f"📅 *{new_slot['fecha_display']}*\n"
                        f"🕐 *{new_slot['hora_inicio'][:5]}*\n\n"
                        "¿Te la reservo?",
                        [{"id": "si", "title": "✅ Sí, reservar"},
                         {"id": "no", "title": "❌ No"}]
                    )
                else:
                    reset_session(phone)
                    return "😔 Lo siento, esa hora fue tomada y no encontré otra disponible. Escribe *hola* para intentar de nuevo."
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
            # Guardar marca de booking reciente para detectar correcciones
            # de titular post-confirmación (bug 56981328760 2026-04-25 13:29).
            try:
                from datetime import datetime as _dt_lb, timezone as _tz_lb
                from session import save_session as _save_lb, get_session as _get_lb
                _sess_lb = _get_lb(phone) or {"state": "IDLE", "data": {}}
                _data_lb = _sess_lb.get("data", {}) or {}
                _data_lb["last_booking_ts"] = _dt_lb.now(_tz_lb.utc).isoformat()
                _save_lb(phone, "IDLE", _data_lb)
            except Exception:
                pass
            nombre_corto = _first_name(paciente.get('nombre'))
            modalidad = data.get("modalidad", "particular").capitalize()
            es_tercero = bool(data.get("booking_for_other"))
            if resultado:
                # Guardar perfil solo si agenda para sí mismo
                if not es_tercero:
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
                    paciente_nombre=paciente["nombre"],
                    es_tercero=es_tercero,
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
                    _hitos = get_milestones_reminder(fecha_nac, paciente["nombre"])
                    if _hitos:
                        pni_msg += f"\n\n{_hitos}"
                if reagendar:
                    extra = ""
                    if not cancel_ok:
                        extra = (
                            "\n\n⚠️ _Tuvimos un inconveniente cancelando la hora anterior; "
                            "recepción la anulará de forma manual. No hay problema._"
                        )
                    if es_tercero:
                        titulo = f"🔄 *¡Listo! La hora de {nombre_corto} fue reagendada.*"
                    else:
                        titulo = f"🔄 *¡Listo, {nombre_corto}! Tu hora fue reagendada.*"
                    return (
                        f"{titulo}\n\n"
                        f"👤 {paciente['nombre']}\n"
                        f"🏥 {slot['especialidad']} — {slot['profesional']}\n"
                        f"📅 {slot['fecha_display']}\n"
                        f"🕐 {slot['hora_inicio'][:5]}\n\n"
                        "Recuerda llegar *15 minutos antes* con cédula de identidad.\n\n"
                        "📍 *Monsalve 102 esq. República, Carampangue*"
                        f"{extra}"
                        f"{cross_ref}"
                        f"{pni_msg}\n\n"
                        "_Escribe *menu* si necesitas algo más._"
                    )
                if es_tercero:
                    titulo = f"✅ *¡Listo! La hora de {nombre_corto} quedó reservada.*"
                else:
                    titulo = f"✅ *¡Listo, {nombre_corto}! Tu hora quedó reservada.*"
                confirmacion_msg = (
                    f"{titulo}\n\n"
                    f"👤 {paciente['nombre']}\n"
                    f"🏥 {slot['especialidad']} — {slot['profesional']}\n"
                    f"📅 {slot['fecha_display']}\n"
                    f"🕐 {slot['hora_inicio'][:5]}\n"
                    f"💳 {modalidad}\n\n"
                    "Recuerda llegar *15 minutos antes* con cédula de identidad.\n\n"
                    "📍 *Monsalve 102 esq. República, Carampangue*\n\n"
                    f"¡Te esperamos! 😊{cross_ref}"
                    f"{pni_msg}"
                )
                # Si es paciente nuevo registrado en este flujo, pedir referido
                # como segundo mensaje con botones (post-confirmación, baja fricción).
                if data.get("is_paciente_nuevo_post_referral"):
                    save_session(phone, "WAIT_REFERRAL_POST", {})
                    # send_whatsapp ya está importado globalmente (línea 33).
                    # El re-import local previo causaba UnboundLocalError porque
                    # Python trataba send_whatsapp como local en TODA la función.
                    # Caso real 2026-04-24 (56999988115).
                    await send_whatsapp(phone, confirmacion_msg)
                    return _btn_msg(
                        "Una última cosa rápida 🙏\n\n*¿Cómo nos conociste?*",
                        [{"id": "ref_amigo", "title": "👥 Amigo / familiar"},
                         {"id": "ref_rrss", "title": "📱 Redes / Google"},
                         {"id": "ref_recurrente", "title": "🔄 Ya venía antes"}]
                    )
                return confirmacion_msg + "\n\n_Escribe *menu* si necesitas algo más._"
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

        return _btn_msg(
            "Responde *Sí* para confirmar o *No* para cambiar.",
            [{"id": "si", "title": "✅ Sí, reservar"},
             {"id": "no", "title": "❌ Cambiar"}]
        )

    # ── WAIT_RUT_CANCELAR ─────────────────────────────────────────────────────
    if state == "WAIT_RUT_CANCELAR":
        # Escape: usuario menciona un profesional → se equivocó y quiere agendar
        apellido_esc = _detectar_apellido_profesional(txt)
        if apellido_esc and any(
            k in tl for k in ("necesito", "quiero", "equivoque", "equivoqué",
                              "con el", "con la", "dr ", "dra ", "doctor ", "doctora ")
        ):
            reset_session(phone)
            return await _iniciar_agendar(phone, {}, apellido_esc)
        rut = clean_rut(txt)
        if not valid_rut(rut):
            return (
                "Hmm, no reconozco ese RUT 🤔\n"
                "Escríbelo así: *12.345.678-9*"
            )

        _ensure_consent(phone)
        paciente, transient = await _buscar_paciente_safe(rut)
        if transient:
            save_session(phone, "HUMAN_TAKEOVER", data)
            return _msg_medilink_transient()
        if not paciente:
            reset_session(phone)
            return (
                "No tenemos ese RUT registrado 😊\n\n"
                f"¿Necesitas ayuda? Llama a recepción:\n📞 *{CMC_TELEFONO}*\n\n"
                "_Escribe *menu* para volver._"
            )

        citas = await listar_citas_paciente(paciente["id"], rut=paciente.get("rut"))
        if not citas:
            reset_session(phone)
            return (
                f"No tienes citas futuras agendadas, *{_first_name(paciente.get('nombre'))}* 📋\n\n"
                "¿Quieres agendar una hora?"
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
            return f"Elige un número entre 1 y {len(citas)} 😊\n_(o escribe *menu* para volver al inicio)_"

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
            cita = data.get("cita_cancelar")
            if not cita or not cita.get("id"):
                log.warning("CONFIRMING_CANCEL sin cita_cancelar en sesión phone=%s", phone)
                reset_session(phone)
                return "No pude recuperar la cita a cancelar. ¿Me das tu RUT para revisar tus reservas?"
            ok = await cancelar_cita(cita["id"])
            reset_session(phone)
            if ok:
                log_event(phone, "cita_cancelada", {"id_cita": cita["id"], "profesional": cita.get("profesional")})
                save_tag(phone, "canceló")
                # ── Event-driven: notificar waitlist al instante ──
                esp_cancelada = cita.get("especialidad", "")
                if esp_cancelada:
                    try:
                        waiters = get_waitlist_by_especialidad(esp_cancelada)
                        for w in waiters[:3]:  # notificar hasta 3 personas
                            w_phone = w["phone"]
                            w_nombre = (w.get("nombre") or "").split()
                            w_saludo = f"*{w_nombre[0]}*" if w_nombre else ""
                            await send_whatsapp(
                                w_phone,
                                f"Hola {w_saludo} 👋 ¡Se acaba de liberar una hora de "
                                f"*{esp_cancelada}* con *{cita.get('profesional', '')}*!\n\n"
                                f"📅 *{cita.get('fecha_display', '')}* a las *{cita.get('hora_inicio', '')}*\n\n"
                                "Escribe *menu* ahora para reservarla antes de que se llene."
                            )
                            mark_waitlist_notified(w["id"])
                            log_event(w_phone, "waitlist_notificado_cancelacion", {
                                "especialidad": esp_cancelada, "cita_cancelada": cita["id"],
                            })
                    except Exception as e:
                        log.warning("Error notificando waitlist post-cancel: %s", e)
                return _btn_msg(
                    f"✅ Cita cancelada.\n\n"
                    f"_{cita['profesional']} · {cita['fecha_display']} · {cita['hora_inicio']}_\n\n"
                    "¿Quieres agendar otra hora?",
                    [
                        {"id": "1", "title": "📅 Sí, agendar"},
                        {"id": "menu_volver", "title": "No, gracias"},
                    ]
                )
            return f"Hubo un problema al cancelar 😕\nLlama a recepción: 📞 *{CMC_TELEFONO}*"

        if tl in NEGACIONES or tl_norm in NEGACIONES:
            reset_session(phone)
            return "Perfecto, tu cita se mantiene 😊\n_Escribe *menu* si necesitas algo más._"

        return _btn_msg(
            "Responde *Sí* para cancelar o *No* para mantener la cita.",
            [{"id": "si", "title": "✅ Sí, cancelar"},
             {"id": "no", "title": "❌ Mantener cita"}]
        )

    # ── WAIT_RUT_REAGENDAR ────────────────────────────────────────────────────
    if state == "WAIT_RUT_REAGENDAR":
        apellido_esc = _detectar_apellido_profesional(txt)
        if apellido_esc:
            reset_session(phone)
            return await _iniciar_agendar(phone, {}, apellido_esc)
        # Usuario respondió con hora/día en vez de RUT ("a las 15:00", "lunes")
        # Excluye RUTs ("12345678-9") y strings-de-digitos-sueltos.
        import re as _re_time
        _parece_rut = bool(_re_time.search(r'\d[-][0-9kK]\b', txt))
        _tiene_hora_explicita = bool(_re_time.search(r'\b\d{1,2}[:.]\d{2}\b', txt))
        _tiene_hora_texto = any(
            k in tl for k in ("a las ", "hrs", "hs", "horas", "puede ser",
                               "lunes", "martes", "miercoles", "miércoles",
                               "jueves", "viernes", "sabado", "sábado",
                               "mañana", "manana", "hoy", "tarde")
        )
        if not _parece_rut and (_tiene_hora_explicita or _tiene_hora_texto):
            # Guardar la preferencia si la extraemos — la usamos cuando tengamos el RUT
            data["reagendar_preferencia"] = txt[:120]
            save_session(phone, "WAIT_RUT_REAGENDAR", data)
            return (
                "¡Perfecto, anoté tu preferencia! 🗓️\n\n"
                "Primero necesito tu *RUT* para buscar tu cita actual:\n"
                "(ej: *12.345.678-9*)\n\n"
                "Cuando me lo des, te ofrezco nuevos horarios."
            )
        rut = clean_rut(txt)
        if not valid_rut(rut):
            return (
                "Hmm, no reconozco ese RUT 🤔\n"
                "Escríbelo así: *12.345.678-9*\n\n"
                "_Si quieres cancelar, escribe *menu*._"
            )

        _ensure_consent(phone)
        paciente, transient = await _buscar_paciente_safe(rut)
        if transient:
            save_session(phone, "HUMAN_TAKEOVER", data)
            return _msg_medilink_transient()
        if not paciente:
            reset_session(phone)
            return (
                "No tenemos ese RUT registrado 😊\n\n"
                f"¿Necesitas ayuda? Llama a recepción:\n📞 *{CMC_TELEFONO}*\n\n"
                "_Escribe *menu* para volver._"
            )

        citas = await listar_citas_paciente(paciente["id"], rut=paciente.get("rut"))
        if not citas:
            reset_session(phone)
            return (
                f"No tienes citas futuras agendadas, *{_first_name(paciente.get('nombre'))}* 📋\n\n"
                "¿Quieres agendar una hora?"
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
            return f"Elige un número entre 1 y {len(citas)} 😊\n_(o escribe *menu* para volver al inicio)_"

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
                + _PRIVACY_NOTE
            )
        if tl == "waitlist_no" or tl in NEGACIONES or tl_norm in NEGACIONES:
            reset_session(phone)
            return (
                "Sin problema 😊 Cuando lo necesites, escríbenos.\n"
                f"_Llama a recepción: 📞 *{CMC_TELEFONO}* · ☎️ *{CMC_TELEFONO_FIJO}*_"
            )
        # Defensa sistémica: si paciente envía intent claro distinto a SI/NO,
        # liberar el estado y procesar como IDLE. Antes el bot quedaba bloqueado
        # ofreciendo waitlist de una especialidad que el paciente ya no quería.
        # Auditoría 2026-04-28: 12 takeovers desde WAIT_WAITLIST_CONFIRM por
        # contaminación de estado (caso 56989488187: bot pedía waitlist de
        # implantología cuando paciente había pedido medicina general).
        _NUEVO_INTENT_KW = (
            "agendar", "hora", "consulta", "cita", "reservar",
            "kine", "kinesiolog", "medico", "médico", "doctor",
            "dental", "odontolog", "psicolog", "ginecolog",
            "ortodonc", "endodonc", "implantolog", "ecograf",
            "nutricion", "matrona", "podologo", "fonoaudiolog",
            "cancelar", "anular", "cambiar", "ver mis", "mis citas",
            "menu", "menú",
        )
        if any(kw in tl for kw in _NUEVO_INTENT_KW):
            log_event(phone, "waitlist_confirm_break", {"txt": txt[:120]})
            reset_session(phone)
            # Reentrar handle_message con la sesión limpia para procesar el
            # nuevo intent. Es recursión controlada (1 nivel máximo: IDLE no
            # vuelve a WAIT_WAITLIST_CONFIRM dentro del mismo turn).
            return await handle_message(phone, txt, get_session(phone))
        return "Responde *SÍ* para inscribirte o *NO* si prefieres llamar a recepción."

    # ── WAIT_WAITLIST_RUT ─────────────────────────────────────────────────────
    if state == "WAIT_WAITLIST_RUT":
        rut = clean_rut(txt)
        if not valid_rut(rut):
            return (
                "Hmm, no reconozco ese RUT 🤔\n"
                "Escríbelo así: *12.345.678-9*"
            )
        _ensure_consent(phone)
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
        # Escape: el usuario menciona un profesional — aclarar que primero
        # necesitamos su RUT para ver sus citas, sin abandonar el flujo.
        apellido_esc = _detectar_apellido_profesional(txt)
        if apellido_esc:
            save_session(phone, "WAIT_RUT_VER", data)
            return (
                f"Para ver tu cita con *{apellido_esc.title()}* necesito tu *RUT* primero 😊\n"
                f"(ej: *12.345.678-9*)\n\n"
                f"_Si querías agendar con otro doctor, escribe *menu*._"
            )
        # Tiempo/día en vez de RUT → clarificar
        import re as _re_time
        _parece_rut_ver = bool(_re_time.search(r'\d[-][0-9kK]\b', txt))
        if not _parece_rut_ver and any(
            k in tl for k in ("a las ", "hoy", "mañana", "manana", "lunes",
                               "martes", "miercoles", "miércoles", "jueves",
                               "viernes", "sabado", "sábado")
        ):
            save_session(phone, "WAIT_RUT_VER", data)
            return (
                "Primero necesito tu *RUT* para buscar tu cita 😊\n"
                "(ej: *12.345.678-9*)"
            )
        rut = clean_rut(txt)
        if not valid_rut(rut):
            return (
                "Hmm, no reconozco ese RUT 🤔\n"
                "Escríbelo así: *12.345.678-9*"
            )

        _ensure_consent(phone)
        paciente, transient = await _buscar_paciente_safe(rut)
        if transient:
            save_session(phone, "HUMAN_TAKEOVER", data)
            return _msg_medilink_transient()
        if not paciente:
            reset_session(phone)
            return "No encontré ese RUT 🔎\nEscribe *menu* para volver o intenta de nuevo."

        citas = await listar_citas_paciente(paciente["id"], rut=paciente.get("rut"))
        reset_session(phone)
        nombre_corto = _first_name(paciente.get('nombre'))
        if not citas:
            return _btn_msg(
                f"No tienes citas futuras agendadas, *{nombre_corto}* 📋",
                [
                    {"id": "1", "title": "📅 Agendar hora"},
                    {"id": "menu_volver", "title": "Ver menú"},
                ]
            )

        lineas = [f"📋 *Tus próximas citas, {nombre_corto}:*\n"]
        for c in citas:
            lineas.append(f"• {c['fecha_display']} {c['hora_inicio']} — {c['profesional']}")
        body = "\n".join(lineas)
        return _btn_msg(
            body,
            [
                {"id": "1", "title": "📅 Agendar otra"},
                {"id": "menu_volver", "title": "Listo"},
            ]
        )

    # ── WAIT_DATOS_NUEVO (registro en un solo mensaje) ────────────────────────
    if state == "WAIT_DATOS_NUEVO":
        raw = txt.strip()

        # ── Separar por comas, punto y coma, pipe, barras, saltos de línea
        # y guiones/raya larga con espacios ("Ruth - Femenino - 28/05/1939"). ──
        parts = [p.strip() for p in re.split(r'[,;|/\n]+|\s+[-–—]+\s+', raw) if p.strip()]

        nombre_raw = None
        sexo = None
        fecha_nac = None
        celular_raw = None
        _SEX_M = re.compile(r'^(m|masculino|hombre|masc)$', re.I)
        _SEX_F = re.compile(r'^(f|femenino|mujer|fem)$', re.I)
        _PHONE_RE = re.compile(r'^(\+?56)?[0-9\s\-]{8,12}$')

        for part in parts:
            p = part.strip()
            # ¿Es sexo?
            if not sexo and _SEX_M.match(p):
                sexo = "M"; continue
            if not sexo and _SEX_F.match(p):
                sexo = "F"; continue
            # ¿Es número de celular? (9 dígitos chilenos, opcionalmente +56)
            if not celular_raw and _PHONE_RE.match(p):
                digits = re.sub(r'[^\d]', '', p)
                if digits.startswith("56") and len(digits) >= 10:
                    celular_raw = digits[2:]  # sin código país
                    continue
                elif len(digits) >= 8 and len(digits) <= 9:
                    celular_raw = digits
                    continue
            # ¿Es fecha?
            if not fecha_nac:
                f = _parsear_fecha_nacimiento(p)
                if f:
                    fecha_nac = f; continue
            # Lo demás es nombre (primera parte no-matcheada)
            if not nombre_raw:
                nombre_raw = p

        # Si no hubo comas, intentar extraer de tokens sueltos
        if not sexo and nombre_raw:
            tokens = nombre_raw.split()
            for i, t in enumerate(tokens):
                if _SEX_M.match(t):
                    sexo = "M"; tokens.pop(i); nombre_raw = " ".join(tokens); break
                if _SEX_F.match(t):
                    sexo = "F"; tokens.pop(i); nombre_raw = " ".join(tokens); break
        if not fecha_nac and nombre_raw:
            fecha_nac = _parsear_fecha_nacimiento(nombre_raw)
            if fecha_nac:
                nombre_raw = re.sub(r'\d{1,2}[/-]\d{1,2}[/-]\d{2,4}', '', nombre_raw)
                nombre_raw = re.sub(r'\b\d{8}\b', '', nombre_raw)
                nombre_raw = re.sub(r'\d{1,2}\s+de\s+\w+\s+(de\s+)?\d{4}', '', nombre_raw, flags=re.I)
                nombre_raw = nombre_raw.strip()

        # Limpiar nombre
        nombre_raw = re.sub(r'\s+', ' ', nombre_raw or '').strip()
        is_social = phone.startswith("ig_") or phone.startswith("fb_")
        _ej = "María González López, F, 15/03/1990"
        _tip = "\n(Si quieres, agrega tu celular al final: _…, 912345678_)" if is_social else ""
        if not nombre_raw or not re.match(r"^[a-záéíóúñüA-ZÁÉÍÓÚÑÜ\s\-']{3,60}$", nombre_raw):
            return (
                "No reconocí el nombre 😕\n\n"
                "Escríbelo separado por comas:\n"
                "*Nombre Apellido, M o F, DD/MM/AAAA*\n\n"
                f"_Ejemplo: {_ej}_{_tip}"
            )
        partes_nombre = nombre_raw.split()
        if len(partes_nombre) < 2:
            return f"Necesito nombre y apellido, por ejemplo:\n*{_ej}*"

        nombre   = partes_nombre[0].capitalize()
        apellidos = " ".join(p.capitalize() for p in partes_nombre[1:])

        # ── Crear paciente con los datos básicos ──
        rut = data.get("rut", "")
        extra: dict = {}
        if fecha_nac:
            from datetime import date as _date_check
            if fecha_nac.year >= 1920 and fecha_nac <= datetime.now(_CHILE_TZ).date():
                extra["fecha_nacimiento"] = fecha_nac.strftime("%Y-%m-%d")
                data["reg_fecha_nacimiento"] = extra["fecha_nacimiento"]
        if sexo:
            extra["sexo"] = sexo
        # Celular: en IG/FB usar el que escribió, en WA auto-rellenar del número
        is_social = phone.startswith("ig_") or phone.startswith("fb_")
        if celular_raw:
            extra["celular"] = celular_raw
            extra["telefono"] = celular_raw
        elif not is_social:
            cel = phone.lstrip("+")
            if cel.startswith("56") and len(cel) >= 10:
                extra["celular"] = cel[2:]
                extra["telefono"] = cel[2:]

        log_event(phone, "registro_completo", {
            "rut": rut, "campos_extra": list(extra.keys()),
            "total_campos": len(extra),
        })
        data["is_paciente_nuevo_post_referral"] = True  # pedir referido tras confirmar
        paciente = await crear_paciente(rut, nombre, apellidos, **extra)
        if not paciente:
            reset_session(phone)
            return f"Hubo un problema al registrarte 😕\nLlama a recepción: 📞 *{CMC_TELEFONO}*"

        save_profile(phone, rut, paciente["nombre"],
                     fecha_nacimiento=data.get("reg_fecha_nacimiento"))
        # Código de referido (silencioso)
        try:
            from session import generate_referral_code
            generate_referral_code(phone)
        except Exception:
            pass

        data.update({"paciente": paciente, "rut": rut})
        save_session(phone, "CONFIRMING_CITA", data)
        slot = data["slot_elegido"]
        modalidad = data.get("modalidad", "particular").capitalize()
        return _btn_msg(
            f"¡Registrado/a, *{nombre}*! 🙌\n\n"
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

    # ── WAIT_NOMBRE_NUEVO (legacy — para sesiones activas pre-update) ─────────
    if state == "WAIT_NOMBRE_NUEVO":
        nombre_raw = txt.strip()
        # Validar que solo tenga letras, espacios, guiones y apóstrofes
        if not re.match(r"^[a-záéíóúñüA-ZÁÉÍÓÚÑÜ\s\-']{3,60}$", nombre_raw):
            return "Escribe tu nombre usando solo letras (ej: *María González*)."
        partes = nombre_raw.split()
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
            f"Gracias, *{nombre}* 😊 Solo faltan unos datos rápidos "
            "(puedes escribir *saltar* en cualquiera).\n\n"
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
            if fecha_nac.year < 1920 or fecha_nac > datetime.now(_CHILE_TZ).date():
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
            if re.match(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", email):
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
                {"id": "ref_codigo",     "title": "Tengo un código"},
                {"id": "ref_saltar",     "title": "Prefiero no decir"},
            ]}]
        )

    # ── WAIT_REFERRAL_POST (1 mensaje post-confirmación, baja fricción) ──
    if state == "WAIT_REFERRAL_POST":
        _POST_MAP = {
            "ref_amigo": "amigo",
            "ref_rrss": "rrss",
            "ref_recurrente": "recurrente",
            "ref_google": "google",
        }
        # Mapeo por button id o por texto libre
        ref_source = _POST_MAP.get(tl)
        if not ref_source:
            tl_low = tl.lower()
            if any(w in tl_low for w in ("amig", "famili", "conoci", "vecin", "recomen")):
                ref_source = "amigo"
            elif any(w in tl_low for w in ("instagram", "facebook", "tiktok", "red social", "rrss", "google", "internet", "busq")):
                ref_source = "rrss" if "google" not in tl_low else "google"
            elif any(w in tl_low for w in ("antes", "siempre", "años", "venia", "venía", "recurr")):
                ref_source = "recurrente"
            elif any(w in tl_low for w in ("volante", "calle", "letrero", "fachada", "pasaba")):
                ref_source = "calle"
        if ref_source:
            save_tag(phone, f"referido:{ref_source}")
            log_event(phone, "registro_referral_post", {"source": ref_source, "raw": txt[:60]})
            reset_session(phone)
            return "¡Gracias! 🙏 Eso nos ayuda a mejorar.\n\n_Escribe *menu* si necesitas algo más._"
        # Si no se mapeó, agradecer y soltar igual
        log_event(phone, "registro_skip", {"step": "referral_post", "raw": txt[:60]})
        reset_session(phone)
        return "Perfecto, gracias 🙏\n\n_Escribe *menu* si necesitas algo más._"

    # ── WAIT_REFERRAL ─────────────────────────────────────────────────────
    if state == "WAIT_REFERRAL":
        _REF_MAP = {
            "ref_amigo": "amigo", "ref_google": "google",
            "ref_rrss": "rrss", "ref_recurrente": "recurrente",
        }
        ref_source = _REF_MAP.get(tl)
        if tl == "ref_codigo":
            # Pedir que escriba el código
            save_session(phone, "WAIT_REFERRAL_CODE", data)
            return "Escribe tu código de referido (ej: *CMC-A1B2*):"
        if not ref_source and tl in ("saltar", "skip", "paso", "no", "ref_saltar"):
            log_event(phone, "registro_skip", {"step": "referral"})
        elif ref_source:
            save_tag(phone, f"referido:{ref_source}")
            log_event(phone, "registro_referral", {"source": ref_source})
        else:
            # Código de referido (CMC-XXXX)
            import re as _re_ref
            _code_match = _re_ref.match(r"^CMC-[A-Z0-9]{4}$", txt.upper().strip())
            if _code_match:
                from session import validate_referral_code, use_referral_code
                _code = _code_match.group(0)
                _ref_data = validate_referral_code(_code)
                if _ref_data:
                    use_referral_code(_code, phone)
                    save_tag(phone, "referido:codigo")
                    log_event(phone, "registro_referral", {
                        "source": "codigo", "code": _code,
                        "referrer": _ref_data["phone"]})
                else:
                    log_event(phone, "registro_skip", {
                        "step": "referral", "raw": txt[:60],
                        "invalid_code": True})
            # Texto libre: intentar mapear
            elif any(w in tl for w in ("amig", "famili", "conoci", "vecin")):
                save_tag(phone, "referido:amigo")
                log_event(phone, "registro_referral", {"source": "amigo", "raw": txt[:60]})
            elif any(w in tl for w in ("google", "internet", "busq", "web")):
                save_tag(phone, "referido:google")
                log_event(phone, "registro_referral", {"source": "google", "raw": txt[:60]})
            elif any(w in tl for w in ("instagram", "facebook", "tiktok", "red")):
                save_tag(phone, "referido:rrss")
                log_event(phone, "registro_referral", {"source": "rrss", "raw": txt[:60]})
            elif any(w in tl for w in ("antes", "siempre", "años", "venia", "venía")):
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
        # Guardar perfil con fecha_nacimiento para campaña de cumpleaños
        save_profile(phone, rut, paciente["nombre"],
                     fecha_nacimiento=data.get("reg_fecha_nacimiento"))
        # Enviar mensaje de bienvenida (no-blocking)
        try:
            bienvenida = (
                f"¡Bienvenido/a al *Centro Médico Carampangue*, *{nombre}*! 🏥\n\n"
                "Desde ahora puedes:\n"
                "• Agendar, cancelar o reagendar citas\n"
                "• Consultar horarios y precios\n"
                "• Recibir recordatorios automáticos\n\n"
                "Todo escribiéndonos aquí por WhatsApp, a cualquier hora.\n"
                "Si necesitas hablar con recepción, escribe *recepción*.\n\n"
                f"📍 Monsalve 102 esq. República, Carampangue\n"
                f"📞 {CMC_TELEFONO}"
            )
            await send_whatsapp(phone, bienvenida)
            from session import log_message as _log_msg
            _log_msg(phone, "out", bienvenida, "CONFIRMING_CITA")
            log_event(phone, "bienvenida_enviada", {})
        except Exception as e:
            log.warning("Error enviando bienvenida phone=%s: %s", phone, e)
        # Generar código de referido para el nuevo paciente
        try:
            from session import generate_referral_code
            ref_code = generate_referral_code(phone)
            ref_msg = (
                f"🎁 *Tu código de referido: {ref_code}*\n\n"
                "Compártelo con amigos y familiares. "
                "Cuando alguien se registre con tu código, "
                "ambos recibirán un beneficio."
            )
            await send_whatsapp(phone, ref_msg)
        except Exception as e:
            log.warning("Error generando código referido phone=%s: %s", phone, e)
        data.update({"paciente": paciente, "rut": rut})
        save_session(phone, "CONFIRMING_CITA", data)
        slot = data["slot_elegido"]
        modalidad = data.get("modalidad", "particular").capitalize()
        _sx = (paciente.get("sexo") or data.get("sexo") or "").upper()
        _flex_reg = "registrada" if _sx == "F" else "registrado"
        return _btn_msg(
            f"¡Listo, *{nombre}*! Ya estás {_flex_reg} 🙌\n\n"
            f"Te reservo esta hora:\n\n"
            f"👤 {paciente['nombre']}\n"
            f"🏥 {slot['especialidad']} — {slot['profesional']}\n"
            f"📅 {slot['fecha_display']}\n"
            f"🕐 {slot['hora_inicio'][:5]}\n"
            f"💳 {modalidad}\n\n"
            "¿La confirmo?",
            [
                {"id": "si", "title": "✅ Sí, reservar"},
                {"id": "no", "title": "❌ Cambiar"},
            ]
        )

    # ── WAIT_REFERRAL_CODE ────────────────────────────────────────────────────
    if state == "WAIT_REFERRAL_CODE":
        import re as _re_ref2
        _code_match2 = _re_ref2.match(r"^CMC-[A-Z0-9]{4}$", txt.upper().strip())
        if _code_match2:
            from session import validate_referral_code, use_referral_code
            _code2 = _code_match2.group(0)
            _ref_data2 = validate_referral_code(_code2)
            if _ref_data2:
                use_referral_code(_code2, phone)
                save_tag(phone, "referido:codigo")
                log_event(phone, "registro_referral", {
                    "source": "codigo", "code": _code2,
                    "referrer": _ref_data2["phone"]})
            else:
                log_event(phone, "registro_skip", {
                    "step": "referral_code", "invalid_code": _code2})
        elif tl in ("saltar", "skip", "no", "paso"):
            log_event(phone, "registro_skip", {"step": "referral_code"})
        else:
            log_event(phone, "registro_skip", {
                "step": "referral_code", "raw": txt[:60]})
        # Continuar con creación del paciente (mismo código que WAIT_REFERRAL)
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
                "Hubo un problema al registrarte \U0001f615\n"
                f"Llama a recepción: \U0001f4de *{CMC_TELEFONO}*"
            )
        save_profile(phone, rut, paciente["nombre"],
                     fecha_nacimiento=data.get("reg_fecha_nacimiento"))
        data.update({"paciente": paciente, "rut": rut})
        save_session(phone, "CONFIRMING_CITA", data)
        slot = data["slot_elegido"]
        modalidad = data.get("modalidad", "particular").capitalize()
        _sx2 = (paciente.get("sexo") or data.get("sexo") or "").upper()
        _flex_reg2 = "registrada" if _sx2 == "F" else "registrado"
        return _btn_msg(
            f"\u00a1Listo, *{nombre}*! Ya quedaste {_flex_reg2} \U0001f64c\n\n"
            f"\u00bfConfirmas esta hora?\n\n"
            f"\U0001f464 *{paciente['nombre']}*\n"
            f"\U0001f3e5 *{slot['especialidad']}* \u2014 {slot['profesional']}\n"
            f"\U0001f4c5 *{slot['fecha_display']}*\n"
            f"\U0001f550 *{slot['hora_inicio'][:5]}*\n"
            f"\U0001f4b3 *{modalidad}*",
            [
                {"id": "si", "title": "\u2705 Confirmar"},
                {"id": "no", "title": "\u274c Cambiar"},
            ]
        )

    # ── HUMAN_TAKEOVER ────────────────────────────────────────────────────────
    # Principio: HUMAN_TAKEOVER es inviolable. Solo la recepcionista sale del
    # estado (botón "devolver al bot") o el paciente con un reset explícito
    # ("menu"/"hola"/"inicio" — ya manejado arriba como _es_comando_reset).
    #
    # Por qué: el auto-escape basado en intent detection contradecía a la
    # recepcionista y desinformaba al paciente (ej: bot respondía que el bono
    # Fonasa se compraba en CESFAM cuando la recepcionista estaba diciendo
    # lo contrario). Los supuestos "rescates automáticos" tenían más falsos
    # positivos que beneficios. Ahora el comportamiento es determinístico.
    if state == "HUMAN_TAKEOVER":
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

        # Si la recepcionista ya respondió alguna vez, NO repetir el ack —
        # el paciente sabe que está hablando con una persona. Repetir el
        # "Recibido 🙏" cada mensaje confunde y se mezcla con las respuestas
        # reales de la recepcionista (caso real 56975932459, 2026-04-23: 10
        # acks repetidos en una conversación activa).
        if msgs_sin_respuesta == 1 and not data.get("human_replied"):
            # Primer ack — el paciente sabe que una recepcionista vendra.
            return (
                "Recibido 🙏 Una recepcionista te responderá en este chat en breve.\n\n"
                f"_Si es urgente puedes llamar: 📞 *{CMC_TELEFONO}*_"
            )
        # Desde msg 2+ el bot queda SILENCIOSO. No spamear al paciente con
        # Seguimos atentos ni Recibido 🙏 repetidos — la recepcionista ya
        # esta respondiendo desde el panel y el ruido confunde. Cada 15
        # mensajes sin respuesta humana mandamos un recordatorio suave.
        if msgs_sin_respuesta > 0 and msgs_sin_respuesta % 15 == 0:
            return f"Seguimos aquí 🙌 Si es urgente, llama al 📞 *{CMC_TELEFONO}*"
        return ""

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
    "esp_trauma":  "medicina general",  # traumatología redirigida
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
                # Traumatología temporalmente deshabilitada (Dr. Barraza no disponible)
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

# Apellidos de profesionales específicos → key de ESPECIALIDADES_MAP (que resuelve a 1 ID).
# Usado como override cuando Claude clasifica genéricamente pero el texto crudo
# menciona a un doctor puntual (ej. "Con Olavarria" → narrow a solo ese).
_APELLIDOS_PROFESIONAL = [
    # Variaciones por profesional. Incluye: sin tilde, confusión b↔v,
    # j↔g↔x↔h, ll↔y, s↔z al final, errores de escritura rural.
    # `in` es substring — orden no importa demasiado salvo colisiones.
    # Mapean a keys que deben EXISTIR en ESPECIALIDADES_MAP de medilink.py.

    # ── Medicina General: 3 colegas (Olavarría 1, Abarca 73, Márquez 13) ──
    ("olavarr",      "olavarría"),     # olavarría, olavarria, olavarr
    ("olavari",      "olavarría"),     # olavarí
    ("abarca",       "abarca"),
    ("avarca",       "abarca"),        # b↔v
    ("abaca",        "abarca"),        # error común
    ("marquez",      "marquez"),       # antes "medicina familiar" — caía en _ESP_MED_GENERAL
    ("márquez",      "marquez"),
    ("marques",      "marquez"),       # s↔z
    ("márques",      "marquez"),

    # ── Odontología: 2 colegas (Burgos 55, Jiménez 72) ──
    ("burgos",       "burgos"),        # antes "odontología" — mezclaba con Jiménez
    ("vurgos",       "burgos"),        # b↔v
    ("burgo",        "burgos"),        # sin s
    ("jimenez",      "jimenez"),       # antes "odontología" — mezclaba con Burgos
    ("jiménez",      "jimenez"),
    ("ximenez",      "jimenez"),       # j↔x
    ("ximénez",      "jimenez"),
    ("gimenez",      "jimenez"),       # j↔g
    ("giménez",      "jimenez"),
    ("himenez",      "jimenez"),       # j↔h
    ("jimene",       "jimenez"),       # sin z

    # ── Psicología Adulto: 2 colegas (Montalba 74, Rodríguez 49) ──
    ("montalba",     "montalba"),      # antes "psicología" — mezclaba con Rodríguez
    ("montalva",     "montalba"),      # b↔v
    ("montalbo",     "montalba"),      # error terminación
    ("rodriguez",    "rodriguez"),     # NUEVO — no estaba listado
    ("rodríguez",    "rodriguez"),
    ("rodrigez",     "rodriguez"),     # sin ui
    ("rodrigues",    "rodriguez"),     # s↔z
    ("rodrígez",     "rodriguez"),
    ("juan pablo",   "rodriguez"),

    # ── Kinesiología: 2 colegas (Armijo 77, Etcheverry 21) ──
    ("armijo",       "armijo"),
    ("armiho",       "armijo"),        # j↔h
    ("armigo",       "armijo"),        # j↔g
    ("etcheverry",   "etcheverry"),
    ("echeverry",    "etcheverry"),    # sin t
    ("echeverri",    "etcheverry"),    # sin y final
    ("etcheveri",    "etcheverry"),
    ("echaverri",    "etcheverry"),    # e↔a

    # ── Profesionales únicos en su especialidad ──
    ("borrego",      "otorrinolaringología"),
    ("vorrego",      "otorrinolaringología"),  # b↔v
    ("borego",       "otorrinolaringología"),  # sin doble r

    ("millan",       "cardiología"),
    ("millán",       "cardiología"),
    ("milan",        "cardiología"),   # ll↔l
    ("milán",        "cardiología"),
    ("miyan",        "cardiología"),   # ll↔y

    ("rejon",        "ginecología"),
    ("rejón",        "ginecología"),
    ("rehon",        "ginecología"),   # j↔h
    ("regon",        "ginecología"),   # j↔g

    ("quijano",      "gastroenterología"),
    ("kijano",       "gastroenterología"),  # qu↔k
    ("quihano",      "gastroenterología"),  # j↔h

    ("castillo",     "ortodoncia"),
    ("castiyo",      "ortodoncia"),    # ll↔y
    ("castilo",      "ortodoncia"),    # sin doble l
    ("casiyo",       "ortodoncia"),

    ("fredes",       "endodoncia"),
    ("fredez",       "endodoncia"),    # s↔z
    ("frede",        "endodoncia"),    # sin s

    ("valdes",       "implantología"),
    ("valdés",       "implantología"),
    ("valdez",       "implantología"),
    ("baldes",       "implantología"), # b↔v
    ("baldés",       "implantología"),

    ("fuentealba",   "estética facial"),
    ("fuentealva",   "estética facial"),  # b↔v
    ("fuentesalba",  "estética facial"),  # error común
    # "valentina" removido — nombre común de pacientes genera falsos positivos

    ("acosta",       "masoterapia"),
    ("acostas",      "masoterapia"),   # s extra

    ("pinto",        "nutrición"),
    ("pintos",       "nutrición"),
    ("gisela",       "nutrición"),
    ("gise",         "nutrición"),

    ("arratia",      "fonoaudiología"),
    ("aratia",       "fonoaudiología"),  # sin doble r
    ("juana",        "fonoaudiología"),

    ("guevara",      "podología"),
    ("gevara",       "podología"),     # sin u
    ("guebara",      "podología"),     # b↔v
    ("andrea guevara", "podología"),

    ("pardo",        "ecografía"),
    ("pardos",       "ecografía"),
    ("david pardo",  "ecografía"),

    # Matrona (no estaba) — Sarai Gómez (67). "gómez" y "sarai" son únicos en el centro.
    ("sarai",        "matrona"),
    ("saraí",        "matrona"),
    ("sara gomez",   "matrona"),
    ("sarai gomez",  "matrona"),
    ("saraí gómez",  "matrona"),

    # ── COBERTURA EXHAUSTIVA: nombres, apellidos, nombre+apellido, apodos,
    # typos frecuentes (b/v, j/g/h/x, ll/y, z/s, letras omitidas o dobles).
    # El shortcut IDLE filtra "soy X / me llamo X" para evitar falsos positivos.

    # === Dr. Rodrigo Olavarría (1) — Medicina General ===
    ("rodrigo",      "olavarría"),
    ("rodri",        "olavarría"),
    ("rodriguito",   "olavarría"),
    ("drigo",        "olavarría"),
    ("olabarria",    "olavarría"),
    ("olabarría",    "olavarría"),
    ("olaverria",    "olavarría"),
    ("holavarria",   "olavarría"),
    ("rodrigo olavarria",   "olavarría"),
    ("rodrigo olavarría",   "olavarría"),
    ("rodri olavarria",     "olavarría"),
    ("dr olavarria",        "olavarría"),
    ("dr rodrigo",          "olavarría"),

    # === Dr. Andrés Abarca (73) — Medicina General ===
    ("andres",       "abarca"),
    ("andrés",       "abarca"),
    ("andy",         "abarca"),
    ("andre",        "abarca"),
    ("andresito",    "abarca"),
    ("abarka",       "abarca"),
    ("abalca",       "abarca"),
    ("abarcas",      "abarca"),
    ("andres abarca",    "abarca"),
    ("andrés abarca",    "abarca"),
    ("dr abarca",        "abarca"),
    ("dr andres",        "abarca"),

    # === Dr. Alonso Márquez (13) — Medicina General ===
    ("alonso",       "marquez"),
    ("alonzo",       "marquez"),
    ("markez",       "marquez"),
    ("markes",       "marquez"),
    ("marke",        "marquez"),
    ("alonso marquez",   "marquez"),
    ("alonso márquez",   "marquez"),
    ("dr marquez",       "marquez"),
    ("dr alonso",        "marquez"),

    # === Dr. Manuel Borrego (23) — Otorrinolaringología ===
    ("manuel",       "otorrinolaringología"),
    ("manu",         "otorrinolaringología"),
    ("manolo",       "otorrinolaringología"),
    ("manuelito",    "otorrinolaringología"),
    ("boregos",      "otorrinolaringología"),
    ("borregos",     "otorrinolaringología"),
    ("manuel borrego",   "otorrinolaringología"),
    ("dr borrego",       "otorrinolaringología"),
    ("dr manuel",        "otorrinolaringología"),

    # === Dr. Miguel Millán (60) — Cardiología ===
    ("miguel",       "cardiología"),
    ("migue",        "cardiología"),
    ("mike",         "cardiología"),
    ("miki",         "cardiología"),
    ("miguelito",    "cardiología"),
    ("milian",       "cardiología"),
    ("millian",      "cardiología"),
    ("miguel millan",    "cardiología"),
    ("miguel millán",    "cardiología"),
    ("dr millan",        "cardiología"),
    ("dr miguel",        "cardiología"),

    # === Dr. Claudio Barraza (64) — Traumatología ===
    ("claudio",      "traumatología"),
    ("clau",         "traumatología"),
    ("claudi",       "traumatología"),
    ("claudito",     "traumatología"),
    ("barraza",      "traumatología"),
    ("baraza",       "traumatología"),
    ("varraza",      "traumatología"),
    ("barras",       "traumatología"),
    ("barraz",       "traumatología"),
    ("claudio barraza",  "traumatología"),
    ("dr barraza",       "traumatología"),
    ("dr claudio",       "traumatología"),

    # === Dr. Tirso Rejón (61) — Ginecología ===
    ("tirso",        "ginecología"),
    ("tirzo",        "ginecología"),
    ("rexon",        "ginecología"),
    ("reyón",        "ginecología"),
    ("rejones",      "ginecología"),
    ("tirso rejon",      "ginecología"),
    ("tirso rejón",      "ginecología"),
    ("dr rejon",         "ginecología"),
    ("dr tirso",         "ginecología"),

    # === Dr. Nicolás Quijano (65) — Gastroenterología ===
    ("nicolas",      "gastroenterología"),
    ("nicolás",      "gastroenterología"),
    ("nico",         "gastroenterología"),
    ("nicolasito",   "gastroenterología"),
    ("quijan",       "gastroenterología"),
    ("quixano",      "gastroenterología"),
    ("qijano",       "gastroenterología"),
    ("kijanu",       "gastroenterología"),
    ("nicolas quijano",  "gastroenterología"),
    ("nicolás quijano",  "gastroenterología"),
    ("dr quijano",       "gastroenterología"),
    ("dr nicolas",       "gastroenterología"),

    # === Dra. Javiera Burgos (55) — Odontología General ===
    ("javiera",      "burgos"),
    ("xaviera",      "burgos"),
    ("haviera",      "burgos"),
    ("yaviera",      "burgos"),
    ("javi",         "burgos"),
    ("javy",         "burgos"),
    ("xavi",         "burgos"),
    ("jabiera",      "burgos"),
    ("javierita",    "burgos"),
    ("vurgo",        "burgos"),
    ("burgoss",      "burgos"),
    ("javiera burgos",   "burgos"),
    ("javi burgos",      "burgos"),
    ("dra burgos",       "burgos"),
    ("dra javiera",      "burgos"),
    ("doctora javiera",  "burgos"),

    # === Dr. Carlos Jiménez (72) — Odontología General ===
    ("carlos",       "jimenez"),
    ("carlitos",     "jimenez"),
    ("carli",        "jimenez"),
    ("carl",         "jimenez"),
    ("carlos jimenez",   "jimenez"),
    ("carlos jiménez",   "jimenez"),
    ("carlos ximenez",   "jimenez"),
    ("dr jimenez",       "jimenez"),
    ("dr carlos",        "jimenez"),

    # === Dra. Daniela Castillo (66) — Ortodoncia ===
    ("daniela",      "ortodoncia"),
    ("dani",         "ortodoncia"),
    ("danny",        "ortodoncia"),
    ("danielita",    "ortodoncia"),
    ("castilllo",    "ortodoncia"),
    ("catillo",      "ortodoncia"),
    ("daniela castillo", "ortodoncia"),
    ("dra castillo",     "ortodoncia"),
    ("dra daniela",      "ortodoncia"),
    ("doctora daniela",  "ortodoncia"),

    # === Dr. Fernando Fredes (75) — Endodoncia ===
    ("fernando",     "endodoncia"),
    ("fer",          "endodoncia"),
    ("nando",        "endodoncia"),
    ("fefe",         "endodoncia"),
    ("fercho",       "endodoncia"),
    ("fredesh",      "endodoncia"),
    ("fernando fredes",  "endodoncia"),
    ("dr fredes",        "endodoncia"),
    ("dr fernando",      "endodoncia"),

    # === Dra. Aurora Valdés (69) — Implantología ===
    ("aurora",       "implantología"),
    ("au",           "implantología"),
    ("aurorita",     "implantología"),
    ("valdeth",      "implantología"),
    ("baldesh",      "implantología"),
    ("aurora valdes",    "implantología"),
    ("aurora valdés",    "implantología"),
    ("dra valdes",       "implantología"),
    ("dra aurora",       "implantología"),

    # === Dra. Valentina Fuentealba (76) — Estética Facial ===
    # NOTA: "valentina"/"vale"/"valen"/"valenti" removidos — nombres comunes
    # de pacientes generaban FP ("para Valentina Medina", "vale bono").
    # Requieren contexto: "dra" o apellido "fuentealba".
    ("fuentealba",           "estética facial"),
    ("valentina fuentealba", "estética facial"),
    ("valen fuentealba",     "estética facial"),
    ("dra fuentealba",       "estética facial"),
    ("dra valentina",        "estética facial"),

    # === Paola Acosta (59) — Masoterapia ===
    ("paola",        "masoterapia"),
    ("pao",          "masoterapia"),
    ("pauli",        "masoterapia"),
    ("paolita",      "masoterapia"),
    ("agosta",       "masoterapia"),
    ("acustai",      "masoterapia"),
    ("paola acosta",     "masoterapia"),

    # === Luis Armijo (77) — Kinesiología ===
    ("luis",         "armijo"),
    ("lucho",        "armijo"),
    ("luisito",      "armijo"),
    ("luigi",        "armijo"),
    ("armijos",      "armijo"),
    ("luis armijo",      "armijo"),
    ("kine luis",        "armijo"),
    ("don luis",         "armijo"),

    # === Leonardo Etcheverry (21) — Kinesiología ===
    ("leonardo",     "etcheverry"),
    ("leo",          "etcheverry"),
    ("leonel",       "etcheverry"),
    ("leito",        "etcheverry"),
    ("etcheberry",   "etcheverry"),
    ("echeberry",    "etcheverry"),
    ("etchevery",    "etcheverry"),
    ("leonardo etcheverry", "etcheverry"),
    ("kine leonardo",    "etcheverry"),

    # === Gisela Pinto (52) — Nutrición ===
    ("gisel",        "nutrición"),
    ("gisela pinto",     "nutrición"),
    ("pintos",           "nutrición"),
    ("jisela",           "nutrición"),
    ("hisela",           "nutrición"),
    ("nutricionista",    "nutrición"),

    # === Jorge Montalba (74) — Psicología ===
    # "jorge" solo removido — nombre común de paciente (caso 56994855278: Jorge Pezo)
    ("jorgito",      "montalba"),
    ("coque",        "montalba"),
    ("horge",        "montalba"),
    ("gorge",        "montalba"),
    ("montalva",     "montalba"),
    ("jorge montalba",   "montalba"),
    ("jorge montalva",   "montalba"),
    ("dr montalba",      "montalba"),
    ("dr jorge",         "montalba"),

    # === Dr. Juan Pablo Rodríguez (49) — Psicología ===
    ("juan pablo rodriguez", "rodriguez"),
    ("juanpa",       "rodriguez"),
    ("juampa",       "rodriguez"),
    ("jp rodriguez", "rodriguez"),
    ("dr rodriguez",     "rodriguez"),
    ("dr juan pablo",    "rodriguez"),

    # === Juana Arratia (70) — Fonoaudiología ===
    ("juani",        "fonoaudiología"),
    ("juanita",      "fonoaudiología"),
    ("juanis",       "fonoaudiología"),
    ("xuana",        "fonoaudiología"),
    ("huana",        "fonoaudiología"),
    ("juana arratia",    "fonoaudiología"),
    ("fono juana",       "fonoaudiología"),

    # === Sarai Gómez (67) — Matrona ===
    ("gomez",        "matrona"),
    ("gómez",        "matrona"),
    ("gomes",        "matrona"),
    ("sarah",        "matrona"),
    ("matrona sarai",    "matrona"),

    # === Andrea Guevara (56) — Podología ===
    ("andrea",       "podología"),
    ("andi",         "podología"),
    ("andre guevara",    "podología"),
    ("andreita",     "podología"),
    ("gebaras",      "podología"),
    ("guevara andrea",   "podología"),

    # === Dr. David Pardo (68) — Ecografía ===
    ("david",        "ecografía"),
    ("dave",         "ecografía"),
    ("pardu",        "ecografía"),
    ("dr pardo",         "ecografía"),
    ("dr david",         "ecografía"),

    # ── Apellidos/nombres INCOMPLETOS (cuando el paciente no está seguro
    # de la ortografía y escribe solo el prefijo). Longitud mínima 4-5 letras
    # para evitar falsos positivos. Prefijos 3 letras serían muy ambiguos.
    # Evitados: rodr/pard/gom/vald (demasiado cortos o ambiguos).

    # Medicina General
    ("olava",        "olavarría"),   # "dr olava", "olava" → ya casi completo
    ("olavar",       "olavarría"),
    ("olabar",       "olavarría"),
    ("olaber",       "olavarría"),
    ("abarc",        "abarca"),      # "abarc" sin a final
    ("avarc",        "abarca"),
    ("abar",         "abarca"),      # suficientemente único
    ("marq",         "marquez"),     # "marq", "márq"
    ("márq",         "marquez"),

    # ORL
    ("borre",        "otorrinolaringología"),
    ("borr",         "otorrinolaringología"),   # cuidado con "borrico" pero raro
    ("vorre",        "otorrinolaringología"),

    # Cardiología
    ("milla",        "cardiología"),
    ("milán",        "cardiología"),  # ya está variantes, refuerzo
    ("mille",        "cardiología"),

    # Traumatología
    ("barra",        "traumatología"),
    ("baras",        "traumatología"),
    ("barrasa",      "traumatología"),

    # Ginecología
    ("rejo",         "ginecología"),  # "rejo" únicamente, "rejon" ya está
    ("reho",         "ginecología"),
    ("rego",         "ginecología"),

    # Gastroenterología
    ("quija",        "gastroenterología"),
    ("kija",         "gastroenterología"),
    ("quihan",       "gastroenterología"),

    # Odontología Burgos
    ("burgo",        "burgos"),      # ya estaba pero refuerzo

    # Odontología Jiménez
    ("jime",         "jimenez"),
    ("jimen",        "jimenez"),
    ("xime",         "jimenez"),
    ("gime",         "jimenez"),

    # Ortodoncia Castillo
    ("castil",       "ortodoncia"),
    ("casti",        "ortodoncia"),
    ("castiy",       "ortodoncia"),

    # Endodoncia Fredes
    ("frede",        "endodoncia"),
    ("fredec",       "endodoncia"),

    # Implantología Valdés
    ("valde",        "implantología"),
    ("balde",        "implantología"),

    # Estética Fuentealba
    ("fuente",       "estética facial"),
    ("fuentea",      "estética facial"),
    ("fuentes",      "estética facial"),

    # Masoterapia Acosta
    ("acost",        "masoterapia"),
    ("agost",        "masoterapia"),

    # Kinesiología Armijo
    ("armi",         "armijo"),
    ("armih",        "armijo"),

    # Kinesiología Etcheverry
    ("etche",        "etcheverry"),
    ("eche",         "etcheverry"),
    ("echeb",        "etcheverry"),
    ("etcheb",       "etcheverry"),

    # Psicología Montalba
    ("montal",       "montalba"),
    ("montalv",      "montalba"),

    # Psicología Rodríguez (solo con apellido completo — "rodri" ambiguo)
    ("rodriguezz",   "rodriguez"),

    # Fonoaudiología Arratia
    ("arrat",        "fonoaudiología"),
    ("arati",        "fonoaudiología"),

    # Podología Guevara
    ("gueva",        "podología"),
    ("gueb",         "podología"),
    ("geva",         "podología"),

    # Matrona Sarai Gómez (gomez ya está como "gomez")
    ("sarahi",       "matrona"),

    # Nutrición Pinto (cuidado: "pinto" es verbo. Lo dejo con pinto entero.)
    # "pint" sería demasiado riesgoso (matchea "pinto", "pinta", "pintar")
]


def _normalizar_para_apellido(txt: str) -> str:
    """Normaliza texto libre para detección robusta de apellidos.
    Objetivo: que "M4rquez", "márq_uez", "Márquez 😊", "el dr. M A R Q U E Z"
    todos colapsen al mismo string base donde buscar 'marquez' como substring.

    Pasos:
    1. Unicode NFKC (fullwidth → ASCII).
    2. Quita chars invisibles (ZWSP, ZWJ, BOM).
    3. Lowercase.
    4. Quita tildes (NFD + drop combining).
    5. Elimina TODO lo que no sea letra a-z/ñ — espacios, dígitos, emojis,
       underscores, puntuación, símbolos. Queda una sola tira de letras.
    """
    if not txt:
        return ""
    import unicodedata
    t = unicodedata.normalize("NFKC", txt)
    t = re.sub(r"[\u200b-\u200f\u2060\ufeff]", "", t)
    t = t.lower()
    t = unicodedata.normalize("NFD", t)
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r"[^a-zñ]+", "", t)
    return t


# Precomputar apellidos normalizados una sola vez (optimización)
_APELLIDOS_NORM = [(re.sub(r"[^a-zñ]+", "", a.lower()), key) for a, key in _APELLIDOS_PROFESIONAL]
# Filtra aliases < 2 chars o conocidos como problemáticos (matchean dentro de
# palabras comunes sin aportar valor porque hay variantes largas).
# Casos reales observados en producción 2026-04-21/22:
#   "au"   → traumatólogo, paula, autos → falso positivo implantología
#   "vale" → "vale el bono", "vale la pena" → falso positivo estética facial
#   "pao"  → "por", "pao-r", "sapao" → falso positivo masoterapia
#   "fer"  → "conferencia", "preferir", "feria", "oferta" → FP endodoncia
#   "armi" → "ecotomografia mamaria" tiene "mamari" pero no es el caso;
#            revisar si existe, quitarlo si sí
_APELLIDOS_BLACKLIST = {"au", "vale", "pao", "fer", "armi"}
_APELLIDOS_NORM = [(a, k) for (a, k) in _APELLIDOS_NORM if a not in _APELLIDOS_BLACKLIST]


def _normalizar_para_apellido_ws(txt: str) -> str:
    """Como _normalizar_para_apellido pero PRESERVA espacios para permitir
    matching con word boundary en aliases cortos."""
    if not txt:
        return ""
    import unicodedata
    t = unicodedata.normalize("NFKC", txt)
    t = re.sub(r"[\u200b-\u200f\u2060\ufeff]", "", t)
    t = t.lower()
    t = unicodedata.normalize("NFD", t)
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r"[^a-zñ0-9\s]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


# Especialidades que el CMC NO atiende — si el texto las menciona, NO matchear
# apellidos (evita que aliases cortos tipo "au" de implantología colisionen
# dentro de palabras como "traumatólogo").
_ESPECIALIDADES_NO_DISPONIBLES_NORM = {
    "traumatolog", "traumatologo", "traumatologa", "traumatologia",
    "pediatra", "pediatria",
    "dermatolog", "dermatologo", "dermatologa", "dermatologia",
    "urologo", "urologa", "urologia",
    "oftalmolog", "oftalmologo", "oftalmologa", "oftalmologia",
    "neurolog", "neurologo", "neurologa", "neurologia",
    "psiquiatra",
    "reumatolog", "reumatologo", "reumatologa", "reumatologia",
}


# Keys de _APELLIDOS_NORM que son APELLIDOS INDIVIDUALES (no especialidades).
# Cuando Claude Haiku retorna una de estas como "especialidad" pero el texto
# del paciente NO menciona el apellido, es alucinación → descartar y caer al
# detector local de especialidad. Caso real 2026-04-28 (56993584481).
_APELLIDOS_INDIVIDUALES_KEYS = frozenset({
    "abarca", "armijo", "burgos", "etcheverry", "jimenez", "marquez",
    "montalba", "olavarría", "olavarria", "rodriguez",
})


def _detectar_apellido_profesional(txt: str) -> str | None:
    """Si el texto menciona un apellido de profesional, devuelve la key de
    ESPECIALIDADES_MAP correspondiente. Normaliza el input para tolerar
    underscores, emojis, dígitos insertados, tildes, fullwidth, etc.

    Reglas de matching:
    - Hard-block: si el texto menciona especialidad NO disponible (traumatólogo,
      pediatra, etc.), no matchear apellidos.
    - Aliases >=5 chars: substring match en versión colapsada (tolera "M4rquez",
      "márq_uez", etc.).
    - Aliases <5 chars: word-boundary regex en versión con espacios (evita
      "vale" matchando en "vale el bono", "pao" en "por", etc.).
    """
    if not txt:
        return None
    norm_collapsed = _normalizar_para_apellido(txt)
    norm_ws = _normalizar_para_apellido_ws(txt)
    if not norm_collapsed:
        return None
    # Hard-block de especialidades no disponibles
    for esp_no in _ESPECIALIDADES_NO_DISPONIBLES_NORM:
        if esp_no in norm_collapsed:
            return None
    for apellido_norm, key in _APELLIDOS_NORM:
        if not apellido_norm:
            continue
        if len(apellido_norm) >= 5:
            if apellido_norm in norm_collapsed:
                return key
        else:
            # Alias corto — exigir word-boundary
            if re.search(r"\b" + re.escape(apellido_norm) + r"\b", norm_ws):
                return key
    # ── Fuzzy fallback (typos no en diccionario): "cabalga" → "carballo",
    # "olavaria" → "olavarria", "abracas" → "abarca", etc. Solo aplica cuando
    # el paciente menciona explícitamente "doctor"/"dra"/"dr." para evitar
    # que palabras al azar matcheen apellidos similares por casualidad.
    _MENCIONA_PROF = any(p in norm_ws for p in (
        "doctor", "doctora", "dr ", "dra ", "medico", "medica",
        "kinesiologo", "kinesiologa", "kinesiolog",
        "psicologo", "psicologa", "psicolog",
        "dentista", "odontologo", "odontologa",
    ))
    if _MENCIONA_PROF:
        from difflib import SequenceMatcher
        # Tomar tokens del input >=5 chars (eviar matches espurios con "tiene",
        # "para", "como", etc.) y comparar contra apellidos >=7 chars (los
        # aliases cortos como "jimene" generaban falsos positivos: "tiene" vs
        # "jimene" daba 0.727. Caso real 2026-04-28 56993584481).
        _PALABRAS_COMUNES = frozenset({
            "tiene", "tienes", "tengo", "tengas", "tener",
            "hora", "horas", "horita", "horario",
            "para", "como", "donde", "cuando", "cuanto",
            "sera", "será", "serán", "seran",
            "necesito", "quisiera", "quiero", "deseo",
            "puedo", "puede", "puedes",
            "manana", "mañana", "tarde", "noche",
            "hoy", "ayer",
            "medico", "medica", "doctor", "doctora",
            "dental", "dentista", "kine",
            "con", "del", "esa", "ese", "esto", "esta",
            "entonces", "tambien",
        })
        _tokens = [
            t for t in norm_ws.split()
            if len(t) >= 5 and t not in _PALABRAS_COMUNES
        ]
        _best: tuple[float, str | None] = (0.0, None)
        for tok in _tokens:
            for apellido_norm, key in _APELLIDOS_NORM:
                if len(apellido_norm) < 7:
                    continue
                ratio = SequenceMatcher(None, tok, apellido_norm).ratio()
                if ratio > _best[0]:
                    _best = (ratio, key)
        if _best[0] >= 0.85:  # 85% — muy estricto, evita falsos positivos
            return _best[1]
    return None


# Frases comunes → key de ESPECIALIDADES_MAP. Cuando Claude no detecta la
# especialidad pero el texto claramente la menciona (ej. "médico familiar"
# en intent disponibilidad), este detector sirve de fallback.
_FRASES_ESPECIALIDAD = [
    ("médico familiar",       "medicina familiar"),
    ("medico familiar",       "medicina familiar"),
    ("medicina familiar",     "medicina familiar"),
    ("medicina general",      "medicina general"),
    ("médico general",        "medicina general"),
    ("medico general",        "medicina general"),
    # "médico" / "medico" aislado → medicina general (convención rural)
    ("con medico",            "medicina general"),
    ("con médico",            "medicina general"),
    ("un medico",             "medicina general"),
    ("un médico",             "medicina general"),
    ("al medico",             "medicina general"),
    ("al médico",             "medicina general"),
    ("del medico",            "medicina general"),
    ("del médico",            "medicina general"),
    ("para el medico",        "medicina general"),
    ("para el médico",        "medicina general"),
    ("kinesiolog",            "kinesiología"),
    ("kine",                  "kinesiología"),
    ("dentista",              "odontología"),
    ("odontolog",             "odontología"),
    ("odontoloj",             "odontología"),
    ("endodoncia",            "endodoncia"),
    ("endodoncis",            "endodoncia"),
    ("conducto",              "endodoncia"),
    ("ortodoncia",            "ortodoncia"),
    ("brackets",              "ortodoncia"),
    ("frenillos",             "ortodoncia"),
    ("implant",               "implantología"),
    ("masoterapia",           "masoterapia"),
    ("masaje",                "masoterapia"),
    ("otorrino",              "otorrinolaringología"),
    ("orl",                   "otorrinolaringología"),
    ("cardiolog",             "cardiología"),
    ("gastro",                "gastroenterología"),
    ("ginecolog",             "ginecología"),
    ("matrona",               "matrona"),
    ("fonoaudiolog",          "fonoaudiología"),
    ("fono",                  "fonoaudiología"),
    ("psicolog",              "psicología"),
    ("nutricion",             "nutrición"),
    ("nutrición",             "nutrición"),
    ("podolog",               "podología"),
    ("ecograf",               "ecografía"),
    ("ecotomograf",           "ecografía"),
    ("ecotomo",               "ecografía"),
    ("eco abdom",             "ecografía"),
    ("eco tiroid",            "ecografía"),
    ("eco mama",              "ecografía"),
    ("eco testi",             "ecografía"),
    ("testicul",              "ecografía"),
    ("texticul",              "ecografía"),
    ("inguino escrotal",      "ecografía"),
    ("inguinal escrotal",     "ecografía"),
    ("estetica",              "estética facial"),
    ("estética",              "estética facial"),
    ("botox",                 "estética facial"),
    ("traumato",              "traumatología"),
]


def _detectar_especialidad_en_texto(txt: str) -> str | None:
    """Detecta una especialidad mencionada en el texto crudo. Usado como
    fallback cuando Claude no extrae especialidad correctamente.

    Primero intenta match exacto. Si falla, normaliza typos fonéticos comunes
    (j→g, y→ll, sh→ch, sin tildes) y reintenta."""
    if not txt:
        return None
    tl = txt.lower()
    for frase, key in _FRASES_ESPECIALIDAD:
        if frase in tl:
            return key
    # Fuzzy pass: normalizar typos fonéticos y ortográficos comunes en chile rural
    tl_fuzzy = tl
    _FIXES = [
        # Typos verbales comunes
        ("biene", "viene"), ("bienen", "vienen"), ("bamos", "vamos"),
        ("horits", "horas"), ("orita", "hora"), ("oritas", "horas"),
        ("pars", "para"), ("hpra", "hora"), ("hoy dia", "hoy"),
        # Typos fonéticos
        ("jeneral", "general"), ("jeberal", "general"), ("geberal", "general"),
        ("jinecologia", "ginecologia"), ("jenital", "genital"),
        ("endodonsia", "endodoncia"), ("ortodonsia", "ortodoncia"),
        ("dentizta", "dentista"), ("odontoloja", "odontologia"),
        ("kinesiologo", "kinesiologia"), ("kinesiolog", "kinesiologia"),
        ("cirujano dentista", "dentista"),
        ("psicologa", "psicologia"), ("psicologo", "psicologia"),
        ("nutricionista", "nutricion"),
        ("matron ", "matrona "), ("matron?", "matrona"),
        ("cardiologo", "cardiologia"),
    ]
    for wrong, right in _FIXES:
        if wrong in tl_fuzzy:
            tl_fuzzy = tl_fuzzy.replace(wrong, right)
    if tl_fuzzy != tl:
        for frase, key in _FRASES_ESPECIALIDAD:
            if frase in tl_fuzzy:
                return key
    return None


_ESPECIALIDADES_TEXTO = (
    "• Medicina General\n"
    "• Medicina Familiar\n"
    "• Otorrinolaringología\n"
    "• Cardiología\n"
    # "• Traumatología\n"  # temporalmente deshabilitada
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
    """Expande horarios de Medicina General.
    Stage 0→1: muestra slots del doctor sugerido (ya cargados).
    Stage 1→2: muestra los 3 (Abarca + Olavarría + Márquez) con todos los
               horarios del día. Antes requería 2 pasos (Abarca+Olavarría,
               después +Márquez); colapsado para reducir fricción."""
    next_stage = stage + 1

    if next_stage == 1:
        # Mostrar los slots del doctor sugerido (ya guardados en data["slots"])
        data["expansion_stage"] = 1
        save_session(phone, "WAIT_SLOT", data)
        return _format_slots(data["slots"])

    # next_stage >= 2: mostrar los 3 profesionales de MG agrupados.
    # NO hacer fallback a buscar_primer_dia para profs sin horario ese día —
    # evita mostrar slots de otro día bajo el header de fecha actual.
    all_groups = []
    todos_all = []
    for pid in _MED_GENERAL_IDS:
        _, slots_pid = (await buscar_slots_dia_por_ids([pid], fecha)) if fecha else ([], [])
        if slots_pid:
            all_groups.append({"slots": slots_pid})
            todos_all.extend(slots_pid)

    data["expansion_stage"] = 2
    data["slots"] = todos_all
    data["todos_slots"] = todos_all
    save_session(phone, "WAIT_SLOT", data)

    return _format_slots_expansion(all_groups) if all_groups else "No hay más horarios disponibles."


# Tracking en memoria de cuándo se le mostró "modo_degradado" a cada phone
# para no repetir el mensaje una y otra vez durante una caída larga.
_MODO_DEGRADADO_AVISADO: dict[str, float] = {}
_MODO_DEGRADADO_TTL_SEG = 15 * 60  # 15 min


def _modo_degradado(phone: str, intent: str, state_snap: str = "") -> str:
    """Respuesta cuando Medilink está caído. Encola la intención y avisa al paciente.
    Devuelve un mensaje graceful que el bot enviará por WhatsApp.

    Si ya se avisó en los últimos 15 min, pasa a HUMAN_TAKEOVER en vez de
    repetir el mismo mensaje (el paciente ya sabe que hay problema técnico).
    """
    import time as _time_deg
    enqueue_intent(phone, intent, state_snap)
    log_event(phone, "modo_degradado", {"intent": intent})

    ahora = _time_deg.time()
    last_aviso = _MODO_DEGRADADO_AVISADO.get(phone, 0.0)
    if ahora - last_aviso < _MODO_DEGRADADO_TTL_SEG:
        # Ya le avisamos recientemente — pasar a humano directo
        save_session(phone, "HUMAN_TAKEOVER", {})
        log_event(phone, "modo_degradado_takeover", {"intent": intent})
        return (
            "Una recepcionista va a ayudarte directamente por acá 🙏\n\n"
            "_El sistema automático sigue en pausa, pero ya lo están revisando._"
        )

    _MODO_DEGRADADO_AVISADO[phone] = ahora
    reset_session(phone)
    return (
        "Nuestro sistema de citas está con un problema técnico en este momento 😕\n\n"
        "Guardé tu mensaje y te avisaré apenas vuelva a estar operativo. "
        "Mientras tanto puedes llamarnos:\n"
        f"📞 *{CMC_TELEFONO}*\n"
        f"☎️ *{CMC_TELEFONO_FIJO}*\n\n"
        "_Gracias por tu paciencia._"
    )


def _normalizar_slot_especialidad(slots: list, esp_solicitada: str) -> None:
    """Override in-place del campo slot[\"especialidad\"] para que refleje
    la especialidad SOLICITADA por el paciente cuando difiera de la registrada
    en PROFESIONALES.

    Caso real 2026-04-23: Jorge Montalba (74) esta registrado como
    \"Psicologia Adulto\". Un paciente pide \"psicologia infantil\" y el slot
    card mostraba \"Psicologia Adulto\". Confuso.

    Reglas actuales:
    - \"psicologia infantil\" (o \"psicologo infantil\") -> override a
      \"Psicologia Infantil\".
    Agregar nuevos casos aqui si aparecen.
    """
    if not slots or not esp_solicitada:
        return
    esp_low = esp_solicitada.lower().strip()
    # Mapear esp_low -> label de display
    overrides = {
        "psicologia infantil": "Psicologia Infantil",
        "psicología infantil": "Psicología Infantil",
        "psicologo infantil": "Psicología Infantil",
        "psicólogo infantil": "Psicología Infantil",
    }
    label = overrides.get(esp_low)
    if not label:
        return
    for s in slots:
        if isinstance(s, dict) and s.get("especialidad", "").lower().startswith("psicolog"):
            s["especialidad"] = label


async def _iniciar_agendar(phone: str, data: dict, especialidad: str | None,
                            saludo_prefix: str | None = None) -> str:
    if is_medilink_down():
        return _modo_degradado(phone, "agendar", especialidad or "")
    if not especialidad:
        save_session(phone, "WAIT_ESPECIALIDAD", data)
        return f"Claro, te ayudo a agendar 😊\n\n¿Qué especialidad necesitas?\n\n{_ESPECIALIDADES_TEXTO}"
    especialidad_lower = especialidad.lower().strip()
    # ── "general" / "generica" solas → medicina general ──
    # Caso 2026-04-23 (56968396554): paciente en WAIT_ESPECIALIDAD respondió
    # "General" (nombre de la especialidad); sin alias, Claude la mandaba
    # a off-topic. Normalizamos.
    if especialidad_lower in ("general", "generica", "genérica", "general medica",
                              "mg", "m.g.", "m.g", "medico", "médico"):
        especialidad = "medicina general"
        especialidad_lower = "medicina general"
    # ── Traumatología: derivada a medicina general (Barraza temporalmente no
    # disponible). Avisar al paciente antes de mostrar slots para que no se
    # confunda viendo Dr. Abarca cuando pidió traumatólogo.
    # Caso real 2026-04-23: 56954490708, 56951933878, 56941520432 — pacientes
    # preguntaban por traumatólogo y el bot ofrecía MG sin explicar el cambio.
    if especialidad_lower in ("traumatología", "traumatologia", "traumatólogo", "traumatologo"):
        if not saludo_prefix:
            saludo_prefix = (
                "Actualmente nuestro *traumatólogo* no está disponible 😔\n"
                "Te ofrezco *Medicina General* — puede evaluar tu caso y "
                "derivar a kinesiología o solicitar imágenes si corresponde.\n\n"
            )
        especialidad = "medicina general"
        especialidad_lower = "medicina general"
    # Detectar si la especialidad no existe en nuestro catálogo
    from medilink import _ids_para_especialidad as _ids_esp_check
    if not _ids_esp_check(especialidad_lower):
        # Sanity check: si la "especialidad" no parece serlo (solo signos, saludos,
        # agradecimientos, muy corta) NO decir "no contamos con *X*" — mostrar el
        # menú. Esto evita responses absurdas como "no contamos con *?*" o
        # "no contamos con *muchas gracias*".
        _esp_clean = re.sub(r"[^a-záéíóúñü ]", "", especialidad_lower).strip()
        _SALUDOS_GRACIAS = {
            "hola", "hi", "buenos dias", "buenas tardes", "buenas noches",
            "gracias", "muchas gracias", "graxias", "grcias", "ok", "oki", "vale",
            "perfecto", "perfect", "listo", "dale", "si", "no",
        }
        # Tokens que indican saludo o intención de pedir hora (no son especialidad)
        _SALUDOS_TOKENS = {"hola", "buenos", "buenas", "saludos", "hi", "hey", "ola"}
        _AGEND_TOKENS = {"pedir", "agendar", "reservar", "quiero", "necesito"}
        _palabras = set(_esp_clean.split())
        _es_saludo_compuesto = bool(_palabras & _SALUDOS_TOKENS)
        _es_intencion_agendar = bool(_palabras & _AGEND_TOKENS) and "hora" in _palabras
        if (len(_esp_clean) < 4 or _esp_clean in _SALUDOS_GRACIAS or not _esp_clean
                or _es_saludo_compuesto or _es_intencion_agendar):
            save_session(phone, "WAIT_ESPECIALIDAD", data)
            return f"Claro, te ayudo a agendar 😊\n\n¿Qué especialidad necesitas?\n\n{_ESPECIALIDADES_TEXTO}"
        # Antes de decir no contamos con, probar FAQ local (radiografia/telemed/etc)
        try:
            from claude_helper import _local_faq_fallback as _faq_fb_esp
            _faq_resp = _faq_fb_esp(especialidad)
            if _faq_resp:
                save_demanda_no_disponible(phone, especialidad, "especialidad")
                log_event(phone, "demanda_no_disponible_faq", {"solicitud": especialidad})
                reset_session(phone)
                return _faq_resp
        except Exception:
            pass
        # Especialidad plausible pero que no tenemos → registrar demanda
        save_demanda_no_disponible(phone, especialidad, "especialidad")
        log_event(phone, "demanda_no_disponible", {"solicitud": especialidad, "tipo": "especialidad"})
        reset_session(phone)
        return (
            f"En el CMC no contamos con *{especialidad}* por el momento 😔\n\n"
            f"🩺 Si quieres orientación, puedes agendar con nuestra *Medicina General* — el médico te evalúa y deriva si corresponde.\n\n"
            f"🏥 *Otras opciones:*\n\n"
            f"*Atención pública:* tu CESFAM te puede derivar al especialista en la red SSC (Hospital de Curanilahue, Cañete, Las Higueras o Regional de Concepción).\n\n"
            f"*Atención privada en Concepción:* las clínicas con mayor cobertura son Clínica Universitaria, Sanatorio Alemán, Andes Salud y RedSalud Mayor.\n\n"
            f"📞 Recepción: *{CMC_TELEFONO}*\n\n"
            "_Escribe *menu* para ver opciones._"
        )
    # Ortodoncia requiere evaluación previa con odontología general.
    # La dentista evalúa, pide radiografías y gestiona la derivación.
    if especialidad_lower in ("ortodoncia", "ortodoncista", "brackets", "frenillos"):
        log_event(phone, "ortodoncia_redirigida_odonto", {"especialidad_original": especialidad})
        # Redirigir a odontología general con el mensaje del flujo real
        data["ortodoncia_redirigida"] = True
        return await _iniciar_agendar(
            phone, data, "odontología",
            saludo_prefix=(
                "🦷 *¡Buena decisión!*\n\n"
                "Para ortodoncia, el primer paso es una evaluación con nuestra "
                "*dentista general*.\n"
                "Ella evalúa tu caso, solicita radiografías, toma fotografías "
                "y gestiona la derivación con la ortodoncista.\n\n"
                "💰 Presupuesto dental: *$15.000* (gratis si decides empezar "
                "tratamiento previo ese día).\n\n"
            ),
        )

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
    # Si paciente dijo "para hoy"/"para mañana" en IDLE, propagar a fecha_preferida
    # para que el branch correspondiente respete la fecha pedida.
    if data.get("fecha_pedida_idle") and not data.get("fecha_preferida"):
        data["fecha_preferida"] = data.pop("fecha_pedida_idle")

    # Medicina general: stage 0 = slot más próximo entre Abarca (08-16) y Olavarría (16-21).
    # Márquez (15-20) solo aparece como overflow si Abarca+Olavarría no tienen cupo.
    if especialidad_lower in _ESP_MED_GENERAL:
        _fp_mg = data.get("fecha_preferida")
        if _fp_mg:
            # Paciente pidió fecha específica — buscar solo ese día primero
            smart, todos = await buscar_slots_dia(especialidad_lower, _fp_mg)
            todos = [s for s in (todos or []) if s.get("fecha") == _fp_mg and s.get("id_profesional") in _MED_AO_IDS]
            smart = [s for s in (smart or []) if s.get("fecha") == _fp_mg and s.get("id_profesional") in _MED_AO_IDS]
            if todos:
                mejor = todos[0]
            else:
                # Sin disponibilidad ese día — marcar para disclaimer y caer al siguiente
                data["_aviso_sin_fecha_pedida"] = _fp_mg
                data.pop("fecha_preferida", None)
                smart, todos = await buscar_primer_dia(especialidad_lower, solo_ids=_MED_AO_IDS)
                if todos:
                    mejor = todos[0]
                else:
                    smart, todos = await buscar_primer_dia(especialidad_lower, solo_ids=[_MED_OVERFLOW_ID])
                    mejor = todos[0] if todos else None
        else:
            smart, todos = await buscar_primer_dia(especialidad_lower, solo_ids=_MED_AO_IDS)
            if todos:
                mejor = todos[0]  # más próximo entre ambos doctores
            else:
                # Abarca + Olavarría sin disponibilidad → Márquez como overflow
                smart, todos = await buscar_primer_dia(especialidad_lower, solo_ids=[_MED_OVERFLOW_ID])
                mejor = todos[0] if todos else None
    else:
        # Si el paciente indicó una fecha preferida ("mañana", "viernes", etc.),
        # buscarla directamente en vez de usar primer_dia.
        _fecha_pref = data.get("fecha_preferida")
        if _fecha_pref:
            smart, todos = await buscar_slots_dia(especialidad_lower, _fecha_pref)
            # Filtrar estrictamente a la fecha pedida (Medilink puede devolver vecinas)
            todos_dia_pref = [s for s in (todos or []) if s.get("fecha") == _fecha_pref]
            if todos_dia_pref:
                smart = [s for s in (smart or []) if s.get("fecha") == _fecha_pref] or todos_dia_pref[:5]
                todos = todos_dia_pref
            else:
                # Sin cupo ese día específico — marcar para disclaimer y caer al siguiente
                data["_aviso_sin_fecha_pedida"] = _fecha_pref
                smart, todos = await buscar_primer_dia(especialidad_lower)
            data.pop("fecha_preferida", None)
        else:
            smart, todos = await buscar_primer_dia(especialidad_lower)
        mejor = smart[0] if smart else (todos[0] if todos else None)

    # Normaliza display de especialidad (ej: Psicologia Infantil vs Adulto)
    _normalizar_slot_especialidad(smart, especialidad_lower)
    _normalizar_slot_especialidad(todos, especialidad_lower)
    if mejor:
        _normalizar_slot_especialidad([mejor], especialidad_lower)

    if not todos or not mejor:
        log_event(phone, "sin_disponibilidad", {"especialidad": (especialidad or "").strip().lower()})
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
    nombre_corto = _first_name(nombre_conocido) if nombre_conocido else ""
    # Si viene con saludo_prefix (ej. desde un motivo del menú), el prefix
    # actúa como header y se omite el "¡Hola de nuevo!" para no duplicar saludos.
    if saludo_prefix:
        header = saludo_prefix
    else:
        header = f"¡Hola de nuevo, *{nombre_corto}*! " if nombre_corto else ""
    # Disclaimer cuando el paciente pidió fecha específica y no había slots ese
    # día — antes el bot mostraba el siguiente disponible sin avisar.
    _fecha_avisar = data.pop("_aviso_sin_fecha_pedida", None)
    if _fecha_avisar:
        try:
            _d_av = datetime.strptime(_fecha_avisar, "%Y-%m-%d")
            _DIAS_AV = ["lunes","martes","miércoles","jueves","viernes","sábado","domingo"]
            _MESES_AV = ["enero","febrero","marzo","abril","mayo","junio","julio","agosto","septiembre","octubre","noviembre","diciembre"]
            _lbl_av = f"{_DIAS_AV[_d_av.weekday()]} {_d_av.day} de {_MESES_AV[_d_av.month - 1]}"
        except Exception:
            _lbl_av = _fecha_avisar
        header = f"⚠️ No tengo horarios para *{_lbl_av}* 😕\nTe muestro la *próxima disponible*:\n\n" + header
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
    # Señal de escasez cuando quedan pocas horas
    n_slots = len(todos)
    escasez = ""
    if n_slots <= 2:
        escasez = "⚡ _Última hora disponible_\n"
    elif n_slots <= 4:
        escasez = f"⚡ _Quedan solo {n_slots} horas_\n"
    return _btn_msg(
        f"{header}Te encontré hora ✨\n\n"
        f"🏥 *{mejor['especialidad']}* — {mejor['profesional']}\n"
        f"📅 *{mejor['fecha_display']}*\n"
        f"🕐 *{mejor['hora_inicio'][:5]}* ⭐\n"
        f"{precio_bloque}"
        f"{escasez}\n"
        "¿Te la reservo?",
        botones
    )


async def _iniciar_cancelar(phone: str, data: dict, txt: str = "") -> str:
    if is_medilink_down():
        return _modo_degradado(phone, "cancelar")
    save_session(phone, "WAIT_RUT_CANCELAR", data)
    # Defensa sistémica: si el mensaje original ya contiene un RUT válido,
    # procesarlo directo sin pedirlo otra vez. Caso real 2026-04-28 (Camila
    # Salas, 56967753900): paciente escribió "Para que me la anulen porfa
    # 21.234.722-1" y el bot le pidió el RUT 2 veces más.
    if txt:
        from medilink import clean_rut as _cr, valid_rut as _vr
        _rut_emb = _cr(txt)
        if _vr(_rut_emb):
            log_event(phone, "rut_extraido_de_frase", {"flow": "cancelar"})
            return await handle_message(phone, _rut_emb, get_session(phone))
    return (
        "Claro, te ayudo a cancelar una hora.\n\n"
        "Necesito tu RUT para buscarte:\n"
        "(ej: *12.345.678-9*)"
        + _PRIVACY_NOTE
    )


async def _iniciar_ver(phone: str, data: dict, txt: str = "") -> str:
    if is_medilink_down():
        return _modo_degradado(phone, "ver_reservas")
    save_session(phone, "WAIT_RUT_VER", data)
    # Mismo defensivo: extraer RUT del mensaje si está embebido.
    if txt:
        from medilink import clean_rut as _cr, valid_rut as _vr
        _rut_emb = _cr(txt)
        if _vr(_rut_emb):
            log_event(phone, "rut_extraido_de_frase", {"flow": "ver"})
            return await handle_message(phone, _rut_emb, get_session(phone))
    return (
        "Claro, te muestro tus reservas.\n\n"
        "Necesito tu RUT:\n"
        "(ej: *12.345.678-9*)"
        + _PRIVACY_NOTE
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
            citas = await listar_citas_paciente(paciente["id"], rut=paciente.get("rut"))
            if not citas:
                reset_session(phone)
                return (
                    f"No encontré citas futuras para *{_first_name(paciente.get('nombre'))}* 📋\n\n"
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
        + _PRIVACY_NOTE
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
    nombre_corto = _first_name(nombre)
    saludo = f"*{nombre_corto}*, " if nombre_corto else ""
    _sx_w = (data.get("sexo") or (data.get("paciente") or {}).get("sexo") or "").upper()
    _flex_ins = "inscrita" if _sx_w == "F" else "inscrito"
    return (
        f"✅ Listo {saludo}quedaste {_flex_ins} en la lista de espera de *{esp}*.\n\n"
        "Apenas se libere un cupo te aviso por este mismo chat 📱\n\n"
        "_Escribe *menu* si necesitas algo más._"
    )


def _format_citas_reagendar(citas: list, nombre_paciente: str) -> dict:
    """Muestra las citas del paciente para que elija cuál reagendar."""
    nombre = _first_name(nombre_paciente)
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
            title = f"⚡ {hora} — Primero disp." if i == 1 and not mostrar_todos else hora
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
        prefix = f"*{i}.* ⚡ {hora} — Primero disponible" if i == 1 and not mostrar_todos else f"*{i}.* {hora}"
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
    nombre = _first_name(nombre_paciente)
    rows = []
    for i, c in enumerate(citas, 1):
        fecha_short = f"{c['fecha'][8:10]}/{c['fecha'][5:7]}" if c.get("fecha") else c.get("fecha_display", "")[:5]
        rows.append({
            "id": str(i),
            "title": f"{fecha_short} {c['hora_inicio'][:5]}"[:24],
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


async def _admin_status_report_live() -> str:
    """Genera el reporte de salud en vivo para el admin (comando /status).
    Separado de handle_message para aislar los imports locales y evitar
    que sombreen variables globales (UnboundLocalError)."""
    try:
        from datetime import datetime as _dt_now
        from zoneinfo import ZoneInfo as _ZI
        from medilink import get_stats_429, _proxima_cache
        from resilience import is_medilink_down as _is_down
        from session import _conn as _conn_fn
        import sys as _sys
        ahora = _dt_now.now(_ZI("America/Santiago")).strftime("%H:%M")
        stats = get_stats_429()
        total_429 = stats.get("total", 0)
        cache_n = len(_proxima_cache)
        _mod = _sys.modules.get("app.main") or _sys.modules.get("main")
        scheduler = getattr(_mod, "scheduler", None) if _mod else None
        sched_running = bool(scheduler and scheduler.running)
        sched_jobs = len(scheduler.get_jobs()) if scheduler else 0
        try:
            with _conn_fn() as c:
                r = c.execute("""
                    SELECT
                      SUM(CASE WHEN direction='in' THEN 1 ELSE 0 END) AS ins,
                      SUM(CASE WHEN direction='out' THEN 1 ELSE 0 END) AS outs
                    FROM messages WHERE ts >= datetime('now','-30 minutes')
                """).fetchone()
                msgs_in = r["ins"] or 0
                msgs_out = r["outs"] or 0
        except Exception:
            msgs_in = msgs_out = "?"
        medilink_down = _is_down()
        icono = "🟢" if (not medilink_down and sched_running and sched_jobs > 0) else "🔴"
        return (
            f"{icono} *CMC bot · {ahora}*\n\n"
            f"Medilink: {'DOWN' if medilink_down else 'ok'}\n"
            f"429 totales: {total_429}\n"
            f"Cache próxima: {cache_n} entradas\n"
            f"Scheduler: {sched_jobs} jobs · running={sched_running}\n"
            f"Mensajes 30min: in={msgs_in} · out={msgs_out}\n\n"
            f"_Ventana 24h abierta ✅ · los reportes periódicos llegarán_"
        )
    except Exception as _e:
        log.error("Error en _admin_status_report_live: %s", _e)
        return "⚠️ Error generando reporte. Revisa logs."
