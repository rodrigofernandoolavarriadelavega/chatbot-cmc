"""
Regresión — consolidado 2026-08-24, hallazgo #1 (×12): confirmaciones/cierres
con typos y chilenismos ("Sii", "Siiii", "Si!", "hay estaré", "ahi estaré",
"ahi voy", "oki", "okey", "agradecida", "si asistiré. saludos") caían al menú
de bienvenida completo en vez de confirmar asistencia (post-recordatorio) o
dar un cierre breve (post-confirmación/cierre).

Fix en `app/flows.py`:
- `_TOKENS_CONFIRM_RECOD_SOFT` (nuevo): tokens suaves que, CON cita pendiente
  de confirmar (reminder_sent=1, sin confirmation_status), confirman
  asistencia igual que "confirmo"/"asistiré".
- Sin cita pendiente, los tokens suaves YA NO devuelven "¿Qué quieres
  confirmar?" (esa pregunta queda solo para "confirmo"/"asistiré" explícitos)
  — caen al flujo normal, que termina en el cierre breve de `_CLOSINGS`.
- `_CLOSINGS` ganó "sii", "si!", "oki", "agradecida", "hay estare"/"hay
  estaré", "ahi voy"/"ahí voy".

Uso:
    PYTHONPATH=app:. python tests/test_confirmacion_typos_2026_08_24.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT))

TMP_DB = Path(tempfile.mkdtemp(prefix="cmc_test_confirm_typos_")) / "test_sessions.db"
os.environ["SESSIONS_DB"] = str(TMP_DB)
os.environ.setdefault("SQLCIPHER_KEY", "")

import session  # noqa: E402
session.DB_PATH = TMP_DB


def _make_mock_medilink():
    import medilink as _ml
    _ml.buscar_primer_dia = AsyncMock(return_value=([], []))
    _ml.buscar_slots_dia = AsyncMock(return_value=[])
    _ml.buscar_paciente = AsyncMock(return_value=None)
    _ml.listar_citas_paciente = AsyncMock(return_value=[])
    _ml.consultar_proxima_fecha = AsyncMock(return_value=None)


def _make_mock_messaging():
    import messaging as _msg
    _msg.send_whatsapp = AsyncMock(return_value="wamid.TEST")


_make_mock_medilink()
_make_mock_messaging()
import flows as flows_mod  # noqa: E402 — después de mockear medilink/messaging


def _seed_cita_con_recordatorio(phone: str, id_cita: str = "9101",
                                 especialidad: str = "Medicina General",
                                 profesional: str = "Dr. Prueba",
                                 fecha: str = "2099-12-31", hora: str = "10:00"):
    with session.db() as conn:
        conn.execute(
            "INSERT INTO citas_bot (phone, id_cita, especialidad, profesional, "
            "fecha, hora, modalidad, reminder_sent) VALUES (?,?,?,?,?,?,?,1)",
            (phone, id_cita, especialidad, profesional, fecha, hora, "Presencial"),
        )
        conn.commit()


class TestConfirmacionPostRecordatorio(unittest.IsolatedAsyncioTestCase):
    """Con cita pendiente de confirmar, los tokens suaves SÍ confirman
    asistencia (no deben caer ni al menú ni a un cierre genérico)."""

    def setUp(self):
        with session.db() as conn:
            conn.execute("DELETE FROM sessions")
            conn.execute("DELETE FROM citas_bot")
            conn.execute("DELETE FROM conversation_events")
            conn.commit()

    async def _check_confirma(self, phone: str, txt: str):
        _seed_cita_con_recordatorio(phone)
        resp = await flows_mod.handle_message(phone, txt, {"state": "IDLE", "data": {}})
        self.assertIn("confirmada", resp.lower(), f"{txt!r} -> {resp!r}")
        self.assertNotIn("¿Qué necesitas hoy?", resp, f"{txt!r} cayó al menú")

    async def test_sii(self):
        await self._check_confirma("56900000001", "Sii")

    async def test_hay_estare(self):
        await self._check_confirma("56900000002", "hay estaré")

    async def test_ahi_voy(self):
        await self._check_confirma("56900000003", "ahi voy")

    async def test_oki(self):
        await self._check_confirma("56900000004", "oki")

    async def test_okey(self):
        await self._check_confirma("56900000005", "okey")

    async def test_agradecida(self):
        await self._check_confirma("56900000006", "agradecida")

    async def test_si_asistire_saludos(self):
        await self._check_confirma("56900000007", "si asistiré. saludos")


class TestCierreBreveSinCitaPendiente(unittest.IsolatedAsyncioTestCase):
    """Sin ninguna cita con recordatorio pendiente, los tokens suaves deben
    dar un cierre breve — nunca el menú completo ni "¿Qué quieres
    confirmar?" (esa pregunta es solo para "confirmo" explícito)."""

    def setUp(self):
        with session.db() as conn:
            conn.execute("DELETE FROM sessions")
            conn.execute("DELETE FROM citas_bot")
            conn.execute("DELETE FROM conversation_events")
            conn.commit()

    async def _check_cierre_breve(self, phone: str, txt: str):
        resp = await flows_mod.handle_message(phone, txt, {"state": "IDLE", "data": {}})
        self.assertNotIn("¿Qué necesitas hoy?", resp, f"{txt!r} cayó al menú: {resp!r}")
        self.assertNotIn("¿Qué quieres confirmar?", resp, f"{txt!r} -> {resp!r}")

    async def test_sii(self):
        await self._check_cierre_breve("56900000011", "Sii")

    async def test_siiii(self):
        # "Siiii" colapsa a "si" en el normalizador léxico (regla de 3+
        # vocales repetidas): hereda el comportamiento YA EXISTENTE de "si"
        # bare (línea ~3993, hook horas_vacías/atajo agendar), no tocado por
        # este fix a propósito — bare "si" ya tenía usos legítimos en otros
        # flujos (ej. atajo numérico) y widenarlo acá los rompería. Solo se
        # deja registro de que no revienta.
        resp = await flows_mod.handle_message(
            "56900000012", "Siiii", {"state": "IDLE", "data": {}})
        self.assertTrue(resp)

    async def test_si_exclamacion(self):
        await self._check_cierre_breve("56900000013", "Si!")

    async def test_oki(self):
        await self._check_cierre_breve("56900000014", "oki")

    async def test_agradecida(self):
        await self._check_cierre_breve("56900000015", "agradecida")

    async def test_hay_estare(self):
        await self._check_cierre_breve("56900000016", "hay estaré")

    async def test_ahi_voy(self):
        await self._check_cierre_breve("56900000017", "ahi voy")

    async def test_confirmo_explicito_sigue_preguntando(self):
        """Control: 'confirmo' explícito sin cita pendiente SIGUE pidiendo
        aclaración (no es un cierre) — no se debe regresionar ese caso."""
        resp = await flows_mod.handle_message(
            "56900000018", "confirmo", {"state": "IDLE", "data": {}})
        self.assertIn("qué quieres confirmar", resp.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
