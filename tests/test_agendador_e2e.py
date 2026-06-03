"""
Test e2e del agendador público — wiring + salvaguardas.
Prueba lecturas reales contra Medilink y los caminos de RECHAZO de /reservar.
NO ejercita el happy-path de reserva (crearía una cita real). Ese se valida
en vivo con el usuario.

Ejecución:
    SESSIONS_DB=/tmp/agtest_sessions.db PYTHONPATH=app python tests/test_agendador_e2e.py
"""
import os, sys
sys.path.insert(0, "app")

from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient
import config
config.AGENDADOR_PUBLICO_ENABLED = True  # habilitar para el test (sin preview)

import agendador_routes

app = FastAPI()
app.include_router(agendador_routes.router)

_HTML = open("templates/agendador.html", encoding="utf-8").read()

@app.get("/agendar", response_class=HTMLResponse)
def page(request: Request, preview: str | None = Query(None)):
    return _HTML.replace("__PREVIEW__", "")

c = TestClient(app)
F = []
def ck(cond, label, extra=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f" — {extra}" if extra and not cond else ""))
    if not cond: F.append(label)

print("\n1) Página /agendar")
r = c.get("/agendar")
ck(r.status_code == 200 and "__PREVIEW__" not in r.text, "sirve la página sin placeholder", r.status_code)
ck("agendar/api/catalogo" in r.text or "/agendar/api/" in r.text, "el HTML referencia la API real")

print("\n2) GET /catalogo")
r = c.get("/agendar/api/catalogo")
ck(r.status_code == 200, "200", r.status_code)
grupos = r.json().get("grupos", [])
ck(len(grupos) == 5, "5 grupos")
mg = next((e for g in grupos for e in g["especialidades"] if e["especialidad"] == "Medicina General"), None)
ck(mg and "Fonasa $7.880" in mg["precio"], "precio MG real", mg["precio"] if mg else "—")
dent = next((e for g in grupos for e in g["especialidades"] if e["especialidad"] == "Odontología General"), None)
ck(dent and "Crédito" in dent["metodos_pago"], "dentales con débito/crédito")

print("\n3) GET /slots (Medilink real, Dr. Olavarría id=1, primer día)")
r = c.get("/agendar/api/slots", params={"id_prof": 1})
ck(r.status_code == 200, "200", r.status_code)
j = r.json()
ck(bool(j.get("fecha")) and len(j.get("slots", [])) > 0, "devuelve primer día con cupos", str(j.get("fecha")))
if j.get("slots"):
    s = j["slots"][0]
    ck(all(k in s for k in ("hora_inicio", "hora_fin", "id_profesional")), "shape de slot ok")

print("\n4) GET /slots profesional inválido")
r = c.get("/agendar/api/slots", params={"id_prof": 99999})
ck(r.status_code == 400, "400 prof no reconocido", r.status_code)

print("\n5) POST /identificar — RUT inválido")
r = c.post("/agendar/api/identificar", json={"rut": "12345678-0"})
ck(r.status_code == 400, "400 RUT inválido (DV malo)", r.status_code)

print("\n6) POST /reservar — sin consentimiento (no debe tocar Medilink)")
r = c.post("/agendar/api/reservar", json={"rut": "11111111-1", "telefono": "+56912345678",
           "id_profesional": 1, "fecha": "2030-01-10", "hora_inicio": "09:00", "hora_fin": "09:15"})
ck(r.status_code == 400 and "datos" in (r.json().get("detail", "")).lower(), "400 exige consentimiento", str(r.json().get("detail")))

print("\n7) POST /reservar — RUT inválido (con consent)")
r = c.post("/agendar/api/reservar", json={"rut": "12345678-0", "telefono": "+56912345678", "consent": True,
           "id_profesional": 1, "fecha": "2030-01-10", "hora_inicio": "09:00", "hora_fin": "09:15"})
ck(r.status_code == 400, "400 RUT inválido", r.status_code)

print("\n8) POST /reservar — teléfono inválido (con consent + rut válido)")
r = c.post("/agendar/api/reservar", json={"rut": "11111111-1", "telefono": "123", "consent": True,
           "id_profesional": 1, "fecha": "2030-01-10", "hora_inicio": "09:00", "hora_fin": "09:15"})
ck(r.status_code == 400 and "celular" in (r.json().get("detail", "")).lower(), "400 celular inválido")

print("\n9) Gating: con flag OFF, 404")
config.AGENDADOR_PUBLICO_ENABLED = False
r = c.get("/agendar/api/catalogo")
ck(r.status_code == 404, "404 con agendador apagado", r.status_code)
r = c.get("/agendar/api/catalogo", params={"preview": config.ADMIN_TOKEN})
ck(r.status_code == 200, "200 con preview token", r.status_code)
config.AGENDADOR_PUBLICO_ENABLED = True

print()
if F:
    print(f"RESULTADO: {len(F)} fallo(s) — {F}")
    sys.exit(1)
print("RESULTADO: TODO VERDE — wiring + salvaguardas + gating OK")
