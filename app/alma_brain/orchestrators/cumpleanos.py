"""Orquestador: cumpleaños del día.

Señal: pacientes que cumplen años hoy (BI dim_paciente). Acción: worklist de
saludo + tip preventivo por edad. Gesto de fidelización barato y de alto cariño.
Propone (no auto-contacta)."""
from .base import Orchestrator, Proposal


class CumpleanosOrq(Orchestrator):
    name = "cumpleanos"
    descripcion = "Pacientes que cumplen años hoy → saludo + tip preventivo"
    dominio = "fidelización"
    cadencia = "cron:08:00"

    async def sense(self) -> dict:
        from .. import sensors
        rows = sensors._bi_rows("""
            SELECT nombre, apellido, telefono,
                   date_part('year', age(fecha_nacimiento))::int AS edad
            FROM bi.dim_paciente
            WHERE fecha_nacimiento IS NOT NULL
              AND telefono IS NOT NULL AND telefono <> ''
              AND to_char(fecha_nacimiento, 'MM-DD') = to_char(current_date, 'MM-DD')
        """)
        if rows is None:
            return {"source_status": "unavailable", "cumpleaneros": []}
        cumple = [{"nombre": f"{(r[0] or '').strip()} {(r[1] or '').strip()}".strip(),
                   "telefono": (r[2] or "").strip(), "edad": r[3]} for r in rows]
        return {"source_status": "ok", "cumpleaneros": cumple}

    async def propose(self, signals: dict) -> list[Proposal]:
        cumple = signals.get("cumpleaneros", [])
        if not cumple:
            return []

        def _row(p):
            tel = p["telefono"].lstrip("+")
            return {"nombre": p["nombre"], "edad": p["edad"], "telefono": p["telefono"],
                    "wa": f"https://wa.me/{tel}"}

        return [Proposal(
            kind="tarea_manual",
            summary=f"Saludar a {len(cumple)} paciente(s) que cumplen años hoy",
            rationale="Gesto de fidelización de alto cariño y bajo costo. Saludo + un tip "
                      "preventivo acorde a la edad (engancha con preventivo_edad).",
            params={"worklist": [_row(p) for p in cumple]},
            impact="Fidelización / recompra",
        )]


ORQ = CumpleanosOrq()
