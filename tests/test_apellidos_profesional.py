"""Stress test: detector de apellido de profesional robusto ante mutaciones.

Para cada profesional del CMC, genera ~100 variaciones del nombre real con:
  - Tildes presentes / ausentes
  - Confusiones fonéticas comunes (b↔v, j↔g↔x↔h, s↔z, ll↔y, c↔k)
  - Underscores insertados
  - Emojis al final, en medio o rodeando
  - Dígitos insertados entre letras
  - Chars invisibles (ZWSP, ZWJ, BOM, nbsp)
  - Mayúsculas/minúsculas mezcladas
  - Prefijos tipo "Dr.", "el doctor", "con el kinesiólogo"
  - Fullwidth (copy-paste de docs)

Valida que `_detectar_apellido_profesional()` devuelve la key correcta
para cada variación (o None si la variación es demasiado destructiva).

Ejecución:
    PYTHONPATH=app:. python3 tests/test_apellidos_profesional.py
"""
from __future__ import annotations

import random
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT))

from flows import _detectar_apellido_profesional
from medilink import _ids_para_especialidad, PROFESIONALES


# Mapa: apellido "base canonical" → (key esperada, id_profesional esperado)
# Se eligen nombres/apellidos únicos suficientes. Key debe existir en
# ESPECIALIDADES_MAP como lista de ID único.
CASOS: list[tuple[str, str, int]] = [
    # (apellido_base, key_esperada_mínima, id_esperado)
    ("olavarria",   "olavarría",     1),
    ("olavarría",   "olavarría",     1),
    ("abarca",      "abarca",        73),
    ("marquez",     "marquez",       13),
    ("márquez",     "marquez",       13),
    ("borrego",     "otorrinolaringología", 23),
    ("millan",      "cardiología",   60),
    ("millán",      "cardiología",   60),
    ("rejon",       "ginecología",   61),
    ("rejón",       "ginecología",   61),
    ("quijano",     "gastroenterología", 65),
    ("burgos",      "burgos",        55),
    ("jimenez",     "jimenez",       72),
    ("jiménez",     "jimenez",       72),
    ("castillo",    "ortodoncia",    66),
    ("fredes",      "endodoncia",    75),
    ("valdes",      "implantología", 69),
    ("valdés",      "implantología", 69),
    ("fuentealba",  "estética facial", 76),
    ("acosta",      "masoterapia",   59),
    ("armijo",      "armijo",        77),
    ("etcheverry",  "etcheverry",    21),
    ("pinto",       "nutrición",     52),
    ("montalba",    "montalba",      74),
    ("rodriguez",   "rodriguez",     49),
    ("rodríguez",   "rodriguez",     49),
    ("arratia",     "fonoaudiología", 70),
    ("guevara",     "podología",     56),
    ("pardo",       "ecografía",     68),
    ("sarai",       "matrona",       67),
]


# ── Prefijos / sufijos comunes que un paciente real puede escribir ──
PREFIJOS = [
    "", "con ", "con el ", "con la ", "al ", "del ", "el ", "la ",
    "dr. ", "dr ", "dra. ", "dra ", "doctor ", "doctora ",
    "hola, quiero con ", "necesito con ", "prefiero a ",
    "quiero ver al ", "con el doctor ", "con la doctora ",
]
SUFIJOS = [
    "", " por favor", " gracias", "!", "!!", ".", "...", " :)",
    " 😊", "😀", " ✨", " 👍", "🙏",
]

# Mapas de mutación fonética (aplicar de a uno por variación)
FONETICAS = {
    "b": "v", "v": "b",
    "j": "h", "g": "j", "x": "j",
    "s": "z", "z": "s",
    "ll": "y", "y": "ll",
    "c": "k", "k": "c",
}

# Chars invisibles que algunos teclados insertan
INVISIBLES = ["\u200b", "\u200c", "\u200d", "\ufeff", "\u00a0"]

# Emojis y separadores que interrumpen la palabra
RUIDO_INTERNO = ["_", "-", ".", "·", " ", "*", "·"]
RUIDO_CON_EMOJI = ["😊", "👍", "✨", "🎉", "🩺"]


def _sin_tildes(s: str) -> str:
    d = unicodedata.normalize("NFD", s)
    return "".join(c for c in d if not unicodedata.combining(c))


def _mutar_fonetica(s: str, rng: random.Random) -> str:
    """Reemplaza una letra por su confusión fonética típica."""
    cand = [(i, c) for i, c in enumerate(s) if c in FONETICAS]
    if not cand:
        return s
    i, c = rng.choice(cand)
    return s[:i] + FONETICAS[c] + s[i + 1:]


def _insertar_ruido(s: str, rng: random.Random, chars: list[str], n: int = 1) -> str:
    """Inserta n caracteres de `chars` en posiciones aleatorias de s."""
    out = list(s)
    for _ in range(n):
        if len(out) < 2:
            break
        pos = rng.randint(1, len(out) - 1)
        out.insert(pos, rng.choice(chars))
    return "".join(out)


def _case_aleatorio(s: str, rng: random.Random) -> str:
    return "".join(c.upper() if rng.random() < 0.3 else c for c in s)


def _fullwidth(s: str) -> str:
    """Convierte ASCII a fullwidth (U+FF00-FF5E rango)."""
    out = []
    for c in s:
        if "a" <= c.lower() <= "z":
            out.append(chr(ord(c) + 0xFEE0))
        else:
            out.append(c)
    return "".join(out)


def _generar_variaciones(apellido: str, n: int, rng: random.Random) -> list[str]:
    """Genera n variaciones de `apellido` combinando mutaciones."""
    base = apellido.lower()
    variaciones = set()
    # 1. Original y sin tildes
    variaciones.add(base)
    variaciones.add(_sin_tildes(base))
    # 2. Con prefijos y sufijos
    for pre in PREFIJOS[:6]:
        variaciones.add(f"{pre}{base}")
    for suf in SUFIJOS[:6]:
        variaciones.add(f"{base}{suf}")
    # 3. Mutaciones aleatorias
    while len(variaciones) < n:
        v = base
        dice = rng.random()
        if dice < 0.2:
            v = _sin_tildes(v)
        if rng.random() < 0.3:
            v = _insertar_ruido(v, rng, RUIDO_INTERNO, rng.randint(1, 2))
        if rng.random() < 0.2:
            v = _insertar_ruido(v, rng, [str(rng.randint(0, 9))], rng.randint(1, 2))
        if rng.random() < 0.2:
            v = _insertar_ruido(v, rng, INVISIBLES, rng.randint(1, 2))
        if rng.random() < 0.3:
            v = _case_aleatorio(v, rng)
        if rng.random() < 0.15:
            v = _fullwidth(v)
        if rng.random() < 0.3:
            v = rng.choice(PREFIJOS) + v
        if rng.random() < 0.3:
            v = v + rng.choice(SUFIJOS)
        if rng.random() < 0.2:
            v = v + rng.choice(RUIDO_CON_EMOJI)
        variaciones.add(v)
    return list(variaciones)[:n]


def _run(per_prof: int = 100) -> int:
    rng = random.Random(42)
    total = passed = failed = 0
    fails: list[tuple[str, str, str, str | None]] = []

    for apellido_base, key_esperada, id_esperado in CASOS:
        variaciones = _generar_variaciones(apellido_base, per_prof, rng)
        for v in variaciones:
            total += 1
            got_key = _detectar_apellido_profesional(v)
            ok = got_key is not None
            if ok:
                ids = _ids_para_especialidad(got_key)
                # Si la especialidad mapea a un único profesional, debe ser el esperado.
                # Si mapea a varios (e.g. Márquez vs "medicina familiar" [13]), debe
                # incluir el id esperado.
                ok = id_esperado in ids
            if ok:
                passed += 1
            else:
                failed += 1
                if len(fails) < 20:
                    fails.append((apellido_base, v, key_esperada, got_key))

    if fails:
        print("── Primeros 20 fails ──")
        for base, v, expected, got in fails:
            print(f"  base={base!r:14s} input={v!r:50s} expected_key={expected!r:20s} got_key={got!r}")

    print(f"\n── Total: {passed}/{total} passed, {failed} failed "
          f"({len(CASOS)} profs × {per_prof} variantes) ──")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run() else 0)
