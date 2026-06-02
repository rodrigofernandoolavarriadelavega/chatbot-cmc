"""
Router /alma/api/pacientes — Ficha 360 del paciente.

Vista centrada en el paciente que UNIFICA lo que hoy está disperso: perfil
(contact_profiles), pagos (pagos_cmc), citas del bot (citas_bot), actividad de
conversación (messages) y etiquetas (contact_tags). No duplica Medilink: es la
capa de lectura transversal sobre los datos que ya tenemos en sessions.db.

Auth: alma_common.require_admin.
"""
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request, Query, Cookie

from alma_common import require_admin

log = logging.getLogger("pacientes_routes")
_CHILE_TZ = ZoneInfo("America/Santiago")
router = APIRouter(prefix="/alma/api/pacientes", tags=["pacientes"])


def _digits(s: str) -> str:
    return "".join(c for c in (s or "") if c.isdigit())


@router.get("/resumen")
async def resumen(token: str | None = Query(None), cmc_session: str | None = Cookie(None), request: Request = None):
    require_admin(request, token=token, cmc_session=cmc_session)
    from session import _conn
    now = datetime.now(_CHILE_TZ)
    mes = now.strftime("%Y-%m")
    with _conn() as conn:
        total = conn.execute("SELECT COUNT(*) c FROM contact_profiles WHERE nombre IS NOT NULL AND nombre != ''").fetchone()["c"]
        con_rut = conn.execute("SELECT COUNT(*) c FROM contact_profiles WHERE rut IS NOT NULL AND rut != ''").fetchone()["c"]
        nuevos = conn.execute("SELECT COUNT(*) c FROM contact_profiles WHERE substr(updated_at,1,7)=?", (mes,)).fetchone()["c"]
        # pacientes que pagaron este mes (distinct rut en pagos)
        try:
            activos = conn.execute("SELECT COUNT(DISTINCT rut) c FROM pagos_cmc WHERE substr(fecha,1,7)=? AND rut!=''", (mes,)).fetchone()["c"]
        except Exception:
            activos = 0
    return {"total": total, "con_rut": con_rut, "nuevos_mes": nuevos, "activos_mes": activos}


@router.get("/buscar")
async def buscar(q: str = Query("", description="nombre, RUT o teléfono"),
                 limit: int = Query(40),
                 token: str | None = Query(None), cmc_session: str | None = Cookie(None), request: Request = None):
    require_admin(request, token=token, cmc_session=cmc_session)
    from session import _conn
    like = f"%{q.lower()}%"
    with _conn() as conn:
        if q.strip():
            rows = conn.execute(
                """SELECT phone, rut, nombre, updated_at FROM contact_profiles
                   WHERE (LOWER(nombre) LIKE ? OR rut LIKE ? OR phone LIKE ?)
                     AND nombre IS NOT NULL AND nombre!=''
                   ORDER BY updated_at DESC LIMIT ?""",
                (like, f"%{q}%", f"%{_digits(q)}%", limit)
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT phone, rut, nombre, updated_at FROM contact_profiles
                   WHERE nombre IS NOT NULL AND nombre!='' ORDER BY updated_at DESC LIMIT ?""",
                (limit,)
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            rut = d.get("rut") or ""
            # enriquecer con resumen de pagos + última cita
            try:
                pg = conn.execute(
                    "SELECT COUNT(*) n, COALESCE(SUM(copago),0) total, MAX(fecha) ult FROM pagos_cmc WHERE rut=? AND rut!=''",
                    (rut,)
                ).fetchone()
                d["n_pagos"] = pg["n"]; d["total_pagado"] = pg["total"]; d["ultimo_pago"] = pg["ult"]
            except Exception:
                d["n_pagos"] = 0; d["total_pagado"] = 0; d["ultimo_pago"] = None
            try:
                ct = conn.execute("SELECT especialidad, fecha FROM citas_bot WHERE phone=? ORDER BY fecha DESC LIMIT 1", (d["phone"],)).fetchone()
                d["ultima_cita_esp"] = ct["especialidad"] if ct else None
                d["ultima_cita_fecha"] = ct["fecha"] if ct else None
            except Exception:
                d["ultima_cita_esp"] = None; d["ultima_cita_fecha"] = None
            out.append(d)
    return {"pacientes": out, "total": len(out)}


@router.get("/ficha")
async def ficha(phone: str = Query(...),
                token: str | None = Query(None), cmc_session: str | None = Cookie(None), request: Request = None):
    require_admin(request, token=token, cmc_session=cmc_session)
    from session import _conn
    with _conn() as conn:
        prof = conn.execute("SELECT phone, rut, nombre, updated_at FROM contact_profiles WHERE phone=?", (phone,)).fetchone()
        if not prof:
            raise HTTPException(404, "Paciente no encontrado")
        prof = dict(prof)
        rut = prof.get("rut") or ""
        # fecha_nacimiento si existe la columna
        try:
            fn = conn.execute("SELECT fecha_nacimiento FROM contact_profiles WHERE phone=?", (phone,)).fetchone()
            prof["fecha_nacimiento"] = fn["fecha_nacimiento"] if fn else None
        except Exception:
            prof["fecha_nacimiento"] = None
        # tags
        try:
            tags = [r["tag"] for r in conn.execute("SELECT tag FROM contact_tags WHERE phone=? ORDER BY ts DESC", (phone,)).fetchall()]
        except Exception:
            tags = []
        # citas del bot
        try:
            citas = [dict(r) for r in conn.execute(
                "SELECT especialidad, profesional, fecha, hora, modalidad FROM citas_bot WHERE phone=? ORDER BY fecha DESC, hora DESC LIMIT 20", (phone,)
            ).fetchall()]
        except Exception:
            citas = []
        # pagos
        try:
            pagos = [dict(r) for r in conn.execute(
                "SELECT fecha, area, profesional, copago, metodo_pago, prevision FROM pagos_cmc WHERE rut=? AND rut!='' ORDER BY fecha DESC LIMIT 30", (rut,)
            ).fetchall()]
            total_pagado = sum(p["copago"] or 0 for p in pagos)
        except Exception:
            pagos = []; total_pagado = 0
        # actividad de mensajes
        try:
            mst = conn.execute("SELECT COUNT(*) n, MAX(ts) ult FROM messages WHERE phone=?", (phone,)).fetchone()
            msg_n = mst["n"]; msg_ult = mst["ult"]
        except Exception:
            msg_n = 0; msg_ult = None
        # interconsultas y exámenes del paciente (por rut exacto o nombre) — fail-safe
        nom = prof.get("nombre") or ""
        try:
            ics = [dict(r) for r in conn.execute(
                "SELECT fecha, origen_especialidad, destino_especialidad, prioridad, estado "
                "FROM interconsultas WHERE (rut!='' AND rut=?) OR paciente_nombre=? "
                "ORDER BY fecha DESC LIMIT 10", (rut, nom)).fetchall()]
        except Exception:
            ics = []
        try:
            exs = [dict(r) for r in conn.execute(
                "SELECT fecha_solicitud, tipo, examen, estado FROM examenes_cmc "
                "WHERE (rut!='' AND rut=?) OR paciente_nombre=? "
                "ORDER BY fecha_solicitud DESC LIMIT 10", (rut, nom)).fetchall()]
        except Exception:
            exs = []
    return {
        "perfil": prof, "tags": tags, "citas": citas, "pagos": pagos,
        "total_pagado": total_pagado, "n_pagos": len(pagos),
        "n_mensajes": msg_n, "ultimo_mensaje": msg_ult,
        "interconsultas": ics, "examenes": exs,
    }
