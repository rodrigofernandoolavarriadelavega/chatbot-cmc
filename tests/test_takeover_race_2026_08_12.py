"""Tests de regresión — takeover silencioso 2026-08-12 (3 casos reales).

Caso A: `/admin/api/reply` guardaba SIEMPRE state="HUMAN_TAKEOVER", ignorando
        la excepción de estados transaccionales (el comentario decía "NO
        cambiamos el estado" pero el save de abajo lo pisaba igual).
Caso B: consecuencia directa de A — paciente 56973898136 quedó HUMAN_TAKEOVER
        en pleno WAIT_MODALIDAD porque recepción respondió justo cuando el
        bot acababa de guardar ese estado.
Caso C: red de seguridad — recepción inactiva no debe silenciar mensajes del
        paciente sin dejar rastro; a los 30 min con hora apartada, retomar.

Uso:
  PYTHONPATH=app:. python tests/test_takeover_race_2026_08_12.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
TMP_DB = Path(tempfile.mkdtemp()) / "test_takeover_race.db"
os.environ["SESSIONS_DB"] = str(TMP_DB)
os.environ.setdefault("SQLCIPHER_KEY", "")


def _make_mock_medilink():
    import medilink as _ml
    _ml.buscar_primer_dia = AsyncMock(return_value=None)
    _ml.buscar_slots_dia = AsyncMock(return_value=[])
    _ml.buscar_slots_dia_por_ids = AsyncMock(return_value=[])
    _ml.buscar_paciente = AsyncMock(return_value={
        "id": 100, "nombre": "Paciente Prueba", "rut": "11111111-1",
    })
    _ml.buscar_paciente_por_nombre = AsyncMock(return_value=None)
    _ml.crear_paciente = AsyncMock(return_value={"id": 999})
    _ml.crear_cita = AsyncMock(return_value={"id": 5555})
    _ml.listar_citas_paciente = AsyncMock(return_value=[])
    _ml.cancelar_cita = AsyncMock(return_value=True)
    _ml.obtener_agenda_dia = AsyncMock(return_value=[])
    _ml.consultar_proxima_fecha = AsyncMock(return_value=None)
    _ml.verificar_slot_disponible = AsyncMock(return_value=True)


def _make_mock_messaging():
    import messaging as _msg
    _msg.send_whatsapp = AsyncMock(return_value="wamid.TEST")
    _msg.send_instagram = AsyncMock(return_value=True)
    _msg.send_messenger = AsyncMock(return_value=True)
    _msg.react_whatsapp = AsyncMock(return_value=True)
    _msg.unreact_whatsapp = AsyncMock(return_value=True)


class _FakeRequest:
    """Reemplazo mínimo de fastapi.Request — solo necesitamos .json()."""

    def __init__(self, payload: dict):
        self._payload = payload

    async def json(self):
        return self._payload


async def _setup():
    _make_mock_medilink()
    _make_mock_messaging()

    import session as _s
    _s.DB_PATH = TMP_DB

    import flows as _f
    import admin_routes as _ar
    # admin_routes importa send_whatsapp/send_instagram/send_messenger a nivel
    # de módulo — parchear ahí también, no solo en messaging.
    import messaging as _msg
    _ar.send_whatsapp = _msg.send_whatsapp
    _ar.send_instagram = _msg.send_instagram
    _ar.send_messenger = _msg.send_messenger
    return _f, _ar


class TestCasoA_ReplyRespetaEstadoTransaccional(unittest.IsolatedAsyncioTestCase):
    """Caso A: /admin/api/reply NO debe pisar un estado transaccional."""

    async def asyncSetUp(self):
        self.flows, self.admin = await _setup()
        import session as _s
        _s.DB_PATH = TMP_DB

    def _phone(self, suffix: str) -> str:
        return f"5698887{suffix}"

    async def test_reply_en_wait_modalidad_no_cambia_estado(self):
        """(a) — regresión directa del bug hardcodeado."""
        from session import save_session, get_session
        phone = self._phone("001")
        save_session(phone, "WAIT_MODALIDAD", {
            "slot_elegido": {"especialidad": "Medicina General",
                              "profesional": "Dr. Andrés Abarca",
                              "fecha": "2027-06-20", "fecha_display": "vie 20 jun",
                              "hora_inicio": "10:00", "hora_fin": "10:15"},
            "rut_conocido": "11111111-1",
        })

        req = _FakeRequest({"phone": phone, "message": "El doctor no atiende esta semana"})
        result = await self.admin.admin_reply(req, "test-token")
        self.assertTrue(result.get("ok"))

        sess = get_session(phone)
        self.assertEqual(sess.get("state"), "WAIT_MODALIDAD",
            f"El reply de recepción pisó un estado transaccional: {sess.get('state')}")
        # El slot elegido no debe perderse.
        self.assertIn("slot_elegido", sess.get("data", {}))

    async def test_reply_en_idle_si_hace_takeover(self):
        """(b) — comportamiento intencional intacto: fuera de un estado
        transaccional, el reply de recepción SÍ fuerza HUMAN_TAKEOVER."""
        from session import save_session, get_session
        phone = self._phone("002")
        save_session(phone, "IDLE", {})

        req = _FakeRequest({"phone": phone, "message": "Hola, en qué te ayudo"})
        result = await self.admin.admin_reply(req, "test-token")
        self.assertTrue(result.get("ok"))

        sess = get_session(phone)
        self.assertEqual(sess.get("state"), "HUMAN_TAKEOVER")
        self.assertTrue(sess.get("data", {}).get("human_replied"))


class TestCasoC_RedDeSeguridad(unittest.IsolatedAsyncioTestCase):
    """Caso C: recepción inactiva no debe silenciar sin dejar rastro."""

    async def asyncSetUp(self):
        self.flows, self.admin = await _setup()
        import session as _s
        _s.DB_PATH = TMP_DB

    def _phone(self, suffix: str) -> str:
        return f"5698886{suffix}"

    async def _msg(self, phone: str, txt: str) -> str:
        from session import get_session
        sess = get_session(phone)
        return await self.flows.handle_message(phone, txt, sess)

    def _log_recepcionista_respondio(self, phone: str, mins_ago: float):
        """Inserta el evento con un ts explícito en el pasado (simula que
        recepción respondió hace `mins_ago` minutos)."""
        from session import db
        ts = (datetime.now(timezone.utc) - timedelta(minutes=mins_ago)).strftime("%Y-%m-%d %H:%M:%S")
        with db() as conn:
            conn.execute(
                "INSERT INTO conversation_events (phone, event, meta, ts) VALUES (?, 'recepcionista_respondio', '{}', ?)",
                (phone, ts),
            )

    async def test_ack_unico_con_recepcion_inactiva(self):
        """(c) — recepción inactiva ≥15 min: UN solo ack "recepción ocupada",
        no repetido en el siguiente mensaje."""
        from session import save_session
        phone = self._phone("101")
        entrada = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
        save_session(phone, "HUMAN_TAKEOVER", {
            "hold_sent": True, "handoff_reason": "manual",
            "takeover_entered_ts": entrada,
            "msgs_sin_respuesta": 3, "human_replied": False,
        })

        resp1 = await self._msg(phone, "sigo esperando")
        self.assertIn("recepción ocupada", resp1.lower(),
            f"No mandó el ack de recepción inactiva: {resp1!r}")

        resp2 = await self._msg(phone, "hola?")
        self.assertNotIn("recepción ocupada", (resp2 or "").lower(),
            f"Repitió el ack de recepción ocupada en el 2º mensaje: {resp2!r}")

    async def test_no_ack_con_recepcion_activa_reciente(self):
        """(e) — recepción respondió hace poco (<15 min): no dispara el ack
        ni la retoma, aunque haya pasado el umbral desde que entró al takeover."""
        from session import save_session
        phone = self._phone("102")
        entrada = (datetime.now(timezone.utc) - timedelta(minutes=40)).isoformat()
        save_session(phone, "HUMAN_TAKEOVER", {
            "hold_sent": True, "handoff_reason": "manual",
            "takeover_entered_ts": entrada,
            "slot_elegido": {"especialidad": "Medicina General",
                              "profesional": "Dr. Andrés Abarca",
                              "fecha": "2027-06-20", "fecha_display": "vie 20 jun",
                              "hora_inicio": "10:00", "hora_fin": "10:15"},
            "rut_conocido": "11111111-1",
            "msgs_sin_respuesta": 1, "human_replied": True,
        })
        self._log_recepcionista_respondio(phone, mins_ago=5)

        resp = await self._msg(phone, "aló?")
        low = (resp or "").lower()
        self.assertNotIn("recepción ocupada", low)
        self.assertNotIn("seguimos donde quedamos", low)

        from session import get_session
        self.assertEqual(get_session(phone).get("state"), "HUMAN_TAKEOVER",
            "No debió retomar el flujo con recepción activa hace poco")

    async def test_retoma_a_los_30min_con_slot_apartado_sin_cita(self):
        """(d) — 30+ min sin recepción y hay hora apartada sin cita creada
        → retoma el flujo transaccional."""
        self.flows.listar_citas_paciente = AsyncMock(return_value=[])  # sin citas creadas

        from session import save_session, get_session
        phone = self._phone("103")
        entrada = (datetime.now(timezone.utc) - timedelta(minutes=40)).isoformat()
        save_session(phone, "HUMAN_TAKEOVER", {
            "hold_sent": True, "handoff_reason": "manual",
            "takeover_entered_ts": entrada,
            "slot_elegido": {"especialidad": "Medicina General",
                              "profesional": "Dr. Andrés Abarca",
                              "fecha": "2027-06-20", "fecha_display": "vie 20 jun",
                              "hora_inicio": "10:00", "hora_fin": "10:15"},
            "rut_conocido": "11111111-1",
            "msgs_sin_respuesta": 2, "human_replied": False,
        })

        resp = await self._msg(phone, "hola, seguís ahí?")
        low = (resp["interactive"]["body"]["text"].lower()
               if isinstance(resp, dict) else (resp or "").lower())
        self.assertIn("seguimos donde quedamos", low, f"No retomó: {resp!r}")

        sess = get_session(phone)
        self.assertEqual(sess.get("state"), "WAIT_SLOT")

    async def test_no_retoma_si_recepcion_ya_agendo_por_otra_via(self):
        """(d, negativo) — si Medilink ya muestra una cita de la misma
        especialidad, NO retoma (evita duplicar)."""
        self.flows.listar_citas_paciente = AsyncMock(return_value=[{
            "id": 900, "especialidad": "Medicina General",
            "fecha": "2027-06-21", "hora_inicio": "09:00",
        }])

        from session import save_session, get_session
        phone = self._phone("104")
        entrada = (datetime.now(timezone.utc) - timedelta(minutes=45)).isoformat()
        save_session(phone, "HUMAN_TAKEOVER", {
            "hold_sent": True, "handoff_reason": "manual",
            "takeover_entered_ts": entrada,
            "slot_elegido": {"especialidad": "Medicina General",
                              "profesional": "Dr. Andrés Abarca",
                              "fecha": "2027-06-20", "fecha_display": "vie 20 jun",
                              "hora_inicio": "10:00", "hora_fin": "10:15"},
            "rut_conocido": "11111111-1",
            "msgs_sin_respuesta": 2, "human_replied": False,
        })

        resp = await self._msg(phone, "hola, seguís ahí?")
        low = (resp["interactive"]["body"]["text"].lower()
               if isinstance(resp, dict) else (resp or "").lower())
        self.assertNotIn("seguimos donde quedamos", low,
            f"Retomó pese a que ya había una cita creada por otra vía: {resp!r}")

        sess = get_session(phone)
        self.assertEqual(sess.get("state"), "HUMAN_TAKEOVER")


if __name__ == "__main__":
    asyncio.run(asyncio.sleep(0))  # warm up event loop
    unittest.main(verbosity=2)
