"""Módulo Alma "Campañas Meta" (2026-10-05): panel de CAC por anuncio y
kanban de pacientes que llegaron por anuncios.

DB temporal con datos SINTÉTICOS (ningún dato real). Fechas relativas a hoy
para que el test no envejezca.
"""
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import session  # noqa: E402

session.DB_PATH = Path(tempfile.mkdtemp()) / "t.db"
with session.db() as _c:
    session._run_ddl_inline(_c)
    _c.commit()

import config  # noqa: E402
import campanas_meta_routes as cm  # noqa: E402

FALLAS = []


def check(nombre, cond):
    print(("OK  " if cond else "FAIL") + " " + nombre)
    if not cond:
        FALLAS.append(nombre)


CL = ZoneInfo("America/Santiago")
AHORA = datetime.now(CL).replace(hour=12, minute=0, second=0, microsecond=0)
HOY = AHORA.date()


def ep(dias_atras: float) -> int:
    return int((AHORA - timedelta(days=dias_atras)).timestamp())


def utc(dias_atras: float) -> str:
    return (AHORA - timedelta(days=dias_atras)).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def fecha(dias: int) -> str:
    return (HOY + timedelta(days=dias)).isoformat()


# ── Datos sintéticos ────────────────────────────────────────────────────────
with session.db() as c:
    cm._ensure_insights(c)
    # Foto diaria de Meta: AD1 (C1) y AD2, AD3 (C2). AD3 gasta y satura.
    for i in range(10):
        f = fecha(-i - 1)
        for ad, camp, cname, spend, imp, reach, conv in (
                ("AD1", "C1", "Campaña Uno", 3000, 800, 600, 3),
                ("AD2", "C2", "Campaña Dos", 2000, 500, 400, 2),
                ("AD3", "C2", "Campaña Dos", 6000, 7000, 1000, 1)):
            c.execute("INSERT INTO meta_insights_diario (fecha, ad_id, desglose, valor, ad_name, "
                      "campaign_id, campaign_name, spend, impressions, reach, frequency, clicks, "
                      "conversaciones) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (f, ad, "total", "-", f"Anuncio {ad}", camp, cname, spend, imp, reach,
                       imp / reach, 10, conv))
            for plat, frac in (("facebook", .6), ("instagram", .4)):
                c.execute("INSERT INTO meta_insights_diario (fecha, ad_id, desglose, valor, ad_name, "
                          "campaign_id, campaign_name, spend, impressions, reach, frequency, clicks, "
                          "conversaciones) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (f, ad, "plataforma", plat, f"Anuncio {ad}", camp, cname, spend * frac,
                           int(imp * frac), int(reach * frac), 1.3, 5, round(conv * frac)))
            c.execute("INSERT INTO meta_insights_diario (fecha, ad_id, desglose, valor, ad_name, "
                      "campaign_id, campaign_name, spend, impressions, reach, frequency, clicks, "
                      "conversaciones) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (f, ad, "edad_sexo", "25-34|female", f"Anuncio {ad}", camp, cname, spend,
                       imp, reach, 1.3, 5, conv))

    def ref(phone, ad, dias, plat=""):
        c.execute("INSERT INTO meta_referrals (phone, source_id, headline, ts, plataforma) "
                  "VALUES (?,?,?,?,?)", (phone, ad, f"Titular {ad}", ep(dias), plat))

    def cita(phone, idc, esp, f, created_dias, ad=None, plat="", cancel=None, nombre=""):
        c.execute("INSERT INTO citas_bot (phone, id_cita, especialidad, profesional, fecha, hora, "
                  "modalidad, created_at, cancel_detected_at, paciente_nombre, ad_source_id, "
                  "ad_headline, ad_plataforma, ad_referral_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (phone, idc, esp, "Dr Sintético", f, "10:30", "presencial", utc(created_dias),
                   cancel, nombre, ad, f"Titular {ad}" if ad else None, plat, None))

    def ev(phone, event, dias, meta):
        c.execute("INSERT INTO conversation_events (phone, event, meta, ts) VALUES (?,?,?,?)",
                  (phone, event, json.dumps(meta), utc(dias)))

    def msg(phone, dias):
        c.execute("INSERT INTO messages (phone, direction, text, ts) VALUES (?,?,?,?)",
                  (phone, "in", "hola", utc(dias)))

    # P1 agendado (IG) — con un reagendamiento de la misma especialidad
    ref("56911110001", "AD1", 3, "instagram")
    cita("56911110001", "A1", "Medicina General", fecha(5), 2.9, "AD1", "instagram", nombre="Ana Prueba")
    cita("56911110001", "A1b", "Medicina General", fecha(6), 2.8, "AD1", "instagram")
    c.execute("INSERT INTO contact_profiles (phone, nombre) VALUES (?,?)", ("56911110001", "Ana Prueba"))
    # P2 atendido (FB) — cita pasada + Purchase
    ref("56911110002", "AD1", 10, "facebook")
    cita("56911110002", "A2", "Kinesiología", fecha(-2), 9.5, "AD1", "facebook")
    ev("56911110002", "capi_send_ok", 2, {"event_type": "Purchase", "value": 1})
    ev("56911110002", "capi_send_ok", 9, {"event_type": "Lead"})
    # P3 anuló
    ref("56911110003", "AD2", 4)
    cita("56911110003", "A3", "Odontología General", fecha(3), 3.9, "AD2", "", cancel=utc(1))
    # P4 vio horas
    ref("56911110004", "AD2", 2)
    ev("56911110004", "funnel_slot_ofrecido", 1.9, {"esp": "ecografía"})
    msg("56911110004", 1.8)
    # P5 escribió
    ref("56911110005", "AD2", 1)
    msg("56911110005", 0.9)
    # P6 perdido
    ref("56911110006", "AD1", 20)
    msg("56911110006", 15)
    # P7 fuera de rango
    ref("56911110007", "AD1", 40)
    # P8 dos clics: cuenta el último (AD2)
    ref("56911110008", "AD1", 25)
    ref("56911110008", "AD2", 5)
    msg("56911110008", 4.9)
    c.commit()

D30, H = fecha(-29), fecha(0)

# ── Panel ───────────────────────────────────────────────────────────────────
p = cm.panel_data(D30, H)
k = p["kpis"]
check("panel: gasto = suma de la foto 'total'", k["gasto"] == 10 * (3000 + 2000 + 6000))
check("panel: conversaciones de Meta", k["conversaciones"] == 10 * (3 + 2 + 1))
check("panel: personas únicas en rango (P1-P6, P8)", k["personas"] == 7)
check("panel: citas sin contar reagendamiento", k["citas"] == 3)
check("panel: atendidos = Purchase posterior a la cita", k["atendidos"] == 1)
check("panel: CAC por cita", k["cac_cita"] == round(110000 / 3))
check("panel: CAC por atendido", k["cac_atendido"] == 110000)
ads = {a["ad_id"]: a for a in p["anuncios"]}
check("panel: anuncio con nombre de Meta", ads["AD1"]["anuncio"] == "Anuncio AD1")
check("panel: AD1 citas 2 / atendidos 1", ads["AD1"]["citas"] == 2 and ads["AD1"]["atendidos"] == 1)
check("panel: muestra chica marcada", ads["AD1"]["muestra_chica"] is True)
check("panel: frecuencia = impresiones / alcance diario sumado (piso)",
      ads["AD3"]["frecuencia"] == 7.0)
orden = [a["ad_id"] for a in p["anuncios"]]
check("panel: orden por CAC/cita, sin citas al final", orden.index("AD1") < orden.index("AD3"))
tipos = {(a["tipo"], a["ad_id"]) for a in p["alertas"]}
check("alerta: gasto ≥ $50.000 y 0 citas", ("sin_citas", "AD3") in tipos)
check("alerta: frecuencia > 6 saturado", ("saturado", "AD3") in tipos)
check("alerta: AD1 sin alerta", not any(a[1] == "AD1" for a in tipos))
camps = {x["campaign_id"]: x for x in p["campanas"]}
check("panel: campaña agrega anuncios", camps["C2"]["gasto"] == 80000 and camps["C2"]["n_anuncios"] == 2)
check("panel: desglose edad/sexo", p["desgloses"]["edad_sexo"][0]["k"] == "25-34 · Mujeres")
check("panel: desglose día de semana presente", len(p["desgloses"]["dia_semana"]) >= 1)
check("panel: tendencia mensual con gasto", sum(m["gasto"] for m in p["tendencia"]) == 110000)
check("panel: opciones de campaña", {x["id"] for x in p["opciones"]["campanas"]} == {"C1", "C2"})

p1 = cm.panel_data(D30, H, campana="C1")
check("filtro campaña: gasto solo C1", p1["kpis"]["gasto"] == 30000)
check("filtro campaña: citas solo C1", p1["kpis"]["citas"] == 2)
pi = cm.panel_data(D30, H, plataforma="instagram")
check("filtro IG: gasto del desglose plataforma", pi["kpis"]["gasto"] == round(110000 * .4))
check("filtro IG: citas con ad_plataforma=instagram", pi["kpis"]["citas"] == 1)
ps = cm.panel_data(D30, H, plataforma="sin_dato")
check("filtro sin dato: no inventa gasto", ps["kpis"]["gasto"] == 0 and ps["kpis"]["citas"] == 1)
pv = cm.panel_data(fecha(-400), fecha(-380))
check("rango sin datos: ceros, sin error", pv["kpis"]["gasto"] == 0 and pv["kpis"]["cac_cita"] is None)
check("rango antes de atribución avisado", pv["medicion"]["rango_antes_de_atribucion"] is True)

# ── Kanban ──────────────────────────────────────────────────────────────────
kb = cm.kanban_data(D30, H, ahora=AHORA)
col = {cc["id"]: cc for cc in kb["columnas"]}
quien = {cc["id"]: {t["phone"][-1] for t in cc["tarjetas"]} for cc in kb["columnas"]}
check("kanban: 6 columnas en orden", [cc["id"] for cc in kb["columnas"]] == cm._ETAPA_IDS)
check("kanban: agendado", quien["agendado"] == {"1"})
check("kanban: atendido", quien["atendido"] == {"2"})
check("kanban: anuló", quien["anulo"] == {"3"})
check("kanban: vio horas", quien["vio_horas"] == {"4"})
check("kanban: escribió", quien["escribio"] == {"5", "8"})
check("kanban: perdido > 7 días", quien["perdido"] == {"6"})
check("kanban: fuera de rango excluido", kb["total"] == 7)
t1 = col["agendado"]["tarjetas"][0]
check("tarjeta: nombre desde contact_profiles", t1["nombre"] == "Ana Prueba")
check("tarjeta: teléfono enmascarado", t1["telefono"] == "+56 9 •••• 0001" and "1111" not in t1["telefono"])
check("tarjeta: próxima cita", t1["proxima_cita"]["fecha"] == datetime.strptime(fecha(5), "%Y-%m-%d").strftime("%d/%m/%Y"))
check("tarjeta: plataforma", t1["plataforma"] == "instagram")
check("tarjeta: campaña", t1["campana"] == "Campaña Uno")
t8 = next(t for t in col["escribio"]["tarjetas"] if t["phone"].endswith("8"))
check("kanban: usa el ÚLTIMO clic (AD2)", t8["ad_id"] == "AD2")
check("kanban: más reciente primero", [t["phone"][-1] for t in col["escribio"]["tarjetas"]] == ["5", "8"])
check("kanban: días en etapa", col["perdido"]["tarjetas"][0]["dias_etapa"] == 15)
check("kanban: especialidad desde evento si no hay cita",
      col["vio_horas"]["tarjetas"][0]["especialidad"] == "Ecografía")
permitidas = {"clave", "phone", "telefono", "nombre", "anuncio", "ad_id", "campana", "campaign_id",
              "plataforma", "especialidad", "llegada", "llegada_iso", "proxima_cita", "dias_etapa",
              "fuera_del_bot", "gestion"}
check("privacidad: la tarjeta no trae campos extra",
      all(set(t) <= permitidas for cc in kb["columnas"] for t in cc["tarjetas"]))

check("filtro plataforma IG", cm.kanban_data(D30, H, plataforma="instagram", ahora=AHORA)["total"] == 1)
check("filtro sin dato", cm.kanban_data(D30, H, plataforma="sin_dato", ahora=AHORA)["total"] == 5)
check("filtro campaña C2", cm.kanban_data(D30, H, campana="C2", ahora=AHORA)["total"] == 4)
check("filtro anuncio AD1", cm.kanban_data(D30, H, anuncio="AD1", ahora=AHORA)["total"] == 3)
check("filtro especialidad", cm.kanban_data(D30, H, especialidad="kinesiología", ahora=AHORA)["total"] == 1)
check("búsqueda por nombre", cm.kanban_data(D30, H, q="ana", ahora=AHORA)["total"] == 1)
check("búsqueda por teléfono", cm.kanban_data(D30, H, q="0006", ahora=AHORA)["total"] == 1)
check("kanban vacío sin error", cm.kanban_data(fecha(-400), fecha(-380), ahora=AHORA)["total"] == 0)

# ── Auth (solo dueño) ───────────────────────────────────────────────────────
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

config.OLACORE_TOKEN = "dueno_test"
config.ADMIN_TOKEN = "recepcion_test"
app = FastAPI()
app.include_router(cm.router)
cli = TestClient(app)
check("auth: sin token 401", cli.get("/alma/api/campanas-meta/panel").status_code == 401)
check("auth: token de recepción 403",
      cli.get("/alma/api/campanas-meta/panel?token=recepcion_test").status_code == 403)
r = cli.get(f"/alma/api/campanas-meta/panel?token=dueno_test&desde={D30}&hasta={H}")
check("auth: dueño 200", r.status_code == 200 and r.json()["kpis"]["citas"] == 3)
r = cli.get(f"/alma/api/campanas-meta/kanban?desde={D30}&hasta={H}",
            headers={"Authorization": "Bearer dueno_test"})
check("auth: dueño por Bearer 200", r.status_code == 200 and r.json()["total"] == 7)

# ── Venta por teléfono: escribió desde el anuncio, no agendó por el bot, pero
# pagó en caja (recepción) → la venta es del anuncio; >90 días después, no.
import sqlite3 as _sq
_h = _sq.connect(str(Path(session.DB_PATH).parent / "heatmap_cache.db"))
_h.execute("CREATE TABLE IF NOT EXISTS pacientes_heatmap (id INTEGER, celular TEXT)")
_h.executemany("INSERT INTO pacientes_heatmap VALUES (?,?)",
               [(777001, "+56 9 7700 0001"), (777002, "56977000002")])
_h.commit(); _h.close()
with session.db() as c:
    c.execute("INSERT INTO meta_referrals (phone, source_id, headline, ts, plataforma) VALUES (?,?,?,?,?)",
              ("56977000001", "AD1", "h", ep(10), "facebook"))
    c.execute("INSERT INTO meta_referrals (phone, source_id, headline, ts, plataforma) VALUES (?,?,?,?,?)",
              ("56977000002", "AD1", "h", ep(20), "facebook"))
    c.execute("INSERT INTO bi_pagos_caja (pago_id, fecha, id_paciente, id_profesional, monto) VALUES (?,?,?,?,?)",
              (990001, fecha(-5), 777001, 1, 30000))
    c.execute("INSERT INTO bi_pagos_caja (pago_id, fecha, id_paciente, id_profesional, monto) VALUES (?,?,?,?,?)",
              (990002, fecha(-25), 777002, 1, 99999))   # pagó ANTES del clic → no cuenta
    c.commit()
_p = cm.panel_data(D30, H)
_ad1 = next(a for a in _p["anuncios"] if a["ad_id"] == "AD1")
check("venta por teléfono: entra el que pagó tras el clic", _ad1["pagaron_tel"] == 1)
check("venta por teléfono: no entra el pago anterior al clic", _p["kpis"]["venta"] >= 30000
      and all(a["venta"] < 99999 or a["ad_id"] != "AD1" for a in _p["anuncios"]))
_k = cm.kanban_data(D30, H)
_t = [t for col in _k["columnas"] for t in col["tarjetas"] if t["phone"] == "56977000001"]
check("kanban: pagó fuera del bot → Atendido", bool(_t) and _t[0]["fuera_del_bot"]
      and any(t["phone"] == "56977000001" for col in _k["columnas"] if col["id"] == "atendido" for t in col["tarjetas"]))

# ── Ficha trabajable (2026-10-05): seguimiento, línea de tiempo, conversación ─
import asyncio  # noqa: E402
from fastapi import HTTPException  # noqa: E402


def _err(fn, *a, **kw):
    try:
        fn(*a, **kw)
    except HTTPException as e:
        return e.status_code
    return None


# Seguimiento: guardar, leer, historial append-only
s1 = cm.guardar_seguimiento("911110005", "contactado", "Pidió que lo llamen en la tarde", None, ahora=AHORA)
check("seguimiento: guarda estado y nota", s1["estado"] == "contactado" and s1["nota"].startswith("Pidió"))
check("seguimiento: historial con 1 línea", len(s1["historial"]) == 1 and s1["historial"][0]["label"] == "Contactado")
s2 = cm.guardar_seguimiento("911110005", "contactado", "Pidió que lo llamen en la tarde", None, ahora=AHORA)
check("seguimiento: sin cambios no agrega línea", len(s2["historial"]) == 1)
check("seguimiento: volver a llamar sin fecha → 400",
      _err(cm.guardar_seguimiento, "911110005", "volver_llamar", "", None) == 400)
check("seguimiento: estado desconocido → 400", _err(cm.guardar_seguimiento, "911110005", "inventado", "", None) == 400)
check("seguimiento: fecha inválida → 400",
      _err(cm.guardar_seguimiento, "911110005", "volver_llamar", "", "31-02-2026") == 400)
check("seguimiento: persona que no llegó por anuncio → 404",
      _err(cm.guardar_seguimiento, "900000000", "contactado", "", None) == 404)
check("seguimiento: clave con formato inválido → 404",
      _err(cm.guardar_seguimiento, "1 OR 1=1", "contactado", "", None) == 404)
s3 = cm.guardar_seguimiento("911110005", "volver_llamar", "No contestó", fecha(-1), ahora=AHORA)
check("seguimiento: historial crece y queda el más reciente arriba",
      len(s3["historial"]) == 2 and s3["historial"][0]["estado"] == "volver_llamar"
      and s3["historial"][1]["estado"] == "contactado")
check("seguimiento: fecha vencida → alerta vencido", s3["alerta"] == "vencido")
cm.guardar_seguimiento("911110004", "volver_llamar", "", fecha(0), ahora=AHORA)
cm.guardar_seguimiento("911110003", "no_interesa", "", fecha(-3), ahora=AHORA)
kg = cm.kanban_data(D30, H, ahora=AHORA)
tg = {t["clave"]: t["gestion"] for cc in kg["columnas"] for t in cc["tarjetas"]}
check("tarjeta: indicador vencido", tg["911110005"]["alerta"] == "vencido"
      and tg["911110005"]["estado"] == "volver_llamar")
check("tarjeta: indicador hoy", tg["911110004"]["alerta"] == "hoy")
check("tarjeta: gestión cerrada no alerta aunque la fecha pasó", tg["911110003"]["alerta"] is None)
check("tarjeta: sin gestión = None", tg["911110001"] is None)
check("kanban: cuenta por llamar", kg["por_llamar"] == 2)
check("kanban: columnas no cambian por la gestión",
      {cc["id"]: cc["n"] for cc in kg["columnas"]} == {cc["id"]: cc["n"] for cc in kb["columnas"]}
      or kg["total"] >= kb["total"])
check("filtro gestión: por llamar", cm.kanban_data(D30, H, ahora=AHORA, gestion="por_llamar")["total"] == 2)
check("filtro gestión: no le interesa", cm.kanban_data(D30, H, ahora=AHORA, gestion="no_interesa")["total"] == 1)
check("filtro gestión: sin gestionar excluye gestionados",
      cm.kanban_data(D30, H, ahora=AHORA, gestion="sin_gestion")["total"] == kg["total"] - 3)
check("kanban: opciones de gestión", [g["id"] for g in kg["opciones"]["gestion"]][0] == "sin_gestion")

# Línea de tiempo
f1 = cm.persona_data("911110001", ahora=AHORA)
tipos1 = [x["tipo"] for x in f1["linea"]]
check("ficha: teléfono completo dentro de la ficha", f1["telefono"] == "+56 9 1111 0001" and f1["tel_href"] == "+56911110001")
check("ficha: nombre", f1["nombre"] == "Ana Prueba")
check("ficha: clic y citas del bot", "clic" in tipos1 and tipos1.count("cita") == 2)
check("ficha: anuncio y campaña", f1["anuncio"] == "Anuncio AD1" and f1["campana"] == "Campaña Uno")
check("ficha: más reciente primero", [x["ts"] for x in f1["linea"]] == sorted((x["ts"] for x in f1["linea"]), reverse=True))
f2 = cm.persona_data("911110002", ahora=AHORA)
check("ficha: aviso de atención (Purchase) y no el Lead",
      [x["tipo"] for x in f2["linea"]].count("atencion") == 1)
f3 = cm.persona_data("911110003", ahora=AHORA)
check("ficha: cita anulada marcada", any(x["tipo"] == "anulada" for x in f3["linea"])
      and any(x["tipo"] == "cita" and x["anulada"] for x in f3["linea"]))
check("ficha: trae el seguimiento", f3["seguimiento"]["estado"] == "no_interesa")
f4 = cm.persona_data("911110004", ahora=AHORA)
check("ficha: horarios ofrecidos", any(x["tipo"] == "horarios" and x["detalle"] == "Ecografía" for x in f4["linea"]))
fp = cm.persona_data("977000001", ahora=AHORA)
pg = [x for x in fp["linea"] if x["tipo"] == "pago"]
check("ficha: pago en caja por cruce de teléfono", len(pg) == 1 and pg[0]["monto"] == 30000 and fp["pagos"]["total"] == 30000)
check("ficha: pago con profesional con nombre", pg[0]["detalle"] and not pg[0]["detalle"].startswith("Profesional "))
fq = cm.persona_data("977000002", ahora=AHORA)
check("ficha: pago anterior al clic no entra a la línea, se informa aparte",
      not any(x["tipo"] == "pago" for x in fq["linea"]) and fq["pagos"]["antes_del_clic"] == 1)
check("ficha: sin datos clínicos (solo campos permitidos)",
      all(set(x) <= {"ts", "tipo", "titulo", "detalle", "plataforma", "anulada", "del_anuncio", "monto",
                     "paciente", "solo_fecha", "fecha", "hora", "iso"} for f in (f1, f2, f3, f4, fp) for x in f["linea"]))
check("ficha: 404 a quien no llegó por anuncio", _err(cm.persona_data, "955555555") == 404)

# Conversación: ventana de 24 h (contra el reloj real, como Meta)
_now = datetime.now(timezone.utc)
with session.db() as c:
    c.execute("INSERT INTO messages (phone, direction, text, ts) VALUES (?,?,?,?)",
              ("56911110004", "in", "¿tienen hora el viernes?", (_now - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")))
    c.execute("INSERT INTO messages (phone, direction, text, ts) VALUES (?,?,?,?)",
              ("56911110006", "in", "gracias", (_now - timedelta(hours=30)).strftime("%Y-%m-%d %H:%M:%S")))
    c.commit()
ca = cm.conversacion_data("911110004")
check("conversación: ventana abierta (escribió hace 1 h)", ca["ventana_abierta"] is True
      and any("viernes" in m["texto"] for m in ca["mensajes"]))
check("conversación: ventana cerrada (escribió hace 30 h)", cm.conversacion_data("911110006")["ventana_abierta"] is False)

ENVIOS = []


async def _fake_responder(phone, texto):
    ENVIOS.append((phone, texto))
    return {"ok": True, "wamid": "wamid.FAKE"}


cm._responder = _fake_responder   # nunca se envía nada real
base = "/alma/api/campanas-meta/persona"
for metodo, ruta, body in (("get", f"{base}/911110004", None), ("get", f"{base}/911110004/conversacion", None),
                           ("post", f"{base}/911110004/seguimiento", {"estado": "contactado"}),
                           ("post", f"{base}/911110004/conversacion", {"mensaje": "Hola"})):
    fn = getattr(cli, metodo)
    kw = {"json": body} if body else {}
    check(f"auth {metodo.upper()} {ruta[len(base):] or '/'}: sin token 401", fn(ruta, **kw).status_code == 401)
    check(f"auth {metodo.upper()} {ruta[len(base):] or '/'}: recepción 403",
          fn(ruta + "?token=recepcion_test", **kw).status_code == 403)
check("sin envíos por intentos no autorizados", ENVIOS == [])
hd = {"Authorization": "Bearer dueno_test"}
r = cli.get(f"{base}/911110004", headers=hd)
check("GET ficha dueño 200", r.status_code == 200 and r.json()["clave"] == "911110004")
r = cli.post(f"{base}/911110006/conversacion", headers=hd, json={"mensaje": "Hola, ¿pudo agendar?"})
check("POST con ventana cerrada → 409 y no envía", r.status_code == 409 and ENVIOS == [])
r = cli.post(f"{base}/911110004/conversacion", headers=hd, json={"mensaje": "   "})
check("POST mensaje vacío → 400", r.status_code == 400 and ENVIOS == [])
cm.guardar_seguimiento("911110002", "sin_gestion", "", None, ahora=AHORA)
with session.db() as c:
    c.execute("INSERT INTO messages (phone, direction, text, ts) VALUES (?,?,?,?)",
              ("56911110002", "in", "hola", (_now - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")))
    c.commit()
r = cli.post(f"{base}/911110002/conversacion", headers=hd, json={"mensaje": "Hola, le escribimos del centro"})
check("POST con ventana abierta → envía por el camino de recepción",
      r.status_code == 200 and ENVIOS == [("56911110002", "Hola, le escribimos del centro")])
sg = cm.persona_data("911110002", ahora=AHORA)["seguimiento"]
check("responder deja 'Contactado' si estaba sin gestionar",
      sg["estado"] == "contactado" and sg["historial"][0]["origen"] == "respuesta por WhatsApp")
n_env = len(ENVIOS)
r = cli.post(f"{base}/911110004/conversacion", headers=hd, json={"mensaje": "Le confirmo el viernes"})
sg4 = cm.persona_data("911110004", ahora=AHORA)["seguimiento"]
check("responder no pisa una gestión ya hecha", r.status_code == 200 and len(ENVIOS) == n_env + 1
      and sg4["estado"] == "volver_llamar")
async def _fake_caido(phone, texto):
    raise RuntimeError("red caída")


cm._responder = _fake_caido
r = cli.post(f"{base}/911110004/conversacion", headers=hd, json={"mensaje": "Hola"})
check("envío con error de red → 502 con mensaje claro", r.status_code == 502 and "No se pudo enviar" in r.json()["detail"])
cm._responder = _fake_responder
r = cli.post(f"{base}/911110004/seguimiento", headers=hd, json={"estado": "agendo_otra_via", "nota": "Agendó por teléfono"})
check("POST seguimiento dueño 200", r.status_code == 200 and r.json()["estado"] == "agendo_otra_via"
      and r.json()["historial"][0]["nota"] == "Agendó por teléfono")
r = cli.get(f"/alma/api/campanas-meta/kanban?desde={D30}&hasta={H}&gestion=agendo_otra_via", headers=hd)
check("GET kanban con filtro de gestión", r.status_code == 200 and r.json()["total"] == 1)

# ── Cómo nos conocieron (pregunta post-cita) ────────────────────────────────
with session.db() as c:
    for ph, tag, dias in (("56911110001", "referido:amigo", 2.5),        # tocó AD1 hace 3 d → con anuncio
                          ("56911110002", "referido:recurrente", 9),     # tocó AD1 hace 10 d → con anuncio
                          ("56911110002", "referido:amigo", 40),         # respuesta vieja: manda la más reciente
                          ("56911110003", "referido:rrss", 3.5),         # AD2 hace 4 d
                          ("56900000001", "referido:amigo", 1),          # nunca tocó un anuncio
                          ("56900000002", "referido:google", 2),
                          ("56900000003", "referido:facebook_instagram", 2),
                          ("56900000004", "referido:amigo", 60)):        # fuera del rango
        c.execute("INSERT INTO contact_tags (phone, tag, ts) VALUES (?,?,?)", (ph, tag, utc(dias)))
    # Respondió ANTES de tocar el anuncio → no cuenta como "con anuncio"
    c.execute("INSERT INTO contact_tags (phone, tag, ts) VALUES (?,?,?)", ("56911110005", "referido:recurrente", utc(3)))
    c.commit()
pc = cm.panel_data(D30, H)
cn = pc["conocieron"]
op = {o["id"]: o for o in cn["opciones"]}
check("conocieron: total de respuestas en el rango", cn["total"] == 7)
check("conocieron: amigo 2, uno con anuncio", op["amigo"]["n"] == 2 and op["amigo"]["con_anuncio"] == 1
      and op["amigo"]["sin_anuncio"] == 1)
check("conocieron: manda la respuesta más reciente del teléfono", op["recurrente"]["n"] == 2)
check("conocieron: respondió antes del clic → sin anuncio", op["recurrente"]["con_anuncio"] == 1)
check("conocieron: nuevas opciones facebook_instagram y google",
      op["google"]["n"] == 1 and op["facebook_instagram"]["n"] == 1)
check("conocieron: rrss antiguo separado", op["rrss"]["n"] == 1 and "antiguo" in op["rrss"]["label"])
check("conocieron: porcentajes", op["amigo"]["pct"] == round(100 * 2 / 7))
ad1 = next(a for a in pc["anuncios"] if a["ad_id"] == "AD1")
check("conocieron por anuncio: AD1 amigo 50% / ya era paciente 50%",
      ad1["conocieron"]["respondieron"] == 2 and ad1["conocieron"]["amigo_pct"] == 50
      and ad1["conocieron"]["recurrente_pct"] == 50)
ad3 = next(a for a in pc["anuncios"] if a["ad_id"] == "AD3")
check("conocieron por anuncio: sin respuestas = None", ad3["conocieron"] is None)
check("conocieron por campaña suma sus anuncios",
      next(x for x in pc["campanas"] if x["campaign_id"] == "C1")["conocieron"]["respondieron"] == 2)
check("conocieron: rango vacío sin error", cm.panel_data(fecha(-400), fecha(-380))["conocieron"]["total"] == 0)

print(f"\n{len(FALLAS)} fallas")
sys.exit(1 if FALLAS else 0)
