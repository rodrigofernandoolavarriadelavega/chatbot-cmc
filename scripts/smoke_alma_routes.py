#!/usr/bin/env python3
"""Smoke-test de todas las rutas del shell Alma (mejora #15 plan auditoría 2026-06-09).

Recorre ALMA_MODULE_REGISTRY + rutas núcleo y verifica que respondan 200 con el
token dueño. Habría cazado el 404 de /alma/orquestadores (template no commiteado).

Uso:
  python3 scripts/smoke_alma_routes.py                      # contra prod (agentecmc.cl)
  python3 scripts/smoke_alma_routes.py --base http://127.0.0.1:8001
  ALMA_SMOKE_TOKEN=xxx python3 scripts/smoke_alma_routes.py # token explícito

Token: usa ALMA_SMOKE_TOKEN, o OLACORE_TOKEN/ADMIN_TOKEN del .env del directorio.
Exit code 0 = todo 200 · 1 = al menos una ruta falló (apto para cron con alerta).
Cron sugerido (VPS): 15 8 * * * cd /opt/chatbot-cmc && venv/bin/python scripts/smoke_alma_routes.py >> /var/log/alma-smoke.log 2>&1
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import httpx
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

# Rutas núcleo fuera del registry que también deben vivir.
EXTRA_ROUTES = ["/health", "/alma", "/admin/v2", "/admin/v3", "/autopilot", "/boxes", "/demanda"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://agentecmc.cl")
    ap.add_argument("--timeout", type=float, default=20.0)
    args = ap.parse_args()

    token = (os.getenv("ALMA_SMOKE_TOKEN") or os.getenv("OLACORE_TOKEN")
             or os.getenv("ADMIN_TOKEN") or "")
    if not token:
        print("SMOKE: sin token (ALMA_SMOKE_TOKEN/OLACORE_TOKEN/ADMIN_TOKEN) — abort")
        return 1

    from app.config import ALMA_MODULE_REGISTRY  # noqa: E402

    rutas = []
    for key, mod in ALMA_MODULE_REGISTRY.items():
        src = mod.get("src", "")
        if src.startswith("http"):  # módulos externos (ej. impresión) se saltan
            continue
        rutas.append((key, src))
    for r in EXTRA_ROUTES:
        rutas.append(("_core", r))

    fallas = []
    with httpx.Client(timeout=args.timeout, follow_redirects=False) as cli:
        for key, path in rutas:
            url = f"{args.base}{path}"
            try:
                resp = cli.get(url, params={"token": token} if path != "/health" else None)
                code = resp.status_code
            except Exception as e:  # red caída cuenta como falla
                code = f"EXC:{type(e).__name__}"
            ok = code == 200
            if not ok:
                fallas.append((key, path, code))
            print(f"{'OK ' if ok else 'FAIL'} {code} {path} ({key})")

    from datetime import datetime
    stamp = datetime.now().isoformat(timespec="seconds")
    if fallas:
        print(f"SMOKE {stamp}: {len(fallas)}/{len(rutas)} rutas FALLARON: "
              + ", ".join(f"{p}={c}" for _, p, c in fallas))
        return 1
    print(f"SMOKE {stamp}: {len(rutas)}/{len(rutas)} rutas OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
