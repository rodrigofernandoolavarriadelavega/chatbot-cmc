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

        # Puente phone → id_paciente Medilink (para cruzar a la caja real).
        bridge = dict(c.execute(
            "SELECT phone, id_paciente_medilink FROM citas_bot "
            "WHERE id_paciente_medilink IS NOT NULL AND id_paciente_medilink != 0"
        ).fetchall())

        # Clasificación: prioridad meta > winback > espontaneo.
        PRI = {"meta": 3, "winback": 2, "espontaneo": 1}
        detalle = []
        idpac_origin: dict[int, str] = {}   # un origen por paciente (gana prioridad alta)
        for phone in phones_periodo:
            if phone in meta:
                origen = "meta"
            elif phone in winback:
                origen = "winback"
            else:
                origen = "espontaneo"
            tipo = "nuevo" if primera_cita.get(phone, "9999") >= desde else "recurrente"
            detalle.append((phone, origen, tipo))
            idp = bridge.get(phone)
            if idp and (idp not in idpac_origin or PRI[origen] > PRI[idpac_origin[idp]]):
                idpac_origin[idp] = origen

        # Cruce a plata: caja real del período, atribuida al origen del paciente.
        hoy = datetime.now().strftime("%Y-%m-%d")
        ingreso = Counter()
        pagadores = Counter()
        if idpac_origin:
            ids = list(idpac_origin)
            qm = ",".join("?" for _ in ids)
            for idp, monto in c.execute(
                f"SELECT id_paciente, COALESCE(SUM(monto),0) FROM bi_pagos_caja "
                f"WHERE id_paciente IN ({qm}) AND fecha BETWEEN ? AND ? GROUP BY id_paciente",
                ids + [desde, hoy],
            ).fetchall():
                o = idpac_origin[idp]
                ingreso[o] += monto or 0
                if monto:
                    pagadores[o] += 1
        total_caja = c.execute(
            "SELECT COALESCE(SUM(monto),0) FROM bi_pagos_caja WHERE fecha BETWEEN ? AND ?",
            (desde, hoy),
        ).fetchone()[0] or 0

    # Conteos limpios.
    nuevos = Counter(o for _, o, t in detalle if t == "nuevo")
    recurr = Counter(o for _, o, t in detalle if t == "recurrente")
    ingreso_atribuible = sum(ingreso.values())
    return {
        "periodo_dias": dias,
        "desde": desde,
        "total_phones": len(phones_periodo),
        "total_nuevos": sum(nuevos.values()),
        "total_recurrentes": sum(recurr.values()),
        "adquisicion_nuevos": {o: nuevos.get(o, 0) for o in ORIGENES},
        "recurrentes_por_origen": {o: recurr.get(o, 0) for o in ORIGENES},
        "todos_por_origen": {o: nuevos.get(o, 0) + recurr.get(o, 0) for o in ORIGENES},
        "ingreso_por_origen": {o: ingreso.get(o, 0) for o in ORIGENES},
        "pagadores_por_origen": {o: pagadores.get(o, 0) for o in ORIGENES},
        "ingreso_atribuible": ingreso_atribuible,
        "caja_total_periodo": total_caja,
        "caja_no_rastreada": total_caja - ingreso_atribuible,
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

    # ── Loop a plata ───────────────────────────────────────────────────────────
    def clp(n): return f"${int(n):,}".replace(",", ".")
    print()
    print("💰 INGRESO REAL por origen (caja Medilink del período):")
    for o in ORIGENES:
        ing = d["ingreso_por_origen"][o]
        pag = d["pagadores_por_origen"][o]
        tk = ing / pag if pag else 0
        linea = f"  {o:<11} {clp(ing):>12}  ({pag} pagaron · ticket {clp(tk)})"
        spend = d.get("spend_por_origen", {}).get(o)
        if spend is not None:
            cac = spend / d["adquisicion_nuevos"][o] if d["adquisicion_nuevos"][o] else 0
            roas = ing / spend if spend else float("inf")
            linea += f"\n               gasto {clp(spend)} · CAC {clp(cac)} · ROAS {roas:.1f}x"
        print(linea)
    print()
    print(f"  Atribuible (canales digitales): {clp(d['ingreso_atribuible'])}")
    print(f"  Caja total del período:         {clp(d['caja_total_periodo'])}")
    share = 100 * d["ingreso_atribuible"] / (d["caja_total_periodo"] or 1)
    print(f"  No rastreada (presencial/sin bot): {clp(d['caja_no_rastreada'])}  "
          f"→ digital explica {share:.0f}% de la caja")
    if "spend_por_origen" not in d:
        print("\n  (CAC/ROAS de Meta: reejecutá con --spend para traer el gasto de la Marketing API)")


def _meta_spend_total(desde: str, hoy: str) -> float | None:
    """Gasto total Meta del período vía cac_report.meta_spend(). None si falla."""
    try:
        from cac_report import meta_spend
        ads = meta_spend(desde, hoy)
        return sum(float(v.get("spend", 0) or 0) for v in ads.values())
    except Exception as e:
        print(f"[!] no pude traer Meta spend: {e}", file=sys.stderr)
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dias", type=int, default=30)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--spend", action="store_true",
                    help="Trae gasto Meta (Marketing API, ~60s) para CAC/ROAS real.")
    args = ap.parse_args()
    d = clasificar(args.dias)
    if args.spend:
        total = _meta_spend_total(d["desde"], datetime.now().strftime("%Y-%m-%d"))
        if total is not None:
            # Gasto solo en Meta; winback/espontáneo ≈ $0 (no hay pauta detrás).
            d["spend_por_origen"] = {"meta": total, "winback": 0.0, "espontaneo": 0.0}
    if args.json:
        print(json.dumps(d, ensure_ascii=False, indent=2))
    else:
        render(d)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
