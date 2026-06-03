"""Orquestador: resultados de examen listos sin avisar.

Señal: resultados que recepción marcó como recibidos pero que el paciente aún no
sabe (tabla `resultados_pendientes`). Acción: worklist para avisar (o reenviar el
template `informe_listo`). Un resultado que queda sin avisar es ansiedad del
paciente y una atención de seguimiento perdida. Propone (no auto-contacta)."""
from .base import Orchestrator, Proposal


class ResultadosExamenesOrq(Orchestrator):
    name = "resultados_examenes"
    descripcion = "Resultados recibidos sin avisar → worklist de notificación"
    dominio = "clínico"
    cadencia = "cron:12:00"

    async def sense(self) -> dict:
        from session import get_resultados_para_avisar
        pend = [r for r in get_resultados_para_avisar() if r.get("phone")]
        return {"source_status": "ok", "pendientes": pend}

    async def propose(self, signals: dict) -> list[Proposal]:
        pend = signals.get("pendientes", [])
        if not pend:
            return []

        def _row(r):
            tel = (r.get("phone") or "").lstrip("+")
            return {"nombre": r.get("nombre") or "paciente", "examen": r.get("examen") or "examen",
                    "telefono": r.get("phone"), "recibido_at": r.get("recibido_at"),
                    "resultado_id": r.get("id"),
                    "wa": f"https://wa.me/{tel}" if tel else ""}

        return [Proposal(
            kind="tarea_manual",
            summary=f"Avisar {len(pend)} resultado(s) de examen ya recibido(s)",
            rationale="Resultados que llegaron pero el paciente no sabe. Avisar a tiempo baja la "
                      "ansiedad y rescata la atención de seguimiento (informe_listo).",
            params={"worklist": [_row(r) for r in pend]},
            impact="Seguimiento clínico / experiencia del paciente",
        )]


ORQ = ResultadosExamenesOrq()
