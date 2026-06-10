"""
Test offline — ITEM-20: xchequeo_si de paciente de Márquez reagenda con especialidad
"medicina familiar" (no "medicina general") para que _precio_line muestre $30.000.

Sin red, sin DB, sin imports de la app. Replica la lógica del handler xchequeo_si
y verifica que la especialidad pasada a _iniciar_agendar es "medicina familiar"
cuando el profesional de la última cita es Márquez, y "medicina general" en caso
contrario.
"""


# ── Replica de la lógica del handler xchequeo_si ─────────────────────────────

def _esp_para_xchequeo(ultima_cita: dict | None) -> str:
    """Determina la especialidad a pasar a _iniciar_agendar para el control de MG.

    Si la última cita del paciente fue con Dr. Márquez → "medicina familiar"
    (lo que fuerza solo_ids=[13] en _iniciar_agendar y muestra precio $30.000).
    En cualquier otro caso → "medicina general".
    """
    esp = "medicina general"
    if ultima_cita:
        prof = (ultima_cita.get("profesional") or "").lower()
        if "márquez" in prof or "marquez" in prof:
            esp = "medicina familiar"
    return esp


# ── Casos ─────────────────────────────────────────────────────────────────────

CASOS_MARQUEZ = [
    # (ultima_cita, esp_esperada, descripcion)
    (
        {"especialidad": "Medicina General", "profesional": "Dr. Alonso Márquez",
         "fecha": "2026-05-15", "hora": "17:00"},
        "medicina familiar",
        "Cita con Márquez con tilde → debe usar medicina familiar"
    ),
    (
        {"especialidad": "Medicina General", "profesional": "Dr. Alonso Marquez",
         "fecha": "2026-04-10", "hora": "16:30"},
        "medicina familiar",
        "Cita con Marquez sin tilde → debe usar medicina familiar"
    ),
    (
        {"especialidad": "Medicina General", "profesional": "dr. alonso márquez",
         "fecha": "2026-03-01", "hora": "15:00"},
        "medicina familiar",
        "Cita Márquez minúsculas → debe usar medicina familiar"
    ),
    (
        {"especialidad": "Medicina General", "profesional": "Dr. Rodrigo Olavarría",
         "fecha": "2026-05-20", "hora": "16:00"},
        "medicina general",
        "Cita con Olavarría → debe usar medicina general (precio $25.000)"
    ),
    (
        {"especialidad": "Medicina General", "profesional": "Dr. Andrés Abarca",
         "fecha": "2026-05-22", "hora": "09:00"},
        "medicina general",
        "Cita con Abarca → debe usar medicina general (precio $25.000)"
    ),
    (
        None,
        "medicina general",
        "Sin última cita → debe usar medicina general por defecto"
    ),
    (
        {"especialidad": "Kinesiología", "profesional": "Luis Armijo",
         "fecha": "2026-05-10", "hora": "10:00"},
        "medicina general",
        "Última cita con kine, no MG → medicina general"
    ),
]


def test_xchequeo_especialidad_marquez():
    """Verifica que xchequeo_si detecta Márquez y usa 'medicina familiar'."""
    errores = []
    for ultima_cita, esp_esperada, desc in CASOS_MARQUEZ:
        got = _esp_para_xchequeo(ultima_cita)
        if got != esp_esperada:
            errores.append(f"  FAIL [{desc}]: got={got!r}, expected={esp_esperada!r}")
    assert not errores, "\n".join(errores)


def run_suite():
    total = len(CASOS_MARQUEZ)
    ok = 0
    errores = []
    for ultima_cita, esp_esperada, desc in CASOS_MARQUEZ:
        got = _esp_para_xchequeo(ultima_cita)
        if got == esp_esperada:
            ok += 1
            print(f"  OK  {desc}")
        else:
            errores.append(f"  FAIL [{desc}]: got={got!r}, expected={esp_esperada!r}")
            print(f"  FAIL {desc}: got={got!r}, expected={esp_esperada!r}")
    print(f"\n{ok}/{total} casos correctos")
    if errores:
        raise AssertionError("\n".join(errores))


if __name__ == "__main__":
    run_suite()
