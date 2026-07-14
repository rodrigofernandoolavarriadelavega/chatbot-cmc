#!/usr/bin/env python3
"""
purga_retencion.py — Purga MANUAL de datos según la política de retención.

╔═══════════════════════════════════════════════════════════════════════════╗
║  ⚠️  ADVERTENCIA — LEER ANTES DE USAR                                      ║
║                                                                            ║
║  • Este script se corre MANUALMENTE, solo tras decisión explícita del      ║
║    dueño/responsable del tratamiento y con RESPALDO VERIFICADO             ║
║    (scripts/backup-cmc-db.sh hace smoke test de que la copia se abre).     ║
║  • NUNCA agregarlo a cron, systemd, launchd ni registrarlo en main.py.     ║
║    Cada purga es una decisión humana, cada vez.                            ║
║  • Por defecto es DRY-RUN: muestra cuántas filas borraría y los cortes de  ║
║    fecha, SIN tocar nada. Solo borra con AMBOS flags:                      ║
║        --ejecutar --confirmo-que-hay-respaldo                              ║
║  • Alcance deliberadamente mínimo (bajo riesgo / alto volumen):            ║
║        conversation_events  > N meses (default 24)                         ║
║        messages             > N meses (default 24)                         ║
║    NO toca perfiles, citas, patient_vitals, privacy_consents,              ║
║    family_links ni gdpr_deletions — esas purgas son MANUALES caso a caso   ║
║    (ver docs/politica_retencion_datos_BORRADOR.md §4).                     ║
║  • Los datos purgados persisten en los respaldos hasta que estos rotan.    ║
╚═══════════════════════════════════════════════════════════════════════════╝

Uso:
    python3 scripts/purga_retencion.py                          # dry-run (default)
    python3 scripts/purga_retencion.py --meses-eventos 36       # dry-run, corte distinto
    python3 scripts/purga_retencion.py --ejecutar --confirmo-que-hay-respaldo
    python3 scripts/purga_retencion.py --ejecutar --confirmo-que-hay-respaldo --vacuum

BD: data/sessions.db relativa a la raíz del repo (override con --db).
Si SQLCIPHER_KEY está definida en el entorno usa SQLCipher (mismo patrón que
app/session.py y scripts/migrate_phone_prefix.py); si no, sqlite3 plano.
"""

import argparse
import calendar
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH_DEFAULT = REPO_ROOT / "data" / "sessions.db"

# Guardrail: nunca aceptar un corte menor a esto (evita borrados masivos por typo)
MESES_MINIMO = 6

# ── Apertura de BD (mismo patrón que app/session.py) ─────────────────────────
SQLCIPHER_KEY = (os.environ.get("SQLCIPHER_KEY") or "").strip()

if SQLCIPHER_KEY:
    try:
        from sqlcipher3 import dbapi2 as sqlite3_mod  # type: ignore
        _DRIVER = "sqlcipher3"
    except ImportError:
        try:
            from pysqlcipher3 import dbapi2 as sqlite3_mod  # type: ignore
            _DRIVER = "pysqlcipher3"
        except ImportError:
            print("ERROR: SQLCIPHER_KEY está seteada pero no hay módulo sqlcipher "
                  "(pip install sqlcipher3-binary). Abortando para no abrir la BD mal.")
            sys.exit(1)
else:
    import sqlite3 as sqlite3_mod
    _DRIVER = "sqlite3 (sin cifrar)"


def abrir_conn(db_path: Path):
    conn = sqlite3_mod.connect(str(db_path), timeout=10)
    conn.row_factory = sqlite3_mod.Row
    if SQLCIPHER_KEY:
        import re as _re
        if not _re.fullmatch(r"[0-9a-fA-F]+", SQLCIPHER_KEY):
            print("ERROR: SQLCIPHER_KEY debe ser hex (0-9, a-f).")
            sys.exit(1)
        conn.execute(f"PRAGMA key = \"x'{SQLCIPHER_KEY}'\"")
        conn.execute("PRAGMA cipher_page_size = 4096")
    conn.execute("PRAGMA busy_timeout=10000")
    # Smoke test: si la llave es incorrecta, esto falla acá y no a mitad de purga
    conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
    return conn


# ── Utilidades ───────────────────────────────────────────────────────────────

def restar_meses(dt: datetime, meses: int) -> datetime:
    """Resta meses calendario (28-feb-safe)."""
    total = dt.year * 12 + (dt.month - 1) - meses
    year, month0 = divmod(total, 12)
    month = month0 + 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


def tabla_existe(conn, nombre: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (nombre,)
    ).fetchone()
    return row is not None


def contar(conn, tabla: str, col_ts: str, corte: str) -> tuple[int, int]:
    """Retorna (filas_a_borrar, filas_totales)."""
    a_borrar = conn.execute(
        f"SELECT COUNT(*) AS c FROM {tabla} WHERE {col_ts} < ?", (corte,)
    ).fetchone()["c"]
    total = conn.execute(f"SELECT COUNT(*) AS c FROM {tabla}").fetchone()["c"]
    return a_borrar, total


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Purga manual de retención (dry-run por defecto). "
                    "Ver header del script y docs/politica_retencion_datos_BORRADOR.md.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--meses-eventos", type=int, default=24,
                        help="Meses de retención para conversation_events")
    parser.add_argument("--meses-mensajes", type=int, default=24,
                        help="Meses de retención para messages")
    parser.add_argument("--ejecutar", action="store_true",
                        help="Borra de verdad (requiere además --confirmo-que-hay-respaldo)")
    parser.add_argument("--confirmo-que-hay-respaldo", action="store_true",
                        dest="confirmo_respaldo",
                        help="Declaración explícita de que existe respaldo verificado")
    parser.add_argument("--vacuum", action="store_true",
                        help="Correr VACUUM tras el borrado (solo con --ejecutar; "
                             "puede tardar y bloquear — usar en horario de baja carga)")
    parser.add_argument("--db", type=Path, default=DB_PATH_DEFAULT,
                        help="Ruta a sessions.db")
    args = parser.parse_args()

    # ── Validaciones defensivas ──────────────────────────────────────────
    if args.ejecutar and not args.confirmo_respaldo:
        print("ABORTADO: --ejecutar requiere también --confirmo-que-hay-respaldo.")
        print("Verifica el respaldo primero (scripts/backup-cmc-db.sh hace smoke test).")
        return 2
    if args.confirmo_respaldo and not args.ejecutar:
        print("NOTA: --confirmo-que-hay-respaldo sin --ejecutar → sigue siendo dry-run.")

    for nombre, meses in (("--meses-eventos", args.meses_eventos),
                          ("--meses-mensajes", args.meses_mensajes)):
        if meses < MESES_MINIMO:
            print(f"ABORTADO: {nombre}={meses} es menor al mínimo de seguridad "
                  f"({MESES_MINIMO} meses). Si de verdad quieres un corte tan "
                  f"agresivo, edita MESES_MINIMO conscientemente.")
            return 2

    if not args.db.exists():
        print(f"ERROR: no existe la BD en {args.db}")
        print("(Este script corre contra data/sessions.db del repo/VPS; "
              "en el VPS: cd /opt/chatbot-cmc && venv/bin/python3 scripts/purga_retencion.py)")
        return 1

    # ts en estas tablas es datetime('now') de SQLite = UTC "YYYY-MM-DD HH:MM:SS"
    ahora_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    objetivos = [
        # (tabla, columna_ts, meses, corte_str)
        ("conversation_events", "ts", args.meses_eventos,
         restar_meses(ahora_utc, args.meses_eventos).strftime("%Y-%m-%d %H:%M:%S")),
        ("messages", "ts", args.meses_mensajes,
         restar_meses(ahora_utc, args.meses_mensajes).strftime("%Y-%m-%d %H:%M:%S")),
    ]

    modo = "EJECUCIÓN REAL" if (args.ejecutar and args.confirmo_respaldo) else "DRY-RUN (no se toca nada)"
    print("=" * 74)
    print("purga_retencion.py — política de retención CMC (BORRADOR)")
    print(f"  Modo:    {modo}")
    print(f"  BD:      {args.db}")
    print(f"  Driver:  {_DRIVER}")
    print(f"  Ahora:   {ahora_utc:%Y-%m-%d %H:%M:%S} UTC")
    print("=" * 74)

    try:
        conn = abrir_conn(args.db)
    except Exception as e:  # llave mala, BD corrupta, permisos…
        print(f"ERROR abriendo la BD: {e}")
        return 1

    resumen: list[tuple[str, str, str, int, int, str]] = []
    ejecutando = args.ejecutar and args.confirmo_respaldo

    try:
        for tabla, col_ts, meses, corte in objetivos:
            if not tabla_existe(conn, tabla):
                print(f"\n[{tabla}] la tabla NO existe en esta BD — se omite y se continúa.")
                resumen.append((tabla, f"{meses}m", "—", 0, 0, "tabla inexistente"))
                continue

            a_borrar, total = contar(conn, tabla, col_ts, corte)
            print(f"\n[{tabla}]")
            print(f"  Retención:      {meses} meses")
            print(f"  Corte de fecha: {col_ts} < {corte} (UTC)")
            print(f"  Filas totales:  {total:,}")
            print(f"  Filas a borrar: {a_borrar:,}")

            if not ejecutando:
                resumen.append((tabla, f"{meses}m", corte, a_borrar, total, "dry-run"))
                continue

            if a_borrar == 0:
                print("  Nada que borrar.")
                resumen.append((tabla, f"{meses}m", corte, 0, total, "sin filas vencidas"))
                continue

            cur = conn.execute(
                f"DELETE FROM {tabla} WHERE {col_ts} < ?", (corte,)
            )
            conn.commit()
            print(f"  BORRADAS:       {cur.rowcount:,} filas")
            resumen.append((tabla, f"{meses}m", corte, cur.rowcount, total, "BORRADO"))

        if ejecutando and args.vacuum:
            print("\nCorriendo VACUUM (puede tardar)…")
            conn.execute("VACUUM")
            print("VACUUM listo.")
        elif ejecutando:
            print("\nNOTA: sin --vacuum el espacio no se devuelve al filesystem. "
                  "Correr con --vacuum en horario de baja carga si se quiere recuperar.")
    finally:
        conn.close()

    # ── Resumen final ────────────────────────────────────────────────────
    print("\n" + "=" * 74)
    print("RESUMEN")
    print(f"{'tabla':<22}{'ret.':<6}{'corte (UTC)':<21}{'filas':>10}{'total':>10}  estado")
    print("-" * 74)
    for tabla, ret, corte, filas, total, estado in resumen:
        print(f"{tabla:<22}{ret:<6}{corte:<21}{filas:>10,}{total:>10,}  {estado}")
    print("-" * 74)
    if not ejecutando:
        print("DRY-RUN: no se modificó nada. Para borrar de verdad:")
        print("  python3 scripts/purga_retencion.py --ejecutar --confirmo-que-hay-respaldo")
    else:
        print("Purga ejecutada. Recuerda: los datos persisten en los respaldos "
              "hasta que estos roten (ver política §5).")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    sys.exit(main())
