"""
"Sí" al consentimiento v1 → oferta de limpieza dental con hora real (2026-10-07).

v1 cubre "novedades del centro" → se puede ofrecer limpieza ($30.000) tras el
"Sí". v2 (solo avisos de controles) NO recibe la oferta. La oferta deja la
sesión en WAIT_SLOT con slot_sugerido → "Sí, agendar" (confirmar_sugerido)
reserva directo, y la cita lleva la marca [LIMPIEZA DENTAL $30.000 …].

Correr: venv/bin/python -m pytest tests/test_consent_v1_oferta_limpieza_2026_10_07.py -q
"""
import asyncio
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

TMP_DB = Path(tempfile.mkdtemp(prefix="cmc_test_limp_")) / "test_sessions.db"
os.environ["SESSIONS_DB"] = str(TMP_DB)
os.environ.setdefault("SQLCIPHER_KEY", "")

try:
    import winback  # noqa: F401
except Exception:
    if "winback" not in sys.modules:
        _fw = types.ModuleType("winback")
        _fw.is_template_approved = None
        _fw.phone_in_opt_out = lambda phone: False
        _fw.has_marketing_consent = lambda phone: True
        sys.modules["winback"] = _fw
    import winback  # noqa: F401

import session  # noqa: E402
session.DB_PATH = TMP_DB

import messaging as _msg  # noqa: E402
_msg.send_whatsapp = AsyncMock(return_value="wamid.TEST")

import consent_marketing  # noqa: E402
import flows  # noqa: E402
import jobs  # noqa: E402

_ADR_REAL = flows._atencion_dental_reciente

PHONE = "56933334444"
SLOT = {"fecha": "2026-10-09", "fecha_display": "jueves 9 de octubre",
        "hora_inicio": "10:30:00", "hora_fin": "11:30:00",
        "id_profesional": 55, "profesional": "Dra. Javiera Burgos"}


@pytest.fixture
def entorno(monkeypatch):
    with session.db() as conn:
        for t in ("citas_bot", "sessions", "messages", "conversation_events"):
            conn.execute(f"DELETE FROM {t}")
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
    monkeypatch.setattr(flows, "buscar_primer_dia",
                        AsyncMock(return_value=([SLOT], [SLOT])))
    monkeypatch.setattr(flows, "_atencion_dental_reciente", lambda p, dias=180: "no")
    return winback_enviado


def _plantilla(version: str):
    session.log_message(PHONE, "out", f"[template: consent_marketing_{version}]\nHola…", "IDLE")


def _si(state="IDLE"):
    async def _go():
        r = await flows._responder_consent_marketing(PHONE, True, "Sí, acepto",
                                                     state=state, data={})
        await asyncio.sleep(0)
        return r
    return asyncio.run(_go())


def _ids(resp):
    return [b["reply"]["id"] for b in resp["interactive"]["action"]["buttons"]]


def test_jobs_usan_consent_v1():
    assert jobs.CONSENT_MARKETING_TEMPLATE == "consent_marketing_v1"
    src = (ROOT / "app" / "jobs.py").read_text(encoding="utf-8")
    assert 'send_whatsapp_template(teln, "consent_marketing_v2"' not in src


def test_si_a_v1_ofrece_limpieza_con_hora_real(entorno):
    _plantilla("v1")
    resp = _si()
    body = resp["interactive"]["body"]["text"]
    assert "Limpieza dental: $30.000" in body
    assert "jueves 9 de octubre" in body and "10:30" in body and "Burgos" in body
    assert _ids(resp) == ["confirmar_sugerido", "ver_otros", "xlimpieza_no"]
    sess = session.get_session(PHONE)
    assert sess["state"] == "WAIT_SLOT"
    assert sess["data"]["slot_sugerido"]["id_profesional"] == 55
    assert sess["data"]["obs_prestacion"].startswith("[LIMPIEZA DENTAL $30.000")
    assert entorno == []  # no manda además el win-back genérico


def test_si_a_v2_no_ofrece_promo(entorno):
    _plantilla("v2")
    resp = _si()
    assert resp is None and len(entorno) == 1  # sigue el win-back de siempre


def test_si_en_medio_de_otro_flujo_no_lo_saca(entorno):
    _plantilla("v1")
    resp = _si(state="WAIT_RUT_AGENDAR")
    assert not isinstance(resp, dict)
    assert session.get_session(PHONE)["state"] != "WAIT_SLOT"


def test_sin_horas_ofrece_buscar(entorno, monkeypatch):
    monkeypatch.setattr(flows, "buscar_primer_dia", AsyncMock(return_value=([], [])))
    _plantilla("v1")
    resp = _si()
    assert _ids(resp) == ["xlimpieza_si", "xlimpieza_no"]


def test_ahora_no_cierra_y_registra(entorno):
    _plantilla("v1")
    _si()
    resp = asyncio.run(flows.handle_message(PHONE, "xlimpieza_no",
                                            session.get_session(PHONE)))
    assert "Sin problema" in str(resp)
    assert session.get_session(PHONE)["state"] == "IDLE"
    with session.db() as conn:
        evs = [r[0] for r in conn.execute(
            "SELECT event FROM conversation_events WHERE phone=?", (PHONE,))]
    assert "consent_oferta_limpieza_rechazo" in evs


# ── Limpieza reciente (2026-10-07) ──────────────────────────────────────────

def test_limpieza_al_dia_en_cmc_ofrece_blanqueamiento_y_ortodoncia(entorno, monkeypatch):
    monkeypatch.setattr(flows, "_atencion_dental_reciente", lambda p, dias=180: "cmc")
    monkeypatch.setattr(flows, "_ortodoncia_activa", lambda p, dias=90: False)
    _plantilla("v1")
    resp = _si()
    body = resp["interactive"]["body"]["text"]
    assert "Blanqueamiento dental: $75.000" in body and "Ortodoncia" in body
    assert _ids(resp) == ["xblanq_si", "xortoeval_si", "xoferta_no"]


def test_en_ortodoncia_no_le_ofrece_ortodoncia(entorno, monkeypatch):
    monkeypatch.setattr(flows, "_atencion_dental_reciente", lambda p, dias=180: "cmc")
    monkeypatch.setattr(flows, "_ortodoncia_activa", lambda p, dias=90: True)
    _plantilla("v1")
    assert _ids(_si()) == ["xblanq_si", "xoferta_no"]


def test_blanqueamiento_ofrece_hora_real_con_marca(entorno):
    resp = asyncio.run(flows.handle_message(PHONE, "xblanq_si", {"state": "IDLE", "data": {}}))
    assert "$75.000" in resp["interactive"]["body"]["text"]
    assert _ids(resp)[0] == "confirmar_sugerido"
    d = session.get_session(PHONE)["data"]
    assert d["obs_prestacion"].startswith("[BLANQUEAMIENTO $75.000")


def test_ya_me_la_hice_cierra_y_no_reofrece(entorno, monkeypatch):
    monkeypatch.setattr(flows, "_atencion_dental_reciente", _ADR_REAL)
    import winback as _wb

    class _C:  # BI sin atenciones dentales
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def cursor(self): return self
        def execute(self, *a): pass
        def fetchone(self): return None
    monkeypatch.setattr(_wb, "bi_conn", lambda: _C(), raising=False)
    _plantilla("v1")
    assert isinstance(_si(), dict)  # primera vez: oferta
    resp = asyncio.run(flows.handle_message(
        PHONE, "ya me la hice hace poco en Arauco", session.get_session(PHONE)))
    body = resp["interactive"]["body"]["text"]
    assert "Bien ahí" in body and "Toxina botulínica" in body and "Ácido hialurónico" in body
    assert "$159.990" in body and "se descuenta" in body
    assert _ids(resp) == ["xestfacial_si", "xestfacial_no"]
    assert session.get_session(PHONE)["state"] == "IDLE"
    # si vuelve a aceptar (otra plantilla v1), ya no se le ofrece
    _plantilla("v1")
    assert isinstance(_si(), str)


def test_bi_caido_no_ofrece(entorno, monkeypatch):
    monkeypatch.setattr(flows, "_atencion_dental_reciente", _ADR_REAL)
    import winback as _wb

    def _boom():
        raise RuntimeError("BI caído")
    monkeypatch.setattr(_wb, "bi_conn", _boom, raising=False)
    _plantilla("v1")
    assert isinstance(_si(), str)


def test_me_interesa_estetica_agenda_con_marca(entorno, monkeypatch):
    ini = AsyncMock(return_value="OK")
    monkeypatch.setattr(flows, "_iniciar_agendar", ini)
    asyncio.run(flows.handle_message(PHONE, "xestfacial_si", {"state": "IDLE", "data": {}}))
    assert ini.await_args.args[2] == "estética facial"
    assert ini.await_args.args[1]["obs_prestacion_esp"] == "estética"
