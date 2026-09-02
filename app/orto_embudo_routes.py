# -*- coding: utf-8 -*-
"""Router /alma/api/orto-embudo — Embudo de ortodoncia ANTES de la instalacion.

POR QUE EXISTE (y por que no alcanzaba el modulo que ya habia)
--------------------------------------------------------------
`ortodoncia_routes.py` sigue a los pacientes YA instalados: controles vencidos,
avance del tratamiento, plan de pago. Empieza donde termina este.

Javiera Burgos pidio el 2026-09-01 el tramo anterior, que no existia: *"uno que
tenga el proceso inicial, tratamiento previo, venta de cupones, radiografia
recibida, enviada a Dani, respuesta de Dani, respuesta a pcte, agendado para
instalacion... ahora que ha aumentado el flujo se podria hacer uno... ahora lo
estoy haciendo con mi mente nomas"*.

El riesgo concreto: la Dra. Castillo vive en Concepcion y el ida y vuelta de
radiografias pasa por WhatsApp. Un paciente que se cae entre "enviada a Dani" y
"respuesta a paciente" no lo nota nadie, porque el unico registro esta en la
cabeza de una persona. Las dos etapas de espera externa son las fragiles: por
eso `dias_en_etapa` y el semaforo de atraso son el corazon del modulo, no un
adorno.

Tabla: orto_embudo (sessions.db). Auth: token de ortodoncia o admin.
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

log = logging.getLogger("orto_embudo_routes")
_CHILE_TZ = ZoneInfo("America/Santiago")
router = APIRouter(prefix="/alma/api/orto-embudo", tags=["orto-embudo"])

# Las 8 etapas son textuales de Javiera, en su orden. `espera` marca las que
# dependen de un tercero (la Dra. Castillo) — ahi es donde se cae la gente.
# `alerta` = dias despues de los cuales la tarjeta se pone en rojo.
ETAPAS = [
    {"id": "proceso_inicial",     "label": "Proceso inicial",       "alerta": 7,  "espera": False},
    {"id": "tratamiento_previo",  "label": "Tratamiento previo",    "alerta": 30, "espera": False},
    {"id": "venta_cupones",       "label": "Venta de cupones",      "alerta": 10, "espera": False},
    {"id": "rx_recibida",         "label": "Radiografía recibida",  "alerta": 5,  "espera": False},
    {"id": "enviada_dani",        "label": "Enviada a Dani",        "alerta": 4,  "espera": True},
    {"id": "respuesta_dani",      "label": "Respuesta de Dani",     "alerta": 3,  "espera": False},
    {"id": "respuesta_paciente",  "label": "Respuesta a paciente",  "alerta": 5,  "espera": True},
    {"id": "agendado",            "label": "Agendado instalación",  "alerta": 30, "espera": False},
]
FINALES = [
    {"id": "instalado",  "label": "Instalado",  "alerta": 0, "espera": False},
    {"id": "descartado", "label": "No sigue",   "alerta": 0, "espera": False},
]
_TODAS = {e["id"]: e for e in ETAPAS + FINALES}
_ORDEN = [e["id"] for e in ETAPAS] + [e["id"] for e in FINALES]


def _ahora() -> str:
    return datetime.now(_CHILE_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _crear_tabla() -> None:
    with db() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS orto_embudo (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                paciente     TEXT NOT NULL,
                telefono     TEXT,
                rut          TEXT,
                etapa        TEXT NOT NULL DEFAULT 'proceso_inicial',
                etapa_desde  TEXT NOT NULL,
                notas        TEXT,
                valor_cupones INTEGER DEFAULT 0,
                historial    TEXT,
                creado_por   TEXT,
                created_at   TEXT,
                updated_at   TEXT
            )""")
        c.execute("CREATE INDEX IF NOT EXISTS ix_orto_embudo_etapa ON orto_embudo(etapa, etapa_desde)")
        c.commit()


_crear_tabla()


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


def _dias(desde: str) -> int:
    try:
        d0 = datetime.strptime(desde[:10], "%Y-%m-%d").date()
        return (datetime.now(_CHILE_TZ).date() - d0).days
    except Exception:
        return 0


def _fila(r) -> dict:
    etapa = r[4] if r[4] in _TODAS else "proceso_inicial"
    meta = _TODAS[etapa]
    d = _dias(r[5])
    return {
        "id": r[0], "paciente": r[1], "telefono": r[2], "rut": r[3],
        "etapa": etapa, "etapa_label": meta["label"], "etapa_desde": r[5][:10],
        "dias_en_etapa": d, "espera_externa": meta["espera"],
        "atrasado": bool(meta["alerta"]) and d > meta["alerta"],
        "limite": meta["alerta"],
        "notas": r[6], "valor_cupones": r[7] or 0,
    }


_SEL = ("SELECT id,paciente,telefono,rut,etapa,etapa_desde,notas,valor_cupones "
        "FROM orto_embudo")


@router.get("/tablero")
def tablero(request: Request, incluir_finales: int = Query(0),
            token: str | None = Query(None), cmc_session: str | None = Cookie(None)):
    """El tablero completo, una columna por etapa."""
    _auth(request, token, cmc_session)
    with db() as c:
        filas = [_fila(r) for r in c.execute(_SEL + " ORDER BY etapa_desde")]
    activos = [f for f in filas if f["etapa"] not in ("instalado", "descartado")]
    cols = []
    for e in ETAPAS + (FINALES if incluir_finales else []):
        ps = [f for f in filas if f["etapa"] == e["id"]]
        ps.sort(key=lambda x: -x["dias_en_etapa"])
        cols.append({"id": e["id"], "label": e["label"], "espera": e["espera"],
                     "limite": e["alerta"], "n": len(ps), "pacientes": ps})
    atrasados = [f for f in activos if f["atrasado"]]
    return {
        "columnas": cols,
        "total_activos": len(activos),
        "atrasados": len(atrasados),
        "esperando_dani": len([f for f in activos if f["etapa"] == "enviada_dani"]),
        "esperando_paciente": len([f for f in activos if f["etapa"] == "respuesta_paciente"]),
        "valor_cupones": sum(f["valor_cupones"] for f in activos),
        "peor": sorted(atrasados, key=lambda x: -x["dias_en_etapa"])[:5],
    }


@router.post("")
async def crear(request: Request, token: str | None = Query(None),
                cmc_session: str | None = Cookie(None)):
    _auth(request, token, cmc_session)
    b = await request.json()
    nombre = (b.get("paciente") or "").strip()
    if not nombre:
        raise HTTPException(400, "Falta el nombre del paciente")
    etapa = b.get("etapa") if b.get("etapa") in _TODAS else "proceso_inicial"
    with db() as c:
        c.execute("INSERT INTO orto_embudo(paciente,telefono,rut,etapa,etapa_desde,notas,"
                  "valor_cupones,historial,creado_por,created_at,updated_at) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                  (nombre, (b.get("telefono") or "").strip() or None,
                   (b.get("rut") or "").strip() or None, etapa, _ahora(),
                   b.get("notas"), int(b.get("valor_cupones") or 0),
                   f"{_ahora()} creado en {etapa}", b.get("creado_por") or "recepcion",
                   _ahora(), _ahora()))
        pid = list(c.execute("SELECT last_insert_rowid()"))[0][0]
        c.commit()
    log_event(None, "orto_embudo_alta", {"paciente": nombre, "etapa": etapa})
    return {"ok": True, "id": pid}


@router.patch("/{pid}")
async def editar(pid: int, request: Request, token: str | None = Query(None),
                 cmc_session: str | None = Cookie(None)):
    """Mover de etapa o corregir datos.

    Al cambiar de etapa se reinicia `etapa_desde` — es lo que hace que el
    contador de dias signifique "cuanto lleva esperando ESTO", que es la
    pregunta que Javiera hoy responde de memoria.
    """
    _auth(request, token, cmc_session)
    b = await request.json()
    with db() as c:
        row = list(c.execute("SELECT etapa,historial FROM orto_embudo WHERE id=?", (pid,)))
        if not row:
            raise HTTPException(404, "No existe ese paciente en el embudo")
        if "etapa" in b:
            nueva = b["etapa"]
            if nueva not in _TODAS:
                raise HTTPException(400, "Etapa desconocida")
            if nueva != row[0][0]:
                hist = (row[0][1] or "") + f"\n{_ahora()} {row[0][0]} → {nueva}"
                c.execute("UPDATE orto_embudo SET etapa=?,etapa_desde=?,historial=?,updated_at=? WHERE id=?",
                          (nueva, _ahora(), hist[-4000:], _ahora(), pid))
        for campo in ("paciente", "telefono", "rut", "notas"):
            if campo in b:
                c.execute(f"UPDATE orto_embudo SET {campo}=?,updated_at=? WHERE id=?",
                          (b[campo], _ahora(), pid))
        if "valor_cupones" in b:
            c.execute("UPDATE orto_embudo SET valor_cupones=?,updated_at=? WHERE id=?",
                      (int(b["valor_cupones"] or 0), _ahora(), pid))
        c.commit()
    return {"ok": True}


@router.delete("/{pid}")
def borrar(pid: int, request: Request, token: str | None = Query(None),
           cmc_session: str | None = Cookie(None)):
    _auth(request, token, cmc_session)
    with db() as c:
        c.execute("DELETE FROM orto_embudo WHERE id=?", (pid,))
        c.commit()
    return {"ok": True}


@router.get("/etapas")
def etapas(request: Request, token: str | None = Query(None),
           cmc_session: str | None = Cookie(None)):
    _auth(request, token, cmc_session)
    return {"etapas": ETAPAS, "finales": FINALES, "orden": _ORDEN}


@router.get("/export")
def export(request: Request, token: str | None = Query(None),
           cmc_session: str | None = Cookie(None)):
    _auth(request, token, cmc_session)
    with db() as c:
        filas = [_fila(r) for r in c.execute(_SEL + " ORDER BY etapa, etapa_desde")]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Paciente", "Telefono", "RUT", "Etapa", "Desde", "Dias en etapa",
                "Atrasado", "Valor cupones", "Notas"])
    for f in filas:
        w.writerow([f["paciente"], f["telefono"] or "", f["rut"] or "", f["etapa_label"],
                    f["etapa_desde"], f["dias_en_etapa"], "SI" if f["atrasado"] else "",
                    f["valor_cupones"], (f["notas"] or "").replace("\n", " ")])
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": 'attachment; filename="embudo-ortodoncia.csv"'})
