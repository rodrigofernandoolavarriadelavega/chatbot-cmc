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
