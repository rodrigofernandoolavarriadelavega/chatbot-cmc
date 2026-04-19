"""Stress test: 1000 variantes de RUT generadas combinatoriamente.

Combina:
  - 10 RUTs válidos sintéticos (cuerpo 8 dígitos + DV calculado en runtime)
  - 21 separadores cuerpo-DV (-, _, /, |, :, *, ·, •, dashes Unicode, espacios)
  - 9 prefijos (ninguno, 'rut:', 'RUT:', 'mi rut es', 'ci:', 'cédula:', ...)
  - con/sin puntos de miles
  - DV upper/lower

Muestreo aleatorio de 1000 con semilla fija para reproducibilidad.

Ejecución:
    PYTHONPATH=app:. python3 tests/test_rut_variantes.py

Independiente de Medilink, Claude, WhatsApp.
"""
from __future__ import annotations

import itertools
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT))

from medilink import clean_rut, valid_rut, _calcular_dv_rut


# ── 10 RUTs válidos sintéticos (cuerpo 8 dígitos + DV calculado) ──
_rng = random.Random(42)
BASES: list[tuple[str, str]] = []
while len(BASES) < 10:
    cuerpo = str(_rng.randint(10_000_000, 99_999_999))
    dv = _calcular_dv_rut(cuerpo)
    if dv:
        BASES.append((cuerpo, dv))

# ── 21 separadores ──
SEPARADORES = [
    "-", "_", "/", "|", ":", "*", "·", "•",
    "\u2010",  # hyphen
    "\u2011",  # non-breaking hyphen
    "\u2012",  # figure dash
    "\u2013",  # en dash
    "\u2014",  # em dash
    "\u2015",  # horizontal bar
    "\u2212",  # minus sign
    " ",       # espacio simple
    "  ",      # doble espacio
    " - ",     # guion con espacios
    "- ",
    " -",
    "--",      # doble guion
]

# ── 9 prefijos ──
PREFIJOS = [
    "",
    "rut: ",
    "RUT: ",
    "Rut ",
    "mi rut es ",
    "mi cédula es ",
    "ci: ",
    "cédula: ",
    "cedula ",
]


def _con_puntos(cuerpo: str) -> str:
    """12345678 → 12.345.678"""
    s = cuerpo[::-1]
    parts = [s[i : i + 3] for i in range(0, len(s), 3)]
    return ".".join(p[::-1] for p in parts[::-1])


def _generar_casos(n: int = 1000) -> list[tuple[str, str]]:
    """[(input, expected_canonical), ...]"""
    todos: list[tuple[str, str]] = []
    combos = itertools.product(
        BASES, SEPARADORES, PREFIJOS, [True, False], [True, False]
    )
    for (cuerpo, dv), sep, pre, con_puntos, dv_upper in combos:
        cuerpo_str = _con_puntos(cuerpo) if con_puntos else cuerpo
        dv_str = dv.upper() if dv_upper else dv.lower()
        inp = f"{pre}{cuerpo_str}{sep}{dv_str}"
        expected = f"{cuerpo}-{dv.upper()}"
        todos.append((inp, expected))
    _rng2 = random.Random(42)
    _rng2.shuffle(todos)
    return todos[:n]


def _run(verbose: bool = False) -> int:
    casos = _generar_casos(1000)
    passed = failed = 0
    fails: list[tuple[str, str, str, bool]] = []
    for inp, expected in casos:
        got = clean_rut(inp)
        ok_clean = got == expected
        ok_valid = valid_rut(got)
        if ok_clean and ok_valid:
            passed += 1
        else:
            failed += 1
            if len(fails) < 30:
                fails.append((inp, expected, got, ok_valid))
    if verbose and fails:
        print("── Primeros 30 fails ──")
        for inp, exp, got, v in fails:
            print(f"  input={inp!r:45s} expected={exp!r:15s} got={got!r:20s} valid={v}")
    print(f"\n── Total: {passed}/{passed + failed} passed, {failed} failed ──")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run(verbose="-v" in sys.argv) else 0)
