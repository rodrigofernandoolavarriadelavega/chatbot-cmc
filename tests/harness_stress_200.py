"""
harness_stress_200.py — 200 casos de stress para el chatbot CMC.

Foco:
  (A) Cambios de tema mid-flow: ¿el bot detecta cuando el paciente abandona
      el flujo en curso y empieza una conversación nueva?
  (B) Respuestas incoherentes / tangenciales durante estados con expectativa
      estricta (WAIT_SLOT, WAIT_RUT, WAIT_CITA_*).
  (C) Entradas adversariales (muy largas, unicode raro, inyección).
  (D) Emergencias mid-flow.
  (E) Re-entrada: terminar un flujo y empezar otro.

Reutiliza toda la maquinaria de mocks de harness_50 (importarlo aplica los
monkey-patches a medilink + claude_helper + flows). No toca producción.

Ejecución:
    PYTHONPATH=app:. venv/bin/python tests/harness_stress_200.py
"""
from __future__ import annotations

import asyncio
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

# Importar harness_50 → aplica monkey-patches y nos deja usar run_convo,
# NO_ENTENDI_MARKERS, fake_* fixtures, etc.
import harness_50  # noqa: E402
from harness_50 import (  # noqa: E402
    run_convo, NO_ENTENDI_MARKERS, BUGS,
    FAKE_CITAS_PACIENTE,
)

NO_ERROR = {"none": NO_ENTENDI_MARKERS}


def setup_una_cita_generic():
    FAKE_CITAS_PACIENTE.clear()
    FAKE_CITAS_PACIENTE.extend([{
        "id": 801, "id_profesional": 73,
        "profesional": "Dr. Andrés Abarca",
        "especialidad": "Medicina General",
        "fecha": "2026-04-20", "fecha_display": "lun 20 abr",
        "hora": "10:00", "hora_inicio": "10:00", "hora_fin": "10:15",
    }])


async def main():
    results: list[tuple] = []

    def mk(name, phone, steps, setup=None):
        results.append((name, phone, steps, setup))

    # ═════════════════════════════════════════════════════════════════════════
    # GROUP A — CAMBIOS DE TEMA MID-FLOW (50 tests)
    # El paciente empieza un flujo y a mitad de camino cambia de idea,
    # pregunta otra cosa, o se desvía. ¿El bot reacciona o queda atrapado?
    # ═════════════════════════════════════════════════════════════════════════

    # A1-A10: en WAIT_ESPECIALIDAD (eligiendo categoría/especialidad)
    mk("A01 WAIT_ESPECIALIDAD → pregunta precio", "56901000001", [
        ("quiero agendar", {"any": ["especialidad", "Medicina", "Agendar"]}),
        ("cuánto cuesta una tapadura", None),
    ])
    mk("A02 WAIT_ESPECIALIDAD → hola", "56901000002", [
        ("quiero agendar", {"any": ["especialidad", "Medicina", "Agendar"]}),
        ("hola", {"any": ["Agendar", "opciones"]}),
    ])
    mk("A03 WAIT_ESPECIALIDAD → emergencia", "56901000003", [
        ("quiero agendar", {"any": ["especialidad", "Medicina", "Agendar"]}),
        ("me duele mucho el pecho fuerte", ["SAMU", "131"]),
    ])
    mk("A04 WAIT_ESPECIALIDAD → cancelar", "56901000004", [
        ("quiero agendar", {"any": ["especialidad", "Medicina", "Agendar"]}),
        ("mejor cancelo mi hora", None),
    ])
    mk("A05 WAIT_ESPECIALIDAD → texto random", "56901000005", [
        ("quiero agendar", {"any": ["especialidad", "Medicina", "Agendar"]}),
        ("xqwertyasdf", None),
    ])
    mk("A06 WAIT_ESPECIALIDAD → número random", "56901000006", [
        ("quiero agendar", {"any": ["especialidad", "Medicina", "Agendar"]}),
        ("88", None),
    ])
    mk("A07 WAIT_ESPECIALIDAD → menu explícito", "56901000007", [
        ("quiero agendar", {"any": ["especialidad", "Medicina", "Agendar"]}),
        ("menu", {"any": ["Agendar", "opciones"]}),
    ])
    mk("A08 WAIT_ESPECIALIDAD → recepción", "56901000008", [
        ("quiero agendar", {"any": ["especialidad", "Medicina", "Agendar"]}),
        ("mejor hablar con alguien", None),
    ])
    mk("A09 WAIT_ESPECIALIDAD → 'ya no quiero'", "56901000009", [
        ("quiero agendar", {"any": ["especialidad", "Medicina", "Agendar"]}),
        ("ya no quiero nada", None),
    ])
    mk("A10 WAIT_ESPECIALIDAD → pregunta horario", "56901000010", [
        ("quiero agendar", {"any": ["especialidad", "Medicina", "Agendar"]}),
        ("a qué hora abren", None),
    ])

    # A11-A20: en WAIT_SLOT (viendo horarios disponibles)
    mk("A11 WAIT_SLOT → cuanto cuesta", "56901000011", [
        ("quiero agendar medicina general", ["09:"]),
        ("cuánto cuesta", None),
    ])
    mk("A12 WAIT_SLOT → cambio de especialidad", "56901000012", [
        ("quiero agendar medicina general", ["09:"]),
        ("mejor quiero kinesiología", None),
    ])
    mk("A13 WAIT_SLOT → hola", "56901000013", [
        ("quiero agendar medicina general", ["09:"]),
        ("hola", {"any": ["Agendar", "opciones"]}),
    ])
    mk("A14 WAIT_SLOT → emergencia", "56901000014", [
        ("quiero agendar medicina general", ["09:"]),
        ("me estoy ahogando no puedo respirar", ["SAMU", "131"]),
    ])
    mk("A15 WAIT_SLOT → pregunta atienden fonasa", "56901000015", [
        ("quiero agendar medicina general", ["09:"]),
        ("atienden fonasa", None),
    ])
    mk("A16 WAIT_SLOT → me arrepentí", "56901000016", [
        ("quiero agendar medicina general", ["09:"]),
        ("mejor no, me arrepentí", None),
    ])
    mk("A17 WAIT_SLOT → emoji random", "56901000017", [
        ("quiero agendar medicina general", ["09:"]),
        ("🤔🤷", None),
    ])
    mk("A18 WAIT_SLOT → ok", "56901000018", [
        ("quiero agendar medicina general", ["09:"]),
        ("ok", {"any": ["Fonasa", "Particular", "modalidad"]}),
    ])
    mk("A19 WAIT_SLOT → '4000'", "56901000019", [
        ("quiero agendar medicina general", ["09:"]),
        ("4000", None),
    ])
    mk("A20 WAIT_SLOT → ideación suicida", "56901000020", [
        ("quiero agendar medicina general", ["09:"]),
        ("no quiero seguir viviendo", {"all": ["Salud Responde"]}),
    ])

    # A21-A30: en WAIT_RUT_AGENDAR (después de elegir slot, pide RUT)
    mk("A21 WAIT_RUT_AGENDAR → pregunta precio", "56901000021", [
        ("quiero agendar medicina general", ["09:"]),
        ("ok", {"any": ["Fonasa", "Particular"]}),
        ("1", {"any": ["rut", "RUT"]}),
        ("cuánto cuesta", None),
    ])
    mk("A22 WAIT_RUT_AGENDAR → cambio intención", "56901000022", [
        ("quiero agendar medicina general", ["09:"]),
        ("ok", {"any": ["Fonasa", "Particular"]}),
        ("1", {"any": ["rut", "RUT"]}),
        ("mejor quiero cancelar mi hora", None),
    ])
    mk("A23 WAIT_RUT_AGENDAR → emergencia", "56901000023", [
        ("quiero agendar medicina general", ["09:"]),
        ("ok", {"any": ["Fonasa", "Particular"]}),
        ("1", {"any": ["rut", "RUT"]}),
        ("me muero", ["SAMU", "131"]),
    ])
    mk("A24 WAIT_RUT_AGENDAR → hola", "56901000024", [
        ("quiero agendar medicina general", ["09:"]),
        ("ok", {"any": ["Fonasa", "Particular"]}),
        ("1", {"any": ["rut", "RUT"]}),
        ("hola", {"any": ["Agendar", "opciones"]}),
    ])
    mk("A25 WAIT_RUT_AGENDAR → 'no sé mi rut'", "56901000025", [
        ("quiero agendar medicina general", ["09:"]),
        ("ok", {"any": ["Fonasa", "Particular"]}),
        ("1", {"any": ["rut", "RUT"]}),
        ("no sé mi rut", None),
    ])
    mk("A26 WAIT_RUT_AGENDAR → texto largo", "56901000026", [
        ("quiero agendar medicina general", ["09:"]),
        ("ok", {"any": ["Fonasa", "Particular"]}),
        ("1", {"any": ["rut", "RUT"]}),
        ("buenos dias necesito saber si tienen disponibilidad urgente", None),
    ])
    mk("A27 WAIT_RUT_AGENDAR → emoji solo", "56901000027", [
        ("quiero agendar medicina general", ["09:"]),
        ("ok", {"any": ["Fonasa", "Particular"]}),
        ("1", {"any": ["rut", "RUT"]}),
        ("😀", None),
    ])
    mk("A28 WAIT_RUT_AGENDAR → pregunta dirección", "56901000028", [
        ("quiero agendar medicina general", ["09:"]),
        ("ok", {"any": ["Fonasa", "Particular"]}),
        ("1", {"any": ["rut", "RUT"]}),
        ("dónde están ubicados", None),
    ])
    mk("A29 WAIT_RUT_AGENDAR → rut válido (baseline)", "56901000029", [
        ("quiero agendar medicina general", ["09:"]),
        ("ok", {"any": ["Fonasa", "Particular"]}),
        ("1", {"any": ["rut", "RUT"]}),
        ("11111111-1", None),
    ])
    mk("A30 WAIT_RUT_AGENDAR → rut chocho", "56901000030", [
        ("quiero agendar medicina general", ["09:"]),
        ("ok", {"any": ["Fonasa", "Particular"]}),
        ("1", {"any": ["rut", "RUT"]}),
        ("123", None),
    ])

    # A31-A40: en WAIT_CITA_CANCELAR / WAIT_CITA_REAGENDAR
    mk("A31 WAIT_CITA_CANCELAR → hola", "56901000031", [
        ("cancelar", {"any": ["rut", "RUT"]}),
        ("11111111-1", None),
        ("hola", {"any": ["Agendar", "opciones"]}),
    ])
    mk("A32 WAIT_CITA_CANCELAR → 'mejor agendar otra'", "56901000032", [
        ("cancelar", {"any": ["rut", "RUT"]}),
        ("11111111-1", None),
        ("mejor quiero agendar otra", None),
    ], setup=setup_una_cita_generic)
    mk("A33 WAIT_CITA_CANCELAR → pregunta precio", "56901000033", [
        ("cancelar", {"any": ["rut", "RUT"]}),
        ("11111111-1", None),
        ("cuánto cuesta una ecografia", None),
    ], setup=setup_una_cita_generic)
    mk("A34 WAIT_CITA_REAGENDAR → 'no ya no'", "56901000034", [
        ("reagendar", {"any": ["rut", "RUT"]}),
        ("11111111-1", None),
        ("no ya no quiero", None),
    ], setup=setup_una_cita_generic)
    mk("A35 WAIT_CITA_REAGENDAR → emergencia", "56901000035", [
        ("reagendar", {"any": ["rut", "RUT"]}),
        ("11111111-1", None),
        ("tengo dolor fuerte en el pecho", ["SAMU", "131"]),
    ], setup=setup_una_cita_generic)
    mk("A36 WAIT_CITA_CANCELAR → emoji", "56901000036", [
        ("cancelar", {"any": ["rut", "RUT"]}),
        ("11111111-1", None),
        ("🤷", None),
    ], setup=setup_una_cita_generic)
    mk("A37 WAIT_CITA_CANCELAR → pregunta horario", "56901000037", [
        ("cancelar", {"any": ["rut", "RUT"]}),
        ("11111111-1", None),
        ("hasta que hora atienden", None),
    ], setup=setup_una_cita_generic)
    mk("A38 WAIT_CITA_REAGENDAR → 'menu'", "56901000038", [
        ("reagendar", {"any": ["rut", "RUT"]}),
        ("11111111-1", None),
        ("menu", {"any": ["Agendar", "opciones"]}),
    ], setup=setup_una_cita_generic)
    mk("A39 WAIT_CITA_CANCELAR → 'muchas gracias'", "56901000039", [
        ("cancelar", {"any": ["rut", "RUT"]}),
        ("11111111-1", None),
        ("muchas gracias", None),
    ], setup=setup_una_cita_generic)
    mk("A40 WAIT_CITA_REAGENDAR → num grande", "56901000040", [
        ("reagendar", {"any": ["rut", "RUT"]}),
        ("11111111-1", None),
        ("9999", None),
    ], setup=setup_una_cita_generic)

    # A41-A50: en CONFIRMING_CITA (último paso antes de crear)
    mk("A41 CONFIRMING_CITA → pregunta precio", "56901000041", [
        ("quiero agendar medicina general", ["09:"]),
        ("ok", {"any": ["Fonasa", "Particular"]}),
        ("1", {"any": ["rut", "RUT"]}),
        ("11111111-1", {"any": ["confirm"]}),
        ("cuánto es", None),
    ])
    mk("A42 CONFIRMING_CITA → hola", "56901000042", [
        ("quiero agendar medicina general", ["09:"]),
        ("ok", {"any": ["Fonasa", "Particular"]}),
        ("1", {"any": ["rut", "RUT"]}),
        ("11111111-1", {"any": ["confirm"]}),
        ("hola", {"any": ["Agendar", "opciones"]}),
    ])
    mk("A43 CONFIRMING_CITA → 'no, tengo urgencia'", "56901000043", [
        ("quiero agendar medicina general", ["09:"]),
        ("ok", {"any": ["Fonasa", "Particular"]}),
        ("1", {"any": ["rut", "RUT"]}),
        ("11111111-1", {"any": ["confirm"]}),
        ("no tengo una urgencia", None),
    ])
    mk("A44 CONFIRMING_CITA → emergencia directa", "56901000044", [
        ("quiero agendar medicina general", ["09:"]),
        ("ok", {"any": ["Fonasa", "Particular"]}),
        ("1", {"any": ["rut", "RUT"]}),
        ("11111111-1", {"any": ["confirm"]}),
        ("me muero", ["SAMU", "131"]),
    ])
    mk("A45 CONFIRMING_CITA → 'ya no, chao'", "56901000045", [
        ("quiero agendar medicina general", ["09:"]),
        ("ok", {"any": ["Fonasa", "Particular"]}),
        ("1", {"any": ["rut", "RUT"]}),
        ("11111111-1", {"any": ["confirm"]}),
        ("ya no, chao", None),
    ])
    mk("A46 CONFIRMING_CITA → texto aleatorio", "56901000046", [
        ("quiero agendar medicina general", ["09:"]),
        ("ok", {"any": ["Fonasa", "Particular"]}),
        ("1", {"any": ["rut", "RUT"]}),
        ("11111111-1", {"any": ["confirm"]}),
        ("asdfgh", None),
    ])
    mk("A47 CONFIRMING_CITA → confirmar (baseline)", "56901000047", [
        ("quiero agendar medicina general", ["09:"]),
        ("ok", {"any": ["Fonasa", "Particular"]}),
        ("1", {"any": ["rut", "RUT"]}),
        ("11111111-1", {"any": ["confirm"]}),
        ("si", {"any": ["reserv", "confirm", "✅"]}),
    ])
    mk("A48 CONFIRMING_CITA → pregunta si hay estacionamiento", "56901000048", [
        ("quiero agendar medicina general", ["09:"]),
        ("ok", {"any": ["Fonasa", "Particular"]}),
        ("1", {"any": ["rut", "RUT"]}),
        ("11111111-1", {"any": ["confirm"]}),
        ("hay estacionamiento", None),
    ])
    mk("A49 CONFIRMING_CITA → 'quiero otra hora'", "56901000049", [
        ("quiero agendar medicina general", ["09:"]),
        ("ok", {"any": ["Fonasa", "Particular"]}),
        ("1", {"any": ["rut", "RUT"]}),
        ("11111111-1", {"any": ["confirm"]}),
        ("quiero otra hora", None),
    ])
    mk("A50 CONFIRMING_CITA → emoji pensante", "56901000050", [
        ("quiero agendar medicina general", ["09:"]),
        ("ok", {"any": ["Fonasa", "Particular"]}),
        ("1", {"any": ["rut", "RUT"]}),
        ("11111111-1", {"any": ["confirm"]}),
        ("🤔", None),
    ])

    # ═════════════════════════════════════════════════════════════════════════
    # GROUP B — RESPUESTAS INCOHERENTES EN IDLE (40 tests)
    # Qué hace el bot con inputs raros cuando está en IDLE.
    # ═════════════════════════════════════════════════════════════════════════

    # B1-B15: texto random / gibberish
    for i, txt in enumerate([
        "asdfghjkl", "xxxxxx", "lorem ipsum", "???", "...",
        "ñññ", "1234567890", "aeiou", "😀😁😂🤣", "🚀🚀🚀",
        "x", "a", "o", "hmm", "eh",
    ], start=1):
        mk(f"B{i:02d} IDLE + gibberish {txt!r}", f"56902{i:06d}", [
            (txt, None),
        ])

    # B16-B25: single words coloquiales chilenos
    for i, txt in enumerate([
        "weon", "cachai", "po", "al tiro", "filo",
        "bacán", "la zorra", "jajaja", "aweonao", "chucha",
    ], start=16):
        mk(f"B{i:02d} IDLE + coloquial {txt!r}", f"56902{i:06d}", [
            (txt, None),
        ])

    # B26-B35: preguntas sueltas
    for i, txt in enumerate([
        "por qué?", "y?", "seguro?", "en serio?", "qué?",
        "cómo así?", "cuál?", "quién?", "dónde?", "cuándo?",
    ], start=26):
        mk(f"B{i:02d} IDLE + pregunta suelta {txt!r}", f"56902{i:06d}", [
            (txt, None),
        ])

    # B36-B40: frases truncadas
    for i, txt in enumerate([
        "quiero", "necesito", "me duele", "tengo", "hola me",
    ], start=36):
        mk(f"B{i:02d} IDLE + frase truncada {txt!r}", f"56902{i:06d}", [
            (txt, None),
        ])

    # ═════════════════════════════════════════════════════════════════════════
    # GROUP C — ADVERSARIAL / EDGE CASES (30 tests)
    # ═════════════════════════════════════════════════════════════════════════

    # C1-C10: inputs muy largos / raros
    mk("C01 texto muy largo repetitivo", "56903000001", [
        ("hola " * 300, None),
    ])
    mk("C02 texto muy largo con emergencia al final", "56903000002", [
        ("bla " * 100 + "me muero", ["SAMU", "131"]),
    ])
    mk("C03 solo espacios", "56903000003", [
        ("     ", None),
    ])
    mk("C04 solo tabs", "56903000004", [
        ("\t\t\t", None),
    ])
    mk("C05 solo newlines", "56903000005", [
        ("\n\n\n", None),
    ])
    mk("C06 mezcla de idiomas", "56903000006", [
        ("hello can you help me please", None),
    ])
    mk("C07 portugues", "56903000007", [
        ("olá preciso de ajuda", None),
    ])
    mk("C08 unicode raro", "56903000008", [
        ("𝓱𝓸𝓵𝓪", None),
    ])
    mk("C09 caracteres especiales", "56903000009", [
        ("@#$%^&*()", None),
    ])
    mk("C10 null byte", "56903000010", [
        ("hola\x00mundo", None),
    ])

    # C11-C20: intentos de inyección / exploit
    mk("C11 SQL injection", "56903000011", [
        ("'; DROP TABLE sessions;--", None),
    ])
    mk("C12 SQL injection 2", "56903000012", [
        ("1' OR '1'='1", None),
    ])
    mk("C13 shell injection", "56903000013", [
        ("$(rm -rf /)", None),
    ])
    mk("C14 xss attempt", "56903000014", [
        ("<script>alert(1)</script>", None),
    ])
    mk("C15 path traversal", "56903000015", [
        ("../../../../etc/passwd", None),
    ])
    mk("C16 template injection", "56903000016", [
        ("{{7*7}}", None),
    ])
    mk("C17 format string", "56903000017", [
        ("%s%s%s%s%s%s%s", None),
    ])
    mk("C18 json injection", "56903000018", [
        ('{"hack":"yes"}', None),
    ])
    mk("C19 unicode zero-width", "56903000019", [
        ("hola\u200bmundo", None),
    ])
    mk("C20 muchos emoji", "56903000020", [
        ("😀" * 200, None),
    ])

    # C21-C30: números raros
    mk("C21 número negativo", "56903000021", [
        ("-1", None),
    ])
    mk("C22 número decimal", "56903000022", [
        ("1.5", None),
    ])
    mk("C23 número enorme", "56903000023", [
        ("99999999999999999999", None),
    ])
    mk("C24 número en otra base", "56903000024", [
        ("0xFF", None),
    ])
    mk("C25 notación científica", "56903000025", [
        ("1e10", None),
    ])
    mk("C26 número con separador", "56903000026", [
        ("1,234", None),
    ])
    mk("C27 'uno'", "56903000027", [
        ("uno", None),
    ])
    mk("C28 'dos'", "56903000028", [
        ("dos", None),
    ])
    mk("C29 'tres'", "56903000029", [
        ("tres", None),
    ])
    mk("C30 '1.', '2.'", "56903000030", [
        ("1.", None),
        ("2.", None),
    ])

    # ═════════════════════════════════════════════════════════════════════════
    # GROUP D — MULTI-FLOW / RE-ENTRADA (30 tests)
    # El paciente completa o aborta un flujo, y después empieza otro.
    # ═════════════════════════════════════════════════════════════════════════

    mk("D01 agendar → después preguntar precio", "56904000001", [
        ("quiero agendar medicina general", ["09:"]),
        ("ok", {"any": ["Fonasa", "Particular"]}),
        ("1", {"any": ["rut", "RUT"]}),
        ("11111111-1", {"any": ["confirm"]}),
        ("si", {"any": ["reserv", "confirm", "✅"]}),
        ("cuánto cuesta una tapadura", None),
    ])
    mk("D02 cancelar → después agendar otra", "56904000002", [
        ("cancelar", {"any": ["rut", "RUT"]}),
        ("11111111-1", None),
        ("no", None),
        ("quiero agendar kinesiología", None),
    ], setup=setup_una_cita_generic)
    mk("D03 ver reservas → agendar nueva", "56904000003", [
        ("mis horas", {"any": ["rut", "RUT"]}),
        ("11111111-1", None),
        ("quiero agendar una", None),
    ], setup=setup_una_cita_generic)
    mk("D04 emergencia → agendar después", "56904000004", [
        ("me muero", ["SAMU", "131"]),
        ("ya estoy mejor, quiero agendar", None),
    ])
    mk("D05 FAQ → agendar", "56904000005", [
        ("atienden fonasa", None),
        ("quiero agendar", {"any": ["área", "categoría", "categoria", "Agendar", "ok"]}),
    ])
    mk("D06 menu varias veces", "56904000006", [
        ("menu", {"any": ["Agendar"]}),
        ("menu", {"any": ["Agendar"]}),
        ("menu", {"any": ["Agendar"]}),
    ])
    mk("D07 hola → menu → agendar", "56904000007", [
        ("hola", {"any": ["Agendar"]}),
        ("menu", {"any": ["Agendar"]}),
        ("1", None),
    ])
    mk("D08 agendar abort → otra vez agendar", "56904000008", [
        ("quiero agendar medicina general", ["09:"]),
        ("menu", {"any": ["Agendar"]}),
        ("quiero agendar dental", None),
    ])
    mk("D09 cambiar intención 3 veces", "56904000009", [
        ("quiero agendar", {"any": ["especialidad", "Medicina", "Agendar"]}),
        ("mejor cancelar", None),
        ("en realidad ver mis horas", None),
    ], setup=setup_una_cita_generic)
    mk("D10 flujo completo sin errores", "56904000010", [
        ("hola", {"any": ["Agendar"]}),
        ("1", None),
        ("cat_medico", None),
        ("esp_medgen", None),
        ("1", None),
        ("1", None),
        ("11111111-1", None),
        ("si", {"any": ["reserv", "confirm", "✅"]}),
    ])
    mk("D11 usar atajos numéricos consecutivos", "56904000011", [
        ("1", None),
        ("menu", None),
        ("2", None),
        ("menu", None),
        ("3", None),
        ("menu", None),
        ("4", None),
    ])
    mk("D12 ver horas sin citas", "56904000012", [
        ("ver mis horas", {"any": ["rut", "RUT"]}),
        ("11111111-1", None),
    ])
    mk("D13 atajo 5 lista espera", "56904000013", [
        ("5", None),
    ])
    mk("D14 atajo 6 recepción", "56904000014", [
        ("6", None),
    ])
    mk("D15 humano → luego agendar", "56904000015", [
        ("quiero hablar con alguien", {"any": ["recepción", "recepcion"]}),
        ("mejor quiero agendar medicina general", None),
    ])
    mk("D16 humano → luego precio", "56904000016", [
        ("quiero hablar con alguien", {"any": ["recepción", "recepcion"]}),
        ("cuánto cuesta una ecografía", None),
    ])
    mk("D17 humano → emergencia", "56904000017", [
        ("quiero hablar con alguien", {"any": ["recepción", "recepcion"]}),
        ("me muero", ["SAMU", "131"]),
    ])
    mk("D18 humano → hola", "56904000018", [
        ("quiero hablar con alguien", {"any": ["recepción", "recepcion"]}),
        ("hola", {"any": ["Agendar"]}),
    ])
    mk("D19 humano → mensaje clínico", "56904000019", [
        ("quiero hablar con alguien", {"any": ["recepción", "recepcion"]}),
        ("tengo diabetes", {"any": ["SAMU", "urgente", "📞"], "none": ["Recibido 🙏"]}),
    ])
    mk("D20 cancelar abort → agendar", "56904000020", [
        ("cancelar", {"any": ["rut", "RUT"]}),
        ("menu", {"any": ["Agendar"]}),
        ("quiero agendar medicina general", None),
    ])
    mk("D21 waitlist → agendar", "56904000021", [
        ("5", None),
        ("menu", {"any": ["Agendar"]}),
        ("quiero agendar", None),
    ])
    mk("D22 varias FAQs seguidas", "56904000022", [
        ("atienden fonasa", None),
        ("dónde están", None),
        ("tienen estacionamiento", None),
    ])
    mk("D23 reagendar abort → cancelar", "56904000023", [
        ("reagendar", {"any": ["rut", "RUT"]}),
        ("menu", {"any": ["Agendar"]}),
        ("cancelar", {"any": ["rut", "RUT"]}),
    ], setup=setup_una_cita_generic)
    mk("D24 flujo agendar dental completo", "56904000024", [
        ("quiero agendar odontología", None),
        ("1", None),
        ("1", None),
        ("11111111-1", None),
        ("si", {"any": ["reserv", "confirm", "✅"]}),
    ])
    mk("D25 agendar kine → cambiar a psico mid", "56904000025", [
        ("quiero agendar kinesiología", None),
        ("mejor psicología", None),
    ])
    mk("D26 FAQ ecografia → agendar", "56904000026", [
        ("cuánto cuesta una ecografía", None),
        ("si quiero agendar", None),
    ])
    mk("D27 emergencia → precio → agendar", "56904000027", [
        ("me duele fuerte el pecho", ["SAMU", "131"]),
        ("cuánto cuesta", None),
        ("quiero agendar", None),
    ])
    mk("D28 flujo waitlist completo", "56904000028", [
        ("ponme en lista de espera para kine", None),
    ])
    mk("D29 flujo reagendar completo", "56904000029", [
        ("quiero reagendar", {"any": ["rut", "RUT"]}),
        ("11111111-1", None),
    ], setup=setup_una_cita_generic)
    mk("D30 ver reservas → cancelar directo", "56904000030", [
        ("ver mis horas", {"any": ["rut", "RUT"]}),
        ("11111111-1", None),
        ("cancelar", None),
    ], setup=setup_una_cita_generic)

    # ═════════════════════════════════════════════════════════════════════════
    # GROUP E — AMBIGÜEDAD DE RESPUESTAS (30 tests)
    # ═════════════════════════════════════════════════════════════════════════

    mk("E01 'si no sé' ambiguo", "56905000001", [
        ("quiero agendar", None),
        ("si no sé", None),
    ])
    mk("E02 'no si quiero'", "56905000002", [
        ("quiero agendar medicina general", ["09:"]),
        ("no si quiero", None),
    ])
    mk("E03 'tal vez'", "56905000003", [
        ("quiero agendar medicina general", ["09:"]),
        ("tal vez", None),
    ])
    mk("E04 'depende'", "56905000004", [
        ("quiero agendar medicina general", ["09:"]),
        ("depende", None),
    ])
    mk("E05 'quizás'", "56905000005", [
        ("quiero agendar medicina general", ["09:"]),
        ("quizás", None),
    ])
    mk("E06 'eh no sé'", "56905000006", [
        ("quiero agendar medicina general", ["09:"]),
        ("eh no sé", None),
    ])
    mk("E07 'mmm'", "56905000007", [
        ("quiero agendar medicina general", ["09:"]),
        ("mmm", None),
    ])
    mk("E08 'ok pero después'", "56905000008", [
        ("quiero agendar medicina general", ["09:"]),
        ("ok pero después", None),
    ])
    mk("E09 'si eso'", "56905000009", [
        ("quiero agendar medicina general", ["09:"]),
        ("si eso", None),
    ])
    mk("E10 'esque no...'", "56905000010", [
        ("quiero agendar medicina general", ["09:"]),
        ("esque no...", None),
    ])
    mk("E11 'deja voy a ver'", "56905000011", [
        ("quiero agendar medicina general", ["09:"]),
        ("deja voy a ver", None),
    ])
    mk("E12 'a ver'", "56905000012", [
        ("quiero agendar medicina general", ["09:"]),
        ("a ver", None),
    ])
    mk("E13 'un momento'", "56905000013", [
        ("quiero agendar medicina general", ["09:"]),
        ("un momento", None),
    ])
    mk("E14 'ya veré'", "56905000014", [
        ("quiero agendar medicina general", ["09:"]),
        ("ya veré", None),
    ])
    mk("E15 'espera'", "56905000015", [
        ("quiero agendar medicina general", ["09:"]),
        ("espera", None),
    ])
    mk("E16 negacion con typo 'np'", "56905000016", [
        ("quiero agendar medicina general", ["09:"]),
        ("np", None),
    ])
    mk("E17 afirmacion con typo 'sipo'", "56905000017", [
        ("quiero agendar medicina general", ["09:"]),
        ("sipo", None),
    ])
    mk("E18 afirmacion con typo 'siii'", "56905000018", [
        ("quiero agendar medicina general", ["09:"]),
        ("siii", None),
    ])
    mk("E19 'dale po'", "56905000019", [
        ("quiero agendar medicina general", ["09:"]),
        ("dale po", None),
    ])
    mk("E20 'buenas'", "56905000020", [
        ("buenas", None),
    ])
    mk("E21 'hola q tal'", "56905000021", [
        ("hola q tal", None),
    ])
    mk("E22 'tenga buenas'", "56905000022", [
        ("tenga buenas", None),
    ])
    mk("E23 'holi'", "56905000023", [
        ("holi", None),
    ])
    mk("E24 'good morning'", "56905000024", [
        ("good morning", None),
    ])
    mk("E25 'hola hola'", "56905000025", [
        ("hola hola hola", None),
    ])
    mk("E26 'OK' mayusculas", "56905000026", [
        ("quiero agendar medicina general", ["09:"]),
        ("OK", None),
    ])
    mk("E27 'SI' mayusculas", "56905000027", [
        ("quiero agendar medicina general", ["09:"]),
        ("SI", None),
    ])
    mk("E28 'NO' mayusculas", "56905000028", [
        ("quiero agendar medicina general", ["09:"]),
        ("NO", None),
    ])
    mk("E29 'YES' inglés", "56905000029", [
        ("quiero agendar medicina general", ["09:"]),
        ("YES", None),
    ])
    mk("E30 pregunta sobre el bot", "56905000030", [
        ("eres un robot?", None),
    ])

    # ═════════════════════════════════════════════════════════════════════════
    # GROUP F — ESCENARIOS CLÍNICOS COMUNES (30 tests)
    # ═════════════════════════════════════════════════════════════════════════

    # F1-F10: síntomas coloquiales chilenos
    for i, (txt, expect) in enumerate([
        ("me duele la guata", None),
        ("tengo cototo en la rodilla", None),
        ("me agarró un empacho", None),
        ("tengo rasquiña en la espalda", None),
        ("me vino un gripazo", None),
        ("tengo unos pitos en el oído", None),
        ("me arde al hacer pipí", None),
        ("se me bajó la presión", None),
        ("tengo una culebrilla", None),
        ("me sacaron una muela y me duele", None),
    ], start=1):
        mk(f"F{i:02d} síntoma coloquial {txt!r}", f"56906{i:06d}", [
            (txt, expect),
        ])

    # F11-F20: síntomas con abreviaciones WhatsApp
    for i, (txt, expect) in enumerate([
        ("tngo dlor de kbza", None),
        ("me duele muxo el pxo", None),
        ("xq me duele tanto el estomago", None),
        ("tngo feber alta", None),
        ("stoy con diarea", None),
        ("no pdo dormir x el dlor", None),
        ("tngo gripa fuerte", None),
        ("mi hja tiene tos", None),
        ("tngo un grano en la espalda", None),
        ("me sangran las encias", None),
    ], start=11):
        mk(f"F{i:02d} abreviación {txt!r}", f"56906{i:06d}", [
            (txt, expect),
        ])

    # F21-F30: preguntas clínicas frecuentes
    for i, txt in enumerate([
        "qué es una tapadura",
        "cuánto cuesta una endodoncia",
        "tienen kinesiólogo",
        "atienden niños",
        "hacen papanicolau",
        "dan licencia médica",
        "tienen ginecólogo",
        "hacen ecografías",
        "atienden en la tarde",
        "tienen pediatra",
    ], start=21):
        mk(f"F{i:02d} pregunta clínica {txt!r}", f"56906{i:06d}", [
            (txt, None),
        ])

    # ═════════════════════════════════════════════════════════════════════════
    # GROUP G — EMERGENCIAS (20 tests)
    # ═════════════════════════════════════════════════════════════════════════

    # G1-G10: emergencias físicas con variantes
    for i, (txt, expect) in enumerate([
        ("me muero", ["SAMU", "131"]),
        ("creo que me muero", ["SAMU", "131"]),
        ("me voy a morir", ["SAMU", "131"]),
        ("me estoy muriendo", ["SAMU", "131"]),
        ("me duele mucho el pecho", ["SAMU", "131"]),
        ("no puedo respirar", ["SAMU", "131"]),
        ("me ahogo", ["SAMU", "131"]),
        ("sangrado abundante", ["SAMU", "131"]),
        ("me picó una araña de rincón", ["SAMU", "131"]),
        ("convulsión", ["SAMU", "131"]),
    ], start=1):
        mk(f"G{i:02d} emergencia {txt!r}", f"56907{i:06d}", [
            (txt, expect),
        ])

    # G11-G15: coloquialismos que NO son emergencia (no deben disparar SAMU)
    for i, txt in enumerate([
        "me muero de hambre",
        "me muero de risa con este chiste",
        "me muero de sed",
        "me voy a morir de ganas de ir",
        "me muero de sueño",
    ], start=11):
        mk(f"G{i:02d} coloquialismo NO emergencia {txt!r}", f"56907{i:06d}", [
            (txt, {"none": ["SAMU", "urgenc"]}),
        ])

    # G16-G20: salud mental (respuesta diferenciada)
    for i, txt in enumerate([
        "me quiero matar",
        "quiero suicidarme",
        "no quiero seguir viviendo",
        "quiero acabar con mi vida",
        "me quiero morir",
    ], start=16):
        mk(f"G{i:02d} salud mental {txt!r}", f"56907{i:06d}", [
            (txt, {"all": ["Salud Responde"]}),
        ])

    # ── Run ─────────────────────────────────────────────────────────────────
    passed = 0
    failed = 0
    bugs_by_group: dict[str, int] = {}
    for name, phone, steps, setup in results:
        ok = await run_convo(name, phone, steps, setup)
        group = name.split(" ")[0][:3]
        if not ok:
            bugs_by_group[group] = bugs_by_group.get(group, 0) + 1
        mark = "✅" if ok else "❌"
        print(f"{mark} {name}")
        if ok:
            passed += 1
        else:
            failed += 1

    print()
    print(f"── Total: {passed}/{len(results)} passed, {failed} failed ──")
    print()
    if bugs_by_group:
        print("── Bugs por grupo ──")
        for g, n in sorted(bugs_by_group.items()):
            print(f"  {g}: {n}")
        print()
    if BUGS:
        print(f"── BUGS / ISSUES ENCONTRADOS ({len(BUGS)}) ──")
        for i, b in enumerate(BUGS, 1):
            print(f"\n[{i}] {b['test']} — step {b['step']}")
            print(f"    input: {b['input']!r}")
            print(f"    error: {b['error']}")
            if "got" in b:
                print(f"    got: {b['got'][:300]}")
            if "traceback" in b:
                tb_lines = b["traceback"].strip().split("\n")
                print(f"    traceback (tail):")
                for line in tb_lines[-3:]:
                    print(f"      {line}")
    return failed


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(0 if exit_code == 0 else 1)
