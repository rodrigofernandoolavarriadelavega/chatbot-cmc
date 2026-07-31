"""Franja horaria pedida por el paciente — FUENTE ÚNICA DE VERDAD.

Antes de este módulo el bot tenía TRES tablas de rangos distintas y dos
parsers de franja incompatibles, todos en flows.py:

  - _detectar_franja_horaria (:131)  mañana 8-12  · tarde 12-18 · noche 18-22
  - intent buscar_fecha      (:2486) mañana 7-13  · tarde 13-19 · noche >=19
  - _PERIODOS                (:7795) mañana 0-12  · tarde 14-19 · noche 19-24

El mismo slot de las 12:30 era "mañana" por un camino y "tarde" por otro, y
un slot de las 13:00 aparecía o no según qué handler lo procesara.

Caso real que motivó el módulo (Sara Bustamante, 56981712917, 2026-05-14):
escribió "Disponibilidad para kinesiologo para mañana durante la mañana" y
recibió horarios de 17:20 y 18:00. "durante la mañana" no estaba en ninguna
de las listas de keywords, así que la franja se perdió entera.

Peor: la detección de DÍA y la de FRANJA eran EXCLUYENTES. _detectar_fecha_
pedida_idle miraba una lista de 4 strings para decidir si "mañana" era día o
franja, así que "quiero hora durante la mañana" se leía como el DÍA siguiente
y el paciente que quería HOY temprano quedaba agendado para el otro día.
Acá son aditivas: un mensaje puede traer día Y franja, y se devuelven ambos.
"""

from __future__ import annotations

import re

# ── Tabla única de rangos ────────────────────────────────────────────────────
# Half-open: h_min <= hora < h_max. Half-open evita el solape que tenía el
# filtro viejo de flows.py:14574 (usaba <= en ambos extremos, así que un slot
# de 12:00 contaba como mañana Y como tarde según quién preguntara).
#
# Cortes elegidos para el CMC, que atiende ~08:00-20:00: la mañana llega hasta
# las 13 porque un slot de 12:40 el paciente rural todavía lo vive como
# "en la mañana" (alcanza a volverse en la micro del mediodía).
FRANJAS: dict[str, tuple[int, int]] = {
    "mañana":   (8, 13),
    "mediodia": (12, 15),
    "tarde":    (13, 19),
    "noche":    (19, 23),
}

# Etiqueta en español para armar mensajes ("No tengo horas en *la mañana*").
LABELS: dict[str, str] = {
    "mañana":   "la mañana",
    "mediodia": "el mediodía",
    "tarde":    "la tarde",
    "noche":    "la noche",
}

# Preposiciones que anteceden a la franja. La lista vieja solo tenía "en la" y
# "por la"; faltaban "durante la" (el caso de Sara), "para la", "a la" y el
# uso pelado ("hora tarde por favor"). El grupo es opcional justamente para
# cubrir ese último caso.
_PREPS = r"en|por|durante|para|a|de"
_PREP = rf"(?:\b(?:{_PREPS})\s+la\s+|\b(?:en|por|durante|para)\s+el\s+)?"

# Construcción "<preposición> la mañana" — el único patrón donde "mañana" es
# franja horaria y NO el día siguiente. Se usa en los tres puntos del módulo
# (parse, es_franja_no_dia, dia_y_franja) para que no puedan divergir: tener
# tres copias de esta regla fue exactamente el bug que el módulo elimina.
_RE_MANANA_FRANJA = re.compile(rf"\b(?:{_PREPS})\s+la\s+ma[ñn]an(?:a|ita)\b")

_RE_MANANA = re.compile(_PREP + r"ma[ñn]an(?:a|ita)\b")
_RE_TARDE = re.compile(_PREP + r"tard(?:e|ecita|ecito)\b")
_RE_NOCHE = re.compile(_PREP + r"noche\b")
_RE_MEDIODIA = re.compile(r"medio\s?d[íi]a\b")

# "temprano" / "más tarde" son franjas relativas, no absolutas: el paciente las
# usa sin preposición y sin sustantivo ("algo tempranito").
_RE_TEMPRANO = re.compile(r"\btempran(?:o|ito)\b")
_RE_MAS_TARDE = re.compile(r"\bm[áa]s\s+tard(?:e|ecito)\b")

_RE_DESPUES = re.compile(r"despu[ée]s\s+de\s+las?\s+(\d{1,2})")
_RE_ANTES = re.compile(r"antes\s+de\s+las?\s+(\d{1,2})")

# "mañana" como DÍA, no como franja: solo cuando va sin preposición de franja.
# "para mañana" es ambiguo en Chile ("para mañana" = el día siguiente), por eso
# se resuelve en dia_y_franja() con la regla de precedencia documentada ahí.
_RE_MANANA_DIA = re.compile(r"\b(?:para\s+)?ma[ñn]ana\b")


# Aplazamiento de la CONVERSACIÓN, no preferencia de HORARIO. En Chile las
# expresiones temporales son ambiguas por defecto y lo que desambigua es el
# verbo que las sigue: "más tarde le confirmo" / "en la tarde le aviso" son
# cortesía para cortar, no una hora pedida.
# Caso real (56959883429, 2026-07-29 12:56): escribió "Más tarde le confirmo"
# y quedó con franja 15-23 pegada en la sesión; dos minutos después el bot le
# filtró los slots del 3 de agosto con una preferencia que nunca pidió.
_RE_APLAZAMIENTO = re.compile(
    r"\b(?:m[áa]s\s+tarde|en\s+la\s+tarde|por\s+la\s+tarde|despu[ée]s|"
    r"en\s+la\s+ma[ñn]ana|ma[ñn]ana)\b"
    r"(?:\s+(?:se|le|te|les|me|nos|lo|la))?"
    r"\s+(?:confirm|avis|dig|dec|llam|habl|respond|contest|escrib|"
    r"coment|decid|pregunt|consult|convers)\w*"
)


def _es_aplazamiento(tl: str) -> bool:
    """True si la expresión temporal aplaza la conversación en vez de pedir hora."""
    return bool(_RE_APLAZAMIENTO.search(tl))


def _pm(hora: int, texto: str) -> int:
    """Convierte a 24h una hora ambigua ('después de las 5' → 17 si dice tarde)."""
    if hora <= 12 and re.search(r"tarde|noche|\bpm\b|p\.m", texto):
        return hora + 12
    return hora


def parse(txt: str) -> tuple[int, int] | None:
    """Franja horaria pedida, como (h_min, h_max) half-open. None si no hay.

    El orden importa: las franjas explícitas con hora ("después de las 5")
    ganan sobre las nominales ("en la tarde"), porque son más específicas.
    """
    if not txt:
        return None
    tl = txt.lower()
    # "Más tarde le confirmo" no pide una hora — corta la conversación.
    if _es_aplazamiento(tl):
        return None

    m = _RE_DESPUES.search(tl)
    if m:
        return (_pm(int(m.group(1)), tl), 23)
    m = _RE_ANTES.search(tl)
    if m:
        return (8, _pm(int(m.group(1)), tl))

    if _RE_MAS_TARDE.search(tl):
        return (15, 23)
    if _RE_TEMPRANO.search(tl):
        return (8, 11)
    if _RE_MEDIODIA.search(tl):
        return FRANJAS["mediodia"]
    if _RE_NOCHE.search(tl):
        return FRANJAS["noche"]
    if _RE_TARDE.search(tl):
        return FRANJAS["tarde"]
    # "mañana" es franja SOLO si viene con preposición ("en/por/durante la
    # mañana"). "mañana" pelado o "para mañana" es el día siguiente y lo
    # resuelve dia_y_franja(). Sin este chequeo, "vengo mañana" filtraría
    # slots AM sin que el paciente lo pidiera.
    if _RE_MANANA_FRANJA.search(tl):
        return FRANJAS["mañana"]
    return None


def es_franja_no_dia(txt: str) -> bool:
    """True si el 'mañana' del texto es franja horaria y NO el día siguiente.

    Reemplaza la lista hardcodeada de 4 strings de flows.py:89, que dejaba
    fuera "durante la mañana" / "para la mañana" / "a la mañana".
    """
    if not txt:
        return False
    return bool(_RE_MANANA_FRANJA.search(txt.lower()))


def dia_y_franja(txt: str) -> tuple[bool, tuple[int, int] | None]:
    """Devuelve (menciona_dia_manana, franja). ADITIVO, no excluyente.

    Un mensaje puede traer las dos cosas: "para mañana durante la mañana"
    → (True, (8, 13)). El código viejo devolvía una u otra y perdía la que
    no ganara. El caller decide qué hacer con cada una.

    Regla de precedencia para el "mañana" ambiguo:
      - "en/por/durante/a la mañana"  → SOLO franja  (no es el día)
      - "para mañana", "mañana"       → SOLO día
      - ambas construcciones presentes → día Y franja
    """
    if not txt:
        return (False, None)
    tl = txt.lower()
    franja = parse(tl)

    # Un "mañana" que ya fue consumido como franja no cuenta como día. Se borra
    # del texto la construcción de franja y se busca si queda otro "mañana":
    # así "para mañana durante la mañana" devuelve día Y franja, mientras que
    # "una hora para la mañana" devuelve solo franja.
    tl_sin_franja = _RE_MANANA_FRANJA.sub(" ", tl)
    menciona_dia = bool(_RE_MANANA_DIA.search(tl_sin_franja))
    return (menciona_dia, franja)


def filtrar(slots: list, franja: tuple[int, int] | None) -> list:
    """Slots dentro de la franja. Sin franja devuelve la lista intacta.

    Un slot sin hora_inicio legible se CONSERVA: es preferible mostrar un
    horario de más que perder al paciente por un dato mal formado.
    """
    if not franja or not slots:
        return slots
    h_min, h_max = franja
    out = []
    for s in slots:
        raw = s.get("hora_inicio") or s.get("hora") or ""
        try:
            h = int(str(raw)[:2])
        except (ValueError, TypeError):
            out.append(s)
            continue
        if h_min <= h < h_max:
            out.append(s)
    return out


def label(franja: tuple[int, int] | None) -> str:
    """Etiqueta en español para el mensaje al paciente.

    Cae a un rango explícito ("entre las 15 y las 23 h") cuando la franja no
    calza con ninguna nominal — pasa con "después de las 3".
    """
    if not franja:
        return ""
    for nombre, rango in FRANJAS.items():
        if rango == franja:
            return LABELS[nombre]
    h_min, h_max = franja
    if h_max >= 23:
        return f"después de las {h_min}"
    if h_min <= 8:
        return f"antes de las {h_max}"
    return f"entre las {h_min} y las {h_max} h"


def sin_cupo_en_franja(
    franja: tuple[int, int] | None,
    slots_del_dia: list,
    fecha_pedida: str,
) -> dict:
    """POLÍTICA: qué ofrecer cuando el día pedido NO tiene cupo en la franja.

    Este es el punto exacto donde se perdió a Sara. Pidió el viernes por la
    mañana; el viernes no tenía nada en la mañana; el bot le mostró 17:20 sin
    decir una palabra sobre la franja. Ella respondió "Quiero viernes",
    volvió a chocar, y terminó en recepción.

    slots_del_dia son los slots del día pedido que quedaron FUERA de la franja
    (los de la tarde, en el caso de Sara). fecha_pedida es YYYY-MM-DD.

    Debe devolver:
        {"accion": "...", "slots": [...], "mensaje": "..."}

    donde "accion" es una de:
        "mostrar_fuera_franja" — mostrar los slots del día fuera de la franja,
                                 avisando que no hay en la franja pedida
        "buscar_otro_dia"      — no mostrar nada de este día; el caller busca
                                 el próximo día CON cupo en la franja pedida
        "ambos"                — mostrar los de fuera de franja Y ofrecer
                                 buscar otro día en la franja
    """
    # TODO(Rodrigo): implementar la política.
    raise NotImplementedError("política sin_cupo_en_franja pendiente")
