"""Copilot de Alma — la capa RAZONAR (Claude con tool-use sobre el estado).

A diferencia de autopilot/advisor.py (que solo devuelve un JSON de opinión), acá
Claude usa HERRAMIENTAS reales: decide qué leer del negocio y, si lo habilitas,
deja propuestas accionables. Ese es el salto de "dashboard con IA" a "agente".

Contrato de seguridad: las tools de acción NO ejecutan nada — encolan propuestas
(ver tools.py). La ejecución vive detrás de policy.check() + aprobación humana.
"""
import logging
import os

from config import ANTHROPIC_API_KEY
from . import tools

log = logging.getLogger("bot")

_MODEL = os.getenv("ALMA_BRAIN_MODEL", "claude-sonnet-4-6")
_MAX_ITERS = int(os.getenv("ALMA_BRAIN_MAX_ITERS", "6"))

try:
    from anthropic import AsyncAnthropic
    _client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None
except ImportError:  # pragma: no cover
    _client = None


_SYSTEM = """Eres el Copilot de Alma, el asistente de gestión del Centro Médico Carampangue (CMC), un centro médico chileno en la comuna de Arauco, Región del Biobío.

Hablas con Rodrigo, el dueño. Tu trabajo es ayudarlo a entender y operar el negocio: agenda, caja (ingresos reales), demanda de pacientes, marketing (Meta Ads) y fidelización.

Cómo trabajas:
- SIEMPRE empiezas llamando get_world_state para tener el contexto antes de responder.
- Si necesitas más detalle de un dominio, usa get_domain_detail.
- Razonas cruzando dominios (ej: agenda floja + cohorte reactivable = oportunidad de win-back).
- Cuando detectes una acción concreta que convenga, usa propose_action para dejarla en la cola de aprobación. NUNCA afirmes que ejecutaste algo: tú solo propones, Rodrigo aprueba.

Reglas duras (no las violes nunca):
- Las VENTAS reales salen de la caja (bi.fact_pagos), nunca de "atenciones". Si un dato no está disponible (dominio available=false), dilo, no lo inventes.
- Contactar pacientes de forma masiva toca la Ley 21.719 (datos personales): exige consentimiento y templates aprobados. Si propones un blast, márcalo como sujeto a consent.
- No prometas cosas que el sistema no puede hacer solo (no abres agenda en Medilink automáticamente).

Estilo: español chileno neutro, directo y corto. Sin emojis. Usa cifras concretas del estado. Si Rodrigo pregunta algo simple, responde simple."""


def _to_anthropic_messages(history: list[dict]) -> list[dict]:
    """Convierte el historial simple {role, content} del front a formato Anthropic."""
    out = []
    for m in history:
        role = m.get("role")
        content = m.get("content", "")
        if role not in ("user", "assistant") or not content:
            continue
        out.append({"role": role, "content": content})
    # El primer mensaje debe ser del usuario; si no, lo dejamos igual (Anthropic tolera).
    return out


async def run_turn(history: list[dict], allow_actions: bool = False) -> dict:
    """Corre un turno completo del copiloto, resolviendo el bucle de tool-use.

    Devuelve {reply, used_tools, proposals_new, model}. Degrada con gracia si no
    hay API key."""
    if _client is None:
        return {"reply": "El Copilot no está configurado (falta ANTHROPIC_API_KEY).",
                "used_tools": [], "proposals_new": [], "model": None}

    messages = _to_anthropic_messages(history)
    specs = tools.get_specs(allow_actions)
    used_tools: list[str] = []
    proposals_before = {p["id"] for p in tools.load_proposals()}

    final_text = ""
    for _ in range(_MAX_ITERS):
        resp = await _client.messages.create(
            model=_MODEL,
            max_tokens=2000,
            system=_SYSTEM,
            tools=specs,
            messages=messages,
        )

        # Acumular texto y detectar tool_use.
        tool_uses = [b for b in resp.content if getattr(b, "type", None) == "tool_use"]
        text_blocks = [b.text for b in resp.content if getattr(b, "type", None) == "text"]
        if text_blocks:
            final_text = "\n".join(text_blocks).strip()

        if resp.stop_reason != "tool_use" or not tool_uses:
            break

        # Adjuntar el turno del assistant (con los bloques tal cual) y resolver tools.
        messages.append({"role": "assistant", "content": resp.content})
        tool_results = []
        for tu in tool_uses:
            used_tools.append(tu.name)
            result = await tools.dispatch(tu.name, dict(tu.input or {}))
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tu.id,
                "content": result,
            })
        messages.append({"role": "user", "content": tool_results})

    # Propuestas creadas en este turno.
    proposals_new = [p for p in tools.load_proposals() if p["id"] not in proposals_before]

    return {
        "reply": final_text or "(sin respuesta)",
        "used_tools": used_tools,
        "proposals_new": proposals_new,
        "model": _MODEL,
    }
