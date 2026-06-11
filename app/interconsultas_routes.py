"""
Router /alma/api/interconsultas — Derivaciones interdisciplinarias.

El corazón "interdisciplinario" de la clínica: deriva un paciente de una
especialidad a otra (traumato→kine, ORL↔fono, MG→cardio, odonto→estética...),
con prioridad y estado. Permite ver la red de derivaciones entre disciplinas.

Tabla self-contained: interconsultas. Auth: alma_common.require_admin.
"""
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request, Query, Cookie

from alma_common import require_admin

log = logging.getLogger("interconsultas_routes")
_CHILE_TZ = ZoneInfo("America/Santiago")
router = APIRouter(prefix="/alma/api/interconsultas", tags=["interconsultas"])

PRIORIDADES = ["rutina", "preferente", "urgente"]
ESTADOS = ["pendiente", "agendada", "completada", "rechazada"]


def ensure_table() -> None:
    from session import db as _conn
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS interconsultas (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                fecha               TEXT NOT NULL,
                paciente_nombre     TEXT NOT NULL DEFAULT '',
                rut                 TEXT DEFAULT '',
                telefono            TEXT DEFAULT '',
                origen_especialidad TEXT DEFAULT '',
                origen_profesional  TEXT DEFAULT '',
                destino_especialidad TEXT DEFAULT '',
                destino_profesional TEXT DEFAULT '',
                motivo              TEXT DEFAULT '',
                prioridad           TEXT DEFAULT 'rutina',
                estado              TEXT DEFAULT 'pendiente',
                notas               TEXT DEFAULT '',
                creado_por          TEXT DEFAULT 'recepcion',
                created_at          TEXT DEFAULT (datetime('now')),
                updated_at          TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ic_estado ON interconsultas(estado)")
        conn.commit()


@router.get("/resumen")
async def resumen(token: str | None = Query(None), cmc_session: str | None = Cookie(None), request: Request = None):
    require_admin(request, token=token, cmc_session=cmc_session)
    ensure_table()
    from session import db as _conn
    with _conn() as conn:
        rows = [dict(r) for r in conn.execute("SELECT estado, prioridad, destino_especialidad FROM interconsultas").fetchall()]
    pend = sum(1 for r in rows if r["estado"] == "pendiente")
    urg = sum(1 for r in rows if r["prioridad"] == "urgente" and r["estado"] in ("pendiente", "agendada"))
    comp = sum(1 for r in rows if r["estado"] == "completada")
    red: dict[str, int] = {}
    for r in rows:
        d = r["destino_especialidad"] or "—"
        red[d] = red.get(d, 0) + 1
    return {"total": len(rows), "pendientes": pend, "urgentes": urg, "completadas": comp, "por_destino": red}


@router.get("")
async def listar(estado: str | None = Query(None), prioridad: str | None = Query(None),
                 token: str | None = Query(None), cmc_session: str | None = Cookie(None), request: Request = None):
    require_admin(request, token=token, cmc_session=cmc_session)
    ensure_table()
    sql = "SELECT * FROM interconsultas WHERE 1=1"; p = []
    if estado and estado != "todas": sql += " AND estado=?"; p.append(estado)
    if prioridad and prioridad != "todas": sql += " AND prioridad=?"; p.append(prioridad)
    sql += " ORDER BY CASE prioridad WHEN 'urgente' THEN 0 WHEN 'preferente' THEN 1 ELSE 2 END, fecha DESC, id DESC"
    from session import db as _conn
    with _conn() as conn:
        rows = [dict(r) for r in conn.execute(sql, p).fetchall()]
    return {"interconsultas": rows, "total": len(rows)}


@router.post("")
async def crear(request: Request, token: str | None = Query(None), cmc_session: str | None = Cookie(None)):
    require_admin(request, token=token, cmc_session=cmc_session)
    ensure_table()
    try:
        b = await request.json()
    except Exception:
        raise HTTPException(400, "Body JSON inválido")
    nombre = (b.get("paciente_nombre") or "").strip()
    if not nombre:
        raise HTTPException(400, "paciente_nombre requerido")
    if not (b.get("destino_especialidad") or "").strip():
        raise HTTPException(400, "destino_especialidad requerido")
    prioridad = (b.get("prioridad") or "rutina").lower()
    if prioridad not in PRIORIDADES: prioridad = "rutina"
    fecha = (b.get("fecha") or datetime.now(_CHILE_TZ).strftime("%Y-%m-%d")).strip()
    from session import db as _conn
    with _conn() as conn:
        cur = conn.execute(
            """INSERT INTO interconsultas
               (fecha, paciente_nombre, rut, telefono, origen_especialidad, origen_profesional,
                destino_especialidad, destino_profesional, motivo, prioridad, estado, notas, creado_por)
               VALUES (?,?,?,?,?,?,?,?,?,?, 'pendiente', ?, ?)""",
            (fecha, nombre, (b.get("rut") or "").strip(), (b.get("telefono") or "").strip(),
             (b.get("origen_especialidad") or "").strip(), (b.get("origen_profesional") or "").strip(),
             (b.get("destino_especialidad") or "").strip(), (b.get("destino_profesional") or "").strip(),
             (b.get("motivo") or "").strip(), prioridad, (b.get("notas") or "").strip(),
             (b.get("creado_por") or "recepcion").strip()))
        conn.commit()
        return {"ok": True, "id": cur.lastrowid}


@router.patch("/{ic_id}")
async def editar(ic_id: int, request: Request, token: str | None = Query(None), cmc_session: str | None = Cookie(None)):
    require_admin(request, token=token, cmc_session=cmc_session)
    ensure_table()
    try:
        b = await request.json()
    except Exception:
        raise HTTPException(400, "Body JSON inválido")
    ok = {"paciente_nombre", "rut", "telefono", "origen_especialidad", "origen_profesional",
          "destino_especialidad", "destino_profesional", "motivo", "prioridad", "estado", "notas", "fecha"}
    sets, p = [], []
    for k, v in b.items():
        if k not in ok: continue
        if k == "prioridad" and v not in PRIORIDADES: v = "rutina"
        if k == "estado" and v not in ESTADOS: continue
        sets.append(f"{k}=?"); p.append(v)
    if not sets:
        raise HTTPException(400, "Nada que actualizar")
    sets.append("updated_at=datetime('now')"); p.append(ic_id)
    from session import db as _conn
    with _conn() as conn:
        cur = conn.execute(f"UPDATE interconsultas SET {','.join(sets)} WHERE id=?", p)
        conn.commit()
    if cur.rowcount == 0:
        raise HTTPException(404, "No encontrada")
    return {"ok": True}


@router.delete("/{ic_id}")
async def eliminar(ic_id: int, token: str | None = Query(None), cmc_session: str | None = Cookie(None), request: Request = None):
    require_admin(request, token=token, cmc_session=cmc_session)
    ensure_table()
    from session import db as _conn
    with _conn() as conn:
        cur = conn.execute("DELETE FROM interconsultas WHERE id=?", (ic_id,))
        conn.commit()
    if cur.rowcount == 0:
        raise HTTPException(404, "No encontrada")
    return {"ok": True}
