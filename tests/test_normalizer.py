"""
Tests unitarios para `normalizar_texto_paciente` en app/triage_ges.py.

Verifica que el diccionario de abreviaciones/typos/modismos rurales
transforma correctamente el texto libre de pacientes de WhatsApp,
sin destrozar mensajes legítimos ni IDs de botón del sistema.

Ejecución:
    PYTHONPATH=app:. venv/bin/python tests/test_normalizer.py

No depende de Medilink, Claude, WhatsApp ni del servicio GES.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT))

from triage_ges import normalizar_texto_paciente as N


# ── Casos: (id, input, expected_output) ───────────────────────────────────
# Si un caso solo verifica que una subcadena esté presente en el output,
# usamos un tuple (id, input, "contains", substring).
CASES: list[tuple] = [
    # ── Básicos: tildes y mayúsculas ──
    ("TIL-01", "DOLOR DE CABEZA", "dolor de cabeza"),
    ("TIL-02", "Dolor de Cabeza", "dolor de cabeza"),
    ("TIL-03", "tengo náuseas y mareos", "tengo nauseas y mareos"),
    ("TIL-04", "me siento raro", "me siento raro"),
    ("TIL-05", "señora doctora", "señora doctora"),  # preserva ñ

    # ── Abreviaciones WhatsApp clásicas ──
    ("ABR-01", "tngo dlor d cbza", "tengo dolor de cabeza"),
    ("ABR-02", "xq m duele muxo la grgnta", "porque me duele mucho la garganta"),
    ("ABR-03", "stoy ml dsp de comer", "estoy mal después de comer"),
    ("ABR-04", "kiero agendar pa mnna", "quiero agendar para mañana"),
    ("ABR-05", "tngo dlor d pcho y krzn", "tengo dolor de pecho y corazon"),
    ("ABR-06", "ta bn el horario dl dr", "esta bien el horario del doctor"),
    ("ABR-07", "xfa ayudnm", "por favor ayudnm"),  # ayudnm no está en dict

    # ── Typos médicos frecuentes ──
    ("TYP-01", "tengo feber alta", "tengo fiebre alta"),
    ("TYP-02", "me duele la gargnta", "me duele la garganta"),
    ("TYP-03", "tngo diarea hace 3 dias", "tengo diarrea hace 3 dias"),
    ("TYP-04", "bomitos y nausias", "vomitos y nauseas"),
    ("TYP-05", "me duele la cabesa", "me duele la cabeza"),
    ("TYP-06", "dolor en la rodia", "dolor en la rodilla"),
    ("TYP-07", "tngo preson alta y diabetis", "tengo presion alta y diabetes"),
    ("TYP-08", "creo q tngo bronkitis", "creo que tengo bronquitis"),
    ("TYP-09", "hemoragia x la naris", "hemorragia por la nariz"),

    # ── Participios rurales -ao → -ado ──
    ("PAR-01", "estoy sangrao mucho", "estoy sangrado mucho"),
    ("PAR-02", "tengo el tobillo hinchao", "tengo el tobillo hinchado"),
    ("PAR-03", "me lo dejo parao", "me lo dejo parado"),
    # No debe tocar palabras cortas
    ("PAR-04", "tao bien", "tao bien"),  # 3 letras antes de "ao" → no match

    # ── Modismos rurales chilenos ──
    # Nota: el normalizer NO ajusta concordancia de género del artículo
    # ("la guata" → "la estomago"). Eso es trabajo del motor GES, que
    # matchea por substring sobre sinónimos canónicos.
    ("CHI-01", "me duele la guata hace 2 dias", "me duele la estomago hace 2 dias"),
    ("CHI-02", "la guatita del bebe esta dura", "la estomago del bebe esta dura"),
    ("CHI-03", "tengo empacho", "tengo indigestion"),
    ("CHI-04", "cototo en la frente", "hinchazon en la frente"),
    ("CHI-05", "rasquiña en la piel", "picazon en la piel"),
    ("CHI-06", "mucha picason x todo el cuerpo", "mucha picazon por todo el cuerpo"),
    ("CHI-07", "escozor al orinar", "ardor al orinar"),

    # ── Composiciones realistas de pacientes ──
    ("REAL-01", "tngo dlor d cbza y guata hace 2 dias",
                "tengo dolor de cabeza y estomago hace 2 dias"),
    ("REAL-02", "m duele muxo la gargnta y tngo feber",
                "me duele mucho la garganta y tengo fiebre"),
    ("REAL-03", "stoy sangrao x la naris y me duele el pxo",
                "estoy sangrado por la nariz y me duele el pecho"),
    ("REAL-04", "tnga cototo en la rodia dsp de caerme",
                "tenga hinchazon en la rodilla después de caerme"),

    # ── Edge cases ──
    ("EDG-01", "", ""),
    ("EDG-02", "   ", "   "),  # solo espacios → retorna original
    ("EDG-03", "a", "a"),
    ("EDG-04", "!!!", "!!!"),
    ("EDG-05", "12345", "12345"),
    ("EDG-06", "hola", "hola"),

    # ── Preservación de IDs de botón del sistema ──
    # Estos NO deben ser normalizados — son IDs internos que viajan en
    # payloads de WhatsApp. Si el normalizer los destroza, se rompe el bot.
    ("BTN-01", "cat_medico", "cat_medico"),
    ("BTN-02", "cita_confirm:9001", "cita_confirm:9001"),
    ("BTN-03", "cita_reagendar:12345", "cita_reagendar:12345"),
    ("BTN-04", "cita_cancelar:7777", "cita_cancelar:7777"),

    # ── Preservación de puntuación al final de token ──
    ("PUN-01", "tngo dlor.", "tengo dolor."),
    ("PUN-02", "xq?", "porque?"),
    ("PUN-03", "tngo feber, muxa tos y gargnta",
               "tengo fiebre, mucha tos y garganta"),

    # ── No debe colisionar con palabras válidas del español ──
    ("COL-01", "voy a comprar pan", "voy a comprar pan"),
    ("COL-02", "el perro del vecino", "el perro del vecino"),
    ("COL-03", "mi hija tiene 5 años", "mi hija tiene 5 años"),
]


def run() -> int:
    """Corre los casos. Retorna número de fallos (0 = todos pasan)."""
    passed = 0
    failed = 0
    for case in CASES:
        cid, inp, *rest = case
        if len(rest) == 1:
            expected = rest[0]
            actual = N(inp)
            ok = actual == expected
        elif rest[0] == "contains":
            substring = rest[1]
            actual = N(inp)
            ok = substring in actual
            expected = f"<contains: {substring!r}>"
        else:
            print(f"❌ {cid} — caso malformado: {case}")
            failed += 1
            continue

        if ok:
            print(f"✅ {cid}  {inp!r:55s} → {actual!r}")
            passed += 1
        else:
            print(f"❌ {cid}  {inp!r}")
            print(f"      esperado: {expected!r}")
            print(f"      obtenido: {actual!r}")
            failed += 1

    total = passed + failed
    print()
    print(f"── Total: {passed}/{total} passed, {failed} failed ──")
    return failed


if __name__ == "__main__":
    sys.exit(1 if run() else 0)
