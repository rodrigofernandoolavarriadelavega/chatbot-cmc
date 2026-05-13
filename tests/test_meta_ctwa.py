"""
Tests para la feature CTWA (Click-to-WhatsApp) Meta Ads:
- Mapping headline → especialidad
- Formato de slots (fmt_slot_ctwa)
- Handler WAIT_META_SLOT_CHOICE (unit, sin Medilink real)
"""
import sys
import os
import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# Apuntar al directorio app
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))


# ── 1. Tests de mapping headline → especialidad ───────────────────────────────
class TestHeadlineToEspecialidad(unittest.TestCase):
    def setUp(self):
        from medilink import headline_to_especialidad
        self.h2e = headline_to_especialidad

    def test_ecografia_exacta(self):
        self.assertEqual(self.h2e("Ecografía"), "ecografía")

    def test_ecografia_sin_tilde(self):
        self.assertEqual(self.h2e("Ecografia"), "ecografía")

    def test_eco_abreviado(self):
        self.assertEqual(self.h2e("Eco"), "ecografía")

    def test_ecotomografia(self):
        self.assertEqual(self.h2e("Ecotomografía"), "ecografía")

    def test_ortodoncia_exacta(self):
        self.assertEqual(self.h2e("Ortodoncia"), "ortodoncia")

    def test_orto_abreviado(self):
        self.assertEqual(self.h2e("Orto"), "ortodoncia")

    def test_frenillos(self):
        self.assertEqual(self.h2e("Frenillos"), "ortodoncia")

    def test_orl_abreviado(self):
        self.assertEqual(self.h2e("ORL"), "otorrinolaringología")

    def test_otorrino(self):
        self.assertEqual(self.h2e("Otorrino"), "otorrinolaringología")

    def test_medicina_general(self):
        self.assertEqual(self.h2e("Medicina General"), "medicina general")

    def test_medicina_general_mixedcase(self):
        self.assertEqual(self.h2e("MEDICINA GENERAL"), "medicina general")

    def test_dentista(self):
        self.assertEqual(self.h2e("Dentista"), "odontología")

    def test_kine_abreviado(self):
        self.assertEqual(self.h2e("Kine"), "kinesiología")

    def test_headline_con_substring(self):
        # "Hora de Ecografía abdominal" → ecografía por substring
        self.assertEqual(self.h2e("Hora de Ecografía abdominal"), "ecografía")

    def test_unmapped_retorna_none(self):
        self.assertIsNone(self.h2e("Reumatología"))

    def test_headline_vacio(self):
        self.assertIsNone(self.h2e(""))

    def test_headline_none(self):
        self.assertIsNone(self.h2e(None))

    def test_caso_mixto_ginecologia(self):
        self.assertEqual(self.h2e("Ginecología"), "ginecología")

    def test_caso_mixto_nutricion(self):
        self.assertEqual(self.h2e("Nutrición"), "nutrición")


# ── 2. Tests de formato de slots ──────────────────────────────────────────────
class TestFmtSlotCtwa(unittest.TestCase):
    def setUp(self):
        from medilink import fmt_slot_ctwa
        self.fmt = fmt_slot_ctwa

    def test_formato_basico(self):
        slot = {
            "fecha": "2026-05-14",
            "hora_inicio": "10:30",
            "profesional": "Dr. Andrés Abarca",
            "especialidad": "Medicina General",
        }
        resultado = self.fmt(slot)
        # Jueves 14 may · 10:30 — Dr. Andrés Abarca
        self.assertIn("Jueves", resultado)
        self.assertIn("14", resultado)
        self.assertIn("may", resultado)
        self.assertIn("10:30", resultado)
        self.assertIn("Dr. Andrés Abarca", resultado)
        self.assertIn("·", resultado)
        self.assertIn("—", resultado)

    def test_dia_semana_minuscula_primer_char_may(self):
        # El día de la semana debe ir con primera letra mayúscula
        slot = {"fecha": "2026-05-11", "hora_inicio": "09:00", "profesional": "X"}
        resultado = self.fmt(slot)
        # 2026-05-11 es lunes
        self.assertTrue(resultado.startswith("Lunes"))

    def test_mes_abreviado_tres_letras(self):
        slot = {"fecha": "2026-01-07", "hora_inicio": "08:00", "profesional": "Dr. X"}
        resultado = self.fmt(slot)
        self.assertIn("ene", resultado)

    def test_hora_formato_dos_puntos(self):
        slot = {"fecha": "2026-05-15", "hora_inicio": "16:00:00", "profesional": "Dr. X"}
        resultado = self.fmt(slot)
        self.assertIn("16:00", resultado)

    def test_fecha_invalida_no_crash(self):
        slot = {"fecha": "INVALID", "hora_inicio": "10:00", "profesional": "Dr. X",
                "fecha_display": "Lunes 11 may"}
        resultado = self.fmt(slot)
        self.assertIsInstance(resultado, str)
        self.assertIn("10:00", resultado)


# ── 3. Tests del handler WAIT_META_SLOT_CHOICE ────────────────────────────────
# handle_message(phone, texto, session) recibe el session dict ya cargado
# (el caller en main.py hace get_session antes). Lo pasamos directamente.
class TestWaitMetaSlotChoiceHandler(unittest.IsolatedAsyncioTestCase):

    def _make_slot(self, n: int) -> dict:
        return {
            "fecha": f"2026-05-{14 + n}",
            "fecha_display": f"Jueves {14 + n} may",
            "hora_inicio": f"{10 + n}:00",
            "hora_fin": f"{10 + n}:15",
            "profesional": "David Pardo",
            "especialidad": "Ecografía",
            "id_profesional": 68,
            "id_recurso": 1,
        }

    def _session(self, extra: dict = None) -> dict:
        d = {
            "meta_offered_slots": [self._make_slot(0), self._make_slot(1), self._make_slot(2)],
            "meta_esp": "ecografía",
        }
        if extra:
            d.update(extra)
        return {"state": "WAIT_META_SLOT_CHOICE", "data": d}

    async def _call(self, txt: str, session: dict = None):
        from flows import handle_message
        s = session or self._session()
        with (
            patch("flows.save_session"),
            patch("flows.log_event"),
            patch("flows.get_profile",       return_value=None),
            patch("flows.is_medilink_down",  return_value=False),
            patch("flows.has_recent_event",  return_value=False),
            patch("flows.get_tags",          return_value=[]),
            patch("flows.save_tag"),
            patch("flows.buscar_paciente",   new_callable=AsyncMock, return_value=None),
            patch("flows._slot_confirmed",   new_callable=AsyncMock,
                  return_value="SLOT_CONFIRMED_MOCK"),
            patch("flows._iniciar_agendar",  new_callable=AsyncMock,
                  return_value="INICIAR_AGENDAR_MOCK"),
        ):
            return await handle_message("56912345678", txt, s)

    async def test_elige_1(self):
        result = await self._call("1")
        self.assertEqual(result, "SLOT_CONFIRMED_MOCK")

    async def test_elige_2(self):
        result = await self._call("2")
        self.assertEqual(result, "SLOT_CONFIRMED_MOCK")

    async def test_elige_3(self):
        result = await self._call("3")
        self.assertEqual(result, "SLOT_CONFIRMED_MOCK")

    async def test_otra_fecha_lanza_iniciar_agendar(self):
        result = await self._call("otra fecha")
        self.assertEqual(result, "INICIAR_AGENDAR_MOCK")

    async def test_otro_dia_lanza_iniciar_agendar(self):
        result = await self._call("otro día")
        self.assertEqual(result, "INICIAR_AGENDAR_MOCK")

    async def test_no_gracias_regresa_a_idle(self):
        from flows import handle_message
        s = self._session()
        with (
            patch("flows.save_session") as mock_save,
            patch("flows.log_event"),
            patch("flows.get_profile",       return_value=None),
            patch("flows.is_medilink_down",  return_value=False),
            patch("flows.has_recent_event",  return_value=False),
            patch("flows.get_tags",          return_value=[]),
            patch("flows.save_tag"),
        ):
            result = await handle_message("56912345678", "no gracias", s)
            self.assertIn("menu", str(result).lower())
            mock_save.assert_called()
            states = [c[0][1] for c in mock_save.call_args_list]
            self.assertIn("IDLE", states)

    async def test_input_random_redispatch(self):
        # Texto libre → re-dispatch a IDLE (no quedarse atascado)
        from flows import handle_message
        s = self._session()
        with (
            patch("flows.save_session"),
            patch("flows.log_event"),
            patch("flows.get_profile",       return_value=None),
            patch("flows.is_medilink_down",  return_value=False),
            patch("flows.has_recent_event",  return_value=False),
            patch("flows.get_tags",          return_value=[]),
            patch("flows.save_tag"),
            # El redispatch llama handle_message con state IDLE
            # Mockear detect_intent para que no haga llamada real
            patch("flows.detect_intent",    new_callable=AsyncMock,
                  return_value={"intent": "otro"}),
            patch("flows.respuesta_faq",    new_callable=AsyncMock,
                  return_value="respuesta faq mock"),
        ):
            result = await handle_message("56912345678", "hola que tal el dia de hoy", s)
            # No debe lanzar excepción y debe retornar algo coherente
            self.assertIsNotNone(result)
            self.assertIsInstance(result, (str, dict))


if __name__ == "__main__":
    unittest.main(verbosity=2)
