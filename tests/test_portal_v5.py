"""Tests del backend del Portal v5 (portal_routes.py) — offline, sin red.

Cubre lo agregado en el rediseño v5/Fase 2:
- Tokens de login por magic link (firma, expiración, manipulación).
- Datos demo de exámenes (shape que consume el semáforo del frontend).
- Whitelist de eventos de telemetría.
- Rate limiter en memoria de los links.

Correr: python3 -m pytest tests/test_portal_v5.py -q
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import portal_routes as pr  # noqa: E402


# ── Magic link de login ───────────────────────────────────────────────────────

def test_login_token_valido():
    tok = pr.generar_login_token("12345678-5", "56911112222")
    res = pr.verificar_login_token(tok)
    assert res == ("12345678-5", "56911112222")


def test_login_token_expirado(monkeypatch):
    tok = pr.generar_login_token("12345678-5", "56911112222")
    real_time = time.time
    monkeypatch.setattr(pr.time, "time", lambda: real_time() + pr._LOGIN_LINK_TTL + 5)
    assert pr.verificar_login_token(tok) is None


def test_login_token_manipulado():
    tok = pr.generar_login_token("12345678-5", "56911112222")
    # cambiar el rut del payload invalida la firma
    partes = tok.split("|")
    partes[0] = "99999999-9"
    assert pr.verificar_login_token("|".join(partes)) is None
    # basura directa
    assert pr.verificar_login_token("") is None
    assert pr.verificar_login_token("a|b|c") is None
    assert pr.verificar_login_token("a|b|c|d|e") is None


def test_login_token_distinto_del_magic_de_citas():
    """El token de login NO debe validar como token de /mis-citas ni viceversa
    (claves HMAC derivadas con propósitos distintos)."""
    tok_login = pr.generar_login_token("12345678-5", "56911112222")
    assert pr.verificar_magic_token(tok_login) is None
    tok_citas = pr.generar_magic_token("56911112222")
    assert pr.verificar_login_token(tok_citas) is None


# ── Exámenes demo (contrato con el frontend) ─────────────────────────────────

def test_demo_examenes_shape():
    exs = pr._demo_examenes()
    assert len(exs) >= 3
    campos = {"id", "nombre", "fecha", "valor", "unidad", "rango_min", "rango_max",
              "escala_min", "escala_max", "nivel", "etiqueta", "conclusion", "que_hacer"}
    for ex in exs:
        assert campos.issubset(ex.keys()), f"faltan campos en {ex.get('nombre')}"
        assert ex["nivel"] in {"normal", "atencion", "alto"}
        assert ex["escala_min"] < ex["rango_min"] < ex["rango_max"] <= ex["escala_max"] or \
               ex["rango_min"] == 0  # colesterol usa rango 0-200
        # el marcador siempre cae dentro de la escala dibujable
        assert ex["escala_min"] <= ex["valor"] <= ex["escala_max"]


def test_demo_examenes_sin_jerga_sin_explicar():
    exs = pr._demo_examenes()
    for ex in exs:
        # las conclusiones hablan en usted/impersonal y sin siglas sueltas
        assert "Ud." not in ex["conclusion"]
        assert len(ex["conclusion"]) > 20
        assert len(ex["que_hacer"]) > 15


# ── Telemetría ────────────────────────────────────────────────────────────────

def test_eventos_whitelist():
    assert "wiz_exito" in pr._EVENTOS_PORTAL
    assert "cita_anula" in pr._EVENTOS_PORTAL
    assert "checkin" in pr._EVENTOS_PORTAL
    # nada de eventos arbitrarios
    assert "drop_table" not in pr._EVENTOS_PORTAL


def test_link_rate_limiter():
    key = f"test:{time.time()}"
    for _ in range(3):
        assert pr._link_rate_ok(key, 3, 60) is True
    assert pr._link_rate_ok(key, 3, 60) is False


# ── Demo: cita de HOY presente (para el check-in de la demo) ─────────────────

def test_demo_tiene_cita_hoy():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    hoy = datetime.now(ZoneInfo("America/Santiago")).date().strftime("%Y-%m-%d")
    data = pr._demo_data()
    fechas = [c["fecha"] for c in data["citas_futuras"]]
    assert hoy in fechas, "la demo debe tener una cita HOY para mostrar el check-in"


def test_demo_perfil_incluye_phone():
    assert pr.DEMO_PHONE == "56900000000"


# ── Puente de exámenes reales (estructurar → revisar → publicar) ─────────────

def _mini_client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(pr.router)
    return TestClient(app)


def test_examenes_admin_gate():
    c = _mini_client()
    assert c.get("/portal/examenes-admin").status_code == 401
    assert c.get("/portal/api/examenes/lista").status_code == 401
    from config import ADMIN_TOKEN
    assert c.get(f"/portal/examenes-admin?token={ADMIN_TOKEN}").status_code == 200


def test_examenes_guardar_publicar_ciclo():
    from config import ADMIN_TOKEN
    c = _mini_client()
    ex = {"nombre": "Prueba pytest (glicemia)", "fecha": "2026-07-01", "valor": 95,
          "unidad": "mg/dL", "rango_min": 70, "rango_max": 100, "escala_min": 40,
          "escala_max": 200, "nivel": "normal", "etiqueta": "Normal",
          "conclusion": "Su azúcar está dentro de lo normal.",
          "que_hacer": "Nada que hacer: siga con sus controles."}
    r = c.post(f"/portal/api/examenes/guardar?token={ADMIN_TOKEN}",
               json={"rut": "50000000-7", "examenes": [ex], "publicar": False})
    assert r.status_code == 200 and r.json()["guardados"] == 1
    r2 = c.get(f"/portal/api/examenes/lista?token={ADMIN_TOKEN}&rut=50000000-7")
    filas = [e for e in r2.json()["examenes"] if e["nombre"].startswith("Prueba pytest")]
    assert filas and filas[0]["publicado"] == 0
    ids = [f["id"] for f in filas]
    r3 = c.post(f"/portal/api/examenes/publicar?token={ADMIN_TOKEN}",
                json={"ids": ids, "accion": "publicar"})
    assert r3.status_code == 200
    # limpieza
    c.post(f"/portal/api/examenes/publicar?token={ADMIN_TOKEN}",
           json={"ids": ids, "accion": "eliminar"})


def test_examenes_guardar_rut_invalido():
    from config import ADMIN_TOKEN
    c = _mini_client()
    r = c.post(f"/portal/api/examenes/guardar?token={ADMIN_TOKEN}",
               json={"rut": "11111111-9", "examenes": [{"valor": 1}], "publicar": False})
    assert r.status_code == 400
