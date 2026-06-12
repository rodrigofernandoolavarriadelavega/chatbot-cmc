"""Skills aprendidas (nivel 6) — destila REGLAS REUTILIZABLES desde la experiencia.

Inspirado en el patrón "self-learning skills" de agentes tipo Hermes, pero ATADO a
la economía del CMC y con PUERTA DE PROMOCIÓN explícita: el Optimizador
(`optimizer.py`) detecta desalineaciones PUNTUALES y efímeras (cada corrida las
re-descubre desde cero); este módulo las convierte en SKILLS DURABLES cuando el
patrón se REPITE y es ESTABLE, las re-verifica en cada corrida y las decae/retira si
dejan de cumplirse. Cierra el lazo experiencia → regla → aplicación → re-verificación
para que el sistema no tenga que re-aprender lo mismo cada semana.

Reusa el lazo que YA existe (cierre Optimizer→policy, 2026-06-10): una skill de
margen GRADUADA se espeja al mismo `settings['policy_margen_override']` que
`policy.py` ya consulta en vivo → aplica sin redeploy y sin tocar el motor.

PISOS (heredados de optimizer/policy): SOLO parámetros de marketing/operación
(márgenes, cohortes de win-back). JAMÁS triage, derivación clínica ni consent. Una
skill NUNCA crea una acción nueva: solo ajusta un parámetro numérico ya existente,
dentro de los límites duros de `policy.HardLimits`.

Gating (ambos OFF por defecto — nace inerte):
  • LEARNED_SKILLS_ACTIVE   → corre la observación/graduación (si OFF, `run()` es no-op).
  • LEARNED_SKILLS_AUTOAPPLY → permite que una skill ACTIVA se espeje a policy en vivo.
    Si OFF (default), una skill graduada queda "lista para aplicar" pero NO se aplica:
    el dueño la revisa y la prende. Coherente con AUTOAPPLY del Autopilot (OFF).

Persistencia: clave `learned_skills` dentro de data/autopilot_settings.json
(reusa `settings.py`, mismo archivo gitignored que el resto de `data/`).
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

log = logging.getLogger("bot")

# ── Knobs de graduación (la PUERTA DE PROMOCIÓN). Ver should_promote(). ──────────
MIN_CONFIRMATIONS = int(os.getenv("LEARNED_SKILLS_MIN_CONFIRMATIONS", "3"))
RETIRE_AFTER_REVERSALS = int(os.getenv("LEARNED_SKILLS_RETIRE_REVERSALS", "2"))
VALUE_TOLERANCE = 0.15   # ±15%: dos valores dentro de esto cuentan como "la misma" propuesta
_CONF_NUM = {"alta": 0.9, "media": 0.6, "baja": 0.3}

_SETTINGS_KEY = "learned_skills"
_POLICY_MARGEN_KEY = "policy_margen_override"   # la misma clave que policy._margen_overrides_aplicados lee


def _flag(name: str) -> bool:
    return os.getenv(name, "false").strip().lower() in ("1", "true", "yes", "on")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ─────────────────────────────────────────────────────────────────────────────
# Persistencia (sobre settings.py — mismo archivo que el resto del Autopilot)
# ─────────────────────────────────────────────────────────────────────────────
def _load() -> dict:
    try:
        from . import settings as ap_settings
        return dict(ap_settings.get(_SETTINGS_KEY) or {})
    except Exception as e:  # noqa: BLE001
        log.warning("learned_skills: no se pudo leer settings (%s)", e)
        return {}


def _save(skills: dict) -> None:
    try:
        from . import settings as ap_settings
        ap_settings.set_setting(_SETTINGS_KEY, skills)
    except Exception as e:  # noqa: BLE001
        log.warning("learned_skills: no se pudo guardar (%s)", e)


# ─────────────────────────────────────────────────────────────────────────────
# Modelo de una skill (dict serializable)
# ─────────────────────────────────────────────────────────────────────────────
def _rec_to_candidate(rec: dict) -> dict | None:
    """Traduce una recomendación efímera del optimizer a un CANDIDATO de skill.

    Solo destilamos recs que ajustan un parámetro numérico conocido. Hoy: márgenes
    por especialidad (las únicas con efecto cableado a policy en vivo). Las de
    win-back se observan también, pero su efecto queda 'manual' (policy aún no las
    consulta) — ver nota en run().
    """
    policy = str(rec.get("policy", ""))
    esp = (rec.get("especialidad") or "").lower().strip()
    val = rec.get("proposed_clp")

    if policy.startswith("margen[") and esp and val:
        return {
            "id": f"margen:{esp}",
            "kind": "margen",
            "scope": esp,
            "effect": {"param": "margen", "scope": esp, "value_clp": int(val)},
            "action": rec.get("action", "revisar"),
            "pattern": f"margen real ≈ ${int(val):,} para {esp} (asumido {rec.get('current')})",
            "evidence": rec.get("evidence", ""),
            "conf_obs": _CONF_NUM.get(rec.get("confidence"), 0.3),
            "source": rec.get("analyzer", "optimizer"),
        }
    return None


# ─────────────────────────────────────────────────────────────────────────────
# PUERTA DE PROMOCIÓN — el corazón del juicio (knob del dueño)
# ─────────────────────────────────────────────────────────────────────────────
def should_promote(skill: dict) -> bool:
    """¿Esta skill candidata es lo bastante CONFIABLE para graduarla a 'active'
    (= elegible para aplicarse en vivo)?

    Esta es la decisión de RIESGO de negocio: una skill graduada puede terminar
    moviendo presupuesto real de ads. Promover muy rápido = aprender ruido y
    sobre-gastar; muy lento = el sistema nunca capitaliza lo que ya descubrió.

    Señal de estabilidad disponible: `confirmations` = nº de corridas del optimizer
    (≈ semanales, independientes) que coincidieron en la MISMA dirección y valor.
    Cada confirmación extra es otra semana de datos diciendo lo mismo.

    >>> TODO(Rodrigo): calibrá el umbral a TU tolerancia al riesgo. Preguntas:
        - ¿Cuántas semanas seguidas confirmando exiges antes de confiar? (MIN_CONFIRMATIONS)
        - ¿Exiges que la última confianza del optimizer sea 'alta', o aceptas 'media'?
        - ¿Querés un piso distinto para parámetros caros (margen dental, que tolera
          CAC más alto) vs baratos?
    Abajo hay una implementación de referencia conservadora. Ajustala.
    """
    confirmations = int(skill.get("confirmations", 0))
    conf_obs = float(skill.get("conf_obs", 0.0))

    # Referencia: racha estable del valor ACTUAL (≥K confirmaciones seguidas) y la
    # última observación del optimizer de confianza decente. NO gateamos por
    # `reversals`: un cambio legítimo del mundo (margen que bajó de verdad) re-prueba
    # su nueva racha y debe poder graduar; solo el flip-flop crónico (que cuenta
    # `reversals` hacia el retiro) impide aplicar una skill.
    return confirmations >= MIN_CONFIRMATIONS and conf_obs >= 0.6


# ─────────────────────────────────────────────────────────────────────────────
# Observación: alimenta candidatos, confirma/reversa, gradúa y decae
# ─────────────────────────────────────────────────────────────────────────────
def _same_value(a: int, b: int) -> bool:
    if not a or not b:
        return a == b
    return abs(a - b) / max(abs(a), abs(b)) <= VALUE_TOLERANCE


def observe(recs: list[dict], skills: dict | None = None) -> dict:
    """Fusiona las recomendaciones de una corrida del optimizer en el registro de skills.

    - candidato nuevo            → se crea status='observing', confirmations=1.
    - mismo id y misma dirección → confirmations += 1 (estabilidad), valor = promedio suave.
    - dirección REVERTIDA        → reversals += 1; si ≥ RETIRE_AFTER_REVERSALS → 'retired'.
    - skill ACTIVA ausente este run → NEUTRAL, no decae. (Sutil pero clave: una vez que
      el override se aplica, el optimizer ya ve 'alineado' y deja de proponerlo; su
      AUSENCIA es ÉXITO, no fallo. Solo una rec en dirección OPUESTA la decae.)

    Devuelve el registro actualizado (NO persiste; eso lo hace run()).
    """
    skills = dict(skills if skills is not None else _load())
    seen: set[str] = set()

    for rec in recs or []:
        cand = _rec_to_candidate(rec)
        if not cand:
            continue
        sid = cand["id"]
        seen.add(sid)
        new_val = cand["effect"]["value_clp"]
        cur = skills.get(sid)

        if not cur:
            skills[sid] = {**cand, "status": "observing", "confirmations": 1,
                           "reversals": 0, "created_at": _now(), "updated_at": _now(),
                           "last_verified": _now()}
            continue

        if cur.get("status") == "retired":
            continue  # terminal: una skill retirada por ruido no se reanima sola

        prev_val = (cur.get("effect") or {}).get("value_clp", 0)
        cur["conf_obs"] = cand["conf_obs"]
        cur["evidence"] = cand["evidence"]
        cur["last_verified"] = _now()
        cur["updated_at"] = _now()

        if _same_value(prev_val, new_val):
            cur["confirmations"] = int(cur.get("confirmations", 0)) + 1
            # promedio suave para no saltar con cada corrida (EMA 0.7 viejo / 0.3 nuevo)
            cur["effect"]["value_clp"] = int(round(0.7 * prev_val + 0.3 * new_val))
            cur["pattern"] = cand["pattern"]
        else:
            # El valor contradice la racha actual. Cuenta como reversión de por vida y
            # arranca una racha FRESCA para el valor nuevo: si el mundo cambió de verdad,
            # se re-graduará; si oscila, las reversiones acumuladas lo retiran.
            cur["reversals"] = int(cur.get("reversals", 0)) + 1
            cur["confirmations"] = 1
            cur["effect"]["value_clp"] = new_val
            cur["pattern"] = cand["pattern"]
            if cur.get("status") == "active":
                cur["status"] = "observing"          # deja de aplicarse hasta re-probarse
                cur["demoted_at"] = _now()
            if cur["reversals"] >= RETIRE_AFTER_REVERSALS:
                cur["status"] = "retired"            # flip-flop crónico → fuera
                cur["retired_at"] = _now()
                continue

        # graduación
        if cur.get("status") == "observing" and should_promote(cur):
            cur["status"] = "active"
            cur["promoted_at"] = _now()
            log.info("learned_skills: GRADUADA %s tras %d confirmaciones",
                     sid, cur.get("confirmations"))

    return skills


# ─────────────────────────────────────────────────────────────────────────────
# Aplicación: espeja skills ACTIVAS al settings que policy ya lee (solo si AUTOAPPLY)
# ─────────────────────────────────────────────────────────────────────────────
def sync_active_to_policy(skills: dict) -> dict:
    """Espeja las skills de margen ACTIVAS a settings['policy_margen_override'].

    policy._margen_overrides_aplicados() ya lee esa clave en vivo → la skill aplica
    sin redeploy. SOLO corre si LEARNED_SKILLS_AUTOAPPLY=true; si no, no toca nada
    (las skills quedan graduadas pero inertes, esperando el OK del dueño).

    Devuelve {"applied": [...], "skipped_autoapply_off": bool}.
    """
    if not _flag("LEARNED_SKILLS_AUTOAPPLY"):
        return {"applied": [], "skipped_autoapply_off": True}
    try:
        from . import settings as ap_settings
    except Exception as e:  # noqa: BLE001
        log.warning("learned_skills: settings no importable para aplicar (%s)", e)
        return {"applied": [], "skipped_autoapply_off": False}

    ov = dict(ap_settings.get(_POLICY_MARGEN_KEY) or {})
    applied = []
    for s in skills.values():
        if s.get("status") != "active" or s.get("kind") != "margen":
            continue
        eff = s.get("effect") or {}
        scope, val = eff.get("scope"), eff.get("value_clp")
        if scope and val and ov.get(scope) != val:
            ov[scope] = int(val)
            applied.append({"scope": scope, "value_clp": int(val)})
    if applied:
        ap_settings.set_setting(_POLICY_MARGEN_KEY, ov)
        log.info("learned_skills: aplicadas %d skills de margen a policy", len(applied))
    return {"applied": applied, "skipped_autoapply_off": False}


# ─────────────────────────────────────────────────────────────────────────────
# Orquestación
# ─────────────────────────────────────────────────────────────────────────────
def run() -> dict:
    """Corre el optimizer, destila/actualiza skills, gradúa, aplica (si AUTOAPPLY) y
    persiste. No-op si LEARNED_SKILLS_ACTIVE está OFF. Pensado para un cron semanal
    o el botón del panel. Degrada limpio si el optimizer no tiene datos."""
    if not _flag("LEARNED_SKILLS_ACTIVE"):
        return {"active": False, "note": "LEARNED_SKILLS_ACTIVE off — no-op"}

    try:
        from . import optimizer
        rep = optimizer.run_analysis()
        recs = rep.get("recommendations", [])
    except Exception as e:  # noqa: BLE001
        log.warning("learned_skills: optimizer falló (%s)", e)
        recs, rep = [], {"notes": [f"optimizer error: {e}"]}

    skills = observe(recs)
    apply_res = sync_active_to_policy(skills)
    _save(skills)

    # Nota honesta: por ahora SOLO las skills de margen tienen efecto cableado a policy.
    # Las de win-back se observan/gradúan pero su aplicación es manual (policy todavía
    # no consulta cohortes) → quedan listadas como 'sin cableado' para futura fase.
    return {"active": True, "skills": _public(skills),
            "graduated": [s["id"] for s in skills.values() if s.get("status") == "active"],
            "applied": apply_res, "optimizer_notes": rep.get("notes", [])}


def summary() -> dict:
    """Vista read-only para el panel/API. No corre nada — lee lo persistido."""
    return {"skills": _public(_load()),
            "autoapply": _flag("LEARNED_SKILLS_AUTOAPPLY"),
            "active": _flag("LEARNED_SKILLS_ACTIVE")}


def _public(skills: dict) -> list[dict]:
    """Ordena para mostrar: activas primero, luego por confirmaciones desc."""
    _order = {"active": 0, "observing": 1, "retired": 2}
    return sorted(skills.values(),
                  key=lambda s: (_order.get(s.get("status"), 9),
                                 -int(s.get("confirmations", 0))))


def _print(rep: dict) -> None:
    print(f"\n-- Skills aprendidas - active={rep.get('active')} --\n")
    for s in rep.get("skills", []):
        eff = s.get("effect", {})
        print(f"[{s.get('status','?').upper():9}] {s['id']}  "
              f"conf×{s.get('confirmations',0)} rev×{s.get('reversals',0)}")
        print(f"    {s.get('pattern','')}")
        print(f"    efecto: {eff.get('param')}[{eff.get('scope')}] = ${eff.get('value_clp',0):,}\n")
    if rep.get("applied", {}).get("skipped_autoapply_off"):
        print("  (AUTOAPPLY off → skills graduadas NO aplicadas; esperan OK del dueño)")
    for n in rep.get("optimizer_notes", [])[:6]:
        print(f"  · {n}")


if __name__ == "__main__":
    import sys
    sys.path.insert(0, "app")
    try:
        from dotenv import load_dotenv
        load_dotenv(".env")
    except Exception:  # noqa: BLE001
        pass
    # Para inspección manual forzamos el flag de lectura sin tocar el .env real.
    os.environ.setdefault("LEARNED_SKILLS_ACTIVE", "true")
    _print(run())
