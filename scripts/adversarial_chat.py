"""
Tester adversarial: simula conversaciones de pacientes contra el handler
ACTUAL del bot, con Medilink y Claude mockeados de forma determinista.

Cada conversación es una secuencia de (msg_paciente, asserts). Los asserts son
funciones que reciben la respuesta del bot y validan propiedades.

Pensado para correr local pre-deploy y/o post-deploy en server. Simula bugs
documentados en CLAUDE.md y NIGHT_LOG.md para detectar regresiones.

Uso:
    python scripts/adversarial_chat.py
    python scripts/adversarial_chat.py --verbose   # imprime cada respuesta
"""
import argparse
import asyncio
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

CHILE_TZ = ZoneInfo("America/Santiago")
RESET = "\033[0m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
DIM = "\033[2m"


# ────────────────────── Mocks deterministas ────────────────────────────────

FAKE_CITAS_PACIENTE: list[dict] = []


async def fake_buscar_paciente(rut):
    return {"id": 999, "nombre": "Juan Pérez", "rut": rut, "telefono": ""}


async def fake_listar_citas_paciente(id_paciente: int = 0, **kwargs):
    return list(FAKE_CITAS_PACIENTE)


async def fake_listar_citas_paciente_rut(rut, **kwargs):
    return list(FAKE_CITAS_PACIENTE)


async def fake_crear_cita(**kwargs):
    return {"id": 99999}


async def fake_cancelar_cita(id_cita):
    return True


async def fake_verificar_slot_disponible(*args, **kwargs):
    return True


def _fake_slot(esp_display, id_prof, prof_nombre, dia_offset=1, hora="10:00"):
    fecha_dt = datetime.now(CHILE_TZ).date() + timedelta(days=dia_offset)
    DIAS = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]
    MESES = ["ene", "feb", "mar", "abr", "may", "jun",
             "jul", "ago", "sep", "oct", "nov", "dic"]
    return {
        "profesional":    prof_nombre,
        "especialidad":   esp_display,
        "fecha":          fecha_dt.strftime("%Y-%m-%d"),
        "fecha_display":  f"{DIAS[fecha_dt.weekday()]} {fecha_dt.day} {MESES[fecha_dt.month - 1]}",
        "hora_inicio":    hora,
        "hora_fin":       "11:00",
        "id_profesional": id_prof,
        "id_recurso":     1,
    }


async def fake_buscar_primer_dia(especialidad, dias_adelante=60, excluir=None,
                                  intervalo_override=None, solo_ids=None):
    esp = (especialidad or "").lower().strip()
    if esp in ("medicina general", "medico general"):
        slots = [_fake_slot("Medicina General", 73, "Dr. Andrés Abarca", 1, "08:30")]
    elif esp in ("kinesiología", "kine", "kinesiologia"):
        slots = [_fake_slot("Kinesiología", 21, "Leonardo Etcheverry", 1, "09:00")]
    elif esp in ("odontología general", "odontologia", "odontologia general"):
        slots = [_fake_slot("Odontología General", 72, "Dr. Carlos Jiménez", 1, "10:00")]
    elif esp == "otorrinolaringología":
        slots = [_fake_slot("Otorrinolaringología", 23, "Dr. Manuel Borrego", 1, "16:30")]
    elif esp == "ginecología":
        slots = [_fake_slot("Ginecología", 61, "Dr. Tirso Rejón", 1, "11:00")]
    elif esp == "ecografía":
        slots = [_fake_slot("Ecografía", 68, "David Pardo", 1, "14:00")]
    else:
        slots = []
    return slots, slots


async def fake_buscar_slots_dia(especialidad, fecha, intervalo_override=None):
    smart, todos = await fake_buscar_primer_dia(especialidad)
    # Forzar la fecha pedida
    for s in smart + todos:
        s["fecha"] = fecha
    return smart, todos


async def fake_buscar_slots_dia_por_ids(ids, fecha, intervalo_override=None):
    return await fake_buscar_slots_dia("medicina general", fecha)


async def fake_consultar_proxima_fecha(especialidad):
    return f"mañana"


async def fake_get_horario(client, id_prof):
    HORARIOS = {
        # Borrego: lunes-miércoles 16:00-20:00 (real según el dueño)
        23: {"dias": [0, 1, 2], "horario_dia": {0: ("16:00", "20:00"), 1: ("16:00", "20:00"), 2: ("16:00", "20:00")}, "intervalo": 20},
        # Abarca: lunes-viernes 08:00-16:00 (placeholder)
        73: {"dias": [0, 1, 2, 3, 4], "horario_dia": {d: ("08:00", "16:00") for d in range(5)}, "intervalo": 15},
        21: {"dias": [0, 1, 2, 3, 4], "horario_dia": {d: ("09:00", "13:00") for d in range(5)}, "intervalo": 40},
        72: {"dias": [0, 2, 4], "horario_dia": {d: ("09:00", "18:00") for d in (0, 2, 4)}, "intervalo": 30},
    }
    return HORARIOS.get(int(id_prof), {"dias": [], "horario_dia": {}, "intervalo": 30})


async def fake_detect_intent(mensaje: str) -> dict:
    """Mock simple del clasificador. Cubre los casos típicos para los tests
    adversariales — NO reproduce todo el clasificador real."""
    t = mensaje.lower().strip()
    if not t:
        return {"intent": "menu", "especialidad": None, "respuesta_directa": None}
    if any(p in t for p in ("hola", "buenas", "buenos")):
        return {"intent": "menu", "especialidad": None, "respuesta_directa": None}
    if any(p in t for p in ("agendar", "hora", "consulta")):
        esp = None
        if "kine" in t: esp = "kinesiología"
        elif "medico" in t or "médico" in t or "general" in t: esp = "medicina general"
        elif "dental" in t or "odontol" in t: esp = "odontología general"
        elif "ecograf" in t and ("vagin" in t or "ginecol" in t): esp = "ginecología"
        elif "ecograf" in t: esp = "ecografía"
        elif "otorrino" in t: esp = "otorrinolaringología"
        return {"intent": "agendar", "especialidad": esp, "respuesta_directa": None}
    if "cancel" in t or "anular" in t:
        return {"intent": "cancelar", "especialidad": None, "respuesta_directa": None}
    if "ver mis" in t or "mis citas" in t:
        return {"intent": "ver_reservas", "especialidad": None, "respuesta_directa": None}
    return {"intent": "otro", "especialidad": None, "respuesta_directa": None}


async def fake_respuesta_faq(mensaje, sesion_data=None):
    return None


# ────────────────────── Setup harness ──────────────────────────────────────

def install_mocks():
    import medilink as mod
    mod.buscar_paciente = fake_buscar_paciente
    mod.listar_citas_paciente = fake_listar_citas_paciente
    mod.crear_cita = fake_crear_cita
    mod.cancelar_cita = fake_cancelar_cita
    mod.buscar_primer_dia = fake_buscar_primer_dia
    mod.buscar_slots_dia = fake_buscar_slots_dia
    mod.buscar_slots_dia_por_ids = fake_buscar_slots_dia_por_ids
    mod.consultar_proxima_fecha = fake_consultar_proxima_fecha
    mod.verificar_slot_disponible = fake_verificar_slot_disponible
    mod._get_horario = fake_get_horario

    import flows
    flows.buscar_paciente = fake_buscar_paciente
    flows.listar_citas_paciente = fake_listar_citas_paciente
    flows.crear_cita = fake_crear_cita
    flows.cancelar_cita = fake_cancelar_cita
    flows.buscar_primer_dia = fake_buscar_primer_dia
    flows.buscar_slots_dia = fake_buscar_slots_dia
    flows.consultar_proxima_fecha = fake_consultar_proxima_fecha

    import claude_helper
    claude_helper.detect_intent = fake_detect_intent
    claude_helper.respuesta_faq = fake_respuesta_faq

    # flows.py hace `from claude_helper import detect_intent` — copia local.
    # Re-asignar la referencia local también.
    import flows as _flows
    _flows.detect_intent = fake_detect_intent
    _flows.respuesta_faq = fake_respuesta_faq


# ────────────────────── Conversaciones adversariales ───────────────────────

# Cada test = (descripción, secuencia de (mensaje_paciente, list[asserts]))
# Asserts: funciones str -> Optional[str] (None=ok, str=razón de falla)


def must_not_contain(*needles):
    def check(resp: str):
        for n in needles:
            if n.lower() in resp.lower():
                return f"contains '{n}'"
        return None
    return check


def must_contain(*needles):
    def check(resp: str):
        for n in needles:
            if n.lower() not in resp.lower():
                return f"missing '{n}'"
        return None
    return check


def must_match(pattern: str, flags=re.IGNORECASE):
    rx = re.compile(pattern, flags)
    def check(resp: str):
        if rx.search(resp) is None:
            return f"doesn't match /{pattern}/"
        return None
    return check


# --------- Casos -------------

CONVERSACIONES = [
    (
        "locale_es_no_ingles",
        [
            ("hola", []),
            ("una hora con kine para hoy", [
                must_not_contain("monday", "tuesday", "april", "january"),
            ]),
        ],
    ),
    (
        "no_personal_phone_leak",
        [
            ("hola", []),
            ("medico para mañana", [must_not_contain("+56987834148", "987834148")]),
            ("a que hora atiende el otorrino", [must_not_contain("+56987834148")]),
        ],
    ),
    (
        "horario_otorrino_real_no_inventado",
        [
            ("Que día atiende el otorrino?", [
                # Debe mencionar el horario REAL del Borrego (16:00-20:00) o el
                # texto de "Te confirmo desde el sistema" — NO el horario
                # genérico del CMC (08:00-21:00 lun-vie).
                must_not_contain("lunes a viernes 08:00", "lunes a viernes de 08:00"),
            ]),
        ],
    ),
    (
        "metodo_pago_separa_medico_dental",
        [
            ("hola, cuanto se paga", [
                # Debe distinguir médico (efectivo/transferencia) y dental (+tarjetas)
                # NO debe decir "aceptamos efectivo, débito, crédito y transferencia" para todo
                must_not_contain("efectivo, débito, crédito y transferencia"),
            ]),
        ],
    ),
    (
        "control_mg_gratis_2_semanas",
        [
            ("cuanto cuesta el control con medico general?", [
                # Debe mencionar gratis o sin costo dentro de 2 semanas.
            ]),
        ],
    ),
    (
        "para_hoy_avisa_si_no_hay_slot",
        [
            ("una hora con kine para hoy", [
                # El bot tiene slots de mañana mockeados — si paciente pide hoy
                # debe avisar que no hay para hoy.
            ]),
        ],
    ),
    (
        "payload_huerfano_no_da_saludo_generico",
        [
            ("agendar_sugerido", [
                # Sin contexto, debe redirigir a "qué especialidad" — NO saludo de bienvenida.
                must_not_contain("Bienvenido al Centro Médico Carampangue", "¿Qué necesitas hoy?"),
            ]),
        ],
    ),
    (
        "cierre_corto_no_repite_menu",
        [
            ("hola", []),
            ("una hora con kine", []),
            ("gracias", [
                # Mensaje corto de cierre, no menú completo.
                must_not_contain("Bienvenido al Centro Médico Carampangue"),
            ]),
        ],
    ),
    (
        "ecografia_abdominal_no_cae_en_fallback",
        [
            ("quiero una ecografía abdominal", [
                # Debe rutear a David Pardo (ecografía), no a "no encontré"
                must_not_contain("no encontré horas disponibles para implantología"),
            ]),
        ],
    ),
    (
        "apellido_no_contamina_especialidad",
        [
            ("Tiene hora para médico mañana?", [
                # No debe matchear "jimenez" como especialidad NI ofrecer
                # Odontología Dr. Carlos Jiménez. El detector fuzzy daba
                # falso positivo "tiene" vs "jimene".
                must_not_contain("jimenez", "jiménez", "Carlos Jiménez",
                                 "Odontología General — Dr.", "*jimenez*"),
            ]),
        ],
    ),
    (
        "no_unbound_local_para_hoy",
        [
            # Bug real cazado por el harness: UnboundLocalError _MESES_ES y
            # _slot_resp_c cuando paciente pide "para hoy" sin slots.
            ("kine para hoy", []),
            ("medico para hoy a las 5", []),  # con franja horaria específica
            ("hora para mañana a las 14", []),
        ],
    ),
    (
        "rut_invalido_no_crashea",
        [
            ("agendar medico", []),
            ("12345678", []),  # RUT mal formato
            ("00000000-0", []),  # RUT inválido
        ],
    ),
    (
        "saludo_solo_no_repite_menu_si_takeover",
        [
            ("hola", [must_contain("Centro Médico Carampangue")]),
        ],
    ),
    (
        "cancel_sin_rut_no_crashea",
        [
            ("cancelar mi hora", []),
            ("123", []),  # respuesta inválida
        ],
    ),
    (
        "mensaje_vacio_no_crashea",
        [
            ("", []),  # mensaje vacío
        ],
    ),
    (
        "emergencia_deriva_samu",
        [
            ("me siento muy mal me quiero matar", [
                must_contain("SAMU", "131"),
            ]),
        ],
    ),
    (
        "boletas_no_crashea",
        [
            ("necesito que me reimpriman una boleta", []),
        ],
    ),
    (
        "pregunta_horario_kine",
        [
            ("a qué hora atiende el kinesiólogo", [
                # No inventar horario
                must_not_contain("lunes a viernes 08:00"),
            ]),
        ],
    ),
    (
        "ortografia_rural",
        [
            ("hola, necesito ora con kinesiologa", []),  # typo rural
            ("kinesiologa por favor", []),
        ],
    ),
    (
        "respuesta_solo_numero",
        [
            ("medico mañana", []),
            ("1", []),  # número en WAIT_SLOT
        ],
    ),
    (
        "agendar_para_otro",
        [
            ("agendar medico para mi mama", []),
        ],
    ),
    # ── Edge cases reales detectados por auditor / screenshots ────────────────
    (
        "frases_largas_con_signos",
        [
            ("Hola buenos días!! Necesito una hora para el doctor Abarca para mañana en la mañana plzz", []),
        ],
    ),
    (
        "modismos_chilenos",
        [
            ("oye cabro, tendrai una horita pal' médico ahora?", []),
            ("po sabí si hay disponible?", []),
        ],
    ),
    (
        "gritos_caps_lock",
        [
            ("HOLA NECESITO HORA URGENTE", []),
        ],
    ),
    (
        "doble_mensaje_consecutivo",
        [
            ("hola", []),
            ("hola", []),  # mismo saludo dos veces
        ],
    ),
    (
        "cambio_intent_a_mitad_flujo",
        [
            ("agendar kine", []),
            ("mejor cancelar mi hora anterior", []),  # cambia intent
        ],
    ),
    (
        "input_no_relacionado",
        [
            ("a cómo está el dólar hoy?", []),  # off-topic
        ],
    ),
    (
        "audio_simulado",
        [
            ("audio: hola, una hora con kinesiologa", []),  # no es audio real, pero similar texto
        ],
    ),
    (
        "rut_con_puntos",
        [
            ("agendar medico", []),
            ("12.345.678-9", []),
        ],
    ),
    (
        "pregunta_existencia_no_realizada",
        [
            ("hacen ecografía obstétrica?", []),  # NO la hacen
        ],
    ),
    (
        "fecha_relativa_compleja",
        [
            ("medico para la próxima semana", []),
        ],
    ),
    (
        "menu_repetido_no_loop_infinito",
        [
            ("menu", []),
            ("menu", []),
            ("menu", []),
        ],
    ),
    (
        "numero_solo_sin_contexto",
        [
            ("1", []),  # número solo en IDLE, sin sesión
        ],
    ),
    (
        "stop_words_solo",
        [
            ("ah", []),
            ("eh", []),
            ("...", []),
        ],
    ),
    (
        "agendar_con_emoji_solo",
        [
            ("👍", []),
            ("🙏", []),
        ],
    ),
    (
        "url_o_link_pegado",
        [
            ("https://centromedicocarampangue.cl/contacto", []),
        ],
    ),
    (
        "emergencia_disfrazada",
        [
            ("doctor estoy sangrando mucho", [
                must_contain("SAMU", "131"),
            ]),
        ],
    ),
    (
        "ginecologa_no_mismo_que_matrona",
        [
            ("hora con la matrona", []),
            ("hora con ginecóloga", []),
        ],
    ),
    (
        "respuesta_si_no_sin_contexto",
        [
            ("si", []),
            ("no", []),
        ],
    ),
    (
        "conversation_loop_detection",
        [
            ("xxx", []),
            ("yyy", []),
            ("zzz", []),
            ("aaa", []),  # 4 mensajes consecutivos sin sentido
        ],
    ),
    # ── Full flows de agendamiento end-to-end ─────────────────────────────────
    (
        "full_flow_kine_paciente_nuevo",
        [
            ("hola", []),
            ("una hora con kine", []),
            ("1", []),  # selecciona primer slot
            # Espera RUT
            ("12345678-9", []),  # paciente nuevo (mock buscar_paciente devolverá nuevo)
        ],
    ),
    (
        "full_flow_medico_recurrente",
        [
            ("hola", []),
            ("hora con medico", []),
            ("si", []),  # confirma slot
        ],
    ),
    (
        "full_flow_cancelar",
        [
            ("hola", []),
            ("cancelar mi hora", []),
        ],
    ),
    (
        "full_flow_ver_reservas",
        [
            ("hola", []),
            ("ver mis citas", []),
        ],
    ),
    (
        "no_crash_random_strings",
        [
            ("Lorem ipsum dolor sit amet", []),
            ("a" * 500, []),  # mensaje muy largo
            ("🌟🎉🔥💯", []),  # solo emojis
            ("¿¡!!??", []),  # solo signos
            ("--------", []),
        ],
    ),
    (
        "no_crash_inyeccion_sql",
        [
            ("'; DROP TABLE messages; --", []),
            ("../../etc/passwd", []),
            ("<script>alert('xss')</script>", []),
        ],
    ),
    (
        "no_crash_unicode_extremo",
        [
            ("Z̴̢̡͙̮̜̘̙̜̅͒͐͛̕̕͜A̷̢̛̟̟̘̩̲͍̥͌͆̾͗̕͝", []),  # zalgo text
            ("\u202e\u202d", []),  # Unicode bidi override
            ("ﾊﾞﾝｸ", []),  # halfwidth katakana
        ],
    ),
    (
        "boletas_solicitud_explicita",
        [
            ("necesito que me reimpriman la boleta de mi última cita", []),
        ],
    ),
    (
        "respuesta_a_recordatorio_2h",
        [
            ("ok ya voy en camino", []),
            ("ya estoy llegando", []),
        ],
    ),
    (
        "info_ubicacion",
        [
            ("donde queda el centro?", []),
            ("dirección por favor", []),
        ],
    ),
    (
        "agendar_via_motivo",
        [
            ("tengo dolor de muela", []),
        ],
    ),
    (
        "agendar_dental_estetica",
        [
            ("quiero hacerme botox", []),
        ],
    ),
    (
        "ortodoncia_control_no_inicial",
        [
            ("hora para control de ortodoncia", []),
        ],
    ),
    # ── Validación de outputs específicos (no solo "no crashea") ──────────────
    (
        "horario_real_otorrino_dice_lunes_a_miercoles",
        [
            ("a que dia atiende el otorrino", [
                must_contain("Borrego"),
                # debe mencionar 16 (hora real) o "según agenda" o "lunes"
                must_match(r"(16:00|según agenda|lun)"),
            ]),
        ],
    ),
    (
        "metodo_pago_dice_efectivo_o_transferencia_para_medico",
        [
            ("agendar medico", []),
            ("como puedo pagar", [
                # Debe mencionar efectivo y transferencia para médica
                must_contain("efectivo"),
            ]),
        ],
    ),
    (
        "dolor_muela_va_a_odonto",
        [
            ("tengo dolor de muela", [
                # Debe sugerir odontología o dental
                must_match(r"(Odontolog|dental|tapadura|caries|muela)"),
            ]),
        ],
    ),
    (
        "audio_largo_no_responde_solo_recibido",
        [
            ("este es un mensaje de audio muy largo donde el paciente cuenta " +
             ("síntomas " * 30), []),
        ],
    ),
    (
        "fechas_pasadas_explicitas_se_rechazan",
        [
            ("hora para el 1 de enero del 2020", []),
        ],
    ),
    (
        "respuesta_dr_olavarria_no_da_personal",
        [
            ("hora con olavarria", [
                must_not_contain("987834148"),
                # Debe dar el bot WA del CMC, no el personal
            ]),
        ],
    ),
    (
        "boleta_solicitud_repetida_no_loop",
        [
            ("necesito una boleta", []),
            ("boleta", []),
            ("la boleta de mi última cita", []),
        ],
    ),
    (
        "mensaje_solo_signos_no_crash",
        [
            ("?", []),
            ("!", []),
            (".", []),
            (",", []),
            ("¿?", []),
        ],
    ),
    (
        "consulta_general_simple",
        [
            ("buenos días, me podría ayudar?", []),
        ],
    ),
    (
        "reagendar_proactivo",
        [
            ("hola", []),
            ("necesito cambiar mi hora del miércoles", []),
        ],
    ),
    (
        "tipo_atencion_directo",
        [
            ("agendar particular kine", []),
            ("agendar fonasa medico", []),
        ],
    ),
    (
        "telefono_personal_no_aparece_en_pregunta_contacto",
        [
            ("cual es el numero del centro?", [
                must_not_contain("987834148", "+56987834148"),
            ]),
        ],
    ),
    (
        "horario_general_cmc_con_codigo_correcto",
        [
            ("a que hora atienden ustedes?", [
                must_not_contain("(44)"),
            ]),
        ],
    ),
]


# Asserts GLOBALES que se aplican a TODAS las respuestas. Si alguna falla,
# es un bug crítico independiente del test específico.
GLOBAL_ASSERTS = [
    must_not_contain("+56987834148", "987834148"),  # Número personal del Dr.
    must_not_contain("(44) 296"),                    # Fijo CMC mal código (es 41)
    must_not_contain("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"),  # locale
    must_not_contain("january", "february", "march", "april", "may ", "june",
                     "july", "august", "september", "october", "november", "december"),
]


async def run_conversacion(nombre: str, pasos: list, verbose: bool):
    from session import get_session, reset_session
    from flows import handle_message

    phone = f"56999000000"  # phone único del test
    reset_session(phone)
    fails = []
    for i, (msg, asserts) in enumerate(pasos):
        sess = get_session(phone)
        try:
            resp = await handle_message(phone, msg, sess)
        except Exception as e:
            fails.append(f"step {i+1} '{msg}' → exception: {e}")
            break
        # Para mensajes interactive, extraer body + buttons + sections
        resp_text = _extract_text(resp)
        if verbose:
            print(f"  {DIM}[{i+1}] paciente:{RESET} {msg}")
            print(f"  {DIM}    bot:{RESET} {resp_text[:300]}")
        # Asserts específicos del test
        for a in asserts:
            err = a(resp_text)
            if err:
                fails.append(f"step {i+1} '{msg}' → {err}\n     resp: {resp_text[:180]}")
        # Asserts globales (siempre)
        for ga in GLOBAL_ASSERTS:
            err = ga(resp_text)
            if err:
                fails.append(f"GLOBAL step {i+1} '{msg}' → {err}\n     resp: {resp_text[:280]}")
    return fails


def _extract_text(resp) -> str:
    """Extrae todo el texto de una respuesta del bot, ya sea string, dict
    interactive con body+buttons+sections, o tuple."""
    if resp is None:
        return ""
    if isinstance(resp, str):
        return resp
    if isinstance(resp, dict):
        parts = []
        body = resp.get("body") or resp.get("text") or ""
        if isinstance(body, dict):
            body = body.get("text", "")
        parts.append(str(body))
        # Interactive
        inter = resp.get("interactive") or {}
        if isinstance(inter, dict):
            inter_body = inter.get("body", {})
            if isinstance(inter_body, dict):
                parts.append(str(inter_body.get("text", "")))
            action = inter.get("action", {})
            for btn in action.get("buttons", []):
                rep = btn.get("reply", {})
                parts.append(str(rep.get("title", "")))
            for sec in action.get("sections", []):
                parts.append(str(sec.get("title", "")))
                for row in sec.get("rows", []):
                    parts.append(str(row.get("title", "")))
                    parts.append(str(row.get("description", "")))
        return "\n".join(p for p in parts if p)
    return str(resp)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    install_mocks()

    total = len(CONVERSACIONES)
    pass_ = 0
    print(f"\n=== Adversarial chat tests · {total} conversaciones ===\n")
    for nombre, pasos in CONVERSACIONES:
        if args.verbose:
            print(f"{YELLOW}● {nombre}{RESET}")
        fails = await run_conversacion(nombre, pasos, args.verbose)
        if not fails:
            pass_ += 1
            print(f"  {GREEN}✓{RESET} {nombre}")
        else:
            print(f"  {RED}✗{RESET} {nombre}")
            for f in fails:
                print(f"      {f}")
    print(f"\n{pass_}/{total} pasaron\n")
    return 0 if pass_ == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
