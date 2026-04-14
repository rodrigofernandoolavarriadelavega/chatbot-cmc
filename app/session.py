"""
Gestión de sesiones por número de WhatsApp usando SQLite.
Cada sesión guarda: estado actual + datos del flujo en curso.
"""
import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("session")

DB_PATH = Path(__file__).parent.parent / "data" / "sessions.db"
SESSION_TIMEOUT_MIN = 30  # minutos sin actividad → volver a IDLE


def _conn():
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    # WAL mode + busy_timeout reducen "database is locked" bajo concurrencia
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS citas_cache (
            id_prof         INTEGER,
            id_paciente     INTEGER,
            paciente_nombre TEXT,
            fecha           TEXT,
            hora_inicio     TEXT,
            synced_at       TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (id_prof, id_paciente, fecha, hora_inicio)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cache_fecha ON citas_cache(fecha)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ortodoncia_cache (
            id_atencion     INTEGER PRIMARY KEY,
            id_paciente     INTEGER,
            paciente_nombre TEXT,
            fecha           TEXT,
            hora_inicio     TEXT,
            total           INTEGER,
            tipo            TEXT,
            tipo_manual     INTEGER DEFAULT 0,
            synced_at       TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ort_pac ON ortodoncia_cache(id_paciente)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ort_fecha ON ortodoncia_cache(fecha)")
    # Cola de intenciones recibidas durante caídas de Medilink (modo degradado)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS intent_queue (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            phone       TEXT NOT NULL,
            intent      TEXT,
            state_snap  TEXT DEFAULT '',
            ts_enqueued TEXT DEFAULT (datetime('now')),
            notified    INTEGER DEFAULT 0
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_queue_notified ON intent_queue(notified)")
    # Estado del sistema (clave/valor): estado de Medilink, última notificación a recepción, etc.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS system_state (
            key         TEXT PRIMARY KEY,
            value       TEXT,
            updated_at  TEXT DEFAULT (datetime('now'))
        )
    """)
    # Lista de espera: inscripciones de pacientes cuando no hay cupos
    conn.execute("""
        CREATE TABLE IF NOT EXISTS waitlist (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            phone           TEXT NOT NULL,
            rut             TEXT,
            nombre          TEXT,
            especialidad    TEXT NOT NULL,
            id_prof_pref    INTEGER,
            created_at      TEXT DEFAULT (datetime('now')),
            notified_at     TEXT,
            canceled_at     TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_waitlist_active ON waitlist(canceled_at, notified_at)")
    # Tracking de estados de entrega de mensajes salientes (sent/delivered/read/failed)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS message_statuses (
            wamid       TEXT PRIMARY KEY,
            phone       TEXT NOT NULL,
            status      TEXT NOT NULL,
            ts          TEXT DEFAULT (datetime('now')),
            error_code  TEXT,
            error_title TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_msgstatus_phone ON message_statuses(phone)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_msgstatus_ts ON message_statuses(ts)")
    # BSUID mapping (Business-Scoped User ID — Meta June 2026)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bsuid_map (
            bsuid       TEXT PRIMARY KEY,
            phone       TEXT,
            first_seen  TEXT DEFAULT (datetime('now')),
            last_seen   TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bsuid_phone ON bsuid_map(phone)")
    # Notas internas de recepción por paciente (persistentes)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS contact_notes (
            phone       TEXT PRIMARY KEY,
            notes       TEXT DEFAULT '',
            updated_at  TEXT DEFAULT (datetime('now'))
        )
    """)
    # Migración: agregar canal a messages si no existe
    try:
        conn.execute("ALTER TABLE messages ADD COLUMN canal TEXT DEFAULT 'whatsapp'")
    except sqlite3.OperationalError:
        pass  # columna ya existe, nada que hacer
    # Migración: confirmación de asistencia pre-cita
    # Valores: NULL/pending (sin responder), confirmed, reagendar, cancelar
    try:
        conn.execute("ALTER TABLE citas_bot ADD COLUMN confirmation_status TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE citas_bot ADD COLUMN confirmation_at TEXT")
    except sqlite3.OperationalError:
        pass
    # Migración: recordatorio 2 horas antes (separado del recordatorio 24h)
    try:
        conn.execute("ALTER TABLE citas_bot ADD COLUMN reminder_2h_sent INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
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


_REGISTRO_STATES = {"WAIT_NOMBRE_NUEVO", "WAIT_FECHA_NAC", "WAIT_SEXO", "WAIT_COMUNA", "WAIT_EMAIL"}


def get_session(phone: str) -> dict:
    """Devuelve la sesión actual del número. Si expiró o no existe, retorna sesión limpia."""
    with _conn() as conn:
        row = conn.execute("SELECT * FROM sessions WHERE phone=?", (phone,)).fetchone()
        if not row:
            return {"state": "IDLE", "data": {}}
        # Verificar timeout
        updated = datetime.fromisoformat(row["updated_at"])
        if datetime.now(timezone.utc) - updated.replace(tzinfo=timezone.utc) > timedelta(minutes=SESSION_TIMEOUT_MIN):
            old_state = row["state"]
            # Trackear abandono en flujo de registro de paciente nuevo
            if old_state in _REGISTRO_STATES:
                try:
                    log_event(phone, "registro_abandono", {"step": old_state})
                except Exception:
                    pass
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
    """Devuelve citas del bot para una fecha dada donde aún no se envió recordatorio.
    Incluye nombre del paciente desde contact_profiles (LEFT JOIN)."""
    with _conn() as conn:
        rows = conn.execute(
            """SELECT c.*, cp.nombre AS paciente_nombre
               FROM citas_bot c
               LEFT JOIN contact_profiles cp ON c.phone = cp.phone
               WHERE c.fecha=? AND c.reminder_sent=0""", (fecha,)
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


def get_phone_by_rut(rut: str) -> str | None:
    """Busca el teléfono asociado a un RUT en contact_profiles."""
    if not rut:
        return None
    with _conn() as conn:
        row = conn.execute(
            "SELECT phone FROM contact_profiles WHERE rut=? LIMIT 1", (rut,)
        ).fetchone()
        return row["phone"] if row else None


def mark_reminder_sent(cita_id: int):
    """Marca una cita como recordatorio enviado."""
    with _conn() as conn:
        conn.execute("UPDATE citas_bot SET reminder_sent=1 WHERE id=?", (cita_id,))
        conn.commit()


def get_citas_bot_para_2h_reminder(fecha: str, hora_min: str, hora_max: str) -> list[dict]:
    """Devuelve citas del día `fecha` cuyo horario cae en [hora_min, hora_max]
    y donde aún no se envió el recordatorio de 2 horas antes. Los rangos horarios
    se comparan como string HH:MM:SS (formato en el que se almacenan en citas_bot).
    Incluye nombre del paciente desde contact_profiles (LEFT JOIN)."""
    with _conn() as conn:
        rows = conn.execute(
            """SELECT c.*, cp.nombre AS paciente_nombre
               FROM citas_bot c
               LEFT JOIN contact_profiles cp ON c.phone = cp.phone
               WHERE c.fecha=? AND c.hora>=? AND c.hora<=?
                 AND (c.reminder_2h_sent IS NULL OR c.reminder_2h_sent=0)
                 AND (c.confirmation_status IS NULL OR c.confirmation_status != 'cancelar')""",
            (fecha, hora_min, hora_max),
        ).fetchall()
        return [dict(r) for r in rows]


def mark_reminder_2h_sent(cita_id: int):
    """Marca una cita como recordatorio 2h enviado."""
    with _conn() as conn:
        conn.execute("UPDATE citas_bot SET reminder_2h_sent=1 WHERE id=?", (cita_id,))
        conn.commit()


def get_cita_bot_by_id_cita(id_cita: str, phone: str = None) -> dict | None:
    """Busca una cita del bot por id_cita (id Medilink).
    Si se pasa phone, además filtra por ese teléfono (seguridad)."""
    with _conn() as conn:
        if phone:
            row = conn.execute(
                "SELECT * FROM citas_bot WHERE id_cita=? AND phone=? ORDER BY id DESC LIMIT 1",
                (str(id_cita), phone),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM citas_bot WHERE id_cita=? ORDER BY id DESC LIMIT 1",
                (str(id_cita),),
            ).fetchone()
        return dict(row) if row else None


def mark_cita_confirmation(id_cita: str, phone: str, status: str):
    """Guarda la respuesta del paciente al recordatorio pre-cita.
    status ∈ {'confirmed', 'reagendar', 'cancelar'}"""
    with _conn() as conn:
        conn.execute(
            """UPDATE citas_bot
               SET confirmation_status=?, confirmation_at=datetime('now')
               WHERE id_cita=? AND phone=?""",
            (status, str(id_cita), phone),
        )
        conn.commit()


def get_confirmaciones_dia(fecha: str) -> list[dict]:
    """Devuelve las citas del bot para una fecha con su estado de confirmación.
    Usado por el panel admin para mostrar el estado."""
    with _conn() as conn:
        rows = conn.execute(
            """SELECT id_cita, phone, especialidad, profesional, hora, modalidad,
                      confirmation_status, confirmation_at
               FROM citas_bot
               WHERE fecha=?
               ORDER BY hora""",
            (fecha,),
        ).fetchall()
        return [dict(r) for r in rows]


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


def purge_old_data(msgs_days: int = 90, events_days: int = 180) -> dict:
    """Borra mensajes y eventos antiguos para evitar crecimiento ilimitado del SQLite.
    Retorna conteos de filas eliminadas."""
    with _conn() as conn:
        cur = conn.execute(
            "DELETE FROM messages WHERE ts < datetime('now', ?)",
            (f"-{msgs_days} days",),
        )
        msgs_del = cur.rowcount
        cur = conn.execute(
            "DELETE FROM conversation_events WHERE ts < datetime('now', ?)",
            (f"-{events_days} days",),
        )
        events_del = cur.rowcount
        # Reconstruir espacio libre
        conn.commit()
    with _conn() as conn:
        conn.execute("VACUUM")
    log.info("purge_old_data: -%d messages, -%d events", msgs_del, events_del)
    return {"messages_deleted": msgs_del, "events_deleted": events_del}


def log_message(phone: str, direction: str, text: str, state: str = "IDLE", canal: str = "whatsapp"):
    """Registra un mensaje entrante ('in') o saliente ('out') en el historial."""
    with _conn() as conn:
        conn.execute(
            "INSERT INTO messages (phone, direction, text, state, canal) VALUES (?, ?, ?, ?, ?)",
            (phone, direction, str(text)[:2000], state, canal)
        )
        conn.commit()


def upsert_message_status(wamid: str, phone: str, status: str,
                          error_code: str = None, error_title: str = None):
    """Upsert message delivery status from Meta webhook.
    Statuses: sent -> delivered -> read (or failed).
    Only upgrades status: sent < delivered < read. 'failed' always overwrites."""
    _STATUS_ORDER = {"sent": 1, "delivered": 2, "read": 3, "failed": 0}
    with _conn() as conn:
        existing = conn.execute(
            "SELECT status FROM message_statuses WHERE wamid=?", (wamid,)
        ).fetchone()
        if existing:
            old_rank = _STATUS_ORDER.get(existing["status"], 0)
            new_rank = _STATUS_ORDER.get(status, 0)
            # failed always overwrites; otherwise only upgrade
            if status != "failed" and new_rank <= old_rank:
                return
        conn.execute("""
            INSERT INTO message_statuses (wamid, phone, status, ts, error_code, error_title)
            VALUES (?, ?, ?, datetime('now'), ?, ?)
            ON CONFLICT(wamid) DO UPDATE SET
                status=excluded.status, ts=excluded.ts,
                error_code=excluded.error_code, error_title=excluded.error_title
        """, (wamid, phone, status, error_code, error_title))
        conn.commit()


def get_message_status_summary(phone: str) -> dict:
    """Get delivery status summary for a phone's outgoing messages (last 24h)."""
    with _conn() as conn:
        rows = conn.execute("""
            SELECT status, COUNT(*) as cnt
            FROM message_statuses
            WHERE phone=? AND ts > datetime('now', '-24 hours')
            GROUP BY status
        """, (phone,)).fetchall()
        return {r["status"]: r["cnt"] for r in rows}


def get_last_message_status(phone: str) -> str | None:
    """Get the status of the last outgoing message to this phone."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT status FROM message_statuses WHERE phone=? ORDER BY ts DESC LIMIT 1",
            (phone,)
        ).fetchone()
        return row["status"] if row else None


def get_messages(phone: str, limit: int = 300) -> list[dict]:
    """Retorna los últimos `limit` mensajes de un número, ordenados cronológicamente
    (más antiguo primero, más reciente al final — lo que espera el panel para mostrar
    estilo WhatsApp). Antes usaba ORDER BY id ASC LIMIT N lo que devolvía los MÁS
    ANTIGUOS y cortaba los mensajes nuevos en conversaciones largas."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, phone, direction, text, state, ts, COALESCE(canal,'whatsapp') AS canal FROM messages "
            "WHERE phone=? ORDER BY id DESC LIMIT ?",
            (phone, limit)
        ).fetchall()
        return [dict(r) for r in reversed(rows)]


def search_messages(query: str, limit: int = 50) -> list[dict]:
    """Busca mensajes que contengan el texto en todas las conversaciones."""
    with _conn() as conn:
        rows = conn.execute(
            """SELECT m.id, m.phone, m.direction, m.text, m.ts,
                      p.nombre
               FROM messages m
               LEFT JOIN contact_profiles p ON p.phone = m.phone
               WHERE m.text LIKE ?
               ORDER BY m.ts DESC
               LIMIT ?""",
            (f"%{query}%", limit)
        ).fetchall()
        return [dict(r) for r in rows]


# ── Notas internas de recepción ──────────────────────────────────────────────

def get_notes(phone: str) -> str:
    """Retorna las notas internas de un paciente."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT notes FROM contact_notes WHERE phone=?", (phone,)
        ).fetchone()
        return row["notes"] if row else ""


def save_notes(phone: str, notes: str):
    """Guarda notas internas de un paciente (upsert)."""
    with _conn() as conn:
        conn.execute(
            """INSERT INTO contact_notes (phone, notes, updated_at)
               VALUES (?, ?, datetime('now'))
               ON CONFLICT(phone) DO UPDATE SET notes=excluded.notes, updated_at=excluded.updated_at""",
            (phone, notes)
        )
        conn.commit()


# ── Contexto del paciente (panel admin) ──────────────────────────────────────

def get_patient_context(phone: str) -> dict:
    """Datos enriquecidos de un paciente para el panel de contexto admin."""
    with _conn() as conn:
        last_cita = conn.execute(
            "SELECT especialidad, profesional, fecha, hora FROM citas_bot "
            "WHERE phone=? ORDER BY fecha DESC, hora DESC LIMIT 1",
            (phone,)
        ).fetchone()
        total_citas = conn.execute(
            "SELECT COUNT(*) FROM citas_bot WHERE phone=?", (phone,)
        ).fetchone()[0]
        waitlist_row = conn.execute(
            "SELECT especialidad, created_at FROM waitlist "
            "WHERE phone=? AND notified_at IS NULL AND canceled_at IS NULL "
            "ORDER BY id DESC LIMIT 1",
            (phone,)
        ).fetchone()
        return {
            "last_cita": dict(last_cita) if last_cita else None,
            "total_citas": total_citas,
            "waitlist": dict(waitlist_row) if waitlist_row else None,
        }


# ── Estadísticas de registro de pacientes ────────────────────────────────────

def get_registration_stats(dias: int = 30) -> dict:
    """Completados vs abandonados en el flujo de registro."""
    with _conn() as conn:
        completados = conn.execute(
            "SELECT COUNT(*) FROM conversation_events "
            "WHERE event='registro_completo' AND ts >= datetime('now', ?)",
            (f"-{dias} days",)
        ).fetchone()[0]
        abandonados = conn.execute(
            "SELECT COUNT(*) FROM conversation_events "
            "WHERE event='registro_abandono' AND ts >= datetime('now', ?)",
            (f"-{dias} days",)
        ).fetchone()[0]
        total = completados + abandonados
        tasa = round(completados / total * 100, 1) if total else 0
        return {"completados": completados, "abandonados": abandonados,
                "total": total, "tasa_completado": tasa}


def get_referral_stats(dias: int = 30) -> dict:
    """Estadísticas de cómo nos conocieron los pacientes nuevos."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT tag, COUNT(*) as cnt FROM contact_tags "
            "WHERE tag LIKE 'referido:%' AND ts >= datetime('now', ?) "
            "GROUP BY tag ORDER BY cnt DESC",
            (f"-{dias} days",)
        ).fetchall()
        by_source = {}
        total = 0
        for r in rows:
            source = r["tag"].replace("referido:", "")
            by_source[source] = r["cnt"]
            total += r["cnt"]
        return {"by_source": by_source, "total": total, "dias": dias}


def get_conversations(limit: int = 200) -> list[dict]:
    """Lista todas las conversaciones con último mensaje y estado actual.

    Ordena por la última actividad real (mayor entre session.updated_at
    y messages.ts), para que un mensaje que no dispare save_session
    (p.ej. una pregunta FAQ en mitad del flujo) igual "suba" la
    conversación al tope del panel.
    """
    with _conn() as conn:
        rows = conn.execute("""
            SELECT
                s.phone,
                s.state,
                s.data,
                CASE
                    WHEN m.ts IS NOT NULL AND m.ts > s.updated_at THEN m.ts
                    ELSE s.updated_at
                END           AS updated_at,
                m.text        AS last_text,
                m.direction   AS last_dir,
                m.ts          AS last_ts,
                COALESCE(m.canal, 'whatsapp') AS canal,
                p.nombre,
                p.rut,
                (SELECT COUNT(*) FROM messages WHERE phone = s.phone) AS msg_count
            FROM sessions s
            LEFT JOIN messages m ON m.id = (
                SELECT id FROM messages WHERE phone = s.phone ORDER BY id DESC LIMIT 1
            )
            LEFT JOIN contact_profiles p ON p.phone = s.phone
            ORDER BY updated_at DESC
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
            except (json.JSONDecodeError, TypeError, AttributeError) as exc:
                log.warning("session data corrupta phone=%s: %s", d.get("phone"), exc)
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
            except json.JSONDecodeError as exc:
                log.warning("session data corrupta phone=%s: %s", d.get("phone"), exc)
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


def get_metricas_fidelizacion(dias: int | None = None) -> dict:
    """Métricas de campañas de fidelización.

    dias=7 → últimos 7 días, dias=30 → últimos 30 días, dias=None → todo.
    Retorna por cada tipo de campaña: total_sent, total_responded, conversion_rate.
    Para postconsulta: desglose mejor/igual/peor.
    También calcula responded como: pacientes que enviaron un mensaje (direction='in')
    dentro de las 24h siguientes al envío de la campaña.
    """
    with _conn() as conn:
        where = ""
        params: list = []
        if dias:
            where = "WHERE f.enviado_en >= datetime('now', ?)"
            params = [f"-{dias} days"]

        # ── Totales enviados y respondidos (campo respuesta) por tipo ─────
        rows = conn.execute(f"""
            SELECT f.tipo,
                   COUNT(*)                             AS total_sent,
                   COUNT(f.respuesta)                   AS total_responded
            FROM fidelizacion_msgs f
            {where}
            GROUP BY f.tipo
        """, params).fetchall()

        tipos = {}
        for r in rows:
            t = dict(r)
            sent = t["total_sent"]
            resp = t["total_responded"]
            # Fallback: si no hay respuesta directa, check messages within 24h
            t["conversion_rate"] = round(resp / sent * 100, 1) if sent else 0.0
            tipos[t["tipo"]] = t

        # ── Fallback: count patients that sent a message within 24h ───────
        # Only for campaigns with 0 direct responses (e.g. reactivacion, adherencia, crosssell)
        for tipo_key, td in tipos.items():
            if td["total_responded"] == 0 and td["total_sent"] > 0:
                tipo_where = f"{where} AND f.tipo = ?" if where else "WHERE f.tipo = ?"
                count_row = conn.execute(f"""
                    SELECT COUNT(DISTINCT f.id) AS cnt
                    FROM fidelizacion_msgs f
                    INNER JOIN messages m
                        ON m.phone = f.phone
                        AND m.direction = 'in'
                        AND m.ts > f.enviado_en
                        AND m.ts <= datetime(f.enviado_en, '+24 hours')
                    {tipo_where}
                """, params + [tipo_key]).fetchone()
                if count_row:
                    cnt = count_row["cnt"]
                    td["total_responded"] = cnt
                    td["conversion_rate"] = round(cnt / td["total_sent"] * 100, 1)

        # ── Desglose postconsulta ─────────────────────────────────────────
        postconsulta_breakdown = {"mejor": 0, "igual": 0, "peor": 0}
        pc_rows = conn.execute(f"""
            SELECT f.respuesta, COUNT(*) AS cnt
            FROM fidelizacion_msgs f
            {where + " AND" if where else "WHERE"} f.tipo = 'postconsulta'
            AND f.respuesta IS NOT NULL
            GROUP BY f.respuesta
        """, params).fetchall()
        for r in pc_rows:
            resp_val = r["respuesta"]
            if resp_val in postconsulta_breakdown:
                postconsulta_breakdown[resp_val] = r["cnt"]

        # ── Rango de fechas para contexto ─────────────────────────────────
        range_row = conn.execute(f"""
            SELECT MIN(f.enviado_en) AS desde, MAX(f.enviado_en) AS hasta,
                   COUNT(*) AS total_global
            FROM fidelizacion_msgs f
            {where}
        """, params).fetchone()

        return {
            "dias": dias,
            "desde": range_row["desde"] if range_row else None,
            "hasta": range_row["hasta"] if range_row else None,
            "total_global": range_row["total_global"] if range_row else 0,
            "por_tipo": tipos,
            "postconsulta_breakdown": postconsulta_breakdown,
        }


# ── Kinesiología tracking ──────────────────────────────────────────────────────

def _ensure_kine_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS kine_tracking (
            id_paciente INTEGER NOT NULL,
            id_prof     INTEGER NOT NULL,
            total_sesiones INTEGER DEFAULT 0,
            modalidad   TEXT DEFAULT 'fonasa',
            notas       TEXT DEFAULT '',
            updated_at  TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (id_paciente, id_prof)
        )
    """)
    conn.commit()


def get_kine_tracking_all() -> list:
    """Retorna todos los registros de seguimiento de pacientes en control."""
    with _conn() as conn:
        _ensure_kine_table(conn)
        rows = conn.execute("SELECT * FROM kine_tracking ORDER BY updated_at DESC").fetchall()
        return [dict(r) for r in rows]


def save_kine_tracking(id_paciente: int, id_prof: int, total_sesiones: int,
                       modalidad: str = "fonasa", notas: str = ""):
    """Guarda o actualiza el seguimiento de un paciente en control."""
    with _conn() as conn:
        _ensure_kine_table(conn)
        conn.execute("""
            INSERT INTO kine_tracking (id_paciente, id_prof, total_sesiones, modalidad, notas, updated_at)
            VALUES (?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(id_paciente, id_prof) DO UPDATE SET
                total_sesiones=excluded.total_sesiones,
                modalidad=excluded.modalidad,
                notas=excluded.notas,
                updated_at=excluded.updated_at
        """, (id_paciente, id_prof, total_sesiones, modalidad, notas))
        conn.commit()


def puede_enviar_campana(phone: str, tipo: str, dias_cooldown: int = 7) -> bool:
    """True si no se envió este tipo de campaña en los últimos dias_cooldown días."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM fidelizacion_msgs WHERE phone=? AND tipo=? "
            "AND enviado_en >= datetime('now', ?)",
            (phone, tipo, f"-{dias_cooldown} days")
        ).fetchone()
        return row is None


def get_kine_candidatos_adherencia(gap_dias: int = 4) -> list[dict]:
    """
    Pacientes con cita de kinesiología hace gap_dias+ días,
    sin cita kine futura, sin mensaje de adherencia en últimos 7 días.
    """
    with _conn() as conn:
        rows = conn.execute("""
            SELECT cb.phone, MAX(cb.fecha) AS ultima_fecha, cb.profesional, p.nombre
            FROM citas_bot cb
            LEFT JOIN contact_profiles p ON p.phone = cb.phone
            WHERE cb.especialidad LIKE 'Kinesiolog%'
              AND cb.fecha <= date('now', ?)
              AND cb.fecha >= date('now', '-60 days')
              AND NOT EXISTS (
                  SELECT 1 FROM citas_bot cb2
                  WHERE cb2.phone = cb.phone
                    AND cb2.especialidad LIKE 'Kinesiolog%'
                    AND cb2.fecha > date('now')
              )
              AND NOT EXISTS (
                  SELECT 1 FROM fidelizacion_msgs f
                  WHERE f.phone = cb.phone AND f.tipo = 'adherencia_kine'
                    AND f.enviado_en >= datetime('now', '-7 days')
              )
            GROUP BY cb.phone
        """, (f"-{gap_dias} days",)).fetchall()
        return [dict(r) for r in rows]


def get_control_candidatos(especialidad: str, dias_control: int) -> list[dict]:
    """
    Pacientes cuya última cita de la especialidad fue hace dias_control+ días,
    sin cita futura de esa especialidad, sin recordatorio de control en 15 días.
    """
    tipo_fidel = f"control_{especialidad.lower().replace(' ', '_')}"
    with _conn() as conn:
        rows = conn.execute("""
            SELECT cb.phone, MAX(cb.fecha) AS ultima_fecha, cb.profesional, p.nombre
            FROM citas_bot cb
            LEFT JOIN contact_profiles p ON p.phone = cb.phone
            WHERE cb.especialidad = ?
              AND cb.fecha <= date('now', ?)
              AND cb.fecha >= date('now', '-180 days')
              AND NOT EXISTS (
                  SELECT 1 FROM citas_bot cb2
                  WHERE cb2.phone = cb.phone
                    AND cb2.especialidad = ?
                    AND cb2.fecha > date('now')
              )
              AND NOT EXISTS (
                  SELECT 1 FROM fidelizacion_msgs f
                  WHERE f.phone = cb.phone AND f.tipo = ?
                    AND f.enviado_en >= datetime('now', '-15 days')
              )
            GROUP BY cb.phone
        """, (especialidad, f"-{dias_control} days", especialidad, tipo_fidel)).fetchall()
        return [dict(r) for r in rows]


def get_crosssell_kine_candidatos() -> list[dict]:
    """
    Pacientes con cita de medicina/traumatología hace 1-5 días,
    sin cita de kinesiología reciente, sin cross-sell enviado en 14 días.
    """
    with _conn() as conn:
        rows = conn.execute("""
            SELECT cb.phone, MAX(cb.fecha) AS ultima_fecha, cb.especialidad, p.nombre
            FROM citas_bot cb
            LEFT JOIN contact_profiles p ON p.phone = cb.phone
            WHERE cb.especialidad IN ('Medicina General', 'Medicina Familiar', 'Traumatología')
              AND cb.fecha >= date('now', '-5 days')
              AND cb.fecha <= date('now', '-1 days')
              AND NOT EXISTS (
                  SELECT 1 FROM citas_bot cb2
                  WHERE cb2.phone = cb.phone
                    AND cb2.especialidad LIKE 'Kinesiolog%'
                    AND cb2.fecha >= date('now', '-30 days')
              )
              AND NOT EXISTS (
                  SELECT 1 FROM fidelizacion_msgs f
                  WHERE f.phone = cb.phone AND f.tipo = 'crosssell_kine'
                    AND f.enviado_en >= datetime('now', '-14 days')
              )
            GROUP BY cb.phone
        """).fetchall()
        return [dict(r) for r in rows]


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


# ─────────────────────────────────────────────────────────────────────────────
# Caché de citas Medilink (módulo pacientes en control)
# ─────────────────────────────────────────────────────────────────────────────

def upsert_citas_cache(citas: list[dict]):
    """Inserta o actualiza citas en el caché local. Cada cita debe tener:
    id_prof, id_paciente, paciente_nombre, fecha, hora_inicio."""
    if not citas:
        return
    with _conn() as conn:
        conn.executemany("""
            INSERT INTO citas_cache (id_prof, id_paciente, paciente_nombre, fecha, hora_inicio, synced_at)
            VALUES (:id_prof, :id_paciente, :paciente_nombre, :fecha, :hora_inicio, datetime('now'))
            ON CONFLICT(id_prof, id_paciente, fecha, hora_inicio) DO UPDATE SET
                paciente_nombre=excluded.paciente_nombre,
                synced_at=excluded.synced_at
        """, citas)
        conn.commit()


def delete_citas_cache_fecha(id_prof: int, fecha: str):
    """Borra todas las citas cacheadas de un profesional para una fecha (antes de re-sync)."""
    with _conn() as conn:
        conn.execute("DELETE FROM citas_cache WHERE id_prof=? AND fecha=?", (id_prof, fecha))
        conn.commit()


def citas_cache_tiene_fecha(id_prof: int, fecha: str) -> bool:
    """True si ya hay datos cacheados para este profesional y fecha."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM citas_cache WHERE id_prof=? AND fecha=? LIMIT 1",
            (id_prof, fecha)
        ).fetchone()
        return row is not None


def upsert_ortodoncia_cache(visitas: list[dict]):
    """Inserta o actualiza visitas de ortodoncia. No sobreescribe tipo_manual=1."""
    if not visitas:
        return
    with _conn() as conn:
        for v in visitas:
            conn.execute("""
                INSERT INTO ortodoncia_cache
                    (id_atencion, id_paciente, paciente_nombre, fecha, hora_inicio, total, tipo, tipo_manual, synced_at)
                VALUES (:id_atencion, :id_paciente, :paciente_nombre, :fecha, :hora_inicio, :total, :tipo, 0, datetime('now'))
                ON CONFLICT(id_atencion) DO UPDATE SET
                    paciente_nombre=excluded.paciente_nombre,
                    total=excluded.total,
                    tipo=CASE WHEN tipo_manual=1 THEN tipo ELSE excluded.tipo END,
                    synced_at=excluded.synced_at
            """, v)
        conn.commit()


def set_ortodoncia_tipo(id_atencion: int, tipo: str):
    """Guarda clasificación manual de una visita (instalacion/control)."""
    with _conn() as conn:
        conn.execute(
            "UPDATE ortodoncia_cache SET tipo=?, tipo_manual=1 WHERE id_atencion=?",
            (tipo, id_atencion)
        )
        conn.commit()


def get_ortodoncia_pacientes() -> list[dict]:
    """Retorna todos los pacientes de ortodoncia con sus visitas agrupadas."""
    with _conn() as conn:
        rows = conn.execute("""
            SELECT id_atencion, id_paciente, paciente_nombre, fecha, hora_inicio, total, tipo, tipo_manual
            FROM ortodoncia_cache
            ORDER BY id_paciente, fecha
        """).fetchall()
        pacientes: dict = {}
        for r in rows:
            pid = r["id_paciente"]
            if pid not in pacientes:
                pacientes[pid] = {"id_paciente": pid, "nombre": r["paciente_nombre"], "visitas": []}
            pacientes[pid]["visitas"].append({
                "id_atencion": r["id_atencion"],
                "fecha": r["fecha"],
                "hora_inicio": r["hora_inicio"],
                "total": r["total"],
                "tipo": r["tipo"],
                "tipo_manual": r["tipo_manual"],
            })
        return list(pacientes.values())


def get_ortodoncia_sync_max_fecha() -> str | None:
    """Retorna la fecha más reciente sincronizada en ortodoncia_cache."""
    with _conn() as conn:
        row = conn.execute("SELECT MAX(fecha) FROM ortodoncia_cache").fetchone()
        return row[0] if row else None


def get_citas_cache_todos(ids_prof: list[int]) -> list[dict]:
    """Retorna todas las citas cacheadas (sin filtro de mes) para los profesionales dados."""
    placeholders = ",".join("?" * len(ids_prof))
    with _conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM citas_cache WHERE id_prof IN ({placeholders}) "
            f"AND id_paciente != 0 ORDER BY fecha, hora_inicio",
            (*ids_prof,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_citas_cache_mes(year: int, month: int, ids_prof: list[int]) -> list[dict]:
    """Retorna citas del caché para el mes y profesionales dados."""
    import calendar
    last_day = calendar.monthrange(year, month)[1]
    fecha_ini = f"{year}-{month:02d}-01"
    fecha_fin = f"{year}-{month:02d}-{last_day:02d}"
    placeholders = ",".join("?" * len(ids_prof))
    with _conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM citas_cache WHERE id_prof IN ({placeholders}) "
            f"AND fecha >= ? AND fecha <= ? ORDER BY fecha, hora_inicio",
            (*ids_prof, fecha_ini, fecha_fin)
        ).fetchall()
        return [dict(r) for r in rows]


# ─────────────────────────────────────────────────────────────────────────────
# Modo degradado: cola de intenciones + estado del sistema
# ─────────────────────────────────────────────────────────────────────────────

def enqueue_intent(phone: str, intent: str, state_snap: str = ""):
    """Guarda en la cola una intención recibida durante una caída de Medilink.
    Se usa para avisar al paciente cuando el sistema vuelva a estar operativo."""
    with _conn() as conn:
        # Evitar duplicados: si el mismo teléfono ya tiene una intención pendiente
        # en los últimos 10 min, no volver a encolar.
        existing = conn.execute("""
            SELECT id FROM intent_queue
            WHERE phone = ? AND notified = 0
              AND ts_enqueued >= datetime('now', '-10 minutes')
            LIMIT 1
        """, (phone,)).fetchone()
        if existing:
            return
        conn.execute(
            "INSERT INTO intent_queue (phone, intent, state_snap) VALUES (?, ?, ?)",
            (phone, intent, state_snap)
        )
        conn.commit()


def get_pending_intent_queue() -> list[dict]:
    """Retorna todas las intenciones pendientes de notificar al paciente."""
    with _conn() as conn:
        rows = conn.execute("""
            SELECT id, phone, intent, state_snap, ts_enqueued
            FROM intent_queue
            WHERE notified = 0
            ORDER BY ts_enqueued ASC
        """).fetchall()
        return [dict(r) for r in rows]


def mark_intent_notified(queue_id: int):
    """Marca una entrada de la cola como notificada."""
    with _conn() as conn:
        conn.execute("UPDATE intent_queue SET notified = 1 WHERE id = ?", (queue_id,))
        conn.commit()


def intent_queue_depth() -> int:
    """Cantidad de intenciones pendientes de notificar (para /health)."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM intent_queue WHERE notified = 0"
        ).fetchone()
        return int(row[0]) if row else 0


def system_state_get(key: str) -> str | None:
    """Lee un valor del estado del sistema."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT value FROM system_state WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None


def system_state_set(key: str, value: str):
    """Escribe un valor en el estado del sistema (upsert)."""
    with _conn() as conn:
        conn.execute("""
            INSERT INTO system_state (key, value, updated_at)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
        """, (key, value))
        conn.commit()


def system_state_updated_at(key: str) -> str | None:
    """Retorna cuándo se actualizó por última vez un valor del estado."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT updated_at FROM system_state WHERE key = ?", (key,)
        ).fetchone()
        return row["updated_at"] if row else None


# ─────────────────────────────────────────────────────────────────────────────
# Lista de espera (waitlist)
# ─────────────────────────────────────────────────────────────────────────────

def add_to_waitlist(phone: str, rut: str, nombre: str,
                    especialidad: str, id_prof_pref: int | None = None) -> int:
    """Inscribe a un paciente en la lista de espera. Si ya existe una inscripción
    activa (no notificada ni cancelada) para el mismo phone+especialidad, la
    actualiza en lugar de duplicar. Retorna el id de la fila."""
    with _conn() as conn:
        existing = conn.execute("""
            SELECT id FROM waitlist
            WHERE phone = ? AND especialidad = ?
              AND notified_at IS NULL AND canceled_at IS NULL
            ORDER BY id DESC LIMIT 1
        """, (phone, especialidad)).fetchone()
        if existing:
            conn.execute("""
                UPDATE waitlist SET rut=?, nombre=?, id_prof_pref=?, created_at=datetime('now')
                WHERE id=?
            """, (rut, nombre, id_prof_pref, existing["id"]))
            conn.commit()
            return int(existing["id"])
        cur = conn.execute("""
            INSERT INTO waitlist (phone, rut, nombre, especialidad, id_prof_pref)
            VALUES (?, ?, ?, ?, ?)
        """, (phone, rut, nombre, especialidad, id_prof_pref))
        conn.commit()
        return int(cur.lastrowid)


def get_waitlist_pending() -> list[dict]:
    """Retorna todas las inscripciones activas (no notificadas ni canceladas)
    ordenadas por antigüedad (FIFO). La usa el cron de chequeo diario."""
    with _conn() as conn:
        rows = conn.execute("""
            SELECT id, phone, rut, nombre, especialidad, id_prof_pref, created_at
            FROM waitlist
            WHERE notified_at IS NULL AND canceled_at IS NULL
            ORDER BY created_at ASC
        """).fetchall()
        return [dict(r) for r in rows]


def mark_waitlist_notified(waitlist_id: int):
    """Marca una entrada como notificada (ya se avisó al paciente)."""
    with _conn() as conn:
        conn.execute(
            "UPDATE waitlist SET notified_at = datetime('now') WHERE id = ?",
            (waitlist_id,)
        )
        conn.commit()


def cancel_waitlist(waitlist_id: int):
    """Marca una entrada como cancelada (por el paciente o la recepción)."""
    with _conn() as conn:
        conn.execute(
            "UPDATE waitlist SET canceled_at = datetime('now') WHERE id = ?",
            (waitlist_id,)
        )
        conn.commit()


def get_waitlist_all(limit: int = 200) -> list[dict]:
    """Retorna todas las entradas de waitlist (para el panel admin)."""
    with _conn() as conn:
        rows = conn.execute("""
            SELECT w.*, p.nombre AS perfil_nombre
            FROM waitlist w
            LEFT JOIN contact_profiles p ON p.phone = w.phone
            ORDER BY w.id DESC LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]


def waitlist_depth() -> int:
    """Cantidad de inscripciones activas (para /health)."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM waitlist "
            "WHERE notified_at IS NULL AND canceled_at IS NULL"
        ).fetchone()
        return int(row[0]) if row else 0


# ── BSUID mapping ─────────────────────────────────────────────────────────

def upsert_bsuid(bsuid: str, phone: str | None = None):
    """Store or update a BSUID→phone mapping. phone can be None if hidden."""
    if not bsuid:
        return
    with _conn() as conn:
        conn.execute("""
            INSERT INTO bsuid_map (bsuid, phone, last_seen)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(bsuid) DO UPDATE SET
                phone = COALESCE(excluded.phone, bsuid_map.phone),
                last_seen = datetime('now')
        """, (bsuid, phone))
        conn.commit()


def resolve_phone_from_bsuid(bsuid: str) -> str | None:
    """Look up the phone number for a BSUID. Returns None if unknown."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT phone FROM bsuid_map WHERE bsuid=?", (bsuid,)
        ).fetchone()
        return row["phone"] if row else None


def get_bsuid_stats() -> dict:
    """Return BSUID mapping statistics for /health endpoint."""
    with _conn() as conn:
        total = conn.execute("SELECT COUNT(*) as c FROM bsuid_map").fetchone()["c"]
        with_phone = conn.execute(
            "SELECT COUNT(*) as c FROM bsuid_map WHERE phone IS NOT NULL"
        ).fetchone()["c"]
        return {"total": total, "with_phone": with_phone, "phone_hidden": total - with_phone}
