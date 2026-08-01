"""Tests de app/serie_kine.py — detección de órdenes con N sesiones y helpers.

Uso:
  pytest tests/test_serie_kine.py -v
  python tests/test_serie_kine.py
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

os.environ.setdefault("SQLCIPHER_KEY", "")

from serie_kine import detectar_sesiones_kine, _parse_fecha, _display, _mins  # noqa: E402


class TestDeteccion(unittest.TestCase):
    def test_orden_tipica_10_sesiones(self):
        r = detectar_sesiones_kine(["10 sesiones kinesiterapia motora rodilla izquierda"])
        self.assertIsNotNone(r)
        self.assertEqual(r["n"], 10)

    def test_knt_abreviado(self):
        r = detectar_sesiones_kine(["KNT 8 sesiones hombro derecho"])
        self.assertIsNotNone(r)
        self.assertEqual(r["n"], 8)

    def test_fisioterapia(self):
        r = detectar_sesiones_kine(["Fisioterapia 12 sesiones lumbar"])
        self.assertEqual(r["n"], 12)

    def test_orden_mixta_encuentra_la_kine(self):
        r = detectar_sesiones_kine([
            "Radiografía de rodilla", "6 sesiones de kinesiología"])
        self.assertEqual(r["n"], 6)

    def test_eco_no_dispara(self):
        self.assertIsNone(detectar_sesiones_kine(["Ecografía de rodilla izquierda"]))

    def test_sesiones_sin_kine_no_dispara(self):
        # "10 sesiones de psicoterapia" no es serie kine
        self.assertIsNone(detectar_sesiones_kine(["10 sesiones de psicoterapia"]))

    def test_kine_sin_numero_no_dispara(self):
        self.assertIsNone(detectar_sesiones_kine(["Kinesiterapia motora rodilla"]))

    def test_una_sesion_no_es_serie(self):
        self.assertIsNone(detectar_sesiones_kine(["1 sesión kinesiterapia"]))

    def test_numero_absurdo_no_dispara(self):
        # regex toma máximo 2 dígitos y el rango 2-15 filtra
        self.assertIsNone(detectar_sesiones_kine(["99 sesiones kinesiterapia"]))


class TestHelpers(unittest.TestCase):
    def test_parse_iso(self):
        self.assertEqual(str(_parse_fecha("2026-08-03")), "2026-08-03")

    def test_parse_chileno(self):
        # Medilink a veces devuelve DD/MM/YYYY
        self.assertEqual(str(_parse_fecha("03/08/2026")), "2026-08-03")

    def test_parse_basura(self):
        self.assertIsNone(_parse_fecha("mañana"))

    def test_display(self):
        self.assertEqual(_display("2026-08-03"), "lun 03/08")

    def test_mins(self):
        self.assertEqual(_mins("09:30"), 570)
        self.assertEqual(_mins(""), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
