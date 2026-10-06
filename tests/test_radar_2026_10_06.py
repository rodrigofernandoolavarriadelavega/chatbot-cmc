"""Alma Radar (2026-10-06): /alma/radar + /alma/api/radar/*.

DB temporal con datos SINTÉTICOS. Ninguna llamada a Medilink ni a Meta: se
verifica además que abrir el Radar no intente la consulta en vivo de estados
de anuncios (`campanas_meta_routes._estados_meta` sale a la red sin la marca
`solo_datos_locales`).
"""
import json
import sqlite3
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
import capi_purchase as cp  # noqa: E402
import gsc_snapshot as gs  # noqa: E402
import ausentismo  # noqa: E402
import persistencia  # noqa: E402
import radar_routes as rr  # noqa: E402

FALLAS = []


def check(nombre, cond):
    print(("OK  " if cond else "FAIL") + " " + nombre)
    if not cond:
        FALLAS.append(nombre)


config.OLACORE_TOKEN = "dueno_test"
config.ADMIN_TOKEN = "recepcion_test"
config.ADMIN_ALERT_PHONE = "56900000000"

# Cualquier intento de salir a Meta durante el Radar es una falla.
_LLAMADAS_RED = []
import httpx  # noqa: E402
_orig_client = httpx.Client


class _ClienteEspia(_orig_client):
    def get(self, url, *a, **k):
        if "graph.facebook.com" in str(url) or "healthatom" in str(url):
            _LLAMADAS_RED.append(str(url))
            raise httpx.ConnectError("sin red en tests")
        return super().get(url, *a, **k)


httpx.Client = _ClienteEspia
config.META_ACCESS_TOKEN = "tok_falso"   # con token, _estados_meta INTENTARÍA llamar a Meta

CL = ZoneInfo("America/Santiago")
AHORA = datetime.now(CL).replace(hour=11, minute=30, second=0, microsecond=0)
HOY = AHORA.date()
rr._ahora = lambda: AHORA


def utc(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# ── 1. Vacío: todo responde 200 y con estados vacíos, sin inventar ─────────
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

app = FastAPI()
app.include_router(cm.router)
app.include_router(rr.router)
app.include_router(rr.pagina)
cli = TestClient(app)
B = "/alma/api/radar/"
H_ = {"Authorization": "Bearer dueno_test"}
RUTAS = ("pulso", "captacion", "conversion", "finanzas", "bitacora")

for r in RUTAS:
    res = cli.get(B + r, headers=H_)
    check(f"vacío: {r} 200", res.status_code == 200)
pv = cli.get(B + "pulso", headers=H_).json()
check("vacío: pulso sin eventos ni citas", pv["eventos"] == [] and pv["citas_hoy"] == 0 and pv["esperando_total"] == 0)
check("vacío: agenda de mañana marcada sin datos (no 0 cupos inventados)", pv["agenda_manana"]["sin_datos"] is True)
cv = cli.get(B + "captacion", headers=H_).json()
check("vacío: embudo en cero real", cv["embudo"]["meta"]["citas"] == 0 and cv["embudo"]["web"]["personas"] == 0)
bv = cli.get(B + "bitacora", headers=H_).json()
check("vacío: bitácora sin eventos inventados", bv["noche"]["eventos"] == [] or all(e["clave"] != "capi" for e in bv["noche"]["eventos"]))
rr.limpiar_cache()

# ── 2. Auth ────────────────────────────────────────────────────────────────
for r in RUTAS:
    check(f"auth {r}: 401 sin token", cli.get(B + r).status_code == 401)
    check(f"auth {r}: 403 con el token de recepción", cli.get(B + r + "?token=recepcion_test").status_code == 403)
    check(f"auth {r}: 200 con token del dueño en query", cli.get(B + r + "?token=dueno_test").status_code == 200)
check("página: 401 sin token", cli.get("/alma/radar").status_code == 401)
check("página: 403 con token de recepción", cli.get("/alma/radar?token=recepcion_test").status_code == 403)
pag = cli.get("/alma/radar?token=dueno_test")
check("página: 200 con el dueño y no-store", pag.status_code == 200 and "no-store" in pag.headers.get("cache-control", ""))
check("página: 404 en el dominio clínico", cli.get("/alma/radar?token=dueno_test",
                                                    headers={"host": "centromedicocarampangue.cl"}).status_code == 404)

# ── 3. Datos sintéticos ────────────────────────────────────────────────────
ausentismo.ensure_ausentismo_table()
cp.ensure_table()
with session.db() as c:
    cm._ensure_insights(c)
    ci._ensure_cupos(c)
    ci._ensure_creativos(c)
    gs.ensure_tables(c)
    persistencia._ensure_table(c)
    for i in range(1, 40):
        f = (HOY - timedelta(days=i)).isoformat()
        c.execute("INSERT INTO meta_insights_diario (fecha, ad_id, desglose, valor, ad_name, campaign_id, campaign_name, spend, "
                  "impressions, reach, frequency, clicks, conversaciones, actualizado_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (f, "AD_MG", "total", "-", "Medicina General hoy", "C1", "Medicina", 5000, 1000, 600, 1.6, 20, 3,
                   int(AHORA.timestamp()) - 3600))
    # 6 personas de Meta en el rango, 4 con cita, 3 atendidas con pago
    for n in range(6):
        ph = f"5696660{n:04d}"
        t = AHORA - timedelta(days=10 + n)
        c.execute("INSERT INTO meta_referrals (phone, source_id, headline, ts, plataforma) VALUES (?,?,?,?,?)",
                  (ph, "AD_MG", "h", int(t.timestamp()), "facebook" if n % 2 else "instagram"))
        if n < 4:
            pid = 7000 + n
            c.execute("INSERT INTO citas_bot (phone, id_cita, especialidad, fecha, hora, created_at, ad_source_id, ad_plataforma, "
                      "id_paciente_medilink) VALUES (?,?,?,?,?,?,?,?,?)",
                      (ph, str(900 + n), "Medicina General", (t + timedelta(days=1)).date().isoformat(), "10:00",
                       utc(t + timedelta(minutes=5)), "AD_MG", "facebook", pid))
            if n < 3:
                c.execute("INSERT INTO ausentismo_citas (id_cita, id_profesional, id_paciente, fecha, hora, id_estado, estado_cita, "
                          "anulacion) VALUES (?,?,?,?,?,?,?,?)", (900 + n, 1, pid, (t + timedelta(days=1)).date().isoformat(),
                                                                  "10:00", 2, "", 0))
                c.execute("INSERT INTO bi_pagos_caja (pago_id, fecha, id_profesional, id_paciente, monto, metodo_pago) "
                          "VALUES (?,?,?,?,?,?)", (100 + n, (t + timedelta(days=1)).date().isoformat(), 1, pid, 20000, "Efectivo"))
    # Caja del mes y del anterior
    for k in range(5):
        c.execute("INSERT INTO bi_pagos_caja (pago_id, fecha, id_profesional, id_paciente, monto, metodo_pago) VALUES (?,?,?,?,?,?)",
                  (500 + k, HOY.replace(day=1).isoformat(), 77, 8000 + k, 10000, "Transferencia"))
    # HOY: tres personas escriben; una agenda, una espera a recepción, una es el teléfono del dueño (excluido)
    hoy_9 = AHORA.replace(hour=9, minute=0)
    for ph, mins in (("56977000001", 0), ("56977000002", 30), ("56900000000", 40), ("fb_123456", 50)):
        c.execute("INSERT INTO messages (phone, direction, text, state, ts) VALUES (?,?,?,?,?)",
                  (ph, "in", "Hola, mi RUT es 12.345.678-5", "IDLE", utc(hoy_9 + timedelta(minutes=mins))))
    c.execute("INSERT OR REPLACE INTO contact_profiles (phone, nombre) VALUES (?,?)", ("56977000001", "Ana María Pérez Soto"))
    c.execute("INSERT OR REPLACE INTO contact_profiles (phone, nombre) VALUES (?,?)", ("56977000002", "José Luis Rojas"))
    c.execute("INSERT INTO meta_referrals (phone, source_id, headline, ts, plataforma) VALUES (?,?,?,?,?)",
              ("56977000001", "AD_MG", "h", int(hoy_9.timestamp()) - 10, "instagram"))
    c.execute("INSERT INTO citas_bot (phone, id_cita, especialidad, fecha, hora, created_at, ad_source_id, ad_plataforma, paciente_nombre) "
              "VALUES (?,?,?,?,?,?,?,?,?)", ("56977000001", "999", "Kinesiología", (HOY + timedelta(days=1)).isoformat(), "10:00",
                                              utc(hoy_9 + timedelta(minutes=3)), "AD_MG", "instagram", "Ana María Pérez Soto"))
    c.execute("INSERT INTO messages (phone, direction, text, state, ts) VALUES (?,?,?,?,?)",
              ("56977000002", "in", "¿hay hora?", "HUMAN_TAKEOVER", utc(hoy_9 + timedelta(minutes=31))))
    c.execute("INSERT OR REPLACE INTO sessions (phone, state, data, updated_at) VALUES (?,?,?,?)",
              ("56977000002", "HUMAN_TAKEOVER", json.dumps({"especialidad": "ortodoncia"}), utc(hoy_9 + timedelta(minutes=31))))
    # Agenda de mañana
    for prof, lib in ((1, 3), (77, 2)):
        c.execute("INSERT INTO agenda_cupos_cache (id_profesional, fecha, libres, primera_hora, actualizado_ts) VALUES (?,?,?,?,?)",
                  (prof, (HOY + timedelta(days=1)).isoformat(), lib, "09:00", int(AHORA.timestamp()) - 3600))
    # Bitácora
    c.execute("INSERT INTO capi_purchase_corridas (fecha_corrida, enviados, diferidos, no_atendidos, duplicados, errores, value_total, "
              "estimados) VALUES (?,?,?,?,?,?,?,?)", (AHORA.replace(hour=7, minute=7).isoformat(), 5, 0, 1, 0, 0, 40000, 0))
    c.execute("INSERT INTO conversation_events (phone, event, meta, ts) VALUES (?,?,?,?)",
              ("centinela", "centinela_diario", json.dumps({"hallazgos": 2}), utc(AHORA.replace(hour=7, minute=30))))
    for st in ("accepted", "accepted", "declined"):
        c.execute("INSERT INTO conversation_events (phone, event, meta, ts) VALUES (?,?,?,?)",
                  ("56911112222", "marketing_consent_respuesta", json.dumps({"status": st, "raw": "Sí, actívenlos"}),
                   utc(AHORA - timedelta(days=1))))
    # Experimentos: 10 con hora (3 agendan), 10 sin hora (1 agenda)
    for n in range(20):
        ph = f"5693300{n:04d}"
        t = AHORA - timedelta(days=5, hours=n)
        con = n < 10
        c.execute("INSERT INTO conversation_events (phone, event, meta, ts) VALUES (?,?,?,?)",
                  (ph, "reenganche_enviado", json.dumps({"variante": "a_punto_elegir_" + ("con_hora" if con else "sin_hora")}), utc(t)))
        if (con and n < 3) or (not con and n == 10):
            c.execute("INSERT INTO citas_bot (phone, id_cita, especialidad, created_at) VALUES (?,?,?,?)",
                      (ph, f"r{n}", "Medicina General", utc(t + timedelta(hours=3))))
    c.commit()
hm = sqlite3.connect(TMP / "heatmap_cache.db")
hm.execute("CREATE TABLE pacientes_heatmap (id INTEGER PRIMARY KEY, nombre TEXT, apellidos TEXT, rut TEXT, comuna TEXT, ciudad TEXT, "
           "direccion TEXT, fecha_nacimiento TEXT, sexo TEXT, celular TEXT, email TEXT)")
hm.commit()
hm.close()
rr.limpiar_cache()
_LLAMADAS_RED.clear()

# ── 4. Cada endpoint con datos ─────────────────────────────────────────────
D = {}
for r in RUTAS:
    res = cli.get(B + r, headers=H_)
    check(f"datos: {r} 200", res.status_code == 200)
    D[r] = res.json()
    errs = [k for k, v in D[r].items() if isinstance(v, dict) and "error" in v]
    check(f"datos: {r} sin fuentes caídas ({errs})", not errs)
check("ninguna llamada de red (ni Meta ni Medilink) al abrir el Radar", _LLAMADAS_RED == [])

# Pulso
p = D["pulso"]
check("pulso: 3 entradas hoy (el teléfono del dueño se excluye)", len(p["eventos"]) == 3)
canales = {e["canal"] for e in p["eventos"]}
check("pulso: canales detectados (anuncio IG, WhatsApp, Messenger)", canales == {"ig", "wa", "ms"})
est = {e["iniciales"]: e["estado"] for e in p["eventos"]}
check("pulso: agendó / con recepción", est.get("A.M.P.") == "agendo" and est.get("J.L.R.") == "recepcion")
check("pulso: citas de hoy y de Meta", p["citas_hoy"] == 1 and p["citas_hoy_meta"] == 1)
check("pulso: una persona esperando a recepción", p["esperando_total"] == 1 and p["esperando"][0]["especialidad"] == "Ortodoncia")
check("pulso: agenda de mañana con 5 cupos", p["agenda_manana"]["libres"] == 5 and not p["agenda_manana"]["sin_datos"])
check("pulso: decisión real con enlace a la acción", any(d["accion"]["href"] == "/admin/v2" for d in p["decisiones"]))
check("pulso: venta del mes = caja", p["venta_mes"]["venta"] == 50000 + sum(
    20000 for n in range(3) if (AHORA - timedelta(days=10 + n) + timedelta(days=1)).date() >= HOY.replace(day=1)))

# Privacidad del feed: ni teléfono, ni nombre completo, ni RUT
blob = json.dumps(D, ensure_ascii=False)
check("privacidad: ningún teléfono en las respuestas", not any(x in blob for x in ("56977000001", "56977000002", "56900000000",
                                                                                     "fb_123456", "5696660", "977000")))
check("privacidad: ningún nombre completo", "Pérez" not in blob and "Ana María" not in blob and "Rojas" not in blob)
check("privacidad: ningún RUT ni texto de mensajes", "12.345.678" not in blob and "hay hora" not in blob)
check("privacidad: solo iniciales en el feed", all(set(e) == {"id", "min", "hora", "canal", "estado", "especialidad", "iniciales"}
                                                    for e in p["eventos"]))

# Cifras = panel_data (misma función, mismo rango)
d0, h0 = rr._rango(HOY)
with cm.solo_datos_locales():
    pk = cm.panel_data(d0, h0, canal="meta")["kpis"]
    pkw = cm.panel_data(d0, h0, canal="web")["kpis"]
em = D["captacion"]["embudo"]["meta"]
check("cifras = panel_data: citas, atendidos, gasto, venta, centro, CAC",
      all(em[k] == pk[k] for k in ("citas", "atendidos", "gasto", "venta", "centro", "cac_atendido", "personas")))
check("cifras = panel_data: web", D["captacion"]["embudo"]["web"]["personas"] == pkw["personas"])
check("cifras: valores esperados del set sintético", em["citas"] == 5 and em["atendidos"] >= 3 and em["venta"] >= 60000)
fz = {c_["canal"]: c_ for c_ in D["finanzas"]["canales"]}
check("finanzas usa el mismo panel_data", fz["meta"]["gasto"] == pk["gasto"] and fz["meta"]["centro"] == pk["centro"])
check("finanzas: serie de 7 meses y mes en curso", len(D["finanzas"]["meses"]["serie"]) == 7
      and D["finanzas"]["meses"]["actual"]["venta"] >= 50000)
check("finanzas: para el centro = venta × (1 − pct)", (lambda a: a["centro"] < a["venta"])(D["finanzas"]["meses"]["actual"]))

# Conversión
ex = D["conversion"]["experimentos"]["reenganche"]
check("experimentos: con hora 3/10, sin hora 1/10", ex["con_hora"]["enviados"] == 10 and ex["con_hora"]["agendaron"] == 3
      and ex["sin_hora"]["agendaron"] == 1 and ex["con_hora"]["ic95"][0] < 30 < ex["con_hora"]["ic95"][1])
check("conversión: velocidad presente", "curva" in D["conversion"]["velocidad"])

# Bitácora
ev = {e["clave"]: e for e in D["bitacora"]["noche"]["eventos"]}
check("bitácora: aviso a Meta 07:07 real", ev.get("capi", {}).get("cuando", "").endswith("07:07-03:00") or "07:07" in ev.get("capi", {}).get("cuando", ""))
check("bitácora: centinela con hallazgos", ev.get("centinela", {}).get("estado") == "aviso")
check("bitácora: foto de Meta", "insights" in ev)
check("bitácora: lo que no deja rastro no se inventa (respaldo)", "respaldo" not in ev
      and any("Respaldo" in x for x in D["bitacora"]["noche"]["sin_rastro"]))
cs = D["bitacora"]["consentimiento"]
check("consentimiento: conteos agregados 2 sí / 1 no", cs["marketing"] == {"accepted": 2, "declined": 1})
check("consentimiento: sin datos por paciente", "56911112222" not in blob and "actívenlos" not in blob)

# Caché 5 min: una segunda lectura no recalcula
_calls = []
_orig_panel = cm.panel_data
cm.panel_data = lambda *a, **k: (_calls.append(1), _orig_panel(*a, **k))[1]
cli.get(B + "captacion", headers=H_)
check("caché: captación no recalcula panel_data dentro de 5 min", _calls == [])
cm.panel_data = _orig_panel

# ── 5. Plantilla y registro ────────────────────────────────────────────────
html = (RAIZ / "templates" / "alma_radar.html").read_text(encoding="utf-8")
check("plantilla: sin CDN ni fuentes externas", "googleapis" not in html and "https://" not in html and "cdn." not in html)
check("plantilla: Montserrat local", "/static/fonts/montserrat-var-latin.woff2" in html)
check("plantilla: token desde la URL con _tk", "const _tk=" in html and "URLSearchParams(location.search).get('token')" in html)
check("plantilla: escapa con esc()", "const esc=" in html and html.count("esc(") > 40)
check("plantilla: sin alert/confirm/prompt nativos", not any(x in html for x in ("alert(", "confirm(", "prompt(")))
check("plantilla: sin voseo", not any(w in html for w in ("tenés", "podés", "hacé ", "mirá", "elegí", "querés")))
check("plantilla: etiquetas Dato real e Ilustrativo", "Dato real" in html and "Ilustrativo" in html)
check("plantilla: refresco del pulso cada 60 s", "60000" in html)
check("config: módulo radar en el registro del shell", config.ALMA_MODULE_REGISTRY.get("radar", {}).get("src") == "/alma/radar")
check("config: radar NO está en la allowlist de recepción",
      "radar" not in (config.ALMA_PROFILES.get(config.ADMIN_TOKEN, {}).get("modulos") or []))
main_src = (RAIZ / "app" / "main.py").read_text(encoding="utf-8")
check("main.py registra API y página", "app.include_router(radar_routes.router)" in main_src
      and "app.include_router(radar_routes.pagina)" in main_src)

httpx.Client = _orig_client
print(f"\n{len(FALLAS)} fallas")
sys.exit(1 if FALLAS else 0)
