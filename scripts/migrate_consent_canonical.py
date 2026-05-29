#!/usr/bin/env python3
"""Migración one-shot: canonicalizar bi.marketing_consent + reconstruir consents.

CONTEXTO (root cause 2026-05-29)
--------------------------------
La PK `bi.marketing_consent.phone` acumuló el mismo número en >=4 formatos
distintos ('+56...', '56...', '977281627', '9 6100 6968') porque los bordes que
escribían no canonicalizaban parejo. Encima, `bi_conn()` no hacía rollback antes
de devolver la conexión al pool: una query fallida dejaba la conexión en estado
"transaction aborted" y el siguiente INSERT de `registrar_consent_respuesta`
fallaba silenciosamente (el except lo tragaba). Resultado: 0 filas accepted en
marketing_consent pese a 279 respuestas reales de pacientes.

La VERDAD durable de quién aceptó/declinó NO se perdió: vive en
`sessions.db.conversation_events` (event='marketing_consent_respuesta', con
meta.status = accepted|declined y el timestamp real de la respuesta).

QUÉ HACE
--------
1. Lee la verdad desde conversation_events (último estado por teléfono canónico).
2. Agrupa todas las filas de marketing_consent por teléfono canónico
   (session.normalize_wa_id → 56XXXXXXXXX).
3. Para cada grupo: colapsa las filas duplicadas en UNA sola fila canónica,
   tomando el estado real desde los eventos cuando existe (accepted/declined +
   response_at = ts del evento). Si no hay evento, conserva el mejor estado
   existente. Preserva columnas extra de la fila más completa.
4. Reconstruye bi.opt_outs_marketing para los que declinaron y canonicaliza las
   filas existentes de opt_outs.

SEGURIDAD
---------
- Dry-run POR DEFECTO: imprime el plan, no escribe nada.
- `--apply` ejecuta dentro de UNA transacción (todo o nada).
- Idempotente: correr dos veces deja el mismo resultado.
- Solo lectura sobre sessions.db.

USO (en el VPS, donde viven BI Postgres + sessions.db)
------------------------------------------------------
    cd /opt/chatbot-cmc
    ./venv/bin/python scripts/migrate_consent_canonical.py            # dry-run
    ./venv/bin/python scripts/migrate_consent_canonical.py --apply    # ejecutar
"""
import argparse
import importlib.util
import os
import sys
from collections import defaultdict

import psycopg2

_DEFAULT_APP = "/opt/chatbot-cmc/app"
_COMPUTED_APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")


def _load_normalizer(app_dir: str):
    """Importa normalize_wa_id desde app/session.py por path explícito (no depende
    del cwd ni de sys.path). Garantiza usar EL MISMO canónico que producción."""
    sess_path = os.path.join(app_dir, "session.py")
    spec = importlib.util.spec_from_file_location("_canon_session", sess_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.normalize_wa_id


normalize_wa_id = None  # se setea en main() según --app-dir


def _load_env(path: str) -> dict:
    d = {}
    if not os.path.exists(path):
        return d
    for ln in open(path):
        ln = ln.strip()
        if ln and not ln.startswith("#") and "=" in ln:
            k, v = ln.split("=", 1)
            d[k] = v.strip().strip('"').strip("'")
    return d


def _canon(phone: str) -> str | None:
    """Teléfono canónico para BI: 56XXXXXXXXX. None si no parseable a >=9 dígitos."""
    if not phone:
        return None
    c = normalize_wa_id(str(phone))
    digits = "".join(ch for ch in c if ch.isdigit())
    if len(digits) < 9:
        return None
    return c


def _open_sessions_db(env: dict, db_path: str):
    key = env.get("SQLCIPHER_KEY", "").strip()
    try:
        from sqlcipher3 import dbapi2 as sc
        conn = sc.connect(db_path)
        if key:
            conn.execute('PRAGMA key="x\'%s\'";' % key)
        # validar que la key abre la DB
        conn.execute("SELECT count(*) FROM sqlite_master")
        return conn
    except Exception:
        import sqlite3
        return sqlite3.connect(db_path)


def load_truth(env: dict, db_path: str) -> dict:
    """Devuelve {canon_phone: {'status':.., 'ts':..}} con el ÚLTIMO estado real
    por teléfono canónico, leído de conversation_events."""
    conn = _open_sessions_db(env, db_path)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT phone, json_extract(meta,'$.status') AS status, ts
        FROM conversation_events
        WHERE event='marketing_consent_respuesta'
          AND json_extract(meta,'$.status') IN ('accepted','declined')
        ORDER BY ts ASC
        """
    )
    truth: dict[str, dict] = {}
    raw_count = 0
    for phone, status, ts in cur.fetchall():
        raw_count += 1
        canon = _canon(phone)
        if not canon:
            continue
        # ORDER BY ts ASC → el último que pisa gana = estado más reciente
        truth[canon] = {"status": status, "ts": ts}
    conn.close()
    print(f"  eventos de respuesta leídos: {raw_count} → {len(truth)} teléfonos canónicos con estado")
    return truth


# Texto EXACTO de los quick-reply del template consent_marketing_v1. Recuperamos
# respuestas que el bot NO logueó como evento (bug del 28-may: el guard de lectura
# comparaba teléfono entrante sin '+' vs fila '+56...' → la rama de consent se
# saltaba y el "Sí, acepto"/"No, gracias" se perdía sin generar evento).
_ACC_BTN = ("sí, acepto", "si, acepto", "sí acepto", "si acepto")
_DEC_BTN = ("no, gracias", "no gracias")


def load_msg_recovery(env: dict, db_path: str, exclude_canon: set) -> dict:
    """Recupera consent desde el log crudo de mensajes (messages) para teléfonos
    que respondieron textualmente el botón pero NO tienen evento. Devuelve
    {canon: {'status':.., 'ts':.., 'source':'message_log'}} con el ÚLTIMO botón.
    El gate marketing-vs-dental se aplica en main() (acá no hay PG)."""
    conn = _open_sessions_db(env, db_path)
    cur = conn.cursor()
    cur.execute(
        "SELECT phone, text, ts FROM messages "
        "WHERE direction='in' AND ts >= '2026-05-13' ORDER BY ts ASC"
    )
    rec: dict[str, dict] = {}
    for phone, text, ts in cur.fetchall():
        canon = _canon(phone)
        if not canon or canon in exclude_canon:
            continue
        t = (text or "").strip().lower()
        if t in _ACC_BTN:
            rec[canon] = {"status": "accepted", "ts": ts, "source": "message_log"}
        elif t in _DEC_BTN:
            rec[canon] = {"status": "declined", "ts": ts, "source": "message_log"}
    conn.close()
    print(f"  candidatos a recuperar desde messages (sin evento): {len(rec)}")
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="ejecutar (default: dry-run)")
    ap.add_argument("--env", default="/opt/chatbot-cmc/.env")
    ap.add_argument("--app-dir", default=None,
                    help="dir con session.py (default: junto al repo o /opt/chatbot-cmc/app)")
    ap.add_argument("--sessions-db", default=None,
                    help="ruta a sessions.db (default: <app>/../data/sessions.db)")
    args = ap.parse_args()

    app_dir = args.app_dir or (_COMPUTED_APP if os.path.exists(os.path.join(_COMPUTED_APP, "session.py")) else _DEFAULT_APP)
    sessions_db = args.sessions_db or os.path.join(os.path.dirname(app_dir), "data", "sessions.db")

    global normalize_wa_id
    normalize_wa_id = _load_normalizer(app_dir)

    env = _load_env(args.env)
    print("=" * 70)
    print("MIGRACIÓN consent canónico  —  modo:", "APPLY" if args.apply else "DRY-RUN")
    print(f"  app_dir={app_dir}  sessions_db={sessions_db}")
    print("=" * 70)

    print("\n[1/4] Cargando verdad desde sessions.db conversation_events…")
    truth = load_truth(env, sessions_db)

    pg = psycopg2.connect(
        host=env.get("BI_DB_HOST", "127.0.0.1"),
        port=env.get("BI_DB_PORT", "5432"),
        dbname=env.get("BI_DB_NAME", "health_bi"),
        user=env.get("BI_DB_USER", "health_user"),
        password=env.get("BI_DB_PASSWORD", "password123"),
        connect_timeout=10,
    )
    pg.autocommit = False
    cur = pg.cursor()

    # Columnas reales de la tabla (para preservar columnas extra)
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='bi' AND table_name='marketing_consent' "
        "ORDER BY ordinal_position"
    )
    cols = [r[0] for r in cur.fetchall()]
    print(f"\n[2/4] marketing_consent columnas: {cols}")

    cur.execute("SELECT %s FROM bi.marketing_consent" % ", ".join(cols))
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    print(f"  filas actuales: {len(rows)}")

    groups: dict[str, list[dict]] = defaultdict(list)
    unparseable = 0
    for r in rows:
        canon = _canon(r["phone"])
        if not canon:
            unparseable += 1
            continue
        groups[canon].append(r)
    print(f"  grupos canónicos: {len(groups)}  (filas sin parsear: {unparseable})")

    # ── Recuperar respuestas perdidas desde el log de mensajes (bug 28-may) ──
    # Gate de seguridad: solo recuperar si el teléfono tiene fila en
    # marketing_consent y NO tiene un dental_consent pending (evita cruzar el
    # "Sí, acepto" del consent dental con el de marketing).
    dental_pending: set[str] = set()
    try:
        cur.execute("SELECT phone FROM bi.dental_consent WHERE status='pending'")
        for (ph,) in cur.fetchall():
            c = _canon(ph)
            if c:
                dental_pending.add(c)
    except Exception as e:
        print(f"  (sin dental_consent o error: {e})")
    msg_rec = load_msg_recovery(env, sessions_db, exclude_canon=set(truth.keys()))
    rec_aplicados = rec_skip_sin_fila = rec_skip_dental = 0
    for canon, info in msg_rec.items():
        if canon not in groups:
            rec_skip_sin_fila += 1
            continue
        if canon in dental_pending:
            rec_skip_dental += 1
            print(f"    ⚠ skip ambiguo (dental+marketing): ...{canon[-4:]} dijo {info['status']}")
            continue
        truth[canon] = info  # tratado como verdad (mismo peso que un evento)
        rec_aplicados += 1
    print(f"  recuperados del log: {rec_aplicados}  "
          f"(skip sin fila marketing: {rec_skip_sin_fila}, skip ambiguo dental: {rec_skip_dental})")

    STATUS_RANK = {"accepted": 3, "declined": 3, "no_response": 2, "pending": 1}

    plan = []  # (canon, final_status, n_filas_colapsadas, tiene_evento)
    declined_canon = set()
    for canon, grp in groups.items():
        t = truth.get(canon)
        if t:
            final_status = t["status"]
            response_at = t["ts"]
            response_method = t.get("source") or "reply"
        else:
            best = max(grp, key=lambda x: STATUS_RANK.get(x.get("status"), 0))
            final_status = best.get("status") or "pending"
            response_at = max((g.get("response_at") for g in grp if g.get("response_at")), default=None)
            response_method = next((g.get("response_method") for g in grp if g.get("response_method")), None)
        if final_status == "declined":
            declined_canon.add(canon)
        plan.append((canon, grp, final_status, response_at, response_method, bool(t)))

    # Teléfonos que aceptaron/declinaron por evento pero NO tienen NINGUNA fila
    # en marketing_consent (su INSERT se perdió por la conexión envenenada).
    existing_canon = set(groups.keys())
    huerfanos = [(c, v) for c, v in truth.items() if c not in existing_canon]
    print(f"  teléfonos con evento pero SIN fila en consent (a crear): {len(huerfanos)}")

    # ── Resumen ──
    from collections import Counter
    final_counter = Counter(p[2] for p in plan)
    final_counter.update(v["status"] for _, v in huerfanos)
    print("\n[3/4] Estado final proyectado de marketing_consent:")
    for st, n in sorted(final_counter.items()):
        print(f"    {st:<12} {n}")
    print(f"    filas a eliminar (duplicados de formato): "
          f"{sum(len(p[1]) for p in plan) - len(plan)}")
    print(f"    opt-outs a reconstruir (declined): {len(declined_canon)}")

    if not args.apply:
        print("\n[4/4] DRY-RUN — no se escribió nada. Re-corré con --apply para ejecutar.")
        # muestra 8 ejemplos de colapso
        ejemplos = [p for p in plan if len(p[1]) > 1 or p[5]][:8]
        if ejemplos:
            print("\n  Ejemplos de cambios:")
            for canon, grp, fs, rat, rm, tev in ejemplos:
                formatos = [g["phone"] for g in grp]
                print(f"    {formatos} → {canon}  status={fs}  (evento={tev})")
        pg.rollback()
        pg.close()
        return

    print("\n[4/4] APPLY — ejecutando en una transacción…")
    try:
        col_list = ", ".join(cols)
        placeholders = ", ".join(["%s"] * len(cols))
        for canon, grp, final_status, response_at, response_method, tev in plan:
            base = max(grp, key=lambda x: STATUS_RANK.get(x.get("status"), 0)).copy()
            base["phone"] = canon
            base["status"] = final_status
            if "response_at" in base:
                base["response_at"] = response_at
            if "response_method" in base:
                base["response_method"] = response_method
            if "consent_sent_at" in base and not base.get("consent_sent_at"):
                base["consent_sent_at"] = response_at
            # borrar todas las variantes de formato del grupo
            cur.execute(
                "DELETE FROM bi.marketing_consent WHERE phone = ANY(%s)",
                ([g["phone"] for g in grp],),
            )
            cur.execute(
                f"INSERT INTO bi.marketing_consent ({col_list}) VALUES ({placeholders})",
                [base.get(c) for c in cols],
            )
        # huérfanos: crear fila canónica desde el evento
        for canon, v in huerfanos:
            base = {c: None for c in cols}
            base["phone"] = canon
            base["status"] = v["status"]
            if "response_at" in base:
                base["response_at"] = v["ts"]
            if "response_method" in base:
                base["response_method"] = "reply"
            if "consent_sent_at" in base:
                base["consent_sent_at"] = v["ts"]
            cur.execute(
                f"INSERT INTO bi.marketing_consent ({col_list}) VALUES ({placeholders})",
                [base.get(c) for c in cols],
            )
            if v["status"] == "declined":
                declined_canon.add(canon)

        # opt_outs_marketing: canonicalizar existentes + reconstruir declined
        cur.execute("SELECT phone FROM bi.opt_outs_marketing")
        opt_rows = [r[0] for r in cur.fetchall()]
        for ph in opt_rows:
            canon = _canon(ph)
            if canon and canon != ph:
                cur.execute("DELETE FROM bi.opt_outs_marketing WHERE phone=%s", (ph,))
                cur.execute(
                    "INSERT INTO bi.opt_outs_marketing (phone, source, reason) "
                    "VALUES (%s,%s,%s) ON CONFLICT (phone) DO NOTHING",
                    (canon, "migracion_canonica", "canonicalizado"),
                )
        for canon in declined_canon:
            cur.execute(
                "INSERT INTO bi.opt_outs_marketing (phone, source, reason) "
                "VALUES (%s,%s,%s) ON CONFLICT (phone) DO NOTHING",
                (canon, "consent_marketing_v1", "declined_marketing"),
            )

        pg.commit()
        print("  ✓ COMMIT ok.")
    except Exception as e:
        pg.rollback()
        print(f"  ✗ ROLLBACK — error: {e}")
        raise
    finally:
        cur.execute(
            "SELECT status, COUNT(*) FROM bi.marketing_consent GROUP BY 1 ORDER BY 1"
        )
        print("  Estado final real:", cur.fetchall())
        pg.close()


if __name__ == "__main__":
    main()
