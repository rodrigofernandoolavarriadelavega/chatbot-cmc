"""Autopilot de marketing CMC — bucle cerrado: medir → decidir → (proponer/actuar) → re-medir.

Fase 0  (lector):   world_state.py  — estado unificado por campaña.
Fase 1  (dry-run):  policy.py + advisor.py + engine.py — proponen acciones, NO ejecutan.
Fase 2+ (ejecución con límites duros): pendiente de aprobación explícita.

El módulo es read-only por defecto. Cualquier escritura a la Marketing API
está detrás de `AUTOPILOT_EXECUTE` (config) y nunca se activa en Fase 0/1.
"""
