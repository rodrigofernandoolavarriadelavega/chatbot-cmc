"""
Router /alma/api/documentos — Bóveda de documentos y cumplimiento clínico.

Consentimientos informados, protocolos clínicos (IAAS, REAS, aseo), contratos y
certificados, con control de vencimiento. Distinto de Habilitación (que es el
expediente SEREMI): acá viven los documentos vivos de la operación clínica.

Tabla self-contained: documentos_cmc (guarda metadatos + enlace/nota, no archivos
binarios). Auth: alma_common.require_admin.
"""
import logging
from datetime import datetime, date
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request, Query, Cookie

from alma_common import require_admin

log = logging.getLogger("documentos_routes")
_CHILE_TZ = ZoneInfo("America/Santiago")
router = APIRouter(prefix="/alma/api/documentos", tags=["documentos"])

CATEGORIAS = ["Consentimiento informado", "Protocolo clínico", "Contrato",
              "Certificado", "Manual", "Otro"]

SEED = [
    {"titulo": "Consentimiento informado — Procedimiento dental", "categoria": "Consentimiento informado", "responsable": "Odontología"},
    {"titulo": "Consentimiento informado — Estética facial", "categoria": "Consentimiento informado", "responsable": "Estética"},
    {"titulo": "Consentimiento informado — Endodoncia", "categoria": "Consentimiento informado", "responsable": "Endodoncia"},
    {"titulo": "Consentimiento informado — Implantología", "categoria": "Consentimiento informado", "responsable": "Implantología"},
    {"titulo": "Protocolo IAAS (prevención infecciones asociadas a atención)", "categoria": "Protocolo clínico", "responsable": "Dirección clínica"},
    {"titulo": "Protocolo REAS (residuos de establecimientos de atención de salud)", "categoria": "Protocolo clínico", "responsable": "Dirección clínica"},
    {"titulo": "Protocolo de aseo y desinfección de superficies", "categoria": "Protocolo clínico", "responsable": "Auxiliar de aseo"},
    {"titulo": "Protocolo de trazabilidad de esterilización", "categoria": "Protocolo clínico", "responsable": "Esterilización"},
]


def ensure_table() -> None:
    from session import _conn
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS documentos_cmc (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                titulo        TEXT NOT NULL,
                categoria     TEXT DEFAULT 'Otro',
                descripcion   TEXT DEFAULT '',
                url           TEXT DEFAULT '',
                responsable   TEXT DEFAULT '',
                vence         TEXT DEFAULT '',
                estado        TEXT DEFAULT 'vigente',
                created_at    TEXT DEFAULT (datetime('now')),
                updated_at    TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.commit()


def seed_if_empty() -> int:
    ensure_table()
    from session import _conn
    with _conn() as conn:
        n = conn.execute("SELECT COUNT(*) c FROM documentos_cmc").fetchone()["c"]
        if n > 0:
            return 0
        for d in SEED:
            conn.execute(
                "INSERT INTO documentos_cmc (titulo, categoria, responsable, estado) VALUES (?,?,?, 'vigente')",
                (d["titulo"], d["categoria"], d["responsable"]))
        conn.commit()
    return len(SEED)


def _estado_calc(vence: str, estado: str) -> str:
    if not vence:
        return estado or "vigente"
    try:
        v = datetime.strptime(vence[:10], "%Y-%m-%d").date()
    except ValueError:
        return estado or "vigente"
    hoy = datetime.now(_CHILE_TZ).date()
    if v < hoy:
        return "vencido"
    if (v - hoy).days <= 30:
        return "por_vencer"
    return "vigente"


@router.get("/resumen")
async def resumen(token: str | None = Query(None), cmc_session: str | None = Cookie(None), request: Request = None):
    require_admin(request, token=token, cmc_session=cmc_session)
    seed_if_empty()
    from session import _conn
    with _conn() as conn:
        rows = [dict(r) for r in conn.execute("SELECT vence, estado, categoria FROM documentos_cmc").fetchall()]
    por_venc = sum(1 for r in rows if _estado_calc(r["vence"], r["estado"]) == "por_vencer")
    vencidos = sum(1 for r in rows if _estado_calc(r["vence"], r["estado"]) == "vencido")
    por_cat: dict[str, int] = {}
    for r in rows:
        por_cat[r["categoria"]] = por_cat.get(r["categoria"], 0) + 1
    return {"total": len(rows), "por_vencer": por_venc, "vencidos": vencidos, "por_categoria": por_cat}


@router.get("")
async def listar(categoria: str | None = Query(None),
                 token: str | None = Query(None), cmc_session: str | None = Cookie(None), request: Request = None):
    require_admin(request, token=token, cmc_session=cmc_session)
    seed_if_empty()
    sql = "SELECT * FROM documentos_cmc WHERE 1=1"; p = []
    if categoria and categoria != "Todas": sql += " AND categoria=?"; p.append(categoria)
    sql += " ORDER BY categoria, titulo"
    from session import _conn
    with _conn() as conn:
        rows = [dict(r) for r in conn.execute(sql, p).fetchall()]
    for r in rows:
        r["estado_calc"] = _estado_calc(r["vence"], r["estado"])
    return {"documentos": rows, "total": len(rows)}


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
    cat = (b.get("categoria") or "Otro").strip()
    if cat not in CATEGORIAS: cat = "Otro"
    from session import _conn
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO documentos_cmc (titulo, categoria, descripcion, url, responsable, vence, estado) VALUES (?,?,?,?,?,?, 'vigente')",
            (titulo, cat, (b.get("descripcion") or "").strip(), (b.get("url") or "").strip(),
             (b.get("responsable") or "").strip(), (b.get("vence") or "").strip()))
        conn.commit()
        return {"ok": True, "id": cur.lastrowid}


@router.patch("/{doc_id}")
async def editar(doc_id: int, request: Request, token: str | None = Query(None), cmc_session: str | None = Cookie(None)):
    require_admin(request, token=token, cmc_session=cmc_session)
    ensure_table()
    try:
        b = await request.json()
    except Exception:
        raise HTTPException(400, "Body JSON inválido")
    ok = {"titulo", "categoria", "descripcion", "url", "responsable", "vence", "estado"}
    sets, p = [], []
    for k, v in b.items():
        if k not in ok: continue
        if k == "categoria" and v not in CATEGORIAS: v = "Otro"
        sets.append(f"{k}=?"); p.append(v)
    if not sets:
        raise HTTPException(400, "Nada que actualizar")
    sets.append("updated_at=datetime('now')"); p.append(doc_id)
    from session import _conn
    with _conn() as conn:
        cur = conn.execute(f"UPDATE documentos_cmc SET {','.join(sets)} WHERE id=?", p)
        conn.commit()
    if cur.rowcount == 0:
        raise HTTPException(404, "No encontrado")
    return {"ok": True}


@router.delete("/{doc_id}")
async def eliminar(doc_id: int, token: str | None = Query(None), cmc_session: str | None = Cookie(None), request: Request = None):
    require_admin(request, token=token, cmc_session=cmc_session)
    ensure_table()
    from session import _conn
    with _conn() as conn:
        cur = conn.execute("DELETE FROM documentos_cmc WHERE id=?", (doc_id,))
        conn.commit()
    if cur.rowcount == 0:
        raise HTTPException(404, "No encontrado")
    return {"ok": True}
