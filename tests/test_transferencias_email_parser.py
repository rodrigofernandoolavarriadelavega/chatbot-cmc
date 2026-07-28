"""Tests de app/transferencias_email_parser.py — parsers de avisos de
transferencia bancaria (12 bancos) + filtro de cuenta destino del CMC.

Los cuerpos de correo son SINTÉTICOS con la misma estructura, orden de
campos y frases fijas que los correos reales verificados en el buzón del
CMC (`centromedicocarampangue@gmail.com`, auditoría 2026-07-14) — nombres,
montos y números de operación reemplazados por datos ficticios. Se incluye
al menos una variante "plantilla legada" (formato usado hasta ~2023, antes
de que los bancos rediseñaran sus correos) por cada banco que la tuvo, y al
menos un caso de correo REAL descartado por no ser de la cuenta del CMC
(hallazgo del módulo hermano `abono_transferencia.py`: el buzón recibe
avisos de otras cuentas del dueño).

Ejecución:
    PYTHONPATH=app:. venv/bin/python3 tests/test_transferencias_email_parser.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

import transferencias_email_parser as tep  # noqa: E402


class TestIdentificarBanco(unittest.TestCase):
    def test_reconoce_los_12_bancos(self):
        casos = {
            "bancoestado": '"BancoEstado" <noreply@correo.bancoestado.cl>',
            "falabella": "notificaciones@cl.bancofalabella.com",
            "bancochile": "serviciodetransferencias@bancochile.cl",
            "scotiabank": "avisos.info@scotiabank.cl",
            "santander": "mensajeria@santander.cl",
            "bci": "transferencias@bci.cl",
            "ripley": "informaciones@bancoripley.cl",
            "coopeuch": "notificaciones@transaccionalcoopeuch.com",
            "tenpo": "no-reply@tenpo.cl",
            "mach": "noreply@somosmach.com",
            "losandes": "no.reply@losandesprepago.cl",
            "losheroes": "transferenciaprepago@losheroes.cl",
        }
        for esperado, remitente in casos.items():
            with self.subTest(banco=esperado):
                self.assertEqual(tep.identificar_banco(remitente), esperado)

    def test_remitente_desconocido_devuelve_none(self):
        self.assertIsNone(tep.identificar_banco("marketing@tienda-random.cl"))
        self.assertIsNone(tep.identificar_banco(""))
        self.assertIsNone(tep.identificar_banco(None))


class TestSantander(unittest.TestCase):
    def test_plantilla_vigente(self):
        texto = (
            "Comprobante\nTransferencia de fondos\n"
            "Estimado(a) Centro médico Carampangue Eirl:\n"
            "Te informamos que, con fecha 13/07/2026, nuestro cliente MARIA JOSE PEREZ SOTO "
            "realizó una transferencia a tu cuenta. Este es el detalle:\n"
            "Monto transferido\n$ 7.880\nDatos de destino\nNombre\nCentro médico Carampangue Eirl\n"
            "RUT\n77.140.898-2\nBanco\nBanco Itaú\nNº de cuenta\n0-002-21-70853-8\nComentario\n"
        )
        r = tep.parse_email("santander", "Comprobante Transferencia de fondos", texto)
        self.assertIsNotNone(r)
        self.assertEqual(r["nombre"], "MARIA JOSE PEREZ SOTO")
        self.assertEqual(r["monto"], 7880)
        self.assertEqual(r["fecha"], "2026-07-13")

    def test_plantilla_legada_2022(self):
        texto = (
            "Transferencia de Fondos\nComprobante Transferencia de fondos\n"
            "Estimado (a) Centro medico Carampangue eirl :\n"
            "Te informamos que con fecha 12/09/2022, nuestro cliente JUAN CARLOS SOTO LAGOS "
            "ha instruido una transferencia de fondos a su cuenta con el siguiente detalle:\n"
            "Banco de destino:\nCuenta de destino Nro.:\nBanco Itaú\n0-002-21-70853-8\n"
            "Rut destinatario:\nMonto de la Operacion:\n77.140.898-2\n50.000\nComentario:\n"
        )
        r = tep.parse_email("santander", "Transferencia de fondos", texto)
        self.assertIsNotNone(r)
        self.assertEqual(r["nombre"], "JUAN CARLOS SOTO LAGOS")
        self.assertEqual(r["monto"], 50000)
        self.assertEqual(r["fecha"], "2022-09-12")

    def test_correo_a_otra_cuenta_se_descarta(self):
        """Caso REAL (anonimizado): mismo remitente Santander, pero la cuenta
        destino es OTRA cuenta del dueño (Banco Estado, RUT distinto al del
        CMC) — no debe contarse como ingreso del CMC."""
        texto = (
            "Transferencia de Fondos\nComprobante Transferencia de fondos\n"
            "Estimado (a) Rodrigo Perez de la vega :\n"
            "Te informamos que con fecha 14/01/2022, nuestro cliente ANA MARIA GONZALEZ RUIZ "
            "ha instruido una transferencia de fondos a su cuenta con el siguiente detalle:\n"
            "Banco de destino:\nCuenta de destino Nro.:\nBanco Estado\n052370511705\n"
            "Rut destinatario:\nMonto de la Operacion:\n18.144.758-3\n30.000\nComentario:\n"
        )
        r = tep.parse_email("santander", "Transferencia de fondos", texto)
        self.assertIsNone(r)


class TestFalabella(unittest.TestCase):
    def test_plantilla_vigente_multilinea(self):
        """El HTML real, una vez limpiado, deja saltos de línea entre label
        y valor (no todo en una sola línea) — el parser debe tolerarlos."""
        texto = (
            "Aviso de transferencia de fondos recibida\n"
            "Centro Medico Carampangue,\n"
            "Le informamos que hoy, 04-06-2026, nuestro(a) cliente\n"
            "PEDRO PABLO ROJAS ha\n"
            "instruido una transferencia de fondos a su cuenta con el\n"
            "siguiente detalle:\nDetalle de transferencia\nBanco de destinoBanco Itau\n"
            "Cuenta de destinoCuenta Corriente 0221708538\nRut destinatario77.140.898-2\n"
            "AsuntoTransferencia\nMonto transferencia\n$35.000\nDatos transacción\nFecha\n"
            "04-06-2026\nHora\n10:15\nNº de operación\n384620936793\n"
        )
        r = tep.parse_email("falabella", "Aviso de transferencia de fondos recibida", texto)
        self.assertIsNotNone(r)
        self.assertEqual(r["nombre"], "PEDRO PABLO ROJAS")
        self.assertEqual(r["monto"], 35000)
        self.assertEqual(r["fecha"], "2026-06-04")
        self.assertEqual(r["hora"], "10:15")
        self.assertEqual(r["num_operacion"], "384620936793")

    def test_nombre_mixto_mayus_minus(self):
        """Caso real: el nombre no siempre viene todo en mayúsculas."""
        texto = (
            "Aviso de transferencia de fondos recibida\n"
            "Le informamos que hoy, 20-10-2025, nuestro(a) cliente\n"
            "Ricardo ALVAREZ Munoz ha\n"
            "instruido una transferencia de fondos a su cuenta con el\n"
            "siguiente detalle:\nBanco de destinoBanco Itaú\nCuenta de destino0221708538\n"
            "Rut destinatario77.140.898-2\nMonto transferencia\n$15.000\nFecha20-10-2025\n"
            "Hora09:30\nNumero de operacion112233445566\n"
        )
        r = tep.parse_email("falabella", "Aviso de transferencia de fondos recibida", texto)
        self.assertIsNotNone(r)
        self.assertEqual(r["nombre"], "Ricardo ALVAREZ Munoz")

    def test_correo_a_otra_cuenta_se_descarta(self):
        texto = (
            "Aviso de transferencia de fondos recibida\n"
            "Le informamos que hoy, 20-10-2022, nuestro(a) cliente\n"
            "Freddy TORRES Munoz ha\n"
            "instruido una transferencia de fondos a su cuenta con el\n"
            "siguiente detalle:\nBanco de destinoBanco Estado\nCuenta de destino52370511705\n"
            "Rut destinatario18.144.758-3\nMonto transferencia\n$30.000\n"
        )
        r = tep.parse_email("falabella", "Aviso de transferencia de fondos recibida", texto)
        self.assertIsNone(r)


class TestBancoChile(unittest.TestCase):
    def test_plantilla_vigente(self):
        texto = (
            "Banco de Chile | Mi Banco\nComprobante de transferencia electrónica de fondos\n"
            "Estimado(a): Centro medico Carampangue\n"
            "Te informamos que nuestro(a) cliente Marcela Andrea Fuentes ha efectuado una transferencia\n"
            "de fondos a tu cuenta con el siguiente detalle:\nDatos de cuenta\nFecha\n13/07/2026\n"
            "Asunto\nDatos de destinatario\nNombre y Apellido\nCentro medico Carampangue\nRut\n"
            "77140898-2\nEmail\ncentromedicocarampangue@gmail.com\nBanco\nBanco Itau Chile\n"
            "Cuenta destino\nCuenta Corriente\n00-022-17085-38\nMonto\n$2.364\n"
            "Número de comprobante\nTEFMBCO2607131715305417376980\nFecha y Hora:\n"
            "lunes 13 de julio de 2026 17:15\n"
        )
        r = tep.parse_email("bancochile", "Aviso de transferencia de fondos", texto)
        self.assertIsNotNone(r)
        self.assertEqual(r["nombre"], "Marcela Andrea Fuentes")
        self.assertEqual(r["monto"], 2364)
        self.assertEqual(r["fecha"], "2026-07-13")
        self.assertEqual(r["hora"], "17:15")

    def test_correo_a_cuenta_personal_se_descarta(self):
        """Caso REAL (anonimizado): 'Transferencia a terceros' con RUT
        PERSONAL del dueño (no el RUT del CMC) y cuenta en otro banco."""
        texto = (
            "Banco de Chile | Banca Movil\nComprobante de Transferencia a terceros\n"
            "Estimado(a) Rodrigo Perez de la Vega ,\n"
            "Le informamos que nuestro(a) cliente Camila Soto Reyes ha efectuado una Transferencia "
            "a terceros a su cuenta con el siguiente detalle:\nNombre:\nRodrigo Perez de la Vega\n"
            "Rut:\n18144758-3\nTipo de Cuenta:\nCuenta Vista\nNº de Cuenta:\n05-237-05117-05\n"
            "Banco:\nBanco Estado\nMonto:\n$15.000\n"
        )
        r = tep.parse_email("bancochile", "Aviso de transferencia de fondos", texto)
        self.assertIsNone(r)


class TestBancoEstado(unittest.TestCase):
    def test_plantilla_vigente_envio_o_recepcion(self):
        texto = (
            "31/05/2025\nComprobante de Transferencia Electronica de Fondos (TEF)\n"
            "Estimado(a) Centro Mdico Carampangue\n"
            "Te informamos que hoy 31/05/2025 10:58:55, has recibido una Transferencia Electronica, "
            "de nuestro(a) cliente Loreto Andrea Munoz, con los siguientes datos:\n"
            "Monto transferido:\n $15.000\nDetalle del destinatario\nNombre:Centro Mdico Carampangue\n"
            "RUT:77.140.898-2\nBanco:Itau Corpbanca\nN de cuenta:221708538\n"
            "Número de Operación:7081282\nComentario:pago consulta\n"
        )
        r = tep.parse_email("bancoestado", "Aviso de Transferencia de Fondos", texto)
        self.assertIsNotNone(r)
        self.assertEqual(r["nombre"], "Loreto Andrea Munoz")
        self.assertEqual(r["monto"], 15000)
        self.assertEqual(r["fecha"], "2025-05-31")
        self.assertEqual(r["hora"], "10:58")
        self.assertEqual(r["num_operacion"], "7081282")
        self.assertEqual(r["mensaje"], "pago consulta")

    def test_plantilla_legada(self):
        texto = (
            "Comprobante de transferencia recibida\nBancoEstado\n"
            "Estimado(a) Centro medico :\n"
            "Has recibido una Transferencia Electronica\nde nuestro(a) cliente Patricia Elena Diaz\n"
            "Datos de la transferencia que recibiste\nMonto$7.880\nParaCentro medico \n"
            "RUT77.140.898-2\nCuenta0221708538\nBancoBANCO ITAU\nMensajeBono\n"
            "Fecha y hora01/07/2026 14:20:04\nN° transaccion8036026\n"
        )
        r = tep.parse_email("bancoestado", "Aviso de Transferencia de Fondos", texto)
        self.assertIsNotNone(r)
        self.assertEqual(r["nombre"], "Patricia Elena Diaz")
        self.assertEqual(r["monto"], 7880)


class TestScotiabank(unittest.TestCase):
    def test_plantilla_con_error_grafia_nuestros_cliente(self):
        """El correo real dice 'nuestros cliente' (sic, error del banco) —
        el parser debe tolerar singular Y este plural-mal-concordado."""
        texto = (
            "Scotiabank - Banca Persona\nAviso importante\nTransferencia de fondos recibida\n"
            "¡Hola centro medico Carampangue!\n"
            "Con fecha de hoy 13/07/2026, nuestros cliente el Sr(a) LORENA PAZ MUNOZ ha instruido "
            "transferencia de fondos a su cuenta N° 221708538.\nNombre Destinatario\n:\n"
            "centro medico Carampangue\nNúmero de Cuenta\n:\n221708538\nBanco\n:\nBANCO ITAU\n"
            "Monto\n:\n2.364\nMensaje\n:\nconsulta\n"
        )
        r = tep.parse_email("scotiabank", "Aviso de Transferencia", texto)
        self.assertIsNotNone(r)
        self.assertEqual(r["nombre"], "LORENA PAZ MUNOZ")
        self.assertEqual(r["monto"], 2364)

    def test_destino_banco_estado_se_descarta(self):
        texto = (
            "Scotiabank - Banca Persona\nTransferencia de fondos recibida\n"
            "¡Hola Rodrigo Perez!\nCon fecha de hoy 10/01/2022, nuestros cliente el Sr(a) "
            "CARLOS EDUARDO ROJAS ha instruido transferencia de fondos a su cuenta N° 52370511705.\n"
            "Nombre Destinatario\n:\nRodrigo Perez\nNúmero de Cuenta\n:\n52370511705\nBanco\n:\n"
            "BANCO DEL ESTADO DE CHILE\nMonto\n:\n6.460\n"
        )
        r = tep.parse_email("scotiabank", "Aviso de Transferencia", texto)
        self.assertIsNone(r)


class TestBCI(unittest.TestCase):
    def test_hacia_cuenta_itau_se_acepta(self):
        texto = (
            "centromedicocarampangue@gmail.com\nHola\ncentro médico Carampangue\n"
            "Has recibido una transferencia de fondos de Nicolas Esteban Vega hacia tu cuenta del "
            "Banco ITAU\nDatos de la transferencia\nMonto recibido\n$2.364\nBanco de origen\n"
            "Banco Bci\nFecha de la transferencia\n09/07/2026\nMensaje\nSin mensaje\n"
            "Número de comprobante\n1197302873\n"
        )
        r = tep.parse_email("bci", "Aviso de Transferencia de Fondos.", texto)
        self.assertIsNotNone(r)
        self.assertEqual(r["nombre"], "Nicolas Esteban Vega")
        self.assertEqual(r["monto"], 2364)

    def test_hacia_cuenta_banco_estado_se_descarta(self):
        """Caso REAL (anonimizado): el mismo remitente BCI también notifica
        transferencias hacia OTRA cuenta del dueño en Banco Estado — sin
        número de cuenta explícito, el único criterio es el nombre del banco
        destino."""
        texto = (
            "centromedicocarampangue@gmail.com\nHola\nRodrigo Perez\n"
            "Has recibido una transferencia de fondos de Sandra Ivonne Lara hacia tu cuenta del "
            "BANCO ESTADO\nDatos de la transferencia\nMonto recibido\n$13.000\nBanco de origen\n"
            "BANCO BCI\nFecha de la transferencia\n07/03/2023\nMensaje\nconsulta medica\n"
            "Número de comprobante\n662370661\n"
        )
        r = tep.parse_email("bci", "Aviso de Transferencia de Fondos.", texto)
        self.assertIsNone(r)


class TestRipley(unittest.TestCase):
    def test_hacia_cuenta_itau_se_acepta(self):
        texto = (
            "Banco Ripley - Aviso de Transferencia de Fondos\n02/07/2026 05:29:55\n"
            "Aviso de transferencia de fondos\nEstimado (a): Centro Medico Carampangue\n"
            "Nuestro cliente TOMAS ALEJANDRO PENA ha realizado una transferencia de fondos en línea "
            "a su cuenta del Banco Itau.\nTe enviamos el detalle de esta operación:\n"
            "Monto Transferido:\n$2.584\nBanco de Origen:\nBanco Ripley\nComentario:\nPago consulta\n"
            "Número de Transacción:\n178987591\n"
        )
        r = tep.parse_email("ripley", "Transferencia Electrónica de Fondos Banco Ripley", texto)
        self.assertIsNotNone(r)
        self.assertEqual(r["nombre"], "TOMAS ALEJANDRO PENA")
        self.assertEqual(r["monto"], 2584)

    def test_hacia_banco_estado_se_descarta(self):
        texto = (
            "Banco Ripley - Aviso de Transferencia de Fondos\n16/03/2022 11:58:23\n"
            "Estimado (a): Rodrigo Perez\nNuestro cliente MARCO SILVA ESCOBAR ha realizado una "
            "transferencia de fondos en línea a su cuenta del Banco Estado.\nMonto Transferido:\n"
            "$6.750\nBanco de Origen:\nBanco Ripley\n"
        )
        r = tep.parse_email("ripley", "Transferencia Electrónica de Fondos Banco Ripley", texto)
        self.assertIsNone(r)


class TestCoopeuch(unittest.TestCase):
    def test_reordena_apellidos_nombres(self):
        texto = (
            "Información al: 03-03-2026 Hora: 10:41\nAviso de Transferencia\n"
            "Le informamos que nuestro Socio / Cliente ha realizado una transferencia de fondos con "
            "el siguiente detalle\nFecha: 03-03-2026 Hora: 10:41\nTransacción Num\n83613312\n"
            "Cuenta de Origen\nCUENTA VISTA 185224812\nNombre: VERGARA, ANGELICA MARIA\n"
            "RUT: 127720835\nInstitución: COOPEUCH / DALE\nCuenta Destino\n"
            "CUENTA CORRIENTE 221708538\nRUT: 771408982\nInstitución: BANCO ITAU\n"
            "E-mail: centromedicocarampangue@gmail.com\nMotivo: Sin Mensaje\nMonto\n$ 7750\n"
        )
        r = tep.parse_email("coopeuch", "Transferencia de fondo Coopeuch", texto)
        self.assertIsNotNone(r)
        self.assertEqual(r["nombre"], "ANGELICA MARIA VERGARA")
        self.assertEqual(r["monto"], 7750)


class TestTenpo(unittest.TestCase):
    def test_plantilla_vigente(self):
        texto = (
            "Comprobante de transferencia exitosa\n"
            "La transferencia de DANIELA ESPERANZA ROJAS  por $7.880 a tu cuenta fue exitosa.\n"
            "Monto transferencia:\n $ 7.880\nNombre del destinatario:\n CAMILA ANDREA CUEVAS \n"
            "Banco de destino:\n ITAÚ CORPBANC\nNº cuenta de destino:\n 221708538\nRUT:\n "
            "77.140.898-2\nFecha:\n 01-06-2026\nMensaje:\n \nHora:\n 16:12:18\n"
            "Código de transferencia:\n 000077374867\n"
        )
        r = tep.parse_email("tenpo", "Comprobante de transferencia - Tenpo", texto)
        self.assertIsNotNone(r)
        self.assertEqual(r["monto"], 7880)
        self.assertEqual(r["hora"], "16:12")
        self.assertEqual(r["num_operacion"], "000077374867")


class TestMach(unittest.TestCase):
    def test_plantilla_vigente(self):
        texto = (
            "Recibiste una transferencia\n"
            "Hola Centro. Acabas de recibir una transferencia de Ignacio Andres Molina sin costo "
            "desde MACHBANK.\nDetalle\nFecha\n27/03/2026 - 18:34:21\nNombre destinatario\n"
            "Centro Medico Carampangue Eirl\nRUT\n77.140.898-2\nBanco destino\nBANCO ITAU\n"
            "Cuenta destino\n221708538\nMonto\n$3.152\nMensaje\nCódigo de confirmación\n"
            "492947252689\n"
        )
        r = tep.parse_email("mach", "Recibiste una transferencia de Ignacio Andres Molina", texto)
        self.assertIsNotNone(r)
        self.assertEqual(r["nombre"], "Ignacio Andres Molina")
        self.assertEqual(r["monto"], 3152)
        self.assertEqual(r["fecha"], "2026-03-27")
        self.assertEqual(r["hora"], "18:34")


class TestLosAndesYLosHeroes(unittest.TestCase):
    def test_losandes_plantilla(self):
        texto = (
            "Caja Los Andes\nHola Centro Medico Carampangue,\n"
            "Te informamos que Alicia Beatriz Fuentes ha enviado una transferencia de fondos a tu "
            "cuenta.\nDatos de origen:\nDe cuenta: 20698543\nDatos de destino:\n"
            "Número de cuenta: 0221708538\nBanco destino: Banco Itaú Corpbanca\nMonto: $7.880\n"
            "Datos de transacción:\nFecha: 08/05/2026\nCódigo de transacción: 275509820233\n"
        )
        r = tep.parse_email("losandes", "Notificación de Transferencia", texto)
        self.assertIsNotNone(r)
        self.assertEqual(r["nombre"], "Alicia Beatriz Fuentes")
        self.assertEqual(r["monto"], 7880)

    def test_losheroes_plantilla(self):
        texto = (
            "Comprobante de Transferencia de Fondos\nEstimado(a),\n"
            "Le informamos que nuestro(a) cliente ROSA ELVIRA CASTRO ha efectuado una transferencia "
            "de fondos a su cuenta con el siguiente detalle:\nMONTO TRANSFERIDO: $7.880\nORIGEN\n"
            "Comprobante: Titular\nCuenta Nro.: 116013129\nDESTINO\nInstitución: Banco Itaú\n"
            "Tipo Cuenta: Cuenta Corriente\nCuenta Nro.: 221708538\nRut: 77.140.898-2\n"
            "Mail: centromedicocarampangue@gmail.com\nComprobante:\n621914\nFecha y Hora:\n"
            "23 de junio de 2026 12:07:25\n"
        )
        r = tep.parse_email("losheroes", "Prepago Los Héroes - Comprobante Transferencia", texto)
        self.assertIsNotNone(r)
        self.assertEqual(r["nombre"], "ROSA ELVIRA CASTRO")
        self.assertEqual(r["monto"], 7880)
        self.assertEqual(r["fecha"], "2026-06-23")
        self.assertEqual(r["hora"], "12:07")


class TestDegradaConGracia(unittest.TestCase):
    def test_banco_desconocido_no_lanza(self):
        self.assertIsNone(tep.parse_email("banco_inexistente", "asunto", "cuerpo"))

    def test_cuerpo_vacio_no_lanza(self):
        self.assertIsNone(tep.parse_email("santander", "", ""))
        self.assertIsNone(tep.parse_email("falabella", "asunto", None))

    def test_cuerpo_basura_no_lanza(self):
        basura = "�" * 500 + "\x00\x01\x02" + "<html>roto"
        for banco in tep.PARSERS:
            with self.subTest(banco=banco):
                r = tep.parse_email(banco, "asunto raro", basura)
                self.assertIsNone(r)  # no lanza, y no inventa un match


if __name__ == "__main__":
    unittest.main(verbosity=2)
