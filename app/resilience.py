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


# ── Estado de salud de Claude (Anthropic API) ──────────────────────────────
# Mismo tratamiento que Medilink: la IA del bot es una dependencia crítica por
# mensaje (detect_intent). Si se cae (saldo agotado, API down, timeout), sin
# observabilidad el apagón corre en silencio. Caso real: 2026-05-29 a 2026-06-01
# el saldo Anthropic se agotó y el bot degradó a menú genérico 3 días sin alerta.
_KEY_CLAUDE = "claude_status"              # "up" | "down"
_KEY_CLAUDE_REASON = "claude_reason"       # última razón de falla (ej. "saldo agotado")
_KEY_CLAUDE_DOWN_AT = "claude_down_since_at"  # timestamp ISO de la primera falla de la racha


def note_claude_failure(reason: str = ""):
    """Marca Claude como caído. Idempotente: solo fija down_at la primera vez
    de la racha (flap protection idéntico a Medilink)."""
    already_down = system_state_get(_KEY_CLAUDE) == "down"
    system_state_set(_KEY_CLAUDE, "down")
    if not already_down:
        system_state_set(_KEY_CLAUDE_DOWN_AT, datetime.now(timezone.utc).isoformat())
    if reason:
        system_state_set(_KEY_CLAUDE_REASON, reason[:300])


def note_claude_ok():
    """Marca Claude como operativo. Se llama tras cada llamada exitosa."""
    if system_state_get(_KEY_CLAUDE) != "up":
        system_state_set(_KEY_CLAUDE, "up")


def is_claude_down() -> bool:
    """True si la última llamada a Claude falló y no ha habido éxito desde."""
    return system_state_get(_KEY_CLAUDE) == "down"


def claude_down_reason() -> str:
    """Última razón registrada de la caída de Claude (o cadena vacía)."""
    return system_state_get(_KEY_CLAUDE_REASON) or ""


def claude_down_since() -> str | None:
    """Timestamp ISO de cuándo empezó la racha de fallas (si está caído)."""
    if not is_claude_down():
        return None
    return system_state_get(_KEY_CLAUDE_DOWN_AT)


# Alerta de IA caída al dueño. Idempotente por racha: usamos el timestamp de
# inicio de la racha (claude_down_since) como id — alertamos máximo 1 vez por
# racha. Cierra el agujero del apagón silencioso de saldo (caso 2026-06-29:
# ~10h sin cerebro sin aviso). Ver _job_claude_watchdog en jobs.py.
_KEY_CLAUDE_ALERTED = "claude_down_alerted_for"
_KEY_CLAUDE_ALERTED_AT = "claude_down_alerted_at"   # ISO del ÚLTIMO aviso enviado

# Cada cuánto INSISTIR mientras el apagón siga vivo. Una sola alerta por racha
# asume que alguien la leyó, y nada distingue "avisado" de "resuelto":
#   · 21-jul-2026: avisó a los 16 s, nadie recargó → 6 días caído, 137 pacientes
#     perdidos (193 recibieron "problema técnico para entender tu mensaje").
#   · 18-ago-2026: avisó a los 2 min y se calló igual; se detectó por casualidad
#     al revisar el dashboard.
# Un fallo que EXIGE acción humana y no se resuelve tiene que seguir molestando.
_REALERTA_HORAS = 6.0


def horas_caido() -> float:
    """Horas que lleva la racha actual de Claude caído (0.0 si está arriba)."""
    down_at = claude_down_since()
    if not down_at:
        return 0.0
    try:
        dt = datetime.fromisoformat(down_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 3600
    except Exception:
        return 0.0


def should_alert_claude_down(min_minutes: float = 3.0) -> bool:
    """True si hay que avisarle al dueño que la IA está caída.

    Avisa la primera vez de cada racha y después **re-avisa cada
    `_REALERTA_HORAS`** mientras siga caída — no una sola vez, que era el
    agujero por el que se colaron los 6 días de julio.

    Saldo agotado / API key inválida → primer aviso inmediato (requieren acción
    humana). Fallas transitorias (timeout, overload, rate limit) → espera
    `min_minutes` de caída sostenida, para no alertar por un blip que se
    recupera solo.
    """
    if not is_claude_down():
        return False
    down_at = claude_down_since()
    if not down_at:
        return False

    ya_avisada = system_state_get(_KEY_CLAUDE_ALERTED) == down_at
    if ya_avisada:
        # Re-alerta: insistir mientras el apagón siga sin resolverse.
        ultimo = system_state_get(_KEY_CLAUDE_ALERTED_AT)
        if not ultimo:
            return True   # racha marcada por una versión previa, sin timestamp
        try:
            dt_u = datetime.fromisoformat(ultimo)
            if dt_u.tzinfo is None:
                dt_u = dt_u.replace(tzinfo=timezone.utc)
            horas = (datetime.now(timezone.utc) - dt_u).total_seconds() / 3600
            return horas >= _REALERTA_HORAS
        except Exception:
            return True

    reason = (claude_down_reason() or "").lower()
    if ("saldo" in reason) or ("key" in reason) or ("billing" in reason):
        return True  # urgente: acción humana necesaria
    try:
        dt = datetime.fromisoformat(down_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        mins = (datetime.now(timezone.utc) - dt).total_seconds() / 60
        return mins >= min_minutes
    except Exception:
        return True


def es_realerta() -> bool:
    """True si el aviso que viene es una INSISTENCIA, no el primero de la racha.
    El mensaje lo usa para cambiar el tono y decir cuánto lleva caído."""
    return system_state_get(_KEY_CLAUDE_ALERTED) == (claude_down_since() or "\x00")


def mark_claude_down_alerted():
    """Registra la racha alertada Y cuándo fue el último aviso (para la
    re-alerta). Sin el timestamp no hay forma de saber cuándo insistir."""
    system_state_set(_KEY_CLAUDE_ALERTED, claude_down_since() or "")
    system_state_set(_KEY_CLAUDE_ALERTED_AT, datetime.now(timezone.utc).isoformat())


def _classify_claude_error(err: str) -> str:
    """Traduce el error crudo de Anthropic a una causa legible para el admin."""
    e = (err or "").lower()
    if "credit balance is too low" in e or "billing" in e:
        return "saldo agotado (recargar en console.anthropic.com)"
    if "rate" in e and "limit" in e:
        return "rate limit Anthropic"
    if "overloaded" in e or "529" in e:
        return "API Anthropic sobrecargada"
    if "timeout" in e or "timed out" in e:
        return "timeout"
    if "authentication" in e or "401" in e or "api key" in e:
        return "API key inválida"
    return (err or "desconocido")[:120]


# ── Tasks en background con tracking (evita GC de fire-and-forget) ──────────
import asyncio as _asyncio_bg
_background_tasks: set = set()


def spawn_task(coro, name: str | None = None):
    """Crea una asyncio.Task y guarda la referencia hasta que termina.

    Python puede garbage-collectar una Task si no hay referencia viva, cancelando
    la coroutine silenciosamente. Este helper resuelve ese problema.
    Uso: spawn_task(send_whatsapp(...)) en lugar de asyncio.create_task(...).
    El kwarg name se pasa a create_task para facilitar debugging (visible en asyncio logs).
    """
    task = _asyncio_bg.create_task(coro, name=name)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


# ── Lock por phone (serializa procesamiento de mensajes del mismo paciente) ─
# Valor: (Lock, timestamp_ultima_uso). TTL 7 días para evitar memory leak.
_phone_locks: dict = {}
_LOCK_TTL_SEC = 86400 * 7


def get_phone_lock(phone: str) -> "_asyncio_bg.Lock":
    """Retorna (o crea) un asyncio.Lock asociado al teléfono. Garantiza que
    dos mensajes simultáneos del mismo paciente se procesen en serie, evitando
    race conditions en session/state. Lazy GC cuando el dict supera 500 entries."""
    import time as _t_pl
    now = _t_pl.monotonic()
    if len(_phone_locks) > 500:
        for p, val in list(_phone_locks.items()):
            if isinstance(val, tuple) and (now - val[1]) > _LOCK_TTL_SEC:
                _phone_locks.pop(p, None)
    # setdefault es atómico en dict — evita race donde 2 corrutinas crean 2 Locks.
    new_lock = _asyncio_bg.Lock()
    entry = _phone_locks.setdefault(phone, (new_lock, now))
    if isinstance(entry, tuple):
        lock, _ = entry
        _phone_locks[phone] = (lock, now)  # refresh ts
        return lock
    return entry  # backwards compat
