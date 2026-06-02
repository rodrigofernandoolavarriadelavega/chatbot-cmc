"""Bus de eventos — la flota reacciona en tiempo real, no solo por reloj.

Hoy los agentes despiertan por cron. Esto los hace reactivos: cuando pasa algo
en la clínica (una cancelación, un "Peor" en post-consulta, una anomalía de
pauta), se EMITE un evento y los agentes suscritos reaccionan en el momento.

  emit(tipo, payload)  →  busca agentes con ese tipo en `agent.triggers`
                          →  corre su run_reactive() (mismo gating + guardrails)

Inerte por defecto: si el maestro está off, emit() no hace nada (solo loggea).
Por eso es seguro dejar llamadas a emit() sembradas en flows/jobs — no tienen
efecto hasta encender la flota. `emit(..., dry=True)` previsualiza sin ejecutar.
"""
import logging

from . import guardrails, registry

log = logging.getLogger("bot")

# Catálogo de eventos conocidos (documentación; emit acepta cualquiera).
EVENTS = {
    "cita_cancelada": "Un paciente o recepción anuló una cita → cupo libre.",
    "slot_liberado": "Se liberó un cupo concreto (esp/fecha/hora).",
    "post_consulta_peor": "Un paciente reportó sentirse peor en el seguimiento.",
    "nps_detractor": "Llegó un NPS bajo (detractor) → recuperar reputación.",
    "nps_promotor": "Llegó un NPS alto (promotor) → pedir reseña pública.",
    "ad_anomalia": "Métrica de pauta fuera de rango (CPL/frecuencia).",
    "stock_critico": "Un insumo cruzó el umbral de quiebre.",
    "paciente_abandono_flujo": "Un paciente abandonó un flujo de agendamiento.",
    "resultado_examen_listo": "Llegó un resultado de examen para avisar.",
}


def subscribers(event_type: str) -> list:
    """Agentes cuyo `triggers` incluye este evento."""
    return [a for a in registry.all_agents().values()
            if event_type in (getattr(a, "triggers", None) or [])]


async def emit(event_type: str, payload: dict | None = None, dry: bool = False) -> dict:
    """Dispara un evento. Devuelve un resumen de quién reaccionó y qué hizo.

    Con el maestro off es un no-op seguro (las llamadas sembradas en producción
    no tienen efecto hasta encender la flota). `dry=True` fuerza preview.
    """
    payload = payload or {}
    subs = subscribers(event_type)
    summary = {"event": event_type, "n_suscritos": len(subs),
               "reacciones": [], "skipped": None}

    if not guardrails.master_enabled():
        summary["skipped"] = "flota apagada (ALMA_AGENTS_ENABLED=false) → no-op"
        log.debug("events.emit(%s): flota apagada, no-op", event_type)
        return summary
    if not subs:
        log.debug("events.emit(%s): sin agentes suscritos", event_type)
        return summary

    for agent in subs:
        try:
            res = await agent.run_reactive(event_type, payload, force_dry_run=dry)
            summary["reacciones"].append({
                "agent_id": agent.id,
                "propuestas": len(res.get("actions_proposed", [])),
                "ejecutadas": len(res.get("actions_executed", [])),
                "bloqueadas": len(res.get("actions_blocked", [])),
                "notes": res.get("notes", ""),
            })
        except Exception as e:  # noqa: BLE001 — un agente nunca tumba el emisor
            log.error("events.emit(%s): %s falló: %s", event_type, agent.id, e)
            summary["reacciones"].append({"agent_id": agent.id, "error": str(e)})

    log.info("events.emit(%s): %d agentes reaccionaron", event_type, len(subs))
    return summary


def preview(event_type: str, payload: dict | None = None) -> dict:
    """Quién reaccionaría a este evento (sin ejecutar nada). Para el panel."""
    return {"event": event_type,
            "descripcion": EVENTS.get(event_type, "(evento ad-hoc)"),
            "suscritos": [a.id for a in subscribers(event_type)],
            "conocido": event_type in EVENTS}
