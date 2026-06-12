"""Módulo EBITDA / Resultado — shell Alma (key "ebitda").

EBITDA = Ingresos − Honorarios profesionales − Gastos operativos
(antes de intereses, impuestos, depreciación y amortización).

Fuentes:
- Ingresos: bi_pagos_caja (caja real Medilink, ya cuadrada por profesional).
- Honorarios: % por profesional desde equipo_cmc.pct_honorario (o monto fijo de
  contrato). El honorario BRUTO es el costo del CMC; el líquido al profesional es
  bruto × (1 − 0.1525) por la retención de boleta de honorarios (15,25%).
- Gastos operativos: egresos_cmc (arriendo, sueldos, servicios, insumos…), con
  flag `recurrente` para gastos fijos que aplican todos los meses.

Solo lectura del cálculo; los gastos se editan vía endpoints POST/DELETE.
"""

from pathlib import Path
from datetime import date
from fastapi import HTTPException, Query, Cookie, Body
from fastapi.responses import HTMLResponse

_TEMPLATE_DIR = Path(__file__).parent.parent / "templates"

RETENCION_BOLETA = 0.1525   # retención SII boleta de honorarios (Chile)
PCT_DEFAULT = 70            # % honorario si el profesional no está en equipo_cmc
# Profesionales que FACTURAN (empresa) en vez de emitir boleta de honorarios:
# no se les aplica la retención 15,25% → su líquido = honorario bruto.
SIN_RETENCION = {65, 68, 73}    # Quijano (gastro), David Pardo (ecografía), Abarca (fijo) — facturan
# Honorario FIJO mensual (no % del ingreso). Único contrato fijo: Dr. Abarca (id 73).
# Su CMC = ingreso − fijo puede ser negativo (riesgo del centro). El fijo cambió:
# hasta abril 2026 era $3.414.126; desde mayo 2026 es la mitad ($1.707.063).
def honorario_fijo(pid: int, mes: str):
    if pid == 73:  # Dr. Abarca contrato
        return 1707063 if mes >= "2026-05" else 3414126
    return None


def _mes_bounds(mes: str):
    yr, mo = int(mes[:4]), int(mes[5:7])
    inicio = f"{mes}-01"
    fin_y = yr + (mo // 12); fin_mo = (mo % 12) + 1
    fin = f"{fin_y}-{fin_mo:02d}-01"
    return inicio, fin


def _gastos_mes(c, mes: str) -> tuple[int, list]:
    """Gastos del mes = puntuales con fecha en el mes + recurrentes activos
    (recurrente=1, dados de alta en o antes del mes)."""
    inicio, fin = _mes_bounds(mes)
    detalle = []
    total = 0
    # puntuales del mes
    for r in c.execute(
        "SELECT id, fecha, categoria, descripcion, monto, recurrente, proveedor "
        "FROM egresos_cmc WHERE recurrente=0 AND fecha>=? AND fecha<? ORDER BY monto DESC",
        (inicio, fin),
    ).fetchall():
        detalle.append(dict(r)); total += int(r["monto"] or 0)
    # recurrentes (aplican a cada mes desde su fecha de alta)
    for r in c.execute(
        "SELECT id, fecha, categoria, descripcion, monto, recurrente, proveedor "
        "FROM egresos_cmc WHERE recurrente=1 AND substr(fecha,1,7)<=? ORDER BY monto DESC",
        (mes,),
    ).fetchall():
        detalle.append(dict(r)); total += int(r["monto"] or 0)
    return total, detalle


def _ebitda_mes(c, mes: str) -> dict:
    from medilink import PROFESIONALES
    inicio, fin = _mes_bounds(mes)

    # ingresos por profesional (caja real)
    rows = c.execute(
        "SELECT id_profesional, SUM(monto) AS ingreso, COUNT(DISTINCT id_paciente) AS pac "
        "FROM bi_pagos_caja WHERE fecha>=? AND fecha<? AND id_profesional IS NOT NULL "
        "GROUP BY id_profesional ORDER BY 2 DESC",
        (inicio, fin),
    ).fetchall()
    # mapa pct por id_medilink
    pct_map = {}
    fijo_map = {}
    for r in c.execute("SELECT id_medilink, pct_honorario, tipo_contrato FROM equipo_cmc").fetchall():
        if r["id_medilink"] is not None:
            pct_map[r["id_medilink"]] = r["pct_honorario"] or 0

    profs = []
    tot_ing = tot_bruto = tot_liq = tot_cmc = 0
    for r in rows:
        pid = r["id_profesional"]
        ingreso = int(r["ingreso"] or 0)
        info = PROFESIONALES.get(pid, {})
        nombre = info.get("nombre") or f"Prof {pid}"
        _fijo = honorario_fijo(pid, mes)
        if _fijo is not None:
            pct = None
            bruto = _fijo
        else:
            pct = pct_map.get(pid, PCT_DEFAULT)
            bruto = round(ingreso * pct / 100)
        if pid in SIN_RETENCION:
            liquido = bruto          # factura: sin retención
        else:
            liquido = round(bruto * (1 - RETENCION_BOLETA))
        retencion = bruto - liquido
        cmc = ingreso - bruto
        profs.append({
            "id": pid, "nombre": nombre, "especialidad": info.get("especialidad", ""),
            "ingreso": ingreso, "pct": pct, "fijo": _fijo is not None, "bruto": bruto,
            "liquido": liquido, "retencion": retencion, "cmc": cmc,
            "pacientes": r["pac"],
        })
        tot_ing += ingreso; tot_bruto += bruto; tot_liq += liquido; tot_cmc += cmc

    gastos, gastos_detalle = _gastos_mes(c, mes)
    ebitda = tot_cmc - gastos
    return {
        "mes": mes,
        "ingresos": tot_ing,
        "honorarios_bruto": tot_bruto,
        "retencion_total": tot_bruto - tot_liq,
        "liquido_total": tot_liq,
        "margen_cmc": tot_cmc,
        "gastos": gastos,
        "ebitda": ebitda,
        "margen_ebitda_pct": round(ebitda / tot_ing * 100, 1) if tot_ing else 0,
        "margen_cmc_pct": round(tot_cmc / tot_ing * 100, 1) if tot_ing else 0,
        "profesionales": profs,
        "gastos_detalle": gastos_detalle,
    }


def register_ebitda_routes(app):
    """Registra el módulo EBITDA. Llamar desde main.py."""

    def _auth(token, cmc_session):
        from admin_routes import _verify_cookie, _is_admin_token
        if not ((token and _is_admin_token(token)) or _verify_cookie(cmc_session)):
            raise HTTPException(403, "No autorizado")

    @app.get("/api/cmc/ebitda", tags=["bi"])
    def api_ebitda(mes: str | None = Query(None), token: str | None = Query(None),
                   cmc_session: str | None = Cookie(None)):
        _auth(token, cmc_session)
        from session import db
        if not mes:
            mes = date.today().strftime("%Y-%m")
        with db() as c:
            data = _ebitda_mes(c, mes)
            # evolución: meses disponibles + EBITDA de cada uno
            meses = [r[0] for r in c.execute(
                "SELECT DISTINCT substr(fecha,1,7) m FROM bi_pagos_caja "
                "WHERE fecha>='2024-01-01' ORDER BY m DESC"
            ).fetchall()]
            evol = []
            for m in sorted(meses)[-12:]:
                e = _ebitda_mes(c, m)
                evol.append({"mes": m, "ingresos": e["ingresos"], "honorarios": e["honorarios_bruto"],
                             "gastos": e["gastos"], "ebitda": e["ebitda"]})
        data["meses_disponibles"] = meses
        data["evolucion"] = evol
        return data

    @app.post("/api/cmc/ebitda/gasto", tags=["bi"])
    def api_gasto_add(payload: dict = Body(...), token: str | None = Query(None),
                      cmc_session: str | None = Cookie(None)):
        _auth(token, cmc_session)
        from session import db
        try:
            from finanzas_routes import ensure_table as _ensure_egresos
            _ensure_egresos()
        except Exception:
            pass
        fecha = (payload.get("fecha") or date.today().isoformat())[:10]
        cat = (payload.get("categoria") or "Otros").strip()
        desc = (payload.get("descripcion") or "").strip()
        monto = int(payload.get("monto") or 0)
        rec = 1 if payload.get("recurrente") else 0
        prov = (payload.get("proveedor") or "").strip()
        if monto <= 0:
            raise HTTPException(400, "monto debe ser > 0")
        with db() as c:
            c.execute(
                "INSERT INTO egresos_cmc (fecha, categoria, descripcion, monto, recurrente, proveedor, creado_por) "
                "VALUES (?,?,?,?,?,?, 'ebitda')",
                (fecha, cat, desc, monto, rec, prov),
            )
            c.commit()
            gid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        return {"ok": True, "id": gid}

    @app.delete("/api/cmc/ebitda/gasto/{gid}", tags=["bi"])
    def api_gasto_del(gid: int, token: str | None = Query(None),
                      cmc_session: str | None = Cookie(None)):
        _auth(token, cmc_session)
        from session import db
        with db() as c:
            c.execute("DELETE FROM egresos_cmc WHERE id=?", (gid,))
            c.commit()
        return {"ok": True}

    @app.get("/cmc/ebitda", response_class=HTMLResponse, tags=["alma"])
    def cmc_ebitda_page(token: str | None = Query(None), cmc_session: str | None = Cookie(None)):
        _auth(token, cmc_session)
        p = _TEMPLATE_DIR / "cmc_ebitda.html"
        if not p.exists():
            raise HTTPException(404, "template no encontrado")
        return HTMLResponse(p.read_text(encoding="utf-8"))
