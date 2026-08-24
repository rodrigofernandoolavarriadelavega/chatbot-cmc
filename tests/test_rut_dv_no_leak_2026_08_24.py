"""
Regresión — consolidado 2026-08-24, hallazgo #3: el mensaje de error de RUT
inválido revelaba el dígito verificador CORRECTO calculado ("Si tu RUT es
*28027007*, el dígito correcto es *5*") — fuga de validación de terceros:
cualquiera podía tipear un cuerpo de RUT ajeno y el bot le confirmaba el DV
real, sin que esa persona hubiese demostrado ser su dueña.

Fix: `medilink.hint_rut_error()` ya no incluye el DV calculado en ningún
mensaje — solo indica que el RUT no es válido.

También cubre: '_'→'-' y ','→'.' en `clean_rut` (ya resuelto en el código
existente, ver tests/test_rut.py CASES COMA-*) y 'olabarria'→'olavarría' en
el diccionario de apellidos de `flows.py` (ya resuelto, línea ~13976).

Uso:
    PYTHONPATH=app:. python tests/test_rut_dv_no_leak_2026_08_24.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT))

from medilink import hint_rut_error, _calcular_dv_rut  # noqa: E402
import flows  # noqa: E402

PASS = 0
FAIL = 0


def check(label: str, cond: bool, detail: str = ""):
    global PASS, FAIL
    PASS += cond
    FAIL += not cond
    print(("OK  " if cond else "FAIL"), label, "" if cond else f"— {detail}")


def main():
    # RUT con cuerpo válido pero DV incorrecto (DV real calculado != el escrito)
    casos_dv_incorrecto = [
        "28.027.007-9",   # DV real es distinto de 9
        "12.345.678-1",
        "10.568.006-1",
    ]
    for rut in casos_dv_incorrecto:
        cuerpo = rut.replace(".", "").split("-")[0]
        dv_real = _calcular_dv_rut(cuerpo)
        msg = hint_rut_error(rut)
        check(f"no dice 'dígito correcto' ({rut})",
              "dígito correcto" not in msg.lower() and "digito correcto" not in msg.lower(),
              msg)
        check(f"no dice 'Si tu RUT es' ({rut})",
              "si tu rut es" not in msg.lower(), msg)
        check(f"no incluye el cuerpo del RUT ajeno en la respuesta ({rut})",
              cuerpo not in msg, msg)
        check(f"DV real ({dv_real}) no aparece pegado al cuerpo ({rut})",
              f"{cuerpo}-{dv_real}" not in msg, msg)

    # RUT sin estructura reconocible -> mensaje genérico, sin cambios
    msg_generico = hint_rut_error("abc")
    check("mensaje genérico para input sin estructura",
          "no reconozco ese RUT" in msg_generico, msg_generico)

    # olabarria -> olavarría (ya cubierto en flows.py, confirmación)
    check("'olabarria' mapea a olavarría",
          flows._detectar_apellido_profesional("olabarria") == "olavarría")


if __name__ == "__main__":
    main()
    print(f"\n{PASS} pasaron, {FAIL} fallaron")
    sys.exit(1 if FAIL else 0)
