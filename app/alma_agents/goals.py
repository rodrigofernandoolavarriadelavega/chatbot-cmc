"""Goal-setting con presupuestos — la flota persigue metas, no solo tareas.

El salto de agencia: en vez de que tú prendas flags, el cerebro PROPONE metas
semanales a partir de su estado (alertas), les asigna los agentes adecuados y un
presupuesto de contacto, y mide el avance contra el Ledger. Tú apruebas (creas)
las que quieras; la flota las persigue y reporta progreso.

  propose_goals(world_state)  →  metas candidatas (read-only, no persiste)
  create_goal(...)            →  la activa (tabla agent_goals en sessions.db)
  progress(goal)              →  citas atribuidas a sus agentes / target

Proponer es seguro siempre; perseguir respeta el gating de la flota.
"""
import logging
from datetime import datetime, timedelta, timezone

log = logging.getLogger("bot")

# Mapa determinístico: dominio del cerebro → plantilla de meta.
_GOAL_TEMPLATES = {
    "agenda":       {"titulo": "Llenar la agenda", "metric": "citas", "target": 20,
                     "agents": ["yield_agenda"], "budget_contacts": 40},
    "demanda":      {"titulo": "Capturar demanda no satisfecha", "metric": "citas", "target": 10,
                     "agents": ["yield_agenda", "demanda_estrategia"], "budget_contacts": 25},
    "fidelizacion": {"titulo": "Recuperar pacientes inactivos", "metric": "citas", "target": 10,
                     "agents": ["reactivacion_winback"], "budget_contacts": 30},
    "caja":         {"titulo": "Recuperar copagos pendientes", "metric": "respuestas", "target": 15,
                     "agents": ["cobranza"], "budget_contacts": 20},
    "ads":          {"titulo": "Optimizar el gasto de pauta", "metric": "citas", "target": 8,
                     "agents": ["ads_executor"], "budget_contacts": 0},
}


def _conn():
    from session import _conn as _session_conn
    return _session_conn()


def ensure_table() -> None:
    try:
        conn = _conn()
        with conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS agent_goals (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    titulo       TEXT NOT NULL,
                    metric       TEXT NOT NULL,
                    target       INTEGER NOT NULL,
                    window_days  INTEGER DEFAULT 7,
                    agents       TEXT,            -- csv de agent_id
                    budget_contacts INTEGER DEFAULT 0,
                    status       TEXT DEFAULT 'activa',  -- activa|cumplida|expirada
                    created_at   TEXT DEFAULT (datetime('now'))
                )
            """)
        conn.close()
    except Exception as e:  # noqa: BLE001
        log.warning("goals.ensure_table falló: %s", e)


def propose_goals(world_state: dict | None) -> list[dict]:
    """Deriva metas candidatas de las alertas del cerebro. No persiste."""
    if not world_state:
        return []
    out, seen = [], set()
    # Ordenar alertas por severidad para priorizar.
    rank = {"critico": 0, "oportunidad": 1, "warn": 2, "info": 3}
    alerts = sorted(world_state.get("alerts", []) or [],
                    key=lambda a: rank.get(a.get("severity"), 9))
    for a in alerts:
        dom = a.get("domain")
        if dom in _GOAL_TEMPLATES and dom not in seen:
            seen.add(dom)
            tpl = _GOAL_TEMPLATES[dom]
            out.append({**tpl, "window_days": 7, "origen_alerta": a.get("message", ""),
                        "severidad": a.get("severity")})
    return out


def create_goal(titulo: str, metric: str, target: int, agents: list[str],
                window_days: int = 7, budget_contacts: int = 0) -> int | None:
    try:
        ensure_table()
        conn = _conn()
        with conn:
            cur = conn.execute(
                """INSERT INTO agent_goals (titulo, metric, target, window_days, agents, budget_contacts)
                   VALUES (?,?,?,?,?,?)""",
                (titulo, metric, int(target), int(window_days), ",".join(agents), int(budget_contacts)))
            gid = cur.lastrowid
        conn.close()
        return gid
    except Exception as e:  # noqa: BLE001
        log.warning("goals.create_goal falló: %s", e)
        return None


def _progress_row(conn, agents: list[str], metric: str, since_iso: str, until_iso: str) -> int:
    """Cuenta outcomes (citas o respuestas) atribuidos a esos agentes en la ventana."""
    if not agents:
        return 0
    qa = ",".join("?" * len(agents))
    outcome = "cita" if metric == "citas" else "respuesta"
    cur = conn.execute(
        f"""SELECT COUNT(*) FROM agent_action_ledger
            WHERE agent_id IN ({qa}) AND outcome = ?
              AND acted_at >= ? AND acted_at <= ?""",
        (*agents, outcome, since_iso, until_iso))
    return cur.fetchone()[0] or 0


def progress(goal: dict) -> dict:
    """Avance de una meta: outcomes logrados / target en su ventana."""
    from . import ledger
    try:
        ledger.ensure_table()
        conn = _conn()
        agents = (goal.get("agents") or "").split(",") if isinstance(goal.get("agents"), str) else goal.get("agents", [])
        agents = [a for a in agents if a]
        created = goal.get("created_at") or "1970-01-01 00:00:00"
        try:
            start = datetime.strptime(created[:19], "%Y-%m-%d %H:%M:%S")
        except Exception:  # noqa: BLE001
            start = datetime.now(timezone.utc).replace(tzinfo=None)
        end = (start + timedelta(days=goal.get("window_days", 7))).strftime("%Y-%m-%d %H:%M:%S")
        done = _progress_row(conn, agents, goal.get("metric", "citas"), created[:19], end)
        conn.close()
        target = goal.get("target", 1) or 1
        return {"done": done, "target": target,
                "pct": round(min(1.0, done / target) * 100), "cumplida": done >= target}
    except Exception as e:  # noqa: BLE001
        log.warning("goals.progress falló: %s", e)
        return {"done": 0, "target": goal.get("target", 0), "pct": 0, "cumplida": False}


def list_goals(with_progress: bool = True) -> list[dict]:
    try:
        ensure_table()
        conn = _conn()
        cols = ["id", "titulo", "metric", "target", "window_days", "agents",
                "budget_contacts", "status", "created_at"]
        rows = conn.execute(
            f"SELECT {','.join(cols)} FROM agent_goals ORDER BY created_at DESC").fetchall()
        conn.close()
        out = []
        for r in rows:
            g = dict(zip(cols, r))
            if with_progress:
                g["progreso"] = progress(g)
            out.append(g)
        return out
    except Exception as e:  # noqa: BLE001
        log.warning("goals.list_goals falló: %s", e)
        return []
