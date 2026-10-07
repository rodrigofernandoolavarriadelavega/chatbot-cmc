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

# Mismo shim que los otros tests de cross-sell/consent: si este archivo se
# importa primero, los demás reutilizan el módulo y esperan estos atributos.
try:
    import winback as _real_winback  # noqa: F401 — si importa, los tests lo parchean por atributo
except Exception:
    _real_winback = None
if _real_winback is None and "winback" not in sys.modules:
    _fake_winback = types.ModuleType("winback")
    _fake_winback.is_template_approved = None
    _fake_winback.phone_in_opt_out = lambda phone: False
    _fake_winback.has_marketing_consent = lambda phone: True
    sys.modules["winback"] = _fake_winback

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
    assert entorno["enviados"] == [(PHONE, "consent_marketing_v1", ["María"])]
    assert entorno["registrados"] == [PHONE]
    with session.db() as conn:
        txt = conn.execute("SELECT text FROM messages WHERE phone=? AND direction='out'",
                           (PHONE,)).fetchone()[0]
    # consent_marketing.detectar reconoce la respuesta por este prefijo
    assert txt.startswith("[template: consent_marketing_v1]")


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


# ── Al responder "Sí": con cita próxima NO se manda el win-back ─────────────

def _respuesta_si(monkeypatch, con_cita_futura: bool):
    import flows
    import consent_marketing
    with session.db() as conn:
        conn.execute("DELETE FROM citas_bot")
        if con_cita_futura:
            conn.execute(
                "INSERT INTO citas_bot (phone, id_cita, especialidad, profesional, fecha, hora) "
                "VALUES (?,?,?,?, date('now','+3 days'), '10:00')",
                (PHONE, "9002", "Medicina General", "Dr. X"))
        conn.commit()
    winback_enviado = []

    async def fake_winback(cand, prefer_session=True):
        winback_enviado.append(cand)
        return True

    monkeypatch.setattr(consent_marketing, "registrar", lambda *a, **k: None)
    for k, v in {"WINBACK_ACTIVE": True,
                 "get_candidato_por_phone": lambda p: {"ultima_especialidad": "medicina general"},
                 "ya_enviado_winback_hoy": lambda p: False,
                 "send_winback_smart": fake_winback,
                 "_especialidad_sin_profesional": lambda e: False}.items():
        monkeypatch.setattr(winback, k, v, raising=False)

    async def _go():
        r = await flows._responder_consent_marketing(PHONE, True, "Sí, actívenlos")
        await asyncio.sleep(0)  # deja correr la task del win-back si se creó
        return r

    return asyncio.run(_go()), winback_enviado


def test_si_con_cita_proxima_no_manda_winback(monkeypatch):
    resp, wb = _respuesta_si(monkeypatch, con_cita_futura=True)
    assert wb == []
    assert "quedó activado" in resp


def test_si_sin_cita_proxima_mantiene_winback(monkeypatch):
    resp, wb = _respuesta_si(monkeypatch, con_cita_futura=False)
    assert resp is None and len(wb) == 1



# ── También lista de espera y avisos de horas liberadas (2026-10-07) ────────

def test_pide_consent_a_quien_quedo_en_lista_de_espera(entorno):
    with session.db() as conn:
        conn.execute("DELETE FROM waitlist")
        conn.execute("INSERT INTO waitlist (phone, nombre, especialidad, created_at) "
                     "VALUES (?, 'Rosa Pérez', 'cardiología', datetime('now','-30 minutes'))",
                     (PHONE,))
        conn.commit()
    asyncio.run(jobs._job_consent_post_agenda())
    with session.db() as conn:
        conn.execute("DELETE FROM waitlist"); conn.commit()
    assert entorno["enviados"] == [(PHONE, "consent_marketing_v1", ["Rosa"])]


def test_pide_consent_tras_aviso_de_horas_liberadas(entorno):
    import time
    with session.db() as conn:
        conn.execute("DELETE FROM horas_vacias_envios")
        conn.execute("INSERT INTO horas_vacias_envios (phone, especialidad, profesional_id, "
                     "fecha_slot, hora_slot, enviado_ts) VALUES (?, 'medicina general', 73, "
                     "'2026-10-08', '09:00', ?)", (PHONE, int(time.time()) - 1800))
        conn.commit()
    asyncio.run(jobs._job_consent_post_agenda())
    with session.db() as conn:
        conn.execute("DELETE FROM horas_vacias_envios"); conn.commit()
    assert len(entorno["enviados"]) == 1


# ── Consentimiento inmediato tras agendar / lista de espera ─────────────────

def test_wrapper_lista_espera_no_es_recursivo(monkeypatch):
    import flows
    llamadas = []
    monkeypatch.setattr(flows, "add_to_waitlist", lambda *a, **k: llamadas.append(a) or 7)
    monkeypatch.setattr(flows, "_consent_tras_agendar", lambda p, o, nombre="": llamadas.append(("consent", p, o)))
    assert flows._add_waitlist_consent(PHONE, "1-9", "Rosa", "cardiología", None) == 7
    assert llamadas[-1] == ("consent", PHONE, "lista_espera")


def test_pedir_si_corresponde_fail_closed(monkeypatch):
    import consent_marketing
    enviados = []

    async def fake_tpl(phone, name, body_params=None, **k):
        enviados.append(phone)

    monkeypatch.setattr(messaging, "send_whatsapp_template", fake_tpl)

    def boom(_):
        raise RuntimeError("BI caído")
    monkeypatch.setattr(consent_marketing, "estado_bi", boom)
    assert asyncio.run(consent_marketing.pedir_si_corresponde(PHONE, "Ana", "cita")) is False
    monkeypatch.setattr(consent_marketing, "estado_bi", lambda p: ("declined", False))
    assert asyncio.run(consent_marketing.pedir_si_corresponde(PHONE, "Ana", "cita")) is False
    assert enviados == []


def test_consent_tras_agendar_espera_idle_y_envia(monkeypatch):
    import flows, resilience, consent_marketing
    tareas, pedidos = [], []
    monkeypatch.setattr(resilience, "spawn_task", lambda coro: tareas.append(coro))

    async def no_sleep(s):
        session.save_session(PHONE, "IDLE", {})  # respondió "cómo nos conociste"
    monkeypatch.setattr(asyncio, "sleep", no_sleep)

    async def fake_pedir(phone, nombre, origen):
        pedidos.append((phone, origen))
        return True
    monkeypatch.setattr(consent_marketing, "pedir_si_corresponde", fake_pedir)
    session.save_session(PHONE, "WAIT_REFERRAL_POST", {})
    flows._consent_tras_agendar(PHONE, "cita", nombre="Ana")
    assert len(tareas) == 1
    asyncio.run(tareas[0])
    assert pedidos == [(PHONE, "cita")]
