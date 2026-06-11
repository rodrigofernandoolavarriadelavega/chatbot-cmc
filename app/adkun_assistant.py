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
        "• *reglas* — políticas mal calibradas (Optimizador)\n"
        "• *caja* — cuadre Medilink×recepción + efectivo en caja + qué falta registrar\n\n"
        "🔀 *modos* — cambiar a modo CMC o paciente\n"
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


def _caja() -> str:
    from cuadre_caja import texto_cuadre
    return texto_cuadre()


_ROUTES = [
    (("menu", "menú", "hola", "ayuda", "help", "inicio", "buenas"), _menu),
    (("plata", "pyl", "p&l", "pnl", "ingreso", "ganancia", "rendimiento"), _pnl),
    (("winback", "win-back", "recuper", "reenganch"), _winback),
    (("director", "gabinete", "maestro", "switches", "prender", "apagar"), _director),
    (("ads", "autopilot", "publicidad", "campaña", "campana", "meta"), _ads),
    (("reglas", "optimiz", "margen", "calibr", "politica", "política"), _reglas),
    (("caja", "cuadre", "efectivo", "deposito", "depósito", "deposit"), _caja),
]


def respond(text: str) -> str:
    """Despacha un reporte del modo ADKUN para el texto del dueño."""
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


# ═════════════════════════════════════════════════════════════════════════════
# SWITCHER DE 3 MODOS — paciente / asistente CMC / asistente Adkun, en un número
# ═════════════════════════════════════════════════════════════════════════════
_VALID_MODES = ("adkun", "cmc", "paciente")


def _mode_table(conn) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS owner_assistant_mode (
        phone TEXT PRIMARY KEY, mode TEXT, updated_at TEXT DEFAULT (datetime('now')))""")


def get_mode(phone: str) -> str:
    try:
        from session import db as _conn
        with _conn() as c:
            _mode_table(c)
            r = c.execute("SELECT mode FROM owner_assistant_mode WHERE phone=?", (phone,)).fetchone()
        return r["mode"] if r and r["mode"] in _VALID_MODES else "adkun"
    except Exception:  # noqa: BLE001
        return "adkun"


def set_mode(phone: str, mode: str) -> None:
    if mode not in _VALID_MODES:
        return
    try:
        from session import db as _conn
        with _conn() as c:
            _mode_table(c)
            c.execute("""INSERT INTO owner_assistant_mode (phone, mode, updated_at)
                VALUES (?,?,datetime('now')) ON CONFLICT(phone) DO UPDATE SET
                mode=excluded.mode, updated_at=datetime('now')""", (phone, mode))
            c.commit()
    except Exception as e:  # noqa: BLE001
        log.warning("set_mode: %s", e)


def _modes_menu() -> str:
    return (
        "🔀 *Tus 3 modos* (escribe la palabra para cambiar):\n\n"
        "🧠 *adkun* — la capa agéntica (P&L, win-back, Director)\n"
        "🏥 *cmc* — operación de la clínica (agenda, demanda, actividad)\n"
        "🧪 *paciente* — usar el bot como un paciente normal (para probar)\n\n"
        "_Estás en un solo número; cambias de sombrero con una palabra._"
    )


# ── Modo CMC: operación de la clínica (data real de sessions.db) ──────────────
def _cmc_menu() -> str:
    return ("🏥 *Asistente CMC* — la clínica\n\nEscríbeme:\n"
            "• *agenda* — citas que agendó el bot (hoy/semana)\n"
            "• *demanda* — qué piden y no tenemos\n"
            "• *actividad* — conversaciones y registros\n\n"
            "_Cambia de modo: *adkun* · *paciente* · *modos*_")


def _cmc_agenda() -> str:
    try:
        from session import db as _conn
        with _conn() as c:
            hoy = c.execute("SELECT COUNT(*) n FROM citas_bot WHERE date(created_at)=date('now')").fetchone()["n"]
            sem = c.execute("SELECT COUNT(*) n FROM citas_bot WHERE created_at>=datetime('now','-7 days')").fetchone()["n"]
            rows = c.execute("""SELECT especialidad, COUNT(*) n FROM citas_bot
                WHERE created_at>=datetime('now','-7 days') GROUP BY especialidad
                ORDER BY n DESC LIMIT 6""").fetchall()
    except Exception as e:  # noqa: BLE001
        return f"No pude leer la agenda 🙏 ({e})"
    out = [f"📅 *Agenda (bot)*\n", f"• Hoy: *{hoy}* citas", f"• Últimos 7 días: *{sem}*"]
    if rows:
        out.append("\nPor especialidad (7 días):")
        for r in rows:
            out.append(f"  • {r['especialidad'] or '—'}: {r['n']}")
    return "\n".join(out)


def _cmc_demanda() -> str:
    try:
        from session import db as _conn
        with _conn() as c:
            rows = c.execute("""SELECT solicitud, COUNT(*) n FROM demanda_no_disponible
                WHERE created_at>=datetime('now','-30 days')
                GROUP BY solicitud ORDER BY n DESC LIMIT 8""").fetchall()
    except Exception as e:  # noqa: BLE001
        return f"No pude leer la demanda 🙏 ({e})"
    if not rows:
        return "🔎 *Demanda*: sin registros de demanda no satisfecha en 30 días."
    out = ["🔎 *Demanda no satisfecha (30 días)*\n"]
    for r in rows:
        out.append(f"• {r['solicitud'] or '—'}: *{r['n']}*")
    out.append("\n_Lo que piden y no ofrecemos = pistas para contratar/agregar._")
    return "\n".join(out)


def _cmc_actividad() -> str:
    try:
        from session import db as _conn
        with _conn() as c:
            convs = c.execute("SELECT COUNT(DISTINCT phone) n FROM messages WHERE date(ts)=date('now')").fetchone()["n"]
            regs = c.execute("""SELECT COUNT(*) n FROM contact_profiles
                WHERE date(updated_at)>=date('now','-7 days')""").fetchone()["n"]
    except Exception as e:  # noqa: BLE001
        return f"No pude leer la actividad 🙏 ({e})"
    return (f"📈 *Actividad*\n\n• Conversaciones hoy: *{convs}*\n"
            f"• Perfiles actualizados (7 días): *{regs}*")


_CMC_ROUTES = [
    (("menu", "menú", "hola", "ayuda", "inicio"), _cmc_menu),
    (("agenda", "cita", "hora"), _cmc_agenda),
    (("demanda", "piden", "falta"), _cmc_demanda),
    (("actividad", "conversa", "registro", "movimiento"), _cmc_actividad),
]


def _cmc_dispatch(t: str) -> str:
    for keys, fn in _CMC_ROUTES:
        if any(k in t for k in keys):
            try:
                return fn()
            except Exception as e:  # noqa: BLE001
                log.error("cmc %s: %s", fn.__name__, e)
                return "Problema con ese reporte 🙏. Probá *menu*."
    return "No te entendí 🤔.\n\n" + _cmc_menu()


def route(phone: str, text: str) -> tuple[bool, str | None]:
    """Router maestro del dueño. Devuelve (handled, reply):
      handled=True  → respondemos nosotros (modo adkun o cmc, o cambio de modo).
      handled=False → caer al flujo de PACIENTE normal (modo paciente).
    Los comandos de modo se atienden SIEMPRE (aun en modo paciente, para poder volver)."""
    t = (text or "").strip().lower()

    # Comandos de cambio de modo (prioridad máxima)
    if t in ("adkun", "modo adkun"):
        set_mode(phone, "adkun"); return True, "🧠 *Modo Adkun.*\n\n" + _menu()
    if t in ("cmc", "modo cmc", "centro", "clinica", "clínica"):
        set_mode(phone, "cmc"); return True, _cmc_menu()
    if t in ("paciente", "modo paciente", "probar bot"):
        set_mode(phone, "paciente")
        return True, ("🧪 *Modo paciente activado.* Ahora el bot te atiende como un paciente "
                      "normal (para probarlo). Escribe *adkun*, *cmc* o *modos* para volver.")
    if t in ("modos", "cambiar modo", "menu principal", "switch"):
        return True, _modes_menu()

    mode = get_mode(phone)
    if mode == "paciente":
        return False, None            # cae al flujo de pacientes
    if mode == "cmc":
        return True, _cmc_dispatch(t)
    return True, respond(text)        # modo adkun (default)
