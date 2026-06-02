"""Agente Post-consulta Smart — seguimiento a quienes se atendieron hoy.

Riesgo MEDIO: contacta PACIENTES. Detecta citas ocurridas hoy que no tienen
seguimiento postconsulta enviado y les pregunta cómo se sienten. Además,
detecta quienes ya respondieron "peor" en seguimientos previos (últimas 24h)
y genera una acción de alerta a staff (is_staff=True) para el doctor/recepción.

Reusa:
  - get_citas_para_seguimiento(fecha, hora_corte) de session.py: citas de hoy
    ya ocurridas sin postconsulta enviado.
  - save_fidelizacion_msg / log_message de session.py: registro del envío.
  - _get_respondieron_peor_hoy: query directa a fidelizacion_msgs (respuesta='peor').
"""
import logging
import os
from zoneinfo import ZoneInfo

from ..base import Agent, AgentAction

log = logging.getLogger("bot")
_CLT = ZoneInfo("America/Santiago")


class PostconsultaSmartAgent(Agent):

    async def perceive(self) -> dict:
        ctx: dict = {"citas_pendientes": [], "peor_hoy": [], "error": None}
        try:
            from datetime import datetime
            ahora = datetime.now(_CLT)
            hoy = ahora.date().isoformat()
            hora_corte = ahora.strftime("%H:%M")

            from session import get_citas_para_seguimiento
            ctx["citas_pendientes"] = get_citas_para_seguimiento(hoy, hora_corte)
        except Exception as e:  # noqa: BLE001
            log.error("alma_agents[postconsulta_smart]: perceive citas falló: %s", e)
            ctx["error"] = str(e)

        try:
            ctx["peor_hoy"] = _get_respondieron_peor_hoy()
        except Exception as e:  # noqa: BLE001
            log.warning("alma_agents[postconsulta_smart]: perceive peor_hoy falló: %s", e)

        return ctx

    async def decide(self, ctx: dict) -> list[AgentAction]:
        acciones: list[AgentAction] = []

        # 1. Seguimiento a citas de hoy sin postconsulta enviado
        for cita in ctx.get("citas_pendientes", []):
            phone = cita.get("phone", "")
            if not phone:
                continue
            texto = _render_postconsulta(cita)
            acciones.append(AgentAction(
                kind="postconsulta_seguimiento",
                summary=f"Seguimiento post-consulta a {phone} ({cita.get('especialidad', '?')})",
                risk="medio",
                target=phone,
                requires_contact=True,
                params={
                    "texto": texto,
                    "id_cita": str(cita.get("id_cita", "")),
                    "especialidad": cita.get("especialidad", ""),
                },
            ))

        # 2. Alerta a staff por pacientes que reportaron sentirse "peor"
        admin = os.getenv("ADMIN_ALERT_PHONE", "")
        for caso in ctx.get("peor_hoy", []):
            phone_pac = caso.get("phone", "?")
            nombre_pac = caso.get("nombre") or phone_pac
            esp = caso.get("especialidad") or "consulta"
            prof = caso.get("profesional") or ""
            prof_label = f" ({prof})" if prof else ""
            alerta = (
                f"*Alerta post-consulta Alma*\n\n"
                f"El paciente *{nombre_pac}* ({phone_pac}) reportó sentirse *peor* "
                f"tras su {esp}{prof_label} de hoy.\n\n"
                "Revisar si requiere contacto o derivación."
            )
            if admin:
                acciones.append(AgentAction(
                    kind="postconsulta_alerta_peor",
                    summary=f"Alertar a recepción/doctor: {nombre_pac} reportó sentirse peor",
                    risk="medio",
                    target=admin,
                    requires_contact=True,
                    is_staff=True,
                    params={"texto": alerta, "paciente_phone": phone_pac},
                ))

        return acciones

    async def execute_one(self, action: AgentAction) -> dict:
        try:
            from messaging import send_whatsapp_proactive
            wamid = await send_whatsapp_proactive(action.target, action.params["texto"])

            if action.kind == "postconsulta_seguimiento" and wamid:
                from session import save_fidelizacion_msg, log_message
                id_cita = action.params.get("id_cita", "")
                save_fidelizacion_msg(action.target, "postconsulta", id_cita)
                log_message(action.target, "out", action.params["texto"][:200], "IDLE")

            return {"sent": bool(wamid), "wamid": wamid, "kind": action.kind}
        except Exception as e:  # noqa: BLE001
            log.error("alma_agents[postconsulta_smart]: execute_one falló phone=%s kind=%s: %s",
                      action.target, action.kind, e)
            return {"sent": False, "error": str(e)}


# ── Helpers de percepción ────────────────────────────────────────────────────

def _get_respondieron_peor_hoy() -> list[dict]:
    """Pacientes que respondieron 'peor' en las últimas 24h en fidelizacion_msgs.
    Degrada a [] si session.db no está disponible."""
    try:
        from session import _conn
        conn = _conn()
    except Exception:  # noqa: BLE001
        return []
    try:
        rows = conn.execute(
            """
            SELECT f.phone, f.respuesta, cb.especialidad, cb.profesional, p.nombre
            FROM fidelizacion_msgs f
            LEFT JOIN citas_bot cb ON cb.id_cita = f.cita_id AND cb.phone = f.phone
            LEFT JOIN contact_profiles p ON p.phone = f.phone
            WHERE f.tipo = 'postconsulta'
              AND f.respuesta = 'peor'
              AND f.enviado_en >= datetime('now', '-1 days')
            ORDER BY f.enviado_en DESC
            """,
        ).fetchall()
        return [
            {
                "phone": r[0],
                "respuesta": r[1],
                "especialidad": r[2],
                "profesional": r[3],
                "nombre": r[4],
            }
            for r in rows
        ]
    except Exception as e:  # noqa: BLE001
        log.warning("alma_agents[postconsulta_smart]: query peor_hoy falló: %s", e)
        return []
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def _render_postconsulta(cita: dict) -> str:
    """Mensaje de seguimiento post-consulta en texto plano."""
    nombre = (cita.get("nombre") or "").strip()
    saludo = f"Hola *{nombre}* " if nombre else "Hola "
    esp = (cita.get("especialidad") or "tu consulta").strip()
    prof = (cita.get("profesional") or "").strip()
    prof_label = f" con *{prof}*" if prof else ""

    return (
        f"{saludo}del *Centro Médico Carampangue*.\n\n"
        f"Esperamos que tu visita de hoy a *{esp}*{prof_label} haya ido bien. "
        "¿Cómo te sientes?\n\n"
        "Si tienes alguna duda o necesitas algo, estamos para ayudarte."
    )


AGENT = PostconsultaSmartAgent(
    id="postconsulta_smart",
    name="Post-consulta smart",
    descr=(
        "Seguimiento a pacientes atendidos hoy que no recibieron postconsulta. "
        "Alerta a staff cuando alguien reporta sentirse peor."
    ),
    risk="medio",
    flag="ALMA_AGENT_POSTCONSULTA",
    category="fidelizacion",
    schedule={"hour": 19, "minute": 0},
)
