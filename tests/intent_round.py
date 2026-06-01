"""
Round-runner: carga un corpus JSONL, lo pasa por el probe offline y emite un
reporte categorizado + vuelca los subconjuntos WRONG y CLAUDE para iterar.

Uso:
    PYTHONPATH=app:. venv/bin/python tests/intent_round.py tests/corpus_intent.jsonl

Escribe:
    /tmp/intent_wrong.jsonl   — resolvió offline al intent equivocado (BUGS)
    /tmp/intent_claude.jsonl  — cayó a Claude (oportunidad de promoción offline)
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT))

from offline_intent_probe import run_cases  # noqa: E402


def main():
    path = sys.argv[1]
    cases = [json.loads(l) for l in Path(path).read_text(encoding="utf-8").splitlines() if l.strip()]

    # 'sintoma' se mapea: no esperamos resolución offline; el verdict aceptable es claude.
    # Para el probe, lo tratamos como intent=None (no exigimos intent) salvo que sea otra cosa.
    for c in cases:
        if c.get("intent") == "sintoma":
            c["_expect_claude_ok"] = True
            c["intent"] = None  # no exigimos un intent offline para síntomas

    res = run_cases(cases)
    total = len(cases)
    p, cl, wr, er = len(res["pass"]), len(res["claude"]), len(res["wrong"]), len(res["error"])
    print(f"\n{'='*64}")
    print(f" CORPUS {Path(path).name}: {total} casos")
    print(f"  offline_ok = {p:4}  ({100*p/total:.0f}%)")
    print(f"  claude     = {cl:4}  ({100*cl/total:.0f}%)  ← fallthrough")
    print(f"  WRONG      = {wr:4}  ({100*wr/total:.0f}%)  ← BUGS offline")
    print(f"  error      = {er:4}")
    print(f"{'='*64}")

    # Desglose de WRONG por (want_intent → got_intent)
    if res["wrong"]:
        cnt = Counter((r.get("intent"), r["got_intent"]) for r in res["wrong"])
        print("\n WRONG por transición (want → got):")
        for (wi, gi), n in cnt.most_common():
            print(f"   {str(wi):16} → {str(gi):16}  x{n}")

    # Desglose CLAUDE por grupo
    if res["claude"]:
        cnt = Counter(r.get("group", "?") for r in res["claude"])
        print("\n CLAUDE fallthrough por grupo:")
        for g, n in cnt.most_common():
            print(f"   {g:20} x{n}")

    Path("/tmp/intent_wrong.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in res["wrong"]), encoding="utf-8")
    Path("/tmp/intent_claude.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in res["claude"]), encoding="utf-8")
    if res["error"]:
        Path("/tmp/intent_error.jsonl").write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in res["error"]), encoding="utf-8")
        print("\n ERRORES:")
        for r in res["error"][:20]:
            print(f"   {r['input']!r}: {r['got_intent']} {r['got_esp']}")

    print(f"\n → /tmp/intent_wrong.jsonl ({wr})   /tmp/intent_claude.jsonl ({cl})")
    sys.exit(1 if (wr or er) else 0)


if __name__ == "__main__":
    main()
