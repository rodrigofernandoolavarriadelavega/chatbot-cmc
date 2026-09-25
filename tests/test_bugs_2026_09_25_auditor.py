"""
Regresión — 4 bugs reales encontrados por el auditor (Sonnet 5) el 2026-09-25,
verificados contra conversaciones y logs reales de producción.

1. "No quiero la hora" en WAIT_RUT_AGENDAR se trataba como RUT inválido y
   quedaba en loop pidiendo RUT (caso 56959521523: 13:33:21 → "Hmm, no
   reconozco ese RUT."). Fix: salir del flujo con un cierre amable (no hay
   cita creada todavía en este estado, así que no hay nada que cancelar).

2. "No podre ir" (lenguaje natural) en WAIT_CITA_REAGENDAR solo generaba
   "Elige un número entre 1 y 3" (caso 56949341431). Fix: reconocer la
   intención de cancelar y pivotear a la selección de cancelación, reusando
   la misma lista de citas ya cargada.

3a. "¿Atienden con Fonasa?" en un estado WAIT_* (pre-router `classify_with_
    context`, tag "preguntar_info") caía al bloque genérico de dirección/
    teléfono/horario sin contestar la pregunta (caso 56991531985). Fix: si
    la pregunta menciona previsión/cobertura, usar la tabla completa de
    `respuesta_faq` en vez de la ficha de ubicación.

3b. "Con fonasa" en WAIT_ABONO_COMPROBANTE (abono-gate de Psiquiatría) solo
    repetía "estoy esperando el comprobante" sin aclarar que la especialidad
    es solo particular (caso 56973790616). Fix: rama explícita que aclara la
    cobertura antes de repetir el mensaje de espera.

4. "Confirmo" a un recordatorio de una cita YA confirmada el día anterior
   (un recordatorio posterior para la MISMA cita) hacía que el bot pidiera
   el RUT como si no tuviera ninguna cita (caso 56982248297: confirmó el
   24-sep, el recordatorio 24h del 25-sep volvió a preguntar). Fix: si no
   hay cita PENDIENTE de confirmar pero sí hay una ya confirmada, reconocerlo
   en vez de tratarlo como "sin cita".

Uso:
    PYTHONPATH=app:. python tests/test_bugs_2026_09_25_auditor.py
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

TMP_DB = Path(tempfile.mkdtemp(prefix="cmc_test_bugs_20260925_")) / "test_sessions.db"
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
import claude_helper  # noqa: E402


# El pre-router universal (`_pre_router_wait`) llama a `classify_with_context`
# (Claude Haiku) en CUALQUIER estado WAIT_*/CONFIRMING_* antes de que el
# handler del estado se ejecute. Estos tests NUNCA deben golpear la API real
# de Anthropic (saldo agotado) — se mockea por defecto a "continue" (deja
# pasar al handler normal), igual que tests/harness_50.py. Los tests que
# necesitan una clasificación específica la sobreescriben puntualmente.
async def _default_classify_with_context(mensaje: str, state: str, session_data: dict):
    return {"action": "continue"}


flows_mod.classify_with_context = _default_classify_with_context

# Defensa adicional: ningún test de este archivo debe poder golpear la API
# real de Anthropic aunque algún camino inesperado llame a detect_intent o
# respuesta_faq directamente (saldo agotado — regla dura de esta tarea).
claude_helper.detect_intent = AsyncMock(
    return_value={"intent": "menu", "especialidad": None, "respuesta_directa": None}
)
claude_helper.respuesta_faq = AsyncMock(
    return_value="Para más información, comunícate con recepción 😊"
)
flows_mod.detect_intent = claude_helper.detect_intent
flows_mod.respuesta_faq = claude_helper.respuesta_faq


def _fecha_futura(dias: int) -> str:
    return (datetime.now() + timedelta(days=dias)).strftime("%Y-%m-%d")


def _clean():
    with session.db() as conn:
        conn.execute("DELETE FROM sessions")
        conn.execute("DELETE FROM citas_bot")
        conn.execute("DELETE FROM conversation_events")
        conn.commit()


class TestNoQuieroLaHoraEnRutAgendar(unittest.IsolatedAsyncioTestCase):
    """Bug 1 — caso 56959521523."""

    def setUp(self):
        _clean()

    async def test_no_quiero_la_hora_sale_del_flujo(self):
        phone = "56900000101"
        data = {"especialidad": "gastroenterología", "rut": None}
        resp = await flows_mod.handle_message(
            phone, "No quiero la hora", {"state": "WAIT_RUT_AGENDAR", "data": data}
        )
        self.assertIsInstance(resp, str)
        self.assertNotIn("no reconozco ese rut", resp.lower(), resp)
        self.assertNotIn("problema técnico", resp.lower(), resp)
        # Sale del flujo, no queda pidiendo RUT en loop.
        sess = session.get_session(phone)
        self.assertNotEqual(sess.get("state"), "WAIT_RUT_AGENDAR", sess)

    async def test_ya_no_quiero_variantes(self):
        # "ya no quiero"/"no gracias" caen en el fix nuevo (texto). "olvídalo"
        # ya tenía un rescate universal previo (_SALIR_KW, devuelve un botón
        # interactivo) — ambos caminos son válidos con tal que NINGUNO
        # devuelva el mensaje de RUT inválido.
        for i, txt in enumerate(("ya no quiero", "no gracias", "olvídalo")):
            phone = f"5690000011{i}"
            data = {"especialidad": "medicina general"}
            resp = await flows_mod.handle_message(
                phone, txt, {"state": "WAIT_RUT_AGENDAR", "data": data}
            )
            texto = resp if isinstance(resp, str) else str(resp)
            self.assertNotIn("no reconozco ese rut", texto.lower(), f"{txt!r} -> {resp!r}")

    async def test_otra_persona_sigue_funcionando(self):
        # Guardrail: el fix no debe robarle el caso a un escape ya existente.
        phone = "56900000120"
        data = {"especialidad": "medicina general"}
        resp = await flows_mod.handle_message(
            phone, "otra persona", {"state": "WAIT_RUT_AGENDAR", "data": data}
        )
        self.assertIn("rut", resp.lower())
        sess = session.get_session(phone)
        self.assertEqual(sess.get("state"), "WAIT_RUT_AGENDAR", sess)


class TestNoPodreIrEnReagendar(unittest.IsolatedAsyncioTestCase):
    """Bug 2 — caso 56949341431."""

    def setUp(self):
        _clean()

    def _citas(self):
        return [
            {"id": "111", "especialidad": "Kinesiología", "profesional": "Toloza",
             "fecha": _fecha_futura(1), "fecha_display": "Viernes 25 de septiembre",
             "hora_inicio": "20:30:00"},
            {"id": "222", "especialidad": "Kinesiología", "profesional": "Toloza",
             "fecha": _fecha_futura(2), "fecha_display": "Sábado 26 de septiembre",
             "hora_inicio": "10:40:00"},
            {"id": "333", "especialidad": "Medicina General", "profesional": "Morales",
             "fecha": _fecha_futura(4), "fecha_display": "Lunes 28 de septiembre",
             "hora_inicio": "16:20:00"},
        ]

    async def test_no_podre_ir_pivotea_a_cancelar(self):
        phone = "56900000201"
        data = {"paciente": {"nombre": "Elizabeth", "id": 1, "rut": "11111111-1"},
                "citas": self._citas(), "rut": "11111111-1"}
        resp = await flows_mod.handle_message(
            phone, "No podre ir", {"state": "WAIT_CITA_REAGENDAR", "data": data}
        )
        # No debe ser el mensaje de error de índice.
        if isinstance(resp, str):
            self.assertNotIn("elige un número entre", resp.lower(), resp)
        sess = session.get_session(phone)
        self.assertEqual(sess.get("state"), "WAIT_CITA_CANCELAR", sess)
        self.assertEqual(sess.get("data", {}).get("citas"), self._citas())

    async def test_quiero_cancelar_tambien_pivotea(self):
        phone = "56900000202"
        data = {"paciente": {"nombre": "Elizabeth"}, "citas": self._citas()}
        resp = await flows_mod.handle_message(
            phone, "quiero cancelar", {"state": "WAIT_CITA_REAGENDAR", "data": data}
        )
        sess = session.get_session(phone)
        self.assertEqual(sess.get("state"), "WAIT_CITA_CANCELAR", sess)

    async def test_seleccion_numerica_sigue_funcionando(self):
        # Guardrail: elegir por número no debe verse afectado por el fix.
        phone = "56900000210"
        data = {"paciente": {"nombre": "Elizabeth"}, "citas": self._citas()}
        resp = await flows_mod.handle_message(
            phone, "1", {"state": "WAIT_CITA_REAGENDAR", "data": data}
        )
        # Selecciona la cita 1 y avanza a buscar slots (_iniciar_agendar) —
        # no debe quedar en WAIT_CITA_CANCELAR.
        sess = session.get_session(phone)
        self.assertNotEqual(sess.get("state"), "WAIT_CITA_CANCELAR", sess)


class TestPreguntaFonasaSinResponder(unittest.IsolatedAsyncioTestCase):
    """Bug 3a — caso 56991531985 (pregunta general de Fonasa en estado WAIT_*)."""

    def setUp(self):
        _clean()
        self._orig_classify = flows_mod.classify_with_context
        self._orig_respuesta_faq = claude_helper.respuesta_faq

    def tearDown(self):
        flows_mod.classify_with_context = self._orig_classify
        claude_helper.respuesta_faq = self._orig_respuesta_faq

    async def test_atienden_con_fonasa_responde_cobertura(self):
        async def _fake_classify(mensaje, state, session_data):
            return {"action": "answer_and_continue", "intent": "preguntar_info", "args": {}}

        flows_mod.classify_with_context = _fake_classify
        claude_helper.respuesta_faq = AsyncMock(
            return_value=(
                "Sí, atendemos con Fonasa (bono MLE) en Medicina General, "
                "Kinesiología, Nutrición y Psicología. El resto es particular."
            )
        )
        phone = "56900000301"
        data = {}
        resp = await flows_mod.handle_message(
            phone, "Disculpe atienden con fonasa",
            {"state": "WAIT_META_SLOT_CHOICE", "data": data},
        )
        texto = resp if isinstance(resp, str) else str(resp)
        self.assertIn("fonasa", texto.lower(), texto)
        claude_helper.respuesta_faq.assert_awaited()


class TestFonasaEnAbonoComprobante(unittest.IsolatedAsyncioTestCase):
    """Bug 3b — caso 56973790616 (Psiquiatría, abono-gate)."""

    def setUp(self):
        _clean()

    async def test_con_fonasa_aclara_solo_particular(self):
        phone = "56900000401"
        data = {
            "abono_gate_slot": {"especialidad": "Psiquiatría", "id_profesional": 78},
            "abono_gate_ts": datetime.now().isoformat(),
        }
        resp = await flows_mod.handle_message(
            phone, "Con fonasa", {"state": "WAIT_ABONO_COMPROBANTE", "data": data}
        )
        self.assertIsInstance(resp, str)
        self.assertIn("particular", resp.lower(), resp)
        self.assertNotIn("estoy esperando el comprobante", resp.lower(), resp)


class TestConfirmoCitaYaConfirmada(unittest.IsolatedAsyncioTestCase):
    """Bug 4 — caso 56982248297."""

    def setUp(self):
        _clean()

    async def test_confirmo_cita_ya_confirmada_no_pide_rut(self):
        phone = "56900000501"
        with session.db() as conn:
            conn.execute(
                "INSERT INTO citas_bot (phone, id_cita, especialidad, profesional, "
                "fecha, hora, modalidad, reminder_sent, confirmation_status) "
                "VALUES (?,?,?,?,?,?,?,1,?)",
                (phone, "9999", "Kinesiología", "Leonardo Etcheverry",
                 _fecha_futura(1), "11:00", "Fonasa", "confirmed"),
            )
            conn.commit()
        resp = await flows_mod.handle_message(
            phone, "Confirmo", {"state": "IDLE", "data": {}}
        )
        self.assertIsInstance(resp, str)
        self.assertNotIn("¿qué quieres confirmar?", resp.lower(), resp)
        self.assertIn("confirmad", resp.lower(), resp)


if __name__ == "__main__":
    unittest.main(verbosity=2)


# ── Guardas agregadas al integrar (casos límite) ─────────────────────────────
def test_guardas_rut_y_reagendar_no_sobreinterpretan():
    """'ya no me acuerdo de mi rut' NO es rechazo de la hora, y 'no puedo ir el
    martes, quiero otro día' es reagendar, no pivot a cancelar."""
    import asyncio as _a
    from session import save_session as _ss, get_session as _gs, reset_session as _rs
    ph = "56900025099"
    _rs(ph)
    _ss(ph, "WAIT_RUT_AGENDAR", {"especialidad": "medicina general",
                                  "slot_elegido": {"fecha": "2030-01-10", "hora_inicio": "10:00"}})
    _a.run(flows_mod.handle_message(ph, "ya no me acuerdo de mi rut", _gs(ph)))
    assert _gs(ph)["state"] != "IDLE", "perdió la reserva por 'ya no … rut'"
    _rs(ph)
