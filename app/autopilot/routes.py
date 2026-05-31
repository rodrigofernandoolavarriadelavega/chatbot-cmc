"""Rutas del dashboard del autopilot. APIRouter para incluir en main.py con 1 línea.

- GET    /autopilot                 → dashboard HTML (estética panel v2, embebible en Ánima)
- GET    /autopilot/api/snapshot    → JSON del último run (lee snapshot, no golpea Meta)
- POST   /autopilot/api/refresh     → fuerza un dry-run nuevo (read-only sobre Meta)
- GET    /autopilot/api/designs     → galería de diseños Canva (visor embebido)
- POST   /autopilot/api/designs     → agrega un diseño a la galería
- DELETE /autopilot/api/designs/{id}→ elimina un diseño

Auth: mismo token admin que el resto del panel (?token= o cookie).
"""
import logging
from pathlib import Path

from fastapi import APIRouter, Query, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

from config import ADMIN_TOKEN
from .world_state import load_snapshot
from .designs import load_designs, add_design, delete_design

log = logging.getLogger("bot")
router = APIRouter()

_TEMPLATE = Path(__file__).parent.parent.parent / "templates" / "autopilot_dashboard.html"


def _check_token(token: str | None, request: Request | None) -> None:
    """Valida el token admin (query o cookie). Coherente con el resto del panel."""
    if token and token == ADMIN_TOKEN:
        return
    cookie = request.cookies.get("admin_token") if request else None
    if cookie and cookie == ADMIN_TOKEN:
        return
    raise HTTPException(status_code=403, detail="Token inválido")


@router.get("/autopilot", response_class=HTMLResponse)
def autopilot_dashboard(token: str | None = Query(None), request: Request = None):
    _check_token(token, request)
    if not _TEMPLATE.exists():
        raise HTTPException(404, "Dashboard no disponible")
    return HTMLResponse(_TEMPLATE.read_text(encoding="utf-8"))


@router.get("/autopilot/api/snapshot")
def autopilot_snapshot(token: str | None = Query(None), request: Request = None):
    _check_token(token, request)
    snap = load_snapshot()
    if snap is None:
        return JSONResponse({"empty": True, "message": "Aún no hay corridas del autopilot."})
    return JSONResponse(snap)


@router.post("/autopilot/api/refresh")
async def autopilot_refresh(window: int = Query(7), token: str | None = Query(None),
                            request: Request = None):
    """Fuerza un dry-run nuevo. Read-only sobre Meta (solo lee insights)."""
    _check_token(token, request)
    try:
        from .engine import run_dry_run
        run = await run_dry_run(window_days=window)
        return JSONResponse({"ok": True, "n_actions": len(run.actions)})
    except Exception as e:  # noqa: BLE001
        log.error("autopilot refresh falló: %s", e)
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# ── Galería de diseños (Canva) ──────────────────────────────────────────────

@router.get("/autopilot/api/designs")
def autopilot_designs(token: str | None = Query(None), request: Request = None):
    """Lista los diseños de la galería (más reciente primero)."""
    _check_token(token, request)
    return JSONResponse({"designs": load_designs()})


@router.post("/autopilot/api/designs")
async def autopilot_designs_add(request: Request, token: str | None = Query(None)):
    """Agrega/actualiza un diseño. Body = registro (ver designs.py)."""
    _check_token(token, request)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(400, "JSON inválido")
    if not isinstance(body, dict) or not body.get("title"):
        raise HTTPException(400, "Falta 'title'")
    return JSONResponse({"ok": True, "design": add_design(body)})


@router.delete("/autopilot/api/designs/{rid}")
def autopilot_designs_del(rid: str, token: str | None = Query(None), request: Request = None):
    """Elimina un diseño por id."""
    _check_token(token, request)
    return JSONResponse({"ok": delete_design(rid)})
