"""Campañas Meta — valor a 12 meses por especialidad de entrada (2026-10-06).

Cohortes reales con 12 meses cumplidos, proyección de pacientes nuevos, nuevos vs
ya pacientes por anuncio/campaña, derivación entre especialidades, ortodoncia como
tratamiento, muestra chica y cero datos personales.

DB temporal con datos SINTÉTICOS. Sin llamadas a Medilink ni a Meta.
"""
import asyncio
import json
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import session  # noqa: E402

TMP = Path(tempfile.mkdtemp())
session.DB_PATH = TMP / "t.db"
with session.db() as _c:
    session._run_ddl_inline(_c)
    _c.commit()

import config  # noqa: E402
import campanas_meta_routes as cm  # noqa: E402
import campanas_meta_integraciones as ci  # noqa: E402
import valor_cohortes as vc  # noqa: E402
import medilink as ml  # noqa: E402
import httpx  # noqa: E402

FALLAS = []


def check(nombre, cond):
    print(("OK  " if cond else "FAIL") + " " + nombre)
    if not cond:
        FALLAS.append(nombre)


def cerca(a, b, tol=2):
    return a is not None and abs(a - b) <= tol


# Cualquier salida a Meta o a Medilink durante el panel es una falla.
_RED = []
_orig_client = httpx.Client


class _Espia(_orig_client):
    def get(self, url, *a, **k):
        if "graph.facebook.com" in str(url) or "healthatom" in str(url):
            _RED.append(str(url))
            raise httpx.ConnectError("sin red en tests")
        return super().get(url, *a, **k)


httpx.Client = _Espia
config.META_ACCESS_TOKEN = ""
config.OLACORE_TOKEN = "dueno_test"
config.ADMIN_TOKEN = "recepcion_test"
cm._estados_meta = lambda: {}
ml.PROFESIONALES = {1: {"nombre": "Dr Uno", "especialidad": "Medicina General", "intervalo": 15},
                    2: {"nombre": "Kine Dos", "especialidad": "Kinesiología", "intervalo": 40}}

CL = ZoneInfo("America/Santiago")
AHORA = datetime.now(CL).replace(hour=12, minute=0, second=0, microsecond=0)
HOY = AHORA.date()


def fecha(d):
    return (HOY + timedelta(days=d)).isoformat()


def utc(dias, mins=0):
    return (AHORA - timedelta(days=dias) + timedelta(minutes=mins)).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def ep(dias):
    return int((AHORA - timedelta(days=dias)).timestamp())


# ── A. Piezas puras ─────────────────────────────────────────────────────────
check("meses atrás: fin de mes seguro", vc._meses_atras(date(2026, 5, 31), 3) == date(2026, 2, 28)
      and vc._meses_atras(date(2026, 10, 6), 27) == date(2024, 7, 6))
check("especialidad: psicología unificada, vacío → None", vc._canon("Psicología Adulto") == "Psicología"
      and vc._canon("  Kinesiología ") == "Kinesiología" and vc._canon("") is None)
check("IC: con 1 dato no hay intervalo, con varios sí", vc._ic([5]) is None and vc._ic([10, 20, 30, 40]) is not None)
vacio = {"esp": {}}
check("proyección: sin cohorte devuelve lo pagado y fuente None", vc.proyectar(vacio, "X", 10, 100, 50) == (100, 50, None))
cum = [2, 2, 2, 4, 4, 4, 4]
check("derivación esperada: interpolación a los 15 días de 2/40 a los 30", abs(vc._frac(cum, 40, 15) - 0.025) < 1e-9
      and abs(vc._frac(cum, 40, 365) - 0.1) < 1e-9 and vc._frac(cum, 0, 10) == 0.0)

# ── B. Datos sintéticos ─────────────────────────────────────────────────────
with session.db() as c:
    c.execute("""CREATE TABLE IF NOT EXISTS equipo_cmc (id INTEGER PRIMARY KEY AUTOINCREMENT, id_medilink INTEGER,
        nombre TEXT NOT NULL, especialidad TEXT DEFAULT '', rol TEXT DEFAULT '', tipo_contrato TEXT DEFAULT 'honorarios',
        pct_honorario INTEGER DEFAULT 0, telefono TEXT DEFAULT '', email TEXT DEFAULT '', estado TEXT DEFAULT 'activo',
        licencia_desde TEXT DEFAULT '', licencia_hasta TEXT DEFAULT '', notas TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now')), updated_at TEXT DEFAULT (datetime('now')))""")
    for idm, nom, esp, pct in ((1, "Dr Uno", "Medicina General", 70), (2, "Kine Dos", "Kinesiología", 40),
                                (55, "Dra Odonto", "Odontología General", 50), (66, "Dra Orto", "Ortodoncia", 60)):
        c.execute("INSERT INTO equipo_cmc (id_medilink, nombre, especialidad, pct_honorario) VALUES (?,?,?,?)", (idm, nom, esp, pct))
    c.execute("""CREATE TABLE IF NOT EXISTS convenio_consumo (detalle_id INTEGER PRIMARY KEY, convenio TEXT NOT NULL DEFAULT 'imagendent',
        atencion_id INTEGER NOT NULL DEFAULT 0, fecha TEXT NOT NULL, id_paciente INTEGER, id_profesional INTEGER,
        costo INTEGER DEFAULT 0, venta INTEGER DEFAULT 0, cobrado INTEGER DEFAULT 0)""")
    # el profesional 99 NO está en equipo_cmc → "Sin especialidad registrada"
    n_pago = [100000]

    def pago(pid, dias, monto, prof):
        n_pago[0] += 1
        c.execute("INSERT INTO bi_pagos_caja (pago_id, fecha, id_paciente, id_profesional, monto) VALUES (?,?,?,?,?)",
                  (n_pago[0], fecha(-dias), pid, prof, monto))

    # Cohorte Medicina General: 40 pacientes, primer pago hace 400 días
    for i in range(40):
        pid = 7000000 + i
        pago(pid, 400, 20000, 1)
        if i < 10:
            pago(pid, 370, 10000, 1)                  # control a los 30 días
        if i < 2:
            pago(pid, 390, 40000, 2)                  # kine al día 10
        elif i < 4:
            pago(pid, 300, 40000, 2)                  # kine al día 100
        if i == 5:
            pago(pid, 395, 5000, 99)                  # profesional sin especialidad registrada
        if i == 6:
            pago(pid, 397, 25000, 1)                  # radiografía (Imagendent) cobrada por el profesional
            c.execute("INSERT INTO convenio_consumo (detalle_id, convenio, fecha, id_paciente, id_profesional, venta, cobrado, costo) "
                      "VALUES (1, 'imagendent', ?, ?, 1, 25000, 25000, 10000)", (fecha(-397), pid))
        if i == 39:
            pago(pid, 0, 99999, 1)                    # fuera de los 12 meses desde su primer pago: NO cuenta
    # Cohorte Kinesiología: 10 pacientes (muestra chica)
    for i in range(10):
        pago(7100000 + i, 380, 50000, 2)
    # Cohorte Odontología General: 30 pacientes; 6 instalan ortodoncia (id 66) al día 20
    for i in range(30):
        pid = 7200000 + i
        pago(pid, 400, 30000, 55)
        if i < 6:
            pago(pid, 380, 120000, 66)
            pago(pid, 340, 30000, 66)
    # Paciente antiguo (primer pago hace 900 días: fuera de la ventana) que instaló hace 500 días
    pago(7300000, 900, 30000, 55)
    pago(7300000, 500, 120000, 66)
    pago(7300000, 480, 30000, 66)
    # Fuera de la ventana: muy recientes (no tienen 12 meses)
    for i in range(5):
        pago(7400000 + i, 100, 99000, 1)
    c.commit()

with session.db() as c:
    _existe = c.execute("SELECT name FROM sqlite_master WHERE name='valor_cohorte_especialidad'").fetchone()
check("sin calcular: la tabla cache aún no existe", _existe is None)

# ── C. Cohortes ─────────────────────────────────────────────────────────────
with session.db() as c:
    v = vc.cargar(c)                   # calcula al vuelo y guarda
    tabla_n = c.execute("SELECT COUNT(*) FROM valor_cohorte_especialidad").fetchone()[0]
E = v["esp"]
check("al vuelo: se guardó en la tabla cache (una fila por especialidad + total)", tabla_n == len(E) >= 4)
mg = E["Medicina General"]
check("MG: n=40, no es muestra chica (≥30)", mg["n"] == 40 and mg["muestra_chica"] is False)
check("MG: ventana móvil — el paciente con pago fuera de 12 meses no suma, los recientes no entran",
      v["meta"]["n_total"] == 40 + 10 + 30 and v["meta"]["ventana"]["hasta"] == fecha(-365))
# venta: 20000*40 + 10*10000 (día 30) + 2*40000 (día 10) + 5000 + 25000 = 1.010.000 a 30 días
check("MG: venta media a 30 días = $25.250", cerca(mg["v"]["30"], 25250, 1))
check("MG: venta media a 365 días = $27.250 (incluye kine al día 100, excluye el pago fuera de plazo)", cerca(mg["v"]["365"], 27250, 1))
# centro: MG 0,3 · kine 0,6 · sin especialidad 0,3 (70% por defecto) · radiografía Imagendent: venta − costo
check("MG: centro a 365 días con honorario del profesional e Imagendent = $9.562",
      cerca(mg["c"]["365"], 9562.5, 1))
check("MG: curva acumulada empieza en el primer pago y termina en el valor a 12 meses",
      mg["curva_v"][0] == 20000 and mg["curva_v"][365] == mg["v"]["365"] and len(mg["curva_v"]) == 366
      and all(mg["curva_c"][i] <= mg["curva_c"][i + 1] for i in range(365)))
check("MG: visitas por paciente = 1,4", mg["visitas"] == 1.4)
check("MG: valor en la propia especialidad vs otras",
      mg["v365_entrada"] + mg["v365_otras"] == mg["v"]["365"] and cerca(mg["v365_otras"], 4 * 40000 / 40 + 5000 / 40, 1))
check("MG: intervalo de confianza del valor a 12 meses", mg["ic_v365"] and mg["ic_v365"][0] < mg["v"]["365"] < mg["ic_v365"][1])
kn = E["Kinesiología"]
check("Kine: n=10 → muestra chica", kn["n"] == 10 and kn["muestra_chica"] is True)
check("Odonto general: n=30 (justo el mínimo) no es muestra chica", E["Odontología General"]["n"] == 30
      and E["Odontología General"]["muestra_chica"] is False)
check("Total de todas las especialidades", E[vc.TODAS]["n"] == 80)

# derivación
dv = {x["esp"]: x for x in mg["deriv"]}
check("derivación MG→Kinesiología: 10% pasa, $40.000 por quien pasa, aporte $4.000 por paciente de entrada",
      dv["Kinesiología"]["n"] == 4 and dv["Kinesiología"]["pct"] == 10.0 and dv["Kinesiología"]["venta_pasan"] == 40000
      and dv["Kinesiología"]["aporte_v"] == 4000)
check("derivación: curva de cuántos pasaron a los 30/60/90/120 días", dv["Kinesiología"]["cum"] == [2, 2, 2, 4, 4, 4, 4])
check("derivación: el profesional sin especialidad cae en 'Sin especialidad registrada'",
      dv[vc.SIN_ESP]["n"] == 1 and dv[vc.SIN_ESP]["muestra_chica"] is True)
check("derivación: la propia especialidad de entrada no cuenta como 'pasó'", "Medicina General" not in dv)
check("sin especialidad: se lista el id para que el dueño lo complete",
      [x["id"] for x in v["meta"]["sin_especialidad"]] == [99] and v["meta"]["sin_especialidad"][0]["venta"] == 5000)
p1 = next(x for x in v["meta"]["profesionales"] if x["id"] == 1)
check("por profesional de entrada: tasa a 12 meses (5 de 40 pagaron en otra especialidad = 12,5%)",
      p1["n"] == 40 and p1["pasan"] == 5 and p1["pct"] == 12.5 and p1["destinos"][0]["esp"] == "Kinesiología")
check("por profesional: sin datos personales (solo id, nombre del profesional y cifras)",
      set(p1) == {"id", "nombre", "especialidad", "n", "pasan", "pct", "muestra_chica", "destinos"})

# ortodoncia
o = v["meta"]["orto"]
check("orto: 6 de 80 pacientes nuevos instalaron en 12 meses, todos con entrada por odontología general",
      o["entrada"]["instalan"] == 6 and o["entrada"]["n_cohorte"] == 80
      and o["entrada"]["por_entrada"][0] == {"esp": "Odontología General", "n": 30, "instalan": 6, "pct": 20.0})
check("orto: valor de quien instala a 12 meses = $180.000 (ortodoncia $150.000), para el centro $75.000",
      o["entrada"]["v365"] == 180000 and o["entrada"]["v365_orto"] == 150000 and o["entrada"]["c365"] == 75000)
check("orto: desde la instalación (7 pacientes, 6 nuevos + 1 antiguo) $150.000 en 12 meses",
      o["desde_instalacion"]["n"] == 7 and o["desde_instalacion"]["v365"] == 150000
      and o["desde_instalacion"]["c365"] == 60000 and o["desde_instalacion"]["muestra_chica"] is True)
check("orto: el criterio queda documentado", "ortodoncista" in o["criterio"])

# vista pública
with session.db() as c:
    pub = vc.publico(c)
check("público: sin curvas diarias ni arreglos largos", all("curva_v" not in e for e in pub["especialidades"])
      and pub["titulo_matriz"] == "Dónde sigue el recorrido")
check("público: matriz entrada→derivada y nota de especialidades sin registrar",
      next(m for m in pub["matriz"] if m["entrada"] == "Medicina General")["destinos"][0]["esp"] == "Kinesiología"
      and pub["sin_especialidad"][0]["id"] == 99)

# ── D. Anuncios: nuevos vs ya pacientes, proyección, recorrido ──────────────
with session.db() as c:
    cm._ensure_insights(c)
    c.execute("INSERT INTO meta_insights_diario (fecha, ad_id, desglose, valor, ad_name, campaign_id, campaign_name, spend, "
              "impressions, reach, frequency, clicks, conversaciones) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (fecha(-5), "AD1", "total", "-", "Medicina General hoy", "C1", "Campaña MG", 100000, 10000, 5000, 2, 100, 20))
    c.execute("INSERT INTO meta_insights_diario (fecha, ad_id, desglose, valor, ad_name, campaign_id, campaign_name, spend, "
              "impressions, reach, frequency, clicks, conversaciones) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (fecha(-5), "AD2", "total", "-", "Kinesiología ya", "C2", "Campaña Kine", 50000, 5000, 2500, 2, 50, 10))

    def ref(ph, ad, dias):
        c.execute("INSERT INTO meta_referrals (phone, source_id, headline, ts, plataforma) VALUES (?,?,?,?,?)",
                  (ph, ad, "h", ep(dias), "facebook"))

    def cita(ph, idc, pid, dias, ad, esp="Medicina General"):
        c.execute("INSERT INTO citas_bot (phone, id_cita, especialidad, profesional, fecha, hora, created_at, ad_source_id, "
                  "ad_plataforma, id_paciente_medilink) VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (ph, idc, esp, "Dr", fecha(-dias + 2), "10:00", utc(dias, 30), ad, "facebook", pid))

    # 3 pacientes NUEVOS de AD1 (medicina general): primer pago hace 18 días, 20.000
    for i in range(3):
        ph = f"5691110000{i}"
        ref(ph, "AD1", 20)
        cita(ph, f"A{i}", 8100000 + i, 20, "AD1")
        pago(8100000 + i, 18, 20000, 1)
    pago(8100000, 10, 30000, 2)                       # el primero además fue a kinesiología
    # 2 que YA ERAN pacientes: pagaron hace 200 días y vuelven por el anuncio (30.000 cada uno)
    for i in range(2):
        ph = f"5692220000{i}"
        ref(ph, "AD1", 20)
        cita(ph, f"B{i}", 8200000 + i, 20, "AD1")
        pago(8200000 + i, 200, 15000, 1)
        pago(8200000 + i, 15, 30000, 1)
    # AD2: 1 nuevo que entra por kinesiología (cohorte chica → usa la global)
    ref("56933300000", "AD2", 12)
    cita("56933300000", "C0", 8300000, 12, "AD2", "Kinesiología")
    pago(8300000, 10, 40000, 2)
    # 2 pacientes nuevos del centro que NO vienen de anuncios
    pago(8400000, 5, 20000, 1)
    pago(8400001, 3, 20000, 1)
    c.commit()

D30 = fecha(-29)
n_antes = len(_RED)
# el panel solo LEE la tabla: borrar los pagos de la cohorte NO cambia sus valores
with session.db() as c:
    c.execute("DELETE FROM bi_pagos_caja WHERE id_paciente BETWEEN 7000000 AND 7099999")
    c.commit()
p = cm.panel_data(D30, fecha(0))
check("panel: no sale a Meta ni a Medilink", len(_RED) == n_antes)
with session.db() as c:
    mg2 = vc.cargar(c)["esp"]["Medicina General"]
check("panel: lee la cohorte de la cache, no recalcula en cada carga", mg2["n"] == 40 and mg2["v"]["365"] == mg["v"]["365"])

a1 = next(x for x in p["anuncios"] if x["ad_id"] == "AD1")["valor12m"]
check("anuncio: 3 nuevos y 2 que ya eran pacientes", a1["nuevos"] == 3 and a1["ya"] == 2)
check("anuncio: venta de cada grupo", a1["nuevos_venta"] == 3 * 20000 + 30000 and a1["ya_venta"] == 60000)
check("anuncio: para el centro de cada grupo (MG 30%, kine 60%)", cerca(a1["nuevos_centro"], 3 * 6000 + 18000, 1)
      and a1["ya_centro"] == 18000)
check("anuncio: costo por paciente nuevo = gasto ÷ nuevos", a1["costo_nuevo"] == round(100000 / 3))
# proyección: lo pagado + (valor a 12 m − valor de la cohorte a su edad). Edad 18 días.
add_c = mg["c"]["365"] - mg["curva_c"][18]
paga_c = 3 * 6000 + 18000
check("proyección: pagado + lo que falta según la cohorte MG a los 18 días", cerca(a1["proy_centro"], paga_c + 3 * add_c, 4))
check("proyección: los que ya eran pacientes NO se proyectan (solo lo pagado, aparte)",
      cerca(a1["centro_12m"], a1["proy_centro"] + 18000, 1) and a1["ya_centro"] == 18000)
check("retorno a 12 meses = centro proyectado + ya pagado ÷ gasto",
      a1["retorno_12m"] == round(a1["centro_12m"] / 100000, 2))
check("proyección ≥ lo ya pagado", a1["proy_centro"] >= a1["nuevos_centro"] and a1["proy_venta"] >= a1["nuevos_venta"])
check("anuncio: ya pacientes no inflan el costo por nuevo", a1["costo_nuevo"] == round(100000 / 3))
a2 = next(x for x in p["anuncios"] if x["ad_id"] == "AD2")["valor12m"]
check("kine: cohorte propia chica (10) → proyecta con la global y lo declara", a2["nuevos"] == 1 and a2["n_cohorte_global"] == 1
      and a2["n_sin_cohorte"] == 0 and a2["muestra_chica"] is True)
g = E[vc.TODAS]
check("kine: proyección con la curva de todas las especialidades a los 10 días",
      cerca(a2["proy_centro"], 40000 * 0.6 + g["c"]["365"] - g["curva_c"][10], 3))

# recorrido
rec = a1["recorrido"]
check("recorrido: entrada MG con 3 pacientes", len(rec) == 1 and rec[0]["entrada"] == "Medicina General" and rec[0]["n"] == 3)
k_ = next(x for x in rec[0]["pasaron"] if x["esp"] == "Kinesiología")
check("recorrido: a kinesiología pasó 1 de 3 ($30.000), esperado por la matriz según su edad",
      k_["n"] == 1 and k_["venta"] == 30000 and k_["pct"] == 33.3 and cerca(k_["esperado_n"], 3 * 0.05 * 18 / 30, 0.06)
      and k_["venta_pasan_cohorte"] == 40000)
check("recorrido: no es muestra chica de cohorte, sí de anuncio (3 nuevos)", rec[0]["muestra_chica"] is True)

# campaña y total
c1 = next(x for x in p["campanas"] if x["campaign_id"] == "C1")["valor12m"]
check("campaña: suma lo de sus anuncios", c1["nuevos"] == a1["nuevos"] and c1["proy_centro"] == a1["proy_centro"]
      and c1["recorrido"][0]["n"] == 3)
kt = p["kpis"]["valor12m"]
check("KPI: nuevos totales = AD1 + AD2", kt["nuevos"] == 4 and kt["ya"] == 2)
check("KPI de cabecera: N de M nuevos del rango (los 4 de anuncios + 2 sin anuncio)",
      kt["nuevos_rango"] == 4 and kt["nuevos_centro_rango"] == 6 and kt["pct_nuevos_anuncios"] == 66.7)
check("KPI: costo por nuevo = gasto total ÷ nuevos", kt["costo_nuevo"] == round(150000 / 4) and kt["costo_nuevo_rango"] == round(150000 / 4))
check("meta de cohortes y nota de proyección en la respuesta", p["valor12m"]["hay_cohortes"] is True
      and "no es venta realizada" in p["valor12m"]["nota"])

# retorno estricto y completo conviven
fa = next(x for x in p["anuncios"] if x["ad_id"] == "AD1")
check("conviven retorno estricto, completo y a 12 meses", "retorno_estricto" in fa and "retorno_centro" in fa
      and fa["valor12m"]["retorno_12m"] is not None)

# sin datos personales
dump = json.dumps(p) + json.dumps(pub)
check("sin datos personales: ni teléfonos ni ids de paciente ni pagos sueltos",
      not any(s in dump for s in ("5691110000", "5692220000", "56933300000", "8100000", "8200000", "8300000", "7000000", "7100000"))
      and '"pagos": [' not in dump)

# ── E. Sin cohortes: el panel no se rompe ───────────────────────────────────
with session.db() as c:
    c.execute("DELETE FROM valor_cohorte_especialidad")
    c.execute("DELETE FROM valor_cohorte_estado")
    c.execute("DELETE FROM bi_pagos_caja WHERE id_paciente BETWEEN 7000000 AND 7999999")
    c.commit()
p0 = cm.panel_data(D30, fecha(0))
a1b = next(x for x in p0["anuncios"] if x["ad_id"] == "AD1")["valor12m"]
check("sin cohortes: se proyecta lo pagado y se declara sin cohorte (no se inventa)",
      p0["valor12m"]["hay_cohortes"] is False and a1b["n_sin_cohorte"] == 3 and a1b["proy_centro"] == a1b["nuevos_centro"])

# ── F. Job mensual, flag y endpoint ─────────────────────────────────────────
config.VALOR_COHORTES_ACTIVE = False
with session.db() as c:
    c.execute("DELETE FROM valor_cohorte_estado")
    c.commit()
asyncio.run(vc.job_valor_cohortes())
with session.db() as c:
    apagado = c.execute("SELECT COUNT(*) FROM valor_cohorte_estado").fetchone()[0]
check("job: con el flag apagado no recalcula", apagado == 0)
config.VALOR_COHORTES_ACTIVE = True
asyncio.run(vc.job_valor_cohortes())
with session.db() as c:
    meta_r = c.execute("SELECT valor FROM valor_cohorte_estado WHERE clave='meta'").fetchone()
check("job: con el flag encendido recalcula y guarda", meta_r is not None)
src = (Path(__file__).resolve().parent.parent / "app" / "main.py").read_text(encoding="utf-8")
check("job: registrado el día 1 a las 04:30 CLT con misfire_grace_time",
      "job_valor_cohortes" in src and "day=1, hour=4, minute=30" in src and 'id="valor_cohortes"' in src)

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

app = FastAPI()
app.include_router(cm.router)
cli = TestClient(app)
ruta = "/alma/api/campanas-meta/valor12m"
check("endpoint valor12m: 401 / 403 / 200", cli.get(ruta).status_code == 401 and cli.get(ruta + "?token=recepcion_test").status_code == 403
      and cli.get(ruta, headers={"Authorization": "Bearer dueno_test"}).status_code == 200)
check("endpoint recalcular: solo el dueño", cli.post(ruta + "/recalcular").status_code == 401
      and cli.post(ruta + "/recalcular", headers={"Authorization": "Bearer dueno_test"}).status_code == 200)
html = (Path(__file__).resolve().parent.parent / "templates" / "alma_campanas_meta.html").read_text(encoding="utf-8")
check("plantilla: derivación, nuevos vs ya, retorno 12 m y recorrido, sin CDN",
      all(x in html for x in ("Dónde sigue el recorrido", "valor12mHtml", "recorridoHtml", "nuevosKpi", "no es venta realizada"))
      and "cdn." not in html.lower() and "googleapis" not in html.lower())

# ── G. Alma Radar reutiliza panel_data: expone también el valor 12 m ────────
import radar_routes as rr  # noqa: E402
rr.limpiar_cache()
cap = rr.captacion_data(HOY)
check("radar: captación trae valor12m (cohortes) sin error", isinstance(cap.get("valor12m"), dict) and "error" not in cap["valor12m"]
      and "top" in cap["valor12m"] and "meta" in cap["valor12m"])
check("radar: el embudo de Meta incluye los nuevos vs ya pacientes", "valor12m" in cap["embudo"]["meta"]
      and "nuevos" in (cap["embudo"]["meta"]["valor12m"] or {}))
rhtml = (Path(__file__).resolve().parent.parent / "templates" / "alma_radar.html").read_text(encoding="utf-8")
check("radar: plantilla muestra el valor a 12 meses con la nota de proyección", "Valor de un paciente nuevo en 12 meses" in rhtml
      and "no es venta realizada" in rhtml)

print(f"\n{len(FALLAS)} fallas")
sys.exit(1 if FALLAS else 0)
