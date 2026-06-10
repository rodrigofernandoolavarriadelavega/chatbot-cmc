"""
app/patient_source.py — Fuente de adquisición DECLARADA por el paciente.

Para pacientes que NO pasan por el bot (fijo / walk-in / recurrentes) no hay forma de
saber por qué canal llegaron mirando la caja. La única forma EXACTA es preguntarle al
paciente: "¿cómo nos conoció?". Este módulo guarda esa respuesta (declarada, no
inferida), keyed por RUT.

Se captura en: check-in QR (autoservicio, mejor adopción) y panel de recepción. El bot
ya guarda lo suyo en contact_tags (referido:*); acá queda el equivalente para los no-bot.
"""
from __future__ import annotations

import logging
import re

from session import _conn

log = logging.getLogger("bot.patient_source")

# Catálogo de fuentes (el orden es el orden de los botones en la UI).
FUENTES: list[tuple[str, str]] = [
    ("instagram",      "Instagram"),
    ("facebook",       "Facebook"),
    ("google",         "Google / busqué en internet"),
    ("nos_llamo",      "Los llamé por teléfono"),
    ("paso_por_fuera", "Pasaba por fuera / vi el local"),
    ("recomendado",    "Me recomendaron"),
    ("ya_paciente",    "Ya soy paciente"),
    ("otro",           "Otro"),
]
FUENTE_LABEL = dict(FUENTES)
FUENTE_KEYS = {k for k, _ in FUENTES}
# Fuentes que cuentan como "vino de publicidad / redes" → cruzables con ad spend.
FUENTES_AD = {"instagram", "facebook", "google"}


def _norm_rut(rut: str | None) -> str:
    return re.sub(r"[^0-9kK]", "", (rut or "")).upper()


def ensure_table() -> None:
    with _conn() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS patient_source (
                   rut         TEXT PRIMARY KEY,
                   id_paciente INTEGER,
                   nombre      TEXT,
                   fuente      TEXT,
                   detalle     TEXT,
                   via         TEXT,        -- checkin · recepcion · bot
                   captured_at TEXT DEFAULT (datetime('now')),
                   updated_at  TEXT DEFAULT (datetime('now'))
               )"""
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_psource_fuente ON patient_source(fuente)")


def get_source(rut: str | None) -> dict | None:
    r = _norm_rut(rut)
    if not r:
        return None
    ensure_table()
    with _conn() as c:
        row = c.execute("SELECT * FROM patient_source WHERE rut=?", (r,)).fetchone()
    return dict(row) if row else None


def set_source(rut, fuente, via="recepcion", id_paciente=None,
               nombre=None, detalle=None, overwrite=False) -> dict:
    """Guarda (upsert) la fuente declarada de un paciente. Idempotente por RUT.

    Por defecto NO pisa una fuente ya capturada (la primera respuesta es la buena);
    overwrite=True fuerza el cambio (corrección desde recepción).
    """
    r = _norm_rut(rut)
    if not r:
        return {"error": "rut_invalido"}
    if fuente not in FUENTE_KEYS:
        return {"error": "fuente_invalida", "validas": sorted(FUENTE_KEYS)}
    ensure_table()
    with _conn() as c:
        existing = c.execute("SELECT fuente FROM patient_source WHERE rut=?", (r,)).fetchone()
        if existing and not overwrite:
            return {"ok": True, "ya_existia": True, "fuente": existing["fuente"]}
        c.execute(
            """INSERT INTO patient_source
                   (rut,id_paciente,nombre,fuente,detalle,via,captured_at,updated_at)
               VALUES (?,?,?,?,?,?,datetime('now'),datetime('now'))
               ON CONFLICT(rut) DO UPDATE SET
                   fuente=excluded.fuente, detalle=excluded.detalle, via=excluded.via,
                   id_paciente=COALESCE(excluded.id_paciente, patient_source.id_paciente),
                   nombre=COALESCE(excluded.nombre, patient_source.nombre),
                   updated_at=datetime('now')""",
            (r, id_paciente, nombre, fuente, detalle, via),
        )
        c.commit()
    log.info("patient_source: rut=%s*** fuente=%s via=%s", r[:4], fuente, via)
    return {"ok": True, "fuente": fuente}


def stats(desde: str, hasta: str) -> dict:
    """Mezcla de canales declarados en [desde,hasta] (por captured_at)."""
    ensure_table()
    with _conn() as c:
        rows = c.execute(
            "SELECT fuente, COUNT(*) n FROM patient_source "
            "WHERE date(captured_at) BETWEEN ? AND ? GROUP BY fuente ORDER BY n DESC",
            (desde, hasta),
        ).fetchall()
    by = {r["fuente"]: r["n"] for r in rows}
    total = sum(by.values())
    de_ad = sum(v for k, v in by.items() if k in FUENTES_AD)
    return {
        "desde": desde, "hasta": hasta, "total": total,
        "por_fuente": [
            {"fuente": k, "label": FUENTE_LABEL.get(k, k), "n": v, "es_ad": k in FUENTES_AD}
            for k, v in sorted(by.items(), key=lambda kv: -kv[1])
        ],
        "de_ad": de_ad, "pct_ad": round(100 * de_ad / total) if total else 0,
    }
