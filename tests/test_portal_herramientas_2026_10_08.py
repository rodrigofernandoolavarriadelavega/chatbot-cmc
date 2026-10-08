"""Portal: herramientas (remedios, ficha de emergencia, qué me toca, lista de espera).

Corre:  cd tests && PYTHONPATH=../app ~/chatbot-cmc/venv/bin/python test_portal_herramientas_2026_10_08.py
Usa una sessions.db temporal (nunca la real) y una app FastAPI mínima con solo
este router. No toca Medilink ni WhatsApp.
"""
import asyncio
import os
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import session as S  # noqa: E402

_TMP = tempfile.mkdtemp()
S.DB_PATH = Path(_TMP) / "sessions.db"

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import portal_routes as PR  # noqa: E402
import portal_herramientas_routes as PH  # noqa: E402

app = FastAPI()
app.include_router(PH.router)
c = TestClient(app)

OK = FAIL = 0


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


TEL = "56911112222"          # teléfono COMPARTIDO por dos pacientes
A, B = "11111111-1", "22222222-2"
REM = {"nombre": "Losartán 50 mg", "dosis": "1 comprimido", "horarios": ["20:00", "08:00"], "dias": []}

# ── 401 sin sesión ─────────────────────────────────────────────────────────
for m, u in [("GET", "/portal/api/herramientas/remedios"), ("POST", "/portal/api/herramientas/remedios"),
             ("DELETE", "/portal/api/herramientas/remedios/1"), ("GET", "/portal/api/herramientas/remedios.ics"),
             ("GET", "/portal/api/herramientas/ficha"), ("POST", "/portal/api/herramientas/ficha"),
             ("DELETE", "/portal/api/herramientas/ficha"), ("POST", "/portal/api/herramientas/lista-espera")]:
    r = req(m, u, json={})
    check(f"401 sin sesión {m} {u}", r.status_code == 401, r.status_code)
r = req("GET", "/portal/api/herramientas/remedios", {PR._COOKIE_NAME: "falsa:firma"})
check("401 con cookie adulterada", r.status_code == 401)

# ── Guardar y aislar por RUT (mismo teléfono) ──────────────────────────────
r = req("POST", "/portal/api/herramientas/remedios", cookies(A, TEL), json=REM)
check("A guarda remedio", r.status_code == 200 and r.json()["ok"], r.text)
rid_a = r.json().get("id")
r = req("GET", "/portal/api/herramientas/remedios", cookies(A, TEL))
la = r.json()["remedios"]
check("A ve su remedio con horarios ordenados", len(la) == 1 and la[0]["horarios"] == ["08:00", "20:00"], la)
check("la respuesta no expone el RUT", "rut" not in la[0])
r = req("GET", "/portal/api/herramientas/remedios", cookies(B, TEL))
check("B (mismo teléfono) NO ve los remedios de A", r.json()["remedios"] == [], r.json())
r = req("DELETE", f"/portal/api/herramientas/remedios/{rid_a}", cookies(B, TEL))
check("B no puede borrar el remedio de A (404)", r.status_code == 404)
r = req("POST", "/portal/api/herramientas/remedios", cookies(B, TEL), json={**REM, "id": rid_a, "nombre": "X"})
check("B no puede editar el remedio de A (404)", r.status_code == 404)
# Cookie activa hacia un RUT NO vinculado: se ignora y queda en el titular
r = req("GET", "/portal/api/herramientas/remedios", cookies(B, TEL, active=A))
check("perfil activo no vinculado se ignora (B sigue viendo lo suyo)", r.json()["remedios"] == [])

# ── Validaciones ───────────────────────────────────────────────────────────
r = req("POST", "/portal/api/herramientas/remedios", cookies(A, TEL), json={**REM, "nombre": "  "})
check("400 sin nombre", r.status_code == 400)
r = req("POST", "/portal/api/herramientas/remedios", cookies(A, TEL), json={**REM, "horarios": ["25:00"]})
check("400 hora inválida", r.status_code == 400)
r = req("POST", "/portal/api/herramientas/remedios", cookies(A, TEL), json={**REM, "quedan": "abc"})
check("400 'quedan' no numérico", r.status_code == 400)
r = req("POST", "/portal/api/herramientas/remedios", cookies(A, TEL),
        json={"nombre": "Vitamina D", "dosis": "1 cápsula; con comida, sí", "horarios": ["09:00"], "dias": [0, 3],
              "quedan": "12", "por_toma": "1"})
check("A guarda remedio por días (lun y jue)", r.status_code == 200)

# ── .ics ───────────────────────────────────────────────────────────────────
r = req("GET", "/portal/api/herramientas/remedios.ics", cookies(A, TEL))
ics = r.text
check(".ics 200 text/calendar con nombre de archivo", r.status_code == 200 and r.headers["content-type"].startswith("text/calendar")
      and "mis-remedios.ics" in r.headers.get("content-disposition", ""))
raw = r.content
check(".ics usa CRLF en todas las líneas", raw.count(b"\r\n") == raw.count(b"\n"))
check(".ics líneas ≤75 octetos (RFC 5545 §3.1)", max(len(x) for x in raw.split(b"\r\n")) <= 75)
check(".ics trae VTIMEZONE America/Santiago", "BEGIN:VTIMEZONE\r\nTZID:America/Santiago" in ics)
check(".ics 3 eventos (2 tomas + 1)", ics.count("BEGIN:VEVENT") == 3)
check(".ics cada evento con VALARM a la hora exacta", ics.count("BEGIN:VALARM") == 3 and ics.count("TRIGGER:PT0S") == 3)
check(".ics diaria y semanal por días", "RRULE:FREQ=DAILY" in ics and "RRULE:FREQ=WEEKLY;BYDAY=MO,TH" in ics)
check(".ics escapa coma y punto y coma", "1 cápsula\\; con comida\\, sí" in ics.replace("\r\n ", ""))
check(".ics recuerda seguir la indicación médica", "Siga siempre la indicación de su médico" in ics.replace("\r\n ", ""))
uids1 = re.findall(r"UID:(\S+)", ics)
uids2 = re.findall(r"UID:(\S+)", req("GET", "/portal/api/herramientas/remedios.ics", cookies(A, TEL)).text)
check(".ics UID estable al re-descargar (no duplica)", uids1 == uids2 and len(set(uids1)) == 3)
check("UID distinto para otro paciente con el mismo remedio", PH.uid_toma(A, "x", "08:00") != PH.uid_toma(B, "x", "08:00"))
if os.getenv("ICS_OUT"):
    Path(os.environ["ICS_OUT"]).write_bytes(raw)
r = req("GET", "/portal/api/herramientas/remedios.ics", cookies(B, TEL))
check(".ics 404 si no hay remedios con horario", r.status_code == 404)
# Primera toma en día válido (lunes o jueves)
lun = datetime(2026, 10, 7, 9, 0, tzinfo=ZoneInfo("America/Santiago"))  # miércoles
txt = PH.construir_ics(A, [{"uid": "u", "nombre": "N", "dosis": "", "horarios": ["09:00"], "dias": [0, 3]}], ahora=lun)
check("primera toma semanal cae en el próximo día válido (jue 8)", "DTSTART;TZID=America/Santiago:20261008T090000" in txt)

# ── Ficha de emergencia ────────────────────────────────────────────────────
F = {"alergias": "Penicilina", "enfermedades": "Presión alta", "grupo_sanguineo": "O+",
     "contacto_nombre": "Juan", "contacto_telefono": "+56 9 8765 4321", "notas": ""}
r = req("POST", "/portal/api/herramientas/ficha", cookies(A, TEL), json=F)
check("ficha SIN consentimiento → 400", r.status_code == 400)
r = req("POST", "/portal/api/herramientas/ficha", cookies(A, TEL), json={**F, "consent": "si"})
check("consentimiento debe ser true explícito (no 'si')", r.status_code == 400)
r = req("POST", "/portal/api/herramientas/ficha", cookies(A, TEL), json={**F, "grupo_sanguineo": "Z", "consent": True})
check("grupo sanguíneo inválido → 400", r.status_code == 400)
r = req("POST", "/portal/api/herramientas/ficha", cookies(A, TEL), json={**F, "consent": True})
check("ficha con consentimiento → 200", r.status_code == 200)
r = req("GET", "/portal/api/herramientas/ficha", cookies(A, TEL))
check("A lee su ficha", r.json()["ficha"]["alergias"] == "Penicilina" and r.json().get("consent_at"))
r = req("GET", "/portal/api/herramientas/ficha", cookies(B, TEL))
check("B (mismo teléfono) no ve la ficha de A", r.json()["ficha"] is None)
r = req("DELETE", "/portal/api/herramientas/ficha", cookies(B, TEL))
r = req("GET", "/portal/api/herramientas/ficha", cookies(A, TEL))
check("borrar desde B no borra la de A", r.json()["ficha"] is not None)

# ── Familiar: 403 sin verificar, acceso completo verificado ────────────────
DEP = "33333333-3"
PR.is_family_link = lambda o, d: (o, d) == (A, DEP)          # el vínculo existe
_orig = PR._acceso_vinculo


async def _no_verificado(o, p, d):
    return {"acceso": PR.ACCESO_SOLO_HORAS, "metodo": "declared", "motivo": "ficha_no_coincide"}


async def _verificado(o, p, d):
    return {"acceso": PR.ACCESO_COMPLETO, "metodo": "otp", "motivo": ""}

PR._acceso_vinculo = _no_verificado
for m, u in [("GET", "/portal/api/herramientas/remedios"), ("POST", "/portal/api/herramientas/remedios"),
             ("GET", "/portal/api/herramientas/remedios.ics"), ("GET", "/portal/api/herramientas/ficha"),
             ("POST", "/portal/api/herramientas/ficha")]:
    r = req(m, u, cookies(A, TEL, active=DEP), json={**REM, **F, "consent": True})
    check(f"403 familiar NO verificado {m} {u}", r.status_code == 403
          and r.json()["detail"]["error"] == "vinculo_no_verificado", r.status_code)
PR._acceso_vinculo = _verificado
r = req("POST", "/portal/api/herramientas/remedios", cookies(A, TEL, active=DEP),
        json={"nombre": "Salbutamol", "horarios": [], "dias": []})
check("familiar verificado: se guarda en SU ficha", r.status_code == 200)
r = req("GET", "/portal/api/herramientas/remedios", cookies(A, TEL, active=DEP))
check("familiar verificado: ve solo lo del familiar", [x["nombre"] for x in r.json()["remedios"]] == ["Salbutamol"])
r = req("GET", "/portal/api/herramientas/remedios", cookies(A, TEL))
check("titular no mezcla lo del familiar", "Salbutamol" not in [x["nombre"] for x in r.json()["remedios"]])
with S.db() as cx:
    row = cx.execute("SELECT creado_por FROM portal_remedios WHERE rut=?", (DEP,)).fetchone()
check("queda registrado quién lo anotó (titular)", row and row[0] == A)
PR._acceso_vinculo = _orig

# ── Lista de espera = la MISMA tabla del bot ───────────────────────────────
r = req("POST", "/portal/api/herramientas/lista-espera", cookies(A, TEL),
        json={"especialidad": "Cardiología", "id_profesional": 60, "nombre": "Ana Uno"})
check("lista de espera 200", r.status_code == 200, r.text)
with S.db() as cx:
    w = cx.execute("SELECT phone, rut, especialidad, id_prof_pref, notas FROM waitlist").fetchall()
check("inscribe en session.waitlist con teléfono del titular y RUT del paciente",
      len(w) == 1 and w[0]["phone"] == TEL and w[0]["rut"] == A and w[0]["id_prof_pref"] == 60 and w[0]["notas"] == "portal", [dict(x) for x in w])
req("POST", "/portal/api/herramientas/lista-espera", cookies(A, TEL), json={"especialidad": "Cardiología"})
with S.db() as cx:
    n = cx.execute("SELECT COUNT(*) FROM waitlist").fetchone()[0]
check("re-inscribir no duplica (usa add_to_waitlist del bot)", n == 1)

# ── Demo nunca escribe ─────────────────────────────────────────────────────
r = req("POST", "/portal/api/herramientas/remedios", cookies(PR.DEMO_RUT, PR.DEMO_PHONE), json=REM)
with S.db() as cx:
    n = cx.execute("SELECT COUNT(*) FROM portal_remedios WHERE rut=?", (PR.DEMO_RUT,)).fetchone()[0]
check("RUT demo no persiste nada", r.json().get("demo") and n == 0)

# ── Guía preventiva: misma fuente que el bot ───────────────────────────────
g = req("GET", "/portal/api/herramientas/guia-preventiva").json()
check("guía pública sin sesión", g["ok"])
check("todas las filas del bot tienen presentación (sin_mapa vacío)", g["sin_mapa"] == [], g["sin_mapa"])
ids = {i["id"] for i in g["items"]}
check("guía cubre PAP, mamografía, colesterol, presión, EMPAM, colon, próstata, vacunas",
      {"pap", "mamografia_ges", "colesterol", "presion", "empam", "colon", "prostata", "vacunas_mayor"} <= ids, ids)
from autocuidado import _EXAMENES_PREVENTIVOS  # noqa: E402
pap = next(i for i in g["items"] if i["id"] == "pap")
fila = next(f for f in _EXAMENES_PREVENTIVOS if "PAP" in f[3])
check("rango del PAP = el del bot", (pap["edad_min"], pap["edad_max"], pap["sexo"]) == fila[:3])
texto = str(g).lower()
check("ningún ítem manda consultas de niños al CESFAM",
      "cesfam" not in next(i for i in g["items"] if i["id"] == "control_sano")["donde_txt"].lower()
      and all(v["donde"] in ("vacunatorio", "colegio") for v in g["vacunas"]))
check("el centro no vacuna (vacunas fuera)", next(i for i in g["items"] if i["id"] == "vacunas_mayor")["donde"] == "vacunatorio")

# ── Derecho al olvido incluye las tablas nuevas ────────────────────────────
res = S.delete_patient_data(None, A, deleted_by="test")
with S.db() as cx:
    quedan = cx.execute("SELECT (SELECT COUNT(*) FROM portal_remedios WHERE rut=?) + (SELECT COUNT(*) FROM portal_ficha_emergencia WHERE rut=?)", (A, A)).fetchone()[0]
check("delete_patient_data borra remedios y ficha del RUT", quedan == 0 and res.get("portal_remedios") and res.get("portal_ficha_emergencia"), res)
with S.db() as cx:
    dep = cx.execute("SELECT COUNT(*) FROM portal_remedios WHERE rut=?", (DEP,)).fetchone()[0]
check("…y no toca los de otra persona", dep == 1)

print(f"\n{OK}/{OK + FAIL} OK")
sys.exit(1 if FAIL else 0)
