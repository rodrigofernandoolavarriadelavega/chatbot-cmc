# -*- coding: utf-8 -*-
"""Alma Radar v2 — portada «Buen día», puente de resultado, centro de datos y
laboratorio. Rutas en el mismo `radar_routes.router` (SOLO OLACORE_TOKEN).

PRINCIPIOS (los mismos del Radar)
---------------------------------
- Abrir la página no sale a la red: todo es SQLite/caché local. Las funciones
  que sí podrían salir (estados de anuncios, gasto de Meta del EBITDA) se
  llaman dentro de `solo_datos_locales()`.
- Nada se inventa: una fuente sin datos aparece como «sin datos» con su motivo,
  nunca como cero. Un monto estimado dice cómo se estimó y con qué confianza.
- Sin datos personales: solo conteos y agregados.

PORTADA · OPORTUNIDADES DEL DÍA
-------------------------------
Cada fuente propone a lo más una oportunidad con: evidencia (cifra + fuente +
fecha), acción (enlace a la herramienta real), monto estimado para el centro y
confianza (alta n≥30 · media n≥10 · baja). Se ordenan por monto. Las que miden
en parte la misma agenda o las mismas personas se marcan como solapadas: sus
montos no se suman.

PUENTE «¿QUÉ QUEDA EN EL CENTRO?»
---------------------------------
Reutiliza `ebitda_routes._ebitda_mes` (módulo EBITDA): venta de caja −
honorarios (pct por profesional, sueldo fijo de Abarca) − publicidad Meta −
comisión Transbank − gastos operativos registrados en `egresos_cmc`. Los gastos
se editan en el módulo EBITDA (/cmc/ebitda); aquí solo se leen.

CENTRO DE DATOS
---------------
Estado real de cada proceso nocturno según el rastro que deja en la base:
última corrida, frescura contra su horario (verde/ámbar/rojo), filas y error.

LABORATORIO
-----------
Simulador semanal por especialidad con tasas reales de 90 días. La curva de
conversaciones según gasto es A·(1−e^(−s/B)), ajustada a las semanas observadas
(o con un supuesto explícito si no hay variación suficiente). La banda es de
sensibilidad a la curvatura, no un intervalo estadístico. Los escenarios se
guardan en `radar_escenarios`; nada envía mensajes ni cambia campañas.
"""
from __future__ import annotations

import json
import logging
import math
import os
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from statistics import median

from fastapi import HTTPException, Request, Query

import campanas_meta_routes as cm
import radar_routes as rr
from session import db

log = logging.getLogger("radar_v2")

NOMBRE_DUENO = os.getenv("RADAR_NOMBRE_DUENO", "Rodrigo")
N_OPORTUNIDADES = 5
LAB_DIAS = 90
MAX_ESCENARIOS = 60


def _confianza(n: int | None) -> str:
    n = n or 0
    return "alta" if n >= 30 else "media" if n >= 10 else "baja"


def _fmt_clp(n: float | int | None) -> str:
    return "$" + f"{round(n or 0):,}".replace(",", ".")


# ═══════════════════════════════════════════════════════════════════════════
# Valores unitarios reales (caja y ausentismo)
# ═══════════════════════════════════════════════════════════════════════════

def _ticket_centro(c, hoy: date, dias: int = 120) -> dict:
    """Mediana de lo que deja al centro una atención (paciente × día ×
    profesional) en la caja de los últimos `dias`: monto × (1 − pct/100)."""
    pct = cm._pct_honorarios(c)
    desde = (hoy - timedelta(days=dias)).isoformat()
    vals = []
    try:
        for pid, monto in c.execute(
                "SELECT id_profesional, SUM(monto) FROM bi_pagos_caja WHERE fecha >= ? AND fecha < ? AND monto > 0 "
                "GROUP BY id_profesional, id_paciente, substr(fecha,1,10)", (desde, hoy.isoformat())):
            vals.append((monto or 0) * (100 - (pct.get(pid) or cm.PCT_HONORARIO_DEFAULT)) / 100)
    except Exception:  # noqa: BLE001 — entorno sin BI
        vals = []
    return {"mediana": round(median(vals)) if vals else None, "n": len(vals), "dias": dias}


_EST_ATENDIDO = ("atendid",)
_EST_NO_ASISTE = ("no asiste",)


def _asistencia(c, hoy: date, dias: int = 90, profs: set[int] | None = None) -> dict:
    """Atendido ÷ (atendido + no asiste) en el espejo de ausentismo."""
    if not cm._tabla_existe(c, "ausentismo_citas"):
        return {"tasa": None, "n": 0}
    desde = (hoy - timedelta(days=dias)).isoformat()
    at = na = 0
    for pid, est, n in c.execute(
            "SELECT id_profesional, lower(estado_cita), COUNT(*) FROM ausentismo_citas WHERE fecha >= ? AND fecha < ? "
            "GROUP BY 1, 2", (desde, hoy.isoformat())):
        if profs is not None and pid not in profs:
            continue
        e = est or ""
        if e.startswith(_EST_ATENDIDO):
            at += n
        elif e.startswith(_EST_NO_ASISTE):
            na += n
    tot = at + na
    return {"tasa": round(at / tot, 4) if tot else None, "n": tot, "no_asiste": na, "atendidos": at}


# ═══════════════════════════════════════════════════════════════════════════
# 1 · OPORTUNIDADES DEL DÍA
# ═══════════════════════════════════════════════════════════════════════════

SOLAPES = {
    "cupos": ("recall",),
    "recall": ("cupos", "esperando"),
    "esperando": ("recall",),
}


def _op_cupos(ag: dict) -> tuple[dict | None, dict | None]:
    if not isinstance(ag, dict) or ag.get("error"):
        return None, {"clave": "cupos", "fuente": "Cupos libres de la agenda",
                      "motivo": "No se pudo leer la caché de agenda."}
    if ag.get("sin_datos"):
        return None, {"clave": "cupos", "fuente": "Cupos libres de la agenda (14 días)",
                      "motivo": "La caché de agenda está vacía: el recolector de las 05:30 y 21:30 no ha guardado "
                                "cupos. No se estima el valor de las horas vacías hasta que exista una lectura."}
    grupos = [g for g in ag.get("grupos", []) if g.get("libres_14d")]
    valor = sum(g.get("valor_14d") or 0 for g in grupos)
    libres = sum(g["libres_14d"] for g in grupos)
    if not libres:
        return None, None
    top = sorted(grupos, key=lambda g: -(g.get("valor_14d") or 0))[:3]
    profs = [p for p in ag.get("profesionales", []) if p.get("libres_14d")]
    con_precio = [p for p in profs if p.get("precio")]
    act = rr._iso_epoch(ag.get("actualizado_ts"))
    conf = "alta" if len(con_precio) == len(profs) and not ag.get("desactualizado") else \
        "media" if len(con_precio) >= len(profs) / 2 else "baja"
    return {
        "clave": "cupos", "icono": "calendar",
        "titulo": f"Hay {libres} horas libres en los próximos 14 días",
        "evidencia": "Más valiosas: " + ", ".join(f"{g['grupo']} ({g['libres_14d']})" for g in top)
                     + ". Valor = cupos × precio típico de caja (mediana de 120 días) × parte del centro.",
        "fuente": "Caché de agenda (agenda_cupos_cache)", "fecha": act,
        "accion": {"label": "Ver agenda × anuncios", "modulo": "campanas_meta", "href": "/alma/campanas-meta#agenda"},
        "monto": round(valor), "monto_tipo": "techo",
        "monto_nota": "Techo si se llenaran todas; en la práctica se llena una parte.",
        "confianza": conf, "n": len(profs),
        "nota_confianza": f"{len(con_precio)} de {len(profs)} profesionales con precio de caja"
                          + (" · lectura desactualizada" if ag.get("desactualizado") else ""),
    }, None


def _op_esperando(vivo: dict, vel: dict, ticket: dict, asist: dict) -> tuple[dict | None, dict | None]:
    if not isinstance(vivo, dict) or vivo.get("error"):
        return None, {"clave": "esperando", "fuente": "Conversaciones esperando a recepción",
                      "motivo": "No se pudo leer el estado de las conversaciones."}
    n = vivo.get("esperando_total") or 0
    if not n:
        return None, None
    mx = vivo.get("esperando_max_min")
    monto = None
    nota = ""
    nconf = 0
    if isinstance(vel, dict) and not vel.get("error"):
        r, l_ = vel.get("rapidos") or {}, vel.get("lentos") or {}
        if r.get("pct") is not None and l_.get("pct") is not None and r["pct"] > l_["pct"] and ticket.get("mediana"):
            lift = (r["pct"] - l_["pct"]) / 100
            monto = round(n * lift * (asist.get("tasa") or 1) * ticket["mediana"])
            nconf = min(r.get("personas") or 0, l_.get("personas") or 0)
            nota = (f"Responder en menos de 30 min agenda al {r['pct']}% contra {l_['pct']}% si se tarda más de 2 h "
                    f"(30 días). Por persona: esa diferencia × asistencia × lo que deja una atención "
                    f"({_fmt_clp(ticket['mediana'])}).")
    return {
        "clave": "esperando", "icono": "msg",
        "titulo": f"{n} persona{'s' if n != 1 else ''} espera{'n' if n != 1 else ''} una respuesta de recepción",
        "evidencia": (f"La más antigua lleva {mx} min hábiles sin respuesta humana." if mx
                      else "Escribieron fuera del horario: la espera corre desde la apertura.") + (" " + nota if nota else ""),
        "fuente": "Sesiones en recepción y mensajes (hoy)", "fecha": rr._ahora().isoformat(timespec="minutes"),
        "accion": {"label": "Abrir la cola de recepción", "modulo": "recepcion_kanban", "href": "/alma/recepcion-kanban"},
        "monto": monto, "monto_tipo": "estimado",
        "monto_nota": "Citas que se ganarían respondiendo rápido, valoradas a la primera atención." if monto else
                      "Sin tasas suficientes para estimar el monto.",
        "confianza": _confianza(nconf) if monto else "baja", "n": nconf,
        "nota_confianza": f"Tasas medidas con {nconf} personas en el tramo más chico" if monto else "",
        "urgente": bool(mx and mx >= rr.ESPERA_ALERTA_MIN),
    }, None


def _op_anuncios(pm: dict) -> tuple[dict | None, dict | None]:
    if not isinstance(pm, dict) or pm.get("error"):
        return None, {"clave": "anuncios", "fuente": "Anuncios de Meta (30 días)", "motivo": "No se pudo leer el panel."}
    if not pm.get("hay_insights"):
        return None, {"clave": "anuncios", "fuente": "Anuncios de Meta (30 días)",
                      "motivo": "No hay foto diaria de Meta guardada."}
    import ebitda_routes as eb
    malos = []
    for a in pm.get("anuncios", []):
        if a.get("canal") != "meta" or (a.get("gasto") or 0) < cm.UMBRAL_GASTO_SIN_CITAS:
            continue
        # La cuenta publicitaria se comparte con otros negocios de la familia: no son del CMC.
        if any(k in f"{a.get('campana') or ''} {a.get('anuncio') or ''}".lower() for k in eb._META_NO_CMC):
            continue
        ret12 = (a.get("valor12m") or {}).get("retorno_12m")
        ret = ret12 if ret12 is not None else a.get("retorno_centro")
        estricto = a.get("retorno_estricto")
        if (estricto is not None and estricto < 0.5) and (ret is None or ret < 1):
            perdida = round(a["gasto"] * (1 - min(1.0, ret or 0)))
            if perdida > 0:
                malos.append({**a, "_perdida": perdida, "_ret": ret})
    if not malos:
        return None, None
    malos.sort(key=lambda a: -a["_perdida"])
    monto = sum(a["_perdida"] for a in malos)
    gasto = sum(a["gasto"] for a in malos)
    citas = sum(a.get("citas") or 0 for a in malos)
    peor = malos[0]
    rg = pm.get("rango") or {}
    return {
        "clave": "anuncios", "icono": "pause",
        "titulo": f"{len(malos)} anuncio{'s' if len(malos) != 1 else ''} gasta{'n' if len(malos) != 1 else ''} más de lo que devuelve{'n' if len(malos) != 1 else ''}",
        "evidencia": (f"{_fmt_clp(gasto)} de gasto en 30 días con {citas} cita{'s' if citas != 1 else ''}. "
                      f"El que más pierde: «{(peor.get('anuncio') or '')[:60]}», retorno estricto "
                      f"{_rx(peor.get('retorno_estricto'))}."),
        "fuente": "Panel de Campañas Meta (misma vara)", "fecha": f"{rg.get('desde')} a {rg.get('hasta')}",
        "accion": {"label": "Revisar anuncios", "modulo": "campanas_meta", "href": "/alma/campanas-meta"},
        "monto": monto, "monto_tipo": "estimado",
        "monto_nota": "Gasto de 30 días que no vuelve al centro (con el valor a 12 meses cuando existe). "
                      "El cambio se hace en el Administrador de anuncios.",
        "confianza": _confianza(citas), "n": citas,
        "nota_confianza": f"{citas} citas atribuidas en esos anuncios",
    }, None


REENGANCHE_DIAS = 90


def _tasa_reenganche(c, hoy: date) -> dict:
    """Del total de mensajes de reenganche (todas las variantes) de 90 días,
    cuántos terminaron en una cita del bot dentro de 24 h."""
    desde = cm._utc_txt(cm._epoch_ini(hoy - timedelta(days=REENGANCHE_DIAS)))
    env = [(r[0], cm._utc_txt_epoch(r[1]) or 0) for r in c.execute(
        "SELECT phone, ts FROM conversation_events WHERE event='reenganche_enviado' AND ts >= ?", (desde,))]
    if not env:
        return {"tasa": None, "n": 0}
    citas: dict[str, list[int]] = defaultdict(list)
    for ph, ts in c.execute("SELECT phone, created_at FROM citas_bot WHERE created_at >= ?", (desde,)):
        citas[ph].append(cm._utc_txt_epoch(ts) or 0)
    ag = sum(1 for ph, t in env if any(t <= x <= t + 86400 for x in citas.get(ph, ())))
    return {"tasa": ag / len(env), "n": len(env), "agendaron": ag}


def _op_recall(rec_d: dict, exp: dict | None, ticket: dict, asist: dict) -> tuple[dict | None, dict | None]:
    if not isinstance(rec_d, dict) or rec_d.get("error"):
        return None, {"clave": "recall", "fuente": "Recuperar pacientes (30 días)",
                      "motivo": "No se pudo armar la lista de personas recuperables."}
    n = rec_d.get("total") or 0
    if not n:
        return None, None
    res = rec_d.get("resumen") or {}
    tasa, nconf = (exp or {}).get("tasa"), (exp or {}).get("n") or 0
    monto = round(n * tasa * (asist.get("tasa") or 1) * ticket["mediana"]) if tasa and ticket.get("mediana") else None
    lbl = {"no_asistio": "no asistieron", "vio_horas": "vieron horas y no agendaron", "anulo": "anularon",
           "escribio": "escribieron y no siguieron", "sin_respuesta": "llevan más de 7 días sin respuesta"}
    partes = [f"{res[k]} {v}" for k, v in lbl.items() if res.get(k)]
    return {
        "clave": "recall", "icono": "repeat",
        "titulo": f"{n} persona{'s' if n != 1 else ''} de anuncios y web se puede{'n' if n != 1 else ''} recuperar con un contacto",
        "evidencia": "; ".join(partes) + "." + (" Un mensaje de reenganche termina en cita dentro de 24 h en el "
                                                + f"{100 * tasa:.1f}".replace(".", ",") + f"% de los casos ({nconf} envíos, 90 días)."
                                                if tasa else ""),
        "fuente": "Lista de Recuperar pacientes (30 días)", "fecha": rr._hoy().isoformat(),
        "accion": {"label": "Abrir Recuperar pacientes", "modulo": "recuperar", "href": "/alma/recuperar"},
        "monto": monto, "monto_tipo": "estimado",
        "monto_nota": "Personas × tasa del reenganche automático × asistencia × lo que deja una atención. "
                      "Es una referencia: el contacto manual puede rendir distinto." if monto else
                      "Sin tasa de reenganche medida para estimar el monto.",
        "confianza": "baja" if not monto else ("media" if _confianza(nconf) == "alta" else "baja"),
        "n": nconf, "nota_confianza": "Tasa tomada de otro canal (reenganche automático)" if monto else "",
    }, None


def _op_confirmar(c, hoy: date, ticket: dict, asist: dict) -> tuple[dict | None, dict | None]:
    if not cm._tabla_existe(c, "ausentismo_citas"):
        return None, {"clave": "confirmar", "fuente": "Citas de mañana sin confirmar",
                      "motivo": "No existe el espejo de ausentismo en esta base."}
    manana = (hoy + timedelta(days=1)).isoformat()
    filas = c.execute("SELECT lower(estado_cita), COUNT(*), MAX(updated_at) FROM ausentismo_citas WHERE fecha = ? "
                      "AND COALESCE(anulacion,0) = 0 GROUP BY 1", (manana,)).fetchall()
    if not filas:
        return None, {"clave": "confirmar", "fuente": "Citas de mañana sin confirmar",
                      "motivo": "El espejo de ausentismo no tiene citas para mañana."}
    total = sum(r[1] for r in filas if not (r[0] or "").startswith("anulad"))
    sin = sum(r[1] for r in filas if (r[0] or "") == "no confirmado")
    leido = max((r[2] or "") for r in filas)
    if not sin:
        return None, None
    tasa_ns = (1 - asist["tasa"]) if asist.get("tasa") is not None else None
    monto = round(sin * tasa_ns * ticket["mediana"]) if tasa_ns and ticket.get("mediana") else None
    return {
        "clave": "confirmar", "icono": "phone",
        "titulo": f"{sin} de {total} citas de mañana siguen sin confirmar",
        "evidencia": (f"Inasistencia real de 90 días: {round(100 * tasa_ns, 1):g}% ".replace(".", ",") +
                      f"({asist.get('no_asiste', 0)} de {asist.get('n', 0)} citas)." if tasa_ns is not None else
                      "Sin tasa de inasistencia medida.") + " Estado según la lectura de Medilink de las 04:50.",
        "fuente": "Espejo de ausentismo (ausentismo_citas)", "fecha": leido[:16] or None,
        "accion": {"label": "Abrir Ausentismo: a quién confirmar", "modulo": "ausentismo", "href": "/alma/ausentismo"},
        "monto": monto, "monto_tipo": "en_riesgo",
        "monto_nota": "Valor en riesgo: citas sin confirmar × tasa de inasistencia × lo que deja una atención.",
        "confianza": "media" if asist.get("n", 0) >= 30 else "baja", "n": asist.get("n", 0),
        "nota_confianza": "Tasa general del centro, no la de cada paciente",
    }, None


def _op_no_cerradas(c) -> tuple[dict | None, dict | None]:
    for t in ("atenciones_no_cerradas", "bi_atenciones_no_cerradas"):
        if cm._tabla_existe(c, t):
            try:
                n, monto = c.execute(f"SELECT COUNT(*), COALESCE(SUM(monto_estimado),0) FROM {t} "
                                     "WHERE fecha >= date('now','-30 day')").fetchone()
            except Exception:  # noqa: BLE001
                return None, {"clave": "no_cerradas", "fuente": "Atenciones no cerradas",
                              "motivo": "La tabla existe pero no tiene el formato esperado."}
            if not n:
                return None, None
            return {"clave": "no_cerradas", "icono": "alert", "titulo": f"{n} atenciones quedaron sin cerrar en Medilink",
                    "evidencia": "Atendidas sin valor registrado en los últimos 30 días.", "fuente": t,
                    "fecha": rr._hoy().isoformat(),
                    "accion": {"label": "Abrir Ausentismo", "modulo": "ausentismo", "href": "/alma/ausentismo"},
                    "monto": round(monto or 0), "monto_tipo": "estimado", "monto_nota": "Ticket estimado por atención.",
                    "confianza": _confianza(n), "n": n, "nota_confianza": ""}, None
    return None, {"clave": "no_cerradas", "fuente": "Atenciones no cerradas en Medilink",
                  "motivo": "No hay una tabla local con este dato: hoy se mide contra Medilink con un script aparte."}


def _lectura(ops: list[dict], puente: dict, hoy: date) -> str:
    partes = []
    ant = (puente or {}).get("anterior") or {}
    if ant.get("ingresos"):
        mes = rr._ahora().replace(day=1) - timedelta(days=1)
        nombre = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre",
                  "octubre", "noviembre", "diciembre"][mes.month - 1]
        partes.append(f"{nombre.capitalize()} cerró con {_fmt_clp(ant['ingresos'])} de caja y "
                      f"{_fmt_clp(ant['margen_cmc'])} para el centro después de honorarios")
    con_monto = [o for o in ops if o.get("monto")]
    if ops:
        top = con_monto[0] if con_monto else ops[0]
        partes.append(f"hoy hay {len(ops)} oportunidad{'es' if len(ops) != 1 else ''} a la vista; "
                      f"la primera: {top['titulo'][0].lower() + top['titulo'][1:]}")
    else:
        partes.append("hoy no aparece ninguna oportunidad con evidencia suficiente")
    return "; ".join(partes) + "."


def oportunidades_data(ahora: datetime | None = None) -> dict:
    import campanas_meta_integraciones as ci
    ahora = ahora or rr._ahora()
    hoy = ahora.date()
    d30, h30 = rr._rango(hoy)
    ag = rr._seguro("agenda", rr._agenda)
    pm = rr._seguro("panel_meta", rr._panel, "meta", d30, h30)
    vivo = rr._seguro("vivo", lambda: rr._cacheado(f"vivo:{hoy}", rr.TTL_PULSO, lambda: rr._vivo(ahora)))
    vel = rr._seguro("velocidad", rr._velocidad, d30, h30)

    def _rec():
        import recuperacion as rec
        with cm.solo_datos_locales():
            d = rec.lista(30, ahora)
        return {"total": d["total"], "resumen": d["resumen"]}
    rec_d = rr._seguro("recuperar", lambda: rr._cacheado(f"recup:{hoy}", rr.TTL_PESADO, _rec))

    with db() as c:
        ticket = _ticket_centro(c, hoy)
        asist = _asistencia(c, hoy)
        tasa_re = _tasa_reenganche(c, hoy)
        conf_op, conf_sin = _op_confirmar(c, hoy, ticket, asist)
        nc_op, nc_sin = _op_no_cerradas(c)
    pares = [_op_cupos(ag), _op_esperando(vivo, vel, ticket, asist), _op_anuncios(pm),
             _op_recall(rec_d, tasa_re, ticket, asist), (conf_op, conf_sin), (nc_op, nc_sin)]
    ops = [o for o, _ in pares if o]
    sin = [s for _, s in pares if s]
    ops.sort(key=lambda o: (o.get("monto") is None, -(o.get("monto") or 0)))
    ops = ops[:N_OPORTUNIDADES]
    claves = {o["clave"] for o in ops}
    for o in ops:
        o["solapa_con"] = [k for k in SOLAPES.get(o["clave"], ()) if k in claves]
    solapes = sorted({tuple(sorted((o["clave"], k))) for o in ops for k in o["solapa_con"]})
    return {"oportunidades": ops, "sin_datos": sin, "solapes": [list(s) for s in solapes],
            "unitarios": {"ticket_centro": ticket, "asistencia": asist}}


# ═══════════════════════════════════════════════════════════════════════════
# 2 · PUENTE «¿QUÉ QUEDA EN EL CENTRO?» (reutiliza EBITDA)
# ═══════════════════════════════════════════════════════════════════════════

# El local es propio (dueño, 6-oct-2026): no se exige arriendo.
CATEGORIAS_CLAVE = (("sueldo", "Sueldos y leyes sociales"),)


def _mes_txt(d: date) -> str:
    return d.strftime("%Y-%m")


def _mes_anterior(hoy: date) -> str:
    return _mes_txt(hoy.replace(day=1) - timedelta(days=1))


def _puente_mes(c, mes: str, radar_mes: dict | None) -> dict:
    import ebitda_routes as eb
    with eb.solo_datos_locales():
        e = eb._ebitda_mes(c, mes)
    publicidad = transbank = 0
    meta_fuente = None
    operativos: dict[str, int] = defaultdict(int)
    for g in e["gastos_detalle"]:
        cat = (g.get("categoria") or "Otros").strip()
        low = cat.lower()
        monto = int(g.get("monto") or 0)
        if "public" in low or "meta" in low:
            publicidad += monto
            meta_fuente = g.get("fuente") or ("registrado" if not g.get("auto") else meta_fuente)
        elif "transbank" in low:
            transbank += monto
        else:
            operativos[cat] += monto
    op_total = sum(operativos.values())
    aporte = e["margen_cmc"] - publicidad - transbank
    cats_low = " ".join(k.lower() for k in operativos)
    faltan = [lbl for clave, lbl in CATEGORIAS_CLAVE if clave not in cats_low]
    out = {
        "mes": mes, "ingresos": e["ingresos"], "honorarios": e["honorarios_bruto"], "margen_cmc": e["margen_cmc"],
        "publicidad": publicidad, "publicidad_fuente": meta_fuente, "transbank": transbank,
        "aporte": aporte, "operativos": sorted(({"categoria": k, "monto": v} for k, v in operativos.items()),
                                               key=lambda x: -x["monto"]),
        "operativos_total": op_total, "hay_costos": op_total > 0,
        "resultado": aporte - op_total if op_total > 0 else None,
        "faltan_categorias": faltan if op_total > 0 else [],
        "abarca_fijo": next((p["bruto"] for p in e["profesionales"] if p.get("fijo")), None),
    }
    if radar_mes and radar_mes.get("venta") is not None:
        out["radar"] = {"venta": radar_mes["venta"], "centro": radar_mes["centro"],
                        "dif_venta": e["ingresos"] - radar_mes["venta"],
                        "dif_centro": e["margen_cmc"] - radar_mes["centro"]}
    return out


def puente_data(hoy: date | None = None) -> dict:
    hoy = hoy or rr._hoy()
    meses = rr._seguro("meses", lambda: rr._cacheado(f"meses:{hoy}", rr.TTL_PESADO, lambda: rr._meses(hoy)))
    serie = {s["mes"]: s for s in (meses.get("serie") or [])} if isinstance(meses, dict) else {}
    ant, act = _mes_anterior(hoy), _mes_txt(hoy)
    try:   # tabla de gastos del módulo EBITDA (CREATE IF NOT EXISTS; abre su propia conexión)
        from finanzas_routes import ensure_table as _ensure_egresos
        _ensure_egresos()
    except Exception:  # noqa: BLE001
        pass
    with db() as c:
        if not cm._tabla_existe(c, "bi_pagos_caja"):
            return {"error": "No hay caja sincronizada en esta base."}
        faltan = [t for t in ("egresos_cmc", "equipo_cmc", "pagos_cmc", "bi_atenciones") if not cm._tabla_existe(c, t)]
        if faltan:
            return {"no_disponible": True,
                    "motivo": "El módulo EBITDA no está disponible en esta base (faltan las tablas " + ", ".join(faltan) + ")."}
        out = {"anterior": _puente_mes(c, ant, serie.get(ant)), "actual": _puente_mes(c, act, serie.get(act))}
    out["actual"]["dias_mes"] = (date(hoy.year + hoy.month // 12, hoy.month % 12 + 1, 1) - hoy.replace(day=1)).days
    out["actual"]["dia"] = hoy.day
    out["editar"] = {"label": "Editar gastos en EBITDA", "modulo": "ebitda", "href": "/cmc/ebitda"}
    return out


# ═══════════════════════════════════════════════════════════════════════════
# 3 · CENTRO DE DATOS
# ═══════════════════════════════════════════════════════════════════════════

GRACIA_MIN = 45
TOLERANCIA_MIN = 20


def _ultimas_programadas(ahora: datetime, horas: list[tuple[int, int]], mensual_dia: int | None = None) -> tuple[datetime, datetime]:
    """(última programada ya vencida con gracia, la anterior) en hora de Chile."""
    limite = ahora - timedelta(minutes=GRACIA_MIN)
    cands = []
    if mensual_dia:
        y, m = limite.year, limite.month
        for _ in range(3):
            hh, mm = horas[0]
            cands.append(limite.replace(year=y, month=m, day=mensual_dia, hour=hh, minute=mm, second=0, microsecond=0))
            y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    else:
        for k in range(0, 4):
            d = (limite - timedelta(days=k)).date()
            for hh, mm in horas:
                cands.append(datetime(d.year, d.month, d.day, hh, mm, tzinfo=ahora.tzinfo))
    pas = sorted([x for x in cands if x <= limite], reverse=True)
    return pas[0], pas[1]


def _frescura(ultima: datetime | None, ahora: datetime, horas, mensual_dia=None) -> str:
    if not ultima:
        return "rojo"
    ult_prog, prev_prog = _ultimas_programadas(ahora, horas, mensual_dia)
    tol = timedelta(minutes=TOLERANCIA_MIN)
    if ultima >= ult_prog - tol:
        return "verde"
    if ultima >= prev_prog - tol:
        return "ambar"
    return "rojo"


def _dt_utc_txt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00").replace(" ", "T"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(cm._CL)


def _dt_cl_txt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace(" ", "T"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=cm._CL)
    return dt.astimezone(cm._CL)


def _dt_ep(ep) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(ep), cm._CL) if ep else None
    except (TypeError, ValueError, OSError):
        return None


def _proc(clave, nombre, horario, fuente, ultima, filas, estado, detalle="", error=None, extra=None) -> dict:
    return {"clave": clave, "nombre": nombre, "horario": horario, "fuente": fuente,
            "ultima": ultima.isoformat(timespec="minutes") if ultima else None,
            "filas": filas, "estado": estado, "detalle": detalle, "error": error, **(extra or {})}


def centro_datos_data(ahora: datetime | None = None) -> dict:
    import campanas_meta_integraciones as ci
    ahora = ahora or rr._ahora()
    P = []
    with db() as c:
        T = lambda n: cm._tabla_existe(c, n)  # noqa: E731

        # 1. Caja
        if T("bi_pagos_caja"):
            r = c.execute("SELECT MAX(synced_at), COUNT(*), MAX(fecha) FROM bi_pagos_caja").fetchone()
            ult = _dt_utc_txt(r[0])
            err = None
            if T("bi_sync_log"):
                corte = (ahora - timedelta(hours=24)).astimezone(timezone.utc).replace(tzinfo=None).isoformat()
                e = c.execute("SELECT COUNT(*), MAX(fin) FROM bi_sync_log WHERE ok = 0 AND fin >= ?", (corte,)).fetchone()
                if e and e[0]:
                    err = f"{e[0]} tramo(s) con error en las últimas 24 h."
            est = _frescura(ult, ahora, [(14, 0), (19, 0), (23, 59)])
            if err and est == "verde":
                est = "ambar"
            P.append(_proc("caja", "Sincronización de caja (Medilink)", "23:59 · también 14:00 y 19:00", "bi_pagos_caja",
                           ult, r[1], est, f"Último día con pagos: {(r[2] or '')[:10] or '—'}.", err))
        else:
            P.append(_proc("caja", "Sincronización de caja (Medilink)", "23:59 · también 14:00 y 19:00", "bi_pagos_caja",
                           None, 0, "rojo", "La tabla no existe en esta base."))

        # 2. Ausentismo
        if T("ausentismo_citas"):
            r = c.execute("SELECT MAX(updated_at), COUNT(*), MAX(fecha) FROM ausentismo_citas").fetchone()
            ult = _dt_cl_txt(r[0])
            P.append(_proc("ausentismo", "Recolector de ausentismo", "04:50", "ausentismo_citas", ult, r[1],
                           _frescura(ult, ahora, [(4, 50)]),
                           f"Estado de citas hasta el {(r[2] or '')[:10] or '—'}." if r[1] else "La tabla está vacía."))
        else:
            P.append(_proc("ausentismo", "Recolector de ausentismo", "04:50", "ausentismo_citas", None, 0, "rojo",
                           "La tabla no existe en esta base."))

        # 3. Agenda (cupos)
        ci._ensure_cupos(c)
        r = c.execute("SELECT MAX(actualizado_ts), COUNT(*), COUNT(DISTINCT id_profesional) FROM agenda_cupos_cache").fetchone()
        corr = ci._get_estado(c, "ultima_corrida") or {}
        ult = _dt_ep(r[0]) or _dt_ep(corr.get("ts"))
        err = None
        if corr.get("corte"):
            err = f"La última corrida se cortó ({corr['corte']})."
        elif corr.get("errores"):
            err = f"{corr['errores']} consulta(s) con error en la última corrida."
        if not r[1]:
            det = ("La caché está vacía: el recolector no ha guardado cupos"
                   + (" (apagado por configuración: AGENDA_CUPOS_ACTIVE)." if not ci._activo_cupos() else
                      "; si la última corrida existe, no encontró horarios." if corr else "; no hay registro de ninguna corrida."))
            est = "rojo"
        else:
            det = f"{r[2]} profesionales con cupos leídos."
            est = _frescura(ult, ahora, [(5, 30), (21, 30)])
            if err and est == "verde":
                est = "ambar"
        P.append(_proc("agenda", "Cupos libres de la agenda", "05:30 y 21:30", "agenda_cupos_cache", ult, r[1], est,
                       det, err))

        # 4. Insights Meta
        cm._ensure_insights(c)
        r = c.execute("SELECT MAX(actualizado_ts), COUNT(*), MAX(fecha) FROM meta_insights_diario").fetchone()
        ult = _dt_ep(r[0])
        est = _frescura(ult, ahora, [(6, 20)]) if r[1] else "rojo"
        det = f"Último día con datos: {r[2] or '—'}." if r[1] else "Sin fotos guardadas."
        if r[2] and (ahora.date() - date.fromisoformat(r[2])).days > 2 and est == "verde":
            est, det = "ambar", det + " La foto corre, pero el último día con datos tiene más de 2 días."
        P.append(_proc("insights", "Foto diaria de anuncios Meta", "06:20", "meta_insights_diario", ult, r[1], est, det))

        # 5. Google Search Console
        if T("gsc_diario"):
            r = c.execute("SELECT MAX(actualizado_ts), COUNT(*), MAX(fecha) FROM gsc_diario").fetchone()
            ult = _dt_ep(r[0])
            P.append(_proc("gsc", "Google Search Console", "06:40", "gsc_diario", ult, r[1],
                           _frescura(ult, ahora, [(6, 40)]) if r[1] else "rojo",
                           f"Último día con datos: {r[2] or '—'} (Google publica con 2 a 3 días de atraso)." if r[1]
                           else "Sin días guardados."))
        else:
            P.append(_proc("gsc", "Google Search Console", "06:40", "gsc_diario", None, 0, "rojo",
                           "La tabla no existe: el respaldo de Google nunca corrió en esta base."))

        # 6. Aviso a Meta (CAPI Purchase)
        if T("capi_purchase_corridas"):
            r = c.execute("SELECT fecha_corrida, enviados, errores, (SELECT COUNT(*) FROM capi_purchase_corridas) "
                          "FROM capi_purchase_corridas ORDER BY id DESC LIMIT 1").fetchone()
            ult = _dt_cl_txt(r[0]) if r else None
            est = _frescura(ult, ahora, [(7, 7)]) if r else "rojo"
            err = f"{r[2]} envío(s) con error en la última corrida." if r and r[2] else None
            if err and est == "verde":
                est = "ambar"
            P.append(_proc("capi", "Aviso de atenciones a Meta", "07:07", "capi_purchase_corridas", ult,
                           r[3] if r else 0, est, f"Última corrida: {r[1] or 0} avisos aceptados." if r else
                           "La tabla existe pero no tiene corridas.", err))
        else:
            P.append(_proc("capi", "Aviso de atenciones a Meta", "07:07", "capi_purchase_corridas", None, 0, "rojo",
                           "Sin rastro: la tabla de corridas no existe, así que el aviso nunca terminó una corrida aquí."))

        # 7. Creativos
        ci._ensure_creativos(c)
        r = c.execute("SELECT MAX(actualizado_ts), COUNT(*), SUM(CASE WHEN archivo IS NOT NULL AND archivo != '' THEN 1 ELSE 0 END) "
                      "FROM meta_creativos").fetchone()
        ult = _dt_ep(r[0])
        P.append(_proc("creativos", "Textos e imágenes de anuncios", "06:50", "meta_creativos", ult, r[1],
                       _frescura(ult, ahora, [(6, 50)]) if r[1] else "rojo",
                       f"{r[2] or 0} de {r[1]} con imagen guardada." if r[1] else "Sin anuncios guardados."))

        # 8. Cohortes de valor a 12 meses
        if T("valor_cohorte_estado"):
            row = c.execute("SELECT valor FROM valor_cohorte_estado WHERE clave='meta'").fetchone()
            n = c.execute("SELECT COUNT(*) FROM valor_cohorte_especialidad").fetchone()[0] \
                if T("valor_cohorte_especialidad") else 0
            try:
                meta = json.loads(row[0]) if row else {}
            except (ValueError, TypeError):
                meta = {}
            ult = _dt_ep(meta.get("calculado_ts"))
            P.append(_proc("cohortes", "Cohortes de valor a 12 meses", "Día 1 de cada mes, 04:30", "valor_cohorte_especialidad",
                           ult, n, _frescura(ult, ahora, [(4, 30)], mensual_dia=1) if n else "rojo",
                           f"{meta.get('n_total', 0)} pacientes nuevos en la ventana." if n else "Sin cohortes calculadas."))
        else:
            P.append(_proc("cohortes", "Cohortes de valor a 12 meses", "Día 1 de cada mes, 04:30", "valor_cohorte_especialidad",
                           None, 0, "rojo", "Nunca se calcularon en esta base."))

        # 9. Temas de opinión
        import opinion_temas as ot
        if T("opinion_temas"):
            r = c.execute("SELECT MAX(created_at), COUNT(*) FROM opinion_temas").fetchone()
            ult = _dt_utc_txt(r[0])
            P.append(_proc("opinion", "Temas de opinión", "03:40", "opinion_temas", ult, r[1],
                           _frescura(ult, ahora, [(3, 40)]) if r[1] else "rojo",
                           "Clasificación de comentarios de pacientes." if r[1] else "La tabla está vacía."))
        else:
            P.append(_proc("opinion", "Temas de opinión", "03:40", "opinion_temas", None, 0, "rojo",
                           "La tabla no existe: la clasificación nunca corrió aquí"
                           + ("." if ot.activo() else " (apagada por configuración).")))

    # 10. Nota de Google (archivo, no tabla; se refresca a demanda cada 6 h)
    import google_rating as gr
    d = gr.cached_rating()
    if d and d.get("rating"):
        ult = _dt_ep(d.get("updated_at"))
        h = (ahora - ult).total_seconds() / 3600 if ult else None
        est = "verde" if h is not None and h <= 24 else "ambar" if h is not None and h <= 72 else "rojo"
        P.append(_proc("google_rating", "Nota de Google", "A demanda · cada 6 h como máximo", "google_rating.json", ult, 1,
                       est, f"Nota {str(d.get('rating')).replace('.', ',')} con {d.get('review_count') or 0} reseñas."))
    else:
        src = (gr._CACHE.get("data") or {}).get("source") if isinstance(gr._CACHE.get("data"), dict) else None
        P.append(_proc("google_rating", "Nota de Google", "A demanda · cada 6 h como máximo", "google_rating.json", None, 0,
                       "rojo", "No hay una nota guardada en el servidor: la consulta a Google Places no ha tenido éxito.",
                       f"Último intento: {src}." if src else None))

    for p in P:   # filas sin hora de actualización: no se confunden con "nunca corrió"
        if p["filas"] and not p["ultima"] and p["clave"] not in ("google_rating",):
            p["detalle"] = (p["detalle"] + " Las filas no registran la hora en que se guardaron.").strip()
    orden = {"rojo": 0, "ambar": 1, "verde": 2}
    al_dia = sum(1 for p in P if p["estado"] == "verde")
    return {"ahora": ahora.isoformat(timespec="minutes"), "procesos": sorted(P, key=lambda p: orden[p["estado"]]),
            "al_dia": al_dia, "total": len(P),
            "rojos": sum(1 for p in P if p["estado"] == "rojo"), "ambar": sum(1 for p in P if p["estado"] == "ambar"),
            "reglas": {"gracia_min": GRACIA_MIN, "tolerancia_min": TOLERANCIA_MIN}}


# ═══════════════════════════════════════════════════════════════════════════
# 4 · LABORATORIO
# ═══════════════════════════════════════════════════════════════════════════

FIT_MIN_SEMANAS = 6
FIT_MIN_CV = 0.25
SUPUESTO_B = 2.0          # sin ajuste: B = 2 × gasto semanal observado
SENS = (0.6, 1.6)         # banda de sensibilidad: B × 0,6 (satura antes) y × 1,6


def _ajuste(xs: list[float], ys: list[float]) -> tuple[float, float, float] | None:
    """Mínimos cuadrados de y = A(1 − e^(−x/B)) buscando B en una grilla
    logarítmica; A tiene forma cerrada para cada B. → (A, B, r2) o None."""
    pts = [(x, y) for x, y in zip(xs, ys) if x > 0]
    if len(pts) < FIT_MIN_SEMANAS:
        return None
    mx = sum(x for x, _ in pts) / len(pts)
    var = sum((x - mx) ** 2 for x, _ in pts) / len(pts)
    if mx <= 0 or math.sqrt(var) / mx < FIT_MIN_CV:
        return None
    my = sum(y for _, y in pts) / len(pts)
    sst = sum((y - my) ** 2 for _, y in pts) or 1e-9
    mejor = None
    for i in range(61):
        B = mx * 0.3 * (40 ** (i / 60))          # 0,3× a 12× el gasto medio
        f = [1 - math.exp(-x / B) for x, _ in pts]
        den = sum(v * v for v in f)
        if den <= 0:
            continue
        A = sum(v * y for v, (_, y) in zip(f, pts)) / den
        sse = sum((y - A * v) ** 2 for v, (_, y) in zip(f, pts))
        if mejor is None or sse < mejor[2]:
            mejor = (A, B, sse)
    if not mejor or mejor[0] <= 0:
        return None
    return mejor[0], mejor[1], round(1 - mejor[2] / sst, 3)


def _semanas_por_grupo(c, hoy: date, semanas: int = 13) -> tuple[dict, dict]:
    import campanas_meta_integraciones as ci
    cm._ensure_insights(c)
    mapa = cm._mapa_anuncios(c)
    fin = hoy - timedelta(days=1)
    ini = fin - timedelta(days=7 * semanas - 1)
    memo: dict = {}
    serie: dict[str, list[list[float]]] = defaultdict(lambda: [[0.0, 0.0] for _ in range(semanas)])
    for ad, fecha, gasto, conv in c.execute(
            "SELECT ad_id, fecha, SUM(spend), SUM(conversaciones) FROM meta_insights_diario WHERE desglose='total' "
            "AND fecha >= ? AND fecha <= ? GROUP BY ad_id, fecha", (ini.isoformat(), fin.isoformat())):
        g = ci._grupo_anuncio(c, mapa, ad, memo)
        if not g:
            continue
        i = (date.fromisoformat(fecha) - ini).days // 7
        if 0 <= i < semanas:
            serie[g][i][0] += gasto or 0
            serie[g][i][1] += conv or 0
    return dict(serie), {"desde": ini.isoformat(), "hasta": fin.isoformat(), "semanas": semanas}


def _valor12m_por_grupo(c) -> dict[str, dict]:
    import valor_cohortes as vc
    try:
        p = vc.publico(c)
    except Exception:  # noqa: BLE001
        return {}
    acc: dict[str, dict] = {}
    for e in p.get("especialidades", []):
        esp = e.get("especialidad") or ""
        if esp in (vc.TODAS, vc.SIN_ESP):
            continue
        g = cm._grupo(esp) or esp
        c365 = (e.get("c") or {}).get("365")
        if c365 is None or not e.get("n"):
            continue
        a = acc.setdefault(g, {"suma": 0.0, "n": 0, "fuentes": []})
        a["suma"] += c365 * e["n"]
        a["n"] += e["n"]
        a["fuentes"].append(esp)
    return {g: {"valor": round(a["suma"] / a["n"]), "n": a["n"], "especialidades": a["fuentes"]} for g, a in acc.items()}


def laboratorio_data(hoy: date | None = None) -> dict:
    import campanas_meta_integraciones as ci
    hoy = hoy or rr._hoy()
    h = hoy.isoformat()
    d = (hoy - timedelta(days=LAB_DIAS - 1)).isoformat()
    pm = rr._seguro("panel_90", rr._panel, "meta", d, h)
    ag = rr._seguro("agenda", rr._agenda)
    if not isinstance(pm, dict) or pm.get("error"):
        return {"error": "No se pudo leer el panel de Campañas Meta de 90 días."}
    acc: dict[str, dict] = {}
    for a in pm.get("anuncios", []):
        if a.get("canal") != "meta" or not a.get("grupo"):
            continue
        g = acc.setdefault(a["grupo"], {"gasto": 0, "conv": 0, "citas": 0, "atendidos": 0, "no_asistio": 0, "centro": 0,
                                        "anuncios": 0})
        for k_src, k in (("gasto", "gasto"), ("conversaciones", "conv"), ("citas", "citas"), ("atendidos", "atendidos"),
                         ("no_asistio", "no_asistio"), ("centro", "centro")):
            g[k] += a.get(k_src) or 0
        g["anuncios"] += 1
    with db() as c:
        series, ventana = _semanas_por_grupo(c, hoy)
        v12 = _valor12m_por_grupo(c)
        ticket = _ticket_centro(c, hoy)
        asist_global = _asistencia(c, hoy)
        profs_grupo: dict[str, set] = defaultdict(set)
        try:
            from medilink import PROFESIONALES
            for pid in PROFESIONALES:
                gp = cm._grupo_prof(pid)
                if gp:
                    profs_grupo[gp].add(pid)
        except Exception:  # noqa: BLE001
            pass
        asist_grupo = {g: _asistencia(c, hoy, profs=profs_grupo.get(g, set())) for g in acc}
    cap = {}
    if isinstance(ag, dict) and not ag.get("error") and not ag.get("sin_datos"):
        cap = {g["grupo"]: g["libres_14d"] / 2 for g in ag.get("grupos", [])}
    semanas_rango = LAB_DIAS / 7
    out, sin_tasa = [], []
    for g, a in sorted(acc.items(), key=lambda x: -x[1]["gasto"]):
        if a["gasto"] <= 0 or a["conv"] <= 0:
            sin_tasa.append({"grupo": g, "motivo": "Sin gasto o sin conversaciones en 90 días: no hay costo por conversación."})
            continue
        gasto_sem = a["gasto"] / semanas_rango
        conv_sem = a["conv"] / semanas_rango
        sr = series.get(g) or []
        fit = _ajuste([x[0] for x in sr], [x[1] for x in sr]) if sr else None
        if fit:
            A, B, r2 = fit
            metodo = "ajuste"
        else:
            B = SUPUESTO_B * gasto_sem
            A = conv_sem / (1 - math.exp(-gasto_sem / B))
            r2 = None
            metodo = "supuesto"
        conv_cita = min(1.0, a["citas"] / a["conv"])
        n_des = a["atendidos"] + a["no_asistio"]
        ag_ = asist_grupo.get(g) or {}
        if n_des >= 10:
            asist, asist_n, asist_f = a["atendidos"] / n_des, n_des, "Pacientes de anuncios (90 días)"
        elif ag_.get("tasa") is not None:
            asist, asist_n, asist_f = ag_["tasa"], ag_["n"], "Todas las citas de la especialidad (ausentismo, 90 días)"
        elif asist_global.get("tasa") is not None:
            asist, asist_n, asist_f = asist_global["tasa"], asist_global["n"], "Todas las citas del centro (90 días)"
        else:
            asist, asist_n, asist_f = None, 0, "Sin dato"
        primera = round(a["centro"] / a["atendidos"]) if a["atendidos"] >= 5 else ticket.get("mediana")
        v = v12.get(g) or {}
        out.append({
            "grupo": g, "anuncios": a["anuncios"],
            "gasto_90": round(a["gasto"]), "conv_90": a["conv"], "citas_90": a["citas"], "atendidos_90": a["atendidos"],
            "gasto_sem": round(gasto_sem), "conv_sem": round(conv_sem, 1),
            "costo_conv": round(a["gasto"] / a["conv"]),
            "conv_cita": round(conv_cita, 4), "conv_cita_n": a["conv"],
            "asistencia": round(asist, 4) if asist is not None else None, "asistencia_n": asist_n, "asistencia_fuente": asist_f,
            "capacidad_sem": cap.get(g),
            "valor_12m": v.get("valor"), "valor_12m_n": v.get("n", 0), "valor_12m_esp": v.get("especialidades", []),
            "valor_primera": primera,
            "valor_primera_fuente": "Anuncios de 90 días (para el centro ÷ atendidos)" if a["atendidos"] >= 5
                                    else "Mediana de caja del centro (120 días)",
            "curva": {"A": round(A, 3), "B": round(B), "metodo": metodo, "r2": r2,
                      "semanas": [[round(x[0]), round(x[1], 1)] for x in sr], "s_ref": round(gasto_sem)},
            "confianza": _confianza(a["citas"]),
        })
    return {"rango": {"desde": d, "hasta": h, "dias": LAB_DIAS}, "ventana_semanas": ventana,
            "especialidades": out, "sin_tasa": sin_tasa,
            "capacidad": {"hay": bool(cap), "actualizado": rr._iso_epoch((ag or {}).get("actualizado_ts")) if isinstance(ag, dict) else None},
            "metodo": {"supuesto_b": SUPUESTO_B, "sens": list(SENS), "fit_min_semanas": FIT_MIN_SEMANAS, "fit_min_cv": FIT_MIN_CV},
            "escenarios": escenarios_lista()}


# ── Escenarios ──────────────────────────────────────────────────────────────

_PARAMS = {  # clave → (mín, máx)
    "presupuesto": (0, 5_000_000), "d_conv": (-50, 50), "d_asist": (-50, 50), "d_valor": (-90, 300),
}


def _ensure_escenarios(c) -> None:
    c.execute("CREATE TABLE IF NOT EXISTS radar_escenarios (id INTEGER PRIMARY KEY AUTOINCREMENT, nombre TEXT NOT NULL, "
              "params_json TEXT NOT NULL, creado_at TEXT NOT NULL)")


def escenarios_lista() -> list[dict]:
    with db() as c:   # leer no crea la tabla: nace con el primer escenario guardado
        filas = c.execute("SELECT id, nombre, params_json, creado_at FROM radar_escenarios ORDER BY id DESC").fetchall() \
            if cm._tabla_existe(c, "radar_escenarios") else []
    out = []
    for r in filas:
        try:
            p = json.loads(r[2])
        except (ValueError, TypeError):
            p = {}
        out.append({"id": r[0], "nombre": r[1], "params": p, "creado_at": r[3]})
    return out


def validar_escenario(body: dict) -> tuple[str, dict]:
    if not isinstance(body, dict):
        raise HTTPException(400, "Cuerpo inválido")
    nombre = " ".join(str(body.get("nombre") or "").split())[:80]
    if not nombre:
        raise HTTPException(400, "Falta el nombre del escenario")
    p = body.get("params")
    if not isinstance(p, dict):
        raise HTTPException(400, "Faltan los parámetros")
    grupo = str(p.get("grupo") or "").strip()[:60]
    if not grupo:
        raise HTTPException(400, "Falta la especialidad")
    limpio: dict = {"grupo": grupo, "capacidad": bool(p.get("capacidad", True))}
    for k, (lo, hi) in _PARAMS.items():
        try:
            v = float(p.get(k, 0))
        except (TypeError, ValueError):
            raise HTTPException(400, f"Parámetro inválido: {k}")
        if not math.isfinite(v) or v < lo or v > hi:
            raise HTTPException(400, f"Parámetro fuera de rango: {k}")
        limpio[k] = round(v, 2)
    res = p.get("resultado")
    if isinstance(res, dict):
        limpio["resultado"] = {k: round(float(v), 2) for k, v in res.items()
                               if k in ("conv", "citas", "atendidos", "valor", "primera", "retorno")
                               and isinstance(v, (int, float)) and math.isfinite(v)}
    return nombre, limpio


def guardar_escenario(body: dict) -> dict:
    nombre, params = validar_escenario(body)
    with db() as c:
        _ensure_escenarios(c)
        n = c.execute("SELECT COUNT(*) FROM radar_escenarios").fetchone()[0]
        if n >= MAX_ESCENARIOS:
            raise HTTPException(409, f"Ya hay {MAX_ESCENARIOS} escenarios guardados: elimine alguno antes de guardar otro.")
        cur = c.execute("INSERT INTO radar_escenarios (nombre, params_json, creado_at) VALUES (?,?,?)",
                        (nombre, json.dumps(params, ensure_ascii=False), rr._ahora().isoformat(timespec="seconds")))
        c.commit()
        eid = cur.lastrowid
    return {"ok": True, "id": eid, "nombre": nombre, "params": params}


def borrar_escenario(eid: int) -> dict:
    with db() as c:
        _ensure_escenarios(c)
        n = c.execute("DELETE FROM radar_escenarios WHERE id=?", (eid,)).rowcount
        c.commit()
    if not n:
        raise HTTPException(404, "El escenario no existe")
    return {"ok": True}


# ═══════════════════════════════════════════════════════════════════════════
# Portada (agrega lo anterior) y rutas
# ═══════════════════════════════════════════════════════════════════════════

def _saludo(ahora: datetime) -> str:
    h = ahora.hour
    return "Buen día" if 5 <= h < 12 else "Buenas tardes" if 12 <= h < 20 else "Buenas noches"


def inicio_data(ahora: datetime | None = None) -> dict:
    ahora = ahora or rr._ahora()
    op = rr._seguro("oportunidades", lambda: rr._cacheado(f"op:{ahora:%Y-%m-%dT%H:%M}"[:-1], 60,
                                                          lambda: oportunidades_data(ahora)))
    pu = rr._seguro("puente", lambda: rr._cacheado(f"puente:{ahora.date()}", rr.TTL_PESADO,
                                                   lambda: puente_data(ahora.date())))
    cd = rr._seguro("datos", lambda: rr._cacheado(f"datos:{ahora:%Y-%m-%dT%H}", 120, lambda: centro_datos_data(ahora)))
    ops = op.get("oportunidades", []) if isinstance(op, dict) else []
    return {"ahora": ahora.isoformat(timespec="minutes"), "saludo": _saludo(ahora), "nombre": NOMBRE_DUENO,
            "lectura": _lectura(ops, pu if isinstance(pu, dict) else {}, ahora.date()),
            "oportunidades": op, "puente": pu,
            "datos": ({k: cd[k] for k in ("al_dia", "total", "rojos", "ambar")}
                      | {"rojos_lista": [p["nombre"] for p in cd["procesos"] if p["estado"] == "rojo"]})
            if isinstance(cd, dict) and "error" not in cd else cd}


@rr.router.get("/inicio")
def api_inicio(request: Request, token: str | None = Query(None)):
    cm._auth(request, token)
    return rr._json(inicio_data())


@rr.router.get("/datos")
def api_datos(request: Request, token: str | None = Query(None)):
    cm._auth(request, token)
    return rr._json(centro_datos_data())


@rr.router.get("/laboratorio")
def api_laboratorio(request: Request, token: str | None = Query(None)):
    cm._auth(request, token)
    hoy = rr._hoy()
    d = rr._seguro("laboratorio", lambda: rr._cacheado(f"lab:{hoy}", rr.TTL_PESADO, lambda: laboratorio_data(hoy)))
    if isinstance(d, dict) and "error" not in d:
        d = {**d, "escenarios": escenarios_lista()}
    return rr._json(d)


@rr.router.post("/escenarios")
async def api_escenario_nuevo(request: Request, token: str | None = Query(None)):
    cm._auth(request, token)
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(400, "Cuerpo inválido")
    return rr._json(guardar_escenario(body))


@rr.router.delete("/escenarios/{eid}")
def api_escenario_borrar(eid: int, request: Request, token: str | None = Query(None)):
    cm._auth(request, token)
    return rr._json(borrar_escenario(eid))


# ═══════════════════════════════════════════════════════════════════════════
# 5 · REGLAS DE PRESUPUESTO CON DATOS (módulo Presupuesto autónomo)
# ═══════════════════════════════════════════════════════════════════════════

LEAD_DIAS = 90
FREQ_SEMANAS = 8


def _lead_time(c, hoy: date) -> dict[str, dict]:
    """Mediana de días entre que la persona agenda por el bot y su hora, por grupo."""
    desde = cm._utc_txt(cm._epoch_ini(hoy - timedelta(days=LEAD_DIAS)))
    por: dict[str, list[int]] = defaultdict(list)
    for esp, fecha, creada in c.execute("SELECT especialidad, fecha, created_at FROM citas_bot WHERE created_at >= ? "
                                        "AND fecha IS NOT NULL AND fecha != ''", (desde,)):
        g = cm._grupo(esp)
        ep = cm._utc_txt_epoch(creada)
        if not g or not ep:
            continue
        try:
            d = (date.fromisoformat(str(fecha)[:10]) - datetime.fromtimestamp(ep, cm._CL).date()).days
        except ValueError:
            continue
        if 0 <= d <= 120:
            por[g].append(d)
    return {g: {"mediana": median(v), "n": len(v)} for g, v in por.items() if v}


def _frecuencia(c, hoy: date) -> dict[str, dict]:
    """Días por semana con atenciones (espejo de ausentismo, 8 semanas), por grupo."""
    if not cm._tabla_existe(c, "ausentismo_citas"):
        return {}
    desde = (hoy - timedelta(days=7 * FREQ_SEMANAS)).isoformat()
    dias: dict[str, set] = defaultdict(set)
    for pid, f in c.execute("SELECT DISTINCT id_profesional, fecha FROM ausentismo_citas WHERE fecha >= ? AND fecha < ?",
                            (desde, hoy.isoformat())):
        g = cm._grupo_prof(pid) if pid is not None else None
        if g:
            dias[g].add(f)
    return {g: {"dias_semana": round(len(v) / FREQ_SEMANAS, 1)} for g, v in dias.items()}


def reglas_presupuesto(ag: dict, hoy: date | None = None) -> dict:
    import campanas_meta_integraciones as ci
    hoy = hoy or rr._hoy()
    if not isinstance(ag, dict) or ag.get("error") or ag.get("sin_datos"):
        return {"sin_datos": True}
    h = hoy.isoformat()
    d = (hoy - timedelta(days=LAB_DIAS - 1)).isoformat()
    pm = rr._seguro("panel_90", rr._panel, "meta", d, h)
    ret: dict[str, dict] = defaultdict(lambda: {"gasto": 0, "centro": 0, "centro12": 0, "citas": 0})
    if isinstance(pm, dict) and not pm.get("error"):
        for a in pm.get("anuncios", []):
            if a.get("canal") == "meta" and a.get("grupo"):
                r = ret[a["grupo"]]
                r["gasto"] += a.get("gasto") or 0
                r["centro"] += a.get("centro") or 0
                r["centro12"] += (a.get("valor12m") or {}).get("centro_12m") or 0
                r["citas"] += a.get("citas") or 0
    with db() as c:
        lead = _lead_time(c, hoy)
        freq = _frecuencia(c, hoy)
    fechas = ag.get("fechas") or []
    prox: dict[str, int] = {}
    for p in ag.get("profesionales", []):
        for i, n in enumerate(p.get("por_dia") or []):
            if n:
                g = p["grupo"]
                prox[g] = min(prox.get(g, 99), i)
                break
    out = []
    for g in ag.get("grupos", []):
        grupo = g["grupo"]
        if grupo in ("Otra", "", None):
            continue
        r = ret.get(grupo) or {}
        gasto90 = r.get("gasto") or 0
        rc = round(r["centro"] / gasto90, 2) if gasto90 else None
        r12 = round(r["centro12"] / gasto90, 2) if gasto90 and r.get("centro12") else None
        lt = lead.get(grupo)
        fr = freq.get(grupo)
        dp = prox.get(grupo)
        gasta = (g.get("gasto_dia") or 0) >= ci.UMBRAL_GASTO_DIA
        porque = []
        cuando = ("ninguna en 14 días" if dp is None else "hoy" if dp == 0 else "mañana" if dp == 1 else f"en {dp} días")
        porque.append(f"Próxima hora libre: {cuando}"
                      + (f" ({g['libres_14d']} cupos libres en 14 días)." if g.get("libres_14d") else "."))
        if lt:
            m = lt["mediana"]
            mt = (f"{m:.1f}".replace(".", ",") if m % 1 else str(int(m)))
            porque.append(f"Quien agenda por el bot lo hace, en la mediana, para {mt} día{'s' if m != 1 else ''} "
                          f"después ({lt['n']} citas, 90 días).")
        if fr:
            porque.append(f"La especialidad atiende {str(fr['dias_semana']).replace('.', ',')} días por semana (últimas 8 semanas).")
        if gasto90:
            porque.append(f"Retorno de 90 días: {_rx(rc)} para el centro en la primera etapa"
                          + (f"; {_rx(r12)} proyectado a 12 meses." if r12 is not None else "."))
        porque.append(f"Gasto actual: {_fmt_clp(g.get('gasto_dia'))} al día (promedio de 7 días).")
        lead_ref = (lt or {}).get("mediana")
        accion, titulo = "mantener", "Sin señal para cambiar el gasto"
        if gasta and (dp is None or (lead_ref is not None and dp > max(lead_ref * 1.5, lead_ref + 3))):
            accion, titulo = "bajar", "Bajar o pausar hasta que se abran horas"
        elif gasta and rc is not None and (r12 if r12 is not None else rc) < 0.7 and r.get("citas", 0) >= 5:
            accion, titulo = "revisar", "Revisar el anuncio antes de subir"
        elif (g.get("libres_14d") or 0) >= ci.UMBRAL_VACIOS_14D and dp is not None and (lead_ref is None or dp <= lead_ref + 2) \
                and (r12 if r12 is not None else rc or 0) >= 1:
            accion, titulo = "subir", "Subir: hay agenda dentro del plazo en que la gente agenda y el gasto se ha pagado"
        elif (g.get("libres_14d") or 0) >= ci.UMBRAL_VACIOS_14D and not gasta and gasto90 == 0:
            accion, titulo = "probar", "Sin historial de anuncios: probar con un monto acotado"
        nota = None
        if fr and fr["dias_semana"] <= 1.5:
            nota = "Atiende pocos días por semana: conviene que el anuncio corra los 3 a 7 días previos al día de atención."
        out.append({"grupo": grupo, "accion": accion, "titulo": titulo, "porque": porque, "nota": nota,
                    "datos": {"proxima_dias": dp, "lead_mediana": lead_ref, "lead_n": (lt or {}).get("n", 0),
                              "dias_semana": (fr or {}).get("dias_semana"), "retorno_90": rc, "retorno_12m": r12,
                              "gasto_dia": g.get("gasto_dia"), "libres_14d": g.get("libres_14d")}})
    orden = {"bajar": 0, "revisar": 1, "subir": 2, "probar": 3, "mantener": 4}
    out.sort(key=lambda x: (orden[x["accion"]], -(x["datos"]["gasto_dia"] or 0)))
    return {"sin_datos": False, "reglas": out}


def _rx(x: float | None) -> str:
    """Razones con dos decimales entre 0,5 y 2 (donde un decimal engaña: 0,97× no es 1,0×)."""
    if x is None:
        return "—"
    return (f"{x:.2f}" if 0.5 <= x < 2 else f"{x:.1f}").replace(".", ",") + "×"


# ═══════════════════════════════════════════════════════════════════════════
# 6 · RECEPCIÓN DENTRO DEL HORARIO (sesgo de selección)
# ═══════════════════════════════════════════════════════════════════════════

def velocidad_en_horario(d: str, h: str) -> dict:
    """La misma curva de conversión por tramo de espera, pero solo con quienes
    pasaron a recepción DENTRO del horario de atención."""
    import campanas_meta_integraciones as ci
    import recepcion_tiempos as rt
    with db() as c:
        ctx = ci._contexto(c, date.fromisoformat(d), date.fromisoformat(h), "todos", None, None)
        rap = cm._rapidez(c, ctx["clics"], ctx["e1"])
    filas = [f for f in rap.values() if f.get("necesito") and f.get("inicio") and rt.en_horario(int(f["inicio"]))]
    r = ci.resumen_velocidad(filas)
    return {k: r.get(k) for k in ("necesitaron", "mediana_min", "curva", "rapidos", "lentos", "citas_perdidas_estimadas")}


# ═══════════════════════════════════════════════════════════════════════════
# 7 · METAS DEL DUEÑO
# ═══════════════════════════════════════════════════════════════════════════

# Cuatro líneas separadas y su suma. Solo el ingreso total trae meta precargada
# (decisión del dueño: roadmap «a los 40», $40M/mes al 06-05-2032); es un
# DEFAULT en código: leer nunca escribe en radar_metas.
METAS = {
    "venta": {"nombre": "Venta mensual del CMC", "monto": None, "fecha": None,
              "formula": "Venta de caja del mes (Medilink, espejo diario)."},
    "ebitda": {"nombre": "EBITDA del CMC", "monto": None, "fecha": None,
               "formula": "Resultado operativo del módulo EBITDA: venta − honorarios − gastos registrados."},
    "honorarios": {"nombre": "Honorarios médicos del Dr. Rodrigo Olavarría", "monto": None, "fecha": None,
                   "formula": "Caja del profesional 1 × su porcentaje de honorario (bruto, antes de retención)."},
    "ingreso": {"nombre": "Ingreso total del dueño", "monto": 40_000_000, "fecha": "2032-05-06", "total": True,
                "formula": "EBITDA del CMC + honorarios médicos del Dr. Olavarría."},
}
ID_DUENO = 1
METAS_MESES = 6


def _ensure_metas(c) -> None:
    c.execute("CREATE TABLE IF NOT EXISTS radar_metas (clave TEXT PRIMARY KEY, monto INTEGER, fecha_objetivo TEXT, "
              "incluir_ebitda INTEGER DEFAULT 0, actualizado_at TEXT)")


def _meses_cerrados(hoy: date, n: int) -> list[str]:
    """Los n últimos meses cerrados, del más antiguo al más reciente."""
    out, d = [], hoy.replace(day=1)
    for _ in range(n):
        d = (d - timedelta(days=1)).replace(day=1)
        out.append(_mes_txt(d))
    return out[::-1]


def _lineas_mes(e: dict) -> dict:
    hon = next((p["bruto"] for p in e["profesionales"] if p["id"] == ID_DUENO), 0) or 0
    return {"venta": e["ingresos"], "ebitda": e["ebitda"], "honorarios": hon, "ingreso": e["ebitda"] + hon}


def ritmo_necesario(actual: float | None, meta: float | None, mes_actual: str, fecha_objetivo: str | None) -> dict | None:
    """Crecimiento mensual compuesto que lleva el valor del último mes cerrado a
    la meta en la fecha objetivo: (meta / actual) ^ (1 / meses) − 1."""
    if not meta or not fecha_objetivo or actual is None:
        return None
    try:
        f = date.fromisoformat(fecha_objetivo[:10])
        y, m = (int(x) for x in mes_actual.split("-"))
    except (ValueError, AttributeError):
        return None
    meses = (f.year - y) * 12 + (f.month - m)
    if meses <= 0:
        return {"meses": meses, "pct_mensual": None, "motivo": "La fecha objetivo ya pasó."}
    if actual <= 0:
        return {"meses": meses, "pct_mensual": None, "motivo": "El valor actual no es positivo: no hay ritmo compuesto calculable."}
    if actual >= meta:
        return {"meses": meses, "pct_mensual": 0.0, "pct_anual": 0.0, "motivo": "La meta ya se alcanza."}
    r = (meta / actual) ** (1 / meses) - 1
    return {"meses": meses, "pct_mensual": round(100 * r, 2), "pct_anual": round(100 * ((1 + r) ** 12 - 1), 1)}


def metas_data(hoy: date | None = None) -> dict:
    import ebitda_routes as eb
    hoy = hoy or rr._hoy()
    meses = _meses_cerrados(hoy, METAS_MESES)
    mes = meses[-1]
    serie: dict[str, list] = {k: [] for k in METAS}
    sin_gastos = None
    with db() as c:
        guard = {} if not cm._tabla_existe(c, "radar_metas") else {r[0]: {"monto": r[1], "fecha": r[2], "actualizado_at": r[3]}
                 for r in c.execute("SELECT clave, monto, fecha_objetivo, actualizado_at FROM radar_metas")}
        if all(cm._tabla_existe(c, t) for t in ("bi_pagos_caja", "egresos_cmc", "equipo_cmc", "pagos_cmc", "bi_atenciones")):
            with eb.solo_datos_locales():
                for m_ in meses:
                    e = eb._ebitda_mes(c, m_)
                    ln = _lineas_mes(e)
                    for k in METAS:
                        serie[k].append({"mes": m_, "valor": ln[k]})
                    if m_ == mes:
                        sin_gastos = not any(not x.get("auto") for x in e["gastos_detalle"])
    out = []
    for k, base in METAS.items():
        g = guard.get(k)
        monto = g["monto"] if g else base["monto"]
        fecha = g["fecha"] if g else base["fecha"]
        se = serie[k]
        actual = se[-1]["valor"] if se else None
        previo = se[-2]["valor"] if len(se) >= 2 else None
        out.append({"clave": k, "nombre": base["nombre"], "formula": base["formula"], "total": bool(base.get("total")),
                    "monto": monto, "fecha_objetivo": fecha, "configurada": bool(g), "precargada": not g and base["monto"] is not None,
                    "mes": mes, "actual": actual, "mes_previo": previo,
                    "variacion_pct": round(100 * (actual - previo) / abs(previo)) if actual is not None and previo else None,
                    "serie": se,
                    "avance_pct": round(100 * actual / monto) if monto and actual is not None else None,
                    "ritmo": ritmo_necesario(actual, monto, mes, fecha),
                    "sin_gastos": bool(sin_gastos) and k in ("ebitda", "ingreso")})
    return {"mes": mes, "meses": meses, "metas": out}


def guardar_meta(body: dict) -> dict:
    if not isinstance(body, dict) or body.get("clave") not in METAS:
        raise HTTPException(400, "Meta desconocida")
    monto = body.get("monto")
    try:
        monto = None if monto in (None, "") else int(float(monto))
    except (TypeError, ValueError):
        raise HTTPException(400, "Monto inválido")
    if monto is not None and not (0 < monto <= 10_000_000_000):
        raise HTTPException(400, "Monto fuera de rango")
    fecha = (body.get("fecha_objetivo") or "").strip()[:10] or None
    if fecha:
        try:
            date.fromisoformat(fecha)
        except ValueError:
            raise HTTPException(400, "Fecha inválida")
    with db() as c:
        _ensure_metas(c)
        c.execute("INSERT INTO radar_metas (clave, monto, fecha_objetivo, incluir_ebitda, actualizado_at) VALUES (?,?,?,?,?) "
                  "ON CONFLICT(clave) DO UPDATE SET monto=excluded.monto, fecha_objetivo=excluded.fecha_objetivo, "
                  "incluir_ebitda=excluded.incluir_ebitda, actualizado_at=excluded.actualizado_at",
                  (body["clave"], monto, fecha, 0,
                   rr._ahora().isoformat(timespec="seconds")))
        c.commit()
    rr.limpiar_cache()
    return {"ok": True}


@rr.router.post("/metas")
async def api_meta(request: Request, token: str | None = Query(None)):
    cm._auth(request, token)
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(400, "Cuerpo inválido")
    return rr._json(guardar_meta(body))


# ═══════════════════════════════════════════════════════════════════════════
# 8 · PRUEBA CON GRUPO DE CONTROL: SEGUNDO TOQUE (holdout)
# ═══════════════════════════════════════════════════════════════════════════

HOLDOUT_DIAS = 90
HOLDOUT_VENTANA_H = 72
HOLDOUT_MIN_N = 100


def holdout_data(hoy: date | None = None) -> dict:
    hoy = hoy or rr._hoy()
    desde = cm._utc_txt(cm._epoch_ini(hoy - timedelta(days=HOLDOUT_DIAS)))
    grupos = {"persistencia_enviada": [], "persistencia_holdout": []}
    with db() as c:
        try:
            for ph, ev, ts in c.execute("SELECT phone, event, ts FROM conversation_events WHERE event IN "
                                        "('persistencia_enviada','persistencia_holdout') AND ts >= ?", (desde,)):
                grupos[ev].append((ph, cm._utc_txt_epoch(ts) or 0))
        except Exception:  # noqa: BLE001
            pass
        citas: dict[str, list[int]] = defaultdict(list)
        if any(grupos.values()):
            for ph, ts in c.execute("SELECT phone, created_at FROM citas_bot WHERE created_at >= ?", (desde,)):
                citas[cm._clave(ph)].append(cm._utc_txt_epoch(ts) or 0)
    res = {}
    for ev, lbl in (("persistencia_enviada", "con_toque"), ("persistencia_holdout", "control")):
        n = len(grupos[ev])
        k = sum(1 for ph, t in grupos[ev] if any(t <= x <= t + HOLDOUT_VENTANA_H * 3600 for x in citas.get(cm._clave(ph), ())))
        res[lbl] = {"n": n, "agendaron": k, "pct": round(100 * k / n, 1) if n else None}
    a, b = res["con_toque"], res["control"]
    dif = ic = None
    if a["n"] and b["n"]:
        p1, p2 = a["agendaron"] / a["n"], b["agendaron"] / b["n"]
        dif = round(100 * (p1 - p2), 1)
        se = math.sqrt(p1 * (1 - p1) / a["n"] + p2 * (1 - p2) / b["n"])
        ic = [round(100 * (p1 - p2 - 1.96 * se), 1), round(100 * (p1 - p2 + 1.96 * se), 1)]
    estado = "sin_datos" if not (a["n"] or b["n"]) else "en_curso" if min(a["n"], b["n"]) < HOLDOUT_MIN_N else "con_muestra"
    return {"dias": HOLDOUT_DIAS, "ventana_h": HOLDOUT_VENTANA_H, "min_n": HOLDOUT_MIN_N, "estado": estado,
            "con_toque": a, "control": b, "diferencia_pp": dif, "ic95_pp": ic}
