"""Alma Agents — flota de agentes autónomos para que la clínica se opere sola.

Cada agente percibe → decide → (si los guardrails lo permiten) actúa. Toda
acción de todo agente pasa por `guardrails.authorize()` (consent Ley 21.719,
horas de silencio, presupuesto de contacto por paciente, escrituras Medilink,
riesgo extremo). Gating en cascada con defaults OFF: encender es deliberado.

Ver docs/ALMA_AGENTS.md. Construido 2026-06-02.
"""

__all__ = ["base", "guardrails", "store", "registry", "scheduler_hook"]
