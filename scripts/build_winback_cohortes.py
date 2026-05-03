"""
Construye tabla `winback_cohortes` cruzando heatmap_cache.db (atenciones reales
de Medilink) con sessions.db (perfiles paciente). Cohortes basadas en días
desde la última cita atendida.

Cohortes:
  30  → 30-59 días sin atenderse
  60  → 60-89 días
  90  → 90-179 días
  180 → 180-364 días
  365 → 365+ días

Uso:
  python3 scripts/build_winback_cohortes.py [--dry-run]
  python3 scripts/build_winback_cohortes.py --print-stats
"""
import argparse
import os
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HEATMAP_DB = ROOT / "data" / "heatmap_cache.db"
SESSIONS_DB = ROOT / "data" / "sessions.db"


def cohorte_for_days(d: int) -> str | None:
    if d < 30:
        return None
    if d < 60:
        return "30"
    if d < 90:
        return "60"
    if d < 180:
        return "90"
    if d < 365:
        return "180"
    return "365"


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS winback_cohortes (
    id_paciente_medilink   INTEGER PRIMARY KEY,
    rut                    TEXT,
    nombre                 TEXT,
    apellidos              TEXT,
    celular                TEXT,
    comuna                 TEXT,
    sexo                   TEXT,
    fecha_nacimiento       TEXT,
    ultima_cita_fecha      TEXT,
    ultimo_profesional     TEXT,
    ultimo_id_profesional  INTEGER,
    dias_inactivo          INTEGER,
    cohorte                TEXT,
    total_citas_historico  INTEGER,
    contactado_at          TEXT,
    contactado_canal       TEXT,
    respondio_at           TEXT,
    agendo_post_winback_at TEXT,
    opt_out                INTEGER DEFAULT 0,
    updated_at             TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_winback_cohorte  ON winback_cohortes(cohorte);
CREATE INDEX IF NOT EXISTS idx_winback_celular  ON winback_cohortes(celular);
CREATE INDEX IF NOT EXISTS idx_winback_dias     ON winback_cohortes(dias_inactivo);
CREATE INDEX IF NOT EXISTS idx_winback_comuna   ON winback_cohortes(comuna);
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--print-stats", action="store_true")
    ap.add_argument("--sessions-db", default=str(SESSIONS_DB))
    ap.add_argument("--heatmap-db", default=str(HEATMAP_DB))
    args = ap.parse_args()

    if not Path(args.heatmap_db).exists():
        print(f"ERROR: no existe {args.heatmap_db}", file=sys.stderr)
        sys.exit(2)

    sessions_db = args.sessions_db
    if not Path(sessions_db).exists():
        print(f"WARN: {sessions_db} no existe — la tabla se creará en runtime del bot")

    today = date.today()
    print(f"Build winback_cohortes — referencia: {today.isoformat()}")

    hm = sqlite3.connect(args.heatmap_db)
    hm.row_factory = sqlite3.Row

    # 1. Última cita atendida por paciente + total citas
    rows = hm.execute("""
        SELECT
            p.id, p.rut, p.nombre, p.apellidos, p.celular, p.comuna,
            p.sexo, p.fecha_nacimiento,
            MAX(c.fecha) AS ultima_fecha,
            COUNT(c.id) AS total_citas
        FROM pacientes_heatmap p
        JOIN citas_heatmap c ON c.id_paciente = p.id
        WHERE c.estado_cita = 'Atendido'
        GROUP BY p.id
    """).fetchall()

    # 2. Profesional de la última cita por paciente
    last_prof = {
        r["id_paciente"]: (r["nombre_profesional"], r["id_profesional"])
        for r in hm.execute("""
            SELECT c1.id_paciente, c1.nombre_profesional, c1.id_profesional
            FROM citas_heatmap c1
            JOIN (
                SELECT id_paciente, MAX(fecha) AS f
                FROM citas_heatmap WHERE estado_cita='Atendido'
                GROUP BY id_paciente
            ) m ON m.id_paciente = c1.id_paciente AND m.f = c1.fecha
            WHERE c1.estado_cita='Atendido'
        """).fetchall()
    }

    cohortes_n = {"30": 0, "60": 0, "90": 0, "180": 0, "365": 0, None: 0}
    payload = []
    for r in rows:
        try:
            ultima = datetime.strptime(r["ultima_fecha"], "%Y-%m-%d").date()
        except (ValueError, TypeError):
            continue
        dias = (today - ultima).days
        c = cohorte_for_days(dias)
        cohortes_n[c] += 1
        if c is None:
            continue
        prof_nombre, prof_id = last_prof.get(r["id"], (None, None))
        payload.append((
            r["id"], r["rut"], r["nombre"], r["apellidos"], r["celular"],
            r["comuna"], r["sexo"], r["fecha_nacimiento"],
            r["ultima_fecha"], prof_nombre, prof_id,
            dias, c, r["total_citas"],
        ))

    print(f"\nCohortes calculadas:")
    for k in ("30", "60", "90", "180", "365"):
        print(f"  {k:>4}d → {cohortes_n[k]:>4} pacientes")
    print(f"  <30d (excluidos) → {cohortes_n[None]} pacientes")
    print(f"\nTotal a poblar: {len(payload)}")

    if args.print_stats:
        return

    if args.dry_run:
        print("\n--dry-run — no se escribió en sessions.db")
        return

    if not Path(sessions_db).exists():
        print(f"\nERROR: {sessions_db} no existe — corre el bot al menos una vez antes")
        sys.exit(2)

    # SQLCipher / cifrado: si la DB está cifrada el script no la abre.
    # Para entornos con SQLCipher correr este script desde el contexto del bot.
    sess = sqlite3.connect(sessions_db)
    sess.executescript(SCHEMA_SQL)

    # UPSERT: preserva contactado_at, respondio_at, agendo_post, opt_out si ya existían.
    sess.executemany("""
        INSERT INTO winback_cohortes (
            id_paciente_medilink, rut, nombre, apellidos, celular, comuna,
            sexo, fecha_nacimiento, ultima_cita_fecha, ultimo_profesional,
            ultimo_id_profesional, dias_inactivo, cohorte, total_citas_historico,
            updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT(id_paciente_medilink) DO UPDATE SET
            rut=excluded.rut,
            nombre=excluded.nombre,
            apellidos=excluded.apellidos,
            celular=excluded.celular,
            comuna=excluded.comuna,
            sexo=excluded.sexo,
            fecha_nacimiento=excluded.fecha_nacimiento,
            ultima_cita_fecha=excluded.ultima_cita_fecha,
            ultimo_profesional=excluded.ultimo_profesional,
            ultimo_id_profesional=excluded.ultimo_id_profesional,
            dias_inactivo=excluded.dias_inactivo,
            cohorte=excluded.cohorte,
            total_citas_historico=excluded.total_citas_historico,
            updated_at=datetime('now')
    """, payload)
    sess.commit()
    sess.close()
    hm.close()

    print(f"\nOK — {len(payload)} filas upsert en winback_cohortes")


if __name__ == "__main__":
    main()
