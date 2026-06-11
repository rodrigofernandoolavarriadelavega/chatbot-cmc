"""
Tests del fix F010: cross-sell post-dental -> ortodoncia.

Bug original: el template APPROVED crosssell_ortodoncia_post_dental_v1 tiene
DOS placeholders ({{1}} nombre, {{2}} apellido Dra.) pero el codigo enviaba
body_params con UN solo valor -> Meta rechazaba con 132000 (param mismatch),
_post_meta no reintenta 4xx, y el cooldown de 180 dias ya estaba quemado.

Correr: venv/bin/python -m pytest tests/test_crosssell_post_dental_template.py -q
"""
import asyncio
import json
import os
import re
import sys
import types
import unittest.mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))

# winback usa psycopg2 (no disponible localmente). Shimeamos el módulo completo
# antes de cualquier import que lo jalé transitivamente.
if "winback" not in sys.modules:
    _fake_winback = types.ModuleType("winback")
    _fake_winback.is_template_approved = None  # se parcheará por test
    sys.modules["winback"] = _fake_winback

import fidelizacion  # noqa: E402

# Referencia al módulo shim para monkeypatch
import winback  # noqa: E402


TPL_V1 = os.path.join(ROOT, "templates", "whatsapp_templates",
                      "crosssell_ortodoncia_post_dental_v1.json")
TPL_V2 = os.path.join(ROOT, "templates", "whatsapp_templates",
                      "crosssell_ortodoncia_post_dental_v2.json")


def _placeholders_body(path):
    with open(path, encoding="utf-8") as f:
        tpl = json.load(f)
    body = next(c for c in tpl["components"] if c["type"] == "BODY")
    return sorted(set(int(n) for n in re.findall(r"\{\{(\d+)\}\}", body["text"])))


def _setup(monkeypatch, candidatos, window_open=False, template_aprobado=True):
    sent_templates = []
    saved = []
    pending = []
    events = []

    async def fake_send_template_fn(phone, template_name, body_params=None,
                                    button_payloads=None, **kw):
        sent_templates.append({"phone": phone, "template": template_name,
                               "body_params": list(body_params or []),
                               "button_payloads": list(button_payloads or [])})
        return "wamid.TEST"

    async def fake_is_approved(name):
        return template_aprobado

    monkeypatch.setattr(fidelizacion, "_get_crosssell_post_dental_candidatos",
                        lambda: candidatos)
    monkeypatch.setattr(fidelizacion, "USE_TEMPLATES", True)
    monkeypatch.setattr(fidelizacion, "puede_enviar_campana",
                        lambda phone, campana, dias_cooldown=180: True)
    monkeypatch.setattr(fidelizacion, "has_privacy_consent", lambda phone: True)
    monkeypatch.setattr(fidelizacion, "is_window_open", lambda phone: window_open)
    monkeypatch.setattr(fidelizacion, "save_fidelizacion_msg",
                        lambda phone, campana: saved.append((phone, campana)))
    monkeypatch.setattr(fidelizacion, "set_pending_crosssell",
                        lambda phone, tipo, destino: pending.append((phone, tipo)))
    monkeypatch.setattr(fidelizacion, "log_message", lambda *a, **k: None)
    monkeypatch.setattr(fidelizacion, "log_event",
                        lambda phone, ev, data=None: events.append((phone, ev)))
    monkeypatch.setattr(winback, "is_template_approved", fake_is_approved)
    return fake_send_template_fn, sent_templates, saved, events


async def _noop_send(phone, msg):
    return None


def test_template_v1_local_tiene_2_placeholders_y_fono_44():
    assert _placeholders_body(TPL_V1) == [1, 2]
    for path in (TPL_V1, TPL_V2):
        with open(path, encoding="utf-8") as f:
            raw = f.read()
        assert "(41) 296 5226" not in raw, (
            path + ": fijo (41) erroneo, el canonico es (44) 296 5226")


def test_envia_2_body_params_para_dra(monkeypatch):
    tpl_fn, sent, saved, events = _setup(
        monkeypatch,
        [{"phone": "56911111111", "nombre": "Carmen Soto",
          "profesional": "Dra. Javiera Burgos"}],
    )
    asyncio.run(fidelizacion.enviar_crosssell_post_dental_ortodoncia(
        _noop_send, send_template_fn=tpl_fn))
    assert len(sent) == 1
    n_params = len(sent[0]["body_params"])
    n_placeholders = len(_placeholders_body(TPL_V1))
    assert n_params == n_placeholders == 2, (
        "body_params=%d != placeholders=%d -> Meta 132000" % (n_params, n_placeholders))
    assert sent[0]["body_params"][1] == "Burgos"
    assert saved == [("56911111111", "crosssell_post_dental_ortodoncia")]


def test_dr_masculino_no_usa_template_femenino(monkeypatch):
    # "la Dra. {{2}}" no calza con Dr. Carlos Jimenez -> ventana cerrada = skip
    tpl_fn, sent, saved, events = _setup(
        monkeypatch,
        [{"phone": "56922222222", "nombre": "Pedro Paz",
          "profesional": "Dr. Carlos Jiménez"}],
    )
    asyncio.run(fidelizacion.enviar_crosssell_post_dental_ortodoncia(
        _noop_send, send_template_fn=tpl_fn))
    assert sent == []
    assert saved == []  # no quema cooldown


def test_envio_fallido_no_quema_cooldown(monkeypatch):
    tpl_fn, sent, saved, events = _setup(
        monkeypatch,
        [{"phone": "56933333333", "nombre": "Ana Ruiz",
          "profesional": "Dra. Javiera Burgos"}],
    )

    async def tpl_falla(phone, template_name, body_params=None,
                        button_payloads=None, **kw):
        return None  # Meta 4xx -> _post_meta retorna None

    asyncio.run(fidelizacion.enviar_crosssell_post_dental_ortodoncia(
        _noop_send, send_template_fn=tpl_falla))
    assert saved == [], "cooldown 180d quemado pese a envio fallido"
    assert ("56933333333", "template_send_failed") in events


def test_template_no_aprobado_cae_a_texto_libre_con_ventana(monkeypatch):
    enviados_libres = []

    tpl_fn, sent, saved, events = _setup(
        monkeypatch,
        [{"phone": "56944444444", "nombre": "Eva Mora",
          "profesional": "Dra. Javiera Burgos"}],
        window_open=True,
        template_aprobado=False,
    )

    async def send_libre(phone, msg):
        enviados_libres.append(phone)

    asyncio.run(fidelizacion.enviar_crosssell_post_dental_ortodoncia(
        send_libre, send_template_fn=tpl_fn))
    assert sent == []  # no uso template (no aprobado)
    assert enviados_libres == ["56944444444"]
    assert saved == [("56944444444", "crosssell_post_dental_ortodoncia")]
