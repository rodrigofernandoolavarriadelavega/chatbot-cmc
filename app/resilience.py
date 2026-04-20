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
_KEY_RECEPTION_NOTIFIED = "reception_notified_at"  # timestamp ISO de última notif de "sigue caído"
_KEY_RECOVERY_NOTIFIED = "recovery_notified_at"    # timestamp ISO de última notif de "recuperado"
_KEY_MEDILINK_DOWN_AT = "medilink_down_since_at"   # timestamp ISO de la última caída confirmada
RECOVERY_THROTTLE_MIN = 30          # no notificar recuperación más de 1 vez cada 30 min
MIN_OUTAGE_MIN_FOR_NOTIF = 3        # no notificar recuperación si la caída duró <3 min (oscilación)

RECEPTION_THROTTLE_MIN = 30


# ── Estado Medilink ───────────────────────────────────────────────────────────

def mark_medilink_down(reason: str = ""):
    """Marca Medilink como caído. Idempotente.
    Solo actualiza el timestamp de caída la *primera* vez (para flap protection):
    si ya estaba down, no resetea down_at — así should_notify_recovery() puede
    distinguir entre una caída larga real y una oscilación corta."""
    already_down = system_state_get(_KEY_MEDILINK) == "down"
    system_state_set(_KEY_MEDILINK, "down")
    if not already_down:
        system_state_set(_KEY_MEDILINK_DOWN_AT, datetime.now(timezone.utc).isoformat())
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


def should_notify_recovery() -> bool:
    """True si podemos enviar la notificación de 'Medilink recuperado'.
    Evita spam cuando Medilink oscila (429 intermitente):
      - No más de 1 notificación cada RECOVERY_THROTTLE_MIN minutos.
      - No notificar si la caída confirmada duró menos de MIN_OUTAGE_MIN_FOR_NOTIF.
    """
    # Throttle: ya notificamos recientemente
    last_notif = system_state_updated_at(_KEY_RECOVERY_NOTIFIED)
    if last_notif:
        try:
            ts = datetime.fromisoformat(last_notif)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - ts) < timedelta(minutes=RECOVERY_THROTTLE_MIN):
                return False
        except (ValueError, TypeError):
            pass
    # Flap protection: la caída actual fue muy breve → oscilación, no notificar
    down_at = system_state_get(_KEY_MEDILINK_DOWN_AT)
    if down_at:
        try:
            ts = datetime.fromisoformat(down_at)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - ts) < timedelta(minutes=MIN_OUTAGE_MIN_FOR_NOTIF):
                return False
        except (ValueError, TypeError):
            pass
    return True


def mark_recovery_notified():
    """Registra que se envió la notificación de recuperación."""
    system_state_set(_KEY_RECOVERY_NOTIFIED, datetime.now(timezone.utc).isoformat())


# ── Tasks en background con tracking (evita GC de fire-and-forget) ──────────
import asyncio as _asyncio_bg
_background_tasks: set = set()


def spawn_task(coro):
    """Crea una asyncio.Task y guarda la referencia hasta que termina.

    Python puede garbage-collectar una Task si no hay referencia viva, cancelando
    la coroutine silenciosamente. Este helper resuelve ese problema.
    Uso: spawn_task(send_whatsapp(...)) en lugar de asyncio.create_task(...).
    """
    task = _asyncio_bg.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task
