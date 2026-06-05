#!/usr/bin/env python3
"""Atribución unificada — UN origen canónico de ADQUISICIÓN por paciente.

Corrige el doble-conteo de la vista por-señal (donde 'utility' inflaba al 51%
porque un recordatorio es RETENCIÓN, no origen). Aquí cada paciente recibe un
único origen = cómo LLEGÓ, con prioridad de adquisición:

    1. Meta      — tiene click-to-WhatsApp en meta_referrals (anuncio)
    2. Winback   — fue reactivado por campaña winback (evento winback_*)
    3. Espontáneo— escribió por su cuenta, sin anuncio ni reactivación

'utility/recordatorio' NO es un origen: aplica a pacientes ya adquiridos. Quien
solo tiene touches de utility cae a Espontáneo (orgánico/recurrente).

Además separa NUEVO vs RECURRENTE: 'nuevo' = su primera cita-por-bot cae dentro
del período (proxy del tracking, ~desde 2026-05). La atribución de marketing que
importa es la de los NUEVOS.

Gotchas de datos (ver memoria):
- conversation_events.ts = string ISO 'YYYY-MM-DD HH:MM:SS' → filtrar con string.
- meta_referrals.ts      = epoch int → membresía por phone (el click ES la adquisición).

Uso (venv, con .env cargado):
    /opt/chatbot-cmc/venv/bin/python scripts/atribucion_unificada.py            # 30d
    /opt/chatbot-cmc/venv/bin/python scripts/atribucion_unificada.py --dias 90
    /opt/chatbot-cmc/venv/bin/python scripts/atribucion_unificada.py --json     # snapshot
"""
import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
from session import _conn  # noqa: E402

ORIGENES = ["meta", "winback", "espontaneo"]


def clasificar(dias: int) -> dict:
    desde = (datetime.now() - timedelta(days=dias)).strftime("%Y-%m-%d")

    with _conn() as c:
        # Primera cita-por-bot de cada phone (all-time) → define nuevo/recurrente.
        primera_cita: dict[str, str] = {}
        for phone, ts in c.execute(
            "SELECT phone, MIN(ts) FROM conversation_events "
            "WHERE event='cita_creada' AND phone IS NOT NULL AND phone!='' GROUP BY phone"
        ).fetchall():
            primera_cita[phone] = ts

        # Phones con AL MENOS una cita dentro del período.
        phones_periodo = set(
            r[0] for r in c.execute(
                "SELECT DISTINCT phone FROM conversation_events "
                "WHERE event='cita_creada' AND ts >= ? AND phone IS NOT NULL AND phone!=''",
                (desde,),
            ).fetchall()
        )

        # Señales de adquisición (membresía por phone).
        meta = set(r[0] for r in c.execute(
            "SELECT DISTINCT phone FROM meta_referrals WHERE phone IS NOT NULL AND phone!=''"
        ).fetchall())
        winback = set(r[0] for r in c.execute(
            "SELECT DISTINCT phone FROM conversation_events WHERE event LIKE 'winback%'"
        ).fetchall())

    # Clasificación: prioridad meta > winback > espontaneo.
    detalle = []
    for phone in phones_periodo:
        if phone in meta:
            origen = "meta"
        elif phone in winback:
            origen = "winback"
        else:
            origen = "espontaneo"
        tipo = "nuevo" if primera_cita.get(phone, "9999") >= desde else "recurrente"
        detalle.append((phone, origen, tipo))

    # Conteos limpios.
    by_origen_tipo = Counter((o, t) for _, o, t in detalle)
    nuevos = Counter(o for _, o, t in detalle if t == "nuevo")
    recurr = Counter(o for _, o, t in detalle if t == "recurrente")
    return {
        "periodo_dias": dias,
        "desde": desde,
        "total_phones": len(phones_periodo),
        "total_nuevos": sum(nuevos.values()),
        "total_recurrentes": sum(recurr.values()),
        "adquisicion_nuevos": {o: nuevos.get(o, 0) for o in ORIGENES},
        "recurrentes_por_origen": {o: recurr.get(o, 0) for o in ORIGENES},
        "todos_por_origen": {o: nuevos.get(o, 0) + recurr.get(o, 0) for o in ORIGENES},
    }


def render(d: dict) -> None:
    tot = d["total_phones"] or 1
    print(f"=== Atribución unificada — últimos {d['periodo_dias']}d (desde {d['desde']}) ===")
    print(f"Pacientes con cita en período: {d['total_phones']}  "
          f"(nuevos {d['total_nuevos']} · recurrentes {d['total_recurrentes']})\n")

    print("ADQUISICIÓN (solo NUEVOS — lo que mide marketing):")
    tn = d["total_nuevos"] or 1
    for o in ORIGENES:
        n = d["adquisicion_nuevos"][o]
        print(f"  {o:<11} {n:>4}  ({100*n/tn:4.1f}% de nuevos)")
    print()
    print("RECURRENTES (retención — aquí caen los recordatorios utility):")
    for o in ORIGENES:
        print(f"  {o:<11} {d['recurrentes_por_origen'][o]:>4}")
    print()
    print("TODOS los del período por origen:")
    for o in ORIGENES:
        n = d["todos_por_origen"][o]
        print(f"  {o:<11} {n:>4}  ({100*n/tot:4.1f}%)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dias", type=int, default=30)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    d = clasificar(args.dias)
    if args.json:
        print(json.dumps(d, ensure_ascii=False, indent=2))
    else:
        render(d)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
