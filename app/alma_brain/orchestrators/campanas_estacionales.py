"""Orquestador: campañas estacionales.

Señal: el mes del año. Acción: propone la campaña de salud estacional pertinente
(invierno respiratorio, vuelta a clases, mes del corazón, etc.) como tarea de
planificación para marketing. Lógica de calendario (sin red), siempre disponible.
Propone (no auto-ejecuta)."""
from .base import Orchestrator, Proposal

# mes → (tema, especialidades gancho)
_CALENDARIO = {
    1:  ("Vacaciones sanas / chequeo de verano", "medicina general, dermatología"),
    3:  ("Vuelta a clases: salud escolar", "pediatría, oftalmología, fonoaudiología"),
    4:  ("Otoño: prevención respiratoria", "medicina general, otorrino, kinesiología respiratoria"),
    5:  ("Invierno: vacunación y respiratorio", "medicina general, kinesiología respiratoria"),
    6:  ("Invierno: respiratorio agudo", "medicina general, otorrino"),
    8:  ("Mes del corazón: salud cardiovascular", "cardiología, nutrición"),
    9:  ("Primavera: alergias y control", "otorrino, medicina general"),
    10: ("Mes de la salud mental", "psicología"),
    11: ("Mes de la diabetes", "medicina general, nutrición"),
    12: ("Cierre de año: pendientes de salud", "odontología, chequeos"),
}


class CampanasEstacionalesOrq(Orchestrator):
    name = "campanas_estacionales"
    descripcion = "Campaña de salud pertinente al mes → planificar"
    dominio = "marketing"
    cadencia = "cron:mensual"

    async def sense(self) -> dict:
        # Sin Date.now reproducible en workflows, pero acá corremos en proceso real.
        from datetime import date
        mes = date.today().month
        tema, esp = _CALENDARIO.get(mes, (None, None))
        return {"source_status": "ok", "mes": mes, "tema": tema, "especialidades": esp}

    async def propose(self, signals: dict) -> list[Proposal]:
        tema = signals.get("tema")
        if not tema:
            return []
        return [Proposal(
            kind="tarea_manual",
            summary=f"Activar campaña estacional: {tema}",
            rationale=f"El mes actual corresponde a '{tema}'. Preparar contenido + segmento "
                      f"(especialidades gancho: {signals.get('especialidades')}). Engancha con "
                      "el módulo de Email/Diseños del Autopilot.",
            params={"mes": signals.get("mes"), "tema": tema,
                    "especialidades": signals.get("especialidades")},
            impact="Captación estacional / ocupación de agenda",
        )]


ORQ = CampanasEstacionalesOrq()
