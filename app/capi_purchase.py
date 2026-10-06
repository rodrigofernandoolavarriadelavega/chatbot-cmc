"""
app/capi_purchase.py — Evento CAPI `Purchase` por paciente atendido, con valor real.

Antes (fidelizacion._dispatch_capi_purchase, 22:00 CLT) el evento salía con un monto
FIJO por especialidad y para toda cita del bot que "ya pasó", aunque el paciente no
hubiera llegado. Ahora corre DESPUÉS del sync de caja y manda solo lo que ocurrió.

REGLA DE "ATENDIDO"
  `ausentismo_citas.id_estado = 2` ("Atendido", anulacion=0) para el id_cita del bot.
  No-show (8), anuladas (1/9/10/15), cambio de fecha (14), confirmadas/no confirmadas
  que nunca se marcaron atendidas: NO envían. Si la cita aún no está en
  ausentismo_citas (recolector 04:50) se difiere. Una cita no resuelta no se marca,
  así que se re-evalúa cada día mientras esté dentro de la ventana de Meta.

VALUE = lo que le queda al CENTRO de lo cobrado ese día a ese paciente
  pagos de `bi_pagos_caja` del paciente en la fecha de la cita, del mismo profesional
  (si ninguno calza y en TODO Medilink el paciente fue atendido ese día solo por ese
  profesional, todos sus pagos del día; si lo atendieron varios, va a estimado para
  no cargarle al anuncio plata de otra atención). Por pago: monto * (1 - pct_honorario/100) con `_pct_honorarios`
  (campanas_meta_routes; pct es lo que se lleva el PROFESIONAL, default 70, Abarca 62).
  Imagendent (`convenio_consumo`): ese monto se saca de la línea del profesional y
  al centro le queda venta - costo. `custom_data.venta_total` = lo cobrado.

SIN PAGO ESE DÍA
  - Caja aún no sincronizada para esa fecha (el sync completo corre 23:59 CLT):
    se DIFIERE, no se inventa monto.
  - Caja ya cerrada y sin pago (bono Fonasa 100%, abono/pack pagado antes, serie de
    kine): la atención sí ocurrió, así que se manda con value = margen del centro de
    la mediana de pagos del profesional (90 días previos) y
    `custom_data.value_estimado = true`. Sin historial del profesional: value 0.

IDEMPOTENCIA: tabla `capi_purchase_enviados` (id_cita PK) + event_id determinístico
`purchase_cita_<id_cita>` (Meta también deduplica). Solo se marca tras respuesta de
Meta con `events_received`; si falla se reintenta al día siguiente. Un paciente con
dos citas atendidas del mismo profesional el mismo día cuenta UNA visita.

VENTANA: citas de los últimos 6 días (Meta acepta event_time ≤ 7 días atrás);
`event_time` = hora real de la cita. Solo citas con fecha >= CAPI_PURCHASE_DESDE
(las anteriores ya salieron por el job viejo de las 22:00 con monto fijo).
"""
from __future__ import annotations

import logging
import os
import statistics
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from session import db

log = logging.getLogger("bot.capi_purchase")

_CLT = ZoneInfo("America/Santiago")
VENTANA_DIAS = 6
# Primer día cuyas atenciones maneja este job (ajustar a la fecha del deploy).
CAPI_PURCHASE_DESDE = os.getenv("CAPI_PURCHASE_DESDE", "2026-10-05")
_CIERRE_CAJA_HORA = 23   # el sync completo corre 23:59 CLT; se exige synced_at >= 23:00 del día


def ensure_table() -> None:
    with db() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS capi_purchase_enviados (
                   id_cita     TEXT PRIMARY KEY,
                   phone       TEXT,
                   id_paciente INTEGER,
                   fecha       TEXT,
                   estado      TEXT,      -- sent | omitida_dup
                   value       REAL,
                   venta_total INTEGER,
                   estimado    INTEGER DEFAULT 0,
                   sent_at     TEXT DEFAULT (datetime('now'))
               )"""
        )


def _caja_cerrada(c, fecha: str) -> bool:
    """¿El sync completo de caja ya corrió DESPUÉS de terminar `fecha`?

    Evidencia: algún pago con fecha >= `fecha` fue (re)sincronizado desde las 23:00 CLT
    de ese día (el diario 23:59 re-sincroniza 7 días con force → refresca synced_at).
    """
    corte = datetime.fromisoformat(fecha).replace(hour=_CIERRE_CAJA_HORA, tzinfo=_CLT)
    corte_utc = corte.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M:%S")
    r = c.execute("SELECT MAX(synced_at) m FROM bi_pagos_caja WHERE fecha >= ?", (fecha,)).fetchone()
    return bool(r and r["m"] and r["m"] >= corte_utc)


def _margen(venta: float, pct_prof: int) -> float:
    return venta * (100 - pct_prof) / 100


def _valor_cobrado(c, id_paciente: int, id_prof: int, fecha: str, pagos: list,
                   pct_map: dict) -> tuple[float, int]:
    """(margen del centro, venta total) para los pagos dados, con regla Imagendent."""
    from campanas_meta_routes import PCT_HONORARIO_DEFAULT
    pct = pct_map.get(id_prof) or PCT_HONORARIO_DEFAULT
    venta = sum(int(p["monto"] or 0) for p in pagos)
    centro = _margen(venta, pct)
    try:
        img = c.execute(
            "SELECT venta, cobrado, costo FROM convenio_consumo WHERE convenio='imagendent' "
            "AND id_paciente=? AND fecha=? AND id_profesional=?", (id_paciente, fecha, id_prof)
        ).fetchall()
    except Exception:
        img = []
    restante = venta
    for r in img:
        monto = int(r["cobrado"] or r["venta"] or 0)
        if monto and restante >= monto:   # si no se encuentra el cobro en caja, no se inventa
            restante -= monto
            centro += -_margen(monto, pct) + (monto - int(r["costo"] or 0))
    return max(centro, 0.0), venta


def _estimado(c, id_prof: int, fecha: str, pct_map: dict) -> float:
    """Margen del centro de la mediana de pagos del profesional (90 días previos)."""
    from campanas_meta_routes import PCT_HONORARIO_DEFAULT
    desde = (date.fromisoformat(fecha) - timedelta(days=90)).isoformat()
    montos = [int(r["monto"]) for r in c.execute(
        "SELECT monto FROM bi_pagos_caja WHERE id_profesional=? AND fecha>=? AND fecha<? AND monto>0",
        (id_prof, desde, fecha))]
    if not montos:
        return 0.0
    pct = pct_map.get(id_prof) or PCT_HONORARIO_DEFAULT
    return round(_margen(statistics.median(montos), pct))


def evaluar(hoy: date | None = None) -> list[dict]:
    """Decide qué Purchase corresponde enviar. Solo lee; no envía ni marca.

    Cada item: {id_cita, phone, nombre, especialidad, fecha, hora, id_paciente,
    value, venta_total, estimado, accion}; accion in
    'enviar' | 'omitir_dup' | 'diferir' | 'no_atendido'.
    """
    from campanas_meta_routes import _pct_honorarios
    ensure_table()
    hoy = hoy or datetime.now(_CLT).date()
    desde = max((hoy - timedelta(days=VENTANA_DIAS)).isoformat(), CAPI_PURCHASE_DESDE)
    hasta = (hoy - timedelta(days=1)).isoformat()
    out: list[dict] = []
    with db() as c:
        pct_map = _pct_honorarios(c)
        citas = c.execute(
            """SELECT cb.id_cita, cb.phone, cb.especialidad, cb.fecha, cb.hora,
                      cb.id_paciente_medilink AS pac_bot, p.nombre,
                      a.id_estado, a.anulacion, a.id_profesional, a.id_paciente AS pac_aus
               FROM citas_bot cb
               LEFT JOIN contact_profiles p ON p.phone = cb.phone
               LEFT JOIN ausentismo_citas a ON a.id_cita = CAST(cb.id_cita AS INTEGER)
               WHERE cb.fecha BETWEEN ? AND ?
                 AND cb.cancel_detected_at IS NULL
                 AND cb.phone NOT LIKE 'fb\\_%' ESCAPE '\\' AND cb.phone NOT LIKE 'ig\\_%' ESCAPE '\\'
                 AND NOT EXISTS (SELECT 1 FROM capi_purchase_enviados e WHERE e.id_cita = cb.id_cita)
               GROUP BY cb.id_cita
               ORDER BY cb.fecha, cb.hora, CAST(cb.id_cita AS INTEGER)""",
            (desde, hasta),
        ).fetchall()
        atendidas = [r for r in citas if r["id_estado"] == 2 and not r["anulacion"]
                     and (r["pac_aus"] or r["pac_bot"]) and r["id_profesional"]]
        por_pac_dia: dict[tuple, int] = {}
        for r in atendidas:
            k = (r["pac_aus"] or r["pac_bot"], r["fecha"])
            por_pac_dia[k] = por_pac_dia.get(k, 0) + 1
        vistas: set[tuple] = set()
        for r in citas:
            base = {"id_cita": str(r["id_cita"]), "phone": r["phone"], "nombre": r["nombre"],
                    "especialidad": r["especialidad"], "fecha": r["fecha"], "hora": r["hora"],
                    "id_paciente": r["pac_aus"] or r["pac_bot"], "value": None,
                    "venta_total": None, "estimado": False}
            if r["id_estado"] is None:
                out.append({**base, "accion": "diferir"})       # aún no recolectada
                continue
            if r not in atendidas:
                out.append({**base, "accion": "no_atendido"})
                continue
            pac, prof, fecha = base["id_paciente"], r["id_profesional"], r["fecha"]
            if (pac, fecha, prof) in vistas:
                out.append({**base, "accion": "omitir_dup"})
                continue
            vistas.add((pac, fecha, prof))
            pagos = c.execute(
                "SELECT monto, id_profesional FROM bi_pagos_caja WHERE id_paciente=? AND fecha=? "
                "AND monto>0", (pac, fecha)).fetchall()
            propios = [p for p in pagos if p["id_profesional"] == prof]
            if not propios and pagos and por_pac_dia[(pac, fecha)] == 1:
                # Medilink a veces deja el pago con otro profesional (o sin él). Se asumen
                # todos los pagos del día SOLO si en TODO Medilink (no solo el bot) el
                # paciente fue atendido por un único profesional; si no, va a estimado.
                profs_dia = {x["id_profesional"] for x in c.execute(
                    "SELECT id_profesional FROM ausentismo_citas WHERE id_paciente=? AND fecha=? "
                    "AND id_estado=2 AND COALESCE(anulacion,0)=0", (pac, fecha))}
                if len(profs_dia - {prof}) == 0:
                    propios = list(pagos)
            if propios:
                value, venta = _valor_cobrado(c, pac, prof, fecha, propios, pct_map)
                out.append({**base, "id_profesional": prof, "value": float(round(value)),
                            "venta_total": venta, "accion": "enviar"})
            elif _caja_cerrada(c, fecha):
                out.append({**base, "id_profesional": prof, "value": float(_estimado(c, prof, fecha, pct_map)),
                            "venta_total": 0, "estimado": True, "accion": "enviar"})
            else:
                out.append({**base, "accion": "diferir"})
    return out


def _marcar(item: dict, estado: str) -> None:
    with db() as c:
        c.execute(
            "INSERT OR IGNORE INTO capi_purchase_enviados"
            "(id_cita,phone,id_paciente,fecha,estado,value,venta_total,estimado) VALUES(?,?,?,?,?,?,?,?)",
            (item["id_cita"], item["phone"], item["id_paciente"], item["fecha"], estado,
             item["value"], item["venta_total"], 1 if item["estimado"] else 0))


def _event_time(item: dict) -> int:
    hora = (item.get("hora") or "00:00")[:5]
    dt = datetime.fromisoformat(f"{item['fecha']}T{hora}").replace(tzinfo=_CLT)
    return int(dt.timestamp())


async def enviar_purchases(hoy: date | None = None) -> dict:
    """Evalúa y envía. Devuelve contadores. No lanza por un evento individual."""
    import meta_capi
    from session import get_profile, get_meta_referral_fresh
    items = evaluar(hoy)
    res = {"enviados": 0, "errores": 0, "diferidos": 0, "no_atendidos": 0, "duplicados": 0,
           "value_total": 0.0, "estimados": 0}
    for it in items:
        a = it["accion"]
        if a == "diferir":
            res["diferidos"] += 1
        elif a == "no_atendido":
            res["no_atendidos"] += 1
        elif a == "omitir_dup":
            _marcar(it, "omitida_dup")
            res["duplicados"] += 1
        else:
            try:
                prof = get_profile(it["phone"]) or {}
                nom = (it["nombre"] or prof.get("nombre") or "").split()
                try:
                    ref = get_meta_referral_fresh(it["phone"], ttl_horas=2160)
                except Exception:
                    ref = None
                cd = {"content_name": it["especialidad"] or "",
                      "content_category": "medical_appointment",
                      "venta_total": it["venta_total"], "currency": "CLP"}
                if it["estimado"]:
                    cd["value_estimado"] = True
                r = await meta_capi.send_event(
                    "Purchase", phone=it["phone"], value=it["value"], currency="CLP",
                    rut=prof.get("rut") or None,
                    first_name=nom[0] if nom else None,
                    last_name=nom[-1] if len(nom) > 1 else None,
                    ctwa_clid=(ref or {}).get("ctwa_clid") or None,
                    event_id=f"purchase_cita_{it['id_cita']}",
                    event_time=_event_time(it), custom_data=cd)
            except Exception as e:
                log.warning("CAPI Purchase cita %s falló: %s", it["id_cita"], e)
                res["errores"] += 1
                continue
            if isinstance(r, dict) and r.get("events_received") and not r.get("error"):
                _marcar(it, "sent")
                res["enviados"] += 1
                res["value_total"] += it["value"]
                res["estimados"] += 1 if it["estimado"] else 0
            else:
                log.warning("CAPI Purchase cita %s sin confirmar (%s); reintenta mañana", it["id_cita"], r)
                res["errores"] += 1
    log.info("capi_purchase_diario: %s", res)
    return res


async def job_capi_purchase_diario():
    """07:07 CLT: después del sync de caja (23:59) y del recolector de ausentismo (04:50)."""
    try:
        await enviar_purchases()
    except Exception as e:
        log.exception("job_capi_purchase_diario falló: %s", e)
