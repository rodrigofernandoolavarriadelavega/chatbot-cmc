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
import html as _html
import json
import io
import logging
import os
import smtplib
import threading
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
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
                examenes     TEXT,
                valor_cupones INTEGER DEFAULT 0,
                historial    TEXT,
                creado_por   TEXT,
                created_at   TEXT,
                updated_at   TEXT
            )""")
        c.execute("CREATE INDEX IF NOT EXISTS ix_orto_embudo_etapa ON orto_embudo(etapa, etapa_desde)")
        c.execute("""
            CREATE TABLE IF NOT EXISTS orto_embudo_config (
                clave TEXT PRIMARY KEY,
                valor TEXT
            )""")
        c.commit()


_crear_tabla()

# La tabla pudo nacer sin `examenes` (se desplego antes que esta funcion).
with db() as _c:
    if "examenes" not in [r[1] for r in _c.execute("PRAGMA table_info(orto_embudo)")]:
        _c.execute("ALTER TABLE orto_embudo ADD COLUMN examenes TEXT")
        _c.commit()
        log.info("orto_embudo: columna examenes agregada")


# ── Aviso por correo ────────────────────────────────────────────────────────
# El equipo ya se coordina por correo, asi que el aviso entra por el canal que
# ya usan en vez de inventar uno nuevo. Los destinatarios viven en la BD (no en
# el codigo) para poder cambiarlos sin un deploy.
#
# PRIVACIDAD: el correo lleva SOLO nombre y etapa. Nada clinico, ningun RUT,
# ningun telefono. Es un aviso de coordinacion, no una ficha (Ley 21.719).
# La solicitud de examenes NO se inventa: se siembra del catalogo real del
# convenio (`vales_routes.PRESTACIONES`), donde el "Set radiologico ortodoncia"
# ya viene definido y con precio como bitewing + panoramica + teleradiografia.
# Javiera edita la lista desde la pagina; esto es solo el punto de partida.
_EXAMENES_SEED = [
    {"n": "Set radiológico ortodoncia (bitewing + panorámica + teleradiografía)", "d": 1},
    {"n": "Fotografías clínicas (intraorales y extraorales)", "d": 1},
    {"n": "Modelos de estudio", "d": 1},
    {"n": "Radiografía Panorámica", "d": 0},
    {"n": "Teleradiografía de perfil", "d": 0},
    {"n": "Escaneo intraoral digital — ambos maxilares", "d": 0},
    {"n": "CONE BEAM Bimaxilar", "d": 0},
]
_DEFAULTS = {"mails": "", "avisar_alta": "1", "avisar_dani": "1",
             "examenes": json.dumps(_EXAMENES_SEED, ensure_ascii=False)}


def _examenes_catalogo() -> list:
    try:
        v = json.loads(_cfg().get("examenes") or "[]")
        return v if isinstance(v, list) and v else list(_EXAMENES_SEED)
    except Exception:
        return list(_EXAMENES_SEED)


def _cfg() -> dict:
    with db() as c:
        filas = dict(c.execute("SELECT clave, valor FROM orto_embudo_config"))
    out = dict(_DEFAULTS)
    out.update({k: v for k, v in filas.items() if v is not None})
    return out


def _cfg_set(d: dict) -> None:
    with db() as c:
        for k, v in d.items():
            if k in _DEFAULTS:
                c.execute("INSERT INTO orto_embudo_config(clave,valor) VALUES (?,?) "
                          "ON CONFLICT(clave) DO UPDATE SET valor=excluded.valor", (k, str(v)))
        c.commit()


def _destinatarios() -> list[str]:
    raw = _cfg().get("mails") or ""
    return [x.strip() for x in raw.replace(";", ",").split(",") if "@" in x.strip()]


def _enviar(asunto: str, cuerpo_html: str, para: list[str]) -> None:
    """Envia por el Gmail del centro. Corre en un hilo: si el correo falla o
    tarda, la anotacion de Javiera ya quedo guardada igual."""
    user = os.getenv("GMAIL_CMC_USER", "")
    pwd = os.getenv("GMAIL_CMC_APP_PASSWORD", "")
    if not (user and pwd and para):
        log.info("orto_embudo: aviso omitido (sin credencial o sin destinatarios)")
        return
    msg = MIMEMultipart("alternative")
    msg["Subject"] = asunto
    msg["From"] = f"Centro Medico Carampangue <{user}>"
    msg["To"] = ", ".join(para)
    msg.attach(MIMEText(cuerpo_html, "html", "utf-8"))
    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=25) as sv:
            sv.starttls()
            sv.login(user, pwd)
            sv.sendmail(user, para, msg.as_string())
        log.info("orto_embudo: aviso enviado a %d destinatarios", len(para))
    except Exception as e:
        log.warning("orto_embudo: no se pudo enviar el aviso: %s", e)


def _avisar(asunto: str, titulo: str, lineas: list, pie: str,
            examenes: list | None = None) -> None:
    para = _destinatarios()
    if not para:
        return
    bloque = ""
    if examenes:
        items = "".join(f'<li style="margin:4px 0">{_html.escape(x)}</li>' for x in examenes)
        bloque = (f'<div style="margin:18px 0 0;padding:14px 16px;background:#F1F8FA;'
                  f'border-left:3px solid #4FBECE;border-radius:0 8px 8px 0">'
                  f'<div style="font-size:12px;letter-spacing:.12em;text-transform:uppercase;'
                  f'color:#1172AB;font-weight:700">Solicitud de examenes</div>'
                  f'<ul style="margin:9px 0 0;padding-left:18px;color:#0F3F68;font-size:14px">'
                  f'{items}</ul></div>')
    filas = "".join(
        f'<tr><td style="padding:5px 14px 5px 0;color:#6b8095;font-size:13px">{_html.escape(k)}</td>'
        f'<td style="padding:5px 0;color:#16324f;font-size:14px;font-weight:600">{_html.escape(v)}</td></tr>'
        for k, v in lineas)
    cuerpo = f"""<div style="font-family:Helvetica,Arial,sans-serif;max-width:520px">
<h2 style="color:#16324f;font-size:17px;margin:0 0 4px">{_html.escape(titulo)}</h2>
<p style="color:#6b8095;font-size:13px;margin:0 0 14px">Embudo de Ortodoncia &middot; Centro Medico Carampangue</p>
<table style="border-collapse:collapse">{filas}</table>{bloque}
<p style="color:#6b8095;font-size:12px;margin:16px 0 0;line-height:1.6">{_html.escape(pie)}</p>
<p style="color:#9aabbd;font-size:11px;margin:14px 0 0;line-height:1.5">
Aviso automatico de coordinacion. No contiene informacion clinica.</p></div>"""
    threading.Thread(target=_enviar, args=(asunto, cuerpo, para), daemon=True).start()


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
        exs = [x.strip() for x in (b.get("examenes") or []) if str(x).strip()]
        c.execute("INSERT INTO orto_embudo(paciente,telefono,rut,etapa,etapa_desde,notas,"
                  "valor_cupones,examenes,historial,creado_por,created_at,updated_at) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                  (nombre, (b.get("telefono") or "").strip() or None,
                   (b.get("rut") or "").strip() or None, etapa, _ahora(),
                   b.get("notas"), int(b.get("valor_cupones") or 0),
                   json.dumps(exs, ensure_ascii=False) if exs else None,
                   f"{_ahora()} creado en {etapa}", b.get("creado_por") or "recepcion",
                   _ahora(), _ahora()))
        pid = list(c.execute("SELECT last_insert_rowid()"))[0][0]
        c.commit()
    log_event(None, "orto_embudo_alta", {"paciente": nombre, "etapa": etapa})
    if _cfg().get("avisar_alta") == "1":
        lineas = [("Paciente", nombre), ("Etapa", _TODAS[etapa]["label"]),
                  ("Fecha", _ahora()[:16])]
        _avisar(f"Ortodoncia · paciente nuevo: {nombre}",
                "Entro un paciente nuevo al embudo", lineas,
                "Queda registrado en el embudo de ortodoncia de Alma.",
                examenes=exs)
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
                if nueva == "enviada_dani" and _cfg().get("avisar_dani") == "1":
                    nom = list(c.execute("SELECT paciente FROM orto_embudo WHERE id=?", (pid,)))[0][0]
                    _avisar(f"Ortodoncia · radiografia enviada: {nom}",
                            "Se envio una radiografia para evaluacion",
                            [("Paciente", nom), ("Enviada", _ahora()[:16])],
                            "Queda esperando respuesta. Si pasan mas de 4 dias, el embudo la marca en rojo.")
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


@router.get("/paciente/{pid}")
def detalle(pid: int, request: Request, token: str | None = Query(None),
            cmc_session: str | None = Cookie(None)):
    """Ficha completa con su linea de tiempo.

    `historial` se guarda como texto append-only ("<ts> <de> -> <a>") porque el
    volumen es chico y asi sobrevive a cualquier cambio de esquema. Se parsea
    aca para la vista en vez de guardar filas: si manana cambian las etapas, el
    historial viejo se sigue leyendo igual.
    """
    _auth(request, token, cmc_session)
    with db() as c:
        r = list(c.execute(
            "SELECT id,paciente,telefono,rut,etapa,etapa_desde,notas,valor_cupones,"
            "historial,created_at FROM orto_embudo WHERE id=?", (pid,)))
    if not r:
        raise HTTPException(404, "No existe")
    row = r[0]
    pasos = []
    for linea in (row[8] or "").strip().split("\n"):
        linea = linea.strip()
        if not linea:
            continue
        ts, _, resto = linea.partition(" ")
        if len(ts) == 10 and " " in linea:
            ts, resto = linea[:19], linea[20:]
        if "→" in resto:
            de, _, a = resto.partition("→")
            pasos.append({"ts": ts, "de": _TODAS.get(de.strip(), {}).get("label", de.strip()),
                          "a": _TODAS.get(a.strip(), {}).get("label", a.strip())})
        else:
            pasos.append({"ts": ts, "de": None, "a": resto.strip()})
    base = _fila(row[:8])
    base["creado"] = (row[9] or "")[:16]
    base["pasos"] = pasos
    base["orden"] = _ORDEN.index(base["etapa"]) + 1 if base["etapa"] in _ORDEN else 0
    base["total_etapas"] = len(ETAPAS)
    return base


@router.get("/examenes")
def examenes_get(request: Request, token: str | None = Query(None),
                 cmc_session: str | None = Cookie(None)):
    _auth(request, token, cmc_session)
    return {"items": _examenes_catalogo()}


@router.post("/examenes")
async def examenes_set(request: Request, token: str | None = Query(None),
                       cmc_session: str | None = Cookie(None)):
    """Guarda cual es 'lo que se pide siempre'. Solo Javiera lo sabe."""
    _auth(request, token, cmc_session)
    b = await request.json()
    items = [{"n": str(x.get("n", "")).strip(), "d": 1 if x.get("d") else 0}
             for x in (b.get("items") or []) if str(x.get("n", "")).strip()]
    if not items:
        raise HTTPException(400, "La lista no puede quedar vacia")
    _cfg_set({"examenes": json.dumps(items, ensure_ascii=False)})
    return {"ok": True, "items": items}


@router.get("/config")
def config_get(request: Request, token: str | None = Query(None),
               cmc_session: str | None = Cookie(None)):
    _auth(request, token, cmc_session)
    c = _cfg()
    return {"mails": c.get("mails", ""), "avisar_alta": c.get("avisar_alta") == "1",
            "avisar_dani": c.get("avisar_dani") == "1",
            "hay_credencial": bool(os.getenv("GMAIL_CMC_USER")),
            "destinatarios": _destinatarios()}


@router.post("/config")
async def config_set(request: Request, token: str | None = Query(None),
                     cmc_session: str | None = Cookie(None)):
    _auth(request, token, cmc_session)
    b = await request.json()
    _cfg_set({"mails": (b.get("mails") or "").strip(),
              "avisar_alta": "1" if b.get("avisar_alta") else "0",
              "avisar_dani": "1" if b.get("avisar_dani") else "0"})
    return {"ok": True, "destinatarios": _destinatarios()}


@router.post("/config/probar")
def config_probar(request: Request, token: str | None = Query(None),
                  cmc_session: str | None = Cookie(None)):
    """Correo de prueba, para confirmar que llega antes de confiar en el."""
    _auth(request, token, cmc_session)
    para = _destinatarios()
    if not para:
        raise HTTPException(400, "No hay correos configurados")
    _avisar("Ortodoncia · correo de prueba", "Prueba de aviso",
            [("Estado", "Si te llego esto, los avisos funcionan"),
             ("Enviado", _ahora()[:16])],
            "Puedes ignorar este correo.")
    return {"ok": True, "enviados_a": para}


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
