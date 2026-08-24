"""
Regresión — consolidado 2026-08-24, hallazgo #7: hitos del desarrollo
enviados con la edad de OTRA persona ("Santiago adulto" recibió la ficha de
"Santiago, 4 años").

Investigación: `app/flows.py` (~línea 11220, fix del 19-ago "Benjamin/
Escarleth") YA prioriza `paciente.get("fecha_nacimiento")` de Medilink (el
registro REAL de la persona agendada por RUT) por sobre `reg_fecha_nacimiento`
de la sesión, y usa la MISMA variable `fecha_nac` para PNI e hitos — el pedido
explícito de "que el fix PNI del 19-ago cubra también hitos" YA estaba
cumplido (misma fuente para ambos, no hay una ruta separada que use un dato
inferido/viejo solo para hitos).

Root cause real del caso "Santiago": si el celular del adulto tenía
contaminado el RUT del hijo pequeño en `contact_profiles` (mismo bug de
`try_autocapture_rut_name` corregido en el hallazgo #9 de esta ronda),
`buscar_paciente(rut)` devolvía la ficha del HIJO, no la del adulto — Medilink
"real" pero de la persona equivocada. El fix de #9 (verificación del celular
de ficha antes de cachear un RUT ajeno) ataca esta causa de raíz; no hay una
segunda causa en `hitos_desarrollo.py` en sí.

Este test fija el invariante explícito pedido: sin fecha_nacimiento real, NO
se envía nada (nunca se infiere de edad/nombre).

Uso:
    PYTHONPATH=app:. python tests/test_hitos_fecha_real_2026_08_24.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT))

from hitos_desarrollo import get_milestones_reminder  # noqa: E402
from pni import get_vaccine_reminder  # noqa: E402

PASS = 0
FAIL = 0


def check(label: str, cond: bool, detail: str = ""):
    global PASS, FAIL
    PASS += cond
    FAIL += not cond
    print(("OK  " if cond else "FAIL"), label, "" if cond else f"— {detail}")


def main():
    # Sin fecha_nacimiento (None/"") → nunca se infiere de nombre/edad, no
    # hay ficha de hitos ni de vacunas.
    check("sin fecha_nacimiento, get_milestones_reminder retorna None",
          get_milestones_reminder("", "Santiago") is None)
    check("sin fecha_nacimiento, get_vaccine_reminder retorna None",
          get_vaccine_reminder("", "Santiago") is None)

    # Con fecha de un ADULTO (>9 años), get_milestones_reminder no dispara
    # (rango pediátrico 0-9 años) — nunca infiere "adulto es niño" por nombre.
    from datetime import date
    fn_adulto = date(date.today().year - 30, 1, 1).isoformat()
    check("adulto (fecha real) NO recibe hitos aunque se llame 'Santiago' (nombre no es edad)",
          get_milestones_reminder(fn_adulto, "Santiago") is None)

    # Con fecha de un niño de 4 años, SÍ corresponde (control positivo).
    fn_nino = date(date.today().year - 4, 6, 1).isoformat()
    msg_nino = get_milestones_reminder(fn_nino, "Santiago")
    check("niño de 4 años (fecha real) SÍ recibe hitos", bool(msg_nino))


if __name__ == "__main__":
    main()
    print(f"\n{PASS} pasaron, {FAIL} fallaron")
    sys.exit(1 if FAIL else 0)
