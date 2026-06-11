"""
Router /alma/api/mantencion — Equipos clínicos y mantención preventiva.

Ciclo de vida del equipamiento (autoclave, sillones dentales, ecógrafo, rayos,
compresor, refrigerador de vacunas): estado, última y próxima mantención, y un
libro de mantenciones (preventiva/correctiva/validación). Cubre el ítem SEREMI
"programa de mantención preventiva de equipos".

Tablas: equipos_clinicos, mantenciones. Auth: alma_common.require_admin.
"""
import logging
from datetime import datetime, date
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request, Query, Cookie

from alma_common import require_admin

log = logging.getLogger("mantencion_routes")
_CHILE_TZ = ZoneInfo("America/Santiago")
router = APIRouter(prefix="/alma/api/mantencion", tags=["mantencion"])

TIPOS = ["Autoclave", "Sillón dental", "Ecógrafo", "Rayos dental", "Compresor",
         "Refrigerador vacunas", "Esterilizador", "Otro"]
ESTADOS = ["operativo", "mantencion", "baja"]
TIPO_MANT = ["preventiva", "correctiva", "validacion"]

SEED = [
    ("Autoclave 1", "Autoclave", "Esterilización", 6),
    ("Sillón dental — Box 1", "Sillón dental", "Box 1", 12),
    ("Sillón dental — Box 2", "Sillón dental", "Box 2", 12),
    ("Ecógrafo", "Ecógrafo", "Box ecografía", 12),
    ("Compresor dental", "Compresor", "Sala máquinas", 12),
    ("Equipo rayos dental", "Rayos dental", "Box dental", 12),
    ("Refrigerador de vacunas (PNI)", "Refrigerador vacunas", "Vacunatorio", 6),
]


def _add_months(d: date, months: int) -> date:
    m = d.month - 1 + months
    y = d.year + m // 12
    m = m % 12 + 1
    import calendar
    day = min(d.day, calendar.monthrange(y, m)[1])
    return date(y, m, day)


def ensure_tables() -> None:
    from session import db as _conn
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS equipos_clinicos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nombre TEXT NOT NULL, tipo TEXT DEFAULT 'Otro', ubicacion TEXT DEFAULT '',
                marca TEXT DEFAULT '', modelo TEXT DEFAULT '', serie TEXT DEFAULT '',
                estado TEXT DEFAULT 'operativo', ultima_mantencion TEXT DEFAULT '',
                proxima_mantencion TEXT DEFAULT '', frecuencia_meses INTEGER DEFAULT 12,
                responsable TEXT DEFAULT '', notas TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now')), updated_at TEXT DEFAULT (datetime('now')))
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS mantenciones (
                id INTEGER PRIMARY KEY AUTOINCREMENT, equipo_id INTEGER NOT NULL,
                fecha TEXT NOT NULL, tipo TEXT DEFAULT 'preventiva', descripcion TEXT DEFAULT '',
                costo INTEGER DEFAULT 0, realizado_por TEXT DEFAULT '', notas TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now')))
        """)
        conn.commit()


def seed_if_empty() -> int:
    ensure_tables()
    from session import db as _conn
    with _conn() as conn:
        n = conn.execute("SELECT COUNT(*) c FROM equipos_clinicos").fetchone()["c"]
        if n > 0:
            return 0
        for nombre, tipo, ubic, freq in SEED:
            conn.execute("INSERT INTO equipos_clinicos (nombre, tipo, ubicacion, frecuencia_meses) VALUES (?,?,?,?)",
                         (nombre, tipo, ubic, freq))
        conn.commit()
    return len(SEED)


def _estado_mant(prox: str) -> str:
    if not prox:
        return "sin_plan"
    try:
        p = datetime.strptime(prox[:10], "%Y-%m-%d").date()
    except ValueError:
        return "sin_plan"
    hoy = datetime.now(_CHILE_TZ).date()
    if p < hoy:
        return "vencida"
    if (p - hoy).days <= 30:
        return "proxima"
    return "ok"


@router.get("/resumen")
async def resumen(token: str | None = Query(None), cmc_session: str | None = Cookie(None), request: Request = None):
    require_admin(request, token=token, cmc_session=cmc_session)
    seed_if_empty()
    from session import db as _conn
    with _conn() as conn:
        rows = [dict(r) for r in conn.execute("SELECT estado, proxima_mantencion FROM equipos_clinicos").fetchall()]
    oper = sum(1 for r in rows if r["estado"] == "operativo")
    venc = sum(1 for r in rows if _estado_mant(r["proxima_mantencion"]) == "vencida")
    prox = sum(1 for r in rows if _estado_mant(r["proxima_mantencion"]) == "proxima")
    return {"total": len(rows), "operativos": oper, "mant_vencida": venc, "mant_proxima": prox}


@router.get("")
async def listar(estado: str | None = Query(None), token: str | None = Query(None), cmc_session: str | None = Cookie(None), request: Request = None):
    require_admin(request, token=token, cmc_session=cmc_session)
    seed_if_empty()
    sql = "SELECT * FROM equipos_clinicos WHERE 1=1"; p = []
    if estado and estado != "todos": sql += " AND estado=?"; p.append(estado)
    sql += " ORDER BY tipo, nombre"
    from session import db as _conn
    with _conn() as conn:
        rows = [dict(r) for r in conn.execute(sql, p).fetchall()]
    for r in rows:
        r["estado_mant"] = _estado_mant(r["proxima_mantencion"])
    return {"equipos": rows, "total": len(rows)}


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
    from session import db as _conn
    with _conn() as conn:
        cur = conn.execute(
            """INSERT INTO equipos_clinicos (nombre, tipo, ubicacion, marca, modelo, serie, estado, frecuencia_meses, responsable, notas, proxima_mantencion)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (nombre, (b.get("tipo") or "Otro"), (b.get("ubicacion") or "").strip(), (b.get("marca") or "").strip(),
             (b.get("modelo") or "").strip(), (b.get("serie") or "").strip(), (b.get("estado") or "operativo"),
             int(b.get("frecuencia_meses") or 12), (b.get("responsable") or "").strip(), (b.get("notas") or "").strip(),
             (b.get("proxima_mantencion") or "").strip()))
        conn.commit()
        return {"ok": True, "id": cur.lastrowid}


@router.patch("/{eid}")
async def editar(eid: int, request: Request, token: str | None = Query(None), cmc_session: str | None = Cookie(None)):
    require_admin(request, token=token, cmc_session=cmc_session)
    ensure_tables()
    try:
        b = await request.json()
    except Exception:
        raise HTTPException(400, "Body JSON inválido")
    ok = {"nombre", "tipo", "ubicacion", "marca", "modelo", "serie", "estado", "ultima_mantencion",
          "proxima_mantencion", "frecuencia_meses", "responsable", "notas"}
    sets, p = [], []
    for k, v in b.items():
        if k not in ok: continue
        if k == "frecuencia_meses": v = int(v or 12)
        if k == "estado" and v not in ESTADOS: continue
        sets.append(f"{k}=?"); p.append(v)
    if not sets:
        raise HTTPException(400, "Nada que actualizar")
    sets.append("updated_at=datetime('now')"); p.append(eid)
    from session import db as _conn
    with _conn() as conn:
        cur = conn.execute(f"UPDATE equipos_clinicos SET {','.join(sets)} WHERE id=?", p)
        conn.commit()
    if cur.rowcount == 0:
        raise HTTPException(404, "No encontrado")
    return {"ok": True}


@router.post("/{eid}/mantencion")
async def registrar_mant(eid: int, request: Request, token: str | None = Query(None), cmc_session: str | None = Cookie(None)):
    require_admin(request, token=token, cmc_session=cmc_session)
    ensure_tables()
    try:
        b = await request.json()
    except Exception:
        raise HTTPException(400, "Body JSON inválido")
    tipo = (b.get("tipo") or "preventiva").lower()
    if tipo not in TIPO_MANT: tipo = "preventiva"
    fecha = (b.get("fecha") or datetime.now(_CHILE_TZ).strftime("%Y-%m-%d")).strip()
    from session import db as _conn
    with _conn() as conn:
        eq = conn.execute("SELECT frecuencia_meses FROM equipos_clinicos WHERE id=?", (eid,)).fetchone()
        if not eq:
            raise HTTPException(404, "Equipo no encontrado")
        conn.execute(
            "INSERT INTO mantenciones (equipo_id, fecha, tipo, descripcion, costo, realizado_por, notas) VALUES (?,?,?,?,?,?,?)",
            (eid, fecha, tipo, (b.get("descripcion") or "").strip(), int(b.get("costo") or 0),
             (b.get("realizado_por") or "").strip(), (b.get("notas") or "").strip()))
        # actualizar última y próxima (solo preventiva/validación recalcula próxima)
        prox = ""
        if tipo in ("preventiva", "validacion"):
            try:
                fd = datetime.strptime(fecha, "%Y-%m-%d").date()
                prox = _add_months(fd, int(eq["frecuencia_meses"] or 12)).isoformat()
            except ValueError:
                prox = ""
        if prox:
            conn.execute("UPDATE equipos_clinicos SET ultima_mantencion=?, proxima_mantencion=?, estado='operativo', updated_at=datetime('now') WHERE id=?", (fecha, prox, eid))
        else:
            conn.execute("UPDATE equipos_clinicos SET ultima_mantencion=?, updated_at=datetime('now') WHERE id=?", (fecha, eid))
        conn.commit()
    return {"ok": True, "proxima_mantencion": prox}


@router.get("/{eid}/historial")
async def historial(eid: int, token: str | None = Query(None), cmc_session: str | None = Cookie(None), request: Request = None):
    require_admin(request, token=token, cmc_session=cmc_session)
    ensure_tables()
    from session import db as _conn
    with _conn() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM mantenciones WHERE equipo_id=? ORDER BY fecha DESC, id DESC LIMIT 100", (eid,)).fetchall()]
    return {"mantenciones": rows}


@router.delete("/{eid}")
async def eliminar(eid: int, token: str | None = Query(None), cmc_session: str | None = Cookie(None), request: Request = None):
    require_admin(request, token=token, cmc_session=cmc_session)
    ensure_tables()
    from session import db as _conn
    with _conn() as conn:
        cur = conn.execute("DELETE FROM equipos_clinicos WHERE id=?", (eid,))
        conn.commit()
    if cur.rowcount == 0:
        raise HTTPException(404, "No encontrado")
    return {"ok": True}
