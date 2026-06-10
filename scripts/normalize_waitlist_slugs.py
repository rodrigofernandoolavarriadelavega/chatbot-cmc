#!/usr/bin/env python3
"""M3 — Migración one-shot: normaliza slugs de especialidad en tabla waitlist.

Uso:
  python scripts/normalize_waitlist_slugs.py          # dry-run (solo muestra cambios)
  python scripts/normalize_waitlist_slugs.py --apply  # aplica cambios en DB local

NO ejecutar contra producción directamente. En VPS:
  1. Copiar el script al servidor
  2. Activar el venv del proyecto
  3. python scripts/normalize_waitlist_slugs.py        # revisar antes
  4. python scripts/normalize_waitlist_slugs.py --apply
"""
import sys
import os

# Permitir importar desde app/ aunque el script esté en scripts/
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "app"))

try:
    from session import _conn, _normalize_waitlist_esp
except ImportError as e:
    print(f"Error importando session: {e}")
    print("Ejecuta desde la raiz del repo con el venv activo.")
    sys.exit(1)

DRY_RUN = "--apply" not in sys.argv


def main():
    with _conn() as conn:
        rows = conn.execute("""
            SELECT id, especialidad FROM waitlist
            WHERE canceled_at IS NULL
            ORDER BY id ASC
        """).fetchall()

    if not rows:
        print("No hay filas activas en waitlist.")
        return

    cambios = []
    for row in rows:
        wid = row["id"]
        esp_orig = row["especialidad"] or ""
        esp_norm = _normalize_waitlist_esp(esp_orig)
        if esp_norm != esp_orig:
            cambios.append((wid, esp_orig, esp_norm))

    if not cambios:
        print(f"Revisadas {len(rows)} filas. Ninguna necesita cambio.")
        return

    print(f"Revisadas {len(rows)} filas. {len(cambios)} necesitan normalizacion:\n")
    for wid, orig, norm in cambios:
        print(f"  id={wid}: {orig!r:40s} -> {norm!r}")

    if DRY_RUN:
        print(f"\n[DRY RUN] Pasa --apply para ejecutar los {len(cambios)} cambios.")
        return

    # Aplicar
    with _conn() as conn:
        for wid, orig, norm in cambios:
            conn.execute(
                "UPDATE waitlist SET especialidad = ? WHERE id = ?",
                (norm, wid),
            )
        conn.commit()
    print(f"\n[APLICADO] {len(cambios)} filas actualizadas.")


if __name__ == "__main__":
    main()
