"""Campañas Meta v3 (2026-10-06): rapidez de recepción, alerta de takeover sin
respuesta, cohortes de valor, sugerencia de presupuesto y "A quién llamar hoy".

DB temporal con datos SINTÉTICOS. Ningún envío real: el canal se mockea.
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

session.DB_PATH = Path(tempfile.mkdtemp()) / "t.db"
with session.db() as _c:
    session._run_ddl_inline(_c)
    _c.commit()

import config  # noqa: E402
import campanas_meta_routes as cm  # noqa: E402
import meta_alertas as ma  # noqa: E402
import recepcion_tiempos as rt  # noqa: E402

FALLAS = []


def check(nombre, cond):
    print(("OK  " if cond else "FAIL") + " " + nombre)
    if not cond:
        FALLAS.append(nombre)


CL = ZoneInfo("America/Santiago")


def ep_cl(y, m, d, hh=0, mm=0):
    return int(datetime(y, m, d, hh, mm, tzinfo=CL).timestamp())


# ── 1. recepcion_tiempos (puro) ─────────────────────────────────────────────
# 2026-10-05 es lunes.
check("hábil: lunes 10:00 a 10:20 = 20 min", rt.minutos_habiles(ep_cl(2026, 10, 5, 10), ep_cl(2026, 10, 5, 10, 20)) == 20)
check("hábil: mensaje de madrugada cuenta desde las 08:00",
      rt.minutos_habiles(ep_cl(2026, 10, 5, 3), ep_cl(2026, 10, 5, 8, 10)) == 10)
check("hábil: viernes 20:50 → lunes 08:05 = 10 + sábado 300 + 5",
      rt.minutos_habiles(ep_cl(2026, 10, 9, 20, 50), ep_cl(2026, 10, 12, 8, 5)) == 10 + 300 + 5)
check("hábil: domingo no cuenta", rt.minutos_habiles(ep_cl(2026, 10, 11, 10), ep_cl(2026, 10, 11, 18)) == 0)
check("en horario: sábado 13:59 sí, 14:00 no",
      rt.en_horario(ep_cl(2026, 10, 10, 13, 59)) and not rt.en_horario(ep_cl(2026, 10, 10, 14)))
check("tramos", [rt.tramo(m) for m in (5, 15, 59, 60, 239, 240)] == ["lt15", "15_60", "15_60", "1_4h", "1_4h", "gt4h"])
t0 = ep_cl(2026, 10, 5, 10)
M = lambda dt, d, txt="hola", st="IDLE": {"ts": t0 + dt * 60, "dir": d, "texto": txt, "state": st}
r = rt.respuesta_humana([M(0, "in"), M(1, "out", "bot"), M(2, "in", "quiero hablar", "HUMAN_TAKEOVER"),
                         M(27, "out", "[Recepcionista] Hola, le ayudo", "HUMAN_TAKEOVER")], desde=t0)
check("respuesta: desde el paso a recepción hasta la 1ª humana = 25 min", r["necesito"] and r["minutos"] == 25
      and r["tramo"] == "15_60")
r = rt.respuesta_humana([M(0, "in"), M(1, "out", "bot")], desde=t0)
check("respuesta: el bot la atendió entero → no necesitó", r["necesito"] is False and r["tramo"] is None)
r = rt.respuesta_humana([M(0, "in"), M(3, "out", "[Recepcionista tomó la conversación]", "HUMAN_TAKEOVER"),
                         M(4, "in", "?", "HUMAN_TAKEOVER")], desde=t0, ahora=t0 + 3600)
check("respuesta: aviso de sistema no cuenta como humana → sin respuesta",
      r["necesito"] and not r["respondida"] and r["tramo"] == "sin_respuesta")
r = rt.respuesta_humana([M(0, "in"), M(8, "out", "[Recepcionista] Hola")], desde=t0)
check("respuesta: recepción escribió primero sin derivación → proactiva, fuera de la curva",
      r["proactiva"] and not r["necesito"] and r["tramo"] is None)
r = rt.respuesta_humana([M(0, "in"), M(8, "out", "[Recepcionista] Hola 👋 ¿En qué le ayudo?", "HUMAN_TAKEOVER"),
                         M(9, "in", "una hora", "HUMAN_TAKEOVER"), M(30, "out", "[Recepcionista] listo", "HUMAN_TAKEOVER")], desde=t0)
check("respuesta: saludo de recepción en HUMAN_TAKEOVER no es una espera de 0 min (caso prod)", r["proactiva"] and r["minutos"] is None)
r = rt.respuesta_humana([M(0, "in"), M(1, "out", "Le aviso a recepción", "HUMAN_TAKEOVER"),
                         M(2, "in", "ok", "HUMAN_TAKEOVER"), M(41, "out", "[Recepcionista] Hola", "HUMAN_TAKEOVER")], desde=t0)
check("respuesta: desde el aviso del bot al derivar (40 min)", r["necesito"] and r["minutos"] == 40 and r["tramo"] == "15_60")
cv = rt.curva([{"tramo": "lt15", "agendo": True}, {"tramo": "lt15", "agendo": False}, {"tramo": "sin_respuesta", "agendo": False}])
check("curva: % que agendó por tramo", cv[0]["personas"] == 2 and cv[0]["pct"] == 50 and cv[-1]["pct"] == 0
      and cv[1]["pct"] is None)
e = rt.espera_actual([M(0, "in", st="HUMAN_TAKEOVER"), M(5, "out", "[Recepcionista] Hola"),
                      M(30, "in", "y?", "HUMAN_TAKEOVER"), M(31, "in", "??", "HUMAN_TAKEOVER")], t0 + 60 * 50)
check("espera actual: episodio abierto tras la última respuesta humana", e and e["inicio"] == t0 + 1800 and e["minutos"] == 20)
check("espera actual: nadie esperando", rt.espera_actual([M(0, "in", st="HUMAN_TAKEOVER"),
                                                          M(5, "out", "[Recepcionista] Hola")], t0 + 3600) is None)
check("mediana", rt.mediana([1, 9, 30]) == 9 and rt.mediana([]) is None)

# ── Datos sintéticos (relativos a hoy) ──────────────────────────────────────
AHORA = datetime.now(CL).replace(hour=12, minute=0, second=0, microsecond=0)
HOY = AHORA.date()


def ep(dias):
    return int((AHORA - timedelta(days=dias)).timestamp())


def utc(dias, mins=0):
    return (AHORA - timedelta(days=dias) + timedelta(minutes=mins)).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def fecha(d):
    return (HOY + timedelta(days=d)).isoformat()


with session.db() as c:
    cm._ensure_insights(c)
    for i in range(28):
        f = fecha(-i - 1)
        for ad, camp, cname, spend, imp, reach in (("AD1", "C1", "Buena", 5000, 1000, 800),
                                                   ("AD2", "C2", "Mala", 5000, 1000, 800),
                                                   ("AD3", "C3", "Sin citas", 3000, 900, 700)):
            c.execute("INSERT INTO meta_insights_diario (fecha, ad_id, desglose, valor, ad_name, campaign_id, "
                      "campaign_name, spend, impressions, reach, frequency, clicks, conversaciones) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (f, ad, "total", "-", f"Anuncio {ad}", camp, cname, spend, imp, reach, 1.2, 5, 2))

    def ref(ph, ad, dias):
        c.execute("INSERT INTO meta_referrals (phone, source_id, headline, ts, plataforma) VALUES (?,?,?,?,?)",
                  (ph, ad, "h", ep(dias), "facebook"))

    def msg(ph, d, txt, dias, mins, st="IDLE"):
        c.execute("INSERT INTO messages (phone, direction, text, state, ts) VALUES (?,?,?,?,?)",
                  (ph, d, txt, st, utc(dias, mins)))

    def cita(ph, idc, pid, dias, ad="AD1", f=-2):
        c.execute("INSERT INTO citas_bot (phone, id_cita, especialidad, profesional, fecha, hora, created_at, "
                  "ad_source_id, ad_plataforma, id_paciente_medilink) VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (ph, idc, "Medicina General", "Dr", fecha(f), "10:00", utc(dias, 30), ad, "facebook", pid))

    # R1: pasó a recepción y le respondieron rápido, agendó (AD1)
    ref("56950000001", "AD1", 5)
    msg("56950000001", "in", "hola", 5, 0)
    msg("56950000001", "in", "con una persona", 5, 1, "HUMAN_TAKEOVER")
    msg("56950000001", "out", "[Recepcionista] Hola", 5, 6, "HUMAN_TAKEOVER")
    cita("56950000001", "8001", 9900001, 5)
    # R2: pasó a recepción, nadie respondió, no agendó (AD1)
    ref("56950000002", "AD1", 4)
    msg("56950000002", "in", "hola", 4, 0, "HUMAN_TAKEOVER")
    # R3: solo bot, agendó (AD1)
    ref("56950000003", "AD1", 3)
    msg("56950000003", "in", "hola", 3, 0)
    cita("56950000003", "8003", 9900003, 3)
    # Venta para AD1 (C1 se paga sola) y casi nada para AD2
    for pago, pid, monto, f in ((99001, 9900001, 200000, -2), (99002, 9900003, 120000, -2), (99003, 9900001, 40000, -1)):
        c.execute("INSERT INTO bi_pagos_caja (pago_id, fecha, id_paciente, id_profesional, monto) VALUES (?,?,?,?,?)",
                  (pago, fecha(f), pid, 1, monto))
    for i in range(6):   # AD2: 6 citas, poca venta → bajar
        ph = f"5695100000{i}"
        ref(ph, "AD2", 10)
        cita(ph, f"81{i}", 9910000 + i, 10, ad="AD2")
    c.execute("INSERT INTO bi_pagos_caja (pago_id, fecha, id_paciente, id_profesional, monto) VALUES (?,?,?,?,?)",
              (99100, fecha(-3), 9910000, 1, 15000))
    for i in range(5):   # AD1: más citas para que no sea muestra chica
        ph = f"5695200000{i}"
        ref(ph, "AD1", 12)
        cita(ph, f"82{i}", 9920000 + i, 12)
        c.execute("INSERT INTO bi_pagos_caja (pago_id, fecha, id_paciente, id_profesional, monto) VALUES (?,?,?,?,?)",
                  (99200 + i, fecha(-6), 9920000 + i, 1, 30000))
    c.commit()

# ── 1b. Rapidez en el panel ─────────────────────────────────────────────────
D30, H = fecha(-29), fecha(0)
p = cm.panel_data(D30, H)
rp = p["rapidez"]
check("panel: rapidez necesitaron 2 (R1, R2), solo bot aparte", rp["necesitaron"] == 2 and rp["sin_respuesta"] == 1)
ad1 = next(a for a in p["anuncios"] if a["ad_id"] == "AD1")
check("panel: mediana por anuncio", ad1["resp_necesitaron"] == 2 and ad1["resp_sin"] == 1
      and ad1["resp_mediana_min"] is not None)
cv = {x["tramo"]: x for x in rp["curva"]}
check("panel: curva — el que no recibió respuesta no agendó", cv["sin_respuesta"]["personas"] == 1
      and cv["sin_respuesta"]["agendaron"] == 0)
check("panel: solo bot que agendó", rp["solo_bot_agendaron"] >= 1)
c1 = next(x for x in p["campanas"] if x["campaign_id"] == "C1")
check("panel: curva por campaña", sum(x["personas"] for x in c1["resp_curva"]) == 2)

# ── 2. Alerta de recepción ──────────────────────────────────────────────────
config.RECEPCION_ALERTA_ACTIVE = True
config.RECEPCION_ALERTA_MIN = 15
lun10 = datetime.combine(HOY, datetime.min.time(), CL)
while lun10.weekday() != 0:
    lun10 -= timedelta(days=1)
ahora_h = int((lun10 + timedelta(hours=10, minutes=30)).timestamp())   # lunes 10:30
with session.db() as c:
    def m_abs(ph, d, txt, epoch, st):
        c.execute("INSERT INTO messages (phone, direction, text, state, ts) VALUES (?,?,?,?,?)",
                  (ph, d, txt, st, datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")))
    for ph, minutos in (("56960000001", 40), ("56960000002", 5), ("56960000003", 40)):
        c.execute("INSERT INTO meta_referrals (phone, source_id, headline, ts, plataforma) VALUES (?,?,?,?,?)",
                  (ph, "AD1", "h", ahora_h - 86400, "facebook"))
        c.execute("INSERT OR REPLACE INTO sessions (phone, state, data, updated_at) VALUES (?,?,?,?)",
                  (ph, "HUMAN_TAKEOVER", "{}", "2026-01-01 00:00:00"))
        m_abs(ph, "in", "necesito ayuda", ahora_h - minutos * 60, "HUMAN_TAKEOVER")
    m_abs("56960000003", "out", "[Recepcionista] Ya le respondo", ahora_h - 60, "HUMAN_TAKEOVER")
    # sin anuncio ni web: nunca alerta
    c.execute("INSERT OR REPLACE INTO sessions (phone, state, data, updated_at) VALUES (?,?,?,?)",
              ("56960000009", "HUMAN_TAKEOVER", "{}", "2026-01-01 00:00:00"))
    m_abs("56960000009", "in", "hola", ahora_h - 3600, "HUMAN_TAKEOVER")
    c.commit()
al = ma.evaluar_recepcion_sin_respuesta(ahora_h)
claves = [k for k, _ in al]
check("alerta: solo el de anuncio que espera > 15 min", len(al) == 1 and ":960000001:" in claves[0])
check("alerta: el texto no trae el teléfono completo", "56960000001" not in al[0][1] and "•" in al[0][1])
check("alerta: fuera de horario no avisa", ma.evaluar_recepcion_sin_respuesta(ahora_h - 4 * 3600) == [])
check("alerta: umbral configurable", len(ma.evaluar_recepcion_sin_respuesta(ahora_h, umbral_min=60)) == 0)

ENV = []


async def _fake_envio(texto):
    ENV.append(texto)
    return "telegram"


ma.enviar_al_dueno = _fake_envio
_dt_real = ma.datetime


class _DT(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime.fromtimestamp(ahora_h, tz or CL)


ma.datetime = _DT
asyncio.run(ma.job_recepcion_sin_respuesta())
asyncio.run(ma.job_recepcion_sin_respuesta())
check("job: avisa una vez y no repite por persona", len(ENV) == 1 and "esperan más de 15 min" in ENV[0])
config.RECEPCION_ALERTA_ACTIVE = False
asyncio.run(ma.job_recepcion_sin_respuesta())
check("job: flag apagado no hace nada", len(ENV) == 1)
config.RECEPCION_ALERTA_ACTIVE = True
ma.datetime = _dt_real

# ── 3. Cohortes ─────────────────────────────────────────────────────────────
co = cm.cohortes_data("canal", "meta", hoy=HOY)
check("cohortes: hay filas por mes de llegada", len(co["cohortes"]) >= 1 and co["horizontes"] == [30, 90, 180, 365])
fila = co["cohortes"][-1]
check("cohortes: personas y pacientes (todas las cohortes)", sum(x["personas"] for x in co["cohortes"]) >= 14
      and sum(x["pacientes"] for x in co["cohortes"]) >= 7)
check("cohortes: venta a 30 días incluye pagos tras el contacto", fila["horizontes"][0]["venta"] > 0)
check("cohortes: acumulado no decrece", all(fila["horizontes"][i]["venta"] <= fila["horizontes"][i + 1]["venta"]
                                           for i in range(3)))
check("cohortes: volvieron (≥2 días de pago)", sum(x["volvieron"] for x in co["cohortes"]) >= 1)
check("cohortes: horizontes recientes marcados incompletos", fila["horizontes"][-1]["completo"] is False)
check("cohortes: estado de recuperación del gasto", fila["estado"] in ("pagado", "aun_no", "no_se_pago"))
cc = cm.cohortes_data("campana", "meta", hoy=HOY)
check("cohortes por campaña", {x["grupo_id"] for x in cc["cohortes"]} >= {"C1", "C2"})
check("cohortes: gasto de la campaña en ese mes", all(x["gasto"] >= 0 for x in cc["cohortes"]))
check("cohortes vacías sin error", cm.cohortes_data("canal", "web", hoy=HOY)["cohortes"] == [])

# ── 4. Sugerencia de presupuesto ────────────────────────────────────────────
R = ma.recomendar
check("sugerir: pausar sin citas con gasto", R({"gasto": 84000, "citas": 0, "retorno_centro": None})["accion"] == "pausar")
check("sugerir: subir si se paga solo", R({"gasto": 140000, "citas": 9, "retorno_centro": 1.6, "frecuencia": 1.5})["accion"] == "subir")
check("sugerir: monto aprox. +20% semanal", R({"gasto": 140000, "citas": 9, "retorno_centro": 1.6, "frecuencia": 1.5})["monto_semana"] == 7000)
check("sugerir: saturado → mantener y renovar", "creativo" in R({"gasto": 140000, "citas": 9, "retorno_centro": 1.6,
                                                                 "frecuencia": 5})["razon"])
check("sugerir: muestra chica no baja", R({"gasto": 140000, "citas": 2, "retorno_centro": 0.2})["accion"] == "mantener"
      and R({"gasto": 140000, "citas": 2, "retorno_centro": 0.2})["muestra_chica"])
check("sugerir: bajar fuerte si no se paga", R({"gasto": 140000, "citas": 8, "retorno_centro": 0.3})["accion"] == "bajar"
      and R({"gasto": 140000, "citas": 8, "retorno_centro": 0.3})["monto_semana"] == round(35000 * .3 / 1000) * 1000)
check("sugerir: mantener cerca del equilibrio", R({"gasto": 140000, "citas": 8, "retorno_centro": 1.0})["accion"] == "mantener")
check("sugerir: sin gasto → nada", R({"gasto": 0, "citas": 0}) is None)
_estados = cm._estados_meta
cm._estados_meta = lambda: {"AD1": "ACTIVE", "AD2": "ACTIVE", "AD3": "ACTIVE"}
sg = ma.sugerencias_presupuesto(HOY)
acc = {x["campaign_id"]: x["accion"] for x in sg["campanas"]}
check("sugerencias: una por campaña activa", set(acc) == {"C1", "C2", "C3"})
check("sugerencias: C3 pausar, C2 bajar", acc["C3"] == "pausar" and acc["C2"] == "bajar")
check("sugerencias: orden pausar → bajar → subir/mantener", sg["campanas"][0]["accion"] == "pausar")
lin = ma._lineas_presupuesto(sg)
check("resumen del lunes: bloque de presupuesto", lin and lin[0].startswith("*Sugerencia de presupuesto*")
      and any("Pausar" in l for l in lin))
txt = ma.construir_resumen(HOY)
check("resumen del lunes incluye la sugerencia", "Sugerencia de presupuesto" in txt)
cm._estados_meta = _estados

# ── 5. A quién llamar hoy ───────────────────────────────────────────────────
import ausentismo  # noqa: E402
ausentismo.ensure_ausentismo_table()
with session.db() as c:
    # L1 no asistió hace 2 días
    c.execute("INSERT INTO meta_referrals (phone, source_id, headline, ts, plataforma) VALUES (?,?,?,?,?)",
              ("56970000001", "AD1", "h", ep(6), "facebook"))
    c.execute("INSERT INTO citas_bot (phone, id_cita, especialidad, fecha, hora, created_at, ad_source_id, "
              "id_paciente_medilink) VALUES (?,?,?,?,?,?,?,?)",
              ("56970000001", "9001", "Medicina General", fecha(-2), "10:00", utc(5.9), "AD1", 9970001))
    c.execute("INSERT INTO ausentismo_citas (id_cita, id_profesional, id_paciente, fecha, hora, id_estado, "
              "estado_cita, anulacion) VALUES (?,?,?,?,?,?,?,?)", (9001, 1, 9970001, fecha(-2), "10:00", 8, "", 0))
    # L2 vio horas hace 1 día
    c.execute("INSERT INTO meta_referrals (phone, source_id, headline, ts, plataforma) VALUES (?,?,?,?,?)",
              ("56970000002", "AD1", "h", ep(1.2), "facebook"))
    c.execute("INSERT INTO conversation_events (phone, event, meta, ts) VALUES (?,?,?,?)",
              ("56970000002", "funnel_slot_ofrecido", json.dumps({"esp": "kine"}), utc(1)))
    c.execute("INSERT INTO messages (phone, direction, text, ts) VALUES (?,?,?,?)", ("56970000002", "in", "ok", utc(1)))
    # L3 seguimiento vencido
    c.execute("INSERT INTO meta_referrals (phone, source_id, headline, ts, plataforma) VALUES (?,?,?,?,?)",
              ("56970000003", "AD1", "h", ep(2), "facebook"))
    c.execute("INSERT INTO messages (phone, direction, text, ts) VALUES (?,?,?,?)", ("56970000003", "in", "hola", utc(2)))
    # L4 vio horas pero ya cerrado en la gestión
    c.execute("INSERT INTO meta_referrals (phone, source_id, headline, ts, plataforma) VALUES (?,?,?,?,?)",
              ("56970000004", "AD1", "h", ep(1.1), "facebook"))
    c.execute("INSERT INTO conversation_events (phone, event, meta, ts) VALUES (?,?,?,?)",
              ("56970000004", "funnel_slot_ofrecido", json.dumps({"esp": "kine"}), utc(1)))
    c.execute("INSERT INTO messages (phone, direction, text, ts) VALUES (?,?,?,?)", ("56970000004", "in", "ok", utc(1)))
    c.commit()
cm.guardar_seguimiento("970000003", "volver_llamar", "", fecha(-1), ahora=AHORA)
cm.guardar_seguimiento("970000004", "no_interesa", "", None, ahora=AHORA)
lh = cm.llamar_hoy_data(D30, H, ahora=AHORA)
orden = [x["clave"] for x in lh["personas"]]
check("llamar hoy: no-show primero, vio horas, luego seguimiento",
      orden.index("970000001") < orden.index("970000002") < orden.index("970000003"))
check("llamar hoy: gestión cerrada no aparece", "970000004" not in orden)
x1 = next(x for x in lh["personas"] if x["clave"] == "970000001")
check("llamar hoy: motivo legible", x1["motivo"] == "no_asistio" and x1["motivos"][0].startswith("No asistió"))
check("llamar hoy: conteo por prioridad", lh["por_prioridad"][1] >= 1 and lh["por_prioridad"][2] >= 1)

# ── Auth de los endpoints nuevos ────────────────────────────────────────────
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
config.OLACORE_TOKEN = "dueno_test"
config.ADMIN_TOKEN = "recepcion_test"
app = FastAPI()
app.include_router(cm.router)
cli = TestClient(app)
cm._estados_meta = lambda: {}
for ruta in ("/alma/api/campanas-meta/cohortes", "/alma/api/campanas-meta/llamar-hoy",
             "/alma/api/campanas-meta/sugerencias-presupuesto"):
    check(f"auth {ruta}: 401 / 403 / 200",
          cli.get(ruta).status_code == 401 and cli.get(ruta + "?token=recepcion_test").status_code == 403
          and cli.get(ruta, headers={"Authorization": "Bearer dueno_test"}).status_code == 200)

print(f"\n{len(FALLAS)} fallas")
sys.exit(1 if FALLAS else 0)
