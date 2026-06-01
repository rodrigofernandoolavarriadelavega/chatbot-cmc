"""
Router /alma/api/conciliacion — Módulo Financiero / Conciliador (Fase 2 Alma).

Motor: reusa DIRECTAMENTE las clases y funciones de auditor.py (raíz del repo).
  - Pago, MovimientoBancario, Hallazgo
  - parse_banco, parse_transbank, normalizar_texto, normalizar_medio
  - parsear_monto, parsear_fecha, similitud_nombre
  - Auditor (con sus 4 cruces + helpers)

Inputs:
  - RECEPCION    → tabla pagos_cmc (sessions.db). Ya no es CSV.
  - MEDILINK     → API /pagos (módulo Cajas). Ya no es CSV.
  - Externos     → upload de CSV por la UI: Itaú / BancoEstado / Transbank D+C / Imed.

Capa IMED (nueva, no está en auditor.py):
  - Esperado = Σ bonificaciones de pagos Fonasa del período (de pagos_cmc).
  - Recibido  = Σ montos del CSV Imed subido (depósito/reporte semanal).
  - Hallazgo si diferencia > TOLERANCIA_MONTO.

Auth: misma que pagos_routes (_require_admin_dep).
"""
import io
import csv
import sys
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request, Query, Cookie, UploadFile, File, Form
from fastapi.responses import JSONResponse

log = logging.getLogger("conciliacion")
_CHILE_TZ = ZoneInfo("America/Santiago")

router = APIRouter(prefix="/alma/api/conciliacion", tags=["conciliacion"])

# ── Importar motor de auditor.py (raíz del repo) ─────────────────────────────
# auditor.py vive en /opt/chatbot-cmc/ (un nivel arriba de app/).
# Al arrancar, sys.path ya incluye app/ (lo hace main.py con sys.path.insert).
# Para auditor.py necesitamos el directorio raíz.

_REPO_ROOT = Path(__file__).parent.parent   # /opt/chatbot-cmc/
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from auditor import (                         # noqa: E402
    Pago,
    MovimientoBancario,
    Hallazgo,
    Auditor,
    parse_banco,
    parse_transbank,
    normalizar_texto,
    normalizar_medio,
    parsear_monto,
    parsear_fecha,
    similitud_nombre,
    TOLERANCIA_MONTO,
    TOLERANCIA_DIAS,
    TIPOS_HALLAZGO,
)

# ── Auth ──────────────────────────────────────────────────────────────────────

def _require_admin(request: Request,
                   token: str | None = Query(None),
                   cmc_session: str | None = Cookie(None)) -> str:
    import hmac as _hmac
    from config import ADMIN_TOKEN
    from admin_routes import _verify_cookie

    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        tk = auth_header.split(None, 1)[1].strip()
        if _hmac.compare_digest(tk, ADMIN_TOKEN):
            return tk
    if cmc_session:
        role = _verify_cookie(cmc_session)
        if role in ("admin", "ortodoncia"):
            return ADMIN_TOKEN
    if token and _hmac.compare_digest(token, ADMIN_TOKEN):
        return token
    raise HTTPException(status_code=401, detail="Token inválido")


# ── Helpers de carga de fuentes ───────────────────────────────────────────────

def _parse_csv_bytes(data: bytes, fuente: str) -> list[dict]:
    """Decodifica bytes CSV (utf-8-sig tolerante a BOM) → lista de dicts."""
    text = data.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    rows = []
    for row in reader:
        row["_archivo"] = fuente
        rows.append(row)
    return rows


def _pagos_cmc_a_pagos(d_desde: date, d_hasta: date) -> list[Pago]:
    """
    Lee tabla pagos_cmc de sessions.db y convierte a objetos Pago de auditor.py.
    Solo incluye registros en el rango dado.
    Mapeo de campos:
      fecha           → Pago.fecha
      paciente_nombre → Pago.paciente
      copago          → Pago.monto  (lo que paga el paciente)
      metodo_pago     → Pago.medio  (efectivo/transferencia/debito/credito)
      profesional     → Pago.profesional
      id_profesional  → Pago.id (reutilizado como referencia)
      bonificacion    → Pago.observacion (guardado para capa Imed)
    """
    from session import _conn
    try:
        with _conn() as conn:
            rows = conn.execute(
                """SELECT id, fecha, paciente_nombre, copago, bonificacion,
                          metodo_pago, profesional, id_profesional, prevision,
                          rut, id_cita
                   FROM pagos_cmc
                   WHERE fecha BETWEEN ? AND ?
                   ORDER BY fecha, hora""",
                (d_desde.isoformat(), d_hasta.isoformat())
            ).fetchall()
    except Exception as e:
        log.error("_pagos_cmc_a_pagos: error leyendo pagos_cmc: %s", e)
        return []

    pagos = []
    for row in rows:
        fecha_obj = parsear_fecha(row["fecha"]) if row["fecha"] else None
        monto = float(row["copago"] or 0)
        if monto <= 0:
            # Si copago=0 pero hay bonificacion, igual incluir para cruce Medilink
            monto = float(row["bonificacion"] or 0)
        if monto <= 0:
            continue

        medio_raw = row["metodo_pago"] or "efectivo"
        medio = normalizar_medio(medio_raw)

        pagos.append(Pago(
            fuente="RECEPCION",
            fecha=fecha_obj,
            paciente=str(row["paciente_nombre"] or "").strip(),
            monto=monto,
            medio=medio,
            profesional=str(row["profesional"] or "").strip(),
            observacion=str(row["bonificacion"] or "0"),  # bonif guardada aquí
            id=row["id"],
        ))
    return pagos


def _medilink_pagos_a_pagos(d_desde: date, d_hasta: date) -> list[Pago]:
    """
    Llama la API Medilink /pagos (módulo Cajas) y convierte a objetos Pago.
    Wrapper síncrono: usa httpx síncrono para no complicar el endpoint.
    Fechas en formato DD/MM/YYYY para la API.
    """
    import httpx
    import os

    base_url = os.getenv("MEDILINK_BASE_URL", "https://api.medilink2.healthatom.com/api/v5")
    token = os.getenv("MEDILINK_TOKEN", "")
    sucursal = os.getenv("MEDILINK_SUCURSAL", "1")

    if not token:
        log.warning("_medilink_pagos_a_pagos: MEDILINK_TOKEN no configurado")
        return []

    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    desde_str = d_desde.strftime("%d/%m/%Y")
    hasta_str = d_hasta.strftime("%d/%m/%Y")

    pagos = []
    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.get(
                f"{base_url}/pagos",
                params={
                    "id_sucursal": sucursal,
                    "fecha_desde": desde_str,
                    "fecha_hasta": hasta_str,
                },
                headers=headers,
            )
        if resp.status_code != 200:
            log.warning("_medilink_pagos_a_pagos: HTTP %d", resp.status_code)
            return []
        data = resp.json()
        items = data if isinstance(data, list) else data.get("data", data.get("pagos", []))
        for i, item in enumerate(items):
            # Campos típicos del endpoint /pagos de Medilink
            fecha_raw = (
                item.get("fecha") or item.get("fecha_pago") or
                item.get("fecha_atencion") or ""
            )
            fecha_obj = parsear_fecha(str(fecha_raw)) if fecha_raw else None
            if fecha_obj and not (d_desde <= fecha_obj <= d_hasta):
                continue

            monto_raw = (
                item.get("monto") or item.get("total") or
                item.get("copago") or item.get("valor") or 0
            )
            monto = float(monto_raw or 0)
            if monto <= 0:
                continue

            paciente = str(
                item.get("paciente") or item.get("nombre_paciente") or
                item.get("nombre") or ""
            ).strip()

            medio_raw = str(
                item.get("forma_pago") or item.get("medio") or
                item.get("tipo_pago") or ""
            )
            medio = normalizar_medio(medio_raw) if medio_raw else "DESCONOCIDO"

            profesional = str(
                item.get("profesional") or item.get("medico") or ""
            ).strip()

            pagos.append(Pago(
                fuente="MEDILINK",
                fecha=fecha_obj,
                paciente=paciente,
                monto=monto,
                medio=medio,
                profesional=profesional,
                id=i,
            ))
    except Exception as e:
        log.error("_medilink_pagos_a_pagos: error llamando API: %s", e)

    return pagos


# ── Capa Imed (nueva — no existe en auditor.py) ───────────────────────────────

def _bonif_desde_arancel(area: str) -> float:
    """
    Retorna la bonificacion Imed esperada para un area Fonasa, tomada del
    arancel N3. Si el area no está en el arancel retorna 0.
    Importa _ARANCEL_N3 desde pagos_routes para no duplicar la constante.
    """
    try:
        from pagos_routes import _ARANCEL_N3
        a = _ARANCEL_N3.get(area)
        if a:
            return float(a.get("bonif", 0))
    except Exception as e:
        log.warning("_bonif_desde_arancel: %s", e)
    return 0.0


def _cruzar_imed(
    pagos_recepcion: list[Pago],
    movs_imed: list[MovimientoBancario],
    d_desde: date,
    d_hasta: date,
) -> list[Hallazgo]:
    """
    Concilia bonificaciones Fonasa esperadas de Imed contra el depósito/reporte
    semanal de Imed (CSV subido).

    Lógica:
    1. Esperado = Σ bonif_arancel_N3(area) de cada registro Fonasa del período.
       La bonificacion NO se lee del campo guardado (ya no se ingresa en caja).
       Se calcula desde _ARANCEL_N3 por area, igual que lo hacía _sugerir_copago.
    2. Recibido  = Σ de los movimientos del CSV Imed (todos son CREDITO).
    3. Hallazgo FALTANTE si recibido < esperado - tolerancia.
    4. Hallazgo SOBRANTE si recibido > esperado + tolerancia.
    """
    hallazgos: list[Hallazgo] = []

    # Leer registros Fonasa del período y calcular bonif desde arancel N3
    from session import _conn
    try:
        with _conn() as conn:
            rows = conn.execute(
                """SELECT id, fecha, paciente_nombre, area, copago
                   FROM pagos_cmc
                   WHERE fecha BETWEEN ? AND ?
                     AND prevision = 'fonasa'
                   ORDER BY fecha""",
                (d_desde.isoformat(), d_hasta.isoformat())
            ).fetchall()
    except Exception as e:
        log.error("_cruzar_imed: error leyendo pagos_cmc: %s", e)
        rows = []

    # Calcular bonif esperada para cada registro desde arancel N3
    bonifs = [_bonif_desde_arancel(r["area"] or "") for r in rows]
    rows_con_bonif = [(r, b) for r, b in zip(rows, bonifs) if b > 0]
    esperado_total = sum(b for _, b in rows_con_bonif)
    recibido_total = sum(m.monto for m in movs_imed if m.tipo == "CREDITO")
    n_bonif = len(rows_con_bonif)
    n_depositos = len([m for m in movs_imed if m.tipo == "CREDITO"])

    if esperado_total <= 0 and recibido_total <= 0:
        return hallazgos   # Sin datos para conciliar

    diferencia = recibido_total - esperado_total

    # Resumen global Imed
    if abs(diferencia) > TOLERANCIA_MONTO:
        if diferencia < 0:
            hallazgos.append(Hallazgo(
                fecha=None,
                paciente="RESUMEN IMED",
                monto_interno=esperado_total,
                monto_externo=recibido_total,
                fuente_interna="RECEPCION",
                fuente_externa="IMED",
                medio="IMED",
                tipo="FALTANTE",
                comentario=(
                    f"Imed pagó ${recibido_total:,.0f} pero se esperaban ${esperado_total:,.0f} "
                    f"({n_bonif} bonificaciones Fonasa del período). "
                    f"Diferencia: ${abs(diferencia):,.0f} menos de lo esperado."
                ),
                prioridad="ALTA",
            ))
        else:
            hallazgos.append(Hallazgo(
                fecha=None,
                paciente="RESUMEN IMED",
                monto_interno=esperado_total,
                monto_externo=recibido_total,
                fuente_interna="RECEPCION",
                fuente_externa="IMED",
                medio="IMED",
                tipo="SOBRANTE",
                comentario=(
                    f"Imed pagó ${recibido_total:,.0f} pero se esperaban ${esperado_total:,.0f} "
                    f"({n_bonif} bonificaciones Fonasa del período). "
                    f"Diferencia: ${abs(diferencia):,.0f} más de lo esperado — verificar período."
                ),
                prioridad="MEDIA",
            ))
    # Si no hay CSV Imed pero hay bonificaciones esperadas
    if n_depositos == 0 and esperado_total > 0:
        hallazgos.append(Hallazgo(
            fecha=None,
            paciente="RESUMEN IMED",
            monto_interno=esperado_total,
            monto_externo=None,
            fuente_interna="RECEPCION",
            fuente_externa="IMED",
            medio="IMED",
            tipo="SIN_RESPALDO_BANCARIO",
            comentario=(
                f"No se subió CSV de Imed. Bonificaciones esperadas: ${esperado_total:,.0f} "
                f"({n_bonif} registros Fonasa). Subir el reporte semanal de Imed para conciliar."
            ),
            prioridad="MEDIA",
        ))

    return hallazgos


# ── AuditorWeb: extiende Auditor de auditor.py ───────────────────────────────

class AuditorWeb(Auditor):
    """
    Versión del Auditor adaptada para la web:
    - cargar_datos() lee desde pagos_cmc (SQLite) + API Medilink en vez de CSV.
    - Agrega cruzar_imed() con los movimientos Imed del upload.
    """

    def __init__(
        self,
        desde: Optional[date],
        hasta: Optional[date],
        movs_transferencia: list[MovimientoBancario] = None,
        movs_efectivo: list[MovimientoBancario] = None,
        movs_tb_debito: list[MovimientoBancario] = None,
        movs_tb_credito: list[MovimientoBancario] = None,
        movs_imed: list[MovimientoBancario] = None,
    ):
        super().__init__(desde=desde, hasta=hasta)
        # Inyectar movimientos ya parseados desde los uploads
        self.movs_transferencia = movs_transferencia or []
        self.movs_efectivo      = movs_efectivo or []
        self.movs_tb_debito     = movs_tb_debito or []
        self.movs_tb_credito    = movs_tb_credito or []
        self.movs_imed          = movs_imed or []

    def cargar_datos_web(self):
        """Carga recepción (SQLite) y Medilink (API). Movimientos ya cargados en __init__."""
        d_desde = self.desde or date.today().replace(day=1)
        d_hasta = self.hasta or date.today()

        self.pagos_recepcion = _pagos_cmc_a_pagos(d_desde, d_hasta)
        self.pagos_medilink  = _medilink_pagos_a_pagos(d_desde, d_hasta)

        log.info(
            "AuditorWeb.cargar_datos_web: recepcion=%d medilink=%d "
            "transf=%d efvo=%d tbd=%d tbc=%d imed=%d",
            len(self.pagos_recepcion), len(self.pagos_medilink),
            len(self.movs_transferencia), len(self.movs_efectivo),
            len(self.movs_tb_debito), len(self.movs_tb_credito),
            len(self.movs_imed),
        )

    def auditar_web(self):
        """Ejecuta todos los cruces incluyendo la capa Imed."""
        self.cargar_datos_web()
        self.cruzar_recepcion_medilink()
        self.cruzar_transferencias()
        self.cruzar_efectivo()
        self.cruzar_transbank("TRANSBANK_DEBITO",  self.movs_tb_debito)
        self.cruzar_transbank("TRANSBANK_CREDITO", self.movs_tb_credito)
        # Capa Imed (nueva)
        d_desde = self.desde or date.today().replace(day=1)
        d_hasta = self.hasta or date.today()
        imed_hallazgos = _cruzar_imed(
            self.pagos_recepcion, self.movs_imed, d_desde, d_hasta
        )
        self.hallazgos.extend(imed_hallazgos)

    def _totales_externos(self) -> dict:
        """Extiende el método base para incluir IMED."""
        base = super()._totales_externos()
        base["IMED"] = sum(m.monto for m in self.movs_imed if m.tipo == "CREDITO")
        return base

    def _totales_internos_imed(self) -> float:
        """Suma de bonificaciones Fonasa del período (esperado Imed)."""
        d_desde = self.desde or date.today().replace(day=1)
        d_hasta = self.hasta or date.today()
        from session import _conn
        try:
            with _conn() as conn:
                row = conn.execute(
                    """SELECT COALESCE(SUM(bonificacion), 0) as total
                       FROM pagos_cmc
                       WHERE fecha BETWEEN ? AND ?
                         AND prevision = 'fonasa'
                         AND bonificacion > 0""",
                    (d_desde.isoformat(), d_hasta.isoformat())
                ).fetchone()
            return float(row["total"] or 0)
        except Exception:
            return 0.0


# ── Helpers de serialización ──────────────────────────────────────────────────

def _hallazgo_to_dict(h: Hallazgo) -> dict:
    return {
        "fecha":          h.fecha.isoformat() if h.fecha else None,
        "paciente":       h.paciente or "—",
        "monto_interno":  h.monto_interno,
        "monto_externo":  h.monto_externo,
        "fuente_interna": h.fuente_interna,
        "fuente_externa": h.fuente_externa,
        "medio":          h.medio,
        "tipo":           h.tipo,
        "comentario":     h.comentario,
        "prioridad":      h.prioridad,
    }


def _cuadre_to_list(auditor: AuditorWeb) -> list[dict]:
    return auditor._cuadre_diario()


def _kpis(auditor: AuditorWeb) -> dict:
    totales_int = auditor._totales_internos()
    totales_ext = auditor._totales_externos()
    imed_esperado = auditor._totales_internos_imed()

    medios = ["TRANSFERENCIA", "EFECTIVO", "TRANSBANK_DEBITO", "TRANSBANK_CREDITO", "IMED"]
    fuentes = []
    for m in medios:
        if m == "IMED":
            registrado = imed_esperado
        else:
            registrado = totales_int.get(m, 0.0)
        respaldado = totales_ext.get(m, 0.0)
        diferencia = registrado - respaldado
        fuentes.append({
            "medio":      m,
            "registrado": registrado,
            "respaldado": respaldado,
            "diferencia": diferencia,
            "ok":         abs(diferencia) <= TOLERANCIA_MONTO or registrado == 0,
        })

    total_registrado = sum(totales_int.values())
    total_respaldado = sum(v for k, v in totales_ext.items() if k != "IMED")
    n_alta = sum(1 for h in auditor.hallazgos if h.prioridad == "ALTA")
    n_media = sum(1 for h in auditor.hallazgos if h.prioridad == "MEDIA")
    n_baja  = sum(1 for h in auditor.hallazgos if h.prioridad == "BAJA")

    cuadre = auditor._cuadre_diario()
    n_ok  = sum(1 for c in cuadre if c["estado"] == "CUADRA")
    n_obs = sum(1 for c in cuadre if c["estado"] == "CUADRA CON OBSERVACIONES")
    n_no  = sum(1 for c in cuadre if c["estado"] == "NO CUADRA")

    nivel_riesgo = "BAJO"
    if n_alta > 5 or n_no > 0:
        nivel_riesgo = "ALTO"
    elif n_alta > 0 or n_obs > 0:
        nivel_riesgo = "MEDIO"

    pct_conciliado = 0.0
    if total_registrado > 0:
        # porcentaje que tiene respaldo externo
        pct_conciliado = min(100.0, round(total_respaldado / total_registrado * 100, 1))

    return {
        "periodo_desde":   auditor.desde.isoformat() if auditor.desde else None,
        "periodo_hasta":   auditor.hasta.isoformat() if auditor.hasta else None,
        "total_registrado":  total_registrado,
        "total_respaldado":  total_respaldado,
        "diferencia_neta":   total_registrado - total_respaldado,
        "pct_conciliado":    pct_conciliado,
        "n_hallazgos":       len(auditor.hallazgos),
        "n_alta":            n_alta,
        "n_media":           n_media,
        "n_baja":            n_baja,
        "nivel_riesgo":      nivel_riesgo,
        "dias_cuadran":      n_ok,
        "dias_observacion":  n_obs,
        "dias_no_cuadran":   n_no,
        "n_recepcion":       len(auditor.pagos_recepcion),
        "n_medilink":        len(auditor.pagos_medilink),
        "fuentes":           fuentes,
        "imed_esperado":     imed_esperado,
        "imed_recibido":     totales_ext.get("IMED", 0.0),
    }


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/run")
async def run_conciliacion(
    request: Request,
    token: str | None = Query(None),
    cmc_session: str | None = Cookie(None),
    fecha_desde: str | None = Form(None),
    fecha_hasta: str | None = Form(None),
    itau: UploadFile | None = File(None),
    banco_estado: UploadFile | None = File(None),
    transbank_debito: UploadFile | None = File(None),
    transbank_credito: UploadFile | None = File(None),
    imed: UploadFile | None = File(None),
):
    """
    Ejecuta la conciliación completa para el período dado.

    Inputs automáticos:
      - Recepción: tabla pagos_cmc (SQLite, sin upload)
      - Medilink: API /pagos en vivo (sin upload)

    Inputs por upload (opcionales):
      - itau             → CSV extracto Itaú (transferencias)
      - banco_estado     → CSV extracto BancoEstado (efectivo)
      - transbank_debito → CSV reporte Transbank débito
      - transbank_credito→ CSV reporte Transbank crédito
      - imed             → CSV reporte/depósito Imed

    Retorna JSON con kpis, hallazgos y cuadre_diario.
    """
    _require_admin(request, token=token, cmc_session=cmc_session)

    # Período
    now_cl = datetime.now(_CHILE_TZ)
    try:
        d_desde = date.fromisoformat(fecha_desde) if fecha_desde else now_cl.date().replace(day=1)
        d_hasta = date.fromisoformat(fecha_hasta) if fecha_hasta else now_cl.date()
    except ValueError:
        raise HTTPException(400, "fechas deben ser YYYY-MM-DD")

    if d_desde > d_hasta:
        raise HTTPException(400, "fecha_desde no puede ser mayor que fecha_hasta")

    # Parsear archivos subidos
    async def _read_upload(up: UploadFile | None) -> bytes:
        if up is None or up.filename == "":
            return b""
        return await up.read()

    itau_bytes    = await _read_upload(itau)
    be_bytes      = await _read_upload(banco_estado)
    tbd_bytes     = await _read_upload(transbank_debito)
    tbc_bytes     = await _read_upload(transbank_credito)
    imed_bytes    = await _read_upload(imed)

    movs_transf = parse_banco(_parse_csv_bytes(itau_bytes, "ITAU"), "TRANSFERENCIA") if itau_bytes else []
    movs_efvo   = parse_banco(_parse_csv_bytes(be_bytes, "BANCO_ESTADO"), "EFECTIVO") if be_bytes else []
    movs_tbd    = parse_transbank(_parse_csv_bytes(tbd_bytes, "TRANSBANK_DEBITO"), "TRANSBANK_DEBITO") if tbd_bytes else []
    movs_tbc    = parse_transbank(_parse_csv_bytes(tbc_bytes, "TRANSBANK_CREDITO"), "TRANSBANK_CREDITO") if tbc_bytes else []
    movs_imed_parsed = parse_banco(_parse_csv_bytes(imed_bytes, "IMED"), "IMED") if imed_bytes else []

    auditor = AuditorWeb(
        desde=d_desde,
        hasta=d_hasta,
        movs_transferencia=movs_transf,
        movs_efectivo=movs_efvo,
        movs_tb_debito=movs_tbd,
        movs_tb_credito=movs_tbc,
        movs_imed=movs_imed_parsed,
    )
    auditor.auditar_web()

    hallazgos_sorted = sorted(
        auditor.hallazgos,
        key=lambda h: (0 if h.prioridad == "ALTA" else 1 if h.prioridad == "MEDIA" else 2,
                       h.fecha or date.min)
    )

    return {
        "ok": True,
        "kpis": _kpis(auditor),
        "hallazgos": [_hallazgo_to_dict(h) for h in hallazgos_sorted],
        "cuadre_diario": _cuadre_to_list(auditor),
    }


@router.get("/preview")
async def preview_periodo(
    fecha_desde: str | None = Query(None),
    fecha_hasta: str | None = Query(None),
    token: str | None = Query(None),
    cmc_session: str | None = Cookie(None),
    request: Request = None,
):
    """
    Vista previa rápida sin uploads: solo recepción + Medilink.
    Útil para ver el estado antes de subir los extractos externos.
    """
    _require_admin(request, token=token, cmc_session=cmc_session)

    now_cl = datetime.now(_CHILE_TZ)
    try:
        d_desde = date.fromisoformat(fecha_desde) if fecha_desde else now_cl.date().replace(day=1)
        d_hasta = date.fromisoformat(fecha_hasta) if fecha_hasta else now_cl.date()
    except ValueError:
        raise HTTPException(400, "fechas deben ser YYYY-MM-DD")

    auditor = AuditorWeb(desde=d_desde, hasta=d_hasta)
    auditor.cargar_datos_web()
    auditor.cruzar_recepcion_medilink()

    return {
        "ok": True,
        "periodo_desde": d_desde.isoformat(),
        "periodo_hasta": d_hasta.isoformat(),
        "n_recepcion":   len(auditor.pagos_recepcion),
        "n_medilink":    len(auditor.pagos_medilink),
        "n_hallazgos":   len(auditor.hallazgos),
        "hallazgos":     [_hallazgo_to_dict(h) for h in auditor.hallazgos],
        "total_recepcion": sum(p.monto for p in auditor.pagos_recepcion),
        "total_medilink":  sum(p.monto for p in auditor.pagos_medilink),
    }


@router.get("/tipos")
async def get_tipos(
    token: str | None = Query(None),
    request: Request = None,
):
    """Lista de tipos de hallazgo y fuentes disponibles (para filtros en la UI)."""
    return {
        "tipos_hallazgo": TIPOS_HALLAZGO,
        "fuentes_externas": ["ITAU", "BANCO_ESTADO", "TRANSBANK_DEBITO", "TRANSBANK_CREDITO", "IMED"],
        "prioridades": ["ALTA", "MEDIA", "BAJA"],
    }
