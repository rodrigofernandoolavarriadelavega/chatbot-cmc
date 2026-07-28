"""recepcion_kanban_v2.py — La cola de trabajo de recepción, no un tablero de conversaciones.

Qué falló en la v1 (medido, no opinado): en 8 días se abrió 10 veces contra 164
del panel de recepción. Mostraba 220 tarjetas, de las cuales el 76% era ruido
(113 primeros mensajes sin intención + 54 ya atendidas que nunca salían), sin
límites de WIP, sin edad visible, ordenadas por recencia en vez de por espera, y
sin poder ACTUAR desde ahí. Un tablero que no deja ejecutar es un informe, y un
informe se mira una vez al día.

La v2 aplica cuatro prácticas del método Kanban que la v1 no tenía:

  1. POLÍTICA DE SALIDA — lo cerrado abandona el tablero. Un tablero acumulativo
     deja de ser un tablero. Medido: de 220 tarjetas quedan 34 accionables; el
     resto se convierte en tres números (atendidas hoy, con hora más adelante,
     escribieron sin decir a qué) que informan sin estorbar.

  2. CLASES DE SERVICIO — cada tarjeta se clasifica por su costo de demora, no
     por su etapa. Las cuatro clásicas, traducidas al mesón:

       expedite     el paciente VIO horarios y no cerró. Es lo más caro que
                    existe acá: intención máxima, ventana que se cierra sola.
                    Va en su propio carril arriba de todo.
       fecha_fija   tiene hora hoy o mañana. El tiempo lo fija el calendario.
       estandar     espera respuesta nuestra. Se atiende FIFO — la regla del
                    método es "siempre saca la más vieja".
       intangible   escribió sin decir a qué. Útil, sin costo de demora.

  3. LÍMITES DE WIP — cada columna tiene un tope. Pasado el tope, la columna se
     marca: es una señal de cuello, no un adorno.

  4. POLÍTICAS EXPLÍCITAS — escritas en el propio tablero. En Kanban esto es una
     práctica central: si la regla no está a la vista, no existe.

Y lo que lo hace usable: cada tarjeta abre la conversación en el panel de
recepción con un clic. La cola dice a quién le toca; el panel deja atenderlo.
"""
from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

log = logging.getLogger("recepcion_kanban_v2")
_CLT = ZoneInfo("America/Santiago")

# ALARMA POR ANTIGÜEDAD, no por cantidad (2026-07-28, medido).
#
# El tope de 10 de la primera versión estaba inventado. Al medirlo contra 30 días
# de producción (1.751 respuestas humanas reales, excluyendo al bot que contesta
# en segundos y ahogaba la medición) apareció esto:
#
#     mediana de respuesta de recepción ....  7 minutos
#     p90 .................................. ~3 días
#
# O sea el problema del centro NO es el volumen ni la velocidad: la mediana es
# excelente. El problema es la cola larga — un puñado de conversaciones que
# simplemente nunca se contestan y arrastran el p90 a días.
#
# Un límite de cantidad no detecta eso: 3 conversaciones pueden estar perfectas o
# llevar dos días esperando, y el número es el mismo. Por eso el umbral es de
# TIEMPO. Se fija en 30 min ≈ 4 veces la mediana: suficiente para no gritar por
# el ritmo normal, y suficiente para que nada se hunda un día entero sin que se
# note.
MINUTOS_ALARMA = 30

# Tope de cantidad como referencia secundaria (se muestra, no alarma solo).
WIP = {"responder": 10, "sin_cerrar": 15, "eligiendo": 20}

COLUMNAS = [
    {"id": "responder",  "label": "Responder ahora",
     "politica": "Alguien escribió y nadie contestó. Se atiende de la más vieja a la más nueva."},
    {"id": "sin_cerrar", "label": "Vio horarios, no cerró",
     "politica": "Vio horas disponibles y no confirmó. Es la más cara de perder: hubo intención."},
    {"id": "eligiendo",  "label": "Eligiendo con el bot",
     "politica": "En conversación con el bot. No tocar salvo que se trabe."},
    {"id": "agendado",   "label": "Con hora tomada",
     "politica": "Ya tiene hora. Solo aparece si es para hoy o mañana."},
]

# Las 5 etapas del diseño original del dueño: el recorrido del PACIENTE.
# Conviven con las columnas de trabajo, no compiten: responden preguntas
# distintas. Las columnas dicen "a quién atiendo ahora"; estas etapas dicen
# "dónde se cae la gente". El panel las ofrece como dos vistas del mismo dato.
ETAPAS_EMBUDO = [
    {"id": "primer_mensaje", "label": "1º mensaje",
     "politica": "Escribió y todavía no dijo a qué viene."},
    {"id": "area",           "label": "Eligió área",
     "politica": "Ya dijo qué especialidad necesita."},
    {"id": "profesional",    "label": "Eligiendo horario",
     "politica": "Tiene profesional y está viendo horas."},
    {"id": "agendado",       "label": "Agendado",
     "politica": "Cerró la hora."},
    {"id": "atendido",       "label": "Atendido",
     "politica": "Vino y quedó cobrado. Cruzado con el panel de pagos."},
]

CLASES = {
    "expedite":   {"label": "Sin cerrar", "orden": 0,
                   "politica": "Máxima prioridad. Vio horarios y no confirmó."},
    "fecha_fija": {"label": "Hora próxima", "orden": 1,
                   "politica": "Tiene hora hoy o mañana. El calendario manda."},
    "estandar":   {"label": "Espera respuesta", "orden": 2,
                   "politica": "FIFO: siempre se saca la más vieja."},
    "intangible": {"label": "Sin intención", "orden": 3,
                   "politica": "Escribió sin decir a qué. Sin costo de demora."},
}


def _ahora():
    return datetime.now(_CLT)


def _parse(ts: str | None):
    if not ts:
        return None
    try:
        return datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=_CLT)
    except Exception:
        return None


def _espera_min(card: dict) -> int | None:
    """Minutos que el paciente lleva esperando NUESTRA respuesta.

    Solo cuenta si el último mensaje es de él. Si el último es nuestro, la pelota
    está en su cancha y no debe figurar como demora nuestra — medir mal esto
    infla la cola con gente que no está esperando nada.
    """
    if (card.get("last_dir") or "") != "in":
        return None
    t = _parse(card.get("last_ts"))
    if not t:
        return None
    return max(int((_ahora() - t).total_seconds() // 60), 0)


def _dias_a_cita(card: dict) -> int | None:
    c = card.get("cita") or {}
    f = c.get("fecha")
    if not f:
        return None
    try:
        y, m, d = (int(x) for x in str(f)[:10].split("-"))
        return (datetime(y, m, d, tzinfo=_CLT).date() - _ahora().date()).days
    except Exception:
        return None


def clasificar(card: dict) -> tuple[str, str]:
    """Devuelve (columna, clase_de_servicio) para una tarjeta.

    El orden de las preguntas importa: primero lo que más cuesta perder.
    """
    etapa = card.get("etapa")
    espera = _espera_min(card)
    d_cita = _dias_a_cita(card)

    # 1. Vio horarios y no cerró. Intención máxima, ventana corta.
    if etapa == "profesional" and not card.get("cita"):
        return "sin_cerrar", "expedite"

    # 2. Tiene hora hoy o mañana: el tiempo lo fija el calendario.
    if d_cita is not None and 0 <= d_cita <= 1:
        return "agendado", "fecha_fija"

    # 3. Está esperando que le contestemos.
    # OJO: "recepción tomó el control" (HUMAN_TAKEOVER) NO alcanza — si el
    # último mensaje es NUESTRO, ya le contestamos y la pelota está en su
    # cancha. Contarlo como espera infla la cola con gente que no espera nada
    # y vuelve inútil el número que más importa del tablero.
    if espera is not None or card.get("msg_estado") == "no_visto":
        return "responder", "estandar"

    # 4. En conversación, con un área ya declarada.
    if etapa in ("area", "profesional"):
        return "eligiendo", "estandar"

    # 5. Con hora más adelante: no requiere acción hoy, fuera del tablero.
    # Tener hora para dentro de dos semanas no es trabajo pendiente de nadie.
    if card.get("cita"):
        return None, "fecha_fija"

    return "eligiendo", "intangible"


def construir(cards: list[dict]) -> dict:
    """Aplica política de salida, clasifica y ordena. Entra el board v1, sale la cola."""
    ahora = _ahora()
    fuera_cerradas = fuera_sin_intencion = fuera_agendadas = 0
    vivas = []

    for c in cards:
        # POLÍTICA DE SALIDA: lo atendido se va del tablero SIEMPRE, no a las
        # 24 h. El paciente ya vino y ya pagó — no hay nada que hacer con esa
        # tarjeta. Medido contra producción: 54 de 220 tarjetas eran atenciones
        # ya cerradas del mismo día ocupando la columna más grande del tablero.
        # No desaparecen: se cuentan como ATENDIDAS HOY, que es el rendimiento
        # del equipo y merece ser un número, no 54 tarjetas que hay que scrollear.
        if c.get("etapa") == "atendido":
            fuera_cerradas += 1
            continue

        col, clase = clasificar(c)
        espera = _espera_min(c)

        # Sin columna = no es trabajo de nadie hoy (hora agendada para más
        # adelante). Sale del tablero, se cuenta aparte.
        if col is None:
            fuera_agendadas += 1
            continue

        # Los "escribió y no dijo a qué", sin espera nuestra, no ocupan tablero.
        # Se cuentan aparte para que el número no desaparezca sin explicación.
        if clase == "intangible" and not espera:
            fuera_sin_intencion += 1
            continue

        d = dict(c)
        d["columna"] = col
        d["clase"] = clase
        d["espera_min"] = espera
        d["dias_cita"] = _dias_a_cita(c)
        vivas.append(d)

    # Orden dentro de cada columna: primero la clase más cara, y dentro de la
    # misma clase la que lleva MÁS rato esperando. Es la regla FIFO del método
    # ("saca siempre la más vieja"), que es justo lo contrario de ordenar por
    # lo más reciente como hacía la v1.
    vivas.sort(key=lambda x: (CLASES[x["clase"]]["orden"], -(x["espera_min"] or 0)))

    por_col = {c["id"]: [] for c in COLUMNAS}
    for v in vivas:
        por_col.setdefault(v["columna"], []).append(v)

    columnas = []
    for meta in COLUMNAS:
        items = por_col.get(meta["id"], [])
        tope = WIP.get(meta["id"])
        # La columna se enciende si ALGO lleva demasiado esperando — no por
        # tener muchas tarjetas. Ver MINUTOS_ALARMA.
        vieja = max((i.get("espera_min") or 0) for i in items) if items else 0
        columnas.append({**meta, "items": items, "n": len(items), "wip": tope,
                         "mas_vieja": vieja,
                         "alarma": vieja > MINUTOS_ALARMA,
                         "excedido": bool(tope and len(items) > tope)})

    # ── Vista embudo: TODAS las tarjetas por etapa del paciente ──────────
    # Acá no se filtra nada: el sentido del embudo es justamente ver cuántos se
    # quedan en el camino. Filtrar sería borrar la respuesta.
    por_etapa = {e["id"]: [] for e in ETAPAS_EMBUDO}
    for c in cards:
        por_etapa.setdefault(c.get("etapa") or "primer_mensaje", []).append({
            **c, "espera_min": _espera_min(c), "clase": clasificar(c)[1]})
    # OJO: esto es una FOTO de dónde está cada quien ahora, NO una conversión.
    # Calcular "cuántos pasaron de una etapa a la siguiente" dividiendo estos
    # números da disparates (los agendados de hoy no salieron de los que ahora
    # están eligiendo horario: pasaron por ahí hace días). La conversión real se
    # mide siguiendo cohortes en el tiempo y vive en el endpoint /friccion.
    base = len(cards) or 1
    embudo = [{**e,
               "items": sorted(por_etapa.get(e["id"], []),
                               key=lambda x: -(x["espera_min"] or 0)),
               "n": len(por_etapa.get(e["id"], [])),
               "pct": round(len(por_etapa.get(e["id"], [])) / base * 100, 1)}
              for e in ETAPAS_EMBUDO]

    esperas = [v["espera_min"] for v in vivas if v["espera_min"]]
    return {
        "embudo": embudo,
        "columnas": columnas,
        "clases": CLASES,
        "total_vivas": len(vivas),
        "fuera_cerradas": fuera_cerradas,          # = atendidas hoy (rendimiento)
        "fuera_sin_intencion": fuera_sin_intencion,
        "fuera_agendadas": fuera_agendadas,        # con hora para más adelante
        "espera_max": max(esperas) if esperas else 0,
        "espera_mediana": sorted(esperas)[len(esperas) // 2] if esperas else 0,
        "esperando": len(esperas),
        "minutos_alarma": MINUTOS_ALARMA,
        "expedite": sum(1 for v in vivas if v["clase"] == "expedite"),
        "generado": ahora.strftime("%H:%M"),
    }
