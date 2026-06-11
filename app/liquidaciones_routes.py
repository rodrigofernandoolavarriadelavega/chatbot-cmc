"""
Router /alma/api/liquidaciones — Liquidación de honorarios por profesional.

Conecta Equipo (% honorario por profesional) con Pagos (producción real en caja):
para un período (mes), calcula la producción de cada profesional (Σ copago en
pagos_cmc) y su honorario (producción × %). Permite ajustes manuales y marcar
pagado/pendiente. Resuelve la tarea recurrente de "cuánto le pago a cada uno".

Tabla: liquidaciones. Lee: pagos_cmc + equipo_cmc. Auth: alma_common.require_admin.

Nota: la producción base es el copago en caja (lo realmente recaudado). Para
honorarios sobre arancel total, usar el campo Ajuste. El cuadre fino de Imed lo
hace Conciliación.
"""
import logging
import csv, io
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request, Query, Cookie
from fastapi.responses import StreamingResponse

from alma_common import require_admin

log = logging.getLogger("liquidaciones_routes")
_CHILE_TZ = ZoneInfo("America/Santiago")
router = APIRouter(prefix="/alma/api/liquidaciones", tags=["liquidaciones"])

ESTADOS = ["pendiente", "pagado"]


def ensure_table() -> None:
    from session import db as _conn
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS liquidaciones (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                periodo TEXT NOT NULL, id_medilink INTEGER, nombre TEXT DEFAULT '',
                produccion INTEGER DEFAULT 0, pct INTEGER DEFAULT 0,
                monto_calculado INTEGER DEFAULT 0, ajuste INTEGER DEFAULT 0,
                estado TEXT DEFAULT 'pendiente', pagado_fecha TEXT DEFAULT '',
                notas TEXT DEFAULT '', updated_at TEXT DEFAULT (datetime('now')),
                UNIQUE(periodo, id_medilink))
        """)
        conn.commit()


def _generar(periodo: str) -> int:
    """Calcula/actualiza la liquidación del período desde pagos + equipo.
    Preserva ajuste/estado/notas de filas ya existentes. Devuelve nº de profesionales."""
    ensure_table()
    from session import db as _conn
    with _conn() as conn:
        # producción por profesional en el período (copago = caja real)
        prod = {}
        try:
            for r in conn.execute(
                "SELECT id_profesional, COALESCE(SUM(copago),0) t FROM pagos_cmc "
                "WHERE substr(fecha,1,7)=? AND id_profesional IS NOT NULL GROUP BY id_profesional", (periodo,)):
                prod[int(r["id_profesional"])] = r["t"] or 0
        except Exception:
            prod = {}
        # equipo con % honorario
        equipo = [dict(r) for r in conn.execute(
            "SELECT id_medilink, nombre, pct_honorario FROM equipo_cmc WHERE id_medilink IS NOT NULL")]
        n = 0
        for e in equipo:
            mid = int(e["id_medilink"])
            produccion = prod.get(mid, 0)
            pct = int(e["pct_honorario"] or 0)
            # solo crear/actualizar si hay producción o ya existe una fila
            existe = conn.execute("SELECT id FROM liquidaciones WHERE periodo=? AND id_medilink=?", (periodo, mid)).fetchone()
            if produccion == 0 and not existe:
                continue
            monto = round(produccion * pct / 100)
            if existe:
                conn.execute(
                    "UPDATE liquidaciones SET nombre=?, produccion=?, pct=?, monto_calculado=?, updated_at=datetime('now') "
                    "WHERE periodo=? AND id_medilink=?",
                    (e["nombre"], produccion, pct, monto, periodo, mid))
            else:
                conn.execute(
                    "INSERT INTO liquidaciones (periodo, id_medilink, nombre, produccion, pct, monto_calculado) VALUES (?,?,?,?,?,?)",
                    (periodo, mid, e["nombre"], produccion, pct, monto))
            n += 1
        conn.commit()
    return n


@router.post("/generar")
async def generar(periodo: str | None = Query(None), token: str | None = Query(None), cmc_session: str | None = Cookie(None), request: Request = None):
    require_admin(request, token=token, cmc_session=cmc_session)
    periodo = periodo or datetime.now(_CHILE_TZ).strftime("%Y-%m")
    n = _generar(periodo)
    return {"ok": True, "periodo": periodo, "profesionales": n}


@router.get("/resumen")
async def resumen(periodo: str | None = Query(None), token: str | None = Query(None), cmc_session: str | None = Cookie(None), request: Request = None):
    require_admin(request, token=token, cmc_session=cmc_session)
    ensure_table()
    periodo = periodo or datetime.now(_CHILE_TZ).strftime("%Y-%m")
    from session import db as _conn
    with _conn() as conn:
        rows = [dict(r) for r in conn.execute("SELECT monto_calculado, ajuste, estado FROM liquidaciones WHERE periodo=?", (periodo,)).fetchall()]
    total = sum((r["monto_calculado"] or 0) + (r["ajuste"] or 0) for r in rows)
    pagado = sum((r["monto_calculado"] or 0) + (r["ajuste"] or 0) for r in rows if r["estado"] == "pagado")
    return {"periodo": periodo, "n_profesionales": len(rows), "total_honorarios": total,
            "pagado": pagado, "pendiente": total - pagado}


@router.get("")
async def listar(periodo: str | None = Query(None), token: str | None = Query(None), cmc_session: str | None = Cookie(None), request: Request = None):
    require_admin(request, token=token, cmc_session=cmc_session)
    ensure_table()
    periodo = periodo or datetime.now(_CHILE_TZ).strftime("%Y-%m")
    from session import db as _conn
    with _conn() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM liquidaciones WHERE periodo=? ORDER BY (monto_calculado+ajuste) DESC", (periodo,)).fetchall()]
    for r in rows:
        r["monto_final"] = (r["monto_calculado"] or 0) + (r["ajuste"] or 0)
    return {"liquidaciones": rows, "total": len(rows), "periodo": periodo}


@router.patch("/{liq_id}")
async def editar(liq_id: int, request: Request, token: str | None = Query(None), cmc_session: str | None = Cookie(None)):
    require_admin(request, token=token, cmc_session=cmc_session)
    ensure_table()
    try:
        b = await request.json()
    except Exception:
        raise HTTPException(400, "Body JSON inválido")
    sets, p = [], []
    if "ajuste" in b: sets.append("ajuste=?"); p.append(int(b["ajuste"] or 0))
    if "notas" in b: sets.append("notas=?"); p.append((b["notas"] or "").strip())
    if "estado" in b and b["estado"] in ESTADOS:
        sets.append("estado=?"); p.append(b["estado"])
        sets.append("pagado_fecha=?"); p.append(datetime.now(_CHILE_TZ).strftime("%Y-%m-%d") if b["estado"] == "pagado" else "")
    if "pct" in b:
        sets.append("pct=?"); p.append(int(b["pct"] or 0))
    if not sets:
        raise HTTPException(400, "Nada que actualizar")
    sets.append("updated_at=datetime('now')"); p.append(liq_id)
    from session import db as _conn
    with _conn() as conn:
        # si cambió pct, recalcular monto_calculado
        cur = conn.execute(f"UPDATE liquidaciones SET {','.join(sets)} WHERE id=?", p)
        if "pct" in b:
            conn.execute("UPDATE liquidaciones SET monto_calculado=round(produccion*pct/100.0) WHERE id=?", (liq_id,))
        conn.commit()
    if cur.rowcount == 0:
        raise HTTPException(404, "No encontrada")
    return {"ok": True}


@router.get("/export")
async def export_csv(periodo: str | None = Query(None), token: str | None = Query(None), cmc_session: str | None = Cookie(None), request: Request = None):
    require_admin(request, token=token, cmc_session=cmc_session)
    ensure_table()
    periodo = periodo or datetime.now(_CHILE_TZ).strftime("%Y-%m")
    from session import db as _conn
    with _conn() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM liquidaciones WHERE periodo=? ORDER BY (monto_calculado+ajuste) DESC", (periodo,)).fetchall()]
    buf = io.StringIO(); w = csv.writer(buf, delimiter=";")
    w.writerow(["Periodo", "Profesional", "Produccion", "%", "Calculado", "Ajuste", "A pagar", "Estado", "Pagado fecha"])
    for r in rows:
        final = (r["monto_calculado"] or 0) + (r["ajuste"] or 0)
        w.writerow([r["periodo"], r["nombre"], r["produccion"], r["pct"], r["monto_calculado"], r["ajuste"], final, r["estado"], r["pagado_fecha"]])
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="liquidaciones_{periodo}.csv"'})
