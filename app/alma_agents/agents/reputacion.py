"""Agente Reputacion — pide resena de Google a pacientes con NPS alto recientes.

Riesgo MEDIO: contacta PACIENTES. Cada contacto pasa por el piso completo de
guardrails (consent, horas de silencio, presupuesto anti-spam).

Fuentes reusadas:
  - session.py::get_nps_por_profesional  → NPS por profesional
  - session._conn + tabla fidelizacion   → pacientes con respuesta "mejor" reciente
  - google_rating.get_review_link        → link directo a escribir resena en Google
  - messaging.send_whatsapp_proactive    → envio del mensaje
"""
import logging
import os

from ..base import Agent, AgentAction

log = logging.getLogger("bot")

# Dias hacia atras para buscar pacientes con respuesta positiva
_DIAS = int(os.getenv("ALMA_AGENT_REPUTACION_DIAS", "14"))
# NPS minimo (0-100) para considerar al profesional "caliente"
_NPS_MIN = float(os.getenv("ALMA_AGENT_REPUTACION_NPS_MIN", "40"))
# Max pacientes a contactar por corrida
_MAX_CONTACTOS = int(os.getenv("ALMA_AGENT_REPUTACION_MAX", "5"))
# Tag que marcamos para no volver a pedir resena al mismo paciente
_TAG_REVIEW_SOLICITADA = "review_solicitada"


def _pacientes_nps_alto(dias: int, limite: int) -> list[dict]:
    """Pacientes que respondieron 'mejor' en el seguimiento post-consulta reciente
    y que NO tienen el tag review_solicitada. Degrada a [] si falla."""
    try:
        from session import _conn
        conn = _conn()
    except Exception:  # noqa: BLE001
        return []
    try:
        # Busca en fidelizacion_respuestas las respuestas positivas
        cur = conn.execute(
            """
            SELECT DISTINCT fr.phone
            FROM fidelizacion_respuestas fr
            WHERE fr.respuesta IN ('mejor', 'Mejor')
              AND fr.ts >= datetime('now', ?)
              AND fr.phone IS NOT NULL
              AND fr.phone != ''
              AND fr.phone NOT IN (
                  SELECT phone FROM contact_tags
                  WHERE tag = ?
              )
            ORDER BY fr.ts DESC
            LIMIT ?
            """,
            (f"-{dias} days", _TAG_REVIEW_SOLICITADA, limite),
        )
        return [{"phone": r[0]} for r in cur.fetchall()]
    except Exception as e:  # noqa: BLE001
        log.warning("reputacion: query pacientes NPS alto fallo (%s)", e)
        return []
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def _nps_global_positivo(dias: int, umbral: float) -> bool:
    """Verifica que el NPS global reciente sea suficientemente alto para pedir resenas.
    Si no se puede medir, es conservador y permite el contacto."""
    try:
        from session import get_nps_por_profesional
        data = get_nps_por_profesional(dias=dias)
        nps_global = data.get("global_nps")
        if nps_global is None:
            return True  # sin datos → no bloquear
        return float(nps_global) >= umbral
    except Exception:  # noqa: BLE001
        return True  # degrada conservador: permite el contacto


def _marcar_review_solicitada(phone: str) -> None:
    """Agrega el tag review_solicitada para no volver a pedir al mismo paciente."""
    try:
        from session import save_tag
        save_tag(phone, _TAG_REVIEW_SOLICITADA)
    except Exception as e:  # noqa: BLE001
        log.warning("reputacion: no se pudo marcar tag en %s: %s", phone, e)


class ReputacionAgent(Agent):

    async def perceive(self) -> dict:
        try:
            from google_rating import fetch_rating, get_review_link
            rating_data = await fetch_rating()
            review_link = get_review_link()
        except Exception as e:  # noqa: BLE001
            log.warning("reputacion perceive: google_rating fallo (%s)", e)
            rating_data = {}
            from google_rating import PLACE_ID
            review_link = f"https://search.google.com/local/writereview?placeid={PLACE_ID}"

        nps_ok = _nps_global_positivo(_DIAS, _NPS_MIN)
        candidatos = _pacientes_nps_alto(_DIAS, _MAX_CONTACTOS) if nps_ok else []

        return {
            "rating": rating_data.get("rating"),
            "review_count": rating_data.get("review_count"),
            "review_link": review_link,
            "nps_ok": nps_ok,
            "candidatos": candidatos,
        }

    async def decide(self, ctx: dict) -> list[AgentAction]:
        if not ctx.get("nps_ok"):
            return []
        review_link = ctx.get("review_link", "")
        if not review_link:
            return []
        rating = ctx.get("rating")
        out = []
        for c in ctx.get("candidatos", []):
            phone = c.get("phone", "")
            if not phone:
                continue
            if rating and rating >= 4.8:
                # Rating ya excelente: mencionar que queremos seguir mejorando
                msg = (
                    f"Hola, esperamos que te hayas sentido bien atendido en el "
                    f"Centro Medico Carampangue. Si tienes un momento, nos ayudaria "
                    f"mucho que dejaras una resena en Google: {review_link}\n"
                    f"Tu opinion es muy importante para seguir mejorando. Muchas gracias."
                )
            else:
                msg = (
                    f"Hola, esperamos que tu atencion en el Centro Medico Carampangue "
                    f"haya sido de tu agrado. Si tienes unos minutos, nos ayudaria que "
                    f"compartieras tu experiencia en Google: {review_link}\n"
                    f"Gracias por confiar en nosotros."
                )
            out.append(AgentAction(
                kind="solicitar_resena",
                summary=f"Pedir resena de Google a {phone}",
                risk="medio",
                target=phone,
                requires_contact=True,
                is_staff=False,
                params={"texto": msg, "review_link": review_link},
            ))
        return out

    async def execute_one(self, action: AgentAction) -> dict:
        from messaging import send_whatsapp_proactive
        phone = action.target
        texto = action.params.get("texto", "")
        try:
            wamid = await send_whatsapp_proactive(phone, texto)
            if wamid:
                _marcar_review_solicitada(phone)
            return {"sent": bool(wamid), "wamid": wamid}
        except Exception as e:  # noqa: BLE001
            log.error("reputacion execute_one fallo para %s: %s", phone, e)
            return {"sent": False, "error": str(e)}


AGENT = ReputacionAgent(
    id="reputacion",
    name="Gestion de Reputacion",
    descr=(
        "Pide resena de Google a pacientes con NPS alto recientes que aun no han "
        "dejado resena. Contacta pacientes (pasa por guardrails completos)."
    ),
    risk="medio",
    flag="ALMA_AGENT_REPUTACION",
    category="marketing",
    schedule={"hour": 12, "minute": 0},
)
