"""
simulador_horas.py — ¿qué pasa con la plata si muevo horas entre profesionales?

POR QUÉ EXISTE
    El simulador de boxes responde "¿cabe?" (choques de sala). Esta es la otra
    pregunta del dueño, que ninguna pantalla contestaba: "si le doy 8 horas a
    Abarca y yo bajo las mías, ¿cuánto me baja el ingreso?".

DE DÓNDE SALEN LOS NÚMEROS (y por qué de ahí)
    · Citas y horas → `ausentismo_citas`, que es el barrido día por día de
      Medilink `/citas`. NO se usa `bi_atenciones`: subestima, porque las
      Fonasa y los bonos no siempre dejan atención registrada.
    · Plata → `bi_pagos_caja`, la caja real. Es la única fuente de venta total
      del centro; Medilink marca 99,7% "Efectivo" y el medio de pago es inútil.
    · Sólo DÍAS TRABAJADOS. Promediar sobre días corridos mete los feriados y
      los días sin agenda, y hunde artificialmente la ocupación.

LA CUENTA QUE IMPORTA (y que es contraintuitiva)
    Al traspasar pacientes de un profesional a otro, el dueño **no pierde la
    venta entera**: sigue recibiendo el margen del centro. Lo que pierde es
    exactamente la comisión que le paga al que los recibe. Por eso el resultado
    es `venta_traspasada × comisión`, no `venta_traspasada`.

CUIDADO CON LA OCUPACIÓN
    La ocupación medida sale de la jornada ACTUAL. Extrapolarla a una jornada
    del doble es optimista: las horas marginales son las difíciles de llenar.
    Por eso `simular` devuelve siempre un abanico de sensibilidad y no un solo
    número — un valor único acá se lee como promesa.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

log = logging.getLogger("simulador_horas")

# Estados de Medilink (verificados contra producción 2026-08-06)
_ATENDIDO = 2
_NO_ASISTE = 8


def _m(hhmm) -> int | None:
    try:
        a, b = str(hhmm).split(":")[:2]
        return int(a) * 60 + int(b)
    except Exception:
        return None


def medir(prof_ids: list[int], dias: int = 90) -> dict[int, dict]:
    """Radiografía real de cada profesional en la ventana pedida.

    Devuelve por id: días trabajados, citas/día, horas presente/día, ocupación
    y ticket por cita. Todo medido, nada estimado.
    """
    from session import db
    from medilink import PROFESIONALES

    hasta = date.today()
    desde = (hasta - timedelta(days=dias)).isoformat()
    hasta_s = hasta.isoformat()
    out: dict[int, dict] = {}

    with db() as c:
        for pid in prof_ids:
            iv = int((PROFESIONALES.get(pid) or {}).get("intervalo") or 15) or 15
            filas = c.execute(
                "SELECT fecha, hora, id_estado FROM ausentismo_citas "
                "WHERE id_profesional=? AND fecha>=? AND fecha<? AND anulacion=0",
                (pid, desde, hasta_s),
            ).fetchall()
            por_dia: dict[str, list] = {}
            for f, h, est in filas:
                por_dia.setdefault(f, []).append((h, est))

            n_dias = span_total = n_citas = n_at = n_falta = 0
            for _f, items in por_dia.items():
                horas = sorted(x for x, _ in items if x)
                if not horas:
                    continue
                ini, fin = _m(horas[0]), _m(horas[-1])
                if ini is None or fin is None:
                    continue
                n_dias += 1
                # La jornada se mide de la primera a la última cita + un cupo:
                # es el tiempo que el profesional estuvo comprometido, que es lo
                # que se paga. La hora contratada no sirve — hay días sin agenda.
                span_total += (fin - ini) + iv
                n_citas += len(items)
                n_at += sum(1 for _, e in items if e == _ATENDIDO)
                n_falta += sum(1 for _, e in items if e == _NO_ASISTE)

            caja = c.execute(
                "SELECT COUNT(*), COALESCE(SUM(monto),0) FROM bi_pagos_caja "
                "WHERE id_profesional=? AND fecha>=? AND fecha<?",
                (pid, desde, hasta_s),
            ).fetchone() or (0, 0)

            h_pres = span_total / 60
            h_cita = n_citas * iv / 60
            out[pid] = {
                "prof_id": pid,
                "nombre": (PROFESIONALES.get(pid) or {}).get("nombre", f"#{pid}"),
                "intervalo": iv,
                "dias_trabajados": n_dias,
                "citas": n_citas,
                "atendidas": n_at,
                "no_asiste": n_falta,
                "citas_dia": round(n_citas / n_dias, 2) if n_dias else 0,
                "horas_dia": round(h_pres / n_dias, 2) if n_dias else 0,
                "horas_presente": round(h_pres, 1),
                "horas_cita": round(h_cita, 1),
                "ocupacion": round(h_cita / h_pres, 4) if h_pres else 0,
                "caja_total": int(caja[1] or 0),
                "pagos": int(caja[0] or 0),
                # Ticket por CITA AGENDADA, no por pago: es lo que rinde un cupo
                # de agenda, que es la unidad que se está moviendo. Por pago
                # saldría más alto y sobreestimaría el traspaso.
                "ticket_cita": int((caja[1] or 0) / n_citas) if n_citas else 0,
                "dias_mes": round(n_dias / (dias / 30), 1) if n_dias else 0,
            }
    return out


def simular(base: dict[int, dict], cambios: list[dict], origen_id: int | None = None) -> dict:
    """Aplica cambios de jornada y devuelve el efecto en el ingreso del dueño.

    `cambios`: [{prof_id, horas_dia, ocupacion?, comision}]
      · `ocupacion` omitida = se mantiene la medida (y se avisa que es optimista
        si la jornada crece).
    `origen_id`: de quién salen los pacientes. Sin él se asume demanda nueva y
      no se descuenta a nadie — hay que decirlo, porque cambia todo el resultado.
    """
    origen = base.get(origen_id) if origen_id else None
    tk_origen = (origen or {}).get("ticket_cita") or 0
    filas, avisos = [], []
    total_traspasado = total_comision = 0.0
    delta_citas_total = 0.0

    for cb in cambios:
        pid = int(cb["prof_id"])
        b = base.get(pid)
        if not b:
            continue
        horas = float(cb.get("horas_dia") or b["horas_dia"])
        occ_med = b["ocupacion"] or 0
        occ = float(cb["ocupacion"]) if cb.get("ocupacion") is not None else occ_med
        comision = float(cb.get("comision") or 0)
        iv = b["intervalo"]

        cupos = horas * 60 / iv
        citas_nuevas = cupos * occ
        delta = citas_nuevas - b["citas_dia"]
        # Los DÍAS pesan tanto como las horas y es el supuesto que más se
        # escapa: el traspaso sólo ocurre los días que trabaja QUIEN RECIBE.
        # Abarca hace 18 días/mes; calcularlo sobre los 23,7 del origen infla
        # el traspaso ~32%. Se puede fijar aparte para simular "más días".
        dias_mes = float(cb.get("dias_mes") or b["dias_mes"] or 22)
        # El ticket es el del ORIGEN: los pacientes que se mueven son suyos y
        # llegan con su propia venta, no con la del que los recibe.
        tk = tk_origen or b["ticket_cita"]
        traspaso_mes = delta * tk * dias_mes

        if horas > b["horas_dia"] * 1.3 and cb.get("ocupacion") is None:
            avisos.append(
                f"{b['nombre']}: el {occ_med*100:.0f}% está medido sobre jornadas de "
                f"{b['horas_dia']:.2f} h. Sostenerlo en {horas:.0f} h es optimista — "
                f"las horas marginales son las que cuesta llenar.")

        filas.append({
            "prof_id": pid, "nombre": b["nombre"],
            "horas_hoy": b["horas_dia"], "horas_nuevas": round(horas, 2),
            "citas_hoy": b["citas_dia"], "citas_nuevas": round(citas_nuevas, 1),
            "delta_citas": round(delta, 1),
            "ocupacion_usada": round(occ, 4), "ocupacion_medida": round(occ_med, 4),
            "cupos": round(cupos), "comision": comision,
            "dias_mes": round(dias_mes, 1), "dias_mes_medidos": b["dias_mes"],
            "traspaso_mes": round(traspaso_mes),
            "paga_comision_mes": round(traspaso_mes * comision),
            "queda_centro_mes": round(traspaso_mes * (1 - comision)),
        })
        total_traspasado += traspaso_mes
        total_comision += traspaso_mes * comision
        delta_citas_total += delta

    res = {
        "cambios": filas,
        "avisos": avisos,
        "traspaso_mes": round(total_traspasado),
        "baja_ingreso_mes": round(total_comision),
        "baja_ingreso_ano": round(total_comision * 12),
    }

    if origen:
        # OJO: el origen sólo cede pacientes los días que el receptor trabaja.
        # En los otros mantiene su carga completa, así que su jornada nueva es
        # un promedio ponderado, no la resta directa.
        citas_post = origen["citas_dia"] - delta_citas_total
        res["origen"] = {
            "prof_id": origen["prof_id"], "nombre": origen["nombre"],
            "citas_hoy": origen["citas_dia"], "citas_post": round(citas_post, 1),
            "horas_hoy": origen["horas_dia"],
            # Su jornada nueva es el tiempo de las citas que le quedan: el punto
            # de reducir horas es justamente no quedarse esperando.
            "horas_post": round(max(0.0, citas_post) * origen["intervalo"] / 60, 2),
            "horas_liberadas": round(origen["horas_dia"]
                                     - max(0.0, citas_post) * origen["intervalo"] / 60, 2),
        }
        if citas_post < 0:
            res["avisos"].append(
                f"{origen['nombre']} quedaría con agenda negativa: se está "
                f"traspasando más volumen del que tiene.")
    return res


def sensibilidad(base: dict[int, dict], prof_id: int, horas: float,
                 comisiones=(0.20, 0.55), ocupaciones=(0.65, 0.55, 0.45, 0.35),
                 origen_id: int | None = None) -> list[dict]:
    """El mismo cálculo a varias ocupaciones. Un número solo se lee como promesa."""
    out = []
    for occ in ocupaciones:
        fila = {"ocupacion": occ}
        for com in comisiones:
            r = simular(base, [{"prof_id": prof_id, "horas_dia": horas,
                                "ocupacion": occ, "comision": com}], origen_id)
            fila[f"com_{int(com*100)}"] = r["baja_ingreso_mes"]
            fila["traspaso_mes"] = r["traspaso_mes"]
            fila["citas_nuevas"] = r["cambios"][0]["citas_nuevas"] if r["cambios"] else 0
        out.append(fila)
    return out
