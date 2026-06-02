"""Agente Reactivación Win-back — recupera cohortes RFM de pacientes inactivos.

Riesgo ALTO: contacta PACIENTES. Es el de mayor riesgo legal de la flota:
contacta pacientes que no han venido en 90+ días, lo que requiere consent
explícito de marketing (Ley 21.719). El check de consent lo hace
guardrails.authorize() — NO se duplica aquí.

IMPORTANTE sobre el target: cada AgentAction.target es el teléfono canónico
del paciente (campo `telefono` de la vista BI o `phone` de sessions.db).
El framework de guardrails necesita ese target para verificar consent y
presupuesto de contacto anti-spam.

Reusa:
  - winback.get_candidatos_dia(cohorte, limite): candidatos desde
    bi.v_winback_cohortes_contactables con consent verificado a nivel BI.
    Degrada a [] si la BI no responde.
  - get_pacientes_winback(dias_min, dias_max) de session.py: fuente de
    fallback desde sessions.db cuando la BI no está disponible.
  - get_tags(phone) de session.py: personaliza el mensaje si hay dx:*.
  - winback.ya_enviado_winback_hoy(phone): guard de idempotencia diaria.
  - winback._registrar_envio: registra el envío en bi.winback_envios.
"""
import logging
import os

from ..base import Agent, AgentAction

log = logging.getLogger("bot")


class ReactivacionWinbackAgent(Agent):

    async def perceive(self) -> dict:
        cohorte = os.getenv("ALMA_WINBACK_COHORTE", "090d")
        limite = int(os.getenv("ALMA_WINBACK_LIMITE", "20"))
        candidatos: list[dict] = []
        fuente = "bi"

        # Fuente primaria: BI Postgres (incluye consent verificado a nivel vista)
        try:
            from winback import get_candidatos_dia, _phones_con_cita_futura
            citas_futuras = _phones_con_cita_futura()
            todos = get_candidatos_dia(cohorte=cohorte, limite=limite * 2)
            # Excluir phones con cita futura en sessions.db
            for c in todos:
                tel = c.get("telefono", "")
                digits = "".join(ch for ch in tel if ch.isdigit())
                canon = digits[-9:] if len(digits) >= 9 else digits
                in_futuro = any(
                    (canon and f.endswith(canon)) or tel == f
                    for f in citas_futuras
                )
                if not in_futuro:
                    candidatos.append(c)
                if len(candidatos) >= limite:
                    break
        except Exception as e:  # noqa: BLE001
            log.warning("alma_agents[winback]: fuente BI falló, usando sessions.db: %s", e)
            fuente = "sessions_db"
            # Fuente de fallback: sessions.db
            try:
                from session import get_pacientes_winback
                dias_min = int(os.getenv("ALMA_WINBACK_DIAS_MIN", "91"))
                dias_max = int(os.getenv("ALMA_WINBACK_DIAS_MAX", "365"))
                filas = get_pacientes_winback(dias_min=dias_min, dias_max=dias_max)
                candidatos = filas[:limite]
            except Exception as e2:  # noqa: BLE001
                log.error("alma_agents[winback]: fuente fallback sessions.db falló: %s", e2)
                return {"candidatos": [], "fuente": fuente, "error": str(e2)}

        return {"candidatos": candidatos, "fuente": fuente, "cohorte": cohorte}

    async def decide(self, ctx: dict) -> list[AgentAction]:
        if ctx.get("error"):
            return []
        acciones: list[AgentAction] = []
        for c in ctx.get("candidatos", []):
            # Normalizar teléfono: la BI usa campo 'telefono', sessions.db usa 'phone'
            phone = c.get("telefono") or c.get("phone") or ""
            if not phone:
                continue

            # Guard de idempotencia: no reenviar si ya se mandó winback hoy
            try:
                from winback import ya_enviado_winback_hoy
                if ya_enviado_winback_hoy(phone):
                    continue
            except Exception:  # noqa: BLE001
                pass

            texto = _render_winback(c)
            acciones.append(AgentAction(
                kind="winback_outreach",
                summary=(
                    f"Win-back a {phone} "
                    f"(cohorte={c.get('cohorte', ctx.get('cohorte', '?'))}, "
                    f"esp={c.get('ultima_especialidad') or c.get('especialidad', '?')})"
                ),
                risk="alto",
                target=phone,
                requires_contact=True,
                params={
                    "texto": texto,
                    "paciente_id": c.get("paciente_id", ""),
                    "cohorte": c.get("cohorte") or ctx.get("cohorte", "090d"),
                    "especialidad": c.get("ultima_especialidad") or c.get("especialidad", ""),
                    "fuente": ctx.get("fuente", "bi"),
                },
            ))
        return acciones

    async def execute_one(self, action: AgentAction) -> dict:
        try:
            from messaging import send_whatsapp_proactive
            from session import save_fidelizacion_msg, log_message
            wamid = await send_whatsapp_proactive(action.target, action.params["texto"])
            if wamid:
                # Registrar en sessions.db para dedup de fidelizacion.py
                save_fidelizacion_msg(action.target, "winback")
                log_message(action.target, "out", action.params["texto"][:200], "IDLE")

                # Registrar en bi.winback_envios si viene de la BI
                if action.params.get("paciente_id") and action.params.get("fuente") == "bi":
                    try:
                        from winback import _registrar_envio
                        _registrar_envio(
                            paciente_id=action.params["paciente_id"],
                            cohorte=action.params["cohorte"],
                            telefono=action.target,
                            template_meta="alma_agent_winback",
                            canal="whatsapp",
                            wamid=wamid or "",
                        )
                    except Exception as e:  # noqa: BLE001
                        log.warning("alma_agents[winback]: _registrar_envio falló: %s", e)

            return {
                "sent": bool(wamid),
                "wamid": wamid,
                "fuente": action.params.get("fuente"),
            }
        except Exception as e:  # noqa: BLE001
            log.error("alma_agents[winback]: execute_one falló phone=%s: %s",
                      action.target, e)
            return {"sent": False, "error": str(e)}


# ── Helpers ──────────────────────────────────────────────────────────────────

def _render_winback(c: dict) -> str:
    """Mensaje personalizado por patología crónica o especialidad previa.
    Calca el tono de _msg_winback de fidelizacion.py pero en texto plano.
    """
    # Nombre: BI usa 'nombre'+'apellido', sessions.db usa 'nombre' completo
    nombre_bi = (c.get("nombre") or "").strip()
    apellido_bi = (c.get("apellido") or "").strip()
    nombre_full = f"{nombre_bi} {apellido_bi}".strip() if apellido_bi else nombre_bi
    nombre_corto = nombre_full.split()[0] if nombre_full else ""
    saludo = f"Hola *{nombre_corto}* " if nombre_corto else "Hola "

    # Tags dx:* para personalización
    dx_tags: list[str] = []
    try:
        phone = c.get("telefono") or c.get("phone") or ""
        if phone:
            from session import get_tags
            tags = get_tags(phone)
            dx_tags = [t.replace("dx:", "").upper() for t in tags if t.startswith("dx:")]
    except Exception:  # noqa: BLE001
        pass

    esp = (c.get("ultima_especialidad") or c.get("especialidad") or "").strip()
    esp_label = f" de *{esp}*" if esp else ""

    if dx_tags:
        patologia = dx_tags[0]
        return (
            f"{saludo}del *Centro Médico Carampangue*.\n\n"
            f"Con *{patologia}*, dejar pasar más de 3 meses sin control "
            "puede aumentar el riesgo de complicaciones.\n\n"
            "Tenemos horas disponibles esta semana. "
            "Responde este mensaje y te ayudamos a agendar."
        )
    else:
        return (
            f"{saludo}del *Centro Médico Carampangue*.\n\n"
            f"Han pasado varios meses desde tu última visita{esp_label}. "
            "Un chequeo preventivo puede detectar problemas a tiempo.\n\n"
            "Tenemos horas disponibles. "
            "Responde este mensaje y te ayudamos a agendar."
        )


AGENT = ReactivacionWinbackAgent(
    id="reactivacion_winback",
    name="Reactivación win-back",
    descr=(
        "Contacta cohortes RFM de pacientes inactivos (>90d) con consent de marketing. "
        "Fuente primaria: bi.v_winback_cohortes_contactables. Fallback: sessions.db."
    ),
    risk="alto",
    flag="ALMA_AGENT_WINBACK",
    category="fidelizacion",
    schedule={"hour": 10, "minute": 0},
)
