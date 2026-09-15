"""motivo_abandono.py — Por qué se cae una consulta de WhatsApp, y cuánto vale.

El embudo de ortodoncia (`orto_embudo_routes`) ya mostró que el 81% pregunta y
no agenda. Este módulo hace lo mismo para TODAS las especialidades y agrega lo
que ahí faltaba: **el motivo** de la caída y **el peso en pesos** de cada una.

Metodología (medida contra producción 2026-09-07, 120 días, 201k mensajes):

  - Un HILO es una corrida de mensajes del mismo teléfono con cortes de 24 h.
    Se eligió 24 h porque es la ventana de servicio de Meta: más allá de eso
    la conversación ya no se puede retomar sin plantilla, así que es otra.

  - 🔴 SOLO cuentan los hilos ENTRANTES. Este es el filtro que decide todo el
    número. Sin él la caída "mide" 68,6%; con él, 66,2%. La diferencia son
    2.722 hilos que iniciamos nosotros: confirmaciones de recordatorio
    ("confirmo", 344), opt-ins ("sí, acepto", 164), bajas de campaña
    ("no, gracias", 413). Contar eso como venta perdida infla el diagnóstico
    y es exactamente el error que comete un dashboard que mira "conversaciones
    que no convirtieron" sin preguntarse quién habló primero.

  - Un hilo CONVIRTIÓ si hay cita en `citas_bot` creada entre el inicio del
    hilo (−2 h de gracia) y su fin +3 días. Los 3 días cubren al que corta la
    conversación, lo piensa y llama a recepción al otro día.

Números de referencia (120 días a 2026-09-07), para detectar regresión:

    8.429 consultas entrantes · 2.850 agendaron (33,8%) · 5.579 se cayeron

El baseline de recuperación natural (14,9% vuelve solo dentro de 45 días) es
el dato más importante del módulo: **es el piso contra el que hay que medir
cualquier campaña de recuperación**. Una campaña que recupera 15% no recuperó
nada — eso pasaba igual. Cualquier proveedor externo que cobre comisión "sobre
lo recuperado" sin descontar este piso está cobrando por el clima.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from collections import defaultdict
from datetime import datetime, timedelta

from session import db

log = logging.getLogger(__name__)

# Ventana de servicio de Meta: dos mensajes separados por más de esto ya no son
# la misma conversación (no se puede responder sin plantilla).
CORTE_HILO = timedelta(hours=24)

# Gracia para atribuir una cita al hilo que la originó.
GRACIA_ANTES = timedelta(hours=2)
GRACIA_DESPUES = timedelta(days=3)

# Ventana para preguntar "¿volvió solo?". 45 días: más corto subestima al
# paciente que espera el próximo sueldo, más largo empieza a capturar demanda
# nueva que no tiene relación con la consulta perdida.
VENTANA_RETORNO = timedelta(days=45)


def _norm(s: str) -> str:
    """Minúsculas, sin tildes, espacios colapsados."""
    s = unicodedata.normalize("NFD", (s or "").lower())
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", s).strip()


def _ts(s: str):
    try:
        return datetime.strptime((s or "")[:19], "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Qué NO es demanda comercial
# ---------------------------------------------------------------------------
# Prefijos/marcas con los que arranca un hilo que originamos nosotros. Salieron
# de contar los primeros mensajes salientes reales, no de imaginarlos.
_MARCAS_SALIENTES = (
    "[template", "[cross-sell", "[campana", "[campaña",
    "hace un rato estabas viendo",
    "te contactamos del centro",
    "nos gustaria saber",
    "una ultima cosa",
)

# Respuestas de una sola palabra a algo que preguntamos nosotros. Si el hilo
# entero del paciente es esto, no vino a comprar: vino a contestar.
_RESPUESTAS_NO_COMERCIALES = frozenset({
    "confirmo", "si, acepto", "acepto", "si, activenlos", "activenlos",
    "no, gracias", "no gracias", "no por ahora", "mas adelante",
    "no_control", "no_agendar", "si, cancelar", "baja", "stop",
})

_KW_PRECIO = ("costo", "valor", "precio", "cuanto", "cuesta", "vale", "arancel")
_KW_DISPONIBILIDAD = ("hora", "horas", "disponib", "agenda", "cupo", "cuando", "turno")


class Hilo:
    """Una conversación: sus mensajes, dónde murió y con qué intención empezó."""

    __slots__ = ("phone", "ini", "fin", "n_in", "n_out", "ins", "primer_out",
                 "primer_dir", "ultimo_dir", "ultimo_estado")

    def __init__(self, phone, ts, direction, state):
        self.phone = phone
        self.ini = self.fin = ts
        self.n_in = self.n_out = 0
        self.ins: list[str] = []
        self.primer_out: str | None = None
        self.primer_dir = direction
        self.ultimo_dir = direction
        self.ultimo_estado = state

    @property
    def es_entrante(self) -> bool:
        """¿Lo empezó el paciente por voluntad propia?

        Tres formas de que la respuesta sea no, y las tres importan:
        el primer mensaje es nuestro; el primer mensaje nuestro trae marca de
        campaña; o el paciente sólo dijo una palabra de trámite ("confirmo").
        """
        if self.primer_dir == "out":
            return False
        cabeza = _norm(self.primer_out or "")[:90]
        if any(m in cabeza for m in _MARCAS_SALIENTES):
            return False
        if self.n_in == 1 and _norm(self.ins[0]) in _RESPUESTAS_NO_COMERCIALES:
            return False
        return True

    @property
    def motivo(self) -> str:
        """Por qué se cayó. El estado de la máquina manda sobre el texto.

        El estado en que quedó el hilo es un hecho registrado; adivinar desde
        el texto es interpretación. Por eso el texto sólo decide cuando la
        máquina no dice nada útil (IDLE), que es la mitad de los casos.
        """
        st = self.ultimo_estado or ""
        if self.ultimo_dir == "in":
            return "no_respondimos"
        if st == "HUMAN_TAKEOVER":
            return "takeover_sin_cierre"
        if st in ("WAIT_SLOT", "WAIT_META_SLOT_CHOICE", "WAIT_QUICK_BOOK"):
            return "vio_horas_no_reservo"
        if st.startswith("WAIT_RUT") or st in ("WAIT_DATOS_NUEVO", "WAIT_MODALIDAD"):
            return "traba_en_datos"
        if st == "WAIT_ESPECIALIDAD":
            return "eligiendo_especialidad"
        texto = " ".join(_norm(x) for x in self.ins)
        if any(k in texto for k in _KW_PRECIO):
            return "pregunto_precio"
        if any(k in texto for k in _KW_DISPONIBILIDAD):
            return "pregunto_disponibilidad"
        return "consulta_general"


# Qué significa cada motivo y qué se hace con él. El texto va al panel: sin
# esto el motivo es una etiqueta y nadie sabe a quién le toca actuar.
MOTIVOS = {
    "no_respondimos":         ("El paciente habló último y nadie contestó", "recepción"),
    "takeover_sin_cierre":    ("Recepción tomó la conversación y no la cerró", "recepción"),
    "vio_horas_no_reservo":   ("Vio horas concretas y no reservó", "bot"),
    "traba_en_datos":         ("Se trabó pidiéndole RUT o datos", "bot"),
    "eligiendo_especialidad": ("No supo qué especialidad pedir", "bot"),
    "pregunto_precio":        ("Preguntó precio y desapareció", "comercial"),
    "pregunto_disponibilidad":("Preguntó por horas y desapareció", "comercial"),
    "consulta_general":       ("Consulta general sin intención declarada", "—"),
}


def _cargar(dias: int = 120):
    """Lee mensajes, citas y ticket por especialidad. Una sola pasada a la db.

    Guardrail 2026-06-10: no se sostiene la conexión mientras se procesa; se
    materializa todo a memoria y recién ahí se arma el embudo.
    """
    with db() as c:
        msgs = list(c.execute(
            "SELECT phone, direction, ts, state, text FROM messages "
            "WHERE ts >= datetime('now', ?) ORDER BY phone, ts", (f"-{int(dias)} days",)))

        citas = defaultdict(list)
        for ph, ts, esp in c.execute(
                "SELECT phone, created_at, especialidad FROM citas_bot "
                "WHERE created_at >= datetime('now', ?)", (f"-{int(dias) + 60} days",)):
            d = _ts(ts)
            if ph and d:
                citas[ph].append((d, esp or ""))

        # Ticket real por especialidad. El join va por id_paciente + fecha,
        # NUNCA por nombre: cruzar por `paciente_nombre` abre un fan-out que
        # multiplicó Medicina General a n=8.142 cuando son 1.334 atenciones.
        # (El promedio aguantó — $17.135 vs $17.434 — pero el n mentía, y con
        # un n mentido no se puede decidir cuándo confiar en el promedio.)
        ticket = {}
        for esp, n, prom in c.execute("""
            SELECT cb.especialidad, COUNT(DISTINCT a.atencion_id), AVG(x.pagado)
            FROM citas_bot cb
            JOIN bi_atenciones a
              ON a.id_paciente = cb.id_paciente_medilink AND a.fecha = cb.fecha
            JOIN (SELECT atencion_id, SUM(monto) pagado
                    FROM bi_pagos_caja GROUP BY 1) x ON x.atencion_id = a.atencion_id
            WHERE cb.id_paciente_medilink IS NOT NULL
              AND cb.especialidad IS NOT NULL AND cb.especialidad <> ''
            GROUP BY 1 HAVING COUNT(DISTINCT a.atencion_id) >= 4"""):
            ticket[esp] = {"n": n, "promedio": int(prom or 0)}
    return msgs, citas, ticket


def _armar_hilos(msgs) -> list[Hilo]:
    hilos, cur = [], None
    for phone, direction, ts, state, text in msgs:
        d = _ts(ts)
        if d is None:
            continue
        if cur is None or cur.phone != phone or d - cur.fin > CORTE_HILO:
            if cur is not None:
                hilos.append(cur)
            cur = Hilo(phone, d, direction, state)
        cur.fin = d
        cur.ultimo_dir = direction
        cur.ultimo_estado = state
        if direction == "in":
            cur.n_in += 1
            cur.ins.append(text or "")
        else:
            cur.n_out += 1
            if cur.primer_out is None:
                cur.primer_out = text or ""
    if cur is not None:
        hilos.append(cur)
    return [h for h in hilos if h.n_in > 0]


def analizar(dias: int = 120) -> dict:
    """Embudo conversacional con motivo de caída y recuperación natural."""
    msgs, citas, ticket = _cargar(dias)
    hilos = _armar_hilos(msgs)

    def convirtio(h: Hilo) -> str | None:
        for d, esp in citas.get(h.phone, []):
            if h.ini - GRACIA_ANTES <= d <= h.fin + GRACIA_DESPUES:
                return esp or "?"
        return None

    def volvio_solo(h: Hilo) -> bool:
        """¿Agendó por su cuenta después, sin que nadie lo persiguiera?"""
        desde = h.fin + GRACIA_DESPUES
        return any(desde < d <= h.fin + VENTANA_RETORNO for d, _ in citas.get(h.phone, []))

    entrantes = [h for h in hilos if h.es_entrante]
    perdidos = [h for h in entrantes if convirtio(h) is None]

    por_motivo = defaultdict(lambda: {"perdidos": 0, "volvio_solo": 0})
    for h in perdidos:
        m = por_motivo[h.motivo]
        m["perdidos"] += 1
        if volvio_solo(h):
            m["volvio_solo"] += 1
    for m, v in por_motivo.items():
        v["retorno_natural"] = round(100 * v["volvio_solo"] / v["perdidos"], 1) if v["perdidos"] else 0.0
        v["glosa"], v["responsable"] = MOTIVOS.get(m, ("—", "—"))

    n_vuelve = sum(v["volvio_solo"] for v in por_motivo.values())
    return {
        "dias": dias,
        "hilos_totales": len(hilos),
        "descartados_no_entrantes": len(hilos) - len(entrantes),
        "consultas": len(entrantes),
        "agendaron": len(entrantes) - len(perdidos),
        "perdidos": len(perdidos),
        "tasa_caida": round(100 * len(perdidos) / len(entrantes), 1) if entrantes else 0.0,
        "retorno_natural": round(100 * n_vuelve / len(perdidos), 1) if perdidos else 0.0,
        "por_motivo": dict(por_motivo),
        "ticket": ticket,
    }


# ---------------------------------------------------------------------------
# TODO(Rodrigo): la fórmula de prioridad
# ---------------------------------------------------------------------------
def _prioridad(motivo: str, ticket_esp: int, dias_desde_caida: int,
               retorno_natural: float) -> float:
    """Cuánto vale perseguir esta conversación perdida. Mayor = primero.

    Esta es la única decisión del módulo que no sale de la data: es de negocio,
    y cambia a quién llama recepción el lunes. Las cuatro variables ya están
    medidas y disponibles; lo que falta es cómo pesarlas.

    Las opciones y qué pasa con cada una, con los números reales:

    a) Sólo valor  →  `ticket_esp`
       Persigue Odontología ($56.032) y Psiquiatría. Ignora que Medicina
       General son 1.334 atenciones y el resto son decenas. Cola corta y cara.

    b) Valor × intención  →  ticket × peso(motivo)
       Prioriza `vio_horas_no_reservo` (1.055 casos: vieron horas concretas y
       no reservaron — máxima intención demostrada). Es lo que haría Cassper.

    c) Valor × (1 − retorno_natural)  →  el que NO va a volver solo
       `pregunto_precio` vuelve solo apenas 8,9%; `pregunto_disponibilidad`
       vuelve 21,6%. Perseguir al segundo es pagar por lo que iba a pasar
       igual. Esta opción evita justamente el autoengaño de atribuirse el
       14,9% de base.

    d) Todo lo anterior × decaimiento por antigüedad
       Un hilo de hace 40 días está frío; uno de ayer sigue tibio.

    Mi recomendación es (c) con decaimiento, porque es la única que mide valor
    INCREMENTAL — pero la decisión de si recepción debe llamar primero al que
    más plata deja o al que más se va a perder es tuya, no mía.

    Devuelve un score; la unidad no importa mientras ordene.
    """
    raise NotImplementedError("definir la fórmula de prioridad")


def reporte(dias: int = 120) -> str:
    """Informe de texto para correr a mano o desde cron."""
    r = analizar(dias)
    L = [
        f"EMBUDO CONVERSACIONAL — últimos {r['dias']} días",
        "",
        f"  hilos con mensaje del paciente : {r['hilos_totales']:>6}",
        f"  descartados (los iniciamos nosotros): {r['descartados_no_entrantes']:>6}",
        f"  consultas reales               : {r['consultas']:>6}",
        f"  agendaron                      : {r['agendaron']:>6}",
        f"  se cayeron                     : {r['perdidos']:>6}  ({r['tasa_caida']}%)",
        "",
        f"  🔴 PISO: {r['retorno_natural']}% de los perdidos vuelve solo sin que nadie lo llame.",
        "     Una campaña que recupere menos que eso no recuperó nada.",
        "",
        f"  {'motivo':<26}{'perdidos':>9}{'vuelve solo':>13}  responsable",
    ]
    for m, v in sorted(r["por_motivo"].items(), key=lambda x: -x[1]["perdidos"]):
        L.append(f"  {m:<26}{v['perdidos']:>9}{v['retorno_natural']:>12}%  {v['responsable']}")
        L.append(f"      └ {v['glosa']}")
    L.append("")
    L.append("  TICKET REAL POR ESPECIALIDAD (caja, join por id_paciente)")
    for esp, t in sorted(r["ticket"].items(), key=lambda x: -x[1]["promedio"]):
        conf = "" if t["n"] >= 20 else "   ⚠ n bajo"
        L.append(f"    {esp:<28} ${t['promedio']:>8,}".replace(",", ".") + f"  n={t['n']}{conf}")
    return "\n".join(L)


if __name__ == "__main__":
    import sys
    print(reporte(int(sys.argv[1]) if len(sys.argv) > 1 else 120))
