"""
Regresión — portaviones consolidado 2026-09-24, problema #11 (parte 2:
cross-sell de kinesiología ofrecido a menores).

Casos reales (misma sesión, mismo pitch fijo "Si tienes dolor crónico de
espalda, cuello u hombros..."):
- 56987851161: Alonso Ariel Gavilán Velásquez, 11 años, agendado "Para mí"
  en Medicina Familiar (Dr. Márquez) — recibió el pitch de kine justo
  después del recordatorio de vacunas PNI (VPH, 4° básico).
- 56991989581 / 56987851161: Javier Aedo Burgos, 13 años, agendado como
  titular del teléfono en Medicina General — mismo pitch tras el
  recordatorio de dTpa 8° básico.
- 56974072362: Cristobal (11 años) — mismo pitch.

Root cause: `_CROSS_SELL_RULES["Medicina General"/"Medicina Familiar"]`
ofrece Kinesiología sin filtro de edad, y el gate existente
(`not reagendar and not es_tercero`) no distingue: cuando el paciente
quedó como "titular" de un teléfono compartido (flujo `WAIT_BOOKING_FOR`
→ "Para mí" con un RUT de un niño, o quick-book reusando el último
paciente), `es_tercero` es False aunque el paciente real sea un niño.

Fix: `_cross_sell_interactive` acepta `excluir_destinos`; el call site en
la confirmación de cita calcula `es_menor_de(fecha_nac, 12)` (mismo
`fecha_nac` ya resuelto para PNI/hitos) y excluye "Kinesiología" cuando
el paciente es menor de 12 años.

Ejecución:
    python tests/test_portaviones_2026_09_24_crosssell_menor.py
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT))

import flows  # noqa: E402
import pni  # noqa: E402

FAILS = 0


def check(label: str, got, expected):
    global FAILS
    ok = got == expected
    print(f"{'OK  ' if ok else 'FAIL'} {label}: got={got!r} expected={expected!r}")
    if not ok:
        FAILS += 1


def check_true(label: str, cond: bool):
    global FAILS
    print(f"{'OK  ' if cond else 'FAIL'} {label}")
    if not cond:
        FAILS += 1


# ── es_menor_de: unidad pura ──
hoy = date.today()
fecha_11 = f"{hoy.year - 11}-01-15"   # 11 años (asumiendo ya cumplió en enero)
fecha_13 = f"{hoy.year - 13}-01-15"
fecha_30 = f"{hoy.year - 30}-06-01"

check_true("es_menor_de_11_bajo_12", pni.es_menor_de(fecha_11, 12))
check_true("es_menor_de_13_NO_es_menor_12", not pni.es_menor_de(fecha_13, 12))
check_true("es_menor_de_adulto_NO_es_menor_12", not pni.es_menor_de(fecha_30, 12))
check_true("es_menor_de_fecha_vacia_no_bloquea", not pni.es_menor_de("", 12))
check_true("es_menor_de_fecha_invalida_no_bloquea", not pni.es_menor_de("no-es-fecha", 12))


# ── _cross_sell_interactive: excluir_destinos filtra la regla ──
with patch("session.puede_cross_sell", return_value=True), \
     patch("session.log_cross_sell", return_value=None):
    _cs_sin_filtro = flows._cross_sell_interactive(
        "56900000000", "Medicina General", {}
    )
    check_true(
        "sin_excluir_ofrece_kine_normalmente",
        _cs_sin_filtro is not None
        and _cs_sin_filtro["_cross_sell_esp_destino"] == "Kinesiología",
    )

    _cs_con_filtro = flows._cross_sell_interactive(
        "56900000000", "Medicina General", {}, {"Kinesiología"}
    )
    check_true(
        "con_excluir_kine_salta_a_nutricion",
        _cs_con_filtro is not None
        and _cs_con_filtro["_cross_sell_esp_destino"] == "Nutrición",
    )

    # Medicina Familiar tiene la misma regla (caso real Alonso/Márquez)
    _cs_familiar = flows._cross_sell_interactive(
        "56900000000", "Medicina Familiar", {}, {"Kinesiología"}
    )
    check_true(
        "medicina_familiar_tambien_respeta_exclusion",
        _cs_familiar is not None
        and _cs_familiar["_cross_sell_esp_destino"] == "Nutrición",
    )

print()
if FAILS:
    print(f"{FAILS} fallo(s)")
    sys.exit(1)
print("Todos los casos OK")
