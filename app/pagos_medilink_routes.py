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


def _norm_nombre(s: str) -> str:
    """Normaliza un nombre para cruzar entre tablas (sin tildes, minúsculas, 1 espacio)."""
    import unicodedata
    s = (s or "").strip().lower()
    s = "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")
    return " ".join(s.split())


def _fetch(d_desde: str, d_hasta: str, scope: int | None) -> list[dict]:
    """Pagos de la caja real en el rango, con ATRIBUCIÓN POR CAPAS del profesional.

    Capa 0 — override manual (bi_pago_overrides): si el dueño reasignó el pago, gana.
    Capa 1 — heurístico (bi_pagos_caja.id_profesional): la misma atribución que DB
             Mensual; base razonable, se equivoca solo en casos ambiguos.
    Capa 2 — REPASADA: corrige la capa 1 cruzando contra el módulo Pagos (pagos_cmc,
             registrado a mano por recepción CON el profesional correcto). Se cruza
             por (fecha + nombre normalizado); si recepción atendió a esa persona ese
             día con un ÚNICO profesional, ese profesional manda. Si vio a dos, queda
             ambiguo → se respeta una atención del día con monto exacto, o el heurístico.

    El filtro por profesional (scope, p.ej. Gisela) usa la atribución YA corregida."""
    from session import _conn
    try:
        with _conn() as c:
            rows = c.execute(
                """SELECT p.pago_id, p.fecha, p.monto, p.metodo_pago, p.n_folio,
                          p.id_profesional AS heur_prof, p.id_paciente AS id_pac,
                          (SELECT a.id_profesional FROM bi_atenciones a
                             WHERE a.id_paciente=p.id_paciente AND a.fecha=p.fecha AND a.total=p.monto
                             ORDER BY a.atencion_id LIMIT 1) AS exact_prof,
                          (SELECT a.paciente_nombre FROM bi_atenciones a
                             WHERE a.id_paciente=p.id_paciente AND a.fecha=p.fecha
                             ORDER BY a.atencion_id LIMIT 1) AS nombre_aten,
                          (SELECT 1 FROM bi_pago_overrides o WHERE o.pago_id=p.pago_id) AS has_override
                   FROM bi_pagos_caja p
                   WHERE p.fecha >= ? AND p.fecha <= ?
                   ORDER BY p.fecha DESC, p.pago_id DESC""",
                (d_desde, d_hasta),
            ).fetchall()
            nombres_local = {
                r[0]: r[1] for r in
                c.execute("SELECT id_paciente, paciente_nombre FROM citas_cache").fetchall()
            }
            horas_local = {
                (r[0], r[1]): r[2] for r in
                c.execute("SELECT id_paciente, fecha, hora_inicio FROM citas_cache").fetchall()
            }
            # Repasada: (fecha, nombre normalizado) → set de profesionales que recepción
            # registró ese día para esa persona (fuente humana, verificada).
            cmc: dict = {}
            for fe, nom, idp in c.execute(
                "SELECT fecha, paciente_nombre, id_profesional FROM pagos_cmc "
                "WHERE id_profesional IS NOT NULL AND fecha >= ? AND fecha <= ?",
                (d_desde, d_hasta),
            ).fetchall():
                cmc.setdefault((fe, _norm_nombre(nom)), set()).add(idp)
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
        nombre = r["nombre_aten"] or nombres_local.get(pid) or ""
        # ── Atribución por capas ──
        if r["has_override"]:
            prof_id = r["heur_prof"]                       # capa 0: manual (gana)
        else:
            cmc_profs = cmc.get((r["fecha"], _norm_nombre(nombre)))
            if cmc_profs and len(cmc_profs) == 1:
                prof_id = next(iter(cmc_profs))            # capa 2: repasada (recepción, único prof del día)
            elif r["exact_prof"] is not None:
                prof_id = r["exact_prof"]                  # atención del día con monto exacto
            else:
                prof_id = r["heur_prof"]                   # capa 1: heurístico
        if scope is not None and prof_id != scope:
            continue
        prof = PROFESIONALES.get(prof_id, {}) if isinstance(PROFESIONALES, dict) else {}
        out.append({
            "id": r["pago_id"],
            "fecha": r["fecha"],
            "hora": horas_local.get((pid, r["fecha"]), "") or "",
            "id_profesional": prof_id,
            "profesional": prof.get("nombre", "") or "—",
            "area": (prof.get("especialidad", "") or "").split(" / ")[0],
            "paciente_nombre": nombre or "—",
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


@router.put("/reasignar/{pago_id}")
async def reasignar(pago_id: int, request: Request,
                    token: str | None = Query(None),
                    cmc_session: str | None = Cookie(None)):
    """Reasigna el profesional de un pago (override manual). SOLO dueño/recepción
    (un perfil de profesional → 403). Escribe bi_pago_overrides (NIVEL 0 del matcher,
    persiste ante re-syncs) y actualiza bi_pagos_caja → corrige este módulo Y DB Mensual."""
    _tk, scope = _resolve(request, token, cmc_session)
    if scope is not None:
        raise HTTPException(403, "Solo el dueño puede reasignar")
    try:
        id_prof = int((await request.json()).get("id_profesional"))
    except Exception:
        raise HTTPException(400, "id_profesional inválido")
    from medilink import PROFESIONALES
    if id_prof not in PROFESIONALES:
        raise HTTPException(400, "Profesional inexistente")
    from session import _conn
    with _conn() as c:
        row = c.execute("SELECT atencion_id FROM bi_pagos_caja WHERE pago_id=?", (pago_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Pago no existe")
        c.execute(
            "INSERT OR REPLACE INTO bi_pago_overrides "
            "(pago_id, id_profesional, atencion_id, reason, created_at) "
            "VALUES (?,?,?,?, datetime('now'))",
            (pago_id, id_prof, row["atencion_id"], "reasignación manual (Pagos Medilink)"),
        )
        c.execute("UPDATE bi_pagos_caja SET id_profesional=? WHERE pago_id=?", (id_prof, pago_id))
        c.commit()
    return {"ok": True, "id_profesional": id_prof,
            "profesional": PROFESIONALES[id_prof].get("nombre", "")}


def aplicar_repasada(d_desde: str, d_hasta: str) -> dict:
    """Persiste la REPASADA a la fuente: corrige bi_pagos_caja (y por ende DB Mensual)
    cruzando contra el módulo Pagos. CONSERVADOR: solo corrige cuando recepción registró
    a esa persona ese día con un ÚNICO profesional (caso claro) y difiere del actual;
    NUNCA pisa un override manual existente. Escribe override (auditable) + actualiza
    bi_pagos_caja. Idempotente (re-correr no duplica)."""
    from session import _conn
    corregidos, revisados = 0, 0
    with _conn() as c:
        cmc: dict = {}
        for fe, nom, idp in c.execute(
            "SELECT fecha, paciente_nombre, id_profesional FROM pagos_cmc "
            "WHERE id_profesional IS NOT NULL AND fecha >= ? AND fecha <= ?",
            (d_desde, d_hasta),
        ).fetchall():
            cmc.setdefault((fe, _norm_nombre(nom)), set()).add(idp)
        nombres_local = {
            r[0]: r[1] for r in
            c.execute("SELECT id_paciente, paciente_nombre FROM citas_cache").fetchall()
        }
        rows = c.execute(
            """SELECT p.pago_id, p.fecha, p.atencion_id, p.id_paciente, p.id_profesional,
                      (SELECT a.paciente_nombre FROM bi_atenciones a
                         WHERE a.id_paciente=p.id_paciente AND a.fecha=p.fecha
                         ORDER BY a.atencion_id LIMIT 1) AS nombre_aten
               FROM bi_pagos_caja p
               WHERE p.fecha >= ? AND p.fecha <= ?
                 AND NOT EXISTS (SELECT 1 FROM bi_pago_overrides o WHERE o.pago_id=p.pago_id)""",
            (d_desde, d_hasta),
        ).fetchall()
        for r in rows:
            revisados += 1
            nombre = r["nombre_aten"] or nombres_local.get(r["id_paciente"]) or ""
            profs = cmc.get((r["fecha"], _norm_nombre(nombre)))
            if profs and len(profs) == 1:
                correcto = next(iter(profs))
                if correcto != r["id_profesional"]:
                    c.execute(
                        "INSERT OR REPLACE INTO bi_pago_overrides "
                        "(pago_id, id_profesional, atencion_id, reason, created_at) "
                        "VALUES (?,?,?,?, datetime('now'))",
                        (r["pago_id"], correcto, r["atencion_id"], "repasada auto (cruce módulo Pagos)"),
                    )
                    c.execute("UPDATE bi_pagos_caja SET id_profesional=? WHERE pago_id=?",
                              (correcto, r["pago_id"]))
                    corregidos += 1
        c.commit()
    log.info("repasada %s..%s: revisados=%d corregidos=%d", d_desde, d_hasta, revisados, corregidos)
    return {"revisados": revisados, "corregidos": corregidos, "desde": d_desde, "hasta": d_hasta}


@router.post("/aplicar-repasada")
async def aplicar_repasada_endpoint(desde: str | None = Query(None),
                                    hasta: str | None = Query(None),
                                    token: str | None = Query(None),
                                    cmc_session: str | None = Cookie(None),
                                    request: Request = None):
    """Corre la repasada persistente sobre un rango (default: últimos 120 días). Solo dueño."""
    _tk, scope = _resolve(request, token, cmc_session)
    if scope is not None:
        raise HTTPException(403, "Solo el dueño")
    from datetime import timedelta
    hoy = datetime.now(_TZ).date()
    d_desde = desde or (hoy - timedelta(days=120)).isoformat()
    d_hasta = hasta or hoy.isoformat()
    return aplicar_repasada(d_desde, d_hasta)
