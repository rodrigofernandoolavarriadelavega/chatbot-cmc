"""Capstone — el ciclo autónomo que une cerebro + flota + ledger.

Las piezas existen sueltas: el cerebro (alma_brain) percibe el estado del negocio
y emite alertas; la flota (alma_agents) actúa en sus propios horarios; el ledger
mide si esas acciones convirtieron. El capstone es el **latido diario** que las
une en un solo ciclo y produce UN digest:

    sense (cerebro: alertas/estado)  →  intent (flota: qué hará hoy, simulado)
        →  measure (ledger: qué convirtió)  →  digest (un informe unificado)

No re-ejecuta los agentes (cada uno corre en su cron y se auto-gatea); orquesta
el sensado, la medición y el reporte. Con la flota apagada es un informe diario
de "qué haría y cuánto rindió la capa autónoma"; encendida, es la consola de la
clínica operándose sola. Read-only salvo la tabla del ledger.
"""
import json
import logging
import os
from datetime import datetime, timezone

from . import guardrails, ledger, simulator

log = logging.getLogger("bot")

_DIR = os.getenv("ALMA_AGENTS_DATA_DIR", "data/alma_agents")
_PATH = os.path.join(_DIR, "capstone.json")

_SEV_RANK = {"critico": 0, "oportunidad": 1, "warn": 2, "info": 3}


def _top_alerts(world: dict | None, limit: int = 6) -> list[dict]:
    if not world:
        return []
    alerts = world.get("alerts", []) or []
    return sorted(alerts, key=lambda a: _SEV_RANK.get(a.get("severity"), 9))[:limit]


def _headline(fs: dict, sim_tot: dict, eff_tot: dict, n_alertas: int) -> str:
    if not fs.get("master_enabled"):
        modo = "Flota APAGADA"
    elif fs.get("execute_enabled"):
        modo = "Flota EJECUTANDO"
    else:
        modo = "Flota en dry-run"
    return (f"{modo} · {sim_tot.get('acciones', 0)} acciones propuestas hoy "
            f"({sim_tot.get('se_ejecutaria', 0)} se ejecutarían) · "
            f"{n_alertas} alertas del cerebro · "
            f"{eff_tot.get('citas', 0)} citas atribuidas acumuladas")


async def run_cycle(_world=None, _sim=None) -> dict:
    """Corre el ciclo y devuelve el digest. Inyectables `_world`/`_sim` para tests.

    Nunca lanza: cada etapa degrada de forma independiente.
    """
    # 1) SENSE — estado del cerebro (read-only). Reusa snapshot si existe.
    world = _world
    if world is None:
        try:
            from alma_brain.state import load_state, build_state
            world = load_state() or build_state(7)
        except Exception as e:  # noqa: BLE001
            log.warning("capstone: cerebro no disponible (%s)", e)
            world = None

    # 2) MEASURE — actualiza conversiones del ledger.
    try:
        measured = ledger.measure_outcomes()
    except Exception as e:  # noqa: BLE001
        log.warning("capstone: ledger.measure falló (%s)", e)
        measured = {}
    try:
        eff = ledger.effectiveness_summary()
    except Exception as e:  # noqa: BLE001
        eff = {"per_agent": [], "totales": {}}

    # 3) INTENT — qué haría la flota hoy (simulación agregada, sin ejecutar).
    sim = _sim
    if sim is None:
        try:
            sim = await simulator.simulate_fleet()
        except Exception as e:  # noqa: BLE001
            log.warning("capstone: simulación falló (%s)", e)
            sim = {"totales": {}, "riesgo_agregado": {}}

    fs = guardrails.fleet_status()
    sim_tot = sim.get("totales", {}) if sim else {}
    eff_tot = eff.get("totales", {}) if eff else {}
    alerts = _top_alerts(world)

    # 4) DIGEST — un informe unificado de la capa autónoma.
    digest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "headline": _headline(fs, sim_tot, eff_tot, len(alerts)),
        "fleet_status": fs,
        "cerebro": {
            "disponible": world is not None,
            "dominios_ok": (world or {}).get("domains_available", []),
            "alertas_top": alerts,
            "n_alertas": len((world or {}).get("alerts", []) or []),
        },
        "flota_intencion": {
            "acciones": sim_tot.get("acciones", 0),
            "por_riesgo": sim_tot.get("por_riesgo", {}),
            "contactos_pacientes": sim_tot.get("contactos_pacientes", 0),
            "escrituras_medilink": sim_tot.get("escrituras_medilink", 0),
            "se_ejecutaria": sim_tot.get("se_ejecutaria", 0),
            "se_bloquearia": sim_tot.get("se_bloquearia", 0),
            "riesgo_agregado": (sim.get("riesgo_agregado", {}) if sim else {}),
        },
        "efectividad": {
            "totales": eff_tot,
            "top_agentes": (eff.get("per_agent", [])[:5] if eff else []),
            "medido_en_este_ciclo": measured,
        },
    }
    _persist(digest)
    log.info("capstone: ciclo OK — %s", digest["headline"])
    return digest


def _persist(digest: dict) -> None:
    try:
        os.makedirs(_DIR, exist_ok=True)
        tmp = _PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(digest, f, ensure_ascii=False, default=str)
        os.replace(tmp, _PATH)
    except Exception as e:  # noqa: BLE001
        log.warning("capstone: no pude persistir digest (%s)", e)


def load_last() -> dict | None:
    try:
        with open(_PATH, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception:  # noqa: BLE001
        return None
