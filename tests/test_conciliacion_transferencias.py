"""Tests de app/conciliacion_transferencias.py — motor de cruce puro
(`_conciliar_listas`) contra fixtures en memoria. Sin IMAP, sin SQLite real
(`_conciliar_listas` no toca la base — solo recibe y devuelve listas/dicts).

Cubre las 6 categorías del reporte:
  1. Cruce exacto por código de transferencia (pagos_cmc.codigo_transferencia
     == num_operacion del correo).
  2. Cruce exacto por monto+fecha+nombre coincide.
  3. Cruce "probable" — monto+fecha coinciden, nombre NO coincide (la trampa
     del familiar que paga: no debe reportarse como "sin registrar").
  4. Cruce con fecha desfasada ±1 día.
  5. Grupo ambiguo — 2+ pagos y 2+ correos comparten el mismo monto ese día,
     sin nombre que desempate → NINGUNO se asigna, se reporta el empate.
  6. Registrado sin correo / correo sin registro cuando no hay ningún
     candidato de monto+fecha del otro lado.

Ejecución:
    PYTHONPATH=app:. venv/bin/python3 tests/test_conciliacion_transferencias.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

import conciliacion_transferencias as ct  # noqa: E402


def _pago(id, fecha, monto, paciente, profesional="Dr. Prueba", codigo_transferencia=""):
    return {
        "id": id, "fecha": fecha, "hora": "10:00", "paciente_nombre": paciente,
        "rut": "", "profesional": profesional, "area": "Medicina General",
        "copago": monto, "codigo_transferencia": codigo_transferencia,
    }


def _email(uid, fecha, monto, nombre, banco="falabella", num_operacion="", hora="10:00"):
    return {
        "uid": uid, "banco": banco, "nombre_transfiere": nombre, "monto": monto,
        "fecha": fecha, "hora": hora, "num_operacion": num_operacion, "mensaje": "",
    }


class TestCrucePorCodigo(unittest.TestCase):
    def test_codigo_transferencia_gana_aunque_nombre_no_coincida(self):
        pagos = [_pago(1, "2026-07-01", 15000, "Paciente Distinto", codigo_transferencia="384620936793")]
        emails = [_email(101, "2026-07-01", 15000, "OTRO NOMBRE TOTALMENTE DISTINTO", num_operacion="384620936793")]
        r = ct._conciliar_listas(pagos, emails, "2026-07-01", "2026-07-01")
        self.assertEqual(len(r["matches"]), 1)
        self.assertEqual(r["matches"][0]["confianza"], "exacto_codigo")
        self.assertEqual(r["totales"]["registrado_sin_correo_n"], 0)
        self.assertEqual(r["totales"]["correo_sin_registro_n"], 0)


class TestCruceExacto(unittest.TestCase):
    def test_monto_fecha_nombre_coincide(self):
        pagos = [_pago(1, "2026-07-01", 7880, "Maria Jose Perez")]
        emails = [_email(101, "2026-07-01", 7880, "MARIA JOSE PEREZ")]
        r = ct._conciliar_listas(pagos, emails, "2026-07-01", "2026-07-01")
        self.assertEqual(len(r["matches"]), 1)
        self.assertEqual(r["matches"][0]["confianza"], "exacto")


class TestCruceProbableNombreDistinto(unittest.TestCase):
    def test_familiar_que_paga_no_es_sin_registrar(self):
        """LA TRAMPA: el hijo paga la consulta de la madre. Monto y fecha
        coinciden, el nombre NO — debe conciliarse como 'probable', jamás
        reportarse como plata sin registrar."""
        pagos = [_pago(1, "2026-07-01", 15000, "Elena Rodriguez Paciente")]
        emails = [_email(101, "2026-07-01", 15000, "Cristobal Rodriguez Hijo")]
        r = ct._conciliar_listas(pagos, emails, "2026-07-01", "2026-07-01")
        self.assertEqual(len(r["matches"]), 1)
        self.assertEqual(r["matches"][0]["confianza"], "probable")
        self.assertEqual(r["totales"]["registrado_sin_correo_n"], 0)
        self.assertEqual(r["totales"]["correo_sin_registro_n"], 0)


class TestFechaDesfasada(unittest.TestCase):
    def test_desfase_un_dia_conciliado_como_tal(self):
        pagos = [_pago(1, "2026-07-02", 7880, "Ana Lopez")]
        emails = [_email(101, "2026-07-01", 7880, "ANA LOPEZ")]  # correo llegó 1 día antes
        r = ct._conciliar_listas(pagos, emails, "2026-07-01", "2026-07-02")
        self.assertEqual(len(r["matches"]), 1)
        self.assertIn("fecha_desfasada", r["matches"][0]["confianza"])

    def test_desfase_dos_dias_no_concilia(self):
        pagos = [_pago(1, "2026-07-03", 7880, "Ana Lopez")]
        emails = [_email(101, "2026-07-01", 7880, "ANA LOPEZ")]
        r = ct._conciliar_listas(pagos, emails, "2026-07-01", "2026-07-03")
        self.assertEqual(len(r["matches"]), 0)


class TestAmbiguo(unittest.TestCase):
    def test_tres_pacientes_mismo_monto_mismo_dia_no_se_adivina(self):
        """Si 3 pacientes pagaron $15.000 el mismo día y llegaron 3 correos
        de banco por $15.000 ese día, sin nombre que desempate ninguno debe
        asignarse — se reporta el grupo ambiguo completo."""
        pagos = [
            _pago(1, "2026-07-01", 15000, "Paciente Uno"),
            _pago(2, "2026-07-01", 15000, "Paciente Dos"),
            _pago(3, "2026-07-01", 15000, "Paciente Tres"),
        ]
        emails = [
            _email(101, "2026-07-01", 15000, "Nombre Completamente Distinto A"),
            _email(102, "2026-07-01", 15000, "Nombre Completamente Distinto B"),
            _email(103, "2026-07-01", 15000, "Nombre Completamente Distinto C"),
        ]
        r = ct._conciliar_listas(pagos, emails, "2026-07-01", "2026-07-01")
        self.assertEqual(len(r["matches"]), 0)
        self.assertEqual(r["totales"]["ambiguo_grupos"], 1)
        self.assertEqual(len(r["ambiguos"][0]["pagos"]), 3)
        self.assertEqual(len(r["ambiguos"][0]["correos"]), 3)
        # No deben aparecer también como "sin registrar" — están en el ambiguo.
        self.assertEqual(r["totales"]["registrado_sin_correo_n"], 0)
        self.assertEqual(r["totales"]["correo_sin_registro_n"], 0)

    def test_nombre_fuerte_desambigua_y_el_resto_queda_unico(self):
        """Dentro de un grupo con montos empatados, el par con nombre
        claramente coincidente se asigna primero como 'exacto'. Una vez
        retirado, el ÚNICO par que queda (aunque el nombre no coincida)
        también se concilia — ya no hay ningún otro candidato posible con
        quien confundirlo, mismo criterio que 'el familiar que paga'."""
        pagos = [
            _pago(1, "2026-07-01", 15000, "Maria Jose Fernandez Soto"),
            _pago(2, "2026-07-01", 15000, "Persona Sin Relacion"),
        ]
        emails = [
            _email(101, "2026-07-01", 15000, "MARIA JOSE FERNANDEZ SOTO"),
            _email(102, "2026-07-01", 15000, "Nombre Que No Calza Con Nadie"),
        ]
        r = ct._conciliar_listas(pagos, emails, "2026-07-01", "2026-07-01")
        self.assertEqual(len(r["matches"]), 2)
        por_pago = {m["pago_id"]: m for m in r["matches"]}
        self.assertEqual(por_pago[1]["confianza"], "exacto")
        self.assertEqual(por_pago[2]["confianza"], "probable")
        self.assertEqual(r["totales"]["ambiguo_grupos"], 0)


class TestAmbiguoNoMezclaFechasLejanas(unittest.TestCase):
    def test_mismo_monto_anios_distintos_no_se_agrupan(self):
        """BUG real encontrado 2026-07-14: agrupar 'todo lo que comparte
        este monto' sin acotar por fecha juntaba pagos y correos de AÑOS
        distintos en un solo grupo ambiguo gigante (montos redondos como
        $15.000 se repiten cientos de veces en 3 años de historia). El
        union-find debe separar estos en grupos por cercanía real de fecha."""
        pagos = [
            _pago(1, "2023-01-10", 15000, "Persona A"),
            _pago(2, "2023-01-10", 15000, "Persona B"),
            _pago(3, "2026-06-20", 15000, "Persona C"),
            _pago(4, "2026-06-20", 15000, "Persona D"),
        ]
        emails = [
            _email(101, "2023-01-10", 15000, "Nombre Distinto Uno"),
            _email(102, "2023-01-10", 15000, "Nombre Distinto Dos"),
            _email(201, "2026-06-20", 15000, "Nombre Distinto Tres"),
            _email(202, "2026-06-20", 15000, "Nombre Distinto Cuatro"),
        ]
        r = ct._conciliar_listas(pagos, emails, "2023-01-01", "2026-12-31")
        self.assertEqual(r["totales"]["ambiguo_grupos"], 2)
        for g in r["ambiguos"]:
            fechas_pagos = {p["fecha"] for p in g["pagos"]}
            fechas_correos = {c["fecha"] for c in g["correos"]}
            # Ningún grupo debe mezclar 2023 con 2026.
            self.assertEqual(len(fechas_pagos), 1)
            self.assertEqual(len(fechas_correos), 1)
            self.assertEqual(len(g["pagos"]), 2)
            self.assertEqual(len(g["correos"]), 2)


class TestSinCorreoYSinRegistro(unittest.TestCase):
    def test_pago_sin_ningun_correo_de_ese_monto(self):
        pagos = [_pago(1, "2026-07-01", 99000, "Paciente Solo")]
        emails = []
        r = ct._conciliar_listas(pagos, emails, "2026-07-01", "2026-07-01")
        self.assertEqual(r["totales"]["registrado_sin_correo_n"], 1)
        self.assertEqual(r["totales"]["registrado_sin_correo_monto"], 99000)
        self.assertEqual(r["totales"]["correo_sin_registro_n"], 0)

    def test_correo_sin_ningun_pago_de_ese_monto(self):
        pagos = []
        emails = [_email(101, "2026-07-01", 88000, "Nadie Registrado")]
        r = ct._conciliar_listas(pagos, emails, "2026-07-01", "2026-07-01")
        self.assertEqual(r["totales"]["correo_sin_registro_n"], 1)
        self.assertEqual(r["totales"]["correo_sin_registro_monto"], 88000)
        self.assertEqual(r["totales"]["registrado_sin_correo_n"], 0)


class TestSimilitudNombre(unittest.TestCase):
    def test_identico(self):
        self.assertEqual(ct._similitud_nombre("Juan Perez", "JUAN PEREZ"), 1.0)

    def test_sin_relacion(self):
        self.assertEqual(ct._similitud_nombre("Juan Perez", "Maria Soto"), 0.0)

    def test_parcial_familiar(self):
        sim = ct._similitud_nombre("Juan Perez Soto", "Juan Perez")
        self.assertGreater(sim, 0.0)
        self.assertLess(sim, 1.0)

    def test_vacio_no_lanza(self):
        self.assertEqual(ct._similitud_nombre("", "algo"), 0.0)
        self.assertEqual(ct._similitud_nombre(None, None), 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
