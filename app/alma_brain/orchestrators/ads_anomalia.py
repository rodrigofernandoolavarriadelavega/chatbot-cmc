"""Orquestador: anomalías de Meta Ads.

Señal: el snapshot del autopilot (sense_ads) muestra que Meta sobre-atribuye
(attribution_ratio<0.7 → el ROAS reportado miente), o hay campañas activas sin
gasto (entrega/presupuesto roto), o el autopilot dejó acciones esperando aprobación.
Acción: proponer revisar Ads Manager. NO toca pauta: solo alerta. Propone."""
from .base import Orchestrator, Proposal

_RATIO_DESCONFIANZA = 0.7


class AdsAnomaliaOrq(Orchestrator):
    name = "ads_anomalia"
    descripcion = "Meta sobre-atribuye / gasto raro / acciones pendientes → revisar Ads"
    dominio = "marketing"
    cadencia = "cron:09:00"

    async def sense(self) -> dict:
        from .. import sensors
        a = sensors.sense_ads()
        if not a.get("available"):
            return {"source_status": "unavailable", "reason": a.get("reason")}
        return {"source_status": "ok", "kpis": a.get("kpis", {}),
                "acciones_propuestas": a.get("acciones_propuestas", [])}

    async def propose(self, signals: dict) -> list[Proposal]:
        k = signals.get("kpis")
        if not k:
            return []
        out = []
        ratio = k.get("attribution_ratio")
        if ratio is not None and ratio < _RATIO_DESCONFIANZA:
            out.append(Proposal(
                kind="tarea_manual",
                summary=f"Meta sobre-atribuye (ratio {ratio:.2f}) — desconfiar del ROAS reportado",
                rationale="La caja real (BI) registra menos ingreso del que Meta atribuye. El ROAS "
                          "del Ads Manager está inflado: decidir presupuesto con la caja real, no "
                          "con la atribución de Meta.",
                params={"attribution_ratio": ratio, "spend_clp": k.get("spend_clp"),
                        "ingreso_real_bi_clp": k.get("ingreso_real_bi_clp")},
                impact="Decisiones de presupuesto sobre datos reales / no quemar plata",
            ))
        if (k.get("n_campanas", 0) > 0) and (k.get("spend_clp", 0) or 0) <= 0:
            out.append(Proposal(
                kind="tarea_manual",
                summary=f"{k.get('n_campanas')} campaña(s) activa(s) sin gasto — revisar entrega/presupuesto",
                rationale="Hay campañas activas pero el gasto es ~0. Suele ser presupuesto agotado, "
                          "campaña en revisión, o segmentación que no entrega. Revisar Ads Manager.",
                params={"n_campanas": k.get("n_campanas"), "spend_clp": k.get("spend_clp")},
                impact="Que la pauta efectivamente entregue",
            ))
        pendientes = [a for a in signals.get("acciones_propuestas", []) if a.get("necesita_aprobacion")]
        if pendientes:
            out.append(Proposal(
                kind="tarea_manual",
                summary=f"{len(pendientes)} acción(es) del autopilot de Ads esperan tu aprobación",
                rationale="El autopilot propuso cambios de pauta que necesitan visto bueno. "
                          "Revisarlos en el Autopilot antes de que la oportunidad pase.",
                params={"acciones": pendientes},
                impact="No perder optimizaciones de pauta propuestas",
            ))
        return out


ORQ = AdsAnomaliaOrq()
