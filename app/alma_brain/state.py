"""Estado global del mundo de Alma — la capa PERCIBIR unificada.

Compone los sensores de todos los dominios en un único snapshot y deriva
ALERTAS cross-domain (señales determinísticas). Las alertas son la semilla del
razonamiento agéntico: cruzan dominios que hoy viven en dashboards aislados
(ej: "agenda cayendo + cohorte inactiva contactable → oportunidad win-back").

El Copilot (Fase 2) razona SOBRE este estado; no vuelve a consultar las fuentes.
Patrón calcado de autopilot/world_state.py: snapshot JSON atómico + load/save.
"""
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import sensors

log = logging.getLogger("bot")

SNAPSHOT_PATH = os.getenv("ALMA_BRAIN_SNAPSHOT_PATH", "data/alma_brain_state.json")


@dataclass
class Alert:
    domain: str
    severity: str  # "info" | "warn" | "oportunidad" | "critico"
    message: str
    data: dict = field(default_factory=dict)


# ── Construcción del estado global ───────────────────────────────────────────

def build_state(window_days: int = 7) -> dict:
    """Arma el snapshot global. Read-only y Medilink-free. Cada dominio degrada
    de forma independiente: un dominio caído no tumba el resto."""
    domains = {
        "agenda": sensors.sense_agenda(window_days),
        "caja": sensors.sense_caja(window_days),
        "demanda": sensors.sense_demanda(90),
        "ads": sensors.sense_ads(),
        "fidelizacion": sensors.sense_fidelizacion(),
    }
    alerts = _derive_alerts(domains)

    available = [k for k, v in domains.items() if v.get("available")]
    state = {
        # Sin Date.now reproducibilidad: este snapshot SÍ es en vivo, marcamos hora real.
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window_days": window_days,
        "domains": domains,
        "domains_available": available,
        "alerts": [a.__dict__ for a in alerts],
    }
    log.info("alma_brain: estado global — %d/%d dominios OK · %d alertas",
             len(available), len(domains), len(alerts))
    return state


def _derive_alerts(domains: dict) -> list[Alert]:
    """Señales cross-domain determinísticas. Acá NO hay IA: son reglas claras
    que el Copilot luego interpreta y prioriza. Mantener conservador y explicable."""
    alerts: list[Alert] = []

    agenda = domains.get("agenda", {})
    caja = domains.get("caja", {})
    demanda = domains.get("demanda", {})
    ads = domains.get("ads", {})
    fidel = domains.get("fidelizacion", {})

    # Agenda cayendo fuerte vs ventana previa.
    if agenda.get("available"):
        d = agenda["kpis"].get("delta_pct")
        if d is not None and d <= -15:
            alerts.append(Alert("agenda", "warn",
                f"Citas cayeron {abs(d):.0f}% vs la ventana previa "
                f"({agenda['kpis']['citas_ventana']} vs {agenda['kpis']['citas_ventana_previa']}).",
                {"delta_pct": d}))
        elif d is not None and d >= 25:
            alerts.append(Alert("agenda", "info",
                f"Citas subieron {d:.0f}% vs la ventana previa.", {"delta_pct": d}))

    # Demanda insatisfecha concentrada en una especialidad.
    if demanda.get("available"):
        top = (demanda.get("servicios_sin_cupo") or [])
        if top:
            t = top[0]
            sol = t.get("solicitudes", 0)
            if sol >= 5:
                alerts.append(Alert("demanda", "oportunidad",
                    f"'{t.get('especialidad')}' acumula {sol} solicitudes sin cupo "
                    f"({t.get('personas','?')} personas). Evaluar abrir agenda.",
                    {"especialidad": t.get("especialidad"), "solicitudes": sol}))

    # Meta sobre/sub-atribuye: desconfiar de la señal publicitaria.
    if ads.get("available"):
        ar = ads["kpis"].get("attribution_ratio")
        if ar is not None and ar < 0.7:
            alerts.append(Alert("ads", "warn",
                f"Meta atribuye más ingreso del que registra la caja real "
                f"(ratio {ar:.2f}). Desconfiar del ROAS reportado.", {"ratio": ar}))
        pend = [a for a in ads.get("acciones_propuestas", []) if a.get("necesita_aprobacion")]
        if pend:
            alerts.append(Alert("ads", "info",
                f"{len(pend)} acción(es) de Ads esperan tu aprobación en el Autopilot.",
                {"n": len(pend)}))

    # Oportunidad cross-domain: agenda floja + hay cohorte contactable.
    if (agenda.get("available") and fidel.get("available")
            and (agenda["kpis"].get("delta_pct") or 0) < 0
            and fidel["kpis"].get("cohortes_contactables", 0) > 0):
        alerts.append(Alert("fidelizacion", "oportunidad",
            f"Agenda a la baja y hay {fidel['kpis']['cohortes_contactables']} pacientes "
            f"reactivables. Buen momento para un win-back dirigido.",
            {"contactables": fidel["kpis"]["cohortes_contactables"]}))

    return alerts


# ── Persistencia (snapshot atómico, mismo patrón que autopilot) ──────────────

def save_state(state: dict) -> None:
    try:
        os.makedirs(os.path.dirname(SNAPSHOT_PATH) or ".", exist_ok=True)
        tmp = SNAPSHOT_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, default=str)
        os.replace(tmp, SNAPSHOT_PATH)
    except Exception as e:  # noqa: BLE001
        log.warning("alma_brain: no pude guardar snapshot (%s)", e)


def load_state() -> dict | None:
    try:
        with open(SNAPSHOT_PATH, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:  # noqa: BLE001
        log.warning("alma_brain: no pude leer snapshot (%s)", e)
        return None


def build_and_save(window_days: int = 7) -> dict:
    """Atajo para el cron: construye y persiste."""
    state = build_state(window_days)
    save_state(state)
    return state
