"""
Router /alma/api/equipo — Directorio del equipo / RRHH clínico.

Cada profesional con su especialidad, rol, tipo de contrato, % de honorario,
contacto y ESTADO (activo / licencia / inactivo). El estado de licencia resuelve
el pendiente conocido: el bot debe saber qué profesionales están de licencia para
no ofrecer sus horas. Se siembra desde PROFESIONALES (medilink.py) — staff real.

Tabla self-contained: equipo_cmc. Auth: alma_common.require_admin.
"""
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request, Query, Cookie

from alma_common import require_admin

log = logging.getLogger("equipo_routes")
_CHILE_TZ = ZoneInfo("America/Santiago")
router = APIRouter(prefix="/alma/api/equipo", tags=["equipo"])

ESTADOS = ["activo", "licencia", "inactivo"]
CONTRATOS = ["honorarios", "planta", "reemplazo"]


def _rol_de(esp: str) -> str:
    e = (esp or "").lower()
    if any(k in e for k in ["odonto", "ortodon", "endodon", "implant", "estética", "estetica"]):
        return "Dental"
    if "kinesi" in e or "masoter" in e:
        return "Kinesiología"
    if "psico" in e:
        return "Salud mental"
    if any(k in e for k in ["nutri", "fono", "podolog", "matrona"]):
        return "Profesional"
    return "Médico"


def ensure_table() -> None:
    from session import _conn
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS equipo_cmc (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                id_medilink   INTEGER,
                nombre        TEXT NOT NULL,
                especialidad  TEXT DEFAULT '',
                rol           TEXT DEFAULT '',
                tipo_contrato TEXT DEFAULT 'honorarios',
                pct_honorario INTEGER DEFAULT 0,
                telefono      TEXT DEFAULT '',
                email         TEXT DEFAULT '',
                estado        TEXT DEFAULT 'activo',
                licencia_desde TEXT DEFAULT '',
                licencia_hasta TEXT DEFAULT '',
                notas         TEXT DEFAULT '',
                created_at    TEXT DEFAULT (datetime('now')),
                updated_at    TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.commit()


def seed_if_empty() -> int:
    ensure_table()
    from session import _conn
    try:
        from medilink import PROFESIONALES
    except Exception:
        PROFESIONALES = {}
    with _conn() as conn:
        n = conn.execute("SELECT COUNT(*) c FROM equipo_cmc").fetchone()["c"]
        if n > 0:
            return 0
        for pid, info in PROFESIONALES.items():
            esp = info.get("especialidad", "")
            conn.execute(
                """INSERT INTO equipo_cmc (id_medilink, nombre, especialidad, rol, tipo_contrato, estado)
                   VALUES (?,?,?,?, 'honorarios', 'activo')""",
                (pid, info.get("nombre", ""), esp, _rol_de(esp)))
        conn.commit()
    log.info("equipo: sembrado desde PROFESIONALES (%d)", len(PROFESIONALES))
    return len(PROFESIONALES)


@router.get("/resumen")
async def resumen(token: str | None = Query(None), cmc_session: str | None = Cookie(None), request: Request = None):
    require_admin(request, token=token, cmc_session=cmc_session)
    seed_if_empty()
    from session import _conn
    with _conn() as conn:
        rows = [dict(r) for r in conn.execute("SELECT estado, rol FROM equipo_cmc").fetchall()]
    activos = sum(1 for r in rows if r["estado"] == "activo")
    lic = sum(1 for r in rows if r["estado"] == "licencia")
    por_rol: dict[str, int] = {}
    for r in rows:
        por_rol[r["rol"] or "—"] = por_rol.get(r["rol"] or "—", 0) + 1
    return {"total": len(rows), "activos": activos, "licencia": lic, "por_rol": por_rol}


@router.get("")
async def listar(estado: str | None = Query(None), rol: str | None = Query(None),
                 token: str | None = Query(None), cmc_session: str | None = Cookie(None), request: Request = None):
    require_admin(request, token=token, cmc_session=cmc_session)
    seed_if_empty()
    sql = "SELECT * FROM equipo_cmc WHERE 1=1"; p = []
    if estado and estado != "todos": sql += " AND estado=?"; p.append(estado)
    if rol and rol != "todos": sql += " AND rol=?"; p.append(rol)
    sql += " ORDER BY estado='inactivo', rol, nombre"
    from session import _conn
    with _conn() as conn:
        rows = [dict(r) for r in conn.execute(sql, p).fetchall()]
    return {"equipo": rows, "total": len(rows)}


@router.post("")
async def crear(request: Request, token: str | None = Query(None), cmc_session: str | None = Cookie(None)):
    require_admin(request, token=token, cmc_session=cmc_session)
    ensure_table()
    try:
        b = await request.json()
    except Exception:
        raise HTTPException(400, "Body JSON inválido")
    nombre = (b.get("nombre") or "").strip()
    if not nombre:
        raise HTTPException(400, "nombre requerido")
    esp = (b.get("especialidad") or "").strip()
    from session import _conn
    with _conn() as conn:
        cur = conn.execute(
            """INSERT INTO equipo_cmc (id_medilink, nombre, especialidad, rol, tipo_contrato, pct_honorario, telefono, email, estado, notas)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (b.get("id_medilink"), nombre, esp, (b.get("rol") or _rol_de(esp)),
             (b.get("tipo_contrato") or "honorarios"), int(b.get("pct_honorario") or 0),
             (b.get("telefono") or "").strip(), (b.get("email") or "").strip(),
             (b.get("estado") or "activo"), (b.get("notas") or "").strip()))
        conn.commit()
        return {"ok": True, "id": cur.lastrowid}


@router.patch("/{eid}")
async def editar(eid: int, request: Request, token: str | None = Query(None), cmc_session: str | None = Cookie(None)):
    require_admin(request, token=token, cmc_session=cmc_session)
    ensure_table()
    try:
        b = await request.json()
    except Exception:
        raise HTTPException(400, "Body JSON inválido")
    ok = {"nombre", "especialidad", "rol", "tipo_contrato", "pct_honorario", "telefono",
          "email", "estado", "licencia_desde", "licencia_hasta", "notas", "id_medilink"}
    sets, p = [], []
    for k, v in b.items():
        if k not in ok: continue
        if k == "pct_honorario": v = int(v or 0)
        if k == "estado" and v not in ESTADOS: continue
        sets.append(f"{k}=?"); p.append(v)
    if not sets:
        raise HTTPException(400, "Nada que actualizar")
    sets.append("updated_at=datetime('now')"); p.append(eid)
    from session import _conn
    with _conn() as conn:
        cur = conn.execute(f"UPDATE equipo_cmc SET {','.join(sets)} WHERE id=?", p)
        conn.commit()
    if cur.rowcount == 0:
        raise HTTPException(404, "No encontrado")
    return {"ok": True}


@router.delete("/{eid}")
async def eliminar(eid: int, token: str | None = Query(None), cmc_session: str | None = Cookie(None), request: Request = None):
    require_admin(request, token=token, cmc_session=cmc_session)
    ensure_table()
    from session import _conn
    with _conn() as conn:
        cur = conn.execute("DELETE FROM equipo_cmc WHERE id=?", (eid,))
        conn.commit()
    if cur.rowcount == 0:
        raise HTTPException(404, "No encontrado")
    return {"ok": True}
