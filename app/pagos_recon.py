"""pagos_recon.py — Reconciliación pagos_cmc × Medilink cada 30 min.

Por qué: muchas veces recepción registra el pago en el módulo Pagos pero el de
Medilink (la caja) NO queda igual — sobre todo en PARTICULAR/FONASA de Medicina
General / Medicina Familiar. Este job cruza ambos y MARCA cada fila para que
recepción vea cuáles arreglar, sin tocar Medilink ni los pagos.

Alcance (acotado a propósito): solo previsión particular/fonasa en MG / Medicina
Familiar. El usuario pidió no hacerlo con todos.

Regla de comparación (clave en Fonasa): recepción `copago + bonificación` (el
arancel total que cobró) vs Medilink `Σ monto` del paciente ese día. Para
particular bonificación=0 → copago vs Medilink. Para Fonasa el total iguala el
arancel de Medilink (si solo se comparara el copago, toda Fonasa saldría
'difiere' por la bonificación Imed).

recon_estado por fila:
  'ok'             → cuadra al peso (NO se vuelve a chequear salvo que se edite)
  'difiere'        → el módulo y Medilink no coinciden en monto
  'falta_medilink' → recepción cobró pero Medilink no tiene el pago

Incluye filas con candado (bloqueado). El match de paciente es por NOMBRE
(recepción no guarda id_paciente), reusando el matcher de cuadre_caja.
"""

import asyncio
import logging
import time

log = logging.getLogger("pagos_recon")

_AREAS_SCOPE = ("medicina general", "medicina familiar")
_PREV_SCOPE = ("particular", "fonasa")
_TOL = 1   # tolerancia de monto en pesos (cuadre al peso)


def _en_alcance(area: str, prevision: str) -> bool:
    a = (area or "").strip().lower()
    p = (prevision or "").strip().lower()
    return p in _PREV_SCOPE and any(x in a for x in _AREAS_SCOPE)


def _reconciliar_fecha(fecha: str) -> dict:
    """Reconcilia las filas en alcance de un día. Devuelve contadores."""
    from session import db
    from cuadre_caja import _tokens, _match
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    c = {"revisadas": 0, "ok": 0, "difiere": 0, "falta_medilink": 0}

    with db() as conn:
        # Medilink oficial por paciente ese día (nombre desde bi_atenciones)
        medilink = []
        try:
            for r in conn.execute(
                """SELECT COALESCE(a.paciente_nombre,'') AS nom, SUM(k.monto) AS monto
                     FROM bi_pagos_caja k
                     LEFT JOIN bi_atenciones a ON a.atencion_id = k.atencion_id
                    WHERE substr(k.fecha,1,10) = ?
                    GROUP BY k.id_paciente""",
                (fecha,),
            ):
                medilink.append({"monto": int(r["monto"] or 0), "tok": _tokens(r["nom"])})
        except Exception as e:  # noqa: BLE001
            log.warning("recon %s: bi_pagos_caja error %s", fecha, e)

        rows = conn.execute(
            """SELECT id, paciente_nombre, prevision, area, copago, bonificacion,
                      COALESCE(recon_estado,'') AS recon_estado,
                      COALESCE(recon_at,'')     AS recon_at,
                      COALESCE(updated_at,'')   AS updated_at
                 FROM pagos_cmc WHERE fecha = ?""",
            (fecha,),
        ).fetchall()

        # Agrupar las filas EN ALCANCE por nombre (un paciente puede tener varias
        # filas ese día → se suman y se comparan contra el total de Medilink).
        groups: dict[str, dict] = {}
        for row in rows:
            if not _en_alcance(row["area"], row["prevision"]):
                continue
            tok = _tokens(row["paciente_nombre"])
            if not tok:
                continue
            # ¿necesita chequeo? sí si no está 'ok', o si se editó después del
            # último recon (updated_at > recon_at), o si nunca se reconcilió.
            need = (
                row["recon_estado"] != "ok"
                or not row["recon_at"]
                or (row["updated_at"] and row["recon_at"] and row["updated_at"] > row["recon_at"])
            )
            key = " ".join(sorted(tok))
            g = groups.setdefault(key, {"tok": tok, "ids": [], "total": 0, "need": False})
            g["ids"].append(row["id"])
            g["total"] += int(row["copago"] or 0) + int(row["bonificacion"] or 0)
            g["need"] = g["need"] or need

        for g in groups.values():
            if not g["need"] or g["total"] <= 0:
                # total 0 = aún sin cobrar (draft) → nada que reconciliar todavía
                continue
            c["revisadas"] += len(g["ids"])
            hit = next((m for m in medilink if _match(g["tok"], m["tok"])), None)
            if hit is None:
                estado, mlink = "falta_medilink", 0
                c["falta_medilink"] += len(g["ids"])
            elif abs(hit["monto"] - g["total"]) <= _TOL:
                estado, mlink = "ok", hit["monto"]
                c["ok"] += len(g["ids"])
            else:
                estado, mlink = "difiere", hit["monto"]
                c["difiere"] += len(g["ids"])
            for pid in g["ids"]:
                conn.execute(
                    "UPDATE pagos_cmc SET recon_estado=?, recon_at=?, recon_medilink=? WHERE id=?",
                    (estado, now, mlink, pid),
                )
        conn.commit()
    return c


def reconciliar(dias: int = 14) -> dict:
    """Reconcilia la ventana reciente. Síncrono (DB); llamar con asyncio.to_thread."""
    from session import db
    with db() as conn:
        fechas = [r[0] for r in conn.execute(
            "SELECT DISTINCT fecha FROM pagos_cmc WHERE fecha >= date('now', ?)",
            (f"-{int(dias)} days",),
        ).fetchall()]
    tot = {"revisadas": 0, "ok": 0, "difiere": 0, "falta_medilink": 0, "dias": len(fechas)}
    for fecha in fechas:
        try:
            r = _reconciliar_fecha(fecha)
            for k in ("revisadas", "ok", "difiere", "falta_medilink"):
                tot[k] += r[k]
        except Exception as e:  # noqa: BLE001
            log.warning("recon fecha %s falló: %s", fecha, e)
    return tot


async def job_reconciliar_pagos():
    """Cron 30 min: refresca la caja Medilink de hoy/ayer y reconcilia la ventana."""
    # 1. Refrescar Medilink reciente para que el cruce no marque falsos 'falta'.
    try:
        from bi_sync import sync_pagos_rango
        import datetime
        from zoneinfo import ZoneInfo
        hoy = datetime.datetime.now(ZoneInfo("America/Santiago"))
        desde = (hoy - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        await sync_pagos_rango(desde=desde, hasta=hoy.strftime("%Y-%m-%d"), force=True)
    except Exception as e:  # noqa: BLE001
        log.warning("recon: sync Medilink falló (sigo con lo que haya): %s", e)
    # 2. Reconciliar (off-thread para no bloquear el event loop).
    try:
        res = await asyncio.to_thread(reconciliar, 14)
        log.info("recon pagos: %s", res)
    except Exception as e:  # noqa: BLE001
        log.warning("recon pagos falló: %s", e)


def resumen() -> dict:
    """Conteo actual por estado (para badge del panel)."""
    from session import db
    with db() as conn:
        rows = conn.execute(
            """SELECT COALESCE(recon_estado,'') AS e, COUNT(*) AS n
                 FROM pagos_cmc WHERE fecha >= date('now','-14 days')
                GROUP BY recon_estado""",
        ).fetchall()
    out = {"ok": 0, "difiere": 0, "falta_medilink": 0, "pendiente": 0}
    for r in rows:
        out[r["e"] or "pendiente"] = out.get(r["e"] or "pendiente", 0) + r["n"]
    return out
