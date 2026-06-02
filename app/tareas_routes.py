"""
Router /alma/api/tareas — Tablero de tareas operativas del equipo.

Pendientes de la clínica (llamar a un paciente, pedir un insumo, preparar un box):
asignación, prioridad, vencimiento y estado. El pegamento operativo que conecta
los demás módulos.

Tabla: tareas_cmc. Auth: alma_common.require_admin.
"""
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request, Query, Cookie

from alma_common import require_admin

log = logging.getLogger("tareas_routes")
_CHILE_TZ = ZoneInfo("America/Santiago")
router = APIRouter(prefix="/alma/api/tareas", tags=["tareas"])

PRIORIDADES = ["baja", "media", "alta"]
ESTADOS = ["pendiente", "en_curso", "hecha"]


def ensure_table() -> None:
    from session import _conn
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tareas_cmc (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                titulo TEXT NOT NULL, descripcion TEXT DEFAULT '', asignado TEXT DEFAULT '',
                prioridad TEXT DEFAULT 'media', estado TEXT DEFAULT 'pendiente', vence TEXT DEFAULT '',
                creado_por TEXT DEFAULT 'recepcion', created_at TEXT DEFAULT (datetime('now')),
                done_at TEXT DEFAULT '', updated_at TEXT DEFAULT (datetime('now')))
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tar_estado ON tareas_cmc(estado)")
        conn.commit()


def _vencida(vence: str, estado: str) -> bool:
    if estado == "hecha" or not vence:
        return False
    try:
        return datetime.strptime(vence[:10], "%Y-%m-%d").date() < datetime.now(_CHILE_TZ).date()
    except ValueError:
        return False


@router.get("/resumen")
async def resumen(token: str | None = Query(None), cmc_session: str | None = Cookie(None), request: Request = None):
    require_admin(request, token=token, cmc_session=cmc_session)
    ensure_table()
    from session import _conn
    with _conn() as conn:
        rows = [dict(r) for r in conn.execute("SELECT estado, vence, done_at FROM tareas_cmc").fetchall()]
    pend = sum(1 for r in rows if r["estado"] != "hecha")
    venc = sum(1 for r in rows if _vencida(r["vence"], r["estado"]))
    en_curso = sum(1 for r in rows if r["estado"] == "en_curso")
    return {"total": len(rows), "pendientes": pend, "vencidas": venc, "en_curso": en_curso}


@router.get("")
async def listar(estado: str | None = Query(None), token: str | None = Query(None), cmc_session: str | None = Cookie(None), request: Request = None):
    require_admin(request, token=token, cmc_session=cmc_session)
    ensure_table()
    sql = "SELECT * FROM tareas_cmc WHERE 1=1"; p = []
    if estado and estado != "todas": sql += " AND estado=?"; p.append(estado)
    sql += " ORDER BY CASE estado WHEN 'en_curso' THEN 0 WHEN 'pendiente' THEN 1 ELSE 2 END, CASE prioridad WHEN 'alta' THEN 0 WHEN 'media' THEN 1 ELSE 2 END, vence, id DESC"
    from session import _conn
    with _conn() as conn:
        rows = [dict(r) for r in conn.execute(sql, p).fetchall()]
    for r in rows:
        r["vencida"] = _vencida(r["vence"], r["estado"])
    return {"tareas": rows, "total": len(rows)}


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
    prio = (b.get("prioridad") or "media").lower()
    if prio not in PRIORIDADES: prio = "media"
    from session import _conn
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO tareas_cmc (titulo, descripcion, asignado, prioridad, estado, vence, creado_por) VALUES (?,?,?,?, 'pendiente', ?, ?)",
            (titulo, (b.get("descripcion") or "").strip(), (b.get("asignado") or "").strip(), prio,
             (b.get("vence") or "").strip(), (b.get("creado_por") or "recepcion").strip()))
        conn.commit()
        return {"ok": True, "id": cur.lastrowid}


@router.patch("/{tid}")
async def editar(tid: int, request: Request, token: str | None = Query(None), cmc_session: str | None = Cookie(None)):
    require_admin(request, token=token, cmc_session=cmc_session)
    ensure_table()
    try:
        b = await request.json()
    except Exception:
        raise HTTPException(400, "Body JSON inválido")
    ok = {"titulo", "descripcion", "asignado", "prioridad", "estado", "vence"}
    sets, p = [], []
    for k, v in b.items():
        if k not in ok: continue
        if k == "prioridad" and v not in PRIORIDADES: continue
        if k == "estado" and v not in ESTADOS: continue
        sets.append(f"{k}=?"); p.append(v)
    if b.get("estado") == "hecha":
        sets.append("done_at=?"); p.append(datetime.now(_CHILE_TZ).strftime("%Y-%m-%d %H:%M"))
    elif b.get("estado") in ("pendiente", "en_curso"):
        sets.append("done_at=?"); p.append("")
    if not sets:
        raise HTTPException(400, "Nada que actualizar")
    sets.append("updated_at=datetime('now')"); p.append(tid)
    from session import _conn
    with _conn() as conn:
        cur = conn.execute(f"UPDATE tareas_cmc SET {','.join(sets)} WHERE id=?", p)
        conn.commit()
    if cur.rowcount == 0:
        raise HTTPException(404, "No encontrada")
    return {"ok": True}


@router.delete("/{tid}")
async def eliminar(tid: int, token: str | None = Query(None), cmc_session: str | None = Cookie(None), request: Request = None):
    require_admin(request, token=token, cmc_session=cmc_session)
    ensure_table()
    from session import _conn
    with _conn() as conn:
        cur = conn.execute("DELETE FROM tareas_cmc WHERE id=?", (tid,))
        conn.commit()
    if cur.rowcount == 0:
        raise HTTPException(404, "No encontrada")
    return {"ok": True}
