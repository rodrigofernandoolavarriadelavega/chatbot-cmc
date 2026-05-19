"""
Tests unitarios para app/health_report.py.
Usan una DB SQLite en memoria inyectada via monkeypatch.
"""
import json
import sqlite3
import sys
import os
from pathlib import Path
from unittest.mock import patch

# Asegurar que app/ está en el path
APP_DIR = Path(__file__).parent.parent / "app"
sys.path.insert(0, str(APP_DIR))


def _make_db():
    """DB SQLite en memoria con las tablas mínimas necesarias."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE conversation_events (
            id    INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT,
            event TEXT,
            meta  TEXT DEFAULT '{}',
            ts    TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE TABLE message_statuses (
            wamid       TEXT PRIMARY KEY,
            phone       TEXT,
            status      TEXT,
            ts          TEXT DEFAULT (datetime('now')),
            error_code  TEXT,
            error_title TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE fidelizacion_msgs (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            phone      TEXT,
            tipo       TEXT,
            respuesta  TEXT,
            cita_id    TEXT,
            enviado_en TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE citas_bot (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            phone      TEXT,
            fecha      TEXT,
            especialidad TEXT,
            profesional  TEXT,
            id_cita    TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    return conn


# ── Helpers de inserción ──────────────────────────────────────────────────────

def _insert_event(conn, event, meta=None, ts=None):
    conn.execute(
        "INSERT INTO conversation_events (phone, event, meta, ts) VALUES (?,?,?,COALESCE(?,datetime('now')))",
        ("56900000001", event, json.dumps(meta or {}), ts),
    )
    conn.commit()


def _insert_status(conn, wamid, status, error_code=None):
    conn.execute(
        "INSERT INTO message_statuses (wamid, phone, status, error_code) VALUES (?,?,?,?)",
        (wamid, "56900000001", status, error_code),
    )
    conn.commit()


def _insert_fidelizacion(conn, tipo, respuesta=None):
    conn.execute(
        "INSERT INTO fidelizacion_msgs (phone, tipo, respuesta, enviado_en) "
        "VALUES (?,?,?,datetime('now'))",
        ("56900000001", tipo, respuesta),
    )
    conn.commit()


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_section_sexo_mismatch_empty():
    from health_report import _section_sexo_mismatch
    conn = _make_db()
    total, top = _section_sexo_mismatch(conn, "2000-01-01 00:00:00")
    assert total == 0
    assert top == []


def test_section_sexo_mismatch_with_data():
    from health_report import _section_sexo_mismatch
    conn = _make_db()
    _insert_event(conn, "tip_sexo_mismatch", {
        "nombre": "Olga", "rut": "12345678-9",
        "sexo_medilink": "M", "sexo_inferido": "F",
    })
    total, top = _section_sexo_mismatch(conn, "2000-01-01 00:00:00")
    assert total == 1
    assert "12345678-9" in top[0]
    assert "Olga" in top[0]


def test_section_template_skips_breakdown():
    from health_report import _section_template_skips
    conn = _make_db()
    _insert_event(conn, "template_skip_no_aprobado", {"template": "postconsulta_seguimiento"})
    _insert_event(conn, "template_skip_no_aprobado", {"template": "postconsulta_seguimiento"})
    _insert_event(conn, "template_skip_no_consent", {"template": "crosssell_kine"})
    skip_ap, skip_consent = _section_template_skips(conn, "2000-01-01 00:00:00")
    assert skip_ap.get("postconsulta_seguimiento") == 2
    assert skip_consent.get("crosssell_kine") == 1


def test_section_proactive_guards():
    from health_report import _section_proactive_guards
    conn = _make_db()
    _insert_event(conn, "proactive_skip_blocklist")
    _insert_event(conn, "proactive_skip_blocklist")
    _insert_event(conn, "proactive_skip_blocklist_late")
    blocklist, late = _section_proactive_guards(conn, "2000-01-01 00:00:00")
    assert blocklist == 2
    assert late == 1


def test_section_meta_errors_categorized():
    from health_report import _section_meta_errors
    conn = _make_db()
    _insert_status(conn, "wamid1", "failed", "131047")
    _insert_status(conn, "wamid2", "failed", "131047")
    _insert_status(conn, "wamid3", "failed", "131042")
    _insert_status(conn, "wamid4", "failed", "999")   # otros
    result = _section_meta_errors(conn, "2000-01-01 00:00:00")
    assert result.get("131047") == 2
    assert result.get("131042") == 1
    assert result.get("otros") == 1


def test_section_capi():
    from health_report import _section_capi
    conn = _make_db()
    _insert_event(conn, "capi_send_ok")
    _insert_event(conn, "capi_send_ok")
    _insert_event(conn, "capi_send_failed")
    ok, failed = _section_capi(conn, "2000-01-01 00:00:00")
    assert ok == 2
    assert failed == 1


def test_section_emergencias_breakdown():
    from health_report import _section_emergencias
    conn = _make_db()
    _insert_event(conn, "emergencia_detectada", {"texto": "me exploto la cabeza de repente"})
    _insert_event(conn, "emergencia_detectada", {"texto": "no puedo mover el brazo"})  # → ACV/FAST
    _insert_event(conn, "emergencia_detectada", {"texto": "me duele un diente"})
    total, bd = _section_emergencias(conn, "2000-01-01 00:00:00")
    assert total == 3
    assert bd["Cefalea subita"] == 1
    assert bd["ACV/FAST"] == 1   # "no puedo mover" matchea ACV keyword
    assert bd["Dental (FP)"] == 1


def test_section_citas():
    from health_report import _section_citas
    conn = _make_db()
    _insert_event(conn, "cita_creada")
    _insert_event(conn, "cita_creada")
    _insert_event(conn, "cita_cancelada")
    creadas, canceladas = _section_citas(conn, "2000-01-01 00:00:00")
    assert creadas == 2
    assert canceladas == 1


def test_section_postconsulta():
    from health_report import _section_postconsulta
    conn = _make_db()
    _insert_fidelizacion(conn, "postconsulta", "mejor")
    _insert_fidelizacion(conn, "postconsulta", "igual")
    _insert_fidelizacion(conn, "postconsulta", None)
    env, resp, bd = _section_postconsulta(conn, "2000-01-01 00:00:00")
    assert env == 3
    assert resp == 2
    assert bd["mejor"] == 1
    assert bd["igual"] == 1
    assert bd["peor"] == 0


def test_section_winback():
    from health_report import _section_winback
    conn = _make_db()
    _insert_fidelizacion(conn, "winback")
    _insert_fidelizacion(conn, "winback")
    env, citas = _section_winback(conn, "2000-01-01 00:00:00")
    assert env == 2
    assert citas == 0  # no hay citas_creadas post-winback en este test


def test_build_weekly_health_report_runs():
    """Smoke test: build_weekly_health_report() retorna un string no vacío sin errores."""
    import health_report as hr
    # Parchear _conn para devolver DB en memoria
    mem_conn = _make_db()

    class _FakeContextConn:
        def __enter__(self):
            return mem_conn
        def __exit__(self, *a):
            pass

    with patch.object(hr, "_conn", return_value=_FakeContextConn()):
        result = hr.build_weekly_health_report()

    assert isinstance(result, str)
    assert len(result) > 50
    assert "Reporte salud bot CMC" in result
    assert len(result) <= 1500


def test_build_weekly_health_report_max_length():
    """El reporte nunca supera 1500 chars."""
    import health_report as hr
    mem_conn = _make_db()
    # Insertar muchos mismatches para forzar texto largo
    for i in range(50):
        _insert_event(mem_conn, "tip_sexo_mismatch", {
            "nombre": f"NombreMuyLargo{i}",
            "rut": f"1234567{i}-9",
            "sexo_medilink": "M",
            "sexo_inferido": "F",
        })

    class _FakeContextConn:
        def __enter__(self):
            return mem_conn
        def __exit__(self, *a):
            pass

    with patch.object(hr, "_conn", return_value=_FakeContextConn()):
        result = hr.build_weekly_health_report()

    assert len(result) <= 1500


if __name__ == "__main__":
    tests = [
        test_section_sexo_mismatch_empty,
        test_section_sexo_mismatch_with_data,
        test_section_template_skips_breakdown,
        test_section_proactive_guards,
        test_section_meta_errors_categorized,
        test_section_capi,
        test_section_emergencias_breakdown,
        test_section_citas,
        test_section_postconsulta,
        test_section_winback,
        test_build_weekly_health_report_runs,
        test_build_weekly_health_report_max_length,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except Exception as e:
            print(f"  FAIL  {t.__name__}: {e}")
            failed += 1
    print(f"\n{len(tests) - failed}/{len(tests)} tests pasaron")
    sys.exit(failed)
