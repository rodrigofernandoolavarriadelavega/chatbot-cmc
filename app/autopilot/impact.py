"""P&L agéntico — el scoreboard de qué hizo la capa autónoma y cuánto rindió.

Responde la pregunta que faltaba: "¿qué hizo el sistema este período y cuánto
ganó/costó?". Sin esto, activar cualquier agente es a ciegas. Es el instrumento que
da permiso de encender más — y el combustible que el Optimizador necesita.

Agrega los outcomes REALES que ya existen, por canal:
  • Win-back (WhatsApp):  bi.winback_envios — envíos, citas, value_clp atribuido.
  • Meta Ads:             data/cac_snapshot.json — spend, atendidos, ingreso (cron).
  • Email:                email_envios (sessions.db) — envíos, clics (gated, ~0 hoy).

READ-ONLY. La atribución no es perfecta (parte de los pacientes llaman al fijo y no
quedan trazados); por eso cada línea marca su método y los totales son un piso, no
una verdad contable. Honesto por diseño.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger("bot")

WA_MSG_COST_CLP = int(os.getenv("WA_MSG_COST_CLP", "50"))   # costo aprox. de un template marketing


def _cac_snapshot_path() -> Path:
    """Ubica cac_snapshot.json de forma robusta (deployado o corrido suelto)."""
    candidates = [
        Path(__file__).parent.parent.parent / "data" / "cac_snapshot.json",  # app/autopilot → repo/data
        Path.cwd() / "data" / "cac_snapshot.json",                            # cwd/data
        Path(os.getenv("CAC_SNAPSHOT_PATH", "")) if os.getenv("CAC_SNAPSHOT_PATH") else None,
    ]
    for p in candidates:
        if p and p.exists():
            return p
    return candidates[0]


def _bi():
    try:
        from winback import bi_conn
        return bi_conn
    except Exception as e:  # noqa: BLE001
        log.warning("impact: BI no disponible (%s)", e)
        return None


def _row(channel, actions, bookings, revenue, cost, method, note=""):
    net = revenue - cost
    roas = round(revenue / cost, 2) if cost > 0 else None
    return {"channel": channel, "actions": actions, "bookings": bookings,
            "revenue_clp": int(revenue), "cost_clp": int(cost), "net_clp": int(net),
            "roas": roas, "method": method, "note": note}


# ─────────────────────────────────────────────────────────────────────────────
# Canales
# ─────────────────────────────────────────────────────────────────────────────
def _winback(days: int) -> dict | None:
    """Win-back REAL. Ingreso desde la CAJA REAL (bi_pagos_caja por id_paciente, pago ≥
    envío) — el MISMO método que Meta Ads (cac_report). NO usa value_clp (valor esperado
    por envío, no ganado) ni el BI fact_ingresos (viejo). Delega en winback_report."""
    try:
        from . import winback_report
        rep = winback_report.report(days=days)
        if rep.get("error"):
            return None
        t = rep["totals"]
    except Exception as e:  # noqa: BLE001
        log.warning("impact winback: %s", e)
        return None
    env = t.get("enviados", 0)
    cost = env * WA_MSG_COST_CLP
    return _row("Win-back (WhatsApp)", env, t.get("agendaron", 0), t.get("ingreso", 0), cost,
                method="bi.winback_envios → bi_pagos_caja (caja real, por id_paciente)",
                note=f"{t.get('agendaron',0)} agendaron, {t.get('pagaron',0)} pagaron. "
                     f"Ingreso = caja real (no value_clp).")


def _email(days: int) -> dict | None:
    try:
        from session import db as _conn
        with _conn() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS email_envios (id INTEGER PRIMARY KEY)""")  # noop si ya existe
            row = conn.execute(
                "SELECT COUNT(*) AS env, "
                "SUM(CASE WHEN clicked_at IS NOT NULL THEN 1 ELSE 0 END) AS clk "
                "FROM email_envios WHERE status='sent' "
                "AND sent_at >= datetime('now', ?)", (f"-{int(days)} days",)).fetchone()
    except Exception as e:  # noqa: BLE001
        log.warning("impact email: %s", e)
        return _row("Email", 0, 0, 0, 0, method="email_envios", note="canal gated / sin envíos.")
    env = (row["env"] if row else 0) or 0
    clk = (row["clk"] if row else 0) or 0
    return _row("Email", env, clk, 0, 0, method="email_envios (clics, sin atribución de ingreso aún)",
                note="canal gated; revenue se atará vía /e/c → cita." if env == 0 else "")


def _meta_ads() -> dict | None:
    try:
        d = json.loads(_cac_snapshot_path().read_text())
    except Exception as e:  # noqa: BLE001
        log.warning("impact meta: %s", e)
        return None
    f = d.get("funnel") or {}
    spend = int(f.get("spend_total") or 0)
    ingreso = int(f.get("ingreso") or 0)
    atendidos = int(f.get("atendidos") or 0)
    agendaron = int(f.get("agendaron") or 0)
    return _row("Meta Ads", agendaron, atendidos, ingreso, spend,
                method="cac_snapshot.json · funnel (cron 00:20 CLT)",
                note="atribución parcial: parte de los pacientes llaman al fijo y no se trazan.")


# ─────────────────────────────────────────────────────────────────────────────
# P&L consolidado
# ─────────────────────────────────────────────────────────────────────────────
def pnl(days: int = 90) -> dict:
    """Scoreboard unificado por canal + totales. Read-only."""
    channels = [c for c in (_winback(days), _email(days), _meta_ads()) if c]
    tot_rev = sum(c["revenue_clp"] for c in channels)
    tot_cost = sum(c["cost_clp"] for c in channels)
    tot_book = sum(c["bookings"] for c in channels)
    tot_act = sum(c["actions"] for c in channels)
    return {
        "period_days": days,
        "channels": channels,
        "totals": {
            "actions": tot_act, "bookings": tot_book,
            "revenue_clp": tot_rev, "cost_clp": tot_cost, "net_clp": tot_rev - tot_cost,
            "blended_roas": round(tot_rev / tot_cost, 2) if tot_cost > 0 else None,
        },
        "caveat": "Cifras = piso atribuible. La atribución no captura pacientes que "
                  "llaman al fijo ni el LTV/recurrencia. Útil para comparar canales, "
                  "no como contabilidad oficial.",
    }


if __name__ == "__main__":
    import sys
    sys.path.insert(0, "app")
    try:
        from dotenv import load_dotenv
        load_dotenv(".env")
    except Exception:  # noqa: BLE001
        pass
    rep = pnl(90)
    print(f"\n-- P&L agentico - ultimos {rep['period_days']} dias --\n")
    print(f"{'CANAL':<22}{'acciones':>9}{'citas':>7}{'ingreso':>13}{'costo':>11}{'neto':>13}{'roas':>7}")
    print("-" * 84)
    for c in rep["channels"]:
        roas = f"{c['roas']}x" if c['roas'] else "-"
        print(f"{c['channel']:<22}{c['actions']:>9,}{c['bookings']:>7,}"
              f"{c['revenue_clp']:>13,}{c['cost_clp']:>11,}{c['net_clp']:>13,}{roas:>7}")
    t = rep["totals"]
    print("-" * 84)
    roas = f"{t['blended_roas']}x" if t['blended_roas'] else "-"
    print(f"{'TOTAL':<22}{t['actions']:>9,}{t['bookings']:>7,}"
          f"{t['revenue_clp']:>13,}{t['cost_clp']:>11,}{t['net_clp']:>13,}{roas:>7}")
    print(f"\n{rep['caveat']}")
