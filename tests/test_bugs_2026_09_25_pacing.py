"""Regresión: el cron horario `prellenar_pagos` (pagos_routes.py) paceaba sus
requests a Medilink a 0.15-0.18s — muy por encima del límite real documentado
(~20 req/min, docs/medilink_gotchas.md #6) — y generaba su propia tormenta de
429 que competía por el presupuesto real de la cuenta con escrituras de
pacientes en vivo.

Caso real 2026-09-25 13:45-13:47 (fecha=2026-09-25, 64 citas): mientras el
job corría, una paciente nueva (56961281031) intentó registrarse. El POST
`/pacientes` chocó con la saturación 429 sostenida generada por el propio
job (log: "MEDILINK_429 ... total=13/14/17/..." en la misma ventana) y su
registro falló con "Hubo un problema al registrarte" — la ficha NUNCA se
creó (verificado: ambos intentos del POST devolvieron 429, sin 5xx real).
El carril batch (`lane_batch()`, fix 2026-07-27) evita que esos 429 tumben
el circuit breaker global, pero no limita cuántas requests reales consume
el job del presupuesto compartido — este test cubre esa parte.

No ejecuta la función completa (requiere Medilink/DB reales): audita que el
pacing usado en los 3 puntos de throttle de `prellenar_pagos` respete un
piso seguro (>=3s, la fracción real del presupuesto de ~20 req/min).

Ejecución:
    PYTHONPATH=app:. venv/bin/python3 tests/test_bugs_2026_09_25_pacing.py
"""
from __future__ import annotations

import inspect
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT))

import pagos_routes  # noqa: E402

# ~20 req/min documentado en docs/medilink_gotchas.md (#6) → 1 request cada
# 3s como mínimo. El job batch debe ir MÁS lento que eso para dejar margen
# real al carril paciente mientras corre en paralelo.
_MIN_SAFE_PACE_S = 3.0


class TestPrellenarPagosPacing(unittest.TestCase):
    def test_constante_de_pacing_respeta_el_presupuesto_real(self):
        self.assertTrue(
            hasattr(pagos_routes, "_BATCH_PACE_S"),
            "prellenar_pagos debe tener una constante de pacing nombrada, "
            "no sleeps mágicos dispersos",
        )
        self.assertGreaterEqual(
            pagos_routes._BATCH_PACE_S, _MIN_SAFE_PACE_S,
            "el pacing del batch debe ser >= 3s (presupuesto real ~20 req/min) "
            "para no generar su propia tormenta de 429 que le quite cupo al "
            "carril paciente",
        )

    def test_los_3_throttles_usan_la_constante_no_valores_sueltos(self):
        """Los 3 puntos de throttle (_fetch_prestacion/_fetch_atencion_meta/
        _fetch_ficha) deben usar `_BATCH_PACE_S`, no un número mágico corto
        (0.15/0.18) que reintroduzca la tormenta."""
        src = inspect.getsource(pagos_routes)
        sleeps_dentro_de_prellenar = re.findall(
            r"await asyncio\.sleep\(([^)]+)\)", src
        )
        # Filtra los sleeps que están en el bloque de prellenar_pagos (los 3
        # helpers _fetch_*). No debe quedar ningún sleep < 3s hardcodeado ahí.
        offenders = [
            s for s in sleeps_dentro_de_prellenar
            if s.strip() not in ("_BATCH_PACE_S",) and _es_numero_corto(s)
        ]
        self.assertEqual(
            offenders, [],
            f"quedaron sleeps cortos hardcodeados fuera de _BATCH_PACE_S: {offenders}",
        )


def _es_numero_corto(expr: str) -> bool:
    expr = expr.strip()
    try:
        return float(expr) < _MIN_SAFE_PACE_S
    except ValueError:
        return False


if __name__ == "__main__":
    unittest.main(verbosity=2)
