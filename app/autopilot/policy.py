"""Fase 1 — Motor de reglas con límites duros.

Convierte un WorldState en una lista de ProposedAction. NO ejecuta nada;
solo decide qué se *debería* hacer. La ejecución (Fase 2) tomará estas
acciones y las aplicará respetando los mismos límites duros.

Filosofía: reglas explícitas y auditables primero. Cada acción lleva su
`reason` legible. Los casos que las reglas no resuelven con confianza se
marcan `ambiguous=True` y el advisor (Claude) opina sobre ellos.
"""
import logging
from dataclasses import dataclass, field
from enum import Enum

from .world_state import WorldState, CampaignState

log = logging.getLogger("bot")


class ActionType(str, Enum):
    INCREASE = "increase_budget"
    DECREASE = "decrease_budget"
    PAUSE = "pause"
    KEEP = "keep"
    ALERT = "alert"          # requiere ojo humano, sin acción automática clara


@dataclass
class HardLimits:
    """Barandas que NINGUNA decisión puede cruzar, ni siquiera la IA.

    Estos topes son el contrato de seguridad del 'autónomo con límites duros'.
    Se leen de env para poder ajustarlos sin redeploy.
    """
    max_step_pct: float = 0.20          # máximo ±20% de cambio de presupuesto por corrida
    min_daily_budget_clp: int = 2000    # piso de presupuesto por ad set
    max_daily_budget_clp: int = 30000   # techo por ad set
    max_total_daily_clp: int = 80000    # techo de gasto diario sumado de toda la cuenta
    min_spend_to_judge_clp: float = 5000  # no juzgar campañas con muy poco gasto (ruido)
    min_purchases_to_trust: float = 2.0   # nº mínimo de Purchases para confiar en el CAC
    approval_threshold_pct: float = 0.20  # cambios > este % piden aprobación humana

    @classmethod
    def from_env(cls) -> "HardLimits":
        import os
        def _f(k, d): return float(os.getenv(k, d))
        def _i(k, d): return int(float(os.getenv(k, d)))
        return cls(
            max_step_pct=_f("AUTOPILOT_MAX_STEP_PCT", 0.20),
            min_daily_budget_clp=_i("AUTOPILOT_MIN_BUDGET", 2000),
            max_daily_budget_clp=_i("AUTOPILOT_MAX_BUDGET", 30000),
            max_total_daily_clp=_i("AUTOPILOT_MAX_TOTAL", 80000),
            min_spend_to_judge_clp=_f("AUTOPILOT_MIN_SPEND", 5000),
            min_purchases_to_trust=_f("AUTOPILOT_MIN_PURCHASES", 2.0),
            approval_threshold_pct=_f("AUTOPILOT_APPROVAL_PCT", 0.20),
        )


@dataclass
class ProposedAction:
    campaign_id: str
    campaign_name: str
    action: ActionType
    reason: str
    current_budget_clp: int | None = None
    proposed_budget_clp: int | None = None
    confidence: float = 0.0        # 0..1
    needs_approval: bool = False
    ambiguous: bool = False        # las reglas no concluyeron → pasa al advisor
    metrics: dict = field(default_factory=dict)


# ─────────────────────────────────────────────────────────────────────────────
#  POLÍTICA ECONÓMICA — esto lo define Rodrigo (conocimiento de negocio)
#
#  El motor necesita saber: ¿cuándo un CAC es "bueno", "tolerable" o "malo"?
#  Eso depende del margen por paciente atendido, que varía por especialidad y
#  que SOLO tú conoces. La función `evaluate_economics` traduce las métricas de
#  una campaña en un veredicto económico. Es el corazón de toda decisión.
#
#  → Implementa la lógica en evaluate_economics() más abajo (marcada con TODO).
# ─────────────────────────────────────────────────────────────────────────────

class EconVerdict(str, Enum):
    WINNER = "winner"       # rentable → candidata a subir presupuesto
    OK = "ok"              # aceptable → mantener
    MARGINAL = "marginal"   # dudosa → bajar o vigilar
    LOSER = "loser"        # quema plata → bajar fuerte o pausar
    UNKNOWN = "unknown"     # sin datos suficientes para juzgar


# ── Umbrales económicos CALIBRADOS POR ESPECIALIDAD (2026-06-05, Rodrigo) ────
# Calibración acordada con el dueño:
#   • Margen de contribución de un paciente NUEVO atendido: ~$15.000 en Medicina
#     General (ancla; rango declarado $10.000–$20.000 sobre la primera consulta).
#   • Segmentación POR ESPECIALIDAD: un implante/endodoncia tolera un CAC mucho
#     mayor que una consulta general.
#   • ROAS ganadora para escalar: 2.5×.
#
# Derivación del margen por especialidad: se ancla en MG ($15k margen / $25k arancel)
# y se escala SUBLINEALMENTE con el arancel real (config.ARANCELES_CLP):
#     margen(esp) = MG_MARGEN · (arancel(esp) / MG_ARANCEL) ^ 0.7
# El exponente 0.7 (<1) refleja que los tratamientos de ticket alto (dental,
# estética, endo) tienen mayor costo variable (materiales, laboratorio, honorario
# del especialista) → su margen NO crece proporcional al precio. Un solo supuesto
# defendible en vez de 20 números inventados. Ajustá MG_MARGEN o el exponente para
# recalibrar todo el set, o sobreescribí una especialidad puntual en _MARGEN_OVERRIDE.
MG_MARGEN = 15000        # margen de contribución de una consulta de Medicina General
MG_ARANCEL = 25000       # arancel base de referencia (debe matchear config)
_MARGEN_EXP = 0.7        # escalamiento sublineal del margen vs arancel
ROAS_GANADORA = 2.5      # ROAS Meta para considerar escalar (acordado)

# Un mensaje de WhatsApp iniciado vale ≈ margen × tasa de conversión a paciente.
# Supuesto conservador: ~1 de cada 7 conversaciones de alta intención se vuelve
# paciente atendido (MSG_CONV). Recalibrar cuando el funnel real (mensaje→Purchase
# atribuido) tenga volumen — el fix de atribución ctwa_clid de hoy lo habilita.
MSG_CONV = 0.14
MSG_MIN_TO_TRUST = 5     # nº mínimo de mensajes para confiar en el costo/mensaje
CLICK_TOLERABLE = 400    # costo por clic al enlace tolerable
CLICK_MIN_TO_TRUST = 30  # nº mínimo de clics para confiar en el costo/clic

# Margen de fallback cuando no se puede inferir la especialidad de la campaña
# (mitad del rango declarado, ligeramente sobre MG por mezcla con especialistas).
_MARGEN_GLOBAL = 16000
_MARGEN_OVERRIDE: dict[str, int] = {}  # esp_lower -> margen, para forzar casos puntuales


def _aranceles() -> dict[str, int]:
    try:
        from config import ARANCELES_CLP
        return ARANCELES_CLP
    except Exception:
        return {}


def _margen_esp(especialidad: str | None) -> int:
    """Margen de contribución estimado por paciente atendido de esa especialidad."""
    esp = (especialidad or "").lower().strip()
    if esp in _MARGEN_OVERRIDE:
        return _MARGEN_OVERRIDE[esp]
    arancel = _aranceles().get(esp)
    if not arancel:
        return _MARGEN_GLOBAL
    return int(round(MG_MARGEN * (arancel / MG_ARANCEL) ** _MARGEN_EXP / 100) * 100)


def _econ_thresholds(especialidad: str | None) -> dict:
    """Devuelve los umbrales económicos para una especialidad, derivados del margen.

    - cac_bueno    = 0.55 · margen  → claramente rentable (escalar)
    - cac_tolerable=        margen  → breakeven de la PRIMERA visita (LTV/recurrencia
                                      y referidos son colchón adicional, no contado)
    - msg_bueno/tolerable = escala del CAC por la tasa de conversión mensaje→paciente
    """
    m = _margen_esp(especialidad)
    cac_tol = m
    cac_bueno = int(round(0.55 * m / 100) * 100)
    msg_tol = int(round(m * MSG_CONV / 100) * 100)
    msg_bueno = int(round(0.55 * m * MSG_CONV / 100) * 100)
    return {"cac_bueno": cac_bueno, "cac_tolerable": cac_tol,
            "msg_bueno": msg_bueno, "msg_tolerable": msg_tol, "margen": m}


# Inferencia de especialidad desde el nombre de la campaña (Meta no lo da estructurado).
# keyword (en el nombre) -> especialidad canónica (clave de ARANCELES_CLP).
_ESP_KEYWORDS: list[tuple[tuple[str, ...], str]] = [
    (("endodonc", "tratamiento de conducto"), "endodoncia"),
    (("implant",), "implantología"),
    (("ortodonc", "orto ", "frenillo", "bracket"), "ortodoncia"),
    (("estetic", "estética", "facial", "botox", "armoniza", "hilos"), "estética facial"),
    (("odonto", "dental", "diente", "muela", "tapadura", "limpieza dental", "blanqueam"), "odontología general"),
    (("eco", "ecotomograf", "ecograf", "ecotomo"), "ecografía"),
    (("cardio", "corazón", "corazon", "presión", "presion"), "cardiología"),
    (("ginec", "matrona", "pap", "preventivo de la mujer"), "ginecología"),
    (("gastro", "colon", "endoscop"), "gastroenterología"),
    (("trauma", "rodilla", "hombro", "fractura", "ortopedia"), "traumatología y ortopedia"),
    (("otorrino", "oído", "oido", "garganta", "nariz"), "otorrinolaringología"),
    (("kine", "kinesio", "rehabilita"), "kinesiología"),
    (("nutric", "nutri", "obesidad", "bajar de peso"), "nutrición"),
    (("psico", "salud mental", "ansiedad", "depres"), "psicología"),
    (("fono", "lenguaje", "voz", "deglu"), "fonoaudiología"),
    (("podolog", "uña", "pie diabét"), "podología"),
    (("masoterap", "masaje"), "masoterapia"),
    (("familiar",), "medicina familiar"),
    (("medicina general", "consulta general", "médico general", "medico general"), "medicina general"),
]


def _infer_especialidad(campaign_name: str | None) -> str | None:
    """Mejor esfuerzo: deduce la especialidad del nombre de la campaña. None si no hay señal."""
    n = (campaign_name or "").lower()
    for keys, esp in _ESP_KEYWORDS:
        if any(k in n for k in keys):
            return esp
    return None


def evaluate_economics(c: CampaignState, limits: HardLimits) -> tuple[EconVerdict, float, str]:
    """Traduce las métricas de una campaña en un veredicto económico.

    Retorna (veredicto, confianza 0..1, explicación legible).

    TODO(Rodrigo): define los umbrales según TU economía. Preguntas a responder:
      - ¿Cuál es el CAC máximo tolerable por paciente atendido? (¿$15.000? ¿$25.000?)
        Pista: debe ser < margen de contribución promedio de una primera consulta.
      - ¿A partir de qué ROAS consideras una campaña "ganadora" para escalar? (¿2x? ¿3x?)
      - ¿Querés umbrales distintos por especialidad/objetivo? (ej: dental tolera CAC más alto)

    Abajo hay una implementación de referencia con valores conservadores. Ajústalos.
    """
    # Guard: muy poco gasto → no juzgar (evita decidir sobre ruido).
    if c.spend < limits.min_spend_to_judge_clp:
        return EconVerdict.UNKNOWN, 0.0, f"gasto ${c.spend:.0f} < mínimo para juzgar"

    # Umbrales POR ESPECIALIDAD: se infiere del nombre de la campaña; si no hay
    # señal, se usa el margen global. Así una campaña de endodoncia tolera un CAC
    # mucho mayor que una de medicina general.
    _esp = _infer_especialidad(c.name)
    _t = _econ_thresholds(_esp)
    CAC_BUENO, CAC_TOLERABLE = _t["cac_bueno"], _t["cac_tolerable"]
    MSG_BUENO, MSG_TOLERABLE = _t["msg_bueno"], _t["msg_tolerable"]
    _esp_tag = f" [{_esp}]" if _esp else " [margen global]"

    # Jerarquía de señal: usamos el evento medible más cercano a la conversión.
    #   1. Purchase (paciente atendido, vía CAPI)  → la señal de oro: ROAS/CAC real.
    #   2. Mensaje WhatsApp iniciado               → proxy fuerte (intención de agendar).
    #   3. Clic al enlace                          → proxy débil (solo interés).
    # Cuando vincules el CAPI Purchase a las campañas, el motor sube solo al nivel 1.

    # ── Nivel 1: Purchase (paciente atendido) ──
    if c.purchases >= limits.min_purchases_to_trust and c.cac_purchase is not None:
        cac, roas = c.cac_purchase, c.roas_meta
        if cac <= CAC_BUENO and roas >= ROAS_GANADORA:
            return EconVerdict.WINNER, 0.85, f"CAC/paciente ${cac:,.0f} y ROAS {roas:.1f}×{_esp_tag} (bueno ≤${CAC_BUENO:,})"
        if cac <= CAC_TOLERABLE:
            return EconVerdict.OK, 0.75, f"CAC/paciente ${cac:,.0f} tolerable{_esp_tag} (tope ${CAC_TOLERABLE:,})"
        if cac <= CAC_TOLERABLE * 1.5:
            return EconVerdict.MARGINAL, 0.65, f"CAC/paciente ${cac:,.0f} sobre lo tolerable{_esp_tag} (tope ${CAC_TOLERABLE:,})"
        return EconVerdict.LOSER, 0.75, f"CAC/paciente ${cac:,.0f} muy alto{_esp_tag} (tope ${CAC_TOLERABLE:,})"

    # ── Nivel 2: Mensajes de WhatsApp iniciados ──
    if c.messages >= MSG_MIN_TO_TRUST and c.cost_per_message is not None:
        cpm = c.cost_per_message
        # Confianza algo menor: un mensaje no es un paciente atendido todavía.
        if cpm <= MSG_BUENO:
            return EconVerdict.WINNER, 0.7, f"{c.messages:.0f} mensajes a ${cpm:,.0f} c/u (barato){_esp_tag}"
        if cpm <= MSG_TOLERABLE:
            return EconVerdict.OK, 0.65, f"costo/mensaje ${cpm:,.0f} tolerable{_esp_tag} (tope ${MSG_TOLERABLE:,})"
        if cpm <= MSG_TOLERABLE * 1.6:
            return EconVerdict.MARGINAL, 0.6, f"costo/mensaje ${cpm:,.0f} alto{_esp_tag} (tope ${MSG_TOLERABLE:,})"
        return EconVerdict.LOSER, 0.65, f"costo/mensaje ${cpm:,.0f} muy alto{_esp_tag} (tope ${MSG_TOLERABLE:,})"

    # ── Nivel 3: Clics al enlace (señal débil — no escalamos solo por clics) ──
    if c.link_clicks >= CLICK_MIN_TO_TRUST and c.cost_per_link_click is not None:
        cpc = c.cost_per_link_click
        if cpc <= CLICK_TOLERABLE:
            return EconVerdict.OK, 0.5, f"costo/clic ${cpc:,.0f} (solo clics, sin conversión medida)"
        return EconVerdict.MARGINAL, 0.5, f"costo/clic ${cpc:,.0f} alto y sin conversión medida"

    # Sin ninguna señal medible: gasta pero no sabemos si convierte.
    return EconVerdict.UNKNOWN, 0.3, (
        f"${c.spend:,.0f} gastados sin conversión ni mensaje atribuido — "
        "falta vincular evento de conversión")


# ─────────────────────────────────────────────────────────────────────────────

def _step_budget(current: int | None, factor: float, limits: HardLimits) -> int | None:
    """Aplica un factor de cambio respetando piso/techo y el paso máximo."""
    if current is None:
        return None
    factor = max(1 - limits.max_step_pct, min(1 + limits.max_step_pct, factor))
    proposed = int(round(current * factor))
    proposed = max(limits.min_daily_budget_clp, min(limits.max_daily_budget_clp, proposed))
    return proposed


def _campaign_budget(c: CampaignState) -> int | None:
    """Presupuesto diario efectivo: CBO en campaña, o suma de ad sets."""
    if c.daily_budget_clp:
        return c.daily_budget_clp
    total = 0
    for a in c.adsets:
        if a.get("daily_budget"):
            total += int(a["daily_budget"])
    return total or None


def decide(ws: WorldState, limits: HardLimits | None = None) -> list[ProposedAction]:
    """Genera acciones propuestas para todas las campañas del snapshot."""
    limits = limits or HardLimits.from_env()
    actions: list[ProposedAction] = []

    # Señal de desconfianza global: si Meta atribuye mucho más ingreso que la caja real,
    # bajamos la confianza de todas las decisiones basadas en ROAS Meta.
    trust = 1.0
    if ws.attribution_ratio is not None and ws.attribution_ratio < 0.5:
        trust = 0.6
        log.warning("autopilot: ratio atribución %.2f < 0.5 → Meta posiblemente inflado, "
                    "bajo confianza global", ws.attribution_ratio)

    for c in ws.campaigns:
        verdict, conf, expl = evaluate_economics(c, limits)
        conf *= trust
        budget = _campaign_budget(c)

        # Gasto consciente de CAPACIDAD: si la agenda de esta especialidad está
        # saturada (próxima hora libre lejana), NO escalar aunque sea ganadora —
        # generaría demanda que no se puede atender. Es además señal de contratar.
        _cap = (ws.capacity or {}).get(_infer_especialidad(c.name) or "", {})
        _saturada = bool(_cap.get("saturated"))

        if verdict == EconVerdict.WINNER and _saturada:
            _dias = _cap.get("days_to_next")
            act = ProposedAction(
                campaign_id=c.id, campaign_name=c.name, action=ActionType.KEEP,
                reason=(f"Ganadora PERO agenda saturada (próx. hora en {_dias} días): "
                        f"no escalar, la demanda extra no se puede atender. "
                        f"Señal de contratación / redirigir presupuesto a especialidad con cupos."),
                current_budget_clp=budget, proposed_budget_clp=budget, confidence=conf,
            )
        elif verdict == EconVerdict.WINNER:
            proposed = _step_budget(budget, 1 + limits.max_step_pct, limits)
            act = ProposedAction(
                campaign_id=c.id, campaign_name=c.name, action=ActionType.INCREASE,
                reason=f"Ganadora: {expl}. Subir presupuesto.",
                current_budget_clp=budget, proposed_budget_clp=proposed,
                confidence=conf,
            )
        elif verdict == EconVerdict.OK:
            act = ProposedAction(
                campaign_id=c.id, campaign_name=c.name, action=ActionType.KEEP,
                reason=f"Estable: {expl}. Mantener.",
                current_budget_clp=budget, proposed_budget_clp=budget, confidence=conf,
            )
        elif verdict == EconVerdict.MARGINAL:
            proposed = _step_budget(budget, 1 - limits.max_step_pct, limits)
            act = ProposedAction(
                campaign_id=c.id, campaign_name=c.name, action=ActionType.DECREASE,
                reason=f"Marginal: {expl}. Bajar y vigilar.",
                current_budget_clp=budget, proposed_budget_clp=proposed, confidence=conf,
            )
        elif verdict == EconVerdict.LOSER:
            # Perdedora clara que YA gastó fuerte (≥4× el piso para juzgar) → proponer
            # PAUSAR (cortar la hemorragia), no solo recortar 20%. Las perdedoras leves
            # (gastaron poco) se recortan. Solo campañas con presupuesto propio (donde
            # el pausado es fiable; las publicaciones impulsadas se dejan al advisor).
            burner = bool(budget) and c.spend >= 4 * limits.min_spend_to_judge_clp
            if burner:
                act = ProposedAction(
                    campaign_id=c.id, campaign_name=c.name, action=ActionType.PAUSE,
                    reason=(f"Quema plata: {expl}. ${c.spend:,.0f} gastados con ~0 "
                            f"conversaciones → pausar."),
                    current_budget_clp=budget, proposed_budget_clp=0, confidence=conf,
                )
            else:
                proposed = _step_budget(budget, 1 - limits.max_step_pct, limits)
                act = ProposedAction(
                    campaign_id=c.id, campaign_name=c.name, action=ActionType.DECREASE,
                    reason=f"Perdedora: {expl}. Recortar al máximo permitido por paso.",
                    current_budget_clp=budget, proposed_budget_clp=proposed, confidence=conf,
                )
        else:  # UNKNOWN → ambiguo, lo verá el advisor
            act = ProposedAction(
                campaign_id=c.id, campaign_name=c.name, action=ActionType.ALERT,
                reason=f"Sin juicio por reglas: {expl}.",
                current_budget_clp=budget, proposed_budget_clp=budget,
                confidence=conf, ambiguous=True,
            )

        # ¿Necesita aprobación humana? Cambios grandes o baja confianza.
        if act.action in (ActionType.INCREASE, ActionType.DECREASE) and budget:
            change = abs((act.proposed_budget_clp or budget) - budget) / budget
            if change >= limits.approval_threshold_pct or act.confidence < 0.6:
                act.needs_approval = True
        # Pausar una campaña es irreversible-en-la-práctica → SIEMPRE pide OK humano
        # (nunca se auto-aplica en Fase 3; _is_auto excluye 'pause').
        if act.action == ActionType.PAUSE:
            act.needs_approval = True
        if act.confidence < 0.6:
            act.ambiguous = True

        act.metrics = {
            "spend": c.spend, "purchases": c.purchases, "cac_purchase": c.cac_purchase,
            "roas_meta": c.roas_meta, "ctr": c.ctr, "verdict": verdict.value,
            "messages": c.messages, "cost_per_message": c.cost_per_message,
            "link_clicks": c.link_clicks, "cost_per_link_click": c.cost_per_link_click,
        }
        actions.append(act)

    # Baranda global: si la suma de presupuestos propuestos excede el techo total,
    # no escalamos (recortamos los INCREASE a KEEP).
    proposed_total = sum((a.proposed_budget_clp or 0) for a in actions)
    if proposed_total > limits.max_total_daily_clp:
        log.warning("autopilot: total propuesto $%.0f > techo $%.0f → cancelo escaladas",
                    proposed_total, limits.max_total_daily_clp)
        for a in actions:
            if a.action == ActionType.INCREASE:
                a.action = ActionType.KEEP
                a.proposed_budget_clp = a.current_budget_clp
                a.reason += " [bloqueado: techo de gasto total alcanzado]"

    return actions
