"""Briefing diario — el 'resumen matinal' de Alma observadora.

Aplana el snapshot dry-run de los 18 orquestadores en un único digest priorizado:
qué necesita atención hoy, ordenado por urgencia e impacto. Es lo que Alma le
mostraría al dueño/recepción cada mañana (Fase 1 'Alma observadora' de la visión).

100% read-only: lee el snapshot (o lo construye), no persiste propuestas ni
contacta a nadie.
"""
from . import snapshot

# Orquestadores cuyo hallazgo es prioritario (riesgo clínico / dinero / caída dura).
_ALTA_PRIORIDAD = {"ges_backlog", "agenda_salud", "cobranza_ortodoncia",
                   "no_show_recovery", "resultados_examenes"}


def _worklist_n(params: dict) -> int:
    wl = params.get("worklist") or params.get("items") or []
    return len(wl) if isinstance(wl, list) else 0


async def build_briefing(*, refresh: bool = False) -> dict:
    snap = (await snapshot.build_and_save()) if refresh else (snapshot.load_snapshot() or await snapshot.build_and_save())
    dominio_de = {c["name"]: c.get("dominio", "") for c in snap.get("catalogo", [])}

    items = []
    for r in snap.get("resultados", []):
        name = r.get("name", "")
        for p in r.get("proposals", []):
            n = _worklist_n(p.get("params", {}))
            items.append({
                "orquestador": name,
                "dominio": dominio_de.get(name, ""),
                "summary": p.get("summary", ""),
                "impact": p.get("impact", ""),
                "kind": p.get("kind", ""),
                "n_worklist": n,
                "alta_prioridad": name in _ALTA_PRIORIDAD,
            })

    # Orden: alta prioridad primero, luego por tamaño de worklist (más gente esperando = más urgente).
    items.sort(key=lambda x: (not x["alta_prioridad"], -x["n_worklist"]))

    por_dominio: dict[str, int] = {}
    for it in items:
        por_dominio[it["dominio"]] = por_dominio.get(it["dominio"], 0) + 1

    return {
        "generated_at": snap.get("generated_at"),
        "n_acciones": len(items),
        "n_personas": sum(it["n_worklist"] for it in items),
        "prioritarias": [it for it in items if it["alta_prioridad"]],
        "items": items,
        "por_dominio": por_dominio,
    }
