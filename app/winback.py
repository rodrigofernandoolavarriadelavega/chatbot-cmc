"""Winback — campanas de reactivación de pacientes inactivos.

Conecta bi.v_winback_cohortes_contactables (Postgres BI) con Meta Cloud API
para enviar Message Templates aprobados a pacientes que no han vuelto al CMC.

Reglas duras:
- WINBACK_ACTIVE=false en .env → NO envía nada (flag de seguridad).
- Max 200 mensajes por día (rate limit duro).
- 30 segundos entre envíos (respetar rate limit Meta).
- Solo L-V 10:00-19:00 hora Chile.
- Excluye pacientes con cita futura en sessions.db (citas_bot).
- Excluye opt-outs (bi.opt_outs_marketing).
- Privacidad: especialidades sensibles usan template genérico sin mencionar especialidad.
- Footer "responde BAJA" obligatorio (en todos los templates).
- Outbound SIEMPRE desde +56966610737 (via Meta Cloud API). Nunca +56987834148.
- Solo enviar a phones con consentimiento explícito en bi.marketing_consent.
"""
import asyncio
import logging
import os
import re
import time
from datetime import datetime, date
from zoneinfo import ZoneInfo

import httpx
import psycopg2
import psycopg2.pool

log = logging.getLogger("bot")

TZ_CHILE = ZoneInfo("America/Santiago")

# ── Feature flag ─────────────────────────────────────────────────────────────
WINBACK_ACTIVE = os.getenv("WINBACK_ACTIVE", "false").lower() in ("true", "1", "yes")

# ── Meta API config ───────────────────────────────────────────────────────────
_META_ACCESS_TOKEN    = os.getenv("META_ACCESS_TOKEN", "")
_META_PHONE_NUMBER_ID = os.getenv("META_PHONE_NUMBER_ID", "")
# META_WABA_ID disponible directo en .env — evita round-trip a Graph API.
_META_WABA_ID         = os.getenv("META_WABA_ID", "")

# ── Caché de approval de templates (TTL 30 min) ───────────────────────────────
_template_approval_cache: dict[str, tuple[bool, float]] = {}
_TEMPLATE_CACHE_TTL = 1800  # segundos


async def _get_waba_id() -> str | None:
    """Retorna el WABA ID. Usa META_WABA_ID del .env si está disponible;
    si no, lo consulta dinámicamente al phone number endpoint."""
    if _META_WABA_ID:
        return _META_WABA_ID
    if not _META_ACCESS_TOKEN or not _META_PHONE_NUMBER_ID:
        return None
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(
                f"https://graph.facebook.com/v22.0/{_META_PHONE_NUMBER_ID}",
                params={"fields": "whatsapp_business_account", "access_token": _META_ACCESS_TOKEN},
            )
        if r.status_code == 200:
            data = r.json()
            waba = data.get("whatsapp_business_account", {})
            return waba.get("id") if waba else None
    except Exception as e:
        log.warning("winback: no pude obtener WABA ID: %s", e)
    return None


async def is_template_approved(template_name: str) -> bool:
    """Verifica si un template está APPROVED en Meta Business Manager.

    Usa caché con TTL de 30 min para no golpear la API en cada envío.
    Si falla la consulta, retorna False (fail-safe: no envía con template no verificado).
    """
    now = time.monotonic()
    cached = _template_approval_cache.get(template_name)
    if cached and (now - cached[1]) < _TEMPLATE_CACHE_TTL:
        return cached[0]

    waba_id = await _get_waba_id()
    if not waba_id or not _META_ACCESS_TOKEN:
        log.warning("winback: is_template_approved: sin WABA ID o token — fail-safe False")
        return False

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"https://graph.facebook.com/v22.0/{waba_id}/message_templates",
                params={
                    "name": template_name,
                    "fields": "name,status",
                    "access_token": _META_ACCESS_TOKEN,
                },
            )
        if r.status_code != 200:
            log.warning("winback: Meta template API status=%s para %s", r.status_code, template_name)
            _template_approval_cache[template_name] = (False, now)
            return False
        data = r.json()
        templates = data.get("data", [])
        approved = any(
            t.get("name") == template_name and t.get("status") == "APPROVED"
            for t in templates
        )
        _template_approval_cache[template_name] = (approved, now)
        if not approved:
            log.warning(
                "winback: WARN winback_v2_not_approved template=%s status=%s",
                template_name,
                next((t.get("status") for t in templates if t.get("name") == template_name),
                     "NOT_FOUND"),
            )
        return approved
    except Exception as e:
        log.warning("winback: error verificando approval de %s: %s", template_name, e)
        _template_approval_cache[template_name] = (False, now)
        return False


# ── Conexion BI ──────────────────────────────────────────────────────────────
_BI_HOST     = os.getenv("BI_DB_HOST", "127.0.0.1")
_BI_PORT     = int(os.getenv("BI_DB_PORT", "5432"))
_BI_NAME     = os.getenv("BI_DB_NAME", "health_bi")
_BI_USER     = os.getenv("BI_DB_USER", "health_user")
_BI_PASSWORD = os.getenv("BI_DB_PASSWORD", "password123")

_bi_pool: psycopg2.pool.ThreadedConnectionPool | None = None


def _get_bi_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _bi_pool
    if _bi_pool is None:
        _bi_pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=1, maxconn=4,
            host=_BI_HOST, port=_BI_PORT,
            dbname=_BI_NAME, user=_BI_USER, password=_BI_PASSWORD,
            connect_timeout=5,
        )
        log.info("winback: BI pool inicializado (%s:%s/%s)", _BI_HOST, _BI_PORT, _BI_NAME)
    return _bi_pool


def bi_conn():
    """Context manager para conexion BI con devolución al pool."""
    import contextlib

    @contextlib.contextmanager
    def _cm():
        pool = _get_bi_pool()
        conn = pool.getconn()
        try:
            yield conn
        finally:
            pool.putconn(conn)

    return _cm()


# ── Privacidad: especialidades sensibles no se mencionan ──────────────────────
_ESPECIALIDADES_SENSIBLES = {
    "psicología", "psicologia",
    "ginecología", "ginecologia",
    "gineco-obstetra",
    "ecografía", "ecografia",
    "matrona",
    "tecnólogo médico ecografista", "tecnologo medico ecografista",
}

def es_sensible(especialidad: str | None) -> bool:
    """Retorna True si la especialidad no debe mencionarse en mensajes outbound."""
    if not especialidad:
        return True  # sin info → tratar como sensible por defecto
    return especialidad.lower().strip() in _ESPECIALIDADES_SENSIBLES


# ── Mapping especialidad → template Meta ─────────────────────────────────────
# Usando templates _v2 que tienen el código de área (41) correcto.
# Los _v1 tenían (44) — número equivocado, NO usar aunque estén APPROVED.
# Si el _v2 correspondiente no está APPROVED en Meta, el envío se saltea.
# Nunca fallback a _v1.
_TEMPLATE_MAP: dict[str, str] = {
    "medicina general":          "winback_medicina_general_v2",
    "medicina interna":          "winback_medicina_general_v2",
    "kinesiología":              "winback_kinesiologia_v2",
    "kinesiologia":              "winback_kinesiologia_v2",
    "otorrinolaringología":      "winback_otorrino_v2",
    "otorrinolaringologia":      "winback_otorrino_v2",
    "odontología general":       "winback_odontologia_v2",
    "odontologia general":       "winback_odontologia_v2",
    "nutrición":                 "winback_medicina_general_v2",   # sin template propio aún
    "nutricion":                 "winback_medicina_general_v2",
    "podología":                 "winback_medicina_general_v2",
    "podologia":                 "winback_medicina_general_v2",
    "fonoaudiología":            "winback_medicina_general_v2",
    "fonoaudiologia":            "winback_medicina_general_v2",
    "cardiología":               "winback_medicina_general_v2",
    "cardiologia":               "winback_medicina_general_v2",
    "traumatología y ortopedia": "winback_medicina_general_v2",
    "traumatologia y ortopedia": "winback_medicina_general_v2",
    "gastroenterología":         "winback_medicina_general_v2",
    "gastroenterologia":         "winback_medicina_general_v2",
    "ortodoncista":              "winback_odontologia_v2",
    "implantología":             "winback_odontologia_v2",
    "implantologia":             "winback_odontologia_v2",
}

_TEMPLATE_SENSIBLE   = "winback_generico_sensible_v2"
_TEMPLATE_ONE_SHOT   = "winback_one_shot_general_v2"

def get_template(especialidad: str | None, cohorte: str) -> str:
    """Selecciona template según privacidad y cohorte.

    Cohorte 365d → one_shot_general sin importar especialidad.
    Especialidad sensible → generico_sensible.
    Resto → template específico (o fallback medicina_general_v2).
    """
    if cohorte == "365d":
        return _TEMPLATE_ONE_SHOT
    if es_sensible(especialidad):
        return _TEMPLATE_SENSIBLE
    key = (especialidad or "").lower().strip()
    return _TEMPLATE_MAP.get(key, "winback_medicina_general_v2")


def get_arancel(especialidad: str | None) -> int:
    """Retorna arancel estimado en CLP para CAPI Purchase value (delegado a config)."""
    from config import get_arancel_cpl
    return get_arancel_cpl(especialidad)


# ── marketing_consent ────────────────────────────────────────────────────────

def has_marketing_consent(phone: str) -> bool:
    """Retorna True si phone tiene status='accepted' en bi.marketing_consent."""
    try:
        with bi_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT status FROM bi.marketing_consent WHERE phone = %s",
                    (phone,)
                )
                row = cur.fetchone()
                return row is not None and row[0] == "accepted"
    except Exception as e:
        log.warning("winback: has_marketing_consent error %s: %s", phone[-4:], e)
        return False


def registrar_consent_enviado(phone: str) -> None:
    """Inserta o actualiza bi.marketing_consent con status='pending' al enviar el consent template."""
    try:
        with bi_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO bi.marketing_consent (phone, status, consent_sent_at)
                    VALUES (%s, 'pending', NOW())
                    ON CONFLICT (phone) DO UPDATE
                        SET status = 'pending',
                            consent_sent_at = NOW()
                        WHERE bi.marketing_consent.status NOT IN ('accepted', 'declined')
                    """,
                    (phone,)
                )
            conn.commit()
    except Exception as e:
        log.warning("winback: registrar_consent_enviado error %s: %s", phone[-4:], e)


def registrar_consent_respuesta(phone: str, status: str, method: str) -> None:
    """Actualiza bi.marketing_consent con la respuesta del paciente.

    status: 'accepted' | 'declined'
    method: 'quick_reply_button' | 'text_si' | 'text_no'
    """
    if status not in ("accepted", "declined"):
        return
    try:
        with bi_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO bi.marketing_consent
                        (phone, status, response_at, response_method)
                    VALUES (%s, %s, NOW(), %s)
                    ON CONFLICT (phone) DO UPDATE
                        SET status = EXCLUDED.status,
                            response_at = NOW(),
                            response_method = EXCLUDED.response_method
                    """,
                    (phone, status, method)
                )
            conn.commit()
        log.info("winback: marketing_consent status=%s phone=...%s method=%s",
                 status, phone[-4:], method)
    except Exception as e:
        log.warning("winback: registrar_consent_respuesta error %s: %s", phone[-4:], e)


def phone_in_opt_out(phone: str) -> bool:
    """Retorna True si el phone está en bi.opt_outs_marketing."""
    try:
        with bi_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM bi.opt_outs_marketing WHERE phone = %s",
                    (phone,)
                )
                return cur.fetchone() is not None
    except Exception as e:
        log.warning("winback: phone_in_opt_out error %s: %s", phone[-4:], e)
        return False


# ── Horario permitido ─────────────────────────────────────────────────────────
def _dentro_horario_permitido() -> bool:
    """L-V 10:00-19:00 hora Chile."""
    now = datetime.now(TZ_CHILE)
    if now.weekday() >= 5:  # sábado=5, domingo=6
        return False
    return 10 <= now.hour < 19


# ── Conteo diario enviados ────────────────────────────────────────────────────
_LIMITE_DIARIO = 200

def _enviados_hoy() -> int:
    """Cuántos winbacks se enviaron hoy (desde bi.winback_envios)."""
    hoy = date.today().isoformat()
    with bi_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM bi.winback_envios "
                "WHERE DATE(enviado_at) = %s",
                (hoy,)
            )
            return cur.fetchone()[0]


# ── Candidatos del día ────────────────────────────────────────────────────────
def get_candidatos_dia(cohorte: str, limite: int = 200) -> list[dict]:
    """Obtiene pacientes contactables para una cohorte, sin cita futura.

    Solo phones con consentimiento explícito de marketing aceptado.
    Excluye:
    - Opt-outs (ya excluidos en la vista).
    - Pacientes que ya recibieron un winback en los últimos 90 días.
    - Pacientes sin consent en bi.marketing_consent.
    - Pacientes con cita futura en citas_bot (sessions.db) — consultado en memoria.
    """
    with bi_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    wc.paciente_id,
                    wc.nombre,
                    wc.apellido,
                    wc.telefono,
                    wc.genero,
                    wc.ultima_atencion,
                    wc.ultima_especialidad,
                    wc.ultimo_profesional,
                    wc.dias_inactivo,
                    wc.cohorte,
                    wc.edad
                FROM bi.v_winback_cohortes_contactables wc
                -- Solo phones con consentimiento explícito de marketing
                INNER JOIN bi.marketing_consent mc
                    ON mc.phone = wc.telefono
                   AND mc.status = 'accepted'
                WHERE wc.cohorte = %s
                  AND NOT EXISTS (
                      SELECT 1 FROM bi.winback_envios we
                      WHERE we.telefono = wc.telefono
                        AND we.enviado_at > NOW() - INTERVAL '90 days'
                  )
                ORDER BY wc.dias_inactivo ASC
                LIMIT %s
                """,
                (cohorte, limite),
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]


# ── Excluir phones con cita futura en sessions.db ────────────────────────────
def _phones_con_cita_futura() -> set[str]:
    """Retorna set de teléfonos que ya tienen cita futura agendada (citas_bot)."""
    try:
        from pathlib import Path
        db_path = Path(__file__).parent.parent / "data" / "sessions.db"

        # sessions.db puede estar cifrado con SQLCipher
        sqlcipher_key = os.getenv("SQLCIPHER_KEY", "").strip()
        hoy = date.today().isoformat()

        if sqlcipher_key:
            try:
                from sqlcipher3 import dbapi2 as sc
                conn = sc.connect(str(db_path), timeout=5)
                conn.execute(
                    f"PRAGMA key=\"x'{sqlcipher_key}'\";"
                )
            except ImportError:
                import sqlite3
                conn = sqlite3.connect(str(db_path), timeout=5)
        else:
            import sqlite3
            conn = sqlite3.connect(str(db_path), timeout=5)

        try:
            # BUG-2 fix 2026-05-18: citas_bot no tiene columna 'estado'.
            # cancel_detected_at IS NULL excluye citas detectadas como anuladas
            # (la única señal de cancelación persistida en el schema real).
            rows = conn.execute(
                "SELECT DISTINCT phone FROM citas_bot "
                "WHERE fecha >= ? AND cancel_detected_at IS NULL",
                (hoy,)
            ).fetchall()
            return {r[0] for r in rows}
        finally:
            conn.close()
    except Exception as e:
        log.warning("winback: no pude leer citas_bot: %s", e)
        return set()


# ── Lookup event-driven (un solo paciente por phone) ─────────────────────────

# Número personal del Dr. Olavarría — excluido hardcoded por defensa.
_PERSONAL_PHONE_DR = "56987834148"


def _is_staff_phone(tel_digits: str) -> bool:
    """Retorna True si el teléfono normalizado a solo dígitos pertenece a staff o al
    número personal del médico. Si es staff, se loguea WARN y el llamador retorna None.

    Compara por sufijo de 9 dígitos para tolerar formatos mixtos.
    """
    from config import STAFF_PHONES as _STAFF_PHONES
    suffix9 = tel_digits[-9:]
    # Chequeo número personal hardcoded
    if tel_digits == _PERSONAL_PHONE_DR or suffix9 == _PERSONAL_PHONE_DR[-9:]:
        log.warning(
            "winback: WARN staff_phone_excluded — phone personal Dr. Olavarría bloqueado de marketing"
        )
        return True
    # Chequeo STAFF_PHONES (dict {"56912345678": "Nombre"})
    for staff_num in _STAFF_PHONES:
        staff_digits = "".join(c for c in staff_num if c.isdigit())
        if not staff_digits:
            continue
        if staff_digits == tel_digits or staff_digits[-9:] == suffix9:
            log.warning(
                "winback: WARN staff_phone_excluded — phone=...%s (%s) bloqueado de marketing",
                suffix9[-4:], _STAFF_PHONES[staff_num],
            )
            return True
    return False


def get_candidato_por_phone(phone: str) -> dict | None:
    """Busca datos de un paciente por teléfono usando cascada de 4 fuentes.

    Orden (primer hit gana, nombre real siempre preferido sobre fallback):

    1. bi.v_winback_cohortes_contactables — candidato ideal con cohorte, especialidad.
    2. bi.dim_paciente directo — para pacientes en BI excluidos de la vista (cohorte fuera
       de rango, winback reciente, etc.). Retorna cohorte='unknown'.
    3. contact_profiles (sessions.db SQLCipher) — pacientes conocidos por interacción
       con el bot que no están en BI.
    4. bi.marketing_consent — al menos tenemos el phone; retorna nombre='Paciente'
       como ultimísimo fallback y loguea WARN.

    En todos los casos: si el phone normalizado coincide con STAFF_PHONES o el número
    personal del médico (+56987834148), retorna None de inmediato (nunca marketing a staff).

    Normalización: compara por últimos 9 dígitos para tolerar formatos mixtos
    (+56966373231 vs 56966373231 vs 966373231, etc.).
    """
    tel = phone.lstrip("+").strip()
    tel_digits = "".join(c for c in tel if c.isdigit())
    if len(tel_digits) < 6:
        return None
    tel_suffix = tel_digits[-9:]

    # Gate: nunca marketing a staff ni al número personal
    if _is_staff_phone(tel_digits):
        return None

    # ── Fuente 1: v_winback_cohortes_contactables ─────────────────────────────
    try:
        with bi_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        paciente_id, nombre, apellido, telefono,
                        genero, ultima_atencion, ultima_especialidad,
                        ultimo_profesional, dias_inactivo, cohorte, edad
                    FROM bi.v_winback_cohortes_contactables
                    WHERE RIGHT(regexp_replace(telefono, '[^0-9]', '', 'g'), 9) = %s
                      AND cohorte IS NOT NULL
                    ORDER BY dias_inactivo ASC
                    LIMIT 1
                    """,
                    (tel_suffix,),
                )
                row = cur.fetchone()
                if row:
                    cols = [
                        "paciente_id", "nombre", "apellido", "telefono",
                        "genero", "ultima_atencion", "ultima_especialidad",
                        "ultimo_profesional", "dias_inactivo", "cohorte", "edad",
                    ]
                    return dict(zip(cols, row))
    except Exception as e:
        log.warning("winback: get_candidato_por_phone fuente1 error phone=...%s: %s", tel_suffix[-4:], e)

    # ── Fuente 2: bi.dim_paciente directo ─────────────────────────────────────
    try:
        with bi_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT paciente_id, nombre, apellido, telefono
                    FROM bi.dim_paciente
                    WHERE RIGHT(regexp_replace(telefono, '[^0-9]', '', 'g'), 9) = %s
                    ORDER BY updated_at DESC NULLS LAST
                    LIMIT 1
                    """,
                    (tel_suffix,),
                )
                row = cur.fetchone()
                if row:
                    log.info(
                        "winback: get_candidato_por_phone fuente2 (dim_paciente) phone=...%s nombre=%s",
                        tel_suffix[-4:], (row[1] or "").strip()[:20],
                    )
                    return {
                        "paciente_id": row[0],
                        "nombre": row[1],
                        "apellido": row[2],
                        "telefono": row[3] or phone,
                        "genero": None,
                        "ultima_atencion": None,
                        "ultima_especialidad": None,
                        "ultimo_profesional": None,
                        "dias_inactivo": None,
                        "cohorte": "unknown",
                        "edad": None,
                    }
    except Exception as e:
        log.warning("winback: get_candidato_por_phone fuente2 error phone=...%s: %s", tel_suffix[-4:], e)

    # ── Fuente 3: contact_profiles (sessions.db) ──────────────────────────────
    try:
        from session import _conn as _session_conn
        phone_variants = (phone, "+" + phone, tel_digits, "56" + tel_suffix, tel_suffix)
        # Eliminar duplicados manteniendo orden
        seen: set[str] = set()
        variants_dedup: list[str] = []
        for v in phone_variants:
            if v not in seen:
                seen.add(v)
                variants_dedup.append(v)
        placeholders = ",".join("?" * len(variants_dedup))
        with _session_conn() as conn:
            row = conn.execute(
                f"SELECT phone, nombre, rut FROM contact_profiles "
                f"WHERE phone IN ({placeholders})",
                variants_dedup,
            ).fetchone()
            if row and row[1]:  # nombre no vacío
                nombre_cp = row[1]
                log.info(
                    "winback: get_candidato_por_phone fuente3 (contact_profiles) phone=...%s nombre=%s",
                    tel_suffix[-4:], nombre_cp[:20],
                )
                return {
                    "paciente_id": 0,
                    "nombre": nombre_cp,
                    "apellido": "",
                    "telefono": phone,
                    "genero": None,
                    "ultima_atencion": None,
                    "ultima_especialidad": None,
                    "ultimo_profesional": None,
                    "dias_inactivo": None,
                    "cohorte": "unknown",
                    "edad": None,
                }
    except Exception as e:
        log.warning("winback: get_candidato_por_phone fuente3 error phone=...%s: %s", tel_suffix[-4:], e)

    # ── Fuente 4: bi.marketing_consent — phone conocido, nombre desconocido ───
    try:
        with bi_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT phone FROM bi.marketing_consent
                    WHERE RIGHT(regexp_replace(phone, '[^0-9]', '', 'g'), 9) = %s
                      AND status = 'accepted'
                    LIMIT 1
                    """,
                    (tel_suffix,),
                )
                row = cur.fetchone()
                if row:
                    log.warning(
                        "winback: WARN candidato_sin_nombre phone=...%s — "
                        "encontrado solo en marketing_consent, sin nombre en BI ni bot. "
                        "Usando fallback 'Paciente'. Revisar datos de fuente.",
                        tel_suffix[-4:],
                    )
                    return {
                        "paciente_id": 0,
                        "nombre": "Paciente",
                        "apellido": "",
                        "telefono": phone,
                        "genero": None,
                        "ultima_atencion": None,
                        "ultima_especialidad": None,
                        "ultimo_profesional": None,
                        "dias_inactivo": None,
                        "cohorte": "unknown",
                        "edad": None,
                    }
    except Exception as e:
        log.warning("winback: get_candidato_por_phone fuente4 error phone=...%s: %s", tel_suffix[-4:], e)

    return None


def ya_enviado_winback_hoy(phone: str) -> bool:
    """Retorna True si ya se envió un winback a este phone hoy.

    Rate limit per-phone: máximo 1 winback por día, independiente del batch o
    del trigger event-driven.
    """
    tel = phone.lstrip("+").strip()
    hoy = date.today().isoformat()
    try:
        with bi_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM bi.winback_envios "
                    "WHERE telefono = %s AND DATE(enviado_at) = %s "
                    "LIMIT 1",
                    (tel, hoy),
                )
                return cur.fetchone() is not None
    except Exception as e:
        log.warning("winback: ya_enviado_winback_hoy error phone=...%s: %s", tel[-4:], e)
        return False  # fail-open: si no podemos verificar, permitir envío


# ── Registro en winback_envios ────────────────────────────────────────────────
def _registrar_envio(
    paciente_id: int,
    telefono: str,
    cohorte: str,
    template_name: str,
    especialidad: str | None,
    value_clp: int,
) -> None:
    with bi_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO bi.winback_envios
                    (paciente_id, cohorte, telefono, template_meta, canal,
                     especialidad, template_id_version, value_clp)
                VALUES (%s, %s, %s, %s, 'whatsapp', %s, %s, %s)
                """,
                (paciente_id, cohorte, telefono, template_name,
                 especialidad if not es_sensible(especialidad) else None,
                 template_name, value_clp),
            )
        conn.commit()


# ── Actualizar respuesta ──────────────────────────────────────────────────────
def registrar_respuesta(telefono: str, response_type: str) -> None:
    """Actualiza response_type en el último winback enviado a este teléfono."""
    with bi_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE bi.winback_envios
                SET response_type = %s,
                    respondio_at = NOW()
                WHERE id = (
                    SELECT id FROM bi.winback_envios
                    WHERE telefono = %s
                    ORDER BY enviado_at DESC
                    LIMIT 1
                )
                """,
                (response_type, telefono),
            )
        conn.commit()


# ── Registrar opt-out ─────────────────────────────────────────────────────────
def registrar_opt_out(telefono: str, source: str = "whatsapp_reply") -> None:
    """Inserta en bi.opt_outs_marketing y actualiza winback_envios con response_type=baja."""
    with bi_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO bi.opt_outs_marketing(phone, source, reason)
                VALUES (%s, %s, 'BAJA via mensaje')
                ON CONFLICT (phone) DO UPDATE SET opted_out_at = NOW()
                """,
                (telefono, source),
            )
            # Marcar en winback_envios también
            cur.execute(
                """
                UPDATE bi.winback_envios
                SET response_type = 'baja', opt_out = true, respondio_at = NOW()
                WHERE telefono = %s AND response_type IS NULL
                """,
                (telefono,),
            )
        conn.commit()
    log.info("winback: opt-out registrado para %s", telefono[-4:])


# ── Atribución de citas a winback_envios ──────────────────────────────────────
def atribuir_cita_a_winback(phone: str, cita_id: int | str) -> int:
    """Marca cita_id en bi.winback_envios y bi.dental_winback_envios para este phone.

    Busca por los últimos 9 dígitos del teléfono (patrón consistente con resto
    del codebase). Solo actualiza filas con cita_id IS NULL y enviadas en los
    últimos 30 días. Idempotente.

    Retorna total de filas actualizadas (0, 1 o 2 si había envío en ambas tablas).
    """
    import re as _re
    phone_norm = _re.sub(r'\D', '', phone or '')[-9:]
    if not phone_norm:
        return 0
    cita_id_int = int(cita_id) if cita_id else None
    if not cita_id_int:
        return 0
    total = 0
    try:
        with bi_conn() as conn:
            with conn.cursor() as cur:
                # bi.winback_envios
                cur.execute(
                    """
                    UPDATE bi.winback_envios
                       SET cita_id   = %s,
                           agendo_at = NOW()
                     WHERE RIGHT(regexp_replace(telefono,'[^0-9]','','g'), 9) = %s
                       AND cita_id   IS NULL
                       AND enviado_at >= NOW() - INTERVAL '30 days'
                    """,
                    (cita_id_int, phone_norm),
                )
                total += cur.rowcount
                # bi.dental_winback_envios
                cur.execute(
                    """
                    UPDATE bi.dental_winback_envios
                       SET cita_id   = %s,
                           agendo_at = NOW()
                     WHERE RIGHT(regexp_replace(telefono,'[^0-9]','','g'), 9) = %s
                       AND cita_id   IS NULL
                       AND enviado_at >= NOW() - INTERVAL '30 days'
                    """,
                    (cita_id_int, phone_norm),
                )
                total += cur.rowcount
            conn.commit()
    except Exception as _e:
        log.warning("atribuir_cita_a_winback error phone=...%s cita=%s: %s",
                    phone_norm[-4:], cita_id_int, _e)
    if total:
        log.info("winback: atribuida cita_id=%s a phone=...%s (%d fila(s))",
                 cita_id_int, phone_norm[-4:], total)
    return total


# ── Envío individual ──────────────────────────────────────────────────────────
async def send_winback(candidato: dict) -> bool:
    """Envía mensaje winback a un candidato.

    Retorna True si el envío fue exitoso.
    Verifica:
    1. Consentimiento explícito de marketing (bi.marketing_consent.status='accepted').
    2. Approval del template _v2 en Meta (WARN winback_v2_not_approved si falla).
    """
    # Normalizar telefono: extraer solo dígitos del valor almacenado en BI.
    # La vista puede tener formatos como '+56966373231', '+5+6961800733', '9 7728 1627',
    # '+44944364612', etc. — Meta Cloud API necesita solo dígitos, sin '+'.
    _tel_raw = (candidato.get("telefono") or "").strip()
    _tel_digits = "".join(c for c in _tel_raw if c.isdigit())
    # Si el número no tiene prefijo de país (menos de 11 dígitos), agregar 56 (Chile)
    if len(_tel_digits) <= 9:
        _tel_digits = "56" + _tel_digits.lstrip("0")
    telefono   = _tel_digits
    especialidad = candidato.get("ultima_especialidad")
    cohorte    = candidato.get("cohorte", "090d")
    nombre     = candidato.get("nombre") or "paciente"
    paciente_id = candidato.get("paciente_id", 0)

    if not telefono or len(telefono) < 8:
        log.warning("winback: telefono inválido para paciente_id=%s", paciente_id)
        return False

    # Gate de consentimiento explícito de marketing.
    if not has_marketing_consent(telefono):
        log.info(
            "winback: INFO winback_skip_no_consent — skip paciente_id=%s phone=...%s",
            paciente_id, telefono[-4:],
        )
        return False

    template_name    = get_template(especialidad, cohorte)
    value_clp        = get_arancel(especialidad)
    profesional_nombre = candidato.get("ultimo_profesional") or ""

    # Verificar que el template _v2 esté APPROVED en Meta.
    # Si no está aprobado, NO usar _v1 (tiene número equivocado) — skip el paciente.
    template_approved = await is_template_approved(template_name)
    if not template_approved:
        log.warning(
            "winback: WARN winback_v2_not_approved — skip paciente_id=%s template=%s",
            paciente_id, template_name,
        )
        return False

    # Parámetros del body template
    # Templates _v2 con {{1}} = nombre paciente
    # Templates _v2 con {{1}} + {{2}} = nombre paciente + nombre profesional
    _TWO_PARAM_TEMPLATES = {
        "winback_medicina_general_v2",
        "winback_kinesiologia_v2",
        "winback_odontologia_v2",
        "winback_otorrino_v2",
    }
    # Usar solo el primer nombre limpio (ej: "MARIA JOSE GONZALEZ" → "Maria")
    first_name = nombre.strip().split()[0][:30].capitalize()
    if template_name in _TWO_PARAM_TEMPLATES and profesional_nombre:
        body_params = [first_name, profesional_nombre]
    else:
        body_params = [first_name]

    try:
        from messaging import send_whatsapp_template, render_template_body as _rtb_wb
        await send_whatsapp_template(
            to=telefono,
            template_name=template_name,
            body_params=body_params,
        )
        from session import log_message as _lm_w1
        _lm_w1(telefono, "out", _rtb_wb(template_name, body_params), "IDLE")
        _registrar_envio(
            paciente_id=paciente_id,
            telefono=telefono,
            cohorte=cohorte,
            template_name=template_name,
            especialidad=especialidad,
            value_clp=value_clp,
        )
        log.info(
            "winback: enviado a %s... cohorte=%s template=%s arancel=%d",
            telefono[-4:], cohorte, template_name, value_clp,
        )
        return True
    except Exception as e:
        log.error("winback: error enviando a %s...: %s", telefono[-4:], e)
        return False


# ── Proceso inbound: detectar BAJA / interés ─────────────────────────────────
def process_inbound_response(phone: str, msg_text: str) -> str | None:
    """Procesa respuesta inbound de un candidato winback.

    Returns: 'opt_out' | 'interesado' | None (no era respuesta winback)
    """
    # Verificar si hay un winback reciente sin respuesta para este phone
    try:
        with bi_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id FROM bi.winback_envios
                    WHERE telefono = %s
                      AND response_type IS NULL
                      AND enviado_at > NOW() - INTERVAL '30 days'
                    ORDER BY enviado_at DESC
                    LIMIT 1
                    """,
                    (phone,)
                )
                row = cur.fetchone()
                if not row:
                    return None  # no hay winback pendiente para este phone
    except Exception as e:
        log.warning("winback: error verificando inbound %s: %s", phone[-4:], e)
        return None

    texto = msg_text.strip().lower()

    # Detectar opt-out
    baja_keywords = {"baja", "no me escribas", "no contactar", "eliminar",
                     "borrar", "stop", "cancelar suscripcion", "no quiero",
                     "no gracias", "no me molest"}
    if any(k in texto for k in baja_keywords):
        registrar_opt_out(phone, source="whatsapp_reply")
        return "opt_out"

    # Detectar interés de agendar
    interes_keywords = {"quiero", "si", "sí", "agendar", "reservar", "hora",
                        "turno", "cita", "cuando", "cuándo", "disponible",
                        "me interesa", "gracias"}
    if any(k in texto for k in interes_keywords):
        registrar_respuesta(phone, "interesado_no_agenda")
        return "interesado"

    # Respuesta pero no clasificable
    registrar_respuesta(phone, "other")
    return None


# ── Batch diario ─────────────────────────────────────────────────────────────
async def run_daily_batch(cohorte: str = "090d") -> dict:
    """Orchestrador del batch diario de winback.

    Orden de cohortes: primero agota cohorte A (030d/060d/090d), luego B (180d/365d).
    Respeta rate limit: 30s entre envíos, 200/día absoluto.

    Returns: dict con stats del batch.
    """
    if not WINBACK_ACTIVE:
        log.info("winback: WINBACK_ACTIVE=false — batch omitido (flag de seguridad)")
        return {"status": "inactive", "enviados": 0}

    if not _dentro_horario_permitido():
        log.info("winback: fuera de horario permitido (L-V 10-19 Chile)")
        return {"status": "fuera_horario", "enviados": 0}

    ya_enviados = _enviados_hoy()
    restante    = _LIMITE_DIARIO - ya_enviados
    if restante <= 0:
        log.info("winback: límite diario %d ya alcanzado", _LIMITE_DIARIO)
        return {"status": "limite_diario", "enviados": 0}

    log.info("winback: iniciando batch cohorte=%s, cupo=%d", cohorte, restante)

    # Phones con cita futura (excluir)
    con_cita = _phones_con_cita_futura()
    log.info("winback: %d phones con cita futura (excluidos)", len(con_cita))

    candidatos = get_candidatos_dia(cohorte=cohorte, limite=restante * 2)  # pedir más por si hay exclusiones
    # Normalizar a últimos 9 dígitos para comparar: BI puede tener "987654321",
    # "9 7728 1627" o "56987654321"; con_cita viene de citas_bot con prefijo.
    def _norm9(p: str) -> str:
        return re.sub(r"\D", "", p or "")[-9:]
    con_cita_n = {_norm9(p) for p in con_cita}
    candidatos = [c for c in candidatos
                  if _norm9(c.get("telefono") or "") not in con_cita_n]
    candidatos = candidatos[:restante]

    log.info("winback: %d candidatos elegibles para cohorte=%s", len(candidatos), cohorte)

    enviados   = 0
    omitidos   = 0
    errores    = 0

    for c in candidatos:
        ok = await send_winback(c)
        if ok:
            enviados += 1
        else:
            errores += 1

        # 30 segundos entre envíos (regla dura)
        if enviados + errores < len(candidatos):
            await asyncio.sleep(30)

    stats = {
        "status":    "ok",
        "cohorte":   cohorte,
        "enviados":  enviados,
        "omitidos":  omitidos,
        "errores":   errores,
        "timestamp": datetime.now(TZ_CHILE).isoformat(),
    }
    log.info("winback: batch finalizado %s", stats)
    return stats


# ── Winback híbrido: session (gratis) cuando ventana 24h está abierta ────────

def _ventana_24h_abierta(phone: str) -> bool:
    """Retorna True si el paciente abrió la ventana de conversación en las últimas 23h.

    Criterio: bi.marketing_consent.response_at >= NOW() - INTERVAL '23 hours'.
    (23h de margen de seguridad antes del límite estricto de 24h de Meta.)

    No consulta sessions.db para evitar dependencia de SQLCipher desde winback.py;
    el timestamp de la respuesta al consent UTILITY es suficiente y garantiza que
    la ventana está abierta (el paciente acaba de responder).
    """
    tel = phone.lstrip("+").strip()
    try:
        with bi_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT 1 FROM bi.marketing_consent
                    WHERE RIGHT(regexp_replace(phone, '[^0-9]', '', 'g'), 9)
                          = RIGHT(regexp_replace(%s,  '[^0-9]', '', 'g'), 9)
                      AND status = 'accepted'
                      AND response_at >= NOW() - INTERVAL '23 hours'
                    """,
                    (tel,),
                )
                return cur.fetchone() is not None
    except Exception as e:
        log.warning("winback: _ventana_24h_abierta error phone=...%s: %s", tel[-4:], e)
        return False  # fail-safe: si no podemos verificar, usar template


async def _send_winback_session(candidato: dict) -> bool:
    """Envía el winback como session message (interactive buttons) — costo $0.

    Usa el mismo copy y lógica de selección de mensaje que send_winback(),
    pero en lugar de un template MARKETING usa un mensaje interactivo de sesión.
    Solo se llama cuando _ventana_24h_abierta() retornó True.
    """
    _tel_raw = (candidato.get("telefono") or "").strip()
    _tel_digits = "".join(c for c in _tel_raw if c.isdigit())
    if len(_tel_digits) <= 9:
        _tel_digits = "56" + _tel_digits.lstrip("0")
    telefono = _tel_digits
    paciente_id = candidato.get("paciente_id", 0)
    nombre = (candidato.get("nombre") or "paciente").capitalize()
    especialidad = candidato.get("ultima_especialidad")
    cohorte = candidato.get("cohorte", "090d")
    profesional = candidato.get("ultimo_profesional") or ""
    value_clp = get_arancel(especialidad)

    if not telefono or len(telefono) < 8:
        log.warning("winback: session: telefono inválido paciente_id=%s", paciente_id)
        return False

    # Construir el body del mensaje según privacidad y cohorte.
    # Mismo criterio que get_template() pero en texto plano.
    from config import CMC_TELEFONO_FIJO as _FIJO
    _fijo = _FIJO or "(41) 296 5226"

    if cohorte == "365d":
        # One-shot genérico: sin mencionar especialidad ni profesional
        body_text = (
            f"Hola {nombre}, te saluda el Centro Médico Carampangue.\n\n"
            f"Hace tiempo que no te vemos por aquí. Si necesitas una consulta o tienes "
            f"alguna duda de salud, estamos disponibles de lunes a viernes.\n\n"
            f"Puedes escribirnos por este WhatsApp o llamarnos al {_fijo}.\n\n"
            f"Responde BAJA si no deseas recibir más mensajes."
        )
    elif es_sensible(especialidad):
        # Sin mencionar especialidad
        body_text = (
            f"Hola {nombre}, te contactamos del Centro Médico Carampangue.\n\n"
            f"Nos gustaría saber cómo estás. Si tienes algún control pendiente, "
            f"podemos ayudarte a agendar con facilidad.\n\n"
            f"Estamos en Carampangue de lunes a viernes. Escríbenos aquí o "
            f"llámanos al {_fijo}.\n\n"
            f"Responde BAJA si no deseas recibir más mensajes."
        )
    else:
        # Mencionar especialidad y profesional cuando aplica
        _esp_display = especialidad or "consulta médica"
        _prof_part = f" con {profesional}" if profesional else ""
        body_text = (
            f"Hola {nombre}, te contactamos del Centro Médico Carampangue.\n\n"
            f"Nos dimos cuenta de que llevas un tiempo sin pasar a {_esp_display}{_prof_part}. "
            f"Si tienes algún control pendiente o un motivo nuevo que te preocupa, "
            f"podemos ayudarte a agendar con facilidad.\n\n"
            f"Estamos en Carampangue de lunes a viernes. Escríbenos aquí o "
            f"llámanos al {_fijo}.\n\n"
            f"Responde BAJA si no deseas recibir más mensajes."
        )

    interactive = {
        "type": "button",
        "body": {"text": body_text},
        "action": {
            "buttons": [
                {
                    "type": "reply",
                    "reply": {"id": "wb_agendar", "title": "Quiero agendar"},
                },
                {
                    "type": "reply",
                    "reply": {"id": "wb_info", "title": "Mas informacion"},
                },
            ]
        },
    }

    try:
        from messaging import send_whatsapp_interactive
        from session import log_message as _lm_ws

        await send_whatsapp_interactive(telefono, interactive)
        _lm_ws(telefono, "out", body_text, "IDLE")

        # Registrar con template_meta=SESSION_<cohorte> y value_clp=0
        _template_meta = f"SESSION_{cohorte}"
        _registrar_envio(
            paciente_id=paciente_id,
            telefono=telefono,
            cohorte=cohorte,
            template_name=_template_meta,
            especialidad=especialidad,
            value_clp=0,
        )
        log.info(
            "winback: sent via session phone=...%s cohorte=%s especialidad=%s value_clp=0",
            telefono[-4:], cohorte, especialidad,
        )
        return True
    except Exception as e:
        log.error("winback: session send error phone=...%s: %s", telefono[-4:], e)
        return False


async def send_winback_smart(candidato: dict, prefer_session: bool = True) -> bool:
    """Envía winback eligiendo la vía mas económica disponible.

    Si prefer_session=True y la ventana 24h está abierta (el paciente acaba
    de responder al consent UTILITY), envía session message — costo $0.
    Si la ventana está cerrada o prefer_session=False, usa template MARKETING
    ($41). Esto se aplica solo al flujo event-driven (consent SI); el batch
    diario sigue usando send_winback() directamente (ventana siempre cerrada).
    """
    phone = (candidato.get("telefono") or "").strip()
    if prefer_session and _ventana_24h_abierta(phone):
        log.info("winback: ventana abierta — usando session message phone=...%s", phone[-4:])
        return await _send_winback_session(candidato)
    log.info("winback: ventana cerrada — usando template MARKETING phone=...%s", phone[-4:])
    return await send_winback(candidato)


# ── Función para el scheduler (jobs.py) ──────────────────────────────────────
async def job_winback_diario() -> None:
    """Entry point para APScheduler — corre cohortes en orden."""
    # Cohorte A primero: 030d → 060d → 090d
    # Cohorte B cuando A esté agotada: 180d → 365d
    ORDEN_COHORTES = ["030d", "060d", "090d", "180d", "365d"]

    for cohorte in ORDEN_COHORTES:
        ya_enviados = _enviados_hoy()
        if ya_enviados >= _LIMITE_DIARIO:
            log.info("winback: límite %d alcanzado, deteniendo cohortes", _LIMITE_DIARIO)
            break
        stats = await run_daily_batch(cohorte=cohorte)
        if stats.get("status") in ("inactive", "fuera_horario", "limite_diario"):
            break
        # Pequeña pausa entre cohortes
        if stats.get("enviados", 0) > 0:
            await asyncio.sleep(5)
