"""Custom Audiences Sync — exporta audiencias de pacientes CMC a Meta Marketing API.

Crea/actualiza 3 Custom Audiences en la cuenta publicitaria del CMC:
  1. "CMC Atendidos 90d" — pacientes atendidos en los últimos 90 días (warm)
  2. "CMC No vuelven 365d" — pacientes sin atención en +365 días (reactivación)
  3. "CMC Ortodoncia activos" — pacientes con atención de ortodoncia en los últimos 180 días

Privacidad:
  - SHA256 del phone normalizado (E.164 sin + → sha256 hex lowercase)
  - No se envían nombres ni RUTs a Meta directamente (solo hashes)

Fuente de datos: bi.v_winback_cohortes (Postgres BI) + bi.fact_atenciones + bi.dim_paciente

Compliance:
  - Solo phones con es_activo=true y telefono no nulo
  - Excluye opt-outs de bi.opt_outs_marketing

Referencia: https://developers.facebook.com/docs/marketing-api/audiences
"""

import asyncio
import hashlib
import logging
import os
import time
from datetime import date

import httpx
import psycopg2

log = logging.getLogger("bot")

# ── Config ────────────────────────────────────────────────────────────────────
_META_ACCESS_TOKEN = os.getenv("META_ACCESS_TOKEN", "")
_META_AD_ACCOUNT_ID = os.getenv("META_AD_ACCOUNT_ID", "act_220608142267129")
_META_API_VERSION = "v22.0"
_META_API_BASE = f"https://graph.facebook.com/{_META_API_VERSION}"

_BI_HOST     = os.getenv("BI_DB_HOST", "127.0.0.1")
_BI_PORT     = int(os.getenv("BI_DB_PORT", "5432"))
_BI_NAME     = os.getenv("BI_DB_NAME", "health_bi")
_BI_USER     = os.getenv("BI_DB_USER", "health_user")
_BI_PASSWORD = os.getenv("BI_DB_PASSWORD", "password123")


# ── Definición de audiencias ─────────────────────────────────────────────────
AUDIENCIAS = [
    {
        "name":        "CMC Atendidos 90d",
        "description": "Pacientes del Centro Médico Carampangue atendidos en los últimos 90 días. Audiencia warm para remarketing.",
        "query": """
            SELECT DISTINCT p.telefono
            FROM bi.dim_paciente p
            JOIN bi.fact_atenciones fa ON fa.paciente_id = p.paciente_id
            WHERE fa.fecha >= CURRENT_DATE - INTERVAL '90 days'
              AND p.es_activo = true
              AND p.telefono IS NOT NULL AND p.telefono <> ''
              AND NOT EXISTS (
                SELECT 1 FROM bi.opt_outs_marketing oo WHERE oo.phone = p.telefono
              )
        """,
        "lookalike_seed": True,  # crear Lookalike 1% Chile basado en esta
    },
    {
        "name":        "CMC No vuelven 365d",
        "description": "Pacientes del Centro Médico Carampangue sin atención en más de 365 días. Audiencia de reactivación.",
        "query": """
            SELECT wc.telefono
            FROM bi.v_winback_cohortes_contactables wc
            WHERE wc.cohorte = '365d'
        """,
        "lookalike_seed": False,
    },
    {
        "name":        "CMC Ortodoncia activos",
        "description": "Pacientes del Centro Médico Carampangue con tratamiento de ortodoncia activo (últimos 180 días). Super warm.",
        "query": """
            SELECT DISTINCT p.telefono
            FROM bi.dim_paciente p
            JOIN bi.fact_atenciones fa ON fa.paciente_id = p.paciente_id
            JOIN bi.dim_profesional dp ON fa.profesional_id = dp.profesional_id
            JOIN bi.dim_especialidad de ON dp.especialidad_id = de.especialidad_id
            WHERE LOWER(de.nombre) IN ('ortodoncista', 'ortodoncia')
              AND fa.fecha >= CURRENT_DATE - INTERVAL '180 days'
              AND p.es_activo = true
              AND p.telefono IS NOT NULL AND p.telefono <> ''
              AND NOT EXISTS (
                SELECT 1 FROM bi.opt_outs_marketing oo WHERE oo.phone = p.telefono
              )
        """,
        "lookalike_seed": False,
    },
]


# ── Hashing ───────────────────────────────────────────────────────────────────
def _normalize_phone(phone: str) -> str | None:
    """Normaliza teléfono a formato sin + para hashing.
    +569XXXXXXXX → 569XXXXXXXX
    """
    if not phone:
        return None
    digits = "".join(c for c in phone if c.isdigit())
    if not digits:
        return None
    if digits.startswith("56") and len(digits) >= 11:
        return digits[:11]
    if digits.startswith("9") and len(digits) == 9:
        return "56" + digits
    if len(digits) == 8:
        return "569" + digits
    return digits if len(digits) >= 8 else None


def _sha256_phone(phone: str) -> str | None:
    """SHA256 hex lowercase del teléfono normalizado."""
    normalized = _normalize_phone(phone)
    if not normalized:
        return None
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


# ── BI: obtener phones ─────────────────────────────────────────────────────────
def _get_phones_bi(query: str) -> list[str]:
    """Ejecuta query en BI y retorna lista de teléfonos normalizados."""
    try:
        conn = psycopg2.connect(
            host=_BI_HOST, port=_BI_PORT,
            dbname=_BI_NAME, user=_BI_USER, password=_BI_PASSWORD,
            connect_timeout=10,
        )
        with conn.cursor() as cur:
            cur.execute(query)
            phones = []
            for row in cur.fetchall():
                tel = row[0]
                norm = _normalize_phone(str(tel)) if tel else None
                if norm:
                    phones.append(norm)
        conn.close()
        return phones
    except Exception as e:
        log.error("custom_audiences: error consultando BI: %s", e)
        return []


# ── Meta Marketing API ────────────────────────────────────────────────────────
async def _meta_get(url: str, params: dict | None = None) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(url, params={"access_token": _META_ACCESS_TOKEN, **(params or {})})
        r.raise_for_status()
        return r.json()


async def _meta_post(url: str, data: dict) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(url, data={"access_token": _META_ACCESS_TOKEN, **data})
        if r.status_code not in (200, 201):
            log.error("Meta Marketing API %s: %s", r.status_code, r.text[:500])
            r.raise_for_status()
        return r.json()


async def _buscar_audiencia_existente(nombre: str) -> str | None:
    """Busca una Custom Audience por nombre en la cuenta. Retorna ID o None."""
    try:
        data = await _meta_get(
            f"{_META_API_BASE}/{_META_AD_ACCOUNT_ID}/customaudiences",
            params={"fields": "id,name", "limit": "100"},
        )
        for aud in data.get("data", []):
            if aud.get("name") == nombre:
                return aud["id"]
    except Exception as e:
        log.error("custom_audiences: error buscando audiencia '%s': %s", nombre, e)
    return None


async def _crear_audiencia(nombre: str, descripcion: str) -> str | None:
    """Crea una Custom Audience nueva. Retorna ID."""
    try:
        result = await _meta_post(
            f"{_META_API_BASE}/{_META_AD_ACCOUNT_ID}/customaudiences",
            data={
                "name": nombre,
                "description": descripcion,
                "subtype": "CUSTOM",
                "customer_file_source": "USER_PROVIDED_ONLY",
            },
        )
        aud_id = result.get("id")
        log.info("custom_audiences: audiencia '%s' creada id=%s", nombre, aud_id)
        return aud_id
    except Exception as e:
        log.error("custom_audiences: error creando audiencia '%s': %s", nombre, e)
        return None


async def _replace_users(audience_id: str, hashed_phones: list[str]) -> bool:
    """Reemplaza usuarios en una Custom Audience (REPLACE).

    Meta acepta batches de máximo 10.000 hashes. Si hay más, se envían en batches.
    """
    BATCH_SIZE = 10_000
    total_enviados = 0

    # Formatear como schema PHONE_SHA256
    schema = ["PHONE"]

    for i in range(0, len(hashed_phones), BATCH_SIZE):
        batch = hashed_phones[i:i + BATCH_SIZE]
        try:
            result = await _meta_post(
                f"{_META_API_BASE}/{audience_id}/users",
                data={
                    "payload": str({
                        "schema": schema,
                        "data": [[p] for p in batch],
                    }).replace("'", '"'),
                },
            )
            num_invalidos = result.get("num_invalid_entries", 0)
            total_enviados += len(batch)
            log.info(
                "custom_audiences: batch %d/%d enviado a id=%s invalidos=%d",
                i // BATCH_SIZE + 1, (len(hashed_phones) + BATCH_SIZE - 1) // BATCH_SIZE,
                audience_id, num_invalidos,
            )
        except Exception as e:
            log.error("custom_audiences: error enviando batch a id=%s: %s", audience_id, e)
            return False

    log.info("custom_audiences: %d hashes enviados a id=%s", total_enviados, audience_id)
    return True


async def _crear_lookalike(seed_audience_id: str) -> str | None:
    """Crea Lookalike 1% Chile desde la audiencia seed."""
    try:
        result = await _meta_post(
            f"{_META_API_BASE}/{_META_AD_ACCOUNT_ID}/customaudiences",
            data={
                "name": "CMC Lookalike 1% Chile",
                "description": "Lookalike 1% Chile basado en pacientes atendidos últimos 90 días (CMC).",
                "subtype": "LOOKALIKE",
                "origin_audience_id": seed_audience_id,
                "lookalike_spec": str({
                    "ratio": 0.01,
                    "country": "CL",
                }).replace("'", '"'),
            },
        )
        aud_id = result.get("id")
        log.info("custom_audiences: Lookalike 1%% Chile creado id=%s seed=%s", aud_id, seed_audience_id)
        return aud_id
    except Exception as e:
        log.error("custom_audiences: error creando Lookalike: %s", e)
        return None


# ── Orchestrador ───────────────────────────────────────────────────────────────
async def sync_custom_audiences() -> dict:
    """Sincroniza las 3 audiencias CMC con Meta.

    Flujo por audiencia:
    1. Consultar BI para obtener phones
    2. Hashear con SHA256
    3. Buscar si la audiencia ya existe en Meta
    4. Si no existe, crear
    5. REPLACE users (borrar anteriores + agregar nuevos)
    6. Si lookalike_seed y no existe aún, crear Lookalike 1% Chile
    """
    if not _META_ACCESS_TOKEN:
        log.warning("custom_audiences: META_ACCESS_TOKEN vacío — sync omitido")
        return {"status": "no_token"}

    stats = {"audiencias": [], "timestamp": date.today().isoformat()}

    # Verificar si la Lookalike ya existe (para no recrear cada día)
    lookalike_existente = await _buscar_audiencia_existente("CMC Lookalike 1% Chile")

    for aud_def in AUDIENCIAS:
        nombre     = aud_def["name"]
        descripcion = aud_def["description"]
        query      = aud_def["query"]
        make_lookalike = aud_def.get("lookalike_seed", False)

        log.info("custom_audiences: procesando '%s'", nombre)

        # 1. Obtener phones desde BI
        phones = _get_phones_bi(query)
        log.info("custom_audiences: '%s' → %d phones desde BI", nombre, len(phones))

        if not phones:
            log.warning("custom_audiences: 0 phones para '%s' — salteando", nombre)
            stats["audiencias"].append({"name": nombre, "status": "empty", "phones": 0})
            continue

        # 2. Hashear
        hashed = [_sha256_phone(p) for p in phones]
        hashed = [h for h in hashed if h]  # filtrar None

        # 3. Buscar o crear audiencia
        aud_id = await _buscar_audiencia_existente(nombre)
        if not aud_id:
            aud_id = await _crear_audiencia(nombre, descripcion)
        if not aud_id:
            stats["audiencias"].append({"name": nombre, "status": "error_crear"})
            continue

        # 4. Replace users
        ok = await _replace_users(aud_id, hashed)

        aud_stat = {
            "name":     nombre,
            "id":       aud_id,
            "phones":   len(hashed),
            "status":   "ok" if ok else "error_sync",
        }

        # 5. Crear Lookalike si corresponde y no existe
        if make_lookalike and not lookalike_existente and ok:
            ll_id = await _crear_lookalike(aud_id)
            aud_stat["lookalike_id"] = ll_id
            if ll_id:
                lookalike_existente = ll_id  # no crear de nuevo en esta misma corrida

        stats["audiencias"].append(aud_stat)
        log.info("custom_audiences: '%s' sincronizado — %d phones id=%s", nombre, len(hashed), aud_id)

    log.info("custom_audiences: sync completo %s", stats)
    return stats


# ── Entry point para jobs.py ──────────────────────────────────────────────────
async def job_custom_audiences_diario() -> None:
    """Job diario 04:00 CLT para sincronizar audiencias Meta."""
    try:
        stats = await sync_custom_audiences()
        log.info("job_custom_audiences_diario: %s", stats)
    except Exception as e:
        log.error("job_custom_audiences_diario fallo: %s", e)


# ── CLI standalone ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(sync_custom_audiences())
