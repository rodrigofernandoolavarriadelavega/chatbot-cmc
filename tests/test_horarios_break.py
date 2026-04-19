"""Tests de _generar_slots_horario con soporte de break/almuerzo.

Caso real: Leonardo Etcheverry (prof 21) lunes 09:40-18:00 con break 13:00-14:00,
intervalo 40 min. El bot ofrecía 13:40 (cae en break) y Medilink rechazaba
el crear_cita con "Profesional no tiene horario para la fecha y duración".

Ejecución:
    PYTHONPATH=app:. python3 tests/test_horarios_break.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT))

from medilink import _generar_slots_horario


def _assert(cond: bool, msg: str):
    if not cond:
        print(f"❌ {msg}")
        return 1
    print(f"✅ {msg}")
    return 0


def run() -> int:
    fails = 0

    # ── Caso real Leonardo lunes ──
    slots = _generar_slots_horario("09:40", "18:00", 40, break_t=("13:00", "14:00"))
    horas = [hi for hi, _ in slots]
    fails += _assert("13:40" not in horas, "Leo lun: 13:40 excluido (está en break)")
    fails += _assert("13:00" not in horas, "Leo lun: 13:00 excluido (inicio del break)")
    fails += _assert("12:20" in horas,     "Leo lun: 12:20-13:00 OK (justo antes del break)")
    fails += _assert("14:00" in horas,     "Leo lun: 14:00 OK (primer slot post-break)")
    fails += _assert("17:20" in horas,     "Leo lun: 17:20 OK (último slot)")
    fails += _assert("17:21" not in horas, "Leo lun: 17:21 no existe (off-grid)")

    # ── Break sin interferencia (ej. intervalo 30 alineado) ──
    slots = _generar_slots_horario("09:00", "17:00", 30, break_t=("13:00", "14:00"))
    horas = [hi for hi, _ in slots]
    fails += _assert("12:30" in horas,     "30min: 12:30 pre-break OK")
    fails += _assert("13:00" not in horas, "30min: 13:00 excluido")
    fails += _assert("13:30" not in horas, "30min: 13:30 excluido")
    fails += _assert("14:00" in horas,     "30min: 14:00 post-break OK")

    # ── Sin break (backward compat) ──
    slots = _generar_slots_horario("09:00", "11:00", 30)
    horas = [hi for hi, _ in slots]
    fails += _assert(horas == ["09:00", "09:30", "10:00", "10:30"],
                     "Sin break: 30min 09-11 → 4 slots")

    # ── Slot parcialmente solapado con break ──
    # 12:50-13:30 solapa: 13:00-13:30 en break. Debe excluirse.
    slots = _generar_slots_horario("09:40", "18:00", 40, break_t=("13:00", "14:00"))
    horas = [hi for hi, _ in slots]
    fails += _assert("12:40" not in horas, "40min: 12:40-13:20 excluido (inicio antes, fin en break)")

    # ── Break muy largo ──
    slots = _generar_slots_horario("08:00", "18:00", 30, break_t=("12:00", "16:00"))
    horas = [hi for hi, _ in slots]
    for h in ["12:00", "12:30", "13:00", "14:00", "15:00", "15:30"]:
        fails += _assert(h not in horas, f"Break largo: {h} excluido")
    fails += _assert("16:00" in horas, "Break largo: 16:00 OK (inicio post-break)")

    # ── Intervalo 60 (Javiera Burgos) con break ──
    slots = _generar_slots_horario("08:00", "18:00", 60, break_t=("13:00", "14:00"))
    horas = [hi for hi, _ in slots]
    fails += _assert("12:00" in horas,     "60min: 12:00-13:00 OK (justo antes break)")
    fails += _assert("13:00" not in horas, "60min: 13:00 excluido")
    fails += _assert("14:00" in horas,     "60min: 14:00 post-break OK")

    total = 21
    passed = total - fails
    print(f"\n── Total: {passed}/{total} passed, {fails} failed ──")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(run())
