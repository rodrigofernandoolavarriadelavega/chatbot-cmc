"""Parser robusto de expresiones de hora en español chileno.

Maneja ~100 variaciones: numérico (10:30, 1030, 10h30, 10 30, 10-30),
AM/PM (10 am, 10pm, 10 p.m.), palabras (diez y media, cuarto para las 11,
mediodía), prefijos (a las, tipo, sobre las), y sufijos (hrs, horas, hs).

Uso:
    from time_parser import parse_hora
    parse_hora("10:30") == (10, 30)
    parse_hora("diez y media") == (10, 30)
    parse_hora("5 de la tarde") == (17, 0)
"""
from __future__ import annotations

import re
import unicodedata
from typing import Optional, Tuple


_NUM_PALABRA_HORA = {
    "cero": 0, "una": 1, "uno": 1, "dos": 2, "tres": 3, "cuatro": 4,
    "cinco": 5, "seis": 6, "siete": 7, "ocho": 8, "nueve": 9,
    "diez": 10, "once": 11, "doce": 12, "trece": 13, "catorce": 14,
    "quince": 15, "dieciseis": 16, "diecisiete": 17, "dieciocho": 18,
    "diecinueve": 19, "veinte": 20, "veintiuno": 21, "veintidos": 22,
    "veintitres": 23, "veinticuatro": 24,
}


def _normalizar(t: str) -> str:
    """Minúsculas, sin tildes (preserva ñ), espacios colapsados."""
    t = t.lower().strip()
    out = []
    for c in unicodedata.normalize("NFD", t):
        if unicodedata.category(c) == "Mn":
            continue
        out.append(c)
    t = "".join(out)
    return re.sub(r"\s+", " ", t)


def _parse_num(raw: str) -> Optional[int]:
    """'10' → 10; 'diez' → 10; otro → None."""
    raw = raw.strip()
    if raw.isdigit():
        n = int(raw)
        if 0 <= n <= 24:
            return n
    return _NUM_PALABRA_HORA.get(raw)


def _resolver_ampm(h: int, ampm: str, ctx: str) -> int:
    """Aplica AM/PM explícito, marcadores de período, y heurística clínica."""
    ampm = ampm.replace(".", "").replace(" ", "")
    if ampm == "pm" and h < 12:
        return h + 12
    if ampm == "am" and h == 12:
        return 0
    if ampm:
        return h
    tarde_noche = re.search(r"\bde\s+la\s+(tarde|noche)\b", ctx) \
                  or re.search(r"\ben\s+la\s+(tarde|noche)\b", ctx) \
                  or re.search(r"\bpor\s+la\s+(tarde|noche)\b", ctx)
    manana = re.search(r"\bde\s+la\s+(manana|madrugada)\b", ctx) \
             or re.search(r"\ben\s+la\s+(manana|madrugada)\b", ctx) \
             or re.search(r"\bpor\s+la\s+(manana|madrugada)\b", ctx)
    if tarde_noche and h < 12:
        return h + 12
    if re.search(r"\bde\s+la\s+noche\b", ctx) and h == 12:
        return 0
    if manana and h == 12:
        return 0
    # Heurística clínica (CMC opera 08:00–21:00): hora 1–7 sin calificador → PM
    if 1 <= h <= 7:
        return h + 12
    return h


_DESCALIFICADORES = (
    "anos", "ano", "meses", "mes", "semana", "semanas",
    "hijos", "hijas", "hijo", "hija", "nietos", "nieto",
    "kilos", "kilo", "kg", "gramos", "libras",
    "metros", "metro", "km", "centimetros", "cm",
    "veces", "numero", "rut", "codigo", "whatsapp",
    "grados", "fiebre", "temperatura",
)


def parse_hora(texto: str) -> Optional[Tuple[int, int]]:
    """Extrae (hora, minuto) de texto libre en español. None si no aplica.

    Rechaza si el texto contiene palabras claramente no-horarias ("años",
    "hijos", "kilos", etc.) para evitar que "mi hijo tiene 10 años" se
    interprete como las 10:00.
    """
    if not texto:
        return None
    t = _normalizar(texto)
    if any(re.search(rf"\b{w}\b", t) for w in _DESCALIFICADORES):
        return None

    # Mediodía / medianoche
    if re.search(r"\b(al\s+)?mediod[ií]?a\b|\bmedio\s?dia\b", t):
        return (12, 0)
    if re.search(r"\bmedia\s?noche\b", t):
        return (0, 0)

    # "cuarto para las N" / "un cuarto para las N"  → (N-1):45
    m = re.search(
        r"\b(?:un\s+)?cuarto\s+(?:para|pal?)\s+(?:las?\s+)?([a-zñ]+|\d{1,2})\b",
        t,
    )
    if m:
        h = _parse_num(m.group(1))
        if h is not None:
            new_h = (h - 1) % 24
            return (_resolver_ampm(new_h, "", t), 45)

    # "N menos cuarto" / "N menos M"
    m = re.search(
        r"\b([a-zñ]+|\d{1,2})\s+menos\s+(cuarto|\d{1,2})\b",
        t,
    )
    if m:
        h = _parse_num(m.group(1))
        if h is not None:
            mins = 15 if m.group(2) == "cuarto" else int(m.group(2))
            new_h = (h - 1) % 24
            new_m = 60 - mins
            if new_m == 60:
                new_m = 0
                new_h = (new_h + 1) % 24
            return (_resolver_ampm(new_h, "", t), new_m)

    # Expresiones minuto multi-palabra (antes del reemplazo simple de dígitos)
    t2 = t
    t2 = re.sub(r"\btres\s+cuartos\b", "45", t2)
    t2 = re.sub(r"\bcuarenta\s+y\s+cinco\b", "45", t2)
    t2 = re.sub(r"\btreinta\s+y\s+cinco\b", "35", t2)
    t2 = re.sub(r"\bcincuenta\s+y\s+cinco\b", "55", t2)

    # Reemplazar palabras-número con dígitos (más largas primero)
    palabras_orden = sorted(_NUM_PALABRA_HORA.keys(), key=len, reverse=True)
    for p in palabras_orden:
        t2 = re.sub(rf"\b{p}\b", str(_NUM_PALABRA_HORA[p]), t2)

    # "N y media/cuarto"  (tres cuartos ya se resolvió arriba)
    t2 = re.sub(r"\b(\d{1,2})\s*y\s+media\b", r"\1:30", t2)
    t2 = re.sub(r"\b(\d{1,2})\s*y\s+cuarto\b", r"\1:15", t2)
    # "N y treinta/cuarenta/cincuenta/quince"  (variantes con "y")
    t2 = re.sub(r"\b(\d{1,2})\s+y\s+treinta\b", r"\1:30", t2)
    t2 = re.sub(r"\b(\d{1,2})\s+y\s+cuarenta\b", r"\1:40", t2)
    t2 = re.sub(r"\b(\d{1,2})\s+y\s+cincuenta\b", r"\1:50", t2)
    t2 = re.sub(r"\b(\d{1,2})\s+y\s+quince\b", r"\1:15", t2)
    # "N treinta/cuarenta/cincuenta/quince" (sin "y")
    t2 = re.sub(r"\b(\d{1,2})\s+treinta\b", r"\1:30", t2)
    t2 = re.sub(r"\b(\d{1,2})\s+cuarenta\b", r"\1:40", t2)
    t2 = re.sub(r"\b(\d{1,2})\s+cincuenta\b", r"\1:50", t2)
    t2 = re.sub(r"\b(\d{1,2})\s+quince\b", r"\1:15", t2)
    # "N y M"  (M puro numérico, ej. "10 y 30")
    t2 = re.sub(r"\b(\d{1,2})\s+y\s+(\d{1,2})\b", r"\1:\2", t2)

    # Regex principal: captura hora, minutos opcionales, sufijo am/pm/hrs
    m = re.search(
        r"(?:a\s+eso\s+de\s+las?|a\s+las?|como\s+a\s+las?|tipo|"
        r"sobre\s+las?|cerca\s+de\s+las?|por\s+ah[ií]\s+de\s+las?|"
        r"para\s+las?|las?)?\s*"
        r"\b(\d{1,2})"
        r"(?:\s*[:.\-,]\s*(\d{2})"
        r"|\s*h\s*(\d{2})"
        r"|\s+(\d{2})(?!\d)"
        r"|(\d{2})(?!\d))?"
        r"\s*(a\.?m\.?|p\.?m\.?|hrs?|horas?|hs|h)?\b",
        t2,
    )
    if not m:
        return None

    h = int(m.group(1))
    mins_raw = m.group(2) or m.group(3) or m.group(4) or m.group(5) or "0"
    mins = int(mins_raw)
    ampm = (m.group(6) or "").lower()

    h = _resolver_ampm(h, ampm, t)
    if 0 <= h <= 23 and 0 <= mins <= 59:
        return (h, mins)
    return None
