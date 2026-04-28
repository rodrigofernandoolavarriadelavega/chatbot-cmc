"""
Harness de 200 tests AGENDA — flujo de agendamiento end-to-end.

Cubre en profundidad el flujo AGENDAR del chatbot CMC:
- Todos los 24 profesionales y 19 especialidades
- Expansión progresiva Medicina General (stages 0→1→2→3)
- Navegación WAIT_SLOT (ver todos, otro día, otro profesional, día específico)
- Masoterapia con duración variable
- Selección Fonasa / Particular (WAIT_MODALIDAD)
- Identificación por RUT (paciente existente y nuevo)
- Registro completo de paciente nuevo (5 pasos opcionales)
- Confirmación / rechazo en CONFIRMING_CITA
- FAQ-to-agendar flow
- Motivos rápidos del menú
- Sin disponibilidad → oferta lista de espera
- Edge cases (RUT inválido, cambio de intención mid-flow, frustración)

Ejecución:
    PYTHONPATH=app:. python3 tests/harness_agenda_200.py

No toca producción, no llama a Medilink real ni a Claude real ni a WhatsApp.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT))

TMP_DB_DIR = Path(tempfile.mkdtemp(prefix="cmc_agenda_"))
TMP_DB = TMP_DB_DIR / "test_sessions.db"
os.environ["SESSIONS_DB"] = str(TMP_DB)

import session  # noqa: E402
session.DB_PATH = TMP_DB
(TMP_DB.parent).mkdir(parents=True, exist_ok=True)

# ── Fake Medilink ────────────────────────────────────────────────────────────
FAKE_FAIL_CREAR_CITA = {"value": False}
FAKE_FAIL_CANCELAR_CITA = {"value": False}
FAKE_SIN_SLOTS = {"value": False}
FAKE_CITAS_PACIENTE: list[dict] = []

async def fake_buscar_paciente(rut: str):
    rut_clean = rut.replace(".", "").replace("-", "").strip().upper()
    if rut_clean.startswith("11111111"):
        return {"id": 100, "nombre": "Juan Prueba Test", "rut": "11111111-1"}
    if rut_clean.startswith("22222222"):
        return {"id": 200, "nombre": "María Multi Citas", "rut": "22222222-2"}
    if rut_clean.startswith("33333333"):
        return {"id": 300, "nombre": "Pedro Agenda López", "rut": "33333333-3",
                "fecha_nacimiento": "1990-03-15", "sexo": "M"}
    return None

async def fake_crear_paciente(rut: str, nombre: str, apellidos: str, **kwargs):
    return {"id": 999, "nombre": f"{nombre} {apellidos}".strip(), "rut": rut}

async def fake_crear_cita(id_paciente, id_profesional, fecha, hora_inicio, hora_fin, id_recurso=1):
    if FAKE_FAIL_CREAR_CITA["value"]:
        return None
    return {"id": 5555}

async def fake_cancelar_cita(id_cita):
    if FAKE_FAIL_CANCELAR_CITA["value"]:
        return False
    return True

async def fake_listar_citas_paciente(id_paciente: int = 0, **kwargs):
    return list(FAKE_CITAS_PACIENTE)

def _fake_slots(esp_display: str, id_prof: int, prof_nombre: str):
    base_fecha = "2026-04-15"
    base_display = "mié 15 abr"
    slots = []
    for h in ["09:00", "09:15", "09:30", "10:00", "10:30"]:
        end_min = int(h.split(":")[0]) * 60 + int(h.split(":")[1]) + 15
        hf = f"{end_min//60:02d}:{end_min%60:02d}"
        slots.append({
            "profesional":    prof_nombre,
            "especialidad":   esp_display,
            "fecha":          base_fecha,
            "fecha_display":  base_display,
            "hora_inicio":    h,
            "hora_fin":       hf,
            "id_profesional": id_prof,
            "id_recurso":     1,
            "duracion":       15,
        })
    return slots

async def fake_buscar_primer_dia(especialidad: str, dias_adelante: int = 60,
                                  excluir=None, intervalo_override=None, solo_ids=None):
    if FAKE_SIN_SLOTS["value"]:
        return [], []
    esp = (especialidad or "").lower()
    if solo_ids:
        id_prof = int(solo_ids[0])
        from medilink import PROFESIONALES
        prof = PROFESIONALES.get(id_prof, {})
        slots = _fake_slots(prof.get("especialidad", "Medicina General"),
                            id_prof=id_prof,
                            prof_nombre=prof.get("nombre", "Dr. Test"))
    elif "masoterapia" in esp or "masaje" in esp:
        slots = _fake_slots("Masoterapia", 59, "Paola Acosta")
    elif "odontolog" in esp or "dentista" in esp:
        slots = _fake_slots("Odontología General", 55, "Dra. Javiera Burgos")
    elif "ortodoncia" in esp or "castillo" in esp:
        slots = _fake_slots("Ortodoncia", 66, "Dra. Daniela Castillo")
    elif "kine" in esp:
        slots = _fake_slots("Kinesiología", 77, "Luis Armijo")
    elif "olavarr" in esp:
        slots = _fake_slots("Medicina General", 1, "Dr. Rodrigo Olavarría")
    elif "abarca" in esp:
        slots = _fake_slots("Medicina General", 73, "Dr. Andrés Abarca")
    elif "psicolog" in esp:
        slots = _fake_slots("Psicología Adulto", 74, "Jorge Montalba")
    elif "nutri" in esp:
        slots = _fake_slots("Nutrición", 52, "Gisela Pinto")
    elif "fonoaudiolog" in esp or "fono" in esp:
        slots = _fake_slots("Fonoaudiología", 70, "Juana Arratia")
    elif "podolog" in esp:
        slots = _fake_slots("Podología", 56, "Andrea Guevara")
    elif "traumatolog" in esp:
        slots = _fake_slots("Traumatología", 64, "Dr. Claudio Barraza")
    elif "cardiolog" in esp or "cardio" in esp:
        slots = _fake_slots("Cardiología", 60, "Dr. Miguel Millán")
    elif "gastro" in esp:
        slots = _fake_slots("Gastroenterología", 65, "Dr. Nicolás Quijano")
    elif "otorrino" in esp or "orl" in esp:
        slots = _fake_slots("Otorrinolaringología", 23, "Dr. Manuel Borrego")
    elif "ginecolog" in esp or "gine" in esp:
        slots = _fake_slots("Ginecología", 61, "Dr. Tirso Rejón")
    elif "matrona" in esp:
        slots = _fake_slots("Matrona", 67, "Sarai Gómez")
    elif "endodoncia" in esp:
        slots = _fake_slots("Endodoncia", 75, "Dr. Fernando Fredes")
    elif "implant" in esp:
        slots = _fake_slots("Implantología", 69, "Dra. Aurora Valdés")
    elif "est" in esp and "facial" in esp:
        slots = _fake_slots("Estética Facial", 76, "Dra. Valentina Fuentealba")
    elif "ecograf" in esp:
        slots = _fake_slots("Ecografía", 68, "David Pardo")
    elif "etcheverry" in esp or "leo" in esp:
        slots = _fake_slots("Kinesiología", 21, "Leonardo Etcheverry")
    elif "armijo" in esp:
        slots = _fake_slots("Kinesiología", 77, "Luis Armijo")
    elif "marquez" in esp or "márquez" in esp or "medicina familiar" in esp:
        slots = _fake_slots("Medicina General", 13, "Dr. Alonso Márquez")
    else:
        slots = _fake_slots("Medicina General", 73, "Dr. Andrés Abarca")
    return slots[:5], slots

async def fake_buscar_slots_dia(especialidad: str, fecha: str, **kwargs):
    if FAKE_SIN_SLOTS["value"]:
        return [], []
    return await fake_buscar_primer_dia(especialidad)

async def fake_buscar_slots_dia_por_ids(ids, fecha, **kwargs):
    if FAKE_SIN_SLOTS["value"]:
        return [], []
    from medilink import PROFESIONALES
    id_prof = int(ids[0]) if ids else 1
    prof = PROFESIONALES.get(id_prof, {})
    slots = _fake_slots(prof.get("especialidad", "Test"),
                        id_prof=id_prof,
                        prof_nombre=prof.get("nombre", "Dr. Test"))
    return slots, slots

async def fake_consultar_proxima_fecha(especialidad: str):
    return "2026-04-15"

# ── Fake Claude ──────────────────────────────────────────────────────────────
def _intent_from_text(m: str) -> dict:
    t = m.lower().strip()
    esp = None

    # Specialty detection (comprehensive)
    if any(w in t for w in ["medicina general", "medico", "médico", "doctor general", "medico general"]):
        esp = "medicina general"
    elif any(w in t for w in ["odontolog", "dentista", "diente", "muela"]):
        esp = "odontología"
    elif "ortodoncia" in t or "brackets" in t or "frenillos" in t:
        esp = "ortodoncia"
    elif "endodoncia" in t or "conducto" in t:
        esp = "endodoncia"
    elif "implant" in t:
        esp = "implantología"
    elif any(w in t for w in ["kine", "kinesiol"]):
        esp = "kinesiología"
    elif "maso" in t or "masaje" in t:
        esp = "masoterapia"
    elif "olavarr" in t:
        esp = "olavarría"
    elif "abarca" in t:
        esp = "abarca"
    elif "etcheverry" in t or "leonardo" in t:
        esp = "etcheverry"
    elif "armijo" in t:
        esp = "armijo"
    elif any(w in t for w in ["psico", "psicologo", "psicóloga", "psicología"]):
        esp = "psicología"
    elif any(w in t for w in ["nutri", "nutricion", "nutrición", "nutricionista"]):
        esp = "nutrición"
    elif any(w in t for w in ["fonoaudiolog", "fono"]):
        esp = "fonoaudiología"
    elif any(w in t for w in ["podolog", "podóloga", "podólogo"]):
        esp = "podología"
    elif any(w in t for w in ["traumatolog", "traumatologo", "traumatólogo"]):
        esp = "traumatología"
    elif any(w in t for w in ["cardiolog", "cardiologo", "cardiólogo", "cardio", "corazon", "corazón"]):
        esp = "cardiología"
    elif any(w in t for w in ["gastro", "gastroenterolog"]):
        esp = "gastroenterología"
    elif any(w in t for w in ["otorrino", "orl"]):
        esp = "otorrinolaringología"
    elif any(w in t for w in ["ginecolog", "ginecólogo", "gine"]):
        esp = "ginecología"
    elif "matrona" in t:
        esp = "matrona"
    elif any(w in t for w in ["ecograf", "ecografía", "ecografia"]):
        esp = "ecografía"
    elif any(w in t for w in ["estética", "estetica", "botox", "relleno"]):
        esp = "estética facial"
    elif "marquez" in t or "márquez" in t:
        esp = "medicina familiar"
    elif "castillo" in t:
        esp = "ortodoncia"
    elif "borrego" in t:
        esp = "otorrinolaringología"
    elif "barraza" in t:
        esp = "traumatología"
    elif "millan" in t or "millán" in t:
        esp = "cardiología"
    elif "quijano" in t:
        esp = "gastroenterología"
    elif "rejon" in t or "rejón" in t or "tirso" in t:
        esp = "ginecología"
    elif "burgos" in t:
        esp = "odontología"
    elif "jimenez" in t or "jiménez" in t:
        esp = "odontología"
    elif "fredes" in t:
        esp = "endodoncia"
    elif "valdés" in t or "valdes" in t:
        esp = "implantología"
    elif "fuentealba" in t:
        esp = "estética facial"
    elif "acosta" in t or "paola" in t:
        esp = "masoterapia"
    elif "pinto" in t or "gisela" in t:
        esp = "nutrición"
    elif "montalba" in t:
        esp = "psicología"
    elif "rodriguez" in t or "rodríguez" in t or "juan pablo" in t:
        esp = "psicología"
    elif "arratia" in t or "juana" in t:
        esp = "fonoaudiología"
    elif "gomez" in t or "gómez" in t or "sarai" in t or "saraí" in t:
        esp = "matrona"
    elif "guevara" in t or "andrea" in t:
        esp = "podología"
    elif "pardo" in t or "david" in t:
        esp = "ecografía"

    # Coloquial symptom → specialty mapping
    if not esp:
        if any(w in t for w in ["guata", "estomago", "estómago", "empacho", "acidez", "reflujo"]):
            esp = "medicina general"
        elif any(w in t for w in ["rodilla", "tobillo", "torci", "esguince", "fractura"]):
            esp = "traumatología"
        elif any(w in t for w in ["espalda", "lumbago", "contractura", "dolor muscular"]):
            esp = "kinesiología"
        elif any(w in t for w in ["no habla bien", "lenguaje", "habla mal", "tartamude"]):
            esp = "fonoaudiología"
        elif any(w in t for w in ["bajar de peso", "dieta", "alimentacion", "alimentación"]):
            esp = "nutrición"
        elif any(w in t for w in ["estresado", "estrés", "estres", "ansiedad", "angustia", "depresion", "depresión"]):
            esp = "psicología"
        elif any(w in t for w in ["garganta", "sinusitis", "oido", "oído", "nariz"]):
            esp = "otorrinolaringología"
        elif any(w in t for w in ["peeling", "arrugas", "rejuvene"]):
            esp = "estética facial"
        elif any(w in t for w in ["uña encarnada", "callos", "hongos en las uñas", "verruga"]):
            esp = "podología"
        elif any(w in t for w in ["embarazo", "pap", "regla", "menstrua"]):
            esp = "matrona"
        elif any(w in t for w in ["presion alta", "presión alta", "hipertension", "hipertensión", "palpitacion", "palpitación"]):
            esp = "cardiología"

    # Intent detection
    if any(w in t for w in ["reagend", "cambiar mi hora", "mover mi hora", "reprograma"]):
        return {"intent": "reagendar", "especialidad": esp, "respuesta_directa": None}
    if any(w in t for w in ["lista de espera", "avísame", "avisame", "cupo cuando"]):
        return {"intent": "waitlist", "especialidad": esp, "respuesta_directa": None}
    if any(w in t for w in ["cancelar", "anular", "borrar mi hora", "elimina mi hora"]):
        return {"intent": "cancelar", "especialidad": esp, "respuesta_directa": None}
    if any(w in t for w in ["mis horas", "mis citas", "ver mis", "que tengo agendado"]):
        return {"intent": "ver_reservas", "especialidad": esp, "respuesta_directa": None}
    if any(w in t for w in ["agendar", "agéndame", "reservar", "necesito una hora", "quiero una hora",
                             "quiero hora", "necesito hora", "necesito ver"]):
        return {"intent": "agendar", "especialidad": esp, "respuesta_directa": None}

    # FAQ triggers
    if "tapadura" in t:
        return {"intent": "info", "especialidad": "odontología",
                "respuesta_directa": "Una tapadura (obturación) rellena una caries. Valor: $25.000 a $40.000."}
    if "blanqueamiento" in t or "dientes amarillos" in t:
        return {"intent": "info", "especialidad": "odontología",
                "respuesta_directa": "El blanqueamiento aclara los dientes. Valor: $75.000 en Odontología General."}
    if "botox" in t or "toxina" in t:
        return {"intent": "info", "especialidad": "estética facial",
                "respuesta_directa": "Toxina botulínica para suavizar arrugas. $159.990 con Estética Facial."}
    if any(w in t for w in ["cuesta", "precio", "valor", "cuanto sale", "cuánto sale", "cuánto cuesta"]):
        return {"intent": "precio", "especialidad": esp, "respuesta_directa": f"El valor de la consulta depende de la especialidad."}
    if any(w in t for w in ["recepcion", "recepción", "hablar con alguien", "humano", "persona"]):
        return {"intent": "humano", "especialidad": esp, "respuesta_directa": None}

    # If specialty detected but no clear intent, default to agendar
    if esp:
        return {"intent": "agendar", "especialidad": esp, "respuesta_directa": None}

    return {"intent": "otro", "especialidad": esp, "respuesta_directa": None}

async def fake_detect_intent(mensaje: str):
    return _intent_from_text(mensaje)

async def fake_respuesta_faq(mensaje: str):
    t = mensaje.lower()
    if "tapadura" in t or "obturación" in t:
        return "Una tapadura (obturación) rellena una caries. Valor: $25.000 a $40.000."
    if "blanqueamiento" in t:
        return "El blanqueamiento aclara los dientes. Valor: $75.000 en Odontología General."
    if "botox" in t:
        return "Toxina botulínica $159.990 con Estética Facial."
    if "fonasa" in t:
        return "Sí, atendemos pacientes Fonasa en todas nuestras especialidades."
    if "precio" in t or "cuesta" in t or "valor" in t:
        return "El valor depende de la especialidad. Consulta en recepción al +56 9 8783 4148."
    return "Esa consulta requiere más información. Llama a recepción al +56 9 8783 4148."

async def fake_clasificar_respuesta_seguimiento(mensaje: str):
    t = mensaje.lower()
    if "peor" in t or "mal" in t:
        return "peor"
    if "mejor" in t or "bien" in t:
        return "mejor"
    return "igual"

# ── Aplicar monkey-patches ───────────────────────────────────────────────────
import medilink  # noqa: E402
import claude_helper  # noqa: E402
import flows  # noqa: E402

for mod in (medilink, flows):
    mod.buscar_paciente = fake_buscar_paciente
    mod.crear_paciente = fake_crear_paciente
    mod.crear_cita = fake_crear_cita
    mod.cancelar_cita = fake_cancelar_cita
    mod.listar_citas_paciente = fake_listar_citas_paciente
    mod.buscar_primer_dia = fake_buscar_primer_dia
    mod.buscar_slots_dia = fake_buscar_slots_dia
    mod.buscar_slots_dia_por_ids = fake_buscar_slots_dia_por_ids
    mod.consultar_proxima_fecha = fake_consultar_proxima_fecha

claude_helper.detect_intent = fake_detect_intent
claude_helper.respuesta_faq = fake_respuesta_faq
claude_helper.clasificar_respuesta_seguimiento = fake_clasificar_respuesta_seguimiento
flows.detect_intent = fake_detect_intent
flows.respuesta_faq = fake_respuesta_faq
flows.clasificar_respuesta_seguimiento = fake_clasificar_respuesta_seguimiento

import resilience  # noqa: E402
resilience.is_medilink_down = lambda: False
flows.is_medilink_down = lambda: False

# Mock send_whatsapp para evitar llamadas HTTP reales
async def fake_send_whatsapp(to, body):
    pass

import messaging  # noqa: E402
messaging.send_whatsapp = fake_send_whatsapp
flows.send_whatsapp = fake_send_whatsapp

# ── Harness ──────────────────────────────────────────────────────────────────
from session import get_session, reset_session, save_profile, log_event  # noqa: E402

BUGS: list[dict] = []

def _normalize(resp: Any) -> str:
    if resp is None:
        return ""
    if isinstance(resp, str):
        return resp
    if not isinstance(resp, dict):
        return str(resp)
    parts: list[str] = []
    if isinstance(resp.get("text"), str):
        parts.append(resp["text"])
    if isinstance(resp.get("body"), str):
        parts.append(resp["body"])
    interactive = resp.get("interactive") or {}
    for k in ("body", "footer", "header"):
        t = (interactive.get(k) or {}).get("text", "")
        if t:
            parts.append(str(t))
    action = interactive.get("action") or {}
    for b in action.get("buttons", []) or []:
        reply = b.get("reply") or {}
        parts.append(str(reply.get("title", "")))
        parts.append(str(reply.get("id", "")))
    for sec in action.get("sections", []) or []:
        parts.append(str(sec.get("title", "")))
        for row in sec.get("rows", []) or []:
            parts.append(str(row.get("title", "")))
            parts.append(str(row.get("description", "")))
            parts.append(str(row.get("id", "")))
    return " | ".join(p for p in parts if p)

NO_ENTENDI_MARKERS = [
    "no te entendí", "no te entendi", "elige un número entre",
    "no entendí tu respuesta",
]

async def run_convo(name: str, phone: str, steps: list[tuple[str, Any]],
                    setup: Callable[[], None] | None = None):
    reset_session(phone)
    FAKE_FAIL_CREAR_CITA["value"] = False
    FAKE_FAIL_CANCELAR_CITA["value"] = False
    FAKE_SIN_SLOTS["value"] = False
    FAKE_CITAS_PACIENTE.clear()
    if setup:
        setup()

    transcript: list[tuple[str, str]] = []
    for i, (user_input, expected) in enumerate(steps):
        try:
            sess = get_session(phone)
            resp = await flows.handle_message(phone, user_input, sess)
            txt = _normalize(resp)
            transcript.append((user_input, txt))
        except Exception as e:
            BUGS.append({
                "test": name, "step": i + 1, "input": user_input,
                "error": f"EXCEPTION {type(e).__name__}: {e}",
                "traceback": traceback.format_exc(),
                "transcript": list(transcript),
            })
            return False

        if not txt.strip():
            BUGS.append({
                "test": name, "step": i + 1, "input": user_input,
                "error": "EMPTY response",
                "transcript": list(transcript),
            })
            return False

        low = txt.lower()

        if expected is None:
            continue
        if isinstance(expected, list):
            expected = {"any": expected}
        if not isinstance(expected, dict):
            continue

        if "any" in expected:
            if not any(sub.lower() in low for sub in expected["any"]):
                BUGS.append({
                    "test": name, "step": i + 1, "input": user_input,
                    "error": f"missing any of: {expected['any']}",
                    "got": txt[:400], "transcript": list(transcript),
                })
                return False
        if "all" in expected:
            missing = [s for s in expected["all"] if s.lower() not in low]
            if missing:
                BUGS.append({
                    "test": name, "step": i + 1, "input": user_input,
                    "error": f"missing all of: {missing}",
                    "got": txt[:400], "transcript": list(transcript),
                })
                return False
        if "none" in expected:
            matched = [s for s in expected["none"] if s.lower() in low]
            if matched:
                BUGS.append({
                    "test": name, "step": i + 1, "input": user_input,
                    "error": f"unexpected present: {matched}",
                    "got": txt[:400], "transcript": list(transcript),
                })
                return False
    return True


# ── Definición de escenarios ─────────────────────────────────────────────────
async def main():
    results: list[tuple] = []

    def mk(name, phone, steps, setup=None):
        results.append((name, phone, steps, setup))

    NO_ERROR = {"none": NO_ENTENDI_MARKERS}

    # ═══════════════════════════════════════════════════════════════════════════
    # BLOQUE 1: FULL FLOW POR PROFESIONAL — agendar→slot→modalidad→RUT→confirm
    # (tests 001-024, 24 tests — todos los 24 profesionales del CMC)
    # ═══════════════════════════════════════════════════════════════════════════

    mk("001 full Dr. Olavarría", "56920000001", [
        ("quiero hora con el dr olavarria", {"any": ["Olavarría", "09:"], **NO_ERROR}),
        ("confirmar_sugerido", {"any": ["Fonasa", "Particular"], **NO_ERROR}),
        ("1", {"any": ["rut", "RUT"], **NO_ERROR}),
        ("11111111-1", {"any": ["Juan", "confirm", "Olavarría"], **NO_ERROR}),
        ("confirmar", {"any": ["reserv", "✅", "Listo"], **NO_ERROR}),
    ])

    mk("002 full Dr. Abarca", "56920000002", [
        ("necesito hora con el doctor abarca", {"any": ["Abarca", "09:"], **NO_ERROR}),
        ("confirmar_sugerido", {"any": ["Fonasa"], **NO_ERROR}),
        ("1", {"any": ["rut"], **NO_ERROR}),
        ("11111111-1", {"any": ["confirm"], **NO_ERROR}),
        ("si", {"any": ["reserv", "✅"], **NO_ERROR}),
    ])

    mk("003 full Dr. Márquez", "56920000003", [
        ("quiero hora con el dr marquez", {"any": ["Márquez", "menú", "menu", "Medicina"], **NO_ERROR}),
    ])

    mk("004 full Dr. Borrego (ORL)", "56920000004", [
        ("hora con borrego", {"any": ["Borrego", "09:"], **NO_ERROR}),
        ("confirmar_sugerido", {"any": ["Fonasa", "Particular"], **NO_ERROR}),
        ("2", {"any": ["rut"], **NO_ERROR}),
        ("11111111-1", {"any": ["confirm"], **NO_ERROR}),
        ("dale", {"any": ["reserv", "✅"], **NO_ERROR}),
    ])

    mk("005 full Dr. Millán (cardio)", "56920000005", [
        ("hora con el dr millan", {"any": ["Millán", "09:"], **NO_ERROR}),
        ("confirmar_sugerido", {"any": ["Fonasa"], **NO_ERROR}),
        ("1", {"any": ["rut"], **NO_ERROR}),
        ("11111111-1", {"any": ["confirm"], **NO_ERROR}),
        ("confirmar", {"any": ["reserv", "✅"], **NO_ERROR}),
    ])

    mk("006 full Dr. Barraza (traumato)", "56920000006", [
        ("necesito hora con barraza", {"any": ["Barraza", "09:"], **NO_ERROR}),
        ("confirmar_sugerido", {"any": ["Fonasa"], **NO_ERROR}),
        ("1", {"any": ["rut"], **NO_ERROR}),
        ("11111111-1", {"any": ["confirm"], **NO_ERROR}),
        ("ok", {"any": ["reserv", "✅"], **NO_ERROR}),
    ])

    mk("007 full Dr. Rejón (gineco)", "56920000007", [
        ("hora con el dr tirso", {"any": ["Rejón", "09:"], **NO_ERROR}),
        ("confirmar_sugerido", None),
        ("1", {"any": ["rut"], **NO_ERROR}),
        ("11111111-1", {"any": ["confirm"], **NO_ERROR}),
        ("ya", {"any": ["reserv", "✅"], **NO_ERROR}),
    ])

    mk("008 full Dr. Quijano (gastro)", "56920000008", [
        ("hora con quijano", {"any": ["Quijano", "09:"], **NO_ERROR}),
        ("confirmar_sugerido", None),
        ("1", {"any": ["rut"], **NO_ERROR}),
        ("11111111-1", {"any": ["confirm"], **NO_ERROR}),
        ("claro", {"any": ["reserv", "✅"], **NO_ERROR}),
    ])

    mk("009 full Dra. Burgos (odonto)", "56920000009", [
        ("hora con la dra burgos", {"any": ["Burgos", "09:"], **NO_ERROR}),
        ("confirmar_sugerido", None),
        ("1", {"any": ["rut"], **NO_ERROR}),
        ("11111111-1", {"any": ["confirm"], **NO_ERROR}),
        ("bueno", {"any": ["reserv", "✅"], **NO_ERROR}),
    ])

    mk("010 full Dr. Jiménez (odonto)", "56920000010", [
        ("necesito hora con jimenez", {"any": ["Jiménez", "09:"], **NO_ERROR}),
        ("confirmar_sugerido", None),
        ("2", {"any": ["rut"], **NO_ERROR}),
        ("11111111-1", {"any": ["confirm"], **NO_ERROR}),
        ("si", {"any": ["reserv", "✅"], **NO_ERROR}),
    ])

    mk("011 full Dra. Castillo (ortodoncia)", "56920000011", [
        ("hora con la dra castillo", {"any": ["Castillo", "09:"], **NO_ERROR}),
        ("confirmar_sugerido", None),
        ("1", {"any": ["rut"], **NO_ERROR}),
        ("11111111-1", {"any": ["confirm"], **NO_ERROR}),
        ("si", {"any": ["reserv", "✅"], **NO_ERROR}),
    ])

    mk("012 full Dr. Fredes (endodoncia)", "56920000012", [
        ("hora con el dr fredes", {"any": ["Fredes", "09:"], **NO_ERROR}),
        ("confirmar_sugerido", None),
        ("1", {"any": ["rut"], **NO_ERROR}),
        ("11111111-1", {"any": ["confirm"], **NO_ERROR}),
        ("confirmar", {"any": ["reserv", "✅"], **NO_ERROR}),
    ])

    mk("013 full Dra. Valdés (implanto)", "56920000013", [
        ("hora con la dra valdes", {"any": ["Valdés", "09:"], **NO_ERROR}),
        ("confirmar_sugerido", None),
        ("1", {"any": ["rut"], **NO_ERROR}),
        ("11111111-1", {"any": ["confirm"], **NO_ERROR}),
        ("si", {"any": ["reserv", "✅"], **NO_ERROR}),
    ])

    mk("014 full Dra. Fuentealba (estética)", "56920000014", [
        ("hora con fuentealba", {"any": ["Fuentealba", "09:"], **NO_ERROR}),
        ("confirmar_sugerido", None),
        ("1", {"any": ["rut"], **NO_ERROR}),
        ("11111111-1", {"any": ["confirm"], **NO_ERROR}),
        ("confirmar", {"any": ["reserv", "✅"], **NO_ERROR}),
    ])

    mk("015 full Paola Acosta (masoterapia)", "56920000015", [
        ("hora con paola acosta", ["minutos", "20", "40"]),
        ("maso_40", {"any": ["09:"], **NO_ERROR}),
        ("confirmar_sugerido", None),
        ("1", {"any": ["rut"], **NO_ERROR}),
        ("11111111-1", {"any": ["confirm"], **NO_ERROR}),
        ("si", {"any": ["reserv", "✅"], **NO_ERROR}),
    ])

    mk("016 full Luis Armijo (kine)", "56920000016", [
        ("hora con luis armijo", {"any": ["Armijo", "09:"], **NO_ERROR}),
        ("confirmar_sugerido", None),
        ("1", {"any": ["rut"], **NO_ERROR}),
        ("11111111-1", {"any": ["confirm"], **NO_ERROR}),
        ("si", {"any": ["reserv", "✅"], **NO_ERROR}),
    ])

    mk("017 full Leonardo Etcheverry (kine)", "56920000017", [
        ("hora con leonardo etcheverry", {"any": ["Etcheverry", "09:"], **NO_ERROR}),
        ("confirmar_sugerido", None),
        ("1", {"any": ["rut"], **NO_ERROR}),
        ("11111111-1", {"any": ["confirm"], **NO_ERROR}),
        ("si", {"any": ["reserv", "✅"], **NO_ERROR}),
    ])

    mk("018 full Gisela Pinto (nutri)", "56920000018", [
        ("hora con gisela pinto", {"any": ["Pinto", "09:"], **NO_ERROR}),
        ("confirmar_sugerido", None),
        ("1", {"any": ["rut"], **NO_ERROR}),
        ("11111111-1", {"any": ["confirm"], **NO_ERROR}),
        ("si", {"any": ["reserv", "✅"], **NO_ERROR}),
    ])

    mk("019 full Jorge Montalba (psico)", "56920000019", [
        ("hora con montalba", {"any": ["Montalba", "09:"], **NO_ERROR}),
        ("confirmar_sugerido", None),
        ("1", {"any": ["rut"], **NO_ERROR}),
        ("11111111-1", {"any": ["confirm"], **NO_ERROR}),
        ("si", {"any": ["reserv", "✅"], **NO_ERROR}),
    ])

    mk("020 full JP Rodríguez (psico)", "56920000020", [
        ("hora con juan pablo rodriguez", {"any": ["Rodríguez", "09:"], **NO_ERROR}),
        ("confirmar_sugerido", None),
        ("1", {"any": ["rut"], **NO_ERROR}),
        ("11111111-1", {"any": ["confirm"], **NO_ERROR}),
        ("si", {"any": ["reserv", "✅"], **NO_ERROR}),
    ])

    mk("021 full Juana Arratia (fono)", "56920000021", [
        ("hora con juana arratia", {"any": ["Arratia", "09:"], **NO_ERROR}),
        ("confirmar_sugerido", None),
        ("1", {"any": ["rut"], **NO_ERROR}),
        ("11111111-1", {"any": ["confirm"], **NO_ERROR}),
        ("si", {"any": ["reserv", "✅"], **NO_ERROR}),
    ])

    mk("022 full Sarai Gómez (matrona)", "56920000022", [
        ("hora con sarai gomez", {"any": ["Gómez", "09:"], **NO_ERROR}),
        ("confirmar_sugerido", None),
        ("1", {"any": ["rut"], **NO_ERROR}),
        ("11111111-1", {"any": ["confirm"], **NO_ERROR}),
        ("si", {"any": ["reserv", "✅"], **NO_ERROR}),
    ])

    mk("023 full Andrea Guevara (podología)", "56920000023", [
        ("hora con andrea guevara", {"any": ["Guevara", "09:"], **NO_ERROR}),
        ("confirmar_sugerido", None),
        ("1", {"any": ["rut"], **NO_ERROR}),
        ("11111111-1", {"any": ["confirm"], **NO_ERROR}),
        ("si", {"any": ["reserv", "✅"], **NO_ERROR}),
    ])

    mk("024 full David Pardo (ecografía)", "56920000024", [
        ("hora con david pardo", {"any": ["Pardo", "09:"], **NO_ERROR}),
        ("confirmar_sugerido", None),
        ("1", {"any": ["rut"], **NO_ERROR}),
        ("11111111-1", {"any": ["confirm"], **NO_ERROR}),
        ("si", {"any": ["reserv", "✅"], **NO_ERROR}),
    ])

    # ═══════════════════════════════════════════════════════════════════════════
    # BLOQUE 2: EXPANSIÓN MEDICINA GENERAL — stages 0→1→2→3
    # (tests 025-040, 16 tests)
    # ═══════════════════════════════════════════════════════════════════════════

    mk("025 MG stage 0: slot sugerido", "56920000025", [
        ("quiero agendar medicina general", {"any": ["Medicina", "09:"], **NO_ERROR}),
    ])

    mk("026 MG ver_otros → stage 1 (mismo doctor)", "56920000026", [
        ("quiero agendar medicina general", {"any": ["09:"], **NO_ERROR}),
        ("ver_otros", {"any": ["09:"], **NO_ERROR}),
    ])

    mk("027 MG ver_todos → stage 1→2 (ambos primarios)", "56920000027", [
        ("quiero agendar medicina general", {"any": ["09:"], **NO_ERROR}),
        ("ver_todos", {"any": ["09:"], **NO_ERROR}),
        ("ver_todos", {"any": ["09:"], **NO_ERROR}),
    ])

    mk("028 MG expansión full 0→1→2→3", "56920000028", [
        ("quiero agendar medicina general", {"any": ["09:"], **NO_ERROR}),
        ("ver_otros", {"any": ["09:"], **NO_ERROR}),        # stage 0→1
        ("ver_todos", {"any": ["09:"], **NO_ERROR}),         # stage 1→2
        ("ver_todos", {"any": ["09:"], **NO_ERROR}),         # stage 2→3
    ])

    mk("029 MG otro profesional desde stage 0", "56920000029", [
        ("quiero agendar medicina general", {"any": ["09:"], **NO_ERROR}),
        ("otro_prof", {"any": ["09:"], **NO_ERROR}),
    ])

    mk("030 MG otro día", "56920000030", [
        ("quiero agendar medicina general", {"any": ["09:"], **NO_ERROR}),
        ("otro día", None),
    ])

    mk("031 MG confirmar slot sugerido directo", "56920000031", [
        ("quiero agendar medicina general", {"any": ["09:"], **NO_ERROR}),
        ("confirmar_sugerido", {"any": ["Fonasa", "Particular"], **NO_ERROR}),
    ])

    mk("032 MG confirmar con 'si' libre", "56920000032", [
        ("quiero agendar medicina general", {"any": ["09:"], **NO_ERROR}),
        ("si", {"any": ["Fonasa", "Particular"], **NO_ERROR}),
    ])

    mk("033 MG confirmar con 'dale'", "56920000033", [
        ("quiero agendar medicina general", {"any": ["09:"], **NO_ERROR}),
        ("dale", {"any": ["Fonasa", "Particular"], **NO_ERROR}),
    ])

    mk("034 MG confirmar con 'ok'", "56920000034", [
        ("quiero agendar medicina general", {"any": ["09:"], **NO_ERROR}),
        ("ok", {"any": ["Fonasa", "Particular"], **NO_ERROR}),
    ])

    mk("035 MG 'ver más' en texto libre", "56920000035", [
        ("quiero agendar medicina general", {"any": ["09:"], **NO_ERROR}),
        ("ver más horarios", {"any": ["09:"], **NO_ERROR}),
    ])

    mk("036 MG 'mostrar todos'", "56920000036", [
        ("quiero agendar medicina general", {"any": ["09:"], **NO_ERROR}),
        ("mostrar todos", {"any": ["09:"], **NO_ERROR}),
    ])

    mk("037 MG 'quiero ver los horarios'", "56920000037", [
        ("quiero agendar medicina general", {"any": ["09:"], **NO_ERROR}),
        ("quiero ver los horarios", {"any": ["09:"], **NO_ERROR}),
    ])

    mk("038 MG precio inline en oferta", "56920000038", [
        ("quiero agendar medicina general", {"any": ["$", "Fonasa", "09:"], **NO_ERROR}),
    ])

    mk("039 MG full flow hasta confirmación", "56920000039", [
        ("quiero agendar medicina general", {"any": ["09:"], **NO_ERROR}),
        ("confirmar_sugerido", {"any": ["Fonasa"], **NO_ERROR}),
        ("1", {"any": ["rut"], **NO_ERROR}),
        ("11111111-1", {"any": ["confirm", "Juan"], **NO_ERROR}),
        ("confirmar", {"any": ["reserv", "✅", "Listo"], **NO_ERROR}),
    ])

    mk("040 MG particular full flow", "56920000040", [
        ("quiero agendar medicina general", {"any": ["09:"], **NO_ERROR}),
        ("confirmar_sugerido", {"any": ["Fonasa", "Particular"], **NO_ERROR}),
        ("2", {"any": ["rut"], **NO_ERROR}),
        ("11111111-1", {"any": ["confirm"], **NO_ERROR}),
        ("si", {"any": ["reserv", "✅", "Particular"], **NO_ERROR}),
    ])

    # ═══════════════════════════════════════════════════════════════════════════
    # BLOQUE 3: NAVEGACIÓN WAIT_SLOT — ver todos, otro día, otro prof, día esp.
    # (tests 041-060, 20 tests)
    # ═══════════════════════════════════════════════════════════════════════════

    mk("041 odonto ver todos", "56920000041", [
        ("quiero agendar odontología", {"any": ["09:"], **NO_ERROR}),
        ("ver todos", {"any": ["09:", "10:"], **NO_ERROR}),
    ])

    mk("042 odonto otro profesional (Burgos↔Jiménez)", "56920000042", [
        ("quiero agendar odontología", {"any": ["09:"], **NO_ERROR}),
        ("otro_prof", {"any": ["09:"], **NO_ERROR}),
    ])

    mk("043 odonto otro día", "56920000043", [
        ("quiero agendar odontología", {"any": ["09:"], **NO_ERROR}),
        ("otro día", None),
    ])

    mk("044 kine otro profesional (Armijo↔Etcheverry)", "56920000044", [
        ("quiero agendar kinesiología", {"any": ["09:"], **NO_ERROR}),
        ("otro_prof", {"any": ["09:"], **NO_ERROR}),
    ])

    mk("045 kine otro día", "56920000045", [
        ("quiero agendar kinesiología", {"any": ["09:"], **NO_ERROR}),
        ("otro día", None),
    ])

    mk("046 psico otro profesional", "56920000046", [
        ("quiero agendar psicología", {"any": ["09:"], **NO_ERROR}),
        ("otro_prof", {"any": ["09:"], **NO_ERROR}),
    ])

    mk("047 traumato ver_otros (solo 1 prof)", "56920000047", [
        ("quiero agendar traumatología", {"any": ["09:"], **NO_ERROR}),
        ("ver_otros", {"any": ["09:"], **NO_ERROR}),
    ])

    mk("048 cardio ver todos y elegir slot 2", "56920000048", [
        ("quiero agendar cardiología", {"any": ["09:"], **NO_ERROR}),
        ("ver_todos", {"any": ["09:", "10:"], **NO_ERROR}),
        ("2", {"any": ["Fonasa", "Particular"], **NO_ERROR}),
    ])

    mk("049 gastro elegir slot 1 directo", "56920000049", [
        ("quiero agendar gastroenterología", {"any": ["09:"], **NO_ERROR}),
        ("1", {"any": ["Fonasa", "Particular"], **NO_ERROR}),
    ])

    mk("050 día específico 'para el viernes'", "56920000050", [
        ("quiero agendar odontología", {"any": ["09:"], **NO_ERROR}),
        ("para el viernes", {"any": ["09:"], **NO_ERROR}),
    ])

    mk("051 día específico 'martes'", "56920000051", [
        ("quiero agendar traumatología", {"any": ["09:"], **NO_ERROR}),
        ("hay para el martes", {"any": ["09:"], **NO_ERROR}),
    ])

    mk("052 'no me sirve' → otro día", "56920000052", [
        ("quiero agendar cardiología", {"any": ["09:"], **NO_ERROR}),
        ("no me sirve", None),
    ])

    mk("053 'no puedo' → otro día", "56920000053", [
        ("quiero agendar ginecología", {"any": ["09:"], **NO_ERROR}),
        ("no puedo ese día", None),
    ])

    mk("054 'cambiar día'", "56920000054", [
        ("quiero agendar nutrición", {"any": ["09:"], **NO_ERROR}),
        ("cambiar día", None),
    ])

    mk("055 'siguiente'", "56920000055", [
        ("quiero agendar fonoaudiología", {"any": ["09:"], **NO_ERROR}),
        ("siguiente", None),
    ])

    mk("056 elegir slot 3 de lista expandida", "56920000056", [
        ("quiero agendar kinesiología", {"any": ["09:"], **NO_ERROR}),
        ("ver todos", {"any": ["09:", "10:"], **NO_ERROR}),
        ("3", {"any": ["Fonasa", "Particular"], **NO_ERROR}),
    ])

    mk("057 ORL ver todos y volver a elegir", "56920000057", [
        ("quiero agendar otorrinolaringología", {"any": ["09:"], **NO_ERROR}),
        ("ver todos", {"any": ["09:"], **NO_ERROR}),
        ("1", {"any": ["Fonasa", "Particular"], **NO_ERROR}),
    ])

    mk("058 ecografía confirmar sugerido", "56920000058", [
        ("quiero agendar ecografía", {"any": ["09:"], **NO_ERROR}),
        ("confirmar_sugerido", {"any": ["Fonasa", "Particular"], **NO_ERROR}),
    ])

    mk("059 endodoncia precio en oferta", "56920000059", [
        ("quiero agendar endodoncia", {"any": ["09:", "$", "desde"], **NO_ERROR}),
    ])

    mk("060 implantología precio en oferta", "56920000060", [
        ("quiero agendar implantología", {"any": ["09:", "$", "desde"], **NO_ERROR}),
    ])

    # ═══════════════════════════════════════════════════════════════════════════
    # BLOQUE 4: MASOTERAPIA — duración variable
    # (tests 061-072, 12 tests)
    # ═══════════════════════════════════════════════════════════════════════════

    mk("061 masoterapia pregunta duración", "56920000061", [
        ("quiero agendar masoterapia", ["minutos", "20", "40"]),
    ])

    mk("062 masoterapia botón maso_20", "56920000062", [
        ("quiero agendar masoterapia", ["minutos", "20", "40"]),
        ("maso_20", {"any": ["09:", "Acosta"], **NO_ERROR}),
    ])

    mk("063 masoterapia botón maso_40", "56920000063", [
        ("quiero agendar masoterapia", ["minutos", "20", "40"]),
        ("maso_40", {"any": ["09:", "Acosta"], **NO_ERROR}),
    ])

    mk("064 masoterapia texto '20 minutos'", "56920000064", [
        ("quiero agendar masoterapia", ["minutos", "20", "40"]),
        ("20 minutos", {"any": ["09:", "Acosta"], **NO_ERROR}),
    ])

    mk("065 masoterapia texto '40 minutos'", "56920000065", [
        ("quiero agendar masoterapia", ["minutos", "20", "40"]),
        ("40 minutos", {"any": ["09:", "Acosta"], **NO_ERROR}),
    ])

    mk("066 masoterapia texto '20'", "56920000066", [
        ("quiero agendar masoterapia", ["minutos", "20", "40"]),
        ("20", {"any": ["09:"], **NO_ERROR}),
    ])

    mk("067 masoterapia texto '40'", "56920000067", [
        ("quiero agendar masoterapia", ["minutos", "20", "40"]),
        ("40", {"any": ["09:"], **NO_ERROR}),
    ])

    mk("068 masoterapia inválido '30' → repreguntar", "56920000068", [
        ("quiero agendar masoterapia", ["minutos", "20", "40"]),
        ("30", ["duración", "minutos", "20", "40"]),
    ])

    mk("069 masoterapia inválido texto → repreguntar", "56920000069", [
        ("quiero agendar masoterapia", ["minutos", "20", "40"]),
        ("una hora", ["duración", "minutos", "20", "40"]),
    ])

    mk("070 masoterapia 20 min full flow", "56920000070", [
        ("quiero agendar masoterapia", ["minutos", "20", "40"]),
        ("maso_20", {"any": ["09:"], **NO_ERROR}),
        ("confirmar_sugerido", None),
        ("1", {"any": ["rut"], **NO_ERROR}),
        ("11111111-1", {"any": ["confirm"], **NO_ERROR}),
        ("si", {"any": ["reserv", "✅"], **NO_ERROR}),
    ])

    mk("071 masoterapia 40 min full flow", "56920000071", [
        ("quiero agendar masoterapia", ["minutos", "20", "40"]),
        ("maso_40", {"any": ["09:"], **NO_ERROR}),
        ("confirmar_sugerido", None),
        ("1", {"any": ["rut"], **NO_ERROR}),
        ("11111111-1", {"any": ["confirm"], **NO_ERROR}),
        ("si", {"any": ["reserv", "✅"], **NO_ERROR}),
    ])

    mk("072 masoterapia coloquial 'quiero masaje'", "56920000072", [
        ("quiero hora de masaje relajante", ["minutos", "20", "40"]),
    ])

    # ═══════════════════════════════════════════════════════════════════════════
    # BLOQUE 5: WAIT_MODALIDAD — Fonasa / Particular
    # (tests 073-082, 10 tests)
    # ═══════════════════════════════════════════════════════════════════════════

    mk("073 modalidad Fonasa con '1'", "56920000073", [
        ("quiero agendar traumatología", {"any": ["09:"], **NO_ERROR}),
        ("confirmar_sugerido", {"any": ["Fonasa", "Particular"], **NO_ERROR}),
        ("1", {"any": ["rut", "Fonasa"], **NO_ERROR}),
    ])

    mk("074 modalidad Particular con '2'", "56920000074", [
        ("quiero agendar traumatología", {"any": ["09:"], **NO_ERROR}),
        ("confirmar_sugerido", None),
        ("2", {"any": ["rut", "Particular"], **NO_ERROR}),
    ])

    mk("075 modalidad 'fonasa' texto libre", "56920000075", [
        ("quiero agendar cardiología", {"any": ["09:"], **NO_ERROR}),
        ("confirmar_sugerido", None),
        ("fonasa", {"any": ["rut", "Fonasa"], **NO_ERROR}),
    ])

    mk("076 modalidad 'particular' texto libre", "56920000076", [
        ("quiero agendar cardiología", {"any": ["09:"], **NO_ERROR}),
        ("confirmar_sugerido", None),
        ("particular", {"any": ["rut", "Particular"], **NO_ERROR}),
    ])

    mk("077 modalidad 'privado'", "56920000077", [
        ("quiero agendar gastroenterología", {"any": ["09:"], **NO_ERROR}),
        ("confirmar_sugerido", None),
        ("privado", {"any": ["rut", "Particular"], **NO_ERROR}),
    ])

    mk("078 modalidad 'fona'", "56920000078", [
        ("quiero agendar ginecología", {"any": ["09:"], **NO_ERROR}),
        ("confirmar_sugerido", None),
        ("fona", {"any": ["rut", "Fonasa"], **NO_ERROR}),
    ])

    mk("079 modalidad inválida → repreguntar", "56920000079", [
        ("quiero agendar odontología", {"any": ["09:"], **NO_ERROR}),
        ("confirmar_sugerido", None),
        ("no sé", {"any": ["Fonasa", "Particular"], **NO_ERROR}),
    ])

    mk("080 modalidad inválida 2x → repreguntar", "56920000080", [
        ("quiero agendar odontología", {"any": ["09:"], **NO_ERROR}),
        ("confirmar_sugerido", None),
        ("qué es eso", {"any": ["Fonasa", "Particular"]}),
        ("tampoco", {"any": ["Fonasa", "Particular"]}),
    ])

    mk("081 modalidad con paciente conocido", "56920000081", [
        ("quiero agendar kinesiología", {"any": ["09:"], **NO_ERROR}),
        ("confirmar_sugerido", None),
        ("1", {"any": ["datos anteriores", "continuar", "rut"], **NO_ERROR}),
    ], setup=lambda: save_profile("56920000081", "11111111-1", "Juan Prueba Test"))

    mk("082 paciente conocido confirma con 'si'", "56920000082", [
        ("quiero agendar kinesiología", {"any": ["09:"], **NO_ERROR}),
        ("confirmar_sugerido", None),
        ("1", {"any": ["datos anteriores", "continuar"], **NO_ERROR}),
        ("si", {"any": ["confirm", "Juan"], **NO_ERROR}),
    ], setup=lambda: save_profile("56920000082", "11111111-1", "Juan Prueba Test"))

    # ═══════════════════════════════════════════════════════════════════════════
    # BLOQUE 6: WAIT_RUT_AGENDAR — validación de RUT
    # (tests 083-097, 15 tests)
    # ═══════════════════════════════════════════════════════════════════════════

    mk("083 RUT válido paciente existente", "56920000083", [
        ("quiero agendar nutrición", {"any": ["09:"], **NO_ERROR}),
        ("confirmar_sugerido", None),
        ("1", {"any": ["rut"], **NO_ERROR}),
        ("11111111-1", {"any": ["Juan", "confirm"], **NO_ERROR}),
    ])

    mk("084 RUT con puntos y guión", "56920000084", [
        ("quiero agendar nutrición", {"any": ["09:"], **NO_ERROR}),
        ("confirmar_sugerido", None),
        ("1", {"any": ["rut"], **NO_ERROR}),
        ("11.111.111-1", {"any": ["Juan", "confirm"], **NO_ERROR}),
    ])

    mk("085 RUT sin guión", "56920000085", [
        ("quiero agendar nutrición", {"any": ["09:"], **NO_ERROR}),
        ("confirmar_sugerido", None),
        ("1", {"any": ["rut"], **NO_ERROR}),
        ("111111111", {"any": ["Juan", "confirm"], **NO_ERROR}),
    ])

    mk("086 RUT inválido → repreguntar", "56920000086", [
        ("quiero agendar nutrición", {"any": ["09:"], **NO_ERROR}),
        ("confirmar_sugerido", None),
        ("1", {"any": ["rut"], **NO_ERROR}),
        ("abc", {"any": ["rut", "válido", "formato", "dígito"]}),
    ])

    mk("087 RUT inválido corto → repreguntar", "56920000087", [
        ("quiero agendar nutrición", {"any": ["09:"], **NO_ERROR}),
        ("confirmar_sugerido", None),
        ("1", {"any": ["rut"], **NO_ERROR}),
        ("123", {"any": ["rut", "válido", "formato"]}),
    ])

    mk("088 RUT no encontrado → registro nuevo", "56920000088", [
        ("quiero agendar nutrición", {"any": ["09:"], **NO_ERROR}),
        ("confirmar_sugerido", None),
        ("1", {"any": ["rut"], **NO_ERROR}),
        ("99999999-9", {"any": ["no encontré", "registr", "nombre"], **NO_ERROR}),
    ])

    mk("089 RUT conocido 'sí continuar'", "56920000089", [
        ("quiero agendar odontología", {"any": ["09:"], **NO_ERROR}),
        ("confirmar_sugerido", None),
        ("1", {"any": ["datos anteriores", "continuar"], **NO_ERROR}),
        ("si", {"any": ["confirm", "Juan"], **NO_ERROR}),
    ], setup=lambda: save_profile("56920000089", "11111111-1", "Juan Prueba Test"))

    mk("090 RUT conocido 'rut_nuevo' → pedir RUT", "56920000090", [
        ("quiero agendar odontología", {"any": ["09:"], **NO_ERROR}),
        ("confirmar_sugerido", None),
        ("1", {"any": ["datos anteriores"], **NO_ERROR}),
        ("rut_nuevo", {"any": ["rut", "formato", "válido", "12.345"]}),
    ], setup=lambda: save_profile("56920000090", "11111111-1", "Juan Prueba Test"))

    mk("091 RUT segundo paciente existente", "56920000091", [
        ("quiero agendar psicología", {"any": ["09:"], **NO_ERROR}),
        ("confirmar_sugerido", None),
        ("1", {"any": ["rut"], **NO_ERROR}),
        ("22222222-2", {"any": ["María", "confirm"], **NO_ERROR}),
    ])

    mk("092 RUT tercer paciente existente", "56920000092", [
        ("quiero agendar fonoaudiología", {"any": ["09:"], **NO_ERROR}),
        ("confirmar_sugerido", None),
        ("1", {"any": ["rut"], **NO_ERROR}),
        ("33333333-3", {"any": ["Pedro", "confirm"], **NO_ERROR}),
    ])

    mk("093 RUT inválido → reintento OK", "56920000093", [
        ("quiero agendar matrona", {"any": ["09:"], **NO_ERROR}),
        ("confirmar_sugerido", None),
        ("1", {"any": ["rut"], **NO_ERROR}),
        ("xxx", {"any": ["rut", "formato"]}),
        ("11111111-1", {"any": ["Juan", "confirm"], **NO_ERROR}),
    ])

    mk("094 RUT inválido 2x → reintento OK", "56920000094", [
        ("quiero agendar ecografía", {"any": ["09:"], **NO_ERROR}),
        ("confirmar_sugerido", None),
        ("1", {"any": ["rut"], **NO_ERROR}),
        ("zzz", {"any": ["rut"]}),
        ("123", {"any": ["rut"]}),
        ("11111111-1", {"any": ["confirm"], **NO_ERROR}),
    ])

    mk("095 RUT conocido 'ok' continuar", "56920000095", [
        ("quiero agendar ortodoncia", {"any": ["09:"], **NO_ERROR}),
        ("confirmar_sugerido", None),
        ("1", {"any": ["datos anteriores"], **NO_ERROR}),
        ("ok", {"any": ["confirm", "Juan"], **NO_ERROR}),
    ], setup=lambda: save_profile("56920000095", "11111111-1", "Juan Prueba Test"))

    mk("096 RUT conocido 'dale' continuar", "56920000096", [
        ("quiero agendar estética facial", {"any": ["09:"], **NO_ERROR}),
        ("confirmar_sugerido", None),
        ("1", {"any": ["datos anteriores"], **NO_ERROR}),
        ("dale", {"any": ["confirm"], **NO_ERROR}),
    ], setup=lambda: save_profile("56920000096", "11111111-1", "Juan Prueba Test"))

    mk("097 RUT conocido 'confirmo' continuar", "56920000097", [
        ("quiero agendar podología", {"any": ["09:"], **NO_ERROR}),
        ("confirmar_sugerido", None),
        ("1", {"any": ["datos anteriores"], **NO_ERROR}),
        ("confirmo", {"any": ["confirm"], **NO_ERROR}),
    ], setup=lambda: save_profile("56920000097", "11111111-1", "Juan Prueba Test"))

    # ═══════════════════════════════════════════════════════════════════════════
    # BLOQUE 7: REGISTRO PACIENTE NUEVO — nombre, fecha nac, sexo, comuna, email, referral
    # (tests 098-117, 20 tests)
    # ═══════════════════════════════════════════════════════════════════════════

    mk("098 registro completo todo proporcionado", "56920000098", [
        ("quiero agendar odontología", {"any": ["09:"], **NO_ERROR}),
        ("confirmar_sugerido", None),
        ("1", {"any": ["rut"], **NO_ERROR}),
        ("99999999-9", {"any": ["registr", "nombre"], **NO_ERROR}),
        ("Ana María González López", {"any": ["fecha de nacimiento", "fecha"], **NO_ERROR}),
        ("15/03/1990", {"any": ["sexo"], **NO_ERROR}),
        ("sexo_f", {"any": ["comuna"], **NO_ERROR}),
        ("Arauco", {"any": ["correo", "email"], **NO_ERROR}),
        ("ana@gmail.com", {"any": ["conociste", "conocist"], **NO_ERROR}),
        ("ref_rrss", {"any": ["confirm", "cita"], **NO_ERROR}),
        ("confirmar", {"any": ["reserv", "✅"], **NO_ERROR}),
    ])

    mk("099 registro todo saltado", "56920000099", [
        ("quiero agendar traumatología", {"any": ["09:"], **NO_ERROR}),
        ("confirmar_sugerido", None),
        ("1", {"any": ["rut"], **NO_ERROR}),
        ("99999999-9", {"any": ["registr", "nombre"], **NO_ERROR}),
        ("Pedro López Soto", {"any": ["fecha de nacimiento"], **NO_ERROR}),
        ("saltar", {"any": ["sexo"], **NO_ERROR}),
        ("saltar", {"any": ["comuna"], **NO_ERROR}),
        ("saltar", {"any": ["correo", "email"], **NO_ERROR}),
        ("saltar", {"any": ["conociste", "conocist"], **NO_ERROR}),
        ("ref_rrss", {"any": ["confirm", "cita"], **NO_ERROR}),
        ("confirmar", {"any": ["reserv", "✅"], **NO_ERROR}),
    ])

    mk("100 registro fecha DD-MM-YYYY", "56920000100", [
        ("quiero agendar cardiología", {"any": ["09:"], **NO_ERROR}),
        ("confirmar_sugerido", None),
        ("1", {"any": ["rut"], **NO_ERROR}),
        ("99999999-9", {"any": ["registr"], **NO_ERROR}),
        ("Carlos Ruiz", {"any": ["fecha"], **NO_ERROR}),
        ("15-03-1985", {"any": ["sexo"], **NO_ERROR}),
    ])

    mk("101 registro fecha 8 dígitos pegados", "56920000101", [
        ("quiero agendar gastroenterología", {"any": ["09:"], **NO_ERROR}),
        ("confirmar_sugerido", None),
        ("1", {"any": ["rut"], **NO_ERROR}),
        ("99999999-9", {"any": ["registr"], **NO_ERROR}),
        ("Luis Torres", {"any": ["fecha"], **NO_ERROR}),
        ("15031985", {"any": ["sexo"], **NO_ERROR}),
    ])

    mk("102 registro fecha 'mes escrito'", "56920000102", [
        ("quiero agendar ginecología", {"any": ["09:"], **NO_ERROR}),
        ("confirmar_sugerido", None),
        ("1", {"any": ["rut"], **NO_ERROR}),
        ("99999999-9", {"any": ["registr"], **NO_ERROR}),
        ("María Fernández", {"any": ["fecha"], **NO_ERROR}),
        ("15 de marzo de 1990", {"any": ["sexo"], **NO_ERROR}),
    ])

    mk("103 registro fecha DD/MM/YY corto", "56920000103", [
        ("quiero agendar fonoaudiología", {"any": ["09:"], **NO_ERROR}),
        ("confirmar_sugerido", None),
        ("1", {"any": ["rut"], **NO_ERROR}),
        ("99999999-9", {"any": ["registr"], **NO_ERROR}),
        ("Rosa Muñoz", {"any": ["fecha"], **NO_ERROR}),
        ("15/03/90", {"any": ["sexo"], **NO_ERROR}),
    ])

    mk("104 registro sexo masculino", "56920000104", [
        ("quiero agendar podología", {"any": ["09:"], **NO_ERROR}),
        ("confirmar_sugerido", None),
        ("1", {"any": ["rut"], **NO_ERROR}),
        ("99999999-9", {"any": ["registr"], **NO_ERROR}),
        ("Andrés Soto Vera", {"any": ["fecha"], **NO_ERROR}),
        ("saltar", {"any": ["sexo"], **NO_ERROR}),
        ("sexo_m", {"any": ["comuna"], **NO_ERROR}),
    ])

    mk("105 registro sexo femenino", "56920000105", [
        ("quiero agendar nutrición", {"any": ["09:"], **NO_ERROR}),
        ("confirmar_sugerido", None),
        ("1", {"any": ["rut"], **NO_ERROR}),
        ("99999999-9", {"any": ["registr"], **NO_ERROR}),
        ("Laura Díaz", {"any": ["fecha"], **NO_ERROR}),
        ("saltar", {"any": ["sexo"], **NO_ERROR}),
        ("sexo_f", {"any": ["comuna"], **NO_ERROR}),
    ])

    mk("106 registro sexo saltado", "56920000106", [
        ("quiero agendar psicología", {"any": ["09:"], **NO_ERROR}),
        ("confirmar_sugerido", None),
        ("1", {"any": ["rut"], **NO_ERROR}),
        ("99999999-9", {"any": ["registr"], **NO_ERROR}),
        ("Camilo Rojas", {"any": ["fecha"], **NO_ERROR}),
        ("saltar", {"any": ["sexo"], **NO_ERROR}),
        ("saltar", {"any": ["comuna"], **NO_ERROR}),
    ])

    mk("107 registro email válido", "56920000107", [
        ("quiero agendar ORL", {"any": ["09:"], **NO_ERROR}),
        ("confirmar_sugerido", None),
        ("1", {"any": ["rut"], **NO_ERROR}),
        ("99999999-9", {"any": ["registr"], **NO_ERROR}),
        ("Felipe Mora", {"any": ["fecha"], **NO_ERROR}),
        ("saltar", None),
        ("saltar", None),
        ("saltar", None),
        ("felipe@mail.com", {"any": ["conociste", "conocist"], **NO_ERROR}),
    ])

    mk("108 registro email saltado", "56920000108", [
        ("quiero agendar ecografía", {"any": ["09:"], **NO_ERROR}),
        ("confirmar_sugerido", None),
        ("1", {"any": ["rut"], **NO_ERROR}),
        ("99999999-9", {"any": ["registr"], **NO_ERROR}),
        ("Jorge Vargas", {"any": ["fecha"], **NO_ERROR}),
        ("saltar", None),
        ("saltar", None),
        ("saltar", None),
        ("saltar", {"any": ["conociste", "conocist"], **NO_ERROR}),
    ])

    mk("109 registro referral redes sociales", "56920000109", [
        ("quiero agendar endodoncia", {"any": ["09:"], **NO_ERROR}),
        ("confirmar_sugerido", None),
        ("1", {"any": ["rut"], **NO_ERROR}),
        ("99999999-9", {"any": ["registr"], **NO_ERROR}),
        ("Pablo Herrera", {"any": ["fecha"], **NO_ERROR}),
        ("saltar", None),
        ("saltar", None),
        ("saltar", None),
        ("saltar", {"any": ["conociste", "conocist"], **NO_ERROR}),
        ("ref_rrss", {"any": ["confirm", "cita"], **NO_ERROR}),
    ])

    mk("110 registro referral recomendación", "56920000110", [
        ("quiero agendar implantología", {"any": ["09:"], **NO_ERROR}),
        ("confirmar_sugerido", None),
        ("1", {"any": ["rut"], **NO_ERROR}),
        ("99999999-9", {"any": ["registr"], **NO_ERROR}),
        ("Marta Salinas", {"any": ["fecha"], **NO_ERROR}),
        ("saltar", None),
        ("saltar", None),
        ("saltar", None),
        ("saltar", {"any": ["conociste", "conocist"], **NO_ERROR}),
        ("ref_recomendacion", {"any": ["confirm", "cita"], **NO_ERROR}),
    ])

    mk("111 registro referral saltado", "56920000111", [
        ("quiero agendar matrona", {"any": ["09:"], **NO_ERROR}),
        ("confirmar_sugerido", None),
        ("1", {"any": ["rut"], **NO_ERROR}),
        ("99999999-9", {"any": ["registr"], **NO_ERROR}),
        ("Sofía Pérez", {"any": ["fecha"], **NO_ERROR}),
        ("saltar", None),
        ("saltar", None),
        ("saltar", None),
        ("saltar", {"any": ["conociste", "conocist"], **NO_ERROR}),
        ("saltar", {"any": ["confirm", "cita"], **NO_ERROR}),
    ])

    mk("112 registro nombre solo primero + apellido", "56920000112", [
        ("quiero agendar kinesiología", {"any": ["09:"], **NO_ERROR}),
        ("confirmar_sugerido", None),
        ("1", {"any": ["rut"], **NO_ERROR}),
        ("99999999-9", {"any": ["registr"], **NO_ERROR}),
        ("Catalina Moya", {"any": ["fecha"], **NO_ERROR}),
    ])

    mk("113 registro nombre completo 4 palabras", "56920000113", [
        ("quiero agendar ortodoncia", {"any": ["09:"], **NO_ERROR}),
        ("confirmar_sugerido", None),
        ("1", {"any": ["rut"], **NO_ERROR}),
        ("99999999-9", {"any": ["registr"], **NO_ERROR}),
        ("Ana María López González", {"any": ["fecha"], **NO_ERROR}),
    ])

    mk("114 registro fecha mes abreviado", "56920000114", [
        ("quiero agendar estética facial", {"any": ["09:"], **NO_ERROR}),
        ("confirmar_sugerido", None),
        ("1", {"any": ["rut"], **NO_ERROR}),
        ("99999999-9", {"any": ["registr"], **NO_ERROR}),
        ("Diego Castro", {"any": ["fecha"], **NO_ERROR}),
        ("5 ene 1988", {"any": ["sexo"], **NO_ERROR}),
    ])

    mk("115 registro comuna Carampangue", "56920000115", [
        ("quiero agendar medicina general", {"any": ["09:"], **NO_ERROR}),
        ("confirmar_sugerido", None),
        ("1", {"any": ["rut"], **NO_ERROR}),
        ("99999999-9", {"any": ["registr"], **NO_ERROR}),
        ("Daniela Núñez", {"any": ["fecha"], **NO_ERROR}),
        ("saltar", None),
        ("saltar", None),
        ("Carampangue", {"any": ["correo", "email"], **NO_ERROR}),
    ])

    mk("116 registro comuna Arauco → tag", "56920000116", [
        ("quiero agendar medicina general", {"any": ["09:"], **NO_ERROR}),
        ("confirmar_sugerido", None),
        ("1", {"any": ["rut"], **NO_ERROR}),
        ("99999999-9", {"any": ["registr"], **NO_ERROR}),
        ("Marcela Ortiz", {"any": ["fecha"], **NO_ERROR}),
        ("saltar", None),
        ("saltar", None),
        ("Arauco", {"any": ["correo", "email"], **NO_ERROR}),
    ])

    mk("117 registro completo + confirmar cita", "56920000117", [
        ("quiero agendar odontología", {"any": ["09:"], **NO_ERROR}),
        ("confirmar_sugerido", None),
        ("1", {"any": ["rut"], **NO_ERROR}),
        ("99999999-9", {"any": ["registr"], **NO_ERROR}),
        ("Isabel Fuentes Arriagada", {"any": ["fecha"], **NO_ERROR}),
        ("25/12/1995", {"any": ["sexo"], **NO_ERROR}),
        ("sexo_f", {"any": ["comuna"], **NO_ERROR}),
        ("Lebu", {"any": ["correo", "email"], **NO_ERROR}),
        ("isabel@outlook.com", {"any": ["conociste", "conocist"], **NO_ERROR}),
        ("ref_rrss", {"any": ["confirm", "cita"], **NO_ERROR}),
        ("confirmar", {"any": ["reserv", "✅", "Listo"], **NO_ERROR}),
    ])

    # ═══════════════════════════════════════════════════════════════════════════
    # BLOQUE 8: CONFIRMING_CITA — sí/no/variantes
    # (tests 118-130, 13 tests)
    # ═══════════════════════════════════════════════════════════════════════════

    mk("118 confirmar con 'si'", "56920000118", [
        ("quiero agendar odontología", {"any": ["09:"], **NO_ERROR}),
        ("confirmar_sugerido", None),
        ("1", {"any": ["rut"], **NO_ERROR}),
        ("11111111-1", {"any": ["confirm"], **NO_ERROR}),
        ("si", {"any": ["reserv", "✅"], **NO_ERROR}),
    ])

    mk("119 confirmar con 'sí' con tilde", "56920000119", [
        ("quiero agendar odontología", {"any": ["09:"], **NO_ERROR}),
        ("confirmar_sugerido", None),
        ("1", {"any": ["rut"], **NO_ERROR}),
        ("11111111-1", {"any": ["confirm"], **NO_ERROR}),
        ("sí", {"any": ["reserv", "✅"], **NO_ERROR}),
    ])

    mk("120 confirmar con 'confirmo'", "56920000120", [
        ("quiero agendar kinesiología", {"any": ["09:"], **NO_ERROR}),
        ("confirmar_sugerido", None),
        ("1", {"any": ["rut"], **NO_ERROR}),
        ("11111111-1", {"any": ["confirm"], **NO_ERROR}),
        ("confirmo", {"any": ["reserv", "✅"], **NO_ERROR}),
    ])

    mk("121 confirmar con 'dale'", "56920000121", [
        ("quiero agendar psicología", {"any": ["09:"], **NO_ERROR}),
        ("confirmar_sugerido", None),
        ("1", {"any": ["rut"], **NO_ERROR}),
        ("11111111-1", {"any": ["confirm"], **NO_ERROR}),
        ("dale", {"any": ["reserv", "✅"], **NO_ERROR}),
    ])

    mk("122 confirmar con 'ya'", "56920000122", [
        ("quiero agendar nutrición", {"any": ["09:"], **NO_ERROR}),
        ("confirmar_sugerido", None),
        ("1", {"any": ["rut"], **NO_ERROR}),
        ("11111111-1", {"any": ["confirm"], **NO_ERROR}),
        ("ya", {"any": ["reserv", "✅"], **NO_ERROR}),
    ])

    mk("123 confirmar con 'ok'", "56920000123", [
        ("quiero agendar fonoaudiología", {"any": ["09:"], **NO_ERROR}),
        ("confirmar_sugerido", None),
        ("1", {"any": ["rut"], **NO_ERROR}),
        ("11111111-1", {"any": ["confirm"], **NO_ERROR}),
        ("ok", {"any": ["reserv", "✅"], **NO_ERROR}),
    ])

    mk("124 confirmar con 'bueno'", "56920000124", [
        ("quiero agendar podología", {"any": ["09:"], **NO_ERROR}),
        ("confirmar_sugerido", None),
        ("1", {"any": ["rut"], **NO_ERROR}),
        ("11111111-1", {"any": ["confirm"], **NO_ERROR}),
        ("bueno", {"any": ["reserv", "✅"], **NO_ERROR}),
    ])

    mk("125 rechazar con 'no'", "56920000125", [
        ("quiero agendar ecografía", {"any": ["09:"], **NO_ERROR}),
        ("confirmar_sugerido", None),
        ("1", {"any": ["rut"], **NO_ERROR}),
        ("11111111-1", {"any": ["confirm"], **NO_ERROR}),
        ("no", {"any": ["problema", "otro día", "menu"], **NO_ERROR}),
    ])

    mk("126 rechazar con 'cancelar'", "56920000126", [
        ("quiero agendar matrona", {"any": ["09:"], **NO_ERROR}),
        ("confirmar_sugerido", None),
        ("1", {"any": ["rut"], **NO_ERROR}),
        ("11111111-1", {"any": ["confirm"], **NO_ERROR}),
        ("cancelar", {"any": ["problema", "otro día", "menu"], **NO_ERROR}),
    ])

    mk("127 creación falla → error", "56920000127", [
        ("quiero agendar traumatología", {"any": ["09:"], **NO_ERROR}),
        ("confirmar_sugerido", None),
        ("1", {"any": ["rut"], **NO_ERROR}),
        ("11111111-1", {"any": ["confirm"], **NO_ERROR}),
        ("si", {"any": ["problema", "error", "recepción"]}),
    ], setup=lambda: FAKE_FAIL_CREAR_CITA.update(value=True))

    mk("128 cross-reference ORL → fono", "56920000128", [
        ("hora con borrego", {"any": ["Borrego", "09:"], **NO_ERROR}),
        ("confirmar_sugerido", None),
        ("1", {"any": ["rut"], **NO_ERROR}),
        ("11111111-1", {"any": ["confirm"], **NO_ERROR}),
        ("si", {"any": ["reserv", "✅", "Fonoaudióloga", "fono"], **NO_ERROR}),
    ])

    mk("129 cross-reference fono → ORL", "56920000129", [
        ("hora con juana arratia", {"any": ["Arratia", "09:"], **NO_ERROR}),
        ("confirmar_sugerido", None),
        ("1", {"any": ["rut"], **NO_ERROR}),
        ("11111111-1", {"any": ["confirm"], **NO_ERROR}),
        ("si", {"any": ["reserv", "✅", "Otorrinolaringólogo", "ORL", "otorrino"], **NO_ERROR}),
    ])

    mk("130 confirmar cita muestra dirección", "56920000130", [
        ("quiero agendar cardiología", {"any": ["09:"], **NO_ERROR}),
        ("confirmar_sugerido", None),
        ("1", {"any": ["rut"], **NO_ERROR}),
        ("11111111-1", {"any": ["confirm"], **NO_ERROR}),
        ("si", {"any": ["Monsalve", "Carampangue", "15 minutos antes"], **NO_ERROR}),
    ])

    # ═══════════════════════════════════════════════════════════════════════════
    # BLOQUE 9: FAQ-TO-AGENDAR — info → botón agendar
    # (tests 131-145, 15 tests)
    # ═══════════════════════════════════════════════════════════════════════════

    mk("131 FAQ tapadura → agendar ofrecido", "56920000131", [
        ("quiero una tapadura", {"any": ["tapadura", "agendar", "Agendar"], **NO_ERROR}),
    ])

    mk("132 FAQ tapadura → acepto con botón", "56920000132", [
        ("quiero una tapadura", {"any": ["tapadura", "agendar"], **NO_ERROR}),
        ("agendar_sugerido", {"any": ["09:", "Odonto"], **NO_ERROR}),
    ])

    mk("133 FAQ tapadura → acepto con 'sí'", "56920000133", [
        ("quiero una tapadura", {"any": ["tapadura", "agendar"], **NO_ERROR}),
        ("si", {"any": ["09:", "Odonto"], **NO_ERROR}),
    ])

    mk("134 FAQ tapadura → rechazo", "56920000134", [
        ("quiero una tapadura", {"any": ["tapadura", "agendar"], **NO_ERROR}),
        ("no_agendar", {"any": ["problema", "cuando lo necesites"], **NO_ERROR}),
    ])

    mk("135 FAQ tapadura → rechazo con 'no'", "56920000135", [
        ("quiero una tapadura", {"any": ["tapadura", "agendar"], **NO_ERROR}),
        ("no", {"any": ["problema", "cuando lo necesites"], **NO_ERROR}),
    ])

    mk("136 FAQ botox → agendar estética", "56920000136", [
        ("cuanto cuesta el botox", {"any": ["botox", "toxina", "agendar", "Estética"], **NO_ERROR}),
    ])

    mk("137 FAQ botox → acepto", "56920000137", [
        ("cuanto cuesta el botox", {"any": ["botox", "agendar"], **NO_ERROR}),
        ("agendar_sugerido", {"any": ["09:", "Estética"], **NO_ERROR}),
    ])

    mk("138 FAQ blanqueamiento → agendar odonto", "56920000138", [
        ("quiero un blanqueamiento", {"any": ["blanqueamiento", "agendar"], **NO_ERROR}),
    ])

    mk("139 FAQ blanqueamiento → acepto con '1'", "56920000139", [
        ("quiero un blanqueamiento", {"any": ["blanqueamiento", "agendar"], **NO_ERROR}),
        ("1", {"any": ["09:", "Odonto"], **NO_ERROR}),
    ])

    mk("140 FAQ → otro mensaje limpia sugerencia", "56920000140", [
        ("quiero una tapadura", {"any": ["tapadura", "agendar"], **NO_ERROR}),
        ("quiero hora de kinesiología", {"any": ["Kine", "09:"], **NO_ERROR}),
    ])

    mk("141 FAQ → menu limpia todo", "56920000141", [
        ("quiero una tapadura", {"any": ["tapadura", "agendar"], **NO_ERROR}),
        ("menu", ["Motivos"]),
    ])

    mk("142 FAQ precio genérico con esp → agendar", "56920000142", [
        ("cuanto cuesta la consulta de traumatología", {"any": ["valor", "precio", "depende", "agendar"], **NO_ERROR}),
    ])

    mk("143 FAQ acepto full flow", "56920000143", [
        ("quiero una tapadura", {"any": ["tapadura", "agendar"], **NO_ERROR}),
        ("agendar_sugerido", {"any": ["09:"], **NO_ERROR}),
        ("confirmar_sugerido", None),
        ("1", {"any": ["rut"], **NO_ERROR}),
        ("11111111-1", {"any": ["confirm"], **NO_ERROR}),
        ("si", {"any": ["reserv", "✅"], **NO_ERROR}),
    ])

    mk("144 FAQ tapadura → 'no gracias'", "56920000144", [
        ("quiero una tapadura", {"any": ["tapadura"], **NO_ERROR}),
        ("no gracias", {"any": ["problema", "cuando lo necesites"], **NO_ERROR}),
    ])

    mk("145 FAQ → 'nop' rechazo", "56920000145", [
        ("cuanto cuesta el botox", {"any": ["Toxina", "botulínica", "$159.990"], **NO_ERROR}),
        ("nop", {"any": ["problema", "cuando lo necesites"], **NO_ERROR}),
    ])

    # ═══════════════════════════════════════════════════════════════════════════
    # BLOQUE 10: MOTIVOS RÁPIDOS DEL MENÚ
    # (tests 146-156, 11 tests)
    # ═══════════════════════════════════════════════════════════════════════════

    mk("146 motivo resfrío → MG", "56920000146", [
        ("menu", ["Motivos"]),
        ("motivo_resfrio", {"all": ["Perfecto", "Medicina General", "09:"], **NO_ERROR}),
    ])

    mk("147 motivo kine → Kinesiología", "56920000147", [
        ("menu", ["Motivos"]),
        ("motivo_kine", {"all": ["Perfecto", "Kinesiología", "09:"], **NO_ERROR}),
    ])

    mk("148 motivo HTA → MG", "56920000148", [
        ("menu", ["Motivos"]),
        ("motivo_hta", {"all": ["Perfecto", "Medicina General", "09:"], **NO_ERROR}),
    ])

    mk("149 motivo dental → Odontología", "56920000149", [
        ("menu", ["Motivos"]),
        ("motivo_dental", {"all": ["Perfecto", "Odontología", "09:"], **NO_ERROR}),
    ])

    mk("150 motivo otra consulta MG", "56920000150", [
        ("menu", ["Motivos"]),
        ("motivo_mg_otra", {"all": ["Perfecto", "Medicina General", "09:"], **NO_ERROR}),
    ])

    mk("151 motivo otra especialidad → selector", "56920000151", [
        ("menu", ["Motivos"]),
        ("motivo_otra_esp", {"any": ["especialidad", "categoría", "categoria"]}),
    ])

    mk("152 motivo resfrío full flow", "56920000152", [
        ("menu", ["Motivos"]),
        ("motivo_resfrio", {"all": ["Perfecto", "Medicina General"], **NO_ERROR}),
        ("confirmar_sugerido", {"any": ["Fonasa"], **NO_ERROR}),
        ("1", {"any": ["rut"], **NO_ERROR}),
        ("11111111-1", {"any": ["confirm"], **NO_ERROR}),
        ("si", {"any": ["reserv", "✅"], **NO_ERROR}),
    ])

    mk("153 motivo kine full flow", "56920000153", [
        ("menu", ["Motivos"]),
        ("motivo_kine", {"all": ["Perfecto", "Kinesiología"], **NO_ERROR}),
        ("confirmar_sugerido", None),
        ("1", {"any": ["rut"], **NO_ERROR}),
        ("11111111-1", {"any": ["confirm"], **NO_ERROR}),
        ("si", {"any": ["reserv", "✅"], **NO_ERROR}),
    ])

    mk("154 motivo dental full flow", "56920000154", [
        ("menu", ["Motivos"]),
        ("motivo_dental", {"all": ["Perfecto", "Odontología"], **NO_ERROR}),
        ("confirmar_sugerido", None),
        ("1", {"any": ["rut"], **NO_ERROR}),
        ("11111111-1", {"any": ["confirm"], **NO_ERROR}),
        ("si", {"any": ["reserv", "✅"], **NO_ERROR}),
    ])

    mk("155 motivo otra esp → seleccionar psicología", "56920000155", [
        ("menu", ["Motivos"]),
        ("motivo_otra_esp", {"any": ["especialidad"], **NO_ERROR}),
        ("psicología", {"any": ["Psicolog", "09:"], **NO_ERROR}),
    ])

    mk("156 motivo otra esp → seleccionar ecografía", "56920000156", [
        ("menu", ["Motivos"]),
        ("motivo_otra_esp", {"any": ["especialidad"], **NO_ERROR}),
        ("ecografía", {"any": ["Ecograf", "09:"], **NO_ERROR}),
    ])

    # ═══════════════════════════════════════════════════════════════════════════
    # BLOQUE 11: SIN DISPONIBILIDAD → LISTA DE ESPERA
    # (tests 157-166, 10 tests)
    # ═══════════════════════════════════════════════════════════════════════════

    def setup_sin_slots():
        FAKE_SIN_SLOTS["value"] = True

    mk("157 sin slots → oferta waitlist", "56920000157", [
        ("quiero agendar odontología", {"any": ["no encontré", "lista de espera", "inscrib"], **NO_ERROR}),
    ], setup=setup_sin_slots)

    mk("158 sin slots → acepto waitlist", "56920000158", [
        ("quiero agendar traumatología", {"any": ["no encontré", "lista de espera"], **NO_ERROR}),
        ("waitlist_si", {"any": ["inscri", "avis"], **NO_ERROR}),
    ], setup=setup_sin_slots)

    mk("159 sin slots → rechazo waitlist", "56920000159", [
        ("quiero agendar cardiología", {"any": ["no encontré", "lista de espera"], **NO_ERROR}),
        ("waitlist_no", None),
    ], setup=setup_sin_slots)

    mk("160 sin slots MG → waitlist", "56920000160", [
        ("quiero agendar medicina general", {"any": ["no encontré", "lista de espera"], **NO_ERROR}),
    ], setup=setup_sin_slots)

    mk("161 sin slots masoterapia → waitlist", "56920000161", [
        ("quiero agendar masoterapia", ["minutos", "20", "40"]),
        ("maso_20", {"any": ["no encontré", "lista de espera", "recepción"]}),
    ], setup=setup_sin_slots)

    mk("162 sin slots kine → waitlist", "56920000162", [
        ("quiero agendar kinesiología", {"any": ["no encontré", "lista de espera"], **NO_ERROR}),
    ], setup=setup_sin_slots)

    mk("163 sin slots nutri → waitlist", "56920000163", [
        ("quiero agendar nutrición", {"any": ["no encontré", "lista de espera"], **NO_ERROR}),
    ], setup=setup_sin_slots)

    mk("164 sin slots psico → waitlist", "56920000164", [
        ("quiero agendar psicología", {"any": ["no encontré", "lista de espera"], **NO_ERROR}),
    ], setup=setup_sin_slots)

    mk("165 sin slots ecografía → waitlist", "56920000165", [
        ("quiero agendar ecografía", {"any": ["no encontré", "lista de espera"], **NO_ERROR}),
    ], setup=setup_sin_slots)

    mk("166 sin slots ortodoncia → waitlist", "56920000166", [
        ("quiero agendar ortodoncia", {"any": ["no encontré", "lista de espera"], **NO_ERROR}),
    ], setup=setup_sin_slots)

    # ═══════════════════════════════════════════════════════════════════════════
    # BLOQUE 12: WAIT_ESPECIALIDAD — selector de especialidad
    # (tests 167-178, 12 tests)
    # ═══════════════════════════════════════════════════════════════════════════

    mk("167 WAIT_ESP sin especialidad → selector", "56920000167", [
        ("quiero agendar una hora", {"any": ["especialidad", "ayudo"], **NO_ERROR}),
    ])

    mk("168 WAIT_ESP elegir odontología", "56920000168", [
        ("quiero agendar una hora", {"any": ["especialidad"], **NO_ERROR}),
        ("odontología", {"any": ["Odonto", "09:"], **NO_ERROR}),
    ])

    mk("169 WAIT_ESP elegir traumatología", "56920000169", [
        ("quiero agendar una hora", {"any": ["especialidad"], **NO_ERROR}),
        ("traumatología", {"any": ["Traumatolog", "09:"], **NO_ERROR}),
    ])

    mk("170 WAIT_ESP elegir kinesiología", "56920000170", [
        ("quiero agendar una hora", {"any": ["especialidad"], **NO_ERROR}),
        ("kinesiología", {"any": ["Kine", "09:"], **NO_ERROR}),
    ])

    mk("171 WAIT_ESP elegir psicología", "56920000171", [
        ("quiero agendar una hora", {"any": ["especialidad"], **NO_ERROR}),
        ("psicología", {"any": ["Psicolog", "09:"], **NO_ERROR}),
    ])

    mk("172 WAIT_ESP elegir nutrición", "56920000172", [
        ("quiero agendar una hora", {"any": ["especialidad"], **NO_ERROR}),
        ("nutrición", {"any": ["Nutri", "09:"], **NO_ERROR}),
    ])

    mk("173 WAIT_ESP elegir fonoaudiología", "56920000173", [
        ("quiero agendar una hora", {"any": ["especialidad"], **NO_ERROR}),
        ("fonoaudiología", {"any": ["Fono", "09:"], **NO_ERROR}),
    ])

    mk("174 WAIT_ESP elegir cardiología", "56920000174", [
        ("quiero agendar una hora", {"any": ["especialidad"], **NO_ERROR}),
        ("cardiología", {"any": ["Cardiolog", "09:"], **NO_ERROR}),
    ])

    mk("175 WAIT_ESP elegir gastro", "56920000175", [
        ("quiero agendar una hora", {"any": ["especialidad"], **NO_ERROR}),
        ("gastroenterología", {"any": ["Gastro", "09:"], **NO_ERROR}),
    ])

    mk("176 WAIT_ESP elegir matrona", "56920000176", [
        ("quiero agendar una hora", {"any": ["especialidad"], **NO_ERROR}),
        ("matrona", {"any": ["Matrona", "09:"], **NO_ERROR}),
    ])

    mk("177 WAIT_ESP elegir estética facial", "56920000177", [
        ("quiero agendar una hora", {"any": ["especialidad"], **NO_ERROR}),
        ("estética facial", {"any": ["Estética", "09:"], **NO_ERROR}),
    ])

    mk("178 WAIT_ESP categoría médico", "56920000178", [
        ("quiero agendar una hora", {"any": ["especialidad"], **NO_ERROR}),
        ("cat_medico", None),
    ])

    # ═══════════════════════════════════════════════════════════════════════════
    # BLOQUE 13: COLOQUIALES CHILENOS → ESPECIALIDAD
    # (tests 179-193, 15 tests)
    # ═══════════════════════════════════════════════════════════════════════════

    mk("179 'me duele la guatita' → MG", "56920000179", [
        ("quiero hora porque me duele la guatita", {"any": ["Medicina", "09:"], **NO_ERROR}),
    ])

    mk("180 'tengo empacho' → MG", "56920000180", [
        ("necesito hora tengo empacho", {"any": ["Medicina", "09:"], **NO_ERROR}),
    ])

    mk("181 'dolor de estómago' → MG", "56920000181", [
        ("quiero hora por dolor de estomago", {"any": ["Medicina", "09:"], **NO_ERROR}),
    ])

    mk("182 'me duele la muela' → odonto", "56920000182", [
        ("quiero hora porque me duele la muela", {"any": ["Odonto", "09:"], **NO_ERROR}),
    ])

    mk("183 'quiero frenillos' → ortodoncia", "56920000183", [
        ("necesito hora para frenillos", {"any": ["Ortodoncia", "09:"], **NO_ERROR}),
    ])

    mk("184 'lumbago fuerte' → kine", "56920000184", [
        ("quiero hora por lumbago fuerte", {"any": ["Kine", "09:"], **NO_ERROR}),
    ])

    mk("185 'contractura espalda' → kine", "56920000185", [
        ("necesito hora tengo contractura en la espalda", {"any": ["Kine", "09:"], **NO_ERROR}),
    ])

    mk("186 'hijo tartamudea' → fono", "56920000186", [
        ("necesito hora mi hijo tartamudea", {"any": ["Fono", "09:"], **NO_ERROR}),
    ])

    mk("187 'quiero bajar de peso' → nutri", "56920000187", [
        ("quiero hora para bajar de peso", {"any": ["Nutri", "09:"], **NO_ERROR}),
    ])

    mk("188 'necesito dieta' → nutri", "56920000188", [
        ("necesito hora necesito una dieta", {"any": ["Nutri", "09:"], **NO_ERROR}),
    ])

    mk("189 'me torci el tobillo' → traumato", "56920000189", [
        ("quiero hora me torci el tobillo", {"any": ["Traumatolog", "09:"], **NO_ERROR}),
    ])

    mk("190 'presión alta' → cardio", "56920000190", [
        ("necesito hora por presion alta", {"any": ["Cardiolog", "09:"], **NO_ERROR}),
    ])

    mk("191 'tengo sinusitis' → ORL", "56920000191", [
        ("quiero hora por sinusitis", {"any": ["Otorrino", "09:"], **NO_ERROR}),
    ])

    mk("192 'estoy con ansiedad' → psico", "56920000192", [
        ("necesito hora estoy con ansiedad", {"any": ["Psicolog", "09:"], **NO_ERROR}),
    ])

    mk("193 'uña encarnada' → podología", "56920000193", [
        ("necesito hora por uña encarnada", {"any": ["Podolog", "09:"], **NO_ERROR}),
    ])

    # ═══════════════════════════════════════════════════════════════════════════
    # BLOQUE 14: EDGE CASES DEL FLUJO AGENDAR
    # (tests 194-200, 7 tests)
    # ═══════════════════════════════════════════════════════════════════════════

    mk("194 CAPS 'QUIERO AGENDAR'", "56920000194", [
        ("QUIERO AGENDAR KINESIOLOGIA", {"any": ["Kine", "09:"], **NO_ERROR}),
    ])

    mk("195 menu en medio del flujo → reset", "56920000195", [
        ("quiero agendar odontología", {"any": ["09:"], **NO_ERROR}),
        ("menu", ["Motivos"]),
    ])

    mk("196 cambio de intent en WAIT_SLOT", "56920000196", [
        ("quiero agendar odontología", {"any": ["09:"], **NO_ERROR}),
        ("en realidad quiero kine", None),
    ])

    mk("197 frustración WAIT_SLOT (3 intentos)", "56920000197", [
        ("quiero agendar cardiología", {"any": ["09:"], **NO_ERROR}),
        ("qwerasdf", {"any": ["número", "otro día", "ver todos"]}),
        ("zxcvbnm", {"any": ["entenderte", "número", "otro día", "menu"]}),
        ("!!!", {"any": ["recepción", "recepcion"]}),
    ])

    mk("198 precio inline ORL ($35.000)", "56920000198", [
        ("quiero agendar otorrinolaringología", {"any": ["09:", "$35"], **NO_ERROR}),
    ])

    mk("199 precio inline kine (Fonasa $7.830)", "56920000199", [
        ("quiero agendar kinesiología", {"any": ["09:", "$", "Fonasa"], **NO_ERROR}),
    ])

    mk("200 atajo '1' desde IDLE → agendar sin esp", "56920000200", [
        ("menu", ["Motivos"]),
        ("1", None),
    ])

    # ═══════════════════════════════════════════════════════════════════════════
    # RUN ALL TESTS
    # ═══════════════════════════════════════════════════════════════════════════

    passed = 0
    failed = 0
    for name, phone, steps, setup in results:
        ok = await run_convo(name, phone, steps, setup)
        mark = "✅" if ok else "❌"
        print(f"{mark} {name}")
        if ok:
            passed += 1
        else:
            failed += 1

    print()
    print(f"── Total: {passed}/{len(results)} passed, {failed} failed ({len(results)} tests) ──")
    print()
    if BUGS:
        print(f"── BUGS / ISSUES ENCONTRADOS ({len(BUGS)}) ──")
        for i, b in enumerate(BUGS, 1):
            print(f"\n[{i}] {b['test']} — step {b['step']}")
            print(f"    input: {b['input']!r}")
            print(f"    error: {b['error']}")
            if "got" in b:
                print(f"    got: {b['got'][:300]}")
            if "traceback" in b:
                tb_lines = b["traceback"].strip().split("\n")
                print(f"    traceback (tail):")
                for line in tb_lines[-4:]:
                    print(f"      {line}")
    return failed


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(0 if exit_code == 0 else 1)
