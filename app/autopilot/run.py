"""Runner manual del autopilot (dry-run). Valida el bucle sin tocar el scheduler.

Uso (desde app/):
    python -m autopilot.run            # ventana 7 días
    python -m autopilot.run 14         # ventana 14 días

Imprime el reporte en stdout y lo agrega a data/decisions.log.
NO ejecuta ningún cambio en Meta (AUTOPILOT_EXECUTE sigue en false).
"""
import asyncio
import sys

from .engine import run_dry_run


async def _main():
    window = 7
    if len(sys.argv) > 1:
        try:
            window = int(sys.argv[1])
        except ValueError:
            pass
    run = await run_dry_run(window_days=window)
    print(run.report_md)


if __name__ == "__main__":
    asyncio.run(_main())
