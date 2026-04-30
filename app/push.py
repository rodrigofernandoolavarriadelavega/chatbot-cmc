"""Web Push notifications module — admin v2 PWA.

Almacena suscripciones por dispositivo y dispara push broadcast a todas las
suscripciones de un rol cuando ocurre un evento (mensaje paciente entrante).

Uso:
    from push import save_subscription, send_to_role
    save_subscription(sub_dict, role='admin', label='Recepción 1')
    send_to_role('admin', title='Marta C.', body='Hola, necesito hora', url='/admin/v2?phone=569123', badge=5)
"""
import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Iterable

from pywebpush import webpush, WebPushException

log = logging.getLogger("bot.push")

VAPID_PUBLIC_KEY = os.getenv("VAPID_PUBLIC_KEY", "")
VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY", "")
VAPID_SUBJECT = os.getenv("VAPID_SUBJECT", "mailto:contacto@centromedicocarampangue.cl")

DB_PATH = Path(__file__).parent.parent / "data" / "sessions.db"

_DB_INIT_DONE = False


def _conn():
    """Reusa session._conn: maneja SQLCipher si está activo en producción.
    Antes este módulo abría sessions.db con sqlite3 plano y fallaba con
    'file is not a database' en cada call después de activar SQLCipher en
    el VPS, bloqueando push notifications al admin (visto 2026-04-30)."""
    from session import _conn as _session_conn
    return _session_conn()


def init_db():
    """Crea tabla push_subscriptions si no existe (idempotente, lazy)."""
    global _DB_INIT_DONE
    if _DB_INIT_DONE:
        return
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS push_subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                endpoint TEXT NOT NULL UNIQUE,
                p256dh TEXT NOT NULL,
                auth TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'admin',
                label TEXT,
                user_agent TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                last_used_at TEXT,
                last_error TEXT
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_push_role ON push_subscriptions(role)")
        c.commit()
    _DB_INIT_DONE = True


def _ensure_db():
    if not _DB_INIT_DONE:
        init_db()


def save_subscription(sub: dict, role: str = "admin", label: str = "", user_agent: str = "") -> int:
    """Guarda (o reemplaza) una suscripción. Retorna el id."""
    _ensure_db()
    endpoint = sub.get("endpoint")
    keys = sub.get("keys", {})
    p256dh = keys.get("p256dh")
    auth = keys.get("auth")
    if not (endpoint and p256dh and auth):
        raise ValueError("subscription incompleta: faltan endpoint/keys")

    with _conn() as c:
        cur = c.execute("""
            INSERT INTO push_subscriptions (endpoint, p256dh, auth, role, label, user_agent)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(endpoint) DO UPDATE SET
                p256dh=excluded.p256dh,
                auth=excluded.auth,
                role=excluded.role,
                label=excluded.label,
                user_agent=excluded.user_agent,
                last_error=NULL
        """, (endpoint, p256dh, auth, role, label, user_agent))
        c.commit()
        row = c.execute("SELECT id FROM push_subscriptions WHERE endpoint=?", (endpoint,)).fetchone()
        return int(row["id"]) if row else cur.lastrowid


def delete_subscription(endpoint: str) -> bool:
    with _conn() as c:
        cur = c.execute("DELETE FROM push_subscriptions WHERE endpoint=?", (endpoint,))
        c.commit()
        return cur.rowcount > 0


def list_subscriptions(role: str | None = None) -> list[dict]:
    _ensure_db()
    with _conn() as c:
        if role:
            rows = c.execute("SELECT * FROM push_subscriptions WHERE role=?", (role,)).fetchall()
        else:
            rows = c.execute("SELECT * FROM push_subscriptions").fetchall()
        return [dict(r) for r in rows]


def _to_subscription_info(row: dict) -> dict:
    return {
        "endpoint": row["endpoint"],
        "keys": {"p256dh": row["p256dh"], "auth": row["auth"]},
    }


def send_to_role(role: str, title: str, body: str = "",
                 url: str = "/admin/v2", badge: int | None = None,
                 tag: str = "cmc-msg") -> dict:
    """Envía push a todas las suscripciones del rol. Retorna stats {sent, failed, removed}."""
    if not (VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY):
        log.warning("VAPID keys no configuradas, push deshabilitado")
        return {"sent": 0, "failed": 0, "removed": 0, "error": "vapid_missing"}

    payload = json.dumps({
        "title": title,
        "body": body,
        "url": url,
        "badge": badge,
        "tag": tag,
        "ts": int(time.time()),
    })

    subs = list_subscriptions(role=role)
    sent = failed = removed = 0
    for row in subs:
        try:
            webpush(
                subscription_info=_to_subscription_info(row),
                data=payload,
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims={"sub": VAPID_SUBJECT},
                ttl=300,
            )
            sent += 1
            with _conn() as c:
                c.execute("UPDATE push_subscriptions SET last_used_at=datetime('now'), last_error=NULL WHERE id=?", (row["id"],))
                c.commit()
        except WebPushException as e:
            status = getattr(e.response, "status_code", 0) if getattr(e, "response", None) else 0
            # 404/410 => suscripción expirada o removida por el usuario
            if status in (404, 410):
                delete_subscription(row["endpoint"])
                removed += 1
                log.info("push: suscripción removida (status=%s) id=%s", status, row["id"])
            else:
                failed += 1
                log.warning("push: fallo enviando id=%s status=%s err=%s", row["id"], status, str(e)[:200])
                with _conn() as c:
                    c.execute("UPDATE push_subscriptions SET last_error=? WHERE id=?", (str(e)[:500], row["id"]))
                    c.commit()
        except Exception as e:
            failed += 1
            log.exception("push: error inesperado id=%s: %s", row["id"], e)

    return {"sent": sent, "failed": failed, "removed": removed, "total": len(subs)}


def count_unread_conversations() -> int:
    """Cuenta conversaciones distintas con mensajes entrantes no vistos por admin.

    Misma lógica que get_unread_counts(): inbound posteriores a admin_seen.seen_at
    (o todos los inbound si nunca se marcó).
    """
    try:
        with _conn() as c:
            row = c.execute("""
                SELECT COUNT(DISTINCT m.phone) AS n
                FROM messages m
                LEFT JOIN admin_seen a ON a.phone = m.phone
                WHERE m.direction='in'
                  AND (a.seen_at IS NULL OR m.ts > a.seen_at)
            """).fetchone()
            return int(row["n"] or 0) if row else 0
    except Exception as e:
        log.warning("count_unread_conversations failed: %s", e)
        return 0
