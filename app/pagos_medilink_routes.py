"""
Router /alma/api/pagos-medilink — Pagos según la CAJA REAL de Medilink.

Espejo de SOLO LECTURA del módulo Pagos, pero alimentado por la MISMA fuente que
DB Mensual: la tabla `bi_pagos_caja` (CSV oficial Medilink /pagos, sync nocturno).
Mientras `pagos_cmc` (módulo Pagos) son los copagos que la recepción registra a
mano, esto es lo que Medilink dice que se cobró — la fuente de verdad financiera.

No es editable (refleja Medilink). Auth con scope vía alma_scope:
  • dueño / recepción  → ve todo.
  • perfil de profesional (ej. Gisela) → filtrado a su id_profesional.

Enriquecimiento "hechos frescos + identidad estable" (igual que caja_helper):
  • monto / método / folio / fecha  → bi_pagos_caja.
  • nombre del profesional + área    → medilink.PROFESIONALES (local).
  • nombre del paciente              → bi.dim_paciente, fallback citas_cache.
  • hora                             → citas_cache por (id_paciente, fecha), best-effort.
"""
import csv
import io
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Cookie, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

log = logging.getLogger("pagos_medilink")
_TZ = ZoneInfo("America/Santiago")

router = APIRouter(prefix="/alma/api/pagos-medilink", tags=["pagos-medilink"])


def _resolve(request, token, cmc_session) -> tuple[str, int | None]:
    """(token_efectivo, scope_prof). scope None = dueño/recepción (ve todo);
    int = perfil de profesional → filtrado a su id_profesional."""
    from alma_scope import resolve
    return resolve(request, token, cmc_session, "pagos_medilink")


def _rango(fecha, fecha_desde, fecha_hasta) -> tuple[str, str]:
    hoy = datetime.now(_TZ).strftime("%Y-%m-%d")
    if fecha_desde and fecha_hasta:
        for d in (fecha_desde, fecha_hasta):
            try:
                datetime.strptime(d, "%Y-%m-%d")
            except ValueError:
                raise HTTPException(400, "Fechas deben ser YYYY-MM-DD")
        return fecha_desde, fecha_hasta
    d = fecha or hoy
    try:
        datetime.strptime(d, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(400, "fecha debe ser YYYY-MM-DD")
    return d, d


def _fetch(d_desde: str, d_hasta: str, scope: int | None) -> list[dict]:
    """Pagos de la caja real en el rango, enriquecidos. Filtra por profesional si scope.

    ATRIBUCIÓN PRECISA: cada pago se liga a la atención del MISMO DÍA cuyo total
    coincide EXACTO con el monto (eso ata el pago a SU atención específica, aunque el
    paciente vea a dos profesionales ese día → no sobre-incluye). Si no hay esa
    coincidencia (típico en copagos Fonasa, donde lo pagado ≠ total bruto), cae al
    id_profesional heurístico de bi_pagos_caja (la misma atribución que DB Mensual).
    Esos casos residuales se corrigen con override manual aguas arriba."""
    from session import _conn
    sub_match_prof = ("(SELECT a.id_profesional FROM bi_atenciones a "
                      "WHERE a.id_paciente=p.id_paciente AND a.fecha=p.fecha AND a.total=p.monto "
                      "ORDER BY a.atencion_id LIMIT 1)")
    sub_nom = ("(SELECT a.paciente_nombre FROM bi_atenciones a "
               "WHERE a.id_paciente=p.id_paciente AND a.fecha=p.fecha "
               "ORDER BY a.atencion_id LIMIT 1)")
    where = "p.fecha >= ? AND p.fecha <= ?"
    params: list = [d_desde, d_hasta]
    if scope is not None:
        where += (" AND (EXISTS (SELECT 1 FROM bi_atenciones a WHERE a.id_paciente=p.id_paciente "
                  "AND a.fecha=p.fecha AND a.total=p.monto AND a.id_profesional=?) "
                  "OR (NOT EXISTS (SELECT 1 FROM bi_atenciones a WHERE a.id_paciente=p.id_paciente "
                  "AND a.fecha=p.fecha AND a.total=p.monto) AND p.id_profesional=?))")
        params += [scope, scope]
    try:
        with _conn() as c:
            rows = c.execute(
                f"""SELECT p.pago_id, p.fecha, p.monto, p.metodo_pago, p.n_folio,
                          COALESCE({sub_match_prof}, p.id_profesional) AS id_prof,
                          p.id_paciente AS id_pac,
                          {sub_nom} AS nombre_aten
                   FROM bi_pagos_caja p
                   WHERE {where}
                   ORDER BY p.fecha DESC, p.pago_id DESC""",
                params,
            ).fetchall()
            nombres_local = {
                r[0]: r[1] for r in
                c.execute("SELECT id_paciente, paciente_nombre FROM citas_cache").fetchall()
            }
            horas_local = {
                (r[0], r[1]): r[2] for r in
                c.execute("SELECT id_paciente, fecha, hora_inicio FROM citas_cache").fetchall()
            }
    except Exception as e:  # noqa: BLE001
        log.warning("bi_pagos_caja no disponible (%s)", e)
        return []

    try:
        from medilink import PROFESIONALES
    except Exception:
        PROFESIONALES = {}

    out = []
    for r in rows:
        pid = r["id_pac"]
        prof = PROFESIONALES.get(r["id_prof"], {}) if isinstance(PROFESIONALES, dict) else {}
        out.append({
            "id": r["pago_id"],
            "fecha": r["fecha"],
            "hora": horas_local.get((pid, r["fecha"]), "") or "",
            "id_profesional": r["id_prof"],
            "profesional": prof.get("nombre", "") or "—",
            "area": (prof.get("especialidad", "") or "").split(" / ")[0],
            "paciente_nombre": r["nombre_aten"] or nombres_local.get(pid) or "—",
            "monto": int(r["monto"] or 0),
            "metodo_pago": r["metodo_pago"] or "",
            "folio": r["n_folio"] or "",
        })
    return out


@router.get("")
async def listar(fecha: str | None = Query(None),
                 fecha_desde: str | None = Query(None),
                 fecha_hasta: str | None = Query(None),
                 token: str | None = Query(None),
                 cmc_session: str | None = Cookie(None),
                 request: Request = None):
    _tk, scope = _resolve(request, token, cmc_session)
    d_desde, d_hasta = _rango(fecha, fecha_desde, fecha_hasta)
    pagos = _fetch(d_desde, d_hasta, scope)
    return {
        "pagos": pagos,
        "total": sum(p["monto"] for p in pagos),
        "n": len(pagos),
        "desde": d_desde, "hasta": d_hasta,
    }


@router.get("/export")
async def export(fecha: str | None = Query(None),
                 fecha_desde: str | None = Query(None),
                 fecha_hasta: str | None = Query(None),
                 token: str | None = Query(None),
                 cmc_session: str | None = Cookie(None),
                 request: Request = None):
    _tk, scope = _resolve(request, token, cmc_session)
    d_desde, d_hasta = _rango(fecha, fecha_desde, fecha_hasta)
    pagos = _fetch(d_desde, d_hasta, scope)
    buf = io.StringIO()
    buf.write("﻿")  # BOM → Excel UTF-8
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Fecha", "Hora", "Paciente", "Profesional", "Area", "Monto", "Metodo", "Folio"])
    for p in pagos:
        w.writerow([p["fecha"], p["hora"], p["paciente_nombre"], p["profesional"],
                    p["area"], p["monto"], p["metodo_pago"], p["folio"]])
    buf.seek(0)
    fname = f"pagos_medilink_{d_desde}_a_{d_hasta}.csv" if d_desde != d_hasta else f"pagos_medilink_{d_desde}.csv"
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="{fname}"'})
