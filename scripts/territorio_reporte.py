# -*- coding: utf-8 -*-
"""Reporte Capa 1: penetracion del CMC por comuna y sector.

Lee heatmap_cache.db (citas + pacientes), resuelve cada paciente a sector con
localidades_arauco, y aplica el motor de territorio.

    PYTHONPATH=app python3 scripts/territorio_reporte.py [YYYY-MM-DD]
"""
import sqlite3
import sys
from collections import defaultdict
from datetime import date

sys.path.insert(0, "app")
from localidades_arauco import (resolver, COMUNAS, COMUNA_DISPLAY,   # noqa: E402
                                LOCALIDADES)

# sector -> comuna, segun el diccionario. Reemplaza a territorio._D2017, que se
# elimino al pasar del Censo 2017 reescalado al Censo 2024 por localidad.
SECTOR_COMUNA = {v[1]: v[0] for v in LOCALIDADES.values()}
import territorio as t                                            # noqa: E402
from imputacion_territorial import (Ficha, Donantes, imputar,     # noqa: E402
                                    normalizar_fono, ACIERTO_COMUNA_PCT)

DB = "data/heatmap_cache.db"


def _ficha(pid: int, direccion, comuna, ciudad, celular) -> Ficha:
    """Construye la Ficha SIEMPRE por nombre de campo.

    Aca hubo un bug real (2026-09-07): `Ficha(pid, fono, *_ubicar(...))` metia
    una tupla (sector, comuna) en un constructor (comuna, sector). Los dos campos
    son `str | None`, asi que nada se quejo — el dato entro invertido y el reporte
    termino con nombres de sector como claves de comuna. Con kwargs no se puede.
    """
    r = resolver(direccion, comuna, ciudad)
    return Ficha(id=pid, fono=normalizar_fono(celular),
                 comuna=r["comuna"], sector=r["sector"])
HASTA = sys.argv[1] if len(sys.argv) > 1 else date.today().isoformat()


def cargar():
    """Atenciones EFECTIVAS por paciente, con su sector resuelto.

    Solo estado 'atendida': una cita 'agendada' es una promesa, no demanda
    capturada. Y solo hasta la fecha de corte — la base trae citas hasta 2027.
    """
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    filas = con.execute("""
        SELECT c.id_paciente, c.fecha, p.direccion, p.comuna, p.ciudad, p.celular
          FROM citas_heatmap c JOIN pacientes_heatmap p ON p.id = c.id_paciente
         WHERE c.estado_cita = 'atendida' AND c.fecha <= ?
    """, (HASTA,)).fetchall()
    # Los donantes se construyen sobre TODAS las fichas, no solo las atendidas:
    # el familiar que aporta el domicilio puede no haberse atendido nunca.
    todas = [_ficha(pid, d, co, ci, cel) for pid, d, co, ci, cel in
             con.execute("SELECT id, direccion, comuna, ciudad, celular FROM pacientes_heatmap")]
    con.close()
    donantes = Donantes.construir(todas)

    ubic: dict[int, tuple[str | None, str | None]] = {}
    imputados = set()
    # comunas y sectores viven en diccionarios SEPARADOS: "Arauco" es ambas cosas
    # y mezclarlos fue exactamente el bug del 2026-09-07.
    por_comuna: dict[str, dict[int, list[t.Visita]]] = defaultdict(lambda: defaultdict(list))
    por_sector: dict[str, dict[int, list[t.Visita]]] = defaultdict(lambda: defaultdict(list))
    sin_ubicar = set()
    for pid, fecha, direccion, comuna, ciudad, celular in filas:
        if pid not in ubic:
            f = _ficha(pid, direccion, comuna, ciudad, celular)
            com = f.comuna
            if com is None:
                # sin domicilio propio: la unica senal es el telefono compartido
                com_i, _sec, fuente = imputar(
                    Ficha(id=pid, fono=f.fono, comuna=None, sector=None), donantes)
                if fuente == "telefono":
                    com, imputados = com_i, imputados | {pid}
            ubic[pid] = (f.sector, com)
        sector, comuna_r = ubic[pid]
        if comuna_r is None:
            sin_ubicar.add(pid)
            continue
        v = t.Visita(pid, fecha, sector, comuna_r)
        por_comuna[comuna_r][pid].append(v)
        if sector and sector in t.POBLACION_SECTOR:
            por_sector[sector][pid].append(v)
    return por_comuna, por_sector, len(ubic), len(sin_ubicar), len(filas), len(imputados)


def linea(p: t.Penetracion) -> str:
    if p.pct is None:
        return f"  {p.display:<26} {'—':>8}  {p.capturados:>6}  {p.parciales:>6}   sin denominador"
    marca = "" if p.confianza == "alta" else "  ~"
    return (f"  {p.display:<26} {p.habitantes:>8,}  {p.capturados:>6}  {p.parciales:>6}"
            f"  {p.pct:>6.2f}%  {p.pct_con_parciales:>6.2f}%{marca}")


def main():
    por_comuna, por_sector, n_pac, n_sin, n_at, n_imp = cargar()
    print(f"\nPENETRACION CMC — corte {HASTA} · ventana {t.VENTANA_MESES} meses")
    print(f"{n_at:,} atenciones · {n_pac:,} pacientes · {n_sin:,} sin comuna resoluble")
    print(f"{n_imp:,} pacientes con comuna IMPUTADA por telefono compartido "
          f"(acierto medido {ACIERTO_COMUNA_PCT}%); el sector nunca se imputa\n")
    hdr = f"  {'ambito':<26} {'habs':>8}  {'capt.':>6}  {'parc.':>6}  {'pen.':>7}  {'+parc':>7}"
    for titulo, ambitos, nivel, datos in (
            ("COMUNAS", COMUNAS, "comuna", por_comuna),
            ("SECTORES", tuple(sorted(t.POBLACION_SECTOR)), "sector", por_sector)):
        print(f"{titulo}\n{hdr}\n  {'-' * 72}")
        filas = [t.penetracion(a, datos.get(a, {}), HASTA, nivel) for a in ambitos]
        for p in sorted(filas, key=lambda x: -(x.pct or -1)):
            print(linea(p))
        print()
    # COBERTURA DE SECTOR — sin esto la tabla de sectores se lee mal.
    # Un vecino de Carampangue escribe "Carampangue" en su direccion; uno de la
    # ciudad de Arauco escribe solo la calle y cae en "sector sin dato". Por eso
    # los sectores rurales estan MEJOR medidos que la ciudad, y comparar
    # 7,64% de Carampangue contra 1,35% de Arauco urbano seria un error: el
    # segundo es un PISO, no una medicion.
    print("COBERTURA DE SECTOR (que fraccion de los capturados de cada comuna")
    print("se pudo asignar a un sector; el resto quedo en 'sector sin dato')\n")
    for comuna in COMUNAS:
        cap_com = t.penetracion(comuna, por_comuna.get(comuna, {}), HASTA, "comuna").capturados
        if not cap_com:
            continue
        cap_sec = sum(t.penetracion(sec, por_sector.get(sec, {}), HASTA, "sector").capturados
                      for sec in t.POBLACION_SECTOR
                      if SECTOR_COMUNA.get(sec) == comuna)
        print(f"  {COMUNA_DISPLAY.get(comuna, comuna):<26} {cap_sec:>5} / {cap_com:<5}"
              f"  {cap_sec / cap_com * 100:>5.1f}%")
    print()
    print("  ~ = denominador de confianza no-alta (hoy no deberia aparecer "
          "ninguno: todos son Censo 2024)")
    print("  'capt.' = >=2 atenciones en 24m separadas >=90d · 'parc.' = un solo episodio\n")


if __name__ == "__main__":
    main()
