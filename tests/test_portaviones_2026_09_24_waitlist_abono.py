"""Regresión: portaviones 2026-09-24 #6 — lista de espera de especialidades
con abono OBLIGATORIO (Gastroenterología $35.000, Psiquiatría $60.000) sin
mencionar el abono.

Caso real José 56945067340: "Gastroenterologia" sin disponibilidad → el bot
lo auto-inscribió en la lista de espera (perfil conocido, feature deliberada
del 2026-05-03, NO se toca) con un mensaje que NO menciona que, cuando se
libere el cupo, hay que pagar $35.000 por adelantado para confirmar. El
paciente se entera recién al intentar reservar — cuando el cupo ya está
tomado por la conversación en curso.

Fix: `_iniciar_agendar` calcula la nota de abono desde
`config.abono_regla()` (misma fuente que el gate real de agendamiento, no
un monto inventado) y la agrega a los 3 textos de "sin disponibilidad"
(auto-inscripción con/sin mensaje personalizado, y la pregunta explícita
WAIT_WAITLIST_CONFIRM). Cardiología y otras especialidades SIN abono
obligatorio no llevan la nota — se verifica que no se les agregue de más.

Ejecución:
    PYTHONPATH=app:. venv/bin/python3 tests/test_portaviones_2026_09_24_waitlist_abono.py
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))


class TestWaitlistAbonoNota(unittest.IsolatedAsyncioTestCase):

    async def test_gastro_auto_inscripcion_menciona_abono(self):
        """Caso José 56945067340: perfil conocido → auto-inscripción, pero
        AHORA con la nota del abono de $35.000."""
        from flows import _iniciar_agendar
        with (
            patch("flows.is_medilink_down", return_value=False),
            patch("flows.buscar_primer_dia", return_value=([], [])),
            patch("flows.get_profile",
                  return_value={"rut": "17912923-k", "nombre": "José Pacheco"}),
            patch("flows.add_to_waitlist", return_value=99),
            patch("flows.save_tag"),
            patch("flows.log_event"),
            patch("flows.reset_session"),
            patch("flows.save_session"),
        ):
            result = await _iniciar_agendar("56945067340", {}, "gastroenterología")
        self.assertIn("35.000", result, "Debe mencionar el monto real del abono")
        self.assertIn("abono", result.lower())
        self.assertIn("lista de espera", result.lower())

    async def test_psiquiatria_auto_inscripcion_menciona_abono(self):
        from flows import _iniciar_agendar
        with (
            patch("flows.is_medilink_down", return_value=False),
            patch("flows.buscar_primer_dia", return_value=([], [])),
            patch("flows.get_profile",
                  return_value={"rut": "17912923-k", "nombre": "José Pacheco"}),
            patch("flows.add_to_waitlist", return_value=100),
            patch("flows.save_tag"),
            patch("flows.log_event"),
            patch("flows.reset_session"),
            patch("flows.save_session"),
        ):
            result = await _iniciar_agendar("56945067340", {}, "psiquiatría")
        self.assertIn("60.000", result, "Debe mencionar el monto real del abono")
        self.assertIn("abono", result.lower())

    async def test_gastro_pregunta_explicita_menciona_abono(self):
        """Paciente SIN perfil conocido → cae al flujo de confirmación
        explícita (WAIT_WAITLIST_CONFIRM); ese texto también debe avisar."""
        from flows import _iniciar_agendar
        with (
            patch("flows.is_medilink_down", return_value=False),
            patch("flows.buscar_primer_dia", return_value=([], [])),
            patch("flows.get_profile", return_value=None),
            patch("flows.save_session"),
        ):
            result = await _iniciar_agendar("56999998888", {}, "gastroenterología")
        # _btn_msg devuelve un dict con la interactive; el body vive ahí.
        texto = result if isinstance(result, str) else result["interactive"]["body"]["text"]
        self.assertIn("35.000", texto)
        self.assertIn("abono", texto.lower())

    async def test_cardiologia_sin_abono_no_agrega_nota_falsa(self):
        """Control: Cardiología NO tiene abono obligatorio — no debe
        aparecer texto de abono inventado."""
        from flows import _iniciar_agendar
        with (
            patch("flows.is_medilink_down", return_value=False),
            patch("flows.buscar_primer_dia", return_value=([], [])),
            patch("flows.get_profile",
                  return_value={"rut": "17912923-k", "nombre": "Alfredo Mora"}),
            patch("flows.add_to_waitlist", return_value=101),
            patch("flows.save_tag"),
            patch("flows.log_event"),
            patch("flows.reset_session"),
            patch("flows.save_session"),
        ):
            result = await _iniciar_agendar("56993871727", {}, "cardiología")
        self.assertNotIn("abono", result.lower())


if __name__ == "__main__":
    unittest.main()
