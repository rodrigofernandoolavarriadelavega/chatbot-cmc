"""Medición de abandono en Medicina General.

Pacientes que pidieron MG y NO terminaron con cita del bot. La telemetría
existente subestima esto: el evento `sin_disponibilidad` casi nunca dispara en
MG porque siempre hay algún cupo (aunque sea lejano), así que el paciente "se va
sin agendar" sin dejar rastro de no-disponibilidad. Aquí se reconstruye el
embudo real desde `conversation_events` (retención indefinida) → se recomputa
on-demand, sin job ni cambios en el motor conversacional.

Auth: token de Alma (_is_admin_token) o cookie cmc_session. Solo lectura.
"""
from __future__ import annotations

import json

from fastapi import Query, Cookie, HTTPException


def _auth(token, cmc_session):
    from admin_routes import _verify_cookie, _is_admin_token
    if not ((token and _is_admin_token(token)) or (cmc_session and _verify_cookie(cmc_session))):
        raise HTTPException(403, "No autorizado")


def _is_mg(s) -> bool:
    s = (s or "").lower()
    return "medicina general" in s or "medicina familiar" in s or s in ("general", "medfam")


def medir(c, dias: int = 30) -> dict:
    """Reconstruye el embudo MG en los últimos `dias`: quién pidió, quién logró
    cita del bot, quién quedó sin cita, y de esos cuántos rescató recepción."""
    desde = f"-{int(dias)} day"
    # última solicitud MG por teléfono (intent / funnel / motivo)
    ult: dict = {}
    for ev in ("intent_agendar", "funnel_especialidad", "motivo_seleccionado"):
        for meta, phone, ts in c.execute(
            "SELECT meta, phone, ts FROM conversation_events WHERE event=? AND ts>=date('now',?)",
            (ev, desde)).fetchall():
            try:
                d = json.loads(meta) if meta else {}
            except Exception:
                d = {}
            if _is_mg(d.get("especialidad") or d.get("esp") or ""):
                if phone not in ult or ts > ult[phone]:
                    ult[phone] = ts
    # citas MG creadas por el bot
    cit: set = set()
    for meta, phone in c.execute(
        "SELECT meta, phone FROM conversation_events WHERE event='cita_creada' AND ts>=date('now',?)",
        (desde,)).fetchall():
        try:
            d = json.loads(meta) if meta else {}
        except Exception:
            d = {}
        if _is_mg(d.get("especialidad", "")):
            cit.add(phone)
    sin = set(ult) - cit
    # nombres conocidos
    nom: dict = {}
    try:
        for p, n in c.execute("SELECT phone, nombre FROM contact_profiles").fetchall():
            if n:
                nom[p] = " ".join(str(n).split())
    except Exception:
        nom = {}
    lista = []
    # clasificación: 'abandono' (recep no respondió) · 'recep_sin_cupo' (recep dijo
    # que no hay) · 'posible_rescate' (recep respondió otra cosa → quizá agendó manual).
    # OJO: recepción respondió ≠ rescatado — muchas veces es "nada disponible para hoy".
    NEG = ("nada dispon", "no hay", "sin cupo", "no tenemos", "no queda", "agotad",
           "lleno", "no dispon", "completo", "no hay hora", "no hay cupo",
           "no contamos", "ya no", "sin hora")
    n_aband = n_negativa = n_posible = 0
    for p in sin:
        evs = [r[0] for r in c.execute(
            "SELECT event FROM conversation_events WHERE phone=? AND ts>=date('now',?)",
            (p, desde)).fetchall()]
        vio = any("slot" in e for e in evs)
        deriv = any("derivado_humano" in e or "takeover" in e for e in evs)
        msgs = [str(t).lower() for (t,) in c.execute(
            "SELECT text FROM messages WHERE phone=? AND text LIKE '[Recep%' AND ts>=date('now',?)",
            (p, desde)).fetchall()]
        if not msgs:
            clasif = "abandono"; n_aband += 1
        elif any(any(k in m for k in NEG) for m in msgs):
            clasif = "recep_sin_cupo"; n_negativa += 1
        else:
            clasif = "posible_rescate"; n_posible += 1
        lista.append({"phone": p, "nombre": nom.get(p, ""), "ultima": str(ult[p])[:16],
                      "vio_cupos": bool(vio), "derivado": bool(deriv), "clasif": clasif})
    lista.sort(key=lambda x: x["ultima"], reverse=True)
    return {
        "dias": dias,
        "pidieron_mg": len(ult),
        "con_cita_bot": len(set(ult) & cit),
        "sin_cita_bot": len(sin),
        "abandono": n_aband,                 # recepción no respondió
        "recep_sin_cupo": n_negativa,        # recepción confirmó que no hay cupo
        "posible_rescate": n_posible,        # recepción respondió otra cosa (no confirmable)
        "perdidos": n_aband + n_negativa,    # piso seguro de perdidos
        "perdidos_max": len(sin),            # techo (si ningún 'posible_rescate' se agendó)
        "lista": lista,
    }


def register_mg_abandono_routes(app):
    @app.get("/api/mg-abandono", tags=["mg-abandono"], include_in_schema=False)
    def mg_abandono(dias: int = Query(30), lista: int = Query(0),
                    token: str | None = Query(None), cmc_session: str | None = Cookie(None)):
        _auth(token, cmc_session)
        from session import db
        with db() as c:
            res = medir(c, dias=dias)
        if not lista:
            res = {k: v for k, v in res.items() if k != "lista"}
        return res
