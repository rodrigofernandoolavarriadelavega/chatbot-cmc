# -*- coding: utf-8 -*-
"""Portal del Profesional — auth, auditoría y alcance por profesional.

DB temporal con datos SINTÉTICOS. Tokens de prueba (los reales del .env quedan
fuera). BI mockeada: ninguna llamada a Medilink, BI ni WhatsApp.

    venv/bin/python -m pytest tests/test_portal_profesional.py -q
"""
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import session  # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="pp_test_"))
session.DB_PATH = _TMP / "t.db"
with session.db() as _c:
    session._run_ddl_inline(_c)

import alma_scope  # noqa: E402
import config  # noqa: E402

config.OLACORE_TOKEN = "t_dueno"
config.ADMIN_TOKEN = "t_recep"
alma_scope._ADMIN_TOKENS = ("t_recep", "t_dueno")
config.ALMA_PROFILES.clear()
config.ALMA_PROFILES.update({
    "t_kine": {"variante": "Kine Prueba", "modulos": ["portal_pro"], "profesional_id": 77},
    "t_sin_modulo": {"variante": "Nutri Prueba", "modulos": ["agenda"], "profesional_id": 52},
})

import portal_profesional_routes as pp  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

PROPIO, AJENO = "11111111-1", "22222222-2"
_BI = {"caida": False}


def _bi_fake(prof_id, keys):
    if _BI["caida"]:
        return None
    return {k for k in keys if prof_id == 77 and k == pp._key(PROPIO)}


pp._pacientes_de_profesional = _bi_fake
pp._citas_bi = lambda k, prof: {"disponible": False, "ultima": None, "proxima": None}

app = FastAPI()
app.include_router(pp.router)
cli = TestClient(app)


def _utc(h):
    return (datetime.now(timezone.utc) - timedelta(hours=h)).strftime("%Y-%m-%d %H:%M:%S")


@pytest.fixture(autouse=True, scope="module")
def _sembrar():
    with session.db() as c:
        c.execute("INSERT INTO contact_profiles (phone,rut,nombre,updated_at) VALUES ('56911111111','11.111.111-1','Paciente Propio',?)", (_utc(5),))
        c.execute("INSERT INTO contact_profiles (phone,rut,nombre,updated_at) VALUES ('56922222222','22222222-2','Paciente Ajeno',?)", (_utc(5),))
        for rut in (PROPIO, AJENO):
            c.execute("INSERT INTO patient_vitals (rut,tipo,valor,valor2,ts,created_at) VALUES (?,?,?,?,?,?)",
                      (rut, "presion", 140, 90, _utc(2).replace(" ", "T") + "Z", _utc(2)))
        # tercero que comparte el teléfono del paciente propio: jamás debe aparecer en su ficha
        c.execute("INSERT INTO contact_profiles (phone,rut,nombre,direccion,updated_at) VALUES ('56911111111x','33333333-3','Tercero Mismo Fono','Dirección del tercero',?)", (_utc(1),))
        c.execute("INSERT INTO family_links (owner_rut,dependent_rut,dependent_nombre,relation,verification_method) VALUES ('33333333-3',?, 'Paciente Propio','padre/madre','declared')", (PROPIO,))
        c.execute("INSERT INTO conversation_events (phone,event,meta,ts) VALUES ('56911111111','portal_checkin_confirmado',?,?)",
                  (json.dumps({"rut": PROPIO, "especialidad": "Kinesiología", "hora": "10:00"}), _utc(1)))
    pp.ensure_tabla_accesos()
    yield


def _accesos():
    with session.db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM portal_pro_accesos ORDER BY id").fetchall()]


# ── Auth ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("url", [
    "/portal-profesional/api/yo",
    "/portal-profesional/api/novedades",
    "/portal-profesional/api/buscar?q=Paciente",
    f"/portal-profesional/api/paciente/{PROPIO}",
    f"/portal-profesional/api/paciente/{PROPIO}/preparar",
    "/portal-profesional/api/accesos",
])
def test_401_sin_auth(url):
    assert cli.get(url).status_code == 401
    assert cli.get(url + ("&" if "?" in url else "?") + "token=inventado").status_code == 401


def test_pagina_sin_auth_401_y_demo_200():
    assert cli.get("/portal-profesional").status_code == 401
    assert cli.get("/portal-profesional?demo=1").status_code == 200
    assert cli.get("/portal-profesional?demo=1", headers={"host": "centromedicocarampangue.cl"}).status_code == 404


def test_perfil_sin_modulo_403():
    assert cli.get("/portal-profesional/api/novedades?token=t_sin_modulo").status_code == 403


# ── Auditoría ───────────────────────────────────────────────────────────────

def test_auditoria_se_escribe_en_cada_acceso():
    antes = len(_accesos())
    r = cli.get(f"/portal-profesional/api/paciente/{PROPIO}?token=t_recep", headers={"x-forwarded-for": "190.1.2.3"})
    assert r.status_code == 200
    a = _accesos()[-1]
    assert len(_accesos()) == antes + 1
    assert a["accion"] == "ver_ficha" and a["rut"] == pp._key(PROPIO)
    assert a["actor"] == "Recepción" and a["rol"] == "recepcion" and a["ip"] == "190.1.2.3"
    assert "t_recep" not in json.dumps(a)  # jamás se guarda el token
    cli.get(f"/portal-profesional/api/paciente/{PROPIO}/preparar?token=t_recep")
    assert _accesos()[-1]["accion"] == "preparar_control"


def test_sin_auditoria_no_hay_datos(monkeypatch):
    def roto(*a, **k):
        raise RuntimeError("disco lleno")
    monkeypatch.setattr(pp, "db", roto)
    r = cli.get(f"/portal-profesional/api/paciente/{PROPIO}?token=t_dueno")
    assert r.status_code == 503 and "Paciente Propio" not in r.text


def test_accesos_solo_dueno():
    assert cli.get("/portal-profesional/api/accesos?token=t_recep").status_code == 403
    assert cli.get("/portal-profesional/api/accesos?token=t_kine").status_code == 403
    r = cli.get("/portal-profesional/api/accesos?token=t_dueno")
    assert r.status_code == 200 and r.json()["accesos"]


# ── Alcance: el profesional solo ve a sus pacientes ────────────────────────

def test_profesional_no_ve_pacientes_ajenos():
    lista = cli.get("/portal-profesional/api/novedades?dias=7&token=t_kine").json()["pacientes"]
    ruts = {pp._key(p["rut"]) for p in lista}
    assert pp._key(PROPIO) in ruts and pp._key(AJENO) not in ruts

    assert cli.get(f"/portal-profesional/api/paciente/{PROPIO}?token=t_kine").status_code == 200
    r = cli.get(f"/portal-profesional/api/paciente/{AJENO}?token=t_kine")
    assert r.status_code == 403 and "Paciente Ajeno" not in r.text
    assert cli.get(f"/portal-profesional/api/paciente/{AJENO}/preparar?token=t_kine").status_code == 403
    neg = _accesos()[-1]
    assert neg["rut"] == pp._key(AJENO) and neg["resultado"] == "denegado"

    b = cli.get("/portal-profesional/api/buscar?q=Paciente&token=t_kine").json()["pacientes"]
    assert [p["nombre"] for p in b] == ["Paciente Propio"]
    assert cli.get(f"/portal-profesional/api/buscar?q={AJENO}&token=t_kine").json()["pacientes"] == []


def test_recepcion_y_dueno_ven_todo():
    for tk in ("t_recep", "t_dueno"):
        ruts = {pp._key(p["rut"]) for p in cli.get(f"/portal-profesional/api/novedades?token={tk}").json()["pacientes"]}
        assert {pp._key(PROPIO), pp._key(AJENO)} <= ruts


def test_bi_caida_falla_cerrado():
    _BI["caida"] = True
    try:
        d = cli.get("/portal-profesional/api/novedades?token=t_kine").json()
        assert d["pacientes"] == [] and d["alcance_verificado"] is False
        assert cli.get(f"/portal-profesional/api/paciente/{PROPIO}?token=t_kine").status_code == 503
    finally:
        _BI["caida"] = False


# ── Datos: por RUT, sin terceros, sin interpretación ───────────────────────

def test_ficha_no_mezcla_datos_de_quien_comparte_telefono():
    f = cli.get(f"/portal-profesional/api/paciente/{PROPIO}?token=t_dueno").json()
    assert f["paciente"]["nombre"] == "Paciente Propio"
    assert f["datos"]["direccion"] is None                      # la dirección es del tercero
    assert all("rut" not in r for r in f["representantes"])     # sin RUT de terceros
    assert f["representantes"][0]["metodo"] == "declared"
    assert f["actividad"][0]["evento"] == "portal_checkin_confirmado"


def test_examen_sin_semaforo_del_paciente():
    e = pp._examen_crudo({"id": 1, "nombre": "Glicemia", "valor": 118, "unidad": "mg/dL", "rango_min": 70,
                          "rango_max": 100, "nivel": "atencion", "conclusion": "Algo alta", "created_at": None})
    assert e["fuera_rango"] is True and "nivel" not in e and "conclusion" not in e
    sin = pp._examen_crudo({"valor": 5, "rango_min": 0, "rango_max": 0})   # rango vacío al cargar
    assert sin["fuera_rango"] is None and sin["rango_min"] is None


def test_demo_no_toca_la_base():
    antes = len(_accesos())
    assert cli.get("/portal-profesional/api/novedades?demo=1").json()["pacientes"]
    assert cli.get("/portal-profesional/api/paciente/50000103-5?demo=1").status_code == 200
    assert cli.get("/portal-profesional/api/paciente/50000103-5/preparar?demo=1").status_code == 200
    assert len(_accesos()) == antes
