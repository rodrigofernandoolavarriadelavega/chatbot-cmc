"""El watchdog de Claude debe INSISTIR, no avisar una sola vez.

Dos apagones reales de saldo, los dos detectados y avisados correctamente —
y los dos se prolongaron porque el aviso no se repitió:

  21-jul-2026  avisó a los 16 s → nadie recargó → 6 días caído
               1.020 fallos · 193 personas con "problema técnico" · 137 no agendaron
  18-ago-2026  avisó a los 2 min y se calló igual; se detectó por casualidad
               al revisar el dashboard, 1 hora después

La idempotencia por racha evita spam, pero cuando el fallo EXIGE acción humana
y esa acción no llega, "ya avisé" se vuelve indistinguible de "ya se resolvió".
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))


# ── Lógica de re-alerta, aislada del estado global ──────────────────────────

REALERTA_HORAS = 6.0


def debe_avisar(ya_avisada: bool, horas_desde_ultimo: float | None) -> bool:
    """Misma condición que should_alert_claude_down para una racha ya marcada."""
    if not ya_avisada:
        return True                      # primer aviso de la racha
    if horas_desde_ultimo is None:
        return True                      # racha sin timestamp (versión previa)
    return horas_desde_ultimo >= REALERTA_HORAS


def test_primer_aviso_siempre_sale():
    assert debe_avisar(ya_avisada=False, horas_desde_ultimo=None)


def test_no_repite_antes_de_la_ventana():
    """No convertir la alerta en spam: bajo 6 h no insiste."""
    for h in (0.0, 0.5, 2.0, 5.9):
        assert not debe_avisar(True, h), f"insistió a las {h} h"


@pytest.mark.parametrize("horas", [6.0, 8.0, 24.0, 144.0])
def test_insiste_pasada_la_ventana(horas):
    assert debe_avisar(True, horas), f"NO insistió a las {horas} h"


def test_racha_marcada_sin_timestamp_avisa():
    """Si la racha viene de la versión vieja (sin claude_down_alerted_at),
    hay que avisar igual en vez de quedarse mudo para siempre."""
    assert debe_avisar(True, None)


# ── El caso de julio: cuántas veces habría avisado ──────────────────────────

def test_el_apagon_de_julio_habria_avisado_23_veces():
    """21-jul 18:02 → 27-jul 14:36 = 140,6 h. A una cada 6 h son 23 insistencias
    además del primer aviso. Con el código viejo: 1 sola."""
    inicio = datetime(2026, 7, 21, 18, 2, tzinfo=timezone.utc)
    fin = datetime(2026, 7, 27, 14, 36, tzinfo=timezone.utc)
    horas = (fin - inicio).total_seconds() / 3600
    assert horas == pytest.approx(140.57, abs=0.1)
    assert int(horas // REALERTA_HORAS) == 23


def test_el_apagon_de_agosto_no_habria_alcanzado_a_reavisar():
    """18-ago 07:49 → ~08:52 = 1 h. Correcto que NO insista todavía: la ventana
    de 6 h existe para no molestar por apagones que se resuelven rápido."""
    horas = 1.05
    assert not debe_avisar(True, horas)


# ── El módulo expone lo que el mensaje necesita ─────────────────────────────

def test_resilience_expone_los_helpers():
    import resilience
    assert hasattr(resilience, "es_realerta")
    assert hasattr(resilience, "horas_caido")
    assert resilience._REALERTA_HORAS == REALERTA_HORAS


def test_mark_guarda_tambien_el_timestamp():
    """Sin el timestamp del último aviso no hay forma de saber cuándo insistir."""
    import inspect
    import resilience
    src = inspect.getsource(resilience.mark_claude_down_alerted)
    assert "_KEY_CLAUDE_ALERTED_AT" in src


def test_el_job_lee_es_realerta_antes_de_marcar():
    """mark_claude_down_alerted() sobrescribe la marca: si el job leyera
    es_realerta() después, toda alerta parecería la primera."""
    import inspect
    import jobs
    src = inspect.getsource(jobs._job_claude_watchdog)
    assert src.index("es_realerta()") < src.index("mark_claude_down_alerted()")
