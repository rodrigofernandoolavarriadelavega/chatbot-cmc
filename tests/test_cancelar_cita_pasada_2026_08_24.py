"""
Regresión — consolidado 2026-08-24, hallazgo #13: cancelar una cita que ya
transcurrió. Caso real: el intento de cancelar llegó 1h47 DESPUÉS de la hora
de la cita (vía recordatorio automático) — el bot llamaba a Medilink igual,
fallaba con un error genérico ("Hubo un problema al cancelar") y derivaba a
teléfono sin explicar la causa real.

Fix en `app/flows.py` (CONFIRMING_CANCEL) + `app/medilink.py`:
- Antes de llamar a `cancelar_cita`, si fecha+hora de la cita ya pasó (hora
  Chile), responde directo sin tocar la API: "ya transcurrió, no es
  necesario cancelarla".
- Si la API falla por otra razón, el mensaje ahora incluye un motivo legible
  (`cancelar_cita_con_motivo`, nuevo — `cancelar_cita` sigue existiendo como
  wrapper compatible para el resto de los callers).

Uso:
    PYTHONPATH=app:. python tests/test_cancelar_cita_pasada_2026_08_24.py
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

TMP_DB = Path(tempfile.mkdtemp(prefix="cmc_test_cancel_pasada_")) / "test_sessions.db"
os.environ["SESSIONS_DB"] = str(TMP_DB)
os.environ.setdefault("SQLCIPHER_KEY", "")

import session  # noqa: E402
session.DB_PATH = TMP_DB

import medilink as medilink_mod  # noqa: E402
import flows as flows_mod  # noqa: E402

_CHILE_TZ = ZoneInfo("America/Santiago")


def _cita_data(fecha: str, hora: str, especialidad="Psicología Adulto",
                profesional="Jorge Montalba", id_cita=63286):
    return {
        "cita_cancelar": {
            "id": id_cita,
            "especialidad": especialidad,
            "profesional": profesional,
            "fecha": fecha,
            "fecha_display": fecha,
            "hora_inicio": hora,
        }
    }


class TestCancelarCitaYaPasada(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        with session.db() as conn:
            conn.execute("DELETE FROM sessions")
            conn.execute("DELETE FROM conversation_events")
            conn.commit()

    async def test_cita_pasada_no_llama_a_medilink(self):
        ayer = (datetime.now(_CHILE_TZ) - timedelta(days=1)).strftime("%Y-%m-%d")
        data = _cita_data(ayer, "09:00")
        with patch.object(flows_mod, "cancelar_cita_con_motivo",
                           new=AsyncMock(return_value=(True, ""))) as mock_cancel:
            resp = await flows_mod.handle_message(
                "56900000301", "si", {"state": "CONFIRMING_CANCEL", "data": data})
        mock_cancel.assert_not_called()
        self.assertIn("ya transcurrió", resp)
        self.assertIn("no es necesario cancelarla", resp)

    async def test_cita_hoy_pero_hora_pasada_no_llama_a_medilink(self):
        hoy = datetime.now(_CHILE_TZ)
        hora_pasada = (hoy - timedelta(hours=2)).strftime("%H:%M")
        data = _cita_data(hoy.strftime("%Y-%m-%d"), hora_pasada)
        with patch.object(flows_mod, "cancelar_cita_con_motivo",
                           new=AsyncMock(return_value=(True, ""))) as mock_cancel:
            resp = await flows_mod.handle_message(
                "56900000302", "si", {"state": "CONFIRMING_CANCEL", "data": data})
        mock_cancel.assert_not_called()
        self.assertIn("ya transcurrió", resp)

    async def test_cita_futura_si_llama_a_medilink(self):
        manana = (datetime.now(_CHILE_TZ) + timedelta(days=1)).strftime("%Y-%m-%d")
        data = _cita_data(manana, "09:00")
        with patch.object(flows_mod, "cancelar_cita_con_motivo",
                           new=AsyncMock(return_value=(True, ""))) as mock_cancel:
            resp = await flows_mod.handle_message(
                "56900000303", "si", {"state": "CONFIRMING_CANCEL", "data": data})
        mock_cancel.assert_called_once()
        _texto = resp["interactive"]["body"]["text"] if isinstance(resp, dict) else resp
        self.assertIn("cancelada", _texto.lower())

    async def test_falla_api_incluye_motivo_legible(self):
        manana = (datetime.now(_CHILE_TZ) + timedelta(days=1)).strftime("%Y-%m-%d")
        data = _cita_data(manana, "09:00")
        with patch.object(flows_mod, "cancelar_cita_con_motivo",
                           new=AsyncMock(return_value=(False, "el sistema de agenda no respondió a tiempo"))):
            resp = await flows_mod.handle_message(
                "56900000304", "si", {"state": "CONFIRMING_CANCEL", "data": data})
        self.assertIn("no respondió a tiempo", resp)
        self.assertIn("problema al cancelar", resp.lower())


class TestCancelarCitaConMotivoWrapper(unittest.IsolatedAsyncioTestCase):
    """cancelar_cita() (bool) sigue funcionando igual para el resto de los
    callers (admin_routes.py, agendador_routes.py) — no debe regresionar."""

    async def test_cancelar_cita_bool_ok(self):
        with patch.object(medilink_mod, "cancelar_cita_con_motivo",
                           new=AsyncMock(return_value=(True, ""))):
            ok = await medilink_mod.cancelar_cita(12345)
        self.assertTrue(ok)

    async def test_cancelar_cita_bool_fail(self):
        with patch.object(medilink_mod, "cancelar_cita_con_motivo",
                           new=AsyncMock(return_value=(False, "algo falló"))):
            ok = await medilink_mod.cancelar_cita(12345)
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main(verbosity=2)
