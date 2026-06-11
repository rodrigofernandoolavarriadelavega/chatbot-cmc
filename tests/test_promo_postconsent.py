"""Tests del riel promo post-consent (consent aceptado + atendido → promo dental).

Cubre: gating OFF, dedupe 1-promo-por-teléfono, gatillo de atención realizada
(pago en caja) y ventana de silencio — todo sin tocar BI, Medilink ni Meta.
"""
import asyncio
import contextlib
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import pytest


def _cand(phone, pids=None, acepto="2026-06-10"):
    return {"phone": phone, "acepto_fecha": acepto, "pids": pids or []}


@pytest.fixture()
def sqlite_db(monkeypatch, tmp_path):
    """session.db() y session._conn() apuntando a una DB temporal."""
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
    monkeypatch.setattr(session_mod, "_conn", _fake_db)
    return path


@pytest.fixture()
def sin_pagos_hoy(monkeypatch):
    """Por defecto los tests no consultan Medilink: cero pagos en vivo."""
    import promo_postconsent as pp

    async def _vacio():
        return set()

    monkeypatch.setattr(pp, "_pagos_hoy_pids", _vacio)


def test_gating_off_no_envia(monkeypatch):
    import promo_postconsent as pp

    monkeypatch.setattr(pp, "_active", lambda: False)
    out = asyncio.get_event_loop().run_until_complete(pp.job_promo_postconsent())
    assert out == {"status": "inactive"}


def test_dedupe_un_envio_por_telefono(sqlite_db):
    import promo_postconsent as pp

    pp._registrar_envio("56911112222", "tpl_x", "no_dental")
    pp._registrar_envio("56911112222", "tpl_x", "no_dental")  # repetido → ignorado
    pp._registrar_envio("56933334444", "tpl_x", "no_dental")
    assert pp._ya_enviados("tpl_x") == {"56911112222", "56933334444"}
    assert pp._ya_enviados("otro_tpl") == set()


def test_sin_atencion_no_hay_promo(sqlite_db, sin_pagos_hoy, monkeypatch):
    """Aceptó el consent pero AÚN no se atiende → queda esperando, no se envía."""
    import promo_postconsent as pp
    import winback as winback_mod

    monkeypatch.setattr(pp, "_candidatos_bi",
                        lambda cfg: [_cand("56911112222", pids=[101])])
    monkeypatch.setattr(winback_mod, "phone_in_opt_out", lambda p: False)

    out = asyncio.get_event_loop().run_until_complete(
        pp.job_promo_postconsent(dry_run=True))
    assert out["candidatos"] == 0
    assert out["esperando_atencion"] == 1


def test_pago_hoy_en_vivo_gatilla(sqlite_db, monkeypatch):
    """Pago en caja HOY (Medilink en vivo) → el paciente entra a la corrida."""
    import promo_postconsent as pp
    import winback as winback_mod

    monkeypatch.setattr(pp, "_candidatos_bi",
                        lambda cfg: [_cand("56911112222", pids=[101]),
                                     _cand("56955556666", pids=[202])])
    monkeypatch.setattr(winback_mod, "phone_in_opt_out", lambda p: False)

    async def _pagos():
        return {101}  # solo el primero pagó (= se atendió) hoy

    monkeypatch.setattr(pp, "_pagos_hoy_pids", _pagos)

    out = asyncio.get_event_loop().run_until_complete(
        pp.job_promo_postconsent(dry_run=True))
    assert out["candidatos"] == 1
    assert out["esperando_atencion"] == 1
    assert out["muestra"] == ["56911***22"]


def test_pago_local_catch_up(sqlite_db, sin_pagos_hoy, monkeypatch):
    """Atención de un día previo registrada en bi_pagos_caja local → gatilla."""
    import promo_postconsent as pp
    import winback as winback_mod

    conn = sqlite3.connect(sqlite_db)
    conn.execute("CREATE TABLE bi_pagos_caja (id_paciente INTEGER, fecha TEXT, monto INTEGER)")
    conn.execute("INSERT INTO bi_pagos_caja VALUES (303, '2026-06-11', 15000)")
    conn.commit(); conn.close()

    monkeypatch.setattr(pp, "_candidatos_bi",
                        lambda cfg: [_cand("56977778888", pids=[303], acepto="2026-06-10")])
    monkeypatch.setattr(winback_mod, "phone_in_opt_out", lambda p: False)

    out = asyncio.get_event_loop().run_until_complete(
        pp.job_promo_postconsent(dry_run=True))
    assert out["candidatos"] == 1


def test_ventana_silencio_pospone(sqlite_db, monkeypatch):
    """Atendido, pero le mandamos algo hace <2h → se pospone a la próxima corrida."""
    import promo_postconsent as pp
    import winback as winback_mod

    conn = sqlite3.connect(sqlite_db)
    conn.execute("CREATE TABLE messages (phone TEXT, direction TEXT, ts TEXT)")
    conn.execute("INSERT INTO messages VALUES ('56911112222', 'out', datetime('now', '-30 minutes'))")
    conn.commit(); conn.close()

    monkeypatch.setattr(pp, "_candidatos_bi",
                        lambda cfg: [_cand("56911112222", pids=[101])])
    monkeypatch.setattr(winback_mod, "phone_in_opt_out", lambda p: False)

    async def _pagos():
        return {101}

    monkeypatch.setattr(pp, "_pagos_hoy_pids", _pagos)

    out = asyncio.get_event_loop().run_until_complete(
        pp.job_promo_postconsent(dry_run=True))
    assert out["candidatos"] == 0
    assert out["pospuestos_por_silencio"] == 1


def test_ya_enviado_no_repite(sqlite_db, monkeypatch):
    import promo_postconsent as pp
    import winback as winback_mod
    from config import DENTAL_PROMO_FLYER_TEMPLATE

    pp._registrar_envio("56911112222", DENTAL_PROMO_FLYER_TEMPLATE, "no_dental")
    monkeypatch.setattr(pp, "_candidatos_bi",
                        lambda cfg: [_cand("56911112222", pids=[101])])
    monkeypatch.setattr(winback_mod, "phone_in_opt_out", lambda p: False)

    async def _pagos():
        return {101}

    monkeypatch.setattr(pp, "_pagos_hoy_pids", _pagos)

    out = asyncio.get_event_loop().run_until_complete(
        pp.job_promo_postconsent(dry_run=True))
    assert out["candidatos"] == 0
    assert out["ya_enviados"] == 1
