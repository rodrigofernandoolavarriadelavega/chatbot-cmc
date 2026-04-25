"""
Gestión de sesiones por número de WhatsApp usando SQLite.
Cada sesión guarda: estado actual + datos del flujo en curso.

Encriptación en reposo (Ley 19.628): si `SQLCIPHER_KEY` está definido en el
entorno y el módulo `sqlcipher3` está instalado, la conexión usa SQLCipher
(AES-256). Fallback transparente a `sqlite3` para dev local y para DBs
históricas sin encriptar.
"""
import json
import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

log = logging.getLogger("session")

DB_PATH = Path(__file__).parent.parent / "data" / "sessions.db"
SESSION_TIMEOUT_MIN = 30  # minutos sin actividad → volver a IDLE (por defecto)
# Flujos activos (paciente está agendando/cancelando/registrando) tienen
# timeout extendido: 240 min (4 h). Mediana tiempo saludo→confirmación es
# 197 min → la sesión expiraba y forzaba empezar de cero. Con 4h, el
# paciente puede volver y retomar.
SESSION_TIMEOUT_FLUJO_MIN = 240
_FLUJO_ACTIVO_STATES = {
    "WAIT_ESPECIALIDAD", "WAIT_SLOT", "WAIT_MODALIDAD", "WAIT_BOOKING_FOR",
    "WAIT_PHONE_OWNER_NAME", "WAIT_RUT_AGENDAR", "WAIT_NOMBRE_NUEVO",
    "WAIT_FECHA_NAC", "WAIT_SEXO", "WAIT_COMUNA", "WAIT_EMAIL",
    "WAIT_REFERRAL", "WAIT_REFERRAL_CODE", "WAIT_DATOS_NUEVO",
    "CONFIRMING_CITA", "WAIT_DURACION_MASOTERAPIA",
    "WAIT_RUT_CANCELAR", "WAIT_CITA_CANCELAR", "CONFIRMING_CANCEL",
    "WAIT_RUT_REAGENDAR", "WAIT_CITA_REAGENDAR", "WAIT_QUICK_BOOK",
}

# ── SQLCipher opcional ───────────────────────────────────────────────────────
_SQLCIPHER_KEY = (os.getenv("SQLCIPHER_KEY") or "").strip()
_sqlcipher_mod = None
if _SQLCIPHER_KEY:
    try:
        from sqlcipher3 import dbapi2 as _sqlcipher_mod  # type: ignore
    except ImportError:
        try:
            from pysqlcipher3 import dbapi2 as _sqlcipher_mod  # type: ignore
        except ImportError:
            log.warning(
                "SQLCIPHER_KEY set but no sqlcipher module found; "
                "continuing with unencrypted sqlite3 (install sqlcipher3-binary)"
            )
            _sqlcipher_mod = None

_USE_SQLCIPHER = _sqlcipher_mod is not None and bool(_SQLCIPHER_KEY)

# Tupla de excepciones de operación DB. sqlcipher3 define sus propias clases
# que NO heredan de sqlite3.*, así que cuando SQLCipher está activo hay que
# capturar ambos tipos (ej. ALTER TABLE duplicado durante migraciones).
if _sqlcipher_mod is not None:
    _OPERATIONAL_ERRORS: tuple = (sqlite3.OperationalError, _sqlcipher_mod.OperationalError)
else:
    _OPERATIONAL_ERRORS = (sqlite3.OperationalError,)


def _conn():
    DB_PATH.parent.mkdir(exist_ok=True)
    if _USE_SQLCIPHER:
        # Validar hex antes de interpolar en PRAGMA (no acepta bindings).
        if not re.fullmatch(r"[0-9a-fA-F]+", _SQLCIPHER_KEY):
            raise ValueError("SQLCIPHER_KEY debe ser hex (0-9, a-f).")
        conn = _sqlcipher_mod.connect(str(DB_PATH), timeout=10)
        conn.execute(f"PRAGMA key = \"x'{_SQLCIPHER_KEY}'\"")
        conn.execute("PRAGMA cipher_page_size = 4096")
        conn.row_factory = _sqlcipher_mod.Row
    else:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.row_factory = sqlite3.Row
    # WAL mode + busy_timeout reducen "database is locked" bajo concurrencia
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
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
    # OTP para portal del paciente
    conn.execute("""
        CREATE TABLE IF NOT EXISTS portal_otp (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            rut         TEXT NOT NULL,
            phone       TEXT NOT NULL,
            code        TEXT NOT NULL,
            created_at  TEXT DEFAULT (datetime('now')),
            used        INTEGER DEFAULT 0
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_portal_otp_rut ON portal_otp(rut, created_at)")
    # Vinculaciones familiares del portal (madre/padre gestiona citas de hijos o mayores)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS family_links (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_rut            TEXT NOT NULL,
            dependent_rut        TEXT NOT NULL,
            dependent_nombre     TEXT,
            relation             TEXT,
            verification_method  TEXT NOT NULL,
            verified_at          TEXT DEFAULT (datetime('now')),
            created_at           TEXT DEFAULT (datetime('now')),
            revoked_at           TEXT,
            UNIQUE(owner_rut, dependent_rut)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_family_owner ON family_links(owner_rut, revoked_at)")
    # Registros personales del paciente (presión, glicemia, peso, temperatura)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS patient_vitals (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            rut        TEXT NOT NULL,
            tipo       TEXT NOT NULL,
            valor      REAL NOT NULL,
            valor2     REAL,
            contexto   TEXT,
            nota       TEXT,
            ts         TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_vitals_rut_ts ON patient_vitals(rut, ts DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_vitals_rut_tipo ON patient_vitals(rut, tipo, ts DESC)")
    # Códigos de referido (programa de referidos)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS referral_codes (
            phone       TEXT PRIMARY KEY,
            code        TEXT UNIQUE NOT NULL,
            created_at  TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS referral_uses (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            code            TEXT NOT NULL,
            referrer_phone  TEXT NOT NULL,
            referred_phone  TEXT NOT NULL,
            created_at      TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_refuse_code ON referral_uses(code)")
    # Campañas estacionales — registro de envíos
    conn.execute("""
        CREATE TABLE IF NOT EXISTS campanas_envios (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            phone       TEXT NOT NULL,
            campana_id  TEXT NOT NULL,
            enviado_en  TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_camp_id ON campanas_envios(campana_id)")
    # Archivos de pacientes (fotos, PDFs, docs enviados por WhatsApp)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS patient_files (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            phone       TEXT NOT NULL,
            filename    TEXT NOT NULL,
            media_type  TEXT NOT NULL,
            mime_type   TEXT DEFAULT '',
            file_path   TEXT NOT NULL,
            file_size   INTEGER DEFAULT 0,
            caption     TEXT DEFAULT '',
            created_at  TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pf_phone ON patient_files(phone)")
    # Demanda de especialistas/exámenes que no tenemos
    conn.execute("""
        CREATE TABLE IF NOT EXISTS demanda_no_disponible (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            phone       TEXT NOT NULL,
            solicitud   TEXT NOT NULL,
            tipo        TEXT DEFAULT 'especialidad',
            created_at  TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_dnd_ts ON demanda_no_disponible(created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_dnd_phone ON demanda_no_disponible(phone)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_citas_bot_esp ON citas_bot(especialidad)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_citas_bot_phone ON citas_bot(phone)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_event_ts ON conversation_events(event, ts)")
    # Marca "visto por admin" — permite al panel limpiar conversaciones sin
    # cambiar el state del flujo conversacional.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS admin_seen (
            phone       TEXT PRIMARY KEY,
            seen_at     TEXT DEFAULT (datetime('now')),
            seen_by     TEXT DEFAULT 'admin'
        )
    """)
    # Migración: columna para trackear cancelaciones notificadas (reagendar 1-click)
    try:
        conn.execute("ALTER TABLE citas_bot ADD COLUMN cancel_detected_at TEXT")
    except Exception:
        pass  # columna ya existe
    # Migración: agregar fecha_nacimiento a contact_profiles
    try:
        conn.execute("ALTER TABLE contact_profiles ADD COLUMN fecha_nacimiento TEXT")
    except _OPERATIONAL_ERRORS:
        pass
    # Migración: campos del perfil editable (actualizar datos desde portal)
    for col, typ in [
        ("email", "TEXT"), ("comuna", "TEXT"), ("direccion", "TEXT"),
        ("sexo", "TEXT"), ("prevision", "TEXT"),
        ("contacto_emerg_nombre", "TEXT"), ("contacto_emerg_telefono", "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE contact_profiles ADD COLUMN {col} {typ}")
        except _OPERATIONAL_ERRORS:
            pass
    # Migración: agregar canal a messages si no existe
    try:
        conn.execute("ALTER TABLE messages ADD COLUMN canal TEXT DEFAULT 'whatsapp'")
    except _OPERATIONAL_ERRORS:
        pass  # columna ya existe, nada que hacer
    # Migración: wamid y edited_at para edición de mensajes enviados
    try:
        conn.execute("ALTER TABLE messages ADD COLUMN wamid TEXT")
    except _OPERATIONAL_ERRORS:
        pass
    try:
        conn.execute("ALTER TABLE messages ADD COLUMN edited_at TEXT")
    except _OPERATIONAL_ERRORS:
        pass
    try:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_msg_wamid ON messages(wamid)")
    except _OPERATIONAL_ERRORS:
        pass
    # Migración: confirmación de asistencia pre-cita
    # Valores: NULL/pending (sin responder), confirmed, reagendar, cancelar
    try:
        conn.execute("ALTER TABLE citas_bot ADD COLUMN confirmation_status TEXT")
    except _OPERATIONAL_ERRORS:
        pass
    try:
        conn.execute("ALTER TABLE citas_bot ADD COLUMN confirmation_at TEXT")
    except _OPERATIONAL_ERRORS:
        pass
    # Migración: recordatorio 2 horas antes (separado del recordatorio 24h)
    try:
        conn.execute("ALTER TABLE citas_bot ADD COLUMN reminder_2h_sent INTEGER DEFAULT 0")
    except _OPERATIONAL_ERRORS:
        pass
    # Migración: nombre paciente + es_tercero (cita para otra persona)
    try:
        conn.execute("ALTER TABLE citas_bot ADD COLUMN paciente_nombre TEXT DEFAULT ''")
    except _OPERATIONAL_ERRORS:
        pass
    try:
        conn.execute("ALTER TABLE citas_bot ADD COLUMN es_tercero INTEGER DEFAULT 0")
    except _OPERATIONAL_ERRORS:
        pass
    # ── Compliance Ley 19.628 (Chile, reforma 2024) ───────────────────────────
    # Registro de consentimiento explícito del paciente para almacenar
    # conversación + datos. Sin un registro 'accepted' aquí NO se almacena
    # conversación salvo mensajes de emergencia (art. 21 — base legal: interés
    # vital del titular).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS privacy_consents (
            phone           TEXT PRIMARY KEY,
            status          TEXT NOT NULL,           -- 'accepted' | 'declined' | 'pending'
            consent_version TEXT DEFAULT '1.0',
            method          TEXT DEFAULT 'whatsapp', -- whatsapp | admin | portal
            consented_at    TEXT DEFAULT (datetime('now')),
            revoked_at      TEXT
        )
    """)
    # Audit log inmutable de ejecución del derecho al olvido (art. 12 Ley 19.628).
    # Esta tabla NO se borra nunca — es la prueba legal de cumplimiento.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS gdpr_deletions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            rut         TEXT,
            phone       TEXT,
            deleted_at  TEXT DEFAULT (datetime('now')),
            deleted_by  TEXT DEFAULT 'admin',
            summary     TEXT DEFAULT '{}'
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_gdpr_rut ON gdpr_deletions(rut)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_gdpr_phone ON gdpr_deletions(phone)")
    conn.commit()
    return conn


def is_duplicate(msg_id: str) -> bool:
    """Retorna True si el msg_id ya fue procesado (idempotencia ante reenvíos de Meta).

    Usa INSERT OR IGNORE atómico: si la fila ya existía, rowcount==0 → duplicado.
    Evita race condition cuando Meta reenvía el mismo msg_id en paralelo.
    """
    with _conn() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO processed_msgs (msg_id) VALUES (?)", (msg_id,)
        )
        # Limpiar entradas de más de 1 hora para no crecer indefinidamente
        conn.execute("DELETE FROM processed_msgs WHERE created_at < datetime('now', '-1 hour')")
        conn.commit()
        return cur.rowcount == 0


_REGISTRO_STATES = {"WAIT_DATOS_NUEVO", "WAIT_NOMBRE_NUEVO", "WAIT_FECHA_NAC", "WAIT_SEXO", "WAIT_COMUNA", "WAIT_EMAIL"}


def get_session(phone: str) -> dict:
    """Devuelve la sesión actual del número. Si expiró o no existe, retorna sesión limpia.

    Timeout diferenciado:
    - Flujos activos (WAIT_*, CONFIRMING_*): 4h (paciente volviendo retoma)
    - IDLE / HUMAN_TAKEOVER / otros: 30 min
    """
    with _conn() as conn:
        row = conn.execute("SELECT * FROM sessions WHERE phone=?", (phone,)).fetchone()
        if not row:
            return {"state": "IDLE", "data": {}}
        updated = datetime.fromisoformat(row["updated_at"])
        elapsed = datetime.now(timezone.utc) - updated.replace(tzinfo=timezone.utc)
        state = row["state"]
        # Timeout según tipo de estado
        limit_min = SESSION_TIMEOUT_FLUJO_MIN if state in _FLUJO_ACTIVO_STATES else SESSION_TIMEOUT_MIN
        if elapsed > timedelta(minutes=limit_min):
            # Trackear abandono en flujo de registro de paciente nuevo
            if state in _REGISTRO_STATES:
                try:
                    log_event(phone, "registro_abandono", {"step": state})
                except Exception:
                    pass
            # Trackear abandono en flujo largo (para métricas)
            elif state in _FLUJO_ACTIVO_STATES:
                try:
                    log_event(phone, "flujo_abandono", {"state": state, "minutos": int(elapsed.total_seconds()/60)})
                except Exception:
                    pass
            _reset(conn, phone)
            return {"state": "IDLE", "data": {}}
        return {"state": state, "data": json.loads(row["data"])}


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


def cleanup_stuck_sessions(hours: int = 4) -> int:
    """Resetea sesiones atascadas en WAIT_*/CONFIRMING_* > hours horas
    PRESERVANDO el snapshot last_slots/last_especialidad (igual que _reset).
    Antes el UPDATE crudo borraba ese snapshot y el paciente perdia la
    posibilidad de retomar con '10:30'."""
    with _conn() as conn:
        rows = conn.execute(
            f"""SELECT phone FROM sessions
                WHERE (state LIKE 'WAIT_%' OR state LIKE 'CONFIRMING_%')
                AND updated_at < datetime('now', '-{int(hours)} hours')"""
        ).fetchall()
        for r in rows:
            try:
                _reset(conn, r["phone"])
            except Exception as _e:
                log.warning("cleanup _reset falló phone=%s: %s", r["phone"], _e)
        conn.commit()
        return len(rows)


def _reset(conn, phone: str):
    # Preservar snapshot de última búsqueda de slots para que el paciente pueda
    # retomar con "10:30" aunque la sesión haya caducado o sido reseteada.
    preserved = {}
    row = conn.execute("SELECT data FROM sessions WHERE phone=?", (phone,)).fetchone()
    if row:
        try:
            old = json.loads(row["data"] or "{}")
            if old.get("todos_slots"):
                preserved = {
                    "last_slots": old.get("todos_slots"),
                    "last_especialidad": old.get("especialidad"),
                    "last_slots_ts": datetime.now(timezone.utc).isoformat(),
                }
            elif old.get("last_slots"):  # ya hay snapshot, mantenerlo
                preserved = {
                    "last_slots": old["last_slots"],
                    "last_especialidad": old.get("last_especialidad"),
                    "last_slots_ts": old.get("last_slots_ts"),
                }
        except Exception:
            pass
    conn.execute("""
        INSERT INTO sessions (phone, state, data, updated_at)
        VALUES (?, 'IDLE', ?, datetime('now'))
        ON CONFLICT(phone) DO UPDATE SET
            state='IDLE', data=excluded.data, updated_at=datetime('now')
    """, (phone, json.dumps(preserved, ensure_ascii=False)))
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


def get_tags_summary() -> dict:
    """Devuelve {counts, by_phone} para filtros dinámicos del panel admin.

    Excluye tags auto-generados (referido:*, dx:*) para que solo aparezcan
    como filtros las etiquetas manuales y las de interés operativo.
    """
    with _conn() as conn:
        rows = conn.execute(
            "SELECT phone, tag FROM contact_tags "
            "WHERE tag NOT LIKE 'referido:%' AND tag NOT LIKE 'dx:%'"
        ).fetchall()
    counts: dict = {}
    by_phone: dict = {}
    for r in rows:
        tag = r["tag"]
        phone = r["phone"]
        counts[tag] = counts.get(tag, 0) + 1
        by_phone.setdefault(phone, []).append(tag)
    return {"counts": counts, "by_phone": by_phone}


# ── Citas creadas por el bot ──────────────────────────────────────────────────

def save_cita_bot(phone: str, id_cita: str, especialidad: str,
                  profesional: str, fecha: str, hora: str, modalidad: str,
                  paciente_nombre: str = "", es_tercero: bool = False):
    """Registra una cita creada por el bot para tracking y recordatorios.
    paciente_nombre: nombre del paciente real (puede ser distinto del dueño del celular).
    es_tercero: True si quien agenda es un familiar/tercero (el celular no es del paciente).
    """
    with _conn() as conn:
        conn.execute(
            """INSERT INTO citas_bot (phone, id_cita, especialidad, profesional,
                                       fecha, hora, modalidad, paciente_nombre, es_tercero)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (phone, id_cita, especialidad, profesional, fecha, hora, modalidad,
             paciente_nombre or "", 1 if es_tercero else 0)
        )
        conn.commit()


def get_citas_bot_pendientes(fecha: str) -> list[dict]:
    """Devuelve citas del bot para una fecha dada donde aún no se envió recordatorio.
    Usa paciente_nombre de citas_bot si existe; fallback a contact_profiles.
    Incluye phone_owner (nombre del dueño del celular) para recordatorios de terceros."""
    with _conn() as conn:
        rows = conn.execute(
            """SELECT c.*,
                      CASE WHEN c.paciente_nombre != '' THEN c.paciente_nombre
                           ELSE cp.nombre END AS paciente_nombre,
                      cp.nombre AS phone_owner
               FROM citas_bot c
               LEFT JOIN contact_profiles cp ON c.phone = cp.phone
               WHERE c.fecha=? AND c.reminder_sent=0""", (fecha,)
        ).fetchall()
        return [dict(r) for r in rows]


# ── Perfiles de paciente ──────────────────────────────────────────────────────

def save_profile(phone: str, rut: str, nombre: str, fecha_nacimiento: str = None):
    """Guarda o actualiza el perfil del paciente asociado al número."""
    with _conn() as conn:
        if fecha_nacimiento:
            conn.execute("""
                INSERT INTO contact_profiles (phone, rut, nombre, fecha_nacimiento, updated_at)
                VALUES (?, ?, ?, ?, datetime('now'))
                ON CONFLICT(phone) DO UPDATE SET
                    rut=excluded.rut, nombre=excluded.nombre,
                    fecha_nacimiento=excluded.fecha_nacimiento,
                    updated_at=excluded.updated_at
            """, (phone, rut, nombre, fecha_nacimiento))
        else:
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


_PERFIL_CAMPOS = (
    "nombre", "fecha_nacimiento", "sexo", "email", "comuna", "direccion",
    "prevision", "contacto_emerg_nombre", "contacto_emerg_telefono",
)


def get_profile_full(phone: str) -> dict:
    """Retorna el perfil completo (todos los campos editables + phone + rut)."""
    cols = ["phone", "rut", *_PERFIL_CAMPOS, "updated_at"]
    with _conn() as conn:
        row = conn.execute(
            f"SELECT {', '.join(cols)} FROM contact_profiles WHERE phone=?",
            (phone,),
        ).fetchone()
        return dict(row) if row else {}


def update_profile_fields(phone: str, rut: str, data: dict) -> None:
    """Actualiza campos editables del perfil. Inserta si no existe."""
    campos = {k: data.get(k) for k in _PERFIL_CAMPOS if k in data}
    if not campos:
        return
    with _conn() as conn:
        # Asegurar que el registro exista
        conn.execute(
            "INSERT OR IGNORE INTO contact_profiles (phone, rut, updated_at) VALUES (?, ?, datetime('now'))",
            (phone, rut),
        )
        sets = ", ".join(f"{k}=?" for k in campos.keys())
        params = list(campos.values()) + [phone]
        conn.execute(
            f"UPDATE contact_profiles SET {sets}, updated_at=datetime('now') WHERE phone=?",
            params,
        )
        conn.commit()


_RUT_RE = __import__("re").compile(r'\b(\d{1,2}\.?\d{3}\.?\d{3}[-.\s]?[\dkK])\b')
_PALABRAS_BASURA = {
    "rut", "mi", "el", "la", "es", "soy", "para", "con", "de", "del", "al",
    "hola", "buenas", "tardes", "dias", "noches", "gracias", "favor",
    "nombre", "paciente", "atiende", "atender", "acompaname", "ayuda",
    "atencion", "porfavor", "por", "cel", "celular", "whatsapp", "numero",
    "telefono", "fono", "correo", "email", "hora", "cita", "reservar",
    "agendar", "cancelar", "quiero", "necesito", "puedo", "puede", "si", "no",
    "ok", "bueno", "dale", "claro",
}


def try_autocapture_rut_name(phone: str, text: str) -> dict | None:
    """Extrae RUT chileno + nombre aproximado de un mensaje libre y los asocia
    al teléfono si no tiene perfil completo. Pasivo y silencioso: nunca rompe
    el flujo. Retorna el perfil guardado (o None si no capturó nada).

    Casos típicos cubiertos:
      - "9.443.926-4 maría Parra pedrero"
      - "mi rut es 12345678-9 Juan Pérez"
      - "RUT: 12.345.678-9"
    """
    if not phone or not text:
        return None
    existing = get_profile(phone) or {}
    has_rut = bool((existing.get("rut") or "").strip())
    has_nombre = bool((existing.get("nombre") or "").strip())
    if has_rut and has_nombre:
        return None

    m = _RUT_RE.search(text)
    rut_fmt = None
    if m:
        raw = m.group(1)
        import re as _re
        digits = _re.sub(r'[^\dkK]', '', raw).upper()
        if 8 <= len(digits) <= 9:
            cuerpo, dv = digits[:-1], digits[-1]
            try:
                cuerpo_fmt = f"{int(cuerpo):,}".replace(",", ".")
                rut_fmt = f"{cuerpo_fmt}-{dv}"
                # Validar con valid_rut si está disponible
                try:
                    from medilink import valid_rut  # type: ignore
                    if not valid_rut(rut_fmt):
                        rut_fmt = None
                except Exception:
                    pass
            except Exception:
                rut_fmt = None
    if not rut_fmt:
        return None  # sin RUT válido no guardamos nada para no meter nombres sueltos

    # Extraer palabras candidato a nombre alrededor del RUT
    import re as _re
    around = (text[:m.start()] + " " + text[m.end():])
    tokens = _re.findall(r"[A-Za-zÁÉÍÓÚÑáéíóúñ]{2,}", around)
    filtrados = [t for t in tokens if t.lower() not in _PALABRAS_BASURA]
    # Tomar hasta 4 palabras consecutivas tras el primer match, con cap sensato
    if filtrados:
        nombre_candidato = " ".join(w.capitalize() for w in filtrados[:4])
    else:
        nombre_candidato = ""

    nombre_final = (existing.get("nombre") or "").strip() or nombre_candidato
    rut_final = rut_fmt
    if not nombre_final:
        # RUT sin nombre: guardar igual el RUT para poder cruzarlo en Medilink
        nombre_final = ""
    save_profile(phone, rut_final, nombre_final)
    try:
        log_event(phone, "autocapture_profile", {
            "rut": rut_final, "nombre": nombre_final[:60],
            "fuente": text[:120],
        })
    except Exception:
        pass
    return {"rut": rut_final, "nombre": nombre_final}


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
    y donde aún no se envió el recordatorio de 2 horas antes.

    Atómico: marca las citas como enviadas dentro de la misma transacción
    (BEGIN IMMEDIATE) para evitar duplicados cuando dos iteraciones del cron
    corren casi simultáneamente. Si el envío posterior falla, la cita queda
    marcada como enviada — preferimos no recordar que molestar con duplicados.
    """
    with _conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            rows = conn.execute(
                """SELECT c.*,
                          CASE WHEN c.paciente_nombre != '' THEN c.paciente_nombre
                               ELSE cp.nombre END AS paciente_nombre,
                          cp.nombre AS phone_owner
                   FROM citas_bot c
                   LEFT JOIN contact_profiles cp ON c.phone = cp.phone
                   WHERE c.fecha=? AND c.hora>=? AND c.hora<=?
                     AND (c.reminder_2h_sent IS NULL OR c.reminder_2h_sent=0)
                     AND (c.confirmation_status IS NULL OR c.confirmation_status != 'cancelar')""",
                (fecha, hora_min, hora_max),
            ).fetchall()
            citas = [dict(r) for r in rows]
            if citas:
                ids = [r["id"] for r in citas]
                placeholders = ",".join("?" for _ in ids)
                conn.execute(
                    f"UPDATE citas_bot SET reminder_2h_sent=1 WHERE id IN ({placeholders})",
                    ids,
                )
            conn.commit()
            return citas
        except Exception:
            conn.rollback()
            raise


def mark_reminder_2h_sent(cita_id: int):
    """Marca una cita como recordatorio 2h enviado."""
    with _conn() as conn:
        conn.execute("UPDATE citas_bot SET reminder_2h_sent=1 WHERE id=?", (cita_id,))
        conn.commit()


def get_next_cita_bot_by_phone(phone: str) -> dict | None:
    """Próxima cita futura (fecha >= hoy CLT) agendada por el bot para un teléfono.
    Incluye paciente_nombre resolviendo por fallback a contact_profiles."""
    hoy = datetime.now(ZoneInfo("America/Santiago")).date().isoformat()
    with _conn() as conn:
        row = conn.execute(
            """SELECT c.*,
                      CASE WHEN c.paciente_nombre != '' THEN c.paciente_nombre
                           ELSE cp.nombre END AS paciente_nombre,
                      cp.nombre AS phone_owner
               FROM citas_bot c
               LEFT JOIN contact_profiles cp ON c.phone = cp.phone
               WHERE c.phone=? AND c.fecha >= ?
               ORDER BY c.fecha ASC, c.hora ASC LIMIT 1""",
            (phone, hoy),
        ).fetchone()
        return dict(row) if row else None


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

def mark_admin_seen(phone: str, seen_by: str = "admin"):
    """Registra que la admin vio la conversación ahora. Persistente."""
    with _conn() as conn:
        conn.execute("""
            INSERT INTO admin_seen (phone, seen_at, seen_by)
            VALUES (?, datetime('now'), ?)
            ON CONFLICT(phone) DO UPDATE SET
                seen_at=datetime('now'), seen_by=excluded.seen_by
        """, (phone, seen_by))
        conn.commit()


def get_unread_counts() -> dict:
    """Retorna {phone: cantidad_mensajes_no_leidos} solo para inbound posteriores
    a admin_seen.seen_at (o todos los inbound si nunca se marcó)."""
    with _conn() as conn:
        rows = conn.execute("""
            SELECT m.phone, COUNT(*) as cnt
            FROM messages m
            LEFT JOIN admin_seen a ON a.phone = m.phone
            WHERE m.direction = 'in'
              AND (a.seen_at IS NULL OR m.ts > a.seen_at)
            GROUP BY m.phone
        """).fetchall()
        return {r["phone"]: r["cnt"] for r in rows}


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


def log_message(phone: str, direction: str, text: str, state: str = "IDLE",
                canal: str = "whatsapp", wamid: str | None = None):
    """Registra un mensaje entrante ('in') o saliente ('out') en el historial."""
    with _conn() as conn:
        conn.execute(
            "INSERT INTO messages (phone, direction, text, state, canal, wamid) VALUES (?, ?, ?, ?, ?, ?)",
            (phone, direction, str(text)[:2000], state, canal, wamid)
        )
        conn.commit()


def update_message_text_by_wamid(wamid: str, new_text: str) -> bool:
    """Actualiza el texto de un mensaje ya registrado y marca edited_at.
    Retorna True si se actualizó al menos una fila."""
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE messages SET text=?, edited_at=datetime('now') WHERE wamid=?",
            (str(new_text)[:2000], wamid)
        )
        conn.commit()
        return cur.rowcount > 0


def get_message_by_wamid(wamid: str) -> dict | None:
    """Retorna el mensaje con ese wamid, o None si no existe."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT id, phone, direction, text, state, ts, COALESCE(canal,'whatsapp') AS canal, wamid, edited_at "
            "FROM messages WHERE wamid=? LIMIT 1",
            (wamid,)
        ).fetchone()
        return dict(row) if row else None


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


def get_last_inbound_ts(phone: str) -> datetime | None:
    """Timestamp UTC del ultimo mensaje entrante del paciente. None si nunca escribio.
    Usado para detectar service window de 24h de Meta (donde se pueden enviar
    mensajes libres sin cobrar template)."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT ts FROM messages WHERE phone=? AND direction='in' "
            "ORDER BY ts DESC LIMIT 1",
            (phone,)
        ).fetchone()
    if not row or not row["ts"]:
        return None
    try:
        return datetime.fromisoformat(row["ts"]).replace(tzinfo=timezone.utc)
    except Exception:
        return None


def get_messages(phone: str, limit: int = 300) -> list[dict]:
    """Retorna los últimos `limit` mensajes de un número, ordenados cronológicamente
    (más antiguo primero, más reciente al final — lo que espera el panel para mostrar
    estilo WhatsApp). Antes usaba ORDER BY id ASC LIMIT N lo que devolvía los MÁS
    ANTIGUOS y cortaba los mensajes nuevos en conversaciones largas."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, phone, direction, text, state, ts, COALESCE(canal,'whatsapp') AS canal, "
            "wamid, edited_at FROM messages "
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


def get_cita_bot_by_id_for_rebook(id_cita: str) -> dict | None:
    """Retorna una cita agendada vía bot por id_cita, si aún no se notificó cancelación."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT phone, id_cita, especialidad, profesional, fecha, hora, modalidad, "
            "cancel_detected_at FROM citas_bot WHERE id_cita=? LIMIT 1",
            (id_cita,)
        ).fetchone()
        return dict(row) if row else None


def mark_cita_cancel_detected(id_cita: str):
    """Marca una cita como 'cancelación detectada y notificada' para evitar duplicados."""
    with _conn() as conn:
        conn.execute(
            "UPDATE citas_bot SET cancel_detected_at = datetime('now') WHERE id_cita=?",
            (id_cita,)
        )
        conn.commit()


def get_ultima_cita_paciente(phone: str) -> dict | None:
    """Retorna la última cita agendada por el paciente (por fecha más reciente).

    Usada para ofrecer Quick-book: "¿agendo otra hora con {profesional} de {especialidad}?"
    Retorna None si el paciente nunca agendó vía bot.
    """
    with _conn() as conn:
        row = conn.execute(
            "SELECT especialidad, profesional, fecha, hora, modalidad "
            "FROM citas_bot "
            "WHERE phone=? AND especialidad IS NOT NULL AND especialidad != '' "
            "ORDER BY datetime(fecha || ' ' || COALESCE(hora, '00:00')) DESC, id DESC "
            "LIMIT 1",
            (phone,)
        ).fetchone()
        return dict(row) if row else None


def get_conversion_funnel_by_especialidad(dias: int = 30) -> list[dict]:
    """Funnel de conversión por especialidad usando conversation_events.

    Etapas:
      - intents: pacientes que iniciaron agendamiento (intent_agendar)
      - confirmados: pacientes que confirmaron cita (cita_confirmada)

    Para cada especialidad retorna: intents, confirmados, tasa (%).
    Dato clave para decidir dónde invertir marketing y optimizar flujo.
    """
    with _conn() as conn:
        rows = conn.execute("""
            SELECT
                COALESCE(json_extract(meta, '$.especialidad'), '(sin especialidad)') AS esp,
                event,
                COUNT(DISTINCT phone) AS pacientes
            FROM conversation_events
            WHERE event IN ('intent_agendar', 'cita_confirmada')
              AND ts >= datetime('now', ?)
            GROUP BY esp, event
        """, (f"-{dias} days",)).fetchall()
        # Pivotar event → columnas
        por_esp: dict[str, dict] = {}
        for r in rows:
            esp = r["esp"] or "(sin especialidad)"
            por_esp.setdefault(esp, {"especialidad": esp, "intents": 0, "confirmados": 0})
            por_esp[esp][
                "intents" if r["event"] == "intent_agendar" else "confirmados"
            ] = r["pacientes"]
        # Calcular tasa y ordenar
        out = []
        for d in por_esp.values():
            intents = d["intents"]
            conf = d["confirmados"]
            d["tasa_conversion"] = round(conf / intents * 100, 1) if intents else 0.0
            d["drop_off"] = max(0, intents - conf)
            out.append(d)
        out.sort(key=lambda x: x["intents"], reverse=True)
        return out


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


def get_conversations(limit: int = 1000) -> list[dict]:
    """Lista todas las conversaciones con último mensaje y estado actual.

    Ordena por la última actividad real (mayor entre session.updated_at
    y messages.ts), para que un mensaje que no dispare save_session
    (p.ej. una pregunta FAQ en mitad del flujo) igual "suba" la
    conversación al tope del panel.
    """
    with _conn() as conn:
        rows = conn.execute("""
            SELECT
                ph.phone,
                COALESCE(s.state, 'IDLE') AS state,
                COALESCE(s.data, '{}')    AS data,
                CASE
                    WHEN m.ts IS NOT NULL AND (s.updated_at IS NULL OR m.ts > s.updated_at) THEN m.ts
                    ELSE s.updated_at
                END           AS updated_at,
                m.text        AS last_text,
                m.direction   AS last_dir,
                m.ts          AS last_ts,
                COALESCE(m.canal, 'whatsapp') AS canal,
                p.nombre,
                p.rut,
                (SELECT COUNT(*) FROM messages WHERE phone = ph.phone) AS msg_count
            FROM (
                SELECT phone FROM sessions
                UNION
                SELECT DISTINCT phone FROM messages
            ) ph
            LEFT JOIN sessions s ON s.phone = ph.phone
            LEFT JOIN messages m ON m.id = (
                SELECT id FROM messages WHERE phone = ph.phone ORDER BY id DESC LIMIT 1
            )
            LEFT JOIN contact_profiles p ON p.phone = ph.phone
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
    estados = ("WAIT_SLOT", "WAIT_MODALIDAD", "WAIT_RUT_AGENDAR", "WAIT_DATOS_NUEVO", "WAIT_NOMBRE_NUEVO")
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
    """True si no se envió este tipo de campaña en los últimos dias_cooldown días
    Y el paciente NO hizo opt-out ni revocó privacidad
    (compliance Ley 21.719 + WhatsApp Business Policy marketing)."""
    with _conn() as conn:
        # Hard-block: opt-out marketing
        opt_out = conn.execute(
            "SELECT 1 FROM contact_tags WHERE phone=? AND tag='marketing_opt_out'",
            (phone,)
        ).fetchone()
        if opt_out:
            return False
        # Hard-block: consentimiento revocado
        try:
            revoked = conn.execute(
                "SELECT 1 FROM privacy_consents WHERE phone=? AND revoked_at IS NOT NULL",
                (phone,)
            ).fetchone()
            if revoked:
                return False
        except Exception:
            pass  # tabla puede no existir en deploys antiguos
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
    Pacientes con cita de medicina/traumatología hace 1-14 días,
    sin cita de kinesiología reciente, sin cross-sell enviado en 21 días.
    Ventana ampliada (1-14d) tras detectar 0 candidatos con 1-5d.
    """
    with _conn() as conn:
        rows = conn.execute("""
            SELECT cb.phone, MAX(cb.fecha) AS ultima_fecha, cb.especialidad, p.nombre
            FROM citas_bot cb
            LEFT JOIN contact_profiles p ON p.phone = cb.phone
            WHERE cb.especialidad IN ('Medicina General', 'Medicina Familiar', 'Traumatología')
              AND cb.fecha >= date('now', '-14 days')
              AND cb.fecha <= date('now', '-1 days')
              AND NOT EXISTS (
                  SELECT 1 FROM citas_bot cb2
                  WHERE cb2.phone = cb.phone
                    AND cb2.especialidad LIKE 'Kinesiolog%'
                    AND cb2.fecha >= date('now', '-60 days')
              )
              AND NOT EXISTS (
                  SELECT 1 FROM fidelizacion_msgs f
                  WHERE f.phone = cb.phone AND f.tipo = 'crosssell_kine'
                    AND f.enviado_en >= datetime('now', '-21 days')
              )
            GROUP BY cb.phone
        """).fetchall()
        return [dict(r) for r in rows]


def get_crosssell_orl_fono_candidatos() -> list[dict]:
    """Cross-sell bidireccional ORL ↔ Fonoaudiología:
    - Paciente con cita de ORL en últimos 14d sin fono reciente → ofrecer fono
    - Paciente con cita de Fono en últimos 14d sin ORL reciente → ofrecer ORL
    Retorna lista con {phone, nombre, especialidad_origen, especialidad_destino}.
    """
    with _conn() as conn:
        rows = conn.execute("""
            SELECT cb.phone, cb.especialidad AS origen, MAX(cb.fecha) AS ultima_fecha, p.nombre
            FROM citas_bot cb
            LEFT JOIN contact_profiles p ON p.phone = cb.phone
            WHERE cb.especialidad LIKE 'Otorrinolaring%'
              AND cb.fecha >= date('now', '-14 days')
              AND cb.fecha <= date('now', '-1 days')
              AND NOT EXISTS (
                  SELECT 1 FROM citas_bot cb2
                  WHERE cb2.phone = cb.phone
                    AND cb2.especialidad LIKE 'Fonoaudiolog%'
                    AND cb2.fecha >= date('now', '-60 days')
              )
              AND NOT EXISTS (
                  SELECT 1 FROM fidelizacion_msgs f
                  WHERE f.phone = cb.phone AND f.tipo IN ('crosssell_orl_fono','crosssell_fono_orl')
                    AND f.enviado_en >= datetime('now', '-21 days')
              )
            GROUP BY cb.phone
            UNION ALL
            SELECT cb.phone, cb.especialidad AS origen, MAX(cb.fecha) AS ultima_fecha, p.nombre
            FROM citas_bot cb
            LEFT JOIN contact_profiles p ON p.phone = cb.phone
            WHERE cb.especialidad LIKE 'Fonoaudiolog%'
              AND cb.fecha >= date('now', '-14 days')
              AND cb.fecha <= date('now', '-1 days')
              AND NOT EXISTS (
                  SELECT 1 FROM citas_bot cb2
                  WHERE cb2.phone = cb.phone
                    AND cb2.especialidad LIKE 'Otorrinolaring%'
                    AND cb2.fecha >= date('now', '-60 days')
              )
              AND NOT EXISTS (
                  SELECT 1 FROM fidelizacion_msgs f
                  WHERE f.phone = cb.phone AND f.tipo IN ('crosssell_orl_fono','crosssell_fono_orl')
                    AND f.enviado_en >= datetime('now', '-21 days')
              )
            GROUP BY cb.phone
        """).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            origen = d.get("origen", "").lower()
            d["destino"] = "Fonoaudiología" if "otorrin" in origen else "Otorrinolaringología"
            out.append(d)
        return out


def get_crosssell_odonto_estetica_candidatos() -> list[dict]:
    """Pacientes con 2+ citas de odontología general en últimos 90d
    (higienista/limpieza frecuente) → candidatos a estética facial.
    Criterio: ya confían en el equipo dental, pueden probar estética."""
    with _conn() as conn:
        rows = conn.execute("""
            SELECT cb.phone, COUNT(*) AS n_citas, p.nombre, MAX(cb.fecha) AS ultima_fecha
            FROM citas_bot cb
            LEFT JOIN contact_profiles p ON p.phone = cb.phone
            WHERE cb.especialidad = 'Odontología General'
              AND cb.fecha >= date('now', '-90 days')
              AND cb.fecha <= date('now', '-2 days')
              AND NOT EXISTS (
                  SELECT 1 FROM citas_bot cb2
                  WHERE cb2.phone = cb.phone
                    AND cb2.especialidad = 'Estética Facial'
                    AND cb2.fecha >= date('now', '-180 days')
              )
              AND NOT EXISTS (
                  SELECT 1 FROM fidelizacion_msgs f
                  WHERE f.phone = cb.phone AND f.tipo = 'crosssell_odonto_estetica'
                    AND f.enviado_en >= datetime('now', '-60 days')
              )
            GROUP BY cb.phone
            HAVING n_citas >= 2
        """).fetchall()
        return [dict(r) for r in rows]


def get_crosssell_mg_chequeo_candidatos() -> list[dict]:
    """Pacientes con cita de MG hace 30-180d sin control/chequeo reciente
    → ofrecer chequeo preventivo (EMPAM, exámenes generales).
    Edad >=40 prioridad (alta prevalencia HTA/DM2/dislipidemia)."""
    with _conn() as conn:
        rows = conn.execute("""
            SELECT cb.phone, p.nombre, p.fecha_nacimiento, MAX(cb.fecha) AS ultima_fecha
            FROM citas_bot cb
            LEFT JOIN contact_profiles p ON p.phone = cb.phone
            WHERE cb.especialidad IN ('Medicina General','Medicina Familiar')
              AND cb.fecha >= date('now', '-180 days')
              AND cb.fecha <= date('now', '-30 days')
              AND NOT EXISTS (
                  SELECT 1 FROM citas_bot cb2
                  WHERE cb2.phone = cb.phone
                    AND cb2.especialidad IN ('Medicina General','Medicina Familiar')
                    AND cb2.fecha >= date('now', '-29 days')
              )
              AND NOT EXISTS (
                  SELECT 1 FROM fidelizacion_msgs f
                  WHERE f.phone = cb.phone AND f.tipo = 'crosssell_mg_chequeo'
                    AND f.enviado_en >= datetime('now', '-90 days')
              )
            GROUP BY cb.phone
        """).fetchall()
        return [dict(r) for r in rows]


def get_cumpleanos_hoy() -> list[dict]:
    """Pacientes cuya fecha_nacimiento coincide con el día y mes de hoy,
    sin mensaje de cumpleaños enviado en los últimos 330 días."""
    with _conn() as conn:
        rows = conn.execute("""
            SELECT p.phone, p.nombre, p.fecha_nacimiento, p.rut
            FROM contact_profiles p
            WHERE p.fecha_nacimiento IS NOT NULL
              AND strftime('%m-%d', p.fecha_nacimiento) = strftime('%m-%d', 'now')
              AND NOT EXISTS (
                  SELECT 1 FROM fidelizacion_msgs f
                  WHERE f.phone = p.phone AND f.tipo = 'cumpleanos'
                    AND f.enviado_en >= datetime('now', '-330 days')
              )
        """).fetchall()
        return [dict(r) for r in rows]


def get_pacientes_winback(dias_min: int = 91, dias_max: int = 365) -> list[dict]:
    """Pacientes cuya última cita fue entre dias_min y dias_max días atrás,
    sin mensaje de winback en los últimos 90 días, sin cita futura."""
    with _conn() as conn:
        rows = conn.execute("""
            SELECT cb.phone, MAX(cb.fecha) AS ultima_cita,
                   MAX(cb.especialidad) AS especialidad, p.nombre
            FROM citas_bot cb
            LEFT JOIN contact_profiles p ON p.phone = cb.phone
            WHERE cb.fecha <= date('now', ?)
              AND cb.fecha >= date('now', ?)
              AND NOT EXISTS (
                  SELECT 1 FROM citas_bot cb2
                  WHERE cb2.phone = cb.phone AND cb2.fecha > date('now')
              )
              AND NOT EXISTS (
                  SELECT 1 FROM fidelizacion_msgs f
                  WHERE f.phone = cb.phone AND f.tipo = 'winback'
                    AND f.enviado_en >= datetime('now', '-90 days')
              )
              AND NOT EXISTS (
                  SELECT 1 FROM fidelizacion_msgs f
                  WHERE f.phone = cb.phone AND f.tipo = 'reactivacion'
                    AND f.enviado_en >= datetime('now', '-30 days')
              )
            GROUP BY cb.phone
        """, (f"-{dias_min} days", f"-{dias_max} days")).fetchall()
        return [dict(r) for r in rows]


def get_nps_por_profesional(dias: int | None = None) -> dict:
    """NPS por profesional basado en respuestas postconsulta (mejor/igual/peor).
    NPS = (mejor - peor) / total * 100.  Retorna global + desglose por profesional."""
    with _conn() as conn:
        where = ""
        params: list = []
        if dias:
            where = "AND f.enviado_en >= datetime('now', ?)"
            params = [f"-{dias} days"]

        rows = conn.execute(f"""
            SELECT cb.profesional,
                   COUNT(*) AS total,
                   SUM(CASE WHEN f.respuesta = 'mejor' THEN 1 ELSE 0 END) AS mejor,
                   SUM(CASE WHEN f.respuesta = 'igual' THEN 1 ELSE 0 END) AS igual,
                   SUM(CASE WHEN f.respuesta = 'peor'  THEN 1 ELSE 0 END) AS peor
            FROM fidelizacion_msgs f
            INNER JOIN citas_bot cb ON cb.id_cita = f.cita_id AND cb.phone = f.phone
            WHERE f.tipo = 'postconsulta' AND f.respuesta IS NOT NULL
            {where}
            GROUP BY cb.profesional
            ORDER BY total DESC
        """, params).fetchall()

        profesionales = []
        global_mejor = global_igual = global_peor = 0
        for r in rows:
            d = dict(r)
            total = d["total"]
            mejor = d["mejor"]
            peor = d["peor"]
            d["nps"] = round((mejor - peor) / total * 100, 1) if total else 0
            profesionales.append(d)
            global_mejor += mejor
            global_igual += d["igual"]
            global_peor += peor

        global_total = global_mejor + global_igual + global_peor
        global_nps = round((global_mejor - global_peor) / global_total * 100, 1) if global_total else 0

        return {
            "dias": dias,
            "global_nps": global_nps,
            "global_total": global_total,
            "global_mejor": global_mejor,
            "global_igual": global_igual,
            "global_peor": global_peor,
            "por_profesional": profesionales,
        }


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


def get_case_study_report(dias: int = 30) -> dict:
    """Reporte consolidado de KPIs para caso de éxito / documentación."""
    with _conn() as conn:
        since = f"-{dias} days"

        # ── Funnel de agendamiento ────────────────────────────────────────
        def _count(ev):
            return conn.execute(
                "SELECT COUNT(*) FROM conversation_events "
                "WHERE event=? AND ts >= datetime('now', ?)", (ev, since)
            ).fetchone()[0]

        conversaciones = conn.execute(
            "SELECT COUNT(DISTINCT phone) FROM conversation_events "
            "WHERE ts >= datetime('now', ?)", (since,)
        ).fetchone()[0]

        intent_agendar = _count("intent_agendar")
        citas_creadas = _count("cita_creada")
        citas_canceladas = _count("cita_cancelada")
        citas_reagendadas = _count("cita_reagendada")
        sin_disponibilidad = _count("sin_disponibilidad")
        waitlist_inscritos = _count("waitlist_inscrito")
        waitlist_notificados = _count("waitlist_notificado")

        tasa_conversion = round(citas_creadas / intent_agendar * 100, 1) if intent_agendar else 0

        # ── Confirmaciones pre-cita ───────────────────────────────────────
        conf_rows = conn.execute("""
            SELECT confirmation_status, COUNT(*) as cnt
            FROM citas_bot
            WHERE created_at >= datetime('now', ?) AND confirmation_status IS NOT NULL
            GROUP BY confirmation_status
        """, (since,)).fetchall()
        confirmaciones = {r["confirmation_status"]: r["cnt"] for r in conf_rows}
        total_con_reminder = conn.execute(
            "SELECT COUNT(*) FROM citas_bot "
            "WHERE created_at >= datetime('now', ?) AND reminder_sent=1",
            (since,)
        ).fetchone()[0]
        tasa_confirmacion = round(
            confirmaciones.get("confirmed", 0) / total_con_reminder * 100, 1
        ) if total_con_reminder else 0

        # ── No-shows evitados (cancelaciones + reagendamientos pre-cita) ──
        noshows_evitados = confirmaciones.get("cancelar", 0) + confirmaciones.get("reagendar", 0)

        # ── Registro pacientes nuevos ─────────────────────────────────────
        reg_completos = _count("registro_completo")
        reg_abandonos = _count("registro_abandono")
        reg_total = reg_completos + reg_abandonos
        tasa_registro = round(reg_completos / reg_total * 100, 1) if reg_total else 0

        # ── Derivaciones a humano ─────────────────────────────────────────
        derivaciones = _count("derivado_humano")
        emergencias = _count("emergencia_detectada")
        crisis = _count("crisis_salud_mental")

        # ── Fidelización ──────────────────────────────────────────────────
        fidel_rows = conn.execute("""
            SELECT tipo, COUNT(*) as enviados,
                   COUNT(respuesta) as respondidos
            FROM fidelizacion_msgs
            WHERE enviado_en >= datetime('now', ?)
            GROUP BY tipo
        """, (since,)).fetchall()
        fidelizacion = {}
        for r in fidel_rows:
            env = r["enviados"]
            resp = r["respondidos"]
            fidelizacion[r["tipo"]] = {
                "enviados": env, "respondidos": resp,
                "tasa": round(resp / env * 100, 1) if env else 0
            }

        # Desglose postconsulta
        pc_rows = conn.execute("""
            SELECT respuesta, COUNT(*) as cnt FROM fidelizacion_msgs
            WHERE tipo='postconsulta' AND respuesta IS NOT NULL
            AND enviado_en >= datetime('now', ?)
            GROUP BY respuesta
        """, (since,)).fetchall()
        postconsulta_desglose = {r["respuesta"]: r["cnt"] for r in pc_rows}

        # ── Cross-sell / upsell ───────────────────────────────────────────
        upsell_ofrecidos = _count("upsell_postconsulta_ofrecido")
        upsell_aceptados = _count("upsell_postconsulta_acepto")
        faq_agendar_acepto = _count("faq_agendar_acepto")
        faq_agendar_rechazo = _count("faq_agendar_rechazo")

        # ── Referral (cómo nos conocieron) ────────────────────────────────
        ref_rows = conn.execute(
            "SELECT tag, COUNT(*) as cnt FROM contact_tags "
            "WHERE tag LIKE 'referido:%' AND ts >= datetime('now', ?) "
            "GROUP BY tag ORDER BY cnt DESC", (since,)
        ).fetchall()
        referral = {r["tag"].replace("referido:", ""): r["cnt"] for r in ref_rows}

        # ── Canales ───────────────────────────────────────────────────────
        canal_rows = conn.execute("""
            SELECT canal, COUNT(DISTINCT phone) as usuarios, COUNT(*) as mensajes
            FROM messages
            WHERE ts >= datetime('now', ?) AND direction='in'
            GROUP BY canal
        """, (since,)).fetchall()
        canales = {r["canal"]: {"usuarios": r["usuarios"], "mensajes": r["mensajes"]}
                   for r in canal_rows}

        # ── Triage GES ────────────────────────────────────────────────────
        triage_match = _count("triage_ges_match")
        triage_nomatch = _count("triage_ges_nomatch")

        # ── Horarios pico ─────────────────────────────────────────────────
        hora_rows = conn.execute("""
            SELECT CAST(strftime('%%H', ts) AS INTEGER) as hora,
                   COUNT(*) as msgs
            FROM messages
            WHERE ts >= datetime('now', ?) AND direction='in'
            GROUP BY hora ORDER BY msgs DESC LIMIT 5
        """, (since,)).fetchall()
        horas_pico = [{"hora": r["hora"], "mensajes": r["msgs"]} for r in hora_rows]

        # ── Especialidades más solicitadas ────────────────────────────────
        esp_rows = conn.execute("""
            SELECT json_extract(meta, '$.especialidad') as esp, COUNT(*) as cnt
            FROM conversation_events
            WHERE event='cita_creada' AND ts >= datetime('now', ?)
            AND json_extract(meta, '$.especialidad') IS NOT NULL
            GROUP BY esp ORDER BY cnt DESC
        """, (since,)).fetchall()
        especialidades_top = [{"especialidad": r["esp"], "citas": r["cnt"]}
                              for r in esp_rows]

        return {
            "periodo_dias": dias,
            "resumen": {
                "conversaciones_unicas": conversaciones,
                "citas_agendadas": citas_creadas,
                "citas_canceladas": citas_canceladas,
                "citas_reagendadas": citas_reagendadas,
                "tasa_conversion": f"{tasa_conversion}%",
                "pacientes_nuevos_registrados": reg_completos,
                "tasa_registro_completado": f"{tasa_registro}%",
                "derivaciones_humano": derivaciones,
                "emergencias_detectadas": emergencias,
            },
            "funnel_agendamiento": {
                "intent_agendar": intent_agendar,
                "citas_creadas": citas_creadas,
                "sin_disponibilidad": sin_disponibilidad,
                "waitlist_inscritos": waitlist_inscritos,
                "waitlist_notificados": waitlist_notificados,
                "tasa_conversion": f"{tasa_conversion}%",
            },
            "confirmacion_precita": {
                "total_recordatorios_enviados": total_con_reminder,
                "confirmados": confirmaciones.get("confirmed", 0),
                "reagendaron": confirmaciones.get("reagendar", 0),
                "cancelaron": confirmaciones.get("cancelar", 0),
                "tasa_confirmacion": f"{tasa_confirmacion}%",
                "noshows_evitados": noshows_evitados,
            },
            "fidelizacion": {
                "por_campana": fidelizacion,
                "postconsulta_desglose": postconsulta_desglose,
            },
            "crosssell": {
                "upsell_ofrecidos": upsell_ofrecidos,
                "upsell_aceptados": upsell_aceptados,
                "tasa_upsell": f"{round(upsell_aceptados / upsell_ofrecidos * 100, 1) if upsell_ofrecidos else 0}%",
                "faq_to_agendar_aceptados": faq_agendar_acepto,
                "faq_to_agendar_rechazados": faq_agendar_rechazo,
            },
            "registro_pacientes": {
                "completados": reg_completos,
                "abandonados": reg_abandonos,
                "tasa_completado": f"{tasa_registro}%",
            },
            "referral": referral,
            "canales": canales,
            "especialidades_top": especialidades_top,
            "horas_pico": horas_pico,
            "triage_ges": {
                "match": triage_match,
                "nomatch": triage_nomatch,
                "cobertura": f"{round(triage_match / (triage_match + triage_nomatch) * 100, 1) if (triage_match + triage_nomatch) else 0}%",
            },
            "seguridad": {
                "emergencias": emergencias,
                "crisis_salud_mental": crisis,
            },
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


def get_waitlist_by_especialidad(especialidad: str) -> list[dict]:
    """Retorna inscripciones activas para una especialidad (FIFO)."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM waitlist "
            "WHERE especialidad = ? AND notified_at IS NULL AND canceled_at IS NULL "
            "ORDER BY created_at ASC",
            (especialidad.lower(),)
        ).fetchall()
        return [dict(r) for r in rows]


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


# ── Portal del paciente — OTP ────────────────────────────────────────────────

def save_portal_otp(rut: str, phone: str, code: str):
    """Guarda un OTP para el portal del paciente."""
    with _conn() as conn:
        conn.execute(
            "INSERT INTO portal_otp (rut, phone, code) VALUES (?, ?, ?)",
            (rut, phone, code)
        )
        conn.commit()


def verify_portal_otp(rut: str, code: str) -> str | None:
    """Verifica un OTP. Retorna el phone si es válido, None si no.
    Válido = código correcto, < 5 min, no usado."""
    with _conn() as conn:
        # Limpiar expirados
        conn.execute(
            "DELETE FROM portal_otp WHERE created_at < datetime('now', '-10 minutes')"
        )
        row = conn.execute(
            """SELECT phone FROM portal_otp
               WHERE rut=? AND code=? AND used=0
               AND created_at >= datetime('now', '-5 minutes')
               ORDER BY created_at DESC LIMIT 1""",
            (rut, code)
        ).fetchone()
        if not row:
            return None
        # Marcar como usado
        conn.execute(
            "UPDATE portal_otp SET used=1 WHERE rut=? AND code=? AND used=0",
            (rut, code)
        )
        conn.commit()
        return row["phone"]


def count_portal_otps(rut: str, minutes: int = 60) -> int:
    """Cuenta OTPs enviados a un RUT en los últimos N minutos (rate limit)."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as c FROM portal_otp WHERE rut=? AND created_at >= datetime('now', ?)",
            (rut, f"-{minutes} minutes")
        ).fetchone()
        return row["c"]


# ── Portal del paciente — vinculaciones familiares ───────────────────────────

def add_family_link(owner_rut: str, dependent_rut: str, dependent_nombre: str,
                    relation: str, verification_method: str) -> int:
    """Crea una vinculación familiar. Si ya existe (revocada), la reactiva.
    verification_method: 'tutor_declaration' (menor) | 'otp' (adulto)."""
    with _conn() as conn:
        conn.execute(
            """INSERT INTO family_links
               (owner_rut, dependent_rut, dependent_nombre, relation, verification_method)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(owner_rut, dependent_rut) DO UPDATE SET
                   dependent_nombre=excluded.dependent_nombre,
                   relation=excluded.relation,
                   verification_method=excluded.verification_method,
                   verified_at=datetime('now'),
                   revoked_at=NULL""",
            (owner_rut, dependent_rut, dependent_nombre, relation, verification_method)
        )
        row = conn.execute(
            "SELECT id FROM family_links WHERE owner_rut=? AND dependent_rut=?",
            (owner_rut, dependent_rut)
        ).fetchone()
        conn.commit()
        return row["id"] if row else 0


def list_family_links(owner_rut: str) -> list[dict]:
    """Lista familiares activos (no revocados) de un titular."""
    with _conn() as conn:
        rows = conn.execute(
            """SELECT dependent_rut, dependent_nombre, relation, verification_method,
                      verified_at, created_at
               FROM family_links
               WHERE owner_rut=? AND revoked_at IS NULL
               ORDER BY created_at DESC""",
            (owner_rut,)
        ).fetchall()
        return [dict(r) for r in rows]


def revoke_family_link(owner_rut: str, dependent_rut: str) -> bool:
    """Marca como revocada una vinculación familiar. Retorna True si hubo cambio."""
    with _conn() as conn:
        cur = conn.execute(
            """UPDATE family_links SET revoked_at=datetime('now')
               WHERE owner_rut=? AND dependent_rut=? AND revoked_at IS NULL""",
            (owner_rut, dependent_rut)
        )
        conn.commit()
        return cur.rowcount > 0


def is_family_link(owner_rut: str, dependent_rut: str) -> bool:
    """True si existe una vinculación activa entre owner y dependent."""
    if owner_rut == dependent_rut:
        return True
    with _conn() as conn:
        row = conn.execute(
            """SELECT 1 FROM family_links
               WHERE owner_rut=? AND dependent_rut=? AND revoked_at IS NULL LIMIT 1""",
            (owner_rut, dependent_rut)
        ).fetchone()
        return row is not None


# ═══ Patient vitals (auto-monitoreo) ═══════════════════════════════════
_VITAL_TIPOS = {"presion", "glicemia", "peso", "temperatura"}


def add_vital(rut: str, tipo: str, valor: float, valor2: float | None = None,
              contexto: str | None = None, nota: str | None = None,
              ts: str | None = None) -> int:
    """Añade un registro de vital. Retorna el id."""
    if tipo not in _VITAL_TIPOS:
        raise ValueError(f"tipo inválido: {tipo}")
    ts = ts or __import__("datetime").datetime.utcnow().isoformat(timespec="seconds") + "Z"
    with _conn() as conn:
        cur = conn.execute(
            """INSERT INTO patient_vitals (rut, tipo, valor, valor2, contexto, nota, ts)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (rut, tipo, valor, valor2, contexto, nota, ts)
        )
        conn.commit()
        return cur.lastrowid


def list_vitals(rut: str, tipo: str | None = None, dias: int | None = None,
                limit: int = 200) -> list[dict]:
    """Lista vitals de un paciente, más recientes primero."""
    where = ["rut = ?"]
    params: list = [rut]
    if tipo:
        where.append("tipo = ?")
        params.append(tipo)
    if dias:
        where.append("ts >= datetime('now', ?)")
        params.append(f"-{int(dias)} days")
    where_sql = " AND ".join(where)
    params.append(limit)
    with _conn() as conn:
        rows = conn.execute(
            f"""SELECT id, rut, tipo, valor, valor2, contexto, nota, ts, created_at
                FROM patient_vitals
                WHERE {where_sql}
                ORDER BY ts DESC LIMIT ?""",
            tuple(params)
        ).fetchall()
        return [dict(r) for r in rows]


def delete_vital(rut: str, vital_id: int) -> bool:
    """Borra un registro de vital (solo si pertenece al RUT)."""
    with _conn() as conn:
        cur = conn.execute(
            "DELETE FROM patient_vitals WHERE id=? AND rut=?",
            (vital_id, rut)
        )
        conn.commit()
        return cur.rowcount > 0


def get_dx_tags(phone: str) -> list[str]:
    """Retorna los tags dx:* de un paciente, con nombres limpios."""
    tags = get_tags(phone)
    return [t.replace("dx:", "").upper() for t in tags if t.startswith("dx:")]


# ── Programa de referidos ────────────────────────────────────────────────────

def generate_referral_code(phone: str) -> str:
    """Genera un código de referido único para un paciente.
    Si ya tiene uno, retorna el existente."""
    import random
    import string as _string
    with _conn() as conn:
        existing = conn.execute(
            "SELECT code FROM referral_codes WHERE phone=?", (phone,)
        ).fetchone()
        if existing:
            return existing["code"]
        while True:
            code = "CMC-" + "".join(random.choices(
                _string.ascii_uppercase + _string.digits, k=4))
            dup = conn.execute(
                "SELECT 1 FROM referral_codes WHERE code=?", (code,)
            ).fetchone()
            if not dup:
                break
        conn.execute(
            "INSERT INTO referral_codes (phone, code) VALUES (?, ?)",
            (phone, code)
        )
        conn.commit()
        return code


def get_referral_code(phone: str) -> str | None:
    """Retorna el código de referido de un paciente, o None."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT code FROM referral_codes WHERE phone=?", (phone,)
        ).fetchone()
        return row["code"] if row else None


def validate_referral_code(code: str) -> dict | None:
    """Valida un código de referido. Retorna {phone, code} o None."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT phone, code FROM referral_codes WHERE code=?",
            (code.upper().strip(),)
        ).fetchone()
        return dict(row) if row else None


def use_referral_code(code: str, referred_phone: str) -> bool:
    """Registra el uso de un código de referido. Retorna True si es válido."""
    code = code.upper().strip()
    with _conn() as conn:
        ref = conn.execute(
            "SELECT phone FROM referral_codes WHERE code=?", (code,)
        ).fetchone()
        if not ref or ref["phone"] == referred_phone:
            return False
        existing = conn.execute(
            "SELECT 1 FROM referral_uses WHERE code=? AND referred_phone=?",
            (code, referred_phone)
        ).fetchone()
        if existing:
            return True
        conn.execute(
            "INSERT INTO referral_uses (code, referrer_phone, referred_phone) "
            "VALUES (?, ?, ?)",
            (code, ref["phone"], referred_phone)
        )
        conn.commit()
        return True


def get_referral_code_stats(dias: int = 30) -> dict:
    """Estadísticas del programa de referidos."""
    with _conn() as conn:
        since = f"-{dias} days"
        total_codes = conn.execute(
            "SELECT COUNT(*) FROM referral_codes"
        ).fetchone()[0]
        total_uses = conn.execute(
            "SELECT COUNT(*) FROM referral_uses "
            "WHERE created_at >= datetime('now', ?)", (since,)
        ).fetchone()[0]
        top = conn.execute("""
            SELECT rc.code, rc.phone, COUNT(ru.id) as referidos,
                   cp.nombre
            FROM referral_codes rc
            INNER JOIN referral_uses ru ON ru.code = rc.code
            LEFT JOIN contact_profiles cp ON cp.phone = rc.phone
            WHERE ru.created_at >= datetime('now', ?)
            GROUP BY rc.code
            ORDER BY referidos DESC LIMIT 10
        """, (since,)).fetchall()
        return {
            "dias": dias,
            "total_codigos": total_codes,
            "usos_periodo": total_uses,
            "top_referidores": [dict(r) for r in top],
        }


# ── Campañas estacionales ────────────────────────────────────────────────────

def save_campana_envio(phone: str, campana_id: str):
    """Registra un envío de campaña estacional."""
    with _conn() as conn:
        conn.execute(
            "INSERT INTO campanas_envios (phone, campana_id) VALUES (?, ?)",
            (phone, campana_id)
        )
        conn.commit()


def get_campana_envio_stats(campana_id: str | None = None) -> list[dict]:
    """Estadísticas de envíos de campañas estacionales."""
    with _conn() as conn:
        if campana_id:
            rows = conn.execute("""
                SELECT campana_id, COUNT(*) as enviados,
                       MIN(enviado_en) as primer_envio,
                       MAX(enviado_en) as ultimo_envio
                FROM campanas_envios WHERE campana_id = ?
                GROUP BY campana_id
            """, (campana_id,)).fetchall()
        else:
            rows = conn.execute("""
                SELECT campana_id, COUNT(*) as enviados,
                       MIN(enviado_en) as primer_envio,
                       MAX(enviado_en) as ultimo_envio
                FROM campanas_envios
                GROUP BY campana_id ORDER BY ultimo_envio DESC
            """).fetchall()
        return [dict(r) for r in rows]


def puede_enviar_campana_estacional(phone: str, campana_id: str,
                                     dias_cooldown: int = 30) -> bool:
    """True si no se ha enviado esta campaña al teléfono en los últimos N días."""
    with _conn() as conn:
        row = conn.execute("""
            SELECT 1 FROM campanas_envios
            WHERE phone = ? AND campana_id = ?
              AND enviado_en >= datetime('now', ?)
            LIMIT 1
        """, (phone, campana_id, f"-{dias_cooldown} days")).fetchone()
        return row is None


def get_segmented_phones(tags: list[str] | None = None,
                         dias_sin_visita: int | None = None) -> list[dict]:
    """Retorna pacientes que cumplen los criterios de segmentación.
    Retorna list[{phone, nombre}]."""
    with _conn() as conn:
        base = conn.execute("""
            SELECT DISTINCT cp.phone, cp.nombre
            FROM contact_profiles cp
            INNER JOIN messages m ON m.phone = cp.phone AND m.direction = 'in'
        """).fetchall()
        phones_map = {r["phone"]: r["nombre"] or "" for r in base}

        if tags:
            placeholders = ",".join("?" * len(tags))
            tag_phones = {r["phone"] for r in conn.execute(
                f"SELECT DISTINCT phone FROM contact_tags "
                f"WHERE tag IN ({placeholders})", tags
            ).fetchall()}
            phones_map = {p: n for p, n in phones_map.items() if p in tag_phones}

        if dias_sin_visita:
            cutoff = (datetime.now(timezone.utc) -
                      timedelta(days=dias_sin_visita)).strftime("%Y-%m-%d")
            active = {r["phone"] for r in conn.execute(
                "SELECT DISTINCT phone FROM citas_bot WHERE fecha >= ?",
                (cutoff,)
            ).fetchall()}
            phones_map = {p: n for p, n in phones_map.items()
                          if p not in active}

        return [{"phone": p, "nombre": n} for p, n in phones_map.items()]


def get_fidelizacion_trends(semanas: int = 4) -> list[dict]:
    """Retorna tendencias semanales de fidelización."""
    with _conn() as conn:
        rows = conn.execute("""
            SELECT
                strftime('%%Y-%%W', enviado_en) as semana,
                tipo,
                COUNT(*) as enviados,
                COUNT(respuesta) as respondidos
            FROM fidelizacion_msgs
            WHERE enviado_en >= datetime('now', ?)
            GROUP BY semana, tipo
            ORDER BY semana ASC
        """, (f"-{semanas * 7} days",)).fetchall()
        return [dict(r) for r in rows]


# ── Patient files (media recibido por WhatsApp) ──────────────────────────────

def save_patient_file(phone: str, filename: str, media_type: str,
                      mime_type: str, file_path: str, file_size: int,
                      caption: str = "") -> int:
    """Guarda referencia a un archivo recibido del paciente."""
    with _conn() as conn:
        cur = conn.execute("""
            INSERT INTO patient_files (phone, filename, media_type, mime_type,
                                       file_path, file_size, caption)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (phone, filename, media_type, mime_type, file_path, file_size, caption))
        conn.commit()
        return cur.lastrowid


def get_patient_files(phone: str, limit: int = 50) -> list[dict]:
    """Lista archivos de un paciente, más recientes primero."""
    with _conn() as conn:
        rows = conn.execute("""
            SELECT id, filename, media_type, mime_type, file_path,
                   file_size, caption, created_at
            FROM patient_files
            WHERE phone = ?
            ORDER BY created_at DESC
            LIMIT ?
        """, (phone, limit)).fetchall()
        return [dict(r) for r in rows]


def get_media_stats() -> dict:
    """Estadísticas de archivos media recibidos (imágenes, docs, etc.) — todo el historial."""
    with _conn() as conn:
        totals = conn.execute("""
            SELECT media_type, COUNT(*) as cnt
            FROM patient_files
            GROUP BY media_type
        """).fetchall()
        images = conn.execute("""
            SELECT pf.phone, cp.nombre, COUNT(*) as cnt,
                   MAX(pf.created_at) as last_at
            FROM patient_files pf
            LEFT JOIN contact_profiles cp ON cp.phone = pf.phone
            WHERE pf.media_type = 'image'
            GROUP BY pf.phone
            ORDER BY cnt DESC
        """).fetchall()
        recent = conn.execute("""
            SELECT pf.id, pf.phone, cp.nombre, pf.filename,
                   pf.created_at
            FROM patient_files pf
            LEFT JOIN contact_profiles cp ON cp.phone = pf.phone
            WHERE pf.media_type = 'image'
            ORDER BY pf.created_at DESC
            LIMIT 100
        """).fetchall()
        return {
            "totals": {r["media_type"]: r["cnt"] for r in totals},
            "images_by_patient": [dict(r) for r in images],
            "recent_images": [dict(r) for r in recent],
            "total_images": sum(r["cnt"] for r in images),
        }


def save_demanda_no_disponible(phone: str, solicitud: str,
                                tipo: str = "especialidad"):
    """Registra un especialista o examen solicitado que no tenemos."""
    with _conn() as conn:
        conn.execute("""
            INSERT INTO demanda_no_disponible (phone, solicitud, tipo)
            VALUES (?, ?, ?)
        """, (phone, solicitud, tipo))
        conn.commit()


def get_demanda_no_disponible(dias: int = 90) -> list[dict]:
    """Lista demanda de especialistas/exámenes no disponibles."""
    with _conn() as conn:
        since = (datetime.now(timezone.utc) - timedelta(days=dias)).strftime("%Y-%m-%d")
        rows = conn.execute("""
            SELECT d.id, d.phone, cp.nombre, d.solicitud, d.tipo,
                   d.created_at
            FROM demanda_no_disponible d
            LEFT JOIN contact_profiles cp ON cp.phone = d.phone
            ORDER BY d.created_at DESC
        """).fetchall()
        return [dict(r) for r in rows]


# ── Compliance Ley 19.628 — consentimiento + derecho al olvido ────────────────

# Versión actual de la política de privacidad. Cambiar aquí fuerza re-consent
# de todos los pacientes (cuando tengamos versionado de política).
PRIVACY_POLICY_VERSION = "1.0"


def has_privacy_consent(phone: str) -> bool:
    """True si el paciente aceptó explícitamente la política vigente.
    False si aún no respondió, si rechazó, o si revocó."""
    if not phone:
        return False
    with _conn() as conn:
        row = conn.execute(
            "SELECT status, consent_version, revoked_at FROM privacy_consents WHERE phone=?",
            (phone,)
        ).fetchone()
        if not row:
            return False
        if row["revoked_at"]:
            return False
        if row["status"] != "accepted":
            return False
        # Si cambió la versión, hay que re-consentir
        if row["consent_version"] != PRIVACY_POLICY_VERSION:
            return False
        return True


def get_privacy_consent(phone: str) -> dict | None:
    """Retorna el registro de consent completo (para auditoría)."""
    if not phone:
        return None
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM privacy_consents WHERE phone=?", (phone,)
        ).fetchone()
        return dict(row) if row else None


def save_privacy_consent(phone: str, status: str, method: str = "whatsapp"):
    """Registra la respuesta del paciente al opt-in.
    status ∈ {'accepted', 'declined', 'pending'}
    method ∈ {'whatsapp', 'admin', 'portal'}
    """
    assert status in ("accepted", "declined", "pending"), f"Invalid status: {status}"
    with _conn() as conn:
        conn.execute("""
            INSERT INTO privacy_consents (phone, status, consent_version, method, consented_at)
            VALUES (?, ?, ?, ?, datetime('now'))
            ON CONFLICT(phone) DO UPDATE SET
                status=excluded.status,
                consent_version=excluded.consent_version,
                method=excluded.method,
                consented_at=excluded.consented_at,
                revoked_at=NULL
        """, (phone, status, PRIVACY_POLICY_VERSION, method))
        conn.commit()


def revoke_privacy_consent(phone: str):
    """Revoca el consent (marca revoked_at). Útil si el paciente escribe 'stop'."""
    with _conn() as conn:
        conn.execute(
            "UPDATE privacy_consents SET revoked_at=datetime('now') WHERE phone=?",
            (phone,)
        )
        conn.commit()


def log_gdpr_deletion(rut: str | None, phone: str | None, summary: dict,
                      deleted_by: str = "admin"):
    """Registra la ejecución de un borrado en cascada (art. 12 Ley 19.628).
    Esta tabla NO debe borrarse nunca — es la prueba legal de cumplimiento."""
    import json as _json
    with _conn() as conn:
        conn.execute(
            "INSERT INTO gdpr_deletions (rut, phone, deleted_by, summary) VALUES (?, ?, ?, ?)",
            (rut, phone, deleted_by, _json.dumps(summary, ensure_ascii=False))
        )
        conn.commit()


# Tablas con PII keyed por `phone` — usadas en el endpoint de borrado en cascada.
# Mantener sincronizado cuando se agregue una tabla nueva con phone.
_PII_TABLES_BY_PHONE = [
    "sessions",
    "contact_tags",
    "citas_bot",
    "conversation_events",
    "contact_profiles",
    "messages",
    "fidelizacion_msgs",
    "intent_queue",
    "waitlist",
    "message_statuses",
    "bsuid_map",
    "contact_notes",
    "portal_otp",
    "referral_codes",
    "campanas_envios",
    "patient_files",
    "demanda_no_disponible",
    "privacy_consents",
]


def delete_patient_data(phone: str | None, rut: str | None,
                        id_paciente_medilink: int | None = None,
                        deleted_by: str = "admin") -> dict:
    """Borra en cascada TODOS los datos del paciente en tablas de nuestro sistema.
    Transacción atómica. Registra el resumen en gdpr_deletions (inmutable).

    Args:
        phone: teléfono normalizado (sin '+'); si None se intenta resolver por rut.
        rut: RUT del paciente (para tablas que lo usan).
        id_paciente_medilink: id_paciente de Medilink, opcional — si se provee
                              también borra de citas_cache/ortodoncia_cache/kine_tracking.
        deleted_by: quién ejecutó el borrado (para audit log).

    Returns:
        dict {tabla: filas_borradas, ..., 'total': N}
    """
    import shutil as _shutil
    import json as _json
    from pathlib import Path as _Path

    # Resolución inversa phone ↔ rut
    resolved_phone = phone
    resolved_rut = rut
    if not resolved_phone and rut:
        resolved_phone = get_phone_by_rut(rut)
    if not resolved_rut and phone:
        prof = get_profile(phone)
        if prof:
            resolved_rut = prof.get("rut")

    deleted = {}
    with _conn() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            # Borrado por phone
            if resolved_phone:
                for table in _PII_TABLES_BY_PHONE:
                    cur = conn.execute(f"DELETE FROM {table} WHERE phone=?", (resolved_phone,))
                    if cur.rowcount:
                        deleted[table] = cur.rowcount
                # referral_uses tiene 2 columnas de phone
                cur = conn.execute(
                    "DELETE FROM referral_uses WHERE referrer_phone=? OR referred_phone=?",
                    (resolved_phone, resolved_phone)
                )
                if cur.rowcount:
                    deleted["referral_uses"] = cur.rowcount
            # Borrado adicional por rut (tablas que lo usan además de phone)
            if resolved_rut:
                cur = conn.execute("DELETE FROM portal_otp WHERE rut=?", (resolved_rut,))
                if cur.rowcount:
                    deleted["portal_otp"] = deleted.get("portal_otp", 0) + cur.rowcount
                cur = conn.execute("DELETE FROM waitlist WHERE rut=?", (resolved_rut,))
                if cur.rowcount:
                    deleted["waitlist"] = deleted.get("waitlist", 0) + cur.rowcount
            # Borrado por id_paciente Medilink (caches locales)
            if id_paciente_medilink:
                for table in ("citas_cache", "ortodoncia_cache", "kine_tracking"):
                    try:
                        cur = conn.execute(
                            f"DELETE FROM {table} WHERE id_paciente=?",
                            (id_paciente_medilink,)
                        )
                        if cur.rowcount:
                            deleted[table] = cur.rowcount
                    except _OPERATIONAL_ERRORS:
                        pass
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    # Borrar archivos físicos (fuera de la transacción SQL)
    if resolved_phone:
        upload_dir = _Path(__file__).parent.parent / "data" / "uploads" / resolved_phone
        if upload_dir.exists():
            try:
                _shutil.rmtree(upload_dir)
                deleted["uploaded_files_dir"] = str(upload_dir)
            except OSError as e:
                log.warning("No pude borrar %s: %s", upload_dir, e)

    deleted["total_rows_deleted"] = sum(
        v for v in deleted.values() if isinstance(v, int)
    )

    # Audit log inmutable
    log_gdpr_deletion(
        rut=resolved_rut,
        phone=resolved_phone,
        summary=deleted,
        deleted_by=deleted_by,
    )
    log.info("GDPR delete executed: phone=%s rut=%s summary=%s",
             resolved_phone, resolved_rut, deleted)
    return deleted
