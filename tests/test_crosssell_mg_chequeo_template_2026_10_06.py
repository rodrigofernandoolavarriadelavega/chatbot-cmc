"""
Cross-sell MG → chequeo preventivo usa el template APPROVED (2026-10-06).

Bug: el template `crosssell_mg_chequeo` estaba APPROVED en Meta pero el job
solo enviaba mensaje libre con ventana 24h abierta → el 6-oct se saltaron
934 de ~960 candidatos (`template_skip_no_aprobado`) y salieron 18.

Además, el botón del template llega al webhook como TEXTO ("Sí, agendar
control") y el mapa de cross-sell traducía a `xmgcheck_si/no`, ids que
ningún handler escucha (el handler es `xchequeo_si/no`).

Correr: venv/bin/python -m pytest tests/test_crosssell_mg_chequeo_template_2026_10_06.py -q
"""
import asyncio
import json
import os
import re
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT))

TMP_DB = Path(tempfile.mkdtemp(prefix="cmc_test_xmg_")) / "test_sessions.db"
os.environ["SESSIONS_DB"] = str(TMP_DB)
os.environ.setdefault("SQLCIPHER_KEY", "")

# winback usa psycopg2 (no disponible localmente): shim antes de importar.
if "winback" not in sys.modules:
    _fake_winback = types.ModuleType("winback")
    _fake_winback.is_template_approved = None
    _fake_winback.phone_in_opt_out = lambda phone: False
    sys.modules["winback"] = _fake_winback

import session  # noqa: E402
session.DB_PATH = TMP_DB

import fidelizacion  # noqa: E402
import winback  # noqa: E402

TPL = ROOT / "templates" / "whatsapp_templates" / "crosssell_mg_chequeo.json"


def _tpl():
    return json.loads(TPL.read_text(encoding="utf-8"))


def _setup(monkeypatch, window_open=False, template_aprobado=True):
    sent_tpl, sent_free, saved, events = [], [], [], []

    async def fake_tpl(phone, template_name, body_params=None,
                       button_payloads=None, **kw):
        sent_tpl.append({"phone": phone, "template": template_name,
                         "body_params": list(body_params or []),
                         "button_payloads": list(button_payloads or [])})
        return "wamid.TEST"

    async def fake_free(phone, msg):
        sent_free.append((phone, msg))

    async def fake_is_approved(name):
        return template_aprobado

    monkeypatch.setattr(fidelizacion, "get_crosssell_mg_chequeo_candidatos",
                        lambda: [{"phone": "56911111111", "nombre": "Juana Pérez",
                                  "fecha_nacimiento": "1970-01-01"}])
    monkeypatch.setattr(fidelizacion, "USE_TEMPLATES", True)
    monkeypatch.setattr(fidelizacion, "puede_enviar_campana",
                        lambda phone, campana, dias_cooldown=180: True)
    monkeypatch.setattr(fidelizacion, "has_privacy_consent", lambda phone: True)
    monkeypatch.setattr(fidelizacion, "is_window_open", lambda phone: window_open)
    monkeypatch.setattr(fidelizacion, "save_fidelizacion_msg",
                        lambda phone, tipo: saved.append((phone, tipo)))
    monkeypatch.setattr(fidelizacion, "set_pending_crosssell",
                        lambda phone, tipo, destino: None)
    monkeypatch.setattr(fidelizacion, "log_message", lambda *a, **k: None)
    monkeypatch.setattr(fidelizacion, "log_event",
                        lambda phone, ev, data=None: events.append((phone, ev)))
    monkeypatch.setattr(winback, "is_template_approved", fake_is_approved)
    return fake_tpl, fake_free, sent_tpl, sent_free, saved, events


def test_template_local_tiene_1_placeholder_y_2_botones():
    tpl = _tpl()
    body = next(c for c in tpl["components"] if c["type"] == "BODY")
    assert sorted(set(re.findall(r"\{\{(\d+)\}\}", body["text"]))) == ["1"]
    btns = next(c for c in tpl["components"] if c["type"] == "BUTTONS")["buttons"]
    assert [b["text"] for b in btns] == ["Sí, agendar control", "No por ahora"]


def test_ventana_cerrada_envia_template(monkeypatch):
    tpl_fn, free_fn, sent_tpl, sent_free, saved, events = _setup(monkeypatch)
    asyncio.run(fidelizacion.enviar_crosssell_mg_chequeo(free_fn, send_template_fn=tpl_fn))
    assert len(sent_tpl) == 1 and sent_free == []
    assert sent_tpl[0]["template"] == "crosssell_mg_chequeo"
    assert sent_tpl[0]["body_params"] == ["Juana"]
    assert sent_tpl[0]["button_payloads"] == ["xchequeo_si", "xchequeo_no"]
    assert saved == [("56911111111", "crosssell_mg_chequeo")]
    assert ("56911111111", "template_skip_no_aprobado") not in events


def test_ventana_abierta_sigue_con_mensaje_libre(monkeypatch):
    tpl_fn, free_fn, sent_tpl, sent_free, saved, events = _setup(monkeypatch, window_open=True)
    asyncio.run(fidelizacion.enviar_crosssell_mg_chequeo(free_fn, send_template_fn=tpl_fn))
    assert sent_tpl == [] and len(sent_free) == 1
    ids = [b["reply"]["id"] for b in sent_free[0][1]["interactive"]["action"]["buttons"]]
    assert ids == ["xchequeo_si", "xchequeo_no"]


def test_template_no_aprobado_salta_como_antes(monkeypatch):
    tpl_fn, free_fn, sent_tpl, sent_free, saved, events = _setup(
        monkeypatch, template_aprobado=False)
    asyncio.run(fidelizacion.enviar_crosssell_mg_chequeo(free_fn, send_template_fn=tpl_fn))
    assert sent_tpl == [] and sent_free == [] and saved == []
    assert ("56911111111", "template_skip_no_aprobado") in events


def test_envio_fallido_no_quema_cooldown(monkeypatch):
    tpl_fn, free_fn, sent_tpl, sent_free, saved, events = _setup(monkeypatch)

    async def tpl_falla(phone, template_name, body_params=None,
                        button_payloads=None, **kw):
        return None

    asyncio.run(fidelizacion.enviar_crosssell_mg_chequeo(free_fn, send_template_fn=tpl_falla))
    assert saved == []
    assert ("56911111111", "template_send_failed") in events


# ── Respuesta al botón del template (llega como texto) ──────────────────────

def _mock_medilink():
    import medilink as _ml
    _ml.buscar_primer_dia = AsyncMock(return_value=([], []))
    _ml.buscar_slots_dia = AsyncMock(return_value=[])
    _ml.buscar_slots_dia_por_ids = AsyncMock(return_value=[])
    _ml.buscar_paciente = AsyncMock(return_value=None)
    _ml.listar_citas_paciente = AsyncMock(return_value=[])
    _ml.consultar_proxima_fecha = AsyncMock(return_value=None)


def _mock_messaging():
    import messaging as _msg
    _msg.send_whatsapp = AsyncMock(return_value="wamid.TEST")


_mock_medilink()
_mock_messaging()
import flows as flows_mod  # noqa: E402


def _seed(phone):
    with session.db() as conn:
        conn.execute("DELETE FROM fidelizacion_msgs")
        conn.execute("DELETE FROM conversation_events")
        conn.commit()
    session.save_fidelizacion_msg(phone, "crosssell_mg_chequeo")
    session.set_pending_crosssell(phone, "crosssell_mg_chequeo", "medicina general")


def _eventos(phone):
    with session.db() as conn:
        return [r[0] for r in conn.execute(
            "SELECT event FROM conversation_events WHERE phone=?", (phone,))]


def test_boton_si_del_template_resuelve_sin_llamar_a_claude():
    phone = "56900002001"
    _seed(phone)
    with patch.object(flows_mod, "_iniciar_agendar",
                      new=AsyncMock(return_value="OK_AGENDAR")) as ini, \
         patch("claude_helper._claude_create",
               new=AsyncMock(side_effect=AssertionError("llamó a Claude"))):
        resp = asyncio.run(flows_mod.handle_message(
            phone, "Sí, agendar control", {"state": "IDLE", "data": {}}))
    assert ini.await_count == 1, f"no inició agendar; resp={resp!r}"
    assert ini.await_args.args[2] == "medicina general"


def test_boton_no_del_template_cierra_sin_llamar_a_claude():
    phone = "56900002002"
    _seed(phone)
    with patch("claude_helper._claude_create",
               new=AsyncMock(side_effect=AssertionError("llamó a Claude"))):
        resp = asyncio.run(flows_mod.handle_message(
            phone, "No por ahora", {"state": "IDLE", "data": {}}))
    assert "Sin problema" in str(resp)
    assert session.get_pending_crosssell(phone, hours=48) is None


def test_mapa_texto_libre_apunta_a_handlers_que_existen():
    # Respaldo cuando no hay pending: el mapa debe traducir a ids con handler.
    src = (ROOT / "app" / "flows.py").read_text(encoding="utf-8")
    assert '"crosssell_mg_chequeo":     ("xchequeo_si", "xchequeo_no")' in src
    assert 'if tl == "xchequeo_si":' in src and 'if tl == "xchequeo_no":' in src
    assert "xmgcheck" not in src
