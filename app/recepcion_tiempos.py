# -*- coding: utf-8 -*-
"""Rapidez de recepción: cuánto tarda una persona en recibir la primera
respuesta HUMANA, contando solo minutos en horario de atención.

Funciones PURAS (sin base de datos): reciben los mensajes de una persona y
devuelven el tiempo. Las usan el panel de Campañas Meta (curva de rapidez vs.
agendamiento) y la alerta de takeover sin respuesta (`meta_alertas.py`).

Cómo se reconoce cada cosa en `messages`
---------------------------------------
- Respuesta humana: mensaje `out` cuyo texto empieza con "[Recepcionista] "
  (prefijo que pone `admin_routes.responder_como_recepcion`). Los avisos de
  sistema "[Recepcionista tomó la conversación]" NO cuentan (no llevan "] ").
- Paso a recepción: primer mensaje NO humano registrado con state =
  'HUMAN_TAKEOVER' (el aviso del bot al derivar, o la persona escribiendo ya
  derivada), antes de cualquier respuesta humana.

Desde cuándo se mide
--------------------
- Si el bot la pasó a recepción: desde ese momento hasta la 1ª respuesta humana.
- Si recepción le escribió primero por iniciativa propia: "proactiva", sin
  espera que medir (fuera de la curva, se cuenta aparte).
- Si nunca pasó a recepción ni le escribió un humano: el bot la atendió entero.

Horario de atención (recepción del CMC, mismo que informa el bot en
`claude_helper.py`): lunes a viernes 08:00–21:00, sábado 09:00–14:00,
domingo cerrado. Un mensaje de madrugada empieza a contar a la apertura.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from statistics import median
from zoneinfo import ZoneInfo

CL = ZoneInfo("America/Santiago")

# weekday() → (minuto de apertura, minuto de cierre)
HORARIO: dict[int, tuple[int, int]] = {0: (480, 1260), 1: (480, 1260), 2: (480, 1260),
                                       3: (480, 1260), 4: (480, 1260), 5: (540, 840)}
PREFIJO_HUMANO = "[Recepcionista] "
ESTADO_RECEPCION = "HUMAN_TAKEOVER"

TRAMOS = [
    ("lt15", "Menos de 15 min", 0, 15),
    ("15_60", "15 a 60 min", 15, 60),
    ("1_4h", "1 a 4 horas", 60, 240),
    ("gt4h", "Más de 4 horas", 240, None),
    ("sin_respuesta", "Sin respuesta", None, None),
]
TRAMO_LBL = {t[0]: t[1] for t in TRAMOS}

# Tramos finos (Campañas Meta → "Velocidad de respuesta"): lo que pidió el
# dueño. Se mide igual que TRAMOS (minutos hábiles hasta la 1ª respuesta
# humana); solo cambian los cortes: la conversación de un anuncio se enfría en
# minutos, no en horas.
TRAMOS_FINOS = [
    ("lt5", "Menos de 5 min", 0, 5),
    ("5_30", "5 a 30 min", 5, 30),
    ("30_120", "30 min a 2 horas", 30, 120),
    ("gt2h", "Más de 2 horas", 120, None),
    ("sin_respuesta", "Sin respuesta", None, None),
]
TRAMO_FINO_LBL = {t[0]: t[1] for t in TRAMOS_FINOS}


def en_horario(epoch: int, horario: dict | None = None) -> bool:
    horario = horario or HORARIO
    dt = datetime.fromtimestamp(epoch, CL)
    h = horario.get(dt.weekday())
    m = dt.hour * 60 + dt.minute
    return bool(h) and h[0] <= m < h[1]


def minutos_habiles(t0: int, t1: int, horario: dict | None = None) -> float:
    """Minutos entre dos epochs que caen dentro del horario de atención."""
    horario = horario or HORARIO
    if t1 <= t0:
        return 0.0
    total = 0.0
    a = datetime.fromtimestamp(t0, CL)
    b = datetime.fromtimestamp(t1, CL)
    dia = a.date()
    while dia <= b.date():
        h = horario.get(dia.weekday())
        if h:
            ini = datetime(dia.year, dia.month, dia.day, tzinfo=CL) + timedelta(minutes=h[0])
            fin = datetime(dia.year, dia.month, dia.day, tzinfo=CL) + timedelta(minutes=h[1])
            lo, hi = max(a, ini), min(b, fin)
            if hi > lo:
                total += (hi - lo).total_seconds() / 60
        dia += timedelta(days=1)
    return round(total, 1)


def es_humano(direction: str, texto: str | None) -> bool:
    return direction == "out" and (texto or "").startswith(PREFIJO_HUMANO)


def respuesta_humana(mensajes: list[dict], desde: int = 0, ahora: int | None = None,
                     horario: dict | None = None) -> dict:
    """Primera respuesta humana de UNA persona, desde que el bot la pasó a
    recepción.

    `mensajes`: dicts con ts (epoch), dir ('in'/'out'), texto, state; en
    cualquier orden. Solo se miran los de `desde` en adelante (la llegada).

    - El paso a recepción es el primer mensaje registrado en HUMAN_TAKEOVER que
      NO es de recepción (el aviso del bot "le aviso a recepción" o un mensaje
      de la persona ya derivada), ANTES de cualquier mensaje humano.
    - Si recepción escribió primero (tomó la conversación por iniciativa
      propia, p. ej. el saludo "[Recepcionista] Hola 👋 ¿En qué…"), no hubo
      espera que medir: `proactiva=True` y queda fuera de la curva. Medido en
      prod 2026-10-05: sin esta regla la mediana daba 0 min, porque el propio
      saludo de recepción es el primer mensaje en HUMAN_TAKEOVER.

    Devuelve: necesito, proactiva, inicio, fin, minutos (hábiles; sin
    respuesta y con `ahora`, minutos de espera hasta ahora), respondida, tramo
    (id de TRAMOS, o None si no hubo espera)."""
    ms = sorted((m for m in mensajes if (m.get("ts") or 0) >= desde), key=lambda m: m["ts"])
    nada = {"necesito": False, "proactiva": False, "inicio": None, "fin": None, "minutos": None,
            "respondida": False, "tramo": None}
    primer_humano = next((m["ts"] for m in ms if es_humano(m.get("dir", ""), m.get("texto"))), None)
    takeover = next((m["ts"] for m in ms if (m.get("state") or "") == ESTADO_RECEPCION
                     and not es_humano(m.get("dir", ""), m.get("texto"))
                     and (primer_humano is None or m["ts"] <= primer_humano)), None)
    if takeover is None:
        return {**nada, "proactiva": primer_humano is not None}
    fin = primer_humano
    if fin is not None:
        mins = minutos_habiles(takeover, fin, horario)
        return {"necesito": True, "proactiva": False, "inicio": takeover, "fin": fin, "minutos": mins,
                "respondida": True, "tramo": tramo(mins)}
    espera = minutos_habiles(takeover, ahora, horario) if ahora else None
    return {"necesito": True, "proactiva": False, "inicio": takeover, "fin": None, "minutos": espera,
            "respondida": False, "tramo": "sin_respuesta"}


def tramo(minutos: float | None, respondida: bool = True) -> str:
    if not respondida or minutos is None:
        return "sin_respuesta"
    for tid, _, lo, hi in TRAMOS:
        if lo is None:
            continue
        if minutos >= lo and (hi is None or minutos < hi):
            return tid
    return "gt4h"


def tramo_fino(minutos: float | None, respondida: bool = True) -> str:
    """Igual que `tramo` pero con los cortes de TRAMOS_FINOS (<5, 5-30, 30-120, >2 h)."""
    if not respondida or minutos is None:
        return "sin_respuesta"
    for tid, _, lo, hi in TRAMOS_FINOS:
        if lo is None:
            continue
        if minutos >= lo and (hi is None or minutos < hi):
            return tid
    return "gt2h"


def curva_fina(personas: list[dict]) -> list[dict]:
    """Como `curva`, con TRAMOS_FINOS. personas: dicts con 'minutos',
    'respondida' y 'agendo'."""
    out = []
    por = [(tramo_fino(p.get("minutos"), bool(p.get("respondida"))), p) for p in personas]
    for tid, lbl, _, _ in TRAMOS_FINOS:
        g = [p for t, p in por if t == tid]
        ag = sum(1 for p in g if p.get("agendo"))
        out.append({"tramo": tid, "label": lbl, "personas": len(g), "agendaron": ag,
                    "pct": round(100 * ag / len(g)) if g else None})
    return out


def mediana(valores: list[float]) -> float | None:
    v = [x for x in valores if x is not None]
    return round(median(v), 1) if v else None


def curva(personas: list[dict]) -> list[dict]:
    """personas: dicts con 'tramo' y 'agendo' (bool). Por tramo: cuántas y %
    que agendó. Solo las que necesitaron recepción (tramo no None)."""
    out = []
    for tid, lbl, _, _ in TRAMOS:
        g = [p for p in personas if p.get("tramo") == tid]
        ag = sum(1 for p in g if p.get("agendo"))
        out.append({"tramo": tid, "label": lbl, "personas": len(g), "agendaron": ag,
                    "pct": round(100 * ag / len(g)) if g else None})
    return out


def espera_actual(mensajes: list[dict], ahora: int, horario: dict | None = None) -> dict | None:
    """Episodio de espera ABIERTO: mensajes de la persona en HUMAN_TAKEOVER
    posteriores a la última respuesta humana. Devuelve {inicio, minutos
    hábiles hasta ahora} o None si no hay nadie esperando."""
    ms = sorted(mensajes, key=lambda m: m["ts"])
    ult_humano = max((m["ts"] for m in ms if es_humano(m.get("dir", ""), m.get("texto"))), default=None)
    pend = [m["ts"] for m in ms if m.get("dir") == "in" and (m.get("state") or "") == ESTADO_RECEPCION
            and (ult_humano is None or m["ts"] > ult_humano) and m["ts"] <= ahora]
    if not pend:
        return None
    inicio = pend[0]
    return {"inicio": inicio, "minutos": minutos_habiles(inicio, ahora, horario)}
