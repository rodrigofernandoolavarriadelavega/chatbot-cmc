"""
Cross-sell MG → chequeo preventivo usa el template APPROVED (2026-10-06).

Bug: el template `crosssell_mg_chequeo` estaba APPROVED en Meta pero el job
solo enviaba mensaje libre con ventana 24h abierta → el 6-oct se saltaron
934 de ~960 candidatos (`template_skip_no_aprobado`) y salieron 18.

Además, el botón del template llega al webhook como TEXTO ("Sí, agendar
control") y el mapa de cross-sell traducía a `xmgcheck_si/no`, ids que
ningún handler escucha (el handler es `xchequeo_si/no`).

Mismo bug en crosssell_odonto_estetica. Y crosssell_post_dental_ortodoncia
nunca envió nada: filtraba especialidad en minúsculas ('odontología general')
y citas_bot guarda 'Odontología General' → 0 candidatos; sus botones además
no tenían handler.

Correr: venv/bin/python -m pytest tests/test_crosssell_templates_2026_10_06.py -q
"""
import asyncio
import json

import pytest
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


# ── Estética: mismo patrón de template ──────────────────────────────────────

def test_estetica_ventana_cerrada_envia_template(monkeypatch):
    tpl_fn, free_fn, sent_tpl, sent_free, saved, events = _setup(monkeypatch)
    monkeypatch.setattr(fidelizacion, "get_crosssell_odonto_estetica_candidatos",
                        lambda: [{"phone": "56911111111", "nombre": "Juana Pérez"}])
    asyncio.run(fidelizacion.enviar_crosssell_odonto_estetica(free_fn, send_template_fn=tpl_fn))
    assert len(sent_tpl) == 1 and sent_free == []
    assert sent_tpl[0]["template"] == "crosssell_odonto_estetica"
    assert sent_tpl[0]["body_params"] == ["Juana"]
    assert saved == [("56911111111", "crosssell_odonto_estetica")]


def test_estetica_envio_fallido_no_quema_cooldown(monkeypatch):
    tpl_fn, free_fn, sent_tpl, sent_free, saved, events = _setup(monkeypatch)
    monkeypatch.setattr(fidelizacion, "get_crosssell_odonto_estetica_candidatos",
                        lambda: [{"phone": "56911111111", "nombre": "Juana Pérez"}])

    async def tpl_falla(*a, **k):
        return None

    asyncio.run(fidelizacion.enviar_crosssell_odonto_estetica(free_fn, send_template_fn=tpl_falla))
    assert saved == []
    assert ("56911111111", "template_send_failed") in events


# ── Post-dental: el filtro de especialidad ya encuentra candidatos ──────────

def test_post_dental_encuentra_odontologia_general_con_mayuscula():
    from datetime import date, timedelta
    with session.db() as conn:
        conn.execute("DELETE FROM citas_bot")
        conn.execute(
            "INSERT INTO citas_bot (phone, id_cita, especialidad, profesional, fecha, hora, paciente_nombre) "
            "VALUES (?,?,?,?,?,?,?)",
            ("56933330001", "7001", "Odontología General", "Dra. Javiera Burgos",
             (date.today() - timedelta(days=2)).isoformat(), "10:00", "Ana Soto"))
        conn.commit()
    cands = fidelizacion._get_crosssell_post_dental_candidatos()
    assert [c["phone"] for c in cands] == ["56933330001"]


# ── Botones de template (llegan como TEXTO) → handler, sin Haiku ────────────

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

_SIN_CLAUDE = AsyncMock(side_effect=AssertionError("llamó a Claude"))


def _seed(phone, tipo, destino):
    with session.db() as conn:
        conn.execute("DELETE FROM fidelizacion_msgs")
        conn.execute("DELETE FROM conversation_events")
        conn.commit()
    session.save_fidelizacion_msg(phone, tipo)
    session.set_pending_crosssell(phone, tipo, destino)


def _eventos(phone):
    with session.db() as conn:
        return [r[0] for r in conn.execute(
            "SELECT event FROM conversation_events WHERE phone=?", (phone,))]


@pytest.mark.parametrize("tipo,destino,texto,esp_agendar", [
    ("crosssell_mg_chequeo", "medicina general", "Sí, agendar control", "medicina general"),
    ("crosssell_odonto_estetica", "estética facial", "Ver horas", "estética facial"),
    ("crosssell_post_dental_ortodoncia", "ortodoncia", "Sí, agendar evaluación", "ortodoncia"),
])
def test_boton_si_inicia_agendar(tipo, destino, texto, esp_agendar):
    phone = "56900002001"
    _seed(phone, tipo, destino)
    with patch.object(flows_mod, "_iniciar_agendar",
                      new=AsyncMock(return_value="OK_AGENDAR")) as ini, \
         patch("claude_helper._claude_create", new=_SIN_CLAUDE):
        resp = asyncio.run(flows_mod.handle_message(
            phone, texto, {"state": "IDLE", "data": {}}))
    assert ini.await_count == 1, f"no inició agendar; resp={resp!r}"
    assert ini.await_args.args[2] == esp_agendar
    assert session.get_pending_crosssell(phone, hours=48) is None


@pytest.mark.parametrize("tipo,destino,texto,evento", [
    ("crosssell_mg_chequeo", "medicina general", "No por ahora", "crosssell_mg_chequeo_rechazo"),
    ("crosssell_odonto_estetica", "estética facial", "No por ahora", "crosssell_odonto_estetica_rechazo"),
    ("crosssell_post_dental_ortodoncia", "ortodoncia", "Más información", "crosssell_post_dental_ortodoncia_info"),
])
def test_otros_botones_llegan_a_su_handler(tipo, destino, texto, evento):
    phone = "56900002002"
    _seed(phone, tipo, destino)
    with patch("claude_helper._claude_create", new=_SIN_CLAUDE):
        asyncio.run(flows_mod.handle_message(phone, texto, {"state": "IDLE", "data": {}}))
    assert evento in _eventos(phone)


def test_estetica_mas_informacion_llega_al_handler():
    phone = "56900002003"
    _seed(phone, "crosssell_odonto_estetica", "estética facial")
    with patch.object(flows_mod, "respuesta_faq", new=AsyncMock(return_value=None)), \
         patch("claude_helper._claude_create", new=_SIN_CLAUDE):
        resp = asyncio.run(flows_mod.handle_message(
            phone, "Más información", {"state": "IDLE", "data": {}}))
    assert "crosssell_odonto_estetica_info" in _eventos(phone)
    assert "estética facial" in str(resp)


@pytest.mark.parametrize("tipo,archivo", [
    ("crosssell_mg_chequeo", "crosssell_mg_chequeo.json"),
    ("crosssell_odonto_estetica", "crosssell_odonto_estetica.json"),
    ("crosssell_post_dental_ortodoncia", "crosssell_ortodoncia_post_dental_v1.json"),
])
def test_cada_boton_del_template_tiene_payload_con_handler(tipo, archivo):
    """Guardia: si alguien cambia los botones del template, esto avisa."""
    import re as _re
    tpl = json.loads((ROOT / "templates" / "whatsapp_templates" / archivo).read_text(encoding="utf-8"))
    botones = next(c for c in tpl["components"] if c["type"] == "BUTTONS")["buttons"]
    src = (ROOT / "app" / "flows.py").read_text(encoding="utf-8")
    for b in botones:
        key = _re.sub(r"[^a-z ]", "", flows_mod._sin_tildes_precio(b["text"])).strip()
        payload = flows_mod._TEMPLATE_BTN_PAYLOAD[tipo].get(key)
        # "No por ahora" sin entrada propia cae al clasificador (caché → "no")
        if payload is None:
            assert key in ("no por ahora",), f"{tipo}: botón {b['text']!r} sin payload"
            continue
        assert f'if tl == "{payload}":' in src, f"{payload} no tiene handler"
