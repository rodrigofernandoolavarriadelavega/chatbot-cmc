"""ausentismo.py — Ranking de pacientes que agendan y no asisten.

Fuente de verdad: Medilink `/citas`. La metodología viene del análisis de
ausentismo 2026-08-05 (validado contra datos reales de producción):

  - No-show REAL       = id_estado=8 ("No asiste") con estado_anulacion=0.
  - Cancelación formal = estado_anulacion=1 (id_estado 1 / 10). NO es no-show.
  - id_estado=14 ("Cambio de fecha") = slot viejo de una reagenda → se EXCLUYE
    del universo por completo (no es una cita con desenlace propio).
  - Deduplicación por paciente-día-profesional: una reagenda intradía deja
    "Anulado"+"Atendido" (o "No asiste"+"Atendido") el mismo día; si el
    paciente terminó atendido ese día con ese profesional, NO cuenta como
    inasistencia.

El navegador NUNCA toca Medilink: lee la tabla local `ausentismo_citas`
(sessions.db), poblada por un job nocturno (04:50 CLT) que barre `/citas`
paginado con carril batch (guardrail 429). Backfill profundo automático la
primera noche (o a demanda vía endpoint gateado a dueño).

Regla anti-deadlock (incidente 2026-06-10): NUNCA se sostiene la conexión
SQLite mientras se llama a Medilink. Fase 1 (async) acumula en memoria;
fase 2 (sync) escribe en una sola transacción.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from config import MEDILINK_BASE_URL, MEDILINK_SUCURSAL

log = logging.getLogger("ausentismo")

_CLT = ZoneInfo("America/Santiago")

# Ventanas de recolección
_DIAS_HISTORIA = 365      # profundidad máxima del backfill
_DIAS_REFRESH = 45        # ventana móvil que se re-consulta cada noche (estados aún mutan)
_DIAS_ADELANTE = 14       # citas futuras: permiten mostrar "tiene próxima cita" al monitorear
_THROTTLE_PAG = 0.3       # pausa entre páginas — cortesía con el rate limit

_STATE_KEY = "ausentismo_recoleccion"   # system_state: metadata del último barrido


# ────────────────────────────────────────────────────────────────────────────
# Tabla local
# ────────────────────────────────────────────────────────────────────────────

def ensure_ausentismo_table() -> None:
    """Crea la tabla si no existe. Idempotente."""
    from session import db
    with db() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS ausentismo_citas (
                id_cita        INTEGER PRIMARY KEY,
                id_profesional INTEGER,
                id_paciente    INTEGER,
                paciente       TEXT DEFAULT '',
                fecha          TEXT,
                hora           TEXT DEFAULT '',
                id_estado      INTEGER,
                estado_cita    TEXT DEFAULT '',
                anulacion      INTEGER DEFAULT 0,
                updated_at     TEXT
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_ausentismo_pac ON ausentismo_citas(id_paciente)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_ausentismo_prof_fecha ON ausentismo_citas(id_profesional, fecha)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_ausentismo_fecha ON ausentismo_citas(fecha)")
        c.commit()


def _q(params: dict) -> str:
    return json.dumps(params, separators=(",", ":"))


def _links(j: dict) -> dict:
    # Medilink devuelve `links` como [] cuando data viene vacío — normalizar.
    links = j.get("links")
    return links if isinstance(links, dict) else {}


# ────────────────────────────────────────────────────────────────────────────
# Recolección desde Medilink
# ────────────────────────────────────────────────────────────────────────────

async def recolectar(dias_atras: int = _DIAS_REFRESH, dias_adelante: int = _DIAS_ADELANTE,
                     max_paginas: int = 900) -> dict:
    """Barre `/citas` (sucursal + rango de fecha, SIN filtro de anulación —
    necesitamos ver anuladas y reagendas) paginado, y upserta en la tabla local.

    Gotchas Medilink que este código respeta:
      - El filtro `fecha` gte/lte se IGNORA con frecuencia (59% de filas fuera
        de rango en un caso real medido) → re-filtro cliente-side obligatorio.
      - El orden de resultados es DESCENDENTE por id (lo más nuevo primero),
        sin importar la fecha → corte temprano cuando varias páginas seguidas
        ya no traen nada dentro del rango pedido.
    """
    from medilink import HEADERS, _get_shared_client, use_batch_lane
    from session import system_state_set

    use_batch_lane()  # guardrail 429: crons/syncs jamás por el carril del paciente

    hoy = datetime.now(_CLT).date()
    desde = (hoy - timedelta(days=dias_atras)).isoformat()
    hasta = (hoy + timedelta(days=dias_adelante)).isoformat()

    params = {
        "id_sucursal": {"eq": int(MEDILINK_SUCURSAL)},
        "fecha":       {"gte": desde, "lte": hasta},
    }
    client = _get_shared_client()
    url = f"{MEDILINK_BASE_URL}/citas"
    request_params = {"q": _q(params)}

    filas: list[tuple] = []
    ahora = datetime.now(_CLT).isoformat(timespec="seconds")
    paginas = total_vistas = 0
    paginas_sin_rango = 0   # corte temprano: N páginas seguidas 100% fuera de rango
    truncado = False

    # ── fase 1: Medilink (async, sin DB tomada) ──
    try:
        while True:
            if paginas >= max_paginas:
                truncado = True
                log.warning("ausentismo.recolectar: tope de %d páginas alcanzado (rango %s→%s incompleto)",
                            max_paginas, desde, hasta)
                break
            r = await client.get(url, params=request_params, headers=HEADERS, timeout=15)
            if r.status_code != 200:
                log.warning("ausentismo.recolectar: HTTP %d en página %d", r.status_code, paginas + 1)
                break
            j = r.json()
            data = j.get("data", [])
            total_vistas += len(data)
            en_rango = 0
            for c in data:
                f = c.get("fecha") or ""
                if not f or f < desde or f > hasta:
                    continue
                cid = c.get("id")
                if cid is None:
                    continue
                en_rango += 1
                filas.append((
                    int(cid),
                    c.get("id_profesional"),
                    c.get("id_paciente"),
                    " ".join((c.get("nombre_paciente") or "").split()),
                    f,
                    (c.get("hora_inicio") or "")[:5],
                    c.get("id_estado"),
                    c.get("estado_cita") or "",
                    1 if c.get("estado_anulacion") == 1 else 0,
                    ahora,
                ))
            paginas += 1
            paginas_sin_rango = paginas_sin_rango + 1 if (data and en_rango == 0) else 0
            if paginas_sin_rango >= 3:
                # el orden es id DESC ≈ cronológico inverso: 3 páginas seguidas
                # sin nada en rango = ya pasamos el horizonte pedido
                break
            nxt = _links(j).get("next")
            if not nxt:
                break
            url, request_params = nxt, None
            await asyncio.sleep(_THROTTLE_PAG)
    except httpx.RequestError as e:
        log.error("ausentismo.recolectar: fallo de red tras %d páginas: %s", paginas, e)

    # ── fase 2: escritura (una transacción, sin Medilink en medio) ──
    from session import db
    ensure_ausentismo_table()
    with db() as c:
        c.executemany(
            """INSERT INTO ausentismo_citas
                 (id_cita, id_profesional, id_paciente, paciente, fecha, hora,
                  id_estado, estado_cita, anulacion, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id_cita) DO UPDATE SET
                 id_estado=excluded.id_estado, estado_cita=excluded.estado_cita,
                 anulacion=excluded.anulacion, paciente=excluded.paciente,
                 fecha=excluded.fecha, hora=excluded.hora,
                 id_profesional=excluded.id_profesional,
                 id_paciente=excluded.id_paciente, updated_at=excluded.updated_at""",
            filas,
        )
        c.commit()

    resultado = {
        "generado_at": ahora, "desde": desde, "hasta": hasta,
        "paginas": paginas, "citas_vistas": total_vistas,
        "citas_guardadas": len(filas), "truncado": truncado,
    }
    system_state_set(_STATE_KEY, json.dumps(resultado, ensure_ascii=False))
    log.info("ausentismo.recolectar: %d páginas, %d citas en rango %s→%s (truncado=%s)",
             paginas, len(filas), desde, hasta, truncado)
    return resultado


async def job_ausentismo_nocturno() -> dict:
    """Job 04:50 CLT. Refresca la ventana móvil (estados recientes aún mutan)
    y, si el histórico todavía no llega a _DIAS_HISTORIA, hace el barrido
    profundo esa misma noche (auto-backfill: solo ocurre hasta completarse)."""
    from session import db
    ensure_ausentismo_table()
    with db() as c:
        row = c.execute("SELECT MIN(fecha) FROM ausentismo_citas").fetchone()
    min_fecha = row[0] if row and row[0] else None
    horizonte = (datetime.now(_CLT).date() - timedelta(days=_DIAS_HISTORIA)).isoformat()
    dias = _DIAS_HISTORIA if (min_fecha is None or min_fecha > horizonte) else _DIAS_REFRESH
    return await recolectar(dias_atras=dias)


# ────────────────────────────────────────────────────────────────────────────
# Clasificación y análisis (solo DB local — nunca Medilink)
# ────────────────────────────────────────────────────────────────────────────

def _clasificar(id_estado, estado_cita: str, anulacion: int) -> str:
    """Estados verificados en producción (docs/medilink_gotchas.md #11 +
    análisis 2026-08-05): 2=Atendido · 8=No asiste · 14=Cambio de fecha ·
    1/10=Anulado. El texto es fallback por si aparece un id nuevo."""
    est = (estado_cita or "").strip().lower()
    if id_estado == 14:
        return "reagenda"
    if id_estado == 2 or est.startswith("atend"):
        return "atendida"
    if anulacion == 1:
        return "anulada"
    if id_estado == 8 or est.startswith("no asist"):
        return "no_show"
    return "otra"


def analizar(dias: int = 180, id_prof: int | None = None, min_no_shows: int = 1,
             limite: int = 200) -> dict:
    """Ranking de pacientes por inasistencias en los últimos `dias` días,
    opcionalmente acotado a un profesional. Devuelve KPIs + ranking +
    lista de profesionales para el filtro + estado de la recolección."""
    from session import db, system_state_get
    from medilink import PROFESIONALES

    hoy = datetime.now(_CLT).date()
    hoy_iso = hoy.isoformat()
    desde = (hoy - timedelta(days=dias)).isoformat()

    ensure_ausentismo_table()
    where_prof = " AND id_profesional = ?" if id_prof else ""
    args_pasado: list = [desde, hoy_iso]
    args_futuro: list = [hoy_iso]
    if id_prof:
        args_pasado.append(id_prof)
        args_futuro.append(id_prof)
    with db() as c:
        pasado = c.execute(
            f"""SELECT id_paciente, paciente, fecha, hora, id_profesional,
                       id_estado, estado_cita, anulacion
                FROM ausentismo_citas
                WHERE fecha >= ? AND fecha < ? AND id_paciente IS NOT NULL{where_prof}""",
            args_pasado).fetchall()
        futuro = c.execute(
            f"""SELECT id_paciente, fecha, hora, id_profesional
                FROM ausentismo_citas
                WHERE fecha >= ? AND anulacion = 0 AND id_estado != 14
                      AND id_paciente IS NOT NULL{where_prof}""",
            args_futuro).fetchall()

    def _prof_nombre(idp) -> str:
        return PROFESIONALES.get(idp, {}).get("nombre") or f"Prof. {idp}"

    # ── unidad de análisis: (paciente, día, profesional) ──
    # Precedencia del desenlace del día: atendida > no_show > anulada > otra.
    # Así la reagenda intradía (Anulado/No asiste + Atendido) NO castiga.
    unidades: dict[tuple, dict] = {}
    nombres: dict[int, str] = {}
    for idpac, nombre, fecha, hora, idp, id_estado, estado, anul in pasado:
        tipo = _clasificar(id_estado, estado, anul)
        if tipo == "reagenda":
            continue
        if nombre:
            nombres[idpac] = nombre
        key = (idpac, fecha, idp)
        u = unidades.setdefault(key, {"tipo": tipo, "hora": hora})
        orden = {"atendida": 3, "no_show": 2, "anulada": 1, "otra": 0}
        if orden[tipo] > orden[u["tipo"]]:
            u["tipo"] = tipo
            u["hora"] = hora

    # ── agregación por paciente ──
    por_pac: dict[int, dict] = {}
    for (idpac, fecha, idp), u in unidades.items():
        p = por_pac.setdefault(idpac, {
            "id_paciente": idpac, "solicitudes": 0, "no_shows": 0,
            "atendidas": 0, "anuladas": 0, "ultima_inasistencia": None,
            "por_prof": {},
        })
        p["solicitudes"] += 1
        pp = p["por_prof"].setdefault(idp, {"id_profesional": idp,
                                            "profesional": _prof_nombre(idp),
                                            "no_shows": 0, "solicitudes": 0})
        pp["solicitudes"] += 1
        if u["tipo"] == "no_show":
            p["no_shows"] += 1
            pp["no_shows"] += 1
            if p["ultima_inasistencia"] is None or fecha > p["ultima_inasistencia"]:
                p["ultima_inasistencia"] = fecha
        elif u["tipo"] == "atendida":
            p["atendidas"] += 1
        elif u["tipo"] == "anulada":
            p["anuladas"] += 1

    # próxima cita vigente por paciente (para actuar: confirmar antes que falte)
    proximas: dict[int, dict] = {}
    for idpac, fecha, hora, idp in futuro:
        actual = proximas.get(idpac)
        if actual is None or (fecha, hora) < (actual["fecha"], actual["hora"]):
            proximas[idpac] = {"fecha": fecha, "hora": hora,
                               "profesional": _prof_nombre(idp)}

    ranking = []
    for idpac, p in por_pac.items():
        if p["no_shows"] < max(1, min_no_shows):
            continue
        p["paciente"] = nombres.get(idpac) or f"Paciente {idpac}"
        p["tasa"] = round(p["no_shows"] / p["solicitudes"] * 100) if p["solicitudes"] else 0
        p["por_prof"] = sorted(p["por_prof"].values(),
                               key=lambda x: (-x["no_shows"], -x["solicitudes"]))
        p["proxima_cita"] = proximas.get(idpac)
        ranking.append(p)
    ranking.sort(key=lambda p: (-p["no_shows"], -p["tasa"], -p["solicitudes"]))
    ranking = ranking[:limite]

    # ── KPIs globales de la ventana (con el filtro de profesional aplicado) ──
    total_unidades = len(unidades)
    total_ns = sum(1 for u in unidades.values() if u["tipo"] == "no_show")
    hace_30 = (hoy - timedelta(days=30)).isoformat()
    ns_30d = sum(1 for (idpac, fecha, idp), u in unidades.items()
                 if u["tipo"] == "no_show" and fecha >= hace_30)
    reincidentes = sum(1 for p in por_pac.values() if p["no_shows"] >= 2)
    con_prox = sum(1 for p in ranking if p["proxima_cita"])

    # profesionales presentes en la ventana COMPLETA (sin filtro) para el selector
    with db() as c:
        profs_rows = c.execute(
            """SELECT id_profesional, COUNT(*) FROM ausentismo_citas
               WHERE fecha >= ? AND fecha < ? AND id_estado = 8 AND anulacion = 0
                     AND id_profesional IS NOT NULL
               GROUP BY id_profesional ORDER BY COUNT(*) DESC""",
            (desde, hoy_iso)).fetchall()
    profesionales = [{"id_profesional": idp, "profesional": _prof_nombre(idp),
                      "no_shows": n} for idp, n in profs_rows]

    raw_state = system_state_get(_STATE_KEY)
    with db() as c:
        row = c.execute("SELECT MIN(fecha), MAX(fecha), COUNT(*) FROM ausentismo_citas").fetchone()

    return {
        "kpis": {
            "no_shows": total_ns,
            "no_shows_30d": ns_30d,
            "tasa_global": round(total_ns / total_unidades * 100, 1) if total_unidades else 0,
            "reincidentes": reincidentes,
            "en_ranking_con_proxima": con_prox,
            "solicitudes": total_unidades,
        },
        "ranking": ranking,
        "profesionales": profesionales,
        "filtro": {"dias": dias, "id_prof": id_prof, "min_no_shows": min_no_shows},
        "recoleccion": {
            "ultimo_barrido": json.loads(raw_state) if raw_state else None,
            "cobertura_desde": row[0], "cobertura_hasta": row[1], "citas_locales": row[2],
        },
    }


def historial_paciente(id_paciente: int, dias: int = 365) -> dict:
    """Detalle cronológico de citas de un paciente (para la fila expandida)."""
    from session import db
    from medilink import PROFESIONALES
    hoy = datetime.now(_CLT).date()
    desde = (hoy - timedelta(days=dias)).isoformat()
    ensure_ausentismo_table()
    with db() as c:
        rows = c.execute(
            """SELECT fecha, hora, id_profesional, id_estado, estado_cita, anulacion, paciente
               FROM ausentismo_citas
               WHERE id_paciente = ? AND fecha >= ?
               ORDER BY fecha ASC, hora ASC""",
            (id_paciente, desde)).fetchall()
    citas = []
    nombre = ""
    for fecha, hora, idp, id_estado, estado, anul, pac in rows:
        if pac:
            nombre = pac
        citas.append({
            "fecha": fecha, "hora": hora,
            "profesional": PROFESIONALES.get(idp, {}).get("nombre") or f"Prof. {idp}",
            "estado": estado or "", "tipo": _clasificar(id_estado, estado, anul),
            "futura": fecha >= hoy.isoformat(),
        })
    return {"id_paciente": id_paciente, "paciente": nombre, "citas": citas}
