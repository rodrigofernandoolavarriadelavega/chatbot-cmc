"""Plano de control en lenguaje natural — manejas la flota por intención.

En vez de prender flags y tocar pesos a mano, le dices a Alma en español qué
quieres esta semana ("enfócate en llenar la agenda de kinesiología", "baja la
cobranza", "prioriza recuperar pacientes") y se traduce a overrides del runtime
(foco + prioridad por agente) que el broker y la flota ya leen.

Dos caminos:
  - `interpret(texto)`  — determinista, por vocabulario. Offline, testeable, es
    el que corre por defecto y nunca falla.
  - `interpret_llm(texto)` — usa Claude (tool-use) para los pedidos difusos; cae
    al determinista si no hay API. (async)

`apply(texto)` interpreta y escribe el runtime. NO enciende agentes (eso sigue
siendo piso de .env); solo orienta la flota cuando ya está encendida.
"""
import logging
import re

from . import runtime

log = logging.getLogger("bot")

# Vocabulario → (focus tag, agentes a inclinar). Determinístico y auditable.
_VOCAB = [
    (("kine", "kinesiolog", "kinési", "kinesi"), "kinesiologia", ["adherencia_kine", "yield_agenda"]),
    (("ortodonc", "bracket", "frenillo", "diente"), "ortodoncia", ["control_cronico", "yield_agenda"]),
    (("llenar", "agenda", "cupos", "cupo", "vacío", "vacio", "hueco"), "agenda", ["yield_agenda"]),
    (("recuper", "winback", "inactiv", "perdido", "reactiv"), "reactivacion", ["reactivacion_winback"]),
    (("cobr", "copago", "deuda", "pagos pendientes", "moroso"), "cobranza", ["cobranza"]),
    (("reputaci", "reseñ", "resena", "review", "nps", "google", "estrella"), "reputacion", ["reputacion"]),
    (("pauta", "ads", "publicidad", "campañ", "campan", "anuncio"), "ads", ["ads_executor", "creativos"]),
    (("inventario", "stock", "insumo", "comprar"), "inventario", ["inventario_auto"]),
    (("control", "crónico", "cronico", "hipertens", "diabét", "diabet"), "control_cronico", ["control_cronico"]),
    (("post", "seguimiento", "postconsulta"), "postconsulta", ["postconsulta_smart"]),
    (("seo", "contenido", "blog", "posicionamiento"), "seo", ["seo_content"]),
]

# Palabras que invierten el sentido (bajar/pausar en vez de priorizar).
_DOWN = ("baja", "bajar", "menos", "pausa", "pausá", "pausar", "deten", "detén",
         "frena", "frená", "suspende", "para ", "parar", "apaga", "apagá", "nada de")
_UP_W = 2.0
_DOWN_W = 0.4


def interpret(text: str) -> dict:
    """Traduce una instrucción a overrides {focus, agent_weights, notes, matches}."""
    t = (text or "").lower()
    down = any(w in t for w in _DOWN)
    weight = _DOWN_W if down else _UP_W
    focus, weights, matches = [], {}, []
    for kws, tag, agents in _VOCAB:
        if any(k in t for k in kws):
            matches.append(tag)
            if tag not in focus:
                focus.append(tag)
            for a in agents:
                weights[a] = weight
    # objetivo numérico opcional ("recupera 10 pacientes")
    m = re.search(r"\b(\d{1,3})\b", t)
    target = int(m.group(1)) if m else None
    return {
        "focus": focus,
        "agent_weights": weights,
        "direction": "bajar" if down else "priorizar",
        "target": target,
        "notes": text,
        "matches": matches,
        "source": "deterministico",
    }


def apply(text: str) -> dict:
    """Interpreta y escribe el runtime. Devuelve {overrides, runtime}."""
    ov = interpret(text)
    state = runtime.apply_overrides({
        "focus": ov["focus"] or None,
        "agent_weights": ov["agent_weights"],
        "notes": ov["notes"],
    })
    return {"overrides": ov, "runtime": state}


# ── Camino LLM (opcional, para pedidos difusos) ──────────────────────────────

_LLM_SYSTEM = (
    "Eres el plano de control de una flota de agentes de una clínica. El dueño te "
    "da una instrucción en español. Devuelve SOLO un JSON con: focus (lista de "
    "etiquetas), agent_weights (objeto agent_id->peso, 2.0=prioriza, 0.4=baja), "
    "notes (texto). Agentes válidos: yield_agenda, adherencia_kine, control_cronico, "
    "reactivacion_winback, reputacion, cobranza, ads_executor, creativos, "
    "postconsulta_smart, inventario_auto, seo_content, abandono_recovery, "
    "pricing_analyst. No inventes agentes."
)


async def interpret_llm(text: str) -> dict:
    """Usa Claude para interpretar; cae al determinista si no hay API o falla."""
    import os
    if not os.getenv("ANTHROPIC_API_KEY"):
        return {**interpret(text), "source": "deterministico (sin API)"}
    try:
        from anthropic import AsyncAnthropic
        client = AsyncAnthropic()
        msg = await client.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=400,
            system=_LLM_SYSTEM,
            messages=[{"role": "user", "content": text}],
        )
        import json
        raw = msg.content[0].text
        # extraer el primer bloque JSON
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        data = json.loads(m.group(0)) if m else {}
        data.setdefault("focus", [])
        data.setdefault("agent_weights", {})
        data.setdefault("notes", text)
        data["source"] = "claude"
        return data
    except Exception as e:  # noqa: BLE001
        log.warning("control_plane.interpret_llm cayó al determinista: %s", e)
        return {**interpret(text), "source": f"deterministico (fallback: {e})"}
