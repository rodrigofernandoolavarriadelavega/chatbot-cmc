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

import io
from fastapi import APIRouter, Depends, HTTPException, Request, Query, Cookie
from fastapi.responses import JSONResponse, StreamingResponse

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
        # Columnas añadidas post-creación (idempotente)
        for col_ddl in [
            "ALTER TABLE pagos_cmc ADD COLUMN tipo_bono TEXT DEFAULT ''",
        ]:
            try:
                conn.execute(col_ddl)
            except Exception:
                pass  # columna ya existe
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


async def _buscar_cita_paciente_fecha(rut: str, fecha: str) -> dict | None:
    """
    Busca en Medilink la(s) cita(s) del paciente (por RUT) para una fecha exacta.
    Retorna la primera cita encontrada como dict con id, id_profesional, profesional, area,
    prestacion, o None si no hay citas ese día.

    Consultas HTTP: 1 (citas) + 1 opcional (atenciones/detalles si hay id_atencion) — 429-safe.

    La prestación viene de /atenciones/{id_atencion}/detalles → nombre_prestacion.
    Este endpoint solo tiene datos cuando la atención ya fue cerrada en Medilink;
    si aún no está cerrada, prestacion queda vacía (editable en el formulario).
    """
    try:
        from medilink import (
            _get_shared_client, _q, _safe_json, HEADERS,
            PROFESIONALES, _rut_safe,
        )
        from config import MEDILINK_BASE_URL

        # RUT canónico: sin puntos, con guión, mayúsculas
        rut_clean = "".join(c for c in rut.upper() if c.isalnum())
        if len(rut_clean) > 1:
            rut_fmt = rut_clean[:-1] + "-" + rut_clean[-1]
        else:
            rut_fmt = rut_clean

        params = {
            "rut":              {"eq": rut_fmt},
            "fecha":            {"eq": fecha},
            "estado_anulacion": {"eq": 0},
        }
        client = _get_shared_client()
        r = await client.get(
            f"{MEDILINK_BASE_URL}/citas",
            params={"q": _q(params)},
            headers=HEADERS,
            timeout=10,
        )
        if r.status_code != 200:
            log.warning(
                "_buscar_cita_paciente_fecha rut=%s fecha=%s → HTTP %d",
                _rut_safe(rut), fecha, r.status_code,
            )
            return None
        data = _safe_json(r).get("data", [])
        if not data:
            return None
        # Ordenar por hora_inicio y tomar la primera
        data.sort(key=lambda c: c.get("hora_inicio", ""))
        cita = data[0]
        id_prof = cita.get("id_profesional")
        prof_info = PROFESIONALES.get(id_prof, {}) if id_prof else {}

        # ── Prestación desde /atenciones/{id_atencion}/detalles ──────────────
        # La cita lleva id_atencion cuando la atención fue creada en Medilink.
        # El detalle tiene nombre_prestacion cuando la atención está cerrada.
        # Si no está cerrada aún, devolvemos prestacion vacía (editable).
        prestacion = ""
        id_aten = cita.get("id_atencion")
        if id_aten:
            try:
                rd = await client.get(
                    f"{MEDILINK_BASE_URL}/atenciones/{id_aten}/detalles",
                    headers=HEADERS,
                    timeout=8,
                )
                if rd.status_code == 200:
                    detalles = _safe_json(rd).get("data", [])
                    # Recopilar nombres únicos de prestaciones realizadas
                    nombres = []
                    for det in detalles:
                        n = (det.get("nombre_prestacion") or "").strip()
                        if n and n not in nombres:
                            nombres.append(n)
                    prestacion = " / ".join(nombres)
                    if prestacion:
                        log.info(
                            "_buscar_cita_paciente_fecha: prestacion desde detalles "
                            "id_aten=%s → %r", id_aten, prestacion
                        )
            except Exception as e_det:
                log.debug("_buscar_cita_paciente_fecha detalles error id_aten=%s: %s", id_aten, e_det)

        return {
            "id_cita":        str(cita["id"]),
            "id_profesional": id_prof,
            "profesional":    cita.get("nombre_profesional", "") or prof_info.get("nombre", ""),
            "area":           prof_info.get("especialidad", ""),
            "prestacion":     prestacion,
        }
    except Exception as e:
        log.warning("_buscar_cita_paciente_fecha error: %s", e)
        return None


@router.get("/sugerencia")
async def get_sugerencia(
    rut: str | None = Query(None),
    id_cita: str | None = Query(None),
    fecha: str | None = Query(None, description="YYYY-MM-DD; por defecto hoy"),
    token: str | None = Query(None),
    cmc_session: str | None = Cookie(None),
    request: Request = None,
):
    """
    Devuelve sugerencias pre-llenadas para el formulario de pago.
    Busca en Medilink con rut o id_cita para obtener paciente y cita.
    Si no viene id_cita pero sí rut+fecha, consulta Medilink para auto-rellenar
    id_cita, profesional y área a partir de la cita del paciente ese día.
    Todo es sugerencia — el frontend lo muestra editable.
    """
    _require_admin_dep(request, token=token, cmc_session=cmc_session)
    ensure_pagos_table()

    from medilink import PROFESIONALES
    now_cl = datetime.now(_CHILE_TZ)

    # Fecha del registro (normalizada a YYYY-MM-DD)
    fecha_registro = now_cl.strftime("%Y-%m-%d")
    if fecha:
        try:
            datetime.strptime(fecha, "%Y-%m-%d")
            fecha_registro = fecha
        except ValueError:
            pass  # si viene malformada, usar hoy

    sugerencia: dict = {
        "fecha":           fecha_registro,
        "hora":            now_cl.strftime("%H:%M"),
        "paciente_nombre": "",
        "rut":             rut or "",
        "id_profesional":  None,
        "profesional":     "",
        "area":            "",
        "prestacion":      "",
        "prevision":       "particular",
        "copago_sugerido": 0,
        "bonif_sugerida":  0,
        "total_arancel":   0,
        "codigo_fonasa":   "",
        "fuente":          "sin_datos",
        "id_cita":         id_cita or "",
        "origen":          "presencial",
        "metodo_pago":     "efectivo",
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
                    "id_profesional":  id_prof_match,
                    "profesional":     prof_nombre,
                    "area":            row["especialidad"] or "",
                    "id_cita":         id_cita,
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

        # Si no hay id_cita todavía, buscar cita del paciente en esa fecha
        if not sugerencia["id_cita"] and rut:
            cita_dia = await _buscar_cita_paciente_fecha(rut.strip(), fecha_registro)
            if cita_dia:
                sugerencia.update({
                    "id_cita":        cita_dia["id_cita"],
                    "id_profesional": cita_dia["id_profesional"],
                    "profesional":    cita_dia["profesional"],
                    "area":           cita_dia["area"],
                    "prestacion":     cita_dia.get("prestacion", ""),
                })
                log.info(
                    "get_sugerencia: cita auto-rellenada id=%s prof=%s fecha=%s",
                    cita_dia["id_cita"], cita_dia["profesional"], fecha_registro,
                )

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
                codigo_transferencia, tipo_bono, procedimiento, origen, id_cita,
                creado_por, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))""",
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
                (body.get("tipo_bono") or "").strip(),
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
                      codigo_transferencia, tipo_bono, procedimiento, origen,
                      id_cita, creado_por, created_at, updated_at
               FROM pagos_cmc
               WHERE fecha BETWEEN ? AND ?
               ORDER BY fecha DESC, hora DESC""",
            (d_desde, d_hasta)
        ).fetchall()

    pagos = [dict(r) for r in rows]
    total_copago = sum(p["copago"] for p in pagos)

    # Bonif calculada desde arancel N3 por area (ya no se guarda en caja)
    def _bonif_para(area: str, prevision: str) -> int:
        if prevision == "fonasa" and area in _ARANCEL_N3:
            return _ARANCEL_N3[area].get("bonif", 0)
        return 0

    total_bonif = sum(
        _bonif_para(p.get("area", ""), p.get("prevision", ""))
        for p in pagos
    )
    # Total recaudado = solo lo que entro a caja (copago)
    total_recaudado = total_copago

    # Totales por metodo de pago (para sección Caja)
    totales_por_metodo: dict[str, int] = {
        "efectivo":      0,
        "transferencia": 0,
        "debito":        0,
        "credito":       0,
    }
    for p in pagos:
        metodo = (p.get("metodo_pago") or "efectivo").lower()
        if metodo in totales_por_metodo:
            totales_por_metodo[metodo] += p.get("copago", 0) or 0

    return {
        "pagos":               pagos,
        "total":               len(pagos),
        "total_copago":        total_copago,
        "total_bonif":         total_bonif,       # informativo, calculado desde arancel
        "total_recaudado":     total_recaudado,
        "totales_por_metodo":  totales_por_metodo,
        "fecha_desde":         d_desde,
        "fecha_hasta":         d_hasta,
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
        "metodo_pago", "folio", "codigo_transferencia", "tipo_bono", "procedimiento",
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


@router.get("/export")
async def export_pagos_xlsx(
    fecha: str | None = Query(None, description="YYYY-MM-DD (omitir = hoy)"),
    fecha_desde: str | None = Query(None),
    fecha_hasta: str | None = Query(None),
    token: str | None = Query(None),
    cmc_session: str | None = Cookie(None),
    request: Request = None,
):
    """
    Exporta los pagos del período como .xlsx.
    Columnas: HORA, PACIENTE, NOMBRE PROFESIONAL, AREA, PREVISION,
              VALOR, METODO DE PAGO, N° FOLIO, PROCEDIMIENTO
    """
    _require_admin_dep(request, token=token, cmc_session=cmc_session)
    ensure_pagos_table()

    now_cl = datetime.now(_CHILE_TZ)

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
            """SELECT hora, paciente_nombre, profesional, area, prevision,
                      copago, bonificacion, metodo_pago, folio,
                      codigo_transferencia, tipo_bono, procedimiento
               FROM pagos_cmc
               WHERE fecha BETWEEN ? AND ?
               ORDER BY fecha ASC, hora ASC""",
            (d_desde, d_hasta)
        ).fetchall()

    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    except ImportError:
        raise HTTPException(500, "openpyxl no disponible")

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Pagos"

    # Fila de título con fecha del período
    periodo = d_desde if d_desde == d_hasta else f"{d_desde} — {d_hasta}"
    ws.merge_cells("A1:L1")
    titulo_cell = ws["A1"]
    titulo_cell.value = f"Pagos CMC — {periodo}"
    titulo_cell.font = Font(name="Calibri", bold=True, size=12, color="FFFFFF")
    titulo_cell.fill = PatternFill("solid", fgColor="0F3F68")
    titulo_cell.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 20

    # Encabezados (fila 2)
    headers = [
        "HORA", "PACIENTE", "NOMBRE PROFESIONAL", "AREA",
        "PREVISION", "PAGO", "METODO DE PAGO", "N° FOLIO",
        "COD. TRANSFERENCIA", "TIPO DE BONO", "PRESTACION",
    ]
    header_fill  = PatternFill("solid", fgColor="1172AB")
    header_font  = Font(name="Calibri", bold=True, size=10, color="FFFFFF")
    thin_border  = Border(
        left=Side(style="thin", color="CCCCCC"),
        right=Side(style="thin", color="CCCCCC"),
        top=Side(style="thin", color="CCCCCC"),
        bottom=Side(style="thin", color="CCCCCC"),
    )
    for col_idx, h in enumerate(headers, start=1):
        cell = ws.cell(row=2, column=col_idx, value=h)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = thin_border
    ws.row_dimensions[2].height = 16

    # Datos (desde fila 3)
    alt_fill = PatternFill("solid", fgColor="EBF5FB")
    data_font = Font(name="Calibri", size=10)
    for row_idx, row in enumerate(rows, start=3):
        # PAGO = lo que entro a caja (copago solamente)
        valor = row["copago"] or 0
        prevision_label = "Fonasa MLE" if (row["prevision"] or "").lower() == "fonasa" else "Particular"
        metodo_label    = (row["metodo_pago"] or "efectivo").capitalize()
        values = [
            (row["hora"] or "")[:5],
            row["paciente_nombre"] or "",
            row["profesional"] or "",
            row["area"] or "",
            prevision_label,
            valor,
            metodo_label,
            row["folio"] or "",
            row["codigo_transferencia"] or "",
            row["tipo_bono"] or "",
            row["procedimiento"] or "",
        ]
        fill = alt_fill if row_idx % 2 == 0 else None
        for col_idx, v in enumerate(values, start=1):
            cell = ws.cell(row=row_idx, column=col_idx, value=v)
            cell.font = data_font
            cell.border = thin_border
            cell.alignment = Alignment(vertical="center")
            if fill:
                cell.fill = fill
            # Columna VALOR → formato moneda CLP
            if col_idx == 6 and isinstance(v, int):
                cell.number_format = '"$"#,##0'
                cell.alignment = Alignment(horizontal="right", vertical="center")

    # Anchos de columna (aproximados)
    col_widths = [8, 24, 22, 18, 12, 12, 14, 12, 18, 14, 28]
    for col_idx, w in enumerate(col_widths, start=1):
        ws.column_dimensions[ws.cell(row=2, column=col_idx).column_letter].width = w

    # Congelar fila de encabezados
    ws.freeze_panes = "A3"

    # Serializar a bytes
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    label = d_desde if d_desde == d_hasta else f"{d_desde}_{d_hasta}"
    filename = f"pagos_{label}.xlsx"

    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/prellenar")
async def prellenar_pagos(
    fecha: str | None = Query(None, description="YYYY-MM-DD; por defecto hoy"),
    token: str | None = Query(None),
    cmc_session: str | None = Cookie(None),
    request: Request = None,
):
    """
    Trae las citas del día desde Medilink y crea filas draft en pagos_cmc
    (pago=0, metodo vacío) para que recepción las complete.

    Estrategia de requests:
    - 1 sola query GET /citas?fecha=DD/MM/YYYY&estado_anulacion=0 para todos los profesionales.
    - Por cada cita con id_atencion: GET /atenciones/{id}/detalles para la prestación,
      throttleado con 0.15s entre requests (evita 429). Best-effort: si falla, prestación queda vacía.
    - Idempotente: se saltea citas que ya tienen fila en pagos_cmc para esa fecha
      (chequeo por id_cita o por rut+fecha).

    Retorna: { creadas: N, saltadas: N, errores: N }
    """
    _require_admin_dep(request, token=token, cmc_session=cmc_session)
    ensure_pagos_table()

    import asyncio
    from medilink import (
        _get_shared_client, _q, _safe_json, HEADERS,
        PROFESIONALES,
    )
    from config import MEDILINK_BASE_URL

    now_cl = datetime.now(_CHILE_TZ)
    if fecha:
        try:
            datetime.strptime(fecha, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(400, "fecha debe ser YYYY-MM-DD")
        fecha_iso = fecha
    else:
        fecha_iso = now_cl.strftime("%Y-%m-%d")

    # Medilink usa DD/MM/YYYY en sus filtros de fecha
    dt_fecha = datetime.strptime(fecha_iso, "%Y-%m-%d")
    fecha_ml = dt_fecha.strftime("%d/%m/%Y")

    # ── 1 request: todas las citas del día ───────────────────────────────────
    client = _get_shared_client()
    try:
        r = await client.get(
            f"{MEDILINK_BASE_URL}/citas",
            params={"q": _q({
                "fecha":            {"eq": fecha_ml},
                "estado_anulacion": {"eq": 0},
            })},
            headers=HEADERS,
            timeout=15,
        )
    except Exception as e:
        log.error("prellenar_pagos: error GET /citas fecha=%s: %s", fecha_iso, e)
        raise HTTPException(502, "Error al contactar Medilink")

    if r.status_code != 200:
        log.warning("prellenar_pagos: GET /citas HTTP %d fecha=%s", r.status_code, fecha_iso)
        raise HTTPException(502, f"Medilink devolvió HTTP {r.status_code}")

    citas = _safe_json(r).get("data", [])
    if not citas:
        return {"creadas": 0, "saltadas": 0, "errores": 0, "mensaje": "Sin citas para esa fecha en Medilink"}

    # Ordenar por hora_inicio para que las filas queden ordenadas
    citas.sort(key=lambda c: c.get("hora_inicio", ""))

    # ── Cargar IDs/ruts ya existentes en pagos_cmc para esa fecha (idempotencia) ──
    from session import _conn
    with _conn() as conn:
        rows_exist = conn.execute(
            "SELECT id_cita, rut FROM pagos_cmc WHERE fecha = ?", (fecha_iso,)
        ).fetchall()

    existing_id_citas = {r["id_cita"] for r in rows_exist if r["id_cita"]}
    existing_ruts     = {r["rut"]     for r in rows_exist if r["rut"]}

    creadas = saltadas = errores = 0

    for cita in citas:
        id_cita_str = str(cita.get("id", ""))
        rut_cita    = (cita.get("rut_paciente") or "").strip()

        # Idempotencia: saltar si ya existe por id_cita o por rut+fecha
        if id_cita_str and id_cita_str in existing_id_citas:
            saltadas += 1
            continue
        if rut_cita and rut_cita in existing_ruts:
            saltadas += 1
            continue

        id_prof  = cita.get("id_profesional")
        prof_info = PROFESIONALES.get(id_prof, {}) if id_prof else {}

        nombre_pac  = (cita.get("nombre_paciente") or "").strip()
        profesional = (cita.get("nombre_profesional") or prof_info.get("nombre", "")).strip()
        area        = prof_info.get("especialidad", "")
        hora_inicio = (cita.get("hora_inicio") or "")[:5]

        # ── Prestación: 1 request por cita con id_atencion, throttleado ─────
        prestacion = ""
        id_aten = cita.get("id_atencion")
        if id_aten:
            try:
                await asyncio.sleep(0.15)
                rd = await client.get(
                    f"{MEDILINK_BASE_URL}/atenciones/{id_aten}/detalles",
                    headers=HEADERS,
                    timeout=8,
                )
                if rd.status_code == 200:
                    detalles = _safe_json(rd).get("data", [])
                    nombres = []
                    for det in detalles:
                        n = (det.get("nombre_prestacion") or "").strip()
                        if n and n not in nombres:
                            nombres.append(n)
                    prestacion = " / ".join(nombres)
            except Exception as e_det:
                log.debug("prellenar_pagos: detalles id_aten=%s error: %s", id_aten, e_det)

        try:
            with _conn() as conn:
                conn.execute(
                    """INSERT INTO pagos_cmc
                       (fecha, hora, paciente_nombre, rut, id_profesional, profesional,
                        area, prevision, copago, bonificacion, metodo_pago, folio,
                        codigo_transferencia, tipo_bono, procedimiento, origen, id_cita,
                        creado_por, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,0,0,'','','','',?,?,?,
                               'prellenar', datetime('now'), datetime('now'))""",
                    (
                        fecha_iso,
                        hora_inicio,
                        nombre_pac,
                        rut_cita,
                        id_prof,
                        profesional,
                        area,
                        "particular",   # default; recepción corrige si es fonasa
                        prestacion,
                        "presencial",
                        id_cita_str,
                    )
                )
                conn.commit()
            # Registrar para idempotencia en este mismo batch
            if id_cita_str:
                existing_id_citas.add(id_cita_str)
            if rut_cita:
                existing_ruts.add(rut_cita)
            creadas += 1
        except Exception as e_ins:
            log.warning("prellenar_pagos: error INSERT cita %s: %s", id_cita_str, e_ins)
            errores += 1

    log.info(
        "prellenar_pagos: fecha=%s creadas=%d saltadas=%d errores=%d",
        fecha_iso, creadas, saltadas, errores,
    )
    return {"creadas": creadas, "saltadas": saltadas, "errores": errores}


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
