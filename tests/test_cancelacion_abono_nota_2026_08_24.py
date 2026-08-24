"""
Regresión — consolidado 2026-08-24, hallazgo #11 (2da parte): al cancelar
una cita de especialidad con abono obligatorio (Psiquiatría/Gastro), el
mensaje de "Cita cancelada" no mencionaba nada sobre la plata ya pagada.

Fix: `CONFIRMING_CANCEL` agrega una nota (recepción coordina devolución/
reprogramación) cuando la especialidad cancelada tiene una regla de abono
con `gate_bot=True` y el flag está activo.

Uso:
    PYTHONPATH=app:. python tests/test_cancelacion_abono_nota_2026_08_24.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT))

TMP_DB = Path(tempfile.mkdtemp(prefix="cmc_test_cancel_abono_")) / "test_sessions.db"
os.environ["SESSIONS_DB"] = str(TMP_DB)
os.environ.setdefault("SQLCIPHER_KEY", "")

import session  # noqa: E402
session.DB_PATH = TMP_DB

import flows as flows_mod  # noqa: E402

_CHILE_TZ = ZoneInfo("America/Santiago")


def _cita_data(especialidad, profesional="Dra. Cecilia Unibazo", id_cita=7001):
    manana = (datetime.now(_CHILE_TZ) + timedelta(days=1)).strftime("%Y-%m-%d")
    return {
        "cita_cancelar": {
            "id": id_cita, "especialidad": especialidad, "profesional": profesional,
            "fecha": manana, "fecha_display": manana, "hora_inicio": "16:00",
        }
    }


def _body_text(resp):
    if isinstance(resp, dict):
        return resp.get("interactive", {}).get("body", {}).get("text", "")
    return resp


class TestNotaDevolucionAbono(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        with session.db() as conn:
            conn.execute("DELETE FROM sessions")
            conn.execute("DELETE FROM conversation_events")
            conn.commit()

    async def test_cancelar_psiquiatria_menciona_devolucion(self):
        with patch.object(flows_mod, "_abono_gate_psiq_activo", return_value=True), \
             patch.object(flows_mod, "cancelar_cita_con_motivo",
                           new=AsyncMock(return_value=(True, ""))):
            resp = await flows_mod.handle_message(
                "56900000901", "si",
                {"state": "CONFIRMING_CANCEL", "data": _cita_data("Psiquiatría")})
        texto = _body_text(resp)
        self.assertIn("recepción", texto.lower())
        self.assertIn("devolución", texto.lower())

    async def test_cancelar_medicina_general_no_menciona_devolucion(self):
        with patch.object(flows_mod, "_abono_gate_psiq_activo", return_value=True), \
             patch.object(flows_mod, "cancelar_cita_con_motivo",
                           new=AsyncMock(return_value=(True, ""))):
            resp = await flows_mod.handle_message(
                "56900000902", "si",
                {"state": "CONFIRMING_CANCEL",
                 "data": _cita_data("Medicina General", "Dr. Rodrigo Olavarría")})
        texto = _body_text(resp)
        self.assertNotIn("devolución", texto.lower())

    async def test_flag_apagado_no_menciona_devolucion(self):
        with patch.object(flows_mod, "_abono_gate_psiq_activo", return_value=False), \
             patch.object(flows_mod, "cancelar_cita_con_motivo",
                           new=AsyncMock(return_value=(True, ""))):
            resp = await flows_mod.handle_message(
                "56900000903", "si",
                {"state": "CONFIRMING_CANCEL", "data": _cita_data("Psiquiatría")})
        texto = _body_text(resp)
        self.assertNotIn("devolución", texto.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
