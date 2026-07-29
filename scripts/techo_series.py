#!/usr/bin/env python3
"""Serie mensual venta + ganancia CMC por profesional, para el dashboard /techo.

NO reimplementa el SELECT: la venta sale de verdad.plata_por_profesional()
(caja real, bi_pagos_caja) y la ganancia de la MISMA regla que /cmc/ebitda
(pct de equipo_cmc, honorario fijo de Abarca). Si esas reglas cambian, este
script las hereda — es el punto del Libro de la Verdad.

Uso (en el VPS):  venv/bin/python3 scripts/techo_series.py 2025-01 2026-06
Escupe JSON a stdout.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import verdad
from ebitda_routes import PCT_DEFAULT, honorario_fijo
from session import db


# Profesionales que salieron del dict PROFESIONALES (ya no atienden) pero SÍ
# tienen caja en el periodo. Resueltos contra Medilink /profesionales/{id}.
# El 1001 es un id de caja: no existe en Medilink (404); sus atenciones apuntan
# al id 36 = Tomás Araneda.
NOMBRES_HISTORICOS = {
    1001: ("Dr. Tomás Araneda", "Medicina General"),      # atenciones → id 36
    64:   ("Dr. Claudio Barraza", "Traumatología"),
    1027: ("Camila Enríquez", "Psicología"),              # atenciones → id 71
    1023: ("Dr. Cristóbal Martínez", "Odontología"),      # atenciones → id 54
}


def meses(desde: str, hasta: str) -> list[str]:
    y, m = int(desde[:4]), int(desde[5:7])
    out = []
    while f"{y:04d}-{m:02d}" <= hasta:
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def main(desde: str, hasta: str) -> None:
    ms = meses(desde, hasta)

    # pct de honorario por profesional (misma tabla que usa /cmc/ebitda)
    with db() as c:
        pct_map = {
            r[0]: (r[1] or 0)
            for r in c.execute(
                "SELECT id_medilink, pct_honorario FROM equipo_cmc "
                "WHERE id_medilink IS NOT NULL"
            ).fetchall()
        }

    serie: dict[str, dict] = {}      # nombre -> {id, venta:[...], gan:[...]}
    advertencias: list[str] = []

    for i, mes in enumerate(ms):
        d, h = verdad.mes_bounds(mes)
        res = verdad.plata_por_profesional(d, h, incluir_sin_asignar=False)
        for a in res["_advertencias"]:
            advertencias.append(f"{mes}: {a}")

        for p in res["profesionales"]:
            pid, nom, venta = p["id_profesional"], p["nombre"], p["ingreso"]
            esp = p["especialidad"]
            if pid in NOMBRES_HISTORICOS:
                nom, esp = NOMBRES_HISTORICOS[pid]

            fijo = honorario_fijo(pid, mes)
            if fijo is not None:
                bruto = fijo                      # Abarca: contrato fijo, CMC puede ser < 0
            else:
                # OJO: .get(pid, DEFAULT) NO cubre un pct guardado como 0 —
                # la clave existe y devuelve 0, dejándole al CMC el 100%.
                # (Era el caso de Valdés y Acosta; corregido en equipo_cmc.)
                pct = pct_map.get(pid) or PCT_DEFAULT
                bruto = round(venta * pct / 100)

            e = serie.setdefault(nom, {
                "id": pid,
                "esp": esp,
                "venta": [0] * len(ms),
                "gan": [0] * len(ms),
            })
            e["venta"][i] = venta
            e["gan"][i] = venta - bruto

    # Abarca cobra su fijo aunque no haya facturado nada ese mes: si no aparece
    # en la caja de un mes, su ganancia NO es 0, es −fijo. Solo aplica a meses
    # dentro de su periodo de contrato (los que sí tienen actividad alrededor).
    for nom, e in serie.items():
        if e["id"] == 73:
            activos = [i for i, v in enumerate(e["venta"]) if v > 0]
            if activos:
                for i in range(min(activos), max(activos) + 1):
                    if e["venta"][i] == 0:
                        e["gan"][i] = -honorario_fijo(73, ms[i])
                        advertencias.append(
                            f"{ms[i]}: Abarca sin caja pero con contrato fijo vigente "
                            f"→ ganancia negativa imputada."
                        )

    print(json.dumps({
        "meses": ms,
        "profesionales": serie,
        "_fuente": "verdad.plata_por_profesional (bi_pagos_caja) + regla honorario de ebitda_routes",
        "_advertencias": advertencias,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "2025-01",
         sys.argv[2] if len(sys.argv) > 2 else "2026-06")
