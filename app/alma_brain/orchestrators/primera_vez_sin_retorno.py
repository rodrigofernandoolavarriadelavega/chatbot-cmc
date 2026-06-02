"""Orquestador: retención de pacientes de primera vez.

Señal: pacientes que se atendieron UNA sola vez hace 30–120 días y no volvieron.
Es el agujero de retención más caro: pagaste el CAC, viniste una vez, y se perdió.
Acción: worklist de reenganche suave ("¿cómo seguiste? ¿agendamos control?").
Propone (no auto-contacta). Ventana 30–120d: deja pasar el mes natural de
recompra y no molesta a quien recién vino."""
from .base import Orchestrator, Proposal

_TOP = 30


class PrimeraVezSinRetornoOrq(Orchestrator):
    name = "primera_vez_sin_retorno"
    descripcion = "Pacientes de 1 sola atención (30–120d) sin volver → reenganche"
    dominio = "fidelización"
    cadencia = "cron:10:00"

    async def sense(self) -> dict:
        from .. import sensors
        rows = sensors._bi_rows("""
            SELECT p.nombre, p.apellido, p.telefono,
                   MAX(fa.fecha)::text AS ultima, COUNT(*) AS n
            FROM bi.fact_atenciones fa
            JOIN bi.dim_paciente p ON p.paciente_id = fa.paciente_id
            WHERE p.telefono IS NOT NULL AND p.telefono <> ''
            GROUP BY p.paciente_id, p.nombre, p.apellido, p.telefono
            HAVING COUNT(*) = 1
               AND MAX(fa.fecha) < CURRENT_DATE - INTERVAL '30 days'
               AND MAX(fa.fecha) > CURRENT_DATE - INTERVAL '120 days'
            ORDER BY MAX(fa.fecha) DESC
        """)
        if rows is None:
            return {"source_status": "unavailable", "pacientes": []}
        pac = [{"nombre": f"{(r[0] or '').strip()} {(r[1] or '').strip()}".strip(),
                "telefono": (r[2] or "").strip(), "ultima": r[3]} for r in rows]
        return {"source_status": "ok", "pacientes": pac}

    async def propose(self, signals: dict) -> list[Proposal]:
        pac = signals.get("pacientes", [])[:_TOP]
        if not pac:
            return []

        def _row(p):
            tel = p["telefono"].lstrip("+")
            return {"nombre": p["nombre"] or "paciente", "telefono": p["telefono"],
                    "ultima_atencion": p["ultima"], "wa": f"https://wa.me/{tel}"}

        return [Proposal(
            kind="tarea_manual",
            summary=f"Reenganchar {len(pac)} paciente(s) de primera vez que no volvieron",
            rationale="Vinieron una sola vez hace 1–4 meses y no regresaron — el agujero de "
                      "retención más caro (ya pagaste el CAC). Un mensaje cálido de seguimiento "
                      "('¿cómo seguiste?, ¿agendamos un control?') recupera parte de esa cohorte.",
            params={"worklist": [_row(p) for p in pac]},
            impact="Retención de pacientes nuevos / valor por paciente (LTV)",
        )]


ORQ = PrimeraVezSinRetornoOrq()
