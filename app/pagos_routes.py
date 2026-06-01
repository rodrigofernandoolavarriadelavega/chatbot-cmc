"""
Router /alma/api/pagos — Módulo de Pagos Alma (Fase 1).

Reemplaza el Google Sheet "pacientes diarios".
Cada registro = un pago procesado en recepción (presencial, teléfono o chat).

Principio rector: todo pre-llenado como SUGERENCIA, todo editable.
La recepcionista acepta rápido lo normal y corrige lo que varíe
(con bono los copagos bajan y varían).

Auth: misma que agenda_routes (_require_admin_dep).
DB: sessions.db (tabla pagos_cmc), misma que todo el stack.
"""
import logging
from datetime import datetime, date
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request, Query, Cookie
from fastapi.responses import JSONResponse

log = logging.getLogger("pagos_routes")
_CHILE_TZ = ZoneInfo("America/Santiago")

router = APIRouter(prefix="/alma/api/pagos", tags=["pagos"])

# ── Arancel Fonasa MLE Nivel 3 (CMC) ─────────────────────────────────────────
# Solo estas áreas aceptan bono Imed (Fonasa MLE).
# El resto es particular.
# Fuente: Arancel MLE 2024/2025, Nivel 3.
_ARANCEL_N3: dict[str, dict] = {
    "Medicina General": {
        "codigo": "01 01 001",
        "total":  15_130,
        "bonif":   7_880,
        "copago":  7_250,
    },
    "Medicina Familiar": {
        "codigo": "01 01 001",
        "total":  15_130,
        "bonif":   7_880,
        "copago":  7_250,
    },
    "Kinesiología": {
        "codigo_sesion": "06 01 105",
        "total_sesion":  11_390,
        "bonif_sesion":   7_830,
        "copago_sesion":  3_560,
        "codigo_eval":   "06 01 101",
        "total_eval":     3_680,
        "bonif_eval":     2_530,
        "copago_eval":    1_150,
        # default = sesión
        "codigo": "06 01 105",
        "total":  11_390,
        "bonif":   7_830,
        "copago":  3_560,
    },
    "Nutrición": {
        "codigo": "26 02 001",
        "total":   9_540,
        "bonif":   4_770,
        "copago":  4_770,
    },
    "Psicología Adulto": {
        "codigo": "09 02 001",
        "total":  20_980,
        "bonif":  14_420,
        "copago":  6_560,
    },
    "Psicología Infantil": {
        "codigo": "09 02 001",
        "total":  20_980,
        "bonif":  14_420,
        "copago":  6_560,
    },
    "Psicología": {
        "codigo": "09 02 001",
        "total":  20_980,
        "bonif":  14_420,
        "copago":  6_560,
    },
}

# Precio PARTICULAR por id_profesional (importado de agenda_routes para no duplicar)
# Se importa lazy para evitar circular imports.
def _precio_particular(id_profesional: int) -> int | None:
    try:
        from agenda_routes import _CAPI_VALUE_BY_PROF
        v = _CAPI_VALUE_BY_PROF.get(id_profesional)
        return int(v) if v is not None else None
    except Exception:
        return None


# ── Auth (mismo patrón que agenda_routes) ────────────────────────────────────

def _require_admin_dep(request: Request,
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


# ── DDL helper — llamado en lifespan de main.py (o lazy en primera query) ────

def ensure_pagos_table() -> None:
    """Crea la tabla pagos_cmc si no existe. Idempotente."""
    from session import _conn
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pagos_cmc (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                fecha               TEXT NOT NULL,
                hora                TEXT NOT NULL,
                paciente_nombre     TEXT NOT NULL DEFAULT '',
                rut                 TEXT DEFAULT '',
                id_profesional      INTEGER,
                profesional         TEXT DEFAULT '',
                area                TEXT DEFAULT '',
                prevision           TEXT DEFAULT 'particular',
                copago              INTEGER DEFAULT 0,
                bonificacion        INTEGER DEFAULT 0,
                metodo_pago         TEXT DEFAULT 'efectivo',
                folio               TEXT DEFAULT '',
                codigo_transferencia TEXT DEFAULT '',
                procedimiento       TEXT DEFAULT '',
                origen              TEXT DEFAULT 'presencial',
                id_cita             TEXT DEFAULT '',
                creado_por          TEXT DEFAULT 'recepcion',
                created_at          TEXT DEFAULT (datetime('now')),
                updated_at          TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pagos_fecha ON pagos_cmc(fecha)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pagos_rut ON pagos_cmc(rut)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pagos_prof ON pagos_cmc(id_profesional)"
        )
        conn.commit()


# ── Lógica de sugerencia ──────────────────────────────────────────────────────

def _sugerir_copago(area: str, prevision: str, id_profesional: int | None) -> dict:
    """
    Retorna copago_sugerido y bonif_sugerida según área y previsión.
    Siempre como sugerencia — el frontend los muestra editables.
    """
    if prevision == "fonasa" and area in _ARANCEL_N3:
        arancel = _ARANCEL_N3[area]
        return {
            "copago_sugerido":  arancel["copago"],
            "bonif_sugerida":   arancel["bonif"],
            "total_arancel":    arancel["total"],
            "codigo_fonasa":    arancel.get("codigo", ""),
            "fuente":           "fonasa_n3",
        }
    # Particular: copago = precio del profesional, sin bonificación
    precio = _precio_particular(id_profesional) if id_profesional else None
    return {
        "copago_sugerido":  precio or 0,
        "bonif_sugerida":   0,
        "total_arancel":    precio or 0,
        "codigo_fonasa":    "",
        "fuente":           "particular",
    }


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/arancel")
async def get_arancel(
    token: str | None = Query(None),
    cmc_session: str | None = Cookie(None),
    request: Request = None,
):
    """
    Retorna la tabla de arancel Fonasa N3 y la lista de areas que aceptan bono.
    Util para el frontend (pre-llenar selectores).
    """
    _require_admin_dep(request, token=token, cmc_session=cmc_session)
    return {
        "nivel": 3,
        "areas_fonasa": list(_ARANCEL_N3.keys()),
        "aranceles": _ARANCEL_N3,
    }


@router.get("/sugerencia")
async def get_sugerencia(
    rut: str | None = Query(None),
    id_cita: str | None = Query(None),
    token: str | None = Query(None),
    cmc_session: str | None = Cookie(None),
    request: Request = None,
):
    """
    Devuelve sugerencias pre-llenadas para el formulario de pago.
    Busca en Medilink con rut o id_cita para obtener paciente y cita.
    TODO es sugerencia — el frontend lo muestra editable.
    """
    _require_admin_dep(request, token=token, cmc_session=cmc_session)
    ensure_pagos_table()

    from medilink import PROFESIONALES
    now_cl = datetime.now(_CHILE_TZ)

    sugerencia: dict = {
        "fecha":         now_cl.strftime("%Y-%m-%d"),
        "hora":          now_cl.strftime("%H:%M"),
        "paciente_nombre": "",
        "rut":           rut or "",
        "id_profesional": None,
        "profesional":   "",
        "area":          "",
        "prevision":     "particular",
        "copago_sugerido": 0,
        "bonif_sugerida":  0,
        "total_arancel":   0,
        "codigo_fonasa":   "",
        "fuente":          "sin_datos",
        "id_cita":        id_cita or "",
        "origen":         "presencial",
        "metodo_pago":    "efectivo",
    }

    # Si viene id_cita: buscar datos de la cita en cache local
    if id_cita:
        try:
            from session import _conn
            with _conn() as conn:
                row = conn.execute(
                    """SELECT profesional, especialidad, id_cita,
                              phone, paciente_nombre
                       FROM citas_bot
                       WHERE id_cita = ? LIMIT 1""",
                    (id_cita,)
                ).fetchone()
            if row:
                # Buscar id_profesional por nombre en PROFESIONALES
                prof_nombre = row["profesional"] or ""
                id_prof_match = None
                for pid, pinfo in PROFESIONALES.items():
                    if pinfo["nombre"] == prof_nombre:
                        id_prof_match = pid
                        break
                sugerencia.update({
                    "id_profesional": id_prof_match,
                    "profesional":    prof_nombre,
                    "area":           row["especialidad"] or "",
                    "id_cita":        id_cita,
                    "paciente_nombre": row["paciente_nombre"] or "",
                })
        except Exception as e:
            log.warning("get_sugerencia: error buscando cita cache: %s", e)

    # Si viene rut: buscar paciente en Medilink
    if rut:
        try:
            from medilink import buscar_paciente
            paciente = await buscar_paciente(rut.strip())
            if paciente:
                nombre = (
                    f"{paciente.get('nombre', '')} {paciente.get('apellidos', '')}".strip()
                )
                sugerencia["paciente_nombre"] = nombre
                sugerencia["rut"] = paciente.get("rut", rut)
                # Previsión desde perfil local si existe
                try:
                    from session import _conn
                    with _conn() as conn:
                        prof_row = conn.execute(
                            "SELECT prevision FROM contact_profiles WHERE rut = ? LIMIT 1",
                            (rut.strip(),)
                        ).fetchone()
                    if prof_row and prof_row["prevision"]:
                        sugerencia["prevision"] = prof_row["prevision"]
                except Exception:
                    pass
        except Exception as e:
            log.warning("get_sugerencia: error buscando paciente Medilink: %s", e)

    # Calcular copago/bonif sugeridos con los datos que tenemos
    area = sugerencia["area"]
    prevision = sugerencia["prevision"]
    id_prof = sugerencia["id_profesional"]
    if area or id_prof:
        montos = _sugerir_copago(area, prevision, id_prof)
        sugerencia.update(montos)

    # Info del profesional (especialidad si area vacía)
    if sugerencia["id_profesional"] and not sugerencia["area"]:
        pinfo = PROFESIONALES.get(sugerencia["id_profesional"], {})
        sugerencia["area"] = pinfo.get("especialidad", "")

    return sugerencia


@router.post("")
async def post_pago(
    request: Request,
    token: str | None = Query(None),
    cmc_session: str | None = Cookie(None),
):
    """
    Crea un registro de pago. Acepta cualquier valor — el formulario ya
    permitió editar las sugerencias.

    Body JSON: ver campos tabla pagos_cmc.
    """
    _require_admin_dep(request, token=token, cmc_session=cmc_session)
    ensure_pagos_table()

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Body JSON inválido")

    # Validaciones mínimas
    paciente_nombre = (body.get("paciente_nombre") or "").strip()
    if not paciente_nombre:
        raise HTTPException(400, "paciente_nombre es requerido")

    now_cl = datetime.now(_CHILE_TZ)
    fecha = (body.get("fecha") or now_cl.strftime("%Y-%m-%d")).strip()
    hora  = (body.get("hora")  or now_cl.strftime("%H:%M")).strip()

    # Validar fecha
    try:
        datetime.strptime(fecha, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(400, "fecha debe ser YYYY-MM-DD")

    prevision = (body.get("prevision") or "particular").lower()
    if prevision not in ("fonasa", "particular"):
        prevision = "particular"

    metodo_pago = (body.get("metodo_pago") or "efectivo").lower()
    if metodo_pago not in ("efectivo", "transferencia", "debito", "credito"):
        metodo_pago = "efectivo"

    origen = (body.get("origen") or "presencial").lower()
    if origen not in ("chat", "telefono", "presencial"):
        origen = "presencial"

    id_prof_raw = body.get("id_profesional")
    id_profesional = int(id_prof_raw) if id_prof_raw is not None else None

    copago      = int(body.get("copago")      or 0)
    bonificacion = int(body.get("bonificacion") or 0)

    from session import _conn
    with _conn() as conn:
        cur = conn.execute(
            """INSERT INTO pagos_cmc
               (fecha, hora, paciente_nombre, rut, id_profesional, profesional,
                area, prevision, copago, bonificacion, metodo_pago, folio,
                codigo_transferencia, procedimiento, origen, id_cita,
                creado_por, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))""",
            (
                fecha,
                hora,
                paciente_nombre,
                (body.get("rut") or "").strip(),
                id_profesional,
                (body.get("profesional") or "").strip(),
                (body.get("area") or "").strip(),
                prevision,
                copago,
                bonificacion,
                metodo_pago,
                (body.get("folio") or "").strip(),
                (body.get("codigo_transferencia") or "").strip(),
                (body.get("procedimiento") or "").strip(),
                origen,
                (body.get("id_cita") or "").strip(),
                (body.get("creado_por") or "recepcion").strip(),
            )
        )
        new_id = cur.lastrowid
        conn.commit()

    log.info(
        "pagos_routes.post_pago: id=%d paciente=%s fecha=%s %s prof=%s "
        "prevision=%s copago=%d bonif=%d metodo=%s",
        new_id, paciente_nombre, fecha, hora,
        body.get("profesional", ""), prevision, copago, bonificacion, metodo_pago,
    )

    return {"ok": True, "id": new_id}


@router.get("")
async def get_pagos(
    fecha: str | None = Query(None, description="YYYY-MM-DD (omitir = hoy)"),
    fecha_desde: str | None = Query(None, description="YYYY-MM-DD rango inicio"),
    fecha_hasta: str | None = Query(None, description="YYYY-MM-DD rango fin"),
    token: str | None = Query(None),
    cmc_session: str | None = Cookie(None),
    request: Request = None,
):
    """
    Lista registros de pago.
    - Sin parámetros: retorna el día de hoy (hora Chile).
    - fecha=YYYY-MM-DD: ese día.
    - fecha_desde + fecha_hasta: rango.
    """
    _require_admin_dep(request, token=token, cmc_session=cmc_session)
    ensure_pagos_table()

    now_cl = datetime.now(_CHILE_TZ)

    # Determinar rango
    if fecha_desde and fecha_hasta:
        try:
            datetime.strptime(fecha_desde, "%Y-%m-%d")
            datetime.strptime(fecha_hasta, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(400, "Fechas deben ser YYYY-MM-DD")
        d_desde, d_hasta = fecha_desde, fecha_hasta
    else:
        d = fecha or now_cl.strftime("%Y-%m-%d")
        try:
            datetime.strptime(d, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(400, "fecha debe ser YYYY-MM-DD")
        d_desde = d_hasta = d

    from session import _conn
    with _conn() as conn:
        rows = conn.execute(
            """SELECT id, fecha, hora, paciente_nombre, rut,
                      id_profesional, profesional, area, prevision,
                      copago, bonificacion, metodo_pago, folio,
                      codigo_transferencia, procedimiento, origen,
                      id_cita, creado_por, created_at, updated_at
               FROM pagos_cmc
               WHERE fecha BETWEEN ? AND ?
               ORDER BY fecha DESC, hora DESC""",
            (d_desde, d_hasta)
        ).fetchall()

    pagos = [dict(r) for r in rows]
    total_copago     = sum(p["copago"]       for p in pagos)
    total_bonif      = sum(p["bonificacion"] for p in pagos)
    total_recaudado  = total_copago + total_bonif

    return {
        "pagos":            pagos,
        "total":            len(pagos),
        "total_copago":     total_copago,
        "total_bonif":      total_bonif,
        "total_recaudado":  total_recaudado,
        "fecha_desde":      d_desde,
        "fecha_hasta":      d_hasta,
    }


@router.patch("/{pago_id}")
async def patch_pago(
    pago_id: int,
    request: Request,
    token: str | None = Query(None),
    cmc_session: str | None = Cookie(None),
):
    """
    Edita un registro existente. Solo los campos enviados en el body se actualizan.
    Útil para corregir errores después de guardar.
    """
    _require_admin_dep(request, token=token, cmc_session=cmc_session)
    ensure_pagos_table()

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Body JSON inválido")

    if not body:
        raise HTTPException(400, "Body vacío")

    # Campos editables (lista blanca)
    _EDITABLE = {
        "fecha", "hora", "paciente_nombre", "rut", "id_profesional",
        "profesional", "area", "prevision", "copago", "bonificacion",
        "metodo_pago", "folio", "codigo_transferencia", "procedimiento",
        "origen", "id_cita", "creado_por",
    }
    campos = {k: v for k, v in body.items() if k in _EDITABLE}
    if not campos:
        raise HTTPException(400, "Sin campos editables en el body")

    # Validaciones puntuales
    if "prevision" in campos and campos["prevision"] not in ("fonasa", "particular"):
        raise HTTPException(400, "prevision debe ser 'fonasa' o 'particular'")
    if "metodo_pago" in campos and campos["metodo_pago"] not in (
        "efectivo", "transferencia", "debito", "credito"
    ):
        raise HTTPException(400, "metodo_pago inválido")
    if "fecha" in campos:
        try:
            datetime.strptime(campos["fecha"], "%Y-%m-%d")
        except ValueError:
            raise HTTPException(400, "fecha debe ser YYYY-MM-DD")

    set_clause = ", ".join(f"{k} = ?" for k in campos)
    values = list(campos.values()) + [pago_id]

    from session import _conn
    with _conn() as conn:
        # Verificar que el registro existe
        exists = conn.execute(
            "SELECT id FROM pagos_cmc WHERE id = ?", (pago_id,)
        ).fetchone()
        if not exists:
            raise HTTPException(404, f"Pago {pago_id} no encontrado")

        conn.execute(
            f"UPDATE pagos_cmc SET {set_clause}, updated_at = datetime('now') WHERE id = ?",
            values
        )
        conn.commit()

    log.info("pagos_routes.patch_pago: id=%d campos=%s", pago_id, list(campos.keys()))
    return {"ok": True, "id": pago_id, "updated": list(campos.keys())}


@router.delete("/{pago_id}")
async def delete_pago(
    pago_id: int,
    request: Request,
    token: str | None = Query(None),
    cmc_session: str | None = Cookie(None),
):
    """Elimina un registro de pago (errores de entrada / duplicados)."""
    _require_admin_dep(request, token=token, cmc_session=cmc_session)
    ensure_pagos_table()

    from session import _conn
    with _conn() as conn:
        exists = conn.execute(
            "SELECT id FROM pagos_cmc WHERE id = ?", (pago_id,)
        ).fetchone()
        if not exists:
            raise HTTPException(404, f"Pago {pago_id} no encontrado")
        conn.execute("DELETE FROM pagos_cmc WHERE id = ?", (pago_id,))
        conn.commit()

    log.info("pagos_routes.delete_pago: id=%d", pago_id)
    return {"ok": True, "id": pago_id}
