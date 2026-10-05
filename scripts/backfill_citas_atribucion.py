"""Rellena citas_bot.ad_* para citas históricas: último clic en anuncio Meta
(meta_referrals) en los 90 días previos a la creación de la cita.

Solo toca filas con ad_source_id NULL → idempotente. Correr en el VPS:
  cd /opt/chatbot-cmc/app && ../venv/bin/python ../scripts/backfill_citas_atribucion.py [--dry-run]
"""
import sys
from datetime import datetime, timezone

sys.path.insert(0, ".")
from dotenv import load_dotenv  # noqa: E402

load_dotenv("../.env")
from session import _run_ddl_inline, db, ultimo_referral_antes_de  # noqa: E402

dry = "--dry-run" in sys.argv
with db() as conn:
    _run_ddl_inline(conn)
    conn.commit()
    filas = conn.execute(
        "SELECT rowid, phone, created_at FROM citas_bot WHERE ad_source_id IS NULL"
    ).fetchall()

asignadas = 0
for f in filas:
    try:
        ts = int(datetime.strptime(f["created_at"], "%Y-%m-%d %H:%M:%S")
                 .replace(tzinfo=timezone.utc).timestamp())
    except (TypeError, ValueError):
        continue
    ref = ultimo_referral_antes_de(f["phone"], ts)
    if not ref:
        continue
    asignadas += 1
    if not dry:
        with db() as conn:
            conn.execute(
                """UPDATE citas_bot SET ad_source_id=?, ad_headline=?,
                          ad_plataforma=?, ad_referral_ts=? WHERE rowid=?""",
                (ref["source_id"], ref["headline"], ref["plataforma"], ref["ts"],
                 f["rowid"]),
            )
            conn.commit()

print(f"citas revisadas={len(filas)} con anuncio={asignadas} dry_run={dry}")
