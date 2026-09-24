"""Regresión: portaviones 2026-09-24 #8 — "Si"/"Sí" pelado a un recordatorio
de cita, y afirmaciones coloquiales en WAIT_SLOT, caían al menú principal.

Casos reales verificados (3+ teléfonos, mismo patrón exacto):
  - 56972978206 (21-sep, recordatorio recepción 2h de Ecografía): responde
    "Si" → bot muestra el menú genérico en vez de confirmar.
  - 56961006968 (06-may) y 56974072362 (×3, 10-ago/16-sep/19-sep): mismo
    patrón — "Si" pelado ignorado; "Confirmo"/"Voy a asistir" SÍ funcionaban
    siempre (confirma que el gate en sí funciona, el hueco era solo de
    vocabulario).
  - 56910111979 (24-sep, WAIT_SLOT): "Si por favor" a un slot ofrecido →
    "No te entendí bien" en vez de reservar esa hora.

Fix 1 (recordatorio): "si"/"sí"/"sip" se agregan a
`_TOKENS_CONFIRM_RECOD_SOFT` — solo confirman si hay una cita real con
recordatorio pendiente (mismo gate ya existente); sin eso, caen al flujo
normal sin forzar "¿Qué quieres confirmar?" (igual que "sii"/"oki").

Fix 2 (WAIT_SLOT): la selección del slot ofrecido usaba
`tl in AFIRMACIONES or tl_norm in AFIRMACIONES` (match EXACTO) en vez del
helper compartido `_afirma()` (que sí tolera "sí, dale"/"si por favor" vía
prefijo + puntuación pegada, ya usado en CONFIRMING_CITA/CONFIRMING_CANCEL).

Ejecución:
    PYTHONPATH=app:. venv/bin/python3 tests/test_portaviones_2026_09_24_confirmacion_si.py
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))


class TestConfirmacionRecordatorioSiPelado(unittest.IsolatedAsyncioTestCase):

    async def test_si_pelado_confirma_cita_bot(self):
        """Caso 56961006968/56974072362: 'Si' solo, con una cita en
        citas_bot con reminder_2h_sent=1 pendiente — debe confirmar."""
        from flows import handle_message
        fila = {
            "id_cita": "999001", "especialidad": "Medicina General",
            "profesional": "Dr. Rodrigo Olavarría", "fecha": "2026-09-30",
            "hora": "18:00",
        }

        class _FakeCursor:
            def fetchone(self):
                return fila

        class _FakeConn:
            def execute(self, *a, **k):
                return _FakeCursor()
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        with (
            patch("session.db", return_value=_FakeConn()),
            patch("flows.mark_cita_confirmation"),
            patch("flows.log_event"),
        ):
            result = await handle_message("56961006968", "Si", {"state": "IDLE", "data": {}})
        self.assertIn("confirmada", result.lower())
        self.assertIn("medicina general", result.lower())

    async def test_si_pelado_confirma_cita_recepcion(self):
        """Caso real 56972978206: cita agendada por RECEPCIÓN (no vive en
        citas_bot) — el fallback get_cita_recepcion_confirmable debe
        activarse igual con 'Si' pelado."""
        from flows import handle_message

        class _EmptyCursor:
            def fetchone(self):
                return None

        class _FakeConnVacia:
            def execute(self, *a, **k):
                return _EmptyCursor()
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        fila_recep = {
            "id_cita": "888002", "especialidad": "Ecografía",
            "profesional": "David Pardo", "fecha": "2026-09-30", "hora": "12:45",
        }
        with (
            patch("session.db", return_value=_FakeConnVacia()),
            patch("flows.get_cita_recepcion_confirmable", return_value=fila_recep),
            patch("flows.mark_cita_confirmation"),
            patch("flows.log_event"),
        ):
            result = await handle_message("56972978206", "Si", {"state": "IDLE", "data": {}})
        self.assertIn("confirmada", result.lower())
        self.assertIn("ecografía", result.lower())

    async def test_si_pelado_sin_cita_pendiente_no_fuerza_pregunta(self):
        """Sin cita real pendiente, un 'Si' pelado NO debe forzar '¿Qué
        quieres confirmar?' (es soft, no hard) — debe caer al flujo normal."""
        from flows import handle_message

        class _EmptyCursor:
            def fetchone(self):
                return None

        class _FakeConnVacia:
            def execute(self, *a, **k):
                return _EmptyCursor()
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        with (
            patch("session.db", return_value=_FakeConnVacia()),
            patch("flows.get_cita_recepcion_confirmable", return_value=None),
            patch("flows.log_event"),
            patch("flows.save_session"),
        ):
            result = await handle_message("56900006666", "Si", {"state": "IDLE", "data": {}})
        self.assertNotIn("¿Qué quieres confirmar?", result)


class TestAfirmacionColoquialWaitSlot(unittest.IsolatedAsyncioTestCase):

    async def test_si_por_favor_reserva_el_slot_ofrecido(self):
        """Caso 56910111979: 'Si por favor' sobre un slot mostrado debe
        confirmarlo, no responder 'No te entendí bien'."""
        from flows import handle_message
        slot = {
            "especialidad": "Ecografía", "profesional": "David Pardo",
            "fecha": "2026-09-28", "fecha_display": "Lunes 28 de septiembre",
            "hora_inicio": "12:45", "id_profesional": 68,
        }
        data = {"slots": [slot], "todos_slots": [slot], "especialidad": "ecografía"}
        with patch("flows._slot_confirmed", return_value="slot confirmado ok") as mock_sc:
            result = await handle_message(
                "56910111979", "Si por favor", {"state": "WAIT_SLOT", "data": data}
            )
        mock_sc.assert_called_once()
        self.assertEqual(result, "slot confirmado ok")


if __name__ == "__main__":
    unittest.main()
