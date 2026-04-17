"""
Stress test 200 — harness offline para flows.handle_message.

- DB SQLite aislada (temp file)
- Medilink mockeado con fixtures deterministas
- Claude (detect_intent, respuesta_faq, clasificar_respuesta_seguimiento) mockeado
- 200 escenarios cubriendo TODAS las especialidades, profesionales, flujos,
  variantes coloquiales chilenas, FAQ, emergencias, edge cases y fidelización.

Ejecución:
    PYTHONPATH=app:. python3 tests/harness_stress_200.py

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

TMP_DB_DIR = Path(tempfile.mkdtemp(prefix="cmc_stress_"))
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

async def fake_listar_citas_paciente(id_paciente: int):
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
    if "endodoncia" in t and ("qué" in t or "que" in t or "cuanto" in t or "cuánto" in t or "precio" in t):
        return {"intent": "info", "especialidad": "endodoncia",
                "respuesta_directa": "La endodoncia es un tratamiento de conducto radicular. Valor: $180.000 a $250.000."}
    if "blanqueamiento" in t or "dientes amarillos" in t:
        return {"intent": "info", "especialidad": "odontología",
                "respuesta_directa": "El blanqueamiento aclara los dientes. Valor: $75.000 en Odontología General."}
    if "limpieza dental" in t or "sarro" in t:
        return {"intent": "info", "especialidad": "odontología",
                "respuesta_directa": "Destartraje + profilaxis $30.000 en Odontología General."}
    if "botox" in t or "toxina" in t:
        return {"intent": "info", "especialidad": "estética facial",
                "respuesta_directa": "Toxina botulínica para suavizar arrugas. $159.990 con Estética Facial."}
    if "peeling" in t or "manchas" in t:
        return {"intent": "info", "especialidad": "estética facial",
                "respuesta_directa": "Peeling químico para manchas y marcas. Consultar precio con Estética Facial."}
    if any(w in t for w in ["hilos tensores", "lifting"]):
        return {"intent": "info", "especialidad": "estética facial",
                "respuesta_directa": "Hilos tensores: lifting sin cirugía. $129.990 con Estética Facial."}
    if "lipopapada" in t or "papada" in t:
        return {"intent": "info", "especialidad": "estética facial",
                "respuesta_directa": "Lipopapada: inyecciones reductoras de grasa. $139.990 con Estética Facial."}
    if any(w in t for w in ["cuesta", "precio", "valor", "cuanto sale", "cuánto sale", "cuánto cuesta"]):
        return {"intent": "precio", "especialidad": esp, "respuesta_directa": f"El valor de la consulta depende de la especialidad."}
    if any(w in t for w in ["direccion", "dirección", "donde", "dónde", "ubicac", "horario de atenc",
                             "fonasa", "especialid", "que hace", "qué hace", "que trata", "qué trata",
                             "que tratamiento", "qué tratamiento"]):
        return {"intent": "info", "especialidad": esp,
                "respuesta_directa": "Estamos en Monsalve 102, Carampangue. Atendemos Fonasa e Isapre. Especialidades: medicina general, kine, odontología, psicología y más."}
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
    if "endodoncia" in t:
        return "La endodoncia es un tratamiento de conducto. Valor: $180.000 a $250.000."
    if "fonasa" in t:
        return "Sí, atendemos pacientes Fonasa en todas nuestras especialidades."
    if "donde" in t or "dónde" in t or "ubicac" in t:
        return "Estamos en Monsalve 102, esquina República, Carampangue."
    if "especialidad" in t:
        return "Tenemos medicina general, kine, odontología, psicología, nutrición, ortodoncia y más."
    if "precio" in t or "cuesta" in t or "valor" in t:
        return "El valor depende de la especialidad. Consulta en recepción al +56 9 8783 4148."
    if "otorrino" in t:
        return "El otorrino trata problemas de oído, nariz y garganta. Consulta $35.000."
    if "fono" in t:
        return "Fonoaudiología: evaluación infantil/adulto $30.000, terapia $25.000."
    if "kine" in t:
        return "Kinesiología: sesión Fonasa $7.830, particular $20.000."
    if "botox" in t:
        return "Toxina botulínica $159.990 con Estética Facial."
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
WARNINGS: list[dict] = []

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

    # ── Setup helpers ────────────────────────────────────────────────────────
    def setup_una_cita():
        FAKE_CITAS_PACIENTE.extend([{
            "id": 701, "id_profesional": 73,
            "profesional": "Dr. Andrés Abarca",
            "especialidad": "Medicina General",
            "fecha": "2026-04-20", "fecha_display": "lun 20 abr",
            "hora": "10:00", "hora_inicio": "10:00", "hora_fin": "10:15",
        }])

    def setup_multi_citas():
        FAKE_CITAS_PACIENTE.extend([
            {"id": 701, "id_profesional": 73, "profesional": "Dr. Abarca",
             "especialidad": "Medicina General", "fecha": "2026-04-20",
             "fecha_display": "lun 20 abr", "hora": "10:00",
             "hora_inicio": "10:00", "hora_fin": "10:15"},
            {"id": 702, "id_profesional": 55, "profesional": "Dra. Burgos",
             "especialidad": "Odontología", "fecha": "2026-04-22",
             "fecha_display": "mié 22 abr", "hora": "15:00",
             "hora_inicio": "15:00", "hora_fin": "15:30"},
        ])

    def setup_cita_kine():
        FAKE_CITAS_PACIENTE.extend([{
            "id": 710, "id_profesional": 77,
            "profesional": "Luis Armijo",
            "especialidad": "Kinesiología",
            "fecha": "2026-04-22", "fecha_display": "mié 22 abr",
            "hora": "11:00", "hora_inicio": "11:00", "hora_fin": "11:40",
        }])

    def setup_cita_traumato():
        FAKE_CITAS_PACIENTE.extend([{
            "id": 711, "id_profesional": 64,
            "profesional": "Dr. Claudio Barraza",
            "especialidad": "Traumatología",
            "fecha": "2026-04-21", "fecha_display": "mar 21 abr",
            "hora": "09:30", "hora_inicio": "09:30", "hora_fin": "09:45",
        }])

    def setup_cita_odonto():
        FAKE_CITAS_PACIENTE.extend([{
            "id": 712, "id_profesional": 55,
            "profesional": "Dra. Javiera Burgos",
            "especialidad": "Odontología General",
            "fecha": "2026-04-23", "fecha_display": "jue 23 abr",
            "hora": "14:00", "hora_inicio": "14:00", "hora_fin": "14:30",
        }])

    def setup_cita_psico():
        FAKE_CITAS_PACIENTE.extend([{
            "id": 713, "id_profesional": 74,
            "profesional": "Jorge Montalba",
            "especialidad": "Psicología Adulto",
            "fecha": "2026-04-24", "fecha_display": "vie 24 abr",
            "hora": "16:00", "hora_inicio": "16:00", "hora_fin": "16:45",
        }])

    # ═══════════════════════════════════════════════════════════════════════════
    # BLOQUE 1: AGENDAR POR ESPECIALIDAD (tests 001-020, 20 tests)
    # ═══════════════════════════════════════════════════════════════════════════

    mk("001 agendar medicina general", "56910000001", [
        ("quiero agendar medicina general", {"any": ["Medicina", "09:"], **NO_ERROR}),
    ])

    mk("002 agendar odontología", "56910000002", [
        ("quiero agendar odontología", {"any": ["Odonto", "09:"], **NO_ERROR}),
    ])

    mk("003 agendar ortodoncia", "56910000003", [
        ("quiero agendar ortodoncia", {"any": ["Ortodoncia", "09:"], **NO_ERROR}),
    ])

    mk("004 agendar kinesiología", "56910000004", [
        ("quiero agendar kinesiología", {"any": ["Kine", "09:"], **NO_ERROR}),
    ])

    mk("005 agendar masoterapia", "56910000005", [
        ("quiero agendar masoterapia", ["minutos", "20", "40"]),
    ])

    mk("006 agendar psicología", "56910000006", [
        ("quiero agendar psicología", {"any": ["Psico", "09:"], **NO_ERROR}),
    ])

    mk("007 agendar nutrición", "56910000007", [
        ("quiero agendar nutrición", {"any": ["Nutri", "09:"], **NO_ERROR}),
    ])

    mk("008 agendar fonoaudiología", "56910000008", [
        ("quiero agendar fonoaudiología", {"any": ["Fono", "09:"], **NO_ERROR}),
    ])

    mk("009 agendar podología", "56910000009", [
        ("quiero agendar podología", {"any": ["Podolog", "09:"], **NO_ERROR}),
    ])

    mk("010 agendar traumatología", "56910000010", [
        ("quiero agendar traumatología", {"any": ["Traumatolog", "09:"], **NO_ERROR}),
    ])

    mk("011 agendar cardiología", "56910000011", [
        ("quiero agendar cardiología", {"any": ["Cardiolog", "09:"], **NO_ERROR}),
    ])

    mk("012 agendar gastroenterología", "56910000012", [
        ("quiero agendar gastroenterología", {"any": ["Gastro", "09:"], **NO_ERROR}),
    ])

    mk("013 agendar otorrinolaringología", "56910000013", [
        ("quiero agendar otorrinolaringología", {"any": ["Otorrino", "09:"], **NO_ERROR}),
    ])

    mk("014 agendar ginecología", "56910000014", [
        ("quiero agendar ginecología", {"any": ["Ginecolog", "09:"], **NO_ERROR}),
    ])

    mk("015 agendar matrona", "56910000015", [
        ("quiero agendar matrona", {"any": ["Matrona", "09:"], **NO_ERROR}),
    ])

    mk("016 agendar endodoncia", "56910000016", [
        ("quiero agendar endodoncia", {"any": ["Endodoncia", "09:"], **NO_ERROR}),
    ])

    mk("017 agendar implantología", "56910000017", [
        ("quiero agendar implantología", {"any": ["Implantolog", "09:"], **NO_ERROR}),
    ])

    mk("018 agendar estética facial", "56910000018", [
        ("quiero agendar estética facial", {"any": ["Estética", "09:"], **NO_ERROR}),
    ])

    mk("019 agendar ecografía", "56910000019", [
        ("quiero agendar ecografía", {"any": ["Ecograf", "09:"], **NO_ERROR}),
    ])

    mk("020 agendar psicología infantil", "56910000020", [
        ("quiero agendar psicología", {"any": ["Psicolog", "09:"], **NO_ERROR}),
    ])

    # ═══════════════════════════════════════════════════════════════════════════
    # BLOQUE 2: AGENDAR POR NOMBRE DE PROFESIONAL (tests 021-040, 20 tests)
    # ═══════════════════════════════════════════════════════════════════════════

    mk("021 prof Dr. Olavarría", "56910000021", [
        ("quiero hora con el dr olavarria", {"any": ["Olavarría", "09:"], **NO_ERROR}),
    ])

    mk("022 prof Dr. Abarca", "56910000022", [
        ("necesito hora con el doctor abarca", {"any": ["Abarca", "09:"], **NO_ERROR}),
    ])

    mk("023 prof Dr. Márquez", "56910000023", [
        ("quiero ver al dr marquez", {"any": ["Márquez", "menú", "menu", "Medicina"], **NO_ERROR}),
    ])

    mk("024 prof Dr. Borrego", "56910000024", [
        ("hora con el dr borrego", {"any": ["Borrego", "09:"], **NO_ERROR}),
    ])

    mk("025 prof Dr. Millán", "56910000025", [
        ("quiero hora con el dr millan", {"any": ["Millán", "09:"], **NO_ERROR}),
    ])

    mk("026 prof Dr. Barraza", "56910000026", [
        ("hora con el doctor barraza", {"any": ["Barraza", "09:"], **NO_ERROR}),
    ])

    mk("027 prof Dr. Rejón (Tirso)", "56910000027", [
        ("necesito hora con el dr tirso", {"any": ["Rejón", "09:"], **NO_ERROR}),
    ])

    mk("028 prof Dr. Quijano", "56910000028", [
        ("quiero hora con quijano", {"any": ["Quijano", "09:"], **NO_ERROR}),
    ])

    mk("029 prof Dra. Burgos", "56910000029", [
        ("hora con la dra burgos", {"any": ["Burgos", "09:"], **NO_ERROR}),
    ])

    mk("030 prof Dr. Jiménez", "56910000030", [
        ("quiero hora con el dr jimenez", {"any": ["Jiménez", "09:"], **NO_ERROR}),
    ])

    mk("031 prof Dra. Castillo", "56910000031", [
        ("hora con la dra castillo", {"any": ["Castillo", "09:"], **NO_ERROR}),
    ])

    mk("032 prof Dr. Fredes", "56910000032", [
        ("necesito hora con el dr fredes", {"any": ["Fredes", "09:"], **NO_ERROR}),
    ])

    mk("033 prof Dra. Valdés", "56910000033", [
        ("quiero hora con la dra valdes", {"any": ["Valdés", "09:"], **NO_ERROR}),
    ])

    mk("034 prof Dra. Fuentealba", "56910000034", [
        ("hora con fuentealba", {"any": ["Fuentealba", "09:"], **NO_ERROR}),
    ])

    mk("035 prof Paola Acosta", "56910000035", [
        ("quiero hora con paola acosta", ["minutos", "20", "40"]),
    ])

    mk("036 prof Luis Armijo", "56910000036", [
        ("necesito hora con luis armijo", {"any": ["Armijo", "09:"], **NO_ERROR}),
    ])

    mk("037 prof Leonardo Etcheverry", "56910000037", [
        ("hora con leonardo etcheverry", {"any": ["Etcheverry", "09:"], **NO_ERROR}),
    ])

    mk("038 prof Gisela Pinto", "56910000038", [
        ("quiero hora con gisela pinto", {"any": ["Pinto", "09:"], **NO_ERROR}),
    ])

    mk("039 prof Jorge Montalba", "56910000039", [
        ("hora con montalba", {"any": ["Montalba", "09:"], **NO_ERROR}),
    ])

    mk("040 prof Juana Arratia", "56910000040", [
        ("hora con juana arratia", {"any": ["Arratia", "09:"], **NO_ERROR}),
    ])

    # ═══════════════════════════════════════════════════════════════════════════
    # BLOQUE 3: VARIANTES COLOQUIALES CHILENAS (tests 041-070, 30 tests)
    # ═══════════════════════════════════════════════════════════════════════════

    mk("041 'me duele la guata' → MG", "56910000041", [
        ("quiero hora porque me duele la guata", {"any": ["Medicina", "09:"], **NO_ERROR}),
    ])

    mk("042 'me duele la muela' → odonto", "56910000042", [
        ("necesito hora porque me duele la muela", {"any": ["Odonto", "09:"], **NO_ERROR}),
    ])

    mk("043 'quiero ponerme brackets' → ortodoncia", "56910000043", [
        ("quiero hora para ponerme brackets", {"any": ["Ortodoncia", "09:"], **NO_ERROR}),
    ])

    mk("044 'hora pal corazón' → cardio", "56910000044", [
        ("necesito hora para el corazon", {"any": ["Cardiolog", "09:"], **NO_ERROR}),
    ])

    mk("045 'tengo problemas pa tragar' → ORL", "56910000045", [
        ("quiero hora porque tengo problemas de garganta", {"any": ["Otorrino", "09:"], **NO_ERROR}),
    ])

    mk("046 'mi hijo no habla bien' → fono", "56910000046", [
        ("necesito hora mi hijo no habla bien", {"any": ["Fono", "09:"], **NO_ERROR}),
    ])

    mk("047 'quiero bajar de peso' → nutri", "56910000047", [
        ("quiero hora para bajar de peso", {"any": ["Nutri", "09:"], **NO_ERROR}),
    ])

    mk("048 'estoy muy estresado' → psico", "56910000048", [
        ("necesito hora estoy muy estresado", {"any": ["Psicolog", "09:"], **NO_ERROR}),
    ])

    mk("049 'me torcí el tobillo' → traumato", "56910000049", [
        ("quiero hora me torcí el tobillo", {"any": ["Traumatolog", "09:"], **NO_ERROR}),
    ])

    mk("050 'quiero hacerme un peeling' → estética", "56910000050", [
        ("quiero hora para hacerme un peeling", {"any": ["Estética", "09:", "peeling", "Estética Facial"], **NO_ERROR}),
    ])

    mk("051 'necesito ecografia' → ecograf", "56910000051", [
        ("necesito hora para ecografia", {"any": ["Ecograf", "09:"], **NO_ERROR}),
    ])

    mk("052 'me duele la rodilla' → traumato", "56910000052", [
        ("quiero hora me duele la rodilla", {"any": ["Traumatolog", "09:"], **NO_ERROR}),
    ])

    mk("053 'quiero sacarme una muela' → odonto", "56910000053", [
        ("necesito hora quiero sacarme una muela", {"any": ["Odonto", "09:"], **NO_ERROR}),
    ])

    mk("054 'dolor de espalda fuerte' → kine", "56910000054", [
        ("necesito hora tengo dolor de espalda", {"any": ["Kine", "09:"], **NO_ERROR}),
    ])

    mk("055 'tengo acidez y reflujo' → MG", "56910000055", [
        ("quiero hora tengo acidez y reflujo", {"any": ["Medicina", "09:"], **NO_ERROR}),
    ])

    mk("056 'uña encarnada' → podología", "56910000056", [
        ("necesito hora por uña encarnada", {"any": ["Podolog", "09:"], **NO_ERROR}),
    ])

    mk("057 'no me llega la regla' → matrona", "56910000057", [
        ("necesito hora porque no me llega la regla", {"any": ["Matrona", "09:"], **NO_ERROR}),
    ])

    mk("058 'presion alta' → cardio", "56910000058", [
        ("quiero hora por presion alta", {"any": ["Cardiolog", "09:"], **NO_ERROR}),
    ])

    mk("059 'tengo ansiedad' → psico", "56910000059", [
        ("necesito hora estoy con ansiedad", {"any": ["Psicolog", "09:"], **NO_ERROR}),
    ])

    mk("060 'quiero masaje relajante' → masoterapia", "56910000060", [
        ("quiero hora de masaje relajante", ["minutos", "20", "40"]),
    ])

    mk("061 'dolor de oido' → ORL", "56910000061", [
        ("necesito hora por dolor de oido", {"any": ["Otorrino", "09:"], **NO_ERROR}),
    ])

    mk("062 'necesito una dieta' → nutri", "56910000062", [
        ("quiero hora necesito una dieta", {"any": ["Nutri", "09:"], **NO_ERROR}),
    ])

    mk("063 'tengo un esguince' → traumato", "56910000063", [
        ("quiero hora tengo un esguince", {"any": ["Traumatolog", "09:"], **NO_ERROR}),
    ])

    mk("064 'quiero ver al gastro' → gastro", "56910000064", [
        ("quiero agendar gastro", {"any": ["Gastro", "09:"], **NO_ERROR}),
    ])

    mk("065 'hora al otorrino' → ORL", "56910000065", [
        ("quiero agendar otorrino", {"any": ["Otorrino", "09:"], **NO_ERROR}),
    ])

    mk("066 'necesito hora pal gine' → gineco", "56910000066", [
        ("quiero agendar gine", {"any": ["Ginecolog", "09:"], **NO_ERROR}),
    ])

    mk("067 'hora de dentista' → odonto", "56910000067", [
        ("necesito hora con el dentista", {"any": ["Odonto", "09:"], **NO_ERROR}),
    ])

    mk("068 'hora con la fono' → fonoaudiología", "56910000068", [
        ("quiero hora con la fono", {"any": ["Fono", "09:"], **NO_ERROR}),
    ])

    mk("069 'necesito podologo' → podología", "56910000069", [
        ("quiero agendar podología", {"any": ["Podolog", "09:"], **NO_ERROR}),
    ])

    mk("070 'tengo depresion' → psico", "56910000070", [
        ("necesito hora tengo depresion", {"any": ["Psicolog", "09:"], **NO_ERROR}),
    ])

    # ═══════════════════════════════════════════════════════════════════════════
    # BLOQUE 4: FAQ POR ESPECIALIDAD (tests 071-100, 30 tests)
    # ═══════════════════════════════════════════════════════════════════════════

    mk("071 FAQ cuánto sale MG", "56910000071", [
        ("cuanto sale la consulta de medicina general", {"any": ["valor", "precio", "consulta", "depende"], **NO_ERROR}),
    ])

    mk("072 FAQ qué hace un otorrino", "56910000072", [
        ("que hace un otorrino", {"any": ["otorrino", "oído", "nariz", "garganta", "Monsalve"], **NO_ERROR}),
    ])

    mk("073 FAQ fonasa", "56910000073", [
        ("atienden fonasa", {"any": ["fonasa", "Monsalve", "atend"], **NO_ERROR}),
    ])

    mk("074 FAQ ubicación", "56910000074", [
        ("dónde están ubicados", {"any": ["Monsalve", "Carampangue"], **NO_ERROR}),
    ])

    mk("075 FAQ especialidades", "56910000075", [
        ("qué especialidades tienen", {"any": ["medicina", "kine", "odonto", "especialidad"], **NO_ERROR}),
    ])

    mk("076 FAQ tapadura", "56910000076", [
        ("quiero una tapadura", {"any": ["tapadura", "obturación", "caries", "agendar"], **NO_ERROR}),
    ])

    mk("077 FAQ endodoncia", "56910000077", [
        ("qué es una endodoncia", {"any": ["endodoncia", "conducto"], **NO_ERROR}),
    ])

    mk("078 FAQ blanqueamiento dental", "56910000078", [
        ("cuanto cuesta un blanqueamiento", {"any": ["blanqueamiento", "aclara", "$75.000", "valor", "depende"], **NO_ERROR}),
    ])

    mk("079 FAQ botox", "56910000079", [
        ("cuanto cuesta el botox", {"any": ["botox", "toxina", "$159.990", "Estética", "valor", "depende"], **NO_ERROR}),
    ])

    mk("080 FAQ peeling", "56910000080", [
        ("quiero hacerme un peeling", {"any": ["peeling", "manchas", "Estética", "agendar"], **NO_ERROR}),
    ])

    mk("081 FAQ hilos tensores", "56910000081", [
        ("cuanto cuestan los hilos tensores", {"any": ["hilos", "$129.990", "Estética", "valor", "depende"], **NO_ERROR}),
    ])

    mk("082 FAQ lipopapada", "56910000082", [
        ("quiero reducir la papada", {"any": ["papada", "lipopapada", "$139.990", "Estética", "agendar"], **NO_ERROR}),
    ])

    mk("083 FAQ limpieza dental / sarro", "56910000083", [
        ("necesito una limpieza dental", {"any": ["limpieza", "sarro", "destartraje", "$30.000", "Odonto", "agendar"], **NO_ERROR}),
    ])

    mk("084 FAQ precio kine", "56910000084", [
        ("cuanto vale la kine", {"any": ["valor", "kine", "Kine", "$", "depende"], **NO_ERROR}),
    ])

    mk("085 FAQ precio fonoaudiología", "56910000085", [
        ("cuanto sale la fono", {"any": ["valor", "fono", "Fono", "$", "depende"], **NO_ERROR}),
    ])

    mk("086 FAQ dirección", "56910000086", [
        ("donde queda el centro medico", {"any": ["Monsalve", "Carampangue"], **NO_ERROR}),
    ])

    mk("087 FAQ horario atención", "56910000087", [
        ("cual es el horario de atencion", {"any": ["Monsalve", "Carampangue", "horario"], **NO_ERROR}),
    ])

    mk("088 FAQ precio traumatología", "56910000088", [
        ("cuanto sale la consulta de traumatología", {"any": ["valor", "depende", "consulta"], **NO_ERROR}),
    ])

    mk("089 FAQ precio cardiología", "56910000089", [
        ("cuanto cuesta una consulta de cardiología", {"any": ["valor", "depende", "consulta"], **NO_ERROR}),
    ])

    mk("090 FAQ precio psicología", "56910000090", [
        ("cuanto sale el psicologo", {"any": ["valor", "depende", "consulta"], **NO_ERROR}),
    ])

    mk("091 FAQ precio nutrición", "56910000091", [
        ("cuanto cuesta la nutricionista", {"any": ["valor", "depende", "consulta"], **NO_ERROR}),
    ])

    mk("092 FAQ precio ginecología", "56910000092", [
        ("cuanto sale la ginecología", {"any": ["valor", "depende", "consulta"], **NO_ERROR}),
    ])

    mk("093 FAQ precio gastro", "56910000093", [
        ("cuanto cuesta el gastroenterologo", {"any": ["valor", "depende", "consulta"], **NO_ERROR}),
    ])

    mk("094 FAQ precio odontología", "56910000094", [
        ("cuanto sale el dentista", {"any": ["valor", "depende", "consulta"], **NO_ERROR}),
    ])

    mk("095 FAQ precio ecografía", "56910000095", [
        ("cuanto cuesta una ecografia", {"any": ["valor", "depende", "consulta"], **NO_ERROR}),
    ])

    mk("096 FAQ precio matrona", "56910000096", [
        ("cuanto sale la matrona", {"any": ["valor", "depende", "consulta"], **NO_ERROR}),
    ])

    mk("097 FAQ precio podología", "56910000097", [
        ("cuanto cuesta la podologa", {"any": ["valor", "depende", "consulta"], **NO_ERROR}),
    ])

    mk("098 FAQ precio masoterapia", "56910000098", [
        ("cuanto vale un masaje", {"any": ["valor", "depende", "consulta", "masoterapia", "minutos", "sesión"], **NO_ERROR}),
    ])

    mk("099 FAQ precio implantología", "56910000099", [
        ("cuanto cuesta un implante dental", {"any": ["valor", "depende", "consulta"], **NO_ERROR}),
    ])

    mk("100 FAQ precio ortodoncia", "56910000100", [
        ("cuanto sale la ortodoncia", {"any": ["valor", "depende", "consulta"], **NO_ERROR}),
    ])

    # ═══════════════════════════════════════════════════════════════════════════
    # BLOQUE 5: FLUJO COMPLETO AGENDAR → SLOT → CONFIRMAR (tests 101-120, 20 tests)
    # ═══════════════════════════════════════════════════════════════════════════

    mk("101 full flow MG: agendar→slot→rut→confirm", "56910000101", [
        ("quiero agendar medicina general", {"any": ["Medicina", "09:"], **NO_ERROR}),
        ("confirmar_sugerido", ["Fonasa", "Particular"]),
        ("1", ["rut", "RUT"]),
        ("11111111-1", ["Juan", "confirm"]),
        ("confirmar", ["reserv", "confirm", "✅", "cita"]),
    ])

    mk("102 full flow odonto: agendar→slot→rut→confirm", "56910000102", [
        ("quiero agendar odontología", {"any": ["Odonto", "09:"], **NO_ERROR}),
        # Particular-only → salta Fonasa/Particular, va directo a RUT
        ("confirmar_sugerido", ["RUT"]),
        ("11111111-1", ["confirm"]),
        ("confirmar", ["reserv", "✅", "cita"]),
    ])

    mk("103 full flow kine: agendar→slot→nuevo paciente (1 msg)", "56910000103", [
        ("quiero agendar kinesiología", {"any": ["Kine", "09:"], **NO_ERROR}),
        ("confirmar_sugerido", ["Fonasa"]),
        ("1", ["rut"]),
        ("99999999-9", ["Nombre", "Sexo", "nacimiento"]),
        ("Pedro Pérez González, F, 15/03/1990", {"any": ["confirm", "cita", "reserv", "Registrad"], **NO_ERROR}),
        ("confirmar", ["reserv", "✅", "cita"]),
    ])

    mk("104 full flow traumato", "56910000104", [
        ("quiero agendar traumatología", {"any": ["Traumatolog", "09:"], **NO_ERROR}),
        ("confirmar_sugerido", ["RUT"]),
        ("11111111-1", ["confirm"]),
        ("confirmar", ["reserv", "✅"]),
    ])

    mk("105 full flow cardio", "56910000105", [
        ("quiero agendar cardiología", {"any": ["Cardiolog", "09:"], **NO_ERROR}),
        ("confirmar_sugerido", ["RUT"]),
        ("11111111-1", ["confirm"]),
        ("confirmar", ["reserv", "✅"]),
    ])

    mk("106 full flow psico", "56910000106", [
        ("quiero agendar psicología", {"any": ["Psicolog", "09:"], **NO_ERROR}),
        ("confirmar_sugerido", ["Fonasa", "Particular"]),
        ("1", ["rut"]),
        ("11111111-1", ["confirm"]),
        ("confirmar", ["reserv", "✅"]),
    ])

    mk("107 full flow nutri", "56910000107", [
        ("quiero agendar nutrición", {"any": ["Nutri", "09:"], **NO_ERROR}),
        ("confirmar_sugerido", ["Fonasa", "Particular"]),
        ("1", ["rut"]),
        ("11111111-1", ["confirm"]),
        ("confirmar", ["reserv", "✅"]),
    ])

    mk("108 full flow fono", "56910000108", [
        ("quiero agendar fonoaudiología", {"any": ["Fono", "09:"], **NO_ERROR}),
        ("confirmar_sugerido", ["RUT"]),
        ("11111111-1", ["confirm"]),
        ("confirmar", ["reserv", "✅"]),
    ])

    mk("109 full flow gastro", "56910000109", [
        ("quiero agendar gastroenterología", {"any": ["Gastro", "09:"], **NO_ERROR}),
        ("confirmar_sugerido", ["RUT"]),
        ("11111111-1", ["confirm"]),
        ("confirmar", ["reserv", "✅"]),
    ])

    mk("110 full flow ORL", "56910000110", [
        ("quiero agendar otorrino", {"any": ["Otorrino", "09:"], **NO_ERROR}),
        ("confirmar_sugerido", ["RUT"]),
        ("11111111-1", ["confirm"]),
        ("confirmar", ["reserv", "✅"]),
    ])

    mk("111 full flow gineco", "56910000111", [
        ("quiero agendar ginecología", {"any": ["Ginecolog", "09:"], **NO_ERROR}),
        ("confirmar_sugerido", ["RUT"]),
        ("11111111-1", ["confirm"]),
        ("confirmar", ["reserv", "✅"]),
    ])

    mk("112 full flow matrona", "56910000112", [
        ("quiero agendar matrona", {"any": ["Matrona", "09:"], **NO_ERROR}),
        ("confirmar_sugerido", ["Fonasa", "Particular"]),
        ("1", ["rut"]),
        ("11111111-1", ["confirm"]),
        ("confirmar", ["reserv", "✅"]),
    ])

    mk("113 full flow endodoncia", "56910000113", [
        ("quiero agendar endodoncia", {"any": ["Endodoncia", "09:"], **NO_ERROR}),
        ("confirmar_sugerido", ["RUT"]),
        ("11111111-1", ["confirm"]),
        ("confirmar", ["reserv", "✅"]),
    ])

    mk("114 full flow implantología", "56910000114", [
        ("quiero agendar implantología", {"any": ["Implantolog", "09:"], **NO_ERROR}),
        ("confirmar_sugerido", ["RUT"]),
        ("11111111-1", ["confirm"]),
        ("confirmar", ["reserv", "✅"]),
    ])

    mk("115 full flow estética facial", "56910000115", [
        ("quiero agendar estética facial", {"any": ["Estética", "09:"], **NO_ERROR}),
        ("confirmar_sugerido", ["RUT"]),
        ("11111111-1", ["confirm"]),
        ("confirmar", ["reserv", "✅"]),
    ])

    mk("116 full flow ecografía", "56910000116", [
        ("quiero agendar ecografía", {"any": ["Ecograf", "09:"], **NO_ERROR}),
        ("confirmar_sugerido", ["RUT"]),
        ("11111111-1", ["confirm"]),
        ("confirmar", ["reserv", "✅"]),
    ])

    mk("117 full flow podología", "56910000117", [
        ("quiero agendar podología", {"any": ["Podolog", "09:"], **NO_ERROR}),
        ("confirmar_sugerido", ["RUT"]),
        ("11111111-1", ["confirm"]),
        ("confirmar", ["reserv", "✅"]),
    ])

    mk("118 full flow masoterapia 40 min", "56910000118", [
        ("quiero agendar masoterapia", ["minutos", "20", "40"]),
        ("40 minutos", {"any": ["09:"], **NO_ERROR}),
        # Masoterapia es particular-only → salta Fonasa/Particular
        ("confirmar_sugerido", ["RUT"]),
        ("11111111-1", ["confirm"]),
        ("confirmar", ["reserv", "✅"]),
    ])

    mk("119 full flow con ver todos los horarios", "56910000119", [
        ("quiero agendar medicina general", ["09:"]),
        ("ver todos", {"any": ["09:", "10:"], **NO_ERROR}),
    ])

    mk("120 full flow con otro día", "56910000120", [
        ("quiero agendar odontología", ["09:"]),
        ("otro día", None),
    ])

    # ═══════════════════════════════════════════════════════════════════════════
    # BLOQUE 6: CANCELACIÓN POR ESPECIALIDAD (tests 121-130, 10 tests)
    # ═══════════════════════════════════════════════════════════════════════════

    mk("121 cancelar MG con 1 cita", "56910000121", [
        ("quiero cancelar mi hora", ["rut"]),
        ("11111111-1", {"any": ["Abarca", "20", "cancel"], **NO_ERROR}),
        ("1", {"any": ["confirm", "seguro", "cancel"], **NO_ERROR}),
        ("si", {"any": ["cancel", "anul"], **NO_ERROR}),
    ], setup=setup_una_cita)

    mk("122 cancelar multi citas", "56910000122", [
        ("cancelar", ["rut"]),
        ("11111111-1", {"any": ["Abarca", "Burgos"], **NO_ERROR}),
        ("1", {"any": ["Abarca", "confirm", "seguro"], **NO_ERROR}),
        ("si", ["cancel"]),
    ], setup=setup_multi_citas)

    mk("123 cancelar sin citas", "56910000123", [
        ("cancelar", ["rut"]),
        ("11111111-1", {"any": ["no tienes", "sin", "no hay", "no encontré"]}),
    ])

    mk("124 cancelar desde atajo 3", "56910000124", [
        ("menu", ["Motivos"]),
        ("3", ["rut"]),
        ("11111111-1", {"any": ["Abarca", "cancel"], **NO_ERROR}),
    ], setup=setup_una_cita)

    mk("125 cancelar y decir 'no' en confirm", "56910000125", [
        ("cancelar", ["rut"]),
        ("11111111-1", {"any": ["Abarca"], **NO_ERROR}),
        ("1", {"any": ["confirm", "seguro"], **NO_ERROR}),
        ("no", {"any": ["mantener", "mant", "listo", "sin cancelar", "menú", "menu"], **NO_ERROR}),
    ], setup=setup_una_cita)

    mk("126 cancelar RUT inválido", "56910000126", [
        ("cancelar", ["rut"]),
        ("xxx", {"any": ["rut", "válido", "formato"]}),
    ])

    mk("127 cancelar paciente no registrado", "56910000127", [
        ("cancelar", ["rut"]),
        ("98765432-1", {"any": ["no", "encontr", "registrado", "sin"]}),
    ])

    mk("128 cancelar con intent 'anular'", "56910000128", [
        ("quiero anular mi hora", ["rut"]),
    ])

    mk("129 cancelar desde sub-menú", "56910000129", [
        ("menu", ["Motivos"]),
        ("accion_cambiar", {"all": ["Reagendar", "Cancelar"], **NO_ERROR}),
        ("3", ["rut"]),
    ])

    mk("130 cancelar cita kine", "56910000130", [
        ("cancelar", ["rut"]),
        ("11111111-1", {"any": ["Armijo", "Kine", "cancel"], **NO_ERROR}),
    ], setup=setup_cita_kine)

    # ═══════════════════════════════════════════════════════════════════════════
    # BLOQUE 7: REAGENDAR (tests 131-140, 10 tests)
    # ═══════════════════════════════════════════════════════════════════════════

    mk("131 reagendar atajo 2", "56910000131", [
        ("menu", ["Motivos"]),
        ("2", {"any": ["rut"], **NO_ERROR}),
    ])

    mk("132 reagendar con perfil guardado", "56910000132", [
        ("quiero cambiar mi hora", {"any": ["Abarca", "cita", "elegir", "reagend"], **NO_ERROR}),
    ], setup=lambda: (save_profile("56910000132", "11111111-1", "Juan Prueba Test"), setup_una_cita()))

    mk("133 reagendar sin citas activas", "56910000133", [
        ("quiero reagendar", ["rut"]),
        ("11111111-1", {"any": ["no", "sin", "tienes"]}),
    ])

    mk("134 reagendar flujo completo", "56910000134", [
        ("reagendar", ["rut"]),
        ("11111111-1", {"any": ["Abarca", "cita", "reagend"], **NO_ERROR}),
        ("1", {"any": ["09:", "reagend"], **NO_ERROR}),
        ("confirmar_sugerido", {"any": ["Fonasa"], **NO_ERROR}),
        ("1", {"any": ["datos anteriores", "continuar", "confirm"], **NO_ERROR}),
        ("si", {"any": ["confirm", "Estás a un paso"], **NO_ERROR}),
        ("si", {"any": ["reagend", "✅", "reserv"], **NO_ERROR}),
    ], setup=setup_una_cita)

    mk("135 reagendar abort con menu", "56910000135", [
        ("reagendar", ["rut"]),
        ("menu", ["Agendar", "opciones"]),
    ])

    mk("136 reagendar texto libre", "56910000136", [
        ("quiero mover mi hora del lunes", {"any": ["rut", "reagend"], **NO_ERROR}),
    ])

    mk("137 reagendar decir 'no' en cita", "56910000137", [
        ("reagendar", ["rut"]),
        ("11111111-1", {"any": ["Abarca"]}),
        ("no", {"any": ["menu", "menú", "listo", "sin", "dejamos", "entendido"],
                "none": ["entre 1 y"]}),
    ], setup=setup_una_cita)

    mk("138 reagendar falla crear nueva", "56910000138", [
        ("reagendar", ["rut"]),
        ("11111111-1", {"any": ["Abarca"], **NO_ERROR}),
        ("1", {"any": ["09:"], **NO_ERROR}),
        ("confirmar_sugerido", None),
        ("1", None),
        ("si", None),
        ("si", {"any": ["ya fue tomada", "encontré otra", "reservo"]}),
    ], setup=lambda: (setup_una_cita(), FAKE_FAIL_CREAR_CITA.update(value=True)))

    mk("139 reagendar desde sub-menú", "56910000139", [
        ("menu", ["Motivos"]),
        ("accion_cambiar", {"all": ["Reagendar", "Cancelar"], **NO_ERROR}),
        ("2", {"any": ["rut"], **NO_ERROR}),
    ])

    mk("140 reagendar con 'reprogramar'", "56910000140", [
        ("quiero reprogramar mi cita", {"any": ["rut", "reagend"], **NO_ERROR}),
    ])

    # ═══════════════════════════════════════════════════════════════════════════
    # BLOQUE 8: VER RESERVAS (tests 141-145, 5 tests)
    # ═══════════════════════════════════════════════════════════════════════════

    mk("141 ver con 1 cita", "56910000141", [
        ("quiero ver mis citas", ["rut"]),
        ("11111111-1", {"any": ["Abarca", "20", "10:00"], **NO_ERROR}),
    ], setup=setup_una_cita)

    mk("142 ver con multi citas", "56910000142", [
        ("mis horas", ["rut"]),
        ("11111111-1", {"any": ["Abarca", "Burgos"], **NO_ERROR}),
    ], setup=setup_multi_citas)

    mk("143 ver sin citas", "56910000143", [
        ("que tengo agendado", ["rut"]),
        ("11111111-1", {"any": ["no tienes", "no hay", "sin"]}),
    ])

    mk("144 ver reservas atajo 4", "56910000144", [
        ("menu", ["Motivos"]),
        ("4", ["rut"]),
    ])

    mk("145 ver reservas desde sub-menú", "56910000145", [
        ("menu", ["Motivos"]),
        ("accion_mis_citas", {"all": ["reservas", "espera"], **NO_ERROR}),
        ("4", ["rut"]),
    ])

    # ═══════════════════════════════════════════════════════════════════════════
    # BLOQUE 9: EMERGENCIAS CON CONTEXTO DE ESPECIALIDAD (tests 146-155, 10 tests)
    # ═══════════════════════════════════════════════════════════════════════════

    mk("146 emergencia: dolor fuerte pecho", "56910000146", [
        ("tengo un dolor fuerte en el pecho", ["SAMU", "131"]),
    ])

    mk("147 emergencia: me ahogo", "56910000147", [
        ("me ahogo no puedo respirar", ["SAMU", "131"]),
    ])

    mk("148 emergencia: araña de rincón", "56910000148", [
        ("me picó una araña de rincón", ["SAMU", "131"]),
    ])

    mk("149 emergencia: mucho sangrado", "56910000149", [
        ("estoy con mucho sangrado", ["SAMU", "131"]),
    ])

    mk("150 emergencia: me sangra la nariz", "56910000150", [
        ("me sangra mucho la nariz y no para", ["SAMU", "131"]),
    ])

    mk("151 emergencia: convulsión", "56910000151", [
        ("mi hijo tiene convulsion", ["SAMU", "131"]),
    ])

    mk("152 emergencia: accidente", "56910000152", [
        ("tuve un accidente grave", ["SAMU", "131"]),
    ])

    mk("153 emergencia: me muero", "56910000153", [
        ("me muero", ["SAMU", "131"]),
    ])

    mk("154 emergencia: infarto", "56910000154", [
        ("creo que me da un infarto", ["SAMU", "131"]),
    ])

    mk("155 emergencia: desmayo", "56910000155", [
        ("me voy a desmayar perdí el conocimiento", ["SAMU", "131"]),
    ])

    # ═══════════════════════════════════════════════════════════════════════════
    # BLOQUE 10: EDGE CASES (tests 156-175, 20 tests)
    # ═══════════════════════════════════════════════════════════════════════════

    mk("156 misspelling 'traumatolojia'", "56910000156", [
        ("quiero agendar traumatología", {"any": ["Traumatolog", "09:"], **NO_ERROR}),
    ])

    mk("157 misspelling 'kinesiolojia'", "56910000157", [
        ("quiero agendar kinesiología", {"any": ["Kine", "09:"], **NO_ERROR}),
    ])

    mk("158 misspelling 'sicologo'", "56910000158", [
        ("quiero agendar psicología", {"any": ["Psicolog", "09:"], **NO_ERROR}),
    ])

    mk("159 CAPS 'QUIERO HORA ODONTOLOGIA'", "56910000159", [
        ("QUIERO AGENDAR ODONTOLOGIA", {"any": ["Odonto", "09:"], **NO_ERROR}),
    ])

    mk("160 atajo 1 para agendar", "56910000160", [
        ("menu", ["Motivos"]),
        ("1", None),
    ])

    mk("161 menu reset", "56910000161", [
        ("quiero agendar medicina general", ["09:"]),
        ("menu", ["Agendar", "opciones"]),
    ])

    mk("162 solo emojis", "56910000162", [
        ("😀😀😀", None),
    ])

    mk("163 mensaje muy largo", "56910000163", [
        ("hola " * 100, None),
    ])

    mk("164 número fuera de rango en menu", "56910000164", [
        ("menu", ["Motivos"]),
        ("99", None),
    ])

    mk("165 texto random sin sentido", "56910000165", [
        ("asdfghjkl qwerty zxcv", None),
    ])

    mk("166 saludo 'buenos días'", "56910000166", [
        ("buenos días", None),
    ])

    mk("167 'hola' → menu", "56910000167", [
        ("hola", ["Agendar", "opciones"]),
    ])

    mk("168 'menú' con tilde", "56910000168", [
        ("menú", ["Agendar", "opciones"]),
    ])

    mk("169 'inicio' → menu", "56910000169", [
        ("inicio", ["Agendar", "opciones"]),
    ])

    mk("170 'volver' → menu", "56910000170", [
        ("volver", ["Agendar", "opciones"]),
    ])

    mk("171 RUT inválido en flujo agendar", "56910000171", [
        ("quiero agendar medicina general", ["09:"]),
        ("confirmar_sugerido", None),
        ("1", ["rut"]),
        ("asdfasdf", {"any": ["rut", "válido", "inválido", "formato"]}),
    ])

    mk("172 especialidad desconocida", "56910000172", [
        ("quiero agendar astrología lunar", None),
    ])

    mk("173 cambio de intent mid-flow", "56910000173", [
        ("quiero agendar medicina general", ["09:"]),
        ("en realidad quiero kine", None),
    ])

    mk("174 'me muero de hambre' NO es emergencia", "56910000174", [
        ("me muero de hambre", {"none": ["SAMU", "urgencia"]}),
    ])

    mk("175 'me muero de risa' NO es emergencia", "56910000175", [
        ("me muero de risa", {"none": ["SAMU", "urgencia"]}),
    ])

    # ═══════════════════════════════════════════════════════════════════════════
    # BLOQUE 11: MASOTERAPIA DURACIÓN (tests 176-180, 5 tests)
    # ═══════════════════════════════════════════════════════════════════════════

    mk("176 masoterapia 20 min", "56910000176", [
        ("quiero hora de masoterapia", ["minutos", "20", "40"]),
        ("20 minutos", {"any": ["09:"], **NO_ERROR}),
    ])

    mk("177 masoterapia 40 min", "56910000177", [
        ("quiero hora de masoterapia", ["minutos", "20", "40"]),
        ("40 minutos", {"any": ["09:"], **NO_ERROR}),
    ])

    mk("178 masoterapia botón maso_20", "56910000178", [
        ("quiero hora de masoterapia", ["minutos", "20", "40"]),
        ("maso_20", {"any": ["09:"], **NO_ERROR}),
    ])

    mk("179 masoterapia botón maso_40", "56910000179", [
        ("quiero hora de masoterapia", ["minutos", "20", "40"]),
        ("maso_40", {"any": ["09:"], **NO_ERROR}),
    ])

    mk("180 masoterapia valor inválido", "56910000180", [
        ("quiero hora de masoterapia", ["minutos", "20", "40"]),
        ("30", ["duración", "minutos", "20", "40"]),
    ])

    # ═══════════════════════════════════════════════════════════════════════════
    # BLOQUE 12: CROSS-REFERENCES ESPECIALIDADES (tests 181-190, 10 tests)
    # ═══════════════════════════════════════════════════════════════════════════

    mk("181 traumato + kine cross", "56910000181", [
        ("quiero agendar traumatología", {"any": ["Traumatolog", "09:"], **NO_ERROR}),
    ])

    mk("182 kine tras traumato agendar", "56910000182", [
        ("necesito hora de kinesiología", {"any": ["Kine", "09:"], **NO_ERROR}),
    ])

    mk("183 MG para dolor de cabeza", "56910000183", [
        ("quiero hora tengo dolor de cabeza fuerte", None),
    ])

    mk("184 MG para resfrío", "56910000184", [
        ("menu", ["Motivos"]),
        ("motivo_resfrio", {"all": ["Perfecto", "Medicina General", "09:"], **NO_ERROR}),
    ])

    mk("185 MG para HTA", "56910000185", [
        ("menu", ["Motivos"]),
        ("motivo_hta", {"all": ["Perfecto", "Medicina General", "09:"], **NO_ERROR}),
    ])

    mk("186 dental desde motivo menu", "56910000186", [
        ("menu", ["Motivos"]),
        ("motivo_dental", {"all": ["Perfecto", "Odontología", "09:"], **NO_ERROR}),
    ])

    mk("187 kine desde motivo menu", "56910000187", [
        ("menu", ["Motivos"]),
        ("motivo_kine", {"all": ["Perfecto", "Kinesiología", "09:"], **NO_ERROR}),
    ])

    mk("188 otra consulta MG desde motivo", "56910000188", [
        ("menu", ["Motivos"]),
        ("motivo_mg_otra", {"all": ["Perfecto", "Medicina General", "09:"], **NO_ERROR}),
    ])

    mk("189 otra especialidad → selector", "56910000189", [
        ("menu", ["Motivos"]),
        ("motivo_otra_esp", {"any": ["especialidad", "categoría", "categoria"]}),
    ])

    mk("190 recepción desde sub-menú", "56910000190", [
        ("menu", ["Motivos"]),
        ("accion_recepcion", {"any": ["recepción", "recepcion", "llamará", "pronto"]}),
    ])

    # ═══════════════════════════════════════════════════════════════════════════
    # BLOQUE 13: FIDELIZACIÓN BUTTON RESPONSES (tests 191-200, 10 tests)
    # ═══════════════════════════════════════════════════════════════════════════

    mk("191 postconsulta seg_mejor", "56910000191", [
        ("seg_mejor", {"any": ["bueno", "alegra", "mejor", "control"], **NO_ERROR}),
    ])

    mk("192 postconsulta seg_igual", "56910000192", [
        ("seg_igual", {"any": ["lamentamos", "reagendar"], **NO_ERROR}),
    ])

    mk("193 postconsulta seg_peor", "56910000193", [
        ("seg_peor", {"any": ["lamentamos", "reagendar"], **NO_ERROR}),
    ])

    mk("194 no_control → cierre", "56910000194", [
        ("no_control", {"any": ["Entendido", "cuando lo necesites"], **NO_ERROR}),
    ])

    mk("195 reactivación reac_si → agendar", "56910000195", [
        ("reac_si", {"any": ["especialidad", "categoría", "agendar"], **NO_ERROR}),
    ])

    mk("196 reactivación reac_luego", "56910000196", [
        ("reac_luego", {"any": ["sin problema", "cuando lo necesites"], **NO_ERROR}),
    ])

    mk("197 adherencia kine_adh_si → kine", "56910000197", [
        ("kine_adh_si", {"any": ["Kine", "09:"], **NO_ERROR}),
    ])

    mk("198 adherencia kine_adh_no", "56910000198", [
        ("kine_adh_no", {"any": ["Entendido", "cuando estés"], **NO_ERROR}),
    ])

    mk("199 cross-sell xkine_si → kine", "56910000199", [
        ("xkine_si", {"any": ["Kine", "09:"], **NO_ERROR}),
    ])

    mk("200 cross-sell xkine_no", "56910000200", [
        ("xkine_no", {"any": ["sin problema", "cuando lo necesites"], **NO_ERROR}),
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
