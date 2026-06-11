"""
Router /alma/api/esterilizacion — Trazabilidad de esterilización.

Registra cada ciclo de autoclave con sus controles (indicador químico y
biológico), carga y resultado. Es trazabilidad clínica real y soporte directo
para la habilitación SEREMI (el área de esterilización exige registro de ciclos
y controles biológicos).

Tabla self-contained: esterilizacion_ciclos. Auth: alma_common.require_admin.
"""
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request, Query, Cookie

from alma_common import require_admin

log = logging.getLogger("esterilizacion_routes")
_CHILE_TZ = ZoneInfo("America/Santiago")
router = APIRouter(prefix="/alma/api/esterilizacion", tags=["esterilizacion"])

RESULTADOS = ["aprobado", "rechazado", "en_proceso"]
INDICADORES = ["ok", "falla", "pendiente", "na"]


def ensure_table() -> None:
    from session import db as _conn
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS esterilizacion_ciclos (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                fecha               TEXT NOT NULL,
                hora                TEXT DEFAULT '',
                equipo              TEXT DEFAULT 'Autoclave 1',
                operador            TEXT DEFAULT '',
                programa            TEXT DEFAULT '',
                temperatura         TEXT DEFAULT '',
                duracion_min        INTEGER DEFAULT 0,
                lote                TEXT DEFAULT '',
                carga               TEXT DEFAULT '',
                ind_quimico         TEXT DEFAULT 'pendiente',
                ind_biologico       TEXT DEFAULT 'na',
                resultado           TEXT DEFAULT 'en_proceso',
                notas               TEXT DEFAULT '',
                creado_por          TEXT DEFAULT 'recepcion',
                created_at          TEXT DEFAULT (datetime('now')),
                updated_at          TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_est_fecha ON esterilizacion_ciclos(fecha)")
        conn.commit()


@router.get("/resumen")
async def resumen(token: str | None = Query(None), cmc_session: str | None = Cookie(None), request: Request = None):
    require_admin(request, token=token, cmc_session=cmc_session)
    ensure_table()
    from session import db as _conn
    mes = datetime.now(_CHILE_TZ).strftime("%Y-%m")
    with _conn() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT resultado, ind_biologico, ind_quimico FROM esterilizacion_ciclos WHERE substr(fecha,1,7)=?", (mes,)
        ).fetchall()]
        total_all = conn.execute("SELECT COUNT(*) c FROM esterilizacion_ciclos").fetchone()["c"]
    n = len(rows)
    aprob = sum(1 for r in rows if r["resultado"] == "aprobado")
    rech = sum(1 for r in rows if r["resultado"] == "rechazado")
    bio_pend = sum(1 for r in rows if r["ind_biologico"] == "pendiente")
    pct = round(aprob / n * 100) if n else 0
    return {"ciclos_mes": n, "aprobados_mes": aprob, "rechazados_mes": rech,
            "pct_aprobado": pct, "biologico_pendiente": bio_pend, "total_historico": total_all}


@router.get("")
async def listar(resultado: str | None = Query(None),
                 token: str | None = Query(None), cmc_session: str | None = Cookie(None), request: Request = None):
    require_admin(request, token=token, cmc_session=cmc_session)
    ensure_table()
    sql = "SELECT * FROM esterilizacion_ciclos WHERE 1=1"; p = []
    if resultado and resultado != "todos": sql += " AND resultado=?"; p.append(resultado)
    sql += " ORDER BY fecha DESC, id DESC LIMIT 300"
    from session import db as _conn
    with _conn() as conn:
        rows = [dict(r) for r in conn.execute(sql, p).fetchall()]
    return {"ciclos": rows, "total": len(rows)}


@router.post("")
async def crear(request: Request, token: str | None = Query(None), cmc_session: str | None = Cookie(None)):
    require_admin(request, token=token, cmc_session=cmc_session)
    ensure_table()
    try:
        b = await request.json()
    except Exception:
        raise HTTPException(400, "Body JSON inválido")
    now = datetime.now(_CHILE_TZ)
    fecha = (b.get("fecha") or now.strftime("%Y-%m-%d")).strip()
    hora = (b.get("hora") or now.strftime("%H:%M")).strip()
    resultado = (b.get("resultado") or "en_proceso").lower()
    if resultado not in RESULTADOS: resultado = "en_proceso"
    from session import db as _conn
    with _conn() as conn:
        cur = conn.execute(
            """INSERT INTO esterilizacion_ciclos
               (fecha, hora, equipo, operador, programa, temperatura, duracion_min, lote, carga,
                ind_quimico, ind_biologico, resultado, notas, creado_por)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (fecha, hora, (b.get("equipo") or "Autoclave 1").strip(), (b.get("operador") or "").strip(),
             (b.get("programa") or "").strip(), (b.get("temperatura") or "").strip(), int(b.get("duracion_min") or 0),
             (b.get("lote") or "").strip(), (b.get("carga") or "").strip(),
             (b.get("ind_quimico") or "pendiente"), (b.get("ind_biologico") or "na"),
             resultado, (b.get("notas") or "").strip(), (b.get("creado_por") or "recepcion").strip()))
        conn.commit()
        return {"ok": True, "id": cur.lastrowid}


@router.patch("/{ciclo_id}")
async def editar(ciclo_id: int, request: Request, token: str | None = Query(None), cmc_session: str | None = Cookie(None)):
    require_admin(request, token=token, cmc_session=cmc_session)
    ensure_table()
    try:
        b = await request.json()
    except Exception:
        raise HTTPException(400, "Body JSON inválido")
    ok = {"fecha", "hora", "equipo", "operador", "programa", "temperatura", "duracion_min",
          "lote", "carga", "ind_quimico", "ind_biologico", "resultado", "notas"}
    sets, p = [], []
    for k, v in b.items():
        if k not in ok: continue
        if k == "duracion_min": v = int(v or 0)
        if k == "resultado" and v not in RESULTADOS: continue
        sets.append(f"{k}=?"); p.append(v)
    if not sets:
        raise HTTPException(400, "Nada que actualizar")
    sets.append("updated_at=datetime('now')"); p.append(ciclo_id)
    from session import db as _conn
    with _conn() as conn:
        cur = conn.execute(f"UPDATE esterilizacion_ciclos SET {','.join(sets)} WHERE id=?", p)
        conn.commit()
    if cur.rowcount == 0:
        raise HTTPException(404, "No encontrado")
    return {"ok": True}


@router.delete("/{ciclo_id}")
async def eliminar(ciclo_id: int, token: str | None = Query(None), cmc_session: str | None = Cookie(None), request: Request = None):
    require_admin(request, token=token, cmc_session=cmc_session)
    ensure_table()
    from session import db as _conn
    with _conn() as conn:
        cur = conn.execute("DELETE FROM esterilizacion_ciclos WHERE id=?", (ciclo_id,))
        conn.commit()
    if cur.rowcount == 0:
        raise HTTPException(404, "No encontrado")
    return {"ok": True}
