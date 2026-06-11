"""Regresion F005 — el cron _job_detectar_cancelaciones marcaba cancel_detected_at
ANTES de llamar a enviar_reagendar_por_cancelacion, cuyo guard de idempotencia
retornaba {'ok': False, 'reason': 'ya_notificado'} sin enviar nada al paciente.

Cubre:
1. force=True salta el guard y envia (reason == 'notificado').
2. Sin force el guard sigue intacto (protege al endpoint admin cancel-doctor).
3. El cron pasa force=True al despachar canceladas proximas (<=48h).
"""
import asyncio
import sys
import types
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import jobs  # noqa: E402

_CL = ZoneInfo("America/Santiago")

CITA_MARCADA = {
    "phone": "56911111111", "id_cita": "99001", "especialidad": "kinesiologia",
    "profesional": "Luis Armijo", "fecha": "2026-06-11", "hora": "10:00",
    "modalidad": "presencial",
    # Pre-marcada por el cron de deteccion (este era el estado que gatillaba el bug)
    "cancel_detected_at": "2026-06-10 09:24:00",
}

SLOTS = [
    {"fecha": "2026-06-11", "hora_inicio": "11:00", "id_profesional": 77},
    {"fecha": "2026-06-11", "hora_inicio": "12:00", "id_profesional": 77},
    {"fecha": "2026-06-12", "hora_inicio": "09:00", "id_profesional": 77},
]


def _patch_envio(monkeypatch, enviados, marcados):
    """Aisla enviar_reagendar_por_cancelacion de red/DB/flows."""
    # Stub liviano de flows (la funcion hace `from flows import _format_slots`).
    _fake_flows = types.ModuleType("flows")
    _fake_flows._format_slots = lambda slots: "1) opcion de prueba"
    monkeypatch.setitem(sys.modules, "flows", _fake_flows)

    monkeypatch.setattr(jobs, "get_cita_bot_by_id_for_rebook",
                        lambda _id: dict(CITA_MARCADA))
    monkeypatch.setattr(jobs, "is_medilink_down", lambda: False)

    async def fake_buscar(_esp):
        return SLOTS, SLOTS

    monkeypatch.setattr(jobs, "buscar_primer_dia", fake_buscar)

    async def fake_send(phone, msg):
        enviados.append(("text", phone))

    async def fake_send_inter(phone, body):
        enviados.append(("interactive", phone))

    monkeypatch.setattr(jobs, "send_whatsapp", fake_send)
    monkeypatch.setattr(jobs, "send_whatsapp_interactive", fake_send_inter)
    monkeypatch.setattr(jobs, "mark_cita_cancel_detected",
                        lambda _id: marcados.append(_id))
    monkeypatch.setattr(jobs, "get_profile",
                        lambda _p: {"rut": "11111111-1", "nombre": "Test"})
    monkeypatch.setattr(jobs, "save_session", lambda *a, **k: None)
    monkeypatch.setattr(jobs, "log_message", lambda *a, **k: None)
    monkeypatch.setattr(jobs, "log_event", lambda *a, **k: None)


def test_force_envia_pese_a_premarca(monkeypatch):
    """Cita pre-marcada por el cron + force=True -> envia y reporta 'notificado'."""
    enviados, marcados = [], []
    _patch_envio(monkeypatch, enviados, marcados)
    res = asyncio.run(jobs.enviar_reagendar_por_cancelacion(
        "99001", motivo="medilink_cancel_detected", force=True))
    assert res["ok"] is True
    assert res["reason"] == "notificado"
    assert res["slots_enviados"] == 3
    assert len(enviados) >= 2  # header de aviso + lista de slots
    assert marcados == ["99001"]


def test_sin_force_guard_intacto(monkeypatch):
    """Sin force, el guard de idempotencia sigue protegiendo (path admin cancel-doctor)."""
    enviados, marcados = [], []
    _patch_envio(monkeypatch, enviados, marcados)
    res = asyncio.run(jobs.enviar_reagendar_por_cancelacion("99001"))
    assert res == {"ok": False, "reason": "ya_notificado"}
    assert enviados == []
    assert marcados == []


def test_cron_despacha_con_force(monkeypatch):
    """_job_detectar_cancelaciones pasa force=True a las canceladas <=48h."""
    import session as session_mod
    import medilink as medilink_mod

    proxima = datetime.now(_CL) + timedelta(hours=20)
    cita_bot = {
        "id_cita": "99002", "phone": "56922222222",
        "especialidad": "kinesiologia", "profesional": "Luis Armijo",
        "fecha": proxima.strftime("%Y-%m-%d"),
        "hora": proxima.strftime("%H:%M"),
        "id_paciente_medilink": "555",
    }
    monkeypatch.setattr(session_mod, "get_citas_bot_para_validar",
                        lambda dias_adelante=14: [dict(cita_bot)])
    monkeypatch.setattr(session_mod, "mark_cita_cancel_detected",
                        lambda _id: None)
    monkeypatch.setattr(session_mod, "expire_stale_offers",
                        lambda: 0, raising=False)

    async def fake_get_cita(_id):
        # Cita anulada en Medilink (id_estado=1) sin reasignacion
        return {"id_estado": 1, "estado_anulacion": 1, "id_paciente": "555",
                "id_profesional": 77}

    monkeypatch.setattr(medilink_mod, "get_cita", fake_get_cita)
    monkeypatch.setattr(jobs, "is_medilink_down", lambda: False)
    monkeypatch.setattr(jobs, "log_event", lambda *a, **k: None)

    llamadas = []

    async def fake_rebook(id_cita, motivo="doctor_cancel", force=False):
        llamadas.append({"id_cita": id_cita, "force": force})
        return {"ok": True, "reason": "notificado"}

    monkeypatch.setattr(jobs, "enviar_reagendar_por_cancelacion", fake_rebook)

    asyncio.run(jobs._job_detectar_cancelaciones())

    assert llamadas == [{"id_cita": "99002", "force": True}]
