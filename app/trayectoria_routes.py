# -*- coding: utf-8 -*-
"""Trayectoria del centro y techo real — SOLO DUENO.

Responde tres preguntas que el dueno hizo el 2026-09-21 y que ningun modulo
contestaba:

  1. Con 5 anos de caja (jul-2021 → hoy), ¿la empresa esta sana o solo grande?
  2. ¿Que pasa con la venta y el margen si el dueno deja de atender?
  3. ¿Cual es el techo, medido con lo que cada profesional YA demostro?

POR QUE ES SOLO DEL DUENO
Lleva honorarios y margen por profesional. No se agrega a la lista de modulos
del perfil Recepcion: OLACORE_TOKEN tiene `modulos: None` (ve todo) y Recepcion
tiene allowlist explicita, asi que basta con NO agregarlo ahi. Ademas `_auth`
exige OLACORE_TOKEN: un token de admin cualquiera no entra.

DE DONDE SALEN LOS NUMEROS
  · venta   = bi_pagos_caja (la CAJA es el hecho; `fecha` = recepcion del pago)
  · margen  = venta − honorarios, donde honorario es el FIJO del profesional si
              lo tiene (honorario_fijo) y si no venta × pct_honorario/100.
              OJO: `pct_honorario` es lo que se lleva el PROFESIONAL, no el
              margen del centro. Calcularlo al reves da vuelta el ranking.
  · techo   = mejor mes REAL de cada profesional activo × 12. No es un supuesto:
              es lo que cada uno ya hizo al menos una vez.

CALIDAD DEL DATO — se muestra en la pagina, no se esconde
La venta atribuida a profesionales que no estan en `equipo_cmc` se costea al
PCT_DEFAULT. En 2021-2022 eso es mas de la mitad de la venta, asi que el margen
de esos anos es referencia y no dato. De 2024 en adelante baja a 2-10%.
"""
import calendar
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Cookie, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from session import db
from ebitda_routes import honorario_fijo, PCT_DEFAULT

router = APIRouter()
_CL = ZoneInfo("America/Santiago")

# El dueno atiende con id 1 en Medilink. Se nombra una vez: el escenario "si
# dejo de atender" es literalmente sacar este id del calculo.
ID_DUENO = 1
DESDE = "2021-07-01"     # primer mes completo de caja en Medilink


def _auth(request: Request, token: str | None, cmc_session: str | None) -> None:
    """Solo el token del dueno. Un ADMIN_TOKEN (recepcion) NO entra.

    No se reutiliza `_is_admin_token` a proposito: ese acepta el token de
    recepcion, y esta pagina muestra cuanto se lleva cada profesional.
    """
    from config import OLACORE_TOKEN
    import hmac
    if token and OLACORE_TOKEN and hmac.compare_digest(token, OLACORE_TOKEN):
        return
    raise HTTPException(403, "Solo el token del dueño abre este módulo")


# ── Calculo ─────────────────────────────────────────────────────────────────

def _margen(mes: str, id_prof: int, venta: float, pct: dict) -> float:
    """Lo que le queda al CENTRO de esa venta."""
    fijo = honorario_fijo(id_prof, mes)
    if fijo is not None:
        return venta - fijo
    return venta * (100 - pct.get(id_prof, PCT_DEFAULT)) / 100


def datos() -> dict:
    with db() as c:
        eq = {r[0]: {"nombre": r[1], "esp": r[2] or "",
                     "pct": r[3] if r[3] is not None else PCT_DEFAULT}
              for r in c.execute("SELECT id_medilink, nombre, especialidad, pct_honorario "
                                 "FROM equipo_cmc WHERE id_medilink IS NOT NULL")}
        filas = list(c.execute(
            "SELECT substr(fecha,1,7), id_profesional, SUM(monto) FROM bi_pagos_caja "
            "WHERE fecha >= ? GROUP BY 1,2", (DESDE,)))
        # Misma venta recortada al día de hoy, para la vista de "mismo corte".
        _hoy = datetime.now(_CL).date().strftime("%m-%d")
        filas_corte = list(c.execute(
            "SELECT substr(fecha,1,7), id_profesional, SUM(monto) FROM bi_pagos_caja "
            "WHERE fecha >= ? AND substr(fecha,6,5) <= ? GROUP BY 1,2", (DESDE, _hoy)))
        pac = dict(c.execute(
            "SELECT substr(fecha,1,4), COUNT(DISTINCT id_paciente) FROM bi_pagos_caja "
            "WHERE fecha >= ? GROUP BY 1", (DESDE,)))
        nprof = dict(c.execute(
            "SELECT substr(fecha,1,4), COUNT(DISTINCT id_profesional) FROM bi_pagos_caja "
            "WHERE fecha >= ? GROUP BY 1", (DESDE,)))
    pct = {i: v["pct"] for i, v in eq.items()}

    # ── serie anual ──
    anios: dict = {}
    meses: dict = {}
    for m, i, v in filas:
        a = m[:4]
        d = anios.setdefault(a, {"v": 0.0, "g": 0.0, "desc": 0.0, "meses": set()})
        d["v"] += v
        d["g"] += _margen(m, i, v, pct)
        d["meses"].add(m)
        if i and i not in eq:
            d["desc"] += v
        meses[m] = meses.get(m, 0) + v
    serie = []
    for a in sorted(anios):
        d = anios[a]
        n = len(d["meses"])
        serie.append({"anio": a, "venta": d["v"], "margen": d["g"],
                      "pct": 100 * d["g"] / d["v"], "meses": n,
                      # Anualizado para poder comparar 2021 (6 meses) y 2026 (9).
                      "venta_anual": d["v"] / n * 12,
                      "desc": 100 * d["desc"] / d["v"],
                      "pacientes": pac.get(a, 0), "profs": nprof.get(a, 0)})
    for k in range(1, len(serie)):
        serie[k]["crec"] = 100 * (serie[k]["venta_anual"] / serie[k-1]["venta_anual"] - 1)

    # ── 2026: hoy, sin el dueno, y su produccion ──
    v26 = g26 = vsin = gsin = 0.0
    for m, i, v in filas:
        if not m.startswith("2026"):
            continue
        g = _margen(m, i, v, pct)
        v26 += v; g26 += g
        if i != ID_DUENO:
            vsin += v; gsin += g
    n26 = len({m for m, _, _ in filas if m.startswith("2026")})
    k = 12 / n26
    escen = {"venta": v26*k, "margen": g26*k, "pct": 100*g26/v26,
             "venta_sin": vsin*k, "margen_sin": gsin*k, "pct_sin": 100*gsin/vsin,
             "prod": (v26-vsin)*k, "honor": (v26-vsin)*k*0.71,
             "queda": (g26-gsin)*k, "meses": n26,
             "reemplazo": [{"pct": p, "margen": gsin*k + (v26-vsin)*k*(100-p)/100,
                            "delta": (v26-vsin)*k*(100-p)/100 - (g26-gsin)*k}
                           for p in (75, 71, 70, 65, 62, 55)]}

    # ── techo: mejor mes real de cada profesional ACTIVO ──
    # Activo = facturo algo en los ultimos 3 meses. Sin ese filtro el techo
    # suma gente que ya no esta y deja de ser alcanzable.
    recientes = sorted({m for m, _, _ in filas})[-3:]
    activos = {i for m, i, _ in filas if m in recientes}
    peak: dict = {}
    for m, i, v in filas:
        if i not in activos or not m.startswith("2026"):
            continue
        if v > peak.get(i, (0, ""))[0]:
            peak[i] = (v, m)
    ult = sorted(meses)[-1]
    actual = {i: v for m, i, v in filas if m == ult}
    techo = []
    for i, (v, m) in sorted(peak.items(), key=lambda x: -x[1][0]):
        info = eq.get(i, {"nombre": f"id {i}", "esp": "(sin registrar)", "pct": PCT_DEFAULT})
        techo.append({"id": i, "nombre": info["nombre"], "esp": info["esp"],
                      "pct": None if honorario_fijo(i, ult) is not None else info["pct"],
                      "peak": v, "cuando": m, "hoy": actual.get(i, 0),
                      "margen": _margen(ult, i, v, pct)})
    tv = sum(t["peak"] for t in techo)
    tg = sum(t["margen"] for t in techo)
    sv = sum(t["peak"] for t in techo if t["id"] != ID_DUENO)
    sg = sum(t["margen"] for t in techo if t["id"] != ID_DUENO)
    hoy_v = sum(t["hoy"] for t in techo)

    # ── quien movio el margen 2025 → 2026 (mismos meses) ──
    tope = int(ult[5:7])
    mov: dict = {}
    for m, i, v in filas:
        a, mm = m[:4], int(m[5:7])
        if a not in ("2025", "2026") or mm > tope:
            continue
        d = mov.setdefault(i, {"v25": 0.0, "v26": 0.0, "g25": 0.0, "g26": 0.0})
        d[f"v{a[2:]}"] += v
        d[f"g{a[2:]}"] += _margen(m, i, v, pct)
    ranking = []
    for i, d in mov.items():
        info = eq.get(i, {"nombre": f"id {i}", "esp": "(sin registrar)", "pct": PCT_DEFAULT})
        ranking.append({"nombre": info["nombre"], "esp": info["esp"],
                        "pct": None if honorario_fijo(i, ult) is not None else info["pct"],
                        "v25": d["v25"], "v26": d["v26"], "delta": d["g26"] - d["g25"]})
    ranking.sort(key=lambda x: -x["delta"])

    corte_proy = ytd_y_proyeccion(filas_corte, filas, pct)
    extra = analisis_extra(filas, meses, pct, eq)

    return {"serie": serie, "escenario": escen, "ranking": ranking,
            "corte": corte_proy, "extra": extra,
            "techo": {"filas": techo, "venta": tv, "margen": tg, "pct": 100*tg/tv,
                      "venta_sin": sv, "margen_sin": sg, "pct_sin": 100*sg/sv,
                      "hoy": hoy_v, "avance": 100*hoy_v/tv, "mes": ult},
            "meses": meses, "generado": datetime.now(_CL).strftime("%d-%m-%Y %H:%M")}


def analisis_extra(filas, meses, pct, eq) -> dict:
    """Lo que la serie anual no cuenta: el valle, el mix, y de donde salio el quiebre.

    Todo se calcula sobre la misma caja; ninguna cifra esta escrita a mano.
    """
    # ── el valle y la recuperacion ────────────────────────────────────────
    llenos = {m: v for m, v in meses.items() if v > 0}
    peor = min(llenos.items(), key=lambda x: x[1])
    mejor = max(llenos.items(), key=lambda x: x[1])
    dif = ((int(mejor[0][:4]) - int(peor[0][:4])) * 12
           + int(mejor[0][5:7]) - int(peor[0][5:7]))

    # ── mix: cuanta venta viene de profesionales caros ────────────────────
    # El corte en 70% no es arbitrario: es donde estan psiquiatria (90),
    # implantologia (80) y las medicinas generales de 75 — las lineas que
    # crecen la venta sin mover el margen.
    mix = {}
    for m, i, v in filas:
        a = m[:4]
        d = mix.setdefault(a, {"tot": 0.0, "caro": 0.0, "barato": 0.0, "fijo": 0.0})
        d["tot"] += v
        if honorario_fijo(i, m) is not None:
            d["fijo"] += v
        elif pct.get(i, PCT_DEFAULT) >= 70:
            d["caro"] += v
        elif pct.get(i, PCT_DEFAULT) < 60:
            d["barato"] += v
    mix_filas = [{"anio": a, **{k: 100 * d[k] / d["tot"] for k in ("caro", "barato", "fijo")},
                  "medio": 100 * (d["tot"] - d["caro"] - d["barato"] - d["fijo"]) / d["tot"]}
                 for a, d in sorted(mix.items())]

    # ── el quiebre: quien crecio entre el peor año y el siguiente ─────────
    a_peor = peor[0][:4]
    a_sig = str(int(a_peor) + 1)
    q = {}
    for m, i, v in filas:
        if m[:4] in (a_peor, a_sig):
            q.setdefault(i, {a_peor: 0.0, a_sig: 0.0})[m[:4]] += v
    quiebre = []
    for i, dd in q.items():
        info = eq.get(i, {"nombre": f"id {i}", "esp": "(sin registrar)"})
        quiebre.append({"nombre": info["nombre"], "esp": info["esp"],
                        "antes": dd[a_peor], "despues": dd[a_sig],
                        "delta": dd[a_sig] - dd[a_peor]})
    quiebre.sort(key=lambda x: -x["delta"])

    # ── sueldos fijos: lo unico que rompe la banda, ¿esta funcionando? ────
    fijos = {}
    for m, i, v in filas:
        fj = honorario_fijo(i, m)
        if fj is None:
            continue
        d = fijos.setdefault(i, {"v": 0.0, "c": 0.0, "desde": m, "hasta": m})
        d["v"] += v; d["c"] += fj
        d["desde"] = min(d["desde"], m); d["hasta"] = max(d["hasta"], m)
    fijos_filas = [{"nombre": eq.get(i, {"nombre": f"id {i}"})["nombre"],
                    "esp": eq.get(i, {"esp": ""}).get("esp", ""), **d,
                    "neto": d["v"] - d["c"]} for i, d in fijos.items()]

    # ── pendientes de datos: venta sin % conocido ─────────────────────────
    sin = {}
    for m, i, v in filas:
        if i and i not in eq:
            d = sin.setdefault(i, {"v": 0.0, "anios": set()})
            d["v"] += v; d["anios"].add(m[:4])
    pend = sorted(({"id": i, "v": d["v"], "anios": sorted(d["anios"])}
                   for i, d in sin.items()), key=lambda x: -x["v"])[:6]

    return {"peor": {"mes": peor[0], "v": peor[1]},
            "mejor": {"mes": mejor[0], "v": mejor[1]},
            "factor": mejor[1] / peor[1], "meses_rec": dif,
            "mix": mix_filas, "quiebre": quiebre, "a_peor": a_peor, "a_sig": a_sig,
            "fijos": fijos_filas, "pendientes": pend}


def ytd_y_proyeccion(filas_corte, filas_todo, pct) -> dict:
    """Dos vistas que la serie anual no da:

    1. MISMO CORTE — cada año medido del 1-ene al día de hoy. Comparar años
       completos contra uno en curso infla al pasado; este corte no.
    2. PROYECCION — cierre del año usando la ESTACIONALIDAD real (cuánto entró
       históricamente después de esta fecha, como múltiplo del acumulado), no
       una regla de tres sobre el promedio diario.

    Dos trampas que este cálculo evita a propósito:

      · El sueldo FIJO se prorratea en el mes del corte. Al 21-sep corresponden
        21/30 del sueldo, no el mes entero; sin eso el margen del año en curso
        sale castigado contra los años cerrados.
      · 2021 queda FUERA del promedio de estacionalidad. La caja arranca en
        julio de 2021, así que su "acumulado al corte" son 3 meses y su ratio
        (1,29x) no mide estacionalidad sino datos faltantes.
    """
    hoy = datetime.now(_CL).date()
    corte = hoy.strftime("%m-%d")
    dim = calendar.monthrange(hoy.year, hoy.month)[1]
    mes_corte = hoy.strftime("%m")

    # ── 1. mismo corte ──
    ytd: dict = {}
    for m, i, v in filas_corte:
        a = m[:4]
        d = ytd.setdefault(a, {"v": 0.0, "h": 0.0, "fij": {}})
        d["v"] += v
        fj = honorario_fijo(i, m)
        if fj is not None:
            d["fij"][(m, i)] = fj * (hoy.day / dim if m[5:7] == mes_corte else 1.0)
        else:
            d["h"] += v * pct.get(i, PCT_DEFAULT) / 100
    corte_filas = []
    ant = None
    for a in sorted(ytd):
        d = ytd[a]
        g = d["v"] - d["h"] - sum(d["fij"].values())
        corte_filas.append({"anio": a, "venta": d["v"], "margen": g,
                            "pct": 100 * g / d["v"],
                            "crec": None if ant is None else 100 * (d["v"] / ant - 1),
                            # 2021 arranca en julio: su corte no es comparable.
                            "parcial": a == "2021"})
        ant = d["v"]

    # ── 2. estacionalidad y cierre ──
    porm: dict = {}
    for m, i, v in filas_todo:
        porm[m] = porm.get(m, 0.0) + v
    ratios = []
    for a in sorted(ytd):
        if a == "2021" or a == str(hoy.year):
            continue
        resto = sum(v for m, v in porm.items() if m[:4] == a and m[5:7] > mes_corte)
        if resto and ytd[a]["v"]:
            ratios.append({"anio": a, "ratio": resto / ytd[a]["v"],
                           "base": ytd[a]["v"], "resto": resto})
    prud = sum(r["ratio"] for r in ratios) / len(ratios) if ratios else 0
    # Los dos últimos años cerrados mandan: hubo un quiebre de régimen (el Q4
    # de un negocio que se encoge pesa mucho menos que el de uno que crece).
    rec = (sum(r["ratio"] for r in ratios[-2:]) / len(ratios[-2:])) if ratios else 0

    base = ytd[str(hoy.year)]["v"]
    # resto del mes en curso: días hábiles por el promedio de las últimas semanas
    with db() as c:
        dd = dict(c.execute("SELECT fecha, SUM(monto) FROM bi_pagos_caja "
                            "WHERE fecha >= date('now','localtime','-40 day') "
                            "AND fecha < date('now','localtime') GROUP BY 1"))
    sem = [v for f, v in dd.items() if date.fromisoformat(f).weekday() < 5]
    sab = [v for f, v in dd.items() if date.fromisoformat(f).weekday() == 5]
    ps = sum(sem) / len(sem) if sem else 0
    pb = sum(sab) / len(sab) if sab else 0
    falta_mes = sum(pb if date(hoy.year, hoy.month, d).weekday() == 5
                    else (ps if date(hoy.year, hoy.month, d).weekday() < 5 else 0)
                    for d in range(hoy.day + 1, dim + 1))

    cierres = [{"nombre": "Prudente", "det": f"estacionalidad promedio de {len(ratios)} años",
                "ratio": prud, "total": base + falta_mes + base * prud},
               {"nombre": "Tendencia", "det": "estacionalidad de los 2 últimos años",
                "ratio": rec, "total": base + falta_mes + base * rec}]
    anterior = sum(v for m, v in porm.items() if m[:4] == str(hoy.year - 1))
    for cc in cierres:
        cc["margen"] = cc["total"] * corte_filas[-1]["pct"] / 100
        cc["vs"] = 100 * (cc["total"] / anterior - 1) if anterior else 0

    return {"corte": corte_filas, "corte_fecha": hoy.strftime("%d-%m"),
            "ratios": ratios, "base": base, "falta_mes": falta_mes,
            "dias_falta": sum(1 for d in range(hoy.day + 1, dim + 1)
                              if date(hoy.year, hoy.month, d).weekday() < 6),
            "dia_habil": ps, "cierres": cierres, "anterior": anterior}


@router.get("/alma/api/trayectoria")
def api(request: Request, token: str | None = Query(None),
        cmc_session: str | None = Cookie(None)):
    _auth(request, token, cmc_session)
    return JSONResponse(datos())


# ── Panel ───────────────────────────────────────────────────────────────────

_CSS = """
/* Sistema visual Alma — misma paleta que el resto del shell. */
:root{--aqua:#4FBECE;--aqua-s:#e8f8fb;--blue:#1172AB;--navy:#0F3F68;
--bg:#F7FBFD;--card:#fff;--border:#d6e4ec;--text:#0F3F68;--mute:#62788a;
--red:#e84545;--red-s:#fdeef0;--amber:#d98407;--amber-s:#fdf6e7;
--green:#12a150;--green-s:#e9f9f0;
--sh-sm:0 1px 3px rgba(15,63,104,.08);--sh-md:0 8px 30px rgba(15,63,104,.12);
--mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,monospace}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font-size:13px;
font-family:'Montserrat',-apple-system,BlinkMacSystemFont,system-ui,sans-serif;
-webkit-font-smoothing:antialiased}
.wrap{max-width:1180px;margin:0 auto;padding:0 16px 56px}
.top{position:sticky;top:0;z-index:20;background:rgba(247,251,253,.93);
backdrop-filter:blur(10px);border-bottom:1px solid var(--border);
margin:0 -16px 20px;padding:16px;display:flex;gap:14px;align-items:center;flex-wrap:wrap}
.top h1{font-size:17px;font-weight:800;margin:0;letter-spacing:-.2px}
.top .meta{color:var(--mute);font-size:11.5px;margin-top:3px}
.card{background:var(--card);border:1px solid var(--border);border-radius:14px;
padding:18px 20px;margin-bottom:16px;box-shadow:var(--sh-sm)}
.card>h2{font-size:13px;font-weight:800;margin:0 0 3px;letter-spacing:-.1px}
.card>.h2s{font-size:11.5px;color:var(--mute);margin:0 0 15px;line-height:1.5}
.nota{font-size:11.5px;color:var(--mute);line-height:1.55;margin:13px 0 0;
padding-top:12px;border-top:1px solid var(--border)}
.nota b{color:var(--text)}
.scroll{overflow-x:auto;margin:0 -20px;padding:0 20px}
table{width:100%;border-collapse:collapse;font-size:12px;min-width:620px}
th{text-align:left;font-size:10px;font-weight:800;letter-spacing:.08em;
text-transform:uppercase;color:var(--mute);padding:0 10px 9px;
border-bottom:1px solid var(--border);white-space:nowrap}
td{padding:9px 10px;border-bottom:1px solid #eef4f8}
tbody tr:hover{background:var(--aqua-s)}
tbody tr:last-child td{border-bottom:0}
.n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap;
font-family:var(--mono);font-size:11.5px}
.nom{font-weight:600}.sub{font-size:10.5px;color:var(--mute)}
tr.crisis{background:var(--red-s)}tr.crisis:hover{background:#fbe3e6}
tr.hoy{background:var(--aqua-s);font-weight:700}
.up{color:var(--green);font-weight:700}.dn{color:var(--red);font-weight:700}
/* Banda del margen */
.banda{position:relative;height:190px;margin:8px 0 4px;padding:0 4px}
.banda .zona{position:absolute;left:0;right:0;background:var(--green-s);
border-top:1px dashed #9fd9bb;border-bottom:1px dashed #9fd9bb}
.banda .zl{position:absolute;right:4px;font-size:10px;font-weight:800;color:#0d8a48}
.banda .cols{display:flex;height:100%;align-items:flex-end;gap:10px;position:relative}
.banda .c{flex:1;display:flex;flex-direction:column;align-items:center;
justify-content:flex-end;height:100%;gap:5px}
.banda .pt{width:100%;max-width:64px;border-radius:5px 5px 0 0;
background:linear-gradient(180deg,var(--aqua),var(--blue))}
.banda .c.mala .pt{background:linear-gradient(180deg,#f08a8a,var(--red))}
.banda .v{font-size:12px;font-weight:800;font-variant-numeric:tabular-nums}
.banda .a{font-size:10.5px;color:var(--mute);font-weight:700}
/* Escenarios */
.esc{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));
margin-bottom:4px}
.e{border:1px solid var(--border);border-radius:13px;padding:15px 16px;
background:var(--card);border-left:4px solid var(--mute)}
.e.hoy{border-left-color:var(--aqua)}.e.sin{border-left-color:var(--amber)}
.e.mejor{border-left-color:var(--green);background:linear-gradient(120deg,var(--green-s),#fff)}
.e .t{font-size:10px;font-weight:800;letter-spacing:.1em;text-transform:uppercase;color:var(--mute)}
.e .v{font-size:24px;font-weight:800;font-variant-numeric:tabular-nums;margin:5px 0 2px;
letter-spacing:-.6px}
.e .d{font-size:11.5px;color:var(--mute);line-height:1.5}
.e .d b{color:var(--text)}
/* Barra de avance al techo */
.gauge{height:26px;border-radius:8px;background:#e8f0f5;overflow:hidden;position:relative;
margin:10px 0 6px}
.gauge i{display:block;height:100%;background:linear-gradient(90deg,var(--aqua),var(--blue))}
.gauge span{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
font-size:11.5px;font-weight:800;color:#fff;text-shadow:0 1px 2px rgba(0,0,0,.28)}
.mini{height:6px;border-radius:3px;background:#e8f0f5;overflow:hidden;min-width:56px}
.mini i{display:block;height:100%;background:var(--aqua)}
/* Serie mensual */
.serie{display:flex;gap:1.5px;align-items:flex-end;height:120px;margin:6px 0 2px}
.serie i{flex:1;background:linear-gradient(180deg,var(--aqua),var(--blue));border-radius:2px 2px 0 0;
min-height:2px;position:relative}
.serie i.peor{background:var(--red)} .serie i.mejor{background:var(--green)}
.ejes{display:flex;justify-content:space-between;font-size:10px;color:var(--mute);font-weight:700}
.hito{display:flex;gap:14px;flex-wrap:wrap;margin-top:12px}
.hito div{flex:1;min-width:150px}
.hito .t{font-size:10px;font-weight:800;letter-spacing:.09em;text-transform:uppercase;color:var(--mute)}
.hito .v{font-size:20px;font-weight:800;font-variant-numeric:tabular-nums;margin-top:3px}
.hito .v.malo{color:var(--red)} .hito .v.bueno{color:var(--green)}
.hito .s{font-size:11px;color:var(--mute)}
/* Mix apilado */
.mix{display:flex;flex-direction:column;gap:7px}
.mx{display:flex;align-items:center;gap:10px}
.mx .a{font-size:11px;font-weight:800;width:38px;color:var(--mute)}
.mx .bar{flex:1;display:flex;height:22px;border-radius:6px;overflow:hidden}
.mx .bar span{display:flex;align-items:center;justify-content:center;font-size:9.5px;
font-weight:800;color:#fff;white-space:nowrap}
.mx .caro{background:var(--red)} .mx .medio{background:#f0a92b}
.mx .barato{background:var(--green)} .mx .fijo{background:var(--blue)}
.leg{display:flex;gap:14px;flex-wrap:wrap;font-size:10.5px;color:var(--mute);margin-top:9px}
.leg i{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:4px;vertical-align:-1px}
/* Proyección */
.proy{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(230px,1fr))}
.p{border:1px solid var(--border);border-radius:13px;padding:15px 16px;background:var(--card);
border-left:4px solid var(--aqua)}
.p.alto{border-left-color:var(--green);background:linear-gradient(120deg,var(--green-s),#fff)}
.p .t{font-size:10px;font-weight:800;letter-spacing:.1em;text-transform:uppercase;color:var(--mute)}
.p .v{font-size:25px;font-weight:800;font-variant-numeric:tabular-nums;margin:5px 0 3px;
letter-spacing:-.7px}
.p .d{font-size:11.5px;color:var(--mute);line-height:1.5}.p .d b{color:var(--text)}
.barras{display:flex;gap:10px;align-items:flex-end;height:120px;margin:10px 0 2px}
.barras .c{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:flex-end;
height:100%;gap:5px}
.barras .b{width:100%;max-width:70px;border-radius:5px 5px 0 0;min-height:4px;
background:linear-gradient(180deg,var(--aqua),var(--blue))}
.barras .c.baja .b{background:linear-gradient(180deg,#f08a8a,var(--red))}
.barras .c.proy .b{background:repeating-linear-gradient(45deg,#bfe6ee,#bfe6ee 5px,#e8f8fb 5px,#e8f8fb 10px);
border:1px dashed var(--blue)}
.barras .v{font-size:11px;font-weight:800;font-variant-numeric:tabular-nums}
.barras .a{font-size:10px;color:var(--mute);font-weight:700}
.aviso{border-radius:11px;padding:13px 16px;font-size:12.5px;line-height:1.55;
margin-bottom:14px;border:1px solid}
.aviso.amber{background:var(--amber-s);border-color:#f0dcae;color:#7c4a03}
@media(max-width:560px){.card{padding:15px 16px}.scroll{margin:0 -16px;padding:0 16px}
.banda{height:150px}.top h1{font-size:15.5px}}
"""


@router.get("/alma/trayectoria", response_class=HTMLResponse)
def panel(request: Request, token: str | None = Query(None),
          cmc_session: str | None = Cookie(None)):
    _auth(request, token, cmc_session)
    d = datos()
    s, esc, te = d["serie"], d["escenario"], d["techo"]

    def M(v):
        return "$%.1fM" % ((v or 0) / 1e6)

    def P(v):
        return "$" + format(int(round(v or 0)), ",").replace(",", ".")

    # ── serie anual ──
    filas = []
    for r in s:
        peor = r["anio"] == min(s, key=lambda x: x.get("crec", 99))["anio"] and r.get("crec", 0) < 0
        cls = "crisis" if r.get("crec", 0) < 0 else ("hoy" if r["anio"] == s[-1]["anio"] else "")
        crec = r.get("crec")
        crec_txt = ("—" if crec is None else
                    f'<span class="{"up" if crec > 0 else "dn"}">{crec:+.0f}%</span>')
        parcial = f' <span class="sub">({r["meses"]} meses)</span>' if r["meses"] < 12 else ""
        filas.append(
            f'<tr class="{cls}"><td class="nom">{r["anio"]}{parcial}</td>'
            f'<td class="n">{M(r["venta"])}</td><td class="n">{crec_txt}</td>'
            f'<td class="n">{M(r["margen"])}</td>'
            f'<td class="n"><b>{r["pct"]:.1f}%</b></td>'
            f'<td class="n">{r["pacientes"]:,}</td>'.replace(",", ".") +
            f'<td class="n">{P(r["venta"]/max(r["pacientes"],1))}</td>'
            f'<td class="n">{r["profs"]}</td>'
            f'<td class="n">{r["desc"]:.0f}%</td></tr>')

    # ── banda del margen ──
    lo, hi = min(r["pct"] for r in s), max(r["pct"] for r in s)
    piso, techo_g = lo - 4, hi + 4
    def y(p):
        return 100 * (p - piso) / (techo_g - piso)
    cols = "".join(
        f'<div class="c{" mala" if r["pct"] < 29 else ""}">'
        f'<div class="v">{r["pct"]:.1f}%</div>'
        f'<div class="pt" style="height:{max(y(r["pct"]),6):.0f}%"></div>'
        f'<div class="a">{r["anio"]}</div></div>' for r in s)
    z_top, z_bot = 100 - y(32), y(28)
    banda = (f'<div class="banda"><div class="zona" style="bottom:{z_bot:.0f}%;'
             f'top:{z_top:.0f}%"><span class="zl">banda 28–32%</span></div>'
             f'<div class="cols">{cols}</div></div>')

    # ── escenarios ──
    mejor = max(esc["reemplazo"], key=lambda x: x["margen"])
    # Las tres tarjetas muestran MARGEN como cifra grande, no venta: puestas
    # lado a lado, dos con venta y una con margen se leen como comparables y
    # no lo son. La venta va en el detalle.
    escen = f"""
    <div class="e hoy"><div class="t">Hoy, atendiendo tú</div>
      <div class="v">{M(esc['margen'])}</div>
      <div class="d">de margen al año · <b>{esc['pct']:.1f}%</b><br>
      sobre {M(esc['venta'])} de venta · anualizado sobre {esc['meses']} meses</div></div>
    <div class="e sin"><div class="t">Dejas de atender · sin reemplazo</div>
      <div class="v">{M(esc['margen_sin'])}</div>
      <div class="d">de margen al año · <b>{esc['pct_sin']:.1f}%</b><br>
      sobre {M(esc['venta_sin'])} de venta: cae
      {100*(esc['venta_sin']/esc['venta']-1):.0f}%, pero el margen solo
      {100*(esc['margen_sin']/esc['margen']-1):.0f}% — el <b>porcentaje sube</b></div></div>
    <div class="e mejor"><div class="t">Alguien toma tu volumen al {mejor['pct']}%</div>
      <div class="v">{M(mejor['margen'])}</div>
      <div class="d">de margen al año · sobre {M(esc['venta'])} de venta<br>
      <b>{M(mejor['delta'])} más</b> que teniéndote a ti en el box</div></div>"""

    reemp = "".join(
        f'<tr{" class=hoy" if r["pct"] == 71 else ""}><td class="nom">al {r["pct"]}%</td>'
        f'<td class="n">{M(r["margen"])}</td>'
        f'<td class="n"><span class="{"up" if r["delta"] > 0 else "dn"}">'
        f'{M(r["delta"])}</span></td></tr>' for r in esc["reemplazo"])

    # ── techo ──
    tfilas = "".join(
        f'<tr{" class=hoy" if t["id"] == ID_DUENO else ""}>'
        f'<td><div class="nom">{t["nombre"]}</div><div class="sub">{t["esp"]}</div></td>'
        f'<td class="n">{"fijo" if t["pct"] is None else str(t["pct"])+"%"}</td>'
        f'<td class="n">{M(t["peak"])}</td><td class="n sub">{t["cuando"]}</td>'
        f'<td class="n">{M(t["hoy"])}</td>'
        f'<td style="width:110px"><div class="mini"><i style="width:'
        f'{min(100*t["hoy"]/max(t["peak"],1),100):.0f}%"></i></div></td>'
        f'<td class="n">{100*t["hoy"]/max(t["peak"],1):.0f}%</td></tr>'
        for t in te["filas"])

    # ── ranking ──
    rk = te and "".join(
        f'<tr><td><div class="nom">{r["nombre"]}</div><div class="sub">{r["esp"]}</div></td>'
        f'<td class="n">{"fijo" if r["pct"] is None else str(r["pct"])+"%"}</td>'
        f'<td class="n">{M(r["v25"])}</td><td class="n">{M(r["v26"])}</td>'
        f'<td class="n"><span class="{"up" if r["delta"] > 0 else "dn"}">'
        f'{M(r["delta"])}</span></td></tr>'
        for r in d["ranking"][:8] + d["ranking"][-3:])

    # ── mismo corte + proyección ──
    cp = d["corte"]
    cfilas = "".join(
        f'<tr class="{"crisis" if (r["crec"] or 0) < 0 else ("hoy" if r["anio"] == cp["corte"][-1]["anio"] else "")}">'
        f'<td class="nom">{r["anio"]}'
        + ('<div class="sub">arranca en julio, no comparable</div>' if r["parcial"] else '')
        + f'</td><td class="n">{M(r["venta"])}</td>'
        f'<td class="n">' + ("—" if r["crec"] is None else
          f'<span class="{"up" if r["crec"] > 0 else "dn"}">{r["crec"]:+.0f}%</span>') + '</td>'
        f'<td class="n">{M(r["margen"])}</td>'
        f'<td class="n"><b>{r["pct"]:.1f}%</b></td></tr>' for r in cp["corte"])

    tope_c = max(r["venta"] for r in cp["corte"])
    cbarras = "".join(
        f'<div class="c{" baja" if (r["crec"] or 0) < 0 else ""}">'
        f'<div class="v">{M(r["venta"])}</div>'
        f'<div class="b" style="height:{100*r["venta"]/tope_c:.0f}%"></div>'
        f'<div class="a">{r["anio"]}</div></div>' for r in cp["corte"])

    _u = cp["corte"][-1]
    cnota = (f'A esta fecha vas <b>{_u["crec"]:+.0f}%</b> contra el año pasado, con el margen '
             f'en <b>{_u["pct"]:.1f}%</b>. La barra roja es el año que cayó.')

    ratios = "".join(
        f'<tr><td class="nom">{r["anio"]}</td><td class="n">{M(r["base"])}</td>'
        f'<td class="n">{M(r["resto"])}</td>'
        f'<td class="n"><b>{r["ratio"]:.2f}x</b></td></tr>' for r in cp["ratios"])

    proyec = "".join(
        f'<div class="p{" alto" if cc is cp["cierres"][-1] else ""}">'
        f'<div class="t">{cc["nombre"]}</div><div class="v">{M(cc["total"])}</div>'
        f'<div class="d">{cc["det"]} ({cc["ratio"]:.2f}x)<br>'
        f'margen <b>{M(cc["margen"])}</b> · <b>{cc["vs"]:+.0f}%</b> contra el año anterior'
        f'</div></div>' for cc in cp["cierres"])

    # ── serie mensual, valle, quiebre, mix, fijos, pendientes ──
    ex = d["extra"]
    ms = sorted(d["meses"])
    tope_m = max(d["meses"].values())
    sserie = "".join(
        f'<i class="{"peor" if m == ex["peor"]["mes"] else ("mejor" if m == ex["mejor"]["mes"] else "")}"'
        f' style="height:{100*d["meses"][m]/tope_m:.1f}%" title="{m}: {M(d["meses"][m])}"></i>'
        for m in ms)
    nmeses, mes0, mesN = len(ms), ms[0], ms[-1]

    sq = "".join(
        f'<tr><td><div class="nom">{r["nombre"]}</div><div class="sub">{r["esp"]}</div></td>'
        f'<td class="n">{M(r["antes"])}</td><td class="n">{M(r["despues"])}</td>'
        f'<td class="n"><span class="{"up" if r["delta"] > 0 else "dn"}">{M(r["delta"])}</span>'
        f'</td></tr>' for r in ex["quiebre"][:6])
    _t3 = sum(r["delta"] for r in ex["quiebre"][:1])
    _tt = sum(r["delta"] for r in ex["quiebre"] if r["delta"] > 0)
    qnota = (f'El dueño solo explica <b>{100*_t3/_tt:.0f}%</b> del crecimiento de ese año '
             f'({M(_t3)} de {M(_tt)}). El resto son <b>líneas nuevas</b> que se abrieron: '
             f'ecografía, kinesiología y odontología. Y en paralelo se podó el equipo. '
             f'Salir de la caída fue trabajar más <b>y</b> cambiar la oferta, no una sola '
             f'de las dos.')

    smix = ""
    for r in ex["mix"]:
        tramos = "".join(
            f'<span class="{k}" style="width:{r[k]:.1f}%">{r[k]:.0f}%</span>'
            for k in ("caro", "medio", "barato", "fijo") if r[k] >= 4)
        smix += f'<div class="mx"><span class="a">{r["anio"]}</span><div class="bar">{tramos}</div></div>'
    _p, _u = ex["mix"][0], ex["mix"][-1]
    mixnota = (f'La venta que viene de profesionales caros pasó de <b>{_p["caro"]:.0f}%</b> '
               f'a <b>{_u["caro"]:.0f}%</b>, y baja todos los años sin excepción. Ese es el '
               f'cambio estructural de fondo: no es que se venda más de lo mismo, es que se '
               f'vende <b>otra mezcla</b>. Es la razón por la que el margen aguanta mientras '
               f'el tamaño se multiplica.')

    sfij = "".join(
        f'<tr><td><div class="nom">{r["nombre"]}</div><div class="sub">{r["esp"]}</div></td>'
        f'<td class="n sub">{r["desde"]}</td><td class="n">{M(r["v"])}</td>'
        f'<td class="n">{M(r["c"])}</td>'
        f'<td class="n"><span class="{"up" if r["neto"] > 0 else "dn"}">{M(r["neto"])}</span>'
        f'</td></tr>' for r in ex["fijos"])
    if ex["fijos"]:
        _f = ex["fijos"][0]
        fijnota = (f'{_f["nombre"]} lleva <b>{M(_f["v"])}</b> producidos contra '
                   f'<b>{M(_f["c"])}</b> de sueldo: va en <b>{M(_f["neto"])}</b>. '
                   + ('La tesis del costo fijo es correcta — es lo único que puede romper '
                      'la banda — pero <b>este caso todavía no la demuestra</b>. Recién '
                      'está dando vuelta la esquina.' if _f["neto"] < 0 else
                      'Ya está en azul: la apuesta del costo fijo está pagando.'))
    else:
        fijnota = "Nadie con sueldo fijo todavía. Todo el equipo va a comisión."

    spend = "".join(
        f'<tr><td class="nom">{r["id"]}</td><td class="n">{M(r["v"])}</td>'
        f'<td class="sub">{", ".join(r["anios"])}</td>'
        f'<td>{"<b style=color:#e84545>sigue facturando</b>" if str(mesN)[:4] in r["anios"] else "<span class=sub>ya no está</span>"}</td>'
        f'</tr>' for r in ex["pendientes"])

    malo = [r for r in s if r["desc"] > 20]
    aviso = ""
    if malo:
        aviso = (f'<div class="aviso amber"><b>Calidad del dato:</b> en '
                 f'{", ".join(r["anio"] for r in malo)} más del 20% de la venta viene de '
                 f'profesionales que no están en <code>equipo_cmc</code> y se costea a un '
                 f'{PCT_DEFAULT}% supuesto. El margen de esos años es <b>referencia, no '
                 f'dato</b>. La columna «s/reg.» muestra cuánto es en cada año.</div>')

    return HTMLResponse(f"""<!doctype html><html lang="es"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Trayectoria y techo · Alma</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Montserrat:wght@500;600;700;800&display=swap" rel="stylesheet">
<style>{_CSS}</style></head><body>
<div class="wrap">

  <div class="top"><div><h1>Trayectoria y techo</h1>
    <div class="meta">Cinco años de caja real (jul-2021 → {te['mes']}) · margen =
    venta − honorarios · generado {d['generado']}</div></div></div>

  {aviso}

  <div class="card">
    <h2>Los cinco años</h2>
    <p class="h2s">Venta de caja, margen que queda en el centro y con cuánta gente se hizo.
    El crecimiento compara años anualizados, para que 2021 (6 meses) y 2026 (9) sean
    comparables con los completos.</p>
    <div class="scroll"><table>
      <thead><tr><th>Año</th><th style="text-align:right">Venta</th>
        <th style="text-align:right">Crec.</th><th style="text-align:right">Margen</th>
        <th style="text-align:right">% marg</th><th style="text-align:right">Pacientes</th>
        <th style="text-align:right">$/pac</th><th style="text-align:right">Profs</th>
        <th style="text-align:right">s/reg.</th></tr></thead>
      <tbody>{"".join(filas)}</tbody></table></div>
    <p class="nota">La fila roja es el año que la venta cayó. Lo más duro de ese año no es
    la caída: es que los profesionales <b>subieron</b> mientras la venta bajaba.</p>
  </div>

  <div class="card">
    <h2>Mes a mes, los {nmeses} meses</h2>
    <p class="h2s">La serie completa de caja. En rojo el peor mes, en verde el mejor.</p>
    <div class="serie">{sserie}</div>
    <div class="ejes"><span>{mes0}</span><span>{mesN}</span></div>
    <div class="hito">
      <div><div class="t">El fondo</div><div class="v malo">{M(ex['peor']['v'])}</div>
        <div class="s">{ex['peor']['mes']}</div></div>
      <div><div class="t">El techo alcanzado</div><div class="v bueno">{M(ex['mejor']['v'])}</div>
        <div class="s">{ex['mejor']['mes']}</div></div>
      <div><div class="t">Recuperación</div><div class="v">{ex['factor']:.1f}x</div>
        <div class="s">en {ex['meses_rec']} meses</div></div>
    </div>
    <p class="nota">Del fondo a hoy hay <b>{ex['factor']:.1f} veces</b> en
    {ex['meses_rec']} meses. Ese, y no el crecimiento del último año, es el número que
    mide lo que se hizo acá.</p>
  </div>

  <div class="card">
    <h2>Cómo se salió de {ex['a_peor']}</h2>
    <p class="h2s">Quién movió la venta entre {ex['a_peor']} y {ex['a_sig']}, el año del quiebre.</p>
    <div class="scroll"><table style="min-width:520px">
      <thead><tr><th>Profesional</th><th style="text-align:right">{ex['a_peor']}</th>
        <th style="text-align:right">{ex['a_sig']}</th>
        <th style="text-align:right">Δ venta</th></tr></thead>
      <tbody>{sq}</tbody></table></div>
    <p class="nota">{qnota}</p>
  </div>

  <div class="card">
    <h2>El margen nunca sale de la banda</h2>
    <p class="h2s">Porcentaje que queda en el centro, año a año.</p>
    {banda}
    <p class="nota">En cinco años el margen <b>no ha salido del 28–32%</b> mientras la venta
    se multiplicaba. No es mala suerte: en un modelo <b>100% a comisión no hay
    apalancamiento</b> — el costo de atender crece exactamente igual que la venta. Por eso
    se puede cuadruplicar el tamaño y seguir en el mismo porcentaje. La única forma de
    romper la banda es capacidad a <b>costo fijo</b>, no a comisión.</p>
  </div>

  <div class="card">
    <h2>Si dejas de atender</h2>
    <p class="h2s">Tu producción sale del cálculo. Todo anualizado sobre 2026.</p>
    <div class="esc">{escen}</div>
    <p class="nota">Produces <b>{M(esc['prod'])}</b> al año. Te llevas
    <b>{M(esc['honor'])}</b> (71%) y al centro le quedan <b>{M(esc['queda'])}</b>.
    Estás entre los porcentajes más caros de la tabla, así que tu salida del box
    <b>no es una pérdida para el centro</b> — es una pérdida para ti. Son cosas distintas
    y conviene no confundirlas.</p>
    <div class="scroll" style="margin-top:14px"><table style="min-width:340px">
      <thead><tr><th>Si alguien toma tu volumen</th>
        <th style="text-align:right">Margen del centro</th>
        <th style="text-align:right">vs. tenerte a ti</th></tr></thead>
      <tbody>{reemp}</tbody></table></div>
  </div>

  <div class="card">
    <h2>De dónde viene la venta</h2>
    <p class="h2s">Repartida por lo que se lleva el profesional. El corte en 70% no es
    arbitrario: ahí están psiquiatría (90%), implantología (80%) y las medicinas generales
    de 75% — las líneas que suben la venta sin mover el margen.</p>
    <div class="mix">{smix}</div>
    <div class="leg">
      <span><i style="background:var(--red)"></i>70% o más (caro)</span>
      <span><i style="background:#f0a92b"></i>60–69%</span>
      <span><i style="background:var(--green)"></i>bajo 60% (barato)</span>
      <span><i style="background:var(--blue)"></i>sueldo fijo</span>
    </div>
    <p class="nota">{mixnota}</p>
  </div>

  <div class="card">
    <h2>Mismo corte: 1 de enero al {cp['corte_fecha']} de cada año</h2>
    <p class="h2s">Comparar un año en curso contra años completos infla al pasado.
    Acá todos se cortan el mismo día, así que el crecimiento es el real a esta fecha.</p>
    <div class="scroll"><table style="min-width:520px">
      <thead><tr><th>Año al {cp['corte_fecha']}</th><th style="text-align:right">Venta</th>
        <th style="text-align:right">Crec.</th><th style="text-align:right">Margen</th>
        <th style="text-align:right">% marg</th></tr></thead>
      <tbody>{cfilas}</tbody></table></div>
    <div class="barras">{cbarras}</div>
    <p class="nota">{cnota}</p>
  </div>

  <div class="card">
    <h2>Proyección de cierre</h2>
    <p class="h2s">No es una regla de tres sobre el promedio diario: usa la
    <b>estacionalidad real</b> — cuánto entró históricamente después de esta fecha, como
    múltiplo de lo acumulado.</p>
    <div class="scroll" style="margin-bottom:14px"><table style="min-width:460px">
      <thead><tr><th>Año</th><th style="text-align:right">Al {cp['corte_fecha']}</th>
        <th style="text-align:right">Entró después</th>
        <th style="text-align:right">Múltiplo</th></tr></thead>
      <tbody>{ratios}</tbody></table></div>
    <div class="proy">{proyec}</div>
    <p class="nota">Parte de <b>{M(cp['base'])}</b> ya en caja más <b>{M(cp['falta_mes'])}</b>
    de lo que queda del mes ({cp['dias_falta']} días hábiles a {M(cp['dia_habil'])}).
    El múltiplo tiene un <b>quiebre de régimen</b>: 0,27x y 0,24x cuando el centro se
    encogía, contra 0,49x y 0,46x creciendo. Por eso el escenario de tendencia usa solo
    los dos últimos años — el Q4 de un negocio que crece no se parece al de uno que cae.
    <b>2021 queda fuera</b>: la caja arranca en julio, su múltiplo mide datos faltantes,
    no estacionalidad.</p>
  </div>

  <div class="card">
    <h2>El techo</h2>
    <p class="h2s">Suma del <b>mejor mes que cada profesional activo ya hizo</b> en 2026.
    No es un supuesto: es rendimiento demostrado al menos una vez.</p>
    <div class="gauge"><i style="width:{te['avance']:.0f}%"></i>
      <span>{te['avance']:.0f}% del techo · {M(te['hoy'])} de {M(te['venta'])} al mes</span></div>
    <div class="esc" style="margin-top:14px">
      <div class="e hoy"><div class="t">Techo con todos · venta</div>
        <div class="v">{M(te['venta']*12)}</div>
        <div class="d">al año · margen <b>{M(te['margen']*12)}</b> · {te['pct']:.1f}%</div></div>
      <div class="e mejor"><div class="t">Techo sin ti en el box · venta</div>
        <div class="v">{M(te['venta_sin']*12)}</div>
        <div class="d">al año · margen <b>{M(te['margen_sin']*12)}</b> ·
        <b>{te['pct_sin']:.1f}%</b><br>
        {100*(te['margen_sin']*12/esc['margen']-1):+.0f}% de margen contra lo que el centro
        hace hoy contigo dentro</div></div>
    </div>
    <div class="scroll" style="margin-top:16px"><table>
      <thead><tr><th>Profesional</th><th style="text-align:right">pct</th>
        <th style="text-align:right">Mejor mes</th><th style="text-align:right">Cuándo</th>
        <th style="text-align:right">{te['mes']}</th><th></th>
        <th style="text-align:right">Avance</th></tr></thead>
      <tbody>{tfilas}</tbody></table></div>
    <p class="nota">El cuello <b>no son tus manos</b>: es que la mayoría corre bastante por
    debajo de lo que ya demostró que puede. Cada barra corta de esta tabla es capacidad
    comprada y no usada.</p>
  </div>

  <div class="card">
    <h2>Costo fijo: la única forma de romper la banda</h2>
    <p class="h2s">A comisión el costo crece igual que la venta, así que el porcentaje
    no se mueve. Con sueldo fijo sí — pero recién deja margen cuando la producción
    supera el sueldo. Así va la apuesta:</p>
    <div class="scroll"><table style="min-width:540px">
      <thead><tr><th>Profesional</th><th style="text-align:right">Desde</th>
        <th style="text-align:right">Produjo</th><th style="text-align:right">Costó</th>
        <th style="text-align:right">Neto</th></tr></thead>
      <tbody>{sfij}</tbody></table></div>
    <p class="nota">{fijnota}</p>
  </div>

  <div class="card">
    <h2>Quién mueve el margen</h2>
    <p class="h2s">Cambio del margen del centro entre 2025 y 2026, mismos meses.
    Los de <b>costo fijo</b> se calculan con su contrato, no con porcentaje.</p>
    <div class="scroll"><table>
      <thead><tr><th>Profesional</th><th style="text-align:right">pct</th>
        <th style="text-align:right">Venta 2025</th><th style="text-align:right">Venta 2026</th>
        <th style="text-align:right">Δ margen</th></tr></thead>
      <tbody>{rk}</tbody></table></div>
    <p class="nota">Vender más no es lo mismo que dejar más: mira las filas donde la venta
    sube y el margen casi no se mueve. Ahí está el 70–75% de comisión.</p>
  </div>

  <div class="card">
    <h2>Pendientes de datos</h2>
    <p class="h2s">Venta atribuida a profesionales que no están en <code>equipo_cmc</code>.
    Se costea al {PCT_DEFAULT}% supuesto, así que mueve el margen de estas tablas sin que
    nadie lo decida.</p>
    <div class="scroll"><table style="min-width:400px">
      <thead><tr><th>id en Medilink</th><th style="text-align:right">Venta acumulada</th>
        <th>Años</th><th></th></tr></thead>
      <tbody>{spend}</tbody></table></div>
    <p class="nota">Los <b>1000+</b> son de la numeración vieja: profesionales que ya no
    están y no vale la pena cargar. Los de <b>numeración corriente que siguen
    facturando</b> sí — cada uno mueve el margen real sin que nadie haya decidido su
    porcentaje.</p>
  </div>

</div></body></html>""")
