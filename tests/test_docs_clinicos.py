"""Tests de app/docs_clinicos.py — dx desde documentos, demanda externa,
conciliación de nombres OCR.

Uso:
  pytest tests/test_docs_clinicos.py -v
  python tests/test_docs_clinicos.py
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

os.environ.setdefault("SQLCIPHER_KEY", "")

from docs_clinicos import (detectar_dx_tags, clasificar_examen_externo)  # noqa: E402


class TestDxDesdeDocumentos(unittest.TestCase):
    def test_receta_metformina(self):
        # El caso del transcript real (pregunta de la metformina)
        self.assertIn("dm2", detectar_dx_tags("Metformina 850 mg cada 12 horas"))

    def test_receta_losartan(self):
        self.assertIn("hta", detectar_dx_tags("Losartán 50 mg 1 al día"))

    def test_dx_orden_nes(self):
        # Receta Clínica NES del corpus: "Sospecha de HPB, Nefropatía diabética"
        tags = detectar_dx_tags("Sospecha de HPB. Nefropatia Diabetica")
        self.assertIn("hpb", tags)
        self.assertIn("dm2", tags)  # "diabetica"
        self.assertIn("irc", tags)  # "nefropatia"

    def test_chat_sigue_funcionando(self):
        # Los keywords conversacionales originales no se perdieron
        self.assertIn("hta", detectar_dx_tags("tengo la presion alta"))
        self.assertIn("asma", detectar_dx_tags("uso inhalador"))

    def test_texto_sano_sin_tags(self):
        self.assertEqual(detectar_dx_tags("quiero hora con kinesiologo"), [])


class TestDemandaExterna(unittest.TestCase):
    def test_radiografia(self):
        # Caso real del corpus (Hosp. Arauco)
        self.assertEqual(clasificar_examen_externo("Radiografía de tórax PA y lateral"),
                         "radiografia")

    def test_escaner(self):
        self.assertEqual(clasificar_examen_externo("TAC de abdomen y pelvis"),
                         "escaner_tac")

    def test_laboratorio(self):
        self.assertEqual(clasificar_examen_externo("Hemograma completo + TSH"),
                         "laboratorio")

    def test_eco_no_es_externa(self):
        self.assertIsNone(clasificar_examen_externo("Ecografía abdominal"))

    def test_kine_no_es_externa(self):
        self.assertIsNone(clasificar_examen_externo("Kinesiterapia motora 10 sesiones"))


class TestSimilitudNombres(unittest.TestCase):
    """La función pública requiere DB; acá probamos el scoring interno vía
    el módulo (mismo algoritmo que usa nombre_mas_probable)."""

    def test_lectura_imperfecta_matchea(self):
        # Caso real 2026-08-01: visión leyó "Anyie Ruby" y el número
        # pertenece a "Anguie Rondoy Yovera"
        import docs_clinicos as dc
        from difflib import SequenceMatcher

        def sim(a, b):
            ta = [t for t in dc._norm(a).split() if len(t) > 2]
            tb = [t for t in dc._norm(b).split() if len(t) > 2]
            scores = [max(SequenceMatcher(None, x, y).ratio() for y in tb)
                      for x in ta]
            return sum(scores) / len(scores)

        self.assertGreaterEqual(sim("Anyie Ruby", "Anguie Rondoy Yovera"), 0.55)
        # Un nombre completamente distinto NO matchea (tercero real)
        self.assertLess(sim("Pedro Soto Fuentes", "Anguie Rondoy Yovera"), 0.55)


if __name__ == "__main__":
    unittest.main(verbosity=2)
