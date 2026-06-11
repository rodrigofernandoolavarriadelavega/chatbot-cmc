"""
Router /alma/api/finanzas — Cockpit financiero: egresos + flujo + resultado.

Hoy el stack registra INGRESOS (pagos_cmc) pero no EGRESOS. Este módulo cierra
el círculo: registra gastos (arriendo, sueldos, honorarios, insumos, servicios,
marketing, equipamiento, impuestos) y cruza ingresos vs egresos por mes para
mostrar el resultado operacional real.

Tabla self-contained: egresos_cmc. Ingresos: lee pagos_cmc (copago = caja real).
Auth: alma_common.require_admin.
"""
import logging
import csv, io
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request, Query, Cookie
from fastapi.responses import StreamingResponse

from alma_common import require_admin

log = logging.getLogger("finanzas_routes")
_CHILE_TZ = ZoneInfo("America/Santiago")
router = APIRouter(prefix="/alma/api/finanzas", tags=["finanzas"])

CATEGORIAS = ["Arriendo", "Sueldos", "Honorarios", "Insumos", "Servicios básicos",
              "Marketing", "Equipamiento", "Impuestos", "Mantención", "Otros"]
METODOS = ["transferencia", "efectivo", "debito", "credito", "automatico"]


def ensure_table() -> None:
    from session import db as _conn
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS egresos_cmc (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                fecha         TEXT NOT NULL,
                categoria     TEXT DEFAULT 'Otros',
                descripcion   TEXT DEFAULT '',
                monto         INTEGER DEFAULT 0,
                metodo_pago   TEXT DEFAULT 'transferencia',
                proveedor     TEXT DEFAULT '',
                recurrente    INTEGER DEFAULT 0,
                notas         TEXT DEFAULT '',
                creado_por    TEXT DEFAULT 'recepcion',
                created_at    TEXT DEFAULT (datetime('now')),
                updated_at    TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_egr_fecha ON egresos_cmc(fecha)")
        conn.commit()


def _ingresos_mes(conn, mes: str) -> int:
    try:
        r = conn.execute("SELECT COALESCE(SUM(copago),0) t FROM pagos_cmc WHERE substr(fecha,1,7)=?", (mes,)).fetchone()
        return r["t"] or 0
    except Exception:
        return 0


@router.get("/flujo")
async def flujo(mes: str | None = Query(None, description="YYYY-MM (default mes actual)"),
                token: str | None = Query(None), cmc_session: str | None = Cookie(None), request: Request = None):
    require_admin(request, token=token, cmc_session=cmc_session)
    ensure_table()
    mes = mes or datetime.now(_CHILE_TZ).strftime("%Y-%m")
    from session import db as _conn
    with _conn() as conn:
        ingresos = _ingresos_mes(conn, mes)
        egresos_rows = [dict(r) for r in conn.execute(
            "SELECT categoria, COALESCE(SUM(monto),0) t FROM egresos_cmc WHERE substr(fecha,1,7)=? GROUP BY categoria", (mes,)
        ).fetchall()]
        egresos_total = sum(r["t"] for r in egresos_rows)
        # serie de los últimos 6 meses
        serie = []
        base = datetime.strptime(mes + "-01", "%Y-%m-%d")
        for i in range(5, -1, -1):
            y = base.year; m = base.month - i
            while m <= 0: m += 12; y -= 1
            mm = f"{y:04d}-{m:02d}"
            ing = _ingresos_mes(conn, mm)
            egr = conn.execute("SELECT COALESCE(SUM(monto),0) t FROM egresos_cmc WHERE substr(fecha,1,7)=?", (mm,)).fetchone()["t"] or 0
            serie.append({"mes": mm, "ingresos": ing, "egresos": egr, "neto": ing - egr})
    neto = ingresos - egresos_total
    por_cat = {r["categoria"]: r["t"] for r in egresos_rows}
    return {"mes": mes, "ingresos": ingresos, "egresos": egresos_total, "neto": neto,
            "margen_pct": round(neto / ingresos * 100) if ingresos else 0,
            "por_categoria": por_cat, "serie": serie}


@router.get("/egresos")
async def listar(mes: str | None = Query(None), categoria: str | None = Query(None),
                 token: str | None = Query(None), cmc_session: str | None = Cookie(None), request: Request = None):
    require_admin(request, token=token, cmc_session=cmc_session)
    ensure_table()
    mes = mes or datetime.now(_CHILE_TZ).strftime("%Y-%m")
    sql = "SELECT * FROM egresos_cmc WHERE substr(fecha,1,7)=?"; p = [mes]
    if categoria and categoria != "Todas": sql += " AND categoria=?"; p.append(categoria)
    sql += " ORDER BY fecha DESC, id DESC"
    from session import db as _conn
    with _conn() as conn:
        rows = [dict(r) for r in conn.execute(sql, p).fetchall()]
    return {"egresos": rows, "total": len(rows), "suma": sum(r["monto"] or 0 for r in rows)}


@router.post("/egresos")
async def crear(request: Request, token: str | None = Query(None), cmc_session: str | None = Cookie(None)):
    require_admin(request, token=token, cmc_session=cmc_session)
    ensure_table()
    try:
        b = await request.json()
    except Exception:
        raise HTTPException(400, "Body JSON inválido")
    monto = int(b.get("monto") or 0)
    if monto <= 0:
        raise HTTPException(400, "monto debe ser > 0")
    categoria = (b.get("categoria") or "Otros").strip()
    if categoria not in CATEGORIAS: categoria = "Otros"
    fecha = (b.get("fecha") or datetime.now(_CHILE_TZ).strftime("%Y-%m-%d")).strip()
    from session import db as _conn
    with _conn() as conn:
        cur = conn.execute(
            """INSERT INTO egresos_cmc (fecha, categoria, descripcion, monto, metodo_pago, proveedor, recurrente, notas, creado_por)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (fecha, categoria, (b.get("descripcion") or "").strip(), monto,
             (b.get("metodo_pago") or "transferencia"), (b.get("proveedor") or "").strip(),
             1 if b.get("recurrente") else 0, (b.get("notas") or "").strip(),
             (b.get("creado_por") or "recepcion").strip()))
        conn.commit()
        return {"ok": True, "id": cur.lastrowid}


@router.patch("/egresos/{eg_id}")
async def editar(eg_id: int, request: Request, token: str | None = Query(None), cmc_session: str | None = Cookie(None)):
    require_admin(request, token=token, cmc_session=cmc_session)
    ensure_table()
    try:
        b = await request.json()
    except Exception:
        raise HTTPException(400, "Body JSON inválido")
    ok = {"fecha", "categoria", "descripcion", "monto", "metodo_pago", "proveedor", "recurrente", "notas"}
    sets, p = [], []
    for k, v in b.items():
        if k not in ok: continue
        if k == "monto": v = int(v or 0)
        if k == "recurrente": v = 1 if v else 0
        if k == "categoria" and v not in CATEGORIAS: v = "Otros"
        sets.append(f"{k}=?"); p.append(v)
    if not sets:
        raise HTTPException(400, "Nada que actualizar")
    sets.append("updated_at=datetime('now')"); p.append(eg_id)
    from session import db as _conn
    with _conn() as conn:
        cur = conn.execute(f"UPDATE egresos_cmc SET {','.join(sets)} WHERE id=?", p)
        conn.commit()
    if cur.rowcount == 0:
        raise HTTPException(404, "No encontrado")
    return {"ok": True}


@router.delete("/egresos/{eg_id}")
async def eliminar(eg_id: int, token: str | None = Query(None), cmc_session: str | None = Cookie(None), request: Request = None):
    require_admin(request, token=token, cmc_session=cmc_session)
    ensure_table()
    from session import db as _conn
    with _conn() as conn:
        cur = conn.execute("DELETE FROM egresos_cmc WHERE id=?", (eg_id,))
        conn.commit()
    if cur.rowcount == 0:
        raise HTTPException(404, "No encontrado")
    return {"ok": True}


@router.get("/export")
async def export_csv(mes: str | None = Query(None), token: str | None = Query(None), cmc_session: str | None = Cookie(None), request: Request = None):
    require_admin(request, token=token, cmc_session=cmc_session)
    ensure_table()
    mes = mes or datetime.now(_CHILE_TZ).strftime("%Y-%m")
    from session import db as _conn
    with _conn() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM egresos_cmc WHERE substr(fecha,1,7)=? ORDER BY fecha", (mes,)).fetchall()]
    buf = io.StringIO(); w = csv.writer(buf, delimiter=";")
    w.writerow(["Fecha", "Categoria", "Descripcion", "Monto", "Metodo", "Proveedor", "Recurrente", "Notas"])
    for r in rows:
        w.writerow([r["fecha"], r["categoria"], r["descripcion"], r["monto"], r["metodo_pago"], r["proveedor"], "si" if r["recurrente"] else "no", r["notas"]])
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="egresos_{mes}.csv"'})
