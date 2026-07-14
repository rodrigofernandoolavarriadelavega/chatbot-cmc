"""agenda_ticker.py — Monitor en vivo del orden de llegada de agendamientos.

Qué resuelve: Medilink NO devuelve fecha de creación de una cita, solo
`fecha_actualizacion` (última modificación). La señal confiable de orden de
creación es el `id` de la cita — es incremental a nivel de sucursal, sin
importar el profesional. Confirmado empíricamente 2026-07-14: una consulta
`GET /citas?q={"id":{"gte":N}}` devuelve las citas con id >= N YA ORDENADAS
DESCENDENTE por id (la más nueva primero), sin importar la fecha de la cita.

Estrategia (UNA sola consulta por pasada — ver docs/medilink_gotchas.md §6,
antecedente del fan-out que tumbó /admin/api/agenda-dia con 429 en cascada):
  1. Cursor persistido en `system_state` (clave agenda_ticker_last_id) =
     mayor id de cita ya visto.
  2. Cada pasada del poller: `id gte cursor+1`, `id_sucursal eq SUCURSAL`,
     `estado_anulacion eq 0`. Sin filtro de profesional ni de fecha — trae
     TODAS las citas nuevas de la sucursal en una sola llamada.
  3. Si en la ventana de poll entraron más de 50 citas (una página), se sigue
     el cursor `next` de Medilink hasta `max_paginas` veces (ráfaga real, no
     fan-out: es la MISMA consulta, paginada secuencialmente).
  4. Cada cita nueva se clasifica por canal (bot / agenda online / recepción)
     cruzando contra `cita_origen` y `citas_bot` (locales, sin costo Medilink)
     y se persiste en `agenda_ticker` (append-only, PK=id_cita → idempotente).

Aviso de atenciones sin cerrar: barrido de baja frecuencia (una vez al día,
horario valle) sobre citas PASADAS de los últimos N días, también con una
sola consulta base (paginada) por `id_sucursal` + rango de `fecha`, sin
filtro de profesional — se agrupa por profesional en memoria. Ver hallazgo
2026-07-14: Dr. Abarca (id 73) con 0% de cierre desde mayo 2026.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from config import MEDILINK_BASE_URL, MEDILINK_SUCURSAL

log = logging.getLogger("agenda_ticker")

_CHILE_TZ = ZoneInfo("America/Santiago")

# Estados Medilink que indican que la cita SÍ quedó administrativamente
# cerrada o que fue una inasistencia real (no es "cierre pendiente").
_ESTADOS_CERRADOS = {"atendido", "no asiste"}


def ensure_agenda_ticker_table() -> None:
    """Crea la tabla del ticker si no existe. Idempotente."""
    from session import db
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS agenda_ticker (
                id_cita              INTEGER PRIMARY KEY,
                id_profesional       INTEGER,
                profesional          TEXT DEFAULT '',
                especialidad         TEXT DEFAULT '',
                id_paciente          INTEGER,
                paciente_nombre      TEXT DEFAULT '',
                fecha_cita           TEXT DEFAULT '',
                hora_inicio          TEXT DEFAULT '',
                estado_cita          TEXT DEFAULT '',
                fecha_actualizacion  TEXT DEFAULT '',
                canal                TEXT DEFAULT 'desconocido',
                canal_detalle        TEXT DEFAULT '',
                seen_at              TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_agenda_ticker_seen ON agenda_ticker(seen_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_agenda_ticker_prof ON agenda_ticker(id_profesional)")
        # fecha_creacion_real: hora EXACTA de creación, tomada del correo que
        # Medilink manda al Gmail del centro (ver email_ticker.py) — cabecera
        # `Date`, convertida a hora de Chile. `fecha_actualizacion` (Medilink)
        # es solo un proxy (última modificación); esta columna, cuando está
        # poblada, es la fuente de verdad y `_fmt_hace`/`get_feed` la
        # priorizan. NULL hasta que el correo correspondiente se cruza.
        try:
            conn.execute("ALTER TABLE agenda_ticker ADD COLUMN fecha_creacion_real TEXT")
        except Exception:
            pass  # columna ya existe
        conn.commit()


def set_fecha_creacion_real(id_cita, email_ts: str) -> None:
    """Alimentado por email_ticker.poll_email_ticker() cuando un correo
    'Cita agendada' se logra cruzar contra una fila de este ticker.
    `email_ts` ya viene en hora de Chile, formato 'YYYY-MM-DD HH:MM:SS'
    (mismo formato que usa _fmt_hace para fecha_actualizacion)."""
    from session import db
    ensure_agenda_ticker_table()  # garantiza la columna antes del UPDATE (carrera de arranque en frío)
    with db() as conn:
        conn.execute(
            "UPDATE agenda_ticker SET fecha_creacion_real = ? WHERE id_cita = ?",
            (email_ts, id_cita),
        )
        conn.commit()


def _q(params: dict) -> str:
    return json.dumps(params, separators=(",", ":"))


def _links(j: dict) -> dict:
    """Medilink devuelve `links` como dict ({"current":..,"next":..}) cuando
    hay resultados, pero como LISTA VACÍA `[]` cuando `data` viene vacío.
    Normaliza siempre a dict para no reventar en .get("next")."""
    links = j.get("links")
    return links if isinstance(links, dict) else {}


def _mapear_canal_origen(canal_raw: str, fuente: str) -> tuple[str, str]:
    """Traduce (canal, fuente) de `cita_origen` (pagos_routes.py) al canal
    de negocio que muestra el ticker. Ver registrar_origen_cita() callers:
    agenda_routes.py (panel Alma, recepción) y agendador_routes.py (widget
    web self-booking)."""
    canal_raw = (canal_raw or "").strip().lower()
    fuente = (fuente or "").strip().lower()
    if canal_raw == "web" and fuente == "agendador":
        return ("agenda_online", "Agendador web")
    if canal_raw == "bot":
        return ("bot_whatsapp", fuente or "")
    if canal_raw == "chat_humano":
        return ("recepcion_chat", fuente or "")
    if canal_raw == "telefono":
        return ("recepcion_telefono", "")
    if canal_raw == "presencial":
        return ("recepcion_presencial", "")
    return ("desconocido", f"{canal_raw}/{fuente}" if canal_raw else "")


def _clasificar_canal(id_cita) -> tuple[str, str]:
    """Cruza el id de Medilink contra `cita_origen` (dato directo, prioridad 1)
    y `citas_bot` (dato indirecto, prioridad 2) — ambas locales, sin llamar a
    Medilink. Si no aparece en ninguna, NO se adivina: se marca 'recepcion'
    (agendada a mano en Medilink, sin paso por el software de Alma) — es
    literalmente el bucket por defecto que pidió el dueño."""
    from session import db
    id_str = str(id_cita)
    try:
        with db() as conn:
            row = conn.execute(
                "SELECT canal, fuente FROM cita_origen WHERE id_cita = ? LIMIT 1",
                (id_str,)
            ).fetchone()
            if row:
                return _mapear_canal_origen(row["canal"], row["fuente"])
            row2 = conn.execute(
                "SELECT 1 FROM citas_bot WHERE id_cita = ? LIMIT 1",
                (id_str,)
            ).fetchone()
            if row2:
                return ("bot_whatsapp", "citas_bot (sin registro de origen)")
    except Exception as e:
        log.warning("_clasificar_canal id_cita=%s error=%s", id_cita, e)
        return ("desconocido", "error al consultar origen local")
    return ("recepcion", "sin registro de origen — agendada a mano en Medilink")


CANAL_LABELS = {
    "bot_whatsapp":        "Bot WhatsApp",
    "agenda_online":       "Agenda online (web)",
    "recepcion_telefono":  "Recepción (teléfono)",
    "recepcion_presencial":"Recepción (presencial)",
    "recepcion_chat":      "Recepción vía WhatsApp",
    "recepcion":           "Recepción / Medilink directo",
    "desconocido":         "Origen desconocido",
}


async def _fetch_max_cita_id(client: httpx.AsyncClient, headers: dict) -> int:
    """Semilla de arranque en frío: el `id` más alto ya existente en la
    sucursal (primer elemento de la primera página, que Medilink ya devuelve
    ordenada descendente por id). NO se hace backfill de historial — el
    ticker arranca vacío y solo muestra agendamientos genuinamente nuevos
    desde que se enciende, para no reportar como 'nuevo' algo agendado hace
    semanas."""
    params = {"id_sucursal": {"eq": int(MEDILINK_SUCURSAL)}, "estado_anulacion": {"eq": 0}}
    r = await client.get(f"{MEDILINK_BASE_URL}/citas", params={"q": _q(params)}, headers=headers, timeout=15)
    r.raise_for_status()
    data = r.json().get("data", [])
    if not data:
        return 0
    return max(c.get("id", 0) for c in data)


async def poll_agenda_ticker(max_paginas: int = 3) -> dict:
    """Job del scheduler (cada ~45s). UNA sola consulta base por pasada
    (con paginación acotada solo si hubo ráfaga > 50 citas nuevas)."""
    from session import system_state_get, system_state_set, db
    from medilink import HEADERS, _get_shared_client, PROFESIONALES

    ensure_agenda_ticker_table()
    client = _get_shared_client()

    cursor_raw = system_state_get("agenda_ticker_last_id")
    if cursor_raw is None:
        try:
            seed = await _fetch_max_cita_id(client, HEADERS)
        except Exception as e:
            log.error("poll_agenda_ticker: no se pudo sembrar cursor inicial: %s", e)
            return {"ok": False, "error": str(e)}
        system_state_set("agenda_ticker_last_id", str(seed))
        log.info("poll_agenda_ticker: arranque en frío, cursor=%d (sin backfill)", seed)
        return {"ok": True, "nuevas": 0, "cursor": seed, "cold_start": True}

    cursor = int(cursor_raw)
    params = {
        "id_sucursal":      {"eq": int(MEDILINK_SUCURSAL)},
        "id":               {"gte": cursor + 1},
        "estado_anulacion": {"eq": 0},
    }
    try:
        r = await client.get(f"{MEDILINK_BASE_URL}/citas", params={"q": _q(params)}, headers=HEADERS, timeout=15)
    except httpx.RequestError as e:
        log.error("poll_agenda_ticker: fallo de red: %s", e)
        return {"ok": False, "error": str(e)}
    if r.status_code != 200:
        log.warning("poll_agenda_ticker: HTTP %d", r.status_code)
        return {"ok": False, "error": f"HTTP {r.status_code}"}

    j = r.json()
    all_rows = list(j.get("data", []))
    links = _links(j)
    paginas = 1
    while links.get("next") and paginas < max_paginas:
        try:
            r2 = await client.get(links["next"], headers=HEADERS, timeout=15)
        except httpx.RequestError as e:
            log.warning("poll_agenda_ticker: falló página %d: %s", paginas + 1, e)
            break
        if r2.status_code != 200:
            break
        j2 = r2.json()
        all_rows.extend(j2.get("data", []))
        links = _links(j2)
        paginas += 1

    if links.get("next"):
        log.warning(
            "poll_agenda_ticker: más de %d citas nuevas en un solo ciclo (paginas=%d) — "
            "posible pérdida de orden, considerar bajar el intervalo de poll",
            paginas * 50, paginas,
        )

    if not all_rows:
        return {"ok": True, "nuevas": 0, "cursor": cursor}

    new_max = max(int(c.get("id", cursor)) for c in all_rows)
    rows_to_insert = []
    for c in all_rows:
        id_cita = c.get("id")
        id_prof = c.get("id_profesional")
        prof_info = PROFESIONALES.get(id_prof, {}) if id_prof else {}
        canal, detalle = _clasificar_canal(id_cita)
        rows_to_insert.append({
            "id_cita":             id_cita,
            "id_profesional":      id_prof,
            "profesional":         c.get("nombre_profesional") or prof_info.get("nombre", ""),
            "especialidad":        prof_info.get("especialidad", ""),
            "id_paciente":         c.get("id_paciente"),
            "paciente_nombre":     (c.get("nombre_paciente") or "").strip(),
            "fecha_cita":          c.get("fecha", ""),
            "hora_inicio":         (c.get("hora_inicio") or "")[:5],
            "estado_cita":         c.get("estado_cita", ""),
            "fecha_actualizacion": c.get("fecha_actualizacion", ""),
            "canal":               canal,
            "canal_detalle":       detalle,
        })

    with db() as conn:
        conn.executemany("""
            INSERT OR IGNORE INTO agenda_ticker
                (id_cita, id_profesional, profesional, especialidad, id_paciente,
                 paciente_nombre, fecha_cita, hora_inicio, estado_cita,
                 fecha_actualizacion, canal, canal_detalle)
            VALUES
                (:id_cita, :id_profesional, :profesional, :especialidad, :id_paciente,
                 :paciente_nombre, :fecha_cita, :hora_inicio, :estado_cita,
                 :fecha_actualizacion, :canal, :canal_detalle)
        """, rows_to_insert)
        conn.commit()

    system_state_set("agenda_ticker_last_id", str(new_max))
    log.info("poll_agenda_ticker: %d cita(s) nueva(s), cursor=%d", len(rows_to_insert), new_max)
    return {"ok": True, "nuevas": len(rows_to_insert), "cursor": new_max}


def _fmt_hace(fecha_actualizacion: str) -> str:
    """'Hace cuánto entró', calculado en el SERVIDOR (hora de Chile) para no
    depender de la zona horaria del navegador. Usa `fecha_actualizacion` de
    Medilink como proxy de creación (ver docstring del módulo) — es exacto
    para una cita recién creada que nadie ha editado todavía."""
    if not fecha_actualizacion:
        return ""
    try:
        s = str(fecha_actualizacion).replace("T", " ")[:19]
        t = datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=_CHILE_TZ)
        delta = datetime.now(_CHILE_TZ) - t
        mins = int(delta.total_seconds() // 60)
        if mins < 1:
            return "recién"
        if mins < 60:
            return f"hace {mins} min"
        horas = mins // 60
        if horas < 24:
            return f"hace {horas} h"
        dias = horas // 24
        return f"hace {dias} d"
    except Exception:
        return ""


def get_feed(limit: int = 60) -> list[dict]:
    """Ticker: las últimas `limit` citas vistas, más nueva primero."""
    from session import db
    ensure_agenda_ticker_table()
    with db() as conn:
        rows = conn.execute("""
            SELECT id_cita, id_profesional, profesional, especialidad, paciente_nombre,
                   fecha_cita, hora_inicio, estado_cita, fecha_actualizacion,
                   fecha_creacion_real, canal, canal_detalle, seen_at
            FROM agenda_ticker
            ORDER BY id_cita DESC
            LIMIT ?
        """, (limit,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["canal_label"] = CANAL_LABELS.get(d["canal"], d["canal"])
        # Prioriza la hora real de creación (correo Medilink, exacta) sobre
        # fecha_actualizacion (Medilink, es solo la última modificación —
        # buen proxy para una cita recién creada, pero no exacto).
        if d.get("fecha_creacion_real"):
            d["hace"] = _fmt_hace(d["fecha_creacion_real"])
            d["fuente_hora"] = "correo"
        else:
            d["hace"] = _fmt_hace(d["fecha_actualizacion"])
            d["fuente_hora"] = "medilink"
        out.append(d)
    return out


def _hoy_chile_bounds() -> tuple[str, str]:
    """Límites [inicio, fin) del día de HOY en hora de Chile, expresados en
    UTC (mismo formato que datetime('now') de SQLite) para acotar la
    consulta de contadores."""
    ahora_cl = datetime.now(_CHILE_TZ)
    inicio_cl = ahora_cl.replace(hour=0, minute=0, second=0, microsecond=0)
    fin_cl = inicio_cl + timedelta(days=1)
    inicio_utc = inicio_cl.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M:%S")
    fin_utc = fin_cl.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M:%S")
    return inicio_utc, fin_utc


def get_contadores_hoy() -> dict:
    """Cuántos agendamientos entraron HOY (desde que el monitor está
    encendido — ver nota en get_feed/UI), por canal y por especialidad.
    'Hoy' = día calendario en hora de Chile."""
    from session import db
    ensure_agenda_ticker_table()
    inicio, fin = _hoy_chile_bounds()
    with db() as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM agenda_ticker WHERE seen_at >= ? AND seen_at < ?",
            (inicio, fin)
        ).fetchone()[0]
        por_canal = conn.execute("""
            SELECT canal, COUNT(*) n FROM agenda_ticker
            WHERE seen_at >= ? AND seen_at < ? GROUP BY canal ORDER BY n DESC
        """, (inicio, fin)).fetchall()
        por_esp = conn.execute("""
            SELECT especialidad, COUNT(*) n FROM agenda_ticker
            WHERE seen_at >= ? AND seen_at < ? GROUP BY especialidad ORDER BY n DESC
        """, (inicio, fin)).fetchall()
    return {
        "total": total,
        "por_canal": [{"canal": r["canal"], "canal_label": CANAL_LABELS.get(r["canal"], r["canal"]), "n": r["n"]} for r in por_canal],
        "por_especialidad": [{"especialidad": r["especialidad"] or "Sin especialidad", "n": r["n"]} for r in por_esp],
    }


# ── Aviso pasivo: citas pasadas sin cerrar (hallazgo Abarca 2026-07-14) ────

async def sync_atenciones_sin_cerrar(dias: int = 30, max_paginas: int = 60) -> dict:
    """Barrido de baja frecuencia (job diario, horario valle): TODAS las citas
    pasadas de los últimos `dias` días, una sola consulta base (id_sucursal +
    rango de fecha, SIN id_profesional) paginada secuencialmente. Cuenta,
    por profesional, cuántas quedaron sin cerrar: ni 'Atendido' ni 'No asiste'
    (esto último es inasistencia real, no negligencia administrativa).
    Persiste el resultado en system_state para que el ticker lo lea sin
    tocar Medilink en cada refresh del panel."""
    from session import system_state_set
    from medilink import HEADERS, _get_shared_client, PROFESIONALES

    client = _get_shared_client()
    hoy = datetime.now(_CHILE_TZ).date()
    hoy_str = hoy.isoformat()
    desde = (hoy - timedelta(days=dias)).isoformat()
    hasta = (hoy - timedelta(days=1)).isoformat()
    params = {
        "id_sucursal":      {"eq": int(MEDILINK_SUCURSAL)},
        "fecha":            {"gte": desde, "lte": hasta},
        "estado_anulacion": {"eq": 0},
    }
    conteos: dict[int, dict] = {}
    url = f"{MEDILINK_BASE_URL}/citas"
    request_params = {"q": _q(params)}
    paginas = 0
    total_vistas = 0
    try:
        while paginas < max_paginas:
            r = await client.get(url, params=request_params, headers=HEADERS, timeout=15)
            if r.status_code != 200:
                log.warning("sync_atenciones_sin_cerrar: HTTP %d en página %d", r.status_code, paginas + 1)
                break
            j = r.json()
            data = j.get("data", [])
            total_vistas += len(data)
            for c in data:
                # Defensa: Medilink ignora `gte`/`lte` de `fecha` con MUCHA más
                # frecuencia de lo documentado (verificado 2026-07-14: en una
                # consulta acotada a mayo-2026 para un solo profesional, 273 de
                # 461 filas devueltas — 59% — venían de fuera del rango pedido).
                # Sin este filtro cliente-side, "sin cerrar" cuenta citas
                # FUTURAS que legítimamente no son "Atendido" todavía, inflando
                # el número. Acotamos ambos extremos a mano.
                f_cita = c.get("fecha", "")
                if not f_cita or f_cita < desde or f_cita >= hoy_str:
                    continue
                est = (c.get("estado_cita") or "").strip().lower()
                if est in _ESTADOS_CERRADOS:
                    continue
                id_prof = c.get("id_profesional")
                if not id_prof:
                    continue
                info = conteos.setdefault(id_prof, {
                    "id_profesional": id_prof,
                    "profesional": c.get("nombre_profesional") or PROFESIONALES.get(id_prof, {}).get("nombre", f"Prof {id_prof}"),
                    "sin_cerrar": 0,
                    "fecha_mas_antigua": None,
                })
                info["sin_cerrar"] += 1
                f = c.get("fecha", "")
                if f and (info["fecha_mas_antigua"] is None or f < info["fecha_mas_antigua"]):
                    info["fecha_mas_antigua"] = f
            nxt = _links(j).get("next")
            paginas += 1
            if not nxt:
                break
            url, request_params = nxt, None
            import asyncio
            await asyncio.sleep(0.25)  # cortesía con el rate limit — barrido de decenas de páginas
    except httpx.RequestError as e:
        log.error("sync_atenciones_sin_cerrar: fallo de red tras %d páginas: %s", paginas, e)

    resultado = sorted(conteos.values(), key=lambda x: -x["sin_cerrar"])
    payload = {
        "generado_at": datetime.now(_CHILE_TZ).isoformat(timespec="seconds"),
        "dias": dias,
        "total_citas_revisadas": total_vistas,
        "por_profesional": resultado,
    }
    system_state_set("agenda_ticker_sin_cerrar", json.dumps(payload, ensure_ascii=False))
    log.info("sync_atenciones_sin_cerrar: %d citas revisadas, %d profesional(es) con pendientes",
              total_vistas, len(resultado))
    return payload


def get_aviso_sin_cerrar() -> dict | None:
    """Lee el último resultado cacheado de sync_atenciones_sin_cerrar()
    (system_state, sin tocar Medilink). None si nunca corrió."""
    from session import system_state_get
    raw = system_state_get("agenda_ticker_sin_cerrar")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None
