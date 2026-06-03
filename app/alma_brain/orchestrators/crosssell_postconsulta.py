"""Orquestador: cross-sell post-consulta contextual.

Señal: pacientes que respondieron 'mejor' al seguimiento, agrupados por la
especialidad que tuvieron. Acción: worklist de cross-sell contextual (no genérico):
el paciente satisfecho de traumatología es candidato natural a kinesiología, etc.
Propone (no auto-contacta)."""
from .base import Orchestrator, Proposal

# especialidad atendida (canon, substring) → (servicio sugerido, gancho)
_MAPA = [
    ("traumat",   "Kinesiología",         "completar la recuperación con kine"),
    ("kinesio",   "Masoterapia",          "complementar las sesiones con masoterapia"),
    ("otorrin",   "Fonoaudiología",       "evaluación fonoaudiológica"),
    ("fonoaud",   "Otorrinolaringología", "control con otorrino"),
    ("odonto",    "Estética facial",      "armonización / estética facial"),
    ("medicina",  "Chequeo preventivo",   "un chequeo preventivo acorde a su edad"),
    ("nutri",     "Control nutricional",  "control de seguimiento nutricional"),
]


def _sugerencia(esp: str):
    e = (esp or "").lower()
    for raiz, servicio, gancho in _MAPA:
        if raiz in e:
            return servicio, gancho
    return None, None


class CrosssellPostconsultaOrq(Orchestrator):
    name = "crosssell_postconsulta"
    descripcion = "Pacientes satisfechos → cross-sell contextual por especialidad"
    dominio = "fidelización"
    cadencia = "cron:11:00"

    async def sense(self) -> dict:
        from session import get_promotores_recientes
        prom = [p for p in get_promotores_recientes(30, limit=60) if p.get("phone")]
        return {"source_status": "ok", "promotores": prom}

    async def propose(self, signals: dict) -> list[Proposal]:
        buckets: dict[str, list] = {}
        for p in signals.get("promotores", []):
            servicio, gancho = _sugerencia(p.get("especialidad", ""))
            if not servicio:
                continue
            tel = p["phone"].lstrip("+")
            buckets.setdefault((servicio, gancho), []).append({
                "nombre": p.get("nombre") or "paciente", "telefono": p["phone"],
                "venia_de": p.get("especialidad", ""), "wa": f"https://wa.me/{tel}"})

        out = []
        for (servicio, gancho), wl in buckets.items():
            out.append(Proposal(
                kind="tarea_manual",
                summary=f"Cross-sell a {servicio}: {len(wl)} paciente(s) satisfecho(s)",
                rationale=f"Pacientes que quedaron conformes son candidatos naturales a {gancho}. "
                          "Cross-sell contextual (no genérico) tras una buena experiencia.",
                params={"servicio": servicio, "worklist": wl[:25]},
                impact="Ingreso por paciente / utilización de especialidades",
            ))
        return out


ORQ = CrosssellPostconsultaOrq()
