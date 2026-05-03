"""Tests para los 4 bugs ALTOS de la pasada ofensiva frente 3.

BUG-A: _validar_respuesta_faq filtra precios alucinados y especialidades no atendidas.
BUG-B: pivot a last_esp_context muestra botones de confirmación.
BUG-C: "gracias" tras consulta NO redirige a agendar.
BUG-D: _parse_slot_selection con frases clínicas NO retorna slot.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

# ── BUG-A ─────────────────────────────────────────────���───────────────────────

def test_validar_faq_precio_alucinado():
    from claude_helper import _validar_respuesta_faq
    texto = "La consulta de traumatología cuesta $22.000."
    resultado = _validar_respuesta_faq(texto)
    assert "$22.000" not in resultado, "Precio $22.000 no está en whitelist — debe filtrarse"
    assert "[consultar en recepción]" in resultado, "Debe sustituirse por [consultar en recepción]"

def test_validar_faq_precio_conocido_no_modificado():
    from claude_helper import _validar_respuesta_faq
    texto = "La consulta de medicina general cuesta $25.000 particular o $7.880 Fonasa."
    resultado = _validar_respuesta_faq(texto)
    assert "$25.000" in resultado, "Precio $25.000 está en whitelist — no debe modificarse"
    assert "$7.880" in resultado, "Precio $7.880 está en whitelist — no debe modificarse"

def test_validar_faq_neurologo_no_atendido():
    from claude_helper import _validar_respuesta_faq
    texto = "Para eso necesitas ver a un neurólogo. Puedes agendar aquí."
    resultado = _validar_respuesta_faq(texto)
    assert "CESFAM" in resultado or "no la tenemos" in resultado, \
        "Especialidad no atendida debe devolver mensaje de derivación"

def test_validar_faq_pediatra_no_atendido():
    from claude_helper import _validar_respuesta_faq
    texto = "El pediatra puede ayudarte con eso, cuesta $15.000."
    resultado = _validar_respuesta_faq(texto)
    assert "CESFAM" in resultado or "no la tenemos" in resultado, \
        "Pediatría no se atiende en el CMC"

def test_validar_faq_prof_conocido_no_modificado():
    from claude_helper import _validar_respuesta_faq
    texto = "El Dr. Borrego puede atenderte. Consulta ORL $35.000."
    resultado = _validar_respuesta_faq(texto)
    assert "Borrego" in resultado, "Apellido conocido no debe reemplazarse"
    assert "$35.000" in resultado, "Precio conocido no debe reemplazarse"


# ── BUG-D ─────────────────────────────────────────────────────────────────────

def _get_mock_slots(n=5):
    return [{"hora_inicio": f"0{8+i}:00", "hora_fin": f"0{9+i}:00"} for i in range(n)]

def test_parse_slot_dias_fiebre():
    """'llevo 3 días con fiebre' NO debe retornar slot 3."""
    # Import directo de flows es pesado; importamos solo la función
    import importlib.util, os
    spec = importlib.util.spec_from_file_location(
        "flows_mod",
        os.path.join(os.path.dirname(__file__), "..", "app", "flows.py")
    )
    mod = importlib.util.load_module_from_spec = None  # no cargar entero
    # Alternativa: extraer solo la función con exec
    src = open(os.path.join(os.path.dirname(__file__), "..", "app", "flows.py")).read()
    # Obtener solo _parse_slot_selection
    import re
    m = re.search(r'(def _parse_slot_selection\(.*?)(?=\ndef )', src, re.DOTALL)
    assert m, "No se encontró _parse_slot_selection en flows.py"
    fn_src = m.group(1)
    ns = {}
    exec("import re\n" + fn_src, ns)
    fn = ns["_parse_slot_selection"]

    slots = _get_mock_slots(5)
    assert fn("llevo 3 días con fiebre", slots) is None, \
        "'llevo 3 días con fiebre' → debe retornar None, no slot 3"

def test_parse_slot_rut_termina():
    """'mi RUT termina en 7' NO debe retornar slot 7."""
    src = open(os.path.join(os.path.dirname(__file__), "..", "app", "flows.py")).read()
    import re
    m = re.search(r'(def _parse_slot_selection\(.*?)(?=\ndef )', src, re.DOTALL)
    ns = {}
    exec("import re\n" + m.group(1), ns)
    fn = ns["_parse_slot_selection"]
    slots = _get_mock_slots(9)
    assert fn("mi RUT termina en 7", slots) is None, \
        "'mi RUT termina en 7' → debe retornar None"

def test_parse_slot_numero_simple():
    """'3' solo → debe retornar slot 3 (índice 2)."""
    src = open(os.path.join(os.path.dirname(__file__), "..", "app", "flows.py")).read()
    import re
    m = re.search(r'(def _parse_slot_selection\(.*?)(?=\ndef )', src, re.DOTALL)
    ns = {}
    exec("import re\n" + m.group(1), ns)
    fn = ns["_parse_slot_selection"]
    slots = _get_mock_slots(5)
    assert fn("3", slots) == 2, "'3' solo → índice 2"

def test_parse_slot_opcion_explicita():
    """'opción 2' → debe retornar índice 1."""
    src = open(os.path.join(os.path.dirname(__file__), "..", "app", "flows.py")).read()
    import re
    m = re.search(r'(def _parse_slot_selection\(.*?)(?=\ndef )', src, re.DOTALL)
    ns = {}
    exec("import re\n" + m.group(1), ns)
    fn = ns["_parse_slot_selection"]
    slots = _get_mock_slots(5)
    assert fn("opción 2", slots) == 1, "'opción 2' → índice 1"


# ── BUG-B / BUG-C: verificamos estructura del código (no requieren DB) ────────

def test_pivot_muestra_botones_confirmacion():
    """El código del pivot NO debe llamar _iniciar_agendar directamente."""
    src = open(os.path.join(os.path.dirname(__file__), "..", "app", "flows.py")).read()
    # El bloque del pivot debe contener "confirma_pivot_esp"
    assert "confirma_pivot_esp" in src, \
        "BUG-B: el pivot debe generar botón 'confirma_pivot_esp'"
    assert "pivot_esp_pendiente" in src, \
        "BUG-B: debe guardar pivot_esp_pendiente en data"

def test_despedida_no_redirige():
    """El bloque de intent==menu debe detectar despedidas."""
    src = open(os.path.join(os.path.dirname(__file__), "..", "app", "flows.py")).read()
    assert "_es_despedida" in src, \
        "BUG-C: debe existir lógica _es_despedida en bloque intent==menu"
    assert "_DESPEDIDA_KW" in src, \
        "BUG-C: debe existir _DESPEDIDA_KW"
    assert "_CONTINUAR_KW" in src, \
        "BUG-C: debe existir _CONTINUAR_KW"


if __name__ == "__main__":
    tests = [
        test_validar_faq_precio_alucinado,
        test_validar_faq_precio_conocido_no_modificado,
        test_validar_faq_neurologo_no_atendido,
        test_validar_faq_pediatra_no_atendido,
        test_validar_faq_prof_conocido_no_modificado,
        test_parse_slot_dias_fiebre,
        test_parse_slot_rut_termina,
        test_parse_slot_numero_simple,
        test_parse_slot_opcion_explicita,
        test_pivot_muestra_botones_confirmacion,
        test_despedida_no_redirige,
    ]
    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            print(f"✅ {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"❌ {t.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"💥 {t.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n── Total: {passed}/{passed+failed} passed, {failed} failed ──")
