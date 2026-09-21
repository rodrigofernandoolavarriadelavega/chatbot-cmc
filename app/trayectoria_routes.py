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
from datetime import date, datetime
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

    return {"serie": serie, "escenario": escen, "ranking": ranking,
            "techo": {"filas": techo, "venta": tv, "margen": tg, "pct": 100*tg/tv,
                      "venta_sin": sv, "margen_sin": sg, "pct_sin": 100*sg/sv,
                      "hoy": hoy_v, "avance": 100*hoy_v/tv, "mes": ult},
            "meses": meses, "generado": datetime.now(_CL).strftime("%d-%m-%Y %H:%M")}


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
    escen = f"""
    <div class="e hoy"><div class="t">Hoy, atendiendo tú</div>
      <div class="v">{M(esc['venta'])}</div>
      <div class="d">margen <b>{M(esc['margen'])}</b> · {esc['pct']:.1f}%<br>
      anualizado sobre {esc['meses']} meses de 2026</div></div>
    <div class="e sin"><div class="t">Dejas de atender · sin reemplazo</div>
      <div class="v">{M(esc['venta_sin'])}</div>
      <div class="d">margen <b>{M(esc['margen_sin'])}</b> ·
      <b>{esc['pct_sin']:.1f}%</b><br>la venta cae
      {100*(esc['venta_sin']/esc['venta']-1):.0f}% y el margen
      {100*(esc['margen_sin']/esc['margen']-1):.0f}% — el <b>porcentaje sube</b></div></div>
    <div class="e mejor"><div class="t">Reemplazo al {mejor['pct']}%</div>
      <div class="v">{M(mejor['margen'])}</div>
      <div class="d">tomando tu mismo volumen<br>
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
    <h2>El techo</h2>
    <p class="h2s">Suma del <b>mejor mes que cada profesional activo ya hizo</b> en 2026.
    No es un supuesto: es rendimiento demostrado al menos una vez.</p>
    <div class="gauge"><i style="width:{te['avance']:.0f}%"></i>
      <span>{te['avance']:.0f}% del techo · {M(te['hoy'])} de {M(te['venta'])} al mes</span></div>
    <div class="esc" style="margin-top:14px">
      <div class="e hoy"><div class="t">Techo con todos</div>
        <div class="v">{M(te['venta']*12)}</div>
        <div class="d">al año · margen <b>{M(te['margen']*12)}</b> · {te['pct']:.1f}%</div></div>
      <div class="e mejor"><div class="t">Techo sin ti en el box</div>
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

</div></body></html>""")
