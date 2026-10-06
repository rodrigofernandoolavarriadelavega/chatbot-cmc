"""Alma Radar v2 (2026-10-06): portada con oportunidades, puente «¿Qué queda en
el centro?» (módulo EBITDA), centro de datos, laboratorio con escenarios y
embudo por plataforma + nuevos sin canal.

DB temporal con datos SINTÉTICOS. Ninguna llamada a Medilink ni a Meta (un
cliente espía falla el test si algo intenta salir a la red).
"""
import json
import math
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "app"))

import session  # noqa: E402

TMP = Path(tempfile.mkdtemp())
session.DB_PATH = TMP / "t.db"
with session.db() as _c:
    session._run_ddl_inline(_c)
    _c.commit()

import config  # noqa: E402
import campanas_meta_routes as cm  # noqa: E402
import campanas_meta_integraciones as ci  # noqa: E402
import ausentismo  # noqa: E402
import radar_routes as rr  # noqa: E402
import radar_v2 as v2  # noqa: E402
import ebitda_routes as eb  # noqa: E402

FALLAS = []


def check(nombre, cond):
    print(("OK  " if cond else "FAIL") + " " + nombre)
    if not cond:
        FALLAS.append(nombre)


config.OLACORE_TOKEN = "dueno_test"
config.ADMIN_TOKEN = "recepcion_test"
config.ADMIN_ALERT_PHONE = "56900000000"
config.META_ACCESS_TOKEN = "tok_falso"   # con token, el EBITDA en vivo INTENTARÍA llamar a Meta

_RED = []
import httpx  # noqa: E402
_orig_client = httpx.Client


class _Espia(_orig_client):
    def send(self, request, *a, **k):
        u = str(request.url)
        if any(h in u for h in ("graph.facebook.com", "healthatom", "googleapis")):
            _RED.append(u)
            raise httpx.ConnectError("sin red en tests")
        return super().send(request, *a, **k)


httpx.Client = _Espia
_orig_get = httpx.get
httpx.get = lambda url, *a, **k: (_RED.append(str(url)), (_ for _ in ()).throw(httpx.ConnectError("sin red")))[1]

CL = ZoneInfo("America/Santiago")
AHORA = datetime.now(CL).replace(hour=11, minute=30, second=0, microsecond=0)
HOY = AHORA.date()
rr._ahora = lambda: AHORA


def utc(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

app = FastAPI()
app.include_router(cm.router)
app.include_router(rr.router)
app.include_router(rr.pagina)
cli = TestClient(app)
B = "/alma/api/radar/"
H_ = {"Authorization": "Bearer dueno_test"}

# ── 1. Base vacía: nada se inventa ─────────────────────────────────────────
ausentismo.ensure_ausentismo_table()
import equipo_routes  # noqa: E402
import pagos_routes  # noqa: E402
import finanzas_routes  # noqa: E402
equipo_routes.ensure_table()
pagos_routes.ensure_pagos_table()
finanzas_routes.ensure_table()

for r in ("inicio", "datos", "laboratorio"):
    res = cli.get(B + r, headers=H_)
    check(f"vacío: {r} 200", res.status_code == 200)
iv = cli.get(B + "inicio", headers=H_).json()
ops0 = iv["oportunidades"]["oportunidades"]
check("vacío: sin oportunidades inventadas", ops0 == [])
sin0 = {s["clave"] for s in iv["oportunidades"]["sin_datos"]}
check("vacío: agenda vacía declarada como fuente sin datos (no 0 cupos)", "cupos" in sin0)
check("vacío: atenciones no cerradas sin tabla → declarado, sin monto", "no_cerradas" in sin0)
pu0 = iv["puente"]
check("vacío: puente sin costos se corta en el aporte", pu0["anterior"]["hay_costos"] is False
      and pu0["anterior"]["resultado"] is None)
dv = cli.get(B + "datos", headers=H_).json()
est0 = {p["clave"]: p["estado"] for p in dv["procesos"]}
check("vacío: agenda_cupos_cache vacía → rojo", est0["agenda"] == "rojo")
check("vacío: capi sin tabla → rojo", est0["capi"] == "rojo")
check("vacío: resumen N de M", dv["total"] == len(dv["procesos"]) and dv["al_dia"] == 0)
rr.limpiar_cache()

# ── 2. Auth ────────────────────────────────────────────────────────────────
for r in ("inicio", "datos", "laboratorio"):
    check(f"auth {r}: 401 sin token", cli.get(B + r).status_code == 401)
    check(f"auth {r}: 403 con token de recepción", cli.get(B + r + "?token=recepcion_test").status_code == 403)
ESC = {"nombre": "Kine rápido", "params": {"grupo": "Kinesiología", "presupuesto": 40000, "d_conv": 5, "d_asist": 3,
                                           "d_valor": 0, "capacidad": True, "resultado": {"atendidos": 2.5, "valor": 50000}}}
check("POST escenario: 401 sin token", cli.post(B + "escenarios", json=ESC).status_code == 401)
check("POST escenario: 403 con token de recepción", cli.post(B + "escenarios?token=recepcion_test", json=ESC).status_code == 403)
r1 = cli.post(B + "escenarios", json=ESC, headers=H_)
check("POST escenario: 200 con el dueño", r1.status_code == 200 and r1.json()["ok"])
eid = r1.json()["id"]
check("POST escenario: valida rangos (400)", cli.post(B + "escenarios", headers=H_, json={
    "nombre": "x", "params": {"grupo": "Dental", "presupuesto": -5}}).status_code == 400)
check("POST escenario: exige nombre (400)", cli.post(B + "escenarios", headers=H_, json={
    "nombre": "  ", "params": {"grupo": "Dental", "presupuesto": 1000}}).status_code == 400)
check("POST escenario: rechaza NaN/infinito", cli.post(B + "escenarios", headers=H_, content=json.dumps(
    {"nombre": "x", "params": {"grupo": "Dental", "presupuesto": "inf"}}), ).status_code in (400, 401))
r2 = cli.post(B + "escenarios", headers=H_, json={**ESC, "nombre": "Kine <script>alert(1)</script> grande",
                                                 "params": {**ESC["params"], "presupuesto": 80000}})
check("POST escenario: segundo escenario", r2.status_code == 200)
lst = v2.escenarios_lista()
check("escenarios: listados más reciente primero", [x["id"] for x in lst] == [r2.json()["id"], eid])
check("escenarios: guardan parámetros y resultado", lst[1]["params"]["d_conv"] == 5 and lst[1]["params"]["resultado"]["valor"] == 50000)
check("escenarios: claves extra se descartan", set(lst[1]["params"]) <= {"grupo", "capacidad", "presupuesto", "d_conv",
                                                                         "d_asist", "d_valor", "resultado"})
check("DELETE escenario: 403 recepción", cli.delete(B + f"escenarios/{eid}?token=recepcion_test").status_code == 403)
check("DELETE escenario: 404 inexistente", cli.delete(B + "escenarios/99999", headers=H_).status_code == 404)
check("DELETE escenario: 200", cli.delete(B + f"escenarios/{eid}", headers=H_).status_code == 200
      and len(v2.escenarios_lista()) == 1)

# ── 3. Datos sintéticos ────────────────────────────────────────────────────
with session.db() as c:
    cm._ensure_insights(c)
    ci._ensure_cupos(c)
    # Caja: 2 profesionales con ≥5 atenciones (precio típico) + mes anterior + mes en curso
    mes_ant = (HOY.replace(day=1) - timedelta(days=1)).replace(day=10)
    pago = 1
    for prof, monto in ((1, 20000), (77, 30000)):
        for k in range(8):
            f = (HOY - timedelta(days=3 + k * 6)).isoformat()
            c.execute("INSERT INTO bi_pagos_caja (pago_id, fecha, id_profesional, id_paciente, monto, metodo_pago) VALUES (?,?,?,?,?,?)",
                      (pago, f, prof, 4000 + pago, monto, "Efectivo"))
            pago += 1
    for k in range(4):
        c.execute("INSERT INTO bi_pagos_caja (pago_id, fecha, id_profesional, id_paciente, monto, metodo_pago) VALUES (?,?,?,?,?,?)",
                  (pago, mes_ant.isoformat(), 1, 6000 + k, 100000, "Efectivo"))
        pago += 1
    # Paciente antiguo (primer pago hace 2 años) que vuelve en el rango: NO es nuevo
    c.execute("INSERT INTO bi_pagos_caja (pago_id, fecha, id_profesional, id_paciente, monto, metodo_pago) VALUES (?,?,?,?,?,?)",
              (pago, (HOY - timedelta(days=700)).isoformat(), 1, 9999, 15000, "Efectivo"))
    c.execute("INSERT INTO bi_pagos_caja (pago_id, fecha, id_profesional, id_paciente, monto, metodo_pago) VALUES (?,?,?,?,?,?)",
              (pago + 1, (HOY - timedelta(days=4)).isoformat(), 1, 9999, 15000, "Efectivo"))
    # Agenda 14 días
    for i in range(14):
        f = (HOY + timedelta(days=i)).isoformat()
        for prof, lib in ((1, 4), (77, 2)):
            c.execute("INSERT INTO agenda_cupos_cache (id_profesional, fecha, libres, primera_hora, actualizado_ts) VALUES (?,?,?,?,?)",
                      (prof, f, lib, "09:00", int(AHORA.timestamp()) - 1800))
    # Meta: AD_MG (13 semanas, gasto que varía por semana) y AD_ORTO (gasta y no trae nada)
    for i in range(1, 92):
        f = (HOY - timedelta(days=i)).isoformat()
        sem = (i - 1) // 7
        gasto = 2000 + 1500 * (sem % 5)
        conv = round(40 * (1 - math.exp(-gasto * 7 / 30000)) / 7, 3)
        for desg, val, sp in (("total", "-", gasto), ("plataforma", "facebook", gasto * 0.7), ("plataforma", "instagram", gasto * 0.3)):
            c.execute("INSERT INTO meta_insights_diario (fecha, ad_id, desglose, valor, ad_name, campaign_id, campaign_name, spend, "
                      "impressions, reach, frequency, clicks, conversaciones, actualizado_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (f, "AD_MG", desg, val, "Medicina General hoy", "C1", "Medicina", sp, 1000, 600, 1.6, 20,
                       round(conv * (1 if desg == "total" else 0.7 if val == "facebook" else 0.3)), int(AHORA.timestamp()) - 7200))
        if i <= 30:
            c.execute("INSERT INTO meta_insights_diario (fecha, ad_id, desglose, valor, ad_name, campaign_id, campaign_name, spend, "
                      "impressions, reach, frequency, clicks, conversaciones, actualizado_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (f, "AD_ORTO", "total", "-", "Ortodoncia brackets", "C2", "Dental", 3000, 800, 500, 1.6, 10, 1,
                       int(AHORA.timestamp()) - 7200))
            c.execute("INSERT INTO meta_insights_diario (fecha, ad_id, desglose, valor, ad_name, campaign_id, campaign_name, spend, "
                      "impressions, reach, frequency, clicks, conversaciones, actualizado_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (f, "AD_MEU", "total", "-", "Almacén Meulen", "C9", "Meulen almacén", 9999, 800, 500, 1.6, 10, 0,
                       int(AHORA.timestamp()) - 7200))
    # Personas de Meta: 3 facebook, 3 instagram, 1 sin plataforma, 1 Messenger (fb_)
    for n, plat in enumerate(("facebook", "facebook", "facebook", "instagram", "instagram", "instagram", None)):
        ph = f"5696660{n:04d}"
        t = AHORA - timedelta(days=5 + n)
        c.execute("INSERT INTO meta_referrals (phone, source_id, headline, ts, plataforma) VALUES (?,?,?,?,?)",
                  (ph, "AD_MG", "h", int(t.timestamp()), plat))
        c.execute("INSERT INTO messages (phone, direction, text, state, ts) VALUES (?,?,?,?,?)",
                  (ph, "in", "Hola quiero hora, soy Juan Pérez Soto", "IDLE", utc(t)))
        if n < 3:
            pid = 7000 + n
            c.execute("INSERT INTO citas_bot (phone, id_cita, especialidad, fecha, hora, created_at, ad_source_id, ad_plataforma, "
                      "id_paciente_medilink) VALUES (?,?,?,?,?,?,?,?,?)",
                      (ph, str(900 + n), "Medicina General", (t + timedelta(days=1)).date().isoformat(), "10:00",
                       utc(t + timedelta(minutes=5)), "AD_MG", plat or "", pid))
            c.execute("INSERT INTO ausentismo_citas (id_cita, id_profesional, id_paciente, fecha, hora, id_estado, estado_cita, "
                      "anulacion, updated_at) VALUES (?,?,?,?,?,?,?,?,?)", (900 + n, 1, pid, (t + timedelta(days=1)).date().isoformat(),
                                                                         "10:00", 2, "Atendido", 0, AHORA.replace(hour=4, minute=50).isoformat()))
            c.execute("INSERT INTO bi_pagos_caja (pago_id, fecha, id_profesional, id_paciente, monto, metodo_pago) VALUES (?,?,?,?,?,?)",
                      (800 + n, (t + timedelta(days=1)).date().isoformat(), 1, pid, 20000, "Efectivo"))
    c.execute("INSERT INTO meta_referrals (phone, source_id, headline, ts, plataforma) VALUES (?,?,?,?,?)",
              ("fb_9988776655", "AD_MG", "h", int((AHORA - timedelta(days=3)).timestamp()), None))
    c.execute("INSERT INTO meta_referrals (phone, source_id, headline, ts, plataforma) VALUES (?,?,?,?,?)",
              ("56988880000", "AD_ORTO", "h", int((AHORA - timedelta(days=6)).timestamp()), "instagram"))
    # Ausentismo: historia 90 días (18 atendidos, 2 no asiste) y mañana 4 sin confirmar + 1 confirmada
    for k in range(20):
        c.execute("INSERT INTO ausentismo_citas (id_cita, id_profesional, id_paciente, fecha, hora, id_estado, estado_cita, anulacion, "
                  "updated_at) VALUES (?,?,?,?,?,?,?,?,?)", (5000 + k, 77, 5100 + k, (HOY - timedelta(days=10 + k)).isoformat(), "10:00",
                                                          2 if k < 18 else 8, "Atendido" if k < 18 else "No asiste", 0,
                                                          AHORA.replace(hour=4, minute=50).isoformat()))
    for k in range(5):
        c.execute("INSERT INTO ausentismo_citas (id_cita, id_profesional, id_paciente, fecha, hora, id_estado, estado_cita, anulacion, "
                  "updated_at) VALUES (?,?,?,?,?,?,?,?,?)", (6000 + k, 1, 6100 + k, (HOY + timedelta(days=1)).isoformat(), "11:00",
                                                          7 if k < 4 else 3, "No confirmado" if k < 4 else "Confirmado por teléfono", 0,
                                                          AHORA.replace(hour=4, minute=50).isoformat()))
    # Una persona espera a recepción
    hoy_9 = AHORA.replace(hour=9, minute=0)
    c.execute("INSERT INTO messages (phone, direction, text, state, ts) VALUES (?,?,?,?,?)",
              ("56977000002", "in", "¿hay hora?", "HUMAN_TAKEOVER", utc(hoy_9)))
    c.execute("INSERT OR REPLACE INTO sessions (phone, state, data, updated_at) VALUES (?,?,?,?)",
              ("56977000002", "HUMAN_TAKEOVER", json.dumps({"especialidad": "kinesiologia"}), utc(hoy_9)))
    c.execute("INSERT OR REPLACE INTO contact_profiles (phone, nombre) VALUES (?,?)", ("56977000002", "José Luis Rojas"))
    # Reenganche: 10 envíos, 2 terminan en cita ≤24 h
    for n in range(10):
        ph = f"5693300{n:04d}"
        t = AHORA - timedelta(days=4, hours=n)
        c.execute("INSERT INTO conversation_events (phone, event, meta, ts) VALUES (?,?,?,?)",
                  (ph, "reenganche_enviado", json.dumps({"variante": "x"}), utc(t)))
        if n < 2:
            c.execute("INSERT INTO citas_bot (phone, id_cita, especialidad, created_at) VALUES (?,?,?,?)",
                      (ph, f"r{n}", "Medicina General", utc(t + timedelta(hours=2))))
    c.commit()
rr.limpiar_cache()
_RED.clear()

# ── 4. Oportunidades ───────────────────────────────────────────────────────
IN = cli.get(B + "inicio", headers=H_).json()
O = IN["oportunidades"]
ops = O["oportunidades"]
claves = [o["clave"] for o in ops]
check("oportunidades: hay cupos, anuncios, confirmar y esperando", {"cupos", "anuncios", "confirmar", "esperando"} <= set(claves))
check("oportunidades: máximo 5", len(ops) <= 5)
montos = [o["monto"] for o in ops if o["monto"] is not None]
check("oportunidades: ordenadas por monto (sin monto al final)", montos == sorted(montos, reverse=True)
      and all(o["monto"] is not None for o in ops[:len(montos)]))
cup = next(o for o in ops if o["clave"] == "cupos")
check("cupos: 84 horas libres y techo = cupos × precio × parte del centro",
      "84" in cup["titulo"] and cup["monto_tipo"] == "techo" and cup["monto"] == round(14 * 4 * 20000 * 0.3 + 14 * 2 * 30000 * 0.3))
check("cupos: acción real a Agenda × anuncios", cup["accion"]["href"] == "/alma/campanas-meta#agenda")
anu = next(o for o in ops if o["clave"] == "anuncios")
check("anuncios: 2 anuncios del CMC con pérdida = gasto × (1 − retorno)", anu["titulo"].startswith("2 anuncios")
      and abs(anu["monto"] - (3000 * 29 + 127000 * (1 - 0.14))) <= 2 and anu["monto_tipo"] == "estimado")
check("anuncios: excluye campañas de otros negocios (Meulen)", "Meulen" not in json.dumps(anu, ensure_ascii=False))
conf = next(o for o in ops if o["clave"] == "confirmar")
check("confirmar: 4 de 5 sin confirmar, valor en riesgo con la inasistencia real",
      conf["titulo"].startswith("4 de 5") and conf["monto_tipo"] == "en_riesgo" and conf["monto"] is not None)
esp = next(o for o in ops if o["clave"] == "esperando")
check("esperando: acción a la cola de recepción", esp["accion"]["href"] == "/alma/recepcion-kanban")
check("confianza: solo alta/media/baja", all(o["confianza"] in ("alta", "media", "baja") for o in ops))
check("cada oportunidad trae evidencia, fuente y acción", all(o["evidencia"] and o["fuente"] and o["accion"]["href"].startswith("/")
                                                              for o in ops))
rec = [o for o in ops if o["clave"] == "recall"]
if rec:
    check("recall: usa la tasa real de reenganche (2 de 10)", "20,0%" in rec[0]["evidencia"])
    check("solapes: cupos y recall marcados, montos no se suman", ["cupos", "recall"] in O["solapes"]
          and "recall" in cup["solapa_con"])
else:
    check("solapes: sin recall no hay solape cupos-recall", ["cupos", "recall"] not in O["solapes"])
check("solapes: esperando y recall se marcan cuando ambos están", (["esperando", "recall"] in O["solapes"]) == bool(rec))
# Función pura de solapes: con ambas fuentes, ambas quedan marcadas
_o1 = {"clave": "cupos", "monto": 10}
_o2 = {"clave": "recall", "monto": 5}
check("SOLAPES: tabla simétrica cupos↔recall", "recall" in v2.SOLAPES["cupos"] and "cupos" in v2.SOLAPES["recall"])
check("lectura: frase con el mes cerrado y la primera oportunidad", "cerró con" in IN["lectura"] and "oportunidad" in IN["lectura"])

# ── 5. Puente con y sin costos ─────────────────────────────────────────────
P = IN["puente"]
ant = P["anterior"]
check("puente: venta del mes anterior = caja (EBITDA)", ant["ingresos"] >= 400000)
check("puente: honorarios 70% por defecto", ant["honorarios"] == round(ant["ingresos"] * 0.7))
check("puente: publicidad sin red y sin campañas de otros negocios (Meulen fuera)",
      ant["publicidad_fuente"] in ("foto_diaria", None) and (ant["publicidad"] == 0 or ant["publicidad"] % 9999 != 0))
check("puente sin costos: se corta en el aporte", ant["hay_costos"] is False and ant["resultado"] is None)
check("puente: enlaza a EBITDA para editar gastos", P["editar"]["href"] == "/cmc/ebitda" and P["editar"]["modulo"] == "ebitda")
with session.db() as c:
    c.execute("INSERT INTO egresos_cmc (fecha, categoria, descripcion, monto, recurrente) VALUES (?,?,?,?,?)",
              ((HOY.replace(day=1) - timedelta(days=60)).isoformat(), "Sueldos y leyes sociales", "recepción", 150000, 1))
    c.execute("INSERT INTO egresos_cmc (fecha, categoria, descripcion, monto, recurrente) VALUES (?,?,?,?,?)",
              (mes_ant.isoformat(), "Servicios básicos", "luz", 20000, 0))
    c.commit()
rr.limpiar_cache()
P2 = v2.puente_data(HOY)
a2 = P2["anterior"]
check("puente con costos: resultado = aporte − gastos operativos", a2["hay_costos"] and a2["operativos_total"] == 170000
      and a2["resultado"] == a2["aporte"] - 170000)
check("puente con costos: no exige arriendo (local propio)", a2["faltan_categorias"] == [])
check("puente: recurrente se repite en el mes en curso, el puntual no", P2["actual"]["operativos_total"] == 150000)
check("puente: diferencia con el Radar calculada", "radar" in a2 and a2["radar"]["dif_venta"] == 0)
check("puente: ninguna llamada a Meta (solo_datos_locales)", not any("graph.facebook" in u for u in _RED))
with eb.solo_datos_locales():
    with session.db() as c:
        gm = eb._gasto_meta_local(c, HOY.strftime("%Y-%m"))
check("EBITDA local: excluye la campaña de Meulen", gm is not None and "Meulen almacén"[:40] in gm[2])

# ── 6. Centro de datos ─────────────────────────────────────────────────────
F = v2._frescura
check("frescura: corrió hoy 04:50 → verde", F(AHORA.replace(hour=4, minute=51), AHORA, [(4, 50)]) == "verde")
check("frescura: corrió ayer 04:50 → ámbar", F(AHORA.replace(hour=4, minute=51) - timedelta(days=1), AHORA, [(4, 50)]) == "ambar")
check("frescura: hace 3 días → rojo", F(AHORA - timedelta(days=3), AHORA, [(4, 50)]) == "rojo")
check("frescura: sin corrida → rojo", F(None, AHORA, [(4, 50)]) == "rojo")
check("frescura: dentro de la gracia aún vale la corrida anterior",
      F(AHORA.replace(hour=4, minute=51) - timedelta(days=1), AHORA.replace(hour=5, minute=10), [(4, 50)]) == "verde")
check("frescura mensual: calculado este mes → verde", F(AHORA.replace(day=1, hour=4, minute=31), AHORA, [(4, 30)], mensual_dia=1) == "verde"
      if AHORA.day > 1 else True)
DD = cli.get(B + "datos", headers=H_).json()
E = {p["clave"]: p for p in DD["procesos"]}
check("datos: ausentismo de hoy 04:50 → verde", E["ausentismo"]["estado"] == "verde")
check("datos: agenda con filas y lectura reciente → verde", E["agenda"]["estado"] == "verde" and E["agenda"]["filas"] == 28)
check("datos: insights de hoy → verde", E["insights"]["estado"] == "verde")
check("datos: capi sin tabla → rojo con su motivo", E["capi"]["estado"] == "rojo" and "no existe" in E["capi"]["detalle"])
check("datos: rojos primero", [p["estado"] for p in DD["procesos"]] == sorted([p["estado"] for p in DD["procesos"]],
                                                                             key={"rojo": 0, "ambar": 1, "verde": 2}.get))
check("datos: resumen consistente", DD["al_dia"] == sum(1 for p in DD["procesos"] if p["estado"] == "verde"))
with session.db() as c:
    c.execute("UPDATE ausentismo_citas SET updated_at=?", ((AHORA - timedelta(days=1)).replace(hour=4, minute=50).isoformat(),))
    c.commit()
check("datos: ausentismo que se saltó una corrida → ámbar",
      {p["clave"]: p for p in v2.centro_datos_data(AHORA)["procesos"]}["ausentismo"]["estado"] == "ambar")
with session.db() as c:
    c.execute("DELETE FROM agenda_cupos_cache")
    c.commit()
check("datos: agenda_cupos_cache vaciada → rojo",
      {p["clave"]: p for p in v2.centro_datos_data(AHORA)["procesos"]}["agenda"]["estado"] == "rojo")
check("portada: resumen de datos con lista de detenidos", IN["datos"]["total"] == 10 and isinstance(IN["datos"]["rojos_lista"], list))

# ── 7. Laboratorio ─────────────────────────────────────────────────────────
xs = [7000 * (1 + (k % 5)) for k in range(13)]
ys = [100 * (1 - math.exp(-x / 30000)) for x in xs]
fit = v2._ajuste(xs, ys)
check("ajuste: recupera B de una curva conocida (±25%)", fit is not None and abs(fit[1] - 30000) / 30000 < 0.25 and fit[2] > 0.95)
check("ajuste: sin variación de gasto no ajusta (supuesto)", v2._ajuste([10000] * 13, [5] * 13) is None)
check("ajuste: pocas semanas no ajusta", v2._ajuste([1000, 2000, 3000], [1, 2, 3]) is None)
rr.limpiar_cache()
L = cli.get(B + "laboratorio", headers=H_).json()
check("laboratorio: 200 sin error", "error" not in L)
G = {e["grupo"]: e for e in L["especialidades"]}
check("laboratorio: Medicina general con tasas reales", "Medicina general" in G)
mg = G.get("Medicina general", {})
check("laboratorio: costo por conversación = gasto ÷ conversaciones", mg and mg["costo_conv"] == round(mg["gasto_90"] / mg["conv_90"]))
check("laboratorio: curva ajustada a las semanas", mg and mg["curva"]["metodo"] == "ajuste" and len(mg["curva"]["semanas"]) == 13)
check("laboratorio: conversión a cita ≤ 1 y asistencia presente", mg and 0 <= mg["conv_cita"] <= 1 and mg["asistencia"] is not None)
check("laboratorio: escenarios incluidos", isinstance(L["escenarios"], list) and len(L["escenarios"]) == 1)
check("laboratorio: método con banda de sensibilidad", L["metodo"]["sens"] == [0.6, 1.6])

# ── 8. Embudo por plataforma y nuevos sin canal ────────────────────────────
rr.limpiar_cache()
CAP = cli.get(B + "captacion", headers=H_).json()
emb = CAP["embudo"]
mp = emb["meta_plataformas"]
check("plataformas: sin error", "error" not in mp)
FL = {f["k"]: f for f in mp["filas"]}
check("plataformas: Instagram, Facebook, Messenger/WhatsApp y sin plataforma", set(FL) == {"ig", "fb", "msg", "sin"})
check("plataformas: personas suman el total de Meta",
      sum(FL[k]["personas"] for k in FL) == emb["meta"]["personas"])
check("plataformas: Messenger cuenta la llegada fb_", FL["msg"]["personas"] == 1)
check("plataformas: gasto por publisher_platform (70/30)", mp["gasto_por_plataforma"] and FL["fb"]["gasto"] > FL["ig"]["gasto"] > 0)
check("plataformas: fecha desde la que se registra", mp["desde"] == cm.PLATAFORMA_DESDE)
nv = emb["nuevos"]
check("nuevos: cuenta pacientes con primer pago en el rango (no el antiguo)", nv["hay"] and nv["nuevos_centro"] >= 3)
check("nuevos sin canal = nuevos − Meta − web", nv["sin_canal"] == max(0, nv["nuevos_centro"] - nv["nuevos_meta"] - nv["nuevos_web"]))
check("nuevos: venta aproximada marcada", nv["venta_aprox"] is True)
# «¿Cómo nos conociste?»: amigo (nuevo), radio, y uno que dijo Facebook pero SÍ hizo clic (ya está en Meta)
with session.db() as c:
    t_ = AHORA - timedelta(days=2)
    for ph, tag in (("56955550001", "referido:amigo"), ("56955550002", "referido:radio"), ("56966600001", "referido:facebook_instagram"),
                    ("56955550003", "referido:recurrente")):
        c.execute("INSERT INTO contact_tags (phone, tag, ts) VALUES (?,?,?)", (ph, tag, utc(t_)))
    c.execute("INSERT INTO citas_bot (phone, id_cita, especialidad, created_at, id_paciente_medilink) VALUES (?,?,?,?,?)",
              ("56955550001", "d1", "Medicina General", utc(t_), 8801))
    c.execute("INSERT INTO bi_pagos_caja (pago_id, fecha, id_profesional, id_paciente, monto, metodo_pago) VALUES (?,?,?,?,?,?)",
              (99001, (HOY - timedelta(days=1)).isoformat(), 1, 8801, 25000, "Efectivo"))
    c.commit()
rr.limpiar_cache()
nv2 = cli.get(B + "captacion", headers=H_).json()["embudo"]["nuevos"]
DF = {f["k"]: f for f in nv2["declarados"]["filas"]}
check("conociste: recomendación con 1 persona, 1 cita, 1 atendido nuevo y su venta",
      DF["recomendacion"]["personas"] == 1 and DF["recomendacion"]["citas"] == 1 and DF["recomendacion"]["atendidos"] == 1
      and DF["recomendacion"]["nuevos"] == 1 and DF["recomendacion"]["venta"] == 25000)
check("conociste: radio cae en Letrero/radio", DF["letrero"]["personas"] == 1)
check("conociste: quien dijo Facebook pero hizo clic NO se duplica", DF["fbig"]["personas"] == 0)
check("conociste: sin QR registrado no hay fila QR", "qr" not in DF)
check("conociste: se descuenta de sin canal", nv2["sin_canal"] == max(0, nv2["nuevos_centro"] - nv2["nuevos_meta"] - nv2["nuevos_web"] - 1)
      and nv2["nuevos_declarados"] == 1)
check("conociste: pregunta activa desde 05/10", nv2["declarados"]["desde"] == "2026-10-05")
with session.db() as c:
    c.execute("INSERT INTO conversation_events (phone, event, meta, ts) VALUES (?,?,?,?)",
              ("56955550009", "qr_origen", json.dumps({"qr": "letrero_plaza"}), utc(AHORA - timedelta(days=1))))
    c.commit()
rr.limpiar_cache()
DF2 = {f["k"]: f for f in cli.get(B + "captacion", headers=H_).json()["embudo"]["nuevos"]["declarados"]["filas"]}
check("conociste: con evento qr_origen aparece la fila QR", DF2.get("qr", {}).get("personas") == 1)
CAP = cli.get(B + "captacion", headers=H_).json()
# Sin desglose por plataforma: no se reparte el gasto
with session.db() as c:
    c.execute("DELETE FROM meta_insights_diario WHERE desglose='plataforma'")
    c.commit()
rr.limpiar_cache()
mp2 = rr._meta_plataformas(*rr._rango(HOY))
F2 = {f["k"]: f for f in mp2["filas"]}
check("sin desglose: gasto entero en «sin plataforma», nada inventado",
      not mp2["gasto_por_plataforma"] and F2["fb"]["gasto"] == 0 and F2["ig"]["gasto"] == 0 and F2["sin"]["gasto"] > 0)

# ── 8b. Revisión externa: reglas, razones, recepción, metas, holdout ───────
check("razones: dos decimales entre 0,5 y 2", v2._rx(0.97) == "0,97×" and v2._rx(1.5) == "1,50×" and v2._rx(2.4) == "2,4×"
      and v2._rx(0.3) == "0,3×" and v2._rx(None) == "—")
AG = {"sin_datos": False, "fechas": [(HOY + timedelta(days=i)).isoformat() for i in range(14)],
      "profesionales": [{"grupo": "Ecografía", "por_dia": [0] * 12 + [3, 0]}, {"grupo": "Kinesiología", "por_dia": [2] * 14}],
      "grupos": [{"grupo": "Ecografía", "libres_7d": 0, "libres_14d": 3, "gasto_dia": 8000},
                 {"grupo": "Kinesiología", "libres_7d": 14, "libres_14d": 28, "gasto_dia": 0}]}
with session.db() as c:
    for n in range(6):   # Ecografía: la gente agenda para 2 días después
        c.execute("INSERT INTO citas_bot (phone, id_cita, especialidad, fecha, created_at) VALUES (?,?,?,?,?)",
                  (f"5691111{n:04d}", f"e{n}", "Ecografía", (HOY - timedelta(days=10 - 2)).isoformat(),
                   utc(AHORA - timedelta(days=10))))
    c.commit()
RG = v2.reglas_presupuesto(AG, HOY)
RGg = {r["grupo"]: r for r in RG["reglas"]}
check("reglas: Ecografía gasta y su próxima hora (12 días) está lejos del plazo típico (2 días) → bajar",
      RGg["Ecografía"]["accion"] == "bajar" and RGg["Ecografía"]["datos"]["proxima_dias"] == 12
      and RGg["Ecografía"]["datos"]["lead_mediana"] == 2)
check("reglas: cada sugerencia explica el porqué", all(len(r["porque"]) >= 2 for r in RG["reglas"])
      and any("Próxima hora libre" in x for x in RGg["Ecografía"]["porque"]))
check("reglas: sin agenda no se sugiere nada", v2.reglas_presupuesto({"sin_datos": True}, HOY) == {"sin_datos": True})
check("reglas: captación las entrega", "reglas" in CAP)
CV = cli.get(B + "conversion", headers=H_).json()
check("recepción: comparación dentro del horario presente", "velocidad_horario" in CV and "error" not in CV["velocidad_horario"])
check("holdout: sin eventos → sin_datos", CV["holdout"]["estado"] == "sin_datos")
with session.db() as c:
    for n in range(40):
        ph = f"5692222{n:04d}"
        ev = "persistencia_enviada" if n < 20 else "persistencia_holdout"
        t = AHORA - timedelta(days=3, hours=n)
        c.execute("INSERT INTO conversation_events (phone, event, meta, ts) VALUES (?,?,?,?)", (ph, ev, "{}", utc(t)))
        if (n < 8) or (20 <= n < 23):
            c.execute("INSERT INTO citas_bot (phone, id_cita, especialidad, created_at) VALUES (?,?,?,?)",
                      (ph, f"h{n}", "Medicina General", utc(t + timedelta(hours=5))))
    c.commit()
HD = v2.holdout_data(HOY)
check("holdout: 8/20 con toque vs 3/20 control", HD["con_toque"] == {"n": 20, "agendaron": 8, "pct": 40.0}
      and HD["control"]["agendaron"] == 3 and HD["diferencia_pp"] == 25.0)
check("holdout: intervalo alrededor de la diferencia y 'en curso' con muestra chica",
      HD["ic95_pp"][0] < 25 < HD["ic95_pp"][1] and HD["estado"] == "en_curso")
FZ = cli.get(B + "finanzas", headers=H_).json()
MT = {m["clave"]: m for m in FZ["metas"]["metas"]}
check("metas: venta, EBITDA, honorarios y la suma (ingreso total)", list(MT) == ["venta", "ebitda", "honorarios", "ingreso"]
      and MT["ingreso"]["total"] is True and not MT["venta"]["total"])
check("metas: solo el ingreso total viene precargado ($40M al 06-05-2032)", MT["ingreso"]["monto"] == 40_000_000
      and MT["ingreso"]["fecha_objetivo"] == "2032-05-06" and MT["ingreso"]["precargada"] and not MT["ingreso"]["configurada"])
check("metas: venta, EBITDA y honorarios sin meta (Definir meta)", all(MT[k]["monto"] is None and MT[k]["avance_pct"] is None
                                                                      for k in ("venta", "ebitda", "honorarios")))
with session.db() as c:
    check("metas: leer no escribe la precarga en la base", not cm._tabla_existe(c, "radar_metas")
          or c.execute("SELECT COUNT(*) FROM radar_metas WHERE clave='ingreso'").fetchone()[0] == 0)
check("finanzas: ya no hay meta fija de venta en la serie de meses", "meta_mensual" not in FZ["meses"]
      and "avance_meta_pct" not in FZ["meses"])
with eb.solo_datos_locales():
    with session.db() as c:
        E_ = eb._ebitda_mes(c, v2._mes_anterior(HOY))
hon1 = next((p_["bruto"] for p_ in E_["profesionales"] if p_["id"] == 1), 0)
check("metas: honorarios = caja del prof. 1 × su % (bruto)", MT["honorarios"]["actual"] == hon1)
check("metas: la suma es EBITDA + honorarios", MT["ingreso"]["actual"] == E_["ebitda"] + hon1
      and MT["ingreso"]["actual"] == MT["ebitda"]["actual"] + MT["honorarios"]["actual"])
check("metas: venta = caja del mes cerrado", MT["venta"]["actual"] == E_["ingresos"])
check("metas: tendencia de 6 meses cerrados, del más antiguo al último", all(len(m["serie"]) == 6 for m in MT.values())
      and MT["venta"]["serie"][-1]["mes"] == v2._mes_anterior(HOY)
      and [x["mes"] for x in MT["venta"]["serie"]] == sorted(x["mes"] for x in MT["venta"]["serie"]))
check("metas: mes previo = penúltimo de la serie", MT["venta"]["mes_previo"] == MT["venta"]["serie"][-2]["valor"])
check("metas: avance del ingreso total contra $40M", MT["ingreso"]["avance_pct"] == round(100 * MT["ingreso"]["actual"] / 40_000_000))
rt = v2.ritmo_necesario(8_890_000, 40_000_000, "2026-09", "2032-05-06")
check("ritmo: $8,89M (sep-2026) → $40M (may-2032) = 68 meses al ~2,24% mensual compuesto",
      rt["meses"] == 68 and abs(rt["pct_mensual"] - 100 * ((40 / 8.89) ** (1 / 68) - 1)) < 0.01 and 2.2 < rt["pct_mensual"] < 2.3)
check("ritmo: sin valor positivo no se inventa", v2.ritmo_necesario(-5, 40_000_000, "2026-09", "2032-05-06")["pct_mensual"] is None)
check("ritmo: sin meta o sin plazo → None", v2.ritmo_necesario(1, None, "2026-09", "2032-05-06") is None
      and v2.ritmo_necesario(1, 10, "2026-09", None) is None)
check("ritmo: fecha pasada", v2.ritmo_necesario(1, 10, "2026-09", "2026-01-01")["pct_mensual"] is None)
check("POST meta: 401 sin token", cli.post(B + "metas", json={"clave": "ebitda", "monto": 8000000}).status_code == 401)
check("POST meta: 403 recepción", cli.post(B + "metas?token=recepcion_test", json={"clave": "ebitda", "monto": 1}).status_code == 403)
check("POST meta: clave desconocida 400", cli.post(B + "metas", headers=H_, json={"clave": "x", "monto": 1}).status_code == 400)
check("POST meta: fecha inválida 400", cli.post(B + "metas", headers=H_, json={"clave": "venta", "monto": 1, "fecha_objetivo": "mañana"}).status_code == 400)
check("POST meta: honorarios", cli.post(B + "metas", headers=H_, json={"clave": "honorarios", "monto": 4000000,
                                                                     "fecha_objetivo": "2027-12-01"}).status_code == 200)
MT2 = {m["clave"]: m for m in v2.metas_data(HOY)["metas"]}
check("metas: la meta guardada se usa y calcula su avance", MT2["honorarios"]["monto"] == 4000000 and MT2["honorarios"]["configurada"]
      and MT2["honorarios"]["avance_pct"] == round(100 * hon1 / 4000000))
cli.post(B + "metas", headers=H_, json={"clave": "ingreso", "monto": None})
check("metas: el dueño puede borrar la meta precargada", {m["clave"]: m for m in v2.metas_data(HOY)["metas"]}["ingreso"]["monto"] is None)

# ── 8b. Pulso: barras por hora y promedio del mismo día ──────────────────
rr.limpiar_cache()
dia_ref = HOY - timedelta(days=7)
with session.db() as c:
    for k_, (h_, m_) in enumerate([(9, 10), (9, 40), (10, 5), (10, 50), (11, 20), (11, 25)]):
        t_ = datetime(dia_ref.year, dia_ref.month, dia_ref.day, h_, m_, tzinfo=CL)
        c.execute("INSERT INTO messages (phone, direction, text, state, ts) VALUES (?,?,?,?,?)",
                  (f"5691818{k_:04d}", "in", "Hola", "IDLE", utc(t_)))
    c.commit()
REF = rr._referencia_dia(HOY, set())
check("pulso: referencia trae promedio por hora (24) y el día de la semana", len(REF["por_hora"]) == 24
      and REF["dia_semana"] in ("lunes", "martes", "miércoles", "jueves", "viernes", "sábados", "domingos"))
check("pulso: por hora = suma de sus dos medias horas", all(abs(REF["por_hora"][h] - REF["por_media_hora"][2 * h]
                                                              - REF["por_media_hora"][2 * h + 1]) < 0.02 for h in range(24)))
ref_sint = {"semanas": 2, "por_media_hora": [0.0] * 18 + [2.0, 4.0] + [0.0] * 28}
check("pulso: promedio hasta ahora suma medias horas completas + fracción", rr._ref_hasta(ref_sint, 9 * 60 + 45) == 4.0
      and rr._ref_hasta(ref_sint, 9 * 60 + 30) == 2.0 and rr._ref_hasta(ref_sint, 9 * 60) == 0.0)
check("pulso: sin semanas de historia no hay promedio", rr._ref_hasta({"semanas": 0, "por_media_hora": [1] * 48}, 600) is None)
PU_ = cli.get(B + "pulso", headers=H_).json()
check("pulso: la API entrega hasta_ahora y por_hora", "hasta_ahora" in PU_["referencia"] and len(PU_["referencia"]["por_hora"]) == 24)

# ── 9. Privacidad y red ────────────────────────────────────────────────────
blob = json.dumps([IN, DD, L, CAP, CV, FZ, v2.escenarios_lista()], ensure_ascii=False)
check("privacidad: ningún teléfono", not any(x in blob for x in ("56977000002", "5696660", "fb_9988776655", "56988880000", "5693300", "5695555", "5692222", "5691111")))
check("privacidad: ningún nombre completo ni texto de mensajes", "Rojas" not in blob and "Juan Pérez" not in blob and "hay hora" not in blob)
check("ninguna llamada de red (Meta/Medilink)", _RED == [])

# ── 10. Plantilla ──────────────────────────────────────────────────────────
html = (RAIZ / "templates" / "alma_radar.html").read_text(encoding="utf-8")
check("plantilla: módulos inicio, laboratorio y centro de datos", all(f"id:'{m}'" in html for m in ("inicio", "laboratorio", "datos")))
check("plantilla: abre en la portada", "let start='inicio'" in html)
check("plantilla: sin alert/confirm/prompt nativos", not any(x in html for x in ("alert(", "confirm(", "prompt(")))
check("plantilla: diálogo propio para eliminar", "function confirmar(" in html and "peligro:true" in html)
check("plantilla: sin CDN", "https://" not in html and "cdn." not in html)
check("plantilla: nota de plataforma desde el 05/10", "La plataforma por paciente se registra desde el" in html)
check("plantilla: sin voseo", not any(w in html for w in ("tenés", "podés", "hacé ", "mirá", "elegí", "querés", "guardá", "poné")))
check("plantilla: experimentos rotulados observacionales y prueba con control", "Observacional, sin grupo de control" in html
      and "Prueba con grupo de control: segundo toque" in html)
check("plantilla: advertencia de sesgo de selección en recepción", "no prueba causa" in html)
check("plantilla: meta fija de $40M reemplazada por metas configurables", "Meta 2032 · $40M" not in html and "Metas del dueño" in html)
check("plantilla: metas con 'Definir meta', ritmo necesario y fila de suma", "Definir meta" in html and "Ritmo necesario" in html
      and "meta-r${m.total?' total':''}" in html)
check("plantilla: pulso en barras por hora, sin canvas tipo ECG", 'id="pulseCv"' not in html and "function pulseSlots(" in html
      and "00–08" in html and "Promedio de " in html)
check("plantilla: estados del pulso como contadores bajo el gráfico", "function pulseStatesHTML(" in html and "Con recepción" in html)
check("plantilla: tooltip por barra (hora, total, canal, promedio)", "function tipHTML(" in html)
check("plantilla: marca Ahora fuera del eje X", "'Ahora '+hhmm(now)" in html)
check("plantilla: razones con dos decimales (rx)", "const rx=" in html and "dec(c.retorno_centro,1)" not in html)
check("plantilla: reglas ilustrativas reemplazadas", "R-01" not in html and "Sugerencias por especialidad" in html)
check("plantilla: clases sin prefijo ad (uBlock)", not any(f'class="ad{x}' in html or f"class='ad{x}" in html for x in ("-", "s", " ", "\"")))

httpx.Client = _orig_client
httpx.get = _orig_get
print(f"\n{len(FALLAS)} fallas")
sys.exit(1 if FALLAS else 0)
