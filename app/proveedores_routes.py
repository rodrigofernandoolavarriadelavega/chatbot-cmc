"""
Router /alma/api/proveedores — Proveedores y órdenes de compra.

Directorio de proveedores (MayorDent y otros) + órdenes de compra con estado
(borrador → enviada → recibida → pagada). Complementa Inventario (que sugiere
qué reponer) con el lado de la compra: a quién, cuánto y en qué estado.

Tablas: proveedores, ordenes_compra. Auth: alma_common.require_admin.
"""
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request, Query, Cookie

from alma_common import require_admin

log = logging.getLogger("proveedores_routes")
_CHILE_TZ = ZoneInfo("America/Santiago")
router = APIRouter(prefix="/alma/api/proveedores", tags=["proveedores"])

RUBROS = ["Insumos dental", "Insumos médico", "Laboratorio", "Aseo", "Servicios",
          "Equipamiento", "Farmacia", "Otro"]
OC_ESTADOS = ["borrador", "enviada", "recibida", "pagada"]

SEED = [
    {"nombre": "MayorDent", "rubro": "Insumos dental", "sitio_web": "mayordent.cl",
     "condiciones_pago": "Contado / transferencia", "notas": "Envío gratis sobre $50.000 (Concepción/Stgo)"},
]


def ensure_tables() -> None:
    from session import _conn
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS proveedores (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nombre TEXT NOT NULL, rubro TEXT DEFAULT 'Otro', contacto TEXT DEFAULT '',
                telefono TEXT DEFAULT '', email TEXT DEFAULT '', direccion TEXT DEFAULT '',
                sitio_web TEXT DEFAULT '', condiciones_pago TEXT DEFAULT '', notas TEXT DEFAULT '',
                activo INTEGER DEFAULT 1, created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')))
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ordenes_compra (
                id INTEGER PRIMARY KEY AUTOINCREMENT, proveedor_id INTEGER,
                proveedor_nombre TEXT DEFAULT '', fecha TEXT NOT NULL, descripcion TEXT DEFAULT '',
                monto INTEGER DEFAULT 0, estado TEXT DEFAULT 'borrador', notas TEXT DEFAULT '',
                creado_por TEXT DEFAULT 'recepcion', created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')))
        """)
        conn.commit()


def seed_if_empty() -> int:
    ensure_tables()
    from session import _conn
    with _conn() as conn:
        n = conn.execute("SELECT COUNT(*) c FROM proveedores").fetchone()["c"]
        if n > 0:
            return 0
        for s in SEED:
            conn.execute(
                "INSERT INTO proveedores (nombre, rubro, sitio_web, condiciones_pago, notas) VALUES (?,?,?,?,?)",
                (s["nombre"], s["rubro"], s.get("sitio_web", ""), s.get("condiciones_pago", ""), s.get("notas", "")))
        conn.commit()
    return len(SEED)


@router.get("/resumen")
async def resumen(token: str | None = Query(None), cmc_session: str | None = Cookie(None), request: Request = None):
    require_admin(request, token=token, cmc_session=cmc_session)
    seed_if_empty()
    from session import _conn
    mes = datetime.now(_CHILE_TZ).strftime("%Y-%m")
    with _conn() as conn:
        activos = conn.execute("SELECT COUNT(*) c FROM proveedores WHERE activo=1").fetchone()["c"]
        oc_abiertas = conn.execute("SELECT COUNT(*) c FROM ordenes_compra WHERE estado!='pagada'").fetchone()["c"]
        gasto_mes = conn.execute("SELECT COALESCE(SUM(monto),0) t FROM ordenes_compra WHERE substr(fecha,1,7)=? AND estado IN ('recibida','pagada')", (mes,)).fetchone()["t"]
    return {"proveedores_activos": activos, "oc_abiertas": oc_abiertas, "gasto_compras_mes": gasto_mes}


@router.get("")
async def listar(rubro: str | None = Query(None), token: str | None = Query(None), cmc_session: str | None = Cookie(None), request: Request = None):
    require_admin(request, token=token, cmc_session=cmc_session)
    seed_if_empty()
    sql = "SELECT * FROM proveedores WHERE activo=1"; p = []
    if rubro and rubro != "Todos": sql += " AND rubro=?"; p.append(rubro)
    sql += " ORDER BY nombre"
    from session import _conn
    with _conn() as conn:
        rows = [dict(r) for r in conn.execute(sql, p).fetchall()]
    return {"proveedores": rows, "total": len(rows)}


@router.post("")
async def crear(request: Request, token: str | None = Query(None), cmc_session: str | None = Cookie(None)):
    require_admin(request, token=token, cmc_session=cmc_session)
    ensure_tables()
    try:
        b = await request.json()
    except Exception:
        raise HTTPException(400, "Body JSON inválido")
    nombre = (b.get("nombre") or "").strip()
    if not nombre:
        raise HTTPException(400, "nombre requerido")
    rubro = (b.get("rubro") or "Otro").strip()
    if rubro not in RUBROS: rubro = "Otro"
    from session import _conn
    with _conn() as conn:
        cur = conn.execute(
            """INSERT INTO proveedores (nombre, rubro, contacto, telefono, email, direccion, sitio_web, condiciones_pago, notas)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (nombre, rubro, (b.get("contacto") or "").strip(), (b.get("telefono") or "").strip(),
             (b.get("email") or "").strip(), (b.get("direccion") or "").strip(), (b.get("sitio_web") or "").strip(),
             (b.get("condiciones_pago") or "").strip(), (b.get("notas") or "").strip()))
        conn.commit()
        return {"ok": True, "id": cur.lastrowid}


@router.patch("/{pid}")
async def editar(pid: int, request: Request, token: str | None = Query(None), cmc_session: str | None = Cookie(None)):
    require_admin(request, token=token, cmc_session=cmc_session)
    ensure_tables()
    try:
        b = await request.json()
    except Exception:
        raise HTTPException(400, "Body JSON inválido")
    ok = {"nombre", "rubro", "contacto", "telefono", "email", "direccion", "sitio_web", "condiciones_pago", "notas"}
    sets, p = [], []
    for k, v in b.items():
        if k not in ok: continue
        if k == "rubro" and v not in RUBROS: v = "Otro"
        sets.append(f"{k}=?"); p.append(v)
    if not sets:
        raise HTTPException(400, "Nada que actualizar")
    sets.append("updated_at=datetime('now')"); p.append(pid)
    from session import _conn
    with _conn() as conn:
        cur = conn.execute(f"UPDATE proveedores SET {','.join(sets)} WHERE id=?", p)
        conn.commit()
    if cur.rowcount == 0:
        raise HTTPException(404, "No encontrado")
    return {"ok": True}


@router.delete("/{pid}")
async def eliminar(pid: int, token: str | None = Query(None), cmc_session: str | None = Cookie(None), request: Request = None):
    require_admin(request, token=token, cmc_session=cmc_session)
    ensure_tables()
    from session import _conn
    with _conn() as conn:
        cur = conn.execute("UPDATE proveedores SET activo=0, updated_at=datetime('now') WHERE id=?", (pid,))
        conn.commit()
    if cur.rowcount == 0:
        raise HTTPException(404, "No encontrado")
    return {"ok": True}


# ── Órdenes de compra ──
@router.get("/ordenes")
async def listar_oc(estado: str | None = Query(None), token: str | None = Query(None), cmc_session: str | None = Cookie(None), request: Request = None):
    require_admin(request, token=token, cmc_session=cmc_session)
    ensure_tables()
    sql = "SELECT * FROM ordenes_compra WHERE 1=1"; p = []
    if estado and estado != "todas": sql += " AND estado=?"; p.append(estado)
    sql += " ORDER BY fecha DESC, id DESC LIMIT 200"
    from session import _conn
    with _conn() as conn:
        rows = [dict(r) for r in conn.execute(sql, p).fetchall()]
    return {"ordenes": rows, "total": len(rows)}


@router.post("/ordenes")
async def crear_oc(request: Request, token: str | None = Query(None), cmc_session: str | None = Cookie(None)):
    require_admin(request, token=token, cmc_session=cmc_session)
    ensure_tables()
    try:
        b = await request.json()
    except Exception:
        raise HTTPException(400, "Body JSON inválido")
    fecha = (b.get("fecha") or datetime.now(_CHILE_TZ).strftime("%Y-%m-%d")).strip()
    estado = (b.get("estado") or "borrador").lower()
    if estado not in OC_ESTADOS: estado = "borrador"
    prov_id = b.get("proveedor_id")
    prov_nombre = (b.get("proveedor_nombre") or "").strip()
    from session import _conn
    with _conn() as conn:
        if prov_id and not prov_nombre:
            r = conn.execute("SELECT nombre FROM proveedores WHERE id=?", (prov_id,)).fetchone()
            prov_nombre = r["nombre"] if r else ""
        cur = conn.execute(
            "INSERT INTO ordenes_compra (proveedor_id, proveedor_nombre, fecha, descripcion, monto, estado, notas, creado_por) VALUES (?,?,?,?,?,?,?,?)",
            (prov_id, prov_nombre, fecha, (b.get("descripcion") or "").strip(), int(b.get("monto") or 0),
             estado, (b.get("notas") or "").strip(), (b.get("creado_por") or "recepcion").strip()))
        conn.commit()
        return {"ok": True, "id": cur.lastrowid}


@router.patch("/ordenes/{oc_id}")
async def editar_oc(oc_id: int, request: Request, token: str | None = Query(None), cmc_session: str | None = Cookie(None)):
    require_admin(request, token=token, cmc_session=cmc_session)
    ensure_tables()
    try:
        b = await request.json()
    except Exception:
        raise HTTPException(400, "Body JSON inválido")
    ok = {"proveedor_id", "proveedor_nombre", "fecha", "descripcion", "monto", "estado", "notas"}
    sets, p = [], []
    for k, v in b.items():
        if k not in ok: continue
        if k == "monto": v = int(v or 0)
        if k == "estado" and v not in OC_ESTADOS: continue
        sets.append(f"{k}=?"); p.append(v)
    if not sets:
        raise HTTPException(400, "Nada que actualizar")
    sets.append("updated_at=datetime('now')"); p.append(oc_id)
    from session import _conn
    with _conn() as conn:
        cur = conn.execute(f"UPDATE ordenes_compra SET {','.join(sets)} WHERE id=?", p)
        conn.commit()
    if cur.rowcount == 0:
        raise HTTPException(404, "No encontrada")
    return {"ok": True}


@router.delete("/ordenes/{oc_id}")
async def eliminar_oc(oc_id: int, token: str | None = Query(None), cmc_session: str | None = Cookie(None), request: Request = None):
    require_admin(request, token=token, cmc_session=cmc_session)
    ensure_tables()
    from session import _conn
    with _conn() as conn:
        cur = conn.execute("DELETE FROM ordenes_compra WHERE id=?", (oc_id,))
        conn.commit()
    if cur.rowcount == 0:
        raise HTTPException(404, "No encontrada")
    return {"ok": True}
