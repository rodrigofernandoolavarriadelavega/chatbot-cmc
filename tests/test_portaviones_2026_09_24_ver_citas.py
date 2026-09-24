"""Regresión: portaviones 2026-09-24 #7 — "Ver mis citas" enrutaba a
cancelar + "Ninguna" atrapado en WAIT_CITA_CANCELAR.

Caso real 56991471428: tras dar su RUT suelto en IDLE, el bot ofrece 3
botones "Agendar hora / Ver mis citas / Cancelar cita". El botón "Ver mis
citas" llevaba id="3" — pero el dispatcher global de atajos numéricos de
IDLE interpreta "3" como CANCELAR (mapa real: 1=agendar, 2=reagendar,
3=cancelar, 4=ver). El paciente terminó directo en WAIT_CITA_CANCELAR
mostrando SU CITA RECIÉN CREADA como candidata a anular. Al responder
"Ninguna" (quería decir "no quiero cancelar ninguna", solo estaba mirando)
quedó atrapado en "Elige un número entre 1 y 1" porque ese estado solo
reconocía menu/salir/atras como escape.

Fix 1: los ids del menú "Recibí tu RUT" se alinean con el mapa global
(Ver mis citas=4, Cancelar cita=3).
Fix 2: "ninguna"/"ninguno" se agregan al set de salida de las 4 pantallas de
selección de cita (CANCELAR, REAGENDAR y sus variantes _FAMILIAR).

Ejecución:
    PYTHONPATH=app:. venv/bin/python3 tests/test_portaviones_2026_09_24_ver_citas.py
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))


class TestVerMisCitasIdCollision(unittest.IsolatedAsyncioTestCase):

    async def test_boton_ver_mis_citas_no_usa_id_de_cancelar(self):
        """El menú post-RUT no debe reusar id="3" (=cancelar en el mapa
        global) para el botón "Ver mis citas"."""
        from flows import handle_message
        with (
            patch("flows.save_session"),
            patch("flows.log_event"),
        ):
            # RUT válido (módulo 11) — con DV incorrecto el texto no matchea
            # `valid_rut` y cae más abajo al clasificador de Claude.
            result = await handle_message(
                "56991471428", "12.345.678-5", {"state": "IDLE", "data": {}}
            )
        self.assertIsInstance(result, dict, "Debe ser un mensaje interactivo con botones")
        botones = result["interactive"]["action"]["buttons"]
        by_title = {b["reply"]["title"]: b["reply"]["id"] for b in botones}
        self.assertEqual(by_title.get("Ver mis citas"), "4",
                          "id debe coincidir con el atajo global 4=ver")
        self.assertEqual(by_title.get("Cancelar cita"), "3",
                          "id debe coincidir con el atajo global 3=cancelar")
        self.assertNotEqual(by_title.get("Ver mis citas"), by_title.get("Cancelar cita"))

    async def test_id_4_despacha_a_iniciar_ver_no_a_cancelar(self):
        """Con el id correcto, tocar 'Ver mis citas' (id=4) debe llamar a
        _iniciar_ver, nunca a _iniciar_cancelar."""
        from flows import handle_message
        with (
            patch("flows._iniciar_ver", return_value="ver ok") as mock_ver,
            patch("flows._iniciar_cancelar", return_value="cancelar ok") as mock_cancelar,
        ):
            result = await handle_message(
                "56991471428", "4",
                {"state": "IDLE", "data": {"rut_conocido": "12345678-5"}},
            )
        mock_ver.assert_called_once()
        mock_cancelar.assert_not_called()
        self.assertEqual(result, "ver ok")


class TestNingunaEscapaDeSeleccionCita(unittest.IsolatedAsyncioTestCase):

    async def _cita_dummy(self):
        return {
            "id": "12345",
            "especialidad": "Ecografía",
            "profesional": "David Pardo",
            "fecha_display": "21/09",
            "hora_inicio": "09:15",
        }

    async def test_ninguna_en_wait_cita_cancelar_no_queda_atrapado(self):
        """Caso 56991471428: 'Ninguna' con 1 sola cita en la lista debe
        declinar, NO repetir 'Elige un número entre 1 y 1'."""
        from flows import handle_message
        cita = await self._cita_dummy()
        with patch("flows.reset_session"):
            result = await handle_message(
                "56991471428", "Ninguna",
                {"state": "WAIT_CITA_CANCELAR", "data": {"citas": [cita]}},
            )
        self.assertNotIn("Elige un número", result)
        self.assertIn("no cancelamos nada", result.lower())

    async def test_ninguna_en_wait_cita_reagendar_no_queda_atrapado(self):
        from flows import handle_message
        cita = await self._cita_dummy()
        with patch("flows.reset_session"):
            result = await handle_message(
                "56900005555", "ninguno",
                {"state": "WAIT_CITA_REAGENDAR", "data": {"citas": [cita]}},
            )
        self.assertNotIn("Elige un número", result)


if __name__ == "__main__":
    unittest.main()
