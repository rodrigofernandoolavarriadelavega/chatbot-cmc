"""Agente SEO Content — audita brechas de contenido y propone oportunidades a Rodrigo.

Riesgo MEDIO: solo propone a Rodrigo (staff). No toca el sitio, no contacta
pacientes, no publica nada. Envia un resumen WhatsApp al admin con el top de
oportunidades de SEO y contenido.

Fuentes reusadas:
  - autopilot.seo_audit.load_snapshot / fetch_and_audit  → auditoria de paginas
  - autopilot.seo_audit.ESPECIALIDADES / COMUNAS         → matriz cobertura
  - autopilot.seo_audit.coverage_matrix                  → gaps esp x comuna
  - autopilot.seo_audit.OPPORTUNITY_TEMPLATES            → plantillas best-practice
  - messaging.send_whatsapp_proactive                    → envio WA al admin
"""
import logging
import os

from ..base import Agent, AgentAction

log = logging.getLogger("bot")

# Numero maximo de oportunidades a incluir en el resumen
_TOP_N = int(os.getenv("ALMA_AGENT_SEO_TOP_N", "5"))
# Si true, corre fetch_and_audit (hace requests HTTP al sitio); si false, solo lee snapshot
_FETCH_LIVE = os.getenv("ALMA_AGENT_SEO_FETCH_LIVE", "false").lower() in ("true", "1")


def _load_seo_snapshot() -> dict | None:
    """Lee el snapshot SEO guardado. None si no existe o falla."""
    try:
        from autopilot.seo_audit import SNAPSHOT_PATH
        import json
        with open(SNAPSHOT_PATH, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:  # noqa: BLE001
        log.warning("seo_content: no se pudo leer seo_snapshot: %s", e)
        return None


def _coverage_gaps(top_n: int) -> list[dict]:
    """Combinaciones especialidad x comuna con alta intencion sin pagina cubierta.
    Degrada a [] si falla."""
    try:
        from autopilot.seo_audit import coverage_matrix, ESPECIALIDADES, COMUNAS
        matrix = coverage_matrix()
        gaps = []
        for row in matrix:
            if not row.get("covered", True):
                gaps.append(row)
        # Ordenar por demanda (especialidad) * concentracion (comuna) desc
        esp_dem = {s: d for s, _, d in ESPECIALIDADES}
        com_conc = {s: c for s, _, c in COMUNAS}
        gaps.sort(
            key=lambda r: esp_dem.get(r.get("esp_slug", ""), 0)
                          * com_conc.get(r.get("com_slug", ""), 0),
            reverse=True,
        )
        return gaps[:top_n]
    except Exception as e:  # noqa: BLE001
        log.warning("seo_content: coverage_matrix fallo (%s)", e)
        return []


def _low_score_pages(snapshot: dict | None, top_n: int) -> list[dict]:
    """Paginas del sitio con score SEO mas bajo desde el snapshot."""
    if not snapshot:
        return []
    try:
        pages = snapshot.get("pages", [])
        scored = [p for p in pages if isinstance(p.get("score"), (int, float))]
        scored.sort(key=lambda p: p.get("score", 100))
        return scored[:top_n]
    except Exception:  # noqa: BLE001
        return []


class SeoContentAgent(Agent):

    async def perceive(self) -> dict:
        snapshot = None
        if _FETCH_LIVE:
            try:
                from autopilot.seo_audit import fetch_and_audit, default_targets
                snapshot = await fetch_and_audit(default_targets())
            except Exception as e:  # noqa: BLE001
                log.warning("seo_content perceive: fetch_and_audit fallo (%s)", e)
                snapshot = _load_seo_snapshot()
        else:
            snapshot = _load_seo_snapshot()

        gaps = _coverage_gaps(_TOP_N)
        low_pages = _low_score_pages(snapshot, _TOP_N)

        # Top templates de oportunidad (solo lectura)
        templates_preview = []
        try:
            from autopilot.seo_audit import OPPORTUNITY_TEMPLATES
            templates_preview = [
                {"id": t["id"], "name": t["name"], "intent": t.get("intent", "")}
                for t in OPPORTUNITY_TEMPLATES[:3]
            ]
        except Exception:  # noqa: BLE001
            pass

        return {
            "gaps": gaps,
            "low_pages": low_pages,
            "templates_preview": templates_preview,
            "snapshot_available": snapshot is not None,
        }

    async def decide(self, ctx: dict) -> list[AgentAction]:
        admin = os.getenv("ADMIN_ALERT_PHONE", "")
        if not admin:
            return []

        gaps = ctx.get("gaps", [])
        low_pages = ctx.get("low_pages", [])
        if not gaps and not low_pages:
            return []

        texto = self._render_reporte(ctx)
        return [AgentAction(
            kind="seo_reporte",
            summary="Enviar reporte SEO con oportunidades de contenido a Rodrigo",
            risk="bajo",
            target=admin,
            is_staff=True,
            requires_contact=True,
            requires_medilink_write=False,
            params={"texto": texto},
        )]

    def _render_reporte(self, ctx: dict) -> str:
        lines = ["*Reporte SEO Alma — oportunidades de contenido*", ""]

        gaps = ctx.get("gaps", [])
        if gaps:
            lines.append("*Brechas de cobertura (especialidad x comuna sin pagina):*")
            for g in gaps[:_TOP_N]:
                esp = g.get("esp_nombre") or g.get("esp_slug", "")
                com = g.get("com_nombre") or g.get("com_slug", "")
                lines.append(f"• {esp} en {com}")
            lines.append("")

        low = ctx.get("low_pages", [])
        if low:
            lines.append("*Paginas con score SEO mas bajo:*")
            for p in low[:3]:
                url = p.get("url", "")
                score = p.get("score", "?")
                label = p.get("label") or url.split("/")[-1] or url
                lines.append(f"• {label}: {score}/100 ({url})")
            lines.append("")

        tpls = ctx.get("templates_preview", [])
        if tpls:
            lines.append("*Plantillas SEO de alta conversion disponibles en /autopilot:*")
            for t in tpls:
                lines.append(f"• {t['name']} ({t.get('intent', '')})")
            lines.append("")

        lines.append("Ver detalle completo en agentecmc.cl/autopilot (pestana SEO).")
        return "\n".join(lines)

    async def execute_one(self, action: AgentAction) -> dict:
        from messaging import send_whatsapp_proactive
        try:
            wamid = await send_whatsapp_proactive(action.target, action.params["texto"])
            return {"sent": bool(wamid), "wamid": wamid}
        except Exception as e:  # noqa: BLE001
            log.error("seo_content execute_one fallo: %s", e)
            return {"sent": False, "error": str(e)}


AGENT = SeoContentAgent(
    id="seo_content",
    name="Auditor SEO y Contenido",
    descr=(
        "Audita brechas de contenido SEO (especialidad x comuna sin pagina) y "
        "paginas con bajo score. Propone oportunidades a Rodrigo por WhatsApp. "
        "No publica nada ni contacta pacientes."
    ),
    risk="medio",
    flag="ALMA_AGENT_SEO",
    category="marketing",
    schedule={"hour": 6, "minute": 30},
)
