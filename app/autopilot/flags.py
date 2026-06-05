"""Lectura de los flags del Autopilot con override EN VIVO desde la Sala de Máquinas.

Prioridad: override del switchboard (alma_switchboard, prendido/apagado desde
/alma/control sin reiniciar) > variable de entorno > default. Si el switchboard no
está disponible, cae limpio al valor de entorno → comportamiento idéntico a antes.
"""
import os


def flag_on(name: str, default: str = "false") -> bool:
    env_on = os.getenv(name, default).lower() in ("true", "1", "yes")
    try:
        import alma_switchboard as sb
        return sb.effective(name, env_on)
    except Exception:  # noqa: BLE001 — sin switchboard, mandan el env/default
        return env_on
