"""Fechas de nacimiento leídas como fecha de cita.

Caso real: Mackarena Binimelis (56984110506, 2026-07-21). El registro falló
("Hubo un problema al registrarte"), la sesión volvió a IDLE, ella reenvió
"Mackarena Binimelis Delgado / 4/12/80" y el parser de salto de fecha leyó
"4/12/80" como el 4 de diciembre de 2080. Evento en prod:

    salto_fecha_directo {"fecha": "2080-12-04"}

El mensaje que recibió decía "Miércoles 4 de diciembre" — sin año, así que
la fecha absurda era invisible. 2080-12-04 efectivamente cae en miércoles.

En 70 eventos salto_fecha_directo de producción había 3 con año imposible:
2080-12-04, 2001-01-16 y 2009-12-21. Las tres son fechas de nacimiento.
"""

import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))


# ── Ventana de siglo ─────────────────────────────────────────────────────────

def _resolver_anio(yy: int, anio_actual: int) -> int:
    """Misma regla que flows.py: 2 dígitos por sobre el año actual +1 → 1900s.

    El +1 deja pasar el año entrante ("27" en 2026 es 2027, no 1927). Más allá
    no hace falta afinar: la cota de sanidad de 365 días descarta igual
    cualquier fecha a más de un año.
    """
    return yy + (2000 if yy <= anio_actual % 100 + 1 else 1900)


@pytest.mark.parametrize("yy,esperado", [
    (80, 1980),   # el caso Mackarena — antes daba 2080
    (99, 1999),
    (75, 1975),
    (27, 2027),   # año entrante, sigue siendo válido
    (26, 2026),
    (28, 1928),   # +2 años ya cae al siglo pasado; la cota lo descarta igual
    (1,  2001),
])
def test_ventana_de_siglo(yy, esperado):
    assert _resolver_anio(yy, 2026) == esperado


def test_ochenta_no_es_2080():
    """Regresión directa: el +2000 ciego mandaba a 2080."""
    assert _resolver_anio(80, 2026) == 1980
    assert _resolver_anio(80, 2026) != 2080


# ── Cota de sanidad ──────────────────────────────────────────────────────────

def _en_rango(cand: date, hoy: date) -> bool:
    """Misma cota que flows.py: hoy <= fecha <= hoy + 365 días."""
    return hoy <= cand <= hoy + timedelta(days=365)


@pytest.mark.parametrize("fecha", ["2080-12-04", "2001-01-16", "2009-12-21"])
def test_los_tres_casos_reales_quedan_descartados(fecha):
    """Los 3 saltos con año imposible que había en producción."""
    hoy = date(2026, 7, 21)
    assert not _en_rango(datetime.strptime(fecha, "%Y-%m-%d").date(), hoy)


def test_cota_acepta_fechas_razonables():
    hoy = date(2026, 7, 21)
    for delta in (0, 1, 30, 200, 365):
        assert _en_rango(hoy + timedelta(days=delta), hoy)


def test_cota_rechaza_pasado_y_lejano():
    hoy = date(2026, 7, 21)
    assert not _en_rango(hoy - timedelta(days=1), hoy)
    assert not _en_rango(hoy + timedelta(days=366), hoy)


# ── El año visible en el mensaje ─────────────────────────────────────────────

def test_fmt_fecha_muestra_anio_cuando_no_es_el_actual():
    """Sin el año, "Miércoles 4 de diciembre" de 2080 parece una fecha normal."""
    import medilink
    hoy = datetime.now(medilink._CHILE_TZ)
    otro_anio = hoy.year + 2
    salida = medilink._fmt_fecha(f"{otro_anio}-12-04")
    assert str(otro_anio) in salida, salida


def test_fmt_fecha_omite_anio_en_el_actual():
    """El caso normal no debe cambiar — el 99% de los mensajes son de este año."""
    import medilink
    hoy = datetime.now(medilink._CHILE_TZ)
    salida = medilink._fmt_fecha(f"{hoy.year}-12-04")
    assert str(hoy.year) not in salida
    assert "4 de diciembre" in salida


# ── Mes por substring (2026-08-19) ───────────────────────────────────────────
# El parser de solo-mes (flows.py, bloque 1b) hacía match por substring sin
# borde derecho e incluía abreviaturas de 3 letras. Casos reales:
#   "Es un adulto MAYOr"   → mayo 2027  (psiquiatría, 56959282309)
#   "Alonso MARquez"       → marzo 2027 (MG, 56961770276)
#   paciente llamado Julio → cita real creada el 01/07/2027 (56989050528)
# Regla nueva: solo nombres COMPLETOS de mes y como palabra entera (\b...\b).

import re as _re

_MESES_FULL_TEST = ("enero", "febrero", "marzo", "abril", "mayo", "junio",
                    "julio", "agosto", "septiembre", "octubre",
                    "noviembre", "diciembre")


def _detecta_mes(texto: str):
    """Réplica de la regla del bloque 1b post-fix."""
    for m in _MESES_FULL_TEST:
        if _re.search(rf"\b{m}\b", texto):
            return m
    return None


@pytest.mark.parametrize("texto", [
    "es un adulto mayor",          # caso real → mayo 2027
    "alonso marquez",              # caso real → marzo 2027
    "hora con el dr marquez",
    "mi novia dice que si",        # "nov" y "dic" eran abreviaturas válidas
    "quiero para dic",             # abreviatura sola ya no dispara solo-mes
    "se me agoto el bono",         # "ago"
    "la sra mayorga",
])
def test_mes_no_detectado_en_substring(texto):
    assert _detecta_mes(texto) is None, texto


@pytest.mark.parametrize("texto,mes", [
    ("para mayo", "mayo"),
    ("mayo", "mayo"),
    ("en septiembre porfa", "septiembre"),
    ("quiero hora para diciembre", "diciembre"),
    ("el mes de marzo", "marzo"),
])
def test_mes_detectado_como_palabra(texto, mes):
    assert _detecta_mes(texto) == mes
