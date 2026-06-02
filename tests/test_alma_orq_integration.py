"""Test de integración end-to-end de los orquestadores de Alma.

Monta una FastAPI mínima con SOLO app/admin_routes.router (no importa main.py,
para no arrastrar WIP de sesiones paralelas) y golpea los endpoints + la página
con un TestClient real, validando status 200/403 y JSON/HTML sano.

Si fastapi/testclient no están disponibles, degrada a SKIP (no falla).

Correr: python3 tests/test_alma_orq_integration.py
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

# Paths temporales ANTES de importar módulos que los leen en import.
_props = tempfile.NamedTemporaryFile(suffix=".json", delete=False); _props.close()
_snap = tempfile.NamedTemporaryFile(suffix=".json", delete=False); _snap.close()
os.environ["ALMA_BRAIN_PROPOSALS_PATH"] = _props.name
os.environ["ALMA_ORQ_SNAPSHOT_PATH"] = _snap.name

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
except Exception as e:  # noqa: BLE001
    print(f"SKIP: fastapi/TestClient no disponible ({e}) — test de integración omitido")
    sys.exit(0)

import session  # noqa: E402
_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False); _db.close()
session.DB_PATH = Path(_db.name)

import admin_routes  # noqa: E402
from config import ADMIN_TOKEN  # noqa: E402
# Belt-and-suspenders: forzar paths temporales en los módulos ya cargados.
from alma_brain.orchestrators import snapshot as _snapmod  # noqa: E402
_snapmod.SNAPSHOT_PATH = _snap.name
from alma_brain import tools as _toolsmod  # noqa: E402
_toolsmod._PROPOSALS_PATH = _props.name


def _ok(cond, msg):
    print(("OK   " if cond else "FALLO ") + msg)
    assert cond, msg


def main():
    app = FastAPI()
    app.include_router(admin_routes.router)
    client = TestClient(app)
    T = {"token": ADMIN_TOKEN}

    # Catálogo
    r = client.get("/admin/api/orquestadores", params=T)
    _ok(r.status_code == 200, f"catálogo 200 ({r.status_code})")
    _ok(len(r.json().get("orquestadores", [])) == 23, "catálogo trae los 23 orquestadores")

    # Métricas
    r = client.get("/admin/api/orquestadores/metrics", params=T)
    _ok(r.status_code == 200, f"metrics 200 ({r.status_code})")
    m = r.json()
    _ok(m["total"] == 23 and m["encendidos"] == 0, "metrics: 23 total, 0 encendidos")
    _ok(sum(m["por_dominio"].values()) == 23, "metrics: por_dominio suma 23")

    # Snapshot (lo construye la 1ª vez)
    r = client.get("/admin/api/orquestadores/snapshot", params=T)
    _ok(r.status_code == 200, f"snapshot 200 ({r.status_code})")
    _ok(r.json().get("n_orquestadores") == 23, "snapshot cubre los 23")

    # Briefing
    r = client.get("/admin/api/orquestadores/briefing", params=T)
    _ok(r.status_code == 200, f"briefing 200 ({r.status_code})")
    _ok("items" in r.json() and "por_dominio" in r.json(), "briefing trae items + por_dominio")

    # Dry-run de uno
    r = client.get("/admin/api/orquestadores/dryrun", params={**T, "name": "resultados_examenes"})
    _ok(r.status_code == 200, f"dryrun 200 ({r.status_code})")
    _ok("proposals" in r.json(), "dryrun trae proposals")

    # Inbox de propuestas
    r = client.get("/admin/api/orquestadores/propuestas", params=T)
    _ok(r.status_code == 200, f"propuestas 200 ({r.status_code})")
    _ok("propuestas" in r.json(), "propuestas trae la lista")

    # Página HTML con token reemplazado
    r = client.get("/alma/orquestadores", params=T)
    _ok(r.status_code == 200, f"página 200 ({r.status_code})")
    _ok("text/html" in r.headers.get("content-type", ""), "página devuelve HTML")
    _ok("__TOKEN__" not in r.text, "placeholder __TOKEN__ ya reemplazado")
    _ok(ADMIN_TOKEN in r.text, "el token quedó inyectado en la página")

    # Auth: sin token / token malo → no autorizado (401 o 403 según la capa).
    r = client.get("/admin/api/orquestadores")
    _ok(r.status_code in (401, 403), f"sin token → no autorizado ({r.status_code})")
    r = client.get("/admin/api/orquestadores", params={"token": "no-soy-admin"})
    _ok(r.status_code in (401, 403), f"token malo → no autorizado ({r.status_code})")
    r = client.get("/alma/orquestadores")
    _ok(r.status_code in (401, 403), f"página sin token → no autorizado ({r.status_code})")

    print("\nintegración OK — endpoints + página wirean end-to-end")
    for p in (_db.name, _props.name, _snap.name):
        try: os.unlink(p)
        except OSError: pass


if __name__ == "__main__":
    main()
