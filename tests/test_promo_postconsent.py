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
    """Por defecto los tests no consultan Medilink: cero pagos ni atendidos."""
    import promo_postconsent as pp

    async def _vacio(buffer_min=0):
        return set()

    monkeypatch.setattr(pp, "_pagos_hoy_pids", _vacio)
    monkeypatch.setattr(pp, "_atendidos_hoy_pids", _vacio)


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

    async def _pagos(buffer_min=0):
        return {101}  # solo el primero pagó (= se atendió) hoy

    monkeypatch.setattr(pp, "_pagos_hoy_pids", _pagos)

    async def _sin_atendidos(buffer_min=0):
        return set()

    monkeypatch.setattr(pp, "_atendidos_hoy_pids", _sin_atendidos)

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

    async def _pagos(buffer_min=0):
        return {101}

    monkeypatch.setattr(pp, "_pagos_hoy_pids", _pagos)

    async def _sin_atendidos(buffer_min=0):
        return set()

    monkeypatch.setattr(pp, "_atendidos_hoy_pids", _sin_atendidos)

    out = asyncio.get_event_loop().run_until_complete(
        pp.job_promo_postconsent(dry_run=True))
    assert out["candidatos"] == 0
    assert out["pospuestos_por_silencio"] == 1


def test_cita_atendida_gatilla(sqlite_db, monkeypatch):
    """Señal PRIMARIA: cita marcada 'Atendido' en el panel Medilink → promo,
    aunque el pago no aparezca (o no exista, ej. convenio)."""
    import promo_postconsent as pp
    import winback as winback_mod

    monkeypatch.setattr(pp, "_candidatos_bi",
                        lambda cfg: [_cand("56911112222", pids=[101]),
                                     _cand("56955556666", pids=[202])])
    monkeypatch.setattr(winback_mod, "phone_in_opt_out", lambda p: False)

    async def _sin_pagos(buffer_min=0):
        return set()

    async def _atendidos(buffer_min=0):
        return {101}  # solo el primero está marcado Atendido

    monkeypatch.setattr(pp, "_pagos_hoy_pids", _sin_pagos)
    monkeypatch.setattr(pp, "_atendidos_hoy_pids", _atendidos)

    out = asyncio.get_event_loop().run_until_complete(
        pp.job_promo_postconsent(dry_run=True))
    assert out["candidatos"] == 1
    assert out["esperando_atencion"] == 1
    assert out["muestra"] == ["56911***22"]


def test_atendido_buffer_recien_marcada_no_cuenta(monkeypatch):
    """Cita marcada Atendido hace 5 min → puede seguir en recepción → espera.
    Marcada hace 1 hora → cuenta."""
    import promo_postconsent as pp
    import medilink as medilink_mod
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    now_cl = datetime.now(ZoneInfo("America/Santiago")).replace(tzinfo=None)
    fmt = "%Y-%m-%d %H:%M:%S"
    citas = [
        {"id_paciente": 101, "estado_cita": "Atendido",
         "fecha_actualizacion": (now_cl - timedelta(minutes=5)).strftime(fmt)},
        {"id_paciente": 202, "estado_cita": "Atendido",
         "fecha_actualizacion": (now_cl - timedelta(hours=1)).strftime(fmt)},
        {"id_paciente": 303, "estado_cita": "No asiste",
         "fecha_actualizacion": (now_cl - timedelta(hours=2)).strftime(fmt)},
    ]

    class _FakeResp:
        status_code = 200

    class _FakeCli:
        async def get(self, url, **kw):
            return _FakeResp()

    monkeypatch.setattr(medilink_mod, "_get_shared_client", lambda: _FakeCli())
    monkeypatch.setattr(medilink_mod, "_safe_json", lambda r: {"data": citas})

    pids = asyncio.get_event_loop().run_until_complete(pp._atendidos_hoy_pids(20))
    assert pids == {202}  # la recién marcada espera; "No asiste" jamás entra
    pids = asyncio.get_event_loop().run_until_complete(pp._atendidos_hoy_pids(0))
    assert pids == {101, 202}


def test_buffer_pago_reciente_no_cuenta(monkeypatch):
    """El pago se hace AL LLEGAR: un pago de hace 10 min = paciente en el box
    → no cuenta. Uno de hace 2 horas = ya salió → cuenta."""
    import promo_postconsent as pp
    import bi_sync as bi_sync_mod
    import medilink as medilink_mod
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    now_cl = datetime.now(ZoneInfo("America/Santiago")).replace(tzinfo=None)
    fmt = "%Y-%m-%d %H:%M:%S"

    async def _fake_fetch(cli, fecha):
        yield [
            {"id_paciente": 101,
             "fecha_creacion": (now_cl - timedelta(minutes=10)).strftime(fmt)},
            {"id_paciente": 202,
             "fecha_creacion": (now_cl - timedelta(hours=2)).strftime(fmt)},
        ]

    monkeypatch.setattr(bi_sync_mod, "_fetch_pagos_dia", _fake_fetch)
    monkeypatch.setattr(medilink_mod, "_get_shared_client", lambda: None)

    pids = asyncio.get_event_loop().run_until_complete(pp._pagos_hoy_pids(75))
    assert pids == {202}
    # Sin buffer, cuentan los dos.
    pids = asyncio.get_event_loop().run_until_complete(pp._pagos_hoy_pids(0))
    assert pids == {101, 202}


def test_ya_enviado_no_repite(sqlite_db, monkeypatch):
    import promo_postconsent as pp
    import winback as winback_mod
    from config import DENTAL_PROMO_FLYER_TEMPLATE

    pp._registrar_envio("56911112222", DENTAL_PROMO_FLYER_TEMPLATE, "no_dental")
    monkeypatch.setattr(pp, "_candidatos_bi",
                        lambda cfg: [_cand("56911112222", pids=[101])])
    monkeypatch.setattr(winback_mod, "phone_in_opt_out", lambda p: False)

    async def _pagos(buffer_min=0):
        return {101}

    monkeypatch.setattr(pp, "_pagos_hoy_pids", _pagos)

    async def _sin_atendidos(buffer_min=0):
        return set()

    monkeypatch.setattr(pp, "_atendidos_hoy_pids", _sin_atendidos)

    out = asyncio.get_event_loop().run_until_complete(
        pp.job_promo_postconsent(dry_run=True))
    assert out["candidatos"] == 0
    assert out["ya_enviados"] == 1
