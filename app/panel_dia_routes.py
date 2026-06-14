"""Endpoints de datos REALES para el Panel del Día (v1).

- GET /api/panel-dia/financiero  → Nivel 3 (ingresos, EBITDA, métodos, conciliación, transferencias)
- GET /api/panel-dia/operativo    → Nivel 1 (agenda del día por profesional + venta real)
- GET /api/panel-dia/chat         → Nivel 2 (derivados, lista de espera, multiagente)
- GET /api/panel-dia/conv         → Nivel 2 (conversación de un paciente)

Auth: token de Alma (_is_admin_token) o cookie cmc_session. Solo lectura.
Datos: sessions.db (SQLCipher) vía session.db(). NO llama Medilink en vivo (evita 429).
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta

from fastapi import Query, Cookie, HTTPException


def _auth(token, cmc_session):
    from admin_routes import _verify_cookie, _is_admin_token
    if not ((token and _is_admin_token(token)) or (cmc_session and _verify_cookie(cmc_session))):
        raise HTTPException(403, "No autorizado")


def _canal(phone: str) -> str:
    p = (phone or "").lower()
    if p.startswith("fb_") or p.startswith("fb:"):
        return "Facebook"
    if p.startswith("ig_") or p.startswith("ig:"):
        return "Instagram"
    return "WhatsApp"


def _canal_cls(canal: str) -> str:
    return {"WhatsApp": "wa", "Instagram": "ig", "Facebook": "fb"}.get(canal, "wa")


def _hace_min(ts) -> int:
    try:
        s = str(ts).replace("T", " ").split(".")[0]
        t = datetime.fromisoformat(s)
        return max(0, int((datetime.now() - t).total_seconds() // 60))
    except Exception:
        return 0


def _nombre(c, phone: str) -> str:
    """Mejor esfuerzo para el nombre del paciente; cae al teléfono enmascarado."""
    for sql in (
        "SELECT nombre FROM contact_profiles WHERE phone=?",
        "SELECT nombre FROM contactos WHERE phone=?",
    ):
        try:
            r = c.execute(sql, (phone,)).fetchone()
            if r and r[0]:
                return " ".join(str(r[0]).split())
        except Exception:
            pass
    try:
        r = c.execute("SELECT data FROM sessions WHERE phone=?", (phone,)).fetchone()
        if r and r[0]:
            d = json.loads(r[0])
            for k in ("nombre", "paciente_nombre", "nombre_paciente"):
                if d.get(k):
                    return " ".join(str(d[k]).split())
    except Exception:
        pass
    p = str(phone or "")
    return ("Paciente " + p[-4:]) if p[-4:].isdigit() else "Paciente"


# ── mapeo especialidad Medilink → clave de área del frontend ──────────────────
def _area_key(esp: str) -> str:
    e = (esp or "").lower()
    if "gastro" in e: return "gastro"
    if "cardio" in e: return "cardio"
    if "gineco" in e: return "gineco"
    if "otorrino" in e or "orl" in e: return "orl"
    if "trauma" in e: return "traumato"
    if "ecograf" in e or "eco" in e: return "eco"
    if "matron" in e: return "matrona"
    if "kinesi" in e: return "kine"
    if "nutri" in e: return "nutricion"
    if "psiquiatr" in e: return "psiquiatria"
    if "psico" in e: return "psicologia"
    if "fono" in e: return "fono"
    if "odonto" in e or "dental" in e or "ortodon" in e or "endodon" in e or "implant" in e: return "dental"
    if "estétic" in e or "estetic" in e: return "estetica"
    return "general"


def register_panel_dia_routes(app):

    # ───────────────────────── NIVEL 3 · financiero ─────────────────────────
    @app.get("/api/panel-dia/financiero", tags=["panel-dia"], include_in_schema=False)
    def panel_financiero(mes: str | None = Query(None), token: str | None = Query(None),
                         cmc_session: str | None = Cookie(None)):
        _auth(token, cmc_session)
        from session import db
        from ebitda_routes import _ebitda_mes
        if not mes:
            mes = date.today().strftime("%Y-%m")
        with db() as c:
            e = _ebitda_mes(c, mes)
            prev_mes = (date.fromisoformat(mes + "-01") - timedelta(days=1)).strftime("%Y-%m")
            try:
                ep_ing = _ebitda_mes(c, prev_mes).get("ingresos", 0)
            except Exception:
                ep_ing = 0
            # por área (desde profesionales del EBITDA)
            area: dict[str, int] = {}
            for p in e.get("profesionales", []):
                a = p.get("especialidad") or "Otros"
                area[a] = area.get(a, 0) + int(p.get("ingreso") or 0)
            por_area = sorted([[k, v] for k, v in area.items() if v], key=lambda x: -x[1])[:9]
            # métodos reales (pagos_cmc)
            metodos = [[(m or "sin método").capitalize(), int(v or 0)] for m, n, v in c.execute(
                "SELECT metodo_pago, COUNT(*), SUM(copago) FROM pagos_cmc "
                "WHERE substr(fecha,1,7)=? GROUP BY metodo_pago ORDER BY 3 DESC", (mes,)).fetchall() if v]
            # conciliación: Medilink (bi_pagos_caja) vs recepción (pagos_cmc)
            medilink = int(c.execute("SELECT COALESCE(SUM(monto),0) FROM bi_pagos_caja WHERE substr(fecha,1,7)=?", (mes,)).fetchone()[0] or 0)
            recep = int(c.execute("SELECT COALESCE(SUM(copago),0) FROM pagos_cmc WHERE substr(fecha,1,7)=?", (mes,)).fetchone()[0] or 0)
            n_med = c.execute("SELECT COUNT(*) FROM bi_pagos_caja WHERE substr(fecha,1,7)=?", (mes,)).fetchone()[0]
            n_rec = c.execute("SELECT COUNT(*) FROM pagos_cmc WHERE substr(fecha,1,7)=?", (mes,)).fetchone()[0]
            fuentes = [["Medilink (caja)", n_med, medilink], ["Recepción (copago)", n_rec, recep]]
            for m, n, v in c.execute("SELECT metodo_pago, COUNT(*), SUM(copago) FROM pagos_cmc WHERE substr(fecha,1,7)=? GROUP BY metodo_pago ORDER BY 3 DESC", (mes,)).fetchall():
                if v:
                    fuentes.append(["· " + (m or "sin método").capitalize(), n, int(v or 0)])
            # transferencias del copago (para cruce con cartola Itaú — cartola no disponible aún)
            transf = []
            for fch, nom, monto, cod in c.execute(
                "SELECT fecha, paciente_nombre, copago, codigo_transferencia FROM pagos_cmc "
                "WHERE substr(fecha,1,7)=? AND lower(metodo_pago) LIKE '%transf%' ORDER BY fecha DESC LIMIT 40", (mes,)).fetchall():
                tiene = bool((cod or "").strip())
                transf.append({"fecha": (fch or "")[5:], "ref": (cod or "—"), "pac": " ".join((nom or "").split())[:28] or "—",
                               "monto": int(monto or 0), "match": tiene})
        return {
            "mes": mes,
            "ingresos": e["ingresos"], "ebitda": e["ebitda"], "ebitdaPct": e["margen_ebitda_pct"],
            "honorarios": e["honorarios_bruto"], "gastos": e["gastos"], "prevIngresos": ep_ing,
            "porArea": por_area, "metodos": metodos,
            "conciliacion": {"medilink": medilink, "recepcion": recep, "dif": medilink - recep,
                             "pct": round(recep / medilink * 100) if medilink else 0,
                             "n_medilink": n_med, "n_recepcion": n_rec, "fuentes": fuentes},
            "itau": {"transferencias": transf, "cartola_disponible": False,
                     "conciliadas": sum(1 for t in transf if t["match"]), "total": len(transf)},
            "flujo": {"ebitda": e["ebitda"], "entradas": e["ingresos"], "salidas": e["honorarios_bruto"] + e["gastos"]},
        }

    # ───────────────────────── NIVEL 1 · operativo ──────────────────────────
    @app.get("/api/panel-dia/operativo", tags=["panel-dia"], include_in_schema=False)
    def panel_operativo(fecha: str | None = Query(None), auto: int = Query(1),
                        token: str | None = Query(None), cmc_session: str | None = Cookie(None)):
        _auth(token, cmc_session)
        from session import db
        from medilink import PROFESIONALES
        if not fecha:
            fecha = date.today().isoformat()
        with db() as c:
            # auto-fallback: si la fecha no tiene agenda, usar la última que sí (para no abrir vacío)
            if auto and not c.execute("SELECT 1 FROM citas_cache WHERE fecha=? LIMIT 1", (fecha,)).fetchone():
                r = c.execute("SELECT fecha FROM citas_cache WHERE fecha<=? ORDER BY fecha DESC LIMIT 1", (fecha,)).fetchone()
                if r:
                    fecha = r[0]
            citas: dict = {}
            for idp, idpac, nom, hora in c.execute(
                "SELECT id_prof, id_paciente, paciente_nombre, hora_inicio FROM citas_cache "
                "WHERE fecha=? ORDER BY hora_inicio", (fecha,)).fetchall():
                citas.setdefault(idp, []).append({"id_pac": idpac, "paciente": " ".join((nom or "Paciente").split())[:24], "hora": (hora or "")[:5]})
            pagos: dict = {}
            for idp, idpac, monto in c.execute(
                "SELECT id_profesional, id_paciente, SUM(monto) FROM bi_pagos_caja WHERE fecha=? "
                "GROUP BY id_profesional, id_paciente", (fecha,)).fetchall():
                pagos[(idp, idpac)] = int(monto or 0)
            # previsión (Fonasa/particular) por RUT — pagos_cmc no tiene id_paciente, se cruza por nombre↔citas no es fiable;
            # se usa solo para los contadores, no para la valorización (que es por profesional).
            prev_rut: dict = {}
            try:
                for nom, pv in c.execute("SELECT paciente_nombre, prevision FROM pagos_cmc WHERE fecha=?", (fecha,)).fetchall():
                    if nom:
                        prev_rut[" ".join(str(nom).split()).lower()] = "fonasa" if "fonasa" in (pv or "").lower() else "particular"
            except Exception:
                prev_rut = {}
            # ticket promedio 90 días por profesional
            desde = (date.fromisoformat(fecha) - timedelta(days=90)).strftime("%Y-%m-%d")
            ticket: dict = {}
            for idp, t in c.execute("SELECT id_profesional, AVG(monto) FROM bi_pagos_caja WHERE fecha>=? AND monto>0 GROUP BY id_profesional", (desde,)).fetchall():
                ticket[idp] = int(t or 0)
            profs = []
            for idp, info in PROFESIONALES.items():
                ag = citas.get(idp, [])
                tk = ticket.get(idp) or 18000
                agenda = []
                for cita in ag:
                    pago = pagos.get((idp, cita["id_pac"]), 0)
                    tipo = prev_rut.get(cita["paciente"].lower(), "particular")
                    agenda.append({"hora": cita["hora"], "paciente": cita["paciente"],
                                   "estado": "atendido" if pago > 0 else "agendado",
                                   "monto": pago, "esperado": tk, "tipo": tipo})
                profs.append({
                    "id": idp, "nombre": info.get("nombre", f"Prof {idp}"),
                    "area": _area_key(info.get("especialidad", "")),
                    "ticket": tk, "presente": len(ag) > 0, "cap": len(agenda), "agenda": agenda,
                })
        return {"fecha": fecha, "profesionales": profs}

    # ───────────────────────── NIVEL 2 · chat ───────────────────────────────
    @app.get("/api/panel-dia/chat", tags=["panel-dia"], include_in_schema=False)
    def panel_chat(token: str | None = Query(None), cmc_session: str | None = Cookie(None)):
        _auth(token, cmc_session)
        from session import db
        with db() as c:
            derivados = []
            for phone, updated in c.execute("SELECT phone, updated_at FROM sessions WHERE state='HUMAN_TAKEOVER' ORDER BY updated_at DESC").fetchall():
                last = c.execute("SELECT text, ts, direction FROM messages WHERE phone=? ORDER BY ts DESC LIMIT 1", (phone,)).fetchone()
                canal = _canal(phone)
                derivados.append({"id": phone, "nombre": _nombre(c, phone), "canal": canal, "canalCls": _canal_cls(canal),
                                  "ultimo": (str(last[0])[:60] if last and last[0] else ""), "hace": _hace_min(last[1] if last else updated)})
            # lista de espera / multiagente desde waitlist_offers
            espera_g: dict = {}
            agente = {"contactados": 0, "agendados": 0, "pendientes": 0, "grupos": {}}
            try:
                rows = c.execute("SELECT phone, nombre, especialidad, id_prof, fecha, hora, estado FROM waitlist_offers ORDER BY created_at DESC").fetchall()
            except Exception:
                rows = []
            from medilink import PROFESIONALES
            for phone, nombre, esp, idp, fch, hr, estado in rows:
                profn = (PROFESIONALES.get(idp, {}) or {}).get("nombre") or (esp or "Profesional")
                pac = {"id": phone, "nombre": " ".join((nombre or _nombre(c, phone)).split()),
                       "canal": _canal(phone), "canalCls": _canal_cls(_canal(phone)),
                       "esp": esp or "", "slot": (str(fch or "")[5:] + " " + str(hr or "")[:5]).strip(),
                       "estado": estado or "enviada", "hace": _hace_min(None)}
                espera_g.setdefault(profn, {"prof": profn, "area": _area_key(esp), "pacientes": []})["pacientes"].append(pac)
                agente["contactados"] += 1
                if str(estado).lower() in ("agendada", "agendado", "claimed", "recepcion"):
                    agente["agendados"] += 1
                else:
                    agente["pendientes"] += 1
                agente["grupos"].setdefault(profn, {"prof": profn, "area": _area_key(esp), "pacientes": []})["pacientes"].append(pac)
        return {
            "derivados": derivados,
            "espera": list(espera_g.values()),
            "agente": {"contactados": agente["contactados"], "agendados": agente["agendados"],
                       "pendientes": agente["pendientes"], "grupos": list(agente["grupos"].values())},
        }

    @app.get("/api/panel-dia/conv", tags=["panel-dia"], include_in_schema=False)
    def panel_conv(phone: str = Query(...), token: str | None = Query(None),
                   cmc_session: str | None = Cookie(None)):
        _auth(token, cmc_session)
        from session import db
        with db() as c:
            msgs = []
            for direction, text, ts in c.execute(
                "SELECT direction, text, ts FROM messages WHERE phone=? ORDER BY ts ASC LIMIT 60", (phone,)).fetchall():
                if not text:
                    continue
                de = "pac" if str(direction) == "in" else "bot"
                t = str(text)
                if de == "bot" and t.startswith("[Recep"):
                    de = "rec"
                msgs.append({"de": de, "txt": t, "t": str(ts)[11:16]})
            nombre = _nombre(c, phone)
        return {"phone": phone, "nombre": nombre, "canal": _canal(phone), "msgs": msgs}
