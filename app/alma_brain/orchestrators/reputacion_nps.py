"""Orquestador: reputación (NPS → reseñas y alertas).

Dos señales del seguimiento postconsulta:
  - Promotores ('mejor') recientes → worklist para pedirles reseña en Google
    (la reputación online es el lever de captación orgánica del CMC).
  - NPS bajo (global o por profesional) → alerta al dueño para revisar.
Propone (no auto-contacta)."""
from .base import Orchestrator, Proposal

_NPS_ALERTA = 0          # NPS global/por-prof bajo este umbral → alerta
_MIN_RESP = 5            # mínimo de respuestas para que el NPS de un prof sea señal


class ReputacionNpsOrq(Orchestrator):
    name = "reputacion_nps"
    descripcion = "Promotores → pedir reseña; NPS bajo → alertar"
    dominio = "reputación"
    cadencia = "cron:11:00"

    async def sense(self) -> dict:
        from session import get_nps_por_profesional, get_promotores_recientes
        nps = get_nps_por_profesional(30)
        promotores = [p for p in get_promotores_recientes(30) if p.get("phone")]
        return {"source_status": "ok",
                "global_nps": nps.get("global_nps", 0),
                "global_total": nps.get("global_total", 0),
                "por_profesional": nps.get("por_profesional", []),
                "promotores": promotores}

    async def propose(self, signals: dict) -> list[Proposal]:
        out = []

        promotores = signals.get("promotores", [])
        if promotores:
            def _row(p):
                return {"nombre": p.get("nombre") or "paciente", "telefono": p["phone"],
                        "profesional": p.get("profesional", ""),
                        "wa": f"https://wa.me/{p['phone'].lstrip('+')}"}
            out.append(Proposal(
                kind="tarea_manual",
                summary=f"Pedir reseña Google a {len(promotores)} promotor(es) recientes",
                rationale="Respondieron 'mejor' al seguimiento — momento ideal para pedir una "
                          "reseña. La reputación online es el lever de captación orgánica.",
                params={"worklist": [_row(p) for p in promotores]},
                impact="Reseñas Google / captación orgánica",
            ))

        # Alertas NPS bajo.
        if signals.get("global_total", 0) >= _MIN_RESP and signals.get("global_nps", 0) < _NPS_ALERTA:
            out.append(Proposal(
                kind="tarea_manual",
                summary=f"NPS global negativo ({signals['global_nps']}) — revisar experiencia",
                rationale=f"Sobre {signals['global_total']} respuestas, el NPS global es "
                          f"{signals['global_nps']}. Señal de deterioro de experiencia.",
                params={"global_nps": signals["global_nps"], "global_total": signals["global_total"]},
                impact="Experiencia del paciente / retención",
            ))
        bajos = [p for p in signals.get("por_profesional", [])
                 if (p.get("total") or 0) >= _MIN_RESP and (p.get("nps") or 0) < _NPS_ALERTA]
        for p in bajos:
            out.append(Proposal(
                kind="tarea_manual",
                summary=f"NPS bajo de {p.get('profesional','?')} ({p.get('nps')}) — revisar",
                rationale=f"{p.get('profesional','?')} tiene NPS {p.get('nps')} sobre "
                          f"{p.get('total')} respuestas. Conversar feedback con el profesional.",
                params={"profesional": p.get("profesional"), "nps": p.get("nps"),
                        "total": p.get("total")},
                impact="Calidad por profesional",
            ))
        return out


ORQ = ReputacionNpsOrq()
