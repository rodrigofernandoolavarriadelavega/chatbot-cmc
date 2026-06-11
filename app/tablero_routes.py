"""
Router /alma/api/tablero — Inicio / cockpit ejecutivo de Alma.

Agrega en una sola pantalla los KPIs y las ALERTAS de todos los módulos
operativos: qué requiere atención hoy (stock bajo, biológico pendiente,
exámenes sin entregar, interconsultas urgentes, mantención vencida, tareas
vencidas, docs por vencer, licencias, honorarios pendientes, avance SEREMI).

Lectura pura sobre sessions.db. FAIL-SAFE POR BLOQUE: cada métrica va en su
try/except, así una tabla que aún no existe (se crean perezosamente) o un error
puntual no tumba el tablero completo. Distinto del Copilot (capa agéntica): esto
es un rollup determinístico, sin IA.

Auth: alma_common.require_admin.
"""
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request, Query, Cookie

from alma_common import require_admin

log = logging.getLogger("tablero_routes")
_CHILE_TZ = ZoneInfo("America/Santiago")
router = APIRouter(prefix="/alma/api/tablero", tags=["tablero"])


def _q1(conn, sql, params=(), default=0):
    """SELECT de un escalar, fail-safe (tabla inexistente → default)."""
    try:
        r = conn.execute(sql, params).fetchone()
        if not r:
            return default
        v = r[0]
        return v if v is not None else default
    except Exception:
        return default


@router.get("/resumen")
async def resumen(token: str | None = Query(None), cmc_session: str | None = Cookie(None), request: Request = None):
    require_admin(request, token=token, cmc_session=cmc_session)
    from session import db as _conn
    now = datetime.now(_CHILE_TZ)
    mes = now.strftime("%Y-%m")
    hoy = now.strftime("%Y-%m-%d")

    # Período seleccionable (?desde=YYYY-MM-DD&hasta=YYYY-MM-DD). Default = mes en curso.
    def _valid_date(s):
        try:
            datetime.strptime(s, "%Y-%m-%d")
            return True
        except Exception:
            return False
    qp = request.query_params if request is not None else {}
    desde = (qp.get("desde") or "").strip()
    hasta = (qp.get("hasta") or "").strip()
    if not _valid_date(desde):
        desde = now.strftime("%Y-%m-01")
    if not _valid_date(hasta):
        hasta = hoy
    if desde > hasta:
        desde, hasta = hasta, desde

    kpis = {}
    alertas = []

    def alerta(modulo, label, valor, severidad, ruta, sub=""):
        if valor and valor > 0:
            alertas.append({"modulo": modulo, "label": label, "valor": valor,
                            "severidad": severidad, "ruta": ruta, "sub": sub})

    with _conn() as conn:
        # ── KPIs financieros del mes ──
        ingresos = _q1(conn, "SELECT COALESCE(SUM(copago),0) FROM pagos_cmc WHERE substr(fecha,1,7)=?", (mes,))
        egresos = _q1(conn, "SELECT COALESCE(SUM(monto),0) FROM egresos_cmc WHERE substr(fecha,1,7)=?", (mes,))
        kpis["ingresos_mes"] = ingresos
        kpis["egresos_mes"] = egresos
        kpis["neto_mes"] = ingresos - egresos
        kpis["pacientes_activos_mes"] = _q1(conn, "SELECT COUNT(DISTINCT rut) FROM pagos_cmc WHERE substr(fecha,1,7)=? AND rut!=''", (mes,))
        kpis["pagos_hoy"] = _q1(conn, "SELECT COUNT(*) FROM pagos_cmc WHERE fecha=?", (hoy,))
        kpis["recaudado_hoy"] = _q1(conn, "SELECT COALESCE(SUM(copago),0) FROM pagos_cmc WHERE fecha=?", (hoy,))
        kpis["citas_bot_hoy"] = _q1(conn, "SELECT COUNT(*) FROM citas_bot WHERE fecha=?", (hoy,))

        # ── KPIs del período seleccionado (default = mes en curso) ──
        ing_p = _q1(conn, "SELECT COALESCE(SUM(copago),0) FROM pagos_cmc WHERE fecha BETWEEN ? AND ?", (desde, hasta))
        egr_p = _q1(conn, "SELECT COALESCE(SUM(monto),0) FROM egresos_cmc WHERE fecha BETWEEN ? AND ?", (desde, hasta))
        kpis["recaudado"] = ing_p          # SUM(copago) = lo recaudado en el período
        kpis["ingresos"] = ing_p
        kpis["egresos"] = egr_p
        kpis["neto"] = ing_p - egr_p
        # cuenta por RUT cuando existe, si no por nombre (los históricos importados no traen RUT)
        kpis["pacientes_activos"] = _q1(conn, "SELECT COUNT(DISTINCT COALESCE(NULLIF(rut,''), paciente_nombre)) FROM pagos_cmc WHERE fecha BETWEEN ? AND ? AND COALESCE(NULLIF(rut,''), paciente_nombre)!=''", (desde, hasta))
        kpis["pagos"] = _q1(conn, "SELECT COUNT(*) FROM pagos_cmc WHERE fecha BETWEEN ? AND ?", (desde, hasta))
        kpis["citas_bot"] = _q1(conn, "SELECT COUNT(*) FROM citas_bot WHERE fecha BETWEEN ? AND ?", (desde, hasta))

        # ── Alertas por módulo ──
        # Inventario: bajo mínimo o agotado
        inv_bajo = _q1(conn, "SELECT COUNT(*) FROM inventario_dental WHERE activo=1 AND stock_actual<=stock_minimo")
        alerta("Inventario", "Insumos bajo mínimo / agotados", inv_bajo, "alta", "/alma/inventario", "reponer stock dental")

        # Esterilización: biológico pendiente de lectura
        est_bio = _q1(conn, "SELECT COUNT(*) FROM esterilizacion_ciclos WHERE ind_biologico='pendiente'")
        alerta("Esterilización", "Controles biológicos pendientes", est_bio, "alta", "/alma/esterilizacion", "lectura de esporas")

        # Exámenes: resultados listos sin entregar
        ex_listos = _q1(conn, "SELECT COUNT(*) FROM examenes_cmc WHERE estado='resultado_listo'")
        alerta("Exámenes", "Resultados listos sin entregar", ex_listos, "alta", "/alma/examenes", "avisar al paciente")

        # Interconsultas: urgentes pendientes/agendadas
        ic_urg = _q1(conn, "SELECT COUNT(*) FROM interconsultas WHERE prioridad='urgente' AND estado IN ('pendiente','agendada')")
        alerta("Interconsultas", "Derivaciones urgentes activas", ic_urg, "alta", "/alma/interconsultas")
        ic_pend = _q1(conn, "SELECT COUNT(*) FROM interconsultas WHERE estado='pendiente'")
        alerta("Interconsultas", "Derivaciones por gestionar", ic_pend, "media", "/alma/interconsultas")

        # Calidad: eventos graves abiertos
        cal_grave = _q1(conn, "SELECT COUNT(*) FROM calidad_eventos WHERE gravedad='grave' AND estado!='cerrado'")
        alerta("Calidad", "Eventos graves abiertos", cal_grave, "alta", "/alma/calidad", "seguridad del paciente")
        cal_abierto = _q1(conn, "SELECT COUNT(*) FROM calidad_eventos WHERE estado='abierto'")
        alerta("Calidad", "Eventos/reclamos abiertos", cal_abierto, "media", "/alma/calidad")

        # Mantención: vencida
        try:
            eq = conn.execute("SELECT proxima_mantencion FROM equipos_clinicos").fetchall()
            mant_venc = sum(1 for r in eq if r[0] and r[0][:10] < hoy)
        except Exception:
            mant_venc = 0
        alerta("Mantención", "Equipos con mantención vencida", mant_venc, "media", "/alma/mantencion")

        # Tareas vencidas
        try:
            tv = conn.execute("SELECT vence, estado FROM tareas_cmc WHERE estado!='hecha'").fetchall()
            tar_venc = sum(1 for r in tv if r[0] and r[0][:10] < hoy)
        except Exception:
            tar_venc = 0
        alerta("Tareas", "Tareas vencidas", tar_venc, "media", "/alma/tareas")

        # Documentos por vencer / vencidos
        try:
            dv = conn.execute("SELECT vence FROM documentos_cmc WHERE vence!=''").fetchall()
            doc_venc = sum(1 for r in dv if r[0] and r[0][:10] < hoy)
        except Exception:
            doc_venc = 0
        alerta("Documentos", "Documentos vencidos", doc_venc, "media", "/alma/documentos")

        # Equipo en licencia
        equipo_lic = _q1(conn, "SELECT COUNT(*) FROM equipo_cmc WHERE estado='licencia'")
        alerta("Equipo", "Profesionales en licencia", equipo_lic, "info", "/alma/equipo", "el bot no ofrece sus horas")

        # Liquidaciones pendientes del mes
        liq_pend = _q1(conn, "SELECT COUNT(*) FROM liquidaciones WHERE periodo=? AND estado='pendiente'", (mes,))
        alerta("Liquidaciones", "Honorarios pendientes de pago", liq_pend, "media", "/alma/liquidaciones")

        # Habilitación: % avance (informativo, no alerta de count)
        try:
            tot = _q1(conn, "SELECT COUNT(*) FROM habilitacion_items")
            hechos = _q1(conn, "SELECT COUNT(*) FROM habilitacion_items WHERE hecho=1")
            kpis["habilitacion_pct"] = round(hechos / tot * 100) if tot else 0
        except Exception:
            kpis["habilitacion_pct"] = 0

    # ordenar alertas: alta → media → info, luego por valor desc
    sev_rank = {"alta": 0, "media": 1, "info": 2}
    alertas.sort(key=lambda a: (sev_rank.get(a["severidad"], 9), -a["valor"]))

    return {
        "fecha": hoy,
        "periodo": {"desde": desde, "hasta": hasta},
        "kpis": kpis,
        "alertas": alertas,
        "n_alertas_altas": sum(1 for a in alertas if a["severidad"] == "alta"),
    }
