#!/usr/bin/env python3
"""
BUG-09: Migración de registros duplicados con/sin prefijo '+' en sessions.db.

Tablas afectadas: sessions, contact_profiles, messages, fidelizacion_msgs,
conversation_events, citas_bot, y otras tablas con columna phone.

Estrategia segura (transacción atómica):
1. Para cada par ('+56XXX', '56XXX') en sessions:
   - Si '+56XXX' es más reciente → copiar sus datos a '56XXX' y borrar '+56XXX'
   - Si '56XXX' es más reciente → simplemente borrar '+56XXX'
2. Para cada '+56XXX' sin gemelo '56XXX' → UPDATE quitando el '+'
3. Repetir UPDATE simple en todas las demás tablas que usan phone.
"""

import sys
import os

DB_PATH = "/opt/chatbot-cmc/data/sessions.db"
SQLCIPHER_KEY = os.environ.get("SQLCIPHER_KEY", "")

# Abrir con sqlcipher3 si hay clave, si no sqlite3 plano
if SQLCIPHER_KEY:
    try:
        from sqlcipher3 import dbapi2 as sqlite3_mod
        print(f"Usando sqlcipher3 (clave {SQLCIPHER_KEY[:8]}...)")
    except ImportError:
        try:
            from pysqlcipher3 import dbapi2 as sqlite3_mod
            print("Usando pysqlcipher3")
        except ImportError:
            print("ERROR: sqlcipher3 no disponible y SQLCIPHER_KEY está seteada")
            sys.exit(1)
else:
    import sqlite3 as sqlite3_mod
    print("Usando sqlite3 plano (sin clave)")


def open_conn():
    conn = sqlite3_mod.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3_mod.Row
    if SQLCIPHER_KEY:
        # Hex raw key (mismo formato que session.py: PRAGMA key = "x'<hex>'")
        conn.execute(f"PRAGMA key = \"x'{SQLCIPHER_KEY}'\"")
        conn.execute("PRAGMA cipher_page_size = 4096")
    conn.execute("PRAGMA busy_timeout=10000")
    # Prueba rápida de que la clave es correcta
    conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
    return conn


def run():
    if not os.path.exists(DB_PATH):
        print(f"ERROR: No se encontró {DB_PATH}")
        sys.exit(1)

    conn = open_conn()

    # Tablas que tienen columna 'phone'
    TABLAS_PHONE = [
        "sessions",
        "contact_profiles",
        "messages",
        "fidelizacion_msgs",
        "conversation_events",
        "citas_bot",
        "contact_notes",
        "privacy_consents",
        "waitlist",
        "demanda_no_disponible",
    ]

    # Verificar cuáles existen realmente
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tablas_existentes = {r["name"] for r in cursor.fetchall()}
    print(f"Tablas en DB: {sorted(tablas_existentes)}\n")

    total_migrados = 0
    total_eliminados = 0

    with conn:
        # ── PASO 1: tabla sessions (tiene phone como PK) ──────────────────────
        tabla = "sessions"
        if tabla in tablas_existentes:
            cols_info = conn.execute(f"PRAGMA table_info({tabla})").fetchall()
            col_names_all = [r[1] for r in cols_info]
            col_set = set(col_names_all)

            rows_plus = conn.execute(
                f"SELECT * FROM {tabla} WHERE phone LIKE '+%'"
            ).fetchall()
            print(f"{tabla}: encontrados {len(rows_plus)} registros con '+'")

            for row in rows_plus:
                phone_plus = row["phone"]
                phone_sin  = phone_plus[1:]  # quitar el '+'

                gemelo = conn.execute(
                    f"SELECT * FROM {tabla} WHERE phone=?", (phone_sin,)
                ).fetchone()

                if gemelo is None:
                    # No hay gemelo → solo renombrar
                    conn.execute(
                        f"UPDATE {tabla} SET phone=? WHERE phone=?",
                        (phone_sin, phone_plus)
                    )
                    total_migrados += 1
                    print(f"  RENAME {phone_plus} → {phone_sin}")
                else:
                    # Hay gemelo → comparar updated_at
                    up_plus = row["updated_at"] if "updated_at" in col_set else "0"
                    up_sin  = gemelo["updated_at"] if "updated_at" in col_set else "0"

                    if str(up_plus or "0") > str(up_sin or "0"):
                        # '+' es más reciente → copiar datos (sin phone) al gemelo
                        set_parts = [f"{c}=?" for c in col_names_all if c != "phone"]
                        vals = [row[c] for c in col_names_all if c != "phone"]
                        conn.execute(
                            f"UPDATE {tabla} SET {', '.join(set_parts)} WHERE phone=?",
                            vals + [phone_sin]
                        )
                        print(f"  MERGE (+ más reciente) → actualizando {phone_sin}")
                    else:
                        print(f"  MERGE (sin '+' más reciente) → {phone_sin} se mantiene")

                    # En ambos casos borrar el '+' duplicado
                    conn.execute(
                        f"DELETE FROM {tabla} WHERE phone=?", (phone_plus,)
                    )
                    total_eliminados += 1

        # ── PASO 2: resto de tablas ──────────────────────────────────────────
        # Tablas con phone como PK deben tratarse con merge (igual que sessions)
        TABLAS_PK_PHONE = {"contact_profiles", "privacy_consents"}

        for tabla in TABLAS_PHONE:
            if tabla == "sessions":
                continue
            if tabla not in tablas_existentes:
                print(f"SKIP {tabla}: no existe")
                continue

            cols_info_t = conn.execute(f"PRAGMA table_info({tabla})").fetchall()
            col_names_t = [r[1] for r in cols_info_t]
            cols_set_t = set(col_names_t)
            if "phone" not in cols_set_t:
                print(f"SKIP {tabla}: sin columna phone")
                continue

            n = conn.execute(
                f"SELECT COUNT(*) FROM {tabla} WHERE phone LIKE '+%'"
            ).fetchone()[0]
            print(f"{tabla}: {n} registros con '+'")

            if n == 0:
                continue

            if tabla in TABLAS_PK_PHONE:
                # PK en phone → merge igual que sessions
                rows_plus_t = conn.execute(
                    f"SELECT * FROM {tabla} WHERE phone LIKE '+%'"
                ).fetchall()
                for row_t in rows_plus_t:
                    phone_plus_t = row_t["phone"]
                    phone_sin_t  = phone_plus_t[1:]
                    gemelo_t = conn.execute(
                        f"SELECT phone FROM {tabla} WHERE phone=?", (phone_sin_t,)
                    ).fetchone()
                    if gemelo_t is None:
                        conn.execute(
                            f"UPDATE {tabla} SET phone=? WHERE phone=?",
                            (phone_sin_t, phone_plus_t)
                        )
                        total_migrados += 1
                        print(f"  RENAME {phone_plus_t} → {phone_sin_t}")
                    else:
                        # Gemelo existe → borrar el '+'
                        conn.execute(
                            f"DELETE FROM {tabla} WHERE phone=?", (phone_plus_t,)
                        )
                        total_eliminados += 1
                        print(f"  DELETE duplicado {phone_plus_t} (mantiene {phone_sin_t})")
            else:
                # Sin PK en phone → UPDATE masivo seguro
                conn.execute(
                    f"UPDATE {tabla} SET phone = SUBSTR(phone, 2) WHERE phone LIKE '+%'"
                )
                total_migrados += n

    # Verificación final
    print("\n── Verificación final ────────────────────────────────────────")
    conn2 = open_conn()
    for tabla in TABLAS_PHONE:
        if tabla not in tablas_existentes:
            continue
        cols_set = {r[1] for r in conn2.execute(f"PRAGMA table_info({tabla})").fetchall()}
        if "phone" not in cols_set:
            continue
        n = conn2.execute(
            f"SELECT COUNT(*) FROM {tabla} WHERE phone LIKE '+%'"
        ).fetchone()[0]
        status = "OK (0 restantes)" if n == 0 else f"WARN: {n} restantes"
        print(f"  {tabla}: {status}")
    conn2.close()

    print(f"\n── Resumen ───────────────────────────────────────────────────")
    print(f"  Registros migrados (rename/update): {total_migrados}")
    print(f"  Registros eliminados (duplicados):  {total_eliminados}")
    print("  Migración completada.")


if __name__ == "__main__":
    run()
