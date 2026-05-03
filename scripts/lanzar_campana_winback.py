"""
Lanza campaña win-back para una cohorte específica.

Lee pacientes desde bi.v_winback_cohortes (Postgres del BI), excluye opt-outs,
manda template Meta aprobado, registra en bi.winback_envios.

Uso:
    PYTHONPATH=app:. python3 scripts/lanzar_campana_winback.py \\
        --cohorte 090d --template winback_mg_v1 --limit 50 --dry-run

    # Real (sin --dry-run) — cuando Meta haya aprobado el template:
    PYTHONPATH=app:. python3 scripts/lanzar_campana_winback.py \\
        --cohorte 090d --template winback_mg_v1 --limit 50

Flags:
    --cohorte    030d / 060d / 090d / 180d / 365d (requerido)
    --template   nombre del template Meta APPROVED (requerido)
    --limit      máximo de envíos en este batch (default 50)
    --dry-run    no envía nada, solo lista
    --especialidad   filtra por última especialidad atendida (opcional)
    --comuna     filtra por comuna del paciente (opcional)
    --rate       segundos entre envíos (default 2)

Reglas duras:
- NO enviar a phones con marketing_opt_out (tag en sessions.db del bot)
- NO enviar a phones con HUMAN_TAKEOVER actual
- NO repetir envío al mismo phone en últimos 90 días (bi.winback_envios)
- Solo phones formato CL +56 9 XXXXXXXX
- Logea cada envío exitoso en bi.winback_envios
"""
import argparse
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

import httpx
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

CHATBOT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(CHATBOT_ROOT / ".env")

# Meta API
META_TOKEN = os.getenv("META_ACCESS_TOKEN", "")
META_PHONE_ID = os.getenv("META_PHONE_NUMBER_ID", "")
META_API = f"https://graph.facebook.com/v22.0/{META_PHONE_ID}/messages"

# Postgres BI (Docker local del Mac por ahora)
PG_HOST = os.getenv("BI_PG_HOST", "localhost")
PG_PORT = int(os.getenv("BI_PG_PORT", "5432"))
PG_DB = os.getenv("BI_PG_DB", "health_bi")
PG_USER = os.getenv("BI_PG_USER", "health_user")
PG_PASS = os.getenv("BI_PG_PASS", "password123")

# SQLite del bot (para leer marketing_opt_out + estados activos)
SESSIONS_DB = CHATBOT_ROOT / "data" / "sessions.db"

PHONE_RE_CL = re.compile(r"^\+?56\s?9\s?\d{4}\s?\d{4}$|^\+?569\d{8}$|^9\d{8}$")


def normalize_phone(raw: str) -> str | None:
    """Normaliza a +569XXXXXXXX. None si no es válido."""
    if not raw:
        return None
    s = re.sub(r"[\s\-\.\(\)]", "", raw)
    if s.startswith("+56"):
        s = s[1:]
    if s.startswith("56") and len(s) == 11:
        return "+" + s
    if s.startswith("9") and len(s) == 9:
        return "+56" + s
    return None


def get_cohorte(pg_conn, cohorte: str, especialidad: str | None,
                 comuna: str | None) -> list[dict]:
    sql = """
        SELECT paciente_id, rut, nombre, apellido, telefono, comuna,
               ultima_especialidad, ultimo_profesional, dias_inactivo
        FROM bi.v_winback_cohortes
        WHERE cohorte = %s
          AND telefono IS NOT NULL AND telefono <> ''
    """
    params = [cohorte]
    if especialidad:
        sql += " AND ultima_especialidad ILIKE %s"
        params.append(f"%{especialidad}%")
    if comuna:
        sql += " AND comuna ILIKE %s"
        params.append(f"%{comuna}%")
    sql += " ORDER BY dias_inactivo ASC"
    with pg_conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        return list(cur.fetchall())


def already_sent_recently(pg_conn, paciente_id: int, days: int = 90) -> bool:
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM bi.winback_envios WHERE paciente_id = %s "
            "AND enviado_at >= NOW() - INTERVAL '%s days' LIMIT 1",
            (paciente_id, days),
        )
        return cur.fetchone() is not None


def is_excluded_in_bot(phone: str, sess_conn) -> str | None:
    """Retorna razón de exclusión o None si está OK."""
    norm = phone.lstrip("+")
    cur = sess_conn.execute(
        "SELECT 1 FROM contact_tags WHERE phone IN (?, ?) AND tag = 'marketing_opt_out' LIMIT 1",
        (phone, norm),
    )
    if cur.fetchone():
        return "marketing_opt_out"
    cur = sess_conn.execute(
        "SELECT state FROM sessions WHERE phone IN (?, ?) LIMIT 1",
        (phone, norm),
    )
    row = cur.fetchone()
    if row and row[0] == "HUMAN_TAKEOVER":
        return "human_takeover"
    return None


def first_name(full: str) -> str:
    parts = (full or "").strip().split()
    return parts[0].title() if parts else "paciente"


def send_template(phone: str, template: str, params: list[str]) -> tuple[int, dict]:
    payload = {
        "messaging_product": "whatsapp",
        "to": phone.lstrip("+"),
        "type": "template",
        "template": {
            "name": template,
            "language": {"code": "es"},
            "components": [{
                "type": "body",
                "parameters": [{"type": "text", "text": p} for p in params],
            }],
        },
    }
    headers = {"Authorization": f"Bearer {META_TOKEN}",
               "Content-Type": "application/json"}
    try:
        r = httpx.post(META_API, headers=headers, json=payload, timeout=15)
        return r.status_code, r.json()
    except httpx.HTTPError as e:
        return 0, {"error": {"message": str(e)}}


def log_envio(pg_conn, paciente_id: int, cohorte: str, telefono: str,
              template: str, error: str | None = None) -> None:
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO bi.winback_envios "
            "(paciente_id, cohorte, telefono, template_meta, nota) "
            "VALUES (%s, %s, %s, %s, %s)",
            (paciente_id, cohorte, telefono, template, error),
        )
    pg_conn.commit()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cohorte", required=True,
                    choices=["030d", "060d", "090d", "180d", "365d"])
    ap.add_argument("--template", required=True)
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--especialidad")
    ap.add_argument("--comuna")
    ap.add_argument("--rate", type=float, default=2.0)
    args = ap.parse_args()

    pg = psycopg2.connect(host=PG_HOST, port=PG_PORT, dbname=PG_DB,
                          user=PG_USER, password=PG_PASS)
    sess = sqlite3.connect(str(SESSIONS_DB))

    candidatos = get_cohorte(pg, args.cohorte, args.especialidad, args.comuna)
    print(f"Cohorte {args.cohorte}: {len(candidatos)} candidatos en BI")

    enviados = 0
    saltados = 0
    errores = 0
    motivos: dict[str, int] = {}

    for p in candidatos:
        if enviados >= args.limit:
            break

        phone = normalize_phone(p["telefono"])
        if not phone:
            saltados += 1
            motivos["telefono_invalido"] = motivos.get("telefono_invalido", 0) + 1
            continue

        if already_sent_recently(pg, p["paciente_id"], days=90):
            saltados += 1
            motivos["ya_enviado_90d"] = motivos.get("ya_enviado_90d", 0) + 1
            continue

        excl = is_excluded_in_bot(phone, sess)
        if excl:
            saltados += 1
            motivos[excl] = motivos.get(excl, 0) + 1
            continue

        nombre = first_name(p.get("nombre") or "")
        if args.dry_run:
            print(f"  [DRY] {phone} ({nombre}) — esp={p.get('ultima_especialidad')} días={p['dias_inactivo']}")
            enviados += 1
            continue

        status, resp = send_template(phone, args.template, [nombre])
        if status == 200:
            log_envio(pg, p["paciente_id"], args.cohorte, phone, args.template)
            enviados += 1
            print(f"  OK   {phone} ({nombre})")
        else:
            errores += 1
            err = (resp.get("error", {}) or {}).get("message", str(resp))[:120]
            log_envio(pg, p["paciente_id"], args.cohorte, phone, args.template,
                      error=f"HTTP {status}: {err}")
            print(f"  XX   {phone} ({nombre}) — {err}")
        time.sleep(args.rate)

    print(f"\n=== Resumen ===")
    print(f"Enviados:  {enviados}")
    print(f"Saltados:  {saltados} ({motivos})")
    print(f"Errores:   {errores}")
    if args.dry_run:
        print("(DRY-RUN — no se envió nada ni se logueó)")

    pg.close()
    sess.close()


if __name__ == "__main__":
    main()
