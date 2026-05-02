"""
FIX-5: Test que SYSTEM_PROMPT y FAQ local no contengan el número personal
del Dr. Olavarría (+56987834148). También verifica _final_phone_guard.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))

PERSONAL = "987834148"


def test_system_prompt_clean():
    from claude_helper import SYSTEM_PROMPT
    assert PERSONAL not in SYSTEM_PROMPT, (
        f"SYSTEM_PROMPT contiene el número personal: {PERSONAL}"
    )


def test_faq_local_clean():
    from claude_helper import _FAQ_LOCAL_FALLBACKS
    for keywords, respuesta in _FAQ_LOCAL_FALLBACKS:
        assert PERSONAL not in respuesta, (
            f"_FAQ_LOCAL contiene el número personal en entrada {keywords}"
        )


def test_final_phone_guard():
    from messaging import _final_phone_guard
    texto_con_leak = f"Llama al +56{PERSONAL} para más info."
    resultado = _final_phone_guard(texto_con_leak)
    assert PERSONAL not in resultado, (
        f"_final_phone_guard no limpió el número: {resultado}"
    )


def test_scrub_telefonos():
    from claude_helper import _scrub_telefonos
    texto = f"El número es +56{PERSONAL}."
    resultado = _scrub_telefonos(texto)
    assert PERSONAL not in resultado, (
        f"_scrub_telefonos no limpió el número: {resultado}"
    )


if __name__ == "__main__":
    test_system_prompt_clean()
    test_faq_local_clean()
    test_final_phone_guard()
    test_scrub_telefonos()
    print("✓ Todos los tests de FIX-5 pasaron (número personal no expuesto).")
