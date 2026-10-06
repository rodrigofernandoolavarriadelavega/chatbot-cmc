"""Demo local del panel Campañas Meta con valor a 12 meses (datos SINTÉTICOS, DB temporal).

    ~/chatbot-cmc/venv/bin/python scripts/demo_valor12m.py          # http://127.0.0.1:8812/alma/campanas-meta?token=dueno_demo
    ~/chatbot-cmc/venv/bin/python scripts/demo_valor12m.py --port 8813

No toca la DB real ni sale a Meta/Medilink. Pestaña "Valor en el tiempo" = cohortes y "Dónde sigue el recorrido".
"""
import argparse
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import session  # noqa: E402

session.DB_PATH = Path(tempfile.mkdtemp()) / "demo.db"
with session.db() as _c:
    session._run_ddl_inline(_c)
    _c.commit()

import config  # noqa: E402
import campanas_meta_routes as cm  # noqa: E402
import campanas_meta_integraciones  # noqa: E402,F401  (cuelga sus rutas de cm.router)
import medilink as ml  # noqa: E402
from fastapi import FastAPI, HTTPException, Query  # noqa: E402
from fastapi.responses import HTMLResponse  # noqa: E402

config.OLACORE_TOKEN = "dueno_demo"
config.META_ACCESS_TOKEN = ""
cm._estados_meta = lambda: {}
ml.PROFESIONALES = {1: {"nombre": "Dr. Uno", "especialidad": "Medicina General", "intervalo": 15}}
CL = ZoneInfo("America/Santiago")
AHORA = datetime.now(CL).replace(hour=12, minute=0, second=0, microsecond=0)
HOY = AHORA.date()


def fecha(d):
    return (HOY + timedelta(days=d)).isoformat()


def utc(dias, mins=0):
    return (AHORA - timedelta(days=dias) + timedelta(minutes=mins)).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def sembrar():
    with session.db() as c:
        cm._ensure_insights(c)
        c.execute("""CREATE TABLE IF NOT EXISTS equipo_cmc (id INTEGER PRIMARY KEY AUTOINCREMENT, id_medilink INTEGER,
            nombre TEXT NOT NULL, especialidad TEXT DEFAULT '', rol TEXT DEFAULT '', tipo_contrato TEXT DEFAULT 'honorarios',
            pct_honorario INTEGER DEFAULT 0, telefono TEXT DEFAULT '', email TEXT DEFAULT '', estado TEXT DEFAULT 'activo',
            licencia_desde TEXT DEFAULT '', licencia_hasta TEXT DEFAULT '', notas TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now')), updated_at TEXT DEFAULT (datetime('now')))""")
        for idm, nom, esp, pct in ((1, "Dr. Uno", "Medicina General", 70), (2, "Kine Dos", "Kinesiología", 45),
                                    (3, "Dra. Tres", "Odontología General", 50), (4, "Dr. Cuatro", "Ecografía", 70),
                                    (66, "Dra. Orto", "Ortodoncia", 60), (5, "Dr. Cinco", "Otorrinolaringología", 75)):
            c.execute("INSERT INTO equipo_cmc (id_medilink, nombre, especialidad, pct_honorario) VALUES (?,?,?,?)", (idm, nom, esp, pct))
        n = [500000]

        def pago(pid, dias, monto, prof):
            n[0] += 1
            c.execute("INSERT INTO bi_pagos_caja (pago_id, fecha, id_paciente, id_profesional, monto) VALUES (?,?,?,?,?)",
                      (n[0], fecha(-dias), pid, prof, monto))

        # Cohortes con 12 meses cumplidos
        for i in range(120):                       # medicina general
            pid = 1000000 + i
            d0 = 380 + (i % 200)
            pago(pid, d0, 20000, 1)
            if i % 4 == 0:
                pago(pid, d0 - 30, 15000, 1)
            if i % 25 == 0:
                pago(pid, d0 - 40, 45000, 2)
            if i % 40 == 0:
                pago(pid, d0 - 90, 60000, 3)
            if i % 30 == 0:
                pago(pid, d0 - 60, 35000, 4)
        for i in range(40):                        # odontología general → ortodoncia
            pid = 2000000 + i
            d0 = 400 + (i % 150)
            pago(pid, d0, 30000, 3)
            if i % 4 == 0:
                pago(pid, d0 - 20, 120000, 66)
                pago(pid, d0 - 60, 30000, 66)
                pago(pid, d0 - 90, 30000, 66)
        for i in range(34):                        # ecografía
            pid = 3000000 + i
            pago(pid, 380 + i * 3, 50000, 4)
            if i % 7 == 0:
                pago(pid, 330 + i * 3, 20000, 1)
        for i in range(12):                        # kinesiología (muestra chica)
            pid = 4000000 + i
            d0 = 390 + i * 4
            for k in range(6):
                pago(pid, d0 - k * 20, 20000, 2)
        for i in range(6):                         # profesional que falta en equipo_cmc
            pago(5000000 + i, 400 + i, 25000, 77)
        # Anuncios de los últimos 30 días
        ads = (("AD1", "C1", "Campaña medicina general", "Medicina general sin esperas", 380000),
               ("AD2", "C1", "Campaña medicina general", "Consulta médica hoy", 210000),
               ("AD3", "C2", "Campaña dental", "Ortodoncia sonrisa", 300000))
        for ad, camp, cname, nombre, gasto in ads:
            for k in range(1, 31):
                c.execute("INSERT INTO meta_insights_diario (fecha, ad_id, desglose, valor, ad_name, campaign_id, campaign_name, spend, "
                          "impressions, reach, frequency, clicks, conversaciones) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (fecha(-k), ad, "total", "-", nombre, camp, cname, gasto / 30, 1500, 900, 1.6, 30, 4))

        def persona(k, ad, esp, dias_cita, pid, pagos_prev, pagos_nuevos):
            ph = f"5697000{k:04d}"
            c.execute("INSERT INTO meta_referrals (phone, source_id, headline, ts, plataforma) VALUES (?,?,?,?,?)",
                      (ph, ad, "h", int((AHORA - timedelta(days=dias_cita)).timestamp()), "facebook"))
            c.execute("INSERT INTO citas_bot (phone, id_cita, especialidad, profesional, fecha, hora, created_at, ad_source_id, "
                      "ad_plataforma, id_paciente_medilink) VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (ph, f"D{k}", esp, "Dr", fecha(-dias_cita + 2), "10:00", utc(dias_cita, 30), ad, "facebook", pid))
            for dias, monto, prof in pagos_prev + pagos_nuevos:
                pago(pid, dias, monto, prof)
        k = 0
        for i in range(14):                        # AD1: nuevos de medicina general, algunos con kine
            k += 1
            persona(k, "AD1", "Medicina General", 25 - i, 6000000 + k, [], [(23 - i, 20000, 1)] + ([(10, 40000, 2)] if i % 5 == 0 else []))
        for i in range(9):                         # AD1: ya eran pacientes
            k += 1
            persona(k, "AD1", "Medicina General", 22 - i, 6000000 + k, [(220, 15000, 1)], [(19 - i, 30000, 1)])
        for i in range(8):                         # AD2
            k += 1
            persona(k, "AD2", "Medicina General", 20 - i, 6000000 + k, [], [(18 - i, 20000, 1)])
        for i in range(4):
            k += 1
            persona(k, "AD2", "Medicina General", 18 - i, 6000000 + k, [(300, 15000, 1)], [(15 - i, 25000, 1)])
        for i in range(5):                         # AD3: ortodoncia entra por odontología general
            k += 1
            persona(k, "AD3", "Odontología General", 26 - i, 6000000 + k, [], [(24 - i, 30000, 3)] + ([(8, 120000, 66)] if i == 0 else []))
        for i in range(30):                        # nuevos del centro que no vienen de anuncios
            pago(7000000 + i, 1 + i % 28, 20000, 1)
        c.commit()


sembrar()
app = FastAPI()
app.include_router(cm.router)
HTML = (Path(__file__).resolve().parent.parent / "templates" / "alma_campanas_meta.html").read_text(encoding="utf-8")


@app.get("/alma/campanas-meta", response_class=HTMLResponse)
def pagina(token: str | None = Query(None)):
    if not cm.token_dueno(token):
        raise HTTPException(403, "Solo el token del dueño")
    return HTMLResponse(HTML.replace("__TOKEN__", token), headers={"Cache-Control": "no-store"})


if __name__ == "__main__":
    import uvicorn
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8812)
    a = ap.parse_args()
    print(f"Demo en http://127.0.0.1:{a.port}/alma/campanas-meta?token=dueno_demo")
    uvicorn.run(app, host="127.0.0.1", port=a.port, log_level="warning")
