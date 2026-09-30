"""
Regresión — fallas del bot en pacientes reales del 2026-09-29.

1. Constanza (...7808): en WAIT_RUT_AGENDAR tocó el botón "📅 Horario u otro
   día" de la confirmación anterior; llegó como `cd_horario` y se leyó como RUT
   ("no reconozco ese RUT"). Luego "Hora para hoy" → lo mismo. Se perdió.
2. Regla sistémica: en cualquier WAIT_RUT_*, lo que no puede ser RUT (< 6
   dígitos) se atiende por lo que es (botón, otra fecha, pregunta) y NO cuenta
   como RUT fallido.
3. Ginecología (...0431): "¿nada antes?" en WAIT_SLOT → "No te entendí bien".
   Ahora: se le dice que no hay antes y se ofrece tomarla o lista de espera.

Uso:
    PYTHONPATH=app:. python tests/test_bugs_2026_09_29_rut_y_antes.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT))
TMP_DB = Path(tempfile.mkdtemp(prefix="cmc_test_20260929_")) / "s.db"
os.environ["SESSIONS_DB"] = str(TMP_DB)
os.environ.setdefault("SQLCIPHER_KEY", "")
import session  # noqa: E402
session.DB_PATH = TMP_DB

import medilink as _ml  # noqa: E402
_ml.buscar_primer_dia = AsyncMock(return_value=([], []))
_ml.buscar_slots_dia = AsyncMock(return_value=([], []))
_ml.buscar_paciente = AsyncMock(return_value=None)
_ml.listar_citas_paciente = AsyncMock(return_value=[])
_ml.consultar_proxima_fecha = AsyncMock(return_value=None)
import messaging as _msg  # noqa: E402
_msg.send_whatsapp = AsyncMock(return_value="wamid.TEST")

import flows as flows_mod  # noqa: E402
import claude_helper  # noqa: E402


async def _continue(mensaje, state, session_data):
    return {"action": "continue"}


flows_mod.classify_with_context = _continue
claude_helper.detect_intent = AsyncMock(return_value={"intent": "menu", "especialidad": None, "respuesta_directa": None})
flows_mod.detect_intent = claude_helper.detect_intent
flows_mod.respuesta_faq = AsyncMock(return_value="Sí, trae tu bono Fonasa o lo compras aquí con huella.")


def _clean():
    with session.db() as c:
        for t in ("sessions", "citas_bot", "conversation_events"):
            c.execute(f"DELETE FROM {t}")
        c.commit()


def _eventos(phone):
    with session.db() as c:
        return [r[0] for r in c.execute("SELECT event FROM conversation_events WHERE phone=?", (phone,))]


def _txt(resp):
    if isinstance(resp, dict):
        return str(resp)
    return resp or ""


_F = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d")
SLOT = {"especialidad": "Medicina Familiar", "profesional": "Dr. Alonso Márquez",
        "id_profesional": 13, "fecha": _F, "fecha_display": "Miércoles 7 de octubre",
        "hora_inicio": "16:00:00", "hora_fin": "16:20:00"}


class TestEntradaNoRut(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _clean()

    async def test_boton_cd_horario_no_se_lee_como_rut(self):
        p = "56900009001"
        data = {"especialidad": "medicina familiar", "slot_elegido": dict(SLOT),
                "modalidad": "fonasa", "booking_for_other": True}
        r = await flows_mod.handle_message(p, "cd_horario", {"state": "WAIT_RUT_AGENDAR", "data": data})
        self.assertNotIn("no reconozco ese rut", _txt(r).lower(), r)
        self.assertIn("rut_boton_anterior", _eventos(p))

    async def test_hora_para_hoy_busca_horario(self):
        p = "56900009002"
        data = {"especialidad": "medicina familiar", "slot_elegido": dict(SLOT), "modalidad": "fonasa"}
        r = await flows_mod.handle_message(p, "Hora para hoy", {"state": "WAIT_RUT_AGENDAR", "data": data})
        self.assertNotIn("no reconozco ese rut", _txt(r).lower(), r)
        self.assertIn("rut_pide_otra_fecha", _eventos(p))

    async def test_pregunta_se_responde_y_no_cuenta_como_rut_fallido(self):
        p = "56900009003"
        data = {"especialidad": "medicina general", "slot_elegido": dict(SLOT), "modalidad": "fonasa"}
        r = await flows_mod.handle_message(p, "Debo llevar bono Fonasa?", {"state": "WAIT_RUT_AGENDAR", "data": data})
        self.assertIn("bono", _txt(r).lower(), r)
        self.assertIn("rut", _txt(r).lower(), r)
        s = session.get_session(p)
        self.assertEqual(s["state"], "WAIT_RUT_AGENDAR")
        self.assertFalse(s["data"].get("intentos_rut_invalido"), s["data"])

    async def test_rut_mal_escrito_sigue_contando(self):
        p = "56900009004"
        data = {"especialidad": "medicina general", "slot_elegido": dict(SLOT), "modalidad": "fonasa"}
        r = await flows_mod.handle_message(p, "12.345.678-9", {"state": "WAIT_RUT_AGENDAR", "data": data})
        self.assertIn("rut", _txt(r).lower())
        self.assertEqual(session.get_session(p)["data"].get("intentos_rut_invalido"), 1)

    async def test_boton_viejo_en_cancelar_no_cuenta(self):
        p = "56900009005"
        r = await flows_mod.handle_message(p, "slot_3", {"state": "WAIT_RUT_CANCELAR", "data": {}})
        self.assertNotIn("no reconozco ese rut", _txt(r).lower(), r)
        self.assertFalse(session.get_session(p)["data"].get("rut_cancelar_intentos"))

    async def test_tres_textos_sin_rut_derivan_a_recepcion(self):
        p = "56900009006"
        st = {"state": "WAIT_RUT_VER", "data": {}}
        for t in ("no se", "mmm", "ya po"):
            await flows_mod.handle_message(p, t, st)
            st = session.get_session(p)
        self.assertIn("rut_entrada_no_rut", _eventos(p))
        self.assertNotEqual(st.get("state"), "WAIT_RUT_VER")


class TestNadaAntes(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _clean()

    async def test_nada_antes_ofrece_tomar_o_avisar(self):
        p = "56900009010"
        slot = {"especialidad": "Ginecología", "profesional": "Dr. Tirso Rejón", "id_profesional": 61,
                "fecha": _F, "fecha_display": "Martes 6 de octubre", "hora_inicio": "10:20:00",
                "hora_fin": "10:40:00"}
        data = {"especialidad": "ginecología", "slots": [slot], "todos_slots": [slot],
                "slot_sugerido": slot, "fechas_vistas": [_F]}
        r = await flows_mod.handle_message(p, "nada antes?", {"state": "WAIT_SLOT", "data": data})
        t = _txt(r)
        self.assertNotIn("no te entendí", t.lower(), r)
        self.assertIn("waitlist_antes_si", t)
        self.assertIn("confirmar_sugerido", t)


class TestListaDeEsperaEscrita(unittest.IsolatedAsyncioTestCase):
    """`accion_waitlist` ("lista de espera" escrito) no tenía handler."""
    def setUp(self):
        _clean()

    async def test_en_idle_abre_lista_de_espera(self):
        p = "56900009020"
        r = await flows_mod.handle_message(p, "lista de espera", {"state": "IDLE", "data": {}})
        self.assertIn("lista de espera", _txt(r).lower(), r)
        self.assertEqual(session.get_session(p)["state"], "WAIT_ESPECIALIDAD")

    async def test_eligiendo_horario_inscribe_para_esa_especialidad(self):
        p = "56900009021"
        slot = {"especialidad": "Ginecología", "profesional": "Dr. Tirso Rejón", "id_profesional": 61,
                "fecha": _F, "fecha_display": "Martes 6 de octubre", "hora_inicio": "10:20:00"}
        data = {"especialidad": "ginecología", "slots": [slot], "todos_slots": [slot], "slot_sugerido": slot}
        await flows_mod.handle_message(p, "lista de espera", {"state": "WAIT_SLOT", "data": data})
        s = session.get_session(p)
        self.assertEqual(s["state"], "WAIT_WAITLIST_RUT")
        self.assertEqual(s["data"].get("waitlist_especialidad"), "ginecología")


if __name__ == "__main__":
    unittest.main(verbosity=2)
