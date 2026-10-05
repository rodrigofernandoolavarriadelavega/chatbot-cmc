"""El paciente confirma desde el recordatorio → la cita pasa a
"Confirmado por bot 48hrs" (24) o "2hrs" (25) en Medilink, sin pisar estados
que la recepción ya movió (anulada, confirmada, en sala, atendida)."""
import asyncio
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import medilink  # noqa: E402


class _Resp:
    def __init__(self, status, text=""):
        self.status_code, self.text = status, text


class _Client:
    def __init__(self, status=200, text=""):
        self.puts, self._r = [], _Resp(status, text)

    async def put(self, url, json=None, headers=None):
        self.puts.append((url, json))
        return self._r


def _run(cita, client):
    async def fake_get_cita(_id):
        return cita
    medilink.get_cita, medilink._get_shared_client = fake_get_cita, lambda: client
    return asyncio.run(medilink.confirmar_cita_whatsapp(123, 24))


def test_no_confirmada_pasa_a_estado_bot():
    c = _Client()
    ok, _ = _run({"id_estado": 7, "estado_anulacion": 0, "estado_cita": "No confirmado"}, c)
    assert ok and c.puts == [(f"{medilink.MEDILINK_BASE_URL}/citas/123", {"id_estado": 24})]


def test_notificada_por_whatsapp_pasa_a_estado_bot():
    c = _Client()
    ok, _ = _run({"id_estado": 18, "estado_anulacion": 0}, c)
    assert ok and len(c.puts) == 1


def test_no_pisa_estados_avanzados_ni_confirmados():
    for est in (2, 3, 5, 6, 8, 11, 20, 22, 24, 25, 26):
        c = _Client()
        ok, _ = _run({"id_estado": est, "estado_anulacion": 0}, c)
        assert not ok and c.puts == [], est


def test_no_toca_anulada():
    c = _Client()
    ok, motivo = _run({"id_estado": 14, "estado_anulacion": 1, "estado_cita": "Cambio de fecha"}, c)
    assert not ok and c.puts == [] and "anulada" in motivo


def test_cita_ilegible_no_hace_put():
    c = _Client()
    ok, _ = _run(None, c)
    assert not ok and c.puts == []


def test_rechazo_medilink_no_lanza():
    c = _Client(status=422, text="estado no permitido")
    ok, motivo = _run({"id_estado": 7, "estado_anulacion": 0}, c)
    assert not ok and "422" in motivo


def test_confirmado_whatsapp_cuenta_como_confirmada():
    assert medilink.cita_esta_confirmada({"id_estado": 20, "estado_cita": "Confirmado por Whatsapp"})


def test_confirmado_por_bot_cuenta_como_confirmada():
    assert medilink.cita_esta_confirmada({"id_estado": 24, "estado_cita": "Confirmado por bot 48hrs"})
    assert medilink.cita_esta_confirmada({"id_estado": 25, "estado_cita": "Confirmado por bot 2hrs"})


_AHORA = datetime(2026, 10, 2, 9, 0, tzinfo=ZoneInfo("America/Santiago"))


def test_estado_segun_horas_que_faltan():
    f = medilink.estado_confirmacion_bot
    assert f("2026-10-04", "10:00", _AHORA) == 24   # recordatorio 48h
    assert f("2026-10-03", "10:00", _AHORA) == 24   # recordatorio 24h (09:00 día antes)
    assert f("2026-10-02", "16:00", _AHORA) == 24   # faltan 7 h
    assert f("2026-10-02", "15:00", _AHORA) == 25   # faltan 6 h justas
    assert f("2026-10-02", "11:00", _AHORA) == 25   # recordatorio 2h
    assert f("2026-10-02", "11:00:00", _AHORA) == 25  # hora con segundos
    assert f("", "", _AHORA) == 24                   # sin datos → 48hrs
