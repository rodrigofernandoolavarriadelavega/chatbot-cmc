"""CONFIRMING_CITA: pedir otro día no es una respuesta inválida.

Caso real — Irma Sepúlveda (56931434082, 2026-07-28):

    16:27:25  out  CONFIRMING_CITA  '*Irma*, te reservo esta hora... Martes 28 de julio 13:30'
    16:28:02  in   CONFIRMING_CITA  'Miércoles'
    16:28:04  out  CONFIRMING_CITA  'Responde *Sí* para confirmar, o toca ❌ Cambiar'
    ...abandonó...
    22:31     [Recepcionista] Me da su rut para agendarla para mañana a las 15:30?
    22:39     [Recepcionista] Ok quedó agendada para el dia de mañana 15:30hrs

El bot tenía la información que necesitaba ("Miércoles") y la descartó por no
ser sí/no. Seis horas después una persona hizo a mano exactamente eso.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT))

_TMP_DB = Path(tempfile.mkdtemp(prefix="cmc_confdia_")) / "sessions.db"
os.environ["SESSIONS_DB"] = str(_TMP_DB)
os.environ.setdefault("SQLCIPHER_KEY", "")

import session as _session_mod  # noqa: E402

_session_mod.DB_PATH = _TMP_DB

import claude_helper  # noqa: E402
import flows  # noqa: E402
import medilink  # noqa: E402
import messaging  # noqa: E402
import resilience  # noqa: E402

_HOY = date(2026, 7, 28)          # martes — el día que el bot le ofreció a Irma
_MIERCOLES = _HOY + timedelta(days=1)

# Fechas sin cupo, para el camino "pidió un día que no tiene horas".
_SIN_SLOTS: set[str] = set()


def _slot(fecha: date, hora: str = "15:30"):
    return {
        "profesional": "Dr. Rodrigo Olavarría",
        "especialidad": "Medicina General",
        "fecha": fecha.strftime("%Y-%m-%d"),
        "fecha_display": f"{fecha.day} de julio",
        "hora_inicio": hora,
        "hora_fin": "15:45",
        "id_profesional": 1,
        "id_recurso": 1,
        "duracion": 15,
    }


async def _fake_bsd(especialidad, fecha, intervalo_override=None, **kw):
    if fecha in _SIN_SLOTS:
        return [], []
    try:
        f = date.fromisoformat(fecha)
    except (ValueError, TypeError):
        f = _MIERCOLES
    s = [_slot(f)]
    return s, s


async def _fake_noop(*a, **kw):
    return None


for _mod in (medilink, flows):
    _mod.buscar_slots_dia = _fake_bsd
    _mod.buscar_slots_dia_por_ids = _fake_bsd

resilience.is_medilink_down = lambda: False
flows.is_medilink_down = lambda: False
messaging.send_whatsapp = _fake_noop
flows.send_whatsapp = _fake_noop


async def _fake_det(msg, **kw):
    return {"intent": "otro", "especialidad": None, "respuesta_directa": None}


claude_helper.detect_intent = _fake_det
flows.detect_intent = _fake_det

import triage_ges as _triage  # noqa: E402


async def _fake_triage(texto):
    return None


_triage.triage_sintomas = _fake_triage
flows.triage_sintomas = _fake_triage

import datetime as _dt  # noqa: E402


class _FakeDatetime(_dt.datetime):
    @classmethod
    def now(cls, tz=None):
        return _dt.datetime(_HOY.year, _HOY.month, _HOY.day, 16, 28, 0, tzinfo=tz)


flows.datetime = _FakeDatetime  # type: ignore[attr-defined]

from session import get_session, reset_session, save_session  # noqa: E402


def _texto(resp: Any) -> str:
    """Aplana la respuesta (str o payload interactivo) a un solo string."""
    if resp is None:
        return ""
    if isinstance(resp, str):
        return resp
    if not isinstance(resp, dict):
        return str(resp)
    partes = [resp.get("text", ""), resp.get("body", "")]
    inter = resp.get("interactive") or {}
    for k in ("body", "footer", "header"):
        partes.append((inter.get(k) or {}).get("text", ""))
    accion = inter.get("action") or {}
    for b in accion.get("buttons", []) or []:
        r = b.get("reply") or {}
        partes += [r.get("title", ""), r.get("id", "")]
    for sec in accion.get("sections", []) or []:
        for row in sec.get("rows", []) or []:
            partes += [row.get("title", ""), row.get("id", "")]
    return " | ".join(str(p) for p in partes if p)


PHONE = "56931434082"


def _poner_en_confirming():
    reset_session(PHONE)
    save_session(PHONE, "CONFIRMING_CITA", {
        "especialidad": "medicina general",
        "slot_elegido": _slot(_HOY, "13:30"),
        "rut": "13389875-8",
        "paciente": {"id": 1, "nombre": "Irma Ines Sepúlveda Gómez"},
        "modalidad": "fonasa",
    })
    return get_session(PHONE)


def _responder(mensaje: str) -> str:
    ses = _poner_en_confirming()
    return _texto(asyncio.run(flows.handle_message(PHONE, mensaje, ses)))


# ── Regresión del caso Irma ──────────────────────────────────────────────────

def test_miercoles_no_recibe_responde_si():
    """El bug exacto: 'Miércoles' devolvía 'Responde Sí para confirmar'."""
    resp = _responder("Miércoles")
    assert "Responde" not in resp or "confirmar" not in resp, resp


def test_miercoles_ofrece_horarios_de_ese_dia():
    resp = _responder("Miércoles")
    assert "15:30" in resp, resp


def test_miercoles_deja_la_sesion_en_wait_slot():
    _responder("Miércoles")
    assert get_session(PHONE)["state"] == "WAIT_SLOT"


@pytest.mark.parametrize("msg", ["Miércoles", "miercoles", "el jueves", "viernes"])
def test_dias_de_semana_se_entienden(msg):
    resp = _responder(msg)
    assert "Responde" not in resp or "confirmar" not in resp, f"{msg!r} → {resp}"


# ── No romper el camino normal ───────────────────────────────────────────────

def test_texto_sin_sentido_sigue_pidiendo_si_o_no():
    """Sin día ni fecha, el catch-all de siempre debe seguir intacto."""
    resp = _responder("asdfgh")
    assert "Responde" in resp and "confirmar" in resp, resp


def test_sin_cupo_ese_dia_no_suelta_la_hora_apartada():
    """Si el día pedido no tiene horas, la reserva original sigue viva."""
    global _SIN_SLOTS
    _SIN_SLOTS = {_MIERCOLES.strftime("%Y-%m-%d")}
    try:
        resp = _responder("Miércoles")
        assert "sigue apartada" in resp, resp
        assert "13:30" in resp, resp
    finally:
        _SIN_SLOTS = set()
