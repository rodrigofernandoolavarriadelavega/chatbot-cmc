"""
Cuadre de caja: cruce caja oficial Medilink (bi_pagos_caja) × registro de recepción
(pagos_cmc) + reporte de texto para el dueño (WhatsApp / asistente Adkun).

Dos usos:
  - cruce_dia(fecha)   → estructura: total Medilink vs recepción, atenciones que
                          Medilink cobró pero recepción NO registró (plata posiblemente
                          perdida), y al revés.
  - texto_cuadre()     → reporte legible: cuadre de ayer + efectivo en caja +
                          días sin depositar + faltan registrar.

El match paciente↔paciente es por NOMBRE normalizado tolerante (Medilink no expone
el RUT en bi_pagos_caja y recepción no guarda id_paciente): se consideran la misma
persona si comparten ≥2 tokens significativos o si un nombre es subconjunto del otro.
Esto evita falsos "no registrado" por formato ("Heraldo Soto" vs "Heraldo Soto Soto").
"""
import logging
import re
import unicodedata
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

log = logging.getLogger("cuadre_caja")
_CHILE_TZ = ZoneInfo("America/Santiago")

# Partículas que no aportan a la identidad (no cuentan como token significativo)
_STOP = {"de", "del", "la", "las", "los", "san", "santa", "y"}


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFD", (s or "").lower())
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", s).strip()


def _tokens(s: str) -> set[str]:
    return {t for t in _norm(s).split() if len(t) >= 3 and t not in _STOP}


def _match(a: set[str], b: set[str]) -> bool:
    """Misma persona si comparten ≥2 tokens, o uno es subconjunto del otro (≥1 token)."""
    if not a or not b:
        return False
    common = a & b
    if len(common) >= 2:
        return True
    if a <= b or b <= a:
        return True
    return False


def _clp(n) -> str:
    return "$" + format(int(n or 0), ",d").replace(",", ".")


def cruce_dia(fecha: str) -> dict:
    """Cruza la caja oficial Medilink contra el registro de recepción para `fecha`."""
    from session import db as _conn
    medilink: list[dict] = []
    recepcion: list[dict] = []
    with _conn() as conn:
        # Medilink oficial: monto por paciente (nombre desde bi_atenciones)
        try:
            for r in conn.execute(
                """SELECT k.id_paciente,
                          COALESCE(a.paciente_nombre,'') AS nom,
                          SUM(k.monto) AS monto, COUNT(*) AS n
                     FROM bi_pagos_caja k
                     LEFT JOIN bi_atenciones a ON a.atencion_id = k.atencion_id
                    WHERE substr(k.fecha,1,10) = ?
                    GROUP BY k.id_paciente""",
                (fecha,),
            ):
                medilink.append({"nombre": r["nom"], "monto": int(r["monto"] or 0),
                                 "n": r["n"], "tok": _tokens(r["nom"])})
        except Exception as e:
            log.warning("cruce_dia: bi_pagos_caja error: %s", e)
        # Recepción: copago por nombre
        for r in conn.execute(
            """SELECT paciente_nombre AS nom, SUM(copago) AS cop, COUNT(*) AS n
                 FROM pagos_cmc WHERE fecha = ? GROUP BY paciente_nombre""",
            (fecha,),
        ):
            recepcion.append({"nombre": r["nom"], "copago": int(r["cop"] or 0),
                              "n": r["n"], "tok": _tokens(r["nom"])})

    med_total = sum(m["monto"] for m in medilink)
    med_n = sum(m["n"] for m in medilink)
    rec_total = sum(r["copago"] for r in recepcion)
    rec_n = sum(r["n"] for r in recepcion)

    # Match: para cada paciente Medilink, ¿hay una fila de recepción que calce?
    rec_libre = list(recepcion)
    faltan_registrar: list[dict] = []   # en Medilink, sin registro en recepción
    matched = 0
    for m in medilink:
        hit = next((r for r in rec_libre if _match(m["tok"], r["tok"])), None)
        if hit:
            matched += 1
            rec_libre.remove(hit)
        else:
            faltan_registrar.append({"nombre": m["nombre"], "monto": m["monto"]})

    # Lo que quedó en recepción sin par en Medilink (registró de más / aún no en caja)
    de_mas = [{"nombre": r["nombre"], "copago": r["copago"]}
              for r in rec_libre if r["copago"] > 0]

    faltan_registrar.sort(key=lambda x: -x["monto"])
    de_mas.sort(key=lambda x: -x["copago"])

    return {
        "fecha":             fecha,
        "medilink_total":    med_total,
        "medilink_n":        med_n,
        "recepcion_total":   rec_total,
        "recepcion_n":       rec_n,
        "matched":           matched,
        "bonif_imed":        max(0, med_total - rec_total),  # ≈ bonif Imed (Fonasa)
        "faltan_registrar":  faltan_registrar,
        "faltan_monto":      sum(f["monto"] for f in faltan_registrar),
        "de_mas":            de_mas,
    }


def texto_cuadre(fecha: str | None = None) -> str:
    """Reporte legible del cuadre de un día (default: ayer) + estado de la caja."""
    if not fecha:
        fecha = (datetime.now(_CHILE_TZ) - timedelta(days=1)).strftime("%Y-%m-%d")
    c = cruce_dia(fecha)

    from caja_diaria_routes import calcular_saldo_caja
    saldo = calcular_saldo_caja()

    fcorta = "/".join(reversed(fecha.split("-")))
    out = [f"📊 *Cuadre de caja — {fcorta}*", ""]
    out.append(f"🏥 Medilink oficial: *{_clp(c['medilink_total'])}* ({c['medilink_n']} atenciones)")
    out.append(f"🧾 Recepción registró: *{_clp(c['recepcion_total'])}* ({c['recepcion_n']})")
    out.append(f"≈ Bonif. Imed (la paga el seguro): {_clp(c['bonif_imed'])}")
    out.append("")

    # Faltan registrar (atenciones que Medilink cobró y recepción no anotó)
    fr = c["faltan_registrar"]
    if fr:
        out.append(f"⚠️ *Faltan registrar: {len(fr)} atención(es)* ({_clp(c['faltan_monto'])})")
        for f in fr[:8]:
            out.append(f"   • {f['nombre'][:32]} — {_clp(f['monto'])}")
        if len(fr) > 8:
            out.append(f"   …y {len(fr)-8} más")
    else:
        out.append("✅ Todas las atenciones de Medilink están registradas.")
    out.append("")

    # Estado de la caja (efectivo físico)
    en_caja = saldo.get("en_caja", 0)
    dias = saldo.get("dias_sin_depositar")
    out.append(f"💵 En la caja ahora: *{_clp(en_caja)}*")
    ud = saldo.get("ultimo_deposito")
    if ud:
        udf = "/".join(reversed(ud["fecha"].split("-")))
        línea = f"🏦 Último depósito: {_clp(ud['valor'])} ({udf}"
        línea += f", hace {dias} día(s))" if dias is not None else ")"
        out.append(línea)
        if dias is not None and dias >= 3:
            out.append(f"   ⚠️ Llevas {dias} días sin depositar — conviene llevar al banco.")
    return "\n".join(out)
