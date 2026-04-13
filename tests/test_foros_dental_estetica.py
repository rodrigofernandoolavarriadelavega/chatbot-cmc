"""Test ad-hoc: frases reales de foros de salud (dental + estética).
Verifica que detect_intent() las clasifique como intent='info' con
especialidad rellenada, para que el pipeline FAQ-to-agendar pre-busque
slot y ofrezca agendamiento DESPUÉS de explicar el tratamiento.

Ejecución: PYTHONPATH=app:. venv/bin/python tests/test_foros_dental_estetica.py
"""
from __future__ import annotations
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT))

from claude_helper import detect_intent  # noqa: E402

DENTAL = [
    "Duele sacarse la muela del juicio?",
    "Se me rompió una muela, qué hago",
    "Tengo una caries profunda, necesito conducto?",
    "Me sangran las encías",
    "Cuánto dura una tapadura",
    "Quiero blanquearme los dientes",
    "Tengo los dientes chuecos, quiero arreglármelos",
    "Se me cayó una tapadura",
    "Tengo sensibilidad al frío en los dientes",
    "Quiero ortodoncia invisible",
    "Tengo gingivitis",
    "Flemón en la encía",
    "Cuánto cuesta sacar una muela picada",
    "Me duele la muela en la noche",
    "Me salió un absceso en la encía",
    "Quiero una limpieza dental",
    "Quiero ponerme un implante",
    "Cuánto cuestan los brackets",
]

ESTETICA = [
    "Cuánto cuesta el botox",
    "Duele la mesoterapia?",
    "Me hago rellenos en los labios",
    "Quiero bajar la papada",
    "Arrugas en el entrecejo",
    "Quiero hilos tensores",
    "Qué es un bioestimulador",
    "Quiero rellenar los pómulos",
    "Tengo surco nasogeniano muy marcado",
    "Quiero ácido hialurónico",
    "Quiero exosomas para la cara",
    "Rellenar ojeras",
    "Hacerme un peeling",
    "Quiero armonización facial",
    "Qué es la hidroxiapatita",
    "Lipopapada cuánto cuesta",
]


async def run(label, cases):
    print(f"\n══════ {label} ══════")
    fails = []
    for tx in cases:
        try:
            r = await detect_intent(tx)
            intent = r.get("intent", "?")
            esp = r.get("especialidad")
            rd = r.get("respuesta_directa") or ""
            ok = (intent == "info" and esp and len(rd) > 20)
            mark = "✅" if ok else "❌"
            print(f"{mark} {tx!r}")
            print(f"   intent={intent}  esp={esp!r}")
            if rd:
                print(f"   rd: {rd[:200]}")
            if not ok:
                fails.append((tx, intent, esp, rd[:150]))
        except Exception as e:
            print(f"💥 {tx!r}  → {e}")
            fails.append((tx, "ERR", None, str(e)[:100]))
    print(f"\n── {label}: {len(cases) - len(fails)}/{len(cases)} OK ──")
    return fails


async def main():
    f1 = await run("DENTAL (foros)", DENTAL)
    f2 = await run("ESTÉTICA (foros)", ESTETICA)
    total = len(DENTAL) + len(ESTETICA)
    ok_total = total - len(f1) - len(f2)
    print(f"\n══════ TOTAL: {ok_total}/{total} OK ══════")
    if f1 or f2:
        print("\n── GAPS / FALLOS ──")
        for grp, fs in [("Dental", f1), ("Estética", f2)]:
            if fs:
                print(f"\n{grp}:")
                for tx, i, e, rd in fs:
                    print(f"  • {tx!r}  → intent={i} esp={e}")
                    if rd:
                        print(f"     {rd}")


if __name__ == "__main__":
    asyncio.run(main())
