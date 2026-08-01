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
            # Pélvica — también la palabra sola: "Ecotomagrias pélvica femenina"
            # no contiene "eco pelvica" y quedaba sin match (corpus 2026-08-01).
            "pelvica",
            "pelvis",
            "eco pelvica",
            "ecografia pelvica",
            # Ginecológica — raíz sola: en prod los pacientes contestan
            # "Ginecóloga"/"Ginecológia" a la pregunta de tipo (corpus 2026-08-01).
            # Seguro: el gate de contexto eco impide que "hora con ginecología"
            # (sin mención de eco) caiga acá.
            "ginecolog",
            "eco ginecologica",
            "ecografia ginecologica",
            # Ovarios / útero
            "eco de ovarios",
            "ecografia de ovarios",
            "eco de utero",
            "ecografia de utero",
            "eco utero",
            "ecografia utero",
        ],
        "mensaje": (
            "La ecografía {tipo} la realiza el Dr. Tirso Rejón (Ginecólogo) en el CMC, "
            "no el ecografista general.\nValor: $35.000 particular."
        ),
    },

    # ── Ecografía obstétrica (de embarazo) → NO se realiza en el CMC ────────
    # Keywords coloquiales de embarazo con contexto eco (gate _tiene_contexto_eco
    # ya exige raíz ecográfica antes de llegar acá).
    "obstetrica_no_disponible": {
        "id_profesional": None,
        "especialidad_destino": "obstetrica",
        "flujo": "no_disponible",
        "precio_particular": None,
        "keywords": [
            "obstetrica",
            "obstétrica",
            "obstretic",     # typo con letras cambiadas: "obstretica" (corpus 2026-08-01)
            "eco obstetrica",
            "ecografia obstetrica",
            "embarazo",
            "eco embarazo",
            "eco de embarazo",
            "ecografia de embarazo",
            "prenatal",
            "eco prenatal",
            "ecografia prenatal",
            # Coloquial — gate eco ya exige "eco/ecografia/ultrasonido/..." en texto
            "embaraz",
            "embaras",
            "ver el bebe",
            "ver al bebe",
            "ver el bb",
            "la guagua",
            "mi guagua",
            "la wawa",
        ],
        "mensaje": (
            "Por ahora *no realizamos ecografía obstétrica de embarazo* en el "
            "Centro Médico Carampangue.\n\n"
            "Para ecografías de embarazo puedes consultar en la *Clínica de "
            "Maternidad* más cercana o el Hospital de tu red de salud."
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
            # Muslo / pierna / pantorrilla (tejidos blandos miembro inferior)
            "muslo",
            "de muslo",
            "eco muslo",
            "ecografia muslo",
            "ecotomografia muslo",
            "pierna",
            "de pierna",
            "eco pierna",
            "ecografia pierna",
            "pantorrilla",
            "de pantorrilla",
            "eco pantorrilla",
            "gemelo",  # músculo gastrocnemio
            "de gemelo",
            # Zona lumbar / columna (partes blandas paravertebrales)
            # Lumbosacra y variantes reales de órdenes médicas (corpus 2026-08-01):
            # "Lumbrosaca", "lumbo sacr?", "lumbosacra"
            "lumbosacr",
            "lumbo sacr",
            "lumbrosac",
            "lumbar",
            "de lumbar",
            "eco lumbar",
            "ecografia lumbar",
            "ecotomografia lumbar",
            "paravertebral",
            # Omóplato / escápula / hombro posterior
            "omoplato",
            "omóplato",
            "escapula",
            "escápula",
            "eco omoplato",
            "ecografia omoplato",
            # Glúteos (partes blandas)
            "gluteo",
            "gluteos",
            "de gluteo",
            "eco gluteo",
            "ecografia gluteo",
            "ecografia gluteos",
            # Pared abdominal (hernias, lipomas, defectos musculoaponeuróticos)
            "pared abdominal",
            "eco pared abdominal",
            "ecografia pared abdominal",
            # Cervical / cuello (tiroides ya incluida arriba; este cubre partes blandas cervicales)
            "cervical",
            "eco cervical",
            "ecografia cervical",
            "ecotomografia cervical",
            # Articulación genérico
            "articulacion",
            "articulación",
            "eco articulacion",
            "de articulacion",
            # Doppler genérico (no cardíaco)
            "doppler",
            "dopler",           # typo una sola p (corpus 2026-08-01)
            "eco doppler",
            "ecografia doppler",
            # Inguinal
            "inguinal",
            "unguinal",         # typo frecuente: "ecografia unguinal bilateral" (corpus 2026-08-01)
            "eco inguinal",
            "ecografia inguinal",
            # Variantes pegadas / typos frecuentes de pacientes (portavión 2026-06-09)
            "ecomamaria",       # eco+mamaria pegado → mamaria (partes blandas, Pardo)
            "renovesical",      # renal + vesical combinado → Pardo
            "renovecical",      # typo de renovesical
            "reno vesical",
            # NOTA: "ecotomografia", "ecotografia", "ecotomagrafia", "eco grafia",
            # "eco tomografia" son alias raíz SIN órgano — viven en _SOLO_ECO_KEYWORDS
            # para que route_ecografia retorne None y pregunte el tipo, igual que "eco".
        ],
        "mensaje": (
            "La ecografía {tipo} la realiza David Pardo en el CMC. "
            "Valor: $40.000 particular."
        ),
    },
}

# Palabras que indican ecografía sin especificar órgano.
# Al matchear, route_ecografia retorna None → el bot pregunta el tipo al paciente.
# Incluir aquí los alias raíz SIN órgano (ecotomografia, eco grafia, etc.) para que
# no caigan a un grupo específico de Pardo cuando el paciente no especificó el órgano.
_SOLO_ECO_KEYWORDS = frozenset({
    "ecografia",
    "eco",
    "ecotomografia",   # sin tilde — igual que "ecografia" pero con alias ecotomo
    "ecotomografo",
    "ecotomo",
    "ultrasonido",
    "ecografista",
    # Alias raíz con variantes ortográficas / typos frecuentes
    "ecotografia",     # typo (sin -mo-)
    "ecotomagrafia",   # typo doble
    "eco grafia",      # con espacio
    "eco grafias",
    "eco tomografia",  # con espacio
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
    r"ecotom\w*|"                   # ecotomografia, ecotomografo, ecotomo, ecotomagria(s) (typo)
    r"ecotograf\w*|"                # ecotografia (typo sin -mo-)
    r"ecodoppler|"
    r"ecomamaria|"                  # variante pegada
    r"ultrasonido\w*|"
    r"doppler|dopler|"              # dopler = typo una p
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
    "• Transvaginal / pélvica / ginecológica → Ginecología (Dr. Rejón), $35.000\n"
    "• Mamaria → Ecografía (David Pardo), $40.000 — es partes blandas\n"
    "• Ecocardiograma (corazón) → Cardiología (Dr. Millán), $110.000\n\n"
    "Escribe el tipo que necesitas."
)


# ── Capa fuzzy (fallback del match exacto) ──────────────────────────────────
# El matcher principal es substring exacto: cada typo nuevo exigía una línea
# nueva de vocabulario. Esta capa cierra la clase completa de errores de 1-2
# letras ("dopler", "unguinal", "muscoesqueletica") con difflib (stdlib, sin
# dependencia nueva en el VPS). Solo corre si el match exacto no encontró nada.
#
# Guardas anti-falso-positivo:
#   - solo tokens y keywords de ≥5 letras (evita "pie"≈"por", "eco"≈"esa")
#   - solo keywords de UNA palabra (las multi-palabra exigen exactitud)
#   - umbral 0.85 de SequenceMatcher.ratio
#   - respeta la misma prioridad de grupos que el match exacto
from difflib import SequenceMatcher as _SeqMatcher

_FUZZY_MIN_LEN = 5
_FUZZY_THRESHOLD = 0.85
_TOKEN_RE = _re_eco.compile(r"[a-zñ]+")


def _fuzzy_route(txt_norm: str) -> dict | None:
    """Match difuso token-a-keyword. Retorna el routing del primer grupo
    (en orden de prioridad) con algún keyword ≥ umbral, o None."""
    tokens = [t for t in _TOKEN_RE.findall(txt_norm) if len(t) >= _FUZZY_MIN_LEN]
    if not tokens:
        return None
    for key in ("obstetrica_no_disponible", "ginecologia_rejon",
                "cardiologia_millan_waitlist", "ecografia_general_pardo"):
        routing = ECOGRAFIA_ROUTING[key]
        for kw in routing["keywords"]:
            kwn = _norm(kw)
            if " " in kwn or len(kwn) < _FUZZY_MIN_LEN:
                continue
            for tok in tokens:
                if _SeqMatcher(None, tok, kwn).ratio() >= _FUZZY_THRESHOLD:
                    return routing
    return None


def route_ecografia(texto: str, assume_context: bool = False) -> dict | None:
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
    #
    # assume_context=True saltea el gate: lo usan los call sites que YA saben que
    # estamos resolviendo el TIPO de una ecografía (el bot acaba de preguntar
    # "¿de qué tipo es la ecografía?" y el paciente responde "abdominal",
    # "de rodilla", "mamaria", etc.). Sin esto, el gate rompería la selección de
    # tipo de eco legítima. Ver flows.py wait_eco_tipo y _iniciar_agendar.
    if not assume_context and not _tiene_contexto_eco(txt_norm):
        return None

    # Prioridad fija: obstétrica no-disponible primero (para no caer en ginecología),
    # luego ginecología, luego cardiología, luego general.
    for key in ("obstetrica_no_disponible", "ginecologia_rejon",
                "cardiologia_millan_waitlist", "ecografia_general_pardo"):
        routing = ECOGRAFIA_ROUTING[key]
        for kw in routing["keywords"]:
            if _norm(kw) in txt_norm:
                return routing

    # Fallback difuso: typos de 1-2 letras que el substring exacto no ve
    # ("dopler" ya es exacto, pero "avdominal", "tiroydes", "muscoesqueletica"
    # caen acá). Ver _fuzzy_route y sus guardas anti-falso-positivo.
    fuzzy = _fuzzy_route(txt_norm)
    if fuzzy is not None:
        return fuzzy

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


# ─────────────────────────────────────────────────────────────────────────────
#  BASE DE CONOCIMIENTO DE ECOGRAFÍAS
#  Para que el bot explique al paciente qué es / para qué sirve / cómo prepararse
#  cuando pregunta por un tipo concreto. Info general del estudio (NO diagnóstico).
#  Cada entrada: keywords, qué evalúa, para qué sirve, preparación, profesional/precio.
# ─────────────────────────────────────────────────────────────────────────────

# Bloques de profesional/precio reutilizables.
_PARDO = ("David Pardo (Tecnólogo Médico · Ecografía)", 40000)
_REJON = ("Dr. Tirso Rejón (Ginecología)", 35000)

ECO_INFO: dict[str, dict] = {
    "abdominal": {
        "nombre": "Ecografía abdominal",
        "keywords": ["abdominal", "abdomen", "eco abdominal", "abdomen completo"],
        "evalua": "hígado, vesícula y vías biliares, páncreas, bazo, riñones y la aorta abdominal",
        "sirve": "estudiar dolor abdominal, cálculos en la vesícula, hígado graso, quistes o masas",
        "preparacion": "Ayuno de 6 a 8 horas (no comer antes). Puedes tomar agua y tus medicamentos.",
        "prof": _PARDO,
    },
    "renal": {
        "nombre": "Ecografía renal / urinaria",
        "keywords": ["renal", "rinon", "riñon", "urinaria", "vias urinarias", "eco renal"],
        "evalua": "los riñones y la vejiga",
        "sirve": "buscar cálculos (piedras), quistes, infecciones a repetición o alteraciones del flujo de orina",
        "preparacion": "Vejiga llena: toma 4-6 vasos de agua 1 hora antes y no orines hasta el examen.",
        "prof": _PARDO,
    },
    "vesical": {
        "nombre": "Ecografía vesical (vejiga)",
        "keywords": ["vesical", "vejiga", "eco vesical"],
        "evalua": "la vejiga (paredes, contenido y vaciado)",
        "sirve": "estudiar molestias al orinar, retención o infecciones urinarias",
        "preparacion": "Vejiga llena: toma harta agua 1 hora antes y no orines hasta el examen.",
        "prof": _PARDO,
    },
    "hepatica": {
        "nombre": "Ecografía hepática / de vesícula",
        "keywords": ["hepatica", "higado", "vesicula", "vesicula biliar", "eco higado"],
        "evalua": "el hígado y la vesícula biliar",
        "sirve": "detectar cálculos en la vesícula, hígado graso o alteraciones del hígado",
        "preparacion": "Ayuno de 6 a 8 horas.",
        "prof": _PARDO,
    },
    "tiroidea": {
        "nombre": "Ecografía de tiroides",
        "keywords": ["tiroides", "tiroidea", "eco tiroides", "eco tiroidea"],
        "evalua": "la glándula tiroides en el cuello",
        "sirve": "estudiar nódulos, bocio o alteraciones detectadas en exámenes de tiroides",
        "preparacion": "No requiere preparación.",
        "prof": _PARDO,
    },
    "mamaria": {
        "nombre": "Ecografía mamaria",
        "keywords": ["mamaria", "mamas", "mama", "eco de mamas", "ecotomografia mamaria"],
        "evalua": "las mamas (es un estudio de partes blandas, no ginecológico)",
        "sirve": "estudiar nódulos o quistes palpables y complementar la mamografía, sobre todo en mamas densas o en mujeres jóvenes",
        "preparacion": "No requiere preparación. Idealmente la primera semana después de la regla.",
        "prof": _PARDO,
    },
    "partes_blandas": {
        "nombre": "Ecografía de partes blandas",
        "keywords": ["partes blandas", "superficial", "eco superficial", "bulto", "lipoma", "ganglio", "ganglios"],
        "evalua": "estructuras bajo la piel: bultos, ganglios, lipomas, quistes",
        "sirve": "estudiar un bulto o aumento de volumen que se palpa",
        "preparacion": "No requiere preparación.",
        "prof": _PARDO,
    },
    "testicular": {
        "nombre": "Ecografía testicular / escrotal",
        "keywords": ["testicular", "testiculo", "escrotal", "escroto", "eco testicular"],
        "evalua": "los testículos y el escroto",
        "sirve": "estudiar dolor, aumento de volumen, masas o varicocele",
        "preparacion": "No requiere preparación.",
        "prof": _PARDO,
    },
    "prostatica": {
        "nombre": "Ecografía prostática (vía abdominal)",
        "keywords": ["prostata", "prostatica", "eco prostata"],
        "evalua": "la próstata y la vejiga por vía abdominal",
        "sirve": "estimar el tamaño de la próstata y estudiar síntomas urinarios",
        "preparacion": "Vejiga llena: toma harta agua 1 hora antes y no orines.",
        "prof": _PARDO,
    },
    "doppler": {
        "nombre": "Ecografía Doppler",
        "keywords": ["doppler", "eco doppler", "flujo", "venas", "varices", "arterial"],
        "evalua": "el flujo de sangre en vasos y órganos",
        "sirve": "estudiar várices, trombosis, circulación de las piernas u órganos",
        "preparacion": "Depende de la zona; en general no requiere preparación especial. Te confirmamos al agendar.",
        "prof": _PARDO,
    },
    "transvaginal": {
        "nombre": "Ecografía transvaginal (ginecológica)",
        "keywords": ["transvaginal", "transvajinal", "intravaginal", "endovaginal", "vaginal", "ginecologica", "ovarios", "utero", "endometrio"],
        "evalua": "el útero, los ovarios y el endometrio, mediante una sonda vaginal (da más detalle que la vía abdominal)",
        "sirve": "estudiar quistes de ovario, miomas, dolor pélvico, sangrados o control ginecológico",
        "preparacion": "Vejiga vacía (orina antes del examen). La realiza el ginecólogo.",
        "prof": _REJON,
    },
    "pelvica": {
        "nombre": "Ecografía pélvica / ginecológica (vía abdominal)",
        "keywords": ["pelvica", "pelvis", "ginecologica abdominal"],
        "evalua": "el útero y los ovarios por vía abdominal",
        "sirve": "estudio ginecológico general cuando no se hace por vía transvaginal",
        "preparacion": "Vejiga llena: toma harta agua 1 hora antes. La realiza el ginecólogo.",
        "prof": _REJON,
    },
}

# Tipos que el CMC NO realiza (responder claro para no generar falsas expectativas).
ECO_NO_DISPONIBLE = {
    "obstetrica": ("Ecografía obstétrica (de embarazo)",
                   "Por ahora *no realizamos ecografía obstétrica de embarazo* en el Centro Médico Carampangue."),
    "ecocardiograma": ("Ecocardiograma (del corazón)",
                       "El ecocardiograma lo realiza el *Dr. Miguel Millán* (Cardiología), $110.000 particular, por lista de espera."),
}

# keyword normalizada → clave de ECO_INFO (orden importa: lo más específico primero).
_ECO_INFO_INDEX: list[tuple[str, str]] = []
for _k, _v in ECO_INFO.items():
    for _kw in _v["keywords"]:
        _ECO_INFO_INDEX.append((_norm(_kw), _k))
# Más largos primero → "transvaginal" antes que "vaginal", etc.
_ECO_INFO_INDEX.sort(key=lambda p: -len(p[0]))

_ECO_NO_DISP_KW = {
    "obstetrica": ["obstetrica", "embarazo", "embarazada", "feto", "prenatal", "gestacion"],
    "ecocardiograma": ["ecocardiograma", "corazon", "cardiaca", "cardiaco", "eco al corazon", "eco del corazon"],
}


def detectar_tipo_eco(texto: str) -> str | None:
    """Devuelve la clave de ECO_INFO del tipo de eco mencionado, o None."""
    t = _norm(texto)
    for kw, clave in _ECO_INFO_INDEX:
        if kw in t:
            return clave
    return None


def info_ecografia(texto: str) -> str | None:
    """Respuesta explicativa para un tipo de eco preguntado por el paciente.
    Devuelve el texto formateado (qué evalúa, para qué, preparación, precio) o None
    si no se reconoce un tipo concreto. Solo info del estudio, NO diagnóstico."""
    t = _norm(texto)
    # Primero: tipos NO disponibles (responder claro).
    for clave, kws in _ECO_NO_DISP_KW.items():
        if any(k in t for k in kws):
            nombre, msg = ECO_NO_DISPONIBLE[clave]
            return f"*{nombre}*\n\n{msg}"
    clave = detectar_tipo_eco(texto)
    if not clave:
        return None
    e = ECO_INFO[clave]
    prof_nom, precio = e["prof"]
    precio_fmt = f"${precio:,}".replace(",", ".")   # formato chileno: $40.000
    return (
        f"🩺 *{e['nombre']}*\n\n"
        f"*¿Qué evalúa?* {e['evalua'][0].upper()}{e['evalua'][1:]}.\n"
        f"*¿Para qué sirve?* Sirve para {e['sirve']}.\n"
        f"*Preparación:* {e['preparacion']}\n"
        f"*La realiza:* {prof_nom} · {precio_fmt}\n\n"
        "_Es un estudio de imágenes; el resultado lo interpreta tu médico._"
    )


def prep_ayuno_eco() -> str:
    """Preparación de ecografía por tipo (para responder 'cuántas horas de ayuno' /
    'cómo me preparo' cuando el paciente tiene una eco agendada). Conservador."""
    return (
        "🩺 *Preparación para tu ecografía*\n\n"
        "Depende del tipo:\n"
        "• *Abdominal / hígado / vesícula:* ayuno de 6 a 8 horas (no comer antes; "
        "puedes tomar agua y tus remedios).\n"
        "• *Renal / vejiga / próstata:* vejiga llena — toma harta agua 1 hora antes y "
        "no orines hasta el examen.\n"
        "• *Tiroides / mama / partes blandas / testicular:* no requiere preparación.\n\n"
        "¿De qué tipo es la tuya? Así te confirmo exacto 😊"
    )
