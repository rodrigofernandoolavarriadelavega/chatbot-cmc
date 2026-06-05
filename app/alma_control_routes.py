"""Sala de Máquinas — /alma/control.

Mapa único + control en vivo de TODO el poder agéntico de Alma (flota de
agentes + orquestadores del cerebro + operativa). Descubre las piezas desde
los registries (no hay lista hardcodeada → se mantiene en sync), muestra flag /
riesgo / estado de cada una, y permite:

  - Encender/apagar en vivo (sin SSH ni reinicio) vía alma_switchboard.
  - Ensayar (dry-run) cualquier pieza: preview de qué haría, sin tocar a nadie.

Solo dueño (OLACORE_TOKEN). El piso legal (consent Ley 21.719, escritura
Medilink, riesgo extremo, auto-confirmación) se muestra como candado de
solo-lectura: no se conmuta desde acá.
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Query, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

from config import OLACORE_TOKEN
import alma_switchboard as sb

log = logging.getLogger("bot")
router = APIRouter(tags=["alma-control"])

_TEMPLATE = Path(__file__).parent.parent / "templates" / "alma_control.html"

# Riesgo por orquestador (meta() no lo trae). Default "medio".
_ORQ_RISK = {
    "briefing": "bajo", "snapshot": "bajo", "ges_backlog": "bajo",
    "ficha_incompleta": "bajo", "preventivo_edad": "bajo", "cumpleanos": "bajo",
    "confirmaciones": "medio", "control_cronico": "medio", "adherencia_kine": "medio",
    "controles_ortodoncia": "medio", "reactivacion": "medio", "no_show_recovery": "medio",
    "crosssell_postconsulta": "medio", "reputacion_nps": "medio", "resultados_examenes": "medio",
    "referral_sin_cerrar": "medio", "primera_vez_sin_retorno": "medio",
    "conversacion_parada": "medio", "agenda_salud": "medio",
    "hueco_proactivo": "alto", "demanda_abrir_agenda": "alto",
    "inventario_compra": "alto", "ads_anomalia": "alto",
    "cobranza_ortodoncia": "extremo", "campanas_estacionales": "alto",
}

# ── auth: solo dueño ─────────────────────────────────────────────────────────

def _check_owner(token: str | None, request: Request | None) -> str:
    eff = token
    if not eff and request is not None:
        eff = request.cookies.get("admin_token")
    if eff != OLACORE_TOKEN:
        raise HTTPException(status_code=403, detail="Solo el dueño puede operar la Sala de Máquinas")
    return eff


def _actor(token: str | None, request: Request | None) -> str:
    """Etiqueta legible para el log de auditoría."""
    try:
        from config import ALMA_PROFILES
        eff = token or (request.cookies.get("admin_token") if request else None)
        prof = ALMA_PROFILES.get(eff)
        if prof:
            return f"dueño ({prof.get('variante', 'OLACORE')})"
    except Exception:
        pass
    return "dueño"


# ── descubrimiento del mapa ──────────────────────────────────────────────────

def _build_map() -> dict:
    overrides = sb.get_overrides()

    def is_over(flag: str) -> bool:
        return flag in overrides

    def _ap_flag(name: str) -> bool:
        # Valor efectivo de un flag del Autopilot (switchboard > env), mismo helper
        # que usa el Autopilot, para que panel y motor muestren/usen lo mismo.
        try:
            from autopilot.flags import flag_on
            return flag_on(name)
        except Exception:
            return False

    # --- Maestros (conmutables) ---
    from alma_agents import guardrails
    fs = guardrails.fleet_status()
    masters = [
        {"flag": "ALMA_AGENTS_ENABLED", "label": "Maestro de la flota",
         "descr": "Kill-switch global: si está apagado, ningún agente corre (ni en propose/execute).",
         "value": fs["master_enabled"], "overridden": is_over("ALMA_AGENTS_ENABLED")},
        {"flag": "ALMA_AGENTS_EXECUTE", "label": "Ejecución real (vs dry-run)",
         "descr": "Apagado = todo es ensayo en seco. Encendido = los agentes habilitados actúan de verdad.",
         "value": fs["execute_enabled"], "overridden": is_over("ALMA_AGENTS_EXECUTE")},
        {"flag": "ALMA_OPERATIVA_ENABLED", "label": "Operativa: relleno de cupos",
         "descr": "Al cancelarse una cita, invita a la lista de espera y aparta el cupo.",
         "value": sb.effective("ALMA_OPERATIVA_ENABLED",
                               __import__("os").getenv("ALMA_OPERATIVA_ENABLED", "false").lower() in ("true", "1", "yes")),
         "overridden": is_over("ALMA_OPERATIVA_ENABLED")},
        # --- Autopilot Meta Ads (cerebro de marketing) ---
        {"flag": "AUTOPILOT_ENABLED", "label": "Autopilot: motor de Meta Ads",
         "descr": "El motor analiza campañas (CAC real) y ENCOLA propuestas de presupuesto. Sin esto no propone nada.",
         "value": _ap_flag("AUTOPILOT_ENABLED"), "overridden": is_over("AUTOPILOT_ENABLED")},
        {"flag": "AUTOPILOT_EXECUTE", "label": "Autopilot: aplicar a Meta (vs simular)",
         "descr": "Apagado = simula los cambios. Encendido = escribe presupuestos reales en Meta (tras tu aprobación).",
         "value": _ap_flag("AUTOPILOT_EXECUTE"), "overridden": is_over("AUTOPILOT_EXECUTE")},
        {"flag": "AUTOPILOT_AUTOAPPLY", "label": "Autopilot: auto-aplicar (Fase 3)",
         "descr": "Aplica los movimientos de plata de ALTA confianza sin esperar tu OK. OJO: no prendas esto y el agente 'ads_executor' de la flota a la vez (doble acción sobre las mismas campañas).",
         "value": _ap_flag("AUTOPILOT_AUTOAPPLY"), "overridden": is_over("AUTOPILOT_AUTOAPPLY")},
    ]

    # --- Candados legales (solo lectura) ---
    import os
    def _envbool(n, d="false"):
        return os.getenv(n, d).lower() in ("true", "1", "yes")
    locks = [
        {"flag": "ALMA_BRAIN_ALLOW_MEDILINK_WRITES", "label": "Escritura en Medilink",
         "descr": "Permite que un agente cree/anule citas reales. Solo por .env.",
         "value": fs["medilink_writes_allowed"]},
        {"flag": "ALMA_AGENTS_ALLOW_EXTREME", "label": "Acciones de riesgo extremo",
         "descr": "Habilita lo extremo (p. ej. cobranza). Solo por .env.",
         "value": fs["extreme_allowed"]},
        {"flag": "ALMA_AGENTS_REQUIRE_CONSENT", "label": "Exige consentimiento (Ley 21.719)",
         "descr": "Bloquea contacto a pacientes sin opt-in vigente. Debe quedar SÍ.",
         "value": fs["require_consent"]},
        {"flag": "ALMA_OPERATIVA_AUTOCONFIRM", "label": "Auto-confirmar cupos",
         "descr": "Confirma en Medilink sin pasar por recepción. Solo por .env.",
         "value": _envbool("ALMA_OPERATIVA_AUTOCONFIRM")},
        {"flag": "_quiet_hours", "label": "Horas de silencio (21–09 CLT)",
         "descr": "No se contacta a pacientes de madrugada.",
         "value": fs["in_quiet_hours"], "info": True},
        {"flag": "_contact_budget", "label": f"Presupuesto de contacto: {fs['contact_max_week']}/semana",
         "descr": "Tope anti-spam de mensajes por paciente.",
         "value": True, "info": True},
    ]

    # --- Capa 1: Flota de agentes ---
    from alma_agents import registry, store
    fleet_pieces = []
    for aid, a in sorted(registry.all_agents().items()):
        last = None
        try:
            lr = store.load_last_run(aid)
            if lr:
                last = {"ran_at": lr.get("ran_at"), "dry_run": lr.get("dry_run"),
                        "notes": lr.get("notes")}
        except Exception:
            pass
        fleet_pieces.append({
            "id": aid, "layer": "flota", "name": a.name, "descr": a.descr,
            "risk": a.risk, "flag": a.flag,
            "enabled": guardrails.agent_enabled(a.flag),
            "overridden": is_over(a.flag),
            "last_run": last,
        })

    # --- Capa 2: Orquestadores del cerebro ---
    orq_pieces = []
    try:
        from alma_brain.orchestrators import catalog
        for m in catalog():
            name = m.get("name")
            orq_pieces.append({
                "id": name, "layer": "orq", "name": name,
                "descr": m.get("descr") or m.get("description") or "",
                "risk": _ORQ_RISK.get(name, "medio"),
                "flag": m.get("flag"),
                "enabled": bool(m.get("enabled")),
                "overridden": is_over(m.get("flag")),
                "cadencia": m.get("cadencia"),
            })
    except Exception as e:
        log.warning("alma_control: catálogo de orquestadores no disponible: %s", e)

    return {
        "masters": masters,
        "locks": locks,
        "layers": [
            {"key": "flota", "label": f"Flota de Agentes ({len(fleet_pieces)})",
             "descr": "Agentes autónomos que operan la clínica. Encender exige maestro + ejecución.",
             "pieces": fleet_pieces},
            {"key": "orq", "label": f"Orquestadores del Cerebro ({len(orq_pieces)})",
             "descr": "Rutinas que detectan oportunidades y proponen acciones por su cadencia.",
             "pieces": orq_pieces},
        ],
        "overrides": overrides,
        "n_overrides": len(overrides),
        "audit": sb.get_audit(30),
    }


# ── flags conmutables (whitelist auto-derivada) ──────────────────────────────

def _toggleable_flags() -> set[str]:
    flags = {"ALMA_AGENTS_ENABLED", "ALMA_AGENTS_EXECUTE", "ALMA_OPERATIVA_ENABLED",
             "AUTOPILOT_ENABLED", "AUTOPILOT_EXECUTE", "AUTOPILOT_AUTOAPPLY"}
    try:
        from alma_agents import registry
        flags |= {a.flag for a in registry.all_agents().values()}
    except Exception:
        pass
    try:
        from alma_brain.orchestrators import catalog
        flags |= {m.get("flag") for m in catalog() if m.get("flag")}
    except Exception:
        pass
    return flags


# ── endpoints ────────────────────────────────────────────────────────────────

@router.get("/alma/control", response_class=HTMLResponse)
def control_page(token: str | None = Query(None), request: Request = None):
    _check_owner(token, request)
    if not _TEMPLATE.exists():
        raise HTTPException(500, "template no encontrado")
    return _TEMPLATE.read_text(encoding="utf-8")


@router.get("/alma/control/api/map")
def control_map(token: str | None = Query(None), request: Request = None):
    _check_owner(token, request)
    return JSONResponse(_build_map())


@router.get("/alma/control/api/audit")
def control_audit(limit: int = 50, token: str | None = Query(None), request: Request = None):
    _check_owner(token, request)
    return JSONResponse({"audit": sb.get_audit(limit)})


@router.post("/alma/control/api/toggle")
async def control_toggle(request: Request, token: str | None = Query(None)):
    _check_owner(token, request)
    body = await request.json()
    flag = (body.get("flag") or "").strip()
    value = body.get("value")  # true | false | null(=limpiar override)
    if flag not in _toggleable_flags():
        raise HTTPException(400, f"flag no conmutable desde el panel: {flag}")
    if value is not None and not isinstance(value, bool):
        raise HTTPException(400, "value debe ser true, false o null")
    overrides = sb.set_flag(flag, value, actor=_actor(token, request))
    return JSONResponse({"ok": True, "flag": flag, "value": value,
                         "overrides": overrides})


@router.post("/alma/control/api/reset")
def control_reset(token: str | None = Query(None), request: Request = None):
    _check_owner(token, request)
    return JSONResponse({"ok": True, "overrides": sb.reset(actor=_actor(token, request))})


@router.post("/alma/control/api/dryrun")
async def control_dryrun(request: Request, token: str | None = Query(None)):
    _check_owner(token, request)
    body = await request.json()
    layer = (body.get("layer") or "").strip()
    pid = (body.get("id") or "").strip()

    if layer == "flota":
        from alma_agents import registry, guardrails
        agent = registry.get(pid)
        if agent is None:
            raise HTTPException(404, "agente no encontrado")
        try:
            ctx = await agent.perceive()
            actions = await agent.decide(ctx)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(500, f"preview falló: {e}")
        acc = []
        for a in actions:
            allow, reason = guardrails.authorize(a)
            acc.append({"summary": a.summary, "risk": a.risk,
                        "target": a.target, "se_ejecutaria": allow, "motivo": reason})
        return JSONResponse({"layer": layer, "id": pid, "n": len(acc), "acciones": acc})

    if layer == "orq":
        from alma_brain.orchestrators import run_one
        res = await run_one(pid, mode="dry")
        props = res.get("proposals", []) if isinstance(res, dict) else []
        acc = [{"summary": p.get("summary") or p.get("titulo") or str(p),
                "risk": p.get("risk", ""), "target": p.get("target", "")}
               for p in props]
        return JSONResponse({"layer": layer, "id": pid, "n": len(acc),
                             "acciones": acc, "notes": res.get("notes") if isinstance(res, dict) else None})

    raise HTTPException(400, "layer debe ser 'flota' u 'orq'")
