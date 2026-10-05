# -*- coding: utf-8 -*-
"""Router /alma/api/campanas-meta — Campañas Meta: gasto → conversación → cita → atención.

POR QUE EXISTE
--------------
Desde el 4-may-2026 cada cita que agenda el bot queda amarrada al anuncio que
trajo a la persona (`citas_bot.ad_source_id`, último clic ≤90 días), y desde el
5-oct-2026 sabemos además si el clic vino de Facebook o de Instagram
(`meta_referrals.plataforma`). La foto diaria de Meta Ads
(`meta_insights_diario`) pone el gasto. Con las tres cosas se puede, por
primera vez, decir cuánto cuesta una CITA y una ATENCIÓN por anuncio — no solo
una conversación, que es lo único que muestra el Administrador de anuncios.

Dos vistas en una página (`templates/alma_campanas_meta.html`):

  1. Panel  — KPIs, tabla por campaña/anuncio ordenada por CAC, desgloses de
              Meta (solo hasta conversación), tendencia mensual y alertas.
  2. Kanban — una tarjeta por persona que llegó por un anuncio, en la columna
              del momento en que está con la agenda. Se calcula solo.

QUE ES MEDIDO Y QUE ES PROXY (la UI lo dice tal cual)
------------------------------------------------------
- Gasto, impresiones, alcance, conversaciones: Meta (foto diaria).
- Personas captadas: teléfonos únicos en `meta_referrals` (medido por el bot).
- Citas: citas del bot atribuidas a un anuncio, creadas en el rango. Se cuenta
  una por (persona, especialidad) para que un reagendamiento no sume dos.
- Atendidos: la cita tiene aviso CAPI `Purchase` (`conversation_events`
  `capi_send_ok`). Ese aviso lo emite el seguimiento post-consulta cuando la
  hora ya pasó y no fue anulada: es un PROXY de atención, no un registro de
  caja. Una inasistencia no anulada hoy cuenta como atendida.
- Venta: lo cobrado en caja (`bi_pagos_caja`, espejo de Medilink) a cada
  paciente desde el día en que agendó la cita atribuida, hasta hoy. Cada
  paciente cuenta UNA vez, en el anuncio de su primera cita del rango, para
  que la suma por anuncio cuadre con el total. Incluye todo lo que pagó
  después (controles, otras especialidades): es la venta que trajo el anuncio,
  no solo la primera consulta. Si el paciente ya era del centro, sus pagos
  posteriores igual se cuentan — la UI lo advierte.
- Para el centro: cada pago × (1 − pct_honorario/100) del profesional que lo
  atendió (`equipo_cmc.pct_honorario` = lo que se lleva el PROFESIONAL, NO el
  margen — ver guardrail). Sin pct cargado → 70% de referencia. Abarca (73)
  tiene sueldo fijo: se usa 62%, la proporción equivalente de su contrato.
  Resultado = para el centro − gasto en el anuncio.
- Frecuencia: la foto es diaria, y el alcance de días distintos no se puede
  sumar sin contar dos veces a la misma persona. Se muestra
  impresiones ÷ suma del alcance diario, que es un PISO de la frecuencia real
  del rango. Si el piso pasa de 6, el anuncio está saturado con seguridad.

Auth: SOLO el token del dueño (OLACORE_TOKEN). Muestra gasto publicitario: el
token de recepción (ADMIN_TOKEN) NO entra, y la cookie `admin` tampoco, porque
la comparten recepción y dueño.
"""
from __future__ import annotations

import hmac
import json
import logging
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query, Request

from session import db

log = logging.getLogger("campanas_meta_routes")
router = APIRouter(prefix="/alma/api/campanas-meta", tags=["campanas-meta"])

_CL = ZoneInfo("America/Santiago")

# Fechas desde las que existe cada dato (para que la UI no prometa de más).
ATRIBUCION_DESDE = "2026-05-04"   # citas_bot.ad_source_id
PLATAFORMA_DESDE = "2026-10-05"   # meta_referrals.plataforma por persona

UMBRAL_GASTO_SIN_CITAS = 50_000   # CLP en el rango y 0 citas → alerta
UMBRAL_FRECUENCIA = 6.0           # piso de frecuencia sobre esto → "saturado"
MUESTRA_CHICA = 5                 # menos citas que esto → CAC poco confiable
DIAS_PERDIDO = 7                  # sin respuesta más de esto y sin cita → perdido

_EV_SLOTS = ("funnel_slot_ofrecido", "ctwa_slots_ofrecidos")
_PLATS = ("facebook", "instagram")

ETAPAS = [
    {"id": "escribio",  "label": "Escribió, sin ver horas",
     "ayuda": "Llegó por el anuncio y conversó, pero el bot aún no le ofreció horarios."},
    {"id": "vio_horas", "label": "Vio horarios, no agendó",
     "ayuda": "El bot le ofreció horas concretas y no tomó ninguna."},
    {"id": "agendado",  "label": "Agendado",
     "ayuda": "Tiene una cita futura vigente."},
    {"id": "atendido",  "label": "Atendido",
     "ayuda": "Aviso de atención (CAPI Purchase) o cita ya pasada y no anulada."},
    {"id": "anulo",     "label": "Anuló / no asistió",
     "ayuda": "Su cita fue anulada y no tiene otra vigente. Las inasistencias aún no llegan desde Medilink."},
    {"id": "perdido",   "label": "Sin respuesta > 7 días",
     "ayuda": "Sin cita y sin escribir hace más de 7 días."},
]
_ETAPA_IDS = [e["id"] for e in ETAPAS]


# ── Auth ────────────────────────────────────────────────────────────────────

def token_dueno(token: str | None) -> bool:
    from config import OLACORE_TOKEN
    return bool(token) and bool(OLACORE_TOKEN) and hmac.compare_digest(token, OLACORE_TOKEN)


def _auth(request: Request, token: str | None) -> None:
    auth = request.headers.get("authorization", "") or ""
    tk = auth.split(None, 1)[1].strip() if auth.lower().startswith("bearer ") else token
    if not tk:
        raise HTTPException(401, "Falta el token")
    if not token_dueno(tk):
        raise HTTPException(403, "Solo el token del dueño abre Campañas Meta")


# ── Utilidades ──────────────────────────────────────────────────────────────

def _hoy() -> date:
    return datetime.now(_CL).date()


def _rango(desde: str | None, hasta: str | None) -> tuple[date, date]:
    def _p(s):
        try:
            return datetime.strptime((s or "")[:10], "%Y-%m-%d").date()
        except ValueError:
            return None
    h = _p(hasta) or _hoy()
    d = _p(desde) or (h - timedelta(days=29))
    if d > h:
        d, h = h, d
    if (h - d).days > 731:
        d = h - timedelta(days=731)
    return d, h


def _epoch_ini(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=_CL).timestamp())


def _epoch_fin(d: date) -> int:
    """Epoch del inicio del día SIGUIENTE (rango semiabierto)."""
    n = d + timedelta(days=1)
    return int(datetime(n.year, n.month, n.day, tzinfo=_CL).timestamp())


def _utc_txt_epoch(s: str | None) -> int | None:
    if not s:
        return None
    try:
        return int(datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
                   .replace(tzinfo=timezone.utc).timestamp())
    except ValueError:
        try:
            return int(datetime.strptime(s[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            return None


def _utc_txt(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _clave(phone: str | None) -> str:
    """Identidad de persona: últimos 9 dígitos (mismo criterio que la
    atribución en session.ultimo_referral_antes_de). fb_/ig_ quedan tal cual."""
    p = (phone or "").strip()
    dig = "".join(ch for ch in p if ch.isdigit())
    if len(dig) >= 9 and not p.startswith(("fb_", "ig_")):
        return dig[-9:]
    return p


def _mascara(phone: str | None) -> str:
    p = (phone or "").strip()
    if p.startswith("fb_"):
        return "Messenger"
    if p.startswith("ig_"):
        return "Instagram Direct"
    dig = "".join(ch for ch in p if ch.isdigit())
    if len(dig) >= 9:
        return f"+56 9 •••• {dig[-4:]}"
    return "••••" + dig[-4:] if dig else "—"


def _plat(p: str | None) -> str:
    p = (p or "").strip().lower()
    return p if p in _PLATS else "sin_dato"


def _div(a: float, b: float) -> float | None:
    return round(a / b) if b else None


def _ensure_insights(c) -> None:
    try:
        import meta_insights_snapshot as _mis
        _mis._ensure_table(c)
    except Exception as e:  # pragma: no cover — la tabla la crea el snapshot
        log.debug("campanas_meta: no se pudo asegurar meta_insights_diario: %s", e)


def _mapa_anuncios(c) -> dict[str, dict]:
    """ad_id → nombre/campaña, desde la foto de Meta (la fila más reciente)."""
    _ensure_insights(c)
    out = {}
    for r in c.execute(
            "SELECT ad_id, ad_name, campaign_id, campaign_name, adset_name, MAX(fecha) "
            "FROM meta_insights_diario WHERE desglose='total' GROUP BY ad_id"):
        out[r[0]] = {"ad_name": r[1] or "", "campaign_id": r[2] or "",
                     "campaign_name": r[3] or "", "adset_name": r[4] or ""}
    return out


def _info_ad(mapa: dict, ad_id: str, headline: str = "") -> dict:
    m = mapa.get(ad_id or "")
    if m:
        return {"ad_id": ad_id, "anuncio": m["ad_name"] or headline or ad_id,
                "campaign_id": m["campaign_id"], "campana": m["campaign_name"] or "Sin nombre"}
    return {"ad_id": ad_id or "", "anuncio": headline or (f"Anuncio {ad_id[-6:]}" if ad_id else "Sin anuncio"),
            "campaign_id": "", "campana": "Sin campaña identificada"}


def _pasa_filtros(info: dict, plataforma: str, campana: str | None, plat_filtro: str | None,
                  anuncio: str | None = None) -> bool:
    if campana and info["campaign_id"] != campana:
        return False
    if anuncio and info["ad_id"] != anuncio:
        return False
    if plat_filtro and _plat(plataforma) != plat_filtro:
        return False
    return True


def _purchases(c, desde_epoch: int) -> dict[str, list[int]]:
    """clave de persona → epochs de avisos CAPI Purchase."""
    out: dict[str, list[int]] = defaultdict(list)
    for r in c.execute(
            "SELECT phone, ts, meta FROM conversation_events WHERE event='capi_send_ok' "
            "AND ts >= ? AND meta LIKE '%Purchase%'", (_utc_txt(desde_epoch),)):
        try:
            if json.loads(r[2] or "{}").get("event_type") != "Purchase":
                continue
        except (ValueError, TypeError):
            continue
        e = _utc_txt_epoch(r[1])
        if e:
            out[_clave(r[0])].append(e)
    return out


def _cancelada(cita: dict) -> bool:
    return bool(cita.get("cancel_detected_at")) or (cita.get("confirmation_status") or "") == "cancelar"


def _citas_atribuidas(c, desde_epoch: int, hasta_epoch: int, mapa: dict,
                      campana: str | None, plat: str | None) -> list[dict]:
    """Citas del bot atribuidas a un anuncio y creadas en el rango, una por
    (persona, especialidad) — la primera — para no contar reagendamientos."""
    filas = c.execute(
        "SELECT phone, id_cita, especialidad, fecha, hora, created_at, cancel_detected_at, "
        "confirmation_status, ad_source_id, ad_headline, ad_plataforma, id_paciente_medilink FROM citas_bot "
        "WHERE ad_source_id IS NOT NULL AND ad_source_id != '' AND created_at >= ? AND created_at < ? "
        "ORDER BY created_at", (_utc_txt(desde_epoch), _utc_txt(hasta_epoch))).fetchall()
    vistas, out = set(), []
    for r in filas:
        d = dict(r)
        k = (_clave(d["phone"]), (d["especialidad"] or "").strip().lower())
        if k in vistas:
            continue
        info = _info_ad(mapa, d["ad_source_id"], d["ad_headline"] or "")
        if not _pasa_filtros(info, d["ad_plataforma"], campana, plat):
            continue
        vistas.add(k)
        d.update(info)
        d["clave"] = k[0]
        d["created_epoch"] = _utc_txt_epoch(d["created_at"]) or 0
        out.append(d)
    return out


PCT_HONORARIO_DEFAULT = 70
PCT_HONORARIO_FIJO = {73: 62}   # Abarca: sueldo fijo → proporción equivalente del contrato


def _pct_honorarios(c) -> dict[int, int]:
    """% que se lleva el PROFESIONAL por id_medilink (no es el margen)."""
    try:
        m = {r["id_medilink"]: int(r["pct_honorario"] or 0)
             for r in c.execute("SELECT id_medilink, pct_honorario FROM equipo_cmc "
                                "WHERE id_medilink IS NOT NULL")}
    except Exception:
        m = {}
    m.update(PCT_HONORARIO_FIJO)
    return m


def _venta_por_anuncio(c, citas: list[dict]) -> dict[str, tuple[int, float]]:
    """(venta, para el centro) cobrado en caja a cada paciente desde el día de
    su primera cita atribuida (citas viene ordenado por created_at). Un
    paciente → un anuncio."""
    inicio: dict[int, tuple[str, str]] = {}
    for ci in citas:
        pid = ci.get("id_paciente_medilink")
        if pid and pid not in inicio:
            dia = datetime.fromtimestamp(ci["created_epoch"], _CL).date().isoformat()
            inicio[pid] = (ci["ad_id"], dia)
    out: dict[str, list] = defaultdict(lambda: [0, 0.0])
    pct = _pct_honorarios(c)
    ids = list(inicio)
    for i in range(0, len(ids), 500):
        lote = ids[i:i + 500]
        q = ("SELECT id_paciente, id_profesional, fecha, monto FROM bi_pagos_caja WHERE id_paciente IN (%s)"
             % ",".join("?" * len(lote)))
        try:
            filas = c.execute(q, lote).fetchall()
        except Exception as e:  # tabla ausente (entorno sin BI) → sin venta, no rompe el panel
            log.warning("campanas_meta: venta no disponible: %s", e)
            return {}
        for r in filas:
            ad_id, dia = inicio[r["id_paciente"]]
            if (r["fecha"] or "")[:10] >= dia:
                monto = int(r["monto"] or 0)
                p_prof = pct.get(r["id_profesional"]) or PCT_HONORARIO_DEFAULT
                out[ad_id][0] += monto
                out[ad_id][1] += monto * (100 - p_prof) / 100
    return {k: (v[0], v[1]) for k, v in out.items()}


# ── Panel ───────────────────────────────────────────────────────────────────

def _insights_filas(c, d: date, h: date, campana: str | None, plat: str | None,
                    desglose: str = "total") -> list[dict]:
    """Filas de la foto diaria. Con filtro de plataforma, el total sale del
    desglose 'plataforma' (Meta no deja filtrar el total)."""
    q = ("SELECT fecha, ad_id, valor, campaign_id, campaign_name, ad_name, spend, impressions, "
         "reach, frequency, clicks, conversaciones FROM meta_insights_diario "
         "WHERE fecha >= ? AND fecha <= ? AND desglose = ?")
    args: list = [d.isoformat(), h.isoformat()]
    if desglose == "total" and plat in _PLATS:
        args.append("plataforma")
        q += " AND valor = ?"
        args.append(plat)
    elif desglose == "ubicacion" and plat in _PLATS:
        args.append("ubicacion")
        q += " AND valor LIKE ?"
        args.append(plat + "|%")
    else:
        args.append(desglose)
    if campana:
        q += " AND campaign_id = ?"
        args.append(campana)
    return [dict(r) for r in c.execute(q, args)]


def _acum() -> dict:
    return {"gasto": 0.0, "impresiones": 0, "alcance": 0, "clics": 0, "conv": 0,
            "personas": set(), "citas": 0, "atendidos": 0, "venta": 0, "centro": 0}


def _cerrar(a: dict) -> dict:
    g = round(a["gasto"])
    citas, aten = a["citas"], a["atendidos"]
    return {
        "gasto": g, "impresiones": a["impresiones"], "clics": a["clics"],
        "conversaciones": a["conv"], "personas": len(a["personas"]),
        "citas": citas, "atendidos": aten,
        "cac_conv": _div(g, a["conv"]), "cac_cita": _div(g, citas),
        "cac_atendido": _div(g, aten),
        "venta": round(a["venta"]),
        "retorno": round(a["venta"] / g, 2) if g else None,
        "centro": round(a["centro"]),
        "centro_pct": round(100 * a["centro"] / a["venta"]) if a["venta"] else None,
        "resultado": round(a["centro"]) - g,
        "frecuencia": round(a["impresiones"] / a["alcance"], 2) if a["alcance"] else None,
        "muestra_chica": citas < MUESTRA_CHICA,
    }


def _orden_cac(x: dict):
    # CAC por cita de menor a mayor; sin citas pero con gasto al final (el peor
    # caso: plata sin resultado), y sin gasto ni citas al fondo.
    if x["cac_cita"] is not None:
        return (0, x["cac_cita"])
    if x["gasto"] > 0:
        return (1, -x["gasto"])
    return (2, -x["personas"])


_DIAS = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
_UBIC = {"feed": "Feed", "facebook_reels": "Reels", "instagram_reels": "Reels",
         "instagram_stories": "Historias", "facebook_stories": "Historias",
         "story": "Historias", "reels": "Reels", "marketplace": "Marketplace",
         "video_feeds": "Videos", "instagram_explore": "Explorar",
         "instagram_explore_grid_home": "Explorar", "search": "Búsqueda",
         "facebook_profile_feed": "Perfil", "instagram_profile_feed": "Perfil",
         "right_hand_column": "Columna derecha", "an_classic": "Audience Network",
         "messenger_inbox": "Bandeja Messenger", "threads_feed": "Threads"}
_PLAT_LBL = {"facebook": "Facebook", "instagram": "Instagram", "messenger": "Messenger",
             "audience_network": "Audience Network", "threads": "Threads", "sin_dato": "Sin dato"}
_SEXO = {"female": "Mujeres", "male": "Hombres", "unknown": "Sin dato"}


def _desglose(filas: list[dict], etiqueta) -> list[dict]:
    acc: dict[str, dict] = {}
    for f in filas:
        k = etiqueta(f)
        if k is None:
            continue
        a = acc.setdefault(k, {"k": k, "gasto": 0.0, "conv": 0, "impresiones": 0})
        a["gasto"] += f["spend"] or 0
        a["conv"] += f["conversaciones"] or 0
        a["impresiones"] += f["impressions"] or 0
    out = []
    for a in acc.values():
        g = round(a["gasto"])
        out.append({"k": a["k"], "gasto": g, "conversaciones": a["conv"],
                    "impresiones": a["impresiones"], "cac_conv": _div(g, a["conv"])})
    return out


def _lbl_ubic(f):
    plat, _, pos = (f["valor"] or "").partition("|")
    return f"{_PLAT_LBL.get(plat, plat.title() or 'Otro')} · {_UBIC.get(pos, pos.replace('_', ' ').capitalize() or 'Otro')}"


def _lbl_edad_sexo(f):
    edad, _, sexo = (f["valor"] or "").partition("|")
    return f"{edad or '—'} · {_SEXO.get(sexo, sexo or 'Sin dato')}"


def _lbl_hora(f):
    v = (f["valor"] or "")[:2]
    return v if v.isdigit() else None


def panel_data(desde: str | None = None, hasta: str | None = None,
               campana: str | None = None, plataforma: str | None = None) -> dict:
    d, h = _rango(desde, hasta)
    plat = plataforma if plataforma in (*_PLATS, "sin_dato") else None
    e0, e1 = _epoch_ini(d), _epoch_fin(h)
    with db() as c:
        mapa = _mapa_anuncios(c)
        ins = _insights_filas(c, d, h, campana, plat if plat in _PLATS else None)
        # 'sin_dato' no existe en Meta: el gasto no se puede filtrar así.
        if plat == "sin_dato":
            ins = []
        por_ad: dict[str, dict] = defaultdict(_acum)
        tot = _acum()
        for f in ins:
            for a in (por_ad[f["ad_id"]], tot):
                a["gasto"] += f["spend"] or 0
                a["impresiones"] += f["impressions"] or 0
                a["alcance"] += f["reach"] or 0
                a["clics"] += f["clicks"] or 0
                a["conv"] += f["conversaciones"] or 0

        # Personas captadas: teléfonos únicos que llegaron por anuncio.
        for r in c.execute(
                "SELECT phone, source_id, headline, plataforma FROM meta_referrals "
                "WHERE ts >= ? AND ts < ?", (e0, e1)):
            info = _info_ad(mapa, r["source_id"] or "", r["headline"] or "")
            if not _pasa_filtros(info, r["plataforma"], campana, plat):
                continue
            k = _clave(r["phone"])
            por_ad[info["ad_id"]]["personas"].add(k)
            tot["personas"].add(k)

        citas = _citas_atribuidas(c, e0, e1, mapa, campana, plat)
        compras = _purchases(c, e0)
        for ci in citas:
            ci["atendido"] = any(t >= ci["created_epoch"] for t in compras.get(ci["clave"], []))
            for a in (por_ad[ci["ad_id"]], tot):
                a["citas"] += 1
                a["atendidos"] += 1 if ci["atendido"] else 0

        for ad_id, (monto, centro) in _venta_por_anuncio(c, citas).items():
            for a in (por_ad[ad_id], tot):
                a["venta"] += monto
                a["centro"] += centro

        # Tabla por anuncio y por campaña
        anuncios, por_camp = [], defaultdict(_acum)
        camp_nombre: dict[str, str] = {}
        for ad_id, a in por_ad.items():
            info = _info_ad(mapa, ad_id)
            if ad_id and ad_id not in mapa:
                # nombre desde el headline del referral, si lo hay
                hl = c.execute("SELECT headline FROM meta_referrals WHERE source_id=? AND headline != '' "
                               "ORDER BY ts DESC LIMIT 1", (ad_id,)).fetchone()
                if hl:
                    info["anuncio"] = hl[0]
            fila = {**info, **_cerrar(a)}
            anuncios.append(fila)
            cid = info["campaign_id"]
            camp_nombre[cid] = info["campana"]
            pc = por_camp[cid]
            for k in ("gasto", "impresiones", "alcance", "clics", "conv", "citas", "atendidos", "venta", "centro"):
                pc[k] += a[k]
            pc["personas"] |= a["personas"]
        campanas = [{"campaign_id": cid, "campana": camp_nombre[cid],
                     "n_anuncios": sum(1 for x in anuncios if x["campaign_id"] == cid),
                     **_cerrar(a)} for cid, a in por_camp.items()]
        anuncios.sort(key=_orden_cac)
        campanas.sort(key=_orden_cac)

        # Desgloses (solo hasta conversación)
        des = {
            "plataforma": _desglose(_insights_filas(c, d, h, campana, None, "plataforma"),
                                    lambda f: _PLAT_LBL.get(f["valor"], (f["valor"] or "Otro").title())),
            "ubicacion": _desglose(_insights_filas(c, d, h, campana, plat if plat in _PLATS else None, "ubicacion"),
                                   _lbl_ubic),
            "edad_sexo": _desglose(_insights_filas(c, d, h, campana, None, "edad_sexo"), _lbl_edad_sexo),
            "hora": _desglose(_insights_filas(c, d, h, campana, None, "hora"), _lbl_hora),
            "dia_semana": _desglose(ins, lambda f: _DIAS[datetime.strptime(f["fecha"], "%Y-%m-%d").weekday()]),
        }
        des["plataforma"].sort(key=lambda x: -x["gasto"])
        des["ubicacion"].sort(key=lambda x: -x["gasto"])
        des["ubicacion"] = des["ubicacion"][:8]
        des["edad_sexo"].sort(key=lambda x: x["k"])
        des["hora"].sort(key=lambda x: x["k"])
        orden_dia = {n: i for i, n in enumerate(_DIAS)}
        des["dia_semana"].sort(key=lambda x: orden_dia.get(x["k"], 9))

        # Tendencia mensual: 12 meses hasta el fin del rango.
        _m = h.year * 12 + h.month - 1 - 11          # 11 meses antes del mes de `hasta`
        t0 = date(_m // 12, _m % 12 + 1, 1)
        tend_f = [] if plat == "sin_dato" else _insights_filas(c, t0, h, campana, plat if plat in _PLATS else None)
        meses: dict[str, dict] = {}
        for f in tend_f:
            m = meses.setdefault(f["fecha"][:7], {"gasto": 0.0, "conv": 0, "imp": 0, "alc": 0})
            m["gasto"] += f["spend"] or 0
            m["conv"] += f["conversaciones"] or 0
            m["imp"] += f["impressions"] or 0
            m["alc"] += f["reach"] or 0
        tendencia = [{"mes": k, "gasto": round(v["gasto"]), "conversaciones": v["conv"],
                      "cac_conv": _div(round(v["gasto"]), v["conv"]),
                      "frecuencia": round(v["imp"] / v["alc"], 2) if v["alc"] else None}
                     for k, v in sorted(meses.items())]

        # Alertas
        alertas = []
        for x in anuncios:
            if x["gasto"] >= UMBRAL_GASTO_SIN_CITAS and x["citas"] == 0:
                alertas.append({"tipo": "sin_citas", "ad_id": x["ad_id"], "anuncio": x["anuncio"],
                                "campana": x["campana"], "gasto": x["gasto"],
                                "texto": "Gastó sin traer ninguna cita en el rango."})
            if (x["frecuencia"] or 0) > UMBRAL_FRECUENCIA:
                alertas.append({"tipo": "saturado", "ad_id": x["ad_id"], "anuncio": x["anuncio"],
                                "campana": x["campana"], "frecuencia": x["frecuencia"],
                                "texto": "Saturado: la misma gente lo ve una y otra vez."})
        alertas.sort(key=lambda a: (a["tipo"] != "sin_citas", -(a.get("gasto") or 0)))

        _uf = c.execute("SELECT MAX(fecha) FROM meta_insights_diario").fetchone()[0]
        hay_insights = _uf is not None
        opciones = _opciones(c, mapa)

    k = _cerrar(tot)
    return {
        "rango": {"desde": d.isoformat(), "hasta": h.isoformat(), "dias": (h - d).days + 1},
        "filtros": {"campana": campana or "", "plataforma": plat or ""},
        "kpis": k,
        "campanas": campanas,
        "anuncios": anuncios,
        "desgloses": des,
        "tendencia": tendencia,
        "alertas": alertas,
        "hay_insights": hay_insights,
        "ultima_foto": _uf,
        "opciones": opciones,
        "umbrales": {"gasto_sin_citas": UMBRAL_GASTO_SIN_CITAS, "frecuencia": UMBRAL_FRECUENCIA,
                     "muestra_chica": MUESTRA_CHICA},
        "medicion": {"atribucion_desde": ATRIBUCION_DESDE, "plataforma_desde": PLATAFORMA_DESDE,
                     "rango_antes_de_atribucion": d.isoformat() < ATRIBUCION_DESDE,
                     "rango_antes_de_plataforma": d.isoformat() < PLATAFORMA_DESDE},
    }


def _opciones(c, mapa: dict) -> dict:
    camps: dict[str, str] = {}
    for m in mapa.values():
        if m["campaign_id"]:
            camps[m["campaign_id"]] = m["campaign_name"] or m["campaign_id"]
    ads = [{"id": a, "nombre": m["ad_name"] or a, "campaign_id": m["campaign_id"]}
           for a, m in mapa.items() if a]
    ads.sort(key=lambda x: x["nombre"].lower())
    esp = sorted({(r[0] or "").strip() for r in c.execute(
        "SELECT DISTINCT especialidad FROM citas_bot WHERE ad_source_id IS NOT NULL "
        "AND ad_source_id != ''") if (r[0] or "").strip()}, key=str.lower)
    return {"campanas": sorted(({"id": k, "nombre": v} for k, v in camps.items()),
                               key=lambda x: x["nombre"].lower()),
            "anuncios": ads, "especialidades": esp}


# ── Kanban ──────────────────────────────────────────────────────────────────

def _fmt_fecha(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        return datetime.strptime(iso[:10], "%Y-%m-%d").strftime("%d/%m/%Y")
    except ValueError:
        return iso


def _cita_futura(ci: dict, hoy: str, ahora_hm: str) -> bool:
    f = (ci.get("fecha") or "")[:10]
    return f > hoy or (f == hoy and (ci.get("hora") or "")[:5] >= ahora_hm)


def kanban_data(desde: str | None = None, hasta: str | None = None,
                campana: str | None = None, anuncio: str | None = None,
                plataforma: str | None = None, especialidad: str | None = None,
                q: str | None = None, ahora: datetime | None = None) -> dict:
    """Una tarjeta por persona (clave = últimos 9 dígitos) cuyo ÚLTIMO clic en
    un anuncio cae en el rango. La etapa sale de los datos, por prioridad:

      1. Agendado   — tiene una cita creada desde ese clic, futura y no anulada.
      2. Atendido   — aviso CAPI Purchase desde el clic, o una cita creada
                      desde el clic que ya pasó y no fue anulada.
      3. Anuló      — todas sus citas desde el clic están anuladas.
      4. Perdido    — sin cita, y su último mensaje (o el clic) tiene > 7 días.
      5. Vio horas  — el bot le ofreció horarios después del clic.
      6. Escribió   — todo lo demás.
    """
    ahora = ahora or datetime.now(_CL)
    hoy, ahora_hm = ahora.date().isoformat(), ahora.strftime("%H:%M")
    ahora_ep = int(ahora.timestamp())
    d, h = _rango(desde, hasta)
    plat = plataforma if plataforma in (*_PLATS, "sin_dato") else None
    e0, e1 = _epoch_ini(d), _epoch_fin(h)
    esp_f = (especialidad or "").strip().lower()
    qn = (q or "").strip().lower()
    qd = "".join(ch for ch in qn if ch.isdigit())

    with db() as c:
        mapa = _mapa_anuncios(c)
        # Último referral por persona en el rango
        ult: dict[str, dict] = {}
        for r in c.execute(
                "SELECT phone, source_id, headline, plataforma, ts FROM meta_referrals "
                "WHERE ts >= ? AND ts < ? ORDER BY ts", (e0, e1)):
            ult[_clave(r["phone"])] = dict(r)
        personas = {}
        for k, r in ult.items():
            info = _info_ad(mapa, r["source_id"] or "", r["headline"] or "")
            if not _pasa_filtros(info, r["plataforma"], campana, plat, anuncio):
                continue
            personas[k] = {**info, "phone": r["phone"], "ts": r["ts"],
                           "plataforma": _plat(r["plataforma"])}
        if not personas:
            return _kanban_vacio(d, h, _opciones(c, mapa))

        # Citas, eventos y mensajes desde el primer clic — agrupados por persona.
        t_min = min(p["ts"] for p in personas.values()) - 3600
        citas: dict[str, list[dict]] = defaultdict(list)
        for r in c.execute(
                "SELECT phone, especialidad, profesional, fecha, hora, created_at, cancel_detected_at, "
                "confirmation_status, paciente_nombre FROM citas_bot WHERE created_at >= ?",
                (_utc_txt(t_min),)):
            k = _clave(r["phone"])
            if k in personas:
                dd = dict(r)
                dd["created_epoch"] = _utc_txt_epoch(dd["created_at"]) or 0
                citas[k].append(dd)
        compras = _purchases(c, t_min)
        slots: dict[str, list[int]] = defaultdict(list)
        esp_ev: dict[str, str] = {}
        for r in c.execute(
                "SELECT phone, event, ts, meta FROM conversation_events WHERE ts >= ? AND event IN (?,?,?) "
                "ORDER BY ts", (_utc_txt(t_min), *_EV_SLOTS, "funnel_especialidad")):
            k = _clave(r["phone"])
            if k not in personas:
                continue
            ep = _utc_txt_epoch(r["ts"]) or 0
            if r["event"] in _EV_SLOTS:
                slots[k].append(ep)
            try:
                e = (json.loads(r["meta"] or "{}").get("esp") or "").strip()
            except (ValueError, TypeError):
                e = ""
            if e and ep >= personas[k]["ts"] - 3600:
                esp_ev[k] = e
        ult_msg: dict[str, int] = {}
        for r in c.execute(
                "SELECT phone, MAX(ts) FROM messages WHERE direction='in' AND ts >= ? GROUP BY phone",
                (_utc_txt(t_min),)):
            k = _clave(r[0])
            if k in personas:
                ep = _utc_txt_epoch(r[1]) or 0
                ult_msg[k] = max(ult_msg.get(k, 0), ep)
        nombres: dict[str, str] = {}
        for r in c.execute("SELECT phone, nombre FROM contact_profiles WHERE nombre IS NOT NULL AND nombre != ''"):
            k = _clave(r[0])
            if k in personas:
                nombres[k] = r[1].strip()
        opciones = _opciones(c, mapa)

    cols: dict[str, list[dict]] = {e: [] for e in _ETAPA_IDS}
    for k, p in personas.items():
        R = p["ts"]
        mis = [ci for ci in citas.get(k, []) if ci["created_epoch"] >= R - 3600]
        vigentes = [ci for ci in mis if not _cancelada(ci)]
        futuras = sorted([ci for ci in vigentes if _cita_futura(ci, hoy, ahora_hm)],
                         key=lambda x: (x["fecha"], x["hora"] or ""))
        pasadas = sorted([ci for ci in vigentes if not _cita_futura(ci, hoy, ahora_hm)],
                         key=lambda x: (x["fecha"], x["hora"] or ""))
        comp = [t for t in compras.get(k, []) if t >= R]
        sl = [t for t in slots.get(k, []) if t >= R - 60]
        actividad = max(ult_msg.get(k, 0), R)

        if futuras:
            etapa, desde_ep = "agendado", futuras[0]["created_epoch"]
        elif comp or pasadas:
            etapa = "atendido"
            if pasadas:
                f = pasadas[-1]["fecha"][:10]
                desde_ep = _epoch_ini(datetime.strptime(f, "%Y-%m-%d").date())
            else:
                desde_ep = min(comp)
        elif mis:
            etapa = "anulo"
            canc = [_utc_txt_epoch(ci.get("cancel_detected_at")) for ci in mis]
            canc = [x for x in canc if x]
            desde_ep = max(canc) if canc else max(ci["created_epoch"] for ci in mis)
        elif ahora_ep - actividad > DIAS_PERDIDO * 86400:
            etapa, desde_ep = "perdido", actividad
        elif sl:
            etapa, desde_ep = "vio_horas", min(sl)
        else:
            etapa, desde_ep = "escribio", R

        ref_cita = futuras[0] if futuras else (pasadas[-1] if pasadas else (mis[-1] if mis else None))
        esp = (ref_cita or {}).get("especialidad") or esp_ev.get(k, "")
        if esp_f and esp.strip().lower() != esp_f:
            continue
        nombre = nombres.get(k) or next((ci["paciente_nombre"] for ci in reversed(mis)
                                         if (ci.get("paciente_nombre") or "").strip()), "")
        if qn:
            hay = (qn in nombre.lower()) or (len(qd) >= 3 and qd in "".join(ch for ch in p["phone"] if ch.isdigit()))
            if not hay:
                continue
        llegada = datetime.fromtimestamp(R, _CL)
        cols[etapa].append({
            "clave": k,
            "phone": p["phone"],
            "telefono": _mascara(p["phone"]),
            "nombre": nombre,
            "anuncio": p["anuncio"], "ad_id": p["ad_id"],
            "campana": p["campana"], "campaign_id": p["campaign_id"],
            "plataforma": p["plataforma"],
            "especialidad": esp.strip().capitalize() if esp and esp.islower() else esp,
            "llegada": llegada.strftime("%d/%m/%Y"),
            "llegada_iso": llegada.strftime("%Y-%m-%d %H:%M"),
            "proxima_cita": ({"fecha": _fmt_fecha(futuras[0]["fecha"]), "hora": (futuras[0]["hora"] or "")[:5]}
                             if futuras else None),
            "dias_etapa": max(0, (ahora_ep - desde_ep) // 86400),
            "_orden": actividad,
        })

    columnas = []
    for e in ETAPAS:
        ts = sorted(cols[e["id"]], key=lambda x: -x["_orden"])  # más reciente primero
        for t in ts:
            t.pop("_orden", None)
        columnas.append({**e, "n": len(ts), "tarjetas": ts})
    return {
        "rango": {"desde": d.isoformat(), "hasta": h.isoformat(), "dias": (h - d).days + 1},
        "columnas": columnas,
        "total": sum(c_["n"] for c_ in columnas),
        "opciones": opciones,
        "medicion": {"atribucion_desde": ATRIBUCION_DESDE, "plataforma_desde": PLATAFORMA_DESDE,
                     "dias_perdido": DIAS_PERDIDO},
    }


def _kanban_vacio(d: date, h: date, opciones: dict) -> dict:
    return {"rango": {"desde": d.isoformat(), "hasta": h.isoformat(), "dias": (h - d).days + 1},
            "columnas": [{**e, "n": 0, "tarjetas": []} for e in ETAPAS], "total": 0,
            "opciones": opciones,
            "medicion": {"atribucion_desde": ATRIBUCION_DESDE, "plataforma_desde": PLATAFORMA_DESDE,
                         "dias_perdido": DIAS_PERDIDO}}


# ── Endpoints ───────────────────────────────────────────────────────────────

@router.get("/panel")
def panel(request: Request, desde: str | None = Query(None), hasta: str | None = Query(None),
          campana: str | None = Query(None), plataforma: str | None = Query(None),
          token: str | None = Query(None)):
    _auth(request, token)
    return panel_data(desde, hasta, campana or None, plataforma or None)


@router.get("/kanban")
def kanban(request: Request, desde: str | None = Query(None), hasta: str | None = Query(None),
           campana: str | None = Query(None), anuncio: str | None = Query(None),
           plataforma: str | None = Query(None), especialidad: str | None = Query(None),
           q: str | None = Query(None), token: str | None = Query(None)):
    _auth(request, token)
    return kanban_data(desde, hasta, campana or None, anuncio or None, plataforma or None,
                       especialidad or None, q or None)
