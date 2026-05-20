"""Backfill: atribuir citas a winback_envios y dental_winback_envios.

Ejecutar una vez desde el servidor:
    cd /opt/chatbot-cmc && source venv/bin/activate
    python scripts/backfill_winback_attribution.py

Lógica:
1. Lee bi.winback_envios con cita_id IS NULL y enviado_at >= '2026-05-13'.
2. Para cada phone, busca en bi.fact_citas via dim_paciente.telefono la cita
   creada POSTERIOR al envío del winback (cubre casos con ETL al día).
3. Si no hay match (ETL lag), consulta la API de Medilink por RUT del paciente.
4. Actualiza cita_id + agendo_at + value_clp donde corresponde.
5. Mismo proceso para bi.dental_winback_envios.
6. Reporta totales.
"""

import asyncio
import json
import logging
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlencode

import httpx
import psycopg2

# ── Cargar .env del proyecto ──────────────────────────────────────────────────
_BASE = Path(__file__).parent.parent.resolve()
_DOTENV = _BASE / ".env"
if _DOTENV.exists():
    for _line in _DOTENV.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

sys.path.insert(0, str(_BASE))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("backfill_winback")

# ── Conexión BI ───────────────────────────────────────────────────────────────
BI_CONN_KWARGS = dict(
    host=os.getenv("BI_DB_HOST", "127.0.0.1"),
    port=int(os.getenv("BI_DB_PORT", "5432")),
    dbname=os.getenv("BI_DB_NAME", "health_bi"),
    user=os.getenv("BI_DB_USER", "health_user"),
    password=os.getenv("BI_DB_PASSWORD", "password123"),
)

# ── Medilink API ──────────────────────────────────────────────────────────────
_ML_BASE   = os.getenv("MEDILINK_BASE_URL", "https://api.medilink2.healthatom.com/api/v5")
_ML_TOKEN  = os.getenv("MEDILINK_TOKEN", "")
_ML_HDRS   = {"Authorization": f"Token {_ML_TOKEN}"}
_ML_SUC    = int(os.getenv("MEDILINK_SUCURSAL", "1"))

# ── Valor por especialidad (arancel típico CLP) ───────────────────────────────
_VALUE_CLP: dict[str, int] = {
    "medicina general": 14000,
    "medicina familiar": 14000,
    "medicina familiar/general": 14000,
    "otorrinolaringología": 30000,
    "cardiología": 35000,
    "traumatología": 30000,
    "ginecología": 30000,
    "ecografía gineco-obstétrica": 25000,
    "gastroenterología": 30000,
    "odontología": 18000,
    "ortodoncia": 18000,
    "endodoncia": 18000,
    "implantología": 18000,
    "estética facial": 18000,
    "masoterapia": 12000,
    "kinesiología": 12000,
    "nutrición": 18000,
    "psicología": 25000,
    "fonoaudiología": 18000,
    "matrona": 18000,
    "podología": 10000,
    "ecografía": 25000,
}
_DEFAULT_VALUE = 15000


def _phone_norm(phone: str) -> str:
    return re.sub(r'\D', '', phone or '')[-9:]


def _value_for_esp(esp: str | None) -> int:
    if not esp:
        return _DEFAULT_VALUE
    return _VALUE_CLP.get(esp.lower().strip(), _DEFAULT_VALUE)


def _rut_clean(rut: str) -> str:
    """Limpia RUT a formato sin puntos ni guión: ej. '9442579-4' -> '94425794'."""
    return "".join(c for c in (rut or "").upper() if c.isalnum())


def _rut_body(rut: str) -> str:
    """Retorna solo el cuerpo del RUT (sin DV) para búsqueda con lk en Medilink."""
    clean = _rut_clean(rut)
    return clean[:-1] if len(clean) > 1 else clean


def _parse_medilink_date(fecha_str: str) -> date | None:
    """Parsea fecha Medilink DD/MM/YYYY o YYYY-MM-DD."""
    try:
        if "/" in fecha_str:
            parts = fecha_str.split("/")
            return date(int(parts[2]), int(parts[1]), int(parts[0]))
        return date.fromisoformat(fecha_str[:10])
    except Exception:
        return None


# ── Búsqueda en BI (fact_citas) ───────────────────────────────────────────────
def _find_cita_in_bi(phone: str, enviado_at) -> tuple[int | None, str | None, int]:
    """Busca en bi.fact_citas la cita más temprana posterior al envío winback."""
    p9 = _phone_norm(phone)
    if not p9:
        return None, None, 0
    try:
        with psycopg2.connect(**BI_CONN_KWARGS) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT fc.cita_id, de.nombre, fc.fecha
                    FROM bi.fact_citas fc
                    JOIN bi.dim_paciente dp ON dp.paciente_id = fc.paciente_id
                    LEFT JOIN bi.dim_especialidad de
                           ON de.especialidad_id = fc.especialidad_id
                    WHERE RIGHT(regexp_replace(dp.telefono,'[^0-9]','','g'),9) = %s
                      AND fc.etl_updated_at > %s
                    ORDER BY fc.cita_id ASC
                    LIMIT 1
                    """,
                    (p9, enviado_at),
                )
                row = cur.fetchone()
                if row:
                    cid, esp, fecha = row
                    log.info("  BI match: phone=...%s -> cita_id=%s esp=%s fecha=%s",
                             p9[-6:], cid, esp, fecha)
                    return cid, esp, _value_for_esp(esp)
    except Exception as e:
        log.warning("  _find_cita_in_bi error phone=...%s: %s", p9[-6:], e)
    return None, None, 0


# ── Búsqueda en Medilink API ──────────────────────────────────────────────────
async def _find_cita_medilink(phone: str, enviado_at) -> tuple[int | None, str | None, int]:
    """Consulta Medilink por RUT (obtenido de dim_paciente) para hallar citas recientes."""
    p9 = _phone_norm(phone)
    if not p9:
        return None, None, 0

    # Obtener RUT desde dim_paciente (múltiples pacientes con mismo phone → probar todos)
    try:
        with psycopg2.connect(**BI_CONN_KWARGS) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT DISTINCT dp.rut
                    FROM bi.dim_paciente dp
                    WHERE RIGHT(regexp_replace(dp.telefono,'[^0-9]','','g'),9) = %s
                      AND dp.rut IS NOT NULL AND dp.rut != ''
                    ORDER BY dp.rut
                    """,
                    (p9,),
                )
                ruts = [r[0] for r in cur.fetchall()]
    except Exception as e:
        log.warning("  dim_paciente lookup error phone=...%s: %s", p9[-6:], e)
        return None, None, 0

    if not ruts:
        log.debug("  No RUT en dim_paciente para phone=...%s", p9[-6:])
        return None, None, 0

    enviado_date = enviado_at.date() if hasattr(enviado_at, 'date') else date.fromisoformat(str(enviado_at)[:10])

    for rut in ruts:
        rut_body = _rut_body(rut)
        if not rut_body:
            continue
        await asyncio.sleep(2)  # Respetar rate limit Medilink (~30 req/min)
        # Formato Medilink: q={"rut":{"lk":"XXXXXXX"}}
        q_param = json.dumps({"rut": {"lk": rut_body}})
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(
                    f"{_ML_BASE}/pacientes",
                    headers=_ML_HDRS,
                    params={"q": q_param},
                )
            if r.status_code != 200:
                log.debug("  Medilink pacientes rut=%s HTTP %d: %s",
                          rut, r.status_code, r.text[:100])
                continue
            data = r.json().get("data", [])
            if not data:
                continue
            id_paciente = data[0].get("id")
            if not id_paciente:
                continue

            # Buscar citas por RUT directamente (el endpoint que funciona)
            hoy_str = datetime.now().date().strftime("%Y-%m-%d")
            # Buscar sin filtro de fecha para capturar citas pasadas desde envío
            q_citas = json.dumps({
                "rut": {"eq": _rut_clean(rut)},
                "estado_anulacion": {"eq": 0},
            })
            async with httpx.AsyncClient(timeout=10) as client:
                rc = await client.get(
                    f"{_ML_BASE}/citas",
                    headers=_ML_HDRS,
                    params={"q": q_citas},
                )
            if rc.status_code != 200:
                log.debug("  Medilink citas HTTP %d rut=%s", rc.status_code, rut)
                continue

            citas = rc.json().get("data", [])
            candidatos = []
            for c in citas:
                fecha_str = c.get("fecha", "") or ""
                cita_date = _parse_medilink_date(fecha_str)
                if cita_date and cita_date >= enviado_date:
                    cid = c.get("id")
                    esp_obj = c.get("especialidad") or {}
                    esp = esp_obj.get("nombre") if isinstance(esp_obj, dict) else str(esp_obj)
                    if cid:
                        candidatos.append((int(cid), esp, cita_date))

            if candidatos:
                candidatos.sort(key=lambda x: x[2])
                cid, esp, cita_date = candidatos[0]
                log.info("  Medilink match: phone=...%s rut=%s -> cita_id=%s esp=%s fecha=%s",
                         p9[-6:], rut, cid, esp, cita_date)
                return cid, esp, _value_for_esp(esp)

        except Exception as e:
            log.warning("  Medilink lookup rut=%s error: %s", rut, e)
            continue

    return None, None, 0


# ── Backfill de una tabla ─────────────────────────────────────────────────────
async def _backfill_table(table: str) -> dict:
    stats = {"intentados": 0, "atribuidos_bi": 0, "atribuidos_medilink": 0, "sin_match": 0}

    with psycopg2.connect(**BI_CONN_KWARGS) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT id, telefono, enviado_at
                FROM bi.{table}
                WHERE cita_id IS NULL
                  AND enviado_at >= '2026-05-13'
                ORDER BY enviado_at
                """,
            )
            envios = cur.fetchall()

    log.info("[%s] filas sin atribución: %d", table, len(envios))

    for env_id, tel, enviado_at in envios:
        stats["intentados"] += 1
        log.info("  env_id=%d tel=...%s enviado=%s", env_id, tel[-6:], enviado_at)

        cita_id, esp, value_clp = _find_cita_in_bi(tel, enviado_at)
        if cita_id:
            stats["atribuidos_bi"] += 1
        else:
            cita_id, esp, value_clp = await _find_cita_medilink(tel, enviado_at)
            if cita_id:
                stats["atribuidos_medilink"] += 1

        if cita_id:
            with psycopg2.connect(**BI_CONN_KWARGS) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"""
                        UPDATE bi.{table}
                           SET cita_id   = %s,
                               agendo_at = NOW(),
                               value_clp = %s
                         WHERE id = %s
                           AND cita_id IS NULL
                        """,
                        (cita_id, value_clp, env_id),
                    )
                conn.commit()
            log.info("    -> ATRIBUIDO cita_id=%s value_clp=%s", cita_id, value_clp)
        else:
            stats["sin_match"] += 1
            log.warning("    -> SIN MATCH (ETL lag o cita vía recepción sin RUT bot)")

    return stats


# ── Main ──────────────────────────────────────────────────────────────────────
async def main():
    log.info("=== Backfill winback attribution ===")
    log.info("Medilink base: %s | Token: ...%s", _ML_BASE, _ML_TOKEN[-8:] if _ML_TOKEN else "NONE")

    # Asegurar columnas en ambas tablas
    with psycopg2.connect(**BI_CONN_KWARGS) as conn:
        with conn.cursor() as cur:
            for tbl in ("winback_envios", "dental_winback_envios"):
                cur.execute(
                    f"ALTER TABLE bi.{tbl} ADD COLUMN IF NOT EXISTS cita_id INTEGER"
                )
                cur.execute(
                    f"ALTER TABLE bi.{tbl} ADD COLUMN IF NOT EXISTS agendo_at TIMESTAMPTZ"
                )
                cur.execute(
                    f"ALTER TABLE bi.{tbl} ADD COLUMN IF NOT EXISTS value_clp INTEGER"
                )
        conn.commit()
    log.info("Columnas verificadas OK")

    stats_wb = await _backfill_table("winback_envios")
    log.info("[winback_envios] resultado: %s", stats_wb)

    stats_dw = await _backfill_table("dental_winback_envios")
    log.info("[dental_winback_envios] resultado: %s", stats_dw)

    total = (stats_wb["atribuidos_bi"] + stats_wb["atribuidos_medilink"] +
             stats_dw["atribuidos_bi"] + stats_dw["atribuidos_medilink"])

    log.info("=== RESUMEN ===")
    log.info("winback_envios:        intentados=%d  atribuidos=%d  sin_match=%d",
             stats_wb["intentados"],
             stats_wb["atribuidos_bi"] + stats_wb["atribuidos_medilink"],
             stats_wb["sin_match"])
    log.info("dental_winback_envios: intentados=%d  atribuidos=%d  sin_match=%d",
             stats_dw["intentados"],
             stats_dw["atribuidos_bi"] + stats_dw["atribuidos_medilink"],
             stats_dw["sin_match"])
    log.info("Total atribuidos: %d", total)

    # Verificación final
    with psycopg2.connect(**BI_CONN_KWARGS) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM bi.winback_envios WHERE cita_id IS NOT NULL")
            wb_n = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM bi.dental_winback_envios WHERE cita_id IS NOT NULL")
            dw_n = cur.fetchone()[0]

    log.info("Post-backfill: winback_envios con cita_id NOT NULL = %d", wb_n)
    log.info("Post-backfill: dental_winback_envios con cita_id NOT NULL = %d", dw_n)


# ── Patch directo: casos confirmados por evidencia en producción ──────────────
# Estos registros fueron agendados vía recepción directamente en Medilink
# (sin pasar por el bot ni el panel admin), por lo que la API de citas solo
# los devuelve como histórico sin RUT y no se pueden cruzar automáticamente.
# Actualizamos con los IDs confirmados en la evidencia original de esta tarea.
_DIRECT_PATCHES = [
    # (env_id, winback_table, cita_id, value_clp)
    (9, "winback_envios", 57057, 14000),   # 56941870898 — medicina general
    (4, "winback_envios", 57070, 14000),   # 56935679371 — medicina general
]


def _apply_direct_patches():
    applied = 0
    with psycopg2.connect(**BI_CONN_KWARGS) as conn:
        with conn.cursor() as cur:
            for env_id, table, cita_id, value_clp in _DIRECT_PATCHES:
                cur.execute(
                    f"""
                    UPDATE bi.{table}
                       SET cita_id   = %s,
                           agendo_at = NOW(),
                           value_clp = %s
                     WHERE id = %s AND cita_id IS NULL
                    """,
                    (cita_id, value_clp, env_id),
                )
                if cur.rowcount:
                    log.info("direct patch: env_id=%d table=%s -> cita_id=%s", env_id, table, cita_id)
                    applied += cur.rowcount
        conn.commit()
    return applied


if __name__ == "__main__":
    asyncio.run(main())
    patched = _apply_direct_patches()
    if patched:
        log.info("Direct patches aplicados: %d", patched)
