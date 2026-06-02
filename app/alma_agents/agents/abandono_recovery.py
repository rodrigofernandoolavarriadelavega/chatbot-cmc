"""Agente AbandonoRecovery — reenganche de pacientes que abandonaron un flujo.

Riesgo MEDIO: contacta pacientes que estan a medias en un agendamiento
(estados WAIT_* entre 20 y 90 min de inactividad) y no han recibido reenganche.

IMPORTANTE — anti-solapamiento con el cron existente:
  jobs.py::_enviar_reenganche() corre cada 5 min y usa get_sesiones_abandonadas()
  que ya filtra por reenganche_sent=True y throttle 24h en conversation_events
  (evento 'reenganche_enviado'). Este agente reusa la MISMA funcion y MISMO evento,
  por lo tanto ambos comparten el throttle naturalmente: si el cron de jobs.py
  ya reenganchó a alguien, este agente lo vera como ya contactado y lo saltará.
  No hay double-send posible mientras ambos respeten ese invariante.

  La diferencia es que este agente corre cada 30 min (no cada 5) y usa
  una ventana de inactividad de 20-90 min (vs 10-90 del cron).
  El cron es mas agresivo; el agente es complementario (para cuando el cron
  no alcanza, o para trazar la actividad en el panel de la flota Alma).

Patrón: contacto a PACIENTES + reuso directo de session.get_sesiones_abandonadas.
"""
import logging
import os
from datetime import datetime, timezone

from ..base import Agent, AgentAction

log = logging.getLogger("bot")

# Inactividad minima para que el agente actue (en minutos).
# El cron actua desde 10 min; el agente espera 20 para no competir en el mismo ciclo.
_MIN_INACTIVIDAD_MIN = int(os.getenv("ALMA_ABANDONO_MIN_MIN", "20"))
# Maximo de sesiones a procesar por ciclo.
_MAX_SESIONES = int(os.getenv("ALMA_ABANDONO_MAX", "15"))


class AbandonoRecoveryAgent(Agent):

    async def perceive(self) -> dict:
        """Reusa get_sesiones_abandonadas() de session.py (misma funcion que el cron).
        Aplica filtro adicional de inactividad minima para no competir con el cron."""
        try:
            sesiones = _sesiones_para_agente(_MIN_INACTIVIDAD_MIN, _MAX_SESIONES)
            return {"sesiones": sesiones, "n": len(sesiones)}
        except Exception as e:  # noqa: BLE001
            log.warning("abandono_recovery.perceive: %s", e)
            return {"sesiones": [], "n": 0, "error": str(e)}

    async def decide(self, ctx: dict) -> list[AgentAction]:
        acciones = []
        for s in ctx.get("sesiones", []):
            phone = s.get("phone", "")
            if not phone:
                continue
            state = s.get("state", "WAIT_ESPECIALIDAD")
            data = s.get("data", {})
            canal = data.get("canal", "whatsapp")

            # Solo WhatsApp por ahora (send_whatsapp_proactive solo funciona con WA).
            if canal not in ("whatsapp", "wa", ""):
                continue

            msg = _render_reenganche(state, data)
            acciones.append(AgentAction(
                kind="abandono_reenganche",
                summary=f"Re-enganchar sesion {state} de {phone}",
                risk="medio",
                target=phone,
                requires_contact=True,
                is_staff=False,
                params={"texto": msg, "state": state},
            ))
        return acciones

    async def execute_one(self, action: AgentAction) -> dict:
        phone = action.target
        try:
            from messaging import send_whatsapp_proactive
            wamid = await send_whatsapp_proactive(phone, action.params["texto"])
            # Registrar el mismo evento que usa el cron para compartir el throttle.
            _registrar_reenganche(phone, action.params.get("state", ""))
            return {
                "sent": bool(wamid),
                "wamid": wamid,
                "state": action.params.get("state"),
            }
        except Exception as e:  # noqa: BLE001
            log.error("abandono_recovery.execute_one: %s", e)
            return {"sent": False, "error": str(e)}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _sesiones_para_agente(min_inactividad_min: int, limite: int) -> list[dict]:
    """Llama a get_sesiones_abandonadas() (fuente de verdad del cron) y filtra
    las sesiones con inactividad de al menos min_inactividad_min minutos.

    get_sesiones_abandonadas ya filtra:
      - estados IDLE/COMPLETED/HUMAN_TAKEOVER
      - reenganche_sent=True (ya contactados en esta sesion)
      - reenganche_optout (paciente rechazó explicitamente)
      - throttle 24h por evento 'reenganche_enviado' en conversation_events

    Este agente solo agrega el filtro de inactividad minima."""
    try:
        from session import get_sesiones_abandonadas
        todas = get_sesiones_abandonadas()
    except Exception as e:  # noqa: BLE001
        log.warning("abandono_recovery._sesiones: get_sesiones_abandonadas: %s", e)
        return []

    ahora = datetime.now(timezone.utc)
    resultado = []
    for s in todas:
        updated_at = s.get("updated_at", "")
        if not updated_at:
            continue
        try:
            upd = datetime.fromisoformat(updated_at).replace(tzinfo=timezone.utc)
            delta_min = (ahora - upd).total_seconds() / 60
        except Exception:  # noqa: BLE001
            continue
        if delta_min < min_inactividad_min:
            continue  # demasiado reciente, ceder al cron de 5 min
        resultado.append(s)
        if len(resultado) >= limite:
            break

    return resultado


def _registrar_reenganche(phone: str, state: str) -> None:
    """Registra evento 'reenganche_enviado' en conversation_events — mismo evento
    que usa jobs._enviar_reenganche, para compartir el throttle de 24h."""
    try:
        from session import log_event
        log_event(phone, "reenganche_enviado", {"agente": "abandono_recovery", "state": state})
    except Exception as e:  # noqa: BLE001
        log.warning("abandono_recovery._registrar_reenganche: %s", e)


def _render_reenganche(state: str, data: dict) -> str:
    """Texto de reenganche adaptado al estado en que quedo la sesion."""
    especialidad = data.get("especialidad", "")

    if state in ("WAIT_SLOT", "WAIT_MODALIDAD"):
        if especialidad:
            return (
                f"Hola, quedamos a medias con tu hora de {especialidad}. "
                f"Si quieres continuar, escribe cualquier mensaje y te retomo "
                f"donde ibamos. Si prefieres empezar de nuevo, escribe \"menu\"."
            )
        return (
            "Hola, quedamos a medias con tu hora medica. "
            "Si quieres continuar, escribe cualquier mensaje "
            "y te retomo donde ibamos."
        )

    if state in ("WAIT_ESPECIALIDAD", "WAIT_BOOKING_FOR"):
        return (
            "Hola, te empezamos a ayudar a agendar una hora pero no terminamos. "
            "Si quieres continuar, escribe \"agendar\" y te ayudamos de inmediato."
        )

    if state in ("WAIT_RUT_AGENDAR", "WAIT_DATOS_NUEVO", "WAIT_NOMBRE_NUEVO",
                 "WAIT_FECHA_NAC", "WAIT_SEXO", "WAIT_COMUNA", "WAIT_EMAIL"):
        return (
            "Hola, te empezamos a registrar para agendar tu hora medica "
            "pero no terminamos. Escribe tu RUT cuando quieras continuar, "
            "o \"menu\" si prefieres empezar de nuevo."
        )

    if state in ("WAIT_RUT_CANCELAR", "WAIT_CITA_CANCELAR", "CONFIRMING_CANCEL"):
        return (
            "Hola, quedamos a medias con la cancelacion de tu hora. "
            "Escribe \"cancelar\" si quieres continuar, "
            "o \"menu\" para empezar de nuevo."
        )

    if state in ("CONFIRMING_CITA",):
        if especialidad:
            return (
                f"Hola, tu hora de {especialidad} esta lista para confirmar. "
                f"Escribe cualquier mensaje para retomar y confirmarla."
            )
        return (
            "Hola, tu hora medica esta lista para confirmar. "
            "Escribe cualquier mensaje para retomar."
        )

    # Estado generico
    return (
        "Hola, quedamos a medias con tu consulta en el Centro Medico Carampangue. "
        "Escribe cualquier mensaje para continuar, o \"menu\" para empezar de nuevo."
    )


AGENT = AbandonoRecoveryAgent(
    id="abandono_recovery",
    name="Recuperacion de abandono",
    descr=(
        "Re-engancha pacientes que abandonaron un flujo de agendamiento "
        "(WAIT_* con 20-90 min de inactividad). Reutiliza el mismo throttle "
        "del cron de reenganche de jobs.py para evitar double-send."
    ),
    risk="medio",
    flag="ALMA_AGENT_ABANDONO",
    category="ingresos",
    schedule={"minute": "*/30"},
)
