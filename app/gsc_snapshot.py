"""Foto diaria de Google Search Console → tablas `gsc_diario` y `gsc_paginas_diario`.

Search Console solo guarda 16 meses y no entrega nada por persona: guardado
día a día se puede cruzar POR PÁGINA con quienes escribieron por WhatsApp
desde esa página (`web_origen`, canal "Página web" de Campañas Meta).

Dos consultas por rango:
  - gsc_diario          dimensiones date + page + query (consultas principales)
  - gsc_paginas_diario  dimensiones date + page (totales EXACTOS: en la
                        combinación con query Google oculta las consultas
                        anónimas, así que sumar gsc_diario da menos clics)

Basura: el sitio fue hackeado en el pasado y Google aún muestra páginas
/products/ y consultas en japonés/chino/coreano. Se GUARDAN (para ver cuándo
desaparecen) con basura=1 y se excluyen de todos los totales.

Credenciales: service account (solo lectura, scope webmasters.readonly) en
GSC_CREDENTIALS_PATH (default /opt/cmc-secrets/gsc-key.json), propiedad
GSC_SITE (default sc-domain:centromedicocarampangue.cl).

Job diario 06:40 CLT: re-baja los últimos 5 días (Search Console tiene ~2-3
días de retraso y ajusta cifras). Flag GSC_SNAPSHOT_ACTIVE (default true).
Backfill:  python app/gsc_snapshot.py 2025-06-01 2026-10-05
"""
from __future__ import annotations

import logging
import re
import time
from datetime import date, timedelta
from urllib.parse import quote, urlparse

log = logging.getLogger("gsc_snapshot")

_API = "https://searchconsole.googleapis.com/webmasters/v3/sites/{site}/searchAnalytics/query"
_SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"
ROW_LIMIT = 25000
TRAMO_DIAS = 31          # cada consulta cubre a lo más un mes (paginando adentro)

# Hangul, kana, ideogramas CJK (y sus signos de puntuación de ancho completo)
_CJK = re.compile(r"[぀-ヿ㐀-䶿一-鿿가-힯＀-￯]")


# ── Clasificación ────────────────────────────────────────────────────────────

def es_basura(page: str | None, query: str | None = None) -> bool:
    return "/products/" in (page or "") or bool(_CJK.search(query or ""))


def _slug(t: str) -> str:
    import unicodedata
    t = unicodedata.normalize("NFD", (t or "").lower())
    t = "".join(ch for ch in t if unicodedata.category(ch) != "Mn")
    return re.sub(r"[^a-z0-9]+", "-", t).strip("-")


_COMUNAS_SLUG = ("comuna", "lebu", "canete", "curanilahue", "los-alamos")


def pagina_de_url(url: str | None) -> tuple[str, str]:
    """URL del sitio → (página, artículo) con la MISMA regla que el marcador
    "(web: página · artículo · botón)" de static/cmc-wa.js, para cruzar con
    `web_origen`. Ej.: / → (home, ""); /blog/eco-abdominal/ → (blog,
    eco-abdominal); /curanilahue → (comuna, curanilahue)."""
    p = urlparse(url or "").path if "://" in (url or "") else (url or "")
    p = p.rstrip("/") or "/"
    segs = [s for s in p.split("/") if s]
    art = "" if p in ("/", "/blog", "/comuna") or not segs else _slug(segs[-1])
    if p == "/":
        pag = "home"
    elif segs[0] == "blog":
        pag = "blog"
    elif segs[0] in _COMUNAS_SLUG:
        pag = "comuna"
    elif segs[0] == "ortodoncia":
        pag = "landing_ortodoncia"
    else:
        pag = _slug(segs[0]).replace("-", "_") or "sitio"
    return pag, art


# ── Tablas ───────────────────────────────────────────────────────────────────

def ensure_tables(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS gsc_diario (
            fecha       TEXT NOT NULL,
            page        TEXT NOT NULL,
            query       TEXT NOT NULL,
            clicks      INTEGER,
            impressions INTEGER,
            ctr         REAL,
            position    REAL,
            basura      INTEGER DEFAULT 0,
            actualizado_ts INTEGER,
            PRIMARY KEY (fecha, page, query)
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_gsc_fecha ON gsc_diario(fecha)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_gsc_query ON gsc_diario(query)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS gsc_paginas_diario (
            fecha       TEXT NOT NULL,
            page        TEXT NOT NULL,
            clicks      INTEGER,
            impressions INTEGER,
            ctr         REAL,
            position    REAL,
            basura      INTEGER DEFAULT 0,
            actualizado_ts INTEGER,
            PRIMARY KEY (fecha, page)
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_gscp_fecha ON gsc_paginas_diario(fecha)")


# ── API ──────────────────────────────────────────────────────────────────────

def _cfg() -> tuple[str, str]:
    import config
    return (getattr(config, "GSC_CREDENTIALS_PATH", "") or "/opt/cmc-secrets/gsc-key.json",
            getattr(config, "GSC_SITE", "") or "sc-domain:centromedicocarampangue.cl")


def _session():
    """Sesión HTTP autorizada con la service account (solo lectura)."""
    from google.oauth2 import service_account
    from google.auth.transport.requests import AuthorizedSession
    ruta, _ = _cfg()
    cred = service_account.Credentials.from_service_account_file(ruta, scopes=[_SCOPE])
    return AuthorizedSession(cred)


def _consulta(sess, site: str, desde: str, hasta: str, dims: list[str]) -> list[dict]:
    """Todas las filas de una consulta, paginando con startRow."""
    url = _API.format(site=quote(site, safe=""))
    out, start = [], 0
    while True:
        body = {"startDate": desde, "endDate": hasta, "dimensions": dims,
                "rowLimit": ROW_LIMIT, "startRow": start, "dataState": "all"}
        r = sess.post(url, json=body, timeout=60)
        if r.status_code != 200:
            raise RuntimeError(f"Search Console {r.status_code}: {str(getattr(r, 'text', ''))[:300]}")
        filas = (r.json() or {}).get("rows") or []
        out += filas
        if len(filas) < ROW_LIMIT:
            return out
        start += ROW_LIMIT


def snapshot_tramo(desde: str, hasta: str, sess=None) -> tuple[int, int]:
    """Baja y reemplaza [desde, hasta] en ambas tablas. Primero baja TODO y
    recién después escribe (si la API falla a mitad no se borra nada)."""
    from session import db
    _, site = _cfg()
    sess = sess or _session()
    con_q = _consulta(sess, site, desde, hasta, ["date", "page", "query"])
    pags = _consulta(sess, site, desde, hasta, ["date", "page"])
    ahora = int(time.time())
    with db() as conn:
        ensure_tables(conn)
        conn.execute("DELETE FROM gsc_diario WHERE fecha >= ? AND fecha <= ?", (desde, hasta))
        conn.execute("DELETE FROM gsc_paginas_diario WHERE fecha >= ? AND fecha <= ?", (desde, hasta))
        for f in con_q:
            fecha, page, query = (f.get("keys") or ["", "", ""])[:3]
            conn.execute("INSERT OR REPLACE INTO gsc_diario VALUES (?,?,?,?,?,?,?,?,?)",
                         (fecha, page, query, int(f.get("clicks") or 0), int(f.get("impressions") or 0),
                          float(f.get("ctr") or 0), float(f.get("position") or 0),
                          1 if es_basura(page, query) else 0, ahora))
        for f in pags:
            fecha, page = (f.get("keys") or ["", ""])[:2]
            conn.execute("INSERT OR REPLACE INTO gsc_paginas_diario VALUES (?,?,?,?,?,?,?,?)",
                         (fecha, page, int(f.get("clicks") or 0), int(f.get("impressions") or 0),
                          float(f.get("ctr") or 0), float(f.get("position") or 0),
                          1 if es_basura(page) else 0, ahora))
        conn.commit()
    return len(con_q), len(pags)


def snapshot_rango(desde: str, hasta: str, sess=None) -> int:
    d, fin, total = date.fromisoformat(desde), date.fromisoformat(hasta), 0
    sess = sess or _session()
    while d <= fin:
        h = min(fin, d + timedelta(days=TRAMO_DIAS - 1))
        try:
            nq, np_ = snapshot_tramo(d.isoformat(), h.isoformat(), sess)
            total += nq + np_
            log.info("gsc_snapshot %s..%s: %d filas con consulta, %d por página", d, h, nq, np_)
        except Exception as e:
            log.warning("gsc_snapshot %s..%s falló: %s", d, h, e)
        d = h + timedelta(days=1)
    return total


async def job_gsc_diario() -> None:
    """Cron 06:40 CLT: últimos 5 días (retraso de ~2-3 días + ajustes)."""
    import asyncio
    import config
    if not getattr(config, "GSC_SNAPSHOT_ACTIVE", True):
        log.info("gsc_diario: apagado (GSC_SNAPSHOT_ACTIVE=false)")
        return
    hoy = date.today()
    desde, hasta = (hoy - timedelta(days=5)).isoformat(), (hoy - timedelta(days=1)).isoformat()
    try:
        n = await asyncio.to_thread(snapshot_rango, desde, hasta)
        log.info("gsc_diario %s..%s: %d filas", desde, hasta, n)
    except Exception as e:   # credenciales ausentes, etc.: no tumba el scheduler
        log.error("gsc_diario: %s", e)


if __name__ == "__main__":
    import sys
    from pathlib import Path
    from dotenv import load_dotenv
    # A mano (fuera del servicio) hay que cargar el .env: sin SQLCIPHER_KEY
    # sessions.db da "file is not a database".
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    logging.basicConfig(level=logging.INFO)
    print(snapshot_rango(sys.argv[1], sys.argv[2]), "filas")
