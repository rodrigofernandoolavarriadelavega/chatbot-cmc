"""Orquestador: confirmaciones de citas de mañana.

Señal: citas del bot para mañana que aún NO están confirmadas. Acción: worklist
para que recepción confirme (o el bot reenvíe el template de confirmación). Una
cita no confirmada es un no-show probable y un cupo que podría ir a la lista de
espera. Propone (no auto-contacta)."""
from datetime import date, timedelta

from .base import Orchestrator, Proposal


class ConfirmacionesOrq(Orchestrator):
    name = "confirmaciones"
    descripcion = "Citas de mañana sin confirmar → worklist de confirmación"
    dominio = "agenda"
    cadencia = "cron:18:00"

    async def sense(self) -> dict:
        from session import get_confirmaciones_dia
        manana = (date.today() + timedelta(days=1)).isoformat()
        citas = get_confirmaciones_dia(manana)
        sin_confirmar = [c for c in citas
                         if (c.get("confirmation_status") or "").lower()
                         not in ("confirmado", "confirmada", "si", "sí")]
        return {"source_status": "ok", "fecha": manana,
                "total": len(citas), "sin_confirmar": sin_confirmar}

    async def propose(self, signals: dict) -> list[Proposal]:
        sin = signals.get("sin_confirmar", [])
        if not sin:
            return []

        def _row(c):
            tel = (c.get("phone") or "").lstrip("+")
            return {"hora": c.get("hora"), "especialidad": c.get("especialidad"),
                    "profesional": c.get("profesional"), "telefono": c.get("phone"),
                    "estado": c.get("confirmation_status") or "sin respuesta",
                    "wa": f"https://wa.me/{tel}" if tel else ""}

        return [Proposal(
            kind="tarea_manual",
            summary=f"Confirmar {len(sin)} cita(s) de mañana ({signals.get('fecha')}) sin confirmar",
            rationale="Una cita sin confirmar es un no-show probable. Confirmarlas hoy reduce "
                      "inasistencias y, si alguien no puede, libera el cupo para la lista de "
                      "espera (engancha con el orquestador hueco_proactivo / Fase 4).",
            params={"fecha": signals.get("fecha"), "worklist": [_row(c) for c in sin[:80]]},
            impact="Reducción de no-shows / ocupación de agenda",
        )]


ORQ = ConfirmacionesOrq()
