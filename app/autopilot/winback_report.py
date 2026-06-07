"""Reporte de Win-back día por día — envíos, conversión e INGRESO REAL.

Cruza los envíos (bi.winback_envios) con la CAJA REAL (bi_pagos_caja en sessions.db,
fresca desde Medilink /pagos por cron) por id_paciente — el MISMO método de atribución
que usa cac_report para Meta Ads. El ingreso de un contactado = sus pagos en caja con
fecha >= la del envío (atribución por ventana).

NO usa value_clp (valor esperado por envío, no ganado) ni el BI fact_ingresos (viejo).
"""
from __future__ import annotations

import logging

log = logging.getLogger("bot")


def _norm_date(s) -> str:
    s = str(s or "")
    if "/" in s:  # DD/MM/YYYY -> YYYY-MM-DD
        p = s.split("/")
        if len(p) == 3:
            try:
                return f"{p[2]}-{int(p[1]):02d}-{int(p[0]):02d}"
            except Exception:  # noqa: BLE001
                return s[:10]
    return s[:10]


def _mask_phone(t: str) -> str:
    t = "".join(ch for ch in str(t or "") if ch.isdigit())
    return (t[:5] + "•••" + t[-2:]) if len(t) >= 8 else (t or "—")


def report(days: int = 120) -> dict:
    """Día por día: envíos, agendaron, pagaron e ingreso real, con el desglose de
    destinatarios (teléfono, nombre, cohorte, especialidad, agendó, ingreso)."""
    try:
        from winback import bi_conn
    except Exception as e:  # noqa: BLE001
        return {"error": f"BI no disponible: {e}", "days": [], "totals": {}}

    # 1) Envíos + datos del destinatario (BI)
    sends = []
    try:
        with bi_conn() as c:
            cur = c.cursor()
            cur.execute("""
                SELECT w.enviado_at::date AS dia, w.paciente_id, w.telefono, w.cohorte,
                       COALESCE(w.especialidad, '') AS esp,
                       (w.agendo_at IS NOT NULL OR w.cita_atribuida_id IS NOT NULL) AS agendo,
                       COALESCE(p.nombre, '') AS nombre
                FROM bi.winback_envios w
                LEFT JOIN bi.dim_paciente p ON p.paciente_id = w.paciente_id
                WHERE w.enviado_at >= CURRENT_DATE - (%s * INTERVAL '1 day')
                ORDER BY w.enviado_at
            """, (days,))
            for dia, pid, tel, coh, esp, agendo, nombre in cur.fetchall():
                sends.append({"dia": str(dia), "paciente_id": int(pid) if pid else None,
                              "telefono": tel, "cohorte": str(coh or ""), "especialidad": esp,
                              "agendo": bool(agendo), "nombre": nombre})
    except Exception as e:  # noqa: BLE001
        return {"error": f"query envíos: {e}", "days": [], "totals": {}}

    # 2) Pagos de esos pacientes (CAJA REAL, sessions.db)
    pids = {s["paciente_id"] for s in sends if s["paciente_id"]}
    pagos: dict[int, list] = {}
    try:
        from session import _conn
        with _conn() as conn:
            rows = conn.execute(
                "SELECT id_paciente, fecha, monto FROM bi_pagos_caja WHERE id_paciente IS NOT NULL"
            ).fetchall()
        for r in rows:
            try:
                pid = int(r["id_paciente"])
            except Exception:  # noqa: BLE001
                continue
            if pid in pids:
                pagos.setdefault(pid, []).append((_norm_date(r["fecha"]), int(r["monto"] or 0)))
    except Exception as e:  # noqa: BLE001
        log.warning("winback_report caja: %s", e)

    # 3) Ingreso atribuido por envío = pagos del paciente con fecha >= envío
    by_day: dict[str, dict] = {}
    for s in sends:
        ingreso = 0
        for fecha, monto in pagos.get(s["paciente_id"], []):
            if fecha >= s["dia"]:
                ingreso += monto
        s["ingreso"] = ingreso
        s["pago"] = ingreso > 0
        s["telefono_masked"] = _mask_phone(s["telefono"])
        d = by_day.setdefault(s["dia"], {"dia": s["dia"], "enviados": 0, "agendaron": 0,
                                         "pagaron": 0, "ingreso": 0, "destinatarios": []})
        d["enviados"] += 1
        d["agendaron"] += 1 if s["agendo"] else 0
        d["pagaron"] += 1 if s["pago"] else 0
        d["ingreso"] += ingreso
        d["destinatarios"].append({
            "telefono": s["telefono_masked"], "nombre": (s["nombre"] or "").split(" ")[0][:18] or "—",
            "cohorte": s["cohorte"], "especialidad": s["especialidad"],
            "agendo": s["agendo"], "pago": s["pago"], "ingreso": ingreso,
        })

    days_list = sorted(by_day.values(), key=lambda d: d["dia"], reverse=True)
    for d in days_list:  # convertidos primero dentro del día
        d["destinatarios"].sort(key=lambda r: (-r["ingreso"], not r["agendo"]))

    tot = {
        "enviados": sum(d["enviados"] for d in days_list),
        "agendaron": sum(d["agendaron"] for d in days_list),
        "pagaron": sum(d["pagaron"] for d in days_list),
        "ingreso": sum(d["ingreso"] for d in days_list),
        "dias": len(days_list),
    }
    tot["conv_pct"] = round(100 * tot["agendaron"] / tot["enviados"], 1) if tot["enviados"] else 0
    return {"days": days_list, "totals": tot,
            "note": "Ingreso = caja real (bi_pagos_caja) por id_paciente, pago ≥ fecha de envío. "
                    "Mismo método de atribución que Meta Ads."}
