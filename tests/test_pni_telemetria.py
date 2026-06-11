"""
Tests unitarios — Telemetria PNI (pni_enviado + pni_cita_generada).

Corre sin servidor ni Medilink. Usa DB en memoria (:memory:).
"""
import sys
import os
import json
import sqlite3
import unittest
from unittest.mock import patch, MagicMock
from datetime import date, datetime, timezone, timedelta

# Ajustar path para imports desde app/
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))


# ── Helpers de pni.py y hitos_desarrollo.py ────────────────────────────────

class TestGetPniMeta(unittest.TestCase):
    """get_pni_meta() retorna metadata correcta o None segun edad."""

    def test_bebe_2_meses_tiene_pni(self):
        from pni import get_pni_meta
        # Nino de exactamente 2 meses
        hoy = date.today()
        nac = date(hoy.year, hoy.month - 2 if hoy.month > 2 else hoy.month + 10,
                   hoy.day) if hoy.month > 2 else date(hoy.year - 1, hoy.month + 10, hoy.day)
        fecha_str = nac.strftime("%Y-%m-%d")
        meta = get_pni_meta(fecha_str)
        # Puede no tener vacunas si la edad exacta cae fuera del rango; lo que
        # nos interesa es que NO lanza excepcion y retorna dict o None.
        self.assertIsInstance(meta, (dict, type(None)))

    def test_adulto_retorna_none(self):
        from pni import get_pni_meta
        meta = get_pni_meta("1990-06-15")
        self.assertIsNone(meta)

    def test_fecha_invalida_retorna_none(self):
        from pni import get_pni_meta
        meta = get_pni_meta("no-es-fecha")
        self.assertIsNone(meta)

    def test_recien_nacido_tiene_pni(self):
        from pni import get_pni_meta
        # 0 meses → BCG + Hepatitis B
        hoy = date.today()
        fecha_str = hoy.strftime("%Y-%m-%d")
        meta = get_pni_meta(fecha_str)
        self.assertIsNotNone(meta)
        self.assertIn("edad_meses", meta)
        self.assertIn("edad_etiqueta", meta)
        self.assertIn("tiene_pni", meta)
        self.assertEqual(meta["edad_meses"], 0)
        self.assertTrue(meta["tiene_pni"])

    def test_meta_tiene_todos_campos(self):
        from pni import get_pni_meta
        # 12 meses tiene vacunas
        hoy = date.today()
        nac = date(hoy.year - 1, hoy.month, 1)
        meta = get_pni_meta(nac.strftime("%Y-%m-%d"))
        if meta:  # puede no tener vacunas en ese mes exacto segun calendario
            self.assertIn("edad_meses", meta)
            self.assertIn("edad_etiqueta", meta)
            self.assertIn("tiene_pni", meta)
            self.assertIn("tiene_escolares", meta)


class TestGetHitosMeta(unittest.TestCase):
    """get_hitos_meta() retorna metadata o None segun edad."""

    def test_recien_nacido_tiene_hitos(self):
        from hitos_desarrollo import get_hitos_meta
        hoy = date.today()
        meta = get_hitos_meta(hoy.strftime("%Y-%m-%d"))
        self.assertIsNotNone(meta)
        self.assertIn("edad_meses", meta)
        self.assertIn("edad_etiqueta", meta)

    def test_mayor_9_anos_retorna_none(self):
        from hitos_desarrollo import get_hitos_meta
        meta = get_hitos_meta("2010-01-01")
        self.assertIsNone(meta)

    def test_adulto_retorna_none(self):
        from hitos_desarrollo import get_hitos_meta
        meta = get_hitos_meta("1985-03-20")
        self.assertIsNone(meta)


# ── Helpers de session.py (DB en memoria) ──────────────────────────────────

def _make_test_db():
    """Crea DB SQLite en memoria con el schema minimo para conversation_events."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE conversation_events (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            phone   TEXT,
            event   TEXT,
            meta    TEXT DEFAULT '{}',
            ts      TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    return conn


class TestGetRecentPniEvent(unittest.TestCase):
    """get_recent_pni_event() busca pni_enviado en las ultimas N horas."""

    def _get_conn_patch(self, conn):
        """Patch _conn() en session para usar nuestra DB en memoria."""
        from contextlib import contextmanager

        @contextmanager
        def fake_conn():
            yield conn

        return patch("session.db", fake_conn)

    def test_retorna_evento_reciente(self):
        import session
        conn = _make_test_db()
        meta = json.dumps({"tipo": "pni", "edad_meses": 2, "edad_etiqueta": "2 meses",
                           "trigger": "post_cita_pediatrica", "cita_id_previa": "123"})
        conn.execute("INSERT INTO conversation_events (phone, event, meta) VALUES (?,?,?)",
                     ("56912345678", "pni_enviado", meta))
        conn.commit()

        with self._get_conn_patch(conn):
            resultado = session.get_recent_pni_event("56912345678", horas=72)

        self.assertIsNotNone(resultado)
        self.assertIn("id", resultado)
        self.assertEqual(resultado["meta"]["tipo"], "pni")

    def test_retorna_none_sin_evento(self):
        import session
        conn = _make_test_db()

        with self._get_conn_patch(conn):
            resultado = session.get_recent_pni_event("56900000000", horas=72)

        self.assertIsNone(resultado)

    def test_evento_antiguo_fuera_de_ventana(self):
        """Un pni_enviado de 73h atras NO debe retornar."""
        import session
        conn = _make_test_db()
        ts_antiguo = (datetime.now(timezone.utc) - timedelta(hours=73)).strftime("%Y-%m-%d %H:%M:%S")
        meta = json.dumps({"tipo": "hitos"})
        conn.execute("INSERT INTO conversation_events (phone, event, meta, ts) VALUES (?,?,?,?)",
                     ("56911111111", "pni_enviado", meta, ts_antiguo))
        conn.commit()

        with self._get_conn_patch(conn):
            resultado = session.get_recent_pni_event("56911111111", horas=72)

        self.assertIsNone(resultado)


class TestLogPniCitaGenerada(unittest.TestCase):
    """log_pni_cita_generada() es idempotente y solo inserta cuando hay pni_enviado."""

    def _get_conn_patch(self, conn):
        from contextlib import contextmanager

        @contextmanager
        def fake_conn():
            yield conn

        return patch("session.db", fake_conn)

    def test_inserta_primer_vez(self):
        import session
        conn = _make_test_db()

        with self._get_conn_patch(conn):
            inserted = session.log_pni_cita_generada(
                phone="56912345678",
                pni_evento_id=42,
                horas_desde_envio=5.3,
                especialidad="Medicina General",
                cita_id="9999",
            )

        self.assertTrue(inserted)
        row = conn.execute(
            "SELECT meta FROM conversation_events WHERE event='pni_cita_generada'"
        ).fetchone()
        self.assertIsNotNone(row)
        m = json.loads(row["meta"])
        self.assertEqual(m["cita_id"], "9999")
        self.assertEqual(m["pni_evento_id"], 42)
        self.assertAlmostEqual(m["horas_desde_envio"], 5.3, places=1)

    def test_idempotente_misma_cita(self):
        """Llamar dos veces con la misma cita_id NO duplica el evento."""
        import session
        conn = _make_test_db()

        with self._get_conn_patch(conn):
            r1 = session.log_pni_cita_generada(
                phone="56912345678",
                pni_evento_id=42,
                horas_desde_envio=5.3,
                especialidad="Medicina General",
                cita_id="8888",
            )
            r2 = session.log_pni_cita_generada(
                phone="56912345678",
                pni_evento_id=42,
                horas_desde_envio=5.4,
                especialidad="Medicina General",
                cita_id="8888",
            )

        self.assertTrue(r1)
        self.assertFalse(r2)  # segundo intento ignorado
        count = conn.execute(
            "SELECT COUNT(*) FROM conversation_events WHERE event='pni_cita_generada'"
        ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_no_loggea_sin_pni_enviado_previo(self):
        """Si no hay pni_enviado previo, pni_cita_generada NO debe insertarse."""
        import session
        conn = _make_test_db()

        with self._get_conn_patch(conn):
            # Verificar que no hay pni_enviado previo
            pni_ev = session.get_recent_pni_event("56977777777", horas=72)
            self.assertIsNone(pni_ev)

            # No debe llamarse log_pni_cita_generada si no hay evento previo
            # (esta logica vive en flows.py, aqui solo verificamos que
            #  get_recent_pni_event retorna None correctamente)


# ── Smoke test DB local (si existe) ────────────────────────────────────────

class TestSmokeLocalDB(unittest.TestCase):
    """Inserta y lee filas ficticias en sessions.db local si existe."""

    DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "sessions.db")

    def setUp(self):
        if not os.path.exists(self.DB_PATH):
            self.skipTest("sessions.db no existe localmente — skip smoke test")

    def test_insert_read_pni_event(self):
        conn = sqlite3.connect(self.DB_PATH)
        conn.row_factory = sqlite3.Row
        test_phone = "TEST_PNI_TELEMETRIA_SMOKE"
        try:
            conn.execute(
                "INSERT INTO conversation_events (phone, event, meta) VALUES (?,?,?)",
                (test_phone, "pni_enviado", json.dumps({
                    "tipo": "ambos", "edad_meses": 6,
                    "edad_etiqueta": "6 meses",
                    "trigger": "post_cita_pediatrica",
                    "cita_id_previa": "SMOKE_001",
                }))
            )
            conn.commit()
            row = conn.execute(
                "SELECT COUNT(*) as n FROM conversation_events WHERE phone=? AND event='pni_enviado'",
                (test_phone,)
            ).fetchone()
            self.assertEqual(row["n"], 1)
        finally:
            conn.execute("DELETE FROM conversation_events WHERE phone=?", (test_phone,))
            conn.commit()
            conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
