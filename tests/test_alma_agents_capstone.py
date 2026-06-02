"""Tests del capstone — el ciclo que une cerebro + flota + ledger en un digest.

Inyecta un cerebro y una simulación falsos (determinista, sin red) y verifica que
el digest una las tres fuentes: alertas ordenadas por severidad, intención de la
flota, y efectividad. DB + data dir aislados. Corre:
`python3 tests/test_alma_agents_capstone.py`
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

# Aislar data dir (digest persistido) ANTES de importar el módulo.
_TMP = tempfile.mkdtemp(prefix="capstone_test_")
os.environ["ALMA_AGENTS_DATA_DIR"] = _TMP
os.environ.setdefault("ALMA_AGENTS_ENABLED", "false")

import session  # noqa: E402
session.DB_PATH = Path(_TMP) / "sessions.db"  # ledger usa una DB temporal limpia

from alma_agents import capstone  # noqa: E402

_OK = 0; _FAIL = 0


def check(cond, label):
    global _OK, _FAIL
    if cond: _OK += 1; print(f"OK  {label}")
    else: _FAIL += 1; print(f"XX  FALLA: {label}")


def main():
    fake_world = {
        "domains_available": ["agenda", "caja"],
        "alerts": [
            {"domain": "caja", "severity": "info", "message": "info menor"},
            {"domain": "agenda", "severity": "critico", "message": "agenda vacía mañana"},
            {"domain": "demanda", "severity": "oportunidad", "message": "demanda gastro alta"},
        ],
    }
    fake_sim = {
        "totales": {"acciones": 9, "por_riesgo": {"bajo": 2, "medio": 3, "alto": 4, "extremo": 0},
                    "contactos_pacientes": 5, "escrituras_medilink": 1,
                    "se_ejecutaria": 0, "se_bloquearia": 9},
        "riesgo_agregado": {"alerta": "Sin solapamiento", "pacientes_sobre_presupuesto": []},
    }

    d = asyncio.run(capstone.run_cycle(_world=fake_world, _sim=fake_sim))

    # Headline: flota apagada por defecto, refleja acciones y alertas
    check("APAGADA" in d["headline"], f"headline marca flota apagada (got: {d['headline']})")
    check("9 acciones" in d["headline"], "headline trae las 9 acciones simuladas")

    # Cerebro: alertas ordenadas por severidad (critico primero)
    top = d["cerebro"]["alertas_top"]
    check(top and top[0]["severity"] == "critico", f"alerta crítica va primero (got {top[0]['severity'] if top else None})")
    check(d["cerebro"]["n_alertas"] == 3, f"3 alertas totales (got {d['cerebro']['n_alertas']})")
    check(d["cerebro"]["disponible"] is True, "cerebro marcado disponible")

    # Flota: intención agregada pasa al digest
    fi = d["flota_intencion"]
    check(fi["acciones"] == 9 and fi["escrituras_medilink"] == 1, "intención de flota reflejada")
    check(fi["se_ejecutaria"] == 0, "0 se ejecutaría (flota off)")

    # Efectividad presente (vacía pero estructurada)
    check("efectividad" in d and "totales" in d["efectividad"], "bloque de efectividad presente")
    check("medido_en_este_ciclo" in d["efectividad"], "incluye medición del ciclo")

    # Persistencia: load_last devuelve el mismo digest
    last = capstone.load_last()
    check(last is not None and last["headline"] == d["headline"], "digest persistido y recuperable")

    # Degradación: sin cerebro no rompe
    d2 = asyncio.run(capstone.run_cycle(_world=None, _sim=fake_sim))
    check(d2["cerebro"]["disponible"] in (True, False), "sin cerebro inyectado no rompe (degrada)")

    print(f"\n{_OK} OK · {_FAIL} FALLAS")
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()
