"""Guardrails — la capa que hace sobrevivible el "máximo riesgo".

Con una flota de agentes que pueden contactar pacientes, mover plata y escribir
en Medilink, el riesgo NO es ningún agente individual: es el AGREGADO (que entre
todos spameen al mismo paciente, o que ejecuten algo ilegal). Toda acción de todo
agente pasa por `authorize()` antes de tener efecto. Gating en cascada:

  ALMA_AGENTS_ENABLED   (maestro)  → si false, nada corre
  flag por agente                  → si false, ese agente no corre
  ALMA_AGENTS_EXECUTE   (global)   → si false, TODO es dry-run (propone, no actúa)
  ── y para CADA acción ──
  horas de silencio                → no se contacta pacientes fuera de 09–21 CLT
  consent (Ley 21.719)             → contacto masivo exige opt-in vigente
  presupuesto de contacto/paciente → máx N mensajes proactivos por paciente / 7d
                                      (sumado entre TODOS los agentes — anti-spam)
  escrituras Medilink              → off salvo flag explícito
  riesgo 'extremo'                 → off salvo flag explícito aparte

Defaults: maestro off, execute off, consent requerido, medilink off, extremo off.
Con eso, NADA se ejecuta. Encender es una decisión deliberada y escalonada.

NO-DELEGABLE: las compuertas legales (consent, templates, horas) no las decide
ningún modelo ni se pueden saltar subiendo el flag de execute. Son piso duro.
"""
import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

log = logging.getLogger("bot")
_CLT = ZoneInfo("America/Santiago")


# ── Flags (todos OFF por defecto) ────────────────────────────────────────────

def _flag(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).lower() in ("true", "1", "yes")


def master_enabled() -> bool:
    """Kill-switch maestro de TODA la flota."""
    return _flag("ALMA_AGENTS_ENABLED")


def execute_enabled() -> bool:
    """Si false, todos los agentes corren en dry-run (proponen, no actúan)."""
    return _flag("ALMA_AGENTS_EXECUTE")


def agent_enabled(flag_name: str) -> bool:
    return _flag(flag_name)


def extreme_allowed() -> bool:
    return _flag("ALMA_AGENTS_ALLOW_EXTREME")


def medilink_writes_allowed() -> bool:
    # Reusa el mismo flag del cerebro/operativa para que encender escrituras sea
    # una sola decisión, no varias dispersas.
    return _flag("ALMA_BRAIN_ALLOW_MEDILINK_WRITES")


def require_consent() -> bool:
    return _flag("ALMA_AGENTS_REQUIRE_CONSENT", "true")


# ── Horas de silencio (no molestar pacientes de madrugada) ───────────────────

def in_quiet_hours(now: datetime | None = None) -> bool:
    now = now or datetime.now(_CLT)
    start = int(os.getenv("ALMA_AGENTS_QUIET_START", "21"))  # 21:00
    end = int(os.getenv("ALMA_AGENTS_QUIET_END", "9"))       # 09:00
    h = now.hour
    # Ventana que cruza medianoche: [21:00, 09:00).
    return h >= start or h < end


# ── Presupuesto de contacto por paciente (anti-spam agregado) ────────────────

def _outbound_last_days(phone: str, days: int) -> int | None:
    """Cuántos mensajes salientes recibió este teléfono en los últimos `days`.
    None si no se puede determinar (degrada conservador)."""
    try:
        from session import _conn
        conn = _conn()
    except Exception as e:  # noqa: BLE001
        log.warning("alma_agents guardrails: sin sessions.db (%s)", e)
        return None
    try:
        cur = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE phone=? AND direction='out' "
            "AND ts >= datetime('now', ?)",
            (phone, f"-{days} days"),
        )
        return int(cur.fetchone()[0])
    except Exception as e:  # noqa: BLE001
        log.warning("alma_agents guardrails: query budget falló (%s)", e)
        return None
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def contact_budget_remaining(phone: str) -> int | None:
    """Cupos de contacto proactivo que quedan para este paciente en la ventana.
    None si no se pudo medir (el caller lo trata como 0 cuando execute está on)."""
    max_per_week = int(os.getenv("ALMA_AGENTS_CONTACT_MAX_WEEK", "2"))
    used = _outbound_last_days(phone, 7)
    if used is None:
        return None
    return max(0, max_per_week - used)


def _has_consent(phone: str) -> bool:
    try:
        from session import has_privacy_consent
        return bool(has_privacy_consent(phone))
    except Exception as e:  # noqa: BLE001
        log.warning("alma_agents guardrails: consent check falló (%s)", e)
        return False


# ── Autorización central de una acción ───────────────────────────────────────

def authorize(action) -> tuple[bool, str]:
    """¿Esta acción puede EJECUTARSE ahora? Devuelve (allow, motivo).

    `action` es un AgentAction (ver base.py). No juzga si la acción es buena idea
    (eso lo razonó el agente); juzga si es segura/legal/dentro de presupuesto."""
    if not execute_enabled():
        return False, "execute global off → dry-run (propuesta registrada, sin efecto)"

    risk = getattr(action, "risk", "medio")
    if risk == "extremo" and not extreme_allowed():
        return False, "acción de riesgo EXTREMO bloqueada (ALMA_AGENTS_ALLOW_EXTREME=false)"

    if getattr(action, "requires_medilink_write", False) and not medilink_writes_allowed():
        return False, "escritura a Medilink bloqueada (ALMA_BRAIN_ALLOW_MEDILINK_WRITES=false)"

    if getattr(action, "requires_contact", False):
        # El contacto a STAFF (Rodrigo / recepción) no pasa por consent ni budget,
        # pero sí respeta el execute global. El contacto a PACIENTES, todo el piso.
        if not getattr(action, "is_staff", False):
            if in_quiet_hours():
                return False, "fuera de horario de contacto (horas de silencio 21–09 CLT)"
            target = getattr(action, "target", None)
            if not target:
                return False, "acción de contacto sin destinatario"
            if require_consent() and not _has_consent(target):
                return False, "paciente sin consentimiento vigente (Ley 21.719)"
            rem = contact_budget_remaining(target)
            if rem is None:
                return False, "no se pudo verificar presupuesto de contacto → se niega por precaución"
            if rem <= 0:
                return False, "presupuesto de contacto agotado para este paciente (anti-spam)"

    return True, "OK"


def fleet_status() -> dict:
    """Foto de los kill-switches para el panel de control."""
    return {
        "master_enabled": master_enabled(),
        "execute_enabled": execute_enabled(),
        "extreme_allowed": extreme_allowed(),
        "medilink_writes_allowed": medilink_writes_allowed(),
        "require_consent": require_consent(),
        "in_quiet_hours": in_quiet_hours(),
        "contact_max_week": int(os.getenv("ALMA_AGENTS_CONTACT_MAX_WEEK", "2")),
    }
