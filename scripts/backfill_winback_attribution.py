"""Backfill: atribuir citas a winback_envios y dental_winback_envios.

Ejecutar una vez desde el servidor:
    cd /opt/chatbot-cmc && source venv/bin/activate
    python scripts/backfill_winback_attribution.py

Lógica:
1. Lee bi.winback_envios con cita_id IS NULL y enviado_at >= '2026-05-13'.
2. Para cada phone, busca en bi.fact_citas via dim_paciente.telefono la cita
   creada POSTERIOR al envío del winback.
3. Si fact_citas no tiene match (ETL lag), consulta la API de Medilink
   directamente para obtener citas del paciente.
4. Actualiza cita_id + agendo_at + value_clp donde corresponde.
5. Mismo proceso para bi.dental_winback_envios.
6. Reporta totales.
"""

import asyncio
import logging
import os
import re
import sys
from datetime import datetime, timezone, date

import psycopg2

# Agrega la ruta del proyecto para imports
_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BASE)

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

# ── Valor por especialidad (arancel típico CLP) ───────────────────────────────
_VALUE_CLP = {
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


def _find_cita_in_bi(cur, phone: str, enviado_at: datetime) -> tuple[int | None, str | None, int]:
    """Busca en bi.fact_citas la cita más temprana posterior al envío winback.

    Retorna (cita_id, especialidad, value_clp) o (None, None, 0).
    """
    p9 = _phone_norm(phone)
    if not p9:
        return None, None, 0
    cur.execute(
        """
        SELECT fc.cita_id, de.nombre_especialidad, fc.fecha
        FROM bi.fact_citas fc
        JOIN bi.dim_paciente dp ON dp.paciente_id = fc.paciente_id
        LEFT JOIN bi.dim_especialidad de ON de.especialidad_id = fc.especialidad_id
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
        val = _value_for_esp(esp)
        log.info("  BI match: phone=...%s -> cita_id=%s esp=%s fecha=%s", p9[-6:], cid, esp, fecha)
        return cid, esp, val
    return None, None, 0


async def _find_cita_medilink(phone: str, enviado_at: datetime) -> tuple[int | None, str | None, int]:
    """Consulta Medilink API para obtener citas del paciente identificado por phone.

    Usa buscar_paciente_por_nombre no disponible directamente. En su lugar,
    usa listar_citas_paciente si conocemos el id_paciente.
    Como el phone está en dim_paciente, obtenemos el paciente_id de Medilink
    desde la tabla y llamamos la API.
    """
    try:
        from medilink import listar_citas_paciente, buscar_paciente
        # Obtener RUT desde dim_paciente para buscar en Medilink
        with psycopg2.connect(**BI_CONN_KWARGS) as conn:
            with conn.cursor() as cur:
                p9 = _phone_norm(phone)
                cur.execute(
                    """
                    SELECT dp.rut, dp.nombre
                    FROM bi.dim_paciente dp
                    WHERE RIGHT(regexp_replace(dp.telefono,'[^0-9]','','g'),9) = %s
                    LIMIT 1
                    """,
                    (p9,),
                )
                row = cur.fetchone()
                if not row:
                    return None, None, 0
                rut, nombre = row

        if not rut:
            return None, None, 0

        # Buscar paciente en Medilink por RUT
        paciente = await buscar_paciente(rut)
        if not paciente or not paciente.get("id"):
            return None, None, 0

        id_paciente = paciente["id"]
        citas = await listar_citas_paciente(id_paciente)
        if not citas:
            return None, None, 0

        # Filtrar citas creadas/programadas después del envío winback
        enviado_date = enviado_at.date() if hasattr(enviado_at, 'date') else date.fromisoformat(str(enviado_at)[:10])
        candidatos = []
        for c in citas:
            fecha_str = c.get("fecha", "") or ""
            try:
                # Medilink fecha DD/MM/YYYY
                if "/" in fecha_str:
                    parts = fecha_str.split("/")
                    cita_date = date(int(parts[2]), int(parts[1]), int(parts[0]))
                else:
                    cita_date = date.fromisoformat(fecha_str[:10])
                if cita_date >= enviado_date:
                    candidatos.append((c.get("id"), c.get("especialidad"), cita_date))
            except Exception:
                continue

        if not candidatos:
            return None, None, 0

        # La más temprana
        candidatos.sort(key=lambda x: x[2])
        cid, esp, cita_date = candidatos[0]
        if cid:
            val = _value_for_esp(esp)
            log.info("  Medilink match: phone=...%s -> cita_id=%s esp=%s fecha=%s",
                     _phone_norm(phone)[-6:], cid, esp, cita_date)
            return int(cid), esp, val

    except Exception as e:
        log.warning("  Medilink lookup falló phone=...%s: %s", _phone_norm(phone)[-6:], e)
    return None, None, 0


async def _backfill_table(table: str) -> dict:
    """Ejecuta el backfill para una tabla winback."""
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
        log.info("  Procesando env_id=%d tel=...%s enviado=%s", env_id, tel[-6:], enviado_at)

        cita_id = None
        esp = None
        value_clp = 0

        # Paso 1: buscar en BI fact_citas
        with psycopg2.connect(**BI_CONN_KWARGS) as conn:
            with conn.cursor() as cur:
                cita_id, esp, value_clp = _find_cita_in_bi(cur, tel, enviado_at)

        if cita_id:
            stats["atribuidos_bi"] += 1
        else:
            # Paso 2: consultar Medilink API
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
            log.warning("    -> SIN MATCH (requiere sync ETL o revisión manual)")

    return stats


async def main():
    log.info("=== Backfill winback attribution ===")

    # Verificar que value_clp existe en dental_winback_envios (ya migrado)
    with psycopg2.connect(**BI_CONN_KWARGS) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_schema = 'bi'
                  AND table_name = 'dental_winback_envios'
                  AND column_name = 'value_clp'
                """,
            )
            if not cur.fetchone():
                cur.execute(
                    "ALTER TABLE bi.dental_winback_envios ADD COLUMN IF NOT EXISTS value_clp INTEGER"
                )
                conn.commit()
                log.info("Columna value_clp agregada a dental_winback_envios")

    # Backfill winback_envios
    stats_wb = await _backfill_table("winback_envios")
    log.info("[winback_envios] resultado: %s", stats_wb)

    # Backfill dental_winback_envios
    stats_dw = await _backfill_table("dental_winback_envios")
    log.info("[dental_winback_envios] resultado: %s", stats_dw)

    total_atribuidos = stats_wb["atribuidos_bi"] + stats_wb["atribuidos_medilink"] + \
                       stats_dw["atribuidos_bi"] + stats_dw["atribuidos_medilink"]

    log.info("=== RESUMEN ===")
    log.info("winback_envios:        intentados=%d atribuidos=%d sin_match=%d",
             stats_wb["intentados"],
             stats_wb["atribuidos_bi"] + stats_wb["atribuidos_medilink"],
             stats_wb["sin_match"])
    log.info("dental_winback_envios: intentados=%d atribuidos=%d sin_match=%d",
             stats_dw["intentados"],
             stats_dw["atribuidos_bi"] + stats_dw["atribuidos_medilink"],
             stats_dw["sin_match"])
    log.info("Total atribuidos: %d", total_atribuidos)

    # Verificación final: cuántas filas tienen cita_id NOT NULL
    with psycopg2.connect(**BI_CONN_KWARGS) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM bi.winback_envios WHERE cita_id IS NOT NULL"
            )
            wb_total = cur.fetchone()[0]
            cur.execute(
                "SELECT COUNT(*) FROM bi.dental_winback_envios WHERE cita_id IS NOT NULL"
            )
            dw_total = cur.fetchone()[0]

    log.info("Post-backfill: winback_envios con cita_id NOT NULL: %d", wb_total)
    log.info("Post-backfill: dental_winback_envios con cita_id NOT NULL: %d", dw_total)


if __name__ == "__main__":
    asyncio.run(main())
