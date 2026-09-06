#!/usr/bin/env python3
"""
Sube los respaldos del chatbot CMC a DigitalOcean Spaces.

Por qué existe: hasta hoy los respaldos vivían en el MISMO disco que la base que
respaldan. Eso protege contra un `DELETE` mal escrito, pero no contra perder el
servidor — y el propio `backup-cmc-db.sh` deja constancia de que ya pasó algo
así: «el incidente del symlink data/ (2026-06-10) borró uploads y NO había
backup → imágenes/PDFs de pacientes previos se perdieron».

Tres decisiones:

1. **Afuera se guarda MÁS historia que adentro.** En el disco del droplet el
   espacio es el problema (25 GB en total); en el Space hay 250 GB ya pagados.
   Local conserva 7+4+3; acá se conservan 30 diarios + 12 semanales + 12
   mensuales: un año de alcance, unos 13 GB. Sería un desperdicio guardar lo
   mismo en los dos lados.

2. **Se verifica lo que llegó, no lo que se mandó.** Después de subir se
   pregunta al Space cuánto pesa el objeto y se compara con el archivo local.
   Un respaldo que se subió a medias es peor que no tenerlo: da tranquilidad
   falsa.

3. **La llave es limitada a este bucket.** Si el servidor se ve comprometido, el
   daño máximo alcanza a `cmc-respaldos` y no a toda la cuenta. El respaldo
   tiene que sobrevivir al desastre que respalda.

Los archivos ya van cifrados desde el origen (SQLCipher con la misma key), así
que en el Space no queda nada legible sin la clave del chatbot.

    python3 subir-respaldos.py            sube lo que falte y purga
    python3 subir-respaldos.py --simular  no sube ni borra nada
"""
import os
import re
import sys
from collections import defaultdict
from datetime import datetime

CARPETA = "/opt/backups/chatbot-cmc"
ENV = "/etc/cmc-spaces.env"
PREFIJO = "chatbot-cmc/"
DIARIOS, SEMANAS, MESES = 30, 12, 12
PATRON = re.compile(r"^(?P<familia>[a-z_]+)_(?P<fecha>\d{8})_(?P<hora>\d{6})\.(?P<ext>.+)$")
SOBRAS = ("db-journal", "db-wal", "db-shm")


def config():
    cfg = {}
    with open(ENV) as f:
        for linea in f:
            linea = linea.strip()
            if linea and not linea.startswith("#") and "=" in linea:
                k, v = linea.split("=", 1)
                cfg[k.strip()] = v.strip()
    faltan = [k for k in ("SPACES_KEY", "SPACES_SECRET", "SPACES_BUCKET",
                          "SPACES_ENDPOINT", "SPACES_REGION") if not cfg.get(k)]
    if faltan:
        sys.exit(f"Faltan credenciales en {ENV}: {', '.join(faltan)}")
    return cfg


def cliente(cfg):
    import boto3
    return boto3.session.Session().client(
        "s3", region_name=cfg["SPACES_REGION"], endpoint_url=cfg["SPACES_ENDPOINT"],
        aws_access_key_id=cfg["SPACES_KEY"], aws_secret_access_key=cfg["SPACES_SECRET"])


def locales():
    salida = []
    for nombre in sorted(os.listdir(CARPETA)):
        m = PATRON.match(nombre)
        if not m or m["ext"] in SOBRAS:
            continue
        ruta = os.path.join(CARPETA, nombre)
        if os.path.isfile(ruta):
            salida.append({"nombre": nombre, "ruta": ruta,
                           "peso": os.path.getsize(ruta)})
    return salida


def remotos(s3, bucket):
    objetos, token = {}, None
    while True:
        kw = {"Bucket": bucket, "Prefix": PREFIJO}
        if token:
            kw["ContinuationToken"] = token
        r = s3.list_objects_v2(**kw)
        for o in r.get("Contents", []):
            objetos[o["Key"][len(PREFIJO):]] = o["Size"]
        if not r.get("IsTruncated"):
            break
        token = r.get("NextContinuationToken")
    return objetos


def a_conservar(nombres):
    """Misma idea que la purga local, con ventanas más largas: acá el espacio
    sobra y lo que escasea es la memoria hacia atrás."""
    fams = defaultdict(list)
    for n in nombres:
        m = PATRON.match(n)
        if m:
            fams[m["familia"]].append(
                (datetime.strptime(m["fecha"] + m["hora"], "%Y%m%d%H%M%S"), n))
    conservar = set()
    for archivos in fams.values():
        archivos.sort(reverse=True)
        conservar.update(n for _, n in archivos[:DIARIOS])
        for limite, clave in ((SEMANAS, lambda f: f.isocalendar()[:2]),
                              (MESES, lambda f: (f.year, f.month))):
            vistos = set()
            for f, n in archivos:
                k = clave(f)
                if k in vistos or len(vistos) >= limite:
                    continue
                vistos.add(k)
                conservar.add(n)
    return conservar


def mb(n):
    return f"{n/1048576:,.0f} MB".replace(",", ".")


def main():
    simular = "--simular" in sys.argv
    cfg = config()
    s3 = cliente(cfg)
    bucket = cfg["SPACES_BUCKET"]

    try:
        ya = remotos(s3, bucket)
    except Exception as e:
        sys.exit(f"[{datetime.now():%FT%T}] ERROR: no se pudo leer el Space: {e}")

    subidos = fallidos = 0
    for a in locales():
        if ya.get(a["nombre"]) == a["peso"]:
            continue                       # ya está, y del mismo tamaño
        motivo = "nuevo" if a["nombre"] not in ya else "estaba incompleto"
        print(f"  subiendo {a['nombre']} ({mb(a['peso'])}, {motivo})")
        if simular:
            continue
        clave = PREFIJO + a["nombre"]
        try:
            s3.upload_file(a["ruta"], bucket, clave)
            # Verificar lo que LLEGÓ, no lo que se mandó.
            remoto = s3.head_object(Bucket=bucket, Key=clave)["ContentLength"]
            if remoto != a["peso"]:
                raise IOError(f"llegó {remoto} de {a['peso']} bytes")
            subidos += 1
        except Exception as e:
            print(f"    ERROR: {a['nombre']} — {e}", file=sys.stderr)
            fallidos += 1

    ya = remotos(s3, bucket) if not simular else ya
    conservar = a_conservar(ya)
    sobran = [n for n in ya if n not in conservar]
    if sobran:
        print(f"  purgando {len(sobran)} copia(s) vieja(s) del Space "
              f"({mb(sum(ya[n] for n in sobran))})")
        if not simular:
            for i in range(0, len(sobran), 900):
                s3.delete_objects(Bucket=bucket, Delete={
                    "Objects": [{"Key": PREFIJO + n} for n in sobran[i:i+900]]})

    quedan = {n: p for n, p in ya.items() if n in conservar}
    print(f"[{datetime.now():%FT%T}] {'SIMULACIÓN · ' if simular else ''}"
          f"Space {bucket}: {len(quedan)} copias, {mb(sum(quedan.values()))}"
          f" · subidas {subidos} · fallidas {fallidos}")
    sys.exit(1 if fallidos else 0)


if __name__ == "__main__":
    main()
