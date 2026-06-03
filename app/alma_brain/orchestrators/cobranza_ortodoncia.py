"""Orquestador: cobranza de Ortodoncia (cartera).

Señal: pacientes con saldo pendiente (plan de pago) y tratamiento activo. Acción:
worklist de recordatorio de pago amable. La ortodoncia es ticket alto a plazos;
la cartera vencida es flujo de caja perdido. Propone (no auto-contacta); el tono
de cobranza lo decide un humano."""
from .base import Orchestrator, Proposal

_TOP = 20


class CobranzaOrtodonciaOrq(Orchestrator):
    name = "cobranza_ortodoncia"
    descripcion = "Saldos de ortodoncia pendientes → worklist de cobranza amable"
    dominio = "finanzas"
    cadencia = "cron:10:00"

    async def sense(self) -> dict:
        from ortodoncia_routes import _compute, ensure_ortodoncia_plan_table
        ensure_ortodoncia_plan_table()
        data = _compute(meses=18)
        pac = data.get("pacientes", [])
        # Con saldo > 0 y tratamiento NO finalizado (cartera viva).
        con_saldo = [p for p in pac
                     if (p.get("saldo") or 0) > 0 and p.get("estado") != "finalizado"
                     and p.get("telefono")]
        con_saldo.sort(key=lambda p: -(p.get("saldo") or 0))
        return {"source_status": data.get("source_status", "?"),
                "valor_cartera": data.get("kpis", {}).get("valor_cartera", 0),
                "con_saldo": con_saldo}

    async def propose(self, signals: dict) -> list[Proposal]:
        con_saldo = signals.get("con_saldo", [])[:_TOP]
        if not con_saldo:
            return []
        total = sum(p.get("saldo") or 0 for p in con_saldo)

        def _row(p):
            return {"paciente": p["paciente"], "telefono": p["telefono"],
                    "saldo": p.get("saldo"), "cuota_mensual": p.get("cuota_mensual"),
                    "wa": f"https://wa.me/{p['telefono'].lstrip('+')}"}

        return [Proposal(
            kind="tarea_manual",
            summary=f"Cobranza amable a {len(con_saldo)} paciente(s) — saldo total ${total:,.0f}".replace(",", "."),
            rationale="Ortodoncia es ticket alto a plazos; la cartera viva es flujo de caja "
                      "pendiente. Recordatorio de pago manual (el tono lo decide recepción).",
            params={"worklist": [_row(p) for p in con_saldo],
                    "valor_cartera": signals.get("valor_cartera", 0)},
            impact="Recuperación de cartera / flujo de caja",
        )]


ORQ = CobranzaOrtodonciaOrq()
