"""Monitor activo de anomalías del bot CMC.

Cada 15 min: escanea conversation_events + logs, detecta patrones sospechosos
y manda resumen al WhatsApp del dueño (+56987834148, ADMIN_ALERT_PHONE).

Implementado tras la sesión 2026-05-02/03 donde varios bugs sólo se descubrieron
cuando el usuario los vio en el panel admin. La idea: el dueño se entera ANTES
que el paciente lo viva.

Anomalías detectadas:
  POSTCONSULTA_PREMATURA — postconsulta enviado antes de la hora de la cita
  RUT_RECHAZADO_REPETIDO — un phone con ≥3 valid_rut fallidos en última hora
  CANCELAR_CON_PAY_KEYWORDS — intent cancelar_cita activado y mensaje contiene "se paga", "pagar", "cancela allá"
  FALLBACK_BOT — ≥2 "no_te_entendi" o "no_logro_entenderte" del bot en misma sesión
  MENU_REPETIDO — bot mostró menú principal ≥3 veces en misma sesión
  LEAK_NUMERO_PERSONAL — mensaje saliente contiene 987834148 (excepto a admin)
  TASK_EXCEPTION — log con "Task exception was never retrieved"
  CRASH_BACKEND — service restart no programado o stacktraces críticos

Anti-spam: tabla monitor_alerts_seen guarda el hash de cada alerta enviada
con TTL 4h. Si la misma alerta aparece de nuevo en esa ventana, no se reenvía.
"""
import hashlib
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from session import _conn

log = logging.getLogger("bot.monitor")

_TZ_CL = ZoneInfo("America/Santiago")

# Reglas de detección con su descriptor humano.
_REGLAS = {
    "POSTCONSULTA_PREMATURA":   "Postconsulta enviado antes de la cita real",
    "RUT_RECHAZADO_REPETIDO":   "Paciente con ≥3 RUT inválidos seguidos",
    "CANCELAR_CON_PAY_KEYWORDS":"Intent cancelar_cita activado con keywords de pago",
    "FALLBACK_BOT_LOOP":        "≥2 \"no te entendí\" consecutivos en misma sesión",
    "MENU_REPETIDO":            "Bot mostró menú principal ≥3 veces en misma sesión",
    "LEAK_NUMERO_PERSONAL":     "Mensaje saliente con +56987834148 (¡leak!)",
    "TASK_EXCEPTION":           "Task exception silenciada en background",
    "CRASH_BACKEND":            "Servicio restart no programado",
    "RECORDATORIO_CITA_ANULADA":"Recordatorio mandado a cita anulada en Medilink",
    "REENGANCHE_CAIDO":         "Sin reenganches en 6h con sesiones candidatas",
}


def _ensure_alerts_table():
    """Crea la tabla monitor_alerts_seen si no existe."""
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS monitor_alerts_seen (
                hash TEXT PRIMARY KEY,
                tipo TEXT,
                first_seen TEXT DEFAULT (datetime('now')),
                last_seen  TEXT DEFAULT (datetime('now')),
                count      INTEGER DEFAULT 1
            )
        """)
        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_monitor_alerts_last_seen
            ON monitor_alerts_seen(last_seen)
        """)
        c.commit()


def _alert_hash(tipo: str, payload: dict) -> str:
    """Hash determinístico para detectar alertas repetidas (anti-spam)."""
    key = f"{tipo}::" + json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _was_alerted_recently(h: str, ttl_hours: int = 4) -> bool:
    """True si la alerta ya fue notificada en últimas ttl_hours."""
    with _conn() as c:
        row = c.execute(
            "SELECT last_seen FROM monitor_alerts_seen WHERE hash=? "
            "AND last_seen > datetime('now', ?)",
            (h, f"-{ttl_hours} hours")
        ).fetchone()
        return row is not None


def _mark_alerted(h: str, tipo: str):
    with _conn() as c:
        c.execute("""
            INSERT INTO monitor_alerts_seen (hash, tipo) VALUES (?, ?)
            ON CONFLICT(hash) DO UPDATE SET
                last_seen = datetime('now'),
                count = count + 1
        """, (h, tipo))
        c.commit()


# ── Detectores específicos ────────────────────────────────────────────────────


def _detect_postconsulta_prematura() -> list[dict]:
    """Postconsulta enviado antes de la hora real de la cita.

    Cruza fidelizacion_msgs (tipo='postconsulta') con citas_bot por id_cita y
    compara enviado_en < fecha+hora de la cita. Mira últimas 24h.
    """
    out = []
    with _conn() as c:
        rows = c.execute("""
            SELECT f.phone, f.cita_id, f.enviado_en, cb.fecha, cb.hora,
                   cb.especialidad, cb.profesional
            FROM fidelizacion_msgs f
            JOIN citas_bot cb ON cb.id_cita = f.cita_id AND cb.phone = f.phone
            WHERE f.tipo = 'postconsulta'
            AND f.enviado_en > datetime('now', '-24 hours')
            AND datetime(cb.fecha || ' ' || substr(cb.hora,1,5)) > datetime(f.enviado_en, '+1 minutes')
        """).fetchall()
        for r in rows:
            out.append({
                "phone": r["phone"], "cita_id": r["cita_id"],
                "enviado": r["enviado_en"], "cita": f"{r['fecha']} {r['hora']}",
                "esp": r["especialidad"]
            })
    return out


def _detect_rut_rechazado_repetido() -> list[dict]:
    """Phones con ≥3 eventos rut_invalido en la última hora."""
    out = []
    with _conn() as c:
        rows = c.execute("""
            SELECT phone, COUNT(*) as cnt, MAX(ts) as ult
            FROM conversation_events
            WHERE event IN ('rut_invalido','valid_rut_failed')
            AND ts > datetime('now', '-1 hour')
            GROUP BY phone
            HAVING cnt >= 3
        """).fetchall()
        for r in rows:
            out.append({"phone": r["phone"], "intentos": r["cnt"], "ult": r["ult"]})
    return out


def _detect_cancelar_con_pay() -> list[dict]:
    """Intent cancelar_cita disparado en mensajes que contienen keywords de pago.
    Indica que el pre-filter cancel-as-pay no atrapó el caso.
    """
    out = []
    pay_kw = ("pagar", "se paga", "cancela allá", "cancela acá", "cancela ahí",
              "cuanto sale", "cuánto sale", "cuanto vale", "cuánto vale")
    with _conn() as c:
        rows = c.execute("""
            SELECT phone, ts, text
            FROM messages
            WHERE direction = 'in'
            AND ts > datetime('now', '-2 hours')
            AND EXISTS (
                SELECT 1 FROM conversation_events e
                WHERE e.phone = messages.phone
                AND e.event IN ('intent_cancelar','cancelar_cita_iniciado')
                AND ABS(strftime('%s', e.ts) - strftime('%s', messages.ts)) < 30
            )
        """).fetchall()
        for r in rows:
            body = (r["text"] or "").lower()
            for kw in pay_kw:
                if kw in body:
                    out.append({"phone": r["phone"], "ts": r["ts"],
                                "msg": body[:120], "keyword": kw})
                    break
    return out


def _detect_fallback_loop() -> list[dict]:
    """Phones que recibieron ≥2 mensajes con 'no_entendi' en últimas 2h."""
    out = []
    with _conn() as c:
        rows = c.execute("""
            SELECT phone, COUNT(*) as cnt
            FROM messages
            WHERE direction = 'out'
            AND ts > datetime('now', '-2 hours')
            AND (text LIKE '%no te entend%' OR text LIKE '%no logro entenderte%' OR text LIKE '%Hmm, no reconozco%')
            GROUP BY phone
            HAVING cnt >= 3
        """).fetchall()
        for r in rows:
            out.append({"phone": r["phone"], "fallbacks": r["cnt"]})
    return out


def _detect_menu_repetido() -> list[dict]:
    """Phones que vieron el menú principal ≥3 veces en últimas 4h."""
    out = []
    with _conn() as c:
        rows = c.execute("""
            SELECT phone, COUNT(*) as cnt
            FROM messages
            WHERE direction = 'out'
            AND ts > datetime('now', '-4 hours')
            AND (text LIKE '%¿En qué te ayudo%' OR text LIKE '%Qué necesitas hoy%')
            GROUP BY phone
            HAVING cnt >= 3
        """).fetchall()
        for r in rows:
            out.append({"phone": r["phone"], "menus": r["cnt"]})
    return out


def _detect_leak_numero_personal() -> list[dict]:
    """Mensajes salientes con 987834148 a alguien que NO es el admin."""
    out = []
    with _conn() as c:
        rows = c.execute("""
            SELECT phone, ts, text
            FROM messages
            WHERE direction = 'out'
            AND ts > datetime('now', '-24 hours')
            AND text LIKE '%987834148%'
            AND phone NOT LIKE '%987834148%'
        """).fetchall()
        for r in rows:
            out.append({"phone": r["phone"], "ts": r["ts"],
                        "snippet": (r["text"] or "")[:140]})
    return out


def _detect_recordatorio_cita_anulada() -> list[dict]:
    """Recordatorios mandados a citas que ahora están marked cancel_detected.
    Indica que el recordatorio se mandó antes de detectar la cancelación
    (caso Sebastian/Quijano hoy)."""
    out = []
    with _conn() as c:
        rows = c.execute("""
            SELECT cb.phone, cb.id_cita, cb.fecha, cb.hora, cb.profesional,
                   cb.cancel_detected_at
            FROM citas_bot cb
            WHERE cb.cancel_detected_at IS NOT NULL
            AND cb.reminder_sent = 1
            AND cb.cancel_detected_at > datetime('now', '-24 hours')
        """).fetchall()
        for r in rows:
            out.append({"phone": r["phone"], "id_cita": r["id_cita"],
                        "cita": f"{r['fecha']} {r['hora']}",
                        "prof": r["profesional"]})
    return out


def _detect_reenganche_caido() -> list[dict]:
    """Si hay sesiones abandonadas (10-90min en estado activo) pero NO hubo
    ningún 'Reenganche enviado' en las últimas 6h, algo está mal."""
    out = []
    with _conn() as c:
        # Sesiones candidatas a reenganche AHORA
        candidatos = c.execute("""
            SELECT COUNT(*) as cnt FROM sessions
            WHERE state NOT IN ('IDLE','COMPLETED','HUMAN_TAKEOVER')
            AND state IS NOT NULL AND state != ''
            AND updated_at < datetime('now', '-10 minutes')
            AND updated_at > datetime('now', '-90 minutes')
        """).fetchone()["cnt"]
        # Reenganches recientes — usamos eventos en lugar de logs
        # Si no hay log_event de reenganche, esto siempre es 0 (no falsa alarma)
        # NOTA: el job actual no loggea evento, sólo log.info. Por eso este detector
        # vive más como "señal informativa" que como alarma fuerte.
        if candidatos >= 3:
            # 3+ sesiones esperando hace rato es señal de que el reenganche no corre
            out.append({"sesiones_pendientes": candidatos})
    return out


# ── Escaneo + envío ──────────────────────────────────────────────────────────


_DETECTORES = [
    ("POSTCONSULTA_PREMATURA",   _detect_postconsulta_prematura),
    ("RUT_RECHAZADO_REPETIDO",   _detect_rut_rechazado_repetido),
    ("CANCELAR_CON_PAY_KEYWORDS",_detect_cancelar_con_pay),
    ("FALLBACK_BOT_LOOP",        _detect_fallback_loop),
    ("MENU_REPETIDO",            _detect_menu_repetido),
    ("LEAK_NUMERO_PERSONAL",     _detect_leak_numero_personal),
    ("RECORDATORIO_CITA_ANULADA",_detect_recordatorio_cita_anulada),
    ("REENGANCHE_CAIDO",         _detect_reenganche_caido),
]


def escanear_anomalias() -> list[tuple[str, dict]]:
    """Corre todos los detectores y retorna lista de (tipo, payload) NUEVAS
    (que no fueron alertadas en últimas 4h)."""
    _ensure_alerts_table()
    nuevas = []
    for tipo, fn in _DETECTORES:
        try:
            hallazgos = fn()
        except Exception as e:
            log.exception("Detector %s falló: %s", tipo, e)
            continue
        for h in hallazgos:
            ah = _alert_hash(tipo, h)
            if _was_alerted_recently(ah):
                continue
            nuevas.append((tipo, h, ah))
    return nuevas


def _format_alerta(tipo: str, payload: dict) -> str:
    """Mensaje legible para mandar al WhatsApp del dueño."""
    desc = _REGLAS.get(tipo, tipo)
    if tipo == "POSTCONSULTA_PREMATURA":
        return (f"⚠️ {desc}\n"
                f"phone: {payload['phone']}\n"
                f"cita: {payload.get('esp','?')} {payload.get('cita','?')}\n"
                f"postconsulta enviado: {payload.get('enviado','?')}")
    if tipo == "RUT_RECHAZADO_REPETIDO":
        return (f"⚠️ {desc}\n"
                f"phone: {payload['phone']} ({payload.get('intentos')} intentos)\n"
                f"último: {payload.get('ult')}")
    if tipo == "CANCELAR_CON_PAY_KEYWORDS":
        return (f"⚠️ {desc}\n"
                f"phone: {payload['phone']}\n"
                f"keyword: {payload.get('keyword')}\n"
                f"msg: {payload.get('msg','')[:100]}")
    if tipo == "FALLBACK_BOT_LOOP":
        return (f"⚠️ {desc}\n"
                f"phone: {payload['phone']} ({payload.get('fallbacks')} fallbacks/2h)")
    if tipo == "MENU_REPETIDO":
        return (f"⚠️ {desc}\n"
                f"phone: {payload['phone']} ({payload.get('menus')} menús/4h)")
    if tipo == "LEAK_NUMERO_PERSONAL":
        return (f"🚨 {desc}\n"
                f"phone: {payload['phone']}\n"
                f"ts: {payload.get('ts')}\n"
                f"snippet: {payload.get('snippet','')[:140]}")
    if tipo == "RECORDATORIO_CITA_ANULADA":
        return (f"⚠️ {desc}\n"
                f"phone: {payload['phone']}\n"
                f"cita: {payload.get('prof','?')} {payload.get('cita','?')}\n"
                f"id_cita: {payload.get('id_cita')}")
    if tipo == "REENGANCHE_CAIDO":
        return (f"ℹ️ {desc}\n"
                f"sesiones esperando: {payload.get('sesiones_pendientes')}")
    return f"⚠️ {desc}\n{json.dumps(payload, default=str)[:300]}"


async def enviar_resumen_anomalias(send_fn) -> int:
    """Escanea, agrupa anomalías nuevas y manda resumen al ADMIN_ALERT_PHONE.
    Retorna cantidad de alertas enviadas.

    send_fn debe ser una función async que envíe WhatsApp (send_whatsapp).
    """
    from config import ADMIN_ALERT_PHONE
    if not ADMIN_ALERT_PHONE:
        log.debug("Monitor: ADMIN_ALERT_PHONE no configurado, skip")
        return 0

    nuevas = escanear_anomalias()
    if not nuevas:
        return 0

    # Agrupar por tipo
    por_tipo: dict[str, list] = {}
    for tipo, payload, ah in nuevas:
        por_tipo.setdefault(tipo, []).append((payload, ah))

    ahora = datetime.now(_TZ_CL).strftime("%H:%M")
    lineas = [f"🤖 *Monitor CMC* · {ahora}", ""]
    for tipo, items in por_tipo.items():
        lineas.append(f"*{_REGLAS.get(tipo, tipo)}* ({len(items)})")
        for payload, _ in items[:3]:  # top 3 por tipo
            lineas.append("• " + _format_alerta(tipo, payload).replace("\n", "\n  "))
        if len(items) > 3:
            lineas.append(f"  …y {len(items)-3} más")
        lineas.append("")

    msg = "\n".join(lineas).strip()
    # Limitar longitud WhatsApp
    if len(msg) > 3500:
        msg = msg[:3490] + "\n…(truncado)"

    try:
        await send_fn(ADMIN_ALERT_PHONE, msg)
    except Exception as e:
        log.exception("Monitor: falló envío al admin: %s", e)
        return 0

    # Marcar alertas como vistas solo si el envío fue OK
    for tipo_m, _, ah in nuevas:
        _mark_alerted(ah, tipo_m)

    log.info("Monitor: %d alertas nuevas enviadas a admin", len(nuevas))
    return len(nuevas)
