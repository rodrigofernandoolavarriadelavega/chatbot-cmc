"""
Test de robustez de detección de intención (typos / variaciones / fonética rural).

Corre 1459 casos adversariales (corpus_intent_golden.jsonl) por el detect_intent
REAL con el cliente Claude stubbeado (cero costo API) y exige que NINGUNO resuelva
OFFLINE a la intención equivocada (WRONG=0). Los fallthroughs a Claude son legítimos
(el LLM los resuelve); lo que este test bloquea es un MISROUTE offline confiado.

Origen: sprint 2026-05-31. Bugs que este test pinea para que no regresen:
  - eco-bleed: cualquier parte del cuerpo ("rodilla","pie","dientes") enrutaba a
    ecografía. Fix = gate de contexto ecográfico en app/ecografias.py.
  - cancel-over-match: preguntas de política ("se puede cancelar?") y negaciones
    ("ya no kiero cancelar") disparaban el flujo de anulación. Fix = _CANCELAR_INFO_RE
    extendido + _CANCEL_NEGADO_RE + _CANCEL_AMBIGUO_RE en app/claude_helper.py.

Ejecución:
    PYTHONPATH=app:. venv/bin/python tests/test_intent_robustness.py

No depende de red, Claude, Medilink ni del servicio GES.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT))

from offline_intent_probe import run_cases  # noqa: E402

GOLDEN = Path(__file__).resolve().parent / "corpus_intent_golden.jsonl"
# Piso de cobertura offline (a la baja = posible regresión de prefilters/cache).
MIN_OFFLINE = 190


def main():
    cases = [json.loads(l) for l in GOLDEN.read_text(encoding="utf-8").splitlines() if l.strip()]
    # 'sintoma' no exige resolución offline (lo decide Claude/GES).
    for c in cases:
        if c.get("intent") == "sintoma":
            c["intent"] = None

    res = run_cases(cases)
    p, cl, wr, er = (len(res[k]) for k in ("pass", "claude", "wrong", "error"))
    total = len(cases)
    print(f"\n── Intent robustness: {total} casos ──")
    print(f"   offline_ok={p}  claude={cl}  WRONG={wr}  error={er}")

    failed = False
    if wr:
        failed = True
        print(f"\n❌ {wr} MISROUTES offline (deberían ser 0):")
        for r in res["wrong"]:
            print(f"   [{r.get('intent')}→{r['got_intent']}] {r['input']!r} "
                  f"(esp want={r.get('especialidad')} got={r['got_esp']})")
    if er:
        failed = True
        print(f"\n❌ {er} ERRORES:")
        for r in res["error"]:
            print(f"   {r['input']!r}: {r['got_intent']} {r['got_esp']}")
    if p < MIN_OFFLINE:
        failed = True
        print(f"\n❌ cobertura offline {p} < piso {MIN_OFFLINE} (posible regresión de prefilters/cache)")

    if failed:
        print("\n── FALLÓ ──")
        sys.exit(1)
    print(f"\n── OK: WRONG=0, cobertura offline={p} (≥{MIN_OFFLINE}) ──")


if __name__ == "__main__":
    main()
