"""Estudio de Marketing — panel de publicidad y contenido del CMC.

Cabina para la creadora de contenido (no un BI): marca + fuente de verdad
(precios/profesionales/promos REALES), galería de creatividades, calendario
editorial, qué funciona y banco de copys. El generador arma el prompt de
ChatGPT con el precio REAL para que ninguna gráfica lleve datos errados.

Auth: token admin/olacore o cookie cmc_session.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from fastapi import Query, Cookie, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse


def _auth(token, cmc_session):
    from admin_routes import _verify_cookie, _is_admin_token
    if not ((token and _is_admin_token(token)) or (cmc_session and _verify_cookie(cmc_session))):
        raise HTTPException(403, "No autorizado")


_TPL = Path(__file__).parent.parent / "templates" / "marketing_estudio.html"

# contenido editable por ella (se guarda en marketing_kv); estos son los seeds
_DEFAULTS = {
    "promos": [
        {"titulo": "Nutricionista bono Fonasa", "detalle": "$4.770 con bono · particular $20.000", "vigente": True},
        {"titulo": "Ortodoncia", "detalle": "Instalación completa $120.000 · controles $30.000", "vigente": True},
        {"titulo": "Limpieza dental", "detalle": "Evaluación $15.000 · promo del mes", "vigente": True},
    ],
    "copys": [
        {"area": "Dental", "texto": "Tu sonrisa también es salud 🦷 Agenda tu evaluación dental en Carampangue. Escríbenos por WhatsApp.", "cta": "Agenda por WhatsApp"},
        {"area": "Medicina General", "texto": "¿Necesitas hora con médico general? Bono Fonasa desde $7.880. Te atendemos en Carampangue.", "cta": "Reserva tu hora"},
        {"area": "Nutrición", "texto": "Empieza a comer mejor con acompañamiento profesional. Nutricionista con bono Fonasa $4.770.", "cta": "Agenda hoy"},
    ],
    "hashtags": [
        "#CentroMedicoCarampangue", "#SaludArauco", "#Carampangue", "#Curanilahue",
        "#SaludDental", "#BonoFonasa", "#TuSaludPrimero",
    ],
    "calendario": [
        {"mes": 6, "titulo": "Mes del chequeo de invierno", "nota": "Influenza + respiratorio"},
        {"mes": 7, "titulo": "Salud dental de vacaciones", "nota": "Aprovecha las vacaciones para el dentista"},
    ],
    "cola_disenos": [],
}


def _ensure_kv(c):
    c.execute("CREATE TABLE IF NOT EXISTS marketing_kv (k TEXT PRIMARY KEY, v TEXT, updated_at TEXT)")


def _get_kv(c, k):
    r = c.execute("SELECT v FROM marketing_kv WHERE k=?", (k,)).fetchone()
    if r:
        try:
            return json.loads(r[0])
        except Exception:
            pass
    return _DEFAULTS.get(k)


def _set_kv(c, k, value):
    c.execute(
        "INSERT INTO marketing_kv (k, v, updated_at) VALUES (?,?,?) "
        "ON CONFLICT(k) DO UPDATE SET v=excluded.v, updated_at=excluded.updated_at",
        (k, json.dumps(value, ensure_ascii=False), datetime.now().isoformat(timespec="seconds")))


def _precios_list():
    from flows import PRECIOS_SLOT
    out = []
    for esp, t in PRECIOS_SLOT.items():
        if t and t[0] == "ambas":
            out.append({"esp": esp, "fonasa": t[1], "particular": (t[3] if len(t) > 3 else None), "nota": ""})
        else:
            nota = t[2] if (len(t) > 2 and isinstance(t[2], str)) else ""
            out.append({"esp": esp, "fonasa": None, "particular": t[1], "nota": nota})
    return out


def register_marketing_routes(app):

    @app.get("/marketing", response_class=HTMLResponse, include_in_schema=False)
    def marketing_page(token: str | None = Query(None), cmc_session: str | None = Cookie(None)):
        _auth(token, cmc_session)
        html = _TPL.read_text(encoding="utf-8") if _TPL.exists() else "<h1>Falta el template</h1>"
        html = html.replace("__MKT_TOKEN__", token or "")
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    @app.get("/api/marketing/data", tags=["marketing"], include_in_schema=False)
    def marketing_data(token: str | None = Query(None), cmc_session: str | None = Cookie(None)):
        _auth(token, cmc_session)
        from session import db
        from medilink import PROFESIONALES
        from fidelizacion import CAMPANAS_ESTACIONALES as C
        try:
            from autopilot.designs import load_designs
            disenos = load_designs()
        except Exception:
            disenos = []
        profs = [{"id": i, "nombre": v.get("nombre"), "esp": v.get("especialidad")}
                 for i, v in PROFESIONALES.items()]
        camps = [{"id": k, "nombre": v.get("nombre"), "temporada": v.get("temporada"),
                  "icono": v.get("icono"), "meses": v.get("meses_sugeridos"),
                  "descripcion": v.get("descripcion"), "mensaje": v.get("mensaje")}
                 for k, v in C.items()]
        with db() as c:
            _ensure_kv(c)
            kv = {k: _get_kv(c, k) for k in ("promos", "copys", "hashtags", "calendario")}
        return {"profesionales": profs, "precios": _precios_list(),
                "campanas": camps, "disenos": disenos, "kv": kv}

    @app.post("/api/marketing/kv", tags=["marketing"], include_in_schema=False)
    async def marketing_kv_save(request: Request, k: str = Query(...),
                                token: str | None = Query(None), cmc_session: str | None = Cookie(None)):
        _auth(token, cmc_session)
        if k not in ("promos", "copys", "hashtags", "calendario"):
            raise HTTPException(400, "clave no permitida")
        body = await request.json()
        from session import db
        with db() as c:
            _ensure_kv(c)
            _set_kv(c, k, body)
        return {"ok": True}

    @app.post("/api/marketing/design-request", tags=["marketing"], include_in_schema=False)
    async def marketing_design_request(request: Request,
                                       token: str | None = Query(None), cmc_session: str | None = Cookie(None)):
        _auth(token, cmc_session)
        body = await request.json()
        body["_ts"] = datetime.now().isoformat(timespec="seconds")
        body["status"] = "pendiente"
        from session import db
        with db() as c:
            _ensure_kv(c)
            cola = _get_kv(c, "cola_disenos") or []
            cola.insert(0, body)
            _set_kv(c, "cola_disenos", cola[:50])
        return {"ok": True, "en_cola": len(cola)}

    @app.get("/api/marketing/design-queue", tags=["marketing"], include_in_schema=False)
    def marketing_design_queue(token: str | None = Query(None), cmc_session: str | None = Cookie(None)):
        """Cola de pedidos de diseño — para que el alma-image-runner la consuma."""
        _auth(token, cmc_session)
        from session import db
        with db() as c:
            _ensure_kv(c)
            cola = _get_kv(c, "cola_disenos") or []
        return JSONResponse({"cola": cola})
