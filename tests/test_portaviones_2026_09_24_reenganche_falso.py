"""Regresión: portaviones 2026-09-24 #5 — "Tienes una reserva pendiente"
disparado sin slot elegido o con la cita ya confirmada.

Casos reales verificados en prod (`/admin/api/conversations/<phone>`):
  - fb_38574770855503490 / fb_9250731408388759: estado WAIT_ESPECIALIDAD,
    el paciente NUNCA eligió especialidad — no hay ningún horario que
    "reservar", pero `_enviar_reenganche` mandaba igual "Tienes una reserva
    pendiente... ¿Te la reservo antes de que se llene?" (cae al `else`
    genérico porque WAIT_ESPECIALIDAD no tenía rama propia).
  - 56931103936 (WAIT_AGENDAR_OTRO) / 56975778835 y 56961930267
    (WAIT_PARENTESCO): la cita YA estaba confirmada ("¡Listo! Tu hora quedó
    reservada") y el bot preguntaba una cosa puramente opcional (parentesco /
    agendar para otra persona) — el reenganche igual mandó "tienes una
    reserva pendiente" 14-20 min después, un mensaje directamente falso.

Fix: WAIT_ESPECIALIDAD tiene copy honesto propio; WAIT_PARENTESCO y
WAIT_AGENDAR_OTRO se suman al bloque de "oferta opcional post-acción" que ya
existía para WAIT_CROSS_SELL/WAIT_REFERRAL_POST (mismo patrón, mismo motivo:
la reserva ya está hecha, no hay nada que "salvar").

Ejecución:
    PYTHONPATH=app:. venv/bin/python3 tests/test_portaviones_2026_09_24_reenganche_falso.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT))

TMP_DB_DIR = Path(tempfile.mkdtemp(prefix="cmc_test_reenganche_falso_"))
TMP_DB = TMP_DB_DIR / "test_sessions.db"
os.environ["SESSIONS_DB"] = str(TMP_DB)

import session  # noqa: E402
session.DB_PATH = TMP_DB

import jobs  # noqa: E402


def _insert_session(phone: str, state: str, data: dict, minutes_ago: int):
    updated = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    with session.db() as c:
        c.execute(
            "INSERT OR REPLACE INTO sessions (phone, state, data, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (phone, state, json.dumps(data), updated.strftime("%Y-%m-%d %H:%M:%S")),
        )


class TestReenganchePendienteFalso(unittest.TestCase):

    def setUp(self):
        self.mensajes: list[tuple] = []

        async def _fake_interactive(phone, interactive):
            body = interactive.get("body", {}).get("text", "")
            self.mensajes.append((phone, body))

        self._patches = [
            mock.patch.object(jobs, "send_whatsapp_interactive",
                               new=mock.AsyncMock(side_effect=_fake_interactive)),
            mock.patch.object(jobs, "is_medilink_down", lambda: True),  # sin red
            mock.patch.object(jobs, "verificar_cita_externa",
                               new=mock.AsyncMock(return_value="sin_rut")),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def test_wait_especialidad_no_dice_reserva_pendiente(self):
        """fb_38574770855503490 / fb_9250731408388759: nada elegido todavía,
        el copy no debe hablar de una reserva."""
        phone = "56900001111"
        _insert_session(phone, "WAIT_ESPECIALIDAD", {}, minutes_ago=20)

        asyncio.run(jobs._enviar_reenganche())

        self.assertEqual(len(self.mensajes), 1, "Debe reenganchar (abandono real)")
        _, body = self.mensajes[0]
        self.assertNotIn("reserva pendiente", body.lower(),
                          "WAIT_ESPECIALIDAD no debe afirmar que hay una reserva pendiente")
        self.assertIn("especialidad", body.lower())

    def test_wait_parentesco_no_reenganchea(self):
        """56975778835 / 56961930267: la cita YA quedó reservada, solo falta
        la pregunta opcional de parentesco. No debe mandar nada."""
        phone = "56900002222"
        _insert_session(phone, "WAIT_PARENTESCO", {"nombre_conocido": "Emilia"}, minutes_ago=15)

        asyncio.run(jobs._enviar_reenganche())

        self.assertEqual(self.mensajes, [],
                          "No debe reenganchar sobre una cita ya confirmada (WAIT_PARENTESCO)")

    def test_wait_agendar_otro_no_reenganchea(self):
        """56931103936: cita ya reservada, falta la oferta opcional de
        agendar para otra persona. No debe mandar nada."""
        phone = "56900003333"
        _insert_session(phone, "WAIT_AGENDAR_OTRO", {"nombre_conocido": "Gabriela"}, minutes_ago=15)

        asyncio.run(jobs._enviar_reenganche())

        self.assertEqual(self.mensajes, [],
                          "No debe reenganchar sobre una cita ya confirmada (WAIT_AGENDAR_OTRO)")

    def test_wait_slot_sigue_diciendo_reserva_pendiente(self):
        """Control: el caso REAL de slot elegido sin confirmar debe seguir
        con su copy propio (no se rompió el resto de la máquina)."""
        phone = "56900004444"
        _insert_session(phone, "WAIT_SLOT", {"especialidad": "medicina general"}, minutes_ago=35)

        asyncio.run(jobs._enviar_reenganche())

        self.assertEqual(len(self.mensajes), 1)
        _, body = self.mensajes[0]
        self.assertIn("elegir tu hora", body.lower())


if __name__ == "__main__":
    unittest.main()
