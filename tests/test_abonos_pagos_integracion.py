"""
Tests de integración Abonos ↔ Pagos.

Cubre:
    1. crear abono  → pago vinculado con copago=monto_abono y origen='abono'
    2. editar abono → pago vinculado actualiza copago y metodo_pago
    3. borrar abono → pago vinculado eliminado (solo si no aplicado)
    4. aplicar abono → pago de SALDO sin duplicar el monto_abono ya registrado

Patrón: SQLite in-memory + monkeypatch de session.db.
tearDown purga MagicMocks de sys.modules para no contaminar otras suites.
"""

import asyncio
import os
import sqlite3
import sys
import types
import unittest
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

# Ajustar path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_db():
    """Crea una DB SQLite in-memory con las tablas abonos_cmc y pagos_cmc."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS abonos_cmc (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fecha TEXT NOT NULL DEFAULT '',
            hora TEXT NOT NULL DEFAULT '',
            paciente_nombre TEXT NOT NULL DEFAULT '',
            rut TEXT DEFAULT '',
            id_profesional INTEGER,
            profesional TEXT DEFAULT '',
            area TEXT DEFAULT '',
            procedimiento TEXT DEFAULT '',
            fecha_cita TEXT DEFAULT '',
            precio_total INTEGER DEFAULT 0,
            monto_abono INTEGER DEFAULT 0,
            saldo INTEGER DEFAULT 0,
            metodo_pago TEXT DEFAULT 'transferencia',
            folio TEXT DEFAULT '',
            codigo_transferencia TEXT DEFAULT '',
            estado TEXT DEFAULT 'pendiente',
            id_cita TEXT DEFAULT '',
            pago_id INTEGER,
            nota TEXT DEFAULT '',
            creado_por TEXT DEFAULT 'recepcion',
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pagos_cmc (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fecha TEXT DEFAULT '',
            hora TEXT DEFAULT '',
            paciente_nombre TEXT DEFAULT '',
            rut TEXT DEFAULT '',
            id_profesional INTEGER,
            profesional TEXT DEFAULT '',
            area TEXT DEFAULT '',
            prevision TEXT DEFAULT 'particular',
            copago INTEGER DEFAULT 0,
            bonificacion INTEGER DEFAULT 0,
            metodo_pago TEXT DEFAULT 'efectivo',
            folio TEXT DEFAULT '',
            codigo_transferencia TEXT DEFAULT '',
            procedimiento TEXT DEFAULT '',
            origen TEXT DEFAULT 'presencial',
            id_cita TEXT DEFAULT '',
            creado_por TEXT DEFAULT 'recepcion',
            canal TEXT DEFAULT 'presencial',
            fuente TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    return conn


@contextmanager
def _db_ctx(conn):
    """Context manager que devuelve la conexión directamente (sin begin/commit extra)."""
    yield conn


class _FakeDB:
    """Reemplaza session.db — devuelve siempre la misma in-memory conn."""
    def __init__(self, conn):
        self._conn = conn

    def __call__(self):
        return _db_ctx(self._conn)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ── Módulos a mockear que no necesitamos en estos tests ──────────────────────
_STUB_MODULES = [
    "config", "admin_routes", "alma_scope", "prof_canon",
    "pagos_routes",  # lo stubeamos para controlar ensure_pagos_table
    "resilience", "messaging", "medilink", "fidelizacion",
    "winback", "meta_capi", "promo_postconsent", "alma_switchboard",
]


class AbonosPagosIntegracionTest(unittest.TestCase):

    def setUp(self):
        self._conn = _make_db()
        self._patches = []

        # Stubs básicos de módulos que abonos_routes importa
        for mod in _STUB_MODULES:
            p = patch.dict("sys.modules", {mod: MagicMock()})
            p.start()
            self._patches.append(p)

        # config.ADMIN_TOKEN
        sys.modules["config"].ADMIN_TOKEN = "test_token"

        # admin_routes._is_admin_token → siempre True; _verify_cookie → None
        sys.modules["admin_routes"]._is_admin_token = lambda tk: True
        sys.modules["admin_routes"]._verify_cookie = lambda ck: None

        # alma_scope.resolve → token de admin sin scope
        sys.modules["alma_scope"].resolve = lambda req, tok, ck, mod: ("test_token", None)

        # prof_canon.canonical_name → pasa el nombre tal cual
        sys.modules["prof_canon"].canonical_name = lambda id_p, name: name

        # pagos_routes.ensure_pagos_table → no-op (tabla ya existe en in-memory)
        sys.modules["pagos_routes"].ensure_pagos_table = lambda: None

        # Inyectar session.db apuntando a nuestra DB in-memory
        self._fake_db = _FakeDB(self._conn)

        # Remover abonos_routes de sys.modules para re-importar con stubs frescos
        for k in list(sys.modules):
            if "abonos_routes" in k:
                del sys.modules[k]

        # Crear módulo session fake en sys.modules
        session_mod = types.ModuleType("session")
        session_mod.db = self._fake_db
        sys.modules["session"] = session_mod

        # Importar el módulo a testear
        import abonos_routes as ar
        self._ar = ar

    def tearDown(self):
        for p in self._patches:
            p.stop()
        # Purgar MagicMocks de sys.modules para no contaminar otras suites
        for k in list(sys.modules):
            if isinstance(sys.modules.get(k), MagicMock):
                sys.modules.pop(k, None)
        # Remover abonos_routes para re-importar limpio en el próximo test
        for k in list(sys.modules):
            if "abonos_routes" in k:
                del sys.modules[k]
        self._conn.close()

    # ── Helpers internos ──────────────────────────────────────────────────────

    def _fake_request(self, body: dict):
        """Crea un Request fake con el body dado."""
        req = MagicMock()
        req.headers = {}

        async def _json():
            return body
        req.json = _json
        return req

    def _abono_row(self, abono_id: int) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM abonos_cmc WHERE id=?", (abono_id,)
        ).fetchone()
        return dict(row) if row else None

    def _pago_row(self, pago_id: int) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM pagos_cmc WHERE id=?", (pago_id,)
        ).fetchone()
        return dict(row) if row else None

    def _pagos_con_origen(self, origen: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM pagos_cmc WHERE origen=?", (origen,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Test 1: crear abono → pago vinculado ─────────────────────────────────

    def test_01_crear_abono_crea_pago_vinculado(self):
        """POST /alma/api/abonos → pago en pagos_cmc con copago=monto_abono y origen='abono'."""
        ar = self._ar
        ar.ensure_abonos_table = lambda: None  # tabla ya existe

        req = self._fake_request({
            "paciente_nombre": "Juan Perez",
            "rut": "12.345.678-9",
            "area": "Psiquiatría",
            "precio_total": 60000,
            "monto_abono": 60000,
            "metodo_pago": "transferencia",
            "folio": "F001",
        })

        result = _run(ar.post_abono(request=req, token="test_token", cmc_session=None))

        self.assertTrue(result["ok"])
        abono_id = result["id"]
        pago_id = result["pago_id"]
        self.assertIsNotNone(pago_id, "Debe crearse un pago vinculado")

        ab = self._abono_row(abono_id)
        self.assertEqual(ab["pago_id"], pago_id)
        self.assertEqual(ab["estado"], "pendiente")
        self.assertEqual(ab["saldo"], 0)  # precio==abono → saldo=0

        pg = self._pago_row(pago_id)
        self.assertIsNotNone(pg)
        self.assertEqual(pg["copago"], 60000, "copago debe ser monto_abono, no precio_total")
        self.assertEqual(pg["origen"], "abono")
        self.assertEqual(pg["paciente_nombre"], "Juan Perez")
        self.assertEqual(pg["metodo_pago"], "transferencia")

    def test_01b_abono_sin_monto_no_crea_pago(self):
        """Si monto_abono=0 no se debe crear pago vinculado."""
        ar = self._ar
        ar.ensure_abonos_table = lambda: None

        req = self._fake_request({
            "paciente_nombre": "Ana Lopez",
            "area": "Nutrición",
            "precio_total": 20000,
            "monto_abono": 0,
        })

        result = _run(ar.post_abono(request=req, token="test_token", cmc_session=None))

        self.assertTrue(result["ok"])
        self.assertIsNone(result["pago_id"], "monto_abono=0 no debe crear pago")
        pagos = self._pagos_con_origen("abono")
        self.assertEqual(len(pagos), 0)

    def test_01c_metodo_imed_se_convierte_a_transferencia(self):
        """Si metodo_pago='imed' en el abono, el pago vinculado usa 'transferencia'."""
        ar = self._ar
        ar.ensure_abonos_table = lambda: None

        req = self._fake_request({
            "paciente_nombre": "Carlos Muñoz",
            "area": "Fonoaudiología",
            "precio_total": 20000,
            "monto_abono": 10000,
            "metodo_pago": "imed",
        })

        result = _run(ar.post_abono(request=req, token="test_token", cmc_session=None))

        pg = self._pago_row(result["pago_id"])
        self.assertEqual(pg["metodo_pago"], "transferencia",
                         "'imed' debe convertirse a 'transferencia' en pagos_cmc")

    # ── Test 2: editar abono → pago actualizado ───────────────────────────────

    def test_02_editar_abono_actualiza_pago(self):
        """PUT abono con nuevo monto_abono → pago vinculado refleja el cambio."""
        ar = self._ar
        ar.ensure_abonos_table = lambda: None

        # Crear primero
        req_c = self._fake_request({
            "paciente_nombre": "Maria Soto",
            "area": "Psiquiatría",
            "precio_total": 60000,
            "monto_abono": 30000,
            "metodo_pago": "efectivo",
        })
        res_c = _run(ar.post_abono(request=req_c, token="test_token", cmc_session=None))
        abono_id = res_c["id"]
        pago_id = res_c["pago_id"]
        self.assertIsNotNone(pago_id)

        # Editar → cambiar monto_abono a 45000 y metodo a 'debito'
        req_e = self._fake_request({
            "monto_abono": 45000,
            "metodo_pago": "debito",
        })
        res_e = _run(ar.put_abono(abono_id=abono_id, request=req_e,
                                   token="test_token", cmc_session=None))
        self.assertTrue(res_e["ok"])
        self.assertEqual(res_e["saldo"], 15000)  # 60000 - 45000

        pg = self._pago_row(pago_id)
        self.assertEqual(pg["copago"], 45000, "pago vinculado debe reflejar nuevo monto")
        self.assertEqual(pg["metodo_pago"], "debito")

    # ── Test 3: borrar abono → pago eliminado ────────────────────────────────

    def test_03_borrar_abono_elimina_pago_vinculado(self):
        """DELETE abono pendiente → elimina pago vinculado con origen='abono'."""
        ar = self._ar
        ar.ensure_abonos_table = lambda: None

        req_c = self._fake_request({
            "paciente_nombre": "Luis Vera",
            "area": "Nutrición",
            "precio_total": 20000,
            "monto_abono": 10000,
            "metodo_pago": "transferencia",
        })
        res_c = _run(ar.post_abono(request=req_c, token="test_token", cmc_session=None))
        abono_id = res_c["id"]
        pago_id = res_c["pago_id"]
        self.assertIsNotNone(pago_id)

        # Verificar pago existe antes de borrar
        self.assertIsNotNone(self._pago_row(pago_id))

        # Crear request DELETE fake
        req_d = MagicMock()
        req_d.headers = {}

        res_d = _run(ar.delete_abono(abono_id=abono_id, request=req_d,
                                      token="test_token", cmc_session=None))
        self.assertTrue(res_d["ok"])

        # Abono eliminado
        self.assertIsNone(self._abono_row(abono_id))
        # Pago también eliminado
        self.assertIsNone(self._pago_row(pago_id),
                          "El pago vinculado debe eliminarse al borrar el abono")

    def test_03b_borrar_abono_aplicado_no_elimina_pago(self):
        """DELETE abono APLICADO → el pago real de saldo NO debe borrarse."""
        ar = self._ar
        ar.ensure_abonos_table = lambda: None

        # Crear abono
        req_c = self._fake_request({
            "paciente_nombre": "Pedro Diaz",
            "area": "Fonoaudiología",
            "precio_total": 20000,
            "monto_abono": 10000,
        })
        res_c = _run(ar.post_abono(request=req_c, token="test_token", cmc_session=None))
        abono_id = res_c["id"]
        pago_id_abono = res_c["pago_id"]

        # Marcar como aplicado directamente en la DB (simula aplicar_abono)
        self._conn.execute(
            "UPDATE abonos_cmc SET estado='aplicado' WHERE id=?", (abono_id,)
        )
        self._conn.commit()

        req_d = MagicMock()
        req_d.headers = {}

        res_d = _run(ar.delete_abono(abono_id=abono_id, request=req_d,
                                      token="test_token", cmc_session=None))
        self.assertTrue(res_d["ok"])

        # El pago del abono original debe sobrevivir (ya fue a caja)
        if pago_id_abono:
            self.assertIsNotNone(
                self._pago_row(pago_id_abono),
                "El pago de un abono APLICADO no debe borrarse al eliminar el abono"
            )

    # ── Test 4: aplicar abono → pago de saldo sin duplicar ───────────────────

    def test_04_aplicar_abono_crea_pago_saldo_sin_duplicar(self):
        """aplicar_abono → crea pago por SALDO; suma monto_abono+saldo == precio_total."""
        ar = self._ar
        ar.ensure_abonos_table = lambda: None

        # Crear abono parcial (abono=30k, saldo=30k)
        req_c = self._fake_request({
            "paciente_nombre": "Rosa Torres",
            "area": "Fonoaudiología",
            "precio_total": 60000,
            "monto_abono": 30000,
            "metodo_pago": "transferencia",
        })
        res_c = _run(ar.post_abono(request=req_c, token="test_token", cmc_session=None))
        abono_id = res_c["id"]
        pago_id_anticipo = res_c["pago_id"]
        self.assertIsNotNone(pago_id_anticipo)

        # pago del anticipo ya está en pagos_cmc con copago=30000
        pg_anticipo = self._pago_row(pago_id_anticipo)
        self.assertEqual(pg_anticipo["copago"], 30000)

        # Aplicar abono
        req_a = MagicMock()
        req_a.headers = {}

        async def _json_aplicar():
            return {"crear_pago": True, "metodo_saldo": "efectivo"}
        req_a.json = _json_aplicar

        res_a = _run(ar.aplicar_abono(abono_id=abono_id, request=req_a,
                                       token="test_token", cmc_session=None))
        self.assertTrue(res_a["ok"])

        # El abono debe apuntar a un NUEVO pago (el del saldo)
        ab = self._abono_row(abono_id)
        self.assertEqual(ab["estado"], "aplicado")
        pago_id_saldo = ab["pago_id"]
        self.assertIsNotNone(pago_id_saldo)
        # El pago de saldo debe ser DISTINTO al del anticipo
        self.assertNotEqual(pago_id_saldo, pago_id_anticipo,
                            "El pago del saldo debe ser un registro nuevo, no el del anticipo")

        pg_saldo = self._pago_row(pago_id_saldo)
        self.assertIsNotNone(pg_saldo)
        self.assertEqual(pg_saldo["copago"], 30000, "El pago de saldo debe ser el saldo (30000)")

        # Anti-doble-conteo: anticipo + saldo == precio_total
        total_caja = pg_anticipo["copago"] + pg_saldo["copago"]
        self.assertEqual(total_caja, 60000,
                         f"anticipo ({pg_anticipo['copago']}) + saldo ({pg_saldo['copago']}) debe == precio_total (60000)")

        # Cantidad de pagos con origen='abono' debe ser exactamente 1 (solo el anticipo)
        pagos_abono = self._pagos_con_origen("abono")
        self.assertEqual(len(pagos_abono), 1,
                         "Solo el pago del anticipo debe tener origen='abono'")
        self.assertEqual(pagos_abono[0]["copago"], 30000)

    def test_04b_aplicar_abono_ya_aplicado_da_409(self):
        """Aplicar dos veces el mismo abono → 409 Conflict."""
        from fastapi import HTTPException as _HTTP
        ar = self._ar
        ar.ensure_abonos_table = lambda: None

        req_c = self._fake_request({
            "paciente_nombre": "Hugo Salas",
            "area": "Psiquiatría",
            "precio_total": 60000,
            "monto_abono": 60000,
        })
        res_c = _run(ar.post_abono(request=req_c, token="test_token", cmc_session=None))
        abono_id = res_c["id"]

        # Marcar aplicado
        self._conn.execute("UPDATE abonos_cmc SET estado='aplicado' WHERE id=?", (abono_id,))
        self._conn.commit()

        req_a = MagicMock()
        req_a.headers = {}
        async def _j(): return {}
        req_a.json = _j

        with self.assertRaises(_HTTP) as ctx:
            _run(ar.aplicar_abono(abono_id=abono_id, request=req_a,
                                   token="test_token", cmc_session=None))
        self.assertEqual(ctx.exception.status_code, 409)


if __name__ == "__main__":
    unittest.main()
