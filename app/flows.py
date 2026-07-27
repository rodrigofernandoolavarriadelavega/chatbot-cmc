"""
Máquina de estados para los flujos de conversación.
Opción C: Claude detecta intención → sistema guía el flujo → Medilink ejecuta.
"""
import asyncio
import logging
import re
import time
from datetime import datetime, timedelta, timezone, date
from zoneinfo import ZoneInfo

import httpx
_CHILE_TZ = ZoneInfo("America/Santiago")

from claude_helper import (detect_intent, respuesta_faq, clasificar_respuesta_seguimiento,
                           clasificar_respuesta_crosssell,
                           consulta_clinica_doctor, classify_with_context)
from medilink import (buscar_primer_dia, buscar_slots_dia, buscar_slots_dia_por_ids,
                      buscar_paciente, buscar_paciente_por_nombre, crear_paciente, crear_cita,
                      listar_citas_paciente, cancelar_cita, obtener_agenda_dia,
                      valid_rut, clean_rut, hint_rut_error, especialidades_disponibles,
                      consultar_proxima_fecha, verificar_slot_disponible)
from session import (save_session, reset_session, get_session, save_tag, delete_tag, get_tags,
                     save_cita_bot, log_event, has_recent_event,
                     save_profile, get_profile, save_fidelizacion_respuesta, get_ultimo_seguimiento,
                     enqueue_intent, add_to_waitlist, cancel_waitlist,
                     get_cita_bot_by_id_cita, mark_cita_confirmation, get_phone_by_rut,
                     get_cita_recepcion_by_id_cita, get_cita_recepcion_confirmable,
                     save_demanda_no_disponible, get_waitlist_by_especialidad,
                     mark_waitlist_notified, get_ultima_cita_paciente,
                     has_privacy_consent, save_privacy_consent, revoke_privacy_consent,
                     get_citas_bot_futuras,
                     adquirir_slot_lock, liberar_slot_lock,
                     log_cross_sell, puede_cross_sell,
                     get_pending_crosssell, consume_pending_crosssell,
                     marcar_bono_primera_cita, marcar_bono_notificado,
                     registrar_bono_referral, conteo_referidos_mes,
                     mark_horas_vacias_respondio, mark_horas_vacias_agendo,
                     registrar_slot_rechazado, get_slots_rechazados,
                     get_recent_pni_event, log_pni_cita_generada,
                     add_family_link, list_family_links)
from resilience import is_medilink_down
from triage_ges import triage_sintomas, normalizar_texto_paciente
from pni import get_vaccine_reminder, get_pni_meta
from hitos_desarrollo import get_milestones_reminder, get_hitos_meta
from config import CMC_TELEFONO, CMC_TELEFONO_FIJO, ADMIN_ALERT_PHONE
from messaging import send_whatsapp

log = logging.getLogger("bot.flows")


def _ctwa_clid_for(phone: str) -> str | None:
    """Recupera el ctwa_clid (Click-to-WhatsApp click-id) del paciente para CAPI.

    Los ads CTWA NO traen `fbclid` (eso es solo para clics web); el click-id nativo
    es `ctwa_clid`, que se guarda en meta_referrals al llegar el referral. Sin esto,
    los eventos Lead/Schedule/CompleteRegistration salen con attr=none y Meta no los
    atribuye a la campaña aunque el clic esté fresco. TTL amplio (90d) porque el ciclo
    clic→agenda→atención puede exceder los 7d; Meta ignora los fuera de su ventana,
    enviarlo de más no daña. Devuelve None si el paciente no vino de un ad CTWA.
    """
    try:
        from session import get_meta_referral_fresh as _gmrf_capi
        return ((_gmrf_capi(phone, ttl_horas=2160) or {}).get("ctwa_clid")) or None
    except Exception:
        return None


# Dirección canónica del CMC — usar siempre esta constante, no strings hardcodeados.
_CMC_DIRECCION = "Monsalve 102 (frente a la antigua estación de trenes), Carampangue"

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


def _abono_gate_psiq_activo() -> bool:
    """Flag efectivo para el Abono-Gate de Psiquiatría.

    Patrón idéntico a promo_postconsent._active():
    switchboard (Sala de Máquinas) > env var > OFF por defecto.
    """
    env_val = __import__("os").getenv("ABONO_GATE_PSIQ_ACTIVE", "false").lower() in ("true", "1", "yes")
    try:
        from alma_switchboard import effective
        return effective("ABONO_GATE_PSIQ_ACTIVE", env_val)
    except Exception:
        return env_val


def _first_name(nombre) -> str:
    """Primer token de un nombre, seguro ante None/vacío/solo-espacios.
    Devuelve "" cuando el nombre está vacío para que los callers puedan
    decidir si saludan por nombre o usan un saludo genérico (guard: if nombre_corto:).
    """
    parts = (nombre or "").split()
    return parts[0] if parts else ""


def _detectar_franja_horaria(txt: str) -> "tuple[int, int] | None":
    """Retorna (hora_min, hora_max) si detecta franja horaria en el texto, None si no.
    Ejemplos: "despues de las 5 de la tarde" -> (17, 23), "en la manana" -> (8, 12).
    Se guarda en data["franja_horaria"] para filtrar slots al presentarlos.
    """
    tl = txt.lower()
    m = re.search(r"despu[e\xe9]s\s+de\s+las\s+(\d{1,2})", tl)
    if m:
        h = int(m.group(1))
        if h <= 12 and any(k in tl for k in ("tarde", "noche", "pm", "p.m")):
            h += 12
        return (h, 23)
    m = re.search(r"antes\s+de\s+las\s+(\d{1,2})", tl)
    if m:
        h = int(m.group(1))
        if h <= 12 and any(k in tl for k in ("tarde", "pm", "p.m")):
            h += 12
        return (8, h)
    if re.search(r"(?:en|por)\s+la\s+ma[\xf1n]ana", tl):
        return (8, 12)
    if re.search(r"(?:en|por)\s+la\s+tarde", tl):
        return (12, 18)
    if re.search(r"(?:en|por)\s+la\s+noche", tl):
        return (18, 22)
    return None


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


async def _buscar_slots_dia_con_retry(especialidad: str, fecha: str, **kwargs):
    """FIX 3 (2026-06-10): retry con backoff para buscar_slots_dia.
    Intenta hasta 3 veces (0.5s y 1s de espera entre reintentos).
    Si los 3 fallan, relanza la última excepción para que el caller la maneje.
    """
    ultimo_exc = None
    for intento, espera in enumerate([0, 0.5, 1.0]):
        if espera:
            await asyncio.sleep(espera)
        try:
            return await buscar_slots_dia(especialidad, fecha, **kwargs)
        except Exception as _e:
            ultimo_exc = _e
            if intento < 2:
                log.warning("buscar_slots_dia reintento %d/%d esp=%s fecha=%s: %s",
                            intento + 1, 3, especialidad, fecha, _e)
    raise ultimo_exc


EMERGENCIAS  = {
    # generales
    "emergencia", "urgencia", "no puedo respirar",
    "estoy grave", "me estoy muriendo", "perdí el conocimiento", "perdi el conocimiento",
    "accidente", "desmayo", "convulsion", "convulsión",
    # NOTA (2026-07-01): "mucho dolor" y "dolor muy fuerte" se RETIRARON de este
    # set. Eran gatillos SAMU por substring y disparaban ambulancia ante
    # "mucho dolor de cabeza/muela/regla/espalda" — dolor sin localización vital
    # NO es SAMU (caso real: paciente pedía reagendar su kine por cefalea → SAMU).
    # El dolor genérico ahora pasa por el triage GES normal. Las frases realmente
    # vitales (pecho, cefalea en trueno, ACV, hemorragia) siguen abajo, enumeradas.
    # respiratorio severo
    "me ahogo", "no me entra aire", "ahogo fuerte",
    # cardiovascular severo
    "dolor de pecho fuerte", "dolor fuerte en el pecho", "dolor en el pecho fuerte",
    "me duele mucho el pecho", "infarto", "me da un infarto",
    "me duele el pecho", "dolor en el pecho", "opresion en el pecho", "opresión en el pecho",
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
    # neurológico ACV / stroke (ventana trombolisis 4.5h)
    "boca torcida", "boca chueca", "se me torció la boca", "se me torcio la boca",
    "no puedo hablar", "habla trabada", "se trabó la lengua", "se trabo la lengua",
    "brazo que no levanta", "no siento el brazo", "no puedo mover el brazo",
    "cara caída", "cara caida", "se le cayó la cara", "se le cayo la cara",
    "no me sale la palabra", "no me salen las palabras",
    "acv", "derrame cerebral", "hemiparesia",
    # cefalea en trueno (hemorragia subaracnoidea)
    "peor dolor de cabeza de mi vida", "el peor dolor de cabeza",
    "me exploto la cabeza", "me explotó la cabeza",
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
    # BUG-6: pecho sin intensificador → también es potencial emergencia
    re.compile(r"\bme\s+duele\s+el\s+pecho\b"),
    re.compile(r"\bdolor\s+(?:fuerte\s+)?en\s+el\s+pecho\b"),
    re.compile(r"\bopresi[oó]n\s+(?:en\s+)?(?:el\s+)?pecho\b"),
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

# Detecta cuando el paciente quiere agendar para otra persona (WAIT_MODALIDAD).
# Compilado a nivel de módulo — antes se compilaba en cada mensaje, lo que
# causaba re.error en runtime por paréntesis desbalanceados (Bug 2026-05-18:
# el \b( exterior nunca cerraba → 7 pacientes recibieron "problema técnico").
# Fix: doble )) al final del bloque "para mi (?:...)" para cerrar el (?:...)
# y el grupo captura exterior. re.IGNORECASE para cubrir "Mi hijo", "MI ESPOSA".
_OTRA_PERSONA_RE = re.compile(
    r"\b(otra persona|otr[oa] familiar|mi esposo|mi esposa|"
    r"mi hijo|mi hija|mi mam[aá]|mi pap[aá]|mi hermano|mi hermana|"
    r"mi abuelo|mi abuela|mi abuelito|mi abuelita|"
    r"mi pololo|mi polola|mi pareja|mi nieto|mi nieta|"
    r"mi suegro|mi suegra|mis suegros|"
    r"mi cuñado|mi cuñada|mis cuñados|mis cuñadas|"
    r"mi sobrino|mi sobrina|mis sobrinos|mis sobrinas|"
    r"mi tío|mi tía|mis tíos|mis tías|"
    r"mi vecino|mi vecina|"
    r"mi yerno|mi nuera|"
    r"un familiar|para un amigo|para una amiga|"
    r"mi beb[eé]|mi guagua|mi niñ[oa]|mi niet[oa]|mi chic[oa]|"
    r"mi pequeñ[oa]|mi hij[oa] menor|mi hij[oa] de|"
    r"para mi beb[eé]|para mi guagua|para mi niñ[oa]|"
    r"para mi (?:hijo|hija|mam[aá]|pap[aá]|hermano|hermana|"
    r"abuelo|abuela|abuelito|abuelita|esposo|esposa|pareja|"
    r"nieto|nieta|suegro|suegra|cuñado|cuñada|"
    r"sobrino|sobrina|tío|tía|vecino|vecina|"
    r"yerno|nuera|pololo|polola|"
    r"beb[eé]|guagua|niñ[oa]|chic[oa]|pequeñ[oa]))|"
    r"\ba nombre de\s+\w+|"
    r"\bla cita es para\s+\w+|"
    r"\b(?:reservar|agendar|hora)\s+para\s+\w+\s+\w+\b",
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
    # (modalidad, precio_base, sufijo_opcional)
    # Para "ambas" la tupla es: ("ambas", precio_fonasa, None, precio_particular)
    "Medicina General":       ("ambas",      7880,  None, 25000),  # Fonasa $7.880 / Particular $25.000
    "Medicina Familiar":      ("ambas",      7880,  None, 30000),  # Fonasa $7.880 / Particular $30.000 (Dr. Márquez)
    "Kinesiología":           ("ambas",     7830, None, 20000),   # Fonasa $7.830 / Particular $20.000 (F035)
    "Psicología Adulto":      ("ambas",    14420, None, 20000),   # Fonasa $14.420 / Particular $20.000 (F035)
    "Psicología Infantil":    ("ambas",    14420, None, 20000),   # Fonasa $14.420 / Particular $20.000 (F035)
    "Nutrición":              ("ambas",     4770, None, 20000),   # Fonasa $4.770 / Particular $20.000 (F035)
    "Bioimpedanciometría":    ("particular", 15000),  # Gisela Pinto — examen aparte, sin bono Fonasa

    "Matrona":                ("ambas",     16000,  None, 20000),  # Fonasa $16.000 / Particular $20.000
    "Psiquiatría":            ("particular", 60000),
    "Neurología":             ("particular", 65000),
    "Tecnología Médica Oftalmológica": ("particular", 15000),  # TM Ana Celedón, $15.000 a TODOS (sin Fonasa)
    "Fonoaudiología":         ("particular", 25000),
    "Podología":              ("particular", 20000, "desde"),
    "Cardiología":            ("particular", 40000),
    "Ginecología":            ("particular", 30000, "eco ginecológica: $35.000"),  # dueño 2026-06-12: ATENCIÓN $30.000, ECO $35.000 (F034 había conflado la eco)
    # "Traumatología" — temporalmente deshabilitada (Dr. Barraza no disponible)
    "Otorrinolaringología":   ("particular", 35000),
    "Gastroenterología":      ("particular", 35000),
    "Ecografía":              ("particular", 40000),
    "Odontología General":    ("particular", 15000, "evaluación"),
    "Ortodoncia":             ("particular", 30000, "control"),   # control / evaluación $30.000; instalación boca completa $120.000
    "Endodoncia":             ("particular",110000, "desde"),
    "Implantología":          ("particular",650000, "desde"),
    "Estética Facial":        ("particular", 15000, "evaluación"),
    # Masoterapia se resuelve dinámicamente según la duración real del slot.
}

# ── Bioimpedanciometría (Gisela Pinto, 52) ───────────────────────────────────
# Prestación aparte de la consulta nutricional: $15.000, bloque de 15 min, sin
# bono Fonasa. Se agenda sola (no requiere consulta) y también se ofrece como
# complemento a quien agenda Nutrición.
_BIA_KEYS: set[str] = {
    "bioimpedanciometría", "bioimpedanciometria",
    "bioimpedancia", "bio impedancia",
    "composición corporal", "composicion corporal",
}

# Indicaciones de preparación. Se envían AL AGENDAR (no sirven el día antes:
# el paciente necesita saber que no debe comer 3 h antes ni entrenar ese día).
# Validadas contra protocolos de fabricante (Tanita/InBody) y ESPEN 2004.
_BIA_PREPARACION = (
    "📋 *Cómo prepararte para tu bioimpedanciometría*\n\n"
    "• *No comas* nada las *3 horas* antes. No necesitas ayuno de toda la noche.\n"
    "• Toma tu agua normal el día anterior. *Evita alcohol y café* el día previo.\n"
    "• *No hagas ejercicio* fuerte ese día, ni sauna ni ducha muy caliente antes.\n"
    "• *Pasa al baño* justo antes del examen.\n"
    "• Ven con *ropa liviana*. El examen se hace *descalzo* (sin zapatos ni calcetines).\n"
    "• Sácate *reloj, celular, cinturón y joyas*.\n"
    "• *No te pongas cremas ni aceites* en manos ni pies ese día.\n"
    "• Al medir: quieto, sin hablar y con los brazos separados del cuerpo.\n\n"
    "Si tienes *marcapasos*, *desfibrilador implantado* o estás *embarazada*, "
    "avísanos: en esos casos no realizamos este examen."
)

# ── Mensajes personalizados de sin-disponibilidad por especialidad ────────────
# Cuando _iniciar_agendar no encuentra slots, consulta esta tabla antes de
# usar el mensaje genérico. Permite explicar razones específicas (ej: ORL sin
# fecha de regreso) en vez de un "no hay horas" genérico.
# Clave = especialidad lowercase exacta. Fácil de revertir: borrar la entrada.
# Valor = str | None. None → usar mensaje genérico.
_ESP_SIN_DISPONIBILIDAD_MSG: dict[str, str | None] = {
    "otorrinolaringología": (
        "El *otorrinolaringólogo* (Dr. Manuel Borrego) no tiene fecha de atención "
        "disponible por el momento.\n\n"
        "¿Quieres que te avisemos apenas tengamos fecha? Te inscribo en la lista "
        "de espera y te escribo por WhatsApp."
    ),
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
    # Todo paciente que agenda con la nutricionista recibe la oferta del examen
    # (decisión dueño 2026-07-11). Es la prestación que hace medible su plan:
    # sin ella, el control siguiente solo compara kilos en la pesa.
    "Nutrición": (
        "\n\n📊 *¿Sabías que hacemos Bioimpedanciometría?*\n"
        "Es un examen rápido e indoloro que mide *de qué están hechos tus kilos*: "
        "cuánta grasa, cuánto músculo y cuánta agua tienes.\n\n"
        "Sirve para saber si lo que bajas es *grasa* (lo que buscamos) o *músculo* "
        "(lo que hay que evitar) — algo que la pesa sola no puede decirte.\n\n"
        "💰 *$15.000* · dura 15 minutos · lo realiza la misma *Gisela Pinto*\n\n"
        "Si quieres agregarlo, escribe *bioimpedanciometría* y te doy hora 😊"
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
        "Dra. Javiera Burgos realiza:\n"  # 2026-07-08: solo Burgos agenda odontología general
        "• Blanqueamiento dental ($75.000)\n"  # F033: precio real $75.000 (no $120.000)
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

# ── Cross-sell interactivo post-confirmación ─────────────────────────────────
# Se dispara UNA VEZ tras confirmar la cita (no en reagendar).
# Cooldown: 1 cross-sell por sesión + 30 días por par (esp_origen, esp_destino).
# Clave = especialidad exacta (igual que PROFESIONALES["especialidad"]).
# Valor = lista de (esp_destino, mensaje_oferta) — se elige el primero disponible.
_CROSS_SELL_RULES: dict[str, list[tuple[str, str]]] = {
    "Ginecología": [
        # La ecografía ginecológica (transvaginal) la hace el propio Dr. Rejón —
        # NO cross-sell a David Pardo. Se quita ese par para no confundir.
        ("Matrona",
         "Aprovecha tu visita ginecológica con un PAP o control con matrona — "
         "Sarai Gómez atiende en el CMC.\n\n"
         "¿Te agendo la toma de muestra?"),
    ],
    # Traumatología eliminada: Dr. Barraza no disponible, CMC no ofrece traumatología.
    "Otorrinolaringología": [
        ("Fonoaudiología",
         "Muchas consultas de otorrino se complementan con una audiometría — "
         "examen indoloro de ~20 min en cabina silente, $25.000.\n\n"
         "La hace Juana Arratia en el CMC. ¿Te la agendo?"),
    ],
    "Endodoncia": [
        ("Odontología General",
         "Después de tu endodoncia es clave una limpieza profunda y evaluar si el "
         "diente necesita corona.\n\n"
         "¿Te agendo el control con nuestros odontólogos?"),
    ],
    "Estética Facial": [
        ("Odontología General",
         "Para complementar tu tratamiento estético, un blanqueamiento dental potencia "
         "el resultado.\n\n"
         "¿Te agendo una evaluación con odontología?"),
    ],
    "Cardiología": [
        ("Medicina General",
         "Antes de tu cardiología es útil tener exámenes recientes de chequeo metabólico "
         "(perfil lipídico, glucosa, función renal).\n\n"
         "¿Te agendo una consulta de medicina general para solicitarlos?"),
    ],
    "Medicina General": [
        ("Kinesiología",
         "Si tienes dolor crónico de espalda, cuello u hombros, la kinesiología "
         "puede darte alivio duradero.\n\n"
         "¿Te agendo una evaluación con nuestros kinesiólogos?"),
        ("Nutrición",
         "Si quieres mejorar tu salud integral, una evaluación nutricional puede "
         "complementar tu chequeo médico.\n\n"
         "Gisela Pinto atiende en el CMC. ¿Te interesa agendar?"),
    ],
    "Medicina Familiar": [
        ("Kinesiología",
         "Si tienes dolor crónico de espalda, cuello u hombros, la kinesiología "
         "puede darte alivio duradero.\n\n"
         "¿Te agendo una evaluación con nuestros kinesiólogos?"),
        ("Nutrición",
         "Si quieres mejorar tu salud integral, una evaluación nutricional puede "
         "complementar tu chequeo médico.\n\n"
         "Gisela Pinto atiende en el CMC. ¿Te interesa agendar?"),
    ],
    # Psiquiatría → Psicología (el inverso: terapia complementa los fármacos).
    # El lado Psicología→Psiquiatría se agrega a la clave "Psicología Adulto" de abajo.
    "Psiquiatría": [
        ("Psicología",
         "El tratamiento psiquiátrico suele potenciarse con *terapia psicológica* "
         "en paralelo.\n\n"
         "¿Te agendo sesiones con nuestros psicólogos para acompañar tu tratamiento?"),
    ],
    "Psicología Adulto": [
        # Psiquiatría primero: el cross-sell más relevante para un paciente en terapia
        # (evaluación de fármacos). Teleconsulta con la Dra. Cecilia Unibazo (jueves).
        ("Psiquiatría",
         "Como complemento a tu terapia, el CMC ahora tiene *psiquiatría* por "
         "teleconsulta (Dra. Cecilia Unibazo, los jueves). El psiquiatra puede evaluar "
         "si un apoyo con medicamentos te ayudaría junto a tu proceso.\n\n"
         "¿Quieres que te agende una evaluación psiquiátrica?"),
        ("Medicina General",
         "El bienestar mental se complementa con salud física. Un chequeo de medicina "
         "general puede descartar causas orgánicas (tiroides, anemia, déficits) que "
         "afectan el ánimo.\n\n"
         "¿Te agendo una consulta?"),
        ("Nutrición",
         "La alimentación impacta directamente el ánimo y la energía. Una evaluación "
         "con nutricionista puede complementar tu proceso terapéutico.\n\n"
         "¿Te agendo con Gisela Pinto?"),
    ],
    "Nutrición": [
        ("Kinesiología",
         "Para potenciar tu plan nutricional, una evaluación kinesiológica puede sumar "
         "— actividad física guiada acelera resultados.\n\n"
         "¿Te interesa agendar con nuestros kinesiólogos?"),
    ],
    "Ortodoncia": [
        ("Estética Facial",
         "Complementa tu nueva sonrisa con estética facial.\n\n"
         "La Dra. Valentina Fuentealba realiza blanqueamiento, armonización facial y más. "
         "¿Te interesa agendar una evaluación?"),
    ],
    # Ecografía: sin cross-sell automático. Las ecografías son multi-tipo
    # (abdominal, tiroidea, musculoesquelética, mamaria, próstata) y muchos
    # pacientes son hombres. Sugerir Ginecología por defecto sería absurdo.
    # Cuando el bot diferencie ECO transvaginal del resto, ahí sí mapear
    # ECO TV → control con Ginecología.
    "Odontología General": [
        ("Ortodoncia",
         "Si quieres alinear tus dientes, este es buen momento para evaluarlo.\n\n"
         "La Dra. Daniela Castillo (ortodoncista) atiende en el CMC. "
         "¿Te interesa una evaluación de ortodoncia?"),
        ("Estética Facial",
         "Si te gustaría complementar tu sonrisa con tratamientos estéticos "
         "(blanqueamiento, armonización, rellenos), la Dra. Valentina Fuentealba "
         "atiende en el CMC.\n\n"
         "¿Te interesa una evaluación?"),
    ],
    "Implantología": [
        ("Odontología General",
         "Antes de tu implante se recomienda una limpieza profunda.\n\n"
         "Nuestros odontólogos pueden realizarla. ¿Te interesa agendar?"),
        ("Estética Facial",
         "Ahora que tienes tu implante, podrías evaluar complementar tu sonrisa con "
         "tratamientos estéticos faciales.\n\n"
         "¿Te agendo una evaluación con la Dra. Fuentealba?"),
    ],
}


def _cross_sell_interactive(phone: str, esp_origen: str,
                            slot_data: dict) -> dict | None:
    """Genera mensaje interactivo de cross-sell si aplica cooldown y regla.
    Retorna dict con el payload de botones, o None si no corresponde."""
    from session import puede_cross_sell, log_cross_sell
    reglas = _CROSS_SELL_RULES.get(esp_origen.strip())
    if not reglas:
        return None
    for esp_destino, mensaje in reglas:
        if puede_cross_sell(phone, esp_origen, esp_destino):
            log_cross_sell(phone, esp_origen, esp_destino, "ofrecido")
            return {
                "_cross_sell_esp_destino": esp_destino,
                "payload": _btn_msg(
                    mensaje,
                    [{"id": f"cs_si:{esp_destino}", "title": "Sí, me interesa"},
                     {"id": "cs_no", "title": "No por ahora"}]
                ),
            }
    return None


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
        "Ya que estás bien, ¿te interesa un control con *solicitud de exámenes generales*? 🩺\n"
        "El doctor te entrega la orden para tomarte sangre, orina y lo que considere según tu edad.\n\n"
        "¿Te lo agendo?",
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


# Mapping de IDs de la lista NPS de post-consulta (1-5 estrellas) a las 3
# categorías legacy ("mejor"/"igual"/"peor") + el rating numérico para NPS.
# Incluye también los IDs antiguos (seg_mejor/seg_igual/seg_peor) para no
# romper mensajes ya enviados antes del cambio a escala 1-5.
_SEG_ID_MAP: dict[str, tuple[str, int]] = {
    "seg_5":     ("mejor", 5),
    "seg_4":     ("mejor", 4),
    "seg_3":     ("igual", 3),
    "seg_2":     ("peor",  2),
    "seg_1":     ("peor",  1),
    "seg_mejor": ("mejor", 5),
    "seg_igual": ("igual", 3),
    "seg_peor":  ("peor",  1),
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


def _precio_line(especialidad: str, slot: dict | None = None, modalidad_override: str | None = None, id_profesional: int | None = None) -> str:
    """Línea de precio para insertar en la oferta de slot.
    Retorna string vacío si la especialidad no tiene precio registrado.

    id_profesional: permite el override por-profesional incluso cuando no hay
    slot completo disponible (ej: WAIT_MODALIDAD, confirmación, reagendamiento).
    Si slot está presente, su id_profesional tiene precedencia sobre este param.
    """
    if not especialidad:
        return ""
    esp = especialidad.strip()
    # Override por-profesional: Dr. Alonso Márquez (id 13) está en el pool de
    # "Medicina General" para el ruteo (ver bypass en medilink.py), pero su
    # consulta particular es $30.000, NO $25.000. Forzamos la tarifa
    # "Medicina Familiar" cuando el slot es suyo, sin tocar su especialidad de
    # ruteo. Cubre cualquier camino (lo agenden como MG o como Med. Familiar):
    # con slot completo, o solo con id_profesional en data de sesión.
    _pid = (slot.get("id_profesional") if slot else None) or id_profesional
    if _pid == 13 and esp.lower() in ("medicina general", "medicina familiar"):
        esp = "Medicina Familiar"
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
        # F004 (auditoría 2026-06-10): data["especialidad"] viaja en MINÚSCULAS
        # por el funnel de agendamiento (_iniciar_agendar guarda especialidad_lower
        # en flows.py:13503), pero las claves de PRECIOS_SLOT son Title Case
        # (espejo de PROFESIONALES en medilink.py). Sin este fallback,
        # _precio_line("kinesiología") devolvía "" y el bot derivaba a recepción
        # un precio que SÍ está en la tabla. Se recupera la clave canónica para
        # que los mensajes que interpolan {esp} muestren la especialidad bien
        # escrita.
        for _k_ps in PRECIOS_SLOT:
            if _k_ps.lower() == esp.lower():
                esp = _k_ps
                entry = PRECIOS_SLOT[_k_ps]
                break
    if not entry:
        return ""
    modalidad = entry[0]
    precio = entry[1]
    sufijo = entry[2] if len(entry) > 2 else None
    precio_str = f"${precio:,}".replace(",", ".")
    # Modalidad "ambas": especialidad tiene precio Fonasa Y precio particular.
    # Tupla: ("ambas", precio_fonasa, None, precio_particular)
    if modalidad == "ambas":
        precio_fonasa = entry[1]
        precio_particular = entry[3] if len(entry) > 3 else None
        f_str = f"${precio_fonasa:,}".replace(",", ".")
        p_str = f"${precio_particular:,}".replace(",", ".") if precio_particular else "—"
        if modalidad_override == "fonasa":
            return f"💰 Fonasa: {f_str}"
        if modalidad_override == "particular":
            return f"💰 Particular: {p_str}"
        # Sin override: mostrar ambos
        if precio_particular:
            return f"💰 Fonasa: {f_str} · Particular: {p_str}"
        return f"💰 Fonasa: {f_str}"
    # Si el paciente preguntó por una modalidad distinta a la default, ser
    # explícito: para Kine/Psico/Nutri/etc el único precio es Fonasa.
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
    if sufijo:
        # Sufijo libre = nota aclaratoria (ej. gineco: la eco tiene otro valor)
        return f"💰 Consulta: {precio_str} · _{sufijo}_"
    return f"💰 Consulta: {precio_str}"


# Especialidades con opción Fonasa — las demás son solo particular y se salta
# la pregunta Fonasa/Particular para reducir un paso en el flujo.
_FONASA_SPECIALTIES = frozenset({
    "Medicina General", "Medicina Familiar", "Kinesiología", "Psicología Adulto",
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
        # ── Roster familiar (arreglo de raíz 2026-06-02) ─────────────────────
        # Si el dueño del celular tiene familiares registrados, NO auto-reservar
        # con su propio RUT: saludarlo por su nombre y preguntar para quién es la
        # hora. El fast-track ciego reservaba siempre al dueño y, al intentar
        # agendar a un hijo con el mismo doctor, chocaba con el límite por
        # profesional (caso real: dos mamás bloqueadas al agendar a sus hijos).
        _owner_rut_ft = perfil.get("rut") or ""
        _deps_ft = []
        try:
            _deps_ft = list_family_links(_owner_rut_ft) if _owner_rut_ft else []
        except Exception as _e_ft:
            log.debug("list_family_links (fast-track) error: %s", _e_ft)
        if _deps_ft:
            tags = get_tags(phone)
            last_modalidad = "fonasa"
            for t in tags:
                if t.startswith("modalidad-"):
                    last_modalidad = t.replace("modalidad-", "")
                    break
            data.update({
                "rut_conocido": _owner_rut_ft,
                "nombre_conocido": perfil.get("nombre") or "",
                "modalidad": last_modalidad,
                "booking_for_other": False,
            })
            _deps_mostrar_ft = _deps_ft[:8]
            _rows_ft = [{"id": "dep_self", "title": "Para mí"}]
            _rows_ft += [
                {"id": f"dep_{d['dependent_rut']}",
                 "title": _first_name(d["dependent_nombre"])[:24],
                 "description": (d.get("relation") or "familiar")[:72]}
                for d in _deps_mostrar_ft
            ]
            _rows_ft.append({"id": "dep_nuevo", "title": "Otra persona"})
            data["_deps_roster"] = [d["dependent_rut"] for d in _deps_mostrar_ft]
            save_session(phone, "WAIT_BOOKING_WHO", data)
            _saludo_ft = _first_name(perfil.get("nombre") or "")
            _hola_ft = f"Hola *{_saludo_ft}* 👋 " if _saludo_ft else ""
            return _list_msg(
                body_text=f"{_hola_ft}¿Para quién es la hora?",
                button_label="Seleccionar",
                sections=[{"title": "¿Para quién?", "rows": _rows_ft}],
            )
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


_RX_CANAL_WA = re.compile(
    r"(?:puedes?\s+)?(?:escri(?:bir(?:me|nos)?|nos)|cont[aá]ct[aá](?:nos|rnos)?)"
    r"(?:\s+(?:al?\s+)?)?(?:por\s+)?(?:el\s+)?[Ww]hats[Aa]pp"
    r"(?:\s+(?:del?\s+)?CMC)?"
    r"(?:\s+[\(+]?\s*5[Ss6]\s*9\s*\d[\d\s\-]*)?",
    re.IGNORECASE,
)

def _strip_canal_circular(text: str, phone: str) -> str:
    """BUG-F: si el paciente ya está escribiendo por WA, quitar frases como
    'puedes escribirme por WhatsApp del CMC (+56966610737)' — referencia circular.
    Solo aplica a phones de WA (no tienen prefijo ig_/fb_)."""
    if not text or not phone:
        return text
    is_wa = not (phone.startswith("ig_") or phone.startswith("fb_"))
    if not is_wa:
        return text
    if not _RX_CANAL_WA.search(text):
        return text
    import logging as _log_cc
    _log_cc.getLogger("bot.flows").info(
        "CANAL_CIRCULAR_STRIPPED phone=%s snippet=%r", phone, text[:120]
    )
    cleaned = _RX_CANAL_WA.sub("", text)
    # Limpiar conectores sueltos que puedan quedar (", o", "o directamente", etc.)
    cleaned = re.sub(r"[\s,]*(o|y)\s+directamente[\s,]*", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"[\s,]+(o|y)\s*$", "", cleaned.strip(), flags=re.IGNORECASE)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    return cleaned


def _menu_msg(primer_contacto: bool = False, nombre: str = "") -> dict:
    """Menú principal. Si primer_contacto=True agrega disclosure Ley 21.719.
    Si nombre está presente y no es primer contacto, saluda por nombre."""
    if primer_contacto:
        # FIX-17: Disclosure obligatorio primera vez (Ley 21.719 + best practices)
        intro = (
            "Hola 👋 Soy el *asistente automático* del Centro Médico Carampangue "
            "(no soy una persona).\n\n"
            "_No entrego consejo médico ni evalúo síntomas. "
            "Si es una urgencia, llama al *SAMU 131*._\n\n"
            f"📍 {_CMC_DIRECCION}.\n\n"
            "¿Qué necesitas hoy?"
        )
    elif nombre:
        # Bug 2 fix: saludo personalizado para pacientes conocidos
        intro = (
            f"Hola de nuevo, *{nombre}* 👋\n\n"
            "¿Qué necesitas hoy?"
        )
    else:
        intro = (
            "Hola 👋 Soy el asistente del *Centro Médico Carampangue*.\n\n"
            f"📍 {_CMC_DIRECCION}.\n\n"
            "¿Qué necesitas hoy?"
        )
    return _list_msg(
        body_text=intro,
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
            msg += f"  • {c['fecha_display']} {c['hora_inicio'][:5]} — {c['profesional']}\n"
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
            {"id": "doc_modo_asistente", "title": "🩺 Asistente"},
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

    # ── "espera" (promesa antigua de la notificación de cancelación) ────────
    # Auditoría promesas 2026-06-12: la notif al profesional decía "responde
    # *espera*" pero el handler nunca existió. Hoy la oferta a lista de espera
    # es AUTOMÁTICA (Alma Operativa); si un profesional responde el token viejo,
    # respuesta honesta en vez de caer al asistente clínico.
    if tl in ("espera", "lista de espera", "ofrecer espera"):
        return ("✅ El cupo liberado ya se ofrece *automáticamente* a la lista "
                "de espera (Alma Operativa). Si alguien lo toma, recepción "
                "confirma la reserva — no necesitas hacer nada.")

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
        return _btn_msg(
            "No pude procesar tu respuesta 😕",
            [{"id": "menu", "title": "🏠 Volver al inicio"}]
        )

    cita_bot = get_cita_bot_by_id_cita(id_cita, phone=phone)
    if not cita_bot:
        # Fallback: las citas agendadas en RECEPCIÓN viven en
        # citas_recepcion_reminders (no en citas_bot). Sin esto, confirmar/
        # reagendar/cancelar desde el recordatorio de recepción devolvía
        # "No encontré esa cita" (bug 2026-06-23). Mismo shape → resto del
        # handler funciona igual (mark_cita_confirmation es no-op tolerante).
        cita_bot = get_cita_recepcion_by_id_cita(id_cita, phone=phone)
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
            f"📍 {_CMC_DIRECCION}\n\n"
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
        # Normalizar a int para evitar TypeError: %d format cuando id_cita es str
        try:
            _id_cita_int = int(id_cita)
        except (ValueError, TypeError):
            _id_cita_int = id_cita
        cita_cancelar = {
            "id": _id_cita_int,
            "especialidad": esp,
            "profesional": prof,
            "fecha": fecha,
            "fecha_display": fecha,
            "hora_inicio": hora,
        }
        data = dict(data or {})
        data["cita_cancelar"] = cita_cancelar
        save_session(phone, "CONFIRMING_CANCEL", data)
        _prof_label_r = f"{esp} — {prof}" if esp else prof
        return _btn_msg(
            f"Entendido 😕 Vamos a cancelar esta hora:\n\n"
            f"🏥 {_prof_label_r}\n"
            f"📅 {fecha}\n"
            f"🕐 {hora}\n\n"
            "¿Confirmas la cancelación?",
            [
                {"id": "si", "title": "✅ Sí, cancelar"},
                {"id": "no", "title": "❌ No, mantener"},
            ]
        )

    return _btn_msg(
            "No pude procesar tu respuesta 😕",
            [{"id": "menu", "title": "🏠 Volver al inicio"}]
        )


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
    "cd_horario", "cd_persona", "cd_datos",
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
        # Frases libres que contienen la modalidad ("el bono es particular", "voy con fonasa", etc.)
        _tl_m = tl
        if ("particular" in _tl_m or "privad" in _tl_m
                or "fonasa" in _tl_m or "bono fonasa" in _tl_m):
            return True
    # WAIT_SLOT: frases muy cortas de navegación
    if state == "WAIT_SLOT":
        if tl in ("otro dia", "otro día", "otra fecha", "cambiar fecha",
                  "ver todos", "todos", "ver mas",
                  "ver más", "mañana", "manana", "hoy", "pasado mañana",
                  "pasado manana",
                  # BUG-G: "otros horarios" y variantes deben llegar al VER_TODOS set
                  # sin pasar por Claude — el pre-router podía interceptarlos y
                  # causar loop (auditor detectó caso fb_35916275847970645 2026-05-02)
                  "otros horarios", "otras horas", "otros", "ver otros",
                  "ver otros horarios", "mas horarios", "más horarios",
                  "otras opciones", "otras alternativas", "mas opciones", "más opciones"):
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


async def _send_review_request_if_due(phone: str, especialidad: str = "", rating: int | None = None) -> None:
    """Pide al paciente que dejó 'mejor' que califique en Google.
    Anti-spam: máx 1 vez cada 90 días por teléfono.
    Se dispara con spawn_task tras la respuesta de upsell/control para no
    competir con el cross-sell ni bloquear la conversación."""
    import asyncio
    # A6: cooldown alineado con el copy "una vez al año" (antes 90d → 4x/año
    # con promesa falsa). 365d cumple exactamente lo que se le dice al paciente.
    if has_recent_event(phone, "review_request_sent", days=365):
        log.info("review_request omitido (anti-spam 365d) phone=%s rating=%s", phone, rating)
        return
    # Sleep corto: solo respiro tras el msg de upsell. Antes 4s, reducido a 2s
    # para minimizar ventana de cancelación si el paciente responde rápido.
    await asyncio.sleep(2)
    try:
        from google_rating import get_review_link
        link = get_review_link()
    except Exception:
        link = "https://search.google.com/local/writereview?placeid=ChIJfwqzraTvaZYRBlt0l4W85JE"
    estrellas = "⭐" * (rating if rating is not None else 5)
    msg = (
        f"Una última cosa {estrellas}\n\n"
        "Si te tomas 30 segundos, ¿podrías dejarnos una reseña en Google? "
        "Tu opinión ayuda a otras familias de Arauco a encontrarnos.\n\n"
        f"👉 {link}\n\n"
        "_Solo te pedimos esto una vez al año. ¡Gracias!_"
    )
    try:
        await send_whatsapp(phone, msg)
        from session import log_message
        log_message(phone, "out", msg, "IDLE")
        log_event(phone, "review_request_sent", {"especialidad": especialidad, "rating": rating})
        log.info("review_request enviado phone=%s rating=%s esp=%s", phone, rating, especialidad)
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
                    # PROFESIONALES se importa más abajo (1941) → Python lo marca
                    # local en toda la función → acá lanzaba UnboundLocalError y el
                    # switch fallaba EN SILENCIO (paciente seguía con el doctor
                    # original). Alias propio importado antes del uso.
                    from medilink import PROFESIONALES as _PROFS_SW
                    # Intentar cargar slots del nuevo doctor para ofrecerlos
                    try:
                        esp_prof = _PROFS_SW.get(int(prof_id), {}).get("especialidad", "").lower()
                        if esp_prof:
                            smart, todos = await buscar_primer_dia(esp_prof, solo_ids=[int(prof_id)])
                            if todos:
                                data["slots"] = (smart or todos)[:5]
                                data["todos_slots"] = todos
                                data["prof_sugerido_id"] = int(prof_id)
                                data["especialidad"] = esp_prof
                                save_session(phone, "WAIT_SLOT", data)
                                prof_nombre_sw = _PROFS_SW.get(int(prof_id), {}).get("nombre", "")
                                # _format_slots puede devolver dict (interactive list)
                                # con <=8 slots → no concatenar, mandar header como msg separado.
                                _slot_resp = _format_slots((smart or todos)[:5])
                                if isinstance(_slot_resp, dict):
                                    await send_whatsapp(phone, f"Cambié a *{prof_nombre_sw}* 👨‍⚕️")
                                    from session import log_message as _lm_sw
                                    _lm_sw(phone, "out", f"Cambié a *{prof_nombre_sw}* 👨‍⚕️", "WAIT_SLOT")
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
        from medilink import _get_horario, PROFESIONALES as _PROFS_HQ
        async with _httpx.AsyncClient(timeout=10) as _c:
            horario = await _get_horario(_c, int(prof_id))
        prof_nombre = _PROFS_HQ.get(int(prof_id), {}).get("nombre", "El profesional")
        especialidad = _PROFS_HQ.get(int(prof_id), {}).get("especialidad", "")
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
    # Alias de texto libre usados como data["especialidad"] antes de normalizar
    "dental", "dentista", "brackets", "limpieza dental", "destartraje",
    "profilaxis", "blanqueamiento", "sarro", "tapadura", "resina",
    "corona dental", "carilla", "frenillo",
}

_ECG_KEYWORDS = frozenset({
    "ecg", "electrocardiograma", "electro cardiograma", "electrocardiografia",
    "electrocardiografía", "electro", "trazado cardiaco", "trazado cardíaco",
})

def _preguntar_precio_respuesta(data: dict | None = None, txt: str = "") -> str:
    """Responde a preguntas de PRECIO (valor monetario).
    Diferente de métodos de pago — el paciente quiere saber CUÁNTO cuesta.

    Lógica:
    1. Si hay especialidad activa con precio en PRECIOS_SLOT → mostrar precio + métodos.
    2. Si hay especialidad activa SIN precio en PRECIOS_SLOT → derivar a recepción.
    3. Sin especialidad en contexto → derivar a recepción con oferta de agendar.
    NO inventar precios. Solo mostrar lo que está en PRECIOS_SLOT o ecografias.py.
    """
    # ECG/electrocardiograma: precio conocido pero NO agendable directo.
    # Informar valor y ofrecer waitlist — no enviarlo a un flujo de agendamiento.
    _txt_low_ecg = (txt or "").lower()
    _esp_low_ecg = ""
    if data:
        _slot_ecg = data.get("slot_elegido") or {}
        _esp_low_ecg = (
            _slot_ecg.get("especialidad") or data.get("especialidad") or ""
        ).lower()
    _menciona_ecg = (
        any(kw in _txt_low_ecg for kw in _ECG_KEYWORDS)
        or any(kw in _esp_low_ecg for kw in _ECG_KEYWORDS)
    )
    if _menciona_ecg:
        return (
            "💰 *ECG (electrocardiograma):* $20.000\n\n"
            "Lo realiza el cardiólogo Dr. Miguel Millán. Por ahora no tenemos "
            "fecha disponible para este examen.\n\n"
            "¿Quieres anotarte en lista de espera? Te avisamos apenas tengamos fecha."
        )
    # Ortodoncia boca completa (instalación de brackets): $120.000
    _menciona_boca_completa = (
        ("boca completa" in _txt_low_ecg or "brackets completo" in _txt_low_ecg)
        and any(k in _txt_low_ecg for k in ("bracket", "ortodoncia", "frenillo"))
    ) or "instalacion de brackets" in _txt_low_ecg or "instalación de brackets" in _txt_low_ecg
    if _menciona_boca_completa:
        return (
            "💰 *Ortodoncia — boca completa (instalación):* $120.000\n\n"
            "Incluye brackets, arcos y ataches para ambas arcadas.\n"
            "Los controles posteriores: $30.000 por visita.\n\n"
            "💳 Pago: efectivo, transferencia, débito o crédito.\n"
            "Para coordinar el inicio del tratamiento escríbenos o llama "
            f"al *{CMC_TELEFONO}*."
        )
    if data:
        slot = data.get("slot_elegido") or {}
        esp = (slot.get("especialidad") or data.get("especialidad") or "").strip()
        if esp:
            _txt_low = (txt or "").lower()
            _modalidad_pedida = None
            if "particular" in _txt_low or "privado" in _txt_low:
                _modalidad_pedida = "particular"
            elif "fonasa" in _txt_low:
                _modalidad_pedida = "fonasa"
            _pid_precio = (slot.get("id_profesional") if slot else None) or data.get("prof_sugerido_id")
            linea = _precio_line(esp, slot if slot else None, modalidad_override=_modalidad_pedida, id_profesional=_pid_precio)
            if linea:
                # Precio conocido → mostrarlo + métodos de pago aplicables
                esp_low = esp.lower()
                if esp_low and any(d in esp_low for d in _ESP_DENTALES):
                    metodos = "• Efectivo, transferencia, débito o crédito\n"
                else:
                    metodos = "• Efectivo o transferencia\n"
                return (
                    f"{linea}\n\n"
                    "💳 *Pago:* se cancela al momento de la atención.\n"
                    f"{metodos}"
                    "No se cobra al agendar la hora."
                )
            # Especialidad activa pero sin precio en tabla → derivar sin inventar
            return (
                f"Para confirmarte el valor exacto de *{esp}*, "
                f"te paso con recepción 😊\n\n"
                f"📞 *{CMC_TELEFONO}* · ☎️ *{CMC_TELEFONO_FIJO}*\n\n"
                "_¿Seguimos con la reserva o prefieres llamar primero?_"
            )
    # Sin especialidad en contexto → derivar a recepción
    return (
        "Para confirmarte el valor exacto, consúltanos directamente:\n\n"
        f"📞 *{CMC_TELEFONO}* · ☎️ *{CMC_TELEFONO_FIJO}*\n\n"
        "_¿Qué especialidad necesitas? Te busco disponibilidad._"
    )


def _preguntar_pago_respuesta(data: dict | None = None, txt: str = "") -> str:
    """Responde sobre MÉTODOS DE PAGO (forma/momento de pago, NO el monto).
    Si hay especialidad activa, muestra los métodos aplicables (médica vs dental).
    Sin contexto, muestra ambos.
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
            _pid_pago = (slot.get("id_profesional") if slot else None) or data.get("prof_sugerido_id")
            linea = _precio_line(esp, slot if slot else None, modalidad_override=_modalidad_pedida, id_profesional=_pid_pago)
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
        f"📍 {_CMC_DIRECCION}\n"
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


def _es_pedido_humano_explicito(txt: str, tl: str) -> bool:
    """¿El paciente pide CLARAMENTE hablar con una persona?

    Decide qué pasa cuando un mensaje matchea `_HUMANO_KW`:
      • True  → escalar directo a recepción (no insistir). El paciente fue
                explícito ("quiero hablar con una persona", "pásame con alguien").
      • False → el mensaje solo MENCIONA recepción/secretaria pero parece una
                pregunta resoluble por el bot (ej: "a qué hora abre recepción",
                "dónde queda la recepción"). Dejamos que el clasificador intente
                resolverla antes de molestar a una recepcionista.

    Por decisión de negocio (2026-06-02): solo deflectamos los pedidos
    IMPLÍCITOS/ambiguos; los explícitos siempre escalan.

    `tl` = texto en minúsculas, sin tildes, ya normalizado.
    """
    # Frases que son una orden inequívoca de hablar con un humano. Si el mensaje
    # contiene alguna, es explícito → escalar. (No exhaustivo; reusa _HUMANO_KW
    # para verbos de intención.)
    _PEDIDO_DIRECTO = (
        "quiero hablar", "necesito hablar", "puedo hablar", "hablar con alguien",
        "hablar con una persona", "con una persona", "persona real", "agente humano",
        "asistente humano", "atencion humana", "no quiero el bot", "no me sirve el bot",
        "pasame con", "comuniqueme con", "comunicame con", "atiendame una persona",
    )
    if any(frase in tl for frase in _PEDIDO_DIRECTO):
        return True

    # TODO(Rodrigo): afinar el caso AMBIGUO con tu conocimiento de cómo escriben
    # los pacientes de Arauco. Aquí decides cuándo una mención de "recepción" /
    # "secretaria" NO es un pedido de humano sino una pregunta resoluble.
    # Pista: si el mensaje trae un interrogativo (que/cuando/donde/cuanto/a que
    # hora/cuanto cuesta) junto a la keyword, casi siempre es FAQ, no handoff.
    # Devuelve False en esos casos para que el bot intente responder solo.
    _INTERROGATIVOS = ("?", "a que hora", "cuando", "donde", "cuanto", "que precio",
                       "que valor", "horario", "abren", "abre", "cierran", "queda")
    if any(w in tl for w in _INTERROGATIVOS):
        return False  # parece pregunta → no es pedido explícito de humano

    # Default conservador: ante la duda, tratar como explícito y escalar.
    # (Más seguro para un centro médico que dejar a un paciente sin respuesta.)
    return True


async def _pre_router_wait(phone: str, txt: str, tl: str, state: str, data: dict):
    """
    Pre-router universal para estados WAIT_*.
    Retorna str (respuesta final) si tomó control; None si el handler normal debe continuar.
    """
    # FIX-12: Rescue intents globales — deben funcionar en CUALQUIER estado.
    # Se procesan ANTES del clasificador (sin costo de Haiku) para máxima fiabilidad.
    # NOTA: "cancelar" NO se incluye aquí porque en WAIT_RUT_CANCELAR el paciente
    # puede escribir "cancelar" como acción del flujo, no para salir.
    tl_rescue = tl.strip()
    _HUMANO_KW = {"humano", "agente", "persona", "secretaria", "secretario",
                  "recepcion", "recepción", "atencion humana", "atención humana",
                  "hablar con alguien", "quiero hablar", "asistente humano",
                  # Variantes 2026-05-10 (auditoría: fb_26075855928754227 x3)
                  "chatear con alguien", "quiero chatear con alguien",
                  "con una persona", "persona real",
                  "no quiero el bot", "atencion humana", "atender por persona",
                  "quiero hablar con alguien", "necesito hablar con alguien"}
    _MENU_KW = {"menu", "menú", "inicio", "reiniciar", "empezar de nuevo", "volver al inicio"}
    _SALIR_KW = {"salir", "olvida", "olvidalo", "olvídalo", "olvida todo",
                 "cancelar flujo", "no quiero nada", "no importa"}

    if tl_rescue in _HUMANO_KW or any(k in tl_rescue for k in _HUMANO_KW if len(k) > 7):
        # Solo escalar si el pedido es EXPLÍCITO. Los ambiguos (ej: "a qué hora
        # abre recepción") caen al clasificador de abajo, que ya sabe responder
        # horario/precio/ubicación, en vez de molestar a una recepcionista.
        if _es_pedido_humano_explicito(txt, tl_rescue):
            log_event(phone, "rescue_humano", {"state": state, "txt": txt[:80]})
            return _derivar_humano(phone=phone, contexto=f"rescue desde {state}")
        log_event(phone, "deflect_recepcion", {"state": state, "txt": txt[:80]})
        # cae al pipeline normal (classify_with_context más abajo)

    if tl_rescue in _MENU_KW:
        log_event(phone, "rescue_menu", {"state": state})
        reset_session(phone)
        _pf_rescue = get_profile(phone)
        _nm_rescue = _first_name((_pf_rescue or {}).get("nombre", "")) if _pf_rescue else ""
        return _menu_msg(nombre=_nm_rescue)

    if tl_rescue in _SALIR_KW:
        log_event(phone, "rescue_salir", {"state": state})
        reset_session(phone)
        return _btn_msg(
        "Listo, salimos del proceso 😊",
        [{"id": "menu", "title": "🏠 Volver al inicio"}]
    )

    # Fast path — evita Claude cuando la respuesta es obvia
    if _es_respuesta_obvia_al_prompt(txt, tl, state, data):
        return None

    # FIX 1 (2026-06-10): en WAIT_WAITLIST_CONFIRM, si el texto empieza con
    # negación (ej: "No del Otorrino"), no llamar a classify_with_context para
    # que el handler normal lo procese como negación ampliada.
    if state == "WAIT_WAITLIST_CONFIRM" and re.match(r"^no\b", tl.strip(), re.IGNORECASE):
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
        elif tag == "preguntar_precio":
            # Pregunta por VALOR monetario → función dedicada que distingue
            # precio conocido / precio desconocido / sin especialidad.
            # NUNCA devolver solo el bloque de métodos de pago como respuesta de precio.
            resp = _preguntar_precio_respuesta(data, txt=txt)
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
        # _responder_pregunta_horario puede devolver un DICT interactivo (lista de
        # slots de OTRO profesional via _format_slots). NO interpolarlo en un
        # f-string — eso mandaba el JSON crudo del payload al paciente (bug
        # 2026-06-23 tel ...0467). Enrutar el dict tal cual a send_whatsapp_interactive.
        if isinstance(resp, dict):
            return resp
        if not resp:
            return recordatorio or None
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
    tl    = txt.lower().strip("*_~").strip()  # BUG-B: quita marcadores de formato WhatsApp (*menu*, _menu_, ~salir~)

    # ── Tracking origen web: marcador "(web)" / "(web: slug)" en texto precargado ──
    # El CTA del sitio (centromedicocarampangue.cl) y los blogs precargan el
    # mensaje wa.me con un marcador discreto al final, ej:
    #   "Hola, quiero agendar una hora. (web)"
    #   "Hola, quiero agendar cardiología. (web: cardiologia)"
    # Es lo único que WhatsApp transmite: los UTMs del query string del wa.me se
    # pierden (WhatsApp solo pasa el campo text=). Detectamos, taggeamos y
    # LIMPIAMOS el marcador para no contaminar la detección de intención.
    _web_match = re.search(
        r"\(\s*web(?:\s*[:：]\s*([\w-]+))?\s*\)\s*$", txt, re.IGNORECASE
    )
    if _web_match:
        _slug = (_web_match.group(1) or "").strip().lower()
        if "referral_source:web" not in get_tags(phone):
            save_tag(phone, "referral_source:web")
            log_event(phone, "referral_source_auto", {"source": "web"})
            if _slug:
                save_tag(phone, f"referral_source:web_{_slug}")
                log_event(phone, "referral_source_auto", {"source": f"web_{_slug}"})
        # Limpiar el marcador del texto para el resto del pipeline conversacional
        txt = txt[: _web_match.start()].rstrip(" .,-–—")
        texto = txt
        tl = txt.lower().strip("*_~").strip()

    # ── Tracking origen email: marcador "(email)" / "(email: segmento)" ──────────
    # El clic en un correo de marketing pasa por /e/c/{token}, que redirige a wa.me
    # con este marcador. Cierra la cadena email → conversación → cita (mismo método
    # que el marcador web: lo único que WhatsApp transmite es el text= precargado).
    _email_match = re.search(
        r"\(\s*email(?:\s*[:：]\s*([\w-]+))?\s*\)\s*$", txt, re.IGNORECASE
    )
    if _email_match:
        _eslug = (_email_match.group(1) or "").strip().lower()
        if "referral_source:email" not in get_tags(phone):
            save_tag(phone, "referral_source:email")
            log_event(phone, "referral_source_auto", {"source": "email"})
            if _eslug:
                save_tag(phone, f"referral_source:email_{_eslug}")
                log_event(phone, "referral_source_auto", {"source": f"email_{_eslug}"})
        txt = txt[: _email_match.start()].rstrip(" .,-–—")
        texto = txt
        tl = txt.lower().strip("*_~").strip()

    # ── BUG-K FIX: Staff whitelist — silencio permanente en IDLE ──────────────
    # Personal médico/admin (ej: Dra. Javiera Burgos 56938738734) usa el canal
    # público para coordinar con recepción. El bot los interceptaba en cada
    # mensaje IDLE generando ~71 mensajes basura/semana y 6 takeovers automáticos.
    # Fix: si el phone está en la whitelist → loggear, guardar mensaje, NO responder.
    # Se acepta HUMAN_TAKEOVER activo (recepcionista tomó control) para que la
    # conversación fluya normalmente cuando recepción quiere responder.
    # EXCEPCIÓN: ADMIN_ALERT_PHONE (dueño) y profesionales con permiso wa_access
    # opt-in al Modo Asistente y SÍ deben recibir respuesta del bot.
    from staff_whitelist import is_staff, get_staff_name
    if is_staff(phone):
        _tiene_wa_access = False
        if phone != ADMIN_ALERT_PHONE:
            try:
                from admin_routes import get_permiso as _gp
                _tiene_wa_access = _gp(phone, "wa_access", default=False)
            except Exception:
                pass
        if phone != ADMIN_ALERT_PHONE and not _tiene_wa_access:
            nombre_staff = get_staff_name(phone)
            log_event(phone, "staff_silenciado", {"nombre": nombre_staff, "state": state})
            # No responder al staff en ningún estado — el mensaje ya fue guardado
            # por log_message en el webhook antes de llegar acá.
            return None

    # ── Fase 4 (Alma operativa): aceptación de cupo liberado ──────────────────
    # Si este paciente tiene una oferta de cupo abierta (le ofrecimos una hora que
    # se liberó por una cancelación) y responde aceptándola (TOMAR / sí), la
    # resolvemos acá: claim atómico "primero que acepta gana" + política de
    # confirmación. El matcher es angosto (no choca con emergencias ni con el
    # flujo normal). Solo en IDLE; gateado internamente por ALMA_OPERATIVA_ENABLED.
    if state == "IDLE":
        try:
            from alma_brain import operativa
            if await operativa.maybe_accept_offer(phone, tl):
                return None
        except Exception as e:
            log.warning("operativa: maybe_accept_offer falló phone=%s: %s", phone, e)
    elif state != "HUMAN_TAKEOVER" and ("tomar" in tl or "tomo" in tl
                                        or "la quiero" in tl or "lo quiero" in tl):
        # Auditoría promesas 2026-06-12: la oferta de cupo llega proactiva y el
        # paciente puede estar a medio flujo (WAIT_SLOT, etc.). Un "TOMAR"
        # EXPLÍCITO (no un "sí" pelado, que ahí significa otra cosa) rescata la
        # oferta desde cualquier estado no-takeover. Solo actúa si DE VERDAD
        # tiene una oferta abierta — si no, cae al handler del estado normal.
        try:
            from alma_brain import operativa
            from session import get_open_offer_for_phone as _goofp
            if _goofp(phone) and await operativa.maybe_accept_offer(phone, tl):
                return None
        except Exception as e:
            log.warning("operativa: rescate tomar falló phone=%s: %s", phone, e)

    # ── Comando admin: /status (y sinónimos) desde el celular del admin ───
    # Abre la ventana 24h de WhatsApp y devuelve el reporte EN VIVO. Útil
    # cuando el job periódico no llegó por "Re-engagement message" (131047).
    if phone == ADMIN_ALERT_PHONE and tl in ("/status", "status", "ping",
                                             "reporte", "/reporte", "health",
                                             "/health", "estado", "/estado"):
        return await _admin_status_report_live()

    # ── Activar WhatsApp CMC (opt-in presencial via QR de recepción) ──────────
    # El paciente atendido offline escanea el QR de recepción → WhatsApp abre
    # con texto pre-llenado "Activar WhatsApp CMC". Esto captura el opt-in
    # formal con botones (Ley 19.628).
    if any(s in tl for s in ("activar whatsapp cmc", "activar whatsapp",
                             "activar wsp cmc", "activar wsp",
                             "activar mensajes cmc")):
        from messaging import send_whatsapp_interactive
        _msg = _btn_msg(
            "👋 ¡Hola! Para enviarte recordatorios de citas, resultados de "
            "exámenes y seguimiento post-consulta del Centro Médico Carampangue "
            "por WhatsApp, necesitamos tu autorización.\n\n"
            "_Tus datos se usan solo para tu atención médica · "
            "Ley 19.628 · agentecmc.cl/privacidad_",
            [
                {"id": "optin_si", "title": "✅ Sí, autorizo"},
                {"id": "optin_no", "title": "❌ No, gracias"},
            ],
        )
        await send_whatsapp_interactive(phone, _msg)
        from session import log_message as _lm_f2
        _lm_f2(phone, "out", "[opt-in QR — autorización privacidad]", "WAIT_OPTIN_CONFIRM")
        save_session(phone, "WAIT_OPTIN_CONFIRM", data)
        log_event(phone, "optin_qr_iniciado", {})
        return None

    # ── Respuesta al opt-in (estado WAIT_OPTIN_CONFIRM) ───────────────────────
    if state == "WAIT_OPTIN_CONFIRM":
        if tl in ("optin_si", "si, autorizo", "si autorizo", "si", "sí",
                  "✅ sí, autorizo", "sí, autorizo", "autorizo", "acepto"):
            save_privacy_consent(phone, status="accepted", method="qr_recepcion")
            log_event(phone, "optin_qr_aceptado", {"method": "qr_recepcion"})
            reset_session(phone)
            return ("✅ ¡Listo! Tu WhatsApp quedó activado para el Centro "
                    "Médico Carampangue.\n\n"
                    "Vas a recibir:\n"
                    "• Recordatorios de tus citas (1 día antes y 2 horas antes)\n"
                    "• Aviso cuando estén listos tus exámenes\n"
                    "• Seguimiento post-consulta\n\n"
                    "Si en cualquier momento quieres desactivar, escribe *salir* o "
                    "*no quiero más mensajes*.\n\n"
                    "Escribe *menú* para ver lo que puedo hacer por ti.")
        if tl in ("optin_no", "no", "no, gracias", "no gracias",
                  "❌ no, gracias", "rechazo", "no quiero"):
            log_event(phone, "optin_qr_rechazado", {})
            reset_session(phone)
            return ("Entendido. No te enviaremos mensajes automáticos. "
                    "Si cambias de opinión, puedes volver a escanear el "
                    "código en recepción cuando quieras. 🙏")
        return ("Por favor, responde con uno de los dos botones: "
                "*✅ Sí, autorizo* o *❌ No, gracias*.")

    # tl_norm = texto del paciente normalizado léxicamente (sin tildes,
    # abreviaciones WhatsApp expandidas, typos frecuentes corregidos,
    # participios rurales arreglados). Lo usamos en los matches hard-coded
    # (emergencias, comandos globales, afirmaciones, negaciones, arauco) para
    # ganar recall con mensajes como "tngo dlor d pcho" o "sangrao mucho".
    # OJO: mantenemos `tl`/`txt` para parseos estrictos (RUT, números, IDs de
    # botón `cat_medico`/`cita_confirm:*`, selección de slot, captura de
    # nombre) y para pasarle a `detect_intent` el texto original.
    tl_norm = normalizar_texto_paciente(txt)

    # Fecha de hoy en Chile — disponible en todos los handlers del flujo.
    # IMPORTANTE: NO mover dentro de bloques condicionales — varios handlers
    # (WAIT_SLOT, etc.) la necesitan independientemente del estado de entrada.
    # Bug cd7aec1: estaba solo en el bloque `intent == "agendar"` → UnboundLocalError
    # cuando state == WAIT_SLOT y el paciente llegaba por otra ruta (ej: consent reply).
    _hoy_cl = datetime.now(_CHILE_TZ).date()

    # ── Contexto recepcionista post-HUMAN_TAKEOVER ────────────────────────────
    # Si la sesión viene de un takeover humano, data puede tener recepcion_resumen.
    # Aplicamos TTL de 30 min: si pasó más tiempo, borramos el contexto para
    # no contaminar conversaciones nuevas.
    _recepcion_resumen: list | None = None
    if isinstance(data, dict) and data.get("recepcion_resumen"):
        try:
            from datetime import datetime as _dt_rc, timezone as _tz_rc
            from zoneinfo import ZoneInfo as _ZI_rc
            _rc_ts_raw = data.get("recepcion_resumen_ts")
            _rc_expired = True
            if _rc_ts_raw:
                _rc_ts = _dt_rc.fromisoformat(_rc_ts_raw)
                if _rc_ts.tzinfo is None:
                    _rc_ts = _rc_ts.replace(tzinfo=_ZI_rc("America/Santiago"))
                _rc_age_min = (_dt_rc.now(_tz_rc.utc) - _rc_ts.astimezone(_tz_rc.utc)).total_seconds() / 60
                _rc_expired = _rc_age_min > 30
            if _rc_expired:
                data.pop("recepcion_resumen", None)
                data.pop("recepcion_resumen_ts", None)
                save_session(phone, state, data)
                log_event(phone, "recepcion_ctx_expirado", {})
            else:
                _recepcion_resumen = data["recepcion_resumen"]
        except Exception as _e_rc:
            log.warning("Error procesando recepcion_resumen: %s", _e_rc)

    # Flush pending_tips: si hay tips guardados del ultimo post-consulta
    # (bug #5 fidelizacion), enviarlos ahora que el paciente escribio y
    # la ventana 24h esta abierta.
    _pending_tips = data.get("pending_tips") if isinstance(data, dict) else None
    if _pending_tips:
        try:
            await send_whatsapp(phone, _pending_tips)
            from session import log_message as _lm_f3
            _lm_f3(phone, "out", _pending_tips, state)
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
    # ── Datos bancarios / de pago → recepción ─────────────────────────────────
    # Caso real Consuelo 2026-06-12: mandó cuenta corriente + banco + email y el
    # bot respondió "¡Gracias por tus datos! ¿Qué especialidad quieres?" (canned).
    # Datos de pago = gestión humana (y dato sensible): derivar de inmediato.
    _BANK_KW = ("cuenta corriente", "cuenta rut", "cta cte", "cta. cte",
                "banco santander", "santander", "bancoestado", "banco estado",
                "banco de chile", "scotiabank", "banco bci", " bci",
                "transferencia", "transferi", "deposite", "deposité")
    if state != "HUMAN_TAKEOVER" and any(k in tl for k in _BANK_KW) \
            and _re_url.search(r"\d{6,}", txt.replace(".", "").replace(" ", "").replace("-", "")):
        save_session(phone, "HUMAN_TAKEOVER", data)
        log_event(phone, "datos_bancarios_a_humano", {"len": len(txt)})
        return (
            "Recibí tus datos 🙏 Una recepcionista los revisará y te confirmará"
            " por acá en breve.\n\n"
            f"_Si es urgente: 📞 *{CMC_TELEFONO}*_"
        )

    # ── Documento Word/PDF recibido ───────────────────────────────────────────
    # El webhook pre-procesa adjuntos Word/PDF y los inyecta con el prefijo
    # "[DOCUMENTO]" para que el bot los derive a recepción sin procesar el
    # texto extraído como si fuera un mensaje normal del paciente.
    if txt.startswith("[DOCUMENTO]") and state != "HUMAN_TAKEOVER":
        save_session(phone, "HUMAN_TAKEOVER", data)
        log_event(phone, "documento_a_humano", {"txt": txt[:200]})
        return (
            "Recibí tu documento 📄 Una recepcionista lo revisará y te responderá"
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
        "otra fecha": "otro_dia",
        "cambiar fecha": "otro_dia",
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

    # ── Texto libre de confirmación de recordatorio ───────────────────────────
    # Caso real: bot envió recordatorio con botones, paciente responde texto libre
    # ("Confirmo", "Sí asistiré", "ahí estaré", "ok") en vez de tocar el botón.
    # Si el phone tiene una cita futura con reminder_sent=1 y sin confirmation_status,
    # interpretar afirmaciones como confirmación y acusar recibo SIN abrir flujo
    # de agendamiento. Solo aplica si la sesión está en IDLE (el recordatorio
    # llega cuando el paciente no está en ningún flujo activo).
    _TOKENS_CONFIRM_RECOD = {
        "confirmo", "confirmar", "confirmado", "confirmada",
        "asistire", "asistiré", "ahi estare", "ahí estaré",
        "alli estare", "allí estaré", "voy a asistir", "voy a ir",
        "si asistire", "sí asistiré", "si voy", "sí voy",
        "si confirmo", "sí confirmo", "si, confirmo", "sí, confirmo",
        "ahí estoy", "ahi estoy",
        "confirmo mi hora", "confirmo asistencia",
    }
    if state == "IDLE" and tl_norm in _TOKENS_CONFIRM_RECOD:
        try:
            from session import db as _conn_rc
            import time as _time_rc
            # Buscar cita futura con recordatorio enviado y sin respuesta aún
            _hoy_rc = datetime.now(_CHILE_TZ).strftime("%Y-%m-%d")
            with _conn_rc() as _c_rc:
                _fila_rc = _c_rc.execute(
                    "SELECT id_cita, especialidad, profesional, fecha, hora "
                    "FROM citas_bot "
                    "WHERE phone=? AND fecha >= ? AND (reminder_sent=1 OR reminder_2h_sent=1) "
                    "AND (confirmation_status IS NULL OR confirmation_status='') "
                    "AND (cancel_detected_at IS NULL) "
                    "ORDER BY fecha ASC, hora ASC LIMIT 1",
                    (phone, _hoy_rc),
                ).fetchone()
            if not _fila_rc:
                # Fallback: cita agendada en RECEPCIÓN (vive en
                # citas_recepcion_reminders, no en citas_bot) — bug 2026-06-23.
                _fila_rc = get_cita_recepcion_confirmable(phone, _hoy_rc)
            if _fila_rc:
                _id_cita_rc = str(_fila_rc["id_cita"])
                mark_cita_confirmation(_id_cita_rc, phone, "confirmed")
                log_event(phone, "cita_confirmada_texto_libre", {
                    "id_cita": _id_cita_rc,
                    "especialidad": _fila_rc["especialidad"],
                    "txt": txt[:80],
                })
                return "Perfecto, te esperamos. Hasta pronto."
        except Exception as _e_rc:
            log.warning("confirm_recordatorio_texto_libre falló: %s", _e_rc)
        # Si no hay cita con recordatorio pendiente, dejar caer al flujo normal

    # ── Texto libre NEGATIVO tras recordatorio ("No", "no puedo") ─────────────
    # Espejo del bloque anterior (caso real María 2026-06-11: recordatorio 2h →
    # respondió "No" → el bot le mostró el menú genérico y la hora quedó tomada).
    # Si hay cita futura recordada sin respuesta, un "no" corto = probablemente
    # no puede asistir → preguntar explícito con botones que reusan el handler
    # de confirmación pre-cita (cita_cancelar:/cita_confirm:).
    _TOKENS_NO_RECOD = {
        "no", "no puedo", "no voy", "no ire", "no iré",
        "no puedo ir", "no puedo asistir", "no podre ir", "no podré ir",
        "no podre", "no podré", "no asistire", "no asistiré",
        "no voy a poder", "no voy a poder ir", "no alcanzo",
    }
    if state == "IDLE" and tl_norm in _TOKENS_NO_RECOD:
        try:
            from session import db as _conn_nr
            _hoy_nr = datetime.now(_CHILE_TZ).strftime("%Y-%m-%d")
            with _conn_nr() as _c_nr:
                _fila_nr = _c_nr.execute(
                    "SELECT id_cita, especialidad, profesional, fecha, hora "
                    "FROM citas_bot "
                    "WHERE phone=? AND fecha >= ? "
                    "AND (reminder_sent=1 OR reminder_2h_sent=1) "
                    "AND (confirmation_status IS NULL OR confirmation_status='') "
                    "AND (cancel_detected_at IS NULL) "
                    "ORDER BY fecha ASC, hora ASC LIMIT 1",
                    (phone, _hoy_nr),
                ).fetchone()
            if _fila_nr:
                _id_nr = str(_fila_nr["id_cita"])
                log_event(phone, "recordatorio_respuesta_negativa", {
                    "id_cita": _id_nr, "txt": txt[:60],
                })
                _cuando_nr = ("hoy" if _fila_nr["fecha"] == _hoy_nr
                              else f"el {_fila_nr['fecha']}")
                return _btn_msg(
                    f"¿No puedes asistir a tu hora de *{_fila_nr['especialidad']}* "
                    f"{_cuando_nr} a las *{_fila_nr['hora']}* con {_fila_nr['profesional']}? 🤔\n\n"
                    "Si la cancelas, el cupo queda libre para otro paciente.",
                    [
                        {"id": f"cita_cancelar:{_id_nr}", "title": "❌ Cancelar mi hora"},
                        {"id": f"cita_confirm:{_id_nr}", "title": "✅ Sí asistiré"},
                    ],
                )
        except Exception as _e_nr:
            log.warning("recordatorio_respuesta_negativa falló: %s", _e_nr)
        # Sin cita recordada pendiente → dejar caer al flujo normal

    # ── Opt-out "No avisar" (avisos de horas liberadas / horas_vacias) ────────
    # El aviso proactivo promete: "Si no quieres recibir más avisos, responde
    # *No avisar*" — pero el handler NO existía (caso real Nataly 2026-06-11:
    # respondió "No avisar" y el bot le mostró el menú genérico). El filtro de
    # candidatos ya excluye el tag marketing_opt_out; acá lo seteamos.
    _TOKENS_NO_AVISAR = {
        "no avisar", "no avisarme", "no quiero avisos", "no mas avisos",
        "no más avisos", "no quiero mas avisos", "no quiero más avisos",
        "dejar de avisar", "no me avisen", "no me avises",
    }
    if state != "HUMAN_TAKEOVER" and tl_norm in _TOKENS_NO_AVISAR:
        try:
            save_tag(phone, "marketing_opt_out")
            log_event(phone, "horas_vacias_optout", {"txt": txt[:60]})
        except Exception as _e_na:
            log.warning("no_avisar opt-out falló phone=...%s: %s", phone[-4:], _e_na)
        return ("Listo 👍 No te enviaremos más avisos de horas disponibles.\n\n"
                "Tus recordatorios de citas que ya tengas reservadas siguen "
                "llegando normal. Si algún día quieres volver a recibir avisos, "
                "escríbenos por acá 😊")

    # ── "¿Tengo hora (hoy)?" — pregunta por SUS citas, no por disponibilidad ──
    # Caso real María 2026-06-11: "Entonces para hoy no tengo hora?" → el bot
    # respondió con el fallback de disponibilidad y tuvo que entrar recepción.
    # Respuesta directa desde citas_bot, sin pedir RUT (el phone ya identifica).
    _PREG_MIS_HORAS = (
        "tengo hora", "no tengo hora", "tengo una hora", "tengo cita",
        "tengo una cita", "tengo hora hoy", "tengo alguna hora",
        "quedo mi hora", "quedó mi hora", "quedo agendada", "quedó agendada",
        "quedo agendado", "quedó agendado", "mi hora quedo", "mi hora quedó",
    )
    if state == "IDLE" and any(p in tl_norm for p in _PREG_MIS_HORAS)             and ("?" in txt or tl_norm.startswith(("entonces", "y ", "no ")) or "hoy" in tl_norm or "manana" in tl_norm):
        try:
            from session import db as _conn_mh
            _hoy_mh = datetime.now(_CHILE_TZ).strftime("%Y-%m-%d")
            with _conn_mh() as _c_mh:
                _citas_mh = _c_mh.execute(
                    "SELECT especialidad, profesional, fecha, hora FROM citas_bot "
                    "WHERE phone=? AND fecha >= ? AND cancel_detected_at IS NULL "
                    "ORDER BY fecha ASC, hora ASC LIMIT 3",
                    (phone, _hoy_mh),
                ).fetchall()
            if _citas_mh:
                log_event(phone, "consulta_mis_horas_atajo", {"n": len(_citas_mh)})
                _lineas_mh = []
                for _cm in _citas_mh:
                    _dia_mh = "HOY" if _cm["fecha"] == _hoy_mh else _cm["fecha"]
                    _lineas_mh.append(
                        f"• *{_dia_mh}* {_cm['hora']} — {_cm['especialidad']} "
                        f"con {_cm['profesional']}")
                return ("Sí ✅ Tienes reservado:\n\n" + "\n".join(_lineas_mh)
                        + "\n\nRecuerda llegar 15 minutos antes con tu cédula. "
                          "Escribe *menu* si necesitas algo más.")
            # Sin citas del bot: puede tener hora tomada en recepción → honesto.
            log_event(phone, "consulta_mis_horas_atajo", {"n": 0})
            return ("Por este chat no veo horas reservadas a tu nombre 🤔\n\n"
                    "Si reservaste por teléfono o en recepción, igual puede estar "
                    f"agendada — confírmalo al {CMC_TELEFONO}.\n\n"
                    "¿Quieres que te agende una hora? Escribe *menu* 😊")
        except Exception as _e_mh:
            log.warning("consulta_mis_horas_atajo falló: %s", _e_mh)

    # ── Respuesta al reenganche "No por ahora" ────────────────────────────────
    # Bug 2026-04-25 (56933748605, 15:32): el botón de jobs.py mandaba
    # "no_gracias_reeng" pero no había handler → caía en HUMAN_TAKEOVER y el
    # bot decía "Una recepcionista te responderá", asustando al paciente que
    # solo dijo "no por ahora". Cerramos amable sin escalar.
    if tl == "no_gracias_reeng":
        log_event(phone, "reenganche_rechazado", {"state": state})
        # Marcar opt-out para que el cron no vuelva a insistir en esta
        # sesion. El flag se limpia en reset_session cuando el paciente
        # inicia un flujo nuevo (escribe menu u otra intencion valida).
        data.pop("reenganche_sent", None)
        data["reenganche_optout"] = True
        save_session(phone, state, data)
        return (
            "Sin problema. Cuando quieras retomar, escribe *menu* y te ayudo.\n\n"
            f"_Tambien nos puedes llamar al {CMC_TELEFONO}._"
        )

    # ── Respuesta al consent_marketing_v1 (Tarea B win-back) ─────────────────
    # Quick Replies del template UTILITY consent_marketing_v1:
    #   Botón 1 → "Sí, acepto"   (o texto libre "SI" / "si acepto")
    #   Botón 2 → "No, gracias"  (o texto libre "NO" / "no gracias")
    # Si el registro en bi.marketing_consent está en estado 'pending',
    # se actualiza al estado correspondiente.
    _es_consent_si = (
        tl in ("sí, acepto", "si, acepto", "si acepto", "si",
               "sí, actívenlos", "si, activenlos", "sí, activenlos", "actívenlos", "activenlos")
        or tl_norm in ("si, acepto", "si acepto", "si",
                       "si, activenlos", "si activenlos", "activenlos")
        or txt in ("Sí, acepto", "Si, acepto", "Sí, actívenlos", "Si, activenlos")
    )
    _es_consent_no = (
        tl in ("no, gracias", "no gracias", "no", "no por ahora")
        or tl_norm in ("no, gracias", "no gracias", "no", "no por ahora")
        or txt in ("No, gracias", "No gracias", "No por ahora")
    )
    # Guard: solo interceptar si el paciente está en IDLE o tiene una solicitud
    # de consent pendiente. "si" en CONFIRMING_CANCEL, WAIT_SLOT, etc. NO debe
    # interceptarse aquí — pertenece al flujo activo del estado.
    _FLOW_STATES = {
        "WAIT_ESPECIALIDAD", "WAIT_SLOT", "WAIT_MODALIDAD", "WAIT_BOOKING_FOR",
        "WAIT_BOOKING_WHO", "WAIT_AGENDAR_OTRO", "WAIT_SLOT_OTRO", "WAIT_PARENTESCO",
        "WAIT_PHONE_OWNER_NAME", "WAIT_RUT_AGENDAR", "WAIT_NOMBRE_NUEVO",
        "WAIT_FECHA_NAC", "WAIT_SEXO", "WAIT_COMUNA", "WAIT_EMAIL",
        "WAIT_REFERRAL", "WAIT_REFERRAL_POST", "CONFIRMING_CITA",
        "WAIT_RUT_CANCELAR", "WAIT_CITA_CANCELAR", "CONFIRMING_CANCEL",
        "WAIT_CITA_CANCELAR_FAMILIAR", "WAIT_RUT_FAMILIAR_CANCELAR",
        "WAIT_RUT_REAGENDAR", "WAIT_CITA_REAGENDAR",
        "WAIT_CITA_REAGENDAR_FAMILIAR", "WAIT_RUT_FAMILIAR_REAGENDAR",
        "WAIT_WAITLIST_CONFIRM", "WAIT_WAITLIST_RUT", "WAIT_WAITLIST_NOMBRE",
        "WAIT_WAITLIST_CONFIRM_ECOCA", "WAIT_WAITLIST_RUT_ECOCA",
        "WAIT_RUT_VER", "WAIT_DATOS_NUEVO",
        "WAIT_QUICK_BOOK", "WAIT_DURACION_MASOTERAPIA", "WAIT_BIA_SCREENING",
        "WAIT_ORTODONCIA_ACTIVO",
        "WAIT_CONFIRMAR_ADULTO", "WAIT_MEDFAM_FALLBACK",
        "WAIT_CROSS_SELL",
        "WAIT_META_SLOT_CHOICE", "WAIT_META_WAITLIST",
    }
    _consent_in_active_flow = state in _FLOW_STATES

    # Routing de consents: si hay un dental_consent pending para este phone,
    # dental siempre gana (handler dental ~línea 2514). Si solo hay marketing
    # pending, gana el general. Si ninguno está pending, la respuesta "si/no"
    # NO se intercepta acá — pertenece a otro handler (doctor, flujo, etc).
    _tiene_dental_pending = False
    _tiene_marketing_pending = False
    if (_es_consent_si or _es_consent_no) and not _consent_in_active_flow:
        try:
            from winback import bi_conn as _bi_route
            with _bi_route() as _pg_route:
                with _pg_route.cursor() as _cur_route:
                    # Guard estricto: solo considerar pending si efectivamente
                    # se envió el consent template en los últimos 7 días.
                    # Sin este guard, un "no, gracias" a un reenganche/cross-sell
                    # se interpretaba como decline marketing (bug 2026-05-28:
                    # 258 phones víctimas, 98 dados de baja sin preguntar).
                    # Match con el teléfono canónico (56XXXXXXXXX). Las filas
                    # de consent ahora se guardan normalizadas; igualar formatos
                    # es lo que destraba el 0/798 (entrante sin '+' vs guardado).
                    from session import normalize_wa_id as _norm_match
                    _phone_match = _norm_match(phone)
                    _cur_route.execute(
                        "SELECT 'dental' FROM bi.dental_consent "
                        "WHERE phone=%s AND status='pending' "
                        "  AND consent_sent_at IS NOT NULL "
                        "  AND consent_sent_at > NOW() - INTERVAL '7 days' "
                        "UNION ALL "
                        "SELECT 'marketing' FROM bi.marketing_consent "
                        "WHERE phone=%s AND status='pending' "
                        "  AND consent_sent_at IS NOT NULL "
                        "  AND consent_sent_at > NOW() - INTERVAL '7 days'",
                        (_phone_match, _phone_match),
                    )
                    _rows_route = _cur_route.fetchall()
                    _tiene_dental_pending = any(r[0] == "dental" for r in _rows_route)
                    _tiene_marketing_pending = any(r[0] == "marketing" for r in _rows_route)
        except Exception as _re:
            log.warning("consent routing error phone=%s: %s", phone, _re)

    # PRIORIDAD: si hay postconsulta_seguimiento pendiente sin respuesta en últimas
    # 24h, "no/sí" responde a ESO, no al consent. Caso Daniela 2026-05-26: respondió
    # "No, gracias" al reenganche dental, postconsulta ya estaba pendiente, bot lo
    # interpretó erróneamente como decline marketing.
    _tiene_postconsulta_pending = False
    if (_es_consent_si or _es_consent_no) and not _consent_in_active_flow \
            and (_tiene_marketing_pending or _tiene_dental_pending):
        try:
            from session import db as _pc_conn
            with _pc_conn() as _c_pc:
                _row_pc = _c_pc.execute(
                    "SELECT 1 FROM fidelizacion_msgs "
                    "WHERE phone=? AND tipo IN ('postconsulta','postconsulta_morning') "
                    "  AND enviado_en >= datetime('now','-24 hours') "
                    "  AND (respuesta IS NULL OR respuesta='') LIMIT 1",
                    (phone,)
                ).fetchone()
                _tiene_postconsulta_pending = bool(_row_pc)
        except Exception:
            pass

    if (_es_consent_si or _es_consent_no) and not _consent_in_active_flow \
            and _tiene_marketing_pending and not _tiene_dental_pending \
            and not _tiene_postconsulta_pending:
        try:
            from winback import (
                registrar_consent_respuesta,
                WINBACK_ACTIVE,
                get_candidato_por_phone,
                ya_enviado_winback_hoy,
                send_winback_smart,
                _especialidad_sin_profesional,
            )
            _consent_status = "accepted" if _es_consent_si else "declined"
            registrar_consent_respuesta(phone, _consent_status, method="reply")
            log_event(phone, "marketing_consent_respuesta", {
                "status": _consent_status,
                "raw": txt[:120],
            })
            if _es_consent_no:
                # Insertar en opt_outs_marketing para exclusión permanente
                try:
                    from winback import bi_conn as _bi_conn
                    with _bi_conn() as _pg:
                        with _pg.cursor() as _cur:
                            _cur.execute(
                                "INSERT INTO bi.opt_outs_marketing "
                                "(phone, source, reason) "
                                "VALUES (%s, %s, %s) "
                                "ON CONFLICT (phone) DO NOTHING",
                                (_phone_match, "consent_marketing_v1", "declined_marketing"),
                            )
                            _pg.commit()
                except Exception as _oe:
                    log.warning("opt_out insert error phone=%s: %s", phone, _oe)
                return "Listo, no recibirás más mensajes de marketing."

            # ── Consent SI: enviar winback inmediato si WINBACK_ACTIVE ────────
            if not WINBACK_ACTIVE:
                # Flag desactivado: confirmar consent sin enviar winback
                log_event(phone, "winback_event_skip_inactive", {})
                return "Listo, queda registrado. Pronto recibirás recordatorios de salud."

            # Rate limit: no enviar más de 1 winback por phone por día
            if ya_enviado_winback_hoy(phone):
                log_event(phone, "winback_event_skip_rate_limit", {})
                return "Listo, queda registrado. Pronto recibirás recordatorios de salud."

            # Buscar datos del paciente en BI (incluye filtros consent + opt-out)
            _candidato = get_candidato_por_phone(phone)
            if not _candidato:
                # Paciente no en BI o no contactable — confirmación genérica
                log_event(phone, "winback_event_skip_no_candidato", {})
                return "Listo, queda registrado. Pronto recibirás recordatorios de salud."

            # Guard disponibilidad: si la especialidad del paciente no tiene
            # profesional disponible (licencia/vacaciones), NO lo invitamos a una
            # hora que no existe — confirmamos el consent sin winback (no silencio).
            if _especialidad_sin_profesional(_candidato.get("ultima_especialidad")):
                log_event(phone, "winback_event_skip_sin_disponibilidad",
                          {"especialidad": _candidato.get("ultima_especialidad")})
                return "Listo, queda registrado. Pronto recibirás recordatorios de salud."

            # Enviar winback event-driven (asíncrono, no bloquea la respuesta)
            import asyncio as _asyncio

            async def _send_now():
                try:
                    ok = await send_winback_smart(_candidato, prefer_session=True)
                    log_event(phone, "winback_event_driven", {
                        "ok": ok,
                        "cohorte": _candidato.get("cohorte"),
                        "especialidad": _candidato.get("ultima_especialidad"),
                    })
                except Exception as _we:
                    log.warning("winback event-driven error phone=...%s: %s", phone[-4:], _we)

            _loop = _asyncio.get_event_loop()
            _loop.create_task(_send_now())

            # El winback ES la respuesta — no mandar acuse intermedio
            return None

        except Exception as _ce:
            log.warning("consent handler error phone=%s: %s", phone, _ce)
            # No escalar a humano
            return "Listo, queda registrado.\n_Escribe *menu* si necesitas algo más._"

    # ── Respuesta al consent_dental_v1 (Win-back Dental) ─────────────────────
    # Quick Replies del template UTILITY consent_dental_v1:
    #   Botón 1 → "Sí, acepto"   (o texto libre "SI")
    #   Botón 2 → "No, gracias"  (o texto libre "NO")
    # Solo se intercepta si hay un consent dental 'pending' para este phone
    # y el paciente no está en un flujo conversacional activo.
    _es_dental_consent_si = (
        tl in ("sí, acepto", "si, acepto", "si acepto", "si")
        or tl_norm in ("si, acepto", "si acepto", "si")
        or txt in ("Sí, acepto", "Si, acepto")
    )
    _es_dental_consent_no = (
        tl in ("no, gracias", "no gracias", "no")
        or tl_norm in ("no, gracias", "no gracias", "no")
        or txt in ("No, gracias", "No gracias")
    )
    if (_es_dental_consent_si or _es_dental_consent_no) and not _consent_in_active_flow \
            and _tiene_dental_pending:
        if True:  # _tiene_dental_pending ya computado arriba en routing
            try:
                from dental_winback import (
                    registrar_dental_consent_respuesta,
                    registrar_dental_opt_out,
                    DENTAL_WINBACK_ACTIVE,
                    get_candidato_dental_por_phone,
                    ya_enviado_dental_winback_hoy,
                    send_dental_winback_smart,
                )
                _dental_status = "accepted" if _es_dental_consent_si else "declined"
                registrar_dental_consent_respuesta(phone, _dental_status, method="reply")
                log_event(phone, "dental_consent_respuesta", {
                    "status": _dental_status,
                    "raw": txt[:120],
                })

                if _es_dental_consent_no:
                    registrar_dental_opt_out(phone, source="consent_dental_v1")
                    return "Listo, no recibirás más mensajes del área dental."

                # ── Promo flyer dental (junio): a TODO el que recién acepta ────
                # Va antes del winback "candidato" porque el flyer es general (no
                # requiere historial dental). Gateado por DENTAL_PROMO_FLYER_ACTIVE.
                try:
                    from config import (DENTAL_PROMO_FLYER_ACTIVE,
                                        DENTAL_PROMO_FLYER_TEMPLATE,
                                        DENTAL_PROMO_FLYER_IMG)
                except Exception:
                    DENTAL_PROMO_FLYER_ACTIVE = False
                if DENTAL_PROMO_FLYER_ACTIVE:
                    import asyncio as _aio_promo
                    async def _send_promo_flyer():
                        try:
                            from messaging import (send_whatsapp_template,
                                                   render_template_body as _rtb_promo)
                            await send_whatsapp_template(
                                phone, DENTAL_PROMO_FLYER_TEMPLATE,
                                header_image_url=DENTAL_PROMO_FLYER_IMG,
                            )
                            # Log con el COPY REAL del template (no una etiqueta) +
                            # la URL para que el panel muestre la miniatura.
                            log_message(
                                phone, "out",
                                _rtb_promo(DENTAL_PROMO_FLYER_TEMPLATE) + "\n" + DENTAL_PROMO_FLYER_IMG,
                                "IDLE",
                            )
                            log_event(phone, "dental_promo_flyer_enviado", {
                                "template": DENTAL_PROMO_FLYER_TEMPLATE,
                            })
                        except Exception as _pfe:
                            log.warning("dental promo flyer error phone=...%s: %s",
                                        phone[-4:], _pfe)
                    _aio_promo.get_event_loop().create_task(_send_promo_flyer())
                    return None  # el flyer ES la respuesta al "Sí, acepto"

                # ── Consent SI dental: enviar winback inmediato si activo ──────
                if not DENTAL_WINBACK_ACTIVE:
                    log_event(phone, "dental_winback_event_skip_inactive", {})
                    return "Listo, queda registrado. Te avisaremos cuando haya disponibilidad dental."

                if ya_enviado_dental_winback_hoy(phone):
                    log_event(phone, "dental_winback_event_skip_rate_limit", {})
                    return "Listo, queda registrado. Te avisaremos cuando haya disponibilidad dental."

                _candidato_dental = get_candidato_dental_por_phone(phone)
                if not _candidato_dental:
                    log_event(phone, "dental_winback_event_skip_no_candidato", {})
                    return "Listo, queda registrado. Te avisaremos cuando haya disponibilidad dental."

                import asyncio as _asyncio_dental

                async def _send_dental_now():
                    try:
                        ok = await send_dental_winback_smart(_candidato_dental, prefer_session=True)
                        log_event(phone, "dental_winback_event_driven", {
                            "ok": ok,
                            "subcohorte": _candidato_dental.get("subcohorte"),
                            "especialidad": _candidato_dental.get("ultima_especialidad"),
                        })
                    except Exception as _dwe:
                        log.warning("dental_winback event-driven error phone=...%s: %s",
                                    phone[-4:], _dwe)

                _asyncio_dental.get_event_loop().create_task(_send_dental_now())
                return None  # el winback dental ES la respuesta

            except Exception as _dce:
                log.warning("dental_consent handler error phone=%s: %s", phone, _dce)
                return "Listo, queda registrado.\n_Escribe *menu* si necesitas algo más._"

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
    #
    # BUG-G FIX: "urgencia" como sustantivo de excusa o referida en pasado/tercera
    # persona NO debe disparar SAMU. Exigir señal de gravedad inmediata presente.
    # Ejemplos falsos positivos:
    #   - "se me presentó una urgencia y no voy a poder asistir" → excusa de cancelación
    #   - "llevé a mi hijo a urgencias y me dijeron..." → relato pasado de tercero
    # La heurística: si el único match de EMERGENCIAS es "urgencia" o "emergencia"
    # (sin otros términos graves), y el mensaje contiene patrones de excusa/relato
    # pasado, se inhibe el trigger.
    _solo_urgencia_trigger = (
        all(p not in tl_norm for p in EMERGENCIAS if p not in ("urgencia", "emergencia"))
        and all(not pat.search(tl_norm) for pat in EMERGENCIAS_PATRONES)
        and all(not pat.search(tl_norm) for pat in EMERGENCIAS_VITAL_PATRONES)
        and all(p not in tl for p in EMERGENCIAS if p not in ("urgencia", "emergencia"))
        and all(not pat.search(tl) for pat in EMERGENCIAS_PATRONES)
        and all(not pat.search(tl) for pat in EMERGENCIAS_VITAL_PATRONES)
        and ("urgencia" in tl_norm or "emergencia" in tl_norm)
    )
    _URGENCIA_EXCUSA = re.compile(
        r"(se me (presento|presentó|surgio|surgió)|tuve una|me surgio|me surgió"
        r"|me (impide|impidio|impidió)|no (voy|puedo|pude) (a )?asistir"
        r"|no (voy|puedo|pude) (a )?ir|no asisti|no asistí"
        r"|llev[eé] a|fui a|fue a|fueron a|lo llev|la llev"
        r"|me dijeron|le dijeron|nos dijeron|me dijo|le dijo"
        r"|el otro día|el otro dia|ayer|la semana pasada|hace unos días|hace unos dias)",
        re.IGNORECASE,
    )
    _GRAVEDAD_INMEDIATA = re.compile(
        r"(me duele|no puedo respirar|estoy sangrando|tengo fiebre alta"
        r"|me siento mal|no aguanto|se está poniendo peor|se esta poniendo peor"
        r"|ahora mismo|en este momento|ahora|no para de|no me para)",
        re.IGNORECASE,
    )
    # C1: "urgencia dental" / "con urgencia para hoy" sin señales vitales → no SAMU
    _DENTAL_URGENCIA_KEYWORDS = re.compile(
        r"(dental|diente|muela|muelas|dientes|para hoy|para ma[ñn]ana|horita|tienen para"
        r"|hora hoy|hora para|hora ma[ñn]ana)",
        re.IGNORECASE,
    )
    _VITAL_SIGNAL = re.compile(
        r"(no puedo respirar|dolor de pecho|infarto|sangrado|desmayo|convuls"
        r"|hemiparesi|boca torcida|boca chueca|cara ca[ií]da|acv|derrame cerebral"
        r"|no siento el brazo|habla trabada|me explot[oó] la cabeza)",
        re.IGNORECASE,
    )
    _urgencia_dental_falso_positivo = (
        _solo_urgencia_trigger
        and _DENTAL_URGENCIA_KEYWORDS.search(tl)
        and not _VITAL_SIGNAL.search(tl)
    )
    # FIX 2 (2026-06-10): inhibir también cuando el match de keyword de emergencia
    # ocurre dentro de una frase claramente en pasado/condicional Y sin ningún
    # indicador de urgencia presente. CONSERVADOR: si hay duda, disparar igual.
    # Caso prod (paciente ...1412): "esa misma preocupación debió haber tenido
    # cuando me correspondía control" → queja histórica, sin urgencia real.
    _PASADO_CONDICIONAL = re.compile(
        r"(debió\s+(haber|haberlo|tenerlo)|debería\s+haber|tendría\s+que\s+haber"
        r"|hubiera\s+(sido|tenido|hecho)|hubiese\s+(sido|tenido|hecho)"
        r"|cuando\s+(me\s+)(correspondía|toc[oó]|atendieron|vine|fui\s+a)"
        r"|en\s+(esa|aquel|ese)\s+(entonces|momento|tiempo|oportunidad)"
        r"|hace\s+(meses|años|tiempo|semanas)\s+(atrás|que))",
        re.IGNORECASE,
    )
    _URGENCIA_PRESENTE = re.compile(
        r"(ahora|en\s+este\s+momento|me\s+siento|tengo\s+(fiebre|dolor|sangrado)"
        r"|estoy\s+(mal|grave|sangrando|desmay|convuls)"
        r"|no\s+puedo\s+respirar|me\s+duele\s+mucho)",
        re.IGNORECASE,
    )
    _inhibir_por_pasado = (
        _PASADO_CONDICIONAL.search(tl)
        and not _URGENCIA_PRESENTE.search(tl)
        and not _VITAL_SIGNAL.search(tl)
    )
    # FIX 3 (2026-07-01): inhibir cuando el mensaje claramente pide MOVER/CAMBIAR
    # una hora futura (reagendar) y NO hay señal vital ni gravedad inmediata. El
    # paciente que explica "tengo dolor de cabeza, ¿puede cambiar mi hora para el
    # viernes?" está planificando a futuro — lo opuesto a una urgencia aguda. Un
    # keyword de emergencia que aparezca de pasada no debe secuestrar el flujo de
    # reagendamiento. CONSERVADOR: cualquier señal vital o gravedad presente lo
    # deja disparar igual (mejor un SAMU de más que suprimir uno real).
    _REAGENDAR_CTX = re.compile(
        r"(reagend|reprogram"
        r"|cambiar\s+(la|mi|de)\s+(hora|cita|d[ií]a)|cambiar\s+para"
        r"|mover\s+(la|mi)\s+(hora|cita)|correr\s+(la|mi)\s+(hora|cita)"
        r"|pasar\s+(la|mi)\s+(hora|cita)|dejar\w*\s+(la\s+hora\s+)?para\s+(el|la|otro)"
        r"|no\s+(voy\s+a\s+)?(podr[ée]|puedo|pude)\s+(ir|asistir|llegar)"
        r"|para\s+(el|la|este|el\s+pr[oó]ximo)\s+"
        r"(lunes|martes|mi[eé]rcoles|jueves|viernes|s[aá]bado|domingo|otro\s+d[ií]a))",
        re.IGNORECASE,
    )
    _inhibir_por_reagendar = (
        _REAGENDAR_CTX.search(tl)
        and not _VITAL_SIGNAL.search(tl)
        and not _GRAVEDAD_INMEDIATA.search(tl)
    )
    _inhibir_emergencia = (
        (_solo_urgencia_trigger and _URGENCIA_EXCUSA.search(tl) and not _GRAVEDAD_INMEDIATA.search(tl))
        or _urgencia_dental_falso_positivo
        or _inhibir_por_pasado
        or _inhibir_por_reagendar
    )

    if not _inhibir_emergencia and (
            any(p in tl_norm for p in EMERGENCIAS)
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
        # ── Re-opt-in "aceptar" (inverso del BAJA; auditoría promesas 2026-06-12:
        #    el mensaje de baja promete "escribe *aceptar*" y el handler NO existía
        #    → el paciente quedaba bloqueado para siempre aunque pidiera volver).
        #    Solo si efectivamente está dado de baja; si no, cae al flujo normal.
        if tl_norm in ("aceptar", "acepto volver", "quiero volver a recibir mensajes") \
                and state == "IDLE":
            try:
                if "marketing_opt_out" in (get_tags(phone) or []):
                    save_privacy_consent(phone, "accepted", method="re_optin_aceptar")
                    delete_tag(phone, "marketing_opt_out")
                    try:
                        from winback import remover_opt_out_marketing, registrar_consent_respuesta
                        remover_opt_out_marketing(phone)
                        registrar_consent_respuesta(phone, "accepted", method="re_optin_aceptar")
                    except Exception as _e_ro:
                        log.warning("re-optin BI falló (no bloquea): %s", _e_ro)
                    log_event(phone, "re_optin_aceptar", {})
                    return ("¡Bienvenido/a de vuelta! 🙌 Volverás a recibir nuestros "
                            "recordatorios y avisos.\n\n"
                            "_Escribe *baja* si cambias de opinión, o *menu* para agendar._")
            except Exception as _e_acc:
                log.warning("re_optin_aceptar falló phone=...%s: %s", phone[-4:], _e_acc)

        if tl in ("stop", "detener", "baja") or tl_norm in ("stop", "detener", "baja"):
            # BUG-D: "baja" es opt-out de campañas, NO debe activarse si el
            # paciente está en flujo activo o con recepcionista. Ejemplos reales:
            # - recepcionista pregunta algo, paciente responde "Baja" (presión baja)
            # - paciente en WAIT_* escribe "baja" referido a síntoma
            # Solo procesar como opt-out desde IDLE cuando no hay flujo activo.
            _estados_activos = (
                "HUMAN_TAKEOVER", "WAIT_SLOT", "CONFIRMING_CITA", "WAIT_MODALIDAD",
                "WAIT_RUT_CANCELAR", "WAIT_CITA_CANCELAR", "WAIT_RUT_REAGENDAR",
                "WAIT_CITA_REAGENDAR", "WAIT_RUT_VER", "WAIT_DATOS_NUEVO",
                "WAIT_NOMBRE_NUEVO", "WAIT_FECHA_NAC", "WAIT_SEXO", "WAIT_BOOKING_FOR",
                "WAIT_BOOKING_WHO", "WAIT_AGENDAR_OTRO", "WAIT_SLOT_OTRO",
                "WAIT_WAITLIST_CONFIRM", "WAIT_REFERRAL_POST",
                "WAIT_META_SLOT_CHOICE", "WAIT_META_WAITLIST",
                "WAIT_WAITLIST_CONFIRM_ECOCA", "WAIT_WAITLIST_RUT_ECOCA",
                "WAIT_ABONO_COMPROBANTE",  # abono-gate psiquiatría
            )
            if state in _estados_activos:
                # No interpretar como opt-out — dejar que el handler del estado decida
                pass
            else:
                revoke_privacy_consent(phone)
                save_tag(phone, "marketing_opt_out")
                log_event(phone, "privacy_consent_revoked")
                # Propagar el opt-out a BI (gap caso Hada 2026-05-30): sin esto
                # el "Baja" no excluía del pool winback y el paciente reaparecía
                # a los 90 días pese a haber pedido baja.
                try:
                    from winback import registrar_opt_out_marketing
                    registrar_opt_out_marketing(phone, source="baja_keyword",
                                                 reason="opt_out_baja")
                except Exception as _ob_err:
                    log.debug("opt-out marketing BI falló (no bloquea): %s", _ob_err)
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
            # Quien pide borrado queda excluido de marketing de inmediato en BI
            # (no esperar a la ejecución manual del borrado).
            try:
                from winback import registrar_opt_out_marketing
                registrar_opt_out_marketing(phone, source="gdpr_deletion",
                                             reason="deletion_requested")
            except Exception as _gd_err:
                log.debug("opt-out marketing BI (borrado) falló: %s", _gd_err)
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

    # ── Hook: respuesta a push de horas vacías ──────────────────────────────
    # Si el paciente recibió horas_vacias_enviado en las últimas 4h y responde
    # "SI" / "AGENDAR" en IDLE, lo llevamos al flujo de agendamiento con la
    # especialidad del push precargada.
    if state == "IDLE" and tl_norm in ("si", "si", "s", "agendar", "quiero", "si quiero"):
        try:
            from session import db as _hv_conn
            import time as _hv_time
            _hv_cutoff = int(_hv_time.time()) - 4 * 3600
            with _hv_conn() as _hv_c:
                _hv_row = _hv_c.execute(
                    "SELECT especialidad, fecha_slot, hora_slot "
                    "FROM horas_vacias_envios "
                    "WHERE phone=? AND enviado_ts >= ? "
                    "ORDER BY enviado_ts DESC LIMIT 1",
                    (phone, _hv_cutoff)
                ).fetchone()
            if _hv_row:
                _hv_esp = _hv_row["especialidad"]
                log_event(phone, "horas_vacias_respondio", {
                    "especialidad": _hv_esp,
                    "fecha_slot": _hv_row["fecha_slot"],
                })
                mark_horas_vacias_respondio(phone, _hv_esp)
                return await _iniciar_agendar(phone, data, _hv_esp)
        except Exception as _hv_err:
            log.warning("Hook horas_vacias: %s", _hv_err)

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
        "WAIT_SLOT", "WAIT_MODALIDAD", "WAIT_BOOKING_FOR", "WAIT_BOOKING_WHO",
        "WAIT_SLOT_OTRO",
        "WAIT_RUT_AGENDAR", "CONFIRMING_CITA",
        "WAIT_RUT_CANCELAR", "WAIT_CITA_CANCELAR", "CONFIRMING_CANCEL",
        "WAIT_CITA_CANCELAR_FAMILIAR", "WAIT_RUT_FAMILIAR_CANCELAR",
        "WAIT_RUT_REAGENDAR", "WAIT_CITA_REAGENDAR",
        "WAIT_CITA_REAGENDAR_FAMILIAR", "WAIT_RUT_FAMILIAR_REAGENDAR",
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
    # BUG-02: Si el paciente escribe otro saludo después de que ya se ofreció
    # retomar (segunda vez "hola"), re-mostrar el prompt de retomar en vez de
    # resetear. Sin este guard caía al _es_comando_reset y destruía el estado.
    if _es_saludo_puro and state in _FLUJO_RETOMABLE and data.get("_retomar_ofrecido"):
        esp_retomar = data.get("especialidad") or data.get("quick_esp") or "tu cita"
        return _btn_msg(
            f"¡Hola! Tienes un trámite pendiente de *{esp_retomar}*. "
            "¿Continuamos o prefieres empezar de cero?",
            [
                {"id": "retomar_si", "title": "✅ Retomar"},
                {"id": "retomar_no", "title": "🔄 Empezar de cero"},
                {"id": "retomar_menu", "title": "📋 Ver menú"},
            ]
        )
    # Fix psiquiatría 2026-06-30 (caso Carolina): una oferta de reserva sin
    # confirmar ("¿Te la reservo?") vive como state=IDLE + especialidad_sugerida
    # (TTL 2min). NO está en _FLUJO_RETOMABLE, así que un saludo puro ("Saludos
    # Carolina") caía al reset de bienvenida y borraba el contexto de la
    # especialidad ofrecida. Aquí, si el saludo llega con una sugerencia fresca,
    # re-emitimos la oferta en vez de resetear.
    if _es_saludo_puro and state == "IDLE":
        _esp_pend = data.get("especialidad_sugerida")
        _esp_pend_ts = data.get("especialidad_sugerida_ts")
        _pend_fresh = False
        if _esp_pend and _esp_pend_ts:
            try:
                _tg = datetime.fromisoformat(_esp_pend_ts)
                if _tg.tzinfo is None:
                    _tg = _tg.replace(tzinfo=timezone.utc)
                _pend_fresh = (datetime.now(timezone.utc) - _tg).total_seconds() <= 120
            except (ValueError, TypeError):
                _pend_fresh = False
        if _pend_fresh:
            log_event(phone, "saludo_preserva_sugerida", {"esp": _esp_pend})
            return _btn_msg(
                f"¡Hola! 👋 Quedó pendiente reservar tu hora de *{_esp_pend}*.\n\n"
                "¿Te la reservo?",
                [
                    {"id": "agendar_sugerido", "title": "✅ Sí, agendar"},
                    {"id": "no_agendar",      "title": "No por ahora"},
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
        _pf_retomar = get_profile(phone)
        _nm_retomar = _first_name((_pf_retomar or {}).get("nombre", "")) if _pf_retomar else ""
        return _menu_msg(nombre=_nm_retomar)
    if _es_comando_reset and state != "HUMAN_TAKEOVER":
        reset_session(phone)
        if phone == _doctor_phone:
            # El modo se lee del tag, no de la sesión — sobrevive el reset
            doc_mode = _get_doctor_mode(phone)
            if doc_mode == "agente":
                _pf_doc = get_profile(phone)
                _nm_doc = _first_name((_pf_doc or {}).get("nombre", "")) if _pf_doc else ""
                return _menu_msg(nombre=_nm_doc)
            if doc_mode == "asistente":
                return (
                    "👨‍⚕️ *Asistente Clínico* listo.\n"
                    "Escribe *modo* para cambiar."
                )
            return _doctor_mode_menu()
        # FIX 4 (2026-06-10): si venimos de un resume de takeover reciente (<24h),
        # NO enviar saludo de primera interacción — el bot simplemente queda
        # escuchando. El paciente ya sabe que es el bot (le avisó admin_resume).
        import time as _time_resume
        _resumed_at = data.get("_resumed_from_takeover_at") if isinstance(data, dict) else None
        _es_resume_reciente = (
            _resumed_at is not None
            and (_time_resume.time() - float(_resumed_at)) < 86400  # 24h
        )
        if _es_resume_reciente:
            # Consumir el flag y procesar el mensaje del paciente como IDLE normal
            data_clean = {k: v for k, v in (data or {}).items() if k != "_resumed_from_takeover_at"}
            save_session(phone, "IDLE", data_clean)
            log_event(phone, "resume_post_takeover_skip_saludo", {})
            return await handle_message(phone, txt, {"state": "IDLE", "data": data_clean})

        # FIX-17 (FIX-1-2026-05-13): disclosure en primer contacto (Ley 21.719)
        _primer_contacto_disclosure = not has_recent_event(phone, "disclosure_enviado", days=3650)

        # ── Saludo adaptativo CTWA: disclosure + oferta directa de 3 slots ───
        # Si el paciente llegó desde un anuncio Meta (CTWA con headline),
        # ofrecemos las 3 horas más cercanas de la especialidad del anuncio
        # directamente después del disclosure, sin preguntar "¿quieres agendar?".
        # El disclosure se envía SIEMPRE; los slots son el siguiente bloque.
        if _primer_contacto_disclosure:
            _disclosure_txt = (
                "Hola 👋 Soy el *asistente automático* del Centro Médico Carampangue "
                "(no soy una persona).\n\n"
                "_No entrego consejo médico ni evalúo síntomas. "
                "Si es una urgencia, llama al *SAMU 131*._\n\n"
                f"📍 {_CMC_DIRECCION}."
            )
            try:
                from session import get_meta_referral_fresh as _get_ref_bienvenida
                _ref_bienvenida = _get_ref_bienvenida(phone, ttl_horas=24)
                # Si el paciente YA convirtió (creó una cita hace <24h), el aviso
                # cumplió su pega: NO replayar la oferta del ad ni mandarlo a
                # waitlist (caso María 2026-06-11: agendó 10:02, "menu" 10:17 →
                # el bot le repitió el aviso de brackets y la inscribió en espera).
                _ya_convirtio_ctwa = has_recent_event(phone, "cita_creada", days=1)
                if _ref_bienvenida and _ref_bienvenida.get("headline") and not _ya_convirtio_ctwa:
                    _headline_bv = _ref_bienvenida["headline"]
                    log_event(phone, "disclosure_enviado", {})
                    log_event(phone, "bienvenida_adaptativa_meta", {"headline": _headline_bv[:80]})

                    # Intentar mapear headline → especialidad y buscar 3 slots
                    from medilink import headline_to_especialidad as _h2esp, top3_slots_especialidad as _top3
                    _esp_ctwa = _h2esp(_headline_bv)
                    if _esp_ctwa:
                        try:
                            _slots_ctwa = await _top3(_esp_ctwa, dias=7)
                        except Exception as _e_ctwa:
                            log.warning("CTWA top3_slots falló esp=%s: %s", _esp_ctwa, _e_ctwa)
                            _slots_ctwa = []

                        if _slots_ctwa:
                            from medilink import fmt_slot_ctwa as _fmt_ctwa
                            _lineas = []
                            for _i, _s in enumerate(_slots_ctwa, 1):
                                _lineas.append(f"  *{_i}.* {_fmt_ctwa(_s)}")
                            _esp_display = _headline_bv  # usar el headline original para el mensaje
                            _slots_txt = "\n".join(_lineas)
                            log_event(phone, "ctwa_slots_ofrecidos", {
                                "especialidad": _esp_ctwa,
                                "headline": _headline_bv[:80],
                                "n_slots": len(_slots_ctwa),
                            })
                            # Guardar slots en sesión para handler WAIT_META_SLOT_CHOICE
                            data["meta_offered_slots"] = _slots_ctwa
                            data["meta_esp"] = _esp_ctwa
                            save_session(phone, "WAIT_META_SLOT_CHOICE", data)
                            return (
                                f"{_disclosure_txt}\n\n"
                                f"———\n\n"
                                f"Vi que llegaste desde nuestro aviso de *{_esp_display}*. 👋\n\n"
                                f"Tengo estas horas disponibles esta semana:\n\n"
                                f"{_slots_txt}\n\n"
                                f"¿Te reservo alguna? Responde con el número (*1*, *2* o *3*),\n"
                                f"o escribe *otra fecha* si prefieres otro horario."
                            )

                        # Sin disponibilidad en 7 días
                        log_event(phone, "ctwa_sin_disponibilidad", {
                            "especialidad": _esp_ctwa,
                            "headline": _headline_bv[:80],
                        })
                        _esp_display = _headline_bv
                        data["meta_waitlist_esp"] = _esp_ctwa
                        save_session(phone, "WAIT_META_WAITLIST", data)
                        return (
                            f"{_disclosure_txt}\n\n"
                            f"———\n\n"
                            f"Vi que llegaste desde nuestro aviso de *{_esp_display}*.\n\n"
                            f"No hay horas disponibles esta semana para *{_esp_ctwa.capitalize()}* 😕\n\n"
                            f"¿Quieres que te avisemos cuando se libere una hora?\n"
                            f"Responde *sí* y te contactamos en cuanto haya disponibilidad."
                        )

                    # Headline no mapea a ninguna especialidad → saludo genérico
                    _sx_bv = ((get_profile(phone) or {}).get("sexo") or "").upper()
                    _bv_word = "bienvenida" if _sx_bv == "F" else "bienvenido"
                    return (
                        f"{_disclosure_txt}\n\n"
                        f"Vi que llegaste desde nuestro aviso de *{_headline_bv}*. "
                        f"{_bv_word.capitalize()} al CMC.\n\n"
                        f"¿Quieres agendar una hora o tienes alguna pregunta?"
                    )
            except Exception:
                log.exception("CTWA disclosure bloque falló para phone=%s", phone)
            log_event(phone, "disclosure_enviado", {})

        return _menu_msg(primer_contacto=_primer_contacto_disclosure)

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
    # BUG-E: NO restaurar WAIT_SLOT si la recepcionista tiene la conversación.
    # Caso real: recepcionista mandó "10:20 11:00 11:20 11:40 12:20", paciente
    # respondió "11:00", bot restauró WAIT_SLOT y respondió "¿En qué te puedo ayudar?".
    if state == "IDLE" and state != "HUMAN_TAKEOVER" and data.get("last_slots") and data.get("last_slots_ts"):
        try:
            from time_parser import parse_hora as _parse_hora_idle
            _ls = data["last_slots"]
            _ls_valido = (
                isinstance(_ls, list)
                and _ls
                and all(isinstance(s, dict) and s.get("hora_inicio") for s in _ls)
            )
            if _ls_valido and _parse_hora_idle(txt):
                # FIX-9: last_slots_ts puede ser naive (guardado sin tz).
                # Comparar naive con aware causa TypeError silenciado por el except.
                _ts_snap = datetime.fromisoformat(data["last_slots_ts"])
                if _ts_snap.tzinfo is None:
                    _ts_snap = _ts_snap.replace(tzinfo=timezone.utc)
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
        # Detectar "para hoy/mañana/pasado mañana" UNA VEZ al entrar a IDLE y
        # stash en data. Cualquier path que termine llamando _iniciar_agendar
        # (intent=agendar, triage GES, apellido shortcut, motivo del menú,
        # quick_book) propaga la fecha pedida al disclaimer. Antes solo el
        # branch agendar la propagaba; "Medico para hoy tiene?" pasaba por
        # triage GES y perdía la fecha → bot mostraba sábado sin avisar
        # (caso María 56968621918 + Norma Muñoz, CLAUDE.md pendiente #1).
        _fp_idle_top = _detectar_fecha_pedida_idle(txt)
        if _fp_idle_top:
            data["fecha_pedida_idle"] = _fp_idle_top
        _fr_idle_top = _detectar_franja_horaria(txt)
        if _fr_idle_top:
            data["franja_horaria"] = _fr_idle_top

        # ── Pending cross-sell: el bot envió hace ≤72h un cross-sell con botones
        # (kine / orl-fono / odonto-estética / mg-chequeo / post-dental-ortodoncia).
        # Si el paciente responde con texto libre en vez de tocar el botón,
        # interpretamos su intención y consumimos el pending para no perderlo.
        # Bug original (Ernesto 2026-05-28): bot ofreció kine, paciente respondió
        # "Sí, me interesa" y el bot cayó al menú genérico perdiendo el contexto.
        _pending_cs = get_pending_crosssell(phone, hours=48)
        # Guard: no interceptar si el texto ya es un button payload conocido;
        # esos tienen handler dedicado más abajo y no necesitan clasificación.
        # Se excluyen: cross-sell (x*), adherencia (kine_*), reactivación (reac_*),
        # control (ctrl_*), winback interactivo (wb_*), confirmación citas (cita_*).
        _PROACTIVE_BUTTON_PREFIXES = ("x", "kine_", "reac_", "ctrl_", "wb_", "cita_")
        if _pending_cs and tl and not tl.startswith(_PROACTIVE_BUTTON_PREFIXES):
            # Consumer unificado: cubre cross-sells originales + campañas proactivas
            # (reactivacion, adherencia_kine, control_*, winback_bi, winback_bi_session,
            # winback_fidelizacion). Todos comparten el mismo flujo: si el paciente
            # responde afirmativamente en texto libre → _iniciar_agendar(destino).
            try:
                _cs_destino = _pending_cs["destino"]
                _cs_tipo = _pending_cs["tipo"]
                _cs_decision = await clasificar_respuesta_crosssell(txt, _cs_destino)
                log_event(phone, "proactive_pending_consumido", {
                    "tipo": _cs_tipo,
                    "destino": _cs_destino,
                    "decision": _cs_decision,
                    "txt": txt[:120],
                })
                if _cs_decision == "si":
                    consume_pending_crosssell(phone)
                    perfil = get_profile(phone)
                    if perfil:
                        data["rut_conocido"] = perfil["rut"]
                        data["nombre_conocido"] = perfil["nombre"]
                    return await _iniciar_agendar(phone, data, _cs_destino or None)
                if _cs_decision == "no":
                    consume_pending_crosssell(phone)
                    return (
                        "Sin problema 😊 Cuando lo necesites, estamos acá.\n"
                        "_Escribe *menu* para ver todas las opciones._"
                    )
                # decision == "ambiguo" → no consumir, dejar al router seguir
            except Exception as _e_cs:
                log.warning("pending_crosssell consumer falló: %s", _e_cs)

        # ── Botones residuales de WAIT_SLOT que llegaron tarde (sesión expiró,
        # usuario volvió al menú pero el mensaje tardó en llegar). En vez de
        # devolver el menú genérico, relanzar el flujo de agendar. ──
        if tl in ("ver_otros", "ver_todos", "otro_dia", "otro_día",
                  "otro_prof", "confirmar_sugerido") or tl.startswith("agendar_prof_"):
            return await _iniciar_agendar(phone, data, None)

        # ── BUG-B: botones de aclaración nombre inexistente (pedro kine) ────
        if tl == "prof_armijo":
            log_event(phone, "nombre_inexistente_resuelto", {"prof": "armijo"})
            return await _iniciar_agendar(phone, data, "armijo")
        if tl == "prof_etcheverry":
            log_event(phone, "nombre_inexistente_resuelto", {"prof": "etcheverry"})
            return await _iniciar_agendar(phone, data, "etcheverry")

        # ── BUG-4: botones de la oferta traumatología → MG ──────────────────
        if tl == "trauma_mg":
            data.pop("_traumato_redirect_confirmed", None)
            data.pop("_waitlist_trauma_pending", None)
            data["_traumato_redirect_confirmed"] = True
            log_event(phone, "traumato_acepta_mg", {"phone": phone})
            return await _iniciar_agendar(phone, data, "medicina general")
        if tl == "trauma_waitlist":
            data.pop("_traumato_redirect_confirmed", None)
            data.pop("_waitlist_trauma_pending", None)
            data["waitlist_especialidad"] = "traumatología"
            save_session(phone, "WAIT_WAITLIST_CONFIRM", data)
            log_event(phone, "traumato_elige_waitlist", {"phone": phone})
            return _btn_msg(
                "Te anoto en lista de espera para *Traumatología* 📋\n\n"
                "Te avisaremos en cuanto el Dr. Barraza esté disponible.",
                [
                    {"id": "waitlist_si", "title": "📝 Sí, anotarme"},
                    {"id": "waitlist_no", "title": "No, gracias"},
                ]
            )

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
                    _ts_book = _dt_titular.fromisoformat(_last_book_ts)
                    if _ts_book.tzinfo is None:  # FIX-9: naive guard
                        _ts_book = _ts_book.replace(tzinfo=_tz_titular.utc)
                    _delta = _dt_titular.now(_tz_titular.utc) - _ts_book
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

        # ── BUG-5 FIX: detector de preguntas sobre cita activa ──────────────
        # "A qué hora era mi cita?" / "Qué doctor me toca?" / "Era mañana?"
        _CITA_INFO_KW = re.compile(
            r"(qu[eé]\s+hora|a\s+qu[eé]\s+hora|cu[aá]ndo\s+(?:era|es|tengo)|"
            r"qu[eé]\s+(?:doctor|m[eé]dico|profesional)|d[oó]nde\s+(?:es|atiende|queda)|"
            r"era\s+(?:ma[ñn]ana|hoy|el\s+\w+)|mi\s+(?:cita|hora)\s+(?:era|es|queda)|"
            r"mi\s+pr[oó]xima\s+(?:cita|hora)|tengo\s+(?:cita|hora)\s+(?:hoy|ma[ñn]ana))",
            re.IGNORECASE,
        )
        if _CITA_INFO_KW.search(tl):
            _perfil_cita = get_profile(phone)
            _cita_info = None
            # 1) Buscar en citas_bot (futuras o recientes)
            try:
                from session import get_proxima_cita_paciente as _get_prox_cita
                _cita_info = _get_prox_cita(phone)
            except Exception:
                pass
            # 2) Fallback: data["last_confirmed_cita"]
            if not _cita_info:
                _cita_info = data.get("last_confirmed_cita")
            if _cita_info:
                _ci_prof = _cita_info.get("profesional") or _cita_info.get("nombre_profesional", "")
                _ci_esp = _cita_info.get("especialidad", "")
                _ci_fecha = _cita_info.get("fecha_display") or _cita_info.get("fecha", "")
                _ci_hora = (_cita_info.get("hora_inicio") or _cita_info.get("hora", ""))[:5]
                log_event(phone, "consulta_info_cita", {"prof": _ci_prof, "fecha": _ci_fecha})
                return (
                    f"Tu próxima cita es:\n"
                    f"📅 *{_ci_fecha}*\n"
                    f"🕐 *{_ci_hora}*\n"
                    f"🏥 *{_ci_esp}* — {_ci_prof}\n"
                    f"📍 {_CMC_DIRECCION}\n\n"
                    "_¿Necesitas algo más?_"
                )
            else:
                return (
                    "No tengo registro de una cita próxima tuya por acá.\n\n"
                    "¿Quieres que te ayude a agendar?\n"
                    "_Escribe *menu* para ver opciones._"
                )

        # ── FIX 5 (2026-05-10): confirmaciones post-cita ─────────────────────
        # Paciente acaba de agendar y escribe algo como "está reservada la hora",
        # "ya reserve hora a médico", "Esta Lista". En vez de fallback o menú,
        # responder con info de la cita confirmada si existe una reciente (≤2h).
        # Caso real: 56967963365 llegó a fallback_loop_escalado por esto.
        _POST_CITA_RE = re.compile(
            r"\b(ya\s+reserv[eé]|ya\s+agend[eé]|est[aá]\s+reservada|"
            r"qued[oó]\s*(lista|listo|agendad[ao]|reservad[ao])|"
            r"est[aá]\s+lista|esta\s+lista|"
            r"ya\s+qued[oó]|listo\s+gracia[s]?|todo\s+bien|"
            r"me\s+confirm[ao]|hora\s+reservada|cita\s+reservada|"
            r"ya\s+reserve\s+hora|reserv[eé]\s+(la\s+)?hora|"
            r"\bgr[a]?cia[s]?\b|\blisto\b|\blista\b)\b",
            re.IGNORECASE,
        )
        if _POST_CITA_RE.search(tl):
            # Buscar cita creada en las últimas 2 horas
            _cita_post = None
            try:
                from session import get_proxima_cita_paciente as _get_pc
                _cita_post = _get_pc(phone)
            except Exception:
                pass
            if not _cita_post:
                _cita_post = data.get("last_confirmed_cita")
            if _cita_post:
                # Verificar que la cita fue creada recientemente si tiene timestamp
                _cita_ts = _cita_post.get("created_at") or data.get("last_booking_ts")
                _cita_reciente = True
                if _cita_ts:
                    try:
                        from datetime import datetime as _dt_pc
                        _ts_pc = _dt_pc.fromisoformat(_cita_ts)
                        if _ts_pc.tzinfo is None:
                            _ts_pc = _ts_pc.replace(tzinfo=timezone.utc)
                        _cita_reciente = (datetime.now(timezone.utc) - _ts_pc).total_seconds() < 7200
                    except Exception:
                        _cita_reciente = True  # asumir reciente si no parsea
                if _cita_reciente:
                    _pc_prof = _cita_post.get("profesional") or _cita_post.get("nombre_profesional", "")
                    _pc_esp = _cita_post.get("especialidad", "")
                    _pc_fecha = _cita_post.get("fecha_display") or _cita_post.get("fecha", "")
                    _pc_hora = (_cita_post.get("hora_inicio") or _cita_post.get("hora", ""))[:5]
                    log_event(phone, "confirmacion_post_cita_detectada", {"prof": _pc_prof, "fecha": _pc_fecha})
                    return (
                        f"Tu hora con *{_pc_prof}* ({_pc_esp}) el *{_pc_fecha}* a las *{_pc_hora}* "
                        f"está confirmada.\n\n"
                        f"Te llegará un recordatorio el día anterior y 2 horas antes.\n\n"
                        "_Si necesitas algo más, escribe *menu*._"
                    )

        # ── Seguimiento de FAQ con sugerencia de agendar ──────────────────────
        # Debe ir ANTES de los atajos numéricos (1..4) porque aquí interpretamos
        # "1"/"sí"/botón como "agendar la especialidad ya sugerida en el FAQ".
        esp_sug_prev = data.get("especialidad_sugerida")
        # BUG-8: verificar timestamp de especialidad_sugerida. Si tiene >2 min
        # y el paciente no está respondiendo al botón explícito, limpiar para
        # evitar agendar lo equivocado en otro contexto.
        if esp_sug_prev and tl not in ("agendar_sugerido", "no_agendar"):
            _esp_ts8 = data.get("especialidad_sugerida_ts")
            if _esp_ts8:
                try:
                    from datetime import datetime as _dt8
                    _ts8 = _dt8.fromisoformat(_esp_ts8)
                    if _ts8.tzinfo is None:
                        _ts8 = _ts8.replace(tzinfo=timezone.utc)
                    if (datetime.now(timezone.utc) - _ts8).total_seconds() > 120:
                        data.pop("especialidad_sugerida", None)
                        data.pop("especialidad_sugerida_ts", None)
                        esp_sug_prev = None
                        log_event(phone, "esp_sugerida_expirada", {"esp": esp_sug_prev})
                except Exception:
                    pass
        # ── Defensa sistémica: payload de botón viejo sin contexto ────────────
        # El paciente clickeó un botón "Sí, agendar" / "Otros horarios" / etc.
        # pero la sesión expiró (timeout 30 min) o nunca se guardó el contexto.
        # Sin este handler caía al menú genérico y mostraba un saludo de
        # bienvenida confuso. Caso real 2026-04-28 (56931330787): paciente
        # clickeó "agendar_sugerido" tras un mensaje del bot y recibió 2 saludos
        # genéricos en lugar de retomar el agendamiento.
        if not esp_sug_prev and not data.get("slots"):
            # Botón de vuelta al flujo presencial desde telemedicina
            if tl == "agendar_presencial_tele":
                return await _iniciar_agendar(phone, data, None)
            # FIX 5: payloads huérfanos de botones viejos de WhatsApp.
            # En vez de texto de error, lanzar flujo activo de agendar.
            # agendar_sugerido / confirmar_sugerido: ir directo a selección de esp.
            # agendar_prof_<id>: pre-seleccionar esa especialidad si el ID es conocido.
            # ver_otros / ver_todos / otro_dia / otro_prof: misma lógica.
            _HUERFANOS_AGENDAR = {
                "agendar_sugerido", "confirmar_sugerido",
                "ver_otros", "ver_todos", "otro_dia", "otro_día", "otro_prof",
            }
            if tl in _HUERFANOS_AGENDAR or tl.startswith("agendar_prof_"):
                log_event(phone, "payload_huerfano", {"payload": tl})
                # Intentar extraer especialidad de agendar_prof_<id>
                _esp_hub: str | None = None
                if tl.startswith("agendar_prof_"):
                    try:
                        from medilink import PROFESIONALES as _PROFS_HUB
                        _id_hub = int(tl.replace("agendar_prof_", ""))
                        _esp_hub = _PROFS_HUB.get(_id_hub, {}).get("especialidad")
                    except Exception:
                        _esp_hub = None
                # Preservar perfil para fast-track
                _perfil_hub = get_profile(phone)
                if _perfil_hub and _perfil_hub.get("rut"):
                    data["rut_conocido"] = _perfil_hub["rut"]
                    data["nombre_conocido"] = _perfil_hub.get("nombre", "")
                return await _iniciar_agendar(phone, data, _esp_hub)
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
            # BUG-J FIX: si el mensaje tiene referencias temporales ("para otro día",
            # "para mañana", "lunes", "próxima semana", etc.), conservar especialidad
            # del contexto FAQ y retomar agendamiento en vez de descartar el contexto.
            # Caso real (7 ocurrencias/7d): paciente responde "Para otro día" a slot
            # sugerido → bot mostraba "Hola, parece que tu mensaje quedó incompleto".
            _TEMPORAL_KWS = re.compile(
                r"\b(otro d[ií]a|otra fecha|otro momento|mas adelante|más adelante"
                r"|manana|mañana|lunes|martes|miercoles|miércoles|jueves|viernes"
                r"|sabado|sábado|domingo|próxima semana|proxima semana"
                r"|la semana que viene|para el [a-z]+|otro rato|despues|después"
                r"|cuando pueda|en otro momento|en otra fecha)\b",
                re.IGNORECASE,
            )
            if _TEMPORAL_KWS.search(tl_norm):
                log_event(phone, "faq_esp_otra_fecha", {"esp": esp_sug_prev, "txt": txt[:100]})
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
        if txt == "1":
            # F134: si viene del botón post-cancelación, usar la especialidad de la cita cancelada
            _pce = data.pop("_post_cancel_esp", None)
            if _pce:
                save_session(phone, "IDLE", data)
            return await _iniciar_agendar(phone, data, _pce or None)
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

        # ── BUG-B FIX: handlers de confirmación de pivot last_esp_context ─────
        if tl == "confirma_pivot_esp":
            _pivot_esp = data.pop("pivot_esp_pendiente", None)
            data.pop("last_esp_context", None)
            data.pop("last_esp_context_ts", None)
            log_event(phone, "ctx_pivot_confirmado", {"esp": _pivot_esp})
            return await _iniciar_agendar(phone, data, _pivot_esp)

        if tl == "cambiar_esp":
            data.pop("pivot_esp_pendiente", None)
            data.pop("last_esp_context", None)
            data.pop("last_esp_context_ts", None)
            log_event(phone, "ctx_pivot_cancelado", {})
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

        # ── Respuestas de fidelización (escala NPS 1-5 + legacy 3-puntos) ─────
        if tl in _SEG_ID_MAP:
            categoria, rating = _SEG_ID_MAP[tl]
            # IMPORTANTE: obtener seguimiento ANTES de guardar respuesta
            # (get_ultimo_seguimiento busca respuesta IS NULL)
            seg = get_ultimo_seguimiento(phone)
            save_fidelizacion_respuesta(phone, "postconsulta", categoria)
            esp = seg.get("especialidad", "") if seg else ""
            prof = seg.get("profesional", "") if seg else ""

            if categoria == "mejor":
                # Anti-loop: si el paciente repite "Mejor" (escribe en vez de
                # tocar el botón), NO re-ofrecer el mismo upsell. Acusa recibo y
                # da salida (hallazgo auditoría: bot repetía el upsell de masoterapia).
                _ups_ts = data.get("upsell_postconsulta_ts")
                if _ups_ts:
                    try:
                        _prev = datetime.fromisoformat(_ups_ts)
                        if (datetime.now(timezone.utc) - _prev).total_seconds() < 1800:
                            return _btn_msg(
                                "¡Genial que te sientas bien! 😊 Cuando quieras "
                                "agendar tu control, escríbeme *agendar*.",
                                [{"id": "menu", "title": "🏠 Menú"}],
                            )
                    except Exception:
                        pass
                log_event(phone, "seguimiento_mejor",
                          {"especialidad": esp, "rating": rating})
                # Pide reseña Google solo a promotores (rating ≥ 4)
                try:
                    from resilience import spawn_task
                    spawn_task(_send_review_request_if_due(phone, esp, rating=rating))
                except Exception:
                    pass
                # Cross-sell inteligente según especialidad
                upsell = UPSELL_POSTCONSULTA.get(esp.lower()) if esp else None
                data["upsell_postconsulta_ts"] = datetime.now(timezone.utc).isoformat()
                if upsell:
                    upsell_msg, upsell_esp = upsell
                    data["upsell_especialidad"] = upsell_esp
                    save_session(phone, "IDLE", data)
                    log_event(phone, "upsell_postconsulta_ofrecido",
                              {"especialidad_origen": esp, "especialidad_destino": upsell_esp})
                    return _btn_msg(
                        f"Qué bueno saberlo 😊 Nos alegra que te sientas bien.\n\n{upsell_msg}",
                        [{"id": "upsell_si", "title": "Sí, me interesa"},
                         {"id": "no_control", "title": "No por ahora"}]
                    )
                save_session(phone, "IDLE", data)  # persistir upsell_postconsulta_ts (anti-loop)
                return _btn_msg(
                    "Qué bueno saberlo 😊 Nos alegra que te sientas bien.\n\n"
                    "¿Quieres agendar tu control de seguimiento?",
                    [{"id": "1", "title": "Sí, agendar control"},
                     {"id": "no_control", "title": "Por ahora no"}]
                )

            # Detractor (peor, rating 1-2) o neutro (igual, rating 3)
            log_event(phone, "seguimiento_negativo",
                      {"respuesta": categoria, "rating": rating, "especialidad": esp})
            # Alerta al doctor SOLO si es detractor (peor)
            if categoria == "peor" and ADMIN_ALERT_PHONE:
                perfil = get_profile(phone)
                nombre_pac = perfil["nombre"] if perfil else phone
                alerta = (
                    f"⚠️ *Alerta seguimiento*\n\n"
                    f"Paciente *{nombre_pac}* ({phone}) calificó {rating}/5 "
                    f"después de {esp} con {prof}.\n"
                    f"Revisar situación clínica."
                )
                log_event(phone, "seguimiento_alerta_peor",
                          {"especialidad": esp, "profesional": prof, "rating": rating})
                try:
                    from resilience import spawn_task
                    spawn_task(send_whatsapp(ADMIN_ALERT_PHONE, alerta))
                except Exception:
                    log.warning("No se pudo enviar alerta peor a %s", ADMIN_ALERT_PHONE)
                # ── Notif al profesional tratante (opt-in via dashboard) ───
                try:
                    import prof_notifications as _pn_pp
                    from medilink import PROFESIONALES as _PROFS_PP
                    _id_prof_pp = next(
                        (pid for pid, info in _PROFS_PP.items()
                         if info.get("nombre") == prof),
                        None
                    )
                    if _id_prof_pp:
                        from resilience import spawn_task as _spawn_pp
                        _spawn_pp(_pn_pp.notify_paciente_peor(
                            id_prof=_id_prof_pp,
                            profesional_nombre=prof,
                            paciente_nombre=nombre_pac,
                            paciente_phone=phone,
                            especialidad=esp,
                        ), name=f"prof_notif_peor_{_id_prof_pp}")
                except Exception as _pn_pp_err:
                    log.warning("prof_notif_paciente_peor falló: %s", _pn_pp_err)
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
        if tl == "wb_agendar":
            log_event(phone, "winback_btn_agendar", {})
            perfil = get_profile(phone)
            if perfil:
                data["rut_conocido"] = perfil.get("rut", "")
                data["nombre_conocido"] = perfil.get("nombre", "")
            # Precargar última especialidad si existe para acelerar flujo
            ultima = get_ultima_cita_paciente(phone)
            esp_wb = (ultima or {}).get("especialidad") or None
            return await _iniciar_agendar(phone, data, esp_wb)
        if tl == "wb_info":
            log_event(phone, "winback_btn_info", {})
            return await handle_message(phone, "menu", {"state": "IDLE", "data": data})
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
                "Entendido 😊 Cuando quieras retomar, escríbenos.\n"
                "_Escribe *menu* para volver al inicio._"
            )

        # ── Normalización texto libre → payload cross-sell ─────────────────
        # Si el paciente responde "Sí, me interesa" / "No, gracias" en texto libre
        # a un template de cross-sell, mapear al payload del botón correspondiente.
        # Caso real: paciente respondió "Sí, me interesa" al cross-sell kine y el
        # bot cayó al fallback genérico en vez de iniciar flujo de kine.
        _AFIRMATIVOS_CS = {"sí, me interesa", "si, me interesa", "sí me interesa",
                           "si me interesa", "me interesa", "sí me interesa.",
                           "si me interesa.", "si interesa"}
        _NEGATIVOS_CS = {"no, gracias", "no gracias", "no por ahora",
                         "no por ahora.", "no, gracias.", "no me interesa",
                         "no, no me interesa"}
        if (tl in _AFIRMATIVOS_CS or tl in _NEGATIVOS_CS) and tl not in ("xkine_si","xkine_no","xorlfono_si","xorlfono_no","xestetica_si","xestetica_info","xestetica_no","xmgcheck_si","xmgcheck_no","kine_adh_si","kine_adh_no","reac_si","reac_luego","wb_agendar","wb_info","upsell_si","no_control"):
            try:
                from session import db as _cs_conn
                with _cs_conn() as _ccs:
                    _row_cs = _ccs.execute(
                        "SELECT tipo FROM fidelizacion_msgs "
                        "WHERE phone=? AND tipo LIKE 'crosssell%' "
                        "  AND enviado_en >= datetime('now','-48 hours') "
                        "  AND (respuesta IS NULL OR respuesta='') "
                        "ORDER BY enviado_en DESC LIMIT 1",
                        (phone,)
                    ).fetchone()
                if _row_cs:
                    _tipo_cs = _row_cs[0]
                    _es_afirm = tl in _AFIRMATIVOS_CS
                    _MAP_CS = {
                        "crosssell_kine":           ("xkine_si", "xkine_no"),
                        "crosssell_orl_fono":       ("xorlfono_si", "xorlfono_no"),
                        "crosssell_odonto_estetica":("xestetica_si", "xestetica_no"),
                        "crosssell_mg_chequeo":     ("xmgcheck_si", "xmgcheck_no"),
                    }
                    if _tipo_cs in _MAP_CS:
                        tl = _MAP_CS[_tipo_cs][0 if _es_afirm else 1]
                        log_event(phone, "crosssell_freetext_normalizado",
                                  {"tipo": _tipo_cs, "payload": tl, "raw": txt[:80]})
            except Exception as _cs_err:
                log.warning("normalize crosssell freetext error: %s", _cs_err)

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
            # ITEM-20: si la última cita del paciente fue con Dr. Márquez (id=13),
            # el control debe reagendar con él (cobra $30.000, no $25.000).
            # Pasamos especialidad="medicina familiar" → _iniciar_agendar entra
            # por el path _ESP_MED_FAMILIAR que usa solo_ids=[13] y normaliza
            # los slots a "Medicina Familiar" → _precio_line ve id=13 → $30.000.
            _ult_cita_xch = get_ultima_cita_paciente(phone)
            _esp_xch = "medicina general"
            if _ult_cita_xch:
                _prof_xch = (_ult_cita_xch.get("profesional") or "").lower()
                if "márquez" in _prof_xch or "marquez" in _prof_xch:
                    _esp_xch = "medicina familiar"
                    log_event(phone, "xchequeo_forzado_marquez", {})
            return await _iniciar_agendar(phone, data, _esp_xch)
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
                    # Texto libre: si el paciente escribió "5" o "excelente",
                    # usamos rating 5 implícito para que el mensaje muestre 5⭐.
                    rating_libre = 5 if "5" in (txt or "") else 4
                    log_event(phone, "seguimiento_mejor",
                              {"especialidad": esp, "fuente": "texto_libre", "rating": rating_libre})
                    try:
                        from resilience import spawn_task
                        spawn_task(_send_review_request_if_due(phone, esp, rating=rating_libre))
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
            # "para hoy"/"para mañana"/"para el [día]" = scheduling intent, NO síntoma.
            # Visto 2026-04-27 (56964679269): "Medico para hoy tiene?" pasaba a
            # triage GES → matcheaba odontología por "medico" → bot ofrecía
            # Carlos Jiménez. Skip triage cuando hay marcador temporal explícito.
            "para hoy", "para manana", "para mañana", "para el lunes",
            "para el martes", "para el miercoles", "para el miércoles",
            "para el jueves", "para el viernes", "para el sabado",
            "para el sábado", "tiene hora", "tiene horita", "tiene medico",
            "tiene doctor", "hay hora", "hay horita", "hay medico",
            "hay doctor", "habra hora", "habrá hora", "tendra hora",
            "tendrá hora",
        )
        _skip_triage = any(k in tl for k in _TRIAGE_SKIP_KWS)
        # Fix H: crisis salud mental nunca debe pasar por triage GES bajo ninguna
        # circunstancia — el triage puede retornar especialidades incoherentes
        # (ej. "odontología") para textos de ideación suicida. El crisis check
        # en la línea ~2603 debería haberlo capturado, pero si la sesión fue
        # reseteada por un webhook duplicado y volvió a IDLE, puede llegar acá.
        _skip_triage_crisis = (
            any(p in tl_norm for p in SALUD_MENTAL_CRISIS)
            or any(pat.search(tl_norm) for pat in SALUD_MENTAL_PATRONES)
            or any(p in tl for p in SALUD_MENTAL_CRISIS)
            or any(pat.search(tl) for pat in SALUD_MENTAL_PATRONES)
        )
        if _skip_triage_crisis:
            # Re-ejecutar contención aquí como safety net — el crisis check
            # arriba ya la emitió en el mismo request pero no en los reenvíos.
            log_event(phone, "crisis_salud_mental_triage_guard", {"texto": txt[:240]})
            save_tag(phone, "crisis-salud-mental")
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
                    _triage_msg = (
                        f"Por lo que me cuentas, es importante que te evalúe "
                        f"un especialista en *{especialidad_triage}* pronto.\n\n"
                        "Te busco la hora más cercana disponible ahora mismo."
                    )
                    await send_whatsapp(phone, _triage_msg)
                    from session import log_message as _lm_f4
                    _lm_f4(phone, "out", _triage_msg, "IDLE")
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
        # BUG-B: nombre de pila inexistente con contexto de especialidad conocida → aclarar.
        # "pedro kine" / "kine pedro": no existe kine Pedro en el CMC.
        # Se evalúa ANTES del detector de apellidos para que no caiga silenciosamente
        # a mostrar ambos kines sin aclarar.
        _norm_bugb = _normalizar_para_apellido_ws(txt)
        _tiene_kine_ctx_b = any(k in tl for k in ("kine", "kinesiolog", "kinesio"))
        _SELF_INTRO_BUGB = ("soy ", "me llamo", "mi nombre es", "yo soy")
        if (_tiene_kine_ctx_b
                and re.search(r"\bpedro\b", _norm_bugb)
                and not any(s in tl for s in _SELF_INTRO_BUGB)):
            log_event(phone, "nombre_inexistente_kine_pedro", {"txt": txt[:80]})
            return _btn_msg(
                "No tenemos ningún kinesiólogo llamado Pedro en el CMC.\n\n"
                "¿A quién buscas?",
                [
                    {"id": "prof_armijo",    "title": "Luis Armijo (Kine)"},
                    {"id": "prof_etcheverry","title": "Leonardo Etcheverry (Kine)"},
                    {"id": "menu_volver",    "title": "Ver otras opciones"},
                ]
            )

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
        # Procedimientos dentales específicos: solo disparar shortcut de agendar
        # si el texto contiene verbo explícito de cita ("hora", "agendar", "reservar").
        # "quiero una tapadura" sin "hora/agendar" es intent=info (FAQ de precio/tratamiento),
        # no intent=agendar. Sin esta exclusión, _detectar_especialidad_en_texto mapea
        # "tapadura" → odontología y el shortcut bypasea detect_intent.
        _PROCEDIMIENTOS_DENTALES = (
            "tapadura", "limpieza dental", "limpieza de dientes", "limpieza bucal",
            "limpieza de boca", "destartraje", "detartraje", "profilaxis dental",
            "sarro", "blanqueamiento dental", "blanqueamiento", "blanqueo dental",
            "resina dental",
        )
        _es_proc_dental = any(p in tl for p in _PROCEDIMIENTOS_DENTALES)
        _tiene_verbo_cita_explicito = any(k in tl for k in ("hora", "agendar", "reservar"))
        if _esp_idle and any(
            k in tl for k in (
                "hora", "agendar", "reservar", "necesito", "quiero",
                "tiene alguna", "tendra", "tendrá", "tendrán",
            )
        ) and not _ES_PREGUNTA_INFO and not (_es_proc_dental and not _tiene_verbo_cita_explicito):
            log_event(phone, "intent_detectado_local", {"esp": _esp_idle})
            data["_txt_raw"] = txt
            # Si la especialidad detectada es "medicina general" pero el texto
            # original mencionaba "pediatra/pediatría", marcar para que
            # _iniciar_agendar muestre el saludo_prefix explicativo.
            _PEDIATRIA_ALIAS = (
                "pediatra", "pediatría", "pediatria", "pediátrico",
                "pediatrico", "medico infantil", "médico infantil",
                "doctor infantil", "medico para niños", "médico para niños",
                "doctor para niños", "medico para ninos",
            )
            if _esp_idle == "medicina general" and any(k in tl_norm for k in _PEDIATRIA_ALIAS):
                data["_pediatra_a_mg"] = True
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
                data["especialidad_sugerida_ts"] = datetime.now(timezone.utc).isoformat()
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
        # pregunta por disponibilidad/cupos/hora, mostrar botones de confirmación.
        # BUG-B FIX: antes el bot redirigía silenciosamente a agendar, lo que
        # confundía a pacientes que habían cambiado de tema.
        _ctx_esp = data.get("last_esp_context")
        _ctx_ts = data.get("last_esp_context_ts")
        if _ctx_esp and _ctx_ts:
            try:
                from datetime import datetime as _dt_ctx2
                _ts_ctx2 = _dt_ctx2.fromisoformat(_ctx_ts)
                if _ts_ctx2.tzinfo is None:  # FIX-9: naive guard
                    _ts_ctx2 = _ts_ctx2.replace(tzinfo=timezone.utc)
                _edad = _dt_ctx2.now(timezone.utc) - _ts_ctx2
                if _edad < timedelta(minutes=5):
                    _pivot_kw = ("cupo", "cupos", "disponib", "hora dispon",
                                 "horario", "cuando hay", "cuándo hay",
                                 "hay hora", "para cuando", "para cuándo",
                                 "en que horario", "en qué horario")
                    if any(k in tl for k in _pivot_kw):
                        log_event(phone, "ctx_pivot_confirmacion", {"esp": _ctx_esp})
                        data["pivot_esp_pendiente"] = _ctx_esp
                        save_session(phone, state, data)
                        _esp_display = _ctx_esp.capitalize()
                        return _btn_msg(
                            f"¿Querías agendar *{_esp_display}*?",
                            [
                                {"id": "confirma_pivot_esp", "title": "Sí, agendar"},
                                {"id": "cambiar_esp",        "title": "Otra especialidad"},
                            ]
                        )
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

        # Obtener referral Meta fresco (anuncio Click-to-WA/IG/FB) si existe
        _meta_referral_ctx: dict | None = None
        try:
            from session import get_meta_referral_fresh as _get_ref
            _meta_referral_ctx = _get_ref(phone, ttl_horas=24)
        except Exception:
            pass

        # ── Pre-check: retiro de informe, preparación de examen, laboratorio ──
        # Estas intenciones no tienen especialidad que agendar — no deben llegar
        # a detect_intent ni caer en el loop "¿qué especialidad necesitas?".
        # Datos usados: solo teléfono de recepción (CMC_TELEFONO, CMC_TELEFONO_FIJO)
        # que ya existen en el módulo. NO se inventan plazos ni instrucciones
        # de preparación (no están en el código) — se deriva a recepción.
        _tl_pre = tl_norm
        _es_retiro = any(k in _tl_pre for k in (
            "retiro informe", "retirar informe", "buscar informe",
            "retirar resultado", "retiro resultado", "buscar resultado",
            "retirar mi informe", "buscar mi informe",
            "retiro de informe", "retiro de resultado", "retirar mi resultado",
            "informe listo", "resultado listo", "ya esta mi informe",
            "ya esta mi resultado", "cuando puedo retirar", "cuando retiro",
            "cuando busco mi informe", "cuando busco el informe",
            "pasaron los dias", "pasaron los dias y no", "ya paso",
        ))
        _es_prep_examen = any(k in _tl_pre for k in (
            "preparacion para", "preparacion de", "como prepararme",
            "que debo hacer antes", "ayuno", "en ayunas", "vejiga llena",
            "preparacion ecografi", "como es la preparacion",
            "como vengo para", "debo venir en ayunas", "tengo que venir en ayunas",
        ))
        _es_laboratorio = any(k in _tl_pre for k in (
            "toma de muestra", "toma de sangre", "examen de sangre",
            "examen de orina", "hemograma", "glucosa en ayuno",
            "perfil lipidico", "perfil bioquimico", "hacer examenes",
            "hacerme examenes", "examenes de laboratorio", "pedir examenes",
            "solicitar examenes",
        ))
        # Guard: si el mensaje TAMBIÉN contiene intención de agendar/ver especialidad,
        # no interrumpir con el mensaje de derivación — dejar que detect_intent lo maneje.
        _tiene_intent_agendar = any(k in _tl_pre for k in (
            "hora", "agendar", "cita", "reservar", "quiero ver", "necesito ver",
            "consulta", "medico", "médico", "doctor", "ecografia", "ecografía",
            "eco", "cardiolog", "ginecolog", "traumatol", "nutrici", "psicolog",
            "kinesi", "odontol", "otorrinol", "gastroenterol", "fonoaudiolog",
            "matrona", "podolog", "masoterapia", "endodoncia", "ortodoncia",
            "implantol", "estetica", "estética",
        ))
        if _es_retiro and not _tiene_intent_agendar:
            log_event(phone, "intent_retiro_informe", {"txt": txt[:120]})
            reset_session(phone)
            return (
                "Para el retiro de informes o resultados, comunícate con recepción:\n\n"
                f"📞 *{CMC_TELEFONO}*\n"
                f"☎️ *{CMC_TELEFONO_FIJO}*\n\n"
                "Horario de atención: lun-vie 08:00-21:00 · sáb 09:00-14:00."
            )
        if _es_prep_examen and not _tiene_intent_agendar:
            log_event(phone, "intent_preparacion_examen", {"txt": txt[:120]})
            reset_session(phone)
            return (
                "Las indicaciones de preparación varían según el tipo de examen.\n\n"
                "Para confirmarlo, consulta con recepción:\n\n"
                f"📞 *{CMC_TELEFONO}*\n"
                f"☎️ *{CMC_TELEFONO_FIJO}*\n\n"
                "Horario: lun-vie 08:00-21:00 · sáb 09:00-14:00."
            )
        if _es_laboratorio and not _tiene_intent_agendar:
            # El CMC no realiza toma de muestras de laboratorio. El médico
            # puede dar la orden para hacerlos en un laboratorio externo.
            log_event(phone, "intent_laboratorio_externo", {"txt": txt[:120]})
            save_demanda_no_disponible(phone, "laboratorio/toma de muestras", "servicio")
            reset_session(phone)
            return (
                "El CMC no realiza toma de muestras de laboratorio directamente.\n\n"
                "Nuestros médicos pueden darte la *orden médica* para que te los hagas "
                "en un laboratorio cercano.\n\n"
                "Si necesitas una consulta para solicitar exámenes, escribe *agendar* "
                "o llama a recepción:\n\n"
                f"📞 *{CMC_TELEFONO}*\n"
                f"☎️ *{CMC_TELEFONO_FIJO}*"
            )

        result = await detect_intent(txt, recepcion_resumen=_recepcion_resumen,
                                     meta_referral=_meta_referral_ctx)
        intent = result.get("intent", "otro")
        log_event(phone, "intent_detectado", {"intent": intent, "esp": result.get("especialidad")})

        # ── Guard post-takeover: si la recepcionista ya agendó manualmente,
        # bloquear intent=agendar para que el bot no re-inicie ese flujo.
        if intent == "agendar" and _recepcion_resumen:
            _AGENDAR_MANUAL_KWS = ("agendé", "agende", "le agendé", "le agende",
                                   "quedó agendado", "quedo agendado", "tiene hora",
                                   "le saqué hora", "le saque hora", "ya tiene cita",
                                   "ya quedó", "ya quedo")
            _rc_lower = " ".join(_recepcion_resumen).lower()
            if any(kw in _rc_lower for kw in _AGENDAR_MANUAL_KWS):
                log_event(phone, "agendar_bloqueado_por_ctx_recepcion", {})
                intent = "info"
                result["intent"] = "info"
                result["respuesta_directa"] = (
                    "Según lo que te indicó la recepcionista, ya quedaste agendado. "
                    "Si tienes dudas sobre tu hora, escribe *menu* o llama al "
                    f"*{CMC_TELEFONO_FIJO}*."
                )

        # ── BUG-2: intent=otro post-bot_reanudado → human takeover silencioso ─────
        # Caso real: "Sergio abonará 55.000", "Al nombre de Thomas pezo peña",
        # "Buenos sias" → primer mensaje tras bot_reanudado cae en menú genérico.
        # Fix: si el último evento bot_reanudado fue hace <30 min y intent=otro,
        # derivar silenciosamente a HUMAN_TAKEOVER (el paciente habla con recepción).
        if intent == "otro":
            try:
                from session import _conn as _conn2
                _c2 = _conn2()
                _row2 = _c2.execute(
                    "SELECT ts FROM conversation_events "
                    "WHERE phone=? AND event='bot_reanudado' "
                    "ORDER BY ts DESC LIMIT 1",
                    (phone,),
                ).fetchone()
                _c2.close()
                if _row2:
                    from datetime import datetime as _dt2
                    _ts2 = _dt2.fromisoformat(_row2[0])
                    if _ts2.tzinfo is None:
                        _ts2 = _ts2.replace(tzinfo=timezone.utc)
                    _secs2 = (datetime.now(timezone.utc) - _ts2).total_seconds()
                    if _secs2 < 1800:  # 30 min
                        log_event(phone, "otro_post_reanudado", {"txt": txt[:120], "secs": int(_secs2)})
                        save_session(phone, "HUMAN_TAKEOVER", data)
                        return (
                            "Te paso a recepción 🙋\n"
                            "_(Puedes seguir escribiendo, aquí lo verán.)_"
                        )
            except Exception as _e2:
                log.warning("BUG-2 check falló: %s", _e2)

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
            # BUG-C FIX: solo redirigir a last_esp_context si el mensaje es
            # claramente afirmativo. Antes el bot redirigía con CUALQUIER mensaje
            # clasificado como "menu" (incluyendo "gracias", "chao", etc.).
            _last_esp_a = data.get("last_esp_context")
            _last_ts_a  = data.get("last_esp_context_ts")
            _CONTINUAR_KW = ("si", "sí", "agendar", "ok", "dale", "claro",
                             "perfecto", "vamos", "quiero", "queria", "quería")
            _DESPEDIDA_KW = ("gracias", "chao", "adios", "adiós", "bye",
                             "nada", "ya está", "ya esta", "listo", "muchas gracias",
                             "mil gracias", "ok gracias", "perfecto gracias",
                             "listo gracias", "ya gracias")
            _tl_rescue = tl.strip().lower()
            _es_continuar = any(
                _tl_rescue == k or _tl_rescue.startswith(k + " ")
                for k in _CONTINUAR_KW
            )
            _es_despedida = any(k in _tl_rescue for k in _DESPEDIDA_KW)
            if _last_esp_a and _last_ts_a and _es_continuar and not _es_despedida:
                try:
                    from datetime import datetime as _dt_a
                    _t_a = _dt_a.fromisoformat(_last_ts_a)
                    if (datetime.now(timezone.utc) - _t_a).total_seconds() < 300:
                        log_event(phone, "menu_redir_esp_context", {"esp": _last_esp_a})
                        return await _iniciar_agendar(phone, data, _last_esp_a)
                except (ValueError, TypeError):
                    pass
            if _es_despedida:
                log_event(phone, "despedida_detectada", {"tl": _tl_rescue})
                return _btn_msg(
                "Listo, fue un gusto ayudarte 😊",
                [{"id": "menu", "title": "🏠 Volver al inicio"}]
            )
            _pf_idle = get_profile(phone)
            _nm_idle = _first_name((_pf_idle or {}).get("nombre", "")) if _pf_idle else ""
            return _menu_msg(nombre=_nm_idle)

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
            # BUG-E: heredar last_esp_context cuando intent=agendar sin especialidad.
            # Caso real: paciente preguntó "¿cuánto cuesta la cardiología?" (info),
            # bot guardó last_esp_context=cardiología. Siguiente msg: "agendar".
            # Claude no extrae especialidad → bot iba a WAIT_ESPECIALIDAD a pesar de
            # tener contexto fresco. 56% de los casos con esp=null ignoraban el contexto.
            if not especialidad:
                _last_esp_e = data.get("last_esp_context")
                _last_ts_e = data.get("last_esp_context_ts")
                if _last_esp_e and _last_ts_e:
                    try:
                        from datetime import datetime as _dt_e
                        _t_e = _dt_e.fromisoformat(_last_ts_e)
                        if _t_e.tzinfo is None:
                            _t_e = _t_e.replace(tzinfo=timezone.utc)
                        if (datetime.now(timezone.utc) - _t_e).total_seconds() < 300:
                            especialidad = _last_esp_e
                            data.pop("last_esp_context", None)
                            data.pop("last_esp_context_ts", None)
                            log_event(phone, "intent_agendar_esp_heredado",
                                      {"esp": especialidad, "age_s": int(
                                          (datetime.now(timezone.utc) - _t_e).total_seconds()
                                      )})
                    except (ValueError, TypeError):
                        pass
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
                    # FIX 5: buscar el próximo slot disponible y mostrarlo directamente.
                    # Regla Meta-referral: ofrecer la hora concreta, no preguntar vago.
                    _qb_smart, _qb_todos = [], []
                    _qb_slot = None
                    try:
                        _prof_id_qb = (ultima or {}).get("id_profesional")
                        if _prof_id_qb:
                            _qb_smart, _qb_todos = await buscar_primer_dia(
                                esp_ultima.lower(), solo_ids=[int(_prof_id_qb)]
                            )
                        if not _qb_todos:
                            _qb_smart, _qb_todos = await buscar_primer_dia(esp_ultima.lower())
                        _qb_slot = (_qb_smart[0] if _qb_smart else (_qb_todos[0] if _qb_todos else None))
                    except Exception as _e_qb:
                        log.debug("quick_book buscar_primer_dia falló: %s", _e_qb)

                    save_session(phone, "WAIT_QUICK_BOOK", data)
                    log_event(phone, "quick_book_offered", {
                        "especialidad": esp_ultima,
                        "esp_claude": especialidad or None,
                        "slot_encontrado": bool(_qb_slot),
                    })
                    nombre_corto = _first_name(perfil.get("nombre"))
                    saludo = f"¡Hola de nuevo, *{nombre_corto}*! ⚡\n\n" if nombre_corto else "⚡ "
                    con_prof = f" con *{prof_ultima}*" if prof_ultima else ""
                    if _qb_slot:
                        # Mostrar hora concreta
                        _qb_fecha = _qb_slot.get("fecha_display") or _qb_slot.get("fecha", "")
                        _qb_hora = (_qb_slot.get("hora_inicio") or "")[:5]
                        _qb_prof_display = _qb_slot.get("profesional") or prof_ultima
                        _con_prof_display = f" con *{_qb_prof_display}*" if _qb_prof_display else con_prof
                        data["slot_quick_book"] = _qb_slot
                        save_session(phone, "WAIT_QUICK_BOOK", data)
                        return _btn_msg(
                            f"{saludo}Encontré una hora disponible de *{esp_ultima}*{_con_prof_display}:\n\n"
                            f"📅 *{_qb_fecha}*  🕐 *{_qb_hora}*\n\n"
                            f"¿Te la reservo?",
                            [
                                {"id": "quick_yes", "title": "✅ Sí, reservar"},
                                {"id": "quick_other", "title": "🔄 Otra especialidad"},
                                {"id": "quick_cancel", "title": "✋ Ahora no"},
                            ]
                        )
                    # Sin slot disponible → fallback al flujo vago original
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
            data["_txt_raw"] = txt

            # Patrón 4 FIX (2026-05-19): paciente activo en tratamiento de
            # ortodoncia -> ofrecer menú especial en vez de flujo estándar.
            # Evidencia: 4 phones activos escribieron 1-3 veces sin resultado
            # (reimpresión boletas, pago control, próxima cita, retiro brackets).
            _esp_orto_activo = (especialidad or "").lower().strip()
            _keywords_orto = ("ortodoncia", "bracket", "frenillo", "ortodonc", "control")
            _txt_orto = tl_norm
            _es_posible_activo = (
                _esp_orto_activo in ("ortodoncia", "brackets")
                or any(k in _txt_orto for k in _keywords_orto)
            )
            if _es_posible_activo and perfil and perfil.get("rut"):
                _cnt_orto = await _paciente_ortodoncia_activo(phone)
                if _cnt_orto > 0:
                    log_event(phone, "ortodoncia_activo_menu_ofrecido",
                              {"rut": perfil["rut"], "atenciones_6m": _cnt_orto})
                    save_session(phone, "WAIT_ORTODONCIA_ACTIVO", data)
                    nombre_orto = _first_name(perfil.get("nombre", ""))
                    saludo_orto = f"Hola *{nombre_orto}* " if nombre_orto else "Hola "
                    return _list_msg(
                        body_text=(
                            f"{saludo_orto}— como paciente de ortodoncia con la Dra. Castillo, "
                            "¿en qué te podemos ayudar?"
                        ),
                        button_label="Ver opciones",
                        sections=[{
                            "title": "Ortodoncia",
                            "rows": [
                                {"id": "orto_agendar",  "title": "Agendar hora/control"},
                                {"id": "orto_ver_cita", "title": "Mi próxima cita"},
                                {"id": "orto_boleta",   "title": "Reimpresión de boleta"},
                                {"id": "orto_urgencia", "title": "Bracket suelto / urgencia"},
                            ],
                        }],
                    )

            # Si Claude detectó pediatría y el normalizador la mapeó a MG,
            # marcar para que _iniciar_agendar muestre aclaración.
            if (especialidad or "").lower().strip() in (
                "pediatría", "pediatria", "pediatra",
                "médico infantil", "medico infantil",
            ):
                data["_pediatra_a_mg"] = True
                especialidad = "medicina general"
            return await _iniciar_agendar(phone, data, especialidad)

        if intent == "reagendar":
            return await _iniciar_reagendar(phone, data)

        if intent == "cancelar":
            # BUG-4 FIX: multi-intent cancelar+agendar — guardar intent secundario
            if isinstance(result, dict) and result.get("multi_intent_pendiente"):
                data["_intent_pendiente"] = result["multi_intent_pendiente"]
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
                            f"📍 {_CMC_DIRECCION}.\n"
                            f"_Llega 15 min antes con tu cédula._"
                        )
                    return (
                        "No veo una cita tuya para hoy 🤔\n\n"
                        "¿Quieres que te muestre tus próximas citas? Escribe *ver mis citas*."
                    )

        if intent == "waitlist":
            especialidad = result.get("especialidad")
            return await _iniciar_waitlist(phone, data, especialidad)

        # ── Intent telemedicina ───────────────────────────────────────────────
        if intent == "telemedicina":
            from config import TELEMEDICINA_ENABLED
            if not TELEMEDICINA_ENABLED:
                log_event(phone, "telemedicina_pedida_pausada", {"texto": txt[:120]})
                return _txt(
                    "Por ahora atendemos solo de forma *presencial* en el centro 🏥\n\n"
                    f"📍 {_CMC_DIRECCION}\n"
                    "🕐 Lun-Vie 08:00-21:00 · Sáb 09:00-14:00\n\n"
                    "Si quieres agendar una hora presencial, escribe *agendar*."
                )
            save_session(phone, "WAIT_TELEMEDICINA_ESPECIALIDAD", data)
            return _btn_msg(
                "Sí, ofrecemos atención por videollamada en algunas especialidades:\n\n"
                "✅ Medicina General — controles y recetas crónicas\n"
                "✅ Psicología — sesiones de seguimiento\n"
                "✅ Nutrición — controles\n"
                "✅ Cardiología — interpretación de exámenes\n\n"
                "La primera consulta siempre debe ser presencial (excepto Medicina General).\n\n"
                "¿Para qué especialidad necesitas la videollamada?",
                [
                    {"id": "tele_mg",     "title": "Medicina General"},
                    {"id": "tele_psico",  "title": "Psicología"},
                    {"id": "tele_nutri",  "title": "Nutrición"},
                    {"id": "tele_otro",   "title": "Otra especialidad"},
                ]
            )

        # ── Intent referido: paciente pide su código para compartir ──────────
        _REFERIDO_KW = ("referir", "referido", "codigo referido", "código referido",
                        "trae amigo", "traer amigo", "recomendar amigo", "invitar amigo",
                        "mi codigo", "mi código", "quiero referir")
        if any(kw in tl_norm for kw in _REFERIDO_KW) or intent == "referido":
            from config import REFERRAL_BONOS_ENABLED
            if not REFERRAL_BONOS_ENABLED:
                log_event(phone, "referido_pedido_pausado", {"texto": txt[:120]})
                return _txt(
                    "Gracias por querer recomendarnos. Estamos terminando de "
                    "definir los detalles del programa de referidos y lo "
                    "habilitaremos pronto.\n\n"
                    "Mientras tanto, si quieres que un familiar o amigo agende, "
                    "puede escribirnos directamente a este WhatsApp."
                )
            from session import generate_referral_code, get_referral_code
            _existing = get_referral_code(phone)
            _code = _existing or generate_referral_code(phone)
            _desc_medica = "20% de descuento en tu próxima consulta médica"
            _desc_dental = "15% de descuento en tu próxima atención dental"
            reset_session(phone)
            return (
                f"Tu *código de referido* es: *{_code}*\n\n"
                "Compártelo con amigos o familiares. "
                "Cuando agenden su *primera cita* en el CMC usando tu código, "
                "tú recibes:\n\n"
                f"• {_desc_medica}\n"
                f"• {_desc_dental}\n\n"
                "Puedes acumular hasta *3 referidos por mes*.\n\n"
                "_Escribe *menu* si necesitas algo más._"
            )

        if intent == "consulta_farmaco":
            # Consulta sobre medicación/fármaco — derivar SIEMPRE a humano.
            # Independiente del contexto: nunca aconsejar sobre dosis, cambios,
            # interacciones ni efectos adversos.
            log_event(phone, "consulta_farmaco_derivada", {"texto": txt[:240]})
            return _derivar_humano(
                phone=phone,
                contexto=txt,
                takeover_reason="farmaco",
            )

        # FIX 5a (2026-06-10): aviso de atraso — responder con confirmación simple
        # + log_event para que aparezca en el panel, sin derivar a humano completo.
        # También capturar por regex en caso de que Claude no devuelva el intent.
        _ATRASO_RE = re.compile(
            r"(llego?\s+tarde|llegaré\s+tarde|llegare\s+tarde"
            r"|voy\s+(atrasad[oa]|demorad[oa]|en\s+camino)"
            r"|me\s+(atrasé|atrase|demoré|demore)"
            r"|llegaré?\s+(con\s+retraso|unos?\s+\d+\s+min)"
            r"|estoy\s+(en\s+camino|de\s+camino))",
            re.IGNORECASE,
        )
        if intent == "aviso_atraso" or _ATRASO_RE.search(tl):
            log_event(phone, "aviso_atraso", {"texto": txt[:200]})
            # Notificar a recepción via log (aparece en panel como evento)
            log_event(phone, "recepcion_pendiente", {"tipo": "aviso_atraso", "texto": txt[:200]})
            return (
                "¡Gracias por avisar! Le informamos a recepción 🙂\n\n"
                "Si tienes alguna otra consulta, escribe *menu*."
            )

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
            # Fix Carolina 2026-06-30: si la pregunta de disponibilidad no nombra
            # especialidad ("¿en qué horario?", "¿y después del 9 de julio?"),
            # hereda la recién ofrecida (last_esp_context, TTL 5min) en vez de caer
            # al fallback "dime qué especialidad" que rompía el hilo.
            if not especialidad:
                _le = data.get("last_esp_context")
                _le_ts = data.get("last_esp_context_ts")
                if _le and _le_ts:
                    try:
                        _t = datetime.fromisoformat(_le_ts)
                        if _t.tzinfo is None:
                            _t = _t.replace(tzinfo=timezone.utc)
                        if (datetime.now(timezone.utc) - _t).total_seconds() < 300:
                            especialidad = _le
                            log_event(phone, "disp_esp_heredada", {"esp": _le})
                    except (ValueError, TypeError):
                        pass
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
                    data["especialidad_sugerida_ts"] = datetime.now(timezone.utc).isoformat()
                    # Fix Carolina 2026-06-30: además de la sugerencia, sembrar
                    # last_esp_context (TTL 5min) para que un follow-up vago en el
                    # siguiente turno ("¿en qué horario?", "¿y después del 9?")
                    # herede la especialidad ya ofrecida en vez de perder el hilo.
                    data["last_esp_context"] = especialidad.lower()
                    data["last_esp_context_ts"] = datetime.now(timezone.utc).isoformat()
                    # Si es ecografía, persistir el órgano del texto original para
                    # que el click en "Sí, agendar" no re-pregunte el tipo (menu-loop).
                    try:
                        from ecografias import texto_menciona_ecografia as _tme_disp
                        if _tme_disp(txt):
                            data["eco_tipo_text"] = txt
                    except Exception:  # noqa: BLE001
                        pass
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

            # ── Hereda contexto reciente si la pregunta actual NO menciona
            # especialidad. Caso real fb_36265734933013648 2026-05-03:
            # 1) "hacen ecomamaria" → bot guarda last_esp_context=ecografía
            # 2) 2min después "Y la hacen por Fonasa?" → esp=null en detect
            # Sin este fix, el bot daba respuesta genérica sin contexto.
            txt_enriquecido = txt
            if not _esp_ctx:
                _last_esp = data.get("last_esp_context")
                _last_ts = data.get("last_esp_context_ts")
                if _last_esp and _last_ts:
                    try:
                        from datetime import datetime as _dt_check
                        _t = _dt_check.fromisoformat(_last_ts)
                        _age = (datetime.now(timezone.utc) - _t).total_seconds()
                        if _age < 300:  # 5 min
                            _esp_ctx = _last_esp
                            txt_enriquecido = f"{txt} (sobre {_last_esp})"
                            log_event(phone, "esp_context_heredado",
                                      {"esp": _last_esp, "age_s": int(_age)})
                    except (ValueError, TypeError):
                        pass

            if _esp_ctx:
                from datetime import datetime as _dt_ctx
                data["last_esp_context"] = _esp_ctx
                data["last_esp_context_ts"] = _dt_ctx.now(timezone.utc).isoformat()
                save_session(phone, "IDLE", data)
            # Respuesta DETERMINÍSTICA para preguntas sobre un tipo de ecografía
            # (qué es / para qué sirve / preparación). Más precisa que Claude y sin
            # riesgo de inventar; base en ecografias.ECO_INFO. Cae a FAQ si no aplica.
            _eco_info_resp = None
            _eco_offer_book = True   # info de tipo → ofrecer agendar; prep (ya agendó) → no
            try:
                from ecografias import info_ecografia as _info_eco_fn
                _eco_info_resp = _info_eco_fn(txt)
            except Exception:  # noqa: BLE001
                _eco_info_resp = None
            # Ayuno/preparación: si pregunta eso y tiene una ECO agendada, dar la
            # preparación de eco (caso real: agendó eco y preguntó "cuántas horas de
            # ayuno" → antes respondía genérico). Solo si no detectó ya un tipo concreto.
            if not _eco_info_resp:
                _txt_low_ayuno = (txt or "").lower()
                _AYUNO_KW = ("ayuno", "ayunas", "ayunar", "en ayunas", "preparacion",
                             "preparación", "preparo", "prepararme", "me preparo")
                if any(k in _txt_low_ayuno for k in _AYUNO_KW):
                    try:
                        from session import db as _c_eco_ayuno
                        with _c_eco_ayuno() as _cn_ay:
                            _row_eco = _cn_ay.execute(
                                "SELECT 1 FROM citas_bot WHERE phone=? AND "
                                "(especialidad LIKE '%cograf%' OR especialidad LIKE '%cotomograf%') "
                                "AND fecha>=date('now','-1 day') ORDER BY id DESC LIMIT 1",
                                (phone,)).fetchone()
                        if _row_eco:
                            from ecografias import prep_ayuno_eco as _prep_eco_fn
                            _eco_info_resp = _prep_eco_fn()
                            _eco_offer_book = False   # ya tiene la eco agendada
                            log_event(phone, "eco_prep_ayuno_respondida", {})
                    except Exception:  # noqa: BLE001
                        pass
            if _eco_info_resp:
                resp = _eco_info_resp + ("\n\n¿Quieres que te la agende? Responde *sí* 😊"
                                         if _eco_offer_book else "")
                log_event(phone, "eco_info_respondida", {"txt": txt[:80]})
                # Persistir el órgano de la eco para que, si el paciente acepta
                # ("sí" o botón "✅ Sí, agendar"), _iniciar_agendar lo recupere y
                # NO vuelva a preguntar el tipo (menu-loop). Solo cuando se ofrece
                # agendar (no en prep/ayuno, donde la eco ya está agendada).
                if _eco_offer_book:
                    data["eco_tipo_text"] = txt
            else:
                resp = result.get("respuesta_directa") or await respuesta_faq(txt_enriquecido, recepcion_resumen=_recepcion_resumen, meta_referral=_meta_referral_ctx)
            resp = _strip_canal_circular(resp, phone)  # BUG-F
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
                    elif esp_lower in _ESP_MED_FAMILIAR:
                        _smart, _todos = await buscar_primer_dia("medicina general", solo_ids=_MED_FAMILIAR_IDS)
                        mejor = _todos[0] if _todos else None
                        if mejor:
                            mejor["especialidad"] = "Medicina Familiar"
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
                    data["especialidad_sugerida_ts"] = datetime.now(timezone.utc).isoformat()
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
                    data["especialidad_sugerida_ts"] = datetime.now(timezone.utc).isoformat()
                    save_session(phone, "IDLE", data)
                    return _btn_msg(
                        f"{resp}\n\n¿Te agendo en *{esp_sug}*?",
                        [
                            {"id": "agendar_sugerido", "title": "✅ Sí, agendar"},
                            {"id": "no_agendar",      "title": "No por ahora"},
                        ]
                    )
            # M5: marcar sesion para follow-up proactivo a los 10 min.
            # Solo cuando NO tenemos esp_sug (ya se ofrecio agendar arriba con boton).
            from datetime import datetime as _dt_m5
            data["followup_info_ts"] = _dt_m5.now(timezone.utc).isoformat()
            data["followup_info_esp"] = (result.get("especialidad") or "").strip()
            data["followup_info_sent"] = False
            save_session(phone, "IDLE", data)
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
            # BUG-C: guardar last_esp_context si Claude detectó especialidad,
            # para que el siguiente mensaje de seguimiento herede el contexto.
            _esp_ctx_otro = result.get("especialidad")
            if _esp_ctx_otro:
                from datetime import datetime as _dt_o
                data["last_esp_context"] = _esp_ctx_otro
                data["last_esp_context_ts"] = _dt_o.now(timezone.utc).isoformat()
                save_session(phone, "IDLE", data)
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
                    faq_resp = await respuesta_faq(txt, recepcion_resumen=_recepcion_resumen)
                    faq_resp = _strip_canal_circular(faq_resp, phone)  # BUG-F
                    if faq_resp and len(faq_resp) > 20:
                        log_event(phone, "fallback_faq", {"txt": txt[:120]})
                        return f"{faq_resp}\n\n_Escribe *menu* si prefieres ver las opciones._"
                except Exception:
                    pass
        # Fallback final (saludo o input muy corto) → mostrar menú
        _pf_fb = get_profile(phone)
        _nm_fb = _first_name((_pf_fb or {}).get("nombre", "")) if _pf_fb else ""
        return _menu_msg(nombre=_nm_fb)

    # ── WAIT_BIA_SCREENING ────────────────────────────────────────────────────
    # Tamizaje de seguridad de la bioimpedanciometría. Contraindicada por los
    # fabricantes del equipo en marcapasos/DAI, y no se realiza en embarazo.
    # Si el paciente declara alguno, NO se agenda: se deriva a recepción.
    if state == "WAIT_BIA_SCREENING":
        _bia_si = (
            tl == "bia_si_riesgo"
            or tl in ("si", "sí", "sip", "yes", "tengo", "estoy embarazada", "embarazada")
            or any(k in tl for k in ("marcapaso", "desfibrilador", "embaraz", "encinta"))
        )
        _bia_no = (
            tl == "bia_no_riesgo"
            or tl in ("no", "nop", "ninguno", "ninguna", "no tengo", "nada")
        )
        if _bia_si:
            log_event(phone, "bia_screening_bloqueado", {"txt": txt[:120]})
            save_tag(phone, "bia-contraindicada")
            reset_session(phone)
            return (
                "Gracias por avisar 🙏\n\n"
                "Con *marcapasos*, *desfibrilador implantado* o en *embarazo* no "
                "realizamos la bioimpedanciometría: los fabricantes del equipo lo "
                "contraindican, y en el embarazo además el resultado no sería confiable.\n\n"
                "Igual podemos ayudarte: la *nutricionista Gisela Pinto* puede hacerte "
                "una evaluación nutricional completa sin este examen "
                "(consulta $20.000 particular · *bono Fonasa $4.770*).\n\n"
                "¿Quieres que te dé hora con ella? Responde *nutrición*.\n"
                "Si tienes dudas, escribe *recepción* y te contactamos."
            )
        if _bia_no:
            log_event(phone, "bia_screening_ok", {})
            data["_bia_screening_ok"] = True
            return await _iniciar_agendar(phone, data, "bioimpedanciometría")
        save_session(phone, "WAIT_BIA_SCREENING", data)
        return _btn_msg(
            "Perdón, no te entendí. Para poder darte hora necesito saberlo:\n\n"
            "¿Tienes *marcapasos*, *desfibrilador implantado* u otro dispositivo "
            "médico electrónico implantado, o estás *embarazada*?",
            [
                {"id": "bia_no_riesgo", "title": "No, ninguno"},
                {"id": "bia_si_riesgo", "title": "Sí"},
            ]
        )

    # ── WAIT_DURACION_MASOTERAPIA ──────────────────────────────────────────────
    # ── WAIT_CONFIRMAR_ADULTO ────────────────────────────────────────────────
    # Paciente mencionó menor en flujo de MG/MF — confirmar si la cita es para adulto
    if state == "WAIT_CONFIRMAR_ADULTO":
        esp_pendiente = data.pop("_especialidad_pendiente", None) or "medicina general"
        if tl in ("menor_es_adulto", "continuar", "adulto", "es adulto", "para adulto", "si", "sí", "1"):
            log_event(phone, "menor_confirma_adulto", {"phone": phone})
            data["_menor_confirmado_adulto"] = True
            return await _iniciar_agendar(phone, data, esp_pendiente)
        if tl in ("menor_es_menor", "menor", "no", "2", "es menor", "para menor", "para el menor"):
            log_event(phone, "menor_confirma_menor", {"phone": phone})
            reset_session(phone)
            return (
                "Sin problema. Para el menor puedes agendar con *Medicina General* "
                "(Dr. Abarca, Dr. Olavarría o Dr. Márquez), que atienden a pacientes de todas las edades.\n\n"
                "Escribe *agendar* o *menu* para continuar."
            )
        data["_especialidad_pendiente"] = esp_pendiente
        save_session(phone, "WAIT_CONFIRMAR_ADULTO", data)
        return _btn_msg(
            "¿La cita es para un adulto o para un menor?",
            [
                {"id": "menor_es_adulto", "title": "Continuar (es adulto)"},
                {"id": "menor_es_menor",  "title": "Es para un menor"},
            ]
        )

    # ── WAIT_MEDFAM_FALLBACK ─────────────────────────────────────────────────
    # Márquez (Medicina Familiar) sin cupo → paciente decidió si acepta MG en cambio.
    if state == "WAIT_MEDFAM_FALLBACK":
        if tl in ("medfam_fallback_si", "si", "sí", "1", "ok", "dale", "mostrar", "claro"):
            log_event(phone, "medfam_fallback_acepta_mg", {"phone": phone})
            data.pop("medfam_solo_marquez", None)
            data.pop("force_prof_ids", None)
            data.pop("medfam_sin_cupo_ofrecer_mg", None)
            return await _iniciar_agendar(phone, data, "medicina general")
        if tl in ("medfam_fallback_no", "no", "2", "gracias", "no gracias"):
            log_event(phone, "medfam_fallback_rechaza_mg", {"phone": phone})
            reset_session(phone)
            return (
                "Sin problema. Si en otro momento quieres buscar hora con el Dr. Márquez, "
                "escribe *menu* y elige Medicina Familiar.\n\n"
                f"También puedes llamar a recepción: {CMC_TELEFONO}"
            )
        save_session(phone, "WAIT_MEDFAM_FALLBACK", data)
        return _btn_msg(
            "¿Quieres que te muestre horas con *Medicina General*?",
            [
                {"id": "medfam_fallback_si", "title": "Sí, mostrar Medicina General"},
                {"id": "medfam_fallback_no", "title": "No, gracias"},
            ]
        )

    if state == "WAIT_DURACION_MASOTERAPIA":
        # Matchear número exacto o texto escrito
        num = re.findall(r"\b(20|40)\b", txt)
        _es_20 = tl == "maso_20" or (num and num[0] == "20") or "veinte" in tl
        _es_40 = tl == "maso_40" or (num and num[0] == "40") or "cuarenta" in tl
        # BUG-5: detectar "30" / "media hora" / "sesión corta" → responder
        # con mensaje específico en vez de re-preguntar genéricamente.
        _es_30 = (
            re.search(r"\b30\b", txt) is not None
            or "media hora" in tl
            or "media hr" in tl
            or "sesion corta" in tl
            or "sesión corta" in tl
            or "treinta" in tl
        )
        if _es_20:
            duracion_maso = 20
        elif _es_40:
            duracion_maso = 40
        elif _es_30:
            save_session(phone, "WAIT_DURACION_MASOTERAPIA", data)
            return _btn_msg(
                "Solo tenemos sesiones de *20 minutos* (más rápido) o *40 minutos* "
                "(más completo).\n\nMedia hora no está disponible. ¿Cuál prefieres?",
                [
                    {"id": "maso_20", "title": "20 min"},
                    {"id": "maso_40", "title": "40 min"},
                ]
            )
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
            log_event(phone, "sin_disponibilidad", {"especialidad": "masoterapia"})
            save_tag(phone, "sin-disponibilidad")
            # FIX 2: ofrecer waitlist como en todas las demás especialidades
            data["waitlist_especialidad"] = "masoterapia"
            data["waitlist_id_prof_pref"] = 59
            save_session(phone, "WAIT_WAITLIST_CONFIRM", data)
            return _btn_msg(
                f"No encontré disponibilidad para masoterapia en los próximos días 😕\n\n"
                "¿Quieres que te avise apenas se libere un cupo?\n\n"
                f"También puedes llamarnos: 📞 *{CMC_TELEFONO}*",
                [
                    {"id": "waitlist_si", "title": "📝 Sí, inscribirme"},
                    {"id": "waitlist_no", "title": "No, gracias"},
                ]
            )
        fecha = todos[0]["fecha"]
        mejor = smart[0]
        prof_sugerido_id = mejor.get("id_profesional")
        data.update({"especialidad": "masoterapia", "slots": smart,
                     "todos_slots": todos, "fechas_vistas": [fecha],
                     "expansion_stage": 0, "prof_sugerido_id": prof_sugerido_id,
                     "slot_sugerido": mejor})
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

    # ── WAIT_ORTODONCIA_ACTIVO ───────────────────────────────────────────────
    # Patrón 4 (2026-05-19): paciente activo en tratamiento de ortodoncia.
    # Lista: agendar hora/control (directo con Castillo) / ver próxima cita /
    # reimpresión boleta / urgencia bracket suelto (2026-07-08: se agregó la
    # primera opción — antes el menú solo cubría trámites administrativos y
    # no dejaba agendar).
    if state == "WAIT_ORTODONCIA_ACTIVO":
        tl_oa = txt.strip().lower()
        if tl_oa in ("orto_agendar", "agendar hora/control", "agendar hora",
                     "agendar", "quiero agendar", "quiero hora", "hora",
                     "control", "4"):
            log_event(phone, "ortodoncia_activo_opcion", {"opcion": "agendar_castillo"})
            data["_orto_bypass_evaluacion"] = True
            return await _iniciar_agendar(
                phone, data, "ortodoncia",
                saludo_prefix=(
                    "Como ya estás en tratamiento con la Dra. Daniela Castillo "
                    "(ortodoncista), te muestro tus horas directamente con ella 👇\n\n"
                    "⚠️ Las horas están sujetas a cambios, porque a veces debemos "
                    "ajustar los horarios.\n\n"
                ),
            )
        if tl_oa in ("orto_ver_cita", "mi próxima cita", "mi proxima cita",
                     "ver cita", "próxima cita", "1"):
            reset_session(phone)
            log_event(phone, "ortodoncia_activo_opcion", {"opcion": "ver_cita"})
            return (
                "Te conecto con recepción para que te confirmen tu próxima "
                "cita con la Dra. Castillo.\n\n"
                "Llama al *(44) 296 5226* o escribe *humano* y te respondemos "
                "en cuanto podamos."
            )
        if tl_oa in ("orto_boleta", "reimpresión de boleta", "reimpresion de boleta",
                     "boleta", "comprobante", "2"):
            reset_session(phone)
            log_event(phone, "ortodoncia_activo_opcion", {"opcion": "boleta"})
            return (
                "La reimpresión de boletas la gestiona recepción directamente.\n\n"
                "Llama al *(44) 296 5226* en horario de atención "
                "(lun-vie 08:00-21:00, sáb 09:00-14:00)."
            )
        if tl_oa in ("orto_urgencia", "bracket suelto", "urgencia", "3"):
            reset_session(phone)
            log_event(phone, "ortodoncia_activo_opcion", {"opcion": "urgencia"})
            return (
                "Para urgencias con brackets (bracket suelto, alambre salido, "
                "dolor agudo), llama de inmediato al *(44) 296 5226*.\n\n"
                "Si es fuera de horario, puedes escribir aquí y recepción te "
                "responde al día siguiente."
            )
        # Cualquier otro texto → flujo normal IDLE
        reset_session(phone)
        return await handle_message(phone, txt, {"state": "IDLE", "data": {}})

    # ── WAIT_QUICK_BOOK ───────────────────────────────────────────────────────
    # Oferta "agendar otra hora como la última vez" para pacientes conocidos.
    # 3 botones: sí / otra especialidad / ahora no. Cualquier otro texto cae al
    # detector de intent general (permite "cancelar", "ver reservas", etc.).
    if state == "WAIT_QUICK_BOOK":
        tl = txt.strip().lower()
        if tl in ("quick_yes", "si", "sí", "1", "agendar", "ok", "dale"):
            esp = data.get("quick_esp", "")
            log_event(phone, "quick_book_accepted", {"especialidad": esp})
            # FIX 5: si hay slot pre-buscado, ir directo a _slot_confirmed
            # sin segunda búsqueda — reduce latencia y paso extra.
            _slot_qb = data.pop("slot_quick_book", None)
            data.pop("quick_esp", None)
            data.pop("quick_prof", None)
            if _slot_qb:
                data["especialidad"] = esp
                log_event(phone, "quick_book_slot_directo", {
                    "esp": esp, "fecha": _slot_qb.get("fecha"),
                    "hora": (_slot_qb.get("hora_inicio") or "")[:5],
                })
                return await _slot_confirmed(phone, data, _slot_qb)
            log_event(phone, "quick_book_iniciar_agendar", {"esp": esp or None, "data_keys": list(data.keys())})
            # saludo_prefix="" suprime "¡Hola de nuevo!" — el paciente ya lo
            # recibió en el mensaje del quick_book que inició este flujo.
            result_qb = await _iniciar_agendar(phone, data, esp or None, saludo_prefix="")
            log_event(phone, "quick_book_agendar_ok", {"resp_type": type(result_qb).__name__})
            return result_qb
        if tl in ("quick_other", "otra", "otra especialidad", "2", "cambiar"):
            log_event(phone, "quick_book_other")
            data.pop("quick_esp", None)
            data.pop("quick_prof", None)
            return await _iniciar_agendar(phone, data, None, saludo_prefix="")
        if tl in ("quick_cancel", "ahora no", "no", "3", "cancelar", "menu"):
            log_event(phone, "quick_book_declined")
            reset_session(phone)
            return _btn_msg(
            "Sin problema 😊 Cuando quieras, escríbeme.",
            [{"id": "menu", "title": "🏠 Volver al inicio"}]
        )
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

    # ── WAIT_META_SLOT_CHOICE ─────────────────────────────────────────────────
    # Paciente llegó desde anuncio Meta (CTWA). Se le ofrecieron 3 slots directo.
    # Acepta: "1"/"2"/"3" → pre-llenar slot y pasar a flujo estándar.
    # "otra fecha" / "más" / "no" / "otro día" → flujo completo de agendamiento.
    # Cualquier otro texto → re-detectar intent (no quedarse atascado).
    if state == "WAIT_META_SLOT_CHOICE":
        _offered = data.get("meta_offered_slots", [])
        _esp_meta = data.get("meta_esp", "")
        _tl_meta = txt.strip().lower()

        # Selección por número 1/2/3
        if _tl_meta in ("1", "2", "3") and _offered:
            _idx = int(_tl_meta) - 1
            if _idx < len(_offered):
                _slot_elegido = _offered[_idx]
                log_event(phone, "ctwa_slot_elegido", {
                    "idx": _idx,
                    "especialidad": _esp_meta,
                    "fecha": _slot_elegido.get("fecha"),
                    "hora": _slot_elegido.get("hora_inicio"),
                })
                data.pop("meta_offered_slots", None)
                data.pop("meta_esp", None)
                data["especialidad"] = _esp_meta
                data["slots"] = [_slot_elegido]
                data["todos_slots"] = [_slot_elegido]
                # Pre-cargar perfil si existe (fast-track paciente recurrente)
                _perfil_meta = get_profile(phone)
                if _perfil_meta and _perfil_meta.get("rut"):
                    data["rut_conocido"] = _perfil_meta["rut"]
                    data["nombre_conocido"] = _perfil_meta["nombre"]
                return await _slot_confirmed(phone, data, _slot_elegido)
            # Número fuera de rango (no debería ocurrir, pero)
            save_session(phone, "WAIT_META_SLOT_CHOICE", data)
            return f"Elige *1*, *2* o *3* según las horas que te mostré."

        # "otra fecha" / "más" / "otro día" / variantes → flujo completo
        _OTRA_FECHA_KWS = (
            "otra fecha", "otro dia", "otro día", "otra hora",
            "otros horarios", "mas opciones", "más opciones",
            "otra", "otras", "mas", "más", "ver mas", "ver más",
            "no me acomoda", "no me sirve",
        )
        if any(kw in _tl_meta for kw in _OTRA_FECHA_KWS):
            log_event(phone, "ctwa_otra_fecha", {"especialidad": _esp_meta})
            data.pop("meta_offered_slots", None)
            data.pop("meta_esp", None)
            _perfil_meta = get_profile(phone)
            if _perfil_meta and _perfil_meta.get("rut"):
                data["rut_conocido"] = _perfil_meta["rut"]
                data["nombre_conocido"] = _perfil_meta["nombre"]
            return await _iniciar_agendar(phone, data, _esp_meta or None)

        # "no" / "no gracias" → cerrar amable
        if _tl_meta in NEGACIONES or _tl_meta in ("no", "no gracias", "no por ahora", "ahora no"):
            log_event(phone, "ctwa_rechazo", {"especialidad": _esp_meta})
            data.pop("meta_offered_slots", None)
            data.pop("meta_esp", None)
            save_session(phone, "IDLE", data)
            return (
                "Sin problema 😊 Cuando lo necesites, estamos acá.\n"
                "_Escribe *menu* para ver todas las opciones._"
            )

        # Texto libre en CTWA: intentar detectar especialidad/intención distinta
        # antes de caer en el dispatch genérico. Si el paciente escribe "ginecología",
        # "necesito una eco de muslo", "piso pélvico", pivotamos directamente a
        # _iniciar_agendar con esa especialidad en vez de mostrar el menú genérico.
        _esp_pivot = _detectar_especialidad_en_texto(txt)
        if not _esp_pivot:
            # Intentar también detección de ecografía específica
            try:
                from ecografias import route_ecografia as _reco_ctwa, texto_menciona_ecografia as _tme_ctwa
                if _tme_ctwa(txt):
                    _eco_ctwa = _reco_ctwa(txt)
                    if _eco_ctwa:
                        _esp_pivot = _eco_ctwa["especialidad_destino"]
            except Exception:
                pass
        if _esp_pivot and _esp_pivot.lower() != (_esp_meta or "").lower():
            log_event(phone, "ctwa_pivot_especialidad", {
                "esp_original": _esp_meta, "esp_nueva": _esp_pivot, "txt": txt[:80]
            })
            data.pop("meta_offered_slots", None)
            data.pop("meta_esp", None)
            data["_txt_raw"] = txt
            _perfil_pivot = get_profile(phone)
            if _perfil_pivot and _perfil_pivot.get("rut"):
                data["rut_conocido"] = _perfil_pivot["rut"]
                data["nombre_conocido"] = _perfil_pivot.get("nombre", "")
            return await _iniciar_agendar(phone, data, _esp_pivot)
        # Sin especialidad reconocible → re-dispatch a IDLE para intent normal
        log_event(phone, "ctwa_texto_libre", {"txt": txt[:80], "especialidad": _esp_meta})
        data.pop("meta_offered_slots", None)
        data.pop("meta_esp", None)
        save_session(phone, "IDLE", data)
        return await handle_message(phone, txt, {"state": "IDLE", "data": data})

    # ── WAIT_META_WAITLIST ────────────────────────────────────────────────────
    # Sin disponibilidad en 7 días: paciente puede optar a lista de espera.
    if state == "WAIT_META_WAITLIST":
        _esp_wl = data.get("meta_waitlist_esp", "")
        _tl_wl = txt.strip().lower()
        if _tl_wl in AFIRMACIONES or _tl_wl in ("si", "sí", "si quiero", "sí quiero", "dale"):
            log_event(phone, "ctwa_waitlist_acepta", {"especialidad": _esp_wl})
            # FIX F003: inscribir DE VERDAD en la tabla waitlist (lo único que
            # leen el cron _job_waitlist_check y el aviso post-cancelación).
            # Antes solo se guardaba un tag "waitlist:esp" que ningún código lee
            # → el paciente de anuncio quedaba botado con la promesa rota.
            data["waitlist_especialidad"] = (_esp_wl or "").lower()
            data["waitlist_id_prof_pref"] = None
            data.pop("meta_waitlist_esp", None)
            _perfil_wl = get_profile(phone)
            if _perfil_wl and _perfil_wl.get("rut") and _perfil_wl.get("nombre"):
                data["rut"] = _perfil_wl["rut"]
                data["paciente_nombre"] = _perfil_wl["nombre"]
                return _inscribir_waitlist_y_responder(phone, data)
            # Paciente nuevo (llegó desde un anuncio): pedir RUT y seguir el
            # flujo estándar WAIT_WAITLIST_RUT → add_to_waitlist.
            save_session(phone, "WAIT_WAITLIST_RUT", data)
            return (
                f"Perfecto 👍 Te inscribo en la lista de espera de "
                f"*{(_esp_wl or '').capitalize()}*.\n\n"
                "Para eso necesito tu RUT:\n"
                "(ej: *12.345.678-9*)"
                + _PRIVACY_NOTE
            )
        log_event(phone, "ctwa_waitlist_rechaza", {"especialidad": _esp_wl})
        data.pop("meta_waitlist_esp", None)
        save_session(phone, "IDLE", data)
        return (
            "Sin problema 😊 Estamos acá cuando lo necesites.\n"
            "_Escribe *menu* para ver las opciones._"
        )

    # ── WAIT_ESPECIALIDAD ─────────────────────────────────────────────────────
    if state == "WAIT_ESPECIALIDAD":
        # Fix I: si el bot preguntó el tipo de ecografía, el próximo mensaje
        # es la respuesta del paciente. Pasarlo directamente a route_ecografia
        # para que "abdominal", "renal", "hombro", "rodilla", etc. sean reconocidos.
        # Sin este guard, el normalizador general no sabe que estamos en contexto
        # de eco-tipo y llama a Claude que puede retornar cualquier cosa.
        # Caso Consuelo 2026-06-12: en plena selección de especialidad preguntó
        # "¿hacen ecotomografía mamaria?" → el FAQ contestaba "escribe 1" pero el
        # estado seguía interpretando 1 = primera especialidad de la lista (MG) →
        # quedó agendada en Medicina General queriendo una eco. Si el texto
        # matchea un tipo de eco CONCRETO (estricto, requiere mención de eco),
        # ES su elección de especialidad: rutear por el carril eco-tipo existente.
        if not data.get("wait_eco_tipo"):
            try:
                from ecografias import route_ecografia as _reco_pre
                _eco_pre = _reco_pre(txt)
            except Exception:
                _eco_pre = None
            if _eco_pre is not None:
                log_event(phone, "eco_en_wait_especialidad", {"txt": txt[:120]})
                data["wait_eco_tipo"] = True
                return await handle_message(phone, txt,
                                            {"state": "WAIT_ESPECIALIDAD", "data": data})

        if data.get("wait_eco_tipo"):
            data.pop("wait_eco_tipo", None)
            try:
                from ecografias import route_ecografia as _reco_fi, MSG_PREGUNTAR_TIPO as _MSG_REFI
                # assume_context=True: ya estamos en selección de tipo de eco
                # (el bot acaba de preguntar el tipo), así "abdominal"/"de rodilla"
                # deben resolver aunque no repitan la palabra "ecografía".
                _eco_fi = _reco_fi(txt, assume_context=True)
            except Exception:
                _eco_fi = None
                _MSG_REFI = None
            if _eco_fi is not None:
                log_event(phone, "ecografia_tipo_matched", {
                    "txt": txt[:120],
                    "destino": _eco_fi.get("especialidad_destino", ""),
                    "id_prof": _eco_fi.get("id_profesional"),
                    "flujo": _eco_fi.get("flujo"),
                })
                _esp_eco_fi = _eco_fi["especialidad_destino"]
                if _eco_fi.get("flujo") == "no_disponible":
                    # Eco obstétrica → el CMC no la realiza
                    log_event(phone, "eco_obstetrica_no_disponible", {"txt": txt[:120]})
                    reset_session(phone)
                    return _eco_fi["mensaje"].format(tipo=txt)
                if _eco_fi.get("flujo") == "waitlist":
                    # Ecocardiograma → waitlist
                    data["waitlist_especialidad"] = _esp_eco_fi
                    data["waitlist_id_prof_pref"] = _eco_fi["id_profesional"]
                    save_session(phone, "WAIT_WAITLIST_CONFIRM_ECOCA", data)
                    return _btn_msg(
                        _eco_fi["mensaje"].format(tipo=txt),
                        [
                            {"id": "ecoca_waitlist_si", "title": "Sí, lista de espera"},
                            {"id": "ecoca_waitlist_no", "title": "No, gracias"},
                            {"id": "ecoca_menu", "title": "Volver al menú"},
                        ]
                    )
                # Normal → iniciar agendar con la especialidad resuelta.
                # FIX 1a: guardar el texto original en _txt_raw para que
                # _iniciar_agendar pueda llamar route_ecografia con el órgano
                # ("abdominal", "renal", etc.) en vez de solo "ecografía", evitando
                # el loop donde route_ecografia("ecografía") retorna None y vuelve
                # a preguntar el tipo indefinidamente.
                data["_txt_raw"] = txt
                return await _iniciar_agendar(phone, data, _esp_eco_fi)
            else:
                # Texto no reconocido como tipo de eco — volver a preguntar.
                # FIX 1b: max 2 reintentos antes de escalar a recepcionista.
                _eco_reintentos = data.get("eco_tipo_reintentos", 0) + 1
                log_event(phone, "ecografia_sin_tipo", {
                    "txt": txt[:120], "reintento": True, "intento": _eco_reintentos
                })
                if _eco_reintentos >= 2:
                    # Escalar a recepción — el paciente no logra escribir el tipo
                    log_event(phone, "ecografia_escalada_recepcion", {"txt": txt[:120]})
                    save_session(phone, "HUMAN_TAKEOVER", {})
                    return (
                        "No logré identificar el tipo de ecografía que necesitas 😕\n\n"
                        "Una recepcionista va a ayudarte directamente.\n\n"
                        f"También puedes llamarnos: 📞 *{CMC_TELEFONO}*"
                    )
                data["wait_eco_tipo"] = True  # mantener el flag para el próximo intento
                data["eco_tipo_reintentos"] = _eco_reintentos
                save_session(phone, "WAIT_ESPECIALIDAD", data)
                _MSG_REFI_FB = _MSG_REFI or (
                    "No reconocí ese tipo de ecografía. Por favor escribe uno de los tipos del menú:\n\n"
                    "• Abdominal · Renal · Tiroides · Hombro · Rodilla → David Pardo\n"
                    "• Transvaginal · Pélvica · Obstétrica → Dr. Rejón\n"
                    "• Ecocardiograma → Dr. Millán"
                )
                return _MSG_REFI_FB

        # FIX 4: Mapeo de entrada numérica (1-8) en WAIT_ESPECIALIDAD.
        # Tras takeover de recepción, el paciente escribe "1" creyendo que es menú
        # numerado. Sin este handler el bot lo rechazaba con "no reconocí eso".
        # Lógica: si hay lista de especialidades en data["esp_lista"], mapear al ítem.
        # Si no hay lista, usar fallback: 1→medicina general (la más frecuente).
        import re as _re_num4
        if _re_num4.fullmatch(r"[1-8]", tl.strip()):
            _num4 = int(tl.strip())
            _esp_lista4 = data.get("esp_lista")  # lista guardada si el bot mostró un menú numerado
            if _esp_lista4 and isinstance(_esp_lista4, list) and _num4 <= len(_esp_lista4):
                _esp_elegida4 = _esp_lista4[_num4 - 1]
                log_event(phone, "wait_esp_num_lista", {"num": _num4, "esp": _esp_elegida4})
                return await _iniciar_agendar(phone, data, _esp_elegida4)
            # Sin lista en contexto: fallback seguro 1→medicina general
            _FALLBACK_NUM4 = {
                1: "medicina general",
                2: "medicina familiar",
                3: "kinesiología",
                4: "psicología",
                5: "nutrición",
                6: "odontología",
                7: "matrona",
                8: "fonoaudiología",
            }
            _esp_fallback4 = _FALLBACK_NUM4.get(_num4)
            if _esp_fallback4:
                log_event(phone, "wait_esp_num_fallback", {"num": _num4, "esp": _esp_fallback4})
                return await _iniciar_agendar(phone, data, _esp_fallback4)

        # Resolución contextual de "ese examen" / "el mismo examen" / "lo que dijiste":
        # el paciente se refiere a algo mencionado por el bot antes del reset. Caso real
        # 56950836674 (2026-05-25): bot explicó bioimpedanciometría → Nutrición durante
        # HUMAN_TAKEOVER, sesión se reseteó, paciente volvió pidiendo "solo necesito
        # realizarme ese examen" y el bot no lo reconoció. Solución: escanear los últimos
        # mensajes salientes del bot buscando una especialidad mencionada.
        import re as _re_eo
        _RE_REF_PREV = _re_eo.compile(
            r"\b(ese|esa|este|esta|el|la|lo)\s+(mismo\s+)?(examen|estudio|chequeo|test|procedimiento|control)"
            r"|\b(lo\s+que|el\s+que|la\s+que)\s+(dijiste|mencionaste|comentaste|me\s+dijiste|recomendaste)"
            r"|\b(solo|sólo|nada\s+más)\s+(ese|esa|el|la|necesito\s+(ese|esa|el|la))",
            _re_eo.IGNORECASE,
        )
        if _RE_REF_PREV.search(tl):
            try:
                from session import _conn as _s_conn_ref
                _c_ref = _s_conn_ref()
                _rows_ref = _c_ref.execute(
                    "SELECT text FROM messages WHERE phone=? AND direction='out' "
                    "ORDER BY ts DESC LIMIT 8",
                    (phone,),
                ).fetchall()
                _c_ref.close()
            except Exception:
                _rows_ref = []
            _esp_ctx = None
            for (_t,) in _rows_ref:
                if not _t or _t.startswith("[template:") or _t.startswith("[Recepcionista]"):
                    continue
                _hit = _detectar_especialidad_en_texto(_t)
                if _hit:
                    _esp_ctx = _hit
                    break
            if _esp_ctx:
                log_event(phone, "wait_esp_resuelto_por_contexto",
                          {"texto": tl[:140], "esp": _esp_ctx})
                return await _iniciar_agendar(phone, data, _esp_ctx)
            # No hay contexto utilizable → preguntar con menos fricción
            save_session(phone, "WAIT_ESPECIALIDAD", data)
            return (
                "¿De qué examen me hablas? Necesito el nombre o la especialidad "
                "(ej: Bioimpedanciometría, Ecografía, Audiometría)."
            )

        # C2: payloads de botones que no son nombres de especialidad.
        # Si llegan aquí (ej. quick_yes, quick_no, menu_volver, no_agendar)
        # deben enrutarse correctamente, no pasarlos al normalizador de especialidad.
        _BUTTON_IDS_WE = frozenset({
            "quick_yes", "quick_other", "quick_no", "no_agendar", "menu_volver",
        })
        if tl in _BUTTON_IDS_WE:
            if tl == "quick_yes":
                # Repetir menú de especialidades
                save_session(phone, "WAIT_ESPECIALIDAD", data)
                return f"¿Qué especialidad necesitas?\n\n{_ESPECIALIDADES_TEXTO}"
            if tl in ("quick_other", "quick_no", "no_agendar"):
                save_session(phone, "WAIT_ESPECIALIDAD", data)
                return f"¿Qué especialidad necesitas?\n\n{_ESPECIALIDADES_TEXTO}"
            if tl == "menu_volver":
                reset_session(phone)
                return await handle_message(phone, "menu", {"state": "IDLE", "data": {}})
        # C2: preguntas genéricas de agendamiento que no son nombres de especialidad
        # ("¿puedo reservar una cita?", "¿tienen para hoy?", "¿hay hora?")
        _es_pregunta_agendamiento = (
            (tl.startswith("¿") or tl.endswith("?"))
            and any(kw in tl for kw in ("puedo", "tienen", "hay", "agendar", "reservar", "hora"))
        )
        if _es_pregunta_agendamiento:
            save_session(phone, "WAIT_ESPECIALIDAD", data)
            return f"Sí, claro. ¿Qué especialidad necesitas?\n\n{_ESPECIALIDADES_TEXTO}"

        # Detectar fecha pedida en este mensaje y propagar al flujo de agendar.
        # Bug histórico (caso María 56968621918): paciente clickea "Agendar" → llega
        # a WAIT_ESPECIALIDAD → escribe "medicina general para hoy". El branch
        # IDLE no detecta la fecha porque ya pasamos a otro estado, y el bot
        # mostraba el siguiente día sin avisar.
        _fp_we = _detectar_fecha_pedida_idle(txt)
        if _fp_we:
            data["fecha_pedida_idle"] = _fp_we
        _fr_we = _detectar_franja_horaria(txt)
        if _fr_we:
            data["franja_horaria"] = _fr_we

        # Selección de categoría (paso intermedio)
        if tl == "cat_medico":
            save_session(phone, "WAIT_ESPECIALIDAD", data)
            return _especialidades_medico_msg()
        if tl == "cat_dental":
            save_session(phone, "WAIT_ESPECIALIDAD", data)
            return _especialidades_dental_msg()

        # Psiquiatría disponible desde 2026: Dra. Cecilia Unibazo (prof 78),
        # TELECONSULTA solo jueves 16-20. Antes este branch respondía "no
        # tenemos psiquiatra" (fósil de cuando no había) y contradecía al
        # flujo IDLE, que sí agendaba con ella.
        if any(k in tl_norm for k in ("psiquiatra", "psiquiatria", "psiquiatría",
                                       "psiquiatras")):
            return await _iniciar_agendar(phone, data, "psiquiatría")
        # Neurología: Dra. Franca González (prof 79), TELEMEDICINA. Mismo
        # bypass explícito que psiquiatría para no depender del fallback genérico.
        if any(k in tl_norm for k in ("neurologo", "neurologa", "neurología",
                                       "neurologia", "neurólogo", "neuróloga")):
            return await _iniciar_agendar(phone, data, "neurología")
        # Oftalmología: TM Ana Celedón (prof 80), PRESENCIAL. Mismo bypass
        # explícito que psiquiatría/neurología para no depender del fallback genérico.
        if any(k in tl_norm for k in ("oftalmologo", "oftalmologa", "oftalmología",
                                       "oftalmologia", "oftalmólogo", "oftalmóloga",
                                       "optometra", "optometria", "optometría",
                                       "optometrista", "celedon", "celedón")):
            return await _iniciar_agendar(phone, data, "tecnología médica oftalmológica")
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
        # Guard 2026-05-10: si después de todos los fallbacks la especialidad
        # candidata sigue siendo texto libre sospechoso (frase larga, palabras
        # de no-especialidad, texto con puntuación libre), volver a preguntar.
        # Evita "no contamos con he llamado a ambos números..." y similares.
        _ec_lower = (especialidad_candidata or "").lower().strip()
        _ec_palabras = set(re.sub(r"[^a-záéíóúñü ]", "", _ec_lower).split())
        _EC_NO_ESP = {
            "llamado", "llame", "llamo", "numero", "numeros", "telefono", "celular",
            "particular", "apagado", "ocupado", "comunico", "espera", "esperar",
            "porque", "cuando", "mientras", "forma", "ambos",
        }
        _ec_es_frase_libre = (
            not especialidad_candidata
            or len(_ec_lower) > 40
            or bool(_ec_palabras & _EC_NO_ESP)
            or ".-" in _ec_lower
            or _ec_lower.count(" ") >= 5
        )
        # FIX 1c (2026-06-10): si el texto normalizado empieza con "no " o ES
        # exactamente "no", nunca generar un slug de especialidad.
        # Caso prod: "No del Otorrino" → especialidad_candidata="no otorrino" →
        # "No encontré horas para *no otorrino*". Ahora se bloquea aquí.
        _ec_empieza_no = bool(re.match(r"^no(\s|$)", _ec_lower))
        if _ec_es_frase_libre or _ec_empieza_no:
            log_event(phone, "wait_esp_texto_libre_rechazado", {"txt": txt[:120]})
            save_session(phone, "WAIT_ESPECIALIDAD", data)
            return f"No reconocí eso como una especialidad. ¿Qué especialidad necesitas?\n\n{_ESPECIALIDADES_TEXTO}"
        # Si venimos de _modo_degradado sin especialidad: Medilink sigue caído,
        # saltar directo a waitlist sin re-entrar a _iniciar_agendar.
        if data.pop("_modo_degradado_esp_pending", False) and especialidad_candidata:
            _esp_dg = especialidad_candidata
            _esp_dg_display = _esp_dg.title()
            data["waitlist_especialidad"] = _esp_dg
            save_session(phone, "WAIT_WAITLIST_CONFIRM", data)
            log_event(phone, "modo_degradado_esp_resuelta_waitlist", {"especialidad": _esp_dg})
            return _btn_msg(
                f"Gracias. Te anoto en la lista de espera para *{_esp_dg_display}*.\n\n"
                f"En cuanto el sistema vuelva, te contactamos con una hora disponible.\n\n"
                f"También puedes llamarnos:\n"
                f"📞 *{CMC_TELEFONO}* · ☎️ *{CMC_TELEFONO_FIJO}*",
                [
                    {"id": "waitlist_si", "title": "Sí, avísame"},
                    {"id": "waitlist_no", "title": "No, gracias"},
                ]
            )
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
        # C3: filtrar slots pasados que puedan quedar en cache entre días
        _hoy_str_ws = _hoy_cl.strftime("%Y-%m-%d")
        def _filtrar_slots_pasados(lst: list) -> list:
            return [s for s in lst if (s.get("fecha") or "") >= _hoy_str_ws]
        slots_mostrados = _filtrar_slots_pasados(data.get("slots", []))
        todos_slots     = _filtrar_slots_pasados(data.get("todos_slots", slots_mostrados))
        # P1-A: en reagendar, excluir la cita vieja de los slots ofrecidos para
        # que el paciente no vea su propia hora como "disponible".
        _reag_excluir = data.get("_reagendar_excluir")
        if _reag_excluir:
            _rex_fecha, _rex_hora = _reag_excluir
            _rex_hora5 = _rex_hora[:5]
            slots_mostrados = [s for s in slots_mostrados
                               if not (s.get("fecha") == _rex_fecha
                                       and s.get("hora_inicio", "")[:5] == _rex_hora5)]
            todos_slots     = [s for s in todos_slots
                               if not (s.get("fecha") == _rex_fecha
                                       and s.get("hora_inicio", "")[:5] == _rex_hora5)]
        if slots_mostrados != data.get("slots"):
            data["slots"] = slots_mostrados
        if todos_slots != data.get("todos_slots"):
            data["todos_slots"] = todos_slots
        fechas_vistas   = data.get("fechas_vistas", [])
        especialidad    = data.get("especialidad", "")

        # ── SOBRECUPO (gateado OFF) ──────────────────────────────────────────
        # Para especialidades que sobrecupean (eco, autorizado por David), si la hora
        # formal está lejos, anteponer cupos "por medio" cercanos para no perder al
        # paciente. Inerte salvo SOBRECUPO_ENABLED=true (generar_slots devuelve []).
        # Dedup: solo inyecta una vez (si ya hay un slot sobrecupo en la lista, skip).
        if not any(s.get("sobrecupo") for s in todos_slots):
            try:
                import sobrecupo as _sc
                _sobres = await _sc.generar_slots(especialidad)
                if _sobres:
                    from medilink import _fmt_fecha as _ff_sc
                    _primera_formal = todos_slots[0]["fecha"] if todos_slots else "9999-99-99"
                    for _s in _sobres:
                        _s.setdefault("fecha_display", _ff_sc(_s["fecha"]))
                    # Anteponer SOLO los sobrecupos anteriores a la 1ª hora formal.
                    # Sin el fallback "or _sobres": si no hay sobrecupos de fecha
                    # anterior, no inyectar ninguno (evita duplicados cuando los
                    # sobrecupos son del mismo día que los slots formales).
                    _sobres = [s for s in _sobres if s["fecha"] < _primera_formal]
                    if _sobres:
                        todos_slots = _sobres + todos_slots
                    # Dedup defensivo por (fecha, hora_inicio) — cubre casos donde
                    # la misma hora aparece en sobrecupos y slots formales.
                    _visto_slots: set = set()
                    _todos_dedup = []
                    for _s_dd in todos_slots:
                        _k_dd = (_s_dd.get("fecha"), _s_dd.get("hora_inicio"))
                        if _k_dd not in _visto_slots:
                            _visto_slots.add(_k_dd)
                            _todos_dedup.append(_s_dd)
                    todos_slots = _todos_dedup
                    slots_mostrados = todos_slots[:5]
                    data["slots"] = slots_mostrados
                    data["todos_slots"] = todos_slots
                    log_event(phone, "sobrecupo_ofrecido",
                              {"esp": especialidad, "n": len(_sobres)})
            except Exception as _e_sc:  # noqa: BLE001 — nunca romper el agendamiento
                log.warning("sobrecupo offering falló: %s", _e_sc)

        fecha_actual    = todos_slots[0]["fecha"] if todos_slots else None
        # tl_norm_slot: normalizado usado por todo el handler. Definido al inicio
        # porque bloques tempranos (mes/fecha/semana) lo referencian antes del
        # punto donde históricamente se asignaba (~línea 3140). Causaba NameError
        # crashes. Caso real 2026-04-23: 15 crashes en pacientes que escribieron
        # "20hrs", botón "otro_dia", "6", frases con mes antes de llegar a 3140.
        tl_norm_slot = txt.lower().strip()

        # Bug A fix (2026-05-15): si el paciente acaba de recibir la propuesta
        # "el Dr. X no tiene horas el viernes, ¿quieres ver el lunes?" y responde
        # afirmativamente, cargar la alternativa guardada y mostrar esos slots.
        _otro_prox = data.get("_otro_prof_prox")
        if _otro_prox and (tl in AFIRMACIONES or tl_norm in AFIRMACIONES or tl_norm_slot in ("si", "sí", "dale", "ya", "ok")):
            data.pop("_otro_prof_prox", None)
            smart_prox = _otro_prox.get("smart", [])
            todos_prox = _otro_prox.get("todos", [])
            prox_fecha  = _otro_prox.get("fecha")
            prox_pid    = _otro_prox.get("prof_id")
            if prox_fecha and prox_fecha not in fechas_vistas:
                fechas_vistas = fechas_vistas + [prox_fecha]
            filtrado = [s for s in smart_prox if s.get("id_profesional") == prox_pid] or smart_prox
            data.update({"slots": filtrado, "todos_slots": todos_prox,
                         "fechas_vistas": fechas_vistas, "expansion_stage": 0,
                         "prof_sugerido_id": prox_pid})
            save_session(phone, "WAIT_SLOT", data)
            return _format_slots(filtrado)

        # Respuesta al sugerido proactivo (botón o texto libre "si"/"sí"/"confirmo"/...)
        # Afirmación libre: "puedo reservar?", "sí reservalo", "reserva esa hora",
        # "agenda esa", "tomo esa hora", etc. Caso real 2026-04-28
        # (fb_27066996906237198): bot ofreció Podología 14:00, paciente preguntó
        # "¿Puedo reservar una cita?" como confirmación implícita y el bot
        # reseteó el flow con "Claro, te ayudo a agendar 😊".
        # BUG-10: ampliar afirmaciones libres específicas a WAIT_SLOT.
        # Frases como "si la tomo", "me acomoda", "está bien", "me sirve"
        # deben confirmar el slot prominente directamente.
        _AFIRM_SLOT_EXTRA = {
            "si la tomo", "si, la tomo", "la tomo", "tomo la hora", "tomo esa",
            "esa misma", "esa hora", "me acomoda", "me acomoda esa",
            "esta bien", "está bien", "ta bien", "tá bien",
            "ok la tomo", "dale la tomo", "dale", "me sirve esa",
            "me sirve", "ok me sirve",
        }
        _afirm_libre = (
            tl_norm_slot in _AFIRM_SLOT_EXTRA
            or (
                ("reserv" in tl or "agenda" in tl or "tomo" in tl or "tomar" in tl
                 or "confirm" in tl or "esa hora" in tl or "esa hora me sirve" in tl)
                and not any(neg in tl for neg in (
                    "no reserv", "no quiero reserv", "no agenda",
                    "no la reserv", "no me sirve", "no gracias",
                ))
            )
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
            # FIX Capa-1 (2026-06-12): si hay un slot_sugerido guardado (el que se
            # mostró con ⭐), usarlo exactamente. Cuando SOBRECUPO_ENABLED=true, el
            # bloque de inyección de sobrecupos puede reordenar slots_mostrados entre
            # la primera oferta y el confirm, haciendo que slots_mostrados[0] sea un
            # sobrecupo ≠ al slot mostrado al paciente. slot_sugerido ancla el slot
            # correcto. Se hace pop para no contaminar reintentos posteriores.
            _sugerido = data.pop("slot_sugerido", None)
            # El ancla slot_sugerido solo es válida si AÚN coincide con un slot
            # mostrado ahora. Si el paciente navegó a otro día o cambió de
            # profesional, el ancla viejo apunta a otra cita y reservaba el slot
            # equivocado (bug 2026-06-23: pidió Márquez/navegó pero reservaba el
            # slot original de Abarca / del primer día). Si el ancla sigue en la
            # lista (caso reordenamiento por sobrecupo) se respeta; si no, se usa
            # el primero mostrado.
            def _slot_key(s):
                return (s.get("fecha"), (s.get("hora_inicio") or "")[:5], s.get("id_profesional"))
            if _sugerido and any(_slot_key(s) == _slot_key(_sugerido) for s in slots_mostrados):
                slot = _sugerido
            else:
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
                # ITEM-19: capturar errores de Medilink en _handle_expansion para
                # no propagar hasta main.py (que hace reset_session → paciente pierde
                # todo el flujo). Mantenemos WAIT_SLOT intacto y mostramos mensaje amable.
                try:
                    return await _handle_expansion(phone, data, slots_mostrados, todos_slots,
                                                   data.get("expansion_stage", 0), fecha_actual)
                except (httpx.RequestError, Exception) as _exp_he:
                    _is_medilink_err = isinstance(_exp_he, httpx.RequestError) or "Medilink" in str(_exp_he)
                    if not _is_medilink_err:
                        raise
                    save_session(phone, "WAIT_SLOT", data)
                    log_event(phone, "expansion_medilink_error", {"error": str(_exp_he)[:200]})
                    return (
                        "Tuve un problema al cargar más horarios en este momento 😕\n\n"
                        "Puedes intentarlo de nuevo en unos segundos, o escribe "
                        "*otro día* para buscar en otra fecha."
                    )
            # Defensa sistémica: si solo hay 1 slot total (ya mostrado), no
            # tiene sentido "ver_otros" del mismo día — debemos expandir a otro
            # día o profesional. Caso real 2026-04-28 (56934363158): bot ofreció
            # Dr. Abarca 08:00, paciente clickeó "ver_otros", bot mostró el
            # mismo slot duplicado.
            if len(todos_slots or []) <= 1:
                # Buscar slots de OTROS días para esta especialidad.
                # NO re-importar buscar_primer_dia: ya está al tope del módulo (línea 15).
                # Un `from medilink import buscar_primer_dia` local marca la variable
                # como local en TODA la función handle_message (compile-time), y dispara
                # UnboundLocalError en cualquier otro branch que la use antes
                # (visto 2026-04-30 en otro_prof y otro_dia con varios pacientes).
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

        # Botón "Agendar con <Prof>" que aparece cuando los otros profs no tienen
        # slots ese día: el paciente elige quedarse con el prof original.
        # Formato del id: "agendar_prof_<id_profesional>"
        if tl.startswith("agendar_prof_"):
            try:
                _pid_orig = int(tl.split("_")[-1])
            except (ValueError, IndexError):
                _pid_orig = None
            if _pid_orig and todos_slots:
                slots_prof_orig = [s for s in todos_slots if s.get("id_profesional") == _pid_orig]
                if slots_prof_orig:
                    data["slots"] = slots_prof_orig
                    data["prof_sugerido_id"] = _pid_orig
                    save_session(phone, "WAIT_SLOT", data)
                    return _format_slots(slots_prof_orig, mostrar_todos=True)
            # Si por alguna razón no hay slots en caché, re-mostrar lo que hay
            save_session(phone, "WAIT_SLOT", data)
            return _format_slots(slots_mostrados or todos_slots, mostrar_todos=True)

        # ── Normalizar sinónimos de "otro profesional" antes del handler ──
        # "quiero otro doctor", "cambiar doctor", etc. → re-dispatch a otro_prof
        _OTRO_PROF_SYNS = (
            "no quiero ese", "no me gusta", "otro doctor", "otro profesional",
            "otra doctora", "otro médico", "otro medico", "con otro",
            "con otra", "cambiar doctor", "cambiar profesional",
            "no ese", "no ese doctor", "prefiero otro",
            "quiero otro doctor", "quiero otra doctora", "quiero otro médico",
            "quiero otro medico", "quiero otro profesional",
        )
        if tl != "otro_prof" and any(p in tl_norm_slot for p in _OTRO_PROF_SYNS):
            tl = "otro_prof"

        # "Otro profesional" → muestra slots del/los otro(s) doctor(es) de la especialidad
        if tl == "otro_prof":
            from medilink import _ids_para_especialidad
            prof_sugerido_id = data.get("prof_sugerido_id")
            ids_esp = _ids_para_especialidad(especialidad)
            if especialidad in _ESP_MED_GENERAL:
                ids_esp = list(_MED_GENERAL_IDS)  # [73, 1, 13] = Abarca, Olavarría, Márquez
            elif especialidad in _ESP_MED_FAMILIAR:
                ids_esp = list(_MED_FAMILIAR_IDS)  # Solo Márquez (ID 13)
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

            # Helper: agrupar lista flat de slots por profesional (orden de aparición)
            def _agrupar_por_prof(flat: list) -> list:
                grupos: dict[int, list] = {}
                for s in flat:
                    pid = s.get("id_profesional")
                    if pid not in grupos:
                        grupos[pid] = []
                    grupos[pid].append(s)
                return [{"slots": v} for v in grupos.values()]

            _DIAS_N_OP = ["lunes","martes","miércoles","jueves","viernes","sábado","domingo"]
            def _fecha_label_corta(iso: str) -> str:
                try:
                    _d = date.fromisoformat(iso)
                    return f"{_DIAS_N_OP[_d.weekday()]} {_d.day:02d}/{_d.month:02d}"
                except Exception:
                    return iso

            _maso_override = {59: data["maso_duracion"]} if especialidad == "masoterapia" and data.get("maso_duracion") else None

            # 1) Intentar con los slots que ya tenemos del mismo día (todos_slots).
            # Mostrar TODOS los otros profesionales con disponibilidad ese día,
            # agrupados por profesional. NO filtrar al primero que aparezca.
            slots_otros_mismo_dia = [s for s in todos_slots if s.get("id_profesional") in set(otros_ids)]
            if slots_otros_mismo_dia:
                grupos = _agrupar_por_prof(slots_otros_mismo_dia)
                flat_ordered = [s for g in grupos for s in g["slots"]]
                data["slots"] = flat_ordered
                data["todos_slots"] = flat_ordered
                data["prof_sugerido_id"] = flat_ordered[0].get("id_profesional")
                data["profs_vistos"] = list(profs_vistos)
                save_session(phone, "WAIT_SLOT", data)
                return _format_slots_expansion(grupos)

            # 2) No hay cupo de los otros en la caché del mismo día.
            # Consultar la fecha activa en Medilink directamente para los otros profs.
            # NUNCA saltar a otra fecha sin que el paciente lo pida.
            if fecha_actual:
                smart_misma, todos_misma = await buscar_slots_dia_por_ids(
                    otros_ids, fecha_actual, intervalo_override=_maso_override)
                if todos_misma:
                    # Mostrar TODOS los otros profesionales con slots ese día, agrupados.
                    grupos = _agrupar_por_prof(todos_misma)
                    flat_ordered = [s for g in grupos for s in g["slots"]]
                    data.update({"slots": flat_ordered, "todos_slots": todos_misma,
                                 "prof_sugerido_id": flat_ordered[0].get("id_profesional"),
                                 "profs_vistos": list(profs_vistos)})
                    save_session(phone, "WAIT_SLOT", data)
                    return _format_slots_expansion(grupos)

                # Ninguno de los otros tiene slots ese día exacto.
                # Avisar claramente con opciones explícitas; NO buscar otra fecha solos.
                from medilink import PROFESIONALES as _PROFS_CAMBIO
                _fecha_orig_fmt = _fecha_label_corta(fecha_actual)
                # Nombre del profesional actual (el que sí tiene slots ese día)
                _prof_actual_nombre = (
                    _PROFS_CAMBIO.get(int(prof_sugerido_id), {}).get("nombre", "el profesional actual")
                    if prof_sugerido_id else "el profesional actual"
                )
                # Botones: ver otras fechas / agendar con el prof actual / menú
                _btn_agendar_id = f"agendar_prof_{prof_sugerido_id}" if prof_sugerido_id else "menu"
                _btn_agendar_title = f"Agendar con {_prof_actual_nombre.split()[-1]}"[:20]
                data["profs_vistos"] = list(profs_vistos)
                save_session(phone, "WAIT_SLOT", data)
                return _btn_msg(
                    f"Para el {_fecha_orig_fmt} solo {_prof_actual_nombre} tiene horas disponibles. "
                    f"Los otros profesionales no trabajan ese día.\n\n"
                    f"¿Qué prefieres hacer?",
                    [
                        {"id": "otro_dia",          "title": "Ver otras fechas"},
                        {"id": _btn_agendar_id,      "title": _btn_agendar_title},
                        {"id": "menu",               "title": "Volver al menú"},
                    ]
                )

            # fecha_actual es None (sesión sin slots vigentes): fallback original
            _esp_api = "medicina general" if especialidad in _ESP_MED_FAMILIAR else especialidad
            smart_nuevo, todos_nuevo = await buscar_primer_dia(
                _esp_api, excluir=fechas_vistas,
                solo_ids=otros_ids, intervalo_override=_maso_override)
            if not todos_nuevo:
                return (
                    "No encontré disponibilidad con otros profesionales en los próximos días.\n\n"
                    "Escribe *otro día* para seguir buscando con el mismo doctor, "
                    f"o llama a recepción: {CMC_TELEFONO}"
                )
            nueva_fecha = todos_nuevo[0]["fecha"]
            if nueva_fecha not in fechas_vistas:
                fechas_vistas = fechas_vistas + [nueva_fecha]
            grupos_nuevo = _agrupar_por_prof(todos_nuevo)
            flat_nuevo = [s for g in grupos_nuevo for s in g["slots"]]
            nuevo_sugerido_id = flat_nuevo[0].get("id_profesional")
            data.update({"slots": flat_nuevo, "todos_slots": todos_nuevo,
                         "fechas_vistas": fechas_vistas, "expansion_stage": 0,
                         "prof_sugerido_id": nuevo_sugerido_id,
                         "profs_vistos": list(profs_vistos)})
            save_session(phone, "WAIT_SLOT", data)
            return _format_slots_expansion(grupos_nuevo)

        # "ver todos" / "ver más" → expansión progresiva para med general, o todos del día para el resto
        VER_TODOS = {"ver todos", "todos", "ver todo", "todos los horarios", "mostrar todos",
                     "ver horarios", "quiero ver los horarios", "ver todos los horarios",
                     "mostrar horarios", "quiero ver horarios", "ver mas", "ver más", "ver_todos",
                     # BUG-03: texto libre equivalente al botón "Otros horarios"
                     "otros horarios", "otras horas", "ver otros", "otros", "ver otros horarios",
                     "mas horarios", "más horarios", "otras opciones", "otras alternativas"}
        if tl in VER_TODOS or any(p in tl for p in ["ver todos", "todos los horarios", "ver horarios",
                                                      "ver mas", "ver más", "otros horarios",
                                                      "mas horarios", "más horarios"]):
            if especialidad in _ESPECIALIDADES_EXPANSION:
                # "ver_todos" debe saltar directo al stage 2 (todos los profesionales),
                # sin importar el stage actual. Si pasamos expansion_stage=0 llegaríamos
                # a next_stage=1 que solo muestra el doctor sugerido ya en sesión — eso
                # era el bug: Vicente Salas veía solo Abarca, nunca Márquez ni Olavarría.
                # Forzamos stage=1 → next_stage=2 → _handle_expansion consulta _MED_GENERAL_IDS.
                _stage_ver_todos = max(data.get("expansion_stage", 0), 1)
                # ITEM-19: ídem al bloque "ver_otros" — capturar error Medilink
                # sin resetear sesión ni perder el contexto de agendamiento.
                try:
                    return await _handle_expansion(phone, data, slots_mostrados, todos_slots,
                                                   _stage_ver_todos, fecha_actual)
                except (httpx.RequestError, Exception) as _exp_vt:
                    _is_ml_err = isinstance(_exp_vt, httpx.RequestError) or "Medilink" in str(_exp_vt)
                    if not _is_ml_err:
                        raise
                    save_session(phone, "WAIT_SLOT", data)
                    log_event(phone, "expansion_medilink_error", {"error": str(_exp_vt)[:200]})
                    return (
                        "Tuve un problema al cargar más horarios en este momento 😕\n\n"
                        "Puedes intentarlo de nuevo en unos segundos, o escribe "
                        "*otro día* para buscar en otra fecha."
                    )
            data["slots"] = todos_slots
            save_session(phone, "WAIT_SLOT", data)
            return _format_slots(todos_slots, mostrar_todos=True)

        # Día específico → "para el viernes", "hay para el martes", etc.
        _maso_override = {59: data["maso_duracion"]} if especialidad == "masoterapia" and data.get("maso_duracion") else None
        dia_pedido = next((wd for nombre, wd in _DIAS_SEMANA.items() if nombre in tl), None)
        if dia_pedido is not None:
            fecha_dia = _proxima_fecha_dia(dia_pedido)
            if fecha_dia:
                try:
                    smart_dia, todos_dia = await _buscar_slots_dia_con_retry(
                        especialidad, fecha_dia, intervalo_override=_maso_override)
                except Exception as _e_dia_ped:
                    log.warning("buscar_slots_dia dia_pedido falló tras retries: %s", _e_dia_ped)
                    smart_dia, todos_dia = [], []
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
        # 1a) Fecha "DD de MMMM" o "el DD de MMMM" — va primero para capturar
        #     el día exacto antes de caer al parser de solo-mes.
        #     A3: sin este paso, "para hoy 19 de mayo" caía al parser de mes
        #     que generaba 2026-05-01 (pasado) o 2027-05-01 (año siguiente).
        _m_dia_mes = re.search(
            r"\b(\d{1,2})\s+de\s+(" + "|".join(
                k for k in _MESES_ES if len(k) >= 3
            ) + r")\b",
            tl_norm_slot,
        )
        if _m_dia_mes:
            try:
                _dia_dm = int(_m_dia_mes.group(1))
                _mes_dm = _MESES_ES[_m_dia_mes.group(2)]
                _hoy_dt = datetime.now(_CHILE_TZ).date()
                _anio_dm = _hoy_dt.year
                _candidato_dm = _hoy_dt.replace(year=_anio_dm, month=_mes_dm, day=1)
                # Si la fecha ya pasó, avanzar al año siguiente
                import datetime as _dt_mod
                try:
                    _candidato_dm = _dt_mod.date(_anio_dm, _mes_dm, _dia_dm)
                except ValueError:
                    _candidato_dm = None
                if _candidato_dm and _candidato_dm < _hoy_dt:
                    _candidato_dm = _dt_mod.date(_anio_dm + 1, _mes_dm, _dia_dm)
                if _candidato_dm:
                    _fecha_objetivo = _candidato_dm.strftime("%Y-%m-%d")
            except (KeyError, ValueError, Exception):
                pass
        # 1b) Solo mes mencionado: "para mayo", "en mayo", "mayo", "para junio"
        if not _fecha_objetivo:
          for _mes_nombre, _mes_num in _MESES_ES.items():
            if len(_mes_nombre) < 3:
                continue
            if (f" {_mes_nombre}" in f" {tl_norm_slot}"
                    or tl_norm_slot.startswith(_mes_nombre)
                    or tl_norm_slot.endswith(_mes_nombre)):
                _hoy_dt = datetime.now(_CHILE_TZ).date()
                _anio = _hoy_dt.year
                # A3: comparar desde el primer día del mes mencionado.
                # Antes comparaba solo mes+dia > 25, fallando cuando
                # el día actual era < 25 pero el mes ya pasó este año.
                import datetime as _dt_mod2
                _primer_dia_mes = _dt_mod2.date(_anio, _mes_num, 1)
                if _primer_dia_mes < _hoy_dt:
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
                    "no me acomoda", "cambiar dia", "cambiar día", "siguiente", "otro_dia",
                    # BUG-03: variantes texto libre
                    "otro día disponible", "siguiente dia", "siguiente día",
                    "buscar otro dia", "buscar otro día", "mañana otro dia",
                    # xfail rechazo_fecha — alias de rechazo libre de fecha
                    "no me sirve ese dia", "no me sirve ese día", "ese dia no",
                    "ese día no", "no puedo ese dia", "no puedo ese día",
                    "otro dia por favor", "otro día por favor"}
        if tl in OTRO_DIA or any(p in tl for p in ["otro dia", "otro día", "no puedo"]):
            # BUG-C: registrar slot sugerido como rechazado para no re-ofrecerlo
            if especialidad and slots_mostrados:
                _slot_rej = slots_mostrados[0]
                try:
                    registrar_slot_rechazado(
                        phone, especialidad,
                        _slot_rej.get("fecha", ""),
                        _slot_rej.get("hora_inicio", "")[:5],
                        _slot_rej.get("id_profesional"),
                    )
                except Exception:
                    pass
            # Blindar las llamadas a Medilink igual que las ramas hermanas
            # (ver_otros/ver_todos/dia_pedido). buscar_primer_dia hace `raise`
            # deliberado ante error de Medilink; sin captura subía al catch-all de
            # main.py que respondía "Tuve un problema técnico" + reset_session →
            # el paciente perdía TODO el contexto (bug 2026-06-23 otorrino).
            try:
                if especialidad in _ESP_MED_GENERAL:
                    smart_nuevo, todos_nuevo = await buscar_primer_dia(
                        especialidad, excluir=fechas_vistas, solo_ids=_MED_AO_IDS)
                    if not todos_nuevo:  # overflow a Márquez
                        smart_nuevo, todos_nuevo = await buscar_primer_dia(
                            especialidad, excluir=fechas_vistas, solo_ids=[_MED_OVERFLOW_ID])
                elif especialidad in _ESP_MED_FAMILIAR:
                    smart_nuevo, todos_nuevo = await buscar_primer_dia(
                        "medicina general", excluir=fechas_vistas, solo_ids=_MED_FAMILIAR_IDS)
                    for s in (todos_nuevo or []):
                        if isinstance(s, dict):
                            s["especialidad"] = "Medicina Familiar"
                else:
                    smart_nuevo, todos_nuevo = await buscar_primer_dia(
                        especialidad, excluir=fechas_vistas, intervalo_override=_maso_override)
            except Exception as _e_od:
                log_event(phone, "otro_dia_medilink_error",
                          {"especialidad": especialidad, "err": str(_e_od)[:120]})
                save_session(phone, "WAIT_SLOT", data)
                return (
                    "No pude consultar otras fechas en este momento 😕\n"
                    "Intenta de nuevo en unos segundos o llama a recepción: "
                    f"📞 *{CMC_TELEFONO}*"
                )
            if not todos_nuevo:
                data["waitlist_especialidad"] = especialidad
                data["waitlist_id_prof_pref"] = data.get("prof_sugerido_id")
                save_session(phone, "WAIT_WAITLIST_CONFIRM", data)
                return _btn_msg(
                    f"No encontré más disponibilidad para *{especialidad}* en los próximos días 😕\n\n"
                    "¿Quieres que te avise apenas se libere un cupo?",
                    [
                        {"id": "waitlist_si", "title": "📝 Sí, inscribirme"},
                        {"id": "waitlist_no", "title": "No, gracias"},
                    ]
                )
            # Bug 6 fix: filtrar slots de fecha pasada antes de presentar
            from datetime import datetime as _dt_b6
            from zoneinfo import ZoneInfo as _ZI_b6
            _hoy_b6 = _dt_b6.now(_ZI_b6("America/Santiago")).date().strftime("%Y-%m-%d")
            todos_nuevo = [s for s in todos_nuevo if (s.get("fecha") or "") >= _hoy_b6]
            smart_nuevo = [s for s in smart_nuevo if (s.get("fecha") or "") >= _hoy_b6]
            if not todos_nuevo:
                data["waitlist_especialidad"] = especialidad
                data["waitlist_id_prof_pref"] = data.get("prof_sugerido_id")
                save_session(phone, "WAIT_WAITLIST_CONFIRM", data)
                return _btn_msg(
                    f"No encontré más disponibilidad futura para *{especialidad}* 😕\n\n"
                    "¿Quieres que te avise apenas se libere un cupo?",
                    [
                        {"id": "waitlist_si", "title": "📝 Sí, inscribirme"},
                        {"id": "waitlist_no", "title": "No, gracias"},
                    ]
                )
            nueva_fecha = todos_nuevo[0]["fecha"]
            fechas_vistas = fechas_vistas + [nueva_fecha]
            data.update({"slots": smart_nuevo or todos_nuevo[:5], "todos_slots": todos_nuevo,
                         "fechas_vistas": fechas_vistas, "expansion_stage": 0})
            save_session(phone, "WAIT_SLOT", data)
            return _format_slots(smart_nuevo or todos_nuevo[:5])

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

        # ── BUG-E: Escape "no gracias / ya tengo / déjalo así" en WAIT_SLOT ──
        # Frases que indican que el paciente quiere salir SIN elegir nueva hora.
        # Distinto del "no" suelto (que sigue en el flujo mostrando alternativas).
        _tl_slot = txt.strip().lower()
        _NO_GRACIAS_ESCAPE = {
            "no gracias me quedo", "ya tengo", "ya tengo hora", "dejalo asi",
            "déjalo así", "dejalo así", "déjalo asi", "olvida", "mejor no",
            "no necesito hora", "no necesito", "ya no necesito", "ya tengo cita",
        }
        if (_tl_slot in _NO_GRACIAS_ESCAPE
                or any(_tl_slot.startswith(kw) for kw in _NO_GRACIAS_ESCAPE)):
            reset_session(phone)
            return (
                "Entendido. Tu cita anterior sigue activa.\n\n"
                "Escribe *menu* si necesitas algo más."
            )

        # ── "No" suelto en WAIT_SLOT → ofrecer alternativas (no confundir con negación real) ──
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
                f"📍 {_CMC_DIRECCION}\n\n"
                "_Seguimos con tu reserva: elige un número del listado o escribe *otro día*._"
            )

        # ── Pregunta por teléfono/dirección en WAIT_SLOT ──
        _INFO_CONTACTO = ("numero de contacto", "número de contacto", "telefono de contacto",
                          "teléfono de contacto", "a que numero", "a qué número",
                          "direccion del centro", "dirección del centro",
                          "donde queda", "dónde queda", "como llego", "cómo llego")
        if any(p in tl_norm_slot for p in _INFO_CONTACTO):
            return (
                f"📞 *{CMC_TELEFONO}* o ☎️ *(44) 296 5226*\n"
                f"📍 {_CMC_DIRECCION}.\n\n"
                "_Elige un número del listado, *ver todos* para más horarios, u *otro día*._"
            )

        # ── BUG-05 / P1-C: Pregunta de cobertura/modalidad en WAIT_SLOT ──
        # "Atiende con fonasa?", "cubre isapre?", "solo particular?" → responder
        # sin salir del flujo. Antes caía al fallback genérico y respondía la dirección.
        # P1-C fix: permitir respuesta aunque todos_slots esté vacío (usa especialidad
        # del contexto). Psiquiatría era el caso más común — paciente preguntaba
        # "¿no atiende por Fonasa?" y el bot respondía con la dirección del CMC.
        _COBERTURA_KW = (
            "fonasa", "isapre", "dipreca", "capredena", "particular",
            "bono", "cubre", "cobertura", "atiende con", "acepta",
        )
        _es_pregunta_cobertura = (
            any(k in tl_norm_slot for k in _COBERTURA_KW)
            and not tl_norm_slot.isdigit()
            and len(tl_norm_slot) >= 4
        )
        if _es_pregunta_cobertura and (todos_slots or especialidad):
            _esp_cob = (todos_slots[0].get("especialidad", especialidad) if todos_slots
                        else especialidad) or especialidad
            _slot_cob = todos_slots[0] if todos_slots else None
            _precio_cob = _precio_line(_esp_cob, _slot_cob) if _esp_cob else ""
            # Determinar modalidad. _FONASA_SPECIALTIES es Title Case → comparar lowercase.
            _esp_cob_lower = _esp_cob.lower()
            _es_solo_particular = not any(
                _fsp.lower() == _esp_cob_lower for _fsp in _FONASA_SPECIALTIES
            )
            if _es_solo_particular:
                # Incluir "No atiende por Fonasa" explícitamente (caso psiquiatría IG).
                _resp_cob = (
                    "*{esp}* no atiende por Fonasa en el CMC.\n"
                    "Es atención *solo Particular*.{precio}\n\n"
                    "\u00bfTe sirve el horario? Elige un n\u00famero para reservar."
                ).format(
                    esp=str(_esp_cob or especialidad),
                    precio=("\n" + _precio_cob) if _precio_cob else "",
                )
            else:
                _resp_cob = (
                    "*{esp}* acepta *Fonasa* (bono MLE) y *Particular*.{precio}\n\n"
                    "\u00bfTe sirve el horario? Elige un n\u00famero para reservar."
                ).format(
                    esp=str(_esp_cob or especialidad),
                    precio=("\n" + _precio_cob) if _precio_cob else "",
                )
            save_session(phone, "WAIT_SLOT", data)
            return _resp_cob

        # ── Apellido específico mencionado ("con el dr marquez", "quiero con abarca") ──
        # PRIORIDAD MÁXIMA: si el paciente pide un doctor por nombre, filtramos
        # slots actuales a ese profesional o lanzamos búsqueda fresca con él.
        # Evita loop donde el paciente pedía Márquez y el bot ofrecía Olavarría.
        _apellido_slot = _detectar_apellido_profesional(txt) if tl != "otro_prof" else None
        if _apellido_slot:
            from medilink import _ids_para_especialidad
            ids_apellido = set(_ids_para_especialidad(_apellido_slot))
            if ids_apellido:
                # PROFESIONALES no está importado en el scope de handle_message:
                # usarlo crudo abajo (6959/6966) lanzaba NameError → "Tuve un
                # problema técnico" + reset (66 casos en logs 2026-06-08/09).
                # Alias propio para no convertir PROFESIONALES en local de toda
                # la función (evita el UnboundLocalError de _responder_pregunta_horario).
                from medilink import PROFESIONALES as _PROFS_AP
                slots_de_ese = [s for s in todos_slots if s.get("id_profesional") in ids_apellido]
                if slots_de_ese:
                    # Bug 7 fix: si el texto también menciona una hora (ej. "Andrés Abarca
                    # a las 12:45"), buscarla en los slots del profesional y confirmar directo.
                    import re as _re_b7
                    _hm_b7 = _re_b7.search(r'\b(\d{1,2})[:\.](\d{2})\b', tl_norm_slot)
                    if _hm_b7:
                        _h7 = int(_hm_b7.group(1))
                        _m7 = int(_hm_b7.group(2))
                        _hora_buscada_b7 = f"{_h7:02d}:{_m7:02d}"
                        _slot_b7 = next(
                            (s for s in slots_de_ese
                             if s.get("hora_inicio", "")[:5] == _hora_buscada_b7),
                            None
                        )
                        if _slot_b7:
                            log_event(phone, "slot_hora_apellido_autoconfirm",
                                      {"hora": _hora_buscada_b7, "apellido": _apellido_slot})
                            return await _slot_confirmed(phone, data, _slot_b7)
                    data["slots"] = slots_de_ese[:10]
                    data["prof_sugerido_id"] = slots_de_ese[0].get("id_profesional")
                    _pv = set(data.get("profs_vistos", []))
                    _pv.update(ids_apellido)
                    data["profs_vistos"] = list(_pv)
                    save_session(phone, "WAIT_SLOT", data)
                    return _format_slots(slots_de_ese[:10], mostrar_todos=True)
                # Sin slots de ese profesional en el día actual.
                # FIX-4: intentar primero en la fecha ya mostrada (fecha_actual)
                # para no corromper la fecha en la confirmación. Si no hay slots
                # en esa fecha, buscar el primer día disponible preservando la
                # sesión (no reset_session) para que la fecha no se recalcule
                # desde cero con otro path.
                if fecha_actual:
                    try:
                        _sm_ap, _td_ap = await buscar_slots_dia_por_ids(
                            list(ids_apellido), fecha_actual)
                        if _td_ap:
                            data["slots"] = _td_ap[:5]
                            data["todos_slots"] = _td_ap
                            data["prof_sugerido_id"] = _td_ap[0].get("id_profesional")
                            _pv2 = set(data.get("profs_vistos", []))
                            _pv2.update(ids_apellido)
                            data["profs_vistos"] = list(_pv2)
                            save_session(phone, "WAIT_SLOT", data)
                            _nombre_ap = _PROFS_AP.get(list(ids_apellido)[0], {}).get("nombre", "ese doctor")
                            # F030: calcular _format_slots UNA vez; si es dict (interactivo) devolverlo
                            # directo (ya trae los slots); si es str, adjuntar el encabezado.
                            _fmt_ap = _format_slots(_td_ap[:5])
                            if isinstance(_fmt_ap, dict):
                                return _fmt_ap
                            return f"Encontré horas con *{_nombre_ap}* para el mismo día:\n\n{_fmt_ap}"
                    except Exception as _e_ap:
                        log.warning("buscar_slots_dia_por_ids apellido mismo día falló: %s", _e_ap)
                # Fallback: búsqueda fresca preservando especialidad y fechas_vistas
                _esp_ap = _PROFS_AP.get(list(ids_apellido)[0], {}).get("especialidad", especialidad).lower() if ids_apellido else especialidad
                try:
                    _sm_ap2, _td_ap2 = await buscar_primer_dia(
                        _esp_ap, excluir=fechas_vistas, solo_ids=list(ids_apellido))
                    if _td_ap2:
                        _nf_ap = _td_ap2[0].get("fecha")
                        _fv_ap = list(fechas_vistas) + ([_nf_ap] if _nf_ap and _nf_ap not in fechas_vistas else [])
                        data.update({"slots": _td_ap2[:5], "todos_slots": _td_ap2,
                                     "fechas_vistas": _fv_ap,
                                     "prof_sugerido_id": _td_ap2[0].get("id_profesional"),
                                     "especialidad": _esp_ap})
                        save_session(phone, "WAIT_SLOT", data)
                        return _format_slots(_td_ap2[:5])
                except Exception as _e_ap2:
                    log.warning("buscar_primer_dia apellido fallback falló: %s", _e_ap2)
                # Sin disponibilidad alguna: avisar y mantener sesión
                save_session(phone, "WAIT_SLOT", data)
                return (
                    f"No encontré disponibilidad con ese profesional en los próximos días 😕\n\n"
                    "Escribe *otro profesional* o *otro día* para continuar."
                )

        # ── Intento de cambio de profesional por lenguaje natural ──
        # "no quiero ese profesional", "con otro doctor", "no me gusta", etc.
        _OTRO_PROF_PHRASES = (
            "no quiero ese", "no me gusta", "otro doctor", "otro profesional",
            "otra doctora", "otro médico", "otro medico", "con otro",
            "con otra", "cambiar doctor", "cambiar profesional",
            "no ese", "no ese doctor", "prefiero otro",
            # xfail sinonimo_otro_doctor — variantes text-libre
            "quiero otro doctor", "quiero otra doctora", "quiero otro médico",
            "quiero otro medico", "quiero otro profesional",
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
            try:
                smart_dia, todos_dia = await _buscar_slots_dia_con_retry(
                    especialidad, _DIA_RELATIVO, intervalo_override=_maso_override)
            except Exception as _e_dia_rel:
                log.warning("buscar_slots_dia DIA_RELATIVO falló tras retries: %s", _e_dia_rel)
                smart_dia, todos_dia = [], []
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
                # Bug 7 fix: si el slot coincide exactamente (delta==0) o está en la
                # lista mostrada, confirmar directamente en vez de re-mostrar.
                # Cubre "11:45 si me sirve", "a las 12:45", "las 10:00 me sirve".
                if delta == 0 or best_slot in slots_mostrados:
                    return await _slot_confirmed(phone, data, best_slot)
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
                # Inyectar el header en el body de la lista interactiva en vez
                # de mandar dos mensajes (el segundo send_whatsapp se perdía
                # silenciosamente para usuarios IG/FB cuyos phones empiezan
                # con `fb_`/`ig_` y no son números WhatsApp válidos — caso
                # 2026-04-30 fb_9644586545608248 con "A las 11-15", el bot
                # mostraba slots sin avisar que no había hora exacta).
                try:
                    _body = _slot_resp_c.get("interactive", {}).get("body", {})
                    _orig = _body.get("text", "")
                    _body["text"] = (_hdr + "\n\n" + _orig)[:1024]
                except Exception:
                    await send_whatsapp(phone, _hdr)
                    from session import log_message as _lm_f5
                    _lm_f5(phone, "out", _hdr, "WAIT_SLOT")
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

        # BUG-2 FIX: Detectar "para mi bebé/niño/guagua/etc." en WAIT_SLOT también.
        # Si el paciente menciona un menor, redirigir al flujo de terceros en vez
        # de interpretar el número embebido como selección de slot.
        _OTRA_PERSONA_SLOT_RE = re.compile(
            r"\b(otra persona|otr[oa] familiar|mi esposo|mi esposa|"
            r"mi hijo|mi hija|mi mam[aá]|mi pap[aá]|mi hermano|mi hermana|"
            r"mi abuelo|mi abuela|mi pololo|mi polola|mi pareja|mi nieto|"
            r"mi nieta|un familiar|para un amigo|para una amiga|"
            r"mi beb[eé]|mi guagua|mi niñ[oa]|mi niet[oa]|mi chic[oa]|"
            r"mi pequeñ[oa]|"
            r"para mi beb[eé]|para mi guagua|para mi niñ[oa]|"
            r"para mi (?:hijo|hija|mam[aá]|pap[aá]|hermano|hermana|"
            r"abuelo|abuela|esposo|esposa|pareja|nieto|nieta|"
            r"beb[eé]|guagua|niñ[oa]|chic[oa]|pequeñ[oa]))\b",
            re.IGNORECASE,
        )
        if _OTRA_PERSONA_SLOT_RE.search(tl_norm_slot):
            data["booking_for_other"] = True
            save_session(phone, "WAIT_SLOT", data)
            return _btn_msg(
                "Entendido, es para otra persona 😊\n\n¿La atención será *Fonasa* o *Particular*?",
                [{"id": "1", "title": "Fonasa"},
                 {"id": "2", "title": "Particular"}]
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

        # BUG-05: Bypass determinístico de precio en WAIT_SLOT con especialidad activa.
        # FIX 7: pregunta sobre el procedimiento con slot activo de ginecología/eco.
        # Si el paciente pregunta por transvaginal/pélvica/mamaria/etc. y ya hay
        # un slot de ginecología o ecografía activo, responder contextualmente
        # y re-ofrecer confirmar sin perder el slot.
        _PROC_KW = (
            "transvaginal", "transvajinal", "pelvica", "pélvica",
            "mamaria", "mamas", "mama", "obstetrica", "obstétrica",
            "realiza", "hacen", "hace ", "tienen", "ofrece",
        )
        _esp_slot_activo = (todos_slots[0].get("especialidad") if todos_slots else especialidad or "").lower()
        _es_slot_gineco_eco = any(k in _esp_slot_activo for k in ("ginec", "ecograf", "matr"))
        if (
            idx is None
            and _es_slot_gineco_eco
            and any(k in tl_norm_slot for k in _PROC_KW)
            and len(tl_norm_slot) >= 5
        ):
            # FIX exactitud médica: NO atribuir eco ginecológica/obstétrica a David
            # Pardo. Routing real: transvaginal/pélvica/ovarios/útero → Dr. Tirso
            # Rejón (Ginecología); mamaria/abdominal/partes blandas → David Pardo
            # (Ecografía); obstétrica de embarazo → NO se realiza en el CMC.
            _GINECO_KW_SLOT = ("transvaginal", "transvajinal", "intravaginal",
                               "endovaginal", "pelvica", "pélvica", "obstetrica",
                               "obstétrica", "ovario", "utero", "útero")
            _pidio_gineco = any(k in tl_norm_slot for k in _GINECO_KW_SLOT)
            _slot_es_eco = "ecograf" in _esp_slot_activo
            _slot_es_gineco = "ginec" in _esp_slot_activo

            # Caso crítico: pide eco GINECOLÓGICA pero el slot activo es de Pardo
            # (Ecografía) → él NO la hace. Redirigir a Ginecología en vez de ofrecer
            # confirmar una hora equivocada.
            if _pidio_gineco and _slot_es_eco:
                log_event(phone, "eco_gineco_redirect_pardo_a_rejon", {"texto": tl_norm_slot[:80]})
                return await _iniciar_agendar(
                    phone, {}, "Ginecología",
                    saludo_prefix=(
                        "La ecografía *ginecológica* (transvaginal/pélvica) la realiza "
                        "el *Dr. Tirso Rejón* en *Ginecología* ($35.000), no David "
                        "Pardo. La eco *obstétrica* de embarazo no se realiza en el "
                        "CMC.\n\nTe busco hora con el Dr. Rejón 👇"
                    ),
                )

            # El slot activo SÍ corresponde a lo que pide. Descripción específica
            # por especialidad (sin mezclar ámbitos).
            _proc_slot = todos_slots[0] if todos_slots else {}
            _prof_slot = _proc_slot.get("profesional", "el profesional")
            _esp_slot_disp = _proc_slot.get("especialidad", "").capitalize() or especialidad.capitalize()
            _precio_proc = _precio_line(_esp_slot_activo) or ""
            if _slot_es_gineco:
                _incluye = ("incluyendo ecografías ginecológicas (transvaginal y "
                            "pélvica). La eco obstétrica de embarazo no se realiza en el CMC.")
            elif _slot_es_eco:
                _incluye = ("incluyendo ecografías generales: abdominal, mamaria, "
                            "tiroidea, renal y de partes blandas.")
            else:
                _incluye = ""
            _resp_proc = (
                f"*{_prof_slot}* atiende {_esp_slot_disp} en el CMC"
                + (f", {_incluye}" if _incluye else ".")
                + (f"\n{_precio_proc}" if _precio_proc else "")
            )
            save_session(phone, "WAIT_SLOT", data)
            return _btn_msg(
                f"{_resp_proc}\n\n¿Continuamos con tu reserva?",
                [
                    {"id": "confirmar_sugerido", "title": "✅ Sí, reservar"},
                    {"id": "otro_dia", "title": "📅 Otro horario"},
                ]
            )

        # Sin esto, inputs cortos como "precio" o "cuánto" pasan a detect_intent que
        # puede retornar intent != "precio" y la respuesta es inconsistente (FAQ genérica
        # o "comunícate con recepción"). Con especialidad activa: siempre precio directo.
        _PRECIO_KW_SLOT = ("precio", "cuánto", "cuanto", "vale", "cuesta", "costo",
                           "valor", "bono", "cobran", "cobra")
        if idx is None and any(k in tl_norm_slot for k in _PRECIO_KW_SLOT):
            _esp_precio = todos_slots[0]["especialidad"] if todos_slots else especialidad
            if _esp_precio:
                _pid_ws = (todos_slots[0].get("id_profesional") if todos_slots else None) or data.get("prof_sugerido_id")
                _precio_resp = _precio_line(_esp_precio, id_profesional=_pid_ws)
                if _precio_resp:
                    save_session(phone, "WAIT_SLOT", data)
                    return (
                        f"{_precio_resp}\n\n"
                        "_Elige un número para continuar con tu reserva o escribe *menu* para volver._"
                    )
            # Sin precio en tabla → respuesta_faq con contexto de especialidad
            _consulta_precio = f"¿Cuánto cuesta una consulta de {_esp_precio}?" if _esp_precio else txt
            try:
                _resp_p = await respuesta_faq(_consulta_precio)
            except Exception:
                _resp_p = f"Para precios comunícate con recepción: 📞 *{CMC_TELEFONO}*"
            save_session(phone, "WAIT_SLOT", data)
            return (
                f"{_resp_p}\n\n"
                "_Elige un número para continuar con tu reserva o escribe *menu* para volver._"
            )

        # ── Negativa explícita al slot ofrecido → mostrar otros slots ──
        # Paciente rechaza el horario con lenguaje libre antes de llegar a Claude.
        # Sin esto, detect_intent derivaba a WAIT_MODALIDAD mostrando el mismo slot.
        _NEGATIVAS_SLOT = (
            "no puedo", "no me sirve", "otra hora", "otro horario",
            "otro día", "otro dia", "más tarde", "mas tarde",
            "más temprano", "mas temprano", "no ese", "ese no",
            "cambiar hora", "cambiar el horario", "no me acomoda",
            "no me queda", "no me viene", "no tengo tiempo",
        )
        if idx is None and any(neg in tl_norm_slot for neg in _NEGATIVAS_SLOT):
            log_event(phone, "slot_rechazado_texto_libre", {"raw_text": txt[:200]})
            fechas_vistas_neg = data.get("fechas_vistas", [])
            if not isinstance(fechas_vistas_neg, list):
                fechas_vistas_neg = list(fechas_vistas_neg)
            _maso_ov_neg = {59: data["maso_duracion"]} if especialidad == "masoterapia" and data.get("maso_duracion") else None
            try:
                smart_neg, todos_neg = await buscar_primer_dia(
                    especialidad, excluir=fechas_vistas_neg)
            except Exception:
                smart_neg, todos_neg = [], []
            if todos_neg:
                nueva_fecha_neg = todos_neg[0].get("fecha")
                if nueva_fecha_neg and nueva_fecha_neg not in fechas_vistas_neg:
                    fechas_vistas_neg.append(nueva_fecha_neg)
                data.update({
                    "slots": (smart_neg or todos_neg)[:5],
                    "todos_slots": todos_neg,
                    "fechas_vistas": fechas_vistas_neg,
                    "expansion_stage": 0,
                })
                save_session(phone, "WAIT_SLOT", data)
                return _format_slots((smart_neg or todos_neg)[:5], mostrar_todos=False)
            # FIX 2: ofrecer waitlist con botones, no instrucción de texto libre
            _wl_esp_neg = especialidad or data.get("especialidad", "")
            data["waitlist_especialidad"] = _wl_esp_neg
            data["waitlist_id_prof_pref"] = data.get("prof_sugerido_id")
            save_session(phone, "WAIT_WAITLIST_CONFIRM", data)
            return _btn_msg(
                f"No hay más horarios disponibles para *{_wl_esp_neg or 'esta especialidad'}* en los próximos días 😕\n\n"
                "¿Quieres que te avise apenas se libere un cupo?",
                [
                    {"id": "waitlist_si", "title": "📝 Sí, inscribirme"},
                    {"id": "waitlist_no", "title": "No, gracias"},
                ]
            )

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
                    # BUG-7: mostrar slots de la misma franja horaria en vez de
                    # solo listar texto. "10" → mostrar 10:20, 10:40 como opciones.
                    _h_int_ped = int(_hora_match.group(1))
                    _slots_franja = [
                        s for s in todos_slots
                        if s.get("hora_inicio", "")[:2].lstrip("0") == str(_h_int_ped)
                        or s.get("hora_inicio", "")[:2] == f"{_h_int_ped:02d}"
                    ]
                    if _slots_franja:
                        data["slots"] = _slots_franja[:10]
                        save_session(phone, "WAIT_SLOT", data)
                        _hdr7 = (
                            f"A las *{h_pedida}:00* en punto no tengo, pero sí cerca:\n\n"
                        )
                        _fmt7 = _format_slots(_slots_franja[:10], mostrar_todos=True)
                        if isinstance(_fmt7, dict):
                            try:
                                _body7 = _fmt7.get("interactive", {}).get("body", {})
                                _orig7 = _body7.get("text", "")
                                _body7["text"] = (_hdr7 + _orig7)[:1024]
                            except Exception:
                                pass
                            return _fmt7
                        return _hdr7 + _fmt7
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
            # M1: funnel — el paciente llegó aquí sin elegir slot (rechazo implícito)
            log_event(phone, "funnel_slot_rechazado", {
                "esp": especialidad,
                "paso": "slot_no_elegido",
                "txt": txt[:80],
            })
            # M2: Rescate de slot rechazado — detectar negación explícita y ofrecer
            # botones de rescate en vez del mensaje genérico "no te entendí".
            # Fail-safe total: cualquier error vuelve al comportamiento anterior.
            try:
                _NEGACION_SLOT_KW = (
                    "no", "nop", "no me sirve", "no me acomoda", "no me queda",
                    "no puedo", "no puedo a esa hora", "no puedo ir",
                    "muy tarde", "muy temprano", "no me viene",
                    "no me viene bien", "no quiero", "no me interesa",
                    "no por ahora", "ahora no", "despues", "después",
                    "no esa hora", "no ese dia", "no ese día",
                    "no me sirve ese", "no alcanza", "no llego",
                )
                _es_negacion_slot = (
                    tl_norm_slot in ("no", "nop", "nope")
                    or any(k in tl_norm_slot for k in _NEGACION_SLOT_KW)
                )
                if _es_negacion_slot and slots_mostrados:
                    try:
                        from medilink import _ids_para_especialidad as _ids_rescate
                        _ids_rescate_esp = _ids_rescate(especialidad)
                        if especialidad in _ESP_MED_GENERAL:
                            _ids_rescate_esp = list(_MED_GENERAL_IDS)
                        _hay_otro_prof = len([i for i in _ids_rescate_esp
                                              if i != data.get("prof_sugerido_id")]) > 0
                    except Exception:
                        _hay_otro_prof = False
                    _botones_rescate = [{"id": "otro_dia", "title": "Otro dia"}]
                    if _hay_otro_prof:
                        _botones_rescate.append({"id": "otro_prof", "title": "Otro profesional"})
                    _botones_rescate.append({"id": "accion_recepcion", "title": "Llamar a recepcion"})
                    log_event(phone, "funnel_rescate_ofrecido", {
                        "esp": especialidad,
                        "hay_otro_prof": _hay_otro_prof,
                        "txt": txt[:80],
                    })
                    save_session(phone, "WAIT_SLOT", data)
                    return _btn_msg(
                        "Sin problema 😊 ¿Qué prefieres?",
                        _botones_rescate,
                    )
            except Exception as _e_rescate:
                log.debug("M2 rescate slot error (ignorado): %s", _e_rescate)
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
            # FIX-11: Frustration detector — escalada en 3 niveles.
            # Nivel 2: ofrecer botón de recepción explícito.
            data["intentos_fallidos"] = data.get("intentos_fallidos", 0) + 1
            intentos = data["intentos_fallidos"]
            if intentos >= 3:
                return _derivar_humano(phone=phone, contexto="frustración WAIT_SLOT")
            save_session(phone, "WAIT_SLOT", data)
            if intentos == 2:
                return _btn_msg(
                    "Todavía no logro entenderte 😕\n\n"
                    "Elige una opción o escribe el *número* del horario:",
                    [
                        {"id": "ver_todos",       "title": "📋 Ver todos los horarios"},
                        {"id": "otro_dia",        "title": "📅 Buscar otro día"},
                        {"id": "accion_recepcion","title": "💬 Hablar con recepción"},
                    ]
                )
            # ── Detector de preferencia temporal antes del fallback ──
            # Caso real 56961986439 2026-05-03: paciente en WAIT_SLOT escribió
            # "necesito atención para hoy" y bot respondió genérico "no te entendí".
            # Hoy es domingo: el bot debe avisar que no atienden hoy y ofrecer
            # mañana, no quedarse callado.
            _TEMP_HOY = re.compile(r"\b(hoy|hoy mismo|para hoy|ahora|ya mismo|esta tarde|esta noche|esta mañana)\b", re.IGNORECASE)
            _TEMP_MAÑANA = re.compile(r"\bma[ñn]ana\b", re.IGNORECASE)
            _TEMP_URGENTE = re.compile(r"\b(urgente|de urgencia|emergencia|me siento mal|me duele mucho)\b", re.IGNORECASE)
            if _TEMP_URGENTE.search(tl):
                log_event(phone, "wait_slot_urgencia_detectada", {"txt": txt[:80]})
                return (
                    "⚠️ Si es una *urgencia médica*, llama al *SAMU 131* o ve directamente al *Hospital de Arauco*.\n\n"
                    "Si quieres agendar una hora regular, escribe el *número* del horario o *otro día*."
                )
            if _TEMP_HOY.search(tl):
                from datetime import datetime as _dt_h
                _hoy = _dt_h.now(_CHILE_TZ)
                _dow = _hoy.weekday()  # 0=lunes ... 6=domingo
                if _dow == 6:  # domingo
                    log_event(phone, "wait_slot_hoy_domingo", {})
                    return (
                        "Hoy es *domingo* y no atendemos 😕\n\n"
                        "Tenemos horas disponibles desde *mañana lunes*. ¿Te muestro?\n\n"
                        "Escribe *otro día* para ver opciones o el *número* del horario que ya te ofrecí."
                    )
                if _dow == 5:  # sábado
                    if _hoy.hour >= 14:
                        log_event(phone, "wait_slot_hoy_sabado_tarde", {})
                        return (
                            "Hoy sábado ya cerramos 😕\n\n"
                            "Te puedo agendar *desde el lunes*. Escribe *otro día* para ver opciones."
                        )
                # Día hábil: avisar que vamos a buscar para hoy
                log_event(phone, "wait_slot_hoy_pedido", {"hora_actual": _hoy.strftime("%H:%M")})
                return (
                    "Veo que necesitas hora *para hoy*. Las horas que te mostré arriba son las próximas disponibles.\n\n"
                    "Si necesitas algo *más tarde hoy*, escribe *ver todos* y revisamos la agenda completa de hoy.\n\n"
                    "Si es *urgente*, llama al *(44) 296 5226* o escribe *humano* y te conecto con recepción."
                )
            if _TEMP_MAÑANA.search(tl):
                log_event(phone, "wait_slot_manana_pedido", {})
                return (
                    "Veo que prefieres mañana 😊\n\n"
                    "Escribe *otro día* y te muestro horarios para mañana específicamente."
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
        # Detectar frases libres de modalidad antes del chequeo de precio
        # para que "el bono es particular" no caiga en el handler de precio.
        _es_particular_libre = (
            "particular" in tl
            or "privad" in tl
        )
        _es_fonasa_libre = (
            "fonasa" in tl
            or "bono fonasa" in tl
        )
        if _es_fonasa_libre and not _es_particular_libre:
            data["modalidad"] = "fonasa"
        elif _es_particular_libre:
            data["modalidad"] = "particular"
        elif tl in FONASA or tl_norm in FONASA:
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
            # Negativa al slot en WAIT_MODALIDAD → volver a mostrar otros slots
            # Cubre: paciente llegó a WAIT_MODALIDAD pero quiere cambiar la hora.
            _NEGATIVAS_MODAL = (
                "no puedo", "no me sirve", "otra hora", "otro horario",
                "otro día", "otro dia", "más tarde", "mas tarde",
                "más temprano", "mas temprano", "cambiar hora",
                "no me acomoda", "no me queda",
            )
            if any(neg in tl for neg in _NEGATIVAS_MODAL):
                log_event(phone, "slot_rechazado_texto_libre", {"raw_text": txt[:200], "from_state": "WAIT_MODALIDAD"})
                _esp_modal_neg = data.get("especialidad", "")
                _fv_modal_neg = data.get("fechas_vistas", [])
                if not isinstance(_fv_modal_neg, list):
                    _fv_modal_neg = list(_fv_modal_neg)
                _maso_ov_modal = {59: data["maso_duracion"]} if _esp_modal_neg == "masoterapia" and data.get("maso_duracion") else None
                try:
                    smart_mn, todos_mn = await buscar_primer_dia(
                        _esp_modal_neg, excluir=_fv_modal_neg)
                except Exception:
                    smart_mn, todos_mn = [], []
                if todos_mn:
                    _nf_mn = todos_mn[0].get("fecha")
                    if _nf_mn and _nf_mn not in _fv_modal_neg:
                        _fv_modal_neg.append(_nf_mn)
                    data.update({
                        "slots": (smart_mn or todos_mn)[:5],
                        "todos_slots": todos_mn,
                        "fechas_vistas": _fv_modal_neg,
                        "expansion_stage": 0,
                    })
                    save_session(phone, "WAIT_SLOT", data)
                    return _format_slots((smart_mn or todos_mn)[:5], mostrar_todos=False)
                # FIX 2: ofrecer waitlist con botones, no instrucción de texto libre
                # F031: `especialidad` puede no estar definida si se llega a WAIT_MODALIDAD
                # sin pasar por WAIT_SLOT (ej: restauración de sesión tras timeout).
                _wl_esp_mn = _esp_modal_neg or data.get("especialidad", "")
                data["waitlist_especialidad"] = _wl_esp_mn
                data["waitlist_id_prof_pref"] = data.get("prof_sugerido_id")
                save_session(phone, "WAIT_WAITLIST_CONFIRM", data)
                return _btn_msg(
                    f"No encontré más horarios disponibles para *{_wl_esp_mn or 'esta especialidad'}* 😕\n\n"
                    "¿Quieres que te avise apenas se libere un cupo?",
                    [
                        {"id": "waitlist_si", "title": "📝 Sí, inscribirme"},
                        {"id": "waitlist_no", "title": "No, gracias"},
                    ]
                )
            # BUG-03: Escape explícito "no quiero" / "cancelar" / "salir" → reset limpio
            _NO_QUIERO_KW = ("no quiero", "no quero", "ya no quiero", "ya no", "no gracias",
                             "cancelar", "cancel", "salir", "no necesito", "dejalo", "déjalo",
                             "olvida", "olvidalo", "olvídalo", "no importa", "nada")
            if any(k in tl for k in _NO_QUIERO_KW) or tl in NEGACIONES:
                reset_session(phone)
                return (
                    "Entendido, no hay problema 😊\n\n"
                    "_Escribe *menu* si necesitas algo más._"
                )
            # BUG-03: Pregunta de precio → responder con precio de la especialidad activa
            _PRECIO_KW = ("precio", "cuánto", "cuanto", "vale", "cuesta", "costo", "valor", "bono")
            if any(k in tl for k in _PRECIO_KW):
                esp_modal = data.get("especialidad", "")
                if esp_modal:
                    _pid_modal = data.get("prof_sugerido_id") or (data.get("slot_elegido") or {}).get("id_profesional")
                    precio_l = _precio_line(esp_modal, id_profesional=_pid_modal)
                    if precio_l:
                        save_session(phone, "WAIT_MODALIDAD", data)
                        return _btn_msg(
                            f"{precio_l}\n\n¿La atención será *Fonasa* o *Particular*?",
                            [{"id": "1", "title": "Fonasa"},
                             {"id": "2", "title": "Particular"}]
                        )
                # Sin especialidad conocida: derivar a FAQ
                try:
                    resp_faq = await respuesta_faq(txt)
                except Exception:
                    resp_faq = f"Para precios comunícate con recepción: 📞 *{CMC_TELEFONO}*"
                save_session(phone, "WAIT_MODALIDAD", data)
                return _btn_msg(
                    f"{resp_faq}\n\n¿La atención será *Fonasa* o *Particular*?",
                    [{"id": "1", "title": "Fonasa"},
                     {"id": "2", "title": "Particular"}]
                )
            # FIX (2026-07-01): reacción corta de sorpresa/entusiasmo ("¿será
            # posible?!", "en serio?!", "de verdad?!", "genial!", "que bueno")
            # NO es una pregunta informativa. Mandarla a respuesta_faq() hace que
            # Haiku devuelva un "No entiendo bien tu pregunta…" confuso (caso real:
            # paciente reacciona al slot ofrecido con "Será posible ?!?!!"). Mejor
            # confirmar con calidez y re-pedir la modalidad, sin gastar un llamado
            # a Claude. Conservador: solo ≤4 palabras + regex de reacción (una
            # pregunta real como "será posible pagar con tarjeta?" tiene >4 y pasa).
            _REACCION_CORTA_RE = re.compile(
                r"^\W*(ser[áa]\s+posible|posible|en\s+serio|enserio|de\s+verdad"
                r"|deverdad|wow|gua+u|genial|buen[íi]simo|excelente|perfecto"
                r"|que\s+bueno|qu[ée]\s+bueno|incre[íi]ble|no\s+puede\s+ser)\b",
                re.IGNORECASE,
            )
            if len(tl.split()) <= 4 and _REACCION_CORTA_RE.search(tl.strip()):
                save_session(phone, "WAIT_MODALIDAD", data)
                return _btn_msg(
                    "¡Sí! 🙌 Para reservarte el horario, dime cómo será la atención:",
                    [{"id": "1", "title": "Fonasa"},
                     {"id": "2", "title": "Particular"}]
                )
            # BUG-03: Pregunta libre (contiene "?") → responder y volver a pedir modalidad
            if "?" in txt and len(txt) >= 5:
                try:
                    resp_faq = await respuesta_faq(txt)
                except Exception:
                    resp_faq = f"Para más información llama a recepción: 📞 *{CMC_TELEFONO}*"
                save_session(phone, "WAIT_MODALIDAD", data)
                return _btn_msg(
                    f"{resp_faq}\n\n¿La atención será *Fonasa* o *Particular*?",
                    [{"id": "1", "title": "Fonasa"},
                     {"id": "2", "title": "Particular"}]
                )
            # Escape: menciona "otra persona" → saltar a flujo de terceros
            # Regex con word-boundary evita matchear "para otro DÍA" o
            # "para otra CITA". Caso real 2026-04-21 (56982709417): "necesito
            # una hora para otro día" → bot decía "Entendido, es para otra persona".
            # BUG-1 FIX: ampliado con suegra/cuñado/sobrina/tía/vecino/yerno/pololo
            # _OTRA_PERSONA_RE definido a nivel de módulo (bug fix 2026-05-18)
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
        # Saltar WAIT_BOOKING_FOR → ir directo al RUT (si quiere para otro, escribe "otra persona").
        # Excepción: si booking_for_other ya era True (viene de cd_persona sin modalidad previa),
        # preservar el flag en vez de pisarlo — de lo contrario el paciente nuevo quedaba como propio.
        if not data.get("booking_for_other"):
            data["booking_for_other"] = False

        # ── Roster de dependientes ──────────────────────────────────────────
        # Si el dueño del cel tiene familiares registrados, ofrecer lista en vez
        # de pedir RUT directo. Aplica tanto para "booking_for_other" True como False.
        _owner_profile = get_profile(phone)
        _owner_rut = (_owner_profile or {}).get("rut") or ""
        _deps: list[dict] = []
        if _owner_rut:
            try:
                _deps = list_family_links(_owner_rut)
            except Exception as _deps_err:
                log.debug("list_family_links error (ignorado): %s", _deps_err)

        if data.get("booking_for_other"):
            # Tercero explícito: limpiar datos propios y ofrecer roster (si existe)
            data.pop("rut_conocido", None)
            data.pop("nombre_conocido", None)
            if _deps:
                _deps_mostrar = _deps[:8]  # máx 8 + "Otra persona"
                _rows_deps = [
                    {
                        "id": f"dep_{d['dependent_rut']}",
                        "title": _first_name(d["dependent_nombre"])[:24],
                        "description": (d.get("relation") or "familiar")[:72],
                    }
                    for d in _deps_mostrar
                ]
                _rows_deps.append({"id": "dep_nuevo", "title": "Otra persona"})
                data["_deps_roster"] = [d["dependent_rut"] for d in _deps_mostrar]
                save_session(phone, "WAIT_BOOKING_WHO", data)
                return _list_msg(
                    body_text=f"Perfecto, atención *{modalidad_str}*. ¿Para quién es la hora?",
                    button_label="Seleccionar",
                    sections=[{"title": "Familiares registrados", "rows": _rows_deps}],
                )
            # Sin dependientes: comportamiento original (pedir RUT del paciente real)
        else:
            # Para sí mismo: si tiene dependientes, ofrecer también la opción
            if _deps and _owner_rut:
                _deps_mostrar = _deps[:8]
                _rows_deps = [{"id": "dep_self", "title": "Para mí"}]
                _rows_deps += [
                    {
                        "id": f"dep_{d['dependent_rut']}",
                        "title": _first_name(d["dependent_nombre"])[:24],
                        "description": (d.get("relation") or "familiar")[:72],
                    }
                    for d in _deps_mostrar
                ]
                _rows_deps.append({"id": "dep_nuevo", "title": "Otra persona"})
                data["_deps_roster"] = [d["dependent_rut"] for d in _deps_mostrar]
                save_session(phone, "WAIT_BOOKING_WHO", data)
                return _list_msg(
                    body_text=f"Perfecto, atención *{modalidad_str}*. ¿Para quién es la hora?",
                    button_label="Seleccionar",
                    sections=[{"title": "¿Para quién?", "rows": _rows_deps}],
                )
            # Sin dependientes: comportamiento original (atajo con datos propios)
        # ── fin roster dependientes ─────────────────────────────────────────

        # Atajo para pacientes conocidos
        rut_c = data.get("rut_conocido")
        nombre_c = data.get("nombre_conocido")
        if rut_c and nombre_c and not data.get("booking_for_other"):
            save_session(phone, "WAIT_RUT_AGENDAR", data)
            return _btn_msg(
                f"Perfecto, atención *{modalidad_str}* 😊\n\n"
                f"¿Agendo con tus datos, *{_first_name(nombre_c)}*?",
                [{"id": "si", "title": "✅ Sí, continuar"},
                 {"id": "rut_nuevo", "title": "Ingresar otro RUT"}]
            )

        # ── Meta CAPI: evento Lead — paciente calificado (eligió esp + modalidad) ──
        try:
            import meta_capi as _mc_lead
            _lead_esp = data.get("especialidad") or data.get("quick_esp") or ""
            asyncio.create_task(_mc_lead.send_event(
                "Lead",
                phone=phone,
                fbclid=data.get("fbclid"),
                fbclid_ts=data.get("fbclid_ts"),
                ctwa_clid=_ctwa_clid_for(phone),
                custom_data={
                    "content_name": _lead_esp,
                    "content_category": "appointment_intent",
                    "lead_source": "whatsapp_flow",
                },
            ))
        except Exception as _capi_lead_err:
            log.debug("CAPI Lead create_task falló: %s", _capi_lead_err)
        # ── fin CAPI Lead ──────────────────────────────────────────────────

        save_session(phone, "WAIT_RUT_AGENDAR", data)
        return (
            f"Perfecto, atención *{modalidad_str}* 😊\n\n"
            "Para reservar necesito tu *RUT*:\n"
            "(ej: *12.345.678-9*)\n\n"
            "_Si es para otra persona, escribe *otra persona*._"
            + _PRIVACY_NOTE
        )

    # ── WAIT_BOOKING_WHO ──────────────────────────────────────────────────────
    # Handler para el roster de dependientes. Muestra lista de familiares conocidos
    # y permite seleccionar para quién es la cita sin pedir RUT manualmente.
    if state == "WAIT_BOOKING_WHO":
        _deps_roster = data.get("_deps_roster", [])
        if tl == "dep_self":
            # Para el dueño del cel
            data["booking_for_other"] = False
            data.pop("_deps_roster", None)
            save_session(phone, "WAIT_RUT_AGENDAR", data)
            rut_conocido = data.get("rut_conocido")
            nombre_conocido = data.get("nombre_conocido")
            if rut_conocido and nombre_conocido:
                return _btn_msg(
                    f"¿Agendo con tus datos anteriores, *{_first_name(nombre_conocido)}*?",
                    [{"id": "si", "title": "Sí, continuar"},
                     {"id": "rut_nuevo", "title": "Ingresar otro RUT"}],
                )
            return (
                "Para confirmar necesito tu RUT:\n"
                "(ej: *12.345.678-9*)"
                + _PRIVACY_NOTE
            )
        if tl == "dep_nuevo":
            # Otra persona no registrada — flujo normal
            data["booking_for_other"] = True
            data.pop("rut_conocido", None)
            data.pop("nombre_conocido", None)
            data.pop("_deps_roster", None)
            save_session(phone, "WAIT_RUT_AGENDAR", data)
            return (
                "Sin problema. Necesito el *RUT* de la persona que se va a atender:\n"
                "(ej: *12.345.678-9*)"
                + _PRIVACY_NOTE
            )
        # Selección de dependiente registrado. Match case-insensitive: los IDs
        # de lista de WhatsApp vuelven en minúscula → un RUT terminado en K (≈1
        # de cada 11) no matchearía contra el roster que guarda la K en mayúscula.
        if tl.startswith("dep_"):
            _dep_sel_lower = tl[4:]
            _dep_rut_sel = next(
                (r for r in _deps_roster if str(r).lower() == _dep_sel_lower), None
            )
            if _dep_rut_sel:
                data["rut"] = _dep_rut_sel
                data["rut_conocido"] = _dep_rut_sel
                data["booking_for_other"] = True
                data["dep_preselected"] = True
                data.pop("_deps_roster", None)
                data.pop("nombre_conocido", None)  # buscar nombre real en Medilink
                save_session(phone, "WAIT_RUT_AGENDAR", data)
                log_event(phone, "dep_preselected", {"rut": _dep_rut_sel})
                # Mostrar el nombre del familiar para que el paciente confirme a
                # QUIÉN va dirigida la hora; la confirmación final con datos de
                # Medilink ocurre luego en CONFIRMING_CITA.
                _dep_nom = ""
                try:
                    _own_rut = (get_profile(phone) or {}).get("rut") or ""
                    if _own_rut:
                        for _d in list_family_links(_own_rut):
                            if str(_d.get("dependent_rut")).lower() == _dep_sel_lower:
                                _dep_nom = _first_name(_d.get("dependent_nombre") or "")
                                break
                except Exception:
                    pass
                _quien = f"para *{_dep_nom}*" if _dep_nom else "para este familiar"
                return _btn_msg(
                    f"Perfecto, la hora es {_quien}. ¿Confirmo?",
                    [{"id": "si", "title": "Sí, continuar"},
                     {"id": "rut_nuevo", "title": "Ingresar otro RUT"}],
                )
        # Input no reconocido: re-mostrar la lista
        _owner_profile_who = get_profile(phone)
        _owner_rut_who = (_owner_profile_who or {}).get("rut") or ""
        _deps_who: list[dict] = []
        if _owner_rut_who:
            try:
                _deps_who = list_family_links(_owner_rut_who)
            except Exception:
                pass
        if _deps_who:
            _deps_mostrar_who = _deps_who[:8]
            _rows_who: list[dict] = []
            if not data.get("booking_for_other"):
                _rows_who.append({"id": "dep_self", "title": "Para mí"})
            _rows_who += [
                {
                    "id": f"dep_{d['dependent_rut']}",
                    "title": _first_name(d["dependent_nombre"])[:24],
                    "description": (d.get("relation") or "familiar")[:72],
                }
                for d in _deps_mostrar_who
            ]
            _rows_who.append({"id": "dep_nuevo", "title": "Otra persona"})
            data["_deps_roster"] = [d["dependent_rut"] for d in _deps_mostrar_who]
            save_session(phone, "WAIT_BOOKING_WHO", data)
            return _list_msg(
                body_text="¿Para quién es la hora?",
                button_label="Seleccionar",
                sections=[{"title": "Selecciona una opción", "rows": _rows_who}],
            )
        # Fallback si perdemos la lista: pedir RUT
        save_session(phone, "WAIT_RUT_AGENDAR", data)
        return (
            "Necesito el *RUT* de la persona que se va a atender:\n"
            "(ej: *12.345.678-9*)"
            + _PRIVACY_NOTE
        )

    # ── WAIT_AGENDAR_OTRO ───────────────────────────────────────────────────────
    # Tras confirmar una cita propia ofrecemos agendar a otra persona (familiar).
    # Si acepta, mostramos los 2 cupos más cercanos (antes/después de la hora
    # recién agendada) con el mismo profesional para encadenar sin fricción.
    if state == "WAIT_AGENDAR_OTRO":
        _AFIRM_OTRO = AFIRMACIONES | {"otro_si"}
        _NEG_OTRO = NEGACIONES | {"otro_no"}
        if tl in _NEG_OTRO or tl_norm in _NEG_OTRO:
            reset_session(phone)
            return "Perfecto 😊\n\n_Escribe *menu* si necesitas algo más._"
        if tl in _AFIRM_OTRO or tl_norm in _AFIRM_OTRO:
            lb = data.get("last_booked") or {}
            _esp_lb = lb.get("especialidad", "")
            _idprof_lb = lb.get("id_profesional")
            _fecha_lb = lb.get("fecha", "")
            _hora_lb = (lb.get("hora_inicio") or "")[:5]
            _contig: list = []
            try:
                _, _todos_lb = await buscar_slots_dia(_esp_lb, _fecha_lb)

                def _to_min_otro(h):
                    try:
                        return int(h[:2]) * 60 + int(h[3:5])
                    except Exception:
                        return -1
                _ref_otro = _to_min_otro(_hora_lb)
                _mismos = [s for s in (_todos_lb or [])
                           if str(s.get("id_profesional", "")) == str(_idprof_lb)
                           and s.get("hora_inicio")]
                _despues = sorted(
                    [s for s in _mismos if _to_min_otro(s["hora_inicio"][:5]) > _ref_otro],
                    key=lambda s: _to_min_otro(s["hora_inicio"][:5]) - _ref_otro)
                _antes = sorted(
                    [s for s in _mismos if 0 <= _to_min_otro(s["hora_inicio"][:5]) < _ref_otro],
                    key=lambda s: _ref_otro - _to_min_otro(s["hora_inicio"][:5]))
                if _despues:
                    _contig.append(_despues[0])
                if _antes:
                    _contig.append(_antes[0])
            except Exception as _e_contig:
                log.warning("búsqueda slots contiguos falló: %s", _e_contig)
            # Preparar flujo de tercero con el mismo profesional/especialidad
            data["booking_for_other"] = True
            data["modalidad"] = lb.get("modalidad", "particular")
            data["especialidad"] = _esp_lb
            for _k in ("rut_conocido", "nombre_conocido", "paciente", "slot_elegido", "rut"):
                data.pop(_k, None)
            if _contig:
                data["_slots_otro"] = _contig[:2]
                _rows_c = [
                    {"id": f"slot_otro_{i}", "title": f"{s['hora_inicio'][:5]} hrs",
                     "description": (s.get("profesional", "") or "")[:72]}
                    for i, s in enumerate(_contig[:2])
                ]
                _rows_c.append({"id": "slot_otro_dia", "title": "Ver otro horario"})
                save_session(phone, "WAIT_SLOT_OTRO", data)
                return _list_msg(
                    body_text=(
                        f"Genial 🙌 Agendemos a tu familiar con "
                        f"{_contig[0].get('profesional','el mismo profesional')} "
                        f"el {lb.get('fecha_display','mismo día')}.\n\n"
                        "Estos son los cupos más cercanos:"),
                    button_label="Elegir hora",
                    sections=[{"title": "Horas contiguas", "rows": _rows_c}],
                )
            # Sin cupos contiguos: caer al flujo normal de tercero
            reset_session(phone)
            return await _iniciar_agendar(
                phone,
                {"booking_for_other": True, "modalidad": lb.get("modalidad", "particular")},
                _esp_lb or None,
            )
        # Input ambiguo → re-preguntar
        save_session(phone, "WAIT_AGENDAR_OTRO", data)
        return _btn_msg(
            "¿Deseas agendar una hora para otra persona (un familiar)?",
            [{"id": "otro_si", "title": "✅ Sí"},
             {"id": "otro_no", "title": "No, gracias"}]
        )

    # ── WAIT_SLOT_OTRO ────────────────────────────────────────────────────────
    # Selección de una de las 2 horas contiguas para el familiar.
    if state == "WAIT_SLOT_OTRO":
        _slots_otro = data.get("_slots_otro") or []
        if tl == "slot_otro_dia":
            _esp_so = data.get("especialidad", "")
            reset_session(phone)
            return await _iniciar_agendar(
                phone,
                {"booking_for_other": True, "modalidad": data.get("modalidad", "particular")},
                _esp_so or None,
            )
        _idx_so = None
        if tl in ("slot_otro_0", "slot_otro_1"):
            _idx_so = int(tl.rsplit("_", 1)[-1])
        if _idx_so is not None and _idx_so < len(_slots_otro):
            data["slot_elegido"] = _slots_otro[_idx_so]
            data["booking_for_other"] = True
            data.pop("_slots_otro", None)
            save_session(phone, "WAIT_RUT_AGENDAR", data)
            return (
                "Perfecto 😊 Ahora necesito el *RUT* de la persona que se va a "
                "atender:\n(ej: *12.345.678-9*)"
                + _PRIVACY_NOTE
            )
        # Input no reconocido: re-mostrar
        save_session(phone, "WAIT_SLOT_OTRO", data)
        _rows_c = [
            {"id": f"slot_otro_{i}", "title": f"{s['hora_inicio'][:5]} hrs",
             "description": (s.get("profesional", "") or "")[:72]}
            for i, s in enumerate(_slots_otro[:2])
        ]
        _rows_c.append({"id": "slot_otro_dia", "title": "Ver otro horario"})
        return _list_msg(
            body_text="Elige una hora para tu familiar:",
            button_label="Elegir hora",
            sections=[{"title": "Horas contiguas", "rows": _rows_c}],
        )

    # ── WAIT_PARENTESCO ─────────────────────────────────────────────────────────
    # Tras agendar a un familiar, preguntamos el parentesco (opcional) para
    # saludar por nombre la próxima vez y construir el árbol familiar del portal.
    if state == "WAIT_PARENTESCO":
        _PAR_MAP = {
            "par_hijo":   ("hijo/a",     "tutor_declaration"),
            "par_padre":  ("padre/madre", "declared"),
            "par_pareja": ("pareja",     "declared"),
            "par_hermano": ("hermano/a", "declared"),
            "par_otro":   ("familiar",   "declared"),
        }
        _sel_par = _PAR_MAP.get(tl)
        if _sel_par:
            _rel_par, _verif_par = _sel_par
            try:
                _po_par = data.get("par_owner_rut") or ""
                _dr_par = data.get("par_dep_rut") or ""
                _dn_par = data.get("par_dep_nombre") or _dr_par
                if _po_par and _dr_par and _po_par != _dr_par:
                    add_family_link(owner_rut=_po_par, dependent_rut=_dr_par,
                                    dependent_nombre=_dn_par, relation=_rel_par,
                                    verification_method=_verif_par)
                    log_event(phone, "parentesco_guardado", {"relation": _rel_par})
            except Exception as _e_par:
                log.warning("guardar parentesco falló (no bloquea): %s", _e_par)
            reset_session(phone)
            return ("¡Gracias! Lo dejé registrado 😊\n\n"
                    "_Escribe *menu* si necesitas algo más._")
        if tl in ("par_skip", "menu", "menú") or tl in NEGACIONES:
            reset_session(phone)
            return "Listo 😊\n\n_Escribe *menu* si necesitas algo más._"
        # Re-preguntar
        save_session(phone, "WAIT_PARENTESCO", data)
        _dn_re = _first_name(data.get("par_dep_nombre") or "") or "tu familiar"
        return _list_msg(
            body_text=f"¿Qué es *{_dn_re}* tuyo/a? (opcional)",
            button_label="Responder",
            sections=[{"title": "Parentesco", "rows": [
                {"id": "par_hijo", "title": "Hijo/a"},
                {"id": "par_padre", "title": "Padre/Madre"},
                {"id": "par_pareja", "title": "Pareja"},
                {"id": "par_hermano", "title": "Hermano/a"},
                {"id": "par_otro", "title": "Otro"},
                {"id": "par_skip", "title": "Prefiero no decir"},
            ]}],
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

        # BUG-5 FIX: Texto libre con keywords de menor/intento de agendar SIN patrón RUT.
        # Caso real 56958940517: "Hola necesito una hora con médico que vea niños porfavor"
        # llegaba a WAIT_RUT_AGENDAR y recibía "RUT inválido". Si el texto no tiene
        # formato de RUT y contiene keywords de intent claro → reset + re-dispatch.
        if state == "WAIT_RUT_AGENDAR":
            _RUT_PATTERN_RX = re.compile(r'\b\d{5,8}[-–][\dkK]\b')
            _MENOR_KW_RUT = re.compile(
                r'\b(niñ[oa]|nino|bebe|bebé|guagua|menor|lactante|infante|' +
                r'pediatr|medico para ninos|médico para niños|' +
                r'médico que vea|medico que vea|' +
                r'necesito una hora|quiero agendar|quiero una hora|necesito hora)\b',
                re.IGNORECASE,
            )
            if (
                not _RUT_PATTERN_RX.search(txt)
                and len([c for c in txt if c.isdigit()]) < 4
                and _MENOR_KW_RUT.search(txt)
                and len(txt.strip()) > 8
            ):
                log_event(phone, "rut_agendar_reintent_menor", {"texto": txt[:120]})
                reset_session(phone)
                return await handle_message(phone, txt, {"state": "IDLE", "data": {}})

    if state == "WAIT_RUT_AGENDAR":
        # BUG-H: si ya hubo 2+ rechazos de RUT y el paciente envía texto sin formato de
        # RUT (parece un nombre), derivar a recepción con el nombre como contexto.
        _RUT_LIKE = re.compile(r'\b\d{5,8}[-–][\dkK]\b|\b\d{7,9}\b')
        if (
            data.get("intentos_rut_invalido", 0) >= 2
            and not _RUT_LIKE.search(txt)
            and len(txt.split()) >= 2
            and len([c for c in txt if c.isdigit()]) < 4
        ):
            log_event(phone, "rut_fallback_nombre", {"texto": txt[:120]})
            return _derivar_humano(
                phone=phone,
                contexto=f"paciente no tiene RUT exacto; nombre indicado: {txt[:120]}"
            )

        # BUG-3 FIX: Respuesta a aviso pediátrico (botones ped_continuar / ped_no)
        if tl == "ped_continuar":
            # Paciente acepta continuar pese al aviso de edad — limpiar flag y pedir RUT
            data.pop("pediatria_aviso_visto", None)
            save_session(phone, "WAIT_RUT_AGENDAR", data)
            return (
                "Sin problema 😊 Ingresa el *RUT* del paciente:\n"
                "(ej: *12.345.678-9*)"
            )
        if tl == "ped_no":
            reset_session(phone)
            return (
                "Entendido 😊 Te recomendamos acudir al *CESFAM Carampangue* "
                "o a un pediatra en el Hospital de Arauco para atención especializada.\n\n"
                "Si en algún momento necesitas una consulta para adultos, estaremos aquí."
            )

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
            # BUG-06 / BUG-H: contador específico para RUT inválido (distinto del genérico intentos_fallidos)
            # Al 2do rechazo: ofrecer escape por nombre o recepción (evita loop infinito).
            # Al 3er rechazo: derivar a recepción.
            data["intentos_rut_invalido"] = data.get("intentos_rut_invalido", 0) + 1
            data["intentos_fallidos"] = data.get("intentos_fallidos", 0) + 1
            intentos_rut = data["intentos_rut_invalido"]
            if intentos_rut >= 3:
                return _derivar_humano(phone=phone, contexto="RUT inválido repetido WAIT_RUT_AGENDAR")
            save_session(phone, "WAIT_RUT_AGENDAR", data)
            hint = hint_rut_error(txt)
            if intentos_rut >= 2:
                # BUG-H: segundo rechazo → ofrecer escape explícito con nombre o recepción
                hint += (
                    "\n\nNo logro validar ese RUT. Si no lo tienes exacto, puedes:\n"
                    "1) Escribirme el *nombre completo* del paciente y te busco\n"
                    f"2) Llamar a recepción al *(44) 296 5226*"
                )
            return hint

        data.pop("intentos_rut_invalido", None)  # BUG-06: reset al RUT valido
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

        # BUG-3 FIX: Aviso suave pediátrico (no bloqueo) para especialidades
        # que el CMC atiende pero sin pediatría especializada. Solo se muestra
        # una vez (flag pediatria_aviso_visto en sesión).
        try:
            from config import EDAD_AVISO_PEDIATRIA as _EDAD_AVISO_PED
            _esp_ped = (data.get("especialidad") or "").lower().strip()
            _fn_ped = paciente.get("fecha_nacimiento") or ""
            _edad_ped: int | None = None
            if _fn_ped:
                try:
                    _dparsed_ped = datetime.strptime(_fn_ped, "%d/%m/%Y").date()
                    _today_ped = datetime.now(_CHILE_TZ).date()
                    _edad_ped = (_today_ped - _dparsed_ped).days // 365
                except Exception:
                    pass
            _umbral_ped = _EDAD_AVISO_PED.get(_esp_ped)
            if (
                _umbral_ped is not None
                and _edad_ped is not None
                and _edad_ped < _umbral_ped
                and not data.get("pediatria_aviso_visto")
            ):
                data["pediatria_aviso_visto"] = True
                save_session(phone, "WAIT_RUT_AGENDAR", data)
                log_event(phone, "aviso_pediatria", {"esp": _esp_ped, "edad": _edad_ped})
                return _btn_msg(
                    f"*Aviso importante:* el paciente tiene {_edad_ped} años. "
                    f"En el CMC no tenemos pediatría especializada — nuestros profesionales "
                    f"pueden atender niños sanos para consultas básicas, pero para temas "
                    f"pediátricos complejos te recomendamos ir a tu CESFAM o a un pediatra externo.\n\n"
                    f"¿Quieres continuar con la cita en el CMC?",
                    [{"id": "ped_continuar", "title": "Sí, continuar"},
                     {"id": "ped_no", "title": "No, mejor ir al CESFAM"}],
                )
        except Exception as _e_ped:
            log.warning("aviso pediatria error (ignorado): %s", _e_ped)

        # FIX-13: Validación pre-flight edad/género antes de confirmar cita.
        # Evita agendar menores en especialidades adultas (o vice-versa).
        try:
            from config import (EDAD_MIN_ESPECIALIDAD, EDAD_MAX_ESPECIALIDAD,
                                GENERO_REQUERIDO, ALTERNATIVA_ESPECIALIDAD)
            _esp_lower = (data.get("especialidad") or "").lower().strip()
            _sexo_pac = (paciente.get("sexo") or "").upper()[:1]
            _fn_pac = paciente.get("fecha_nacimiento") or ""
            _edad_pac: int | None = None
            if _fn_pac:
                try:
                    from datetime import datetime as _dtpf, date as _dpf
                    # Medilink devuelve DD/MM/YYYY
                    _dparsed = datetime.strptime(_fn_pac, "%d/%m/%Y").date()
                    _today = datetime.now(_CHILE_TZ).date()
                    _edad_pac = (_today - _dparsed).days // 365
                except Exception:
                    pass
            _pf_err: str | None = None
            if _edad_pac is not None:
                _min_e = EDAD_MIN_ESPECIALIDAD.get(_esp_lower)
                _max_e = EDAD_MAX_ESPECIALIDAD.get(_esp_lower)
                if _min_e and _edad_pac < _min_e:
                    alt = ALTERNATIVA_ESPECIALIDAD.get(_esp_lower, "")
                    _pf_err = (
                        f"Esta especialidad (*{_esp_lower.title()}*) es para mayores de {_min_e} años. "
                        f"El paciente tiene {_edad_pac} años."
                        + (f"\n\n¿Quieres agendar *{alt.title()}* en su lugar? Escribe *{alt}* o *menu*." if alt else "")
                    )
                elif _max_e and _edad_pac > _max_e:
                    alt = ALTERNATIVA_ESPECIALIDAD.get(_esp_lower, "")
                    _pf_err = (
                        f"Esta especialidad (*{_esp_lower.title()}*) es para menores de {_max_e + 1} años. "
                        f"El paciente tiene {_edad_pac} años."
                        + (f"\n\n¿Quieres agendar *{alt.title()}* en su lugar? Escribe *{alt}* o *menu*." if alt else "")
                    )
            _genero_req = GENERO_REQUERIDO.get(_esp_lower)
            if not _pf_err and _genero_req and _sexo_pac and _sexo_pac != _genero_req:
                _lbl = "mujeres" if _genero_req == "F" else "hombres"
                _pf_err = (
                    f"La especialidad *{_esp_lower.title()}* solo atiende {_lbl}. "
                    "Puedo ayudarte a buscar otra especialidad."
                )
            if _pf_err:
                log_event(phone, "preflight_edad_genero_fallo",
                          {"esp": _esp_lower, "edad": _edad_pac, "sexo": _sexo_pac})
                reset_session(phone)
                return _pf_err
        except Exception as _e_pf:
            log.warning("preflight edad/genero error (ignorado): %s", _e_pf)

        data.update({"paciente": paciente, "rut": rut})
        log_event(phone, "funnel_confirmacion", {
            "esp": (data.get("slot_elegido") or {}).get("especialidad", data.get("especialidad", "")),
            "paso": "llegando_confirming_cita",
        })
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
                {"id": "cambiar_datos", "title": "❌ Cambiar"},
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
        # Paciente quiere cambiar algo → mostrar sub-menú de opciones
        # (FIX 3: 21% abandona porque antes se saltaba directo a WAIT_MODALIDAD
        # sin preguntar QUÉ quería cambiar)
        if tl == "cambiar_datos":
            slot = data.get("slot_elegido", {})
            log_event(phone, "cambiar_datos_submenu", {
                "especialidad": slot.get("especialidad", ""),
                "fecha": slot.get("fecha_display", ""),
            })
            save_session(phone, "CONFIRMING_CITA", data)  # mantener estado y slot
            return _btn_msg(
                f"Sin problema 😊 Tu hora sigue apartada:\n\n"
                f"🏥 *{slot.get('especialidad', '')}* — {slot.get('profesional', '')}\n"
                f"📅 *{slot.get('fecha_display', '')}*\n"
                f"🕐 *{slot.get('hora_inicio', '')[:5]}*\n\n"
                "¿Qué quieres cambiar?",
                [
                    {"id": "cd_horario",  "title": "📅 Horario u otro día"},
                    {"id": "cd_persona",  "title": "👤 Es para otra persona"},
                    {"id": "cd_datos",    "title": "✏️ Mis datos (RUT/nombre)"},
                ]
            )
        # Sub-opciones del cambiar_datos
        if tl == "cd_horario":
            # Buscar nueva disponibilidad para la misma especialidad
            _slot_cd = data.get("slot_elegido", {})
            _esp_cd = _slot_cd.get("especialidad") or data.get("especialidad", "")
            log_event(phone, "cambiar_datos_horario", {"esp": _esp_cd})
            # Limpiar slot elegido pero preservar especialidad y datos del paciente
            data.pop("slot_elegido", None)
            data.pop("slots", None)
            data.pop("todos_slots", None)
            return await _iniciar_agendar(phone, data, _esp_cd or None)
        if tl == "cd_persona":
            # Redirigir a flujo de "otra persona" — preservar slot
            _slot_cd = data.get("slot_elegido", {})
            log_event(phone, "cambiar_datos_otra_persona", {})
            data["booking_for_other"] = True
            data.pop("paciente", None)
            data.pop("rut", None)
            perfil = get_profile(phone)
            if perfil and perfil.get("rut"):
                data["rut_conocido"] = perfil["rut"]
                data["nombre_conocido"] = perfil.get("nombre", "")
            # Si la modalidad ya fue elegida (Fonasa/Particular), la preservamos
            # y vamos directo al RUT del paciente nuevo.
            # Si NO existe modalidad, no podemos asumir "particular": preguntamos primero.
            if data.get("modalidad"):
                save_session(phone, "WAIT_RUT_AGENDAR", data)
                return (
                    f"Perfecto 😊 La hora sigue reservada:\n\n"
                    f"🏥 *{_slot_cd.get('especialidad', '')}* — {_slot_cd.get('profesional', '')}\n"
                    f"📅 *{_slot_cd.get('fecha_display', '')}*\n"
                    f"🕐 *{_slot_cd.get('hora_inicio', '')[:5]}*\n\n"
                    "Escríbeme el *RUT* de la persona que va a atenderse."
                )
            # Sin modalidad: preguntar antes de seguir (booking_for_other queda True en data)
            save_session(phone, "WAIT_MODALIDAD", data)
            return _btn_msg(
                f"La hora sigue reservada:\n\n"
                f"🏥 *{_slot_cd.get('especialidad', '')}* — {_slot_cd.get('profesional', '')}\n"
                f"📅 *{_slot_cd.get('fecha_display', '')}*\n"
                f"🕐 *{_slot_cd.get('hora_inicio', '')[:5]}*\n\n"
                "¿La atención de la otra persona será *Fonasa* o *Particular*?",
                [{"id": "1", "title": "Fonasa"}, {"id": "2", "title": "Particular"}]
            )
        if tl == "cd_datos":
            # Cambiar RUT/nombre → flujo completo desde WAIT_MODALIDAD
            data.pop("paciente", None)
            data.pop("rut", None)
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
        # Bug 1 fix: detectar afirmación como prefijo ("si reservar", "si confirma",
        # "si por favor", "siii" ya normalizado a "si" por triage_ges/flows).
        # También colapsar vocales repetidas en el propio tl_norm del flujo.
        import re as _re_b1
        _tl_norm_c = _re_b1.sub(r"([aeiou])\1{2,}", r"\1", tl_norm)
        _es_afirmacion_confirming = (
            tl in AFIRMACIONES
            or tl_norm in AFIRMACIONES
            or _tl_norm_c in AFIRMACIONES
            or any(_tl_norm_c == a or _tl_norm_c.startswith(a + " ")
                   for a in AFIRMACIONES)
        )
        if _es_afirmacion_confirming:
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
                        paciente["id"], rut=paciente.get("rut"),
                        raise_on_error=True,
                    )
                except Exception as e:
                    # Fail-open DELIBERADO: no bloqueamos el agendamiento por un
                    # hiccup de Medilink. Pero queda observable: antes
                    # listar_citas_paciente devolvía [] sin levantar nada y este
                    # except era código muerto (el bypass del bloqueo
                    # anti-duplicados ocurría en silencio total).
                    log.warning("dup-check listar_citas falló phone=%s: %s", phone, e)
                    log_event(phone, "dup_check_medilink_fallo", {"error": str(e)[:200]})
                    existing_citas = []

                # 1) Bloqueo quirúrgico por RUT: el MISMO paciente (RUT) ya tiene
                #    una hora con el MISMO profesional el MISMO día.
                #    NO limitamos por número de teléfono: un apoderado puede traer
                #    varios hijos (RUTs distintos) en un mismo celular (caso real:
                #    una mamá con sus 4 hijos), y una persona puede agendar con
                #    distintos profesionales el mismo día. Solo cuando el mismo RUT
                #    repite profesional+día no dejamos agendar y ofrecemos cambiar
                #    esa hora. `existing_citas` es del paciente que se está
                #    agendando (listar_citas_paciente por su id/rut) → es per-RUT.
                same_prof_dia = next(
                    (c for c in (existing_citas or [])
                     if str(c.get("id_profesional", "")) == str(slot.get("id_profesional", ""))
                     and c.get("fecha") == slot.get("fecha")),
                    None,
                )
                if same_prof_dia:
                    log_event(phone, "cita_bloqueada_mismo_rut_prof_dia", {
                        "id_profesional": slot.get("id_profesional"),
                        "profesional": slot.get("profesional"),
                        "fecha": slot.get("fecha"),
                        "hora_existente": (same_prof_dia.get("hora_inicio", "") or "")[:5],
                    })
                    _nom_blk = _first_name(paciente.get("nombre"))
                    # OJO: `es_tercero` se asigna recién más abajo en esta misma
                    # función (~L8691) → referenciarlo aquí daría UnboundLocalError.
                    # Derivamos el flag directo de `data`, igual que esa línea.
                    _es_tercero_blk = bool(data.get("booking_for_other"))
                    _hora_blk = (same_prof_dia.get("hora_inicio", "") or "")[:5]
                    _prof_blk = same_prof_dia.get("profesional", "este profesional")
                    _fecha_blk = slot.get("fecha_display", "ese día")

                    # REGLA (Rodrigo): pueden ser VARIAS personas en el mismo celular con
                    # el mismo profesional; lo que NO puede es la MISMA persona dos veces.
                    # El quick-book usa el RUT del PERFIL del teléfono sin preguntar para
                    # quién es → si una mamá agenda para su hijo, bloqueaba mal. Cuando NO
                    # es un agendamiento explícito para tercero, en vez de bloquear de una,
                    # preguntamos para quién es (preservando el cupo). Si es otra persona,
                    # se agenda con SU RUT y el bloqueo no aplica.
                    if not _es_tercero_blk:
                        data.pop("dup_pending", None)
                        save_session(phone, "WAIT_BOOKING_FOR", data)
                        return _btn_msg(
                            f"📋 Con ese RUT ya hay una hora con *{_prof_blk}* el "
                            f"{_fecha_blk} a las *{_hora_blk}*.\n\n"
                            "¿Esta nueva hora es para *ti* o para *otra persona* "
                            "(un familiar en este mismo teléfono)?",
                            [{"id": "booking_other", "title": "👤 Otra persona"},
                             {"id": "accion_recepcion", "title": "📞 Es mía / recepción"}]
                        )
                    reset_session(phone)
                    return _btn_msg(
                        f"📋 {_nom_blk} ya tiene una hora con *{_prof_blk}* el "
                        f"{_fecha_blk} a las *{_hora_blk}*.\n\n"
                        "Esa persona no puede tener dos horas con el mismo profesional "
                        "el mismo día.\n\n¿Quieres *cambiar* esa hora?",
                        [{"id": "reagendar", "title": "🔄 Cambiar la hora"},
                         {"id": "accion_recepcion", "title": "📞 Recepción"}]
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
                                f"*{dup.get('hora_inicio','')[:5]}* con "
                                f"{dup.get('profesional','')}.\n\n"
                                f"¿Igual quieres agendar esta segunda hora a las "
                                f"*{slot['hora_inicio'][:5]}*?",
                                [{"id": "si", "title": "✅ Sí, agendar igual"},
                                 {"id": "no", "title": "❌ Cancelar"}]
                            )
            # ── Lock optimista anti-race (TTL 30s) ──
            # Evita que dos pacientes confirmen el mismo slot en paralelo.
            # Si otro lo tiene → fallthrough a la lógica de "slot ocupado".
            _lock_ok = adquirir_slot_lock(
                slot["id_profesional"], slot["fecha"],
                slot["hora_inicio"], phone, ttl_segundos=30,
            )
            # ── Doble-check: verificar que el slot sigue libre ──
            # SOBRECUPO: es doble-cupo INTENCIONAL (David lo autorizó) → saltar el
            # chequeo de disponibilidad (siempre dará "ocupado"); basta el lock
            # anti-race + el tope diario que valida el módulo sobrecupo.
            if slot.get("sobrecupo"):
                slot_libre = _lock_ok
            else:
                # Deadline duro a la operación (incidente Matías 2026-06-05): el
                # timeout=15 de httpx acota cada request, pero retries + backoff
                # 429 + espera del semáforo _MEDILINK_SEM no tienen tope. Una
                # reserva colgada >90s no drena en el stop de systemd → SIGKILL
                # → reserva perdida en silencio. 25s aquí + 45s en crear_cita
                # garantizan terminar (o fallar con sesión preservada) antes.
                try:
                    slot_libre = (await asyncio.wait_for(verificar_slot_disponible(
                        slot["id_profesional"], slot["fecha"],
                        slot["hora_inicio"], slot["hora_fin"],
                    ), timeout=25)) if _lock_ok else False
                except asyncio.TimeoutError:
                    log_event(phone, "reserva_deadline_timeout", {
                        "fase": "verificar_slot",
                        "fecha": slot.get("fecha"), "hora": slot.get("hora_inicio"),
                    })
                    save_session(phone, "CONFIRMING_CITA", data)
                    return (
                        "Estamos con alta demanda en este momento y no pude confirmar "
                        "la reserva.\n\n"
                        "Tu selección sigue guardada. Escribe *si* en unos segundos "
                        "para reintentar, o llama a recepción:\n"
                        f"📞 *{CMC_TELEFONO}*"
                    )
            if not _lock_ok:
                log.warning("Slot lock ocupado por otro paciente: %s %s prof %s",
                            slot["fecha"], slot["hora_inicio"], slot["id_profesional"])
                log_event(phone, "slot_lock_perdido", {
                    "fecha": slot["fecha"], "hora": slot["hora_inicio"],
                })
            if not slot_libre:
                log.warning("Slot %s %s ya no está disponible para prof %s",
                            slot["fecha"], slot["hora_inicio"], slot["id_profesional"])
                log_event(phone, "slot_ya_ocupado", {
                    "fecha": slot["fecha"], "hora": slot["hora_inicio"],
                    "profesional": slot.get("profesional", ""),
                })
                # Re-buscar y ofrecer nueva hora (acotado: el fallback día-por-día
                # de buscar_primer_dia puede tardar minutos bajo tormenta 429)
                esp = data.get("especialidad", slot.get("especialidad", ""))
                try:
                    smart, todos = await asyncio.wait_for(
                        buscar_primer_dia(esp), timeout=30)
                except asyncio.TimeoutError:
                    log_event(phone, "reserva_deadline_timeout", {
                        "fase": "rebuscar_slot", "especialidad": esp,
                    })
                    smart, todos = [], []  # → cae a la oferta de waitlist
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
                    # FIX 2: ofrecer waitlist cuando no hay alternativa
                    _esp_tomada = data.get("especialidad", slot.get("especialidad", ""))
                    _id_prof_tomado = slot.get("id_profesional")
                    data["waitlist_especialidad"] = (_esp_tomada or "").lower()
                    data["waitlist_id_prof_pref"] = _id_prof_tomado
                    save_session(phone, "WAIT_WAITLIST_CONFIRM", data)
                    return _btn_msg(
                        "😔 Esa hora fue tomada y no encontré otra disponible.\n\n"
                        "¿Quieres que te avise apenas se libere un cupo?",
                        [
                            {"id": "waitlist_si", "title": "📝 Sí, inscribirme"},
                            {"id": "waitlist_no", "title": "No, gracias"},
                        ]
                    )
            # Psiquiatría (Dra. Cecilia Unibazo, prof 78) es SOLO teleconsulta → forzar
            # modalidad TELEMEDICINA (marca [ONLINE] en Medilink), sin preguntar presencial.
            if str(slot.get("id_profesional")) == "78" or "psiquiatr" in (slot.get("especialidad", "") or "").lower():
                data["telemedicina_modalidad"] = "TELEMEDICINA"

            # Neurología (Dra. Franca González, prof 79) es SOLO telemedicina → forzar
            # modalidad TELEMEDICINA igual que psiquiatría, sin abono-gate (no lo pidió el dueño).
            if str(slot.get("id_profesional")) == "79" or "neurolog" in (slot.get("especialidad", "") or "").lower():
                data["telemedicina_modalidad"] = "TELEMEDICINA"

            # ── Abono-Gate Psiquiatría (feature 2026-06-11) ───────────────────
            # Cuando ABONO_GATE_PSIQ_ACTIVE está ON, NO creamos la cita todavía:
            # pedimos el comprobante de transferencia ($20.000) primero. La hora
            # queda "apartada" 90 min en la sesión; el handler WAIT_ABONO_COMPROBANTE
            # procesa la imagen y crea la cita al validar el monto.
            # Con flag OFF el flujo sigue igual que antes (crea cita directamente).
            _es_psiquiatria_gate = (
                "psiquiatr" in (slot.get("especialidad", "") or "").lower()
                and not reagendar  # reagendas ya tienen cita → no pedir abono de nuevo
            )
            if _es_psiquiatria_gate and _abono_gate_psiq_activo():
                from config import CMC_TRANSFERENCIA as _CTF_AG, ABONO_PSIQUIATRIA_CLP as _ABO_AG
                # Guardar TODO lo necesario para crear la cita después
                data["abono_gate_slot"]     = slot
                data["abono_gate_paciente"] = paciente
                data["abono_gate_ts"]       = datetime.now(_CHILE_TZ).isoformat()
                liberar_slot_lock(slot["id_profesional"], slot["fecha"], slot["hora_inicio"])
                save_session(phone, "WAIT_ABONO_COMPROBANTE", data)
                log_event(phone, "abono_gate_activado", {
                    "especialidad": slot.get("especialidad"),
                    "fecha": slot.get("fecha"),
                    "hora": slot.get("hora_inicio"),
                    "monto_requerido": _ABO_AG,
                })
                _monto_fmt = f"${_ABO_AG:,}".replace(",", ".")
                return (
                    f"Para confirmar tu hora de *Psiquiatría* pedimos un abono de "
                    f"*{_monto_fmt} CLP* — corresponde al valor total de la consulta, "
                    "así que el día de la atención no pagas nada adicional.\n\n"
                    "*Datos para transferir:*\n"
                    f"{_CTF_AG['banco']}\n"
                    f"{_CTF_AG['tipo']} {_CTF_AG['numero']}\n"
                    f"{_CTF_AG['titular']}\n"
                    f"RUT: {_CTF_AG['rut']}\n"
                    f"Correo: {_CTF_AG['correo']}\n\n"
                    "Tu hora queda *apartada por 90 minutos*.\n"
                    "Envía el comprobante por este chat 📎 y confirmo tu reserva de inmediato.\n\n"
                    "_Si prefieres abonar en recepción, escribe *recepcion* y te orientamos._"
                )
            # ── fin Abono-Gate ────────────────────────────────────────────────

            # Marca durable ANTES del POST: si el proceso muere con la reserva en
            # vuelo (SIGKILL en restart), este evento queda sin su par
            # "reserva_resultado" → la reserva perdida es auditable en vez de
            # evaporarse en silencio (incidente Matías 2026-06-05).
            log_event(phone, "reserva_en_vuelo", {
                "fecha": slot.get("fecha"), "hora": slot.get("hora_inicio"),
                "id_profesional": slot.get("id_profesional"),
            })
            try:
                if slot.get("sobrecupo"):
                    # Reserva de SOBRECUPO: crea la cita marcada [SOBRECUPO] y registra
                    # el cupo para el tope diario (gateado por SOBRECUPO_ENABLED).
                    import sobrecupo as _sc
                    resultado = await asyncio.wait_for(_sc.crear_sobrecupo(
                        slot, paciente["id"], phone=phone,
                        rut=data.get("rut", ""),
                        modalidad=data.get("telemedicina_modalidad", "PRESENCIAL"),
                    ), timeout=45)
                else:
                    # 45s > peor caso interno de medilink._post (15+3+15 ≈ 33s):
                    # el deadline casi siempre corta una ESPERA (semáforo/cola),
                    # no un POST en vuelo — minimiza el riesgo de cita creada
                    # pero reportada como fallida.
                    # Bioimpedanciometría: la cita cae en la agenda de Gisela (52),
                    # cuya especialidad en Medilink es "Nutrición". Sin esta marca
                    # recepción la vería como consulta y cobraría $20.000 en vez de
                    # $15.000. observaciones_extra la hace visible en la agenda.
                    _obs_prestacion = (
                        "[BIOIMPEDANCIOMETRÍA $15.000]"
                        if data.get("especialidad") in _BIA_KEYS else ""
                    )
                    resultado = await asyncio.wait_for(crear_cita(
                        id_paciente=paciente["id"],
                        id_profesional=slot["id_profesional"],
                        fecha=slot["fecha"],
                        hora_inicio=slot["hora_inicio"],
                        hora_fin=slot["hora_fin"],
                        id_recurso=slot.get("id_recurso", 1),
                        modalidad=data.get("telemedicina_modalidad", "PRESENCIAL"),
                        observaciones_extra=_obs_prestacion,
                    ), timeout=45)
            except Exception as _crear_err:
                # P1-B: Medilink exige campo videoconsulta para ciertos slots
                # (teleconsulta Dr. Olavarría y posiblemente otros). El slot sigue
                # disponible — reintentamos automáticamente con modalidad TELEMEDICINA
                # (ya usada para psiquiatría/Unibazo). Si el reintentar también falla,
                # avisamos sin destruir la sesión (paciente puede elegir otro horario).
                from medilink import MedilinkVideoconsultaRequired as _MlinkVidReq
                if isinstance(_crear_err, _MlinkVidReq):
                    log_event(phone, "crear_cita_videoconsulta_required", {
                        "fecha": slot.get("fecha"),
                        "hora": slot.get("hora_inicio"),
                        "profesional": slot.get("profesional", ""),
                    })
                    try:
                        resultado_video = await asyncio.wait_for(crear_cita(
                            id_paciente=paciente["id"],
                            id_profesional=slot["id_profesional"],
                            fecha=slot["fecha"],
                            hora_inicio=slot["hora_inicio"],
                            hora_fin=slot["hora_fin"],
                            id_recurso=slot.get("id_recurso", 1),
                            modalidad="TELEMEDICINA",
                        ), timeout=45)
                    except Exception:
                        resultado_video = None
                    if resultado_video:
                        # Éxito con TELEMEDICINA — continuar el flujo normal
                        # marcando la modalidad para que la confirmación lo refleje
                        data["telemedicina_modalidad"] = "TELEMEDICINA"
                        resultado = resultado_video
                        log_event(phone, "crear_cita_videoconsulta_retry_ok", {
                            "fecha": slot.get("fecha"),
                            "hora": slot.get("hora_inicio"),
                            "id_cita": resultado_video.get("id"),
                        })
                    else:
                        # El retry también falló — no destruir la sesión
                        log_event(phone, "crear_cita_videoconsulta_retry_fail", {
                            "fecha": slot.get("fecha"),
                            "hora": slot.get("hora_inicio"),
                        })
                        log_event(phone, "reserva_resultado", {
                            "ok": False, "causa": "videoconsulta_required",
                            "fecha": slot.get("fecha"), "hora": slot.get("hora_inicio"),
                        })
                        # Ofrecer elegir otro horario sin perder la sesión
                        data.pop("slot_elegido", None)
                        data.pop("slots", None)
                        data.pop("todos_slots", None)
                        _esp_vid = data.get("especialidad", slot.get("especialidad", ""))
                        return await _iniciar_agendar(
                            phone, data, _esp_vid or None,
                            saludo_prefix=(
                                "Ese horario es de videoconsulta y no pude confirmarlo.\n\n"
                                "Te muestro otros horarios disponibles:\n\n"
                            ),
                        )
                # C3 fix: httpx.RequestError se lanza cuando Medilink persiste en
                # 429 tras todos los reintentos de medilink._post. Antes este error
                # subía al except genérico de main.py que llamaba reset_session(),
                # destruyendo todo el progreso del agendamiento.
                # Un 429 es transitorio — preservar sesión en CONFIRMING_CITA para
                # que el paciente pueda reintentar sin perder slot ni datos.
                import httpx as _httpx
                # asyncio.TimeoutError = deadline de 45s vencido (cola/semáforo
                # Medilink saturado) — mismo tratamiento que el 429: transitorio,
                # preservar sesión para que el paciente reintente con *si*.
                if isinstance(_crear_err, (_httpx.RequestError, asyncio.TimeoutError)):
                    log.warning(
                        "CONFIRMING_CITA: crear_cita falló por %s "
                        "phone=%s slot=%s/%s — preservando sesión",
                        type(_crear_err).__name__,
                        phone, slot.get("fecha"), slot.get("hora_inicio"),
                    )
                    log_event(phone, "crear_cita_429_preservado", {
                        "fecha": slot.get("fecha"),
                        "hora": slot.get("hora_inicio"),
                        "profesional": slot.get("profesional", ""),
                        "causa": type(_crear_err).__name__,
                    })
                    log_event(phone, "reserva_resultado", {
                        "ok": False, "causa": type(_crear_err).__name__,
                        "fecha": slot.get("fecha"), "hora": slot.get("hora_inicio"),
                    })
                    save_session(phone, "CONFIRMING_CITA", data)
                    return (
                        "Estamos con alta demanda en este momento y no pude confirmar "
                        "la reserva.\n\n"
                        "Tu selección sigue guardada. Escribe *si* en unos segundos "
                        "para reintentar, o llama a recepción:\n"
                        f"📞 *{CMC_TELEFONO}*"
                    )
                # Otros errores (4xx, errores de datos, etc.): dejar subir para que
                # el except genérico de main.py los maneje normalmente. Cerrar el
                # par de auditoría para no contar esto como "muerta en vuelo".
                log_event(phone, "reserva_resultado", {
                    "ok": False, "causa": type(_crear_err).__name__,
                    "fecha": slot.get("fecha"), "hora": slot.get("hora_inicio"),
                })
                raise
            # Liberar lock tentativo — éxito o fallo, ya no lo necesitamos.
            # Si la cita se creó, Medilink ya tiene el slot ocupado real.
            # Si falló, otro paciente puede intentar este slot.
            liberar_slot_lock(slot["id_profesional"], slot["fecha"], slot["hora_inicio"])
            # Cierra el par con "reserva_en_vuelo" — en_vuelo sin resultado = la
            # reserva murió con el proceso (restart/crash) y hay que auditarla.
            log_event(phone, "reserva_resultado", {
                "ok": bool(resultado),
                "fecha": slot.get("fecha"), "hora": slot.get("hora_inicio"),
                "id_cita": (resultado or {}).get("id"),
            })
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
                # La bioimpedanciometría se agenda en la agenda de la nutricionista,
                # así que slot["especialidad"] dice "Nutrición". Corregirlo acá hace
                # que el tag, la cita guardada, el recordatorio y el value del evento
                # Purchase (CAPI) usen la prestación real y su arancel ($15.000).
                if data.get("especialidad") in _BIA_KEYS:
                    esp = "Bioimpedanciometría"
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
                    id_paciente_medilink=paciente.get("id"),
                )
                # ── Telemedicina: guardar link videollamada ─────────────────
                _link_video = None
                if data.get("telemedicina_modalidad") == "TELEMEDICINA" and id_cita:
                    try:
                        from session import (generar_link_videollamada,
                                             save_telemedicina_cita)
                        _link_video = generar_link_videollamada(id_cita)
                        _fecha_hora_tele = f"{slot['fecha']}T{slot['hora_inicio']}"
                        save_telemedicina_cita(
                            medilink_cita_id=id_cita,
                            phone=phone,
                            profesional_id=slot["id_profesional"],
                            fecha_hora=_fecha_hora_tele,
                            link_videollamada=_link_video,
                        )
                        log_event(phone, "telemedicina_cita_guardada", {
                            "cita_id": id_cita, "link": _link_video})
                    except Exception as _te:
                        log.error("Error guardando telemedicina_cita: %s", _te)
                data["_link_video"] = _link_video
                # ── fin telemedicina ────────────────────────────────────────
                log_event(phone, "cita_reagendada" if reagendar else "cita_creada", {
                    "especialidad": esp,
                    "profesional": slot["profesional"],
                    "fecha": slot["fecha"],
                    "modalidad": data.get("modalidad", "particular"),
                    "id_cita_old": cita_old.get("id") if reagendar else None,
                })
                # ── Guardar vínculo familiar si la cita fue para tercero ─────
                # Solo en citas nuevas (no reagendar). Secundario: un fallo aquí
                # nunca debe interrumpir la confirmación de la cita.
                if not reagendar and data.get("booking_for_other"):
                    try:
                        _dep_owner_profile = get_profile(phone)
                        _dep_owner_rut = (_dep_owner_profile or {}).get("rut") or ""
                        _dep_rut_pac = data.get("rut") or ""
                        if _dep_owner_rut and _dep_rut_pac and _dep_owner_rut != _dep_rut_pac:
                            # Determinar relación desde texto original si está disponible
                            _dep_txt_raw = data.get("_txt_raw", "").lower()
                            _dep_relation = "familiar"
                            for _kw, _rel in (
                                ("hijo", "hijo"), ("hija", "hija"),
                                ("madre", "madre"), ("mama", "madre"), ("mamá", "madre"),
                                ("padre", "padre"), ("papa", "padre"), ("papá", "padre"),
                                ("hermano", "hermano"), ("hermana", "hermana"),
                                ("abuelo", "abuelo"), ("abuela", "abuela"),
                                ("esposo", "esposo"), ("esposa", "esposa"),
                                ("cónyuge", "cónyuge"), ("conyugue", "cónyuge"),
                                ("nieto", "nieto"), ("nieta", "nieta"),
                                ("suegro", "suegro"), ("suegra", "suegra"),
                            ):
                                if _kw in _dep_txt_raw:
                                    _dep_relation = _rel
                                    break
                            # Determinar verification_method por edad del paciente
                            _dep_verif = "declared"
                            _dep_fn = (paciente.get("fecha_nacimiento") or "")
                            if _dep_fn:
                                try:
                                    from datetime import datetime as _dt_dep
                                    _dep_parsed = _dt_dep.strptime(_dep_fn, "%d/%m/%Y").date()
                                    _dep_age = (datetime.now(_CHILE_TZ).date() - _dep_parsed).days // 365
                                    if _dep_age < 18:
                                        _dep_verif = "tutor_declaration"
                                except Exception:
                                    pass
                            _dep_nombre_pac = paciente.get("nombre") or _dep_rut_pac
                            add_family_link(
                                owner_rut=_dep_owner_rut,
                                dependent_rut=_dep_rut_pac,
                                dependent_nombre=_dep_nombre_pac,
                                relation=_dep_relation,
                                verification_method=_dep_verif,
                            )
                            log_event(phone, "dependiente_guardado", {
                                "owner_rut": _dep_owner_rut,
                                "dep_rut": _dep_rut_pac,
                                "relation": _dep_relation,
                                "verification_method": _dep_verif,
                            })
                    except Exception as _dep_save_err:
                        log.warning("guardar dependiente falló (no bloquea cita): %s", _dep_save_err)
                # ── fin guardar vínculo familiar ───────────────────────────
                # ── Notificación al profesional (push WA, ventana 24h, $0) ───
                # Best practice: avisar al profesional cuando bot agenda/reagenda
                # un paciente en su agenda — para que no le caiga uno sorpresa.
                try:
                    import prof_notifications as _pn
                    _id_prof = slot.get("id_profesional")
                    if _id_prof:
                        from resilience import spawn_task as _spawn
                        if reagendar:
                            _spawn(_pn.notify_reagenda(
                                id_prof=_id_prof,
                                profesional_nombre=slot["profesional"],
                                paciente_nombre=paciente.get("nombre", ""),
                                fecha_old=cita_old.get("fecha", ""),
                                hora_old=cita_old.get("hora_inicio", ""),
                                fecha_new=slot["fecha"],
                                hora_new=slot["hora_inicio"],
                                id_cita_old=str(cita_old.get("id", "")),
                                id_cita_new=id_cita,
                            ), name=f"prof_notif_reagenda_{_id_prof}")
                        else:
                            _spawn(_pn.notify_nueva_cita(
                                id_prof=_id_prof,
                                profesional_nombre=slot["profesional"],
                                paciente_nombre=paciente.get("nombre", ""),
                                fecha=slot["fecha"],
                                hora=slot["hora_inicio"],
                                modalidad=data.get("modalidad", "particular"),
                                id_cita=id_cita,
                            ), name=f"prof_notif_nueva_cita_{_id_prof}")
                except Exception as _pn_err:
                    log.warning("prof_notification falló (no bloquea cita): %s", _pn_err)
                # ── Telemetria PNI: pni_cita_generada ─────────────────────
                # Si este phone tenia un pni_enviado en las ultimas 72h,
                # loggear atribucion. Idempotente por cita_id.
                if not reagendar and id_cita:
                    try:
                        _pni_ev = get_recent_pni_event(phone, horas=72)
                        if _pni_ev:
                            from datetime import datetime as _dt, timezone as _tz
                            _pni_ts = _pni_ev["ts"]
                            _pni_dt = _dt.fromisoformat(_pni_ts).replace(tzinfo=_tz.utc)
                            _ahora = _dt.now(_tz.utc)
                            _horas_diff = (_ahora - _pni_dt).total_seconds() / 3600
                            log_pni_cita_generada(
                                phone=phone,
                                pni_evento_id=_pni_ev["id"],
                                horas_desde_envio=_horas_diff,
                                especialidad=esp,
                                cita_id=id_cita,
                            )
                    except Exception as _pni_attr_err:
                        log.debug("pni_cita_generada tracking error: %s", _pni_attr_err)
                # ── fin telemetria PNI ─────────────────────────────────────
                # ── Atribución winback: si este phone tiene envío reciente ─
                if id_cita:
                    try:
                        from winback import atribuir_cita_a_winback as _wb_attr
                        _wb_attr(phone, id_cita)
                    except Exception as _wb_attr_err:
                        log.debug("atribuir_cita_a_winback error: %s", _wb_attr_err)
                # ── fin atribución winback ─────────────────────────────────
                # ── Métricas horas vacías: marcar que el paciente agendó ──
                if not reagendar:
                    try:
                        mark_horas_vacias_agendo(phone, esp)
                    except Exception:
                        pass
                # ── Meta CAPI: evento Schedule ─────────────────────────────
                # create_task: no bloquea el flujo si CAPI falla o tarda.
                try:
                    import meta_capi as _mc
                    _capi_rut = data.get("rut") or ""
                    _capi_nom = (paciente.get("nombre") or "").split()
                    _capi_fn  = _capi_nom[0] if _capi_nom else None
                    _capi_ln  = _capi_nom[-1] if len(_capi_nom) > 1 else None
                    _capi_email = data.get("reg_email") or data.get("email") or None
                    asyncio.create_task(_mc.send_event(
                        "Schedule",
                        phone=phone,
                        rut=_capi_rut or None,
                        first_name=_capi_fn,
                        last_name=_capi_ln,
                        email=_capi_email,
                        fbclid=data.get("fbclid"),
                        fbclid_ts=data.get("fbclid_ts"),
                        ctwa_clid=_ctwa_clid_for(phone),
                        custom_data={
                            "content_name": esp,
                            "content_category": "appointment",
                        },
                    ))
                except Exception as _capi_err:
                    log.debug("CAPI Schedule create_task falló: %s", _capi_err)
                # ── fin CAPI ───────────────────────────────────────────────

                # ── Referral bono: notificar referente si es primera cita ───
                # Solo en citas nuevas (no reagendar). Verificar si el paciente
                # vino con código de referido y si es su primera cita agendada.
                if not reagendar:
                    try:
                        _bonos_pendientes = marcar_bono_primera_cita(phone)
                        for _bono in _bonos_pendientes:
                            _ref_phone = _bono["referrer_phone"]
                            _tipo = _bono["tipo_bono"]
                            _bono_desc = (
                                "20% de descuento en tu próxima consulta médica"
                                if _tipo == "medica_20"
                                else "15% de descuento en tu próxima atención dental"
                            )
                            _cant_mes = conteo_referidos_mes(_ref_phone)
                            if _cant_mes <= 3:
                                _notif = (
                                    f"¡Tu referido acaba de agendar su primera cita en el CMC!\n\n"
                                    f"Por referir a alguien, tienes un bono disponible:\n"
                                    f"*{_bono_desc}*\n\n"
                                    "Preséntate en recepción y menciona tu código de referido "
                                    "para canjear el descuento.\n\n"
                                    f"Este mes llevas *{_cant_mes} referido(s)* validado(s) "
                                    "(máx. 3/mes)."
                                )
                                asyncio.create_task(send_whatsapp(_ref_phone, _notif))
                                from session import log_message as _lm_f6
                                _lm_f6(_ref_phone, "out", _notif, "IDLE")
                                marcar_bono_notificado(_bono["id"])
                                log_event(_ref_phone, "bono_referral_notificado", {
                                    "bono_id": _bono["id"],
                                    "referred_phone": phone,
                                    "tipo_bono": _tipo,
                                })
                    except Exception as _bono_err:
                        log.warning("Error procesando bono referral phone=%s: %s", phone, _bono_err)
                # ── fin referral bono ──────────────────────────────────────

                cross_ref = _cross_reference_msg(esp)
                # Bioimpedanciometría: las indicaciones van AL AGENDAR, no el día
                # antes — el paciente tiene que saber hoy que no debe comer 3 h antes
                # ni entrenar ese día. Reemplazan al cross-sell (no ofrecerle el
                # examen a quien acaba de agendarlo).
                if esp == "Bioimpedanciometría":
                    cross_ref = f"\n\n{_BIA_PREPARACION}"
                # Recordatorio PNI para pacientes pediátricos
                fecha_nac = (data.get("reg_fecha_nacimiento")
                             or paciente.get("fecha_nacimiento", ""))
                pni_msg = ""
                _pni_telemetria = None  # metadata para log_event("pni_enviado")
                if fecha_nac:
                    _pni = get_vaccine_reminder(fecha_nac, paciente["nombre"])
                    if _pni:
                        pni_msg = f"\n\n{_pni}"
                    _hitos = get_milestones_reminder(fecha_nac, paciente["nombre"])
                    if _hitos:
                        pni_msg += f"\n\n{_hitos}"
                    # Calcular metadata de telemetria solo si hay algo para enviar
                    if pni_msg:
                        _pni_meta_raw = get_pni_meta(fecha_nac)
                        _hitos_meta_raw = get_hitos_meta(fecha_nac)
                        _tiene_pni = _pni_meta_raw is not None and _pni_meta_raw.get("tiene_pni")
                        _tiene_hitos = _hitos_meta_raw is not None
                        if _tiene_pni and _tiene_hitos:
                            _tipo_pni = "ambos"
                        elif _tiene_pni:
                            _tipo_pni = "pni"
                        else:
                            _tipo_pni = "hitos"
                        _meta_ref = _pni_meta_raw or _hitos_meta_raw
                        _pni_telemetria = {
                            "tipo": _tipo_pni,
                            "edad_meses": (_meta_ref or {}).get("edad_meses"),
                            "edad_etiqueta": (_meta_ref or {}).get("edad_etiqueta"),
                            "trigger": "post_cita_pediatrica",
                            "cita_id_previa": id_cita or None,
                        }
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
                    _msg_rea = (
                        f"{titulo}\n\n"
                        f"👤 {paciente['nombre']}\n"
                        f"🏥 {slot['especialidad']} — {slot['profesional']}\n"
                        f"📅 {slot['fecha_display']}\n"
                        f"🕐 {slot['hora_inicio'][:5]}\n\n"
                        "Recuerda llegar *15 minutos antes* con cédula de identidad.\n\n"
                        f"📍 {_CMC_DIRECCION}"
                        f"{extra}"
                        f"{cross_ref}"
                    )
                    # BUG-D: PNI/hitos en segundo mensaje para evitar truncamiento WA
                    if pni_msg and send_whatsapp:
                        await send_whatsapp(phone, _msg_rea + "\n\n_Escribe *menu* si necesitas algo más._")
                        from session import log_message as _lm_f7
                        _lm_f7(phone, "out", _msg_rea + "\n\n_Escribe *menu* si necesitas algo más._", "CONFIRMING_CITA")
                        import asyncio as _asyncio_pni
                        await _asyncio_pni.sleep(2.5)
                        if _pni_telemetria:
                            log_event(phone, "pni_enviado", _pni_telemetria)
                        return pni_msg.strip()
                    return _msg_rea + "\n\n_Escribe *menu* si necesitas algo más._"
                if es_tercero:
                    titulo = f"✅ *¡Listo! La hora de {nombre_corto} quedó reservada.*"
                else:
                    titulo = f"✅ *¡Listo, {nombre_corto}! Tu hora quedó reservada.*"
                _link_video = data.pop("_link_video", None)
                if _link_video:
                    # Confirmación telemedicina — incluye link + instrucciones de pago
                    from config import CMC_TRANSFERENCIA as _CTF_TELE
                    confirmacion_msg = (
                        f"{titulo}\n\n"
                        f"👤 {paciente['nombre']}\n"
                        f"🖥️ {slot['especialidad']} — {slot['profesional']}\n"
                        f"📅 {slot['fecha_display']}\n"
                        f"🕐 {slot['hora_inicio'][:5]}\n"
                        f"📡 *Consulta por videollamada*\n\n"
                        f"*Tu link de videollamada:*\n{_link_video}\n\n"
                        "*Pago (solo transferencia):*\n"
                        # CUENTA REAL desde config (incidente 2026-06-12: acá había
                        # una cuenta INVENTADA — BancoEstado CuentaRUT 16.625.671-3,
                        # "SpA" con CuentaRUT que no existe — 6 pacientes la recibieron).
                        f"{_CTF_TELE['banco']}\n"
                        f"{_CTF_TELE['tipo']} {_CTF_TELE['numero']}\n"
                        f"{_CTF_TELE['titular']}\n"
                        f"RUT: {_CTF_TELE['rut']}\n"
                        "Monto: según lo indicado por recepción\n"
                        "Envía el comprobante a este chat.\n\n"
                        "El link se activa 15 min antes de tu hora.\n\n"
                        f"¡Te esperamos online! 😊{cross_ref}"
                    )
                else:
                    confirmacion_msg = (
                        f"{titulo}\n\n"
                        f"👤 {paciente['nombre']}\n"
                        f"🏥 {slot['especialidad']} — {slot['profesional']}\n"
                        f"📅 {slot['fecha_display']}\n"
                        f"🕐 {slot['hora_inicio'][:5]}\n"
                        f"💳 {modalidad}\n\n"
                        "Recuerda llegar *15 minutos antes* con cédula de identidad.\n\n"
                        f"📍 {_CMC_DIRECCION}\n\n"
                        f"¡Te esperamos! 😊{cross_ref}"
                    )
                # ── Abono Psiquiatría (pedido dueño 2026-06-12): la hora se
                # confirma con un abono por transferencia. Segundo mensaje aparte
                # (mismo patrón PNI: evita el truncamiento "ver más" de WA).
                # Solo presencial — telemedicina ya trae su propio bloque de pago.
                if not _link_video and "psiquiatr" in (slot.get("especialidad") or "").lower():
                    try:
                        from config import CMC_TRANSFERENCIA as _CTF, ABONO_PSIQUIATRIA_CLP as _ABO
                        _abono_txt = (
                            "💳 *Importante — abono para confirmar tu hora de Psiquiatría*\n\n"
                            f"Pedimos un abono de *${_ABO:,} CLP* para asegurar tu hora "
                            "— corresponde al valor total de la consulta; el día de la atención no pagas nada adicional.\n\n"
                            "*Datos para transferir:*\n"
                            f"{_CTF['banco']}\n"
                            f"{_CTF['tipo']} {_CTF['numero']}\n"
                            f"{_CTF['titular']}\n"
                            f"RUT: {_CTF['rut']}\n"
                            f"Correo: {_CTF['correo']}\n\n"
                            "Envía el comprobante por este chat 📎 y recepción deja tu hora confirmada.\n\n"
                            "_Si prefieres, también puedes abonar directamente en recepción._"
                        ).replace(",", ".")
                        from resilience import spawn_task as _spawn_abono
                        async def _send_abono_psiq():
                            import asyncio as _ai_ab
                            await _ai_ab.sleep(4)  # después de la confirmación
                            await send_whatsapp(phone, _abono_txt)
                            from session import log_message as _lm_ab
                            _lm_ab(phone, "out", _abono_txt, "IDLE")
                            log_event(phone, "abono_psiq_instrucciones_enviadas",
                                      {"monto": _ABO, "id_cita": str(data.get("id_cita_creada", ""))})
                        _spawn_abono(_send_abono_psiq())
                    except Exception as _e_abono:
                        log.warning("abono psiquiatría msg falló: %s", _e_abono)

                # BUG-D: PNI/hitos en segundo mensaje separado para evitar truncamiento
                # WA trunca con "ver más" ~1000 chars, ocultando dirección y CTA.
                # ── Tracking referral_source pasivo (sin preguntar al paciente) ──
                # Si la sesión tiene fbclid → proviene de Meta Ads; taggear antes
                # de preguntar para no perder la atribución si salta la pregunta.
                if data.get("fbclid"):
                    save_tag(phone, "referral_source:meta_ads")
                    log_event(phone, "referral_source_auto", {"source": "meta_ads", "fbclid": data["fbclid"][:40]})
                # Si el primer mensaje contiene un slug de blog → SEO orgánico
                else:
                    import re as _re_rs
                    _first_msg = (data.get("first_message") or "")
                    _blog_match = _re_rs.search(r"/blog/([\w-]+)", _first_msg)
                    if _blog_match:
                        _blog_slug = _blog_match.group(1)
                        save_tag(phone, f"referral_source:seo_{_blog_slug}")
                        log_event(phone, "referral_source_auto", {"source": f"seo_{_blog_slug}"})
                # ── fin tracking referral_source ──────────────────────────────

                # Si es paciente nuevo registrado en este flujo, pedir referido
                # como segundo mensaje con botones (post-confirmación, baja fricción).
                # En este caso debemos enviar confirmacion_msg directamente porque
                # el return es un interactivo (botón referral). Logueamos manualmente
                # para que recepción vea el mensaje de confirmación en el panel.
                if data.get("is_paciente_nuevo_post_referral"):
                    save_session(phone, "WAIT_REFERRAL_POST", {})
                    await send_whatsapp(phone, confirmacion_msg)
                    from session import log_message as _log_msg_conf
                    _log_msg_conf(phone, "out", confirmacion_msg, "CONFIRMING_CITA")
                    # PNI/hitos: tercer mensaje via spawn_task para no bloquear
                    if pni_msg:
                        from resilience import spawn_task as _spawn
                        _pni_tel_ref = _pni_telemetria  # captura por closure
                        async def _send_pni_referral():
                            import asyncio as _ai
                            await _ai.sleep(2.5)
                            await send_whatsapp(phone, pni_msg.strip())
                            from session import log_message as _lm
                            _lm(phone, "out", pni_msg.strip(), "WAIT_REFERRAL_POST")
                            if _pni_tel_ref:
                                log_event(phone, "pni_enviado", _pni_tel_ref)
                        _spawn(_send_pni_referral())
                    return _btn_msg(
                        "Una última cosa rápida 🙏\n\n*¿Cómo nos conociste?*",
                        [{"id": "ref_amigo", "title": "👥 Amigo / familiar"},
                         {"id": "ref_rrss", "title": "📱 Redes / Google"},
                         {"id": "ref_recurrente", "title": "🔄 Ya venía antes"}]
                    )
                # Sufijo estándar para cierres de confirmación
                _conf_suffix = "\n\n_Escribe *menu* si necesitas algo más._"
                # Si hay PNI/hitos, enviarlos como segundo mensaje con delay
                # (evita truncamiento WA ~1000 chars) vía spawn_task para no bloquear.
                # main.py enviará y logueará confirmacion_msg normalmente (return abajo).
                if pni_msg:
                    from resilience import spawn_task as _spawn_pni
                    _pni_tel_std = _pni_telemetria  # captura por closure
                    async def _send_pni_delayed():
                        import asyncio as _ai2
                        await _ai2.sleep(2.5)
                        await send_whatsapp(phone, pni_msg.strip())
                        from session import log_message as _lm2
                        _lm2(phone, "out", pni_msg.strip(), get_session(phone).get("state", "IDLE"))
                        if _pni_tel_std:
                            log_event(phone, "pni_enviado", _pni_tel_std)
                    _spawn_pni(_send_pni_delayed())
                # ── Tercero: preguntar parentesco (opcional) ───────────────────
                # Si la cita fue para un familiar, el vínculo ya quedó guardado
                # (heurístico, arriba). Preguntamos el parentesco explícito para
                # saludar por nombre la próxima vez y armar el árbol del portal.
                if es_tercero and not reagendar:
                    _po_par = (get_profile(phone) or {}).get("rut") or ""
                    _dr_par = data.get("rut") or ""
                    if _po_par and _dr_par and _po_par != _dr_par:
                        await send_whatsapp(phone, confirmacion_msg + _conf_suffix)
                        from session import log_message as _lm_par_c
                        _lm_par_c(phone, "out", confirmacion_msg + _conf_suffix, "WAIT_PARENTESCO")
                        _dn_par_c = paciente.get("nombre") or _dr_par
                        save_session(phone, "WAIT_PARENTESCO", {
                            "par_owner_rut": _po_par,
                            "par_dep_rut": _dr_par,
                            "par_dep_nombre": _dn_par_c,
                        })
                        return _list_msg(
                            body_text=(
                                f"Para tenerlo a mano la próxima vez: "
                                f"¿qué es *{_first_name(_dn_par_c)}* tuyo/a? (opcional)"),
                            button_label="Responder",
                            sections=[{"title": "Parentesco", "rows": [
                                {"id": "par_hijo", "title": "Hijo/a"},
                                {"id": "par_padre", "title": "Padre/Madre"},
                                {"id": "par_pareja", "title": "Pareja"},
                                {"id": "par_hermano", "title": "Hermano/a"},
                                {"id": "par_otro", "title": "Otro"},
                                {"id": "par_skip", "title": "Prefiero no decir"},
                            ]}],
                        )
                # ── Cross-sell post-confirmación ──────────────────────────────
                # Solo en citas nuevas (no reagendar), solo si no es tercero.
                # Cooldown: 1 por sesión + 30 días por par.
                # Bug 5 fix: throttle para evitar triple burst.
                # Si ya hay PNI/autocuidado programado, postergamos el cross-sell
                # mínimo 12s (después del PNI). También guardamos cross_sell_sent_ts
                # en data para que cualquier otro disparador (fidelización, etc.)
                # pueda respetar la ventana de 600s.
                import time as _time_cs
                _cs_last_ts = data.get("cross_sell_sent_ts", 0)
                _cs_throttle_ok = ((_time_cs.time() - _cs_last_ts) >= 600)
                if not reagendar and not es_tercero and _cs_throttle_ok:
                    _cs = _cross_sell_interactive(phone, esp, slot)
                    if _cs:
                        _cs_dest = _cs["_cross_sell_esp_destino"]
                        data["cross_sell_sent_ts"] = _time_cs.time()
                        data_cs = {"cross_sell_esp_origen": esp,
                                   "cross_sell_esp_destino": _cs_dest,
                                   "cross_sell_sent_ts": data["cross_sell_sent_ts"]}
                        save_session(phone, "WAIT_CROSS_SELL", data_cs)
                        # Antes de retornar el cross-sell, enviar confirmacion_msg.
                        await send_whatsapp(phone, confirmacion_msg + _conf_suffix)
                        from session import log_message as _log_msg_cs
                        _log_msg_cs(phone, "out", confirmacion_msg + _conf_suffix, "WAIT_CROSS_SELL")
                        import asyncio as _asyncio_cs
                        # Bug 5: si hay PNI spawneado (llega a 2.5s), esperar 5s
                        # para que el cross-sell llegue mínimo 3s después del PNI.
                        _cs_delay = 5.5 if pni_msg else 1.5
                        await _asyncio_cs.sleep(_cs_delay)
                        return _cs["payload"]
                # ── Self: ofrecer agendar a otra persona (familiar) ────────────
                # Cierre estándar post-reserva propia: tras "tu hora quedó
                # reservada", preguntamos si quiere agendar a un familiar. Si
                # acepta, le ofrecemos cupos contiguos con el mismo profesional.
                if not reagendar and not es_tercero:
                    await send_whatsapp(phone, confirmacion_msg + _conf_suffix)
                    from session import log_message as _lm_otro_c
                    _lm_otro_c(phone, "out", confirmacion_msg + _conf_suffix, "WAIT_AGENDAR_OTRO")
                    save_session(phone, "WAIT_AGENDAR_OTRO", {
                        "last_booked": {
                            "especialidad": esp,
                            "id_profesional": slot.get("id_profesional"),
                            "profesional": slot.get("profesional"),
                            "fecha": slot.get("fecha"),
                            "fecha_display": slot.get("fecha_display"),
                            "hora_inicio": slot.get("hora_inicio"),
                            "modalidad": data.get("modalidad", "particular"),
                        }
                    })
                    return _btn_msg(
                        "¿Deseas agendar una hora para otra persona (un familiar)?",
                        [{"id": "otro_si", "title": "✅ Sí"},
                         {"id": "otro_no", "title": "No, gracias"}]
                    )
                # Caso normal: main.py envía y loguea el mensaje de confirmación.
                return confirmacion_msg + _conf_suffix
            else:
                # FIX Capa-2 (2026-06-12): si el slot era un sobrecupo y Medilink lo
                # rechazó (400 "tope con otra cita"), es una carrera — otro paciente lo
                # tomó primero. En lugar del error genérico que pierde al paciente,
                # re-salvar la sesión en WAIT_SLOT con el slot fallido filtrado para
                # que pueda ver otros horarios sin re-intentar el slot ya ocupado.
                # reset_session() ya se ejecutó arriba, así que salvamos de nuevo.
                if slot.get("sobrecupo"):
                    log_event(phone, "sobrecupo_rechazado_race", {
                        "fecha": slot.get("fecha"),
                        "hora": slot.get("hora_inicio"),
                        "profesional": slot.get("profesional", ""),
                    })
                    _slot_key = (slot.get("fecha"), slot.get("hora_inicio"))
                    _slots_ok = [s for s in (data.get("slots") or [])
                                 if (s.get("fecha"), s.get("hora_inicio")) != _slot_key]
                    _todos_ok = [s for s in (data.get("todos_slots") or [])
                                 if (s.get("fecha"), s.get("hora_inicio")) != _slot_key]
                    data["slots"] = _slots_ok
                    data["todos_slots"] = _todos_ok
                    data.pop("slot_sugerido", None)  # ya consumido antes de llegar aquí
                    save_session(phone, "WAIT_SLOT", data)
                    return _btn_msg(
                        "Esa hora ya fue reservada por otro paciente 😕\n\n"
                        "¿Quieres ver otros horarios disponibles?",
                        [
                            {"id": "ver_otros", "title": "📋 Ver otros horarios"},
                            {"id": "otro_dia",  "title": "📅 Otro día"},
                        ]
                    )
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
            "Responde *Sí* para confirmar, o toca *❌ Cambiar* para modificar.",
            [{"id": "si", "title": "✅ Sí, reservar"},
             {"id": "cambiar_datos", "title": "❌ Cambiar"}]
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
            # BUG-C: 74% abandono. Contador con escalación tras 2 intentos fallidos.
            _rut_cancel_intentos = data.get("rut_cancelar_intentos", 0) + 1
            data["rut_cancelar_intentos"] = _rut_cancel_intentos
            if _rut_cancel_intentos >= 2:
                log_event(phone, "rut_cancelar_escalado", {"intentos": _rut_cancel_intentos})
                save_session(phone, "HUMAN_TAKEOVER",
                             {"hold_sent": False, "handoff_reason": "rut_cancelar_fallos"})
                return _btn_msg(
                    "No pude validar el RUT 😕\n\n"
                    "Una recepcionista puede cancelar tu cita directamente.",
                    [
                        {"id": "accion_recepcion", "title": "Hablar con recepción"},
                        {"id": "menu_volver", "title": "Volver al menú"},
                    ]
                )
            save_session(phone, "WAIT_RUT_CANCELAR", data)
            return hint_rut_error(txt) + "\n\n_Escribe *menu* si prefieres volver._"

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

        # BUG-01: fallback a citas_bot local para cubrir lag de indexación de Medilink.
        # Caso real: Beatriz creó cita y al cancelar inmediatamente, Medilink aún
        # no la indexó → devolvía lista vacía. Lógica:
        #   lista_A = Medilink (puede ser [] por lag o por error de red)
        #   lista_B = citas_bot local (solo citas recientes ≤10 min, con id_cita)
        #   merge deduplic. por id → si ambas vacías → mensaje normal
        medilink_error = False
        try:
            citas_medilink = await listar_citas_paciente(paciente["id"], rut=paciente.get("rut"))
        except Exception as _e:
            log.warning("WAIT_RUT_CANCELAR: listar_citas_paciente falló phone=%s: %s", phone, _e)
            citas_medilink = []
            medilink_error = True

        # Fallback local: solo si Medilink devuelve vacío
        if not citas_medilink:
            citas_local = get_citas_bot_futuras(phone, max_age_minutes=10)
            if citas_local:
                log_event(phone, "cancelar_fallback_local", {
                    "n_local": len(citas_local),
                    "medilink_error": medilink_error,
                })
            citas = citas_local
        else:
            # Medilink devolvió resultados: agregar locales que no estén ya (dedup por id)
            ids_medilink = {str(c["id"]) for c in citas_medilink}
            citas_local_extra = [
                c for c in get_citas_bot_futuras(phone, max_age_minutes=10)
                if str(c["id"]) not in ids_medilink
            ]
            citas = citas_medilink + citas_local_extra

        if not citas:
            # Buscar citas de familiares antes de responder "no hay"
            try:
                familiares_con_citas = await _buscar_citas_familiares(rut)
            except Exception:
                familiares_con_citas = []
            if familiares_con_citas:
                citas_planas = _flatten_citas_familiares(familiares_con_citas)
                data.update({"citas_familiares": citas_planas, "rut": rut})
                save_session(phone, "WAIT_CITA_CANCELAR_FAMILIAR", data)
                log_event(phone, "cancelar_familiar_sugerido", {"rut": rut, "n_citas": len(citas_planas)})
                return _format_citas_familiares_cancelar(familiares_con_citas)
            reset_session(phone)
            return (
                f"No tienes citas futuras agendadas, *{_first_name(paciente.get('nombre'))}* 📋\n\n"
                "Si la hora está a nombre de otra persona (hijo/a, familiar), "
                "escribe su RUT y la busco."
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
        _esp_c = cita.get('especialidad', '')
        _prof_label_c = f"{_esp_c} — {cita['profesional']}" if _esp_c else cita['profesional']
        return _btn_msg(
            f"Vas a cancelar esta hora:\n\n"
            f"🏥 {_prof_label_c}\n"
            f"📅 {cita['fecha_display']}\n"
            f"🕐 {cita['hora_inicio'][:5]}\n\n"
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
            # BUG-4 FIX: preservar _intent_pendiente antes de reset_session
            _intent_pendiente_cancel = data.get("_intent_pendiente")
            # F134: preservar especialidad de la cita cancelada para ofrecerla
            # directamente si el paciente quiere agendar de nuevo.
            _esp_cita_cancelada = (cita.get("especialidad") or "").lower().strip()
            ok = await cancelar_cita(cita["id"])
            reset_session(phone)
            if ok:
                # Guardar la especialidad en la sesión recién reseteada para que el
                # botón "Sí, agendar" la use directamente sin volver a preguntar.
                if _esp_cita_cancelada:
                    _s_post_cancel = get_session(phone)
                    _s_post_cancel["data"]["_post_cancel_esp"] = _esp_cita_cancelada
                    save_session(phone, "IDLE", _s_post_cancel["data"])
                log_event(phone, "cita_cancelada", {"id_cita": cita["id"], "profesional": cita.get("profesional")})
                save_tag(phone, "canceló")
                # ── Notificación al profesional (push WA, ventana 24h, $0) ───
                try:
                    import prof_notifications as _pn_c
                    from medilink import PROFESIONALES as _PROFS_C
                    _prof_nombre_c = cita.get("profesional", "")
                    _id_prof_c = next(
                        (pid for pid, info in _PROFS_C.items()
                         if info.get("nombre") == _prof_nombre_c),
                        None
                    )
                    if _id_prof_c:
                        from resilience import spawn_task as _spawn_c
                        _pac_nombre_c = get_profile(phone).get("nombre", "") if get_profile(phone) else ""
                        _spawn_c(_pn_c.notify_cancelacion(
                            id_prof=_id_prof_c,
                            profesional_nombre=_prof_nombre_c,
                            paciente_nombre=_pac_nombre_c,
                            fecha=cita.get("fecha", ""),
                            hora=cita.get("hora_inicio", ""),
                            id_cita=str(cita.get("id", "")),
                        ), name=f"prof_notif_cancel_{_id_prof_c}")
                except Exception as _pn_c_err:
                    log.warning("prof_notif_cancelacion falló: %s", _pn_c_err)
                # ── Event-driven: notificar waitlist al instante ──
                esp_cancelada = cita.get("especialidad", "")
                if esp_cancelada:
                    try:
                        waiters = get_waitlist_by_especialidad(esp_cancelada)
                        for w in waiters[:3]:  # notificar hasta 3 personas
                            w_phone = w["phone"]
                            w_nombre = (w.get("nombre") or "").split()
                            w_saludo = f"*{w_nombre[0]}*" if w_nombre else ""
                            _wl_msg = (
                                f"Hola {w_saludo} 👋 ¡Se acaba de liberar una hora de "
                                f"*{esp_cancelada}* con *{cita.get('profesional', '')}*!\n\n"
                                f"📅 *{cita.get('fecha_display', '')}* a las *{cita.get('hora_inicio', '')[:5]}*\n\n"
                                "Escribe *menu* ahora para reservarla antes de que se llene."
                            )
                            await send_whatsapp(w_phone, _wl_msg)
                            from session import log_message as _lm_f8
                            _lm_f8(w_phone, "out", _wl_msg, "IDLE")
                            mark_waitlist_notified(w["id"])
                            log_event(w_phone, "waitlist_notificado_cancelacion", {
                                "especialidad": esp_cancelada, "cita_cancelada": cita["id"],
                            })
                    except Exception as e:
                        log.warning("Error notificando waitlist post-cancel: %s", e)
                # BUG-4: si venía con multi_intent, el mensaje es más explícito
                _cancel_suffix = (
                    "\n\nCancelé tu cita. ¿Quieres ahora agendar una nueva hora?"
                    if _intent_pendiente_cancel == "agendar"
                    else "\n\n¿Quieres agendar otra hora?"
                )
                return _btn_msg(
                    f"✅ Cita cancelada.\n\n"
                    f"_{cita['profesional']} · {cita['fecha_display']} · {cita['hora_inicio'][:5]}_"
                    f"{_cancel_suffix}",
                    [
                        {"id": "1", "title": "Sí, agendar"},
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
            # F032: guardar la preferencia de franja para pre-filtrar slots.
            # (Antes se prometía "anoté tu preferencia" pero nunca se leía.)
            data["reagendar_preferencia"] = txt[:120]
            save_session(phone, "WAIT_RUT_REAGENDAR", data)
            return (
                "Primero necesito tu *RUT* para buscar tu cita actual 🗓️\n"
                "(ej: *12.345.678-9*)\n\n"
                "Cuando me lo des, buscamos los nuevos horarios."
            )
        rut = clean_rut(txt)
        if not valid_rut(rut):
            # BUG-C: 80% abandono. Contador con escalación tras 2 intentos fallidos.
            _rut_reag_intentos = data.get("rut_reagendar_intentos", 0) + 1
            data["rut_reagendar_intentos"] = _rut_reag_intentos
            if _rut_reag_intentos >= 2:
                log_event(phone, "rut_reagendar_escalado", {"intentos": _rut_reag_intentos})
                save_session(phone, "HUMAN_TAKEOVER",
                             {"hold_sent": False, "handoff_reason": "rut_reagendar_fallos"})
                return _btn_msg(
                    "No pude validar el RUT 😕\n\n"
                    "Una recepcionista puede ayudarte a reagendar directamente.",
                    [
                        {"id": "accion_recepcion", "title": "Hablar con recepción"},
                        {"id": "menu_volver", "title": "Volver al menú"},
                    ]
                )
            save_session(phone, "WAIT_RUT_REAGENDAR", data)
            return hint_rut_error(txt) + "\n\n_Escribe *menu* si prefieres volver._" 

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
            # Buscar citas de familiares antes de responder "no hay"
            try:
                familiares_con_citas = await _buscar_citas_familiares(rut)
            except Exception:
                familiares_con_citas = []
            if familiares_con_citas:
                citas_planas = _flatten_citas_familiares(familiares_con_citas)
                data.update({"citas_familiares": citas_planas, "rut": rut})
                save_session(phone, "WAIT_CITA_REAGENDAR_FAMILIAR", data)
                log_event(phone, "reagendar_familiar_sugerido", {"rut": rut, "n_citas": len(citas_planas)})
                return _format_citas_familiares_reagendar(familiares_con_citas)
            reset_session(phone)
            return (
                f"No tienes citas futuras agendadas, *{_first_name(paciente.get('nombre'))}* 📋\n\n"
                "Si la hora está a nombre de otra persona (hijo/a, familiar), "
                "escribe su RUT y la busco."
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
        # Guardar fecha+hora de la cita que se reagenda para excluirla de los
        # slots ofrecidos (bug P1-A: la cita propia aparecía como disponible).
        _hora_excluir = cita_old.get("hora_inicio", "")[:5]
        _fecha_excluir = cita_old.get("fecha", "")
        if _hora_excluir and _fecha_excluir:
            data["_reagendar_excluir"] = (_fecha_excluir, _hora_excluir)
        log_event(phone, "reagendar_elegida_cita",
                  {"id_cita": cita_old["id"], "especialidad": esp_lower})
        return await _iniciar_agendar(phone, data, esp_lower)

    # ── WAIT_WAITLIST_CONFIRM_ECOCA ──────────────────────────────────────────
    # Estado especial para ecocardiograma: usa la waitlist estándar pero con
    # mensaje de confirmación propio y sin ofrecer slots de Medilink.
    if state == "WAIT_WAITLIST_CONFIRM_ECOCA":
        tl_ec = tl.lower().strip()
        if tl_ec == "ecoca_waitlist_si" or tl_ec in AFIRMACIONES or tl_norm in AFIRMACIONES:
            perfil = get_profile(phone)
            if perfil:
                data["rut"] = perfil["rut"]
                data["paciente_nombre"] = perfil["nombre"]
            if not data.get("rut"):
                # Sin RUT → pedir antes de inscribir
                save_session(phone, "WAIT_WAITLIST_RUT_ECOCA", data)
                return (
                    "Perfecto, para anotarte necesito tu RUT:\n"
                    "(ej: *12.345.678-9*)"
                    + _PRIVACY_NOTE
                )
            wid = add_to_waitlist(
                phone,
                data.get("rut", ""),
                data.get("paciente_nombre", ""),
                "ecocardiograma",
                60,
                notas="precio $110.000 particular, espera fecha mensual cardiólogo Dr. Millán",
            )
            save_tag(phone, "waitlist-ecocardiograma")
            log_event(phone, "waitlist_ecocardiograma_inscrito",
                      {"id": wid, "phone": phone, "rut": data.get("rut", "")})
            reset_session(phone)
            nombre_corto = _first_name(data.get("paciente_nombre", ""))
            saludo = f"*{nombre_corto}*, " if nombre_corto else ""
            return (
                f"Listo, {saludo}quedaste anotado en la lista de espera para *ecocardiograma*. "
                "Cuando el Dr. Millán confirme la próxima fecha (es una vez al mes), "
                "te avisamos por aquí.\n\n"
                "_Escribe *menu* si necesitas algo más._"
            )
        if tl_ec in ("ecoca_waitlist_no", "ecoca_menu") or tl_ec in NEGACIONES or tl_norm in NEGACIONES:
            reset_session(phone)
            return (
                "Sin problema. Cuando quieras anotarte o necesites otra cosa, escríbenos.\n"
                f"_Recepción: 📞 *{CMC_TELEFONO}*_"
            )
        return (
            "Responde *Sí* para anotarte en la lista de espera del ecocardiograma "
            "o *No* si prefieres llamar a recepción."
        )

    # ── WAIT_WAITLIST_RUT_ECOCA ───────────────────────────────────────────────
    if state == "WAIT_WAITLIST_RUT_ECOCA":
        rut = clean_rut(txt)
        if not valid_rut(rut):
            return hint_rut_error(txt)
        _ensure_consent(phone)
        data["rut"] = rut
        paciente = await buscar_paciente(rut)
        if paciente:
            data["paciente_nombre"] = paciente["nombre"]
            save_profile(phone, rut, paciente["nombre"])
        wid = add_to_waitlist(
            phone,
            rut,
            data.get("paciente_nombre", ""),
            "ecocardiograma",
            60,
            notas="precio $110.000 particular, espera fecha mensual cardiólogo Dr. Millán",
        )
        save_tag(phone, "waitlist-ecocardiograma")
        log_event(phone, "waitlist_ecocardiograma_inscrito",
                  {"id": wid, "phone": phone, "rut": rut})
        reset_session(phone)
        nombre_corto = _first_name(data.get("paciente_nombre", ""))
        saludo = f"*{nombre_corto}*, " if nombre_corto else ""
        return (
            f"Listo, {saludo}quedaste anotado en la lista de espera para *ecocardiograma*. "
            "Cuando el Dr. Millán confirme la próxima fecha (es una vez al mes), "
            "te avisamos por aquí.\n\n"
            "_Escribe *menu* si necesitas algo más._"
        )

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
        # FIX 1c-pre: si el texto empieza con negación aunque contenga keyword
        # de especialidad (ej: "No del Otorrino"), tratar como negación antes
        # de reciclar como IDLE. Evita slug "no otorrino".
        _tl_empieza_no = bool(re.match(r"^no\b", tl.strip(), re.IGNORECASE))
        if any(kw in tl for kw in _NUEVO_INTENT_KW) and not _tl_empieza_no:
            log_event(phone, "waitlist_confirm_break", {"txt": txt[:120]})
            reset_session(phone)
            # FIX-10: pasar dict limpio en lugar de get_session() post-reset
            # (evita lectura redundante a SQLite y fragilidad ante busy).
            return await handle_message(phone, txt, {"state": "IDLE", "data": {}})

        # FIX 1 (2026-06-10): catch-all de negación ampliada — variantes que no
        # están en NEGACIONES estricto pero son claramente un "no" contextual.
        # Caso prod: "No del Otorrino" → normalizador de especialidades generaba
        # slug "no otorrino" y el bot respondía "no tengo horas para no otorrino".
        _NEGACION_AMPLIADA = re.compile(
            r"^(no\b|nop\b|nope\b|no gracias\b|no,?\s+gracias|nel\b|negativo\b"
            r"|cuando pueda\b|después\b|despues\b|la llamo\b|los llamo\b"
            r"|le llamo\b|voy a llamar\b|prefiero llamar\b|gracias,?\s*pero\b"
            r"|por ahora no\b|ahora no\b|no por ahora\b"
            r"|gracias$)",  # "gracias" a secas sin más texto = rechazo cortés
            re.IGNORECASE,
        )
        if _NEGACION_AMPLIADA.match(tl.strip()):
            log_event(phone, "waitlist_negacion_ampliada", {"txt": txt[:120]})
            reset_session(phone)
            return (
                "Sin problema 😊 Cuando lo necesites, escríbenos.\n"
                f"_Llama a recepción: 📞 *{CMC_TELEFONO}* · ☎️ *{CMC_TELEFONO_FIJO}*_"
            )

        # FIX 1b: tope de repeticiones — si el bot ya envió el prompt binario 2 veces
        # sin respuesta válida, salir a IDLE en vez de seguir repromptando.
        _wl_reprompts = data.get("_wl_reprompts", 0) + 1
        data["_wl_reprompts"] = _wl_reprompts
        if _wl_reprompts >= 2:
            log_event(phone, "waitlist_confirm_timeout", {"reprompts": _wl_reprompts, "txt": txt[:120]})
            reset_session(phone)
            return "Quedo atento si necesitas algo más 🙂"
        save_session(phone, "WAIT_WAITLIST_CONFIRM", data)
        return "Responde *SÍ* para inscribirte o *NO* si prefieres llamar a recepción."

    # ── WAIT_WAITLIST_RUT ─────────────────────────────────────────────────────
    if state == "WAIT_WAITLIST_RUT":
        rut = clean_rut(txt)
        if not valid_rut(rut):
            return hint_rut_error(txt)
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
    # BUG-C: 100% abandono. Skip silencioso tras 1 intento fallido — la cita
    # ya no existe (waitlist), el paciente no tiene incentivo de seguir.
    if state == "WAIT_WAITLIST_NOMBRE":
        partes = txt.strip().split()
        # Guard semántico compartido con WAIT_DATOS_NUEVO: rechazar frases que
        # no son nombres (peticiones, verbos de necesidad, términos médicos).
        _NOMBRE_INVALIDO_WL = re.compile(
            r"\b(necesito|quiero|tengo|quisiera|necesita|requiero|solicito|"
            r"ficha|doctor|doctora|dra?\.?|radiograf|ecograf|examen|hora|cita|"
            r"consulta|control|costo|precio|valor|cuanto|cuando|agendar|cancelar|"
            r"ver\s+mis|mis\s+citas|mis\s+horas|urgente|urgencia)\b",
            re.IGNORECASE,
        )
        _nombre_es_frase = _NOMBRE_INVALIDO_WL.search(txt.strip())
        if len(partes) < 2 or _nombre_es_frase:
            _intentos_wn = data.get("waitlist_nombre_intentos", 0) + 1
            if _intentos_wn >= 1:
                # Skip: usar "paciente" genérico y completar la inscripción
                log_event(phone, "waitlist_nombre_skipped",
                          {"intentos": _intentos_wn, "txt": txt[:60]})
                data["paciente_nombre"] = "Paciente"
                return _inscribir_waitlist_y_responder(phone, data)
            data["waitlist_nombre_intentos"] = _intentos_wn
            save_session(phone, "WAIT_WAITLIST_NOMBRE", data)
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
            # BUG-C: 96% abandono en WAIT_RUT_VER. Tras 2 intentos fallidos,
            # ofrecer derivación a recepción en vez de seguir pidiendo RUT.
            _rut_ver_intentos = data.get("rut_ver_intentos", 0) + 1
            data["rut_ver_intentos"] = _rut_ver_intentos
            if _rut_ver_intentos >= 2:
                log_event(phone, "rut_ver_escalado", {"intentos": _rut_ver_intentos})
                save_session(phone, "HUMAN_TAKEOVER",
                             {"hold_sent": False, "handoff_reason": "rut_ver_fallos"})
                return _btn_msg(
                    "No pude validar el RUT 😕\n\n"
                    "¿Quieres que te ayude una recepcionista?",
                    [
                        {"id": "accion_recepcion", "title": "Hablar con recepción"},
                        {"id": "menu_volver",      "title": "Volver al menú"},
                    ]
                )
            save_session(phone, "WAIT_RUT_VER", data)
            return hint_rut_error(txt) + "\n\n_Escribe *menu* si prefieres volver._"

        _ensure_consent(phone)
        paciente, transient = await _buscar_paciente_safe(rut)
        if transient:
            save_session(phone, "HUMAN_TAKEOVER", data)
            return _msg_medilink_transient()
        if not paciente:
            reset_session(phone)
            return _btn_msg(
            "No encontré ese RUT 🔎\n\n¿Intentamos de nuevo?",
            [
                {"id": "menu", "title": "🏠 Volver al inicio"},
                {"id": "accion_recepcion", "title": "💬 Hablar con recepción"},
            ]
        )

        try:
            citas = await listar_citas_paciente(
                paciente["id"], rut=paciente.get("rut"), raise_on_error=True
            )
        except Exception as e:
            # Medilink falló: NO decirle al paciente "no tienes citas" (falso).
            log.warning("ver_reservas: listar_citas falló phone=%s: %s", phone, e)
            log_event(phone, "ver_reservas_medilink_fallo", {"error": str(e)[:200]})
            reset_session(phone)
            return (
                "No pude consultar tus citas en este momento porque el sistema "
                "de agenda está lento 😕\n\n"
                "Intenta de nuevo en unos minutos, o llama a recepción:\n"
                f"📞 *{CMC_TELEFONO}*"
            )
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
            lineas.append(f"• {c['fecha_display']} {c['hora_inicio'][:5]} — {c['profesional']}")
        body = "\n".join(lineas)
        return _btn_msg(
            body,
            [
                {"id": "1", "title": "📅 Agendar otra"},
                {"id": "menu_volver", "title": "Volver al menú"},
            ]
        )

    # ── WAIT_CITA_CANCELAR_FAMILIAR ───────────────────────────────────────────
    # El usuario llegó acá porque su propio RUT no tenía citas pero sí las
    # tienen sus familiares vinculados. Le mostramos esa lista y esperamos
    # que elija un número, o que mande un RUT de familiar no vinculado.
    if state == "WAIT_CITA_CANCELAR_FAMILIAR":
        citas_planas = data.get("citas_familiares", [])
        _SET_SALIR = {"menu", "menú", "salir", "atras", "atrás"}
        if tl in _SET_SALIR or tl_norm in _SET_SALIR:
            reset_session(phone)
            return "Perfecto, no cancelamos nada 😊\n_Escribe *menu* si necesitas algo más._"
        # ¿Es un RUT? → buscar por ese RUT directamente
        try:
            from medilink import clean_rut as _cr_f, valid_rut as _vr_f
            _rut_f = _cr_f(txt)
            if _vr_f(_rut_f):
                pac_f, transient_f = await _buscar_paciente_safe(_rut_f)
                if transient_f:
                    save_session(phone, "HUMAN_TAKEOVER", data)
                    return _msg_medilink_transient()
                if not pac_f:
                    save_session(phone, "WAIT_CITA_CANCELAR_FAMILIAR", data)
                    return "No encontré ese RUT 🔎\n\nElige el número de la lista o escribe *menu* para volver."
                citas_f = await listar_citas_paciente(pac_f["id"], rut=pac_f.get("rut"))
                if not citas_f:
                    save_session(phone, "WAIT_CITA_CANCELAR_FAMILIAR", data)
                    return (
                        f"No hay citas futuras para *{_first_name(pac_f.get('nombre'))}* 📋\n\n"
                        "Elige el número de la lista o escribe *menu*."
                    )
                data.update({"paciente": pac_f, "citas": citas_f})
                save_session(phone, "WAIT_CITA_CANCELAR", data)
                log_event(phone, "cancelar_familiar_por_rut", {"rut": _rut_f})
                return _format_citas_cancelar(citas_f, pac_f["nombre"])
        except Exception:
            pass
        # ¿Es un número de la lista?
        try:
            idx = int(txt) - 1
            if 0 <= idx < len(citas_planas):
                cita = citas_planas[idx]
                pac_sel = cita.get("_familiar_paciente", {})
                data.update({"paciente": pac_sel, "citas": [cita], "cita_cancelar": cita})
                save_session(phone, "CONFIRMING_CANCEL", data)
                _esp_c = cita.get("especialidad", "")
                _prof_c = cita.get("profesional", "")
                _label_c = f"{_esp_c} — {_prof_c}" if _esp_c else _prof_c
                _nombre_pac = _first_name(pac_sel.get("nombre", "")) if pac_sel else ""
                return _btn_msg(
                    f"Vas a cancelar esta hora de *{_nombre_pac}*:\n\n"
                    f"🏥 {_label_c}\n"
                    f"📅 {cita['fecha_display']}\n"
                    f"🕐 {cita['hora_inicio'][:5]}\n\n"
                    "¿Confirmas la cancelación?",
                    [
                        {"id": "si", "title": "✅ Sí, cancelar"},
                        {"id": "no", "title": "❌ No, mantener"},
                    ]
                )
        except (ValueError, TypeError):
            pass
        retries = data.get("familiar_cancelar_retries", 0) + 1
        if retries >= 3:
            save_session(phone, "HUMAN_TAKEOVER", {"hold_sent": False, "handoff_reason": "familiar_cancelar_retries"})
            return _btn_msg(
                "No logro entender la selección 😕\n\nUna recepcionista puede ayudarte.",
                [{"id": "accion_recepcion", "title": "Hablar con recepción"}],
            )
        data["familiar_cancelar_retries"] = retries
        save_session(phone, "WAIT_CITA_CANCELAR_FAMILIAR", data)
        return f"Elige un número entre 1 y {len(citas_planas)}, o escribe el RUT del familiar 😊"

    # ── WAIT_CITA_REAGENDAR_FAMILIAR ──────────────────────────────────────────
    if state == "WAIT_CITA_REAGENDAR_FAMILIAR":
        citas_planas = data.get("citas_familiares", [])
        _SET_SALIR = {"menu", "menú", "salir", "atras", "atrás"}
        if tl in _SET_SALIR or tl_norm in _SET_SALIR:
            reset_session(phone)
            return "Perfecto, dejamos las citas como están 😊\n_Escribe *menu* si necesitas algo más._"
        # ¿Es un RUT? → buscar por ese RUT directamente
        try:
            from medilink import clean_rut as _cr_rf, valid_rut as _vr_rf
            _rut_rf = _cr_rf(txt)
            if _vr_rf(_rut_rf):
                pac_rf, transient_rf = await _buscar_paciente_safe(_rut_rf)
                if transient_rf:
                    save_session(phone, "HUMAN_TAKEOVER", data)
                    return _msg_medilink_transient()
                if not pac_rf:
                    save_session(phone, "WAIT_CITA_REAGENDAR_FAMILIAR", data)
                    return "No encontré ese RUT 🔎\n\nElige el número de la lista o escribe *menu* para volver."
                citas_rf = await listar_citas_paciente(pac_rf["id"], rut=pac_rf.get("rut"))
                if not citas_rf:
                    save_session(phone, "WAIT_CITA_REAGENDAR_FAMILIAR", data)
                    return (
                        f"No hay citas futuras para *{_first_name(pac_rf.get('nombre'))}* 📋\n\n"
                        "Elige el número de la lista o escribe *menu*."
                    )
                data.update({"paciente": pac_rf, "citas": citas_rf, "rut": _rut_rf})
                save_session(phone, "WAIT_CITA_REAGENDAR", data)
                log_event(phone, "reagendar_familiar_por_rut", {"rut": _rut_rf})
                return _format_citas_reagendar(citas_rf, pac_rf["nombre"])
        except Exception:
            pass
        # ¿Es un número de la lista?
        try:
            idx = int(txt) - 1
            if 0 <= idx < len(citas_planas):
                cita = citas_planas[idx]
                pac_sel = cita.get("_familiar_paciente", {})
                # Aquí pasamos al handler normal de reagendar — data["citas"] = [cita] y paciente = familiar
                data.update({"paciente": pac_sel, "citas": [cita], "rut": pac_sel.get("rut", "")})
                save_session(phone, "WAIT_CITA_REAGENDAR", data)
                log_event(phone, "reagendar_familiar_elegido", {"rut": pac_sel.get("rut", "")})
                return _format_citas_reagendar([cita], pac_sel.get("nombre", ""))
        except (ValueError, TypeError):
            pass
        retries = data.get("familiar_reagendar_retries", 0) + 1
        if retries >= 3:
            save_session(phone, "HUMAN_TAKEOVER", {"hold_sent": False, "handoff_reason": "familiar_reagendar_retries"})
            return _btn_msg(
                "No logro entender la selección 😕\n\nUna recepcionista puede ayudarte.",
                [{"id": "accion_recepcion", "title": "Hablar con recepción"}],
            )
        data["familiar_reagendar_retries"] = retries
        save_session(phone, "WAIT_CITA_REAGENDAR_FAMILIAR", data)
        return f"Elige un número entre 1 y {len(citas_planas)}, o escribe el RUT del familiar 😊"

    # ── WAIT_DATOS_NUEVO (registro en un solo mensaje) ────────────────────────
    if state == "WAIT_DATOS_NUEVO":
        raw = txt.strip()

        # ── Filtrar prefijos que son respuesta a la pregunta "¿es primera vez?" ──
        # Caso real: paciente escribe "Si primera vez\nLeonor Eduvijes\n..."
        # El parser tomaba "Si primera vez" como nombre. Estos tokens se descartan.
        _PREFIJOS_PRIMERA_VEZ = re.compile(
            r'^(s[ií]\s+primera\s+vez|primera\s+vez|primera|s[ií]|no|control|'
            r'continuaci[oó]n|seguimiento|segunda\s+vez|es\s+primera|es\s+primera\s+vez)\s*$',
            re.I
        )

        # ── Separar por comas, punto y coma, pipe, barras, saltos de línea
        # y guiones/raya larga con espacios ("Ruth - Femenino - 28/05/1939"). ──
        parts_raw = [p.strip() for p in re.split(r'[,;|/\n]+|\s+[-–—]+\s+', raw) if p.strip()]
        # Descartar partes que son solo prefijos de respuesta previa (no son nombre)
        parts = [p for p in parts_raw if not _PREFIJOS_PRIMERA_VEZ.match(p)]

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
        # Frase reservada de terceros ("otra persona", "mi hija", "mi esposo"…):
        # NO es un nombre. Antes se registraba un paciente ficticio "Otra Persona"
        # y se agendaba a ese nombre (bug 2026-06-23). Marcar tercero y pedir los
        # datos REALES de esa persona (booking_for_other evita pisar el perfil del
        # dueño del celular).
        if _OTRA_PERSONA_RE.search(nombre_raw.lower()):
            data["booking_for_other"] = True
            save_session(phone, "WAIT_DATOS_NUEVO", data)
            return (
                "¡Entendido, la cita es para otra persona! 😊\n\n"
                "Escríbeme el *nombre y datos de esa persona* separados por comas:\n"
                "*Nombre Apellido, M o F, DD/MM/AAAA*\n\n"
                f"_Ejemplo: {_ej}_{_tip}"
            )
        # Guard semántico: detectar frases que NO son nombres de persona.
        # Casos reales: "Necesito Una Radiografia Transvaginal", "No Tengo Ficha
        # Con El Doctor", "Los". Verbos de necesidad/acción y sustantivos médicos
        # son señales inequívocas de que el paciente escribió su petición, no su nombre.
        _NOMBRE_INVALIDO_KW = re.compile(
            r"\b(necesito|quiero|tengo|quisiera|necesita|requiero|solicito|"
            r"ficha|doctor|doctora|dra?\.?|radiograf|ecograf|examen|hora|cita|"
            r"consulta|control|costo|precio|valor|cuanto|cuando|agendar|cancelar|"
            r"ver\s+mis|mis\s+citas|mis\s+horas|urgente|urgencia)\b",
            re.IGNORECASE,
        )
        if _NOMBRE_INVALIDO_KW.search(nombre_raw):
            return (
                "Parece que escribiste una consulta, no un nombre 😊\n\n"
                "Necesito tu *nombre completo* (nombre y apellido):\n\n"
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

        # A2: guardar perfil solo si es el dueño del celular, no un tercero.
        # Si booking_for_other=True, el RUT/nombre del paciente recién creado
        # pertenece al tercero y NO debe pisar el perfil del dueño del teléfono.
        if not data.get("booking_for_other"):
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
        # FIX 6b: usar sexo para evitar la barra genérica
        _sx_datos = (sexo or (paciente.get("sexo") or "")).upper()
        _flex_datos = "Registrada" if _sx_datos == "F" else "Registrado"
        return _btn_msg(
            f"¡{_flex_datos}, *{nombre}*! 🙌\n\n"
            f"¿Confirmas esta hora?\n\n"
            f"👤 *{paciente['nombre']}*\n"
            f"🏥 *{slot['especialidad']}* — {slot['profesional']}\n"
            f"📅 *{slot['fecha_display']}*\n"
            f"🕐 *{slot['hora_inicio'][:5]}*\n"
            f"💳 *{modalidad}*",
            [
                {"id": "si", "title": "✅ Confirmar"},
                {"id": "cambiar_datos", "title": "❌ Cambiar"},
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
                # ── Doble opt-in de canal email (Ley 21.719) — GATED ──────────
                # Capturamos el "sí" blando al dar el correo y disparamos el correo
                # de confirmación: SOLO el clic en ese correo habilita envíos (nadie
                # recibe marketing sin confirmar). Fire-and-forget — jamás rompe el
                # registro ni lo demora.
                from config import EMAIL_OPTIN_ENABLED as _EMAIL_OPTIN
                if _EMAIL_OPTIN:
                    try:
                        import asyncio as _aio
                        from autopilot.email_tracking import send_optin_confirmation
                        _nom = data.get("reg_nombre") or data.get("nombre") or ""
                        _aio.create_task(send_optin_confirmation(phone, email, _nom))
                        log_event(phone, "email_optin_requested", {"src": "registro"})
                    except Exception as _eo:  # noqa: BLE001
                        log.warning("email opt-in no disparado: %s", _eo)
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
        # FIX 4: si el paciente escribe una pregunta operativa, no descartarla
        # con "Perfecto, gracias". Re-despachar a IDLE para respuesta real.
        _tl_rp = tl.lower()
        _OPERATIVAS_KW = (
            "hora", "horario", "cambiar", "reagendar", "cancelar",
            "precio", "valor", "cuesta", "cuánto", "cuanto",
            "bono", "fonasa", "particular",
            "dónde", "donde", "dirección", "direccion",
            "transvaginal", "procedimiento",
        )
        if any(k in _tl_rp for k in _OPERATIVAS_KW) and len(tl.strip()) >= 4:
            log_event(phone, "referral_post_pregunta_operativa", {"raw": txt[:120]})
            reset_session(phone)
            return await handle_message(phone, txt, {"state": "IDLE", "data": {}})

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

    # ── WAIT_TELEMEDICINA_ESPECIALIDAD ────────────────────────────────────
    # Paciente eligió especialidad; verificar si es primera vez.
    if state == "WAIT_TELEMEDICINA_ESPECIALIDAD":
        _TELE_ESP_MAP = {
            "tele_mg":    "Medicina General",
            "tele_psico": "Psicología",
            "tele_nutri": "Nutrición",
            "tele_cardio": "Cardiología",
        }
        # Detección desde botón o texto libre
        esp_tele = _TELE_ESP_MAP.get(tl)
        if not esp_tele:
            tl_low = tl.lower()
            if any(w in tl_low for w in ("medic", "general", "familiar", "medico")):
                esp_tele = "Medicina General"
            elif any(w in tl_low for w in ("psico", "psicolog")):
                esp_tele = "Psicología"
            elif any(w in tl_low for w in ("nutri")):
                esp_tele = "Nutrición"
            elif any(w in tl_low for w in ("cardio")):
                esp_tele = "Cardiología"
        # BUG-6: si el texto libre corresponde a una especialidad no-telemedicina,
        # no asumir MG silenciosamente — mostrar lista de disponibles.
        _TELE_NO_DISPONIBLES = (
            "traumato", "gine", "otorrino", "orl", "gastro", "matrona",
            "fono", "podo", "kine", "ortod", "endod", "implant", "estetic",
            "ecograf", "maso",
        )
        if not esp_tele and any(k in tl for k in _TELE_NO_DISPONIBLES):
            esp_tele = None  # Forzar el fallback de "no disponible"
        if not esp_tele or tl in ("tele_otro", "otro", "otra"):
            reset_session(phone)
            return (
                "Por ahora ofrecemos videollamada solo para:\n\n"
                "✅ *Medicina General* — controles y recetas crónicas\n"
                "✅ *Psicología* — sesiones de seguimiento\n"
                "✅ *Nutrición* — controles\n"
                "✅ *Cardiología* — interpretación de exámenes\n\n"
                f"Para otras especialidades la atención es presencial en "
                f"*{_CMC_DIRECCION}*.\n\n"
                "Escribe *agendar* si quieres reservar una hora presencial."
            )
        # Guardar especialidad elegida y preguntar si es primera vez
        data["tele_especialidad"] = esp_tele
        save_session(phone, "WAIT_TELEMEDICINA_PRIMERA_VEZ", data)
        log_event(phone, "telemedicina_esp_elegida", {"especialidad": esp_tele})
        return _btn_msg(
            f"Entendido — *{esp_tele}* por videollamada.\n\n"
            "¿Es tu primera vez atendiendo esta especialidad en el CMC?",
            [
                {"id": "tele_primera_si",  "title": "Sí, primera vez"},
                {"id": "tele_primera_no",  "title": "No, ya soy paciente"},
            ]
        )

    # ── WAIT_TELEMEDICINA_PRIMERA_VEZ ─────────────────────────────────────
    if state == "WAIT_TELEMEDICINA_PRIMERA_VEZ":
        esp_tele = data.get("tele_especialidad", "esta especialidad")
        es_primera = tl in ("tele_primera_si", "si", "sí", "primera", "primera vez", "s", "yes")
        es_paciente = tl in ("tele_primera_no", "no", "ya", "ya soy", "ya tengo", "recurrente")
        if not es_primera and not es_paciente:
            # Heurística texto libre
            tl_low = tl.lower()
            if any(w in tl_low for w in ("primera", "nuevo", "nunca", "primera vez")):
                es_primera = True
            elif any(w in tl_low for w in ("ya", "antes", "recurrente", "control", "seguimiento")):
                es_paciente = True
        if not es_primera and not es_paciente:
            return _btn_msg(
                "¿Es tu primera vez con esta especialidad en el CMC?",
                [
                    {"id": "tele_primera_si",  "title": "Sí, primera vez"},
                    {"id": "tele_primera_no",  "title": "No, ya soy paciente"},
                ]
            )
        # MG siempre puede hacer telemedicina (incluso primera vez)
        primera_bloquea = es_primera and esp_tele != "Medicina General"
        if primera_bloquea:
            reset_session(phone)
            return _btn_msg(
                f"Para *{esp_tele}* la primera consulta debe ser presencial "
                "para que el profesional pueda evaluarte bien.\n\n"
                "Las siguientes consultas de seguimiento sí las podemos hacer "
                "por videollamada.\n\n"
                "¿Te agendo una hora presencial para este primer control?",
                [
                    {"id": "agendar_presencial_tele", "title": "Sí, agendar presencial"},
                    {"id": "no_agendar",              "title": "No por ahora"},
                ]
            )
        # Paciente puede hacer telemedicina — mostrar requisitos antes de agendar
        data["telemedicina_modalidad"] = "TELEMEDICINA"
        data["especialidad"] = esp_tele.lower()
        save_session(phone, "WAIT_TELEMEDICINA_REQUISITOS", data)
        log_event(phone, "telemedicina_flujo_ok", {"especialidad": esp_tele, "primera": es_primera})
        return _btn_msg(
            "Perfecto. Antes de agendar, necesitas:\n\n"
            "✓ Conexión a internet estable\n"
            "✓ Celular o computador con cámara y audio\n"
            "✓ Lugar tranquilo y privado\n"
            "✓ Tener a mano tus exámenes o recetas previas\n"
            "✓ Pagar antes de la cita por transferencia (te envío los datos al confirmar)\n\n"
            "¿Continuamos con el agendamiento?",
            [
                {"id": "tele_confirma_requisitos", "title": "Sí, continuar"},
                {"id": "no_agendar",               "title": "No por ahora"},
            ]
        )

    # ── WAIT_TELEMEDICINA_REQUISITOS ──────────────────────────────────────
    if state == "WAIT_TELEMEDICINA_REQUISITOS":
        esp_tele = data.get("tele_especialidad", "medicina general")
        acepta = tl in ("tele_confirma_requisitos", "si", "sí", "s", "ok", "claro",
                        "confirmar", "continuar", "yes")
        if not acepta:
            reset_session(phone)
            return (
                "Entendido. Cuando quieras agendar tu consulta online, "
                "escribe *telemedicina*.\n\n"
                "También puedes agendar presencialmente escribiendo *agendar*."
            )
        # Transferir al flujo normal de agendamiento con flag telemedicina activo
        log_event(phone, "telemedicina_requisitos_aceptados", {"especialidad": esp_tele})
        return await _iniciar_agendar(phone, data, esp_tele.lower())

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
                    # Crear bono pendiente (se activa con primera cita del referido)
                    try:
                        registrar_bono_referral(
                            code=_code,
                            referrer_phone=_ref_data["phone"],
                            referred_phone=phone,
                            tipo_bono="medica_20",
                        )
                    except Exception as _be:
                        log.warning("Error registrando bono referral: %s", _be)
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
        # Guardar perfil con fecha_nacimiento para campaña de cumpleaños.
        # A2: guard — no pisar perfil del dueño si es registro de tercero.
        if not data.get("booking_for_other"):
            save_profile(phone, rut, paciente["nombre"],
                         fecha_nacimiento=data.get("reg_fecha_nacimiento"))
        # ── Meta CAPI: evento CompleteRegistration ─────────────────────────
        try:
            import meta_capi as _mc_reg
            _reg_nom = (paciente.get("nombre") or "").split()
            asyncio.create_task(_mc_reg.send_event(
                "CompleteRegistration",
                phone=phone,
                rut=rut or None,
                first_name=_reg_nom[0] if _reg_nom else nombre or None,
                last_name=_reg_nom[-1] if len(_reg_nom) > 1 else apellidos or None,
                email=data.get("reg_email") or None,
                fbclid=data.get("fbclid"),
                fbclid_ts=data.get("fbclid_ts"),
                ctwa_clid=_ctwa_clid_for(phone),
                custom_data={"registration_method": "whatsapp"},
            ))
        except Exception as _capi_reg_err:
            log.debug("CAPI CompleteRegistration create_task falló: %s", _capi_reg_err)
        # ── fin CAPI ───────────────────────────────────────────────────────
        # FIX 6a: la bienvenida y el código de referido inline interrumpían
        # WAIT_REFERRAL_POST. Se eliminan — el mensaje de confirmación de cita
        # ya cumple la función de bienvenida ("¡Listo, X! Ya estás registrado/a").
        # El código de referido se generó silenciosamente arriba (generate_referral_code).
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
                {"id": "cambiar_datos", "title": "❌ Cambiar"},
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
                try:
                    registrar_bono_referral(
                        code=_code2,
                        referrer_phone=_ref_data2["phone"],
                        referred_phone=phone,
                        tipo_bono="medica_20",
                    )
                except Exception as _be2:
                    log.warning("Error registrando bono referral WAIT_REFERRAL_CODE: %s", _be2)
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
        # A2: guard — no pisar perfil del dueño si es registro de tercero.
        if not data.get("booking_for_other"):
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

    # ── WAIT_ABONO_COMPROBANTE ────────────────────────────────────────────────
    # Estado post abono-gate: la cita NO fue creada aún. El paciente debe
    # enviar el comprobante de transferencia. Manejamos 4 casos:
    #   (A) imagen → leer_comprobante → crear cita + INSERT abonos_cmc
    #   (B) texto "ya transferí" / "ya envié" sin imagen → recordar que mande foto
    #   (C) texto "no puedo transferir" / "en recepción" → derivar a recepción (HUMAN_TAKEOVER)
    #   (D) texto libre genérico → recordar que esperamos comprobante
    #
    # El gate de imagen es el PRIMERO en llamarse (desde main.py) cuando llega
    # un msg_type="image" con el estado WAIT_ABONO_COMPROBANTE. Cuando el paciente
    # escribe texto el handler llega acá vía el pipeline normal de handle_message.
    # El timeout de 90 min se verifica perezosamente al llegar cualquier mensaje.
    if state == "WAIT_ABONO_COMPROBANTE":
        from datetime import datetime as _dt_ag, timezone as _tz_ag_utc
        from zoneinfo import ZoneInfo as _ZI_ag
        _CHILE_TZ_ag = _ZI_ag("America/Santiago")

        # ── Verificar timeout 90 min ─────────────────────────────────────────
        _gate_ts_str = data.get("abono_gate_ts", "")
        _gate_expirado = False
        if _gate_ts_str:
            try:
                _gate_dt = _dt_ag.fromisoformat(_gate_ts_str)
                if _gate_dt.tzinfo is None:
                    _gate_dt = _gate_dt.replace(tzinfo=_CHILE_TZ_ag)
                _elapsed_min = (_dt_ag.now(_CHILE_TZ_ag) - _gate_dt).total_seconds() / 60
                _gate_expirado = _elapsed_min > 90
            except Exception:
                pass  # fromisoformat falla → ignorar, no bloquear

        if _gate_expirado:
            log_event(phone, "abono_gate_timeout", {
                "gate_ts": _gate_ts_str,
            })
            reset_session(phone)
            return (
                "El tiempo para enviar el comprobante venció y el aparte fue liberado.\n\n"
                "Escribe *menu* si quieres volver a buscar una hora de Psiquiatría."
            )

        # ── Caso C: quiere abonar en recepción / no puede transferir ─────────
        _kw_recepcion = ("recepcion", "recepción", "presencial", "efectivo",
                         "no puedo transferir", "no tengo como", "no tengo cómo",
                         "no tengo transferencia", "no puedo", "sin transferencia")
        if any(k in tl_norm for k in _kw_recepcion):
            log_event(phone, "abono_gate_recepcion", {})
            save_session(phone, "HUMAN_TAKEOVER", {
                "hold_sent": True,
                "handoff_reason": "abono_gate_prefiere_recepcion",
                "abono_gate_slot": data.get("abono_gate_slot"),
            })
            # Aviso a recepción
            _slot_r = data.get("abono_gate_slot") or {}
            _pac_r  = data.get("abono_gate_paciente") or {}
            _nom_r  = _pac_r.get("nombre", "")
            if ADMIN_ALERT_PHONE:
                from resilience import spawn_task as _spawn_ag_r
                async def _aviso_recep_abono():
                    from config import CMC_TRANSFERENCIA as _ctf_r
                    _msg_r = (
                        f"📋 *Abono Psiquiatría — abonar en recepción*\n"
                        f"Paciente: {_nom_r}\n"
                        f"Fecha cita: {_slot_r.get('fecha_display', _slot_r.get('fecha', ''))}\n"
                        f"Hora: {(_slot_r.get('hora_inicio') or '')[:5]}\n"
                        f"WA: {phone}\n"
                        "El paciente prefiere abonar presencialmente. Coordinar en recepción."
                    )
                    await send_whatsapp(ADMIN_ALERT_PHONE, _msg_r)
                    from session import log_message as _lm_agr
                    _lm_agr(ADMIN_ALERT_PHONE, "out", _msg_r, "WAIT_ABONO_COMPROBANTE")
                _spawn_ag_r(_aviso_recep_abono())
            return (
                "Sin problema. Una recepcionista te va a contactar para coordinar el abono "
                "en el centro.\n\n"
                "Si preferes llamar directamente: 📞 (44) 296 5226\n\n"
                "_Tu hora queda pendiente de confirmar hasta que se reciba el abono._"
            )

        # ── Caso B: texto "ya transferí" / "ya envié" pero sin imagen ────────
        _kw_ya_transferi = ("ya transferi", "ya transferí", "ya pagué", "ya pagé",
                            "ya mande", "ya mandé", "ya envié", "ya envie",
                            "ya hice la transferencia", "ya realice", "ya realicé",
                            "hice la transferencia", "hice el pago")
        if any(k in tl_norm for k in _kw_ya_transferi):
            save_session(phone, "WAIT_ABONO_COMPROBANTE", data)
            return (
                "Gracias por transferir. Para confirmar tu hora necesito ver el comprobante.\n\n"
                "Envía una *foto* del comprobante de transferencia por este chat 📎"
            )

        # ── Caso D: texto libre genérico ──────────────────────────────────────
        # (El caso A —imagen— no llega aquí; main.py lo intercepta antes.)
        save_session(phone, "WAIT_ABONO_COMPROBANTE", data)
        from config import ABONO_PSIQUIATRIA_CLP as _ABO_D
        _monto_d = f"${_ABO_D:,}".replace(",", ".")
        return (
            f"Estoy esperando el comprobante de la transferencia de *{_monto_d} CLP* "
            "para confirmar tu hora de Psiquiatría.\n\n"
            "Envía una *foto* del comprobante por este chat 📎\n\n"
            "_Si no puedes hacer la transferencia, escribe *recepcion* y te ayudamos._"
        )

    # ── WAIT_CROSS_SELL ───────────────────────────────────────────────────────
    if state == "WAIT_CROSS_SELL":
        esp_origen  = data.get("cross_sell_esp_origen", "")
        esp_destino = data.get("cross_sell_esp_destino", "")
        # Botón "cs_si:<esp>" o texto afirmativo
        _cs_acepto = (
            tl.startswith("cs_si:")
            or tl in AFIRMACIONES
            or tl_norm in AFIRMACIONES
        )
        _cs_rechazo = (
            tl == "cs_no"
            or tl in NEGACIONES
            or tl_norm in NEGACIONES
        )
        if _cs_acepto:
            log_cross_sell(phone, esp_origen, esp_destino, "aceptado")
            log_event(phone, "cross_sell_aceptado", {
                "esp_origen": esp_origen, "esp_destino": esp_destino})
            reset_session(phone)
            return await _iniciar_agendar(phone, {}, esp_destino)
        elif _cs_rechazo:
            log_cross_sell(phone, esp_origen, esp_destino, "rechazado")
            log_event(phone, "cross_sell_rechazado", {
                "esp_origen": esp_origen, "esp_destino": esp_destino})
            reset_session(phone)
            return "_Escribe *menu* si necesitas algo más._"
        else:
            # Respuesta ambigua (ej: "¿cuánto cuesta?", "gracias", RUT, texto libre)
            # → reprompt una vez; al segundo intento fallido, escapar para evitar loop.
            _cs_intentos = data.get("cs_intentos", 0) + 1
            if _cs_intentos > 1:
                log_cross_sell(phone, esp_origen, esp_destino, "timeout")
                log_event(phone, "cross_sell_loop_escape", {
                    "esp_origen": esp_origen, "esp_destino": esp_destino,
                    "intentos": _cs_intentos,
                })
                reset_session(phone)
                return (
                    "_No te preocupes, puedes escribir *menu* cuando quieras retomar._"
                )
            data["cs_intentos"] = _cs_intentos
            save_session(phone, "WAIT_CROSS_SELL", data)
            return _btn_msg(
                f"¿Te gustaría agendar una hora de *{esp_destino}*?",
                [
                    {"id": f"cs_si:{esp_destino}", "title": "Sí, agendar"},
                    {"id": "cs_no", "title": "No, gracias"},
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
        # ── HUMAN_TAKEOVER SELECTIVO ──────────────────────────────────────
        # Principio: la consulta original (médica, fármaco, complaint) queda
        # pendiente para la recepcionista. Pero intents puramente operativos
        # (ver_reservas, agendar, cancelar, info, precio, horario, ubicación)
        # los puede resolver el bot SIN necesidad de que el humano haya
        # respondido. El flag de takeover se mantiene activo.
        #
        # Intents SEGUROS que el bot puede atender durante HUMAN_TAKEOVER:
        _SAFE_INTENTS_TAKEOVER = frozenset({
            "ver_reservas", "agendar", "cancelar", "reagendar",
            "precio", "info", "menu", "disponibilidad", "waitlist",
        })
        # Intents MÉDICOS que siempre quedan bloqueados:
        _MEDICAL_INTENTS_TAKEOVER = frozenset({
            "consulta_farmaco", "triage", "sintomas", "humano",
        })

        # Clasificar el intent del nuevo mensaje
        _takeover_reason = data.get("takeover_reason", "")
        msgs_sin_respuesta = data.get("msgs_sin_respuesta", 0) + 1
        data["msgs_sin_respuesta"] = msgs_sin_respuesta

        # Detectar keywords clínicas (síntomas, medicación, patologías)
        _CLINICAL_KWS = (
            "diabet", "hipert", "presion", "presión", "azucar", "azúcar",
            "colesterol", "tiroid", "asma", "epilep", "cancer", "cáncer",
            "embaraz", "operac", "cirug", "medicament", "pastilla",
            "remedio", "receta", "f\xe1rmaco", "farmaco", "dosis",
            "tratamient", "diagnost", "diagn\xf3stic",
        )
        _texto_es_clinico = (
            _SENALES_SINTOMA.search(tl)
            or any(kw in tl_norm for kw in _CLINICAL_KWS)
            or any(kw in tl for kw in _CLINICAL_KWS)
        )

        # Si el texto es claramente operativo (reset commands ya se procesaron arriba),
        # intentar clasificar con detect_intent para decidir si el bot puede responder.
        # Solo lo hacemos si el texto NO tiene señal clínica (evitar latencia innecesaria).
        _can_bot_respond = False
        _new_intent_result = None
        if not _texto_es_clinico:
            try:
                _new_intent_result = await detect_intent(txt)
                _new_intent = _new_intent_result.get("intent", "otro")
                if _new_intent in _SAFE_INTENTS_TAKEOVER:
                    _can_bot_respond = True
                    log_event(phone, "takeover_selectivo_bot_responde",
                              {"intent": _new_intent, "takeover_reason": _takeover_reason})
            except Exception as _e_ti:
                log.warning("takeover detect_intent falló: %s", _e_ti)

        # Bot puede responder: procesar normalmente pero mantener HUMAN_TAKEOVER
        if _can_bot_respond and _new_intent_result:
            _new_intent = _new_intent_result.get("intent", "otro")
            # FIX-5: Si la recepcionista ya respondió (human_replied=True), la
            # conversación está activa y el bot NO debe pisar con FAQ automáticas.
            # "¡De nada!", "Con gusto", precios y horarios mezclados con respuestas
            # reales de recepción confunden al paciente.
            # Solo responder FAQ si la recep aún NO ha dicho nada (primer ack pendiente).
            _recep_ya_respondio = data.get("human_replied", False)
            if _new_intent in ("precio", "info") and _new_intent_result.get("respuesta_directa"):
                _rd_ht = _new_intent_result["respuesta_directa"]
                # Bloquear respuestas de cortesía cortas (saludos de cierre, "de nada")
                # que no aportan valor y suenan extrañas mezcladas con recepcionista.
                _CORTESIA_PATTERN = re.compile(
                    r"^(¡?de\s+nada|con\s+gusto|gracias|un\s+placer|"
                    r"perfecto|claro|bienvenid|buenas?|hola)[!., ]*$",
                    re.IGNORECASE,
                )
                if _CORTESIA_PATTERN.match(_rd_ht.strip()):
                    # Silencio — no responder cortesías automáticas en takeover
                    save_session(phone, "HUMAN_TAKEOVER", data)
                    return ""
                if _recep_ya_respondio:
                    # Recepcionista activa: suprimir FAQ para no pisar la conversación
                    log_event(phone, "takeover_faq_suprimida_recep_activa",
                              {"intent": _new_intent, "rd_snippet": _rd_ht[:80]})
                    save_session(phone, "HUMAN_TAKEOVER", data)
                    return ""
                save_session(phone, "HUMAN_TAKEOVER", data)
                return _rd_ht
            # Para agendar/cancelar/ver_reservas: salir temporalmente de HUMAN_TAKEOVER,
            # procesar el intent, y restaurar el takeover al final de ese flujo no es
            # trivial. En su lugar, reseteamos la sesión a IDLE con un flag que indica
            # que la consulta médica original sigue pendiente para el humano.
            if _new_intent in ("ver_reservas", "agendar", "cancelar", "reagendar",
                               "disponibilidad", "waitlist"):
                # Guard anti-falso-positivo ver_reservas: el clasificador a veces
                # mapea propuestas de horario ("para el martes 2 tendría una hora
                # en la tarde") a ver_reservas por las palabras "hora"/"día"/"tarde".
                # Caso real 56950836674 (2026-05-25): bot estaba explicando bioimpe-
                # danciometría, paciente proponía slot y el bot reseteó a flow "ver
                # reservas" pidiendo RUT. Requerimos un disparador léxico explícito:
                # posesivo + sustantivo de cita, o pregunta directa por reserva.
                if _new_intent == "ver_reservas":
                    import re as _re_vr
                    _VER_RES_OK = _re_vr.compile(
                        r"\b(mis?|mi)\s+(hora|cita|reserva|control|hr)s?\b"
                        r"|\b(tengo|reserv[eé]|agend[eé]|tom[eé])\s+\w*\s*"
                        r"(hora|cita|reserva|control)\b"
                        r"|\b(cu[aá]ndo|qu[eé]\s+d[ií]a|a\s+qu[eé]\s+hora)\s+(es|tengo|me\s+toca)\b"
                        r"|\b(ver|saber|mostrar|consultar)\s+\w*\s*"
                        r"(hora|cita|reserva|agendamiento)\b",
                        _re_vr.IGNORECASE,
                    )
                    if not _VER_RES_OK.search(txt):
                        log_event(phone, "takeover_ver_reservas_falso_positivo",
                                  {"texto": txt[:240], "takeover_reason": _takeover_reason})
                        # No es ver_reservas real → mantener HUMAN_TAKEOVER sin desviar
                        save_session(phone, "HUMAN_TAKEOVER", data)
                        return None
                # Guardia: si la recepcionista respondió hace menos de 30 min,
                # NO resetear — podría haber una conversación activa y el bot
                # agendaría en paralelo (caso real 569785******: recep respondió
                # 11:54, bot agendó solo a las 11:56 tras siguiente mensaje).
                _human_replied = data.get("human_replied", False)
                _recep_reciente = False
                if _human_replied:
                    try:
                        from session import _conn as _s_conn_tr
                        _conn_tr = _s_conn_tr()
                        _upd_row = _conn_tr.execute(
                            "SELECT updated_at FROM sessions WHERE phone=?", (phone,)
                        ).fetchone()
                        _conn_tr.close()
                        if _upd_row:
                            _upd_raw = _upd_row[0]
                            _upd_dt = datetime.fromisoformat(_upd_raw)
                            if _upd_dt.tzinfo is None:
                                _upd_dt = _upd_dt.replace(tzinfo=timezone.utc)
                            _mins_ago = (datetime.now(timezone.utc) - _upd_dt).total_seconds() / 60
                            _recep_reciente = _mins_ago < 30
                    except Exception:
                        _recep_reciente = False
                if _recep_reciente:
                    # Recepcionista activa: registrar el intent pero no desviar
                    log_event(phone, "takeover_selectivo_bloqueado_recep_activa",
                              {"intent": _new_intent, "takeover_reason": _takeover_reason})
                    save_session(phone, "HUMAN_TAKEOVER", data)
                    return (
                        "Hay una recepcionista respondiendo tu consulta en este momento 😊 "
                        "En cuanto termine, puedes continuar con tu solicitud."
                    )
                # Guardamos nota de la consulta pendiente antes de resetear
                _pending_msg = data.get("handoff_reason", "")
                reset_session(phone)
                # Guardar en sesión nueva el flag de consulta_pendiente_humano
                _new_data = get_session(phone).get("data", {})
                _new_data["consulta_pendiente_humano"] = _pending_msg[:300] if _pending_msg else "consulta previa registrada"
                save_session(phone, "IDLE", _new_data)
                log_event(phone, "takeover_selectivo_reset_idle",
                          {"intent": _new_intent, "takeover_reason": _takeover_reason})
                # Procesar el intent en IDLE normalmente
                return await handle_message(phone, txt, {"state": "IDLE", "data": _new_data})

        # Texto clínico dentro de HUMAN_TAKEOVER — ack específico
        if _texto_es_clinico:
            log_event(phone, "human_takeover_clinico", {"texto": txt[:240]})
            save_session(phone, "HUMAN_TAKEOVER", data)
            return (
                "Gracias por contarnos 🙏 Ya registré tu mensaje para que una "
                "recepcionista te responda en este chat.\n\n"
                f"*Si es urgente o empeora, llama ahora:*\n📞 *{CMC_TELEFONO}*\n"
                "🚑 *SAMU*: 131"
            )

        # ── FIX C: Slot preservado durante takeover ───────────────────────────
        # Si la recepcionista intervino mientras había una hora concreta ofrecida
        # (WAIT_SLOT / CONFIRMING_CITA / WAIT_META_SLOT_CHOICE), el takeover
        # preserva data["slot_elegido"] / data["slots"] en la sesión. Si el
        # paciente aprieta el BOTÓN de confirmación, completamos la reserva sin
        # perder el slot.
        #
        # Condiciones de seguridad:
        #   1. Solo dispara con payloads de botón explícitos ("quick_yes" /
        #      "confirmar_sugerido") — NUNCA con texto libre para no meter al
        #      bot en una conversación activa de recepcionista.
        #   2. Suprimido si la recepcionista estuvo activa en los últimos 30 min
        #      (_recep_reciente ya calculado arriba): que recepción maneje.
        #   3. Verifica disponibilidad real del slot antes de crear la cita; si
        #      ya fue tomado por otro paciente, busca nueva disponibilidad.
        #   4. Solo actúa con slot concreto + rut ya conocido.
        _TAKEOVER_CONFIRM_PAYLOADS = frozenset({"quick_yes", "confirmar_sugerido"})
        _ht_slot = data.get("slot_elegido") or (
            (data.get("slots") or [None])[0] if data.get("slots") else None
        )
        _ht_rut = data.get("rut_conocido") or data.get("rut")
        if (
            _ht_slot and _ht_rut
            and not _texto_es_clinico
            and not _recep_reciente          # guard: recepcionista activa → no actuar
            and tl in _TAKEOVER_CONFIRM_PAYLOADS  # solo botón, nunca texto libre
        ):
            # Verificar disponibilidad real antes de crear la cita —
            # el slot puede llevar minutos u horas esperando en sesión.
            _ht_disponible = False
            try:
                _ht_disponible = await verificar_slot_disponible(
                    _ht_slot.get("id_profesional") or data.get("id_profesional"),
                    _ht_slot.get("fecha", ""),
                    _ht_slot.get("hora_inicio", ""),
                    _ht_slot.get("hora_fin", ""),
                )
            except Exception:
                _ht_disponible = False
            if not _ht_disponible:
                # Slot ya tomado: avisar y redirigir a nueva búsqueda
                log_event(phone, "takeover_slot_preservado_expirado", {
                    "slot": _ht_slot.get("hora_inicio"),
                    "fecha": _ht_slot.get("fecha"),
                    "rut": _ht_rut,
                })
                _esp_ht = _ht_slot.get("especialidad") or data.get("especialidad", "")
                reset_session(phone)
                return await _iniciar_agendar(
                    phone, {}, _esp_ht or None,
                    saludo_prefix=(
                        "Esa hora ya no está disponible (fue reservada por otro "
                        "paciente mientras esperabas). Te busco la siguiente:\n\n"
                    ),
                )
            # Slot disponible → reservar
            log_event(phone, "takeover_slot_preservado_confirm", {
                "slot": _ht_slot.get("hora_inicio"), "rut": _ht_rut,
            })
            # Restaurar estado mínimo para _slot_confirmed
            data["slots"] = [_ht_slot]
            data["todos_slots"] = [_ht_slot]
            data["slot_elegido"] = _ht_slot
            return await _slot_confirmed(phone, data, _ht_slot)
        # ── fin FIX C ─────────────────────────────────────────────────────────

        save_session(phone, "HUMAN_TAKEOVER", data)

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
        # "Seguimos atentos" ni "Recibido 🙏" repetidos — la recepcionista ya
        # está respondiendo desde el panel y el ruido confunde. Cada 15
        # mensajes sin respuesta humana mandamos un recordatorio suave.
        if msgs_sin_respuesta > 0 and msgs_sin_respuesta % 15 == 0:
            return f"Seguimos aquí 🙌 Si es urgente, llama al 📞 *{CMC_TELEFONO}*"
        return ""

    # Fallback
    reset_session(phone)
    _pf_end = get_profile(phone)
    _nm_end = _first_name((_pf_end or {}).get("nombre", "")) if _pf_end else ""
    return _menu_msg(nombre=_nm_end)


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
    "esp_neuro":   "neurología",
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
                {"id": "esp_neuro",   "title": "Neurología"},
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
_ESP_MED_GENERAL = {"medicina general"}  # Abarca, Olavarría, Márquez
_ESP_MED_FAMILIAR = {"medicina familiar", "médico familiar", "medico familiar"}
_MED_FAMILIAR_IDS = [13]  # Solo Dr. Márquez atiende Medicina Familiar

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
    # Hard-block: frases de acción (cancelar, anular) sin mención explícita de
    # "doctor/dr./dra." no deben producir falsos positivos de apellido.
    # "cancelar mi hora" colapsa a "cancelarmihora" → contiene "armih" (Armijo).
    _txt_low = txt.lower()
    _ACCION_CANCEL = ("cancelar", "anular", "reagendar")
    _MENCIONA_PROF_EXPL = any(p in _txt_low for p in ("doctor", "dr.", "dra.", " dr ", " dra "))
    if any(v in _txt_low for v in _ACCION_CANCEL) and not _MENCIONA_PROF_EXPL:
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
    ("bioimpedanc",           "nutrición"),   # examen de composición corporal → Nutrición (Gisela Pinto)
    ("composicion corporal",  "nutrición"),
    ("composición corporal",  "nutrición"),
    ("podolog",               "podología"),
    ("posolog",               "podología"),     # typo posología → podología
    ("posologia",             "podología"),
    ("posología",             "podología"),
    # Servicios dentales: deben preceder a "ecograf" para evitar colisiones.
    # "limpieza dental" sin este entry podía caer a ecografía en algunos paths.
    ("limpieza dental",       "odontología"),
    ("limpieza de dientes",   "odontología"),
    ("limpieza de diente",    "odontología"),
    ("limpieza bucal",        "odontología"),
    ("limpieza de boca",      "odontología"),
    ("destartraje",           "odontología"),
    ("detartraje",            "odontología"),   # typo frecuente
    ("profilaxis dental",     "odontología"),
    ("sarro",                 "odontología"),
    ("blanqueamiento dental", "odontología"),
    ("blanqueamiento",        "odontología"),
    ("blanqueo dental",       "odontología"),
    ("tapadura",              "odontología"),
    ("resina dental",         "odontología"),
    # Ecografías: solo el prefijo genérico. El routing por órgano lo maneja
    # route_ecografia() de ecografias.py — ver _iniciar_agendar y detect_intent.
    # Los keywords cardíacos y ginecológicos se removieron de aquí para que
    # no dupliquen la lógica centralizada.
    ("ecograf",               "ecografía"),
    ("ecotomograf",           "ecografía"),
    ("ecotomo",               "ecografía"),
    ("ultrasonido",           "ecografía"),
    ("estetica",              "estética facial"),
    ("estética",              "estética facial"),
    ("botox",                 "estética facial"),
    ("traumato",              "traumatología"),
    # Aliases añadidos 2026-05-10 (auditoría 72h): variantes naturales no cubiertas
    ("médico infantil",       "medicina general"),   # CMC no tiene pediatra; MG atiende niños
    ("medico infantil",       "medicina general"),
    ("doctor infantil",       "medicina general"),
    ("doctora infantil",      "medicina general"),
    ("pediatra",              "medicina general"),   # derivar a MG + aclaración
    ("pediatría",             "medicina general"),
    ("pediátrico",            "medicina general"),
    ("pediatrico",            "medicina general"),
    ("doctor general",        "medicina general"),
    ("doctora general",       "medicina general"),
    ("necesito médico",       "medicina general"),
    ("necesito medico",       "medicina general"),
    ("quiero médico",         "medicina general"),
    ("quiero medico",         "medicina general"),
    ("quiero doctor",         "medicina general"),
    ("necesito doctor",       "medicina general"),
    ("necesito una hora",     "medicina general"),   # sin especialidad → MG por defecto
    ("pedir hora médico",     "medicina general"),
    ("pedir hora medico",     "medicina general"),
    # Bug 4 fix: salud mental, fuzzy ortodoncia, ansiedad/depresion
    ("salud mental",          "psicología"),
    ("salud emocional",       "psicología"),
    ("bienestar mental",      "psicología"),
    ("ansiedad",              "psicología"),
    ("depresion",             "psicología"),
    ("depresión",             "psicología"),
    ("ortodancia",            "ortodoncia"),
    ("ortodonsia",            "ortodoncia"),
    ("ortodencias",           "ortodoncia"),
]


def _detectar_especialidad_en_texto(txt: str) -> str | None:
    """Detecta una especialidad mencionada en el texto crudo. Usado como
    fallback cuando Claude no extrae especialidad correctamente.

    Primero intenta match exacto. Si falla, normaliza typos fonéticos comunes
    (j→g, y→ll, sh→ch, sin tildes) y reintenta."""
    if not txt:
        return None
    tl = txt.lower().strip()
    # BUG-04: "eco" solo (o "eco" como palabra) → ecografía.
    # "eco" es 3 chars: match substring daría falsos positivos ("económico", "ecología").
    # Usar word-boundary para palabras ≤4 caracteres que son ambiguas.
    import re as _re
    # _FRASES_ESPECIALIDAD tiene prioridad sobre _SHORT_EXACT para "eco" en contexto
    # cardíaco ("eco corazón", "eco cardiograma", etc.) — recorremos primero la lista.
    for frase, key in _FRASES_ESPECIALIDAD:
        if frase in tl:
            return key
    # BUG-04: "eco" solo (o "eco" como palabra) → ecografía (word-boundary).
    _SHORT_EXACT = {
        "eco": "ecografía",
        "orl": "otorrinolaringología",
        "kine": "kinesiología",
        "fono": "fonoaudiología",
    }
    for _word, _esp in _SHORT_EXACT.items():
        if _re.search(r'\b' + _re.escape(_word) + r'\b', tl):
            return _esp
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


# ── Catálogo de especialidades que ve el paciente ────────────────────────────
# DERIVADO de PROFESIONALES (medilink.py), no hardcodeado. Antes era una lista
# manual y se desincronizó tres veces seguidas: Psiquiatría (78), Masoterapia
# (59) y Oftalmología (80) quedaron invisibles para el paciente que pedía hora
# sin nombrar especialidad. Al derivarla, toda alta futura aparece sola.
#
# _ESP_ORDEN fija el orden de presentación (médicas → dental → terapias).
# Lo que NO esté en _ESP_ORDEN se agrega al final, así una especialidad nueva
# nunca desaparece por olvido: a lo más queda mal ordenada.
_ESP_OCULTAS: set[str] = {
    "Traumatología",  # Dr. Barraza no disponible → se redirige a Medicina General
}

_ESP_ORDEN: list[str] = [
    "Medicina General", "Medicina Familiar", "Otorrinolaringología", "Cardiología",
    "Ginecología", "Gastroenterología", "Neurología", "Psiquiatría",
    "Tecnología Médica Oftalmológica",
    "Odontología General", "Ortodoncia", "Endodoncia", "Implantología",
    "Estética Facial",
    "Kinesiología", "Masoterapia", "Nutrición", "Psicología", "Fonoaudiología",
    "Matrona", "Podología", "Ecografía",
]

# Prestaciones agendables que NO son la especialidad propia de un profesional
# en PROFESIONALES, pero que el bot sí agenda (cuelgan de la agenda de alguien).
# Sin esto la lista derivada las perdería:
#   · Medicina Familiar → Dr. Márquez (13) figura como "Medicina General".
#   · Bioimpedanciometría → la realiza la nutricionista (Gisela Pinto, 52).
_PRESTACIONES_EXTRA: list[tuple[str, str]] = [
    ("Medicina Familiar", "Medicina General"),   # (etiqueta, se muestra después de…)
    ("Bioimpedanciometría", "Nutrición"),
]

# Etiquetas más legibles para el paciente que el nombre interno de la
# especialidad. Clave = PROFESIONALES[id]["especialidad"].
_ESP_ETIQUETA: dict[str, str] = {
    # NO llamarla "Oftalmología": Ana Celedón es TECNÓLOGA MÉDICA, no oftalmóloga,
    # y el CMC no tiene oftalmólogo médico. Anunciarla como oftalmología sería
    # publicidad engañosa. Las keywords del paciente ("oftalmología") sí rutean
    # acá — lo que no puede pasar es que el bot se autodenomine así.
    "Tecnología Médica Oftalmológica": "Tecnología Médica Oftalmológica (examen de la vista y lentes)",
    "Psicología Adulto": "Psicología",
    "Psicología Infantil": "Psicología",
}


def _build_especialidades_texto() -> str:
    """Lista de especialidades para el paciente, derivada de PROFESIONALES."""
    from medilink import PROFESIONALES as _PROFS_CAT

    vivas: list[str] = []
    for _info in _PROFS_CAT.values():
        esp = _info.get("especialidad", "").strip()
        if not esp or esp in _ESP_OCULTAS:
            continue
        etiqueta = _ESP_ETIQUETA.get(esp, esp)
        if etiqueta not in vivas:
            vivas.append(etiqueta)

    # Ordenar según _ESP_ORDEN; lo desconocido va al final (visible igual).
    def _rank(etiqueta: str) -> int:
        for i, esp in enumerate(_ESP_ORDEN):
            if _ESP_ETIQUETA.get(esp, esp) == etiqueta:
                return i
        return len(_ESP_ORDEN)

    vivas.sort(key=_rank)

    # Insertar prestaciones extra justo después de su especialidad ancla.
    for etiqueta, ancla in _PRESTACIONES_EXTRA:
        ancla_lbl = _ESP_ETIQUETA.get(ancla, ancla)
        if etiqueta in vivas:
            continue
        if ancla_lbl in vivas:
            vivas.insert(vivas.index(ancla_lbl) + 1, etiqueta)
        else:
            vivas.append(etiqueta)

    return "\n".join(f"• {e}" for e in vivas)


_ESPECIALIDADES_TEXTO = _build_especialidades_texto()


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


def _modo_degradado(phone: str, intent: str, state_snap: str = "",
                    especialidad: str = "") -> str:
    """Respuesta cuando Medilink está caído. Encola la intención y avisa al paciente.
    Devuelve un mensaje graceful que el bot enviará por WhatsApp.

    Fix K: cuando intent=agendar y hay especialidad, ofrece lista de espera para
    que el paciente no se vaya sin acción — cuando Medilink vuelva, el job de
    waitlist lo procesa automáticamente.

    Si ya se avisó en los últimos 15 min, pasa a HUMAN_TAKEOVER en vez de
    repetir el mismo mensaje (el paciente ya sabe que hay problema técnico).
    """
    import time as _time_deg
    enqueue_intent(phone, intent, state_snap)
    log_event(phone, "modo_degradado", {"intent": intent, "especialidad": especialidad})

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

    # Fix K: si intent=agendar y hay especialidad, ofrecer lista de espera
    if intent == "agendar" and especialidad:
        _esp_display = especialidad.capitalize()
        _data_wl = {"waitlist_especialidad": especialidad, "waitlist_source": "medilink_down"}
        save_session(phone, "WAIT_WAITLIST_CONFIRM", _data_wl)
        log_event(phone, "modo_degradado_waitlist_ofrecida", {"especialidad": especialidad})
        return _btn_msg(
            f"Nuestro sistema de citas tiene un problema técnico en este momento 😕\n\n"
            f"¿Quieres que te avise cuando se libere una hora de *{_esp_display}*? "
            f"Te contactamos en cuanto el sistema vuelva.\n\n"
            f"También puedes llamarnos:\n"
            f"📞 *{CMC_TELEFONO}* · ☎️ *{CMC_TELEFONO_FIJO}*",
            [
                {"id": "waitlist_si", "title": "Sí, avísame"},
                {"id": "waitlist_no", "title": "No, gracias"},
            ]
        )

    # FIX 3: si especialidad está vacía, preguntar antes de inscribir en waitlist;
    # si hay especialidad pero intent != agendar, igualmente ofrecer waitlist.
    if intent == "agendar" and not especialidad:
        # No sabemos aún qué especialidad necesita → preguntar para poder inscribirlo
        _data_ask = {"_modo_degradado_esp_pending": True, "intent_encolado": intent}
        save_session(phone, "WAIT_ESPECIALIDAD", _data_ask)
        return (
            "Nuestro sistema de citas tiene un problema técnico en este momento 😕\n\n"
            "¿Qué especialidad necesitas? "
            "Cuando vuelva el sistema te agendo o te anoto en lista de espera.\n\n"
            "_Ejemplo: Medicina General, Kinesiología, Ecografía…_"
        )

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


_RE_MENOR_KEYWORDS = re.compile(
    r"\b(bebe|bebé|bebita|guagua|guagüita|niño|niña|nino|nina|hijo|hija|"
    r"infante|chico|chica|pequeño|pequeña|menor|adolescente|"
    r"([0-9]+)\s*(años?|meses?))\b",
    re.IGNORECASE,
)
_RE_MENOR_EDAD = re.compile(r"\b([0-9]{1,2})\s*años?\b", re.IGNORECASE)

def _detectar_menor_en_texto(txt: str) -> bool:
    """Retorna True si el texto menciona a un menor (<18 años o keyword de niño/bebé).

    BUG-3 FIX: antes cortaba en < 14, dejando pasar 14-17 (legalmente menores).
    """
    if not txt:
        return False
    txt_l = txt.lower()
    # Keywords directas de menor sin edad explícita
    _MENOR_KW = {"bebe", "bebé", "bebita", "guagua", "guagüita", "guagüa",
                 "niño", "niña", "nino", "nina", "infante",
                 # "hijo/hija" sin edad explícita se asume potencialmente menor
                 # cuando el contexto es "médico infantil" / "pediatra"
                 "mi hijo", "mi hija", "para mi hijo", "para mi hija",
                 "para el niño", "para la niña",
                 "infantil"}
    if any(k in txt_l for k in _MENOR_KW):
        return True
    # Edad explícita < 18 (menores de edad legales)
    for m in _RE_MENOR_EDAD.finditer(txt_l):
        if int(m.group(1)) < 18:
            return True
    return False


def _es_adolescente_en_texto(txt: str) -> bool:
    """Retorna True si hay edad 14-17 en el texto.

    Distingue: < 14 → derivación fuerte, 14-17 → advertencia con consentimiento tutor.
    """
    if not txt:
        return False
    txt_l = txt.lower()
    for m in _RE_MENOR_EDAD.finditer(txt_l):
        edad = int(m.group(1))
        if 14 <= edad <= 17:
            return True
    return False


async def _paciente_ortodoncia_activo(phone: str) -> int:
    """Cuenta atenciones (6 meses) del paciente con la Dra. Castillo (id 66).

    Señal de que el paciente YA está en tratamiento de ortodoncia (grupos
    2/3/4: instalación parcial, instalación completa, en control) — a
    diferencia del grupo 1 (nunca evaluado, quiere brackets pero primero
    necesita evaluación con odontología general).

    Identifica al paciente por phone → rut (contact_profiles/get_profile) →
    consulta BI real. Única fuente para esta pregunta: reutilizada por el
    gate WAIT_ORTODONCIA_ACTIVO (Patrón 4) y por _iniciar_agendar (ruteo
    dental/ortodoncia). Retorna 0 si no hay perfil, RUT, o falla la consulta.
    """
    perfil = get_profile(phone)
    if not perfil or not perfil.get("rut"):
        return 0
    try:
        # Query psycopg2 síncrona: usar asyncio.to_thread para no bloquear
        # el event loop (F137 auditoría 2026-06-10).
        from winback import bi_conn as _bi_orto_conn

        def _query_orto_sync():
            with _bi_orto_conn() as _pg:
                with _pg.cursor() as _cur:
                    _cur.execute(
                        "SELECT COUNT(*) FROM bi.fact_atenciones "
                        "WHERE rut = %s "
                        "  AND id_profesional = 66 "
                        "  AND fecha_atencion >= NOW() - INTERVAL '6 months'",
                        (perfil["rut"],),
                    )
                    return (_cur.fetchone() or [0])[0]

        return await asyncio.to_thread(_query_orto_sync)
    except Exception as e:
        log.warning("_paciente_ortodoncia_activo phone=%s: %s", phone, e)
        return 0


async def _iniciar_agendar(phone: str, data: dict, especialidad: str | None,
                            saludo_prefix: str | None = None) -> str:
    if is_medilink_down():
        # Fail-open VERIFICADO (2026-07-27). El flag puede estar viejo: los 429
        # del cron de pagos lo dejaban en "down" mientras /citas y /agendas
        # respondían 200, y el paciente se iba a lista de espera con la agenda
        # llena (caso Jessica 08:31: waitlist de MG; 40 min después el mismo bot
        # le reservó las 10:30 del MISMO día con Abarca). Antes de cortar, se le
        # pregunta a Medilink: 1 request (~200 ms) contra regalar una hora real.
        from medilink import probe_up as _probe_medilink
        if await _probe_medilink():
            log_event(phone, "breaker_falso_positivo",
                      {"especialidad": (especialidad or "").strip().lower()})
        else:
            return _modo_degradado(phone, "agendar", state_snap=especialidad or "",
                                   especialidad=especialidad or "")
    # ── Detección de menor: aplica a especialidades adultas, NO a MG/MF/Odonto/Fono/Psico ──
    # MG (Abarca, Olavarría, Márquez), MF (Márquez), Odontología, Fonoaudiología y
    # Psicología Infantil (Montalba) atienden niños y adultos por igual — no interrumpir.
    # Para el resto de especialidades adultas (ORL, cardio, gineco, gastro, trauma, etc.)
    # se muestra aviso con confirmación del usuario.
    _ESP_ATIENDE_MENORES = {
        "medicina general", "medicina familiar",
        "odontología general", "odontologia general", "odontología", "odontologia",
        "ortodoncia", "endodoncia", "implantología", "implantologia",
        "fonoaudiología", "fonoaudiologia",
        "psicología infantil", "psicologia infantil",
        "nutrición", "nutricion",
        "kinesiología", "kinesiologia",
        "podología", "podologia",
    }
    _txt_raw = data.pop("_txt_raw", "") or ""
    # Texto del órgano de eco capturado cuando el bot ofreció "Sí, agendar" tras
    # explicar un tipo de eco. Se consume aquí (pop) para que el click del botón
    # recupere "abdominal"/"renal"/... y route_ecografia NO vuelva a preguntar el
    # tipo (menu-loop dominante de ecografía, auditoría 2026-06-07).
    _eco_tipo_sugerido = data.pop("eco_tipo_text", "") or ""
    # ── Detección SISTÉMICA de tercero (fix 2026-05-29) ──────────────────────
    # Antes _OTRA_PERSONA_RE solo se evaluaba en WAIT_MODALIDAD, así que
    # "quiero agendar para mi hijo" desde el primer mensaje (o por audio
    # transcrito) NO marcaba el flag y el paciente tenía que repetir 2-3 veces
    # "es para mi hijo / no, para mí". _iniciar_agendar es el chokepoint único
    # de TODAS las rutas de agendamiento y ya lee _txt_raw para el aviso de
    # menores → marcamos aquí el flag para que cubra texto y audio por igual.
    # Casos reales 2026-05: 56927011946 (3 intentos), 56971590564, 56951169548.
    if (
        _txt_raw
        and not data.get("booking_for_other")
        and _OTRA_PERSONA_RE.search(_txt_raw.lower())
    ):
        data["booking_for_other"] = True
        log_event(phone, "tercero_detectado_iniciar", {"txt": _txt_raw[:120]})
    _esp_lower_menor = (especialidad or "").lower().strip()
    _saltar_aviso_menor = (
        not _txt_raw
        or data.get("_menor_confirmado_adulto")
        or _esp_lower_menor in _ESP_ATIENDE_MENORES
    )
    if not _saltar_aviso_menor and _detectar_menor_en_texto(_txt_raw):
        _es_adol = _es_adolescente_en_texto(_txt_raw)
        _esp_display = (especialidad or "la especialidad solicitada").capitalize()
        log_event(phone, "menor_detectado_esp", {
            "txt": _txt_raw[:120], "esp": especialidad, "adolescente": _es_adol
        })
        data["_especialidad_pendiente"] = especialidad
        if _es_adol:
            _msg_menor = (
                "Veo que la cita podría ser para un adolescente.\n\n"
                f"*{_esp_display}* generalmente atiende adultos. "
                "Si el paciente tiene entre 14 y 17 años, un tutor o apoderado "
                "debe confirmar la cita.\n\n"
                "¿Quieres continuar?"
            )
        else:
            _msg_menor = (
                "Veo que la cita es para un menor de edad.\n\n"
                f"*{_esp_display}* atiende principalmente adultos. "
                "Para niños, lo ideal es Medicina General o una especialidad pediátrica.\n\n"
                "Si igual quieres continuar con esta especialidad, presiona *Continuar*."
            )
        save_session(phone, "WAIT_CONFIRMAR_ADULTO", data)
        return _btn_msg(
            _msg_menor,
            [
                {"id": "menor_es_adulto", "title": "Continuar"},
                {"id": "menor_es_menor",  "title": "Prefiero otra opción"},
            ]
        )
    # ── BUG-A: loggear entrada a _iniciar_agendar para diagnóstico ──
    log_event(phone, "medfam_filtra_marquez_call",
              {"especialidad": especialidad, "lower": (especialidad or "").lower()})
    # ── Solo Dr. Márquez atiende Medicina Familiar ──
    # El guard se ejecuta ANTES del check de especialidad=None para que el
    # path WAIT_ESPECIALIDAD → _iniciar_agendar("Medicina Familiar") dispare
    # el evento sin importar capitalización.
    if especialidad and especialidad.lower() in _ESP_MED_FAMILIAR:
        data["esp_pedida_original"] = "Medicina Familiar"
        data["force_prof_ids"] = [13]
        data["medfam_solo_marquez"] = True
        log_event(phone, "medfam_filtra_marquez",
                  {"especialidad_raw": especialidad, "phone": phone})
    if not especialidad:
        save_session(phone, "WAIT_ESPECIALIDAD", data)
        return f"Claro, te ayudo a agendar 😊\n\n¿Qué especialidad necesitas?\n\n{_ESPECIALIDADES_TEXTO}"
    especialidad_lower = especialidad.lower().strip()
    # BUG-02: capturar especialidad pedida antes de normalizar
    _esp_pedida_original = especialidad_lower
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
        # BUG-4: presentar oferta explícita con botón de lista de espera antes
        # de redirigir a MG. Solo si no viene ya confirmado.
        if not data.pop("_traumato_redirect_confirmed", False):
            data["_traumato_redirect_confirmed"] = True
            data["_waitlist_trauma_pending"] = True
            save_session(phone, "IDLE", data)
            log_event(phone, "traumato_redirect_ofrecido", {"phone": phone})
            return _btn_msg(
                "🏥 *Medicina General* — Dr. Rodrigo Olavarría\n"
                "_Nuestro traumatólogo no está disponible. Medicina General puede "
                "evaluar inicialmente y derivarte si es necesario._\n\n"
                "¿Continúas con Medicina General o prefieres esperar al traumatólogo?",
                [
                    {"id": "trauma_mg",       "title": "✅ Continuar con MG"},
                    {"id": "trauma_waitlist", "title": "⏳ Esperar al traumatólogo"},
                ]
            )
        if not saludo_prefix:
            saludo_prefix = (
                "🏥 *Medicina General* — Dr. Rodrigo Olavarría\n"
                "_Nuestro traumatólogo no está disponible. Medicina General puede "
                "evaluar inicialmente y derivarte si es necesario._\n\n"
            )
        especialidad = "medicina general"
        especialidad_lower = "medicina general"
    # ── Ecografías: routing centralizado vía ecografias.route_ecografia() ──────
    # Razón: en CMC distintos tipos de ecografía los realiza distinto especialista.
    # Tabla autoritativa en app/ecografias.py — NO agregar alias aquí.
    #
    # Caso A — especialidad_lower es un tipo de ecografía específico (ecocardiograma,
    #           ginecología viene de transvaginal, etc.): route_ecografia ya resolvió
    #           el tipo antes de llegar aquí (via detect_intent o _detectar_especialidad_en_texto).
    # Caso B — especialidad_lower == "ecografía" y el texto original del paciente tiene
    #           un keyword de órgano: route_ecografia lo re-rutea al correcto.
    # Caso C — especialidad_lower == "ecografía" sin órgano: preguntar tipo.
    if especialidad_lower in ("ecografía", "ecografia", "eco", "ecotomografía", "ecotomografia", "ecotomo"):
        _txt_para_eco = _eco_tipo_sugerido or data.get("_txt_raw") or _txt_raw or especialidad
        try:
            from ecografias import route_ecografia as _route_eco, MSG_PREGUNTAR_TIPO as _MSG_ECO
            # assume_context=True: ya decidimos especialidad=ecografía, este texto
            # es el órgano/tipo ("abdominal","renal","hombro"...). El gate de
            # contexto eco no debe bloquearlo. Ver ecografias.route_ecografia.
            _eco_r = _route_eco(_txt_para_eco, assume_context=True)
        except Exception:
            _eco_r = None
            _MSG_ECO = (
                "¿De qué tipo es la ecografía? Por ejemplo:\n\n"
                "• Abdominal / renal / tiroides → David Pardo, $40.000\n"
                "• Transvaginal / pélvica / ginecológica → Ginecología (Dr. Rejón), $35.000\n"
                "• Mamaria → Ecografía (David Pardo), $40.000 — es partes blandas, no ginecológica\n"
                "• Ecocardiograma (corazón) → Cardiología (Dr. Millán), $110.000\n\n"
                "Escribe el tipo que necesitas."
            )
        if _eco_r is not None:
            # Eco obstétrica → el CMC no la realiza
            if _eco_r.get("flujo") == "no_disponible":
                log_event(phone, "eco_obstetrica_no_disponible", {"txt": _txt_para_eco[:120]})
                reset_session(phone)
                return _eco_r["mensaje"].format(tipo=_txt_para_eco)
            # Hay tipo reconocido — re-invocar con la especialidad destino correcta
            especialidad = _eco_r["especialidad_destino"]
            especialidad_lower = especialidad.lower()
        else:
            # Sin tipo especificado → preguntar
            # Fix I: guardar contexto para que WAIT_ESPECIALIDAD sepa que el
            # próximo mensaje es el tipo de ecografía y lo pase a route_ecografia
            # en vez del normalizador de especialidad general.
            log_event(phone, "ecografia_sin_tipo", {"txt": _txt_para_eco[:120]})
            data["wait_eco_tipo"] = True
            save_session(phone, "WAIT_ESPECIALIDAD", data)
            return _MSG_ECO

    # ── Ecocardiograma: flujo especial — Dr. Millán, no Pardo, no Medilink ────
    # Interceptar ANTES del guard _ids_esp_check (ecocardiograma no está en ESPECIALIDADES_MAP
    # intencionalmente para no dejar que Medilink lo asigne a Pardo ID 68).
    if especialidad_lower == "ecocardiograma":
        log_event(phone, "ecocardiograma_handler", {"phone": phone})
        data["waitlist_especialidad"] = "ecocardiograma"
        data["waitlist_id_prof_pref"] = 60
        save_session(phone, "WAIT_WAITLIST_CONFIRM_ECOCA", data)
        return _btn_msg(
            "El *ecocardiograma* lo realiza el Dr. Miguel Millán (cardiólogo).\n\n"
            "Valor: *$110.000* (solo particular, no aplica Fonasa)\n"
            "Disponibilidad: se realiza una vez al mes, aún no tenemos la próxima fecha confirmada.\n\n"
            "¿Quieres anotarte en la lista de espera? Te avisamos apenas confirmemos la fecha.",
            [
                {"id": "ecoca_waitlist_si", "title": "Sí, lista de espera"},
                {"id": "ecoca_waitlist_no", "title": "No, gracias"},
                {"id": "ecoca_menu",        "title": "Volver al menú"},
            ]
        )

    # Medicina Familiar: NO convertir a medicina general — tiene su propio branch de búsqueda
    # (antes este bloque ponía un saludo_prefix engañoso asumiendo que se mapeaba a MG)
    # Detectar si la especialidad no existe en nuestro catálogo
    from medilink import _ids_para_especialidad as _ids_esp_check
    if not _ids_esp_check(especialidad_lower):
        # Sanity check: si la "especialidad" no parece serlo (solo signos, saludos,
        # agradecimientos, muy corta) NO decir "no contamos con *X*" — mostrar el
        # menú. Esto evita responses absurdas como "no contamos con *?*" o
        # "no contamos con *muchas gracias*".
        # Validación positiva 2026-05-13: el texto solo pasa al branch "no contamos con X"
        # si es reconocible como nombre de especialidad médica. Si no matchea ninguna
        # clave conocida, volvemos a preguntar — evita mensajes absurdos como
        # "no contamos con *Pero para ponérmelos*" o "no contamos con *Hay mañana le.hablo*".
        #
        # Criterio positivo (cualquiera de los dos basta):
        #   1. _detectar_especialidad_en_texto matchea alguna frase en _FRASES_ESPECIALIDAD
        #   2. El texto normalizado coincide con alguna clave de ESPECIALIDADES_MAP
        #
        # Casos que SÍ deben pasar: "Pediatra", "Traumatología", "reumatología"
        # (especialidades plausibles aunque el CMC no las tenga).
        # Casos que NO deben pasar: "Pero para ponérmelos", "Hay mañana le.hablo",
        # "hola", frases libres del usuario.
        from medilink import ESPECIALIDADES_MAP as _ESPEC_MAP
        import unicodedata as _ud
        def _norm_esp(t: str) -> str:
            """Minúscula sin acentos (preserva ñ)."""
            t = t.lower()
            return "".join(
                c if c == "ñ" else _ud.normalize("NFD", c)[0]
                for c in t
            )
        _esp_norm = _norm_esp(especialidad_lower)
        # Check 1: alias en _FRASES_ESPECIALIDAD (via función ya existente)
        _matchea_alias = _detectar_especialidad_en_texto(especialidad_lower) is not None
        # Check 2: coincide con alguna clave del catálogo Medilink (normalizada)
        _matchea_catalogo = any(
            _esp_norm in _norm_esp(k) or _norm_esp(k) in _esp_norm
            for k in _ESPEC_MAP
            if len(k) >= 4  # descartar claves muy cortas como "orl", "eco", "kine"
        )
        # Check 3: morfología de especialidad médica — sufijos clínicos estándar.
        # Captura especialidades plausibles no registradas en el CMC
        # (ej: "reumatología", "neurología", "dermatología", "pediatría").
        _SUFIJOS_MED = (
            "logia", "logía", "atria", "atría", "iatria", "iatría",
            "ologo", "ólogo", "ologa", "óloga",
            "cista", "ista",  # endocrinista, oncologista
            "urgia", "urgía",  # neurocirugía
            "terapia",         # hidroterapia, quimioterapia
        )
        _esp_sin_tildes = _norm_esp(especialidad_lower)
        _matchea_sufijo = (
            " " not in especialidad_lower.strip()   # palabra única
            and len(especialidad_lower.strip()) >= 6  # mínimo razonable
            and any(_esp_sin_tildes.endswith(s) or _esp_sin_tildes.endswith(s + "s") for s in _SUFIJOS_MED)
        )
        _es_especialidad_reconocida = _matchea_alias or _matchea_catalogo or _matchea_sufijo
        if not _es_especialidad_reconocida:
            log_event(phone, "especialidad_texto_libre_rechazado", {
                "texto_usuario": especialidad,
                "estado_previo": "WAIT_ESPECIALIDAD",
            })
            save_session(phone, "WAIT_ESPECIALIDAD", data)
            return (
                "¿Qué especialidad necesitas? "
                "Por ejemplo: Medicina General, Kinesiología, Odontología, Ecografía."
            )
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
    # ── Ortodoncia/dental: rutea según si el paciente YA está en tratamiento ──
    # 4 macrogrupos de negocio, 2 destinos (dueño 2026-07-08):
    #   Grupo 1 (nunca evaluado, quiere brackets)              → odontología
    #     general (Dra. Burgos, $15.000 evaluación/presupuesto, gratis si
    #     empieza tratamiento previo ese día).
    #   Grupos 2/3/4 (instalación parcial/completa, en control) → YA están
    #     con la Dra. Castillo (66) → horas directo con ELLA, con aviso de
    #     que pueden ajustarse.
    # Identidad: phone → rut → BI (_paciente_ortodoncia_activo, misma consulta
    # que el gate WAIT_ORTODONCIA_ACTIVO/Patrón 4). También dispara con la
    # palabra "control" cuando piden ortodoncia explícita (grupo 4 sin perfil
    # con RUT todavía). Aplica también a pedidos de "dental" genéricos — un
    # paciente activo que escribe "necesito hora dental" va con Castillo, no
    # con Burgos.
    _orto_bypass = data.pop("_orto_bypass_evaluacion", False)
    _es_pedido_ortodoncia = especialidad_lower in ("ortodoncia", "ortodoncista", "brackets", "frenillos")
    _es_pedido_dental_generico = especialidad_lower in (
        "odontología", "odontologia", "dentista", "dental",
        "odontólogo", "odontologo", "odontología general", "odontologia general",
    )
    if not _orto_bypass and (_es_pedido_ortodoncia or _es_pedido_dental_generico):
        _txt_raw_orto = (_txt_raw or "").lower()
        _es_control_orto = _es_pedido_ortodoncia and any(
            k in _txt_raw_orto
            for k in ("control", "seguimiento", "mantención", "mantencion",
                      "ajuste", "cita de control", "mi control")
        )
        _activo_orto = (await _paciente_ortodoncia_activo(phone)) > 0
        if _es_control_orto or _activo_orto:
            # Grupos 2/3/4 — ya en tratamiento con la Dra. Castillo.
            log_event(phone, "ortodoncia_activo_directo_castillo", {
                "especialidad_original": especialidad,
                "es_control_texto": _es_control_orto,
                "activo_bi": _activo_orto,
            })
            data["_dental_origen"] = "ortodoncia"
            data["ortodoncia_redirigida"] = True
            data["_orto_bypass_evaluacion"] = True
            return await _iniciar_agendar(
                phone, data, "ortodoncia",
                saludo_prefix=(
                    "Como ya estás en tratamiento con la Dra. Daniela Castillo "
                    "(ortodoncista), te muestro tus horas directamente con ella 👇\n\n"
                    "⚠️ Las horas están sujetas a cambios, porque a veces debemos "
                    "ajustar los horarios.\n\n"
                ),
            )
        if _es_pedido_ortodoncia:
            # Grupo 1 — nunca evaluado, quiere brackets. Requiere evaluación
            # previa con odontología general (Dra. Burgos). Mensaje textual
            # exacto acordado con el dueño (2026-07-08).
            log_event(phone, "ortodoncia_redirigida_odonto", {"especialidad_original": especialidad})
            data["_dental_origen"] = "ortodoncia"
            data["ortodoncia_redirigida"] = True
            return await _iniciar_agendar(
                phone, data, "odontología",
                saludo_prefix=(
                    "¿Quieres empezar tu tratamiento de ortodoncia? 🦷✨\n\n"
                    "Primero debes agendar una cita con nuestra dentista general.\n"
                    "Ella evaluará tu caso, verá si necesitas algún tratamiento previo, "
                    "te dará la orden para radiografías y tomará fotografías.\n"
                    "Después, ¡ella misma gestionará tu derivación con la ortodoncista! 😁\n\n"
                    "El valor del presupuesto es de $15.000, pero si decides comenzar tu "
                    "tratamiento previo en ese momento, el presupuesto te sale gratis y "
                    "solo pagas la acción que se realice ese día.\n\n"
                    "Quedamos atentos si quieres agendar tu hora. 😊\n\n"
                ),
            )
        # _es_pedido_dental_generico sin señal de tratamiento activo → sigue
        # el flujo normal de odontología general más abajo (solo Dra. Burgos).

    # Endodoncia (tratamiento de conducto): requiere evaluación previa con
    # odontología general. La dentista evalúa, toma radiografías y confirma
    # si corresponde tratamiento de conducto con el Dr. Fredes (endodoncista).
    if especialidad_lower in ("endodoncia", "conducto", "tratamiento de conducto",
                               "endodoncista", "canal"):
        log_event(phone, "endodoncia_redirigida_odonto", {"especialidad_original": especialidad})
        data["_dental_origen"] = "endodoncia"
        data["endodoncia_redirigida"] = True
        return await _iniciar_agendar(
            phone, data, "odontología",
            saludo_prefix=(
                "El tratamiento de conducto (endodoncia) con el Dr. Fredes requiere "
                "primero una *evaluación con odontología general*. La dentista revisa "
                "el diente, toma una radiografía y confirma si necesitas endodoncia "
                "antes de derivarte al especialista.\n\n"
                "💰 Evaluación: *$15.000*\n\n"
                "Te muestro horas disponibles para la evaluación 👇\n\n"
            ),
        )

    # Implantología (implantes dentales): requiere evaluación previa con
    # odontología general. La dentista evalúa el hueso, la encía y el estado
    # general antes de derivar a la especialista si corresponde.
    if especialidad_lower in ("implantología", "implantologia", "implante",
                               "implantes", "implantólogo", "implantologa",
                               "implantóloga", "implantologo"):
        log_event(phone, "implantologia_redirigida_odonto", {"especialidad_original": especialidad})
        data["_dental_origen"] = "implantología"
        data["implantologia_redirigida"] = True
        return await _iniciar_agendar(
            phone, data, "odontología",
            saludo_prefix=(
                "Los implantes dentales requieren primero una *evaluación con "
                "odontología general*. La dentista evalúa el hueso, la encía y "
                "el estado bucal general, y coordina la derivación a la especialista "
                "si eres candidato.\n\n"
                "💰 Evaluación: *$15.000*\n\n"
                "Te muestro horas disponibles para la evaluación 👇\n\n"
            ),
        )

    # ── Bioimpedanciometría: tamizaje de seguridad ANTES de mostrar horas ─────
    # Los fabricantes de los equipos (Tanita, InBody) contraindican la BIA en
    # portadores de marcapasos/desfibrilador implantado, y no se realiza en
    # embarazo (además el resultado no sería confiable: cambia el agua corporal).
    # Un paciente con marcapasos NO puede llegar a agendar solo por WhatsApp:
    # se le corta acá y se deriva a recepción.
    if especialidad_lower in _BIA_KEYS:
        data["especialidad"] = "bioimpedanciometría"
        if not data.pop("_bia_screening_ok", False):
            save_session(phone, "WAIT_BIA_SCREENING", data)
            return _btn_msg(
                "Antes de darte hora para la *bioimpedanciometría* necesito hacerte "
                "una pregunta de seguridad.\n\n"
                "¿Tienes *marcapasos*, *desfibrilador implantado* u otro dispositivo "
                "médico electrónico implantado, o estás *embarazada*?",
                [
                    {"id": "bia_no_riesgo", "title": "No, ninguno"},
                    {"id": "bia_si_riesgo", "title": "Sí"},
                ]
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

    # Pediatría solicitada → Medicine General con aclaración explícita.
    # El flag _pediatra_a_mg lo setea IDLE cuando detecta alias pediátrico.
    if data.pop("_pediatra_a_mg", False) and not saludo_prefix:
        saludo_prefix = (
            "Nuestros médicos generales atienden pacientes de todas las edades, "
            "incluidos niños.\n\n"
            "Para atención pediátrica especializada, lo más adecuado es el CESFAM "
            "o el Hospital de Arauco, pero para consultas generales o de morbilidad "
            "en niños, nuestros médicos pueden ayudarte.\n\n"
            "Te muestro la disponibilidad 👇\n\n"
        )

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
    elif especialidad_lower in _ESP_MED_FAMILIAR:
        # Solo Dr. Alonso Márquez (ID 13) atiende Medicina Familiar en el CMC.
        # Si no tiene horas esta semana, ofrecer explícitamente cambio a Medicina General
        # — NO autoswitch silencioso a otros profesionales.
        _fp_mf = data.get("fecha_preferida")
        if _fp_mf:
            smart, todos = await buscar_slots_dia("medicina general", _fp_mf)
            todos = [s for s in (todos or []) if s.get("fecha") == _fp_mf and s.get("id_profesional") in _MED_FAMILIAR_IDS]
            smart = [s for s in (smart or []) if s.get("fecha") == _fp_mf and s.get("id_profesional") in _MED_FAMILIAR_IDS]
            if todos:
                mejor = todos[0]
            else:
                data["_aviso_sin_fecha_pedida"] = _fp_mf
                data.pop("fecha_preferida", None)
                smart, todos = await buscar_primer_dia("medicina general", solo_ids=_MED_FAMILIAR_IDS)
                mejor = todos[0] if todos else None
        else:
            smart, todos = await buscar_primer_dia("medicina general", solo_ids=_MED_FAMILIAR_IDS)
            mejor = todos[0] if todos else None
        # Normalizar label del slot a "Medicina Familiar" para display correcto
        for s in (todos or []):
            if isinstance(s, dict):
                s["especialidad"] = "Medicina Familiar"
        if mejor and isinstance(mejor, dict):
            mejor["especialidad"] = "Medicina Familiar"
        # Sin disponibilidad de Márquez → ofrecer cambio explícito a Medicina General,
        # NO caer silenciosamente a Abarca/Olavarría.
        if not todos or not mejor:
            data["medfam_sin_cupo_ofrecer_mg"] = True
            save_session(phone, "WAIT_MEDFAM_FALLBACK", data)
            return _btn_msg(
                "El Dr. Márquez (*Medicina Familiar*) no tiene horas disponibles esta semana.\n\n"
                "¿Te muestro horas con *Medicina General* (Dr. Abarca, Dr. Olavarría o Dr. Márquez)?",
                [
                    {"id": "medfam_fallback_si", "title": "Sí, mostrar Medicina General"},
                    {"id": "medfam_fallback_no", "title": "No, gracias"},
                ]
            )
        especialidad_lower = "medicina familiar"
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
                try:
                    smart, todos = await buscar_primer_dia(especialidad_lower)
                except Exception as _e_bp:
                    log.warning("buscar_primer_dia excepción esp=%s: %s", especialidad_lower, _e_bp)
                    smart, todos = [], []
            data.pop("fecha_preferida", None)
        else:
            # FIX 4: capturar excepción de Medilink (ej: ORL sin agenda abierta)
            # y tratar como 0 slots — así ofrece waitlist en vez de "error técnico".
            # "0 slots sin error" ya llega aquí con ([], []) y fluye a waitlist.
            # Solo con excepción real (timeout, HTTP 5xx) el código llegaba al
            # except del webhook y mostraba "Tuve un problema técnico".
            try:
                smart, todos = await buscar_primer_dia(especialidad_lower)
            except Exception as _e_bp:
                log.warning("buscar_primer_dia excepción esp=%s: %s", especialidad_lower, _e_bp)
                smart, todos = [], []
        mejor = smart[0] if smart else (todos[0] if todos else None)

    # Normaliza display de especialidad (ej: Psicologia Infantil vs Adulto)
    _normalizar_slot_especialidad(smart, especialidad_lower)
    _normalizar_slot_especialidad(todos, especialidad_lower)
    if mejor:
        _normalizar_slot_especialidad([mejor], especialidad_lower)

    # BUG-C: filtrar slots que el paciente ya rechazó en las últimas 48h
    try:
        _rechazados = get_slots_rechazados(phone, especialidad_lower)
        if _rechazados:
            def _no_rechazado(s):
                return (s.get("fecha", ""), s.get("hora_inicio", "")[:5]) not in _rechazados
            _todos_filtrado = [s for s in (todos or []) if _no_rechazado(s)]
            _smart_filtrado = [s for s in (smart or []) if _no_rechazado(s)]
            if _todos_filtrado:
                todos = _todos_filtrado
                smart = _smart_filtrado or _todos_filtrado[:5]
                if mejor and not _no_rechazado(mejor):
                    mejor = _todos_filtrado[0]
    except Exception:
        pass

    if not todos or not mejor:
        # Segunda baranda (2026-07-27): búsqueda vacía + breaker caído NO es
        # "no hay horas", es "no pudimos consultar". Inscribir en lista de
        # espera a alguien cuya agenda quizá está llena de cupos es la peor
        # respuesta posible: le cierra la puerta y encima le promete un aviso.
        if is_medilink_down():
            log_event(phone, "agendar_vacio_con_breaker_caido",
                      {"especialidad": especialidad_lower or ""})
            return _modo_degradado(phone, "agendar", state_snap=especialidad or "",
                                   especialidad=especialidad_lower or especialidad or "")
        log_event(phone, "sin_disponibilidad", {"especialidad": (especialidad or "").strip().lower()})
        save_tag(phone, "sin-disponibilidad")
        # Si la especialidad resuelve a un único profesional (ej. "olavarria",
        # "castillo"), lo guardamos como preferencia → el cron buscará solo a ese.
        from medilink import _ids_para_especialidad
        ids_resueltos = _ids_para_especialidad(especialidad_lower)
        id_prof_pref = int(ids_resueltos[0]) if len(ids_resueltos) == 1 else None
        data["waitlist_especialidad"] = especialidad_lower
        data["waitlist_id_prof_pref"] = id_prof_pref

        # Auditoría 2026-05-03: 145 sin_disponibilidad/30d → 0 inserts en waitlist.
        # Pacientes abandonaban en WAIT_WAITLIST_CONFIRM sin responder. Fix: si ya
        # conocemos al paciente (perfil completo) inscribir AUTOMÁTICO con opt-out
        # (sin fricción). Si no, fallback al flujo de pregunta explícita.
        perfil = get_profile(phone)
        if perfil and perfil.get("rut") and perfil.get("nombre"):
            data["rut"] = perfil["rut"]
            data["paciente_nombre"] = perfil["nombre"]
            try:
                wid = add_to_waitlist(phone, perfil["rut"], perfil["nombre"],
                                       especialidad_lower, id_prof_pref)
                save_tag(phone, f"waitlist-{especialidad_lower}")
                log_event(phone, "waitlist_inscrito_auto",
                          {"id": wid, "especialidad": especialidad_lower,
                           "id_prof_pref": id_prof_pref})
                reset_session(phone)
                nombre_corto = _first_name(perfil["nombre"])
                saludo = f"*{nombre_corto}*, " if nombre_corto else ""
                _msg_auto = _ESP_SIN_DISPONIBILIDAD_MSG.get(especialidad_lower)
                if _msg_auto:
                    _header_auto = _msg_auto.split("\n\n")[0]  # primera línea: contexto
                    return (
                        f"{_header_auto}\n\n"
                        f"Te inscribí {saludo}en la lista de espera. Apenas tengamos fecha "
                        "te aviso por este mismo chat 📱\n\n"
                        "Si prefieres no recibir aviso, responde *BAJA*.\n"
                        "_Escribe *menu* si necesitas algo más._"
                    )
                return (
                    f"No hay horas disponibles para *{especialidad}* en los próximos días 😕\n\n"
                    f"Te inscribí {saludo}en la lista de espera. Apenas se libere un cupo "
                    "te aviso por este mismo chat 📱\n\n"
                    "Si prefieres no recibir aviso, responde *BAJA*.\n"
                    "_Escribe *menu* si necesitas algo más._"
                )
            except Exception as e:
                log.warning("Error en auto-inscripción waitlist phone=%s: %s", phone, e)
                # cae al flujo con pregunta explícita

        save_session(phone, "WAIT_WAITLIST_CONFIRM", data)
        # Mensaje personalizado por especialidad (ej: ORL sin fecha de regreso).
        _msg_sin_disp = _ESP_SIN_DISPONIBILIDAD_MSG.get(especialidad_lower)
        _texto_sin_disp = (
            _msg_sin_disp
            if _msg_sin_disp
            else (
                f"No encontré horas disponibles para *{especialidad}* en los próximos días 😕\n\n"
                "¿Quieres que te avise apenas se libere un cupo?\n"
                "Te inscribo en nuestra lista de espera y te escribo por WhatsApp."
            )
        )
        return _btn_msg(
            _texto_sin_disp,
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
    # BUG-4 FIX: filtrar por franja horaria si el paciente la indicó
    _franja = data.pop("franja_horaria", None)
    if _franja:
        _h_min, _h_max = _franja
        def _slot_en_franja(s):
            try:
                _h = int(s.get("hora_inicio", "00:00")[:2])
                return _h_min <= _h <= _h_max
            except Exception:
                return True
        _smart_f = [s for s in smart_sugerido if _slot_en_franja(s)]
        _todos_f  = [s for s in todos if _slot_en_franja(s)]
        if _smart_f:
            smart_sugerido = _smart_f
        if _todos_f:
            todos = _todos_f
            if not mejor or not _slot_en_franja(mejor):
                mejor = _todos_f[0]
    # SOBRECUPO en la PRIMERA oferta: si la especialidad sobrecupea (eco) y la hora
    # formal está LEJOS, anteponer cupos cercanos ANTES de persistir/mostrar, para no
    # perder al paciente. Sin esto la 1ª oferta mostraba el formal lejano (caso real:
    # paciente con dolor recibió el martes 16 en vez del sobrecupo del lunes 8; la
    # inyección de WAIT_SLOT recién entraba en el render siguiente). Se actualiza mejor +
    # smart_sugerido + todos para que el mensaje, data["slots"] y "Sí, esa hora" usen el
    # sobrecupo. Inerte salvo SOBRECUPO_ENABLED=true (generar_slots → []).
    if not any(s.get("sobrecupo") for s in todos):
        try:
            import sobrecupo as _sc_first
            _sobres_first = await _sc_first.generar_slots(especialidad_lower)
            if _sobres_first:
                from medilink import _fmt_fecha as _ff_first
                _mejor_fecha = (mejor or {}).get("fecha", "9999-99-99") if mejor else "9999-99-99"
                _cercanos = [s for s in _sobres_first if s.get("fecha", "9999") < _mejor_fecha]
                if _cercanos:
                    for _s in _cercanos:
                        _s.setdefault("fecha_display", _ff_first(_s["fecha"]))
                    todos = _cercanos + todos
                    smart_sugerido = _cercanos + smart_sugerido
                    mejor = _cercanos[0]
                    log_event(phone, "sobrecupo_primera_oferta",
                              {"esp": especialidad_lower, "fecha": mejor.get("fecha")})
        except Exception as _e_scf:  # noqa: BLE001 — nunca romper el agendamiento
            log.warning("sobrecupo primera oferta falló: %s", _e_scf)

    data.update({"especialidad": especialidad_lower, "slots": smart_sugerido,
                 "todos_slots": todos, "fechas_vistas": [fecha],
                 "expansion_stage": 0, "prof_sugerido_id": prof_sugerido_id,
                 "slot_sugerido": mejor})
    log_event(phone, "funnel_especialidad", {
        "esp": especialidad_lower,
        "paso": "especialidad_resuelta",
        "n_slots": len(todos),
    })
    save_session(phone, "WAIT_SLOT", data)
    log_event(phone, "funnel_slot_ofrecido", {
        "esp": especialidad_lower,
        "paso": "slot_ofrecido",
        "fecha": fecha,
        "profesional": mejor.get("profesional", ""),
        "hora": mejor.get("hora_inicio", "")[:5],
    })
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
        # BUG-08: cuando hay aviso de redireccion, omitir saludo "Hola de nuevo"
        # para no mezclar "No tengo horarios para hoy" + "Hola de nuevo, Jaime! Te encontre hora"
        header = f"\u26a0\ufe0f No tengo horarios para *{_lbl_av}* \U0001f615\nTe muestro la *proxima disponible*:\n\n"
    # Tercer botón: "Otro profesional" si hay >1 doctor; si no, "Otro día"
    from medilink import _ids_para_especialidad
    ids_esp = _ids_para_especialidad(especialidad_lower)
    if especialidad_lower in _ESP_MED_GENERAL:
        ids_esp = list(_MED_GENERAL_IDS)  # Abarca, Olavarría, Márquez
    elif especialidad_lower in _ESP_MED_FAMILIAR:
        ids_esp = list(_MED_FAMILIAR_IDS)  # Solo Márquez — no hay "otro profesional"
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
    # P1-C bonus: para especialidades solo-particular, anotar "Solo Particular"
    # junto al precio para evitar la pregunta Fonasa posterior.
    _esp_bl = (mejor.get("especialidad") or especialidad_lower or "").lower()
    _es_solo_part_bl = bool(_esp_bl) and not any(
        _fsp.lower() == _esp_bl for _fsp in _FONASA_SPECIALTIES
    )
    _particular_nota = "_Solo Particular (no Fonasa)_\n" if _es_solo_part_bl and precio_linea else ""
    precio_bloque = f"{precio_linea}\n{_particular_nota}" if precio_linea else ""
    # Señal de escasez cuando quedan pocas horas
    n_slots = len(todos)
    escasez = ""
    if n_slots <= 2:
        escasez = "⚡ _Última hora disponible_\n"
    elif n_slots <= 4:
        escasez = f"⚡ _Quedan solo {n_slots} horas_\n"

    # BUG-A: si el paciente pidió a un profesional específico por nombre y
    # la primera disponibilidad NO es hoy, agregar aviso explícito.
    # Evita el caso real: paciente pidió "Dr. Rodrigo", bot mostró martes
    # sin aclarar que el doctor no atiende hoy.
    _aviso_no_hoy = ""
    _prof_pedido_id = data.get("prof_pedido_explicito")
    if not _prof_pedido_id:
        # También detectar por especialidad de prof único (olavarría, armijo, etc.)
        from medilink import _ids_para_especialidad as _ids_check_a
        _ids_a = _ids_check_a(especialidad_lower)
        if len(_ids_a) == 1:
            _prof_pedido_id = _ids_a[0]
    if _prof_pedido_id and not _fecha_avisar and not saludo_prefix:
        _hoy_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        _slot_fecha = mejor.get("fecha", "")
        if _slot_fecha and _slot_fecha != _hoy_str:
            try:
                from medilink import PROFESIONALES as _PROFS_A
                _prof_nombre = _PROFS_A.get(int(_prof_pedido_id), {}).get("nombre", "")
                if _prof_nombre:
                    _d_prox = datetime.strptime(_slot_fecha, "%Y-%m-%d")
                    _DIAS_ES = ["lunes","martes","miércoles","jueves","viernes","sábado","domingo"]
                    _MESES_ES = ["enero","febrero","marzo","abril","mayo","junio","julio","agosto","septiembre","octubre","noviembre","diciembre"]
                    _lbl_prox = f"{_DIAS_ES[_d_prox.weekday()]} {_d_prox.day} de {_MESES_ES[_d_prox.month - 1]}"
                    _aviso_no_hoy = (
                        f"_{_prof_nombre} no tiene horas disponibles hoy._\n"
                        f"_Te muestro su próxima disponibilidad para el {_lbl_prox}:_\n\n"
                    )
                    # Reemplaza header para no combinar con "¡Hola de nuevo!" duplicado
                    header = ""
            except Exception:
                pass

    return _btn_msg(
        f"{_aviso_no_hoy}{header}Te encontré hora ✨\n\n"
        f"🏥 *{mejor['especialidad']}* — {mejor['profesional']}\n"
        f"📅 *{mejor['fecha_display']}*\n"
        f"🕐 *{mejor['hora_inicio'][:5]}* ⭐\n"
        f"{precio_bloque}"
        f"{escasez}\n"
        "¿Te la reservo?",
        botones
    )


async def _buscar_citas_familiares(rut_titular: str) -> list[dict]:
    """Busca citas futuras de los dependientes vinculados a rut_titular via family_links.

    Retorna lista de dicts con keys 'paciente' y 'citas'. Solo incluye
    dependientes que tengan al menos una cita futura. Vacío si no hay links
    activos o si todos están sin citas. Nunca lanza — ante cualquier error
    retorna [].
    """
    try:
        dependientes = list_family_links(rut_titular)
        if not dependientes:
            return []
        resultado = []
        for dep in dependientes:
            dep_rut = dep.get("dependent_rut", "")
            dep_nombre = dep.get("dependent_nombre") or ""
            if not dep_rut:
                continue
            try:
                pac = await buscar_paciente(dep_rut)
                if not pac:
                    continue
                citas = await listar_citas_paciente(pac["id"], rut=pac.get("rut"))
                if citas:
                    # Usar el nombre de Medilink si el guardado en family_links está vacío
                    if not dep_nombre:
                        dep_nombre = pac.get("nombre", dep_rut)
                    resultado.append({"paciente": pac, "citas": citas, "dep_nombre": dep_nombre})
            except Exception:
                continue
        return resultado
    except Exception:
        return []


def _format_citas_familiares_cancelar(familiares: list[dict]) -> str:
    """Formatea la lista de citas de familiares para presentarla al usuario (flujo cancelar)."""
    lineas = ["Encontré citas a nombre de tus familiares:\n"]
    n = 1
    for fam in familiares:
        nombre_corto = _first_name(fam["dep_nombre"])
        for cita in fam["citas"]:
            esp = cita.get("especialidad", "")
            prof = cita.get("profesional", "")
            label = f"{esp} — {prof}" if esp else prof
            lineas.append(
                f"*{n}.* {nombre_corto} — {label}\n"
                f"   📅 {cita['fecha_display']} {cita['hora_inicio'][:5]}"
            )
            n += 1
    lineas.append("\nEscribe el *número* de la cita que quieres cancelar.")
    return "\n".join(lineas)


def _format_citas_familiares_reagendar(familiares: list[dict]) -> str:
    """Formatea la lista de citas de familiares para presentarla al usuario (flujo reagendar)."""
    lineas = ["Encontré citas a nombre de tus familiares:\n"]
    n = 1
    for fam in familiares:
        nombre_corto = _first_name(fam["dep_nombre"])
        for cita in fam["citas"]:
            esp = cita.get("especialidad", "")
            prof = cita.get("profesional", "")
            label = f"{esp} — {prof}" if esp else prof
            lineas.append(
                f"*{n}.* {nombre_corto} — {label}\n"
                f"   📅 {cita['fecha_display']} {cita['hora_inicio'][:5]}"
            )
            n += 1
    lineas.append("\nEscribe el *número* de la cita que quieres reagendar.")
    return "\n".join(lineas)


def _flatten_citas_familiares(familiares: list[dict]) -> list[dict]:
    """Aplana la lista de familiares+citas a una lista plana de citas,
    cada una con 'paciente' adjunto. El índice coincide con la numeración
    que muestra _format_citas_familiares_*."""
    planas = []
    for fam in familiares:
        for cita in fam["citas"]:
            cita_con_pac = dict(cita)
            cita_con_pac["_familiar_paciente"] = fam["paciente"]
            planas.append(cita_con_pac)
    return planas


async def _iniciar_cancelar(phone: str, data: dict, txt: str = "") -> str:
    if is_medilink_down():
        return _modo_degradado(phone, "cancelar")
    # Si ya conocemos el perfil, saltamos directo a mostrar sus citas (mismo
    # patrón que _iniciar_reagendar). Caso real 2026-05-25: Maritza Campos
    # (56976104434) tenía perfil guardado pero el bot le pedía RUT igual.
    perfil = get_profile(phone)
    if perfil and perfil.get("rut"):
        paciente = await buscar_paciente(perfil["rut"])
        if paciente:
            citas = await listar_citas_paciente(paciente["id"], rut=paciente.get("rut"))
            if not citas:
                # Buscar citas de familiares vinculados antes de responder "no hay"
                try:
                    familiares_con_citas = await _buscar_citas_familiares(perfil["rut"])
                except Exception:
                    familiares_con_citas = []
                if familiares_con_citas:
                    citas_planas = _flatten_citas_familiares(familiares_con_citas)
                    data.update({"citas_familiares": citas_planas, "rut": perfil["rut"]})
                    save_session(phone, "WAIT_CITA_CANCELAR_FAMILIAR", data)
                    log_event(phone, "cancelar_familiar_sugerido", {"rut": perfil["rut"], "n_citas": len(citas_planas)})
                    return _format_citas_familiares_cancelar(familiares_con_citas)
                reset_session(phone)
                return (
                    f"No encontré citas futuras para *{_first_name(paciente.get('nombre'))}* 📋\n\n"
                    "Si la hora está a nombre de otra persona (hijo/a, familiar), "
                    "escribe su RUT y la busco.\n\n"
                    "¿O prefieres agendar una nueva hora? Escribe *1* o *menu*."
                )
            data.update({"paciente": paciente, "citas": citas, "rut": perfil["rut"]})
            save_session(phone, "WAIT_CITA_CANCELAR", data)
            log_event(phone, "rut_autocompletado_cancelar", {"rut": perfil["rut"]})
            return _format_citas_cancelar(citas, paciente["nombre"])
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
            # FIX-10: sesión ya fue saved con WAIT_RUT_CANCELAR arriba; leer de nuevo es redundante
            return await handle_message(phone, _rut_emb, {"state": "WAIT_RUT_CANCELAR", "data": data})
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
            # FIX-10: sesión ya fue saved con WAIT_RUT_VER arriba; leer de nuevo es redundante
            return await handle_message(phone, _rut_emb, {"state": "WAIT_RUT_VER", "data": data})
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
                # Buscar citas de familiares vinculados antes de responder "no hay"
                try:
                    familiares_con_citas = await _buscar_citas_familiares(perfil["rut"])
                except Exception:
                    familiares_con_citas = []
                if familiares_con_citas:
                    citas_planas = _flatten_citas_familiares(familiares_con_citas)
                    data.update({"citas_familiares": citas_planas, "rut": perfil["rut"]})
                    save_session(phone, "WAIT_CITA_REAGENDAR_FAMILIAR", data)
                    log_event(phone, "reagendar_familiar_sugerido", {"rut": perfil["rut"], "n_citas": len(citas_planas)})
                    return _format_citas_familiares_reagendar(familiares_con_citas)
                reset_session(phone)
                return (
                    f"No encontré citas futuras para *{_first_name(paciente.get('nombre'))}* 📋\n\n"
                    "Si la hora está a nombre de otra persona (hijo/a, familiar), "
                    "escribe su RUT y la busco.\n\n"
                    "¿O prefieres agendar una nueva hora? Escribe *1* o *menu*."
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


def _derivar_humano(phone: str = None, contexto: str = "",
                    takeover_reason: str = "") -> str:
    if phone:
        # BUG-6 fix: reset msgs_sin_respuesta para que el primer mensaje
        # post-derivación reciba el ack "Recibido" en el handler HUMAN_TAKEOVER.
        # Si no reseteamos, un paciente que ya pasó por recepción antes tendría
        # msgs_sin_respuesta > 1 y quedaría en silencio.
        save_session(phone, "HUMAN_TAKEOVER", {
            "hold_sent": True,
            "handoff_reason": contexto[:200],
            "takeover_reason": takeover_reason or contexto[:50],
            "msgs_sin_respuesta": 0,
            "human_replied": False,
        })
        log_event(phone, "derivado_humano", {
            "razon": contexto[:200],
            "takeover_reason": takeover_reason or contexto[:50],
        })

    # Mensaje diferenciado para consultas médicas/fármacos: avisa que el bot
    # sigue disponible para trámites mientras la recepcionista responde.
    _es_consulta_medica = takeover_reason in ("consulta_medica", "farmaco", "sintoma")
    if _es_consulta_medica:
        msg = (
            "Tu consulta fue registrada 🙏 Una recepcionista te responderá en este chat.\n\n"
            "Mientras tanto, puedes:\n"
            "• Ver tus citas → escribe *mis horas*\n"
            "• Agendar una nueva hora → escribe *agendar*\n"
            "• Consultar precios o ubicación → escribe *precio* o *info*\n\n"
            f"Si es urgente o empeora: 📞 *{CMC_TELEFONO}* · 🚑 *SAMU 131*\n\n"
            "_Atendemos de lunes a sábado._"
        )
    else:
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
    """Interpreta texto libre como selección de slot. Retorna índice (0-based) o None.

    FIX-1: expandido para manejar ordinales en español y confirmaciones cortas
    cuando hay un solo slot mostrado.
    """
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

    # FIX-1: Ordinales en español → mapear a índice.
    # "la primera", "el primero", "primer hora", "la segunda", "el último", etc.
    _ORDINALES: dict[str, int] = {
        "primer":   0, "primera":  0, "primero":  0, "1er": 0, "1ro": 0, "1ra": 0,
        "segund":   1, "segunda":  1, "segundo":  1, "2do": 1, "2da": 1,
        "tercer":   2, "tercera":  2, "tercero":  2, "3ro": 2, "3ra": 2,
        "cuart":    3, "cuarta":   3, "cuarto":   3,
        "quint":    4, "quinta":   4, "quinto":   4,
    }
    for token, ord_idx in _ORDINALES.items():
        if token in tl and 0 <= ord_idx < len(slots):
            return ord_idx
    # "el último" / "la última" → último slot disponible
    if any(k in tl for k in ("ultim", "último", "ultima", "última", "last")):
        return len(slots) - 1

    # BUG-D FIX: endurecer detección de número como slot.
    # Antes: r'\b([1-9])\b' matcheaba en cualquier frase larga como
    # "llevo 3 días con fiebre", "RUT termina en 7", "soy nivel 5".
    # Ahora: primero verificar que el texto no contenga keywords de
    # cantidad/duración/contexto clínico; luego permitir número solo si
    # el texto es corto O contiene contexto ordinal explícito.
    _CANTIDAD_KW = (
        "días", "dia", "horas", "hora", "veces", "vez", "años", "año",
        "meses", "mes", "minutos", "kilos", "kilo", "metros", "litros",
        "fiebre", "dolor", "molestia", "sintoma", "síntoma", "semana",
        "nivel", "termina en", "termina", "grado", "grados",
    )
    _ORDINAL_CTX_KW = ("opcion", "opción", "numero", "número", "horario", "slot",
                       "el ", "la ", "opcion ", "opción ")

    # BUG-1 FIX original (edad/menor): mantener también
    _EDAD_CTX_RE = re.compile(
        r'\b\d+\s*(?:años?|meses?|añitos?)\b'
        r'|\b(?:beb[eé]|guagua|niñ[oa]|niet[oa]|hij[oa]|menor|lactante|infante|pequeñ[oa]|chic[oa])\b'
        r'|\b(?:para|es para|hora para|reservar para)\s+(?:mi|el|la|un|una)\s+',
        re.IGNORECASE,
    )

    # Si el texto contiene una hora de reloj (HH:MM), NO dejar que la rama de
    # "número suelto" consuma los dígitos de la hora: "19:00" matchea "19" → idx 18
    # → reservaba un slot totalmente distinto (bug 2026-06-23: pidió 19:00, reservó
    # 12:00). Saltar la rama de número y dejar que actúe el matcher de hora de abajo.
    _es_hora_reloj = bool(re.search(r'\b\d{1,2}[:.]\d{2}\b', tl))
    # Número dentro del texto: "el 1", "opción 2", "quiero el 3"
    m = re.search(r'\b([1-9]\d?)\b', tl)
    if m and not _es_hora_reloj:
        _palabras = [p for p in tl.split() if p.strip()]
        # Rechazar si hay keywords de cantidad/duración
        if any(k in tl for k in _CANTIDAD_KW):
            return None
        # Rechazar si texto largo sin contexto ordinal explícito
        if len(_palabras) > 4:
            if not any(k in tl for k in _ORDINAL_CTX_KW):
                return None
        # Rechazar contexto de edad/menor (BUG-1 guard)
        if len(_palabras) > 2 and _EDAD_CTX_RE.search(tl):
            return None
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
        lineas.append(f"*{i}.* {c['fecha_display']} · {c['hora_inicio'][:5]} · {c['profesional']}")
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
        from session import db as _conn_fn
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


# ── Abono-Gate: procesar imagen de comprobante ────────────────────────────────

async def procesar_imagen_abono(phone: str, img_bytes: bytes,
                                content_type: str | None) -> str:
    """Llama a leer_comprobante y decide: crear cita o derivar a humano.

    Retorna el texto del mensaje a enviar al paciente.
    Esta función es async para poder llamar a crear_cita (también async).
    Es llamada DIRECTAMENTE desde main.py cuando msg_type=="image" y
    state=="WAIT_ABONO_COMPROBANTE", ANTES del pipeline normal de media.
    """
    import asyncio
    from datetime import datetime as _dt_pc
    from zoneinfo import ZoneInfo as _ZI_pc
    from session import get_session, save_session, reset_session, log_event
    from session import log_message
    from abono_comprobante import leer_comprobante
    from config import ABONO_PSIQUIATRIA_CLP, CMC_TELEFONO_FIJO, ADMIN_ALERT_PHONE
    from abonos_routes import ensure_abonos_table
    from session import db as _sdb
    from medilink import crear_cita
    from resilience import spawn_task as _spawn

    _CHILE_TZ_pc = _ZI_pc("America/Santiago")

    sess = get_session(phone)
    if not sess or sess.get("state") != "WAIT_ABONO_COMPROBANTE":
        # Ya no estamos en el estado correcto — no hacer nada
        return ""

    data = sess.get("data") or {}
    slot    = data.get("abono_gate_slot") or {}
    paciente = data.get("abono_gate_paciente") or {}

    # Verificar timeout 90 min (mismo check que en el handler de texto)
    _gate_ts_str = data.get("abono_gate_ts", "")
    if _gate_ts_str:
        try:
            _gate_dt = _dt_pc.fromisoformat(_gate_ts_str)
            if _gate_dt.tzinfo is None:
                _gate_dt = _gate_dt.replace(tzinfo=_CHILE_TZ_pc)
            _elapsed = (_dt_pc.now(_CHILE_TZ_pc) - _gate_dt).total_seconds() / 60
            if _elapsed > 90:
                log_event(phone, "abono_gate_timeout", {"gate_ts": _gate_ts_str})
                reset_session(phone)
                return (
                    "El tiempo para enviar el comprobante venció y el aparte fue liberado.\n\n"
                    "Escribe *menu* si quieres volver a buscar una hora de Psiquiatría."
                )
        except Exception:
            pass

    # Leer comprobante con Claude vision
    resultado_vision = leer_comprobante(img_bytes, content_type)
    legible = resultado_vision.get("legible", False)
    monto   = resultado_vision.get("monto") or 0

    log_event(phone, "abono_comprobante_leido", {
        "legible": legible,
        "monto": monto,
        "codigo": resultado_vision.get("codigo_operacion"),
        "banco_origen": resultado_vision.get("banco_origen"),
    })

    # Validación suave: monto suficiente Y legible
    if not legible or monto < ABONO_PSIQUIATRIA_CLP:
        # Derivar a humano con contexto — la cita NO se crea
        motivo = "monto_insuficiente" if (legible and monto < ABONO_PSIQUIATRIA_CLP) else "ilegible"
        log_event(phone, "abono_comprobante_fallo", {
            "motivo": motivo,
            "monto_leido": monto,
            "monto_requerido": ABONO_PSIQUIATRIA_CLP,
        })
        save_session(phone, "HUMAN_TAKEOVER", {
            "hold_sent": True,
            "handoff_reason": f"abono_comprobante_{motivo}",
            "abono_gate_slot": slot,
        })

        # Aviso a recepción con el contexto
        if ADMIN_ALERT_PHONE:
            _nom_pf = paciente.get("nombre", "")
            _slot_fd = slot.get("fecha_display", slot.get("fecha", ""))
            _hora_pf = (slot.get("hora_inicio") or "")[:5]
            if motivo == "monto_insuficiente":
                _aviso_pf = (
                    f"⚠️ *Abono Psiquiatría — validar manual*\n"
                    f"Paciente: {_nom_pf} · WA: {phone}\n"
                    f"Cita: {_slot_fd} {_hora_pf}\n"
                    f"Comprobante recibido. Monto leído: ${monto:,} "
                    f"(requerido: ${ABONO_PSIQUIATRIA_CLP:,}). Monto no calza — verificar con el banco."
                )
            else:
                _aviso_pf = (
                    f"⚠️ *Abono Psiquiatría — comprobante ilegible*\n"
                    f"Paciente: {_nom_pf} · WA: {phone}\n"
                    f"Cita: {_slot_fd} {_hora_pf}\n"
                    "Comprobante recibido pero no pude leerlo automáticamente — validar manual con el banco."
                )
            async def _notif_recep_fallo():
                from messaging import send_whatsapp as _sw_pf
                await _sw_pf(ADMIN_ALERT_PHONE, _aviso_pf)
                log_message(ADMIN_ALERT_PHONE, "out", _aviso_pf, "WAIT_ABONO_COMPROBANTE")
            _spawn(_notif_recep_fallo())

        if motivo == "monto_insuficiente":
            _monto_req_fmt = f"${ABONO_PSIQUIATRIA_CLP:,}".replace(",", ".")
            _monto_leido_fmt = f"${monto:,}".replace(",", ".")
            return (
                f"Vi que el monto en el comprobante es *{_monto_leido_fmt}* y necesitamos "
                f"*{_monto_req_fmt}* para confirmar la hora de Psiquiatría.\n\n"
                "Le avisé a recepción para que te contacte y lo aclaren.\n\n"
                f"Si tienes dudas, llama al 📞 *{CMC_TELEFONO_FIJO}*"
            )
        else:
            return (
                "No pude leer el comprobante automáticamente. "
                "Le avisé a recepción para que lo revise manualmente.\n\n"
                f"Si tienes dudas, llama al 📞 *{CMC_TELEFONO_FIJO}*"
            )

    # ── Validación OK: crear cita en Medilink ────────────────────────────────
    id_cita = ""
    try:
        resultado_ml = await asyncio.wait_for(crear_cita(
            id_paciente=paciente["id"],
            id_profesional=slot["id_profesional"],
            fecha=slot["fecha"],
            hora_inicio=slot["hora_inicio"],
            hora_fin=slot["hora_fin"],
            id_recurso=slot.get("id_recurso", 1),
            modalidad=data.get("telemedicina_modalidad", "TELEMEDICINA"),
        ), timeout=45)
        if isinstance(resultado_ml, dict):
            id_cita = str(resultado_ml.get("id", ""))
    except Exception as _err_ml:
        log.error("procesar_imagen_abono: crear_cita falló: %s", _err_ml)
        resultado_ml = None

    if not resultado_ml:
        # La cita falló (slot tomado u otro error) — intentar re-buscar
        from medilink import buscar_primer_dia as _bpd_ag
        try:
            _smart_ag, _todos_ag = await asyncio.wait_for(
                _bpd_ag("psiquiatría"), timeout=30)
        except Exception:
            _smart_ag, _todos_ag = [], []

        if _smart_ag:
            _new_slot = _smart_ag[0]
            data["abono_gate_slot"] = _new_slot
            data["slot_elegido"]    = _new_slot
            save_session(phone, "CONFIRMING_CITA", data)
            log_event(phone, "abono_gate_slot_tomado_rebusco", {
                "new_fecha": _new_slot.get("fecha"),
                "new_hora":  _new_slot.get("hora_inicio"),
            })
            return (
                "Recibí tu comprobante ✅ pero esa hora fue tomada mientras esperábamos.\n\n"
                f"Te encontré otra disponible:\n"
                f"📅 *{_new_slot.get('fecha_display', '')}* a las "
                f"*{_new_slot.get('hora_inicio', '')[:5]}* con "
                f"*{_new_slot.get('profesional', '')}*\n\n"
                "¿La reservo con el abono que ya enviaste? Escribe *si* para confirmar."
            )
        else:
            # No hay alternativa → humano
            reset_session(phone)
            log_event(phone, "abono_gate_sin_alternativa", {})
            return (
                "Recibí tu comprobante ✅ pero al intentar reservar la hora ya no estaba disponible "
                "y no encontré otra. Avisé a recepción para que te contacten y coordinen.\n\n"
                f"📞 *{CMC_TELEFONO_FIJO}*"
            )

    # ── Cita creada → INSERT en abonos_cmc ───────────────────────────────────
    now_cl = _dt_pc.now(_CHILE_TZ_pc)
    precio_total = 60000  # valor consulta psiquiatría
    saldo = max(precio_total - monto, 0)
    fecha_cita_str = slot.get("fecha_display", slot.get("fecha", ""))
    try:
        ensure_abonos_table()
        with _sdb() as _conn_ab:
            _conn_ab.execute(
                """INSERT INTO abonos_cmc
                   (fecha, hora, paciente_nombre, rut, id_profesional, profesional,
                    area, fecha_cita, precio_total, monto_abono, saldo,
                    metodo_pago, codigo_transferencia, estado, id_cita, nota,
                    creado_por, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))""",
                (
                    now_cl.strftime("%Y-%m-%d"),
                    now_cl.strftime("%H:%M"),
                    paciente.get("nombre", ""),
                    (data.get("rut") or "").strip(),
                    slot.get("id_profesional"),
                    slot.get("profesional", ""),
                    "Psiquiatría",
                    fecha_cita_str,
                    precio_total,
                    monto,
                    saldo,
                    "transferencia",
                    resultado_vision.get("codigo_operacion") or "",
                    "pendiente",
                    id_cita,
                    f"auto-bot: comprobante leído por visión — banco {resultado_vision.get('banco_origen') or '?'}",
                    "bot",
                )
            )
            _conn_ab.commit()
        log_event(phone, "abono_comprobante_ok", {
            "monto": monto,
            "id_cita": id_cita,
            "codigo": resultado_vision.get("codigo_operacion"),
        })
    except Exception as _e_ab:
        log.warning("procesar_imagen_abono: INSERT abonos_cmc falló: %s", _e_ab)

    # ── Confirmación al paciente ──────────────────────────────────────────────
    nombre_corto = _first_name(paciente.get("nombre", ""))
    saludo = f"*{nombre_corto}*" if nombre_corto else "Tu hora"
    _saldo_fmt = f"${saldo:,}".replace(",", ".")
    confirmacion = (
        f"✅ *{saludo}, tu hora de Psiquiatría quedó confirmada.*\n\n"
        f"👤 {paciente.get('nombre', '')}\n"
        f"🏥 Psiquiatría — {slot.get('profesional', '')}\n"
        f"📅 {slot.get('fecha_display', slot.get('fecha', ''))}\n"
        f"🕐 {(slot.get('hora_inicio') or '')[:5]}\n\n"
        f"Abono recibido: ${monto:,} CLP\n"
        f"Saldo a pagar el día de la atención: {_saldo_fmt} CLP\n\n"
        "Recepción validará la transferencia con el banco.\n"
        "_Escribe *menu* si necesitas algo más._"
    ).replace(",", ".")

    reset_session(phone)

    # Aviso a recepción para validación real
    if ADMIN_ALERT_PHONE:
        _nom_conf = paciente.get("nombre", "")
        _aviso_conf = (
            f"✅ *Abono Psiquiatría — VALIDAR CON BANCO*\n"
            f"Paciente: {_nom_conf} · WA: {phone}\n"
            f"Cita: {fecha_cita_str} {(slot.get('hora_inicio') or '')[:5]} "
            f"(ID Medilink: {id_cita})\n"
            f"Monto: ${monto:,}\n"
            f"Código op.: {resultado_vision.get('codigo_operacion') or '?'}\n"
            f"Banco origen: {resultado_vision.get('banco_origen') or '?'}\n"
            f"Titular: {resultado_vision.get('titular_origen') or '?'}\n"
            "⚠️ Verificar que la transferencia llegó al banco antes de confirmar."
        ).replace(",", ".")
        async def _notif_recep_ok():
            from messaging import send_whatsapp as _sw_conf
            await _sw_conf(ADMIN_ALERT_PHONE, _aviso_conf)
            log_message(ADMIN_ALERT_PHONE, "out", _aviso_conf, "WAIT_ABONO_COMPROBANTE")
        _spawn(_notif_recep_ok())

    return confirmacion
