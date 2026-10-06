# -*- coding: utf-8 -*-
"""Valor de un paciente nuevo en 12 meses, por especialidad de entrada.

POR QUE EXISTE
--------------
El panel Campañas Meta mide la venta de cada anuncio hasta hoy. Un paciente
que llegó hace 20 días todavía no terminó de pagar: sus controles, su kine, su
ecografía vienen después. Para decir cuánto vale un paciente nuevo de verdad
hay que mirar cohortes REALES con 12 meses cumplidos y proyectar con ellas a
los pacientes recientes. Este módulo calcula esas cohortes.

DEFINICIONES (todo sale de `bi_pagos_caja`, la caja espejo de Medilink)
-----------------------------------------------------------------------
- Paciente NUEVO: su primer pago con monto > 0 en la historia de la caja.
  Ese día es el día 0 de su cohorte.
- Especialidad de ENTRADA: la del profesional que cobró ese primer pago
  (`equipo_cmc.especialidad` por `id_medilink`). Si el mismo día hubo varios
  pagos, manda el de menor `pago_id`. Los profesionales que no están en
  `equipo_cmc` (o sin especialidad) caen en "Sin especialidad registrada" y se
  listan por id en `meta.sin_especialidad` para que el dueño los complete.
- Cohorte con 12 meses cumplidos, ventana móvil: primeros pagos entre
  hoy − 27 meses y hoy − 365 días. Así cada paciente tiene su año completo.
- Valor a 30/90/180/365 días: suma de TODOS sus pagos (todas las
  especialidades) dentro de ese plazo desde el día 0. Dos medidas:
    venta   = monto cobrado en caja.
    centro  = monto × (1 − pct/100), con `pct` = lo que se lleva el PROFESIONAL
              (`_pct_honorarios`: equipo_cmc, 70% por defecto, Abarca 62%).
              Las radiografías de Imagendent siguen su regla: al centro le
              queda venta − costo del convenio, sin honorario.
- Curva acumulada: valor medio por paciente en cada día 0..365. Sirve para
  proyectar: un paciente de 40 días ya "lleva" lo que la cohorte llevaba a los
  40 días; lo que falta es el valor a 12 meses menos esa cifra.
- Derivación ("dónde sigue el recorrido"): de los pacientes que entraron por
  la especialidad E, qué % pagó también en otra especialidad X dentro de 12
  meses, cuánto vende en promedio quien pasa, y el aporte por cada paciente de
  entrada (venta total en X ÷ n de la cohorte E). "Pasó" NO significa
  "derivado por el médico": es que pagó en otra especialidad.
- Muestra chica: cohortes con menos de 30 pacientes (se calcula además un
  intervalo de confianza del 95% del valor a 12 meses).

ORTODONCIA COMO TRATAMIENTO
---------------------------
Casi nadie entra por ortodoncia: entra por odontología general y después
instala. Por eso aparte se mide:
  (a) de los pacientes nuevos de la cohorte, quiénes instalaron en sus
      primeros 12 meses y cuánto valen (todas las especialidades y la parte
      de ortodoncia), y
  (b) desde la instalación: pacientes cuya instalación cae en la ventana,
      valor de sus 12 meses desde ese día.
"Instaló" usa el mismo criterio que el panel (`_instalaciones`): un cobro de la
ortodoncista (id 66) de $80.000 o más, o una atención suya de $80.000 o más
(`bi_atenciones`), o `ortodoncia_cache.tipo='instalacion'`. El embudo
(`orto_embudo`) no se usa acá porque va por teléfono, no por paciente.

CACHE
-----
`valor_cohorte_especialidad` (una fila por especialidad de entrada + una fila
"Todas las especialidades") y `valor_cohorte_estado` (metadatos). Se recalcula
el día 1 de cada mes 04:30 CLT (`job_valor_cohortes`, flag
VALOR_COHORTES_ACTIVE) y al vuelo si nunca se calculó. El panel solo LEE: no
llama a Medilink ni a Meta.
"""
from __future__ import annotations

import asyncio
import calendar
import json
import logging
import math
import threading
import time
from collections import defaultdict
from datetime import date, datetime, timedelta

from session import db

log = logging.getLogger("valor_cohortes")

HORIZONTES = (30, 90, 180, 365)
CHECKS = (30, 60, 90, 120, 180, 270, 365)   # puntos de la curva de derivación
DIAS_ANIO = 365
MUESTRA_CHICA = 30          # cohortes con menos pacientes → "muestra chica"
MUESTRA_ANUNCIO = 5         # anuncio con menos pacientes nuevos → "muestra chica"
PROF_MIN_N = 10             # profesionales de entrada con menos pacientes no se detallan
SIN_ESP = "Sin especialidad registrada"
TODAS = "Todas las especialidades"
MESES_ATRAS = 27
OBSOLETA_DIAS = 45

_LOCK = threading.Lock()

NOTA_PROYECCION = ("Proyección según cohortes reales; no es venta realizada. A cada paciente nuevo se le suma lo que "
                   "le falta, en promedio, a un paciente de su misma especialidad de entrada para llegar a su valor "
                   "a 12 meses.")


def _cm():
    import campanas_meta_routes as cm   # import tardío: cm importa este módulo
    return cm


# ── Tablas ──────────────────────────────────────────────────────────────────

def ensure(c) -> None:
    c.execute("""CREATE TABLE IF NOT EXISTS valor_cohorte_especialidad (
        especialidad TEXT PRIMARY KEY, n INTEGER NOT NULL,
        v30 INTEGER, v90 INTEGER, v180 INTEGER, v365 INTEGER,
        c30 INTEGER, c90 INTEGER, c180 INTEGER, c365 INTEGER,
        payload TEXT NOT NULL, calculado_ts INTEGER NOT NULL)""")
    c.execute("CREATE TABLE IF NOT EXISTS valor_cohorte_estado (clave TEXT PRIMARY KEY, valor TEXT)")


# ── Utilidades ──────────────────────────────────────────────────────────────

def _meses_atras(d: date, m: int) -> date:
    t = d.year * 12 + d.month - 1 - m
    y, mo = divmod(t, 12)
    return date(y, mo + 1, min(d.day, calendar.monthrange(y, mo + 1)[1]))


def _canon(txt: str | None) -> str | None:
    t = " ".join((txt or "").split())
    if not t:
        return None
    if t.casefold().startswith("psicolog"):
        return "Psicología"
    return t


def _especialidades(c) -> dict[int, str]:
    """id_medilink → especialidad (equipo_cmc). Sin fila o vacía: no aparece."""
    out: dict[int, str] = {}
    vistos: dict[str, str] = {}
    try:
        filas = c.execute("SELECT id_medilink, especialidad FROM equipo_cmc WHERE id_medilink IS NOT NULL").fetchall()
    except Exception:   # noqa: BLE001 — entorno sin equipo_cmc
        return out
    for r in filas:
        esp = _canon(r[1])
        if esp:
            out.setdefault(int(r[0]), vistos.setdefault(esp.casefold(), esp))
    return out


def _cargar_pagos(c) -> dict[int, list[tuple]]:
    """paciente → [(fecha, id_prof, monto, pago_id)] ordenado por fecha, pago_id."""
    por: dict[int, list[tuple]] = defaultdict(list)
    try:
        filas = c.execute(
            "SELECT pago_id, id_paciente, id_profesional, substr(fecha,1,10), monto FROM bi_pagos_caja "
            "WHERE id_paciente IS NOT NULL AND monto > 0 AND fecha IS NOT NULL AND fecha != '' "
            "ORDER BY id_paciente, substr(fecha,1,10), pago_id")
        for r in filas:
            por[int(r[1])].append((r[3], r[2], int(r[4]), r[0]))
    except Exception as e:   # noqa: BLE001
        log.warning("valor_cohortes: sin caja: %s", e)
    return por


def _ajustes_imagendent(c, pct: dict, defecto: int, pagos: dict) -> dict[int, list[tuple[str, float]]]:
    """paciente → [(fecha, Δ centro)] por las radiografías de Imagendent.

    Misma regla de `_venta_por_anuncio`: el examen lo hace Imagendent y al
    centro le queda venta − costo (sin honorario). El cobro se mueve de la
    línea del profesional a la de Imagendent; la venta total no cambia, solo
    el centro. Solo se ajusta si el cobro aparece en caja ese día, con ese
    profesional: no se inventa."""
    adj: dict[int, list[tuple[str, float]]] = defaultdict(list)
    try:
        filas = c.execute("SELECT id_paciente, id_profesional, fecha, venta, cobrado, costo FROM convenio_consumo "
                          "WHERE convenio='imagendent' AND id_paciente IS NOT NULL").fetchall()
    except Exception:   # noqa: BLE001
        return adj
    resto: dict[tuple, int] = {}
    for r in filas:
        pid, prof, fecha = int(r[0]), r[1], (r[2] or "")[:10]
        monto = int(r[4] or r[3] or 0)
        if not monto or pid not in pagos:
            continue
        k = (pid, fecha, prof)
        if k not in resto:
            resto[k] = sum(m for f, p, m, _ in pagos[pid] if f == fecha and p == prof)
        if resto[k] < monto:
            continue
        resto[k] -= monto
        p = pct.get(prof) or defecto
        adj[pid].append((fecha, (monto - int(r[5] or 0)) - monto * (100 - p) / 100))
    return adj


def _fechas_instalacion(c, pagos: dict) -> dict[int, str]:
    """paciente → fecha de su instalación de ortodoncia (la más antigua)."""
    cm = _cm()
    inst: dict[int, str] = {}
    for pid, lista in pagos.items():
        for f, p, m, _ in lista:
            if p == cm.ORTODONCISTA and m >= cm.INSTALACION_MIN:
                inst[pid] = f
                break
    for q, args in (("SELECT id_paciente, substr(fecha,1,10) FROM bi_atenciones WHERE id_profesional=? AND total>=?",
                     (cm.ORTODONCISTA, cm.INSTALACION_MIN)),
                    ("SELECT id_paciente, substr(fecha,1,10) FROM ortodoncia_cache WHERE tipo='instalacion'", ())):
        try:
            for r in c.execute(q, args):
                if r[0] and r[1] and (int(r[0]) not in inst or r[1] < inst[int(r[0])]):
                    inst[int(r[0])] = r[1]
        except Exception:   # noqa: BLE001
            pass
    return inst


def _ic(vals: list[float]) -> list[int] | None:
    """Intervalo de confianza del 95% de la media (aprox. normal)."""
    n = len(vals)
    if n < 2:
        return None
    m = sum(vals) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in vals) / (n - 1))
    e = 1.96 * sd / math.sqrt(n)
    return [max(0, round(m - e)), round(m + e)]


# ── Cálculo de cohortes ─────────────────────────────────────────────────────

def _grupo() -> dict:
    return {"n": 0, "v": dict.fromkeys(HORIZONTES, 0), "c": dict.fromkeys(HORIZONTES, 0.0),
            "dv": [0.0] * (DIAS_ANIO + 1), "dc": [0.0] * (DIAS_ANIO + 1), "vis": 0, "v_ent": 0,
            "v365s": [], "c365s": [], "deriv": {}}


def _sumar_grupo(g: dict, ev: list[tuple], esp_ent: str) -> None:
    g["n"] += 1
    v365 = c365 = 0.0
    dias = set()
    for off, m, ce, esp, _prof in ev:
        for h in HORIZONTES:
            if off <= h:
                g["v"][h] += m
                g["c"][h] += ce
        g["dv"][off] += m
        g["dc"][off] += ce
        v365 += m
        c365 += ce
        if esp is not None:
            dias.add(off)
            if esp == esp_ent:
                g["v_ent"] += m
    g["vis"] += len(dias)
    g["v365s"].append(v365)
    g["c365s"].append(c365)


def _fila_grupo(label: str, g: dict) -> dict:
    n = g["n"]

    def med(x):
        return round(x / n) if n else 0

    def curva(arr):
        out, acc = [], 0.0
        for x in arr:
            acc += x
            out.append(round(acc / n))
        return out

    deriv = []
    for esp, d in g["deriv"].items():
        deriv.append({"esp": esp, "n": d["n"], "pct": round(100 * d["n"] / n, 1),
                      "venta_pasan": round(d["v"] / d["n"]), "centro_pasan": round(d["c"] / d["n"]),
                      "aporte_v": round(d["v"] / n), "aporte_c": round(d["c"] / n),
                      "muestra_chica": d["n"] < 5, "cum": d["cum"]})
    deriv.sort(key=lambda x: -x["aporte_v"])
    return {"especialidad": label, "n": n, "muestra_chica": n < MUESTRA_CHICA,
            "v": {str(h): med(g["v"][h]) for h in HORIZONTES},
            "c": {str(h): med(g["c"][h]) for h in HORIZONTES},
            "ic_v365": _ic(g["v365s"]), "ic_c365": _ic(g["c365s"]),
            "v365_entrada": med(g["v_ent"]), "v365_otras": med(g["v"][DIAS_ANIO] - g["v_ent"]),
            "visitas": round(g["vis"] / n, 2) if n else 0,
            "curva_v": curva(g["dv"]), "curva_c": curva(g["dc"]), "deriv": deriv}


def _orto(cohorte: list[tuple], inst: dict, pagos: dict, pct: dict, defecto: int, lo: str, hi: str) -> dict:
    cm = _cm()
    ort = cm.ORTODONCISTA

    def _cen(prof, m):
        return m * (100 - (pct.get(prof) or defecto)) / 100

    # (a) pacientes nuevos de la cohorte que instalaron en sus primeros 12 meses
    por_ent: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    sv = sc = sov = soc = 0.0
    k = 0
    for pid, esp_ent, f0 in cohorte:
        por_ent[esp_ent][0] += 1
        i = inst.get(pid)
        if not i:
            continue
        d0 = date.fromisoformat(f0)
        if not (f0 <= i <= (d0 + timedelta(days=DIAS_ANIO)).isoformat()):
            continue
        k += 1
        por_ent[esp_ent][1] += 1
        lim = (d0 + timedelta(days=DIAS_ANIO)).isoformat()
        for f, p, m, _ in pagos[pid]:
            if f > lim:
                break
            sv += m
            sc += _cen(p, m)
            if p == ort:
                sov += m
                soc += _cen(p, m)
    n_coh = len(cohorte)
    a = {"n_cohorte": n_coh, "instalan": k, "pct": round(100 * k / n_coh, 2) if n_coh else None,
         "muestra_chica": k < MUESTRA_CHICA,
         "v365": round(sv / k) if k else None, "c365": round(sc / k) if k else None,
         "v365_orto": round(sov / k) if k else None, "c365_orto": round(soc / k) if k else None,
         "por_entrada": sorted(({"esp": e, "n": v[0], "instalan": v[1],
                                 "pct": round(100 * v[1] / v[0], 2) if v[0] else None}
                                for e, v in por_ent.items() if v[1]), key=lambda x: -x["instalan"])}
    # (b) desde la instalación: instalaciones dentro de la ventana, 12 meses desde ese día
    n = 0
    bv = bc = bov = boc = 0.0
    for pid, i in inst.items():
        if not (lo <= i <= hi) or pid not in pagos:
            continue
        lim = (date.fromisoformat(i) + timedelta(days=DIAS_ANIO)).isoformat()
        n += 1
        for f, p, m, _ in pagos[pid]:
            if f < i:
                continue
            if f > lim:
                break
            bv += m
            bc += _cen(p, m)
            if p == ort:
                bov += m
                boc += _cen(p, m)
    b = {"n": n, "muestra_chica": n < MUESTRA_CHICA,
         "v365": round(bv / n) if n else None, "c365": round(bc / n) if n else None,
         "v365_orto": round(bov / n) if n else None, "c365_orto": round(boc / n) if n else None}
    return {"entrada": a, "desde_instalacion": b,
            "criterio": ("Instaló = cobro de la ortodoncista (id %d) de $%s o más, atención suya de ese monto o "
                         "marca de instalación en ortodoncia_cache." % (ort, f"{cm.INSTALACION_MIN:,}".replace(",", ".")))}


def calcular(c, hoy: date | None = None) -> dict:
    """Calcula cohortes y metadatos. NO escribe: devuelve {"filas": [...], "meta": {...}}."""
    cm = _cm()
    hoy = hoy or cm._hoy()
    hi = (hoy - timedelta(days=DIAS_ANIO)).isoformat()
    lo = _meses_atras(hoy, MESES_ATRAS).isoformat()
    pct = cm._pct_honorarios(c)
    defecto = cm.PCT_HONORARIO_DEFAULT
    esp_de = _especialidades(c)
    pagos = _cargar_pagos(c)
    adj = _ajustes_imagendent(c, pct, defecto, pagos)
    nombres = cm._nombres_profesionales(c)

    grupos: dict[str, dict] = defaultdict(_grupo)
    profs: dict[int, dict] = {}
    sin_esp: dict[int, list[float]] = defaultdict(lambda: [0, 0.0])
    cohorte: list[tuple] = []
    historia_desde = None
    for lista in pagos.values():
        if historia_desde is None or lista[0][0] < historia_desde:
            historia_desde = lista[0][0]

    for pid, lista in pagos.items():
        f0 = lista[0][0]
        if not (lo <= f0 <= hi):
            continue
        d0 = date.fromisoformat(f0)
        prof0 = lista[0][1]
        ent = esp_de.get(prof0) or SIN_ESP
        ev: list[tuple] = []
        for f, prof, m, _ in lista:
            off = (date.fromisoformat(f) - d0).days
            if off > DIAS_ANIO:
                break
            esp = esp_de.get(prof) or SIN_ESP
            if esp == SIN_ESP:
                s = sin_esp[prof]
                s[0] += 1
                s[1] += m
            ev.append((off, m, m * (100 - (pct.get(prof) or defecto)) / 100, esp, prof))
        for f, delta in adj.get(pid, ()):
            off = (date.fromisoformat(f) - d0).days
            if 0 <= off <= DIAS_ANIO:
                ev.append((off, 0, delta, None, None))
        cohorte.append((pid, ent, f0))
        _sumar_grupo(grupos[TODAS], ev, ent)
        g = grupos[ent]
        _sumar_grupo(g, ev, ent)
        # derivación: a qué otras especialidades pagó dentro de 12 meses
        dest: dict[str, list] = {}
        for off, m, ce, esp, prof in ev:
            if prof is None or esp == ent:
                continue
            d = dest.setdefault(esp, [off, 0, 0.0])
            d[0] = min(d[0], off)
            d[1] += m
            d[2] += ce
        for esp, (off1, v, ce) in dest.items():
            d = g["deriv"].setdefault(esp, {"n": 0, "v": 0, "c": 0.0, "cum": [0] * len(CHECKS)})
            d["n"] += 1
            d["v"] += v
            d["c"] += ce
            for j, ck in enumerate(CHECKS):
                if off1 <= ck:
                    d["cum"][j] += 1
        p = profs.setdefault(prof0, {"n": 0, "pasan": 0, "dest": defaultdict(int), "esp": ent})
        p["n"] += 1
        if dest:
            p["pasan"] += 1
            for esp in dest:
                p["dest"][esp] += 1

    filas = [_fila_grupo(lbl, g) for lbl, g in grupos.items() if g["n"]]
    filas.sort(key=lambda x: (x["especialidad"] != TODAS, -x["n"]))

    profesionales, otros_n = [], 0
    for pid, p in sorted(profs.items(), key=lambda kv: -kv[1]["n"]):
        if p["n"] < PROF_MIN_N:
            otros_n += p["n"]
            continue
        profesionales.append({
            "id": pid, "nombre": nombres.get(pid) or f"Profesional {pid}", "especialidad": p["esp"], "n": p["n"],
            "pasan": p["pasan"], "pct": round(100 * p["pasan"] / p["n"], 1), "muestra_chica": p["n"] < MUESTRA_CHICA,
            "destinos": [{"esp": e, "pct": round(100 * k / p["n"], 1)}
                         for e, k in sorted(p["dest"].items(), key=lambda kv: -kv[1])[:3]]})

    inst = _fechas_instalacion(c, pagos)
    meta = {"hoy": hoy.isoformat(), "ventana": {"desde": lo, "hasta": hi}, "n_total": len(cohorte),
            "historia_desde": historia_desde, "calculado_ts": int(time.time()),
            "profesionales": profesionales, "profesionales_otros_n": otros_n,
            "sin_especialidad": sorted(({"id": pid, "nombre": nombres.get(pid) or f"Profesional {pid}",
                                         "pagos": v[0], "venta": round(v[1])} for pid, v in sin_esp.items()),
                                       key=lambda x: -x["venta"]),
            "orto": _orto(cohorte, inst, pagos, pct, defecto, lo, hi)}
    return {"filas": filas, "meta": meta}


def guardar(c, res: dict) -> None:
    ensure(c)
    ts = res["meta"]["calculado_ts"]
    c.execute("DELETE FROM valor_cohorte_especialidad")
    for f in res["filas"]:
        c.execute("INSERT INTO valor_cohorte_especialidad (especialidad, n, v30, v90, v180, v365, c30, c90, c180, c365, "
                  "payload, calculado_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                  (f["especialidad"], f["n"], f["v"]["30"], f["v"]["90"], f["v"]["180"], f["v"]["365"],
                   f["c"]["30"], f["c"]["90"], f["c"]["180"], f["c"]["365"],
                   json.dumps(f, ensure_ascii=False), ts))
    c.execute("INSERT INTO valor_cohorte_estado (clave, valor) VALUES ('meta', ?) "
              "ON CONFLICT(clave) DO UPDATE SET valor=excluded.valor", (json.dumps(res["meta"], ensure_ascii=False),))


def recalcular(hoy: date | None = None) -> dict:
    """Calcula y guarda. Lo usa el job mensual y el botón/endpoint de recálculo."""
    with _LOCK:
        with db() as c:
            ensure(c)
            res = calcular(c, hoy)
            guardar(c, res)
    log.info("valor_cohortes: %d cohortes, %d pacientes", len(res["filas"]), res["meta"]["n_total"])
    return {"ok": True, "cohortes": len(res["filas"]), "pacientes": res["meta"]["n_total"]}


async def job_valor_cohortes() -> None:
    """Día 1 de cada mes, 04:30 CLT. Flag VALOR_COHORTES_ACTIVE. Solo lee la DB local."""
    import config
    if not getattr(config, "VALOR_COHORTES_ACTIVE", True):
        log.info("valor_cohortes: apagado (VALOR_COHORTES_ACTIVE=false)")
        return
    try:
        await asyncio.to_thread(recalcular)
    except Exception as e:   # noqa: BLE001
        log.error("valor_cohortes: falló: %s", e, exc_info=True)


# ── Lectura (la usa el panel; nunca recalcula salvo que no exista nada) ─────

def cargar(c, hoy: date | None = None) -> dict:
    """{"meta": {...}, "esp": {etiqueta: fila}, "desactualizada": bool}. Si nunca se calculó, calcula al vuelo."""
    ensure(c)

    def _leer():
        m = c.execute("SELECT valor FROM valor_cohorte_estado WHERE clave='meta'").fetchone()
        return json.loads(m[0]) if m else None
    meta = _leer()
    if meta is None:
        with _LOCK:
            meta = _leer()
            if meta is None:
                res = calcular(c, hoy)
                guardar(c, res)
                meta = res["meta"]
    esp = {}
    for r in c.execute("SELECT payload FROM valor_cohorte_especialidad"):
        f = json.loads(r[0])
        esp[f["especialidad"]] = f
    viejo = (time.time() - (meta.get("calculado_ts") or 0)) > OBSOLETA_DIAS * 86400
    return {"meta": meta, "esp": esp, "desactualizada": viejo}


def _fila_proy(vc: dict, esp: str) -> tuple[dict | None, str | None]:
    """Cohorte a usar para proyectar: la propia si tiene 30+ pacientes; si no, la global."""
    f = vc["esp"].get(esp)
    if f and f["n"] >= MUESTRA_CHICA:
        return f, "propia"
    g = vc["esp"].get(TODAS)
    if g and g["n"] >= MUESTRA_CHICA:
        return g, "global"
    return None, None


def proyectar(vc: dict, esp: str, edad: int, pagado_v: float, pagado_c: float) -> tuple[float, float, str | None]:
    """Valor a 12 meses de un paciente nuevo: lo pagado + lo que le falta, en promedio, a su cohorte.

    proyectado = pagado + max(0, valor_12m_cohorte − valor_cohorte_a_su_edad)
    Con edad ≥ 365 días no se proyecta (ya cumplió el año). Sin cohorte
    utilizable devuelve lo pagado y fuente None."""
    f, fuente = _fila_proy(vc, esp)
    if not f or edad >= DIAS_ANIO:
        return pagado_v, pagado_c, (fuente if f else None)
    a = max(0, int(edad))
    cv, cc = f["curva_v"], f["curva_c"]
    return (pagado_v + max(0, cv[DIAS_ANIO] - cv[a]), pagado_c + max(0, cc[DIAS_ANIO] - cc[a]), fuente)


def _frac(cum: list[int], n: int, edad: int) -> float:
    """Fracción esperada de la cohorte que ya pasó a la otra especialidad a los `edad` días (interpolada)."""
    if not n:
        return 0.0
    a = min(max(edad, 0), DIAS_ANIO)
    px, py = 0, 0.0
    for ck, k in zip(CHECKS, cum):
        if a <= ck:
            return py + (k / n - py) * ((a - px) / (ck - px))
        px, py = ck, k / n
    return py


# ── Por anuncio / campaña ───────────────────────────────────────────────────

def acc() -> dict:
    return {"nuevos": 0, "nuevos_rango": 0, "nuevos_venta": 0, "nuevos_centro": 0.0, "ya": 0, "ya_venta": 0,
            "ya_centro": 0.0, "proy_v": 0.0, "proy_c": 0.0, "n_global": 0, "n_sin": 0, "n_maduros": 0, "rec": {}}


def sumar(dst: dict, src: dict | None) -> None:
    if not src:
        return
    for k, v in src.items():
        if k == "rec":
            for ent, r in v.items():
                d = dst["rec"].setdefault(ent, {"n": 0, "obs": {}, "exp": {}, "ref": {}, "chica": r["chica"],
                                                "sin_cohorte": r["sin_cohorte"]})
                d["n"] += r["n"]
                for x, (n, venta) in r["obs"].items():
                    o = d["obs"].setdefault(x, [0, 0])
                    o[0] += n
                    o[1] += venta
                for x, e in r["exp"].items():
                    d["exp"][x] = d["exp"].get(x, 0.0) + e
                d["ref"].update(r["ref"])
        else:
            dst[k] += v


def valor_por_anuncio(c, detalle: dict, vc: dict, d: date, h: date, hoy: date) -> tuple[dict, dict]:
    """(por anuncio, total) de acumuladores a partir de `detalle` (lo llena `_venta_por_anuncio`).

    Nuevo = sin pago previo a su fecha de inicio de atribución. Para los
    nuevos: proyección a 12 meses y recorrido (a qué otras especialidades
    pagaron). Para los que ya eran pacientes: solo lo pagado, aparte."""
    esp_de = _especialidades(c)
    por_ad: dict[str, dict] = defaultdict(acc)
    tot = acc()
    for det in detalle.values():
        accs = (por_ad[det["ad_id"]], tot)
        if not det.get("nuevo"):
            for a in accs:
                a["ya"] += 1
                a["ya_venta"] += det["venta"]
                a["ya_centro"] += det["centro"]
            continue
        pg = sorted(p for p in (det.get("pagos") or ()) if p[2] > 0)
        if not pg:
            continue
        f0, prof0 = pg[0][0], pg[0][1]
        ent = esp_de.get(prof0) or SIN_ESP
        edad = (hoy - date.fromisoformat(f0)).days
        pv, pc, fuente = proyectar(vc, ent, edad, det["venta"], det["centro"])
        fila = vc["esp"].get(ent)
        # recorrido observado dentro de los primeros 12 meses
        d0 = date.fromisoformat(f0)
        dest: dict[str, int] = {}
        for f, prof, m, _ in pg:
            if (date.fromisoformat(f) - d0).days > DIAS_ANIO:
                break
            esp = esp_de.get(prof) or SIN_ESP
            if esp != ent:
                dest[esp] = dest.get(esp, 0) + m
        esperado = {}
        ref = {}
        if fila:
            for dv in fila["deriv"]:
                esperado[dv["esp"]] = _frac(dv["cum"], fila["n"], edad)
                ref[dv["esp"]] = dv["venta_pasan"]
        for a in accs:
            a["nuevos"] += 1
            a["nuevos_rango"] += 1 if d.isoformat() <= f0 <= h.isoformat() else 0
            a["nuevos_venta"] += det["venta"]
            a["nuevos_centro"] += det["centro"]
            a["proy_v"] += pv
            a["proy_c"] += pc
            a["n_global"] += 1 if fuente == "global" else 0
            a["n_sin"] += 1 if fuente is None else 0
            a["n_maduros"] += 1 if edad >= DIAS_ANIO else 0
            r = a["rec"].setdefault(ent, {"n": 0, "obs": {}, "exp": {}, "ref": {},
                                          "chica": bool(fila and fila["n"] < MUESTRA_CHICA),
                                          "sin_cohorte": fila is None})
            r["n"] += 1
            for x, venta in dest.items():
                o = r["obs"].setdefault(x, [0, 0])
                o[0] += 1
                o[1] += venta
            for x, e in esperado.items():
                r["exp"][x] = r["exp"].get(x, 0.0) + e
            r["ref"].update(ref)
    return dict(por_ad), tot


def _recorrido(rec: dict) -> list[dict]:
    out = []
    for ent, r in sorted(rec.items(), key=lambda kv: -kv[1]["n"])[:4]:
        n = r["n"]
        xs = []
        for x in set(r["obs"]) | set(r["exp"]):
            on, ov = r["obs"].get(x, [0, 0])
            e = r["exp"].get(x, 0.0)
            if on == 0 and e < 0.5:
                continue
            xs.append({"esp": x, "n": on, "pct": round(100 * on / n, 1), "venta": round(ov),
                       "esperado_n": round(e, 1), "esperado_pct": round(100 * e / n, 1),
                       "venta_pasan_cohorte": r["ref"].get(x)})
        xs.sort(key=lambda z: -max(z["n"], z["esperado_n"]))
        out.append({"entrada": ent, "n": n, "muestra_chica": r["chica"] or n < MUESTRA_ANUNCIO,
                    "sin_cohorte": r["sin_cohorte"], "pasaron": xs[:8]})
    return out


def cerrar(a: dict | None, gasto: float) -> dict:
    a = a or acc()
    n = a["nuevos"]
    centro12 = a["proy_c"] + a["ya_centro"]
    return {
        "nuevos": n, "nuevos_rango": a["nuevos_rango"], "nuevos_venta": round(a["nuevos_venta"]),
        "nuevos_centro": round(a["nuevos_centro"]),
        "ya": a["ya"], "ya_venta": round(a["ya_venta"]), "ya_centro": round(a["ya_centro"]),
        "costo_nuevo": round(gasto / n) if gasto and n else None,
        "proy_venta": round(a["proy_v"]), "proy_centro": round(a["proy_c"]),
        "centro_12m": round(centro12),
        "retorno_12m": round(centro12 / gasto, 2) if gasto else None,
        "n_cohorte_global": a["n_global"], "n_sin_cohorte": a["n_sin"], "n_maduros": a["n_maduros"],
        "muestra_chica": n < MUESTRA_ANUNCIO,
        "recorrido": _recorrido(a["rec"]),
    }


def nuevos_del_centro(c, d: date, h: date) -> int:
    """Pacientes cuyo PRIMER pago de la historia cae en [d, h] (todos los del centro, no solo los de anuncios)."""
    try:
        r = c.execute("SELECT COUNT(*) FROM (SELECT id_paciente FROM bi_pagos_caja WHERE id_paciente IS NOT NULL "
                      "AND monto > 0 AND fecha IS NOT NULL AND fecha != '' GROUP BY id_paciente "
                      "HAVING MIN(substr(fecha,1,10)) >= ? AND MIN(substr(fecha,1,10)) <= ?)",
                      (d.isoformat(), h.isoformat())).fetchone()
        return int(r[0])
    except Exception as e:   # noqa: BLE001
        log.warning("valor_cohortes: nuevos del centro no disponible: %s", e)
        return 0


def resumen_meta(vc: dict) -> dict:
    m = vc["meta"]
    return {"calculado": datetime.fromtimestamp(m.get("calculado_ts") or 0).strftime("%Y-%m-%d"),
            "desactualizada": vc.get("desactualizada", False), "hay_cohortes": bool(m.get("n_total")),
            "ventana": m.get("ventana"), "n_total": m.get("n_total"), "nota": NOTA_PROYECCION}


# ── Vista pública (endpoint /valor12m) ──────────────────────────────────────

def publico(c) -> dict:
    """Cohortes para la UI, sin las curvas diarias."""
    vc = cargar(c)
    meta = vc["meta"]
    esp = []
    matriz = []
    for f in vc["esp"].values():
        row = {k: f[k] for k in ("especialidad", "n", "muestra_chica", "v", "c", "ic_v365", "ic_c365",
                                 "v365_entrada", "v365_otras", "visitas")}
        esp.append(row)
        if f["especialidad"] == TODAS:
            continue
        matriz.append({"entrada": f["especialidad"], "n": f["n"], "muestra_chica": f["muestra_chica"],
                       "destinos": [{k: dv[k] for k in ("esp", "n", "pct", "venta_pasan", "centro_pasan",
                                                        "aporte_v", "aporte_c", "muestra_chica")}
                                    for dv in f["deriv"][:8]]})
    esp.sort(key=lambda x: (x["especialidad"] != TODAS, -x["n"]))
    matriz.sort(key=lambda x: -x["n"])
    return {"meta": resumen_meta(vc), "especialidades": esp, "matriz": matriz,
            "profesionales": meta.get("profesionales", []), "profesionales_otros_n": meta.get("profesionales_otros_n", 0),
            "sin_especialidad": meta.get("sin_especialidad", []), "orto": meta.get("orto"),
            "historia_desde": meta.get("historia_desde"), "muestra_min": MUESTRA_CHICA,
            "titulo_matriz": "Dónde sigue el recorrido"}
