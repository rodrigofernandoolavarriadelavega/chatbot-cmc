"""dental_winback.py — Campanas de reactivación focalizadas en pacientes dentales.

Pool: pacientes con al menos 1 atención con profesional dental (IDs Medilink 55, 72, 66, 75, 69, 76).
Tablas propias (bi.dental_*) para independencia de opt-outs del programa general.
Un paciente puede estar en marketing_consent general 'declined' y en dental_consent 'accepted',
o viceversa.

Reglas duras:
- DENTAL_CONSENT_BLAST_ACTIVE=false -> no envía consent dental.
- DENTAL_WINBACK_ACTIVE=false -> no envía winback dental.
- Max 100/día (pool más chico, más conservador que los 200 del general).
- 30s entre envíos.
- Solo L-V 10:00-19:00 CLT.
- Excluir patients con cita futura en citas_bot.
- Excluir dental_opt_outs.
- Solo enviar winback si dental_consent.status='accepted'.
- Outbound SIEMPRE desde +56966610737. Nunca +56987834148.
- Footer "responde BAJA" en todos los templates MARKETING.
"""
import asyncio
import logging
import os
import time
from datetime import datetime, date
from zoneinfo import ZoneInfo

# Reutilizar pool y helpers genéricos de winback.py
from winback import (
    _get_bi_pool,
    bi_conn,
    is_template_approved,
    _phones_con_cita_futura,
    _dentro_horario_permitido,
)

log = logging.getLogger("bot")

TZ_CHILE = ZoneInfo("America/Santiago")

# -- Feature flags ------------------------------------------------------------
DENTAL_CONSENT_BLAST_ACTIVE = os.getenv(
    "DENTAL_CONSENT_BLAST_ACTIVE", "false"
).lower() in ("true", "1", "yes")

DENTAL_WINBACK_ACTIVE = os.getenv(
    "DENTAL_WINBACK_ACTIVE", "false"
).lower() in ("true", "1", "yes")

# -- Profesionales dentales (IDs Medilink) ------------------------------------
# 55 = Burgos Odontología General (60 min)
# 72 = Jiménez Odontología General (30 min)
# 66 = Castillo Ortodoncia (30 min)
# 75 = Fredes Endodoncia (30 min)
# 69 = Valdés Implantología (30 min)
# 76 = Fuentealba Estética Facial (30 min)
PROFESIONALES_DENTAL_IDS = {55, 72, 66, 75, 69, 76}

# -- Sub-cohortes: intervalos de días desde última atención dental ------------
# Se mapean a vista bi.v_dental_cohortes_contactables filtrada por rango de días.
SUBCOHORTES = {
    "dental_odonto_general_180d": {
        "ids_prof": {55, 72},
        "dias_min": 90,
        "dias_max": 180,
        "template": "winback_dental_odonto_general_v1",
    },
    "dental_odonto_general_365d": {
        "ids_prof": {55, 72},
        "dias_min": 181,
        "dias_max": 365,
        "template": "winback_dental_odonto_general_v1",
    },
    "dental_ortodoncia_180d": {
        "ids_prof": {66},
        "dias_min": 30,   # control mensual perdido
        "dias_max": 180,
        "template": "winback_dental_ortodoncia_v1",
    },
    "dental_endo_implanto_180d": {
        "ids_prof": {75, 69},
        "dias_min": 90,
        "dias_max": 365,
        "template": "winback_dental_endo_implanto_v1",
    },
    "dental_estetica_180d": {
        "ids_prof": {76},
        "dias_min": 90,
        "dias_max": 365,
        "template": "winback_dental_estetica_v1",
    },
}

# -- Límite diario dental -----------------------------------------------------
# 200/día (subido desde 100 el 2026-05-27) — dental es canal primario por mayor margen.
_LIMITE_DIARIO_DENTAL = 200


# -- Dental consent -----------------------------------------------------------

def has_dental_consent(phone: str) -> bool:
    """Retorna True si phone tiene dental_consent.status='accepted'."""
    try:
        with bi_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT status FROM bi.dental_consent WHERE phone = %s",
                    (phone,),
                )
                row = cur.fetchone()
                return row is not None and row[0] == "accepted"
    except Exception as e:
        log.warning("dental_winback: has_dental_consent error phone=...%s: %s", phone[-4:], e)
        return False


def registrar_dental_consent_enviado(phone: str) -> None:
    """Marca dental_consent como 'pending' al enviar el template de consent."""
    from session import normalize_wa_id
    phone = normalize_wa_id(phone)  # canónico 56XXXXXXXXX para matchear el wa_id entrante
    try:
        with bi_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO bi.dental_consent (phone, status, consent_sent_at)
                    VALUES (%s, 'pending', NOW())
                    ON CONFLICT (phone) DO UPDATE
                        SET status = 'pending',
                            consent_sent_at = NOW()
                        WHERE bi.dental_consent.status NOT IN ('accepted', 'declined')
                    """,
                    (phone,),
                )
            conn.commit()
    except Exception as e:
        log.warning("dental_winback: registrar_dental_consent_enviado error phone=...%s: %s", phone[-4:], e)


def registrar_dental_consent_respuesta(phone: str, status: str, method: str) -> None:
    """Actualiza dental_consent con la respuesta del paciente.

    status: 'accepted' | 'declined'
    method: 'quick_reply_button' | 'text_si' | 'text_no' | 'reply'
    """
    if status not in ("accepted", "declined"):
        return
    from session import normalize_wa_id
    phone = normalize_wa_id(phone)  # canónico, debe coincidir con la fila pending
    try:
        with bi_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO bi.dental_consent
                        (phone, status, response_at, response_method)
                    VALUES (%s, %s, NOW(), %s)
                    ON CONFLICT (phone) DO UPDATE
                        SET status = EXCLUDED.status,
                            response_at = NOW(),
                            response_method = EXCLUDED.response_method
                    """,
                    (phone, status, method),
                )
            conn.commit()
        log.info(
            "dental_winback: dental_consent status=%s phone=...%s method=%s",
            status, phone[-4:], method,
        )
    except Exception as e:
        log.warning("dental_winback: registrar_dental_consent_respuesta error phone=...%s: %s", phone[-4:], e)


def phone_in_dental_opt_out(phone: str) -> bool:
    """Retorna True si el phone está en bi.dental_opt_outs."""
    try:
        with bi_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM bi.dental_opt_outs WHERE phone = %s",
                    (phone,),
                )
                return cur.fetchone() is not None
    except Exception as e:
        log.warning("dental_winback: phone_in_dental_opt_out error phone=...%s: %s", phone[-4:], e)
        return False


def registrar_dental_opt_out(telefono: str, source: str = "whatsapp_reply") -> None:
    """Inserta en bi.dental_opt_outs y marca respuesta en dental_winback_envios."""
    try:
        with bi_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO bi.dental_opt_outs(phone, source, reason)
                    VALUES (%s, %s, 'BAJA via mensaje')
                    ON CONFLICT (phone) DO UPDATE SET opted_out_at = NOW()
                    """,
                    (telefono, source),
                )
                cur.execute(
                    """
                    UPDATE bi.dental_winback_envios
                    SET response_type = 'baja', opt_out = true, respondio_at = NOW()
                    WHERE telefono = %s AND response_type IS NULL
                    """,
                    (telefono,),
                )
            conn.commit()
        log.info("dental_winback: opt-out dental registrado para ...%s", telefono[-4:])
    except Exception as e:
        log.warning("dental_winback: registrar_dental_opt_out error phone=...%s: %s", telefono[-4:], e)


# -- Conteo diario enviados ---------------------------------------------------

def _dental_enviados_hoy() -> int:
    """Cuántos winbacks dentales se enviaron hoy."""
    hoy = date.today().isoformat()
    try:
        with bi_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM bi.dental_winback_envios "
                    "WHERE DATE(enviado_at) = %s",
                    (hoy,),
                )
                return cur.fetchone()[0]
    except Exception as e:
        log.warning("dental_winback: _dental_enviados_hoy error: %s", e)
        return 0


def ya_enviado_dental_winback_hoy(phone: str) -> bool:
    """Rate limit per-phone: máximo 1 winback dental por día."""
    hoy = date.today().isoformat()
    try:
        with bi_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM bi.dental_winback_envios "
                    "WHERE telefono = %s AND DATE(enviado_at) = %s LIMIT 1",
                    (phone, hoy),
                )
                return cur.fetchone() is not None
    except Exception as e:
        log.warning("dental_winback: ya_enviado_dental_winback_hoy error phone=...%s: %s", phone[-4:], e)
        return False


# -- Candidatos por sub-cohorte -----------------------------------------------

def get_candidatos_dental(subcohorte: str, limite: int = 100) -> list[dict]:
    """Obtiene candidatos dental para una sub-cohorte.

    Filtra desde bi.v_dental_cohortes_contactables:
    - Solo phones con dental_consent.status='accepted'.
    - Excluye dental_opt_outs (ya excluidos en la vista).
    - Excluye phones con dental_winback_envios en los últimos 90 días.
    - Excluye registros con ultima_especialidad NULL o vacía (historial dental no claro).
    """
    cfg = SUBCOHORTES.get(subcohorte)
    if not cfg:
        log.warning("dental_winback: sub-cohorte desconocida: %s", subcohorte)
        return []

    try:
        with bi_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        dc.paciente_id,
                        dc.nombre,
                        dc.apellido,
                        dc.telefono,
                        dc.ultima_atencion,
                        dc.ultima_especialidad,
                        dc.ultimo_profesional,
                        dc.profesional_id_ultimo AS id_profesional,
                        dc.dias_inactivo,
                        dc.subcohorte
                    FROM bi.v_dental_cohortes_contactables dc
                    INNER JOIN bi.dental_consent mc
                        ON mc.phone = dc.telefono
                       AND mc.status = 'accepted'
                    WHERE dc.subcohorte = %s
                      AND dc.ultima_especialidad IS NOT NULL
                      AND TRIM(dc.ultima_especialidad) <> ''
                      AND NOT EXISTS (
                          SELECT 1 FROM bi.dental_winback_envios dwe
                          WHERE dwe.telefono = dc.telefono
                            AND dwe.enviado_at > NOW() - INTERVAL '90 days'
                      )
                    ORDER BY dc.dias_inactivo ASC
                    LIMIT %s
                    """,
                    (subcohorte, limite),
                )
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception as e:
        log.warning("dental_winback: get_candidatos_dental error subcohorte=%s: %s", subcohorte, e)
        return []


def get_candidato_dental_por_phone(phone: str) -> dict | None:
    """Busca datos dentales de un paciente por phone usando cascada de 4 fuentes.

    Orden (primer hit gana):
    1. bi.v_dental_cohortes_contactables — candidato ideal con subcohorte y especialidad dental.
    2. bi.dim_paciente directo — nombre real aunque esté fuera de la vista dental.
    3. contact_profiles (sessions.db) — pacientes conocidos por el bot pero no en BI.
    4. bi.dental_consent — phone con consent dental; nombre='Paciente' como ultimísimo fallback.

    Staff y número personal del médico siempre retornan None (nunca marketing a staff).
    Normalización por últimos 9 dígitos.
    """
    from winback import _is_staff_phone  # reutilizar la misma lógica de exclusión staff
    tel = phone.lstrip("+").strip()
    tel_digits = "".join(c for c in tel if c.isdigit())
    if len(tel_digits) < 6:
        return None
    tel_suffix = tel_digits[-9:]

    # Gate: nunca marketing a staff ni al número personal
    if _is_staff_phone(tel_digits):
        return None

    # ── Fuente 1: v_dental_cohortes_contactables ──────────────────────────────
    try:
        with bi_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        paciente_id, nombre, apellido, telefono,
                        ultima_atencion, ultima_especialidad,
                        ultimo_profesional, profesional_id_ultimo AS id_profesional,
                        dias_inactivo, subcohorte
                    FROM bi.v_dental_cohortes_contactables
                    WHERE RIGHT(regexp_replace(telefono, '[^0-9]', '', 'g'), 9) = %s
                      AND subcohorte IS NOT NULL
                    ORDER BY dias_inactivo ASC
                    LIMIT 1
                    """,
                    (tel_suffix,),
                )
                row = cur.fetchone()
                if row:
                    cols = [
                        "paciente_id", "nombre", "apellido", "telefono",
                        "ultima_atencion", "ultima_especialidad",
                        "ultimo_profesional", "id_profesional",
                        "dias_inactivo", "subcohorte",
                    ]
                    return dict(zip(cols, row))
    except Exception as e:
        log.warning("dental_winback: get_candidato_dental_por_phone fuente1 error phone=...%s: %s", tel_suffix[-4:], e)

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
                        "dental_winback: get_candidato_dental_por_phone fuente2 (dim_paciente) phone=...%s nombre=%s",
                        tel_suffix[-4:], (row[1] or "").strip()[:20],
                    )
                    return {
                        "paciente_id": row[0],
                        "nombre": row[1],
                        "apellido": row[2],
                        "telefono": row[3] or phone,
                        "ultima_atencion": None,
                        "ultima_especialidad": None,
                        "ultimo_profesional": None,
                        "id_profesional": None,
                        "dias_inactivo": None,
                        "subcohorte": "unknown",
                    }
    except Exception as e:
        log.warning("dental_winback: get_candidato_dental_por_phone fuente2 error phone=...%s: %s", tel_suffix[-4:], e)

    # ── Fuente 3: contact_profiles (sessions.db) ──────────────────────────────
    try:
        from session import db as _session_conn
        phone_variants = (phone, "+" + phone, tel_digits, "56" + tel_suffix, tel_suffix)
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
            if row and row[1]:
                nombre_cp = row[1]
                log.info(
                    "dental_winback: get_candidato_dental_por_phone fuente3 (contact_profiles) phone=...%s nombre=%s",
                    tel_suffix[-4:], nombre_cp[:20],
                )
                return {
                    "paciente_id": 0,
                    "nombre": nombre_cp,
                    "apellido": "",
                    "telefono": phone,
                    "ultima_atencion": None,
                    "ultima_especialidad": None,
                    "ultimo_profesional": None,
                    "id_profesional": None,
                    "dias_inactivo": None,
                    "subcohorte": "unknown",
                }
    except Exception as e:
        log.warning("dental_winback: get_candidato_dental_por_phone fuente3 error phone=...%s: %s", tel_suffix[-4:], e)

    # ── Fuente 4: bi.dental_consent — phone conocido, nombre desconocido ──────
    try:
        with bi_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT phone FROM bi.dental_consent
                    WHERE RIGHT(regexp_replace(phone, '[^0-9]', '', 'g'), 9) = %s
                      AND status = 'accepted'
                    LIMIT 1
                    """,
                    (tel_suffix,),
                )
                row = cur.fetchone()
                if row:
                    log.warning(
                        "dental_winback: WARN candidato_dental_sin_nombre phone=...%s — "
                        "encontrado solo en dental_consent, sin nombre en BI ni bot. "
                        "Usando fallback 'Paciente'. Revisar datos de fuente.",
                        tel_suffix[-4:],
                    )
                    return {
                        "paciente_id": 0,
                        "nombre": "Paciente",
                        "apellido": "",
                        "telefono": phone,
                        "ultima_atencion": None,
                        "ultima_especialidad": None,
                        "ultimo_profesional": None,
                        "id_profesional": None,
                        "dias_inactivo": None,
                        "subcohorte": "unknown",
                    }
    except Exception as e:
        log.warning("dental_winback: get_candidato_dental_por_phone fuente4 error phone=...%s: %s", tel_suffix[-4:], e)

    return None


# -- Registro de envíos -------------------------------------------------------

def _registrar_envio_dental(
    paciente_id: int,
    telefono: str,
    subcohorte: str,
    template_name: str,
    especialidad: str | None,
) -> None:
    try:
        with bi_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO bi.dental_winback_envios
                        (paciente_id, subcohorte, telefono, template_meta, canal, especialidad)
                    VALUES (%s, %s, %s, %s, 'whatsapp', %s)
                    """,
                    (paciente_id, subcohorte, telefono, template_name, especialidad),
                )
            conn.commit()
    except Exception as e:
        log.warning("dental_winback: _registrar_envio_dental error phone=...%s: %s", telefono[-4:], e)


def registrar_dental_respuesta(telefono: str, response_type: str) -> None:
    """Actualiza response_type en el último dental_winback_envios de este phone."""
    try:
        with bi_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE bi.dental_winback_envios
                    SET response_type = %s,
                        respondio_at = NOW()
                    WHERE id = (
                        SELECT id FROM bi.dental_winback_envios
                        WHERE telefono = %s
                        ORDER BY enviado_at DESC
                        LIMIT 1
                    )
                    """,
                    (response_type, telefono),
                )
            conn.commit()
    except Exception as e:
        log.warning("dental_winback: registrar_dental_respuesta error phone=...%s: %s", telefono[-4:], e)


# -- Envío individual ---------------------------------------------------------

async def send_dental_winback(candidato: dict) -> bool:
    """Envía mensaje winback dental a un candidato.

    Verifica:
    1. dental_consent.status='accepted'.
    2. Template APPROVED en Meta.
    3. No está en dental_opt_outs.
    """
    _tel_raw = (candidato.get("telefono") or "").strip()
    _tel_digits = "".join(c for c in _tel_raw if c.isdigit())
    if len(_tel_digits) <= 9:
        _tel_digits = "56" + _tel_digits.lstrip("0")
    telefono = _tel_digits

    paciente_id = candidato.get("paciente_id", 0)
    nombre = candidato.get("nombre") or "paciente"
    especialidad = candidato.get("ultima_especialidad")
    profesional = candidato.get("ultimo_profesional") or ""
    subcohorte = candidato.get("subcohorte", "dental_odonto_general_180d")

    if not telefono or len(telefono) < 8:
        log.warning("dental_winback: telefono inválido para paciente_id=%s", paciente_id)
        return False

    if phone_in_dental_opt_out(telefono):
        log.info("dental_winback: skip opt-out paciente_id=%s phone=...%s", paciente_id, telefono[-4:])
        return False

    if not has_dental_consent(telefono):
        log.info(
            "dental_winback: skip_no_consent paciente_id=%s phone=...%s",
            paciente_id, telefono[-4:],
        )
        return False

    # Excluir candidatos sin historial dental claro (especialidad null/vacía o cohorte unknown)
    if not especialidad or not str(especialidad).strip():
        log.info(
            "dental_winback: skip_especialidad_nula paciente_id=%s phone=...%s subcohorte=%s",
            paciente_id, telefono[-4:], subcohorte,
        )
        return False
    if subcohorte == "unknown":
        log.info(
            "dental_winback: skip_subcohorte_unknown paciente_id=%s phone=...%s",
            paciente_id, telefono[-4:],
        )
        return False

    cfg = SUBCOHORTES.get(subcohorte, {})
    template_name = cfg.get("template", "winback_dental_odonto_general_v1")

    template_ok = await is_template_approved(template_name)
    if not template_ok:
        log.warning(
            "dental_winback: template_not_approved template=%s paciente_id=%s -- skip",
            template_name, paciente_id,
        )
        return False

    _TWO_PARAM = {
        "winback_dental_odonto_general_v1",
        "winback_dental_endo_implanto_v1",
    }
    # Usar solo el primer nombre limpio (ej: "CARLOS ANDRES ROJAS" → "Carlos")
    first_name = nombre.strip().split()[0][:30].capitalize()
    if template_name in _TWO_PARAM:
        # Los de 2 params EXIGEN {{2}}; genérico si no hay profesional para no
        # romper con (#132000). Conteo verificado contra Meta 2026-05-30.
        body_params = [first_name, profesional or "nuestro equipo"]
    else:
        body_params = [first_name]

    try:
        from messaging import send_whatsapp_template, render_template_body as _rtb
        from session import log_message as _lm, save_campana_envio as _save_camp

        await send_whatsapp_template(
            to=telefono,
            template_name=template_name,
            body_params=body_params,
        )
        _lm(telefono, "out", _rtb(template_name, body_params), "IDLE")
        _registrar_envio_dental(
            paciente_id=paciente_id,
            telefono=telefono,
            subcohorte=subcohorte,
            template_name=template_name,
            especialidad=especialidad,
        )
        # Registrar también en campanas_envios (SQLite) para medir conversión en panel admin
        try:
            _save_camp(telefono, f"dental_winback_{subcohorte}")
        except Exception as _e_camp:
            log.warning("dental_winback: save_campana_envio error phone=...%s: %s", telefono[-4:], _e_camp)
        log.info(
            "dental_winback: enviado phone=...%s subcohorte=%s template=%s",
            telefono[-4:], subcohorte, template_name,
        )
        return True
    except Exception as e:
        log.error("dental_winback: error enviando phone=...%s: %s", telefono[-4:], e)
        return False


# -- Detección de respuesta inbound dental ------------------------------------

def process_inbound_dental(phone: str, msg_text: str) -> str | None:
    """Procesa respuesta inbound a un winback dental.

    Returns: 'opt_out' | 'interesado' | None
    """
    try:
        with bi_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id FROM bi.dental_winback_envios
                    WHERE telefono = %s
                      AND response_type IS NULL
                      AND enviado_at > NOW() - INTERVAL '30 days'
                    ORDER BY enviado_at DESC
                    LIMIT 1
                    """,
                    (phone,),
                )
                if not cur.fetchone():
                    return None
    except Exception as e:
        log.warning("dental_winback: error verificando inbound phone=...%s: %s", phone[-4:], e)
        return None

    texto = msg_text.strip().lower()

    baja_keywords = {
        "baja", "no me escribas", "no contactar", "eliminar",
        "borrar", "stop", "cancelar suscripcion", "no quiero",
        "no gracias", "no me molest",
    }
    if any(k in texto for k in baja_keywords):
        registrar_dental_opt_out(phone, source="whatsapp_reply")
        return "opt_out"

    interes_keywords = {
        "quiero", "si", "si", "agendar", "reservar", "hora",
        "turno", "cita", "cuando", "cuando", "disponible",
        "me interesa", "gracias", "control",
    }
    if any(k in texto for k in interes_keywords):
        registrar_dental_respuesta(phone, "interesado_no_agenda")
        return "interesado"

    registrar_dental_respuesta(phone, "other")
    return None


# -- Batch diario -------------------------------------------------------------

async def run_dental_batch(subcohorte: str) -> dict:
    """Orchestrador del batch diario de winback dental para una sub-cohorte."""
    if not DENTAL_WINBACK_ACTIVE:
        log.info("dental_winback: DENTAL_WINBACK_ACTIVE=false -- batch omitido")
        return {"status": "inactive", "enviados": 0}

    if not _dentro_horario_permitido():
        log.info("dental_winback: fuera de horario permitido (L-V 10-19 Chile)")
        return {"status": "fuera_horario", "enviados": 0}

    ya_enviados = _dental_enviados_hoy()
    restante = _LIMITE_DIARIO_DENTAL - ya_enviados
    if restante <= 0:
        log.info("dental_winback: limite diario %d alcanzado", _LIMITE_DIARIO_DENTAL)
        return {"status": "limite_diario", "enviados": 0}

    log.info("dental_winback: iniciando batch subcohorte=%s cupo=%d", subcohorte, restante)

    con_cita = _phones_con_cita_futura()
    candidatos = get_candidatos_dental(subcohorte=subcohorte, limite=restante * 2)
    candidatos = [c for c in candidatos
                  if (c.get("telefono") or "").lstrip("+") not in con_cita]
    candidatos = candidatos[:restante]

    log.info("dental_winback: %d candidatos elegibles subcohorte=%s", len(candidatos), subcohorte)

    enviados = 0
    errores = 0

    for c in candidatos:
        ok = await send_dental_winback(c)
        if ok:
            enviados += 1
        else:
            errores += 1
        if enviados + errores < len(candidatos):
            await asyncio.sleep(30)

    stats = {
        "status": "ok",
        "subcohorte": subcohorte,
        "enviados": enviados,
        "errores": errores,
        "timestamp": datetime.now(TZ_CHILE).isoformat(),
    }
    log.info("dental_winback: batch finalizado %s", stats)
    return stats


async def job_dental_winback_diario() -> None:
    """Entry point para APScheduler -- corre sub-cohortes en orden de prioridad."""
    ORDEN = [
        "dental_ortodoncia_180d",
        "dental_endo_implanto_180d",
        "dental_odonto_general_180d",
        "dental_odonto_general_365d",
        "dental_estetica_180d",
    ]
    for subcohorte in ORDEN:
        ya = _dental_enviados_hoy()
        if ya >= _LIMITE_DIARIO_DENTAL:
            log.info("dental_winback: limite %d alcanzado, deteniendo sub-cohortes", _LIMITE_DIARIO_DENTAL)
            break
        stats = await run_dental_batch(subcohorte=subcohorte)
        if stats.get("status") in ("inactive", "fuera_horario", "limite_diario"):
            break
        if stats.get("enviados", 0) > 0:
            await asyncio.sleep(5)


# -- Blast de consent dental --------------------------------------------------

async def run_dental_consent_blast() -> dict:
    """Envía consent_dental_v2 (UTILITY) a candidatos dentales sin registro consent.

    Solo corre si DENTAL_CONSENT_BLAST_ACTIVE=true y template APPROVED.
    Max 200/día, 30s entre envíos, L-V 10-19 CLT.
    """
    if not DENTAL_CONSENT_BLAST_ACTIVE:
        log.debug("dental_winback: DENTAL_CONSENT_BLAST_ACTIVE=false -- blast omitido")
        return {"status": "inactive", "enviados": 0}

    if not _dentro_horario_permitido():
        log.debug("dental_winback: consent blast fuera de horario")
        return {"status": "fuera_horario", "enviados": 0}

    if not await is_template_approved("consent_dental_v2"):
        log.warning("dental_winback: consent_dental_v2 no esta APPROVED en Meta -- skip")
        return {"status": "template_no_aprobado", "enviados": 0}

    LIMITE_DIA = 200
    SLEEP_ENTRE = 30

    try:
        with bi_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM bi.dental_consent "
                    "WHERE DATE(consent_sent_at AT TIME ZONE 'America/Santiago') = CURRENT_DATE"
                )
                enviados_hoy = cur.fetchone()[0]
    except Exception as e:
        log.error("dental_winback: error contando consent_dental enviados hoy: %s", e)
        return {"status": "error", "enviados": 0}

    if enviados_hoy >= LIMITE_DIA:
        log.info("dental_winback: consent blast limite diario %d alcanzado", LIMITE_DIA)
        return {"status": "limite_diario", "enviados": 0}

    cupo = LIMITE_DIA - enviados_hoy

    try:
        with bi_conn() as conn2:
            with conn2.cursor() as cur:
                cur.execute(
                    """
                    SELECT dc.telefono, dc.nombre
                    FROM bi.v_dental_cohortes_contactables dc
                    WHERE dc.telefono NOT IN (
                        SELECT phone FROM bi.dental_consent
                    )
                    AND dc.telefono NOT IN (
                        SELECT phone FROM bi.dental_opt_outs
                    )
                    LIMIT %s
                    """,
                    (cupo,),
                )
                candidates = [(r[0], r[1] or "Paciente") for r in cur.fetchall()]
    except Exception as e:
        log.error("dental_winback: error obteniendo candidatos consent dental: %s", e)
        return {"status": "error", "enviados": 0}

    log.info("dental_winback: consent blast -- %d candidatos", len(candidates))
    enviados = 0

    from messaging import send_whatsapp_template, render_template_body as _rtb
    from session import log_message as _lm

    for phone, nombre in candidates:
        _now = datetime.now(TZ_CHILE)
        if not (10 <= _now.hour < 19) or _now.weekday() >= 5:
            log.info("dental_winback: consent blast ventana cerrada en %d enviados", enviados)
            break
        try:
            await send_whatsapp_template(
                phone,
                "consent_dental_v2",
                body_params=[nombre],
            )
            _lm(phone, "out", _rtb("consent_dental_v2", [nombre]), "IDLE")
            registrar_dental_consent_enviado(phone)
            enviados += 1
            log.info("dental_winback: consent enviado phone=...%s (%d)", phone[-4:], enviados)
        except Exception as e:
            log.error("dental_winback: consent blast error phone=...%s: %s", phone[-4:], e)
        await asyncio.sleep(SLEEP_ENTRE)

    log.info("dental_winback: consent blast finalizado, enviados=%d", enviados)
    return {"status": "ok", "enviados": enviados}


# -- Winback dental híbrido: session (gratis) cuando ventana 24h abierta ------

def _ventana_24h_abierta_dental(phone: str) -> bool:
    """Retorna True si bi.dental_consent.response_at es de las últimas 23h.

    Misma lógica que winback._ventana_24h_abierta pero para la tabla dental_consent.
    """
    tel = phone.lstrip("+").strip()
    try:
        with bi_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT 1 FROM bi.dental_consent
                    WHERE RIGHT(regexp_replace(phone, '[^0-9]', '', 'g'), 9)
                          = RIGHT(regexp_replace(%s,  '[^0-9]', '', 'g'), 9)
                      AND status = 'accepted'
                      AND response_at >= NOW() - INTERVAL '23 hours'
                    """,
                    (tel,),
                )
                return cur.fetchone() is not None
    except Exception as e:
        log.warning("dental_winback: _ventana_24h_abierta_dental error phone=...%s: %s", tel[-4:], e)
        return False


async def _send_dental_winback_session(candidato: dict) -> bool:
    """Envía el winback dental como session message (interactive buttons) — costo $0.

    Solo se llama cuando _ventana_24h_abierta_dental() retornó True.
    """
    _tel_raw = (candidato.get("telefono") or "").strip()
    _tel_digits = "".join(c for c in _tel_raw if c.isdigit())
    if len(_tel_digits) <= 9:
        _tel_digits = "56" + _tel_digits.lstrip("0")
    telefono = _tel_digits
    paciente_id = candidato.get("paciente_id", 0)
    nombre = (candidato.get("nombre") or "paciente").capitalize()
    especialidad = candidato.get("ultima_especialidad")
    profesional = candidato.get("ultimo_profesional") or ""
    subcohorte = candidato.get("subcohorte", "dental_odonto_general_180d")

    if not telefono or len(telefono) < 8:
        log.warning("dental_winback: session: telefono inválido paciente_id=%s", paciente_id)
        return False

    from config import CMC_TELEFONO_FIJO as _FIJO
    _fijo = _FIJO or "(44) 296 5226"

    # Si no hay especialidad clara, no enviar session winback dental — historial ambiguo.
    if not especialidad or not especialidad.strip():
        log.info(
            "dental_winback: session skip — especialidad nula/vacía paciente_id=%s phone=...%s",
            paciente_id, telefono[-4:],
        )
        return False

    _esp_display = especialidad.strip()
    _prof_part = f" con {profesional}" if profesional else ""

    body_text = (
        f"Hola {nombre}, te contactamos del Centro Médico Carampangue.\n\n"
        f"Llevas un tiempo sin tu control de {_esp_display}{_prof_part}. "
        f"Responde SÍ y te agendamos de inmediato — tenemos horas disponibles esta semana.\n\n"
        f"Estamos en Carampangue de lunes a viernes. También puedes llamarnos al {_fijo}.\n\n"
        f"Responde BAJA si no deseas recibir más mensajes."
    )

    interactive = {
        "type": "button",
        "body": {"text": body_text},
        "action": {
            "buttons": [
                {
                    "type": "reply",
                    "reply": {"id": "wb_agendar", "title": "Si, agendame"},
                },
                {
                    "type": "reply",
                    "reply": {"id": "wb_info", "title": "Ver horarios"},
                },
            ]
        },
    }

    try:
        from messaging import send_whatsapp_interactive
        from session import log_message as _lm_dws, save_campana_envio as _save_camp_s

        await send_whatsapp_interactive(telefono, interactive)
        _lm_dws(telefono, "out", body_text, "IDLE")

        _template_meta = f"SESSION_DENTAL_{subcohorte}"
        _registrar_envio_dental(
            paciente_id=paciente_id,
            telefono=telefono,
            subcohorte=subcohorte,
            template_name=_template_meta,
            especialidad=especialidad,
        )
        # Registrar en campanas_envios (SQLite) para medir conversión en panel admin
        try:
            _save_camp_s(telefono, f"dental_winback_session_{subcohorte}")
        except Exception as _e_cs:
            log.warning("dental_winback: save_campana_envio session error phone=...%s: %s", telefono[-4:], _e_cs)
        log.info(
            "dental_winback: sent via session phone=...%s subcohorte=%s value_clp=0",
            telefono[-4:], subcohorte,
        )
        return True
    except Exception as e:
        log.error("dental_winback: session send error phone=...%s: %s", telefono[-4:], e)
        return False


async def send_dental_winback_smart(candidato: dict, prefer_session: bool = True) -> bool:
    """Envía winback dental eligiendo la vía mas económica disponible.

    Si prefer_session=True y la ventana 24h está abierta (el paciente acaba
    de responder al consent_dental_v2), envía session message — costo $0.
    Si la ventana está cerrada, usa template MARKETING. El batch diario sigue
    usando send_dental_winback() directamente (ventana siempre cerrada).
    """
    phone = (candidato.get("telefono") or "").strip()
    if prefer_session and _ventana_24h_abierta_dental(phone):
        log.info("dental_winback: ventana abierta — session message phone=...%s", phone[-4:])
        return await _send_dental_winback_session(candidato)
    log.info("dental_winback: ventana cerrada — template MARKETING phone=...%s", phone[-4:])
    return await send_dental_winback(candidato)
