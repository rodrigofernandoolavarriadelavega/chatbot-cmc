"""
Router /alma/api/calidad — Calidad, incidentes y reclamos.

Registro de eventos de calidad: incidentes, eventos adversos, reclamos
(libro de reclamos digital — requisito legal/SEREMI), sugerencias y
felicitaciones. Cultura de seguridad del paciente con seguimiento de estado
y acción correctiva.

Tabla: calidad_eventos. Auth: alma_common.require_admin.
"""
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request, Query, Cookie

from alma_common import require_admin

log = logging.getLogger("calidad_routes")
_CHILE_TZ = ZoneInfo("America/Santiago")
router = APIRouter(prefix="/alma/api/calidad", tags=["calidad"])

TIPOS = ["Incidente", "Evento adverso", "Reclamo", "Sugerencia", "Felicitación"]
GRAVEDAD = ["leve", "moderado", "grave"]
ESTADOS = ["abierto", "en_analisis", "cerrado"]


def ensure_table() -> None:
    from session import _conn
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS calidad_eventos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fecha TEXT NOT NULL, tipo TEXT DEFAULT 'Incidente', titulo TEXT NOT NULL,
                descripcion TEXT DEFAULT '', gravedad TEXT DEFAULT 'leve', area TEXT DEFAULT '',
                paciente_nombre TEXT DEFAULT '', estado TEXT DEFAULT 'abierto',
                accion TEXT DEFAULT '', responsable TEXT DEFAULT '',
                creado_por TEXT DEFAULT 'recepcion', created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')))
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cal_estado ON calidad_eventos(estado)")
        conn.commit()


@router.get("/resumen")
async def resumen(token: str | None = Query(None), cmc_session: str | None = Cookie(None), request: Request = None):
    require_admin(request, token=token, cmc_session=cmc_session)
    ensure_table()
    from session import _conn
    mes = datetime.now(_CHILE_TZ).strftime("%Y-%m")
    with _conn() as conn:
        rows = [dict(r) for r in conn.execute("SELECT tipo, gravedad, estado, fecha FROM calidad_eventos").fetchall()]
    abiertos = sum(1 for r in rows if r["estado"] != "cerrado")
    graves = sum(1 for r in rows if r["gravedad"] == "grave" and r["estado"] != "cerrado")
    reclamos_mes = sum(1 for r in rows if r["tipo"] == "Reclamo" and (r["fecha"] or "").startswith(mes))
    por_tipo = {}
    for r in rows:
        por_tipo[r["tipo"]] = por_tipo.get(r["tipo"], 0) + 1
    return {"total": len(rows), "abiertos": abiertos, "graves_abiertos": graves, "reclamos_mes": reclamos_mes, "por_tipo": por_tipo}


@router.get("")
async def listar(estado: str | None = Query(None), tipo: str | None = Query(None),
                 token: str | None = Query(None), cmc_session: str | None = Cookie(None), request: Request = None):
    require_admin(request, token=token, cmc_session=cmc_session)
    ensure_table()
    sql = "SELECT * FROM calidad_eventos WHERE 1=1"; p = []
    if estado and estado != "todos": sql += " AND estado=?"; p.append(estado)
    if tipo and tipo != "Todos": sql += " AND tipo=?"; p.append(tipo)
    sql += " ORDER BY CASE estado WHEN 'abierto' THEN 0 WHEN 'en_analisis' THEN 1 ELSE 2 END, fecha DESC, id DESC"
    from session import _conn
    with _conn() as conn:
        rows = [dict(r) for r in conn.execute(sql, p).fetchall()]
    return {"eventos": rows, "total": len(rows)}


@router.post("")
async def crear(request: Request, token: str | None = Query(None), cmc_session: str | None = Cookie(None)):
    require_admin(request, token=token, cmc_session=cmc_session)
    ensure_table()
    try:
        b = await request.json()
    except Exception:
        raise HTTPException(400, "Body JSON inválido")
    titulo = (b.get("titulo") or "").strip()
    if not titulo:
        raise HTTPException(400, "titulo requerido")
    tipo = (b.get("tipo") or "Incidente").strip()
    if tipo not in TIPOS: tipo = "Incidente"
    grav = (b.get("gravedad") or "leve").lower()
    if grav not in GRAVEDAD: grav = "leve"
    fecha = (b.get("fecha") or datetime.now(_CHILE_TZ).strftime("%Y-%m-%d")).strip()
    from session import _conn
    with _conn() as conn:
        cur = conn.execute(
            """INSERT INTO calidad_eventos (fecha, tipo, titulo, descripcion, gravedad, area, paciente_nombre, estado, accion, responsable, creado_por)
               VALUES (?,?,?,?,?,?,?, 'abierto', ?, ?, ?)""",
            (fecha, tipo, titulo, (b.get("descripcion") or "").strip(), grav, (b.get("area") or "").strip(),
             (b.get("paciente_nombre") or "").strip(), (b.get("accion") or "").strip(),
             (b.get("responsable") or "").strip(), (b.get("creado_por") or "recepcion").strip()))
        conn.commit()
        return {"ok": True, "id": cur.lastrowid}


@router.patch("/{ev_id}")
async def editar(ev_id: int, request: Request, token: str | None = Query(None), cmc_session: str | None = Cookie(None)):
    require_admin(request, token=token, cmc_session=cmc_session)
    ensure_table()
    try:
        b = await request.json()
    except Exception:
        raise HTTPException(400, "Body JSON inválido")
    ok = {"fecha", "tipo", "titulo", "descripcion", "gravedad", "area", "paciente_nombre", "estado", "accion", "responsable"}
    sets, p = [], []
    for k, v in b.items():
        if k not in ok: continue
        if k == "gravedad" and v not in GRAVEDAD: continue
        if k == "estado" and v not in ESTADOS: continue
        if k == "tipo" and v not in TIPOS: continue
        sets.append(f"{k}=?"); p.append(v)
    if not sets:
        raise HTTPException(400, "Nada que actualizar")
    sets.append("updated_at=datetime('now')"); p.append(ev_id)
    from session import _conn
    with _conn() as conn:
        cur = conn.execute(f"UPDATE calidad_eventos SET {','.join(sets)} WHERE id=?", p)
        conn.commit()
    if cur.rowcount == 0:
        raise HTTPException(404, "No encontrado")
    return {"ok": True}


@router.delete("/{ev_id}")
async def eliminar(ev_id: int, token: str | None = Query(None), cmc_session: str | None = Cookie(None), request: Request = None):
    require_admin(request, token=token, cmc_session=cmc_session)
    ensure_table()
    from session import _conn
    with _conn() as conn:
        cur = conn.execute("DELETE FROM calidad_eventos WHERE id=?", (ev_id,))
        conn.commit()
    if cur.rowcount == 0:
        raise HTTPException(404, "No encontrado")
    return {"ok": True}
