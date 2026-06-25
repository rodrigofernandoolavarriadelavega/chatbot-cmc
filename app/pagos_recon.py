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
    """Reconcilia las filas en alcance de un día por id_paciente. Contadores."""
    from session import db
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    c = {"revisadas": 0, "ok": 0, "difiere": 0, "falta_medilink": 0}

    with db() as conn:
        # Medilink: efectivo cobrado por id_paciente ese día (bi_pagos_caja es
        # 100% efectivo → ese monto ES el copago que debería calzar el módulo).
        med: dict[int, int] = {}
        try:
            # id_paciente PROPIO de bi_pagos_caja (NO joinear a bi_atenciones: esa
            # tabla viene con id_paciente/nombre vacíos → perdía el 93% del enlace).
            for r in conn.execute(
                """SELECT id_paciente AS idp, SUM(monto) AS monto
                     FROM bi_pagos_caja
                    WHERE substr(fecha,1,10) = ? AND COALESCE(id_paciente,0) > 0
                    GROUP BY id_paciente""",
                (fecha,),
            ):
                med[int(r["idp"])] = int(r["monto"] or 0)
        except Exception as e:  # noqa: BLE001
            log.warning("recon %s: bi_pagos_caja error %s", fecha, e)

        rows = conn.execute(
            """SELECT id, COALESCE(id_paciente,0) AS idp, lower(prevision) AS prev,
                      area, copago, COALESCE(monto_medilink,0) AS mm,
                      COALESCE(recon_estado,'') AS recon_estado,
                      COALESCE(recon_at,'')     AS recon_at,
                      COALESCE(updated_at,'')   AS updated_at
                 FROM pagos_cmc WHERE fecha = ?""",
            (fecha,),
        ).fetchall()

        # Agrupar EN ALCANCE por id_paciente (exacto). Sin id_paciente no se
        # reconcilia (se deja 'pendiente' — no se marca en falso).
        groups: dict[int, dict] = {}
        for row in rows:
            if not _en_alcance(row["area"], row["prev"]):
                continue
            idp = int(row["idp"] or 0)
            if not idp:
                continue
            need = (
                row["recon_estado"] != "ok"
                or not row["recon_at"]
                or (row["updated_at"] and row["recon_at"] and row["updated_at"] > row["recon_at"])
            )
            g = groups.setdefault(idp, {"ids": [], "copago": 0, "mm": 0,
                                        "fonasa": False, "need": False})
            g["ids"].append(row["id"])
            g["copago"] += int(row["copago"] or 0)
            g["mm"] = max(g["mm"], int(row["mm"] or 0))
            g["fonasa"] = g["fonasa"] or (row["prev"] == "fonasa")
            g["need"] = g["need"] or need

        for idp, g in groups.items():
            if not g["need"] or g["copago"] <= 0:
                continue   # copago 0 = aún sin cobrar (draft) → nada que cuadrar
            c["revisadas"] += len(g["ids"])
            mlink = med.get(idp)
            if mlink is None:
                estado, mv = "falta_medilink", 0        # recepción cobró, Medilink no lo tiene
                c["falta_medilink"] += len(g["ids"])
            else:
                # Medilink guarda el ARANCEL TOTAL. Se compara contra el arancel que
                # capturó prellenar (monto_medilink); en particular = copago. En
                # fonasa SIN arancel capturado no se puede validar el monto (el
                # copago siempre es menor que el total) → basta con que EXISTA.
                base = g["mm"] if g["mm"] > 0 else g["copago"]
                if (g["fonasa"] and g["mm"] <= 0) or abs(mlink - base) <= _TOL:
                    estado, mv = "ok", mlink
                    c["ok"] += len(g["ids"])
                else:
                    estado, mv = "difiere", mlink         # monto no coincide
                    c["difiere"] += len(g["ids"])
            for pid in g["ids"]:
                conn.execute(
                    "UPDATE pagos_cmc SET recon_estado=?, recon_at=?, recon_medilink=? WHERE id=?",
                    (estado, now, mv, pid),
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
