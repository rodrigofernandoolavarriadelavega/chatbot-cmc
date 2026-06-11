"""Orquestador: conversaciones paradas sin responder.

Señal: conversaciones donde el ÚLTIMO mensaje es del paciente (direction='in') y
quedó sin respuesta hace entre 2h y 7 días. Acción: worklist para que recepción
responda. Un paciente que escribió y nadie le contestó es atención y conversión
perdida (y mala experiencia). Propone (no auto-contacta — lo resuelve un humano).

Ventana 2h–7d: deja el margen normal de respuesta (no marca lo recién llegado) y
no resucita conversaciones antiquísimas."""
from .base import Orchestrator, Proposal

_HORAS_MIN = 2
_DIAS_MAX = 7
_TOP = 40


class ConversacionParadaOrq(Orchestrator):
    name = "conversacion_parada"
    descripcion = "Último mensaje del paciente sin responder (2h–7d) → responder"
    dominio = "atención"
    cadencia = "cron:cada_2h"

    async def sense(self) -> dict:
        from session import db as _conn
        with _conn() as conn:
            rows = conn.execute(
                """
                SELECT m.phone, m.ts, m.text,
                       COALESCE(cp.nombre, '') AS nombre
                FROM messages m
                JOIN (SELECT phone, MAX(id) AS mx FROM messages GROUP BY phone) last
                  ON last.phone = m.phone AND last.mx = m.id
                LEFT JOIN contact_profiles cp ON cp.phone = m.phone
                WHERE m.direction = 'in'
                  AND m.ts <= datetime('now', ?)
                  AND m.ts >= datetime('now', ?)
                ORDER BY m.ts DESC
                """,
                (f"-{_HORAS_MIN} hours", f"-{_DIAS_MAX} days")).fetchall()
        paradas = [{"phone": r["phone"], "nombre": r["nombre"] or "",
                    "ultimo_ts": r["ts"], "ultimo_texto": (r["text"] or "")[:80]}
                   for r in rows if r["phone"]]
        return {"source_status": "ok", "paradas": paradas}

    async def propose(self, signals: dict) -> list[Proposal]:
        paradas = signals.get("paradas", [])[:_TOP]
        if not paradas:
            return []

        def _row(p):
            tel = (p["phone"] or "").lstrip("+")
            return {"nombre": p.get("nombre") or "paciente", "telefono": p["phone"],
                    "ultimo_mensaje": p.get("ultimo_texto", ""), "cuando": p.get("ultimo_ts"),
                    "wa": f"https://wa.me/{tel}" if tel else ""}

        return [Proposal(
            kind="tarea_manual",
            summary=f"Responder {len(paradas)} conversación(es) sin contestar (2h–7d)",
            rationale="El último mensaje es del paciente y nadie respondió hace horas. Es atención "
                      "y conversión perdida, y mala experiencia. Recepción debería retomarlas hoy.",
            params={"worklist": [_row(p) for p in paradas]},
            impact="Experiencia / conversión / no dejar pacientes en visto",
        )]


ORQ = ConversacionParadaOrq()
