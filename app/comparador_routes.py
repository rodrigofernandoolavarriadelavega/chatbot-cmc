"""Comparador BI — módulo del shell Alma (key "comparador").

Compara recaudación de caja (bi_pagos_caja) entre N rangos de fechas arbitrarios,
desglosado por área o por profesional. Cada "columna" del front es un rango libre.
La serie diaria incluye el día de semana (dow) para que el gráfico pueda alinear
por día hábil (primer lunes con primer lunes) en vez de día calendario.

Misma fuente que DB Mensual (/cmc/mensual): bi_pagos_caja, caja real Medilink.
Solo lectura.
"""

from pathlib import Path
from datetime import date, timedelta
from fastapi import HTTPException, Query, Cookie
from fastapi.responses import HTMLResponse

_TEMPLATE_DIR = Path(__file__).parent.parent / "templates"

# Mismo mapa que api_cmc_mensual en main.py — un profesional → un área.
AREA_MAP = {
    1: "med", 73: "med", 13: "med", 23: "med", 60: "med", 61: "med",
    65: "med", 64: "med",
    68: "tecmed",
    55: "dent", 72: "dent", 66: "dent", 75: "dent", 69: "dent", 76: "dent",
    59: "maso", 77: "kine", 21: "kine",
    52: "nutri", 74: "psico", 49: "psico", 70: "fono",
    67: "matrona", 56: "podo",
}
AREA_LABELS = {
    "med": "Medicina", "dent": "Dental", "kine": "Kinesiología",
    "maso": "Masoterapia", "nutri": "Nutrición", "psico": "Psicología",
    "fono": "Fonoaudiología", "matrona": "Matrona", "podo": "Podología",
    "tecmed": "Ecografía", "otros": "Otros", "sin_asignar": "Sin asignar",
}


def _datos_rango(desde: str, hasta: str, por: str) -> dict:
    """Agrega bi_pagos_caja en [desde, hasta] (ambos inclusive) por área o
    profesional, y devuelve también la serie diaria con día de semana."""
    from session import db
    from medilink import PROFESIONALES
    # validar y normalizar fechas (hasta inclusive → +1 día para el < SQL)
    d_desde = date.fromisoformat(desde)
    d_hasta = date.fromisoformat(hasta)
    fin_excl = (d_hasta + timedelta(days=1)).isoformat()

    with db() as c:
        rows = c.execute(
            "SELECT id_profesional, SUM(monto) AS total, "
            "       COUNT(*) AS n, COUNT(DISTINCT id_paciente) AS pac "
            "FROM bi_pagos_caja WHERE fecha>=? AND fecha<? "
            "GROUP BY id_profesional",
            (desde, fin_excl),
        ).fetchall()
        serie_rows = c.execute(
            "SELECT fecha, SUM(monto) AS total, COUNT(DISTINCT id_paciente) AS pac "
            "FROM bi_pagos_caja WHERE fecha>=? AND fecha<? "
            "GROUP BY fecha ORDER BY fecha",
            (desde, fin_excl),
        ).fetchall()

    grupos: dict = {}
    total = 0
    pac_total = 0
    n_total = 0
    for r in rows:
        pid = r["id_profesional"]
        t = int(r["total"] or 0)
        total += t
        n_total += r["n"]
        if por == "prof":
            if pid is None:
                key, label, esp = "sin_asignar", "Sin asignar", "(sin cruce)"
            else:
                info = PROFESIONALES.get(pid, {})
                key = str(pid)
                label = info.get("nombre") or f"Prof {pid}"
                esp = info.get("especialidad", "") or ""
        else:
            key = AREA_MAP.get(pid, "sin_asignar" if pid is None else "otros")
            label = AREA_LABELS.get(key, key)
            esp = ""
        g = grupos.setdefault(key, {"key": key, "label": label, "esp": esp,
                                    "total": 0, "pacientes": 0, "n": 0})
        g["total"] += t
        g["pacientes"] += r["pac"] or 0
        g["n"] += r["n"]

    # serie diaria con día de semana (0=lunes … 6=domingo)
    serie = []
    for r in serie_rows:
        f = r["fecha"]
        try:
            dow = date.fromisoformat(f[:10]).weekday()
        except Exception:
            dow = None
        serie.append({"fecha": f, "dow": dow,
                      "total": int(r["total"] or 0), "pacientes": r["pac"] or 0})
        pac_total += r["pac"] or 0

    dias_activos = len([s for s in serie if s["total"] > 0])
    return {
        "desde": desde, "hasta": hasta, "por": por,
        "total": total, "n_pagos": n_total,
        "dias_activos": dias_activos,
        "grupos": sorted(grupos.values(), key=lambda g: -g["total"]),
        "serie": serie,
    }


def register_comparador_routes(app):
    """Registra el módulo Comparador. Llamar desde main.py:
    import comparador_routes; comparador_routes.register_comparador_routes(app)"""

    @app.get("/api/cmc/comparador", tags=["bi"])
    def api_comparador(desde: str = Query(...), hasta: str = Query(...),
                       por: str = Query("area")):
        if por not in ("area", "prof"):
            por = "area"
        try:
            return _datos_rango(desde, hasta, por)
        except ValueError:
            raise HTTPException(400, "fechas inválidas (usar YYYY-MM-DD)")

    @app.get("/cmc/comparador", response_class=HTMLResponse, tags=["alma"])
    def cmc_comparador_page(token: str | None = Query(None),
                            cmc_session: str | None = Cookie(None)):
        from admin_routes import _verify_cookie, _is_admin_token
        ok = (token and _is_admin_token(token)) or _verify_cookie(cmc_session)
        if not ok:
            raise HTTPException(403, "No autorizado")
        p = _TEMPLATE_DIR / "cmc_comparador.html"
        if not p.exists():
            raise HTTPException(404, "template no encontrado")
        return HTMLResponse(p.read_text(encoding="utf-8"))
