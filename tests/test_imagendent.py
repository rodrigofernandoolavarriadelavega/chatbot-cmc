# -*- coding: utf-8 -*-
"""Tests del modulo de convenio Imagendent.

El caso de oro es el consumo REAL barrido de Medilink el 2026-09-07: 13 lineas
de prestacion que suman 27 cupones, dejando 3 de los 30 del Plan Oro. Ese "3"
es el numero que el dueno sabia de memoria y que ningun sistema podia confirmar
— si este test se cae, el modulo volvio a mentir.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import session  # noqa: E402

M = None    # se puebla en setUpModule
db = None

_TMP = None
_DB_ORIGINAL = None


def setUpModule():
    """Aisla la DB SIN efectos al importar el archivo.

    Varios tests de esta suite hacen `session.DB_PATH = TMP` a nivel de modulo.
    pytest importa TODOS los archivos de test antes de correr el primero, asi
    que el ultimo import gana y le pisa la DB a los demas — hacerlo aca costo
    11 fallas ajenas la primera vez. Dentro de setUpModule la reasignacion pasa
    cuando este archivo REALMENTE corre, y tearDownModule la devuelve.
    """
    global M, db, _TMP, _DB_ORIGINAL
    _DB_ORIGINAL = session.DB_PATH
    _TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    _TMP.close()
    session.DB_PATH = Path(_TMP.name)

    # vales_routes crea `vales_convenio` al importarse. Va ANTES de abrir
    # cualquier conexion — igual que main.py en produccion. Si su DDL corre
    # dentro de un `with db()` ya abierto, SQLite tira "database is locked" y
    # cada test se cuelga 10 s en el busy_timeout.
    import vales_routes  # noqa: F401
    import imagendent_routes
    from session import db as _db
    M, db = imagendent_routes, _db
    M._crear_tablas()


def tearDownModule():
    session.DB_PATH = _DB_ORIGINAL
    try:
        os.unlink(_TMP.name)
    except OSError:
        pass


# Lo que devolvio Medilink el 2026-09-07 (13 detalles, profs 55 y 72).
CONSUMO_REAL = [
    ("2026-08-14", 55271, "Anguie Rondoy Yovera",        5748),
    ("2026-08-19", 55600, "Camila Cuevas Flores",        5745),
    ("2026-08-19", 55600, "Camila Cuevas Flores",        5746),
    ("2026-08-20", 55750, "Sebastian Bustos",            5747),
    ("2026-08-25", 56123, "Joaquin Veloso Veloso",       5745),
    ("2026-08-25", 56123, "Joaquin Veloso Veloso",       5747),
    ("2026-08-25", 56185, "Martina Sanhueza Hermosilla", 5748),
    ("2026-08-26", 56220, "Marisa Vasquez Paredes",      5748),
    ("2026-09-01", 56701, "Ambaar Sanchez Paredes",      5748),
    ("2026-09-02", 56782, "Michelle Ramirez Pena",       5748),
    ("2026-09-02", 56785, "Deisy Gatica",                5748),
    ("2026-09-02", 56786, "Matias Naegel Veloso",        5748),
    ("2026-09-03", 56906, "Jose Villarroel Martinez",    5745),
]


def _sembrar(filas=CONSUMO_REAL, pagados=True):
    # Re-afirma la DB y la tabla JUSTO antes de escribir. `session.DB_PATH` es un
    # global que media suite reasigna; fijarlo en setUpModule/setUp no basta.
    session.DB_PATH = Path(_TMP.name)
    M._crear_tablas()
    tarifas = {pid: M._tarifa(reg["slug"]) for pid, reg in M.MEDILINK_CONVENIO.items()}
    with db() as c:
        c.execute("DELETE FROM convenio_consumo")
        for i, (fecha, aid, pac, pid) in enumerate(filas, start=1):
            reg = M.MEDILINK_CONVENIO[pid]
            tar = tarifas[pid]
            cobrado = int(tar.get("costo") or 0) + 10_000 if pid == 5748 else int(tar.get("venta") or 0)
            c.execute(
                """INSERT INTO convenio_consumo
                   (detalle_id, atencion_id, fecha, id_paciente, paciente, id_profesional,
                    profesional, id_prestacion, prestacion, slug, bolsa, unidades,
                    costo, venta, cobrado, pagado, realizado, synced_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (i, aid, fecha, 1000 + i, pac, 55, "", pid, reg["slug"], reg["slug"],
                 tar.get("bolsa", ""), reg["unidades"], int(tar.get("costo") or 0),
                 int(tar.get("venta") or 0), cobrado,
                 cobrado if pagados else 0, 1, "2026-09-07T00:00:00"))


class _BaseImagendent(unittest.TestCase):
    """Re-afirma la DB temporal antes de CADA test.

    No alcanza con fijarla en setUpModule: `session.DB_PATH` es un global mutable
    y varios modulos de esta suite lo reasignan durante la corrida, no solo al
    importarse. Sin esto los tests pasan solos y fallan dentro de la suite
    completa con "no such table: convenio_consumo".
    """

    def setUp(self):
        session.DB_PATH = Path(_TMP.name)
        M._crear_tablas()


class ContadorDeCupones(_BaseImagendent):

    def test_el_pack_de_ortodoncia_vale_TRES_cupones(self):
        """La razon numero uno por la que contar filas da mal: 7 packs = 21 RX."""
        self.assertEqual(M.MEDILINK_CONVENIO[5748]["unidades"], 3)
        for suelta in (5745, 5746, 5747):
            self.assertEqual(M.MEDILINK_CONVENIO[suelta]["unidades"], 1)

    def test_consumo_real_del_7_de_septiembre_deja_3_cupones(self):
        _sembrar()
        oro = M.estado()["oro"]
        # 7 packs x3 + 3 panoramicas + 2 teles + 1 bitewing = 27
        self.assertEqual(oro["rx_usados"], 27)
        self.assertEqual(oro["rx_restantes"], 3)

    def test_las_13_filas_no_se_confunden_con_13_cupones(self):
        """Regresion del bug conceptual: 13 lineas != 13 cupones."""
        _sembrar()
        self.assertNotEqual(M.estado()["oro"]["rx_usados"], len(CONSUMO_REAL))

    def test_los_2_cbct_de_cortesia_siguen_intactos(self):
        _sembrar()
        oro = M.estado()["oro"]
        self.assertEqual(oro["cbct_usados"], 0)
        self.assertEqual(oro["cbct_restantes"], 2)

    def test_la_cuenta_de_saldo_no_se_toca_con_puras_RX(self):
        """Las dos bolsas son independientes: gastar cupones no baja el saldo."""
        _sembrar()
        sal = M.estado()["saldo"]
        self.assertEqual(sal["usado"], 0)
        self.assertEqual(sal["restante"], M.CARGA_SALDO)

    def test_el_cbct_descuenta_la_cortesia_antes_que_el_saldo(self):
        filas = CONSUMO_REAL + [("2026-09-04", 56999, "Rosa Marin Carcamo", 5749)]
        _sembrar(filas)
        e = M.estado()
        self.assertEqual(e["oro"]["cbct_usados"], 1)
        self.assertEqual(e["oro"]["cbct_restantes"], 1)
        self.assertEqual(e["saldo"]["restante"], M.CARGA_SALDO)  # aun gratis

    def test_el_tercer_cbct_si_descuenta_el_saldo(self):
        extra = [("2026-09-04", 56990 + n, f"Paciente {n}", 5749) for n in range(3)]
        _sembrar(CONSUMO_REAL + extra)
        e = M.estado()
        self.assertEqual(e["oro"]["cbct_usados"], 2)          # cortesia agotada
        self.assertEqual(e["saldo"]["usado"], 35_000)         # el 3ro va al saldo
        self.assertEqual(e["saldo"]["restante"], M.CARGA_SALDO - 35_000)

    def test_nunca_da_restantes_negativos(self):
        muchos = [("2026-09-05", 57000 + n, f"P{n}", 5748) for n in range(20)]
        _sembrar(muchos)
        self.assertEqual(M.estado()["oro"]["rx_restantes"], 0)

    def test_el_ritmo_semanal_muestra_la_aceleracion(self):
        """3 -> 3 -> 8 -> 13: es el dato que justifica pedir un tramo mas grande."""
        _sembrar()
        self.assertEqual(M.estado()["ritmo_semanal"], [3, 3, 8, 13])

    def test_margen_con_el_precio_correcto_del_pack(self):
        """7 packs cobrados a $40.000 en vez de $45.000 = $35.000 en la mesa."""
        _sembrar()
        p = M.estado()["plata"]
        # costo: 27 RX x $10.000
        self.assertEqual(p["costo"], 270_000)
        self.assertEqual(p["facturado"], 7 * 40_000 + 3 * 15_000 + 2 * 15_000 + 1 * 15_000)
        self.assertEqual(p["margen"], p["facturado"] - p["costo"])

    def test_una_prestacion_ajena_al_convenio_no_entra(self):
        session.DB_PATH = Path(_TMP.name)
        M._crear_tablas()
        with db() as c:
            c.execute("DELETE FROM convenio_consumo")
        self.assertEqual(M.estado()["oro"]["rx_usados"], 0)
        self.assertNotIn(5621, M.MEDILINK_CONVENIO)   # "Revision de examen medico Gratis"
        self.assertNotIn(5738, M.MEDILINK_CONVENIO)   # ecotomografia de codo

    def test_tarifario_no_se_duplica_viene_de_vales_routes(self):
        """Si alguien copia el tarifario, este test lo caza cuando diverja."""
        from vales_routes import PRESTACIONES
        for reg in M.MEDILINK_CONVENIO.values():
            self.assertIn(reg["slug"], PRESTACIONES)
            self.assertEqual(M._tarifa(reg["slug"]), PRESTACIONES[reg["slug"]])


class ReglaDeReposicion(_BaseImagendent):
    """La regla la escribe el dueno. Estos tests describen el contrato."""

    def test_devuelve_none_o_un_dict_con_nivel_y_texto(self):
        for restantes, semanas in ((30, []), (3, [3, 3, 8, 13]), (0, [13])):
            r = M.alerta_reposicion(restantes, semanas)
            if r is None:
                continue
            self.assertIn(r.get("nivel"), ("amarillo", "rojo"))
            self.assertTrue(r.get("texto"))

    def test_el_panel_no_revienta_sin_la_regla_escrita(self):
        _sembrar()
        self.assertIn("alerta", M.estado())


if __name__ == "__main__":
    unittest.main(verbosity=2)
