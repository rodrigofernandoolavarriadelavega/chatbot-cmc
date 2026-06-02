"""
Reporte standalone de la "lista de hoy" de reenganche clínico.

Cruza todos los programas de especialidad (Kine, Ortodoncia, Nutrición,
Psicología, Cardiología, etc.) y arma la cola priorizada de pacientes a
contactar — la misma que `programas.build_digest()` alimenta en la UI y en el
Copilot Alma. Sirve para correrlo a mano o por cron (no toca el bot en vivo).

Uso:
  python programas_report.py            # imprime la lista priorizada
  python programas_report.py --json     # JSON (para cron → snapshot)
  python programas_report.py --top 30   # limita filas
  python programas_report.py --programa kine

Cron sugerido (cuando se despliegue): generar el snapshot cada mañana 07:30 CLT
y dejarlo en data/programas_digest.json para que recepción lo tenga al abrir.
"""
import argparse
import json
import sys


def _build(meses: int = 6) -> dict:
    # Import diferido: requiere el entorno del bot (psycopg2, BI, sessions.db).
    import programas
    return programas.build_digest(meses)


def main() -> int:
    ap = argparse.ArgumentParser(description="Lista de hoy — reenganche clínico CMC")
    ap.add_argument("--json", action="store_true", help="salida JSON")
    ap.add_argument("--top", type=int, default=50, help="máximo de filas (default 50)")
    ap.add_argument("--programa", default=None, help="filtrar a un programa (kine, ortodoncia, nutricion, …)")
    ap.add_argument("--meses", type=int, default=6, help="ventana de análisis (default 6)")
    args = ap.parse_args()

    d = _build(args.meses)
    items = d["items"]
    if args.programa:
        f = args.programa.strip().lower()
        items = [it for it in items if it["programa"] == f or f in it["programa_label"].lower()]
    items = items[: args.top]

    if args.json:
        print(json.dumps({**d, "items": items}, ensure_ascii=False, indent=2, default=str))
        return 0

    if d["source_status"] != "ok":
        print(f"[aviso] fuente de datos: {d['source_status']} (¿BI arriba? ¿.env cargado?)\n")
    print("═" * 64)
    print(f"  LISTA DE HOY — REENGANCHE CLÍNICO CMC   ({d['total']} pacientes)")
    print("═" * 64)
    if d["por_programa"]:
        resumen = " · ".join(f"{k}: {v}" for k, v in sorted(d["por_programa"].items(), key=lambda x: -x[1]))
        print(f"  {resumen}\n")
    if not items:
        print("  Sin pendientes. Todo al día.")
        return 0
    print(f"  {'#':>2}  {'PACIENTE':<26} {'PROGRAMA':<16} {'ESTADO':<16} {'DÍAS':>5}  TELÉFONO")
    print("  " + "-" * 78)
    for i, it in enumerate(items, 1):
        print(f"  {i:>2}  {it['paciente'][:25]:<26} {it['programa_label'][:15]:<16} "
              f"{it['estado'][:15]:<16} {it['dias_sin_visita']:>5}  {it.get('telefono','') or '—'}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
