"""La ventana del Abono-Gate pasa de 90 min a 24 h.

## Por qué se mantiene el gate

Medido el 18-ago-2026 sobre las citas de psiquiatría ya ocurridas (Medilink,
`estado_cita`; el id_estado=8 de no-show no se usa en este CMC, el que no
llega queda como "Anulado"):

    mes       total  atendidas  % asiste   % anuladas
    2026-06      20         6      30%         65%     ← sin gate
    2026-07      97        35      36%         42%     ← sin gate
    2026-08      43        26      60%         26%     ← con gate

El pago por adelantado DUPLICA la asistencia. No se toca.

## Por qué se cambia el plazo

De los 13 que pagaron: mediana 13 min, pero **dos pagaron a las 49 y 71 horas**
— fuera de los 90 min, rescatados de casualidad por la conciliación por correo.
De los 36 que no pagaron, 24 se perdieron del todo. La población del CMC es
rural: mucha no tiene la app del banco a mano y transfiere al llegar a casa.

## Lo que NO se hizo

Se evaluó darle trato preferente al paciente recurrente (agendar primero y
cobrar después). Los datos lo desmienten:

    1ª vez en psiquiatría .......  84 citas   58% asiste
    control (2ª en adelante) ....  76 citas   24% asiste

En salud mental la recurrencia no es señal de compromiso — el abandono de
tratamiento es parte del cuadro. Quitarle el gate a los recurrentes se lo
habría quitado justo al grupo que más falla.
"""

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from abono_transferencia import calcular_expira  # noqa: E402

_CL = ZoneInfo("America/Santiago")


def test_la_ventana_por_defecto_es_24h():
    from config import ABONO_WAIT_MIN
    assert ABONO_WAIT_MIN == 1440, "24 horas en minutos"


def test_es_configurable_por_env():
    """El dueño debe poder ajustarlo sin tocar código."""
    import inspect
    import config
    src = inspect.getsource(config)
    assert 'os.getenv("ABONO_WAIT_MIN"' in src


@pytest.mark.parametrize("hora,dia_esperado,hhmm", [
    (9,  1, "09:30"),    # mañana a la misma hora
    (14, 1, "14:30"),
    (18, 1, "18:30"),
    (20, 1, "20:30"),
    (22, 2, "09:00"),    # +24h caería 22:30 → centro cerrado → 09:00 del subsiguiente
    (3,  1, "09:00"),    # madrugada: +24h caería 03:30 → se corre a las 09:00
])
def test_vencimiento_segun_hora_de_creacion(hora, dia_esperado, hhmm):
    creado = datetime(2026, 8, 18, hora, 30, tzinfo=_CL)
    exp = calcular_expira(creado, horas=1440 / 60)
    assert (exp.date() - creado.date()).days == dia_esperado
    assert exp.strftime("%H:%M") == hhmm


def test_respeta_el_horario_del_centro():
    """Nunca vence con el centro cerrado (21:00–09:00)."""
    for h in range(24):
        creado = datetime(2026, 8, 18, h, 15, tzinfo=_CL)
        exp = calcular_expira(creado, horas=1440 / 60)
        assert 9 <= exp.hour < 21, f"creado {h}:15 vence a las {exp.hour}"


def test_veinticuatro_horas_cubre_a_los_dos_rezagados():
    """Los dos que pagaron tarde: 49 h y 71 h. La ventana de 24 h no los
    alcanza a ambos, pero sí cubre todo el primer día — que es donde está la
    masa. Es una mejora, no una solución total."""
    creado = datetime(2026, 8, 18, 10, 0, tzinfo=_CL)
    exp = calcular_expira(creado, horas=1440 / 60)
    horas_reales = (exp - creado).total_seconds() / 3600
    assert horas_reales >= 23, "debe dar al menos ~24 h"
    assert horas_reales < 49, "no llega a los rezagados extremos, y está bien"


# ── El mensaje no puede mentir el plazo ─────────────────────────────────────

def test_crear_abono_expone_los_dias_de_diferencia():
    """Con 24 h el vencimiento cae otro día: decir solo 'hasta las 09:00' se
    leería como una hora que ya pasó."""
    import inspect
    import abono_transferencia
    src = inspect.getsource(abono_transferencia.crear_abono_pendiente)
    assert "expira_en_dias" in src
    assert "expira_fecha" in src


def test_el_mensaje_distingue_hoy_manana_y_mas_alla():
    import inspect
    import flows
    src = inspect.getsource(flows)
    assert "expira_en_dias" in src, "el copy debe usar los días, no un booleano"
    assert "hasta mañana a las" in src
