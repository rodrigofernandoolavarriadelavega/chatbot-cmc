"""Panel del Día — cache nocturna de CAPACIDAD REAL por profesional.

Problema: `bi_pagos_caja` solo tiene atenciones PAGADAS. Muchas Fonasa/bono que
Medilink marca "Atendido" no dejan registro de pago → el proxy p85 de pagos/día
SUBESTIMA la capacidad real, y el potencial del Panel del Día sale bajo.

Verdad completa = Medilink `/citas` por (profesional, fecha). Pero consultarlo en
vivo para los ~24 profesionales gatilla el rate-limit (el fan-out que tumbó
`/admin/api/agenda-dia`). Solución: este job nocturno barre Medilink **secuencial
y throttleado** (1 llamada por prof/fecha, `citas_dia_lite`, sin fan-out de
/pacientes) y guarda los conteos en `panel_cap_cache`. El endpoint operativo lee
ese cache para el `cap` (→ potencial) y la ocupación reales; si no hay cache, cae
al proxy de pagos.

Regla anti-deadlock (incidente 2026-06-10): NUNCA se sostiene la conexión SQLite
mientras se llama a Medilink. Fase 1 (async) acumula en memoria; fase 2 (sync)
escribe en una sola transacción.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta

log = logging.getLogger("panel_dia_jobs")

# ventana móvil por defecto: días hacia atrás (histórico) y hacia adelante (agenda futura)
_DIAS_ATRAS = 21
_DIAS_ADELANTE = 3
# días recientes que SIEMPRE se re-consultan (los estados/citas aún mutan);
# los más viejos ya cacheados se saltan (las citas pasadas no cambian)
_MUTABLE_DIAS = 2
# pausa entre llamadas Medilink — secuencial y suave para no gatillar 429
_THROTTLE = 0.4


def _ensure_cap_table(c) -> None:
    c.execute(
        """CREATE TABLE IF NOT EXISTS panel_cap_cache (
              id_profesional INTEGER NOT NULL,
              fecha          TEXT    NOT NULL,
              cap            INTEGER NOT NULL,
              n_citas        INTEGER NOT NULL,
              n_at           INTEGER NOT NULL,
              n_falto        INTEGER NOT NULL,
              updated_at     TEXT,
              PRIMARY KEY (id_profesional, fecha)
           )"""
    )


def _h2m(h):
    try:
        a, b = str(h).split(":")[:2]
        return int(a) * 60 + int(b)
    except Exception:
        return None


def _cap_counts(citas: list, paid: set, fecha: str, intervalo: int, hoy_iso: str) -> dict:
    """Replica la grilla por horario de /api/panel-dia/agenda pero devuelve SOLO
    conteos: cap (slots de la ventana de trabajo, libres incluidos), n_citas,
    n_at (atendidos), n_falto (no se presentó)."""
    es_pasado = fecha < hoy_iso
    n_at = n_falto = 0
    mins = []
    for ct in citas:
        est_txt = (ct.get("estado_cita") or "").lower()
        pagado = ct.get("id_paciente") in paid
        if pagado or "atend" in est_txt:
            n_at += 1
        elif any(k in est_txt for k in ("anul", "cancel", "no asist", "no asiste", "ausente", "falt", "no lleg")):
            n_falto += 1
        elif es_pasado:
            n_falto += 1
        m = _h2m(ct.get("hora"))
        if m is not None:
            mins.append(m)
    n_citas = len(citas)
    if mins:
        t0, t1 = min(mins), max(mins)
        step = intervalo if intervalo and intervalo > 0 else 15
        cap = (t1 - t0) // step + 1
        cap = max(cap, n_citas)   # nunca menos que las citas reales (dobles cupos)
    else:
        cap = n_citas
    return {"cap": cap, "n_citas": n_citas, "n_at": n_at, "n_falto": n_falto}


async def refrescar_cap_cache(dias_atras: int = _DIAS_ATRAS, dias_adelante: int = _DIAS_ADELANTE,
                              solo_fecha: str | None = None, full: bool = False) -> dict:
    """Barre Medilink y actualiza `panel_cap_cache`. Secuencial + throttle.

    - `solo_fecha`: refresca solo esa fecha (todas las profs).
    - `full=True`: re-consulta TODAS las fechas de la ventana (ignora el cache previo).
    """
    from session import db
    from medilink import PROFESIONALES, citas_dia_lite

    hoy = date.today()
    hoy_iso = hoy.isoformat()
    if solo_fecha:
        fechas = [solo_fecha]
    else:
        fechas = [(hoy - timedelta(days=d)).isoformat() for d in range(dias_atras, 0, -1)]
        fechas.append(hoy_iso)
        fechas += [(hoy + timedelta(days=d)).isoformat() for d in range(1, dias_adelante + 1)]

    # ── lecturas previas (DB tomada brevemente, SIN Medilink en medio) ──
    paid_by: dict = {}          # (fecha, prof) -> set(id_paciente pagados)
    ya_cacheado: set = set()    # (prof, fecha) ya en cache
    with db() as c:
        _ensure_cap_table(c)
        fmin, fmax = min(fechas), max(fechas)
        for f, idp, idpac in c.execute(
            "SELECT fecha, id_profesional, id_paciente FROM bi_pagos_caja WHERE fecha BETWEEN ? AND ?",
            (fmin, fmax)).fetchall():
            paid_by.setdefault((f, idp), set()).add(idpac)
        for idp, f in c.execute(
            "SELECT id_profesional, fecha FROM panel_cap_cache WHERE fecha BETWEEN ? AND ?",
            (fmin, fmax)).fetchall():
            ya_cacheado.add((idp, f))

    # ── fase Medilink (async, sin DB tomada) ──
    resultados = []
    llamadas = saltadas = errores = 0
    limite_mutacion = (hoy - timedelta(days=_MUTABLE_DIAS)).isoformat()
    for fecha in fechas:
        for idp in PROFESIONALES:
            # fechas viejas ya cacheadas no cambian → saltar (salvo full)
            if not full and fecha < limite_mutacion and (idp, fecha) in ya_cacheado:
                saltadas += 1
                continue
            try:
                citas = await citas_dia_lite(idp, fecha)
                llamadas += 1
                intervalo = (PROFESIONALES.get(idp, {}) or {}).get("intervalo") or 15
                paid = paid_by.get((fecha, idp), set())
                cnt = _cap_counts(citas, paid, fecha, intervalo, hoy_iso)
                # no guardar días sin actividad alguna (ahorra filas y refetch futuro)
                if cnt["n_citas"] == 0 and cnt["cap"] == 0:
                    await asyncio.sleep(_THROTTLE)
                    continue
                resultados.append((idp, fecha, cnt))
            except Exception as e:
                errores += 1
                log.warning("cap_cache prof=%s fecha=%s error: %s", idp, fecha, e)
            await asyncio.sleep(_THROTTLE)

    # ── escritura (DB tomada al final, una transacción) ──
    ahora = datetime.now().isoformat(timespec="seconds")
    with db() as c:
        _ensure_cap_table(c)
        for idp, fecha, cnt in resultados:
            c.execute(
                """INSERT INTO panel_cap_cache (id_profesional, fecha, cap, n_citas, n_at, n_falto, updated_at)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(id_profesional, fecha) DO UPDATE SET
                     cap=excluded.cap, n_citas=excluded.n_citas, n_at=excluded.n_at,
                     n_falto=excluded.n_falto, updated_at=excluded.updated_at""",
                (idp, fecha, cnt["cap"], cnt["n_citas"], cnt["n_at"], cnt["n_falto"], ahora))

    resumen = {"llamadas": llamadas, "guardadas": len(resultados),
               "saltadas": saltadas, "errores": errores,
               "fechas": len(fechas), "profs": len(PROFESIONALES)}
    log.info("cap_cache refrescado: %s", resumen)
    return resumen
