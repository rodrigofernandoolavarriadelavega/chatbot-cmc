"""WAIT_SLOT: "sí por favor" confirma la hora sugerida, pero un "sí" que trae
otra fecha u hora ("sí pero mejor el martes", "sí a las 5") NO — ese texto lo
interpreta el resto del handler. Guardia sobre el fix del portaviones #8."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

import flows  # noqa: E402


def _ok(txt):
    tl = txt.lower().strip()
    return flows._afirma_slot(tl, tl)


def test_afirmaciones_simples_confirman():
    for t in ("si", "sí por favor", "si dale", "Sí!", "siii", "si porfa", "ya"):
        assert _ok(t), t


def test_si_con_otra_fecha_u_hora_no_confirma():
    for t in ("si pero mejor el martes", "si a las 5", "sí, mañana", "si otro dia",
              "si, el viernes", "sí pero en la tarde", "si 16:30"):
        assert not _ok(t), t
