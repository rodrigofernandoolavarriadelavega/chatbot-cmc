# -*- coding: utf-8 -*-
"""Demo LOCAL de Alma Radar con datos SINTÉTICOS (nada de producción).

    venv/bin/python scripts/radar_demo_local.py            # http://127.0.0.1:8811/alma/radar?token=demo_radar
    venv/bin/python scripts/radar_demo_local.py --vacio    # misma página sin datos (estados vacíos)

Crea una base temporal con personas, citas, caja, anuncios, Google, agenda y
bitácora inventados; levanta solo los routers de Radar y Campañas Meta (para
las imágenes de los anuncios). No toca data/, no llama a Medilink ni a Meta.
"""
from __future__ import annotations

import io
import json
import os
import random
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "app"))

CL = ZoneInfo("America/Santiago")
TOKEN = "demo_radar"
_T0 = datetime.now(CL)
_DESFASE = timedelta(0)   # --hora HH:MM corre el reloj de la demo a esa hora de hoy


def reloj() -> datetime:
    return datetime.now(CL) + _DESFASE


def _utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


NOMBRES = ["María Rivas", "José Castro", "Ana Soto", "Luis Pérez", "Carla Vega", "Rosa Muñoz", "Elena Fuentes",
           "Pedro Alarcón", "Gloria Sáez", "Nicolás Tapia", "Valentina Hormazábal", "Daniel Rojas", "Francisca Mella",
           "Ignacio Cid", "Sofía Lagos", "Bernardo Ortiz", "Katherine Zúñiga", "Patricio Araya", "Camila Neira",
           "Héctor Vidal", "Paula Inostroza", "Tomás Bravo", "Javiera Lillo", "Marcela Riquelme", "Cristian Ulloa"]
ESPS = [("Medicina General", 1), ("Kinesiología", 77), ("Odontología General", 55), ("Ortodoncia", 66),
        ("Otorrinolaringología", 23), ("Ecografía", 68), ("Medicina General", 73)]


def sembrar(tmp: Path, vacio: bool = False) -> None:
    import session
    session.DB_PATH = tmp / "sessions.db"
    with session.db() as c:
        session._run_ddl_inline(c)
        c.commit()
    import campanas_meta_routes as cm
    import campanas_meta_integraciones as ci
    import capi_purchase as cp
    import gsc_snapshot as gs
    import ausentismo
    import persistencia
    ausentismo.ensure_ausentismo_table()
    cp.ensure_table()
    with session.db() as c:
        cm._ensure_insights(c)
        ci._ensure_cupos(c)
        ci._ensure_creativos(c)
        gs.ensure_tables(c)
        persistencia._ensure_table(c)
        c.commit()
    import equipo_routes
    import pagos_routes
    import finanzas_routes
    equipo_routes.ensure_table()
    pagos_routes.ensure_pagos_table()
    finanzas_routes.ensure_table()   # egresos_cmc (gastos del módulo EBITDA)
    if vacio:
        return

    rnd = random.Random(7)
    ahora = reloj()
    hoy = ahora.date()

    def ult(h, m):   # la última vez que el reloj pasó por h:m (hoy o ayer)
        t = ahora.replace(hour=h, minute=m, second=0, microsecond=0)
        return t if t <= ahora else t - timedelta(days=1)
    with session.db() as c:
        for pid, nom, pct in ((1, "Dr. Rodrigo Olavarría", 0), (73, "Dr. Andrés Abarca", 0), (77, "Luis Armijo", 50),
                              (55, "Dra. Javiera Burgos", 45), (66, "Dra. Daniela Castillo", 50), (23, "Dr. Manuel Borrego", 70),
                              (68, "David Pardo", 65)):
            c.execute("INSERT INTO equipo_cmc (id_medilink, nombre, pct_honorario) VALUES (?,?,?)", (pid, nom, pct or 70))
        # ── Meta: 4 anuncios, 75 días ───────────────────────────────────────
        ads = [("AD_OD", "Odontología · limpieza y evaluación", "C1", "Odontología octubre", 6500),
               ("AD_MG", "Medicina General hoy en Carampangue", "C2", "Medicina general", 5200),
               ("AD_KI", "Kinesiología con bono Fonasa", "C3", "Kinesiología", 3800),
               ("AD_OR", "Ortodoncia evaluación brackets", "C4", "Ortodoncia", 4200)]
        for i in range(1, 76):
            f = (hoy - timedelta(days=i)).isoformat()
            for ad, nom, cid, cn, sp in ads:
                fat = 1 + (0.9 if ad == "AD_OR" and i <= 7 else 0)
                if ad == "AD_MG":   # el gasto de Medicina General varía por semana (el Laboratorio ajusta la curva)
                    sp = 5200 * (0.4 + 0.35 * (((i - 1) // 7) % 4))
                imp = int(sp / 4 * (1 + rnd.random() * .3))
                c.execute("INSERT INTO meta_insights_diario (fecha, ad_id, desglose, valor, ad_name, campaign_id, campaign_name, "
                          "spend, impressions, reach, frequency, clicks, conversaciones, actualizado_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (f, ad, "total", "-", nom, cid, cn, sp * (1 + rnd.random() * .2), imp, int(imp / (1.6 * fat)), 1.5,
                           int(imp * (0.012 if ad == "AD_OR" and i <= 7 else 0.022)), rnd.randint(2, 6),
                           int((ult(6, 20) - timedelta(days=i - 1)).timestamp())))
            for ad, nom, cid, cn, sp in ads:   # desglose por plataforma de publicación (70% Facebook, 30% Instagram)
                for plat, fr in (("facebook", .7), ("instagram", .3)):
                    c.execute("INSERT INTO meta_insights_diario (fecha, ad_id, desglose, valor, ad_name, campaign_id, campaign_name, "
                              "spend, impressions, reach, frequency, clicks, conversaciones, actualizado_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                              (f, ad, "plataforma", plat, nom, cid, cn, sp * fr, int(sp / 4 * fr), int(sp / 6 * fr), 1.5, 10,
                               rnd.randint(0, 3), int((ult(6, 20) - timedelta(days=i - 1)).timestamp())))
        # Creativos con imagen generada
        from PIL import Image, ImageDraw
        cdir = tmp / "creativos"
        cdir.mkdir(exist_ok=True)
        colores = {"AD_OD": (17, 114, 171), "AD_MG": (15, 63, 104), "AD_KI": (79, 190, 206), "AD_OR": (11, 115, 131)}
        for ad, nom, *_ in ads:
            im = Image.new("RGB", (480, 300), colores[ad])
            d = ImageDraw.Draw(im)
            for k in range(6):
                d.ellipse((300 + k * 8, -60 + k * 6, 520 - k * 8, 160 - k * 6), outline=(255, 255, 255), width=2)
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=80)
            (cdir / f"{ad}.jpg").write_bytes(buf.getvalue())
            c.execute("INSERT INTO meta_creativos (ad_id, creative_id, titulo, texto, cta, tipo, archivo, imagen_ts, actualizado_ts, error) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (ad, "cr" + ad, nom.split(" · ")[0], f"{nom}. Escríbanos por WhatsApp y elija su hora.", "WHATSAPP_MESSAGE",
                       "imagen", f"{ad}.jpg", int(ahora.timestamp()), int(ult(6, 50).timestamp()), ""))
        # ── Personas de anuncios y web (30 días) + citas + caja ─────────────
        pago_id = 1
        for n in range(170):
            dias = rnd.randint(1, 29)
            t = (ahora - timedelta(days=dias)).replace(hour=rnd.randint(8, 21), minute=rnd.randint(0, 59))
            ph = f"5696{n:07d}"
            ad = rnd.choice([a[0] for a in ads] + ["web"])
            if ad == "web":
                c.execute("INSERT INTO conversation_events (phone, event, meta, ts) VALUES (?,?,?,?)",
                          (ph, "web_origen", json.dumps({"pagina": rnd.choice(["home", "blog", "comuna"]), "texto": "Hola, quiero agendar una hora (web: home)"}), _utc(t)))
            else:
                c.execute("INSERT INTO meta_referrals (phone, source_type, source_id, headline, body, ts, plataforma) VALUES (?,?,?,?,?,?,?)",
                          (ph, "ad", ad, "Centro Médico Carampangue", dict((a[0], a[1]) for a in ads)[ad], int(t.timestamp()),
                           rnd.choice(["facebook", "instagram", ""])))
            c.execute("INSERT INTO messages (phone, direction, text, state, ts) VALUES (?,?,?,?,?)", (ph, "in", "Hola", "IDLE", _utc(t)))
            if rnd.random() < .3:
                c.execute("INSERT INTO messages (phone, direction, text, state, ts) VALUES (?,?,?,?,?)",
                          (ph, "out", "Le aviso a recepción", "HUMAN_TAKEOVER", _utc(t + timedelta(minutes=2))))
                c.execute("INSERT INTO messages (phone, direction, text, state, ts) VALUES (?,?,?,?,?)",
                          (ph, "out", "[Recepcionista] Hola, ¿en qué le ayudo?", "HUMAN_TAKEOVER",
                           _utc(t + timedelta(minutes=2 + rnd.choice([1, 3, 8, 20, 45, 160])))))
            c.execute("INSERT OR REPLACE INTO contact_profiles (phone, nombre, comuna) VALUES (?,?,?)",
                      (ph, rnd.choice(NOMBRES), rnd.choice(["Arauco", "Curanilahue", "Los Álamos", "Carampangue", "Lebu", "", ""])))
            if rnd.random() < .45:
                esp, prof = rnd.choice(ESPS)
                pid = 900000 + n
                cid = str(70000 + n)
                fcita = (t + timedelta(days=rnd.randint(1, 4))).date()
                c.execute("INSERT INTO citas_bot (phone, id_cita, especialidad, profesional, fecha, hora, created_at, ad_source_id, "
                          "ad_plataforma, id_paciente_medilink, paciente_nombre) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                          (ph, cid, esp, "Prof", fcita.isoformat(), "10:00", _utc(t + timedelta(minutes=6)),
                           None if ad == "web" else ad, None, pid, ""))
                if fcita <= hoy:
                    est = rnd.choices([2, 8, 1], [80, 8, 12])[0]
                    c.execute("INSERT INTO ausentismo_citas (id_cita, id_profesional, id_paciente, fecha, hora, id_estado, estado_cita, "
                              "anulacion, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                              (int(cid), prof, pid, fcita.isoformat(), "10:00", est,
                               {2: "Atendido", 8: "No asiste", 1: "Anulado"}[est], 1 if est == 1 else 0, ult(4, 50).isoformat()))
                    if est == 2:
                        for k in range(rnd.choice([1, 1, 2, 3])):
                            if fcita + timedelta(days=k * 9) > hoy:
                                break
                            c.execute("INSERT INTO bi_pagos_caja (pago_id, fecha, id_profesional, id_paciente, monto, metodo_pago, synced_at) "
                                      "VALUES (?,?,?,?,?,?,?)", (pago_id, (fcita + timedelta(days=k * 9)).isoformat(), prof, pid,
                                                                 rnd.choice([12000, 15130, 20980, 35000, 11390]), "Transferencia",
                                                                 _utc(ult(0, 10))))
                            pago_id += 1
        # Caja general: 7 meses
        d0 = (hoy.replace(day=1) - timedelta(days=190)).replace(day=1)
        f = d0
        while f <= hoy:
            if f.weekday() < 6:
                for _ in range(rnd.randint(55, 75)):
                    esp, prof = rnd.choice(ESPS)
                    c.execute("INSERT INTO bi_pagos_caja (pago_id, fecha, id_profesional, id_paciente, monto, metodo_pago, synced_at) "
                              "VALUES (?,?,?,?,?,?,?)", (pago_id, f.isoformat(), prof, rnd.randint(1000, 9000),
                                                         rnd.choice([12000, 15130, 20980, 11390, 35000, 25000]),
                                                         rnd.choices(["Efectivo", "Transferencia", "Débito", "Crédito", "Bono Fonasa"], [22, 30, 20, 8, 20])[0],
                                                         _utc(ult(0, 10))))
                    pago_id += 1
            f += timedelta(days=1)
        # ── Hoy: entradas por canal, estados y recepción esperando ──────────
        hoy_ini = ahora.replace(hour=8, minute=2, second=0)
        span = max(30, int((ahora - hoy_ini).total_seconds() / 60)) if ahora > hoy_ini else 30
        for n in range(23):
            t = hoy_ini + timedelta(minutes=int(span * (n + rnd.random()) / 23))
            if t > ahora:
                t = ahora - timedelta(minutes=rnd.randint(1, 20))
            ph = f"56977{n:06d}" if n % 9 else f"fb_{n:010d}"
            c.execute("INSERT INTO messages (phone, direction, text, state, ts) VALUES (?,?,?,?,?)", (ph, "in", "Hola", "IDLE", _utc(t)))
            c.execute("INSERT OR REPLACE INTO contact_profiles (phone, nombre) VALUES (?,?)", (ph, NOMBRES[n]))
            if n % 3 == 0:
                c.execute("INSERT INTO meta_referrals (phone, source_id, headline, ts, plataforma) VALUES (?,?,?,?,?)",
                          (ph, rnd.choice(ads)[0], "h", int(t.timestamp()), rnd.choice(["facebook", "instagram"])))
            elif n % 7 == 0:
                c.execute("INSERT INTO conversation_events (phone, event, meta, ts) VALUES (?,?,?,?)",
                          (ph, "web_origen", json.dumps({"pagina": "home"}), _utc(t)))
            esp, prof = ESPS[n % len(ESPS)]
            if n % 2 == 0:
                c.execute("INSERT INTO citas_bot (phone, id_cita, especialidad, fecha, hora, created_at, ad_source_id, ad_plataforma) "
                          "VALUES (?,?,?,?,?,?,?,?)", (ph, str(99000 + n), esp, (hoy + timedelta(days=1)).isoformat(), "16:30",
                                                     _utc(t + timedelta(minutes=4)), "AD_OD" if n % 3 == 0 else None,
                                                     "instagram" if n % 3 == 0 else None))
                st = "IDLE"
            elif n % 5 == 1:
                st = "HUMAN_TAKEOVER"
                c.execute("INSERT INTO messages (phone, direction, text, state, ts) VALUES (?,?,?,?,?)",
                          (ph, "in", "¿Tienen hora?", "HUMAN_TAKEOVER", _utc(t + timedelta(minutes=1))))
            else:
                st = rnd.choice(["WAIT_SLOT", "WAIT_ESPECIALIDAD", "IDLE"])
            c.execute("INSERT OR REPLACE INTO sessions (phone, state, data, updated_at) VALUES (?,?,?,?)",
                      (ph, st, json.dumps({"especialidad": esp.lower()}), _utc(t + timedelta(minutes=1))))
        # Ausentismo: historia de 90 días y citas de mañana (algunas sin confirmar)
        for n in range(400):
            fch = (hoy - timedelta(days=rnd.randint(1, 89))).isoformat()
            est = rnd.choices([2, 8], [92, 8])[0]
            c.execute("INSERT OR IGNORE INTO ausentismo_citas (id_cita, id_profesional, id_paciente, fecha, hora, id_estado, estado_cita, "
                      "anulacion, updated_at) VALUES (?,?,?,?,?,?,?,?,?)", (300000 + n, rnd.choice(ESPS)[1], 300000 + n, fch, "10:00", est,
                                                                       "Atendido" if est == 2 else "No asiste", 0, ult(4, 50).isoformat()))
        for n in range(31):
            st = rnd.choices([(7, "No confirmado"), (3, "Confirmado por teléfono")], [55, 45])[0]
            c.execute("INSERT OR IGNORE INTO ausentismo_citas (id_cita, id_profesional, id_paciente, fecha, hora, id_estado, estado_cita, "
                      "anulacion, updated_at) VALUES (?,?,?,?,?,?,?,?,?)", (310000 + n, rnd.choice(ESPS)[1], 310000 + n,
                                                                       (hoy + timedelta(days=1)).isoformat(), "11:00", st[0], st[1], 0,
                                                                       ult(4, 50).isoformat()))
        # «¿Cómo nos conociste?» (tags referido:*) de personas que no llegaron por anuncio
        for n, tag in enumerate(["amigo"] * 9 + ["google"] * 4 + ["facebook_instagram"] * 3 + ["calle", "radio"]):
            ph = f"56944{n:06d}"
            t = ahora - timedelta(days=rnd.randint(0, 20), hours=rnd.randint(0, 8))
            c.execute("INSERT INTO contact_tags (phone, tag, ts) VALUES (?,?,?)", (ph, "referido:" + tag, _utc(t)))
            if rnd.random() < .7:
                pid = 950000 + n
                c.execute("INSERT INTO citas_bot (phone, id_cita, especialidad, created_at, id_paciente_medilink) VALUES (?,?,?,?,?)",
                          (ph, str(95000 + n), "Medicina General", _utc(t), pid))
                if t.date() < hoy:
                    c.execute("INSERT INTO bi_pagos_caja (pago_id, fecha, id_profesional, id_paciente, monto, metodo_pago, synced_at) "
                              "VALUES (?,?,?,?,?,?,?)", (pago_id, (t.date() + timedelta(days=1)).isoformat(), 1, pid, 15130,
                                                         "Efectivo", _utc(ult(0, 10))))
                    pago_id += 1
        # Gastos registrados en el módulo EBITDA (sin arriendo: el puente lo advierte)
        mes0 = (hoy.replace(day=1) - timedelta(days=150)).replace(day=1).isoformat()
        for cat, monto in (("Sueldos y leyes sociales", 3099908), ("Insumos clínicos", 400000), ("Software/sistemas", 250000),
                           ("Contador", 95000), ("Servicios básicos (luz/agua/internet)", 40000)):
            c.execute("INSERT INTO egresos_cmc (fecha, categoria, descripcion, monto, recurrente) VALUES (?,?,?,?,1)",
                      (mes0, cat, "Demo", monto))
        # Referencia: 4 semanas atrás, mismo día
        for k in range(1, 5):
            dia = ahora - timedelta(days=7 * k)
            for n in range(rnd.randint(28, 40)):
                t = dia.replace(hour=8, minute=0) + timedelta(minutes=int(rnd.betavariate(2, 2.4) * 780))
                c.execute("INSERT INTO messages (phone, direction, text, state, ts) VALUES (?,?,?,?,?)",
                          (f"5698{k}{n:05d}", "in", "Hola", "IDLE", _utc(t)))
        # ── Agenda (cache de cupos) ─────────────────────────────────────────
        for prof in (1, 73, 77, 55, 66, 23, 68):
            for i in range(14):
                fch = (hoy + timedelta(days=i)).isoformat()
                lib = {68: 0, 66: rnd.randint(0, 3), 77: rnd.randint(2, 7)}.get(prof, rnd.randint(0, 5))
                c.execute("INSERT INTO agenda_cupos_cache (id_profesional, fecha, libres, primera_hora, actualizado_ts) VALUES (?,?,?,?,?)",
                          (prof, fch, lib, "09:00" if lib else None, int(ult(5, 34).timestamp())))
        ci._set_estado(c, "ultima_corrida", {"ts": int(ult(5, 48).timestamp()), "consultas": 96,
                                             "errores": 0, "sin_horario": 2, "profesionales": 7, "corte": None})
        # ── Google ───────────────────────────────────────────────────────────
        for i in range(2, 32):
            fch = (hoy - timedelta(days=i)).isoformat()
            for url, base in (("https://centromedicocarampangue.cl/", 14), ("https://centromedicocarampangue.cl/blog/dolor-de-oido", 4),
                              ("https://centromedicocarampangue.cl/arauco", 3)):
                im = base * 30 + rnd.randint(0, 40)
                c.execute("INSERT INTO gsc_paginas_diario (fecha, page, clicks, impressions, ctr, position, basura, actualizado_ts) "
                          "VALUES (?,?,?,?,?,?,?,?)", (fch, url, base + rnd.randint(0, 4), im, 0.03, 6.5, 0,
                                                     int(ult(6, 40).timestamp())))
                c.execute("INSERT INTO gsc_diario (fecha, page, query, clicks, impressions, ctr, position, basura, actualizado_ts) "
                          "VALUES (?,?,?,?,?,?,?,?,?)", (fch, url, "centro medico carampangue" if base > 10 else "otorrino arauco",
                                                       base, im, 0.03, 4.4 if base > 10 else 9.5, 0, 0))
        # ── Bitácora ────────────────────────────────────────────────────────
        c.execute("INSERT INTO capi_purchase_corridas (fecha_corrida, enviados, diferidos, no_atendidos, duplicados, errores, value_total, estimados) "
                  "VALUES (?,?,?,?,?,?,?,?)", (ult(7, 7).isoformat(), 14, 1, 2, 0, 0, 186400, 2))
        c.execute("INSERT INTO conversation_events (phone, event, meta, ts) VALUES (?,?,?,?)",
                  ("centinela", "centinela_diario", json.dumps({"hallazgos": 0}), _utc(ult(7, 30))))
        c.execute("INSERT INTO bi_sync_log (tipo, id_profesional, fecha, inicio, fin, n_registros, n_errores, ok) VALUES (?,?,?,?,?,?,?,?)",
                  ("pagos", 0, "x", "", ult(0, 9).astimezone(timezone.utc).replace(tzinfo=None).isoformat(), 412, 0, 1))
        for n in range(60):
            t = ahora - timedelta(days=rnd.randint(0, 29), hours=rnd.randint(0, 10))
            c.execute("INSERT INTO conversation_events (phone, event, meta, ts) VALUES (?,?,?,?)",
                      (f"5695{n:07d}", "marketing_consent_respuesta",
                       json.dumps({"status": rnd.choices(["accepted", "declined"], [8, 2])[0], "via": "flujo"}), _utc(t)))
        for st, n in (("accepted", 4120), ("declined", 96), ("pending", 310)):
            for k in range(n // 40):
                c.execute("INSERT OR IGNORE INTO privacy_consents (phone, status) VALUES (?,?)", (f"{st}{k}", st))
        # Reenganche (E-01) y persistencia (E-02)
        for n in range(320):
            t = ahora - timedelta(days=rnd.randint(1, 80), hours=rnd.randint(0, 12))
            ph = f"56933{n:06d}"
            con = n % 2 == 0
            c.execute("INSERT INTO conversation_events (phone, event, meta, ts) VALUES (?,?,?,?)",
                      (ph, "reenganche_enviado", json.dumps({"variante": "a_punto_elegir_" + ("con_hora" if con else "sin_hora")}), _utc(t)))
            if rnd.random() < (.19 if con else .04):
                c.execute("INSERT INTO citas_bot (phone, id_cita, especialidad, created_at) VALUES (?,?,?,?)",
                          (ph, str(500000 + n), "Medicina General", _utc(t + timedelta(hours=2))))
        for n in range(40):
            t = ahora - timedelta(days=rnd.randint(0, 25), hours=rnd.randint(1, 40))
            c.execute("INSERT INTO consultas_persistencia (funnel_id, phone, especialidad, opened_at, estado) VALUES (?,?,?,?,?)",
                      (f"f{n}", f"56922{n:06d}", "Kinesiología", _utc(t),
                       rnd.choices(["agendada", "expirada", "no_explicito", "abierta", "contactada"], [9, 14, 3, 4, 3])[0]))
        c.commit()
    # Respaldo visible
    bdir = tmp / "backups"
    bdir.mkdir(exist_ok=True)
    for k in range(2):
        p = bdir / f"sessions_2026100{k}_033002.db.gz"
        p.write_bytes(b"x")
        os.utime(p, (ahora.timestamp() - 3600 * (6 + 24 * k),) * 2)
    os.environ["RADAR_BACKUP_DIR"] = str(bdir)
    # Directorio de pacientes (territorio)
    hm = sqlite3.connect(tmp / "heatmap_cache.db")
    hm.execute("CREATE TABLE IF NOT EXISTS pacientes_heatmap (id INTEGER PRIMARY KEY, nombre TEXT, apellidos TEXT, rut TEXT, comuna TEXT, "
               "ciudad TEXT, direccion TEXT, fecha_nacimiento TEXT, sexo TEXT, celular TEXT, email TEXT)")
    hm.commit()
    hm.close()


def app_demo():
    from fastapi import FastAPI
    from fastapi.staticfiles import StaticFiles
    import config
    config.OLACORE_TOKEN = TOKEN
    import campanas_meta_routes as cm
    import campanas_meta_integraciones  # noqa: F401 — cuelga /creativo/{id} del router
    import radar_routes
    radar_routes.BACKUP_DIR = os.environ.get("RADAR_BACKUP_DIR", radar_routes.BACKUP_DIR)
    radar_routes._ahora = reloj
    app = FastAPI()
    app.include_router(cm.router)
    app.include_router(radar_routes.router)
    app.include_router(radar_routes.pagina)
    app.mount("/static", StaticFiles(directory=str(RAIZ / "static")), name="static")
    return app


if __name__ == "__main__":
    import uvicorn
    if "--hora" in sys.argv:
        hh, mm = sys.argv[sys.argv.index("--hora") + 1].split(":")
        _DESFASE = _T0.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0) - _T0
    tmp = Path(tempfile.mkdtemp(prefix="radar_demo_"))
    sembrar(tmp, vacio="--vacio" in sys.argv)
    puerto = int(os.environ.get("RADAR_DEMO_PORT", "8811"))
    print(f"Alma Radar (demo sintética): http://127.0.0.1:{puerto}/alma/radar?token={TOKEN}  · base {tmp}")
    uvicorn.run(app_demo(), host="127.0.0.1", port=puerto, log_level="warning")
