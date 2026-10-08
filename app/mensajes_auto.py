"""Resultados de los mensajes automáticos (winback, cross-sell, consentimiento,
ofertas, reactivación, horas liberadas…) para el módulo Campañas Meta.

Por cada envío se mide, cruzando por teléfono y hora:
  - llegó: estado delivered/read en message_statuses (hasta 6 h después)
  - falló: estado failed (código de error más frecuente)
  - respondió: algún mensaje entrante del paciente dentro de 48 h
  - agendó: alguna cita creada por el bot dentro de 7 días (cualquier especialidad)
Es una medición aproximada: el estado de Meta se cruza por teléfono y hora,
no por id de mensaje, porque varias fuentes no guardan el wamid.
Solo lectura. Resultado en caché 10 min por rango.
"""
from __future__ import annotations

import json
import logging
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

log = logging.getLogger("bot")

# key → (nombre visible, grupo, qué es)
PROGRAMAS: dict[str, tuple[str, str, str]] = {
    "consent": ("Pregunta de consentimiento", "Consentimiento y ofertas",
                "Plantilla consent_marketing (v1/v2): envío masivo, barrido de recepción y al agendar."),
    "oferta_limpieza": ("Oferta de limpieza dental", "Consentimiento y ofertas",
                        "Tras aceptar el consentimiento: flyer + primera hora real (desde $30.000)."),
    "oferta_post_limpieza": ("Oferta blanqueamiento / ortodoncia", "Consentimiento y ofertas",
                             "Tras aceptar, a quien ya tiene la limpieza al día en el CMC."),
    "oferta_estetica": ("Oferta de estética facial", "Consentimiento y ofertas",
                        "A quien dijo que ya se hizo la limpieza en otro lado."),
    "crosssell_mg_chequeo": ("Medicina general → chequeo", "Cross-sell",
                             "Pacientes de MG de hace 30 a 180 días: control con exámenes."),
    "crosssell_odonto_estetica": ("Odontología → estética facial", "Cross-sell",
                                  "Pacientes dentales frecuentes: estética con la Dra. Fuentealba."),
    "crosssell_kine": ("Medicina / trauma → kinesiología", "Cross-sell",
                       "Tras una consulta de medicina o traumatología."),
    "crosssell_orl_fono": ("Otorrino ↔ fonoaudiología", "Cross-sell",
                           "Bidireccional entre ORL y fono."),
    "crosssell_post_dental_ortodoncia": ("Post-dental → ortodoncia", "Cross-sell",
                                         "48-72 h después de una atención dental."),
    "winback_bi": ("Winback por especialidad", "Winback y reactivación",
                   "Pacientes inactivos 30-365 días con consentimiento (plantillas winback_*)."),
    "winback_dental": ("Winback dental", "Winback y reactivación",
                       "Pacientes dentales con consentimiento dental."),
    "winback_local": ("Winback (texto, ventana abierta)", "Winback y reactivación",
                      "Winback de fidelización solo a quien escribió en las últimas 24 h."),
    "reactivacion": ("Reactivación", "Winback y reactivación",
                     "Pacientes que no han vuelto, con la especialidad de su última atención."),
    "control": ("Control por especialidad", "Seguimiento clínico",
                "Recordatorio de control (psicología, nutrición, cardiología…)."),
    "adherencia_kine": ("Adherencia kinesiología", "Seguimiento clínico",
                        "Pacientes de kine que dejaron de venir a sus sesiones."),
    "horas_liberadas": ("Aviso de horas liberadas", "Avisos",
                        "Se liberaron horas para mañana, a quien pidió esa especialidad."),
    "reenganche_paz": ("Dr. Paz → alternativas Fonasa", "Avisos",
                       "A quien vio horas del Dr. Paz y no respondió."),
    "cumpleanos": ("Cumpleaños", "Avisos", "Saludo de cumpleaños con tip preventivo."),
}

_FID_MAP = {
    "crosssell_mg_chequeo": "crosssell_mg_chequeo",
    "crosssell_odonto_estetica": "crosssell_odonto_estetica",
    "crosssell_kine": "crosssell_kine",
    "crosssell_orl_fono": "crosssell_orl_fono",
    "crosssell_fono_orl": "crosssell_orl_fono",
    "crosssell_post_dental_ortodoncia": "crosssell_post_dental_ortodoncia",
    "winback": "winback_local",
    "reactivacion": "reactivacion",
    "adherencia_kine": "adherencia_kine",
    "cumpleanos": "cumpleanos",
}
_EVENT_MAP = {
    "consent_oferta_limpieza": "oferta_limpieza",
    "consent_oferta_post_limpieza": "oferta_post_limpieza",
    "consent_oferta_estetica": "oferta_estetica",
}

_CACHE: dict[int, tuple[float, dict]] = {}
_TTL = 600


def _utc_str(dt: datetime) -> str:
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _envios(c, desde: str) -> dict[str, list[tuple[str, str]]]:
    """{programa: [(phone, ts_utc)]} desde `desde` (UTC 'YYYY-MM-DD HH:MM:SS')."""
    out: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for phone, tipo, ts in c.execute(
            "SELECT phone, tipo, enviado_en FROM fidelizacion_msgs WHERE enviado_en >= ?", (desde,)):
        k = _FID_MAP.get(tipo) or ("control" if (tipo or "").startswith("control_") else None)
        if k:
            out[k].append((phone, ts))
    for phone, ts in c.execute(
            "SELECT phone, ts FROM messages WHERE direction='out' AND ts >= ? "
            "AND (text LIKE '[template: consent_marketing_v1]%' OR text LIKE '[template: consent_marketing_v2]%')",
            (desde,)):
        out["consent"].append((phone, ts))
    for phone, ev, ts, meta in c.execute(
            "SELECT phone, event, ts, meta FROM conversation_events WHERE ts >= ? AND event IN "
            "('consent_oferta_limpieza','consent_oferta_post_limpieza','consent_oferta_estetica','reenganche_enviado')",
            (desde,)):
        if ev == "reenganche_enviado":
            if "paz_alternativas" in (meta or ""):
                out["reenganche_paz"].append((phone, ts))
        else:
            out[_EVENT_MAP[ev]].append((phone, ts))
    desde_epoch = int(datetime.strptime(desde, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp())
    for phone, ets in c.execute(
            "SELECT phone, enviado_ts FROM horas_vacias_envios WHERE enviado_ts >= ?", (desde_epoch,)):
        out["horas_liberadas"].append((phone, _utc_str(datetime.fromtimestamp(ets, timezone.utc))))
    try:
        from winback import bi_conn
        from session import normalize_wa_id
        with bi_conn() as conn, conn.cursor() as cur:
            for tabla, k in (("winback_envios", "winback_bi"), ("dental_winback_envios", "winback_dental")):
                cur.execute(f"SELECT telefono, enviado_at FROM bi.{tabla} WHERE enviado_at >= %s::timestamptz",
                            (desde + "+00",))
                for tel, at in cur.fetchall():
                    if tel and at:
                        out[k].append((normalize_wa_id(tel), _utc_str(at)))
    except Exception as e:  # noqa: BLE001 — BI caído: se muestran solo las fuentes locales
        log.warning("mensajes_auto: BI no disponible: %s", e)
        out["_bi_error"] = []
    return out


def _medir(c, envios: list[tuple[str, str]]) -> dict:
    llego = fallo = leido = respondio = agendo = 0
    errores: Counter = Counter()
    for phone, ts in envios:
        st = c.execute(
            "SELECT status, CAST(error_code AS TEXT) FROM message_statuses WHERE phone=? "
            "AND ts BETWEEN datetime(?, '-1 minutes') AND datetime(?, '+6 hours') "
            "ORDER BY CASE status WHEN 'failed' THEN 0 WHEN 'read' THEN 1 WHEN 'delivered' THEN 2 ELSE 3 END LIMIT 1",
            (phone, ts, ts)).fetchone()
        if st:
            if st[0] == "failed":
                fallo += 1
                errores[st[1] or "?"] += 1
            elif st[0] in ("delivered", "read"):
                llego += 1
                leido += st[0] == "read"
        if c.execute("SELECT 1 FROM messages WHERE phone=? AND direction='in' "
                      "AND ts > ? AND ts <= datetime(?, '+48 hours') LIMIT 1", (phone, ts, ts)).fetchone():
            respondio += 1
        if c.execute("SELECT 1 FROM citas_bot WHERE phone=? AND created_at > ? "
                     "AND created_at <= datetime(?, '+7 days') LIMIT 1", (phone, ts, ts)).fetchone():
            agendo += 1
    n = len(envios)
    pct = (lambda x: round(100 * x / n, 1) if n else None)
    return {"enviados": n, "personas": len({p for p, _ in envios}), "llegaron": llego,
            "leidos": leido, "fallaron": fallo, "error_top": errores.most_common(1)[0][0] if errores else None,
            "respondieron": respondio, "agendaron": agendo,
            "pct_llego": pct(llego), "pct_respondio": pct(respondio), "pct_agendo": pct(agendo)}


def _aceptaron_consent(c, envios: list[tuple[str, str]]) -> tuple[int, int]:
    si = no = 0
    for phone, ts in envios:
        r = c.execute("SELECT meta FROM conversation_events WHERE phone=? AND event='marketing_consent_respuesta' "
                      "AND ts > ? AND ts <= datetime(?, '+7 days') ORDER BY ts LIMIT 1", (phone, ts, ts)).fetchone()
        if r:
            st = (json.loads(r[0] or "{}") or {}).get("status")
            si += st == "accepted"
            no += st == "declined"
    return si, no


def resumen(dias: int = 30) -> dict:
    dias = max(1, min(int(dias or 30), 180))
    hit = _CACHE.get(dias)
    if hit and time.time() - hit[0] < _TTL:
        return hit[1]
    from session import db
    desde = _utc_str(datetime.now(timezone.utc) - timedelta(days=dias))
    with db() as c:
        env = _envios(c, desde)
        bi_error = "_bi_error" in env
        filas = []
        for k, (nombre, grupo, que) in PROGRAMAS.items():
            m = _medir(c, env.get(k, []))
            if k == "consent":
                m["aceptaron"], m["rechazaron"] = _aceptaron_consent(c, env.get(k, []))
            filas.append({"key": k, "nombre": nombre, "grupo": grupo, "que": que, **m})
    tot = {x: sum(f[x] for f in filas) for x in ("enviados", "llegaron", "fallaron", "respondieron", "agendaron")}
    out = {"dias": dias, "desde_utc": desde, "filas": filas, "total": tot, "bi_error": bi_error,
           "generado": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    _CACHE[dias] = (time.time(), out)
    return out
