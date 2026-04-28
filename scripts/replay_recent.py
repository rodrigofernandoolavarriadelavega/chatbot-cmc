"""
Replay de mensajes inbound REALES contra el handler actual del bot.

Carga los últimos N mensajes de pacientes de sessions.db de producción
(vía SQLCipher), ejecuta `handle_message()` con sesión vacía y mocks
deterministas de Medilink/Claude, y aplica las propiedades globales.

Cualquier excepción o violación = potencial regresión vs el comportamiento
de producción al momento del mensaje.

Uso (en server):
    cd /opt/chatbot-cmc
    venv/bin/python3 scripts/replay_recent.py --limit 100
"""
import argparse
import asyncio
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT))

# Importar adversarial_chat para reusar mocks + asserts
sys.path.insert(0, str(ROOT / "scripts"))
from adversarial_chat import install_mocks, GLOBAL_ASSERTS, _extract_text


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    install_mocks()

    from session import _conn, get_session, reset_session
    from flows import handle_message

    with _conn() as cn:
        rows = cn.execute(
            """
            SELECT phone, ts, text FROM messages
             WHERE direction = 'in'
               AND text IS NOT NULL
               AND length(text) > 0
             ORDER BY ts DESC
             LIMIT ?
            """,
            (args.limit,),
        ).fetchall()

    print(f"\n=== Replay de {len(rows)} mensajes inbound recientes ===\n")

    fails = []
    exceptions = []
    for i, (phone, ts, text) in enumerate(rows):
        # Phone único por test para evitar contaminar sesiones
        test_phone = f"56999{i:06d}"
        reset_session(test_phone)
        sess = get_session(test_phone)
        try:
            resp = await handle_message(test_phone, text, sess)
        except Exception as e:
            exceptions.append((ts, phone[-4:], text[:80], str(e), traceback.format_exc()))
            continue

        resp_text = _extract_text(resp)
        for ga in GLOBAL_ASSERTS:
            err = ga(resp_text)
            if err:
                fails.append((ts, phone[-4:], text[:80], err, resp_text[:160]))

        if args.verbose and i < 20:
            print(f"  [{i+1}] {ts} {phone[-4:]}: {text[:60]!r}")
            print(f"      → {resp_text[:120]!r}")

    print(f"\n=== Resultados ===")
    print(f"Mensajes procesados: {len(rows)}")
    print(f"Excepciones: {len(exceptions)}")
    print(f"Violaciones de propiedades: {len(fails)}")

    if exceptions:
        print(f"\n● {len(exceptions)} excepciones:")
        for ts, ph, txt, err, tb in exceptions[:10]:
            print(f"  · {ts}  {ph}  '{txt}'")
            print(f"    → {err}")

    if fails:
        print(f"\n● {len(fails)} violaciones:")
        for ts, ph, txt, err, snip in fails[:10]:
            print(f"  · {ts}  {ph}  '{txt}'")
            print(f"    → {err}")
            print(f"    resp: '{snip}'")

    return 1 if (exceptions or fails) else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
