"""
Harness de pruebas offline para flows.handle_message.

- DB SQLite aislada (temp file)
- Medilink mockeado con fixtures deterministas
- Claude (detect_intent, respuesta_faq, clasificar_respuesta_seguimiento) mockeado
- 50 escenarios que cubren los flujos críticos y edge cases

Ejecución:
    PYTHONPATH=app:. venv/bin/python tests/harness_50.py

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

TMP_DB_DIR = Path(tempfile.mkdtemp(prefix="cmc_test_"))
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

async def fake_verificar_slot_disponible(id_profesional, fecha, hora_inicio, hora_fin):
    if FAKE_FAIL_CREAR_CITA["value"]:
        return False  # slot no disponible cuando simula fallo
    return True

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
    elif "masoterapia" in esp:
        slots = _fake_slots("Masoterapia", 59, "Paola Acosta")
    elif "odontolog" in esp or "dentista" in esp:
        slots = _fake_slots("Odontología General", 55, "Dra. Javiera Burgos")
    elif "ortodoncia" in esp or "castillo" in esp:
        slots = _fake_slots("Ortodoncia", 66, "Dra. Daniela Castillo")
    elif "kine" in esp:
        slots = _fake_slots("Kinesiología", 77, "Luis Armijo")
    elif "olavarr" in esp:
        slots = _fake_slots("Medicina General", 1, "Dr. Rodrigo Olavarría")
    elif "psicolog" in esp:
        slots = _fake_slots("Psicología Adulto", 74, "Jorge Montalba")
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
    if any(w in t for w in ["medicina general", "medico", "médico", "doctor general"]):
        esp = "medicina general"
    elif any(w in t for w in ["odontolog", "dentista", "diente", "muela"]):
        esp = "odontología"
    elif "ortodoncia" in t or "castillo" in t or "brackets" in t:
        esp = "ortodoncia"
    elif "kine" in t:
        esp = "kinesiología"
    elif "maso" in t or "masaje" in t:
        esp = "masoterapia"
    elif "olavarr" in t:
        esp = "olavarría"
    elif "psico" in t:
        esp = "psicología"
    elif "nutri" in t:
        esp = "nutrición"

    if any(w in t for w in ["reagend", "cambiar mi hora", "mover mi hora", "reprograma"]):
        return {"intent": "reagendar", "especialidad": esp, "respuesta_directa": None}
    if any(w in t for w in ["lista de espera", "avísame", "avisame", "cupo cuando"]):
        return {"intent": "waitlist", "especialidad": esp, "respuesta_directa": None}
    if any(w in t for w in ["cancelar", "anular", "borrar mi hora", "elimina mi hora"]):
        return {"intent": "cancelar", "especialidad": esp, "respuesta_directa": None}
    if any(w in t for w in ["mis horas", "mis citas", "ver mis", "que tengo agendado"]):
        return {"intent": "ver_reservas", "especialidad": esp, "respuesta_directa": None}
    if any(w in t for w in ["agendar", "agéndame", "reservar", "necesito una hora", "quiero una hora"]):
        return {"intent": "agendar", "especialidad": esp, "respuesta_directa": None}
    if "tapadura" in t:
        return {"intent": "precio", "especialidad": "odontología",
                "respuesta_directa": "Una tapadura (obturación) rellena una caries. Valor: $25.000 a $40.000."}
    if "endodoncia" in t:
        return {"intent": "precio", "especialidad": "endodoncia",
                "respuesta_directa": "La endodoncia es un tratamiento de conducto radicular. Valor: $180.000 a $250.000."}
    if any(w in t for w in ["cuesta", "precio", "valor", "cuanto sale"]):
        return {"intent": "precio", "especialidad": esp, "respuesta_directa": None}
    if any(w in t for w in ["direccion", "dirección", "donde", "dónde", "ubicac", "horario de atenc", "fonasa", "especialid"]):
        return {"intent": "info", "especialidad": esp,
                "respuesta_directa": "Estamos en Monsalve 102, Carampangue. Atendemos Fonasa e Isapre. Especialidades: medicina general, kine, odontología, psicología y más."}
    if any(w in t for w in ["recepcion", "recepción", "hablar con alguien", "humano", "persona"]):
        return {"intent": "humano", "especialidad": esp, "respuesta_directa": None}
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
    mod.verificar_slot_disponible = fake_verificar_slot_disponible

claude_helper.detect_intent = fake_detect_intent
claude_helper.respuesta_faq = fake_respuesta_faq
claude_helper.clasificar_respuesta_seguimiento = fake_clasificar_respuesta_seguimiento
flows.detect_intent = fake_detect_intent
flows.respuesta_faq = fake_respuesta_faq
flows.clasificar_respuesta_seguimiento = fake_clasificar_respuesta_seguimiento

import resilience  # noqa: E402
resilience.is_medilink_down = lambda: False
flows.is_medilink_down = lambda: False

# Mock send_whatsapp para evitar llamadas HTTP reales (usado por alerta peor)
async def fake_send_whatsapp(to, body):
    pass

import messaging  # noqa: E402
messaging.send_whatsapp = fake_send_whatsapp
flows.send_whatsapp = fake_send_whatsapp

# ── Harness ──────────────────────────────────────────────────────────────────
from session import get_session, reset_session, save_profile, log_event, save_privacy_consent  # noqa: E402

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
                    setup: Callable[[], None] | None = None,
                    auto_consent: bool = True):
    """
    steps: list de (input, expectation).
    expectation:
      - None: solo verifica que no crashea y response no vacía
      - list[str]: al menos un substring tiene que aparecer (OR)
      - dict {"any": [...]}: OR
      - dict {"all": [...]}: AND
      - dict {"none": [...]}: ninguno de los substrings (util para no "no te entendí")
      - dict con varios de los anteriores combinados
    """
    reset_session(phone)
    # Pre-acepta el consent para que los tests existentes no sean interceptados
    # por el gate de privacidad (se testea por separado en los casos CONSENT-*).
    if auto_consent:
        save_privacy_consent(phone, "accepted", method="test")
    else:
        # Limpia cualquier consent previo de este phone para forzar el gate
        from session import _conn as _session_conn
        with _session_conn() as _c:
            _c.execute("DELETE FROM privacy_consents WHERE phone=?", (phone,))
            _c.commit()
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

        # Expectativas
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

    # ── AGENDAR (15) ────────────────────────────────────────────────────────
    mk("01 agendar medicina general intent", "56900000001", [
        ("hola", ["Agendar", "opciones"]),
        ("quiero agendar medicina general", {"any": ["Medicina", "09:"], **NO_ERROR}),
        ("confirmar_sugerido", ["Fonasa", "Particular"]),
        ("1", ["RUT"]),
        ("11111111-1", ["Juan", "confirm"]),
        ("confirmar", ["reserv", "confirm", "✅", "cita"]),
    ])

    mk("02 agendar odontologia via lista", "56900000002", [
        ("menu", ["Motivos"]),
        ("1", ["especialidad", "categoría", "categoria"]),
        ("odontología", {"any": ["Odonto", "09:"], **NO_ERROR}),
        # Odonto es particular-only → salta modalidad, va directo a RUT
        ("confirmar_sugerido", ["RUT"]),
        ("11111111-1", ["confirm"]),
        ("confirmar", ["reserv", "✅", "confirm"]),
    ])

    mk("03 agendar paciente nuevo registro (1 mensaje)", "56900000003", [
        ("quiero agendar kine", {"any": ["Kine", "09:"], **NO_ERROR}),
        ("confirmar_sugerido", ["Fonasa"]),
        ("1", ["RUT"]),
        ("99999999-9", ["Nombre", "Sexo", "nacimiento"]),
        ("Pedro Pérez González, M, 15/03/1990", {"any": ["confirm", "cita", "reserv", "Registrad"], **NO_ERROR}),
        ("confirmar", ["reserv", "✅", "cita"]),
    ])

    mk("04 agendar texto libre doctor", "56900000004", [
        ("necesito una hora con el doctor", {"any": ["Medicina", "09:"], **NO_ERROR}),
    ])

    mk("05 agendar atajo 1 -> lista esp", "56900000005", [
        ("menu", ["Motivos"]),
        ("1", ["especialidad"]),
        ("medicina general", {"any": ["09:"], **NO_ERROR}),
    ])

    mk("06 agendar doctor específico castillo", "56900000006", [
        ("quiero agendar con castillo", {"any": ["Ortodoncia", "Castillo", "09:"], **NO_ERROR}),
    ])

    mk("07 agendar ver todos", "56900000007", [
        ("quiero agendar odontología", ["09:"]),
        ("ver todos", {"any": ["09:", "10:"], **NO_ERROR}),
    ])

    mk("08 agendar otro día", "56900000008", [
        ("quiero agendar odontología", ["09:"]),
        ("otro día", None),  # acepta cualquier respuesta no-crash
    ])

    mk("09 agendar masoterapia con duración", "56900000009", [
        ("quiero una hora de masoterapia", ["minutos", "20", "40"]),
        ("20 minutos", {"any": ["09:"], **NO_ERROR}),
    ])

    mk("10 agendar y abortar con menu", "56900000010", [
        ("quiero agendar medicina general", ["09:"]),
        ("menu", ["Agendar", "opciones"]),
    ])

    mk("11 agendar RUT inválido", "56900000011", [
        ("quiero agendar medicina general", ["09:"]),
        ("confirmar_sugerido", None),
        ("1", ["RUT"]),
        ("asdfasdf", {"any": ["rut", "válido", "inválido", "formato"]}),
    ])

    mk("12 agendar especialidad desconocida", "56900000012", [
        ("quiero agendar astrología lunar", None),  # no debe crashear
    ])

    mk("13 agendar psicología", "56900000013", [
        ("quiero agendar psicología", {"any": ["Psico", "09:"], **NO_ERROR}),
    ])

    mk("14 agendar ortodoncia", "56900000014", [
        ("quiero agendar ortodoncia", {"any": ["Ortodoncia", "09:"], **NO_ERROR}),
    ])

    mk("15 agendar cambio de intent mid-flow", "56900000015", [
        ("quiero agendar medicina general", ["09:"]),
        ("en realidad quiero kine", None),
    ])

    # ── CANCELAR (7) ────────────────────────────────────────────────────────
    mk("16 cancelar con 1 cita", "56900000016", [
        ("quiero cancelar mi hora", ["rut"]),
        ("11111111-1", {"any": ["Abarca", "20", "cancel"], **NO_ERROR}),
        ("1", {"any": ["confirm", "seguro", "cancel"], **NO_ERROR}),
        ("si", {"any": ["cancel", "anul"], **NO_ERROR}),
    ], setup=setup_una_cita)

    mk("17 cancelar multi citas", "56900000017", [
        ("cancelar", ["rut"]),
        ("11111111-1", {"any": ["Abarca", "Burgos"], **NO_ERROR}),
        ("1", {"any": ["Abarca", "confirm", "seguro"], **NO_ERROR}),
        ("si", ["cancel"]),
    ], setup=setup_multi_citas)

    mk("18 cancelar sin citas", "56900000018", [
        ("cancelar", ["rut"]),
        ("11111111-1", {"any": ["no tienes", "sin", "no hay", "no encontré"]}),
    ])

    mk("19 cancelar desde atajo 3", "56900000019", [
        ("menu", ["Motivos"]),
        ("3", ["rut"]),
        ("11111111-1", {"any": ["Abarca", "cancel"], **NO_ERROR}),
    ], setup=setup_una_cita)

    mk("20 cancelar y decir 'no' en confirm", "56900000020", [
        ("cancelar", ["rut"]),
        ("11111111-1", {"any": ["Abarca"], **NO_ERROR}),
        ("1", {"any": ["confirm", "seguro"], **NO_ERROR}),
        ("no", {"any": ["mantener", "mant", "listo", "sin cancelar", "menú", "menu"], **NO_ERROR}),
    ], setup=setup_una_cita)

    mk("21 cancelar RUT inválido", "56900000021", [
        ("cancelar", ["rut"]),
        ("xxx", {"any": ["rut", "válido", "formato"]}),
    ])

    mk("22 cancelar paciente no registrado", "56900000022", [
        ("cancelar", ["rut"]),
        ("98765432-1", {"any": ["no", "encontr", "registrado", "sin"]}),
    ])

    # ── REAGENDAR (7) ────────────────────────────────────────────────────────
    mk("23 reagendar atajo 2", "56900000023", [
        ("menu", ["Motivos"]),
        ("2", {"any": ["rut"], **NO_ERROR}),
    ])

    mk("24 reagendar con perfil guardado", "56900000024", [
        ("quiero cambiar mi hora", {"any": ["Abarca", "cita", "elegir", "reagend"], **NO_ERROR}),
    ], setup=lambda: (save_profile("56900000024", "11111111-1", "Juan Prueba Test"), setup_una_cita()))

    mk("25 reagendar sin citas activas", "56900000025", [
        ("quiero reagendar", ["rut"]),
        ("11111111-1", {"any": ["no", "sin", "tienes"]}),
    ])

    mk("26 reagendar flujo completo", "56900000026", [
        ("reagendar", ["rut"]),
        ("11111111-1", {"any": ["Abarca", "cita", "reagend"], **NO_ERROR}),
        ("1", {"any": ["09:", "reagend"], **NO_ERROR}),
        ("confirmar_sugerido", {"any": ["Fonasa"], **NO_ERROR}),
        # WAIT_MODALIDAD detecta rut_conocido → atajo "¿Agendo con tus datos?"
        ("1", {"any": ["Agendo con tus datos", "continuar"], **NO_ERROR}),
        ("si", {"any": ["confirm", "Estás a un paso"], **NO_ERROR}),
        ("si", {"any": ["reagend", "✅", "reserv"], **NO_ERROR}),
    ], setup=setup_una_cita)

    mk("27 reagendar abort con menu", "56900000027", [
        ("reagendar", ["rut"]),
        ("menu", ["Agendar", "opciones"]),
    ])

    mk("28 reagendar texto libre", "56900000028", [
        ("quiero mover mi hora del lunes", {"any": ["rut", "reagend"], **NO_ERROR}),
    ])

    mk("29 reagendar falla crear nueva", "56900000029", [
        ("reagendar", ["rut"]),
        ("11111111-1", {"any": ["Abarca"], **NO_ERROR}),
        ("1", {"any": ["09:"], **NO_ERROR}),
        ("confirmar_sugerido", None),
        # WAIT_MODALIDAD con rut_conocido → "¿Agendo con tus datos?"
        ("1", None),
        ("si", None),  # confirmar datos
        ("si", {"any": ["ya fue tomada", "encontré otra", "reservo"]}),
    ], setup=lambda: (setup_una_cita(), FAKE_FAIL_CREAR_CITA.update(value=True)))

    # ── WAITLIST (5) ────────────────────────────────────────────────────────
    mk("30 waitlist atajo 5", "56900000030", [
        ("menu", ["espera"]),
        ("5", {"any": ["especialidad", "espera"], **NO_ERROR}),
        ("odontología", {"any": ["espera", "inscrib", "✅", "rut"], **NO_ERROR}),
    ])

    mk("31 waitlist con especialidad", "56900000031", [
        ("quiero lista de espera para ortodoncia", {"any": ["espera", "inscrib", "✅"], **NO_ERROR}),
    ])

    mk("32 waitlist oferta automática sin cupo", "56900000032", [
        ("quiero agendar medicina general", {"any": ["encontré", "espera", "cupo", "disponib"], **NO_ERROR}),
        ("waitlist_si", {"any": ["rut", "inscrib", "espera"], **NO_ERROR}),
    ], setup=lambda: FAKE_SIN_SLOTS.update(value=True))

    mk("33 waitlist inscripción end-to-end", "56900000033", [
        ("lista de espera para odontología", {"any": ["espera", "inscrib", "rut"], **NO_ERROR}),
        ("waitlist_si", {"any": ["rut", "11111111", "inscrib"], **NO_ERROR}),
        ("11111111-1", {"any": ["inscrib", "espera", "✅"], **NO_ERROR}),
    ])

    mk("34 waitlist intent libre 'avísame'", "56900000034", [
        ("avísame cuando haya cupo con castillo", {"any": ["espera", "inscrib", "Ortodoncia"], **NO_ERROR}),
    ])

    # ── VER RESERVAS (3) ────────────────────────────────────────────────────
    mk("35 ver con 1 cita", "56900000035", [
        ("quiero ver mis citas", ["rut"]),
        ("11111111-1", {"any": ["Abarca", "20", "10:00"], **NO_ERROR}),
    ], setup=setup_una_cita)

    mk("36 ver con multi citas", "56900000036", [
        ("mis horas", ["rut"]),
        ("11111111-1", {"any": ["Abarca", "Burgos"], **NO_ERROR}),
    ], setup=setup_multi_citas)

    mk("37 ver sin citas", "56900000037", [
        ("que tengo agendado", ["rut"]),
        ("11111111-1", {"any": ["no tienes", "no hay", "sin"]}),
    ])

    # ── FAQ (5) ─────────────────────────────────────────────────────────────
    mk("38 FAQ tapadura", "56900000038", [
        ("¿qué es una tapadura?", {"any": ["tapadura", "obturación", "caries", "agendar"], **NO_ERROR}),
    ])

    mk("39 FAQ endodoncia", "56900000039", [
        ("qué es una endodoncia", {"any": ["endodoncia", "conducto"], **NO_ERROR}),
    ])

    mk("40 FAQ fonasa", "56900000040", [
        ("atienden fonasa", {"any": ["fonasa", "atend"], **NO_ERROR}),
    ])

    mk("41 FAQ ubicación", "56900000041", [
        ("dónde están ubicados", {"any": ["Monsalve", "Carampangue", "ubicac"], **NO_ERROR}),
    ])

    mk("42 FAQ especialidades", "56900000042", [
        ("qué especialidades tienen", {"any": ["medicina", "kine", "odonto", "especialidad"], **NO_ERROR}),
    ])

    # ── EMERGENCIAS (3) ─────────────────────────────────────────────────────
    mk("43 emergencia: me ahogo", "56900000043", [
        ("me ahogo no puedo respirar", ["SAMU", "131"]),
    ])

    mk("44 emergencia: araña de rincón", "56900000044", [
        ("me picó una araña de rincón", ["SAMU", "131"]),
    ])

    mk("45 emergencia: mucho dolor", "56900000045", [
        ("tengo mucho dolor en el pecho", ["SAMU", "131"]),
    ])

    # ── EDGE CASES (5) ──────────────────────────────────────────────────────
    mk("46 solo emojis", "56900000046", [
        ("😀😀😀", None),  # no crash
    ])

    mk("47 mensaje muy largo", "56900000047", [
        ("hola " * 100, None),  # no crash (puede ser rate limited upstream)
    ])

    mk("48 saludo 'buenos días'", "56900000048", [
        ("buenos días", None),
    ])

    mk("49 número fuera de rango en menu", "56900000049", [
        ("menu", ["Motivos"]),
        ("99", None),
    ])

    mk("50 texto random sin sentido", "56900000050", [
        ("asdfghjkl qwerty", None),
    ])

    # ═════════════════════════════════════════════════════════════════════════
    # BUG CONFIRMATION TESTS — estos DEBEN fallar si el bug existe.
    # Si en el futuro pasan, el bug fue arreglado.
    # ═════════════════════════════════════════════════════════════════════════

    mk("BUG-01 'si' en WAIT_SLOT debe confirmar sugerido", "56900000101", [
        ("quiero agendar medicina general", ["09:"]),
        # Tras el slot sugerido, el usuario escribe "si" (en vez de tocar el botón).
        # ESPERADO: debería preguntar modalidad (Fonasa/Particular).
        # BUG ACTUAL: cae en el frustration detector "no te entendí".
        ("si", {"any": ["Fonasa", "Particular", "modalidad"], **NO_ERROR}),
    ])

    mk("BUG-02 'sí' acentuado en WAIT_SLOT", "56900000102", [
        ("quiero agendar kine", ["09:"]),
        ("sí", {"any": ["Fonasa", "Particular"], **NO_ERROR}),
    ])

    mk("BUG-03 'confirmo' en WAIT_SLOT", "56900000103", [
        ("quiero agendar odontología", ["09:"]),
        # Odontología es solo particular → salta directo al RUT
        ("confirmo", {"any": ["RUT", "rut"], **NO_ERROR}),
    ])

    mk("BUG-04 'no' en WAIT_CITA_CANCELAR aborta", "56900000104", [
        ("cancelar", ["rut"]),
        ("11111111-1", {"any": ["Abarca"]}),
        # Usuario se arrepiente y escribe "no". ESPERADO: aborta el flujo.
        # BUG ACTUAL: "Elige un número entre 1 y 1".
        ("no", {"any": ["menú", "listo", "sin cancelar", "mantener", "menu", "entendido"],
                "none": ["entre 1 y"]}),
    ], setup=setup_una_cita)

    mk("BUG-05 'no' en WAIT_CITA_REAGENDAR aborta", "56900000105", [
        ("reagendar", ["rut"]),
        ("11111111-1", {"any": ["Abarca"]}),
        ("no", {"any": ["menu", "menú", "listo", "sin", "dejamos", "entendido"],
                "none": ["entre 1 y"]}),
    ], setup=setup_una_cita)

    mk("BUG-06 emergencia 'dolor fuerte en el pecho'", "56900000106", [
        # EMERGENCIAS tiene "dolor de pecho fuerte" y "dolor muy fuerte" pero
        # el usuario típicamente dice "dolor fuerte en el pecho". Debería disparar.
        ("tengo un dolor fuerte en el pecho", ["SAMU", "131"]),
    ])

    mk("BUG-07 emergencia 'mucho sangrado'", "56900000107", [
        # EMERGENCIAS tiene "sangrado abundante" y "hemorragia" pero no "mucho sangrado"
        ("estoy con mucho sangrado", ["SAMU", "131"]),
    ])

    mk("BUG-08 emergencia 'me sangra mucho la nariz'", "56900000108", [
        ("me sangra mucho la nariz y no para", ["SAMU", "131"]),
    ])

    mk("BUG-09 amenaza vital 'me muero'", "56900000109", [
        # Caso real de producción: paciente escribió "Me muero" y el bot lo
        # clasificó como "humano" (derivar a recepción) en vez de urgencia.
        ("me muero", ["SAMU", "131"]),
    ])

    mk("BUG-09b no confundir con coloquialismo 'me muero de hambre'", "56900000110", [
        # Regression: el set EMERGENCIAS con substring "me muero" disparaba
        # SAMU para "me muero de hambre" / "me muero de risa" (false positive).
        # Con `EMERGENCIAS_VITAL_PATRONES` + lookahead negativo no debería.
        ("me muero de hambre", {"none": ["SAMU", "urgencia"]}),
    ])

    mk("BUG-10 crisis salud mental 'me quiero matar'", "56900000111", [
        # Ideación suicida debe disparar mensaje diferenciado con
        # Salud Responde 600 360 7777 además de SAMU 131.
        ("me quiero matar", {"all": ["Salud Responde", "600 360 7777"]}),
    ])

    mk("BUG-10b crisis salud mental 'quiero acabar con mi vida'", "56900000112", [
        ("ya no aguanto mas, quiero acabar con mi vida",
         {"all": ["Salud Responde", "600 360 7777"]}),
    ])

    mk("BUG-11 HUMAN_TAKEOVER + texto clinico no dice 'Recibido'", "56900000113", [
        # Paciente pide hablar con recepción, queda en HUMAN_TAKEOVER, luego
        # escribe "diabetes". Antes respondía "Recibido 🙏" sin reconocer el
        # contenido clínico. Ahora debe referenciar el canal de urgencia.
        ("quiero hablar con alguien", {"any": ["recepción", "recepcion"]}),
        ("diabetes", {"any": ["SAMU", "urgente", "📞"],
                      "none": ["Recibido 🙏"]}),
    ])

    mk("BUG-12 HUMAN_TAKEOVER + dolor pecho fuerte → emergencia", "56900000114", [
        ("quiero hablar con alguien", {"any": ["recepción", "recepcion"]}),
        # Estando en HUMAN_TAKEOVER, el paciente escribe un mensaje que es
        # emergencia. La capa de emergencias está ANTES del handler de
        # HUMAN_TAKEOVER en handle_message, así que debe disparar SAMU.
        ("me duele el pecho fuerte", ["SAMU", "131"]),
    ])

    mk("BUG-13 override defensivo: humano + danger kw → emergencia", "56900000115", [
        # Simulamos que Claude (vía fake_detect_intent) clasifica como
        # "humano" por tener "hablar con alguien", pero el texto tiene
        # "super mal" en _DANGER_KW. El override debe reroutear a emergencia.
        ("hablar con alguien me siento super mal", ["SAMU", "131"]),
    ])

    # ── Normalización global: typos/abreviaciones WhatsApp rural ────────────
    # Estos tests cubren el wiring de `tl_norm` en las ramas hard-coded de
    # handle_message (emergencias, comandos, AFIRMACIONES, NEGACIONES).

    mk("NORM-01 emergencia 'dlor fuerte d pcho'", "56900000301", [
        # "dlor d pcho" es cómo realmente escriben los pacientes en WhatsApp.
        # Normalización: "dlor"→"dolor", "d"→"de", "pcho"→"pecho" → dispara
        # el patrón regex `dolor.{0,20}fuerte.{0,20}pecho`.
        ("tngo dlor fuerte d pcho", ["SAMU", "131"]),
    ])

    mk("NORM-02 emergencia 'sangrao mucho'", "56900000302", [
        # Participio rural: "sangrao" → "sangrado" vía regex de participios,
        # y luego "mucho sangrado" matchea el set EMERGENCIAS.
        ("estoy sangrao mucho", ["SAMU", "131"]),
    ])

    mk("NORM-03 comando global sin tilde 'menú'", "56900000303", [
        ("quiero agendar medicina general", ["09:"]),
        # Paciente escribe "menú" con tilde. Debe resetear sesión.
        ("menú", ["Agendar", "opciones"]),
    ])

    mk("NORM-04 afirmación con abreviación 'dale'", "56900000304", [
        ("quiero agendar medicina general", ["09:"]),
        # "dale" ya está en AFIRMACIONES, sirve de sanity check para
        # confirmar que el branch `tl_norm in AFIRMACIONES` no rompe nada.
        ("dale", {"any": ["Fonasa", "Particular"], **NO_ERROR}),
    ])

    mk("NORM-05 FONASA/PARTICULAR acepta 'fonaza' sin tilde", "56900000305", [
        ("quiero agendar medicina general", ["09:"]),
        ("si", {"any": ["Fonasa", "Particular"], **NO_ERROR}),
        ("fonasa", ["RUT"]),
    ])

    mk("NORM-06 negación 'nop' en CONFIRMING_CITA", "56900000306", [
        ("quiero agendar medicina general", ["09:"]),
        ("si", {"any": ["Fonasa", "Particular"]}),
        ("1", ["RUT"]),
        ("11111111-1", ["confirm"]),
        ("nop", {"any": ["otro día", "menu", "problema"], **NO_ERROR}),
    ])

    # ── Menú nuevo: motivos rápidos + acciones agrupadas ───────────────────
    mk("MENU-01 motivo_kine → slot directo con pausa", "56900000401", [
        ("menu", ["Motivos"]),
        # Tap en motivo kine → saludo prefix + slot + precio en un solo mensaje
        ("motivo_kine", {"all": ["Perfecto", "Kinesiología", "09:", "$"], **NO_ERROR}),
    ])

    mk("MENU-02 motivo_resfrio → MG con pausa", "56900000402", [
        ("menu", ["Motivos"]),
        ("motivo_resfrio", {"all": ["Perfecto", "Medicina General", "09:"], **NO_ERROR}),
    ])

    mk("MENU-03 motivo_otra_esp → selector tradicional", "56900000403", [
        ("menu", ["Motivos"]),
        ("motivo_otra_esp", {"any": ["especialidad", "categoría", "categoria"]}),
    ])

    mk("MENU-04 accion_cambiar → sub-menú reagendar/cancelar", "56900000404", [
        ("menu", ["Motivos"]),
        ("accion_cambiar", {"all": ["Reagendar", "Cancelar"], **NO_ERROR}),
        # Tap "Cancelar" (id=3) desde el sub-menú debe iniciar flujo cancelar
        ("3", ["rut"]),
    ])

    mk("MENU-05 accion_mis_citas → sub-menú ver/espera", "56900000405", [
        ("menu", ["Motivos"]),
        ("accion_mis_citas", {"all": ["reservas", "espera"], **NO_ERROR}),
        # Tap "Ver mis reservas" (id=4) desde el sub-menú
        ("4", ["rut"]),
    ])

    mk("MENU-06 accion_recepcion → handoff humano", "56900000406", [
        ("menu", ["Motivos"]),
        ("accion_recepcion", {"any": ["recepción", "recepcion", "llamará", "pronto"]}),
    ])

    # ── Confirmación pre-cita (respuesta al recordatorio 09:00) ─────────────
    def setup_cita_bot_confirm():
        """Inserta una cita_bot en SQLite para probar los botones del recordatorio."""
        from session import save_cita_bot, save_profile
        phone_p = "56900000201"
        save_profile(phone_p, "11.111.111-1", "Juan Pérez")
        save_cita_bot(phone_p, "9001", "Medicina General", "Dr. Andrés Abarca",
                      "2026-04-11", "10:00:00", "particular")

    def setup_cita_bot_reagendar():
        from session import save_cita_bot, save_profile
        phone_p = "56900000202"
        save_profile(phone_p, "11.111.111-1", "Juan Pérez")
        save_cita_bot(phone_p, "9002", "Medicina General", "Dr. Andrés Abarca",
                      "2026-04-11", "10:00:00", "particular")

    def setup_cita_bot_cancelar():
        from session import save_cita_bot, save_profile
        phone_p = "56900000203"
        save_profile(phone_p, "11.111.111-1", "Juan Pérez")
        save_cita_bot(phone_p, "9003", "Medicina General", "Dr. Andrés Abarca",
                      "2026-04-11", "10:00:00", "particular")

    mk("51 confirma asistencia tocando botón", "56900000201", [
        ("cita_confirm:9001", {"any": ["confirmada", "esperamos", "✅"], **NO_ERROR}),
    ], setup=setup_cita_bot_confirm)

    mk("52 cambiar hora desde recordatorio", "56900000202", [
        # El botón dispara reagendar con la especialidad pre-cargada → debe mostrar slots
        ("cita_reagendar:9002", {"any": ["Medicina", "09:", "10:", "slot", "horario", "fecha"],
                                 **NO_ERROR}),
    ], setup=setup_cita_bot_reagendar)

    mk("53 no podré ir → cancelación pre-rellenada", "56900000203", [
        ("cita_cancelar:9003", {"any": ["cancelar", "mantener", "Sí, cancelar"], **NO_ERROR}),
        ("si", {"any": ["cancelada", "✅"], **NO_ERROR}),
    ], setup=setup_cita_bot_cancelar)

    mk("54 botón confirmar con cita inexistente", "56900000204", [
        ("cita_confirm:99999", ["no encontré", "recepción"]),
    ])

    # ── Registro paciente nuevo: variantes fecha nacimiento ──────────────
    mk("REG-01 fecha dd/mm/yyyy", "56900000501", [
        ("quiero agendar kine", {"any": ["Kine", "09:"], **NO_ERROR}),
        ("confirmar_sugerido", ["Fonasa"]),
        ("1", ["RUT"]),
        ("99999999-9", ["Nombre", "Sexo", "nacimiento"]),
        ("Ana López, F, 15/03/1990", {"any": ["Registrad", "confirm"], **NO_ERROR}),
    ])

    mk("REG-02 fecha texto '15 de marzo de 1990'", "56900000502", [
        ("quiero agendar kine", {"any": ["Kine", "09:"], **NO_ERROR}),
        ("confirmar_sugerido", ["Fonasa"]),
        ("1", ["RUT"]),
        ("99999999-9", ["Nombre", "Sexo", "nacimiento"]),
        ("Luis Pérez, M, 15 de marzo de 1990", {"any": ["Registrad", "confirm"], **NO_ERROR}),
    ])

    mk("REG-03 fecha con guión dd-mm-yyyy", "56900000503", [
        ("quiero agendar kine", {"any": ["Kine", "09:"], **NO_ERROR}),
        ("confirmar_sugerido", ["Fonasa"]),
        ("1", ["RUT"]),
        ("99999999-9", ["Nombre", "Sexo", "nacimiento"]),
        ("María Soto, F, 15-03-1990", {"any": ["Registrad", "confirm"], **NO_ERROR}),
    ])

    mk("REG-04 fecha corta dd/mm/yy", "56900000504", [
        ("quiero agendar kine", {"any": ["Kine", "09:"], **NO_ERROR}),
        ("confirmar_sugerido", ["Fonasa"]),
        ("1", ["RUT"]),
        ("99999999-9", ["Nombre", "Sexo", "nacimiento"]),
        ("Juan Muñoz, M, 15/03/90", {"any": ["Registrad", "confirm"], **NO_ERROR}),
    ])

    mk("REG-05 fecha 8 digitos pegados", "56900000505", [
        ("quiero agendar kine", {"any": ["Kine", "09:"], **NO_ERROR}),
        ("confirmar_sugerido", ["Fonasa"]),
        ("1", ["RUT"]),
        ("99999999-9", ["Nombre", "Sexo", "nacimiento"]),
        ("Rosa Díaz, F, 15031990", {"any": ["Registrad", "confirm"], **NO_ERROR}),
    ])

    mk("REG-06 nombre inválido → pide de nuevo", "56900000506", [
        ("quiero agendar kine", {"any": ["Kine", "09:"], **NO_ERROR}),
        ("confirmar_sugerido", ["Fonasa"]),
        ("1", ["RUT"]),
        ("99999999-9", ["Nombre", "Sexo", "nacimiento"]),
        ("123", ["No reconocí"]),
        ("Carlos Vega, M, 01/01/1985", {"any": ["Registrad", "confirm"], **NO_ERROR}),
    ])

    mk("REG-07 solo nombre y sexo (sin fecha)", "56900000507", [
        ("quiero agendar kine", {"any": ["Kine", "09:"], **NO_ERROR}),
        ("confirmar_sugerido", ["Fonasa"]),
        ("1", ["RUT"]),
        ("99999999-9", ["Nombre", "Sexo", "nacimiento"]),
        ("Sofía Paredes, F", {"any": ["Registrad", "confirm"], **NO_ERROR}),
    ])

    mk("REG-08 solo nombre → registra igual", "56900000508", [
        ("quiero agendar kine", {"any": ["Kine", "09:"], **NO_ERROR}),
        ("confirmar_sugerido", ["Fonasa"]),
        ("1", ["RUT"]),
        ("99999999-9", ["Nombre", "Sexo", "nacimiento"]),
        ("Pedro Nada", {"any": ["Registrad", "confirm"], **NO_ERROR}),
    ])

    mk("REG-09 fecha con mes abreviado '15 mar 1990'", "56900000509", [
        ("quiero agendar kine", {"any": ["Kine", "09:"], **NO_ERROR}),
        ("confirmar_sugerido", ["Fonasa"]),
        ("1", ["RUT"]),
        ("99999999-9", ["Nombre", "Sexo", "nacimiento"]),
        ("Tomás Rojas, M, 15 mar 1990", {"any": ["Registrad", "confirm"], **NO_ERROR}),
    ])

    # ── Tercero (booking for another person) ────────────────────────────────
    mk("TERC-01 tercero sin perfil → pide RUT directo (sin fricción)", "56900000601", [
        ("quiero agendar kine", {"any": ["Kine", "09:"], **NO_ERROR}),
        ("confirmar_sugerido", ["Fonasa"]),
        ("1", ["RUT"]),
        # Nuevo flujo: "otra persona" → va directo a pedir RUT del paciente a atender
        ("otra persona", {"any": ["RUT", "atender"], **NO_ERROR}),
        ("99999999-9", ["Nombre", "Sexo", "nacimiento"]),
        ("Daniel López, M, 01/01/2000", {"any": ["Daniel", "confirm", "Registrad"], **NO_ERROR}),
    ])

    mk("TERC-02 tercero con perfil conocido → fast-track + cambiar datos", "56900000602", [
        ("quiero agendar kine", {"any": ["Kine", "09:"], **NO_ERROR}),
        # Fast-track: perfil conocido → salta directo a CONFIRMING_CITA
        ("confirmar_sugerido", {"any": ["reservo", "confirmo", "Juan"], **NO_ERROR}),
        # Quiere agendar para otra persona → "Cambiar algo" → flujo completo
        ("cambiar_datos", {"any": ["Fonasa", "Particular"], **NO_ERROR}),
        # WAIT_MODALIDAD detecta rut_conocido → atajo, pero quiere para otra persona
        ("1", {"any": ["Agendo con tus datos", "continuar"], **NO_ERROR}),
        # En WAIT_RUT_AGENDAR escribe "otra persona"
        ("otra persona", ["RUT", "atender"]),
        ("99999999-9", ["Nombre", "Sexo", "nacimiento"]),
        ("Carlos Pérez, M, 05/06/1985", {"any": ["Carlos", "confirm", "Registrad"], **NO_ERROR}),
    ], setup=lambda: save_profile("56900000602", "11111111-1", "María Gómez")),

    # ── Fast-track paciente recurrente (salta Fonasa/RUT) ─────────────────
    mk("FT-01 fast-track: paciente recurrente salta 3 pasos", "56900000701", [
        ("quiero agendar medicina general", {"any": ["09:", "encontré"], **NO_ERROR}),
        # Fast-track: perfil + RUT conocido → salta Fonasa + Para ti + RUT
        ("confirmar_sugerido", {"any": ["reservo", "confirmo", "Juan"], **NO_ERROR}),
        # Confirma directo → ¡Listo!
        ("si", {"any": ["reserv", "✅", "Listo"], **NO_ERROR}),
    ], setup=lambda: save_profile("56900000701", "11111111-1", "Juan Prueba Test")),

    mk("FT-02 fast-track: 'cambiar algo' → flujo completo", "56900000702", [
        ("quiero agendar kine", {"any": ["Kine", "09:"], **NO_ERROR}),
        # Fast-track ofrece confirmar
        ("confirmar_sugerido", {"any": ["reservo", "confirmo", "Juan"], **NO_ERROR}),
        # Toca "Cambiar algo" → vuelve a Fonasa/Particular
        ("cambiar_datos", {"any": ["Fonasa", "Particular"], **NO_ERROR}),
        # WAIT_MODALIDAD detecta rut_conocido → atajo "¿Agendo con tus datos?"
        ("1", {"any": ["Agendo con tus datos", "continuar"], **NO_ERROR}),
        ("si", {"any": ["reservo", "confirmo"], **NO_ERROR}),
        ("si", {"any": ["reserv", "✅", "Listo"], **NO_ERROR}),
    ], setup=lambda: save_profile("56900000702", "11111111-1", "Juan Prueba Test")),

    mk("FT-03 sin perfil → flujo normal (no fast-track)", "56900000703", [
        ("quiero agendar medicina general", {"any": ["09:", "encontré"], **NO_ERROR}),
        # Sin perfil → va a Fonasa/Particular como antes
        ("confirmar_sugerido", {"any": ["Fonasa", "Particular"], **NO_ERROR}),
    ]),

    # ── Ley 19.628 — consent inline + derecho al olvido ─────────────────────
    # El consent ya NO bloquea al inicio. Se registra inline al dar el RUT.

    # Upgrade el resto a la tupla de 5 para uniformidad con auto_consent
    _results_normalized = []
    for r in results:
        if len(r) == 4:
            _results_normalized.append((*r, True))
        else:
            _results_normalized.append(r)
    results[:] = _results_normalized

    results.append(("CONSENT-01 primer msg sin consent gate → menú normal", "56999000001", [
        ("hola", {"any": ["Carampangue", "agendar", "centro médico", "opciones"],
                  **NO_ERROR}),
    ], None, False))

    results.append(("CONSENT-02 nota privacidad al pedir RUT", "56999000002", [
        ("quiero agendar hora medicina general", {"any": ["horario", "slot", "agenda"],
                                                   **NO_ERROR}),
        ("1", {"any": ["Fonasa", "Particular"], **NO_ERROR}),
        ("fonasa", {"any": ["Para mí", "otra persona"], **NO_ERROR}),
        ("para mi", {"any": ["RUT", "privacidad"], **NO_ERROR}),
    ], None, False))

    results.append(("CONSENT-03 emergencia sin consent OK", "56999000003", [
        ("me duele mucho el pecho", {"any": ["SAMU", "131", "urgencia"],
                                      **NO_ERROR}),
    ], None, False))

    results.append(("CONSENT-04 STOP revoca consent", "56999000004", [
        ("stop", {"any": ["No recibirás", "marketing", "aceptar"], **NO_ERROR}),
    ], None, True))

    results.append(("CONSENT-05 'borrar mis datos' inicia proceso", "56999000005", [
        ("borrar mis datos", {"any": ["solicitud de borrado", "validar",
                                       "identidad"], **NO_ERROR}),
    ], None, True))

    # ── Run ─────────────────────────────────────────────────────────────────
    passed = 0
    failed = 0
    for item in results:
        if len(item) == 5:
            name, phone, steps, setup, auto_consent = item
        else:
            name, phone, steps, setup = item
            auto_consent = True
        ok = await run_convo(name, phone, steps, setup, auto_consent=auto_consent)
        mark = "✅" if ok else "❌"
        print(f"{mark} {name}")
        if ok:
            passed += 1
        else:
            failed += 1

    print()
    print(f"── Total: {passed}/{len(results)} passed, {failed} failed ──")
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
