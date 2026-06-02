"""Orquestador: adherencia de Kinesiología.

Señal: pacientes kine en 'riesgo' (ventana recuperable) o 'abandono' según el
módulo /alma/kine. Acción: worklist de pacientes a contactar para retomar
sesiones, con deep-link WhatsApp. Propone (no auto-contacta): la recepción/kine
ejecuta. La adherencia es el lever de valor del programa kine (multi-sesión)."""
from .base import Orchestrator, Proposal

# Cuántos contactar por corrida (no saturar). Riesgo primero (más recuperable).
_TOP = 15


class AdherenciaKineOrq(Orchestrator):
    name = "adherencia_kine"
    descripcion = "Pacientes kine en riesgo/abandono → worklist de reenganche"
    dominio = "kinesiología"
    cadencia = "cron:09:00"

    async def sense(self) -> dict:
        from kine_routes import _compute, ensure_kine_plan_table
        ensure_kine_plan_table()
        data = _compute(meses=6)
        pac = data.get("pacientes", [])
        riesgo = [p for p in pac if p.get("estado") == "riesgo" and p.get("telefono")]
        abandono = [p for p in pac if p.get("estado") == "abandono" and p.get("telefono")]
        return {"source_status": data.get("source_status", "?"),
                "kpis": data.get("kpis", {}),
                "riesgo": riesgo, "abandono": abandono}

    async def propose(self, signals: dict) -> list[Proposal]:
        riesgo = signals.get("riesgo", [])
        abandono = signals.get("abandono", [])
        worklist = (riesgo + abandono)[:_TOP]
        if not worklist:
            return []

        def _row(p):
            return {"paciente": p["paciente"], "telefono": p["telefono"],
                    "estado": p["estado"], "dias_sin_sesion": p.get("dias_sin_sesion"),
                    "profesional": p.get("profesional", ""),
                    "wa": f"https://wa.me/{p['telefono'].lstrip('+')}"}

        return [Proposal(
            kind="tarea_manual",
            summary=f"Reenganchar {len(worklist)} paciente(s) kine ({len(riesgo)} en riesgo, "
                    f"{len(abandono)} en abandono)",
            rationale="La adherencia es el lever del programa kine. Pacientes en 'riesgo' "
                      "están dentro de la ventana recuperable; los de 'abandono' valen un "
                      "último intento. Contacto manual con deep-link de WhatsApp.",
            params={"worklist": [_row(p) for p in worklist],
                    "kpis": signals.get("kpis", {})},
            impact="Sesiones completadas / ingreso del programa kine",
        )]


ORQ = AdherenciaKineOrq()
