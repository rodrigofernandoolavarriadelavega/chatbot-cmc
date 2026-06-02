"""Orquestador: salud de la agenda (meta-señal).

Señal: el volumen de citas cae fuerte vs la ventana previa (sensor de agenda).
Acción: propone activar palancas de captación/reactivación. Es un orquestador
'director': no contacta pacientes, dispara la decisión de encender otros frentes
(reactivacion, demanda, campañas). Propone."""
from .base import Orchestrator, Proposal

_CAIDA_PCT = -15.0   # umbral de caída para alertar
_SUBIDA_PCT = 25.0   # subida fuerte → señal de capacidad


class AgendaSaludOrq(Orchestrator):
    name = "agenda_salud"
    descripcion = "Caída/subida fuerte de agenda vs ventana previa → activar palancas"
    dominio = "agenda"
    cadencia = "cron:08:00"

    async def sense(self) -> dict:
        from .. import sensors
        a = sensors.sense_agenda(7)
        if not a.get("available"):
            return {"source_status": "unavailable"}
        k = a.get("kpis", {})
        return {"source_status": "ok",
                "delta_pct": k.get("delta_pct"),
                "citas": k.get("citas_ventana"),
                "citas_prev": k.get("citas_ventana_previa")}

    async def propose(self, signals: dict) -> list[Proposal]:
        d = signals.get("delta_pct")
        if d is None:
            return []
        out = []
        if d <= _CAIDA_PCT:
            out.append(Proposal(
                kind="tarea_manual",
                summary=f"Agenda cayó {abs(d):.0f}% vs la ventana previa "
                        f"({signals.get('citas')} vs {signals.get('citas_prev')}) — activar captación",
                rationale="Caída fuerte de citas. Buen momento para encender palancas: win-back "
                          "dirigido (reactivacion), abrir demanda represada (demanda_abrir_agenda) "
                          "o una campaña estacional. Revisar también el gasto de Meta Ads.",
                params={"delta_pct": d, "citas": signals.get("citas"),
                        "citas_prev": signals.get("citas_prev"),
                        "palancas": ["reactivacion", "demanda_abrir_agenda", "campanas_estacionales"]},
                impact="Recuperar volumen de agenda",
            ))
        elif d >= _SUBIDA_PCT:
            out.append(Proposal(
                kind="tarea_manual",
                summary=f"Agenda subió {d:.0f}% — vigilar capacidad y tiempos de espera",
                rationale="Subida fuerte de demanda atendida. Vigilar que no se sature la agenda "
                          "(esperas largas → fricción). Evaluar abrir más cupos en las especialidades calientes.",
                params={"delta_pct": d, "citas": signals.get("citas")},
                impact="Sostener calidad de servicio bajo demanda alta",
            ))
        return out


ORQ = AgendaSaludOrq()
