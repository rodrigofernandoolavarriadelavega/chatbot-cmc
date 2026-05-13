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
"""
import asyncio
import logging
import os
import time
from datetime import datetime, date
from zoneinfo import ZoneInfo

import psycopg2
import psycopg2.pool

log = logging.getLogger("bot")

TZ_CHILE = ZoneInfo("America/Santiago")

# ── Feature flag ─────────────────────────────────────────────────────────────
WINBACK_ACTIVE = os.getenv("WINBACK_ACTIVE", "false").lower() in ("true", "1", "yes")

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
# Los nombres deben coincidir EXACTAMENTE con los aprobados en Meta Business Manager.
# NOTA: estos templates aún NO están aprobados. El job corre con WINBACK_ACTIVE=false
# hasta que se confirmen los approvals.
_TEMPLATE_MAP: dict[str, str] = {
    "medicina general":       "winback_medicina_general_v1",
    "medicina interna":       "winback_medicina_general_v1",
    "kinesiología":           "winback_kinesiologia_v1",
    "kinesiologia":           "winback_kinesiologia_v1",
    "otorrinolaringología":   "winback_otorrino_v1",
    "otorrinolaringologia":   "winback_otorrino_v1",
    "odontología general":    "winback_odontologia_v1",
    "odontologia general":    "winback_odontologia_v1",
    "nutrición":              "winback_medicina_general_v1",   # sin template propio aún
    "nutricion":              "winback_medicina_general_v1",
    "podología":              "winback_medicina_general_v1",
    "podologia":              "winback_medicina_general_v1",
    "fonoaudiología":         "winback_medicina_general_v1",
    "fonoaudiologia":         "winback_medicina_general_v1",
    "cardiología":            "winback_medicina_general_v1",
    "cardiologia":            "winback_medicina_general_v1",
    "traumatología y ortopedia": "winback_medicina_general_v1",
    "traumatologia y ortopedia": "winback_medicina_general_v1",
    "gastroenterología":      "winback_medicina_general_v1",
    "gastroenterologia":      "winback_medicina_general_v1",
    "ortodoncista":           "winback_odontologia_v1",
    "implantología":          "winback_odontologia_v1",
    "implantologia":          "winback_odontologia_v1",
}

_TEMPLATE_SENSIBLE   = "winback_generico_sensible_v1"
_TEMPLATE_ONE_SHOT   = "winback_one_shot_general_v1"

def get_template(especialidad: str | None, cohorte: str) -> str:
    """Selecciona template según privacidad y cohorte.

    Cohorte 365d → one_shot_general sin importar especialidad.
    Especialidad sensible → generico_sensible.
    Resto → template específico (o fallback medicina_general).
    """
    if cohorte == "365d":
        return _TEMPLATE_ONE_SHOT
    if es_sensible(especialidad):
        return _TEMPLATE_SENSIBLE
    key = (especialidad or "").lower().strip()
    return _TEMPLATE_MAP.get(key, "winback_medicina_general_v1")


def get_arancel(especialidad: str | None) -> int:
    """Retorna arancel estimado en CLP para CAPI Purchase value (delegado a config)."""
    from config import get_arancel_cpl
    return get_arancel_cpl(especialidad)


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

    Excluye:
    - Opt-outs (ya excluidos en la vista).
    - Pacientes que ya recibieron un winback en los últimos 90 días.
    - Pacientes con cita futura en citas_bot (sessions.db) — consultado en memoria
      después de obtener la lista BI, por rendimiento.
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
    """
    telefono   = (candidato.get("telefono") or "").strip().lstrip("+")
    especialidad = candidato.get("ultima_especialidad")
    cohorte    = candidato.get("cohorte", "090d")
    nombre     = candidato.get("nombre") or "paciente"
    paciente_id = candidato.get("paciente_id", 0)

    if not telefono or len(telefono) < 8:
        log.warning("winback: telefono inválido para paciente_id=%s", paciente_id)
        return False

    template_name    = get_template(especialidad, cohorte)
    value_clp        = get_arancel(especialidad)
    profesional_nombre = candidato.get("ultimo_profesional") or ""

    # Parámetros del body template
    # Templates con {{1}} = nombre paciente
    # Templates con {{1}} + {{2}} = nombre paciente + nombre profesional
    # (medicina_general, kinesiologia, odontologia, otorrino)
    _TWO_PARAM_TEMPLATES = {
        "winback_medicina_general_v1",
        "winback_kinesiologia_v1",
        "winback_odontologia_v1",
        "winback_otorrino_v1",
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
