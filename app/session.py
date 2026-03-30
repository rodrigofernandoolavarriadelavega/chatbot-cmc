"""
Gestión de sesiones por número de WhatsApp usando SQLite.
Cada sesión guarda: estado actual + datos del flujo en curso.
"""
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "sessions.db"
SESSION_TIMEOUT_MIN = 30  # minutos sin actividad → volver a IDLE


def _conn():
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            phone       TEXT PRIMARY KEY,
            state       TEXT DEFAULT 'IDLE',
            data        TEXT DEFAULT '{}',
            updated_at  TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS processed_msgs (
            msg_id      TEXT PRIMARY KEY,
            created_at  TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS contact_tags (
            phone       TEXT,
            tag         TEXT,
            ts          TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (phone, tag)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS citas_bot (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            phone           TEXT,
            id_cita         TEXT,
            especialidad    TEXT,
            profesional     TEXT,
            fecha           TEXT,
            hora            TEXT,
            modalidad       TEXT,
            reminder_sent   INTEGER DEFAULT 0,
            created_at      TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS conversation_events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            phone       TEXT,
            event       TEXT,
            meta        TEXT DEFAULT '{}',
            ts          TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS contact_profiles (
            phone       TEXT PRIMARY KEY,
            rut         TEXT,
            nombre      TEXT,
            updated_at  TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    return conn


def is_duplicate(msg_id: str) -> bool:
    """Retorna True si el msg_id ya fue procesado (idempotencia ante reenvíos de Meta)."""
    with _conn() as conn:
        exists = conn.execute(
            "SELECT 1 FROM processed_msgs WHERE msg_id=?", (msg_id,)
        ).fetchone()
        if exists:
            return True
        conn.execute("INSERT INTO processed_msgs (msg_id) VALUES (?)", (msg_id,))
        # Limpiar entradas de más de 1 hora para no crecer indefinidamente
        conn.execute("DELETE FROM processed_msgs WHERE created_at < datetime('now', '-1 hour')")
        conn.commit()
        return False


def get_session(phone: str) -> dict:
    """Devuelve la sesión actual del número. Si expiró o no existe, retorna sesión limpia."""
    with _conn() as conn:
        row = conn.execute("SELECT * FROM sessions WHERE phone=?", (phone,)).fetchone()
        if not row:
            return {"state": "IDLE", "data": {}}
        # Verificar timeout
        updated = datetime.fromisoformat(row["updated_at"])
        if datetime.now(timezone.utc) - updated.replace(tzinfo=timezone.utc) > timedelta(minutes=SESSION_TIMEOUT_MIN):
            _reset(conn, phone)
            return {"state": "IDLE", "data": {}}
        return {"state": row["state"], "data": json.loads(row["data"])}


def save_session(phone: str, state: str, data: dict):
    """Guarda o actualiza la sesión."""
    with _conn() as conn:
        conn.execute("""
            INSERT INTO sessions (phone, state, data, updated_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(phone) DO UPDATE SET
                state=excluded.state,
                data=excluded.data,
                updated_at=excluded.updated_at
        """, (phone, state, json.dumps(data, ensure_ascii=False)))
        conn.commit()


def reset_session(phone: str):
    """Reinicia la sesión a IDLE."""
    with _conn() as conn:
        _reset(conn, phone)


def _reset(conn, phone: str):
    conn.execute("""
        INSERT INTO sessions (phone, state, data, updated_at)
        VALUES (?, 'IDLE', '{}', datetime('now'))
        ON CONFLICT(phone) DO UPDATE SET state='IDLE', data='{}', updated_at=datetime('now')
    """, (phone,))
    conn.commit()


# ── Contact tags ──────────────────────────────────────────────────────────────

def save_tag(phone: str, tag: str):
    """Agrega un tag al contacto (idempotente). Ej: 'cita-kinesiología'."""
    with _conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO contact_tags (phone, tag) VALUES (?, ?)",
            (phone, tag)
        )
        conn.commit()


def get_tags(phone: str) -> list[str]:
    """Devuelve todos los tags de un contacto."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT tag FROM contact_tags WHERE phone=? ORDER BY ts", (phone,)
        ).fetchall()
        return [r["tag"] for r in rows]


# ── Citas creadas por el bot ──────────────────────────────────────────────────

def save_cita_bot(phone: str, id_cita: str, especialidad: str,
                  profesional: str, fecha: str, hora: str, modalidad: str):
    """Registra una cita creada por el bot para tracking y recordatorios."""
    with _conn() as conn:
        conn.execute(
            """INSERT INTO citas_bot (phone, id_cita, especialidad, profesional, fecha, hora, modalidad)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (phone, id_cita, especialidad, profesional, fecha, hora, modalidad)
        )
        conn.commit()


def get_citas_bot_pendientes(fecha: str) -> list[dict]:
    """Devuelve citas del bot para una fecha dada donde aún no se envió recordatorio."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM citas_bot WHERE fecha=? AND reminder_sent=0", (fecha,)
        ).fetchall()
        return [dict(r) for r in rows]


# ── Perfiles de paciente ──────────────────────────────────────────────────────

def save_profile(phone: str, rut: str, nombre: str):
    """Guarda o actualiza el perfil del paciente asociado al número."""
    with _conn() as conn:
        conn.execute("""
            INSERT INTO contact_profiles (phone, rut, nombre, updated_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(phone) DO UPDATE SET
                rut=excluded.rut, nombre=excluded.nombre, updated_at=excluded.updated_at
        """, (phone, rut, nombre))
        conn.commit()


def get_profile(phone: str) -> dict | None:
    """Retorna el perfil del paciente si existe (rut, nombre)."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT rut, nombre FROM contact_profiles WHERE phone=?", (phone,)
        ).fetchone()
        return dict(row) if row else None


def mark_reminder_sent(cita_id: int):
    """Marca una cita como recordatorio enviado."""
    with _conn() as conn:
        conn.execute("UPDATE citas_bot SET reminder_sent=1 WHERE id=?", (cita_id,))
        conn.commit()


# ── Métricas de conversación ──────────────────────────────────────────────────

def log_event(phone: str, event: str, meta: dict = None):
    """
    Registra un evento de conversación.
    Eventos sugeridos: intent_detectado, cita_creada, cita_cancelada,
    sin_disponibilidad, derivado_humano, paciente_nuevo, error_bot
    """
    import json as _json
    with _conn() as conn:
        conn.execute(
            "INSERT INTO conversation_events (phone, event, meta) VALUES (?, ?, ?)",
            (phone, event, _json.dumps(meta or {}, ensure_ascii=False))
        )
        conn.commit()


def get_metricas(dias: int = 30) -> dict:
    """Resumen de métricas de los últimos N días."""
    with _conn() as conn:
        total_conv = conn.execute(
            "SELECT COUNT(DISTINCT phone) FROM conversation_events "
            "WHERE ts >= datetime('now', ?)", (f"-{dias} days",)
        ).fetchone()[0]

        rows = conn.execute(
            "SELECT event, COUNT(*) as cnt FROM conversation_events "
            "WHERE ts >= datetime('now', ?) GROUP BY event ORDER BY cnt DESC",
            (f"-{dias} days",)
        ).fetchall()
        por_evento = {r["event"]: r["cnt"] for r in rows}

        citas = por_evento.get("cita_creada", 0)
        intentos = por_evento.get("intent_agendar", 0)
        tasa_conversion = round(citas / intentos * 100, 1) if intentos else 0

        return {
            "periodo_dias": dias,
            "conversaciones_unicas": total_conv,
            "tasa_conversion_agendamiento": f"{tasa_conversion}%",
            "eventos": por_evento,
        }
