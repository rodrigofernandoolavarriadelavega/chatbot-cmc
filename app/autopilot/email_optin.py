"""Doble opt-in de canal email (Ley 21.719 — dato sensible de salud).

Capa de consentimiento **específica del canal email**, encima del consent general
de marketing (`privacy_consents`). Un paciente puede tener consent general de
marketing y aún NO haber confirmado el canal email: este módulo modela ese segundo
paso, verificable por un clic en un correo de confirmación. Solo el estado
`confirmed` habilita envíos de marketing por email.

Ciclo de vida:
  requested  → el bot preguntó y el paciente dijo "sí" (single opt-in en WhatsApp)
  pending    → se envió el correo de confirmación, esperando el clic
  confirmed  → el paciente hizo clic en el enlace (DOBLE opt-in COMPLETO)
  declined   → dijo "no" en el bot
  revoked    → se dio de baja (clic en /email/baja o escribió 'stop')

Diseño:
- Tabla propia `email_channel_consent` en sessions.db. NO toca `privacy_consents`.
- Tokens opacos (`secrets.token_urlsafe`) para los enlaces de confirmación y baja.
- Idempotente: re-pedir opt-in a un confirmado no lo degrada; re-confirmar es no-op.
"""
from __future__ import annotations

import logging
import secrets

log = logging.getLogger("bot")

# Estados terminales que NO se deben pisar al re-pedir opt-in.
_LOCKED = ("confirmed", "revoked")


def _conn():
    from session import _conn as _c
    return _c()


def _norm_phone(phone: str) -> str:
    """569XXXXXXXX canónico (mismo formato que get_email_consent_sets)."""
    try:
        from session import _normalize_phone_e164
        return _normalize_phone_e164(phone or "") or ""
    except Exception:  # noqa: BLE001
        return (phone or "").lstrip("+")


def _ensure_table(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS email_channel_consent (
            phone        TEXT PRIMARY KEY,
            email        TEXT,
            status       TEXT NOT NULL DEFAULT 'requested',
            token        TEXT UNIQUE,
            source       TEXT DEFAULT 'bot',
            requested_at TEXT DEFAULT (datetime('now')),
            confirmed_at TEXT,
            revoked_at   TEXT,
            updated_at   TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ecc_token  ON email_channel_consent(token)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ecc_status ON email_channel_consent(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ecc_email  ON email_channel_consent(email)")


def _new_token() -> str:
    return secrets.token_urlsafe(24)


# ─────────────────────────────────────────────────────────────────────────────
# Escritura
# ─────────────────────────────────────────────────────────────────────────────
def request_email_optin(phone: str, email: str, source: str = "bot") -> str | None:
    """Registra el "sí" del paciente en WhatsApp (single opt-in) y devuelve el token
    del enlace de confirmación. Si ya estaba confirmado o revocado, NO lo degrada:
    devuelve el token existente (confirmado) o None (revocado, respetamos la baja).
    """
    phone = _norm_phone(phone)
    email = (email or "").strip().lower()
    if not phone or not email:
        return None
    with _conn() as conn:
        _ensure_table(conn)
        row = conn.execute(
            "SELECT status, token FROM email_channel_consent WHERE phone=?", (phone,)
        ).fetchone()
        if row and row["status"] == "confirmed":
            return row["token"]          # ya confirmado: no re-pedir
        if row and row["status"] == "revoked":
            return None                  # respetar la baja: requiere acción explícita
        token = (row["token"] if row and row["token"] else _new_token())
        conn.execute("""
            INSERT INTO email_channel_consent (phone, email, status, token, source, requested_at, updated_at)
            VALUES (?, ?, 'requested', ?, ?, datetime('now'), datetime('now'))
            ON CONFLICT(phone) DO UPDATE SET
                email=excluded.email, status='requested', token=excluded.token,
                source=excluded.source, updated_at=datetime('now')
        """, (phone, email, token, source))
        conn.commit()
        return token


def mark_optin_pending(phone: str) -> None:
    """Marca que ya se envió el correo de confirmación (requested → pending)."""
    phone = _norm_phone(phone)
    with _conn() as conn:
        _ensure_table(conn)
        conn.execute(
            "UPDATE email_channel_consent SET status='pending', updated_at=datetime('now') "
            "WHERE phone=? AND status='requested'", (phone,))
        conn.commit()


def confirm_email_optin(token: str) -> dict | None:
    """Confirma el doble opt-in por clic en el enlace. Idempotente.
    Devuelve el registro {phone, email} o None si el token no existe / fue revocado.
    """
    if not token:
        return None
    with _conn() as conn:
        _ensure_table(conn)
        row = conn.execute(
            "SELECT phone, email, status FROM email_channel_consent WHERE token=?", (token,)
        ).fetchone()
        if not row:
            return None
        if row["status"] == "revoked":
            return None
        conn.execute(
            "UPDATE email_channel_consent SET status='confirmed', "
            "confirmed_at=COALESCE(confirmed_at, datetime('now')), updated_at=datetime('now') "
            "WHERE token=?", (token,))
        conn.commit()
        return {"phone": row["phone"], "email": row["email"]}


def decline_email_optin(phone: str) -> None:
    """El paciente dijo 'no' al canal email en WhatsApp."""
    phone = _norm_phone(phone)
    with _conn() as conn:
        _ensure_table(conn)
        conn.execute("""
            INSERT INTO email_channel_consent (phone, status, updated_at)
            VALUES (?, 'declined', datetime('now'))
            ON CONFLICT(phone) DO UPDATE SET status='declined', updated_at=datetime('now')
            WHERE email_channel_consent.status NOT IN ('confirmed','revoked')
        """, (phone,))
        conn.commit()


def revoke_email_optin(*, token: str | None = None, phone: str | None = None) -> dict | None:
    """Baja del canal email (one-click /email/baja o 'stop'). Devuelve {phone,email}."""
    with _conn() as conn:
        _ensure_table(conn)
        if token:
            row = conn.execute(
                "SELECT phone, email FROM email_channel_consent WHERE token=?", (token,)
            ).fetchone()
        elif phone:
            ph = _norm_phone(phone)
            row = conn.execute(
                "SELECT phone, email FROM email_channel_consent WHERE phone=?", (ph,)
            ).fetchone()
        else:
            return None
        if not row:
            # Baja de alguien que nunca pasó por opt-in: registra revoked por las dudas.
            if phone:
                ph = _norm_phone(phone)
                conn.execute(
                    "INSERT OR IGNORE INTO email_channel_consent (phone, status, revoked_at, updated_at) "
                    "VALUES (?, 'revoked', datetime('now'), datetime('now'))", (ph,))
                conn.commit()
            return None
        conn.execute(
            "UPDATE email_channel_consent SET status='revoked', "
            "revoked_at=datetime('now'), updated_at=datetime('now') WHERE phone=?",
            (row["phone"],))
        conn.commit()
        return {"phone": row["phone"], "email": row["email"]}


# ─────────────────────────────────────────────────────────────────────────────
# Lectura / gating
# ─────────────────────────────────────────────────────────────────────────────
def get_email_optin(phone: str) -> dict | None:
    phone = _norm_phone(phone)
    if not phone:
        return None
    with _conn() as conn:
        _ensure_table(conn)
        row = conn.execute(
            "SELECT * FROM email_channel_consent WHERE phone=?", (phone,)
        ).fetchone()
        return dict(row) if row else None


def get_confirmed_email_sets() -> dict:
    """Sets para gating de envío: solo 'confirmed' habilita; 'revoked' suprime.

    Devuelve:
      confirmed_phones: set(569XXXXXXXX)  — doble opt-in completo
      confirmed_emails: set(lower)        — para suprimir por email también
      revoked_phones:   set(569XXXXXXXX)  — baja explícita (supresión dura)
    """
    confirmed_phones, confirmed_emails, revoked_phones = set(), set(), set()
    with _conn() as conn:
        _ensure_table(conn)
        for r in conn.execute(
            "SELECT phone, email, status FROM email_channel_consent"
        ).fetchall():
            ph = (r["phone"] or "").strip()
            em = (r["email"] or "").strip().lower()
            if r["status"] == "confirmed":
                if ph:
                    confirmed_phones.add(ph)
                if em:
                    confirmed_emails.add(em)
            elif r["status"] == "revoked":
                if ph:
                    revoked_phones.add(ph)
    return {
        "confirmed_phones": confirmed_phones,
        "confirmed_emails": confirmed_emails,
        "revoked_phones": revoked_phones,
    }


def optin_stats() -> dict:
    """Conteos por estado para el dashboard (embudo de opt-in)."""
    out = {"requested": 0, "pending": 0, "confirmed": 0, "declined": 0, "revoked": 0, "total": 0}
    with _conn() as conn:
        _ensure_table(conn)
        for r in conn.execute(
            "SELECT status, COUNT(*) AS n FROM email_channel_consent GROUP BY status"
        ).fetchall():
            st = r["status"] or "?"
            out[st] = r["n"]
            out["total"] += r["n"]
    return out
