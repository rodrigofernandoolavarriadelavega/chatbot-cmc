"""Almacén compartido de hallazgos de auditoría (tabla `audit_findings`).

Fuente de verdad única usada por:
  • scripts/audit_runner.py          (checks deterministas: precio, consent, agenda, leak, finanzas)
  • scripts/conversation_audit_swarm.py (check de conversación con Claude)
  • app/audit_routes.py              (vista /admin/auditoria)

Una sola tabla, una sola vista, una sola compuerta humana. Agregar un check
nuevo = escribir una función que devuelva findings y llamar store_findings().
"""
from __future__ import annotations

import hashlib

# Import dual: dentro del bot (app/ en path) o desde un script (ROOT en path).
try:
    from session import _conn
except ImportError:  # pragma: no cover
    from app.session import _conn


def ensure_table() -> None:
    con = _conn()
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS audit_findings (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                run_ts        TEXT DEFAULT (datetime('now')),
                check_name    TEXT DEFAULT 'conversacion',
                phone         TEXT,
                severity      TEXT,
                category      TEXT,
                issue         TEXT,
                evidence      TEXT,
                fix_type      TEXT,
                suggested_fix TEXT,
                target_hint   TEXT,
                dedup_key     TEXT,
                status        TEXT DEFAULT 'open'
            )
        """)
        # Migraciones idempotentes para tablas creadas por la versión vieja
        # del enjambre (sin check_name / dedup_key).
        cols = [r[1] for r in con.execute("PRAGMA table_info(audit_findings)").fetchall()]
        if "check_name" not in cols:
            con.execute("ALTER TABLE audit_findings ADD COLUMN check_name TEXT DEFAULT 'conversacion'")
        if "dedup_key" not in cols:
            con.execute("ALTER TABLE audit_findings ADD COLUMN dedup_key TEXT")
        # dedup_key único (NULLs múltiples permitidos en SQLite) → re-correr un
        # check no duplica el mismo hallazgo abierto.
        con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_audit_dedup "
                    "ON audit_findings(dedup_key) WHERE dedup_key IS NOT NULL")
        con.execute("CREATE INDEX IF NOT EXISTS idx_audit_run ON audit_findings(run_ts)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_audit_check ON audit_findings(check_name, status)")
        con.commit()
    finally:
        con.close()


def _dedup_key(check_name: str, f: dict) -> str:
    if f.get("dedup_key"):
        return f"{check_name}|{f['dedup_key']}"
    base = f"{check_name}|{f.get('phone', '')}|{(f.get('issue') or '')[:120]}"
    return hashlib.sha1(base.encode("utf-8")).hexdigest()[:20]


def store_findings(check_name: str, findings: list[dict], dry_run: bool = False) -> int:
    """Inserta findings nuevos (INSERT OR IGNORE por dedup_key). Devuelve cuántos
    se insertaron realmente (los repetidos se ignoran)."""
    if dry_run or not findings:
        return 0
    ensure_table()
    con = _conn()
    inserted = 0
    try:
        for f in findings:
            dk = _dedup_key(check_name, f)
            cur = con.execute(
                """INSERT OR IGNORE INTO audit_findings
                   (check_name, phone, severity, category, issue, evidence,
                    fix_type, suggested_fix, target_hint, dedup_key)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (check_name, f.get("phone"), f.get("severity"), f.get("category"),
                 f.get("issue"), f.get("evidence"), f.get("fix_type"),
                 f.get("suggested_fix"), f.get("target_hint"), dk),
            )
            inserted += cur.rowcount
        con.commit()
    finally:
        con.close()
    return inserted


def fetch(limit: int = 300, status: str | None = "open", check: str | None = None) -> list[dict]:
    ensure_table()
    con = _conn()
    try:
        q = ("SELECT id, run_ts, check_name, phone, severity, category, issue, "
             "evidence, fix_type, suggested_fix, target_hint, status FROM audit_findings")
        conds, args = [], []
        if status and status != "all":
            conds.append("status = ?"); args.append(status)
        if check and check != "all":
            conds.append("check_name = ?"); args.append(check)
        if conds:
            q += " WHERE " + " AND ".join(conds)
        q += " ORDER BY run_ts DESC, id DESC LIMIT ?"
        args.append(limit)
        return [dict(r) for r in con.execute(q, args).fetchall()]
    finally:
        con.close()


def counts_by_check(status: str = "open") -> dict[str, int]:
    ensure_table()
    con = _conn()
    try:
        rows = con.execute(
            "SELECT check_name, COUNT(*) FROM audit_findings WHERE status = ? GROUP BY check_name",
            (status,),
        ).fetchall()
        return {r[0]: r[1] for r in rows}
    finally:
        con.close()
