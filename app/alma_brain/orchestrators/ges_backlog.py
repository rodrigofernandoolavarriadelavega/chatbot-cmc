"""Orquestador: backlog de triage GES sin agendar.

Señal: pacientes a los que el motor GES les detectó un síntoma con destino de
especialidad (evento `triage_ges_match`) pero que NO terminaron agendando. Acción:
worklist de seguimiento — un síntoma detectado que no convirtió en cita es una
atención perdida (y, si fue urgente, un riesgo clínico). Propone (no auto-contacta).
"""
import json

from .base import Orchestrator, Proposal

_DIAS = 7
_TOP = 25


class GesBacklogOrq(Orchestrator):
    name = "ges_backlog"
    descripcion = "GES detectó síntoma → especialidad pero el paciente no agendó → seguir"
    dominio = "clínico"
    cadencia = "cron:09:00"

    async def sense(self) -> dict:
        from session import db as _conn
        with _conn() as conn:
            evs = conn.execute(
                "SELECT phone, meta, ts FROM conversation_events "
                "WHERE event = 'triage_ges_match' AND ts >= datetime('now', ?) "
                "ORDER BY ts DESC", (f"-{_DIAS} days",)).fetchall()
            # Dedupe por teléfono (el match más reciente).
            vistos, candidatos = set(), []
            for e in evs:
                phone = e["phone"]
                if not phone or phone in vistos:
                    continue
                vistos.add(phone)
                try:
                    meta = json.loads(e["meta"] or "{}")
                except Exception:
                    meta = {}
                # ¿Agendó algo desde el match? (cualquier cita creada después del evento)
                booked = conn.execute(
                    "SELECT 1 FROM citas_bot WHERE phone = ? AND created_at >= ? LIMIT 1",
                    (phone, e["ts"])).fetchone()
                if booked:
                    continue
                candidatos.append({
                    "phone": phone, "especialidad": meta.get("especialidad") or "",
                    "urgency": bool(meta.get("urgency")), "ts": e["ts"]})
        # Urgentes primero.
        candidatos.sort(key=lambda c: (not c["urgency"], c["ts"]))
        return {"source_status": "ok", "candidatos": candidatos}

    async def propose(self, signals: dict) -> list[Proposal]:
        cand = signals.get("candidatos", [])[:_TOP]
        if not cand:
            return []
        urgentes = [c for c in cand if c["urgency"]]

        def _row(c):
            tel = (c["phone"] or "").lstrip("+")
            return {"telefono": c["phone"], "especialidad": c["especialidad"],
                    "urgente": c["urgency"], "detectado": c["ts"],
                    "wa": f"https://wa.me/{tel}" if tel else ""}

        return [Proposal(
            kind="tarea_manual",
            summary=f"Seguir {len(cand)} paciente(s) con síntoma GES sin agendar"
                    f"{f' ({len(urgentes)} marcados urgentes)' if urgentes else ''}",
            rationale="GES detectó un síntoma con destino de especialidad pero el paciente no "
                      "agendó. Es atención perdida; los urgentes son además riesgo clínico que "
                      "conviene confirmar que resolvieron (SAMU/urgencias).",
            params={"worklist": [_row(c) for c in cand], "urgentes": len(urgentes)},
            impact="Conversión de demanda clínica detectada / seguridad del paciente",
        )]


ORQ = GesBacklogOrq()
