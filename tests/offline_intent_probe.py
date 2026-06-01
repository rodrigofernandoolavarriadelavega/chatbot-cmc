"""
Offline intent probe — clasifica cómo resuelve detect_intent() un texto SIN
gastar una sola llamada a Claude.

Mecanismo: monkeypatch del cliente global `claude_helper.client.messages.create`
para que lance un sentinel `_ReachedClaude`. Así, ejecutar detect_intent() sobre
un texto produce uno de tres veredictos:

  - "offline:<intent>[/<especialidad>]"  → resolvió por prefilter/regex/cache,
                                            NUNCA tocó la red. Esto es lo barato.
  - "claude"                             → cayó al LLM (lo capturamos antes del
                                            request real). Funciona en prod pero
                                            cuesta plata + latencia.

Uso programático:
    from offline_intent_probe import probe, probe_many
    v = probe("kiero hora con el kinesiologo")   # -> ("offline", "agendar", "kinesiología")

Uso CLI (lee JSONL de casos por stdin o archivo):
    PYTHONPATH=app:. venv/bin/python tests/offline_intent_probe.py casos.jsonl

Cada caso JSONL: {"input": "...", "intent": "agendar", "especialidad": "kinesiología"}
(especialidad opcional). Imprime resumen + lista de WRONG y CLAUDE.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT))

import claude_helper  # noqa: E402


class _ReachedClaude(BaseException):
    """Sentinel: detect_intent llegó a la llamada LLM.

    Hereda de BaseException (no Exception) a propósito: detect_intent envuelve la
    llamada a Claude en `try/except Exception` y devolvería un fallback heurístico,
    lo que enmascararía el fallthrough como si fuera una resolución offline.
    """


class _StubMessages:
    async def create(self, *a, **k):  # noqa: ANN001
        raise _ReachedClaude()


class _StubClient:
    messages = _StubMessages()


# Reemplaza el cliente global. Cualquier `await client.messages.create(...)`
# dentro de detect_intent lanza _ReachedClaude antes de tocar la red.
claude_helper.client = _StubClient()


async def _probe_async(text: str):
    try:
        res = await claude_helper.detect_intent(text)
    except _ReachedClaude:
        return ("claude", None, None)
    except Exception as e:  # noqa: BLE001
        return ("error", type(e).__name__, str(e)[:120])
    if not isinstance(res, dict):
        return ("error", "NonDict", repr(res)[:120])
    return ("offline", res.get("intent"), res.get("especialidad"))


def probe(text: str):
    """Devuelve (veredicto, intent, especialidad) para un texto."""
    return asyncio.run(_probe_async(text))


async def _probe_many_async(texts: list[str]):
    return [await _probe_async(t) for t in texts]


def probe_many(texts: list[str]):
    return asyncio.run(_probe_many_async(texts))


def _norm_esp(e):
    if e is None:
        return None
    return str(e).strip().lower()


# Clases de equivalencia de intención en el vocabulario real del bot:
#   - "faq" e "info" son el mismo camino (respuesta_faq): respuesta informativa.
#   - "menu" responde con el menú; un cierre corto ("gracias") cae a info/faq con
#     mensaje de cierre — funcionalmente equivalente a un no-op cordial.
_INTENT_EQUIV = {
    "info": "info", "faq": "info",
}
# Intenciones cuyo flujo NO usa especialidad (no la pedimos al verificar).
_INTENT_SIN_ESP = {"cancelar", "humano", "menu", "ver_reservas", "consulta_farmaco"}


def _intent_eq(a, b) -> bool:
    if a is None or b is None:
        return a == b
    return _INTENT_EQUIV.get(a, a) == _INTENT_EQUIV.get(b, b)


def run_cases(cases: list[dict]) -> dict:
    """Ejecuta casos y clasifica. Devuelve dict con listas pass/claude/wrong/error."""
    out = {"pass": [], "claude": [], "wrong": [], "error": []}
    async def _run():
        for c in cases:
            text = c["input"]
            want_intent = c.get("intent")
            want_esp = _norm_esp(c.get("especialidad"))
            verdict, got_intent, got_esp = await _probe_async(text)
            got_esp_n = _norm_esp(got_esp)
            row = {**c, "verdict": verdict, "got_intent": got_intent, "got_esp": got_esp}
            if verdict == "error":
                out["error"].append(row)
            elif verdict == "claude":
                out["claude"].append(row)
            else:  # offline
                intent_ok = (want_intent is None) or _intent_eq(got_intent, want_intent)
                # No exigimos especialidad si el flujo del intent resuelto no la usa.
                if got_intent in _INTENT_SIN_ESP:
                    esp_ok = True
                else:
                    esp_ok = (want_esp is None) or (got_esp_n == want_esp)
                if intent_ok and esp_ok:
                    out["pass"].append(row)
                else:
                    out["wrong"].append(row)
    asyncio.run(_run())
    return out


def _load(path_or_stdin):
    cases = []
    if path_or_stdin == "-" or path_or_stdin is None:
        lines = sys.stdin
    else:
        lines = Path(path_or_stdin).read_text(encoding="utf-8").splitlines()
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        cases.append(json.loads(ln))
    return cases


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "-"
    cases = _load(path)
    res = run_cases(cases)
    total = len(cases)
    p, cl, wr, er = len(res["pass"]), len(res["claude"]), len(res["wrong"]), len(res["error"])
    print(f"\n== {total} casos ==  offline_ok={p}  claude={cl}  WRONG={wr}  error={er}")
    if res["wrong"]:
        print("\n-- WRONG (resolvió offline al intent equivocado — BUG) --")
        for r in res["wrong"]:
            print(f"  {r['input']!r:55} want={r.get('intent')}/{r.get('especialidad')} "
                  f"got={r['got_intent']}/{r['got_esp']}")
    if res["error"]:
        print("\n-- ERROR --")
        for r in res["error"]:
            print(f"  {r['input']!r:55} {r['got_intent']}: {r['got_esp']}")
    if res["claude"]:
        print(f"\n-- CLAUDE fallthrough ({cl}) — funciona pero paga LLM --")
        for r in res["claude"][:60]:
            print(f"  {r['input']!r:55} (esperado {r.get('intent')}/{r.get('especialidad')})")
        if cl > 60:
            print(f"  ... +{cl-60} más")
    # Exit code: != 0 si hay WRONG o error (los CLAUDE no fallan el build)
    sys.exit(1 if (wr or er) else 0)


if __name__ == "__main__":
    main()
