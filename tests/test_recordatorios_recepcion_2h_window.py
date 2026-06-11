"""
Test de la ventana del recordatorio "2h antes" para citas de recepción (bug F006).

Bug: enviar_recordatorios_recepcion_2h calculaba la ventana [ahora-15, ahora+15]
→ el mensaje llegaba A LA HORA de la cita. Debe ser [ahora+105, ahora+135],
espejo de enviar_recordatorios_2h (versión bot), con guard de medianoche.

Estos tests FALLAN con el código pre-fix y PASAN con el fix.
"""
import asyncio
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent.parent / "app"))

_TZ_CL = ZoneInfo("America/Santiago")


class _FakeDatetime(datetime):
    """Reemplaza reminders.datetime para congelar now()."""
    _frozen = None

    @classmethod
    def now(cls, tz=None):
        if tz is not None:
            return cls._frozen.astimezone(tz)
        return cls._frozen


def _run_capture(frozen_cl):
    """Corre enviar_recordatorios_recepcion_2h con now() congelado y captura
    los argumentos con que se llama a get_citas_recepcion_pendientes_2h."""
    import reminders as rem

    captured = {}

    def fake_get(fecha, hora_min, hora_max):
        captured["fecha"] = fecha
        captured["hora_min"] = hora_min
        captured["hora_max"] = hora_max
        return []

    _FakeDatetime._frozen = frozen_cl
    with patch.object(rem, "RECORDATORIOS_RECEPCION_ENABLED", True), \
         patch.object(rem, "datetime", _FakeDatetime), \
         patch.object(rem, "get_citas_recepcion_pendientes_2h", fake_get):
        result = asyncio.run(rem.enviar_recordatorios_recepcion_2h(AsyncMock()))
    return result, captured


def test_ventana_es_105_a_135_minutos_adelante():
    """Cron corre 10:00 CLT → busca citas entre 11:45 y 12:15 (no 09:45-10:15)."""
    frozen = datetime(2026, 6, 10, 10, 0, tzinfo=_TZ_CL)
    result, captured = _run_capture(frozen)
    assert result == 0
    assert captured["fecha"] == "2026-06-10"
    assert captured["hora_min"] == "11:45", (
        f"hora_min={captured['hora_min']!r}: la ventana debe partir 105 min "
        "adelante, no 15 min atrás (el recordatorio llegaba a la hora de la cita)"
    )
    assert captured["hora_max"] == "12:15"


def test_cita_a_las_12_no_entra_si_cron_corre_a_las_12():
    """A las 12:00 la ventana es [13:45, 14:15] — una cita de las 12:00 NO entra."""
    frozen = datetime(2026, 6, 10, 12, 0, tzinfo=_TZ_CL)
    result, captured = _run_capture(frozen)
    assert result == 0
    assert not (captured["hora_min"] <= "12:00" <= captured["hora_max"]), (
        "una cita a las 12:00 no debe matchear la ventana cuando el cron corre a las 12:00"
    )


def test_ventana_que_cruza_medianoche_no_consulta():
    """A las 23:00 la ventana (+105/+135) cruza medianoche → early-return sin query."""
    frozen = datetime(2026, 6, 10, 23, 0, tzinfo=_TZ_CL)
    result, captured = _run_capture(frozen)
    assert result == 0
    assert captured == {}, "con cruce de medianoche NO debe consultarse la DB"
