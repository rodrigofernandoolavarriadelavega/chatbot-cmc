#!/usr/bin/env python3
"""
Puebla data/heatmap_cache.db (citas_heatmap + pacientes_heatmap) desde el
warehouse BI (Postgres en Docker) en vez de re-bajar día por día de Medilink.

Por qué: el BI ya es el espejo nocturno de Medilink (ETL 04:30) y tiene comuna/
localidad + ~4 años de citas (fact_citas desde 2022). Rebuild instantáneo y con
CERO carga a Medilink. El mapa por comunas sale directo de acá (usa centroides de
comuna). Solo el mapa por DIRECCIONES necesita además geocoding
(scripts/geocode_direcciones.py), porque el BI no guarda lat/lng.

Reemplaza el flujo lento `heatmap_comunas.py download` (Medilink, ~1h, solo 3 meses).

Uso: PYTHONPATH=app python scripts/heatmap_from_bi.py
"""
import logging
import os
import sqlite3

import psycopg2

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

DB_PATH = "data/heatmap_cache.db"
_BI = dict(
    host=os.getenv("BI_DB_HOST", "127.0.0.1"),
    port=int(os.getenv("BI_DB_PORT", "5432")),
    dbname=os.getenv("BI_DB_NAME", "health_bi"),
    user=os.getenv("BI_DB_USER", "health_user"),
    password=os.getenv("BI_DB_PASSWORD", "password123"),
)


def init_db(conn: sqlite3.Connection):
    """Mismo schema que scripts/heatmap_comunas.py::init_db (fuente de verdad)."""
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS citas_heatmap (
            id INTEGER PRIMARY KEY, id_paciente INTEGER, id_profesional INTEGER,
            nombre_profesional TEXT, fecha TEXT, hora_inicio TEXT, estado_cita TEXT,
            descargado_at TEXT DEFAULT CURRENT_TIMESTAMP)
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pacientes_heatmap (
            id INTEGER PRIMARY KEY, nombre TEXT, apellidos TEXT, rut TEXT, comuna TEXT,
            ciudad TEXT, direccion TEXT, fecha_nacimiento TEXT, sexo TEXT, celular TEXT,
            email TEXT, descargado_at TEXT DEFAULT CURRENT_TIMESTAMP)
    """)
    conn.commit()


def main():
    pg = psycopg2.connect(connect_timeout=15, **_BI)
    sq = sqlite3.connect(DB_PATH)
    init_db(sq)

    # ── Pacientes (dim_paciente completo) ──────────────────────────────────
    with pg.cursor() as cur:
        cur.execute("""
            SELECT paciente_id, nombre, apellido, rut, comuna, ciudad,
                   direccion, fecha_nacimiento, genero, telefono, email
            FROM bi.dim_paciente
        """)
        pac = cur.fetchall()
    sq.executemany("""
        INSERT OR REPLACE INTO pacientes_heatmap
        (id,nombre,apellidos,rut,comuna,ciudad,direccion,fecha_nacimiento,sexo,celular,email)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
    """, [
        (r[0], r[1], r[2], r[3], r[4], r[5], r[6],
         str(r[7]) if r[7] else None, r[8], r[9], r[10])
        for r in pac
    ])
    sq.commit()
    con_comuna = sum(1 for r in pac if r[4])
    log.info("pacientes_heatmap: %d (con comuna: %d)", len(pac), con_comuna)

    # ── Citas (fact_citas + nombre del profesional) ────────────────────────
    with pg.cursor() as cur:
        cur.execute("""
            SELECT c.cita_id, c.paciente_id, c.profesional_id,
                   trim(coalesce(p.nombre,'') || ' ' || coalesce(p.apellido,'')),
                   to_char(c.fecha, 'YYYY-MM-DD'), c.hora_inicio, c.estado
            FROM bi.fact_citas c
            LEFT JOIN bi.dim_profesional p ON p.profesional_id = c.profesional_id
            WHERE c.paciente_id IS NOT NULL
        """)
        citas = cur.fetchall()
    sq.executemany("""
        INSERT OR REPLACE INTO citas_heatmap
        (id,id_paciente,id_profesional,nombre_profesional,fecha,hora_inicio,estado_cita)
        VALUES (?,?,?,?,?,?,?)
    """, citas)
    sq.commit()
    log.info("citas_heatmap: %d", len(citas))

    rango = sq.execute("SELECT MIN(fecha), MAX(fecha) FROM citas_heatmap").fetchone()
    log.info("rango fechas citas: %s → %s", rango[0], rango[1])

    pg.close()
    sq.close()
    log.info("heatmap poblado desde BI OK")


if __name__ == "__main__":
    main()
