"""Portal app v2 — ronda 4: tomas compartidas, suscripción de calendario,
estado real de horas pasadas, aislamiento de resultados.

Corre:  PYTHONPATH=app ~/chatbot-cmc/venv/bin/python tests/test_portal_r4_2026_10_08.py
Usa una sessions.db temporal (nunca la real). No toca Medilink ni WhatsApp
(Medilink se simula con un _get falso).
"""
import asyncio
import re
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import session as S  # noqa: E402

_TMP = tempfile.mkdtemp()
S.DB_PATH = Path(_TMP) / "sessions.db"
S.init_db() if hasattr(S, "init_db") else None

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import portal_routes as PR  # noqa: E402
import portal_herramientas_routes as PH  # noqa: E402
import medilink as ML  # noqa: E402

app = FastAPI()
app.include_router(PH.router)
app.include_router(PR.router)
c = TestClient(app)

OK = FAIL = 0
TZ = ZoneInfo("America/Santiago")
HOY = datetime.now(TZ).date()


def check(nombre, cond, extra=""):
    global OK, FAIL
    if cond:
        OK += 1
        print("PASS", nombre)
    else:
        FAIL += 1
        print("FAIL", nombre, extra)


def cookies(owner_rut, phone, active=None):
    ck = {PR._COOKIE_NAME: PR._sign_portal_cookie(owner_rut, phone)}
    if active:
        ck[PR._ACTIVE_COOKIE_NAME] = PR._sign_active_cookie(owner_rut, active)
    return ck


def req(method, url, ck=None, **kw):
    c.cookies.clear()
    for k, v in (ck or {}).items():
        c.cookies.set(k, v)
    return c.request(method, url, **kw)


TEL = "56911112222"            # teléfono COMPARTIDO por A y B
TEL_HIJA = "56933334444"
A, B = "11111111-1", "22222222-2"       # A = mamá, B = otra persona con el mismo celular
HIJA = "44444444-4"                     # hija de A, con su propia sesión
with S.db() as cx:   # perfiles: el del celular compartido es de B (¡no de A!)
    cx.execute("INSERT OR REPLACE INTO contact_profiles (phone, rut, nombre) VALUES (?,?,?)", (TEL, B, "BEATRIZ OTRA"))
    cx.execute("INSERT OR REPLACE INTO contact_profiles (phone, rut, nombre) VALUES (?,?,?)", (TEL_HIJA, HIJA, "ANA HIJA"))

# ── 401 sin sesión ─────────────────────────────────────────────────────────
for m, u in [("GET", "/portal/api/herramientas/tomas"), ("POST", "/portal/api/herramientas/tomas"),
             ("GET", "/portal/api/herramientas/calendario"), ("POST", "/portal/api/herramientas/calendario"),
             ("DELETE", "/portal/api/herramientas/calendario")]:
    r = req(m, u, json={})
    check(f"401 sin sesión {m} {u}", r.status_code == 401, r.status_code)

# ── Remedio de A ───────────────────────────────────────────────────────────
r = req("POST", "/portal/api/herramientas/remedios", cookies(A, TEL),
        json={"nombre": "Losartán 50 mg", "dosis": "1 comprimido", "horarios": ["08:00", "20:00"], "dias": []})
rid = r.json()["id"]
hoy = HOY.strftime("%Y-%m-%d")

# ── Tomas: A marca la suya ─────────────────────────────────────────────────
r = req("POST", "/portal/api/herramientas/tomas", cookies(A, TEL), json={"remedio_id": rid, "hora": "08:00"})
check("A marca su toma de las 08:00", r.status_code == 200 and r.json()["toma"]["propio"] is True, r.text)
check("…marcada por 'usted' con hora HH:MM", re.fullmatch(r"\d\d:\d\d", r.json()["toma"]["marcado_hhmm"] or ""), r.json())
with S.db() as cx:
    row = cx.execute("SELECT marcado_por_rut, marcado_por_nombre FROM portal_tomas").fetchone()
check("teléfono compartido: NO guarda el nombre de B como marcador de A",
      row["marcado_por_rut"] == A and row["marcado_por_nombre"] == "", dict(row))
r = req("GET", "/portal/api/herramientas/tomas", cookies(A, TEL))
check("A ve su toma marcada hoy", r.status_code == 200 and [t["hora"] for t in r.json()["tomas"]] == ["08:00"], r.json())

# ── Aislamiento por teléfono compartido ────────────────────────────────────
r = req("GET", "/portal/api/herramientas/tomas", cookies(B, TEL))
check("B (mismo celular) no ve las tomas de A", r.json()["tomas"] == [], r.json())
r = req("POST", "/portal/api/herramientas/tomas", cookies(B, TEL), json={"remedio_id": rid, "hora": "20:00"})
check("B no puede marcar el remedio de A (404)", r.status_code == 404, r.status_code)
r = req("GET", "/portal/api/herramientas/tomas", cookies(B, TEL, active=A))
check("cookie activa hacia A sin vínculo se ignora", r.json()["tomas"] == [])

# ── Validaciones ───────────────────────────────────────────────────────────
r = req("POST", "/portal/api/herramientas/tomas", cookies(A, TEL), json={"remedio_id": rid, "hora": "09:00"})
check("400 hora que el remedio no tiene", r.status_code == 400)
manana = (HOY + timedelta(days=1)).strftime("%Y-%m-%d")
r = req("POST", "/portal/api/herramientas/tomas", cookies(A, TEL), json={"remedio_id": rid, "hora": "20:00", "fecha": manana})
check("400 no se marca el futuro", r.status_code == 400)
r = req("GET", f"/portal/api/herramientas/tomas?fecha={(HOY - timedelta(days=5)).strftime('%Y-%m-%d')}", cookies(A, TEL))
check("400 no se leen fechas viejas", r.status_code == 400)
r = req("POST", "/portal/api/herramientas/tomas", cookies(A, TEL), json={"remedio_id": "x", "hora": "08:00"})
check("400 remedio inválido", r.status_code == 400)

# ── Familia: la hija marca la toma de la mamá ──────────────────────────────
PR.is_family_link = lambda o, d: (o, d) == (HIJA, A)
_orig = PR._acceso_vinculo


async def _no_verificado(o, p, d):
    return {"acceso": PR.ACCESO_SOLO_HORAS, "metodo": "declared", "motivo": "ficha_no_coincide"}


async def _verificado(o, p, d):
    return {"acceso": PR.ACCESO_COMPLETO, "metodo": "otp", "motivo": ""}

PR._acceso_vinculo = _no_verificado
for m, u, body in [("GET", "/portal/api/herramientas/tomas", None),
                   ("POST", "/portal/api/herramientas/tomas", {"remedio_id": rid, "hora": "20:00"}),
                   ("POST", "/portal/api/herramientas/calendario", {}),
                   ("GET", "/portal/api/examenes", None)]:
    r = req(m, u, cookies(HIJA, TEL_HIJA, active=A), json=body)
    check(f"403 familiar NO verificado {m} {u}", r.status_code == 403
          and r.json()["detail"]["error"] == "vinculo_no_verificado", r.status_code)
with S.db() as cx:
    n = cx.execute("SELECT COUNT(*) FROM portal_tomas WHERE hora='20:00'").fetchone()[0]
check("…y no quedó marcada", n == 0)

PR._acceso_vinculo = _verificado
r = req("POST", "/portal/api/herramientas/tomas", cookies(HIJA, TEL_HIJA, active=A), json={"remedio_id": rid, "hora": "20:00"})
check("familiar verificado marca la toma de la mamá", r.status_code == 200 and r.json()["toma"]["propio"] is True, r.text)
r = req("GET", "/portal/api/herramientas/tomas", cookies(A, TEL))
t20 = next((t for t in r.json()["tomas"] if t["hora"] == "20:00"), None)
check("la mamá ve 'marcado por Ana' (mismas marcas en ambos teléfonos)",
      t20 and t20["propio"] is False and t20["marcado_por"] == "Ana", r.json())
r = req("POST", "/portal/api/herramientas/tomas", cookies(A, TEL), json={"remedio_id": rid, "hora": "20:00"})
check("marcar de nuevo respeta la primera marca (quién y cuándo)", r.json()["toma"]["marcado_por"] == "Ana", r.json())
r = req("POST", "/portal/api/herramientas/tomas", cookies(A, TEL), json={"remedio_id": rid, "hora": "20:00", "tomado": False})
check("desmarcar borra la marca", r.status_code == 200 and r.json()["toma"] is None)

# ── Suscripción de calendario ──────────────────────────────────────────────
r = req("POST", "/portal/api/herramientas/calendario", cookies(A, TEL), headers={"host": "agentecmc.cl", "x-forwarded-proto": "https"})
j = r.json()
tok = (j.get("url") or "").rsplit("/", 1)[-1].removesuffix(".ics")
check("crea link https + webcal", j["url"].startswith("https://agentecmc.cl/portal/cal/") and j["webcal"].startswith("webcal://agentecmc.cl/portal/cal/"), j)
check("token largo y aleatorio (≥43 caracteres)", len(tok) >= 43, tok)
check("la URL no contiene el RUT", A.replace("-", "") not in j["url"] and A not in j["url"])
with S.db() as cx:
    stored = cx.execute("SELECT token_hash FROM portal_cal_tokens").fetchall()
check("en la base queda solo el hash del token", all(tok not in x["token_hash"] for x in stored) and len(stored) == 1)
r = req("GET", f"/portal/cal/{tok}.ics")
ics = r.text.replace("\r\n ", "")
check("feed 200 sin cookie", r.status_code == 200 and r.headers["content-type"].startswith("text/calendar"), r.status_code)
check("feed: solo nombre del remedio y hora (sin dosis)", "SUMMARY:Tomar Losartán 50 mg\r\n" in ics and "1 comprimido" not in ics)
check("feed: sin RUT ni nombre de la persona", A not in ics and A.replace("-", "") not in ics and "BEATRIZ" not in ics.upper())
check("feed: SEQUENCE presente", "SEQUENCE:0" in ics)
check("feed: sugiere refresco", "REFRESH-INTERVAL;VALUE=DURATION:PT6H" in ics)
uids_a = re.findall(r"UID:(\S+)", ics)
# editar el remedio → SEQUENCE sube, UID igual
req("POST", "/portal/api/herramientas/remedios", cookies(A, TEL),
    json={"id": rid, "nombre": "Losartán 100 mg", "horarios": ["08:00", "20:00"], "dias": []})
ics2 = req("GET", f"/portal/cal/{tok}.ics").text.replace("\r\n ", "")
check("tras editar: mismo UID y SEQUENCE:1 (el calendario reemplaza, no duplica)",
      re.findall(r"UID:(\S+)", ics2) == uids_a and "SEQUENCE:1" in ics2 and "Losartán 100 mg" in ics2)
r = req("GET", "/portal/api/herramientas/remedios.ics", cookies(A, TEL))
check("el archivo descargable también lleva SEQUENCE y dosis (es privado)", "SEQUENCE:1" in r.text)
r = req("GET", "/portal/cal/token-que-no-existe-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.ics")
check("token inventado → 404", r.status_code == 404)
r = req("GET", "/portal/cal/corto.ics")
check("token mal formado → 404", r.status_code == 404)
r = req("GET", "/portal/api/herramientas/calendario", cookies(A, TEL))
check("estado: activa", r.json()["activa"] is True)
# crear otro revoca el anterior (un link vigente por persona)
tok2 = req("POST", "/portal/api/herramientas/calendario", cookies(A, TEL)).json()["url"].rsplit("/", 1)[-1].removesuffix(".ics")
check("nuevo link revoca el anterior", req("GET", f"/portal/cal/{tok}.ics").status_code == 404
      and req("GET", f"/portal/cal/{tok2}.ics").status_code == 200)
r = req("DELETE", "/portal/api/herramientas/calendario", cookies(B, TEL))
check("B (mismo celular) no puede revocar el link de A", req("GET", f"/portal/cal/{tok2}.ics").status_code == 200)
r = req("DELETE", "/portal/api/herramientas/calendario", cookies(A, TEL))
check("A revoca → el feed deja de responder", r.json()["revocados"] == 1 and req("GET", f"/portal/cal/{tok2}.ics").status_code == 404)
# link creado por la hija: si quita el vínculo, deja de funcionar solo
tok3 = req("POST", "/portal/api/herramientas/calendario", cookies(HIJA, TEL_HIJA, active=A)).json()["url"].rsplit("/", 1)[-1].removesuffix(".ics")
check("link creado por la hija funciona con el vínculo vigente", req("GET", f"/portal/cal/{tok3}.ics").status_code == 200)
PR.is_family_link = lambda o, d: False
check("…y deja de funcionar al quitar el vínculo", req("GET", f"/portal/cal/{tok3}.ics").status_code == 404)
PR._acceso_vinculo = _orig

# ── Borrar un remedio borra sus tomas ──────────────────────────────────────
req("DELETE", f"/portal/api/herramientas/remedios/{rid}", cookies(A, TEL))
with S.db() as cx:
    n = cx.execute("SELECT COUNT(*) FROM portal_tomas WHERE remedio_id=?", (rid,)).fetchone()[0]
check("borrar remedio borra sus tomas", n == 0)

# ── Demo nunca escribe ─────────────────────────────────────────────────────
for m, u, body in [("POST", "/portal/api/herramientas/tomas", {"remedio_id": 1, "hora": "08:00"}),
                   ("POST", "/portal/api/herramientas/calendario", {})]:
    r = req(m, u, cookies(PR.DEMO_RUT, PR.DEMO_PHONE), json=body)
    check(f"demo no escribe {u}", r.json().get("demo") is True)
with S.db() as cx:
    n = cx.execute("SELECT (SELECT COUNT(*) FROM portal_tomas WHERE rut=?) + (SELECT COUNT(*) FROM portal_cal_tokens WHERE rut=?)",
                   (PR.DEMO_RUT, PR.DEMO_RUT)).fetchone()[0]
check("demo: 0 filas en tablas nuevas", n == 0)

# ── Resultados publicados: un documento ajeno nunca se entrega ─────────────
PR._ensure_tabla_examenes()
with S.db() as cx:
    cx.execute("INSERT INTO portal_examenes (rut, nombre, fecha, valor, publicado) VALUES (?,?,?,?,1)", (A, "Glicemia", hoy, 99))
    cx.execute("INSERT INTO portal_examenes (rut, nombre, fecha, valor, publicado) VALUES (?,?,?,?,0)", (B, "Borrador", hoy, 1))
r = req("GET", "/portal/api/examenes", cookies(A, TEL))
check("A ve su resultado publicado", [e["nombre"] for e in r.json()["examenes"]] == ["Glicemia"])
r = req("GET", "/portal/api/examenes", cookies(B, TEL))
check("B (mismo celular) no ve el resultado de A ni su propio borrador", r.json()["examenes"] == [], r.json())
r = req("GET", "/portal/api/examenes", cookies(B, TEL, active=A))
check("B con cookie activa forjada hacia A: no ve el de A", r.json()["examenes"] == [])

# ── Medilink: historial con estado real y horas de HOY cerradas ────────────
class _R:
    def __init__(self, data):
        self.status_code = 200
        self._d = data

    def json(self):
        return {"data": self._d}

    text = ""

ayer = (HOY - timedelta(days=1)).strftime("%Y-%m-%d")
RAW = [
    {"id": 1, "id_paciente": 7, "id_profesional": 77, "fecha": hoy, "hora_inicio": "09:00:00", "id_estado": 2, "estado_cita": "Atendido"},
    {"id": 2, "id_paciente": 7, "id_profesional": 73, "fecha": hoy, "hora_inicio": "10:00:00", "id_estado": 8, "estado_cita": "No asiste"},
    {"id": 3, "id_paciente": 7, "id_profesional": 60, "fecha": hoy, "hora_inicio": "11:00:00", "id_estado": 7, "estado_cita": "No confirmado"},
    {"id": 4, "id_paciente": 7, "id_profesional": 1, "fecha": ayer, "hora_inicio": "16:00:00", "id_estado": 2, "estado_cita": "Atendido"},
    {"id": 5, "id_paciente": 7, "id_profesional": 52, "fecha": hoy, "hora_inicio": "12:00:00", "id_estado": 5, "estado_cita": "En sala de espera"},
]


async def _fake_get(client, url, params=None, headers=None):
    return _R(RAW)

ML._get = _fake_get
ML._get_shared_client = lambda: None
h_def = asyncio.run(ML.listar_historial_paciente(7, meses=12, rut=A))
h_hoy = asyncio.run(ML.listar_historial_paciente(7, meses=12, rut=A, incluir_hoy_cerradas=True))
check("historial por defecto: igual que antes (solo días pasados)", [x["id"] for x in h_def] == [4])
check("historial trae id_estado/estado (aditivo)", h_def[0]["id_estado"] == 2 and h_def[0]["estado"] == "Atendido")
check("incluir_hoy_cerradas: hoy atendida, en sala y no asiste; NO la pendiente",
      sorted(x["id"] for x in h_hoy) == [1, 2, 4, 5], [x["id"] for x in h_hoy])
fut = asyncio.run(ML.listar_citas_paciente(7, rut=A))
check("citas futuras traen id_estado; la pendiente de hoy sigue ahí", {x["id"]: x["id_estado"] for x in fut}.get(3) == 7
      and 1 not in [x["id"] for x in fut], fut)

# ── Derecho al olvido ──────────────────────────────────────────────────────
r = req("POST", "/portal/api/herramientas/remedios", cookies(A, TEL),
        json={"nombre": "Metformina", "horarios": ["08:00"], "dias": []})
rid2 = r.json()["id"]
PR._acceso_vinculo = _verificado
PR.is_family_link = lambda o, d: (o, d) == (HIJA, A)
req("POST", "/portal/api/herramientas/tomas", cookies(A, TEL), json={"remedio_id": rid2, "hora": "08:00"})
req("POST", "/portal/api/herramientas/calendario", cookies(A, TEL))
# la hija tiene su propio remedio y marca uno de la mamá
r = req("POST", "/portal/api/herramientas/remedios", cookies(HIJA, TEL_HIJA),
        json={"nombre": "Vitamina D", "horarios": ["09:00"], "dias": []})
rid_h = r.json()["id"]
req("POST", "/portal/api/herramientas/tomas", cookies(HIJA, TEL_HIJA), json={"remedio_id": rid_h, "hora": "09:00"})
res = S.delete_patient_data(None, A, deleted_by="test")
with S.db() as cx:
    q = cx.execute("""SELECT (SELECT COUNT(*) FROM portal_tomas WHERE rut=?) + (SELECT COUNT(*) FROM portal_cal_tokens WHERE rut=?)
                      + (SELECT COUNT(*) FROM portal_examenes WHERE rut=?)""", (A, A, A)).fetchone()[0]
    nh = cx.execute("SELECT COUNT(*) FROM portal_tomas WHERE rut=?", (HIJA,)).fetchone()[0]
check("delete_patient_data borra tomas, links de calendario y resultados del RUT", q == 0, res)
check("…sin tocar las tomas de otra persona", nh == 1)
res2 = S.delete_patient_data(None, HIJA, deleted_by="test")
check("borrar a la hija: sus tomas se borran", res2.get("portal_tomas") == 1, res2)
PR._acceso_vinculo = _orig

print(f"\n{OK}/{OK + FAIL} OK")
sys.exit(1 if FAIL else 0)
