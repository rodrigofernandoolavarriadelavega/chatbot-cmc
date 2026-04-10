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

import httpx

from config import GES_ASSISTANT_URL

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

    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.post(
                f"{GES_ASSISTANT_URL}/triage",
                json={"text": texto, "limit": 5},
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
