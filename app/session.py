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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            phone       TEXT NOT NULL,
            direction   TEXT NOT NULL,
            text        TEXT,
            state       TEXT DEFAULT 'IDLE',
            ts          TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_msg_phone ON messages(phone)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_msg_ts    ON messages(ts)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS fidelizacion_msgs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            phone       TEXT,
            tipo        TEXT,
            cita_id     TEXT,
            enviado_en  TEXT DEFAULT (datetime('now')),
            respuesta   TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fidel_phone ON fidelizacion_msgs(phone, tipo)")
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


def delete_tag(phone: str, tag: str):
    """Elimina un tag de un contacto."""
    with _conn() as conn:
        conn.execute("DELETE FROM contact_tags WHERE phone=? AND tag=?", (phone, tag))
        conn.commit()


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


def log_message(phone: str, direction: str, text: str, state: str = "IDLE"):
    """Registra un mensaje entrante ('in') o saliente ('out') en el historial."""
    with _conn() as conn:
        conn.execute(
            "INSERT INTO messages (phone, direction, text, state) VALUES (?, ?, ?, ?)",
            (phone, direction, str(text)[:2000], state)
        )
        conn.commit()


def get_messages(phone: str, limit: int = 100) -> list[dict]:
    """Retorna el historial de mensajes de un número (más reciente al final)."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, phone, direction, text, state, ts FROM messages "
            "WHERE phone=? ORDER BY id ASC LIMIT ?",
            (phone, limit)
        ).fetchall()
        return [dict(r) for r in rows]


def get_conversations(limit: int = 200) -> list[dict]:
    """Lista todas las conversaciones con último mensaje y estado actual."""
    with _conn() as conn:
        rows = conn.execute("""
            SELECT
                s.phone,
                s.state,
                s.data,
                s.updated_at,
                m.text        AS last_text,
                m.direction   AS last_dir,
                m.ts          AS last_ts,
                p.nombre,
                p.rut,
                (SELECT COUNT(*) FROM messages WHERE phone = s.phone) AS msg_count
            FROM sessions s
            LEFT JOIN messages m ON m.id = (
                SELECT id FROM messages WHERE phone = s.phone ORDER BY id DESC LIMIT 1
            )
            LEFT JOIN contact_profiles p ON p.phone = s.phone
            ORDER BY s.updated_at DESC
            LIMIT ?
        """, (limit,)).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            try:
                import json as _json
                session_data = _json.loads(d.pop("data", "{}") or "{}")
                d["msgs_sin_respuesta"] = session_data.get("msgs_sin_respuesta", 0)
                slot = session_data.get("slot_elegido") or {}
                d["flow_data"] = {
                    "especialidad":      session_data.get("especialidad", ""),
                    "profesional":       session_data.get("profesional_nombre", ""),
                    "fecha_display":     slot.get("fecha_display", "") if isinstance(slot, dict) else "",
                    "hora_inicio":       slot.get("hora_inicio", "")   if isinstance(slot, dict) else "",
                    "modalidad":         session_data.get("modalidad", ""),
                    "prev_state":        session_data.get("handoff_reason", ""),
                }
            except Exception:
                d.pop("data", None)
                d["msgs_sin_respuesta"] = 0
                d["flow_data"] = {}
            result.append(d)
        return result


def get_sesiones_abandonadas() -> list[dict]:
    """Retorna sesiones activas sin actividad entre 10 y 60 minutos (candidatas a reenganche)."""
    estados = ("WAIT_SLOT", "WAIT_MODALIDAD", "WAIT_RUT_AGENDAR", "WAIT_NOMBRE_NUEVO")
    placeholders = ",".join("?" * len(estados))
    with _conn() as conn:
        rows = conn.execute(f"""
            SELECT phone, state, data FROM sessions
            WHERE state IN ({placeholders})
            AND updated_at < datetime('now', '-10 minutes')
            AND updated_at > datetime('now', '-60 minutes')
        """, estados).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            try:
                d["data"] = json.loads(d.get("data") or "{}")
            except Exception:
                d["data"] = {}
            if not d["data"].get("reenganche_sent"):
                result.append(d)
        return result


# ── Fidelización ──────────────────────────────────────────────────────────────

def get_citas_para_seguimiento(fecha_ayer: str) -> list[dict]:
    """Citas del bot de ayer que aún no tienen seguimiento post-consulta enviado."""
    with _conn() as conn:
        rows = conn.execute("""
            SELECT cb.id, cb.phone, cb.id_cita, cb.especialidad, cb.profesional, cb.fecha, cb.hora,
                   p.nombre
            FROM citas_bot cb
            LEFT JOIN contact_profiles p ON p.phone = cb.phone
            WHERE cb.fecha = ?
            AND NOT EXISTS (
                SELECT 1 FROM fidelizacion_msgs f
                WHERE f.phone = cb.phone AND f.tipo = 'postconsulta' AND f.cita_id = cb.id_cita
            )
        """, (fecha_ayer,)).fetchall()
        return [dict(r) for r in rows]


def get_pacientes_inactivos(dias_min: int = 30, dias_max: int = 90) -> list[dict]:
    """Pacientes cuya última cita fue entre dias_min y dias_max días atrás,
    sin mensaje de reactivación enviado en los últimos 60 días."""
    with _conn() as conn:
        rows = conn.execute("""
            SELECT cb.phone, MAX(cb.fecha) AS ultima_cita, MAX(cb.especialidad) AS especialidad,
                   p.nombre
            FROM citas_bot cb
            LEFT JOIN contact_profiles p ON p.phone = cb.phone
            WHERE cb.fecha <= date('now', ?)
            AND   cb.fecha >= date('now', ?)
            AND NOT EXISTS (
                SELECT 1 FROM fidelizacion_msgs f
                WHERE f.phone = cb.phone AND f.tipo = 'reactivacion'
                AND   f.enviado_en >= datetime('now', '-60 days')
            )
            GROUP BY cb.phone
        """, (f"-{dias_min} days", f"-{dias_max} days")).fetchall()
        return [dict(r) for r in rows]


def save_fidelizacion_msg(phone: str, tipo: str, cita_id: str = ""):
    """Registra que se envió un mensaje de fidelización."""
    with _conn() as conn:
        conn.execute(
            "INSERT INTO fidelizacion_msgs (phone, tipo, cita_id) VALUES (?, ?, ?)",
            (phone, tipo, cita_id or "")
        )
        conn.commit()


def save_fidelizacion_respuesta(phone: str, tipo: str, respuesta: str):
    """Guarda la respuesta del paciente al último mensaje de fidelización."""
    with _conn() as conn:
        conn.execute("""
            UPDATE fidelizacion_msgs SET respuesta = ?
            WHERE id = (
                SELECT id FROM fidelizacion_msgs WHERE phone = ? AND tipo = ?
                ORDER BY enviado_en DESC LIMIT 1
            )
        """, (respuesta, phone, tipo))
        conn.commit()


def get_ultimo_seguimiento(phone: str) -> dict | None:
    """Retorna el último seguimiento post-consulta sin respuesta para este paciente."""
    with _conn() as conn:
        row = conn.execute("""
            SELECT f.phone, f.cita_id, f.enviado_en, cb.especialidad, cb.profesional
            FROM fidelizacion_msgs f
            LEFT JOIN citas_bot cb ON cb.id_cita = f.cita_id AND cb.phone = f.phone
            WHERE f.phone = ? AND f.tipo = 'postconsulta' AND f.respuesta IS NULL
            ORDER BY f.enviado_en DESC LIMIT 1
        """, (phone,)).fetchone()
        return dict(row) if row else None


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
