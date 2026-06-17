"""Nivel 4 del Panel del Día — Pago de Remuneraciones (honorarios profesionales).

Automatiza el flujo manual del dueño: por profesional calcula ingreso → boleta
(% de honorario) → transferencia (líquido tras retención 15,25%, salvo los que
facturan), lista TODAS las atenciones del mes (fecha/monto/medio/paciente) y arma
el texto para enviar por WhatsApp.

Reusa la lógica financiera ya probada de `ebitda_routes._ebitda_mes` (mismos %,
mismo factor 0,8475, mismos fijos/sin-retención). Datos 100% de `bi_pagos_caja`
(caja real Medilink ya sincronizada) — sin llamadas a Medilink en vivo.

Auth: token admin/olacore o cookie.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime

from fastapi import Query, Cookie, HTTPException, Request


def _auth(token, cmc_session):
    from admin_routes import _verify_cookie, _is_admin_token
    if not ((token and _is_admin_token(token)) or (cmc_session and _verify_cookie(cmc_session))):
        raise HTTPException(403, "No autorizado")


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z ]", " ", s)


_TITULOS = {"dr", "dra", "ps", "tm", "t", "m", "sr", "sra", "klgo", "flga", "nut"}


def _tokens(nombre: str) -> set:
    return {t for t in _norm(nombre).split() if t and t not in _TITULOS and len(t) > 2}


def _mes_bounds(mes: str):
    yr, mo = int(mes[:4]), int(mes[5:7])
    fin_y = yr + (mo // 12)
    fin_mo = (mo % 12) + 1
    return f"{mes}-01", f"{fin_y}-{fin_mo:02d}-01"


def _ensure_cols(c):
    try:
        c.execute("ALTER TABLE bi_pagos_caja ADD COLUMN nombre_paciente TEXT")
    except Exception:
        pass


def _ensure_estado(c):
    c.execute("""CREATE TABLE IF NOT EXISTS remuneracion_estado (
        id_profesional INTEGER NOT NULL, mes TEXT NOT NULL,
        pagado INTEGER DEFAULT 0, pagado_at TEXT,
        PRIMARY KEY (id_profesional, mes))""")


def _backfill_telefonos(c):
    """Rellena equipo_cmc.telefono (vacío) cruzando STAFF_PHONES por nombre. Una vez."""
    from config import STAFF_PHONES
    if not STAFF_PHONES:
        return
    # staff: lista de (telefono, tokens_del_nombre)
    staff = []
    for phone, label in STAFF_PHONES.items():
        nombre = label.split("—")[0].split("-")[0]
        staff.append((re.sub(r"\D", "", phone), _tokens(nombre)))
    try:
        rows = c.execute("SELECT id_medilink, nombre, telefono FROM equipo_cmc WHERE id_medilink IS NOT NULL").fetchall()
    except Exception:
        return
    for r in rows:
        if (r["telefono"] or "").strip():
            continue
        ptoks = _tokens(r["nombre"] or "")
        best, best_score = None, 0
        for phone, stoks in staff:
            score = len(ptoks & stoks)
            if score > best_score:
                best, best_score = phone, score
        if best and best_score >= 1:
            c.execute("UPDATE equipo_cmc SET telefono=? WHERE id_medilink=?", (best, r["id_medilink"]))


def register_remuneraciones_routes(app):

    @app.get("/api/panel-dia/remuneraciones", tags=["panel-dia"], include_in_schema=False)
    def remuneraciones(mes: str | None = Query(None),
                       token: str | None = Query(None), cmc_session: str | None = Cookie(None)):
        _auth(token, cmc_session)
        from session import db
        from ebitda_routes import _ebitda_mes, SIN_RETENCION
        if not mes:
            mes = datetime.now().strftime("%Y-%m")
        with db() as c:
            _ensure_cols(c)
            _ensure_estado(c)
            _backfill_telefonos(c)
            tel_map = {}
            try:
                for r in c.execute("SELECT id_medilink, telefono FROM equipo_cmc WHERE id_medilink IS NOT NULL").fetchall():
                    if r["telefono"]:
                        tel_map[r["id_medilink"]] = re.sub(r"\D", "", r["telefono"])
            except Exception:
                tel_map = {}
            pagados = {r["id_profesional"] for r in c.execute(
                "SELECT id_profesional FROM remuneracion_estado WHERE mes=? AND pagado=1", (mes,)).fetchall()}
            e = _ebitda_mes(c, mes)
        out = []
        tot_liq = tot_pagado = 0
        for p in e.get("profesionales", []):
            if not p.get("ingreso"):
                continue
            pagado = p["id"] in pagados
            tot_liq += p["liquido"]
            if pagado:
                tot_pagado += p["liquido"]
            out.append({
                "id": p["id"], "nombre": p["nombre"], "especialidad": p.get("especialidad", ""),
                "ingreso": p["ingreso"], "pct": p.get("pct"), "fijo": p.get("fijo", False),
                "bruto": p["bruto"], "liquido": p["liquido"], "pacientes": p.get("pacientes", 0),
                "sin_retencion": p["id"] in SIN_RETENCION,
                "telefono": tel_map.get(p["id"], ""), "pagado": pagado,
            })
        out.sort(key=lambda x: (x["pagado"], -x["ingreso"]))
        return {"mes": mes, "profesionales": out,
                "total_transferir": tot_liq, "total_pagado": tot_pagado,
                "total_por_pagar": tot_liq - tot_pagado,
                "n_pagados": len(pagados), "n_total": len(out)}

    @app.get("/api/panel-dia/remuneracion-detalle", tags=["panel-dia"], include_in_schema=False)
    def remuneracion_detalle(prof: int = Query(...), mes: str = Query(...),
                             token: str | None = Query(None), cmc_session: str | None = Cookie(None)):
        _auth(token, cmc_session)
        from session import db
        ini, fin = _mes_bounds(mes)
        with db() as c:
            _ensure_cols(c)
            rows = c.execute(
                "SELECT fecha, monto, metodo_pago, nombre_paciente, id_paciente "
                "FROM bi_pagos_caja WHERE id_profesional=? AND fecha>=? AND fecha<? "
                "ORDER BY fecha DESC, monto DESC", (prof, ini, fin)).fetchall()
        det = []
        total = 0
        con_nombre = 0
        for r in rows:
            nom = (r["nombre_paciente"] or "").strip()
            if nom:
                con_nombre += 1
            else:
                nom = f"Paciente #{r['id_paciente']}"
            total += int(r["monto"] or 0)
            det.append({"fecha": r["fecha"], "monto": int(r["monto"] or 0),
                        "metodo": r["metodo_pago"] or "", "paciente": nom})
        return {"prof": prof, "mes": mes, "detalle": det, "total": total,
                "n": len(det), "con_nombre": con_nombre}

    @app.post("/api/panel-dia/remuneracion-config", tags=["panel-dia"], include_in_schema=False)
    async def remuneracion_config(request: Request,
                                  token: str | None = Query(None), cmc_session: str | None = Cookie(None)):
        """Guarda % de honorario y/o teléfono editados en el popup → equipo_cmc."""
        _auth(token, cmc_session)
        body = await request.json()
        pid = body.get("id")
        if not pid:
            raise HTTPException(400, "falta id")
        from session import db
        with db() as c:
            sets, vals = [], []
            if "pct" in body and body["pct"] is not None:
                sets.append("pct_honorario=?"); vals.append(int(body["pct"]))
            if "telefono" in body:
                sets.append("telefono=?"); vals.append(re.sub(r"\D", "", str(body["telefono"] or "")))
            if not sets:
                return {"ok": True}
            vals.append(pid)
            try:
                c.execute(f"UPDATE equipo_cmc SET {', '.join(sets)}, updated_at=datetime('now') WHERE id_medilink=?", vals)
            except Exception as ex:
                raise HTTPException(500, f"no se pudo guardar: {ex}")
        return {"ok": True}

    @app.post("/api/panel-dia/remuneracion-pagado", tags=["panel-dia"], include_in_schema=False)
    async def remuneracion_pagado(request: Request,
                                  token: str | None = Query(None), cmc_session: str | None = Cookie(None)):
        """Marca/desmarca un profesional como pagado en un mes."""
        _auth(token, cmc_session)
        body = await request.json()
        pid = body.get("id")
        mes = body.get("mes")
        pagado = 1 if body.get("pagado") else 0
        if not pid or not mes:
            raise HTTPException(400, "falta id/mes")
        from session import db
        with db() as c:
            _ensure_estado(c)
            c.execute(
                "INSERT INTO remuneracion_estado (id_profesional, mes, pagado, pagado_at) "
                "VALUES (?,?,?, datetime('now')) "
                "ON CONFLICT(id_profesional, mes) DO UPDATE SET pagado=excluded.pagado, pagado_at=excluded.pagado_at",
                (pid, mes, pagado))
        return {"ok": True, "pagado": bool(pagado)}
