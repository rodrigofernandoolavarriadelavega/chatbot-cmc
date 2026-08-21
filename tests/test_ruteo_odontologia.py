"""
RUTEO ODONTOLÓGICO — la regla de ortodoncia no puede volver a comerse todo.

## Lo que pasó

El 2026-07-08 se decidió que la evaluación previa a ORTODONCIA la hace sólo la
Dra. Burgos: ella evalúa, pide radiografías, toma fotos y gestiona la derivación
a la ortodoncista. Eso es correcto y sigue vigente.

Pero al implementarla, la regla se aplicó a TODA la odontología: en
`ESPECIALIDADES_MAP` las claves genéricas ("odontología", "dentista", "dental")
pasaron de `[72, 55]` a `[55]`, y las que nombran a Jiménez pasaron a apuntar a
Burgos. El Dr. Jiménez quedó fuera del ruteo durante 44 días.

## Por qué costó plata, y no sólo una queja

Agendas leídas de Medilink el 2026-08-21:

    Burgos  (55) → lunes a viernes
    Jiménez (72) → viernes y SÁBADO

Con "solo Burgos", el bot **no podía agendar ni una hora dental de sábado**,
porque el único que atiende ese día estaba fuera del mapa. No fallaba con error:
simplemente no ofrecía nada, que es la forma más cara de fallar.

## Y el modo silencioso

Quien escribía "quiero hora con el Dr. Jiménez" era redirigido a Burgos SIN
AVISO. El paciente pedía un profesional y le agendaban con otro sin enterarse.

Corregido el 2026-08-21 (decisión del dueño): odontología general la atienden los
dos según disponibilidad; quien pide a Jiménez por su nombre va con Jiménez; la
regla de la evaluación previa queda acotada a ORTODONCIA, que es lo que siempre
fue.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import medilink  # noqa: E402

BURGOS, JIMENEZ, CASTILLO = 55, 72, 66
MAPA = medilink.ESPECIALIDADES_MAP


# ── Odontología general: los DOS ────────────────────────────────────────────

@pytest.mark.parametrize("clave", [
    "odontología", "odontologia", "dentista", "dental",
    "odontólogo", "odontologo", "odontología general", "odontologia general",
])
def test_odontologia_general_ofrece_ambos(clave):
    ids = MAPA.get(clave)
    assert ids is not None, f"'{clave}' salió del mapa"
    assert BURGOS in ids, f"'{clave}' dejó fuera a Burgos"
    assert JIMENEZ in ids, (
        f"'{clave}' dejó fuera a Jiménez. Es la regresión del 2026-07-08: sin él "
        "el bot no puede agendar ninguna hora dental de sábado.")


def test_el_sabado_dental_depende_de_jimenez():
    """Documenta POR QUÉ el test de arriba importa. Si alguien cambia las
    agendas y Burgos empieza a trabajar sábado, este test se cae y hay que
    revisar la premisa — no borrar el de arriba."""
    assert JIMENEZ in MAPA["odontología"], (
        "Jiménez atiende viernes y sábado; Burgos lunes a viernes. Sacarlo del "
        "ruteo deja el sábado sin cobertura dental.")


# ── Pedirlo por su nombre ───────────────────────────────────────────────────

@pytest.mark.parametrize("clave", [
    "jimenez", "jiménez", "carlos jimenez", "carlos jiménez",
    "dr jimenez", "dr jiménez",
])
def test_quien_pide_a_jimenez_va_con_jimenez(clave):
    ids = MAPA.get(clave)
    assert ids is not None, f"'{clave}' no está en el mapa"
    assert ids == [JIMENEZ], (
        f"'{clave}' rutea a {ids}. Redirigir a otro profesional SIN AVISAR hace "
        "que el paciente llegue esperando a alguien y se encuentre con otro.")


@pytest.mark.parametrize("clave", ["burgos", "javiera burgos", "dra burgos"])
def test_quien_pide_a_burgos_va_con_burgos(clave):
    assert MAPA.get(clave) == [BURGOS]


# ── La regla de ortodoncia sigue viva, y ACOTADA ────────────────────────────

def test_ortodoncia_sigue_yendo_a_castillo():
    assert MAPA.get("ortodoncia") == [CASTILLO]
    assert MAPA.get("ortodoncista") == [CASTILLO]


def test_la_evaluacion_de_ortodoncia_la_hace_burgos():
    """La regla legítima del 2026-07-08: antes de ortodoncia, evaluación con
    Burgos. Se mantiene — lo que se revirtió fue aplicarla a TODO."""
    import claude_helper
    src = claude_helper.SYSTEM_PROMPT if hasattr(claude_helper, "SYSTEM_PROMPT") \
        else open(os.path.join(os.path.dirname(__file__), "..", "app",
                               "claude_helper.py"), encoding="utf-8").read()
    assert "NO se agenda directamente con ortodoncia" in src
    assert "Javiera Burgos" in src


def test_el_prompt_dice_que_la_regla_es_solo_de_ortodoncia():
    """Sin esta frase, el modelo vuelve a generalizar la regla a toda la
    odontología — que es exactamente el error que se está reparando."""
    ruta = os.path.join(os.path.dirname(__file__), "..", "app", "claude_helper.py")
    src = open(ruta, encoding="utf-8").read()
    assert "ESTO APLICA SOLO A ORTODONCIA" in src


def test_el_catalogo_dental_nombra_a_los_dos():
    ruta = os.path.join(os.path.dirname(__file__), "..", "app", "claude_helper.py")
    src = open(ruta, encoding="utf-8").read()
    assert "Dra. Javiera Burgos y Dr. Carlos Jiménez" in src, (
        "El catálogo de odontología general atribuía todas las prestaciones a "
        "una sola dentista.")


# ── Los dos siguen registrados ──────────────────────────────────────────────

def test_ambos_dentistas_existen_con_su_intervalo():
    p = medilink.PROFESIONALES
    assert p[BURGOS]["especialidad"] == "Odontología General"
    assert p[JIMENEZ]["especialidad"] == "Odontología General"
    # Intervalos distintos: Burgos 60 min, Jiménez 30. Si se igualan por error,
    # el bot ofrece cupos que no existen o desperdicia agenda.
    assert p[BURGOS]["intervalo"] == 60
    assert p[JIMENEZ]["intervalo"] == 30
