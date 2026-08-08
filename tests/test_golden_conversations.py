"""
Golden tests de conversaciones reales / conocidas del chatbot CMC.

Cada fixture JSON en tests/golden/ describe una conversación con steps
de entrada y assertions sobre la respuesta. Este harness los itera,
ejecuta handle_message con mocks deterministas y verifica cada step.

Ejecución:
    pytest tests/test_golden_conversations.py -v
    PYTHONPATH=app:. pytest tests/test_golden_conversations.py -v

Fixtures xfail: bugs conocidos pendientes de fix se marcan con
@pytest.mark.xfail(reason="...") y NO se eliminan.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT))

# ── DB aislada ────────────────────────────────────────────────────────────────
_TMP_DB_DIR = Path(tempfile.mkdtemp(prefix="cmc_golden_"))
_TMP_DB = _TMP_DB_DIR / "sessions.db"
os.environ["SESSIONS_DB"] = str(_TMP_DB)
os.environ.setdefault("SQLCIPHER_KEY", "")

import session as _session_mod
_session_mod.DB_PATH = _TMP_DB

# ── Fecha fija de referencia ──────────────────────────────────────────────────
# "hoy" para todos los tests: viernes 2026-05-15
_TODAY = date(2026, 5, 15)
_TOMORROW = _TODAY + timedelta(days=1)

# ── Slots fixture ─────────────────────────────────────────────────────────────

def _make_slot(prof_nombre: str, esp: str, fecha: date, hora: str,
               id_prof: int, duracion: int = 15) -> dict:
    hm = hora.split(":")
    end_min = int(hm[0]) * 60 + int(hm[1]) + duracion
    hf = f"{end_min // 60:02d}:{end_min % 60:02d}"
    dias = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]
    meses = ["ene", "feb", "mar", "abr", "may", "jun",
             "jul", "ago", "sep", "oct", "nov", "dic"]
    disp = f"{dias[fecha.weekday()]} {fecha.day:02d} {meses[fecha.month - 1]}"
    return {
        "profesional": prof_nombre,
        "especialidad": esp,
        "fecha": fecha.strftime("%Y-%m-%d"),
        "fecha_display": disp,
        "hora_inicio": hora,
        "hora_fin": hf,
        "id_profesional": id_prof,
        "id_recurso": 1,
        "duracion": duracion,
    }


def _slots_mg_abarca(fecha: date) -> list[dict]:
    return [_make_slot("Dr. Andrés Abarca", "Medicina General", fecha, h, 73)
            for h in ["09:00", "09:15", "09:30", "10:00", "10:30"]]


def _slots_mg_marquez(fecha: date) -> list[dict]:
    return [_make_slot("Dr. Alonso Márquez", "Medicina General", fecha, h, 13, 20)
            for h in ["10:00", "10:20", "10:40"]]


def _slots_kine(fecha: date) -> list[dict]:
    return [_make_slot("Luis Armijo", "Kinesiología", fecha, h, 77, 40)
            for h in ["09:00", "09:40", "10:20"]]


def _slots_odonto(fecha: date) -> list[dict]:
    return [_make_slot("Dr. Carlos Jiménez", "Odontología General", fecha, h, 72, 30)
            for h in ["09:00", "09:30", "10:00"]]


def _slots_gastro(fecha: date) -> list[dict]:
    return [_make_slot("Dr. Nicolás Quijano", "Gastroenterología", _TOMORROW, h, 65, 20)
            for h in ["09:00", "09:20", "09:40"]]


# ── Mocks de Medilink ─────────────────────────────────────────────────────────

_FORCE_NO_SLOTS_ON: date | None = None   # si está seteado, esa fecha devuelve []
_SLOTS_DIA_OVERRIDE: dict | None = None  # {fecha_iso: [slots]} para mocks específicos

async def fake_buscar_paciente(rut: str, strict: bool = False):
    rut_clean = rut.replace(".", "").replace("-", "").strip().upper()
    if rut_clean.startswith("11111111"):
        return {"id": 100, "nombre": "Juan Prueba Test", "rut": "11111111-1"}
    if rut_clean.startswith("22222222"):
        return {"id": 200, "nombre": "María Multi Citas", "rut": "22222222-2"}
    return None

async def fake_crear_paciente(rut: str, nombre: str, apellidos: str, **kwargs):
    return {"id": 999, "nombre": f"{nombre} {apellidos}".strip(), "rut": rut}

async def fake_crear_cita(id_paciente, id_profesional, fecha, hora_inicio, hora_fin,
                          id_recurso=1, **kwargs):
    return {"id": 5555}

async def fake_verificar_slot_disponible(id_profesional, fecha, hora_inicio, hora_fin):
    return True

async def fake_cancelar_cita(id_cita, **kwargs):
    return True

_FAKE_CITAS: list[dict] = []

async def fake_listar_citas_paciente(id_paciente: int = 0, **kwargs):
    return list(_FAKE_CITAS)

async def fake_buscar_primer_dia(especialidad: str, dias_adelante: int = 60,
                                  excluir=None, intervalo_override=None, solo_ids=None):
    global _FORCE_NO_SLOTS_ON
    esp = (especialidad or "").lower()
    # Si la fecha base estaría bloqueada, usar el día siguiente
    fecha_base = _TOMORROW
    if _FORCE_NO_SLOTS_ON == _TOMORROW:
        fecha_base = _TOMORROW + timedelta(days=2)
    elif _FORCE_NO_SLOTS_ON == _TODAY:
        # No hay slots hoy → buscar_primer_dia retorna mañana
        fecha_base = _TOMORROW

    if solo_ids:
        pid = int(solo_ids[0])
        if pid == 13:
            slots = _slots_mg_marquez(fecha_base)
        elif pid == 77:
            slots = _slots_kine(fecha_base)
        elif pid == 72:
            slots = _slots_odonto(fecha_base)
        else:
            from medilink import PROFESIONALES as _P
            pr = _P.get(pid, {})
            slots = [_make_slot(pr.get("nombre", "Dr. Test"),
                                pr.get("especialidad", "General"),
                                fecha_base, "09:00", pid,
                                pr.get("intervalo", 15))]
        return slots[:5], slots

    if "kine" in esp or "kinesiolog" in esp:
        slots = _slots_kine(fecha_base)
    elif "odontolog" in esp or "dentista" in esp:
        slots = _slots_odonto(fecha_base)
    elif "gastro" in esp:
        slots = _slots_gastro(fecha_base)
    elif "olavarr" in esp:
        s = [_make_slot("Dr. Rodrigo Olavarría", "Medicina General", fecha_base, "09:00", 1)]
        return s, s
    elif "marquez" in esp or "márquez" in esp:
        # apellido Márquez → solo ID 13
        slots = _slots_mg_marquez(fecha_base)
        return slots[:5], slots
    else:
        slots = _slots_mg_abarca(fecha_base)
    return slots[:5], slots


async def fake_buscar_slots_dia(especialidad: str, fecha: str,
                                 intervalo_override=None, **kwargs):
    global _FORCE_NO_SLOTS_ON, _SLOTS_DIA_OVERRIDE
    try:
        fecha_dt = date.fromisoformat(fecha)
    except (ValueError, TypeError):
        fecha_dt = _TOMORROW

    if _FORCE_NO_SLOTS_ON and fecha_dt == _FORCE_NO_SLOTS_ON:
        return [], []

    if _SLOTS_DIA_OVERRIDE and fecha in _SLOTS_DIA_OVERRIDE:
        sl = _SLOTS_DIA_OVERRIDE[fecha]
        return sl, sl

    esp = (especialidad or "").lower()
    if "kine" in esp or "kinesiolog" in esp:
        slots = _slots_kine(fecha_dt)
    elif "odontolog" in esp or "dentista" in esp:
        slots = _slots_odonto(fecha_dt)
    elif "gastro" in esp:
        slots = _slots_gastro(fecha_dt)
    else:
        slots = _slots_mg_abarca(fecha_dt)
    return slots[:5], slots


async def fake_buscar_slots_dia_por_ids(ids, fecha: str, intervalo_override=None, **kwargs):
    global _FORCE_NO_SLOTS_ON, _SLOTS_DIA_OVERRIDE
    if not ids:
        return [], []
    try:
        fecha_dt = date.fromisoformat(fecha)
    except (ValueError, TypeError):
        fecha_dt = _TOMORROW

    if _FORCE_NO_SLOTS_ON and fecha_dt == _FORCE_NO_SLOTS_ON:
        return [], []

    from medilink import PROFESIONALES as _P
    all_slots: list[dict] = []
    for pid in ids:
        pid = int(pid)
        if pid == 13:
            all_slots.extend(_slots_mg_marquez(fecha_dt))
        elif pid == 77:
            all_slots.extend(_slots_kine(fecha_dt))
        elif pid == 72:
            all_slots.extend(_slots_odonto(fecha_dt))
        else:
            pr = _P.get(pid, {})
            all_slots.append(_make_slot(pr.get("nombre", "Dr. Test"),
                                        pr.get("especialidad", "General"),
                                        fecha_dt, "09:00", pid,
                                        pr.get("intervalo", 15)))
    return all_slots, all_slots


async def fake_consultar_proxima_fecha(especialidad: str):
    return _TOMORROW.strftime("%Y-%m-%d")


# ── Mocks de Claude ───────────────────────────────────────────────────────────

def _intent_from_text(msg: str) -> dict:
    t = msg.lower().strip()
    esp = None
    if any(w in t for w in ["medicina general", "medico general", "médico general",
                              "medico", "médico", "doctor general", "general"]):
        esp = "medicina general"
    elif any(w in t for w in ["kine", "kinesiolog"]):
        esp = "kinesiología"
    elif any(w in t for w in ["odontolog", "dentista", "diente", "muela", "dental"]):
        esp = "odontología"
    elif any(w in t for w in ["gastro", "guata", "estómago", "estomago"]):
        esp = "gastroenterología"
    elif "psico" in t:
        esp = "psicología"
    elif "nutri" in t:
        esp = "nutrición"
    elif "olavarr" in t:
        esp = "olavarría"
    elif "márquez" in t or "marquez" in t:
        esp = "márquez"

    if any(w in t for w in ["cancelar", "anular", "borrar mi hora", "elimina mi hora"]):
        return {"intent": "cancelar", "especialidad": esp, "respuesta_directa": None}
    if any(w in t for w in ["ya reservé", "ya agendé", "ya tengo hora"]):
        return {"intent": "ver_reservas", "especialidad": None, "respuesta_directa": None}
    if any(w in t for w in ["mis horas", "mis citas", "ver mis"]):
        return {"intent": "ver_reservas", "especialidad": esp, "respuesta_directa": None}
    if any(w in t for w in ["reagend", "cambiar mi hora"]):
        return {"intent": "reagendar", "especialidad": esp, "respuesta_directa": None}
    # Precio debe chequearse ANTES de agendar para evitar que "medicina general" en
    # "cuánto vale la consulta de medicina general" active intent=agendar.
    if any(w in t for w in ["cuesta", "precio", "valor", "cuánto vale", "cuanto vale",
                              "cuánto cuesta", "cuanto cuesta"]):
        return {"intent": "precio", "especialidad": esp, "respuesta_directa": None}
    if any(w in t for w in ["agendar", "agéndame", "reservar", "necesito", "quiero hora",
                              "quiero una hora", "hora con", "hora de", "hora para",
                              "urgencia dental", "urgencia odont", "me duele la muela",
                              "me duele el diente"]):
        return {"intent": "agendar", "especialidad": esp, "respuesta_directa": None}
    # Si hay especialidad detectada y el mensaje tiene carga clínica → agendar
    if esp and any(w in t for w in ["urgencia", "me duele", "dolor", "tengo"]):
        return {"intent": "agendar", "especialidad": esp, "respuesta_directa": None}
    if any(w in t for w in ["recepcion", "recepción", "hablar con alguien", "humano",
                              "chatear con alguien", "persona real"]):
        return {"intent": "humano", "especialidad": None, "respuesta_directa": None}
    if any(w in t for w in ["ya gracias", "gracias bye", "hasta luego", "chao", "ok gracias"]):
        return {"intent": "closing", "especialidad": None,
                "respuesta_directa": "Cuando necesites, aquí estaremos. ¡Hasta pronto!"}
    return {"intent": "otro", "especialidad": esp, "respuesta_directa": None}


async def fake_detect_intent(mensaje: str, recepcion_resumen=None, meta_referral=None, **kwargs):
    return _intent_from_text(mensaje)


async def fake_respuesta_faq(mensaje: str, **kwargs):
    t = mensaje.lower()
    if "kine" in t and ("cuesta" in t or "precio" in t or "valor" in t):
        return "La kinesiología tiene un valor de $12.000 a $18.000 por sesión en CMC."
    if ("consulta" in t or "medicina general" in t) and ("cuesta" in t or "precio" in t or "valor" in t or "vale" in t):
        return "La consulta de medicina general tiene un valor de $13.000 (Fonasa) o $25.000 (particular)."
    if "donde" in t or "dónde" in t or "ubic" in t:
        return "Estamos en Monsalve 102, frente a la antigua estación de trenes, Carampangue."
    return "Te puedo ayudar con eso. Llama a recepción al +56966610737."


async def fake_clasificar_respuesta_seguimiento(mensaje: str):
    t = mensaje.lower()
    if "peor" in t or "mal" in t:
        return "peor"
    if "mejor" in t or "bien" in t:
        return "mejor"
    return "igual"


async def fake_classify_with_context(*args, **kwargs):
    return {"intent": "otro"}


async def fake_consulta_clinica_doctor(*args, **kwargs):
    return "Consulta registrada."


# ── Aplicar monkey-patches ────────────────────────────────────────────────────
import medilink
import claude_helper
import flows

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

import resilience
resilience.is_medilink_down = lambda: False
flows.is_medilink_down = lambda: False

import triage_ges

async def fake_triage_sintomas(texto: str):
    """Mock determinista de triage: solo ACV y síntomas severos retornan triage positivo."""
    t = texto.lower()
    # No retornar triage para síntomas leves — el bot debe manejarlos normalmente
    return None

triage_ges.triage_sintomas = fake_triage_sintomas
flows.triage_sintomas = fake_triage_sintomas

import messaging

async def fake_send_whatsapp(to, body, **kwargs):
    pass

messaging.send_whatsapp = fake_send_whatsapp
flows.send_whatsapp = fake_send_whatsapp

# Mockear fecha "hoy" en flows para determinismo
import datetime as _dt_mod

class _FakeDate(_dt_mod.date):
    @classmethod
    def today(cls):
        return _TODAY

# Inyectar fecha fija en flows via monkey-patch de datetime.now
_real_datetime = _dt_mod.datetime

class _FakeDatetime(_dt_mod.datetime):
    @classmethod
    def now(cls, tz=None):
        if tz is not None:
            return _real_datetime(_TODAY.year, _TODAY.month, _TODAY.day,
                                  10, 0, 0, tzinfo=tz)
        return _real_datetime(_TODAY.year, _TODAY.month, _TODAY.day, 10, 0, 0)

flows.datetime = _FakeDatetime  # type: ignore[attr-defined]

from session import (get_session, reset_session, save_session,
                     save_privacy_consent, log_event)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _normalize(resp: Any) -> str:
    """Convierte respuesta (str o dict WhatsApp interactive) a texto plano."""
    if resp is None:
        return ""
    if isinstance(resp, str):
        return resp
    if not isinstance(resp, dict):
        return str(resp)
    parts: list[str] = []
    for key in ("text", "body"):
        v = resp.get(key)
        if isinstance(v, str):
            parts.append(v)
    interactive = resp.get("interactive") or {}
    for key in ("body", "footer", "header"):
        t = (interactive.get(key) or {}).get("text", "")
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


def _get_current_state(phone: str) -> str:
    sess = get_session(phone)
    return sess.get("state", "IDLE")


_DATE_PATTERNS = [
    re.compile(r"\b(\d{1,2})\s+de\s+(\w+)\b", re.IGNORECASE),
    re.compile(r"\b(\d{4}-\d{2}-\d{2})\b"),
    re.compile(r"\b(\d{1,2})[/\-](\d{1,2})\b"),
]

_MESES_ES_MAP = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
    # abreviados
    "ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6,
    "jul": 7, "ago": 8, "sep": 9, "oct": 10, "nov": 11, "dic": 12,
}


def _extract_fecha_from_response(txt: str) -> date | None:
    """Intenta extraer la primera fecha mencionada en la respuesta del bot."""
    low = txt.lower()
    # ISO 2026-05-16
    for pat in _DATE_PATTERNS:
        m = pat.search(low)
        if m:
            groups = m.groups()
            if len(groups) == 1 and "-" in groups[0]:
                try:
                    return date.fromisoformat(groups[0])
                except ValueError:
                    pass
            if len(groups) == 2:
                d_str, mes_str = groups
                try:
                    d = int(d_str)
                    mes = _MESES_ES_MAP.get(mes_str.lower())
                    if mes:
                        return date(_TODAY.year, mes, d)
                except (ValueError, TypeError):
                    pass
    # buscar nombre de día + número: "sáb 16 may"
    m2 = re.search(r"(?:lun|mar|mié|jue|vie|sáb|dom)\w*\s+(\d{1,2})\s+(\w+)", low)
    if m2:
        try:
            d = int(m2.group(1))
            mes = _MESES_ES_MAP.get(m2.group(2).lower())
            if mes:
                return date(_TODAY.year, mes, d)
        except (ValueError, TypeError):
            pass
    return None


def _check_step(step: dict, response_txt: str, state_after: str) -> list[str]:
    """Verifica un step de fixture. Retorna lista de errores (vacía = ok)."""
    errors: list[str] = []
    low = response_txt.lower()

    # expect_state — soporta alternativas separadas por "|"
    exp_state = step.get("expect_state")
    if exp_state:
        valid_states = [s.strip() for s in exp_state.split("|")]
        if state_after not in valid_states:
            errors.append(f"expect_state={exp_state!r} but got {state_after!r}")

    # expect_match (OR con regex)
    for pat in step.get("expect_match", []):
        if not re.search(pat, low, re.IGNORECASE):
            errors.append(f"expect_match {pat!r} not found in response")

    # expect_match_all (AND)
    for pat in step.get("expect_match_all", []):
        if not re.search(pat, low, re.IGNORECASE):
            errors.append(f"expect_match_all {pat!r} not found in response")

    # expect_no_match
    for pat in step.get("expect_no_match", []):
        if re.search(pat, low, re.IGNORECASE):
            errors.append(f"expect_no_match {pat!r} unexpectedly found in response")

    # expect_no_silent_date_jump
    if step.get("expect_no_silent_date_jump"):
        fecha_mostrada = _extract_fecha_from_response(response_txt)
        if fecha_mostrada is not None:
            # Obtener la fecha solicitada del texto de entrada (heurística)
            txt_in = step.get("in", "").lower()
            fecha_pedida = _TODAY if "hoy" in txt_in else _TOMORROW if "mañana" in txt_in else None
            if fecha_pedida and fecha_mostrada != fecha_pedida:
                disclaimer_phrases = [
                    "no encontré para",
                    "no hay horas el",
                    "no tiene horas el",
                    "te muestro la próxima",
                    "próxima disponibilidad",
                    "no trabajan ese día",
                    "solo",
                    "siguiente",
                ]
                if not any(p in low for p in disclaimer_phrases):
                    errors.append(
                        f"silent date jump: pedida {fecha_pedida}, "
                        f"mostrada {fecha_mostrada}, sin disclaimer"
                    )
    return errors


def _run_step(phone: str, step_input: str) -> str:
    """Ejecuta un paso de conversación y retorna el texto normalizado de la respuesta."""
    # Manejar inputs especiales
    if step_input == "__INJECT_CROSS_SELL_STATE__":
        sess = get_session(phone)
        sess["data"]["cross_sell_esp_origen"] = "medicina general"
        sess["data"]["cross_sell_esp_destino"] = "kinesiología"
        save_session(phone, "WAIT_CROSS_SELL", sess["data"])
        return "__STATE_INJECTED__"

    sess = get_session(phone)
    resp = asyncio.get_event_loop().run_until_complete(
        flows.handle_message(phone, step_input, sess)
    )
    return _normalize(resp)


# ── Carga de fixtures ─────────────────────────────────────────────────────────

GOLDEN_DIR = Path(__file__).parent / "golden"

def _load_fixtures() -> list[dict]:
    fixtures = []
    for p in sorted(GOLDEN_DIR.glob("*.json")):
        with open(p) as f:
            fixtures.append(json.load(f))
    return fixtures


# ── Tests conocidos con bugs pendientes ──────────────────────────────────────
# Mapa fixture_id → (xfail, reason)
_XFAIL_MAP: dict[str, str] = {
    # Todos los bugs documentados anteriormente fueron corregidos.
    # Este mapa se mantiene vacío. Si aparecen nuevos bugs conocidos,
    # documentarlos aquí con su fixture_id y razón.
}


# ── Parametrización y ejecución ───────────────────────────────────────────────

_FIXTURES = _load_fixtures()
_FIXTURE_IDS = [f["id"] for f in _FIXTURES]


def _pytest_params():
    params = []
    for fixture in _FIXTURES:
        fid = fixture["id"]
        xfail_reason = _XFAIL_MAP.get(fid)
        mark = pytest.mark.xfail(reason=xfail_reason, strict=False) if xfail_reason else None
        p = pytest.param(fixture, id=fid, marks=[mark] if mark else [])
        params.append(p)
    return params


@pytest.mark.parametrize("fixture", _pytest_params())
def test_golden_conversation(fixture: dict):
    """Ejecuta una conversación golden completa y verifica cada step."""
    phone = fixture["phone"]
    steps = fixture["steps"]
    fixture_id = fixture["id"]

    reset_session(phone)
    save_privacy_consent(phone, "accepted", method="test")
    # Registrar disclosure como enviado para que no bloquee el primer mensaje.
    # En producción el disclosure se envía al primer contacto; aquí lo simulamos.
    log_event(phone, "disclosure_enviado", {"method": "test"})

    _FAKE_CITAS.clear()
    # Configurar citas según mock_config
    mock_cfg = fixture.get("mock_config", {})
    if "listar_citas_paciente" in mock_cfg:
        _FAKE_CITAS.extend(mock_cfg["listar_citas_paciente"])

    global _FORCE_NO_SLOTS_ON, _SLOTS_DIA_OVERRIDE
    _FORCE_NO_SLOTS_ON = None
    _SLOTS_DIA_OVERRIDE = None

    if mock_cfg.get("force_no_slots_on") == "today":
        _FORCE_NO_SLOTS_ON = _TODAY
    elif mock_cfg.get("force_no_slots_on") == "tomorrow":
        _FORCE_NO_SLOTS_ON = _TOMORROW

    # Inyectar slots específicos por fecha desde el fixture
    if mock_cfg.get("buscar_slots_dia_tomorrow"):
        _SLOTS_DIA_OVERRIDE = {_TOMORROW.strftime("%Y-%m-%d"): mock_cfg["buscar_slots_dia_tomorrow"]}

    errors_all: list[str] = []
    transcript: list[tuple[str, str]] = []

    for i, step in enumerate(steps):
        step_in = step["in"]
        comment = step.get("comment", "")

        # Skip assertions para steps de inyección de estado
        if step_in == "__INJECT_CROSS_SELL_STATE__":
            _run_step(phone, step_in)
            continue
        # Skip attachments simulados — solo verificar estado
        if step_in == "__DOCX_ATTACHMENT__":
            # Simular que el bot recibió un documento: inyectar mensaje de texto
            # que imita lo que el webhook enviaría después de procesar el docx
            synthetic = "[DOCUMENTO] ficha_clinica.docx — Paciente Juan Pérez"
            resp_txt = _run_step(phone, synthetic)
        else:
            resp_txt = _run_step(phone, step_in)

        state_after = _get_current_state(phone)
        transcript.append((step_in, resp_txt))

        step_errors = _check_step(step, resp_txt, state_after)
        for err in step_errors:
            errors_all.append(
                f"Step {i+1} [{step_in!r}] — {err}"
                + (f"\n  comment: {comment}" if comment else "")
                + f"\n  response: {resp_txt[:300]!r}"
            )

    if errors_all:
        transcript_str = "\n".join(
            f"  > {inp!r}\n  < {out[:200]!r}" for inp, out in transcript
        )
        pytest.fail(
            f"[{fixture_id}] {len(errors_all)} error(s):\n"
            + "\n".join(errors_all)
            + f"\n\nTranscript:\n{transcript_str}"
        )
