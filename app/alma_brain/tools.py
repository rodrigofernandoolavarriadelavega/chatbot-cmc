"""Registry de herramientas del cerebro de Alma + cola de propuestas.

Dos familias:
  - LECTURA (Fase 2): el copiloto las ejecuta al instante, son read-only.
  - ACCIÓN (Fase 3): el copiloto NO ejecuta nada; registra una PROPUESTA en la
    cola. Rodrigo aprueba/rechaza en la UI. Al aprobar (Fase 4), se pasa por
    `policy.check()` y, si hay executor registrado y los límites lo permiten, se
    ejecuta; si no, queda como tarea manual con los pasos.

Esto materializa "la IA propone, las reglas mandan": el modelo nunca llama a un
executor directo — solo deja propuestas que un humano libera y los límites duros
filtran.
"""
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from . import state as brain_state
from . import policy

log = logging.getLogger("bot")

_PROPOSALS_PATH = os.getenv("ALMA_BRAIN_PROPOSALS_PATH", "data/alma_brain_proposals.json")
_DECISION_LOG = os.getenv("ALMA_BRAIN_DECISION_LOG", "data/alma_brain_decisions.log")


# ── Especificaciones de tools (formato Anthropic) ────────────────────────────

TOOL_SPECS_READ = [
    {
        "name": "get_world_state",
        "description": ("Devuelve el snapshot global del negocio (agenda, caja, demanda, "
                        "ads, fidelización) con sus KPIs y las alertas cross-domain ya "
                        "derivadas. Úsalo SIEMPRE primero para tener contexto."),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_domain_detail",
        "description": "Detalle más profundo de un dominio específico (ventana ampliada / más filas).",
        "input_schema": {
            "type": "object",
            "properties": {
                "domain": {"type": "string",
                           "enum": ["agenda", "caja", "demanda", "ads", "fidelizacion"]},
                "window_days": {"type": "integer", "description": "Ventana en días (default 30)."},
            },
            "required": ["domain"],
        },
    },
    {
        "name": "get_worklist_hoy",
        "description": ("Lista de hoy de reenganche clínico: pacientes a contactar cruzando TODOS "
                        "los programas de especialidad (Kine, Ortodoncia, Nutrición, Psicología, "
                        "Cardiología, etc.) priorizados por urgencia (riesgo de abandono / control "
                        "vencido) y días sin venir. Úsalo para responder '¿a quién hay que llamar/"
                        "contactar hoy?' o para proponer reenganches concretos."),
        "input_schema": {
            "type": "object",
            "properties": {
                "programa": {"type": "string", "description": "Filtrar a un programa puntual (opcional). Ej: kine, ortodoncia, nutricion."},
                "limit": {"type": "integer", "description": "Máximo de pacientes a devolver (default 40)."},
            },
        },
    },
]

TOOL_SPECS_ACTION = [
    {
        "name": "propose_action",
        "description": ("Registra una RECOMENDACIÓN accionable en la cola de aprobación. NO "
                        "la ejecuta — Rodrigo la aprueba o rechaza. Úsala cuando detectes una "
                        "acción concreta que convenga tomar."),
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string",
                         "description": ("Tipo de acción. Usa uno de: winback_blast, email_blast, "
                                         "crosssell_blast, reactivacion_blast, abrir_agenda, "
                                         "ads_budget_change, ads_dryrun_refresh, tarea_manual.")},
                "summary": {"type": "string", "description": "Qué hacer, en 1 frase clara para Rodrigo."},
                "rationale": {"type": "string", "description": "Por qué — apoyado en los datos del estado."},
                "params": {"type": "object", "description": "Parámetros estructurados (especialidad, n_destinatarios, step_pct, etc.)."},
                "impact": {"type": "string", "description": "Impacto esperado / a qué KPI mueve."},
            },
            "required": ["kind", "summary", "rationale"],
        },
    },
]


def get_specs(allow_actions: bool) -> list[dict]:
    return TOOL_SPECS_READ + (TOOL_SPECS_ACTION if allow_actions else [])


# ── Dispatch (el copiloto llama acá con el resultado de cada tool_use) ───────

async def dispatch(name: str, tool_input: dict) -> str:
    """Ejecuta una tool de LECTURA o registra una propuesta. Devuelve string
    (contenido del tool_result que vuelve al modelo)."""
    try:
        if name == "get_world_state":
            st = brain_state.load_state() or brain_state.build_and_save(7)
            return json.dumps(st, ensure_ascii=False, default=str)

        if name == "get_domain_detail":
            from . import sensors
            domain = tool_input.get("domain")
            wd = int(tool_input.get("window_days", 30) or 30)
            fn = {
                "agenda": lambda: sensors.sense_agenda(wd),
                "caja": lambda: sensors.sense_caja(wd),
                "demanda": lambda: sensors.sense_demanda(wd),
                "ads": sensors.sense_ads,
                "fidelizacion": sensors.sense_fidelizacion,
            }.get(domain)
            if fn is None:
                return json.dumps({"error": f"dominio desconocido: {domain}"})
            return json.dumps(fn(), ensure_ascii=False, default=str)

        if name == "get_worklist_hoy":
            import programas as _prog
            d = _prog.build_digest(6)
            items = d["items"]
            filtro = (tool_input.get("programa") or "").strip().lower()
            if filtro:
                items = [it for it in items if it["programa"] == filtro or filtro in it["programa_label"].lower()]
            limit = int(tool_input.get("limit", 40) or 40)
            return json.dumps({
                "total": len(items), "por_programa": d["por_programa"],
                "source_status": d["source_status"],
                "pacientes": [
                    {"paciente": it["paciente"], "programa": it["programa_label"],
                     "estado": it["estado"], "dias_sin_visita": it["dias_sin_visita"],
                     "telefono": it["telefono"]}
                    for it in items[:limit]
                ],
            }, ensure_ascii=False, default=str)

        if name == "propose_action":
            prop = add_proposal(tool_input)
            return json.dumps({"ok": True, "proposal_id": prop["id"],
                               "estado": "encolada para aprobación de Rodrigo",
                               "summary": prop["summary"]}, ensure_ascii=False)

        return json.dumps({"error": f"tool desconocida: {name}"})
    except Exception as e:  # noqa: BLE001 — un fallo de tool no debe romper el turno
        log.warning("alma_brain tool %s falló: %s", name, e)
        return json.dumps({"error": str(e)})


# ── Cola de propuestas (persistente, atómica) ────────────────────────────────

def load_proposals() -> list[dict]:
    try:
        with open(_PROPOSALS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return []
    except Exception as e:  # noqa: BLE001
        log.warning("alma_brain: no pude leer propuestas (%s)", e)
        return []


def _save_proposals(items: list[dict]) -> None:
    os.makedirs(os.path.dirname(_PROPOSALS_PATH) or ".", exist_ok=True)
    tmp = _PROPOSALS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, default=str)
    os.replace(tmp, _PROPOSALS_PATH)


def add_proposal(data: dict) -> dict:
    items = load_proposals()
    # id determinístico-incremental (sin Math.random): prefijo + correlativo.
    pid = f"p{len(items) + 1:04d}-{int(datetime.now(timezone.utc).timestamp())}"
    prop = {
        "id": pid,
        "kind": data.get("kind", "tarea_manual"),
        "summary": data.get("summary", ""),
        "rationale": data.get("rationale", ""),
        "params": data.get("params", {}) or {},
        "impact": data.get("impact", ""),
        "estado": "pendiente",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    items.append(prop)
    _save_proposals(items)
    log.info("alma_brain: propuesta encolada %s (%s)", pid, prop["kind"])
    return prop


def set_status(pid: str, estado: str, extra: dict | None = None) -> dict | None:
    items = load_proposals()
    found = None
    for p in items:
        if p["id"] == pid:
            p["estado"] = estado
            p["resolved_at"] = datetime.now(timezone.utc).isoformat()
            if extra:
                p.update(extra)
            found = p
            break
    if found:
        _save_proposals(items)
    return found


def reject_proposal(pid: str) -> dict:
    p = set_status(pid, "rechazada")
    _decision_log("RECHAZADA", p or {"id": pid})
    return {"ok": bool(p), "proposal": p}


# ── Fase 4: ejecución acotada (executors + policy gate) ──────────────────────

async def _exec_ads_dryrun_refresh(params: dict) -> dict:
    """Único executor seguro auto-ejecutable: re-corre el dry-run del autopilot
    (read-only sobre Meta, no cambia nada). Refresca las recomendaciones de ads."""
    from autopilot.engine import run_dry_run
    run = await run_dry_run(window_days=int(params.get("window_days", 7) or 7))
    return {"acciones_autopilot": len(run.actions)}


# Mapa kind → executor. Lo que NO está acá queda como tarea manual al aprobar
# (el cerebro registra la decisión pero no actúa solo — diseño conservador).
_EXECUTORS = {
    "ads_dryrun_refresh": _exec_ads_dryrun_refresh,
}


async def approve_and_execute(pid: str, approved_by: str) -> dict:
    """Aprueba una propuesta. Pasa por policy.check(); si pasa y hay executor,
    ejecuta; si no, queda aprobada-para-manual."""
    items = load_proposals()
    prop = next((p for p in items if p["id"] == pid), None)
    if prop is None:
        return {"ok": False, "error": "propuesta no encontrada"}
    if prop["estado"] not in ("pendiente",):
        return {"ok": False, "error": f"propuesta ya está en estado '{prop['estado']}'"}

    kind = prop["kind"]
    params = prop.get("params", {})
    ok, reason = policy.check(kind, params)

    if not ok:
        # No se puede ejecutar automáticamente → queda como tarea manual aprobada.
        set_status(pid, "aprobada_manual", {"approved_by": approved_by, "policy_reason": reason})
        _decision_log("APROBADA_MANUAL", {**prop, "approved_by": approved_by, "motivo": reason})
        return {"ok": True, "executed": False, "estado": "aprobada_manual",
                "motivo": reason,
                "mensaje": "Aprobada, pero requiere acción manual (límite duro o sin executor)."}

    executor = _EXECUTORS.get(kind)
    if executor is None:
        set_status(pid, "aprobada_manual", {"approved_by": approved_by,
                                            "policy_reason": "sin executor automático"})
        _decision_log("APROBADA_MANUAL", {**prop, "approved_by": approved_by})
        return {"ok": True, "executed": False, "estado": "aprobada_manual",
                "mensaje": "Aprobada. No hay executor automático para este tipo; hacerla manual."}

    try:
        result = await executor(params)
    except Exception as e:  # noqa: BLE001
        set_status(pid, "error", {"approved_by": approved_by, "error": str(e)})
        _decision_log("ERROR_EJECUCION", {**prop, "error": str(e)})
        return {"ok": False, "error": f"executor falló: {e}"}

    set_status(pid, "ejecutada", {"approved_by": approved_by, "resultado": result})
    _decision_log("EJECUTADA", {**prop, "approved_by": approved_by, "resultado": result})
    return {"ok": True, "executed": True, "estado": "ejecutada", "resultado": result}


def _decision_log(accion: str, prop: dict) -> None:
    """Traza auditable de toda decisión (igual que autopilot._append_decision_log)."""
    try:
        os.makedirs(os.path.dirname(_DECISION_LOG) or ".", exist_ok=True)
        with open(_DECISION_LOG, "a", encoding="utf-8") as f:
            ts = datetime.now(timezone.utc).isoformat()
            f.write(f"{ts}\t{accion}\t{prop.get('id','')}\t{prop.get('kind','')}\t"
                    f"{prop.get('summary','')}\t{json.dumps(prop.get('params',{}), ensure_ascii=False)}\n")
    except Exception as e:  # noqa: BLE001
        log.warning("alma_brain: no pude escribir decision log (%s)", e)
