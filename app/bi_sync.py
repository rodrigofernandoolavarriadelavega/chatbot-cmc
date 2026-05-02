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
SYNC_LOCK = asyncio.Lock()


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
    """KPIs agregados de un profesional desde fecha. Devuelve la estructura
    esperada por el endpoint /api/profesional/{id}/data."""
    from collections import defaultdict
    with _bi_conn() as c:
        rows = c.execute("""
            SELECT atencion_id, fecha, id_paciente, total, abonado,
                   finalizado, bloqueado
            FROM bi_atenciones
            WHERE id_profesional=? AND fecha>=?
            ORDER BY fecha
        """, (id_profesional, desde)).fetchall()

    por_dia: dict = {}
    por_mes: dict = defaultdict(lambda: {"atend": 0, "monto_total": 0, "monto_cobrado": 0,
                                          "atend_pagadas": 0})
    por_dow: dict = defaultdict(list)

    for r in rows:
        f = (r["fecha"] or "")[:10]
        if not f:
            continue
        m = f[:7]
        total = int(r["total"] or 0)
        abonado = int(r["abonado"] or 0)
        # Solo cuenta como atención real si total > 0 (descarta los $0)
        if total <= 0:
            continue
        por_mes[m]["atend"] += 1
        por_mes[m]["monto_total"] += total
        # Cobrado real ≈ abonado (lo que efectivamente entró)
        por_mes[m]["monto_cobrado"] += abonado
        if abonado >= total:
            por_mes[m]["atend_pagadas"] += 1
        por_dia[f] = por_dia.get(f, 0) + 1

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
    return {
        "fecha_actualizacion": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "fuente": "bi_atenciones (Medilink × validación cobrado)",
        "id_profesional": id_profesional,
        "nombre_profesional": prof_info.get("nombre", f"Profesional {id_profesional}"),
        "especialidad": prof_info.get("especialidad", ""),
        "tarifa_real_promedio": round(tarifa_real),
        "cobertura_pct": cobertura_pct,
        "por_dia": por_dia,
        "por_mes": {m: {"atend": v["atend"], "monto_total": v["monto_total"],
                         "monto_cobrado": v["monto_cobrado"],
                         "atend_pagadas": v["atend_pagadas"]}
                     for m, v in por_mes.items()},
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
