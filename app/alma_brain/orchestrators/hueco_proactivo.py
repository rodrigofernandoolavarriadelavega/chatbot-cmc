"""Orquestador: relleno PROACTIVO de huecos desde la lista de espera.

Generaliza la Fase 4 (que reacciona a cancelaciones) a un barrido proactivo: por
cada especialidad con gente en lista de espera, busca el próximo cupo disponible
y propone ofrecerlo. Es el ÚNICO orquestador que puede AUTO-EJECUTAR, y lo hace
delegando en `operativa.fill_freed_slot`, que está doblemente gateado por
`ALMA_OPERATIVA_ENABLED`. Sin ese flag, ejecutar es un no-op.
"""
from .base import Orchestrator, Proposal

_MAX_ESP = 8   # cota de llamadas a Medilink por corrida


class HuecoProactivoOrq(Orchestrator):
    name = "hueco_proactivo"
    descripcion = "Huecos disponibles → ofrecer a la lista de espera (proactivo)"
    dominio = "agenda"
    cadencia = "cron:07:30"

    async def sense(self) -> dict:
        from resilience import is_medilink_down
        if is_medilink_down():
            return {"source_status": "medilink_down", "slots": []}
        from session import get_waitlist_pending
        from medilink import buscar_primer_dia

        pend = get_waitlist_pending()
        # Agrupar por (especialidad, prof preferido) para no repetir búsquedas.
        vistos, grupos = set(), []
        for r in pend:
            key = (r["especialidad"], r.get("id_prof_pref"))
            if key in vistos:
                continue
            vistos.add(key)
            grupos.append({"especialidad": r["especialidad"], "id_prof_pref": r.get("id_prof_pref"),
                           "en_espera": sum(1 for x in pend if x["especialidad"] == r["especialidad"])})

        slots = []
        for g in grupos[:_MAX_ESP]:
            try:
                solo = [int(g["id_prof_pref"])] if g["id_prof_pref"] else None
                _, todos = await buscar_primer_dia(g["especialidad"], dias_adelante=14, solo_ids=solo)
            except Exception:
                continue
            if todos:
                s = todos[0]
                slots.append({"especialidad": g["especialidad"],
                              "id_prof": g["id_prof_pref"] or s.get("id_profesional"),
                              "fecha": s.get("fecha"), "hora": s.get("hora_inicio"),
                              "en_espera": g["en_espera"]})
        return {"source_status": "ok", "slots": slots}

    async def propose(self, signals: dict) -> list[Proposal]:
        out = []
        for s in signals.get("slots", []):
            out.append(Proposal(
                kind="tarea_manual",   # la ejecución real la hace operativa, ya gateada
                summary=f"Ofrecer cupo de {s['especialidad']} ({s['fecha']} {s['hora']}) a "
                        f"{s['en_espera']} en espera",
                rationale="Hay gente en lista de espera y un cupo disponible. Ofrecerlo proactivamente "
                          "(no esperar a una cancelación) acelera la ocupación. Ejecuta vía Fase 4 "
                          "(operativa), que está gateada aparte.",
                params={"slot": s},
                impact="Ocupación de agenda / espera más corta",
            ))
        return out

    async def execute_proposal(self, prop) -> dict:
        """Auto-ejecución: delega en operativa.fill_freed_slot (gateada por
        ALMA_OPERATIVA_ENABLED → no-op si ese flag está apagado)."""
        from .. import operativa
        slot = prop.params.get("slot", {})
        res = await operativa.fill_freed_slot({
            "especialidad": slot.get("especialidad", ""),
            "id_prof": slot.get("id_prof"),
            "fecha": slot.get("fecha", ""),
            "hora": slot.get("hora", ""),
            "phone_cancelador": "",
        })
        return {"executed": bool(res.get("enabled", True)), "resultado": res}


ORQ = HuecoProactivoOrq()
