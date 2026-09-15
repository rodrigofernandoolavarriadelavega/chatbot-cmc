# -*- coding: utf-8 -*-
"""Imputar comuna/sector a pacientes sin direccion, por telefono compartido.

POR QUE
-------
Diagnostico del 2026-09-07 sobre 16.079 fichas: **6.391 no tienen comuna
resoluble, y 6.055 de esas (94,7%) no tienen NI direccion, NI comuna, NI ciudad
— solo celular.** Un diccionario de calles no los rescata. El telefono es la
unica senal que queda, y es la idea del dueno.

LA INFERENCIA
-------------
Dos fichas que comparten celular son casi siempre el mismo hogar (madre que
inscribe a los hijos, pareja, abuelo sin telefono propio). Si una de ellas tiene
domicilio resuelto, la otra vive en la misma comuna con alta probabilidad.

GUARDRAILES
-----------
1. **Unanimidad.** Si los donantes de un mismo telefono discrepan, NO se imputa.
   Un telefono con dos comunas distintas es un numero reciclado o un cuidador de
   varias familias — justo los casos donde la inferencia falla.
2. **Nunca pisa un dato propio.** Solo se imputa a quien no tiene nada.
3. **La confianza baja un escalon.** Un dato imputado jamas se reporta como
   medido; `fuente="telefono"` viaja con el resultado hasta el reporte.
4. **Se imputa comuna, NO sector.** Medido el 2026-09-07 sobre las 16.079 fichas:
   comuna acierta **95,5%** (n=3.482), sector solo **76,8%** (n=482). Compartir
   hogar implica comuna casi siempre; no implica sector — una madre en Arauco
   urbano inscribe a un hijo que vive en Carampangue. 1 de cada 4 mal asignado
   es inaceptable en la variable que decide inversiones (Carampangue vs Arauco
   urbano). `imputar(..., con_sector=True)` existe para poder re-medirlo, pero
   viene apagado a proposito.

Este modulo NO manda mensajes ni expone datos de terceros: solo agrega una
etiqueta geografica a fichas propias. Ley 21.719 — el dato imputado es
inferencia interna, no se le muestra al paciente ni se usa para contactarlo.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable

__all__ = ["normalizar_fono", "Ficha", "Donantes", "imputar", "backtest"]

_NO_DIGITO = re.compile(r"\D+")


def normalizar_fono(cel: str | None) -> str | None:
    """Deja solo los 8 digitos finales del movil chileno.

    Los mismos 8 digitos aparecen como '+56 9 8783 4148', '987834148',
    '56987834148' y '9-8783-4148'. Sin normalizar, el mismo hogar queda en
    cuatro grupos distintos y la imputacion no encuentra donante.
    """
    d = _NO_DIGITO.sub("", cel or "")
    return d[-8:] if len(d) >= 8 else None


@dataclass(frozen=True)
class Ficha:
    id: int
    fono: str | None
    comuna: str | None
    sector: str | None


@dataclass(frozen=True)
class Donantes:
    """Por telefono, la comuna y el sector en que TODOS sus donantes coinciden."""
    comuna: dict[str, str]
    sector: dict[str, str]

    @classmethod
    def construir(cls, fichas: Iterable[Ficha]) -> "Donantes":
        com: dict[str, set[str]] = defaultdict(set)
        sec: dict[str, set[str]] = defaultdict(set)
        for f in fichas:
            if not f.fono:
                continue
            if f.comuna:
                com[f.fono].add(f.comuna)
            if f.sector and "sin dato" not in f.sector:
                sec[f.fono].add(f.sector)
        # unanimidad: un solo valor distinto, o no se usa
        return cls({k: next(iter(v)) for k, v in com.items() if len(v) == 1},
                   {k: next(iter(v)) for k, v in sec.items() if len(v) == 1})


# Acierto medido del backtest, para que el numero viva junto al codigo que lo usa.
ACIERTO_COMUNA_PCT = 95.5      # n=3.482
ACIERTO_SECTOR_PCT = 76.8      # n=482  -> demasiado bajo: apagado por defecto


def imputar(f: Ficha, d: Donantes,
            con_sector: bool = False) -> tuple[str | None, str | None, str]:
    """Devuelve (comuna, sector, fuente). Nunca pisa un dato que la ficha ya tiene.

    `con_sector` viene apagado: el sector imputado acierta 76,8% y se usaria
    justo donde el error es mas caro. Ver el encabezado del modulo.
    """
    if f.comuna:
        return (f.comuna, f.sector, "propia")
    if not f.fono:
        return (None, None, "sin senal")
    com = d.comuna.get(f.fono)
    if not com:
        return (None, None, "sin donante")
    return (com, d.sector.get(f.fono) if con_sector else None, "telefono")


def backtest(fichas: list[Ficha]) -> dict:
    """Mide el acierto ocultando fichas que SI tienen domicilio, una a una.

    Solo se evaluan las que tienen al menos otro donante en su mismo telefono:
    son las unicas donde la imputacion se habria activado. Comuna y sector se
    puntuan por separado — compartir hogar implica comuna, no siempre sector.
    """
    por_fono: dict[str, list[Ficha]] = defaultdict(list)
    for f in fichas:
        if f.fono:
            por_fono[f.fono].append(f)

    r = {"comuna_n": 0, "comuna_ok": 0, "sector_n": 0, "sector_ok": 0, "sin_donante": 0}
    for f in fichas:
        if not f.comuna or not f.fono:
            continue
        otros = [o for o in por_fono[f.fono] if o.id != f.id]
        if not otros:
            continue
        d = Donantes.construir(otros)
        com, sec, fuente = imputar(Ficha(f.id, f.fono, None, None), d)
        if fuente != "telefono":
            r["sin_donante"] += 1
            continue
        r["comuna_n"] += 1
        r["comuna_ok"] += (com == f.comuna)
        if f.sector and "sin dato" not in f.sector and sec:
            r["sector_n"] += 1
            r["sector_ok"] += (sec == f.sector)
    for nivel in ("comuna", "sector"):
        n, ok = r[f"{nivel}_n"], r[f"{nivel}_ok"]
        r[f"{nivel}_pct"] = ok / n * 100 if n else None
    return r
