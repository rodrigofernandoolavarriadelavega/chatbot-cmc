"""F019 — Guard de wamid en envíos por template (fallos silenciosos).

Verifica:
1. send_whatsapp_template retorna el wamid que entrega _post_meta
   (None si Meta rechazó el envío o agotó los 3 reintentos).
2. enviar_recordatorios NO marca reminder_sent cuando el template falla.
3. enviar_recordatorios SÍ marca reminder_sent cuando el envío fue OK.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT))

os.environ.setdefault("SQLCIPHER_KEY", "")

import session as _session_mod
# DB temporal aislada — jamás tocar data/sessions.db real desde tests.
_session_mod.DB_PATH = Path(tempfile.mkdtemp(prefix="f019_")) / "sessions.db"

import messaging
import reminders


def test_send_whatsapp_template_retorna_wamid(monkeypatch):
    async def fake_post_meta(payload):
        assert payload["type"] == "template"
        return "wamid.TEST123"

    async def fake_lang(name):
        return "es_CL"

    monkeypatch.setattr(messaging, "_post_meta", fake_post_meta)
    monkeypatch.setattr(messaging, "_get_template_language", fake_lang)
    out = asyncio.run(messaging.send_whatsapp_template(
        "56912345678", "recordatorio_cita", body_params=["María"]))
    assert out == "wamid.TEST123", (
        "send_whatsapp_template debe retornar el wamid de _post_meta")


def test_send_whatsapp_template_retorna_none_si_meta_falla(monkeypatch):
    async def fake_post_meta(payload):
        return None  # Meta rechazó (template pausado, token vencido, 3 timeouts)

    async def fake_lang(name):
        return "es_CL"

    monkeypatch.setattr(messaging, "_post_meta", fake_post_meta)
    monkeypatch.setattr(messaging, "_get_template_language", fake_lang)
    out = asyncio.run(messaging.send_whatsapp_template(
        "56912345678", "recordatorio_cita", body_params=["María"]))
    assert out is None


def _correr_enviar_recordatorios(monkeypatch, wamid_result):
    """Escenario mínimo: 1 cita válida para mañana, path de template."""
    cita = {
        "id": 1, "id_cita": 9001, "phone": "56912345678",
        "paciente_nombre": "María Pérez", "especialidad": "Kinesiología",
        "profesional": "Luis Armijo", "fecha": "2026-06-11",
        "hora": "10:00:00", "modalidad": "particular",
        "id_paciente_medilink": 555,
    }
    marcadas = []

    monkeypatch.setattr(reminders, "get_citas_bot_pendientes", lambda f: [dict(cita)])
    monkeypatch.setattr(reminders, "mark_reminder_sent", lambda cid: marcadas.append(cid))
    monkeypatch.setattr(reminders, "log_event", lambda *a, **k: None)
    monkeypatch.setattr(reminders, "log_message", lambda *a, **k: None)
    monkeypatch.setattr(reminders, "render_template_body", lambda *a, **k: "tpl")
    monkeypatch.setattr(reminders, "USE_TEMPLATES", True)
    monkeypatch.setattr(reminders, "ADMIN_ALERT_PHONE", "")

    async def fake_get_cita(id_cita):
        # Cita vigente en Medilink (pasa la pre-validación)
        return {"id_estado": 0, "estado_anulacion": 0, "id_paciente": 555}

    monkeypatch.setattr(reminders, "get_cita", fake_get_cita)

    async def fake_template(to, name, body_params=None, button_payloads=None, **kw):
        return wamid_result

    async def fake_text(to, body):
        return "wamid.TXT"

    asyncio.run(reminders.enviar_recordatorios(
        fake_text, send_interactive_fn=None, send_template_fn=fake_template))
    return marcadas


def test_no_marca_reminder_sent_si_template_falla(monkeypatch):
    marcadas = _correr_enviar_recordatorios(monkeypatch, wamid_result=None)
    assert marcadas == [], (
        "mark_reminder_sent NO debe llamarse cuando send_template_fn retorna None "
        "(el paciente no recibió nada; el próximo ciclo debe reintentar)")


def test_marca_reminder_sent_si_template_ok(monkeypatch):
    marcadas = _correr_enviar_recordatorios(monkeypatch, wamid_result="wamid.OK")
    assert marcadas == [1], "mark_reminder_sent debe llamarse cuando hay wamid"
