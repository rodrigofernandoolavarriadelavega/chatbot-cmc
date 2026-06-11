"""Director — la capa agéntica MAESTRA (autonomía sobre la autonomía).

Lee el P&L real de cada agente y DECIDE qué conviene prender o apagar según la
eficiencia económica, dentro de un PRESUPUESTO que el dueño define (día/semana/mes
o libre). Es el cerebro encima del switchboard: en vez de que el dueño mueva 33
switches, mueve UNO (el Director) y el Director propone/gobierna el resto.

v1 = MODO PROPUESTA (read-only): recomienda qué flipear con evidencia, NO toca nada.

Asimetría de seguridad (clave):
  • APAGAR por mal resultado es seguro — corta pérdidas, reversible.
  • PRENDER algo que contacta pacientes o gasta plata exige evidencia verificada
    contra la caja + reversibilidad + que los gates por-agente (legal, presupuesto
    de contacto) sigan firmes. Auto-ejecución gateada (DIRECTOR_AUTOEXEC_ENABLED).
El switch maestro humano siempre manda.

El Director es tan bueno como sus mediciones: solo gobierna agentes con P&L
verificado contra la caja real (bi_pagos_caja). Lo no medible queda al dueño.
"""
from __future__ import annotations

import json
import logging
import os

log = logging.getLogger("bot")

DIRECTOR_AUTOEXEC = os.getenv("DIRECTOR_AUTOEXEC_ENABLED", "false").lower() in ("1", "true", "yes", "on")

# Canal del P&L → switch (flag) que lo prende/apaga + etiqueta.
CHANNEL_SWITCH = {
    "Win-back (WhatsApp)": {"flag": "WINBACK_ACTIVE", "label": "Win-back", "spends": False},
    "Email": {"flag": "EMAIL_SENDING_ENABLED", "label": "Email marketing", "spends": False},
    "Meta Ads": {"flag": "AUTOPILOT_EXECUTE", "label": "Autopilot Ads", "spends": True},
}

_PERIOD_DAYS = {"day": 1, "week": 7, "month": 30}


def _conn():
    from session import db as _c
    return _c()


def _switch_on(flag: str) -> bool:
    return os.getenv(flag, "false").lower() in ("1", "true", "yes", "on")


# ─────────────────────────────────────────────────────────────────────────────
# Presupuesto — el sobre de dinero que el dueño le da al Director
# ─────────────────────────────────────────────────────────────────────────────
def _ensure_table(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS director_config (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            mode TEXT DEFAULT 'free',      -- 'free' | 'fixed'
            amount INTEGER DEFAULT 0,       -- CLP por período (si fixed)
            period TEXT DEFAULT 'week',     -- 'day' | 'week' | 'month'
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)


def get_budget() -> dict:
    """Config de presupuesto. Default: libre (sin tope)."""
    with _conn() as conn:
        _ensure_table(conn)
        row = conn.execute("SELECT mode, amount, period FROM director_config WHERE id=1").fetchone()
    if not row:
        return {"mode": "free", "amount": 0, "period": "week"}
    return {"mode": row["mode"], "amount": row["amount"] or 0, "period": row["period"] or "week"}


def set_budget(mode: str, amount: int = 0, period: str = "week") -> dict:
    """Define el presupuesto. mode='free' (libre) o 'fixed' (tope por período)."""
    mode = "fixed" if str(mode).lower() == "fixed" else "free"
    period = period if period in _PERIOD_DAYS else "week"
    amount = max(0, int(amount or 0))
    with _conn() as conn:
        _ensure_table(conn)
        conn.execute("""
            INSERT INTO director_config (id, mode, amount, period, updated_at)
            VALUES (1, ?, ?, ?, datetime('now'))
            ON CONFLICT(id) DO UPDATE SET mode=excluded.mode, amount=excluded.amount,
                period=excluded.period, updated_at=datetime('now')
        """, (mode, amount, period))
        conn.commit()
    return get_budget()


# ─────────────────────────────────────────────────────────────────────────────
# Evaluación — la decisión del Director
# ─────────────────────────────────────────────────────────────────────────────
def evaluate() -> dict:
    """Lee el P&L real del período del presupuesto y decide, por agente medible:
    mantener/prender (rinde y cabe en el sobre) o apagar (pierde plata). Respeta el
    presupuesto: la suma del gasto recomendado nunca lo supera. NO ejecuta: propone."""
    budget = get_budget()
    days = _PERIOD_DAYS.get(budget["period"], 7)
    try:
        from . import impact
        pnl = impact.pnl(days=days)
    except Exception as e:  # noqa: BLE001
        return {"error": f"P&L no disponible: {e}", "budget": budget, "recommendations": []}

    channels = pnl.get("channels", [])
    # Dos relojes (fix 2026-06-10): el táctico (ventana del presupuesto) dice cuánta
    # plata mover; el estratégico (90d) dice si un canal VIVE o MUERE. Sin esto el
    # operador recomendó apagar win-back por una semana floja (neto −$1.450) cuando
    # el 90d daba ROAS 29× — el canal no estaba malo, tenía el pool de consent seco.
    try:
        pnl90 = impact.pnl(days=90) if days < 90 else pnl
    except Exception:  # noqa: BLE001
        pnl90 = pnl
    ch90 = {c["channel"]: c for c in pnl90.get("channels", [])}
    spend_used = sum(c["cost_clp"] for c in channels
                     if CHANNEL_SWITCH.get(c["channel"], {}).get("spends"))
    cap = budget["amount"] if budget["mode"] == "fixed" else None
    remaining = (cap - spend_used) if cap is not None else None

    recs = []
    for c in channels:
        meta = CHANNEL_SWITCH.get(c["channel"])
        if not meta:
            continue
        on = _switch_on(meta["flag"])
        roas = c.get("roas")
        net = c["net_clp"]
        rev, cost = c["revenue_clp"], c["cost_clp"]

        # Política económica honesta:
        if cost == 0 and c["bookings"] == 0 and rev == 0:
            action, why, conf = "esperar", "sin actividad medible en el período.", "baja"
        elif net > 0 and (roas is None or roas >= 1):
            action = "prender" if not on else "mantener ON"
            why = f"rinde: ${rev:,} ingreso vs ${cost:,} costo (neto +${net:,}{', ROAS '+str(roas)+'x' if roas else ''})."
            conf = "alta" if (roas or 99) >= 2 else "media"
        elif net < 0:
            c90 = ch90.get(c["channel"]) or {}
            net90 = c90.get("net_clp", 0) or 0
            roas90 = c90.get("roas")
            if net90 > 0:
                # Ventana corta negativa pero el 90d RINDE → el canal no está malo,
                # tiene un cuello (pool seco, estacionalidad). No se mata un canal
                # rentable por una semana floja.
                action = "vigilar"
                why = (f"ventana corta pierde (neto ${net:,}) pero a 90d RINDE "
                       f"(neto +${net90:,}" + (f", ROAS {roas90}×" if roas90 else "") +
                       ") → no apagar por una semana floja; revisar el cuello "
                       "(pool de consent / estacionalidad).")
                conf = "media"
            else:
                action = "apagar" if on else "mantener OFF"
                why = f"pierde: ${cost:,} costo vs ${rev:,} ingreso (neto ${net:,}). Cortar es seguro y reversible."
                conf = "alta"
        else:
            action, why, conf = "vigilar", f"al límite (neto ${net:,}).", "media"

        # Gate de presupuesto: si gasta y no cabe en lo que queda, no se recomienda prender.
        budget_block = None
        if meta["spends"] and action in ("prender", "mantener ON") and cap is not None:
            if cost > max(0, remaining if remaining is not None else 0) and not on:
                budget_block = (f"el presupuesto {budget['period']} (${cap:,}) ya está comprometido "
                                f"(${spend_used:,} usado). No alcanza para prender esto sin subir el sobre.")
                action = "esperar presupuesto"
                conf = "media"

        _c90 = ch90.get(c["channel"]) or {}
        recs.append({
            "switch": meta["label"], "flag": meta["flag"], "channel": c["channel"],
            "estado_actual": "ON" if on else "OFF", "accion": action,
            "razon": why, "confianza": conf, "spends": meta["spends"],
            "roas": roas, "net_clp": net, "cost_clp": cost, "revenue_clp": rev,
            "budget_block": budget_block,
            # Dos relojes: el juicio declara su ventana y expone el 90d al lado.
            "ventana_dias": days,
            "roas_90d": _c90.get("roas"), "net_90d_clp": _c90.get("net_clp"),
        })

    order = {"apagar": 0, "prender": 1, "esperar presupuesto": 2, "mantener ON": 3,
             "mantener OFF": 4, "vigilar": 5, "esperar": 6}
    recs.sort(key=lambda r: order.get(r["accion"], 9))

    return {
        "mode": "propose-only",
        "autoexec_enabled": DIRECTOR_AUTOEXEC,
        "budget": {**budget, "spent_period": spend_used,
                   "remaining": remaining,
                   "label": ("Libre (sin tope)" if budget["mode"] == "free"
                             else f"${budget['amount']:,} / {budget['period']}")},
        "recommendations": recs,
        "note": ("Solo gobierna agentes con P&L verificado contra la caja real. "
                 "Lo no medible queda a tu criterio. Asimetría: apagar es libre; "
                 "prender lo que gasta exige evidencia + presupuesto."),
        "pnl_period_days": days,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Sugerencias de agentes NUEVOS — el Director propone qué crear (detecta vacíos)
# ─────────────────────────────────────────────────────────────────────────────
def suggest_new_agents() -> list[dict]:
    """El Director mira los datos y propone agentes que AÚN NO existen pero que
    rellenarían un vacío con plata adentro. Donde puede, con evidencia real."""
    out = []

    # 1) Vacío de consentimiento (data real): si hay muchos contactables sin consent,
    #    el mayor lever es un agente que pida permiso para destrabar el win-back.
    try:
        from winback import bi_conn
        with bi_conn() as c:
            cur = c.cursor()
            cur.execute("""
                SELECT COUNT(DISTINCT RIGHT(regexp_replace(wc.telefono,'[^0-9]','','g'),9))
                FROM bi.v_winback_cohortes_contactables wc
                WHERE NOT EXISTS (SELECT 1 FROM bi.marketing_consent mc
                    WHERE RIGHT(regexp_replace(mc.phone,'[^0-9]','','g'),9)
                        = RIGHT(regexp_replace(wc.telefono,'[^0-9]','','g'),9)
                      AND mc.status='accepted')
            """)
            sin_consent = cur.fetchone()[0] or 0
        if sin_consent >= 500:
            out.append({
                "nombre": "Agente de captación de consentimiento",
                "por_que": f"{sin_consent:,} pacientes contactables NO tienen permiso de marketing → "
                           f"el win-back (que rinde) no les puede llegar. Un agente que pida permiso "
                           f"(mensaje de servicio, legal) destraba a miles de recuperables.",
                "valor": "Es el cuello #1: convierte el pool muerto en recuperable.",
                "evidencia": "dato real (BI)", "riesgo": "medio — envía mensajes, gated"})
    except Exception as e:  # noqa: BLE001
        log.warning("suggest consent gap: %s", e)

    # 2) Agentes medibles sin instrumentar: si un agente está prendido pero no tiene
    #    P&L, el Director está ciego sobre él → propone instrumentarlo.
    out.append({
        "nombre": "Agente de medición (instrumentar lo no medido)",
        "por_que": "El Director solo gobierna lo que tiene P&L verificado contra caja. "
                   "Los agentes sin medición quedan fuera de su decisión.",
        "valor": "Cada agente medido es uno más que el Director puede optimizar solo.",
        "evidencia": "estructural", "riesgo": "bajo — solo lee"})

    # 3) Ideas de cobertura (heurísticas de oficio, a validar con datos):
    out.append({
        "nombre": "Agente anti-no-show (confirmación inteligente)",
        "por_que": "Las inasistencias son plata perdida en agenda. Un agente que confirme "
                   "y reasigne la hora si el paciente no responde recupera cupos.",
        "valor": "Sube la ocupación real sin gastar en ads.",
        "evidencia": "idea — medir tasa de no-show primero", "riesgo": "bajo"})
    out.append({
        "nombre": "Agente de reputación post-pago",
        "por_que": "Pedir reseña justo al paciente que pagó y quedó contento sube el flujo "
                   "orgánico (el canal más barato).",
        "valor": "Baja el CAC futuro sin costo de ads.",
        "evidencia": "idea", "riesgo": "bajo"})
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Aprendizaje — cómo el Director mejora con el tiempo
# ─────────────────────────────────────────────────────────────────────────────
def _ensure_rec_log(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS director_recommendations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            switch TEXT, accion TEXT, net_clp INTEGER, roas REAL,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)


def log_recommendations(recs: list[dict]) -> None:
    """Guarda lo que el Director recomendó hoy. Mañana puede comparar el outcome
    contra lo recomendado → así aprende qué decisiones suyas funcionaron (base del
    loop de mejora; el juicio fino lo da Champion/Challenger sobre la misma data)."""
    try:
        with _conn() as conn:
            _ensure_rec_log(conn)
            for r in recs:
                conn.execute(
                    "INSERT INTO director_recommendations (switch, accion, net_clp, roas) "
                    "VALUES (?,?,?,?)", (r.get("switch"), r.get("accion"),
                                        r.get("net_clp"), r.get("roas")))
            conn.commit()
    except Exception as e:  # noqa: BLE001
        log.warning("log_recommendations: %s", e)


# ─────────────────────────────────────────────────────────────────────────────
# EL GABINETE — el Director no es uno, son varios roles que se vigilan entre sí
# ─────────────────────────────────────────────────────────────────────────────
def self_audit() -> dict:
    """EL AUDITOR — evalúa al PROPIO Director: ¿sus decisiones pasadas funcionaron?
    Compara lo que recomendó antes contra cómo le fue después. Así el gabinete se
    vigila a sí mismo (contrapeso clave: nadie se cuida solo sin un auditor)."""
    try:
        with _conn() as conn:
            _ensure_rec_log(conn)
            n = conn.execute("SELECT COUNT(*) AS n FROM director_recommendations").fetchone()["n"]
            rows = conn.execute(
                "SELECT switch, accion, net_clp, created_at FROM director_recommendations "
                "ORDER BY created_at DESC LIMIT 8").fetchall()
    except Exception as e:  # noqa: BLE001
        return {"rol": "Auditor", "estado": f"sin acceso al log: {e}", "historial": []}
    if n == 0:
        return {"rol": "Auditor",
                "estado": "Sin historial todavía: cada corrida del Director se registra. "
                          "Cuando haya semanas de decisiones, este rol compara recomendación "
                          "vs outcome y reporta qué decisiones del Director funcionaron.",
                "historial": []}
    return {"rol": "Auditor", "estado": f"{n} recomendaciones registradas — base de aprendizaje activa.",
            "historial": [dict(r) for r in rows]}


def decision_review() -> dict:
    """EL OPTIMIZADOR DE DECISIONES — revisa las REGLAS con que el Director decide
    (no las decisiones puntuales). Propone afinar umbrales. La prueba dura (¿este
    cambio de regla mejora?) la corre Champion/Challenger sobre la misma data."""
    notes = [
        "Regla actual: neto>0 → prender/mantener; neto<0 → apagar (apagar es libre, prender exige presupuesto).",
        "Refinamiento sugerido: subir el umbral de 'prender' a ROAS≥2 para lo que gasta (margen de seguridad), "
        "y exigir ≥2 períodos seguidos antes de cambiar, para no reaccionar a ruido.",
    ]
    try:
        from . import experiments
        exps = experiments.list_experiments()
        notes.append(f"Champion/Challenger activo: {len(exps)} experimento(s) de política en el ledger — "
                     f"el Director adopta solo lo que el experimento confirma, no lo marginal.")
    except Exception:  # noqa: BLE001
        pass
    return {"rol": "Optimizador de decisiones", "propuestas": notes}


def rieles_review() -> dict:
    """Vigía de los rieles de mensajería (consent caliente / promo post-atención /
    eco prep / relleno de cupos): lee su P&L real y recomienda con reglas simples
    y honestas — con muestra chica dice 'esperar', no inventa. READ-ONLY."""
    try:
        import sys as _sys
        if "app" not in _sys.path:
            _sys.path.insert(0, "app")
        from rieles_pnl import pnl as _rieles_pnl
        snap = _rieles_pnl()
    except Exception as e:  # noqa: BLE001
        return {"error": f"P&L rieles no disponible: {e}", "recommendations": []}

    recs = []
    for r in snap.get("rieles", []):
        st, key = r.get("stats") or {}, r["key"]
        accion, razon, conf = "esperar", "muestra aún chica para juzgar.", "baja"
        if key == "consent_caliente":
            resp = (st.get("aceptados") or 0) + (st.get("rechazados") or 0)
            tasa = st.get("tasa_aceptacion")
            if resp >= 10 and tasa is not None:
                if tasa >= 55:
                    accion, razon, conf = "mantener", f"acepta el {tasa}% de los que responden — el pool crece sano.", "alta"
                elif tasa < 40:
                    accion, razon, conf = "revisar", f"solo {tasa}% de aceptación ({resp} respuestas) — revisar copy/momento del envío.", "media"
                else:
                    accion, razon, conf = "mantener", f"aceptación {tasa}% — aceptable, seguir mirando.", "media"
        elif key == "promo_postconsent":
            env, ag, caja = st.get("enviados") or 0, st.get("agendaron") or 0, st.get("ingreso_clp") or 0
            if caja > 0:
                accion, razon, conf = "mantener", f"genera caja real: ${caja:,} de {env} promos.", "alta"
            elif env >= 30 and ag == 0:
                accion, razon, conf = "revisar", f"{env} promos sin ninguna agenda — revisar oferta/segmento antes de seguir.", "media"
        elif key == "eco_prep":
            accion, razon, conf = "mantener", "riel de servicio (protege horas de ecógrafo); su retorno es evitar exámenes perdidos, no caja directa.", "media"
        elif key == "operativa":
            of, conf_n = st.get("ofertas_enviadas") or 0, st.get("confirmadas") or 0
            if conf_n > 0:
                accion, razon, conf = "mantener", f"rescata cupos reales: {conf_n} confirmadas de {of} ofertas.", "alta"
            elif of >= 10:
                accion, razon, conf = "revisar", f"{of} ofertas sin ninguna confirmación — ¿lista de espera desactualizada o copy?", "media"
        recs.append({"riel": r["label"], "flag": r["flag"], "resumen": r.get("resumen", ""),
                     "accion": accion, "razon": razon, "confianza": conf})
    return {"since": snap.get("since"), "recommendations": recs,
            "nota": "Los switches de rieles viven en la Sala de Máquinas; esto solo recomienda."}


def cabinet() -> dict:
    """El gabinete completo del Director: 4 roles que se vigilan entre sí.
      • Operador  — decide qué switch prender/apagar (dentro del presupuesto).
      • Auditor   — evalúa al propio Director (sus decisiones pasadas).
      • Inventor  — propone agentes NUEVOS que rellenarían vacíos.
      • Optimizador de decisiones — afina las REGLAS con que se decide.
    READ-ONLY. El switch maestro humano manda sobre todos."""
    operador = evaluate()
    if operador.get("recommendations"):
        log_recommendations(operador["recommendations"])  # alimenta el aprendizaje
    return {
        "gabinete": {
            "operador": operador,
            "auditor": self_audit(),
            "inventor": suggest_new_agents(),
            "optimizador_decisiones": decision_review(),
            "vigia_rieles": rieles_review(),
        },
        "mode": "propose-only",
        "autoexec_enabled": DIRECTOR_AUTOEXEC,
        "nota": "El Director es un gabinete con contrapesos, no un agente-dios. Cada rol "
                "vigila una cosa, incluso a sí mismo. Tú tienes el switch maestro encima.",
    }


if __name__ == "__main__":
    import sys
    sys.path.insert(0, "app")
    try:
        from dotenv import load_dotenv
        load_dotenv(".env")
    except Exception:  # noqa: BLE001
        pass
    rep = evaluate()
    b = rep.get("budget", {})
    print(f"\n-- DIRECTOR ({rep.get('mode')}) -- presupuesto: {b.get('label')} "
          f"(usado ${b.get('spent_period',0):,})\n")
    for r in rep.get("recommendations", []):
        print(f"[{r['accion'].upper()}] {r['switch']} (ahora {r['estado_actual']}, conf {r['confianza']})")
        print(f"   {r['razon']}")
        if r.get("budget_block"):
            print(f"   ⚠ {r['budget_block']}")
    print(f"\n{rep.get('note','')}")
