"""
Ventana de 24 h en dos envíos de recepción (2026-10-07):
1. Recordatorio 48h de citas de recepción iba SIEMPRE libre: 367/476 (77%)
   fallaron 131047. Ahora ventana cerrada → template recordatorio_cita.
2. Respuesta manual del panel con ventana cerrada: Meta la rechazaba después y
   el panel la mostraba enviada. Ahora 409 + plantilla para retomar.
"""
import asyncio
import os
import sys
import tempfile
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
TMP_DB = Path(tempfile.mkdtemp(prefix="cmc_test_rv_")) / "s.db"
os.environ["SESSIONS_DB"] = str(TMP_DB)
os.environ.setdefault("SQLCIPHER_KEY", "")

import session  # noqa: E402
session.DB_PATH = TMP_DB
import reminders  # noqa: E402

PHONE = "56977776666"
CITA = {"id": 1, "phone": PHONE, "id_cita_medilink": "9100", "paciente_nombre": "Ana Ruiz",
        "especialidad": "Kinesiología", "profesional": "Luis Armijo", "hora": "10:00:00",
        "fecha": "2026-10-09", "id_profesional": 77}


def _setup(monkeypatch, ventana_abierta: bool):
    enviados = {"tpl": [], "int": [], "marcados": []}
    monkeypatch.setattr(reminders, "_guard_recepcion", lambda n: True)
    monkeypatch.setattr(reminders, "get_citas_recepcion_pendientes_48h", lambda f: [dict(CITA)])

    async def estado(*a, **k):
        return {"anulada": False, "confirmada": False}
    monkeypatch.setattr(reminders, "_estado_pre_envio", estado)
    monkeypatch.setattr(reminders, "USE_TEMPLATES", True)
    last = datetime.now(timezone.utc) - (timedelta(hours=2) if ventana_abierta else timedelta(days=5))
    monkeypatch.setattr(reminders, "get_last_inbound_ts", lambda p: last)
    monkeypatch.setattr(reminders, "mark_recepcion_reminder_48h_sent", lambda i: enviados["marcados"].append(i))
    monkeypatch.setattr(reminders, "log_message", lambda *a, **k: None)
    monkeypatch.setattr(reminders, "log_event", lambda *a, **k: None)

    async def no_sleep(s):
        return None
    monkeypatch.setattr(reminders.asyncio, "sleep", no_sleep)

    async def tpl(phone, name, body_params=None, button_payloads=None, **k):
        enviados["tpl"].append((name, body_params, button_payloads))
        return "wamid.X"

    async def inter(phone, msg):
        enviados["int"].append(msg)
    return enviados, tpl, inter


def test_48h_ventana_cerrada_usa_template(monkeypatch):
    env, tpl, inter = _setup(monkeypatch, ventana_abierta=False)
    asyncio.run(reminders.enviar_recordatorios_recepcion_48h(None, send_interactive_fn=inter,
                                                             send_template_fn=tpl))
    assert env["int"] == [] and len(env["tpl"]) == 1
    name, params, payloads = env["tpl"][0]
    assert name == "recordatorio_cita" and params[0] == "Ana" and params[1] == "Kinesiología"
    assert payloads == ["cita_confirm:9100", "cita_reagendar:9100", "cita_cancelar:9100"]
    assert env["marcados"] == [1]


def test_48h_ventana_abierta_sigue_interactivo(monkeypatch):
    env, tpl, inter = _setup(monkeypatch, ventana_abierta=True)
    asyncio.run(reminders.enviar_recordatorios_recepcion_48h(None, send_interactive_fn=inter,
                                                             send_template_fn=tpl))
    assert env["tpl"] == [] and len(env["int"]) == 1


def test_48h_template_fallido_no_marca(monkeypatch):
    env, tpl, inter = _setup(monkeypatch, ventana_abierta=False)

    async def tpl_falla(*a, **k):
        return None
    asyncio.run(reminders.enviar_recordatorios_recepcion_48h(None, send_interactive_fn=inter,
                                                             send_template_fn=tpl_falla))
    assert env["marcados"] == []


def test_respuesta_panel_con_ventana_cerrada_da_409(monkeypatch):
    import admin_routes
    monkeypatch.setattr(session, "is_window_open", lambda p: False)
    enviado = []

    async def fake_send(p, m):
        enviado.append(m)
        return "wamid"
    monkeypatch.setattr(admin_routes, "send_whatsapp", fake_send)
    with pytest.raises(HTTPException) as ex:
        asyncio.run(admin_routes.responder_como_recepcion(PHONE, "hola"))
    assert ex.value.status_code == 409 and "ventana_cerrada" in ex.value.detail
    assert enviado == []
