"""Capa unificada Audiencia → Canal.

Hoy hay tres formas de activar pacientes, cada una con su consent por ley:
  • WhatsApp  → winback.py            (consent general de marketing, Ley 19.628)
  • Email     → email_segments.py     (doble opt-in de canal, Ley 21.719)
  • Meta Ads  → custom_audiences_sync (teléfono hasheado, respeta opt-out)

Las tres comparten el MISMO universo (BI de pacientes) pero filtran distinto. Esta
capa expone una sola definición de audiencia (criterios RFM) y responde, para cada
canal, "¿a cuántos puedo llegar legalmente?" y "actívalo". Es un **facade**: delega
en los módulos existentes, no reimplementa su lógica ni toca su estado.

Pensado como el siguiente salto de Alma: una audiencia, varios canales, un solo
lugar donde se ve el alcance real por canal (y el cuello de consent de cada uno).
"""
from __future__ import annotations

import logging

log = logging.getLogger("bot")

CHANNELS = ("whatsapp", "email", "meta")

CHANNEL_LABEL = {
    "whatsapp": "WhatsApp",
    "email": "Email",
    "meta": "Meta (Custom Audience)",
}

# Qué consent exige cada canal (para explicarlo en la UI, no para gatear acá).
CHANNEL_CONSENT = {
    "whatsapp": "Opt-in general de marketing (Ley 19.628)",
    "email": "Doble opt-in de canal email confirmado (Ley 21.719)",
    "meta": "Teléfono hasheado SHA-256; respeta opt-out de marketing",
}


def _universe(criteria: dict) -> list[dict]:
    """Universo canónico de la audiencia: el mismo BI que usa email_segments,
    filtrado por los criterios RFM. Única fuente de verdad de 'quién está en el
    segmento' antes de aplicar el gate de cada canal."""
    from . import email_segments as es
    uni = es._bi_universe()
    if uni is None:
        return []
    return [r for r in uni if es._matches_criteria(r, criteria or {})]


def _norm(phone: str) -> str:
    try:
        from session import _normalize_phone_e164
        return _normalize_phone_e164(phone or "") or ""
    except Exception:  # noqa: BLE001
        return (phone or "").lstrip("+")


# ─────────────────────────────────────────────────────────────────────────────
# Alcance por canal
# ─────────────────────────────────────────────────────────────────────────────
def _reach_whatsapp(rows: list[dict]) -> int:
    """Pacientes con consent de marketing vigente y sin opt-out (gate de winback)."""
    try:
        from winback import has_marketing_consent, phone_in_opt_out
    except Exception as e:  # noqa: BLE001
        log.warning("winback no disponible para reach: %s", e)
        return 0
    n = 0
    for r in rows:
        ph = _norm(r.get("telefono") or "")
        if not ph:
            continue
        try:
            if has_marketing_consent(ph) and not phone_in_opt_out(ph):
                n += 1
        except Exception:  # noqa: BLE001
            continue
    return n


def _reach_email(rows: list[dict]) -> int:
    """Pacientes entregables por email (email válido + doble opt-in confirmado)."""
    from . import email_segments as es
    optin = es._email_optin_sets()
    consent = _email_consent()
    consented, opted_out = consent["consented"], consent["opted_out"]
    n = 0
    for r in rows:
        if not es._valid_email(r.get("email")):
            continue
        ph = _norm(r.get("telefono") or "")
        em = (r.get("email") or "").strip().lower()
        if ph and (ph in opted_out or ph in optin["revoked_phones"]):
            continue
        if not (ph and ph in consented):
            continue
        if es.EMAIL_REQUIRE_DOUBLE_OPTIN and not (
                ph in optin["confirmed_phones"] or em in optin["confirmed_emails"]):
            continue
        n += 1
    return n


def _reach_meta(rows: list[dict]) -> int:
    """Teléfonos válidos para subir a Custom Audience, excluyendo opt-out."""
    try:
        from winback import phone_in_opt_out
    except Exception:  # noqa: BLE001
        phone_in_opt_out = lambda _ph: False  # noqa: E731
    n = 0
    for r in rows:
        ph = _norm(r.get("telefono") or "")
        if len(ph) >= 11 and not _safe(phone_in_opt_out, ph):
            n += 1
    return n


def _safe(fn, *a):
    try:
        return fn(*a)
    except Exception:  # noqa: BLE001
        return False


def _email_consent() -> dict:
    try:
        from session import get_email_consent_sets
        return get_email_consent_sets()
    except Exception:  # noqa: BLE001
        return {"consented": set(), "opted_out": set()}


def audience_overview(criteria: dict) -> dict:
    """Una audiencia, alcance lado a lado por canal. El valor de la capa unificada:
    ver en un solo lugar a cuántos del MISMO segmento puedo llegar por cada canal y
    cuál es el consent que lo limita."""
    rows = _universe(criteria)
    total = len(rows)
    with_email = sum(1 for r in rows if _valid_email_quiet(r.get("email")))
    by_channel = {
        "whatsapp": _reach_whatsapp(rows),
        "email": _reach_email(rows),
        "meta": _reach_meta(rows),
    }
    return {
        "total": total,
        "with_email": with_email,
        "channels": [
            {"key": k, "label": CHANNEL_LABEL[k], "consent": CHANNEL_CONSENT[k],
             "reachable": by_channel[k],
             "pct": round(100 * by_channel[k] / total, 1) if total else 0.0}
            for k in CHANNELS
        ],
    }


def _valid_email_quiet(e) -> bool:
    from . import email_segments as es
    return es._valid_email(e)


def recipients_for(criteria: dict, channel: str, limit: int = 200) -> list[dict]:
    """Filas entregables del segmento por un canal concreto. Delega en el gate del
    canal. Para email reusa resolve_recipients (incluye orden por recencia)."""
    if channel == "email":
        from . import email_segments as es
        return es.resolve_recipients({"criteria": criteria}, limit=limit)
    rows = _universe(criteria)
    if channel == "whatsapp":
        return [r for r in rows if _safe(_wa_ok, r)][:limit]
    if channel == "meta":
        return [r for r in rows if len(_norm(r.get("telefono") or "")) >= 11][:limit]
    return []


def _wa_ok(r: dict) -> bool:
    from winback import has_marketing_consent, phone_in_opt_out
    ph = _norm(r.get("telefono") or "")
    return bool(ph) and has_marketing_consent(ph) and not phone_in_opt_out(ph)


# ─────────────────────────────────────────────────────────────────────────────
# Activación (delegada, GATED en cada canal)
# ─────────────────────────────────────────────────────────────────────────────
async def activate(criteria: dict, channel: str, payload: dict | None = None,
                   *, dry_run: bool = True, limit: int = 200) -> dict:
    """Activa la audiencia por un canal. Cada canal mantiene SU gate:
      email → email_tracking.send_segment (gated EMAIL_SENDING_ENABLED + dry_run)
      whatsapp/meta → de momento devuelven el plan (no auto-contactan): la ejecución
        real vive en sus jobs gateados (winback / custom_audiences). No los disparamos
        desde acá para no saltar sus salvaguardas.
    """
    payload = payload or {}
    if channel == "email":
        from . import email_tracking
        seg = {"id": payload.get("segment_id", "adhoc"),
               "name": payload.get("name", "Audiencia ad-hoc"),
               "criteria": criteria, "email": payload.get("email", {})}
        return await email_tracking.send_segment(seg, limit=limit, dry_run=dry_run)
    if channel in ("whatsapp", "meta"):
        recips = recipients_for(criteria, channel, limit=limit)
        return {"channel": channel, "planned": len(recips), "dry_run": True,
                "note": f"Activación {CHANNEL_LABEL[channel]} se ejecuta en su job "
                        f"gateado; esta capa solo arma el plan."}
    return {"error": f"canal desconocido: {channel}"}
