"""Avisos de Campañas Meta al dueño, sin entrar al panel.

  * Resumen semanal (lunes 08:30 CLT): últimos 7 días vs los 7 anteriores.
  * Alertas diarias (09:05 CLT), sin repetir la misma más de 1 vez cada 48 h.

Los números salen de `campanas_meta_routes.panel_data` (la misma fuente del
panel `/alma/campanas-meta`); acá no se recalcula nada. Solo agregados: el
mensaje no lleva nombres ni teléfonos de pacientes.

Canal: Telegram (`alertas_oob.enviar_telegram`), el mismo que usan las
alertas al dueño del repo (conciliación de caja, templates saltados). No
depende de la ventana de 24 h de WhatsApp. Si Telegram no entrega, se cae a
WhatsApp al ADMIN_ALERT_PHONE SOLO si su ventana de 24 h está abierta
(`jobs._admin_window_open`); si tampoco, queda en log + conversation_events
(`meta_alertas_sin_canal`) y las alertas no se marcan como enviadas, así que
se reintentan al día siguiente. No hay plantilla aprobada para esto.

Apagado: META_ALERTAS_ACTIVE=false en el .env (config.META_ALERTAS_ACTIVE).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

log = logging.getLogger("bot")

_CL = ZoneInfo("America/Santiago")
PANEL_URL = "https://agentecmc.cl/alma/campanas-meta"

# "No repetir más de 1 vez cada 48 h". El cron diario no cae al segundo
# exacto: con 48 h estrictas, 09:05:02 vs 09:05:00 del día 1 saltaría al día 4.
# 1 h de margen para que el ritmo sea "día por medio".
DEDUP_HORAS = 47
MAX_CAMPANAS = 6             # el mensaje debe caber en Telegram/WhatsApp (4.000 caracteres)
DIAS_SIN_CITAS = 14          # ventana de la alerta (a): gasto sin citas
_MESES = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]


def _activo() -> bool:
    import config
    return bool(getattr(config, "META_ALERTAS_ACTIVE", True))


# ── Formato ─────────────────────────────────────────────────────────────────

def _clp(n) -> str:
    n = int(round(n or 0))
    s = f"{abs(n):,}".replace(",", ".")
    return f"-${s}" if n < 0 else f"${s}"


def _signed(n) -> str:
    n = int(round(n or 0))
    return ("+" if n > 0 else "") + _clp(n)


def _x(v) -> str:
    return "—" if v is None else f"{v:.1f}".replace(".", ",") + "x"


def _dia(d: date) -> str:
    return f"{d.day} {_MESES[d.month - 1]}"


# ── Resumen semanal ─────────────────────────────────────────────────────────

def _kpis(desde: date, hasta: date, canal: str) -> dict:
    from campanas_meta_routes import panel_data
    return panel_data(desde.isoformat(), hasta.isoformat(), canal=canal)


def _ant(v, f=lambda x: str(x)) -> str:
    return f"(ant. {f(v)})"


def construir_resumen(hoy: date | None = None) -> str:
    """Texto del resumen semanal. Semana = 7 días cerrados hasta ayer."""
    hoy = hoy or datetime.now(_CL).date()
    h1 = hoy - timedelta(days=1)
    d1 = h1 - timedelta(days=6)
    h0 = d1 - timedelta(days=1)
    d0 = h0 - timedelta(days=6)

    cur = _kpis(d1, h1, "meta")
    prev = _kpis(d0, h0, "meta")
    web_c = _kpis(d1, h1, "web")
    web_p = _kpis(d0, h0, "web")

    L = [f"*Campañas Meta · semana {_dia(d1)} al {_dia(h1)}*",
         f"Entre paréntesis, la semana anterior ({_dia(d0)} al {_dia(h0)}).", ""]

    k, p = cur["kpis"], prev["kpis"]
    L += ["*Total Meta*",
          f"Gasto {_clp(k['gasto'])} {_ant(p['gasto'], _clp)}",
          f"Conversaciones {k['conversaciones']} {_ant(p['conversaciones'])}",
          f"Citas {k['citas']} {_ant(p['citas'])}",
          f"Venta {_clp(k['venta'])} {_ant(p['venta'], _clp)}",
          f"Para el centro {_clp(k['centro'])} {_ant(p['centro'], _clp)}",
          f"Resultado (centro − gasto) {_signed(k['resultado'])} {_ant(p['resultado'], _signed)}",
          f"Retorno de venta {_x(k['retorno'])} {_ant(p['retorno'], _x)} · "
          f"retorno centro {_x(k['retorno_centro'])} {_ant(p['retorno_centro'], _x)}", ""]

    pc = {c["campaign_id"]: c for c in prev["campanas"]}
    camps = [c for c in cur["campanas"]
             if c["gasto"] or c["citas"] or (pc.get(c["campaign_id"]) or {}).get("gasto")]
    camps.sort(key=lambda c: (c["gasto"], c["citas"]), reverse=True)
    for c in camps[:MAX_CAMPANAS]:
        q = pc.get(c["campaign_id"]) or {"gasto": 0, "conversaciones": 0, "citas": 0, "venta": 0,
                                         "centro": 0, "resultado": 0}
        L += [f"*{c['campana'] or 'Sin campaña'}*",
              f"Gasto {_clp(c['gasto'])} {_ant(q['gasto'], _clp)} · conv. {c['conversaciones']} "
              f"{_ant(q['conversaciones'])} · citas {c['citas']} {_ant(q['citas'])}",
              f"Venta {_clp(c['venta'])} {_ant(q['venta'], _clp)} · centro {_clp(c['centro'])} "
              f"{_ant(q['centro'], _clp)}",
              f"Resultado {_signed(c['resultado'])} {_ant(q['resultado'], _signed)} · "
              f"retorno {_x(c['retorno'])} · centro {_x(c['retorno_centro'])}", ""]

    if len(camps) > MAX_CAMPANAS:
        L += [f"…y {len(camps) - MAX_CAMPANAS} campaña(s) más en el panel.", ""]

    ads = [a for a in cur["anuncios"] if a["canal"] == "meta" and a["gasto"] > 0]
    if ads:
        ads.sort(key=lambda a: a["resultado"], reverse=True)

        def _lin(a):
            return (f"{a['anuncio'] or a['ad_id']}: gastó {_clp(a['gasto'])}, {a['citas']} cita(s), "
                    f"resultado {_signed(a['resultado'])}"
                    + (" (pocas citas, cifra frágil)" if a.get("muestra_chica") else ""))
        L.append(f"*Mejor anuncio* · {_lin(ads[0])}")
        if len(ads) > 1:
            L.append(f"*Peor anuncio* · {_lin(ads[-1])}")
        L.append("")

    wk, wp = web_c["kpis"], web_p["kpis"]
    L += ["*Página web*",
          f"Personas {wk['personas']} {_ant(wp['personas'])} · citas {wk['citas']} {_ant(wp['citas'])} · "
          f"venta {_clp(wk['venta'])} {_ant(wp['venta'], _clp)}", "",
          f"Detalle: {PANEL_URL}"]
    return "\n".join(L)


# ── Alertas ─────────────────────────────────────────────────────────────────

def _gasto_dia_cero(c, hoy: date) -> dict | None:
    """(b) Gasto total de la cuenta en $0 ayer, habiendo gastado la semana
    anterior a ayer. Usa meta_insights_diario (desglose total)."""
    ayer = hoy - timedelta(days=1)
    ant0, ant1 = ayer - timedelta(days=7), ayer - timedelta(days=1)

    def _suma(a, b):
        r = c.execute("SELECT COALESCE(SUM(spend),0), COUNT(*) FROM meta_insights_diario "
                      "WHERE desglose='total' AND fecha >= ? AND fecha <= ?",
                      (a.isoformat(), b.isoformat())).fetchone()
        return r[0] or 0, r[1] or 0
    g_ayer, filas_ayer = _suma(ayer, ayer)
    g_ant, _ = _suma(ant0, ant1)
    if g_ayer == 0 and g_ant > 0:
        return {"semana_previa": g_ant, "sin_foto": filas_ayer == 0}
    return None


def _por_llamar(c, hoy: date) -> tuple[int, int]:
    """(d) (para hoy, vencidos) con la misma regla de la tarjeta del kanban."""
    import campanas_meta_routes as cm
    hoy_s = hoy.isoformat()
    n_hoy = n_venc = 0
    for r in c.execute("SELECT clave, estado, nota, proximo, updated_at FROM campanas_seguimiento "
                       "WHERE proximo IS NOT NULL AND proximo != ''"):
        g = cm._gestion_tarjeta(dict(r), hoy_s)
        if g and g["alerta"] == "hoy":
            n_hoy += 1
        elif g and g["alerta"] == "vencido":
            n_venc += 1
    return n_hoy, n_venc


def evaluar_alertas(hoy: date | None = None) -> list[tuple[str, str]]:
    """Todas las alertas vigentes como [(clave_dedup, texto)], sin filtrar por
    antigüedad del último aviso (eso lo hace `job_meta_alertas_diario`)."""
    import campanas_meta_routes as cm
    from session import db
    hoy = hoy or datetime.now(_CL).date()
    ayer = hoy - timedelta(days=1)
    out: list[tuple[str, str]] = []

    # (a) y (c) salen de las alertas del panel sobre 14 días: mismos umbrales
    # (gasto >= UMBRAL_GASTO_SIN_CITAS sin citas; frecuencia > UMBRAL_FRECUENCIA).
    pd = cm.panel_data((ayer - timedelta(days=DIAS_SIN_CITAS - 1)).isoformat(), ayer.isoformat(),
                       canal="meta")
    activos = {a["ad_id"]: a["activo"] for a in pd["anuncios"]}
    for a in pd["alertas"]:
        if a["tipo"] == "sin_citas" and activos.get(a["ad_id"]):
            out.append((f"sin_citas:{a['ad_id']}",
                        f"El anuncio «{a['anuncio'] or a['ad_id']}» ({a['campana']}) gastó "
                        f"{_clp(a['gasto'])} en {DIAS_SIN_CITAS} días y no trajo ninguna cita."))
        elif a["tipo"] == "saturado" and activos.get(a["ad_id"]):
            out.append((f"saturado:{a['ad_id']}",
                        f"El anuncio «{a['anuncio'] or a['ad_id']}» ({a['campana']}) está activo con "
                        f"frecuencia {str(a['frecuencia']).replace('.', ',')} (como mínimo; sobre "
                        f"{int(cm.UMBRAL_FRECUENCIA)} la gente lo ve demasiado). Conviene renovar el creativo."))

    with db() as c:
        cm._ensure_insights(c)
        z = _gasto_dia_cero(c, hoy)
        if z:
            extra = (" No hay registro de Meta de ayer: también puede ser falla del respaldo diario."
                     if z["sin_foto"] else "")
            out.append(("gasto_cero",
                        f"La cuenta de Meta no gastó nada ayer ({_dia(ayer)}) y la semana anterior gastó "
                        f"{_clp(z['semana_previa'])}. Revisa si hay anuncios detenidos, pago rechazado "
                        f"o cuenta restringida.{extra}"))
        n_hoy, n_venc = _por_llamar(c, hoy)
    if n_hoy or n_venc:
        partes = []
        if n_venc:
            partes.append(f"{n_venc} vencida(s)")
        if n_hoy:
            partes.append(f"{n_hoy} para hoy")
        out.append(("por_llamar",
                    f"Hay {n_hoy + n_venc} persona(s) de Campañas Meta por volver a llamar "
                    f"({', '.join(partes)}). Filtro «Por llamar» en el kanban."))
    return out


# ── Estado de envío (dedup 48 h) ────────────────────────────────────────────

def _ensure_estado(c) -> None:
    c.execute("CREATE TABLE IF NOT EXISTS meta_alertas_estado ("
              "clave TEXT PRIMARY KEY, ts_epoch INTEGER NOT NULL)")


def filtrar_recientes(alertas: list[tuple[str, str]], ahora_epoch: int) -> list[tuple[str, str]]:
    from session import db
    corte = ahora_epoch - DEDUP_HORAS * 3600
    with db() as c:
        _ensure_estado(c)
        vistas = {r[0] for r in c.execute("SELECT clave FROM meta_alertas_estado WHERE ts_epoch > ?",
                                          (corte,))}
    return [(k, t) for k, t in alertas if k not in vistas]


def marcar_enviadas(claves: list[str], ahora_epoch: int) -> None:
    from session import db
    with db() as c:
        _ensure_estado(c)
        for k in claves:
            c.execute("INSERT INTO meta_alertas_estado (clave, ts_epoch) VALUES (?,?) "
                      "ON CONFLICT(clave) DO UPDATE SET ts_epoch=excluded.ts_epoch", (k, ahora_epoch))
        # Poda: lo más viejo que una semana ya no sirve para deduplicar.
        c.execute("DELETE FROM meta_alertas_estado WHERE ts_epoch < ?", (ahora_epoch - 7 * 86400,))
        c.commit()


# ── Envío ───────────────────────────────────────────────────────────────────

async def enviar_al_dueno(texto: str) -> str | None:
    """Telegram primero; WhatsApp solo con ventana de 24 h abierta. Devuelve el
    canal usado o None (y deja rastro) si no hubo cómo entregar."""
    from alertas_oob import enviar_telegram
    if await enviar_telegram(texto):
        return "telegram"
    try:
        import config
        from jobs import _admin_window_open
        from messaging import send_whatsapp
        if config.ADMIN_ALERT_PHONE and _admin_window_open():
            if await send_whatsapp(config.ADMIN_ALERT_PHONE, texto[:3900]):
                return "whatsapp"
    except Exception as e:
        log.warning("meta_alertas: falló el respaldo WhatsApp: %s", e)
    log.warning("meta_alertas: sin canal (Telegram no entregó y la ventana de WhatsApp está cerrada)")
    try:
        from session import log_event
        log_event("meta_alertas", "meta_alertas_sin_canal", {"largo": len(texto)})
    except Exception:
        pass
    return None


# ── Jobs ────────────────────────────────────────────────────────────────────

async def job_meta_resumen_semanal() -> None:
    """Lunes 08:30 CLT."""
    if not _activo():
        log.info("meta_resumen_semanal: apagado (META_ALERTAS_ACTIVE=false)")
        return
    try:
        texto = await asyncio.to_thread(construir_resumen)
    except Exception as e:
        log.error("meta_resumen_semanal: no se pudo armar: %s", e, exc_info=True)
        return
    canal = await enviar_al_dueno(texto)
    log.info("meta_resumen_semanal: enviado por %s", canal)


async def job_meta_alertas_diario() -> None:
    """Todos los días 09:05 CLT."""
    if not _activo():
        log.info("meta_alertas_diario: apagado (META_ALERTAS_ACTIVE=false)")
        return
    try:
        vigentes = await asyncio.to_thread(evaluar_alertas)
        ahora = int(datetime.now(_CL).timestamp())
        nuevas = await asyncio.to_thread(filtrar_recientes, vigentes, ahora)
    except Exception as e:
        log.error("meta_alertas_diario: no se pudo evaluar: %s", e, exc_info=True)
        return
    if not nuevas:
        log.info("meta_alertas_diario: %d vigente(s), ninguna nueva", len(vigentes))
        return
    texto = (f"*Alertas Campañas Meta* · {len(nuevas)}\n\n"
             + "\n\n".join(f"• {t}" for _, t in nuevas) + f"\n\nPanel: {PANEL_URL}")
    canal = await enviar_al_dueno(texto)
    if canal:
        await asyncio.to_thread(marcar_enviadas, [k for k, _ in nuevas], ahora)
    log.info("meta_alertas_diario: %d alerta(s) por %s", len(nuevas), canal)
