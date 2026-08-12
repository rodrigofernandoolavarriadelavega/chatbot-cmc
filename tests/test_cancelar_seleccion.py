"""Selección de cita en listas de cancelar/reagendar (bug lista vieja 2026-08-01).

Caso real: tras cancelar la 1ª cita la lista se corre; un tap en la lista
VIEJA (fila "4" = 12/08) aplicado a la lista nueva apuntaba al 14/08.

Uso:  python tests/test_cancelar_seleccion.py
"""
from __future__ import annotations
import os, sys, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
os.environ.setdefault("SQLCIPHER_KEY", "")
os.environ.setdefault("MEDILINK_TOKEN", "x")
os.environ.setdefault("META_ACCESS_TOKEN", "x")
from flows import _resolver_cita_seleccionada  # noqa: E402

# Lista VIGENTE tras cancelar la del 04/08 (caso Anguie)
CITAS = [
    {"id": 111, "fecha": "2026-08-06", "hora_inicio": "09:00"},
    {"id": 112, "fecha": "2026-08-10", "hora_inicio": "09:00"},
    {"id": 113, "fecha": "2026-08-12", "hora_inicio": "09:00"},
    {"id": 114, "fecha": "2026-08-14", "hora_inicio": "09:00"},
    {"id": 115, "fecha": "2026-08-17", "hora_inicio": "09:00"},
]


class TestResolver(unittest.TestCase):
    def test_fila_por_id_medilink(self):
        c, m = _resolver_cita_seleccionada(CITAS, "ccita_113", "ccita_113", "ccita_")
        self.assertEqual(m, "ok")
        self.assertEqual(c["fecha"], "2026-08-12")

    def test_fila_de_lista_vieja_no_adivina(self):
        # ID de una cita ya cancelada (04/08, id 110) → aviso, jamás otra cita
        c, m = _resolver_cita_seleccionada(CITAS, "ccita_110", "ccita_110", "ccita_")
        self.assertIsNone(c)
        self.assertEqual(m, "id_no_vigente")

    def test_numero_escrito_sigue_funcionando(self):
        c, m = _resolver_cita_seleccionada(CITAS, "3", "3", "ccita_")
        self.assertEqual(c["fecha"], "2026-08-12")

    def test_fecha_escrita_caso_real(self):
        # "12/08 09:00" escrito → debe dar el 12/08, NO el 14/08
        c, m = _resolver_cita_seleccionada(CITAS, "12/08 09:00", "12/08 09:00", "ccita_")
        self.assertEqual(m, "ok")
        self.assertEqual(c["fecha"], "2026-08-12")

    def test_fecha_sola(self):
        c, m = _resolver_cita_seleccionada(CITAS, "el 14/08 porfa", "el 14/08 porfa", "ccita_")
        self.assertEqual(c["fecha"], "2026-08-14")

    def test_texto_basura(self):
        c, m = _resolver_cita_seleccionada(CITAS, "la de la tarde", "la de la tarde", "ccita_")
        self.assertIsNone(c)
        self.assertEqual(m, "no_entendido")

    def test_numero_fuera_de_rango(self):
        c, m = _resolver_cita_seleccionada(CITAS, "9", "9", "ccita_")
        self.assertIsNone(c)


if __name__ == "__main__":
    unittest.main(verbosity=2)
