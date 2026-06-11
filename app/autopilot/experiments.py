"""Champion / Challenger — el cierre del nivel 5 (auto-mejora gobernada).

Un cambio de política NO se adopta porque "suena mejor": se prueba como RETADOR
(challenger) contra la regla vigente (champion), se mide con un control, y solo se
promueve si gana con evidencia. Si no, se revierte/descarta. Esa disciplina es lo que
impide que el sistema se auto-degrade optimizando ruido.

Esta primera versión es SEGURA por diseño:
  • Ledger de experimentos (tabla policy_experiments) — propose/list/decide.
  • Asignación determinística champion/challenger (para uso prospectivo).
  • Evaluación RETROSPECTIVA sobre data real ya existente (winback_envios): permite
    un veredicto HOY sin cambiar comportamiento vivo.
  • La PROMOCIÓN es una recomendación: nunca auto-muta una política de producción.
    Aplicar el ganador a policy.py/winback es una acción humana, gateada.

Pisos: solo políticas de marketing/operación. Jamás triage, derivación clínica, consent.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os

log = logging.getLogger("bot")

# Interruptor maestro del modo PROSPECTIVO (asignación en vivo que afecta a quién se
# contacta). OFF por defecto: la maquinaria existe pero NO decide nada hasta prenderlo
# desde la Sala de Máquinas. Encenderlo = el win-back empieza a contactar pacientes
# frescos del brazo challenger → mensajes reales. Decisión deliberada del dueño.
EXPERIMENTS_LIVE = os.getenv("EXPERIMENTS_LIVE_ENABLED", "false").lower() in ("1", "true", "yes", "on")


def _conn():
    from session import db as _c
    return _c()


def _ensure_table(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS policy_experiments (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT,
            policy      TEXT,
            champion    TEXT,
            challenger  TEXT,
            metric      TEXT,
            hypothesis  TEXT,
            holdout_pct INTEGER DEFAULT 50,
            status      TEXT DEFAULT 'proposed',   -- proposed|running|decided
            verdict     TEXT,
            result_json TEXT,
            created_at  TEXT DEFAULT (datetime('now')),
            decided_at  TEXT
        )
    """)


# ─────────────────────────────────────────────────────────────────────────────
# Ledger
# ─────────────────────────────────────────────────────────────────────────────
def propose(name: str, policy: str, champion: str, challenger: str,
            metric: str, hypothesis: str, holdout_pct: int = 50) -> int:
    """Registra un experimento propuesto (idempotente por name). Devuelve su id."""
    with _conn() as conn:
        _ensure_table(conn)
        row = conn.execute("SELECT id FROM policy_experiments WHERE name=?", (name,)).fetchone()
        if row:
            return row["id"]
        cur = conn.execute(
            "INSERT INTO policy_experiments (name, policy, champion, challenger, metric, "
            "hypothesis, holdout_pct) VALUES (?,?,?,?,?,?,?)",
            (name, policy, champion, challenger, metric, hypothesis, holdout_pct))
        conn.commit()
        return cur.lastrowid


def list_experiments() -> list[dict]:
    with _conn() as conn:
        _ensure_table(conn)
        rows = conn.execute(
            "SELECT * FROM policy_experiments ORDER BY created_at DESC").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            if d.get("result_json"):
                try:
                    d["result"] = json.loads(d["result_json"])
                except Exception:  # noqa: BLE001
                    pass
            d.pop("result_json", None)
            out.append(d)
        return out


def _save_verdict(exp_id: int, verdict: str, result: dict) -> None:
    with _conn() as conn:
        _ensure_table(conn)
        conn.execute(
            "UPDATE policy_experiments SET verdict=?, result_json=?, status='decided', "
            "decided_at=datetime('now') WHERE id=?",
            (verdict, json.dumps(result, ensure_ascii=False), exp_id))
        conn.commit()


# ─────────────────────────────────────────────────────────────────────────────
# Asignación determinística (uso prospectivo: misma unidad → mismo brazo siempre)
# ─────────────────────────────────────────────────────────────────────────────
def assign(exp_id: int, unit_key: str, holdout_pct: int = 50) -> str:
    """'challenger' si el hash de (exp, unidad) cae en el holdout; si no 'champion'.
    Determinístico y estable: un paciente queda fijo en su brazo. Listo para cablear a
    los puntos de decisión (winback/autopilot) cuando se corra un experimento en vivo."""
    h = hashlib.sha256(f"{exp_id}:{unit_key}".encode()).hexdigest()
    bucket = int(h[:8], 16) % 100
    return "challenger" if bucket < holdout_pct else "champion"


def set_status(name: str, status: str) -> None:
    """proposed → running → decided. Arrancar un experimento es una acción gateada."""
    with _conn() as conn:
        _ensure_table(conn)
        conn.execute("UPDATE policy_experiments SET status=? WHERE name=?", (status, name))
        conn.commit()


def _ensure_decisions(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS experiment_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            experiment TEXT, unit_key TEXT, arm TEXT,
            decided_at TEXT DEFAULT (datetime('now')),
            UNIQUE(experiment, unit_key)
        )
    """)


def live_arm(exp_name: str, unit_key: str, holdout_pct: int = 50) -> str | None:
    """Punto de decisión PROSPECTIVO. Devuelve 'champion'/'challenger' (estable por
    unidad) y lo registra, SOLO si el experimento está 'running' Y EXPERIMENTS_LIVE
    está prendido. Si no, devuelve None → el caller usa el comportamiento champion
    (status quo). Así, cablearlo a winback es inerte hasta encender el flag.

    Integración prevista (NO cableada aún, zona guard del sprint win-back):
      en winback.get_candidatos_dia / job, para una cohorte fresca (30-60d):
        arm = experiments.live_arm("winback_timing_30d", phone)
        if arm == "challenger":  # solo el brazo retador se contacta fresco
            ...incluir al candidato...
    """
    if not EXPERIMENTS_LIVE:
        return None
    with _conn() as conn:
        _ensure_table(conn)
        _ensure_decisions(conn)
        exp = conn.execute(
            "SELECT id, status, holdout_pct FROM policy_experiments WHERE name=?",
            (exp_name,)).fetchone()
        if not exp or exp["status"] != "running":
            return None
        arm = assign(exp["id"], unit_key, exp["holdout_pct"] or holdout_pct)
        conn.execute(
            "INSERT OR IGNORE INTO experiment_decisions (experiment, unit_key, arm) "
            "VALUES (?,?,?)", (exp_name, unit_key, arm))
        conn.commit()
        return arm


# ─────────────────────────────────────────────────────────────────────────────
# Estadística honesta (sin scipy)
# ─────────────────────────────────────────────────────────────────────────────
def _two_prop(s1: int, n1: int, s2: int, n2: int) -> dict | None:
    """z-test de dos proporciones. s=éxitos, n=total. Brazo 1 = challenger."""
    if n1 == 0 or n2 == 0:
        return None
    p1, p2 = s1 / n1, s2 / n2
    p = (s1 + s2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    if se == 0:
        return None
    z = (p1 - p2) / se
    pval = 2 * (1 - 0.5 * (1 + math.erf(abs(z) / math.sqrt(2))))  # two-tailed
    lift = (p1 / p2) if p2 > 0 else None
    return {"rate_challenger": round(100 * p1, 1), "rate_champion": round(100 * p2, 1),
            "n_challenger": n1, "n_champion": n2,
            "lift": round(lift, 2) if lift else None,
            "z": round(z, 2), "p_value": round(pval, 3)}


def _verdict_from(stat: dict | None) -> tuple[str, str]:
    """(veredicto_corto, explicación). Disciplina: no promover con evidencia marginal."""
    if not stat:
        return "inconcluso", "sin datos suficientes para decidir."
    z, p, lift = stat["z"], stat["p_value"], stat.get("lift")
    better = stat["rate_challenger"] > stat["rate_champion"]
    if better and p < 0.05:
        return "promover", (f"el challenger gana {stat['rate_challenger']}% vs "
                            f"{stat['rate_champion']}% (lift {lift}×, p={p}) — significativo.")
    if better and p < 0.20:
        return "prometedor — confirmar prospectivamente", (
            f"challenger {stat['rate_challenger']}% vs {stat['rate_champion']}% (lift {lift}×) "
            f"pero p={p} (marginal). Correr el experimento EN VIVO antes de promover.")
    if not better:
        return "mantener champion", (f"el challenger NO supera al champion "
                                     f"({stat['rate_challenger']}% vs {stat['rate_champion']}%).")
    return "inconcluso", f"diferencia no concluyente (p={p})."


# ─────────────────────────────────────────────────────────────────────────────
# Evaluador retrospectivo #1 — Timing de win-back (sobre data ya existente)
# ─────────────────────────────────────────────────────────────────────────────
def evaluate_winback_timing(exp_id: int | None = None) -> dict:
    """Champion = win-back estándar (cohortes 90/180/365d). Challenger = disparar
    fresco (30/60d). Mide conversión real de winback_envios y emite veredicto honesto.
    NO cambia nada: es un backtest sobre el histórico."""
    try:
        from winback import bi_conn
    except Exception as e:  # noqa: BLE001
        return {"error": f"BI no disponible: {e}"}
    sql = """
        SELECT cohorte, COUNT(*) AS env,
               SUM(CASE WHEN agendo_at IS NOT NULL OR cita_atribuida_id IS NOT NULL THEN 1 ELSE 0 END) AS ag
        FROM bi.winback_envios GROUP BY cohorte
    """
    try:
        with bi_conn() as c:
            cur = c.cursor()
            cur.execute(sql)
            rows = cur.fetchall()
    except Exception as e:  # noqa: BLE001
        return {"error": f"query falló: {e}"}

    def _d(coh):
        s = "".join(ch for ch in str(coh) if ch.isdigit())
        return int(s) if s else 9999

    fresh_s = fresh_n = std_s = std_n = 0
    by = {}
    for coh, env, ag in rows:
        env, ag, d = env or 0, ag or 0, _d(coh)
        by[str(coh)] = {"env": env, "ag": ag, "dias": d}
        if d == 9999:
            continue
        if d <= 60:
            fresh_s += ag; fresh_n += env
        else:
            std_s += ag; std_n += env

    stat = _two_prop(fresh_s, fresh_n, std_s, std_n)
    verdict, why = _verdict_from(stat)
    result = {"arms": {"challenger (≤60d)": {"sent": fresh_n, "booked": fresh_s},
                       "champion (>60d)": {"sent": std_n, "booked": std_s}},
              "stat": stat, "why": why, "by_cohort": by}
    if exp_id:
        _save_verdict(exp_id, verdict, result)
    return {"verdict": verdict, **result}


# ─────────────────────────────────────────────────────────────────────────────
# Orquestación: seed del primer experimento + correr evaluaciones
# ─────────────────────────────────────────────────────────────────────────────
def seed() -> None:
    """Registra los experimentos conocidos (idempotente)."""
    propose(
        name="winback_timing_30d",
        policy="WINBACK_DAYS (corte de cohorte)",
        champion="disparar a ≥90 días inactivos",
        challenger="disparar fresco a 30-60 días",
        metric="conversión enviado→agendó",
        hypothesis="los pacientes más frescos reenganchan mejor; adelantar el corte sube la conversión total.",
    )


def run_evaluations() -> dict:
    """Corre los evaluadores retrospectivos y devuelve el estado del ledger."""
    seed()
    exps = {e["name"]: e for e in list_experiments()}
    wb = exps.get("winback_timing_30d")
    if wb:
        evaluate_winback_timing(wb["id"])
    return {"experiments": list_experiments()}


if __name__ == "__main__":
    import sys
    sys.path.insert(0, "app")
    try:
        from dotenv import load_dotenv
        load_dotenv(".env")
    except Exception:  # noqa: BLE001
        pass
    rep = run_evaluations()
    for e in rep["experiments"]:
        print(f"\n[{e['status']}] {e['name']} — {e.get('verdict') or 'sin veredicto'}")
        print(f"  {e['champion']}  vs  {e['challenger']}")
        r = e.get("result") or {}
        if r.get("stat"):
            s = r["stat"]
            print(f"  challenger {s['rate_challenger']}% (n={s['n_challenger']}) vs "
                  f"champion {s['rate_champion']}% (n={s['n_champion']}) · lift {s['lift']}× · p={s['p_value']}")
        if r.get("why"):
            print(f"  → {r['why']}")
