"""Imputacion de comuna por telefono compartido.

Contexto: de 16.079 fichas, 6.391 no tienen comuna resoluble — y 6.055 de esas
(94,7%) no tienen NI direccion NI comuna NI ciudad, solo celular. El telefono es
la unica senal que queda para el 95% del punto ciego.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from imputacion_territorial import (Donantes, Ficha, backtest, imputar,  # noqa: E402
                                    normalizar_fono)


def test_mismo_movil_escrito_de_cuatro_formas_es_un_solo_hogar():
    formas = ["+56 9 8783 4148", "987834148", "56987834148", "9-8783-4148"]
    assert len({normalizar_fono(f) for f in formas}) == 1


def test_fono_corto_o_vacio_no_agrupa():
    for malo in (None, "", "123", "sin dato"):
        assert normalizar_fono(malo) is None


def test_imputa_desde_un_familiar_del_mismo_telefono():
    madre = Ficha(id=1, fono="87834148", comuna="Curanilahue", sector="Colico")
    hijo = Ficha(id=2, fono="87834148", comuna=None, sector=None)
    com, sec, fuente = imputar(hijo, Donantes.construir([madre]))
    assert (com, fuente) == ("Curanilahue", "telefono")


def test_donantes_que_discrepan_no_imputan_nada():
    """Un telefono con dos comunas es numero reciclado o cuidador de varias
    familias — justo donde la inferencia falla. Mejor sin dato que mal."""
    a = Ficha(id=1, fono="87834148", comuna="Arauco", sector=None)
    b = Ficha(id=2, fono="87834148", comuna="Curanilahue", sector=None)
    huerfano = Ficha(id=3, fono="87834148", comuna=None, sector=None)
    com, _s, fuente = imputar(huerfano, Donantes.construir([a, b]))
    assert com is None and fuente == "sin donante"


def test_nunca_pisa_el_dato_propio():
    propio = Ficha(id=1, fono="87834148", comuna="Arauco", sector="Carampangue")
    donante = Ficha(id=2, fono="87834148", comuna="Curanilahue", sector=None)
    com, sec, fuente = imputar(propio, Donantes.construir([donante]))
    assert (com, sec, fuente) == ("Arauco", "Carampangue", "propia")


def test_el_sector_no_se_imputa_por_defecto():
    """Medido: comuna acierta 95,5%, sector solo 76,8%. Uno de cada cuatro mal
    asignado en la variable que decide inversiones (Carampangue vs Arauco urbano).
    """
    madre = Ficha(id=1, fono="87834148", comuna="Arauco", sector="Arauco urbano")
    hijo = Ficha(id=2, fono="87834148", comuna=None, sector=None)
    d = Donantes.construir([madre])
    assert imputar(hijo, d)[1] is None
    assert imputar(hijo, d, con_sector=True)[1] == "Arauco urbano"   # solo si se pide


def test_sin_telefono_no_hay_nada_que_hacer():
    assert imputar(Ficha(3, None, None, None), Donantes({}, {}))[2] == "sin senal"


# ── regresion: el splat posicional que invirtio comuna y sector ────────────
def test_ficha_no_acepta_comuna_y_sector_invertidos_por_accidente():
    """Bug real (2026-09-07): `Ficha(pid, fono, *(sector, comuna))` metia la
    tupla al reves en un constructor (comuna, sector). Ambos son `str | None`,
    asi que nada fallo — el reporte termino con nombres de sector como claves de
    comuna. La defensa es construir SIEMPRE por nombre de campo.
    """
    f = Ficha(id=1, fono="87834148", comuna="Arauco", sector="Carampangue")
    assert f.comuna == "Arauco" and f.sector == "Carampangue"
    # un sector jamas debe poder pasar por comuna en los donantes
    d = Donantes.construir([f])
    assert set(d.comuna.values()) == {"Arauco"}
    assert set(d.sector.values()) == {"Carampangue"}


def test_backtest_solo_evalua_donde_la_imputacion_se_habria_activado():
    """Una ficha sin otro donante en su telefono no debe contar como acierto
    ni como error: la imputacion nunca se habria disparado ahi."""
    sola = Ficha(id=1, fono="11111111", comuna="Arauco", sector=None)
    par_a = Ficha(id=2, fono="22222222", comuna="Lebu", sector=None)
    par_b = Ficha(id=3, fono="22222222", comuna="Lebu", sector=None)
    r = backtest([sola, par_a, par_b])
    assert r["comuna_n"] == 2 and r["comuna_ok"] == 2
    assert r["comuna_pct"] == pytest.approx(100.0)
