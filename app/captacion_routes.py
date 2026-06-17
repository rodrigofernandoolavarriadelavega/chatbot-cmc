"""Dashboard de Captación — cómo se enteran del centro los pacientes nuevos.

Cruza la pregunta "¿Cómo nos conociste?" (registro_referral_post → tags
referido:*) con la atribución automática de anuncios (meta_referral_capturado,
CTWA) y la especialidad de la primera cita. Solo lectura.

Auth: token admin/olacore o cookie.
"""
from __future__ import annotations

import json
import collections
from datetime import datetime
from pathlib import Path

from fastapi import Query, Cookie, HTTPException
from fastapi.responses import HTMLResponse


def _auth(token, cmc_session):
    from admin_routes import _verify_cookie, _is_admin_token
    if not ((token and _is_admin_token(token)) or (cmc_session and _verify_cookie(cmc_session))):
        raise HTTPException(403, "No autorizado")


_TPL = Path(__file__).parent.parent / "templates" / "captacion_dashboard.html"
_FUENTE_LABEL = {"amigo": "Amigo / familiar", "rrss": "Redes sociales",
                 "recurrente": "Ya era paciente", "google": "Google / web"}


def _data(c) -> dict:
    # fuentes (todo el historial, phones distintos)
    fuentes = {}
    for tag, n in c.execute(
        "SELECT tag, COUNT(DISTINCT phone) FROM contact_tags WHERE tag LIKE 'referido:%' GROUP BY tag").fetchall():
        fuentes[tag.split(":")[1]] = n

    # tendencia últimos 6 meses: pregunta respondida + ads (CTWA)
    trend = {}
    for mes, n in c.execute(
        "SELECT substr(ts,1,7), COUNT(*) FROM conversation_events "
        "WHERE event='registro_referral_post' AND ts>=date('now','-6 month') GROUP BY 1").fetchall():
        trend.setdefault(mes, {"resp": 0, "ads": 0})["resp"] = n
    for mes, n in c.execute(
        "SELECT substr(ts,1,7), COUNT(*) FROM conversation_events "
        "WHERE event='meta_referral_capturado' AND ts>=date('now','-6 month') GROUP BY 1").fetchall():
        trend.setdefault(mes, {"resp": 0, "ads": 0})["ads"] = n
    trend_list = [{"mes": m, **v} for m, v in sorted(trend.items())]

    # tasa de respuesta (90d)
    def _cnt(ev):
        return c.execute("SELECT COUNT(*) FROM conversation_events WHERE event=? AND ts>=date('now','-90 day')",
                         (ev,)).fetchone()[0]
    completos = _cnt("registro_completo")
    resp90 = _cnt("registro_referral_post")
    skip90 = _cnt("registro_skip")
    ads90 = _cnt("meta_referral_capturado")

    # fuente × especialidad (primera cita por phone)
    src = {}
    for ph, tag in c.execute("SELECT phone, tag FROM contact_tags WHERE tag LIKE 'referido:%'").fetchall():
        src[ph] = tag.split(":")[1]
    esp_by_phone = {}
    for m, ph in c.execute("SELECT meta, phone FROM conversation_events WHERE event='cita_creada'").fetchall():
        try:
            d = json.loads(m) if m else {}
        except Exception:
            d = {}
        e = (d.get("especialidad") or "").strip()
        if e and ph not in esp_by_phone:
            esp_by_phone[ph] = e
    by_src = collections.defaultdict(collections.Counter)
    for ph, s in src.items():
        e = esp_by_phone.get(ph)
        if e:
            by_src[s][e] += 1
    cross = {s: [{"esp": e, "n": n} for e, n in cnt.most_common(6)] for s, cnt in by_src.items()}

    return {
        "fuentes": [{"key": k, "label": _FUENTE_LABEL.get(k, k.title()), "n": v}
                    for k, v in sorted(fuentes.items(), key=lambda x: -x[1])],
        "total_reportado": sum(fuentes.values()),
        "trend": trend_list,
        "tasa": {"completos": completos, "respondieron": resp90, "skip": skip90,
                 "preguntados": resp90 + skip90,
                 "pct_de_registros": round(resp90 / completos * 100) if completos else 0,
                 "pct_de_preguntados": round(resp90 / (resp90 + skip90) * 100) if (resp90 + skip90) else 0},
        "ads_90d": ads90,
        "cross": cross,
        "actualizado": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


def register_captacion_routes(app):

    @app.get("/captacion", response_class=HTMLResponse, include_in_schema=False)
    def captacion_page(token: str | None = Query(None), cmc_session: str | None = Cookie(None)):
        _auth(token, cmc_session)
        html = _TPL.read_text(encoding="utf-8") if _TPL.exists() else "<h1>Falta el template</h1>"
        html = html.replace("__CAP_TOKEN__", token or "")
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    @app.get("/api/captacion/data", tags=["captacion"], include_in_schema=False)
    def captacion_data(token: str | None = Query(None), cmc_session: str | None = Cookie(None)):
        _auth(token, cmc_session)
        from session import db
        with db() as c:
            return _data(c)
