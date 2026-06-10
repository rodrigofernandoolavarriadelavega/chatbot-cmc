"""
Test offline del branching de clasificar_respuesta_seguimiento (FIX 2026-06-09).

Verifica el override determinístico de negación extraído directamente del regex
definido en claude_helper.py. Sin red, sin DB, sin imports de la app.

Escenarios:
- mejor: textos afirmativos (cache) → debe retornar "mejor"
- peor:  negación / persistencia / empeoramiento → debe retornar "peor"
- igual: sin cambios / lo mismo → debe retornar "igual"
"""
import re


# ── Replica exacta de los patrones definidos en claude_helper.py ─────────────

_SEGUIMIENTO_CACHE = {
    "mejor":              "mejor",
    "bien":               "mejor",
    "ya estoy bien":      "mejor",
    "me siento mejor":    "mejor",
    "mejoré":             "mejor",
    "mejore":             "mejor",
    "me recuperé":        "mejor",
    "me recupere":        "mejor",
    "estoy bien":         "mejor",
    "bastante mejor":     "mejor",
    "mucho mejor":        "mejor",
    "igual":              "igual",
    "lo mismo":           "igual",
    "sin cambios":        "igual",
    "igual que antes":    "igual",
    "más o menos":        "igual",
    "mas o menos":        "igual",
    "más o menos igual":  "igual",
    "peor":               "peor",
    "mal":                "peor",
    "sigo mal":           "peor",
    "me siento peor":     "peor",
    "empeoré":            "peor",
    "empeore":            "peor",
    "peor que antes":     "peor",
    "no mejoro":          "peor",
    "no mejoré":          "peor",
    "cada vez peor":      "peor",
    "sigo igual de mal":  "peor",
}

_NEG_PEOR = re.compile(
    r"(no (ha|a|e) (mejorado?|mejora[od]|mejorou|sanado?)|"
    r"sigue? (el |los |la |las )?(dolor|molest|sintom|moles|fiebre|nausea|mareo)|"
    r"persiste|no (me |te |le )?(ha (mejorado?|pasado?)|mejora|pasa)|"
    r"cada vez (mas|más) (mal|peor)|"
    r"(empeor|peorar|empeoró|empeoró|empeoran|se (ha |está |esta )?(puest[oa] )?peor)|"
    r"no (hay |ha |se nota )?(ninguna? |mucha? )?(mejori?a?|mejoría?|cambio|diferencia)|"
    r"(dolor|molest|sintom).{0,30}(persiste|no (cede|mejora|pasa|desaparece)))",
    re.IGNORECASE,
)
_NEG_IGUAL = re.compile(
    r"(igual(ito)?|lo mismo (de antes)?|sin (mucho )?cambio|"
    r"(más|mas) o menos (igual|lo mismo)|no (gran|mucho) (cambio|mejori?a?))",
    re.IGNORECASE,
)


def clasificar_det(mensaje: str):
    """Replica el override determinístico de claude_helper sin llamar Claude."""
    clave = mensaje.strip().lower()
    if clave in _SEGUIMIENTO_CACHE:
        return _SEGUIMIENTO_CACHE[clave]
    if _NEG_PEOR.search(clave):
        return "peor"
    if _NEG_IGUAL.search(clave):
        return "igual"
    return None   # ambiguo → iría a Claude en prod


# ── Casos de prueba ───────────────────────────────────────────────────────────

CASOS_PEOR = [
    ("el dolor de cabeza persiste no a mejorado", "peor"),   # caso real del bug
    ("persiste el dolor",                          "peor"),
    ("sigue el dolor de cabeza",                   "peor"),
    ("no ha mejorado nada",                        "peor"),
    ("no mejoré",                                  "peor"),
    ("no mejoro",                                  "peor"),
    ("cada vez más mal",                           "peor"),
    ("empeoro",                                    "peor"),
    ("cada vez peor",                              "peor"),
    ("el dolor no cede",                           "peor"),
    ("sigo con el mismo dolor no hay ninguna mejoria", "peor"),
    ("me siento peor",                             "peor"),
]

CASOS_IGUAL = [
    ("igual",              "igual"),
    ("lo mismo de antes",  "igual"),
    ("más o menos igual",  "igual"),
    ("sin mucho cambio",   "igual"),
    ("mas o menos lo mismo", "igual"),
]

CASOS_MEJOR = [
    ("mejor",          "mejor"),
    ("bien",           "mejor"),
    ("me siento mejor","mejor"),
    ("ya estoy bien",  "mejor"),
    ("mejoré",         "mejor"),
    ("mucho mejor",    "mejor"),
]


def run_suite():
    fallos = []
    todos = CASOS_PEOR + CASOS_IGUAL + CASOS_MEJOR
    for txt, esperado in todos:
        got = clasificar_det(txt)
        if got != esperado:
            fallos.append(f"  FAIL: {txt!r} → {got!r}  (esperado {esperado!r})")

    if fallos:
        raise AssertionError(f"{len(fallos)} fallo(s):\n" + "\n".join(fallos))
    print(f"OK — {len(todos)} casos, 0 fallos.")


def test_negacion_clasifica_peor():
    """pytest entry point."""
    errores = []
    for txt, _ in CASOS_PEOR:
        got = clasificar_det(txt)
        if got != "peor":
            errores.append(f"  FAIL: {txt!r} → {got!r} (esperado 'peor')")
    assert not errores, "\n".join(errores)


def test_persistencia_clasifica_igual():
    """pytest entry point."""
    errores = []
    for txt, _ in CASOS_IGUAL:
        got = clasificar_det(txt)
        if got != "igual":
            errores.append(f"  FAIL: {txt!r} → {got!r} (esperado 'igual')")
    assert not errores, "\n".join(errores)


def test_afirmativo_clasifica_mejor():
    """pytest entry point."""
    errores = []
    for txt, _ in CASOS_MEJOR:
        got = clasificar_det(txt)
        if got != "mejor":
            errores.append(f"  FAIL: {txt!r} → {got!r} (esperado 'mejor')")
    assert not errores, "\n".join(errores)


if __name__ == "__main__":
    run_suite()
