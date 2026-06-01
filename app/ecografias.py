"""
Routing autoritativo de ecografías a especialista correcto.

Razón: en CMC, varias "ecografías" no las realiza el ecografista general (David Pardo)
sino el especialista del órgano:
  - Cardiológicas → Cardiólogo (Dr. Millán) vía waitlist mensual
  - Ginecológicas/obstétricas → Ginecólogo (Dr. Rejón)
  - Mamarias → Ecografista (David Pardo) — es partes blandas, no ginecológica
  - Resto (abdominal, renal, tiroides, etc) → Ecografista (Pardo)

Cualquier ecografía nueva agregar AQUÍ, no en alias dispersos.
Verificado con dueño CMC el 2026-05-15.

Uso:
    from ecografias import route_ecografia, ECOGRAFIA_ROUTING
    routing = route_ecografia("quiero una eco transvaginal")
    if routing is None:
        # preguntar tipo al paciente
"""
from __future__ import annotations

import unicodedata


def _norm(texto: str) -> str:
    """Minúscula sin tildes (preserva ñ)."""
    t = texto.lower()
    out = []
    for c in t:
        if c == "ñ":
            out.append(c)
        else:
            nfd = unicodedata.normalize("NFD", c)
            out.append(nfd[0])
    return "".join(out)


# Mapeo único y autoritativo de ecografías a especialista correcto.
# Cada lista es exhaustiva — incluye variantes con/sin tildes, plural, abreviaciones.
# Las claves se normalizan en route_ecografia(), no es necesario duplicar con/sin tilde aquí.
ECOGRAFIA_ROUTING: dict[str, dict] = {
    # ── Ginecológicas/obstétricas → Dr. Tirso Rejón (ID 61) ──────────────────
    # Mamaria NO va acá — es partes blandas (Pardo).
    "ginecologia_rejon": {
        "id_profesional": 61,
        "especialidad_destino": "ginecología",
        "flujo": "normal",
        "precio_particular": 35000,
        "keywords": [
            # Transvaginal y variantes (organo más frecuente)
            "transvaginal",
            "transvajinal",   # typo fonético frecuente
            "intravaginal",
            "intravajinal",
            "eco transvaginal",
            "eco transvajinal",
            "eco intravaginal",
            "ecografia transvaginal",
            "ecografia intravaginal",
            "ecografia transvajinal",
            "ecografia intravajinal",
            # Endovaginal
            "endovaginal",
            "eco endovaginal",
            "ecografia endovaginal",
            # Vaginal genérico
            "eco vaginal",
            "ecografia vaginal",
            # Pélvica
            "eco pelvica",
            "ecografia pelvica",
            # Ginecológica
            "eco ginecologica",
            "ecografia ginecologica",
            # Ovarios / útero
            "eco de ovarios",
            "ecografia de ovarios",
            "eco de utero",
            "ecografia de utero",
            "eco utero",
            "ecografia utero",
            # Obstétrica / embarazo / prenatal
            "obstetrica",
            "eco obstetrica",
            "ecografia obstetrica",
            "embarazo",
            "eco embarazo",
            "eco de embarazo",
            "ecografia de embarazo",
            "prenatal",
            "eco prenatal",
            "ecografia prenatal",
            # Embarazo coloquial — solo enrutan porque el GATE ya exigió contexto
            # ecográfico (_tiene_contexto_eco), así "eco pa ver el bebe" → Rejón
            # pero "control de embarazo" (sin eco) sigue yendo a matrona.
            "embaraz",        # embarazo, embarazada, embarasada(_norm sin tilde no aplica aquí)
            "embaras",        # variante fonética "embarasada"
            "ver el bebe",
            "ver al bebe",
            "ver el bb",
            "la guagua",
            "mi guagua",
            "la wawa",
        ],
        "mensaje": (
            "La ecografía {tipo} la realiza el Dr. Tirso Rejón (Ginecólogo) en el CMC, "
            "no el ecografista general.\nValor: $35.000 particular."
        ),
    },

    # ── Ecocardiograma → Dr. Miguel Millán (ID 60), vía waitlist ────────────
    "cardiologia_millan_waitlist": {
        "id_profesional": 60,
        "especialidad_destino": "ecocardiograma",
        "flujo": "waitlist",
        "precio_particular": 110000,
        "keywords": [
            "ecocardiograma",
            "eco cardiograma",
            "eco-cardiograma",
            "eco cardio",
            "eco cardiaca",
            "eco cardíaca",
            "ecografia cardiaca",
            "ecografia cardíaca",
            "ecografia del corazon",
            "eco del corazon",
            "eco corazon",
            "eco al corazon",
            "doppler cardiaco",
            "doppler cardíaco",
            "ultrasonido del corazon",
            "ultrasonido corazon",
            "examen ecocardiograma",
        ],
        "mensaje": (
            "El ecocardiograma lo realiza el Dr. Miguel Millán (Cardiólogo). "
            "$110.000 particular.\n"
            "Se realiza una vez al mes, sin fecha confirmada — "
            "¿te anoto en lista de espera?"
        ),
    },

    # ── Ecografía general → David Pardo (ID 68) ──────────────────────────────
    # Incluye mamaria (partes blandas, NO ginecológica).
    "ecografia_general_pardo": {
        "id_profesional": 68,
        "especialidad_destino": "ecografía",
        "flujo": "normal",
        "precio_particular": 40000,
        "keywords": [
            # Mamaria / mamas (partes blandas, NO ginecológica)
            "mamaria",
            "eco mamaria",
            "eco de mamas",
            "eco mamas",
            "ecografia mamaria",
            "ecografia de mamas",
            "ecotomografia mamaria",
            "eco de mama",
            "ecografia de mama",
            # Abdominal
            "abdominal",
            "eco abdominal",
            "ecografia abdominal",
            "abdomen",
            "abdomen completo",
            # Renal
            "renal",
            "eco renal",
            "ecografia renal",
            # Vesical / vejiga
            "vesical",
            "vejiga",
            "eco vesical",
            "ecografia vesical",
            # Hepática / hígado / vesícula
            "hepatica",
            "ecografia hepatica",
            "higado",
            "eco higado",
            "ecografia higado",
            "vesicula",
            "eco vesicula",
            "ecografia vesicula",
            # Tiroides
            "tiroides",
            "tiroidea",
            "eco tiroides",
            "ecografia tiroides",
            "ecografia tiroidea",
            # Partes blandas / superficial
            "partes blandas",
            "eco partes blandas",
            "ecografia partes blandas",
            "superficial",
            "eco superficial",
            # Testicular
            "testicular",
            "testicul",
            "texticul",
            "eco testicular",
            "ecografia testicular",
            "inguinal escrotal",
            "inguino escrotal",
            # Cuello
            "eco cuello",
            "ecografia de cuello",
            # Próstata
            "prostata",
            "eco prostata",
            "ecografia prostata",
            # Musculoesquelética y articulaciones específicas
            "musculoesqueletica",
            "musculo esqueletica",
            "eco musculo",
            "musculoesqueletico",
            "musculo esqueletico",
            # Articulaciones miembro superior
            "hombro",
            "de hombro",
            "eco hombro",
            "ecografia hombro",
            "brazo",
            "de brazo",
            "eco brazo",
            "codo",
            "de codo",
            "eco codo",
            "muneca",
            "muñeca",
            "de muneca",
            "mano",
            "de mano",
            "eco mano",
            "dedo",
            "de dedo",
            # Articulaciones miembro inferior
            "cadera",
            "de cadera",
            "eco cadera",
            "rodilla",
            "de rodilla",
            "eco rodilla",
            "tobillo",
            "de tobillo",
            "eco tobillo",
            "pie",
            "de pie",
            "eco pie",
            # Articulación genérico
            "articulacion",
            "articulación",
            "eco articulacion",
            "de articulacion",
            # Doppler genérico (no cardíaco)
            "doppler",
            "eco doppler",
            "ecografia doppler",
            # Inguinal
            "inguinal",
            "eco inguinal",
            "ecografia inguinal",
        ],
        "mensaje": (
            "La ecografía {tipo} la realiza David Pardo en el CMC. "
            "Valor: $40.000 particular."
        ),
    },
}

# Palabras que indican ecografía sin especificar órgano
_SOLO_ECO_KEYWORDS = frozenset({
    "ecografia",
    "eco",
    "ecotomografia",
    "ecotomografo",
    "ecotomo",
    "ultrasonido",
    "ecografista",
})

# ── GATE de contexto ecográfico ──────────────────────────────────────────────
# Los keywords de ECOGRAFIA_ROUTING incluyen partes del cuerpo desnudas
# ("rodilla", "pie", "mano", "hombro", "tiroides", "prostata", "embarazo", ...)
# que sirven para elegir EL TIPO una vez que ya sabemos que es una ecografía.
# Usados como gatillo por sí solos, secuestran cualquier mensaje que nombre una
# parte del cuerpo ("me duele la rodilla" → ecografía). Bug sistémico.
#
# Este gate exige que el texto contenga una raíz ecográfica REAL antes de dejar
# que las partes del cuerpo enruten. Evita falsos positivos como "kine pa mi
# rodilla", "podóloga, me duelen los pies", "limpieza de dientes".
#
# Nota sobre "eco": solo la palabra suelta cuenta (\beco\b), para no matchear
# "economico"/"ecologia". Las formas "eco abdominal", "eco de rodilla",
# "eco al corazon" escriben "eco" como token separado, así que entran igual.
import re as _re_eco
_ECO_CONTEXT_RE = _re_eco.compile(
    r"\b(?:"
    r"eco|"                         # token suelto: "eco abdominal", "eco de rodilla"
    r"ecograf\w*|"                  # ecografia, ecografias, ecografista, ecografico
    r"ecocardio\w*|"                # ecocardiograma, ecocardiografia
    r"ecotomograf\w*|ecotomo\w*|"   # ecotomografia, ecotomografo, ecotomo
    r"ecodoppler|"
    r"ultrasonido\w*|"
    r"doppler|"
    r"transvaginal|transvajinal|intravaginal|intravajinal|endovaginal"
    r")\b"
)


def _tiene_contexto_eco(txt_norm: str) -> bool:
    """True si el texto contiene una raíz ecográfica real (no solo una parte
    del cuerpo). Es la precondición para que route_ecografia enrute por órgano."""
    return bool(_ECO_CONTEXT_RE.search(txt_norm))


# Mensaje que el bot envía cuando el paciente solo dice "ecografía"
MSG_PREGUNTAR_TIPO = (
    "¿De qué tipo es la ecografía? Por ejemplo:\n\n"
    "• Abdominal / renal / tiroides / vejiga / articulación / hombro → David Pardo, $40.000\n"
    "• Transvaginal / pélvica / obstétrica → Ginecología (Dr. Rejón), $35.000\n"
    "• Mamaria → Ecografía (David Pardo), $40.000 — es partes blandas\n"
    "• Ecocardiograma (corazón) → Cardiología (Dr. Millán), $110.000\n\n"
    "Escribe el tipo que necesitas."
)


def route_ecografia(texto: str) -> dict | None:
    """Dado texto del paciente que menciona ecografía, retorna el routing correcto.

    Retorna uno de los dicts en ECOGRAFIA_ROUTING si el tipo de ecografía es
    reconocible. Retorna None si el paciente solo dijo "ecografía" / "eco" sin
    especificar órgano — el caller debe responder con MSG_PREGUNTAR_TIPO.

    Prioridad de match: ginecología > cardiología > ecografía general.
    (más específico primero para evitar que "eco abdominal" matchee "ecograf" genérico)

    Los keywords se normalizan (sin tildes, minúscula) antes de comparar.
    """
    if not texto:
        return None

    txt_norm = _norm(texto.strip())

    # GATE: sin contexto ecográfico real, las partes del cuerpo NO enrutan.
    # "me duele la rodilla" / "podóloga, los pies" / "limpieza de dientes" → None.
    if not _tiene_contexto_eco(txt_norm):
        return None

    # Prioridad fija: ginecología primero, luego cardiología, luego general
    for key in ("ginecologia_rejon", "cardiologia_millan_waitlist", "ecografia_general_pardo"):
        routing = ECOGRAFIA_ROUTING[key]
        for kw in routing["keywords"]:
            if _norm(kw) in txt_norm:
                return routing

    # El texto menciona "ecografía" pero sin órgano especificado → preguntar
    for kw in _SOLO_ECO_KEYWORDS:
        if kw in txt_norm:
            return None  # caller usa MSG_PREGUNTAR_TIPO

    # No menciona ecografía en absoluto → no es nuestro problema
    return None


def texto_menciona_ecografia(texto: str) -> bool:
    """True si el texto menciona algún tipo de ecografía (incluido 'eco' solo).

    Útil para saber si hay que llamar a route_ecografia antes del flujo normal.
    """
    if not texto:
        return False
    txt_norm = _norm(texto.strip())
    # Solo cuenta como "menciona ecografía" si hay una raíz ecográfica real.
    # Las partes del cuerpo desnudas (rodilla, pie, tiroides, embarazo...) NO
    # bastan: son qualifiers de tipo, no gatillos. Ver _ECO_CONTEXT_RE.
    return _tiene_contexto_eco(txt_norm)
