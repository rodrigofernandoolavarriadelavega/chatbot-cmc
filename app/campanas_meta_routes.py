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
- Venta por TELÉFONO: si la persona escribió desde el anuncio y no agendó por
  el bot, pero su número está en la ficha de un paciente (pacientes_heatmap,
  citas de recepción) que pagó en caja dentro de 90 días desde el clic, esa
  venta también es del anuncio (desde el día del clic). Así entra lo que
  agenda recepción o llega directo. `pagaron_tel` cuenta esos pacientes.
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
import re
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
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
     "ayuda": "Medilink marca la cita como atendida, o pagó en caja ese día o dentro de 90 días del clic aunque no agendara por el bot. Sin dato de Medilink: cita pasada y no anulada (respaldo)."},
    {"id": "anulo",     "label": "Anuló / no asistió",
     "ayuda": "Medilink la marca como inasistencia (no asistió) o la cita fue anulada, y no tiene otra vigente."},
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
    if p == "web":
        return "web"
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


# Estado de entrega (activo/pausado) por anuncio, en vivo desde Meta. Se cachea
# 15 min: el panel no puede esperar a Meta en cada carga. Si Meta falla, se
# usa como respaldo "tuvo gasto ayer u hoy" en la foto diaria.
_ESTADOS_CACHE: dict = {"ts": 0.0, "data": {}}
_ESTADOS_TTL = 900


def _estados_meta() -> dict[str, str]:
    import time as _t
    if _t.time() - _ESTADOS_CACHE["ts"] < _ESTADOS_TTL and _ESTADOS_CACHE["data"]:
        return _ESTADOS_CACHE["data"]
    out: dict[str, str] = {}
    try:
        import httpx
        import config
        token = getattr(config, "META_ACCESS_TOKEN", "")
        acct = getattr(config, "META_AD_ACCOUNT_ID", "") or "act_220608142267129"
        acct = acct if acct.startswith("act_") else f"act_{acct}"
        if token:
            url = f"https://graph.facebook.com/v22.0/{acct}/ads"
            params = {"fields": "id,effective_status", "limit": 500}
            with httpx.Client(timeout=8) as cl:
                while url:
                    r = cl.get(url, params=params, headers={"Authorization": f"Bearer {token}"})
                    if r.status_code != 200:
                        break
                    b = r.json()
                    for a in b.get("data", []):
                        out[str(a.get("id"))] = a.get("effective_status") or ""
                    url, params = (b.get("paging") or {}).get("next"), None
    except Exception as e:
        log.warning("campanas_meta: estados de Meta no disponibles: %s", e)
    if out:
        _ESTADOS_CACHE.update(ts=_t.time(), data=out)
    return out


def _con_gasto_reciente(c) -> set[str]:
    hace2 = (_hoy() - timedelta(days=2)).isoformat()
    return {r[0] for r in c.execute("SELECT DISTINCT ad_id FROM meta_insights_diario "
                                    "WHERE desglose='total' AND fecha >= ? AND spend > 0", (hace2,))}


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


# ── Canal: anuncios Meta, página web o ambos ────────────────────────────────
# La web (centromedicocarampangue.cl) no tiene gasto ni anuncios: los botones
# de WhatsApp mandan "(web: <página>)" y el bot guarda `referral_source:web` y
# `referral_source:web_<página>` en contact_tags (con la fecha de la PRIMERA
# vez, INSERT OR IGNORE). Cada página se trata como un "anuncio" sintético
# `web:<página>` dentro de la campaña "Página web". En "todos", cada persona
# cuenta solo en el canal de su PRIMER contacto del rango: no se duplica.

CANALES = ("meta", "web", "todos")
_WEB_PAG_LBL = {"home": "Inicio", "blog": "Blog", "comuna": "Página por comuna",
                "landing_ortodoncia": "Landing de ortodoncia", "v2": "Portada v2",
                "sin_pagina": "Sin página identificada", "agendador": "Agendador web"}
# Marcadores antiguos que traían la posición pegada a la página.
_WEB_PAG_ALIAS = {"comuna_float": ("comuna", "flotante"), "blog_float": ("blog", "flotante"),
                  "v2-hero": ("v2", "hero")}
_WEB_POS_LBL = {"hero": "Portada (hero)", "flotante": "Botón flotante", "float": "Botón flotante",
                "header": "Encabezado", "footer": "Pie de página", "cta": "Llamado final"}


def _pag_pos(pag: str, pos: str = "") -> tuple[str, str]:
    pag = (pag or "").strip().lower()
    if pag in _WEB_PAG_ALIAS:
        p2, p_pos = _WEB_PAG_ALIAS[pag]
        return p2, pos or p_pos
    return pag, pos


def _es_posicion(x: str) -> bool:
    x = (x or "").lower()
    return x in _WEB_POS_LBL or x.startswith("cuerpo")


def _pos_lbl(pos: str) -> str:
    if not pos:
        return ""
    if pos.startswith("cuerpo"):
        n = pos.split("-", 1)[1] if "-" in pos else ""
        return "En el texto" + (f" ({n})" if n else "")
    return _WEB_POS_LBL.get(pos, pos.replace("-", " ").replace("_", " ").capitalize())
VENTANA_CITA_WEB_DIAS = 90
VENTANA_MSG_WEB_SEG = 120   # el primer mensaje cae a ±2 min de la fecha del tag

# Comunas que aparecen en los textos de los botones (página de cada comuna).
_COMUNAS = [("Curanilahue", "curanilahue"), ("Los Álamos", "los alamos"), ("Arauco", "arauco"),
            ("Lebu", "lebu"), ("Cañete", "canete"), ("Carampangue", "carampangue"),
            ("Tirúa", "tirua"), ("Contulmo", "contulmo"), ("Laraquete", "laraquete"),
            ("Ramadillas", "ramadillas"), ("Concepción", "concepcion"), ("Coronel", "coronel"),
            ("Lota", "lota"), ("Santa Juana", "santa juana")]
_RE_WEB = None


def _canal(c: str | None) -> str:
    return c if c in CANALES else "meta"


def _es_web_camp(campana: str | None) -> bool:
    return bool(campana) and campana.startswith("web:")


def _web_lbl(pag: str) -> str:
    return _WEB_PAG_LBL.get(pag) or (pag.replace("_", " ").replace("-", " ").strip().capitalize() or "Sin página")


def parse_web(texto: str | None) -> dict:
    """Lee el texto que pre-escribe cada botón del sitio:
    "Hola, quiero agendar una Ecografía. (web: blog)" → página blog, botón
    "Agendar · Ecografía", especialidad Ecografía. Con el marcador extendido
    "(web: blog · eco-abdominal · flotante)" trae además el artículo y la
    posición del botón; en el marcador simple el artículo NO viene."""
    import re
    global _RE_WEB
    if _RE_WEB is None:
        _RE_WEB = re.compile(r"\(\s*web\s*(?:[:：]\s*([^()]{0,120}?))?\s*\)", re.I)
    t = (texto or "").strip()
    m = _RE_WEB.search(t)
    partes = [re.sub(r"[^\w-]", "", x.strip().lower()) for x in re.split(r"[·|/]", (m.group(1) or "") if m else "")]
    partes = [x for x in partes if x] + ["", "", ""]
    pag, articulo, pos = partes[0], partes[1][:80], partes[2][:40]
    articulo = "" if articulo.strip("-") == "" else articulo   # el sitio manda "-" = sin artículo
    if articulo and not pos and _es_posicion(articulo):   # "(web: home · hero)"
        articulo, pos = "", articulo
    pag, pos = _pag_pos(pag, pos)
    resto = _RE_WEB.sub(" ", t) if m else t
    resto = " ".join(re.sub(r"\d+", " ", resto).split()).strip(" .,;:-")
    n = " " + re.sub(r"[^a-z ]+", " ", _sin_tildes(resto)) + " "
    n = " ".join(n.split())
    comuna = next((lbl for lbl, k in _COMUNAS if re.search(r"(^| )" + k + r"( |$)", n)), "")
    explicita = bool(pag)
    if not pag:
        pag = "comuna" if comuna else "sin_pagina"
    esp = _grupo(resto)
    if "vi el blog" in n or "lei el blog" in n or "lei en el blog" in n:
        intent = "blog"
    elif "agend" in n or " hora" in " " + n or "reserv" in n:
        intent = "agendar"
    else:
        intent = ""
    if intent == "agendar":
        boton = f"Agendar · {esp}" if esp else "Agendar (genérico)"
    elif intent == "blog":
        boton = f"Leyó el blog · {esp}" if esp else "Leyó el blog"
    elif esp:
        boton = f"Consulta · {esp}"
    else:
        sin_saludo = re.sub(r"^(hola|buen[oa]s?( dias| tardes| noches)?|buenas)\s*[,.!]*\s*", "", resto, flags=re.I)
        sin_saludo = sin_saludo.strip(" .,;:!-")
        boton = (sin_saludo[:1].upper() + sin_saludo[1:])[:70] if sin_saludo else "Solo saludo (sin pedido)"
    slug = re.sub(r"[^a-z0-9]+", "-", _sin_tildes(boton)).strip("-") or "sin-texto"
    return {"pagina_id": pag, "pagina": _web_lbl(pag), "pagina_explicita": explicita, "articulo": articulo,
            "posicion": pos, "boton": boton, "boton_id": slug,
            "texto": resto, "especialidad": esp or "", "comuna": comuna}


_WEB_INFO: dict[str, dict] = {}   # ad_id web → etiquetas (se llena al leer las llegadas)


def _info_web(ad_id: str) -> dict:
    if ad_id in _WEB_INFO:
        return dict(_WEB_INFO[ad_id])
    partes = (ad_id.split(":") + ["", "", "", ""])[1:5]
    pag = partes[0] or "sin_pagina"
    boton = partes[3].replace("-", " ").capitalize() if partes[3] else "Sin texto"
    return {"ad_id": ad_id, "anuncio": boton, "campaign_id": f"web:{pag}",
            "campana": "Web · " + _web_lbl(pag), "pagina": _web_lbl(pag), "boton": boton, "esp_boton": "",
            "articulo": partes[1] if partes[1] != "-" else "", "posicion": _pos_lbl(partes[2] if partes[2] != "-" else ""),
            "detalle": ""}


def _info(mapa: dict, ad_id: str, headline: str = "") -> dict:
    if (ad_id or "").startswith("web:"):
        return _info_web(ad_id)
    return _info_ad(mapa, ad_id, headline)


def _llegadas_web(c, e0: int | None = None, e1: int | None = None,
                  claves: set[str] | None = None) -> list[dict]:
    """Llegadas desde la web, con la misma forma que una fila de meta_referrals.

    Fuente principal: el evento `web_origen` (desde oct-2026), uno por CADA
    llegada, con página, artículo, posición del botón y el texto. Respaldo para
    lo histórico: el tag `referral_source:web*` (solo la PRIMERA vez) más el
    mensaje entrante "(web…)" más cercano a esa fecha (±2 min). Un tag que cae
    a menos de 5 min de un `web_origen` es la misma llegada y no se repite.
    Incluye el agendador web (`cita_origen` canal 'web') si alguna vez tiene filas."""
    def _ok(k):
        return claves is None or k in claves

    like = ("%" + next(iter(claves))) if claves is not None and len(claves) == 1 else None
    eventos: list[tuple[str, str, int, dict]] = []   # (clave, phone, ts, meta)
    try:
        q = "SELECT phone, ts, meta FROM conversation_events WHERE event='web_origen'"
        for r in c.execute(q + (" AND phone LIKE ?" if like else ""), (like,) if like else ()):
            k = _clave(r["phone"])
            if not _ok(k):
                continue
            try:
                meta = json.loads(r["meta"] or "{}")
            except (ValueError, TypeError):
                meta = {}
            eventos.append((k, r["phone"], _utc_txt_epoch(r["ts"]) or 0, meta))
    except Exception:
        pass
    ev_eps: dict[str, list[int]] = defaultdict(list)
    for k, _, ep, _m in eventos:
        ev_eps[k].append(ep)

    por: dict[str, dict] = {}
    try:
        filas = c.execute("SELECT phone, tag, ts FROM contact_tags WHERE tag LIKE 'referral_source:web%'").fetchall()
    except Exception:
        filas = []
    for r in filas:
        tag = (r["tag"] or "").strip().lower()
        if tag == "referral_source:web":
            pag = None
        elif tag.startswith("referral_source:web_"):
            pag = tag.split("referral_source:web_", 1)[1].strip() or None
        else:
            continue
        k = _clave(r["phone"])
        if not _ok(k):
            continue
        ep = _utc_txt_epoch(r["ts"]) or 0
        d = por.setdefault(k, {"phone": r["phone"], "gen": None, "pags": {}})
        if pag is None:
            d["gen"] = ep if d["gen"] is None else min(d["gen"], ep)
        else:
            d["pags"][pag] = min(d["pags"].get(pag, ep), ep)
    agendador: dict[str, tuple[str, int]] = {}
    try:
        for r in c.execute("SELECT b.phone, o.created_at FROM cita_origen o JOIN citas_bot b ON b.id_cita = o.id_cita "
                           "WHERE lower(o.canal) = 'web'"):
            k = _clave(r["phone"])
            if not _ok(k):
                continue
            ep = _utc_txt_epoch(r["created_at"]) or 0
            if k not in agendador or ep < agendador[k][1]:
                agendador[k] = (r["phone"], ep)
    except Exception:
        pass
    msgs: dict[str, list[tuple[int, str]]] = defaultdict(list)
    if por:
        q = "SELECT phone, text, ts FROM messages WHERE direction='in' AND text LIKE '%(web%'"
        try:
            for r in c.execute(q + (" AND phone LIKE ?" if like else ""), (like,) if like else ()):
                k = _clave(r["phone"])
                if k in por:
                    msgs[k].append((_utc_txt_epoch(r["ts"]) or 0, r["text"] or ""))
        except Exception:
            pass
    out = []

    def _fila(phone, ep, texto, pag_tag="", articulo="", pos="", fuente="tag"):
        w = parse_web(texto) if texto else {"pagina_id": "sin_pagina", "pagina_explicita": False, "articulo": "",
                                            "posicion": "", "boton": "Sin texto registrado",
                                            "boton_id": "sin-texto", "texto": "", "especialidad": "", "comuna": ""}
        pag, pos2 = _pag_pos(pag_tag, pos or w["posicion"])
        if not pag or (w["pagina_explicita"] and fuente == "tag"):
            pag = w["pagina_id"] if (w["pagina_explicita"] or not pag) else pag
        art = (articulo if (articulo or "").strip("-") else "") or w["articulo"]
        ad_id = f"web:{pag}:{art or '-'}:{pos2 or '-'}:{w['boton_id']}"
        pag_lbl, pos_lbl = _web_lbl(pag), _pos_lbl(pos2)
        det = " · ".join(x for x in (f"Artículo «{art}»" if art else "", pos_lbl) if x)
        _WEB_INFO[ad_id] = {"ad_id": ad_id, "anuncio": w["boton"], "campaign_id": f"web:{pag}",
                            "campana": "Web · " + pag_lbl, "pagina": pag_lbl, "boton": w["boton"],
                            "esp_boton": w["especialidad"], "articulo": art, "posicion": pos_lbl, "detalle": det}
        out.append({"phone": phone, "source_id": ad_id, "headline": "", "body": "", "plataforma": "web",
                    "ts": ep, "origen": "web", "web": {"pagina": pag_lbl, "articulo": art, "posicion": pos_lbl,
                                                      "boton": w["boton"], "texto": w["texto"],
                                                      "comuna": w["comuna"], "especialidad": w["especialidad"],
                                                      "fuente": fuente}})

    def _rango_ok(ep):
        return e0 is None or (e0 <= ep < e1)

    for k, phone, ep, meta in eventos:
        if _rango_ok(ep):
            art, pos = (meta.get("articulo") or ""), (meta.get("boton") or "")
            if art and not pos and _es_posicion(art):   # el bot guardó la posición como artículo
                art, pos = "", art
            _fila(phone, ep, meta.get("texto") or "", meta.get("pagina") or "", art, pos, fuente="evento")
    for k, d in por.items():
        pags = d["pags"] or ({"": d["gen"]} if d["gen"] is not None else {})
        for pag, ep in pags.items():
            if not _rango_ok(ep) or any(abs(ep - x) <= 300 for x in ev_eps.get(k, ())):
                continue
            cerca = [m for m in msgs.get(k, []) if abs(m[0] - ep) <= VENTANA_MSG_WEB_SEG]
            texto = min(cerca, key=lambda m: abs(m[0] - ep))[1] if cerca else ""
            _fila(d["phone"], ep, texto, pag)
    for k, (phone, ep) in agendador.items():
        if _rango_ok(ep):
            _fila(phone, ep, "", "agendador", fuente="agendador")
    return out


def _llegadas(c, canal: str, e0: int, e1: int, primera_web: bool = False) -> list[dict]:
    """Contactos del rango según el canal, ordenados por fecha. En 'todos'
    cada persona queda solo con las llegadas del canal de su primer contacto."""
    filas: list[dict] = []
    if canal in ("meta", "todos"):
        filas += [{**dict(r), "origen": "meta"} for r in c.execute(
            "SELECT phone, source_id, headline, plataforma, ts FROM meta_referrals WHERE ts >= ? AND ts < ?",
            (e0, e1))]
    if canal in ("web", "todos"):
        filas += _llegadas_web(c, e0, e1)
    filas.sort(key=lambda r: r["ts"])
    if primera_web:   # una persona con varias llegadas web cuenta en la primera del rango
        vistos: set[str] = set()
        f2 = []
        for r in filas:
            if r["origen"] == "web":
                k = _clave(r["phone"])
                if k in vistos:
                    continue
                vistos.add(k)
            f2.append(r)
        filas = f2
    if canal == "todos":
        primero: dict[str, str] = {}
        for r in filas:
            primero.setdefault(_clave(r["phone"]), r["origen"])
        filas = [r for r in filas if primero[_clave(r["phone"])] == r["origen"]]
    return filas


def _citas_web(c, llegada: dict[str, tuple[int, str]], hasta_epoch: int) -> list[dict]:
    """Citas del bot de personas que llegaron por la web: creadas desde la
    llegada y hasta 90 días después (y antes del fin del rango), una por
    (persona, especialidad)."""
    if not llegada:
        return []
    t_min = min(v[0] for v in llegada.values()) - 3600
    filas = c.execute(
        "SELECT phone, id_cita, especialidad, fecha, hora, created_at, cancel_detected_at, "
        "confirmation_status, id_paciente_medilink FROM citas_bot WHERE created_at >= ? AND created_at < ? "
        "ORDER BY created_at", (_utc_txt(t_min), _utc_txt(hasta_epoch))).fetchall()
    vistas: dict = {}
    out = []
    for r in filas:
        d = dict(r)
        k = _clave(d["phone"])
        if k not in llegada:
            continue
        ts, ad_id = llegada[k]
        ce = _utc_txt_epoch(d["created_at"]) or 0
        if ce < ts - 3600 or ce > ts + VENTANA_CITA_WEB_DIAS * 86400:
            continue
        kk = (k, (d["especialidad"] or "").strip().lower())
        if kk in vistas:
            vistas[kk]["hermanas"].append(dict(r))
            continue
        vistas[kk] = d
        d["hermanas"] = [dict(r)]
        d.update(_info_web(ad_id))
        d["clave"] = k
        d["created_epoch"] = ce
        out.append(d)
    return out


# ── Desenlace real de cada cita (Medilink) ──────────────────────────────────
# Fuente: `ausentismo_citas` (espejo nocturno de Medilink /citas, ver
# app/ausentismo.py), cruzado por id de cita con `citas_bot.id_cita`.
# Misma metodología que el módulo Ausentismo:
#   Atendida = id_estado 2 · No asistió = id_estado 8 sin anulación ·
#   Anulada = anulación 1 · 14 = reagenda (se sigue a la cita nueva del mismo
#   paciente y profesional) · un "no asiste" con otra cita ATENDIDA ese día con
#   el mismo profesional no es inasistencia (reagenda intradía).
# Respaldos, en orden: pago en caja o atención en BI ese día → atendida;
# anulada detectada por el bot → anulada; hora futura → pendiente; aviso CAPI
# Purchase (proxy antiguo) → atendida "por proxy"; si no, sin dato.

_RANGO_DES = {"atendida": 4, "no_show": 3, "pendiente": 2, "anulada": 1, "sin_dato": 0}


def _tabla_existe(c, nombre: str) -> bool:
    return bool(c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (nombre,)).fetchone())


def _clasif_medilink(id_estado, estado: str, anulacion) -> str:
    est = (estado or "").strip().lower()
    if id_estado == 14:
        return "reagenda"
    if id_estado == 2 or est.startswith("atend"):
        return "atendida"
    if (anulacion or 0) == 1:
        return "anulada"
    if id_estado == 8 or est.startswith("no asist"):
        return "no_show"
    return "otra"


class Desenlaces:
    """Calcula el desenlace de citas del bot con una sola carga de datos."""

    def __init__(self, c, citas: list[dict], hoy: str, compras: dict[str, list[int]] | None = None):
        self.hoy, self.compras = hoy, compras or {}
        self.med: dict[int, dict] = {}
        self.por_pac: dict[int, list[dict]] = defaultdict(list)
        self.pagos: set[tuple[int, str]] = set()
        ids, pids = set(), set()
        for ci in citas:
            for h in ci.get("hermanas") or [ci]:
                try:
                    ids.add(int(str(h.get("id_cita") or "").strip()))
                except ValueError:
                    pass
                if h.get("id_paciente_medilink"):
                    pids.add(int(h["id_paciente_medilink"]))
        if ids and _tabla_existe(c, "ausentismo_citas"):
            il = list(ids)
            for i in range(0, len(il), 500):
                lote = il[i:i + 500]
                for r in c.execute("SELECT id_cita, id_profesional, id_paciente, fecha, hora, id_estado, estado_cita, "
                                   "anulacion FROM ausentismo_citas WHERE id_cita IN (%s)" % ",".join("?" * len(lote)), lote):
                    self.med[int(r["id_cita"])] = dict(r)
                    if r["id_paciente"]:
                        pids.add(int(r["id_paciente"]))
            pl = list(pids)
            for i in range(0, len(pl), 500):
                lote = pl[i:i + 500]
                for r in c.execute("SELECT id_cita, id_profesional, id_paciente, fecha, hora, id_estado, estado_cita, "
                                   "anulacion FROM ausentismo_citas WHERE id_paciente IN (%s)" % ",".join("?" * len(lote)), lote):
                    self.por_pac[int(r["id_paciente"])].append(dict(r))
        pl = list(pids)
        for i in range(0, len(pl), 500):
            lote = pl[i:i + 500]
            for q in ("SELECT id_paciente, fecha FROM bi_pagos_caja WHERE id_paciente IN (%s)",
                      "SELECT id_paciente, fecha FROM bi_atenciones WHERE id_paciente IN (%s)"):
                try:
                    for r in c.execute(q % ",".join("?" * len(lote)), lote):
                        self.pagos.add((int(r[0]), (r[1] or "")[:10]))
                except Exception:
                    pass

    def _medilink(self, idc: int) -> str | None:
        m = self.med.get(idc)
        if not m:
            return None
        t = _clasif_medilink(m["id_estado"], m["estado_cita"], m["anulacion"])
        pac, prof, f = m["id_paciente"], m["id_profesional"], (m["fecha"] or "")[:10]
        if t == "no_show" and pac:
            if any(_clasif_medilink(x["id_estado"], x["estado_cita"], x["anulacion"]) == "atendida"
                   and x["id_profesional"] == prof and (x["fecha"] or "")[:10] == f for x in self.por_pac.get(pac, [])):
                return "atendida"
        if t == "reagenda" and pac:
            lim = (date.fromisoformat(f) + timedelta(days=45)).isoformat() if f else "9999"
            nuevas = sorted((x for x in self.por_pac.get(pac, []) if x["id_profesional"] == prof
                             and int(x["id_cita"]) != idc and f <= (x["fecha"] or "")[:10] <= lim),
                            key=lambda x: (x["fecha"] or "", x["hora"] or ""))
            for x in nuevas:
                t2 = _clasif_medilink(x["id_estado"], x["estado_cita"], x["anulacion"])
                if t2 in ("atendida", "no_show", "anulada"):
                    return t2
            return None
        if t in ("atendida", "no_show", "anulada"):
            return t
        return None

    def una(self, h: dict, clave: str = "") -> tuple[str, str]:
        """(desenlace, fuente) de una fila de citas_bot."""
        try:
            idc = int(str(h.get("id_cita") or "").strip())
        except ValueError:
            idc = None
        if idc is not None:
            t = self._medilink(idc)
            if t:
                return t, "medilink"
        f = (h.get("fecha") or "")[:10]
        pac = h.get("id_paciente_medilink") or (self.med.get(idc) or {}).get("id_paciente")
        if pac and f and (int(pac), f) in self.pagos and f <= self.hoy:
            return "atendida", "caja"
        if _cancelada(h):
            return "anulada", "bot"
        if f >= self.hoy:
            return "pendiente", "agenda"
        ce = _utc_txt_epoch(h.get("created_at")) or 0
        if any(t >= ce for t in self.compras.get(clave or _clave(h.get("phone")), [])):
            return "atendida", "proxy"
        return "sin_dato", ""

    def grupo(self, ci: dict) -> tuple[str, str]:
        """Mejor desenlace entre la cita y sus reagendamientos."""
        mejor = ("sin_dato", "")
        for h in ci.get("hermanas") or [ci]:
            d = self.una(h, ci.get("clave", ""))
            if _RANGO_DES[d[0]] > _RANGO_DES[mejor[0]]:
                mejor = d
        return mejor


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
    vistas: dict = {}
    out = []
    for r in filas:
        d = dict(r)
        k = (_clave(d["phone"]), (d["especialidad"] or "").strip().lower())
        if k in vistas:
            vistas[k]["hermanas"].append(dict(r))   # reagendamiento: misma cita lógica
            continue
        info = _info_ad(mapa, d["ad_source_id"], d["ad_headline"] or "")
        if not _pasa_filtros(info, d["ad_plataforma"], campana, plat):
            continue
        vistas[k] = d
        d["hermanas"] = [dict(r)]
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


VENTANA_TELEFONO_DIAS = 90   # el primer pago debe caer ≤90 días después del clic


def _pacientes_por_telefono(c, claves: set[str]) -> dict[str, set[int]]:
    """clave de teléfono → ids de paciente Medilink que tienen ese número.

    Fuentes: ficha de Medilink (`pacientes_heatmap.celular`, heatmap_cache.db,
    se refresca cada madrugada), teléfonos de las citas que agenda recepción
    (`citas_recepcion_reminders`), `pacientes_sin_optin` y `citas_bot`. Un
    teléfono puede tener varios pacientes (la mamá que agenda a sus hijos):
    se toman todos — el anuncio trajo a la familia."""
    out: dict[str, set[int]] = defaultdict(set)

    def _add(tel, pid):
        k = _clave(str(tel or ""))
        if pid and k in claves:
            out[k].add(int(pid))

    for q in ("SELECT phone, id_paciente FROM citas_recepcion_reminders",
              "SELECT celular, id_paciente_medilink FROM pacientes_sin_optin",
              "SELECT phone, id_paciente_medilink FROM citas_bot"):
        try:
            for r in c.execute(q):
                _add(r[0], r[1])
        except Exception:
            pass
    try:
        import sqlite3
        import session as _s
        ruta = Path(_s.DB_PATH).parent / "heatmap_cache.db"
        if ruta.exists():
            h = sqlite3.connect(f"file:{ruta}?mode=ro", uri=True)
            try:
                for tel, pid in h.execute("SELECT celular, id FROM pacientes_heatmap WHERE celular IS NOT NULL"):
                    _add(tel, pid)
            finally:
                h.close()
    except Exception as e:
        log.warning("campanas_meta: directorio de pacientes no disponible: %s", e)
    return out


ID_IMAGENDENT = -1   # línea propia en el desglose por profesional

# ── Ortodoncia: instalaciones atribuidas ────────────────────────────────────
# Una persona del anuncio/web "instaló" si, DESPUÉS de su primer contacto:
#   - la caja tiene un cobro de la ortodoncista (Dra. Castillo, id 66) de
#     instalación (≥ $80.000, misma frontera que ortodoncia_routes), o
#     `ortodoncia_cache` la marca instalación, o
#   - el embudo de ortodoncia la tiene en 'instalado' (o 'en_tratamiento')
#     desde una fecha posterior al contacto.
# Quien ya pagaba a la ortodoncista ANTES del contacto es paciente en curso:
# no cuenta como instalación traída por el anuncio.
ORTODONCISTA = 66
INSTALACION_MIN = 80000
VENTANA_INSTALACION_DIAS = 365


def _instalaciones(c, clics: dict[str, tuple[int, str]]) -> dict[str, dict]:
    """clave → {"fecha", "venta_orto", "fuente"} de quienes instalaron."""
    if not clics:
        return {}
    por_tel = _pacientes_por_telefono(c, set(clics))
    pid_k: dict[int, set[str]] = defaultdict(set)
    for k, pids in por_tel.items():
        for pid in pids:
            pid_k[pid].add(k)
    orto: dict[str, list[tuple[str, int]]] = defaultdict(list)   # clave → [(fecha, monto)]
    marc: dict[str, list[str]] = defaultdict(list)               # clave → fechas de instalación (cache)
    ids = list(pid_k)
    for i in range(0, len(ids), 500):
        lote = ids[i:i + 500]
        ph = ",".join("?" * len(lote))
        for q in (f"SELECT id_paciente, fecha, monto FROM bi_pagos_caja WHERE id_profesional=? AND id_paciente IN ({ph})",):
            try:
                for r in c.execute(q, (ORTODONCISTA, *lote)):
                    for k in pid_k[r[0]]:
                        orto[k].append(((r[1] or "")[:10], int(r[2] or 0)))
            except Exception:
                pass
        try:
            for r in c.execute(f"SELECT id_paciente, fecha, total FROM bi_atenciones WHERE id_profesional=? "
                               f"AND id_paciente IN ({ph})", (ORTODONCISTA, *lote)):
                if int(r[2] or 0) >= INSTALACION_MIN:
                    for k in pid_k[r[0]]:
                        marc[k].append((r[1] or "")[:10])
        except Exception:
            pass
        try:
            for r in c.execute(f"SELECT id_paciente, fecha FROM ortodoncia_cache WHERE tipo='instalacion' "
                               f"AND id_paciente IN ({ph})", lote):
                for k in pid_k[r[0]]:
                    marc[k].append((r[1] or "")[:10])
        except Exception:
            pass
    embudo: dict[str, tuple[str, str]] = {}
    try:
        for r in c.execute("SELECT phone, telefono, etapa, etapa_desde FROM orto_embudo "
                           "WHERE etapa IN ('instalado','en_tratamiento')"):
            for tel in (r[0], r[1]):
                k = _clave(str(tel or ""))
                if k in clics:
                    embudo[k] = (r[2], (r[3] or "")[:10])
    except Exception:
        pass
    out: dict[str, dict] = {}
    for k, (ts, _ad) in clics.items():
        dia = datetime.fromtimestamp(ts, _CL).date()
        d0, lim = dia.isoformat(), (dia + timedelta(days=VENTANA_INSTALACION_DIAS)).isoformat()
        previos = [f for f, m in orto.get(k, []) if f < d0 and m > 0]
        if previos:
            continue   # ya era paciente de ortodoncia
        inst = sorted([f for f, m in orto.get(k, []) if d0 <= f <= lim and m >= INSTALACION_MIN]
                      + [f for f in marc.get(k, []) if d0 <= f <= lim])
        fuente = "caja" if inst else ""
        if not inst and k in embudo and embudo[k][1] >= d0:
            inst, fuente = [embudo[k][1]], "embudo"
        if inst:
            out[k] = {"fecha": inst[0], "fuente": fuente,
                      "venta_orto": sum(m for f, m in orto.get(k, []) if f >= d0)}
    return out

# ── Especialidad del anuncio vs. lo que se vendió ───────────────────────────
# Grupos (no especialidades sueltas): psiquiatría y psicología son "Salud
# mental", ortodoncia/endodoncia/Imagendent son "Dental". El orden importa:
# lo específico antes que lo genérico ("salud mental" antes que "salud").
_GRUPOS = [
    ("Salud mental", ("salud mental", "psicolog", "psiquiatr", "ansiedad", "depresi")),
    ("Dental", ("dental", "odontolog", "ortodon", "orto ", "orto-", "orto battle", "endodon", "implant",
                "bracket", "diente", "sonrisa", "imagendent", "radiografia", "estetica facial")),
    ("Ecografía", ("ecograf", "ecotomograf", "eco ", "eco·", "doppler")),
    ("Kinesiología", ("kinesio", "kine ", "kine·", "kine-", "masoterap", "rehabilit")),
    ("Nutrición", ("nutri", "diabet")),
    ("Cardiología", ("cardio",)),
    ("Traumatología", ("traumato", "tramatolog")),
    ("Gastroenterología", ("gastro",)),
    ("Neurología", ("neurolog",)),
    ("Ginecología", ("ginecolog", "matrona")),
    ("Otorrino y fono", ("otorrino", "fonoaudio")),
    ("Medicina general", ("medicina general", "medicina familiar", "mg ", "mg·", "medico", "bono fonasa",
                          "problemas de salud", "malos habitos", "consulta medica", "hora hoy")),
]


def _sin_tildes(t: str) -> str:
    import unicodedata
    return "".join(ch for ch in unicodedata.normalize("NFD", (t or "").lower())
                   if unicodedata.category(ch) != "Mn")


def _grupo(texto: str | None) -> str | None:
    """Grupo cuya palabra clave aparece PRIMERO en el texto. Los textos de los
    anuncios suelen enumerar varias especialidades ("contamos con medicina
    general, otorrino…"): manda la que el anuncio nombra antes."""
    t = " " + _sin_tildes(texto or "").replace("\n", " ") + " "
    mejor, pos = None, len(t) + 1
    for g, claves in _GRUPOS:
        for k in claves:
            i = t.find(k)
            if 0 <= i < pos:
                mejor, pos = g, i
    return mejor


def _grupo_prof(pid: int) -> str | None:
    if pid == ID_IMAGENDENT:
        return "Dental"
    try:
        from medilink import PROFESIONALES
        esp = PROFESIONALES.get(pid, {}).get("especialidad", "")
    except Exception:
        esp = ""
    return _grupo(esp) or (esp or None)


def _nombres_profesionales(c) -> dict[int, str]:
    m: dict[int, str] = {ID_IMAGENDENT: "Imagendent (radiografías)"}
    try:
        from medilink import PROFESIONALES
        m.update({k: v.get("nombre", "") for k, v in PROFESIONALES.items()})
    except Exception:
        pass
    try:
        m.update({r[0]: r[1] for r in c.execute(
            "SELECT id_medilink, nombre FROM equipo_cmc WHERE id_medilink IS NOT NULL AND nombre != ''")})
    except Exception:
        pass
    return m


def _venta_por_anuncio(c, citas: list[dict], clics: dict[str, tuple[int, str]]) -> dict[str, dict]:
    """Venta y margen por anuncio. Un paciente → un anuncio.

    Dos caminos para saber que una persona del anuncio se atendió:
      1. bot: agendó por el bot y la cita quedó atribuida → desde ese día.
      2. teléfono: escribió desde el anuncio y su número está en la ficha de
         un paciente que pagó en caja dentro de 90 días desde el clic (lo
         agendó recepción, llamó por teléfono, llegó directo) → desde el clic.
    Se cuenta todo lo pagado desde ese día hasta hoy.
    `clics`: clave de teléfono → (epoch del primer clic en el rango, ad_id)."""
    inicio: dict[int, tuple[str, str, str]] = {}   # pid → (ad_id, desde, camino)
    for ci in citas:
        pid = ci.get("id_paciente_medilink")
        if pid and pid not in inicio:
            dia = datetime.fromtimestamp(ci["created_epoch"], _CL).date().isoformat()
            inicio[int(pid)] = (ci["ad_id"], dia, "bot")
    por_tel = _pacientes_por_telefono(c, set(clics))
    for k in sorted(clics, key=lambda x: clics[x][0]):
        ts, ad_id = clics[k]
        dia = datetime.fromtimestamp(ts, _CL).date().isoformat()
        for pid in por_tel.get(k, ()):
            if pid not in inicio:
                inicio[pid] = (ad_id, dia, "telefono")

    pct = _pct_honorarios(c)
    pagos: dict[int, list] = defaultdict(list)
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
            ad_id, dia, _ = inicio[r["id_paciente"]]
            if (r["fecha"] or "")[:10] >= dia:
                pagos[r["id_paciente"]].append(r)

    out: dict[str, dict] = defaultdict(lambda: {"venta": 0, "centro": 0.0, "pagaron": 0, "pagaron_tel": 0,
                                                "profs": {}})
    contados: set[int] = set()
    for pid, filas in pagos.items():
        ad_id, dia, camino = inicio[pid]
        if camino == "telefono":
            primero = min((f["fecha"] or "")[:10] for f in filas)
            limite = (date.fromisoformat(dia) + timedelta(days=VENTANA_TELEFONO_DIAS)).isoformat()
            if primero > limite:
                continue
        contados.add(pid)
        o = out[ad_id]
        o["pagaron"] += 1
        o["pagaron_tel"] += 1 if camino == "telefono" else 0
        for f in filas:
            monto = int(f["monto"] or 0)
            p_prof = pct.get(f["id_profesional"]) or PCT_HONORARIO_DEFAULT
            centro = monto * (100 - p_prof) / 100
            o["venta"] += monto
            o["centro"] += centro
            pp = o["profs"].setdefault(f["id_profesional"], {"venta": 0, "centro": 0.0, "pacientes": set()})
            pp["venta"] += monto
            pp["centro"] += centro
            pp["pacientes"].add(pid)

    # Imagendent: las radiografías se cobran en la atención del profesional
    # (Javiera, Daniela…), pero el examen lo hace Imagendent y al centro le
    # queda venta − costo del convenio, sin honorario. Se mueven de la línea
    # del profesional a su propia línea (la venta total no cambia).
    try:
        filas = []
        ids_ok = list(contados)
        for i in range(0, len(ids_ok), 500):
            lote = ids_ok[i:i + 500]
            filas += c.execute(
                "SELECT id_paciente, id_profesional, fecha, venta, cobrado, costo FROM convenio_consumo "
                "WHERE convenio='imagendent' AND id_paciente IN (%s)" % ",".join("?" * len(lote)), lote).fetchall()
    except Exception:
        filas = []
    for r in filas:
        pid = r["id_paciente"]
        ad_id, dia, _ = inicio[pid]
        o = out.get(ad_id)
        if not o or pid not in contados or (r["fecha"] or "")[:10] < dia:
            continue
        monto = int(r["cobrado"] or r["venta"] or 0)
        if not monto:
            continue
        prof = o["profs"].get(r["id_profesional"])
        if prof and prof["venta"] >= monto:
            p_prof = pct.get(r["id_profesional"]) or PCT_HONORARIO_DEFAULT
            prof["venta"] -= monto
            prof["centro"] -= monto * (100 - p_prof) / 100
            o["centro"] -= monto * (100 - p_prof) / 100
            if prof["venta"] <= 0:
                o["profs"].pop(r["id_profesional"])
        else:
            continue   # no se encontró el cobro en caja: no se inventa
        im = o["profs"].setdefault(ID_IMAGENDENT, {"venta": 0, "centro": 0.0, "pacientes": set()})
        im["venta"] += monto
        im["centro"] += monto - int(r["costo"] or 0)
        im["pacientes"].add(pid)
        o["centro"] += monto - int(r["costo"] or 0)
    return dict(out)


# ── Cómo nos conocieron (pregunta post-cita del bot) ────────────────────────
# Lo que el paciente DECLARA al agendar por primera vez (`contact_tags`
# referido:*). El antiguo "rrss" mezclaba redes sociales y Google en una sola
# opción; desde oct-2026 el bot separa facebook_instagram y google.
CONOCIERON = [
    ("amigo", "Amigo o familiar"),
    ("recurrente", "Ya era paciente"),
    ("facebook_instagram", "Facebook o Instagram"),
    ("google", "Google"),
    ("rrss", "Redes o Google (antiguo)"),
    ("codigo", "Código de referido"),
    ("otro", "Otra respuesta"),
]
_CONOC_LBL = dict(CONOCIERON)


def _conoc_id(tag: str) -> str:
    k = (tag or "").split(":", 1)[-1].strip().lower()
    return k if k in _CONOC_LBL else "otro"


def _tags_referido(c, desde_txt: str | None = None, hasta_txt: str | None = None) -> dict[str, tuple[str, int]]:
    """clave → (opción, epoch) de su respuesta MÁS RECIENTE (un teléfono puede
    tener más de un tag referido:*)."""
    q = "SELECT phone, tag, ts FROM contact_tags WHERE tag LIKE 'referido:%'"
    args: list = []
    if desde_txt:
        q += " AND ts >= ?"
        args.append(desde_txt)
    if hasta_txt:
        q += " AND ts < ?"
        args.append(hasta_txt)
    out: dict[str, tuple[str, int]] = {}
    try:
        filas = c.execute(q, args).fetchall()
    except Exception as e:  # pragma: no cover
        log.warning("campanas_meta: contact_tags no disponible: %s", e)
        return out
    for r in filas:
        k = _clave(r["phone"])
        ep = _utc_txt_epoch(r["ts"]) or 0
        if k not in out or ep >= out[k][1]:
            out[k] = (_conoc_id(r["tag"]), ep)
    return out


def conocieron_data(c, e0: int, e1: int) -> dict:
    """Reparto de respuestas en el rango (por fecha de la respuesta), y de
    cada opción cuántos habían tocado un anuncio ANTES de responder."""
    tags = _tags_referido(c, _utc_txt(e0), _utc_txt(e1))
    primer_clic: dict[str, int] = {}
    if tags:
        for r in c.execute("SELECT phone, MIN(ts) FROM meta_referrals GROUP BY phone"):
            k = _clave(r[0])
            if k in tags and (k not in primer_clic or r[1] < primer_clic[k]):
                primer_clic[k] = r[1]
    acc = {cid: {"id": cid, "label": lbl, "n": 0, "con_anuncio": 0} for cid, lbl in CONOCIERON}
    for k, (cid, ep) in tags.items():
        a = acc[cid]
        a["n"] += 1
        if k in primer_clic and primer_clic[k] <= ep:
            a["con_anuncio"] += 1
    total = len(tags)
    opciones = []
    for cid, _ in CONOCIERON:
        a = acc[cid]
        if not a["n"] and cid not in ("amigo", "recurrente", "facebook_instagram", "google"):
            continue
        a["sin_anuncio"] = a["n"] - a["con_anuncio"]
        a["pct"] = round(100 * a["n"] / total) if total else None
        opciones.append(a)
    return {"total": total, "con_anuncio": sum(a["con_anuncio"] for a in acc.values()), "opciones": opciones}


def _conoc_resumen(cnt: dict[str, int]) -> dict | None:
    n = sum(cnt.values())
    if not n:
        return None
    return {"respondieron": n,
            "amigo_pct": round(100 * cnt.get("amigo", 0) / n),
            "recurrente_pct": round(100 * cnt.get("recurrente", 0) / n),
            "detalle": [{"id": cid, "label": lbl, "n": cnt[cid], "pct": round(100 * cnt[cid] / n)}
                        for cid, lbl in CONOCIERON if cnt.get(cid)]}


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


# ── Rapidez de recepción ────────────────────────────────────────────────────
# Por persona del rango: minutos HÁBILES desde que necesitó a recepción hasta
# la primera respuesta humana (app/recepcion_tiempos.py), y si agendó (cita
# del bot desde su llegada, ≤90 días, o pago en caja ≤90 días). La curva cruza
# ambas cosas: ¿agenda más quien recibe respuesta rápida?

def _rapidez(c, clics: dict[str, tuple[int, str]], hasta_epoch: int) -> dict[str, dict]:
    import recepcion_tiempos as rt
    if not clics:
        return {}
    t_min = min(v[0] for v in clics.values()) - 3600
    msgs: dict[str, list[dict]] = defaultdict(list)
    for r in c.execute("SELECT phone, direction, text, state, ts FROM messages WHERE ts >= ? AND ts < ?",
                       (_utc_txt(t_min), _utc_txt(hasta_epoch + VENTANA_TELEFONO_DIAS * 86400))):
        k = _clave(r["phone"])
        if k in clics:
            msgs[k].append({"ts": _utc_txt_epoch(r["ts"]) or 0, "dir": r["direction"], "texto": r["text"] or "",
                            "state": r["state"] or ""})
    agendo: set[str] = set()
    for r in c.execute("SELECT phone, created_at FROM citas_bot WHERE created_at >= ?", (_utc_txt(t_min),)):
        k = _clave(r["phone"])
        if k in clics:
            ce = _utc_txt_epoch(r["created_at"]) or 0
            ts = clics[k][0]
            if ts - 3600 <= ce <= ts + VENTANA_TELEFONO_DIAS * 86400:
                agendo.add(k)
    por_tel = _pacientes_por_telefono(c, set(clics) - agendo)
    for k, pids in por_tel.items():
        if not pids:
            continue
        dia = datetime.fromtimestamp(clics[k][0], _CL).date()
        q = ",".join("?" * len(pids))
        try:
            f = c.execute(f"SELECT MIN(fecha) FROM bi_pagos_caja WHERE id_paciente IN ({q}) AND fecha >= ?",
                          (*pids, dia.isoformat())).fetchone()[0]
        except Exception:
            f = None
        if f and f[:10] <= (dia + timedelta(days=VENTANA_TELEFONO_DIAS)).isoformat():
            agendo.add(k)
    out = {}
    for k, (ts, ad_id) in clics.items():
        r = rt.respuesta_humana(msgs.get(k, []), desde=ts - 60)
        out[k] = {**r, "agendo": k in agendo, "ad_id": ad_id}
    return out


def _rapidez_resumen(filas: list[dict]) -> dict:
    import recepcion_tiempos as rt
    nec = [f for f in filas if f["necesito"]]
    resp = [f["minutos"] for f in nec if f["respondida"]]
    solo_bot = [f for f in filas if not f["necesito"] and not f.get("proactiva")]
    return {"necesitaron": len(nec), "respondidas": len(resp),
            "proactivas": sum(1 for f in filas if f.get("proactiva")),
            "sin_respuesta": sum(1 for f in nec if not f["respondida"]),
            "mediana_min": rt.mediana(resp),
            "curva": rt.curva(nec),
            "solo_bot": len(solo_bot), "solo_bot_agendaron": sum(1 for f in solo_bot if f["agendo"])}


def _acum() -> dict:
    return {"gasto": 0.0, "impresiones": 0, "alcance": 0, "clics": 0, "conv": 0,
            "personas": set(), "citas": 0, "atendidos": 0, "no_asistio": 0, "anuladas": 0,
            "por_proxy": 0, "con_medilink": 0, "instalaron": set(), "orto_venta": 0, "venta": 0, "centro": 0,
            "pagaron": 0, "pagaron_tel": 0, "profs": {}}


_NOMBRES_PROF: dict[int, str] = {}


def _sumar_profs(dst: dict, src: dict) -> None:
    for pid, v in src.items():
        d = dst.setdefault(pid, {"venta": 0, "centro": 0.0, "pacientes": set()})
        d["venta"] += v["venta"]
        d["centro"] += v["centro"]
        d["pacientes"] |= v["pacientes"]


def _por_grupo(a: dict) -> list[dict]:
    """Venta agrupada por especialidad (grupo) — qué se terminó atendiendo."""
    acc: dict[str, dict] = {}
    for pid, v in a["profs"].items():
        g = _grupo_prof(pid) or "Otra"
        d = acc.setdefault(g, {"grupo": g, "venta": 0, "centro": 0.0, "pacientes": set()})
        d["venta"] += v["venta"]
        d["centro"] += v["centro"]
        d["pacientes"] |= v["pacientes"]
    out = []
    for d in acc.values():
        out.append({"grupo": d["grupo"], "venta": round(d["venta"]), "centro": round(d["centro"]),
                    "pacientes": len(d["pacientes"]),
                    "del_anuncio": d["grupo"] == a.get("grupo")})
    return sorted(out, key=lambda x: (not x["del_anuncio"], -x["venta"]))


def _cerrar(a: dict) -> dict:
    g = round(a["gasto"])
    citas, aten = a["citas"], a["atendidos"]
    return {
        "gasto": g, "impresiones": a["impresiones"], "clics": a["clics"],
        "conversaciones": a["conv"], "personas": len(a["personas"]),
        "citas": citas, "atendidos": aten,
        "no_asistio": a["no_asistio"], "anuladas": a["anuladas"],
        "no_asistio_pct": round(100 * a["no_asistio"] / (aten + a["no_asistio"])) if (aten + a["no_asistio"]) else None,
        "atendidos_proxy": a["por_proxy"], "con_medilink": a["con_medilink"],
        "instalaron": len(a["instalaron"]), "orto_venta": round(a["orto_venta"]),
        "cac_conv": _div(g, a["conv"]) if g else None, "cac_cita": _div(g, citas) if g else None,
        "cac_atendido": _div(g, aten) if g else None,
        "venta": round(a["venta"]),
        "retorno": round(a["venta"] / g, 2) if g else None,
        # lo que le queda al centro (tras honorarios) por cada $1 de gasto en Meta;
        # bajo 1× el anuncio no se paga solo
        "retorno_centro": round(a["centro"] / g, 2) if g else None,
        "centro": round(a["centro"]),
        "centro_pct": round(100 * a["centro"] / a["venta"]) if a["venta"] else None,
        "resultado": round(a["centro"]) - g,
        "pagaron": a["pagaron"], "pagaron_tel": a["pagaron_tel"],
        "grupo": a.get("grupo"),
        "fuera_venta": round(a.get("fuera", 0)),
        "fuera_pct": round(100 * a.get("fuera", 0) / a["venta"]) if a["venta"] and a.get("grupo") else None,
        "por_especialidad": _por_grupo(a),
        "por_profesional": sorted(
            ({"id": pid, "nombre": _NOMBRES_PROF.get(pid) or f"Profesional {pid}",
              "grupo": _grupo_prof(pid),
              "fuera": a.get("grupo") not in (None, "Varias", "mixto") and _grupo_prof(pid) != a.get("grupo"),
              "venta": round(v["venta"]), "centro": round(v["centro"]),
              "centro_pct": round(100 * v["centro"] / v["venta"]) if v["venta"] else None,
              "pacientes": len(v["pacientes"])}
             for pid, v in a["profs"].items()), key=lambda x: -x["venta"]),
        "frecuencia": round(a["impresiones"] / a["alcance"], 2) if a["alcance"] else None,
        "muestra_chica": citas < MUESTRA_CHICA,
        "conocieron": _conoc_resumen(a.get("conoc") or {}),
    }


def _orden_cac(x: dict):
    # CAC por cita de menor a mayor; sin citas pero con gasto al final (el peor
    # caso: plata sin resultado), y sin gasto ni citas al fondo.
    if x["cac_cita"] is not None:
        return (0, x["cac_cita"])
    if x["gasto"] > 0:
        return (1, -x["gasto"])
    return (2, -x["personas"])


def _orden_canal(x: dict):
    # Anuncios por costo por cita; la web (sin gasto) al final, por lo que
    # dejó para el centro y luego por personas.
    if x.get("canal") == "web":
        return (3, -x["centro"], -x["personas"])
    return _orden_cac(x)


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
               campana: str | None = None, plataforma: str | None = None,
               canal: str | None = None) -> dict:
    d, h = _rango(desde, hasta)
    canal = _canal(canal)
    plat = plataforma if plataforma in (*_PLATS, "sin_dato") else None
    if canal == "web":
        plat = None   # la web no tiene plataforma
    e0, e1 = _epoch_ini(d), _epoch_fin(h)
    with db() as c:
        mapa = _mapa_anuncios(c)
        ins = _insights_filas(c, d, h, campana, plat if plat in _PLATS else None)
        # 'sin_dato' no existe en Meta: el gasto no se puede filtrar así.
        # La web no tiene gasto publicitario registrado.
        if plat == "sin_dato" or canal == "web" or _es_web_camp(campana):
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

        # Personas captadas: teléfonos únicos que llegaron por el canal.
        clics: dict[str, tuple[int, str]] = {}   # clave → (primer contacto, ad_id)
        origen_k: dict[str, str] = {}
        for r in _llegadas(c, canal, e0, e1, primera_web=True):
            info = _info(mapa, r["source_id"] or "", r["headline"] or "")
            if not _pasa_filtros(info, r["plataforma"], campana, plat):
                continue
            k = _clave(r["phone"])
            por_ad[info["ad_id"]]["personas"].add(k)
            tot["personas"].add(k)
            origen_k.setdefault(k, r["origen"])
            if k not in clics or r["ts"] < clics[k][0]:
                clics[k] = (r["ts"], info["ad_id"])

        citas = []
        if canal in ("meta", "todos") and not _es_web_camp(campana):
            citas = _citas_atribuidas(c, e0, e1, mapa, campana, plat)
            if canal == "todos":   # quien llegó primero por la web cuenta solo allá
                citas = [ci for ci in citas if origen_k.get(ci["clave"]) != "web"]
        if canal in ("web", "todos"):
            citas += _citas_web(c, {k: v for k, v in clics.items() if v[1].startswith("web:")}, e1)
        compras = _purchases(c, e0)
        des_c = Desenlaces(c, citas, _hoy().isoformat(), compras)
        for ci in citas:
            ci["desenlace"], ci["fuente"] = des_c.grupo(ci)
            ci["atendido"] = ci["desenlace"] == "atendida"
            for a in (por_ad[ci["ad_id"]], tot):
                a["citas"] += 1
                a["atendidos"] += 1 if ci["atendido"] else 0
                a["no_asistio"] += 1 if ci["desenlace"] == "no_show" else 0
                a["anuladas"] += 1 if ci["desenlace"] == "anulada" else 0
                a["por_proxy"] += 1 if ci["fuente"] == "proxy" else 0
                a["con_medilink"] += 1 if ci["fuente"] == "medilink" else 0

        for k, v in _instalaciones(c, clics).items():
            for a in (por_ad[clics[k][1]], tot):
                a["instalaron"].add(k)
                a["orto_venta"] += v["venta_orto"]

        _NOMBRES_PROF.clear()
        _NOMBRES_PROF.update(_nombres_profesionales(c))
        for ad_id, v in _venta_por_anuncio(c, citas, clics).items():
            for a in (por_ad[ad_id], tot):
                for kk in ("venta", "centro", "pagaron", "pagaron_tel"):
                    a[kk] += v[kk]
                _sumar_profs(a["profs"], v["profs"])

        # Especialidad del anuncio (nombre/título; si no dice, la más agendada)
        # y cuánto de su venta cayó en OTRA especialidad.
        esp_citas: dict[str, list[str]] = defaultdict(list)
        for ci in citas:
            g = _grupo(ci.get("especialidad"))
            if g:
                esp_citas[ci["ad_id"]].append(g)
        for ad_id, a in por_ad.items():
            if ad_id.startswith("web:"):
                g = _info_web(ad_id)["esp_boton"] or None
                if not g and esp_citas.get(ad_id):
                    g = max(set(esp_citas[ad_id]), key=esp_citas[ad_id].count)
                a["grupo"] = g
                a["fuera"] = sum(v["venta"] for pid, v in a["profs"].items() if g and _grupo_prof(pid) != g)
                continue
            info = _info_ad(mapa, ad_id)
            ref = c.execute("SELECT headline, body FROM meta_referrals WHERE source_id=? "
                            "ORDER BY ts DESC LIMIT 1", (ad_id,)).fetchone() if ad_id else None
            # El texto del anuncio (body) dice de qué es; el título suele ser
            # solo "Centro Médico Carampangue" y caería en Medicina general.
            nombre = info["anuncio"] if ad_id in mapa else (ref["headline"] if ref else "")
            g = (_grupo(nombre) or _grupo((ref["body"] or "")[:400] if ref else "")
                 or _grupo(info["campana"]))
            if not g and esp_citas.get(ad_id):
                g = max(set(esp_citas[ad_id]), key=esp_citas[ad_id].count)
            a["grupo"] = g
            a["fuera"] = sum(v["venta"] for pid, v in a["profs"].items() if g and _grupo_prof(pid) != g)
        tot["fuera"] = sum(a.get("fuera", 0) for a in por_ad.values())
        tot["grupo"] = "mixto"

        # Cómo dicen que nos conocieron las personas de cada anuncio (su
        # respuesta más reciente a la pregunta post-cita, cuando la hay).
        tags_all = _tags_referido(c)
        for a in list(por_ad.values()) + [tot]:
            cnt: dict[str, int] = defaultdict(int)
            for k in a["personas"]:
                if k in tags_all:
                    cnt[tags_all[k][0]] += 1
            a["conoc"] = cnt
        conocieron = conocieron_data(c, e0, e1)

        rap = _rapidez(c, clics, e1)
        rap_ad: dict[str, list[dict]] = defaultdict(list)
        for v in rap.values():
            rap_ad[v["ad_id"]].append(v)
        rapidez = _rapidez_resumen(list(rap.values()))

        # Tabla por anuncio y por campaña
        anuncios, por_camp = [], defaultdict(_acum)
        camp_nombre: dict[str, str] = {}
        for ad_id, a in por_ad.items():
            info = _info(mapa, ad_id)
            if ad_id and ad_id not in mapa and not ad_id.startswith("web:"):
                # nombre desde el headline del referral, si lo hay
                hl = c.execute("SELECT headline FROM meta_referrals WHERE source_id=? AND headline != '' "
                               "ORDER BY ts DESC LIMIT 1", (ad_id,)).fetchone()
                if hl:
                    info["anuncio"] = hl[0]
            rr = _rapidez_resumen(rap_ad.get(ad_id, []))
            a["rap"] = rap_ad.get(ad_id, [])
            fila = {**info, **_cerrar(a), "resp_mediana_min": rr["mediana_min"], "resp_necesitaron": rr["necesitaron"],
                    "resp_sin": rr["sin_respuesta"], "resp_curva": rr["curva"], "canal": "web" if ad_id.startswith("web:") else "meta"}
            anuncios.append(fila)
            cid = info["campaign_id"]
            camp_nombre[cid] = info["campana"]
            pc = por_camp[cid]
            for k in ("gasto", "impresiones", "alcance", "clics", "conv", "citas", "atendidos", "venta", "centro", "pagaron", "pagaron_tel",
                      "no_asistio", "anuladas", "por_proxy", "con_medilink", "orto_venta"):
                pc[k] += a[k]
            pc["instalaron"] |= a["instalaron"]
            pc.setdefault("rap", []).extend(a.get("rap", []))
            pc["personas"] |= a["personas"]
            _sumar_profs(pc["profs"], a["profs"])
            pcc = pc.setdefault("conoc", defaultdict(int))
            for kk, vv in a.get("conoc", {}).items():
                pcc[kk] += vv
            pc["fuera"] = pc.get("fuera", 0) + a.get("fuera", 0)
            pc.setdefault("grupos", set()).add(a.get("grupo"))
        for pc in por_camp.values():
            gs = pc.pop("grupos", set()) - {None}
            pc["grupo"] = next(iter(gs)) if len(gs) == 1 else ("Varias" if gs else None)
        def _rap_fields(lst):
            rr = _rapidez_resumen(lst)
            return {"resp_mediana_min": rr["mediana_min"], "resp_necesitaron": rr["necesitaron"],
                    "resp_sin": rr["sin_respuesta"], "resp_curva": rr["curva"]}
        campanas = [{"campaign_id": cid, "campana": camp_nombre[cid], "canal": "web" if _es_web_camp(cid) else "meta",
                     "n_anuncios": sum(1 for x in anuncios if x["campaign_id"] == cid),
                     **_cerrar(a), **_rap_fields(a.get("rap", []))} for cid, a in por_camp.items()]
        estados = _estados_meta()
        recientes = _con_gasto_reciente(c) if not estados else set()
        for x in anuncios:
            if x["canal"] == "web":   # la web no se pausa: siempre "activa"
                x["estado"], x["activo"] = "WEB", True
                continue
            st = estados.get(x["ad_id"]) if estados else None
            x["estado"] = st or ("ACTIVE" if x["ad_id"] in recientes else "")
            x["activo"] = (st == "ACTIVE") if estados else (x["ad_id"] in recientes)
        for cp in campanas:
            cp["activo"] = any(x["activo"] for x in anuncios if x["campaign_id"] == cp["campaign_id"])
        estado_fuente = "meta" if estados else "gasto_reciente"
        anuncios.sort(key=_orden_canal)
        campanas.sort(key=_orden_canal)

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
        tend_f = [] if (plat == "sin_dato" or canal == "web") else _insights_filas(c, t0, h, campana, plat if plat in _PLATS else None)
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

        if canal == "web":   # desgloses, tendencia y alertas son solo de Meta
            des = {kk: [] for kk in des}
            alertas = []
        _uf = c.execute("SELECT MAX(fecha) FROM meta_insights_diario").fetchone()[0]
        hay_insights = _uf is not None
        opciones = _opciones(c, mapa, canal)

    k = _cerrar(tot)
    return {
        "rango": {"desde": d.isoformat(), "hasta": h.isoformat(), "dias": (h - d).days + 1},
        "filtros": {"campana": campana or "", "plataforma": plat or "", "canal": canal},
        "canal": canal,
        "kpis": k,
        "campanas": campanas,
        "anuncios": anuncios,
        "desgloses": des,
        "tendencia": tendencia,
        "alertas": alertas,
        "estado_fuente": estado_fuente,
        "conocieron": conocieron,
        "rapidez": rapidez,
        "hay_insights": hay_insights,
        "ultima_foto": _uf,
        "opciones": opciones,
        "umbrales": {"gasto_sin_citas": UMBRAL_GASTO_SIN_CITAS, "frecuencia": UMBRAL_FRECUENCIA,
                     "muestra_chica": MUESTRA_CHICA},
        "medicion": {"atribucion_desde": ATRIBUCION_DESDE, "plataforma_desde": PLATAFORMA_DESDE,
                     "rango_antes_de_atribucion": d.isoformat() < ATRIBUCION_DESDE,
                     "rango_antes_de_plataforma": d.isoformat() < PLATAFORMA_DESDE},
    }


def _opciones(c, mapa: dict, canal: str = "meta") -> dict:
    camps: dict[str, str] = {}
    ads = []
    if canal in ("meta", "todos"):
        for m in mapa.values():
            if m["campaign_id"]:
                camps[m["campaign_id"]] = m["campaign_name"] or m["campaign_id"]
        ads = [{"id": a, "nombre": m["ad_name"] or a, "campaign_id": m["campaign_id"]}
               for a, m in mapa.items() if a]
    if canal in ("web", "todos"):
        for x in sorted({r["source_id"] for r in _llegadas_web(c)}):
            i = _info_web(x)
            camps[i["campaign_id"]] = i["campana"]
            ads.append({"id": x, "nombre": i["campana"] + " · " + i["anuncio"], "campaign_id": i["campaign_id"]})
    ads.sort(key=lambda x: x["nombre"].lower())
    esp = sorted({(r[0] or "").strip() for r in c.execute(
        "SELECT DISTINCT especialidad FROM citas_bot WHERE ad_source_id IS NOT NULL "
        "AND ad_source_id != ''" if canal == "meta" else
        "SELECT DISTINCT especialidad FROM citas_bot") if (r[0] or "").strip()}, key=str.lower)
    return {"campanas": sorted(({"id": k, "nombre": v} for k, v in camps.items()),
                               key=lambda x: x["nombre"].lower()),
            "anuncios": ads, "especialidades": esp}


# ── Google Search Console × canal Página web ────────────────────────────────
# Google no entrega datos por persona: el cruce es POR PÁGINA. Cada URL de
# Search Console se lleva a la misma clave (página, artículo) que usa el
# marcador "(web: página · artículo · botón)" del sitio (gsc_snapshot.
# pagina_de_url), y se pone al lado de lo que pasó con quienes escribieron por
# WhatsApp desde esa página (personas, citas, venta, para el centro).
# Todo excluye la basura del hackeo antiguo (/products/, consultas CJK).
OPORT_MIN_APARICIONES = 30
OPORT_POS = (4.0, 20.0)


def _grupo_consulta(q: str) -> str:
    t = " " + " ".join(re.sub(r"[^a-z ]+", " ", _sin_tildes(q)).split()) + " "
    com = next((lbl for lbl, k in _COMUNAS if f" {k} " in t), None)
    if any(k in t for k in (" centro medico ", " clinica ", " cmc ", " consultorio ")) or t.strip() == (com or "").lower():
        return "Marca: centro médico" + (f" · {com}" if com else "")
    if any(k in t for k in (" pni ", " vacuna", " vacunacion ")):
        return "Vacunas" + (f" · {com}" if com else "")
    esp = _grupo(q)
    if esp and com:
        return f"{esp} · {com}"
    return esp or (f"Centro médico · {com}" if com else "General")


def google_data(desde: str | None = None, hasta: str | None = None) -> dict:
    import gsc_snapshot as gs
    d, h = _rango(desde, hasta)
    with db() as c:
        gs.ensure_tables(c)
        ult = c.execute("SELECT MAX(fecha) FROM gsc_paginas_diario").fetchone()[0]
        por_url = c.execute("SELECT page, SUM(clicks), SUM(impressions), SUM(position * impressions) "
                            "FROM gsc_paginas_diario WHERE basura=0 AND fecha >= ? AND fecha <= ? GROUP BY page",
                            (d.isoformat(), h.isoformat())).fetchall()
        basura = c.execute("SELECT COALESCE(SUM(clicks),0), COALESCE(SUM(impressions),0) FROM gsc_paginas_diario "
                           "WHERE basura=1 AND fecha >= ? AND fecha <= ?", (d.isoformat(), h.isoformat())).fetchone()
        consultas = c.execute("SELECT page, query, SUM(clicks), SUM(impressions), SUM(position * impressions) "
                              "FROM gsc_diario WHERE basura=0 AND fecha >= ? AND fecha <= ? GROUP BY page, query",
                              (d.isoformat(), h.isoformat())).fetchall()
        _m = h.year * 12 + h.month - 1 - 11
        t0 = date(_m // 12, _m % 12 + 1, 1)
        tend = c.execute("SELECT substr(fecha,1,7), SUM(clicks), SUM(impressions), SUM(position * impressions) "
                         "FROM gsc_paginas_diario WHERE basura=0 AND fecha >= ? AND fecha <= ? GROUP BY 1 ORDER BY 1",
                         (t0.isoformat(), h.isoformat())).fetchall()

    def _clave_url(url):
        pag, art = gs.pagina_de_url(url)
        return pag, art

    filas: dict[tuple[str, str], dict] = {}

    def _f(k):
        return filas.setdefault(k, {"pagina_id": k[0], "articulo": k[1], "clicks": 0, "impressions": 0, "_pos": 0.0,
                                    "urls": set(), "consultas": [], "personas": 0, "citas": 0, "venta": 0,
                                    "centro": 0, "pagaron": 0})
    for url, cl, im, pw in por_url:
        f = _f(_clave_url(url))
        f["clicks"] += cl or 0
        f["impressions"] += im or 0
        f["_pos"] += pw or 0
        f["urls"].add(urlparse_path(url))
    for url, q, cl, im, pw in consultas:
        _f(_clave_url(url))["consultas"].append({"q": q, "clicks": cl or 0, "impressions": im or 0,
                                                 "pos": round((pw or 0) / im, 1) if im else None})
    # Lo que pasó con quienes escribieron por WhatsApp desde cada página
    pw_ = panel_data(d.isoformat(), h.isoformat(), canal="web")
    for a in pw_["anuncios"]:
        partes = (a["ad_id"].split(":") + ["", ""])[1:3]
        k = (partes[0], "" if partes[1] in ("-", "") else partes[1])
        f = _f(k)
        for kk in ("personas", "citas", "venta", "centro", "pagaron"):
            f[kk] += a.get(kk) or 0

    out = []
    for f in filas.values():
        im, cl = f["impressions"], f["clicks"]
        top = sorted(f["consultas"], key=lambda x: (-x["clicks"], -x["impressions"]))[:5]
        out.append({"pagina_id": f["pagina_id"], "pagina": _web_lbl(f["pagina_id"]), "articulo": f["articulo"],
                    "urls": sorted(f["urls"])[:3], "clicks": cl, "impressions": im,
                    "ctr": round(100 * cl / im, 1) if im else None,
                    "posicion": round(f["_pos"] / im, 1) if im else None,
                    "consultas": top, "personas": f["personas"], "citas": f["citas"],
                    "venta": round(f["venta"]), "centro": round(f["centro"]), "pagaron": f["pagaron"],
                    "tasa_escribio": round(100 * f["personas"] / cl, 1) if cl else None})
    # Agrupado por página (con sus artículos), más clics primero
    paginas: dict[str, dict] = {}
    for r in out:
        g = paginas.setdefault(r["pagina_id"], {"pagina_id": r["pagina_id"], "pagina": r["pagina"], "clicks": 0,
                                                "impressions": 0, "_pos": 0.0, "personas": 0, "citas": 0,
                                                "venta": 0, "centro": 0, "articulos": []})
        for kk in ("clicks", "impressions", "personas", "citas", "venta", "centro"):
            g[kk] += r[kk]
        g["_pos"] += (r["posicion"] or 0) * r["impressions"]
        g["articulos"].append(r)
    lista = []
    for g in paginas.values():
        im, cl = g.pop("impressions"), g["clicks"]
        g["impressions"] = im
        g["ctr"] = round(100 * cl / im, 1) if im else None
        g["posicion"] = round(g.pop("_pos") / im, 1) if im else None
        g["tasa_escribio"] = round(100 * g["personas"] / cl, 1) if cl else None
        g["articulos"].sort(key=lambda x: (-x["clicks"], -x["personas"], -x["impressions"]))
        lista.append(g)
    lista.sort(key=lambda x: (-x["clicks"], -x["personas"]))

    # Oportunidades: consultas con suficientes apariciones y posición 4-20
    acc: dict[str, dict] = {}
    for url, q, cl, im, pw in consultas:
        a = acc.setdefault(q, {"q": q, "clicks": 0, "impressions": 0, "_pos": 0.0, "paginas": defaultdict(int)})
        a["clicks"] += cl or 0
        a["impressions"] += im or 0
        a["_pos"] += pw or 0
        a["paginas"][url] += im or 0
    oport = []
    for a in acc.values():
        if a["impressions"] < OPORT_MIN_APARICIONES:
            continue
        pos = a["_pos"] / a["impressions"]
        if not (OPORT_POS[0] <= pos <= OPORT_POS[1]):
            continue
        top_url = max(a["paginas"].items(), key=lambda x: x[1])[0]
        pag, art = _clave_url(top_url)
        oport.append({"consulta": a["q"], "grupo": _grupo_consulta(a["q"]), "impressions": a["impressions"],
                      "clicks": a["clicks"], "posicion": round(pos, 1),
                      "ctr": round(100 * a["clicks"] / a["impressions"], 1),
                      "url": urlparse_path(top_url), "pagina": _web_lbl(pag), "articulo": art,
                      "pagina_1": pos < 10.5})
    oport.sort(key=lambda x: -x["impressions"])
    grupos: dict[str, dict] = {}
    for o in oport:
        g = grupos.setdefault(o["grupo"], {"grupo": o["grupo"], "consultas": 0, "impressions": 0, "clicks": 0})
        g["consultas"] += 1
        g["impressions"] += o["impressions"]
        g["clicks"] += o["clicks"]
    tot_cl = sum(g["clicks"] for g in lista)
    tot_im = sum(g["impressions"] for g in lista)
    return {
        "rango": {"desde": d.isoformat(), "hasta": h.isoformat()},
        "hay_datos": ult is not None, "ultima_fecha": ult,
        "totales": {"clicks": tot_cl, "impressions": tot_im,
                    "ctr": round(100 * tot_cl / tot_im, 1) if tot_im else None,
                    "personas": sum(g["personas"] for g in lista),
                    "basura_clicks": basura[0], "basura_impressions": basura[1]},
        "paginas": lista,
        "oportunidades": oport[:60],
        "oportunidades_grupos": sorted(grupos.values(), key=lambda x: -x["impressions"]),
        "tendencia": [{"mes": m, "clicks": cl or 0, "impressions": im or 0,
                       "posicion": round((pw or 0) / im, 1) if im else None} for m, cl, im, pw in tend],
        "umbrales": {"min_apariciones": OPORT_MIN_APARICIONES, "posicion": list(OPORT_POS)},
    }


def urlparse_path(url: str) -> str:
    from urllib.parse import urlparse
    p = urlparse(url or "")
    return (p.path or "/") if p.scheme else (url or "/")


# ── A quién llamar hoy ──────────────────────────────────────────────────────
# Lista priorizada para el dueño, encima del kanban (no lo cambia):
#   1. No asistió hace ≤ 7 días (Medilink) — recuperar la hora.
#   2. Vio horarios y no agendó hace < 3 días — todavía está caliente.
#   3. Seguimiento para hoy o vencido.
# Quien ya quedó cerrado en la gestión (agendó por otra vía / no le interesa)
# no aparece. Dentro de cada prioridad, lo más reciente primero.
LLAMAR_NO_SHOW_DIAS = 7
LLAMAR_VIO_HORAS_DIAS = 3


def llamar_hoy_data(desde: str | None = None, hasta: str | None = None, campana: str | None = None,
                    anuncio: str | None = None, plataforma: str | None = None,
                    especialidad: str | None = None, canal: str | None = None,
                    ahora: datetime | None = None) -> dict:
    kb = kanban_data(desde, hasta, campana, anuncio, plataforma, especialidad, None, ahora=ahora, canal=canal)
    out = []
    for col in kb["columnas"]:
        for t in col["tarjetas"]:
            g = t.get("gestion") or {}
            if g.get("estado") in _GESTION_CERRADA:
                continue
            motivos = []
            if t.get("desenlace") == "no_asistio" and t["dias_etapa"] <= LLAMAR_NO_SHOW_DIAS:
                motivos.append((1, "no_asistio", "No asistió " + ("hoy" if t["dias_etapa"] == 0 else
                                                                  f"hace {t['dias_etapa']} d") + ": reagendar"))
            if col["id"] == "vio_horas" and t["dias_etapa"] < LLAMAR_VIO_HORAS_DIAS:
                motivos.append((2, "vio_horas", "Vio horarios " + ("hoy" if t["dias_etapa"] == 0 else
                                                                   f"hace {t['dias_etapa']} d") + " y no agendó"))
            if g.get("alerta") in ("hoy", "vencido"):
                motivos.append((3, "seguimiento", "Seguimiento vencido" if g["alerta"] == "vencido"
                                else "Seguimiento para hoy"))
            if not motivos:
                continue
            motivos.sort()
            out.append({**t, "columna": col["id"], "columna_label": col["label"], "prioridad": motivos[0][0],
                        "motivo": motivos[0][1], "motivos": [m[2] for m in motivos]})
    out.sort(key=lambda x: (x["prioridad"], x["llegada_iso"]), reverse=False)
    # más reciente primero dentro de cada prioridad
    res = []
    for p in (1, 2, 3):
        res += sorted((x for x in out if x["prioridad"] == p), key=lambda x: x["llegada_iso"], reverse=True)
    return {"rango": kb["rango"], "total": len(res), "personas": res,
            "por_prioridad": {p: sum(1 for x in res if x["prioridad"] == p) for p in (1, 2, 3)}}


# ── Valor en el tiempo (cohortes por mes de llegada) ────────────────────────
# Cada persona entra a la cohorte del mes de su PRIMER contacto (anuncio o web)
# desde may-2026 (antes no hay atribución). Venta y "para el centro"
# acumulados a 30/90/180/365 días desde ese día, con la misma regla de la venta
# del panel: si agendó por el bot, todo lo que pagó desde el contacto; si no,
# solo si su primer pago cae ≤90 días después (cruce por teléfono). Gasto de la
# cohorte = gasto en Meta del mismo mes (de la campaña, si se agrupa por
# campaña); la web no tiene gasto. "Se pagó" = primer horizonte en que lo que
# quedó para el centro supera ese gasto.

COHORTES_DESDE = "2026-05-01"
HORIZONTES = (30, 90, 180, 365)


def cohortes_data(por: str = "canal", canal: str | None = "todos", hoy: date | None = None) -> dict:
    hoy = hoy or _hoy()
    por = por if por in ("canal", "campana") else "canal"
    canal = _canal(canal) if canal else "todos"
    d0 = date.fromisoformat(COHORTES_DESDE)
    e0, e1 = _epoch_ini(d0), _epoch_fin(hoy)
    with db() as c:
        mapa = _mapa_anuncios(c)
        primero: dict[str, dict] = {}
        for r in _llegadas(c, canal, e0, e1, primera_web=True):
            k = _clave(r["phone"])
            if k not in primero:
                primero[k] = r
        if not primero:
            return {"cohortes": [], "horizontes": list(HORIZONTES), "desde": COHORTES_DESDE, "por": por, "canal": canal}
        claves = set(primero)
        # agendó por el bot desde el contacto
        bot: set[str] = set()
        for r in c.execute("SELECT phone, created_at FROM citas_bot WHERE created_at >= ?", (_utc_txt(e0 - 3600),)):
            k = _clave(r["phone"])
            if k in primero and (_utc_txt_epoch(r["created_at"]) or 0) >= primero[k]["ts"] - 3600:
                bot.add(k)
        por_tel = _pacientes_por_telefono(c, claves)
        pid_k: dict[int, set[str]] = defaultdict(set)
        for k, pids in por_tel.items():
            for pid in pids:
                pid_k[pid].add(k)
        pagos: dict[str, list[tuple[str, int, int]]] = defaultdict(list)   # clave → (fecha, monto, id_prof)
        ids = list(pid_k)
        for i in range(0, len(ids), 500):
            lote = ids[i:i + 500]
            try:
                for r in c.execute("SELECT id_paciente, fecha, monto, id_profesional FROM bi_pagos_caja WHERE "
                                   "id_paciente IN (%s) AND fecha >= ?" % ",".join("?" * len(lote)),
                                   (*lote, COHORTES_DESDE)):
                    for k in pid_k[r[0]]:
                        pagos[k].append(((r[1] or "")[:10], int(r[2] or 0), r[3]))
            except Exception as e:
                log.warning("campanas_meta: cohortes sin caja: %s", e)
        pct = _pct_honorarios(c)
        # gasto por mes (total y por campaña)
        gasto_mes: dict[tuple[str, str], float] = defaultdict(float)
        _ensure_insights(c)
        for r in c.execute("SELECT substr(fecha,1,7), campaign_id, SUM(spend) FROM meta_insights_diario "
                           "WHERE desglose='total' AND fecha >= ? GROUP BY 1, 2", (COHORTES_DESDE,)):
            gasto_mes[(r[0], r[1] or "")] += r[2] or 0
            gasto_mes[(r[0], "*meta")] += r[2] or 0

    grupos: dict[tuple[str, str], dict] = {}
    for k, r in primero.items():
        dia = datetime.fromtimestamp(r["ts"], _CL).date()
        mes = dia.strftime("%Y-%m")
        info = _info(mapa, r["source_id"] or "", r["headline"] or "")
        if por == "canal":
            gid, glbl = ("web", "Página web") if r["origen"] == "web" else ("meta", "Anuncios Meta")
        elif r["origen"] == "web":
            gid, glbl = "web", "Página web"
        else:
            gid, glbl = info["campaign_id"] or "-", info["campana"]
        g = grupos.setdefault((mes, gid), {"cohorte": mes, "grupo_id": gid, "grupo": glbl, "personas": 0,
                                           "pacientes": 0, "volvieron": 0,
                                           "h": {h: {"venta": 0, "centro": 0.0} for h in HORIZONTES}})
        g["personas"] += 1
        ps = sorted(p for p in pagos.get(k, []) if p[0] >= dia.isoformat())
        if not ps:
            continue
        if k not in bot and ps[0][0] > (dia + timedelta(days=VENTANA_TELEFONO_DIAS)).isoformat():
            continue   # pagó, pero no dentro de 90 días del contacto: no es del anuncio
        g["pacientes"] += 1
        dias_pago = {p[0] for p in ps if p[0] <= (dia + timedelta(days=365)).isoformat()}
        if len(dias_pago) >= 2:
            g["volvieron"] += 1
        for h in HORIZONTES:
            lim = (dia + timedelta(days=h)).isoformat()
            for f, monto, prof in ps:
                if f <= lim:
                    g["h"][h]["venta"] += monto
                    g["h"][h]["centro"] += monto * (100 - (pct.get(prof) or PCT_HONORARIO_DEFAULT)) / 100

    out = []
    for (mes, gid), g in sorted(grupos.items(), key=lambda x: (x[0][0], x[1]["grupo"])):
        y, m = int(mes[:4]), int(mes[5:])
        fin_mes = (date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1))
        if gid == "web":
            gasto = 0.0
        elif por == "canal":
            gasto = gasto_mes.get((mes, "*meta"), 0.0)
        else:
            gasto = gasto_mes.get((mes, gid), 0.0)
        hs = []
        paga_en = None
        prev = 0
        for h in HORIZONTES:
            completo = hoy >= fin_mes + timedelta(days=h)
            cen = round(g["h"][h]["centro"])
            # se muestra desde que la cohorte entró a esa ventana (parcial hasta completarla)
            hs.append({"dias": h, "venta": g["h"][h]["venta"], "centro": cen, "completo": completo,
                       "alcanzado": hoy >= date(y, m, 1) + timedelta(days=prev)})
            prev = h
            if paga_en is None and gasto > 0 and cen >= gasto:
                paga_en = h
        ult = next((x for x in reversed(hs) if x["alcanzado"]), hs[0])
        if gasto <= 0:
            estado = "sin_gasto"
        elif paga_en:
            estado = "pagado"
        elif hs[-1]["completo"]:
            estado = "no_se_pago"
        else:
            estado = "aun_no"
        out.append({"cohorte": mes, "grupo_id": gid, "grupo": g["grupo"], "personas": g["personas"],
                    "pacientes": g["pacientes"], "volvieron": g["volvieron"],
                    "volvieron_pct": round(100 * g["volvieron"] / g["pacientes"]) if g["pacientes"] else None,
                    "gasto": round(gasto), "cac": round(gasto / g["pacientes"]) if gasto and g["pacientes"] else None,
                    "costo_persona": round(gasto / g["personas"]) if gasto and g["personas"] else None,
                    "horizontes": hs, "paga_en": paga_en, "estado": estado,
                    "falta": max(0, round(gasto - ult["centro"])) if estado == "aun_no" else 0})
    return {"cohortes": out, "horizontes": list(HORIZONTES), "desde": COHORTES_DESDE, "por": por, "canal": canal}


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
                q: str | None = None, ahora: datetime | None = None,
                gestion: str | None = None, canal: str | None = None,
                resultado: str | None = None) -> dict:
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
    canal = _canal(canal)
    plat = plataforma if plataforma in (*_PLATS, "sin_dato") and canal != "web" else None
    e0, e1 = _epoch_ini(d), _epoch_fin(h)
    esp_f = (especialidad or "").strip().lower()
    qn = (q or "").strip().lower()
    qd = "".join(ch for ch in qn if ch.isdigit())

    with db() as c:
        mapa = _mapa_anuncios(c)
        # Último contacto por persona en el rango (en "todos", dentro del canal
        # de su primer contacto: una sola tarjeta por persona).
        ult: dict[str, dict] = {}
        for r in _llegadas(c, canal, e0, e1):
            ult[_clave(r["phone"])] = r
        personas = {}
        for k, r in ult.items():
            info = _info(mapa, r["source_id"] or "", r["headline"] or "")
            if not _pasa_filtros(info, r["plataforma"], campana, plat, anuncio):
                continue
            personas[k] = {**info, "phone": r["phone"], "ts": r["ts"],
                           "plataforma": _plat(r["plataforma"]), "canal": r["origen"], "web": r.get("web")}
        if not personas:
            return _kanban_vacio(d, h, _opciones(c, mapa, canal))

        # Citas, eventos y mensajes desde el primer clic — agrupados por persona.
        t_min = min(p["ts"] for p in personas.values()) - 3600
        citas: dict[str, list[dict]] = defaultdict(list)
        for r in c.execute(
                "SELECT phone, id_cita, especialidad, profesional, fecha, hora, created_at, cancel_detected_at, "
                "confirmation_status, paciente_nombre, id_paciente_medilink FROM citas_bot WHERE created_at >= ?",
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
        # Pagó en caja sin pasar por el bot (lo agendó recepción, llamó, llegó
        # directo): su teléfono está en la ficha de un paciente con un pago
        # ≤90 días después del clic. Misma regla que la venta del panel.
        pago_tel: dict[str, int] = {}
        por_tel = _pacientes_por_telefono(c, set(personas))
        pid_k: dict[int, list[str]] = defaultdict(list)
        for k, pids in por_tel.items():
            for pid in pids:
                pid_k[pid].append(k)
        ids = list(pid_k)
        for i in range(0, len(ids), 500):
            lote = ids[i:i + 500]
            try:
                filas = c.execute("SELECT id_paciente, MIN(fecha) AS f FROM bi_pagos_caja WHERE id_paciente IN (%s) "
                                  "AND fecha >= ? GROUP BY id_paciente" % ",".join("?" * len(lote)),
                                  (*lote, datetime.fromtimestamp(t_min, _CL).date().isoformat())).fetchall()
            except Exception:
                filas = []
            for r in filas:
                for k in pid_k[r["id_paciente"]]:
                    dia = datetime.fromtimestamp(personas[k]["ts"], _CL).date()
                    # primer pago desde el clic: hay que mirar desde ese día, no desde t_min
                    f = c.execute("SELECT MIN(fecha) FROM bi_pagos_caja WHERE id_paciente=? AND fecha >= ?",
                                  (r["id_paciente"], dia.isoformat())).fetchone()[0]
                    if f and f[:10] <= (dia + timedelta(days=VENTANA_TELEFONO_DIAS)).isoformat():
                        ep = _epoch_ini(date.fromisoformat(f[:10]))
                        pago_tel[k] = min(pago_tel.get(k, ep), ep)
        opciones = _opciones(c, mapa, canal)
        segs = _seguimientos(c, set(personas))
        des = Desenlaces(c, [ci for v in citas.values() for ci in v], hoy, compras)

    cols: dict[str, list[dict]] = {e: [] for e in _ETAPA_IDS}
    for k, p in personas.items():
        R = p["ts"]
        mis = [ci for ci in citas.get(k, []) if ci["created_epoch"] >= R - 3600]
        for ci in mis:
            ci["_des"], ci["_fuente"] = des.una(ci, k)
        orden_f = lambda x: (x["fecha"] or "", x["hora"] or "")
        futuras = sorted([ci for ci in mis if ci["_des"] in ("pendiente", "sin_dato") and not _cancelada(ci)
                          and _cita_futura(ci, hoy, ahora_hm)], key=orden_f)
        # Pasadas que cuentan como atendidas: Medilink/caja/proxy, o sin ningún
        # dato (respaldo: hora pasada y no anulada, como antes).
        pasadas = sorted([ci for ci in mis if not _cita_futura(ci, hoy, ahora_hm)
                          and (ci["_des"] == "atendida" or (ci["_des"] in ("sin_dato", "pendiente")
                                                            and not _cancelada(ci)))], key=orden_f)
        no_show = sorted([ci for ci in mis if ci["_des"] == "no_show"], key=orden_f)
        comp = [t for t in compras.get(k, []) if t >= R]
        sl = [t for t in slots.get(k, []) if t >= R - 60]
        actividad = max(ult_msg.get(k, 0), R)

        if futuras:
            etapa, desde_ep = "agendado", futuras[0]["created_epoch"]
        elif pasadas or k in pago_tel or (comp and not mis):
            etapa = "atendido"
            if pasadas:
                f = pasadas[-1]["fecha"][:10]
                desde_ep = _epoch_ini(datetime.strptime(f, "%Y-%m-%d").date())
            elif comp:
                desde_ep = min(comp)
            else:
                desde_ep = pago_tel[k]
        elif no_show:
            etapa = "anulo"
            desde_ep = _epoch_ini(date.fromisoformat(no_show[-1]["fecha"][:10]))
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
        gest = _gestion_tarjeta(segs.get(k), hoy)
        if gestion and not _pasa_gestion(gest, gestion):
            continue
        if resultado == "no_asistio" and not (etapa == "anulo" and no_show):
            continue
        llegada = datetime.fromtimestamp(R, _CL)
        cols[etapa].append({
            "clave": k,
            "phone": p["phone"],
            "telefono": _mascara(p["phone"]),
            "nombre": nombre,
            "anuncio": p["anuncio"], "ad_id": p["ad_id"],
            "fuera_del_bot": etapa == "atendido" and not mis and k in pago_tel,
            "desenlace": ("no_asistio" if no_show else "anulo") if etapa == "anulo" else None,
            "atencion_fuente": (next((ci["_fuente"] for ci in reversed(pasadas) if ci["_des"] == "atendida"), "sin_dato")
                                if pasadas else ("caja" if k in pago_tel else ("proxy" if comp else None)))
                               if etapa == "atendido" else None,
            "campana": p["campana"], "campaign_id": p["campaign_id"],
            "plataforma": p["plataforma"],
            "canal": p["canal"],
            "web": p["web"],
            "especialidad": esp.strip().capitalize() if esp and esp.islower() else esp,
            "llegada": llegada.strftime("%d/%m/%Y"),
            "llegada_iso": llegada.strftime("%Y-%m-%d %H:%M"),
            "proxima_cita": ({"fecha": _fmt_fecha(futuras[0]["fecha"]), "hora": (futuras[0]["hora"] or "")[:5]}
                             if futuras else None),
            "dias_etapa": max(0, (ahora_ep - desde_ep) // 86400),
            "gestion": gest,
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
        "canal": canal,
        "columnas": columnas,
        "total": sum(c_["n"] for c_ in columnas),
        "por_llamar": sum(1 for c_ in columnas for t in c_["tarjetas"]
                          if (t["gestion"] or {}).get("alerta")),
        "no_asistieron": sum(1 for c_ in columnas for t in c_["tarjetas"] if t.get("desenlace") == "no_asistio"),
        "opciones": {**opciones, "gestion": GESTION},
        "medicion": {"atribucion_desde": ATRIBUCION_DESDE, "plataforma_desde": PLATAFORMA_DESDE,
                     "dias_perdido": DIAS_PERDIDO},
    }


def _kanban_vacio(d: date, h: date, opciones: dict) -> dict:
    return {"rango": {"desde": d.isoformat(), "hasta": h.isoformat(), "dias": (h - d).days + 1},
            "canal": "",
            "columnas": [{**e, "n": 0, "tarjetas": []} for e in ETAPAS], "total": 0, "por_llamar": 0, "no_asistieron": 0,
            "opciones": {**opciones, "gestion": GESTION},
            "medicion": {"atribucion_desde": ATRIBUCION_DESDE, "plataforma_desde": PLATAFORMA_DESDE,
                         "dias_perdido": DIAS_PERDIDO}}


# ── Seguimiento (capa de gestión encima del kanban) ─────────────────────────
# Las columnas del kanban se calculan solas con la agenda; nadie las mueve a
# mano. Lo que sí hace una persona es GESTIONAR: llamar, anotar, dejar una
# fecha para volver a llamar. Eso vive aquí, por persona (clave = últimos 9
# dígitos del teléfono), con un historial que solo crece.

GESTION = [
    {"id": "sin_gestion",     "label": "Sin gestionar"},
    {"id": "contactado",      "label": "Contactado"},
    {"id": "volver_llamar",   "label": "Volver a llamar"},
    {"id": "agendo_otra_via", "label": "Agendó por otra vía"},
    {"id": "no_interesa",     "label": "No le interesa"},
]
_GESTION_LBL = {g["id"]: g["label"] for g in GESTION}
_GESTION_CERRADA = {"agendo_otra_via", "no_interesa"}   # una fecha pendiente ya no alerta
FILTRO_POR_LLAMAR = "por_llamar"                         # volver a llamar hoy o vencido


def _crear_tablas_seguimiento() -> None:
    with db() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS campanas_seguimiento (
                clave      TEXT PRIMARY KEY,
                estado     TEXT NOT NULL DEFAULT 'sin_gestion',
                nota       TEXT,
                proximo    TEXT,
                updated_at TEXT
            )""")
        c.execute("""
            CREATE TABLE IF NOT EXISTS campanas_seguimiento_hist (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                clave   TEXT NOT NULL,
                ts      TEXT NOT NULL,
                estado  TEXT,
                nota    TEXT,
                proximo TEXT,
                origen  TEXT
            )""")
        c.execute("CREATE INDEX IF NOT EXISTS ix_campanas_seg_hist ON campanas_seguimiento_hist(clave, id)")
        c.commit()


try:
    _crear_tablas_seguimiento()
except Exception as _e:  # pragma: no cover — sin DB al importar no se cae el bot
    log.warning("campanas_meta: no se pudo crear campanas_seguimiento: %s", _e)


def _seguimientos(c, claves: set[str]) -> dict[str, dict]:
    if not claves:
        return {}
    out, ks = {}, list(claves)
    for i in range(0, len(ks), 500):
        lote = ks[i:i + 500]
        for r in c.execute("SELECT clave, estado, nota, proximo, updated_at FROM campanas_seguimiento "
                           "WHERE clave IN (%s)" % ",".join("?" * len(lote)), lote):
            out[r["clave"]] = dict(r)
    return out


def _gestion_tarjeta(seg: dict | None, hoy: str) -> dict | None:
    """Indicador de gestión para la tarjeta. `alerta`: 'hoy' o 'vencido' si
    hay una fecha de próximo contacto que ya llegó y la gestión sigue abierta."""
    if not seg:
        return None
    estado = seg.get("estado") or "sin_gestion"
    prox = (seg.get("proximo") or "")[:10] or None
    alerta = None
    if prox and estado not in _GESTION_CERRADA:
        alerta = "hoy" if prox == hoy else ("vencido" if prox < hoy else None)
    if estado == "sin_gestion" and not prox and not (seg.get("nota") or "").strip():
        return None
    return {"estado": estado, "label": _GESTION_LBL.get(estado, estado), "proximo": prox,
            "proximo_fmt": _fmt_fecha(prox) if prox else "", "alerta": alerta,
            "con_nota": bool((seg.get("nota") or "").strip())}


def _pasa_gestion(gest: dict | None, filtro: str) -> bool:
    if filtro == FILTRO_POR_LLAMAR:
        return bool(gest and gest.get("alerta"))
    if filtro == "sin_gestion":
        return not gest or gest["estado"] == "sin_gestion"
    return bool(gest) and gest["estado"] == filtro


def _clave_valida(clave: str) -> str:
    clave = (clave or "").strip()
    if clave.startswith(("fb_", "ig_")):
        if len(clave) > 4 and clave[3:].replace("-", "").replace("_", "").isalnum():
            return clave
    elif len(clave) == 9 and clave.isdigit():
        return clave
    raise HTTPException(404, "No existe esa persona")


def _phones_de(c, clave: str, canal: str | None = None) -> list[dict]:
    """Contactos de la persona (más reciente primero): clics en anuncios y
    llegadas desde la página web. Solo se puede abrir a quien llegó por uno de
    esos dos canales: si no hay ninguno, 404 — así el endpoint no sirve para
    leer cualquier conversación del bot."""
    canal = _canal(canal) if canal else "todos"
    filas: list[dict] = []
    if canal in ("meta", "todos"):
        if clave.startswith(("fb_", "ig_")):
            q = c.execute("SELECT phone, source_id, headline, plataforma, ts FROM meta_referrals "
                          "WHERE phone=? ORDER BY ts DESC", (clave,)).fetchall()
        else:
            q = c.execute("SELECT phone, source_id, headline, plataforma, ts FROM meta_referrals "
                          "WHERE phone LIKE ? ORDER BY ts DESC", ("%" + clave,)).fetchall()
        filas += [{**dict(r), "origen": "meta"} for r in q if _clave(r["phone"]) == clave]
    if canal in ("web", "todos"):
        filas += _llegadas_web(c, claves={clave})
    filas.sort(key=lambda r: -r["ts"])
    if not filas:
        raise HTTPException(404, "No existe esa persona")
    return filas


def _telefono_completo(phone: str) -> str:
    p = (phone or "").strip()
    if p.startswith("fb_"):
        return "Messenger"
    if p.startswith("ig_"):
        return "Instagram Direct"
    dig = "".join(ch for ch in p if ch.isdigit())
    if len(dig) == 11 and dig.startswith("569"):
        return f"+56 9 {dig[3:7]} {dig[7:]}"
    return ("+" + dig) if dig else "—"


def _tel_href(phone: str) -> str | None:
    dig = "".join(ch for ch in (phone or "") if ch.isdigit())
    if (phone or "").startswith(("fb_", "ig_")) or len(dig) < 9:
        return None
    return "+" + dig if len(dig) >= 11 else "+56" + dig


def _historial(c, clave: str) -> list[dict]:
    return [{"ts": r["ts"], "estado": r["estado"], "label": _GESTION_LBL.get(r["estado"], r["estado"]),
             "nota": r["nota"] or "", "proximo": r["proximo"] or None,
             "proximo_fmt": _fmt_fecha(r["proximo"]) if r["proximo"] else "", "origen": r["origen"] or ""}
            for r in c.execute("SELECT ts, estado, nota, proximo, origen FROM campanas_seguimiento_hist "
                               "WHERE clave=? ORDER BY id DESC", (clave,))]


def _seguimiento_completo(c, clave: str, hoy: str) -> dict:
    seg = _seguimientos(c, {clave}).get(clave) or {}
    estado = seg.get("estado") or "sin_gestion"
    gest = _gestion_tarjeta(seg, hoy) or {}
    return {"estado": estado, "label": _GESTION_LBL.get(estado, estado), "nota": seg.get("nota") or "",
            "proximo": (seg.get("proximo") or "")[:10] or None, "alerta": gest.get("alerta"),
            "actualizado": seg.get("updated_at"), "historial": _historial(c, clave)}


def guardar_seguimiento(clave: str, estado: str, nota: str | None, proximo: str | None,
                        origen: str = "dueño", ahora: datetime | None = None) -> dict:
    """Guarda el estado de gestión y agrega una línea al historial (append-only).
    Si nada cambió, no agrega línea."""
    clave = _clave_valida(clave)
    if estado not in _GESTION_LBL:
        raise HTTPException(400, "Estado de gestión desconocido")
    nota = (nota or "").strip()
    if len(nota) > 2000:
        raise HTTPException(400, "La nota no puede pasar de 2.000 caracteres")
    prox = (proximo or "").strip()[:10] or None
    if prox:
        try:
            datetime.strptime(prox, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(400, "Fecha de próximo contacto inválida")
    if estado == "volver_llamar" and not prox:
        raise HTTPException(400, "Indique la fecha en que hay que volver a llamar")
    ahora = ahora or datetime.now(_CL)
    ts = ahora.strftime("%Y-%m-%d %H:%M:%S")
    with db() as c:
        _phones_de(c, clave)
        prev = _seguimientos(c, {clave}).get(clave)
        cambio = (not prev or prev["estado"] != estado or (prev["nota"] or "") != nota
                  or ((prev["proximo"] or "")[:10] or None) != prox)
        if cambio:
            c.execute("INSERT INTO campanas_seguimiento (clave, estado, nota, proximo, updated_at) "
                      "VALUES (?,?,?,?,?) ON CONFLICT(clave) DO UPDATE SET estado=excluded.estado, "
                      "nota=excluded.nota, proximo=excluded.proximo, updated_at=excluded.updated_at",
                      (clave, estado, nota, prox, ts))
            c.execute("INSERT INTO campanas_seguimiento_hist (clave, ts, estado, nota, proximo, origen) "
                      "VALUES (?,?,?,?,?,?)", (clave, ts, estado, nota, prox, origen))
            c.commit()
        return _seguimiento_completo(c, clave, ahora.date().isoformat())


def persona_data(clave: str, ahora: datetime | None = None, canal: str | None = None) -> dict:
    """Ficha de una persona que llegó por un anuncio: cabecera, seguimiento y
    línea de tiempo (clics, citas del bot, horarios ofrecidos, avisos de
    atención y pagos en caja). Sin datos clínicos: no hay diagnósticos ni
    motivos de consulta, solo qué pasó con la agenda y la caja."""
    clave = _clave_valida(clave)
    ahora = ahora or datetime.now(_CL)
    hoy = ahora.date().isoformat()
    with db() as c:
        refs = _phones_de(c, clave, canal)
        phone = refs[0]["phone"]
        phones = sorted({r["phone"] for r in refs})
        mapa = _mapa_anuncios(c)
        primer = min(r["ts"] for r in refs)
        ev: list[dict] = []
        for r in refs:
            info = _info(mapa, r["source_id"] or "", r["headline"] or "")
            if r["origen"] == "web":
                w = r.get("web") or {}
                ev.append({"ts": r["ts"], "tipo": "web", "titulo": "Escribió desde la página web",
                           "detalle": " · ".join(x for x in (w.get("pagina"),
                                                             f'artículo «{w["articulo"]}»' if w.get("articulo") else "",
                                                             w.get("posicion"), w.get("boton"),
                                                             f'"{w["texto"]}"' if w.get("texto") else "",
                                                             w.get("comuna")) if x),
                           "plataforma": "web"})
            else:
                ev.append({"ts": r["ts"], "tipo": "clic", "titulo": "Clic en un anuncio",
                           "detalle": f'{info["anuncio"]} · {info["campana"]}',
                           "plataforma": _plat(r["plataforma"])})
        ult = _info(mapa, refs[0]["source_id"] or "", refs[0]["headline"] or "")

        # Citas del bot (todas las del teléfono, también las anteriores al clic:
        # muestran si ya era paciente).
        q_ph = ",".join("?" * len(phones))
        citas = []
        for r in c.execute("SELECT phone, especialidad, profesional, fecha, hora, created_at, "
                           "cancel_detected_at, confirmation_status, paciente_nombre, ad_source_id, id_cita, "
                           "id_paciente_medilink FROM citas_bot WHERE phone LIKE ? OR phone IN (%s)" % q_ph,
                           ("%" + clave if clave.isdigit() else clave, *phones)):
            if _clave(r["phone"]) != clave:
                continue
            citas.append(dict(r))
        des_p = Desenlaces(c, citas, hoy, _purchases(c, primer - 86400 * 400))
        _DES_LBL = {"atendida": "Atendida", "no_show": "No asistió", "anulada": "Anulada",
                    "pendiente": "Por venir", "sin_dato": "Sin dato"}
        _FUE_LBL = {"medilink": "según Medilink", "caja": "pagó en caja ese día", "bot": "detectada por el bot",
                    "proxy": "estimado por aviso a Meta", "agenda": ""}
        for d in citas:
            cre = _utc_txt_epoch(d["created_at"]) or 0
            hora = (d["hora"] or "")[:5]
            dz, fz = des_p.una(d, clave)
            ev.append({"ts": cre, "tipo": "cita", "titulo": "Agendó por el bot",
                       "detalle": " · ".join(x for x in (
                           d["especialidad"] or "", (_fmt_fecha(d["fecha"]) + (" a las " + hora if hora else "")),
                           d["profesional"] or "") if x),
                       "anulada": dz == "anulada", "del_anuncio": bool(d["ad_source_id"]),
                       "desenlace": dz, "desenlace_lbl": " · ".join(x for x in (_DES_LBL[dz], _FUE_LBL.get(fz, "")) if x)})
            if dz == "anulada":
                ca = _utc_txt_epoch(d.get("cancel_detected_at"))
                ev.append({"ts": ca or cre + 1, "tipo": "anulada", "titulo": "Cita anulada",
                           "detalle": " · ".join(x for x in (d["especialidad"] or "", _fmt_fecha(d["fecha"])) if x)})
            elif dz == "no_show":
                try:
                    e_ns = _epoch_ini(date.fromisoformat((d["fecha"] or "")[:10])) + 20 * 3600
                except ValueError:
                    e_ns = cre + 1
                ev.append({"ts": e_ns, "tipo": "no_show", "titulo": "No asistió a la cita",
                           "detalle": " · ".join(x for x in (d["especialidad"] or "", _fmt_fecha(d["fecha"]),
                                                             "según Medilink") if x), "solo_fecha": True})

        # Horarios ofrecidos y avisos de atención (eventos del bot).
        vistos_slot: list[int] = []
        for r in c.execute("SELECT phone, event, ts, meta FROM conversation_events WHERE ts >= ? "
                           "AND event IN (?,?,?) AND (phone LIKE ? OR phone IN (%s)) ORDER BY ts" % q_ph,
                           (_utc_txt(primer - 86400 * 30), *_EV_SLOTS, "capi_send_ok",
                            "%" + clave if clave.isdigit() else clave, *phones)):
            if _clave(r["phone"]) != clave:
                continue
            e = _utc_txt_epoch(r["ts"]) or 0
            try:
                meta = json.loads(r["meta"] or "{}")
            except (ValueError, TypeError):
                meta = {}
            if r["event"] == "capi_send_ok":
                if meta.get("event_type") == "Purchase":
                    ev.append({"ts": e, "tipo": "atencion", "titulo": "Aviso de atención enviado a Meta",
                               "detalle": "Se envía cuando la hora pasa sin anulación; no confirma asistencia "
                                          "(manda el estado de Medilink)"})
                continue
            if any(abs(e - v) < 600 for v in vistos_slot):   # ráfaga de ofertas = una sola
                continue
            vistos_slot.append(e)
            esp = (meta.get("esp") or "").strip()
            ev.append({"ts": e, "tipo": "horarios", "titulo": "El bot le ofreció horarios",
                       "detalle": esp.capitalize() if esp.islower() else esp})

        # Pagos en caja: mismo cruce por teléfono que la venta del panel.
        pids = set(_pacientes_por_telefono(c, {clave}).get(clave, set()))
        nombres_prof = _nombres_profesionales(c)
        dia_clic = datetime.fromtimestamp(primer, _CL).date().isoformat()
        pagos_total, antes_n, antes_total = 0, 0, 0
        pids_l = sorted(pids)
        orden_pid = {pid: i + 1 for i, pid in enumerate(pids_l)}
        if pids_l:
            try:
                filas = c.execute("SELECT fecha, id_profesional, id_paciente, monto FROM bi_pagos_caja "
                                  "WHERE id_paciente IN (%s) ORDER BY fecha" % ",".join("?" * len(pids_l)),
                                  pids_l).fetchall()
            except Exception as e:  # entorno sin BI: sin pagos, no rompe la ficha
                log.warning("campanas_meta: pagos no disponibles: %s", e)
                filas = []
            for r in filas:
                f = (r["fecha"] or "")[:10]
                monto = int(r["monto"] or 0)
                if f < dia_clic:
                    antes_n += 1
                    antes_total += monto
                    continue
                pagos_total += monto
                try:
                    e = _epoch_ini(date.fromisoformat(f)) + 12 * 3600
                except ValueError:
                    continue
                ev.append({"ts": e, "tipo": "pago", "titulo": "Pago en caja", "solo_fecha": True,
                           "detalle": nombres_prof.get(r["id_profesional"]) or f"Profesional {r['id_profesional']}",
                           "monto": monto,
                           "paciente": (f"Paciente {orden_pid[r['id_paciente']]} de {len(pids_l)}"
                                        if len(pids_l) > 1 else "")})

        nombre = ""
        for r in c.execute("SELECT phone, nombre FROM contact_profiles WHERE nombre IS NOT NULL AND nombre != '' "
                           "AND (phone LIKE ? OR phone IN (%s))" % q_ph,
                           ("%" + clave if clave.isdigit() else clave, *phones)):
            if _clave(r["phone"]) == clave:
                nombre = r["nombre"].strip()
        if not nombre:
            nombre = next((ci["paciente_nombre"].strip() for ci in reversed(citas)
                           if (ci.get("paciente_nombre") or "").strip()), "")
        seguimiento = _seguimiento_completo(c, clave, hoy)

    for x in ev:
        dt = datetime.fromtimestamp(x["ts"], _CL)
        x["fecha"] = dt.strftime("%d/%m/%Y")
        x["hora"] = "" if x.get("solo_fecha") else dt.strftime("%H:%M")
        x["iso"] = dt.strftime("%Y-%m-%d %H:%M")
    ev.sort(key=lambda x: -x["ts"])   # más reciente primero
    return {
        "clave": clave, "phone": phone, "telefono": _telefono_completo(phone), "tel_href": _tel_href(phone),
        "nombre": nombre,
        "anuncio": ult["anuncio"], "campana": ult["campana"], "ad_id": ult["ad_id"],
        "plataforma": _plat(refs[0]["plataforma"]),
        "canal": refs[0]["origen"],
        "web": refs[0].get("web"),
        "primer_clic": datetime.fromtimestamp(primer, _CL).strftime("%d/%m/%Y"),
        "clics": sum(1 for r in refs if r["origen"] == "meta"),
        "visitas_web": sum(1 for r in refs if r["origen"] == "web"),
        "linea": ev[:150],
        "pagos": {"total": pagos_total, "pacientes": len(pids_l),
                  "antes_del_clic": antes_n, "antes_total": antes_total},
        "seguimiento": seguimiento,
        "gestion_opciones": GESTION,
    }


# ── Conversación del bot desde la ficha ─────────────────────────────────────
# Mismo mecanismo que el embudo de ortodoncia: la ventana de 24 h se mira
# ANTES de enviar (Meta igual devuelve wamid fuera de ventana) y el envío pasa
# por `responder_como_recepcion` (takeover + lock por teléfono), un solo
# camino para todos los paneles.

def _ventana(phone: str) -> tuple[bool, str | None]:
    from orto_embudo_routes import _ventana_abierta
    return _ventana_abierta(phone)


async def _responder(phone: str, texto: str) -> dict:
    from admin_routes import responder_como_recepcion
    return await responder_como_recepcion(phone, texto, exigir_entrega=True)


def conversacion_data(clave: str) -> dict:
    from session import get_messages, get_session
    clave = _clave_valida(clave)
    with db() as c:
        phone = _phones_de(c, clave)[0]["phone"]
    abierta, ult_in = _ventana(phone)
    msgs = [{"id": m["id"], "dir": m["direction"], "texto": m["text"] or "",
             "ts": m["ts"], "media": m.get("media_tipo")}
            for m in get_messages(phone, limit=150)]
    return {"telefono": _telefono_completo(phone), "mensajes": msgs,
            "ventana_abierta": abierta, "ultimo_del_paciente": ult_in,
            "estado_bot": (get_session(phone) or {}).get("state", "IDLE")}


# ── Endpoints ───────────────────────────────────────────────────────────────

@router.get("/panel")
def panel(request: Request, desde: str | None = Query(None), hasta: str | None = Query(None),
          campana: str | None = Query(None), plataforma: str | None = Query(None),
          canal: str | None = Query(None), token: str | None = Query(None)):
    _auth(request, token)
    return panel_data(desde, hasta, campana or None, plataforma or None, canal or None)


@router.get("/kanban")
def kanban(request: Request, desde: str | None = Query(None), hasta: str | None = Query(None),
           campana: str | None = Query(None), anuncio: str | None = Query(None),
           plataforma: str | None = Query(None), especialidad: str | None = Query(None),
           q: str | None = Query(None), gestion: str | None = Query(None),
           canal: str | None = Query(None), resultado: str | None = Query(None),
           token: str | None = Query(None)):
    _auth(request, token)
    return kanban_data(desde, hasta, campana or None, anuncio or None, plataforma or None,
                       especialidad or None, q or None, gestion=gestion or None, canal=canal or None,
                       resultado=resultado or None)


@router.get("/sugerencias-presupuesto")
def sugerencias_presupuesto_ep(request: Request, token: str | None = Query(None)):
    _auth(request, token)
    import meta_alertas
    return meta_alertas.sugerencias_presupuesto()


@router.get("/llamar-hoy")
def llamar_hoy(request: Request, desde: str | None = Query(None), hasta: str | None = Query(None),
               campana: str | None = Query(None), anuncio: str | None = Query(None),
               plataforma: str | None = Query(None), especialidad: str | None = Query(None),
               canal: str | None = Query(None), token: str | None = Query(None)):
    _auth(request, token)
    return llamar_hoy_data(desde, hasta, campana or None, anuncio or None, plataforma or None,
                           especialidad or None, canal=canal or None)


@router.get("/google")
def google(request: Request, desde: str | None = Query(None), hasta: str | None = Query(None),
           token: str | None = Query(None)):
    _auth(request, token)
    return google_data(desde, hasta)


@router.get("/cohortes")
def cohortes(request: Request, por: str | None = Query("canal"), canal: str | None = Query("todos"),
             token: str | None = Query(None)):
    _auth(request, token)
    return cohortes_data(por or "canal", canal or "todos")


@router.get("/persona/{clave}")
def persona(clave: str, request: Request, canal: str | None = Query(None),
            token: str | None = Query(None)):
    _auth(request, token)
    return persona_data(clave, canal=canal or None)


@router.post("/persona/{clave}/seguimiento")
async def persona_seguimiento(clave: str, request: Request, token: str | None = Query(None)):
    _auth(request, token)
    try:
        b = await request.json()
    except ValueError:
        raise HTTPException(400, "Cuerpo inválido")
    if not isinstance(b, dict):
        raise HTTPException(400, "Cuerpo inválido")
    return guardar_seguimiento(clave, str(b.get("estado") or ""), b.get("nota"), b.get("proximo"))


@router.get("/persona/{clave}/conversacion")
def persona_conversacion(clave: str, request: Request, token: str | None = Query(None)):
    _auth(request, token)
    return conversacion_data(clave)


@router.post("/persona/{clave}/conversacion")
async def persona_responder(clave: str, request: Request, token: str | None = Query(None)):
    """Responde por el WhatsApp del bot. Con la ventana de 24 h cerrada no se
    envía texto libre (409). Al responder, la conversación queda con recepción
    (takeover) y, si la persona estaba sin gestionar, queda como Contactado."""
    _auth(request, token)
    try:
        texto = ((await request.json()).get("mensaje") or "").strip()
    except (ValueError, AttributeError):
        raise HTTPException(400, "Cuerpo inválido")
    if not texto:
        raise HTTPException(400, "Mensaje vacío")
    if len(texto) > 4000:
        raise HTTPException(400, "Mensaje demasiado largo")
    clave = _clave_valida(clave)
    with db() as c:
        phone = _phones_de(c, clave)[0]["phone"]
        prev = _seguimientos(c, {clave}).get(clave)
    abierta, _ = _ventana(phone)
    if not abierta:
        raise HTTPException(409, "Pasaron más de 24 h desde el último mensaje de esta persona: "
                                 "WhatsApp no permite escribirle texto libre hasta que vuelva a escribir.")
    try:
        r = await _responder(phone, texto)
    except HTTPException:
        raise
    except Exception as e:   # red/Meta caída: el dueño debe saber que NO salió
        log.warning("campanas_meta: envío falló para %s: %s", clave, e)
        raise HTTPException(502, "No se pudo enviar el mensaje por WhatsApp. Intente de nuevo en unos minutos "
                                 "o llame por teléfono.")
    from session import log_event
    log_event(phone, "campanas_meta_respuesta", {"clave": clave, "mensaje": texto[:200]})
    if not prev or (prev.get("estado") or "sin_gestion") == "sin_gestion":
        guardar_seguimiento(clave, "contactado", (prev or {}).get("nota"), (prev or {}).get("proximo"),
                            origen="respuesta por WhatsApp")
    return r if isinstance(r, dict) else {"ok": True}
