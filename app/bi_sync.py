"""
BI v2 — sync incremental de atenciones desde Medilink.

Reemplaza el ETL externo del proyecto health-bi-project. Vive en el bot,
escribe directo a sessions.db (cifrado), corre via APScheduler diario.

Estrategia:
- Por chunks mensuales con `q={"fecha":{"gte":"YYYY-MM-DD","lte":"YYYY-MM-DD"}}`
- Sigue cursor `links.next` para paginar.
- Reintenta 429 con backoff exponencial.
- Skip mes-profesional si ya está sincronizado (incremental real).
- Marca cobrado_caja=1 si abonado>=total y abonado>0 (proxy razonable hasta
  tener cruce con módulo Cajas). Esto se afina después.

Uso:
    await sync_profesional(id_profesional=1, desde="2024-01-01")
    await sync_todos(desde="2024-01-01")
    await sync_diario()  # solo el día anterior (cron)
"""
import asyncio
import json
from datetime import date, datetime, timedelta
from typing import AsyncIterator

import httpx

from config import MEDILINK_BASE_URL
from medilink import HEADERS, PROFESIONALES
from session import (
    upsert_bi_atenciones,
    get_bi_fechas_sincronizadas,
    log_bi_sync,
    _conn as _bi_conn,
)
import logging

log = logging.getLogger("bot")

ATEN_URL = f"{MEDILINK_BASE_URL}/atenciones"
PAGOS_URL = f"{MEDILINK_BASE_URL}/pagos"
SYNC_LOCK = asyncio.Lock()
PAGOS_LOCK = asyncio.Lock()


def _month_chunks(desde: date, hasta: date):
    """(gte, lte) iso por mes."""
    cur = date(desde.year, desde.month, 1)
    while cur <= hasta:
        nxt_year = cur.year + (cur.month // 12)
        nxt_month = (cur.month % 12) + 1
        nxt = date(nxt_year, nxt_month, 1)
        last = nxt - timedelta(days=1)
        yield max(cur, desde).isoformat(), min(last, hasta).isoformat()
        cur = nxt


async def _fetch_atenciones_rango(cli: httpx.AsyncClient, gte: str, lte: str
                                   ) -> AsyncIterator[list[dict]]:
    """Genera lotes de atenciones para el rango de fechas. Maneja paginación
    con backoff agresivo (Medilink rate-limita fuerte en histórico)."""
    q = {"fecha": {"gte": gte, "lte": lte}}
    pq = {"q": json.dumps(q, separators=(",", ":"))}
    next_url: str | None = ATEN_URL
    first = True
    while next_url:
        for attempt in range(12):  # antes 6
            try:
                if first:
                    r = await cli.get(next_url, params=pq, headers=HEADERS)
                else:
                    r = await cli.get(next_url, headers=HEADERS)
            except Exception as e:
                log.warning("bi_sync %s..%s attempt=%d excepción: %s",
                            gte, lte, attempt, e)
                await asyncio.sleep(min(60, 3 + attempt * 5))
                continue
            if r.status_code == 200:
                d = r.json()
                yield d.get("data", []) or []
                links = d.get("links", {}) if isinstance(d, dict) else {}
                next_url = links.get("next")
                first = False
                break
            if r.status_code == 429:
                await asyncio.sleep(min(90, 5 + attempt * 8))
                continue
            log.warning("bi_sync %s..%s HTTP %s — abort chunk", gte, lte, r.status_code)
            return
        else:
            log.warning("bi_sync %s..%s sin éxito tras 12 intentos", gte, lte)
            return
        await asyncio.sleep(0.8)  # antes 0.25


async def sync_rango(desde: str = "2024-01-01", hasta: str | None = None,
                     id_profesional: int | None = None,
                     force: bool = False) -> dict:
    """Sincroniza atenciones de Medilink al BI local entre fechas.

    Si id_profesional es None, sincroniza todos los profesionales (un mismo fetch
    sirve para todos porque /atenciones devuelve todo el centro). Si está dado,
    el storage solo guarda los de ese profesional (ahorra espacio por single-prof).

    Skip incremental: si ya hay data para todos los días del rango, no re-fetchea
    a menos que force=True.
    """
    async with SYNC_LOCK:
        try:
            d_desde = date.fromisoformat(desde)
            d_hasta = date.fromisoformat(hasta) if hasta else date.today()
        except ValueError:
            return {"ok": False, "error": "fechas inválidas"}

        inicio = datetime.utcnow().isoformat()

        # Si pidiendo profesional específico, chequeamos qué fechas ya tiene
        skip_fechas: set[str] = set()
        if not force and id_profesional is not None:
            skip_fechas = get_bi_fechas_sincronizadas(id_profesional)

        total_recs = 0
        total_aten = 0
        total_err = 0
        chunks = list(_month_chunks(d_desde, d_hasta))
        async with httpx.AsyncClient(timeout=30) as cli:
            for gte, lte in chunks:
                # Si todas las fechas del mes están en skip_fechas y no es force, skip
                if id_profesional is not None and not force:
                    todas = set()
                    cur = date.fromisoformat(gte)
                    end = date.fromisoformat(lte)
                    while cur <= end:
                        if cur.weekday() != 6:  # skip domingos
                            todas.add(cur.isoformat())
                        cur += timedelta(days=1)
                    if todas and todas.issubset(skip_fechas):
                        log.info("bi_sync skip mes %s..%s (prof=%s ya sync)",
                                 gte, lte, id_profesional)
                        continue
                log.info("bi_sync chunk %s..%s prof=%s", gte, lte, id_profesional or "all")
                pages = 0
                async for records in _fetch_atenciones_rango(cli, gte, lte):
                    pages += 1
                    total_recs += len(records)
                    if id_profesional is not None:
                        records = [r for r in records
                                   if r.get("id_profesional") == id_profesional]
                    n = upsert_bi_atenciones(records)
                    total_aten += n
                if pages == 0:
                    total_err += 1
                # Pausa entre chunks para no saturar Medilink
                await asyncio.sleep(2)

        fin = datetime.utcnow().isoformat()
        log_bi_sync("rango", id_profesional or 0, f"{desde}..{hasta or date.today()}",
                    inicio, fin, total_aten, total_err, total_err == 0)
        log.info("bi_sync done: recs=%d guardados=%d err=%d (prof=%s)",
                 total_recs, total_aten, total_err, id_profesional or "all")
        return {"ok": True, "recs_vistos": total_recs, "guardados": total_aten,
                "errores": total_err, "chunks": len(chunks)}


async def sync_profesional(id_profesional: int, desde: str = "2024-01-01",
                            hasta: str | None = None, force: bool = False) -> dict:
    """Wrapper para sincronizar un profesional específico."""
    return await sync_rango(desde=desde, hasta=hasta,
                            id_profesional=id_profesional, force=force)


async def sync_todos(desde: str = "2024-01-01") -> dict:
    """Full sync de todos los profesionales (un solo fetch global)."""
    return await sync_rango(desde=desde, hasta=None, id_profesional=None, force=False)


async def sync_diario() -> dict:
    """Cron diario: refresca el día anterior y hoy. Sin filtro por profesional
    (un fetch trae todo el centro)."""
    hoy = date.today()
    ayer = hoy - timedelta(days=1)
    return await sync_rango(desde=ayer.isoformat(), hasta=hoy.isoformat(),
                            id_profesional=None, force=True)


def cobertura_validacion(id_profesional: int, mes: str) -> dict:
    """Para un profesional × mes: total atenciones, total facturado vs cobrado.
    Permite detectar atenciones-fantasma. mes en formato YYYY-MM."""
    inicio = f"{mes}-01"
    next_y = int(mes[:4]) + (int(mes[5:7]) // 12)
    next_m = (int(mes[5:7]) % 12) + 1
    fin = f"{next_y}-{next_m:02d}-01"
    with _bi_conn() as c:
        row = c.execute("""
            SELECT COUNT(*)            AS n,
                   SUM(total)          AS sum_total,
                   SUM(abonado)        AS sum_abonado,
                   SUM(deuda)          AS sum_deuda,
                   SUM(CASE WHEN finalizado=1 THEN 1 ELSE 0 END) AS n_finalizadas,
                   SUM(CASE WHEN bloqueado=1 THEN 1 ELSE 0 END)  AS n_bloqueadas,
                   SUM(CASE WHEN total>0 AND abonado>=total THEN 1 ELSE 0 END) AS n_pagadas,
                   SUM(CASE WHEN total>0 AND (abonado IS NULL OR abonado<total) THEN 1 ELSE 0 END) AS n_no_pagadas
            FROM bi_atenciones
            WHERE id_profesional=? AND fecha>=? AND fecha<?
        """, (id_profesional, inicio, fin)).fetchone()
    return dict(row) if row else {}


def stats_profesional(id_profesional: int, desde: str = "2024-01-01") -> dict:
    """KPIs agregados de un profesional.

    Conteo de pacientes/día y mes = PACIENTES ÚNICOS (DISTINCT id_paciente)
    desde bi_atenciones — un paciente con 3 prestaciones el mismo día cuenta
    como 1 paciente, no 3. Incluye controles ($0).

    Monto cobrado = SUM bi_pagos_caja (caja real Medilink).
    Monto facturado = SUM bi_atenciones.total (lo registrado en sistema)."""
    from collections import defaultdict
    # id_profesional=0 (o None) → TODO EL CENTRO consolidado.
    todos = id_profesional in (0, None)
    filtro = "" if todos else "id_profesional=? AND "
    args: tuple = (desde,) if todos else (id_profesional, desde)
    with _bi_conn() as c:
        # Pacientes únicos por día desde bi_atenciones
        atens = c.execute(f"""
            SELECT fecha, id_paciente, total
            FROM bi_atenciones
            WHERE {filtro}fecha>=?
        """, args).fetchall()
        # Cobrado real desde bi_pagos_caja
        pagos = c.execute(f"""
            SELECT fecha, id_paciente, monto FROM bi_pagos_caja
            WHERE {filtro}fecha>=?
        """, args).fetchall()

    # PAGARON: pacientes únicos por día/mes desde bi_pagos_caja (CSV oficial)
    pacientes_dia_pago: dict = defaultdict(set)
    pacientes_mes_pago: dict = defaultdict(set)
    monto_cobrado_mes: dict = defaultdict(int)
    for r in pagos:
        f = (r["fecha"] or "")[:10]
        if not f:
            continue
        m = f[:7]
        pacientes_mes_pago[m].add(r["id_paciente"])
        pacientes_dia_pago[f].add(r["id_paciente"])
        monto_cobrado_mes[m] += int(r["monto"] or 0)

    # ATENDIDOS: pacientes únicos por día/mes desde bi_atenciones (incluye controles $0)
    pacientes_dia_atend: dict = defaultdict(set)
    pacientes_mes_atend: dict = defaultdict(set)
    monto_total_mes: dict = defaultdict(int)
    for r in atens:
        f = (r["fecha"] or "")[:10]
        if not f:
            continue
        m = f[:7]
        if r["id_paciente"]:
            pacientes_dia_atend[f].add(r["id_paciente"])
            pacientes_mes_atend[m].add(r["id_paciente"])
        monto_total_mes[m] += int(r["total"] or 0)

    # Día: dict simple para retro-compat (cuenta pacientes que PAGARON)
    por_dia: dict = {f: len(s) for f, s in pacientes_dia_pago.items()}
    # Día detallado: separa atendidos vs pagaron
    por_dia_detalle: dict = {}
    for f in set(list(pacientes_dia_pago.keys()) + list(pacientes_dia_atend.keys())):
        atend_n = len(pacientes_dia_atend.get(f, set()))
        pago_n = len(pacientes_dia_pago.get(f, set()))
        por_dia_detalle[f] = {
            "atendidos": atend_n,
            "pagaron": pago_n,
            "controles_gratis": max(0, atend_n - pago_n),
        }
    por_dow: dict = defaultdict(list)
    por_mes: dict = {}
    todos_meses = set(list(pacientes_mes_pago.keys()) + list(pacientes_mes_atend.keys()) +
                      list(monto_total_mes.keys()))
    for m in todos_meses:
        atend_n = len(pacientes_mes_atend.get(m, set()))
        pago_n = len(pacientes_mes_pago.get(m, set()))
        por_mes[m] = {
            "atend": pago_n,  # retro-compat
            "atend_pagadas": pago_n,
            "atendidos_total": atend_n,
            "pagaron": pago_n,
            "controles_gratis": max(0, atend_n - pago_n),
            "monto_total": monto_total_mes.get(m, 0),
            "monto_cobrado": monto_cobrado_mes.get(m, 0),
        }

    # backfill días vacíos
    try:
        start = date.fromisoformat(desde)
    except ValueError:
        start = date(2024, 1, 1)
    end = date.today()
    d = start
    while d <= end:
        f = d.isoformat()
        por_dia.setdefault(f, 0)
        d += timedelta(days=1)

    for f, n in por_dia.items():
        if n > 0:
            dt = date.fromisoformat(f)
            por_dow[dt.weekday()].append(n)

    dow_stats = {}
    for w in range(7):
        vals = sorted(por_dow.get(w, []))
        if not vals:
            dow_stats[w] = {"avg": 0, "median": 0, "min": 0, "max": 0, "p90": 0, "n": 0}
        else:
            n_v = len(vals)
            p90_idx = max(0, int(n_v * 0.9) - 1) if n_v >= 10 else n_v - 1
            dow_stats[w] = {
                "avg": round(sum(vals) / n_v, 2),
                "median": vals[n_v // 2],
                "min": vals[0], "max": vals[-1],
                "p90": vals[p90_idx], "n": n_v,
            }

    total_atend = sum(v["atend"] for v in por_mes.values())
    total_facturado = sum(v["monto_total"] for v in por_mes.values())
    total_cobrado = sum(v["monto_cobrado"] for v in por_mes.values())
    n_meses = max(1, len(por_mes))
    avg_atend_mes = total_atend / n_meses
    avg_cobrado_mes = total_cobrado / n_meses
    tarifa_real = total_cobrado / total_atend if total_atend else 0
    cobertura_pct = round(100 * total_cobrado / total_facturado, 1) if total_facturado else 0
    dias_trab = sum(1 for v in por_dia.values() if v > 0)

    # Proyección lineal últimos 6 meses
    meses_ord = sorted(por_mes.keys())
    ult6 = meses_ord[-6:] if len(meses_ord) >= 6 else meses_ord
    proyeccion = {}
    if len(ult6) >= 2:
        ys = [por_mes[m]["atend"] for m in ult6]
        xs = list(range(len(ys)))
        n_x = len(xs)
        mean_x = sum(xs) / n_x
        mean_y = sum(ys) / n_x
        num = sum((xs[i] - mean_x) * (ys[i] - mean_y) for i in range(n_x))
        den = sum((xs[i] - mean_x) ** 2 for i in range(n_x))
        slope = num / den if den else 0
        intercept = mean_y - slope * mean_x
        last_m = date.fromisoformat(meses_ord[-1] + "-01") if meses_ord else date.today()
        for k in range(1, 7):
            yr = last_m.year + ((last_m.month + k - 1) // 12)
            mo = ((last_m.month + k - 1) % 12) + 1
            key = f"{yr}-{mo:02d}"
            est = max(0, round(intercept + slope * (n_x - 1 + k)))
            proyeccion[key] = {"atend": est, "ingreso": round(est * tarifa_real)}

    prof_info = PROFESIONALES.get(id_profesional, {})
    if todos:
        nombre_prof = "Centro Médico Carampangue"
        esp_prof = f"Consolidado · {len(PROFESIONALES)} profesionales"
    else:
        nombre_prof = prof_info.get("nombre", f"Profesional {id_profesional}")
        esp_prof = prof_info.get("especialidad", "")
    # NOTA: post-procesado en main.py inyecta caja_real por mes desde bi_pagos_caja
    return {
        "fecha_actualizacion": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "fuente": "bi_atenciones (Medilink × validación cobrado)",
        "por_dia_detalle": por_dia_detalle,
        "id_profesional": id_profesional,
        "nombre_profesional": nombre_prof,
        "especialidad": esp_prof,
        "tarifa_real_promedio": round(tarifa_real),
        "cobertura_pct": cobertura_pct,
        "por_dia": por_dia,
        "por_mes": por_mes,  # incluye atend, atendidos_total, pagaron, controles_gratis, monto_total, monto_cobrado
        "por_dow": dow_stats,
        "proyeccion": proyeccion,
        "kpis": {
            "total_atend": total_atend,
            "total_facturado": total_facturado,
            "total_cobrado": total_cobrado,
            "atend_avg_mes": round(avg_atend_mes, 1),
            "ing_avg_mes": round(avg_cobrado_mes),
            "tarifa_real_promedio": round(tarifa_real),
            "cobertura_pct": cobertura_pct,
            "dias_con_atencion": dias_trab,
            "n_meses": n_meses,
        },
    }


# ════════════════════════════════════════════════════════════════════════════
# /pagos sync — fuente PRIMARIA de ingreso real (módulo Cajas Medilink)
# ════════════════════════════════════════════════════════════════════════════

def _build_next_cursor_url(current_url: str, last_records: int) -> str | None:
    """Decodifica cursor= y construye uno con page+1.

    Medilink devuelve links.current pero NO links.next. Hay que construir el
    next decodificando el cursor base64 → JSON → incrementando "page".
    Retorna None si la página actual trajo menos del limit (fin del stream).
    """
    if not current_url or "cursor=" not in current_url:
        return None
    try:
        import base64
        from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
        parts = urlparse(current_url)
        qs = parse_qs(parts.query)
        cursor_b64 = (qs.get("cursor") or [""])[0]
        raw = base64.b64decode(cursor_b64).decode("utf-8", errors="ignore")
        brace = raw.find("{")
        if brace < 0:
            return None
        prefix = raw[:brace]
        payload = json.loads(raw[brace:])
        limit = int(payload.get("limit") or last_records or 50)
        if last_records and last_records < limit:
            return None  # última página
        payload["page"] = int(payload.get("page", 0)) + 1
        new_raw = prefix + json.dumps(payload, separators=(",", ":"))
        new_cursor = base64.b64encode(new_raw.encode("utf-8")).decode("ascii")
        qs["cursor"] = [new_cursor]
        new_query = urlencode({k: v[0] for k, v in qs.items()})
        return urlunparse(parts._replace(query=new_query))
    except Exception:
        return None


PAGOS_PAGE_CAP = 50   # Medilink tope duro por respuesta en /pagos


async def _fetch_pagos_dia(cli: httpx.AsyncClient, fecha: str) -> AsyncIterator[list[dict]]:
    """Pagina /pagos?q={fecha_recepcion:eq fecha} POR ID (id < menor_visto).

    El cursor de Medilink viene FIRMADO (`v2.<payload>.<firma>`, estilo JWT) y la
    firma se valida server-side → no se puede forjar `page+1` (devuelve 0). El
    endpoint tampoco acepta page/limit/offset (400) y NUNCA da `links.next`: cada
    respuesta trae solo los 50 ids más altos que matchean. Resultado del bug viejo:
    todo día con >50 pagos perdía el resto, dejando los totales por profesional
    cortos vs Medilink (sin_asignar=0 porque ni se sincronizaban).

    Fix determinista: pedir la 1ª página, y mientras venga llena (==CAP) re-pedir
    con `id: {lt: menor_id_de_la_página}` hasta que una página venga < CAP. El
    upsert es idempotente por pago_id, así que cualquier solape es inofensivo.
    """
    last_min_id: int | None = None
    while True:
        filt: dict = {"fecha_recepcion": {"eq": fecha}}
        if last_min_id is not None:
            filt["id"] = {"lt": last_min_id}
        pq = {"q": json.dumps(filt, separators=(",", ":"))}
        data: list[dict] | None = None
        for attempt in range(12):
            try:
                r = await cli.get(PAGOS_URL, params=pq, headers=HEADERS)
            except Exception as e:
                log.warning("pagos %s attempt=%d excepción: %s", fecha, attempt, e)
                await asyncio.sleep(min(60, 3 + attempt * 5))
                continue
            if r.status_code == 200:
                d = r.json()
                data = d if isinstance(d, list) else (d.get("data", []) or [])
                break
            if r.status_code == 429:
                await asyncio.sleep(min(90, 5 + attempt * 8))
                continue
            log.warning("pagos %s HTTP %s — abort día", fecha, r.status_code)
            return
        else:
            log.warning("pagos %s sin éxito tras 12 intentos", fecha)
            return
        if not data:
            return
        yield data
        ids = [p.get("id") for p in data if p.get("id") is not None]
        if len(data) < PAGOS_PAGE_CAP or not ids:
            return
        new_min = min(ids)
        if last_min_id is not None and new_min >= last_min_id:
            return  # sin progreso → corta para no loopear
        last_min_id = new_min
        await asyncio.sleep(0.8)


_AREA_POR_PROF = {
    1: "med", 73: "med", 13: "med", 23: "med", 60: "med", 61: "med",
    65: "med", 64: "med", 79: "med",
    68: "eco", 80: "eco",
    55: "dent", 72: "dent", 66: "dent", 75: "dent", 69: "dent", 76: "dent",
    59: "maso", 77: "kine", 21: "kine", 52: "nutri", 74: "psico", 49: "psico",
    70: "fono", 67: "matrona", 56: "podo", 78: "psiq",
}

_MONTO_AREA_CACHE: dict = {"ts": 0.0, "tabla": {}}


def _tabla_monto_area(c) -> dict[int, str]:
    """Montos-arancel que identifican especialidad (idea del dueño 2026-08-20):
    los copagos Fonasa son valores fijos por prestación — $20.980 es psicología
    el 100% de las veces, $15.130 medicina 99%, $9.540 nutrición 96%, etc.
    Se construye DINÁMICO desde la distribución real de bi_pagos_caja (120
    días, pureza ≥95%, n≥15) para que sobreviva a cambios de arancel. Los
    montos redondos particulares ($30.000, $40.000...) no alcanzan pureza y
    quedan fuera solos. Cache 24 h por proceso."""
    import time
    if time.time() - _MONTO_AREA_CACHE["ts"] < 86400 and _MONTO_AREA_CACHE["tabla"]:
        return _MONTO_AREA_CACHE["tabla"]
    tabla: dict[int, str] = {}
    try:
        from collections import defaultdict
        rows = c.execute(
            "SELECT monto, id_profesional, COUNT(*) FROM bi_pagos_caja "
            "WHERE fecha >= date('now','-120 days') "
            "AND id_profesional IS NOT NULL GROUP BY monto, id_profesional"
        ).fetchall()
        por_monto: dict = defaultdict(lambda: defaultdict(int))
        for m, pid, n in rows:
            a = _AREA_POR_PROF.get(pid)
            if a and m:
                por_monto[int(m)][a] += n
        for m, areas in por_monto.items():
            tot = sum(areas.values())
            top_a, top_n = max(areas.items(), key=lambda kv: kv[1])
            if tot >= 15 and top_n / tot >= 0.95:
                tabla[m] = top_a
    except Exception:
        return _MONTO_AREA_CACHE["tabla"] or {}
    _MONTO_AREA_CACHE.update(ts=time.time(), tabla=tabla)
    return tabla


def _norm_nombre(s: str) -> str:
    import unicodedata
    s = unicodedata.normalize("NFD", (s or "").lower())
    return " ".join("".join(ch for ch in s
                            if unicodedata.category(ch) != "Mn").split())


def _nombres_calzan(a: str, b: str) -> bool:
    """True si los nombres NORMALIZADOS son iguales o los tokens de uno son
    subconjunto del otro (≥2 tokens). Recepción suele omitir el segundo
    nombre: 'Claudio Lobos Salazar' vs Medilink 'Claudio Ignacio  Lobos
    Salazar' (doble espacio incluido) — la igualdad exacta que se usaba antes
    fallaba ahí y el pago caía a la cascada heurística (caso 37477, $20.980
    de Jorge colgado a Abarca)."""
    if not a or not b:
        return False
    if a == b:
        return True
    ta, tb = set(a.split()), set(b.split())
    chico, grande = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    return len(chico) >= 2 and chico <= grande


def _resolver_profesional_pagos_cmc(c, fecha_iso: str, nombre_pago: str, monto: int
                                     ) -> int | None:
    """NIVEL 0.5 — pagos_cmc (verdad humana: profesional asignado por recepción).

    Responsabilidad ÚNICA: resolver `id_profesional`, nunca `atencion_id`. Antes
    (commit b090e9c0, 2026-06-12) esta lógica vivía inline en
    `_resolver_profesional_pago` y hacía `return profs_cmc.pop(), None` apenas
    encontraba match — cortaba la ejecución ANTES de llegar a la cascada que
    busca `atencion_id`, dejando huérfano cualquier pago que matcheara acá (el
    caso más común y creciente: 0% en abril 2026 → 87% en junio → 93% en julio,
    ver docs/LIBRO_DE_LA_VERDAD.md sección 3). Separado en su propia función
    para que quien la llame NUNCA pueda repetir ese error por accidente: esta
    función no tiene forma de devolver un atencion_id porque no lo busca.

    pagos_cmc está en la misma sessions.db que bi_atenciones, accesible con el
    mismo cursor `c`. Cobertura esperada: ~77% de los pagos de junio.
    Retorna `id_profesional` solo cuando el match es inequívoco (un solo
    profesional ese día para ese paciente, o desempate de monto con gap
    claro). `None` si no hay match o es ambiguo — el llamador decide si sigue
    con la cascada heurística de `bi_atenciones` para inferir profesional.
    """
    if not nombre_pago or not fecha_iso:
        return None
    try:
        # Match de nombre en Python (no en SQL): normaliza tildes/espacios y
        # acepta subconjunto de tokens — la igualdad exacta perdía los casos
        # donde recepción escribe el nombre más corto que Medilink.
        dia_rows = c.execute(
            """
            SELECT id_profesional,
                   COALESCE(copago, 0)      AS copago,
                   COALESCE(bonificacion, 0) AS bonificacion,
                   paciente_nombre
            FROM   pagos_cmc
            WHERE  fecha = ?
              AND  id_profesional IS NOT NULL
            """,
            (fecha_iso,),
        ).fetchall()
        objetivo = _norm_nombre(nombre_pago)
        cmc_rows = [r for r in dia_rows
                    if _nombres_calzan(_norm_nombre(r[3]), objetivo)]

        if not cmc_rows:
            return None
        profs_cmc = set(r[0] for r in cmc_rows)

        if len(profs_cmc) == 1:
            # Un único profesional ese día para ese paciente → match claro.
            return profs_cmc.pop()

        # Varios profesionales ese día → DESEMPATE 1 por ARANCEL (idea del
        # dueño 2026-08-20): si el monto del pago es un arancel que identifica
        # especialidad (tabla dinámica ≥95% pureza) y UNO solo de los
        # profesionales del día es de esa área → ese es.
        area_monto = _tabla_monto_area(c).get(int(monto or 0))
        if area_monto:
            candidatos = [p for p in profs_cmc
                          if _AREA_POR_PROF.get(p) == area_monto]
            if len(candidatos) == 1:
                return candidatos[0]

        # DESEMPATE 2 por monto EXACTO: si el copago+bonificacion registrado
        # por recepción calza AL PESO con el monto del pago para UN solo
        # profesional del día, es él. Antes era "distancia más cercana con
        # gap" — y la distancia ENGAÑA porque el copago de mesón casi nunca
        # coincide con el monto de caja (caso Sebastián Peña 22-08: pago
        # $15.130 de Olavarría empujado a Millán porque su copago $20.000
        # quedaba "más cerca" que el $2.360 de Olavarría). Exacto o nada.
        exactos = {r[0] for r in cmc_rows if (r[1] + r[2]) == monto}
        if len(exactos) == 1:
            return exactos.pop()
        # Ambiguo → caer a heurística
        return None
    except Exception:
        return None  # tabla ausente en entorno de test o error inesperado


def _resolver_atencion_id(c, pid, fecha_iso: str, monto: int,
                           id_profesional: int | None = None
                           ) -> tuple[int | None, int | None]:
    """Cascada heurística que busca la atención de Medilink (`atencion_id`)
    que corresponde a un pago, cruzando contra `bi_atenciones`.

    Si `id_profesional` viene dado (ya resuelto por NIVEL 0/0.5), la búsqueda
    se ACOTA a las atenciones de ESE profesional — nunca se usa para inferir
    un profesional distinto. Esto es lo que evita reabrir el bug que el NIVEL
    0.5 corrigió (pago cruzado al profesional equivocado cuando el paciente
    tenía atenciones de varios profesionales el mismo día — memoria
    `cmc-comparador-y-atribucion-2026-06-12`).

    Si `id_profesional` es `None`, la cascada además INFIERE el profesional a
    partir del match (comportamiento histórico, usado cuando NIVEL 0/0.5 no
    resolvieron nada).

    1. Mismo día + monto exacto match por atención.total
    2. Ventana ±60 días + monto exacto (atención más cercana en fecha)
    3. Ventana ±60 días + monto cercano
    4. Ventana ±60 días + atención con deuda > 0 (FIFO)
    5. Si paciente tiene un único profesional histórico → ese
    6. Fallback: atención más cercana ANTERIOR al pago (±60d antes, ±14d post)

    Retorna (id_profesional, atencion_id). El primer elemento es siempre
    `id_profesional` si vino dado (nunca lo cambia); si no vino dado, es lo
    que la cascada haya inferido (o `None` si nada matchea).
    """
    from datetime import date

    filtro_prof = "" if id_profesional is None else " AND id_profesional=?"
    params = (pid,) if id_profesional is None else (pid, id_profesional)

    # Atenciones con total > 0 (preferidas para matching por monto)
    rows = c.execute(
        f"SELECT atencion_id, id_profesional, total, abonado, deuda, fecha "
        f"FROM bi_atenciones WHERE id_paciente=?{filtro_prof} AND total>0 "
        f"ORDER BY fecha", params
    ).fetchall()
    # Todas las atenciones incluyendo total=0 (Medilink deja total=0 hasta que
    # se registra el cobro; son candidatas naturales para los pasos 5 y 5b).
    rows_incl_zero = c.execute(
        f"SELECT atencion_id, id_profesional, total, abonado, deuda, fecha "
        f"FROM bi_atenciones WHERE id_paciente=?{filtro_prof} "
        f"ORDER BY fecha", params
    ).fetchall()

    try:
        f_pago = date.fromisoformat(fecha_iso)
    except ValueError:
        return id_profesional, None

    if rows or rows_incl_zero:
        # 1. Mismo día + monto exacto
        same_day = [r for r in rows if r["fecha"] == fecha_iso and (r["total"] or 0) == monto]
        if same_day:
            with_debt = [r for r in same_day if (r["deuda"] or 0) > 0]
            if with_debt:
                r = with_debt[0]
                return r["id_profesional"], r["atencion_id"]
            unpaid = [r for r in same_day if (r["abonado"] or 0) < (r["total"] or 0)]
            if unpaid:
                r = unpaid[0]
                return r["id_profesional"], r["atencion_id"]
            r = same_day[0]
            return r["id_profesional"], r["atencion_id"]

        # Construir ventana ±60d sobre total>0 (pasos 2/3) y sobre todos (pasos 5/5b).
        en_ventana = []
        for r in rows:
            try:
                f_at = date.fromisoformat(r["fecha"])
            except (ValueError, TypeError):
                continue
            delta_d = abs((f_pago - f_at).days)
            if delta_d <= 60:
                en_ventana.append((delta_d, r))
        en_ventana.sort(key=lambda x: x[0])

        en_ventana_all = []
        for r in rows_incl_zero:
            try:
                f_at = date.fromisoformat(r["fecha"])
            except (ValueError, TypeError):
                continue
            delta_d = abs((f_pago - f_at).days)
            if delta_d <= 60:
                en_ventana_all.append((delta_d, r))
        en_ventana_all.sort(key=lambda x: x[0])

        # 2. Ventana ±60d + monto exacto.
        # Desempate: preferir atenciones con abonado<total (saldo pendiente) sobre
        # las ya pagadas — un pago normalmente salda la atención impaga, no la ya
        # cobrada. Solo si todas están pagadas, elegir por cercanía de fecha.
        monto_exacto = [t for t in en_ventana if (t[1]["total"] or 0) == monto]
        if monto_exacto:
            with_pending = [t for t in monto_exacto
                            if (t[1]["abonado"] or 0) < (t[1]["total"] or 0)]
            candidate = with_pending[0][1] if with_pending else monto_exacto[0][1]
            return candidate["id_profesional"], candidate["atencion_id"]

        # 3. Ventana ±60d + monto cercano (delta < 5%)
        if en_ventana:
            ranked = sorted(
                en_ventana,
                key=lambda t: (abs((t[1]["total"] or 0) - monto), t[0])
            )
            best_delta = abs((ranked[0][1]["total"] or 0) - monto)
            if best_delta <= max(2000, monto * 0.05):
                r = ranked[0][1]
                return r["id_profesional"], r["atencion_id"]

        # 4. Atención con deuda > 0 anterior al pago, ventana ±60 días.
        # SIN ventana temporal una atención de hace 1 año con deuda residual
        # absorbe pagos recientes, asignándolos al profesional equivocado.
        deudoras = []
        for r in rows:
            if (r["deuda"] or 0) <= 0 or not r["fecha"] or r["fecha"] > fecha_iso:
                continue
            try:
                f_at = date.fromisoformat(r["fecha"])
            except (ValueError, TypeError):
                continue
            if (f_pago - f_at).days <= 60:
                deudoras.append(r)
        if deudoras:
            r = deudoras[-1]  # la más reciente dentro de la ventana
            return r["id_profesional"], r["atencion_id"]

        # 5. Si las atenciones DENTRO de ventana ±60d (incluyendo total=0) son de
        # un único profesional → ese. Se incluyen total=0 porque Medilink deja ese
        # valor hasta que se registra el cobro; ignorarlas causaba falsos "único prof"
        # (bug original: Angelo/Ernesto → Márquez/Abarca en vez de Quijano).
        # Al confirmar único prof, se prefiere la atención con total=0 como destino
        # del pago (es la pendiente de cobro); si no hay, la más cercana.
        rows_ventana_all = [r for _, r in en_ventana_all]
        profs = set(r["id_profesional"] for r in rows_ventana_all if r["id_profesional"])
        if len(profs) == 1 and rows_ventana_all:
            prof_unico = next(iter(profs))
            rows_ranked = sorted(rows_ventana_all, key=lambda r: abs(
                (date.fromisoformat(r["fecha"]) - f_pago).days
            ))
            zero_total = [r for r in rows_ranked if (r["total"] or 0) == 0]
            best = zero_total[0] if zero_total else rows_ranked[0]
            return prof_unico, best["atencion_id"]

        # 5b. Hay atenciones con total=0 en la ventana entre varios profesionales.
        # Un pago sin match exacto de monto probablemente salda una de estas
        # (Medilink aún no registró el monto). Elegir la más cercana al pago.
        zero_in_window = [r for _, r in en_ventana_all if (r["total"] or 0) == 0]
        if zero_in_window:
            zero_ranked = sorted(zero_in_window, key=lambda r: abs(
                (date.fromisoformat(r["fecha"]) - f_pago).days
            ))
            r = zero_ranked[0]
            return r["id_profesional"], r["atencion_id"]

    # 6. Fallback: TODAS las atenciones del paciente (incluyendo total=$0).
    # Necesario porque Medilink a veces deja total=$0 en /atenciones hasta
    # que se cobra el pago. Estrategia: priorizar atenciones ANTERIORES al
    # pago (el flujo natural es atención → pago), dentro de ±60 días. Solo
    # si no hay anteriores, considerar posteriores ±14d como último recurso.
    # rows_incl_zero ya tiene todas las atenciones; si está vacío (paciente sin
    # ninguna atención) la query está fuera del bloque if, así que la hacemos aquí.
    all_rows = rows_incl_zero if rows_incl_zero else c.execute(
        f"SELECT atencion_id, id_profesional, total, abonado, deuda, fecha "
        f"FROM bi_atenciones WHERE id_paciente=?{filtro_prof} "
        f"ORDER BY fecha", params
    ).fetchall()
    prev = []   # atenciones anteriores o iguales al pago (las más probables)
    post = []   # posteriores (raro, pero posible si Medilink registra después)
    for r in all_rows:
        if not r["fecha"] or not r["id_profesional"]:
            continue
        try:
            f_at = date.fromisoformat(r["fecha"])
        except (ValueError, TypeError):
            continue
        days = (f_pago - f_at).days  # positivo si atención fue antes
        if 0 <= days <= 60:
            prev.append((days, r))
        elif -14 <= days < 0:
            post.append((-days, r))
    if prev:
        # La más reciente anterior al pago, desempata por mayor total
        prev.sort(key=lambda x: (x[0], -(x[1]["total"] or 0)))
        r = prev[0][1]
        return r["id_profesional"], r["atencion_id"]
    if post:
        post.sort(key=lambda x: (x[0], -(x[1]["total"] or 0)))
        r = post[0][1]
        return r["id_profesional"], r["atencion_id"]

    return id_profesional, None


def _resolver_profesional_pago(c, pago: dict) -> tuple[int | None, int | None]:
    """Cruza un pago contra pagos_cmc / bi_atenciones para resolver
    (id_profesional, atencion_id). Orquesta 3 niveles, cada uno con
    responsabilidad única (ver docstrings de las funciones que llama):

    NIVEL 0   (override manual): `bi_pago_overrides`. Autoridad absoluta —
              retorna profesional Y atencion_id tal cual quedaron guardados.
    NIVEL 0.5 (`_resolver_profesional_pagos_cmc`): resuelve SOLO el
              profesional contra `pagos_cmc` (verdad de recepción).
    Cascada  (`_resolver_atencion_id`): busca el `atencion_id` en
              `bi_atenciones`. Si NIVEL 0.5 ya resolvió el profesional, la
              cascada se acota a las atenciones de ESE profesional (nunca lo
              reemplaza); si no, la cascada también infiere el profesional.

    IMPORTANTE — no reintroducir el bug de b090e9c0 (2026-06-12): esa versión
    hacía `return profs_cmc.pop(), None` en cuanto NIVEL 0.5 resolvía el
    profesional, cortando la ejecución antes de buscar `atencion_id` (dejó
    huérfano el 87-93% de los pagos de junio/julio 2026 — ver
    docs/LIBRO_DE_LA_VERDAD.md sección 3). Esta función SIEMPRE continúa
    hasta `_resolver_atencion_id`, resuelva o no el profesional antes.

    Retorna (id_profesional, atencion_id). `atencion_id` puede ser `None`
    incluso con `id_profesional` resuelto, si de verdad no hay ninguna
    atención de ese profesional que matchee (paciente sin atenciones
    registradas, por ejemplo) — un `None` genuino, no uno por atajo.
    """
    pago_id_for_override = pago.get("id")
    if pago_id_for_override:
        try:
            ov = c.execute(
                "SELECT id_profesional, atencion_id FROM bi_pago_overrides WHERE pago_id=?",
                (pago_id_for_override,)
            ).fetchone()
            if ov:
                return ov["id_profesional"], ov["atencion_id"]
        except Exception:
            pass

    pid = pago.get("id_paciente")
    fecha = pago.get("fecha_recepcion") or pago.get("fecha")
    monto = int(pago.get("monto_pago") or 0)
    if not pid or not fecha:
        return None, None
    fecha_iso = fecha[:10]

    nombre_pago = (pago.get("nombre_paciente") or "").strip()
    id_prof_0_5 = _resolver_profesional_pagos_cmc(c, fecha_iso, nombre_pago, monto)

    id_prof_cascada, atencion_id = _resolver_atencion_id(
        c, pid, fecha_iso, monto, id_profesional=id_prof_0_5
    )

    # id_prof_cascada es idéntico a id_prof_0_5 cuando este último no es None
    # (la cascada no lo cambia, solo lo usa como filtro); cuando id_prof_0_5
    # es None, id_prof_cascada es lo que la cascada haya inferido por su cuenta.
    return id_prof_cascada, atencion_id


def _upsert_pagos(records: list[dict]) -> tuple[int, int]:
    """Upsert pagos a bi_pagos_caja con id_profesional resuelto via cruce.
    Retorna (n_pagos_guardados, n_sin_profesional)."""
    if not records:
        return 0, 0
    n_ok = 0
    n_sin_prof = 0
    with _bi_conn() as c:
        # columna de nombre del paciente (capturada de /pagos) — idempotente.
        # La usa el módulo de Remuneraciones para listar atenciones con nombre.
        try:
            c.execute("ALTER TABLE bi_pagos_caja ADD COLUMN nombre_paciente TEXT")
        except Exception:
            pass
        for p in records:
            pago_id = p.get("id")
            if not pago_id:
                continue
            id_prof, atencion_id = _resolver_profesional_pago(c, p)
            if id_prof is None:
                n_sin_prof += 1
            c.execute("""
                INSERT INTO bi_pagos_caja
                  (pago_id, atencion_id, fecha, id_profesional, id_paciente,
                   monto, metodo_pago, n_folio, nombre_paciente, synced_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(pago_id) DO UPDATE SET
                  atencion_id=excluded.atencion_id,
                  fecha=excluded.fecha,
                  id_profesional=excluded.id_profesional,
                  id_paciente=excluded.id_paciente,
                  monto=excluded.monto,
                  metodo_pago=excluded.metodo_pago,
                  n_folio=excluded.n_folio,
                  nombre_paciente=excluded.nombre_paciente,
                  synced_at=datetime('now')
            """, (pago_id, atencion_id,
                   (p.get("fecha_recepcion") or "")[:10], id_prof,
                   p.get("id_paciente"), p.get("monto_pago"),
                   p.get("medio_pago"), p.get("numero_referencia"),
                   (p.get("nombre_paciente") or "").strip() or None))
            n_ok += 1
    return n_ok, n_sin_prof


def auditar_atribucion_pagos(desde: str, hasta: str) -> dict:
    """Cruza la atribución de profesional del espejo (bi_pagos_caja, matcher
    heurístico) contra el registro HUMANO de recepción (pagos_cmc). Dos
    señales, ambas solo-lectura (flag, jamás auto-corrige):

    1. `recepcion_mismo_dia`: recepción registró al paciente ese día con UN
       profesional inequívoco y el espejo dice otro. (Julio 2026: 96% de
       cobertura, 0 contradicciones.)
    2. `historial_unanime`: sin registro ese día, pero TODOS los registros del
       paciente en pagos_cmc (≥2, ventana 60 días hacia atrás) apuntan a un
       único profesional distinto al del espejo — habría cazado el caso María
       Melian (pago 36616, $150.000 colgado a Fredes siendo de Javiera).

    Los pagos con override manual (bi_pago_overrides) se saltan: ya son
    verdad nivel 0. El barrido dominical publica los flags por Telegram para
    que el dueño decida el override."""
    import unicodedata

    def _norm(s: str) -> str:
        s = unicodedata.normalize("NFD", (s or "").lower())
        return " ".join("".join(ch for ch in s
                                if unicodedata.category(ch) != "Mn").split())

    with _bi_conn() as c:
        bi = c.execute(
            "SELECT pago_id, fecha, monto, id_profesional, nombre_paciente, "
            "id_paciente FROM bi_pagos_caja WHERE fecha>=? AND fecha<=?",
            (desde, hasta)).fetchall()
        cmc = c.execute(
            "SELECT fecha, paciente_nombre, id_profesional FROM pagos_cmc "
            "WHERE id_profesional IS NOT NULL "
            "AND fecha>=date(?, '-60 days') AND fecha<=?",
            (desde, hasta)).fetchall()
        overrides = {r[0] for r in c.execute(
            "SELECT pago_id FROM bi_pago_overrides").fetchall()}

    mismo_dia: dict = {}
    historial: dict = {}
    for r in cmc:
        nom = _norm(r["paciente_nombre"])
        mismo_dia.setdefault((r["fecha"], nom), set()).add(r["id_profesional"])
        historial.setdefault(nom, []).append(r["id_profesional"])

    # Índice de citas ATENDIDAS por (fecha, id_paciente) — el árbitro supremo
    # (verificado 2026-08-20: 5/5 en el par kine y 16/16 desactivando falsas
    # alarmas de historial en mayo). Si la cita del día confirma al espejo,
    # el flag de historial se SUPRIME: el historial engaña con pacientes de
    # series kine porque recepción registra bajo el jefe (Leo).
    citas_atendidas: dict = {}
    try:
        with _bi_conn() as c4:
            for r in c4.execute(
                    "SELECT fecha, id_paciente, id_profesional FROM ausentismo_citas "
                    "WHERE estado_cita='Atendido' AND fecha>=? AND fecha<=?",
                    (desde, hasta)):
                citas_atendidas.setdefault((r["fecha"], r["id_paciente"]),
                                           set()).add(r["id_profesional"])
    except Exception:
        pass  # tabla ausente (tests) — el suprimidor es opcional

    flags = []
    espejo_dia: dict = {}   # (fecha, nom) -> set de profs que asignó el espejo
    # Los pagos con override SÍ cuentan para el balance del día (son verdad
    # nivel 0 — sin esto un grupo ya corregido seguía flaggeando, caso Milton
    # post-fix), pero NO generan flags propios (skip en el loop siguiente).
    for r in bi:
        espejo_dia.setdefault(
            (r["fecha"], _norm(r["nombre_paciente"])), set()
        ).add(r["id_profesional"])
    for r in bi:
        if r["pago_id"] in overrides:
            continue
        nom = _norm(r["nombre_paciente"])
        base = {"pago_id": r["pago_id"], "fecha": r["fecha"],
                "monto": r["monto"], "espejo": r["id_profesional"],
                "paciente": r["nombre_paciente"]}
        sd = mismo_dia.get((r["fecha"], nom))
        if sd:
            if len(sd) == 1 and r["id_profesional"] not in sd:
                flags.append({**base, "tipo": "recepcion_mismo_dia",
                              "sugerido": next(iter(sd))})
            continue
        hist = historial.get(nom) or []
        if (len(hist) >= 2 and len(set(hist)) == 1
                and r["id_profesional"] is not None
                and r["id_profesional"] != hist[0]):
            # Suprimir si la cita ATENDIDA del día confirma al espejo
            # (cita > historial — 16 falsas alarmas de mayo eran esto).
            atendidos = citas_atendidas.get((r["fecha"], r["id_paciente"]))
            if atendidos and r["id_profesional"] in atendidos:
                continue
            flags.append({**base, "tipo": "historial_unanime",
                          "sugerido": hist[0]})
    # SEÑAL 3 — día multi-profesional desbalanceado (clase Jorge 2026-08-20):
    # recepción vio ≥2 profesionales distintos ese día para el paciente, pero
    # el espejo colgó TODOS sus pagos del día a menos profesionales de los que
    # recepción registró. Firma de: (a) doble atención con matcher volcado a
    # uno solo (caso Claudio Lobos 37476+37477 ambos a 73 siendo 73+74), y
    # (b) pago PARTIDO entre dos tratamientos (caso Samira 37030, $30.520 =
    # $20.980 psico + $9.540 nutri — irrepresentable: el espejo asigna el pago
    # entero a un prof; convención: la parte MAYOR). Los copagos de recepción
    # NO calzan con los montos de caja → no se puede desempatar por monto,
    # solo flaggear para revisar contra el reporte por-profesional de la UI.
    with _bi_conn() as c2:
        tabla_arancel = _tabla_monto_area(c2)
    pagos_grupo: dict = {}
    for r in bi:
        if r["pago_id"] in overrides:
            continue
        pagos_grupo.setdefault((r["fecha"], _norm_nombre(r["nombre_paciente"])),
                               []).append(r)
    for (fecha, nom), profs_recep in mismo_dia.items():
        if len(profs_recep) < 2:
            continue
        profs_esp = espejo_dia.get((fecha, nom))
        if not profs_esp or len(profs_esp) >= len(profs_recep):
            continue
        if (fecha, nom) not in pagos_grupo:
            # Todos los pagos del grupo ya tienen override (caso decidido por
            # el dueño, ej. pago partido de Samira) — nada que revisar.
            continue
        # Intentar resolver cada pago del grupo por ARANCEL: monto → área →
        # ¿un único profesional del día de esa área? Tres desenlaces:
        # confirma al espejo (silencio), lo contradice (flag concreto), o el
        # monto es la SUMA de dos aranceles de las áreas del día (pago
        # PARTIDO — caso Uberlinda $26.520 = $15.130 med + $11.390 kine):
        # convención parte MAYOR; flag solo si el espejo no la sigue.
        resuelto_alguno = False
        for r in pagos_grupo.get((fecha, nom), []):
            monto_r = int(r["monto"] or 0)
            area = tabla_arancel.get(monto_r)
            if area:
                cand = [p for p in profs_recep if _AREA_POR_PROF.get(p) == area]
                if len(cand) == 1:
                    resuelto_alguno = True
                    if r["id_profesional"] != cand[0]:
                        flags.append({"pago_id": r["pago_id"], "fecha": fecha,
                                      "monto": r["monto"],
                                      "espejo": r["id_profesional"],
                                      "paciente": r["nombre_paciente"],
                                      "tipo": "dia_multi_prof_arancel",
                                      "sugerido": cand[0]})
                continue
            # ¿pago partido? monto = arancel_a + arancel_b con áreas del día
            aranceles_dia = {}
            for p in profs_recep:
                a = _AREA_POR_PROF.get(p)
                for m, ar in tabla_arancel.items():
                    if ar == a:
                        aranceles_dia[m] = p
            for m1, p1 in aranceles_dia.items():
                m2 = monto_r - m1
                if m2 in aranceles_dia and aranceles_dia[m2] != p1:
                    mayor = p1 if m1 >= m2 else aranceles_dia[m2]
                    resuelto_alguno = True
                    if r["id_profesional"] != mayor:
                        flags.append({"pago_id": r["pago_id"], "fecha": fecha,
                                      "monto": r["monto"],
                                      "espejo": r["id_profesional"],
                                      "paciente": r["nombre_paciente"],
                                      "tipo": "pago_partido_mayor_parte",
                                      "sugerido": mayor})
                    break
        if not resuelto_alguno:
            flags.append({"pago_id": None, "fecha": fecha, "monto": None,
                          "espejo": sorted(p for p in profs_esp if p),
                          "paciente": nom, "tipo": "dia_multi_prof_desbalance",
                          "sugerido": sorted(profs_recep)})
    # SEÑAL 4 — par kinesiólogos 21/77 (hallazgo 2026-08-20): Leo (21, jefe)
    # abre las atenciones de AMBOS kines, así que la cascada por atenciones
    # siempre apunta a Leo aunque haya atendido Luis (77). La CITA sí dice
    # quién atendió (verificado 5/5 contra recepción). Si el espejo asignó un
    # pago a un kine pero la cita "Atendido" de ese día es SOLO del otro →
    # flag. Cruce por id_paciente (no por nombre).
    _KINES = (21, 77)
    try:
        with _bi_conn() as c3:
            citas_kine = c3.execute(
                "SELECT fecha, id_paciente, id_profesional FROM ausentismo_citas "
                "WHERE id_profesional IN (21, 77) AND estado_cita='Atendido' "
                "AND fecha>=date(?, '-3 days') AND fecha<=?",
                (desde, hasta)).fetchall()
        cit_idx: dict = {}
        for r in citas_kine:
            cit_idx.setdefault((r["fecha"], r["id_paciente"]), set()).add(
                r["id_profesional"])
        for r in bi:
            if r["id_profesional"] not in _KINES or r["pago_id"] in overrides:
                continue
            quien = cit_idx.get((r["fecha"], r["id_paciente"]))
            if quien and len(quien) == 1 and r["id_profesional"] not in quien:
                flags.append({"pago_id": r["pago_id"], "fecha": r["fecha"],
                              "monto": r["monto"], "espejo": r["id_profesional"],
                              "paciente": r["nombre_paciente"],
                              "tipo": "cita_kine_contradice",
                              "sugerido": next(iter(quien))})
    except Exception:
        pass  # tabla ausentismo_citas ausente (tests) — señal opcional
    return {"n": len(flags), "flags": flags}


def _purge_anulados_dia(fecha: str, live_ids: set) -> int:
    """Reconcilia: borra de bi_pagos_caja los pagos de `fecha` que YA NO existen
    en Medilink (anulados/eliminados tras una sync previa). El upsert nunca borra,
    así que sin esto los anulados quedaban inflando el total local (caso 2026-06:
    3 folios = $97.960 de más). Solo se llama con `live_ids` NO vacío (respuesta
    válida de Medilink) → nunca borra masivamente sobre un fetch fallido o un día
    sin pagos."""
    if not live_ids:
        return 0
    with _bi_conn() as c:
        existing = {r[0] for r in c.execute(
            "SELECT pago_id FROM bi_pagos_caja WHERE fecha=?", (fecha,)).fetchall()}
        sobran = existing - live_ids
        if not sobran:
            return 0
        ph = ",".join("?" for _ in sobran)
        c.execute(f"DELETE FROM bi_pagos_caja WHERE fecha=? AND pago_id IN ({ph})",
                  (fecha, *sobran))
        log.info("pagos reconcilia %s: purgados %d anulados %s",
                 fecha, len(sobran), sorted(sobran))
        return len(sobran)


def _alertar_profesionales_sin_nomina() -> list[dict]:
    """Auto-detección: profesionales con pagos del mes en curso cuyo id_profesional
    NO está en equipo_cmc. Su ingreso queda fuera del DB Mensual hasta darlos de
    alta (caso Cecilia Unibazo, id 78). Loggea WARNING accionable por cada uno."""
    desde = date.today().replace(day=1).isoformat()
    faltan: list[dict] = []
    with _bi_conn() as c:
        nomina = {r[0] for r in c.execute(
            "SELECT id_medilink FROM equipo_cmc").fetchall()}
        rows = c.execute(
            "SELECT id_profesional, SUM(monto), COUNT(*) FROM bi_pagos_caja "
            "WHERE fecha>=? AND id_profesional IS NOT NULL GROUP BY id_profesional",
            (desde,)).fetchall()
        for idp, suma, n in rows:
            if idp not in nomina:
                faltan.append({"id_profesional": idp, "monto": int(suma or 0), "pagos": n})
                log.warning(
                    "NOMINA: id_profesional=%s tiene $%s en %d pagos este mes y NO "
                    "está en equipo_cmc → su ingreso queda FUERA del DB Mensual",
                    idp, f"{int(suma or 0):,}", n)
    return faltan


async def sync_pagos_rango(desde: str = "2024-01-01", hasta: str | None = None,
                            force: bool = False) -> dict:
    """Sincroniza pagos día por día. Skip incremental si la fecha ya está cacheada
    (al menos 1 pago para esa fecha)."""
    async with PAGOS_LOCK:
        try:
            d_desde = date.fromisoformat(desde)
            d_hasta = date.fromisoformat(hasta) if hasta else date.today()
        except ValueError:
            return {"ok": False, "error": "fechas inválidas"}

        inicio = datetime.utcnow().isoformat()

        fechas_existentes: set[str] = set()
        if not force:
            with _bi_conn() as c:
                rows = c.execute(
                    "SELECT DISTINCT fecha FROM bi_pagos_caja WHERE fecha IS NOT NULL"
                ).fetchall()
                fechas_existentes = {r[0] for r in rows if r[0]}

        total_pagos = 0
        total_sin_prof = 0
        total_dias = 0
        total_purgados = 0
        purgados_detalle: list[str] = []
        d = d_desde
        async with httpx.AsyncClient(timeout=30) as cli:
            while d <= d_hasta:
                fiso = d.isoformat()
                # NO excluir domingos: el CMC tiene actividad ocasional dom
                # (urgencias, ecografías) y Medilink los cuenta. El filtro
                # `weekday()!=6` perdía esos pagos (ej. dom 7-jun, $40.130).
                if force or fiso not in fechas_existentes:
                    log.info("pagos sync %s", fiso)
                    pagos_dia: list[dict] = []
                    async for batch in _fetch_pagos_dia(cli, fiso):
                        pagos_dia.extend(batch)
                    if pagos_dia:
                        n_ok, n_sin = _upsert_pagos(pagos_dia)
                        total_pagos += n_ok
                        total_sin_prof += n_sin
                        total_dias += 1
                    # Reconciliar anulados: _purge_anulados_dia existía desde el
                    # caso 2026-06 ($97.960 inflados) pero NUNCA se llamaba —
                    # los pagos anulados en Medilink después del sync quedaban
                    # fantasmas para siempre (caso julio 2026: 6 pagos,
                    # $201.760). Guard interno: solo purga con live_ids no
                    # vacío, jamás borra masivo sobre un fetch fallido.
                    live_ids = {int(p["id"]) for p in pagos_dia if p.get("id")}
                    n_purg = _purge_anulados_dia(fiso, live_ids)
                    if n_purg:
                        total_purgados += n_purg
                        purgados_detalle.append(f"{fiso}:{n_purg}")
                    await asyncio.sleep(1.0)  # entre días
                d += timedelta(days=1)

        fin = datetime.utcnow().isoformat()
        log_bi_sync("pagos", 0, f"{desde}..{hasta or date.today()}",
                    inicio, fin, total_pagos, total_sin_prof, True)
        log.info("pagos sync done: dias=%d pagos=%d sin_prof=%d purgados=%d %s",
                 total_dias, total_pagos, total_sin_prof, total_purgados,
                 ",".join(purgados_detalle))
        return {"ok": True, "dias": total_dias, "pagos": total_pagos,
                "sin_profesional": total_sin_prof,
                "purgados": total_purgados,
                "purgados_detalle": purgados_detalle}


def stats_profesional_caja(id_profesional: int, desde: str = "2024-01-01") -> dict:
    """Sumas mensuales de bi_pagos_caja para un profesional. Ese es el INGRESO REAL
    al CMC (módulo Cajas Medilink)."""
    from collections import defaultdict
    # id_profesional=0 (o None) → TODO EL CENTRO consolidado.
    todos = id_profesional in (0, None)
    filtro = "" if todos else "id_profesional=? AND "
    args: tuple = (desde,) if todos else (id_profesional, desde)
    with _bi_conn() as c:
        rows = c.execute(f"""
            SELECT fecha, SUM(monto) AS total, COUNT(*) AS n,
                   GROUP_CONCAT(DISTINCT metodo_pago) AS medios
            FROM bi_pagos_caja
            WHERE {filtro}fecha>=?
            GROUP BY fecha
            ORDER BY fecha
        """, args).fetchall()

    por_mes = defaultdict(lambda: {"caja": 0, "n_pagos": 0})
    total_caja = 0
    total_pagos = 0
    for r in rows:
        m = (r["fecha"] or "")[:7]
        if not m:
            continue
        por_mes[m]["caja"] += int(r["total"] or 0)
        por_mes[m]["n_pagos"] += int(r["n"] or 0)
        total_caja += int(r["total"] or 0)
        total_pagos += int(r["n"] or 0)

    return {
        "por_mes": dict(por_mes),
        "total_caja": total_caja,
        "total_pagos": total_pagos,
    }

