"""
Router /alma/api/abonos — Módulo de Abonos Alma.

Un ABONO es un pago ANTICIPADO PARCIAL que el paciente entrega ANTES de la
atención para asegurar el cupo. Se usa en prestaciones de alta inasistencia /
alto costo donde el centro pide garantía por adelantado:
    Psiquiatría · Fonoaudiología · Nutrición  (configurable abajo).

Diferencia con el módulo Pagos (tabla pagos_cmc):
    - Pago   = evento terminal, plata que entró a caja por una atención dada.
    - Abono  = saldo VIVO. Tiene ciclo de vida hasta que se resuelve:
          pendiente   → registrado, esperando la atención
          aplicado    → el paciente se atendió; el abono se descontó del total
                        y se cobró el SALDO (se crea un pago real en pagos_cmc)
          reembolsado → se devolvió el dinero al paciente
          trasladado  → se movió a una nueva cita (sigue como garantía)
          perdido     → no-show / cancelación tardía; el centro lo retiene

Política de monto (decisión del dueño 2026-06-07):
    - Monto FIJO sugerido por prestación, EDITABLE en cada registro.
    - El día de la atención el abono SE DESCUENTA del total → saldo = total − abono.
    - No-show: la RECEPCIÓN decide caso a caso (perder / reembolsar / trasladar).

Auth y DB: mismo patrón que pagos_routes (sessions.db, _require_admin_dep).
"""
import csv
import io
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Cookie, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

log = logging.getLogger("abonos_routes")
_CHILE_TZ = ZoneInfo("America/Santiago")

router = APIRouter(prefix="/alma/api/abonos", tags=["abonos"])

# ── Política de abonos ───────────────────────────────────────────────────────
# Prestaciones que requieren abono por adelantado.
#   abono_sugerido  = monto fijo que pide recepción (editable por registro).
#   precio_sugerido = valor total de referencia de la prestación (editable).
# El saldo del día = precio_total − monto_abono (se calcula, no se hardcodea).
# Para agregar/quitar una prestación o cambiar montos: editar este dict.
_ABONO_POLICY: dict[str, dict] = {
    "Psiquiatría": {
        "abono_sugerido":  20_000,
        "precio_sugerido": 45_000,
        "id_profesional":  None,   # aún sin profesional fijo en Medilink
    },
    "Fonoaudiología": {
        "abono_sugerido":  10_000,
        "precio_sugerido": 20_000,
        "id_profesional":  70,     # Juana Arratia
    },
    "Nutrición": {
        "abono_sugerido":  10_000,
        "precio_sugerido": 20_000,
        "id_profesional":  52,     # Gisela Pinto
    },
}

_ESTADOS = ("pendiente", "aplicado", "reembolsado", "trasladado", "perdido")
_METODOS = ("efectivo", "transferencia", "debito", "credito", "imed")


# ── Auth (idéntico a pagos_routes) ───────────────────────────────────────────

def _require_admin_dep(request: Request,
                       token: str | None = Query(None),
                       cmc_session: str | None = Cookie(None)) -> str:
    import hmac as _hmac
    from config import ADMIN_TOKEN
    from admin_routes import _verify_cookie

    auth_header = request.headers.get("authorization", "") if request else ""
    if auth_header.lower().startswith("bearer "):
        tk = auth_header.split(None, 1)[1].strip()
        if _hmac.compare_digest(tk, ADMIN_TOKEN):
            return tk
    if cmc_session:
        role = _verify_cookie(cmc_session)
        if role in ("admin", "ortodoncia"):
            return ADMIN_TOKEN
    if token and _hmac.compare_digest(token, ADMIN_TOKEN):
        return token
    raise HTTPException(status_code=401, detail="Token inválido")


# ── DDL ──────────────────────────────────────────────────────────────────────

def ensure_abonos_table() -> None:
    """Crea la tabla abonos_cmc si no existe. Idempotente."""
    from session import _conn
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS abonos_cmc (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                fecha            TEXT NOT NULL,           -- fecha de registro del abono
                hora             TEXT NOT NULL,
                paciente_nombre  TEXT NOT NULL DEFAULT '',
                rut              TEXT DEFAULT '',
                id_profesional   INTEGER,
                profesional      TEXT DEFAULT '',
                area             TEXT DEFAULT '',         -- prestación (Psiquiatría/Fono/Nutrición)
                procedimiento    TEXT DEFAULT '',
                fecha_cita       TEXT DEFAULT '',         -- cita futura que asegura
                precio_total     INTEGER DEFAULT 0,
                monto_abono      INTEGER DEFAULT 0,
                saldo            INTEGER DEFAULT 0,        -- precio_total - monto_abono
                metodo_pago      TEXT DEFAULT 'transferencia',
                folio            TEXT DEFAULT '',
                codigo_transferencia TEXT DEFAULT '',
                estado           TEXT DEFAULT 'pendiente',
                id_cita          TEXT DEFAULT '',
                pago_id          INTEGER,                 -- FK a pagos_cmc al aplicar
                nota             TEXT DEFAULT '',
                creado_por       TEXT DEFAULT 'recepcion',
                created_at       TEXT DEFAULT (datetime('now')),
                updated_at       TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_abonos_fecha  ON abonos_cmc(fecha)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_abonos_estado ON abonos_cmc(estado)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_abonos_rut    ON abonos_cmc(rut)")
        conn.commit()


def _now_cl() -> datetime:
    return datetime.now(_CHILE_TZ)


def _canon_prof(id_profesional, nombre: str) -> str:
    try:
        from prof_canon import canonical_name
        return canonical_name(id_profesional, nombre or "")
    except Exception:
        return (nombre or "").strip()


# ── Política (sugerencias para el formulario) ────────────────────────────────

@router.get("/policy")
async def get_policy(request: Request,
                     token: str | None = Query(None),
                     cmc_session: str | None = Cookie(None)):
    """Prestaciones con abono + montos sugeridos. Fuente única para el form."""
    _require_admin_dep(request, token=token, cmc_session=cmc_session)
    return {
        "prestaciones": [
            {
                "area": area,
                "abono_sugerido":  cfg["abono_sugerido"],
                "precio_sugerido": cfg["precio_sugerido"],
                "id_profesional":  cfg.get("id_profesional"),
            }
            for area, cfg in _ABONO_POLICY.items()
        ],
        "estados": list(_ESTADOS),
        "metodos": list(_METODOS),
    }


# ── Crear abono ──────────────────────────────────────────────────────────────

@router.post("")
async def post_abono(request: Request,
                     token: str | None = Query(None),
                     cmc_session: str | None = Cookie(None)):
    """Registra un abono anticipado. Todo pre-llenado es sugerencia editable."""
    _require_admin_dep(request, token=token, cmc_session=cmc_session)
    ensure_abonos_table()

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Body JSON inválido")

    paciente_nombre = (body.get("paciente_nombre") or "").strip()
    if not paciente_nombre:
        raise HTTPException(400, "paciente_nombre es requerido")

    now_cl = _now_cl()
    fecha = (body.get("fecha") or now_cl.strftime("%Y-%m-%d")).strip()
    hora  = (body.get("hora")  or now_cl.strftime("%H:%M")).strip()
    try:
        datetime.strptime(fecha, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(400, "fecha debe ser YYYY-MM-DD")

    metodo = (body.get("metodo_pago") or "transferencia").lower()
    if metodo not in _METODOS:
        metodo = "transferencia"

    estado = (body.get("estado") or "pendiente").lower()
    if estado not in _ESTADOS:
        estado = "pendiente"

    id_prof_raw = body.get("id_profesional")
    id_profesional = int(id_prof_raw) if id_prof_raw not in (None, "", "null") else None
    profesional = _canon_prof(id_profesional, body.get("profesional") or "")

    precio_total = int(body.get("precio_total") or 0)
    monto_abono  = int(body.get("monto_abono")  or 0)
    saldo = max(precio_total - monto_abono, 0)

    from session import _conn
    with _conn() as conn:
        cur = conn.execute(
            """INSERT INTO abonos_cmc
               (fecha, hora, paciente_nombre, rut, id_profesional, profesional,
                area, procedimiento, fecha_cita, precio_total, monto_abono, saldo,
                metodo_pago, folio, codigo_transferencia, estado, id_cita, nota,
                creado_por, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))""",
            (
                fecha, hora, paciente_nombre, (body.get("rut") or "").strip(),
                id_profesional, profesional,
                (body.get("area") or "").strip(),
                (body.get("procedimiento") or "").strip(),
                (body.get("fecha_cita") or "").strip(),
                precio_total, monto_abono, saldo,
                metodo, (body.get("folio") or "").strip(),
                (body.get("codigo_transferencia") or "").strip(),
                estado, (body.get("id_cita") or "").strip(),
                (body.get("nota") or "").strip(),
                (body.get("creado_por") or "recepcion").strip(),
            )
        )
        new_id = cur.lastrowid
        conn.commit()

    log.info("abonos.post id=%d paciente=%s area=%s abono=%d saldo=%d estado=%s",
             new_id, paciente_nombre, body.get("area", ""), monto_abono, saldo, estado)
    return {"ok": True, "id": new_id}


# ── Listar abonos ────────────────────────────────────────────────────────────

def _fetch_abonos(estado: str | None, fecha_desde: str | None,
                  fecha_hasta: str | None) -> list[dict]:
    from session import _conn
    where, params = [], []
    if estado and estado in _ESTADOS:
        where.append("estado = ?")
        params.append(estado)
    if fecha_desde and fecha_hasta:
        where.append("fecha BETWEEN ? AND ?")
        params.extend([fecha_desde, fecha_hasta])
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    with _conn() as conn:
        rows = conn.execute(
            f"""SELECT id, fecha, hora, paciente_nombre, rut, id_profesional,
                       profesional, area, procedimiento, fecha_cita,
                       precio_total, monto_abono, saldo, metodo_pago, folio,
                       codigo_transferencia, estado, id_cita, pago_id, nota,
                       creado_por, created_at, updated_at
                FROM abonos_cmc {clause}
                ORDER BY datetime(created_at) DESC, id DESC""",
            params
        ).fetchall()
    return [dict(r) for r in rows]


@router.get("")
async def get_abonos(request: Request,
                     estado: str | None = Query(None),
                     fecha_desde: str | None = Query(None),
                     fecha_hasta: str | None = Query(None),
                     token: str | None = Query(None),
                     cmc_session: str | None = Cookie(None)):
    """Lista abonos (más reciente primero). Filtros opcionales: estado, rango."""
    _require_admin_dep(request, token=token, cmc_session=cmc_session)
    ensure_abonos_table()
    abonos = _fetch_abonos(estado, fecha_desde, fecha_hasta)

    # Resumen por estado (para las píldoras del header)
    resumen = {e: {"n": 0, "monto": 0} for e in _ESTADOS}
    for a in abonos:
        e = a.get("estado") or "pendiente"
        if e in resumen:
            resumen[e]["n"] += 1
            resumen[e]["monto"] += a.get("monto_abono") or 0

    pend = [a for a in abonos if (a.get("estado") or "") == "pendiente"]
    return {
        "abonos": abonos,
        "resumen": resumen,
        "total_pendiente_monto": sum(a.get("monto_abono") or 0 for a in pend),
        "total_pendiente_saldo": sum(a.get("saldo") or 0 for a in pend),
        "n_pendientes": len(pend),
    }


# ── Editar abono ─────────────────────────────────────────────────────────────

@router.put("/{abono_id}")
async def put_abono(abono_id: int, request: Request,
                    token: str | None = Query(None),
                    cmc_session: str | None = Cookie(None)):
    """Edita campos de un abono. Recalcula saldo si cambia precio/abono."""
    _require_admin_dep(request, token=token, cmc_session=cmc_session)
    ensure_abonos_table()
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Body JSON inválido")

    from session import _conn
    with _conn() as conn:
        row = conn.execute("SELECT * FROM abonos_cmc WHERE id = ?", (abono_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Abono no encontrado")
        cur = dict(row)

        # Campos editables (texto/num)
        editable = ["paciente_nombre", "rut", "area", "procedimiento", "fecha_cita",
                    "folio", "codigo_transferencia", "id_cita", "nota", "fecha", "hora"]
        for f in editable:
            if f in body and body[f] is not None:
                cur[f] = str(body[f]).strip()

        if "metodo_pago" in body:
            m = (body["metodo_pago"] or "").lower()
            if m in _METODOS:
                cur["metodo_pago"] = m
        if "estado" in body:
            e = (body["estado"] or "").lower()
            if e in _ESTADOS:
                cur["estado"] = e
        if "id_profesional" in body:
            ipr = body["id_profesional"]
            cur["id_profesional"] = int(ipr) if ipr not in (None, "", "null") else None
            cur["profesional"] = _canon_prof(cur["id_profesional"],
                                             body.get("profesional") or cur.get("profesional") or "")
        if "precio_total" in body and body["precio_total"] is not None:
            cur["precio_total"] = int(body["precio_total"] or 0)
        if "monto_abono" in body and body["monto_abono"] is not None:
            cur["monto_abono"] = int(body["monto_abono"] or 0)
        cur["saldo"] = max(int(cur["precio_total"] or 0) - int(cur["monto_abono"] or 0), 0)

        conn.execute(
            """UPDATE abonos_cmc SET
                 fecha=?, hora=?, paciente_nombre=?, rut=?, id_profesional=?,
                 profesional=?, area=?, procedimiento=?, fecha_cita=?,
                 precio_total=?, monto_abono=?, saldo=?, metodo_pago=?, folio=?,
                 codigo_transferencia=?, estado=?, id_cita=?, nota=?,
                 updated_at=datetime('now')
               WHERE id=?""",
            (cur["fecha"], cur["hora"], cur["paciente_nombre"], cur["rut"],
             cur["id_profesional"], cur["profesional"], cur["area"],
             cur["procedimiento"], cur["fecha_cita"], cur["precio_total"],
             cur["monto_abono"], cur["saldo"], cur["metodo_pago"], cur["folio"],
             cur["codigo_transferencia"], cur["estado"], cur["id_cita"],
             cur["nota"], abono_id)
        )
        conn.commit()
    return {"ok": True, "id": abono_id, "saldo": cur["saldo"]}


# ── Aplicar abono (paciente se atendió) ──────────────────────────────────────

@router.post("/{abono_id}/aplicar")
async def aplicar_abono(abono_id: int, request: Request,
                        token: str | None = Query(None),
                        cmc_session: str | None = Cookie(None)):
    """
    Marca el abono como 'aplicado' (el paciente se atendió) y opcionalmente
    registra el cobro del SALDO como un pago real en pagos_cmc.

    Body opcional:
      crear_pago: bool (default True) — registra el saldo en el módulo Pagos.
      metodo_saldo: método con que se cobró el saldo el día (default = el del abono).
    """
    _require_admin_dep(request, token=token, cmc_session=cmc_session)
    ensure_abonos_table()
    try:
        body = await request.json()
    except Exception:
        body = {}

    from session import _conn
    with _conn() as conn:
        row = conn.execute("SELECT * FROM abonos_cmc WHERE id = ?", (abono_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Abono no encontrado")
        ab = dict(row)
        if ab["estado"] == "aplicado":
            raise HTTPException(409, "El abono ya fue aplicado")

        pago_id = ab.get("pago_id")
        crear_pago = body.get("crear_pago", True)
        saldo = int(ab.get("saldo") or 0)

        if crear_pago and saldo > 0 and not pago_id:
            metodo_saldo = (body.get("metodo_saldo") or ab.get("metodo_pago") or "efectivo").lower()
            if metodo_saldo == "imed":          # pagos_cmc no maneja 'imed' como método
                metodo_saldo = "transferencia"
            if metodo_saldo not in ("efectivo", "transferencia", "debito", "credito"):
                metodo_saldo = "efectivo"
            now_cl = _now_cl()
            try:
                from abonos_routes import ensure_abonos_table  # noqa (self ref guard)
            except Exception:
                pass
            # Inserta en pagos_cmc reutilizando su esquema (saldo = lo que entró hoy a caja)
            try:
                import pagos_routes  # garantiza tabla pagos_cmc
                pagos_routes.ensure_pagos_table()
            except Exception:
                pass
            cur = conn.execute(
                """INSERT INTO pagos_cmc
                   (fecha, hora, paciente_nombre, rut, id_profesional, profesional,
                    area, prevision, copago, bonificacion, metodo_pago, folio,
                    procedimiento, origen, id_cita, creado_por, canal, fuente,
                    created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))""",
                (
                    now_cl.strftime("%Y-%m-%d"), now_cl.strftime("%H:%M"),
                    ab["paciente_nombre"], ab.get("rut") or "",
                    ab.get("id_profesional"), ab.get("profesional") or "",
                    ab.get("area") or "", "particular",
                    saldo, 0, metodo_saldo, ab.get("folio") or "",
                    (ab.get("procedimiento") or "saldo abono"),
                    "presencial", ab.get("id_cita") or "",
                    "abono", "presencial", "",
                )
            )
            pago_id = cur.lastrowid

        conn.execute(
            "UPDATE abonos_cmc SET estado='aplicado', pago_id=?, updated_at=datetime('now') WHERE id=?",
            (pago_id, abono_id)
        )
        conn.commit()

    log.info("abonos.aplicar id=%d pago_id=%s saldo=%d", abono_id, pago_id, saldo)
    return {"ok": True, "id": abono_id, "pago_id": pago_id, "saldo_cobrado": saldo}


# ── Eliminar ─────────────────────────────────────────────────────────────────

@router.delete("/{abono_id}")
async def delete_abono(abono_id: int, request: Request,
                       token: str | None = Query(None),
                       cmc_session: str | None = Cookie(None)):
    _require_admin_dep(request, token=token, cmc_session=cmc_session)
    ensure_abonos_table()
    from session import _conn
    with _conn() as conn:
        conn.execute("DELETE FROM abonos_cmc WHERE id = ?", (abono_id,))
        conn.commit()
    return {"ok": True}


# ── Export CSV (Excel-friendly) ──────────────────────────────────────────────

@router.get("/export")
async def export_abonos(request: Request,
                        estado: str | None = Query(None),
                        fecha_desde: str | None = Query(None),
                        fecha_hasta: str | None = Query(None),
                        token: str | None = Query(None),
                        cmc_session: str | None = Cookie(None)):
    _require_admin_dep(request, token=token, cmc_session=cmc_session)
    ensure_abonos_table()
    abonos = _fetch_abonos(estado, fecha_desde, fecha_hasta)

    buf = io.StringIO()
    buf.write("﻿")  # BOM → Excel respeta UTF-8
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Fecha", "Hora", "Paciente", "RUT", "Profesional", "Prestación",
                "Procedimiento", "Fecha cita", "Precio total", "Abono", "Saldo",
                "Método", "Folio", "Estado", "Nota"])
    for a in abonos:
        w.writerow([
            a.get("fecha", ""), a.get("hora", ""), a.get("paciente_nombre", ""),
            a.get("rut", ""), a.get("profesional", ""), a.get("area", ""),
            a.get("procedimiento", ""), a.get("fecha_cita", ""),
            a.get("precio_total", 0), a.get("monto_abono", 0), a.get("saldo", 0),
            a.get("metodo_pago", ""), a.get("folio", ""), a.get("estado", ""),
            a.get("nota", ""),
        ])
    buf.seek(0)
    fname = f"abonos_cmc_{_now_cl().strftime('%Y%m%d')}.csv"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )
