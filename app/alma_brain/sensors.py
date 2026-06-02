"""Sensores por dominio — la capa PERCIBIR del cerebro de Alma.

Cada sensor lee una fuente de verdad YA EXISTENTE y devuelve un dict
normalizado `{available: bool, kpis: {...}, ...}`. Reglas:

  - **Medilink-free**: ningún sensor golpea la API de Medilink (es el cuello de
    rate-limit del bot). Leemos de BI Postgres, sessions.db y snapshots ya
    persistidos por crons existentes.
  - **Degradación con gracia**: cualquier fallo → `available=False` + log warning,
    nunca una excepción que suba. El cerebro debe poder operar con dominios caídos.
  - **Fuentes fieles** (regla del negocio): ventas = `bi.fact_pagos` (Caja real),
    NUNCA `/atenciones.total`.

Los imports de módulos pesados (main, bi_sync, session) son LAZY dentro de cada
función para evitar import circular (main.py incluye el router de alma_brain).
"""
import logging
import os
from datetime import date, timedelta

log = logging.getLogger("bot")


# ── Helpers de conexión (lazy, degradan a None) ──────────────────────────────

def _bi_conn():
    """Conexión a BI Postgres. Reusa el pool de main si existe; si no, conecta
    directo por env vars BI_DB_*. Devuelve (conn, release_fn) o (None, None)."""
    # 1) Intentar el pool ya inicializado en main (eficiente, reusa conexiones).
    try:
        from main import _bi_pool  # lazy: main ya está cargado en runtime
        pool = _bi_pool()
        if pool is not None:
            conn = pool.getconn()
            return conn, lambda: pool.putconn(conn)
    except Exception as e:  # noqa: BLE001
        log.debug("alma_brain: pool BI de main no disponible (%s); intento directo", e)

    # 2) Fallback: conexión directa por env vars (mismo contrato que main).
    try:
        import psycopg2
        conn = psycopg2.connect(
            host=os.getenv("BI_DB_HOST", "127.0.0.1"),
            port=int(os.getenv("BI_DB_PORT", "5432")),
            dbname=os.getenv("BI_DB_NAME", "health_bi"),
            user=os.getenv("BI_DB_USER", "health_user"),
            password=os.getenv("BI_DB_PASSWORD", "password123"),
            connect_timeout=5,
        )
        return conn, conn.close
    except Exception as e:  # noqa: BLE001
        log.warning("alma_brain: sin conexión BI (%s); dominios BI quedan no disponibles", e)
        return None, None


def _bi_rows(sql: str, params: tuple = ()) -> list[tuple] | None:
    """Ejecuta una query de lectura en BI. None si BI no está disponible."""
    conn, release = _bi_conn()
    if conn is None:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    except Exception as e:  # noqa: BLE001
        log.warning("alma_brain: query BI falló (%s)", e)
        return None
    finally:
        try:
            release()
        except Exception:  # noqa: BLE001
            pass


def _sessions_rows(sql: str, params: tuple = ()) -> list | None:
    """Ejecuta una query de lectura en sessions.db (SQLite/SQLCipher). None si falla."""
    try:
        from session import _conn  # lazy
        conn = _conn()
    except Exception as e:  # noqa: BLE001
        log.warning("alma_brain: sin sessions.db (%s)", e)
        return None
    try:
        cur = conn.execute(sql, params)
        return cur.fetchall()
    except Exception as e:  # noqa: BLE001
        log.warning("alma_brain: query sessions.db falló (%s)", e)
        return None
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def _window(window_days: int) -> tuple[str, str]:
    today = date.today()
    return (today - timedelta(days=window_days)).isoformat(), today.isoformat()


# ── 1. AGENDA — volumen y tendencia de citas (desde bi.fact_citas) ───────────

def sense_agenda(window_days: int = 7) -> dict:
    """Salud de la agenda: volumen de citas en la ventana vs ventana previa,
    desglose por especialidad. Lee bi.fact_citas (historico, Medilink-free).

    No calcula ociosidad/yield fino (eso vive en /boxes y requiere modelo de
    capacidad + Medilink); acá damos la señal de volumen y tendencia, suficiente
    para que el cerebro detecte caídas y oportunidades."""
    date_from, date_to = _window(window_days)
    prev_from = (date.fromisoformat(date_from) - timedelta(days=window_days)).isoformat()

    rows = _bi_rows(
        """
        SELECT COALESCE(e.nombre, 'sin especialidad') AS esp, COUNT(*) AS n
        FROM bi.fact_citas c
        LEFT JOIN bi.dim_especialidad e ON e.especialidad_id = c.especialidad_id
        WHERE c.fecha >= %s AND c.fecha <= %s
        GROUP BY esp ORDER BY n DESC
        """,
        (date_from, date_to),
    )
    if rows is None:
        return {"available": False, "reason": "BI no disponible"}

    prev = _bi_rows(
        "SELECT COUNT(*) FROM bi.fact_citas WHERE fecha >= %s AND fecha < %s",
        (prev_from, date_from),
    )
    total = sum(r[1] for r in rows)
    total_prev = (prev[0][0] if prev else 0) or 0
    delta_pct = ((total - total_prev) / total_prev * 100) if total_prev else None

    return {
        "available": True,
        "window_days": window_days,
        "kpis": {
            "citas_ventana": total,
            "citas_ventana_previa": total_prev,
            "delta_pct": round(delta_pct, 1) if delta_pct is not None else None,
        },
        "por_especialidad": [{"especialidad": r[0], "citas": r[1]} for r in rows[:15]],
    }


# ── 2. CAJA — ingreso real (bi.fact_pagos, fuente fiel) ──────────────────────

def sense_caja(window_days: int = 7) -> dict:
    """Ingreso real cobrado en la ventana + mes actual, desde bi.fact_pagos
    (Caja Medilink). Fuente fiel del negocio — NUNCA /atenciones."""
    date_from, date_to = _window(window_days)
    win = _bi_rows(
        "SELECT COALESCE(SUM(monto),0)::float, COUNT(*) FROM bi.fact_pagos WHERE fecha >= %s AND fecha <= %s",
        (date_from, date_to),
    )
    if win is None:
        return {"available": False, "reason": "BI no disponible"}

    monto_win, n_win = win[0]
    # Mes actual (desde el día 1).
    mes_ini = date.today().replace(day=1).isoformat()
    mes = _bi_rows(
        "SELECT COALESCE(SUM(monto),0)::float, COUNT(*) FROM bi.fact_pagos WHERE fecha >= %s",
        (mes_ini,),
    )
    monto_mes, n_mes = (mes[0] if mes else (0, 0))

    por_prof = _bi_rows(
        """
        SELECT COALESCE(p.nombre, 'sin profesional') AS prof,
               COALESCE(SUM(f.monto),0)::float AS total, COUNT(*) AS n
        FROM bi.fact_pagos f
        LEFT JOIN bi.dim_profesional p ON p.profesional_id = f.profesional_id
        WHERE f.fecha >= %s
        GROUP BY prof ORDER BY total DESC
        """,
        (mes_ini,),
    ) or []

    return {
        "available": True,
        "kpis": {
            "ingreso_ventana_clp": round(monto_win or 0),
            "pagos_ventana": int(n_win or 0),
            "ingreso_mes_clp": round(monto_mes or 0),
            "pagos_mes": int(n_mes or 0),
        },
        "por_profesional_mes": [
            {"profesional": r[0], "ingreso_clp": round(r[1]), "pagos": r[2]} for r in por_prof[:15]
        ],
    }


# ── 3. DEMANDA — qué piden los pacientes que no capturamos (sessions.db) ──────

def sense_demanda(window_days: int = 90) -> dict:
    """Demanda no satisfecha: servicios sin cupo + interés por especialidad +
    catálogo que no ofrecemos. Reusa la lógica del dashboard /demanda."""
    # Preferimos la función canónica de main (ya probada). Lazy + degradación.
    try:
        from main import api_demanda_data  # lazy
        data = api_demanda_data(dias=window_days)
        if isinstance(data, dict) and data.get("kpis"):
            return {
                "available": True,
                "window_days": window_days,
                "kpis": data.get("kpis", {}),
                "servicios_sin_cupo": data.get("servicios_sin_cupo", [])[:10],
                "no_ofrecemos": data.get("no_ofrecemos", [])[:10],
            }
    except Exception as e:  # noqa: BLE001
        log.debug("alma_brain: api_demanda_data no usable (%s); fallback SQL directo", e)

    # Fallback: SQL directo sobre sessions.db.
    rows = _sessions_rows(
        """
        SELECT lower(json_extract(meta,'$.especialidad')) esp, COUNT(*) n,
               COUNT(DISTINCT phone) personas
        FROM conversation_events
        WHERE event='sin_disponibilidad' AND ts >= datetime('now', ?)
        GROUP BY esp ORDER BY n DESC
        """,
        (f"-{window_days} days",),
    )
    if rows is None:
        return {"available": False, "reason": "sessions.db no disponible"}
    items = [{"especialidad": r[0] or "?", "solicitudes": r[1], "personas": r[2]} for r in rows[:10]]
    return {
        "available": True,
        "window_days": window_days,
        "kpis": {"servicios_sin_cupo": len(items),
                 "solicitudes_total": sum(i["solicitudes"] for i in items)},
        "servicios_sin_cupo": items,
        "no_ofrecemos": [],
    }


# ── 4. ADS — reusa el snapshot del Autopilot (cero llamada a Meta) ───────────

def sense_ads() -> dict:
    """Estado publicitario: lee el snapshot que el cron del autopilot ya
    persistió. No golpea Meta (eso lo hace el dry-run del autopilot)."""
    try:
        from autopilot.world_state import load_snapshot  # lazy
        snap = load_snapshot()
    except Exception as e:  # noqa: BLE001
        return {"available": False, "reason": f"autopilot no importable ({e})"}
    if not snap:
        return {"available": False, "reason": "sin corridas del autopilot todavía"}
    k = snap.get("kpis", {})
    actions = snap.get("actions", [])
    return {
        "available": True,
        "generated_at": snap.get("generated_at"),
        "mode": snap.get("mode"),
        "kpis": {
            "spend_clp": round(k.get("total_spend", 0)),
            "purchases": k.get("total_purchases", 0),
            "ingreso_real_bi_clp": round(k.get("bi_revenue_real") or 0),
            "attribution_ratio": k.get("attribution_ratio"),
            "n_campanas": k.get("n_campaigns", 0),
        },
        # Acciones que el autopilot ya propuso (alimentan el razonamiento del copiloto).
        "acciones_propuestas": [
            {"campana": a.get("campaign_name"), "accion": a.get("action"),
             "motivo": a.get("reason"), "necesita_aprobacion": a.get("needs_approval")}
            for a in actions if a.get("action") not in (None, "keep")
        ][:10],
    }


# ── 5. FIDELIZACIÓN — cohortes contactables para win-back (BI) ───────────────

def sense_fidelizacion() -> dict:
    """Tamaño de las cohortes de reenganche contactables. Lee la vista BI
    v_winback_cohortes_contactables si existe; degrada si no."""
    rows = _bi_rows("SELECT COUNT(*) FROM bi.v_winback_cohortes_contactables")
    if rows is None:
        return {"available": False, "reason": "vista winback BI no disponible"}
    contactables = (rows[0][0] if rows else 0) or 0
    return {
        "available": True,
        "kpis": {"cohortes_contactables": int(contactables)},
    }
