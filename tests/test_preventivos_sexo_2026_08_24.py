"""
Regresión — consolidado 2026-08-24, hallazgo #12: recomendaciones de salud
preventiva enviadas sin verificar sexo del paciente. Casos reales: "Salim"
(nombre masculino) recibió recordatorio de PAP; "Elian" (nombre masculino)
recibió el recordatorio de vacuna VPH con el texto "previene cáncer
cervicouterino" (cáncer exclusivamente femenino).

Fix:
1. `app/pni.py`: la descripción de VPH en `_PNI_CALENDARIO` ya no menciona
   SOLO "cáncer cervicouterino" — es neutra ("cánceres asociados al VPH:
   cervicouterino, orofaríngeo, anal y otros"), correcta para cualquier sexo,
   sin necesidad de lógica condicional adicional.
2. `app/autocuidado.py`: "salim"/"elian"/otros nombres no tradicionalmente
   chilenos agregados a `_NOMBRES_MASCULINOS` — refuerza el mecanismo YA
   EXISTENTE de `_resolver_sexo()` (prioridad 1: si el nombre infiere sexo
   con certeza y CONTRADICE el registro de Medilink, usa el nombre) para que
   pueda corregir un dato de sexo mal cargado en Medilink.
3. Se confirma (sin cambios, ya estaba bien) que `get_tips_autocuidado` omite
   cualquier tip sexo-específico cuando el sexo no se puede determinar.

Uso:
    PYTHONPATH=app:. python tests/test_preventivos_sexo_2026_08_24.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT))

import autocuidado  # noqa: E402
import pni  # noqa: E402

PASS = 0
FAIL = 0


def check(label: str, cond: bool, detail: str = ""):
    global PASS, FAIL
    PASS += cond
    FAIL += not cond
    print(("OK  " if cond else "FAIL"), label, "" if cond else f"— {detail}")


def main():
    # ── VPH: texto neutro, no exclusivo de mujeres ─────────────────────────
    vph_entries = [row for row in pni._PNI_CALENDARIO if "VPH" in row[2]]
    check("hay entrada VPH en el calendario", len(vph_entries) == 1)
    _desc_vph = vph_entries[0][3].lower()
    check("VPH ya no dice SOLO 'cáncer cervicouterino'",
          "cervicouterino" in _desc_vph and "cánceres asociados" in _desc_vph,
          _desc_vph)

    # get_vaccine_reminder para un niño de 10 años (VPH en 4° básico) —
    # el texto debe seguir siendo correcto/neutro para cualquier sexo.
    from datetime import date
    fn_10 = date(date.today().year - 10, date.today().month, 1).isoformat()
    msg_vph = pni.get_vaccine_reminder(fn_10, "Elian")
    check("recordatorio VPH generado para niño 10 años", bool(msg_vph))
    if msg_vph:
        check("recordatorio no dice SOLO cáncer cervicouterino",
              "cánceres asociados al vph" in msg_vph.lower(), msg_vph)

    # ── Nombres masculinos ampliados ────────────────────────────────────────
    for nombre in ("salim", "elian", "khalil", "omar", "karim", "amir"):
        check(f"'{nombre}' se infiere como masculino",
              autocuidado._inferir_sexo_por_nombre(nombre) == "M")

    # ── _resolver_sexo corrige un Medilink mal cargado usando el nombre ────
    sexo_resuelto = autocuidado._resolver_sexo("F", "Salim Fuentes", phone="56900000601")
    check("_resolver_sexo corrige F→M para 'Salim' vs Medilink erróneo",
          sexo_resuelto == "M", sexo_resuelto)

    # ── get_tips_autocuidado: PAP NO se envía a 'Salim' aunque Medilink
    #    tenga sexo='F' cargado erróneamente ─────────────────────────────────
    from datetime import date as _date
    fn_35 = _date(_date.today().year - 35, 6, 1).isoformat()
    tips_salim = autocuidado.get_tips_autocuidado(
        fecha_nacimiento=fn_35, sexo="F", especialidad="medicina general",
        nombre="Salim Fuentes", phone="56900000602",
    )
    check("PAP no aparece para 'Salim' pese a sexo Medilink='F'",
          "PAP" not in tips_salim, tips_salim)

    # ── Control: mujer real de 35 años SÍ recibe el tip de PAP ──────────────
    tips_mujer = autocuidado.get_tips_autocuidado(
        fecha_nacimiento=fn_35, sexo="F", especialidad="medicina general",
        nombre="Valentina Soto", phone="56900000603",
    )
    check("PAP SÍ aparece para mujer real de 35 años", "PAP" in tips_mujer, tips_mujer)

    # ── Control: sexo desconocido (sin Medilink, nombre no reconocido) →
    #    se omite el tip sexo-específico, no se adivina ──────────────────────
    tips_desconocido = autocuidado.get_tips_autocuidado(
        fecha_nacimiento=fn_35, sexo=None, especialidad="medicina general",
        nombre="Zyx Qwerty", phone="56900000604",
    )
    check("sin sexo conocido, no se envía PAP (se omite, no se adivina)",
          "PAP" not in tips_desconocido, tips_desconocido)


if __name__ == "__main__":
    main()
    print(f"\n{PASS} pasaron, {FAIL} fallaron")
    sys.exit(1 if FAIL else 0)
