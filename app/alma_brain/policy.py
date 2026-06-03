"""Capa GOBERNAR — límites duros del cerebro de Alma.

Principio (heredado de autopilot): **la IA propone, las reglas mandan**.
Ninguna acción se ejecuta sin pasar por `check()`. Los límites se leen del
entorno para poder endurecerlos sin tocar código.

Compuerta legal NO delegable (Ley 21.719 + WhatsApp Business Policy): cualquier
acción que contacte pacientes fuera de la ventana de 24h exige consentimiento y
templates aprobados. Esto NO lo decide el modelo: es un límite duro acá.
"""
import os
from dataclasses import dataclass


def _flag(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).lower() in ("true", "1", "yes")


@dataclass
class HardLimits:
    # Kill-switch global: sin esto, NADA se ejecuta (solo dry-run/propuestas).
    execute_enabled: bool
    # Marketing/contacto a pacientes.
    winback_max_per_day: int
    require_consent: bool          # Ley 21.719 — contacto masivo exige opt-in
    require_approved_templates: bool
    # Ads (reusa la filosofía de autopilot HardLimits).
    ads_max_step_pct: float
    # Acciones que tocan Medilink (agenda/citas) — por defecto NO automatizables.
    allow_medilink_writes: bool

    @classmethod
    def from_env(cls) -> "HardLimits":
        return cls(
            execute_enabled=_flag("ALMA_BRAIN_EXECUTE"),
            winback_max_per_day=int(os.getenv("ALMA_BRAIN_WINBACK_MAX_DAY", "50")),
            require_consent=_flag("ALMA_BRAIN_REQUIRE_CONSENT", "true"),
            require_approved_templates=_flag("ALMA_BRAIN_REQUIRE_TEMPLATES", "true"),
            ads_max_step_pct=float(os.getenv("ALMA_BRAIN_ADS_MAX_STEP", "0.20")),
            allow_medilink_writes=_flag("ALMA_BRAIN_ALLOW_MEDILINK_WRITES"),
        )


# Acciones que tocan/contactan pacientes de forma masiva — sujetas a consent.
_KINDS_CONTACTO = {"winback_blast", "email_blast", "crosssell_blast", "reactivacion_blast"}
# Acciones que escriben en Medilink (agenda).
_KINDS_MEDILINK = {"abrir_agenda", "agendar_cita", "mover_cita"}


def check(kind: str, params: dict, limits: HardLimits | None = None) -> tuple[bool, str]:
    """¿Se puede EJECUTAR esta acción ahora? Devuelve (ok, motivo).

    No evalúa si la acción es buena idea (eso lo razonó el copiloto); evalúa si
    es LEGAL y está dentro de los topes operacionales."""
    limits = limits or HardLimits.from_env()

    if not limits.execute_enabled:
        return False, ("Ejecución global desactivada (ALMA_BRAIN_EXECUTE=false). "
                       "La propuesta queda registrada para acción manual.")

    if kind in _KINDS_CONTACTO:
        if limits.require_consent:
            return False, ("Contacto masivo a pacientes exige consentimiento (Ley 21.719). "
                           "Ejecutar vía el flujo de consent + Custom Audiences, no directo.")
        n = int(params.get("n_destinatarios", 0) or 0)
        if n > limits.winback_max_per_day:
            return False, (f"{n} destinatarios supera el tope diario "
                           f"({limits.winback_max_per_day}).")

    if kind in _KINDS_MEDILINK and not limits.allow_medilink_writes:
        return False, ("Escrituras a Medilink desde el cerebro están deshabilitadas "
                       "(ALMA_BRAIN_ALLOW_MEDILINK_WRITES=false).")

    if kind == "ads_budget_change":
        step = abs(float(params.get("step_pct", 0) or 0))
        if step > limits.ads_max_step_pct:
            return False, (f"Paso de presupuesto {step:.0%} supera el tope "
                           f"({limits.ads_max_step_pct:.0%}).")

    return True, "OK"


# ── Fase 4 (Alma operativa): confirmación de cupo liberado ───────────────────
#
# Cuando un paciente de la lista de espera ACEPTA un cupo liberado, queda en
# 'hold blando' (apartado, sin tocar Medilink). La pregunta es: ¿lo confirma el
# sistema solo, o lo manda a recepción para validación humana?
#
# La compuerta de capacidad es angosta a propósito: la auto-confirmación NO usa
# el flag general ALMA_BRAIN_ALLOW_MEDILINK_WRITES (que abriría TODAS las
# escrituras del cerebro). Usa su propio kill-switch dedicado, así habilitar
# esta automatización acotada no abre nada más.

@dataclass
class OfferContext:
    """Señales de riesgo de una oferta aceptada — todas pre-computadas por el
    orquestador para que la regla de negocio sea PURA (sin I/O, testeable)."""
    paciente_conocido: bool   # RUT presente y encontrado como paciente en Medilink
    rut_valido: bool          # el RUT tiene formato válido
    esp_coincide: bool        # la especialidad del cupo == la que el paciente pidió en su inscripción
    horas_hasta: float        # horas entre ahora y el inicio del cupo (margen de aviso)
    desde_waitlist: bool      # vino de una inscripción formal en la lista de espera (no ad-hoc)
    especialidad: str = ""    # especialidad del cupo (modula el margen exigido)


def autoconfirm_enabled() -> bool:
    """Kill-switch dedicado y angosto. Sin esto, TODO cupo aceptado va a recepción."""
    return _flag("ALMA_OPERATIVA_AUTOCONFIRM")


# Especialistas escasos: un no-show bloquea un cupo difícil de reponer, así que
# exigimos MÁS margen antes de auto-confirmar (más tiempo para que recepción
# reaccione). El resto usa el margen base. Editable: agrega/saca raíces.
_ESP_ESCASAS = ("cardio", "gastro", "otorrino", "gineco")
_MARGEN_BASE_H = 24.0
_MARGEN_ESCASA_H = 48.0


def _margen_horas(especialidad: str) -> float:
    esp = (especialidad or "").strip().lower()
    return _MARGEN_ESCASA_H if any(k in esp for k in _ESP_ESCASAS) else _MARGEN_BASE_H


def should_auto_confirm(ctx: OfferContext) -> tuple[bool, str]:
    """REGLA DE BAJO RIESGO — decisión de negocio de Rodrigo.

    Devuelve (auto, motivo):
      auto=True  → el sistema confirma solo: crea la cita en Medilink sin humano.
      auto=False → cae a recepción para que un humano valide antes de agendar.

    Esta es la pieza que más moldea la feature: define cuánto confías en la
    automatización vs cuánto exiges ojo humano. Ajústala con tu criterio:
      - _ESP_ESCASAS / _MARGEN_*: qué especialidades exigen más margen y cuánto.
      - ¿Exigir historial de asistencia (no-show bajo) como señal extra?
      - ¿Auto-confirmar solo en horario de recepción para que alguien mire?
    """
    if not autoconfirm_enabled():
        return False, "auto-confirmación desactivada (ALMA_OPERATIVA_AUTOCONFIRM=false) → recepción"

    # ── INICIO regla editable por Rodrigo ────────────────────────────────────
    # Señales mínimas: paciente real y verificable, especialidad que pidió, e
    # inscripción formal en la lista de espera (no una oferta ad-hoc).
    if not (ctx.paciente_conocido and ctx.rut_valido and ctx.esp_coincide and ctx.desde_waitlist):
        return False, "señal insuficiente (paciente / RUT / especialidad / inscripción) → recepción"

    margen = _margen_horas(ctx.especialidad)
    if ctx.horas_hasta > margen:
        return True, (f"bajo riesgo: paciente conocido, especialidad coincide, "
                      f"cupo a >{margen:.0f}h")
    return False, (f"cupo a <{margen:.0f}h en {ctx.especialidad or 'general'} "
                   f"→ validación de recepción")
    # ── FIN regla editable ───────────────────────────────────────────────────
