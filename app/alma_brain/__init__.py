"""Alma Brain — la capa agéntica de Alma.

Generaliza el patrón probado del Autopilot (PERCIBIR → DECIDIR → RAZONAR →
ACTUAR → GOBERNAR) de "solo Meta Ads" a TODOS los dominios del centro médico:
agenda, caja, conciliación, demanda, fidelización y ads.

Fases:
  1. state/sensors  → world-state global (read-only, snapshot diario).      [PERCIBIR]
  2. copilot        → Claude con tool-use sobre el estado (read-only).      [RAZONAR]
  3. tools          → registry de acciones con dry-run + aprobación.        [ACTUAR propuesto]
  4. policy         → HardLimits por dominio + ejecución gateada.           [GOBERNAR]

Principio rector (heredado de autopilot/advisor.py): **la IA propone, las
reglas mandan**. Ninguna acción que mueva plata, contacte pacientes o toque
Medilink se ejecuta sin pasar por los límites duros y la aprobación humana.

Todo es ADITIVO y degrada con gracia: si una fuente no está disponible, el
dominio se marca `available=False` y el resto del cerebro sigue operando.
Nunca tumba el bot vivo.
"""

__all__ = ["state", "sensors", "tools", "policy", "copilot", "routes"]
