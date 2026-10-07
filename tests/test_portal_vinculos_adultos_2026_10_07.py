# -*- coding: utf-8 -*-
"""Portal del paciente: vínculos familiares ADULTOS (Ley 21.719, 7-oct-2026).

Menor = completo. Adulto declarado = solo las horas que el propio titular agendó.
Adulto verificado (ficha Medilink / recepción / OTP) = completo.
DB temporal, Medilink y WhatsApp mockeados: ninguna llamada real.

    venv/bin/python -m pytest tests/test_portal_vinculos_adultos_2026_10_07.py -q
"""
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import session  # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="pv_test_"))
session.DB_PATH = _TMP / "t.db"
with session.db() as _c:
    session._run_ddl_inline(_c)

import medilink  # noqa: E402
import portal_routes as pr  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

OWNER, OWNER_PH = "11111111-1", "56911112222"
ADULTO, MENOR, OTRO = "22222222-2", "33333333-3", "44444444-4"
ADULTO_PH = "56933334444"

_ML = {"caida": False, "cancelaciones": [], "ficha_cel": {}}

FICHAS = {
    OWNER: {"id": 1, "nombre": "Titular Prueba", "rut": OWNER, "fecha_nacimiento": "1980-01-01"},
    ADULTO: {"id": 2, "nombre": "Adulto Prueba", "rut": ADULTO, "fecha_nacimiento": "1975-05-05"},
    MENOR: {"id": 3, "nombre": "Menor Prueba", "rut": MENOR, "fecha_nacimiento": "2018-03-03"},
    OTRO: {"id": 4, "nombre": "Otro Adulto", "rut": OTRO, "fecha_nacimiento": "1960-02-02"},
}
CITAS = {
    2: [{"id": 501, "fecha": "2099-01-10", "hora_inicio": "10:00", "especialidad": "Cardiologia", "profesional": "X"},
        {"id": 502, "fecha": "2099-01-11", "hora_inicio": "11:00", "especialidad": "Kinesiologia", "profesional": "Y"}],
    3: [{"id": 601, "fecha": "2099-02-10", "hora_inicio": "09:00", "especialidad": "Pediatria", "profesional": "Z"}],
    4: [{"id": 701, "fecha": "2099-03-10", "hora_inicio": "09:00", "especialidad": "Medicina", "profesional": "W"}],
}


async def _buscar(rut, strict=False):
    if _ML["caida"]:
        raise RuntimeError("Medilink caído")
    clean = "".join(c for c in rut.upper() if c.isalnum())
    clean = clean[:-1] + "-" + clean[-1]
    f = FICHAS.get(clean)
    if not f:
        return None
    return {**f, "celular": _ML["ficha_cel"].get(clean, ""), "telefono": _ML["ficha_cel"].get(clean, "")}


async def _citas(id_pac, rut=None, raise_on_error=False):
    if _ML["caida"]:
        raise RuntimeError("Medilink caído")
    return [dict(c) for c in CITAS.get(id_pac, [])]


async def _cancelar(id_cita):
    _ML["cancelaciones"].append(id_cita)
    return True


async def _hist(*a, **k):
    return [{"fecha": "2025-01-01", "especialidad": "SECRETO", "profesional": "P"}]


pr.buscar_paciente = _buscar
pr.listar_citas_paciente = _citas
pr.listar_historial_paciente = _hist
medilink.cancelar_cita = _cancelar


async def _send(*a, **k):
    return True


pr.send_whatsapp = _send

app = FastAPI()
app.include_router(pr.router)
cli = TestClient(app)


@pytest.fixture(autouse=True)
def _limpio():
    with session.db() as c:
        c.execute("DELETE FROM family_links")
        c.execute("DELETE FROM citas_bot")
        c.execute("DELETE FROM patient_vitals")
        c.commit()
    pr._acceso_cache.clear()
    _ML.update(caida=False, cancelaciones=[], ficha_cel={})
    cli.cookies.clear()
    yield


def _login(active=None):
    cli.cookies.clear()
    cli.cookies.set("portal_session", pr._sign_portal_cookie(OWNER, OWNER_PH))
    if active:
        cli.cookies.set("portal_active", pr._sign_active_cookie(OWNER, active))


def _agendar_tercero(id_cita, id_pac, phone=OWNER_PH):
    session.save_cita_bot(phone, str(id_cita), "Esp", "Prof", "2099-01-10", "10:00",
                          "PRESENCIAL", paciente_nombre="x", es_tercero=True,
                          id_paciente_medilink=id_pac)


def _vinculo(dep, metodo):
    session.add_family_link(OWNER, dep, "Nombre " + dep, "familiar", metodo)


# ── menor ────────────────────────────────────────────────────────────────────
def test_menor_acceso_completo():
    _vinculo(MENOR, "tutor_declaration")
    _login(MENOR)
    d = cli.get("/portal/api/datos").json()
    assert d["acceso"] == "completo"
    assert d["historial"] and d["nombre"] == "Menor Prueba"
    assert cli.get("/portal/api/vitals").status_code == 200
    assert cli.get("/portal/api/perfil").status_code == 200
    assert cli.get("/portal/api/examenes").status_code == 200


def test_tutor_declaration_con_medilink_caido_falla_cerrado():
    # Revisión de seguridad 7-oct: el bot crea tutor_declaration sin mirar la
    # edad; sin confirmar minoría en Medilink no se abre el perfil completo.
    _vinculo(MENOR, "tutor_declaration")
    _ML["caida"] = True
    _login(MENOR)
    assert cli.get("/portal/api/vitals").status_code == 403


# ── adulto declarado ─────────────────────────────────────────────────────────
def test_adulto_declarado_solo_horas_propias():
    _vinculo(ADULTO, "declared")
    _agendar_tercero(501, 2)                 # lo agendó el titular
    session.save_cita_bot("56999990000", "502", "Esp", "Prof", "2099-01-11", "11:00",
                          "PRESENCIAL", es_tercero=True)   # otra persona (otro teléfono)
    _login(ADULTO)
    d = cli.get("/portal/api/datos").json()
    assert d["acceso"] == "solo_horas_agendadas"
    assert [c["id"] for c in d["citas_futuras"]] == [501]
    assert d["historial"] == [] and d["diagnosticos"] == []
    assert "SECRETO" not in str(d)
    assert d["fecha_nacimiento"] == "" and d["sexo"] == ""
    assert "verifiquen el vínculo" in d["aviso"]


def test_adulto_declarado_403_en_datos_sensibles():
    _vinculo(ADULTO, "declared")
    _login(ADULTO)
    assert cli.get("/portal/api/vitals").status_code == 403
    assert cli.post("/portal/api/vitals", json={"tipo": "peso", "valor": 70}).status_code == 403
    assert cli.delete("/portal/api/vitals/1").status_code == 403
    assert cli.get("/portal/api/perfil").status_code == 403
    assert cli.post("/portal/api/perfil", json={"email": "a@b.cl"}).status_code == 403
    assert cli.get("/portal/api/examenes").status_code == 403
    assert cli.post("/portal/api/checkin", json={"id_cita": "501"}).status_code == 403
    det = cli.get("/portal/api/perfil").json()["detail"]
    assert det["error"] == "vinculo_no_verificado" and det["acceso"] == "solo_horas_agendadas"


def test_family_y_overview_sin_datos_del_representado():
    _vinculo(ADULTO, "declared")
    _agendar_tercero(501, 2)
    _login()
    links = cli.get("/portal/api/family").json()["links"]
    assert links[0]["acceso"] == "solo_horas_agendadas" and links[0]["verificado"] is False
    m = [x for x in cli.get("/portal/api/family/overview").json()["members"]
         if x["rut"] == ADULTO][0]
    assert m["acceso"] == "solo_horas_agendadas"
    assert m["proxima"]["especialidad"] == "Cardiologia"
    for k in ("dx", "ultima", "n_at", "sexo", "edad"):
        assert k not in m
    assert "SECRETO" not in str(m)


def test_switch_a_adulto_no_verificado_informa_el_flag():
    _vinculo(ADULTO, "declared")
    _login()
    r = cli.post("/portal/api/family/switch", json={"rut": ADULTO}).json()
    assert r["ok"] and r["acceso"] == "solo_horas_agendadas"


# ── verificación automática por ficha ────────────────────────────────────────
def test_ficha_con_celular_coincidente_da_acceso_completo_y_se_guarda():
    _vinculo(ADULTO, "declared")
    _ML["ficha_cel"][ADULTO] = "+56 9 1111 2222"       # formato distinto, mismo número
    _login(ADULTO)
    d = cli.get("/portal/api/datos").json()
    assert d["acceso"] == "completo" and d["historial"]
    l = session.get_family_link(OWNER, ADULTO)
    assert l["verification_method"] == "ficha_medilink" and l["verified_phone"] == "911112222"
    # la próxima vez no consulta Medilink
    _ML["caida"] = True
    pr._acceso_cache.clear()
    assert cli.get("/portal/api/vitals").status_code == 200


def test_ficha_con_otro_celular_queda_restringido():
    _vinculo(ADULTO, "declared")
    _ML["ficha_cel"][ADULTO] = "56977778888"
    _login(ADULTO)
    assert cli.get("/portal/api/datos").json()["acceso"] == "solo_horas_agendadas"
    assert session.get_family_link(OWNER, ADULTO)["verification_method"] == "declared"


def test_medilink_caido_falla_cerrado_sin_romper_la_pagina():
    _vinculo(ADULTO, "declared")
    _ML["ficha_cel"][ADULTO] = OWNER_PH
    _ML["caida"] = True
    _login(ADULTO)
    r = cli.get("/portal/api/datos")
    assert r.status_code == 200
    d = r.json()
    assert d["acceso"] == "solo_horas_agendadas"
    assert d["historial"] == []
    assert cli.get("/portal/api/vitals").status_code == 403


def test_ficha_vencida_se_reverifica_y_cambio_de_telefono_tambien():
    _vinculo(ADULTO, "declared")
    _ML["ficha_cel"][ADULTO] = OWNER_PH
    _login(ADULTO)
    assert cli.get("/portal/api/vitals").status_code == 200
    # envejecer la verificación 40 días -> re-verifica; Medilink cambió el celular
    with session.db() as c:
        c.execute("UPDATE family_links SET verified_at=datetime('now','-40 days')")
        c.commit()
    _ML["ficha_cel"][ADULTO] = "56900000001"
    pr._acceso_cache.clear()
    assert cli.get("/portal/api/vitals").status_code == 403


# ── recepción y OTP ──────────────────────────────────────────────────────────
def test_recepcion_verifica_y_da_acceso_completo():
    import admin_routes
    app2 = FastAPI()
    app2.include_router(admin_routes.router)
    app2.dependency_overrides[admin_routes.require_admin] = lambda: "ok"
    adm = TestClient(app2)
    _vinculo(ADULTO, "declared")
    lst = adm.get("/admin/api/portal-vinculos", params={"q": OWNER}).json()
    assert [p["dependent_rut"] for p in lst["pendientes"]] == [ADULTO]
    assert adm.post("/admin/api/portal-vinculos/verificar",
                    json={"owner_rut": OWNER, "dependent_rut": ADULTO}).status_code == 400
    r = adm.post("/admin/api/portal-vinculos/verificar",
                 json={"owner_rut": OWNER, "dependent_rut": ADULTO,
                       "verificado_por": "Recepción Ana", "carta_firmada": True})
    assert r.status_code == 200
    l = session.get_family_link(OWNER, ADULTO)
    assert l["verification_method"] == "recepcion" and l["verified_by"] == "Recepción Ana"
    assert l["carta_firmada"] == 1
    assert adm.get("/admin/api/portal-vinculos", params={"q": OWNER}).json()["pendientes"] == []
    _ML["caida"] = True      # no depende de Medilink
    _login(ADULTO)
    assert cli.get("/portal/api/vitals").status_code == 200


def test_otp_deja_metodo_otp_y_acceso_completo():
    session.save_portal_otp(ADULTO, ADULTO_PH, "123456")
    _login()
    r = cli.post("/portal/api/family/verify-otp",
                 json={"rut": ADULTO, "code": "123456", "relation": "hermano"})
    assert r.status_code == 200
    assert session.get_family_link(OWNER, ADULTO)["verification_method"] == "otp"
    _ML["caida"] = True
    _login(ADULTO)
    assert cli.get("/portal/api/vitals").status_code == 200


def test_bot_que_vuelve_a_declarar_no_degrada_un_vinculo_verificado():
    _vinculo(ADULTO, "otp")
    _vinculo(ADULTO, "declared")      # el bot agenda otra hora para él
    assert session.get_family_link(OWNER, ADULTO)["verification_method"] == "otp"
    session.revoke_family_link(OWNER, ADULTO)
    _vinculo(ADULTO, "declared")      # revocado y recreado: vuelve a pendiente
    assert session.get_family_link(OWNER, ADULTO)["verification_method"] == "declared"


# ── anular / cambiar ─────────────────────────────────────────────────────────
def test_anular_hora_propia_ok_y_ajena_403():
    _vinculo(ADULTO, "declared")
    _agendar_tercero(501, 2)
    _login(ADULTO)
    r = cli.post("/portal/api/horas/cancelar", json={"rut": ADULTO, "id_cita": "502"})
    assert r.status_code == 403 and _ML["cancelaciones"] == []      # no la agendó él
    r = cli.post("/portal/api/horas/cancelar", json={"rut": ADULTO, "id_cita": "501"})
    assert r.status_code == 200 and _ML["cancelaciones"] == [501]


def test_no_puede_anular_horas_de_otra_persona_no_vinculada():
    _vinculo(ADULTO, "declared")
    _agendar_tercero(701, 4)
    _login()
    r = cli.post("/portal/api/horas/cancelar", json={"rut": OTRO, "id_cita": "701"})
    assert r.status_code == 403 and _ML["cancelaciones"] == []


def test_verificado_puede_anular_cualquier_hora_futura_de_esa_persona():
    _vinculo(ADULTO, "recepcion")
    _login(ADULTO)
    assert cli.post("/portal/api/horas/cancelar",
                    json={"rut": ADULTO, "id_cita": "502"}).status_code == 200


def test_anular_con_medilink_caido_503_sin_cancelar():
    _vinculo(ADULTO, "declared")
    _agendar_tercero(501, 2)
    _ML["caida"] = True
    _login(ADULTO)
    r = cli.post("/portal/api/horas/cancelar", json={"rut": ADULTO, "id_cita": "501"})
    assert r.status_code == 503 and _ML["cancelaciones"] == []


def test_titular_sigue_viendo_todo_lo_suyo():
    _login()
    d = cli.get("/portal/api/datos").json()
    assert d["acceso"] == "completo" and d["is_dependent"] is False
