"""Orquestador: medicina preventiva por edad/sexo.

Señal: pacientes en ventanas de examen preventivo (GES/EMPA chileno). Acción:
worklists para invitar a su examen preventivo según edad y sexo. Es captación con
propósito clínico (no spam): exámenes que el sistema público recomienda por edad.
Propone (no auto-contacta); contacto masivo pasa por consent (Ley 21.719)."""
from .base import Orchestrator, Proposal

_CAP = 25  # tope por examen para que la worklist sea accionable


# (clave, etiqueta, sexo|None, edad_min, edad_max)
_REGLAS = [
    ("pap", "PAP (citología cervical)", "f", 25, 64),
    ("mamografia", "Mamografía", "f", 50, 69),
    ("psa", "Antígeno prostático (PSA)", "m", 50, 70),
    ("empam", "EMPAM (examen adulto mayor)", None, 65, 120),
    ("empa", "EMPA (examen medicina preventiva)", None, 40, 64),
]


def _es_femenino(genero: str) -> bool:
    return (genero or "").strip().lower().startswith("f")


class PreventivoEdadOrq(Orchestrator):
    name = "preventivo_edad"
    descripcion = "Pacientes en ventana de examen preventivo → invitar"
    dominio = "clínico"
    cadencia = "cron:11:00"

    async def sense(self) -> dict:
        from .. import sensors
        rows = sensors._bi_rows("""
            SELECT nombre, apellido, telefono, genero,
                   date_part('year', age(fecha_nacimiento))::int AS edad
            FROM bi.dim_paciente
            WHERE fecha_nacimiento IS NOT NULL
              AND telefono IS NOT NULL AND telefono <> ''
              AND date_part('year', age(fecha_nacimiento)) BETWEEN 25 AND 120
        """)
        if rows is None:
            return {"source_status": "unavailable", "pacientes": []}
        pac = [{"nombre": (r[0] or "").strip(), "apellido": (r[1] or "").strip(),
                "telefono": (r[2] or "").strip(), "genero": r[3], "edad": r[4]}
               for r in rows]
        return {"source_status": "ok", "pacientes": pac}

    async def propose(self, signals: dict) -> list[Proposal]:
        pac = signals.get("pacientes", [])
        if not pac:
            return []
        out = []
        for clave, etiqueta, sexo, emin, emax in _REGLAS:
            cand = []
            for p in pac:
                if p["edad"] is None or not (emin <= p["edad"] <= emax):
                    continue
                if sexo == "f" and not _es_femenino(p["genero"]):
                    continue
                if sexo == "m" and _es_femenino(p["genero"]):
                    continue
                tel = p["telefono"].lstrip("+")
                cand.append({"nombre": f"{p['nombre']} {p['apellido']}".strip(),
                             "edad": p["edad"], "telefono": p["telefono"],
                             "wa": f"https://wa.me/{tel}"})
            if not cand:
                continue
            out.append(Proposal(
                kind="tarea_manual",
                summary=f"Invitar a {etiqueta}: {len(cand)} paciente(s) en ventana de edad",
                rationale=f"Pacientes en la ventana de {etiqueta} (edad {emin}-{emax}"
                          f"{', mujeres' if sexo == 'f' else ', hombres' if sexo == 'm' else ''}). "
                          "Captación con propósito clínico (examen que el sistema recomienda por edad). "
                          "Contacto masivo requiere consent (Ley 21.719).",
                params={"examen": clave, "worklist": cand[:_CAP], "total_elegibles": len(cand)},
                impact="Cobertura preventiva / captación con propósito clínico",
            ))
        return out


ORQ = PreventivoEdadOrq()
