"""
Routing autoritativo de ecografías a especialista correcto.

Razón: en CMC, varias "ecografías" no las realiza el ecografista general (David Pardo)
sino el especialista del órgano:
  - Cardiológicas → Cardiólogo (Dr. Millán) vía waitlist mensual
  - Ginecológicas/obstétricas/mamarias → Ginecólogo (Dr. Rejón)
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
    # ── Ginecológicas/obstétricas/mamarias → Dr. Tirso Rejón (ID 61) ──────────
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
            # Mamaria / mamas
            "mamaria",
            "eco mamaria",
            "eco de mamas",
            "eco mamas",
            "ecografia mamaria",
            "ecografia de mamas",
            "ecotomografia mamaria",
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
    "ecografia_general_pardo": {
        "id_profesional": 68,
        "especialidad_destino": "ecografía",
        "flujo": "normal",
        "precio_particular": 40000,
        "keywords": [
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
            # Musculoesquelética
            "musculoesqueletica",
            "musculo esqueletica",
            "eco musculo",
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

# Mensaje que el bot envía cuando el paciente solo dice "ecografía"
MSG_PREGUNTAR_TIPO = (
    "¿De qué tipo es la ecografía? Por ejemplo:\n\n"
    "• Abdominal / renal / tiroides / vejiga → David Pardo, $40.000\n"
    "• Transvaginal / mamaria / pélvica / obstétrica → Ginecología (Dr. Rejón), $35.000\n"
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
    # Palabras cortas con word-boundary para no contaminar "económico", "ecología"
    import re
    _SHORT = {"eco", "orl"}
    for kw in _SOLO_ECO_KEYWORDS:
        if len(kw) <= 4:
            if re.search(r"\b" + re.escape(kw) + r"\b", txt_norm):
                return True
        elif kw in txt_norm:
            return True
    # Verificar keywords específicos de todos los grupos
    for routing in ECOGRAFIA_ROUTING.values():
        for kw in routing["keywords"]:
            if _norm(kw) in txt_norm:
                return True
    return False
