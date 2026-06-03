"""Rutas del cerebro de Alma. APIRouter para incluir en main.py con 1 línea.

Fase 1 (estado):
  GET  /alma/brain/api/state     → snapshot global (lee cache; lo construye si falta)
  POST /alma/brain/api/refresh   → reconstruye el estado ahora (read-only sobre fuentes)

Fase 2 (copiloto) y Fase 3 (acciones) se añaden más abajo.

Auth: mismo token admin que el resto del panel (?token= o cookie). El Copilot y
las acciones se restringen además a perfiles con `brain=True`.
"""
import asyncio
import json
import logging
from pathlib import Path

from fastapi import APIRouter, Query, Request, HTTPException, Body
from fastapi.responses import HTMLResponse, JSONResponse

from config import ADMIN_TOKEN, OLACORE_TOKEN, ALMA_PROFILES
from . import state as brain_state

log = logging.getLogger("bot")
router = APIRouter()

_ADMIN_TOKENS = (ADMIN_TOKEN, OLACORE_TOKEN)
_TEMPLATE = Path(__file__).parent.parent.parent / "templates" / "alma_copilot.html"


def _eff_token(token: str | None, request: Request | None) -> str | None:
    if token and token in _ADMIN_TOKENS:
        return token
    cookie = request.cookies.get("admin_token") if request else None
    if cookie and cookie in _ADMIN_TOKENS:
        return cookie
    # tokens de perfil (cmc_admin_olacore, etc.) también valen si están en ALMA_PROFILES
    if token and token in ALMA_PROFILES:
        return token
    if cookie and cookie in ALMA_PROFILES:
        return cookie
    return None


def _check_token(token: str | None, request: Request | None) -> str:
    eff = _eff_token(token, request)
    if eff is None:
        raise HTTPException(status_code=403, detail="Token inválido")
    return eff


def _check_brain_access(token: str | None, request: Request | None) -> str:
    """El Copilot y las acciones requieren un perfil con acceso al cerebro.
    Por defecto SOLO el dueño (perfil con modulos=None / brain=True)."""
    eff = _check_token(token, request)
    prof = ALMA_PROFILES.get(eff, {}) or {}
    # Acceso total (modulos=None) o flag explícito brain=True.
    if prof.get("modulos") is None or prof.get("brain") is True:
        return eff
    raise HTTPException(status_code=403, detail="Perfil sin acceso al Copilot de Alma")


# ── Fase 1: estado ───────────────────────────────────────────────────────────

@router.get("/alma/brain/api/state")
def brain_state_api(token: str | None = Query(None), request: Request = None,
                    rebuild: bool = Query(False)):
    _check_token(token, request)
    st = None if rebuild else brain_state.load_state()
    if st is None:
        st = brain_state.build_and_save(window_days=7)
    return JSONResponse(st)


@router.post("/alma/brain/api/refresh")
async def brain_refresh_api(token: str | None = Query(None), request: Request = None):
    _check_token(token, request)
    # build_state puede tocar BI/SQLite (I/O bloqueante) → a thread para no
    # bloquear el event loop del bot.
    st = await asyncio.to_thread(brain_state.build_and_save, 7)
    return JSONResponse({"ok": True, "domains_available": st.get("domains_available", []),
                         "alerts": len(st.get("alerts", []))})


# ── Fase 2: copiloto conversacional (read-only) ──────────────────────────────

@router.get("/alma/brain", response_class=HTMLResponse)
def brain_dashboard(token: str | None = Query(None), request: Request = None):
    _check_token(token, request)
    if not _TEMPLATE.exists():
        raise HTTPException(404, "Copilot UI no disponible")
    return HTMLResponse(_TEMPLATE.read_text(encoding="utf-8"))


@router.post("/alma/brain/api/chat")
async def brain_chat_api(token: str | None = Query(None), request: Request = None,
                         payload: dict = Body(...)):
    eff = _check_brain_access(token, request)
    messages = payload.get("messages") or []
    if not isinstance(messages, list) or not messages:
        raise HTTPException(400, "Falta 'messages'")
    allow_actions = bool(payload.get("allow_actions", False))
    from . import copilot
    try:
        result = await copilot.run_turn(messages, allow_actions=allow_actions)
    except Exception as e:  # noqa: BLE001
        log.error("alma_brain copilot: turno falló (%s)", e)
        raise HTTPException(500, f"Copilot error: {e}")
    return JSONResponse(result)


# ── Fase 3: cola de acciones propuestas (dry-run + aprobación) ───────────────

@router.get("/alma/brain/api/proposals")
def brain_proposals_api(token: str | None = Query(None), request: Request = None):
    _check_brain_access(token, request)
    from . import tools
    return JSONResponse({"proposals": tools.load_proposals()})


@router.post("/alma/brain/api/proposals/{pid}/approve")
async def brain_proposal_approve(pid: str, token: str | None = Query(None), request: Request = None):
    eff = _check_brain_access(token, request)
    from . import tools
    try:
        result = await tools.approve_and_execute(pid, approved_by=eff)
    except Exception as e:  # noqa: BLE001
        log.error("alma_brain: ejecución de propuesta %s falló (%s)", pid, e)
        raise HTTPException(500, str(e))
    return JSONResponse(result)


@router.post("/alma/brain/api/proposals/{pid}/reject")
def brain_proposal_reject(pid: str, token: str | None = Query(None), request: Request = None):
    _check_brain_access(token, request)
    from . import tools
    return JSONResponse(tools.reject_proposal(pid))
