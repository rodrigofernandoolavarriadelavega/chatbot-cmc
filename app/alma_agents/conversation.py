"""Agentes conversacionales — mini-diálogos dirigidos, no fire-and-forget.

Hoy un agente manda un template y listo. Esto le da la capacidad de sostener
una conversación CORTA y ACOTADA con un objetivo (negociar una hora, manejar una
objeción) y cerrarla: éxito, rechazo, o derivación a recepción.

Es un motor de micro-diálogo con tope duro de turnos, encima del piso de
guardrails. NO reemplaza a flows.py: un agente ABRE un micro-diálogo para un
paciente; mientras está abierto, los mensajes entrantes de ese paciente se rutean
acá (hook en flows, documentado, gateado). Cada mensaje saliente sigue siendo un
contacto que pasa por el presupuesto/consent.

GATING DURO: `ALMA_AGENTS_CONVERSA_ENABLED=false` por defecto. Con el flag off,
`is_enabled()` es False y nada de esto se activa en producción. ENGINE-READY:
probado de punta a punta offline; el wiring en flows.py queda como paso guardado.
"""
import json
import logging
import os
from datetime import datetime, timezone

log = logging.getLogger("bot")

MAX_TURNS_DEFAULT = 4

# Clasificación determinística de la respuesta del paciente (chileno).
_AFIRMA = ("si", "sí", "ya", "dale", "ok", "okay", "bueno", "confirmo", "confirmar",
           "perfecto", "me sirve", "claro", "obvio", "de una", "ya po", "listo")
_NIEGA = ("no", "nop", "ahora no", "no puedo", "imposible", "no me sirve", "otro día",
          "otra hora", "después", "luego", "no gracias")
_OTRA = ("otra", "otro", "cambiar", "más tarde", "mas tarde", "temprano", "tarde")


def is_enabled() -> bool:
    return os.getenv("ALMA_AGENTS_CONVERSA_ENABLED", "false").lower() in ("1", "true", "yes")


def classify_reply(text: str) -> str:
    """afirmativo | negativo | otra_opcion | ambiguo (determinístico)."""
    t = (text or "").strip().lower()
    if not t:
        return "ambiguo"
    if any(w in t for w in _OTRA):
        return "otra_opcion"
    # afirmativo: match de palabra inicial o frase corta
    if any(t == w or t.startswith(w + " ") or f" {w}" in f" {t}" for w in _AFIRMA):
        # pero "no me sirve" contiene "si"? no. ok.
        if not any(w in t for w in _NIEGA):
            return "afirmativo"
    if any(w in t for w in _NIEGA):
        return "negativo"
    return "ambiguo"


def _conn():
    from session import _conn as _session_conn
    return _session_conn()


def ensure_table() -> None:
    try:
        conn = _conn()
        with conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS alma_conversations (
                    phone       TEXT PRIMARY KEY,
                    goal        TEXT NOT NULL,
                    agent_id    TEXT,
                    turns       INTEGER DEFAULT 0,
                    max_turns   INTEGER DEFAULT 4,
                    status      TEXT DEFAULT 'abierta',   -- abierta|exito|rechazo|derivada|expirada
                    context     TEXT DEFAULT '{}',
                    updated_at  TEXT
                )
            """)
        conn.close()
    except Exception as e:  # noqa: BLE001
        log.warning("conversation.ensure_table falló: %s", e)


def _save(phone, goal, agent_id, turns, max_turns, status, context):
    try:
        ensure_table()
        conn = _conn()
        with conn:
            conn.execute(
                """INSERT INTO alma_conversations
                   (phone, goal, agent_id, turns, max_turns, status, context, updated_at)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(phone) DO UPDATE SET goal=excluded.goal, agent_id=excluded.agent_id,
                     turns=excluded.turns, max_turns=excluded.max_turns, status=excluded.status,
                     context=excluded.context, updated_at=excluded.updated_at""",
                (phone, goal, agent_id, turns, max_turns, status,
                 json.dumps(context, ensure_ascii=False),
                 datetime.now(timezone.utc).isoformat()))
        conn.close()
    except Exception as e:  # noqa: BLE001
        log.warning("conversation._save falló: %s", e)


def get_open(phone: str) -> dict | None:
    try:
        ensure_table()
        conn = _conn()
        r = conn.execute(
            "SELECT phone, goal, agent_id, turns, max_turns, status, context FROM alma_conversations WHERE phone=? AND status='abierta'",
            (phone,)).fetchone()
        conn.close()
        if not r:
            return None
        return {"phone": r[0], "goal": r[1], "agent_id": r[2], "turns": r[3],
                "max_turns": r[4], "status": r[5], "context": json.loads(r[6] or "{}")}
    except Exception as e:  # noqa: BLE001
        log.warning("conversation.get_open falló: %s", e)
        return None


# ── Política: negociar una hora ──────────────────────────────────────────────

def _negotiate_open(context: dict) -> str:
    slots = context.get("slots", [])
    s = slots[0] if slots else "una hora"
    return (f"Tenemos {s} disponible para {context.get('especialidad', 'tu atención')}. "
            f"¿Te sirve? Responde *sí* para confirmar o *otra* para ver más horas.")


def _negotiate_step(state: dict, inbound: str) -> dict:
    """Devuelve {message, status, action}. status: abierta|exito|rechazo|derivada."""
    ctx = state["context"]
    slots = ctx.get("slots", [])
    idx = ctx.get("idx", 0)
    cls = classify_reply(inbound)

    if cls == "afirmativo":
        chosen = slots[idx] if idx < len(slots) else (slots[0] if slots else None)
        return {"message": f"¡Listo! Te dejo agendado para {chosen}. Te llega la confirmación.",
                "status": "exito", "action": {"kind": "book", "slot": chosen}}
    if cls == "otra_opcion":
        nxt = idx + 1
        if nxt < len(slots):
            ctx["idx"] = nxt
            return {"message": f"También tengo {slots[nxt]}. ¿Esa te acomoda? (*sí* / *otra*)",
                    "status": "abierta", "action": None}
        return {"message": "Esas son las horas que tengo ahora. Te derivo con recepción para buscar otra. ¡Gracias!",
                "status": "derivada", "action": {"kind": "handoff"}}
    if cls == "negativo":
        return {"message": "Entendido, no hay problema. Si después quieres agendar, escríbenos cuando gustes.",
                "status": "rechazo", "action": None}
    # ambiguo
    return {"message": "¿Te confirmo esa hora? Responde *sí* para tomarla, *otra* para ver más, o *no* si prefieres dejarlo.",
            "status": "abierta", "action": None}


POLICIES = {
    "negociar_hora": {"open": _negotiate_open, "step": _negotiate_step},
}


def start(phone: str, goal: str, context: dict, agent_id: str = None,
          max_turns: int = MAX_TURNS_DEFAULT) -> dict:
    """Abre un micro-diálogo. Devuelve {message} (el primer saliente) o {skipped}."""
    if not is_enabled():
        return {"skipped": "ALMA_AGENTS_CONVERSA_ENABLED=false"}
    pol = POLICIES.get(goal)
    if not pol:
        return {"skipped": f"política desconocida: {goal}"}
    _save(phone, goal, agent_id, 0, max_turns, "abierta", context)
    return {"message": pol["open"](context), "status": "abierta"}


def handle_inbound(phone: str, text: str) -> dict | None:
    """Procesa un mensaje entrante si hay micro-diálogo abierto para ese teléfono.

    Devuelve {message, status, action} o None si no hay diálogo abierto (→ flujo
    normal). El hook en flows.py llamaría esto ANTES del ruteo normal, gateado.
    """
    if not is_enabled():
        return None
    state = get_open(phone)
    if not state:
        return None
    pol = POLICIES.get(state["goal"])
    if not pol:
        return None
    turns = state["turns"] + 1
    # Tope duro de turnos → derivar a recepción.
    if turns > state["max_turns"]:
        _save(phone, state["goal"], state["agent_id"], turns, state["max_turns"],
              "derivada", state["context"])
        return {"message": "Te derivo con recepción para que te ayuden directo. ¡Gracias por la paciencia!",
                "status": "derivada", "action": {"kind": "handoff"}}
    out = pol["step"](state, text)
    _save(phone, state["goal"], state["agent_id"], turns, state["max_turns"],
          out["status"], state["context"])
    return out
