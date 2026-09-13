# -*- coding: utf-8 -*-
"""Baja poblacion por localidad del Censo 2024 (INE) y emite POBLACION_SECTOR.

FUENTE
------
Feature Service publico "Microdatos Censo 2024" (solutions_EsriChile, alojado por
INE Chile en ArcGIS Online). Es el mismo dato del visor manzana-entidad de
censo2024.ine.gob.cl, pero consultable por API — no hay CSV de descarga directa.

    .../Microdatos_Censo_2024/FeatureServer/0  -> Manzanas          (URBANO)
    .../Microdatos_Censo_2024/FeatureServer/1  -> Manzanas-entidades (RURAL)

**Hay que sumar las dos capas.** La capa 1 sola da Carampangue = 363 habitantes,
que no es el pueblo: es solo su periferia rural. El estrato urbano vive en la
capa 0. Confundirlas subestima cada pueblo en un orden de magnitud.

POR QUE REEMPLAZA AL RESCALADO 2017
-----------------------------------
`territorio.POBLACION_SECTOR` usaba distritos del Censo 2017 reescalados por el
crecimiento comunal — confianza "media". Contra el dato real de 2024 ese supuesto
fallaba justo donde mas importa:

    Carampangue    estimado 4.688   real ~3.548   (+32% inflado)
    Ramadillas     estimado 3.021   real ~2.130   (+42%)
    Arauco urbano  estimado 16.379  real ~17.615  (-7%)

Un denominador 32% inflado en Carampangue significaba una penetracion 24% baja
justo en el sector donde se evaluan inversiones (Monsalve 168, edificio propio).

    python3 scripts/censo2024_entidades.py [provincia]
"""
from __future__ import annotations

import json
import ssl
import subprocess
import sys
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from localidades_arauco import COMUNA_DISPLAY, LOCALIDADES, POBLACION  # noqa: E402

# sector -> comuna, segun el diccionario. Un nombre de sector pertenece a UNA
# comuna: es lo que hace que la clave sea usable como identificador.
SECTOR_COMUNA = {v[1]: v[0] for v in LOCALIDADES.values()}

BASE = ("https://services.arcgis.com/r7t1P5pnkoOLRdhr/arcgis/rest/services/"
        "Microdatos_Censo_2024/FeatureServer/{capa}/query")
PROVINCIA = sys.argv[1] if len(sys.argv) > 1 else "Arauco"


def _get(url: str) -> str:
    """GET con el trust store del sistema.

    El Python.org de macOS viene sin CA bundle y falla con
    CERTIFICATE_VERIFY_FAILED contra ArcGIS. Se usa certifi si esta, y si no se
    cae a curl, que en macOS si usa el llavero del sistema. Nunca se desactiva
    la verificacion: bajar datos oficiales sin validar el certificado es
    exactamente como se envenena una fuente.
    """
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
        with urllib.request.urlopen(url, timeout=60, context=ctx) as r:
            return r.read().decode()
    except Exception:
        return subprocess.run(["curl", "-sS", "-m", "60", url],
                              capture_output=True, text=True, check=True).stdout


def _consultar(capa: int, provincia: str) -> list[dict]:
    p = urllib.parse.urlencode({
        "where": f"NOM_PROVINCIA='{provincia}'",
        "groupByFieldsForStatistics": "NOM_COMUNA,NOM_LOCALIDAD",
        "outStatistics": json.dumps([{"statisticType": "sum",
                                      "onStatisticField": "n_per",
                                      "outStatisticFieldName": "pob"}]),
        "resultRecordCount": 500, "f": "json"})
    url = f"{BASE.format(capa=capa)}?{p}"
    d = json.loads(_get(url))
    if "error" in d:
        raise SystemExit(f"capa {capa}: {d['error']}")
    return [f["attributes"] for f in d["features"]]


def _a_sector(nom_localidad: str, comuna_display: str, emitibles: set[str]) -> str | None:
    """Traduce el nombre INE al nombre de sector de localidades_arauco.

    La regla no trivial: el INE llama a la ciudad igual que a la comuna ("Arauco"),
    y localidades_arauco la llama "Arauco urbano" — precisamente para que ciudad y
    comuna no colisionen. Esa colision ya costo un bug (ver poblacion_de).
    """
    for cand in (nom_localidad, f"{nom_localidad} urbano",
                 f"{comuna_display} urbano" if nom_localidad == comuna_display else None):
        if cand and cand in emitibles:
            return cand
    return None


def main():
    emitibles = {v[1] for v in LOCALIDADES.values()}
    pob: dict[tuple[str, str], float] = defaultdict(float)
    sin_mapear: dict[str, float] = defaultdict(float)
    homonimos: dict[str, float] = defaultdict(float)
    inv_comuna = {v: k for k, v in COMUNA_DISPLAY.items()}

    for capa in (0, 1):                       # 0 urbano + 1 rural: las DOS
        for a in _consultar(capa, PROVINCIA):
            comuna_ine, loc, n = a["NOM_COMUNA"], a["NOM_LOCALIDAD"], a["pob"] or 0
            if not loc or loc == "Indeterminada":
                continue
            sector = _a_sector(loc, comuna_ine, emitibles)
            # HOMONIMOS entre comunas: el INE tiene un Quidico en Arauco y otro en
            # Tirua, un Huillinco en Canete y otro en Contulmo, un Ponotro en
            # Canete y otro en Tirua. Como POBLACION_SECTOR se indexa por nombre,
            # emitir ambos produce un dict literal con clave repetida — y Python
            # se queda CALLADO con la ultima, borrando la otra. Solo se acepta la
            # fila cuya comuna coincide con la que el diccionario le asigna a ese
            # sector; la homonima se reporta aparte, nunca se suma.
            comuna_clave = inv_comuna.get(comuna_ine, comuna_ine)
            if sector and SECTOR_COMUNA.get(sector) == comuna_clave:
                pob[(comuna_ine, sector)] += n
            elif sector:
                homonimos[f"{comuna_ine} / {loc} (el diccionario lo tiene en "
                          f"{SECTOR_COMUNA.get(sector)})"] += n
            else:
                sin_mapear[f"{comuna_ine} / {loc}"] += n

    inv = inv_comuna
    print("POBLACION_SECTOR: dict[str, tuple[int, str, Confianza]] = {")
    for (comuna_ine, sector), n in sorted(pob.items(), key=lambda kv: (kv[0][0], -kv[1])):
        print(f'    {sector!r:<24}: ({round(n):>6}, "Censo 2024 INE localidad '
              f'(urbano+rural)", "alta"),   # {inv.get(comuna_ine, comuna_ine)}')
    print("}")

    suma = defaultdict(float)
    for (comuna_ine, _s), n in pob.items():
        suma[inv.get(comuna_ine, comuna_ine)] += n
    print("\n# cobertura: localidades mapeadas / total comunal Censo 2024")
    for comuna, n in sorted(suma.items()):
        tot = POBLACION.get(comuna)
        print(f"#   {comuna:<14}{n:>8,.0f} / {tot:>8,}  {n/tot*100:5.1f}%" if tot
              else f"#   {comuna:<14}{n:>8,.0f}")
    claves = [sec for _c, sec in pob]
    assert len(claves) == len(set(claves)), f"clave repetida: {claves}"
    if homonimos:
        print("\n# HOMONIMOS descartados (mismo nombre, otra comuna que la del "
              "diccionario) — revisar si merecen entrada propia:")
        for k, n in sorted(homonimos.items(), key=lambda kv: -kv[1]):
            print(f"#   {n:>7,.0f}  {k}")
    if sin_mapear:
        print("\n# localidades del INE SIN sector en localidades_arauco "
              "(agregar al diccionario para poder medirlas):")
        for k, n in sorted(sin_mapear.items(), key=lambda kv: -kv[1]):
            print(f"#   {n:>7,.0f}  {k}")


if __name__ == "__main__":
    main()
