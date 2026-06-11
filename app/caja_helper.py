"""Lectura de la CAJA REAL (bi_pagos_caja) como fuente FRESCA para módulos operativos.

Reemplaza las queries a bi.fact_atenciones (BI Postgres — vieja, refresco manual que
depende de Docker en el laptop). bi_pagos_caja la llena el bot por cron nocturno, vive
en sessions.db (systemd, siempre arriba) y no depende de Docker ni del warehouse.

Patrón "hechos frescos + identidad estable":
  - Hechos (qué pagó, cuándo, cuánto) → bi_pagos_caja (fresca a hoy).
  - Identidad del paciente (nombre/teléfono/localidad) → bi.dim_paciente (cambia lento;
    su vejez no importa), con fallback local a citas_cache (nombre) si el BI no responde.
  - Nombre del profesional → medilink.PROFESIONALES (local, sin BI).

Ver memory/cmc_ventas_fuente_fiel: para datos operativos/actuales SIEMPRE la caja.
"""
import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

log = logging.getLogger("caja_helper")
_TZ = ZoneInfo("America/Santiago")


def hoy() -> date:
    return datetime.now(_TZ).date()


def meses_atras(meses: int) -> str:
    """Primer día del mes `meses` atrás (YYYY-MM-DD) para filtrar la caja."""
    t = hoy()
    y, m = t.year, t.month - meses
    while m <= 0:
        m += 12
        y -= 1
    return f"{y:04d}-{m:02d}-01"


def _identidad_pacientes(pids: list[int]) -> dict[int, dict]:
    """{id: {paciente, telefono, lugar}} desde bi.dim_paciente (best-effort).
    Devuelve {} si el BI no está disponible — el caller cae a nombres locales."""
    if not pids:
        return {}
    try:
        from main import _bi_pool
        pool = _bi_pool()
        conn = None
        try:
            conn = pool.getconn()
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT paciente_id, "
                    "TRIM(COALESCE(nombre,'') || ' ' || COALESCE(apellido,'')), "
                    "telefono, COALESCE(NULLIF(TRIM(localidad),''), comuna, '') "
                    "FROM bi.dim_paciente WHERE paciente_id = ANY(%s)",
                    (list(pids),),
                )
                return {
                    r[0]: {"paciente": r[1], "telefono": r[2] or "", "lugar": r[3] or ""}
                    for r in cur.fetchall()
                }
        finally:
            if conn is not None:
                pool.putconn(conn)
    except Exception as e:
        log.warning("identidad BI no disponible, uso cache local (%s)", e)
        return {}


def caja_visitas(prof_ids, meses: int) -> tuple[list[dict], str]:
    """Visitas (pagos) de la CAJA REAL para los profesionales dados, enriquecidas.

    Cada pago = una visita; instalación/control/sesión se clasifica por monto aguas abajo.
    Devuelve (rows, status) con el shape que esperan los _compute de los módulos:
      {paciente_id, paciente, telefono, lugar, fecha, profesional_id, profesional, monto}
    status: "ok" | "caja_unavailable".
    """
    from session import db as _conn
    prof_ids = tuple(prof_ids)
    if not prof_ids:
        return [], "ok"
    desde = meses_atras(meses)
    ph = ",".join("?" * len(prof_ids))
    try:
        with _conn() as c:
            pagos = c.execute(
                f"SELECT id_paciente, fecha, monto, id_profesional FROM bi_pagos_caja "
                f"WHERE id_profesional IN ({ph}) AND fecha >= ? AND id_paciente IS NOT NULL "
                f"ORDER BY id_paciente, fecha",
                (*prof_ids, desde),
            ).fetchall()
            nombres_local = {
                row[0]: row[1]
                for row in c.execute(
                    "SELECT id_paciente, paciente_nombre FROM citas_cache"
                ).fetchall()
            }
    except Exception as e:
        log.warning("caja (bi_pagos_caja) no disponible (%s)", e)
        return [], "caja_unavailable"

    if not pagos:
        return [], "ok"

    try:
        from medilink import PROFESIONALES
        prof_nombres = {pid: PROFESIONALES.get(pid, {}).get("nombre", "") for pid in prof_ids}
    except Exception:
        prof_nombres = {}

    pids = sorted({row[0] for row in pagos})
    ident = _identidad_pacientes(pids)

    rows = []
    for id_pac, fecha, monto, id_prof in pagos:
        info = ident.get(id_pac) or {}
        rows.append({
            "paciente_id": id_pac,
            "paciente": info.get("paciente") or nombres_local.get(id_pac) or "",
            "telefono": info.get("telefono") or "",
            "lugar": info.get("lugar") or "",
            "fecha": fecha,
            "profesional_id": id_prof,
            "profesional": prof_nombres.get(id_prof, ""),
            "monto": float(monto or 0),
        })
    return rows, "ok"
