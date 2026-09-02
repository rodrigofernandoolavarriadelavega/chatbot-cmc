"""
Router /alma/api/ortodoncia — Módulo Seguimiento Ortodoncia en Alma.

Refuerza una especialidad de ticket alto pero frágil: ortodoncia en el CMC es UNA
sola profesional (Dra. Daniela Castillo, vive en Concepción), con tratamientos
largos de 18-24 meses y controles mensuales. El riesgo del negocio es doble:
  1. Un paciente que deja de venir a sus controles abandona un tratamiento ya
     pagado en parte → mala terminación clínica + saldo que no se cobra.
  2. Sin un seguimiento explícito, los controles vencidos pasan desapercibidos
     porque "vienen solos cuando quieren".

Qué hace este módulo:
  - Lista los tratamientos activos con su avance (meses en tratamiento, nº de
    controles) desde el historial real (bi.fact_atenciones, especialidad_id=19).
  - Marca quién tiene su control VENCIDO (más de 45 días sin venir) → worklist
    accionable con deep-link wa.me para citarlo.
  - Plan de pago por paciente (valor total del tratamiento, cuota mensual,
    abonado) → saldo pendiente y valor de la cartera.

Fuentes de datos:
  - bi.fact_atenciones + dim_paciente + dim_profesional + fact_ingresos  (historial
    de controles, teléfono, monto por visita).
  - sessions.db tabla ortodoncia_plan  (plan de pago + estado, llaveado por
    bi.paciente_id). Distinto de la tabla legacy `ortodoncia_cache` (esa viene del
    módulo admin clásico, llaveada por id de Medilink).

Degradación elegante: si la BI no está disponible → listas vacías +
source_status="bi_unavailable" (nunca 500).
"""
import csv
import io
import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Cookie, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

log = logging.getLogger("ortodoncia_routes")
_CHILE_TZ = ZoneInfo("America/Santiago")

router = APIRouter(prefix="/alma/api/ortodoncia", tags=["ortodoncia"])

# Fuente de datos = CAJA REAL (bi_pagos_caja, sync nocturno del bot), NO la BI vieja.
# Ver memory/cmc_ventas_fuente_fiel: para datos operativos/actuales SIEMPRE la caja.
ORTO_PROF_IDS = (66,)   # Dra. Daniela Castillo — único ortodoncista (id Medilink)

# ── Umbrales (control mensual ≈ 30 días) ─────────────────────────────────────
CONTROL_OK_MAX     = 35   # ≤ 35d desde el último control → al día
CONTROL_PRONTO_MAX = 45   # 36-45d → toca/está por vencer
                          # > 45d → control VENCIDO
GAP_NUEVO_TRAT     = 150  # hueco > 150d → tratamiento nuevo (no es el mismo)

# Clasificación de la visita por monto (convención CMC: instalación cara, control barato)
MONTO_INSTALACION  = 80000   # >= → instalación / fase mayor
MONTO_CONTROL_MIN  = 12000   # entre min y instalación → control


# ── Auth ──────────────────────────────────────────────────────────────────────

def _require_admin(request: Request, token: str | None, cmc_session: str | None) -> str:
    import hmac as _hmac
    from config import ADMIN_TOKEN
    from admin_routes import _verify_cookie, _is_admin_token

    auth_header = (request.headers.get("authorization", "") if request else "")
    if auth_header.lower().startswith("bearer "):
        tk = auth_header.split(None, 1)[1].strip()
        if _is_admin_token(tk):
            return tk
    if cmc_session:
        role = _verify_cookie(cmc_session)
        if role in ("admin", "ortodoncia"):
            return ADMIN_TOKEN
    if token and _is_admin_token(token):
        return token
    # El token de ortodoncia entra a SU propio modulo. Se amplia aca y no en el
    # factory generico de paginas Alma, que abriria los 43 modulos del registry
    # (EBITDA incluido) a un token que circula por WhatsApp.
    from config import ORTODONCIA_TOKEN
    if token and ORTODONCIA_TOKEN and token == ORTODONCIA_TOKEN:
        return token
    raise HTTPException(status_code=401, detail="Token inválido")


# ── Capa de gestión propia (sessions.db) ────────────────────────────────────

def ensure_ortodoncia_plan_table() -> None:
    """Plan de pago por paciente (llaveado por bi.paciente_id). Idempotente."""
    from session import db as _conn
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ortodoncia_plan (
                paciente_id    INTEGER PRIMARY KEY,   -- bi.dim_paciente.paciente_id
                valor_total    INTEGER DEFAULT 0,      -- valor total del tratamiento
                cuota_mensual  INTEGER DEFAULT 0,
                abonado        INTEGER DEFAULT 0,      -- pagado a la fecha
                estado_manual  TEXT DEFAULT '',        -- '' | 'finalizado' | 'pausa'
                notas          TEXT DEFAULT '',
                updated_at     TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.commit()


def _planes() -> dict[int, dict]:
    ensure_ortodoncia_plan_table()
    from session import db as _conn
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM ortodoncia_plan").fetchall()
    return {r["paciente_id"]: dict(r) for r in rows}


# ── Fuente de datos: CAJA REAL (bi_pagos_caja) + identidad (dim_paciente) ─────
# La caja la llena el bot por cron nocturno (no depende de Docker/BI). Cada pago de
# ortodoncia = una visita; instalación vs control se clasifica por monto en _compute.
# Identidad (nombre/teléfono/localidad) viene de dim_paciente del BI (cambia lento);
# si el BI no responde, el nombre cae a citas_cache (local) y nunca se rompe.

def _meses_atras(meses: int) -> str:
    """Primer día del mes 'meses' atrás, formato YYYY-MM-DD (para filtrar la caja)."""
    t = _today()
    y, m = t.year, t.month - meses
    while m <= 0:
        m += 12
        y -= 1
    return f"{y:04d}-{m:02d}-01"


def _identidad_bi(pids: list[int]) -> dict[int, dict]:
    """Nombre/teléfono/localidad por id_paciente desde bi.dim_paciente (best-effort).
    Devuelve {} si el BI no está disponible — el caller cae a nombres locales."""
    if not pids:
        return {}
    try:
        from main import _bi_pool
        pool = _bi_pool()
        conn = None
        try:
            conn = pool.getconn()
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT paciente_id, "
                    "TRIM(COALESCE(nombre,'') || ' ' || COALESCE(apellido,'')), "
                    "telefono, COALESCE(NULLIF(TRIM(localidad),''), comuna, '') "
                    "FROM bi.dim_paciente WHERE paciente_id = ANY(%s)",
                    (pids,),
                )
                return {
                    r[0]: {"paciente": r[1], "telefono": r[2] or "", "lugar": r[3] or ""}
                    for r in cur.fetchall()
                }
        finally:
            if conn is not None:
                pool.putconn(conn)
    except Exception as e:
        log.warning("ortodoncia: identidad BI no disponible, uso cache local (%s)", e)
        return {}


def _bi_rows(meses: int) -> tuple[list[dict], str]:
    """Filas de visitas de ortodoncia desde la CAJA REAL (fresca), con identidad enriquecida.
    Mantiene el shape que espera _compute: {paciente_id, paciente, telefono, lugar, fecha, monto}."""
    from session import db as _conn
    desde = _meses_atras(meses)
    ph = ",".join("?" * len(ORTO_PROF_IDS))
    try:
        with _conn() as c:
            pagos = c.execute(
                f"SELECT id_paciente, fecha, monto FROM bi_pagos_caja "
                f"WHERE id_profesional IN ({ph}) AND fecha >= ? AND id_paciente IS NOT NULL "
                f"ORDER BY id_paciente, fecha",
                (*ORTO_PROF_IDS, desde),
            ).fetchall()
            nombres_local = {
                row[0]: row[1]
                for row in c.execute(
                    "SELECT id_paciente, paciente_nombre FROM citas_cache"
                ).fetchall()
            }
    except Exception as e:
        log.warning("ortodoncia: caja (bi_pagos_caja) no disponible (%s)", e)
        return [], "caja_unavailable"

    if not pagos:
        return [], "ok"   # caja vacía no es error: simplemente no hay pagos en la ventana

    pids = sorted({row[0] for row in pagos})
    ident = _identidad_bi(pids)

    rows = []
    for id_pac, fecha, monto in pagos:
        info = ident.get(id_pac) or {}
        rows.append({
            "paciente_id": id_pac,
            "paciente": info.get("paciente") or nombres_local.get(id_pac) or "",
            "telefono": info.get("telefono") or "",
            "lugar": info.get("lugar") or "",
            "fecha": fecha,
            "monto": float(monto or 0),
        })
    return rows, "ok"


def _today() -> date:
    return datetime.now(_CHILE_TZ).date()


def _clasificar(dias: int, plan: dict | None) -> tuple[str, str]:
    if plan and plan.get("estado_manual") == "finalizado":
        return "finalizado", "Finalizado"
    if dias <= CONTROL_OK_MAX:
        return "al_dia", "Al día"
    if dias <= CONTROL_PRONTO_MAX:
        return "pronto", "Control por vencer"
    return "vencido", "Control vencido"


def _compute(meses: int = 6):
    rows, status = _bi_rows(max(meses, 24))  # ventana ancha: ortodoncia dura 18-24m
    planes = _planes()
    today = _today()

    porpac: dict[int, dict] = {}
    for r in rows:
        f = r["fecha"]
        if isinstance(f, str):
            try:
                f = datetime.strptime(f[:10], "%Y-%m-%d").date()
            except Exception:
                continue
        pid = r["paciente_id"]
        d = porpac.setdefault(pid, {
            "paciente_id": pid, "paciente": r["paciente"], "telefono": r.get("telefono") or "",
            "lugar": r.get("lugar") or "", "visitas": [],
        })
        d["visitas"].append((f, float(r.get("monto") or 0)))

    pacientes = []
    valor_cartera = 0
    ingreso_total = 0.0
    ventana_6m = today.toordinal() - meses * 30

    for pid, d in porpac.items():
        visitas = sorted(d["visitas"], key=lambda x: x[0])
        if not visitas:
            continue
        fechas = [v[0] for v in visitas]
        # tratamiento actual = visitas desde el último hueco grande
        inicio_idx = 0
        for i in range(1, len(fechas)):
            if (fechas[i] - fechas[i - 1]).days > GAP_NUEVO_TRAT:
                inicio_idx = i
        tramo = visitas[inicio_idx:]
        inicio = tramo[0][0]
        last = tramo[-1][0]
        dias = (today - last).days
        n_visitas = len(tramo)
        n_instalacion = sum(1 for _, m in tramo if m >= MONTO_INSTALACION)
        n_controles = sum(1 for _, m in tramo if MONTO_CONTROL_MIN <= m < MONTO_INSTALACION)
        meses_trat = round((today - inicio).days / 30.0, 1)
        ingreso_pac = sum(m for f, m in visitas if f.toordinal() >= ventana_6m)
        ingreso_total += ingreso_pac

        plan = planes.get(pid)
        estado, etiqueta = _clasificar(dias, plan)
        valor_total = (plan or {}).get("valor_total", 0) or 0
        abonado = (plan or {}).get("abonado", 0) or 0
        cuota = (plan or {}).get("cuota_mensual", 0) or 0
        saldo = max(valor_total - abonado, 0)
        if estado != "finalizado":
            valor_cartera += saldo

        pacientes.append({
            "paciente_id": pid,
            "paciente": d["paciente"] or f"Paciente {pid}",
            "telefono": d["telefono"],
            "lugar": d["lugar"],
            "inicio": inicio.isoformat(),
            "meses_tratamiento": meses_trat,
            "n_visitas": n_visitas,
            "n_controles": n_controles,
            "n_instalacion": n_instalacion,
            "ultimo_control": last.isoformat(),
            "dias_sin_control": dias,
            "estado": estado,
            "estado_label": etiqueta,
            "valor_total": valor_total,
            "abonado": abonado,
            "cuota_mensual": cuota,
            "saldo": saldo,
            "notas": (plan or {}).get("notas", ""),
        })

    # Orden: último control más reciente arriba → más antiguo abajo (dias_sin_control asc).
    pacientes.sort(key=lambda p: p["dias_sin_control"])

    activos     = [p for p in pacientes if p["estado"] != "finalizado"]
    vencidos    = [p for p in pacientes if p["estado"] == "vencido"]
    pronto      = [p for p in pacientes if p["estado"] == "pronto"]

    kpis = {
        "activos": len(activos),
        "vencidos": len(vencidos),
        "pronto": len(pronto),
        "valor_cartera": valor_cartera,
        "ingreso_orto": round(ingreso_total),
        "n_pacientes": len(pacientes),
    }
    return {"kpis": kpis, "pacientes": pacientes, "source_status": status}


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/resumen")
async def resumen(meses: int = Query(6, ge=1, le=36),
                  token: str | None = Query(None),
                  cmc_session: str | None = Cookie(None),
                  request: Request = None):
    _require_admin(request, token, cmc_session)
    data = _compute(meses)
    return {"kpis": data["kpis"], "source_status": data["source_status"]}


@router.get("/pacientes")
async def pacientes(estado: str | None = Query(None),
                    meses: int = Query(6, ge=1, le=36),
                    token: str | None = Query(None),
                    cmc_session: str | None = Cookie(None),
                    request: Request = None):
    _require_admin(request, token, cmc_session)
    data = _compute(meses)
    pac = data["pacientes"]
    if estado and estado != "todos":
        pac = [p for p in pac if p["estado"] == estado]
    conteos: dict[str, int] = {}
    for p in data["pacientes"]:
        conteos[p["estado"]] = conteos.get(p["estado"], 0) + 1
    return {"pacientes": pac, "conteos": conteos, "total": len(pac),
            "source_status": data["source_status"]}


def _tipo_visita(monto: float) -> str:
    if monto >= MONTO_INSTALACION:
        return "instalacion"
    if monto >= MONTO_CONTROL_MIN:
        return "control"
    return "otro"


def _calendario(meses: int = 3):
    """Agrupa las atenciones de la Dra. Castillo por día (caja real, prof 66).
    - Dedup: un paciente cuenta una vez por día (varias líneas el mismo día se suman).
    - 'Nuevo' = ese día arrancó un tratamiento (1ra visita o tras un hueco > GAP_NUEVO_TRAT);
      'en curso' = visita de seguimiento. Se clasifica con historial ANCHO (24m) para no
      marcar como nuevo a quien empezó antes de la ventana visible."""
    hist, status = _bi_rows(max(meses, 24))  # ventana ancha solo para clasificar
    corte = (_today() - timedelta(days=meses * 31)).isoformat()

    # 1) visitas por paciente, ordenadas, para detectar inicios de tratamiento
    porpac: dict[int, list] = {}
    for r in hist:
        f = (r.get("fecha") or "")[:10]
        if not f:
            continue
        porpac.setdefault(r["paciente_id"], []).append((f, float(r.get("monto") or 0), r))
    es_inicio: dict[tuple, bool] = {}  # (pid, fecha) -> bool
    for pid, visitas in porpac.items():
        visitas.sort(key=lambda x: x[0])
        prev = None
        for f, _m, _r in visitas:
            d = datetime.strptime(f, "%Y-%m-%d").date()
            inicio = prev is None or (d - prev).days > GAP_NUEVO_TRAT
            es_inicio[(pid, f)] = es_inicio.get((pid, f)) or inicio
            prev = d

    # 2) agrupar por día (solo días dentro de la ventana pedida)
    pordia: dict[str, dict] = {}
    for r in hist:
        f = (r.get("fecha") or "")[:10]
        if not f or f < corte:
            continue
        pid = r["paciente_id"]
        dia = pordia.setdefault(f, {"fecha": f, "pacientes": {}})
        p = dia["pacientes"].setdefault(pid, {
            "paciente_id": pid,
            "paciente": r.get("paciente") or f"Paciente {pid}",
            "telefono": r.get("telefono") or "",
            "monto": 0.0,
            "nuevo": es_inicio.get((pid, f), False),
        })
        p["monto"] += float(r.get("monto") or 0)

    dias = []
    for f, dia in pordia.items():
        pacs = sorted(dia["pacientes"].values(), key=lambda x: (not x["nuevo"], -x["monto"]))
        for p in pacs:
            p["tipo"] = _tipo_visita(p["monto"])
        n_nuevos = sum(1 for p in pacs if p["nuevo"])
        dias.append({
            "fecha": f,
            "n_pacientes": len(pacs),
            "n_nuevos": n_nuevos,
            "n_encurso": len(pacs) - n_nuevos,
            "n_instalaciones": sum(1 for p in pacs if p["tipo"] == "instalacion"),
            "total": round(sum(p["monto"] for p in pacs)),
            "pacientes": pacs,
        })
    dias.sort(key=lambda d: d["fecha"], reverse=True)
    return {"dias": dias, "source_status": status}


@router.get("/calendario")
async def calendario(meses: int = Query(3, ge=1, le=24),
                     token: str | None = Query(None),
                     cmc_session: str | None = Cookie(None),
                     request: Request = None):
    _require_admin(request, token, cmc_session)
    return _calendario(meses)


@router.put("/plan/{paciente_id}")
async def set_plan(paciente_id: int, request: Request,
                   token: str | None = Query(None),
                   cmc_session: str | None = Cookie(None)):
    _require_admin(request, token, cmc_session)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Body JSON inválido")
    valor_total = int(body.get("valor_total") or 0)
    cuota_mensual = int(body.get("cuota_mensual") or 0)
    abonado = int(body.get("abonado") or 0)
    estado_manual = (body.get("estado_manual") or "").strip()
    if estado_manual not in ("", "finalizado", "pausa"):
        estado_manual = ""
    notas = (body.get("notas") or "").strip()
    ensure_ortodoncia_plan_table()
    from session import db as _conn
    with _conn() as conn:
        conn.execute("""
            INSERT INTO ortodoncia_plan (paciente_id, valor_total, cuota_mensual, abonado, estado_manual, notas, updated_at)
            VALUES (?,?,?,?,?,?, datetime('now'))
            ON CONFLICT(paciente_id) DO UPDATE SET
                valor_total=excluded.valor_total,
                cuota_mensual=excluded.cuota_mensual,
                abonado=excluded.abonado,
                estado_manual=excluded.estado_manual,
                notas=excluded.notas,
                updated_at=excluded.updated_at
        """, (paciente_id, valor_total, cuota_mensual, abonado, estado_manual, notas))
        conn.commit()
    return {"ok": True}


@router.get("/export")
async def export_csv(meses: int = Query(6, ge=1, le=36),
                     token: str | None = Query(None),
                     cmc_session: str | None = Cookie(None),
                     request: Request = None):
    _require_admin(request, token, cmc_session)
    data = _compute(meses)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Paciente", "Teléfono", "Lugar", "Inicio", "Meses", "Controles",
                "Último control", "Días sin control", "Estado",
                "Valor total", "Abonado", "Saldo"])
    for p in data["pacientes"]:
        w.writerow([p["paciente"], p["telefono"], p["lugar"], p["inicio"],
                    p["meses_tratamiento"], p["n_controles"], p["ultimo_control"],
                    p["dias_sin_control"], p["estado_label"],
                    p["valor_total"], p["abonado"], p["saldo"]])
    buf.seek(0)
    fname = f"ortodoncia_{_today().isoformat()}.csv"
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{fname}"'})
