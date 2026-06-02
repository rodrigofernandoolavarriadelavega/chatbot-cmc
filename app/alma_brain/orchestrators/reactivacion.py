"""Orquestador: reactivación / win-back de cohortes inactivas.

Señal: pacientes inactivos contactables (sensor de fidelización). Acción: propone
una campaña de win-back dirigida. NO auto-contacta: el contacto masivo a la base
de pacientes toca la Ley 21.719 (consent + Custom Audiences hasheadas) y eso lo
gobierna policy.check, no este orquestador. Por eso propone como tarea para el
flujo de win-back ya existente."""
from .base import Orchestrator, Proposal


class ReactivacionOrq(Orchestrator):
    name = "reactivacion"
    descripcion = "Cohortes inactivas contactables → proponer win-back dirigido"
    dominio = "fidelización"
    cadencia = "cron:10:00"

    async def sense(self) -> dict:
        from .. import sensors
        f = sensors.sense_fidelizacion()
        if not f.get("available"):
            return {"source_status": "unavailable", "contactables": 0}
        return {"source_status": "ok", "kpis": f.get("kpis", {}),
                "contactables": f.get("kpis", {}).get("cohortes_contactables", 0)}

    async def propose(self, signals: dict) -> list[Proposal]:
        n = signals.get("contactables", 0)
        if not n:
            return []
        return [Proposal(
            # Kind de contacto: policy.check exige consent + templates (Ley 21.719).
            # Aun aprobado, la ejecución real pasa por el flujo de win-back + Custom
            # Audiences, NO por un blast directo desde acá.
            kind="reactivacion_blast",
            summary=f"Win-back dirigido a {n} paciente(s) inactivo(s) contactable(s)",
            rationale="Hay una cohorte inactiva con consentimiento. Buen momento para un "
                      "win-back segmentado (no genérico): personalizar por última especialidad "
                      "y motivo. Ejecutar vía el flujo de consent + Custom Audiences.",
            params={"n_destinatarios": n, "kpis": signals.get("kpis", {})},
            impact="Pacientes reactivados / agenda",
        )]


ORQ = ReactivacionOrq()
