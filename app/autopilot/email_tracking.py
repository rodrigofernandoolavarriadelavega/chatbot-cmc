"""Tracking + atribución de email marketing.

Cierra el loop que antes se cortaba en "preview": registra cada envío, mide
apertura (pixel) y clic (redirect), aplica cooldown por paciente, y enlaza el clic
con la cadena de atribución existente (wa.me con marcador `(email: <segmento>)`,
el mismo mecanismo que el marcador web — lo único que WhatsApp transmite).

Tabla `email_envios` en sessions.db. Tokens opacos por envío para los enlaces de
pixel/clic. Todo el envío real está GATED aguas arriba por `send_segment()`.

Flujo de un envío:
  record_queued() → token
  inject_tracking(html, token, cta_url) → reescribe el CTA al /e/c/{token} y
      agrega el pixel /e/o/{token}.png
  send_email(...) (gated)  → mark_sent()/mark_error()/mark_gated()
  destinatario abre  → record_open(token)
  destinatario clic  → record_click(token) → redirect a wa.me con marcador
"""
from __future__ import annotations

import logging
import os
import secrets
from urllib.parse import quote

log = logging.getLogger("bot")

PUBLIC_BASE = os.getenv("PUBLIC_BASE_URL", "https://agentecmc.cl").rstrip("/")
WA_NUMBER = os.getenv("CMC_WA_NUMBER", "56966610737")
DEFAULT_COOLDOWN_DAYS = int(os.getenv("EMAIL_COOLDOWN_DAYS", "14"))


def _conn():
    from session import _conn as _c
    return _c()


def _norm_phone(phone: str) -> str:
    try:
        from session import _normalize_phone_e164
        return _normalize_phone_e164(phone or "") or ""
    except Exception:  # noqa: BLE001
        return (phone or "").lstrip("+")


def _ensure_table(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS email_envios (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            token        TEXT UNIQUE,
            phone        TEXT,
            email        TEXT,
            segment_id   TEXT,
            segment_name TEXT,
            subject      TEXT,
            cta_url      TEXT,
            provider_id  TEXT,
            status       TEXT DEFAULT 'queued',  -- queued|sent|error|gated
            error        TEXT,
            sent_at      TEXT,
            opened_at    TEXT,
            clicked_at   TEXT,
            created_at   TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_env_token ON email_envios(token)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_env_phone ON email_envios(phone)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_env_seg   ON email_envios(segment_id)")


def _new_token() -> str:
    return secrets.token_urlsafe(18)


# ─────────────────────────────────────────────────────────────────────────────
# Cooldown
# ─────────────────────────────────────────────────────────────────────────────
def can_send(phone: str, cooldown_days: int = DEFAULT_COOLDOWN_DAYS) -> bool:
    """True si NO se le envió un email en los últimos `cooldown_days`. Respeta el
    frequency cap: no quemamos al paciente con correos seguidos."""
    phone = _norm_phone(phone)
    if not phone:
        return False
    with _conn() as conn:
        _ensure_table(conn)
        row = conn.execute(
            "SELECT 1 FROM email_envios WHERE phone=? AND status='sent' "
            "AND sent_at >= datetime('now', ?) LIMIT 1",
            (phone, f"-{int(cooldown_days)} days"),
        ).fetchone()
        return row is None


# ─────────────────────────────────────────────────────────────────────────────
# Registro de envío
# ─────────────────────────────────────────────────────────────────────────────
def record_queued(phone: str, email: str, *, segment_id: str = "", segment_name: str = "",
                  subject: str = "", cta_url: str = "") -> str:
    phone = _norm_phone(phone)
    token = _new_token()
    with _conn() as conn:
        _ensure_table(conn)
        conn.execute(
            "INSERT INTO email_envios (token, phone, email, segment_id, segment_name, "
            "subject, cta_url, status) VALUES (?,?,?,?,?,?,?, 'queued')",
            (token, phone, (email or "").lower(), segment_id, segment_name, subject, cta_url),
        )
        conn.commit()
    return token


def mark_sent(token: str, provider_id: str | None = None) -> None:
    with _conn() as conn:
        _ensure_table(conn)
        conn.execute(
            "UPDATE email_envios SET status='sent', provider_id=?, sent_at=datetime('now') "
            "WHERE token=?", (provider_id, token))
        conn.commit()


def mark_error(token: str, error: str) -> None:
    with _conn() as conn:
        _ensure_table(conn)
        conn.execute("UPDATE email_envios SET status='error', error=? WHERE token=?",
                     ((error or "")[:300], token))
        conn.commit()


def mark_gated(token: str, reason: str = "") -> None:
    with _conn() as conn:
        _ensure_table(conn)
        conn.execute("UPDATE email_envios SET status='gated', error=? WHERE token=?",
                     ((reason or "")[:300], token))
        conn.commit()


def record_open(token: str) -> bool:
    with _conn() as conn:
        _ensure_table(conn)
        cur = conn.execute(
            "UPDATE email_envios SET opened_at=COALESCE(opened_at, datetime('now')) "
            "WHERE token=?", (token,))
        conn.commit()
        return cur.rowcount > 0


def record_click(token: str) -> dict | None:
    """Marca el clic y devuelve {phone, cta_url, segment_id, segment_name} para el redirect."""
    with _conn() as conn:
        _ensure_table(conn)
        row = conn.execute(
            "SELECT phone, cta_url, segment_id, segment_name FROM email_envios WHERE token=?",
            (token,)).fetchone()
        if not row:
            return None
        conn.execute(
            "UPDATE email_envios SET clicked_at=COALESCE(clicked_at, datetime('now')), "
            "opened_at=COALESCE(opened_at, datetime('now')) WHERE token=?", (token,))
        conn.commit()
        return dict(row)


# ─────────────────────────────────────────────────────────────────────────────
# Enlaces de tracking + inyección en el HTML
# ─────────────────────────────────────────────────────────────────────────────
def click_url(token: str) -> str:
    return f"{PUBLIC_BASE}/e/c/{token}"


def pixel_url(token: str) -> str:
    return f"{PUBLIC_BASE}/e/o/{token}.png"


def unsubscribe_url(optin_token: str) -> str:
    return f"{PUBLIC_BASE}/email/baja?t={quote(optin_token)}"


def confirm_url(optin_token: str) -> str:
    return f"{PUBLIC_BASE}/email/confirmar?t={quote(optin_token)}"


async def send_optin_confirmation(phone: str, email: str, nombre: str = "") -> dict:
    """Paso 2 del doble opt-in: el paciente dijo 'sí' en WhatsApp → le mandamos el
    correo de confirmación con el enlace. GATED por EMAIL_SENDING_ENABLED (si el canal
    no está configurado, registra el opt-in en estado 'requested' igual — la captura
    de consentimiento no depende de que el correo salga). Nunca lanza."""
    from . import email_optin
    from .email_render import render_confirmation_email, send_email

    token = email_optin.request_email_optin(phone, email, source="bot")
    if not token:
        return {"status": "skipped", "reason": "ya confirmado o revocado"}
    html_body = render_confirmation_email(confirm_url(token), nombre)
    res = await send_email(email, "Confirma tu suscripción — Centro Médico Carampangue",
                           html_body, unsubscribe_url=unsubscribe_url(token))
    if res.get("status") == "sent":
        email_optin.mark_optin_pending(phone)
    return {"status": res.get("status"), "token": token, **(
        {"reasons": res.get("reasons")} if res.get("reasons") else {})}


def wa_attribution_url(segment_id: str, base_text: str = "Hola, quiero agendar una hora.") -> str:
    """wa.me con marcador `(email: <segmento>)` — lo detecta flows.py y taggea
    referral_source:email_<segmento>, cerrando email → conversación → cita."""
    slug = "".join(c for c in (segment_id or "").lower() if c.isalnum() or c in "-_")[:32]
    marker = f" (email: {slug})" if slug else " (email)"
    return f"https://wa.me/{WA_NUMBER}?text={quote(base_text + marker)}"


def inject_tracking(html_body: str, token: str, cta_url: str) -> str:
    """Reescribe el href del CTA al redirect de tracking y agrega el pixel de apertura
    antes de </body>. Si no encuentra el CTA, igual agrega el pixel."""
    out = html_body
    if cta_url and cta_url in out:
        out = out.replace(f'href="{cta_url}"', f'href="{click_url(token)}"')
    pixel = (f'<img src="{pixel_url(token)}" width="1" height="1" alt="" '
             f'style="display:none;width:1px;height:1px">')
    if "</body>" in out:
        out = out.replace("</body>", pixel + "</body>", 1)
    else:
        out += pixel
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Estadísticas de campaña
# ─────────────────────────────────────────────────────────────────────────────
def campaign_stats(segment_id: str | None = None) -> dict:
    where = "WHERE segment_id=?" if segment_id else ""
    args = (segment_id,) if segment_id else ()
    with _conn() as conn:
        _ensure_table(conn)
        row = conn.execute(f"""
            SELECT
              SUM(CASE WHEN status='sent'  THEN 1 ELSE 0 END) AS sent,
              SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS errors,
              SUM(CASE WHEN opened_at  IS NOT NULL THEN 1 ELSE 0 END) AS opened,
              SUM(CASE WHEN clicked_at IS NOT NULL THEN 1 ELSE 0 END) AS clicked
            FROM email_envios {where}
        """, args).fetchone()
    sent = row["sent"] or 0
    opened = row["opened"] or 0
    clicked = row["clicked"] or 0
    return {
        "sent": sent, "errors": row["errors"] or 0,
        "opened": opened, "clicked": clicked,
        "open_rate": round(100 * opened / sent, 1) if sent else 0.0,
        "click_rate": round(100 * clicked / sent, 1) if sent else 0.0,
        "ctr_of_opens": round(100 * clicked / opened, 1) if opened else 0.0,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Orquestación de envío de un segmento — GATED
# ─────────────────────────────────────────────────────────────────────────────
async def send_segment(segment: dict, *, limit: int = 200,
                       cooldown_days: int = DEFAULT_COOLDOWN_DAYS,
                       dry_run: bool = True) -> dict:
    """Envía un segmento a su audiencia entregable. Doble gate:
      1. EMAIL_SENDING_ENABLED (en email_render.send_email) — bloquea el envío real.
      2. `dry_run` (default True) — ni siquiera intenta; solo simula y cuenta.

    Solo contacta destinatarios con DOBLE opt-in de email confirmado y fuera de
    cooldown. Registra cada envío para tracking. Nunca lanza: devuelve un resumen.
    """
    from .email_segments import resolve_recipients
    from .email_render import render_email, render_subject, sending_status
    from . import email_optin

    seg_id = segment.get("id") or ""
    seg_name = segment.get("name") or seg_id
    recipients = resolve_recipients(segment, limit=limit)  # solo confirmados + con email
    st = sending_status()

    summary = {
        "segment": seg_name, "dry_run": dry_run,
        "candidates": len(recipients),
        "sent": 0, "skipped_cooldown": 0, "gated": 0, "errors": 0,
        "sending_enabled": st["enabled"], "sending_reasons": st["reasons"],
    }

    for r in recipients:
        phone = r.get("telefono") or r.get("phone") or ""
        email = (r.get("email") or "").strip().lower()
        if not email:
            continue
        if not can_send(phone, cooldown_days):
            summary["skipped_cooldown"] += 1
            continue

        optin = email_optin.get_email_optin(phone) or {}
        unsub = unsubscribe_url(optin.get("token") or "")
        ctx = {"nombre": (r.get("nombre") or "").split(" ")[0],
               "especialidad": r.get("especialidad") or "",
               "comuna": r.get("comuna") or ""}
        subject = render_subject(segment, ctx)
        cta_url = wa_attribution_url(seg_id)
        seg_for_render = {**segment, "email": {**(segment.get("email") or {}), "cta_url": cta_url}}
        html_body = render_email(seg_for_render, ctx, unsubscribe_url=unsub)

        token = record_queued(phone, email, segment_id=seg_id, segment_name=seg_name,
                              subject=subject, cta_url=cta_url)
        tracked = inject_tracking(html_body, token, cta_url)

        if dry_run:
            mark_gated(token, "dry_run")
            summary["gated"] += 1
            continue

        from .email_render import send_email
        res = await send_email(email, subject, tracked, unsubscribe_url=unsub)
        if res.get("status") == "sent":
            mark_sent(token, res.get("id"))
            summary["sent"] += 1
        elif res.get("status") == "gated":
            mark_gated(token, "; ".join(res.get("reasons") or []))
            summary["gated"] += 1
        else:
            mark_error(token, res.get("error") or "?")
            summary["errors"] += 1

    return summary
