"""Fuzz combinatorio del lector de RUT: separadores de miles × separador del DV
× prefijos × sufijos × mayúsculas/minúsculas en K.

Encontró (2026-09-29) que "6,061,6582" (miles con COMA) fabricaba un RUT de otra
persona: la coma no entraba en la extracción y el fallback derivaba el DV a
ciegas. Por defecto corre ~180.000 casos (~1 s); `RUT_FUZZ_N=330` ≈ 1.000.000.
"""
import itertools, os, random, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
from medilink import clean_rut, valid_rut, _calcular_dv_rut

MILES = [".", "", " ", ",", " "]
SEP_DV = ["-", "", " ", "–", "—", "_", "/", " - ", "‐", "|", " -"]
PREFIJOS = ["", "rut ", "RUT: ", "Rut:", "mi rut es ", "el rut de mi hijo es ",
            "Mi RUT: ", "es el ", "rut:", "RUT. ", "Nro ", "C.I. ", "cédula "]
SUFIJOS = ["", ".", " gracias", " 🙏", "\n", " por favor", "!", ",", " ok"]
N = int(os.getenv("RUT_FUZZ_N", "50"))


def _fmt(cuerpo: int, sep: str) -> str:
    s, g = str(cuerpo), []
    while s:
        g.insert(0, s[-3:]); s = s[:-3]
    return sep.join(g)


def test_fuzz_formatos_validos():
    rnd = random.Random(20260929)
    cuerpos = [rnd.randint(1_000_000, 29_999_999) for _ in range(N)]
    # cuerpos con DV = K y con DV = 0 siempre presentes
    cuerpos += [c for c in range(5_000_000, 5_100_000) if _calcular_dv_rut(str(c)) in ("K", "0")][:10]
    fallos = []
    for c in cuerpos:
        dv = _calcular_dv_rut(str(c))
        esperado = f"{c}-{dv}"
        for m, d, p, s in itertools.product(MILES, SEP_DV, PREFIJOS, SUFIJOS):
            for dvx in ((dv, dv.lower()) if dv == "K" else (dv,)):
                txt = f"{p}{_fmt(c, m)}{d}{dvx}{s}"
                r = clean_rut(txt)
                if r != esperado or not valid_rut(r):
                    fallos.append((txt, r))
                    if len(fallos) > 20:
                        break
    assert not fallos, fallos[:20]


def test_nunca_fabrica_un_rut_distinto_al_escrito():
    """Si el texto trae un RUT completo y válido, el resultado es ESE RUT o nada
    válido — jamás otro RUT válido (eso agenda en la ficha de un tercero)."""
    rnd = random.Random(7)
    for _ in range(20000):
        c = rnd.randint(1_000_000, 29_999_999)
        dv = _calcular_dv_rut(str(c))
        txt = rnd.choice(PREFIJOS) + _fmt(c, rnd.choice(MILES)) + rnd.choice(SEP_DV) + dv
        r = clean_rut(txt)
        assert not valid_rut(r) or r == f"{c}-{dv}", (txt, r)
