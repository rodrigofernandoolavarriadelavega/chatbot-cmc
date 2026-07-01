"""Consola de dueño en Telegram — comandos (gratis) + IA (Haiku) como fallback.

Solo responde al chat del dueño (TELEGRAM_ALERT_CHAT_ID); ignora a cualquier otro.
El webhook vive en main.py (POST /telegram/webhook) y delega acá.

Comandos deterministas (sin IA, $0):
  /ayuda /hoy /mes /comparador
Cualquier otro texto → IA Haiku con un snapshot de la caja como contexto (~$0,002).

Fuente de datos: bi_pagos_caja (caja real Medilink), vía bi_sync._bi_conn().
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger("bot")
_TG_SEND = "https://api.telegram.org/bot{token}/sendMessage"
_CLT = ZoneInfo("America/Santiago")
_HAIKU = "claude-haiku-4-5-20251001"

# Dashboards (para botones)
_URL_MENSUAL = "https://agentecmc.cl/bi/mensual"
_URL_COMPARADOR_BASE = "https://agentecmc.cl/cmc/comparador?token="


def _token() -> str:
    return os.getenv("TELEGRAM_ALERT_TOKEN", "").strip()


def _owner_chat() -> str:
    return os.getenv("TELEGRAM_ALERT_CHAT_ID", "").strip()


def _admin_token() -> str:
    """Token admin vigente (desde env; tolera rotación). Para llamadas internas
    y para los botones a dashboards."""
    return os.getenv("ADMIN_TOKEN", "").strip() or os.getenv("OLACORE_TOKEN", "").strip()


def _clp(n) -> str:
    return "$" + format(int(n or 0), ",d").replace(",", ".")


# ── Datos (bi_pagos_caja) ────────────────────────────────────────────────────

def _bounds_mes(ref: date) -> tuple[str, str, str, str]:
    """Devuelve (inicio_mes, inicio_mes_sig, inicio_mes_prev, inicio_mes_actual)."""
    ini = ref.replace(day=1)
    ini_sig = (ini + timedelta(days=32)).replace(day=1)
    ini_prev = (ini - timedelta(days=1)).replace(day=1)
    return ini.isoformat(), ini_sig.isoformat(), ini_prev.isoformat(), ini.isoformat()


def _caja(d1: str, d2: str) -> tuple[int, int]:
    import bi_sync
    c = bi_sync._bi_conn()
    r = c.execute("SELECT COALESCE(SUM(monto),0), COUNT(*) FROM bi_pagos_caja "
                  "WHERE fecha>=? AND fecha<?", (d1, d2)).fetchone()
    return int(r[0] or 0), int(r[1] or 0)


def _nombres() -> dict:
    import bi_sync
    c = bi_sync._bi_conn()
    return {r[0]: r[1] for r in c.execute("SELECT id_medilink, nombre FROM equipo_cmc").fetchall()}


def _por_prof(d1: str, d2: str) -> list[tuple[int, int, int]]:
    import bi_sync
    c = bi_sync._bi_conn()
    return [(idp, int(s or 0), int(n or 0)) for idp, s, n in c.execute(
        "SELECT id_profesional, SUM(monto), COUNT(*) FROM bi_pagos_caja "
        "WHERE fecha>=? AND fecha<? GROUP BY id_profesional ORDER BY 2 DESC", (d1, d2)).fetchall()]


# ── Reportes (texto Markdown + botones) ──────────────────────────────────────

def reporte_hoy() -> tuple[str, list]:
    hoy = datetime.now(_CLT).date()
    d1 = hoy.isoformat(); d2 = (hoy + timedelta(days=1)).isoformat()
    monto, n = _caja(d1, d2)
    nom = _nombres()
    top = _por_prof(d1, d2)[:5]
    txt = ["📅 *Caja de hoy* — %s" % hoy.strftime("%d/%m"),
           "", "💰 Vendido: *%s*" % _clp(monto), "🧾 Pagos: *%d*" % n]
    if top:
        txt.append("")
        txt.append("*Por profesional*")
        for idp, s, cnt in top:
            txt.append("• %s — %s (%d)" % (nom.get(idp, "id %s" % idp), _clp(s), cnt))
    else:
        txt.append("\n_Aún no hay pagos cargados hoy._")
    return "\n".join(txt), [[{"text": "📊 Abrir DB Mensual", "url": _URL_MENSUAL}]]


def reporte_mes() -> tuple[str, list]:
    hoy = datetime.now(_CLT).date()
    ini, ini_sig, ini_prev, _ = _bounds_mes(hoy)
    cur, ncur = _caja(ini, ini_sig)
    prev, _np = _caja(ini_prev, ini)
    delta = cur - prev
    pct = (delta / prev * 100) if prev else 0
    nom = _nombres()
    top = _por_prof(ini, ini_sig)[:5]
    signo = "+" if delta >= 0 else "−"
    txt = ["📊 *DB Mensual* — %s" % hoy.strftime("%B %Y"),
           "_Caja real (Medilink)_", "",
           "💰 Total mes: *%s*" % _clp(cur),
           "🧾 Pagos: *%d*" % ncur,
           "📈 vs mes anterior (%s): *%s%s* (%s%.1f%%)" % (_clp(prev), signo, _clp(abs(delta)), signo, abs(pct)),
           "", "*Top profesionales*"]
    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
    for i, (idp, s, cnt) in enumerate(top):
        txt.append("%s %s — *%s* (%d)" % (medals[i], nom.get(idp, "id %s" % idp), _clp(s), cnt))
    return "\n".join(txt), [[{"text": "📊 Abrir DB Mensual", "url": _URL_MENSUAL}]]


def reporte_comparador() -> tuple[str, list]:
    hoy = datetime.now(_CLT).date()
    ini, ini_sig, ini_prev, _ = _bounds_mes(hoy)
    nom = _nombres()
    pj = {idp: s for idp, s, _ in _por_prof(ini, ini_sig)}
    pm = {idp: s for idp, s, _ in _por_prof(ini_prev, ini)}
    deltas = []
    for idp in set(list(pj) + list(pm)):
        deltas.append((nom.get(idp, "id %s" % idp), pj.get(idp, 0) - pm.get(idp, 0)))
    deltas.sort(key=lambda x: x[1])
    txt = ["📊 *Comparador* — mes actual vs anterior", "", "🟢 *Subieron*"]
    for nm, d in [x for x in deltas[::-1] if x[1] > 0][:3]:
        txt.append("▲ %s  +%s" % (nm, _clp(d)))
    txt.append("")
    txt.append("🔴 *Bajaron*")
    for nm, d in [x for x in deltas if x[1] < 0][:3]:
        txt.append("▼ %s  −%s" % (nm, _clp(abs(d))))
    return "\n".join(txt), [[{"text": "📊 Abrir Comparador", "url": _URL_COMPARADOR_BASE + _admin_token()}]]


async def reporte_agenda() -> tuple[str, list]:
    """Agenda del día por profesional: cupos totales, ocupados y libres.
    Reusa /api/panel-dia/operativo (rate-limit-safe: capacidad por percentil
    histórico + caché nocturna; NO hace fan-out a Medilink)."""
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get("http://127.0.0.1:8001/api/panel-dia/operativo",
                            params={"token": _admin_token(), "auto": 1})
        data = r.json()
    except Exception as e:  # noqa: BLE001
        return "No pude leer la agenda del día (%s)." % str(e)[:80], []
    profs = data.get("profesionales", []) or []
    activos = [p for p in profs if (p.get("cap") or p.get("ocup") or p.get("agenda"))]
    if not activos:
        return "📋 Hoy no hay profesionales con agenda cargada.", []
    activos.sort(key=lambda p: int(p.get("ocup") or 0), reverse=True)
    lines = ["📋 *Agenda de hoy* — %s" % data.get("fecha", ""), ""]
    tcap = tocup = 0
    for p in activos:
        cap = int(p.get("cap") or 0); ocup = int(p.get("ocup") or 0)
        libre = max(0, cap - ocup)
        tcap += cap; tocup += ocup
        ic = "🟢" if libre > 0 else "🔴"
        lines.append("%s *%s* — %d cupos · %d ocup · *%d libres*"
                     % (ic, p.get("nombre", "?"), cap, ocup, libre))
    lines += ["", "Σ *%d cupos* · %d ocupados · *%d libres*" % (tcap, tocup, max(0, tcap - tocup))]
    return "\n".join(lines), [[{"text": "📅 Abrir Panel del Día",
                                "url": "https://agentecmc.cl/alma/panel-dia?token=" + _admin_token()}]]


def reporte_templates() -> tuple[str, list]:
    """Salud de entrega de templates de hoy: global (message_statuses) + por
    template (template_sends × statuses, se puebla desde el nuevo tracking)."""
    from session import db
    hoy = datetime.now(_CLT).date().isoformat()
    lines = ["📤 *Templates de hoy* — %s" % datetime.now(_CLT).strftime("%d/%m")]
    with db() as c:
        st = {s: n for s, n in c.execute(
            "SELECT status, COUNT(*) FROM message_statuses WHERE substr(ts,1,10)=? GROUP BY status",
            (hoy,)).fetchall()}
        deliv = st.get("delivered", 0) + st.get("read", 0)
        lines += ["", "*Entrega global*",
                  "✅ Entregado/leído: *%d*" % deliv,
                  "📤 Enviado (sin confirmar): %d" % st.get("sent", 0),
                  "❌ Falló: *%d*" % st.get("failed", 0)]
        errs = c.execute(
            "SELECT error_code, COUNT(*) FROM message_statuses WHERE substr(ts,1,10)=? "
            "AND status='failed' GROUP BY error_code ORDER BY 2 DESC", (hoy,)).fetchall()
        if errs:
            lines.append("Errores: " + " · ".join("%s×%d" % (e or "?", n) for e, n in errs))
        try:
            rows = c.execute(
                "SELECT t.template_name, COUNT(*), "
                "SUM(CASE WHEN s.status IN ('delivered','read') THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN s.status='failed' THEN 1 ELSE 0 END) "
                "FROM template_sends t LEFT JOIN message_statuses s ON t.wamid=s.wamid "
                "WHERE substr(t.ts,1,10)=? GROUP BY t.template_name ORDER BY 2 DESC",
                (hoy,)).fetchall()
        except Exception:  # noqa: BLE001 — tabla aún no existe hasta el 1er envío
            rows = []
        if rows:
            lines += ["", "*Por template*"]
            for name, tot, ok, fail in rows:
                lines.append("• %s — %d env · %d✅ · %d❌" % (name, tot, ok or 0, fail or 0))
        else:
            lines += ["", "_Desglose por template: se activa desde el próximo envío (tracking nuevo)._"]
    return "\n".join(lines), []


def menu() -> tuple[str, list]:
    txt = ("🤖 *Consola CMC* — tu asistente de dueño\n\n"
           "*Comandos* (instantáneos, gratis):\n"
           "• /agenda — cupos por profesional hoy (totales/ocupados/libres)\n"
           "• /hoy — caja del día\n"
           "• /mes — DB mensual + top profesionales\n"
           "• /comparador — variación vs mes anterior\n"
           "• /templates — entrega de templates de hoy\n"
           "• /ayuda — este menú\n\n"
           "También puedes *escribirme en tus palabras* "
           "(ej: _¿cuánto vendió Olavarría este mes?_) y te respondo.")
    return txt, []


def _snapshot() -> str:
    """Contexto compacto para la IA: caja hoy + mes + por profesional + comparación."""
    hoy = datetime.now(_CLT).date()
    ini, ini_sig, ini_prev, _ = _bounds_mes(hoy)
    nom = _nombres()
    cur, ncur = _caja(ini, ini_sig)
    prev, _n = _caja(ini_prev, ini)
    choy, nhoy = _caja(hoy.isoformat(), (hoy + timedelta(days=1)).isoformat())
    lines = ["FECHA: %s" % hoy.isoformat(),
             "CAJA HOY: %s en %d pagos" % (_clp(choy), nhoy),
             "MES ACTUAL: %s en %d pagos (mes anterior: %s)" % (_clp(cur), ncur, _clp(prev)),
             "POR PROFESIONAL (mes actual):"]
    for idp, s, n in _por_prof(ini, ini_sig):
        lines.append("  - %s: %s (%d pagos)" % (nom.get(idp, "id %s" % idp), _clp(s), n))
    return "\n".join(lines)


async def _ai_answer(pregunta: str) -> str:
    """Responde lenguaje libre con Haiku, usando el snapshot de caja como contexto."""
    try:
        from claude_helper import _claude_create
        snap = _snapshot()
        system = (
            "Eres el asistente de dueño del Centro Médico Carampangue (CMC). "
            "Respondes CORTO y directo, en español de Chile, formal. Montos en pesos "
            "chilenos. Usa SOLO los datos del CONTEXTO; si te preguntan algo que no "
            "está ahí, dilo claramente y sugiere /hoy, /mes o /comparador. Puedes usar "
            "*negrita* de Telegram.\n\nCONTEXTO:\n" + snap)
        resp = await _claude_create(
            model=_HAIKU, max_tokens=400,
            system=system,
            messages=[{"role": "user", "content": pregunta[:500]}])
        return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip() \
            or "No pude generar respuesta. Prueba /hoy, /mes o /comparador."
    except Exception as e:  # noqa: BLE001
        log.warning("telegram IA falló: %s", e)
        return "No pude responder ahora (IA caída o sin saldo). Usa /hoy, /mes o /comparador."


async def _send(text: str, buttons: list | None = None) -> bool:
    token = _token(); chat = _owner_chat()
    if not token or not chat:
        return False
    payload = {"chat_id": chat, "text": text[:4000], "disable_web_page_preview": True}
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": buttons}
    url = _TG_SEND.format(token=token)
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(url, json={**payload, "parse_mode": "Markdown"})
            if r.status_code == 200:
                return True
            await c.post(url, json=payload)  # fallback texto plano
            return True
    except Exception as e:  # noqa: BLE001
        log.warning("telegram_console _send: %s", e)
        return False


_COMANDOS = {
    "ayuda": menu, "start": menu, "menu": menu, "help": menu,
    "agenda": reporte_agenda, "cupos": reporte_agenda,
    "hoy": reporte_hoy, "caja": reporte_hoy,
    "mes": reporte_mes, "mensual": reporte_mes,
    "comparador": reporte_comparador, "comparacion": reporte_comparador,
    "templates": reporte_templates, "template": reporte_templates,
}


async def handle_update(update: dict) -> None:
    """Procesa un update de Telegram. Solo responde al dueño."""
    try:
        msg = update.get("message") or update.get("edited_message") or {}
        chat_id = str((msg.get("chat") or {}).get("id", ""))
        text = (msg.get("text") or "").strip()
        if not text:
            return
        if chat_id != _owner_chat():
            log.info("telegram_console: ignorado chat ajeno %s", chat_id)
            return
        cmd = text.lower().lstrip("/").split()[0] if text else ""
        fn = _COMANDOS.get(cmd)
        if fn is not None:
            res = fn()
            if asyncio.iscoroutine(res):
                res = await res
            txt, btns = res
            await _send(txt, btns)
        else:
            await _send(await _ai_answer(text))
    except Exception as e:  # noqa: BLE001
        log.error("telegram_console.handle_update falló: %s", e)
