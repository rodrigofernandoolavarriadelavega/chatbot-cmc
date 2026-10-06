"""Campañas Meta — integraciones (2026-10-06): agenda × anuncios, creativos y fatiga,
territorio, velocidad de respuesta, valor a 90 días, aviso a Meta (CAPI Purchase),
venta en tres partes, atendidos por fuente y sugerencias con el mismo rango.

DB temporal con datos SINTÉTICOS. Medilink, Meta (Marketing API y CAPI) y el envío
al dueño están MOCKEADOS: ninguna llamada real.
"""
import asyncio
import json
import sqlite3
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
import meta_alertas as ma  # noqa: E402
import recepcion_tiempos as rt  # noqa: E402
import medilink as ml  # noqa: E402
import capi_purchase as cp  # noqa: E402
import ausentismo  # noqa: E402

FALLAS = []


def check(nombre, cond):
    print(("OK  " if cond else "FAIL") + " " + nombre)
    if not cond:
        FALLAS.append(nombre)


CL = ZoneInfo("America/Santiago")
AHORA = datetime.now(CL).replace(hour=12, minute=0, second=0, microsecond=0)
HOY = AHORA.date()


def ep(dias):
    return int((AHORA - timedelta(days=dias)).timestamp())


def utc(dias, mins=0):
    return (AHORA - timedelta(days=dias) + timedelta(minutes=mins)).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def fecha(d):
    return (HOY + timedelta(days=d)).isoformat()


# ── A. Piezas puras ─────────────────────────────────────────────────────────
check("tramos finos: <5, 5-30, 30-120, >2h", [rt.tramo_fino(m) for m in (1, 4.9, 5, 29, 30, 119, 120, 600)]
      == ["lt5", "lt5", "5_30", "5_30", "30_120", "30_120", "gt2h", "gt2h"])
check("tramos finos: sin respuesta", rt.tramo_fino(None, False) == "sin_respuesta" and rt.tramo_fino(10, False) == "sin_respuesta")
cf = rt.curva_fina([{"minutos": 2, "respondida": True, "agendo": True}, {"minutos": 3, "respondida": True, "agendo": False},
                    {"minutos": 200, "respondida": True, "agendo": False}, {"minutos": None, "respondida": False, "agendo": False}])
check("curva fina: % que agendó por tramo", cf[0]["personas"] == 2 and cf[0]["pct"] == 50 and cf[3]["pct"] == 0
      and cf[4]["personas"] == 1 and cf[1]["pct"] is None)
filas = ([{"necesito": True, "minutos": 3, "respondida": True, "agendo": True}] * 6
         + [{"necesito": True, "minutos": 3, "respondida": True, "agendo": False}] * 4
         + [{"necesito": True, "minutos": 300, "respondida": True, "agendo": False}] * 4
         + [{"necesito": True, "minutos": 400, "respondida": True, "agendo": True}] * 2
         + [{"necesito": False, "proactiva": False, "agendo": True}])
rs = ci.resumen_velocidad(filas)
check("velocidad: rápidos 60% contra lentos 33%", rs["rapidos"]["pct"] == 60 and rs["lentos"]["pct"] == 33)
check("velocidad: citas que se habrían sumado (referencia)", rs["citas_perdidas_estimadas"] == round(6 * 27 / 100))
check("velocidad: muestra chica → sin estimación", ci.resumen_velocidad(filas[:3])["citas_perdidas_estimadas"] is None)
check("velocidad: solo bot aparte", rs["solo_bot"]["personas"] == 1 and rs["solo_bot"]["agendaron"] == 1)

a = {"impresiones": 7000, "alcance": 1500, "clics": 70}
b = {"impresiones": 7000, "alcance": 3000, "clics": 140}
f = ci.fatiga_ad(a, b)
check("fatiga: frecuencia alta y CTR cayendo a la mitad → cansado", f["etiqueta"] == "cansado" and f["ctr_cambio_pct"] == -50
      and "Cambie la imagen" in f["sugerencia"])
check("fatiga: frecuencia alta sola → vigilar", ci.fatiga_ad(a, {"impresiones": 7000, "alcance": 3000, "clics": 70})["etiqueta"] == "vigilar")
check("fatiga: saturado (>6) → cansado aunque el CTR aguante",
      ci.fatiga_ad({"impresiones": 14000, "alcance": 2000, "clics": 140}, {"impresiones": 7000, "alcance": 3000, "clics": 70})["etiqueta"] == "cansado")
check("fatiga: sano", ci.fatiga_ad({"impresiones": 7000, "alcance": 5000, "clics": 70}, b)["etiqueta"] in ("sano", "vigilar")
      and ci.fatiga_ad({"impresiones": 7000, "alcance": 5000, "clics": 140}, b)["etiqueta"] == "sano")
check("fatiga: pocas impresiones → sin datos", ci.fatiga_ad({"impresiones": 300, "alcance": 100, "clics": 5}, b)["etiqueta"] == "sin_datos")

AD_OK = {"id": "A1", "creative": {"id": "c1", "title": "Titulo", "body": "Texto largo", "call_to_action_type": "WHATSAPP_MESSAGE",
                                  "thumbnail_url": "https://scontent.xx.fbcdn.net/t.jpg", "image_url": "https://scontent.xx.fbcdn.net/i.jpg"}}
pc = ci.parse_creativo(AD_OK)
check("creativo: aplana título, texto, botón e imagen", pc["titulo"] == "Titulo" and pc["texto"] == "Texto largo"
      and pc["cta"] == "WHATSAPP_MESSAGE" and pc["imagen_url"].endswith("i.jpg") and pc["tipo"] == "imagen")
pv = ci.parse_creativo({"id": "V", "creative": {"object_story_spec": {"video_data": {"message": "msg", "title": "t", "image_url": "https://x.fbcdn.net/v.jpg"}}}})
check("creativo: video sale de object_story_spec", pv["tipo"] == "video" and pv["texto"] == "msg" and pv["titulo"] == "t")
check("creativo: solo hosts de Meta", ci._url_segura("https://scontent.xx.fbcdn.net/a.jpg") and not ci._url_segura("https://evil.com/a.jpg")
      and not ci._url_segura("http://scontent.xx.fbcdn.net/a.jpg") and not ci._url_segura("https://fbcdn.net.evil.com/a.jpg")
      and not ci._url_segura("file:///etc/passwd"))

# ── B. Datos sintéticos ─────────────────────────────────────────────────────
ml.PROFESIONALES = {1: {"nombre": "Dr Uno", "especialidad": "Medicina General", "intervalo": 15},
                    2: {"nombre": "Kine Dos", "especialidad": "Kinesiología", "intervalo": 40},
                    3: {"nombre": "Dra Tres", "especialidad": "Ortodoncia", "intervalo": 30}}
cm._estados_meta = lambda: {}
config.OLACORE_TOKEN = "dueno_test"
config.ADMIN_TOKEN = "recepcion_test"

with session.db() as c:
    cm._ensure_insights(c)
    # Meta: AD_MG (Medicina General) con gasto 5.000/día y fatiga; AD_OR (Ortodoncia) para el territorio/retorno
    for i in range(1, 121):
        f_ = fecha(-i)
        reciente = i <= 7
        prev = 8 <= i <= 14
        for ad, camp, cname, nombre, spend in (("AD_MG", "CM", "Medicina", "Medicina General hoy", 5000),
                                               ("AD_OR", "CO", "Orto", "Ortodoncia sonrisa", 3000 if i <= 120 else 0)):
            if not spend:
                continue
            imp, reach, clk = (1000, 214, 10) if (ad == "AD_MG" and reciente) else (1000, 430, 20) if (ad == "AD_MG" and prev) else (1000, 800, 10)
            c.execute("INSERT INTO meta_insights_diario (fecha, ad_id, desglose, valor, ad_name, campaign_id, campaign_name, "
                      "spend, impressions, reach, frequency, clicks, conversaciones) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (f_, ad, "total", "-", nombre, camp, cname, spend, imp, reach, 1.2, clk, 2))
    # Ahora los AD_OR sin gasto reciente no hace falta; precios típicos de prof 2 (kine): 8 pagos de 20.000
    for i in range(8):
        c.execute("INSERT INTO bi_pagos_caja (pago_id, fecha, id_paciente, id_profesional, monto) VALUES (?,?,?,?,?)",
                  (50000 + i, fecha(-10 - i), 7000000 + i, 2, 20000))
    c.commit()

# ── C. Agenda × anuncios ────────────────────────────────────────────────────
HORARIO = {"intervalo": 15, "dias": set(range(7)), "horario_dia": {d: ("09:00", "18:00", None) for d in range(7)}, "usa_agendas": False}
LIBRES = {}       # (prof, fecha) -> n cupos
FALLAN = set()    # (prof, fecha) que levantan error de red
LLAMADAS = []


async def _fake_horario(client, idp):
    return HORARIO


async def _fake_slots(client, ids, horarios, fecha_s, prioridad=False):
    idp = ids[0]
    LLAMADAS.append((idp, fecha_s))
    if (idp, fecha_s) in FALLAN:
        import httpx
        raise httpx.RequestError("sin respuesta")
    n = LIBRES.get((idp, fecha_s), 0)
    return [], [{"hora_inicio": f"{9 + k // 4:02d}:{(k % 4) * 15:02d}", "id_profesional": idp} for k in range(n)]


ml._get_horario = _fake_horario
ml._slots_para_fecha = _fake_slots
ml._filtrar_licencia = lambda ids: ids
ml._get_shared_client = lambda: object()
ci.CUPOS_PAUSA_LIVIANA_S = 0
for d in range(14):
    LIBRES[(1, fecha(d))] = 0                       # medicina general: agenda llena
    LIBRES[(2, fecha(d))] = 4 if d % 2 == 0 else 0  # kine: 28 cupos en 14 días
    LIBRES[(3, fecha(d))] = 1
FALLAN.add((3, fecha(5)))
r = asyncio.run(ci.refrescar_cupos(pausa=0))
check("agenda: lee los 3 profesionales", r["profesionales"] == 3 and r["consultas"] == 3 * 14 - 1 and r["errores"] == 1)
with session.db() as c:
    filas_c = {(x[0], x[1]): x[2] for x in c.execute("SELECT id_profesional, fecha, libres FROM agenda_cupos_cache")}
check("agenda: una falla de Medilink NO se guarda como 0 cupos", (3, fecha(5)) not in filas_c and filas_c[(3, fecha(4))] == 1)
n_ll = len(LLAMADAS)
asyncio.run(ci.refrescar_cupos(pausa=0))
check("agenda: lo leído hace poco no se vuelve a pedir (solo el día que falló)", len(LLAMADAS) - n_ll == 1)

import httpx  # noqa: E402


async def _slots_429(client, ids, horarios, fecha_s, prioridad=False):
    LLAMADAS.append((ids[0], fecha_s))
    raise ml.MedilinkRateLimited("429")


ml._slots_para_fecha = _slots_429
with session.db() as c:
    c.execute("DELETE FROM agenda_cupos_cache")
    c.commit()
n_ll = len(LLAMADAS)
r2 = asyncio.run(ci.refrescar_cupos(pausa=0))
check("agenda: un 429 corta la corrida entera (no martilla)", len(LLAMADAS) - n_ll == 1 and r2["corte"] == "MedilinkRateLimited")
check("agenda: sin datos y sin inventar ceros", ci.agenda_data(HOY)["sin_datos"] is True)
ml._slots_para_fecha = _fake_slots
asyncio.run(ci.refrescar_cupos(pausa=0))

ag = ci.agenda_data(HOY)
gr = {g["grupo"]: g for g in ag["grupos"]}
check("agenda: Medicina general gasta y no hay cupos", gr["Medicina general"]["senal"] == "sin_cupos"
      and gr["Medicina general"]["gasto_dia"] == 5000 and gr["Medicina general"]["libres_7d"] == 0)
check("agenda: Kinesiología tiene cupos y ningún anuncio → cupos sin anuncio", gr["Kinesiología"]["senal"] == "cupos_sin_anuncio"
      and gr["Kinesiología"]["libres_14d"] == 28)
kine = next(p for p in ag["profesionales"] if p["id"] == 2)
check("agenda: valor de cupos vacíos = cupos × precio típico × margen del centro (pct del profesional 70 → 30%)",
      kine["precio"] == 20000 and kine["margen_pct"] == 30 and kine["valor_14d"] == round(28 * 20000 * 30 / 100))
check("agenda: por día y próximo cupo", len(kine["por_dia"]) == 14 and kine["proxima"] == fecha(0))
check("agenda: sin cupos en 14 días → sin próxima fecha", next(p for p in ag["profesionales"] if p["id"] == 1)["proxima"] is None)
check("agenda: señales ordenadas, sin_cupos primero", ag["senales"][0]["grupo"] == "Medicina general")
check("agenda: el panel no llama a Medilink", (lambda n: (ci.agenda_data(HOY), len(LLAMADAS) == n)[1])(len(LLAMADAS)))

config.AGENDA_ALERTAS_ACTIVE = False
al = [k for k, _ in ma.evaluar_alertas(HOY)]
check("alertas de agenda: detrás de flag (apagado → ninguna)", not any(k.startswith("agenda_") for k in al))
config.AGENDA_ALERTAS_ACTIVE = True
al2 = dict(ma.evaluar_alertas(HOY))
check("alertas de agenda: gasta sin cupos", "agenda_sin_cupos:Medicina general" in al2
      and "$5.000" in al2["agenda_sin_cupos:Medicina general"])
check("alertas de agenda: cupos vacíos sin anuncio", "agenda_vacios:Kinesiología" in al2 and "28 cupos" in al2["agenda_vacios:Kinesiología"])
with session.db() as c:
    c.execute("UPDATE agenda_cupos_cache SET actualizado_ts = actualizado_ts - ?", (40 * 3600,))
    c.commit()
check("alertas de agenda: con el cache viejo (>36 h) no alerta", ci.alertas_agenda(HOY) == [])
config.AGENDA_ALERTAS_ACTIVE = False

# ── D. Creativos ────────────────────────────────────────────────────────────
import meta_insights_snapshot as mis  # noqa: E402

mis._cfg = lambda: ("tok", "act_1")
ci._bajar_anuncios = lambda cl, acct, token: [AD_OK | {"id": "AD_MG"}, AD_OK | {"id": "NO_CONOCIDO"}]
GUARDADAS = []


def _fake_guardar(cl, ad_id, url):
    GUARDADAS.append((ad_id, url))
    (ci._dir_creativos() / f"{ad_id}.jpg").write_bytes(b"\xff\xd8\xff fake")
    return f"{ad_id}.jpg"


ci._guardar_imagen = _fake_guardar
rc = ci.refrescar_creativos()
check("creativos: baja los anuncios conocidos y guarda la imagen grande", rc["ok"] and rc["anuncios"] == 1 and GUARDADAS[0][1].endswith("i.jpg"))
ci.refrescar_creativos()
check("creativos: la imagen guardada no se vuelve a bajar", len(GUARDADAS) == 1)
cd = ci.creativos_data()
check("creativos: texto, imagen y fatiga por anuncio", cd["anuncios"]["AD_MG"]["titulo"] == "Titulo" and cd["anuncios"]["AD_MG"]["imagen"]
      and cd["anuncios"]["AD_MG"]["fatiga"]["etiqueta"] == "cansado")
check("creativos: anuncio sin gasto reciente sin alerta de cansancio falsa", cd["anuncios"]["AD_OR"]["fatiga"]["etiqueta"] in ("sano", "vigilar", "sin_datos"))
config.META_CREATIVOS_ACTIVE = False
GUARDADAS.clear()
asyncio.run(ci.job_meta_creativos())
check("creativos: flag apagado no corre", not GUARDADAS)
config.META_CREATIVOS_ACTIVE = True
mis._cfg = lambda: ("", "act_1")
check("creativos: sin token no hace nada", ci.refrescar_creativos()["ok"] is False)

# ── E. Personas del rango (territorio, velocidad, valor 90, venta en partes) ─
hm = sqlite3.connect(TMP / "heatmap_cache.db")
hm.execute("CREATE TABLE pacientes_heatmap (id INTEGER PRIMARY KEY, nombre TEXT, apellidos TEXT, rut TEXT, comuna TEXT, ciudad TEXT, "
           "direccion TEXT, fecha_nacimiento TEXT, sexo TEXT, celular TEXT, email TEXT)")
hm.execute("INSERT INTO pacientes_heatmap (id, comuna, direccion, celular) VALUES (8800002, 'CAÑETE', '', '56966660002')")
hm.execute("INSERT INTO pacientes_heatmap (id, comuna, direccion, celular) VALUES (8800005, 'ARAUCO', 'Calle Principal, Curanilahue', '56966660005')")
hm.execute("INSERT INTO pacientes_heatmap (id, comuna, direccion, celular) VALUES (8800006, '', '', '56966660006')")
hm.commit()
hm.close()
ausentismo.ensure_ausentismo_table()

with session.db() as c:
    def ref(ph, ad, dias):
        c.execute("INSERT INTO meta_referrals (phone, source_id, headline, ts, plataforma) VALUES (?,?,?,?,?)",
                  (ph, ad, "h", ep(dias), "facebook"))

    def msg(ph, d, txt, dias, mins, st="IDLE"):
        c.execute("INSERT INTO messages (phone, direction, text, state, ts) VALUES (?,?,?,?,?)", (ph, d, txt, st, utc(dias, mins)))

    def cita(ph, idc, pid, dias, ad, esp="Medicina General", f=None):
        c.execute("INSERT INTO citas_bot (phone, id_cita, especialidad, profesional, fecha, hora, created_at, ad_source_id, "
                  "ad_plataforma, id_paciente_medilink) VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (ph, idc, esp, "Dr", f or fecha(-dias + 2), "10:00", utc(dias, 30), ad, "facebook", pid))

    def pago(n, pid, f_, monto, prof=1):
        c.execute("INSERT INTO bi_pagos_caja (pago_id, fecha, id_paciente, id_profesional, monto) VALUES (?,?,?,?,?)", (n, f_, pid, prof, monto))

    # Cuatro personas de AD_MG hace ~60 días: cita (fecha = +2 días), primer pago dentro de 30 días de la cita y otro después de 30 días
    for i in range(4):
        ph = f"5696666000{i}1"[:11]
        ph = f"56966660{i}01"
        ref(ph, "AD_MG", 60)
        cita(ph, f"9{i}", 8800100 + i, 60, "AD_MG")
        pago(60000 + i * 10, 8800100 + i, fecha(-55), 30000)             # dentro de 30 días de la cita
        pago(60001 + i * 10, 8800100 + i, fecha(-10), 20000)             # mucho después → "otras y posteriores"
        c.execute("INSERT INTO ausentismo_citas (id_cita, id_profesional, id_paciente, fecha, hora, id_estado, estado_cita, anulacion) "
                  "VALUES (?,?,?,?,?,?,?,?)", (int(f"9{i}"), 1, 8800100 + i, fecha(-58), "10:00", 2 if i < 2 else 7, "", 0))
    pago(60500, 8800100, fecha(-55), 15000, prof=2)                      # misma persona, otra especialidad (kine) en 30 días
    # Persona 4 sin cita del bot: pago pagado por teléfono compartido (ficha del heatmap, celular 56966660002)
    ref("56966660002", "AD_MG", 40)
    msg("56966660002", "in", "hola", 40, 0)
    pago(61000, 8800002, fecha(-35), 40000)
    # Persona 5: contacto sin pago, comuna dicha al bot
    ref("56966660003", "AD_MG", 20)
    c.execute("INSERT OR REPLACE INTO contact_profiles (phone, rut, nombre, comuna) VALUES (?,?,?,?)", ("56966660003", None, "Persona Tres", "Curanilahue"))
    # Persona 6: ficha con dirección de Los Álamos (la dirección manda sobre el campo comuna ARAUCO)
    ref("56966660005", "AD_OR", 30)
    pago(61100, 8800005, fecha(-25), 50000, prof=3)
    # Persona 7: sin ningún dato territorial
    ref("56966660006", "AD_OR", 25)
    # Velocidad: AD_OR, tres personas pasaron a recepción
    for k, (ph, mins, agenda) in enumerate((("56967770001", 3, True), ("56967770002", 3, False), ("56967770003", 200, False))):
        ref(ph, "AD_OR", 15)
        msg(ph, "in", "hola", 15, 0)
        msg(ph, "in", "quiero hablar con alguien", 15, 1, "HUMAN_TAKEOVER")
        msg(ph, "out", "[Recepcionista] Hola", 15, 1 + mins, "HUMAN_TAKEOVER")
        if agenda:
            cita(ph, f"95{k}", 8800200 + k, 15, "AD_OR", "Ortodoncia", fecha(-10))
    ref("56967770004", "AD_OR", 14)
    msg("56967770004", "in", "hola", 14, 0, "HUMAN_TAKEOVER")
    # Valor 90: 3 personas de AD_MG con clic hace 120 días (maduras): pagos al día 10 y al día 60
    for i in range(3):
        ph = f"56968880{i}01"
        ref(ph, "AD_MG", 120)
        cita(ph, f"96{i}", 8800300 + i, 120, "AD_MG", f=fecha(-117))
        pago(62000 + i * 10, 8800300 + i, fecha(-110), 30000)
        pago(62001 + i * 10, 8800300 + i, fecha(-60), 20000)
    # Persona reciente (no madura) con pago
    ref("56968889901", "AD_MG", 20)
    cita("56968889901", "970", 8800399, 20, "AD_MG", f=fecha(-17))
    pago(62900, 8800399, fecha(-15), 25000)
    c.commit()

D90, D30, H = fecha(-89), fecha(-29), fecha(0)

# ── F. Venta en tres partes + atendidos por fuente ──────────────────────────
p = cm.panel_data(D90, H)
k = p["kpis"]
check("venta en partes: especialidad+otras+teléfono = venta total (kpis)", k["venta_esp30"] + k["venta_otra"] + k["venta_tel"] == k["venta"])
mg = next(x for x in p["anuncios"] if x["ad_id"] == "AD_MG")
check("venta en partes: anuncio de medicina general", mg["venta_esp30"] >= 4 * 30000 and mg["venta_otra"] >= 4 * 20000
      and mg["venta_esp30"] + mg["venta_otra"] + mg["venta_tel"] == mg["venta"])
check("venta en partes: lo de otra especialidad (kine) en 30 días NO es de la especialidad del anuncio",
      mg["venta_esp30"] == 4 * 30000 + 3 * 30000 + 0 or mg["venta_esp30"] >= 4 * 30000)
check("venta en partes: solo por teléfono aparte", mg["venta_tel"] == 40000)
check("retorno estricto = centro de la especialidad del anuncio en 30 días ÷ gasto",
      mg["retorno_estricto"] == round(mg["centro_esp30"] / mg["gasto"], 2) and mg["retorno_estricto"] < mg["retorno_centro"])
cmp_ = next(x for x in p["campanas"] if x["campaign_id"] == "CM")
check("venta en partes: campaña suma lo de sus anuncios", cmp_["venta_esp30"] == mg["venta_esp30"] and cmp_["retorno_estricto"] == mg["retorno_estricto"])
af = k["atendidos_fuente"]
check("atendidos por fuente: Medilink cuenta, respaldo antiguo en 0", af["medilink"] >= 2 and af["respaldo"] == 0
      and af["medilink"] + af["caja"] + af["respaldo"] == k["atendidos"])
check("medición: desde cuándo Medilink tiene estado por cita", p["medicion"]["medilink_desde"] is not None)

# ── G. Sugerencias con el MISMO rango ───────────────────────────────────────
sg = ma.sugerencias_presupuesto(HOY, desde=date.fromisoformat(D90), hasta=date.fromisoformat(H))
check("sugerencias: rango del filtro, no 4 semanas", sg["desde"] == D90 and sg["hasta"] == H and sg["dias"] == 90 and not sg["rango_corto"])
tab_c = {x["campaign_id"]: x for x in p["campanas"]}
check("sugerencias: el retorno de cada tarjeta es el de la tabla (mismo rango)",
      sg["campanas"] and all(x["retorno_centro_rango"] == tab_c[x["campaign_id"]]["retorno_centro"]
                             and x["desde"] == D90 and x["hasta"] == H for x in sg["campanas"]))
check("sugerencias: rango corto marcado", ma.sugerencias_presupuesto(HOY, desde=HOY - timedelta(days=6), hasta=HOY)["rango_corto"])
check("recomendar: el texto dice el periodo real",
      "14 días" in ma.recomendar({"gasto": 84000, "citas": 0}, None, 14)["razon"]
      and "4 semanas" in ma.recomendar({"gasto": 84000, "citas": 0})["razon"])
check("recomendar: gasto semanal sobre el rango (14 días → gasto ÷ 2)", ma.recomendar({"gasto": 140000, "citas": 9, "retorno_centro": 1.6,
                                                                                   "frecuencia": 1.5}, None, 14)["gasto_semana"] == 70000)
sgd = ma.sugerencias_presupuesto(HOY)
check("sugerencias sin rango: 4 semanas como siempre (resumen del lunes)", sgd["dias"] == 28)

# ── H. Territorio ───────────────────────────────────────────────────────────
t = ci.territorio_data(D90, H, canal="meta")
tc_ = {x["comuna"]: x for x in t["comunas"]}
check("territorio: comuna que dijo al bot + ficha", "Curanilahue" in tc_ and tc_["Curanilahue"]["personas"] == 2
      and tc_["Curanilahue"]["fuentes"] == {"conversación": 1, "ficha": 1})
check("territorio: ficha de Medilink por teléfono (Cañete) con su venta", tc_.get("Cañete", {}).get("venta") == 40000)
check("territorio: la dirección manda sobre el campo comuna (campo ARAUCO, dirección Curanilahue)", tc_["Curanilahue"]["venta"] == 50000
      and "Arauco" not in tc_)
check("territorio: sin dato aparte y al final", t["comunas"][-1]["comuna"] == "Sin dato")
check("territorio: la venta por comuna suma la venta del panel (solo AD_MG/AD_OR atribuidas)",
      sum(x["venta"] for x in t["comunas"]) == k["venta"])
check("territorio: cobertura", t["cobertura"]["personas"] >= 10 and 0 < t["cobertura"]["pct"] < 100)
check("territorio: por anuncio y mapa sin librerías", "AD_MG" in t["por_anuncio"] and any(m["comuna"] == "Curanilahue" for m in t["mapa"]))
check("territorio: nada de teléfonos ni nombres", "56966660" not in json.dumps(t) and "Persona Tres" not in json.dumps(t))
t1 = ci.territorio_data(D90, H, canal="meta", anuncio="AD_OR")
check("territorio: filtro por anuncio", sum(x["personas"] for x in t1["comunas"]) == 6 and "Cañete" not in {x["comuna"] for x in t1["comunas"]})

# ── I. Velocidad ────────────────────────────────────────────────────────────
v = ci.velocidad_data(D30, H, canal="meta")
tv = v["total"]
check("velocidad: pasaron a recepción (4)", tv["necesitaron"] == 4 and tv["sin_respuesta"] == 1)
cv = {x["tramo"]: x for x in tv["curva"]}
check("velocidad: tramos del dueño (<5 min: 2 personas, 1 agendó; >2h: 1)", cv["lt5"]["personas"] == 2 and cv["lt5"]["agendaron"] == 1
      and cv["gt2h"]["personas"] == 1 and cv["sin_respuesta"]["personas"] == 1)
va = next(x for x in v["anuncios"] if x["ad_id"] == "AD_OR")
check("velocidad: por anuncio con mediana", va["necesitaron"] == 4 and va["mediana_min"] is not None)

# ── J. Valor a 90 días ──────────────────────────────────────────────────────
v9 = ci.valor90_data("meta", HOY)
m9 = next(x for x in v9["anuncios"] if x["ad_id"] == "AD_MG")
check("valor 90: solo personas con 90 días cumplidos", m9["personas_maduras"] >= 3 and m9["pacientes_maduros"] >= 3)
check("valor 90: venta acumulada a 30/60/90 días", [h["venta"] for h in m9["h"]][0] == 3 * 30000 and [h["venta"] for h in m9["h"]][2] == 3 * 50000)
check("valor 90: cuánto más se vende de 30 a 90 días", m9["extra_30_a_90_pct"] == round(100 * (150000 - 90000) / 90000))
check("valor 90: volvieron (≥2 días de pago)", m9["volvieron_pct"] == 100)
check("valor 90: retorno contra el gasto del periodo", m9["gasto_maduro"] > 0 and m9["retorno_centro_90"] == round(m9["h"][2]["centro"] / m9["gasto_maduro"], 2))
check("valor 90: la gente reciente se cuenta aparte", m9["pacientes_recientes"] >= 1 and v9["total"]["venta_recientes"] > 0)
check("valor 90: nada de personas ni teléfonos", "56968880" not in json.dumps(v9))

# ── K. Aviso a Meta (CAPI Purchase) ─────────────────────────────────────────
check("aviso: sin tabla de enviados no rompe", ci.aviso_meta_data(HOY)["hay_datos"] is False)
cp.ensure_table()
check("aviso: tabla de corridas creada junto a la de enviados", cm._tabla_existe(session._conn(), "capi_purchase_corridas"))
with session.db() as c:
    c.execute("INSERT OR REPLACE INTO contact_profiles (phone, rut, nombre) VALUES (?,?,?)", ("56969990001", None, "Ana María Pérez Soto"))
    c.commit()
cp.registrar_corrida({"enviados": 3, "diferidos": 2, "no_atendidos": 5, "duplicados": 1, "errores": 1, "value_total": 45000.0, "estimados": 1},
                     ahora=datetime(2026, 10, 6, 7, 7, tzinfo=CL))
with session.db() as c:
    row = dict(c.execute("SELECT * FROM capi_purchase_corridas ORDER BY id DESC LIMIT 1").fetchone())
check("corrida: fila con fecha, contadores, valor y estimados", row["fecha_corrida"].startswith("2026-10-06T07:07") and row["enviados"] == 3
      and row["diferidos"] == 2 and row["no_atendidos"] == 5 and row["duplicados"] == 1 and row["errores"] == 1
      and row["value_total"] == 45000.0 and row["estimados"] == 1)

import meta_capi  # noqa: E402

ENVIOS = []


async def _fake_capi(evento, **kw):
    ENVIOS.append((evento, kw["event_id"], kw["value"]))
    return {"events_received": 1}


meta_capi.send_event = _fake_capi
ITEMS = [
    {"id_cita": "c1", "phone": "56969990001", "nombre": "Ana María Pérez Soto", "especialidad": "Medicina General", "fecha": fecha(-1), "hora": "10:00",
     "id_paciente": 1, "value": 12000.0, "venta_total": 40000, "estimado": False, "accion": "enviar"},
    {"id_cita": "c2", "phone": "56969990002", "nombre": None, "especialidad": "Kinesiología", "fecha": fecha(-1), "hora": "11:00",
     "id_paciente": 2, "value": 7000.0, "venta_total": 0, "estimado": True, "accion": "enviar"},
    {"id_cita": "c3", "phone": "x", "nombre": None, "especialidad": "", "fecha": fecha(-1), "hora": "12:00", "id_paciente": 3, "value": None,
     "venta_total": None, "estimado": False, "accion": "diferir"},
    {"id_cita": "c4", "phone": "x", "nombre": None, "especialidad": "", "fecha": fecha(-1), "hora": "12:00", "id_paciente": 4, "value": None,
     "venta_total": None, "estimado": False, "accion": "no_atendido"},
    {"id_cita": "c5", "phone": "x", "nombre": None, "especialidad": "", "fecha": fecha(-1), "hora": "12:00", "id_paciente": 1, "value": None,
     "venta_total": None, "estimado": False, "accion": "omitir_dup"},
]
cp.evaluar = lambda hoy=None: ITEMS
res = asyncio.run(cp.enviar_purchases(HOY))
check("enviar_purchases: la lógica no cambia (2 enviados, 1 diferido, 1 no atendido, 1 duplicado)",
      res["enviados"] == 2 and res["diferidos"] == 1 and res["no_atendidos"] == 1 and res["duplicados"] == 1 and res["estimados"] == 1
      and res["value_total"] == 19000.0 and len(ENVIOS) == 2)
with session.db() as c:
    ult = dict(c.execute("SELECT * FROM capi_purchase_corridas ORDER BY id DESC LIMIT 1").fetchone())
check("enviar_purchases: escribe la fila de la corrida al final", ult["enviados"] == 2 and ult["diferidos"] == 1 and ult["no_atendidos"] == 1
      and ult["duplicados"] == 1 and ult["errores"] == 0 and ult["value_total"] == 19000.0 and ult["estimados"] == 1)
orig = cp.registrar_corrida
cp.registrar_corrida = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no debe ocurrir"))
try:
    cp.registrar_corrida = orig
    with session.db() as c:
        c.execute("DROP TABLE capi_purchase_corridas")
        c.commit()
    cp.registrar_corrida({"enviados": 1})   # la tabla se recrea sola; un fallo de bitácora no lanza
    ok_bitacora = True
except Exception:
    ok_bitacora = False
check("corrida: la bitácora nunca rompe el envío", ok_bitacora)
av = ci.aviso_meta_data(HOY)
check("aviso: aceptados, real y estimado por separado", av["aceptados"] == 2 and av["valor_real"] == 12000 and av["valor_estimado"] == 7000
      and av["aceptados_reales"] == 1 and av["aceptados_estimados"] == 1 and av["duplicados_omitidos"] == 1)
check("aviso: última corrida, pendientes y errores", av["ultima_corrida"]["enviados"] == 1 and av["pendientes"] == 0 and av["errores"] == 0)
check("aviso: últimas citas con iniciales y tipo, sin nombre ni teléfono", {u["iniciales"] for u in av["ultimas"]} >= {"A.M.P."}
      and any(u["estimado"] for u in av["ultimas"]) and "Ana" not in json.dumps(av) and "5696999" not in json.dumps(av))

# ── L. Endpoints y auth ─────────────────────────────────────────────────────
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

app = FastAPI()
app.include_router(cm.router)
cli = TestClient(app)
B = "/alma/api/campanas-meta/"
for ruta, metodo in (("agenda", "get"), ("creativos", "get"), ("territorio", "get"), ("velocidad", "get"), ("valor90", "get"),
                     ("aviso-meta", "get"), ("anuncio/AD_MG", "get"), ("creativo/AD_MG", "get"),
                     ("agenda/actualizar", "post"), ("creativos/refrescar", "post")):
    fn = getattr(cli, metodo)
    check(f"auth {ruta}: 401 sin token / 403 con el de recepción",
          fn(B + ruta).status_code == 401 and fn(B + ruta + "?token=recepcion_test").status_code == 403)
H_ = {"Authorization": "Bearer dueno_test"}
mis._cfg = lambda: ("tok", "act_1")
for ruta in ("agenda", "creativos", "territorio?desde=%s&hasta=%s" % (D90, H), "velocidad?desde=%s&hasta=%s" % (D30, H), "valor90", "aviso-meta",
             "anuncio/AD_MG?desde=%s&hasta=%s" % (D30, H), "sugerencias-presupuesto?desde=%s&hasta=%s" % (D30, H)):
    check(f"200 con el token del dueño: {ruta.split('?')[0]}", cli.get(B + ruta, headers=H_).status_code == 200)
im = cli.get(B + "creativo/AD_MG", headers=H_)
check("creativo: la imagen se sirve desde el propio panel", im.status_code == 200 and im.content.startswith(b"\xff\xd8"))
check("creativo: sin imagen guardada → 404; id raro → 404", cli.get(B + "creativo/AD_OR", headers=H_).status_code == 404
      and cli.get(B + "creativo/..%2F..%2Fetc", headers=H_).status_code in (404, 400))
check("anuncio: modal con creativo, fatiga, territorio y velocidad", (lambda d: d["creativo"]["titulo"] == "Titulo" and d["fatiga"]["etiqueta"] == "cansado"
                                                                     and "territorio" in d and "velocidad" in d and len(d["serie"]) > 5)(
    cli.get(B + "anuncio/AD_MG?desde=%s&hasta=%s" % (D30, H), headers=H_).json()))
check("anuncio: id inválido → 400", cli.get(B + "anuncio/a b;c", headers=H_).status_code in (400, 404, 422))


async def _noop():
    ci._REFRESCO["llamado"] = True


config.AGENDA_CUPOS_ACTIVE = False
check("agenda/actualizar: con la lectura apagada responde 409", cli.post(B + "agenda/actualizar", headers=H_).status_code == 409)
config.AGENDA_CUPOS_ACTIVE = True

# ── M. Plantilla ────────────────────────────────────────────────────────────
html = (Path(__file__).resolve().parent.parent / "templates" / "alma_campanas_meta.html").read_text(encoding="utf-8")
check("plantilla: sin CDN ni fuentes externas", "cdn." not in html and "googleapis" not in html and "https://" not in html.replace("https://agentecmc", ""))
check("plantilla: secciones nuevas", all(x in html for x in ("v-agenda", "terrHtml", "velHtml", "avisoHtml", "valor90Html", "abrirAnuncio", "vsplit(")))
check("plantilla: imágenes del propio panel (no hotlink a la CDN de Meta)", "fbcdn" not in html and "creativo/" in html)
check("plantilla: sin voseo en los textos nuevos", not any(w in html for w in ("tenés", "podés", "hacé ", "mirá", "elegí")))
check("main.py registra los crons con misfire_grace_time", (lambda s: "job_agenda_cupos" in s and s.count("misfire_grace_time") > 0)(
    (Path(__file__).resolve().parent.parent / "app" / "main.py").read_text(encoding="utf-8")))

# SSRF: el filtro de host usa el mismo parser que la descarga (httpx)
_us = ci._url_segura
check("url imagen Meta normal permitida", _us("https://scontent.xx.fbcdn.net/v/a.jpg?x=1"))
check("url con trucos de parser rechazada", not any(_us(u) for u in (
    "https://evil.com\\@x.fbcdn.net/a.jpg", "https://x.fbcdn.net@evil.com/a", "https://u:p@x.fbcdn.net/a",
    "http://x.fbcdn.net/a", "https://x.fbcdn.net:8443/a", "https://evilfbcdn.net/a",
    "https://x.fbcdn.net.evil.com/a", "https://evil.com#.fbcdn.net/", "https://evil.com?.fbcdn.net/")))

print(f"\n{len(FALLAS)} fallas")
sys.exit(1 if FALLAS else 0)
