"""Orquestador: recuperación de inasistencias (proxy).

CMC no tiene un estado 'no-show' en Medilink (gotcha conocido); el mejor proxy es
una cita de ayer que nunca se confirmó. Acción: worklist para que recepción
verifique si asistió y, si no, reagende y libere el cupo. Propone (no auto-actúa).
"""
from datetime import date, timedelta

from .base import Orchestrator, Proposal


class NoShowRecoveryOrq(Orchestrator):
    name = "no_show_recovery"
    descripcion = "Citas de ayer sin confirmar → verificar inasistencia y reagendar"
    dominio = "agenda"
    cadencia = "cron:09:30"

    async def sense(self) -> dict:
        from session import get_confirmaciones_dia
        ayer = (date.today() - timedelta(days=1)).isoformat()
        citas = get_confirmaciones_dia(ayer)
        sospechosas = [c for c in citas
                       if (c.get("confirmation_status") or "").lower()
                       not in ("confirmado", "confirmada", "si", "sí")
                       and c.get("phone")]
        return {"source_status": "ok", "fecha": ayer,
                "total": len(citas), "sospechosas": sospechosas}

    async def propose(self, signals: dict) -> list[Proposal]:
        sosp = signals.get("sospechosas", [])
        if not sosp:
            return []

        def _row(c):
            tel = (c.get("phone") or "").lstrip("+")
            return {"hora": c.get("hora"), "especialidad": c.get("especialidad"),
                    "profesional": c.get("profesional"), "telefono": c.get("phone"),
                    "wa": f"https://wa.me/{tel}" if tel else ""}

        return [Proposal(
            kind="tarea_manual",
            summary=f"Verificar {len(sosp)} posible(s) inasistencia(s) de ayer ({signals.get('fecha')})",
            rationale="Citas de ayer que nunca se confirmaron — proxy de no-show (CMC no tiene "
                      "estado no-show en Medilink). Verificar asistencia; si no asistió, ofrecer "
                      "reagendar y registrar el patrón (un no-show recurrente cambia la política).",
            params={"fecha": signals.get("fecha"), "worklist": [_row(c) for c in sosp[:80]]},
            impact="Recuperación de pacientes perdidos / data de no-show",
        )]


ORQ = NoShowRecoveryOrq()
