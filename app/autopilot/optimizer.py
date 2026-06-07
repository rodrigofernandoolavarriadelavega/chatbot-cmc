"""Optimizador de Políticas (nivel 5) — MODO SOLO-PROPUESTA (read-only).

Cruza outcomes REALES (caja BI, honorarios, conversión de win-back) contra las
políticas vigentes (policy.py, winback) y PROPONE cambios con evidencia. NO aplica
nada: devuelve recomendaciones {policy, current, proposed, evidence, impact,
confidence, action}. La aplicación gobernada (champion/challenger con holdout) es
una fase posterior.

Primer peldaño del nivel 5 más seguro: el sistema detecta sus PROPIAS reglas mal
calibradas, sin tocarlas. Generaliza el descubrimiento manual de ecografía.

Analizadores (cada uno degrada limpio si su data no está):
  - analyze_margins:  margen asumido (policy._margen_esp) vs margen REAL = ingreso ×
                      (1 − pct_honorario), usando dim_honorarios. (No el ingreso pelado:
                      el profesional se queda con ~70% en la mayoría de especialidades.)
  - analyze_winback:  conversión REAL por cohorte (winback_envios: enviado vs agendó).

Pisos: SOLO toca políticas de marketing/operación. Jamás triage, derivación clínica
ni consent.
"""
from __future__ import annotations

import logging

log = logging.getLogger("bot")

MARGIN_GAP_PCT = 25          # % de desalineación para marcar
WINBACK_MIN_SENDS = 25       # mínimo de envíos por cohorte para confiar en la conversión


def _rec(policy, current, proposed, evidence, impact, confidence, action="revisar"):
    return {"policy": policy, "current": current, "proposed": proposed,
            "evidence": evidence, "impact": impact,
            "confidence": confidence, "action": action}


def _bi():
    try:
        from winback import bi_conn
        return bi_conn
    except Exception as e:  # noqa: BLE001
        log.warning("optimizer: BI no disponible (%s)", e)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Analizador 1 — Margen REAL (con honorario) vs asumido por especialidad
# ─────────────────────────────────────────────────────────────────────────────
_SQL_MARGEN = """
WITH hon AS (
  SELECT profesional_id, MAX(pct_profesional) AS pct
  FROM bi.dim_honorarios
  WHERE activo IS NOT FALSE AND tipo_pago = 'porcentaje'
  GROUP BY profesional_id
)
SELECT e.nombre, COUNT(*) AS n,
       ROUND(AVG(f.monto_neto)) AS ingreso,
       ROUND(AVG(h.pct)) AS pct,
       ROUND(AVG(CASE WHEN h.pct IS NOT NULL THEN f.monto_neto * (1 - h.pct/100.0) END)) AS margen_real
FROM bi.fact_ingresos f
JOIN bi.dim_profesional  p ON p.profesional_id = f.profesional_id
JOIN bi.dim_especialidad e ON e.especialidad_id = p.especialidad_id
LEFT JOIN hon h ON h.profesional_id = f.profesional_id
WHERE f.fecha >= CURRENT_DATE - (%s * INTERVAL '1 day')
GROUP BY e.nombre
HAVING COUNT(*) >= %s
ORDER BY n DESC
"""


def analyze_margins(days: int = 90, min_n: int = 5) -> tuple[list[dict], list[str]]:
    """Compara el margen ASUMIDO por policy.py con el margen REAL de contribución =
    ingreso por paciente × (1 − pct_honorario). El honorario (≈70% para el profesional
    en la mayoría de especialidades) es el costo variable dominante, así que esto es
    un margen de verdad, no el ingreso pelado.

      • asumido > real +25% → SOBREESTIMA rentabilidad → el Autopilot tolera CAC más
        alto del real → riesgo de sobre-gasto. Confianza ALTA, action=bajar.
      • asumido < real −25% → SUBESTIMA → demasiado conservador, deja de escalar lo
        que conviene. Confianza MEDIA, action=subir.
    """
    bi_conn = _bi()
    if not bi_conn:
        return [], ["BI no disponible: analyze_margins omitido."]
    try:
        from autopilot import policy
    except Exception as e:  # noqa: BLE001
        return [], [f"policy no importable: {e}"]
    try:
        with bi_conn() as c:
            cur = c.cursor()
            cur.execute(_SQL_MARGEN, (days, min_n))
            rows = cur.fetchall()
    except Exception as e:  # noqa: BLE001
        return [], [f"query de margen falló: {e}"]

    recs, notes = [], []
    for esp, n, ingreso, pct, margen_real in rows:
        ingreso = int(ingreso or 0)
        if not margen_real:
            notes.append(f"{esp}: sin honorario %-based (prob. sueldo fijo) — margen real no estimable acá.")
            continue
        real = int(margen_real)
        asum = policy._margen_esp(esp)
        gap = (asum - real) / real * 100 if real else 0
        base = (f"{n} atenciones/90d · ingreso ${ingreso:,}/paciente · honorario {int(pct or 0)}% → "
                f"margen real CMC ≈ ${real:,}. Asumido ${asum:,} ({gap:+.0f}%).")
        if gap >= MARGIN_GAP_PCT:
            recs.append(_rec(
                policy=f"margen[{esp}]", current=f"${asum:,}", proposed=f"≈ ${real:,}",
                evidence=base,
                impact="SOBREESTIMA la rentabilidad → el Autopilot tolera un CAC más alto del real "
                       "y puede sobre-gastar en esta especialidad. Bajar acerca el CAC tolerable a la realidad.",
                confidence="alta", action="bajar"))
        elif gap <= -MARGIN_GAP_PCT:
            recs.append(_rec(
                policy=f"margen[{esp}]", current=f"${asum:,}", proposed=f"≈ ${real:,}",
                evidence=base,
                impact="SUBESTIMA la rentabilidad → el Autopilot es demasiado conservador y puede "
                       "dejar de escalar campañas que sí convienen.",
                confidence="media", action="subir"))
        else:
            notes.append(f"{esp}: asumido ${asum:,} vs real ${real:,} ({gap:+.0f}%) — alineado.")
    return recs, notes


# ─────────────────────────────────────────────────────────────────────────────
# Analizador 2 — Conversión REAL de win-back por cohorte
# ─────────────────────────────────────────────────────────────────────────────
_SQL_WINBACK = """
SELECT cohorte, COUNT(*) AS enviados,
       SUM(CASE WHEN agendo_at IS NOT NULL OR cita_atribuida_id IS NOT NULL THEN 1 ELSE 0 END) AS agendaron
FROM bi.winback_envios
GROUP BY cohorte
"""


def analyze_winback() -> tuple[list[dict], list[str]]:
    """Conversión REAL (enviado → agendó) por cohorte de días inactivos. Si las
    cohortes FRESCAS convierten claramente mejor que las viejas, propone disparar el
    win-back antes (más recencia = más reenganche). Evidencia dura, no supuestos."""
    bi_conn = _bi()
    if not bi_conn:
        return [], ["BI no disponible: analyze_winback omitido."]
    try:
        with bi_conn() as c:
            cur = c.cursor()
            cur.execute(_SQL_WINBACK)
            rows = cur.fetchall()
    except Exception as e:  # noqa: BLE001
        return [], [f"query de winback falló: {e}"]

    # normaliza cohorte → días para ordenar (030d, 060d, 090d, 180d, 365d)
    def _days(coh):
        s = "".join(ch for ch in str(coh) if ch.isdigit())
        return int(s) if s else 9999

    data = []
    for coh, env, ag in rows:
        env = env or 0
        if env <= 0:
            continue
        data.append({"cohorte": str(coh), "dias": _days(coh),
                     "enviados": env, "agendaron": ag or 0,
                     "conv": round(100 * (ag or 0) / env, 1)})
    data.sort(key=lambda d: d["dias"])

    notes = ["conversión por cohorte: " + " · ".join(
        f"{d['cohorte']}={d['conv']}% (n={d['enviados']})" for d in data)]

    # cohortes con suficiente n para confiar
    trust = [d for d in data if d["enviados"] >= WINBACK_MIN_SENDS and d["dias"] < 9999]
    recs = []
    if len(trust) >= 2:
        fresh = min(trust, key=lambda d: d["dias"])
        old = max(trust, key=lambda d: d["dias"])
        if fresh["conv"] >= 1.5 * max(old["conv"], 0.1) and fresh["conv"] > old["conv"]:
            recs.append(_rec(
                policy="WINBACK — prioridad/corte de cohorte",
                current=f"se contacta a todas; mayor volumen va a cohortes viejas",
                proposed=f"priorizar y/o adelantar el corte hacia {fresh['cohorte']} (champion/challenger)",
                evidence=f"la cohorte {fresh['cohorte']} convierte {fresh['conv']}% vs {old['conv']}% "
                         f"en {old['cohorte']} — los pacientes más frescos reenganchan "
                         f"{fresh['conv']/max(old['conv'],0.1):.1f}× mejor.",
                impact="Mover esfuerzo/presupuesto de contacto hacia cohortes frescas sube la "
                       "conversión total del win-back sin gastar más mensajes.",
                confidence="alta" if fresh["enviados"] >= 2 * WINBACK_MIN_SENDS else "media",
                action="experimentar"))
    else:
        notes.append("pocas cohortes con n suficiente — conversión aún no concluyente.")
    return recs, notes


# ─────────────────────────────────────────────────────────────────────────────
# Orquestación
# ─────────────────────────────────────────────────────────────────────────────
ANALYZERS = [
    ("Margen por especialidad", analyze_margins),
    ("Conversión de win-back", analyze_winback),
]

_CONF_ORDER = {"alta": 0, "media": 1, "baja": 2}


def run_analysis() -> dict:
    """Corre todos los analizadores. Devuelve recomendaciones + notas. NO aplica nada."""
    all_recs, all_notes = [], []
    for name, fn in ANALYZERS:
        try:
            recs, notes = fn()
        except Exception as e:  # noqa: BLE001
            all_notes.append(f"[{name}] error: {e}")
            continue
        for r in recs:
            r["analyzer"] = name
        all_recs.extend(recs)
        all_notes.extend(f"[{name}] {n}" for n in notes)
    all_recs.sort(key=lambda r: _CONF_ORDER.get(r.get("confidence"), 9))
    return {"mode": "propose-only", "recommendations": all_recs,
            "notes": all_notes, "count": len(all_recs)}


def _print_report(rep: dict) -> None:
    recs = rep["recommendations"]
    print(f"\n-- Optimizador de Politicas - {rep['mode']} - {rep['count']} propuestas --\n")
    if not recs:
        print("  Sin desalineaciones accionables con los datos disponibles.")
    for i, r in enumerate(recs, 1):
        print(f"[{i}] {r['policy']}  ({r['confidence'].upper()} - {r['action']})")
        print(f"    actual:    {r['current']}")
        print(f"    propuesto: {r['proposed']}")
        print(f"    evidencia: {r['evidence']}")
        print(f"    impacto:   {r['impact']}\n")
    if rep["notes"]:
        print("-- notas/diagnostico --")
        for n in rep["notes"]:
            print(f"  - {n}")


if __name__ == "__main__":
    import sys
    sys.path.insert(0, "app")
    try:
        from dotenv import load_dotenv
        load_dotenv(".env")
    except Exception:  # noqa: BLE001
        pass
    _print_report(run_analysis())
