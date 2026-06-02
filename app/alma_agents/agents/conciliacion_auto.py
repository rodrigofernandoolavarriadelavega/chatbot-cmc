"""Agente ConciliacionAuto — vigilante financiero diario.

Riesgo BAJO: no contacta pacientes. Corre la conciliacion del mes en curso
usando las fuentes internas disponibles (pagos_cmc de sessions.db + Medilink /pagos)
y alerta a Rodrigo si hay gaps relevantes o atenciones no cobradas.

No ejecuta el auditor pesado de CSVs externos (requiere uploads manuales);
trabaja con las dos fuentes siempre disponibles en produccion.

Patrón: contacto a STAFF + reuso de conciliacion_routes helpers.
"""
import logging
import os
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from ..base import Agent, AgentAction

log = logging.getLogger("bot")
_CLT = ZoneInfo("America/Santiago")

# Umbral: alertar si la diferencia entre lo registrado internamente y lo que
# reporta Medilink supera este porcentaje del total Medilink.
_GAP_PCT_UMBRAL = float(os.getenv("ALMA_CONCILIACION_GAP_PCT", "5.0"))
# Umbral absoluto (CLP): ignorar gaps menores a esta cifra aunque superen el %.
_GAP_ABS_UMBRAL = int(os.getenv("ALMA_CONCILIACION_GAP_ABS", "10000"))


class ConciliacionAutoAgent(Agent):

    async def perceive(self) -> dict:
        """Lee pagos_cmc (local) y /pagos Medilink para el mes en curso.
        Degrada a dict vacio con error si alguna fuente falla."""
        try:
            hoy = datetime.now(_CLT).date()
            d_desde = hoy.replace(day=1)
            d_hasta = hoy
            return _cuadre_mes(d_desde, d_hasta)
        except Exception as e:  # noqa: BLE001
            log.warning("conciliacion_auto.perceive: %s", e)
            return {"error": str(e)}

    async def decide(self, ctx: dict) -> list[AgentAction]:
        admin = os.getenv("ADMIN_ALERT_PHONE", "")
        if not admin or ctx.get("error"):
            return []

        gaps = ctx.get("gaps", [])
        atenciones_no_cobradas = ctx.get("atenciones_no_cobradas", 0)
        total_medilink = ctx.get("total_medilink_clp", 0)
        total_interno = ctx.get("total_interno_clp", 0)

        # Decidir si hay algo relevante que reportar
        hay_alerta = bool(gaps) or atenciones_no_cobradas > 0

        if not hay_alerta:
            return []

        msg = _render_alerta(ctx)
        return [AgentAction(
            kind="conciliacion_alerta",
            summary=(
                f"Gap financiero mes en curso: "
                f"interno ${total_interno:,.0f} vs Medilink ${total_medilink:,.0f}. "
                f"{atenciones_no_cobradas} aten. no cobradas."
            ),
            risk="bajo",
            target=admin,
            is_staff=True,
            requires_contact=True,
            params={"texto": msg},
        )]

    async def execute_one(self, action: AgentAction) -> dict:
        try:
            from messaging import send_whatsapp_proactive
            wamid = await send_whatsapp_proactive(action.target, action.params["texto"])
            return {"sent": bool(wamid), "wamid": wamid}
        except Exception as e:  # noqa: BLE001
            log.error("conciliacion_auto.execute_one: %s", e)
            return {"sent": False, "error": str(e)}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _cuadre_mes(d_desde: date, d_hasta: date) -> dict:
    """Compara pagos_cmc (interno) con /pagos de Medilink para el periodo.
    Degrada: si Medilink falla devuelve source_medilink=unavailable pero
    igualmente reporta el conteo interno para alertar de anomalias propias."""
    resultado: dict = {
        "periodo_desde": d_desde.isoformat(),
        "periodo_hasta": d_hasta.isoformat(),
        "total_interno_clp": 0,
        "n_pagos_interno": 0,
        "total_medilink_clp": 0,
        "n_pagos_medilink": 0,
        "source_medilink": "ok",
        "atenciones_no_cobradas": 0,
        "gaps": [],
    }

    # ── Fuente 1: pagos_cmc en sessions.db ───────────────────────────────────
    try:
        from session import _conn
        conn = _conn()
        rows = conn.execute(
            """SELECT SUM(COALESCE(copago,0) + COALESCE(bonificacion,0)) AS total,
                      COUNT(*) AS n
               FROM pagos_cmc
               WHERE fecha BETWEEN ? AND ?""",
            (d_desde.isoformat(), d_hasta.isoformat()),
        ).fetchone()
        conn.close()
        if rows and rows[0] is not None:
            resultado["total_interno_clp"] = float(rows[0])
            resultado["n_pagos_interno"] = int(rows[1])
    except Exception as e:  # noqa: BLE001
        log.warning("conciliacion_auto._cuadre_mes: sessions.db: %s", e)

    # ── Fuente 2: Medilink /pagos ─────────────────────────────────────────────
    try:
        import httpx
        base_url = os.getenv("MEDILINK_BASE_URL", "https://api.medilink2.healthatom.com/api/v5")
        token_ml = os.getenv("MEDILINK_TOKEN", "")
        sucursal = os.getenv("MEDILINK_SUCURSAL", "1")
        if not token_ml:
            resultado["source_medilink"] = "no_token"
        else:
            headers = {"Authorization": f"Bearer {token_ml}", "Accept": "application/json"}
            with httpx.Client(timeout=20.0) as client:
                resp = client.get(
                    f"{base_url}/pagos",
                    params={
                        "id_sucursal": sucursal,
                        "fecha_desde": d_desde.strftime("%d/%m/%Y"),
                        "fecha_hasta": d_hasta.strftime("%d/%m/%Y"),
                    },
                    headers=headers,
                )
            if resp.status_code == 200:
                data = resp.json()
                items = data if isinstance(data, list) else data.get("data", data.get("pagos", []))
                total_ml = 0.0
                for item in items:
                    monto = float(
                        item.get("monto") or item.get("total") or
                        item.get("valor") or item.get("monto_total") or 0
                    )
                    total_ml += monto
                resultado["total_medilink_clp"] = total_ml
                resultado["n_pagos_medilink"] = len(items)
            else:
                resultado["source_medilink"] = f"http_{resp.status_code}"
    except Exception as e:  # noqa: BLE001
        log.warning("conciliacion_auto._cuadre_mes: medilink: %s", e)
        resultado["source_medilink"] = "unavailable"

    # ── Calcular gap ──────────────────────────────────────────────────────────
    total_ml = resultado["total_medilink_clp"]
    total_int = resultado["total_interno_clp"]
    if total_ml > 0 and total_int > 0:
        diff = abs(total_ml - total_int)
        pct = (diff / total_ml) * 100
        if diff > _GAP_ABS_UMBRAL and pct > _GAP_PCT_UMBRAL:
            resultado["gaps"].append({
                "tipo": "diferencia_totales",
                "diferencia_clp": round(diff),
                "diferencia_pct": round(pct, 1),
                "interno": round(total_int),
                "medilink": round(total_ml),
            })

    # ── Atenciones sin pago registrado (heuristico en sessions.db) ───────────
    try:
        from session import _conn
        conn = _conn()
        # Cuenta citas confirmadas por el bot que no tienen registro en pagos_cmc
        row = conn.execute(
            """SELECT COUNT(*) FROM conversation_events
               WHERE event='cita_creada'
                 AND ts >= datetime(?)
                 AND ts <= datetime(?)""",
            (d_desde.isoformat(), d_hasta.isoformat()),
        ).fetchone()
        conn.close()
        citas_bot = int(row[0]) if row else 0
        # Comparar con n_pagos para estimar no cobradas (heuristico imperfecto)
        n_pagos = resultado["n_pagos_interno"]
        if citas_bot > 0 and n_pagos < citas_bot:
            resultado["atenciones_no_cobradas"] = citas_bot - n_pagos
    except Exception as e:  # noqa: BLE001
        log.warning("conciliacion_auto._cuadre_mes: heuristico: %s", e)

    return resultado


def _render_alerta(ctx: dict) -> str:
    lines = ["*Conciliacion Financiera — Alma*", ""]
    lines.append(f"Periodo: {ctx['periodo_desde']} → {ctx['periodo_hasta']}")
    lines.append("")
    int_clp = ctx["total_interno_clp"]
    ml_clp = ctx["total_medilink_clp"]
    lines.append(f"Registrado internamente: ${int_clp:,.0f} ({ctx['n_pagos_interno']} pagos)")
    src = ctx.get("source_medilink", "ok")
    if src == "ok":
        lines.append(f"Medilink (cajas): ${ml_clp:,.0f} ({ctx['n_pagos_medilink']} pagos)")
    else:
        lines.append(f"Medilink: {src} (no disponible en este momento)")

    for g in ctx.get("gaps", []):
        if g["tipo"] == "diferencia_totales":
            lines.append("")
            lines.append(
                f"*Alerta:* diferencia de ${g['diferencia_clp']:,.0f} "
                f"({g['diferencia_pct']}%) entre fuentes."
            )

    no_cob = ctx.get("atenciones_no_cobradas", 0)
    if no_cob > 0:
        lines.append("")
        lines.append(
            f"*Posibles atenciones sin cobro registrado:* {no_cob} "
            f"(citas creadas por el bot sin pago en pagos_cmc)."
        )

    lines.append("")
    lines.append("Revisar /alma/pagos para detalle.")
    return "\n".join(lines)


AGENT = ConciliacionAutoAgent(
    id="conciliacion_auto",
    name="Conciliacion automatica",
    descr=(
        "Compara pagos internos vs Medilink y alerta a Rodrigo "
        "si hay gaps o atenciones no cobradas. No contacta pacientes."
    ),
    risk="bajo",
    flag="ALMA_AGENT_CONCILIACION",
    category="finanzas",
    schedule={"hour": 7, "minute": 0},
)
