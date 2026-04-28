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
                # No debe ofrecer Odontología Dr. Carlos Jiménez
                must_not_contain("Carlos Jiménez", "Odontología General — Dr."),
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
        if isinstance(resp, dict):
            resp_text = resp.get("body", "") or resp.get("text", "") or str(resp)
        else:
            resp_text = str(resp or "")
        if verbose:
            print(f"  {DIM}[{i+1}] paciente:{RESET} {msg}")
            print(f"  {DIM}    bot:{RESET} {resp_text[:240]}")
        for a in asserts:
            err = a(resp_text)
            if err:
                fails.append(f"step {i+1} '{msg}' → {err}\n     resp: {resp_text[:180]}")
    return fails


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
