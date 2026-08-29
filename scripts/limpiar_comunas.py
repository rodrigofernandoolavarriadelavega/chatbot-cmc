#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Limpieza batch del campo `comuna` de las fichas de Medilink.

POR QUE
-------
El campo `comuna` de la ficha cae por defecto en "ARAUCO". El dueno lo cazo el
2026-08-29 atendiendo a una paciente con direccion de Curanilahue y ficha que
decia Arauco. Este script usa `app/localidades_arauco.py` para leer la
DIRECCION (que es el dato que el paciente dicta de verdad) y corregir o rellenar
la comuna.

DOS CATEGORIAS, MUY DISTINTAS EN RIESGO
---------------------------------------
  A) RELLENO  — la ficha no tiene comuna y la direccion si dice de donde es.
                No pisa nada escrito por una persona. Es seguro.
  B) CONFLICTO — la ficha dice una comuna y la direccion dice otra.
                Esto SI pisa lo que alguien tecleo. Requiere --aplicar-conflictos
                explicito, y solo se tocan los de confianza alta.

Uso:
    python3 scripts/limpiar_comunas.py                      # dry-run, no escribe
    python3 scripts/limpiar_comunas.py --aplicar            # aplica solo RELLENO
    python3 scripts/limpiar_comunas.py --aplicar --aplicar-conflictos
    python3 scripts/limpiar_comunas.py --limite 50          # tope de escrituras

Corre EN EL VPS (necesita heatmap_cache.db y el token de Medilink del .env).
Escribe con `PUT /pacientes/{id}` body parcial — el mismo mecanismo que ya usa
`crear_paciente` para los campos opcionales.
"""
from __future__ import annotations
import argparse
import asyncio
import os
import sqlite3
import sys
from collections import Counter

sys.path.insert(0, "/opt/chatbot-cmc")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

HEATMAP = "/opt/chatbot-cmc/data/heatmap_cache.db"


def clasificar():
    """Devuelve (relleno, conflictos, stats) sin tocar nada."""
    from app.localidades_arauco import resolver

    con = sqlite3.connect(HEATMAP)
    relleno, conflictos = [], []
    stats = Counter()
    for pid, nombre, com, ciu, dire in con.execute(
        "SELECT id, nombre, comuna, ciudad, direccion FROM pacientes_heatmap"
    ):
        r = resolver(dire, com, ciu)
        tiene_campo = bool((com or "").strip() or (ciu or "").strip())
        if not r["comuna"] or r["comuna"] == "Fuera de la provincia":
            stats["sin_resolver"] += 1
            continue
        if r["fuente"] != "direccion":
            stats["solo_campo"] += 1
            continue
        fila = {"id": pid, "nombre": (nombre or "")[:28], "dir": (dire or "")[:44],
                "campo": (com or ciu or "").strip(), "comuna": r["comuna"],
                "sector": r["sector"], "conf": r["confianza"]}
        if not tiene_campo:
            relleno.append(fila); stats["relleno"] += 1
        elif r["conflicto"]:
            conflictos.append(fila); stats["conflicto"] += 1
        else:
            stats["ya_correcto"] += 1
    con.close()
    return relleno, conflictos, stats


async def escribir(filas, limite: int):
    """PUT /pacientes/{id} con {"comuna": ...}. Serial y con pausa: la API
    tiene rate limit y este script no compite con el bot en produccion."""
    import httpx
    from app.config import MEDILINK_BASE_URL
    from app.medilink import HEADERS

    ok = err = 0
    async with httpx.AsyncClient(timeout=15) as c:
        for i, f in enumerate(filas[:limite], 1):
            try:
                r = await c.put(f"{MEDILINK_BASE_URL}/pacientes/{f['id']}",
                                json={"comuna": f["comuna"]}, headers=HEADERS)
                if r.status_code in (200, 201):
                    ok += 1
                else:
                    err += 1
                    print("   ! %s -> %s %s" % (f["id"], r.status_code, r.text[:80]))
            except Exception as e:                                   # noqa: BLE001
                err += 1
                print("   ! %s -> %s" % (f["id"], e))
            if i % 20 == 0:
                print("   … %d/%d" % (i, min(len(filas), limite)))
            await asyncio.sleep(0.35)
    return ok, err


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--aplicar", action="store_true", help="escribe el RELLENO en Medilink")
    ap.add_argument("--aplicar-conflictos", action="store_true",
                    help="ademas pisa las fichas donde la direccion contradice al campo")
    ap.add_argument("--limite", type=int, default=500)
    a = ap.parse_args()

    relleno, conflictos, stats = clasificar()
    print("=== CLASIFICACION (nada escrito todavia)")
    for k in ("relleno", "conflicto", "ya_correcto", "solo_campo", "sin_resolver"):
        print("   %-14s %6d" % (k, stats[k]))

    print("\n=== A) RELLENO — ficha sin comuna, la direccion si la dice (%d)" % len(relleno))
    for f in relleno[:12]:
        print("   %-7s %-28s %-44s -> %s / %s" % (f["id"], f["nombre"], f["dir"], f["comuna"], f["sector"]))
    if len(relleno) > 12:
        print("   … y %d mas" % (len(relleno) - 12))
    por_comuna = Counter(f["comuna"] for f in relleno)
    print("   por comuna:", dict(por_comuna.most_common()))

    print("\n=== B) CONFLICTO — la direccion contradice al campo (%d)" % len(conflictos))
    for f in conflictos:
        print("   %-7s %-24s campo=%-14s dir dice=%-13s conf=%-6s | %s"
              % (f["id"], f["nombre"], f["campo"], f["comuna"], f["conf"], f["dir"]))

    if not a.aplicar:
        print("\n(dry-run: no se escribio nada. Agrega --aplicar)")
        return

    print("\n>>> escribiendo RELLENO (%d, tope %d)…" % (len(relleno), a.limite))
    ok, err = asyncio.run(escribir(relleno, a.limite))
    print("    relleno: %d ok · %d error" % (ok, err))

    if a.aplicar_conflictos:
        altos = [f for f in conflictos if f["conf"] == "alta"]
        print("\n>>> pisando CONFLICTOS de confianza alta (%d de %d)…"
              % (len(altos), len(conflictos)))
        ok2, err2 = asyncio.run(escribir(altos, a.limite))
        print("    conflictos: %d ok · %d error" % (ok2, err2))
    else:
        print("\n(los conflictos NO se tocaron: requieren --aplicar-conflictos)")


if __name__ == "__main__":
    main()
