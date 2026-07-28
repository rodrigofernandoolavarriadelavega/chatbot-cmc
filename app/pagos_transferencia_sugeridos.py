"""pagos_transferencia_sugeridos.py — Prellenado sugerido de pagos por transferencia.

Qué resuelve: hoy la recepción tipea a mano cada transferencia en el módulo
de Pagos (busca al paciente, escribe el monto, elige "Transferencia"). El
banco YA mandó un correo con nombre, monto, fecha y hora — es información
que el sistema ya tiene y que se está re-tecleando.

Este módulo cruza cada correo de banco NUEVO (fecha = hoy) contra los
pacientes con atención HOY que aún no tienen pago registrado
(`pagos_cmc.creado_por='prellenar'`, fila que crea `POST /pagos/prellenar`
al abrir la agenda del día — ver `app/pagos_routes.py`, NO se toca ese
archivo desde acá, solo se LEE la tabla que ya llena) y arma una SUGERENCIA.

REGLA DURA que no se negocia: el sistema NUNCA registra plata por su cuenta.
- `registrar_sugerencia_si_aplica()` solo INSERTA en `pagos_sugeridos`
  (tabla propia, separada de `pagos_cmc`) — nunca escribe en `pagos_cmc`.
- Confirmar (`confirmar_sugerencia`) es la ÚNICA función que escribe en
  `pagos_cmc`, y solo se llama desde un clic explícito de un humano
  (endpoint `POST .../confirmar`, nunca desde un cron).
- Si nadie confirma, la sugerencia queda `pendiente` para siempre — no
  desaparece sola, no se auto-aplica por timeout.

Criterio de cruce (triple filtro, ninguno es absoluto):
  1. Atención hoy — el candidato debe tener una fila `pagos_cmc` de HOY
     creada por el prellenado de agenda (`creado_por='prellenar'`) y AÚN sin
     cobrar (`copago` en 0/NULL, `metodo_pago` vacío).
  2. Nombre — similitud (Jaccard de tokens, mismo criterio que
     `conciliacion_transferencias._similitud_nombre`, reusada de ahí) entre
     quien transfiere y el paciente. Quien transfiere NO siempre es el
     paciente (hijo paga por la madre) — por eso el nombre es una SEÑAL de
     confianza, nunca un filtro que descarte candidatos.
  3. Monto — si Medilink ya trae el arancel real de la atención
     (`pagos_cmc.monto_medilink`, distinto de 0) y coincide exactamente con
     el monto transferido, sube la confianza del candidato aunque el nombre
     no coincida. La mayoría de las atenciones NO traen este dato (fonasa,
     o Medilink no lo entregó) — cuando no está, el criterio queda en
     nombre + atención del día únicamente.

Cuando dos o más pacientes de hoy quedan como candidatos igual de válidos
(el caso más frecuente: varios $15.000 el mismo día, mismo horario de
prestaciones estándar) NO se elige por adivinanza — se listan todos y la
recepción, que conoce a la gente, decide con un clic.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

log = logging.getLogger("pagos_transferencia_sugeridos")

_CLT = ZoneInfo("America/Santiago")

# Bajo este umbral de similitud de nombre, el candidato NO se descarta —
# solo se etiqueta "nombre_distinto" en vez de "nombre_fuerte" (ver
# docstring del módulo: el nombre es señal, no filtro).
_SIM_FUERTE = 0.5


def ensure_pagos_sugeridos_table() -> None:
    from session import db
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pagos_sugeridos (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                uid_email           INTEGER NOT NULL UNIQUE,
                banco               TEXT,
                nombre_transfiere   TEXT,
                monto               INTEGER,
                fecha               TEXT,
                hora                TEXT,
                num_operacion       TEXT,
                candidatos_json     TEXT NOT NULL,
                estado              TEXT DEFAULT 'pendiente',
                elegido_pago_cmc_id INTEGER,
                resuelto_por        TEXT,
                resuelto_at         TEXT,
                creado_at           TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_pagos_sug_estado ON pagos_sugeridos(estado, fecha)")
        conn.commit()


# ── Candidatos ───────────────────────────────────────────────────────────

def _candidatos_pendientes_dia(fecha: str) -> list[dict]:
    """Pacientes con atención `fecha` que YA están en `pagos_cmc` (vía
    prellenado de agenda) pero AÚN no tienen pago registrado. Fuente única:
    la misma tabla que usa el módulo de Pagos — no se re-consulta Medilink
    acá (ya lo hizo `POST /pagos/prellenar` al abrir la agenda del día)."""
    from session import db
    with db() as c:
        rows = c.execute("""
            SELECT id, paciente_nombre, profesional, id_profesional, id_cita,
                   area, monto_medilink, id_paciente
            FROM pagos_cmc
            WHERE fecha = ?
              AND creado_por = 'prellenar'
              AND bloqueado = 0
              AND (copago IS NULL OR copago = 0)
              AND (metodo_pago IS NULL OR metodo_pago = '')
        """, (fecha,)).fetchall()
    return [dict(r) for r in rows]


def _candidatos_todos_dia(fecha: str) -> list[dict]:
    """Todos los pacientes con una fila `pagos_cmc` de esa fecha, sin
    importar si ya se cobraron o con qué método — usado SOLO para el
    backtest histórico (`medir_cobertura_historica`), donde la fecha ya
    pasó y el estado 'sin cobrar' del momento ya no existe."""
    from session import db
    with db() as c:
        rows = c.execute("""
            SELECT id, paciente_nombre, profesional, id_profesional, id_cita,
                   area, monto_medilink, id_paciente
            FROM pagos_cmc
            WHERE fecha = ?
        """, (fecha,)).fetchall()
    return [dict(r) for r in rows]


def _match_candidatos(nombre_transfiere: str, monto: int, candidatos: list[dict]) -> list[dict]:
    """Devuelve los candidatos ordenados por confianza desc., cada uno con
    su score y etiqueta. No decide — solo ordena y etiqueta."""
    from conciliacion_transferencias import _similitud_nombre
    out = []
    for c in candidatos:
        sim = _similitud_nombre(nombre_transfiere, c.get("paciente_nombre"))
        monto_ok = bool(c.get("monto_medilink")) and c["monto_medilink"] == monto
        if sim >= _SIM_FUERTE:
            etiqueta = "nombre_fuerte"
        elif monto_ok:
            etiqueta = "monto_coincide"
        else:
            etiqueta = "nombre_distinto"
        score = sim + (1.0 if monto_ok else 0.0)
        out.append({**c, "sim": round(sim, 2), "monto_ok": monto_ok,
                    "etiqueta": etiqueta, "score": score})
    out.sort(key=lambda x: -x["score"])
    return out


def _elegir_ganador_unico(candidatos_rankeados: list[dict]) -> dict | None:
    """Si hay un único candidato con el score máximo (sin empate) Y ese
    score es > 0 (algo de nombre o de monto lo respalda) O es el ÚNICO
    candidato del día (nadie más con quien confundirse), se puede sugerir
    un ganador único. En cualquier otro caso (empate, o >1 candidato con
    score 0 y ninguna otra señal), NO se elige — queda ambiguo."""
    if not candidatos_rankeados:
        return None
    if len(candidatos_rankeados) == 1:
        return candidatos_rankeados[0]
    top = candidatos_rankeados[0]["score"]
    empatados = [c for c in candidatos_rankeados if c["score"] == top]
    if len(empatados) == 1 and top > 0:
        return empatados[0]
    return None


def generar_sugerencia(nombre_transfiere: str, monto: int, fecha: str) -> dict:
    """Núcleo puro de matching — recibe los datos ya parseados de un correo
    y devuelve {candidatos: [...], ganador: dict|None}. No toca IMAP ni
    inserta nada; lo llaman tanto el poller en vivo como el backtest."""
    candidatos = _candidatos_pendientes_dia(fecha)
    rankeados = _match_candidatos(nombre_transfiere, monto, candidatos)
    ganador = _elegir_ganador_unico(rankeados)
    return {"candidatos": rankeados, "ganador": ganador}


# ── Alta de sugerencia (llamada desde el poller, solo INSERT) ─────────────

def registrar_sugerencia_si_aplica(uid: int, banco: str, nombre_transfiere: str,
                                    monto: int, fecha: str, hora: str | None,
                                    num_operacion: str | None) -> str:
    """Se llama tras guardar un correo de banco NUEVO. Si la fecha del
    correo es HOY (hora de Chile) y hay al menos un candidato, inserta una
    fila `pendiente` en `pagos_sugeridos`. Nunca escribe en `pagos_cmc`.
    Devuelve 'creada' | 'sin_candidatos' | 'no_es_hoy' | 'ya_existia'."""
    hoy = datetime.now(_CLT).strftime("%Y-%m-%d")
    if fecha != hoy:
        return "no_es_hoy"

    from session import db
    ensure_pagos_sugeridos_table()

    resultado = generar_sugerencia(nombre_transfiere, monto, fecha)
    if not resultado["candidatos"]:
        return "sin_candidatos"

    with db() as c:
        try:
            c.execute("""
                INSERT INTO pagos_sugeridos
                    (uid_email, banco, nombre_transfiere, monto, fecha, hora,
                     num_operacion, candidatos_json, estado)
                VALUES (?,?,?,?,?,?,?,?, 'pendiente')
            """, (uid, banco, nombre_transfiere, monto, fecha, hora, num_operacion,
                  json.dumps(resultado["candidatos"], ensure_ascii=False)))
            c.commit()
        except Exception as e:
            # UNIQUE(uid_email) — ya existía, no es un error real.
            if "UNIQUE" in str(e):
                return "ya_existia"
            log.error("registrar_sugerencia_si_aplica: fallo insertando uid=%s: %s", uid, e)
            return "sin_candidatos"
    return "creada"


# ── Confirmar / descartar (las ÚNICAS funciones que escriben pagos_cmc) ───

def confirmar_sugerencia(sugerencia_id: int, pago_cmc_id: int, resuelto_por: str) -> dict:
    """Aplica la sugerencia elegida por un humano: actualiza LA fila
    `pagos_cmc` ya prellenada (nunca crea una fila nueva — ya existe desde
    el prellenado de agenda). Condición defensiva: solo si esa fila SIGUE
    sin cobrar (`bloqueado=0` y `copago` en 0/NULL) — si alguien ya la cobró
    por otro medio entre que se sugirió y se confirmó, no se pisa."""
    from session import db
    ensure_pagos_sugeridos_table()
    with db() as c:
        sug = c.execute(
            "SELECT * FROM pagos_sugeridos WHERE id = ?", (sugerencia_id,)
        ).fetchone()
        if not sug:
            return {"ok": False, "error": "sugerencia no encontrada"}
        sug = dict(sug)
        if sug["estado"] != "pendiente":
            return {"ok": False, "error": f"sugerencia ya estaba en estado '{sug['estado']}'"}

        candidatos = json.loads(sug["candidatos_json"])
        if not any(c_["id"] == pago_cmc_id for c_ in candidatos):
            return {"ok": False, "error": "ese pago_cmc_id no es uno de los candidatos de esta sugerencia"}

        cur = c.execute("""
            UPDATE pagos_cmc
            SET metodo_pago = 'transferencia',
                copago = ?,
                codigo_transferencia = ?,
                match_confianza = 'transferencia_sugerida',
                updated_at = datetime('now')
            WHERE id = ? AND bloqueado = 0 AND (copago IS NULL OR copago = 0)
        """, (sug["monto"], sug["num_operacion"] or "", pago_cmc_id))
        if cur.rowcount == 0:
            c.execute(
                "UPDATE pagos_sugeridos SET estado='descartado', resuelto_por=?, "
                "resuelto_at=datetime('now') WHERE id=?",
                (resuelto_por, sugerencia_id),
            )
            c.commit()
            return {"ok": False, "error": "esa atención ya fue cobrada por otro medio entre tanto — "
                                           "sugerencia descartada automáticamente"}

        c.execute("""
            UPDATE pagos_sugeridos
            SET estado='confirmado', elegido_pago_cmc_id=?, resuelto_por=?,
                resuelto_at=datetime('now')
            WHERE id=?
        """, (pago_cmc_id, resuelto_por, sugerencia_id))
        c.commit()
    return {"ok": True}


def descartar_sugerencia(sugerencia_id: int, resuelto_por: str) -> dict:
    from session import db
    ensure_pagos_sugeridos_table()
    with db() as c:
        cur = c.execute("""
            UPDATE pagos_sugeridos
            SET estado='descartado', resuelto_por=?, resuelto_at=datetime('now')
            WHERE id=? AND estado='pendiente'
        """, (resuelto_por, sugerencia_id))
        c.commit()
    return {"ok": cur.rowcount > 0}


def listar_pendientes() -> list[dict]:
    from session import db
    ensure_pagos_sugeridos_table()
    with db() as c:
        rows = c.execute("""
            SELECT id, uid_email, banco, nombre_transfiere, monto, fecha, hora,
                   num_operacion, candidatos_json, creado_at
            FROM pagos_sugeridos
            WHERE estado = 'pendiente'
            ORDER BY creado_at DESC
        """).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["candidatos"] = json.loads(d.pop("candidatos_json"))
        out.append(d)
    return out


# ── Backtest histórico: ¿qué % de las transferencias del día habría
#    calzado sin ambigüedad contra un paciente atendido ese día? ──────────

def medir_cobertura_historica(dias: int = 60) -> dict:
    """Mide, para cada correo de banco de los últimos `dias`, si habría
    calzado sin ambigüedad contra la agenda de ESE día (aproximación: usa
    TODAS las filas `pagos_cmc` de esa fecha, sin filtrar por 'aún sin
    cobrar', porque para fechas pasadas ya todas se cobraron de un modo u
    otro — es la mejor aproximación disponible al estado real del día sin
    poder viajar en el tiempo). No es el mismo criterio EXACTO que el
    prellenado en vivo (que exige `creado_por='prellenar'` sin cobrar) —
    se documenta la diferencia para no sobre-prometer el número."""
    from datetime import date, timedelta
    from session import db
    ensure_pagos_sugeridos_table()

    hasta = datetime.now(_CLT).date()
    desde = hasta - timedelta(days=dias)

    with db() as c:
        emails = c.execute("""
            SELECT uid, banco, nombre_pagador AS nombre_transfiere, monto, fecha
            FROM transferencias_banco
            WHERE fecha BETWEEN ? AND ?
        """, (desde.isoformat(), hasta.isoformat())).fetchall()
    emails = [dict(r) for r in emails]

    if not emails:
        return {"dias": dias, "total_correos": 0, "nota": "sin correos de banco en el rango"}

    # Cachear candidatos por fecha para no re-consultar la DB por cada correo.
    cache_dia: dict[str, list[dict]] = {}
    calza_unico = calza_nombre_distinto_unico = ambiguo = sin_candidatos = 0

    for e in emails:
        f = e["fecha"]
        if f not in cache_dia:
            cache_dia[f] = _candidatos_todos_dia(f)
        candidatos = cache_dia[f]
        if not candidatos:
            sin_candidatos += 1
            continue
        rankeados = _match_candidatos(e["nombre_transfiere"], e["monto"], candidatos)
        ganador = _elegir_ganador_unico(rankeados)
        if ganador is None:
            ambiguo += 1
        elif ganador["etiqueta"] == "nombre_fuerte":
            calza_unico += 1
        else:
            calza_nombre_distinto_unico += 1

    total = len(emails)
    return {
        "dias": dias,
        "total_correos": total,
        "calza_nombre_fuerte": calza_unico,
        "calza_nombre_distinto_unico": calza_nombre_distinto_unico,
        "calzaria_total": calza_unico + calza_nombre_distinto_unico,
        "pct_calzaria": round(100 * (calza_unico + calza_nombre_distinto_unico) / total, 1),
        "ambiguo": ambiguo,
        "pct_ambiguo": round(100 * ambiguo / total, 1),
        "sin_candidatos_ese_dia": sin_candidatos,
        "pct_sin_candidatos": round(100 * sin_candidatos / total, 1),
        "nota": "aproximación: usa TODAS las filas pagos_cmc de la fecha (ya cobradas o no), "
                "no el subconjunto exacto 'aún sin cobrar' que existiría en vivo ese día.",
    }
