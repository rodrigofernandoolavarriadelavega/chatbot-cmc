# -*- coding: utf-8 -*-
"""Campañas Meta — integraciones (6-oct-2026).

Cinco cruces que el panel `/alma/campanas-meta` no tenía, cada uno con su
endpoint colgado del MISMO router (`campanas_meta_routes.router`, solo
OLACORE_TOKEN):

  1. AGENDA × ANUNCIOS   `GET  /agenda`        cupos libres reales 14 días por
                                              profesional y especialidad, contra
                                              el gasto diario de Meta de esa
                                              especialidad.
  2. CREATIVOS           `GET  /creativos`     miniatura, título y texto de cada
                         `GET  /creativo/{id}`  anuncio (Marketing API, guardados
                         `GET  /anuncio/{id}`   en disco) y fatiga 7d vs 7d.
  3. TERRITORIO          `GET  /territorio`    comuna de quienes trajo cada anuncio.
  4. VELOCIDAD           `GET  /velocidad`     tiempo a la 1ª respuesta humana y
                                              conversión a cita por tramo.
  5. VALOR 90 DÍAS       `GET  /valor90`       venta acumulada a 30/60/90 días de
                                              los pacientes de cada anuncio.

GUARDRAILES
-----------
- AGENDA: el panel JAMÁS llama a Medilink. Lee `agenda_cupos_cache`, que llena
  `refrescar_cupos()` (cron 05:30 y 21:30 CLT, o POST /agenda/actualizar):
  secuencial, con pausa, en carril batch, con el MISMO cálculo de slots que usa
  el bot (`medilink._slots_para_fecha`: intervalo del bot, breaks, bloqueos y
  solape contra todas las citas). Una falla de Medilink NO se guarda como "0
  cupos" (docs/medilink_gotchas.md §6): ese día queda sin fila y el panel dice
  "sin dato". Un 429 o una plataforma inactiva cortan la corrida entera.
- CREATIVOS: las imágenes se guardan en `data/creativos` y se sirven desde el
  propio panel (nada de hotlink a la CDN de Meta, que además caduca las URL).
  Solo se descargan hosts de Meta/Instagram.
- NUNCA datos de terceros: todo lo que sale de aquí son agregados (por anuncio,
  comuna, tramo); ningún teléfono ni nombre de paciente.
- `pct_honorario` es lo que se lleva el PROFESIONAL: para el centro es
  monto × (1 − pct/100). VENTA = `bi_pagos_caja`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import median

from fastapi import HTTPException, Query, Request
from fastapi.responses import FileResponse

import campanas_meta_routes as cm
from session import db

log = logging.getLogger("campanas_meta_integraciones")
router = cm.router

# ═══════════════════════════════════════════════════════════════════════════
# 1. AGENDA × ANUNCIOS
# ═══════════════════════════════════════════════════════════════════════════

CUPOS_DIAS = 14                 # ventana de agenda que se mira
CUPOS_FRESCO_S = 8 * 3600       # más viejo que esto el panel avisa "desactualizado"
CUPOS_PAUSA_S = 3.0             # entre día/profesional que sí pega a /citas (límite Medilink ~20 req/min)
CUPOS_PAUSA_LIVIANA_S = 0.3     # días fuera de jornada: solo cache de /agendas
CUPOS_REUSO_S = 3 * 3600        # un (profesional, día) leído hace menos de esto no se vuelve a pedir
CUPOS_MIN_ENTRE_MANUALES_S = 600

UMBRAL_GASTO_DIA = 3000         # CLP/día (promedio 7 d) desde el que se dice "está gastando" en una especialidad
UMBRAL_GASTO_APAGADO = 1000     # bajo esto se considera "sin anuncio"
UMBRAL_POCOS_7D = 3             # cupos libres en 7 días: ≤ esto con gasto = "casi sin cupos"
UMBRAL_VACIOS_14D = 8           # cupos libres en 14 días desde los que vale la pena avisar "vacíos sin anuncio"
PRECIO_DIAS = 120               # ventana para el precio típico por atención
FOTO_VIEJA_DIAS = 3

_REFRESCO = {"tarea": None, "ultimo_manual": 0.0}


def _ensure_cupos(c) -> None:
    c.execute("""CREATE TABLE IF NOT EXISTS agenda_cupos_cache (
        id_profesional INTEGER NOT NULL, fecha TEXT NOT NULL, libres INTEGER NOT NULL,
        primera_hora TEXT, actualizado_ts INTEGER NOT NULL,
        PRIMARY KEY (id_profesional, fecha))""")
    c.execute("""CREATE TABLE IF NOT EXISTS agenda_cupos_estado (
        clave TEXT PRIMARY KEY, valor TEXT)""")


def _set_estado(c, clave: str, valor) -> None:
    c.execute("INSERT INTO agenda_cupos_estado (clave, valor) VALUES (?,?) "
              "ON CONFLICT(clave) DO UPDATE SET valor=excluded.valor", (clave, json.dumps(valor)))


def _get_estado(c, clave: str):
    r = c.execute("SELECT valor FROM agenda_cupos_estado WHERE clave=?", (clave,)).fetchone()
    try:
        return json.loads(r[0]) if r else None
    except Exception:
        return None


def _activo_cupos() -> bool:
    import config
    return bool(getattr(config, "AGENDA_CUPOS_ACTIVE", True))


async def refrescar_cupos(dias: int = CUPOS_DIAS, pausa: float = CUPOS_PAUSA_S,
                          reuso_s: int = CUPOS_REUSO_S, solo_prof: int | None = None) -> dict:
    """Llena `agenda_cupos_cache` con los cupos libres de cada profesional.

    Regla anti-deadlock (2026-06-10): nunca se sostiene la conexión SQLite
    mientras se llama a Medilink. Fase 1 lee lo ya cacheado; fase 2 consulta
    (async, sin DB); fase 3 escribe de una vez."""
    import httpx
    import medilink as ml
    ml.use_batch_lane()
    ahora = int(time.time())
    hoy = cm._hoy()
    cliente = ml._get_shared_client()
    ids = ml._filtrar_licencia(list(ml.PROFESIONALES))
    if solo_prof is not None:
        ids = [i for i in ids if i == solo_prof]
    fechas = [(hoy + timedelta(days=i)) for i in range(dias)]
    with db() as c:
        _ensure_cupos(c)
        frescos = {(r[0], r[1]) for r in c.execute(
            "SELECT id_profesional, fecha FROM agenda_cupos_cache WHERE actualizado_ts > ?", (ahora - reuso_s,))}
    nuevos: list[tuple] = []
    consultas = errores = sin_horario = 0
    corte = ""
    for idp in ids:
        try:
            horario = await ml._get_horario(cliente, idp)
        except Exception as e:   # noqa: BLE001 — un profesional no corta a los demás
            log.warning("agenda_cupos: horario prof=%s: %s", idp, e)
            errores += 1
            continue
        if not horario.get("horario_dia") and not horario.get("usa_agendas"):
            # Sin horario base ni /agendas: puede ser "no atiende" o una falla
            # de lectura que devolvió el respaldo vacío. No se inventa un 0.
            sin_horario += 1
            continue
        for f in fechas:
            fi = f.isoformat()
            if (idp, fi) in frescos:
                continue
            en_jornada = f.weekday() in horario.get("horario_dia", {})
            try:
                _, libres = await ml._slots_para_fecha(cliente, [idp], {idp: horario}, fi)
            except (ml.MedilinkRateLimited, ml.MedilinkInactiva) as e:
                corte = type(e).__name__
                log.warning("agenda_cupos: corto la corrida (%s) en prof=%s fecha=%s", corte, idp, fi)
                break
            except httpx.RequestError as e:
                errores += 1
                log.warning("agenda_cupos: prof=%s fecha=%s sin respuesta: %s", idp, fi, e)
                await asyncio.sleep(pausa)
                continue
            except Exception as e:   # noqa: BLE001
                errores += 1
                log.warning("agenda_cupos: prof=%s fecha=%s: %s", idp, fi, e, exc_info=True)
                continue
            consultas += 1
            primera = min((s["hora_inicio"] for s in libres), default=None)
            nuevos.append((idp, fi, len(libres), primera, int(time.time())))
            await asyncio.sleep(pausa if en_jornada else CUPOS_PAUSA_LIVIANA_S)
        if corte:
            break
    with db() as c:
        _ensure_cupos(c)
        c.executemany("INSERT INTO agenda_cupos_cache (id_profesional, fecha, libres, primera_hora, actualizado_ts) "
                      "VALUES (?,?,?,?,?) ON CONFLICT(id_profesional, fecha) DO UPDATE SET libres=excluded.libres, "
                      "primera_hora=excluded.primera_hora, actualizado_ts=excluded.actualizado_ts", nuevos)
        # fechas pasadas ya no sirven
        c.execute("DELETE FROM agenda_cupos_cache WHERE fecha < ?", (hoy.isoformat(),))
        res = {"ts": int(time.time()), "consultas": consultas, "errores": errores, "sin_horario": sin_horario,
               "profesionales": len(ids), "corte": corte or None}
        _set_estado(c, "ultima_corrida", res)
        c.commit()
    log.info("agenda_cupos: %s", res)
    return res


async def job_agenda_cupos() -> None:
    """Cron 05:30 y 21:30 CLT. Flag AGENDA_CUPOS_ACTIVE."""
    if not _activo_cupos():
        log.info("agenda_cupos: apagado (AGENDA_CUPOS_ACTIVE=false)")
        return
    try:
        await refrescar_cupos()
    except Exception as e:   # noqa: BLE001
        log.error("agenda_cupos: la corrida falló: %s", e, exc_info=True)


def _grupo_anuncio(c, mapa: dict, ad_id: str, memo: dict) -> str | None:
    """Especialidad (grupo) del anuncio con la MISMA regla del panel: nombre,
    luego texto del referral, luego nombre de la campaña."""
    if ad_id in memo:
        return memo[ad_id]
    info = cm._info_ad(mapa, ad_id)
    ref = c.execute("SELECT headline, body FROM meta_referrals WHERE source_id=? ORDER BY ts DESC LIMIT 1",
                    (ad_id,)).fetchone()
    nombre = info["anuncio"] if ad_id in mapa else (ref["headline"] if ref else "")
    g = (cm._grupo(nombre) or cm._grupo((ref["body"] or "")[:400] if ref else "")
         or cm._grupo(info["campana"]))
    memo[ad_id] = g
    return g


def _gasto_por_grupo(c, mapa: dict, hoy: date) -> dict:
    """Gasto diario promedio (7 días) de Meta por especialidad del anuncio."""
    cm._ensure_insights(c)
    ult = c.execute("SELECT MAX(fecha) FROM meta_insights_diario WHERE desglose='total'").fetchone()[0]
    if not ult:
        return {"grupos": {}, "ultima_foto": None, "sin_grupo": 0, "foto_vieja": True, "desde": None, "hasta": None}
    hasta = min(date.fromisoformat(ult), hoy - timedelta(days=1))
    desde = hasta - timedelta(days=6)
    activos_desde = (hasta - timedelta(days=1)).isoformat()
    memo: dict = {}
    grupos: dict[str, dict] = {}
    sin_grupo = 0.0
    for r in c.execute("SELECT ad_id, SUM(spend), SUM(CASE WHEN fecha >= ? THEN spend ELSE 0 END) "
                       "FROM meta_insights_diario WHERE desglose='total' AND fecha >= ? AND fecha <= ? "
                       "GROUP BY ad_id HAVING SUM(spend) > 0",
                       (activos_desde, desde.isoformat(), hasta.isoformat())):
        ad_id, gasto, reciente = r[0], r[1] or 0, r[2] or 0
        g = _grupo_anuncio(c, mapa, ad_id, memo)
        if not g:
            sin_grupo += gasto
            continue
        o = grupos.setdefault(g, {"gasto_7d": 0.0, "anuncios": []})
        o["gasto_7d"] += gasto
        o["anuncios"].append({"ad_id": ad_id, "anuncio": cm._info_ad(mapa, ad_id)["anuncio"],
                              "gasto_dia": round(gasto / 7), "activo": reciente > 0})
    for o in grupos.values():
        o["gasto_dia"] = round(o["gasto_7d"] / 7)
        o["anuncios"].sort(key=lambda a: -a["gasto_dia"])
        o["activos"] = sum(1 for a in o["anuncios"] if a["activo"])
    return {"grupos": grupos, "ultima_foto": ult, "sin_grupo": round(sin_grupo / 7),
            "foto_vieja": (hoy - date.fromisoformat(ult)).days > FOTO_VIEJA_DIAS,
            "desde": desde.isoformat(), "hasta": hasta.isoformat()}


def _precios_tipicos(c, hoy: date) -> dict[int, int]:
    """Mediana de lo cobrado por atención (paciente × día × profesional) en
    los últimos PRECIO_DIAS días, por profesional. Es lo que un cupo vacío deja
    de vender; la mediana evita que un tratamiento caro lo infle."""
    desde = (hoy - timedelta(days=PRECIO_DIAS)).isoformat()
    por: dict[int, list[int]] = defaultdict(list)
    try:
        for r in c.execute("SELECT id_profesional, SUM(monto) FROM bi_pagos_caja WHERE fecha >= ? AND monto > 0 "
                           "GROUP BY id_profesional, id_paciente, substr(fecha,1,10)", (desde,)):
            por[r[0]].append(int(r[1] or 0))
    except Exception as e:   # entorno sin BI
        log.warning("agenda: sin precios de caja: %s", e)
    return {pid: int(median(v)) for pid, v in por.items() if len(v) >= 5}


def agenda_data(hoy: date | None = None) -> dict:
    hoy = hoy or cm._hoy()
    import medilink as ml
    with db() as c:
        _ensure_cupos(c)
        cm._ensure_insights(c)
        filas = c.execute("SELECT id_profesional, fecha, libres, primera_hora, actualizado_ts FROM agenda_cupos_cache "
                          "WHERE fecha >= ? AND fecha < ?",
                          (hoy.isoformat(), (hoy + timedelta(days=CUPOS_DIAS)).isoformat())).fetchall()
        corrida = _get_estado(c, "ultima_corrida")
        mapa = cm._mapa_anuncios(c)
        nombres = cm._nombres_profesionales(c)
        pct = cm._pct_honorarios(c)
        precios = _precios_tipicos(c, hoy)
        gasto = _gasto_por_grupo(c, mapa, hoy)
    if not filas:
        return {"sin_datos": True, "activo": _activo_cupos(), "corrida": corrida, "hoy": hoy.isoformat(),
                "dias": CUPOS_DIAS, "profesionales": [], "grupos": [], "senales": [],
                "gasto": {"ultima_foto": gasto["ultima_foto"], "foto_vieja": gasto["foto_vieja"]}}
    dias = [(hoy + timedelta(days=i)).isoformat() for i in range(CUPOS_DIAS)]
    por_prof: dict[int, dict] = defaultdict(dict)
    ts_max = 0
    for r in filas:
        por_prof[r[0]][r[1]] = (r[2], r[3])
        ts_max = max(ts_max, r[4] or 0)
    profs = []
    for idp, mapa_d in por_prof.items():
        esp = ml.PROFESIONALES.get(idp, {}).get("especialidad", "")
        grupo = cm._grupo_prof(idp) or esp or "Otra"
        serie = [mapa_d[f][0] if f in mapa_d else None for f in dias]
        l7 = sum(x for x in serie[:7] if x)
        l14 = sum(x for x in serie if x)
        prox = next((f for f in dias if f in mapa_d and mapa_d[f][0] > 0), None)
        precio = precios.get(idp)
        margen = 100 - (pct.get(idp) or cm.PCT_HONORARIO_DEFAULT)
        profs.append({"id": idp, "nombre": nombres.get(idp) or f"Profesional {idp}", "especialidad": esp, "grupo": grupo,
                      "libres_7d": l7, "libres_14d": l14, "por_dia": serie,
                      "dias_leidos": sum(1 for x in serie if x is not None),
                      "proxima": prox, "proxima_hora": mapa_d[prox][1] if prox else None,
                      "precio": precio, "margen_pct": margen,
                      "valor_14d": round(l14 * precio * margen / 100) if precio else None})
    profs.sort(key=lambda p: (p["grupo"], -p["libres_14d"]))
    grupos_p: dict[str, list] = defaultdict(list)
    for p in profs:
        grupos_p[p["grupo"]].append(p)
    out_g, senales = [], []
    for g in sorted(set(grupos_p) | set(gasto["grupos"])):
        ps = grupos_p.get(g, [])
        gg = gasto["grupos"].get(g, {"gasto_dia": 0, "activos": 0, "anuncios": []})
        l7 = sum(p["libres_7d"] for p in ps)
        l14 = sum(p["libres_14d"] for p in ps)
        valor = sum(p["valor_14d"] or 0 for p in ps)
        leidos = sum(p["dias_leidos"] for p in ps)
        gasta = gg["gasto_dia"] >= UMBRAL_GASTO_DIA
        senal = "ok" if (ps or gasta) else "sin_datos"
        if not ps and gasta:
            senal = "sin_agenda"      # gasta en una especialidad sin profesional con agenda leída
        elif gasta and l7 <= UMBRAL_POCOS_7D:
            senal = "sin_cupos" if l7 == 0 else "pocos_cupos"
        elif not gasta and gg["gasto_dia"] < UMBRAL_GASTO_APAGADO and l14 >= UMBRAL_VACIOS_14D and valor > 0:
            senal = "cupos_sin_anuncio"
        o = {"grupo": g, "libres_7d": l7, "libres_14d": l14, "valor_14d": round(valor),
             "gasto_dia": gg["gasto_dia"], "gasto_14d": gg["gasto_dia"] * CUPOS_DIAS,
             "anuncios_activos": gg["activos"], "anuncios": gg["anuncios"][:3],
             "profesionales": len(ps), "dias_leidos": leidos, "senal": senal}
        out_g.append(o)
        if senal in ("sin_cupos", "pocos_cupos", "cupos_sin_anuncio"):
            senales.append(o)
    orden = {"sin_cupos": 0, "pocos_cupos": 1, "cupos_sin_anuncio": 2, "sin_agenda": 3, "ok": 4, "sin_datos": 5}
    out_g.sort(key=lambda x: (orden[x["senal"]], -x["gasto_dia"], -x["valor_14d"]))
    senales.sort(key=lambda x: (orden[x["senal"]], -x["gasto_dia"], -x["valor_14d"]))
    ahora = int(time.time())
    return {"sin_datos": False, "activo": _activo_cupos(), "hoy": hoy.isoformat(), "dias": CUPOS_DIAS,
            "fechas": dias, "actualizado_ts": ts_max, "edad_min": round((ahora - ts_max) / 60) if ts_max else None,
            "desactualizado": (ahora - ts_max) > CUPOS_FRESCO_S, "corrida": corrida,
            "grupos": out_g, "senales": senales, "profesionales": profs,
            "gasto": {"ultima_foto": gasto["ultima_foto"], "foto_vieja": gasto["foto_vieja"],
                      "desde": gasto["desde"], "hasta": gasto["hasta"], "sin_grupo_dia": gasto["sin_grupo"]},
            "umbrales": {"gasto_dia": UMBRAL_GASTO_DIA, "pocos_7d": UMBRAL_POCOS_7D, "vacios_14d": UMBRAL_VACIOS_14D}}


def alertas_agenda(hoy: date | None = None) -> list[tuple[str, str]]:
    """[(clave_dedup, texto)] para meta_alertas.evaluar_alertas. Solo con el
    cache fresco (≤ 36 h): con datos viejos una alerta sería ruido."""
    d = agenda_data(hoy)
    if d["sin_datos"] or (time.time() - (d.get("actualizado_ts") or 0)) > 36 * 3600 or d["gasto"].get("foto_vieja"):
        return []
    from meta_alertas import _clp
    out: list[tuple[str, str]] = []
    for g in d["senales"]:
        if g["senal"] in ("sin_cupos", "pocos_cupos"):
            cupos = ("no tiene ningún cupo libre" if g["libres_7d"] == 0
                     else f"solo tiene {g['libres_7d']} cupo(s) libre(s)")
            out.append((f"agenda_sin_cupos:{g['grupo']}",
                        f"Meta gasta {_clp(g['gasto_dia'])} al día en anuncios de {g['grupo']} y la agenda {cupos} "
                        f"en los próximos 7 días. Conviene bajar o pausar esos anuncios hasta que se abran horas."))
        elif g["senal"] == "cupos_sin_anuncio":
            out.append((f"agenda_vacios:{g['grupo']}",
                        f"{g['grupo']} tiene {g['libres_14d']} cupos libres en los próximos 14 días (hasta "
                        f"{_clp(g['valor_14d'])} para el centro si se llenaran) y no hay anuncios con gasto de esa "
                        f"especialidad. Puede valer la pena activar uno."))
    return out


# ═══════════════════════════════════════════════════════════════════════════
# 2. CREATIVOS
# ═══════════════════════════════════════════════════════════════════════════

_MEDIA_OK = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}
_HOSTS_OK = (".fbcdn.net", ".facebook.com", ".cdninstagram.com", ".fbsbx.com", ".instagram.com")
MAX_IMAGEN = 3 * 1024 * 1024
IMAGEN_VIGENCIA_DIAS = 30
_RE_AD = re.compile(r"^[0-9A-Za-z_:\-]{1,60}$")


def _dir_creativos() -> Path:
    import session as _s
    d = Path(_s.DB_PATH).parent / "creativos"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _ensure_creativos(c) -> None:
    c.execute("""CREATE TABLE IF NOT EXISTS meta_creativos (
        ad_id TEXT PRIMARY KEY, creative_id TEXT, titulo TEXT, texto TEXT, cta TEXT, tipo TEXT,
        thumb_url TEXT, imagen_url TEXT, archivo TEXT, imagen_ts INTEGER, actualizado_ts INTEGER, error TEXT)""")


def _url_segura(url: str | None) -> bool:
    """Host permitido según el MISMO parser que hace la descarga (httpx), para que
    no haya diferencias de lectura (`\\`, `@`, userinfo, puertos raros)."""
    if not url or any(ch in url for ch in "\\@ \t\r\n"):
        return False
    try:
        import httpx
        u = httpx.URL(url)
    except Exception:   # noqa: BLE001
        return False
    host = (u.host or "").lower().rstrip(".")
    return (u.scheme == "https" and not u.userinfo and u.port in (None, 443)
            and any(host.endswith(h) for h in _HOSTS_OK))


def parse_creativo(ad: dict) -> dict:
    """Aplana la respuesta de la Marketing API (`ads?fields=creative{...}`)."""
    cr = ad.get("creative") or {}
    oss = cr.get("object_story_spec") or {}
    ld, vd = oss.get("link_data") or {}, oss.get("video_data") or {}
    pd = oss.get("photo_data") or {}
    titulo = cr.get("title") or ld.get("name") or vd.get("title") or ""
    texto = cr.get("body") or ld.get("message") or vd.get("message") or pd.get("caption") or ""
    cta = cr.get("call_to_action_type") or (ld.get("call_to_action") or {}).get("type") \
        or (vd.get("call_to_action") or {}).get("type") or ""
    imagen = cr.get("image_url") or ld.get("picture") or vd.get("image_url") or pd.get("url") or ""
    tipo = "video" if vd else ("imagen" if (imagen or ld or pd) else (cr.get("object_type") or "").lower())
    return {"creative_id": str(cr.get("id") or ""), "titulo": titulo.strip(), "texto": texto.strip(), "cta": cta,
            "tipo": tipo, "thumb_url": cr.get("thumbnail_url") or "", "imagen_url": imagen}


_AD_FIELDS_BASE = "id,name,effective_status,creative{id,title,body,call_to_action_type,thumbnail_url,image_url,object_type,object_story_spec}"
_AD_FIELDS_GRANDE = ("id,name,effective_status,creative.thumbnail_width(480).thumbnail_height(480)"
                     "{id,title,body,call_to_action_type,thumbnail_url,image_url,object_type,object_story_spec}")


def _bajar_anuncios(cliente, acct: str, token: str) -> list[dict]:
    """Todos los anuncios de la cuenta con su creativo. Pide miniatura de 480 px
    y, si Meta no acepta ese modificador, reintenta con los campos planos."""
    headers = {"Authorization": f"Bearer {token}"}
    for campos in (_AD_FIELDS_GRANDE, _AD_FIELDS_BASE):
        url, params, out, ok = f"https://graph.facebook.com/v22.0/{acct}/ads", {"fields": campos, "limit": 100}, [], True
        for _ in range(30):
            r = cliente.get(url, params=params, headers=headers, timeout=30)
            if r.status_code != 200:
                ok = False
                log.warning("creativos: Meta %s con campos %s: %s", r.status_code, campos[:30], r.text[:160])
                break
            b = r.json()
            out.extend(b.get("data", []))
            url, params = (b.get("paging") or {}).get("next"), None
            if not url:
                break
        if ok:
            return out
    raise RuntimeError("Meta no entregó los creativos")


def _guardar_imagen(cliente, ad_id: str, url: str) -> str | None:
    if not _url_segura(url):
        log.warning("creativos: URL de imagen no permitida para %s", ad_id)
        return None
    try:
        r = cliente.get(url, timeout=20, follow_redirects=False)
    except Exception as e:   # noqa: BLE001
        log.warning("creativos: no se pudo bajar la imagen de %s: %s", ad_id, e)
        return None
    ct = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
    if r.status_code != 200 or ct not in _MEDIA_OK or not r.content or len(r.content) > MAX_IMAGEN:
        return None
    nombre = re.sub(r"[^0-9A-Za-z_\-]", "_", ad_id) + _MEDIA_OK[ct]
    (_dir_creativos() / nombre).write_bytes(r.content)
    return nombre


def refrescar_creativos() -> dict:
    """Baja texto e imagen de cada anuncio con gasto o llegadas conocidas.
    Síncrono (se llama en un hilo). Sin META_ACCESS_TOKEN no hace nada."""
    import httpx
    import meta_insights_snapshot as mis
    token, acct = mis._cfg()
    if not token:
        return {"ok": False, "motivo": "sin META_ACCESS_TOKEN"}
    with db() as c:
        cm._ensure_insights(c)
        _ensure_creativos(c)
        conocidos = {r[0] for r in c.execute("SELECT DISTINCT ad_id FROM meta_insights_diario WHERE ad_id != ''")}
        conocidos |= {r[0] for r in c.execute("SELECT DISTINCT source_id FROM meta_referrals WHERE source_id != ''")}
        previos = {r["ad_id"]: dict(r) for r in c.execute("SELECT * FROM meta_creativos")}
    ahora = int(time.time())
    n_ok = n_img = n_err = 0
    with httpx.Client() as cl:
        try:
            ads = _bajar_anuncios(cl, acct, token)
        except Exception as e:   # noqa: BLE001
            log.error("creativos: no se pudo leer la cuenta: %s", e)
            return {"ok": False, "motivo": str(e)}
        filas = []
        for ad in ads:
            ad_id = str(ad.get("id") or "")
            if ad_id not in conocidos:
                continue
            f = parse_creativo(ad)
            prev = previos.get(ad_id) or {}
            archivo, imagen_ts = prev.get("archivo"), prev.get("imagen_ts") or 0
            vencida = (ahora - imagen_ts) > IMAGEN_VIGENCIA_DIAS * 86400
            falta = not archivo or not (_dir_creativos() / archivo).exists()
            if falta or vencida:
                for u in (f["imagen_url"], f["thumb_url"]):   # la grande primero; si falla, la miniatura
                    if u and (nuevo := _guardar_imagen(cl, ad_id, u)):
                        archivo, imagen_ts = nuevo, ahora
                        n_img += 1
                        break
            err = "" if (archivo or f["texto"] or f["titulo"]) else "sin creativo legible"
            n_err += 1 if err else 0
            n_ok += 1
            filas.append((ad_id, f["creative_id"], f["titulo"], f["texto"], f["cta"], f["tipo"], f["thumb_url"],
                          f["imagen_url"], archivo, imagen_ts, ahora, err))
    with db() as c:
        _ensure_creativos(c)
        c.executemany("INSERT INTO meta_creativos (ad_id, creative_id, titulo, texto, cta, tipo, thumb_url, imagen_url, "
                      "archivo, imagen_ts, actualizado_ts, error) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
                      "ON CONFLICT(ad_id) DO UPDATE SET creative_id=excluded.creative_id, titulo=excluded.titulo, "
                      "texto=excluded.texto, cta=excluded.cta, tipo=excluded.tipo, thumb_url=excluded.thumb_url, "
                      "imagen_url=excluded.imagen_url, archivo=excluded.archivo, imagen_ts=excluded.imagen_ts, "
                      "actualizado_ts=excluded.actualizado_ts, error=excluded.error", filas)
        c.commit()
    res = {"ok": True, "anuncios": n_ok, "imagenes_nuevas": n_img, "sin_creativo": n_err}
    log.info("creativos: %s", res)
    return res


async def job_meta_creativos() -> None:
    """Cron 06:50 CLT (después de la foto diaria de insights). Flag META_CREATIVOS_ACTIVE."""
    import config
    if not getattr(config, "META_CREATIVOS_ACTIVE", True):
        log.info("meta_creativos: apagado (META_CREATIVOS_ACTIVE=false)")
        return
    try:
        await asyncio.to_thread(refrescar_creativos)
    except Exception as e:   # noqa: BLE001
        log.error("meta_creativos: falló: %s", e, exc_info=True)


# ── Fatiga: frecuencia y CTR, 7 días contra los 7 anteriores ────────────────
FATIGA_MIN_IMPRESIONES = 1500     # por ventana; con menos, "sin datos suficientes"
FATIGA_FREC_ALTA = 3.0            # frecuencia de 7 días desde la que se vigila
FATIGA_CAIDA_CTR = 0.20           # CTR cae 20 % o más contra los 7 días previos


def fatiga_ad(act: dict, prev: dict, umbral_saturado: float | None = None) -> dict:
    """Función pura. act/prev: {impresiones, alcance, clics} sumados 7 días.
    La frecuencia es impresiones ÷ suma del alcance diario: un PISO de la real
    (el mismo criterio que el panel). Etiquetas: cansado / vigilar / sano /
    sin_datos."""
    umbral_saturado = umbral_saturado or cm.UMBRAL_FRECUENCIA

    def _m(v):
        imp = v.get("impresiones") or 0
        alc = v.get("alcance") or 0
        return {"impresiones": imp, "frecuencia": round(imp / alc, 2) if alc else None,
                "ctr": round(100 * (v.get("clics") or 0) / imp, 2) if imp else None}
    a, p = _m(act), _m(prev)
    base = {"actual": a, "previo": p, "ctr_cambio_pct": None, "etiqueta": "sin_datos", "sugerencia": ""}
    if a["impresiones"] < FATIGA_MIN_IMPRESIONES:
        base["sugerencia"] = "Pocas impresiones en los últimos 7 días: todavía no hay base para medir cansancio."
        return base
    if p["ctr"] and a["ctr"] is not None:
        base["ctr_cambio_pct"] = round(100 * (a["ctr"] - p["ctr"]) / p["ctr"])
    cae = (base["ctr_cambio_pct"] is not None and p["impresiones"] >= FATIGA_MIN_IMPRESIONES
           and base["ctr_cambio_pct"] <= -100 * FATIGA_CAIDA_CTR)
    f = a["frecuencia"] or 0
    if f > umbral_saturado or (f >= FATIGA_FREC_ALTA and cae):
        base["etiqueta"] = "cansado"
        base["sugerencia"] = ("La misma gente lo ve una y otra vez y cada vez responde menos. Cambie la imagen o el "
                              "primer párrafo (mismo mensaje, otra cara), o amplíe la audiencia, antes de subir presupuesto.")
    elif f >= FATIGA_FREC_ALTA or cae:
        base["etiqueta"] = "vigilar"
        base["sugerencia"] = ("Va con una señal de desgaste (frecuencia alta o CTR a la baja). Prepare un creativo "
                              "de reemplazo y no suba presupuesto todavía.")
    else:
        base["etiqueta"] = "sano"
        base["sugerencia"] = "Sin señales de desgaste: la gente que lo ve sigue respondiendo."
    return base


def fatiga_todos(c, hasta: date | None = None) -> dict[str, dict]:
    cm._ensure_insights(c)
    ult = c.execute("SELECT MAX(fecha) FROM meta_insights_diario WHERE desglose='total'").fetchone()[0]
    if not ult:
        return {}
    h = min(date.fromisoformat(ult), hasta or cm._hoy() - timedelta(days=1))
    a0, p1, p0 = h - timedelta(days=6), h - timedelta(days=7), h - timedelta(days=13)
    acc: dict[str, dict] = defaultdict(lambda: {"act": {"impresiones": 0, "alcance": 0, "clics": 0},
                                                "prev": {"impresiones": 0, "alcance": 0, "clics": 0}})
    for r in c.execute("SELECT ad_id, fecha, impressions, reach, clicks FROM meta_insights_diario "
                       "WHERE desglose='total' AND fecha >= ? AND fecha <= ?", (p0.isoformat(), h.isoformat())):
        k = "act" if r[1] >= a0.isoformat() else "prev"
        o = acc[r[0]][k]
        o["impresiones"] += r[2] or 0
        o["alcance"] += r[3] or 0
        o["clics"] += r[4] or 0
    out = {}
    for ad_id, v in acc.items():
        out[ad_id] = {**fatiga_ad(v["act"], v["prev"]), "ventana": [a0.isoformat(), h.isoformat()]}
    return out


def creativos_data() -> dict:
    with db() as c:
        _ensure_creativos(c)
        cr = {r["ad_id"]: {"titulo": r["titulo"] or "", "texto": r["texto"] or "", "cta": r["cta"] or "",
                           "tipo": r["tipo"] or "", "imagen": bool(r["archivo"]), "actualizado_ts": r["actualizado_ts"]}
              for r in c.execute("SELECT * FROM meta_creativos")}
        fat = fatiga_todos(c)
    ids = set(cr) | set(fat)
    import config
    return {"anuncios": {i: {**cr.get(i, {"titulo": "", "texto": "", "cta": "", "tipo": "", "imagen": False}),
                             "fatiga": fat.get(i)} for i in ids},
            "con_creativos": len(cr), "activo": bool(getattr(config, "META_CREATIVOS_ACTIVE", True)),
            "resumen_fatiga": {e: sum(1 for v in fat.values() if v["etiqueta"] == e)
                               for e in ("cansado", "vigilar", "sano", "sin_datos")}}


# ═══════════════════════════════════════════════════════════════════════════
# Contexto común de personas del rango (clics, citas) — igual que panel_data
# ═══════════════════════════════════════════════════════════════════════════

def _contexto(c, d: date, h: date, canal: str, campana: str | None, plat: str | None,
              anuncio: str | None = None) -> dict:
    e0, e1 = cm._epoch_ini(d), cm._epoch_fin(h)
    mapa = cm._mapa_anuncios(c)
    clics: dict[str, tuple[int, str]] = {}
    origen_k: dict[str, str] = {}
    for r in cm._llegadas(c, canal, e0, e1, primera_web=True):
        info = cm._info(mapa, r["source_id"] or "", r["headline"] or "")
        if not cm._pasa_filtros(info, r["plataforma"], campana, plat):
            continue
        k = cm._clave(r["phone"])
        origen_k.setdefault(k, r["origen"])
        if k not in clics or r["ts"] < clics[k][0]:
            clics[k] = (r["ts"], info["ad_id"])
    citas: list[dict] = []
    if canal in ("meta", "todos") and not cm._es_web_camp(campana):
        citas = cm._citas_atribuidas(c, e0, e1, mapa, campana, plat)
        if canal == "todos":
            citas = [ci for ci in citas if origen_k.get(ci["clave"]) != "web"]
    if canal in ("web", "todos"):
        citas += cm._citas_web(c, {k: v for k, v in clics.items() if v[1].startswith("web:")}, e1)
    if anuncio:
        clics = {k: v for k, v in clics.items() if v[1] == anuncio}
        citas = [ci for ci in citas if ci["ad_id"] == anuncio]
    return {"mapa": mapa, "clics": clics, "citas": citas, "e0": e0, "e1": e1, "origen_k": origen_k}


def _params(desde, hasta, canal, campana, plataforma):
    d, h = cm._rango(desde, hasta)
    canal = cm._canal(canal)
    plat = plataforma if plataforma in cm._PLATS else None
    if canal == "web":
        plat = None
    return d, h, canal, plat


# ═══════════════════════════════════════════════════════════════════════════
# 3. TERRITORIO POR ANUNCIO
# ═══════════════════════════════════════════════════════════════════════════

OTRA_COMUNA = "Otra comuna"
SIN_DATO = "Sin dato"
# Ubicación aproximada (lon, lat) para el mapa de burbujas sin librerías ni CDN.
COORD_COMUNAS = {"Arauco": (-73.32, -37.25), "Curanilahue": (-73.35, -37.48), "Los Álamos": (-73.47, -37.63),
                 "Lebu": (-73.65, -37.61), "Cañete": (-73.40, -37.80), "Contulmo": (-73.23, -38.01),
                 "Tirúa": (-73.50, -38.34)}


def _heatmap_ro():
    import session as _s
    ruta = Path(_s.DB_PATH).parent / "heatmap_cache.db"
    if not ruta.exists():
        return None
    try:
        return sqlite3.connect(f"file:{ruta}?mode=ro", uri=True)
    except Exception as e:   # noqa: BLE001
        log.warning("territorio: heatmap_cache no abre: %s", e)
        return None


def _norm_comuna(valor: str | None) -> str | None:
    """Nombre de comuna a su forma de mostrar. Desconocida pero escrita → 'Otra comuna'."""
    import localidades_arauco as la
    t = (valor or "").strip()
    if not t or la.normalizar(t) in ("0", "-", "SIN DATO", "S/I", "NULL", "NONE"):
        return None
    r = la.resolver(None, t, None)
    if r.get("comuna"):
        return la.COMUNA_DISPLAY.get(r["comuna"], r["comuna"])
    return OTRA_COMUNA


def _comuna_ficha(direccion, comuna, ciudad) -> str | None:
    import localidades_arauco as la
    r = la.resolver(direccion, comuna, ciudad)
    if r.get("comuna"):
        return la.COMUNA_DISPLAY.get(r["comuna"], r["comuna"])
    return _norm_comuna(comuna) or _norm_comuna(ciudad)


def comunas_de_pacientes(pids: set[int]) -> dict[int, str]:
    """pid → comuna según su ficha de Medilink (la dirección manda sobre el campo)."""
    out: dict[int, str] = {}
    h = _heatmap_ro()
    if not h or not pids:
        return out
    try:
        ids = list(pids)
        for i in range(0, len(ids), 500):
            lote = ids[i:i + 500]
            for r in h.execute("SELECT id, direccion, comuna, ciudad FROM pacientes_heatmap WHERE id IN (%s)"
                               % ",".join("?" * len(lote)), lote):
                cm_ = _comuna_ficha(r[1], r[2], r[3])
                if cm_:
                    out[r[0]] = cm_
    except Exception as e:   # noqa: BLE001
        log.warning("territorio: fichas sin comuna: %s", e)
    finally:
        h.close()
    return out


def territorio_data(desde=None, hasta=None, campana=None, plataforma=None, canal=None, anuncio=None) -> dict:
    d, h, canal, plat = _params(desde, hasta, canal, campana, plataforma)
    with db() as c:
        ctx = _contexto(c, d, h, canal, campana, plat, anuncio)
        clics, citas = ctx["clics"], ctx["citas"]
        detalle: dict[int, dict] = {}
        cm._venta_por_anuncio(c, citas, clics, detalle)
        por_tel = cm._pacientes_por_telefono(c, set(clics))
        perfil = {}
        try:
            for r in c.execute("SELECT phone, comuna FROM contact_profiles WHERE comuna IS NOT NULL AND comuna != ''"):
                k = cm._clave(r[0])
                if k in clics:
                    perfil[k] = _norm_comuna(r[1])
        except Exception:
            pass
    pids = {p for ps in por_tel.values() for p in ps} | {p for p in detalle} \
        | {int(ci["id_paciente_medilink"]) for ci in citas if ci.get("id_paciente_medilink")}
    ficha = comunas_de_pacientes(pids)
    # comuna de la persona: lo que dijo al bot > la ficha de sus pacientes (si todas coinciden)
    por_k: dict[str, tuple[str, str]] = {}
    for k in clics:
        if perfil.get(k):
            por_k[k] = (perfil[k], "conversación")
            continue
        cands = {ficha[p] for p in por_tel.get(k, ()) if p in ficha}
        if len(cands) == 1:
            por_k[k] = (next(iter(cands)), "ficha")
    pid_k: dict[int, str] = {}
    for k, ps in por_tel.items():
        for p in ps:
            pid_k.setdefault(p, k)
    for ci in citas:
        if ci.get("id_paciente_medilink"):
            pid_k[int(ci["id_paciente_medilink"])] = ci["clave"]

    def _com(k: str | None) -> str:
        return por_k[k][0] if k in por_k else SIN_DATO

    filas: dict[str, dict] = {}
    por_ad: dict[str, dict[str, dict]] = defaultdict(dict)

    def _fila(dic, com):
        return dic.setdefault(com, {"comuna": com, "personas": 0, "citas": 0, "pagaron": 0, "venta": 0, "centro": 0.0,
                                    "fuentes": defaultdict(int)})
    for k, (_ts, ad_id) in clics.items():
        com = _com(k)
        for dic in (filas, por_ad[ad_id]):
            o = _fila(dic, com)
            o["personas"] += 1
            if k in por_k:
                o["fuentes"][por_k[k][1]] += 1
    for ci in citas:
        com = _com(ci["clave"])
        for dic in (filas, por_ad[ci["ad_id"]]):
            _fila(dic, com)["citas"] += 1
    for pid, det in detalle.items():
        k = pid_k.get(pid)
        com = ficha.get(pid) or _com(k)
        for dic in (filas, por_ad[det["ad_id"]]):
            o = _fila(dic, com)
            o["pagaron"] += 1
            o["venta"] += det["venta"]
            o["centro"] += det["centro"]
    total_p = sum(f["personas"] for f in filas.values())
    total_v = sum(f["venta"] for f in filas.values())
    con = sum(f["personas"] for k, f in filas.items() if k != SIN_DATO)

    def _cerrar(f):
        return {"comuna": f["comuna"], "personas": f["personas"], "citas": f["citas"], "pagaron": f["pagaron"],
                "venta": round(f["venta"]), "centro": round(f["centro"]),
                "pct_personas": round(100 * f["personas"] / total_p) if total_p else 0,
                "pct_venta": round(100 * f["venta"] / total_v) if total_v else 0,
                "fuentes": dict(f["fuentes"])}
    comunas = sorted((_cerrar(f) for f in filas.values()),
                     key=lambda x: (x["comuna"] == SIN_DATO, -x["venta"], -x["personas"]))
    anuncios = {ad: sorted((_cerrar(f) for f in dic.values()),
                           key=lambda x: (x["comuna"] == SIN_DATO, -x["venta"], -x["personas"]))[:5]
                for ad, dic in por_ad.items()}
    mapa = [{"comuna": x["comuna"], "lon": COORD_COMUNAS[x["comuna"]][0], "lat": COORD_COMUNAS[x["comuna"]][1],
             "personas": x["personas"], "venta": x["venta"]} for x in comunas if x["comuna"] in COORD_COMUNAS]
    return {"rango": {"desde": d.isoformat(), "hasta": h.isoformat()}, "canal": canal,
            "cobertura": {"personas": total_p, "con_comuna": con, "pct": round(100 * con / total_p) if total_p else None},
            "comunas": comunas, "por_anuncio": anuncios, "mapa": mapa}


# ═══════════════════════════════════════════════════════════════════════════
# 4. VELOCIDAD DE RESPUESTA
# ═══════════════════════════════════════════════════════════════════════════

VELOZ_RAPIDOS = ("lt5", "5_30")
VELOZ_LENTOS = ("gt2h", "sin_respuesta")
VELOZ_MUESTRA_MIN = 5


def resumen_velocidad(filas: list[dict]) -> dict:
    """Función pura. filas: dicts de `_rapidez` (necesito, proactiva, minutos,
    respondida, agendo). Curva por tramos finos y comparación rápidos/lentos."""
    import recepcion_tiempos as rt
    nec = [f for f in filas if f.get("necesito")]
    solo_bot = [f for f in filas if not f.get("necesito") and not f.get("proactiva")]
    curva = rt.curva_fina(nec)
    por = {x["tramo"]: x for x in curva}

    def _suma(ids):
        n = sum(por[i]["personas"] for i in ids)
        a = sum(por[i]["agendaron"] for i in ids)
        return n, a, (round(100 * a / n) if n else None)
    nr, ar, pr = _suma(VELOZ_RAPIDOS)
    nl, al, pl = _suma(VELOZ_LENTOS)
    perdidas = None
    if pr is not None and pl is not None and nr >= VELOZ_MUESTRA_MIN and nl >= VELOZ_MUESTRA_MIN and pr > pl:
        perdidas = round(nl * (pr - pl) / 100)
    resp = [f["minutos"] for f in nec if f.get("respondida") and f.get("minutos") is not None]
    return {"necesitaron": len(nec), "mediana_min": rt.mediana(resp), "curva": curva,
            "rapidos": {"personas": nr, "agendaron": ar, "pct": pr},
            "lentos": {"personas": nl, "agendaron": al, "pct": pl},
            "citas_perdidas_estimadas": perdidas,
            "sin_respuesta": por["sin_respuesta"]["personas"],
            "solo_bot": {"personas": len(solo_bot), "agendaron": sum(1 for f in solo_bot if f.get("agendo")),
                         "pct": round(100 * sum(1 for f in solo_bot if f.get("agendo")) / len(solo_bot)) if solo_bot else None},
            "proactivas": sum(1 for f in filas if f.get("proactiva"))}


def velocidad_data(desde=None, hasta=None, campana=None, plataforma=None, canal=None, anuncio=None) -> dict:
    d, h, canal, plat = _params(desde, hasta, canal, campana, plataforma)
    with db() as c:
        ctx = _contexto(c, d, h, canal, campana, plat, anuncio)
        rap = cm._rapidez(c, ctx["clics"], ctx["e1"])
        mapa = ctx["mapa"]
    por_ad: dict[str, list[dict]] = defaultdict(list)
    for v in rap.values():
        por_ad[v["ad_id"]].append(v)
    anuncios = []
    for ad_id, filas in por_ad.items():
        r = resumen_velocidad(filas)
        info = cm._info(mapa, ad_id)
        anuncios.append({"ad_id": ad_id, "anuncio": info["anuncio"], "campana": info["campana"],
                         "personas": len(filas), **{k: r[k] for k in (
                             "necesitaron", "mediana_min", "sin_respuesta", "rapidos", "lentos", "citas_perdidas_estimadas")},
                         "solo_bot": r["solo_bot"]})
    anuncios.sort(key=lambda x: (-x["necesitaron"], x["anuncio"]))
    return {"rango": {"desde": d.isoformat(), "hasta": h.isoformat()}, "canal": canal,
            "total": resumen_velocidad(list(rap.values())), "anuncios": anuncios, "muestra_min": VELOZ_MUESTRA_MIN}


# ═══════════════════════════════════════════════════════════════════════════
# 5. VALOR A 90 DÍAS POR ANUNCIO
# ═══════════════════════════════════════════════════════════════════════════
# La pestaña "Valor en el tiempo" agrupa por MES y por canal/campaña; no mira
# anuncio por anuncio. Acá: cada persona cuenta en el anuncio de su primer
# contacto y se suma lo que pagó en caja a los 30, 60 y 90 días de ese contacto
# (misma regla de atribución de `cohortes_data`: agendó por el bot, o su primer
# pago cayó ≤ 90 días después del contacto). Solo las personas con 90 días ya
# cumplidos ("maduras") se comparan contra el gasto de ese mismo periodo.

VALOR_H = (30, 60, 90)
VALOR_MADURA_DIAS = 90
VALOR_MUESTRA_MIN = 5


def valor90_data(canal: str | None = "meta", hoy: date | None = None) -> dict:
    hoy = hoy or cm._hoy()
    canal = cm._canal(canal) if canal else "meta"
    d0 = date.fromisoformat(cm.COHORTES_DESDE)
    e0, e1 = cm._epoch_ini(d0), cm._epoch_fin(hoy)
    corte_madura = hoy - timedelta(days=VALOR_MADURA_DIAS)
    with db() as c:
        mapa = cm._mapa_anuncios(c)
        primero: dict[str, dict] = {}
        for r in cm._llegadas(c, canal, e0, e1, primera_web=True):
            k = cm._clave(r["phone"])
            if k not in primero:
                primero[k] = r
        if not primero:
            return {"anuncios": [], "total": None, "canal": canal, "desde": cm.COHORTES_DESDE, "hoy": hoy.isoformat()}
        bot: set[str] = set()
        for r in c.execute("SELECT phone, created_at FROM citas_bot WHERE created_at >= ?", (cm._utc_txt(e0 - 3600),)):
            k = cm._clave(r["phone"])
            if k in primero and (cm._utc_txt_epoch(r["created_at"]) or 0) >= primero[k]["ts"] - 3600:
                bot.add(k)
        por_tel = cm._pacientes_por_telefono(c, set(primero))
        pid_k: dict[int, set[str]] = defaultdict(set)
        for k, pids in por_tel.items():
            for pid in pids:
                pid_k[pid].add(k)
        pagos: dict[str, list[tuple[str, int, int]]] = defaultdict(list)
        ids = list(pid_k)
        for i in range(0, len(ids), 500):
            lote = ids[i:i + 500]
            try:
                for r in c.execute("SELECT id_paciente, fecha, monto, id_profesional FROM bi_pagos_caja WHERE "
                                   "id_paciente IN (%s) AND fecha >= ?" % ",".join("?" * len(lote)),
                                   (*lote, cm.COHORTES_DESDE)):
                    for k in pid_k[r[0]]:
                        pagos[k].append(((r[1] or "")[:10], int(r[2] or 0), r[3]))
            except Exception as e:   # noqa: BLE001
                log.warning("valor90: sin caja: %s", e)
        pct = cm._pct_honorarios(c)
        cm._ensure_insights(c)
        gasto: dict[str, dict] = {}
        for r in c.execute("SELECT ad_id, SUM(spend), SUM(CASE WHEN fecha <= ? THEN spend ELSE 0 END) "
                           "FROM meta_insights_diario WHERE desglose='total' AND fecha >= ? GROUP BY ad_id",
                           (corte_madura.isoformat(), cm.COHORTES_DESDE)):
            gasto[r[0]] = {"total": r[1] or 0, "maduro": r[2] or 0}

    def _nuevo():
        return {"personas": 0, "personas_m": 0, "pacientes_m": 0, "volvieron_m": 0, "pacientes_rec": 0,
                "venta_rec": 0, "h": {x: {"venta": 0, "centro": 0.0} for x in VALOR_H}}
    por_ad: dict[str, dict] = defaultdict(_nuevo)
    tot = _nuevo()
    for k, r in primero.items():
        dia = datetime.fromtimestamp(r["ts"], cm._CL).date()
        info = cm._info(mapa, r["source_id"] or "", r["headline"] or "")
        ad_id = info["ad_id"] or "-"
        madura = dia <= corte_madura
        ps = sorted(p for p in pagos.get(k, []) if p[0] >= dia.isoformat())
        ok = bool(ps) and (k in bot or ps[0][0] <= (dia + timedelta(days=cm.VENTANA_TELEFONO_DIAS)).isoformat())
        for o in (por_ad[ad_id], tot):
            o["personas"] += 1
            o["personas_m"] += 1 if madura else 0
        if not ok:
            continue
        if madura:
            dias_pago = {p[0] for p in ps if p[0] <= (dia + timedelta(days=VALOR_MADURA_DIAS)).isoformat()}
            for o in (por_ad[ad_id], tot):
                o["pacientes_m"] += 1
                o["volvieron_m"] += 1 if len(dias_pago) >= 2 else 0
                for hz in VALOR_H:
                    lim = (dia + timedelta(days=hz)).isoformat()
                    for f, monto, prof in ps:
                        if f <= lim:
                            o["h"][hz]["venta"] += monto
                            o["h"][hz]["centro"] += monto * (100 - (pct.get(prof) or cm.PCT_HONORARIO_DEFAULT)) / 100
        else:
            for o in (por_ad[ad_id], tot):
                o["pacientes_rec"] += 1
                o["venta_rec"] += sum(m for _, m, _p in ps)

    def _cerrar(ad_id, o):
        if ad_id:
            g = gasto.get(ad_id, {"total": 0, "maduro": 0})
        else:   # total: suma del gasto de los anuncios que aparecen en la tabla
            g = {"total": sum(gasto.get(i, {}).get("total", 0) for i in por_ad),
                 "maduro": sum(gasto.get(i, {}).get("maduro", 0) for i in por_ad)}
        v30, v90 = o["h"][30]["venta"], o["h"][90]["venta"]
        c90 = round(o["h"][90]["centro"])
        gm = round(g["maduro"])
        return {"personas": o["personas"], "personas_maduras": o["personas_m"], "pacientes_maduros": o["pacientes_m"],
                "volvieron": o["volvieron_m"],
                "volvieron_pct": round(100 * o["volvieron_m"] / o["pacientes_m"]) if o["pacientes_m"] else None,
                "h": [{"dias": x, "venta": round(o["h"][x]["venta"]), "centro": round(o["h"][x]["centro"])} for x in VALOR_H],
                "ticket_90": round(v90 / o["pacientes_m"]) if o["pacientes_m"] else None,
                "extra_30_a_90_pct": round(100 * (v90 - v30) / v30) if v30 else None,
                "gasto_maduro": gm, "gasto_total": round(g["total"]),
                "retorno_centro_90": round(c90 / gm, 2) if (gm and o["pacientes_m"]) else None,
                "estado": ("sin_madurar" if not o["pacientes_m"] and not o["personas_m"] else
                           "sin_gasto" if not gm else "se_paga" if c90 >= gm else "no_se_paga"),
                "muestra_chica": o["pacientes_m"] < VALOR_MUESTRA_MIN,
                "pacientes_recientes": o["pacientes_rec"], "venta_recientes": round(o["venta_rec"])}
    filas = []
    for ad_id, o in por_ad.items():
        info = cm._info(mapa, ad_id if ad_id != "-" else "")
        filas.append({"ad_id": ad_id, "anuncio": info["anuncio"], "campana": info["campana"], **_cerrar(ad_id, o)})
    filas.sort(key=lambda x: (-(x["h"][2]["centro"]), -x["personas"]))
    return {"canal": canal, "desde": cm.COHORTES_DESDE, "hoy": hoy.isoformat(), "madura_dias": VALOR_MADURA_DIAS,
            "madura_hasta": corte_madura.isoformat(), "horizontes": list(VALOR_H),
            "anuncios": filas, "total": _cerrar(None, tot), "muestra_min": VALOR_MUESTRA_MIN}


# ═══════════════════════════════════════════════════════════════════════════
# Modal de un anuncio
# ═══════════════════════════════════════════════════════════════════════════

def anuncio_data(ad_id: str, desde=None, hasta=None, canal=None, plataforma=None, campana=None) -> dict:
    if not _RE_AD.match(ad_id or ""):
        raise HTTPException(400, "Anuncio inválido")
    with db() as c:
        _ensure_creativos(c)
        r = c.execute("SELECT * FROM meta_creativos WHERE ad_id=?", (ad_id,)).fetchone()
        fat = fatiga_todos(c).get(ad_id)
        serie = [{"fecha": x[0], "gasto": round(x[1] or 0), "impresiones": x[2] or 0, "clics": x[3] or 0, "conv": x[4] or 0}
                 for x in c.execute("SELECT fecha, spend, impressions, clicks, conversaciones FROM meta_insights_diario "
                                    "WHERE desglose='total' AND ad_id=? AND fecha >= ? ORDER BY fecha",
                                    (ad_id, (cm._hoy() - timedelta(days=30)).isoformat()))]
    creativo = ({"titulo": r["titulo"] or "", "texto": r["texto"] or "", "cta": r["cta"] or "", "tipo": r["tipo"] or "",
                 "imagen": bool(r["archivo"])} if r else None)
    kw = dict(desde=desde, hasta=hasta, campana=campana, plataforma=plataforma, canal=canal, anuncio=ad_id)
    return {"ad_id": ad_id, "creativo": creativo, "fatiga": fat, "serie": serie,
            "territorio": (territorio_data(**kw)["comunas"][:6]),
            "velocidad": velocidad_data(**kw)["total"]}


# ═══════════════════════════════════════════════════════════════════════════
# AVISO A META (evento Purchase de las 07:07, app/capi_purchase.py)
# ═══════════════════════════════════════════════════════════════════════════
# Solo LEE: `capi_purchase_enviados` (una fila por cita avisada) y
# `capi_purchase_corridas` (una fila por corrida del job). Sin nombres ni
# teléfonos: de cada cita salen solo las iniciales.

AVISO_ULTIMAS = 15
AVISO_VENTANA_DIAS = 30


def _iniciales(nombre: str | None) -> str:
    partes = [p for p in re.split(r"\s+", (nombre or "").strip()) if p]
    return "".join(p[0].upper() + "." for p in partes[:3]) or "—"


def aviso_meta_data(hoy: date | None = None) -> dict:
    hoy = hoy or cm._hoy()
    desde = (hoy - timedelta(days=AVISO_VENTANA_DIAS)).isoformat()
    with db() as c:
        existe = cm._tabla_existe(c, "capi_purchase_enviados")
        corridas_ok = cm._tabla_existe(c, "capi_purchase_corridas")
        if not existe:
            return {"hay_datos": False, "ultima_corrida": None, "corridas": [], "aceptados": 0, "ultimas": [],
                    "ventana_dias": AVISO_VENTANA_DIAS}
        corridas = ([dict(r) for r in c.execute(
            "SELECT fecha_corrida, enviados, diferidos, no_atendidos, duplicados, errores, value_total, estimados "
            "FROM capi_purchase_corridas ORDER BY id DESC LIMIT 14")] if corridas_ok else [])
        tot = c.execute(
            "SELECT COUNT(*), COALESCE(SUM(CASE WHEN estimado=0 THEN value END),0), "
            "COALESCE(SUM(CASE WHEN estimado=1 THEN value END),0), COALESCE(SUM(estimado),0), "
            "COALESCE(SUM(CASE WHEN estimado=0 THEN 1 ELSE 0 END),0) "
            "FROM capi_purchase_enviados WHERE estado='sent' AND fecha >= ?", (desde,)).fetchone()
        omitidas = c.execute("SELECT COUNT(*) FROM capi_purchase_enviados WHERE estado='omitida_dup' AND fecha >= ?",
                             (desde,)).fetchone()[0]
        filas = c.execute(
            "SELECT e.id_cita, e.fecha, e.value, e.venta_total, e.estimado, e.sent_at, cb.especialidad, p.nombre "
            "FROM capi_purchase_enviados e LEFT JOIN citas_bot cb ON cb.id_cita = e.id_cita "
            "LEFT JOIN contact_profiles p ON p.phone = e.phone "
            "WHERE e.estado='sent' GROUP BY e.id_cita ORDER BY e.fecha DESC, e.sent_at DESC LIMIT ?",
            (AVISO_ULTIMAS,)).fetchall()
    ult = corridas[0] if corridas else None
    return {
        "hay_datos": True, "ventana_dias": AVISO_VENTANA_DIAS,
        "ultima_corrida": ult, "corridas": corridas,
        # aceptados = Meta contestó con events_received (solo entonces se marca "sent")
        "aceptados": tot[0], "aceptados_reales": tot[4], "aceptados_estimados": tot[3],
        "valor_real": round(tot[1]), "valor_estimado": round(tot[2]),
        "duplicados_omitidos": omitidas,
        # lo pendiente y lo que falló salen de la ÚLTIMA corrida: se reintenta cada día
        "pendientes": (ult or {}).get("diferidos"), "errores": (ult or {}).get("errores"),
        "ultimas": [{"iniciales": _iniciales(r["nombre"]), "especialidad": r["especialidad"] or "",
                     "fecha": r["fecha"], "value": round(r["value"] or 0), "venta_total": r["venta_total"],
                     "estimado": bool(r["estimado"])} for r in filas],
    }


# ═══════════════════════════════════════════════════════════════════════════
# Rutas
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/agenda")
def agenda(request: Request, token: str | None = Query(None)):
    cm._auth(request, token)
    return agenda_data()


@router.post("/agenda/actualizar")
async def agenda_actualizar(request: Request, token: str | None = Query(None)):
    """Lanza la lectura de cupos en segundo plano (una a la vez, cada 10 min como mucho)."""
    cm._auth(request, token)
    if not _activo_cupos():
        raise HTTPException(409, "La lectura de agenda está apagada (AGENDA_CUPOS_ACTIVE=false)")
    t = _REFRESCO["tarea"]
    if t and not t.done():
        return {"estado": "en_curso"}
    if time.time() - _REFRESCO["ultimo_manual"] < CUPOS_MIN_ENTRE_MANUALES_S:
        return {"estado": "reciente"}
    _REFRESCO["ultimo_manual"] = time.time()
    _REFRESCO["tarea"] = asyncio.create_task(job_agenda_cupos())
    return {"estado": "iniciado"}


@router.get("/creativos")
def creativos(request: Request, token: str | None = Query(None)):
    cm._auth(request, token)
    return creativos_data()


@router.post("/creativos/refrescar")
async def creativos_refrescar(request: Request, token: str | None = Query(None)):
    cm._auth(request, token)
    return await asyncio.to_thread(refrescar_creativos)


@router.get("/creativo/{ad_id}")
def creativo_imagen(ad_id: str, request: Request, token: str | None = Query(None)):
    """Imagen guardada del anuncio (nunca la CDN de Meta)."""
    cm._auth(request, token)
    if not _RE_AD.match(ad_id):
        raise HTTPException(404, "Sin imagen")
    with db() as c:
        _ensure_creativos(c)
        r = c.execute("SELECT archivo FROM meta_creativos WHERE ad_id=?", (ad_id,)).fetchone()
    if not r or not r[0]:
        raise HTTPException(404, "Sin imagen")
    ruta = (_dir_creativos() / r[0]).resolve()
    if ruta.parent != _dir_creativos().resolve() or not ruta.exists():
        raise HTTPException(404, "Sin imagen")
    return FileResponse(ruta, headers={"Cache-Control": "private, max-age=86400"})


@router.get("/anuncio/{ad_id}")
def anuncio(ad_id: str, request: Request, desde: str | None = Query(None), hasta: str | None = Query(None),
            canal: str | None = Query(None), plataforma: str | None = Query(None), campana: str | None = Query(None),
            token: str | None = Query(None)):
    cm._auth(request, token)
    return anuncio_data(ad_id, desde, hasta, canal, plataforma, campana)


@router.get("/territorio")
def territorio(request: Request, desde: str | None = Query(None), hasta: str | None = Query(None),
               campana: str | None = Query(None), plataforma: str | None = Query(None),
               canal: str | None = Query(None), token: str | None = Query(None)):
    cm._auth(request, token)
    return territorio_data(desde, hasta, campana, plataforma, canal)


@router.get("/velocidad")
def velocidad(request: Request, desde: str | None = Query(None), hasta: str | None = Query(None),
              campana: str | None = Query(None), plataforma: str | None = Query(None),
              canal: str | None = Query(None), token: str | None = Query(None)):
    cm._auth(request, token)
    return velocidad_data(desde, hasta, campana, plataforma, canal)


@router.get("/valor90")
def valor90(request: Request, canal: str | None = Query("meta"), token: str | None = Query(None)):
    cm._auth(request, token)
    return valor90_data(canal)


@router.get("/aviso-meta")
def aviso_meta(request: Request, token: str | None = Query(None)):
    cm._auth(request, token)
    return aviso_meta_data()
