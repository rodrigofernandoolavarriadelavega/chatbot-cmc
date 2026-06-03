"""Broker de contacto — los agentes compiten por el cupo del paciente por VALOR.

El guardrail evita el spam por orden de llegada (presupuesto de contacto). Esto
lo resuelve con criterio: cuando varios agentes quieren contactar al MISMO
paciente, gana el de mayor **valor esperado** = P(conversión) (que el loop de
aprendizaje calcula desde el Ledger) × valor del contacto (ticket). El paciente
recibe el mensaje que más probablemente convierte, no el primero que llegó.

`resolve(proposals)` recibe pares (agent_id, action) y devuelve, por paciente,
un ganador y los suprimidos. Las acciones que no contactan a pacientes (staff,
escrituras, etc.) pasan sin tocar. Read-only; no ejecuta nada.
"""
import logging

from . import learning, ledger, runtime

log = logging.getLogger("bot")


def _patient_phone(action) -> str:
    """Dígitos del paciente si la acción lo contacta; '' si no aplica."""
    if not getattr(action, "requires_contact", False) or getattr(action, "is_staff", False):
        return ""
    return ledger._resolve_phone(getattr(action, "target", None))


def _value(action) -> float:
    """Valor monetario del contacto. Toma params['valor'] si viene, si no el ticket."""
    try:
        v = (getattr(action, "params", None) or {}).get("valor")
        if v is not None:
            return float(v)
    except Exception:  # noqa: BLE001
        pass
    return float(ledger.AVG_TICKET_CLP)


def expected_value(agent_id: str, action) -> float:
    """EV = P(conversión|agente,variante) × valor × prioridad del dueño.

    La prioridad (runtime.agent_weight) deja que el plano de control incline la
    subasta: 'esta semana enfócate en kine' sube el peso del agente de kine."""
    p = learning.expected_conversion(agent_id, getattr(action, "variant", None))
    w = runtime.agent_weight(agent_id)
    return round(p * _value(action) * w, 2)


def resolve(proposals: list) -> dict:
    """Resuelve la competencia por contacto.

    `proposals`: lista de {"agent_id": str, "action": AgentAction}.
    Devuelve {winners, suppressed, by_patient, passthrough}. Anota
    action.expected_value en cada acción de contacto.
    """
    by_patient: dict[str, list] = {}
    passthrough = []  # acciones que no compiten (no contactan paciente)
    for p in proposals:
        agent_id = p.get("agent_id") if isinstance(p, dict) else p[0]
        action = p.get("action") if isinstance(p, dict) else p[1]
        phone = _patient_phone(action)
        if not phone:
            passthrough.append({"agent_id": agent_id, "action": action})
            continue
        ev = expected_value(agent_id, action)
        try:
            action.expected_value = ev
        except Exception:  # noqa: BLE001
            pass
        by_patient.setdefault(phone, []).append(
            {"agent_id": agent_id, "action": action, "ev": ev})

    winners, suppressed, summary = [], [], []
    for phone, contenders in by_patient.items():
        # Mayor EV gana; desempate estable por agent_id.
        contenders.sort(key=lambda c: (-c["ev"], c["agent_id"]))
        win = contenders[0]
        winners.append(win)
        for c in contenders[1:]:
            suppressed.append(c)
        summary.append({
            "paciente": ledger._digits(phone)[-4:].rjust(4, "·"),
            "n_contendientes": len(contenders),
            "ganador": win["agent_id"], "ev_ganador": win["ev"],
            "suprimidos": [{"agent_id": c["agent_id"], "ev": c["ev"]} for c in contenders[1:]],
        })

    return {
        "winners": winners,
        "suppressed": suppressed,
        "passthrough": passthrough,
        "by_patient": sorted(summary, key=lambda s: s["n_contendientes"], reverse=True),
        "n_pacientes": len(by_patient),
        "n_suprimidos": len(suppressed),
    }
