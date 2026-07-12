#!/usr/bin/env python3
"""
Chequeo de calidad del módulo de pagos (`pagos_cmc`).

Por qué existe
──────────────
`pagos_cmc` es la ÚNICA fuente de caja real que tiene el CMC. Medilink
`/api/v5/pagos` solo expone la caja abierta del día (~50 filas, sin historial),
así que `bi.fact_pagos` está vacío. Todo lo que sabemos de la caja sale de lo que
teclea recepción.

Y sobre ese dato se apoya una decisión de $1,64M/año: el mix débito/crédito que
sostiene el análisis Transbank vs Mercado Pago. Si el mix está mal, la decisión
está mal.

Qué chequea (framework de Kahn et al. 2016)
───────────────────────────────────────────
  · CONFORMANCE  — ¿los valores están en la lista permitida?
  · COMPLETENESS — ¿faltan campos que deberían estar? (tarjeta sin comprobante)
  · PLAUSIBILITY — ¿el mix diario se parece al histórico, o algo se salió de madre?

Lo que NO puede chequear: si un débito se anotó como crédito estando el
comprobante correcto. Para eso hace falta la liquidación de Transbank, que hoy
no se descarga (los directorios TRANSBANK_*/ tienen solo un CSV de ejemplo).

Uso
───
    python3 scripts/qa_pagos.py                      # data/sessions.db
    python3 scripts/qa_pagos.py --db /opt/chatbot-cmc/data/sessions.db
    python3 scripts/qa_pagos.py --dias 30

Solo lectura. Abre la base en modo `ro`.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import defaultdict
from datetime import date, timedelta

METODOS_VALIDOS = ("efectivo", "transferencia", "debito", "credito", "bono_web")
TARJETAS = ("debito", "credito")

# Umbral de plausibilidad: cuánto puede desviarse el share de crédito de un día
# respecto del histórico antes de que valga la pena mirarlo. 2 desviaciones ≈ 95%.
SIGMAS = 2.0


def abrir(ruta: str):
    """En producción `sessions.db` está cifrada con SQLCipher (Ley 19.628), así que
    `sqlite3` responde «file is not a database». Se usa el mismo camino que la app:
    módulo sqlcipher + `PRAGMA key`. En local, sin `SQLCIPHER_KEY`, cae a sqlite3."""
    import os

    clave = (os.getenv("SQLCIPHER_KEY") or "").strip()
    if clave:
        try:
            from sqlcipher3 import dbapi2 as driver  # type: ignore
        except ImportError:
            from pysqlcipher3 import dbapi2 as driver  # type: ignore
        con = driver.connect(ruta)
        con.execute(f"PRAGMA key = '{clave}'")
        con.execute("PRAGMA query_only = ON")   # solo lectura, por si acaso
    else:
        con = sqlite3.connect(f"file:{ruta}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default="data/sessions.db")
    p.add_argument("--dias", type=int, default=90)
    args = p.parse_args()

    try:
        con = abrir(args.db)
    except sqlite3.OperationalError as e:
        print(f"No se pudo abrir {args.db}: {e}", file=sys.stderr)
        return 1

    desde = (date.today() - timedelta(days=args.dias)).isoformat()
    filas = con.execute(
        "SELECT fecha, metodo_pago, copago, codigo_transferencia, creado_por "
        "FROM pagos_cmc WHERE fecha >= ? AND bloqueado = 1",
        (desde,),
    ).fetchall()

    print("═" * 70)
    print(f"QA PAGOS · {desde} → hoy · {len(filas)} pagos")
    print("═" * 70)
    if not filas:
        print("  Sin pagos en la ventana. (¿Base local? Los pagos reales están en prod.)")
        return 0

    # ── CONFORMANCE ───────────────────────────────────────────────
    invalidos = [f for f in filas if (f["metodo_pago"] or "") not in METODOS_VALIDOS]
    print("\n▸ CONFORMANCE — valores fuera de la lista permitida")
    print(f"    {len(invalidos)} de {len(filas)}" + ("   ✓" if not invalidos else "   ← revisar"))

    # ── COMPLETENESS ──────────────────────────────────────────────
    # Una tarjeta sin N° de comprobante es un dato que nunca se va a poder verificar:
    # el tipo (débito/crédito) viene impreso en ese voucher y en ningún otro lado.
    tarjetas = [f for f in filas if (f["metodo_pago"] or "") in TARJETAS]
    sin_comp = [f for f in tarjetas if not (f["codigo_transferencia"] or "").strip()]
    print("\n▸ COMPLETENESS — tarjetas sin N° de comprobante")
    pct = 100 * len(sin_comp) / len(tarjetas) if tarjetas else 0
    print(f"    {len(sin_comp)} de {len(tarjetas)} tarjetas  ({pct:.0f}%)")
    if sin_comp:
        print("    → sin el voucher no hay forma de verificar si el débito/crédito quedó bien.")

    # ── PLAUSIBILITY ──────────────────────────────────────────────
    print("\n▸ PLAUSIBILITY — mix de métodos")
    por_metodo: dict[str, list[int, int]] = defaultdict(lambda: [0, 0])
    for f in filas:
        m = (f["metodo_pago"] or "?").lower()
        por_metodo[m][0] += 1
        por_metodo[m][1] += int(f["copago"] or 0)
    total_monto = sum(v[1] for v in por_metodo.values()) or 1
    for m, (n, monto) in sorted(por_metodo.items(), key=lambda x: -x[1][1]):
        print(f"    {m:<16} {n:>5} pagos   ${monto:>12,.0f}   {100*monto/total_monto:>5.1f}%")

    n_deb = por_metodo.get("debito", [0, 0])[0]
    n_cre = por_metodo.get("credito", [0, 0])[0]
    if n_deb + n_cre:
        share_cre = n_cre / (n_deb + n_cre)
        print(f"\n    Mix tarjeta: {100*(1-share_cre):.0f}% débito / {100*share_cre:.0f}% crédito")
        print("    (este número es el que sostiene la decisión Transbank vs Mercado Pago)")

    # Días cuyo share de crédito se sale del histórico → algo pasó ese día.
    por_dia: dict[str, list[int, int]] = defaultdict(lambda: [0, 0])
    for f in filas:
        m = (f["metodo_pago"] or "").lower()
        if m in TARJETAS:
            por_dia[f["fecha"]][0 if m == "debito" else 1] += 1

    shares = [c / (d + c) for d, c in por_dia.values() if (d + c) >= 3]
    if len(shares) >= 5:
        media = sum(shares) / len(shares)
        var = sum((s - media) ** 2 for s in shares) / len(shares)
        sd = var ** 0.5
        raros = [
            (dia, d, c, c / (d + c))
            for dia, (d, c) in sorted(por_dia.items())
            if (d + c) >= 3 and sd > 0 and abs((c / (d + c)) - media) > SIGMAS * sd
        ]
        print(f"\n▸ Días fuera de rango (>{SIGMAS:g}σ del mix histórico de {100*media:.0f}% crédito)")
        if not raros:
            print("    ninguno   ✓")
        for dia, d, c, s in raros:
            print(f"    {dia}   débito {d:>2} · crédito {c:>2}   → {100*s:.0f}% crédito   ← revisar")

    # ── El default silencioso ─────────────────────────────────────
    # Antes del fix, la UI traía "efectivo" preseleccionado y el backend convertía
    # cualquier valor desconocido en "efectivo", sin avisar. Si el share de efectivo
    # es sospechosamente alto, puede ser gente que nunca tocó el botón.
    n_efe = por_metodo.get("efectivo", [0, 0])[0]
    print(f"\n▸ Share de 'efectivo': {100*n_efe/len(filas):.0f}% de los pagos")
    print("    Antes del fix, 'efectivo' era el default silencioso en TRES lugares")
    print("    (UI preseleccionada, reset del modal, y coerción en el backend).")
    print("    Si este número baja tras el fix, parte de ese 'efectivo' era un olvido.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
