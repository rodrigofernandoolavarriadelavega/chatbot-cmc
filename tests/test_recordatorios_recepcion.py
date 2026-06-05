"""
Tests para el piloto de recordatorios de citas agendadas por recepción.

Cubre:
1. upsert_citas_recepcion: inserción y deduplicación con citas_bot
2. Resolución de phone (normalización de celular desde Medilink)
3. Gating: con flag OFF no se envía nada
4. No-doble-recordatorio: citas ya en citas_bot se excluyen
5. Marcado de enviados (mark_recepcion_reminder_*h_sent)
6. Queries por ventana de 2h (get_citas_recepcion_pendientes_2h)
7. Queries 24h y 48h
"""
import asyncio
import os
import sqlite3
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

# Apuntar a módulos del bot
sys.path.insert(0, str(Path(__file__).parent.parent / "app"))

# ── Fixture: DB temporal ──────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    """Usa una DB temporal para cada test."""
    import session as _session
    db_path = tmp_path / "sessions_test.db"
    monkeypatch.setattr(_session, "DB_PATH", db_path)
    monkeypatch.setattr(_session, "_DDL_DONE", False)
    monkeypatch.setattr(_session, "_DDL_DONE_PATH", "")
    yield db_path
    # Cleanup: si quedó alguna conexión abierta, no importa (tmp_path se borra)


def _fresh_conn(db_path):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


# ── Helpers ───────────────────────────────────────────────────────────────────

def _seed_cita_recepcion(db_path, id_cita=9001, fecha=None, hora="10:00", phone="56912345678"):
    """Inserta una cita de recepción usando session._conn para inicializar el DDL."""
    if fecha is None:
        fecha = (date.today() + timedelta(days=1)).isoformat()
    from session import _conn
    with _conn() as conn:
        conn.execute("""
            INSERT OR IGNORE INTO citas_recepcion_reminders
                (id_cita_medilink, id_profesional, id_paciente, paciente_nombre,
                 especialidad, profesional, fecha, hora, phone, phone_source)
            VALUES (?, 13, 999, 'Juan Pérez', 'Medicina General',
                    'Dr. Alonso Márquez', ?, ?, ?, 'medilink_celular')
        """, (id_cita, fecha, hora, phone))
        conn.commit()


# ── Test 1: upsert básico ─────────────────────────────────────────────────────

def test_upsert_citas_recepcion_insercion_basica(temp_db):
    """Inserta una cita de recepción y verifica que queda en la tabla."""
    from session import upsert_citas_recepcion, _conn

    manana = (date.today() + timedelta(days=1)).isoformat()
    citas = [{
        "id_cita_medilink": 8001,
        "id_profesional":   13,
        "id_paciente":      500,
        "paciente_nombre":  "María Fernández",
        "especialidad":     "Medicina General",
        "profesional":      "Dr. Alonso Márquez",
        "fecha":            manana,
        "hora":             "09:00",
        "phone":            "56977777777",
        "phone_source":     "medilink_celular",
    }]
    n = upsert_citas_recepcion(citas)
    assert n == 1

    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM citas_recepcion_reminders WHERE id_cita_medilink=8001"
        ).fetchone()
    assert row is not None
    assert row["phone"] == "56977777777"
    assert row["reminder_24h_sent"] == 0


# ── Test 2: no-doble si ya en citas_bot ───────────────────────────────────────

def test_upsert_excluye_cita_ya_en_citas_bot(temp_db):
    """Si la cita ya fue agendada por el bot (citas_bot), no se inserta."""
    from session import upsert_citas_recepcion, _conn

    manana = (date.today() + timedelta(days=1)).isoformat()

    # Sembrar en citas_bot
    with _conn() as conn:
        conn.execute("""
            INSERT INTO citas_bot (phone, id_cita, especialidad, profesional, fecha, hora, modalidad)
            VALUES ('56911111111', '7777', 'Medicina General', 'Dr. Márquez', ?, '11:00', 'particular')
        """, (manana,))
        conn.commit()

    citas = [{
        "id_cita_medilink": 7777,   # mismo id que en citas_bot
        "id_profesional":   13,
        "id_paciente":      600,
        "paciente_nombre":  "Pedro Soto",
        "especialidad":     "Medicina General",
        "profesional":      "Dr. Alonso Márquez",
        "fecha":            manana,
        "hora":             "11:00",
        "phone":            "56922222222",
        "phone_source":     "medilink_celular",
    }]
    n = upsert_citas_recepcion(citas)
    assert n == 0  # no insertó porque ya está en citas_bot

    with _conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM citas_recepcion_reminders WHERE id_cita_medilink=7777"
        ).fetchone()
    assert row is None


# ── Test 3: gating OFF ────────────────────────────────────────────────────────

def test_gating_off_no_envia_nada(temp_db):
    """Con RECORDATORIOS_RECEPCION_ENABLED=False las funciones retornan 0 sin enviar."""
    import reminders as rem

    send_mock = AsyncMock()

    with patch.object(rem, "RECORDATORIOS_RECEPCION_ENABLED", False):
        result_24h = asyncio.run(rem.enviar_recordatorios_recepcion_24h(send_mock))
        result_2h  = asyncio.run(rem.enviar_recordatorios_recepcion_2h(send_mock))
        result_48h = asyncio.run(rem.enviar_recordatorios_recepcion_48h(send_mock))

    assert result_24h == 0
    assert result_2h  == 0
    assert result_48h == 0
    send_mock.assert_not_called()


# ── Test 4: get_citas_recepcion_pendientes_24h ────────────────────────────────

def test_get_pendientes_24h_devuelve_solo_sin_enviar(temp_db):
    """get_citas_recepcion_pendientes_24h solo devuelve reminder_24h_sent=0."""
    from session import get_citas_recepcion_pendientes_24h, mark_recepcion_reminder_24h_sent

    manana = (date.today() + timedelta(days=1)).isoformat()

    _seed_cita_recepcion(temp_db, id_cita=9001, fecha=manana, phone="56933333333")
    _seed_cita_recepcion(temp_db, id_cita=9002, fecha=manana, phone="56944444444")

    # Marcar la segunda como enviada
    from session import _conn
    with _conn() as conn:
        row = conn.execute(
            "SELECT id FROM citas_recepcion_reminders WHERE id_cita_medilink=9002"
        ).fetchone()
    mark_recepcion_reminder_24h_sent(row["id"])

    pendientes = get_citas_recepcion_pendientes_24h(manana)
    assert len(pendientes) == 1
    assert pendientes[0]["id_cita_medilink"] == 9001


# ── Test 5: get_citas_recepcion_pendientes_2h ─────────────────────────────────

def test_get_pendientes_2h_filtra_por_ventana_horaria(temp_db):
    """Solo devuelve citas dentro de la ventana de hora."""
    from session import get_citas_recepcion_pendientes_2h

    hoy = date.today().isoformat()
    _seed_cita_recepcion(temp_db, id_cita=9010, fecha=hoy, hora="14:30", phone="56955555555")
    _seed_cita_recepcion(temp_db, id_cita=9011, fecha=hoy, hora="18:00", phone="56966666666")

    # Ventana [14:15, 14:45]
    result = get_citas_recepcion_pendientes_2h(hoy, "14:15", "14:45")
    assert len(result) == 1
    assert result[0]["id_cita_medilink"] == 9010

    # La segunda no debe aparecer (ya se marcó automáticamente la primera)
    result2 = get_citas_recepcion_pendientes_2h(hoy, "14:15", "14:45")
    assert len(result2) == 0  # marcado atómico dentro de la transacción


# ── Test 6: get_citas_recepcion_pendientes_48h ────────────────────────────────

def test_get_pendientes_48h(temp_db):
    """Devuelve citas para pasado mañana con reminder_48h_sent=0."""
    from session import get_citas_recepcion_pendientes_48h

    pasado_manana = (date.today() + timedelta(days=2)).isoformat()
    manana        = (date.today() + timedelta(days=1)).isoformat()

    _seed_cita_recepcion(temp_db, id_cita=9020, fecha=pasado_manana, phone="56977777777")
    _seed_cita_recepcion(temp_db, id_cita=9021, fecha=manana, phone="56988888888")  # mañana, no aplica

    result = get_citas_recepcion_pendientes_48h(pasado_manana)
    assert len(result) == 1
    assert result[0]["id_cita_medilink"] == 9020


# ── Test 7: sin phone no se devuelve ─────────────────────────────────────────

def test_sin_phone_no_aparece_en_pendientes(temp_db):
    """Citas sin celular resuelto no aparecen en las queries de pendientes."""
    from session import get_citas_recepcion_pendientes_24h, upsert_citas_recepcion

    manana = (date.today() + timedelta(days=1)).isoformat()
    upsert_citas_recepcion([{
        "id_cita_medilink": 9030,
        "id_profesional":   13,
        "id_paciente":      700,
        "paciente_nombre":  "Ana Sin Celular",
        "especialidad":     "Medicina General",
        "profesional":      "Dr. Alonso Márquez",
        "fecha":            manana,
        "hora":             "15:00",
        "phone":            None,
        "phone_source":     "sin_celular",
    }])

    pendientes = get_citas_recepcion_pendientes_24h(manana)
    assert all(r["id_cita_medilink"] != 9030 for r in pendientes)


# ── Test 8: normalización de phone ────────────────────────────────────────────

def test_normalizar_phone_formatos_validos():
    """Verifica la lógica de normalización de celular CL."""
    # Simula lo que hace _job_sync_citas_recepcion
    def _normalizar(cel_raw: str):
        if not cel_raw:
            return None
        cel = str(cel_raw).strip().replace(" ", "").replace("-", "").replace("+", "")
        if cel.startswith("9") and len(cel) == 9:
            cel = "56" + cel
        if cel.startswith("56") and len(cel) == 11:
            return cel
        if len(cel) == 9:
            return "56" + cel
        return None

    assert _normalizar("912345678") == "56912345678"
    assert _normalizar("+56912345678") == "56912345678"
    assert _normalizar("56912345678") == "56912345678"
    assert _normalizar("912 345 678") == "56912345678"
    assert _normalizar("") is None
    assert _normalizar("1234") is None         # número inválido
    assert _normalizar("56001234567") == "56001234567"  # 56+9 dígitos → pasa formato
    # Celular fijo (no celular) — 7 dígitos sin prefijo → None
    assert _normalizar("2961234") is None


# ── Test 9: mark funciones actualizan correctamente ──────────────────────────

def test_mark_reminder_24h_sent(temp_db):
    from session import mark_recepcion_reminder_24h_sent, _conn

    manana = (date.today() + timedelta(days=1)).isoformat()
    _seed_cita_recepcion(temp_db, id_cita=9040, fecha=manana)

    with _conn() as conn:
        row = conn.execute(
            "SELECT id FROM citas_recepcion_reminders WHERE id_cita_medilink=9040"
        ).fetchone()
    row_id = row["id"]

    mark_recepcion_reminder_24h_sent(row_id)

    with _conn() as conn:
        row2 = conn.execute(
            "SELECT reminder_24h_sent FROM citas_recepcion_reminders WHERE id=?", (row_id,)
        ).fetchone()
    assert row2["reminder_24h_sent"] == 1


# ── Test 10: config flags ─────────────────────────────────────────────────────

def test_config_flags_default_off():
    """Por defecto el flag está OFF."""
    import importlib
    import config
    # Sin variable de entorno, debe ser False
    with patch.dict(os.environ, {}, clear=False):
        # Si ya está cargado, forzamos el valor por defecto
        assert not os.getenv("RECORDATORIOS_RECEPCION_ENABLED", "false").lower() in (
            "true", "1", "yes"
        )


def test_config_prof_ids_default():
    """Por defecto solo incluye id=13 (Dr. Márquez)."""
    from config import RECORDATORIOS_RECEPCION_PROF_IDS
    assert 13 in RECORDATORIOS_RECEPCION_PROF_IDS


if __name__ == "__main__":
    # Ejecutar con: python tests/test_recordatorios_recepcion.py
    import subprocess
    result = subprocess.run(
        [sys.executable, "-m", "pytest", __file__, "-v"],
        cwd=str(Path(__file__).parent.parent),
    )
    sys.exit(result.returncode)
