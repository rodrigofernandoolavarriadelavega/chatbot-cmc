"""
app/offline_match.py — Meta Offline Conversions para el canal NO-BOT (fijo / walk-in).

PROBLEMA: los ads que mandan al teléfono fijo (no al bot WhatsApp) no llevan click-id
→ sus conversiones quedan sin atribuir y esos ads parecen rendir menos de lo real.

SOLUCIÓN (Meta Offline Conversions, el método estándar): enviar esas conversiones a Meta
CAPI con identificadores hasheados (teléfono, RUT como external_id, email, nombre) y dejar
que META haga el match contra quién vio/clickeó el ad. Meta sí sabe quién vio el ad;
nosotros no. La atribución resultante aparece en Ads Manager (no en el panel local), y de
paso Meta optimiza mejor las campañas.

DISEÑO:
- Conversión = pago en `bi_pagos_caja` de un paciente que NO pasó por el bot (no está en
  `citas_bot`). Enriquecido con `bi.dim_paciente` (rut/nombre/apellido/email/telefono).
- Dedup por `pago_id` (tabla `offline_match_sent`) → cada conversión se envía una vez.
- `event_id` determinístico (`offline_<pago_id>`) → Meta también deduplica de su lado.
- `action_source="physical_store"` (conversión presencial en la clínica), SIN click-id.

GATING (se envían datos de pacientes a Meta → es decisión del dueño):
- `OFFLINE_MATCH_ENABLED` (config) OFF por defecto.
- `dry_run=True` (default) NO envía nada: devuelve exactamente qué se mandaría.
"""
from __future__ import annotations

import logging

from session import _conn

log = logging.getLogger("bot.offline_match")


def _ensure_table() -> None:
    with _conn() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS offline_match_sent (
                   pago_id     INTEGER PRIMARY KEY,
                   id_paciente INTEGER,
                   fecha       TEXT,
                   monto       INTEGER,
                   estado      TEXT,
                   sent_at     TEXT
               )"""
        )


def find_pending(desde: str, hasta: str, limit: int = 1000) -> list[dict]:
    """Pagos de pacientes NO-bot en [desde,hasta] aún no enviados, con identificadores."""
    from winback import bi_conn

    _ensure_table()
    with _conn() as c:
        bot = set(
            r["id_paciente_medilink"]
            for r in c.execute(
                "SELECT DISTINCT id_paciente_medilink FROM citas_bot "
                "WHERE id_paciente_medilink IS NOT NULL"
            )
        )
        sent = set(r["pago_id"] for r in c.execute("SELECT pago_id FROM offline_match_sent"))
        pagos = c.execute(
            "SELECT pago_id, id_paciente, fecha, monto FROM bi_pagos_caja "
            "WHERE fecha BETWEEN ? AND ? AND monto > 0 ORDER BY fecha",
            (desde, hasta),
        ).fetchall()

    cand = [
        dict(p)
        for p in pagos
        if p["id_paciente"] is not None
        and p["id_paciente"] not in bot
        and p["pago_id"] not in sent
    ][:limit]

    # Enriquecer con bi.dim_paciente (una sola query)
    ids = list({p["id_paciente"] for p in cand})
    info: dict[int, dict] = {}
    if ids:
        with bi_conn() as bc:
            cur = bc.cursor()
            cur.execute(
                "SELECT paciente_id, rut, nombre, apellido, email, telefono "
                "FROM bi.dim_paciente WHERE paciente_id = ANY(%s)",
                (ids,),
            )
            for r in cur.fetchall():
                info[r[0]] = {
                    "rut": r[1], "nombre": r[2], "apellido": r[3],
                    "email": r[4], "telefono": r[5],
                }
    return [{**p, **info.get(p["id_paciente"], {})} for p in cand]


def _normalizable_phone(tel: str | None) -> str | None:
    from meta_capi import _normalize_phone
    return _normalize_phone(tel) if tel else None


async def send_one(conv: dict, dry_run: bool = True) -> dict:
    """Envía (o simula) UNA conversión offline a Meta CAPI. Requiere teléfono válido."""
    tel = _normalizable_phone(conv.get("telefono"))
    rut = (conv.get("rut") or "").strip() or None
    if not tel:
        return {"skipped": "sin_telefono_valido", "pago_id": conv["pago_id"]}

    preview = {
        "pago_id": conv["pago_id"],
        "value": int(conv["monto"]),
        "ids": {"ph": True, "external_id": bool(rut),
                "name": bool(conv.get("nombre")), "email": bool(conv.get("email"))},
    }
    if dry_run:
        return {"dry_run": True, **preview}

    import meta_capi
    res = await meta_capi.send_event(
        "Purchase", tel,
        rut=rut,
        first_name=conv.get("nombre") or None,
        last_name=conv.get("apellido") or None,
        email=(conv.get("email") or None),
        value=float(conv["monto"]), currency="CLP",
        event_id=f"offline_{conv['pago_id']}",
        action_source="physical_store",
        custom_data={"canal": "offline_fijo", "content_name": "atencion_presencial"},
    )
    estado = "error" if (isinstance(res, dict) and res.get("error")) else "sent"
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO offline_match_sent"
            "(pago_id,id_paciente,fecha,monto,estado,sent_at) "
            "VALUES(?,?,?,?,?,datetime('now'))",
            (conv["pago_id"], conv["id_paciente"], conv["fecha"], int(conv["monto"]), estado),
        )
    return {"sent": estado == "sent", "pago_id": conv["pago_id"], "estado": estado}


async def run(desde: str, hasta: str, dry_run: bool = True, limit: int = 1000) -> dict:
    """Procesa todas las conversiones offline pendientes del período."""
    pend = find_pending(desde, hasta, limit)
    con_tel = [p for p in pend if _normalizable_phone(p.get("telefono"))]
    monto_enviable = sum(int(p["monto"]) for p in con_tel)

    # El flag solo importa al ENVIAR de verdad (el dry-run nunca manda nada).
    if not dry_run:
        from config import OFFLINE_MATCH_ENABLED
        if not OFFLINE_MATCH_ENABLED:
            return {
                "blocked": "OFFLINE_MATCH_ENABLED=false (prendé el flag para enviar)",
                "pendientes": len(pend), "con_telefono": len(con_tel),
                "monto_enviable": monto_enviable,
            }

    results = [await send_one(c, dry_run=dry_run) for c in pend]
    enviados = sum(1 for r in results if r.get("sent"))
    return {
        "dry_run": dry_run,
        "periodo": f"{desde}→{hasta}",
        "pendientes": len(pend),
        "con_telefono_valido": len(con_tel),
        "monto_enviable": monto_enviable,
        "enviados": enviados,
        "sample": results[:6],
        "nota": "Meta hace el match por identificadores; la atribución se ve en Ads Manager.",
    }


if __name__ == "__main__":
    import asyncio, sys, json
    sys.path.insert(0, "app")
    from dotenv import load_dotenv
    load_dotenv(".env")
    d = sys.argv[1] if len(sys.argv) > 1 else "2026-05-12"
    h = sys.argv[2] if len(sys.argv) > 2 else "2026-06-10"
    out = asyncio.run(run(d, h, dry_run=True))
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
