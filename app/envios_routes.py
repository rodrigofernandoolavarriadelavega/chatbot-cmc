"""
Router /alma/api/envios — Vista de auditoría de Envíos/Campañas.

Lista TODO lo que el bot manda de forma proactiva (campañas): templates
(winback, consent, promos) e imágenes (flyers). Cruza con message_statuses
para mostrar el estado real de entrega (enviado / entregado / leído / falló +
código de error de Meta, ej. 131047 = ventana 24h cerrada).

Fuente: tabla `messages` (direction='out', texto marcado con [template:…] o
[imagen]) + `message_statuses` (por wamid) + `contact_profiles` (nombre).
Read-only. Auth: mismo patrón que pagos/abonos.
"""
import logging
import re

from fastapi import APIRouter, Cookie, HTTPException, Query, Request

log = logging.getLogger("envios_routes")
router = APIRouter(prefix="/alma/api/envios", tags=["envios"])

_IMG_RE = re.compile(r"https?://[^\s<]+\.(?:jpe?g|png|webp|gif)(?:\?[^\s<]*)?", re.I)
_TPL_RE = re.compile(r"^\[template:\s*([^\]]+)\]\s*(.*)$", re.S)

# status Meta → etiqueta + color para el panel
_ESTADO_MAP = {
    "read":      ("Leído",     "ok"),
    "delivered": ("Entregado", "ok"),
    "sent":      ("Enviado",   "info"),
    "failed":    ("Falló",     "bad"),
    "undelivered": ("No entregado", "bad"),
}


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


@router.get("")
async def get_envios(request: Request,
                     dias: int = Query(7, ge=1, le=90),
                     tipo: str = Query("todos"),
                     campana: str | None = Query(None, description="nombre exacto de template"),
                     token: str | None = Query(None),
                     cmc_session: str | None = Cookie(None)):
    """Lista mensajes salientes de campaña (template/imagen) con estado de entrega.
    `campana` filtra a un template específico (ej. dental_limpieza_junio_v2)."""
    _require_admin_dep(request, token=token, cmc_session=cmc_session)

    where = ["m.direction = 'out'",
             "(m.text LIKE '[template:%' OR m.text LIKE '[imagen]%')",
             "m.ts >= datetime('now', ?)"]
    params: list = [f"-{int(dias)} days"]
    if tipo == "template":
        where.append("m.text LIKE '[template:%'")
    elif tipo == "imagen":
        where.append("m.text LIKE '[imagen]%'")
    if campana:
        # Solo nombres de template válidos (alfanumérico + _), evita inyección por LIKE
        safe = re.sub(r"[^A-Za-z0-9_]", "", campana)
        if safe:
            where.append("m.text LIKE ?")
            params.append(f"[template: {safe}]%")

    from session import db as _conn
    with _conn() as conn:
        rows = conn.execute(
            f"""
            SELECT m.id, m.phone, m.text, m.ts, m.wamid,
                   cp.nombre  AS nombre,
                   ms.status  AS estado,
                   ms.error_code AS error_code,
                   ms.error_title AS error_title
            FROM messages m
            LEFT JOIN contact_profiles cp ON cp.phone = m.phone
            LEFT JOIN message_statuses ms ON ms.wamid = m.wamid
            WHERE {' AND '.join(where)}
            ORDER BY m.ts DESC
            LIMIT 800
            """,
            params,
        ).fetchall()

    envios = []
    resumen = {"total": 0, "template": 0, "imagen": 0,
               "entregados": 0, "fallidos": 0}
    for r in rows:
        d = dict(r)
        text = d.get("text") or ""
        es_template = text.startswith("[template:")
        tipo_row = "template" if es_template else "imagen"

        nombre_tpl = ""
        contenido = ""
        img_url = ""
        if es_template:
            m = _TPL_RE.match(text)
            if m:
                nombre_tpl = m.group(1).strip()
                contenido = (m.group(2) or "").strip()
            mi = _IMG_RE.search(contenido)
            if mi:
                img_url = mi.group(0)
        else:
            contenido = text.replace("[imagen]", "", 1).strip()
            mi = _IMG_RE.search(text)
            if mi:
                img_url = mi.group(0)
                contenido = contenido.replace(img_url, "").strip()

        estado_raw = (d.get("estado") or "").lower()
        # Si no hay callback de estado todavía, asumimos 'enviado'.
        if not estado_raw:
            estado_lbl, estado_cls = ("Enviado", "info")
        else:
            estado_lbl, estado_cls = _ESTADO_MAP.get(estado_raw, (estado_raw, "info"))
        err = d.get("error_code")
        if err:
            estado_cls = "bad"
            if str(err) == "131047":
                estado_lbl = "No entregado (ventana cerrada)"

        envios.append({
            "id": d["id"],
            "ts": d.get("ts"),
            "phone": d.get("phone"),
            "nombre": d.get("nombre") or "",
            "tipo": tipo_row,
            "template": nombre_tpl,
            "contenido": contenido[:280],
            "img_url": img_url,
            "estado": estado_lbl,
            "estado_cls": estado_cls,
            "error_code": err,
            "error_title": d.get("error_title") or "",
        })
        resumen["total"] += 1
        resumen[tipo_row] += 1
        if estado_cls == "ok":
            resumen["entregados"] += 1
        if estado_cls == "bad":
            resumen["fallidos"] += 1

    return {"envios": envios, "resumen": resumen, "dias": dias, "tipo": tipo}


@router.get("/campanas")
async def get_campanas(request: Request,
                       dias: int = Query(30, ge=1, le=180),
                       token: str | None = Query(None),
                       cmc_session: str | None = Cookie(None)):
    """Lista de campañas (templates) enviadas + cuántos destinatarios únicos."""
    _require_admin_dep(request, token=token, cmc_session=cmc_session)
    from session import db as _conn
    with _conn() as conn:
        rows = conn.execute(
            """SELECT m.text, m.phone FROM messages m
               WHERE m.direction='out' AND m.text LIKE '[template:%'
                 AND m.ts >= datetime('now', ?)""",
            [f"-{int(dias)} days"],
        ).fetchall()
    agg: dict[str, set] = {}
    for r in rows:
        mt = _TPL_RE.match(r["text"] or "")
        name = mt.group(1).strip() if mt else "?"
        agg.setdefault(name, set()).add(r["phone"])
    campanas = sorted(
        [{"template": k, "destinatarios": len(v)} for k, v in agg.items()],
        key=lambda x: -x["destinatarios"],
    )
    return {"campanas": campanas}


@router.get("/chat")
async def get_chat(request: Request,
                   phone: str = Query(...),
                   token: str | None = Query(None),
                   cmc_session: str | None = Cookie(None)):
    """Conversación completa (in/out) de un paciente, para abrir desde Envíos."""
    _require_admin_dep(request, token=token, cmc_session=cmc_session)
    ph = re.sub(r"[^0-9]", "", phone or "")
    if not ph:
        raise HTTPException(400, "phone inválido")
    from session import db as _conn
    with _conn() as conn:
        nombre_row = conn.execute(
            "SELECT nombre FROM contact_profiles WHERE phone = ? LIMIT 1", (ph,)
        ).fetchone()
        rows = conn.execute(
            """SELECT direction, text, ts FROM messages
               WHERE phone = ? ORDER BY ts ASC LIMIT 300""",
            (ph,),
        ).fetchall()
    msgs = [{"direction": r["direction"], "text": r["text"], "ts": r["ts"]}
            for r in (dict(x) for x in rows)]
    nombre = (dict(nombre_row).get("nombre") if nombre_row else "") or ""
    return {"phone": ph, "nombre": nombre, "mensajes": msgs}
