"""Tests del vocabulario ampliado + capa fuzzy de ecografías (2026-08-01).

Cada caso de "corpus" es una respuesta REAL de paciente en producción
(60 días de sessions.db) que ANTES no matcheaba y dejaba al bot re-preguntando
el tipo (213 ecografia_sin_tipo vs 126 ecografia_tipo_matched).

Uso:
  pytest tests/test_eco_vocab_fuzzy.py -v
  python tests/test_eco_vocab_fuzzy.py
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

os.environ.setdefault("SQLCIPHER_KEY", "")

from ecografias import route_ecografia  # noqa: E402


class TestCorpusReal(unittest.TestCase):
    """Frases reales de producción que fallaban antes del fix."""

    # (texto real, assume_context, id_profesional esperado o flujo)
    def _assert_prof(self, texto, esperado, assume=True):
        r = route_ecografia(texto, assume_context=assume)
        self.assertIsNotNone(r, f"{texto!r} debería matchear")
        self.assertEqual(r["id_profesional"], esperado, f"{texto!r} → {r}")

    def _assert_no_disponible(self, texto, assume=True):
        r = route_ecografia(texto, assume_context=assume)
        self.assertIsNotNone(r, f"{texto!r} debería matchear")
        self.assertEqual(r["flujo"], "no_disponible", f"{texto!r} → {r}")

    # ── Ginecología (Rejón, 61) ──────────────────────────────────────────
    def test_ginecologa_sola(self):
        self._assert_prof("Ginecóloga", 61)

    def test_ginecologia_typo_tilde(self):
        self._assert_prof("Ginecológia", 61)

    def test_ginecologica_sola(self):
        self._assert_prof("Ginecológica", 61)

    def test_pelvica_sola(self):
        self._assert_prof("pélvica", 61)

    def test_ecotomagrias_pelvica(self):
        # "Ecotomagrias" (typo) aporta el contexto eco vía gate ecotom\w*
        self._assert_prof("Ecotomagrias pélvica femenina para niña de 5 años", 61,
                          assume=False)

    # ── Obstétrica → no disponible ───────────────────────────────────────
    def test_obstretica_typo(self):
        self._assert_no_disponible("Ecografia obstretica")

    def test_ginecologa_embarazo_prioriza_obstetrica(self):
        # "embarazo" gana por prioridad: la eco de embarazo NO se realiza
        self._assert_no_disponible("Ginecóloga embarazo")

    # ── Ecografía general (Pardo, 68) ────────────────────────────────────
    def test_unguinal_typo(self):
        self._assert_prof("Ecografia unguinal bilateral", 68, assume=False)

    def test_dopler_typo(self):
        self._assert_prof("Dopler", 68)

    def test_orden_medica_us_doppler_renal(self):
        self._assert_prof("Us Eco tomografía Doppler Renal", 68, assume=False)

    def test_lumbrosaca(self):
        self._assert_prof("Necesito una ecografía Lumbrosaca / Partes Blandas", 68,
                          assume=False)

    def test_lumbo_sacr_cortado(self):
        self._assert_prof("Realizan ecografía partes blandas lumbo sacr?", 68,
                          assume=False)

    def test_muscoesqueletica_typo(self):
        self._assert_prof("Muscoesqueletica de pie derecho", 68)

    # ── Fuzzy puro (typos no diccionarizados) ────────────────────────────
    def test_fuzzy_avdominal(self):
        self._assert_prof("eco avdominal", 68, assume=False)

    def test_fuzzy_tiroydes(self):
        self._assert_prof("ecografia tiroydes", 68, assume=False)

    def test_fuzzy_mamarea(self):
        self._assert_prof("eco mamarea", 68, assume=False)


class TestSinFalsosPositivos(unittest.TestCase):
    """Lo nuevo NO debe secuestrar textos que no son de eco."""

    def test_hora_ginecologia_sin_eco(self):
        # Sin raíz ecográfica el gate bloquea: es una hora normal de gine
        self.assertIsNone(route_ecografia("quiero hora con ginecología"))

    def test_dolor_rodilla_sin_eco(self):
        self.assertIsNone(route_ecografia("me duele la rodilla"))

    def test_con_fonasa_no_es_tipo(self):
        # Pregunta lateral en wait_eco_tipo: no debe matchear ningún grupo
        self.assertIsNone(route_ecografia("Con fonasa", assume_context=True))

    def test_por_favor_no_es_tipo(self):
        self.assertIsNone(route_ecografia("Por favor", assume_context=True))

    def test_valor_no_es_tipo(self):
        self.assertIsNone(route_ecografia("Que valor tiene", assume_context=True))

    def test_ecografia_sola_sigue_preguntando(self):
        self.assertIsNone(route_ecografia("ecografía"))
        self.assertIsNone(route_ecografia("Ecografias"))


class TestRegresionRoutingExistente(unittest.TestCase):
    """El routing histórico no cambia."""

    def test_transvaginal_rejon(self):
        r = route_ecografia("eco transvaginal")
        self.assertEqual(r["id_profesional"], 61)

    def test_mamaria_pardo(self):
        r = route_ecografia("eco mamaria")
        self.assertEqual(r["id_profesional"], 68)

    def test_abdominal_pardo(self):
        r = route_ecografia("ecografia abdominal")
        self.assertEqual(r["id_profesional"], 68)

    def test_ecocardiograma_millan(self):
        r = route_ecografia("ecocardiograma")
        self.assertEqual(r["id_profesional"], 60)
        self.assertEqual(r["flujo"], "waitlist")

    def test_embarazo_no_disponible(self):
        r = route_ecografia("eco de embarazo")
        self.assertEqual(r["flujo"], "no_disponible")


if __name__ == "__main__":
    unittest.main(verbosity=2)
