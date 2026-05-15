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
            rows = conn.execute(
                "SELECT DISTINCT phone FROM citas_bot "
                "WHERE fecha >= ? AND estado NOT IN ('cancelada', 'anulada')",
                (hoy,)
            ).fetchall()
            return {r[0] for r in rows}
        finally:
            conn.close()
    except Exception as e:
        log.warning("winback: no pude leer citas_bot: %s", e)
        return set()


# ── Lookup event-driven (un solo paciente por phone) ─────────────────────────

def get_candidato_por_phone(phone: str) -> dict | None:
    """Busca los datos de un paciente por teléfono en v_winback_cohortes_contactables.

    La vista ya filtra: consent aceptado + sin opt-out.
    Retorna el dict con los campos que espera send_winback, o None si el paciente
    no existe / no es contactable / no tiene cohorte asignada.

    Normalización: compara por últimos 9 dígitos para tolerar formatos mixtos
    en la vista (+56966373231 vs 56966373231 vs 966373231, etc.).
    El blast envía usando el telefono de la vista (ej: +56966373231); Meta normaliza
    y el webhook llega como 56966373231 → sin normalización, get devolvía None.
    """
    tel = phone.lstrip("+").strip()
    # Extraer solo dígitos y tomar sufijo de 9 (ej: 56966373231 → 966373231)
    tel_digits = "".join(c for c in tel if c.isdigit())
    if len(tel_digits) < 6:
        return None
    tel_suffix = tel_digits[-9:]  # últimos 9 dígitos del número
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
                if not row:
                    return None
                cols = [
                    "paciente_id", "nombre", "apellido", "telefono",
                    "genero", "ultima_atencion", "ultima_especialidad",
                    "ultimo_profesional", "dias_inactivo", "cohorte", "edad",
                ]
                return dict(zip(cols, row))
    except Exception as e:
        log.warning("winback: get_candidato_por_phone error phone=...%s: %s", tel[-4:], e)
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
    if template_name in _TWO_PARAM_TEMPLATES and profesional_nombre:
        body_params = [nombre.capitalize(), profesional_nombre]
    else:
        body_params = [nombre.capitalize()]

    try:
        from messaging import send_whatsapp_template
        await send_whatsapp_template(
            to=telefono,
            template_name=template_name,
            body_params=body_params,
        )
        from session import log_message as _lm_w1
        _lm_w1(telefono, "out", f"[template: {template_name}]", "IDLE")
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
    candidatos = [c for c in candidatos
                  if (c.get("telefono") or "").lstrip("+") not in con_cita]
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
