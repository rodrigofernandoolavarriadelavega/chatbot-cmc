"""Tests de la cola de comprobantes WhatsApp (app/comprobantes_pagos.py).

Valida la lógica pura (destinatario CMC) y la decisión del clasificador
sin llamar a la API de visión.

Uso:
  pytest tests/test_comprobantes_pagos.py -v
  python tests/test_comprobantes_pagos.py
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

os.environ.setdefault("SQLCIPHER_KEY", "")

from comprobantes_pagos import evaluar_destinatario  # noqa: E402
from eco_orden_ocr import decidir_accion  # noqa: E402


class TestDestinatario(unittest.TestCase):
    """La cuenta destino del corpus real (Itaú 221708538 / RUT 77.140.898-2)."""

    def test_cuenta_cmc_ok(self):
        self.assertEqual(evaluar_destinatario("221708538", ""), 1)

    def test_cuenta_con_formato(self):
        # La visión puede transcribir con puntos/guiones
        self.assertEqual(evaluar_destinatario("22-170-8538", ""), 1)

    def test_rut_cmc_ok(self):
        self.assertEqual(evaluar_destinatario("", "77.140.898-2"), 1)

    def test_cuenta_ajena_alerta(self):
        # Transfirió a otra cuenta → 0 (alerta roja en el panel)
        self.assertEqual(evaluar_destinatario("123456789", ""), 0)

    def test_rut_ajeno_alerta(self):
        self.assertEqual(evaluar_destinatario("", "12.345.678-9"), 0)

    def test_sin_datos_es_none(self):
        # Comprobante sin destinatario legible → None (badge neutro, no alerta)
        self.assertIsNone(evaluar_destinatario("", ""))

    def test_cuenta_ajena_pero_rut_cmc(self):
        # RUT manda: apps que muestran RUT y una cuenta interna distinta
        self.assertEqual(evaluar_destinatario("999999", "77140898-2"), 1)


class TestComprobanteNoInterfiereConEco(unittest.TestCase):
    """El comprobante sigue cayendo a recepción en decidir_accion (el encolado
    es un side-effect del hook en main.py, no una acción del router de eco)."""

    def test_comprobante_va_a_recepcion(self):
        dec = decidir_accion({
            "tipo_documento": "comprobante_pago",
            "examenes_solicitados": [],
            "confianza": "alta",
            "comprobante": {"monto": 7880, "num_operacion": "8005079"},
        })
        self.assertEqual(dec["accion"], "recepcion")
        self.assertEqual(dec["motivo"], "no_es_orden")

    def test_orden_eco_ignora_campo_comprobante(self):
        dec = decidir_accion({
            "tipo_documento": "orden_medica",
            "examenes_solicitados": ["Ecografía abdominal"],
            "confianza": "alta",
            "comprobante": None,
        })
        self.assertEqual(dec["accion"], "ofrecer_agenda")


if __name__ == "__main__":
    unittest.main(verbosity=2)
