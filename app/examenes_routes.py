"""
Router /alma/api/examenes — Órdenes de exámenes y resultados.

Seguimiento del ciclo de un examen (laboratorio, imagen, ecografía): solicitado →
tomado → resultado listo → entregado al paciente. Evita que resultados queden sin
entregar. Se enlaza naturalmente con la toma de muestras (convenio lab) y ecografía.

Tabla: examenes_cmc. Auth: alma_common.require_admin.
"""
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request, Query, Cookie, UploadFile, File, Form

from alma_common import require_admin

log = logging.getLogger("examenes_routes")
_CHILE_TZ = ZoneInfo("America/Santiago")
router = APIRouter(prefix="/alma/api/examenes", tags=["examenes"])

TIPOS = ["Laboratorio", "Imagen", "Ecografía", "Otro"]
ESTADOS = ["solicitado", "tomado", "resultado_listo", "entregado"]


def ensure_table() -> None:
    from session import db as _conn
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS examenes_cmc (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fecha_solicitud TEXT NOT NULL, paciente_nombre TEXT NOT NULL DEFAULT '',
                rut TEXT DEFAULT '', tipo TEXT DEFAULT 'Laboratorio', examen TEXT DEFAULT '',
                solicitante TEXT DEFAULT '', estado TEXT DEFAULT 'solicitado',
                fecha_resultado TEXT DEFAULT '', lab_externo TEXT DEFAULT '', notas TEXT DEFAULT '',
                creado_por TEXT DEFAULT 'recepcion', created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')))
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ex_estado ON examenes_cmc(estado)")
        conn.commit()


@router.get("/resumen")
async def resumen(token: str | None = Query(None), cmc_session: str | None = Cookie(None), request: Request = None):
    require_admin(request, token=token, cmc_session=cmc_session)
    ensure_table()
    from session import db as _conn
    with _conn() as conn:
        rows = [dict(r) for r in conn.execute("SELECT estado, tipo FROM examenes_cmc").fetchall()]
    pend = sum(1 for r in rows if r["estado"] != "entregado")
    listos = sum(1 for r in rows if r["estado"] == "resultado_listo")
    por_estado = {}
    for r in rows:
        por_estado[r["estado"]] = por_estado.get(r["estado"], 0) + 1
    return {"total": len(rows), "pendientes": pend, "resultados_sin_entregar": listos, "por_estado": por_estado}


@router.get("")
async def listar(estado: str | None = Query(None), tipo: str | None = Query(None),
                 token: str | None = Query(None), cmc_session: str | None = Cookie(None), request: Request = None):
    require_admin(request, token=token, cmc_session=cmc_session)
    ensure_table()
    sql = "SELECT * FROM examenes_cmc WHERE 1=1"; p = []
    if estado and estado != "todos": sql += " AND estado=?"; p.append(estado)
    if tipo and tipo != "Todos": sql += " AND tipo=?"; p.append(tipo)
    sql += " ORDER BY CASE estado WHEN 'resultado_listo' THEN 0 WHEN 'tomado' THEN 1 WHEN 'solicitado' THEN 2 ELSE 3 END, fecha_solicitud DESC, id DESC"
    from session import db as _conn
    with _conn() as conn:
        rows = [dict(r) for r in conn.execute(sql, p).fetchall()]
    return {"examenes": rows, "total": len(rows)}


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
    tipo = (b.get("tipo") or "Laboratorio").strip()
    if tipo not in TIPOS: tipo = "Laboratorio"
    fecha = (b.get("fecha_solicitud") or datetime.now(_CHILE_TZ).strftime("%Y-%m-%d")).strip()
    from session import db as _conn
    with _conn() as conn:
        cur = conn.execute(
            """INSERT INTO examenes_cmc (fecha_solicitud, paciente_nombre, rut, tipo, examen, solicitante, estado, lab_externo, notas, creado_por)
               VALUES (?,?,?,?,?,?, 'solicitado', ?, ?, ?)""",
            (fecha, nombre, (b.get("rut") or "").strip(), tipo, (b.get("examen") or "").strip(),
             (b.get("solicitante") or "").strip(), (b.get("lab_externo") or "").strip(),
             (b.get("notas") or "").strip(), (b.get("creado_por") or "recepcion").strip()))
        conn.commit()
        return {"ok": True, "id": cur.lastrowid}


@router.patch("/{ex_id}")
async def editar(ex_id: int, request: Request, token: str | None = Query(None), cmc_session: str | None = Cookie(None)):
    require_admin(request, token=token, cmc_session=cmc_session)
    ensure_table()
    try:
        b = await request.json()
    except Exception:
        raise HTTPException(400, "Body JSON inválido")
    ok = {"fecha_solicitud", "paciente_nombre", "rut", "tipo", "examen", "solicitante", "estado", "fecha_resultado", "lab_externo", "notas"}
    sets, p = [], []
    for k, v in b.items():
        if k not in ok: continue
        if k == "estado" and v not in ESTADOS: continue
        if k == "tipo" and v not in TIPOS: continue
        sets.append(f"{k}=?"); p.append(v)
    # al pasar a resultado_listo sin fecha, estampar hoy
    if b.get("estado") == "resultado_listo" and not b.get("fecha_resultado"):
        sets.append("fecha_resultado=?"); p.append(datetime.now(_CHILE_TZ).strftime("%Y-%m-%d"))
    if not sets:
        raise HTTPException(400, "Nada que actualizar")
    sets.append("updated_at=datetime('now')"); p.append(ex_id)
    from session import db as _conn
    with _conn() as conn:
        cur = conn.execute(f"UPDATE examenes_cmc SET {','.join(sets)} WHERE id=?", p)
        conn.commit()
    if cur.rowcount == 0:
        raise HTTPException(404, "No encontrado")
    return {"ok": True}


@router.delete("/{ex_id}")
async def eliminar(ex_id: int, token: str | None = Query(None), cmc_session: str | None = Cookie(None), request: Request = None):
    require_admin(request, token=token, cmc_session=cmc_session)
    ensure_table()
    from session import db as _conn
    with _conn() as conn:
        cur = conn.execute("DELETE FROM examenes_cmc WHERE id=?", (ex_id,))
        conn.commit()
    if cur.rowcount == 0:
        raise HTTPException(404, "No encontrado")
    return {"ok": True}


@router.post("/transcribir-foto")
async def transcribir_foto(
    request: Request,
    foto: UploadFile = File(...),
    modelo: str = Form("sonnet"),
    token: str | None = Query(None),
    cmc_session: str | None = Cookie(None),
):
    """Foto de un examen → texto clínico para pegar en la ficha de Medilink.
    Reusa el motor examenes_transcribe. modelo: 'sonnet' (default) o 'opus' (alta
    precisión). No persiste la imagen (dato sensible de salud, Ley 21.719)."""
    require_admin(request, token=token, cmc_session=cmc_session)
    data = await foto.read()
    if not data:
        raise HTTPException(400, "Archivo vacío")
    from examenes_transcribe import transcribir_examen
    texto = await transcribir_examen(data, foto.content_type, modelo)
    if not texto:
        raise HTTPException(502, "No se pudo transcribir el examen. Intenta una foto más nítida.")
    return {"texto": texto, "modelo": "opus" if (modelo or "").lower() == "opus" else "sonnet"}
