"""El dueño prueba el bot desde su número: 'paciente' debe ser paciente de verdad.

Caso 6-oct (56987834148): en modo paciente del Asistente Adkun, el "Hola" caía
en el asistente clínico (capa de comandos del doctor) y en "Agente CMC" un
"Hola" lo expulsaba al menú de modos → imposible probar la respuesta de
ortodoncia.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT))

_TMP_DB = Path(tempfile.mkdtemp(prefix="cmc_modo_dueno_")) / "sessions.db"
os.environ["SESSIONS_DB"] = str(_TMP_DB)
os.environ.setdefault("SQLCIPHER_KEY", "")

import session as _session_mod  # noqa: E402

_session_mod.DB_PATH = _TMP_DB

import adkun_assistant as adk  # noqa: E402
import flows  # noqa: E402
import resilience  # noqa: E402
from session import get_session, reset_session, save_tag  # noqa: E402

resilience.is_medilink_down = lambda: False
flows.is_medilink_down = lambda: False
DUENO = flows.ADMIN_ALERT_PHONE or "56987834148"
flows.ADMIN_ALERT_PHONE = DUENO


def _txt(r) -> str:
    return str(r or "")


def _handle(texto: str) -> str:
    return _txt(asyncio.run(flows.handle_message(DUENO, texto, get_session(DUENO))))


def test_modo_paciente_no_pasa_por_capa_doctor():
    reset_session(DUENO)
    flows._set_doctor_mode(DUENO, "asistente")      # tag que tenía el 6-oct
    handled, _ = adk.route(DUENO, "modo paciente")
    assert handled
    assert adk.route(DUENO, "Hola") == (False, None)  # cae al flujo de pacientes
    resp = _handle("Hola")
    assert "asistente clínico" not in resp.lower()
    assert "doc_modo_agente" not in resp


def test_agente_cmc_no_se_sale_con_hola():
    reset_session(DUENO)
    adk.set_mode(DUENO, "cmc")  # no paciente → la capa doctor sí corre
    flows._set_doctor_mode(DUENO, "agente")
    resp = _handle("Hola")
    assert "doc_modo_agente" not in resp           # no vuelve al menú de modos
    assert flows._get_doctor_mode(DUENO) == "agente"


def test_modo_doctor_explicito():
    handled, resp = adk.route(DUENO, "doctor")
    assert handled and "Asistente clínico" in resp
    assert adk.get_mode(DUENO) == "doctor"
    assert flows._get_doctor_mode(DUENO) == "asistente"
    assert adk.route(DUENO, "agenda") == (False, None)  # lo atiende la capa clínica


def test_modo_y_modos_muestran_selector():
    for t in ("modo", "modos"):
        handled, resp = adk.route(DUENO, t)
        assert handled and "4 modos" in resp
