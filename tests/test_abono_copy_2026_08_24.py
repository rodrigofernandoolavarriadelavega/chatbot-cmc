"""
Regresión — consolidado 2026-08-24, hallazgo #11: especialidades con abono
obligatorio (Psiquiatría/Gastroenterología) mostraban "💰 Consulta: $X" igual
que cualquier consulta normal, sin aclarar que es un ABONO que debe pagarse
ANTES de confirmar la hora (no el día de la atención) — y al cancelar esas
citas no se mencionaba nada sobre la plata ya pagada.

Fix:
1. `_precio_line`: si la especialidad/profesional tiene una regla de abono
   con `gate_bot=True` Y el flag `ABONO_GATE_PSIQ_ACTIVE` está encendido,
   muestra "💳 Abono previo requerido: $X (se paga antes de confirmar la
   hora)" en vez de "💰 Consulta: $X".
2. `CONFIRMING_CANCEL` (cancelación exitosa): si la especialidad cancelada
   tiene abono, agrega una nota indicando que recepción coordina la
   devolución/reprogramación.

Uso:
    PYTHONPATH=app:. python tests/test_abono_copy_2026_08_24.py
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT))

import flows  # noqa: E402

PASS = 0
FAIL = 0


def check(label: str, cond: bool, detail: str = ""):
    global PASS, FAIL
    PASS += cond
    FAIL += not cond
    print(("OK  " if cond else "FAIL"), label, "" if cond else f"— {detail}")


def main():
    # ── Con el flag de abono ACTIVO ─────────────────────────────────────────
    with patch.object(flows, "_abono_gate_psiq_activo", return_value=True):
        linea_psiq = flows._precio_line("Psiquiatría", id_profesional=78)
        check("Psiquiatría muestra 'Abono previo requerido'",
              "Abono previo requerido" in linea_psiq, linea_psiq)
        check("Psiquiatría YA NO dice 'Consulta:' (ambiguo con abono)",
              "Consulta:" not in linea_psiq, linea_psiq)
        check("Psiquiatría menciona el monto $60.000",
              "60.000" in linea_psiq or "60000" in linea_psiq, linea_psiq)

        linea_gastro = flows._precio_line("Gastroenterología", id_profesional=65)
        check("Gastroenterología muestra 'Abono previo requerido'",
              "Abono previo requerido" in linea_gastro, linea_gastro)

        # Control: especialidad SIN abono sigue mostrando "Consulta:" normal
        linea_mg = flows._precio_line("Medicina General", id_profesional=1)
        check("Medicina General (sin abono) sigue diciendo 'Consulta:'",
              "Consulta:" in linea_mg or "Fonasa" in linea_mg, linea_mg)

    # ── Con el flag de abono APAGADO: comportamiento normal (no confundir) ─
    with patch.object(flows, "_abono_gate_psiq_activo", return_value=False):
        linea_psiq_off = flows._precio_line("Psiquiatría", id_profesional=78)
        check("Con el flag OFF, Psiquiatría NO fuerza el copy de abono",
              "Abono previo requerido" not in linea_psiq_off, linea_psiq_off)


if __name__ == "__main__":
    main()
    print(f"\n{PASS} pasaron, {FAIL} fallaron")
    sys.exit(1 if FAIL else 0)
