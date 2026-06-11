"""Tests del riel de preparación pre-examen eco (sin tocar Medilink ni Meta)."""
import asyncio
import contextlib
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import pytest


@pytest.fixture()
def sqlite_db(monkeypatch, tmp_path):
    import session as session_mod

    path = tmp_path / "test_sessions.db"

    @contextlib.contextmanager
    def _fake_db():
        conn = sqlite3.connect(path)
        try:
            yield conn
        finally:
            conn.close()

    monkeypatch.setattr(session_mod, "db", _fake_db)
    return path


def test_gating_off(monkeypatch):
    import eco_prep as ep

    monkeypatch.setattr(ep, "_active", lambda: False)
    out = asyncio.get_event_loop().run_until_complete(ep.job_eco_prep())
    assert out == {"status": "inactive"}


def test_dedupe_por_cita(sqlite_db):
    import eco_prep as ep

    ep._registrar_envio("9001", "56911112222", "2026-06-12")
    ep._registrar_envio("9001", "56911112222", "2026-06-12")  # repetida → ignorada
    ep._registrar_envio("9002", "56933334444", "2026-06-12")
    assert ep._citas_ya_enviadas() == {"9001", "9002"}


def test_fecha_legible():
    import eco_prep as ep

    assert ep._fecha_legible("2026-06-12", "10:30") == "viernes 12/06 a las 10:30"
    assert ep._fecha_legible("2026-06-12", "") == "viernes 12/06"


def test_dryrun_filtra_enviadas_y_solo_pardo(sqlite_db, monkeypatch):
    import eco_prep as ep

    ep._registrar_envio("111", "56900000001", "2026-06-11")

    async def _citas(dias):
        return [
            {"id_cita": "111", "id_paciente": 1, "_fecha": "2026-06-11",
             "hora_inicio": "09:00"},  # ya enviada → fuera
            {"id_cita": "222", "id_paciente": 2, "_fecha": "2026-06-11",
             "hora_inicio": "10:30"},  # nueva → entra
        ]

    monkeypatch.setattr(ep, "_citas_eco_ventana", _citas)

    class _FakeResp:
        status_code = 200

    class _FakeCli:
        async def get(self, url, **kw):
            return _FakeResp()

    import medilink as medilink_mod
    monkeypatch.setattr(medilink_mod, "_get_shared_client", lambda: _FakeCli())
    monkeypatch.setattr(medilink_mod, "_safe_json",
                        lambda r: {"data": {"nombre": "Ana María",
                                            "celular": "+56 9 5555 6666"}})

    out = asyncio.get_event_loop().run_until_complete(ep.job_eco_prep(dry_run=True))
    assert out["candidatos"] == 1
    assert out["ya_enviadas"] == 1
    assert out["muestra"][0]["nombre"] == "Ana"
    assert "***" in out["muestra"][0]["phone"]
    assert out["muestra"][0]["cuando"] == "jueves 11/06 a las 10:30"
