"""Regresión: portaviones 2026-09-24 #4 — followup_info genérico sobre
conversación activa.

Casos reales (7 días, consolidado): fb_29212688258334190, 56976424806,
56926015431, fb_28345312241786286, fb_24104026149198287. El común
denominador verificado en 2/5 (56926015431, fb_28345312241786286): el
paciente RESPONDIÓ tras el turno "info" que marcó `followup_info_ts`
(un "No gracias" explícito, o una pregunta más específica que ya recibió
una oferta con botón "Sí, agendar") pero el cron `_job_followup_info`
disparaba igual 10-25 min después con el genérico "¿te gustaría que te
ayude a agendar una hora?" — ignorando que la conversación ya avanzó.

Root cause: `followup_info_ts`/`followup_info_esp` se setean en el PRIMER
turno info y ninguna rama posterior (oferta específica con botón, "no
gracias", etc.) los limpiaba. Fix: `_job_followup_info` ahora chequea si
hubo algún mensaje ENTRANTE después de `followup_info_ts` — si lo hubo, la
conversación siguió y el follow-up se cancela en silencio (se marca
`followup_info_sent=True` sin enviar nada).

Ejecución:
    PYTHONPATH=app:. venv/bin/python3 tests/test_portaviones_2026_09_24_followup_info.py
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

TMP_DB_DIR = Path(tempfile.mkdtemp(prefix="cmc_test_followup_info_"))
TMP_DB = TMP_DB_DIR / "test_sessions.db"
os.environ["SESSIONS_DB"] = str(TMP_DB)

import session  # noqa: E402
session.DB_PATH = TMP_DB

import jobs  # noqa: E402


def _insert_session(phone: str, state: str, data: dict, updated_at: str):
    with session.db() as c:
        c.execute(
            "INSERT OR REPLACE INTO sessions (phone, state, data, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (phone, state, json.dumps(data), updated_at),
        )


def _insert_message(phone: str, direction: str, text: str, ts: str):
    with session.db() as c:
        c.execute(
            "INSERT INTO messages (phone, direction, text, state, ts) "
            "VALUES (?, ?, ?, 'IDLE', ?)",
            (phone, direction, text, ts),
        )


def _sql_ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


class TestFollowupInfoConversacionActiva(unittest.TestCase):

    def setUp(self):
        self.enviados: list[tuple] = []
        self._patches = [
            mock.patch.object(jobs, "send_whatsapp_interactive",
                               new=mock.AsyncMock(side_effect=self._capturar)),
            mock.patch.object(jobs, "send_instagram",
                               new=mock.AsyncMock(side_effect=self._capturar)),
            mock.patch.object(jobs, "send_messenger",
                               new=mock.AsyncMock(side_effect=self._capturar)),
            mock.patch("session.is_window_open", lambda phone: True),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    async def _capturar(self, phone, *args, **kwargs):
        self.enviados.append(phone)

    def test_no_envia_si_paciente_siguio_hablando(self):
        """Caso 56926015431: dijo 'No gracias solo quería saber' DESPUÉS del
        turno que marcó followup_info_ts. El cron no debe insistir."""
        phone = "56926015431"
        ahora = datetime.now(timezone.utc)
        info_ts = ahora - timedelta(minutes=15)
        _insert_session(phone, "IDLE", {
            "followup_info_ts": info_ts.isoformat(),
            "followup_info_esp": "",
            "followup_info_sent": False,
        }, updated_at=_sql_ts(ahora - timedelta(minutes=14)))
        # Mensajes reales: la pregunta info (antes de ts) y la respuesta
        # explícita del paciente (después de ts).
        _insert_message(phone, "in", "¿Cuál es el costo?", _sql_ts(info_ts - timedelta(seconds=5)))
        _insert_message(phone, "out", "La consulta...", _sql_ts(info_ts))
        _insert_message(phone, "in", "No gracias solo quería saber",
                         _sql_ts(info_ts + timedelta(minutes=1)))
        _insert_message(phone, "out", "¡De nada!",
                         _sql_ts(info_ts + timedelta(minutes=1, seconds=3)))

        asyncio.run(jobs._job_followup_info())

        self.assertEqual(self.enviados, [],
                          "No debe reenviar el follow-up genérico si el paciente ya respondió")

    def test_no_envia_si_ya_recibio_oferta_especifica(self):
        """Caso fb_28345312241786286: turno 1 info genérico (marca ts), turno
        2 pregunta específica que YA recibió una oferta con botón 'Sí,
        agendar'. El follow-up del turno 1 no debe pisar esa oferta."""
        phone = "fb_28345312241786286"
        ahora = datetime.now(timezone.utc)
        info_ts = ahora - timedelta(minutes=12)
        _insert_session(phone, "IDLE", {
            "followup_info_ts": info_ts.isoformat(),
            "followup_info_esp": "",
            "followup_info_sent": False,
            "especialidad_sugerida": "otorrinolaringología",
        }, updated_at=_sql_ts(ahora - timedelta(minutes=11)))
        _insert_message(phone, "in", "¿Costo de los servicios?", _sql_ts(info_ts - timedelta(seconds=5)))
        _insert_message(phone, "out", "Los precios varían...", _sql_ts(info_ts))
        _insert_message(phone, "in", "Otorrino valor?", _sql_ts(info_ts + timedelta(seconds=6)))
        _insert_message(phone, "out", "¿Te agendo en otorrinolaringología?",
                         _sql_ts(info_ts + timedelta(seconds=9)))

        asyncio.run(jobs._job_followup_info())

        self.assertEqual(self.enviados, [],
                          "No debe mandar el genérico encima de una oferta específica ya dada")

    def test_si_envia_cuando_realmente_no_hubo_respuesta(self):
        """Control: sin ningún mensaje entrante después de followup_info_ts,
        el follow-up debe seguir funcionando (no se rompió la feature)."""
        phone = "56900009999"
        ahora = datetime.now(timezone.utc)
        info_ts = ahora - timedelta(minutes=15)
        _insert_session(phone, "IDLE", {
            "followup_info_ts": info_ts.isoformat(),
            "followup_info_esp": "kinesiología",
            "followup_info_sent": False,
        }, updated_at=_sql_ts(ahora - timedelta(minutes=14)))
        _insert_message(phone, "in", "¿Hacen kinesiología?", _sql_ts(info_ts - timedelta(seconds=5)))
        _insert_message(phone, "out", "Sí, ¿qué especialidad...?", _sql_ts(info_ts))

        asyncio.run(jobs._job_followup_info())

        self.assertEqual(self.enviados, [phone],
                          "Sin respuesta real del paciente, el follow-up debe seguir enviándose")


if __name__ == "__main__":
    unittest.main()
