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
              "fuera_del_bot"}
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

print(f"\n{len(FALLAS)} fallas")
sys.exit(1 if FALLAS else 0)
