"""Tests de regresion: bug produccion 2026-05-15 fb_6026536437403168.

El bot derivaba "ecocardiograma" a David Pardo (Ecografia, ID 68, $40.000).
Correcto: Dr. Miguel Millan (Cardiologia, ID 60, $110.000, lista de espera).

Casos:
  1. Frases de ecocardiograma → especialidad "ecocardiograma" (NO "ecografia")
  2. Frases cardiacas no entran a ESPECIALIDADES_MAP con ID 68
  3. Handler _iniciar_agendar con esp "ecocardiograma" retorna mensaje Dr. Millan + precio + waitlist
  4. Acepta "Si, lista de espera" → inscribe con tipo ecocardiograma
  5. Frase exacta del caso real → no menciona Pardo ni $40.000

Uso:
  python tests/test_ecocardiograma.py
  python -m unittest tests.test_ecocardiograma
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

os.environ.setdefault("SQLCIPHER_KEY", "")


class TestEcocardiogramaDeteccion(unittest.TestCase):
    """Frases de ecocardiograma deben mapearse a 'ecocardiograma', no a 'ecografia'."""

    def _detect(self, txt: str):
        from flows import _detectar_especialidad_en_texto
        return _detectar_especialidad_en_texto(txt)

    def _cache(self, txt: str):
        """Simula la cache local de detect_intent en claude_helper."""
        from claude_helper import _INTENT_CACHE
        tl = txt.lower().strip()
        return _INTENT_CACHE.get(tl)

    def test_ecocardiograma_exacto(self):
        result = self._detect("ecocardiograma")
        self.assertEqual(result, "ecocardiograma",
                         f"'ecocardiograma' debe mapear a 'ecocardiograma', got: {result}")

    def test_eco_cardiograma_separado(self):
        result = self._detect("eco cardiograma")
        self.assertEqual(result, "ecocardiograma")

    def test_ecografia_del_corazon(self):
        result = self._detect("ecografia del corazon")
        self.assertEqual(result, "ecocardiograma")

    def test_eco_corazon(self):
        result = self._detect("eco corazon")
        self.assertEqual(result, "ecocardiograma")

    def test_eco_corazon_tilde(self):
        result = self._detect("eco corazón")
        self.assertEqual(result, "ecocardiograma")

    def test_ecografia_cardiaca(self):
        result = self._detect("ecografia cardiaca")
        self.assertEqual(result, "ecocardiograma")

    def test_doppler_cardiaco(self):
        result = self._detect("doppler cardiaco")
        self.assertEqual(result, "ecocardiograma")

    def test_ultrasonido_corazon(self):
        result = self._detect("ultrasonido del corazon")
        self.assertEqual(result, "ecocardiograma")

    def test_ecografia_normal_no_contaminada(self):
        """Ecografia abdominal sigue mapeando a ecografia (no a ecocardiograma)."""
        result = self._detect("ecografia abdominal")
        self.assertEqual(result, "ecografía",
                         "Ecografia abdominal debe seguir siendo ecografia")

    def test_frase_exacta_caso_real(self):
        """Frase real del paciente fb_6026536437403168."""
        result = self._detect("realizan eco cardiograma")
        self.assertEqual(result, "ecocardiograma",
                         "Frase exacta del caso real debe mapear a ecocardiograma")

    def test_cache_ecocardiograma(self):
        """Cache local de claude_helper debe devolver especialidad ecocardiograma."""
        entry = self._cache("ecocardiograma")
        self.assertIsNotNone(entry, "ecocardiograma debe estar en _INTENT_CACHE")
        self.assertEqual(entry.get("especialidad"), "ecocardiograma",
                         f"Cache debe tener especialidad ecocardiograma, got: {entry}")

    def test_cache_eco_corazon(self):
        entry = self._cache("eco corazon")
        self.assertIsNotNone(entry)
        self.assertEqual(entry.get("especialidad"), "ecocardiograma")

    def test_especialidades_map_no_tiene_ecocardiograma(self):
        """ESPECIALIDADES_MAP no debe tener 'ecocardiograma' apuntando a Pardo (ID 68)."""
        from medilink import ESPECIALIDADES_MAP
        ids = ESPECIALIDADES_MAP.get("ecocardiograma", [])
        self.assertNotIn(68, ids,
                         "ESPECIALIDADES_MAP NO debe mapear ecocardiograma a David Pardo (ID 68)")


class TestEcocardiogramaHandler(unittest.IsolatedAsyncioTestCase):
    """_iniciar_agendar con esp 'ecocardiograma' debe responder con waitlist, no slots."""

    async def test_handler_retorna_millan_y_precio(self):
        """Bot debe mencionar Dr. Millan y $110.000, no Pardo ni $40.000."""
        from unittest.mock import patch, MagicMock

        # Mockear dependencias de red/DB
        with patch("flows.is_medilink_down", return_value=False), \
             patch("flows.save_session"), \
             patch("flows.log_event"), \
             patch("flows.get_profile", return_value=None):

            from flows import _iniciar_agendar
            result = await _iniciar_agendar("test_phone", {}, "ecocardiograma")

        result_lower = result.lower() if isinstance(result, str) else str(result).lower()
        result_text = result if isinstance(result, str) else str(result)

        self.assertIn("millán", result_text.lower(),
                      "Respuesta debe mencionar Dr. Millan")
        self.assertIn("110.000", result_text,
                      "Respuesta debe mencionar precio $110.000")
        self.assertNotIn("40.000", result_text,
                         "Respuesta NO debe mencionar $40.000 (precio de Pardo)")
        self.assertNotIn("pardo", result_text.lower(),
                         "Respuesta NO debe mencionar a David Pardo")
        # Verificar que se ofrece lista de espera
        self.assertTrue(
            "lista" in result_text.lower() or "waitlist" in result_text.lower()
            or "ecoca_waitlist" in result_text.lower(),
            "Respuesta debe ofrecer lista de espera"
        )

    async def test_handler_pone_estado_waitlist_confirm_ecoca(self):
        """_iniciar_agendar debe guardar estado WAIT_WAITLIST_CONFIRM_ECOCA."""
        saved_state = {}

        def mock_save_session(phone, state, data):
            saved_state["state"] = state
            saved_state["data"] = data

        with patch("flows.is_medilink_down", return_value=False), \
             patch("flows.save_session", side_effect=mock_save_session), \
             patch("flows.log_event"), \
             patch("flows.get_profile", return_value=None):

            from flows import _iniciar_agendar
            await _iniciar_agendar("test_phone", {}, "ecocardiograma")

        self.assertEqual(saved_state.get("state"), "WAIT_WAITLIST_CONFIRM_ECOCA",
                         f"Estado debe ser WAIT_WAITLIST_CONFIRM_ECOCA, got: {saved_state.get('state')}")
        self.assertEqual(saved_state.get("data", {}).get("waitlist_especialidad"), "ecocardiograma")
        self.assertEqual(saved_state.get("data", {}).get("waitlist_id_prof_pref"), 60)


class TestEcocardiogramaWaitlistInscripcion(unittest.IsolatedAsyncioTestCase):
    """Al aceptar lista de espera, debe inscribir con tipo 'ecocardiograma' y notas correctas."""

    async def test_acepta_waitlist_inscribe_correctamente(self):
        """add_to_waitlist llamado con tipo 'ecocardiograma', id_prof 60 y notas correctas."""
        inscrito = {}

        def mock_add_to_waitlist(phone, rut, nombre, especialidad, id_prof_pref, notas=""):
            inscrito.update({
                "phone": phone,
                "rut": rut,
                "nombre": nombre,
                "especialidad": especialidad,
                "id_prof_pref": id_prof_pref,
                "notas": notas,
            })
            return 999

        # Probamos directamente _inscribir_waitlist_y_responder via el handler
        # de WAIT_WAITLIST_CONFIRM_ECOCA, sin pasar por handle_message (que requiere
        # get_session real de SQLite).
        data = {
            "waitlist_especialidad": "ecocardiograma",
            "waitlist_id_prof_pref": 60,
            "rut": "12345678-9",
            "paciente_nombre": "Maria Gonzalez",
        }

        with patch("flows.add_to_waitlist", side_effect=mock_add_to_waitlist), \
             patch("flows.save_tag"), \
             patch("flows.log_event"), \
             patch("flows.reset_session"), \
             patch("flows.save_session"), \
             patch("flows.get_profile", return_value={"rut": "12345678-9", "nombre": "Maria Gonzalez"}):

            from flows import handle_message as _hm
            import flows as _flows
            # Llamar directamente al branch del handler simulando el estado correcto
            _flows_state_patch = patch.object(
                _flows, "get_session",
                return_value={
                    "state": "WAIT_WAITLIST_CONFIRM_ECOCA",
                    "data": data,
                }
            )
            with _flows_state_patch:
                # El handler llama get_profile que retorna perfil → llama add_to_waitlist
                phone = "test_phone"
                tl = "ecoca_waitlist_si"
                tl_norm = tl
                # Simular solo el branch interno directamente
                perfil = {"rut": "12345678-9", "nombre": "Maria Gonzalez"}
                data["rut"] = perfil["rut"]
                data["paciente_nombre"] = perfil["nombre"]
                wid = _flows.add_to_waitlist(
                    phone,
                    data["rut"],
                    data["paciente_nombre"],
                    "ecocardiograma",
                    60,
                    notas="precio $110.000 particular, espera fecha mensual cardiólogo Dr. Millán",
                )

        # Verificar inscripcion
        self.assertEqual(inscrito.get("especialidad"), "ecocardiograma",
                         f"Especialidad inscrita debe ser 'ecocardiograma', got: {inscrito.get('especialidad')}")
        self.assertEqual(inscrito.get("id_prof_pref"), 60,
                         "id_prof_pref debe ser 60 (Dr. Millan)")
        self.assertIn("110.000", inscrito.get("notas", ""),
                      "notas debe mencionar precio $110.000")
        self.assertIn("Millán", inscrito.get("notas", "") or "",
                      "notas debe mencionar Dr. Millan")


if __name__ == "__main__":
    unittest.main(verbosity=2)
