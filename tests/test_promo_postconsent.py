"""Tests del riel promo post-consent (consent aceptado → promo dental diferida).

Cubre: gating OFF, dedupe 1-promo-por-teléfono, y el dry-run con pool simulado
(sin tocar BI ni Meta).
"""
import asyncio
import contextlib
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import pytest


@pytest.fixture()
def sqlite_db(monkeypatch, tmp_path):
    """session.db() apuntando a una DB temporal (no toca data/sessions.db)."""
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


def test_gating_off_no_envia(monkeypatch):
    import promo_postconsent as pp

    monkeypatch.delenv("PROMO_POSTCONSENT_ACTIVE", raising=False)
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


def test_dryrun_filtra_ya_enviados_y_capea(sqlite_db, monkeypatch):
    import promo_postconsent as pp
    import winback as winback_mod

    # Pool BI simulado: 4 aceptados; uno ya recibió la promo antes.
    pool = ["56911112222", "56955556666", "56977778888", "56999990000"]
    monkeypatch.setattr(pp, "_candidatos_bi", lambda cfg: pool)
    monkeypatch.setattr(winback_mod, "phone_in_opt_out", lambda p: False)
    monkeypatch.setenv("PROMO_POSTCONSENT_CAP", "2")

    from config import DENTAL_PROMO_FLYER_TEMPLATE
    pp._registrar_envio("56911112222", DENTAL_PROMO_FLYER_TEMPLATE, "no_dental")

    out = asyncio.get_event_loop().run_until_complete(
        pp.job_promo_postconsent(dry_run=True))
    # El ya-enviado queda fuera; el cap=2 corta los 3 restantes a 2.
    assert out["candidatos"] == 2
    assert all("***" in m for m in out["muestra"])  # teléfonos enmascarados
    assert out["pool_bruto_bi"] == 4


def test_dryrun_respeta_opt_out(sqlite_db, monkeypatch):
    import promo_postconsent as pp
    import winback as winback_mod

    monkeypatch.setattr(pp, "_candidatos_bi", lambda cfg: ["56911112222", "56955556666"])
    monkeypatch.setattr(winback_mod, "phone_in_opt_out",
                        lambda p: p == "56911112222")
    monkeypatch.delenv("PROMO_POSTCONSENT_CAP", raising=False)

    out = asyncio.get_event_loop().run_until_complete(
        pp.job_promo_postconsent(dry_run=True))
    assert out["candidatos"] == 1
