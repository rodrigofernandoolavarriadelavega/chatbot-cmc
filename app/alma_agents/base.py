"""Contrato base de un agente autónomo de Alma.

Un agente percibe el mundo, decide acciones, y (si los guardrails lo permiten)
las ejecuta. El loop `run()` es idéntico para todos: lo que cambia es perceive/
decide/execute_one. Patrón calcado del Autopilot, pero generalizado a cualquier
dominio y con la capa de guardrails de la flota en el medio.

  perceive()      → dict de contexto (read-only; reusa fuentes existentes)
  decide(ctx)     → list[AgentAction] (acá vive la inteligencia; puede usar LLM)
  execute_one(a)  → efecto real de UNA acción (solo se llama si authorize() pasa)

Cada acción declara su riesgo y qué toca (contacto a paciente / escritura Medilink),
para que `guardrails.authorize()` aplique el piso correcto.
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import guardrails, store, ledger

log = logging.getLogger("bot")


@dataclass
class AgentAction:
    kind: str
    summary: str
    risk: str = "medio"                  # bajo | medio | alto | extremo
    target: str | None = None            # teléfono / rut / campaign_id / sku...
    params: dict = field(default_factory=dict)
    requires_contact: bool = False       # ¿manda WhatsApp a alguien?
    requires_medilink_write: bool = False
    is_staff: bool = False               # el contacto es a Rodrigo/recepción, no paciente
    variant: str | None = None           # "approach" usado (msg A/B, hora, segmento) → learning
    expected_value: float | None = None  # EV estimado (lo llena el broker, opcional)


@dataclass
class Agent:
    id: str
    name: str
    descr: str
    risk: str                            # riesgo nominal del agente (para el panel)
    flag: str                            # env var que lo habilita (default false)
    category: str = "general"
    # Spec de schedule para el scheduler (lo lee scheduler_hook). Ej:
    # {"hour": 7, "minute": 0} o {"minute": "*/30"}.
    schedule: dict = field(default_factory=dict)

    # ── A implementar por cada agente ────────────────────────────────────────
    async def perceive(self) -> dict:
        return {}

    async def decide(self, ctx: dict) -> list[AgentAction]:
        return []

    async def execute_one(self, action: AgentAction) -> dict:
        """Efecto real. Solo se invoca si guardrails.authorize(action) → True.
        Devuelve un dict con el resultado (para el log)."""
        return {"noop": True}

    # ── Loop común (no se sobreescribe) ──────────────────────────────────────
    async def run(self, force_dry_run: bool = False) -> dict:
        ran_at = datetime.now(timezone.utc).isoformat()
        result = {
            "agent_id": self.id, "name": self.name, "risk": self.risk,
            "ran_at": ran_at, "dry_run": True,
            "perceived": {}, "actions_proposed": [],
            "actions_executed": [], "actions_blocked": [], "notes": "",
        }

        # Gating maestro + por agente.
        if not guardrails.master_enabled():
            result["notes"] = "flota apagada (ALMA_AGENTS_ENABLED=false)"
            return result
        if not guardrails.agent_enabled(self.flag):
            result["notes"] = f"agente apagado ({self.flag}=false)"
            return result

        dry = force_dry_run or not guardrails.execute_enabled()
        result["dry_run"] = dry

        try:
            ctx = await self.perceive()
            result["perceived"] = ctx
            actions = await self.decide(ctx)
        except Exception as e:  # noqa: BLE001 — un agente nunca tumba al scheduler
            log.error("alma_agents[%s]: perceive/decide falló: %s", self.id, e)
            result["notes"] = f"error percibiendo/decidiendo: {e}"
            store.record_run(result)
            return result

        for a in actions:
            result["actions_proposed"].append(_action_dict(a))
            allow, reason = guardrails.authorize(a)
            if dry or not allow:
                result["actions_blocked"].append({**_action_dict(a),
                                                  "reason": reason if not allow else "dry-run"})
                continue
            try:
                out = await self.execute_one(a)
                result["actions_executed"].append({**_action_dict(a), "result": out})
            except Exception as e:  # noqa: BLE001
                log.error("alma_agents[%s]: execute_one falló: %s", self.id, e)
                result["actions_blocked"].append({**_action_dict(a), "reason": f"error: {e}"})

        result["notes"] = (f"{len(result['actions_proposed'])} propuestas · "
                           f"{len(result['actions_executed'])} ejecutadas · "
                           f"{len(result['actions_blocked'])} bloqueadas")
        store.record_run(result)
        # Effectiveness Ledger: registra los contactos a pacientes para medir
        # después si convirtieron (nunca lanza; no afecta el run).
        if result["actions_executed"]:
            ledger.record_result(result)
        log.info("alma_agents[%s]: %s", self.id, result["notes"])
        return result


def _action_dict(a: AgentAction) -> dict:
    return {
        "kind": a.kind, "summary": a.summary, "risk": a.risk,
        "target": a.target, "params": a.params,
        "requires_contact": a.requires_contact,
        "requires_medilink_write": a.requires_medilink_write,
        "is_staff": a.is_staff,
        "variant": a.variant, "expected_value": a.expected_value,
    }
