"""Parser de correos Itaú (agregado 2026-08-20 a pedido del dueño).

Fixture calcada del correo REAL del 14-08-2026 (transacción 590197386,
$41.680, pagador Freddy Orellana) — misma plantilla tabular con whitespace.

Uso:  python tests/test_parser_itau.py
"""
from __future__ import annotations
import os, sys, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
os.environ.setdefault("SQLCIPHER_KEY", "")
from transferencias_email_parser import (identificar_banco, parse_email,  # noqa: E402
                                          _parse_itau)

BODY = """Untitled Document

Estimado(a) Cliente: Centro médico rodrigo Olavarriaga de la Vega riel

Adjuntamos comprobante electronico de transferencia de fondos realizada en
Online Banking.

Transaccion de fondos

Fecha - Hora

14/08/2026-19:19:13 hrs

Numero de Transaccion

590197386

Datos de la Cuenta de Origen

 Nombre

 FREDDY MICHAEL ORELLANA MUNOZ

 Cuenta Personal

 0207615276

Datos de la Cuenta de Destino

 Nombre

 Centro médico rodrigo Olavarriaga de la Vega riel

 Rut

 77.140.898-2

 Cuenta de destino

 0221708538

 Comentario



 Monto:

 $41.680
"""


class TestParserItau(unittest.TestCase):
    def test_identifica_remitente(self):
        self.assertEqual(identificar_banco("Itau <transferencias@itau.cl>"), "itau")

    def test_parse_correo_real(self):
        r = parse_email("itau", "Itaú informa.", BODY)
        self.assertIsNotNone(r)
        self.assertEqual(r["monto"], 41680)
        self.assertEqual(r["nombre"], "FREDDY MICHAEL ORELLANA MUNOZ")
        self.assertEqual(r["fecha"], "2026-08-14")
        self.assertEqual(r["hora"], "19:19")
        self.assertEqual(r["num_operacion"], "590197386")

    def test_cuenta_ajena_se_descarta(self):
        # Mismo formato pero hacia OTRA cuenta → _es_cuenta_cmc lo bota
        body_ajeno = BODY.replace("0221708538", "0999999999").replace("77.140.898-2", "11.111.111-1")
        self.assertIsNone(parse_email("itau", "Itaú informa.", body_ajeno))

    def test_sin_monto_none(self):
        r = _parse_itau("texto sin nada", "")
        self.assertIsNone(r["monto"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
