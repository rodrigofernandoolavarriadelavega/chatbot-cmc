"""Atribución por arancel + nombres robustos (idea del dueño 2026-08-20).

Casos reales: pago 37477 Claudio Lobos ($20.980 psico colgado a Abarca por
nombre con doble espacio) y pago 37030 Samira ($30.520 partido).

Uso:  python tests/test_atribucion_monto.py
"""
from __future__ import annotations
import os, sys, sqlite3, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
os.environ.setdefault("SQLCIPHER_KEY", "")
os.environ.setdefault("MEDILINK_TOKEN", "x")
import bi_sync  # noqa: E402
from bi_sync import (_norm_nombre, _nombres_calzan,  # noqa: E402
                      _resolver_profesional_pagos_cmc, _tabla_monto_area)


def _db():
    con = sqlite3.connect(":memory:")
    con.execute("""CREATE TABLE pagos_cmc (fecha TEXT, paciente_nombre TEXT,
        id_profesional INT, copago INT, bonificacion INT)""")
    con.execute("""CREATE TABLE bi_pagos_caja (pago_id INT, fecha TEXT,
        monto INT, id_profesional INT)""")
    # historia: 20 pagos de $20.980 todos de psico (74) → arancel puro
    for i in range(20):
        con.execute("INSERT INTO bi_pagos_caja VALUES (?, date('now','-10 days'), 20980, 74)", (1000 + i,))
    # $30.000 mezclado → NO entra a la tabla
    for i, pid in enumerate([55] * 10 + [1] * 8):
        con.execute("INSERT INTO bi_pagos_caja VALUES (?, date('now','-10 days'), 30000, ?)", (2000 + i, pid))
    return con


class TestNombres(unittest.TestCase):
    def test_doble_espacio_y_tildes(self):
        self.assertEqual(_norm_nombre("Claudio Ignacio  Lobos Salazár"),
                         "claudio ignacio lobos salazar")

    def test_subconjunto_recepcion_corto(self):
        # caso real 37477: recepción omite el segundo nombre
        self.assertTrue(_nombres_calzan(
            _norm_nombre("Claudio Lobos Salazar"),
            _norm_nombre("Claudio Ignacio  Lobos Salazar")))

    def test_un_token_no_calza(self):
        self.assertFalse(_nombres_calzan("claudio", "claudio lobos salazar"))

    def test_distintos_no_calzan(self):
        self.assertFalse(_nombres_calzan("maria perez soto", "juan perez soto"))


class TestDesempateArancel(unittest.TestCase):
    def setUp(self):
        bi_sync._MONTO_AREA_CACHE.update(ts=0.0, tabla={})
        self.c = _db()

    def test_tabla_dinamica(self):
        t = _tabla_monto_area(self.c)
        self.assertEqual(t.get(20980), "psico")
        self.assertNotIn(30000, t)  # impuro → fuera

    def test_caso_claudio_resuelve_por_arancel(self):
        # día con DOS profesionales (73 med + 74 psico), nombre corto en
        # recepción, monto $20.980 (arancel psico) → debe elegir 74
        self.c.execute("INSERT INTO pagos_cmc VALUES ('2026-07-29', 'Claudio Lobos Salazar', 73, 4334, 0)")
        self.c.execute("INSERT INTO pagos_cmc VALUES ('2026-07-29', 'Claudio Lobos Salazar', 74, 7210, 0)")
        r = _resolver_profesional_pagos_cmc(
            self.c, "2026-07-29", "Claudio Ignacio  Lobos Salazar", 20980)
        self.assertEqual(r, 74)

    def test_monto_no_arancel_cae_a_distancia(self):
        self.c.execute("INSERT INTO pagos_cmc VALUES ('2026-07-29', 'Ana Soto Diaz', 73, 15130, 0)")
        self.c.execute("INSERT INTO pagos_cmc VALUES ('2026-07-29', 'Ana Soto Diaz', 74, 50000, 0)")
        # monto 50.000 no es arancel puro → desempate por distancia → 74
        r = _resolver_profesional_pagos_cmc(self.c, "2026-07-29", "Ana Soto Diaz", 50000)
        self.assertEqual(r, 74)

    def test_un_solo_prof_directo(self):
        self.c.execute("INSERT INTO pagos_cmc VALUES ('2026-07-29', 'Rosa Vera Vera', 52, 0, 0)")
        r = _resolver_profesional_pagos_cmc(self.c, "2026-07-29", "Rosa  Vera Vera", 9540)
        self.assertEqual(r, 52)


if __name__ == "__main__":
    unittest.main(verbosity=2)
