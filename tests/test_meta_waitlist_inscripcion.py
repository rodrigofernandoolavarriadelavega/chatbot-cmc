"""
Tests F003: WAIT_META_WAITLIST (CTWA sin disponibilidad en 7 días) debe
inscribir en la TABLA waitlist (add_to_waitlist), no solo guardar un tag.
Sin red ni DB real: todo mockeado, mismo patrón que tests/test_meta_ctwa.py.
"""
import sys
import os
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))


def _session(extra: dict = None) -> dict:
    d = {"meta_waitlist_esp": "cardiología"}
    if extra:
        d.update(extra)
    return {"state": "WAIT_META_WAITLIST", "data": d}


class TestMetaWaitlistInscripcion(unittest.IsolatedAsyncioTestCase):

    async def test_acepta_con_perfil_inscribe_en_tabla(self):
        """Paciente conocido dice 'sí' → add_to_waitlist con su RUT/nombre y tag normalizado."""
        from flows import handle_message
        with (
            patch("flows.save_session"),
            patch("flows.reset_session"),
            patch("flows.log_event"),
            patch("flows.is_medilink_down", return_value=False),
            patch("flows.has_recent_event", return_value=False),
            patch("flows.get_tags", return_value=[]),
            patch("flows.save_tag") as mock_tag,
            patch("flows.get_profile",
                  return_value={"rut": "12345678-5", "nombre": "Juana Pérez"}),
            patch("flows.add_to_waitlist", return_value=77) as mock_wl,
            patch("staff_whitelist.is_staff", return_value=False),
        ):
            result = await handle_message("56912345678", "sí", _session())
        mock_wl.assert_called_once()
        args = mock_wl.call_args.args
        self.assertEqual(args[0], "56912345678")   # phone
        self.assertEqual(args[1], "12345678-5")    # rut
        self.assertEqual(args[2], "Juana Pérez")   # nombre
        self.assertEqual(args[3], "cardiología")   # especialidad
        tags = [c.args[1] for c in mock_tag.call_args_list]
        self.assertIn("waitlist-cardiología", tags)            # tag normalizado con guión
        self.assertNotIn("waitlist:cardiología", tags)         # NO el formato roto
        self.assertIn("lista de espera", result.lower())

    async def test_acepta_sin_perfil_pide_rut(self):
        """Paciente nuevo (vino de un anuncio) dice 'si' → transiciona a WAIT_WAITLIST_RUT
        con waitlist_especialidad en data (el flujo estándar termina en add_to_waitlist)."""
        from flows import handle_message
        with (
            patch("flows.save_session") as mock_save,
            patch("flows.log_event"),
            patch("flows.is_medilink_down", return_value=False),
            patch("flows.has_recent_event", return_value=False),
            patch("flows.get_tags", return_value=[]),
            patch("flows.save_tag"),
            patch("flows.get_profile", return_value=None),
            patch("flows.add_to_waitlist") as mock_wl,
            patch("staff_whitelist.is_staff", return_value=False),
        ):
            result = await handle_message("56912345678", "si", _session())
        mock_wl.assert_not_called()
        estados = [c.args[1] for c in mock_save.call_args_list]
        self.assertIn("WAIT_WAITLIST_RUT", estados)
        _, _state_arg, data_arg = mock_save.call_args.args
        self.assertEqual(data_arg.get("waitlist_especialidad"), "cardiología")
        self.assertIn("rut", result.lower())

    async def test_rechaza_va_a_idle_sin_inscribir(self):
        """'no gracias' → IDLE, sin add_to_waitlist (comportamiento previo intacto)."""
        from flows import handle_message
        with (
            patch("flows.save_session") as mock_save,
            patch("flows.log_event"),
            patch("flows.is_medilink_down", return_value=False),
            patch("flows.has_recent_event", return_value=False),
            patch("flows.get_tags", return_value=[]),
            patch("flows.save_tag"),
            patch("flows.get_profile", return_value=None),
            patch("flows.add_to_waitlist") as mock_wl,
            patch("staff_whitelist.is_staff", return_value=False),
        ):
            result = await handle_message("56912345678", "no gracias", _session())
        mock_wl.assert_not_called()
        estados = [c.args[1] for c in mock_save.call_args_list]
        self.assertIn("IDLE", estados)
        self.assertIn("sin problema", result.lower())


if __name__ == "__main__":
    unittest.main()
