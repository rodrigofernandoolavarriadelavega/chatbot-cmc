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
            # auto-fallback: si la fecha no tiene ACTIVIDAD (ni pagos ni citas), usar la última con pagos
            if auto and not c.execute("SELECT 1 FROM bi_pagos_caja WHERE fecha=? LIMIT 1", (fecha,)).fetchone() \
                     and not c.execute("SELECT 1 FROM citas_cache WHERE fecha=? LIMIT 1", (fecha,)).fetchone():
                r = c.execute("SELECT fecha FROM bi_pagos_caja WHERE fecha<=? ORDER BY fecha DESC LIMIT 1", (fecha,)).fetchone()
                if r:
                    fecha = r[0]
            # citas del día (agenda programada, donde el caché del bot la tenga)
            citas: dict = {}
            for idp, idpac, nom, hora in c.execute(
                "SELECT id_prof, id_paciente, paciente_nombre, hora_inicio FROM citas_cache "
                "WHERE fecha=? ORDER BY hora_inicio", (fecha,)).fetchall():
                citas.setdefault(idp, []).append({"id_pac": idpac, "paciente": " ".join((nom or "Paciente").split())[:24], "hora": (hora or "")[:5]})
            # pagos del día = ATENCIONES REALES → fuente de verdad de "presente" y venta (NO citas_cache, que es parcial)
            pagos: dict = {}        # idp -> {idpac: monto}
            pac_ids: set = set()
            for idp, idpac, monto in c.execute(
                "SELECT id_profesional, id_paciente, SUM(monto) FROM bi_pagos_caja WHERE fecha=? "
                "GROUP BY id_profesional, id_paciente", (fecha,)).fetchall():
                pagos.setdefault(idp, {})[idpac] = int(monto or 0)
                pac_ids.add(idpac)
            # nombres por id_paciente (del caché de citas, cualquier fecha) para atenciones sin cita cacheada
            nombres: dict = {}
            if pac_ids:
                ph = ",".join("?" * len(pac_ids))
                try:
                    for idpac, nom in c.execute(
                        f"SELECT id_paciente, paciente_nombre FROM citas_cache WHERE id_paciente IN ({ph}) GROUP BY id_paciente",
                        tuple(pac_ids)).fetchall():
                        if nom:
                            nombres[idpac] = " ".join(str(nom).split())[:24]
                except Exception:
                    pass
            # previsión (Fonasa/particular) por nombre — solo para contadores (pagos_cmc no tiene id_paciente)
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
            # capacidad de slots por profesional = percentil ~85 de pacientes-distintos/día
            # (su "día lleno" en los últimos 120 días ≈ su capacidad real de cupos).
            # Medilink /agendas solo trae slots libres futuros (capados ~10) → inútil para
            # fechas pasadas; este proxy data-driven funciona para cualquier fecha sin fan-out.
            cap_desde = (date.fromisoformat(fecha) - timedelta(days=120)).strftime("%Y-%m-%d")
            dcounts: dict = {}
            for idp, _f, n in c.execute(
                "SELECT id_profesional, fecha, COUNT(DISTINCT id_paciente) FROM bi_pagos_caja "
                "WHERE fecha BETWEEN ? AND ? AND monto>0 GROUP BY id_profesional, fecha",
                (cap_desde, fecha)).fetchall():
                dcounts.setdefault(idp, []).append(int(n or 0))

            # capacidad/ocupación REAL del día (cache nocturna Medilink, si existe)
            # → preferida sobre el proxy de pagos, que subestima (Fonasa sin pago).
            cap_real: dict = {}
            try:
                for idp, cp, ncit, nat in c.execute(
                    "SELECT id_profesional, cap, n_citas, n_at FROM panel_cap_cache WHERE fecha=?",
                    (fecha,)).fetchall():
                    cap_real[idp] = {"cap": int(cp or 0), "n_citas": int(ncit or 0), "n_at": int(nat or 0)}
            except Exception:
                cap_real = {}

            def _cap_for(idp, used):
                r = cap_real.get(idp)
                if r and r["cap"]:                 # capacidad real (Medilink) — preferida
                    return max(r["cap"], used)
                arr = sorted(dcounts.get(idp, []))
                base = 0
                if arr:
                    k = max(0, min(len(arr) - 1, int(round(0.85 * (len(arr) - 1)))))
                    base = arr[k]
                # nunca menos que lo realmente usado ese día; piso 4 si el prof trabajó
                return max(used, base, 4 if used else base)

            hoy_iso = date.today().isoformat()
            es_pasado = fecha < hoy_iso
            profs = []
            for idp, info in PROFESIONALES.items():
                cit = citas.get(idp, [])
                pg = pagos.get(idp, {})
                tk = ticket.get(idp) or 18000
                agenda = []
                cited: set = set()
                for cita in cit:
                    pago = pg.get(cita["id_pac"], 0)
                    # cita sin pago: en un día pasado = no se presentó (falto); hoy/futuro = agendado
                    if pago > 0:
                        est = "atendido"
                    elif es_pasado:
                        est = "falto"
                    else:
                        est = "agendado"
                    agenda.append({"hora": cita["hora"], "paciente": cita["paciente"],
                                   "estado": est,
                                   "monto": pago, "esperado": tk,
                                   "tipo": prev_rut.get(cita["paciente"].lower(), "particular")})
                    cited.add(cita["id_pac"])
                # atenciones con pago pero sin cita cacheada (agendadas en recepción / walk-in) → atendidas
                for idpac, monto in pg.items():
                    if idpac in cited:
                        continue
                    nom = nombres.get(idpac, "Atención")
                    agenda.append({"hora": "—", "paciente": nom, "estado": "atendido",
                                   "monto": monto, "esperado": tk,
                                   "tipo": prev_rut.get(nom.lower(), "particular")})
                # nº de cupos realmente ocupados (atendido/falto/agendado) ese día
                usados = len(agenda)
                cap = _cap_for(idp, usados)
                n_at = sum(1 for x in agenda if x["estado"] == "atendido")
                n_falto = sum(1 for x in agenda if x["estado"] == "falto")
                # ocupación real (cache Medilink) → coherente con cap; si no, lo pagado
                r = cap_real.get(idp)
                ocup = max(r["n_citas"], usados) if r and r["n_citas"] else usados
                presente = bool(agenda) or bool(r and r["n_citas"])
                profs.append({
                    "id": idp, "nombre": info.get("nombre", f"Prof {idp}"),
                    "area": _area_key(info.get("especialidad", "")),
                    "ticket": tk, "presente": presente, "cap": cap, "ocup": ocup,
                    "usados": usados, "nAt": n_at, "nFalto": n_falto, "agenda": agenda,
                })
        return {"fecha": fecha, "profesionales": profs}

    # ───── NIVEL 1 · comparativa histórica: real vs cupos vs techo histórico ─────
    # Fuente: bi_pagos_caja (libro de la verdad — VENTA). Períodos comparables:
    #   dia    → el MISMO día de la semana en 2 años (ej: el mejor LUNES histórico)
    #   semana → semanas lunes-domingo, mejor semana COMPLETA en 2 años
    #   mes    → meses calendario, mejor mes COMPLETO en 2 años
    # Potencial (máximo por cupos) = cap_día × ticket × días típicos trabajados en
    # el período (p85 histórico) — data-driven, sin depender de la agenda futura.
    # ?prof=ID → la serie es de ese profesional; sin prof → serie del total.
    @app.get("/api/panel-dia/comparativa", tags=["panel-dia"], include_in_schema=False)
    def panel_comparativa(modo: str = Query("dia"), fecha: str | None = Query(None),
                          prof: int = Query(0), n: int = Query(12),
                          token: str | None = Query(None), cmc_session: str | None = Cookie(None)):
        _auth(token, cmc_session)
        from session import db
        from medilink import PROFESIONALES
        if modo not in ("dia", "semana", "mes"):
            raise HTTPException(400, "modo inválido (dia|semana|mes)")
        try:
            f = date.fromisoformat(fecha) if fecha else date.today()
        except ValueError:
            raise HTTPException(400, "fecha inválida")
        n = max(4, min(24, n))
        desde = (f - timedelta(days=730)).isoformat()
        hasta = f.isoformat()

        with db() as c:
            # venta diaria por profesional — UNA query, se agrega en Python
            daily: dict = {}            # idp -> {fecha_iso: monto}
            for idp, fe, m in c.execute(
                "SELECT id_profesional, fecha, SUM(monto) FROM bi_pagos_caja "
                "WHERE fecha BETWEEN ? AND ? AND monto>0 GROUP BY id_profesional, fecha",
                (desde, hasta)).fetchall():
                daily.setdefault(idp, {})[fe] = int(m or 0)
            t_desde = (f - timedelta(days=90)).isoformat()
            ticket = {idp: int(t or 0) for idp, t in c.execute(
                "SELECT id_profesional, AVG(monto) FROM bi_pagos_caja "
                "WHERE fecha>=? AND monto>0 GROUP BY id_profesional", (t_desde,)).fetchall()}
            cap_desde = (f - timedelta(days=120)).isoformat()
            dcounts: dict = {}
            for idp, _fe, cnt in c.execute(
                "SELECT id_profesional, fecha, COUNT(DISTINCT id_paciente) FROM bi_pagos_caja "
                "WHERE fecha BETWEEN ? AND ? AND monto>0 GROUP BY id_profesional, fecha",
                (cap_desde, hasta)).fetchall():
                dcounts.setdefault(idp, []).append(int(cnt or 0))
            cap_cache: dict = {}
            try:
                for idp, cp in c.execute(
                    "SELECT id_profesional, cap FROM panel_cap_cache WHERE fecha=?", (hasta,)).fetchall():
                    if cp:
                        cap_cache[idp] = int(cp)
            except Exception:
                pass

        def p85(arr):
            if not arr:
                return 0
            a = sorted(arr)
            return a[max(0, min(len(a) - 1, int(round(0.85 * (len(a) - 1)))))]

        def cap_for(idp):
            return cap_cache.get(idp) or p85(dcounts.get(idp, []))

        # clave de período para una fecha + pertenencia al conjunto comparable
        if modo == "dia":
            def keyf(d): return d.isoformat()
            def member(d): return d.weekday() == f.weekday()
        elif modo == "semana":
            def keyf(d): return (d - timedelta(days=d.weekday())).isoformat()   # lunes
            def member(d): return True
        else:
            def keyf(d): return d.strftime("%Y-%m")
            def member(d): return True
        target_key = keyf(f)

        # agregación por (profesional, período) + días activos por período
        per: dict = {}                  # idp -> {key: monto}
        pdays: dict = {}                # idp -> {key: n_dias_activos}
        for idp, dd in daily.items():
            for fe, m in dd.items():
                d = date.fromisoformat(fe)
                if not member(d):
                    continue
                k = keyf(d)
                per.setdefault(idp, {})
                per[idp][k] = per[idp].get(k, 0) + m
                pdays.setdefault(idp, {})
                pdays[idp][k] = pdays[idp].get(k, 0) + 1

        # techo honesto: el período EN CURSO (target_key) nunca compite consigo mismo;
        # los períodos pasados están completos por construcción (la ventana parte 730d atrás)
        profs_out = []
        for idp, info in PROFESIONALES.items():
            mine = per.get(idp, {})
            real = mine.get(target_key, 0)
            hist_k, hist_v = None, 0
            for k, v in mine.items():
                if k == target_key:
                    continue
                if v > hist_v:
                    hist_v, hist_k = v, k
            tk = ticket.get(idp, 0)
            cp = cap_for(idp)
            if modo == "dia":
                dias_tip = 1
            else:
                dvals = [v for k, v in pdays.get(idp, {}).items() if k != target_key]
                dias_tip = p85(dvals)
            pot = cp * tk * dias_tip
            profs_out.append({"id": idp, "real": real, "pot": pot,
                              "hist": hist_v, "hist_key": hist_k})

        # serie de los últimos n períodos (total o de un profesional)
        def prev_key(d, i):
            if modo == "dia":
                return keyf(d - timedelta(days=7 * i))
            if modo == "semana":
                return keyf(d - timedelta(days=7 * i))
            mth = (d.year * 12 + d.month - 1) - i
            return f"{mth // 12:04d}-{mth % 12 + 1:02d}"
        keys = [prev_key(f, i) for i in range(n - 1, -1, -1)]
        src = {prof: per.get(prof, {})} if prof else per
        serie = []
        for k in keys:
            tot = sum(mine.get(k, 0) for mine in src.values())
            if prof:
                p_pot = next((x["pot"] for x in profs_out if x["id"] == prof), 0)
                pot_k = p_pot if tot > 0 or k == target_key else 0
            else:
                pot_k = sum(x["pot"] for x in profs_out
                            if per.get(x["id"], {}).get(k, 0) > 0 or k == target_key)
            serie.append({"k": k, "real": tot, "pot": pot_k})
        # techo histórico del agregado de la serie (excluye el período en curso)
        agg: dict = {}
        for mine in src.values():
            for k, v in mine.items():
                if k != target_key:
                    agg[k] = agg.get(k, 0) + v
        hist_total_key = max(agg, key=agg.get) if agg else None
        return {"modo": modo, "fecha": hasta, "target_key": target_key,
                "profesionales": profs_out, "serie": serie,
                "hist_total": agg.get(hist_total_key, 0), "hist_total_key": hist_total_key}

    # ───── NIVEL 1 · agenda detallada de UN profesional (lazy, al fijar popup) ─────
    # UNA sola llamada a Medilink /citas (sin fan-out de /pacientes → sin 429).
    # Devuelve la grilla del día POR HORARIO: cada cita en su hora real + cupos
    # libres rellenando los huecos de la ventana de trabajo observada.
    @app.get("/api/panel-dia/agenda", tags=["panel-dia"], include_in_schema=False)
    async def panel_agenda(prof: int = Query(...), fecha: str = Query(...),
                           token: str | None = Query(None), cmc_session: str | None = Cookie(None)):
        _auth(token, cmc_session)
        from session import db
        from medilink import citas_dia_lite, PROFESIONALES
        citas = await citas_dia_lite(prof, fecha)
        with db() as c:
            pg: dict = {}
            for idpac, monto in c.execute(
                "SELECT id_paciente, SUM(monto) FROM bi_pagos_caja WHERE fecha=? AND id_profesional=? GROUP BY id_paciente",
                (fecha, prof)).fetchall():
                pg[idpac] = int(monto or 0)
            desde90 = (date.fromisoformat(fecha) - timedelta(days=90)).strftime("%Y-%m-%d")
            row = c.execute("SELECT AVG(monto) FROM bi_pagos_caja WHERE id_profesional=? AND monto>0 AND fecha>=?",
                            (prof, desde90)).fetchone()
            tk = int((row and row[0]) or 18000)
            prev: dict = {}
            try:
                for nom, pv in c.execute("SELECT paciente_nombre, prevision FROM pagos_cmc WHERE fecha=?", (fecha,)).fetchall():
                    if nom:
                        prev[" ".join(str(nom).split()).lower()] = "fonasa" if "fonasa" in (pv or "").lower() else "particular"
            except Exception:
                prev = {}
        hoy_iso = date.today().isoformat()
        es_pasado = fecha < hoy_iso
        intervalo = (PROFESIONALES.get(prof, {}) or {}).get("intervalo") or 15

        def _h2m(h):
            try:
                a, b = str(h).split(":")[:2]
                return int(a) * 60 + int(b)
            except Exception:
                return None

        used_rows = []
        paid_seen: set = set()
        for ct in citas:
            est_txt = (ct["estado_cita"] or "").lower()
            monto = pg.get(ct["id_paciente"], 0)
            if ct["id_paciente"] in pg:
                paid_seen.add(ct["id_paciente"])
            if monto > 0 or "atend" in est_txt:
                estado = "atendido"
            elif any(k in est_txt for k in ("anul", "cancel", "no asist", "no asiste", "ausente", "falt", "no lleg")):
                estado = "falto"
            elif es_pasado:
                estado = "falto"   # cita pasada que no quedó atendida ni pagada
            else:
                estado = "agendado"
            used_rows.append({"hora": ct["hora"], "paciente": ct["paciente"], "estado": estado,
                              "monto": monto if estado == "atendido" else 0, "esperado": tk,
                              "tipo": prev.get(ct["paciente"].lower(), "particular")})
        # pagos sin cita registrada (walk-in) → atendidos sin hora
        walkins = []
        for idpac, monto in pg.items():
            if idpac in paid_seen:
                continue
            walkins.append({"hora": "—", "paciente": "Atención", "estado": "atendido",
                            "monto": monto, "esperado": tk, "tipo": "particular"})
        # grilla por horario: ventana observada, huecos = cupos libres
        from collections import defaultdict
        by_hora: dict = defaultdict(list)
        for r in used_rows:
            m = _h2m(r["hora"])
            by_hora[m if m is not None else -1].append(r)
        mins = [m for m in by_hora if m is not None and m >= 0]
        grid = []
        if mins:
            t0, t1 = min(mins), max(mins)
            t = t0
            while t <= t1:
                if t in by_hora:
                    grid.extend(by_hora.pop(t))
                else:
                    grid.append({"hora": f"{t // 60:02d}:{t % 60:02d}", "libre": True})
                t += intervalo
            for m in sorted(k for k in by_hora if k is not None and k >= 0):
                grid.extend(by_hora[m])
        else:
            grid = list(used_rows)
        grid.extend(walkins)
        n_at = sum(1 for r in grid if r.get("estado") == "atendido")
        n_falto = sum(1 for r in grid if r.get("estado") == "falto")
        return {"prof": prof, "fecha": fecha, "agenda": grid, "cap": len(grid),
                "nAt": n_at, "nFalto": n_falto, "fuente": "medilink" if citas else "pagos"}

    # ───── refresco manual del cache de capacidad real (backfill on-demand) ─────
    @app.post("/api/panel-dia/cap-cache/refresh", tags=["panel-dia"], include_in_schema=False)
    async def panel_cap_refresh(dias_atras: int = Query(21), dias_adelante: int = Query(3),
                                fecha: str | None = Query(None), full: int = Query(0),
                                token: str | None = Query(None), cmc_session: str | None = Cookie(None)):
        _auth(token, cmc_session)
        from panel_dia_jobs import refrescar_cap_cache
        return await refrescar_cap_cache(dias_atras=dias_atras, dias_adelante=dias_adelante,
                                         solo_fecha=fecha, full=bool(full))

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
