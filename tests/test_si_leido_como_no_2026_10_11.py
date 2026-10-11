"""WAIT_SLOT: afirmaciones reales que el bot registraba como rechazo
(`funnel_slot_rechazado`, paso `slot_no_elegido`).

Medido 2026-10-11 sobre todos los rechazos de prod desde 10-jun: 62 eran un
"sí" claro; ~la mitad terminó sin cita. Tras los fixes del 24/25-sep seguían
fallando estas variantes: "Sii", "Siporfavor", "Oki"/"Oka"/"Okey",
"De todas maneras", "Por favor, agradecida" y el botón `quick_yes` de un
mensaje anterior llegando con horas en pantalla.

Ejecución:
    PYTHONPATH=app:. venv/bin/python3 tests/test_si_leido_como_no_2026_10_11.py
"""
import os
import sys
import unicodedata
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import flows  # noqa: E402


def _norm(s):
    s = unicodedata.normalize("NFD", s.lower())
    return "".join(c for c in s if unicodedata.category(c) != "Mn").strip()


def _ok(txt):
    return flows._afirma_slot(txt.lower().strip(), _norm(txt))


class TestSiLeidoComoNo(unittest.TestCase):

    def test_variantes_reales_confirman(self):
        for t in ("Sii", "Siii", "Sii ahendo", "Siporfavor", "Oki", "Oka",
                  "Oka gracias", "Okey gracias", "De todas maneras",
                  "Por favor, agradecida", "por favor muchas gracias"):
            self.assertTrue(_ok(t), t)

    def test_lo_que_ya_funcionaba_sigue(self):
        for t in ("Si por favor", "Por favor", "Ya si", "ok", "Sí, por favor"):
            self.assertTrue(_ok(t), t)

    def test_si_con_otra_cosa_no_confirma(self):
        for t in ("Ya para el 27", "Si para otro paciente", "Sii pero mejor el martes",
                  "Oki pero otro día", "Okey a las 5", "Siii mañana", "oka el lunes",
                  "Por favor envíeme el horario", "por favor otro dia",
                  "De todas maneras no puedo", "no", "nada"):
            self.assertFalse(_ok(t), t)


class TestQuickYesEnWaitSlot(unittest.IsolatedAsyncioTestCase):

    async def test_quick_yes_con_horas_en_pantalla_reserva_la_sugerida(self):
        slot = {"fecha": "2026-10-13", "hora_inicio": "09:15", "hora_fin": "09:30",
                "id_profesional": 73, "profesional": "Dr. Andrés Abarca",
                "especialidad": "Medicina General", "fecha_display": "martes 13"}
        data = {"especialidad": "medicina general", "slots": [slot],
                "todos_slots": [slot], "slot_sugerido": dict(slot),
                "fechas_vistas": ["2026-10-13"]}
        confirmed = AsyncMock(return_value="ok")
        clasificador = AsyncMock(return_value={"action": "continue"})
        with patch("flows._slot_confirmed", confirmed), \
             patch("flows.classify_with_context", clasificador), \
             patch("flows.log_event") as log_ev, \
             patch("flows.save_session"):
            await flows.handle_message("56900000001", "quick_yes",
                                       {"state": "WAIT_SLOT", "data": data})
        confirmed.assert_awaited_once()
        self.assertEqual(confirmed.await_args.args[2]["hora_inicio"], "09:15")
        eventos = [c.args[1] for c in log_ev.call_args_list]
        self.assertNotIn("funnel_slot_rechazado", eventos)
        # Es un botón: no debe gastar una llamada al clasificador LLM.
        clasificador.assert_not_awaited()


class TestSiNoPasaPorClasificador(unittest.TestCase):

    def test_si_limpio_en_wait_slot_es_respuesta_obvia(self):
        for t in ("Sii", "Si por favor", "Oki", "De todas maneras", "quick_yes"):
            self.assertTrue(
                flows._es_respuesta_obvia_al_prompt(t, t.lower(), "WAIT_SLOT", {}), t)

    def test_si_con_otra_cosa_sigue_al_clasificador(self):
        for t in ("Sii pero mejor el martes", "Ya para el 27", "Si para otro paciente"):
            self.assertFalse(
                flows._es_respuesta_obvia_al_prompt(t, t.lower(), "WAIT_SLOT", {}), t)


if __name__ == "__main__":
    unittest.main(verbosity=2)
