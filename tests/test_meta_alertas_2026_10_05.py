"""Avisos de Campañas Meta al dueño (app/meta_alertas.py): resumen semanal y
alertas diarias. DB temporal con datos SINTÉTICOS y envío MOCKEADO (no sale
ningún mensaje real ni se llama a Meta/Telegram).
"""
import asyncio
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
import meta_alertas as ma  # noqa: E402
import alertas_oob  # noqa: E402

cm._estados_meta = lambda: {}   # sin red: el estado sale de "tuvo gasto reciente"

FALLAS = []


def check(nombre, cond):
    print(("OK  " if cond else "FAIL") + " " + nombre)
    if not cond:
        FALLAS.append(nombre)


CL = ZoneInfo("America/Santiago")
AHORA = datetime.now(CL).replace(hour=12, minute=0, second=0, microsecond=0)
HOY = AHORA.date()


def fecha(d):
    return (HOY + timedelta(days=d)).isoformat()


def utc(dias_atras):
    return (AHORA - timedelta(days=dias_atras)).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def ep(dias_atras):
    return int((AHORA - timedelta(days=dias_atras)).timestamp())


# ── Datos: 21 días de foto. AD1 rinde (gasta poco, trae citas); AD3 gasta 6.000
# diarios con frecuencia 7 y 0 citas (alerta a y c). ──────────────────────────
with session.db() as c:
    cm._ensure_insights(c)
    for i in range(1, 22):
        for ad, camp, cname, spend, imp, reach in (
                ("AD1", "C1", "Campaña Uno", 3000, 800, 600),
                ("AD3", "C2", "Campaña Dos", 6000, 7000, 1000)):
            c.execute("INSERT INTO meta_insights_diario (fecha, ad_id, desglose, valor, ad_name, "
                      "campaign_id, campaign_name, spend, impressions, reach, frequency, clicks, "
                      "conversaciones) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (fecha(-i), ad, "total", "-", f"Anuncio {ad}", camp, cname, spend, imp, reach,
                       imp / reach, 10, 2))
    for phone, ad, idc, dias in (("56911110001", "AD1", "A1", 2), ("56911110002", "AD1", "A2", 3)):
        c.execute("INSERT INTO meta_referrals (phone, source_id, headline, ts, plataforma) "
                  "VALUES (?,?,?,?,?)", (phone, ad, f"Titular {ad}", ep(dias + .1), "facebook"))
        c.execute("INSERT INTO citas_bot (phone, id_cita, especialidad, profesional, fecha, hora, "
                  "modalidad, created_at, paciente_nombre, ad_source_id, ad_headline, ad_plataforma) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                  (phone, idc, "Medicina General", "Dr Sintético", fecha(4), "10:30", "presencial",
                   utc(dias), "Nombre Secreto", ad, f"Titular {ad}", "facebook"))
    # seguimiento: hoy, vencido, vencido pero cerrado, futuro
    for clave, estado, prox in (("911110001", "volver_llamar", fecha(0)),
                                ("911110002", "volver_llamar", fecha(-2)),
                                ("911110003", "no_interesa", fecha(-3)),
                                ("911110004", "volver_llamar", fecha(3))):
        c.execute("INSERT INTO campanas_seguimiento (clave, estado, nota, proximo, updated_at) "
                  "VALUES (?,?,?,?,?)", (clave, estado, "", prox, utc(1)))
    c.commit()

# ── Formato ─────────────────────────────────────────────────────────────────
check("formato: miles con punto", ma._clp(1234567) == "$1.234.567")
check("formato: negativo", ma._clp(-12000) == "-$12.000")
check("formato: signo en resultado", ma._signed(5000) == "+$5.000" and ma._signed(-5000) == "-$5.000")
check("formato: retorno con coma", ma._x(2.14) == "2,1x" and ma._x(None) == "—")

# ── Resumen semanal ─────────────────────────────────────────────────────────
txt = ma.construir_resumen(HOY)
pd = cm.panel_data(fecha(-7), fecha(-1), canal="meta")
check("resumen: gasto semanal = panel_data (no recalcula)", ma._clp(pd["kpis"]["gasto"]) in txt)
check("resumen: gasto 7 días = 7 x 9.000", pd["kpis"]["gasto"] == 63000 and "$63.000" in txt)
check("resumen: citas del panel", f"Citas {pd['kpis']['citas']} (ant." in txt and pd["kpis"]["citas"] == 2)
check("resumen: semana anterior presente", "(ant. $63.000)" in txt)
check("resumen: por campaña", "*Campaña Uno*" in txt and "*Campaña Dos*" in txt)
check("resumen: resultado y retornos", "Resultado (centro − gasto)" in txt and "retorno centro" in txt)
check("resumen: mejor y peor anuncio",
      "*Mejor anuncio* · Anuncio AD1" in txt and "*Peor anuncio* · Anuncio AD3" in txt)
check("resumen: canal Página web", "*Página web*" in txt)
check("resumen: link al panel", txt.rstrip().endswith(ma.PANEL_URL))
check("resumen: sin datos de pacientes", "Nombre Secreto" not in txt and "5691111" not in txt)
check("resumen: sin voseo", not any(w in txt.lower() for w in (" tenés", " querés", " podés", "vos ")))
check("resumen: corto para WhatsApp", len(txt) < 3000)

# ── Alertas ─────────────────────────────────────────────────────────────────
al = dict(ma.evaluar_alertas(HOY))
check("alerta a: AD3 gastó >= $50.000 en 14 días sin citas", "sin_citas:AD3" in al)
check("alerta a: AD1 (con citas) no alerta", "sin_citas:AD1" not in al)
check("alerta a: texto con monto", "$84.000" in al["sin_citas:AD3"])
check("alerta c: AD3 activo con frecuencia 7 > 6", "saturado:AD3" in al)
check("alerta c: AD1 frecuencia 1,3 no alerta", "saturado:AD1" not in al)
check("alerta b: ayer hubo gasto, no alerta", "gasto_cero" not in al)
check("alerta d: 1 hoy + 1 vencida, ignora cerrada y futura",
      "por_llamar" in al and "2 persona(s)" in al["por_llamar"]
      and "1 vencida(s)" in al["por_llamar"] and "1 para hoy" in al["por_llamar"])
check("alertas: sin datos de pacientes", all("Nombre Secreto" not in t and "9111100" not in t
                                             for t in al.values()))

# (b): hoy desplazado 3 días → "ayer" sin filas y la semana previa con gasto
al_b = dict(ma.evaluar_alertas(HOY + timedelta(days=3)))
check("alerta b: gasto $0 ayer con semana previa gastando", "gasto_cero" in al_b)
check("alerta b: avisa que falta la foto de ayer", "respaldo diario" in al_b["gasto_cero"])
# con fila de ayer en 0 explícito (Meta devolvió 0): alerta sin la nota de foto
with session.db() as c:
    c.execute("INSERT INTO meta_insights_diario (fecha, ad_id, desglose, valor, ad_name, campaign_id, "
              "campaign_name, spend, impressions, reach, frequency, clicks, conversaciones) "
              "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (fecha(2), "AD1", "total", "-", "Anuncio AD1", "C1", "Campaña Uno", 0, 0, 0, 0, 0, 0))
    c.commit()
al_b2 = dict(ma.evaluar_alertas(HOY + timedelta(days=3)))
check("alerta b: gasto 0 explícito también alerta, sin nota de foto",
      "gasto_cero" in al_b2 and "respaldo diario" not in al_b2["gasto_cero"])
with session.db() as c:
    c.execute("DELETE FROM meta_insights_diario WHERE fecha = ?", (fecha(2),))
    c.commit()
# cuenta que nunca gastó: no alerta
with session.db() as c:
    c.execute("DELETE FROM meta_insights_diario WHERE fecha >= ?", (fecha(-8),))
    c.commit()
check("alerta b: sin gasto la semana previa no alerta",
      "gasto_cero" not in dict(ma.evaluar_alertas(HOY)))
with session.db() as c:   # restaurar datos
    for i in range(1, 8):
        for ad, camp, cname, spend, imp, reach in (("AD1", "C1", "Campaña Uno", 3000, 800, 600),
                                                   ("AD3", "C2", "Campaña Dos", 6000, 7000, 1000)):
            c.execute("INSERT INTO meta_insights_diario (fecha, ad_id, desglose, valor, ad_name, "
                      "campaign_id, campaign_name, spend, impressions, reach, frequency, clicks, "
                      "conversaciones) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (fecha(-i), ad, "total", "-", f"Anuncio {ad}", camp, cname, spend, imp, reach,
                       imp / reach, 10, 2))
    c.commit()

# ── Dedup 48 h ──────────────────────────────────────────────────────────────
t0 = 1_800_000_000
v = [("k1", "a"), ("k2", "b")]
check("dedup: nada enviado → todo pasa", ma.filtrar_recientes(v, t0) == v)
ma.marcar_enviadas(["k1"], t0)
check("dedup: a las 24 h no repite", ma.filtrar_recientes(v, t0 + 24 * 3600) == [("k2", "b")])
check("dedup: a las 46 h no repite", ma.filtrar_recientes(v, t0 + 46 * 3600) == [("k2", "b")])
check("dedup: pasadas las 47 h vuelve (cron diario día por medio)",
      ma.filtrar_recientes(v, t0 + 48 * 3600) == v)

# ── Jobs con envío mockeado ─────────────────────────────────────────────────
ENVIADOS = []
TG_OK = {"v": True}


async def fake_tg(texto, header=None):
    if TG_OK["v"]:
        ENVIADOS.append(("telegram", texto))
    return TG_OK["v"]


alertas_oob.enviar_telegram = fake_tg
WA = []


async def fake_wa(to, body):
    WA.append((to, body))
    return "wamid.x"


import messaging  # noqa: E402
import jobs  # noqa: E402

messaging.send_whatsapp = fake_wa


def limpiar_estado():
    with session.db() as c:
        c.execute("DELETE FROM meta_alertas_estado")
        c.commit()


limpiar_estado()
asyncio.run(ma.job_meta_alertas_diario())
check("job diario: manda 1 mensaje por Telegram", len(ENVIADOS) == 1 and ENVIADOS[0][0] == "telegram")
m1 = ENVIADOS[0][1]
check("job diario: contiene alertas a, c, d y el panel",
      "no trajo ninguna cita" in m1 and "frecuencia" in m1 and "volver a llamar" in m1
      and ma.PANEL_URL in m1)
asyncio.run(ma.job_meta_alertas_diario())
check("job diario: segunda corrida el mismo día no repite", len(ENVIADOS) == 1)

# flag apagado
limpiar_estado()
config.META_ALERTAS_ACTIVE = False
n = len(ENVIADOS)
asyncio.run(ma.job_meta_alertas_diario())
asyncio.run(ma.job_meta_resumen_semanal())
check("flag apagado: ni alertas ni resumen", len(ENVIADOS) == n)
config.META_ALERTAS_ACTIVE = True

# resumen semanal
asyncio.run(ma.job_meta_resumen_semanal())
check("job semanal: manda el resumen", len(ENVIADOS) == n + 1 and "Campañas Meta · semana" in ENVIADOS[-1][1])

# Telegram falla + ventana WhatsApp cerrada: no se pierde en silencio ni se marca enviada
limpiar_estado()
TG_OK["v"] = False
config.ADMIN_ALERT_PHONE = "56900000000"
jobs._admin_window_open = lambda *a, **k: False
n = len(ENVIADOS)
asyncio.run(ma.job_meta_alertas_diario())
check("sin canal: no envía nada", len(ENVIADOS) == n and not WA)
with session.db() as c:
    ev = c.execute("SELECT COUNT(*) FROM conversation_events WHERE event='meta_alertas_sin_canal'").fetchone()[0]
    marcadas = c.execute("SELECT COUNT(*) FROM meta_alertas_estado").fetchone()[0]
check("sin canal: deja evento para auditar", ev == 1)
check("sin canal: no marca como enviadas (reintenta mañana)", marcadas == 0)

# Telegram falla + ventana abierta → WhatsApp
jobs._admin_window_open = lambda *a, **k: True
asyncio.run(ma.job_meta_alertas_diario())
check("respaldo: WhatsApp al dueño con ventana abierta", len(WA) == 1 and WA[0][0] == "56900000000")
with session.db() as c:
    marcadas = c.execute("SELECT COUNT(*) FROM meta_alertas_estado").fetchone()[0]
check("respaldo: marca enviadas", marcadas >= 3)

# Registro en el scheduler (guardrail misfire_grace_time + zona horaria)
src = (Path(__file__).resolve().parent.parent / "app" / "main.py").read_text()
for job_id, extra in (("meta_resumen_semanal", 'day_of_week="mon", hour=8, minute=30'),
                      ("meta_alertas_diario", "hour=9, minute=5")):
    i = src.index(f'id="{job_id}"')
    bloque = src[i - 250:i + 200]
    check(f"main.py: {job_id} con CronTrigger CLT y misfire_grace_time",
          extra in bloque and "timezone=_CLT" in bloque and "misfire_grace_time=" in bloque)

print(f"\n{len(FALLAS)} fallas")
sys.exit(1 if FALLAS else 0)
