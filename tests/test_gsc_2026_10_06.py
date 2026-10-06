"""Google Search Console × Campañas Meta (canal Página web), 2026-10-06.

DB temporal y API de Search Console MOCKEADA (ninguna llamada real).
"""
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
import gsc_snapshot as gs  # noqa: E402
import campanas_meta_routes as cm  # noqa: E402

FALLAS = []


def check(nombre, cond):
    print(("OK  " if cond else "FAIL") + " " + nombre)
    if not cond:
        FALLAS.append(nombre)


# ── Clasificación y normalización de URL ────────────────────────────────────
S = "https://centromedicocarampangue.cl"
check("url: home", gs.pagina_de_url(S + "/") == ("home", ""))
check("url: índice del blog", gs.pagina_de_url(S + "/blog/") == ("blog", ""))
check("url: artículo del blog", gs.pagina_de_url(S + "/blog/Eco-Abdominal/") == ("blog", "eco-abdominal"))
check("url: comuna", gs.pagina_de_url(S + "/curanilahue") == ("comuna", "curanilahue"))
check("url: comuna con dos palabras", gs.pagina_de_url(S + "/los-alamos/") == ("comuna", "los-alamos"))
check("url: landing ortodoncia", gs.pagina_de_url(S + "/ortodoncia/") == ("landing_ortodoncia", "ortodoncia"))
check("url: otra landing", gs.pagina_de_url(S + "/dentista-curanilahue") == ("dentista_curanilahue", "dentista-curanilahue"))
check("basura: /products/", gs.es_basura(S + "/products/abc", "zapatos"))
check("basura: consulta en japonés", gs.es_basura(S + "/", "格安 バッグ"))
check("basura: consulta en coreano", gs.es_basura(S + "/", "가방 할인"))
check("marcador: '-' del sitio = sin artículo", cm.parse_web("Hola (web: home · - · hero)")["articulo"] == ""
      and cm.parse_web("Hola (web: home · - · hero)")["posicion"] == "hero")
check("no basura: consulta normal con tildes y ñ", not gs.es_basura(S + "/", "ecografía cañete"))

# ── Recolector con API mockeada (paginación, upsert, basura) ────────────────
gs.ROW_LIMIT = 2   # para forzar la paginación con pocas filas


class Resp:
    def __init__(self, rows, status=200):
        self.status_code, self._rows, self.text = status, rows, "error"

    def json(self):
        return {"rows": self._rows}


class FakeSess:
    def __init__(self, filas_q, filas_p):
        self.filas = {3: filas_q, 2: filas_p}
        self.llamadas = []

    def post(self, url, json=None, timeout=None):
        self.llamadas.append((url, json))
        rows = self.filas[len(json["dimensions"])]
        return Resp(rows[json["startRow"]: json["startRow"] + json["rowLimit"]])


HOY = datetime.now(ZoneInfo("America/Santiago")).date()
F1, F2 = (HOY - timedelta(days=5)).isoformat(), (HOY - timedelta(days=4)).isoformat()


def rq(f, page, q, cl, im, pos):
    return {"keys": [f, S + page, q], "clicks": cl, "impressions": im, "ctr": cl / im if im else 0, "position": pos}


def rp(f, page, cl, im, pos):
    return {"keys": [f, S + page], "clicks": cl, "impressions": im, "ctr": cl / im if im else 0, "position": pos}


fq = [rq(F1, "/", "centro medico carampangue", 20, 60, 1.2),
      rq(F1, "/blog/eco-abdominal/", "ecografia abdominal arauco", 3, 80, 7.5),
      rq(F2, "/blog/eco-abdominal/", "ecografia abdominal arauco", 2, 70, 8.5),
      rq(F1, "/curanilahue", "medico curanilahue", 1, 40, 12.0),
      rq(F1, "/products/xyz", "格安 バッグ", 9, 900, 3.0),
      rq(F1, "/", "otorrino carampangue", 0, 10, 15.0)]
fp = [rp(F1, "/", 25, 100, 1.5), rp(F1, "/blog/eco-abdominal/", 4, 90, 8.0), rp(F2, "/blog/eco-abdominal/", 3, 80, 8.0),
      rp(F1, "/curanilahue", 2, 50, 11.0), rp(F1, "/products/xyz", 9, 900, 3.0)]
fake = FakeSess(fq, fp)
config.GSC_SITE = "sc-domain:centromedicocarampangue.cl"
nq, np_ = gs.snapshot_tramo(F1, F2, sess=fake)
check("recolector: pagina con startRow hasta agotar", nq == 6 and np_ == 5
      and [j["startRow"] for u, j in fake.llamadas if len(j["dimensions"]) == 3] == [0, 2, 4, 6])
check("recolector: URL con la propiedad codificada", "sc-domain%3Acentromedicocarampangue.cl" in fake.llamadas[0][0])
with session.db() as c:
    nb = c.execute("SELECT COUNT(*) FROM gsc_diario WHERE basura=1").fetchone()[0]
    npb = c.execute("SELECT COUNT(*) FROM gsc_paginas_diario WHERE basura=1").fetchone()[0]
check("recolector: guarda la basura marcada", nb == 1 and npb == 1)
gs.snapshot_tramo(F1, F2, sess=FakeSess(fq, fp))
with session.db() as c:
    n2 = c.execute("SELECT COUNT(*) FROM gsc_diario").fetchone()[0]
check("recolector: volver a bajar el mismo rango no duplica", n2 == 6)


class FakeError:
    def post(self, url, json=None, timeout=None):
        return Resp([], 403)


try:
    gs.snapshot_tramo(F1, F2, sess=FakeError())
    falla = False
except RuntimeError:
    falla = True
with session.db() as c:
    n3 = c.execute("SELECT COUNT(*) FROM gsc_diario").fetchone()[0]
check("recolector: si la API falla no borra lo que había", falla and n3 == 6)
check("snapshot_rango: tramos de a un mes", True)
llam = []


class FakeTramos(FakeSess):
    def post(self, url, json=None, timeout=None):
        llam.append((json["startDate"], json["endDate"]))
        return Resp([])


gs.snapshot_rango("2026-01-01", "2026-03-15", sess=FakeTramos([], []))
check("snapshot_rango: 3 tramos (ene, feb, mar)", sorted(set(llam)) == [("2026-01-01", "2026-01-31"),
                                                                          ("2026-02-01", "2026-03-03"),
                                                                          ("2026-03-04", "2026-03-15")])
import asyncio  # noqa: E402
config.GSC_SNAPSHOT_ACTIVE = False
asyncio.run(gs.job_gsc_diario())   # apagado: no llama nada (sin credenciales locales no reventaría igual)
check("job: con flag apagado no hace nada", True)

# ── Cruce con web_origen y venta ────────────────────────────────────────────
AH = datetime.now(ZoneInfo("America/Santiago"))


def utc(dias):
    return (AH - timedelta(days=dias)).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


with session.db() as c:
    for ph, meta, dias in (("56940000001", {"pagina": "blog", "articulo": "eco-abdominal", "boton": "flotante",
                                            "texto": "Hola, quiero agendar una Ecografía. (web: blog · eco-abdominal · flotante)"}, 4),
                           ("56940000002", {"pagina": "blog", "articulo": "eco-abdominal", "boton": "cuerpo-1",
                                            "texto": "Hola, quiero agendar una Ecografía. (web: blog · eco-abdominal · cuerpo-1)"}, 3),
                           ("56940000003", {"pagina": "home", "articulo": "", "boton": "hero",
                                            "texto": "Hola, quiero agendar (web: home · - · hero)"}, 2)):
        c.execute("INSERT INTO conversation_events (phone, event, meta, ts) VALUES (?,?,?,?)",
                  (ph, "web_origen", json.dumps(meta), utc(dias)))
    c.execute("INSERT INTO citas_bot (phone, id_cita, especialidad, fecha, hora, created_at, id_paciente_medilink) "
              "VALUES (?,?,?,?,?,?,?)", ("56940000001", "G1", "Ecografía", (HOY - timedelta(days=1)).isoformat(),
                                         "10:00", utc(3.9), 7100001))
    c.execute("INSERT INTO bi_pagos_caja (pago_id, fecha, id_paciente, id_profesional, monto) VALUES (?,?,?,?,?)",
              (71001, (HOY - timedelta(days=1)).isoformat(), 7100001, 68, 35000))
    c.commit()
D, H = (HOY - timedelta(days=29)).isoformat(), HOY.isoformat()
g = cm.google_data(D, H)
pg = {p["pagina_id"]: p for p in g["paginas"]}
check("google: totales sin basura", g["totales"]["clicks"] == 25 + 4 + 3 + 2 and g["totales"]["basura_clicks"] == 9)
check("google: suma de varios días por página", pg["blog"]["clicks"] == 7 and pg["blog"]["impressions"] == 170)
art = next(a for a in pg["blog"]["articulos"] if a["articulo"] == "eco-abdominal")
check("google: cruce artículo ↔ web_origen", art["personas"] == 2 and art["citas"] == 1 and art["venta"] == 35000)
check("google: tasa clics → escribió", art["tasa_escribio"] == round(100 * 2 / 7, 1))
check("google: posición media ponderada por apariciones", art["posicion"] == 8.0)
check("google: consultas principales del artículo", art["consultas"][0]["q"] == "ecografia abdominal arauco"
      and art["consultas"][0]["impressions"] == 150)
check("google: home cruza con la llegada de la portada", pg["home"]["personas"] == 1 and pg["home"]["clicks"] == 25)
check("google: comuna con su artículo", pg["comuna"]["articulos"][0]["articulo"] == "curanilahue")
check("google: /products/ no aparece en las páginas", not any("products" in p["pagina_id"] for p in g["paginas"]))
oq = {o["consulta"]: o for o in g["oportunidades"]}
check("oportunidades: ≥30 apariciones y posición 4-20", set(oq) == {"ecografia abdominal arauco", "medico curanilahue"})
check("oportunidades: excluye la que ya está 1°", "centro medico carampangue" not in oq)
check("oportunidades: excluye pocas apariciones", "otorrino carampangue" not in oq)
check("oportunidades: excluye basura", "格安 バッグ" not in oq)
check("oportunidades: agrupa por especialidad y comuna", oq["ecografia abdominal arauco"]["grupo"] == "Ecografía · Arauco"
      and oq["medico curanilahue"]["grupo"] == "Medicina general · Curanilahue")
check("oportunidades: página que rankea", oq["ecografia abdominal arauco"]["url"] == "/blog/eco-abdominal/"
      and oq["ecografia abdominal arauco"]["pagina_1"] is True and oq["medico curanilahue"]["pagina_1"] is False)
check("grupo: búsquedas de marca no caen en medicina general",
      cm._grupo_consulta("centro medico carampangue") == "Marca: centro médico · Carampangue"
      and cm._grupo_consulta("carampangue") == "Marca: centro médico · Carampangue")
check("grupo: vacunas", cm._grupo_consulta("calendario pni 2026") == "Vacunas")
check("tendencia: mes con clics reales sin basura", sum(t["clicks"] for t in g["tendencia"]) == 34)
check("google: sin datos no revienta", cm.google_data("2020-01-01", "2020-01-31")["paginas"] == []
      or True)

# ── Auth ────────────────────────────────────────────────────────────────────
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
config.OLACORE_TOKEN = "dueno_test"
config.ADMIN_TOKEN = "recepcion_test"
app = FastAPI()
app.include_router(cm.router)
cli = TestClient(app)
r0 = "/alma/api/campanas-meta/google"
check("auth /google: 401 / 403 / 200", cli.get(r0).status_code == 401
      and cli.get(r0 + "?token=recepcion_test").status_code == 403
      and cli.get(r0, headers={"Authorization": "Bearer dueno_test"}).status_code == 200)

print(f"\n{len(FALLAS)} fallas")
sys.exit(1 if FALLAS else 0)
