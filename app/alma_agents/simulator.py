"""Simulador de flota — vuelo de prueba del AGREGADO antes de encender nada.

El dry-run por agente responde "¿qué haría ESTE agente?". Esto responde la
pregunta que de verdad importa antes de encender 18 agentes que contactan
pacientes y mueven plata: **¿qué haría la flota ENTERA, junta, ahora mismo?**

El riesgo de una flota no es ningún agente solo — es el agregado: que entre
todos spameen al mismo paciente, o que la suma de escrituras a Medilink sea
masiva. Este simulador corre perceive+decide de todos los agentes (NUNCA
ejecuta, nunca contacta — es read-only) y agrega:

  - acciones totales por riesgo
  - contactos a pacientes, agrupados por paciente → detecta a quién contactarían
    DOS O MÁS agentes en la misma corrida (spam agregado; el guardrail central es
    el presupuesto de contacto por paciente)
  - escrituras a Medilink totales
  - con los flags actuales: qué se EJECUTARÍA vs qué se BLOQUEARÍA (y por qué)

Es el tablero que alguien por encender una flota riesgosa necesita ver: el
agregado, no la suma de piezas sueltas.
"""
import asyncio
import logging

from . import guardrails, registry, ledger, broker

log = logging.getLogger("bot")


def _mask(phone: str) -> str:
    """Enmascara un teléfono para el reporte (higiene aunque sea panel de dueño)."""
    d = ledger._digits(phone)
    return ("…" + d[-4:]) if len(d) >= 4 else "…"


async def _collect_one(agent) -> dict:
    """perceive+decide de UN agente, sin ejecutar. Errores aislados."""
    out = {"agent_id": agent.id, "name": agent.name, "risk": agent.risk,
           "enabled": guardrails.agent_enabled(agent.flag), "actions": [], "error": None}
    try:
        ctx = await agent.perceive()
        actions = await agent.decide(ctx)
        out["actions"] = actions or []
    except Exception as e:  # noqa: BLE001 — un agente nunca tumba el simulador
        out["error"] = str(e)
        log.warning("simulator: %s perceive/decide falló: %s", agent.id, e)
    return out


async def simulate_fleet(agents: dict | None = None) -> dict:
    """Corre toda la flota en modo análisis (sin ejecutar) y agrega el resultado.

    `agents` permite inyectar una flota de prueba; por defecto usa el registry.
    """
    fleet = agents if agents is not None else registry.all_agents()
    collected = await asyncio.gather(*[_collect_one(a) for a in fleet.values()])

    by_risk = {"bajo": 0, "medio": 0, "alto": 0, "extremo": 0}
    by_agent = []
    medilink_writes = 0
    staff_contacts = 0
    total_actions = 0
    would_execute = 0
    would_block = 0
    block_reasons: dict[str, int] = {}
    # paciente → set de agentes que lo contactarían en esta corrida
    contacts_by_patient: dict[str, set] = {}

    for c in collected:
        n_contacts = n_medilink = n_extreme = 0
        for a in c["actions"]:
            total_actions += 1
            risk = getattr(a, "risk", "medio")
            by_risk[risk] = by_risk.get(risk, 0) + 1
            if risk == "extremo":
                n_extreme += 1
            if getattr(a, "requires_medilink_write", False):
                medilink_writes += 1; n_medilink += 1
            if getattr(a, "requires_contact", False):
                if getattr(a, "is_staff", False):
                    staff_contacts += 1
                else:
                    n_contacts += 1
                    ph = ledger._digits(getattr(a, "target", "") or "")
                    if ph:
                        contacts_by_patient.setdefault(ph, set()).add(c["agent_id"])
            # ¿qué haría el guardrail con los flags actuales?
            allow, reason = guardrails.authorize(a)
            if allow:
                would_execute += 1
            else:
                would_block += 1
                # normalizar el motivo a una clave corta para el histograma
                key = reason.split("→")[0].split("(")[0].strip()[:60]
                block_reasons[key] = block_reasons.get(key, 0) + 1
        by_agent.append({
            "agent_id": c["agent_id"], "name": c["name"], "risk": c["risk"],
            "enabled": c["enabled"], "error": c["error"],
            "n_actions": len(c["actions"]), "n_contactos": n_contacts,
            "n_medilink": n_medilink, "n_extremo": n_extreme,
        })

    # Riesgo agregado: pacientes que más de un agente contactaría a la vez.
    max_week = guardrails.fleet_status().get("contact_max_week", 2)
    overlap = []
    for ph, ags in contacts_by_patient.items():
        if len(ags) > 1:
            overlap.append({"paciente": _mask(ph), "n_agentes": len(ags),
                            "agentes": sorted(ags)})
    overlap.sort(key=lambda x: x["n_agentes"], reverse=True)
    # cuántos excederían el presupuesto de contacto SOLO con esta corrida
    sobre_presupuesto = [o for o in overlap if o["n_agentes"] > max_week]

    patient_contacts_total = sum(len(a) for a in contacts_by_patient.values())

    # Broker: ¿quién ganaría cada paciente disputado, por valor esperado?
    proposals = []
    for c in collected:
        for a in c["actions"]:
            proposals.append({"agent_id": c["agent_id"], "action": a})
    try:
        brk = broker.resolve(proposals)
        broker_view = {
            "n_suprimidos_por_valor": brk["n_suprimidos"],
            "disputas": brk["by_patient"][:10],
        }
    except Exception as e:  # noqa: BLE001
        log.warning("simulator: broker falló (%s)", e)
        broker_view = {}

    return {
        "fleet_status": guardrails.fleet_status(),
        "totales": {
            "agentes": len(collected),
            "acciones": total_actions,
            "por_riesgo": by_risk,
            "contactos_pacientes": patient_contacts_total,
            "pacientes_unicos": len(contacts_by_patient),
            "contactos_staff": staff_contacts,
            "escrituras_medilink": medilink_writes,
            "se_ejecutaria": would_execute,
            "se_bloquearia": would_block,
        },
        "motivos_bloqueo": block_reasons,
        "riesgo_agregado": {
            "pacientes_multi_agente": overlap,
            "pacientes_sobre_presupuesto": sobre_presupuesto,
            "presupuesto_contacto_semana": max_week,
            "alerta": (
                f"{len(sobre_presupuesto)} paciente(s) serían contactados por más "
                f"agentes ({max(( o['n_agentes'] for o in sobre_presupuesto), default=0)}) "
                f"que el presupuesto semanal ({max_week}) — el guardrail los frenaría, "
                f"pero indica solapamiento de targeting entre agentes."
                if sobre_presupuesto else
                "Sin solapamiento por sobre el presupuesto de contacto en esta corrida."
            ),
        },
        "broker": broker_view,
        "por_agente": sorted(by_agent, key=lambda x: x["n_actions"], reverse=True),
    }
