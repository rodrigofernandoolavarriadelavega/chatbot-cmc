"""Ventana de MATCHING correo↔abono con timestamps mixtos (caso Yendari 2026-08-03).

El correo de Scotiabank por $60.000 a nombre exacto de la paciente quedaba
sin_match: la ventana comparaba strings ('T'+offset vs espacio+naive) y la
'T' (0x54) > espacio (0x20) hacia `creado_at <= email_ts` SIEMPRE falso.

Uso:  python tests/test_abono_matching_ts.py
"""
from __future__ import annotations
import os, sys, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
os.environ.setdefault("SQLCIPHER_KEY", "")
from abono_transferencia import _parse_ts_flexible  # noqa: E402


class TestVentanaMatching(unittest.TestCase):
    def test_caso_yendari(self):
        creado = _parse_ts_flexible("2026-07-31T13:44:00.325785-04:00")
        expira = _parse_ts_flexible("2026-07-31T15:14:00.325785-04:00")
        email = _parse_ts_flexible("2026-07-31 14:00:48")
        self.assertLessEqual(creado, email)   # antes: SIEMPRE falso
        self.assertGreaterEqual(expira, email)

    def test_fuera_de_ventana_sigue_fuera(self):
        expira = _parse_ts_flexible("2026-07-31T15:14:00-04:00")
        email_tarde = _parse_ts_flexible("2026-07-31 20:00:00")
        self.assertLess(expira, email_tarde)

    def test_aware_ambos(self):
        self.assertIsNotNone(_parse_ts_flexible("2026-07-31 14:00:48").tzinfo)
        self.assertIsNotNone(_parse_ts_flexible("2026-07-31T14:00:48-04:00").tzinfo)

    def test_basura(self):
        self.assertIsNone(_parse_ts_flexible("no es fecha"))
        self.assertIsNone(_parse_ts_flexible(""))


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestVentana72h(unittest.TestCase):
    """Caso Katherine Oyarzún (2026-08-20): pagó al día 6 del gate — fuera de
    la ventana original de 90 min. Ahora la ventana es 72 h sobre abonos aún
    pendientes; más allá de 72 h sigue fuera (no re-matchear pagos antiguos)."""

    def test_pago_al_dia_2_entra(self):
        from datetime import timedelta
        creado = _parse_ts_flexible("2026-08-04T13:00:00-04:00")
        email = _parse_ts_flexible("2026-08-06 10:00:00")
        self.assertLessEqual(creado, email)
        self.assertLessEqual(email - creado, timedelta(hours=72))

    def test_pago_al_dia_6_sigue_fuera(self):
        from datetime import timedelta
        creado = _parse_ts_flexible("2026-08-04T13:00:00-04:00")
        email = _parse_ts_flexible("2026-08-10 10:00:00")
        self.assertGreater(email - creado, timedelta(hours=72))
