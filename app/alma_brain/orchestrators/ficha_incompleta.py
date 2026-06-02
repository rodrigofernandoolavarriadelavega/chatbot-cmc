"""Orquestador: fichas de paciente incompletas (calidad de datos).

Señal: pacientes con teléfono pero a los que les falta email, comuna o fecha de
nacimiento en la ficha (BI dim_paciente). Acción: worklist para completar datos
en la próxima interacción. Datos completos habilitan campañas (cumpleaños,
preventivo por edad, email marketing) y mejoran la atención. Propone."""
from .base import Orchestrator, Proposal

_TOP = 40


class FichaIncompletaOrq(Orchestrator):
    name = "ficha_incompleta"
    descripcion = "Pacientes con ficha incompleta (sin email/comuna/fecha_nac) → completar"
    dominio = "datos"
    cadencia = "cron:12:00"

    async def sense(self) -> dict:
        from .. import sensors
        rows = sensors._bi_rows("""
            SELECT nombre, apellido, telefono,
                   (COALESCE(NULLIF(TRIM(email), ''), '') = '')              AS sin_email,
                   (COALESCE(NULLIF(TRIM(comuna), ''), '') = '')             AS sin_comuna,
                   (fecha_nacimiento IS NULL)                                AS sin_fnac
            FROM bi.dim_paciente
            WHERE telefono IS NOT NULL AND telefono <> ''
              AND ( COALESCE(NULLIF(TRIM(email), ''), '') = ''
                 OR COALESCE(NULLIF(TRIM(comuna), ''), '') = ''
                 OR fecha_nacimiento IS NULL )
            LIMIT 400
        """)
        if rows is None:
            return {"source_status": "unavailable", "pacientes": []}
        pac = []
        for r in rows:
            faltan = []
            if r[3]: faltan.append("email")
            if r[4]: faltan.append("comuna")
            if r[5]: faltan.append("fecha nac.")
            pac.append({"nombre": f"{(r[0] or '').strip()} {(r[1] or '').strip()}".strip(),
                        "telefono": (r[2] or "").strip(), "faltan": faltan})
        return {"source_status": "ok", "pacientes": pac}

    async def propose(self, signals: dict) -> list[Proposal]:
        pac = signals.get("pacientes", [])
        if not pac:
            return []
        # Priorizar a quienes les falta más.
        pac.sort(key=lambda p: -len(p["faltan"]))
        top = pac[:_TOP]

        def _row(p):
            tel = p["telefono"].lstrip("+")
            return {"nombre": p["nombre"] or "paciente", "telefono": p["telefono"],
                    "faltan": ", ".join(p["faltan"]), "wa": f"https://wa.me/{tel}"}

        return [Proposal(
            kind="tarea_manual",
            summary=f"Completar ficha de {len(top)} paciente(s) (de {len(pac)} con datos faltantes)",
            rationale="Fichas incompletas bloquean campañas (cumpleaños, preventivo por edad, "
                      "email) y empeoran la atención. Completar email/comuna/fecha de nacimiento "
                      "en la próxima interacción o recepción.",
            params={"worklist": [_row(p) for p in top], "total": len(pac)},
            impact="Calidad de datos / habilita segmentación y campañas",
        )]


ORQ = FichaIncompletaOrq()
