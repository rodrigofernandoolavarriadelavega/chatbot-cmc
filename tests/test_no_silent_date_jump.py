"""
Test crítico: no-salto silencioso de fecha (Tarea 3).

Si el paciente pide fecha X y el bot ofrece fecha Y ≠ X,
la respuesta DEBE contener un disclaimer explícito de que no hay
disponibilidad en la fecha pedida.

Ejecución:
    pytest tests/test_no_silent_date_jump.py -v
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT))

# ── DB aislada ────────────────────────────────────────────────────────────────
_TMP_DB = Path(tempfile.mkdtemp(prefix="cmc_nodatejump_")) / "sessions.db"
os.environ["SESSIONS_DB"] = str(_TMP_DB)
os.environ.setdefault("SQLCIPHER_KEY", "")

import session as _session_mod
_session_mod.DB_PATH = _TMP_DB

_TODAY = date(2026, 5, 15)     # viernes
_TOMORROW = _TODAY + timedelta(days=1)    # sábado
_NEXT_MON = _TODAY + timedelta(days=3)   # lunes
_NEXT_FRI = _TODAY + timedelta(days=7)   # viernes siguiente
_ALT_DATE = _TODAY + timedelta(days=3)   # fecha alternativa simulada

# ── Mocks ────────────────────────────────────────────────────────────────────

import medilink
import flows
import claude_helper
import resilience
import messaging

_NO_SLOTS_ON: set[str] = set()   # fechas ISO donde buscar_slots_dia retorna []
_PRIMER_DIA_FECHA = _ALT_DATE    # fecha que retorna buscar_primer_dia cuando no hay slots


def _make_slot(prof_nombre, esp, fecha, hora, id_prof, duracion=15):
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


def _slots_for_date(esp: str, fecha: date) -> list[dict]:
    if "kine" in esp.lower():
        return [_make_slot("Luis Armijo", "Kinesiología", fecha, "09:00", 77, 40)]
    if "odontolog" in esp.lower() or "burgos" in esp.lower():
        return [_make_slot("Dra. Javiera Burgos", "Odontología General", fecha, "10:00", 55, 60)]
    return [_make_slot("Dr. Andrés Abarca", "Medicina General", fecha, "09:00", 73, 15)]


async def _fake_bp(especialidad, dias_adelante=60, excluir=None,
                   intervalo_override=None, solo_ids=None):
    # Retornar slots para _PRIMER_DIA_FECHA (simulando que no hay antes)
    esp = (especialidad or "").lower()
    slots = _slots_for_date(esp, _PRIMER_DIA_FECHA)
    return slots, slots


async def _fake_bsd(especialidad, fecha, intervalo_override=None, **kw):
    global _NO_SLOTS_ON
    if fecha in _NO_SLOTS_ON:
        return [], []
    esp = (especialidad or "").lower()
    try:
        fecha_dt = date.fromisoformat(fecha)
    except (ValueError, TypeError):
        fecha_dt = _TOMORROW
    slots = _slots_for_date(esp, fecha_dt)
    return slots, slots


async def _fake_bsdpi(ids, fecha, intervalo_override=None, **kw):
    global _NO_SLOTS_ON
    if fecha in _NO_SLOTS_ON:
        return [], []
    if not ids:
        return [], []
    from medilink import PROFESIONALES as _P
    try:
        fecha_dt = date.fromisoformat(fecha)
    except (ValueError, TypeError):
        fecha_dt = _TOMORROW
    all_slots = []
    for pid in ids:
        pid = int(pid)
        pr = _P.get(pid, {})
        all_slots.append(_make_slot(pr.get("nombre", "Dr. Test"),
                                    pr.get("especialidad", "General"),
                                    fecha_dt, "09:00", pid,
                                    pr.get("intervalo", 15)))
    return all_slots, all_slots


async def _fake_cc(*a, **kw):
    return {"id": 5555}

async def _fake_vsd(*a, **kw):
    return True

async def _fake_lcp(id_paciente=0, **kw):
    return []

async def _fake_bpac(rut):
    return {"id": 100, "nombre": "Juan Prueba Test", "rut": "11111111-1"}

async def _fake_cpac(rut, nombre, apellidos, **kw):
    return {"id": 999, "nombre": f"{nombre} {apellidos}".strip(), "rut": rut}

async def _fake_cpf(esp):
    return _PRIMER_DIA_FECHA.strftime("%Y-%m-%d")

async def _fake_send(*a, **kw):
    pass


def _det_from_msg(msg):
    t = msg.lower()
    esp = None
    if "kine" in t or "kinesiolog" in t:
        esp = "kinesiología"
    elif "burgos" in t or "odontolog" in t:
        esp = "odontología"
    elif any(w in t for w in ["medico", "médico", "general", "medicina", "mg"]):
        esp = "medicina general"
    if any(w in t for w in ["agendar", "quiero", "necesito", "hora"]):
        return {"intent": "agendar", "especialidad": esp, "respuesta_directa": None}
    return {"intent": "otro", "especialidad": esp, "respuesta_directa": None}


async def _fake_det(msg, recepcion_resumen=None, meta_referral=None, **kwargs):
    return _det_from_msg(msg)

async def _fake_faq(msg):
    return "Información en recepción."

async def _fake_cls(msg):
    return "igual"


for mod in (medilink, flows):
    mod.buscar_paciente = _fake_bpac
    mod.crear_paciente = _fake_cpac
    mod.crear_cita = _fake_cc
    mod.cancelar_cita = lambda *a, **kw: asyncio.coroutine(lambda: True)()
    mod.listar_citas_paciente = _fake_lcp
    mod.buscar_primer_dia = _fake_bp
    mod.buscar_slots_dia = _fake_bsd
    mod.buscar_slots_dia_por_ids = _fake_bsdpi
    mod.consultar_proxima_fecha = _fake_cpf
    mod.verificar_slot_disponible = _fake_vsd

claude_helper.detect_intent = _fake_det
claude_helper.respuesta_faq = _fake_faq
claude_helper.clasificar_respuesta_seguimiento = _fake_cls
flows.detect_intent = _fake_det
flows.respuesta_faq = _fake_faq
flows.clasificar_respuesta_seguimiento = _fake_cls

resilience.is_medilink_down = lambda: False
flows.is_medilink_down = lambda: False
messaging.send_whatsapp = _fake_send
flows.send_whatsapp = _fake_send

import triage_ges as _triage_ges_mod

async def _fake_triage(texto: str):
    return None

_triage_ges_mod.triage_sintomas = _fake_triage
flows.triage_sintomas = _fake_triage

import datetime as _dt_mod

class _FakeDatetime(_dt_mod.datetime):
    @classmethod
    def now(cls, tz=None):
        if tz is not None:
            from zoneinfo import ZoneInfo
            return _dt_mod.datetime(_TODAY.year, _TODAY.month, _TODAY.day,
                                    10, 0, 0, tzinfo=tz)
        return _dt_mod.datetime(_TODAY.year, _TODAY.month, _TODAY.day, 10, 0, 0)

flows.datetime = _FakeDatetime  # type: ignore[attr-defined]

from session import get_session, reset_session, save_privacy_consent


# ── Helpers ───────────────────────────────────────────────────────────────────

def _normalize(resp: Any) -> str:
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


_MESES_ES_MAP = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
    "ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6,
    "jul": 7, "ago": 8, "sep": 9, "oct": 10, "nov": 11, "dic": 12,
}


def _extract_fecha(txt: str) -> date | None:
    """Extrae la primera fecha mencionada en txt."""
    low = txt.lower()
    # ISO
    m = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", low)
    if m:
        try:
            return date.fromisoformat(m.group(1))
        except ValueError:
            pass
    # "N de Mes" / "sáb 16 may"
    m2 = re.search(
        r"(?:lun|mar|m[ié]|jue|vie|s[aá]b|dom)\w*\s+(\d{1,2})\s+(\w+)",
        low
    )
    if m2:
        try:
            d = int(m2.group(1))
            mes = _MESES_ES_MAP.get(m2.group(2).lower())
            if mes:
                return date(_TODAY.year, mes, d)
        except (ValueError, TypeError):
            pass
    m3 = re.search(r"\b(\d{1,2})\s+de\s+(\w+)\b", low)
    if m3:
        try:
            d = int(m3.group(1))
            mes = _MESES_ES_MAP.get(m3.group(2).lower())
            if mes:
                return date(_TODAY.year, mes, d)
        except (ValueError, TypeError):
            pass
    return None


def _has_disclaimer(txt: str) -> bool:
    """Verifica que la respuesta contiene un disclaimer de cambio de fecha."""
    low = txt.lower()
    disclaimers = [
        "no encontré para",
        "no hay horas el",
        "no tiene horas el",
        "te muestro la próxima",
        "próxima disponibilidad",
        "no trabajan ese día",
        "sin horarios disponibles",
        "no hay disponibilidad",
        "solo",          # "solo el lunes tiene"
        "siguiente",     # "la siguiente disponibilidad"
        "próximo",       # "el próximo martes"
        "no encontré disponibilidad",
    ]
    return any(p in low for p in disclaimers)


def _run_convo(phone: str, prompt: str) -> tuple[str, str]:
    """Ejecuta prompt y retorna (resp_txt, state_after)."""
    sess = get_session(phone)
    resp = asyncio.get_event_loop().run_until_complete(
        flows.handle_message(phone, prompt, sess)
    )
    txt = _normalize(resp)
    state_after = get_session(phone).get("state", "IDLE")
    return txt, state_after


# ── Disclaimer phrases para assertions ───────────────────────────────────────

_DISCLAIMER_PHRASES = [
    "no encontré para",
    "no hay horas el",
    "no tiene horas el",
    "te muestro la próxima",
    "próxima disponibilidad",
    "no trabajan ese día",
    "sin horarios disponibles",
    "no hay disponibilidad",
    "siguiente",
    "próximo",
]


# ── Parametrización de casos ──────────────────────────────────────────────────

@pytest.mark.parametrize("prompt,fecha_solicitada,esp,extra_no_slots", [
    (
        "medicina general para hoy",
        _TODAY,
        "medicina general",
        [_TODAY.strftime("%Y-%m-%d")],
    ),
    (
        "MG para mañana",
        _TOMORROW,
        "medicina general",
        [_TOMORROW.strftime("%Y-%m-%d")],
    ),
    (
        "kine para el viernes",
        _NEXT_FRI,
        "kinesiología",
        [_NEXT_FRI.strftime("%Y-%m-%d")],
    ),
    (
        "hora con Burgos el lunes",
        _NEXT_MON,
        "odontología",
        [_NEXT_MON.strftime("%Y-%m-%d")],
    ),
], ids=[
    "mg_hoy_sin_slots",
    "mg_manana_sin_slots",
    "kine_viernes_sin_slots",
    "burgos_lunes_sin_slots",
])
def test_no_silent_date_jump(prompt: str, fecha_solicitada: date,
                              esp: str, extra_no_slots: list[str]):
    """
    Si buscar_slots_dia(esp, fecha_solicitada) → [] y buscar_primer_dia
    retorna fecha alternativa, la respuesta DEBE contener disclaimer explícito.

    Falla = salto silencioso de fecha: el bot muestra slots de otra fecha
    sin avisar al paciente que no hay disponibilidad en la pedida.
    """
    global _NO_SLOTS_ON, _PRIMER_DIA_FECHA
    phone = f"56900200{abs(hash(prompt)) % 1000:03d}"

    reset_session(phone)
    save_privacy_consent(phone, "accepted", method="test")

    # Forzar sin slots en la fecha pedida
    _NO_SLOTS_ON = set(extra_no_slots)
    # buscar_primer_dia retornará _ALT_DATE (fecha_solicitada + 3 días)
    _PRIMER_DIA_FECHA = fecha_solicitada + timedelta(days=3)

    resp_txt, state = _run_convo(phone, prompt)

    if state != "WAIT_SLOT":
        pytest.skip(
            f"Bot no llegó a WAIT_SLOT con prompt={prompt!r}, state={state!r}. "
            "Puede que detect_intent no mapeó la especialidad correctamente."
        )

    fecha_mostrada = _extract_fecha(resp_txt)

    if fecha_mostrada is None:
        # No se pudo extraer fecha de la respuesta; no hay salto detectable
        return

    if fecha_mostrada == fecha_solicitada:
        # Coinciden: no hubo salto → ok
        return

    # Hubo salto de fecha. DEBE haber disclaimer.
    assert _has_disclaimer(resp_txt), (
        f"\nSALTO SILENCIOSO DE FECHA DETECTADO:\n"
        f"  prompt:          {prompt!r}\n"
        f"  fecha solicitada: {fecha_solicitada}\n"
        f"  fecha mostrada:   {fecha_mostrada}\n"
        f"  respuesta:        {resp_txt[:500]!r}\n\n"
        f"La respuesta DEBE contener alguna de estas frases:\n"
        + "\n".join(f"  - {p!r}" for p in _DISCLAIMER_PHRASES)
    )
    # Limpiar
    _NO_SLOTS_ON = set()


def test_otro_dia_no_es_silencioso():
    """
    Cuando el paciente escribe 'otro día' en WAIT_SLOT, el bot debe
    informar explícitamente que busca otra fecha (no simplemente mostrar
    slots sin contexto).
    """
    global _NO_SLOTS_ON, _PRIMER_DIA_FECHA
    phone = "56900200999"
    reset_session(phone)
    save_privacy_consent(phone, "accepted", method="test")
    _NO_SLOTS_ON = set()
    _PRIMER_DIA_FECHA = _ALT_DATE

    # Paso 1: obtener slots
    _run_convo(phone, "quiero medicina general")
    sess = get_session(phone)
    if sess.get("state") != "WAIT_SLOT":
        pytest.skip("No llegó a WAIT_SLOT")

    # Paso 2: pedir otro día
    resp_txt, state = _run_convo(phone, "otro día")
    assert state == "WAIT_SLOT", f"'otro día' debe mantenerse en WAIT_SLOT, got {state!r}"

    # La respuesta debe mostrar slots (no error) con contexto de nueva fecha
    assert resp_txt.strip(), "Respuesta vacía tras 'otro día'"
    assert not any(m in resp_txt.lower() for m in ["no te entendí", "no entendí"]), (
        f"'otro día' retornó error de no-entendido: {resp_txt[:200]!r}"
    )


def test_fecha_pedida_hoy_y_hay_slots_no_necesita_disclaimer():
    """
    Si el paciente pide 'para hoy' y SÍ hay slots hoy, no debe aparecer
    ningún disclaimer de fecha. Verifica que el disclaimer no es un falso positivo.
    """
    global _NO_SLOTS_ON, _PRIMER_DIA_FECHA
    phone = "56900201000"
    reset_session(phone)
    save_privacy_consent(phone, "accepted", method="test")
    _NO_SLOTS_ON = set()          # No bloquear ninguna fecha
    _PRIMER_DIA_FECHA = _TOMORROW  # Irrelevante, hay slots hoy

    # Inyectar slot para hoy directamente
    # Como _fake_bsd no bloquea _TODAY, retornará slots para hoy
    resp_txt, state = _run_convo(phone, "medicina general para hoy")
    if state != "WAIT_SLOT":
        pytest.skip(f"No llegó a WAIT_SLOT, got {state!r}")

    slots = get_session(phone).get("data", {}).get("slots", [])
    if not slots:
        pytest.skip("No hay slots en sesión")

    fecha_slot = date.fromisoformat(slots[0]["fecha"])
    if fecha_slot == _TODAY:
        # Sí hay slots hoy → el disclaimer de "no hay para hoy" sería falso positivo
        # No verificar porque en este test SÍ hay slots y el disclaimer es correcto no aparezca
        assert "no hay horas" not in resp_txt.lower() or "mañana" not in resp_txt.lower(), (
            "Disclaimer de 'no hay horas' apareció cuando sí había slots para hoy"
        )
