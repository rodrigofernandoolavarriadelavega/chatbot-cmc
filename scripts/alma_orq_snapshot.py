"""Refresca el snapshot dry-run de los orquestadores de Alma.

Corre `run_all(mode="dry")` de los 18+ orquestadores y persiste el resultado a
`data/alma_orq_snapshot.json`. 100% read-only: dry-run no encola propuestas ni
contacta a nadie. Pensado para correr por cron (o a mano) SIN tocar main.py.

Uso:
    PYTHONPATH=app:. python3 scripts/alma_orq_snapshot.py

Cron sugerido (NO registrado aún — requiere aprobación):
    # 06:10 CLT, antes de que recepción abra → el panel muestra el briefing del día
    10 6 * * *  cd /opt/chatbot-cmc && PYTHONPATH=app:. python3 scripts/alma_orq_snapshot.py >> /var/log/alma-orq.log 2>&1
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))


async def _main():
    from alma_brain.orchestrators import snapshot, briefing
    snap = await snapshot.build_and_save()
    brief = await briefing.build_briefing()  # lee el snapshot recién guardado
    print(f"snapshot OK · {snap['n_orquestadores']} orquestadores · "
          f"{snap['total_propuestas_potenciales']} propuestas potenciales · "
          f"{len(brief['prioritarias'])} prioritarias · {brief['n_personas']} personas")
    for it in brief["prioritarias"][:8]:
        print(f"  ! {it['orquestador']}: {it['summary']}")


if __name__ == "__main__":
    asyncio.run(_main())
