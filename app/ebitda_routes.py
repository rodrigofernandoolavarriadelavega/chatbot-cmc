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
_TBK_DIA: dict = {}      # mes → {fecha: comisión} (lo deja _comision_transbank, sin costo extra)

# Publicidad Meta Ads: se lee del gasto real de la cuenta publicitaria (no se
# carga a mano). La cuenta la comparten otros negocios de la familia: esas
# campañas no son gasto del CMC y se excluyen por nombre.
_META_NO_CMC = ("meulen", "terremoto", "brasas", "don pancho")
_META_CACHE: dict = {}
_META_DET: dict = {}     # mes → [{campana, monto, cmc}] (mismo llamado en vivo, sin costo extra)

# Lectores que NO pueden salir a la red (Alma Radar): con esta marca el gasto de
# Meta sale de la caché en memoria (si está fresca) o de la foto diaria local
# `meta_insights_diario`, con la MISMA exclusión de campañas de otros negocios.
# Sin la marca, todo sigue igual que antes (consulta en vivo con caché).
import contextvars as _cv
from contextlib import contextmanager as _ctx

_SOLO_LOCAL = _cv.ContextVar("ebitda_solo_local", default=False)


@_ctx
def solo_datos_locales():
    """`with solo_datos_locales(): _ebitda_mes(c, mes)` — sin llamadas a Meta."""
    tok = _SOLO_LOCAL.set(True)
    try:
        yield
    finally:
        _SOLO_LOCAL.reset(tok)


def _gasto_meta_local(c, mes: str):
    """Mismo contrato que `_gasto_meta` → (monto, n_campañas, excluidas, fuente)
    sin red: caché en memoria si está vigente; si no, la foto diaria."""
    import time
    hit = _META_CACHE.get(mes)
    abierto = mes >= date.today().strftime("%Y-%m")
    if hit and time.time() - hit[0] < (3600 if abierto else 7 * 86400):
        return (*hit[1], "meta_cache")
    inicio, fin = _mes_bounds(mes)
    try:
        filas = c.execute(
            "SELECT campaign_name, SUM(spend) FROM meta_insights_diario WHERE desglose='total' "
            "AND fecha>=? AND fecha<? GROUP BY campaign_id, campaign_name", (inicio, fin)).fetchall()
    except Exception:
        return None
    total, n, excl = 0.0, 0, []
    for nombre_, gasto in filas:
        gasto = float(gasto or 0)
        if gasto <= 0:
            continue
        if any(k in (nombre_ or "").lower() for k in _META_NO_CMC):
            excl.append((nombre_ or "")[:40])
            continue
        total += gasto
        n += 1
    return (round(total), n, excl, "foto_diaria")


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
    por_dia: dict = {}
    for r in c.execute(
        "SELECT monto, fecha, id_paciente FROM bi_pagos_caja WHERE fecha>=? AND fecha<?",
        (inicio, fin),
    ).fetchall():
        nom = nombres.get(r["id_paciente"])
        met = metodos.get((r["fecha"], _norm_nom(nom))) if nom else None
        if met == "debito":
            deb += r["monto"] or 0
            por_dia[r["fecha"]] = por_dia.get(r["fecha"], 0) + (r["monto"] or 0) * TRANSBANK_DEBITO
        elif met in ("credito", "crédito"):
            cred += r["monto"] or 0
            por_dia[r["fecha"]] = por_dia.get(r["fecha"], 0) + (r["monto"] or 0) * TRANSBANK_CREDITO
    com = round(deb * TRANSBANK_DEBITO + cred * TRANSBANK_CREDITO)
    _TBK_DIA[mes] = por_dia
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
    det = []
    for c in data.get("data", []):
        gasto = float(c.get("spend") or 0)
        if gasto <= 0:
            continue
        nombre = (c.get("campaign_name") or "").lower()
        if any(k in nombre for k in _META_NO_CMC):
            excl.append((c.get("campaign_name") or "")[:40])
            det.append({"campana": c.get("campaign_name") or "", "monto": round(gasto), "cmc": False})
            continue
        total += gasto
        n += 1
        det.append({"campana": c.get("campaign_name") or "", "monto": round(gasto), "cmc": True})
    res = (round(total), n, excl)
    _META_CACHE[mes] = (ahora, res)
    _META_DET[mes] = sorted(det, key=lambda x: -x["monto"])
    return res

# WhatsApp Business: desde el 1-oct-2026 Meta cobra también los mensajes de
# servicio (respuestas del bot y de recepción). El costo real sale de
# `pricing_analytics` de la WABA (en USD) y se pasa a CLP con el dólar observado.
_WA_CACHE: dict = {}
_WA_DET: dict = {}       # mes → {dolar, por_dia_usd, volumen} (mismo llamado DAILY)
_USD_CACHE: dict = {}
USD_CLP_RESPALDO = 950      # si mindicador.cl no responde


def _dolar_clp() -> float:
    import time
    import httpx
    hit = _USD_CACHE.get("v")
    if hit and time.time() - hit[0] < 12 * 3600:
        return hit[1]
    try:
        v = float(httpx.get("https://mindicador.cl/api/dolar", timeout=10).json()["serie"][0]["valor"])
    except Exception:
        return hit[1] if hit else USD_CLP_RESPALDO
    _USD_CACHE["v"] = (time.time(), v)
    return v


def _fecha_clt_de_ts(ts: int) -> str:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime.fromtimestamp(ts, ZoneInfo("America/Santiago")).date().isoformat()


def _gasto_whatsapp(mes: str):
    """Costo WhatsApp del mes → (monto_clp, usd, {categoría: usd}) o None si Meta
    no respondió. Misma caché que Meta Ads; en modo solo-local, solo la caché."""
    import time
    import json as _json
    from datetime import datetime, timezone
    import httpx
    import config
    ahora = time.time()
    abierto = mes >= date.today().strftime("%Y-%m")
    hit = _WA_CACHE.get(mes)
    if hit and (_SOLO_LOCAL.get() or ahora - hit[0] < (3600 if abierto else 7 * 86400)):
        return hit[1]
    if _SOLO_LOCAL.get():
        return None
    token = getattr(config, "META_ACCESS_TOKEN", "")
    waba = getattr(config, "META_WABA_ID", "")
    if not token or not waba:
        return None
    inicio, fin = _mes_bounds(mes)
    ts = lambda s: int(datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp())
    campo = (f"pricing_analytics.start({ts(inicio)}).end({ts(fin)})"
             f".granularity(DAILY).dimensions({_json.dumps(['PRICING_CATEGORY'])})")
    try:
        r = httpx.get(f"https://graph.facebook.com/v21.0/{waba}", params={"fields": campo},
                      headers={"Authorization": f"Bearer {token}"}, timeout=20)
        data = r.json()
    except Exception:
        return None
    if not isinstance(data, dict) or data.get("error"):
        return None
    por_cat: dict = {}
    por_dia: dict = {}
    vol_cat: dict = {}
    for blk in (data.get("pricing_analytics") or {}).get("data", []):
        for p in blk.get("data_points", []):
            k = p.get("pricing_category") or "OTRO"
            costo = float(p.get("cost") or 0)
            por_cat[k] = por_cat.get(k, 0.0) + costo
            vol_cat[k] = vol_cat.get(k, 0) + int(p.get("volume") or 0)
            try:
                dia = _fecha_clt_de_ts(int(p.get("start")))
                por_dia[dia] = por_dia.get(dia, 0.0) + costo
            except Exception:
                pass
    usd = sum(por_cat.values())
    dolar = _dolar_clp()
    res = (round(usd * dolar), round(usd, 2), {k: round(v, 2) for k, v in por_cat.items()})
    _WA_CACHE[mes] = (ahora, res)
    _WA_DET[mes] = {"dolar": round(dolar, 2), "por_dia_usd": {k: round(v, 4) for k, v in por_dia.items()},
                    "volumen": vol_cat}
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


def _pct_map(c) -> dict:
    """% de honorario por id_medilink (equipo_cmc)."""
    out = {}
    for r in c.execute("SELECT id_medilink, pct_honorario FROM equipo_cmc").fetchall():
        if r["id_medilink"] is not None:
            out[r["id_medilink"]] = r["pct_honorario"] or 0
    return out


def _regla_honorario(pid, mes: str, pct_map: dict):
    """Regla ÚNICA de honorario → (pct, fijo). Si fijo no es None, el bruto
    del mes es ese monto y pct es None; si no, bruto = ingreso × pct / 100."""
    _fijo = honorario_fijo(pid, mes)
    if _fijo is not None:
        return None, _fijo
    return pct_map.get(pid, PCT_DEFAULT), None


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
    pct_map = _pct_map(c)

    profs = []
    tot_ing = tot_bruto = tot_liq = tot_cmc = 0
    for r in rows:
        pid = r["id_profesional"]
        ingreso = int(r["ingreso"] or 0)
        info = PROFESIONALES.get(pid, {})
        nombre = info.get("nombre") or f"Prof {pid}"
        pct, _fijo = _regla_honorario(pid, mes, pct_map)
        if _fijo is not None:
            bruto = _fijo
        else:
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
        _meta = _gasto_meta_local(c, mes) if _SOLO_LOCAL.get() else _gasto_meta(mes)
        if _meta and _meta[0] > 0:
            gastos += _meta[0]
            gastos_detalle.append({
                "id": None, "categoria": "Publicidad Meta Ads",
                "descripcion": f"Auto · gasto real de {_meta[1]} campañas en Meta"
                               + (f" (excluye {len(_meta[2])} de otros negocios)" if _meta[2] else ""),
                "monto": _meta[0], "recurrente": 0, "auto": True,
                "fuente": _meta[3] if len(_meta) > 3 else "meta_en_vivo",
            })
    if not any("whatsapp" in ((g.get("categoria") or "") + (g.get("descripcion") or "")).lower()
               for g in gastos_detalle):
        _wa = _gasto_whatsapp(mes)
        if _wa and _wa[0] > 0:
            gastos += _wa[0]
            _cats = " · ".join(f"{k.lower()} US${v:,.2f}" for k, v in _wa[2].items() if v > 0)
            gastos_detalle.append({
                "id": None, "categoria": "WhatsApp Business",
                "descripcion": f"Auto · costo real Meta US${_wa[1]:,.2f} ({_cats})",
                "monto": _wa[0], "recurrente": 0, "auto": True,
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


# ───────────────────────── Vista por día ─────────────────────────────────────
# El EBITDA de un mes en curso no se puede leer contra el mes completo: los
# FIJOS (contrato fijo de Abarca + gastos recurrentes) se pagan enteros, pero la
# venta todavía no llega. Para leerlo "a la fecha" los fijos se prorratean:
#   · días corridos  → día X de N del mes (default)
#   · días con caja  → días hábiles transcurridos / días hábiles del mes
#                      (Chile: lun-sáb, sin domingos ni feriados)
# Los variables (honorarios %, Meta, WhatsApp, Transbank, gastos puntuales) van
# tal cual en su fecha. En un mes cerrado el factor es 1 y el acumulado al
# último día es EXACTAMENTE el EBITDA del mes (`_ebitda_mes`).

# Feriados nacionales (irrenunciables + legales). Editar acá si cambian.
FERIADOS_CL = {
    # 2025
    "2025-01-01", "2025-04-18", "2025-04-19", "2025-05-01", "2025-05-21", "2025-06-20",
    "2025-06-29", "2025-07-16", "2025-08-15", "2025-09-18", "2025-09-19", "2025-10-12",
    "2025-10-31", "2025-11-01", "2025-11-16", "2025-12-08", "2025-12-14", "2025-12-25",
    # 2026
    "2026-01-01", "2026-04-03", "2026-04-04", "2026-05-01", "2026-05-21", "2026-06-21",
    "2026-06-29", "2026-07-16", "2026-08-15", "2026-09-18", "2026-09-19", "2026-10-12",
    "2026-10-31", "2026-11-01", "2026-12-08", "2026-12-25",
    # 2027 (revisar cuando se publique el calendario oficial)
    "2027-01-01", "2027-03-26", "2027-03-27", "2027-05-01", "2027-05-21", "2027-06-21",
    "2027-06-28", "2027-07-16", "2027-08-15", "2027-09-18", "2027-09-19", "2027-10-11",
    "2027-10-31", "2027-11-01", "2027-12-08", "2027-12-25",
}


def _hoy_clt() -> date:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("America/Santiago")).date()


def _es_habil(d: date) -> bool:
    return d.weekday() != 6 and d.isoformat() not in FERIADOS_CL


def _mes_prev(mes: str, n: int = 1) -> str:
    yr, mo = int(mes[:4]), int(mes[5:7]) - n
    while mo <= 0:
        mo += 12; yr -= 1
    return f"{yr}-{mo:02d}"


def _dias_del_mes(mes: str) -> list:
    inicio, fin = _mes_bounds(mes)
    a, b = date.fromisoformat(inicio).toordinal(), date.fromisoformat(fin).toordinal()
    return [date.fromordinal(o) for o in range(a, b)]


def _factor(dias: list, hasta: int, criterio: str) -> tuple:
    """(transcurridos, totales, factor) para prorratear los fijos al día `hasta`."""
    if criterio == "habiles":
        tot = sum(1 for d in dias if _es_habil(d))
        tr = sum(1 for d in dias[:hasta] if _es_habil(d))
    else:
        tot, tr = len(dias), hasta
    return tr, tot, (tr / tot if tot else 1.0)


def _repartir(total: float, pesos: dict, dias_iso: list, uniforme_hasta: int) -> dict:
    """Reparte `total` entre días según `pesos` (fecha→peso). Sin pesos, en
    partes iguales entre los días 1..uniforme_hasta. Devuelve fecha→monto."""
    s = sum(v for k, v in pesos.items() if k in dias_iso and v > 0)
    if total and s > 0:
        return {k: total * pesos.get(k, 0) / s for k in dias_iso if pesos.get(k, 0) > 0}
    n = max(1, uniforme_hasta)
    return {k: total / n for k in dias_iso[:n]} if total else {}


def _meta_pesos_dia(c, mes: str) -> dict:
    """Gasto Meta CMC por día desde la foto diaria local (misma exclusión)."""
    inicio, fin = _mes_bounds(mes)
    out = {}
    try:
        for fe, nom, g in c.execute(
            "SELECT fecha, campaign_name, SUM(spend) FROM meta_insights_diario WHERE desglose='total' "
            "AND fecha>=? AND fecha<? GROUP BY fecha, campaign_id, campaign_name", (inicio, fin)).fetchall():
            if any(k in (nom or "").lower() for k in _META_NO_CMC):
                continue
            out[fe] = out.get(fe, 0.0) + float(g or 0)
    except Exception:
        pass
    return out


def _meta_campanas(c, mes: str) -> tuple:
    """Detalle por campaña: en vivo si la caché lo tiene; si no, foto diaria."""
    if _META_DET.get(mes):
        return _META_DET[mes], "meta_en_vivo"
    inicio, fin = _mes_bounds(mes)
    det = []
    try:
        for nom, g in c.execute(
            "SELECT campaign_name, SUM(spend) FROM meta_insights_diario WHERE desglose='total' "
            "AND fecha>=? AND fecha<? GROUP BY campaign_id, campaign_name ORDER BY 2 DESC", (inicio, fin)).fetchall():
            if float(g or 0) <= 0:
                continue
            det.append({"campana": nom or "", "monto": round(float(g)),
                        "cmc": not any(k in (nom or "").lower() for k in _META_NO_CMC)})
    except Exception:
        pass
    return det, "foto_diaria"


def _serie_mes(c, mes: str, hoy: date) -> dict:
    """Serie diaria del mes con el MISMO total que `_ebitda_mes`."""
    e = _ebitda_mes(c, mes)            # fija Meta/WA/Transbank (con caché) y totales
    dias = _dias_del_mes(mes)
    iso = [d.isoformat() for d in dias]
    abierto = mes == hoy.strftime("%Y-%m")
    futuro = mes > hoy.strftime("%Y-%m")
    dia_corte = hoy.day if abierto else (0 if futuro else len(dias))
    inicio, fin = _mes_bounds(mes)
    pct_map = _pct_map(c)

    # venta y honorario variable por día y profesional (misma regla que el mes)
    por_prof: dict = {}
    venta = {k: 0 for k in iso}; hon = {k: 0.0 for k in iso}
    npag = {k: 0 for k in iso}; npac = {k: 0 for k in iso}
    for r in c.execute(
        "SELECT fecha, id_profesional, SUM(monto) m, COUNT(*) n, COUNT(DISTINCT id_paciente) p "
        "FROM bi_pagos_caja WHERE fecha>=? AND fecha<? AND id_profesional IS NOT NULL "
        "GROUP BY fecha, id_profesional", (inicio, fin)).fetchall():
        fe, pid, m = r["fecha"], r["id_profesional"], int(r["m"] or 0)
        if fe not in venta:
            continue
        pct, fijo = _regla_honorario(pid, mes, pct_map)
        venta[fe] += m; npag[fe] += r["n"]; npac[fe] += r["p"]
        if fijo is None:
            hon[fe] += m * pct / 100
        por_prof.setdefault(str(pid), {})[fe] = [m, r["n"], r["p"]]

    # fijos (contrato fijo + recurrentes) vs variables (resto, en su fecha)
    fijo_contrato = sum(p["bruto"] for p in e["profesionales"] if p["fijo"])
    recurrentes = [g for g in e["gastos_detalle"] if g.get("recurrente") and not g.get("auto")]
    puntuales = [g for g in e["gastos_detalle"] if not g.get("recurrente") and not g.get("auto")]
    rec_total = sum(int(g["monto"] or 0) for g in recurrentes)
    auto = {g["categoria"]: int(g["monto"] or 0) for g in e["gastos_detalle"] if g.get("auto")}
    hasta_uni = max(1, dia_corte)
    meta_d = _repartir(auto.get("Publicidad Meta Ads", 0), _meta_pesos_dia(c, mes), iso, hasta_uni)
    wa_pesos = (_WA_DET.get(mes) or {}).get("por_dia_usd") or {}
    wa_d = _repartir(auto.get("WhatsApp Business", 0), wa_pesos, iso, hasta_uni)
    tbk_d = _repartir(auto.get("Comisión Transbank", 0), _TBK_DIA.get(mes) or {}, iso, hasta_uni)
    punt_d: dict = {}
    for g in puntuales:
        k = (g.get("fecha") or inicio)[:10]
        k = k if k in venta else inicio
        punt_d[k] = punt_d.get(k, 0) + int(g["monto"] or 0)

    fijos_total = fijo_contrato + rec_total
    filas, acum_var = [], 0.0
    for i, d in enumerate(dias):
        k = d.isoformat()
        var = venta[k] - hon[k] - meta_d.get(k, 0) - wa_d.get(k, 0) - tbk_d.get(k, 0) - punt_d.get(k, 0)
        fut = i + 1 > dia_corte
        if not fut:
            acum_var += var
        filas.append({
            "fecha": k, "dia": d.day, "dow": d.weekday(), "habil": _es_habil(d),
            "feriado": k in FERIADOS_CL, "futuro": fut,
            "venta": venta[k], "honorarios_var": round(hon[k]), "cmc_var": round(venta[k] - hon[k]),
            "meta": round(meta_d.get(k, 0)), "whatsapp": round(wa_d.get(k, 0)),
            "transbank": round(tbk_d.get(k, 0)), "puntuales": punt_d.get(k, 0),
            "pagos": npag[k], "pacientes": npac[k],
            "var_acum": None if fut else round(acum_var),
        })
    # Residuo de redondeo (honorarios redondeados por profesional en el mes):
    # el acumulado al corte debe calzar al peso con el EBITDA del mes.
    var_total_mes = e["ebitda"] + fijos_total
    if dia_corte:
        resid = var_total_mes - acum_var
        filas[dia_corte - 1]["var_acum"] = round(acum_var + resid)

    crit = {}
    for cr in ("corridos", "habiles"):
        tr, tot, fac = _factor(dias, dia_corte, cr)
        serie = []
        for i, f in enumerate(filas):
            if f["var_acum"] is None:
                serie.append(None); continue
            _, _, fi = _factor(dias, i + 1, cr)
            serie.append(round(f["var_acum"] - fijos_total * fi))
        crit[cr] = {"transcurridos": tr, "totales": tot, "factor": round(fac, 4),
                    "fijos_prorrateados": round(fijos_total * fac),
                    "ebitda_proporcional": round(var_total_mes - fijos_total * fac) if dia_corte else 0,
                    "serie": serie}
    return {
        "mes": mes, "abierto": abierto, "futuro": futuro, "dia_corte": dia_corte, "dias_mes": len(dias),
        "e": e, "dias": filas, "por_prof": por_prof, "criterios": crit,
        "fijos": {"total": fijos_total, "contrato_fijo": fijo_contrato, "recurrentes": rec_total,
                  "detalle_recurrentes": [{"id": g["id"], "categoria": g["categoria"],
                                           "descripcion": g.get("descripcion"), "monto": g["monto"]}
                                          for g in recurrentes],
                  "contratos": [{"id": p["id"], "nombre": p["nombre"], "monto": p["bruto"]}
                                for p in e["profesionales"] if p["fijo"]]},
        "variables_total": round(var_total_mes),
    }


def _corte_info(c, mes: str, hoy: date, serie: dict) -> dict:
    """Hasta dónde llega la caja: última fecha con pagos + hora del último sync."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    ult = None
    for f in serie["dias"]:
        if f["venta"] > 0:
            ult = f["fecha"]
    sync = None
    try:
        r = c.execute("SELECT fin FROM bi_sync_log WHERE tipo='pagos' AND ok=1 "
                      "ORDER BY id DESC LIMIT 1").fetchone()
        if r and r[0]:
            # bi_sync escribe utcnow() → pasar a hora de Chile
            dt = datetime.fromisoformat(r[0][:19]).replace(tzinfo=ZoneInfo("UTC"))
            sync = dt.astimezone(ZoneInfo("America/Santiago")).strftime("%Y-%m-%dT%H:%M")
    except Exception:
        pass
    return {"ultima_fecha_caja": ult, "sync_clt": sync, "hoy": hoy.isoformat()}


def _proyeccion(serie: dict, prev: dict | None, hoy: date) -> dict | None:
    """Cierre proyectado del mes en curso: ritmo diario × días con caja que
    faltan. Rango = mín/máx de 3 ritmos (este mes, mes anterior, últimos 10 días
    hábiles). Es una PROYECCIÓN, no un dato."""
    if not serie["abierto"]:
        return None
    filas = serie["dias"]
    d = serie["dia_corte"]
    completos = [f for f in filas[:d - 1] if f["habil"]]          # sin hoy (parcial)
    restantes = [f for f in filas[d - 1:] if f["habil"]]          # hoy + lo que falta
    if not completos:
        return None
    venta_comp = sum(f["venta"] for f in filas[:d - 1])
    r_mes = sum(f["venta"] for f in completos) / len(completos)
    ritmos = {"este_mes": r_mes}
    if prev:
        ph = [f for f in prev["dias"] if f["habil"] and not f["futuro"]]
        if ph:
            ritmos["mes_anterior"] = sum(f["venta"] for f in ph) / len(ph)
        ult10 = (ph + completos)[-10:]
        ritmos["ultimos_10"] = sum(f["venta"] for f in ult10) / len(ult10)
    venta_hasta = sum(f["venta"] for f in filas[:d - 1])
    cmc_hasta = sum(f["cmc_var"] for f in filas[:d - 1])
    ratio_cmc = cmc_hasta / venta_hasta if venta_hasta else 0.35
    tbk_ratio = (sum(f["transbank"] for f in filas[:d]) / max(1, sum(f["venta"] for f in filas[:d])))
    dias_tr = max(1, d)
    meta_rr = sum(f["meta"] for f in filas[:d]) / dias_tr * serie["dias_mes"]
    wa_rr = sum(f["whatsapp"] for f in filas[:d]) / dias_tr * serie["dias_mes"]
    punt = sum(f["puntuales"] for f in filas)
    fijos = serie["fijos"]["total"]

    def _cierre(ritmo):
        v = venta_comp + ritmo * len(restantes)
        cmc = cmc_hasta + ritmo * len(restantes) * ratio_cmc
        eb = cmc - meta_rr - wa_rr - v * tbk_ratio - punt - fijos
        return round(v), round(eb)

    vals = sorted(ritmos.values())
    lo, mid, hi = _cierre(vals[0]), _cierre(r_mes), _cierre(vals[-1])
    return {
        "ritmos": {k: round(v) for k, v in ritmos.items()},
        "dias_habiles_restantes": len(restantes), "dias_habiles_completos": len(completos),
        "venta": {"bajo": lo[0], "central": mid[0], "alto": hi[0]},
        "ebitda": {"bajo": lo[1], "central": mid[1], "alto": hi[1]},
        "supuestos": {"margen_cmc_variable_pct": round(ratio_cmc * 100, 1),
                      "meta_mes": round(meta_rr), "whatsapp_mes": round(wa_rr)},
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

    @app.get("/api/cmc/ebitda/diario", tags=["bi"])
    def api_ebitda_diario(mes: str | None = Query(None), token: str | None = Query(None),
                          cmc_session: str | None = Cookie(None)):
        """Vista por día: venta, margen CMC y EBITDA acumulado con los fijos
        prorrateados (días corridos y días con caja), venta por profesional por
        día, comparación a la misma altura con los 2 meses anteriores y
        proyección de cierre. Reusa `_ebitda_mes` (mismos totales, misma caché
        de Meta/WhatsApp: no agrega llamadas por día)."""
        _auth(token, cmc_session)
        from session import db
        from medilink import PROFESIONALES
        hoy = _hoy_clt()
        if not mes:
            mes = hoy.strftime("%Y-%m")
        if len(mes) != 7 or mes[4] != "-":
            raise HTTPException(400, "mes debe ser YYYY-MM")
        with db() as c:
            s = _serie_mes(c, mes, hoy)
            corte = _corte_info(c, mes, hoy, s)
            prevs = [_serie_mes(c, _mes_prev(mes, k), hoy) for k in (1, 2)]
            meta_det, meta_fuente = _meta_campanas(c, mes)
        # Comparación a la misma altura: días COMPLETOS (en el mes en curso hoy
        # va parcial → se compara hasta ayer).
        d_cmp = s["dia_corte"] - 1 if s["abierto"] and s["dia_corte"] > 1 else s["dia_corte"]

        def _a_la_altura(sx, d):
            d = min(d, sx["dias_mes"])
            fil = sx["dias"][:d]
            out = {"mes": sx["mes"], "hasta_dia": d,
                   "venta": sum(f["venta"] for f in fil),
                   "cmc_var": sum(f["cmc_var"] for f in fil),
                   "pacientes": sum(f["pacientes"] for f in fil),
                   "venta_mes_completo": sx["e"]["ingresos"],
                   "ebitda_mes_completo": sx["e"]["ebitda"], "abierto": sx["abierto"]}
            for cr in ("corridos", "habiles"):
                ser = sx["criterios"][cr]["serie"]
                out["ebitda_" + cr] = ser[d - 1] if d and ser[d - 1] is not None else None
            return out

        comparacion = [_a_la_altura(x, d_cmp) for x in [s] + prevs] if d_cmp else []
        e = s["e"]
        wa = _WA_DET.get(mes) or {}
        return {
            "mes": mes, "hoy": hoy.isoformat(), "abierto": s["abierto"],
            "dia_corte": s["dia_corte"], "dias_mes": s["dias_mes"], "corte": corte,
            "dias": s["dias"], "criterios": s["criterios"], "fijos": s["fijos"],
            "variables_total": s["variables_total"],
            "ebitda_mes_completo": e["ebitda"], "ingresos": e["ingresos"],
            "resumen": {k: e[k] for k in ("ingresos", "honorarios_bruto", "retencion_total",
                                          "liquido_total", "margen_cmc", "gastos", "ebitda",
                                          "margen_ebitda_pct", "margen_cmc_pct")},
            "gastos_detalle": e["gastos_detalle"],
            "por_prof_dia": s["por_prof"],
            "profesionales": [dict(p, especialidad=p.get("especialidad") or
                                   PROFESIONALES.get(p["id"], {}).get("especialidad", ""))
                              for p in e["profesionales"]],
            "comparacion": comparacion, "dia_comparacion": d_cmp,
            "previos": [{"mes": x["mes"], "abierto": x["abierto"], "dia_corte": x["dia_corte"],
                         "fijos": {k: x["fijos"][k] for k in ("total", "contrato_fijo", "recurrentes")},
                         "ingresos": x["e"]["ingresos"], "ebitda_mes_completo": x["e"]["ebitda"],
                         "dias": [{k: f[k] for k in ("dia", "fecha", "habil", "futuro", "venta",
                                                     "honorarios_var", "cmc_var", "meta", "whatsapp",
                                                     "transbank", "puntuales", "pacientes")}
                                  for f in x["dias"]],
                         "criterios": {cr: {"factor": x["criterios"][cr]["factor"],
                                            "serie": x["criterios"][cr]["serie"]}
                                       for cr in ("corridos", "habiles")}} for x in prevs],
            "proyeccion": _proyeccion(s, prevs[0] if prevs else None, hoy),
            "meta_campanas": meta_det, "meta_fuente": meta_fuente,
            "whatsapp": {"dolar": wa.get("dolar"), "volumen": wa.get("volumen") or {},
                         "por_dia_usd": wa.get("por_dia_usd") or {}},
            "feriados": sorted(f for f in FERIADOS_CL if f.startswith(mes)),
        }

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
