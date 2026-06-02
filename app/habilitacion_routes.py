"""
Router /alma/api/habilitacion — Gestor del expediente de habilitación sanitaria SEREMI.

Lleva a Alma el tracker que veníamos manejando en cmc_habilitacion_estado.md:
salas a habilitar, checklist documental por categoría, hitos del trámite y datos
clave del expediente (ampliación ante SEREMI Biobío). Permite marcar avances
(check/uncheck), agregar ítems y ver el % de avance.

Tablas self-contained: habilitacion_salas, habilitacion_items, habilitacion_meta.
Sembradas con el estado real al 2026-05-26. Auth: alma_common.require_admin.
"""
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request, Query, Cookie

from alma_common import require_admin

log = logging.getLogger("habilitacion_routes")
_CHILE_TZ = ZoneInfo("America/Santiago")
router = APIRouter(prefix="/alma/api/habilitacion", tags=["habilitacion"])

EST_SALA = ["pendiente", "en_revision", "aprobada"]
CATEGORIAS = ["Transversal", "Por sala", "Esterilización", "Toma de muestras", "Hitos", "Decisiones"]

# Estado real al 2026-05-26 (de cmc_habilitacion_estado.md)
SEED_META = {
    "seremi": "Biobío — Subdepartamento Profesiones Médicas (Concepción)",
    "tipo_tramite": "Ampliación / modificación de planta física (no autorización nueva)",
    "resolucion_vigente": "Res. 411/20 SEREMI de Salud (Func. Etapa I) — verificar modificatorias posteriores",
    "pyme_ley_20416": "Acreditado — arancel total $1.000 por trámite (vs ~$340.000)",
    "esterilizacion": "Central propia — autoclave ya registrado en Unidad Salud Ocupacional SEREMI",
    "inicio": "2026-05-26",
}

SEED_SALAS = [
    ("Ecografía", "pendiente", "por definir", "¿médico radiólogo, gineco o matrona acreditada eco-obstétrica?"),
    ("Matrona (gineco-obstétrica)", "pendiente", "matrona inscrita Supersalud", "regularizar PAP/DIU/eco/vacunación"),
    ("Esterilización", "en_revision", "enfermera/matrona responsable", "autoclave OK; faltan 3 áreas + operador DS 10/2012"),
    ("Toma de muestras", "pendiente", "TM/flebotomista (convenio lab)", "identificar laboratorio clínico autorizado"),
]

SEED_ITEMS = [
    # categoria, item, hecho, sala
    ("Transversal", "Resolución sanitaria vigente CMC (copia + número exacto)", 0, ""),
    ("Transversal", "Certificado recepción definitiva DOM (si hubo obra)", 0, ""),
    ("Transversal", "Certificados SEC: TE1 eléctrico + TC6 gas actualizados", 0, ""),
    ("Transversal", "Resolución REAS Unidad Gestión Ambiental SEREMI", 0, ""),
    ("Transversal", "Libros actas foliados (visitas + reclamos)", 0, ""),
    ("Transversal", "Plan evacuación firmado por experto prevención riesgos", 0, ""),
    ("Transversal", "Acreditación PYME (Ley 20.416)", 1, ""),
    ("Por sala", "Plano arquitectónico escala 1:50 firmado", 0, ""),
    ("Por sala", "Memoria descriptiva", 0, ""),
    ("Por sala", "Ficha técnica equipamiento", 0, ""),
    ("Por sala", "Manuales (IAAS, REAS, aseo) firmados y versionados", 0, ""),
    ("Por sala", "Inscripción Supersalud profesional responsable", 0, ""),
    ("Por sala", "Programa mantención preventiva equipos", 0, ""),
    ("Esterilización", "Número de Registro autoclave", 1, "Esterilización"),
    ("Esterilización", "Certificado Competencia Operador (DS 10/2012)", 0, "Esterilización"),
    ("Esterilización", "Validación inicial autoclave", 0, "Esterilización"),
    ("Esterilización", "Protocolo trazabilidad escrito", 0, "Esterilización"),
    ("Esterilización", "Registros indicadores químicos clase 5/6 + biológicos", 0, "Esterilización"),
    ("Esterilización", "Test Bowie-Dick diario (si autoclave prevacío)", 0, "Esterilización"),
    ("Esterilización", "Plano con 3 áreas separadas (sucia/limpia/estéril) flujo unidireccional", 0, "Esterilización"),
    ("Toma de muestras", "Convenio firmado/notariado con laboratorio clínico", 0, "Toma de muestras"),
    ("Toma de muestras", "Copia resolución sanitaria del laboratorio convenido", 0, "Toma de muestras"),
    ("Toma de muestras", "Nómina exámenes a tomar", 0, "Toma de muestras"),
    ("Toma de muestras", "Protocolo cadena custodia + cadena frío", 0, "Toma de muestras"),
    ("Decisiones", "Acreditar CMC como PYME (Ley 20.416)", 1, ""),
    ("Decisiones", "Esterilización: central propia vs convenio → propia", 1, ""),
    ("Decisiones", "Solicitar reunión técnica previa con SEREMI Biobío", 0, ""),
    ("Decisiones", "Identificar arquitecto firmante para planos 1:50", 0, ""),
    ("Decisiones", "Definir matrona/enfermera responsable de esterilización", 0, ""),
    ("Decisiones", "Identificar laboratorio clínico para convenio", 0, ""),
    ("Hitos", "Reunión técnica previa con SEREMI Biobío", 0, ""),
    ("Hitos", "Ingreso expediente en seremienlinea.minsal.cl", 0, ""),
    ("Hitos", "Pago arancel ($1.000 PYME)", 0, ""),
    ("Hitos", "Inspección SEREMI en terreno", 0, ""),
    ("Hitos", "Resolución de ampliación emitida", 0, ""),
]


def ensure_tables() -> None:
    from session import _conn
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS habilitacion_salas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sala TEXT NOT NULL, estado TEXT DEFAULT 'pendiente',
                responsable TEXT DEFAULT '', notas TEXT DEFAULT '',
                updated_at TEXT DEFAULT (datetime('now')))
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS habilitacion_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                categoria TEXT DEFAULT 'Transversal', item TEXT NOT NULL,
                hecho INTEGER DEFAULT 0, sala TEXT DEFAULT '', notas TEXT DEFAULT '',
                updated_at TEXT DEFAULT (datetime('now')))
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS habilitacion_meta (
                clave TEXT PRIMARY KEY, valor TEXT DEFAULT '')
        """)
        conn.commit()


def seed_if_empty() -> int:
    ensure_tables()
    from session import _conn
    with _conn() as conn:
        n = conn.execute("SELECT COUNT(*) c FROM habilitacion_items").fetchone()["c"]
        if n > 0:
            return 0
        for sala, estado, resp, notas in SEED_SALAS:
            conn.execute("INSERT INTO habilitacion_salas (sala, estado, responsable, notas) VALUES (?,?,?,?)",
                         (sala, estado, resp, notas))
        for cat, item, hecho, sala in SEED_ITEMS:
            conn.execute("INSERT INTO habilitacion_items (categoria, item, hecho, sala) VALUES (?,?,?,?)",
                         (cat, item, hecho, sala))
        for k, v in SEED_META.items():
            conn.execute("INSERT OR REPLACE INTO habilitacion_meta (clave, valor) VALUES (?,?)", (k, v))
        conn.commit()
    return len(SEED_ITEMS)


@router.get("/resumen")
async def resumen(token: str | None = Query(None), cmc_session: str | None = Cookie(None), request: Request = None):
    require_admin(request, token=token, cmc_session=cmc_session)
    seed_if_empty()
    from session import _conn
    with _conn() as conn:
        items = [dict(r) for r in conn.execute("SELECT categoria, hecho FROM habilitacion_items").fetchall()]
        salas = [dict(r) for r in conn.execute("SELECT estado FROM habilitacion_salas").fetchall()]
        meta = {r["clave"]: r["valor"] for r in conn.execute("SELECT clave, valor FROM habilitacion_meta").fetchall()}
    total = len(items); hechos = sum(1 for i in items if i["hecho"])
    pct = round(hechos / total * 100) if total else 0
    salas_aprob = sum(1 for s in salas if s["estado"] == "aprobada")
    # avance por categoría
    por_cat = {}
    for c in CATEGORIAS:
        sub = [i for i in items if i["categoria"] == c]
        if sub:
            por_cat[c] = {"hechos": sum(1 for i in sub if i["hecho"]), "total": len(sub)}
    return {"pct_avance": pct, "items_hechos": hechos, "items_total": total,
            "salas_total": len(salas), "salas_aprobadas": salas_aprob,
            "por_categoria": por_cat, "meta": meta}


@router.get("/salas")
async def get_salas(token: str | None = Query(None), cmc_session: str | None = Cookie(None), request: Request = None):
    require_admin(request, token=token, cmc_session=cmc_session)
    seed_if_empty()
    from session import _conn
    with _conn() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM habilitacion_salas ORDER BY id").fetchall()]
    return {"salas": rows}


@router.get("/items")
async def get_items(categoria: str | None = Query(None),
                    token: str | None = Query(None), cmc_session: str | None = Cookie(None), request: Request = None):
    require_admin(request, token=token, cmc_session=cmc_session)
    seed_if_empty()
    sql = "SELECT * FROM habilitacion_items WHERE 1=1"; p = []
    if categoria and categoria != "Todas": sql += " AND categoria=?"; p.append(categoria)
    sql += " ORDER BY CASE categoria WHEN 'Transversal' THEN 0 WHEN 'Por sala' THEN 1 WHEN 'Esterilización' THEN 2 WHEN 'Toma de muestras' THEN 3 WHEN 'Decisiones' THEN 4 ELSE 5 END, id"
    from session import _conn
    with _conn() as conn:
        rows = [dict(r) for r in conn.execute(sql, p).fetchall()]
    return {"items": rows, "categorias": CATEGORIAS}


@router.patch("/items/{item_id}")
async def patch_item(item_id: int, request: Request, token: str | None = Query(None), cmc_session: str | None = Cookie(None)):
    require_admin(request, token=token, cmc_session=cmc_session)
    ensure_tables()
    try:
        b = await request.json()
    except Exception:
        raise HTTPException(400, "Body JSON inválido")
    sets, p = [], []
    if "hecho" in b: sets.append("hecho=?"); p.append(1 if b["hecho"] else 0)
    if "notas" in b: sets.append("notas=?"); p.append((b["notas"] or "").strip())
    if "item" in b: sets.append("item=?"); p.append((b["item"] or "").strip())
    if not sets:
        raise HTTPException(400, "Nada que actualizar")
    sets.append("updated_at=datetime('now')"); p.append(item_id)
    from session import _conn
    with _conn() as conn:
        cur = conn.execute(f"UPDATE habilitacion_items SET {','.join(sets)} WHERE id=?", p)
        conn.commit()
    if cur.rowcount == 0:
        raise HTTPException(404, "No encontrado")
    return {"ok": True}


@router.post("/items")
async def add_item(request: Request, token: str | None = Query(None), cmc_session: str | None = Cookie(None)):
    require_admin(request, token=token, cmc_session=cmc_session)
    ensure_tables()
    try:
        b = await request.json()
    except Exception:
        raise HTTPException(400, "Body JSON inválido")
    item = (b.get("item") or "").strip()
    if not item:
        raise HTTPException(400, "item requerido")
    cat = (b.get("categoria") or "Transversal").strip()
    if cat not in CATEGORIAS: cat = "Transversal"
    from session import _conn
    with _conn() as conn:
        cur = conn.execute("INSERT INTO habilitacion_items (categoria, item, hecho, sala) VALUES (?,?,0,?)",
                           (cat, item, (b.get("sala") or "").strip()))
        conn.commit()
        return {"ok": True, "id": cur.lastrowid}


@router.delete("/items/{item_id}")
async def del_item(item_id: int, token: str | None = Query(None), cmc_session: str | None = Cookie(None), request: Request = None):
    require_admin(request, token=token, cmc_session=cmc_session)
    ensure_tables()
    from session import _conn
    with _conn() as conn:
        cur = conn.execute("DELETE FROM habilitacion_items WHERE id=?", (item_id,))
        conn.commit()
    if cur.rowcount == 0:
        raise HTTPException(404, "No encontrado")
    return {"ok": True}


@router.patch("/salas/{sala_id}")
async def patch_sala(sala_id: int, request: Request, token: str | None = Query(None), cmc_session: str | None = Cookie(None)):
    require_admin(request, token=token, cmc_session=cmc_session)
    ensure_tables()
    try:
        b = await request.json()
    except Exception:
        raise HTTPException(400, "Body JSON inválido")
    sets, p = [], []
    for k in ("estado", "responsable", "notas", "sala"):
        if k in b:
            v = b[k]
            if k == "estado" and v not in EST_SALA: continue
            sets.append(f"{k}=?"); p.append(v)
    if not sets:
        raise HTTPException(400, "Nada que actualizar")
    sets.append("updated_at=datetime('now')"); p.append(sala_id)
    from session import _conn
    with _conn() as conn:
        cur = conn.execute(f"UPDATE habilitacion_salas SET {','.join(sets)} WHERE id=?", p)
        conn.commit()
    if cur.rowcount == 0:
        raise HTTPException(404, "No encontrada")
    return {"ok": True}
