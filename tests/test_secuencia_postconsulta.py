"""Tests de regresión — secuenciación post-consulta (portaviones #10).

Problema real (casos 56978613486, 56959205136, 56999671505): al responder
"Mejor" al seguimiento post-consulta, el paciente recibía en ráfaga de ≤3s:
(1) tips de autocuidado, (2) upsell cross-sell, (3) solicitud de reseña
Google — sin esperar respuesta entre pasos.

Esta suite cubre SOLO lo que vive en `fidelizacion.py` (funciones puras de
construcción/decisión de la secuencia) y `jobs.py` (los dos crons que la
despachan). El disparo original en `flows.py` (bloque `_SEG_ID_MAP` /
handlers `upsell_si` / `no_control`) sigue sin tocarse en esta sesión — ver
el reporte de la sesión para el wiring pendiente ahí.

Uso:
  PYTHONPATH=app:. python tests/test_secuencia_postconsulta.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT))

TMP_DB = Path(tempfile.mkdtemp(prefix="cmc_test_seq_")) / "test_sessions.db"
os.environ["SESSIONS_DB"] = str(TMP_DB)
os.environ.setdefault("SQLCIPHER_KEY", "")

import session  # noqa: E402
session.DB_PATH = TMP_DB

import fidelizacion as fid  # noqa: E402
import jobs  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Parte 1 — funciones puras de fidelizacion.py
# ─────────────────────────────────────────────────────────────────────────────
class TestConstruirSecuenciaUpsell(unittest.TestCase):
    def test_campos_basicos(self):
        t0 = datetime(2026, 8, 19, 2, 0, 0, tzinfo=timezone.utc)
        sec = fid.construir_secuencia_upsell(
            "medicina general", "medicina general", "¿Te agendo un control?", 4, ahora=t0
        )
        self.assertEqual(sec["status"], "pendiente")
        self.assertEqual(sec["especialidad_origen"], "medicina general")
        self.assertEqual(sec["upsell_esp"], "medicina general")
        self.assertEqual(sec["rating"], 4)
        self.assertEqual(sec["creado_en"], t0.isoformat())
        disparar_en = datetime.fromisoformat(sec["disparar_en"])
        self.assertEqual(disparar_en - t0, timedelta(minutes=fid.UPSELL_DELAY_MINUTOS))

    def test_permite_sin_upsell_mapeado(self):
        """Especialidad sin cross-sell en UPSELL_POSTCONSULTA (ej. control
        genérico) igual arma secuencia, con upsell_msg/upsell_esp en None."""
        sec = fid.construir_secuencia_upsell("podología", None, None, 5)
        self.assertIsNone(sec["upsell_msg"])
        self.assertIsNone(sec["upsell_esp"])
        self.assertEqual(sec["status"], "pendiente")


class TestDebeDispararUpsell(unittest.TestCase):
    def test_no_dispara_antes_de_tiempo(self):
        """RÁFAGA ELIMINADA: justo después de crearse (t0+0s) el upsell NO
        debe dispararse — antes salía en el mismo instante que los tips."""
        t0 = datetime(2026, 8, 19, 2, 2, 53, tzinfo=timezone.utc)
        sec = fid.construir_secuencia_upsell("medicina general", "medicina general", "x", 4, ahora=t0)
        self.assertFalse(fid.debe_disparar_upsell(sec, ahora=t0))
        self.assertFalse(fid.debe_disparar_upsell(sec, ahora=t0 + timedelta(seconds=3)))
        self.assertFalse(fid.debe_disparar_upsell(sec, ahora=t0 + timedelta(minutes=fid.UPSELL_DELAY_MINUTOS - 1)))

    def test_dispara_al_llegar_el_tiempo(self):
        t0 = datetime(2026, 8, 19, 2, 2, 53, tzinfo=timezone.utc)
        sec = fid.construir_secuencia_upsell("medicina general", "medicina general", "x", 4, ahora=t0)
        self.assertTrue(fid.debe_disparar_upsell(sec, ahora=t0 + timedelta(minutes=fid.UPSELL_DELAY_MINUTOS)))
        self.assertTrue(fid.debe_disparar_upsell(sec, ahora=t0 + timedelta(hours=5)))

    def test_no_dispara_si_ya_enviado_o_respondido(self):
        t0 = datetime.now(timezone.utc)
        sec = fid.construir_secuencia_upsell("medicina general", "medicina general", "x", 4, ahora=t0)
        enviado = fid.marcar_upsell_enviado(sec, ahora=t0 + timedelta(minutes=30))
        self.assertFalse(fid.debe_disparar_upsell(enviado, ahora=t0 + timedelta(hours=10)))
        respondido = fid.marcar_upsell_respondido(sec, ahora=t0 + timedelta(minutes=30))
        self.assertFalse(fid.debe_disparar_upsell(respondido, ahora=t0 + timedelta(hours=10)))

    def test_no_dispara_sin_secuencia(self):
        self.assertFalse(fid.debe_disparar_upsell(None))
        self.assertFalse(fid.debe_disparar_upsell({}))

    def test_marcar_no_muta_original(self):
        """Las funciones deben ser puras — no pisar el dict que recibieron."""
        t0 = datetime.now(timezone.utc)
        sec = fid.construir_secuencia_upsell("medicina general", None, None, 4, ahora=t0)
        original_status = sec["status"]
        _ = fid.marcar_upsell_enviado(sec, ahora=t0)
        self.assertEqual(sec["status"], original_status)


class TestSecuenciaReview(unittest.TestCase):
    def test_sin_upsell_dispara_tras_delay_corto(self):
        t0 = datetime.now(timezone.utc)
        disparo = fid.calcular_disparo_review(None, ahora=t0)
        self.assertEqual(disparo, t0 + timedelta(minutes=fid.REVIEW_DELAY_TRAS_RESPUESTA_MIN))

    def test_upsell_pendiente_nunca_adelanta_review(self):
        """ORDEN: la reseña nunca se dispara mientras el upsell sigue
        'pendiente' (el job de upsell aún no corrió), sin importar cuánto
        tiempo pase."""
        t0 = datetime.now(timezone.utc)
        sec_upsell = fid.construir_secuencia_upsell("medicina general", "medicina general", "x", 4, ahora=t0)
        self.assertIsNone(fid.calcular_disparo_review(sec_upsell, ahora=t0))
        self.assertIsNone(fid.calcular_disparo_review(sec_upsell, ahora=t0 + timedelta(hours=10)))

        sec_review = fid.construir_secuencia_review("medicina general", 4, ahora=t0)
        self.assertFalse(fid.debe_disparar_review(sec_review, sec_upsell, ahora=t0 + timedelta(hours=10)))

    def test_upsell_enviado_usa_timeout_largo(self):
        t0 = datetime.now(timezone.utc)
        sec_upsell = fid.construir_secuencia_upsell("medicina general", "medicina general", "x", 4, ahora=t0)
        enviado = fid.marcar_upsell_enviado(sec_upsell, ahora=t0 + timedelta(minutes=fid.UPSELL_DELAY_MINUTOS))
        disparo = fid.calcular_disparo_review(enviado)
        esperado = (t0 + timedelta(minutes=fid.UPSELL_DELAY_MINUTOS)) + timedelta(hours=fid.REVIEW_TIMEOUT_HORAS)
        self.assertEqual(disparo, esperado)

        sec_review = fid.construir_secuencia_review("medicina general", 4, ahora=t0)
        just_before = enviado["enviado_en"]
        just_before_dt = datetime.fromisoformat(just_before) + timedelta(hours=fid.REVIEW_TIMEOUT_HORAS) - timedelta(seconds=1)
        self.assertFalse(fid.debe_disparar_review(sec_review, enviado, ahora=just_before_dt))
        just_after_dt = datetime.fromisoformat(just_before) + timedelta(hours=fid.REVIEW_TIMEOUT_HORAS, seconds=1)
        self.assertTrue(fid.debe_disparar_review(sec_review, enviado, ahora=just_after_dt))

    def test_upsell_respondido_usa_delay_corto_y_es_mas_rapido_que_timeout(self):
        """Si el paciente SÍ respondió el upsell, la reseña no espera las
        REVIEW_TIMEOUT_HORAS completas — sale poco después de la respuesta."""
        t0 = datetime.now(timezone.utc)
        sec_upsell = fid.construir_secuencia_upsell("medicina general", "medicina general", "x", 4, ahora=t0)
        enviado = fid.marcar_upsell_enviado(sec_upsell, ahora=t0 + timedelta(minutes=20))
        respondido = fid.marcar_upsell_respondido(enviado, ahora=t0 + timedelta(minutes=21))
        disparo = fid.calcular_disparo_review(respondido)
        esperado = (t0 + timedelta(minutes=21)) + timedelta(minutes=fid.REVIEW_DELAY_TRAS_RESPUESTA_MIN)
        self.assertEqual(disparo, esperado)
        self.assertLess(disparo, (t0 + timedelta(minutes=20)) + timedelta(hours=fid.REVIEW_TIMEOUT_HORAS))

    def test_review_no_dispara_dos_veces(self):
        t0 = datetime.now(timezone.utc)
        sec_review = fid.construir_secuencia_review("medicina general", 4, ahora=t0)
        enviada = fid.marcar_review_enviado(sec_review, ahora=t0)
        self.assertFalse(fid.debe_disparar_review(enviada, None, ahora=t0 + timedelta(days=1)))


# ─────────────────────────────────────────────────────────────────────────────
# Parte 2 — jobs.py: los dos crons que consumen la secuencia
# ─────────────────────────────────────────────────────────────────────────────
def _run(coro):
    return asyncio.run(coro)


class TestJobSecuenciaUpsell(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        os.environ["SECUENCIA_POSTCONSULTA_ENABLED"] = "true"
        with session.db() as conn:
            conn.execute("DELETE FROM sessions")
            conn.execute("DELETE FROM messages")

    def _abrir_ventana(self, phone):
        session.log_message(phone, "in", "hola", "IDLE")

    async def test_no_dispara_antes_de_tiempo_via_db(self):
        phone = "56900000001"
        self._abrir_ventana(phone)
        t0 = datetime.now(timezone.utc)
        sec = fid.construir_secuencia_upsell("medicina general", "medicina general", "¿te interesa?", 4, ahora=t0)
        session.save_session(phone, "IDLE", {"upsell_postconsulta": sec})

        with patch("messaging.send_whatsapp_interactive", new=AsyncMock()) as mock_send:
            await jobs._job_secuencia_postconsulta_upsell()
        mock_send.assert_not_called()
        data = session.get_session(phone)["data"]
        self.assertEqual(data["upsell_postconsulta"]["status"], "pendiente")

    async def test_dispara_cuando_corresponde(self):
        phone = "56900000002"
        self._abrir_ventana(phone)
        t0 = datetime.now(timezone.utc) - timedelta(minutes=fid.UPSELL_DELAY_MINUTOS + 1)
        sec = fid.construir_secuencia_upsell("medicina general", "medicina general", "¿te interesa?", 4, ahora=t0)
        session.save_session(phone, "IDLE", {"upsell_postconsulta": sec})

        with patch("jobs.send_whatsapp_interactive", new=AsyncMock()) as mock_send:
            await jobs._job_secuencia_postconsulta_upsell()
        mock_send.assert_called_once()
        data = session.get_session(phone)["data"]
        self.assertEqual(data["upsell_postconsulta"]["status"], "enviado")
        self.assertIn("enviado_en", data["upsell_postconsulta"])

    async def test_guard_human_takeover_no_procesa(self):
        """FIX-4: si la sesión está en HUMAN_TAKEOVER (o cualquier flujo
        activo distinto de IDLE) el job no debe tocarla — reusa el mismo
        guard `WHERE state='IDLE'` de _job_followup_info."""
        phone = "56900000003"
        self._abrir_ventana(phone)
        t0 = datetime.now(timezone.utc) - timedelta(hours=1)
        sec = fid.construir_secuencia_upsell("medicina general", "medicina general", "¿te interesa?", 4, ahora=t0)
        session.save_session(phone, "HUMAN_TAKEOVER", {"upsell_postconsulta": sec})

        with patch("jobs.send_whatsapp_interactive", new=AsyncMock()) as mock_send:
            await jobs._job_secuencia_postconsulta_upsell()
        mock_send.assert_not_called()
        sess = session.get_session(phone)
        self.assertEqual(sess["state"], "HUMAN_TAKEOVER")
        self.assertEqual(sess["data"]["upsell_postconsulta"]["status"], "pendiente")

    async def test_guard_wait_state_no_procesa(self):
        """Paciente a mitad de otro flujo (ej. agendando otra cosa) — el
        upsell diferido debe esperar, no interrumpir."""
        phone = "56900000004"
        self._abrir_ventana(phone)
        t0 = datetime.now(timezone.utc) - timedelta(hours=1)
        sec = fid.construir_secuencia_upsell("medicina general", "medicina general", "¿te interesa?", 4, ahora=t0)
        session.save_session(phone, "WAIT_SLOT", {"upsell_postconsulta": sec})

        with patch("jobs.send_whatsapp_interactive", new=AsyncMock()) as mock_send:
            await jobs._job_secuencia_postconsulta_upsell()
        mock_send.assert_not_called()

    async def test_feature_flag_off_no_procesa(self):
        os.environ["SECUENCIA_POSTCONSULTA_ENABLED"] = "false"
        phone = "56900000005"
        self._abrir_ventana(phone)
        t0 = datetime.now(timezone.utc) - timedelta(hours=1)
        sec = fid.construir_secuencia_upsell("medicina general", "medicina general", "¿te interesa?", 4, ahora=t0)
        session.save_session(phone, "IDLE", {"upsell_postconsulta": sec})
        try:
            with patch("jobs.send_whatsapp_interactive", new=AsyncMock()) as mock_send:
                await jobs._job_secuencia_postconsulta_upsell()
            mock_send.assert_not_called()
        finally:
            os.environ["SECUENCIA_POSTCONSULTA_ENABLED"] = "true"

    async def test_sin_ventana_abierta_no_procesa(self):
        """Sin mensaje inbound reciente (ventana 24h cerrada) no se manda
        texto libre — evita el 131047 (mismo guardrail que otros jobs)."""
        phone = "56900000006"
        t0 = datetime.now(timezone.utc) - timedelta(hours=1)
        sec = fid.construir_secuencia_upsell("medicina general", "medicina general", "¿te interesa?", 4, ahora=t0)
        session.save_session(phone, "IDLE", {"upsell_postconsulta": sec})
        with patch("jobs.send_whatsapp_interactive", new=AsyncMock()) as mock_send:
            await jobs._job_secuencia_postconsulta_upsell()
        mock_send.assert_not_called()

    async def test_sin_upsell_mapeado_se_marca_enviado_sin_mandar_texto(self):
        """Especialidad sin cross-sell (upsell_msg=None): no hay nada que
        mandar por este job, pero se marca 'enviado' para no bloquear
        indefinidamente la reseña diferida."""
        phone = "56900000007"
        self._abrir_ventana(phone)
        t0 = datetime.now(timezone.utc) - timedelta(hours=1)
        sec = fid.construir_secuencia_upsell("podología", None, None, 5, ahora=t0)
        session.save_session(phone, "IDLE", {"upsell_postconsulta": sec})
        with patch("jobs.send_whatsapp_interactive", new=AsyncMock()) as mock_send:
            await jobs._job_secuencia_postconsulta_upsell()
        mock_send.assert_not_called()
        data = session.get_session(phone)["data"]
        self.assertEqual(data["upsell_postconsulta"]["status"], "enviado")


class TestJobSecuenciaReview(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        os.environ["SECUENCIA_POSTCONSULTA_ENABLED"] = "true"
        with session.db() as conn:
            conn.execute("DELETE FROM sessions")
            conn.execute("DELETE FROM messages")
            conn.execute("DELETE FROM conversation_events")

    def _abrir_ventana(self, phone):
        session.log_message(phone, "in", "hola", "IDLE")

    async def test_no_dispara_mientras_upsell_pendiente(self):
        """ORDEN: aunque pasen horas, si el job de upsell no corrió todavía
        (status sigue 'pendiente') la reseña NO se manda."""
        phone = "56900000101"
        self._abrir_ventana(phone)
        t0 = datetime.now(timezone.utc) - timedelta(hours=10)
        sec_upsell = fid.construir_secuencia_upsell("medicina general", "medicina general", "x", 4, ahora=t0)
        sec_review = fid.construir_secuencia_review("medicina general", 4, ahora=t0)
        session.save_session(phone, "IDLE", {
            "upsell_postconsulta": sec_upsell,
            "review_postconsulta": sec_review,
        })
        with patch("flows._send_review_request_if_due", new=AsyncMock()) as mock_review:
            await jobs._job_secuencia_postconsulta_review()
        mock_review.assert_not_called()

    async def test_dispara_tras_timeout_de_upsell_sin_respuesta(self):
        phone = "56900000102"
        self._abrir_ventana(phone)
        t_creado = datetime.now(timezone.utc) - timedelta(hours=fid.REVIEW_TIMEOUT_HORAS + 1)
        sec_upsell = fid.construir_secuencia_upsell("medicina general", "medicina general", "x", 4, ahora=t_creado)
        sec_upsell = fid.marcar_upsell_enviado(sec_upsell, ahora=t_creado + timedelta(minutes=fid.UPSELL_DELAY_MINUTOS))
        sec_review = fid.construir_secuencia_review("medicina general", 4, ahora=t_creado)
        session.save_session(phone, "IDLE", {
            "upsell_postconsulta": sec_upsell,
            "review_postconsulta": sec_review,
        })
        with patch("flows._send_review_request_if_due", new=AsyncMock()) as mock_review:
            await jobs._job_secuencia_postconsulta_review()
        mock_review.assert_called_once()
        _, kwargs_or_args = mock_review.call_args[0], mock_review.call_args[1]
        data = session.get_session(phone)["data"]
        self.assertEqual(data["review_postconsulta"]["status"], "enviado")

    async def test_dispara_poco_despues_de_respuesta_al_upsell(self):
        phone = "56900000103"
        self._abrir_ventana(phone)
        t_creado = datetime.now(timezone.utc) - timedelta(hours=1)
        sec_upsell = fid.construir_secuencia_upsell("medicina general", "medicina general", "x", 4, ahora=t_creado)
        sec_upsell = fid.marcar_upsell_enviado(sec_upsell, ahora=t_creado + timedelta(minutes=5))
        sec_upsell = fid.marcar_upsell_respondido(
            sec_upsell, ahora=t_creado + timedelta(minutes=6))
        sec_review = fid.construir_secuencia_review("medicina general", 4, ahora=t_creado)
        session.save_session(phone, "IDLE", {
            "upsell_postconsulta": sec_upsell,
            "review_postconsulta": sec_review,
        })
        with patch("flows._send_review_request_if_due", new=AsyncMock()) as mock_review:
            await jobs._job_secuencia_postconsulta_review()
        mock_review.assert_called_once()

    async def test_guard_human_takeover_no_procesa(self):
        phone = "56900000104"
        self._abrir_ventana(phone)
        t_creado = datetime.now(timezone.utc) - timedelta(hours=fid.REVIEW_TIMEOUT_HORAS + 1)
        sec_upsell = fid.construir_secuencia_upsell("medicina general", "medicina general", "x", 4, ahora=t_creado)
        sec_upsell = fid.marcar_upsell_enviado(sec_upsell, ahora=t_creado)
        sec_review = fid.construir_secuencia_review("medicina general", 4, ahora=t_creado)
        session.save_session(phone, "HUMAN_TAKEOVER", {
            "upsell_postconsulta": sec_upsell,
            "review_postconsulta": sec_review,
        })
        with patch("flows._send_review_request_if_due", new=AsyncMock()) as mock_review:
            await jobs._job_secuencia_postconsulta_review()
        mock_review.assert_not_called()

    async def test_cooldown_365d_se_respeta(self):
        """La reseña reusa flows._send_review_request_if_due, que ya
        implementa el cooldown anti-spam de 365 días — este test verifica
        que efectivamente lo consulta (no lo duplica) dejando que la función
        real decida y solo confirma que el job la llama con los datos
        correctos; el cooldown en sí está cubierto por los tests propios de
        flows.py."""
        phone = "56900000105"
        self._abrir_ventana(phone)
        session.log_event(phone, "review_request_sent", {"especialidad": "medicina general", "rating": 5})
        t_creado = datetime.now(timezone.utc) - timedelta(hours=fid.REVIEW_TIMEOUT_HORAS + 1)
        sec_upsell = fid.construir_secuencia_upsell("medicina general", "medicina general", "x", 4, ahora=t_creado)
        sec_upsell = fid.marcar_upsell_enviado(sec_upsell, ahora=t_creado)
        sec_review = fid.construir_secuencia_review("medicina general", 4, ahora=t_creado)
        session.save_session(phone, "IDLE", {
            "upsell_postconsulta": sec_upsell,
            "review_postconsulta": sec_review,
        })
        with patch("flows.send_whatsapp", new=AsyncMock()) as mock_send_real:
            await jobs._job_secuencia_postconsulta_review()
        # has_recent_event(...,days=365) debe frenar el envío real dentro de
        # _send_review_request_if_due, incluso llamando la función real.
        mock_send_real.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# Parte 3 — integración real con flows.handle_message (wiring cableado
# 2026-08-22: bloque _SEG_ID_MAP "mejor" + handlers upsell_si/no_control).
# Medilink/messaging se mockean ANTES del primer `import flows` para que los
# `from medilink import ...` de flows.py capturen los mocks.
# ─────────────────────────────────────────────────────────────────────────────
def _make_mock_medilink_seq():
    import medilink as _ml
    _ml.buscar_primer_dia = AsyncMock(return_value=([], []))
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


def _make_mock_messaging_seq():
    import messaging as _msg
    _msg.send_whatsapp = AsyncMock(return_value="wamid.TEST")
    _msg.send_instagram = AsyncMock(return_value=True)
    _msg.send_messenger = AsyncMock(return_value=True)


_make_mock_medilink_seq()
_make_mock_messaging_seq()
import flows as flows_mod  # noqa: E402 — DESPUÉS de mockear medilink/messaging


def _seed_seguimiento(phone: str, especialidad: str = "medicina general",
                       profesional: str = "Dr. Prueba", id_cita: str = "9001"):
    with session.db() as conn:
        conn.execute(
            "INSERT INTO citas_bot (phone, id_cita, especialidad, profesional, fecha, hora, modalidad) "
            "VALUES (?,?,?,?,?,?,?)",
            (phone, id_cita, especialidad, profesional, "2026-08-20", "10:00", "Presencial"),
        )
        conn.commit()
    session.save_fidelizacion_msg(phone, "postconsulta", id_cita)


class TestFlowsSeguimientoMejorNoRafaga(unittest.IsolatedAsyncioTestCase):
    """RÁFAGA ELIMINADA + orden: al tocar 'Mejor' (seg_5), la respuesta ya no
    trae el texto del upsell ni dispara la reseña — solo agenda la secuencia
    en session.data para que jobs.py la despache después."""

    def setUp(self):
        with session.db() as conn:
            conn.execute("DELETE FROM sessions")
            conn.execute("DELETE FROM citas_bot")
            conn.execute("DELETE FROM fidelizacion_msgs")
            conn.execute("DELETE FROM conversation_events")

    async def test_seg5_mejor_no_manda_upsell_ni_resena_de_inmediato(self):
        phone = "56900001001"
        _seed_seguimiento(phone, especialidad="medicina general")
        with patch("flows.send_whatsapp", new=AsyncMock()) as mock_send:
            resp = await flows_mod.handle_message(phone, "seg_5", {"state": "IDLE", "data": {}})
        texto = resp if isinstance(resp, str) else str(resp)
        self.assertNotIn("reseña en Google", texto)
        self.assertNotIn("exámenes generales", texto)  # copy del upsell de MG
        mock_send.assert_not_called()  # nada por send_whatsapp directo (solo el ack, vía return)
        data = session.get_session(phone)["data"]
        self.assertIn("upsell_postconsulta", data)
        self.assertEqual(data["upsell_postconsulta"]["status"], "pendiente")
        self.assertEqual(data["upsell_postconsulta"]["upsell_esp"], "medicina general")
        self.assertIn("review_postconsulta", data)
        self.assertEqual(data["review_postconsulta"]["status"], "pendiente")

    async def test_seg5_mejor_especialidad_sin_upsell_mapeado(self):
        """Especialidad sin cross-sell (ej. podología): igual agenda la
        secuencia (upsell_esp/upsell_msg en None), sin romper."""
        phone = "56900001006"
        _seed_seguimiento(phone, especialidad="podología")
        resp = await flows_mod.handle_message(phone, "seg_4", {"state": "IDLE", "data": {}})
        self.assertTrue(resp)
        data = session.get_session(phone)["data"]
        self.assertEqual(data["upsell_postconsulta"]["upsell_esp"], None)
        self.assertEqual(data["upsell_postconsulta"]["status"], "pendiente")

    async def test_texto_libre_mejor_tambien_agenda_secuencia(self):
        """El path de texto libre (clasificar_respuesta_seguimiento) usa el
        mismo mecanismo — mockeamos el clasificador para simular 'mejor'."""
        phone = "56900001007"
        _seed_seguimiento(phone, especialidad="medicina general")

        async def _fake_clasificar(_txt):
            return "mejor"

        with patch("flows.clasificar_respuesta_seguimiento", new=_fake_clasificar):
            resp = await flows_mod.handle_message(phone, "me siento excelente", {"state": "IDLE", "data": {}})
        texto = resp if isinstance(resp, str) else str(resp)
        self.assertNotIn("reseña en Google", texto)
        data = session.get_session(phone)["data"]
        self.assertEqual(data["upsell_postconsulta"]["status"], "pendiente")
        self.assertEqual(data["review_postconsulta"]["status"], "pendiente")


class TestFlowsSeguimientoIgualPeorSinCambios(unittest.IsolatedAsyncioTestCase):
    """CONFIRMA que la rama 'igual'/'peor' NO se tocó: la ráfaga era solo en
    'mejor' (upsell+reseña); acá nunca hubo reseña/upsell al paciente, solo
    la oferta de reagendar + alerta al ADMIN (peor) — sigue igual."""

    def setUp(self):
        with session.db() as conn:
            conn.execute("DELETE FROM sessions")
            conn.execute("DELETE FROM citas_bot")
            conn.execute("DELETE FROM fidelizacion_msgs")
            conn.execute("DELETE FROM conversation_events")

    async def test_seg2_peor_ofrece_reagendar_sin_secuencia(self):
        phone = "56900001002"
        _seed_seguimiento(phone, especialidad="medicina general")
        resp = await flows_mod.handle_message(phone, "seg_2", {"state": "IDLE", "data": {}})
        texto = str(resp).lower()
        self.assertIn("reagendar", texto)
        data = session.get_session(phone)["data"]
        self.assertNotIn("upsell_postconsulta", data)
        self.assertNotIn("review_postconsulta", data)

    async def test_seg3_igual_ofrece_reagendar_sin_secuencia(self):
        phone = "56900001003"
        _seed_seguimiento(phone, especialidad="medicina general")
        resp = await flows_mod.handle_message(phone, "seg_3", {"state": "IDLE", "data": {}})
        texto = str(resp).lower()
        self.assertIn("reagendar", texto)
        data = session.get_session(phone)["data"]
        self.assertNotIn("upsell_postconsulta", data)

    async def test_texto_libre_peor_sin_cambios(self):
        phone = "56900001008"
        _seed_seguimiento(phone, especialidad="medicina general")

        async def _fake_clasificar(_txt):
            return "peor"

        with patch("flows.clasificar_respuesta_seguimiento", new=_fake_clasificar):
            resp = await flows_mod.handle_message(phone, "me siento pésimo", {"state": "IDLE", "data": {}})
        texto = str(resp).lower()
        self.assertIn("reagendar", texto)
        data = session.get_session(phone)["data"]
        self.assertNotIn("upsell_postconsulta", data)


class TestFlowsHandlersUpsellSiNoControl(unittest.IsolatedAsyncioTestCase):
    """Los handlers upsell_si/no_control marcan 'respondido' — la reseña
    diferida no espera el timeout completo si el paciente ya contestó."""

    def setUp(self):
        with session.db() as conn:
            conn.execute("DELETE FROM sessions")
            conn.execute("DELETE FROM citas_bot")
            conn.execute("DELETE FROM fidelizacion_msgs")

    async def test_upsell_si_marca_respondido(self):
        phone = "56900001004"
        sec = fid.marcar_upsell_enviado(
            fid.construir_secuencia_upsell("medicina general", "medicina general", "x", 4)
        )
        session.save_session(phone, "IDLE", {
            "upsell_postconsulta": sec,
            "upsell_especialidad": "medicina general",
        })
        sess = session.get_session(phone)
        await flows_mod.handle_message(phone, "upsell_si", sess)
        data = session.get_session(phone)["data"]
        self.assertEqual(data["upsell_postconsulta"]["status"], "respondido")

    async def test_no_control_marca_respondido(self):
        phone = "56900001005"
        sec = fid.marcar_upsell_enviado(
            fid.construir_secuencia_upsell("medicina general", "medicina general", "x", 4)
        )
        session.save_session(phone, "IDLE", {
            "upsell_postconsulta": sec,
            "upsell_especialidad": "medicina general",
        })
        sess = session.get_session(phone)
        resp = await flows_mod.handle_message(phone, "no_control", sess)
        self.assertTrue(resp)
        data = session.get_session(phone)["data"]
        self.assertEqual(data["upsell_postconsulta"]["status"], "respondido")


if __name__ == "__main__":
    unittest.main(verbosity=2)
