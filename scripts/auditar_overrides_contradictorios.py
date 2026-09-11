#!/usr/bin/env python3
"""AUDITA los overrides de atribución que se contradicen a sí mismos.

Un override de `bi_pago_overrides` guarda (pago_id, id_profesional, atencion_id).
Cuando el `id_profesional` NO es el de esa `atencion_id`, el registro afirma dos
cosas incompatibles: "este pago es del profesional X" y "este pago corresponde a
la atención de Y". La repasada automática (`aplicar_repasada`) los producía al
cruzar caja contra `pagos_cmc` por (fecha, nombre) sin mirar la atención del
propio pago: si recepción tipeaba mal el nombre —apellidos duplicados, por
ejemplo— la segunda fila del día dejaba de agrupar, el cruce veía "un solo
profesional" y pisaba el pago del otro. Caso testigo: pago 37756, kinesiología
de Luis Armijo reasignada a Dr. Abarca (Medilink lo lista en Armijo).

SOLO LISTA: no escribe. Se probó revertirlos en masa (devolver cada pago al
profesional de su atención) y se DESCARTÓ — contrastado contra el informe real de
Medilink de kinesiología de agosto 2026, 3 de 4 reversiones eran incorrectas: ahí
el override de recepción coincidía con Medilink y el eslabón malo era la atención,
que el cascade había adivinado. Ninguna de las dos fuentes manda por sí sola.

La verdad del reparto vive en Medilink: un pago se distribuye entre TRATAMIENTOS,
cada uno con su profesional (su informe "Pagos período" muestra $11.390 de un pago
de $26.520). `bi_pagos_caja` guarda 1 pago = 1 fila = 1 profesional, así que ese
reparto no se puede representar; `/pagos` de la API tampoco lo expone. Mientras eso
no cambie, estos casos se resuelven a mano con el informe de Medilink al lado
(botón Reasignar del módulo Pagos), y esta auditoría dice cuáles mirar.

Uso:
    python3 scripts/auditar_overrides_contradictorios.py --desde 2026-08-01
"""
import argparse
import sys
from collections import defaultdict

# `app/medilink.py` usa imports planos (`from config import ...`), así que el
# directorio app/ tiene que estar en sys.path o el import revienta con
# ModuleNotFoundError: config — y los nombres salen como "Prof 73".
for _p in ("/opt/chatbot-cmc", "/opt/chatbot-cmc/app", ".", "app"):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from app.session import db  # noqa: E402

MARCA_AUTO = "repasada auto"


def _clp(n: int) -> str:
    return f"${n:,}".replace(",", ".")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--desde", default="2000-01-01", help="fecha del pago, inclusive")
    ap.add_argument("--hasta", default="2999-12-31", help="fecha del pago, inclusive")
    args = ap.parse_args()

    try:
        from medilink import PROFESIONALES
    except Exception:
        PROFESIONALES = {}
    nom = lambda p: (PROFESIONALES.get(p, {}) or {}).get("nombre", "") or f"Prof {p}"

    with db() as c:
        filas = c.execute(
            """SELECT o.pago_id, o.id_profesional AS ov_prof, o.atencion_id, o.reason,
                      a.id_profesional AS aten_prof, p.fecha, p.monto, p.nombre_paciente
                 FROM bi_pago_overrides o
                 JOIN bi_atenciones a ON a.atencion_id = o.atencion_id
                 JOIN bi_pagos_caja  p ON p.pago_id    = o.pago_id
                WHERE o.id_profesional <> a.id_profesional
                  AND p.fecha >= ? AND p.fecha <= ?
                ORDER BY p.fecha""",
            (args.desde, args.hasta),
        ).fetchall()

        autos = [r for r in filas if MARCA_AUTO in (r["reason"] or "")]
        manuales = [r for r in filas if MARCA_AUTO not in (r["reason"] or "")]

        print(f"Overrides contradictorios entre {args.desde} y {args.hasta}: {len(filas)}")
        print(f"  automáticos: {len(autos)} · {_clp(sum(r['monto'] or 0 for r in autos))}")
        print(f"  manuales:    {len(manuales)} (decisión del dueño — no son un hallazgo)")

        delta: dict[int, int] = defaultdict(int)
        for r in autos:
            delta[r["ov_prof"]] -= r["monto"] or 0
            delta[r["aten_prof"]] += r["monto"] or 0

        print("\nPlata en disputa por profesional (NO es una corrección a aplicar:")
        print("el signo sólo dice quién la tiene hoy vs. quién la tendría según su atención):")
        for pid, d in sorted(delta.items(), key=lambda x: -abs(x[1])):
            if d:
                print(f"  {nom(pid):<28} {'+' if d > 0 else '-'}{_clp(abs(d))}")

        print("\nPago a pago (revisar contra el informe de Medilink del profesional):")
        for r in autos:
            print(f"  {r['fecha']}  pago {r['pago_id']:<6} {_clp(r['monto'] or 0):>10}  "
                  f"hoy={nom(r['ov_prof'])}  ·  su atención {r['atencion_id']}={nom(r['aten_prof'])}"
                  f"  ·  {r['nombre_paciente'] or ''}")
        print("\nSolo lectura: este script no escribe. Para mover un pago, "
              "módulo Pagos Medilink → Reasignar (deja override manual auditable).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
