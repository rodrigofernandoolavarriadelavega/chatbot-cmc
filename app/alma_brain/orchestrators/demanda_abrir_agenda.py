"""Orquestador: demanda insatisfecha → abrir agenda.

Señal: especialidades con solicitudes acumuladas sin cupo (sensor de demanda).
Acción: propone evaluar abrir más agenda / avisar al médico / activar campaña.
Es una decisión de negocio (humana), así que SOLO propone."""
from .base import Orchestrator, Proposal

# Umbral mínimo de solicitudes para que valga la pena la señal.
_MIN_SOLICITUDES = 5


class DemandaAbrirAgendaOrq(Orchestrator):
    name = "demanda_abrir_agenda"
    descripcion = "Especialidad con demanda sin cupo → proponer abrir agenda"
    dominio = "demanda"
    cadencia = "cron:08:00"

    async def sense(self) -> dict:
        from .. import sensors
        d = sensors.sense_demanda(90)
        if not d.get("available"):
            return {"source_status": "unavailable", "servicios": []}
        servicios = [s for s in (d.get("servicios_sin_cupo") or [])
                     if (s.get("solicitudes") or 0) >= _MIN_SOLICITUDES]
        return {"source_status": "ok", "servicios": servicios}

    async def propose(self, signals: dict) -> list[Proposal]:
        out = []
        for s in signals.get("servicios", [])[:6]:
            esp = s.get("especialidad", "?")
            sol = s.get("solicitudes", 0)
            personas = s.get("personas", "?")
            out.append(Proposal(
                kind="tarea_manual",
                summary=f"Evaluar abrir agenda de '{esp}': {sol} solicitudes sin cupo ({personas} personas)",
                rationale=f"'{esp}' acumula {sol} solicitudes que no encontraron cupo en 90 días. "
                          "Demanda comprobada sin oferta: candidato a abrir más horas, sumar día "
                          "del especialista, o activar una campaña dirigida.",
                params={"especialidad": esp, "solicitudes": sol, "personas": personas},
                impact="Conversión de demanda represada / ingreso por especialidad",
            ))
        return out


ORQ = DemandaAbrirAgendaOrq()
