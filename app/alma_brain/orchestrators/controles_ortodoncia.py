"""Orquestador: controles de Ortodoncia vencidos.

Señal: pacientes con control vencido (>45d sin control) según /alma/ortodoncia.
Acción: worklist de recordatorio de control. El tratamiento ortodóntico es largo
y depende de controles periódicos — un control saltado alarga el tratamiento y
deteriora el resultado clínico. Propone (no auto-contacta)."""
from .base import Orchestrator, Proposal

_TOP = 20


class ControlesOrtodonciaOrq(Orchestrator):
    name = "controles_ortodoncia"
    descripcion = "Controles de ortodoncia vencidos → worklist de recordatorio"
    dominio = "ortodoncia"
    cadencia = "cron:09:00"

    async def sense(self) -> dict:
        from ortodoncia_routes import _compute, ensure_ortodoncia_plan_table
        ensure_ortodoncia_plan_table()
        data = _compute(meses=12)
        pac = data.get("pacientes", [])
        vencidos = [p for p in pac if p.get("estado") == "vencido" and p.get("telefono")]
        return {"source_status": data.get("source_status", "?"),
                "kpis": data.get("kpis", {}), "vencidos": vencidos}

    async def propose(self, signals: dict) -> list[Proposal]:
        vencidos = signals.get("vencidos", [])[:_TOP]
        if not vencidos:
            return []

        def _row(p):
            return {"paciente": p["paciente"], "telefono": p["telefono"],
                    "dias_sin_control": p.get("dias_sin_control"),
                    "meses_tratamiento": p.get("meses_tratamiento"),
                    "wa": f"https://wa.me/{p['telefono'].lstrip('+')}"}

        return [Proposal(
            kind="tarea_manual",
            summary=f"Recordar control a {len(vencidos)} paciente(s) de ortodoncia con control vencido",
            rationale="El tratamiento ortodóntico depende de controles periódicos. Un control "
                      "saltado alarga el tratamiento y empeora el resultado. Recordatorio manual.",
            params={"worklist": [_row(p) for p in vencidos], "kpis": signals.get("kpis", {})},
            impact="Controles al día / duración del tratamiento / satisfacción",
        )]


ORQ = ControlesOrtodonciaOrq()
