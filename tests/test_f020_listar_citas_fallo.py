"""
F020 — listar_citas_paciente no debe confundir 'Medilink falló' con 'sin citas'.

Antes del fix: la rama RUT devolvía [] en silencio total ante HTTP no-200
(400/401/403, p.ej. token muerto) y ante httpx.RequestError tras reintentos
(429 sostenido / red) — indistinguible de 'paciente sin citas'. El bot
respondía "No tienes citas futuras agendadas" con Medilink caído, y el
bloqueo anti-citas-duplicadas de CONFIRMING_CITA quedaba bypasseado.

Fix: kwarg raise_on_error=True levanta MedilinkUnavailable / re-levanta
httpx.RequestError. El default (False) mantiene el contrato histórico [].

Ejecución:
    venv/bin/python -m pytest tests/test_f020_listar_citas_fallo.py -q
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT))

import medilink  # noqa: E402


class FakeResp:
    def __init__(self, status_code: int, payload: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload


def _patch_get(monkeypatch, resp=None, exc=None):
    async def fake_get(client, url, **kwargs):
        if exc is not None:
            raise exc
        return resp
    monkeypatch.setattr(medilink, "_get", fake_get)


def test_http_400_default_mantiene_contrato_lista_vacia(monkeypatch):
    _patch_get(monkeypatch, resp=FakeResp(400, text="Bad Request"))
    out = asyncio.run(medilink.listar_citas_paciente(0, rut="11111111-1"))
    assert out == []


def test_http_401_raise_on_error_levanta_medilink_unavailable(monkeypatch):
    _patch_get(monkeypatch, resp=FakeResp(401, text="Unauthorized"))
    with pytest.raises(medilink.MedilinkUnavailable):
        asyncio.run(medilink.listar_citas_paciente(
            0, rut="11111111-1", raise_on_error=True))


def test_error_red_raise_on_error_relevanta(monkeypatch):
    _patch_get(monkeypatch, exc=httpx.RequestError("Medilink no respondió tras 3 intentos"))
    with pytest.raises(httpx.RequestError):
        asyncio.run(medilink.listar_citas_paciente(
            0, rut="11111111-1", raise_on_error=True))


def test_error_red_default_mantiene_contrato_lista_vacia(monkeypatch):
    _patch_get(monkeypatch, exc=httpx.RequestError("boom"))
    out = asyncio.run(medilink.listar_citas_paciente(0, rut="11111111-1"))
    assert out == []


def test_http_200_sin_citas_devuelve_lista_vacia_sin_levantar(monkeypatch):
    _patch_get(monkeypatch, resp=FakeResp(200, payload={"data": []}))
    out = asyncio.run(medilink.listar_citas_paciente(
        0, rut="11111111-1", raise_on_error=True))
    assert out == []
