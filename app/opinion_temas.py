"""Temas de opinión de pacientes (Alma Radar · Reputación).

Fuente: mensajes entrantes dentro de las 72 h posteriores a cada encuesta
postconsulta (últimos 90 días, >= 15 caracteres) y reseñas de Google guardadas.
Un job nocturno (03:40 CLT) clasifica SOLO lo nuevo con Claude Haiku y guarda
el resultado en `opinion_temas`. El Radar lee únicamente esa tabla.

Privacidad: al modelo solo viaja el texto con cifras de 6+ dígitos y correos
enmascarados; jamás teléfono, nombre ni RUT. Apagable con TEMAS_OPINION_ACTIVE=false.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
from datetime import datetime, timezone

log = logging.getLogger("opinion_temas")

MODELO = "claude-haiku-4-5"
TOPE_POR_CORRIDA = 1000
LOTE = 25
VENTANA_DIAS = 90
LARGO_MIN = 15
EJEMPLO_MAX = 140
# USD por millón de tokens (Haiku 4.5)
PRECIO_IN, PRECIO_OUT = 1.0, 5.0

TEMAS = ("trato", "explicacion", "puntualidad_espera", "precio", "contacto_telefono",
         "instalaciones", "resultado_mejoria", "agenda_disponibilidad", "otro")
TONOS = ("positivo", "negativo", "neutro")

DDL = """
CREATE TABLE IF NOT EXISTS opinion_temas (
    msg_key      TEXT PRIMARY KEY,
    fuente       TEXT,
    fecha        TEXT,
    es_opinion   INTEGER,
    tema         TEXT,
    tono         TEXT,
    profesional  TEXT,
    texto        TEXT,
    created_at   TEXT DEFAULT (datetime('now'))
)"""

_RE_CORREO = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_RE_NUM = re.compile(r"\+?\d[\d\s.\-]{4,}\d|\d{6,}")


def activo() -> bool:
    return os.getenv("TEMAS_OPINION_ACTIVE", "true").strip().lower() in ("1", "true", "yes")


def enmascarar(txt: str | None) -> str:
    """Quita correos y secuencias numéricas con 6+ dígitos (teléfonos, RUT)."""
    t = _RE_CORREO.sub("[correo]", txt or "")

    def _n(m):
        return "[nº]" if sum(c.isdigit() for c in m.group(0)) >= 6 else m.group(0)
    return _RE_NUM.sub(_n, t).strip()


def _clave(prefijo: str, valor: str) -> str:
    return hashlib.sha1(f"{prefijo}:{valor}".encode("utf-8")).hexdigest()[:20]


def candidatos_mensajes(conn, limite: int = TOPE_POR_CORRIDA) -> list[dict]:
    """Mensajes entrantes <=72 h tras una encuesta postconsulta, aún sin clasificar."""
    rows = conn.execute(f"""
        SELECT m.id AS mid, m.text AS text, m.ts AS ts, MAX(cb.profesional) AS profesional
        FROM fidelizacion_msgs f
        JOIN messages m ON m.phone = f.phone AND m.direction = 'in'
             AND m.ts > f.enviado_en AND m.ts <= datetime(f.enviado_en, '+72 hours')
        LEFT JOIN citas_bot cb ON cb.id_cita = f.cita_id AND cb.phone = f.phone
        WHERE f.tipo = 'postconsulta'
          AND f.enviado_en >= datetime('now', '-{VENTANA_DIAS} days')
          AND length(m.text) >= {LARGO_MIN}
        GROUP BY m.id
        ORDER BY m.id DESC""").fetchall()
    ya = {r[0] for r in conn.execute("SELECT msg_key FROM opinion_temas")} if _existe(conn) else set()
    out = []
    for r in rows:
        k = _clave("msg", str(r["mid"]))
        if k in ya:
            continue
        out.append({"key": k, "fuente": "mensaje", "fecha": (r["ts"] or "")[:10],
                    "texto": enmascarar(r["text"]), "profesional": r["profesional"] or ""})
        if len(out) >= limite:
            break
    return out


def candidatos_resenas(conn, limite: int) -> list[dict]:
    try:
        import google_rating as gr
        d = gr.cached_rating() or {}
    except Exception:
        return []
    ya = {r[0] for r in conn.execute("SELECT msg_key FROM opinion_temas")} if _existe(conn) else set()
    hoy = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out = []
    for r in (d.get("reviews") or []):
        txt = (r.get("text") or r.get("texto") or "").strip()
        if len(txt) < LARGO_MIN:
            continue
        k = _clave("google", (r.get("author") or r.get("autor") or "") + "|" + txt)
        if k in ya or len(out) >= limite:
            continue
        out.append({"key": k, "fuente": "google", "fecha": hoy, "texto": enmascarar(txt), "profesional": ""})
    return out


def _existe(conn) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='opinion_temas'").fetchone() is not None


_PROMPT = (
    "Clasificas mensajes de pacientes de un centro médico chileno enviados tras su consulta. "
    "Para cada mensaje decide si expresa una OPINION sobre la atención (es_opinion=true) o si es "
    "agenda, logística, trámite o pregunta (es_opinion=false).\n"
    "Temas cerrados: " + ", ".join(TEMAS) + ".\n"
    "Tono: positivo, negativo o neutro.\n"
    "Responde SOLO un arreglo JSON, sin texto adicional: "
    '[{"i":0,"es_opinion":true,"tema":"trato","tono":"positivo"}, ...] con un objeto por mensaje.')


def _extraer_json(txt: str):
    txt = (txt or "").strip()
    ini, fin = txt.find("["), txt.rfind("]")
    if ini < 0 or fin < ini:
        raise ValueError("sin arreglo JSON")
    return json.loads(txt[ini:fin + 1])


def clasificar_lote(client, lote: list[dict]) -> tuple[list[dict], int, int]:
    """Devuelve (resultados validados, tokens_in, tokens_out). Solo envía texto enmascarado."""
    cuerpo = "\n".join(f"{i}: {json.dumps(x['texto'][:600], ensure_ascii=False)}" for i, x in enumerate(lote))
    resp = client.messages.create(model=MODELO, max_tokens=2000, system=_PROMPT,
                                  messages=[{"role": "user", "content": cuerpo}])
    u = getattr(resp, "usage", None)
    t_in, t_out = int(getattr(u, "input_tokens", 0) or 0), int(getattr(u, "output_tokens", 0) or 0)
    texto = "".join(getattr(b, "text", "") for b in resp.content)
    res = []
    for o in _extraer_json(texto):
        try:
            i = int(o["i"])
            base = lote[i]
        except (KeyError, ValueError, TypeError, IndexError):
            continue
        op = o.get("es_opinion") is True
        tema = o.get("tema") if o.get("tema") in TEMAS else "otro"
        tono = o.get("tono") if o.get("tono") in TONOS else "neutro"
        res.append({**base, "es_opinion": 1 if op else 0, "tema": tema if op else None, "tono": tono if op else None})
    return res, t_in, t_out


def costo_usd(t_in: int, t_out: int) -> float:
    return (t_in * PRECIO_IN + t_out * PRECIO_OUT) / 1_000_000


def clasificar_todo(client, items: list[dict]) -> tuple[list[dict], int, int, int]:
    out, ti, to, fallos = [], 0, 0, 0
    for k in range(0, len(items), LOTE):
        try:
            r, a, b = clasificar_lote(client, items[k:k + LOTE])
            out += r
            ti += a
            to += b
        except Exception as e:  # el lote fallido se reintenta la noche siguiente
            fallos += 1
            log.warning("opinion_temas: lote falló: %s", e)
    return out, ti, to, fallos


def correr(client=None, tope: int = TOPE_POR_CORRIDA) -> dict:
    """Clasifica lo nuevo y lo guarda. Incremental; máx `tope` por corrida."""
    from session import db
    if client is None:
        import anthropic
        from config import ANTHROPIC_API_KEY
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    with db() as c:
        c.execute(DDL)
        items = candidatos_mensajes(c, tope)
        if len(items) < tope:
            items += candidatos_resenas(c, tope - len(items))
    if not items:
        log.info("opinion_temas: sin mensajes nuevos")
        return {"nuevos": 0, "opiniones": 0, "costo_usd": 0.0}
    res, ti, to, fallos = clasificar_todo(client, items)
    with db() as c:
        c.executemany(
            "INSERT OR IGNORE INTO opinion_temas(msg_key,fuente,fecha,es_opinion,tema,tono,profesional,texto) "
            "VALUES(?,?,?,?,?,?,?,?)",
            [(r["key"], r["fuente"], r["fecha"], r["es_opinion"], r["tema"], r["tono"], r["profesional"],
              r["texto"][:EJEMPLO_MAX] if r["es_opinion"] else None) for r in res])
    costo = costo_usd(ti, to)
    ops = sum(r["es_opinion"] for r in res)
    log.info("opinion_temas: %d clasificados (%d opiniones), %d lotes con error, tokens in=%d out=%d, costo ~US$%.4f",
             len(res), ops, fallos, ti, to, costo)
    return {"nuevos": len(res), "opiniones": ops, "fallos": fallos, "tokens_in": ti, "tokens_out": to,
            "costo_usd": round(costo, 4)}


async def job_opinion_temas():
    if not activo():
        return
    import asyncio
    try:
        await asyncio.to_thread(correr)
    except Exception:
        log.exception("opinion_temas: job falló")


def temas_data(conn, hoy: datetime | None = None) -> dict:
    """Resumen para el Radar: solo lee la tabla. Sin teléfonos, nombres ni profesional."""
    vacio = {"hay": False, "total": 0, "ultima": None, "temas": []}
    if not _existe(conn):
        return vacio
    desde = f"-{VENTANA_DIAS} days"
    ult = conn.execute("SELECT MAX(created_at) FROM opinion_temas").fetchone()[0]
    if not ult:
        return vacio
    rows = conn.execute(
        "SELECT tema, tono, COUNT(*) n FROM opinion_temas WHERE es_opinion=1 "
        "AND fecha >= date('now', ?) GROUP BY tema, tono", (desde,)).fetchall()
    por = {}
    for r in rows:
        d = por.setdefault(r["tema"], {"tema": r["tema"], "positivo": 0, "negativo": 0, "neutro": 0})
        d[r["tono"] or "neutro"] += r["n"]
    temas = []
    for d in por.values():
        d["total"] = d["positivo"] + d["negativo"] + d["neutro"]
        ej = conn.execute(
            "SELECT texto, tono FROM opinion_temas WHERE es_opinion=1 AND tema=? AND fecha >= date('now', ?) "
            "AND texto IS NOT NULL AND texto != '' ORDER BY fecha DESC, created_at DESC LIMIT 2",
            (d["tema"], desde)).fetchall()
        d["ejemplos"] = [{"texto": enmascarar(e["texto"])[:EJEMPLO_MAX], "tono": e["tono"]} for e in ej]
        temas.append(d)
    temas.sort(key=lambda x: -x["total"])
    return {"hay": True, "total": sum(t["total"] for t in temas), "ultima": ult, "temas": temas}
