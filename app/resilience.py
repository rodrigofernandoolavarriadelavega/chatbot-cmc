"""
Modo degradado: estado del servicio Medilink y throttle de notificaciones.

Cuando la API Medilink está caída, el bot deja de intentar crear/consultar citas
y en cambio:
  1. Encola la intención del paciente en `intent_queue` (session.py).
  2. Responde con un mensaje cordial pidiendo reintentar más tarde.
  3. Avisa a recepción (como máximo una vez cada 30 min) vía WhatsApp.
  4. Cuando Medilink vuelve, un cron avisa a los pacientes encolados.

No se hace auto-replay del flujo porque los slots pueden haberse movido durante
la caída. El paciente decide si quiere retomar.
"""
from datetime import datetime, timedelta, timezone

from session import (
    system_state_get,
    system_state_set,
    system_state_updated_at,
)


# ── Excepciones ───────────────────────────────────────────────────────────────

class MedilinkDown(Exception):
    """Se lanza cuando una llamada a Medilink falla tras agotar reintentos.
    medilink.py convierte httpx.RequestError/HTTPStatusError 5xx en esta excepción
    para que flows.py pueda decidir si encolar o no."""
    pass


# ── Claves en system_state ────────────────────────────────────────────────────

_KEY_MEDILINK = "medilink_status"          # "up" | "down"
_KEY_MEDILINK_REASON = "medilink_reason"
_KEY_RECEPTION_NOTIFIED = "reception_notified_at"  # timestamp ISO de última notif

RECEPTION_THROTTLE_MIN = 30


# ── Estado Medilink ───────────────────────────────────────────────────────────

def mark_medilink_down(reason: str = ""):
    """Marca Medilink como caído. Idempotente."""
    system_state_set(_KEY_MEDILINK, "down")
    if reason:
        system_state_set(_KEY_MEDILINK_REASON, reason[:500])


def mark_medilink_up():
    """Marca Medilink como operativo. Idempotente."""
    system_state_set(_KEY_MEDILINK, "up")


def is_medilink_down() -> bool:
    """True si el sistema está en modo degradado."""
    return system_state_get(_KEY_MEDILINK) == "down"


def medilink_down_since() -> str | None:
    """Timestamp ISO de cuándo se marcó Medilink como caído (si lo está)."""
    if not is_medilink_down():
        return None
    return system_state_updated_at(_KEY_MEDILINK)


def medilink_down_reason() -> str:
    """Última razón registrada de la caída (o cadena vacía)."""
    return system_state_get(_KEY_MEDILINK_REASON) or ""


# ── Throttle de notificación a recepción ──────────────────────────────────────

def should_notify_reception() -> bool:
    """True si han pasado RECEPTION_THROTTLE_MIN minutos desde la última notif."""
    last = system_state_updated_at(_KEY_RECEPTION_NOTIFIED)
    if not last:
        return True
    try:
        ts = datetime.fromisoformat(last)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        return (now - ts) >= timedelta(minutes=RECEPTION_THROTTLE_MIN)
    except (ValueError, TypeError):
        return True


def mark_reception_notified():
    """Registra que recepción fue notificada (resetea el throttle)."""
    system_state_set(_KEY_RECEPTION_NOTIFIED, datetime.now(timezone.utc).isoformat())
