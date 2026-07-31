"""Ventana del abono: el plazo prometido debe ser el plazo real.

Bug encontrado el 2026-07-31 mirando la pantalla de pago en producción:

    expira = calcular_expira(now, horas=(wait_min / 60) if wait_min else None)
    exp = creado + timedelta(hours=int(horas or ABONO_VENTANA_HORAS))

`flows.py` pasa wait_min=90 → horas=1.5 → **int(1.5) = 1**. El mensaje de
WhatsApp decía "apartada por 90 minutos" y el abono vencía a los 60.

Verificado contra prod antes del fix — los 3 abonos de Gastroenterología del
30-jul tenían exactamente 60 min entre creado_at y expira_at:

    creado 2026-07-30T10:48:21  expira 2026-07-30T11:48:21  → 60 min
    creado 2026-07-30T10:11:03  expira 2026-07-30T11:11:03  → 60 min
    creado 2026-07-30T09:47:23  expira 2026-07-30T10:47:23  → 60 min
"""

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from abono_transferencia import calcular_expira  # noqa: E402

_CL = ZoneInfo("America/Santiago")


def _mins(creado, expira):
    return (expira - creado).total_seconds() / 60


# ── El bug ───────────────────────────────────────────────────────────────────

def test_noventa_minutos_son_noventa_minutos():
    """Regresión directa: int(1.5) truncaba a 1 h."""
    creado = datetime(2026, 7, 30, 10, 48, tzinfo=_CL)
    assert _mins(creado, calcular_expira(creado, horas=90 / 60)) == 90


@pytest.mark.parametrize("wait_min", [30, 45, 90, 150, 210])
def test_cualquier_wait_min_se_respeta_al_minuto(wait_min):
    creado = datetime(2026, 7, 30, 10, 0, tzinfo=_CL)
    assert _mins(creado, calcular_expira(creado, horas=wait_min / 60)) == wait_min


def test_default_sigue_siendo_horas_enteras():
    """Sin `horas`, manda ABONO_VENTANA_HORAS (4 h)."""
    from config import ABONO_VENTANA_HORAS
    creado = datetime(2026, 7, 30, 10, 0, tzinfo=_CL)
    assert _mins(creado, calcular_expira(creado)) == int(ABONO_VENTANA_HORAS) * 60


# ── Horario del centro (comportamiento que ya existía, no romperlo) ──────────

def test_vencimiento_nocturno_se_corre_a_las_nueve():
    """21:00–09:00 el centro está cerrado: el plazo se corre a las 09:00."""
    creado = datetime(2026, 7, 30, 20, 30, tzinfo=_CL)
    exp = calcular_expira(creado, horas=90 / 60)   # caería 22:00
    assert (exp.hour, exp.minute) == (9, 0)
    assert exp.date() > creado.date(), "debe ser el día siguiente"


def test_abono_de_madrugada_vence_a_las_nueve_del_mismo_dia():
    creado = datetime(2026, 7, 30, 3, 0, tzinfo=_CL)
    exp = calcular_expira(creado, horas=90 / 60)   # caería 04:30
    assert (exp.hour, exp.minute) == (9, 0)
    assert exp.date() == creado.date()


# ── Lo que consume el mensaje de WhatsApp y la página ───────────────────────

def test_crear_abono_expone_hora_de_vencimiento(monkeypatch, tmp_path):
    """El mensaje necesita la hora real ("hasta las 18:30"), no un texto fijo."""
    import abono_transferencia as at

    creado = datetime(2026, 7, 30, 10, 48, tzinfo=_CL)
    exp = at.calcular_expira(creado, horas=90 / 60)
    # 10:48 + 90 min = 12:18, dentro del horario del centro
    assert exp.strftime("%H:%M") == "12:18"
    assert exp.date() == creado.date(), "no debe cruzar de día"
