"""Runner consolidado de las suites Alma — corre las 3 y reporta el total.

Uso:
    python3 tests/run_alma_suite.py

Cubre: Fase 4 operativa (test_alma_operativa), cerebro/política (test_alma_brain),
chasis de orquestadores (test_alma_orchestrators). Cada suite es offline (sin red).
"""
import os
import subprocess
import sys

_SUITES = [
    "test_alma_operativa.py",
    "test_alma_brain.py",
    "test_alma_orchestrators.py",
    "test_alma_orq_integration.py",
]


def main() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    todo_ok = True
    print("Corriendo suites Alma...\n")
    for s in _SUITES:
        r = subprocess.run([sys.executable, os.path.join(here, s)],
                           capture_output=True, text=True)
        last = (r.stdout.strip().splitlines() or ["(sin salida)"])[-1]
        ok = r.returncode == 0
        todo_ok = todo_ok and ok
        print(f"  {'✅' if ok else '❌'}  {s:30s} {last}")
        if not ok and r.stderr.strip():
            print("     " + r.stderr.strip().splitlines()[-1])
    print("\n" + (f"✅ TODO VERDE — las {len(_SUITES)} suites Alma pasan" if todo_ok else "❌ HAY FALLOS"))
    return 0 if todo_ok else 1


if __name__ == "__main__":
    sys.exit(main())
