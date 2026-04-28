"""
Fuzzer del handler. Genera N strings random con patrones adversariales
(unicode raro, longitud variable, signos puros, números puros, control chars,
etc.) y los pasa por handle_message(). Reporta excepciones y violaciones de
GLOBAL_ASSERTS.

Uso:
    python scripts/fuzz_handler.py --n 500
"""
import argparse
import asyncio
import random
import string
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from adversarial_chat import install_mocks, GLOBAL_ASSERTS, _extract_text


def gen_random_string() -> str:
    """Genera un string adversarial random."""
    kind = random.choice([
        "ascii_short", "ascii_long", "unicode", "emoji",
        "signs", "numbers", "mixed", "control", "rural",
        "url", "rut", "zalgo", "spaces",
    ])
    if kind == "ascii_short":
        return "".join(random.choices(string.ascii_lowercase + " ", k=random.randint(2, 30)))
    if kind == "ascii_long":
        return "".join(random.choices(string.ascii_lowercase + " ", k=random.randint(100, 1500)))
    if kind == "unicode":
        chars = "áéíóúñ¿¡—–áéíóúüçÇ"
        return "".join(random.choices(chars + string.ascii_lowercase + " ", k=random.randint(10, 80)))
    if kind == "emoji":
        return "".join(random.choices("🌟🎉🔥💯😀😎🤔🙏👍👎❤️😢😡✅❌", k=random.randint(1, 30)))
    if kind == "signs":
        return "".join(random.choices("?!¿¡.,;:()[]{}*-_", k=random.randint(1, 20)))
    if kind == "numbers":
        return "".join(random.choices(string.digits, k=random.randint(1, 20)))
    if kind == "mixed":
        all_chars = string.ascii_letters + string.digits + " ?!¿áéí🌟"
        return "".join(random.choices(all_chars, k=random.randint(5, 100)))
    if kind == "control":
        # Caracteres de control que pueden romper parsing
        return "".join(random.choices("\t\n\r\x00\x01\x1b\u200b\u200c\u202e", k=random.randint(1, 10)))
    if kind == "rural":
        words = ["tngo", "hr", "kine", "doc", "abrca", "mañna", "manaa", "queria",
                 "agendaria", "psicolog", "porfa", "nesecito", "horita", "qiero",
                 "podri", "cnsulta", "kn", "xq", "tqm"]
        return " ".join(random.choices(words, k=random.randint(2, 8)))
    if kind == "url":
        return random.choice([
            "https://centromedicocarampangue.cl/x",
            "http://malware.test/evil",
            "agentecmc.cl/test",
            "ftp://localhost",
        ])
    if kind == "rut":
        n = random.randint(1, 99999999)
        d = random.choice("0123456789Kk")
        sep = random.choice([".", "-", " ", ""])
        return f"{n}{sep}{d}"
    if kind == "zalgo":
        base = "hola"
        return "".join(c + "".join(chr(random.randint(0x300, 0x36F)) for _ in range(random.randint(1, 4))) for c in base)
    if kind == "spaces":
        return " " * random.randint(1, 20) + (random.choice(["hola", "agendar", ""]))
    return ""


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    random.seed(args.seed)
    install_mocks()

    from session import get_session, reset_session
    from flows import handle_message

    print(f"\n=== Fuzz {args.n} mensajes random (seed={args.seed}) ===\n")
    exceptions = []
    violations = []
    for i in range(args.n):
        msg = gen_random_string()
        phone = f"56999{i:06d}"
        reset_session(phone)
        sess = get_session(phone)
        try:
            resp = await handle_message(phone, msg, sess)
        except Exception as e:
            exceptions.append((i, msg[:80], str(e)))
            continue
        resp_text = _extract_text(resp)
        for ga in GLOBAL_ASSERTS:
            err = ga(resp_text)
            if err:
                violations.append((i, msg[:80], err, resp_text[:120]))

    print(f"Mensajes: {args.n}")
    print(f"Excepciones: {len(exceptions)}")
    print(f"Violaciones: {len(violations)}")

    if exceptions:
        print("\n● Excepciones:")
        for i, m, e in exceptions[:15]:
            print(f"  [{i}] msg={m!r}")
            print(f"       err={e}")

    if violations:
        print("\n● Violaciones:")
        for i, m, e, snip in violations[:15]:
            print(f"  [{i}] msg={m!r}")
            print(f"       {e}")
            print(f"       resp={snip!r}")

    return 1 if (exceptions or violations) else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
