"""
Router /alma/api/checklist — Sistema de checklists recurrentes de recepción.

Filosofía Gerber (E-Myth): el negocio corre por SISTEMAS documentados que un
ROL ejecuta de forma reiterada y que se MIDEN. Acá eso son tres cosas:

  1. items   → el sistema documentado (qué tarea, cómo se hace, con qué cadencia).
  2. marcas  → la ejecución (quién marcó qué, cuándo).
  3. cumplimiento → la medición (% hecho por día, quién, qué se saltó).

Truco de datos: cada marca guarda un `periodo` calculado según la cadencia
(fecha 'YYYY-MM-DD' para apertura/dia/cierre, 'YYYY-Www' para semanal,
'YYYY-MM' para mensual). "¿Está hecha hoy?" = "¿existe marca con el periodo
actual?". Las diarias se 'resetean' solas cada día sin ningún cron.

Tablas: checklist_items, checklist_marcas. Auth: alma_common.require_admin.
"""
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request, Query, Cookie

from alma_common import require_admin

log = logging.getLogger("checklist_routes")
_CHILE_TZ = ZoneInfo("America/Santiago")
router = APIRouter(prefix="/alma/api/checklist", tags=["checklist"])

# Cadencias en el orden en que se muestran. Las tres primeras son "del día".
CADENCIAS = ["apertura", "dia", "cierre", "semanal", "mensual"]
CADENCIAS_DIA = ["apertura", "dia", "cierre"]


def ensure_table() -> None:
    from session import db as _conn
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS checklist_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                titulo TEXT NOT NULL,
                detalle TEXT DEFAULT '',
                cadencia TEXT DEFAULT 'dia',
                orden INTEGER DEFAULT 0,
                activo INTEGER DEFAULT 1,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')))
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS checklist_marcas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id INTEGER NOT NULL,
                periodo TEXT NOT NULL,
                fecha TEXT DEFAULT '',
                hora TEXT DEFAULT '',
                por TEXT DEFAULT '',
                notas TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now')))
        """)
        # Una marca por item y periodo (idempotente: marcar 2 veces no duplica).
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_chk_item_periodo ON checklist_marcas(item_id, periodo)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_chk_periodo ON checklist_marcas(periodo)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_chk_item_cad ON checklist_items(cadencia, activo)")
        conn.commit()


def _periodo(cadencia: str, ahora: datetime | None = None) -> str:
    """Clave de periodo según la cadencia. Define cuándo una tarea 'se renueva'."""
    ahora = ahora or datetime.now(_CHILE_TZ)
    if cadencia == "semanal":
        y, w, _ = ahora.isocalendar()
        return f"{y}-W{w:02d}"
    if cadencia == "mensual":
        return ahora.strftime("%Y-%m")
    # apertura / dia / cierre → por día
    return ahora.strftime("%Y-%m-%d")


# Sistema inicial — el checklist Gerber pre-cargado para recepción del CMC.
# Se inserta solo si la tabla está vacía (primera vez). Editable luego en la app.
_SEED = [
    # cadencia, titulo, detalle
    ("apertura", "Encender equipos y abrir sistemas", "Computador, abrir Medilink y el panel /admin/v2."),
    ("apertura", "Revisar WhatsApp de la noche", "Inbox del panel: responder lo que llegó fuera de horario."),
    ("apertura", "Revisar agenda del día", "En Medilink: confirmar que no hay choques ni profesionales en licencia."),
    ("apertura", "Revisar pacientes marcados por el bot", "Reservas en vuelo / mensajes huérfanos que el bot dejó pendientes."),
    ("apertura", "Dejar fondo de caja", "Anotar el monto inicial de caja del día."),

    ("dia", "Registrar cada pago al momento de cobrar", "Módulo /alma/pagos: copago + método + folio Imed. No dejarlo 'para después'."),
    ("dia", "Preguntar '¿cómo nos conoció?' a pacientes nuevos", "Registrar en /alma/canal — así medimos de qué ad/canal viene."),
    ("dia", "Inbox de WhatsApp en cero", "Responder los mensajes del panel; meta: nada sin responder al cierre del bloque."),
    ("dia", "Confirmar datos del paciente al llegar", "Check-in / verificar ficha en Medilink."),
    ("dia", "Consentimiento de datos a pacientes nuevos", "Botón 'Consentir' en el panel — el paciente confirma por WhatsApp."),

    ("cierre", "Cuadrar caja", "Efectivo + Transbank vs lo registrado en /alma/pagos."),
    ("cierre", "Confirmar todos los pagos del día registrados", "Que no quede ninguna atención sin su pago cargado."),
    ("cierre", "Inbox de WhatsApp en cero (o dejar nota)", "Si queda algo pendiente, anotarlo para el turno siguiente."),
    ("cierre", "Revisar agenda de mañana", "Pacientes sin confirmar, profesionales que faltan."),
    ("cierre", "Anotar incidencias del día", "Problemas, reclamos, quiebres de stock."),

    ("semanal", "Revisar no-shows de la semana", "Cuántos faltaron y por qué."),
    ("semanal", "Conciliar Imed vs lo esperado", "El pago consolidado semanal contra la suma de bonificaciones."),
    ("semanal", "Revisar stock de insumos de recepción", "Pedir lo que falte antes de que se acabe."),
    ("semanal", "Limpiar lista de espera", "Pacientes que pidieron hora y no se concretó."),

    ("mensual", "Conciliación de caja del mes", "Cruce de las 6 fuentes de cobro (cierre mensual)."),
    ("mensual", "Revisar pacientes para reactivar", "Los que no vienen hace varios meses."),
    ("mensual", "Actualizar precios / profesionales", "Si hubo cambios en aranceles o equipo."),
]


def seed_if_empty() -> None:
    """Carga el checklist base la primera vez (tabla vacía)."""
    ensure_table()
    from session import db as _conn
    with _conn() as conn:
        n = conn.execute("SELECT COUNT(*) FROM checklist_items").fetchone()[0]
        if n:
            return
        for orden, (cad, titulo, detalle) in enumerate(_SEED):
            conn.execute(
                "INSERT INTO checklist_items (titulo, detalle, cadencia, orden) VALUES (?,?,?,?)",
                (titulo, detalle, cad, orden))
        conn.commit()
    log.info("checklist: sistema base sembrado (%d items)", len(_SEED))


def _items_activos(conn):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM checklist_items WHERE activo=1 ORDER BY orden, id").fetchall()]


@router.get("/hoy")
async def hoy(token: str | None = Query(None), cmc_session: str | None = Cookie(None), request: Request = None):
    """Vista operativa: items por cadencia con su estado de hoy (hecho/pendiente)."""
    require_admin(request, token=token, cmc_session=cmc_session)
    ensure_table()
    from session import db as _conn
    ahora = datetime.now(_CHILE_TZ)
    periodos = {cad: _periodo(cad, ahora) for cad in CADENCIAS}
    with _conn() as conn:
        items = _items_activos(conn)
        # Marcas vigentes (periodo actual de cada cadencia) en un solo barrido.
        marcas = {}
        for cad, per in periodos.items():
            for r in conn.execute(
                    "SELECT item_id, hora, por FROM checklist_marcas WHERE periodo=?", (per,)).fetchall():
                marcas[r["item_id"]] = {"hora": r["hora"], "por": r["por"]}

    grupos = {cad: {"items": [], "hechos": 0, "total": 0} for cad in CADENCIAS}
    for it in items:
        cad = it["cadencia"] if it["cadencia"] in CADENCIAS else "dia"
        m = marcas.get(it["id"])
        it["hecho"] = bool(m)
        it["hora"] = m["hora"] if m else ""
        it["por"] = m["por"] if m else ""
        grupos[cad]["items"].append(it)
        grupos[cad]["total"] += 1
        if m:
            grupos[cad]["hechos"] += 1

    dia_total = sum(grupos[c]["total"] for c in CADENCIAS_DIA)
    dia_hechos = sum(grupos[c]["hechos"] for c in CADENCIAS_DIA)
    return {
        "fecha": ahora.strftime("%Y-%m-%d"),
        "grupos": grupos,
        "dia": {"hechos": dia_hechos, "total": dia_total,
                "pct": round(100 * dia_hechos / dia_total) if dia_total else 0},
    }


@router.get("/resumen")
async def resumen(token: str | None = Query(None), cmc_session: str | None = Cookie(None), request: Request = None):
    """KPI compacto del día (para pills / Sala de Máquinas)."""
    require_admin(request, token=token, cmc_session=cmc_session)
    ensure_table()
    from session import db as _conn
    ahora = datetime.now(_CHILE_TZ)
    per_dia = _periodo("dia", ahora)
    with _conn() as conn:
        items = _items_activos(conn)
        dia_ids = [it["id"] for it in items if it["cadencia"] in CADENCIAS_DIA]
        hechos = 0
        if dia_ids:
            ph = ",".join("?" * len(dia_ids))
            hechos = conn.execute(
                f"SELECT COUNT(*) FROM checklist_marcas WHERE periodo=? AND item_id IN ({ph})",
                [per_dia, *dia_ids]).fetchone()[0]
    total = len(dia_ids)
    return {"hechos": hechos, "total": total, "pendientes": total - hechos,
            "pct": round(100 * hechos / total) if total else 0}


@router.post("/{item_id}/marcar")
async def marcar(item_id: int, request: Request, token: str | None = Query(None), cmc_session: str | None = Cookie(None)):
    require_admin(request, token=token, cmc_session=cmc_session)
    ensure_table()
    try:
        b = await request.json()
    except Exception:
        b = {}
    por = (b.get("por") or "").strip()[:60]
    notas = (b.get("notas") or "").strip()[:280]
    ahora = datetime.now(_CHILE_TZ)
    from session import db as _conn
    with _conn() as conn:
        row = conn.execute("SELECT cadencia FROM checklist_items WHERE id=? AND activo=1", (item_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Item no encontrado")
        per = _periodo(row["cadencia"], ahora)
        # Idempotente: si ya estaba marcado este periodo, actualiza quién/cuándo.
        conn.execute("""
            INSERT INTO checklist_marcas (item_id, periodo, fecha, hora, por, notas)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(item_id, periodo) DO UPDATE SET
                fecha=excluded.fecha, hora=excluded.hora, por=excluded.por, notas=excluded.notas
        """, (item_id, per, ahora.strftime("%Y-%m-%d"), ahora.strftime("%H:%M"), por, notas))
        conn.commit()
    return {"ok": True, "periodo": per, "hora": ahora.strftime("%H:%M"), "por": por}


@router.post("/{item_id}/desmarcar")
async def desmarcar(item_id: int, request: Request, token: str | None = Query(None), cmc_session: str | None = Cookie(None)):
    require_admin(request, token=token, cmc_session=cmc_session)
    ensure_table()
    ahora = datetime.now(_CHILE_TZ)
    from session import db as _conn
    with _conn() as conn:
        row = conn.execute("SELECT cadencia FROM checklist_items WHERE id=?", (item_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Item no encontrado")
        per = _periodo(row["cadencia"], ahora)
        conn.execute("DELETE FROM checklist_marcas WHERE item_id=? AND periodo=?", (item_id, per))
        conn.commit()
    return {"ok": True}


# ── Editor del sistema (plantillas) ──────────────────────────────────────────
@router.get("/items")
async def listar_items(token: str | None = Query(None), cmc_session: str | None = Cookie(None), request: Request = None):
    require_admin(request, token=token, cmc_session=cmc_session)
    ensure_table()
    from session import db as _conn
    with _conn() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM checklist_items WHERE activo=1 ORDER BY orden, id").fetchall()]
    return {"items": rows}


@router.post("/items")
async def crear_item(request: Request, token: str | None = Query(None), cmc_session: str | None = Cookie(None)):
    require_admin(request, token=token, cmc_session=cmc_session)
    ensure_table()
    try:
        b = await request.json()
    except Exception:
        raise HTTPException(400, "Body JSON inválido")
    titulo = (b.get("titulo") or "").strip()
    if not titulo:
        raise HTTPException(400, "titulo requerido")
    cad = (b.get("cadencia") or "dia").lower()
    if cad not in CADENCIAS:
        cad = "dia"
    from session import db as _conn
    with _conn() as conn:
        mx = conn.execute("SELECT COALESCE(MAX(orden),0)+1 FROM checklist_items WHERE cadencia=?", (cad,)).fetchone()[0]
        cur = conn.execute(
            "INSERT INTO checklist_items (titulo, detalle, cadencia, orden) VALUES (?,?,?,?)",
            (titulo, (b.get("detalle") or "").strip(), cad, mx))
        conn.commit()
    return {"ok": True, "id": cur.lastrowid}


@router.patch("/items/{item_id}")
async def editar_item(item_id: int, request: Request, token: str | None = Query(None), cmc_session: str | None = Cookie(None)):
    require_admin(request, token=token, cmc_session=cmc_session)
    ensure_table()
    try:
        b = await request.json()
    except Exception:
        raise HTTPException(400, "Body JSON inválido")
    ok = {"titulo", "detalle", "cadencia", "orden"}
    sets, p = [], []
    for k, v in b.items():
        if k not in ok:
            continue
        if k == "cadencia" and v not in CADENCIAS:
            continue
        sets.append(f"{k}=?")
        p.append(v)
    if not sets:
        raise HTTPException(400, "Nada que actualizar")
    sets.append("updated_at=datetime('now')")
    p.append(item_id)
    from session import db as _conn
    with _conn() as conn:
        cur = conn.execute(f"UPDATE checklist_items SET {','.join(sets)} WHERE id=?", p)
        conn.commit()
    if cur.rowcount == 0:
        raise HTTPException(404, "No encontrado")
    return {"ok": True}


@router.delete("/items/{item_id}")
async def borrar_item(item_id: int, token: str | None = Query(None), cmc_session: str | None = Cookie(None), request: Request = None):
    """Borrado suave (activo=0): preserva el historial de cumplimiento."""
    require_admin(request, token=token, cmc_session=cmc_session)
    ensure_table()
    from session import db as _conn
    with _conn() as conn:
        cur = conn.execute("UPDATE checklist_items SET activo=0, updated_at=datetime('now') WHERE id=?", (item_id,))
        conn.commit()
    if cur.rowcount == 0:
        raise HTTPException(404, "No encontrado")
    return {"ok": True}


# ── Cumplimiento (la medición Gerber) ────────────────────────────────────────
@router.get("/cumplimiento")
async def cumplimiento(dias: int = Query(14), token: str | None = Query(None),
                       cmc_session: str | None = Cookie(None), request: Request = None):
    """% de tareas del día completadas en los últimos N días + quién marcó.

    Nota: el denominador usa los items diarios ACTIVOS hoy (no se versiona el
    histórico de activación) — suficiente para supervisar adherencia.
    """
    require_admin(request, token=token, cmc_session=cmc_session)
    ensure_table()
    dias = max(1, min(int(dias or 14), 90))
    ahora = datetime.now(_CHILE_TZ)
    from session import db as _conn
    with _conn() as conn:
        items = _items_activos(conn)
        dia_ids = [it["id"] for it in items if it["cadencia"] in CADENCIAS_DIA]
        total = len(dia_ids)
        dias_out = []
        for d in range(dias):
            fecha = (ahora - timedelta(days=d)).strftime("%Y-%m-%d")
            if dia_ids:
                ph = ",".join("?" * len(dia_ids))
                rows = conn.execute(
                    f"SELECT item_id, por FROM checklist_marcas WHERE periodo=? AND item_id IN ({ph})",
                    [fecha, *dia_ids]).fetchall()
            else:
                rows = []
            hechos = len({r["item_id"] for r in rows})
            quienes = sorted({(r["por"] or "").strip() for r in rows if (r["por"] or "").strip()})
            dias_out.append({
                "fecha": fecha,
                "hechos": hechos, "total": total,
                "pct": round(100 * hechos / total) if total else 0,
                "por": quienes,
            })
    prom = round(sum(d["pct"] for d in dias_out) / len(dias_out)) if dias_out else 0
    return {"dias": dias_out, "total_items_dia": total, "promedio_pct": prom}
