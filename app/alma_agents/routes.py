"""Panel de control de la flota — APIRouter para incluir en main.py con 1 línea.

  GET  /alma/agents                  → UI del panel
  GET  /alma/agents/api/fleet        → estado de la flota (agentes + kill-switches)
  POST /alma/agents/api/{id}/dryrun  → corre un agente en DRY-RUN (preview, sin efecto)

El panel NO enciende agentes (eso se hace por flags en .env, decisión deliberada).
Sí permite PREVISUALIZAR en dry-run qué haría cada agente — seguro, no actúa.

Acceso: solo perfil dueño (igual que el Copilot del cerebro).
"""
import logging
from pathlib import Path

from fastapi import APIRouter, Query, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

from config import ADMIN_TOKEN, OLACORE_TOKEN, ALMA_PROFILES
from . import guardrails, registry, store, ledger, simulator, capstone, learning, events

log = logging.getLogger("bot")
router = APIRouter()

_ADMIN_TOKENS = (ADMIN_TOKEN, OLACORE_TOKEN)
_TEMPLATE = Path(__file__).parent.parent.parent / "templates" / "alma_agents.html"


def _check_owner(token: str | None, request: Request | None) -> str:
    eff = token if (token in _ADMIN_TOKENS or token in ALMA_PROFILES) else None
    if eff is None and request is not None:
        c = request.cookies.get("admin_token")
        if c in _ADMIN_TOKENS or c in ALMA_PROFILES:
            eff = c
    if eff is None:
        raise HTTPException(403, "Token inválido")
    prof = ALMA_PROFILES.get(eff, {}) or {}
    if prof.get("modulos") is None or prof.get("brain") is True:
        return eff
    raise HTTPException(403, "Perfil sin acceso a la flota de agentes")


@router.get("/alma/agents", response_class=HTMLResponse)
def agents_panel(token: str | None = Query(None), request: Request = None):
    _check_owner(token, request)
    if not _TEMPLATE.exists():
        raise HTTPException(404, "Panel de agentes no disponible")
    return HTMLResponse(_TEMPLATE.read_text(encoding="utf-8"))


@router.get("/alma/agents/api/fleet")
def agents_fleet(token: str | None = Query(None), request: Request = None):
    _check_owner(token, request)
    agents = []
    for aid, a in sorted(registry.all_agents().items()):
        last = store.load_last_run(aid)
        agents.append({
            "id": a.id, "name": a.name, "descr": a.descr, "risk": a.risk,
            "category": a.category, "flag": a.flag,
            "enabled": guardrails.agent_enabled(a.flag),
            "schedule": a.schedule,
            "last_run": None if not last else {
                "ran_at": last.get("ran_at"), "dry_run": last.get("dry_run"),
                "notes": last.get("notes"),
                "proposed": len(last.get("actions_proposed", [])),
                "executed": len(last.get("actions_executed", [])),
                "blocked": len(last.get("actions_blocked", [])),
            },
        })
    return JSONResponse({"fleet_status": guardrails.fleet_status(), "agents": agents})


@router.post("/alma/agents/api/{agent_id}/dryrun")
async def agents_dryrun(agent_id: str, token: str | None = Query(None), request: Request = None):
    _check_owner(token, request)
    agent = registry.get(agent_id)
    if agent is None:
        raise HTTPException(404, "Agente no encontrado")
    # force_dry_run=True: percibe y decide de verdad, pero NO ejecuta ninguna
    # acción aunque execute estuviera on. Es un preview seguro.
    # OJO: el run() respeta master+flag del agente; para previsualizar sin
    # encender nada, evaluamos directamente perceive/decide acá.
    try:
        ctx = await agent.perceive()
        actions = await agent.decide(ctx)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"Preview falló: {e}")
    preview = []
    for a in actions:
        allow, reason = guardrails.authorize(a)
        preview.append({
            "kind": a.kind, "summary": a.summary, "risk": a.risk,
            "target": a.target, "is_staff": a.is_staff,
            "requires_contact": a.requires_contact,
            "requires_medilink_write": a.requires_medilink_write,
            "se_ejecutaria": allow, "motivo": reason,
        })
    return JSONResponse({"agent_id": agent_id, "n_acciones": len(preview),
                         "acciones": preview})


@router.get("/alma/agents/api/effectiveness")
def agents_effectiveness(token: str | None = Query(None), request: Request = None):
    """Conversión real por agente (Effectiveness Ledger): contactos → citas/
    respuestas → ingreso recuperable estimado. Read-only."""
    _check_owner(token, request)
    return JSONResponse(ledger.effectiveness_summary())


@router.post("/alma/agents/api/effectiveness/measure")
def agents_effectiveness_measure(token: str | None = Query(None), request: Request = None):
    """Resuelve las filas pendientes cuya ventana ya venció (idempotente)."""
    _check_owner(token, request)
    resolved = ledger.measure_outcomes()
    return JSONResponse({"resolved": resolved, "summary": ledger.effectiveness_summary()})


@router.get("/alma/agents/api/simulate")
async def agents_simulate(token: str | None = Query(None), request: Request = None):
    """Vuelo de prueba del AGREGADO: qué haría la flota entera ahora mismo, sin
    ejecutar nada. Muestra el riesgo del conjunto (solapamiento de contactos,
    escrituras Medilink, qué se ejecutaría vs bloquearía). Read-only."""
    _check_owner(token, request)
    return JSONResponse(await simulator.simulate_fleet())


@router.get("/alma/agents/api/cycle")
async def agents_cycle(token: str | None = Query(None), request: Request = None,
                       live: int = Query(0)):
    """Ciclo capstone: cerebro + flota (intención) + ledger en un digest unificado.
    `live=1` lo recalcula en vivo; por defecto devuelve el último persistido."""
    _check_owner(token, request)
    if live:
        return JSONResponse(await capstone.run_cycle())
    return JSONResponse(capstone.load_last() or await capstone.run_cycle())


@router.get("/alma/agents/api/learning")
def agents_learning(token: str | None = Query(None), request: Request = None):
    """Loop de aprendizaje: por agente, conversión por variante + la recomendada
    (bandit UCB sobre el Ledger). Read-only."""
    _check_owner(token, request)
    return JSONResponse(learning.learning_summary())


@router.get("/alma/agents/api/events")
def agents_events(token: str | None = Query(None), request: Request = None):
    """Catálogo de eventos + qué agente reacciona a cada uno."""
    _check_owner(token, request)
    cat = {k: {"descripcion": v, "suscritos": [a.id for a in events.subscribers(k)]}
           for k, v in events.EVENTS.items()}
    return JSONResponse({"eventos": cat})


@router.post("/alma/agents/api/emit")
async def agents_emit(event_type: str = Query(...), token: str | None = Query(None),
                      request: Request = None):
    """Emite un evento en modo PREVIEW (dry): muestra quién reaccionaría y qué
    propondría, sin ejecutar nada. Para probar el bus desde el panel."""
    _check_owner(token, request)
    return JSONResponse(await events.emit(event_type, {}, dry=True))
