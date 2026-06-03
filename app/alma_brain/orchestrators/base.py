"""Chasis común de los orquestadores de Alma.

Un orquestador SIENTE señales de un dominio (read-only), PROPONE acciones
concretas, y solo EJECUTA lo reversible si la política lo permite. Hereda el ADN
de alma_brain: la IA propone, las reglas mandan.

Garantías estructurales (las cumple el chasis, no cada orquestador):
  - OFF por defecto: kill-switch dedicado `ALMA_ORQ_<NAME>_ENABLED` (default false).
  - dry-run siempre seguro: `mode="dry"` no persiste ni ejecuta, ni siquiera cuando
    el orquestador está apagado → el panel puede previsualizar todo sin riesgo.
  - degradación elegante: si `sense()` falla, se devuelve vacío con el error en
    `signals`, nunca propaga la excepción ni tumba al caller (cron/copiloto).
  - trazabilidad: las propuestas pasan por la cola existente (`tools.add_proposal`)
    y los límites duros por `policy.check`.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

log = logging.getLogger("bot")


def _flag(name: str, default: str = "false") -> bool:
    env_val = os.getenv(name, default).lower() in ("true", "1", "yes")
    try:
        from alma_switchboard import effective
        return effective(name, env_val)
    except Exception:
        return env_val


@dataclass
class Proposal:
    """Acción concreta que un orquestador recomienda. Se mapea 1:1 a la cola de
    propuestas existente de alma_brain.tools."""
    kind: str          # tipo de acción (lo evalúa policy.check). 'tarea_manual' = siempre a humano.
    summary: str       # qué hacer, 1 frase clara
    rationale: str     # por qué, apoyado en datos
    params: dict = field(default_factory=dict)
    impact: str = ""   # a qué KPI mueve

    def as_dict(self) -> dict:
        return {"kind": self.kind, "summary": self.summary, "rationale": self.rationale,
                "params": self.params, "impact": self.impact}


@dataclass
class OrqResult:
    name: str
    enabled: bool
    mode: str
    proposals: list[dict]          # lo que haría (Proposal.as_dict)
    executed: list[dict] = field(default_factory=list)   # lo que efectivamente hizo
    signals: dict = field(default_factory=dict)          # lo que sintió (transparencia)
    notes: str = ""

    def as_dict(self) -> dict:
        return {"name": self.name, "enabled": self.enabled, "mode": self.mode,
                "proposals": self.proposals, "executed": self.executed,
                "signals": self.signals, "notes": self.notes}


class Orchestrator:
    """Base. Subclasea y define `name`, `descripcion`, `sense()` y `propose()`.

    El método `run()` orquesta el gating, la persistencia y la ejecución. La
    mayoría de orquestadores SOLO proponen (Fase 3); auto-ejecutar es opt-in y se
    delega a `execute_proposal()` (que por defecto deja la propuesta para humano)."""

    name: str = "base"
    descripcion: str = ""
    dominio: str = ""
    cadencia: str = "manual"   # "evento" | "cron:HH:MM" | "manual" (informativo)

    # ── gating ───────────────────────────────────────────────────────────────
    def env_flag(self) -> str:
        return f"ALMA_ORQ_{self.name.upper()}_ENABLED"

    def enabled(self) -> bool:
        return _flag(self.env_flag())

    # ── ciclo (lo implementa cada orquestador) ───────────────────────────────
    async def sense(self) -> dict:
        """Señales del dominio. Read-only. Debe degradar con gracia por su cuenta
        o dejar que run() capture la excepción."""
        return {}

    async def propose(self, signals: dict) -> list[Proposal]:
        """Traduce señales en acciones concretas. Sin I/O de escritura."""
        return []

    async def execute_proposal(self, prop: Proposal) -> dict:
        """Ejecuta una propuesta YA aprobada por política. Por defecto NO actúa:
        deja la acción para humano. Los orquestadores que auto-ejecutan lo
        sobreescriben con su acción reversible concreta."""
        return {"executed": False, "reason": "sin executor — queda como tarea manual"}

    # ── runner ────────────────────────────────────────────────────────────────
    async def run(self, *, mode: str = "dry") -> OrqResult:
        """mode: 'dry' (preview, no persiste) | 'propose' (encola) | 'execute' (encola + auto-ejecuta)."""
        enabled = self.enabled()

        # dry-run es seguro siempre; los modos que actúan exigen el flag encendido.
        if mode != "dry" and not enabled:
            return OrqResult(self.name, enabled, mode, proposals=[],
                             notes=f"gateado OFF ({self.env_flag()}=false)")

        try:
            signals = await self.sense()
        except Exception as e:  # noqa: BLE001 — un sensor caído nunca tumba el runner
            log.warning("orq %s: sense() falló: %s", self.name, e)
            return OrqResult(self.name, enabled, mode, proposals=[],
                             signals={"source_status": "error", "error": str(e)},
                             notes="sense() degradó a vacío")

        try:
            proposals = await self.propose(signals)
        except Exception as e:  # noqa: BLE001
            log.warning("orq %s: propose() falló: %s", self.name, e)
            return OrqResult(self.name, enabled, mode, proposals=[], signals=signals,
                             notes=f"propose() falló: {e}")

        prop_dicts = [p.as_dict() for p in proposals]

        if mode == "dry":
            return OrqResult(self.name, enabled, mode, proposals=prop_dicts, signals=signals,
                             notes="dry-run — nada persistido ni ejecutado")

        # propose / execute: encolar y (opcionalmente) ejecutar lo permitido.
        from .. import tools, policy
        executed = []
        for p in proposals:
            enq = tools.add_proposal(p.as_dict())
            if mode == "execute":
                ok, reason = policy.check(p.kind, p.params)
                if ok:
                    try:
                        res = await self.execute_proposal(p)
                    except Exception as e:  # noqa: BLE001
                        res = {"executed": False, "error": str(e)}
                    res["proposal_id"] = enq["id"]
                    executed.append(res)
                else:
                    executed.append({"executed": False, "proposal_id": enq["id"], "reason": reason})

        return OrqResult(self.name, enabled, mode, proposals=prop_dicts, executed=executed,
                         signals=signals,
                         notes=f"{len(prop_dicts)} propuesta(s) encolada(s)")

    def meta(self) -> dict:
        return {"name": self.name, "descripcion": self.descripcion, "dominio": self.dominio,
                "cadencia": self.cadencia, "flag": self.env_flag(), "enabled": self.enabled()}
