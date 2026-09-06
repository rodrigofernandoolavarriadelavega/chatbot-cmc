#!/usr/bin/env python3
"""
Retención escalonada de los respaldos del chatbot CMC.

Reemplaza el `ls -1t | tail -n +31 | xargs rm` que había: ese conserva 30 copias
DIARIAS, o sea 8,3 GB para cubrir un solo mes, y no puede volver más atrás de
ahí. Además sólo mira archivos `.gz`, así que los respaldos que quedaron sin
comprimir (porque el gzip no alcanzó a terminar) son invisibles para él y no se
borran nunca: hoy hay 443 MB así.

La política de acá conserva, por cada familia de respaldo:

    · los 7 más recientes            (uno por día, la última semana)
    · 1 por semana, 4 semanas        (para volver a cualquier punto del mes)
    · 1 por mes, 3 meses             (para volver a un trimestre atrás)

Son ~14 copias en vez de 30, pero alcanzan 4 meses en vez de 1. Menos espacio y
más memoria: la profundidad de un respaldo no la da la cantidad de copias, la
da cómo están repartidas en el tiempo.

Lo que se borra son COPIAS de la base, nunca la base viva: `sessions.db` en
`/opt/chatbot-cmc/data/` no se toca ni se lee acá.

    python3 purgar-respaldos.py --simular    ← no borra, sólo muestra
    python3 purgar-respaldos.py              ← borra
"""
import os
import re
import sys
from collections import defaultdict
from datetime import date, datetime

CARPETA = "/opt/backups/chatbot-cmc"
DIARIOS, SEMANAS, MESES = 7, 4, 3
# familia → cómo reconocer sus archivos. Se toma el .db y el .db.gz juntos: un
# respaldo a medio comprimir es el respaldo de ese día igual.
PATRON = re.compile(r"^(?P<familia>[a-z_]+)_(?P<fecha>\d{8})_(?P<hora>\d{6})\.(?P<ext>.+)$")
# Un `-journal`, `-wal` o `-shm` NO es un respaldo: es basura que dejó una copia
# que se cortó a la mitad. Contarlo como copia hace creer que ese día está
# protegido cuando no lo está.
SOBRAS = ("db-journal", "db-wal", "db-shm")


def inventario():
    fams = defaultdict(list)
    for nombre in os.listdir(CARPETA):
        m = PATRON.match(nombre)
        if not m or m["ext"] in SOBRAS:
            continue
        ruta = os.path.join(CARPETA, nombre)
        if not os.path.isfile(ruta):
            continue
        try:
            f = datetime.strptime(m["fecha"] + m["hora"], "%Y%m%d%H%M%S")
        except ValueError:
            continue
        fams[m["familia"]].append(
            {"nombre": nombre, "ruta": ruta, "fecha": f,
             "peso": os.path.getsize(ruta)})
    for v in fams.values():
        v.sort(key=lambda x: x["fecha"], reverse=True)
    return fams


def decidir(archivos):
    """Devuelve (conservar, borrar). Un archivo se conserva si es de los N más
    recientes, o si es el más nuevo de su semana o de su mes."""
    conservar, motivo = set(), {}
    for a in archivos[:DIARIOS]:
        conservar.add(a["nombre"])
        motivo[a["nombre"]] = "última semana"

    vistas = set()
    for a in archivos:
        clave = a["fecha"].isocalendar()[:2]          # (año, semana)
        if clave in vistas or len(vistas) >= SEMANAS:
            continue
        vistas.add(clave)
        if a["nombre"] not in conservar:
            conservar.add(a["nombre"])
            motivo[a["nombre"]] = f"semana {clave[1]}"

    vistos = set()
    for a in archivos:
        clave = (a["fecha"].year, a["fecha"].month)
        if clave in vistos or len(vistos) >= MESES:
            continue
        vistos.add(clave)
        if a["nombre"] not in conservar:
            conservar.add(a["nombre"])
            motivo[a["nombre"]] = f"mes {clave[1]:02d}"

    return ([a for a in archivos if a["nombre"] in conservar], motivo,
            [a for a in archivos if a["nombre"] not in conservar])


def mb(n):
    return f"{n/1048576:,.0f} MB".replace(",", ".")


def main():
    simular = "--simular" in sys.argv
    fams = inventario()
    if not fams:
        print("No hay respaldos que revisar.")
        return
    total_libre = total_queda = 0
    for familia in sorted(fams):
        archivos = fams[familia]
        quedan, motivo, se_van = decidir(archivos)
        libre = sum(a["peso"] for a in se_van)
        queda = sum(a["peso"] for a in quedan)
        total_libre += libre
        total_queda += queda
        print(f"\n=== {familia} · {len(archivos)} copias, {mb(sum(a['peso'] for a in archivos))}")
        print(f"  SE CONSERVAN {len(quedan)} ({mb(queda)}):")
        for a in quedan:
            print(f"     {a['fecha']:%d-%m-%Y}  {mb(a['peso']):>9}  {motivo[a['nombre']]}")
        print(f"  SE BORRAN {len(se_van)} ({mb(libre)}):")
        for a in se_van[:4]:
            print(f"     {a['fecha']:%d-%m-%Y}  {mb(a['peso']):>9}")
        if len(se_van) > 4:
            print(f"     … y {len(se_van)-4} más, todas anteriores al "
                  f"{se_van[3]['fecha']:%d-%m-%Y}")
        if not simular:
            for a in se_van:
                os.remove(a["ruta"])

    # Un `.db` suelto significa que el gzip no llegó a correr: la copia de ese
    # día quedó a medias y nadie se enteró, porque el script sólo grita cuando
    # falla la verificación, no cuando el proceso se corta.
    sueltos = [n for n in os.listdir(CARPETA) if n.endswith(".db")
               and not os.path.exists(os.path.join(CARPETA, n + ".gz"))]
    if sueltos:
        print(f"\n⚠  {len(sueltos)} respaldo(s) quedaron SIN COMPRIMIR — el proceso se")
        print("   cortó esa noche. Revisar si sirven antes de confiar en ellos:")
        for n in sueltos:
            extra = " (con journal: quedó a medio escribir)" if os.path.exists(
                os.path.join(CARPETA, n + "-journal")) else ""
            print(f"     {n}{extra}")

    print(f"\n{'SIMULACIÓN — no se borró nada.' if simular else 'BORRADO HECHO.'}")
    print(f"  quedan {mb(total_queda)} · {'se liberarían' if simular else 'se liberaron'} {mb(total_libre)}")


if __name__ == "__main__":
    main()
