# -*- coding: utf-8 -*-
"""Carga poblacion por entidad poblada del Censo 2024 y valida antes de aceptarla.

POR QUE EXISTE
--------------
`territorio.POBLACION_SECTOR` usa hoy distritos del Censo **2017** reescalados al
total comunal 2024 — confianza "media", con dos supuestos incomodos: que la
distribucion interna de la comuna no cambio en 7 anos, y que el distrito censal
aproxima al area de influencia. El Censo 2024 a nivel entidad poblada elimina
ambos y sube la confianza a "alta".

DE DONDE SE BAJA (verificado 2026-09-07)
----------------------------------------
No hay CSV de descarga directa en censo2024.ine.gob.cl. Las dos vias reales:

  1. **Redatam Web** — procesa microdatos en linea y exporta:
     https://redatam.ine.gob.cl/redbin/RpWebEngine.exe/Portal?BASE=CENSO_2024
     Cruce: entidad poblada x total de personas, filtrando Region del Biobio,
     Provincia de Arauco.
  2. **Visor manzana-entidad** (ArcGIS):
     https://experience.arcgis.com/experience/53c18d70045b456a88b54b323bdc919f

Formato esperado del CSV (encabezado exacto, separador coma o punto y coma):

    comuna,entidad,poblacion
    Arauco,Carampangue,4812
    Arauco,Laraquete,5240

QUE VALIDA ANTES DE ACEPTAR
---------------------------
1. Que la suma de entidades de una comuna no supere el total comunal del Censo
   2024. Si lo supera, hay entidades duplicadas o mal asignadas.
2. Que cada `entidad` corresponda a un sector que `localidades_arauco` sepa
   emitir. Una entidad sin sector es una clave huerfana: aportaria una fila de
   0% permanente que se lee "ahi no hay nadie" en vez de "no lo miramos".
3. Que ninguna poblacion sea 0 o negativa.

Nada se escribe si alguna validacion falla. Emite el bloque Python listo para
reemplazar `_D2017` en `app/territorio.py`.

    python3 scripts/cargar_censo_entidades.py entidades_arauco.csv
"""
from __future__ import annotations

import csv
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from localidades_arauco import LOCALIDADES, POBLACION      # noqa: E402


def leer(path: str) -> list[tuple[str, str, int]]:
    texto = Path(path).read_text(encoding="utf-8-sig")
    sep = ";" if texto.splitlines()[0].count(";") > texto.splitlines()[0].count(",") else ","
    filas = []
    for i, r in enumerate(csv.DictReader(texto.splitlines(), delimiter=sep), start=2):
        faltan = {"comuna", "entidad", "poblacion"} - set(r)
        if faltan:
            raise SystemExit(f"linea {i}: faltan columnas {sorted(faltan)}")
        filas.append((r["comuna"].strip(), r["entidad"].strip(),
                      int(str(r["poblacion"]).replace(".", "").replace(",", "").strip())))
    return filas


def validar(filas) -> list[str]:
    errores, suma = [], defaultdict(int)
    emitibles = {v[1] for v in LOCALIDADES.values()}
    for comuna, entidad, pob in filas:
        if comuna not in POBLACION:
            errores.append(f"comuna desconocida: {comuna!r}")
            continue
        if pob <= 0:
            errores.append(f"{entidad}: poblacion {pob} no es valida")
        if entidad not in emitibles:
            errores.append(
                f"{entidad!r} no es un sector que localidades_arauco pueda emitir "
                f"— agregalo al diccionario primero, o la fila diria 0% para siempre")
        suma[comuna] += pob
    for comuna, total in suma.items():
        if total > POBLACION[comuna]:
            errores.append(f"{comuna}: entidades suman {total:,} > comuna "
                           f"{POBLACION[comuna]:,} (Censo 2024) — hay duplicados")
    return errores


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    filas = leer(sys.argv[1])
    errores = validar(filas)
    if errores:
        print("NO SE ACEPTA — corregir primero:\n")
        for e in errores:
            print("  ✗", e)
        raise SystemExit(1)

    cobertura = defaultdict(int)
    for comuna, _e, pob in filas:
        cobertura[comuna] += pob
    print("validado. cobertura por comuna (entidades / total comunal):")
    for comuna, total in sorted(cobertura.items()):
        print(f"  {comuna:<14} {total:>7,} / {POBLACION[comuna]:>7,}"
              f"  {total/POBLACION[comuna]*100:5.1f}%")
    print("\n# ── reemplazar _D2017 y POBLACION_SECTOR en app/territorio.py ──")
    print("POBLACION_SECTOR: dict[str, tuple[int, str, Confianza]] = {")
    for comuna, entidad, pob in sorted(filas):
        print(f'    {entidad!r:<24}: ({pob:>6}, "Censo 2024 INE entidad poblada", "alta"),')
    print("}")


if __name__ == "__main__":
    main()
