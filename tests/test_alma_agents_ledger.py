"""Tests del Effectiveness Ledger de la flota — sin red, DB temporal aislada.

Verifica el contrato de atribución: una acción de contacto se acredita una cita
si el paciente agendó/confirmó/respondió DESPUÉS del contacto y DENTRO de la
ventana; si la ventana vence sin señal → sin_respuesta; si sigue abierta →
pendiente. Corre: `python3 tests/test_alma_agents_ledger.py`
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import session  # noqa: E402

# DB temporal aislada ANTES de cualquier conexión.
_TMP = tempfile.mkdtemp(prefix="ledger_test_")
session.DB_PATH = Path(_TMP) / "sessions.db"

from alma_agents import ledger  # noqa: E402

_OK = 0
_FAIL = 0


def check(cond, label):
    global _OK, _FAIL
    if cond:
        _OK += 1
        print(f"OK  {label}")
    else:
        _FAIL += 1
        print(f"XX  FALLA: {label}")


def _db_ts(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _seed_event(phone, event, ts):
    conn = session._conn()
    with conn:
        conn.execute("INSERT INTO conversation_events (phone, event, ts) VALUES (?,?,?)",
                     (phone, event, _db_ts(ts)))
    conn.close()


def _seed_inbound(phone, ts):
    conn = session._conn()
    with conn:
        conn.execute("INSERT INTO messages (phone, direction, text, ts) VALUES (?,?,?,?)",
                     (phone, "in", "hola", _db_ts(ts)))
    conn.close()


def _run(agent_id, actions, acted_at):
    return {"agent_id": agent_id, "ran_at": acted_at.isoformat(),
            "actions_executed": actions}


def _contact(target, kind="winback", staff=False):
    return {"kind": kind, "summary": f"contacto {target}", "risk": "alto",
            "target": target, "requires_contact": True, "is_staff": staff}


def main():
    T0 = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)  # contacto base
    NOW = T0 + timedelta(days=10)  # ventana (7d) ya vencida para T0

    # Agente A contacta 3 pacientes; agente B contacta 1.
    ledger.record_result(_run("winback", [
        _contact("56900000001"),  # → agendará (cita)
        _contact("56900000002"),  # → solo responde (respuesta)
        _contact("56900000003"),  # → nada (sin_respuesta, ventana vencida)
        _contact("+569 0000 0004", staff=True),  # staff → NO entra al ledger
    ], T0))
    ledger.record_result(_run("yield_agenda", [
        _contact("56900000005"),  # contacto reciente, ventana abierta → pendiente
    ], NOW - timedelta(days=2)))

    # Señales reales DESPUÉS del contacto, dentro de ventana
    _seed_event("56900000001", "cita_confirmada", T0 + timedelta(days=1))
    _seed_inbound("56900000002", T0 + timedelta(days=2))
    # 56900000003: nada
    # 56900000005: nada todavía (ventana abierta)

    # Una señal ANTES del contacto no debe contar (anti-falsa-atribución)
    _seed_event("56900000003", "cita_confirmada", T0 - timedelta(days=3))

    res = ledger.measure_outcomes(now_iso=NOW.isoformat())
    check(res["cita"] == 1, f"1 cita resuelta (got {res['cita']})")
    check(res["respuesta"] == 1, f"1 respuesta resuelta (got {res['respuesta']})")
    check(res["sin_respuesta"] == 1, f"1 sin_respuesta (got {res['sin_respuesta']})")

    summ = ledger.effectiveness_summary()
    by = {a["agent_id"]: a for a in summ["per_agent"]}
    check("winback" in by, "winback aparece en el resumen")
    w = by.get("winback", {})
    check(w.get("contactos") == 3, f"winback 3 contactos (staff excluido) (got {w.get('contactos')})")
    check(w.get("citas") == 1, f"winback 1 cita (got {w.get('citas')})")
    check(w.get("respuestas") == 1, f"winback 1 respuesta (got {w.get('respuestas')})")
    check(w.get("sin_respuesta") == 1, f"winback 1 sin_respuesta (got {w.get('sin_respuesta')})")
    check(w.get("conversion") == round(1 / 3, 3), f"winback conversión 1/3 (got {w.get('conversion')})")
    check(w.get("ingreso_recuperable_clp") == ledger.AVG_TICKET_CLP,
          "winback ingreso recuperable = 1 ticket")

    y = by.get("yield_agenda", {})
    check(y.get("pendientes") == 1, f"yield_agenda 1 pendiente (ventana abierta) (got {y.get('pendientes')})")
    check(y.get("citas") == 0, "yield_agenda 0 citas todavía")

    # La cita anterior al contacto NO se atribuyó (56900000003 quedó sin_respuesta)
    check(w.get("citas") == 1, "señal previa al contacto NO infla las citas")

    # Idempotencia: medir de nuevo no cambia nada
    res2 = ledger.measure_outcomes(now_iso=NOW.isoformat())
    check(res2["revisadas"] == 0, "measure_outcomes idempotente (0 re-revisadas)")

    print(f"\n{_OK} OK · {_FAIL} FALLAS")
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()
