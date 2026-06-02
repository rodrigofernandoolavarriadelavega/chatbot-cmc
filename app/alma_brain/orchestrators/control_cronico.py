"""Orquestador: control de pacientes crónicos.

Señal: pacientes con tag dx:* (HTA, DM2, EPOC, etc. — patología crónica detectada
por el bot/doctor) cuya última cita registrada es vieja (>90d) o inexistente.
Acción: worklist para recordarles su control crónico. Un crónico descontrolado
es riesgo clínico y, además, atención recurrente perdida. Propone (no auto-contacta)."""
from .base import Orchestrator, Proposal

_DIAS = 90
_TOP = 30


class ControlCronicoOrq(Orchestrator):
    name = "control_cronico"
    descripcion = "Pacientes crónicos (dx:*) sin control reciente → recordar control"
    dominio = "clínico"
    cadencia = "cron:10:00"

    async def sense(self) -> dict:
        from session import get_cronicos_para_control
        cand = [c for c in get_cronicos_para_control(_DIAS) if c.get("phone")]
        return {"source_status": "ok", "candidatos": cand}

    async def propose(self, signals: dict) -> list[Proposal]:
        cand = signals.get("candidatos", [])[:_TOP]
        if not cand:
            return []

        def _row(c):
            tel = (c["phone"] or "").lstrip("+")
            return {"nombre": c.get("nombre") or "paciente", "telefono": c["phone"],
                    "dx": ", ".join(c.get("dxs", [])), "ultima_cita": c.get("ultima") or "sin registro",
                    "wa": f"https://wa.me/{tel}" if tel else ""}

        return [Proposal(
            kind="tarea_manual",
            summary=f"Recordar control crónico a {len(cand)} paciente(s) sin atención reciente",
            rationale="Pacientes con patología crónica (HTA/DM2/etc.) que no registran cita hace "
                      ">90 días. Un crónico sin control es riesgo clínico y atención recurrente "
                      "perdida. Recordatorio cálido de control (manual).",
            params={"worklist": [_row(c) for c in cand]},
            impact="Adherencia de crónicos / control de riesgo clínico / recurrencia",
        )]


ORQ = ControlCronicoOrq()
