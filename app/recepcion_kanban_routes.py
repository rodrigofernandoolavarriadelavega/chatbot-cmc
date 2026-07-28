"""recepcion_kanban_routes.py — API del panel Recepción Kanban.

Devuelve el "tablero": cada conversación de WhatsApp con su ETAPA computada
(1º mensaje → área → profesional → agendado → atendido) y su ESTADO de mensaje
(no visto / visto / en espera de recepción). Reusa lo que el bot ya sabe; no
modifica el motor conversacional.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Query, Cookie, Request, HTTPException

log = logging.getLogger("recepcion_kanban")
_CLT = ZoneInfo("America/Santiago")

router = APIRouter(prefix="/alma/api/recepcion-kanban", tags=["recepcion-kanban"])

# Estados de la máquina conversacional → etapa
_WAIT_SLOT = {"WAIT_SLOT", "WAIT_SLOT_OTRO", "WAIT_META_SLOT_CHOICE", "WAIT_META_WAITLIST"}
_WAIT_AREA = {"WAIT_ESPECIALIDAD", "WAIT_MODALIDAD", "WAIT_DURACION_MASOTERAPIA"}

_MG = ("medicina general", "medicina familiar", "general", "médico", "medico")
_DENTAL = ("odonto", "dental", "endodon", "ortodon", "implanto", "estética facial",
           "estetica facial", "bruxismo", "blanqueamiento")


def _macrogrupo(esp: str) -> str:
    e = (esp or "").strip().lower()
    if not e:
        return ""
    if any(x in e for x in _MG):
        return "Medicina General/Familiar"
    if any(x in e for x in _DENTAL):
        return "Dental"
    return esp.strip().title()


def _auth(token, cmc_session, request):
    from admin_routes import _is_admin_token, _verify_cookie
    if token and _is_admin_token(token):
        return
    if cmc_session and _verify_cookie(cmc_session) in ("admin", "ortodoncia"):
        return
    raise HTTPException(401, "no autorizado")


def _rango_a_fechas(rango: str | None, desde: str | None, hasta: str | None):
    """Resuelve un preset (dia/semana/mes/todo) o usa desde/hasta explícitos.
    Devuelve (desde, hasta) como 'YYYY-MM-DD' o (None, None) para 'todo'."""
    if desde or hasta:
        return desde, hasta
    hoy = datetime.now(_CLT).date()
    r = (rango or "dia").lower()
    if r == "todo":
        return None, None
    if r == "semana":
        return (hoy - timedelta(days=hoy.weekday())).isoformat(), hoy.isoformat()
    if r == "mes":
        return hoy.replace(day=1).isoformat(), hoy.isoformat()
    return hoy.isoformat(), hoy.isoformat()   # día (default)


def build_board(desde: str | None, hasta: str | None) -> dict:
    from session import get_conversations, get_unread_counts, db
    convs = get_conversations(1500)
    try:
        unread = get_unread_counts() or {}
    except Exception:
        unread = {}

    citas: dict[str, dict] = {}
    paid_keys: set = set()
    destacados: set[str] = set()
    with db() as conn:
        try:
            for r in conn.execute(
                """SELECT phone, especialidad, profesional, fecha, hora, modalidad,
                          id_paciente_medilink
                     FROM citas_bot
                    WHERE fecha >= date('now','localtime','-1 day')
                    ORDER BY fecha"""):
                citas.setdefault(r["phone"], dict(r))   # la cita activa más próxima
        except Exception as e:  # noqa: BLE001
            log.warning("kanban: citas_bot %s", e)
        # Pagos recientes (id_paciente, fecha) → "atendido" SOLO si la cita reciente
        # del paciente ya tiene pago ese día (no cualquier pago histórico).
        try:
            for r in conn.execute(
                """SELECT DISTINCT id_paciente, fecha FROM bi_pagos_caja
                    WHERE monto > 0 AND fecha >= date('now','localtime','-3 day')"""):
                paid_keys.add((r["id_paciente"], r["fecha"]))
        except Exception as e:  # noqa: BLE001
            log.warning("kanban: pagos %s", e)
        try:
            for r in conn.execute("SELECT DISTINCT phone FROM contact_tags WHERE tag='destacado'"):
                destacados.add(r["phone"])
        except Exception as e:  # noqa: BLE001
            log.warning("kanban: destacados %s", e)

    cards = []
    for c in convs:
        ts = (c.get("last_ts") or c.get("updated_at") or "")
        d10 = ts[:10]
        if desde and d10 and d10 < desde:
            continue
        if hasta and d10 and d10 > hasta:
            continue
        phone = c["phone"]
        state = c.get("state") or "IDLE"
        fd = c.get("flow_data") or {}
        esp = (fd.get("especialidad") or "").strip()
        cita = citas.get(phone)

        cita_pagada = bool(cita and (cita.get("id_paciente_medilink"), cita.get("fecha")) in paid_keys)
        if cita_pagada:
            etapa = "atendido"
        elif cita:
            etapa = "agendado"
        elif state in _WAIT_SLOT:
            etapa = "profesional"
        elif esp or state in _WAIT_AREA:
            etapa = "area"
        else:
            etapa = "primer_mensaje"

        # "Utility" = último mensaje es un template saliente sin respuesta del
        # paciente (esperando que conteste). Inferido del texto (sin tocar el envío).
        last_dir = c.get("last_dir") or ""
        ltl = (c.get("last_text") or "").lower()
        es_template = last_dir == "out" and ("[template" in ltl or "template:" in ltl)
        if state == "HUMAN_TAKEOVER":
            msg_estado = "en_espera"
        elif unread.get(phone, 0) > 0:
            msg_estado = "no_visto"
        elif es_template:
            msg_estado = "utility"
        else:
            msg_estado = "visto"

        prof = (cita or {}).get("profesional") or fd.get("profesional") or ""
        esp_eff = esp or (cita or {}).get("especialidad") or ""
        cards.append({
            "phone": phone,
            "nombre": c.get("nombre") or "",
            "rut": c.get("rut") or "",
            "canal": c.get("canal") or "whatsapp",
            "last_text": (c.get("last_text") or "")[:140],
            "last_dir": c.get("last_dir") or "",
            "last_ts": ts,
            "etapa": etapa,
            "area": esp_eff,
            "area_grupo": _macrogrupo(esp_eff),
            "profesional": prof or "Sin asignar",
            "msg_estado": msg_estado,
            "destacado": phone in destacados,
            "sin_resp": c.get("msgs_sin_respuesta", 0) or 0,
            "cita": ({"fecha": cita.get("fecha"), "hora": cita.get("hora"),
                      "modalidad": cita.get("modalidad")} if cita else None),
        })

    return {"cards": cards, "total": len(cards),
            "desde": desde, "hasta": hasta,
            "generado": datetime.now(_CLT).strftime("%Y-%m-%d %H:%M")}


@router.get("/board")
async def api_board(rango: str | None = Query(None),
                    desde: str | None = Query(None),
                    hasta: str | None = Query(None),
                    token: str | None = Query(None),
                    cmc_session: str | None = Cookie(None),
                    request: Request = None):
    _auth(token, cmc_session, request)
    d, h = _rango_a_fechas(rango, desde, hasta)
    return build_board(d, h)


@router.get("/cola")
async def api_cola(rango: str | None = Query(None),
                   desde: str | None = Query(None),
                   hasta: str | None = Query(None),
                   token: str | None = Query(None),
                   cmc_session: str | None = Cookie(None),
                   request: Request = None):
    """v2: la misma data del board, pero convertida en COLA DE TRABAJO.

    Aplica política de salida, clases de servicio y orden FIFO por espera.
    Ver el docstring de `recepcion_kanban_v2` para el porqué de cada regla.
    """
    _auth(token, cmc_session, request)
    d, h = _rango_a_fechas(rango, desde, hasta)
    base = build_board(d, h)
    import recepcion_kanban_v2 as v2
    cola = v2.construir(base.get("cards", []))
    cola["total_crudo"] = base.get("total", 0)
    return cola


@router.post("/destacar/{phone}")
async def api_destacar(phone: str, on: int = Query(1),
                       token: str | None = Query(None),
                       cmc_session: str | None = Cookie(None),
                       request: Request = None):
    """Marca/desmarca una conversación como destacada (tag 'destacado')."""
    _auth(token, cmc_session, request)
    from session import save_tag, delete_tag
    if on:
        save_tag(phone, "destacado")
    else:
        delete_tag(phone, "destacado")
    return {"ok": True, "phone": phone, "destacado": bool(on)}


def _friccion(desde: str | None, hasta: str | None) -> dict:
    """Embudo de agendamiento + fricción (minutos entre etapas) por área y
    profesional. La agenda es el cuello de botella → esto la mide."""
    import json
    lo = (desde + " 00:00:00") if desde else "2000-01-01 00:00:00"
    hi = (hasta + " 23:59:59") if hasta else "2100-01-01 00:00:00"
    from session import db
    with db() as conn:
        rows = conn.execute(
            """SELECT phone, event, meta, ts FROM conversation_events
                WHERE event IN ('funnel_especialidad','funnel_slot_ofrecido','cita_creada')
                  AND ts >= ? AND ts <= ?
                ORDER BY phone, ts""",
            (lo, hi),
        ).fetchall()

    per: dict[str, dict] = {}
    for r in rows:
        d = per.setdefault(r["phone"], {})
        ev, ts = r["event"], r["ts"]
        try:
            meta = json.loads(r["meta"] or "{}")
        except Exception:
            meta = {}
        if ev == "funnel_especialidad" and "area_ts" not in d:
            d["area_ts"] = ts
        elif ev == "funnel_slot_ofrecido" and "slot_ts" not in d:
            d["slot_ts"] = ts
        elif ev == "cita_creada" and "cita_ts" not in d:
            d["cita_ts"] = ts
            d["area"] = (meta.get("especialidad") or "").strip()
            d["prof"] = (meta.get("profesional") or "").strip()

    def secs(a, b):
        try:
            return (datetime.strptime(b, "%Y-%m-%d %H:%M:%S")
                    - datetime.strptime(a, "%Y-%m-%d %H:%M:%S")).total_seconds()
        except Exception:
            return None

    trans = {"area_slot": [], "slot_cita": [], "area_cita": []}
    by_area: dict[str, list] = {}
    by_prof: dict[str, list] = {}
    n_area = n_slot = n_cita = 0
    for d in per.values():
        if d.get("area_ts"):
            n_area += 1
        if d.get("slot_ts"):
            n_slot += 1
        if d.get("cita_ts"):
            n_cita += 1
        if d.get("area_ts") and d.get("slot_ts"):
            s = secs(d["area_ts"], d["slot_ts"])
            if s is not None and s >= 0:
                trans["area_slot"].append(s)
        if d.get("slot_ts") and d.get("cita_ts"):
            s = secs(d["slot_ts"], d["cita_ts"])
            if s is not None and s >= 0:
                trans["slot_cita"].append(s)
        if d.get("area_ts") and d.get("cita_ts"):
            s = secs(d["area_ts"], d["cita_ts"])
            if s is not None and s >= 0:
                trans["area_cita"].append(s)
                by_area.setdefault(_macrogrupo(d.get("area") or "") or "Otra", []).append(s)
                by_prof.setdefault(d.get("prof") or "Sin asignar", []).append(s)

    def avg(l):
        return round((sum(l) / len(l)) / 60, 1) if l else None

    return {
        "embudo": {"area": n_area, "slot": n_slot, "cita": n_cita,
                   "conv": round(100 * n_cita / n_area, 1) if n_area else 0},
        "friccion_min": {k: {"min": avg(v), "n": len(v)} for k, v in trans.items()},
        "por_area": sorted([{"k": a, "min": avg(v), "n": len(v)} for a, v in by_area.items() if v],
                           key=lambda x: -(x["min"] or 0))[:10],
        "por_profesional": sorted([{"k": p, "min": avg(v), "n": len(v)} for p, v in by_prof.items() if v],
                                  key=lambda x: -(x["min"] or 0))[:12],
        "desde": desde, "hasta": hasta,
    }


@router.get("/friccion")
async def api_friccion(rango: str | None = Query(None),
                       desde: str | None = Query(None),
                       hasta: str | None = Query(None),
                       token: str | None = Query(None),
                       cmc_session: str | None = Cookie(None),
                       request: Request = None):
    _auth(token, cmc_session, request)
    d, h = _rango_a_fechas(rango, desde, hasta)
    return _friccion(d, h)
