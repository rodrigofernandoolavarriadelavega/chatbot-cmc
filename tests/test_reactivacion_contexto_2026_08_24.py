"""
Regresión — consolidado 2026-08-24, hallazgo #2 (×8): el proactivo de
reactivación/control nombra una especialidad concreta (eco/psiquiatría/kine/
nutrición/cardiología...), pero al tocar el botón "Sí" el bot perdía ese
contexto (pasaba especialidad=None a `_iniciar_agendar`) y mostraba el menú
genérico de especialidades en vez de retomar directo.

Root cause: `set_pending_crosssell(phone, tipo, especialidad)` SÍ persiste el
contexto al enviar el proactivo, y el consumer de TEXTO LIBRE ("sí, ver
horas") ya lo leía bien — pero los handlers de los BOTONES dedicados
("reac_si", "ctrl_si") nunca lo consultaban, hardcodeando
`_iniciar_agendar(phone, data, None)`.

Fix: `reac_si` y `ctrl_si` ahora leen `get_pending_crosssell` y pasan la
especialidad guardada.

Uso:
    PYTHONPATH=app:. python tests/test_reactivacion_contexto_2026_08_24.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT))

TMP_DB = Path(tempfile.mkdtemp(prefix="cmc_test_reac_ctx_")) / "test_sessions.db"
os.environ["SESSIONS_DB"] = str(TMP_DB)
os.environ.setdefault("SQLCIPHER_KEY", "")

import session  # noqa: E402
session.DB_PATH = TMP_DB
from session import set_pending_crosssell  # noqa: E402

import medilink as medilink_mod  # noqa: E402
medilink_mod.buscar_primer_dia = AsyncMock(return_value=([], []))
medilink_mod.buscar_paciente = AsyncMock(return_value=None)
import messaging as messaging_mod  # noqa: E402
messaging_mod.send_whatsapp = AsyncMock(return_value="wamid.TEST")

import flows as flows_mod  # noqa: E402


class TestReactivacionSiConservaContexto(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        with session.db() as conn:
            conn.execute("DELETE FROM sessions")
            conn.execute("DELETE FROM conversation_events")
            conn.commit()

    async def test_reac_si_pasa_la_especialidad_guardada(self):
        phone = "56900000701"
        set_pending_crosssell(phone, "reactivacion", "ecografía")
        with patch.object(flows_mod, "_iniciar_agendar",
                           new=AsyncMock(return_value="ok")) as mock_ag:
            await flows_mod.handle_message(
                phone, "reac_si", {"state": "IDLE", "data": {}})
        mock_ag.assert_called_once()
        _args, _ = mock_ag.call_args
        self.assertEqual(_args[2], "ecografía")

    async def test_reac_si_sin_pending_no_revienta(self):
        phone = "56900000702"
        with patch.object(flows_mod, "_iniciar_agendar",
                           new=AsyncMock(return_value="ok")) as mock_ag:
            await flows_mod.handle_message(
                phone, "reac_si", {"state": "IDLE", "data": {}})
        mock_ag.assert_called_once()
        _args, _ = mock_ag.call_args
        self.assertIsNone(_args[2])

    async def test_ctrl_si_pasa_la_especialidad_guardada(self):
        phone = "56900000703"
        set_pending_crosssell(phone, "control_psicología_adulto", "psicología adulto")
        with patch.object(flows_mod, "_iniciar_agendar",
                           new=AsyncMock(return_value="ok")) as mock_ag:
            await flows_mod.handle_message(
                phone, "ctrl_si", {"state": "IDLE", "data": {}})
        mock_ag.assert_called_once()
        _args, _ = mock_ag.call_args
        self.assertEqual(_args[2], "psicología adulto")

    async def test_ctrl_si_no_confunde_con_pending_de_otro_tipo(self):
        """Si el pending_crosssell activo es de OTRO tipo (ej. adherencia
        kine), ctrl_si no debe robarle el contexto."""
        phone = "56900000704"
        set_pending_crosssell(phone, "adherencia_kine", "kinesiología")
        with patch.object(flows_mod, "_iniciar_agendar",
                           new=AsyncMock(return_value="ok")) as mock_ag:
            await flows_mod.handle_message(
                phone, "ctrl_si", {"state": "IDLE", "data": {}})
        mock_ag.assert_called_once()
        _args, _ = mock_ag.call_args
        self.assertIsNone(_args[2])


if __name__ == "__main__":
    unittest.main(verbosity=2)
