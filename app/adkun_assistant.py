"""Asistente Adkun — la cara conversacional de la capa agéntica por WhatsApp.

Cuando el DUEÑO le escribe a su número (gateado por ADKUN_ASSISTANT_PHONES), en vez
del flujo de pacientes recibe reportes de la capa agéntica: P&L, win-back, el Director,
Autopilot, Optimizador. Todo dentro de la ventana de 24h con solo mandar un mensaje.

Es READ-ONLY: informa y recomienda, no ejecuta nada (los switches siguen en la Sala
de Máquinas). Diseñado para responder corto y claro, formato WhatsApp.
"""
from __future__ import annotations

import logging

log = logging.getLogger("bot")


def _clp(n) -> str:
    try:
        return "$" + format(int(n or 0), ",").replace(",", ".")
    except Exception:  # noqa: BLE001
        return "$0"


def _menu() -> str:
    return (
        "🧠 *Asistente Adkun* — tu capa agéntica\n\n"
        "Escríbeme una de estas:\n"
        "• *plata* — P&L: qué generó y costó cada canal\n"
        "• *winback* — recuperación de pacientes (ingreso real)\n"
        "• *director* — qué conviene prender/apagar + agentes nuevos\n"
        "• *ads* — rendimiento del Autopilot de publicidad\n"
        "• *reglas* — políticas mal calibradas (Optimizador)\n\n"
        "_Todo es lectura: te informo, tú decides en la Sala de Máquinas._"
    )


def _pnl() -> str:
    try:
        from autopilot import impact
        d = impact.pnl(days=30)
    except Exception as e:  # noqa: BLE001
        return f"No pude leer el P&L ahora 🙏 ({e})"
    t = d["totals"]
    lines = ["💰 *P&L agéntico · últimos 30 días*\n"]
    for c in d["channels"]:
        roas = f"{c['roas']}x" if c.get("roas") else "—"
        lines.append(f"• *{c['channel']}*: {_clp(c['revenue_clp'])} ingreso · "
                     f"{_clp(c['cost_clp'])} costo · ROAS {roas}")
    lines.append(f"\n*Total:* {_clp(t['revenue_clp'])} ingreso · neto {_clp(t['net_clp'])} · "
                 f"ROAS {t.get('blended_roas') or '—'}x")
    lines.append("\n_Cifras verificadas contra la caja real (piso atribuible)._")
    return "\n".join(lines)


def _winback() -> str:
    try:
        from autopilot import winback_report
        d = winback_report.report(days=120)
    except Exception as e:  # noqa: BLE001
        return f"No pude leer el win-back ahora 🙏 ({e})"
    t = d["totals"]
    out = ["📨 *Win-back · recuperación de pacientes*\n",
           f"• Enviados: *{t['enviados']}*",
           f"• Agendaron: *{t['agendaron']}* ({t['conv_pct']}%)",
           f"• Pagaron: *{t['pagaron']}*",
           f"• Ingreso real: *{_clp(t['ingreso'])}*"]
    # mejor día
    best = max((dd for dd in d.get("days", [])), key=lambda x: x["ingreso"], default=None)
    if best and best["ingreso"]:
        out.append(f"\nMejor día: {best['dia']} → {_clp(best['ingreso'])}")
    out.append("\n_Ingreso = caja real por paciente (como Meta Ads)._")
    return "\n".join(out)


def _director() -> str:
    try:
        from autopilot import director
        d = director.cabinet()
    except Exception as e:  # noqa: BLE001
        return f"No pude consultar al Director ahora 🙏 ({e})"
    g = d["gabinete"]
    op = g["operador"]
    b = op.get("budget", {})
    out = ["🎩 *El Director* (gabinete maestro)\n",
           f"Presupuesto: {b.get('label','—')}\n", "*Recomienda:*"]
    for r in op.get("recommendations", [])[:5]:
        emoji = {"apagar": "🔴", "prender": "🟢", "mantener ON": "✅",
                 "mantener OFF": "⚪", "esperar presupuesto": "💸"}.get(r["accion"], "•")
        out.append(f"{emoji} *{r['accion']}*: {r['switch']} (ahora {r['estado_actual']})")
    inv = g.get("inventor", [])
    if inv:
        out.append("\n💡 *Agentes nuevos que propone:*")
        for s in inv[:3]:
            out.append(f"• {s['nombre']}")
    out.append("\n_Propone; tú prendes/apagas en la Sala de Máquinas._")
    return "\n".join(out)


def _ads() -> str:
    try:
        from autopilot import impact
        d = impact.pnl(days=30)
        ads = next((c for c in d["channels"] if c["channel"] == "Meta Ads"), None)
    except Exception as e:  # noqa: BLE001
        return f"No pude leer Autopilot ahora 🙏 ({e})"
    if not ads:
        return "📣 *Autopilot Ads*: sin datos de campañas en el período."
    roas = f"{ads['roas']}x" if ads.get("roas") else "—"
    return ("📣 *Autopilot · publicidad (30 días)*\n\n"
            f"• Gasto: *{_clp(ads['cost_clp'])}*\n"
            f"• Ingreso atribuido: *{_clp(ads['revenue_clp'])}*\n"
            f"• Citas/atendidos: *{ads['bookings']}*\n"
            f"• ROAS: *{roas}* · neto {_clp(ads['net_clp'])}\n\n"
            "_Atribución parcial: parte llama al fijo y no se traza._")


def _reglas() -> str:
    try:
        from autopilot import optimizer
        d = optimizer.run_analysis()
    except Exception as e:  # noqa: BLE001
        return f"No pude leer el Optimizador ahora 🙏 ({e})"
    recs = d.get("recommendations", [])
    if not recs:
        return "🎯 *Optimizador*: todas las reglas se ven bien calibradas ahora."
    out = [f"🎯 *Optimizador* — {len(recs)} reglas a revisar:\n"]
    for r in recs[:6]:
        out.append(f"• *{r['policy']}*: {r['current']} → {r['proposed']}")
    out.append("\n_Propuestas con evidencia; no se aplican solas._")
    return "\n".join(out)


_ROUTES = [
    (("menu", "menú", "hola", "ayuda", "help", "inicio", "buenas"), _menu),
    (("plata", "pyl", "p&l", "pnl", "ingreso", "ganancia", "rendimiento"), _pnl),
    (("winback", "win-back", "recuper", "reenganch"), _winback),
    (("director", "gabinete", "maestro", "switches", "prender", "apagar"), _director),
    (("ads", "autopilot", "publicidad", "campaña", "campana", "meta"), _ads),
    (("reglas", "optimiz", "margen", "calibr", "politica", "política"), _reglas),
]


def respond(text: str) -> str:
    """Devuelve la respuesta del asistente Adkun para el texto del dueño."""
    t = (text or "").strip().lower()
    if not t:
        return _menu()
    for keys, fn in _ROUTES:
        if any(k in t for k in keys):
            try:
                return fn()
            except Exception as e:  # noqa: BLE001
                log.error("adkun_assistant %s: %s", fn.__name__, e)
                return "Tuve un problema generando ese reporte 🙏. Probá *menu*."
    return ("No te entendí 🤔. " + _menu())
