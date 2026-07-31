"""Guardrail: la duración del bot debe dividir el intervalo de Medilink.

Al GENERAR slots el bot impone su propia duración e ignora la de Medilink
(decisión de diseño, documentada en CLAUDE.md). Pero al CREAR la cita manda
`duracion` y ahí Medilink SÍ valida contra su intervalo: si no divide,
responde 400 "Duración no es compatible con el intervalo de atención" — y el
paciente lo ve en el ÚLTIMO paso, después de haber confirmado.

Caso real (jun–jul 2026): Dra. Cecilia Unibazo (prof 78) con 40 min en el bot
y 15 en Medilink. 40 % 15 = 10 → 46 reservas caídas contra 31 exitosas
(60% de fallo), 37 pacientes distintos, siete semanas sin que nada avisara.
Lo resolvió el dueño cambiando el intervalo de Medilink a 5.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))


def divide(bot: int, medilink: int) -> bool:
    """Misma condición que _job_verificar_intervalos."""
    return bot % medilink == 0


# ── El caso que costó las 46 reservas ────────────────────────────────────────

def test_unibazo_antes_del_arreglo_no_dividia():
    assert not divide(40, 15), "40 % 15 = 10 — este era el estado que fallaba"


def test_unibazo_despues_del_arreglo_divide():
    assert divide(40, 5), "el dueño bajó Medilink a 5 el 30-jul y quedó resuelto"


# ── Configuraciones que hoy conviven sin fallar ──────────────────────────────

@pytest.mark.parametrize("nombre,bot,ml", [
    ("Etcheverry",  40, 10),
    ("Burgos",      60, 30),
    ("Castillo",    30,  5),
    ("Pardo",       15,  5),
    ("Quijano",     20, 20),
    ("Olavarría",   15, 15),
])
def test_desajustes_actuales_siguen_dividiendo(nombre, bot, ml):
    """No fallan hoy, pero nada avisaría si alguien moviera uno de los números.
    Justamente por eso existe el cron."""
    assert divide(bot, ml), f"{nombre}: {bot} % {ml} != 0"


# ── Casos límite de la condición ─────────────────────────────────────────────

@pytest.mark.parametrize("bot,ml,esperado", [
    (40, 15, False),   # el bug
    (40,  5, True),
    (30, 20, False),   # media hora contra bloques de 20
    (45, 30, False),
    (20, 20, True),    # iguales
    (60, 15, True),
])
def test_condicion(bot, ml, esperado):
    assert divide(bot, ml) is esperado


# ── El job existe y es usable ────────────────────────────────────────────────

def test_job_registrado_y_sin_asyncio_faltante():
    """jobs.py NO importa asyncio a nivel de módulo — la función usa
    asyncio.sleep, así que necesita el import local o revienta con NameError
    en runtime (que el deep-import del deploy no detecta)."""
    import inspect
    import jobs
    assert hasattr(jobs, "_job_verificar_intervalos")
    src = inspect.getsource(jobs._job_verificar_intervalos)
    if "asyncio." in src:
        assert "import asyncio" in src, "usa asyncio sin importarlo dentro de la función"


def test_lector_de_intervalo_existe():
    import medilink
    assert hasattr(medilink, "intervalo_en_medilink")


def test_job_usa_el_carril_batch():
    """Guardrail 429: todo cron que pegue a Medilink llama use_batch_lane()
    primero, para no competir con los pacientes en vivo."""
    import inspect
    import jobs
    assert "use_batch_lane()" in inspect.getsource(jobs._job_verificar_intervalos)
