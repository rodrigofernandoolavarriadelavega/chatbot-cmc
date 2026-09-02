# -*- coding: utf-8 -*-
"""Router /alma/api/cargos — Cargos a descontar al profesional.

POR QUE EXISTE
--------------
Pedido de Javiera Burgos (odontologia) el 2026-09-01: *"lo que podria servir
seria una plataforma para ir anotando lo de las radiografias y asi dps hacer el
descuento al profesional, y lo mismo con el laboratorio, y asi ir anotando si ya
esta pagado. O sea yo lo voy anotando pero para mi nomas... el mes pasado me
falto descontar unas cubetas"*.

El agujero es real y cuesta DOS veces:
  1. El CMC ya paga esos insumos — `egresos_cmc` trae "Insumos clinicos y
     dentales $400.000" como gasto fijo recurrente.
  2. Si nadie descuenta, ademas se le paga el honorario completo sobre esa
     produccion. Se pierde por los dos lados y, al ser una linea plana mensual,
     no aparece por profesional en ningun reporte.

COMO SE ENCHUFA (esto es lo que lo hace distinto del cuaderno)
--------------------------------------------------------------
`liquidaciones` ya tiene un campo "Ajuste" suelto por profesional. Este modulo
produce el DETALLE que explica ese ajuste: `GET /resumen-liquidacion` devuelve,
por profesional y mes, la suma de cargos pendientes lista para descontar. Si el
detalle vive aparte y hay que ir a mirarlo, es el cuaderno de Javiera con otra
cara y se vuelve a olvidar.

Ademas importa las radiografias desde `vales_convenio` (el modulo Imagendent ya
guarda paciente, profesional y costo de cada vale) para no tipearlas dos veces.

Tablas: cargo_catalogo (que se cobra y a cuanto) + cargo_registro (cada cargo).
El catalogo se siembra desde `inventario_dental` (41 insumos con precio
MayorDent) la primera vez, mas las 3 familias que pidio Javiera.
Auth: require_ortodoncia (Javiera entra con su token; admin tambien).
"""
from __future__ import annotations

import csv
import io
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query, Request, Cookie
from fastapi.responses import StreamingResponse

from session import db, log_event

log = logging.getLogger("cargos_routes")
_CHILE_TZ = ZoneInfo("America/Santiago")
router = APIRouter(prefix="/alma/api/cargos", tags=["cargos"])

# Las tres familias que nombro Javiera. `origen` marca de donde puede llegar el
# cargo solo: los vales de Imagendent ya traen las radiografias con profesional.
CATEGORIAS = ["Radiografia", "Laboratorio", "Insumo", "Otro"]


def _hoy() -> str:
    return datetime.now(_CHILE_TZ).strftime("%Y-%m-%d")


def _ahora() -> str:
    return datetime.now(_CHILE_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _mes_actual() -> str:
    return datetime.now(_CHILE_TZ).strftime("%Y-%m")


def _crear_tablas() -> None:
    with db() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS cargo_catalogo (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                nombre     TEXT NOT NULL,
                categoria  TEXT NOT NULL DEFAULT 'Otro',
                precio     INTEGER NOT NULL DEFAULT 0,
                unidad     TEXT DEFAULT 'unidad',
                activo     INTEGER NOT NULL DEFAULT 1,
                notas      TEXT,
                created_at TEXT,
                updated_at TEXT
            )""")
        c.execute("""
            CREATE TABLE IF NOT EXISTS cargo_registro (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                fecha         TEXT NOT NULL,
                mes           TEXT NOT NULL,
                profesional   TEXT NOT NULL,
                id_profesional INTEGER,
                catalogo_id   INTEGER,
                descripcion   TEXT NOT NULL,
                categoria     TEXT NOT NULL DEFAULT 'Otro',
                cantidad      REAL NOT NULL DEFAULT 1,
                precio_unit   INTEGER NOT NULL DEFAULT 0,
                total         INTEGER NOT NULL DEFAULT 0,
                paciente      TEXT,
                origen        TEXT NOT NULL DEFAULT 'manual',
                origen_ref    TEXT,
                pagado        INTEGER NOT NULL DEFAULT 0,
                pagado_at     TEXT,
                notas         TEXT,
                creado_por    TEXT,
                created_at    TEXT,
                updated_at    TEXT
            )""")
        c.execute("CREATE INDEX IF NOT EXISTS ix_cargo_mes ON cargo_registro(mes, profesional)")
        c.execute("CREATE INDEX IF NOT EXISTS ix_cargo_pagado ON cargo_registro(pagado, mes)")
        # Un vale de Imagendent no puede entrar dos veces aunque se re-importe.
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_cargo_origen "
                  "ON cargo_registro(origen, origen_ref) WHERE origen_ref IS NOT NULL")
        c.commit()


def _sembrar_catalogo() -> None:
    """Siembra una sola vez: 3 placeholders + los insumos de inventario_dental.

    Los precios de radiografia/laboratorio/cubeta quedan en 0 A PROPOSITO: solo
    Javiera sabe a cuanto se cobran. Un precio inventado aca se convierte en un
    descuento equivocado en la liquidacion de alguien.
    """
    with db() as c:
        n = list(c.execute("SELECT COUNT(*) FROM cargo_catalogo"))[0][0]
        if n:
            return
        base = [
            ("Radiografia (definir precio)", "Radiografia", 0, "unidad"),
            ("Trabajo de laboratorio (definir precio)", "Laboratorio", 0, "trabajo"),
            ("Cubeta (definir precio)", "Insumo", 0, "unidad"),
        ]
        for nombre, cat, precio, unidad in base:
            c.execute("INSERT INTO cargo_catalogo(nombre,categoria,precio,unidad,activo,created_at,updated_at)"
                      " VALUES (?,?,?,?,1,?,?)", (nombre, cat, precio, unidad, _ahora(), _ahora()))
        try:
            filas = list(c.execute(
                "SELECT nombre, precio_mayordent, unidad FROM inventario_dental "
                "WHERE COALESCE(activo,1)=1 ORDER BY nombre"))
            for nombre, precio, unidad in filas:
                c.execute("INSERT INTO cargo_catalogo(nombre,categoria,precio,unidad,activo,notas,created_at,updated_at)"
                          " VALUES (?,?,?,?,1,?,?,?)",
                          (nombre, "Insumo", int(precio or 0), unidad or "unidad",
                           "precio MayorDent — confirmar cuanto se cobra", _ahora(), _ahora()))
            log.info("cargo_catalogo sembrado con %d insumos de inventario_dental", len(filas))
        except Exception as e:  # inventario_dental puede no existir en un entorno nuevo
            log.warning("no se pudo sembrar desde inventario_dental: %s", e)
        c.commit()


_crear_tablas()
_sembrar_catalogo()


def _auth(request: Request, token: str | None, cmc_session: str | None) -> str:
    from admin_routes import _verify_cookie, _is_admin_token
    from config import ADMIN_TOKEN, ORTODONCIA_TOKEN

    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        tk = auth_header.split(None, 1)[1].strip()
        if _is_admin_token(tk) or (ORTODONCIA_TOKEN and tk == ORTODONCIA_TOKEN):
            return tk
    if cmc_session:
        role = _verify_cookie(cmc_session)
        if role in ("admin", "ortodoncia", "administracion"):
            return ADMIN_TOKEN
    if token and (_is_admin_token(token) or (ORTODONCIA_TOKEN and token == ORTODONCIA_TOKEN)):
        return token
    raise HTTPException(status_code=401, detail="Token invalido")


def _fila(r) -> dict:
    return {
        "id": r[0], "fecha": r[1], "mes": r[2], "profesional": r[3],
        "descripcion": r[4], "categoria": r[5], "cantidad": r[6],
        "precio_unit": r[7], "total": r[8], "paciente": r[9],
        "origen": r[10], "pagado": bool(r[11]), "pagado_at": r[12], "notas": r[13],
    }


_SEL = ("SELECT id,fecha,mes,profesional,descripcion,categoria,cantidad,"
        "precio_unit,total,paciente,origen,pagado,pagado_at,notas FROM cargo_registro")


@router.get("/catalogo")
def catalogo(request: Request, token: str | None = Query(None),
             cmc_session: str | None = Cookie(None)):
    _auth(request, token, cmc_session)
    with db() as c:
        filas = list(c.execute(
            "SELECT id,nombre,categoria,precio,unidad,activo,notas FROM cargo_catalogo "
            "ORDER BY activo DESC, categoria, nombre"))
    items = [{"id": r[0], "nombre": r[1], "categoria": r[2], "precio": r[3],
              "unidad": r[4], "activo": bool(r[5]), "notas": r[6]} for r in filas]
    return {"items": items, "categorias": CATEGORIAS,
            "sin_precio": sum(1 for i in items if i["activo"] and not i["precio"])}


@router.post("/catalogo")
async def catalogo_upsert(request: Request, token: str | None = Query(None),
                          cmc_session: str | None = Cookie(None)):
    _auth(request, token, cmc_session)
    b = await request.json()
    nombre = (b.get("nombre") or "").strip()
    if not nombre:
        raise HTTPException(400, "Falta el nombre")
    cat = b.get("categoria") if b.get("categoria") in CATEGORIAS else "Otro"
    precio = max(0, int(b.get("precio") or 0))
    unidad = (b.get("unidad") or "unidad").strip()
    activo = 1 if b.get("activo", True) else 0
    cid = b.get("id")
    with db() as c:
        if cid:
            c.execute("UPDATE cargo_catalogo SET nombre=?,categoria=?,precio=?,unidad=?,"
                      "activo=?,notas=?,updated_at=? WHERE id=?",
                      (nombre, cat, precio, unidad, activo, b.get("notas"), _ahora(), int(cid)))
        else:
            c.execute("INSERT INTO cargo_catalogo(nombre,categoria,precio,unidad,activo,notas,"
                      "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                      (nombre, cat, precio, unidad, activo, b.get("notas"), _ahora(), _ahora()))
            cid = list(c.execute("SELECT last_insert_rowid()"))[0][0]
        c.commit()
    return {"ok": True, "id": cid}


@router.get("")
def listar(request: Request, mes: str | None = Query(None),
           profesional: str | None = Query(None),
           estado: str | None = Query(None),
           token: str | None = Query(None), cmc_session: str | None = Cookie(None)):
    _auth(request, token, cmc_session)
    mes = mes or _mes_actual()
    sql, args = _SEL + " WHERE mes=?", [mes]
    if profesional:
        sql += " AND profesional=?"; args.append(profesional)
    if estado == "pendiente":
        sql += " AND pagado=0"
    elif estado == "pagado":
        sql += " AND pagado=1"
    sql += " ORDER BY fecha DESC, id DESC"
    with db() as c:
        filas = [_fila(r) for r in c.execute(sql, args)]
        profs = [r[0] for r in c.execute(
            "SELECT DISTINCT nombre FROM equipo_cmc WHERE COALESCE(estado,'activo')='activo' ORDER BY nombre")]
        meses = [r[0] for r in c.execute(
            "SELECT DISTINCT mes FROM cargo_registro ORDER BY mes DESC LIMIT 24")]
    if mes not in meses:
        meses.insert(0, mes)
    pend = sum(f["total"] for f in filas if not f["pagado"])
    return {"mes": mes, "items": filas, "profesionales": profs, "meses": meses,
            "total": sum(f["total"] for f in filas), "pendiente": pend,
            "pagado": sum(f["total"] for f in filas if f["pagado"])}


@router.post("")
async def crear(request: Request, token: str | None = Query(None),
                cmc_session: str | None = Cookie(None)):
    _auth(request, token, cmc_session)
    b = await request.json()
    prof = (b.get("profesional") or "").strip()
    desc = (b.get("descripcion") or "").strip()
    if not prof:
        raise HTTPException(400, "Falta el profesional")
    if not desc:
        raise HTTPException(400, "Falta que se cobra")
    fecha = (b.get("fecha") or _hoy())[:10]
    cant = float(b.get("cantidad") or 1)
    precio = int(b.get("precio_unit") or 0)
    cat = b.get("categoria") if b.get("categoria") in CATEGORIAS else "Otro"
    total = int(round(cant * precio))
    with db() as c:
        c.execute("INSERT INTO cargo_registro(fecha,mes,profesional,catalogo_id,descripcion,"
                  "categoria,cantidad,precio_unit,total,paciente,origen,pagado,notas,"
                  "creado_por,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,'manual',0,?,?,?,?)",
                  (fecha, fecha[:7], prof, b.get("catalogo_id"), desc, cat, cant, precio,
                   total, (b.get("paciente") or "").strip() or None, b.get("notas"),
                   b.get("creado_por") or "recepcion", _ahora(), _ahora()))
        cid = list(c.execute("SELECT last_insert_rowid()"))[0][0]
        c.commit()
    log_event(None, "cargo_creado", {"prof": prof, "total": total, "desc": desc})
    return {"ok": True, "id": cid, "total": total}


@router.patch("/{cargo_id}")
async def editar(cargo_id: int, request: Request, token: str | None = Query(None),
                 cmc_session: str | None = Cookie(None)):
    _auth(request, token, cmc_session)
    b = await request.json()
    with db() as c:
        row = list(c.execute("SELECT pagado FROM cargo_registro WHERE id=?", (cargo_id,)))
        if not row:
            raise HTTPException(404, "No existe ese cargo")
        if "pagado" in b:
            pg = 1 if b["pagado"] else 0
            c.execute("UPDATE cargo_registro SET pagado=?,pagado_at=?,updated_at=? WHERE id=?",
                      (pg, _ahora() if pg else None, _ahora(), cargo_id))
        for campo in ("descripcion", "paciente", "notas", "profesional", "fecha"):
            if campo in b:
                c.execute(f"UPDATE cargo_registro SET {campo}=?,updated_at=? WHERE id=?",
                          (b[campo], _ahora(), cargo_id))
        if "fecha" in b:
            c.execute("UPDATE cargo_registro SET mes=substr(fecha,1,7) WHERE id=?", (cargo_id,))
        if "cantidad" in b or "precio_unit" in b:
            cur = list(c.execute("SELECT cantidad,precio_unit FROM cargo_registro WHERE id=?",
                                 (cargo_id,)))[0]
            cant = float(b.get("cantidad", cur[0]))
            precio = int(b.get("precio_unit", cur[1]))
            c.execute("UPDATE cargo_registro SET cantidad=?,precio_unit=?,total=?,updated_at=? WHERE id=?",
                      (cant, precio, int(round(cant * precio)), _ahora(), cargo_id))
        c.commit()
    return {"ok": True}


@router.delete("/{cargo_id}")
def borrar(cargo_id: int, request: Request, token: str | None = Query(None),
           cmc_session: str | None = Cookie(None)):
    _auth(request, token, cmc_session)
    with db() as c:
        c.execute("DELETE FROM cargo_registro WHERE id=?", (cargo_id,))
        c.commit()
    return {"ok": True}


@router.post("/importar-vales")
def importar_vales(request: Request, mes: str | None = Query(None),
                   token: str | None = Query(None), cmc_session: str | None = Cookie(None)):
    """Trae las radiografias del convenio Imagendent como cargos.

    `vales_convenio` ya guarda paciente, profesional y costo de cada vale. El
    indice unico (origen, origen_ref) hace la operacion idempotente: re-importar
    no duplica nada.
    """
    _auth(request, token, cmc_session)
    mes = mes or _mes_actual()
    creados = 0
    with db() as c:
        try:
            vales = list(c.execute(
                "SELECT folio, paciente, prestacion_nombre, costo, profesional, creado_at "
                "FROM vales_convenio WHERE substr(creado_at,1,7)=? AND estado!='anulado'", (mes,)))
        except Exception as e:
            raise HTTPException(400, f"No se pudo leer los vales: {e}")
        for folio, paciente, prest, costo, prof, creado in vales:
            if not (prof or "").strip():
                continue  # sin profesional no se le puede cobrar a nadie
            try:
                c.execute("INSERT INTO cargo_registro(fecha,mes,profesional,descripcion,categoria,"
                          "cantidad,precio_unit,total,paciente,origen,origen_ref,pagado,"
                          "creado_por,created_at,updated_at) "
                          "VALUES (?,?,?,?,'Radiografia',1,?,?,?,'vale',?,0,'import',?,?)",
                          (creado[:10], mes, prof.strip(), prest, int(costo or 0),
                           int(costo or 0), paciente, folio, _ahora(), _ahora()))
                creados += 1
            except Exception:
                pass  # ya estaba importado (indice unico)
        c.commit()
    return {"ok": True, "creados": creados, "revisados": len(vales), "mes": mes}


@router.get("/resumen-liquidacion")
def resumen_liquidacion(request: Request, mes: str | None = Query(None),
                        token: str | None = Query(None), cmc_session: str | None = Cookie(None)):
    """Lo que hay que descontarle a cada profesional este mes.

    Este es el endpoint que consume Liquidaciones para llenar el campo Ajuste:
    sin esto el modulo seria otro cuaderno que hay que acordarse de mirar.
    """
    _auth(request, token, cmc_session)
    mes = mes or _mes_actual()
    with db() as c:
        filas = list(c.execute(
            "SELECT profesional, COUNT(*), SUM(total) FROM cargo_registro "
            "WHERE mes=? AND pagado=0 GROUP BY profesional ORDER BY SUM(total) DESC", (mes,)))
    items = [{"profesional": r[0], "n": r[1], "descontar": int(r[2] or 0)} for r in filas]
    return {"mes": mes, "items": items, "total": sum(i["descontar"] for i in items)}


@router.get("/export")
def export(request: Request, mes: str | None = Query(None),
           token: str | None = Query(None), cmc_session: str | None = Cookie(None)):
    _auth(request, token, cmc_session)
    mes = mes or _mes_actual()
    with db() as c:
        filas = [_fila(r) for r in c.execute(_SEL + " WHERE mes=? ORDER BY profesional, fecha", (mes,))]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Fecha", "Profesional", "Que se cobra", "Categoria", "Paciente",
                "Cantidad", "Precio unitario", "Total", "Estado", "Origen"])
    for f in filas:
        w.writerow([f["fecha"], f["profesional"], f["descripcion"], f["categoria"],
                    f["paciente"] or "", f["cantidad"], f["precio_unit"], f["total"],
                    "Pagado" if f["pagado"] else "Pendiente", f["origen"]])
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="cargos-{mes}.csv"'})
