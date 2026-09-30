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
# Comisión Transbank por medio de pago (se calcula sola cruzando caja × medio del
# módulo Pagos). Hoy la recepción registra el medio en pocos pagos → la comisión
# sale baja; cuando se complete el medio en todos, queda exacta automáticamente.
TRANSBANK_DEBITO = 0.006
TRANSBANK_CREDITO = 0.013

# Publicidad Meta Ads: se lee del gasto real de la cuenta publicitaria (no se
# carga a mano). La cuenta la comparten otros negocios de la familia: esas
# campañas no son gasto del CMC y se excluyen por nombre.
_META_NO_CMC = ("meulen", "terremoto", "brasas", "don pancho")
_META_CACHE: dict = {}


def _norm_nom(s: str) -> str:
    import re
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _comision_transbank(c, mes: str):
    """Comisión Transbank del mes = débito×0,6% + crédito×1,3%, donde el monto
    por medio se obtiene cruzando bi_pagos_caja (monto real) con el medio de pago
    de pagos_cmc (por paciente+fecha). Devuelve (comision, monto_debito, monto_credito)."""
    inicio, fin = _mes_bounds(mes)
    metodos = {}
    for fe, nom, met in c.execute(
        "SELECT fecha, paciente_nombre, metodo_pago FROM pagos_cmc WHERE fecha>=? AND fecha<?",
        (inicio, fin),
    ).fetchall():
        metodos[(fe, _norm_nom(nom))] = (met or "").strip().lower()
    nombres = {r["id_paciente"]: r["paciente_nombre"] for r in c.execute(
        "SELECT DISTINCT id_paciente, paciente_nombre FROM bi_atenciones WHERE fecha>=? AND fecha<?",
        (inicio, fin),
    ).fetchall()}
    deb = cred = 0
    for r in c.execute(
        "SELECT monto, fecha, id_paciente FROM bi_pagos_caja WHERE fecha>=? AND fecha<?",
        (inicio, fin),
    ).fetchall():
        nom = nombres.get(r["id_paciente"])
        met = metodos.get((r["fecha"], _norm_nom(nom))) if nom else None
        if met == "debito":
            deb += r["monto"] or 0
        elif met in ("credito", "crédito"):
            cred += r["monto"] or 0
    com = round(deb * TRANSBANK_DEBITO + cred * TRANSBANK_CREDITO)
    return com, deb, cred
def _gasto_meta(mes: str):
    """Gasto de Meta Ads del mes (CLP) → (monto, n_campañas, excluidas) o None si
    Meta no respondió. Caché: 1 h el mes en curso, 7 días los meses cerrados."""
    import time
    import json as _json
    import httpx
    import config
    ahora = time.time()
    abierto = mes >= date.today().strftime("%Y-%m")
    hit = _META_CACHE.get(mes)
    if hit and ahora - hit[0] < (3600 if abierto else 7 * 86400):
        return hit[1]
    token = getattr(config, "META_ACCESS_TOKEN", "")
    acct = getattr(config, "META_AD_ACCOUNT_ID", "") or "act_220608142267129"
    if not acct.startswith("act_"):
        acct = f"act_{acct}"
    if not token:
        return None
    inicio, fin = _mes_bounds(mes)
    hasta = min(date.fromisoformat(fin).toordinal() - 1, date.today().toordinal())
    hasta = date.fromordinal(hasta).isoformat()
    try:
        # Token en header, nunca en la URL (httpx loggea la URL completa).
        r = httpx.get(f"https://graph.facebook.com/v21.0/{acct}/insights",
                      params={"fields": "campaign_name,spend", "level": "campaign",
                              "time_range": _json.dumps({"since": inicio, "until": hasta}),
                              "limit": 500},
                      headers={"Authorization": f"Bearer {token}"}, timeout=20)
        data = r.json()
    except Exception:
        return None
    if not isinstance(data, dict) or data.get("error"):
        return None
    total, n, excl = 0.0, 0, []
    for c in data.get("data", []):
        gasto = float(c.get("spend") or 0)
        if gasto <= 0:
            continue
        nombre = (c.get("campaign_name") or "").lower()
        if any(k in nombre for k in _META_NO_CMC):
            excl.append((c.get("campaign_name") or "")[:40])
            continue
        total += gasto
        n += 1
    res = (round(total), n, excl)
    _META_CACHE[mes] = (ahora, res)
    return res

# Honorario FIJO mensual (no % del ingreso). Único contrato fijo: Dr. Abarca (id 73).
# Su CMC = ingreso − fijo puede ser negativo (riesgo del centro). El fijo cambió:
# hasta abril 2026 era $3.414.126; desde mayo 2026 es la mitad ($1.707.063).
def honorario_fijo(pid: int, mes: str):
    if pid == 73:  # Dr. Abarca contrato
        # Desde el 7-sep-2026: fijo $3.414.126 por 40 h en la tarde (decisión
        # del dueño 31-ago). Septiembre es mixto: fijo prorrateado 24/30 más el
        # 62% de su venta del 1 al 6 bajo el esquema anterior ($442.730).
        if mes >= "2026-10":
            return 3414126
        if mes == "2026-09":
            return round(3414126 * 24 / 30 + 442730 * 0.62)
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
    # Comisión Transbank automática (calculada del cruce caja × medio de pago)
    # Publicidad Meta automática, salvo que ya se haya cargado a mano.
    if not any("public" in (g.get("categoria") or "").lower() or
               "meta" in (g.get("categoria") or "").lower() for g in gastos_detalle):
        _meta = _gasto_meta(mes)
        if _meta and _meta[0] > 0:
            gastos += _meta[0]
            gastos_detalle.append({
                "id": None, "categoria": "Publicidad Meta Ads",
                "descripcion": f"Auto · gasto real de {_meta[1]} campañas en Meta"
                               + (f" (excluye {len(_meta[2])} de otros negocios)" if _meta[2] else ""),
                "monto": _meta[0], "recurrente": 0, "auto": True,
            })
    com_tbk, tbk_deb, tbk_cred = _comision_transbank(c, mes)
    if com_tbk > 0:
        gastos += com_tbk
        gastos_detalle.append({
            "id": None, "categoria": "Comisión Transbank",
            "descripcion": f"Auto · débito ${tbk_deb:,}×0,6% + crédito ${tbk_cred:,}×1,3%",
            "monto": com_tbk, "recurrente": 0, "auto": True,
        })
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
        """Solo dueño/recepción. Antes bastaba CUALQUIER cookie válida — con eso
        el perfil dental abría el EBITDA del centro entero (hallado 2026-09-02)."""
        from admin_routes import _verify_cookie, _is_admin_token
        if token and _is_admin_token(token):
            return
        if _verify_cookie(cmc_session) in ("admin", "administracion"):
            return
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
