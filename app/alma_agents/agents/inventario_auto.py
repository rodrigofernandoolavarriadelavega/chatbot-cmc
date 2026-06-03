"""Agente InventarioAuto — prediccion de quiebres y orden de compra dental.

Riesgo ALTO: no contacta pacientes. Genera una orden de compra borrador
en inventario_dental cuando detecta productos bajo el umbral de stock minimo.
Si no puede crear el borrador (tabla no disponible), alerta a Rodrigo con la
lista de items a reponer.

Patrón: alerta a STAFF + reuso de inventario_routes helpers.
"""
import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from ..base import Agent, AgentAction

log = logging.getLogger("bot")
_CLT = ZoneInfo("America/Santiago")


class InventarioAutoAgent(Agent):

    async def perceive(self) -> dict:
        """Lee inventario_dental y detecta productos bajo minimo o agotados.
        Degrada a dict con error si la tabla no existe aun (inventario no inicializado)."""
        try:
            items_criticos = _items_bajo_minimo()
            return {
                "items_criticos": items_criticos,
                "n_criticos": len(items_criticos),
                "total_reposicion_clp": sum(i["subtotal"] for i in items_criticos),
            }
        except Exception as e:  # noqa: BLE001
            log.warning("inventario_auto.perceive: %s", e)
            return {"items_criticos": [], "n_criticos": 0, "error": str(e)}

    async def decide(self, ctx: dict) -> list[AgentAction]:
        if ctx.get("error") or ctx["n_criticos"] == 0:
            return []

        items = ctx["items_criticos"]
        total = ctx["total_reposicion_clp"]

        # Intentar guardar un borrador de orden de compra en la DB.
        borrador_id = _guardar_borrador(items)

        admin = os.getenv("ADMIN_ALERT_PHONE", "")
        acciones = []

        if borrador_id:
            msg = _render_con_borrador(items, total, borrador_id)
            summary = (
                f"Orden de compra borrador #{borrador_id}: "
                f"{len(items)} items, ${total:,.0f} CLP"
            )
        else:
            msg = _render_sin_borrador(items, total)
            summary = (
                f"Alerta stock dental: {len(items)} items bajo minimo, "
                f"reposicion estimada ${total:,.0f} CLP"
            )

        if admin:
            acciones.append(AgentAction(
                kind="orden_compra",
                summary=summary,
                risk="alto",
                target=admin,
                is_staff=True,
                requires_contact=True,
                params={
                    "texto": msg,
                    "n_items": len(items),
                    "total_clp": total,
                    "borrador_id": borrador_id,
                },
            ))

        return acciones

    async def execute_one(self, action: AgentAction) -> dict:
        try:
            from messaging import send_whatsapp_proactive
            wamid = await send_whatsapp_proactive(action.target, action.params["texto"])
            return {
                "sent": bool(wamid),
                "wamid": wamid,
                "n_items": action.params.get("n_items"),
                "borrador_id": action.params.get("borrador_id"),
            }
        except Exception as e:  # noqa: BLE001
            log.error("inventario_auto.execute_one: %s", e)
            return {"sent": False, "error": str(e)}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _items_bajo_minimo() -> list[dict]:
    """Reusa la logica de inventario_routes.orden_compra: productos bajo minimo
    → reponer a 2x el minimo. Devuelve lista de items con cantidad y subtotal."""
    try:
        from session import _conn
        conn = _conn()
    except Exception:  # noqa: BLE001
        return []

    try:
        rows = conn.execute(
            "SELECT * FROM inventario_dental WHERE activo = 1 ORDER BY categoria, nombre"
        ).fetchall()
    except Exception as e:  # noqa: BLE001
        log.warning("inventario_auto._items_bajo_minimo: %s", e)
        conn.close()
        return []

    items = []
    for row in rows:
        r = dict(row)
        stock_actual = r.get("stock_actual", 0) or 0
        stock_minimo = r.get("stock_minimo", 1) or 1
        if stock_actual > stock_minimo:
            continue  # OK, no reportar
        objetivo = max(stock_minimo * 2, 1)
        cantidad = max(objetivo - stock_actual, 1)
        precio = r.get("precio_mayordent", 0) or 0
        estado = "agotado" if stock_actual <= 0 else "bajo"
        items.append({
            "id": r["id"],
            "nombre": r["nombre"],
            "marca": r.get("marca", ""),
            "categoria": r.get("categoria", ""),
            "stock_actual": stock_actual,
            "stock_minimo": stock_minimo,
            "cantidad_sugerida": cantidad,
            "precio_unit": precio,
            "subtotal": cantidad * precio,
            "estado": estado,
        })

    conn.close()
    return items


def _guardar_borrador(items: list[dict]) -> int | None:
    """Guarda la orden de compra como borrador en inventario_ordenes.
    Si la tabla no existe, la crea. Devuelve el id del borrador o None si falla."""
    if not items:
        return None
    try:
        from session import _conn
        conn = _conn()
    except Exception:  # noqa: BLE001
        return None

    try:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS inventario_ordenes (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                creado_en   TEXT NOT NULL,
                estado      TEXT NOT NULL DEFAULT 'borrador',
                n_items     INTEGER NOT NULL DEFAULT 0,
                total_clp   REAL NOT NULL DEFAULT 0,
                detalle_json TEXT
            )"""
        )
        conn.commit()
        import json
        detalle = json.dumps(items, ensure_ascii=False)
        total = sum(i["subtotal"] for i in items)
        cur = conn.execute(
            """INSERT INTO inventario_ordenes (creado_en, estado, n_items, total_clp, detalle_json)
               VALUES (?, 'borrador', ?, ?, ?)""",
            (datetime.now(_CLT).isoformat(), len(items), total, detalle),
        )
        conn.commit()
        return cur.lastrowid
    except Exception as e:  # noqa: BLE001
        log.warning("inventario_auto._guardar_borrador: %s", e)
        return None
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def _render_con_borrador(items: list[dict], total: float, borrador_id: int) -> str:
    lines = [
        f"*Inventario Dental — Orden de Compra #{borrador_id}*",
        f"Generada automaticamente. {len(items)} producto(s) bajo minimo.",
        "",
    ]
    agotados = [i for i in items if i["estado"] == "agotado"]
    bajos = [i for i in items if i["estado"] == "bajo"]

    if agotados:
        lines.append("*Agotados:*")
        for i in agotados:
            lines.append(
                f"• {i['nombre']} ({i['marca']}) — "
                f"reponer {i['cantidad_sugerida']} {''} "
                f"(${i['subtotal']:,.0f})"
            )
    if bajos:
        if agotados:
            lines.append("")
        lines.append("*Stock bajo:*")
        for i in bajos:
            lines.append(
                f"• {i['nombre']} ({i['marca']}) — "
                f"stock {i['stock_actual']}/{i['stock_minimo']}, "
                f"reponer {i['cantidad_sugerida']} "
                f"(${i['subtotal']:,.0f})"
            )

    lines += [
        "",
        f"*Total estimado reposicion: ${total:,.0f} CLP*",
        "Proveedor referencia: MayorDent (mayordent.cl).",
        "Aprobar o editar en /alma/inventario.",
    ]
    return "\n".join(lines)


def _render_sin_borrador(items: list[dict], total: float) -> str:
    lines = [
        "*Inventario Dental — Alerta de Stock*",
        f"{len(items)} producto(s) bajo el minimo. Reposicion estimada: ${total:,.0f} CLP.",
        "",
    ]
    for i in items[:10]:  # limitar a 10 para no saturar el mensaje
        estado_label = "AGOTADO" if i["estado"] == "agotado" else f"bajo ({i['stock_actual']}/{i['stock_minimo']})"
        lines.append(f"• {i['nombre']}: {estado_label}")
    if len(items) > 10:
        lines.append(f"  ... y {len(items) - 10} mas.")
    lines += ["", "Revisar en /alma/inventario."]
    return "\n".join(lines)


AGENT = InventarioAutoAgent(
    id="inventario_auto",
    name="Inventario automatico",
    descr=(
        "Detecta quiebres de stock dental y genera orden de compra borrador. "
        "Alerta a Rodrigo con el detalle. No contacta pacientes."
    ),
    risk="alto",
    flag="ALMA_AGENT_INVENTARIO",
    category="operaciones",
    schedule={"hour": 8, "minute": 30},
)
