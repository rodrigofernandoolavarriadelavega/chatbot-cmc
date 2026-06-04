#!/usr/bin/env python3
"""Normaliza pagos_cmc.profesional al nombre canónico por id_profesional.

Une las variantes de un mismo profesional (Javiera Burgos Godoy / Dra. Javiera
Burgos → "Dra. Javiera Burgos") para que los reportes por profesional no salgan
fragmentados.

Uso (desde /opt/chatbot-cmc en el VPS, con .env cargado):
    python3 scripts/normalize_pagos_profesionales.py            # dry-run (no escribe)
    python3 scripts/normalize_pagos_profesionales.py --apply    # aplica los cambios

Idempotente: correrlo dos veces no hace nada la segunda vez.
"""
import argparse
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from session import _conn          # noqa: E402
from prof_canon import canonical_name, load_canonical_map  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="Escribe los cambios (si no, solo muestra).")
    args = ap.parse_args()

    # Guard: si el mapa canónico no carga (p. ej. httpx ausente fuera del venv),
    # abortamos en vez de hacer un backfill no-op silencioso.
    try:
        canon_map = load_canonical_map()
    except Exception as e:
        print(f"✗ ABORT: no pude cargar el mapa canónico de profesionales: {e!r}", file=sys.stderr)
        print("  Corré con el python del venv: /opt/chatbot-cmc/venv/bin/python", file=sys.stderr)
        return 2
    if not canon_map:
        print("✗ ABORT: el mapa canónico vino vacío — no normalizo a ciegas.", file=sys.stderr)
        return 2
    print(f"Mapa canónico cargado: {len(canon_map)} profesionales.\n")

    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, id_profesional, profesional FROM pagos_cmc"
        ).fetchall()

        cambios: list[tuple[int, str, str]] = []          # (pago_id, antes, despues)
        resumen: dict[str, dict] = defaultdict(lambda: {"de": set(), "n": 0})

        for pago_id, id_prof, nombre_actual in rows:
            canon = canonical_name(id_prof, nombre_actual or "")
            if canon and canon != (nombre_actual or ""):
                cambios.append((pago_id, nombre_actual, canon))
                resumen[canon]["de"].add(nombre_actual or "(vacío)")
                resumen[canon]["n"] += 1

        if not cambios:
            print("✓ Nada que normalizar — todos los nombres ya son canónicos.")
            return 0

        print(f"Filas a normalizar: {len(cambios)}\n")
        print("Consolidaciones por nombre canónico:")
        for canon in sorted(resumen):
            info = resumen[canon]
            variantes = " · ".join(sorted(info["de"]))
            print(f"  → {canon}  ({info['n']} filas)")
            print(f"      desde: {variantes}")

        if not args.apply:
            print("\n[DRY-RUN] No se escribió nada. Reejecutá con --apply para aplicar.")
            return 0

        for pago_id, _antes, despues in cambios:
            conn.execute(
                "UPDATE pagos_cmc SET profesional = ?, updated_at = datetime('now') WHERE id = ?",
                (despues, pago_id),
            )
        conn.commit()
        print(f"\n✓ Aplicado: {len(cambios)} filas actualizadas.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
