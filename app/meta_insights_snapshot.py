"""Foto diaria de Meta Ads con desgloses → tabla `meta_insights_diario`.

Meta solo entrega estos desgloses consultando en vivo; guardados día a día se
pueden cruzar con citas_bot (ad_source_id) y ver tendencias sin depender de
la API. Nivel anuncio (ad_id = meta_referrals.source_id = citas_bot.ad_source_id).

Desgloses (cada uno es una consulta aparte — Meta no permite combinarlos todos):
  total · plataforma · ubicacion · edad_sexo · region · hora

Job diario: re-baja los últimos 3 días (Meta reajusta cifras hasta ~72h).
Backfill:  python app/meta_insights_snapshot.py 2025-10-01 2026-10-04
"""
from __future__ import annotations

import json
import logging
import time
from datetime import date, timedelta

import httpx

log = logging.getLogger("meta_insights_snapshot")

_API = "https://graph.facebook.com/v22.0"
_CONV = "onsite_conversion.messaging_conversation_started_7d"

# tipo → (breakdowns de Meta, campos de la fila que forman el "valor")
DESGLOSES: dict[str, tuple[str, tuple[str, ...]]] = {
    "total":      ("", ()),
    "plataforma": ("publisher_platform", ("publisher_platform",)),
    "ubicacion":  ("publisher_platform,platform_position",
                   ("publisher_platform", "platform_position")),
    "edad_sexo":  ("age,gender", ("age", "gender")),
    "region":     ("region", ("region",)),
    "hora":       ("hourly_stats_aggregated_by_advertiser_time_zone",
                   ("hourly_stats_aggregated_by_advertiser_time_zone",)),
}

_FIELDS = ("ad_id,ad_name,adset_id,adset_name,campaign_id,campaign_name,"
           "spend,impressions,reach,frequency,clicks,actions")


def _ensure_table(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS meta_insights_diario (
            fecha          TEXT NOT NULL,
            ad_id          TEXT NOT NULL,
            desglose       TEXT NOT NULL,
            valor          TEXT NOT NULL,
            ad_name        TEXT,
            adset_id       TEXT,
            adset_name     TEXT,
            campaign_id    TEXT,
            campaign_name  TEXT,
            spend          REAL,
            impressions    INTEGER,
            reach          INTEGER,
            frequency      REAL,
            clicks         INTEGER,
            conversaciones INTEGER,
            actions_json   TEXT,
            actualizado_ts INTEGER,
            PRIMARY KEY (fecha, ad_id, desglose, valor)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mid_fecha ON meta_insights_diario(fecha)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mid_ad ON meta_insights_diario(ad_id)")


def _cfg() -> tuple[str, str]:
    import config
    token = getattr(config, "META_ACCESS_TOKEN", "")
    acct = getattr(config, "META_AD_ACCOUNT_ID", "") or "act_220608142267129"
    return token, acct if acct.startswith("act_") else f"act_{acct}"


def _filas(client: httpx.Client, acct: str, token: str, fecha: str,
           breakdowns: str) -> list[dict]:
    params = {
        "level": "ad", "fields": _FIELDS, "limit": 500,
        "time_range": json.dumps({"since": fecha, "until": fecha}),
    }
    if breakdowns:
        params["breakdowns"] = breakdowns
    url, out = f"{_API}/{acct}/insights", []
    # Token en header, nunca en la URL (httpx loggea la URL completa).
    headers = {"Authorization": f"Bearer {token}"}
    while url:
        for intento in range(4):
            r = client.get(url, params=params, headers=headers, timeout=60)
            if r.status_code == 200:
                break
            # 17/80004 = rate limit de la Marketing API → esperar y reintentar
            time.sleep(30 * (intento + 1))
        else:
            raise RuntimeError(f"Meta insights {r.status_code}: {r.text[:200]}")
        body = r.json()
        out.extend(body.get("data", []))
        url = (body.get("paging") or {}).get("next")
        params = None  # el link "next" ya trae todos los parámetros
    return out


def snapshot_dia(fecha: str, client: httpx.Client | None = None) -> int:
    """Baja y guarda (upsert) todos los desgloses de un día. Retorna filas."""
    from session import db
    token, acct = _cfg()
    if not token:
        log.warning("meta_insights_snapshot: sin META_ACCESS_TOKEN")
        return 0
    own = client is None
    client = client or httpx.Client()
    n, ahora = 0, int(time.time())
    try:
        with db() as conn:
            _ensure_table(conn)
            for desglose, (bd, campos) in DESGLOSES.items():
                for f in _filas(client, acct, token, fecha, bd):
                    acts = f.get("actions") or []
                    conv = sum(int(float(a.get("value", 0))) for a in acts
                               if a.get("action_type") == _CONV)
                    valor = "|".join(str(f.get(c, "")) for c in campos) or "-"
                    conn.execute(
                        """INSERT OR REPLACE INTO meta_insights_diario VALUES
                           (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (fecha, f.get("ad_id", ""), desglose, valor,
                         f.get("ad_name"), f.get("adset_id"), f.get("adset_name"),
                         f.get("campaign_id"), f.get("campaign_name"),
                         float(f.get("spend") or 0), int(f.get("impressions") or 0),
                         int(f.get("reach") or 0), float(f.get("frequency") or 0),
                         int(f.get("clicks") or 0), conv,
                         json.dumps(acts, ensure_ascii=False), ahora),
                    )
                    n += 1
            conn.commit()
    finally:
        if own:
            client.close()
    return n


def snapshot_rango(desde: str, hasta: str) -> int:
    d, fin, total = date.fromisoformat(desde), date.fromisoformat(hasta), 0
    with httpx.Client() as client:
        while d <= fin:
            try:
                k = snapshot_dia(d.isoformat(), client)
                total += k
                log.info("meta_insights_snapshot %s: %d filas", d, k)
            except Exception as e:
                log.warning("meta_insights_snapshot %s falló: %s", d, e)
            d += timedelta(days=1)
    return total


async def job_meta_insights_diario() -> None:
    """Cron diario: últimos 3 días cerrados (Meta reajusta hasta ~72h)."""
    import asyncio
    hoy = date.today()
    desde = (hoy - timedelta(days=3)).isoformat()
    hasta = (hoy - timedelta(days=1)).isoformat()
    n = await asyncio.to_thread(snapshot_rango, desde, hasta)
    log.info("meta_insights_diario %s..%s: %d filas", desde, hasta, n)


if __name__ == "__main__":
    import sys
    from pathlib import Path
    from dotenv import load_dotenv
    # A mano (fuera del servicio) hay que cargar el .env: sin SQLCIPHER_KEY
    # sessions.db da "file is not a database".
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    logging.basicConfig(level=logging.INFO)
    print(snapshot_rango(sys.argv[1], sys.argv[2]), "filas")
