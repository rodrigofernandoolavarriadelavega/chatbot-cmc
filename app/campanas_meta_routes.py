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
     "ayuda": "Aviso de atención (CAPI Purchase), cita ya pasada y no anulada, o pagó en caja dentro de 90 días del clic aunque no agendara por el bot."},
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


def _acum() -> dict:
    return {"gasto": 0.0, "impresiones": 0, "alcance": 0, "clics": 0, "conv": 0,
            "personas": set(), "citas": 0, "atendidos": 0, "venta": 0, "centro": 0,
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
        "cac_conv": _div(g, a["conv"]), "cac_cita": _div(g, citas),
        "cac_atendido": _div(g, aten),
        "venta": round(a["venta"]),
        "retorno": round(a["venta"] / g, 2) if g else None,
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
        clics: dict[str, tuple[int, str]] = {}   # clave → (primer clic, ad_id)
        for r in c.execute(
                "SELECT phone, source_id, headline, plataforma, ts FROM meta_referrals "
                "WHERE ts >= ? AND ts < ?", (e0, e1)):
            info = _info_ad(mapa, r["source_id"] or "", r["headline"] or "")
            if not _pasa_filtros(info, r["plataforma"], campana, plat):
                continue
            k = _clave(r["phone"])
            por_ad[info["ad_id"]]["personas"].add(k)
            tot["personas"].add(k)
            if k not in clics or r["ts"] < clics[k][0]:
                clics[k] = (r["ts"], info["ad_id"])

        citas = _citas_atribuidas(c, e0, e1, mapa, campana, plat)
        compras = _purchases(c, e0)
        for ci in citas:
            ci["atendido"] = any(t >= ci["created_epoch"] for t in compras.get(ci["clave"], []))
            for a in (por_ad[ci["ad_id"]], tot):
                a["citas"] += 1
                a["atendidos"] += 1 if ci["atendido"] else 0

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
            for k in ("gasto", "impresiones", "alcance", "clics", "conv", "citas", "atendidos", "venta", "centro", "pagaron", "pagaron_tel"):
                pc[k] += a[k]
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
        "conocieron": conocieron,
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
                q: str | None = None, ahora: datetime | None = None,
                gestion: str | None = None) -> dict:
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
        opciones = _opciones(c, mapa)
        segs = _seguimientos(c, set(personas))

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
        elif comp or pasadas or k in pago_tel:
            etapa = "atendido"
            if pasadas:
                f = pasadas[-1]["fecha"][:10]
                desde_ep = _epoch_ini(datetime.strptime(f, "%Y-%m-%d").date())
            elif comp:
                desde_ep = min(comp)
            else:
                desde_ep = pago_tel[k]
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
        llegada = datetime.fromtimestamp(R, _CL)
        cols[etapa].append({
            "clave": k,
            "phone": p["phone"],
            "telefono": _mascara(p["phone"]),
            "nombre": nombre,
            "anuncio": p["anuncio"], "ad_id": p["ad_id"],
            "fuera_del_bot": etapa == "atendido" and not mis and k in pago_tel,
            "campana": p["campana"], "campaign_id": p["campaign_id"],
            "plataforma": p["plataforma"],
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
        "columnas": columnas,
        "total": sum(c_["n"] for c_ in columnas),
        "por_llamar": sum(1 for c_ in columnas for t in c_["tarjetas"]
                          if (t["gestion"] or {}).get("alerta")),
        "opciones": {**opciones, "gestion": GESTION},
        "medicion": {"atribucion_desde": ATRIBUCION_DESDE, "plataforma_desde": PLATAFORMA_DESDE,
                     "dias_perdido": DIAS_PERDIDO},
    }


def _kanban_vacio(d: date, h: date, opciones: dict) -> dict:
    return {"rango": {"desde": d.isoformat(), "hasta": h.isoformat(), "dias": (h - d).days + 1},
            "columnas": [{**e, "n": 0, "tarjetas": []} for e in ETAPAS], "total": 0, "por_llamar": 0,
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


def _phones_de(c, clave: str) -> list[dict]:
    """Referrals de la persona (más reciente primero). Solo se puede abrir a
    quien llegó por un anuncio: si no hay referral, 404 — así el endpoint no
    sirve para leer cualquier conversación del bot."""
    if clave.startswith(("fb_", "ig_")):
        filas = c.execute("SELECT phone, source_id, headline, plataforma, ts FROM meta_referrals "
                          "WHERE phone=? ORDER BY ts DESC", (clave,)).fetchall()
    else:
        filas = c.execute("SELECT phone, source_id, headline, plataforma, ts FROM meta_referrals "
                          "WHERE phone LIKE ? ORDER BY ts DESC", ("%" + clave,)).fetchall()
    filas = [dict(r) for r in filas if _clave(r["phone"]) == clave]
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


def persona_data(clave: str, ahora: datetime | None = None) -> dict:
    """Ficha de una persona que llegó por un anuncio: cabecera, seguimiento y
    línea de tiempo (clics, citas del bot, horarios ofrecidos, avisos de
    atención y pagos en caja). Sin datos clínicos: no hay diagnósticos ni
    motivos de consulta, solo qué pasó con la agenda y la caja."""
    clave = _clave_valida(clave)
    ahora = ahora or datetime.now(_CL)
    hoy = ahora.date().isoformat()
    with db() as c:
        refs = _phones_de(c, clave)
        phone = refs[0]["phone"]
        phones = sorted({r["phone"] for r in refs})
        mapa = _mapa_anuncios(c)
        primer = min(r["ts"] for r in refs)
        ev: list[dict] = []
        for r in refs:
            info = _info_ad(mapa, r["source_id"] or "", r["headline"] or "")
            ev.append({"ts": r["ts"], "tipo": "clic", "titulo": "Clic en un anuncio",
                       "detalle": f'{info["anuncio"]} · {info["campana"]}',
                       "plataforma": _plat(r["plataforma"])})
        ult = _info_ad(mapa, refs[0]["source_id"] or "", refs[0]["headline"] or "")

        # Citas del bot (todas las del teléfono, también las anteriores al clic:
        # muestran si ya era paciente).
        q_ph = ",".join("?" * len(phones))
        citas = []
        for r in c.execute("SELECT phone, especialidad, profesional, fecha, hora, created_at, "
                           "cancel_detected_at, confirmation_status, paciente_nombre, ad_source_id "
                           "FROM citas_bot WHERE phone LIKE ? OR phone IN (%s)" % q_ph,
                           ("%" + clave if clave.isdigit() else clave, *phones)):
            if _clave(r["phone"]) != clave:
                continue
            d = dict(r)
            citas.append(d)
            cre = _utc_txt_epoch(d["created_at"]) or 0
            hora = (d["hora"] or "")[:5]
            ev.append({"ts": cre, "tipo": "cita", "titulo": "Agendó por el bot",
                       "detalle": " · ".join(x for x in (
                           d["especialidad"] or "", (_fmt_fecha(d["fecha"]) + (" a las " + hora if hora else "")),
                           d["profesional"] or "") if x),
                       "anulada": _cancelada(d), "del_anuncio": bool(d["ad_source_id"])})
            if _cancelada(d):
                ca = _utc_txt_epoch(d.get("cancel_detected_at"))
                ev.append({"ts": ca or cre + 1, "tipo": "anulada", "titulo": "Cita anulada",
                           "detalle": " · ".join(x for x in (d["especialidad"] or "", _fmt_fecha(d["fecha"])) if x)})

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
                    ev.append({"ts": e, "tipo": "atencion", "titulo": "Aviso de atención a Meta",
                               "detalle": "La hora pasó y no fue anulada (proxy de atención)"})
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
        "primer_clic": datetime.fromtimestamp(primer, _CL).strftime("%d/%m/%Y"),
        "clics": len(refs),
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
          token: str | None = Query(None)):
    _auth(request, token)
    return panel_data(desde, hasta, campana or None, plataforma or None)


@router.get("/kanban")
def kanban(request: Request, desde: str | None = Query(None), hasta: str | None = Query(None),
           campana: str | None = Query(None), anuncio: str | None = Query(None),
           plataforma: str | None = Query(None), especialidad: str | None = Query(None),
           q: str | None = Query(None), gestion: str | None = Query(None),
           token: str | None = Query(None)):
    _auth(request, token)
    return kanban_data(desde, hasta, campana or None, anuncio or None, plataforma or None,
                       especialidad or None, q or None, gestion=gestion or None)


@router.get("/persona/{clave}")
def persona(clave: str, request: Request, token: str | None = Query(None)):
    _auth(request, token)
    return persona_data(clave)


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
