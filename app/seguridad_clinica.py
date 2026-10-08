"""Detectores léxicos de seguridad clínica (auditoría médica 2026-10-08).

Funciones puras, sin I/O, sin dependencias del bot. Todo el texto se normaliza
(minúscula, sin tildes) antes de comparar, así que los patrones se escriben sin
tildes. flows.py las consume en tres puntos:

  - emergencia_ampliada(): suma al detector de EMERGENCIAS (SAMU 131).
  - crisis_salud_mental_ampliada(): suma a SALUD_MENTAL_CRISIS (Salud Responde).
  - senal_critica_triage(): guarda del triage GES — jamás agendar con esto.

Criterio de diseño: cada patrón nuevo exige contexto (verbo de intención,
parte del cuerpo, cantidad) para no secuestrar a un paciente que solo quería
hora. "control de presión", "control de embarazo", "me falta una hora",
"cortarme el pelo" NO deben disparar nada.
"""
from __future__ import annotations

import re
import unicodedata


def _norm(texto: str) -> str:
    """Minúscula, sin tildes, ñ -> n (solo para comparar), espacios colapsados."""
    if not texto:
        return ""
    t = texto.lower().replace("ñ", "n")
    t = unicodedata.normalize("NFKD", t)
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", t).strip()


def norm_clave(texto: str) -> str:
    """Clave de especialidad normalizada para lookups (cardiología == cardiologia)."""
    return _norm(texto)


def lookup_norm(d: dict, clave: str, default=None):
    """d.get(clave) tolerante a tildes y mayúsculas en claves y consulta."""
    nk = _norm(clave)
    for k, v in d.items():
        if _norm(k) == nk:
            return v
    return default


# ── Emergencias físicas ──────────────────────────────────────────────────────

# Verbo reflexivo/pasado: "se tomó", "me tomé", "tomaron". Sin "tomo" suelto
# (presente: "tomo pastillas para la presión" es tratamiento, no intoxicación).
_VERBO_INGESTA = r"(?:(?:se|me|te)\s+(?:tomo|tome|tomaron|ingirio|ingeri|trago|trague|bebio|bebi)|tomaron|ingirio|ingeri|trago|trague)"
_NO_HABITUAL = r"(?!.{0,25}\b(?:al\s+(?:mes|dia|ano)|cada|por\s+(?:mes|dia|semana)|recetad\w*|indicad\w*))"
_CANTIDAD = r"(?:frasco|caja|envase|punado|monton|muchas|blister|tira)"
_DROGA_FARMACO = r"(?:pastillas|pastis|remedios|medicamentos|comprimidos|calmantes|somniferos|paracetamol|clonazepam)"
_TOXICO = r"(?:cloro|veneno|raticida|soda\s+caustica|bencina|parafina|desinfectante|detergente|insecticida)"

_PATRONES_EMERGENCIA = [
    # Pecho: presión / aprieta / opresión
    re.compile(r"\b(?:presion|opresion|peso|dolor\s+de\s+presion)\s+(?:en|sobre|del)\s+(?:el\s+)?pecho\b"),
    re.compile(r"\b(?:me\s+|le\s+|se\s+le\s+)?(?:aprieta|apreta|oprime|aplasta)\w*\s+(?:el\s+|mi\s+)?pecho\b"),
    re.compile(r"\bpecho\s+(?:apretado|oprimido|apretando|aplastado)\b"),
    # Respiración
    re.compile(r"\b(?:me\s+|le\s+)?falta(?:n)?\s+(?:el\s+)?aire\b(?!\s+acondicionado)"),
    re.compile(r"\bno\s+(?:puedo|puede|logro|logra|alcanzo|alcanza)\s+(?:a\s+)?respirar\b"),
    re.compile(r"\bno\s+respira\b"),
    # Sangre en vómito
    re.compile(r"\bvomit\w*\s+(?:con\s+|de\s+)?sangre\b"),
    re.compile(r"\bvomit\w*\s+(?:algo\s+de\s+|harta\s+|mucha\s+)?sangre\b"),
    # ACV
    re.compile(r"\bcara\s+(?:torcida|chueca|desviada|caida)\b"),
    re.compile(r"\bse\s+(?:le\s+|me\s+)?(?:torcio|corrio|desvio)\s+(?:la\s+)?(?:cara|boca)\b"),
    re.compile(
        r"\bno\s+(?:puede|puedo|logra|logro)\s+mover\s+"
        r"(?:el\s+|la\s+|un\s+|una\s+|mi\s+|su\s+)?(?:lado|brazo|pierna|mitad)\b"
    ),
    re.compile(r"\b(?:lado|mitad)\s+(?:derecho|izquierdo|del\s+cuerpo)\s+(?:dormido|paralizado|sin\s+fuerza|no\s+responde)\b"),
    # Alergia / vía aérea
    re.compile(r"\b(?:se\s+)?(?:me\s+|le\s+)?(?:hincha|hincho|hinchan|inflama|inflamo|cierra|cerro)\w*\s+(?:la\s+)?(?:lengua|garganta)\b"),
    re.compile(r"\b(?:lengua|garganta)\s+(?:hinchada|inflamada|cerrada)\b"),
    # Intoxicación
    re.compile(_VERBO_INGESTA + r"\s+.{0,20}?" + _TOXICO + r"\b"),
    re.compile(_VERBO_INGESTA + r"\s+(?:un\s+|una\s+|el\s+|la\s+)?" + _CANTIDAD + r"\s*(?:de\s+)?(?:las\s+|los\s+)?.{0,12}?" + _DROGA_FARMACO + r"\b" + _NO_HABITUAL),
    re.compile(r"\bsobredosis\b"),
    # Testículo agudo
    re.compile(r"\bdolor\s+(?:subito|repentino|intenso|fuerte)\s+(?:en\s+|de\s+|del\s+|en\s+el\s+)?(?:un\s+|el\s+)?(?:testiculo|escroto|testiculos)\b"),
    re.compile(r"\b(?:testiculo|escroto)\b.{0,30}\b(?:subito|repentino|de\s+repente|de\s+golpe)\b"),
    re.compile(r"\bduele\b.{0,20}\b(?:testiculo|escroto)\b.{0,25}\b(?:subito|repentino|de\s+repente|de\s+golpe|mucho|fuerte)\b"),
    re.compile(r"\b(?:subito|repentino|de\s+repente|de\s+golpe)\b.{0,25}\b(?:testiculo|escroto)\b"),
]

_RE_EMBARAZO = re.compile(r"\b(?:embarazad\w*|gestante|semanas\s+de\s+(?:embarazo|gestacion)|(?:mi|su|en|con)\s+embarazo|\d+\s+semanas\s+(?:y\s+)?(?:no|sangr\w*))\b")
# "sangre" suelto NO cuenta ("examen de sangre", "grupo sanguíneo"): exige verbo
# de sangrar o sangre con contexto de pérdida.
_RE_SANGRADO = re.compile(
    r"\b(?:sangrad\w*|sangrand\w*|sangra\w*|hemorrag\w*|perdiendo\s+sangre|perdida\s+de\s+sangre|"
    r"con\s+sangre|manchas?\s+de\s+sangre|sangre\s+(?:roja|abundante|fresca))\b"
)
_RE_SANGRADO_BENIGNO = re.compile(r"\b(?:encia|encias|nariz|narizazo)\b")
_RE_EMBARAZO_DOLOR_GRAVE = re.compile(
    r"\b(?:dolor\s+(?:muy\s+)?fuerte|dolor\s+(?:abdominal|de\s+barriga|de\s+guata|de\s+vientre|pelvico)|"
    r"contracciones|perdida\s+de\s+liquido|rompi\s+(?:la\s+)?bolsa|se\s+me\s+rompio\s+(?:la\s+)?bolsa|"
    r"liquido\s+amniotico|desmay\w*|convuls\w*)\b"
)
_RE_BEBE_PALABRA = re.compile(r"\b(?:guagua|bebe|feto|criatura|hijo\s+en\s+(?:la\s+)?(?:guata|panza|vientre))\b")
_RE_NO_SE_MUEVE = re.compile(r"\bno\s+se\s+(?:mueve|movio|ha\s+movido)\b")
_RE_NO_SIENTO_MOVER = re.compile(
    r"\bno\s+(?:siento|noto|he\s+sentido|percibo|sentimos)\b.{0,30}\b(?:mover\w*|movimiento\w*|patad\w*|pataleo)\b"
)
_RE_MOVIMIENTO_AUSENTE_2 = re.compile(r"\b(?:sin|dejo\s+de\s+sentir|deje\s+de\s+sentir)\s+(?:los\s+)?(?:movimientos?|patadas)\b")

_RE_FIEBRE = re.compile(r"\b(?:fiebre|febril|calentura|temperatura\s+(?:alta|de\s+3[89])|3[89](?:[.,]\d)?\s*(?:grados|°|c\b))")
_RE_RECIEN_NACIDO = re.compile(r"\b(?:recien\s+nacid\w*|neonato)\b")
_NUM_PALABRA = {"un": 1, "una": 1, "uno": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5,
                "seis": 6, "siete": 7, "ocho": 8, "nueve": 9, "diez": 10, "once": 11, "doce": 12}
_NUM_TOK = r"(\d{1,3}|un|una|uno|dos|tres|cuatro|cinco|seis|siete|ocho|nueve|diez|once|doce)"
_SUJETO_BEBE = r"(?:bebe|guagua|lactante|nene|nena|hij[oa]|ninos?|ninas?|chiquit[oa]|criatura|pequen[oa])\s+(?:recien\s+nacid\w*\s+)?"
# "bebé DE 3 semanas" / "cumple 2 meses": edad inequívoca.
_RE_EDAD_BEBE = re.compile(r"\b" + _SUJETO_BEBE + r"(?:de|cumple)\s+" + _NUM_TOK + r"\s*(dias?|semanas?|meses|mes)\b")
# "tiene 3 semanas" / "con 3 semanas": ambiguo con "con 3 semanas de fiebre", se excluye eso.
_RE_EDAD_BEBE_TIENE = re.compile(
    r"\b" + _SUJETO_BEBE + r"(?:tiene|con)\s+" + _NUM_TOK + r"\s*(dias?|semanas?|meses|mes)\b"
    r"(?!\s+(?:de|con|que)\s+(?:fiebre|tos|diarrea|vomito|dolor|sintomas|evolucion|resfrio))"
)
_RE_EDAD_DE_VIDA = re.compile(
    r"\b(\d{1,3}|un|una|uno|dos|tres|cuatro|cinco|seis|siete|ocho|nueve|diez|once|doce)\s*"
    r"(dias?|semanas?|meses|mes)\s+de\s+(?:vida|nacid[oa]|edad)\b"
)
_RE_MENOR_3_MESES = re.compile(r"\bmenor\s+de\s+(?:3|tres)\s+meses\b")


def _num(tok: str) -> int | None:
    if tok.isdigit():
        return int(tok)
    return _NUM_PALABRA.get(tok)


def _dias_edad(m: re.Match) -> int | None:
    n = _num(m.group(1))
    if n is None:
        return None
    unidad = m.group(2)
    if unidad.startswith("dia"):
        return n
    if unidad.startswith("semana"):
        return n * 7
    return n * 30


def lactante_menor_3_meses_con_fiebre(t: str) -> bool:
    """Fiebre en recién nacido o lactante menor de 3 meses (t ya normalizado)."""
    if not _RE_FIEBRE.search(t):
        return False
    if _RE_RECIEN_NACIDO.search(t) or _RE_MENOR_3_MESES.search(t):
        return True
    for rx in (_RE_EDAD_BEBE, _RE_EDAD_BEBE_TIENE, _RE_EDAD_DE_VIDA):
        for m in rx.finditer(t):
            dias = _dias_edad(m)
            if dias is not None and dias < 90:
                return True
    return False


def embarazo_con_signo_de_alarma(t: str, incluir_dolor: bool = False) -> bool:
    """Embarazo + sangrado / sin movimientos fetales (+ dolor grave opcional)."""
    if _RE_BEBE_PALABRA.search(t) and (_RE_NO_SE_MUEVE.search(t) or _RE_NO_SIENTO_MOVER.search(t)
                                       or _RE_MOVIMIENTO_AUSENTE_2.search(t)):
        # Guagua/bebé que "no se mueve": en embarazo es ausencia de movimiento
        # fetal; en un lactante es falta de reacción. Ambos son urgencia.
        return True
    if not _RE_EMBARAZO.search(t):
        return False
    if _RE_SANGRADO.search(t) and not _RE_SANGRADO_BENIGNO.search(t):
        return True
    if _RE_MOVIMIENTO_AUSENTE_2.search(t):
        return True
    if incluir_dolor and _RE_EMBARAZO_DOLOR_GRAVE.search(t):
        return True
    return False


def emergencia_ampliada(texto: str) -> bool:
    """True si el texto trae una emergencia física que el detector viejo no cubría."""
    t = _norm(texto)
    if not t:
        return False
    if any(p.search(t) for p in _PATRONES_EMERGENCIA):
        return True
    if embarazo_con_signo_de_alarma(t):
        return True
    if lactante_menor_3_meses_con_fiebre(t):
        return True
    return False


# ── Crisis de salud mental ───────────────────────────────────────────────────

_NO_PELO = r"(?!\s+(?:el\s+|la\s+|las\s+|los\s+|mi\s+|mis\s+|un\s+|una\s+)?(?:pelo|cabello|flequillo|fleco|barba|bigote|puntas|melena|unas))"
_PATRONES_CRISIS = [
    re.compile(r"\bquitar(?:me|se|le)\s+la\s+vida\b"),
    re.compile(r"\bme\s+(?:voy\s+a\s+)?quitar\s+la\s+vida\b"),
    re.compile(r"\bterminar\s+con\s+(?:mi\s+vida|mi\s+existencia|todo\s+esto)\b"),
    re.compile(r"\bmejor\s+no\s+despertar\w*\b"),
    re.compile(r"\bojala\s+no\s+despertar\w*\b"),
    re.compile(r"\bno\s+quiero\s+(?:seguir\s+)?estar\s+aqui\s*(?:en\s+este\s+mundo|ya|mas|nunca)?\s*[.!]*$"),
    re.compile(r"\bno\s+quiero\s+estar\s+(?:mas\s+)?(?:aqui\s+)?en\s+este\s+mundo\b"),
    re.compile(r"\bya\s+no\s+aguanto\b.{0,25}\bme\s+(?:tiro|voy\s+a\s+tirar|mato|voy\s+a\s+matar)\b"),
    re.compile(r"\bme\s+voy\s+a\s+tirar\s+(?:del?|al|a\s+la)\s+(?:puente|cerro|techo|balcon|edificio|tren|rio|mar|linea)\b"),
    re.compile(r"\b(?:quiero|quisiera|ganas\s+de|pienso\s+en|pensando\s+en|voy\s+a|tentacion\s+de|miedo\s+de)\s+(?:hacerme|hacerse)\s+dano\b"),
    re.compile(r"\bhacerme\s+dano\s+a\s+proposito\b"),
    re.compile(r"\bcortar(?:me|se)\s+(?:las\s+)?(?:venas|munecas|brazos|piernas)\b"),
    re.compile(r"\bme\s+(?:corto|estoy\s+cortando|cortaba|he\s+cortado)\s+(?:las\s+)?(?:venas|munecas|brazos|piernas)\b"),
    re.compile(r"\bme\s+(?:voy\s+a|quiero)\s+cortar\s+(?:las\s+)?(?:venas|munecas|brazos)\b"),
    re.compile(r"\b(?:quiero|ganas\s+de|pienso\s+en|pensando\s+en|voy\s+a)\s+cortarme\b" + _NO_PELO + r"(?!\s+(?:el|la|las|los|un|una|mi|mis)\s+\w+)"),
    re.compile(r"\bme\s+quiero\s+cortar\b" + _NO_PELO + r"(?!\s+(?:el|la|las|los|un|una|mi|mis)\s+\w+)"),
]


def crisis_salud_mental_ampliada(texto: str) -> bool:
    t = _norm(texto)
    return bool(t) and any(p.search(t) for p in _PATRONES_CRISIS)


# ── Guarda del triage GES ────────────────────────────────────────────────────

_RE_CARDIORRESP_AGUDO = re.compile(
    r"\b(?:presion|opresion)\s+(?:en|sobre)\s+(?:el\s+)?pecho\b|"
    r"\baprieta\w*\s+(?:el\s+)?pecho\b|"
    r"\bdolor\s+(?:en\s+|de\s+)(?:el\s+)?pecho\b|\bme\s+duele\s+el\s+pecho\b|"
    r"\bfalta(?:n)?\s+(?:el\s+)?aire\b|\bno\s+(?:puedo|puede|logro|logra)\s+respirar\b|"
    r"\bdificultad\s+(?:para|al)\s+respirar\b|\bme\s+cuesta\s+(?:mucho\s+)?respirar\b(?!\s+por\s+(?:la\s+)?nariz)|"
    r"\brespira\s+(?:muy\s+)?(?:rapido|agitado|con\s+dificultad)\b|\brespiracion\s+(?:rapida|agitada)\b|"
    r"\bsudor\s+frio\b|\bme\s+ahogo\b|\bahogo\b|\bpalpitaciones\s+(?:fuertes|y\s+mareo)\b"
)
_RE_PECHO_BENIGNO = re.compile(r"\b(?:muscular|costal|contractura|al\s+(?:tocar|presionar|palpar)|por\s+el\s+gym)\b")
_RE_CARDIORRESP_LEVE = re.compile(r"\b(?:pecho|respir\w*|aire|ahogo|palpit\w*)\b")
_RE_BLOQUE_FIEBRE_NINO_GRAVE = re.compile(
    # "decaído" a secas NO entra: es el cuadro de cualquier gripe infantil y
    # bloquearía la hora de medio país. Lo juzga el prompt (claude_helper).
    r"\b(?:muy\s+somnolient[oa]|somnolient[oa]|no\s+reacciona|no\s+despierta|"
    r"quejido|labios\s+morados|convulsion\w*)\b"
)


def senal_critica_triage(texto: str) -> str | None:
    """Señal que prohíbe agendar por triage. Devuelve la categoría o None.

    Categorías: 'embarazo', 'lactante', 'cardiorrespiratoria', 'nino_grave'.
    Ante cualquiera, el bot deriva a urgencia/SAMU 131 en vez de ofrecer hora.
    """
    t = _norm(texto)
    if not t:
        return None
    if embarazo_con_signo_de_alarma(t, incluir_dolor=True):
        return "embarazo"
    if lactante_menor_3_meses_con_fiebre(t):
        return "lactante"
    if _RE_CARDIORRESP_AGUDO.search(t):
        # "dolor de pecho muscular al hacer ejercicio" no es señal aguda: se
        # deja pasar al flujo normal (evaluación médica) en vez de SAMU.
        if _RE_PECHO_BENIGNO.search(t) and not re.search(
                r"\b(?:falta(?:n)?\s+(?:el\s+)?aire|no\s+(?:puedo|puede)\s+respirar|sudor\s+frio|presion\s+en\s+(?:el\s+)?pecho|aprieta)", t):
            return None
        return "cardiorrespiratoria"
    if _RE_BLOQUE_FIEBRE_NINO_GRAVE.search(t) and (
            _RE_FIEBRE.search(t) or re.search(r"\b(?:vomit\w*|sed\b|diabetic\w*)", t)):
        return "nino_grave"
    return None


def hay_senal_cardiorrespiratoria(texto: str) -> bool:
    """Señal amplia (incluye 'pecho', 'respirar', 'aire'): se usa para descartar
    resultados de triage Climaterio/EPOC/Asma que no corresponden a un cuadro agudo."""
    return bool(_RE_CARDIORRESP_LEVE.search(_norm(texto)))


_PAT_DESCARTABLES = ("climaterio", "epoc", "asma")


def patologia_descartable_con_senal_cardiorresp(top_pathology: str | None) -> bool:
    p = _norm(top_pathology or "")
    return any(k in p for k in _PAT_DESCARTABLES)


# ── Acotar el skip del triage a contexto de cita ─────────────────────────────

_RE_DURACION_SINTOMA = re.compile(
    r"\b(?:hace|desde\s+hace|llevo|llevamos|lleva|durante|por|como\s+hace)\s+(?:como\s+|casi\s+|mas\s+de\s+|ya\s+)?"
    r"(?:una|1|media|\d+)\s+hora"
)
_RE_ATRASADO_CITA = re.compile(
    r"\b(?:voy|vamos|ando|estamos|vengo|llego|llegare|llegar|llegando|estoy)\s+(?:un\s+poco\s+|muy\s+|algo\s+)?atrasad[oa]s?\b"
)
_RE_ATRASADA_REGLA = re.compile(r"\b(?:regla|periodo|menstru\w*|atraso\s+menstrual|test\s+de\s+embarazo)\b")


def skip_triage_por_cita(texto: str) -> bool:
    """True si 'una hora', 'mi hora' o 'atrasado/a' están en contexto de CITA.

    'hace una hora que no puedo mover el brazo' (duración de síntoma) y 'estoy
    atrasada' (regla) NO deben saltar el triage.
    """
    t = _norm(texto)
    if not t:
        return False
    if re.search(r"\bmi\s+hora\b", t) and not _RE_DURACION_SINTOMA.search(t):
        return True
    if re.search(r"\buna\s+hora\b", t) and not _RE_DURACION_SINTOMA.search(t):
        return True
    if _RE_ATRASADO_CITA.search(t) and not _RE_ATRASADA_REGLA.search(t):
        return True
    return False
