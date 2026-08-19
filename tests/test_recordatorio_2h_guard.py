"""Regresión: guard de "cita ya pasó" en el recordatorio de 2h (reminders.py).

Cubre el problema #1 del consolidado MENSUAL 2026-08-19 (×38, el mayor del
mes): recordatorios "2h antes" que llegaban 1-2 horas DESPUÉS de la cita.
Causa real (no timezone, medida en prod): la ventana [1h45, 2h15] se calcula
UNA VEZ al inicio del job, pero la pre-validación contra Medilink (una
llamada por cita, throttle 0.7s + reintentos si Medilink está lento/caído)
puede demorar minutos en un backlog grande — para cuando el loop de envío
llega a esa cita, la hora real ya pasó. El fix (commit c05c7e9, deployado
2026-08-19) agrega una re-validación de hora justo antes de enviar cada
mensaje del loop.

Todos los hallazgos de auditoría de este problema (38 en 30 días, incl. los
2 últimos corridos el mismo 19-ago) tienen `run_ts` anterior al deploy de
c05c7e9 (17:58 UTC) — este test fija el comportamiento hacia adelante.

Ejecución:
    PYTHONPATH=app:. venv/bin/python3 tests/test_recordatorio_2h_guard.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT))

TMP_DB_DIR = Path(tempfile.mkdtemp(prefix="cmc_test_reminders_"))
TMP_DB = TMP_DB_DIR / "test_sessions.db"
os.environ["SESSIONS_DB"] = str(TMP_DB)

import session  # noqa: E402
session.DB_PATH = TMP_DB

import reminders  # noqa: E402

_TZ_CL = ZoneInfo("America/Santiago")


class TestRecordatorio2hGuardHoraPasada(unittest.TestCase):
    """La cita seleccionada al INICIO del job puede haber pasado para cuando
    el loop de envío llega a ella (backlog de validación Medilink). El guard
    debe saltarla SIN enviar mensaje, sin importar qué haya dicho la ventana
    de selección inicial."""

    def setUp(self):
        self.enviados: list[tuple] = []

        async def _fake_send_text(phone, msg):
            self.enviados.append((phone, msg))

        self.fake_send_text = _fake_send_text

    def _run(self, citas_mock: list[dict]):
        async def _fake_estado_pre_envio(id_cita, id_prof, fecha):
            return {"anulada": False, "id_paciente": None}

        with mock.patch.object(reminders, "get_citas_bot_para_2h_reminder",
                                side_effect=lambda *a, **k: citas_mock), \
             mock.patch.object(reminders, "_estado_pre_envio",
                                side_effect=_fake_estado_pre_envio), \
             mock.patch.object(session, "mark_cita_cancel_detected",
                                lambda *a, **k: None), \
             mock.patch.object(reminders, "get_last_inbound_ts",
                                lambda *a, **k: None), \
             mock.patch("asyncio.sleep", new=mock.AsyncMock()):
            asyncio.run(reminders.enviar_recordatorios_2h(self.fake_send_text))

    def test_cita_ya_pasada_no_se_envia(self):
        """Caso real 56985831922/56994853413: la ventana de selección dijo
        'en 2 horas' pero para cuando el loop llegó a esa cita (backlog),
        ya habían pasado. Debe saltarse SIN enviar nada."""
        ahora = datetime.now(_TZ_CL)
        pasada = ahora - timedelta(minutes=20)
        cita = {
            "id": 77001,
            "id_cita": "77001",
            "phone": "56900001001",
            "fecha": pasada.strftime("%Y-%m-%d"),
            "hora": pasada.strftime("%H:%M:%S"),
            "especialidad": "Medicina General",
            "profesional": "Dr. Rodrigo Olavarría",
            "paciente_nombre": "Paciente Prueba",
            "es_tercero": 0,
        }
        self._run([cita])
        self.assertEqual(self.enviados, [],
                          "No debe enviar recordatorio de una cita que ya pasó")

    def test_cita_vigente_si_se_envia(self):
        """Control: una cita realmente en el futuro (dentro de la ventana)
        SÍ debe generar el envío — el guard no debe volverse un apagador
        general."""
        ahora = datetime.now(_TZ_CL)
        futura = ahora + timedelta(hours=2)
        cita = {
            "id": 77002,
            "id_cita": "77002",
            "phone": "56900001002",
            "fecha": futura.strftime("%Y-%m-%d"),
            "hora": futura.strftime("%H:%M:%S"),
            "especialidad": "Medicina General",
            "profesional": "Dr. Rodrigo Olavarría",
            "paciente_nombre": "Paciente Prueba",
            "es_tercero": 0,
        }
        self._run([cita])
        # >=1 (no ==1): en horario peak (16-19h) el job también manda el
        # aviso adicional de liberación de slot anti-no-show — eso es
        # comportamiento correcto aparte, no lo que este test verifica.
        self.assertGreaterEqual(len(self.enviados), 1,
                                 "Una cita vigente dentro de la ventana debe enviar recordatorio")
        self.assertTrue(any("En 2 horas" in msg for _, msg in self.enviados))

    def test_cita_justo_en_el_limite_no_se_envia(self):
        """Borde: hora_cita == now() → ya no es 'en 2 horas', es 'ahora mismo'
        o antes. El guard usa <=, debe saltarla."""
        ahora = datetime.now(_TZ_CL).replace(microsecond=0)
        cita = {
            "id": 77003,
            "id_cita": "77003",
            "phone": "56900001003",
            "fecha": ahora.strftime("%Y-%m-%d"),
            "hora": ahora.strftime("%H:%M:%S"),
            "especialidad": "Medicina General",
            "profesional": "Dr. Rodrigo Olavarría",
            "paciente_nombre": "Paciente Prueba",
            "es_tercero": 0,
        }
        self._run([cita])
        self.assertEqual(self.enviados, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
