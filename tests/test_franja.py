"""Franja horaria — fuente única de verdad (app/franja.py).

Caso que motiva el módulo: Sara Bustamante (56981712917, 2026-05-14) escribió
"Disponibilidad para kinesiologo para mañana durante la mañana" y el bot le
ofreció 17:20 y 18:00. "durante la mañana" no estaba en ninguna de las tres
listas de keywords que existían en flows.py.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

import franja  # noqa: E402


# ── Regresión directa del caso Sara ──────────────────────────────────────────

def test_caso_sara_dia_y_franja_juntos():
    """El mensaje trae las dos cosas y ambas deben sobrevivir."""
    dia, fr = franja.dia_y_franja(
        "Disponibilidad para kinesiologo para mañana durante la mañana"
    )
    assert dia is True
    assert fr == franja.FRANJAS["mañana"]


def test_caso_sara_no_ofrece_tarde():
    """Con la franja aplicada, 17:20 y 18:00 quedan fuera."""
    slots = [
        {"hora_inicio": "09:40"},
        {"hora_inicio": "11:00"},
        {"hora_inicio": "17:20"},
        {"hora_inicio": "18:00"},
    ]
    horas = [s["hora_inicio"] for s in franja.filtrar(slots, franja.FRANJAS["mañana"])]
    assert horas == ["09:40", "11:00"]


# ── El bug silencioso: franja leída como día ─────────────────────────────────

@pytest.mark.parametrize("txt", [
    "quiero hora durante la mañana",
    "una hora para la mañana",
    "tiene hora en la mañana",
    "hora por la mañana",
    "algo a la mañana",
])
def test_franja_sola_no_se_lee_como_dia(txt):
    """Estos pedían HOY temprano; el parser viejo los agendaba para el día
    siguiente porque solo conocía "en la"/"por la"."""
    dia, fr = franja.dia_y_franja(txt)
    assert fr == franja.FRANJAS["mañana"]
    assert dia is False, f"{txt!r} se leyó como el día de mañana"


@pytest.mark.parametrize("txt", ["mañana", "para mañana", "vengo mañana"])
def test_dia_solo_no_filtra_franja(txt):
    """Sin preposición, 'mañana' es el día — no debe filtrar slots AM."""
    dia, fr = franja.dia_y_franja(txt)
    assert dia is True
    assert fr is None


def test_manana_en_la_manana_conserva_ambos():
    """Espejo del bug: el parser viejo detectaba franja y perdía el día."""
    dia, fr = franja.dia_y_franja("algo mañana en la mañana")
    assert dia is True
    assert fr == franja.FRANJAS["mañana"]


# ── Cobertura del resto de las franjas ───────────────────────────────────────

@pytest.mark.parametrize("txt,esperado", [
    ("hora en la tarde",           franja.FRANJAS["tarde"]),
    ("tardecita",                  franja.FRANJAS["tarde"]),
    ("por la noche",               franja.FRANJAS["noche"]),
    ("al mediodia",                franja.FRANJAS["mediodia"]),
    ("mediodía",                   franja.FRANJAS["mediodia"]),
    ("algo mas tarde",             (15, 23)),
    ("tempranito por favor",       (8, 11)),
    ("despues de las 5 de la tarde", (17, 23)),
    ("después de las 15",          (15, 23)),
    ("antes de las 11",            (8, 11)),
    ("hora con kine",              None),
    ("",                           None),
])
def test_parse(txt, esperado):
    assert franja.parse(txt) == esperado


# ── Contrato del filtro ──────────────────────────────────────────────────────

def test_filtrar_sin_franja_devuelve_intacto():
    slots = [{"hora_inicio": "09:00"}, {"hora_inicio": "17:00"}]
    assert franja.filtrar(slots, None) == slots


def test_filtrar_es_half_open():
    """13:00 es tarde, no mañana. El filtro viejo (<= en ambos extremos) lo
    contaba en las dos franjas."""
    slots = [{"hora_inicio": "13:00"}]
    assert franja.filtrar(slots, franja.FRANJAS["mañana"]) == []
    assert franja.filtrar(slots, franja.FRANJAS["tarde"]) == slots


def test_filtrar_conserva_slot_con_hora_ilegible():
    """Mejor mostrar un horario de más que perder al paciente por un dato malo."""
    slots = [{"hora_inicio": None}, {"hora_inicio": "09:00"}]
    assert len(franja.filtrar(slots, franja.FRANJAS["mañana"])) == 2


def test_rangos_no_se_solapan_en_los_bordes():
    """Ninguna hora entera puede caer en dos franjas nominales a la vez.
    (mediodía se excluye a propósito: solapa con mañana y tarde por diseño.)"""
    nominales = {k: v for k, v in franja.FRANJAS.items() if k != "mediodia"}
    for h in range(24):
        dentro = [n for n, (lo, hi) in nominales.items() if lo <= h < hi]
        assert len(dentro) <= 1, f"hora {h} cae en {dentro}"


# ── Etiquetas ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("fr,esperado", [
    (franja.FRANJAS["mañana"], "la mañana"),
    (franja.FRANJAS["tarde"],  "la tarde"),
    (franja.FRANJAS["noche"],  "la noche"),
    ((17, 23),                 "después de las 17"),
    ((8, 11),                  "antes de las 11"),
    (None,                     ""),
])
def test_label(fr, esperado):
    assert franja.label(fr) == esperado
