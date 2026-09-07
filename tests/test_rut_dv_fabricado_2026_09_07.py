"""El bot NO puede fabricar un dígito verificador.

Contexto (auditoría 2026-09-07 sobre 30 días de producción): `clean_rut`
tomaba cualquier secuencia de 8 dígitos sin DV explícito como CUERPO y le
derivaba un DV. Pero un paciente que escribe su RUT con espacios
('9 224 066 5' = 9.224.066-5) o pegado ('80875541' = 8.087.554-1) ya incluyó
el DV en esos 8 dígitos. El bot devolvía 92240665-0 / 80875541-6 — RUT de
nadie — y como el resultado ERA self-consistent (`valid_rut` daba True) el
error nunca se veía: no había mensaje de rechazo, el flujo seguía, no
encontraba al paciente en Medilink y se iba a registro creando ficha basura.

Medido en prod: 6 casos en 30 días, 5 de ellos con lectura módulo 11 correcta
disponible y descartada.

Regla que fijan estos tests:
  1. Si el último dígito de 8 hace que los 7 anteriores validen módulo 11,
     ESA es la lectura (el paciente escribió RUT completo).
  2. Si no valida, el cuerpo son los 8 dígitos y el DV se DERIVA — módulo 11
     es determinista dado el cuerpo, así que derivar no es inventar.
  3. Un DV tecleado mal (J por K, l por 1) sólo se corrige si la corrección
     valida. Si no, se rechaza y el bot vuelve a pedirlo — nunca se pisa en
     silencio lo que el paciente escribió.

Ejecución:
    PYTHONPATH=app:. venv/bin/python tests/test_rut_dv_fabricado_2026_09_07.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT))

from medilink import clean_rut, valid_rut, _calcular_dv_rut  # noqa: E402


# (id, entrada real de WhatsApp, salida esperada)
CASES: list[tuple[str, str, str]] = [
    # ── Los 6 casos reales de prod que fabricaban un RUT ajeno ──
    ("FAB-01", "Rut 9 224 066 5",                  "9224066-5"),
    ("FAB-02", "80875541",                         "8087554-1"),
    ("FAB-03", "Es para Jeannette Terán 81668345", "8166834-5"),
    ("FAB-04", "80664206 otra persona( padre)",    "8066420-6"),
    ("FAB-05", "5 134 2992",                       "5134299-2"),
    ("FAB-06", "89481791",                         "8948179-1"),

    # ── Cuerpo de 8 legítimo: ninguna otra lectura valida → DV derivado ──
    ("CUERPO-01", "20997207",   "20997207-7"),
    ("CUERPO-02", "13.512.412", "13512412-5"),

    # ── 9 dígitos: el último siempre es el DV (sin cambios) ──
    ("NUEVE-01", "209972077", "20997207-7"),
    ("NUEVE-02", "123456785", "12345678-5"),

    # ── DV explícito: manda lo que escribió el paciente ──
    ("EXP-01", "12.345.678-5",          "12345678-5"),
    ("EXP-02", "7216452-0 Sonia Díaz",  "7216452-0"),
    ("EXP-03", "9.224.066-5",           "9224066-5"),

    # ── DV tecleado mal, la corrección VALIDA → se acepta ──
    ("TIPEO-01", "12.067.174-J", "12067174-K"),
    ("TIPEO-02", "22.643.819-l", "22643819-K"),
]


def _check_no_fabrica_dv() -> tuple[int, int]:
    """Propiedad: para TODO RUT válido de 7 u 8 dígitos de cuerpo, escribirlo
    sin separadores debe devolver ESE rut, no otro."""
    ok = fail = 0
    for cuerpo_int in range(1_000_000, 30_000_000, 97_777):
        cuerpo = str(cuerpo_int)
        dv = _calcular_dv_rut(cuerpo)
        if not dv or dv == "K":
            continue  # 'K' pegado sin guión es otro caso (letra), no aplica
        pegado = f"{cuerpo}{dv}"          # como lo escribe el paciente
        esperado = f"{cuerpo}-{dv}"
        obtenido = clean_rut(pegado)
        if obtenido == esperado and valid_rut(obtenido):
            ok += 1
        else:
            fail += 1
            if fail <= 5:
                print(f"   ❌ {pegado!r} → {obtenido!r} (esperado {esperado!r})")
    return ok, fail


def main() -> int:
    passed = failed = 0

    print("── Casos reales de producción ──")
    for cid, entrada, esperado in CASES:
        obtenido = clean_rut(entrada)
        valido = valid_rut(obtenido)
        if obtenido == esperado and valido:
            passed += 1
            print(f"✅ {cid:11s} {entrada[:34]!r:36s} → {obtenido}")
        else:
            failed += 1
            print(f"❌ {cid:11s} {entrada[:34]!r:36s} → {obtenido!r} "
                  f"(esperado {esperado!r}, valid={valido})")

    print("\n── DV mal tecleado que NO valida: debe rechazarse, no adivinarse ──")
    # 12.345.678-5 es el válido; con DV 'J' la corrección 'K' NO valida →
    # el bot tiene que rechazar, jamás derivar un DV y seguir.
    for cid, entrada in [("NOADIV-01", "12.345.678-J"), ("NOADIV-02", "12.345.678-O")]:
        obtenido = clean_rut(entrada)
        if not valid_rut(obtenido):
            passed += 1
            print(f"✅ {cid:11s} {entrada!r} → {obtenido!r} rechazado (correcto)")
        else:
            failed += 1
            print(f"❌ {cid:11s} {entrada!r} → {obtenido!r} ACEPTADO — está adivinando")

    print("\n── Propiedad: RUT pegado sin separadores nunca cambia de persona ──")
    ok, bad = _check_no_fabrica_dv()
    passed += ok
    failed += bad
    print(f"   {ok} ok, {bad} fallidos sobre RUTs sintéticos")

    total = passed + failed
    print(f"\n── Total: {passed}/{total} passed, {failed} failed ──")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
