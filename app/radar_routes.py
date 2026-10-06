# -*- coding: utf-8 -*-
"""Alma Radar — `/alma/radar` (HTML) + `/alma/api/radar/*` (JSON). SOLO DUEÑO.

QUÉ ES
------
La vista de captación y crecimiento del CMC en 15 módulos (pulso del día,
embudo, creativos, territorio, recepción, finanzas, bitácora…). Nació como
demo de producto; esta versión CONECTA cada módulo con lo que ya existe y deja
el resto rotulado "Ilustrativo" en la propia página.

DE DÓNDE SALE CADA CIFRA (no se recalcula nada que ya exista)
-------------------------------------------------------------
- Embudo, finanzas por canal, creativos: `campanas_meta_routes.panel_data`
  (MISMA función que el panel /alma/campanas-meta: los números cuadran).
- Agenda de mañana y Motor: `campanas_meta_integraciones.agenda_data`
  (cache `agenda_cupos_cache`, cron 05:30/21:30). Jamás Medilink.
- Creativos y fatiga: `creativos_data` (imágenes en data/creativos, servidas
  por /alma/api/campanas-meta/creativo/{id}). Jamás la CDN de Meta.
- Territorio, velocidad de recepción, valor a 90 días, aviso a Meta:
  `territorio_data`, `velocidad_data`, `valor90_data`, `aviso_meta_data`.
- Google: `campanas_meta_routes.google_data` (tablas gsc_*).
- Venta mensual: `bi_pagos_caja` (VENTA = caja). "Para el centro" = monto ×
  (1 − pct_honorario/100); pct_honorario es lo que se lleva el PROFESIONAL
  (`campanas_meta_routes._pct_honorarios`, default 70, Abarca 62).
- Pulso y "En vivo": `messages` (primer mensaje entrante de HOY por persona),
  `meta_referrals` / evento `web_origen` (canal), `citas_bot` (agendó),
  `sessions` (estado). Solo canal, especialidad, estado e INICIALES: nunca
  teléfono, nombre completo ni RUT.
- Experimentos: eventos `reenganche_enviado` (variante con/sin hora) cruzados
  con `citas_bot` dentro de 24 h; `consultas_persistencia`.
- Bitácora: solo lo que deja rastro (capi_purchase_corridas, meta_insights_diario,
  gsc_paginas_diario, agenda_cupos_estado, meta_creativos, evento
  centinela_diario, bi_sync_log, archivos de respaldo). Lo que no deja rastro
  no se inventa.
- Consentimiento: conteos agregados del evento `marketing_consent_respuesta`
  y de `privacy_consents`. Ninguna fila por paciente.

RENDIMIENTO
-----------
Abrir la página NO llama a Medilink ni a Meta: todo es SQLite/caché local
(`campanas_meta_routes.solo_datos_locales()` apaga la consulta en vivo de
estados de anuncios). Los agregados pesados se cachean 5 minutos; el pulso,
30 segundos (la página lo refresca cada 60).

AUTH
----
Solo OLACORE_TOKEN (mismo `_auth` que Campañas Meta: token en query o Bearer).
El ADMIN_TOKEN de recepción no entra: hay gasto publicitario y venta.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

import campanas_meta_routes as cm
from session import db

log = logging.getLogger("radar_routes")
router = APIRouter(prefix="/alma/api/radar", tags=["radar"])
pagina = APIRouter(tags=["radar"])

_CL = cm._CL
RANGO_DIAS = 30
TTL_PESADO = 300          # agregados de 30/90 días
TTL_PULSO = 30            # entradas de hoy (la página refresca cada 60 s)
ESPERA_ALERTA_MIN = 15    # minutos hábiles esperando a recepción → alerta
FEED_MAX = 14
BACKUP_DIR = os.getenv("RADAR_BACKUP_DIR", "/opt/backups/chatbot-cmc")

_TEMPLATE = Path(__file__).resolve().parent.parent / "templates" / "alma_radar.html"


# ── Caché en memoria (por proceso) ──────────────────────────────────────────

_CACHE: dict[str, tuple[float, object]] = {}
_LOCKS: dict[str, threading.Lock] = defaultdict(threading.Lock)


def _cacheado(clave: str, ttl: int, fn):
    hit = _CACHE.get(clave)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    with _LOCKS[clave]:          # una sola corrida por clave aunque lleguen varias pestañas
        hit = _CACHE.get(clave)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        val = fn()
        _CACHE[clave] = (time.time(), val)
        return val


def limpiar_cache() -> None:
    _CACHE.clear()


def _seguro(nombre: str, fn, *a, **k):
    """Una fuente que falla no tumba el módulo: devuelve {"error": ...}."""
    try:
        return fn(*a, **k)
    except Exception as e:  # noqa: BLE001
        log.warning("radar: %s falló: %s", nombre, e, exc_info=True)
        return {"error": "No se pudo leer esta fuente en este momento."}


# ── Utilidades ──────────────────────────────────────────────────────────────

def _ahora() -> datetime:
    """Reloj de Chile. Una sola función para que la demo local pueda fijarlo."""
    return datetime.now(_CL)


def _hoy() -> date:
    return _ahora().date()


def _rango(hoy: date | None = None) -> tuple[str, str]:
    h = hoy or _hoy()
    return (h - timedelta(days=RANGO_DIAS - 1)).isoformat(), h.isoformat()


def _dia_utc(dia: date) -> tuple[str, str]:
    return cm._utc_txt(cm._epoch_ini(dia)), cm._utc_txt(cm._epoch_fin(dia))


def _iniciales(nombre: str | None) -> str:
    import campanas_meta_integraciones as ci
    return ci._iniciales(nombre)


def _anon(phone: str) -> str:
    """Id estable para que la página reconozca una entrada entre refrescos,
    sin exponer el teléfono."""
    import config
    sal = (getattr(config, "OLACORE_TOKEN", "") or "radar").encode()
    return hashlib.sha256(sal + (phone or "").encode()).hexdigest()[:12]


def _ts_cl(utc_txt: str | None) -> datetime | None:
    ep = cm._utc_txt_epoch(utc_txt)
    return datetime.fromtimestamp(ep, _CL) if ep else None


def _iso_epoch(ep: int | float | None) -> str | None:
    return datetime.fromtimestamp(ep, _CL).isoformat(timespec="minutes") if ep else None


def _en(c, tabla: str, lista: list, cols: str, campo: str = "phone") -> list:
    out = []
    for i in range(0, len(lista), 500):
        lote = lista[i:i + 500]
        try:
            out += c.execute(f"SELECT {cols} FROM {tabla} WHERE {campo} IN ({','.join('?' * len(lote))})",
                             lote).fetchall()
        except Exception as e:  # noqa: BLE001
            log.debug("radar: %s sin datos: %s", tabla, e)
    return out


def _admin_claves() -> set[str]:
    import config
    out = set()
    for k in ("ADMIN_ALERT_PHONE",):
        v = getattr(config, k, "") or ""
        for p in str(v).split(","):
            if p.strip():
                out.add(cm._clave(p.strip()))
    return out


# Canales del pulso (la página tiene su color y su nombre)
CANALES_PULSO = {
    "fb": "Anuncio · Facebook", "ig": "Anuncio · Instagram", "meta": "Anuncio Meta",
    "web": "Página web", "wa": "WhatsApp directo", "ms": "Messenger", "igd": "Instagram Direct",
}
_SESION_ACTIVA = ("WAIT_", "CONFIRMING")


def _canal_ref(plataforma: str | None) -> str:
    p = (plataforma or "").strip().lower()
    return "fb" if p == "facebook" else "ig" if p == "instagram" else "meta"


def _canal_phone(phone: str) -> str:
    return "ms" if phone.startswith("fb_") else "igd" if phone.startswith("ig_") else "wa"


def _esp_de_sesion(data_txt: str | None) -> str:
    try:
        d = json.loads(data_txt or "{}")
    except (ValueError, TypeError):
        return ""
    v = d.get("especialidad") or d.get("especialidad_sugerida") or ""
    return str(v)[:40].strip().capitalize() if isinstance(v, str) else ""


# ═══════════════════════════════════════════════════════════════════════════
# 01 · PULSO
# ═══════════════════════════════════════════════════════════════════════════

def _primeras_entradas(c, dia: date, excluir: set[str]) -> dict[str, int]:
    """phone → epoch del PRIMER mensaje entrante del día (hora de Chile)."""
    a, b = _dia_utc(dia)
    out = {}
    for r in c.execute("SELECT phone, MIN(ts) FROM messages WHERE direction='in' AND ts >= ? AND ts < ? "
                       "GROUP BY phone", (a, b)):
        ph = r[0] or ""
        if not ph or cm._clave(ph) in excluir:
            continue
        ep = cm._utc_txt_epoch(r[1])
        if ep:
            out[ph] = ep
    return out


def _referencia_dia(hoy: date, excluir: set[str]) -> dict:
    """Promedio de entradas por media hora del MISMO día de la semana en las
    4 semanas anteriores: la línea de referencia del monitor."""
    buckets = [0.0] * 48
    horas = [0.0] * 24
    dias = 0
    with db() as c:
        for k in range(1, 5):
            dia = hoy - timedelta(days=7 * k)
            ent = _primeras_entradas(c, dia, excluir)
            if not ent:
                continue
            dias += 1
            for ep in ent.values():
                t = datetime.fromtimestamp(ep, _CL)
                buckets[(t.hour * 60 + t.minute) // 30] += 1
                horas[t.hour] += 1
    if dias:
        buckets = [round(x / dias, 2) for x in buckets]
        horas = [round(x / dias, 2) for x in horas]
    return {"semanas": dias, "por_media_hora": buckets, "por_hora": horas,
            "dia_semana": _DIAS_SEMANA[hoy.weekday()], "total_dia": round(sum(buckets), 1)}


_DIAS_SEMANA = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábados", "domingos")


def _ref_hasta(ref: dict, minuto: float) -> float | None:
    """Personas que en promedio ya habían escrito a esta misma hora del día
    (suma de medias horas completas + la fracción de la media hora en curso)."""
    if not isinstance(ref, dict) or not ref.get("semanas"):
        return None
    b = ref.get("por_media_hora") or []
    minuto = max(0.0, min(1440.0, float(minuto)))
    i = int(minuto // 30)
    tot = sum(b[:i]) + (b[i] * (minuto - i * 30) / 30 if i < len(b) else 0)
    return round(tot, 1)


def _esperando(c, ahora_ep: int, excluir: set[str]) -> list[dict]:
    """Personas que hoy esperan una respuesta humana (sesión en recepción y
    mensajes suyos sin responder). Minutos HÁBILES, igual que Campañas Meta."""
    import recepcion_tiempos as rt
    desde = cm._utc_txt(ahora_ep - 48 * 3600)
    ses = {r["phone"]: r["data"] for r in c.execute(
        "SELECT phone, data FROM sessions WHERE state='HUMAN_TAKEOVER' AND updated_at >= ?", (desde,))
        if r["phone"] and cm._clave(r["phone"]) not in excluir}
    if not ses:
        return []
    phones = list(ses)
    msgs: dict[str, list[dict]] = defaultdict(list)
    for i in range(0, len(phones), 400):
        lote = phones[i:i + 400]
        for r in c.execute(f"SELECT phone, direction, text, state, ts FROM messages WHERE ts >= ? AND phone IN "
                           f"({','.join('?' * len(lote))})", (cm._utc_txt(ahora_ep - 72 * 3600), *lote)):
            msgs[r["phone"]].append({"ts": cm._utc_txt_epoch(r["ts"]) or 0, "dir": r["direction"],
                                     "texto": r["text"] or "", "state": r["state"] or ""})
    refs = {r[0]: r[1] for r in _en(c, "meta_referrals", phones, "phone, plataforma")}
    nombres = {r[0]: r[1] for r in _en(c, "contact_profiles", phones, "phone, nombre")}
    out = []
    for ph in phones:
        e = rt.espera_actual(msgs.get(ph, []), ahora_ep)
        if not e:
            continue
        out.append({"id": _anon(ph), "minutos": round(e["minutos"] or 0),
                    "desde": _iso_epoch(e["inicio"]),
                    "canal": _canal_ref(refs[ph]) if ph in refs else _canal_phone(ph),
                    "especialidad": _esp_de_sesion(ses.get(ph)),
                    "iniciales": _iniciales(nombres.get(ph))})
    out.sort(key=lambda x: -x["minutos"])
    return out


def _vivo(ahora: datetime | None = None) -> dict:
    ahora = ahora or _ahora()
    hoy = ahora.date()
    excluir = _admin_claves()
    e0, e1 = cm._epoch_ini(hoy), cm._epoch_fin(hoy)
    a, b = _dia_utc(hoy)
    with db() as c:
        ent = _primeras_entradas(c, hoy, excluir)
        refs: dict[str, str] = {}
        for r in c.execute("SELECT phone, plataforma, ts FROM meta_referrals WHERE ts >= ? AND ts < ? ORDER BY ts",
                           (e0, e1)):
            refs.setdefault(r[0], r[1] or "")
        web = set()
        try:
            for r in c.execute("SELECT phone FROM conversation_events WHERE event='web_origen' AND ts >= ? AND ts < ?",
                               (a, b)):
                web.add(r[0])
        except Exception:
            pass
        citas: dict[str, dict] = {}
        n_citas = n_citas_meta = 0
        for r in c.execute("SELECT phone, especialidad, ad_source_id, ad_plataforma, paciente_nombre, created_at "
                           "FROM citas_bot WHERE created_at >= ? AND created_at < ? ORDER BY created_at", (a, b)):
            if cm._clave(r["phone"] or "") in excluir:
                continue
            n_citas += 1
            n_citas_meta += 1 if (r["ad_source_id"] or "").strip() else 0
            citas.setdefault(r["phone"], dict(r))
        phones = list(set(ent) | set(citas))
        ses = {r[0]: (r[1] or "", r[2]) for r in _en(c, "sessions", phones, "phone, state, data")}
        nombres = {r[0]: r[1] for r in _en(c, "contact_profiles", phones, "phone, nombre")}
        esperando = _esperando(c, int(ahora.timestamp()), excluir)
        persist = None
        try:
            persist = c.execute("SELECT COUNT(*) FROM consultas_persistencia WHERE estado IN ('abierta','contactada') "
                                "AND opened_at >= ?", (cm._utc_txt(int(ahora.timestamp()) - 48 * 3600),)).fetchone()[0]
        except Exception:
            persist = None

    eventos = []
    for ph in phones:
        ci_ = citas.get(ph)
        ep = ent.get(ph) or cm._utc_txt_epoch(ci_["created_at"]) if ci_ else ent.get(ph)
        if not ep:
            continue
        if ph in refs:
            canal = _canal_ref(refs[ph])
        elif ci_ and (ci_.get("ad_source_id") or "").strip():
            canal = _canal_ref(ci_.get("ad_plataforma"))
        elif ph in web:
            canal = "web"
        else:
            canal = _canal_phone(ph)
        st, data = ses.get(ph, ("", None))
        if ci_:
            estado = "agendo"
        elif st == "HUMAN_TAKEOVER":
            estado = "recepcion"
        elif st.startswith(_SESION_ACTIVA):
            estado = "conversando"
        else:
            estado = "sin_cita"
        esp = (ci_.get("especialidad") or "").strip() if ci_ else _esp_de_sesion(data)
        t = datetime.fromtimestamp(ep, _CL)
        eventos.append({"id": _anon(ph), "min": round(t.hour * 60 + t.minute + t.second / 60, 2),
                        "hora": t.strftime("%H:%M"), "canal": canal, "estado": estado,
                        "especialidad": esp[:40], "iniciales": _iniciales(nombres.get(ph) or (ci_ or {}).get("paciente_nombre"))})
    eventos.sort(key=lambda x: x["min"])
    por_canal: dict[str, int] = defaultdict(int)
    por_estado: dict[str, int] = defaultdict(int)
    for e in eventos:
        por_canal[e["canal"]] += 1
        por_estado[e["estado"]] += 1
    return {"hoy": hoy.isoformat(), "ahora": ahora.isoformat(timespec="seconds"),
            "ahora_min": ahora.hour * 60 + ahora.minute,
            "eventos": eventos, "por_canal": dict(por_canal), "por_estado": dict(por_estado),
            "citas_hoy": n_citas, "citas_hoy_meta": n_citas_meta,
            "esperando": esperando[:12], "esperando_total": len(esperando),
            "esperando_max_min": esperando[0]["minutos"] if esperando else None,
            "persistencia_abiertas_48h": persist}


def _venta_mes(hoy: date) -> dict:
    ini = hoy.replace(day=1).isoformat()
    with db() as c:
        try:
            r = c.execute("SELECT COALESCE(SUM(monto),0), MAX(fecha), MAX(synced_at) FROM bi_pagos_caja "
                          "WHERE fecha >= ? AND fecha <= ?", (ini, hoy.isoformat() + " 23:59:59")).fetchone()
        except Exception:
            return {"venta": None}
    return {"venta": int(r[0] or 0), "hasta": (r[1] or "")[:10] or None, "sincronizado": r[2],
            "desde": ini}


def _agenda() -> dict:
    import campanas_meta_integraciones as ci
    return _cacheado("agenda", TTL_PESADO, lambda: ci.agenda_data())


def _agenda_manana(ag: dict, hoy: date) -> dict:
    manana = (hoy + timedelta(days=1)).isoformat()
    if not ag or ag.get("error") or ag.get("sin_datos"):
        return {"sin_datos": True, "fecha": manana, "activo": (ag or {}).get("activo")}
    idx = ag["fechas"].index(manana) if manana in ag.get("fechas", []) else None
    grupos: dict[str, dict] = {}
    for p in ag["profesionales"]:
        n = p["por_dia"][idx] if idx is not None else None
        g = grupos.setdefault(p["grupo"], {"grupo": p["grupo"], "libres": 0, "valor": 0, "leidos": 0,
                                           "profesionales": 0, "sin_precio": 0})
        if n is None:
            continue
        g["leidos"] += 1
        g["profesionales"] += 1
        g["libres"] += n
        if p.get("precio"):
            g["valor"] += round(n * p["precio"] * p["margen_pct"] / 100)
        elif n:
            g["sin_precio"] += 1
    filas = [g for g in grupos.values() if g["leidos"]]
    filas.sort(key=lambda g: (-g["valor"], -g["libres"]))
    return {"sin_datos": not filas, "fecha": manana, "grupos": filas,
            "libres": sum(g["libres"] for g in filas), "valor": sum(g["valor"] for g in filas),
            "actualizado": _iso_epoch(ag.get("actualizado_ts")), "desactualizado": ag.get("desactualizado")}


def _decisiones(vivo: dict, ag: dict) -> list[dict]:
    out = []
    n = vivo.get("esperando_total") or 0
    if n:
        mx = vivo.get("esperando_max_min") or 0
        out.append({"icono": "msg", "titulo": f"Responder primero a {n} persona{'s' if n != 1 else ''} que espera{'n' if n != 1 else ''} a recepción",
                    "texto": (f"La más antigua lleva {mx} min hábiles sin respuesta humana. Una conversación de anuncio se enfría en minutos."
                              if mx else "Escribieron fuera del horario de recepción: el tiempo de espera empieza a contar a la apertura."),
                    "accion": {"label": "Abrir panel de recepción", "modulo": "panel", "href": "/admin/v2"},
                    "urgente": mx >= ESPERA_ALERTA_MIN})
    for g in [x for x in (ag or {}).get("senales", []) if x.get("grupo") not in ("Otra", "", None)][:2]:
        if g["senal"] in ("sin_cupos", "pocos_cupos"):
            out.append({"icono": "pause",
                        "titulo": f"{g['grupo']}: Meta gasta ${g['gasto_dia']:,}".replace(",", ".") + f" al día y quedan {g['libres_7d']} cupos en 7 días",
                        "texto": "Conviene bajar o pausar esos anuncios hasta que se abran horas. El cambio se hace en el Administrador de anuncios.",
                        "accion": {"label": "Ver agenda × anuncios", "modulo": "campanas_meta", "href": "/alma/campanas-meta#agenda"}})
        elif g["senal"] == "cupos_sin_anuncio":
            out.append({"icono": "send",
                        "titulo": f"{g['grupo']} tiene {g['libres_14d']} cupos libres en 14 días y ningún anuncio con gasto",
                        "texto": "Hasta " + f"${g['valor_14d']:,}".replace(",", ".") + " para el centro si se llenaran. Puede valer la pena activar un anuncio de esa especialidad.",
                        "accion": {"label": "Ver agenda × anuncios", "modulo": "campanas_meta", "href": "/alma/campanas-meta#agenda"}})
    p = vivo.get("persistencia_abiertas_48h")
    if p:
        out.append({"icono": "repeat", "titulo": f"{p} consulta{'s' if p != 1 else ''} de las últimas 48 h quedaron sin agendar",
                    "texto": "Recuperar pacientes ordena a quién llamar y con qué mensaje; el envío lo hace una persona.",
                    "accion": {"label": "Abrir Recuperar pacientes", "modulo": "recuperar", "href": "/alma/recuperar"}})
    return out[:3]


def pulso_data(ahora: datetime | None = None) -> dict:
    ahora = ahora or _ahora()
    hoy = ahora.date()
    vivo = _cacheado(f"vivo:{hoy}", TTL_PULSO, lambda: _vivo(ahora))
    ag = _seguro("agenda", _agenda)
    ref = _seguro("referencia", lambda: _cacheado(f"ref:{hoy}", TTL_PESADO, lambda: _referencia_dia(hoy, _admin_claves())))
    if isinstance(ref, dict) and not ref.get("error"):
        # la fracción depende de la hora: va fuera del caché del día
        ref = {**ref, "hasta_ahora": _ref_hasta(ref, ahora.hour * 60 + ahora.minute)}
    return {**vivo, "agenda_manana": _agenda_manana(ag, hoy), "referencia": ref,
            "venta_mes": _seguro("venta_mes", lambda: _cacheado(f"vm:{hoy}", TTL_PESADO, lambda: _venta_mes(hoy))),
            "decisiones": _decisiones(vivo, ag if isinstance(ag, dict) else {}),
            "canales": CANALES_PULSO}


# ═══════════════════════════════════════════════════════════════════════════
# Captación: embudo, Google, creativos, territorio, valor 90 días, motor
# ═══════════════════════════════════════════════════════════════════════════

def _panel(canal: str, d: str, h: str) -> dict:
    def _f():
        with cm.solo_datos_locales():
            return cm.panel_data(d, h, canal=canal)
    return _cacheado(f"panel:{canal}:{d}:{h}", TTL_PESADO, _f)


def _k_embudo(k: dict) -> dict:
    keys = ("personas", "conversaciones", "citas", "atendidos", "no_asistio", "anuladas", "venta", "centro",
            "gasto", "cac_cita", "cac_atendido", "retorno", "retorno_centro", "retorno_estricto", "pagaron",
            "atendidos_fuente", "muestra_chica", "valor12m")
    return {x: k.get(x) for x in keys}


def _plataformas(d: str, h: str) -> dict:
    e0 = cm._epoch_ini(date.fromisoformat(d))
    e1 = cm._epoch_fin(date.fromisoformat(h))
    with db() as c:
        filas = c.execute("SELECT COALESCE(NULLIF(lower(plataforma),''),'sin_dato'), COUNT(DISTINCT phone) "
                          "FROM meta_referrals WHERE ts >= ? AND ts < ? GROUP BY 1", (e0, e1)).fetchall()
    return {r[0]: r[1] for r in filas}


def _ctr_semanas(ad_ids: list[str], hasta: date, semanas: int = 6) -> dict[str, list]:
    if not ad_ids:
        return {}
    d0 = hasta - timedelta(days=7 * semanas - 1)
    acc: dict[str, list[list[int]]] = {a: [[0, 0] for _ in range(semanas)] for a in ad_ids}
    with db() as c:
        cm._ensure_insights(c)
        for r in c.execute("SELECT ad_id, fecha, impressions, clicks FROM meta_insights_diario WHERE desglose='total' "
                           "AND fecha >= ? AND fecha <= ?", (d0.isoformat(), hasta.isoformat())):
            if r[0] not in acc:
                continue
            i = (date.fromisoformat(r[1]) - d0).days // 7
            if 0 <= i < semanas:
                acc[r[0]][i][0] += r[2] or 0
                acc[r[0]][i][1] += r[3] or 0
    return {a: [round(100 * cl / im, 2) if im else None for im, cl in v] for a, v in acc.items()}


def _creativos(pm: dict) -> dict:
    import campanas_meta_integraciones as ci
    cr = ci.creativos_data()
    anuncios = [a for a in pm.get("anuncios", []) if a.get("canal") == "meta" and (a.get("gasto") or a.get("citas"))]
    ult = pm.get("ultima_foto")
    hasta = date.fromisoformat(ult) if ult else _hoy()
    ctr = _ctr_semanas([a["ad_id"] for a in anuncios if a.get("ad_id")], hasta)
    out = []
    for a in anuncios:
        c_ = cr["anuncios"].get(a["ad_id"]) or {}
        fat = c_.get("fatiga") or {}
        out.append({"ad_id": a["ad_id"], "anuncio": a["anuncio"], "campana": a["campana"],
                    "gasto": a["gasto"], "citas": a["citas"], "atendidos": a["atendidos"], "centro": a["centro"],
                    "retorno_centro": a.get("retorno_centro"), "cac_cita": a.get("cac_cita"),
                    "frecuencia": a.get("frecuencia"), "activo": a.get("activo"), "muestra_chica": a.get("muestra_chica"),
                    "titulo": c_.get("titulo") or "", "texto": (c_.get("texto") or "")[:220], "cta": c_.get("cta") or "",
                    "tipo": c_.get("tipo") or "", "imagen": bool(c_.get("imagen")),
                    "fatiga": {"etiqueta": fat.get("etiqueta") or "sin_datos",
                               "ctr": (fat.get("actual") or {}).get("ctr"),
                               "frecuencia_7d": (fat.get("actual") or {}).get("frecuencia"),
                               "ctr_cambio_pct": fat.get("ctr_cambio_pct"), "sugerencia": fat.get("sugerencia") or "",
                               "ventana": fat.get("ventana")},
                    "ctr_semanas": ctr.get(a["ad_id"], [])})
    out.sort(key=lambda x: (x["retorno_centro"] is None, -(x["retorno_centro"] or 0), -x["gasto"]))
    return {"anuncios": out, "resumen_fatiga": cr.get("resumen_fatiga"), "con_creativos": cr.get("con_creativos"),
            "activo": cr.get("activo"), "ultima_foto": ult}


def _google(d: str, h: str) -> dict:
    def _f():
        with cm.solo_datos_locales():
            g = cm.google_data(d, h)
        return {"hay_datos": g["hay_datos"], "ultima_fecha": g["ultima_fecha"], "totales": g["totales"],
                "paginas": [{"pagina": p["pagina"], "clicks": p["clicks"], "impressions": p["impressions"],
                             "posicion": p["posicion"], "personas": p["personas"], "citas": p["citas"]}
                            for p in g["paginas"][:6]],
                "oportunidades": [{"consulta": o["consulta"], "impressions": o["impressions"], "clicks": o["clicks"],
                                   "posicion": o["posicion"]} for o in g["oportunidades"][:5]]}
    return _cacheado(f"google:{d}:{h}", TTL_PESADO, _f)


def _territorio(d: str, h: str) -> dict:
    import campanas_meta_integraciones as ci

    def _f():
        t = ci.territorio_data(d, h, canal="todos")
        return {"cobertura": t["cobertura"], "comunas": t["comunas"][:12], "mapa": t["mapa"]}
    return _cacheado(f"terr:{d}:{h}", TTL_PESADO, _f)


def _valor90() -> dict:
    import campanas_meta_integraciones as ci

    def _f():
        v = ci.valor90_data("meta")
        top = [{"anuncio": a["anuncio"], "personas": a["personas"], "pacientes_maduros": a["pacientes_maduros"],
                "ticket_90": a["ticket_90"], "volvieron_pct": a["volvieron_pct"], "retorno_centro_90": a["retorno_centro_90"],
                "h": a["h"], "estado": a["estado"], "muestra_chica": a["muestra_chica"]} for a in v.get("anuncios", [])[:5]]
        # sin personas, valor90_data devuelve una forma corta (sin madura_hasta y total=None)
        return {"desde": v.get("desde"), "madura_hasta": v.get("madura_hasta"), "total": v.get("total") or {}, "top": top}
    return _cacheado("valor90", TTL_PESADO, _f)


def _valor12m() -> dict:
    """Valor de un paciente nuevo a 12 meses por puerta de entrada (lee la tabla cache de cohortes)."""
    import valor_cohortes as vc

    def _f():
        with db() as c:
            p = vc.publico(c)
        top = [{"especialidad": e["especialidad"], "n": e["n"], "c365": e["c"]["365"], "v365": e["v"]["365"],
                "visitas": e["visitas"], "muestra_chica": e["muestra_chica"]} for e in p["especialidades"][:6]]
        sig = [{"entrada": m["entrada"], "n": m["n"], "muestra_chica": m["muestra_chica"],
                "destinos": [{"esp": d["esp"], "pct": d["pct"], "venta_pasan": d["venta_pasan"]} for d in m["destinos"][:3]]}
               for m in p["matriz"][:3] if m["destinos"]]
        return {"meta": p["meta"], "top": top, "recorrido": sig, "sin_especialidad": len(p["sin_especialidad"])}
    return _cacheado("valor12m", TTL_PESADO, _f)


def _panel_plat(plat: str, d: str, h: str) -> dict:
    def _f():
        with cm.solo_datos_locales():
            return cm.panel_data(d, h, canal="meta", plataforma=plat)
    return _cacheado(f"panel:meta:{plat}:{d}:{h}", TTL_PESADO, _f)


# Plataforma por persona: `meta_referrals.plataforma` / `citas_bot.ad_plataforma`,
# registradas desde cm.PLATAFORMA_DESDE. Antes de esa fecha la persona queda
# "sin plataforma" aunque Meta sí reparta el gasto por publisher_platform.
_PLAT_FILAS = (("ig", "instagram", "Instagram"), ("fb", "facebook", "Facebook"))
_MSG_PLATS = ("messenger", "whatsapp")


def _meta_plataformas(d: str, h: str) -> dict:
    d_, h_ = date.fromisoformat(d), date.fromisoformat(h)
    e0, e1 = cm._epoch_ini(d_), cm._epoch_fin(h_)
    a, b = cm._utc_txt(e0), cm._utc_txt(e1)
    with db() as c:
        cm._ensure_insights(c)
        ins: dict[str, dict] = defaultdict(lambda: {"gasto": 0.0, "conv": 0, "alcance": 0, "impresiones": 0})
        tot = {"gasto": 0.0, "conv": 0}
        for f in cm._insights_filas(c, d_, h_, None, None, "plataforma"):
            x = ins[(f["valor"] or "").lower()]
            x["gasto"] += f["spend"] or 0
            x["conv"] += f["conversaciones"] or 0
            x["alcance"] += f["reach"] or 0
            x["impresiones"] += f["impressions"] or 0
        for f in cm._insights_filas(c, d_, h_, None, None, "total"):
            tot["gasto"] += f["spend"] or 0
            tot["conv"] += f["conversaciones"] or 0
        ph = ",".join("?" * len(_MSG_PLATS))
        msg_p = c.execute(f"SELECT COUNT(DISTINCT phone) FROM meta_referrals WHERE ts >= ? AND ts < ? AND "
                          f"(lower(COALESCE(plataforma,'')) IN ({ph}) OR substr(phone,1,3) IN ('fb_','ig_'))",
                          (e0, e1, *_MSG_PLATS)).fetchone()[0]
        msg_c = c.execute(f"SELECT COUNT(*) FROM citas_bot WHERE created_at >= ? AND created_at < ? AND "
                          f"COALESCE(ad_source_id,'') != '' AND (lower(COALESCE(ad_plataforma,'')) IN ({ph}) OR "
                          f"substr(phone,1,3) IN ('fb_','ig_'))",
                          (a, b, *_MSG_PLATS)).fetchone()[0]
    hay_desglose = bool(ins)
    filas = []
    for k, plat, lbl in _PLAT_FILAS:
        p = _panel_plat(plat, d, h)
        kk = _k_embudo(p["kpis"])
        x = ins.get(plat) or {}
        kk.update({"gasto": round(x.get("gasto", 0)), "conversaciones": x.get("conv", 0), "alcance": x.get("alcance", 0),
                   "impresiones": x.get("impresiones", 0)})
        filas.append({"k": k, "l": lbl, "plataforma": plat, **kk})
    p = _panel_plat("sin_dato", d, h)
    sin = _k_embudo(p["kpis"])
    resto_g = tot["gasto"] - sum(f["gasto"] for f in filas) if hay_desglose else tot["gasto"]
    resto_c = tot["conv"] - sum(f["conversaciones"] for f in filas) if hay_desglose else tot["conv"]
    sin.update({"gasto": max(0, round(resto_g)), "conversaciones": max(0, resto_c), "alcance": None, "impresiones": None})
    msg = {"k": "msg", "l": "Messenger/WhatsApp clic", "personas": msg_p, "citas": msg_c, "parcial": True}
    sin["personas"] = max(0, (sin.get("personas") or 0) - msg_p)
    sin["citas"] = max(0, (sin.get("citas") or 0) - msg_c)
    filas.append(msg)
    filas.append({"k": "sin", "l": "Meta · sin plataforma", "plataforma": "sin_dato", **sin})
    return {"filas": filas, "desde": cm.PLATAFORMA_DESDE, "antes_de_plataforma": d < cm.PLATAFORMA_DESDE,
            "gasto_por_plataforma": hay_desglose,
            "otras_plataformas_gasto": round(sum(v["gasto"] for kk_, v in ins.items() if kk_ not in ("facebook", "instagram")))}


# Respuestas a «¿Cómo nos conociste?» (tags referido:* que guarda el bot) y el
# marcador de QR (evento qr_origen). Pregunta activa desde PREGUNTA_DESDE.
PREGUNTA_DESDE = "2026-10-05"
DECLARADOS = (
    ("recomendacion", "Recomendación (amigo/familiar)", ("amigo", "codigo")),
    ("fbig", "Dijo Facebook/Instagram sin clic de anuncio", ("facebook_instagram",)),
    ("google", "Google", ("google",)),
    ("letrero", "Letrero/radio", ("calle", "radio")),
    ("qr", "QR", ("qr",)),
)


def _declarados(c, d: str, h: str) -> dict:
    """Personas que respondieron en el rango y NO llegaron por anuncio ni por la
    web (ya contadas allí). Por fila: personas, citas del bot, atendidos (pagaron
    en caja), venta en el rango y cuántos son pacientes NUEVOS (primer pago de su
    historia en el rango, misma regla que el resto del embudo)."""
    d_, h_ = date.fromisoformat(d), date.fromisoformat(h)
    a, b = cm._utc_txt(cm._epoch_ini(d_)), cm._utc_txt(cm._epoch_fin(h_))
    resp: dict[str, tuple[str, str]] = {}          # clave → (opción, ts)
    for ph, tag, ts in c.execute("SELECT phone, tag, ts FROM contact_tags WHERE tag LIKE 'referido:%' AND ts >= ? AND ts < ?",
                                 (a, b)):
        k = cm._clave(ph)
        op = (tag or "").split(":", 1)[-1].strip().lower()
        if k not in resp or (ts or "") >= resp[k][1]:
            resp[k] = (op, ts or "")
    qr_existe = False
    try:
        for ph, ts in c.execute("SELECT phone, ts FROM conversation_events WHERE event='qr_origen' AND ts >= ? AND ts < ?", (a, b)):
            resp[cm._clave(ph)] = ("qr", ts or "")
        qr_existe = c.execute("SELECT 1 FROM conversation_events WHERE event='qr_origen' LIMIT 1").fetchone() is not None
    except Exception:  # noqa: BLE001
        pass
    if not resp:
        return {"filas": [], "desde": PREGUNTA_DESDE, "qr_existe": qr_existe, "nuevos": 0, "venta_nuevos": 0}
    con_anuncio = {cm._clave(r[0]) for r in c.execute("SELECT DISTINCT phone FROM meta_referrals")}
    con_web = {cm._clave(r[0]) for r in c.execute("SELECT DISTINCT phone FROM contact_tags WHERE tag LIKE 'referral_source:web%'")}
    try:
        con_web |= {cm._clave(r[0]) for r in c.execute("SELECT DISTINCT phone FROM conversation_events WHERE event='web_origen'")}
    except Exception:  # noqa: BLE001
        pass
    opcion_a_fila = {op: fid for fid, _, ops in DECLARADOS for op in ops}
    personas: dict[str, set] = defaultdict(set)
    for k, (op, _) in resp.items():
        fid = opcion_a_fila.get(op)
        if fid and k not in con_anuncio and k not in con_web:
            personas[fid].add(k)
    todas = set().union(*personas.values()) if personas else set()
    citas: dict[str, int] = defaultdict(int)
    pids: dict[str, set] = defaultdict(set)
    for ph, pid, creada in c.execute("SELECT phone, id_paciente_medilink, created_at FROM citas_bot WHERE phone IS NOT NULL"):
        k = cm._clave(ph)
        if k not in todas:
            continue
        if pid:
            pids[k].add(int(pid))
        if creada and a <= creada < b:
            citas[k] += 1
    todos_pids = set().union(*pids.values()) if pids else set()
    venta_pid: dict[int, int] = defaultdict(int)
    primer: dict[int, str] = {}
    if todos_pids:
        lista = list(todos_pids)
        for i in range(0, len(lista), 500):
            lote = lista[i:i + 500]
            q = ",".join("?" * len(lote))
            for pid, f, m in c.execute(f"SELECT id_paciente, substr(fecha,1,10), monto FROM bi_pagos_caja WHERE id_paciente IN ({q}) "
                                       "AND monto > 0", lote):
                if not primer.get(pid) or f < primer[pid]:
                    primer[pid] = f
                if d <= f <= h:
                    venta_pid[pid] += int(m or 0)
    antiguas = sum(1 for k, (op, _) in resp.items() if op == "rrss" and k not in con_anuncio and k not in con_web)
    filas, n_nuevos, v_nuevos = [], 0, 0
    for fid, lbl, _ in DECLARADOS:
        ks = personas.get(fid, set())
        if fid == "qr" and not qr_existe:
            continue
        ps = set().union(*(pids[k] for k in ks)) if ks else set()
        nuevos = {p for p in ps if d <= primer.get(p, "9999") <= h}
        n_nuevos += len(nuevos)
        v_nuevos += sum(venta_pid[p] for p in nuevos)
        filas.append({"k": fid, "l": lbl, "personas": len(ks), "citas": sum(citas[k] for k in ks),
                      "atendidos": sum(1 for p in ps if venta_pid.get(p)), "venta": sum(venta_pid[p] for p in ps),
                      "nuevos": len(nuevos)})
    return {"filas": filas, "desde": PREGUNTA_DESDE, "qr_existe": qr_existe, "nuevos": n_nuevos, "venta_nuevos": v_nuevos,
            "opcion_antigua": antiguas}


def _nuevos_rango(d: str, h: str, meta_k: dict | None, web_k: dict | None) -> dict:
    """Pacientes nuevos del centro (primer pago de su historia en el rango) y
    cuántos no tienen canal registrado (ni anuncio de Meta ni página web)."""
    with db() as c:
        try:
            r = c.execute("SELECT COUNT(*), COALESCE(SUM(v),0) FROM (SELECT id_paciente, "
                          "SUM(CASE WHEN substr(fecha,1,10) BETWEEN ? AND ? THEN monto ELSE 0 END) v "
                          "FROM bi_pagos_caja WHERE id_paciente IS NOT NULL AND monto > 0 AND fecha IS NOT NULL "
                          "AND fecha != '' GROUP BY id_paciente HAVING MIN(substr(fecha,1,10)) BETWEEN ? AND ?)",
                          (d, h, d, h)).fetchone()
        except Exception:  # noqa: BLE001
            return {"hay": False}
        decl = _declarados(c, d, h)
    n, venta = int(r[0] or 0), int(r[1] or 0)
    vm = (meta_k or {}).get("valor12m") or {}
    vw = (web_k or {}).get("valor12m") or {}
    nm, nw = vm.get("nuevos_rango") or 0, vw.get("nuevos_rango") or 0
    venta_attr = (vm.get("nuevos_venta") or 0) + (vw.get("nuevos_venta") or 0) + decl["venta_nuevos"]
    return {"hay": True, "nuevos_centro": n, "venta_nuevos_centro": venta, "nuevos_meta": nm, "nuevos_web": nw,
            "nuevos_declarados": decl["nuevos"], "declarados": decl,
            "sin_canal": max(0, n - nm - nw - decl["nuevos"]), "venta_sin_canal": max(0, venta - venta_attr),
            "venta_aprox": True}


def captacion_data(hoy: date | None = None) -> dict:
    d, h = _rango(hoy)
    pm = _seguro("panel_meta", _panel, "meta", d, h)
    pw = _seguro("panel_web", _panel, "web", d, h)
    emb = {"rango": {"desde": d, "hasta": h}}
    if "error" not in pm:
        emb["meta"] = _k_embudo(pm["kpis"])
        emb["ultima_foto"] = pm.get("ultima_foto")
        emb["medicion"] = pm.get("medicion")
    if "error" not in pw:
        emb["web"] = _k_embudo(pw["kpis"])
    emb["plataformas"] = _seguro("plataformas", _plataformas, d, h)
    emb["meta_plataformas"] = _seguro("meta_plataformas", lambda: _cacheado(f"mplat:{d}:{h}", TTL_PESADO,
                                                                         lambda: _meta_plataformas(d, h)))
    emb["nuevos"] = _seguro("nuevos", lambda: _nuevos_rango(d, h, emb.get("meta"), emb.get("web")))
    ag = _seguro("agenda", _agenda)
    reglas = _seguro("reglas", lambda: _cacheado(f"reglas:{h}", TTL_PESADO,
                                                 lambda: __import__("radar_v2").reglas_presupuesto(ag, date.fromisoformat(h))))
    return {"rango": {"desde": d, "hasta": h},
            "reglas": reglas,
            "embudo": emb,
            "google": _seguro("google", _google, d, h),
            "creativos": _seguro("creativos", _creativos, pm) if "error" not in pm else pm,
            "territorio": _seguro("territorio", _territorio, d, h),
            "valor90": _seguro("valor90", _valor90),
            "valor12m": _seguro("valor12m", _valor12m),
            "agenda": ({k: ag.get(k) for k in ("sin_datos", "activo", "hoy", "dias", "grupos", "senales", "gasto",
                                               "desactualizado", "umbrales")}
                       | {"actualizado": _iso_epoch(ag.get("actualizado_ts"))}) if isinstance(ag, dict) and "error" not in ag else ag}


# ═══════════════════════════════════════════════════════════════════════════
# Conversión: recepción, recall, experimentos
# ═══════════════════════════════════════════════════════════════════════════

def _velocidad(d: str, h: str) -> dict:
    import campanas_meta_integraciones as ci

    def _f():
        v = ci.velocidad_data(d, h, canal="todos")["total"]
        return {k: v.get(k) for k in ("necesitaron", "mediana_min", "curva", "rapidos", "lentos",
                                       "citas_perdidas_estimadas", "sin_respuesta", "solo_bot", "proactivas")}
    return _cacheado(f"vel:{d}:{h}", TTL_PESADO, _f)


EXP_DIAS = 90


def _experimentos(hoy: date) -> dict:
    """E-01: reenganche con hora concreta vs sin hora (agendó ≤24 h después del
    mensaje). E-02: segundo toque (consultas_persistencia)."""
    desde = cm._utc_txt(cm._epoch_ini(hoy - timedelta(days=EXP_DIAS)))
    env: list[tuple[str, int, str]] = []
    with db() as c:
        for r in c.execute("SELECT phone, ts, meta FROM conversation_events WHERE event='reenganche_enviado' AND ts >= ?",
                           (desde,)):
            try:
                var = (json.loads(r[2] or "{}") or {}).get("variante") or ""
            except (ValueError, TypeError):
                var = ""
            env.append((r[0], cm._utc_txt_epoch(r[1]) or 0, var))
        citas: dict[str, list[int]] = defaultdict(list)
        for r in c.execute("SELECT phone, created_at FROM citas_bot WHERE created_at >= ?", (desde,)):
            citas[r[0]].append(cm._utc_txt_epoch(r[1]) or 0)
        persist = None
        try:
            persist = {r[0]: r[1] for r in c.execute(
                "SELECT estado, COUNT(*) FROM consultas_persistencia WHERE opened_at >= ? GROUP BY estado",
                (cm._utc_txt(cm._epoch_ini(hoy - timedelta(days=30))),))}
        except Exception:
            persist = None
    grupos = {"con_hora": [0, 0], "sin_hora": [0, 0]}
    for ph, ts, var in env:
        g = "con_hora" if var.endswith("_con_hora") else "sin_hora" if var.endswith("_sin_hora") else None
        if not g:
            continue
        grupos[g][0] += 1
        if any(ts <= x <= ts + 86400 for x in citas.get(ph, ())):
            grupos[g][1] += 1

    def _ic(n, k):
        """Intervalo de Wilson 95% en puntos porcentuales."""
        if not n:
            return None
        z = 1.96
        p = k / n
        den = 1 + z * z / n
        cen = (p + z * z / (2 * n)) / den
        m = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / den
        return [round(100 * max(0, cen - m), 1), round(100 * min(1, cen + m), 1)]
    e01 = {k: {"enviados": n, "agendaron": a, "pct": round(100 * a / n, 1) if n else None, "ic95": _ic(n, a)}
           for k, (n, a) in grupos.items()}
    tot_p = sum((persist or {}).values())
    return {"dias": EXP_DIAS, "reenganche": e01,
            "persistencia": {"por_estado": persist, "total": tot_p,
                             "agendadas": (persist or {}).get("agendada", 0),
                             "pct": round(100 * (persist or {}).get("agendada", 0) / tot_p, 1) if tot_p else None}
            if persist is not None else None}


def conversion_data(hoy: date | None = None) -> dict:
    hoy = hoy or _hoy()
    d, h = _rango(hoy)
    vivo = _seguro("vivo", lambda: _cacheado(f"vivo:{hoy}", TTL_PULSO, lambda: _vivo(_ahora())))
    v90 = _seguro("valor90", _valor90)
    return {"rango": {"desde": d, "hasta": h},
            "velocidad": _seguro("velocidad", _velocidad, d, h),
            "velocidad_horario": _seguro("velocidad_horario", lambda: _cacheado(
                f"velh:{d}:{h}", TTL_PESADO, lambda: __import__("radar_v2").velocidad_en_horario(d, h))),
            "holdout": _seguro("holdout", lambda: _cacheado(f"hold:{hoy}", TTL_PESADO,
                                                            lambda: __import__("radar_v2").holdout_data(hoy))),
            "esperando": {"lista": vivo.get("esperando", []), "total": vivo.get("esperando_total", 0),
                          "max_min": vivo.get("esperando_max_min")} if "error" not in vivo else vivo,
            "experimentos": _seguro("experimentos", lambda: _cacheado(f"exp:{hoy}", TTL_PESADO, lambda: _experimentos(hoy))),
            "volvieron": ({"pct": v90["total"].get("volvieron_pct"), "pacientes": v90["total"].get("pacientes_maduros"),
                           "volvieron": v90["total"].get("volvieron"), "madura_hasta": v90.get("madura_hasta"),
                           "desde": v90.get("desde")} if "error" not in v90 else v90)}


# ═══════════════════════════════════════════════════════════════════════════
# Finanzas
# ═══════════════════════════════════════════════════════════════════════════

MESES_SERIE = 7


def _meses(hoy: date) -> dict:
    m0 = hoy.year * 12 + hoy.month - 1 - (MESES_SERIE - 1)
    ini = date(m0 // 12, m0 % 12 + 1, 1)
    with db() as c:
        pct = cm._pct_honorarios(c)
        filas = c.execute("SELECT substr(fecha,1,7), id_profesional, SUM(monto) FROM bi_pagos_caja "
                          "WHERE fecha >= ? AND fecha <= ? GROUP BY 1, 2",
                          (ini.isoformat(), hoy.isoformat() + " 23:59:59")).fetchall()
        mes_ant = date(hoy.year - (hoy.month == 1), (hoy.month - 2) % 12 + 1, 1)
        # Medio de pago real = módulo Pagos (pagos_cmc, lo que registra recepción);
        # bi_pagos_caja trae todo como "Efectivo". Lo que falta para llegar a la caja
        # es la parte que paga Fonasa (ver memoria "las 2 fuentes").
        try:
            medios = c.execute("SELECT lower(COALESCE(NULLIF(trim(metodo_pago),''),'sin dato')), SUM(copago) FROM pagos_cmc "
                               "WHERE substr(fecha,1,7) = ? GROUP BY 1 HAVING SUM(copago) > 0 ORDER BY 2 DESC",
                               (mes_ant.isoformat()[:7],)).fetchall()
        except Exception:  # noqa: BLE001
            medios = []
        caja_mes = c.execute("SELECT SUM(monto) FROM bi_pagos_caja WHERE substr(fecha,1,7) = ?",
                             (mes_ant.isoformat()[:7],)).fetchone()[0] or 0
        ult = c.execute("SELECT MAX(fecha), MAX(synced_at) FROM bi_pagos_caja WHERE fecha <= ?",
                        (hoy.isoformat() + " 23:59:59",)).fetchone()
    acc: dict[str, dict] = {}
    for mes, prof, monto in filas:
        a = acc.setdefault(mes, {"mes": mes, "venta": 0, "centro": 0.0})
        a["venta"] += int(monto or 0)
        a["centro"] += (monto or 0) * (100 - (pct.get(prof) or cm.PCT_HONORARIO_DEFAULT)) / 100
    serie = []
    for i in range(MESES_SERIE):
        m = m0 + i
        k = f"{m // 12:04d}-{m % 12 + 1:02d}"
        a = acc.get(k, {"mes": k, "venta": 0, "centro": 0.0})
        serie.append({"mes": k, "venta": a["venta"], "centro": round(a["centro"]),
                      "centro_pct": round(100 * a["centro"] / a["venta"]) if a["venta"] else None})
    actual = serie[-1]
    import calendar
    dias_mes = calendar.monthrange(hoy.year, hoy.month)[1]
    ult_fecha = (ult[0] or "")[:10] if ult else ""
    dias_con_caja = (min(hoy, date.fromisoformat(ult_fecha)) - hoy.replace(day=1)).days + 1 \
        if ult_fecha and ult_fecha >= hoy.replace(day=1).isoformat() else 0
    proy = round(actual["venta"] / dias_con_caja * dias_mes) if dias_con_caja >= 3 else None
    _lbl = {"transferencia": "Transferencia", "efectivo": "Efectivo", "debito": "Débito", "débito": "Débito",
            "credito": "Crédito", "crédito": "Crédito", "bono_web": "Bono web", "sin dato": "Sin dato"}
    pagado = sum(int(r[1] or 0) for r in medios)
    fonasa = max(0, int(caja_mes) - pagado) if pagado else 0
    tot_m = pagado + fonasa
    medios_out = []
    for nombre, monto in medios[:5]:
        medios_out.append({"medio": _lbl.get(nombre, nombre.capitalize()), "venta": int(monto or 0),
                           "pct": round(100 * (monto or 0) / tot_m) if tot_m else 0})
    resto = pagado - sum(x["venta"] for x in medios_out)
    if resto > 0:
        medios_out.append({"medio": "Otros", "venta": resto, "pct": round(100 * resto / tot_m)})
    if fonasa:
        medios_out.append({"medio": "Fonasa (bonificación)", "venta": fonasa, "pct": round(100 * fonasa / tot_m)})
    anterior = serie[-2]
    return {"serie": serie, "actual": actual, "anterior": anterior,
            "proyeccion": proy, "dias_con_caja": dias_con_caja, "dias_mes": dias_mes,
            "medios": {"mes": mes_ant.isoformat()[:7], "lista": medios_out},
            "caja_hasta": ult_fecha or None, "sincronizado": ult[1] if ult else None}


def finanzas_data(hoy: date | None = None) -> dict:
    hoy = hoy or _hoy()
    d, h = _rango(hoy)
    pm = _seguro("panel_meta", _panel, "meta", d, h)
    pw = _seguro("panel_web", _panel, "web", d, h)
    v90 = _seguro("valor90", _valor90)
    canales = []
    if "error" not in pm:
        canales.append({"canal": "meta", **_k_embudo(pm["kpis"])})
    if "error" not in pw:
        canales.append({"canal": "web", **_k_embudo(pw["kpis"])})
    return {"rango": {"desde": d, "hasta": h},
            "meses": _seguro("meses", lambda: _cacheado(f"meses:{hoy}", TTL_PESADO, lambda: _meses(hoy))),
            "canales": canales,
            "metas": _seguro("metas", lambda: _cacheado(f"metas:{hoy}", TTL_PESADO,
                                                        lambda: __import__("radar_v2").metas_data(hoy))),
            "puente": _seguro("puente", lambda: _cacheado(f"puente:{hoy}", TTL_PESADO,
                                                          lambda: __import__("radar_v2").puente_data(hoy))),
            "valor90": ({"total": v90["total"], "madura_hasta": v90["madura_hasta"], "desde": v90["desde"]}
                        if "error" not in v90 else v90)}


# ═══════════════════════════════════════════════════════════════════════════
# Bitácora y consentimiento
# ═══════════════════════════════════════════════════════════════════════════

def _ep_de_iso_cl(s: str | None) -> int | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_CL)
        return int(dt.timestamp())
    except ValueError:
        return None


def _respaldo() -> dict | None:
    p = Path(BACKUP_DIR)
    try:
        if not p.is_dir():
            return None
        arch = [f for f in p.iterdir() if f.is_file() and f.name.startswith("sessions_")]
    except OSError:
        return None
    if not arch:
        return None
    ult = max(arch, key=lambda f: f.stat().st_mtime)
    return {"ts": int(ult.stat().st_mtime), "copias": len(arch)}


def _noche(ahora: datetime) -> dict:
    """Lo que el sistema hizo solo, según el rastro que dejó cada tarea."""
    import campanas_meta_integraciones as ci
    ev: list[dict] = []
    sin_rastro: list[str] = []
    with db() as c:
        def _t(n):
            return cm._tabla_existe(c, n)
        if _t("capi_purchase_corridas"):
            r = c.execute("SELECT fecha_corrida, enviados, diferidos, errores, value_total, estimados FROM "
                          "capi_purchase_corridas ORDER BY id DESC LIMIT 1").fetchone()
            if r:
                ev.append({"ts": _ep_de_iso_cl(r[0]), "clave": "capi",
                           "titulo": f"Avisó a Meta {r[1] or 0} {'atenciones' if (r[1] or 0) != 1 else 'atención'} con su valor real",
                           "detalle": "Evento de compra con el margen del centro de la caja de ese día; sin diagnóstico ni datos clínicos."
                                      + (f" {r[2]} quedaron para la próxima corrida." if r[2] else "")
                                      + (f" {r[3]} con error." if r[3] else ""),
                           "estado": "error" if (r[3] or 0) else "ok", "valor": round(r[4] or 0)})
        else:
            sin_rastro.append("Aviso a Meta (07:07)")
        if _t("meta_insights_diario"):
            r = c.execute("SELECT MAX(fecha), MAX(actualizado_ts), COUNT(DISTINCT ad_id) FROM meta_insights_diario "
                          "WHERE desglose='total' AND fecha = (SELECT MAX(fecha) FROM meta_insights_diario WHERE desglose='total')").fetchone()
            if r and r[0]:
                ev.append({"ts": r[1], "clave": "insights", "titulo": "Guardó la foto diaria de los anuncios de Meta",
                           "detalle": f"Último día con datos: {r[0]} · {r[2]} anuncio{'s' if r[2] != 1 else ''}.",
                           "estado": "ok"})
        if _t("gsc_paginas_diario"):
            r = c.execute("SELECT MAX(fecha), MAX(actualizado_ts) FROM gsc_paginas_diario").fetchone()
            if r and r[0]:
                tot = c.execute("SELECT COALESCE(SUM(clicks),0), COALESCE(SUM(impressions),0) FROM gsc_paginas_diario "
                                "WHERE fecha=? AND basura=0", (r[0],)).fetchone()
                ev.append({"ts": r[1], "clave": "gsc", "titulo": "Trajo los datos de Google Search Console",
                           "detalle": f"Día {r[0]}: {tot[0]} clics y {tot[1]} apariciones.", "estado": "ok"})
        if _t("agenda_cupos_estado"):
            r = ci._get_estado(c, "ultima_corrida")
            if r:
                ev.append({"ts": r.get("ts"), "clave": "agenda", "titulo": "Leyó los cupos libres de la agenda (14 días)",
                           "detalle": f"{r.get('consultas', 0)} consultas a Medilink en carril lento"
                                      + (f", {r['errores']} con error" if r.get("errores") else "")
                                      + (f"; se cortó por {r['corte']}" if r.get("corte") else "") + ".",
                           "estado": "error" if r.get("corte") else "ok"})
        if _t("meta_creativos"):
            r = c.execute("SELECT MAX(actualizado_ts), COUNT(*), SUM(CASE WHEN archivo IS NOT NULL AND archivo != '' THEN 1 ELSE 0 END) "
                          "FROM meta_creativos").fetchone()
            if r and r[0]:
                ev.append({"ts": r[0], "clave": "creativos", "titulo": "Actualizó texto e imagen de los anuncios",
                           "detalle": f"{r[1]} anuncios, {r[2] or 0} con imagen guardada en el servidor.", "estado": "ok"})
        try:
            r = c.execute("SELECT ts, meta FROM conversation_events WHERE event='centinela_diario' ORDER BY id DESC LIMIT 1").fetchone()
        except Exception:
            r = None
        if r:
            try:
                h_ = (json.loads(r[1] or "{}") or {}).get("hallazgos", 0)
            except (ValueError, TypeError):
                h_ = 0
            ev.append({"ts": cm._utc_txt_epoch(r[0]), "clave": "centinela", "titulo": "Centinela: revisó webhook, abonos y agenda",
                       "detalle": "Sin hallazgos." if not h_ else f"{h_} hallazgo{'s' if h_ != 1 else ''} enviados al dueño.",
                       "estado": "ok" if not h_ else "aviso"})
        else:
            sin_rastro.append("Centinela (07:30)")
        if _t("bi_sync_log"):
            corte = datetime.fromtimestamp(ahora.timestamp() - 86400, timezone.utc).replace(tzinfo=None).isoformat()
            r = c.execute("SELECT MAX(fin), SUM(n_registros), SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END) FROM bi_sync_log "
                          "WHERE fin >= ?", (corte,)).fetchone()
            if r and r[0]:
                ev.append({"ts": cm._utc_txt_epoch((r[0] or "").replace("T", " ")), "clave": "caja", "titulo": "Sincronizó la caja de Medilink",
                           "detalle": f"{r[1] or 0} registros en las últimas 24 h" + (f"; {r[2]} tramo(s) con error." if r[2] else "."),
                           "estado": "error" if r[2] else "ok"})
    rs = _respaldo()
    if rs:
        ev.append({"ts": rs["ts"], "clave": "respaldo", "titulo": "Respaldo cifrado de la base",
                   "detalle": f"{rs['copias']} copia{'s' if rs['copias'] != 1 else ''} local{'es' if rs['copias'] != 1 else ''}; la historia larga vive fuera del servidor.",
                   "estado": "ok"})
    else:
        sin_rastro.append("Respaldo de la base (no visible desde este servidor)")
    ahora_ep = int(ahora.timestamp())
    for e in ev:
        e["cuando"] = _iso_epoch(e.get("ts"))
        e["horas"] = round((ahora_ep - e["ts"]) / 3600, 1) if e.get("ts") else None
        if e["horas"] is not None and e["horas"] > 36 and e["estado"] == "ok":
            e["estado"] = "atrasado"
        e.pop("ts", None)
    ev.sort(key=lambda e: e["cuando"] or "", reverse=True)
    return {"eventos": ev, "sin_rastro": sin_rastro}


CONSENT_DIAS = 30


def _consentimiento(hoy: date) -> dict:
    desde = cm._utc_txt(cm._epoch_ini(hoy - timedelta(days=CONSENT_DIAS - 1)))
    acc = {"accepted": 0, "declined": 0}
    por_dia: dict[str, dict] = {}
    with db() as c:
        try:
            filas = c.execute("SELECT ts, meta FROM conversation_events WHERE event='marketing_consent_respuesta' AND ts >= ?",
                              (desde,)).fetchall()
        except Exception:
            filas = []
        try:
            priv = {r[0]: r[1] for r in c.execute("SELECT status, COUNT(*) FROM privacy_consents GROUP BY status")}
        except Exception:
            priv = {}
    for ts, meta in filas:
        try:
            st = (json.loads(meta or "{}") or {}).get("status")
        except (ValueError, TypeError):
            st = None
        if st not in acc:
            continue
        acc[st] += 1
        t = _ts_cl(ts)
        if t:
            d = por_dia.setdefault(t.date().isoformat(), {"fecha": t.date().isoformat(), "accepted": 0, "declined": 0})
            d[st] += 1
    dias = sorted(por_dia.values(), key=lambda x: x["fecha"], reverse=True)[:7]
    return {"dias": CONSENT_DIAS, "marketing": acc, "por_dia": dias,
            "privacidad": {"accepted": priv.get("accepted", 0), "declined": priv.get("declined", 0),
                           "pending": priv.get("pending", 0)}}


def _iniciales_resena(nombre: str) -> str:
    partes = [x for x in (nombre or "").replace(".", " ").split() if x[:1].isalpha()]
    return ".".join(x[0].upper() for x in partes[:2]) + "." if partes else "—"


def reputacion_data() -> dict:
    """Nota de Google (última buena guardada; nunca sale a la red) + encuesta
    postconsulta mejor/igual/peor (fidelizacion_msgs). Sin teléfonos ni nombres."""
    def _google():
        import google_rating as gr
        d = gr.cached_rating()
        if not d or not d.get("rating"):
            return {"hay": False}
        revs = []
        for r in (d.get("reviews") or [])[:6]:
            revs.append({"iniciales": _iniciales_resena(r.get("author") or r.get("autor") or r.get("name") or ""),
                         "estrellas": r.get("rating"), "texto": (r.get("text") or r.get("texto") or "")[:400],
                         "cuando": r.get("relative") or r.get("relative_time") or r.get("cuando") or ""})
        return {"hay": True, "rating": d.get("rating"), "total": d.get("review_count"),
                "actualizado": _iso_epoch(d.get("updated_at")), "resenas": revs, "link": gr.get_review_link()}

    def _encuesta():
        from session import get_nps_por_profesional
        out = {}
        for dias in (30, 90):
            n = get_nps_por_profesional(dias)
            out[str(dias)] = {"indice": n["global_nps"], "total": n["global_total"], "mejor": n["global_mejor"],
                              "igual": n["global_igual"], "peor": n["global_peor"],
                              "por_profesional": [{"profesional": x.get("profesional") or "Sin dato", "total": x["total"],
                                                   "mejor": x["mejor"], "igual": x["igual"], "peor": x["peor"],
                                                   "indice": x["nps"]} for x in n["por_profesional"]]}
        return out

    def _temas():
        import opinion_temas
        with db() as c:
            return opinion_temas.temas_data(c)

    return {"google": _seguro("google", _google),
            "encuesta": _seguro("encuesta", lambda: _cacheado("encuesta", 300, _encuesta)),
            "temas": _seguro("temas", _temas)}


def bitacora_data(ahora: datetime | None = None) -> dict:
    ahora = ahora or _ahora()
    import campanas_meta_integraciones as ci
    av = _seguro("aviso", ci.aviso_meta_data)
    if isinstance(av, dict) and "error" not in av:
        av = {k: av.get(k) for k in ("hay_datos", "ventana_dias", "aceptados", "aceptados_reales", "aceptados_estimados",
                                     "valor_real", "valor_estimado", "duplicados_omitidos", "pendientes", "errores")}
    return {"ahora": ahora.isoformat(timespec="minutes"),
            "noche": _seguro("noche", lambda: _cacheado(f"noche:{ahora:%Y-%m-%dT%H}", 60, lambda: _noche(ahora))),
            "aviso_meta": av,
            "consentimiento": _seguro("consent", lambda: _cacheado(f"consent:{ahora.date()}", TTL_PESADO,
                                                                  lambda: _consentimiento(ahora.date())))}


# ═══════════════════════════════════════════════════════════════════════════
# Rutas
# ═══════════════════════════════════════════════════════════════════════════

_NO_STORE = {"Cache-Control": "no-store"}


def _json(data):
    from fastapi.responses import JSONResponse
    return JSONResponse(data, headers=_NO_STORE)


@router.get("/pulso")
def api_pulso(request: Request, token: str | None = Query(None)):
    cm._auth(request, token)
    return _json(pulso_data())


@router.get("/captacion")
def api_captacion(request: Request, token: str | None = Query(None)):
    cm._auth(request, token)
    return _json(captacion_data())


@router.get("/conversion")
def api_conversion(request: Request, token: str | None = Query(None)):
    cm._auth(request, token)
    return _json(conversion_data())


@router.get("/finanzas")
def api_finanzas(request: Request, token: str | None = Query(None)):
    cm._auth(request, token)
    return _json(finanzas_data())


@router.get("/reputacion")
def api_reputacion(request: Request, token: str | None = Query(None)):
    cm._auth(request, token)
    return _json(reputacion_data())


@router.get("/bitacora")
def api_bitacora(request: Request, token: str | None = Query(None)):
    cm._auth(request, token)
    return _json(bitacora_data())


@pagina.get("/alma/radar", response_class=HTMLResponse, include_in_schema=False)
def radar_page(request: Request, token: str | None = Query(None)):
    """Página. Mismo patrón que /alma/campanas-meta: SOLO OLACORE_TOKEN, 404 en
    el dominio clínico. El HTML lee el token de su propia URL (`_tk`)."""
    host = (request.headers.get("host") or "").split(":")[0].lower()
    if host.endswith("centromedicocarampangue.cl"):
        raise HTTPException(404, "Not found")
    if not _TEMPLATE.exists():
        raise HTTPException(404, "Alma Radar no disponible")
    if not token:
        raise HTTPException(401, "Falta el token")
    if not cm.token_dueno(token):
        raise HTTPException(403, "Solo el token del dueño abre Alma Radar")
    return HTMLResponse(_TEMPLATE.read_text(encoding="utf-8"), headers=_NO_STORE)


# Portada, puente de resultado, centro de datos y laboratorio (rutas en este mismo router).
import radar_v2  # noqa: E402,F401
