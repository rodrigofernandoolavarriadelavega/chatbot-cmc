# -*- coding: utf-8 -*-
"""Router /alma/api/orto-fotos — Registro fotográfico de avance en ortodoncia.

POR QUE EXISTE
--------------
Pedido de la ortodoncista (2026-09-02), textual: *"hay varios que llevan seis
controles, ocho controles... quiero tener un control como un PowerPoint con todo
lo del paciente, fotos cada seis meses, para ir teniendo el avance"*.

Y el problema de fondo, que es el caro: *"nunca ando con el tiempo... yo anoto en
mi libreta y eso lo tengo que pasar después... no llevo computador, del celular,
yo estoy preocupada de la parte clínica, entonces tengo que sentarme a pasar los
avances de 20 pacientes"*. Hoy escribe en papel y transcribe despues — trabajo
hecho dos veces, y ella misma dice que hay avances que no estan al dia.

Por eso el diseño apunta a UN gesto desde el celular: foto + nota, en el momento
del control. Si eso toma mas de diez segundos, vuelve al cuaderno.

DECISIONES
----------
- **La imagen se achica en el NAVEGADOR antes de subir** (canvas, lado mayor
  1600 px, JPEG). En Carampangue la señal es irregular: subir 4 MB desde el box
  no funciona. Ademas evita depender de Pillow en el servidor.
- Los archivos viven en `data/orto_fotos/`, NUNCA en git (`data` esta en
  .gitignore — ver el incidente del 2026-06-10 con el symlink).
- Se sirven por un endpoint con auth, jamas como estatico: son datos de salud.
  El dominio clinico publico da 404.
- `paciente_id` es el id de la BI (mismo que Medilink), asi la foto se ata al
  paciente y no a como se escribio su nombre ese dia.

⚠️ Dato sensible (Ley 21.719, vigente 01-12-2026). El dueño confirma que los
pacientes de ortodoncia YA firman consentimiento; verificar que ese texto
mencione el registro fotografico, que es una finalidad distinta del tratamiento.
"""
from __future__ import annotations

import base64
import logging
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Cookie, HTTPException, Query, Request
from fastapi.responses import Response

from session import db, log_event

log = logging.getLogger("orto_fotos_routes")
_CHILE_TZ = ZoneInfo("America/Santiago")
router = APIRouter(prefix="/alma/api/orto-fotos", tags=["orto-fotos"])

_DIR = Path(__file__).resolve().parent.parent / "data" / "orto_fotos"
_DIR.mkdir(parents=True, exist_ok=True)
_MAX_BYTES = 3 * 1024 * 1024          # tope de seguridad; el navegador ya achica
# Vistas estandar del registro ortodoncico.
VISTAS = ["Frontal", "Perfil", "Sonrisa", "Intraoral superior",
          "Intraoral inferior", "Oclusión derecha", "Oclusión izquierda", "Otra"]


def _ahora() -> str:
    return datetime.now(_CHILE_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _hoy() -> str:
    return datetime.now(_CHILE_TZ).strftime("%Y-%m-%d")


def _crear_tabla() -> None:
    with db() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS orto_foto (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                paciente_id  INTEGER,
                paciente     TEXT NOT NULL,
                fecha        TEXT NOT NULL,
                vista        TEXT,
                nota         TEXT,
                archivo      TEXT NOT NULL,
                bytes        INTEGER,
                creado_por   TEXT,
                created_at   TEXT
            )""")
        c.execute("CREATE INDEX IF NOT EXISTS ix_orto_foto_pac "
                  "ON orto_foto(paciente_id, fecha)")
        c.commit()


_crear_tabla()


def _auth(request: Request, token: str | None, cmc_session: str | None) -> str:
    from admin_routes import _verify_cookie, _is_admin_token
    from config import ADMIN_TOKEN, ORTODONCIA_TOKEN

    host = (request.headers.get("host") or "").split(":")[0].lower()
    if host.endswith("centromedicocarampangue.cl"):
        raise HTTPException(status_code=404, detail="Not found")
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        tk = auth_header.split(None, 1)[1].strip()
        if _is_admin_token(tk) or (ORTODONCIA_TOKEN and tk == ORTODONCIA_TOKEN):
            return tk
    if cmc_session and _verify_cookie(cmc_session) in ("admin", "ortodoncia", "administracion"):
        return ADMIN_TOKEN
    if token and (_is_admin_token(token) or (ORTODONCIA_TOKEN and token == ORTODONCIA_TOKEN)):
        return token
    raise HTTPException(status_code=401, detail="Token invalido")


@router.get("/vistas")
def vistas(request: Request, token: str | None = Query(None),
           cmc_session: str | None = Cookie(None)):
    _auth(request, token, cmc_session)
    return {"vistas": VISTAS}


@router.get("")
def listar(request: Request, paciente_id: int | None = Query(None),
           token: str | None = Query(None), cmc_session: str | None = Cookie(None)):
    """Fotos de un paciente, agrupadas por fecha — la línea de tiempo del avance."""
    _auth(request, token, cmc_session)
    sql = ("SELECT id,paciente_id,paciente,fecha,vista,nota,bytes,created_at "
           "FROM orto_foto")
    args: list = []
    if paciente_id:
        sql += " WHERE paciente_id=?"
        args.append(paciente_id)
    sql += " ORDER BY fecha DESC, id DESC"
    with db() as c:
        filas = list(c.execute(sql, args))
    fotos = [{"id": r[0], "paciente_id": r[1], "paciente": r[2], "fecha": r[3],
              "vista": r[4], "nota": r[5], "bytes": r[6], "created_at": r[7]}
             for r in filas]
    # agrupado por sesión (fecha) para pintar el avance sin recalcular en el cliente
    sesiones: dict = {}
    for f in fotos:
        sesiones.setdefault(f["fecha"], []).append(f)
    return {"fotos": fotos,
            "sesiones": [{"fecha": k, "fotos": v} for k, v in
                         sorted(sesiones.items(), reverse=True)],
            "n": len(fotos)}


@router.post("")
async def subir(request: Request, token: str | None = Query(None),
                cmc_session: str | None = Cookie(None)):
    """Recibe la foto ya achicada por el navegador, como data URL.

    Se acepta JSON (no multipart) porque el cliente ya paso la imagen por un
    canvas para achicarla: lo que llega es un data URL, no el archivo original.
    """
    _auth(request, token, cmc_session)
    b = await request.json()
    paciente = (b.get("paciente") or "").strip()
    if not paciente:
        raise HTTPException(400, "Falta el paciente")
    data = b.get("imagen") or ""
    m = re.match(r"^data:image/(jpeg|jpg|png|webp);base64,(.+)$", data, re.S)
    if not m:
        raise HTTPException(400, "La imagen no llegó en el formato esperado")
    try:
        crudo = base64.b64decode(m.group(2), validate=True)
    except Exception:
        raise HTTPException(400, "La imagen viene corrupta")
    if len(crudo) > _MAX_BYTES:
        raise HTTPException(413, "La imagen pesa demasiado incluso achicada")

    ext = "jpg" if m.group(1) in ("jpeg", "jpg") else m.group(1)
    fecha = (b.get("fecha") or _hoy())[:10]
    vista = b.get("vista") if b.get("vista") in VISTAS else "Otra"
    with db() as c:
        c.execute("INSERT INTO orto_foto(paciente_id,paciente,fecha,vista,nota,"
                  "archivo,bytes,creado_por,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                  (b.get("paciente_id"), paciente, fecha, vista,
                   (b.get("nota") or "").strip() or None, "", len(crudo),
                   b.get("creado_por") or "ortodoncia", _ahora()))
        fid = list(c.execute("SELECT last_insert_rowid()"))[0][0]
        nombre = f"{fid}.{ext}"
        (_DIR / nombre).write_bytes(crudo)
        c.execute("UPDATE orto_foto SET archivo=? WHERE id=?", (nombre, fid))
        c.commit()
    log_event(None, "orto_foto_subida",
              {"paciente": paciente, "vista": vista, "kb": len(crudo) // 1024})
    return {"ok": True, "id": fid, "kb": len(crudo) // 1024}


@router.post("/dictar")
async def dictar(request: Request, token: str | None = Query(None),
                 cmc_session: str | None = Cookie(None)):
    """Nota de voz -> texto. La ortodoncista habla, no escribe.

    El audio se transcribe y SE DESCARTA: lo que queda en la ficha es el texto,
    que es el registro clinico y ademas se puede buscar. Guardar tambien el
    audio duplicaria el dato sensible sin agregar nada.

    Ella corrige el texto antes de guardar — Whisper se equivoca con nombres
    propios y numeracion dentaria, y una ficha con un diente equivocado es peor
    que una ficha vacia.

    Nota medida (memoria `alma_stack_clinico_2026_07`): Whisper `tiny` es
    inservible en es-CL (27,4% de error). Se usa `whisper-1` de la API, que es
    el grande — el mismo que ya transcribe las notas de voz del bot.
    """
    _auth(request, token, cmc_session)
    b = await request.json()
    data = b.get("audio") or ""
    m = re.match(r"^data:audio/([a-z0-9.+-]+);base64,(.+)$", data, re.S)
    if not m:
        raise HTTPException(400, "El audio no llegó en el formato esperado")
    try:
        crudo = base64.b64decode(m.group(2), validate=True)
    except Exception:
        raise HTTPException(400, "El audio viene corrupto")
    if not crudo:
        raise HTTPException(400, "El audio llegó vacío")
    if len(crudo) > 8 * 1024 * 1024:
        raise HTTPException(413, "La nota de voz es demasiado larga")
    from messaging import transcribe_audio
    texto = await transcribe_audio(crudo, f"audio/{m.group(1)}")
    if not texto:
        raise HTTPException(502, "No se pudo transcribir — escribe la nota a mano")
    log_event(None, "orto_nota_voz", {"seg_aprox": len(crudo) // 16000,
                                      "chars": len(texto)})
    return {"ok": True, "texto": texto}


@router.get("/img/{fid}")
def imagen(fid: int, request: Request, token: str | None = Query(None),
           cmc_session: str | None = Cookie(None)):
    """Sirve la imagen con auth. Nunca como estático: es dato de salud."""
    _auth(request, token, cmc_session)
    with db() as c:
        r = list(c.execute("SELECT archivo FROM orto_foto WHERE id=?", (fid,)))
    if not r or not r[0][0]:
        raise HTTPException(404, "No existe")
    ruta = _DIR / r[0][0]
    if not ruta.exists():
        raise HTTPException(404, "El archivo no está en disco")
    tipo = "image/png" if ruta.suffix == ".png" else (
        "image/webp" if ruta.suffix == ".webp" else "image/jpeg")
    return Response(ruta.read_bytes(), media_type=tipo,
                    headers={"Cache-Control": "private, max-age=86400"})


@router.patch("/{fid}")
async def editar(fid: int, request: Request, token: str | None = Query(None),
                 cmc_session: str | None = Cookie(None)):
    _auth(request, token, cmc_session)
    b = await request.json()
    with db() as c:
        if not list(c.execute("SELECT 1 FROM orto_foto WHERE id=?", (fid,))):
            raise HTTPException(404, "No existe")
        for campo in ("nota", "vista", "fecha"):
            if campo in b:
                c.execute(f"UPDATE orto_foto SET {campo}=? WHERE id=?", (b[campo], fid))
        c.commit()
    return {"ok": True}


@router.delete("/{fid}")
def borrar(fid: int, request: Request, token: str | None = Query(None),
           cmc_session: str | None = Cookie(None)):
    _auth(request, token, cmc_session)
    with db() as c:
        r = list(c.execute("SELECT archivo,paciente FROM orto_foto WHERE id=?", (fid,)))
        if not r:
            raise HTTPException(404, "No existe")
        c.execute("DELETE FROM orto_foto WHERE id=?", (fid,))
        c.commit()
    try:
        (_DIR / r[0][0]).unlink(missing_ok=True)
    except Exception as e:
        log.warning("orto_foto %s: no se pudo borrar el archivo: %s", fid, e)
    log_event(None, "orto_foto_borrada", {"paciente": r[0][1]})
    return {"ok": True}


@router.get("/resumen")
def resumen(request: Request, token: str | None = Query(None),
            cmc_session: str | None = Cookie(None)):
    """Cuántas fotos y de cuántos pacientes — para el contador del panel."""
    _auth(request, token, cmc_session)
    with db() as c:
        n, pac = list(c.execute(
            "SELECT COUNT(*), COUNT(DISTINCT COALESCE(paciente_id, paciente)) FROM orto_foto"))[0]
        por_pac = {r[0]: r[1] for r in c.execute(
            "SELECT paciente_id, COUNT(*) FROM orto_foto WHERE paciente_id IS NOT NULL "
            "GROUP BY paciente_id")}
    return {"n": n, "pacientes": pac, "por_paciente": por_pac}
