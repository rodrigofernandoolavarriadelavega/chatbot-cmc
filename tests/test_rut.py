"""Tests de clean_rut y valid_rut en app/medilink.py.

Cubre casos reales vistos en WhatsApp: separadores atípicos (_ por -),
con/sin puntos, con/sin DV, con prefijo 'rut:', etc.

Ejecución:
    PYTHONPATH=app:. venv/bin/python tests/test_rut.py

No depende de Medilink, Claude, WhatsApp.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT))

from medilink import clean_rut, valid_rut


# (id, input, expected clean_rut, expected valid_rut)
CASES: list[tuple] = [
    # ── Separador guión bajo (bug 2026-04-19 Natalia Saez) ──
    ("USC-01", "21907308_9",        "21907308-9", True),
    ("USC-02", "21.907.308_9",      "21907308-9", True),
    ("USC-03", "12345678_5",        "12345678-5", True),

    # ── Separador estándar ──
    ("DASH-01", "12.345.678-5",     "12345678-5", True),
    ("DASH-02", "12345678-5",       "12345678-5", True),
    ("DASH-03", "10.000.013-K",     "10000013-K", True),  # DV=K real

    # ── Sin guión, DV pegado ──
    ("NODASH-01", "123456785",      "12345678-5", True),
    ("NODASH-02", "21907308 9",     "21907308-9", True),
    ("NODASH-03", "21.907.308 9",   "21907308-9", True),

    # ── Separadores poco comunes ──
    ("SEP-01", "12345678/5",        "12345678-5", True),
    ("SEP-02", "12345678|5",        "12345678-5", True),
    ("SEP-03", "12345678·5",        "12345678-5", True),

    # ── Prefijo ──
    ("PREF-01", "rut: 12.345.678-5", "12345678-5", True),
    ("PREF-02", "mi rut es 12345678-5", "12345678-5", True),

    # ── RUT inválido (DV incorrecto) ──
    ("INV-01", "12345678-1",        "12345678-1", False),
    ("INV-02", "21907308-0",        "21907308-0", False),  # DV real es 9

    # ── Demasiado corto/largo ──
    ("LEN-01", "1234-5",             None,         False),
    ("LEN-02", "123456789012-3",     None,         False),
]


def _run() -> tuple[int, int]:
    passed = failed = 0
    for case in CASES:
        cid, inp, expected_clean, expected_valid = case
        got_clean = clean_rut(inp)
        got_valid = valid_rut(got_clean) if got_clean else valid_rut(inp)

        clean_ok = (expected_clean is None) or (got_clean == expected_clean)
        valid_ok = got_valid == expected_valid

        if clean_ok and valid_ok:
            print(f"✅ {cid:10s} {inp!r:35s} → clean={got_clean!r:15s} valid={got_valid}")
            passed += 1
        else:
            print(f"❌ {cid:10s} {inp!r:35s}")
            if not clean_ok:
                print(f"     clean: esperado {expected_clean!r} got {got_clean!r}")
            if not valid_ok:
                print(f"     valid: esperado {expected_valid} got {got_valid}")
            failed += 1
    return passed, failed


if __name__ == "__main__":
    p, f = _run()
    print(f"\n── Total: {p}/{p+f} passed, {f} failed ──")
    sys.exit(1 if f else 0)
