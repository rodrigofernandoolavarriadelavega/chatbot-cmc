"""Email Marketing — motor de segmentos para el Autopilot del CMC.

Un **segmento** = audiencia (criterios RFM/lifecycle sobre la base de pacientes) +
contenido (email con merge tags) + estrategia (horario, frequency cap) + gating de
consentimiento. Se renderiza en la pestaña "Email" del dashboard `/autopilot`.

Storage de segmentos: JSON atómico (mismo patrón que designs.py) en
`data/email_segments.json`. Las plantillas best-practice (SEGMENT_TEMPLATES) son
de solo lectura — el usuario las clona a un segmento editable.

Fuente de audiencia:
  1. BI Postgres (bi.dim_paciente + bi.fact_atenciones + bi.fact_pagos) — Medilink,
     trae el email del paciente y los ejes RFM. Es la fuente primaria.
  2. Fallback sessions.db (contact_profiles.email) si BI no responde.

Gating legal (Ley 21.719 — dato sensible de salud):
  - opt-outs SIEMPRE excluidos (bi.opt_outs_marketing + tags + consent revocado).
  - "contactable" exige opt-in de marketing vigente (privacy_consents.accepted).
  - el opt-in específico de canal email se cierra con doble opt-in ANTES de enviar;
    por eso el envío nace gated (EMAIL_SENDING_ENABLED=false).

Diseñado defensivo: si BI no está disponible (p. ej. dev local), degrada a
sessions.db y nunca tumba el dashboard.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone

log = logging.getLogger("bot")

SEGMENTS_PATH = os.getenv("EMAIL_SEGMENTS_PATH", "data/email_segments.json")

WA_BOT_NUMBER = "56966610737"  # número del bot (memory: nunca el personal del Dr.)


# ─────────────────────────────────────────────────────────────────────────────
# Lifecycle / RFM — DECISIÓN DE NEGOCIO. Tunear acá cambia toda la segmentación.
# Bandas por recencia (días desde la última atención) + frecuencia histórica.
# ─────────────────────────────────────────────────────────────────────────────
LIFECYCLE_BANDS = [
    # key            label                   regla(dias_inactivo, freq) → bool
    ("nuevos",       "Nuevos (1ª visita)",   lambda d, f: f <= 1 and d < 90),
    ("vip",          "Leales / alto valor",  lambda d, f: d < 90 and f >= 4),
    ("activos",      "Activos recientes",    lambda d, f: d < 90 and 2 <= f <= 3),
    ("en_riesgo",    "En riesgo (90-179d)",  lambda d, f: 90 <= d < 180),
    ("dormidos",     "Dormidos (180-364d)",  lambda d, f: 180 <= d < 365),
    ("perdidos",     "Perdidos (365d+)",     lambda d, f: d >= 365),
]
_LIFECYCLE_LABEL = {k: lbl for k, lbl, _ in LIFECYCLE_BANDS}


def _classify_lifecycle(dias_inactivo: int | None, freq: int) -> str | None:
    if dias_inactivo is None:
        return None
    for key, _lbl, rule in LIFECYCLE_BANDS:
        if rule(dias_inactivo, freq):
            return key
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Librería de segmentos best-practice (plantillas clonables, orientadas a venta).
# Cada plantilla trae copy de arranque ya optimizado: subject corto, preheader,
# CTA único a WhatsApp con UTM, merge tag {nombre}. El usuario las ajusta.
# ─────────────────────────────────────────────────────────────────────────────
def _wa(text: str, utm: str) -> str:
    msg = f"Hola, vengo del correo y quiero {text}".replace(" ", "%20")
    return f"https://wa.me/{WA_BOT_NUMBER}?text={msg}"


SEGMENT_TEMPLATES = [
    {
        "id": "tpl_bienvenida",
        "name": "Bienvenida a pacientes nuevos",
        "objetivo": "bienvenida",
        "why": "El primer email tras la 1ª visita fija la relación y dispara la 2ª compra. "
               "Onboarding = mayor LTV.",
        "criteria": {"lifecycle": ["nuevos"]},
        "email": {
            "from_name": "Centro Médico Carampangue",
            "subject": "Bienvenido/a al Centro Médico, {nombre}",
            "preheader": "Todo lo que puedes resolver con nosotros, cerca de ti en Carampangue.",
            "headline": "Gracias por tu primera visita, {nombre}",
            "body": "Nos alegra cuidarte. En el Centro Médico Carampangue tienes medicina general, "
                    "especialidades, dental, kinesiología y exámenes en un solo lugar.\n\n"
                    "Cuando necesites una hora, agéndala en segundos por WhatsApp.",
            "cta_label": "Agendar mi próxima hora",
            "cta_url": _wa("agendar una hora", "bienvenida"),
        },
        "schedule": {"best_day": "martes", "best_hour": 11, "cooldown_days": 30},
    },
    {
        "id": "tpl_winback_dormidos",
        "name": "Reactivar dormidos (180-364d)",
        "objetivo": "reactivacion",
        "why": "Recuperar un paciente dormido cuesta una fracción de adquirir uno nuevo. "
               "Recencia 180-364d aún recuerda la marca.",
        "criteria": {"lifecycle": ["dormidos"]},
        "email": {
            "from_name": "Centro Médico Carampangue",
            "subject": "{nombre}, ¿hace cuánto no te chequeas?",
            "preheader": "Tu salud no puede esperar. Reserva con tu equipo de siempre.",
            "headline": "Te echamos de menos, {nombre}",
            "body": "Ha pasado un tiempo desde tu última atención en tu especialidad de "
                    "{especialidad}. Un control a tiempo previene problemas mayores.\n\n"
                    "Tenemos horas disponibles esta semana.",
            "cta_label": "Reservar un control",
            "cta_url": _wa("reservar un control", "winback_dormidos"),
        },
        "schedule": {"best_day": "miércoles", "best_hour": 10, "cooldown_days": 45},
    },
    {
        "id": "tpl_reactivacion_perdidos",
        "name": "Recuperar perdidos (365d+)",
        "objetivo": "reactivacion",
        "why": "Cohorte fría pero de alto LTV potencial. Mensaje empático + facilidad para volver.",
        "criteria": {"lifecycle": ["perdidos"]},
        "email": {
            "from_name": "Centro Médico Carampangue",
            "subject": "{nombre}, seguimos aquí para cuidarte",
            "preheader": "Volver es fácil: una hora por WhatsApp y listo.",
            "headline": "Ha pasado más de un año, {nombre}",
            "body": "Sabemos que el tiempo vuela. Tu salud sigue siendo prioridad y queremos "
                    "facilitarte el regreso.\n\nAgenda con el equipo del Centro Médico cuando "
                    "quieras, sin filas ni llamados.",
            "cta_label": "Volver a agendar",
            "cta_url": _wa("volver a agendar", "reactivacion_perdidos"),
        },
        "schedule": {"best_day": "jueves", "best_hour": 10, "cooldown_days": 60},
    },
    {
        "id": "tpl_vip_preferente",
        "name": "Programa preferente (alto valor)",
        "objetivo": "fidelizacion",
        "why": "Los pacientes frecuentes (F≥4) son la base del ingreso. Reconocerlos sube "
               "retención y referidos.",
        "criteria": {"lifecycle": ["vip"]},
        "email": {
            "from_name": "Centro Médico Carampangue",
            "subject": "Gracias por tu confianza, {nombre}",
            "preheader": "Un beneficio pensado para quienes nos eligen siempre.",
            "headline": "Eres parte de nuestra comunidad, {nombre}",
            "body": "Tu confianza nos mueve. Como paciente frecuente, queremos que agendar y "
                    "controlarte sea aún más simple.\n\nRecuerda que también atendemos a tu familia "
                    "en las mismas especialidades.",
            "cta_label": "Agendar para mí o mi familia",
            "cta_url": _wa("agendar una hora", "vip"),
        },
        "schedule": {"best_day": "martes", "best_hour": 12, "cooldown_days": 45},
    },
    {
        "id": "tpl_crosssell_kine",
        "name": "Cross-sell: traumatología → kinesiología",
        "objetivo": "cross_sell",
        "why": "El paciente traumatológico casi siempre necesita rehabilitación. Cross-sell "
               "natural y de alto cierre.",
        "criteria": {"lifecycle": ["activos", "vip", "en_riesgo"],
                     "especialidades": ["Traumatología"]},
        "email": {
            "from_name": "Centro Médico Carampangue",
            "subject": "{nombre}, tu recuperación continúa",
            "preheader": "Kinesiología para volver a moverte sin dolor.",
            "headline": "El siguiente paso de tu tratamiento",
            "body": "Tras tu atención de {especialidad}, la kinesiología acelera la recuperación "
                    "y previene recaídas.\n\nTenemos kinesiólogos disponibles y también atención "
                    "a domicilio.",
            "cta_label": "Agendar kinesiología",
            "cta_url": _wa("agendar kinesiología", "crosssell_kine"),
        },
        "schedule": {"best_day": "lunes", "best_hour": 17, "cooldown_days": 30},
    },
    {
        "id": "tpl_chequeo_preventivo",
        "name": "Chequeo preventivo por edad",
        "objetivo": "venta",
        "why": "Exámenes preventivos (PAP, mamografía, PSA, perfil) son recurrentes y de margen sano. "
               "Segmentar por edad/sexo eleva relevancia.",
        "criteria": {"lifecycle": ["activos", "vip", "en_riesgo", "dormidos"], "edad_min": 40},
        "email": {
            "from_name": "Centro Médico Carampangue",
            "subject": "{nombre}, ¿al día con tu chequeo anual?",
            "preheader": "Exámenes preventivos a partir de los 40. Reserva en minutos.",
            "headline": "Prevenir es más fácil que tratar",
            "body": "A partir de los 40 conviene un chequeo anual: presión, perfil lipídico, "
                    "glicemia y los exámenes preventivos que correspondan.\n\nCoordina tu chequeo "
                    "con nuestro equipo de medicina general.",
            "cta_label": "Agendar mi chequeo",
            "cta_url": _wa("agendar un chequeo preventivo", "chequeo"),
        },
        "schedule": {"best_day": "miércoles", "best_hour": 9, "cooldown_days": 90},
    },
]
_TEMPLATE_BY_ID = {t["id"]: t for t in SEGMENT_TEMPLATES}


# ─────────────────────────────────────────────────────────────────────────────
# Persistencia (JSON atómico, igual que designs.py)
# ─────────────────────────────────────────────────────────────────────────────
def load_segments() -> list[dict]:
    try:
        with open(SEGMENTS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("segments", []) if isinstance(data, dict) else data
    except FileNotFoundError:
        return []
    except Exception as e:  # noqa: BLE001 — nunca tumbar el dashboard
        log.error("email_segments.load falló: %s", e)
        return []


def _save_all(segments: list[dict]) -> None:
    os.makedirs(os.path.dirname(SEGMENTS_PATH) or ".", exist_ok=True)
    tmp = SEGMENTS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"segments": segments,
                   "updated_at": datetime.now(timezone.utc).isoformat()},
                  f, ensure_ascii=False, indent=2, default=str)
    os.replace(tmp, SEGMENTS_PATH)


def _new_id() -> str:
    return f"seg_{int(datetime.now(timezone.utc).timestamp())}"


def add_segment(record: dict) -> dict:
    segments = load_segments()
    rid = str(record.get("id") or _new_id())
    record["id"] = rid
    record.setdefault("status", "borrador")
    record.setdefault("created_at", datetime.now(timezone.utc).isoformat())
    record["updated_at"] = datetime.now(timezone.utc).isoformat()
    segments = [s for s in segments if str(s.get("id")) != rid]
    segments.insert(0, record)
    _save_all(segments)
    return record


def delete_segment(rid: str) -> bool:
    segments = load_segments()
    kept = [s for s in segments if str(s.get("id")) != str(rid)]
    if len(kept) == len(segments):
        return False
    _save_all(kept)
    return True


# ─────────────────────────────────────────────────────────────────────────────
# Audiencia — universo RFM desde BI (defensivo) + cruce con consent
# ─────────────────────────────────────────────────────────────────────────────
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _valid_email(e: str | None) -> bool:
    return bool(e) and bool(_EMAIL_RE.match(e.strip()))


def _bi_universe() -> list[dict] | None:
    """Trae el universo RFM completo de pacientes desde BI.

    Devuelve None si BI no está disponible (el caller degrada a sessions.db).
    Cada fila: paciente_id, nombre, email, telefono, comuna, edad, genero,
    freq, dias_inactivo, especialidad, gasto_total.
    """
    try:
        from winback import bi_conn
    except Exception:
        return None
    try:
        with bi_conn() as conn:
            with conn.cursor() as cur:
                # Excluir opt-outs solo si la tabla existe (defensivo).
                cur.execute("SELECT to_regclass('bi.opt_outs_marketing')")
                has_optout = cur.fetchone()[0] is not None
                optout_clause = (
                    "AND NOT EXISTS (SELECT 1 FROM bi.opt_outs_marketing oo "
                    "WHERE oo.phone = p.telefono)" if has_optout else ""
                )
                cur.execute(f"""
                    WITH agg AS (
                        SELECT paciente_id, MAX(fecha) AS ultima, COUNT(*) AS freq
                        FROM bi.fact_atenciones GROUP BY paciente_id
                    ),
                    ult_esp AS (
                        SELECT DISTINCT ON (a.paciente_id)
                               a.paciente_id, e.nombre AS especialidad
                        FROM bi.fact_atenciones a
                        LEFT JOIN bi.dim_profesional pr ON pr.profesional_id = a.profesional_id
                        LEFT JOIN bi.dim_especialidad e ON e.especialidad_id = pr.especialidad_id
                        ORDER BY a.paciente_id, a.fecha DESC, a.atencion_id DESC
                    ),
                    mon AS (
                        SELECT paciente_id, COALESCE(SUM(monto_neto), 0) AS gasto
                        FROM bi.fact_pagos GROUP BY paciente_id
                    )
                    SELECT p.paciente_id,
                           COALESCE(p.nombre, '')        AS nombre,
                           p.email, p.telefono,
                           COALESCE(p.comuna, '')         AS comuna,
                           EXTRACT(YEAR FROM AGE(p.fecha_nacimiento))::int AS edad,
                           COALESCE(p.genero, '')         AS genero,
                           a.freq,
                           (CURRENT_DATE - a.ultima)::int AS dias_inactivo,
                           ue.especialidad,
                           COALESCE(m.gasto, 0)::float    AS gasto_total
                    FROM bi.dim_paciente p
                    JOIN agg a       ON a.paciente_id = p.paciente_id
                    LEFT JOIN ult_esp ue ON ue.paciente_id = p.paciente_id
                    LEFT JOIN mon m   ON m.paciente_id = p.paciente_id
                    WHERE COALESCE(p.es_activo, true) = true
                    {optout_clause}
                """)
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception as e:
        log.warning("email_segments: BI no disponible (%s) — fallback sessions.db", e)
        return None


def _matches_criteria(row: dict, crit: dict) -> bool:
    """Aplica los criterios del segmento a una fila del universo RFM."""
    life = _classify_lifecycle(row.get("dias_inactivo"), row.get("freq") or 0)
    want_life = crit.get("lifecycle") or []
    if want_life and life not in want_life:
        return False
    esp = crit.get("especialidades") or []
    if esp and (row.get("especialidad") or "") not in esp:
        return False
    comunas = crit.get("comunas") or []
    if comunas and (row.get("comuna") or "") not in comunas:
        return False
    edad = row.get("edad")
    if crit.get("edad_min") is not None and (edad is None or edad < crit["edad_min"]):
        return False
    if crit.get("edad_max") is not None and (edad is None or edad > crit["edad_max"]):
        return False
    gen = crit.get("genero")
    if gen and (row.get("genero") or "").upper()[:1] != gen.upper()[:1]:
        return False
    return True


def _mask_email(e: str) -> str:
    try:
        user, dom = e.split("@", 1)
        head = user[:2] if len(user) > 2 else user[:1]
        return f"{head}{'*' * max(1, len(user) - len(head))}@{dom}"
    except Exception:
        return "***"


def resolve_audience(segment: dict, sample_size: int = 5) -> dict:
    """Resuelve la audiencia de un segmento en capas (embudo de entregabilidad).

    matched → con email → contactable (opt-in marketing, sin opt-out) = deliverable.
    Es transparente sobre por qué el alcance baja en cada capa: así el usuario ve
    que el cuello es el opt-in de email, no el segmento.
    """
    from session import get_email_consent_sets, _normalize_phone_e164

    crit = segment.get("criteria") or {}
    consent = get_email_consent_sets()
    consented, opted_out = consent["consented"], consent["opted_out"]

    universe = _bi_universe()
    bi_available = universe is not None

    if not bi_available:
        return _resolve_sessions_fallback(crit, consented, opted_out, sample_size)

    matched = [r for r in universe if _matches_criteria(r, crit)]
    with_email = [r for r in matched if _valid_email(r.get("email"))]

    deliverable, suppressed, no_consent, sample = [], 0, 0, []
    by_life: dict[str, int] = {}
    for r in with_email:
        phone = _normalize_phone_e164(r.get("telefono") or "")
        if phone and phone in opted_out:
            suppressed += 1
            continue
        if not (phone and phone in consented):
            no_consent += 1
            continue
        deliverable.append(r)
        life = _classify_lifecycle(r.get("dias_inactivo"), r.get("freq") or 0) or "—"
        by_life[life] = by_life.get(life, 0) + 1
        if len(sample) < sample_size:
            sample.append({
                "nombre": (r.get("nombre") or "").split(" ")[0] or "—",
                "email": _mask_email(r["email"]),
                "especialidad": r.get("especialidad") or "—",
                "dias_inactivo": r.get("dias_inactivo"),
                "lifecycle": _LIFECYCLE_LABEL.get(life, life),
            })

    return {
        "bi_available": True,
        "matched": len(matched),
        "with_email": len(with_email),
        "suppressed_optout": suppressed,
        "no_email_consent": no_consent,
        "deliverable": len(deliverable),
        "coverage_pct": round(100 * len(with_email) / len(matched), 1) if matched else 0,
        "by_lifecycle": {_LIFECYCLE_LABEL.get(k, k): v for k, v in by_life.items()},
        "sample": sample,
        "warnings": _audience_warnings(len(matched), len(with_email), len(deliverable)),
    }


def _resolve_sessions_fallback(crit, consented, opted_out, sample_size) -> dict:
    """Fallback a sessions.db cuando BI no responde (cobertura parcial)."""
    from session import _conn, _normalize_phone_e164

    with _conn() as conn:
        rows = conn.execute(
            "SELECT phone, nombre, email FROM contact_profiles "
            "WHERE email IS NOT NULL AND email <> ''"
        ).fetchall()
    with_email = [dict(r) for r in rows if _valid_email(r["email"])]
    deliverable, sample = [], []
    for r in with_email:
        phone = _normalize_phone_e164(r["phone"] or "")
        if phone and phone in opted_out:
            continue
        if not (phone and phone in consented):
            continue
        deliverable.append(r)
        if len(sample) < sample_size:
            sample.append({"nombre": (r.get("nombre") or "").split(" ")[0] or "—",
                           "email": _mask_email(r["email"]), "especialidad": "—",
                           "dias_inactivo": None, "lifecycle": "—"})
    return {
        "bi_available": False,
        "matched": len(with_email), "with_email": len(with_email),
        "suppressed_optout": 0, "no_email_consent": len(with_email) - len(deliverable),
        "deliverable": len(deliverable), "coverage_pct": 0,
        "by_lifecycle": {}, "sample": sample,
        "warnings": ["BI no disponible: cobertura parcial desde sessions.db. "
                     "Los criterios RFM/lifecycle no se aplican en este modo."],
    }


def _audience_warnings(matched: int, with_email: int, deliverable: int) -> list[str]:
    w = []
    if matched == 0:
        w.append("Ningún paciente cumple estos criterios. Amplía el segmento.")
    elif matched < 20:
        w.append(f"Audiencia muy chica ({matched}). El A/B y las métricas no serán confiables.")
    if matched and with_email / max(matched, 1) < 0.4:
        w.append("Menos del 40% tiene email registrado. Considera capturar email en el bot.")
    if with_email and deliverable == 0:
        w.append("0 contactables: falta opt-in de email. Activa el doble opt-in antes de enviar.")
    return w


# ─────────────────────────────────────────────────────────────────────────────
# Best-practice checklist — "salud del segmento" (0-100)
# ─────────────────────────────────────────────────────────────────────────────
_SPAM_WORDS = ["gratis", "$$$", "100%", "urgente", "oferta", "click aquí", "ganador",
               "promoción", "descuento", "!!!", "compra ya"]


def score_segment(segment: dict, audience: dict | None = None) -> dict:
    """Evalúa el segmento contra las best-practices de email marketing.

    Cada check: {key, label, level: ok|warn|fail, msg, weight}. El score es el
    % ponderado de checks 'ok' (warn cuenta medio).
    """
    em = segment.get("email") or {}
    sched = segment.get("schedule") or {}
    crit = segment.get("criteria") or {}
    subject = (em.get("subject") or "").strip()
    preheader = (em.get("preheader") or "").strip()
    body = (em.get("body") or "").strip()
    cta_url = (em.get("cta_url") or "").strip()
    checks: list[dict] = []

    def add(key, label, ok, warn_if, ok_msg, bad_msg, weight=1):
        level = "ok" if ok else ("warn" if warn_if else "fail")
        checks.append({"key": key, "label": label, "level": level,
                       "msg": ok_msg if ok else bad_msg, "weight": weight})

    # 1. Consentimiento (gate legal, no negociable)
    add("consent", "Gating de consentimiento",
        True, False,
        "Solo se envía a pacientes con opt-in de marketing (Ley 21.719).",
        "", weight=2)

    # 2. Asunto: longitud
    slen = len(subject)
    add("subject_len", "Asunto (20-55 caracteres)",
        20 <= slen <= 55, 0 < slen < 20 or 55 < slen <= 70,
        f"Asunto de {slen} caracteres: buen largo para móvil.",
        f"Asunto de {slen} caracteres: idealmente 20-55 para no cortarse en móvil.")

    # 3. Asunto sin spam words / mayúsculas
    low = subject.lower()
    spammy = [w for w in _SPAM_WORDS if w in low] or \
             (["MAYÚSCULAS"] if subject and subject.upper() == subject else [])
    add("subject_spam", "Asunto sin gatillos de spam",
        not spammy, bool(spammy),
        "Sin palabras que activen filtros de spam.",
        f"Evita: {', '.join(spammy)}. Daña la entregabilidad.")

    # 4. Preheader
    plen = len(preheader)
    add("preheader", "Preheader / texto de preview",
        40 <= plen <= 110, 0 < plen < 40,
        "Preheader presente: aumenta el open rate.",
        "Define un preheader de 40-110 caracteres (es el texto bajo el asunto).")

    # 5. Personalización
    has_merge = "{nombre}" in subject or "{nombre}" in body or "{nombre}" in (em.get("headline") or "")
    add("personalization", "Personalización ({nombre})",
        has_merge, False,
        "Usa el nombre del paciente: más apertura y cercanía.",
        "Agrega {nombre} en el asunto o cuerpo para personalizar.")

    # 6. CTA único y válido (WhatsApp)
    cta_ok = cta_url.startswith("https://wa.me/") or cta_url.startswith("http")
    add("cta", "CTA único y accionable",
        cta_ok and bool(em.get("cta_label")), False,
        "CTA claro hacia WhatsApp (canal de conversión del CMC).",
        "Define un CTA único con enlace a WhatsApp (wa.me).")

    # 7. Cuerpo con largo razonable
    blen = len(body)
    add("body", "Cuerpo conciso (200-1200)",
        200 <= blen <= 1200, 0 < blen < 200 or 1200 < blen <= 2000,
        "Cuerpo con largo de lectura óptima.",
        f"Cuerpo de {blen} caracteres: apunta a 200-1200, directo al beneficio.")

    # 8. Frequency cap
    cooldown = sched.get("cooldown_days") or 0
    add("frequency", "Frequency cap (≥14 días)",
        cooldown >= 14, 0 < cooldown < 14,
        f"Cooldown de {cooldown}d: respeta la bandeja del paciente.",
        "Define un cooldown ≥14 días para no fatigar la lista.")

    # 9. Segmentación específica (no "a todos")
    has_crit = any(crit.get(k) for k in ("lifecycle", "especialidades", "comunas",
                                          "edad_min", "edad_max", "genero"))
    add("targeting", "Segmentación específica",
        has_crit, False,
        "Segmento acotado: más relevancia, mejor conversión.",
        "Sin criterios = enviar a todos. Acota por lifecycle/especialidad/edad.")

    # 10. Send-time definido
    add("send_time", "Horario de envío óptimo",
        sched.get("best_hour") is not None, False,
        f"Programado {sched.get('best_day', '')} {sched.get('best_hour')}:00.",
        "Define día y hora de envío (mar-jue 9-12h rinde mejor en salud).")

    # 11. Baja / unsubscribe (siempre inyectado por el renderer)
    add("unsubscribe", "Link de baja (legal)",
        True, False,
        "El renderer inyecta link de baja + List-Unsubscribe automáticamente.",
        "", weight=1)

    # 12. Audiencia entregable
    if audience is not None:
        deliv = audience.get("deliverable", 0)
        add("deliverable", "Audiencia entregable",
            deliv >= 20, 0 < deliv < 20,
            f"{deliv} pacientes contactables con consentimiento.",
            "Audiencia entregable insuficiente: revisa cobertura de email y opt-in.")

    total_w = sum(c["weight"] for c in checks)
    got = sum(c["weight"] * (1 if c["level"] == "ok" else 0.5 if c["level"] == "warn" else 0)
              for c in checks)
    score = round(100 * got / total_w) if total_w else 0
    grade = "A" if score >= 90 else "B" if score >= 75 else "C" if score >= 60 else "D"
    return {"score": score, "grade": grade, "checks": checks}


def specialty_options() -> list[str]:
    """Lista de especialidades presentes en BI (para el constructor de segmentos)."""
    universe = _bi_universe()
    if not universe:
        return []
    return sorted({(r.get("especialidad") or "").strip()
                   for r in universe if r.get("especialidad")})
