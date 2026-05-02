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
    with _bi_conn() as c:
        # Pacientes únicos por día desde bi_atenciones
        atens = c.execute("""
            SELECT fecha, id_paciente, total
            FROM bi_atenciones
            WHERE id_profesional=? AND fecha>=?
        """, (id_profesional, desde)).fetchall()
        # Cobrado real desde bi_pagos_caja
        pagos = c.execute("""
            SELECT fecha, id_paciente, monto FROM bi_pagos_caja
            WHERE id_profesional=? AND fecha>=?
        """, (id_profesional, desde)).fetchall()

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
    # NOTA: post-procesado en main.py inyecta caja_real por mes desde bi_pagos_caja
    return {
        "fecha_actualizacion": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "fuente": "bi_atenciones (Medilink × validación cobrado)",
        "por_dia_detalle": por_dia_detalle,
        "id_profesional": id_profesional,
        "nombre_profesional": prof_info.get("nombre", f"Profesional {id_profesional}"),
        "especialidad": prof_info.get("especialidad", ""),
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

async def _fetch_pagos_dia(cli: httpx.AsyncClient, fecha: str) -> AsyncIterator[list[dict]]:
    """Pagina /pagos?q={fecha_recepcion:eq fecha}."""
    q = {"fecha_recepcion": {"eq": fecha}}
    pq = {"q": json.dumps(q, separators=(",", ":"))}
    next_url: str | None = PAGOS_URL
    first = True
    while next_url:
        for attempt in range(12):
            try:
                if first:
                    r = await cli.get(next_url, params=pq, headers=HEADERS)
                else:
                    r = await cli.get(next_url, headers=HEADERS)
            except Exception as e:
                log.warning("pagos %s attempt=%d excepción: %s", fecha, attempt, e)
                await asyncio.sleep(min(60, 3 + attempt * 5))
                continue
            if r.status_code == 200:
                d = r.json()
                # Algunos días vienen como lista directa (sin envoltorio data/links)
                if isinstance(d, list):
                    yield d
                    next_url = None
                else:
                    yield d.get("data", []) or []
                    links = d.get("links")
                    # /pagos devuelve links como list[{rel,href}]; /atenciones como dict
                    if isinstance(links, dict):
                        next_url = links.get("next")
                    elif isinstance(links, list):
                        next_url = next((l.get("href") for l in links
                                          if isinstance(l, dict) and l.get("rel") == "next"), None)
                    else:
                        next_url = None
                first = False
                break
            if r.status_code == 429:
                await asyncio.sleep(min(90, 5 + attempt * 8))
                continue
            log.warning("pagos %s HTTP %s — abort día", fecha, r.status_code)
            return
        else:
            log.warning("pagos %s sin éxito tras 12 intentos", fecha)
            return
        await asyncio.sleep(0.8)


def _resolver_profesional_pago(c, pago: dict) -> tuple[int | None, int | None]:
    """Cruza un pago contra bi_atenciones para inferir id_profesional.

    Los pagos pueden llegar días o semanas después de la atención (tratamientos
    dentales, kine, paquetes pagados al final). Estrategia en cascada:

    1. Mismo día + monto exacto match por atención.total
    2. Ventana ±60 días + monto exacto (atención más cercana en fecha)
    3. Ventana ±60 días + monto cercano (delta abs mínimo)
    4. Ventana ±60 días + atención con deuda > 0 (FIFO la más antigua con deuda)
    5. Si paciente tiene un único profesional histórico → ese
    Retorna (id_profesional, atencion_id) o (None, None).
    """
    from datetime import date, timedelta
    pid = pago.get("id_paciente")
    fecha = pago.get("fecha_recepcion") or pago.get("fecha")
    monto = int(pago.get("monto_pago") or 0)
    if not pid or not fecha:
        return None, None
    fecha_iso = fecha[:10]

    # Atenciones del paciente con total > 0 (descarta controles $0)
    rows = c.execute(
        "SELECT atencion_id, id_profesional, total, abonado, deuda, fecha "
        "FROM bi_atenciones WHERE id_paciente=? AND total>0 "
        "ORDER BY fecha", (pid,)
    ).fetchall()
    if not rows:
        return None, None

    try:
        f_pago = date.fromisoformat(fecha_iso)
    except ValueError:
        return None, None

    # 1. Mismo día + monto exacto
    same_day = [r for r in rows if r["fecha"] == fecha_iso and (r["total"] or 0) == monto]
    if same_day:
        # Si hay múltiples atenciones mismo día con mismo monto → prioridad por:
        # (a) atención con deuda > 0 (el pago paga esa deuda)
        # (b) atención sin abonar todavía (abonado < total)
        # (c) primera en orden cronológico
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

    # 2. Ventana ±60d + monto exacto, ordenar por proximidad temporal
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

    monto_exacto = [t for t in en_ventana if (t[1]["total"] or 0) == monto]
    if monto_exacto:
        r = monto_exacto[0][1]
        return r["id_profesional"], r["atencion_id"]

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

    # 4. Atención con deuda > 0 anterior al pago (FIFO)
    deudoras = [r for r in rows
                if (r["deuda"] or 0) > 0 and r["fecha"] and r["fecha"] <= fecha_iso]
    if deudoras:
        r = deudoras[-1]  # la más reciente con deuda
        return r["id_profesional"], r["atencion_id"]

    # 5. Si todas las atenciones del paciente son de un mismo profesional
    profs = set(r["id_profesional"] for r in rows if r["id_profesional"])
    if len(profs) == 1:
        prof_unico = next(iter(profs))
        # Asignar a la atención más cercana en fecha
        rows_ranked = sorted(rows, key=lambda r: abs(
            (date.fromisoformat(r["fecha"]) - f_pago).days
        ) if r["fecha"] else 999999)
        return prof_unico, rows_ranked[0]["atencion_id"]

    return None, None


def _upsert_pagos(records: list[dict]) -> tuple[int, int]:
    """Upsert pagos a bi_pagos_caja con id_profesional resuelto via cruce.
    Retorna (n_pagos_guardados, n_sin_profesional)."""
    if not records:
        return 0, 0
    n_ok = 0
    n_sin_prof = 0
    with _bi_conn() as c:
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
                   monto, metodo_pago, n_folio, synced_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(pago_id) DO UPDATE SET
                  atencion_id=excluded.atencion_id,
                  fecha=excluded.fecha,
                  id_profesional=excluded.id_profesional,
                  id_paciente=excluded.id_paciente,
                  monto=excluded.monto,
                  metodo_pago=excluded.metodo_pago,
                  n_folio=excluded.n_folio,
                  synced_at=datetime('now')
            """, (pago_id, atencion_id,
                   (p.get("fecha_recepcion") or "")[:10], id_prof,
                   p.get("id_paciente"), p.get("monto_pago"),
                   p.get("medio_pago"), p.get("numero_referencia")))
            n_ok += 1
    return n_ok, n_sin_prof


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
        d = d_desde
        async with httpx.AsyncClient(timeout=30) as cli:
            while d <= d_hasta:
                fiso = d.isoformat()
                if d.weekday() != 6 and (force or fiso not in fechas_existentes):
                    log.info("pagos sync %s", fiso)
                    pagos_dia: list[dict] = []
                    async for batch in _fetch_pagos_dia(cli, fiso):
                        pagos_dia.extend(batch)
                    if pagos_dia:
                        n_ok, n_sin = _upsert_pagos(pagos_dia)
                        total_pagos += n_ok
                        total_sin_prof += n_sin
                        total_dias += 1
                    await asyncio.sleep(1.0)  # entre días
                d += timedelta(days=1)

        fin = datetime.utcnow().isoformat()
        log_bi_sync("pagos", 0, f"{desde}..{hasta or date.today()}",
                    inicio, fin, total_pagos, total_sin_prof, True)
        log.info("pagos sync done: dias=%d pagos=%d sin_prof=%d",
                 total_dias, total_pagos, total_sin_prof)
        return {"ok": True, "dias": total_dias, "pagos": total_pagos,
                "sin_profesional": total_sin_prof}


def stats_profesional_caja(id_profesional: int, desde: str = "2024-01-01") -> dict:
    """Sumas mensuales de bi_pagos_caja para un profesional. Ese es el INGRESO REAL
    al CMC (módulo Cajas Medilink)."""
    from collections import defaultdict
    with _bi_conn() as c:
        rows = c.execute("""
            SELECT fecha, SUM(monto) AS total, COUNT(*) AS n,
                   GROUP_CONCAT(DISTINCT metodo_pago) AS medios
            FROM bi_pagos_caja
            WHERE id_profesional=? AND fecha>=?
            GROUP BY fecha
            ORDER BY fecha
        """, (id_profesional, desde)).fetchall()

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

