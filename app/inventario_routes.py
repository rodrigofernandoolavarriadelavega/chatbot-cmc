"""
Router /alma/api/inventario — Módulo de Inventario Dental Alma.

Inventario específico del área dental del CMC (Odontología General, Endodoncia,
Ortodoncia, Implantología, Estética Facial). Catálogo sembrado con precios reales
de MayorDent (mayordent.cl, líder del mercado odontológico chileno) capturados el
2026-06-02. Los precios son referenciales (cambian con cyber/ofertas/IVA) y son
EDITABLES por la recepción — la idea es tener un costo de reposición de partida.

Modelo de datos (fuente de verdad = libro de movimientos):
  - inventario_dental       → catálogo + stock_actual (derivado, cacheado)
  - inventario_movimientos  → libro mayor (entrada / salida / ajuste / merma)

Stock actual se recalcula desde movimientos en cada movimiento, así nunca
se desincroniza. El catálogo guarda precio_mayordent (costo de reposición),
stock_minimo (umbral de alerta) y unidad (caja / jeringa / unidad / bolsa).

Auth: mismo patrón que pagos_routes / agenda_routes.
DB: sessions.db, misma que todo el stack.
"""
import csv
import io
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request, Query, Cookie
from fastapi.responses import JSONResponse, StreamingResponse

log = logging.getLogger("inventario_routes")
_CHILE_TZ = ZoneInfo("America/Santiago")

router = APIRouter(prefix="/alma/api/inventario", tags=["inventario"])

# ── Categorías del inventario dental ─────────────────────────────────────────
CATEGORIAS = [
    "Anestesia",
    "Desechables / EPP",
    "Restauración / Composites",
    "Cementos",
    "Material de impresión",
    "Endodoncia",
    "Fresas",
    "Blanqueamiento / Estética",
    "Ortodoncia",
    "Esterilización",
    "Otros",
]

# ── Catálogo semilla MayorDent (precios reales CLP, capturados 2026-06-02) ────
# Cada item: nombre, marca, categoria, precio_mayordent (CLP), unidad, stock_minimo.
# stock_minimo es una sugerencia conservadora para una clínica dental chica.
# El usuario ajusta todo desde la UI.
SEED_CATALOG: list[dict] = [
    # ── Anestesia ──
    {"nombre": "Anestesia Articaína 4% Caja 50 Uds", "marca": "DFL", "categoria": "Anestesia", "precio_mayordent": 43500, "unidad": "caja", "stock_minimo": 2},
    {"nombre": "Anestesia 2% Mepiadre 100", "marca": "DFL", "categoria": "Anestesia", "precio_mayordent": 33790, "unidad": "caja", "stock_minimo": 2},
    {"nombre": "Anestesia Alphacaína 2%", "marca": "Maver", "categoria": "Anestesia", "precio_mayordent": 29200, "unidad": "caja", "stock_minimo": 2},
    {"nombre": "Anestesia Intracare 2% tubo plástico", "marca": "Maver", "categoria": "Anestesia", "precio_mayordent": 22470, "unidad": "caja", "stock_minimo": 2},
    {"nombre": "Anestesia tópica Doldent (benzocaína 7,5%)", "marca": "Maver", "categoria": "Anestesia", "precio_mayordent": 3990, "unidad": "unidad", "stock_minimo": 2},

    # ── Desechables / EPP ──
    {"nombre": "Caja guantes Nitrilo Negro s/polvo x100", "marca": "Tresor", "categoria": "Desechables / EPP", "precio_mayordent": 3920, "unidad": "caja", "stock_minimo": 5},
    {"nombre": "Caja guantes Nitrilo Azul s/polvo x100", "marca": "Tresor", "categoria": "Desechables / EPP", "precio_mayordent": 4210, "unidad": "caja", "stock_minimo": 5},
    {"nombre": "Caja guantes Nitrilo Azul Cobalto s/polvo x100", "marca": "Tresor", "categoria": "Desechables / EPP", "precio_mayordent": 5980, "unidad": "caja", "stock_minimo": 3},
    {"nombre": "Mascarilla N95 modelo 9010", "marca": "3M", "categoria": "Desechables / EPP", "precio_mayordent": 1000, "unidad": "unidad", "stock_minimo": 10},
    {"nombre": "Servilletas / baberos x100 negro", "marca": "DCARE", "categoria": "Desechables / EPP", "precio_mayordent": 4800, "unidad": "paquete", "stock_minimo": 3},
    {"nombre": "Bolsa de eyectores x100", "marca": "ASA", "categoria": "Desechables / EPP", "precio_mayordent": 2980, "unidad": "bolsa", "stock_minimo": 4},
    {"nombre": "Algodón prensado hidrófilo x kilo", "marca": "Cranberry", "categoria": "Desechables / EPP", "precio_mayordent": 11500, "unidad": "kilo", "stock_minimo": 2},
    {"nombre": "Gasas 5x5 (25 x 200 uds)", "marca": "Dochem", "categoria": "Desechables / EPP", "precio_mayordent": 21250, "unidad": "caja", "stock_minimo": 1},
    {"nombre": "Goma Dique Flexidam Lila 6x6, 30 láminas", "marca": "Coltene", "categoria": "Desechables / EPP", "precio_mayordent": 43900, "unidad": "caja", "stock_minimo": 1},

    # ── Restauración / Composites ──
    {"nombre": "Resina Filtek Z350 XT Jeringa 4grs (tono a elección)", "marca": "3M", "categoria": "Restauración / Composites", "precio_mayordent": 57099, "unidad": "jeringa", "stock_minimo": 2},
    {"nombre": "Resina Filtek Z250 XT Jeringa 3grs (tono a elección)", "marca": "3M", "categoria": "Restauración / Composites", "precio_mayordent": 26632, "unidad": "jeringa", "stock_minimo": 2},
    {"nombre": "Resina Filtek One Bulk Fill Jeringa", "marca": "3M", "categoria": "Restauración / Composites", "precio_mayordent": 58805, "unidad": "jeringa", "stock_minimo": 1},
    {"nombre": "Kit Filtek Easy Match Universal Intro 4grs", "marca": "3M", "categoria": "Restauración / Composites", "precio_mayordent": 97500, "unidad": "kit", "stock_minimo": 1},
    {"nombre": "Ácido Fluorhídrico Jeringa 3ml", "marca": "Indusbello", "categoria": "Restauración / Composites", "precio_mayordent": 5500, "unidad": "jeringa", "stock_minimo": 1},

    # ── Cementos ──
    {"nombre": "Ketac Molar Easymix Kit (ionómero de vidrio)", "marca": "3M", "categoria": "Cementos", "precio_mayordent": 57924, "unidad": "kit", "stock_minimo": 1},
    {"nombre": "Ketac Cem Easymix 30grs/12ml", "marca": "3M", "categoria": "Cementos", "precio_mayordent": 69380, "unidad": "unidad", "stock_minimo": 1},
    {"nombre": "Ketac Cem Easymix 15grs/6ml", "marca": "3M", "categoria": "Cementos", "precio_mayordent": 49540, "unidad": "unidad", "stock_minimo": 1},
    {"nombre": "Vitremer Ionómero de Vidrio Kit Tono A3", "marca": "3M", "categoria": "Cementos", "precio_mayordent": 103420, "unidad": "kit", "stock_minimo": 1},
    {"nombre": "RelyX Temp NE Cemento Provisional sin eugenol", "marca": "3M", "categoria": "Cementos", "precio_mayordent": 36500, "unidad": "kit", "stock_minimo": 1},

    # ── Material de impresión ──
    {"nombre": "Alginato Chroma Print 454grs", "marca": "Coltene", "categoria": "Material de impresión", "precio_mayordent": 6250, "unidad": "bolsa", "stock_minimo": 3},
    {"nombre": "Alginato Jeltrate Plus 454grs", "marca": "Dentsply", "categoria": "Material de impresión", "precio_mayordent": 7950, "unidad": "bolsa", "stock_minimo": 2},
    {"nombre": "Speedex Medium silicona 140ml", "marca": "Coltene", "categoria": "Material de impresión", "precio_mayordent": 15990, "unidad": "unidad", "stock_minimo": 1},

    # ── Endodoncia ──
    {"nombre": "Limas H 25mm", "marca": "Kerr", "categoria": "Endodoncia", "precio_mayordent": 6790, "unidad": "caja", "stock_minimo": 2},
    {"nombre": "Conos Gutapercha N°10 punta de color (120 uds)", "marca": "Gapadent", "categoria": "Endodoncia", "precio_mayordent": 2740, "unidad": "caja", "stock_minimo": 2},
    {"nombre": "Conos de Papel surtidos", "marca": "Gapadent", "categoria": "Endodoncia", "precio_mayordent": 2720, "unidad": "caja", "stock_minimo": 2},
    {"nombre": "Conos Gutapercha WaveOne Gold", "marca": "Maillefer", "categoria": "Endodoncia", "precio_mayordent": 12644, "unidad": "caja", "stock_minimo": 1},
    {"nombre": "C+File Readysteel surtido 25mm x6", "marca": "Dentsply", "categoria": "Endodoncia", "precio_mayordent": 12250, "unidad": "caja", "stock_minimo": 1},
    {"nombre": "EDTA 18% solución 30ml", "marca": "Ultradent", "categoria": "Endodoncia", "precio_mayordent": 11850, "unidad": "unidad", "stock_minimo": 1},
    {"nombre": "Consepsis Clorhexidina 2% 30ml", "marca": "Ultradent", "categoria": "Endodoncia", "precio_mayordent": 22350, "unidad": "unidad", "stock_minimo": 1},

    # ── Fresas ──
    {"nombre": "Fresas diamante cilíndrica", "marca": "Jota", "categoria": "Fresas", "precio_mayordent": 2420, "unidad": "unidad", "stock_minimo": 5},
    {"nombre": "Fresas diamante llama", "marca": "Jota", "categoria": "Fresas", "precio_mayordent": 2307, "unidad": "unidad", "stock_minimo": 5},
    {"nombre": "Fresa diamante aguja corta", "marca": "Jota", "categoria": "Fresas", "precio_mayordent": 2420, "unidad": "unidad", "stock_minimo": 5},
    {"nombre": "Fresas diamante redondas tallo largo", "marca": "Jota Suiza", "categoria": "Fresas", "precio_mayordent": 2650, "unidad": "unidad", "stock_minimo": 5},

    # ── Blanqueamiento / Estética ──
    {"nombre": "Jeringa blanqueamiento Whiteness Perfect", "marca": "FGM", "categoria": "Blanqueamiento / Estética", "precio_mayordent": 13230, "unidad": "jeringa", "stock_minimo": 2},
    {"nombre": "Blanqueador Whiteness Perfect 22% Caja x4 jeringas", "marca": "FGM", "categoria": "Blanqueamiento / Estética", "precio_mayordent": 19200, "unidad": "caja", "stock_minimo": 1},

    # ── Ortodoncia ──
    {"nombre": "Transbond Plus adhesivo para bandas", "marca": "3M Unitek", "categoria": "Ortodoncia", "precio_mayordent": 168320, "unidad": "kit", "stock_minimo": 1},
]


# ── Auth (mismo patrón que pagos_routes) ─────────────────────────────────────

def _require_admin_dep(request: Request,
                       token: str | None = Query(None),
                       cmc_session: str | None = Cookie(None)) -> str:
    import hmac as _hmac
    from config import ADMIN_TOKEN
    from admin_routes import _verify_cookie, _is_admin_token

    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        tk = auth_header.split(None, 1)[1].strip()
        if _is_admin_token(tk):
            return tk
    if cmc_session:
        role = _verify_cookie(cmc_session)
        if role in ("admin", "ortodoncia"):
            return ADMIN_TOKEN
    if token and _is_admin_token(token):
        return token
    raise HTTPException(status_code=401, detail="Token inválido")


# ── DDL ──────────────────────────────────────────────────────────────────────

def ensure_inventario_tables() -> None:
    """Crea las tablas de inventario si no existen. Idempotente."""
    from session import db as _conn
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS inventario_dental (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                nombre           TEXT NOT NULL,
                marca            TEXT DEFAULT '',
                categoria        TEXT DEFAULT 'Otros',
                precio_mayordent INTEGER DEFAULT 0,
                unidad           TEXT DEFAULT 'unidad',
                stock_actual     INTEGER DEFAULT 0,
                stock_minimo     INTEGER DEFAULT 0,
                sku              TEXT DEFAULT '',
                proveedor        TEXT DEFAULT 'MayorDent',
                notas            TEXT DEFAULT '',
                activo           INTEGER DEFAULT 1,
                created_at       TEXT DEFAULT (datetime('now')),
                updated_at       TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS inventario_movimientos (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                producto_id  INTEGER NOT NULL,
                tipo         TEXT NOT NULL,          -- entrada | salida | ajuste | merma
                cantidad     INTEGER NOT NULL,        -- siempre positivo; el signo lo da el tipo
                stock_antes  INTEGER DEFAULT 0,
                stock_despues INTEGER DEFAULT 0,
                motivo       TEXT DEFAULT '',
                creado_por   TEXT DEFAULT 'recepcion',
                created_at   TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_inv_cat ON inventario_dental(categoria)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_inv_mov_prod ON inventario_movimientos(producto_id)")
        conn.commit()


def seed_if_empty() -> int:
    """Siembra el catálogo MayorDent si la tabla está vacía. Devuelve nº insertado."""
    ensure_inventario_tables()
    from session import db as _conn
    with _conn() as conn:
        n = conn.execute("SELECT COUNT(*) AS c FROM inventario_dental").fetchone()["c"]
        if n > 0:
            return 0
        for item in SEED_CATALOG:
            conn.execute(
                """INSERT INTO inventario_dental
                   (nombre, marca, categoria, precio_mayordent, unidad,
                    stock_actual, stock_minimo, proveedor)
                   VALUES (?,?,?,?,?,0,?, 'MayorDent')""",
                (item["nombre"], item["marca"], item["categoria"],
                 item["precio_mayordent"], item["unidad"], item["stock_minimo"])
            )
        conn.commit()
    log.info("inventario: catálogo MayorDent sembrado (%d productos)", len(SEED_CATALOG))
    return len(SEED_CATALOG)


def _estado_stock(actual: int, minimo: int) -> str:
    if actual <= 0:
        return "agotado"
    if actual <= minimo:
        return "bajo"
    return "ok"


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("")
async def listar(
    categoria: str | None = Query(None),
    q: str | None = Query(None, description="búsqueda por nombre/marca"),
    solo_bajo: bool = Query(False, description="solo items en stock bajo/agotado"),
    token: str | None = Query(None),
    cmc_session: str | None = Cookie(None),
    request: Request = None,
):
    """Lista productos con filtros opcionales por categoría / búsqueda / stock bajo."""
    _require_admin_dep(request, token=token, cmc_session=cmc_session)
    ensure_inventario_tables()

    sql = "SELECT * FROM inventario_dental WHERE activo = 1"
    params: list = []
    if categoria and categoria != "Todas":
        sql += " AND categoria = ?"
        params.append(categoria)
    if q:
        sql += " AND (LOWER(nombre) LIKE ? OR LOWER(marca) LIKE ?)"
        like = f"%{q.lower()}%"
        params += [like, like]
    sql += " ORDER BY categoria, nombre"

    from session import db as _conn
    with _conn() as conn:
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]

    for r in rows:
        r["estado"] = _estado_stock(r["stock_actual"], r["stock_minimo"])
        r["valor_stock"] = r["stock_actual"] * r["precio_mayordent"]

    if solo_bajo:
        rows = [r for r in rows if r["estado"] in ("bajo", "agotado")]

    return {"productos": rows, "total": len(rows), "categorias": CATEGORIAS}


@router.get("/resumen")
async def resumen(
    token: str | None = Query(None),
    cmc_session: str | None = Cookie(None),
    request: Request = None,
):
    """KPIs del inventario: valor total, items bajo mínimo, agotados, orden sugerida."""
    _require_admin_dep(request, token=token, cmc_session=cmc_session)
    ensure_inventario_tables()

    from session import db as _conn
    with _conn() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM inventario_dental WHERE activo = 1"
        ).fetchall()]

    valor_total = sum(r["stock_actual"] * r["precio_mayordent"] for r in rows)
    bajo = [r for r in rows if _estado_stock(r["stock_actual"], r["stock_minimo"]) == "bajo"]
    agotado = [r for r in rows if _estado_stock(r["stock_actual"], r["stock_minimo"]) == "agotado"]

    # Orden de compra sugerida: reponer hasta 2x el mínimo
    valor_reposicion = 0
    for r in (bajo + agotado):
        objetivo = max(r["stock_minimo"] * 2, 1)
        faltan = max(objetivo - r["stock_actual"], 0)
        valor_reposicion += faltan * r["precio_mayordent"]

    # Conteo por categoría
    por_categoria: dict[str, int] = {}
    for r in rows:
        por_categoria[r["categoria"]] = por_categoria.get(r["categoria"], 0) + 1

    return {
        "n_productos": len(rows),
        "valor_total": valor_total,
        "n_bajo": len(bajo),
        "n_agotado": len(agotado),
        "n_alertas": len(bajo) + len(agotado),
        "valor_reposicion": valor_reposicion,
        "por_categoria": por_categoria,
    }


@router.get("/orden-compra")
async def orden_compra(
    token: str | None = Query(None),
    cmc_session: str | None = Cookie(None),
    request: Request = None,
):
    """Genera la orden de compra sugerida (items bajo mínimo → reponer a 2x mínimo)."""
    _require_admin_dep(request, token=token, cmc_session=cmc_session)
    ensure_inventario_tables()

    from session import db as _conn
    with _conn() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM inventario_dental WHERE activo = 1 ORDER BY categoria, nombre"
        ).fetchall()]

    items = []
    for r in rows:
        estado = _estado_stock(r["stock_actual"], r["stock_minimo"])
        if estado not in ("bajo", "agotado"):
            continue
        objetivo = max(r["stock_minimo"] * 2, 1)
        cantidad = max(objetivo - r["stock_actual"], 1)
        subtotal = cantidad * r["precio_mayordent"]
        items.append({
            "id": r["id"],
            "nombre": r["nombre"],
            "marca": r["marca"],
            "categoria": r["categoria"],
            "unidad": r["unidad"],
            "stock_actual": r["stock_actual"],
            "stock_minimo": r["stock_minimo"],
            "estado": estado,
            "cantidad_sugerida": cantidad,
            "precio_mayordent": r["precio_mayordent"],
            "subtotal": subtotal,
            "proveedor": r["proveedor"],
        })

    total = sum(i["subtotal"] for i in items)
    return {"items": items, "total": total, "n_items": len(items), "proveedor": "MayorDent"}


@router.post("")
async def crear(
    request: Request,
    token: str | None = Query(None),
    cmc_session: str | None = Cookie(None),
):
    """Crea un producto nuevo."""
    _require_admin_dep(request, token=token, cmc_session=cmc_session)
    ensure_inventario_tables()
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Body JSON inválido")

    nombre = (body.get("nombre") or "").strip()
    if not nombre:
        raise HTTPException(400, "nombre es requerido")

    categoria = (body.get("categoria") or "Otros").strip()
    if categoria not in CATEGORIAS:
        categoria = "Otros"

    stock_inicial = int(body.get("stock_actual") or 0)

    from session import db as _conn
    with _conn() as conn:
        cur = conn.execute(
            """INSERT INTO inventario_dental
               (nombre, marca, categoria, precio_mayordent, unidad,
                stock_actual, stock_minimo, sku, proveedor, notas)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                nombre,
                (body.get("marca") or "").strip(),
                categoria,
                int(body.get("precio_mayordent") or 0),
                (body.get("unidad") or "unidad").strip(),
                stock_inicial,
                int(body.get("stock_minimo") or 0),
                (body.get("sku") or "").strip(),
                (body.get("proveedor") or "MayorDent").strip(),
                (body.get("notas") or "").strip(),
            )
        )
        new_id = cur.lastrowid
        # registrar el stock inicial como movimiento de entrada
        if stock_inicial > 0:
            conn.execute(
                """INSERT INTO inventario_movimientos
                   (producto_id, tipo, cantidad, stock_antes, stock_despues, motivo, creado_por)
                   VALUES (?, 'entrada', ?, 0, ?, 'stock inicial', ?)""",
                (new_id, stock_inicial, stock_inicial, (body.get("creado_por") or "recepcion"))
            )
        conn.commit()
    return {"ok": True, "id": new_id}


@router.patch("/{producto_id}")
async def editar(
    producto_id: int,
    request: Request,
    token: str | None = Query(None),
    cmc_session: str | None = Cookie(None),
):
    """Edita campos del catálogo (no toca stock — para eso usar /movimiento)."""
    _require_admin_dep(request, token=token, cmc_session=cmc_session)
    ensure_inventario_tables()
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Body JSON inválido")

    campos_ok = {"nombre", "marca", "categoria", "precio_mayordent",
                 "unidad", "stock_minimo", "sku", "proveedor", "notas"}
    sets, params = [], []
    for k, v in body.items():
        if k not in campos_ok:
            continue
        if k in ("precio_mayordent", "stock_minimo"):
            v = int(v or 0)
        elif k == "categoria" and v not in CATEGORIAS:
            v = "Otros"
        sets.append(f"{k} = ?")
        params.append(v)
    if not sets:
        raise HTTPException(400, "Nada que actualizar")
    sets.append("updated_at = datetime('now')")
    params.append(producto_id)

    from session import db as _conn
    with _conn() as conn:
        cur = conn.execute(
            f"UPDATE inventario_dental SET {', '.join(sets)} WHERE id = ?", params
        )
        conn.commit()
    if cur.rowcount == 0:
        raise HTTPException(404, "Producto no encontrado")
    return {"ok": True}


@router.post("/{producto_id}/movimiento")
async def movimiento(
    producto_id: int,
    request: Request,
    token: str | None = Query(None),
    cmc_session: str | None = Cookie(None),
):
    """Registra entrada / salida / ajuste / merma y recalcula stock_actual.

    Body: {tipo: entrada|salida|ajuste|merma, cantidad: int, motivo: str}
    - entrada: suma cantidad
    - salida / merma: resta cantidad (no baja de 0)
    - ajuste: fija el stock al valor 'cantidad' (conteo físico)
    """
    _require_admin_dep(request, token=token, cmc_session=cmc_session)
    ensure_inventario_tables()
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Body JSON inválido")

    tipo = (body.get("tipo") or "").lower()
    if tipo not in ("entrada", "salida", "ajuste", "merma"):
        raise HTTPException(400, "tipo debe ser entrada|salida|ajuste|merma")
    cantidad = int(body.get("cantidad") or 0)
    if cantidad < 0:
        raise HTTPException(400, "cantidad debe ser >= 0")

    from session import db as _conn
    with _conn() as conn:
        row = conn.execute(
            "SELECT stock_actual FROM inventario_dental WHERE id = ?", (producto_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "Producto no encontrado")
        antes = row["stock_actual"]

        if tipo == "entrada":
            despues = antes + cantidad
        elif tipo in ("salida", "merma"):
            despues = max(antes - cantidad, 0)
        else:  # ajuste = conteo físico, fija el valor
            despues = cantidad

        conn.execute(
            "UPDATE inventario_dental SET stock_actual = ?, updated_at = datetime('now') WHERE id = ?",
            (despues, producto_id)
        )
        conn.execute(
            """INSERT INTO inventario_movimientos
               (producto_id, tipo, cantidad, stock_antes, stock_despues, motivo, creado_por)
               VALUES (?,?,?,?,?,?,?)""",
            (producto_id, tipo, cantidad, antes, despues,
             (body.get("motivo") or "").strip(), (body.get("creado_por") or "recepcion"))
        )
        conn.commit()
    return {"ok": True, "stock_actual": despues, "stock_antes": antes}


@router.get("/{producto_id}/movimientos")
async def movimientos_producto(
    producto_id: int,
    token: str | None = Query(None),
    cmc_session: str | None = Cookie(None),
    request: Request = None,
):
    """Historial de movimientos de un producto (libro mayor)."""
    _require_admin_dep(request, token=token, cmc_session=cmc_session)
    ensure_inventario_tables()
    from session import db as _conn
    with _conn() as conn:
        rows = [dict(r) for r in conn.execute(
            """SELECT * FROM inventario_movimientos
               WHERE producto_id = ? ORDER BY created_at DESC, id DESC LIMIT 200""",
            (producto_id,)
        ).fetchall()]
    return {"movimientos": rows, "total": len(rows)}


@router.delete("/{producto_id}")
async def eliminar(
    producto_id: int,
    token: str | None = Query(None),
    cmc_session: str | None = Cookie(None),
    request: Request = None,
):
    """Soft-delete (activo = 0)."""
    _require_admin_dep(request, token=token, cmc_session=cmc_session)
    ensure_inventario_tables()
    from session import db as _conn
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE inventario_dental SET activo = 0, updated_at = datetime('now') WHERE id = ?",
            (producto_id,)
        )
        conn.commit()
    if cur.rowcount == 0:
        raise HTTPException(404, "Producto no encontrado")
    return {"ok": True}


@router.post("/seed")
async def seed_endpoint(
    token: str | None = Query(None),
    cmc_session: str | None = Cookie(None),
    request: Request = None,
):
    """Resiembra el catálogo MayorDent solo si la tabla está vacía."""
    _require_admin_dep(request, token=token, cmc_session=cmc_session)
    n = seed_if_empty()
    return {"ok": True, "insertados": n}


@router.get("/export")
async def export_csv(
    token: str | None = Query(None),
    cmc_session: str | None = Cookie(None),
    request: Request = None,
):
    """Exporta el inventario a CSV (Excel-friendly, separador ;)."""
    _require_admin_dep(request, token=token, cmc_session=cmc_session)
    ensure_inventario_tables()
    from session import db as _conn
    with _conn() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM inventario_dental WHERE activo = 1 ORDER BY categoria, nombre"
        ).fetchall()]

    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Categoría", "Producto", "Marca", "Unidad", "Stock actual",
                "Stock mínimo", "Estado", "Precio MayorDent (CLP)",
                "Valor en stock (CLP)", "Proveedor"])
    for r in rows:
        estado = _estado_stock(r["stock_actual"], r["stock_minimo"])
        w.writerow([
            r["categoria"], r["nombre"], r["marca"], r["unidad"],
            r["stock_actual"], r["stock_minimo"], estado,
            r["precio_mayordent"], r["stock_actual"] * r["precio_mayordent"],
            r["proveedor"],
        ])
    buf.seek(0)
    fecha = datetime.now(_CHILE_TZ).strftime("%Y-%m-%d")
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="inventario_dental_{fecha}.csv"'},
    )
