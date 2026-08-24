"""
Regresión — consolidado 2026-08-24, hallazgo #15: restricciones de
especialidad no aplicadas.

1. Edad mínima (neurología ≥15 años, etc.): `buscar_paciente()` de Medilink
   puede devolver fecha_nacimiento en ISO (YYYY-MM-DD), no solo DD/MM/YYYY.
   El pre-flight de edad usaba `strptime(..., "%d/%m/%Y")` fijo — con ISO
   revienta ValueError, el `except: pass` lo traga en silencio y el bloqueo
   de edad queda desactivado sin avisar a nadie (caso real: neurología
   ofrecida a una niña de 3 años). Fix: reusa `_parsear_fecha_nacimiento`
   (multi-formato, ya usado en el registro de pacientes).
2. "ortodoncista"/"ortodonsista" en `claude_helper._INTENT_CACHE`: un cache
   hit ignora `respuesta_directa` (se fuerza a None), saltándose el texto
   que explica el flujo real (evaluación previa con Dra. Burgos → derivación
   a la ortodoncista) — el mismo motivo por el que "ortodoncia" YA estaba
   sacada del caché. Fix: sacadas también del caché.
3. "psicología infantil" → Montalba (74) exclusivamente, nunca Rodríguez
   (49, solo adulto) — ya estaba correcto en `medilink.ESPECIALIDADES_MAP`,
   test de regresión para que no se rompa.

Uso:
    PYTHONPATH=app:. python tests/test_restricciones_especialidad_2026_08_24.py
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT))

import flows  # noqa: E402
import claude_helper  # noqa: E402
from medilink import _ids_para_especialidad  # noqa: E402

PASS = 0
FAIL = 0


def check(label: str, cond: bool, detail: str = ""):
    global PASS, FAIL
    PASS += cond
    FAIL += not cond
    print(("OK  " if cond else "FAIL"), label, "" if cond else f"— {detail}")


def main():
    # ── 1. Parser de fecha_nacimiento multi-formato (raíz del bug de edad) ──
    _hoy = date.today()
    _fn_iso_3anios = date(_hoy.year - 3, _hoy.month, 1).isoformat()  # "YYYY-MM-DD"
    _fn_ddmmyyyy_3anios = f"01/{_hoy.month:02d}/{_hoy.year - 3}"      # "DD/MM/YYYY"

    d_iso = flows._parsear_fecha_nacimiento(_fn_iso_3anios)
    check("parser lee formato ISO (YYYY-MM-DD) de Medilink",
          d_iso is not None and (_hoy - d_iso).days // 365 == 3,
          f"input={_fn_iso_3anios!r} got={d_iso!r}")

    d_ddmm = flows._parsear_fecha_nacimiento(_fn_ddmmyyyy_3anios)
    check("parser lee formato DD/MM/YYYY",
          d_ddmm is not None and (_hoy - d_ddmm).days // 365 == 3,
          f"input={_fn_ddmmyyyy_3anios!r} got={d_ddmm!r}")

    # El caso real exacto: antes `datetime.strptime(fn, "%d/%m/%Y")` con un
    # fn ISO reventaba ValueError silenciosamente. Confirmar que YA NO pasa.
    import datetime as _dt_mod
    _crashed = False
    try:
        _dt_mod.datetime.strptime(_fn_iso_3anios, "%d/%m/%Y")
    except ValueError:
        _crashed = True
    check("confirmado: el formato viejo SÍ rompía con fecha ISO (motivo del bug)",
          _crashed)

    # ── 2. "ortodoncista" fuera del caché rápido ────────────────────────────
    check("'ortodoncista' NO está en el caché rápido (pasa por Claude a explicar)",
          "ortodoncista" not in claude_helper._INTENT_CACHE)
    check("'ortodonsista' (typo) NO está en el caché rápido",
          "ortodonsista" not in claude_helper._INTENT_CACHE)
    check("'ortodoncia' sigue fuera del caché (ya estaba bien)",
          "ortodoncia" not in claude_helper._INTENT_CACHE)

    # ── 3. psicología infantil → Montalba (74) exclusivamente ──────────────
    ids_infantil = set(_ids_para_especialidad("psicología infantil"))
    check("psicología infantil → solo Montalba (74), nunca Rodríguez (49)",
          ids_infantil == {74}, ids_infantil)
    ids_adulto = set(_ids_para_especialidad("psicología adulto"))
    check("psicología adulto → Montalba (74) y Rodríguez (49)",
          ids_adulto == {74, 49}, ids_adulto)


if __name__ == "__main__":
    main()
    print(f"\n{PASS} pasaron, {FAIL} fallaron")
    sys.exit(1 if FAIL else 0)
