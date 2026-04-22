"""100 variaciones de expresiones de hora — garantiza que parse_hora
cubra todas las formas en que un paciente chileno podría escribir un horario.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from time_parser import parse_hora  # noqa: E402


CASOS = [
    # ── Numéricos con separador (12) ─────────────────────────────────────────
    ("10:30",       (10, 30)),
    ("10.30",       (10, 30)),
    ("10-30",       (10, 30)),
    ("10,30",       (10, 30)),
    ("10h30",       (10, 30)),
    ("10H30",       (10, 30)),
    ("10: 30",      (10, 30)),
    ("10 :30",      (10, 30)),
    ("10 : 30",     (10, 30)),
    ("10  30",      (10, 30)),
    ("10 30",       (10, 30)),
    ("1030",        (10, 30)),
    # ── Numéricos con sufijo (10) ────────────────────────────────────────────
    ("10:30hrs",    (10, 30)),
    ("10:30 hrs",   (10, 30)),
    ("10:30 horas", (10, 30)),
    ("10:30hs",     (10, 30)),
    ("10hrs",       (10, 0)),
    ("10 hrs",      (10, 0)),
    ("10 hs",       (10, 0)),
    ("10hs",        (10, 0)),
    ("10 horas",    (10, 0)),
    ("10h",         (10, 0)),
    # ── AM / PM (13) ─────────────────────────────────────────────────────────
    ("10am",        (10, 0)),
    ("10 am",       (10, 0)),
    ("10 AM",       (10, 0)),
    ("10 a.m.",     (10, 0)),
    ("10 a.m",      (10, 0)),
    ("10pm",        (22, 0)),
    ("10 pm",       (22, 0)),
    ("10 PM",       (22, 0)),
    ("10 p.m.",     (22, 0)),
    ("10:30 am",    (10, 30)),
    ("10:30am",     (10, 30)),
    ("10:30 pm",    (22, 30)),
    ("10:30pm",     (22, 30)),
    # ── Prefijos conversacionales (9) ────────────────────────────────────────
    ("a las 10",          (10, 0)),
    ("a las 10:30",       (10, 30)),
    ("tipo 10",           (10, 0)),
    ("tipo 10:30",        (10, 30)),
    ("tipo 17hrs",        (17, 0)),
    ("sobre las 10",      (10, 0)),
    ("cerca de las 10",   (10, 0)),
    ("como a las 10",     (10, 0)),
    ("a eso de las 10",   (10, 0)),
    # ── Marcadores de período (10) ───────────────────────────────────────────
    ("10 de la mañana",   (10, 0)),
    ("10 de la manana",   (10, 0)),
    ("10 por la mañana",  (10, 0)),
    ("5 de la tarde",     (17, 0)),
    ("5 por la tarde",    (17, 0)),
    ("6 de la tarde",     (18, 0)),
    ("1 de la tarde",     (13, 0)),
    ("8 de la noche",     (20, 0)),
    ("9 de la noche",     (21, 0)),
    ("12 de la noche",    (0, 0)),
    # ── Palabras-número (15) ─────────────────────────────────────────────────
    ("diez",              (10, 0)),
    ("diez y media",      (10, 30)),
    ("diez y cuarto",     (10, 15)),
    ("diez treinta",      (10, 30)),
    ("diez quince",       (10, 15)),
    ("nueve y media",     (9, 30)),
    ("once y media",      (11, 30)),
    ("ocho treinta",      (8, 30)),
    ("nueve quince",      (9, 15)),
    ("diez cuarenta y cinco", (10, 45)),
    ("a las diez",        (10, 0)),
    ("a las diez y media",(10, 30)),
    ("las diez",          (10, 0)),
    ("las diez y cuarto", (10, 15)),
    ("ocho y cuarenta",   (8, 40)),
    # ── "y media/cuarto/tres cuartos" (8) ────────────────────────────────────
    ("10 y media",        (10, 30)),
    ("10 y cuarto",       (10, 15)),
    ("10 y tres cuartos", (10, 45)),
    ("10 y 30",           (10, 30)),
    ("10 y 15",           (10, 15)),
    ("10 y 45",           (10, 45)),
    ("9 y 45",            (9, 45)),
    ("8 y 15",            (8, 15)),
    # ── Restas (5) ───────────────────────────────────────────────────────────
    ("10 menos cuarto",       (9, 45)),
    ("10 menos 10",           (9, 50)),
    ("cuarto para las 10",    (9, 45)),
    ("cuarto para las diez",  (9, 45)),
    ("un cuarto para las 10", (9, 45)),
    # ── Mediodía / medianoche (4) ────────────────────────────────────────────
    ("mediodia",       (12, 0)),
    ("medio dia",      (12, 0)),
    ("al mediodia",    (12, 0)),
    ("medianoche",     (0, 0)),
    # ── Heurística clínica 1–7 → PM sin calificador (4) ──────────────────────
    ("a las 5",        (17, 0)),
    ("a las 6",        (18, 0)),
    ("a las 7",        (19, 0)),
    ("5pm",            (17, 0)),
    # ── Frases naturales (10) ────────────────────────────────────────────────
    ("quiero a las 10",         (10, 0)),
    ("prefiero 10:30",          (10, 30)),
    ("me acomoda 10:30",        (10, 30)),
    ("puede ser 10:30",         (10, 30)),
    ("reservame la de 10:30",   (10, 30)),
    ("dame las 10",             (10, 0)),
    ("quisiera 10 y 30",        (10, 30)),
    ("por favor a las 9",       (9, 0)),
    ("la de 10:30",             (10, 30)),
    ("esa de 10 30",            (10, 30)),
]


def main():
    fallos = []
    for texto, esperado in CASOS:
        got = parse_hora(texto)
        if got != esperado:
            fallos.append((texto, esperado, got))
    total = len(CASOS)
    ok = total - len(fallos)
    print(f"[parse_hora] {ok}/{total} pasan")
    if fallos:
        print(f"\n{len(fallos)} FALLAS:")
        for texto, esp, got in fallos:
            print(f"  {texto!r:40} esperado={esp} got={got}")
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
