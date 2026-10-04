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
    from config import ADMIN_TOKEN
    from admin_routes import _verify_cookie, _is_admin_token

    # Acepta el mismo conjunto de tokens que la página /alma/conciliacion
    # (_is_admin_token = ADMIN_TOKEN + OLACORE_TOKEN). Antes solo aceptaba
    # ADMIN_TOKEN exacto, por lo que el dueño con token OLACORE recibía 401.
    auth_header = request.headers.get("authorization", "")
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


def _leer_pagos_cmc(d_desde: date, d_hasta: date) -> list[dict]:
    """Filas de pagos_cmc del rango, con la bonificación Fonasa esperada ya
    calculada desde el arancel N3 (`bonif_arancel`). La columna
    `bonificacion` NO se usa: recepción dejó de ingresarla (sep-2026: 0 filas
    con valor), así que sumarla daba siempre $0."""
    from session import db as _conn
    try:
        with _conn() as conn:
            rows = conn.execute(
                """SELECT id, fecha, paciente_nombre, copago, metodo_pago,
                          profesional, prevision, area, rut
                   FROM pagos_cmc
                   WHERE fecha BETWEEN ? AND ?
                   ORDER BY fecha, hora""",
                (d_desde.isoformat(), d_hasta.isoformat())
            ).fetchall()
    except Exception as e:
        log.error("_leer_pagos_cmc: error leyendo pagos_cmc: %s", e)
        return []
    out = []
    for r in rows:
        d = dict(r)
        d["copago"] = int(d["copago"] or 0)
        es_fonasa = (d["prevision"] or "").strip().lower() == "fonasa"
        d["bonif_arancel"] = int(_bonif_desde_arancel(d["area"] or "")) if es_fonasa else 0
        out.append(d)
    return out


def _pagos_cmc_a_pagos(filas: list[dict]) -> list[Pago]:
    """Convierte filas de pagos_cmc en objetos Pago para los cruces POR MEDIO
    (transferencia, efectivo, Transbank). Pago.monto = copago: es lo único que
    pasa por el medio de pago — la bonificación la paga Imed, no el paciente.

    Antes, con copago=0 se usaba la bonificación como monto y el medio por
    defecto era 'efectivo': plata de Imed aparecía como efectivo esperado en
    BancoEstado. Una fila sin medio queda como SIN_MEDIO, nunca se adivina."""
    pagos = []
    for row in filas:
        if row["copago"] <= 0:
            continue
        medio = normalizar_medio(row["metodo_pago"]) if row["metodo_pago"] else "SIN_MEDIO"
        pagos.append(Pago(
            fuente="RECEPCION",
            fecha=parsear_fecha(row["fecha"]) if row["fecha"] else None,
            paciente=str(row["paciente_nombre"] or "").strip(),
            monto=float(row["copago"]),
            medio=medio,
            profesional=str(row["profesional"] or "").strip(),
            id=row["id"],
        ))
    return pagos


# ── Lado Medilink: caja local (bi_pagos_caja) ─────────────────────────────────
#
# Antes este lado llamaba a la API /pagos de Medilink con `Bearer` (Medilink usa
# `Token`) → HTTP 401 en producción: el lado Medilink venía SIEMPRE vacío y cada
# pago de recepción salía como "FALTANTE ALTA" (~1.500 falsos por mes). Aunque
# autenticara, pedía una sola página (Medilink pagina de a 50 sin ordenar por
# fecha). `bi_pagos_caja` es la misma caja, sincronizada cada 30 min por
# bi_sync con paginación correcta — no toca el HIS al conciliar.

def _leer_caja_medilink(d_desde: date, d_hasta: date) -> list[dict]:
    """Pagos de caja Medilink agregados por (fecha, paciente). El nombre sale de
    la atención del propio pago, luego de cualquier atención del paciente,
    luego de citas_cache. `metodo_pago` de esta tabla NO se usa: es 'Efectivo'
    por defecto en el 99,7% de las filas (ver docs/LIBRO_DE_LA_VERDAD.md)."""
    from session import db as _conn
    try:
        with _conn() as conn:
            rows = conn.execute(
                """SELECT g.fecha, g.id_paciente, g.monto, g.n,
                          COALESCE(
                            (SELECT a.paciente_nombre FROM bi_atenciones a
                              WHERE a.atencion_id = g.atencion_id),
                            (SELECT a.paciente_nombre FROM bi_atenciones a
                              WHERE a.id_paciente = g.id_paciente
                                AND COALESCE(a.paciente_nombre,'') <> ''
                              ORDER BY a.atencion_id DESC LIMIT 1),
                            (SELECT c.paciente_nombre FROM citas_cache c
                              WHERE c.id_paciente = g.id_paciente LIMIT 1),
                            '') AS paciente
                   FROM (SELECT fecha, id_paciente, SUM(monto) AS monto, COUNT(*) AS n,
                                MAX(atencion_id) AS atencion_id
                           FROM bi_pagos_caja
                          WHERE fecha BETWEEN ? AND ?
                          GROUP BY fecha, id_paciente) g""",
                (d_desde.isoformat(), d_hasta.isoformat())
            ).fetchall()
    except Exception as e:
        log.error("_leer_caja_medilink: %s", e)
        return []
    return [{"fecha": r["fecha"], "id_paciente": r["id_paciente"],
             "paciente": r["paciente"] or "", "monto": int(r["monto"] or 0),
             "n": r["n"]} for r in rows if (r["monto"] or 0) > 0]


def _clp(n) -> str:
    return "$" + f"{int(round(n)):,}".replace(",", ".")


# Repeticiones de la MISMA diferencia (esperado, caja) para tratarla como patrón.
_MIN_PATRON = 5


def _tokens_nombre(s: str) -> set[str]:
    return {t for t in normalizar_texto(s or "").replace(".", " ").split() if len(t) >= 3}


def _sim_paciente(a: str, b: str) -> float:
    """Coincidencia de nombres tolerante a que una fuente tenga nombre corto
    ("Juan Pérez") y la otra el completo ("JUAN ANDRÉS PÉREZ SOTO"): fracción
    de tokens del nombre MÁS CORTO presentes en el otro. Exige ≥2 tokens en
    común (salvo nombres de un token) para que un apellido común solo no
    empareje a dos personas distintas."""
    ta, tb = _tokens_nombre(a), _tokens_nombre(b)
    if not ta or not tb:
        return 0.0
    inter = ta & tb
    if len(inter) < min(2, len(ta), len(tb)):
        return 0.0
    return len(inter) / min(len(ta), len(tb))


def _prioridad_cruce_caja(tipo: str, monto: int, prevision: str) -> str:
    """Prioridad de un hallazgo del cruce recepción ↔ caja Medilink.

    tipo: 'FALTANTE'         → recepción registró cobro, la caja Medilink no tiene nada
                               ese paciente-día (la plata no quedó en el HIS: honorarios
                               y BI se calculan desde la caja, así que nadie la ve).
          'SOBRANTE'         → la caja tiene el pago y el módulo Pagos de recepción no
                               (el panel de recepción no lo muestra; la plata sí está).
          'DIFERENCIA_MONTO' → ambos lo tienen pero los montos no calzan; `monto` es
                               |esperado - caja|.
    prevision: 'fonasa' | 'particular' | ... (de recepción; '' en SOBRANTE).
    Devuelve 'ALTA' | 'MEDIA' | 'BAJA'.
    """
    # TODO(Rodrigo): tu regla de negocio — ver mensaje en el chat.
    return "ALTA" if tipo == "FALTANTE" else "MEDIA"


def _cruzar_caja(recep: list[dict], caja: list[dict], hasta_sync: date | None) -> tuple[list[Hallazgo], dict]:
    """Cruce puro recepción (pagos_cmc) ↔ caja Medilink por PACIENTE-DÍA.

    Esperado en caja = Σ(copago + bonificación Fonasa por arancel): Medilink
    guarda el arancel completo de una atención Fonasa, recepción solo el copago.
    Comparar copago contra caja hacía "diferir" a toda la Fonasa.

    Filas de recepción sin cobro (copago 0, sin medio = prellenadas que nunca
    se cobraron) no entran. Días posteriores al último sync de caja tampoco:
    saldrían como faltantes solo porque la caja aún no los trajo."""
    from collections import defaultdict
    grupos_r: dict = defaultdict(lambda: {"esperado": 0, "copago": 0, "nombre": "", "prevision": "", "ids": []})
    for r in recep:
        if r["copago"] <= 0 and not r["metodo_pago"]:
            continue
        esperado = r["copago"] + r["bonif_arancel"]
        if esperado <= 0:
            continue
        f = r["fecha"]
        if hasta_sync and date.fromisoformat(f) > hasta_sync:
            continue
        k = (f, r["rut"] or normalizar_texto(r["paciente_nombre"] or ""))
        g = grupos_r[k]
        g["fecha"], g["nombre"] = f, g["nombre"] or (r["paciente_nombre"] or "").strip()
        g["prevision"] = g["prevision"] or (r["prevision"] or "")
        g["esperado"] += esperado
        g["copago"] += r["copago"]
        g["ids"].append(r["id"])
    caja_f = [c for c in caja if not (hasta_sync and date.fromisoformat(c["fecha"]) > hasta_sync)]

    # Emparejar por mejor similitud: mismo día primero, luego ±1 día.
    pares = []
    for i, g in enumerate(grupos_r.values()):
        fr = date.fromisoformat(g["fecha"])
        for j, c in enumerate(caja_f):
            dd = abs((fr - date.fromisoformat(c["fecha"])).days)
            if dd > 1:
                continue
            s = _sim_paciente(g["nombre"], c["paciente"])
            if s >= 0.66:
                pares.append((dd, -s, abs(g["esperado"] - c["monto"]), i, j))
    pares.sort()
    gl = list(grupos_r.values())
    usados_r, usados_c, hallazgos = set(), set(), []
    n_ok = 0
    difs: list[tuple[dict, dict, int]] = []
    for dd, _s, dif, i, j in pares:
        if i in usados_r or j in usados_c:
            continue
        usados_r.add(i); usados_c.add(j)
        if dif <= TOLERANCIA_MONTO:
            n_ok += 1
        else:
            difs.append((gl[i], caja_f[j], dif))

    # Una diferencia que se repite idéntica (mismo esperado, mismo monto en caja)
    # no es un error de recepción: es una tarifa distinta entre el módulo Pagos y
    # Medilink (ej. sep-2026: 600+ consultas Fonasa MG con recepción $15.760 vs
    # caja $15.130). Se reporta UNA vez como patrón con su total, para que no
    # tape los casos sueltos, que sí son anomalías.
    from collections import Counter
    frec = Counter((g["esperado"], c["monto"]) for g, c, _ in difs)
    patrones: dict = {}
    for g, c, dif in difs:
        clave = (g["esperado"], c["monto"])
        if frec[clave] >= _MIN_PATRON:
            p = patrones.setdefault(clave, {"n": 0, "prev": Counter(), "f0": g["fecha"], "f1": g["fecha"]})
            p["n"] += 1
            p["prev"][g["prevision"] or "?"] += 1
            p["f0"], p["f1"] = min(p["f0"], g["fecha"]), max(p["f1"], g["fecha"])
            continue
        hallazgos.append(Hallazgo(
            fecha=date.fromisoformat(g["fecha"]), paciente=g["nombre"],
            monto_interno=g["esperado"], monto_externo=c["monto"],
            fuente_interna="RECEPCION", fuente_externa="MEDILINK", medio="CAJA",
            tipo="DIFERENCIA_MONTO",
            comentario=(f"Recepción ${g['esperado']:,.0f}"
                        + (f" (copago ${g['copago']:,.0f} + bonif. Fonasa)" if g["esperado"] != g["copago"] else "")
                        + f" vs caja Medilink ${c['monto']:,.0f}").replace(",", "."),
            prioridad=_prioridad_cruce_caja("DIFERENCIA_MONTO", int(dif), g["prevision"]),
        ))
    for (esp, caj), p in sorted(patrones.items(), key=lambda kv: -kv[1]["n"] * abs(kv[0][0] - kv[0][1])):
        prev = p["prev"].most_common(1)[0][0]
        hallazgos.append(Hallazgo(
            fecha=None, paciente=f"PATRÓN · {p['n']} atenciones",
            monto_interno=float(esp * p["n"]), monto_externo=float(caj * p["n"]),
            fuente_interna="RECEPCION", fuente_externa="MEDILINK", medio="CAJA",
            tipo="DIFERENCIA_MONTO",
            comentario=(f"{p['n']} atenciones ({prev}, {p['f0'][8:10]}/{p['f0'][5:7]}–{p['f1'][8:10]}/{p['f1'][5:7]}) "
                        f"con recepción {_clp(esp)} y caja {_clp(caj)}: {_clp(abs(esp - caj))} c/u, "
                        f"{_clp(abs(esp - caj) * p['n'])} en total. Diferencia sistemática: tarifa distinta "
                        f"entre módulo Pagos y Medilink, o área/previsión mal asignada en recepción."),
            prioridad=_prioridad_cruce_caja("DIFERENCIA_MONTO", abs(esp - caj) * p["n"], prev),
        ))
    for i, g in enumerate(gl):
        if i in usados_r:
            continue
        hallazgos.append(Hallazgo(
            fecha=date.fromisoformat(g["fecha"]), paciente=g["nombre"],
            monto_interno=g["esperado"], monto_externo=None,
            fuente_interna="RECEPCION", fuente_externa="MEDILINK", medio="CAJA",
            tipo="FALTANTE",
            comentario="Recepción registró el cobro pero la caja Medilink no tiene pago de este paciente ese día",
            prioridad=_prioridad_cruce_caja("FALTANTE", g["esperado"], g["prevision"]),
        ))
    for j, c in enumerate(caja_f):
        if j in usados_c:
            continue
        hallazgos.append(Hallazgo(
            fecha=date.fromisoformat(c["fecha"]), paciente=c["paciente"] or f"id_paciente {c['id_paciente']}",
            monto_interno=None, monto_externo=c["monto"],
            fuente_interna="RECEPCION", fuente_externa="MEDILINK", medio="CAJA",
            tipo="SOBRANTE",
            comentario="Pago en caja Medilink sin registro en el módulo Pagos de recepción",
            prioridad=_prioridad_cruce_caja("SOBRANTE", c["monto"], ""),
        ))
    resumen = {"grupos_recepcion": len(gl), "grupos_caja": len(caja_f),
               "emparejados": len(usados_r), "cuadran": n_ok,
               "en_patron": sum(p["n"] for p in patrones.values())}
    return hallazgos, resumen


def _hasta_sync_caja() -> tuple[date | None, list[str]]:
    """Último día que la caja local tiene completo (fecha CLT del último sync
    de pagos) + advertencia si el sync está viejo."""
    try:
        import verdad
        st = verdad.sync_status()
        p = st["sync"].get("pagos") or {}
        if not p.get("ultimo_sync_utc"):
            return None, ["No hay registro de sincronización de la caja Medilink; el cruce con caja puede estar incompleto."]
        ts = datetime.fromisoformat(p["ultimo_sync_utc"][:19]).replace(tzinfo=ZoneInfo("UTC"))
        hasta = ts.astimezone(_CHILE_TZ).date()
        adv = []
        if (p.get("edad_horas") or 0) > 2:
            adv.append(f"La caja Medilink se sincronizó hace {p['edad_horas']:.0f} h; lo cobrado después aún no aparece.")
        return hasta, adv
    except Exception as e:
        log.warning("_hasta_sync_caja: %s", e)
        return None, []


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
    from session import db as _conn
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
    - Recepción desde pagos_cmc; lado Medilink desde la caja local bi_pagos_caja
      (cruce por paciente-día, ver `_cruzar_caja`).
    - Transferencias: cartola Itaú subida, o si no hay, los correos de aviso de
      banco ya guardados en `transferencias_banco` (motor de
      conciliacion_transferencias, con ambigüedad declarada).
    - Agrega cruzar_imed() con los movimientos Imed del upload.
    - Solo se comparan los medios que tienen fuente externa: un medio sin
      archivo no "falta", simplemente no se auditó.
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
        self.filas_recepcion: list[dict] = []
        self.caja_medilink: list[dict] = []
        self.advertencias: list[str] = []
        self.resumen_caja: dict = {}
        # medio → etiqueta de la fuente externa usada (solo medios auditados)
        self.fuentes_cargadas: dict[str, str] = {}
        if self.movs_transferencia:
            self.fuentes_cargadas["TRANSFERENCIA"] = "Cartola Itaú"
        if self.movs_efectivo:
            self.fuentes_cargadas["EFECTIVO"] = "BancoEstado"
        if self.movs_tb_debito:
            self.fuentes_cargadas["TRANSBANK_DEBITO"] = "Transbank"
        if self.movs_tb_credito:
            self.fuentes_cargadas["TRANSBANK_CREDITO"] = "Transbank"
        if self.movs_imed:
            self.fuentes_cargadas["IMED"] = "Imed"

    @property
    def _d_desde(self) -> date:
        return self.desde or date.today().replace(day=1)

    @property
    def _d_hasta(self) -> date:
        return self.hasta or date.today()

    def cargar_datos_web(self):
        """Carga recepción y caja Medilink (ambas locales). Externos ya en __init__."""
        self.filas_recepcion = _leer_pagos_cmc(self._d_desde, self._d_hasta)
        self.pagos_recepcion = _pagos_cmc_a_pagos(self.filas_recepcion)
        self.caja_medilink = _leer_caja_medilink(self._d_desde - timedelta(days=1),
                                                 self._d_hasta + timedelta(days=1))
        # pagos_medilink solo alimenta la columna "Medilink" del cuadre diario.
        self.pagos_medilink = [
            Pago(fuente="MEDILINK", fecha=date.fromisoformat(c["fecha"]), paciente=c["paciente"],
                 monto=float(c["monto"]), medio="CAJA")
            for c in self.caja_medilink if self._d_desde.isoformat() <= c["fecha"] <= self._d_hasta.isoformat()
        ]
        sin_medio = [p for p in self.pagos_recepcion if p.medio == "SIN_MEDIO"]
        if sin_medio:
            self.advertencias.append(
                f"{len(sin_medio)} cobro(s) de recepción sin medio de pago "
                f"(${sum(p.monto for p in sin_medio):,.0f}) — no entran a ningún cruce por medio.".replace(",", "."))
        log.info("AuditorWeb.cargar_datos_web: recepcion=%d caja=%d transf=%d efvo=%d tbd=%d tbc=%d imed=%d",
                 len(self.pagos_recepcion), len(self.caja_medilink), len(self.movs_transferencia),
                 len(self.movs_efectivo), len(self.movs_tb_debito), len(self.movs_tb_credito),
                 len(self.movs_imed))

    def cruzar_recepcion_medilink(self):
        """Reemplaza el cruce base (por pago y con medio): ver `_cruzar_caja`."""
        hasta_sync, adv = _hasta_sync_caja()
        self.advertencias.extend(adv)
        if hasta_sync and hasta_sync < self._d_hasta:
            self.advertencias.append(
                f"Caja Medilink sincronizada hasta el {hasta_sync.strftime('%d/%m')}: "
                f"los días posteriores no se cruzan contra la caja.")
        recep = [r for r in self.filas_recepcion]
        caja = self.caja_medilink
        hallazgos, self.resumen_caja = _cruzar_caja(recep, caja, hasta_sync)
        d0, d1 = self._d_desde, self._d_hasta
        self.hallazgos.extend(h for h in hallazgos if h.fecha is None or d0 <= h.fecha <= d1)

    def cruzar_transferencias_correos(self):
        """Sin cartola Itaú: usa los correos de aviso de banco ya parseados.

        Cobertura PARCIAL (no todos los bancos avisan por correo): un registro
        sin correo no prueba nada → BAJA. Un correo sin registro sí es plata
        que entró y nadie anotó → ALTA. Los empates de monto/fecha se reportan
        como revisión manual, nunca se asignan a ciegas."""
        try:
            import conciliacion_transferencias as ct
            r = ct.conciliar(self._d_desde.isoformat(), self._d_hasta.isoformat())
        except Exception as e:
            log.warning("cruzar_transferencias_correos: %s", e)
            self.advertencias.append("No se pudieron leer los correos de banco para cruzar transferencias.")
            return
        t = r["totales"]
        self.fuentes_cargadas["TRANSFERENCIA"] = "Correos de banco (parcial)"
        self.transf_correos_total = (t["conciliado_monto"] + t["correo_sin_registro_monto"]
                                     + t["ambiguo_monto_correos"])
        self.transf_correos_totales = t
        for e in r["correo_sin_registro"]:
            self._agregar(Hallazgo(
                fecha=date.fromisoformat(e["fecha"]), paciente=e.get("nombre_transfiere") or "—",
                monto_interno=None, monto_externo=float(e["monto"]),
                fuente_interna="RECEPCION", fuente_externa="CORREO_BANCO", medio="TRANSFERENCIA",
                tipo="SIN_RESPALDO_INTERNO",
                comentario=(f"Llegó aviso de transferencia ${e['monto']:,.0f} de {e.get('nombre_transfiere') or '—'} "
                            f"y no hay pago registrado que calce").replace(",", "."),
                prioridad="ALTA",
            ))
        for p in r["registrado_sin_correo"]:
            self._agregar(Hallazgo(
                fecha=date.fromisoformat(p["fecha"]), paciente=p.get("paciente_nombre") or "—",
                monto_interno=float(p.get("copago") or 0), monto_externo=None,
                fuente_interna="RECEPCION", fuente_externa="CORREO_BANCO", medio="TRANSFERENCIA",
                tipo="SIN_RESPALDO_BANCARIO",
                comentario="Sin aviso por correo (normal en bancos que no avisan) — confirmar con cartola si importa",
                prioridad="BAJA",
            ))
        for g in r["ambiguos"]:
            fechas = sorted({p["fecha"] for p in g["pagos"]} | {c["fecha"] for c in g["correos"]})
            self._agregar(Hallazgo(
                fecha=date.fromisoformat(fechas[0]),
                paciente=", ".join(p["paciente"] for p in g["pagos"])[:120],
                monto_interno=float(g["monto"] * len(g["pagos"])),
                monto_externo=float(g["monto"] * len(g["correos"])),
                fuente_interna="RECEPCION", fuente_externa="CORREO_BANCO", medio="TRANSFERENCIA",
                tipo="REQUIERE_REVISION_MANUAL", comentario=g["nota"],
                prioridad="BAJA",
            ))
        if t["registrado_sin_correo_n"]:
            self.advertencias.append(
                f"Transferencias cruzadas contra correos de banco: {t['registrado_sin_correo_n']} registro(s) "
                f"sin correo quedan en prioridad BAJA porque no todos los bancos avisan. "
                f"Sube la cartola Itaú para auditarlas todas.")

    def auditar_web(self):
        """Ejecuta todos los cruces incluyendo la capa Imed."""
        self.cargar_datos_web()
        self.cruzar_recepcion_medilink()
        if self.movs_transferencia:
            self.cruzar_transferencias()
        else:
            self.cruzar_transferencias_correos()
        if self.movs_efectivo:
            self.cruzar_efectivo()
        if self.movs_tb_debito:
            self.cruzar_transbank("TRANSBANK_DEBITO", self.movs_tb_debito)
        if self.movs_tb_credito:
            self.cruzar_transbank("TRANSBANK_CREDITO", self.movs_tb_credito)
        if self.movs_imed:
            # Sin reporte Imed no hay nada contra qué comparar: antes salía un
            # "FALTANTE ALTA" por la bonificación entera del período.
            self.hallazgos.extend(_cruzar_imed(self.pagos_recepcion, self.movs_imed,
                                               self._d_desde, self._d_hasta))
        sin = [m for m in ("EFECTIVO", "TRANSBANK_DEBITO", "TRANSBANK_CREDITO", "IMED")
               if m not in self.fuentes_cargadas]
        if sin:
            self.advertencias.append("Sin archivo (no auditados): " + ", ".join(
                {"EFECTIVO": "efectivo/BancoEstado", "TRANSBANK_DEBITO": "Transbank débito",
                 "TRANSBANK_CREDITO": "Transbank crédito", "IMED": "Imed"}[m] for m in sin) + ".")

    def _totales_externos(self) -> dict:
        base = super()._totales_externos()
        if not self.movs_transferencia and hasattr(self, "transf_correos_total"):
            base["TRANSFERENCIA"] = float(self.transf_correos_total)
        base["IMED"] = sum(m.monto for m in self.movs_imed if m.tipo == "CREDITO")
        return base

    def _totales_internos_imed(self) -> float:
        """Bonificación Fonasa esperada del período, por arancel N3 (la misma
        cuenta que usa el hallazgo IMED; antes sumaba la columna
        `bonificacion`, que recepción ya no llena → $0 siempre)."""
        return float(sum(r["bonif_arancel"] for r in self.filas_recepcion
                         if r["copago"] > 0 or r["metodo_pago"]))

    def _cuadre_diario(self) -> list[dict]:
        """Cuadre por día SOLO con los medios que tienen fuente externa. Antes
        restaba toda la recepción contra los archivos subidos: sin Transbank
        subido, cada día con débito salía NO CUADRA."""
        medios = [m for m in ("TRANSFERENCIA", "EFECTIVO", "TRANSBANK_DEBITO", "TRANSBANK_CREDITO")
                  if m in self.fuentes_cargadas]
        if "TRANSFERENCIA" in medios and not self.movs_transferencia:
            # Con correos (cobertura parcial) no hay cuadre diario honesto de
            # transferencias: se excluyen del cuadre y quedan en hallazgos.
            medios.remove("TRANSFERENCIA")
        fechas = sorted({p.fecha for p in self.pagos_recepcion if p.fecha}
                        | {p.fecha for p in self.pagos_medilink if p.fecha})
        filas = []
        for fec in fechas:
            rec = sum(p.monto for p in self.pagos_recepcion if p.fecha == fec and p.medio in medios)
            med = sum(p.monto for p in self.pagos_medilink if p.fecha == fec)
            transf = sum(m.monto for m in self.movs_transferencia if m.fecha == fec and m.tipo == "CREDITO")
            efvo = sum(m.monto for m in self.movs_efectivo if m.fecha == fec and m.tipo == "CREDITO")
            tb = (sum(m.monto for m in self.movs_tb_debito if m.fecha == fec)
                  + sum(m.monto for m in self.movs_tb_credito if m.fecha == fec))
            if not medios:
                estado, dif = "SIN FUENTE", 0.0
            else:
                dif = rec - (transf + efvo + tb)
                if abs(dif) < TOLERANCIA_MONTO:
                    estado = "CUADRA"
                elif rec and abs(dif) < rec * 0.05:
                    estado = "CUADRA CON OBSERVACIONES"
                else:
                    estado = "NO CUADRA"
            filas.append({
                "fecha": fec.strftime("%d/%m/%Y"), "recepcion": rec, "medilink": med,
                "itau": transf, "banco_estado": efvo, "transbank": tb,
                "diferencia": dif, "estado": estado,
            })
        return filas


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
        registrado = imed_esperado if m == "IMED" else totales_int.get(m, 0.0)
        con_fuente = m in auditor.fuentes_cargadas
        respaldado = totales_ext.get(m, 0.0) if con_fuente else 0.0
        diferencia = registrado - respaldado if con_fuente else 0.0
        fuentes.append({
            "medio":      m,
            "registrado": registrado,
            "respaldado": respaldado,
            "diferencia": diferencia,
            "con_fuente": con_fuente,
            "fuente":     auditor.fuentes_cargadas.get(m, ""),
            "parcial":    auditor.fuentes_cargadas.get(m, "").endswith("(parcial)"),
            "ok":         con_fuente and (abs(diferencia) <= TOLERANCIA_MONTO or registrado == 0),
        })

    # Totales solo sobre medios auditados con fuente COMPLETA (los correos de
    # banco son parciales: su "diferencia" no es plata faltante).
    completos = [f for f in fuentes if f["con_fuente"] and not f["parcial"] and f["medio"] != "IMED"]
    total_registrado = sum(f["registrado"] for f in completos)
    total_respaldado = sum(f["respaldado"] for f in completos)
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

    pct_conciliado = None
    if total_registrado > 0:
        pct_conciliado = min(100.0, round(total_respaldado / total_registrado * 100, 1))

    rc = auditor.resumen_caja or {}
    total_recep = sum(r["copago"] + r["bonif_arancel"] for r in auditor.filas_recepcion
                      if r["copago"] > 0 or r["metodo_pago"])
    total_caja = sum(p.monto for p in auditor.pagos_medilink)

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
        "caja": {
            "recepcion_esperado": total_recep,
            "caja_medilink":      total_caja,
            "diferencia":         total_recep - total_caja,
            "pacientes_dia_recepcion": rc.get("grupos_recepcion", 0),
            "pacientes_dia_caja":      rc.get("grupos_caja", 0),
            "emparejados":        rc.get("emparejados", 0),
            "cuadran":            rc.get("cuadran", 0),
        },
        "fuentes":           fuentes,
        "imed_esperado":     imed_esperado,
        "imed_recibido":     totales_ext.get("IMED", 0.0),
        "advertencias":      auditor.advertencias,
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
        # Comparable con la caja: copago + bonificación Fonasa por arancel.
        "total_recepcion_esperado_caja": sum(
            r["copago"] + r["bonif_arancel"] for r in auditor.filas_recepcion
            if r["copago"] > 0 or r["metodo_pago"]),
        "total_medilink":  sum(p.monto for p in auditor.pagos_medilink),
        "advertencias":    auditor.advertencias,
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
