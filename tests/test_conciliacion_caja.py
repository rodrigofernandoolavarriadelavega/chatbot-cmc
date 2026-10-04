"""Cruce recepción (pagos_cmc) ↔ caja Medilink (bi_pagos_caja) del conciliador
/alma/conciliacion — `conciliacion_routes._cruzar_caja`, función pura.

Por qué existe (2026-10-03): el lado Medilink llamaba a la API /pagos con
`Bearer` (Medilink usa `Token`) → HTTP 401 en prod y lado vacío: cada pago de
recepción salía "FALTANTE ALTA" (1.393 falsos en sep-2026). Se reemplazó por la
caja local, cruzada por paciente-día y con la bonificación Fonasa sumada.

Ejecución:
    PYTHONPATH=app:. venv/bin/python3 tests/test_conciliacion_caja.py
"""
from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT))

import conciliacion_routes as cr  # noqa: E402


def _r(id, fecha, nombre, copago, bonif=0, medio="efectivo", prevision="particular", rut=""):
    return {"id": id, "fecha": fecha, "paciente_nombre": nombre, "copago": copago,
            "metodo_pago": medio, "profesional": "", "prevision": prevision,
            "area": "Medicina General", "rut": rut, "bonif_arancel": bonif}


def _c(fecha, nombre, monto, idp=1):
    return {"fecha": fecha, "id_paciente": idp, "paciente": nombre, "monto": monto, "n": 1}


def _tipos(hs):
    return sorted(h.tipo for h in hs)


class TestCruceCaja(unittest.TestCase):
    def test_fonasa_suma_bonificacion_y_cuadra(self):
        """Recepción guarda el copago, Medilink el arancel completo: con la
        bonificación sumada tiene que cuadrar, no 'diferir'."""
        hs, res = cr._cruzar_caja(
            [_r(1, "2026-09-01", "Juan Pérez", 7250, bonif=7880, prevision="fonasa")],
            [_c("2026-09-01", "JUAN ANDRÉS PÉREZ SOTO", 15130)], None)
        self.assertEqual(hs, [])
        self.assertEqual(res["cuadran"], 1)

    def test_varios_pagos_mismo_paciente_dia_se_suman(self):
        hs, _ = cr._cruzar_caja(
            [_r(1, "2026-09-01", "Ana Soto Ruiz", 10000), _r(2, "2026-09-01", "Ana Soto Ruiz", 5000)],
            [_c("2026-09-01", "Ana Soto Ruiz", 15000)], None)
        self.assertEqual(hs, [])

    def test_faltante_y_sobrante(self):
        hs, _ = cr._cruzar_caja(
            [_r(1, "2026-09-01", "Ana Soto Ruiz", 15000)],
            [_c("2026-09-01", "Pedro Lagos Molina", 20000, idp=9)], None)
        self.assertEqual(_tipos(hs), ["FALTANTE", "SOBRANTE"])

    def test_apellido_comun_solo_no_empareja(self):
        """Un solo token en común ('soto') no basta: son dos personas."""
        self.assertEqual(cr._sim_paciente("Ana Soto Ruiz", "Pedro Soto Lagos"), 0.0)
        self.assertGreaterEqual(cr._sim_paciente("Ana Soto", "ANA MARIA SOTO RUIZ"), 0.66)

    def test_desfase_un_dia_empareja(self):
        hs, res = cr._cruzar_caja(
            [_r(1, "2026-09-01", "Ana Soto Ruiz", 15000)],
            [_c("2026-09-02", "Ana Soto Ruiz", 15000)], None)
        self.assertEqual(hs, [])
        self.assertEqual(res["emparejados"], 1)

    def test_prellenado_sin_cobrar_no_es_faltante(self):
        """Fila creada al abrir la agenda (copago 0, sin medio) = nadie cobró."""
        hs, _ = cr._cruzar_caja([_r(1, "2026-09-01", "Ana Soto Ruiz", 0, medio="")], [], None)
        self.assertEqual(hs, [])

    def test_dias_despues_del_sync_no_se_cruzan(self):
        hs, _ = cr._cruzar_caja([_r(1, "2026-09-03", "Ana Soto Ruiz", 15000)], [],
                                date(2026, 9, 2))
        self.assertEqual(hs, [])

    def test_diferencia_repetida_sale_como_un_patron(self):
        rec = [_r(i, "2026-09-01", f"Paciente{i} Apellido{i}", 7880, bonif=7880, prevision="fonasa")
               for i in range(6)]
        caja = [_c("2026-09-01", f"Paciente{i} Apellido{i}", 15130, idp=i) for i in range(6)]
        hs, res = cr._cruzar_caja(rec, caja, None)
        self.assertEqual(len(hs), 1)
        self.assertTrue(hs[0].paciente.startswith("PATRÓN · 6"))
        self.assertIn("$630 c/u", hs[0].comentario)
        self.assertIn("$3.780 en total", hs[0].comentario)
        self.assertEqual(res["en_patron"], 6)

    def test_diferencia_suelta_se_lista(self):
        hs, _ = cr._cruzar_caja([_r(1, "2026-09-01", "Ana Soto Ruiz", 15000)],
                                [_c("2026-09-01", "Ana Soto Ruiz", 12000)], None)
        self.assertEqual(_tipos(hs), ["DIFERENCIA_MONTO"])
        self.assertFalse(hs[0].paciente.startswith("PATRÓN"))


class TestRecepcionPorMedio(unittest.TestCase):
    def test_bonificacion_no_se_cuela_como_plata_del_medio(self):
        """Antes: copago 0 → monto = bonificación y medio 'efectivo' por defecto."""
        pagos = cr._pagos_cmc_a_pagos([_r(1, "2026-09-01", "X Y", 0, bonif=7880, medio="")])
        self.assertEqual(pagos, [])

    def test_sin_medio_no_se_adivina_efectivo(self):
        pagos = cr._pagos_cmc_a_pagos([_r(1, "2026-09-01", "X Y", 5000, medio="")])
        self.assertEqual(pagos[0].medio, "SIN_MEDIO")


if __name__ == "__main__":
    unittest.main(verbosity=2)
