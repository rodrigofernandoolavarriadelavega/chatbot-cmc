"""
Regresión — portaviones consolidado 2026-09-24, problema #13.

Caso real: WA 56947531125, 2026-09-22 19:56:46 — "Buenas tardes, habrá hora
con médico que atienda por Fonasa" (primer mensaje, sin historial) recibió
directo una hora de *Psicología Adulto* con Jorge Montalba, en vez de
Medicina General.

Root cause reconstruido con `app/flows.py::_detectar_apellido_profesional`:
el alias "coque" (apodo de Jorge Montalba, Psicología) usa matching por
SUBSTRING contra el texto colapsado sin espacios (regla normal para alias
>=5 chars). El texto "...con medico QUE atienda..." colapsa a
"...conmedicoqueatienda..." y esa cadena CONTIENE "coque" (la "co" final de
"medico" + "que") — falso positivo que hace que el bot interprete el mensaje
como una mención al Dr. Montalba y ofrezca Psicología en vez de dejar que
`_detectar_especialidad_en_texto` (que sí matchea "con médico" → medicina
general) resuelva el shortcut determinístico.

Fix: `_APELLIDOS_REQUIERE_BORDE` — aliases que, aunque midan >=5 chars, son
apodos completos y deben exigir word-boundary (como los aliases cortos) en
vez de substring-en-colapsado. Sin romper el alias real ("hora con el
coque"/"coque montalba").

Ejecución:
    python tests/test_portaviones_2026_09_24_apellido_coque.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT))

import flows  # noqa: E402

FAILS = 0


def check(label: str, got, expected):
    global FAILS
    ok = got == expected
    print(f"{'OK  ' if ok else 'FAIL'} {label}: got={got!r} expected={expected!r}")
    if not ok:
        FAILS += 1


# ── Caso real: NO debe matchear "coque" (Montalba) por accidente ──
check(
    "coque_falso_positivo_caso_real",
    flows._detectar_apellido_profesional(
        "Buenas tardes, habrá hora con médico que atienda por Fonasa"
    ),
    None,
)

check(
    "coque_falso_positivo_variante_corta",
    flows._detectar_apellido_profesional("tengo que ir al medico"),
    None,
)

check(
    "coque_falso_positivo_necesito",
    flows._detectar_apellido_profesional("necesito medico que atienda hoy"),
    None,
)

# ── El alias real "coque" (nickname completo) debe seguir funcionando ──
check(
    "coque_alias_real",
    flows._detectar_apellido_profesional("quiero hora con el coque"),
    "montalba",
)

check(
    "coque_alias_real_apellido",
    flows._detectar_apellido_profesional("hora con coque montalba"),
    "montalba",
)

# ── End-to-end del detector determinístico de especialidad: con el fix,
#    "con médico" debe resolver a medicina general sin que el falso positivo
#    de apellido lo intercepte antes. ──
check(
    "especialidad_medicina_general_tras_fix",
    flows._detectar_especialidad_en_texto(
        "Buenas tardes, habrá hora con médico que atienda por Fonasa"
    ),
    "medicina general",
)

print()
if FAILS:
    print(f"{FAILS} fallo(s)")
    sys.exit(1)
print("Todos los casos OK")
