"""
Router /alma/api/kine — Módulo Programa Kinesiología (rehabilitación) en Alma.

Refuerza la especialidad más sólida y escalable del CMC: kinesiología tiene
3 profesionales activos y estructura MULTI-SESIÓN (un paciente viene 6-12 veces
por un plan de tratamiento). El mayor lever de negocio NO es captar más, es que
el paciente COMPLETE su plan: el que abandona en la sesión 4 deja ~40% del
ingreso y un peor resultado clínico. Hoy nadie vigila ese abandono a mitad de
tratamiento — es ingreso recuperable que se va silencioso.

Qué hace este módulo:
  - Detecta EPISODIOS de tratamiento desde el historial real (bi.fact_atenciones,
    especialidad_id=3) agrupando sesiones consecutivas; un hueco > 45 días
    abre un episodio nuevo.
  - Clasifica cada paciente por riesgo de abandono según los días desde su
    última sesión vs la cadencia esperada (~7 días en kine).
  - Worklist accionable: a quién hay que contactar HOY antes de que abandone,
    con su teléfono → deep-link wa.me.
  - Plan de tratamiento opcional por paciente (sesiones prescritas) → % de avance.

Fuentes de datos:
  - bi.fact_atenciones + dim_paciente + dim_profesional + fact_ingresos  (analítica
    completa: historial de sesiones, teléfono del paciente, monto por sesión).
  - sessions.db tabla kine_plan  (capa de gestión propia: plan de sesiones, alta,
    notas). Se llavea por bi.paciente_id para no cruzarse con el keyspace de
    Medilink que usa kine_tracking (ese viene del bot).

Degradación elegante: si la BI no está disponible, los endpoints devuelven
listas vacías + source_status="bi_unavailable" (nunca 500).

Auth: mismo patrón que inventario_routes / pagos_routes.
"""
import csv
import io
import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Cookie, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

log = logging.getLogger("kine_routes")
_CHILE_TZ = ZoneInfo("America/Santiago")

router = APIRouter(prefix="/alma/api/kine", tags=["kine"])

# Fuente = CAJA REAL (bi_pagos_caja, fresca, sin Docker), NO la BI vieja.
# Kinesiología = Luis Armijo (77) + Leonardo Etcheverry (21). Ver memory/cmc_ventas_fuente_fiel.
KINE_PROF_IDS = (77, 21)

# ── Umbrales del programa (kinesiología, cadencia esperada ~7 días) ──────────
GAP_NUEVO_EPISODIO = 45   # hueco > 45d → empieza un tratamiento nuevo
EN_CURSO_MAX       = 7    # ≤ 7d desde la última sesión → al día con su cadencia
RIESGO_MAX         = 21   # 8-21d → se saltó su próxima sesión, RECUPERABLE
ABANDONO_MAX       = 45   # 22-45d → abandono temprano probable (win-back)
                          # > 45d → episodio cerrado (no cuenta como activo)
PLAN_DEFAULT       = 0    # 0 = sin plan prescrito (analítica solo por conducta)


# ── Auth ──────────────────────────────────────────────────────────────────────

def _require_admin(request: Request, token: str | None,
                   cmc_session: str | None) -> tuple[str, int | None]:
    """(token_efectivo, scope_profesional). scope None = ve todo el programa;
    int = un kinesiólogo viendo solo SUS pacientes. Delega en alma_scope."""
    from alma_scope import resolve
    return resolve(request, token, cmc_session, "kine")


# ── Capa de gestión propia (sessions.db) ────────────────────────────────────

def ensure_kine_plan_table() -> None:
    """Plan de tratamiento por paciente (llaveado por bi.paciente_id). Idempotente."""
    from session import db as _conn
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS kine_plan (
                paciente_id    INTEGER PRIMARY KEY,   -- bi.dim_paciente.paciente_id
                sesiones_plan  INTEGER DEFAULT 0,      -- sesiones prescritas (0 = sin plan)
                estado_manual  TEXT DEFAULT '',        -- '' | 'alta' (dado de alta) | 'pausa'
                notas          TEXT DEFAULT '',
                updated_at     TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.commit()


def _planes() -> dict[int, dict]:
    ensure_kine_plan_table()
    from session import db as _conn
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM kine_plan").fetchall()
    return {r["paciente_id"]: dict(r) for r in rows}


# ── Fuente de datos: CAJA REAL (bi_pagos_caja) vía caja_helper ───────────────
# Cada pago de kinesiología = una sesión; la clasificación por días la hace _compute.
# Identidad (nombre/teléfono/localidad) y nombre de profesional las resuelve el helper.

def _bi_rows(meses: int, scope_prof: int | None = None) -> tuple[list[dict], str]:
    """Sesiones de kinesiología desde la caja real (fresca). (rows, status).

    `scope_prof`: si se pasa, acota a un solo kinesiólogo (su perfil ve solo
    SUS pacientes). None = todos los kine (admin/recepción).
    """
    from caja_helper import caja_visitas
    ids = (scope_prof,) if scope_prof is not None else KINE_PROF_IDS
    return caja_visitas(ids, meses)


def _today() -> date:
    return datetime.now(_CHILE_TZ).date()


def _split_episodios(fechas: list[date]) -> list[list[date]]:
    """Parte una lista ordenada de fechas en episodios (hueco > GAP → nuevo)."""
    if not fechas:
        return []
    eps: list[list[date]] = []
    cur = [fechas[0]]
    for f in fechas[1:]:
        if (f - cur[-1]).days > GAP_NUEVO_EPISODIO:
            eps.append(cur)
            cur = [f]
        else:
            cur.append(f)
    eps.append(cur)
    return eps


def _clasificar(dias: int, sesiones: int, plan: dict | None) -> tuple[str, str]:
    """Devuelve (estado_key, etiqueta). Plan completado/alta tiene prioridad."""
    if plan and plan.get("estado_manual") == "alta":
        return "alta", "Dado de alta"
    sp = (plan or {}).get("sesiones_plan", 0) or 0
    if sp and sesiones >= sp:
        return "completado", "Plan completado"
    if dias <= EN_CURSO_MAX:
        return "en_curso", "En tratamiento"
    if dias <= RIESGO_MAX:
        return "riesgo", "Riesgo de abandono"
    if dias <= ABANDONO_MAX:
        return "abandono", "Abandono temprano"
    return "cerrado", "Episodio cerrado"


def _compute(meses: int = 6, scope_prof: int | None = None):
    """Núcleo: una sola pasada por la BI → dataset completo de pacientes + KPIs.

    `scope_prof`: acota a un kinesiólogo (ve solo SUS pacientes). None = todos.
    """
    rows, status = _bi_rows(max(meses, 9), scope_prof)  # ventana ancha para contexto de episodios
    planes = _planes()
    today = _today()

    # Agrupar por paciente
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
            "lugar": r.get("lugar") or "", "fechas": [], "monto": 0.0,
            "prof_por_fecha": {},
        })
        d["fechas"].append(f)
        d["monto"] += float(r.get("monto") or 0)
        d["prof_por_fecha"][f] = r.get("profesional") or "—"

    pacientes = []
    carga_prof: dict[str, int] = {}
    sesiones_por_episodio: list[int] = []
    ingreso_total = 0.0
    atendidos_mes = 0  # pacientes distintos con ≥1 sesión en el mes calendario actual

    for pid, d in porpac.items():
        fechas = sorted(set(d["fechas"]))
        if not fechas:
            continue
        if any(f.year == today.year and f.month == today.month for f in fechas):
            atendidos_mes += 1
        episodios = _split_episodios(fechas)
        for ep in episodios:
            sesiones_por_episodio.append(len(ep))
        actual = episodios[-1]
        sesiones = len(actual)
        last = actual[-1]
        dias = (today - last).days
        prof = d["prof_por_fecha"].get(last, "—")
        plan = planes.get(pid)
        estado, etiqueta = _clasificar(dias, sesiones, plan)
        sp = (plan or {}).get("sesiones_plan", 0) or 0
        ingreso_total += d["monto"]
        carga_prof[prof] = carga_prof.get(prof, 0) + (1 if estado in ("en_curso", "riesgo") else 0)

        pacientes.append({
            "paciente_id": pid,
            "paciente": d["paciente"] or f"Paciente {pid}",
            "telefono": d["telefono"],
            "lugar": d["lugar"],
            "profesional": prof,
            "sesiones_episodio": sesiones,
            "sesiones_plan": sp,
            "avance_pct": (round(sesiones / sp * 100) if sp else None),
            "ultima_sesion": last.isoformat(),
            "dias_sin_sesion": dias,
            "n_episodios": len(episodios),
            "estado": estado,
            "estado_label": etiqueta,
            "notas": (plan or {}).get("notas", ""),
        })

    # Orden: del paciente con atención más reciente al de hace más tiempo
    # (menos días sin sesión primero). Estado como desempate secundario.
    prio = {"riesgo": 0, "abandono": 1, "en_curso": 2, "completado": 3, "alta": 4, "cerrado": 5}
    pacientes.sort(key=lambda p: (p["dias_sin_sesion"], prio.get(p["estado"], 9)))

    # KPIs (sobre la ventana pedida)
    activos   = [p for p in pacientes if p["estado"] in ("en_curso", "riesgo")]
    riesgo    = [p for p in pacientes if p["estado"] == "riesgo"]
    abandono  = [p for p in pacientes if p["estado"] == "abandono"]
    eps_validos = [n for n in sesiones_por_episodio if n > 0]
    prom_ses = round(sum(eps_validos) / len(eps_validos), 1) if eps_validos else 0

    kpis = {
        "activos": len(activos),
        "riesgo": len(riesgo),
        "abandono": len(abandono),
        "sesiones_prom": prom_ses,
        "ingreso_programa": round(ingreso_total),
        "atendidos_mes": atendidos_mes,
        "n_pacientes": len(pacientes),
    }
    carga = sorted(
        [{"profesional": k, "activos": v} for k, v in carga_prof.items() if k != "—"],
        key=lambda x: -x["activos"],
    )
    return {"kpis": kpis, "pacientes": pacientes, "carga": carga, "source_status": status}


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/resumen")
async def resumen(meses: int = Query(6, ge=1, le=24),
                  token: str | None = Query(None),
                  cmc_session: str | None = Cookie(None),
                  request: Request = None):
    _tok, scope = _require_admin(request, token, cmc_session)
    data = _compute(meses, scope)
    return {"kpis": data["kpis"], "carga": data["carga"], "source_status": data["source_status"]}


@router.get("/pacientes")
async def pacientes(estado: str | None = Query(None, description="filtrar por estado"),
                    meses: int = Query(6, ge=1, le=24),
                    token: str | None = Query(None),
                    cmc_session: str | None = Cookie(None),
                    request: Request = None):
    _tok, scope = _require_admin(request, token, cmc_session)
    data = _compute(meses, scope)
    pac = data["pacientes"]
    if estado and estado != "todos":
        pac = [p for p in pac if p["estado"] == estado]
    # conteos por estado para los chips
    conteos: dict[str, int] = {}
    for p in data["pacientes"]:
        conteos[p["estado"]] = conteos.get(p["estado"], 0) + 1
    return {"pacientes": pac, "conteos": conteos, "total": len(pac),
            "source_status": data["source_status"]}


@router.put("/plan/{paciente_id}")
async def set_plan(paciente_id: int, request: Request,
                   token: str | None = Query(None),
                   cmc_session: str | None = Cookie(None)):
    _require_admin(request, token, cmc_session)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Body JSON inválido")
    sesiones_plan = int(body.get("sesiones_plan") or 0)
    estado_manual = (body.get("estado_manual") or "").strip()
    if estado_manual not in ("", "alta", "pausa"):
        estado_manual = ""
    notas = (body.get("notas") or "").strip()
    ensure_kine_plan_table()
    from session import db as _conn
    with _conn() as conn:
        conn.execute("""
            INSERT INTO kine_plan (paciente_id, sesiones_plan, estado_manual, notas, updated_at)
            VALUES (?,?,?,?, datetime('now'))
            ON CONFLICT(paciente_id) DO UPDATE SET
                sesiones_plan=excluded.sesiones_plan,
                estado_manual=excluded.estado_manual,
                notas=excluded.notas,
                updated_at=excluded.updated_at
        """, (paciente_id, sesiones_plan, estado_manual, notas))
        conn.commit()
    return {"ok": True}


@router.get("/export")
async def export_csv(meses: int = Query(6, ge=1, le=24),
                     token: str | None = Query(None),
                     cmc_session: str | None = Cookie(None),
                     request: Request = None):
    _tok, scope = _require_admin(request, token, cmc_session)
    data = _compute(meses, scope)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Paciente", "Teléfono", "Lugar", "Kinesiólogo", "Sesiones episodio",
                "Plan", "Avance %", "Última sesión", "Días sin sesión", "Estado"])
    for p in data["pacientes"]:
        w.writerow([p["paciente"], p["telefono"], p["lugar"], p["profesional"],
                    p["sesiones_episodio"], p["sesiones_plan"] or "",
                    p["avance_pct"] if p["avance_pct"] is not None else "",
                    p["ultima_sesion"], p["dias_sin_sesion"], p["estado_label"]])
    buf.seek(0)
    fname = f"programa_kine_{_today().isoformat()}.csv"
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{fname}"'})
