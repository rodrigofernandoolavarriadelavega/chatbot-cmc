"""app/verdad.py — El Libro de la Verdad del CMC.

Por qué existe
---------------
`sessions.db` acumuló 16 tablas donde vive "atenciones" o "plata"
(bi_atenciones, bi_pagos_caja, pagos_cmc, caja_diaria, olavarria_*, etc.) más
Medilink en vivo, el BI en Postgres (health-bi-project) y heatmap_cache.db.
Cada dashboard nuevo elegía su propia fuente — por eso los números "nunca
cuadran a la primera": /cmc/ebitda, /cmc/comparador, /alma/roas y el panel del
día terminaron con 4 variantes ligeramente distintas del mismo SELECT contra
`bi_pagos_caja`.

Este módulo es la ÚNICA forma de preguntar los hechos de negocio del CMC. Una
función por pregunta. Cada función:

  1. Documenta en su docstring: de qué tabla sale, POR QUÉ esa y no otra, qué
     corrección aplica (si aplica alguna) y qué NO puede responder.
  2. Devuelve el dato + su procedencia — cada dict de salida trae, además de
     los campos propios de la pregunta, las claves:
       "_fuente"        → tabla(s) SQL de origen
       "_periodo"       → {"desde":, "hasta":} rango efectivamente consultado
       "_n_filas"       → cantidad de filas agregadas (para poder auditar "¿es
                           una muestra chica?")
       "_advertencias"  → lista de strings; vacía si el dato es limpio. Si no
                           está vacía, el dato está DEGRADADO — leerla antes de
                           citar la cifra.
       "_no_responde"   → qué preguntas relacionadas este número NO contesta
                           (para no sobre-interpretar).
  3. Advierte cuando la tasa de orfandad pago↔atención o la antigüedad del
     sync superan un umbral (ver UMBRAL_* abajo).

Regla dura de conexión (incidente 2026-06-10, deadlock GIL×SQLite que congeló
agentecmc.cl): SIEMPRE `with db() as c:` importado de `session.py`. NUNCA
`with _conn() as c:` — `_conn()` no cierra la conexión de forma explícita y
el destructor puede deadlockear contra el hilo de push de WhatsApp.

Qué NO hace este módulo
------------------------
No calcula EBITDA, honorarios, ni gastos — esas son reglas de negocio propias
(porcentaje de cada profesional, retención de boleta, contratos fijos) que ya
viven correctamente en `ebitda_routes.py` y siguen ahí; ese módulo SÍ debería
llamar a `plata_por_profesional()` de acá para su parte de "ingresos" en vez
de repetir el SELECT (ver docs/LIBRO_DE_LA_VERDAD.md, sección "deuda
pendiente" — no se migró en esta pasada por el volumen de superficie a
re-verificar).

Ver también: docs/LIBRO_DE_LA_VERDAD.md — tabla completa de "qué fuente usar
para cada pregunta" y por qué NO usar cada una de las alternativas.
"""

import logging
from datetime import date, datetime, timedelta

log = logging.getLogger("verdad")

# ── Umbrales de alerta (centralizados — no mágicos por función) ────────────
UMBRAL_ORFANDAD_WARN = 10.0    # % de pagos sin atencion_id → advertir
UMBRAL_ORFANDAD_CRIT = 30.0    # % → degradado severo: no confiar en el
                                # desglose fino por profesional/atención
UMBRAL_SYNC_VIEJO_HORAS = 30   # el cron corre cada 30 min + full 03:59 CLT;
                                # más de 30h sin sync = algo se cayó
UMBRAL_MUESTRA_CHICA = 20      # menos de N pagos en el rango → advertir

# ── Qué NO usar, y por qué (fuente única para docs/LIBRO_DE_LA_VERDAD.md) ──
TABLAS_NO_USAR = {
    "bi_atenciones.total / abonado": (
        "Es lo FACTURADO, no lo cobrado. Incluye atenciones con total=0 que "
        "nunca se cerraron (569 en 180 días medidos 2026-07-12, ~$25,5M/año "
        "de fuga invisible — ver memory cmc-atenciones-no-cerradas) y el ETL "
        "del BI las descarta en silencio. `abonado` además viene en 0 en la "
        "respuesta JSON de Medilink casi siempre — no se puede confiar."
    ),
    "citas_bot.estado / bi_atenciones.finalizado (para contar atenciones)": (
        "El estado de la cita es un TRÁMITE administrativo, no un hecho. Si "
        "el paciente pagó, fue atendido — la CAJA es el hecho, no el estado."
    ),
    "pagos_cmc (para VENTA TOTAL)": (
        "Solo cubre lo que recepción tecleó a mano en el módulo Pagos de "
        "Alma (subcobertura estructural: 9.797 filas históricas contra "
        "36.779 de bi_pagos_caja en el mismo rango). Sirve para MEDIO DE "
        "PAGO y COPAGO, no para venta total."
    ),
    "bi_pagos_caja.metodo_pago (para mix de medios de pago)": (
        "99,7% de las 36.779 filas dicen 'Efectivo' porque nadie cambia el "
        "default en Medilink — no mide el mix real. Usar pagos_cmc.metodo_pago."
    ),
    "olavarria_atenciones_cache": (
        "Cache local vía /citas?estado_cita=atendido&id_profesional=1: "
        "subestima ~22% porque el filtro por estado no captura todo lo que "
        "/atenciones sí ve. Solo usar como fallback si bi_pagos_caja no "
        "responde."
    ),
    "olavarria_bi_ingresos / bi.fact_ingresos (BI Postgres)": (
        "Snapshot histórico (corte 2026-04) del proyecto health-bi-project, "
        "vía /atenciones. Sobreestima ~15% frente a la caja real (atenciones "
        "registradas pero no cobradas realmente). Si por algún motivo se usa "
        "esta tabla, aplicar el factor de corrección 0.85 — pero NO aplicar "
        "ese factor a bi_pagos_caja, que ya es caja real y no lo necesita."
    ),
    "bi.fact_pagos (Postgres, health-bi-project)": (
        "Está VACÍO (ingreso_id NULL 100%) — cualquier análisis basado en "
        "esta tabla queda invalidado. No confundir con `bi_pagos_caja` de "
        "sessions.db, que es la tabla real y viva."
    ),
    "caja_diaria": (
        "Registro manual de efectivo físico a depositar (cuadre de caja "
        "chica), no venta. Sirve para conciliar depósitos, no para medir "
        "ingresos."
    ),
}


# ── Helpers de rango de fechas ──────────────────────────────────────────────
def _fin_exclusivo(hasta: str) -> str:
    """'2026-06-30' (inclusive) → '2026-07-01' (exclusivo, para `fecha < ?`)."""
    return (date.fromisoformat(hasta) + timedelta(days=1)).isoformat()


def mes_bounds(mes: str) -> tuple[str, str]:
    """(desde, hasta) INCLUSIVE para un mes 'YYYY-MM'. Ej: '2026-06' →
    ('2026-06-01', '2026-06-30')."""
    y, m = int(mes[:4]), int(mes[5:7])
    desde = f"{mes}-01"
    fin_y, fin_m = (y + m // 12, m % 12 + 1)
    hasta = (date(fin_y, fin_m, 1) - timedelta(days=1)).isoformat()
    return desde, hasta


def _pct(n_parte, n_total) -> float:
    return round(100.0 * n_parte / n_total, 1) if n_total else 0.0


def _advertencias_orfandad(pct_huerf: float, sujeto: str = "los pagos") -> list[str]:
    if pct_huerf >= UMBRAL_ORFANDAD_CRIT:
        return [
            f"DEGRADADO: {pct_huerf}% de {sujeto} no tiene atencion_id "
            f"vinculado (umbral crítico {UMBRAL_ORFANDAD_CRIT}%). El monto "
            f"sigue siendo correcto (es caja real), pero NO se puede auditar "
            f"contra la atención específica en Medilink. Ver "
            f"tasa_orfandad() y docs/LIBRO_DE_LA_VERDAD.md, sección "
            f"'anomalía pago↔atención'."
        ]
    if pct_huerf >= UMBRAL_ORFANDAD_WARN:
        return [
            f"{pct_huerf}% de {sujeto} sin atencion_id vinculado (umbral de "
            f"aviso {UMBRAL_ORFANDAD_WARN}%) — revisar tasa_orfandad()."
        ]
    return []


# ── Estado del sync (para saber si el resto de las funciones es fresco) ────
def sync_status() -> dict:
    """¿Qué tan fresca está `bi_pagos_caja` / `bi_atenciones`?

    Fuente: `bi_sync_log` (tipo='pagos' → sync incremental cada 30 min; tipo
    'rango' → repasada de atenciones/repasada nocturna 03:59 CLT). Por qué
    esta y no otra: es el único registro de auditoría del propio cron; no hay
    forma de saber la frescura mirando `synced_at` de cada fila sin agregar.
    No responde: si el sync corrió pero encontró datos incorrectos en
    Medilink (`ok=1` con `n_errores>0` es posible y esta función no lo separa
    — ver bi_sync_log.n_errores directamente para ese detalle).
    """
    from session import db
    with db() as c:
        rows = c.execute(
            "SELECT tipo, MAX(fin) FROM bi_sync_log GROUP BY tipo"
        ).fetchall()
    ahora = datetime.utcnow()
    sync: dict = {}
    advert: list[str] = []
    for tipo, fin in rows:
        if not fin:
            continue
        edad_h = None
        try:
            edad_h = (ahora - datetime.fromisoformat(fin[:19])).total_seconds() / 3600
        except Exception:
            pass
        sync[tipo] = {"ultimo_sync_utc": fin, "edad_horas": round(edad_h, 1) if edad_h is not None else None}
        if edad_h is not None and edad_h > UMBRAL_SYNC_VIEJO_HORAS:
            advert.append(
                f"sync '{tipo}' desactualizado hace {edad_h:.0f}h "
                f"(umbral {UMBRAL_SYNC_VIEJO_HORAS}h) — los datos de hoy/ayer "
                f"pueden estar incompletos."
            )
    return {
        "sync": sync,
        "_fuente": "bi_sync_log",
        "_periodo": None,
        "_n_filas": len(rows),
        "_advertencias": advert,
        "_no_responde": "si el contenido sincronizado es correcto, solo si corrió.",
    }


# ── 1. Venta total ──────────────────────────────────────────────────────────
def venta_total(desde: str, hasta: str) -> dict:
    """¿Cuánto vendió el CMC entre `desde` y `hasta` (ambos inclusive)?

    Fuente: `bi_pagos_caja` (sessions.db) — la caja real de Medilink. Cada
    fila es un pago efectivamente cobrado, sincronizado por cron cada 30 min
    desde `/api/v5/pagos` de Medilink (`bi_sync.py`).

    Por qué esta y no otra:
      - `bi_atenciones.total` es lo FACTURADO, no lo cobrado (ver
        TABLAS_NO_USAR).
      - `pagos_cmc` es lo que recepción tecleó a mano (copago) — subcubre
        estructuralmente, no es venta total.
      - `olavarria_bi_ingresos` / BI Postgres son snapshots viejos que
        sobreestiman ~15%.

    Corrección aplicada: ninguna — `bi_pagos_caja` YA es el dato cobrado
    real, no se le aplica el factor 0.85 (ese factor es SOLO para
    `olavarria_bi_ingresos`, ver TABLAS_NO_USAR).

    No responde: medio de pago (99,7% de esta tabla dice "Efectivo" por
    default de Medilink — usar mix_medios_de_pago()), ni honorarios/EBITDA
    (usar ebitda_routes._ebitda_mes / GET /api/cmc/ebitda), ni a qué atención
    de Medilink corresponde cada pago huérfano (ver tasa_orfandad()).
    """
    from session import db
    fin_excl = _fin_exclusivo(hasta)
    with db() as c:
        total, n, pac, n_huerf = c.execute(
            "SELECT COALESCE(SUM(monto),0), COUNT(*), COUNT(DISTINCT id_paciente), "
            "SUM(CASE WHEN atencion_id IS NULL THEN 1 ELSE 0 END) "
            "FROM bi_pagos_caja WHERE fecha>=? AND fecha<?",
            (desde, fin_excl),
        ).fetchone()
    n = n or 0
    pct_huerf = _pct(n_huerf or 0, n)
    advert = _advertencias_orfandad(pct_huerf)
    if n == 0:
        advert.append("Sin pagos en el rango — verificar fechas o sync_status().")
    elif n < UMBRAL_MUESTRA_CHICA:
        advert.append(f"Muestra chica ({n} pagos) — cifra poco estable.")
    return {
        "total": int(total or 0),
        "n_pagos": n,
        "pacientes_distintos": pac or 0,
        "pagos_huerfanos": n_huerf or 0,
        "pagos_huerfanos_pct": pct_huerf,
        "_fuente": "bi_pagos_caja",
        "_periodo": {"desde": desde, "hasta": hasta},
        "_n_filas": n,
        "_advertencias": advert,
        "_no_responde": (
            "medio de pago (ver mix_medios_de_pago), honorarios/EBITDA "
            "(ver /cmc/ebitda), a qué atención de Medilink corresponde cada "
            "pago huérfano (ver tasa_orfandad)."
        ),
    }


# ── 2. Plata por profesional ────────────────────────────────────────────────
def plata_por_profesional(desde: str, hasta: str, incluir_sin_asignar: bool = True) -> dict:
    """Desglose de ingresos por médico/profesional entre `desde` y `hasta`.

    Fuente: `bi_pagos_caja.id_profesional` (sessions.db). SÍ existe esta
    columna en la caja real — una nota vieja de memoria decía que el
    desglose por profesional "no se podía hacer" porque miraba
    `bi.fact_pagos` del BI Postgres (que está vacío); en `bi_pagos_caja`
    (sessions.db) sí está poblada y es la fuente correcta.

    Por qué esta y no otra: mismo criterio que venta_total() — es la caja
    real, no lo facturado. El nombre del profesional se resuelve contra
    `medilink.PROFESIONALES` (diccionario local, no requiere llamar a
    Medilink).

    `id_profesional` se resuelve en `bi_sync.py::_resolver_profesional_pago`
    con una cascada de heurísticas (match contra `pagos_cmc` primero, luego
    contra `bi_atenciones` por monto/fecha/deuda). Es confiable para el
    ingreso ($) — el bug conocido (memory cmc-comparador-y-atribucion) de
    "pago cruzado al profesional equivocado" se corrigió el 2026-06-12
    (commit b090e9c). Lo que ese mismo fix rompió como efecto secundario es
    el VÍNCULO A LA ATENCIÓN (atencion_id), no el profesional — ver
    tasa_orfandad() y docs/LIBRO_DE_LA_VERDAD.md.

    Corrección aplicada: ninguna.

    No responde: honorario líquido al profesional (bruto × %, retención
    15,25% salvo quienes facturan — usar ebitda_routes), ni si el pago tiene
    un atencion_id válido para cruzar contra la ficha clínica (ver
    tasa_orfandad(profesional=...)).
    """
    from session import db
    from medilink import PROFESIONALES
    fin_excl = _fin_exclusivo(hasta)
    where_extra = "" if incluir_sin_asignar else "AND id_profesional IS NOT NULL"
    with db() as c:
        rows = c.execute(
            f"SELECT id_profesional, SUM(monto), COUNT(*), "
            f"COUNT(DISTINCT id_paciente), "
            f"SUM(CASE WHEN atencion_id IS NULL THEN 1 ELSE 0 END) "
            f"FROM bi_pagos_caja WHERE fecha>=? AND fecha<? {where_extra} "
            f"GROUP BY id_profesional ORDER BY 2 DESC",
            (desde, fin_excl),
        ).fetchall()
    profs = []
    tot = n_tot = 0
    advert: list[str] = []
    for pid, monto, n, pac, n_huerf in rows:
        monto = int(monto or 0)
        n = n or 0
        info = PROFESIONALES.get(pid, {}) if pid is not None else {}
        pct_h = _pct(n_huerf or 0, n)
        nombre = info.get("nombre") or ("Sin asignar" if pid is None else f"Prof {pid}")
        profs.append({
            "id_profesional": pid,
            "nombre": nombre,
            "especialidad": info.get("especialidad", ""),
            "ingreso": monto,
            "n_pagos": n,
            "pacientes_distintos": pac or 0,
            "pagos_huerfanos": n_huerf or 0,
            "pagos_huerfanos_pct": pct_h,
        })
        tot += monto
        n_tot += n
        if pct_h >= UMBRAL_ORFANDAD_CRIT:
            advert.append(f"{nombre}: {pct_h}% de sus pagos sin atencion_id (crítico).")
    return {
        "profesionales": profs,
        "total": tot,
        "_fuente": "bi_pagos_caja",
        "_periodo": {"desde": desde, "hasta": hasta},
        "_n_filas": n_tot,
        "_advertencias": advert,
        "_no_responde": "honorario líquido (ver /cmc/ebitda), vínculo a ficha clínica de cada pago huérfano.",
    }


# ── 3. Atenciones (consultas reales) ────────────────────────────────────────
def atenciones(desde: str, hasta: str, profesional: int | None = None) -> dict:
    """¿Cuántas consultas hubo DE VERDAD entre `desde` y `hasta`?

    Fuente: `bi_pagos_caja`, `COUNT(*)` — cada fila es un pago cobrado y se
    cuenta como una visita (misma convención que `caja_helper.caja_visitas`:
    "cada pago = una visita"; instalación/control/sesión de un tratamiento
    largo se distingue después por el monto, no por des-contar visitas).

    Por qué esta y no otra: `citas_bot.estado` / `bi_atenciones.finalizado`
    NO sirven para contar "cuántos pacientes se atendieron" — el estado de
    la cita es un TRÁMITE, no un hecho. Hay 569 atenciones "nunca cerradas"
    (`finalizado=0`, `total=0`, ~$25,5M/año de fuga, ver memory
    cmc-atenciones-no-cerradas) que si se contaran por estado desaparecerían
    de las estadísticas aunque haya plata circulando y paciente atendido.
    Regla dura del dueño: "si el paciente pagó, fue atendido" — la CAJA es
    el hecho.

    Corrección aplicada: ninguna.

    No responde: si el pago (== visita) tiene un `atencion_id` válido en
    Medilink — un pago huérfano sigue contando como atención real acá (el
    dueño cobró, alguien fue atendido), pero no se puede decir a cuál
    atención de Medilink corresponde. Ver tasa_orfandad() para medir cuánto
    de esta cifra es auditable end-to-end contra la ficha.
    """
    from session import db
    fin_excl = _fin_exclusivo(hasta)
    where_prof = "AND id_profesional=?" if profesional is not None else ""
    params: tuple = (desde, fin_excl) + ((profesional,) if profesional is not None else ())
    with db() as c:
        n, pac, n_huerf = c.execute(
            f"SELECT COUNT(*), COUNT(DISTINCT id_paciente), "
            f"SUM(CASE WHEN atencion_id IS NULL THEN 1 ELSE 0 END) "
            f"FROM bi_pagos_caja WHERE fecha>=? AND fecha<? {where_prof}",
            params,
        ).fetchone()
    n = n or 0
    pct_h = _pct(n_huerf or 0, n)
    return {
        "atenciones": n,
        "pacientes_distintos": pac or 0,
        "pagos_huerfanos": n_huerf or 0,
        "pagos_huerfanos_pct": pct_h,
        "_fuente": "bi_pagos_caja",
        "_periodo": {"desde": desde, "hasta": hasta},
        "_n_filas": n,
        "_advertencias": _advertencias_orfandad(pct_h, "las atenciones contadas"),
        "_no_responde": "si cada atención está vinculada a una ficha/atencion_id de Medilink (ver tasa_orfandad).",
    }


# ── 4. Pacientes distintos ──────────────────────────────────────────────────
def pacientes_distintos(desde: str, hasta: str, profesional: int | None = None) -> dict:
    """¿Cuántos pacientes distintos pagaron entre `desde` y `hasta`?

    Fuente: `bi_pagos_caja`, `COUNT(DISTINCT id_paciente)`. Mismo criterio
    que atenciones()/venta_total(): la caja es el hecho. Un mismo paciente
    con varios pagos en el rango (ej. control dental en cuotas) cuenta una
    sola vez.

    No responde: pacientes que fueron atendidos pero cuya atención nunca se
    cobró (las 569 "nunca cerradas") — esos no aparecen acá porque no hay
    pago. No responde nombre/contacto (para eso, cruzar id_paciente contra
    `bi.dim_paciente` o `citas_cache`, ver `caja_helper._identidad_pacientes`).
    """
    from session import db
    fin_excl = _fin_exclusivo(hasta)
    where_prof = "AND id_profesional=?" if profesional is not None else ""
    params: tuple = (desde, fin_excl) + ((profesional,) if profesional is not None else ())
    with db() as c:
        (pac,) = c.execute(
            f"SELECT COUNT(DISTINCT id_paciente) FROM bi_pagos_caja "
            f"WHERE fecha>=? AND fecha<? {where_prof}",
            params,
        ).fetchone()
    return {
        "pacientes_distintos": pac or 0,
        "_fuente": "bi_pagos_caja",
        "_periodo": {"desde": desde, "hasta": hasta},
        "_n_filas": pac or 0,
        "_advertencias": [],
        "_no_responde": "pacientes atendidos sin cobro registrado (atenciones nunca cerradas).",
    }


# ── 5. Mix de medios de pago ────────────────────────────────────────────────
def mix_medios_de_pago(desde: str, hasta: str) -> dict:
    """¿Cómo se distribuye el copago entre efectivo/transferencia/tarjeta?

    Fuente: `pagos_cmc.metodo_pago` (sessions.db) — lo que recepción teclea
    en el módulo Pagos de Alma. ÚNICA fuente usable: `bi_pagos_caja.metodo_pago`
    (el campo homónimo de Medilink) es inútil — 99,7% de sus filas históricas
    dicen "Efectivo" porque nadie cambia el valor por defecto al cargar el
    pago en Medilink, no porque se cobre todo en efectivo.

    Cobertura: `pagos_cmc` NO es venta total — solo cubre lo que recepción
    registró manualmente (bloqueado=1 es una fila BUENA, no se excluye).
    Regla dura: tarjeta (débito/crédito) SOLO aplica a odontología/dental en
    el CMC — las prestaciones médicas se cobran en efectivo/transferencia.
    Esta función reporta `cobertura_pct` = n_filas de pagos_cmc contra
    n_pagos de bi_pagos_caja del mismo rango, para dimensionar cuánto del
    total real queda efectivamente medido por medio de pago.

    No responde: venta total (usar venta_total()); el medio de pago de lo
    que NO pasó por el módulo Pagos de recepción (queda sin clasificar).
    """
    from session import db
    fin_excl = _fin_exclusivo(hasta)
    with db() as c:
        rows = c.execute(
            "SELECT metodo_pago, COUNT(*), COALESCE(SUM(copago),0) "
            "FROM pagos_cmc WHERE fecha>=? AND fecha<? "
            "GROUP BY metodo_pago ORDER BY 3 DESC",
            (desde, fin_excl),
        ).fetchall()
    medios = []
    n_tot = 0
    monto_tot = 0
    for metodo, n, monto in rows:
        medios.append({
            "metodo": (metodo or "").strip() or "sin_registrar",
            "n_pagos": n,
            "copago_total": int(monto or 0),
        })
        n_tot += n
        monto_tot += int(monto or 0)
    ref_caja = venta_total(desde, hasta)
    cobertura_pct = _pct(n_tot, ref_caja["n_pagos"]) if ref_caja["n_pagos"] else 0.0
    advert = []
    if cobertura_pct < 50:
        advert.append(
            f"Cobertura baja: pagos_cmc solo registra {cobertura_pct}% de los "
            f"{ref_caja['n_pagos']} pagos de bi_pagos_caja en el rango — el "
            f"mix por medio de pago NO representa el 100% de la venta."
        )
    elif cobertura_pct > 110:
        advert.append(
            f"Cobertura >{cobertura_pct}%: pagos_cmc tiene más filas ({n_tot}) "
            f"que bi_pagos_caja ({ref_caja['n_pagos']}) en el rango — probable "
            f"doble registro (pagos parciales tecleados más de una vez por "
            f"recepción, o un pago de Medilink que agrupa 2+ filas de "
            f"pagos_cmc). No usar 'n_pagos' de esta función como conteo de "
            f"visitas; para eso usar atenciones()."
        )
    return {
        "medios": medios,
        "n_pagos": n_tot,
        "copago_total": monto_tot,
        "cobertura_pct": cobertura_pct,
        "_fuente": "pagos_cmc",
        "_periodo": {"desde": desde, "hasta": hasta},
        "_n_filas": n_tot,
        "_advertencias": advert,
        "_no_responde": "venta total del CMC (usar venta_total()).",
    }


# ── 6. Copagos por previsión ─────────────────────────────────────────────────
def copagos(desde: str, hasta: str, prevision: str | None = None) -> dict:
    """Copago cobrado por recepción, desglosado por previsión (fonasa/particular).

    Fuente: `pagos_cmc` (copago, bonificacion, prevision). Misma tabla y
    misma limitación de cobertura que mix_medios_de_pago() — es lo que
    recepción tecleó, no la venta total. `bonificacion` es el aporte de
    Fonasa/bono que complementa el copago del paciente (para Fonasa,
    copago + bonificacion ≈ arancel total, ver pagos_recon.py).

    No responde: venta total (usar venta_total()).
    """
    from session import db
    fin_excl = _fin_exclusivo(hasta)
    where_prev = "AND LOWER(prevision)=?" if prevision else ""
    params: tuple = (desde, fin_excl) + ((prevision.lower(),) if prevision else ())
    with db() as c:
        rows = c.execute(
            f"SELECT COALESCE(prevision,'sin_registrar'), COUNT(*), "
            f"COALESCE(SUM(copago),0), COALESCE(SUM(bonificacion),0) "
            f"FROM pagos_cmc WHERE fecha>=? AND fecha<? {where_prev} "
            f"GROUP BY 1 ORDER BY 3 DESC",
            params,
        ).fetchall()
    desglose = [
        {"prevision": p, "n_pagos": n, "copago_total": int(cop or 0), "bonificacion_total": int(bon or 0)}
        for p, n, cop, bon in rows
    ]
    n_tot = sum(d["n_pagos"] for d in desglose)
    return {
        "desglose": desglose,
        "copago_total": sum(d["copago_total"] for d in desglose),
        "_fuente": "pagos_cmc",
        "_periodo": {"desde": desde, "hasta": hasta},
        "_n_filas": n_tot,
        "_advertencias": [] if n_tot else ["Sin registros de copago en el rango."],
        "_no_responde": "venta total (usar venta_total()).",
    }


# ── 7. Mejor mes de un profesional ──────────────────────────────────────────
def mejor_mes(profesional: int, top: int = 5) -> dict:
    """Ranking de los mejores meses históricos de un profesional por ingreso.

    Fuente: `bi_pagos_caja`, agrupado por `substr(fecha,1,7)`. Mismo criterio
    que plata_por_profesional() pero sobre TODO el histórico disponible en
    caja (no un rango acotado), para encontrar el pico.

    Validado contra cifra conocida del dueño: Dr. Abarca (id 73), mejor mes
    histórico = 2025-11 con $5.620.090 y 331 pacientes distintos (352 pagos).

    No responde: si ese mes tuvo una tasa de orfandad alta (afecta la
    auditabilidad fina, no el monto) — cruzar con tasa_orfandad(profesional=,
    desde=, hasta=) del mes en cuestión.
    """
    from session import db
    with db() as c:
        rows = c.execute(
            "SELECT substr(fecha,1,7) AS mes, SUM(monto) AS total, "
            "COUNT(DISTINCT id_paciente) AS pac, COUNT(*) AS n "
            "FROM bi_pagos_caja WHERE id_profesional=? "
            "GROUP BY mes ORDER BY total DESC LIMIT ?",
            (profesional, top),
        ).fetchall()
    ranking = [
        {"mes": mes, "total": int(total or 0), "pacientes_distintos": pac, "n_pagos": n}
        for mes, total, pac, n in rows
    ]
    return {
        "profesional_id": profesional,
        "ranking": ranking,
        "mejor_mes": ranking[0] if ranking else None,
        "_fuente": "bi_pagos_caja",
        "_periodo": None,
        "_n_filas": sum(r["n_pagos"] for r in ranking),
        "_advertencias": [] if ranking else ["Sin pagos históricos para este profesional."],
        "_no_responde": "honorario líquido de ese mes (usar ebitda_routes con mes=ese mes).",
    }


# ── 8. Tasa de orfandad pago↔atención ───────────────────────────────────────
def tasa_orfandad(desde: str, hasta: str, profesional: int | None = None) -> dict:
    """¿Qué porcentaje de los pagos NO tiene un `atencion_id` vinculado?

    Fuente: `bi_pagos_caja.atencion_id` (NULL = pago sin atención de Medilink
    asociada — "huérfano"). El monto y el profesional del pago siguen siendo
    correctos aunque `atencion_id` sea NULL (se resuelven por rutas
    independientes en `bi_sync.py::_resolver_profesional_pago`); lo que se
    pierde es la trazabilidad pago → ficha clínica específica.

    Por qué importa: mientras la tasa se mantenga baja (~4% histórico
    agregado 2020-2026, ver memory cmc-fuentes-de-venta-y-pago), es ruido de
    matching normal. Si sube, `plata_por_profesional()` y `atenciones()`
    SIGUEN siendo correctas (se basan en el pago, no en el vínculo) — pero en
    el límite (orfandad ≈100%) ya no se puede decir cuánto produjo cada
    médico en TÉRMINOS CLÍNICOS auditables, solo cuánto entró en caja a su
    nombre.

    ANOMALÍA MEDIDA (2026-07-14, este módulo): la orfandad NO es estable ni
    focalizada en un profesional — es un salto CMC-WIDE reciente:
      abr-2026  0,0%   (0 / 1.005 pagos)
      may-2026  8,8%   (101 / 1.144)
      jun-2026 87,0%   (1.100 / 1.264)
      jul-2026 92,8%   (parcial, 493 / 531)
    Afecta a prácticamente todos los profesionales activos en junio (Dr.
    Olavarría 396/435 = 91%, Dr. Márquez 144/154 = 94%, Dr. Abarca 144/178 =
    81%, Etcheverry kine 124/141 = 88%, entre otros).

    Causa raíz identificada (lectura de código, no confirmada con el autor):
    el commit `b090e9c0` (2026-06-12, "fix cruce pago→profesional usa
    pagos_cmc como nivel 0.5") agregó en
    `bi_sync.py::_resolver_profesional_pago` un nivel 0.5 que, cuando
    encuentra un match único en `pagos_cmc` por nombre+fecha, hace
    `return profs_cmc.pop(), None` — retorna INMEDIATAMENTE con
    `atencion_id=None`, sin llegar a la cascada heurística de abajo que sí
    intenta resolver `atencion_id` contra `bi_atenciones`. El comentario del
    propio código dice "la heurística existente en el bloque siguiente puede
    completarlo" pero el `return` hace que ese bloque nunca se ejecute. Ese
    commit SÍ arregló el bug real que buscaba resolver (profesional cruzado
    al equivocado, ver memory cmc-comparador-y-atribucion-2026-06-12) — el
    costo fue este efecto secundario no buscado. Cuanto más cobertura tiene
    `pagos_cmc` (recepción registrando bien), MÁS pagos entran por el nivel
    0.5 y MÁS orfandad de `atencion_id` se genera — la tendencia va a seguir
    empeorando si no se toca.

    Esta función SOLO MIDE. No arregla nada. Reportar y sugerir invocar al
    agente `cmc-bot-engineer` para el fix en `bi_sync.py` (mantener el
    `id_profesional` resuelto por nivel 0.5, pero seguir intentando resolver
    `atencion_id` con la cascada heurística existente, filtrada al
    profesional ya confirmado, en vez de retornar `None` de inmediato).

    No responde: cómo arreglarlo (ver nota de arriba), ni si el pago
    huérfano corresponde a una atención real que sí existe en Medilink pero
    no se pudo matchear (probable) vs. una atención que nunca se registró
    (menos probable, dado que el monto y paciente sí llegaron desde
    `/api/v5/pagos`).
    """
    from session import db
    fin_excl = _fin_exclusivo(hasta)
    where_prof = "AND id_profesional=?" if profesional is not None else ""
    params: tuple = (desde, fin_excl) + ((profesional,) if profesional is not None else ())
    with db() as c:
        n, n_huerf = c.execute(
            f"SELECT COUNT(*), SUM(CASE WHEN atencion_id IS NULL THEN 1 ELSE 0 END) "
            f"FROM bi_pagos_caja WHERE fecha>=? AND fecha<? {where_prof}",
            params,
        ).fetchone()
    n = n or 0
    n_huerf = n_huerf or 0
    pct = _pct(n_huerf, n)
    return {
        "n_pagos": n,
        "pagos_huerfanos": n_huerf,
        "pagos_huerfanos_pct": pct,
        "profesional_id": profesional,
        "_fuente": "bi_pagos_caja.atencion_id",
        "_periodo": {"desde": desde, "hasta": hasta},
        "_n_filas": n,
        "_advertencias": _advertencias_orfandad(pct),
        "_no_responde": "cómo arreglar el vínculo (ver docstring; fix en bi_sync.py, invocar cmc-bot-engineer).",
    }


def tasa_orfandad_evolucion(desde: str = "2025-01-01", hasta: str | None = None) -> dict:
    """Serie mensual de la tasa de orfandad, CMC-completo. Ver tasa_orfandad()
    para la explicación completa (causa raíz, umbrales, qué hacer)."""
    from session import db
    hasta = hasta or date.today().isoformat()
    fin_excl = _fin_exclusivo(hasta)
    with db() as c:
        rows = c.execute(
            "SELECT substr(fecha,1,7) AS mes, COUNT(*) AS n, "
            "SUM(CASE WHEN atencion_id IS NULL THEN 1 ELSE 0 END) AS n_huerf "
            "FROM bi_pagos_caja WHERE fecha>=? AND fecha<? "
            "GROUP BY mes ORDER BY mes",
            (desde, fin_excl),
        ).fetchall()
    serie = [
        {"mes": mes, "n_pagos": n, "pagos_huerfanos": n_huerf or 0, "pagos_huerfanos_pct": _pct(n_huerf or 0, n)}
        for mes, n, n_huerf in rows
    ]
    return {
        "serie": serie,
        "_fuente": "bi_pagos_caja.atencion_id",
        "_periodo": {"desde": desde, "hasta": hasta},
        "_n_filas": sum(s["n_pagos"] for s in serie),
        "_advertencias": _advertencias_orfandad(serie[-1]["pagos_huerfanos_pct"]) if serie else [],
        "_no_responde": "desglose por profesional (usar tasa_orfandad(profesional=...) mes a mes).",
    }


# ── 9. Egresos ───────────────────────────────────────────────────────────────
def egresos(desde: str, hasta: str) -> dict:
    """Gastos operativos registrados manualmente entre `desde` y `hasta`.

    Fuente: `egresos_cmc` (arriendo, sueldos, servicios, insumos…). Filtra
    solo por `fecha` dentro del rango — NO expande gastos recurrentes fuera
    de su mes de alta (para eso, ver `ebitda_routes._gastos_mes`, que sí
    proyecta recurrentes a cada mes; esta función es la vista "cruda").

    No responde: EBITDA (ingresos − honorarios − gastos, usar
    ebitda_routes._ebitda_mes / GET /api/cmc/ebitda), gastos recurrentes
    dados de alta antes del rango pero vigentes dentro de él.
    """
    from session import db
    fin_excl = _fin_exclusivo(hasta)
    with db() as c:
        rows = c.execute(
            "SELECT categoria, COUNT(*), COALESCE(SUM(monto),0) "
            "FROM egresos_cmc WHERE fecha>=? AND fecha<? "
            "GROUP BY categoria ORDER BY 3 DESC",
            (desde, fin_excl),
        ).fetchall()
    detalle = [{"categoria": cat, "n": n, "monto": int(m or 0)} for cat, n, m in rows]
    return {
        "detalle": detalle,
        "total": sum(d["monto"] for d in detalle),
        "_fuente": "egresos_cmc",
        "_periodo": {"desde": desde, "hasta": hasta},
        "_n_filas": sum(d["n"] for d in detalle),
        "_advertencias": [],
        "_no_responde": "EBITDA (usar ebitda_routes), gastos recurrentes con alta fuera del rango.",
    }


# ── Auto-verificación contra cifras conocidas ───────────────────────────────
def _self_check() -> None:
    """Corre solo con acceso real a sessions.db (server). Verifica el módulo
    contra números ya validados por el dueño — si no reproduce estas cifras,
    el bug está en el módulo, no en los números."""
    fallas = []

    v = venta_total("2026-06-01", "2026-06-30")
    print("venta_total junio 2026:", v["total"], "| n_pagos:", v["n_pagos"],
          "| huérfanos:", v["pagos_huerfanos_pct"], "%")
    if v["total"] != 26_039_530:
        fallas.append(f"venta_total jun-2026 esperado 26.039.530, obtenido {v['total']}")

    m = mejor_mes(73)  # Dr. Abarca
    print("mejor_mes Abarca:", m["mejor_mes"])
    mm = m["mejor_mes"] or {}
    if mm.get("mes") != "2025-11" or mm.get("total") != 5_620_090 or mm.get("pacientes_distintos") != 331:
        fallas.append(f"mejor_mes Abarca esperado 2025-11/$5.620.090/331pac, obtenido {mm}")

    o = tasa_orfandad("2026-06-01", "2026-06-30")
    print("tasa_orfandad junio 2026 (CMC-wide):", o["pagos_huerfanos_pct"], "%",
          "advertencias:", o["_advertencias"])

    if fallas:
        print("\nFALLAS:")
        for f in fallas:
            print(" -", f)
        raise SystemExit(1)
    print("\nOK — todas las cifras conocidas coinciden.")


if __name__ == "__main__":
    _self_check()
