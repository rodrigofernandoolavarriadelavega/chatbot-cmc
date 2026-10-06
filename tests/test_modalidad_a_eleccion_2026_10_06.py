"""Ps. Salas (82) atiende presencial Y por videollamada: el bot pregunta.

Antes el bot la trataba como solo presencial (decisión del 1-oct: online "a
pedido, lo coordina recepción"), aunque su horario en Medilink tiene ambas
modalidades. Dueño, 6-oct: que el paciente elija al tomar la hora.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT))

_TMP_DB = Path(tempfile.mkdtemp(prefix="cmc_modalidad_")) / "sessions.db"
os.environ["SESSIONS_DB"] = str(_TMP_DB)
os.environ.setdefault("SQLCIPHER_KEY", "")

import session as _session_mod  # noqa: E402

_session_mod.DB_PATH = _TMP_DB

import flows  # noqa: E402
import messaging  # noqa: E402
import resilience  # noqa: E402
from session import get_session, reset_session, save_session  # noqa: E402


async def _fake_noop(*a, **kw):
    return None


resilience.is_medilink_down = lambda: False
flows.is_medilink_down = lambda: False
messaging.send_whatsapp = _fake_noop
flows.send_whatsapp = _fake_noop

_FECHA = (date.today() + timedelta(days=3))
while _FECHA.weekday() > 4:  # día hábil: Montalba es online lun-vie
    _FECHA += timedelta(days=1)


def _slot(id_prof: int, nombre: str):
    return {
        "profesional": nombre, "especialidad": "Psicología Adulto",
        "fecha": _FECHA.strftime("%Y-%m-%d"), "fecha_display": "jueves",
        "hora_inicio": "17:00", "hora_fin": "17:45",
        "id_profesional": id_prof, "id_recurso": 1, "duracion": 45,
    }


PHONE = "56932133850"


def _texto(resp) -> str:
    return str(resp or "")


def _elegir_slot(slot: dict):
    reset_session(PHONE)
    save_session(PHONE, "WAIT_SLOT", {"especialidad": "psicología adulto"})
    data = get_session(PHONE)["data"]
    return asyncio.run(flows._slot_confirmed(PHONE, data, slot))


def _responder(texto: str):
    return asyncio.run(flows.handle_message(PHONE, texto, get_session(PHONE)))


def test_salas_pregunta_modalidad():
    resp = _elegir_slot(_slot(82, "Ps. Jacquelinne Salas"))
    assert get_session(PHONE)["state"] == "WAIT_MODALIDAD_ATENCION"
    assert "mod_presencial" in _texto(resp) and "mod_video" in _texto(resp)


@pytest.mark.parametrize("respuesta, online", [
    ("mod_video", True), ("mod_presencial", False),
    ("por videollamada porfa", True), ("presencial", False),
    ("videoconsulta", True), ("online", True),
])
def test_salas_respeta_lo_elegido(respuesta, online):
    _elegir_slot(_slot(82, "Ps. Jacquelinne Salas"))
    _responder(respuesta)
    ses = get_session(PHONE)
    assert ses["state"] != "WAIT_MODALIDAD_ATENCION"
    slot = ses["data"]["slot_elegido"]
    assert flows._es_teleconsulta(slot) is online
    assert (ses["data"].get("telemedicina_modalidad") == "TELEMEDICINA") is online


def test_respuesta_no_entendida_vuelve_a_preguntar():
    _elegir_slot(_slot(82, "Ps. Jacquelinne Salas"))
    resp = _responder("mmm no sé")
    assert get_session(PHONE)["state"] == "WAIT_MODALIDAD_ATENCION"
    assert "videollamada" in _texto(resp).lower()


def test_salas_sin_elegir_sigue_presencial():
    """Si llega por un camino que no pregunta, el default es presencial (lo seguro)."""
    assert flows._es_teleconsulta(_slot(82, "Ps. Jacquelinne Salas")) is False


def test_montalba_no_pregunta():
    """Montalba se resuelve por día (lun-vie online): no cambia."""
    _elegir_slot(_slot(74, "Jorge Montalba"))
    assert get_session(PHONE)["state"] != "WAIT_MODALIDAD_ATENCION"
    assert flows._es_teleconsulta(_slot(74, "Jorge Montalba")) is True
