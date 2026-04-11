"""Cliente delgado del servicio de triage del GES Clinical Assistant.

Expone `triage_sintomas(texto)` que llama al endpoint POST /triage y
normaliza la respuesta para el flujo conversacional del chatbot.

El servicio devuelve, para un texto libre del paciente, las patologías
más probables, la especialidad recomendada del CMC y si requiere derivar
a urgencias (cuando la patología #1 del ranking es tiempo-dependiente).

Falla silenciosamente: si el servicio está caído o el texto no matchea
nada, devolvemos `None` y el chatbot cae en `detect_intent` como antes.
"""
from __future__ import annotations

import re
import unicodedata

import httpx

from config import GES_ASSISTANT_URL


# ── Normalización ortográfica para WhatsApp rural chileno ────────────────────
# Los pacientes escriben con abreviaciones, sin tildes, con participios
# coloquiales ("sangrao" en vez de "sangrado"), letras pegadas y faltas
# frecuentes. El motor GES matchea por substring sobre sinónimos canónicos,
# así que sin esta normalización perdemos recall. Estos reemplazos son
# seguros porque son tokens aislados (\b) o patrones morfológicos comunes.

# Abreviaciones comunes en WhatsApp. Aplicar con word boundaries para no
# destrozar palabras reales que contengan estas letras.
_ABREVIACIONES = {
    "q": "que",
    "xq": "porque",
    "pq": "porque",
    "xfa": "por favor",
    "tb": "también",
    "tbn": "también",
    "tmb": "también",
    "tmbn": "también",
    "d": "de",
    "dl": "del",
    "dnd": "donde",
    "cdo": "cuando",
    "cnd": "cuando",
    "x": "por",
    "dsp": "después",
    "dspues": "después",
    "pal": "para el",
    "pa": "para",
    "tngo": "tengo",
    "tnga": "tenga",
    "dlr": "dolor",
    "dlor": "dolor",
    "dlor": "dolor",
    "kbza": "cabeza",
    "cbza": "cabeza",
    "cbz": "cabeza",
    "grgnta": "garganta",
    "grganta": "garganta",
    "pcho": "pecho",
    "pxo": "pecho",
    "krzn": "corazon",
    "korzn": "corazon",
    "mucha": "mucha",   # placeholder — lo importante son los demás
    "muxo": "mucho",
    "mxo": "mucho",
    "m": "me",          # ej "m duele"
    "t": "te",          # ej "t siento"
    "stoy": "estoy",
    "sta": "esta",
    "ta": "esta",       # "ta bien"
    "bn": "bien",
    "ml": "mal",
    "kiero": "quiero",
    "kero": "quiero",
    "ke": "que",
}

# Participios rurales chilenos: "sangrao" → "sangrado", "hinchao" → "hinchado".
# Regex: palabra que termina en "ao" precedida de consonante, reemplaza por "ado".
# Se limita a ≥5 letras para no tocar palabras cortas como "tao", "bao".
_RE_PARTICIPIO = re.compile(r"\b([a-z]{3,})ao\b")

# Errores ortográficos frecuentes (1-2 letras). Estos son seguros porque no
# colisionan con palabras válidas del español.
_TYPOS = {
    "feber": "fiebre",
    "fieber": "fiebre",
    "feiber": "fiebre",
    "gargnta": "garganta",
    "garnganta": "garganta",
    "estomgo": "estomago",
    "estmago": "estomago",
    "barigga": "barriga",
    "diarea": "diarrea",
    "diarria": "diarrea",
    "vomitos": "vomitos",  # ya está ok, placeholder
    "bomitos": "vomitos",
    "bomito": "vomito",
}


def _strip_tildes(s: str) -> str:
    """Elimina tildes y diacríticos preservando la ñ."""
    s = s.replace("ñ", "\x00")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.replace("\x00", "ñ")


def normalizar_texto_paciente(texto: str) -> str:
    """Normaliza texto libre de un paciente para aumentar recall del motor GES.

    Transformaciones (en orden):
    1. minúscula + sin tildes
    2. colapsa espacios múltiples
    3. expande abreviaciones WhatsApp comunes (q, xq, dlr, kbza, ...)
    4. corrige typos frecuentes (feber→fiebre, diarea→diarrea, ...)
    5. corrige participios rural chilenos (sangrao→sangrado, hinchao→hinchado)

    NO corrige errores semánticos ni reordena palabras. Solo léxico.
    Retorna el texto original si la normalización produce algo vacío.
    """
    if not texto:
        return texto
    t = _strip_tildes(texto.lower())
    t = re.sub(r"\s+", " ", t).strip()
    if not t:
        return texto

    # Tokenizar por espacio y aplicar reemplazos token a token (preserva word
    # boundaries de forma trivial y es más rápido que regex con alternativas).
    tokens = t.split(" ")
    out: list[str] = []
    for tok in tokens:
        # Separa puntuación al final para no perderla
        m = re.match(r"^([a-z0-9ñ]*)([^\w]*)$", tok)
        if not m:
            out.append(tok)
            continue
        core, tail = m.group(1), m.group(2)
        if core in _ABREVIACIONES:
            core = _ABREVIACIONES[core]
        elif core in _TYPOS:
            core = _TYPOS[core]
        out.append(core + tail)
    t = " ".join(out)

    # Participios: aplicar al final porque el reemplazo depende del token
    # completo en su contexto original ya normalizado.
    t = _RE_PARTICIPIO.sub(r"\1ado", t)

    return t if t else texto

# Mapea la especialidad canónica que devuelve el GES Assistant al nombre
# que espera `_iniciar_agendar()` en flows.py (en minúscula, matching con
# _ids_para_especialidad de medilink.py).
_SPECIALTY_MAP: dict[str, str] = {
    "Medicina General":        "medicina general",
    "Cardiología":             "cardiología",
    "Traumatología":           "traumatología",
    "Ginecología":             "ginecología",
    "Otorrinolaringología":    "otorrinolaringología",
    "Gastroenterología":       "gastroenterología",
    "Nutrición":               "nutrición",
    "Psicología Adulto":       "psicología adulto",
    "Psicología Infantil":     "psicología infantil",
    "Kinesiología":            "kinesiología",
    "Fonoaudiología":          "fonoaudiología",
    "Matrona":                 "matrona",
    "Podología":               "podología",
    "Ecografía":               "ecografía",
    "Odontología General":     "odontología",
    "Ortodoncia":              "ortodoncia",
    "Endodoncia":              "endodoncia",
}

# Score mínimo para considerar que el triage tiene una hipótesis real.
# Valores típicos: matches decentes ≥2.0, sólidos ≥2.5.
_MIN_SCORE = 2.0


async def triage_sintomas(texto: str) -> dict | None:
    """Llama al servicio GES y devuelve un dict normalizado, o None si no hay match.

    Retorna:
        {
            "especialidad": str | None,      # nombre canónico CMC (minúscula)
            "needs_urgency": bool,           # True → derivar a SAMU 131
            "top_pathology": str | None,     # nombre de la patología #1
            "top_score": float,              # score de la patología #1
            "matches": list[dict],           # top 5 crudo para logging
        }
        o None si el servicio no respondió, el texto es muy corto,
        o no se detectó ningún síntoma con score suficiente.
    """
    if not texto or len(texto.strip()) < 10:
        return None

    texto_norm = normalizar_texto_paciente(texto)

    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.post(
                f"{GES_ASSISTANT_URL}/triage",
                json={"text": texto_norm, "limit": 5},
            )
            r.raise_for_status()
            data = r.json()
    except Exception:
        return None

    matches = data.get("matches") or []
    if not matches:
        return None

    top = matches[0]
    top_score = float(top.get("score") or 0)
    if top_score < _MIN_SCORE:
        return None

    ges_specialty = data.get("top_specialty")
    needs_urgency = bool(data.get("needs_urgency"))

    # Casos especiales de routing: URGENCIAS y HOSPITAL no son especialidades
    # agendables — el chatbot debe responder de otra forma.
    especialidad_cmc: str | None
    if ges_specialty in ("URGENCIAS", "HOSPITAL"):
        especialidad_cmc = None
    else:
        especialidad_cmc = _SPECIALTY_MAP.get(ges_specialty or "")

    return {
        "especialidad": especialidad_cmc,
        "ges_specialty_raw": ges_specialty,
        "needs_urgency": needs_urgency,
        "top_pathology": top.get("pathology_name"),
        "top_score": top_score,
        "matches": matches,
    }
