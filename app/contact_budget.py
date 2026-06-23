"""contact_budget.py — Presupuesto de contacto proactivo UNIFICADO (anti-spam agregado).

UNA sola fuente de verdad para "cuántos mensajes proactivos puede recibir un
paciente en una ventana", consultable por TODOS los rieles (promo_postconsent,
eco_prep, dental_winback, winback, dental_consent, cross_sell, marketing_consent…),
no solo la flota `alma_agents`. Cierra el agujero estructural que produjo el spam
`consent_dental_v2` (38 pacientes, 217 msgs): cada riel tenía su dedup propio y
NADA veía el agregado, así que sumados podían saturar al mismo paciente.

Decisiones de diseño:
  • Cuenta CONTACTOS PROACTIVOS (eventos `proactive_contact` que cada riel emite
    vía `record_contact`), NO todo el outbound. Así un paciente que está chateando
    con el bot no agota su cupo por respuestas conversacionales legítimas.
  • Empareja teléfonos por sus ÚLTIMOS 9 DÍGITOS — la causa raíz del spam dental
    fue comparar `+56…/9…` crudo contra el canónico `56…`. Acá hay UN solo
    normalizador y la query SQL también compara por sufijo de 9.
  • Gating `CONTACT_BUDGET_ENFORCE` (default OFF). OFF = observa y loguea
    `contact_budget_would_block` SIN bloquear (cero cambio de conducta, seguro de
    desplegar). ON = bloquea de verdad y loguea `contact_budget_block`. Encender es
    una sola decisión, vía .env o Sala de Máquinas.
  • `record_contact` también es la observabilidad que faltaba: dental_winback y
    winback no emitían NINGÚN evento auditable por envío.

NO depende de `alma_agents` (módulo neutro). `alma_agents.guardrails` delega aquí
para que la flota y los rieles compartan exactamente el mismo presupuesto.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

log = logging.getLogger("contact_budget")

_CLT = ZoneInfo("America/Santiago")

# Evento marcador único que cuenta para el presupuesto.
PROACTIVE_EVENT = "proactive_contact"


# ── Configuración (leída en vivo, no congelada al import) ─────────────────────

def _max_per_window() -> int:
    return int(os.getenv("CONTACT_BUDGET_MAX_WEEK", "2"))


def _window_days() -> int:
    return int(os.getenv("CONTACT_BUDGET_WINDOW_DAYS", "7"))


def _enforce() -> bool:
    """¿Bloquea de verdad? Default OFF (solo observa). Respeta el switchboard."""
    try:
        from autopilot import flags  # type: ignore
        return bool(flags.flag_on("CONTACT_BUDGET_ENFORCE"))
    except Exception:  # noqa: BLE001 — fallback a env directo si no está flags
        return os.getenv("CONTACT_BUDGET_ENFORCE", "false").strip().lower() in ("1", "true", "yes", "on")


# ── Normalización de teléfono (últimos 9 dígitos = identidad canónica) ────────

def last9(phone: str | None) -> str:
    """Últimos 9 dígitos del teléfono. Identidad robusta entre formatos
    (`+56 9 1234 5678`, `569…`, `9…`). Un solo lugar que define 'mismo paciente'."""
    if not phone:
        return ""
    digits = "".join(ch for ch in str(phone) if ch.isdigit())
    return digits[-9:] if len(digits) >= 9 else digits


# ── Horas de silencio (no molestar de madrugada) ─────────────────────────────

def in_quiet_hours(now: datetime | None = None) -> bool:
    """¿Estamos en la ventana de silencio [21:00, 09:00) CLT?"""
    now = now or datetime.now(_CLT)
    start = int(os.getenv("CONTACT_QUIET_START", "21"))
    end = int(os.getenv("CONTACT_QUIET_END", "9"))
    if start == end:  # ventana inválida → conservador: nunca silencio total accidental
        return False
    h = now.hour
    if start > end:          # cruza medianoche, p.ej. [21, 9)
        return h >= start or h < end
    return start <= h < end  # ventana normal en el día


# ── Conteo de contactos proactivos en la ventana ─────────────────────────────

def proactive_count(phone: str, days: int | None = None) -> int | None:
    """Cuántos contactos proactivos recibió este paciente (por últimos 9 díg) en
    los últimos `days`. None si no se pudo medir (caller degrada conservador)."""
    suf = last9(phone)
    if not suf:
        return None
    days = days if days is not None else _window_days()
    try:
        from session import db
        with db() as conn:
            cur = conn.execute(
                "SELECT COUNT(*) FROM conversation_events "
                "WHERE event = ? "
                "AND substr(replace(replace(replace(phone,'+',''),' ',''),'-',''), -9) = ? "
                "AND ts >= datetime('now', ?)",
                (PROACTIVE_EVENT, suf, f"-{int(days)} days"),
            )
            row = cur.fetchone()
            return int(row[0]) if row else 0
    except Exception as e:  # noqa: BLE001
        log.warning("contact_budget: no se pudo contar proactivos (%s)", e)
        return None


def remaining(phone: str) -> int | None:
    """Cupos proactivos restantes para este paciente. None si no se pudo medir."""
    used = proactive_count(phone)
    if used is None:
        return None
    return max(0, _max_per_window() - used)


# ── Gate central que cada riel consulta ANTES de enviar ──────────────────────

def can_contact(
    phone: str,
    *,
    rail: str,
    respect_quiet: bool = True,
) -> tuple[bool, str]:
    """¿Puede este riel contactar proactivamente a este paciente AHORA?

    Devuelve (allow, motivo). Con CONTACT_BUDGET_ENFORCE OFF (default) SIEMPRE
    permite, pero loguea `contact_budget_would_block` cuando habría bloqueado —
    así medimos colisiones reales entre rieles sin cambiar conducta. Con ENFORCE
    ON, niega de verdad y loguea `contact_budget_block`.
    """
    reason_block = None

    if respect_quiet and in_quiet_hours():
        reason_block = "horas de silencio (21–09 CLT)"
    else:
        rem = remaining(phone)
        if rem is None:
            # No se pudo medir: en modo ENFORCE se niega por precaución; en
            # observe-mode no estorba.
            reason_block = "no se pudo medir presupuesto"
        elif rem <= 0:
            reason_block = f"presupuesto agotado ({_max_per_window()}/{_window_days()}d)"

    if reason_block is None:
        return True, "ok"

    enforce = _enforce()
    event = "contact_budget_block" if enforce else "contact_budget_would_block"
    try:
        from session import log_event
        log_event(phone, event, {"rail": rail, "motivo": reason_block})
    except Exception:  # noqa: BLE001 — nunca romper el riel por loguear
        pass

    if enforce:
        return False, reason_block
    return True, f"observe-mode (habría bloqueado: {reason_block})"


def record_contact(phone: str, rail: str, meta: dict | None = None) -> None:
    """Registra que un riel contactó proactivamente a este paciente. Llamar
    DESPUÉS de un envío exitoso. Alimenta el presupuesto y es la traza auditable
    de 'a quién contactamos y cuándo' que varios rieles no emitían."""
    payload = {"rail": rail}
    if meta:
        payload.update(meta)
    try:
        from session import log_event
        log_event(phone, PROACTIVE_EVENT, payload)
    except Exception as e:  # noqa: BLE001
        log.warning("contact_budget: no se pudo registrar contacto de %s (%s)", rail, e)


def status() -> dict:
    """Foto de configuración para paneles."""
    return {
        "enforce": _enforce(),
        "max_per_window": _max_per_window(),
        "window_days": _window_days(),
        "quiet_start": int(os.getenv("CONTACT_QUIET_START", "21")),
        "quiet_end": int(os.getenv("CONTACT_QUIET_END", "9")),
    }
