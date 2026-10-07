"""
Consentimiento de marketing a quien agendó por el bot (2026-10-07).

El barrido horario excluía las citas del bot ("el bot le pregunta") y el bot
nunca preguntaba: 61% de los nuevos agendados por el bot en sep-2026 jamás
recibió consent_marketing_v2. Además MARKETING_CONSENT_BLAST_ACTIVE estaba
apagado desde mayo porque systemd entregaba "true  # auto-set".

Correr: venv/bin/python -m pytest tests/test_consent_post_agenda_2026_10_07.py -q
"""
import asyncio
import importlib
import os
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT))

TMP_DB = Path(tempfile.mkdtemp(prefix="cmc_test_cpa_")) / "test_sessions.db"
os.environ["SESSIONS_DB"] = str(TMP_DB)
os.environ.setdefault("SQLCIPHER_KEY", "")

if "winback" not in sys.modules:
    sys.modules["winback"] = types.ModuleType("winback")

import session  # noqa: E402
session.DB_PATH = TMP_DB

import jobs  # noqa: E402
import winback  # noqa: E402
import messaging  # noqa: E402

PHONE = "56911112222"


@pytest.fixture
def entorno(monkeypatch):
    with session.db() as conn:
        for t in ("citas_bot", "sessions", "messages", "conversation_events", "contact_profiles"):
            conn.execute(f"DELETE FROM {t}")
        conn.commit()
    estado = {"consent": None, "enviados": [], "registrados": []}

    async def fake_tpl(phone, name, body_params=None, **kw):
        estado["enviados"].append((phone, name, list(body_params or [])))
        return "wamid.TEST"

    async def aprobado(name):
        return True

    def registrar(phone):
        estado["registrados"].append(phone)
        estado["consent"] = "pending"

    estado["optout"] = False
    estado["bi_caido"] = False

    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, params):
            if estado["bi_caido"]:
                raise RuntimeError("BI caído")
            self._sql = sql
        def fetchone(self):
            if "marketing_consent" in self._sql:
                return (estado["consent"],) if estado["consent"] else None
            return (1,) if estado["optout"] else None

    class _Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def cursor(self): return _Cur()

    monkeypatch.setattr(winback, "bi_conn", lambda: _Conn(), raising=False)
    monkeypatch.setattr(winback, "registrar_consent_enviado", registrar, raising=False)
    monkeypatch.setattr(winback, "is_template_approved", aprobado, raising=False)
    monkeypatch.setattr(messaging, "send_whatsapp_template", fake_tpl)
    monkeypatch.setenv("CONSENT_POST_AGENDA_ACTIVE", "true")
    # Fijar hora hábil (el job se limita a 09-21 CLT)
    import datetime as _real_dt

    class _FixedDT(_real_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return _real_dt.datetime(2026, 10, 7, 12, 0, tzinfo=tz)

    monkeypatch.setattr(_real_dt, "datetime", _FixedDT)
    return estado


def _cita(minutos_atras: int, nombre="María José"):
    with session.db() as conn:
        conn.execute(
            "INSERT INTO citas_bot (phone, id_cita, especialidad, profesional, fecha, hora, created_at) "
            "VALUES (?,?,?,?,?,?, datetime('now', ?))",
            (PHONE, "9001", "Medicina General", "Dr. X", "2026-10-09", "10:00",
             f"-{minutos_atras} minutes"))
        conn.execute("INSERT OR REPLACE INTO contact_profiles (phone, nombre) VALUES (?, ?)",
                     (PHONE, nombre))
        conn.commit()


def test_pide_consent_a_quien_agendo_por_el_bot(entorno):
    _cita(30)
    r = asyncio.run(jobs._job_consent_post_agenda())
    assert r["enviados"] == 1
    assert entorno["enviados"] == [(PHONE, "consent_marketing_v2", ["María"])]
    assert entorno["registrados"] == [PHONE]
    with session.db() as conn:
        txt = conn.execute("SELECT text FROM messages WHERE phone=? AND direction='out'",
                           (PHONE,)).fetchone()[0]
    # consent_marketing.detectar reconoce la respuesta por este prefijo
    assert txt.startswith("[template: consent_marketing_v2]")


def test_no_repite_si_ya_se_le_pidio(entorno):
    _cita(30)
    asyncio.run(jobs._job_consent_post_agenda())
    asyncio.run(jobs._job_consent_post_agenda())
    assert len(entorno["enviados"]) == 1


def test_no_en_rafaga_con_la_confirmacion(entorno):
    _cita(3)  # recién agendó: espera ≥10 min
    asyncio.run(jobs._job_consent_post_agenda())
    assert entorno["enviados"] == []


def test_no_interrumpe_un_flujo_activo(entorno):
    _cita(30)
    session.save_session(PHONE, "WAIT_REFERRAL_POST", {})
    asyncio.run(jobs._job_consent_post_agenda())
    assert entorno["enviados"] == []


def test_respeta_respuesta_previa(entorno):
    _cita(30)
    entorno["consent"] = "declined"
    asyncio.run(jobs._job_consent_post_agenda())
    assert entorno["enviados"] == []


def test_con_opt_out_no_envia(entorno):
    _cita(30)
    entorno["optout"] = True
    asyncio.run(jobs._job_consent_post_agenda())
    assert entorno["enviados"] == []


def test_bi_caido_no_envia_fail_closed(entorno):
    """Si BI falla no se puede saber si dijo que no → NO se envía."""
    _cita(30)
    entorno["bi_caido"] = True
    asyncio.run(jobs._job_consent_post_agenda())
    assert entorno["enviados"] == [] and entorno["registrados"] == []


def test_apagado_por_flag(entorno, monkeypatch):
    _cita(30)
    monkeypatch.setenv("CONSENT_POST_AGENDA_ACTIVE", "false")
    asyncio.run(jobs._job_consent_post_agenda())
    assert entorno["enviados"] == []


# ── config: comentario inline de systemd ────────────────────────────────────

def test_config_quita_comentario_inline_de_flags(monkeypatch):
    monkeypatch.setenv("MARKETING_CONSENT_BLAST_ACTIVE", "true  # auto-set")
    monkeypatch.setenv("ALGUN_CAP", "30 # tope")
    monkeypatch.setenv("UN_SECRETO", "abc  # no-es-flag")
    import config
    importlib.reload(config)
    assert os.environ["MARKETING_CONSENT_BLAST_ACTIVE"] == "true"
    assert os.environ["ALGUN_CAP"] == "30"
    assert os.environ["UN_SECRETO"] == "abc  # no-es-flag"
