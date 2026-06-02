"""Agente Cobranza — recordatorio de copagos pendientes.

Riesgo EXTREMO: contacta pacientes para cobrarles. El guardrail bloquea esto
salvo que ALMA_AGENTS_ALLOW_EXTREME=true sea seteado deliberadamente.
Ademas requiere consent (Ley 21.719) y presupuesto de contacto.

Tono: respetuoso, nunca hostigador. Solo un recordatorio gentil.
El agente detecta atenciones recientes sin pago registrado en pagos_cmc
y avisa al paciente (si tiene consent y telefono conocido).

Patrón: contacto a PACIENTES con riesgo extremo + reuso de sessions.db.
"""
import logging
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from ..base import Agent, AgentAction

log = logging.getLogger("bot")
_CLT = ZoneInfo("America/Santiago")

# Ventana: buscar citas creadas en los ultimos N dias sin pago asociado.
_DIAS_VENTANA = int(os.getenv("ALMA_COBRANZA_DIAS", "7"))
# Maximo de recordatorios por ciclo (no saturar).
_MAX_CONTACTOS = int(os.getenv("ALMA_COBRANZA_MAX", "10"))
# Solo recordar si el monto estimado supera este umbral (CLP).
_MONTO_MINIMO = int(os.getenv("ALMA_COBRANZA_MONTO_MIN", "3000"))


class CobranzaAgent(Agent):

    async def perceive(self) -> dict:
        """Detecta citas sin pago en la ventana de dias configurada.
        Degrada a lista vacia si falla."""
        try:
            candidatos = _citas_sin_pago(_DIAS_VENTANA, _MAX_CONTACTOS)
            return {"candidatos": candidatos, "ventana_dias": _DIAS_VENTANA}
        except Exception as e:  # noqa: BLE001
            log.warning("cobranza.perceive: %s", e)
            return {"candidatos": [], "error": str(e)}

    async def decide(self, ctx: dict) -> list[AgentAction]:
        acciones = []
        for c in ctx.get("candidatos", []):
            phone = c.get("phone", "")
            if not phone:
                continue
            monto = c.get("monto_estimado", 0)
            if monto < _MONTO_MINIMO:
                continue
            especialidad = c.get("especialidad", "tu atencion medica")
            fecha_str = c.get("fecha_cita", "reciente")
            msg = _render_recordatorio(c["nombre_paciente"], especialidad, fecha_str, monto)
            acciones.append(AgentAction(
                kind="cobranza_recordatorio",
                summary=(
                    f"Recordar pago pendiente a {phone} "
                    f"({especialidad}, ${monto:,.0f})"
                ),
                risk="extremo",
                target=phone,
                requires_contact=True,
                is_staff=False,
                params={
                    "texto": msg,
                    "especialidad": especialidad,
                    "monto_estimado": monto,
                    "fecha_cita": fecha_str,
                },
            ))
        return acciones

    async def execute_one(self, action: AgentAction) -> dict:
        try:
            from messaging import send_whatsapp_proactive
            wamid = await send_whatsapp_proactive(action.target, action.params["texto"])
            # Registrar que se envio para no duplicar en proximos ciclos.
            _marcar_contactado(action.target)
            return {
                "sent": bool(wamid),
                "wamid": wamid,
                "especialidad": action.params.get("especialidad"),
            }
        except Exception as e:  # noqa: BLE001
            log.error("cobranza.execute_one: %s", e)
            return {"sent": False, "error": str(e)}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _citas_sin_pago(dias: int, limite: int) -> list[dict]:
    """Busca citas creadas por el bot (evento cita_creada) en la ventana
    para las que NO hay registro de pago en pagos_cmc.
    Devuelve lista de dicts con phone, nombre, especialidad, monto estimado.
    Excluye phones ya contactados por cobranza en los ultimos 7 dias."""
    try:
        from session import _conn
        conn = _conn()
    except Exception:  # noqa: BLE001
        return []

    try:
        # Citas creadas por el bot en la ventana
        citas_rows = conn.execute(
            """SELECT ce.phone,
                      json_extract(ce.meta, '$.especialidad')  AS especialidad,
                      json_extract(ce.meta, '$.nombre')        AS nombre_paciente,
                      json_extract(ce.meta, '$.fecha_cita')    AS fecha_cita,
                      json_extract(ce.meta, '$.monto')         AS monto_medilink,
                      MAX(ce.ts) AS ts_cita
               FROM conversation_events ce
               WHERE ce.event = 'cita_creada'
                 AND ce.ts >= datetime('now', ?)
               GROUP BY ce.phone, especialidad
               ORDER BY ts_cita DESC
               LIMIT ?""",
            (f"-{dias} days", limite * 3),  # pedir mas para filtrar abajo
        ).fetchall()
    except Exception as e:  # noqa: BLE001
        log.warning("cobranza._citas_sin_pago: query citas: %s", e)
        conn.close()
        return []

    resultado = []
    for row in citas_rows:
        phone = row[0]
        if not phone:
            continue

        # Verificar que no hay pago en pagos_cmc para este telefono en la ventana
        try:
            tiene_pago = conn.execute(
                """SELECT 1 FROM pagos_cmc
                   WHERE rut IN (
                       SELECT rut FROM contact_profiles WHERE phone = ?
                       UNION SELECT phone WHERE phone = ?
                   )
                   AND fecha >= date('now', ?)
                   LIMIT 1""",
                (phone, phone, f"-{dias} days"),
            ).fetchone()
        except Exception:  # noqa: BLE001
            tiene_pago = None  # Si no se puede verificar, conservador: saltar

        if tiene_pago:
            continue

        # Verificar que no se le haya enviado recordatorio de cobranza reciente
        try:
            ya_contactado = conn.execute(
                """SELECT 1 FROM conversation_events
                   WHERE phone = ? AND event = 'cobranza_contactado'
                     AND ts >= datetime('now', '-7 days')
                   LIMIT 1""",
                (phone,),
            ).fetchone()
        except Exception:  # noqa: BLE001
            ya_contactado = None

        if ya_contactado:
            continue

        # Estimar monto: preferir meta.monto si existe, sino usar heuristico
        monto_raw = row[4]
        monto = float(monto_raw) if monto_raw else 0.0
        if monto <= 0:
            monto = _monto_por_especialidad(row[1] or "")

        resultado.append({
            "phone": phone,
            "nombre_paciente": row[2] or "paciente",
            "especialidad": row[1] or "atencion medica",
            "fecha_cita": row[3] or "reciente",
            "monto_estimado": monto,
        })

        if len(resultado) >= limite:
            break

    conn.close()
    return resultado


def _monto_por_especialidad(especialidad: str) -> float:
    """Estimacion conservadora de copago por especialidad para el mensaje.
    Solo se usa cuando no hay monto en el evento. No es arancel oficial."""
    esp = especialidad.lower()
    if "kine" in esp:
        return 3_560
    if "medicina general" in esp or "medicina familiar" in esp:
        return 7_250
    if "nutri" in esp:
        return 4_770
    if "psico" in esp:
        return 6_560
    if "odonto" in esp or "dental" in esp:
        return 25_000
    return 0.0  # Si no se sabe, no recordar (filtro monto_minimo lo excluye)


def _marcar_contactado(phone: str) -> None:
    """Registra evento cobranza_contactado para no duplicar en proximos ciclos."""
    try:
        from session import log_event
        log_event(phone, "cobranza_contactado", {"agente": "cobranza"})
    except Exception as e:  # noqa: BLE001
        log.warning("cobranza._marcar_contactado: %s", e)


def _render_recordatorio(nombre: str, especialidad: str, fecha: str, monto: float) -> str:
    nombre_display = nombre.split()[0].capitalize() if nombre else "hola"
    monto_str = f"${monto:,.0f}" if monto > 0 else "el monto correspondiente"
    return (
        f"Hola {nombre_display}, te escribimos del Centro Medico Carampangue. "
        f"Quedamos con un pago pendiente de tu atencion de {especialidad} "
        f"del {fecha} por {monto_str}. "
        f"Puedes cancelarlo en nuestra recepcion (efectivo o transferencia) "
        f"o llamarnos al (44) 296 5226. Gracias por tu confianza."
    )


AGENT = CobranzaAgent(
    id="cobranza",
    name="Recordatorio de cobranza",
    descr=(
        "Detecta copagos pendientes y envia recordatorio respetuoso al paciente. "
        "Riesgo EXTREMO: requiere ALMA_AGENTS_ALLOW_EXTREME=true para ejecutar."
    ),
    risk="extremo",
    flag="ALMA_AGENT_COBRANZA",
    category="finanzas",
    schedule={"hour": 16, "minute": 0},
)
