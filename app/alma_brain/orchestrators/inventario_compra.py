"""Orquestador: reposición de inventario dental.

Señal: insumos bajo el punto de reorden (stock_actual ≤ stock_minimo). Acción:
orden de compra sugerida (reponer a 2x mínimo) para MayorDent. Propone: la compra
la confirma un humano. Evita quiebres de stock que paran box dental."""
from .base import Orchestrator, Proposal


def _estado(actual: int, minimo: int) -> str:
    if actual <= 0:
        return "agotado"
    if actual <= minimo:
        return "bajo"
    return "ok"


class InventarioCompraOrq(Orchestrator):
    name = "inventario_compra"
    descripcion = "Insumos bajo mínimo → orden de compra sugerida"
    dominio = "inventario"
    cadencia = "cron:08:00"

    async def sense(self) -> dict:
        from inventario_routes import ensure_inventario_tables
        from session import _conn
        ensure_inventario_tables()
        with _conn() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM inventario_dental WHERE activo = 1 ORDER BY categoria, nombre"
            ).fetchall()]
        items = []
        for r in rows:
            est = _estado(r["stock_actual"], r["stock_minimo"])
            if est not in ("bajo", "agotado"):
                continue
            objetivo = max(r["stock_minimo"] * 2, 1)
            cantidad = max(objetivo - r["stock_actual"], 1)
            items.append({
                "nombre": r["nombre"], "marca": r["marca"], "categoria": r["categoria"],
                "unidad": r["unidad"], "stock_actual": r["stock_actual"],
                "stock_minimo": r["stock_minimo"], "estado": est,
                "cantidad_sugerida": cantidad,
                "precio_mayordent": r["precio_mayordent"],
                "subtotal": cantidad * r["precio_mayordent"],
            })
        return {"source_status": "ok", "items": items,
                "total": sum(i["subtotal"] for i in items)}

    async def propose(self, signals: dict) -> list[Proposal]:
        items = signals.get("items", [])
        if not items:
            return []
        agotados = [i for i in items if i["estado"] == "agotado"]
        total = signals.get("total", 0)
        return [Proposal(
            kind="tarea_manual",
            summary=f"Orden de compra MayorDent: {len(items)} insumo(s) bajo mínimo "
                    f"({len(agotados)} agotado(s)) · ${total:,.0f}".replace(",", "."),
            rationale="Insumos bajo el punto de reorden. Reponer a 2x el mínimo evita quiebres "
                      "que paran box dental. La compra la confirma recepción/compras.",
            params={"items": items, "total": total, "proveedor": "MayorDent"},
            impact="Continuidad operativa box dental / evitar quiebres",
        )]


ORQ = InventarioCompraOrq()
