"""
Tests para F043 (waitlist digest crasheaba por sqlite3.Row.get()) y
F042 (idempotencia de notificaciones a profesionales usaba columna 'data' en vez de 'meta').
"""
import sqlite3
import pytest


# ── F043: sqlite3.Row no tiene .get() ────────────────────────────────────────

def _make_waitlist_rows():
    """Crea rows sqlite3 simulando la tabla waitlist."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE waitlist (id INTEGER, phone TEXT, nombre TEXT, "
        "especialidad TEXT, created_at TEXT)"
    )
    conn.execute(
        "INSERT INTO waitlist VALUES (1, '56912345678', 'Juan Pérez', "
        "'Kinesiología', '2026-05-20 10:00:00')"
    )
    conn.execute(
        "INSERT INTO waitlist VALUES (2, '56987654321', NULL, "
        "'Medicina General', '2026-05-15 08:00:00')"
    )
    return conn.execute("SELECT id, phone, nombre, especialidad, created_at FROM waitlist").fetchall()


def test_row_no_tiene_get():
    """sqlite3.Row no tiene .get() — confirma que el bug original existía."""
    rows = _make_waitlist_rows()
    with pytest.raises(AttributeError):
        _ = rows[0].get("nombre")


def test_fix_acceso_por_subscript():
    """El fix accede por subscript directo, que sí funciona y retorna None cuando es NULL."""
    rows = _make_waitlist_rows()
    # Fila con nombre
    r0 = rows[0]
    nombre_raw = r0["nombre"]
    nombre_wd = (nombre_raw or "").split()[0] if nombre_raw else "Paciente"
    assert nombre_wd == "Juan"

    # Fila con nombre NULL
    r1 = rows[1]
    nombre_raw = r1["nombre"]
    nombre_wd = (nombre_raw or "").split()[0] if nombre_raw else "Paciente"
    assert nombre_wd == "Paciente"

    # Especialidad
    esp_wd = r0["especialidad"] or "?"
    assert esp_wd == "Kinesiología"

    # Set comprehension que también usaba .get() (en el log_event)
    especialidades = list({r["especialidad"] for r in rows})
    assert set(especialidades) == {"Kinesiología", "Medicina General"}


# ── F042: columna 'data' vs 'meta' en conversation_events ────────────────────

def _make_events_db():
    """Crea DB con el schema real de conversation_events (columna 'meta')."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE conversation_events "
        "(id INTEGER PRIMARY KEY, phone TEXT, event TEXT, meta TEXT, ts TEXT)"
    )
    conn.execute(
        "INSERT INTO conversation_events (phone, event, meta, ts) VALUES "
        "(?, 'prof_notif_sent', ?, datetime('now'))",
        ("56911111111", '{"event_type":"resumen_precita","dedup_key":"cita_42"}')
    )
    conn.commit()
    return conn


def test_columna_data_falla():
    """Consultar columna 'data' (inexistente) levanta OperationalError."""
    conn = _make_events_db()
    with pytest.raises(Exception):  # sqlite3.OperationalError
        conn.execute(
            "SELECT 1 FROM conversation_events WHERE phone=? AND data LIKE ?",
            ("56911111111", '%resumen_precita%')
        ).fetchone()


def test_fix_columna_meta_matchea():
    """Usando columna 'meta' el match funciona correctamente."""
    conn = _make_events_db()
    r = conn.execute(
        "SELECT 1 FROM conversation_events "
        "WHERE phone=? AND event='prof_notif_sent' "
        "AND meta LIKE ? "
        "AND ts > datetime('now', '-7 days') LIMIT 1",
        ("56911111111", '%"event_type":"resumen_precita"%"dedup_key":"cita_42"%')
    ).fetchone()
    assert r is not None, "Debería encontrar el registro con columna meta"


def test_fix_columna_meta_no_matchea_otro():
    """No hay falso positivo para otro dedup_key."""
    conn = _make_events_db()
    r = conn.execute(
        "SELECT 1 FROM conversation_events "
        "WHERE phone=? AND event='prof_notif_sent' "
        "AND meta LIKE ? "
        "AND ts > datetime('now', '-7 days') LIMIT 1",
        ("56911111111", '%"event_type":"resumen_precita"%"dedup_key":"cita_99"%')
    ).fetchone()
    assert r is None, "No debería encontrar match para dedup_key diferente"
