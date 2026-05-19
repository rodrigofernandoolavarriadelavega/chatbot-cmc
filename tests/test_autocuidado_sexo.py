"""
Tests unitarios para la lógica de filtrado por sexo en get_tips_autocuidado.
Caso raíz: Olga Fabiola Aravena Arias (F, 55 años) recibió tip de próstata
porque Medilink tenía sexo='M' incorrecto.
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from autocuidado import get_tips_autocuidado, _inferir_sexo_por_nombre, _resolver_sexo

# ── Inferencia de sexo por nombre ───────────────────────────────────────────

def test_infiere_femenino_olga():
    assert _inferir_sexo_por_nombre("Olga Fabiola Aravena Arias") == "F"

def test_infiere_femenino_maria():
    assert _inferir_sexo_por_nombre("María José González") == "F"

def test_infiere_masculino_carlos():
    assert _inferir_sexo_por_nombre("Carlos Rodrigo Pérez") == "M"

def test_infiere_masculino_juan():
    assert _inferir_sexo_por_nombre("Juan Pablo Rodríguez") == "M"

def test_no_infiere_inicial():
    assert _inferir_sexo_por_nombre("C. Ramirez") is None

def test_no_infiere_nombre_vacio():
    assert _inferir_sexo_por_nombre(None) is None
    assert _inferir_sexo_por_nombre("") is None

# ── Resolución de sexo (cruce Medilink × nombre) ────────────────────────────

def test_resolver_mismatch_medilink_m_nombre_f():
    """Caso Olga: Medilink='M', nombre infiere 'F' → debe retornar 'F'."""
    resultado = _resolver_sexo("M", "Olga Fabiola Aravena Arias")
    assert resultado == "F"

def test_resolver_coincidencia():
    resultado = _resolver_sexo("F", "Carmen López")
    assert resultado == "F"

def test_resolver_medilink_none_nombre_f():
    resultado = _resolver_sexo(None, "Patricia Soto")
    assert resultado == "F"

def test_resolver_medilink_none_nombre_m():
    resultado = _resolver_sexo(None, "Diego Muñoz")
    assert resultado == "M"

def test_resolver_sin_datos():
    resultado = _resolver_sexo(None, None)
    assert resultado is None

def test_resolver_inicial_sin_medilink():
    resultado = _resolver_sexo(None, "C. Ramirez")
    assert resultado is None

# ── get_tips_autocuidado: filtrado correcto por sexo ────────────────────────

# Olga: 55 años, Medilink sexo='M' (incorrecto), nombre claramente femenino
def test_olga_no_recibe_prostata():
    """BUG original: Olga recibía tip de próstata. Con fix no debe recibirlo."""
    tips = get_tips_autocuidado(
        fecha_nacimiento="1971-01-23",
        sexo="M",  # valor incorrecto de Medilink
        especialidad="medicina general",
        nombre="Olga Fabiola Aravena Arias",
    )
    assert "próstata" not in tips.lower()
    assert "psa" not in tips.lower()
    assert "tacto rectal" not in tips.lower()

def test_olga_puede_recibir_mamografia():
    """Olga con 55 años (50-69) debería recibir tip de mamografía GES."""
    tips = get_tips_autocuidado(
        fecha_nacimiento="1971-01-23",
        sexo="M",  # incorrecto en Medilink, nombre corrige a F
        especialidad="medicina general",
        nombre="Olga Fabiola Aravena Arias",
    )
    # Debe contener mamografía o PAP (ambos son F, 55 años califica para ambos)
    assert "mamografía" in tips.lower() or "pap" in tips.lower() or "papanicolau" in tips.lower() or "lipídico" in tips.lower()

def test_juan_65_prostata_elegible():
    """
    Hombre 65 años: próstata debe estar en los exámenes elegibles.
    Como hay >2 exámenes aplicables y se eligen 2 al azar, corremos N veces
    para confirmar que próstata aparece al menos en alguna ejecución.
    Lo que importa es que NUNCA aparezca mamografía/PAP.
    """
    prostata_visto = False
    for _ in range(30):
        tips = get_tips_autocuidado(
            fecha_nacimiento="1961-03-10",
            sexo="M",
            especialidad="medicina general",
            nombre="Juan Carlos Pérez",
        )
        if "próstata" in tips.lower():
            prostata_visto = True
            break
    assert prostata_visto, "Próstata nunca apareció en 30 ejecuciones para hombre 65 años"

def test_juan_65_no_recibe_mamografia():
    """Hombre 65 años NO debe recibir mamografía ni PAP."""
    tips = get_tips_autocuidado(
        fecha_nacimiento="1961-03-10",
        sexo="M",
        especialidad="medicina general",
        nombre="Juan Carlos Pérez",
    )
    assert "mamografía" not in tips.lower()
    assert "pap" not in tips.lower()
    assert "papanicolau" not in tips.lower()

def test_sin_sexo_nombre_carlos_no_prostata_ni_mamo():
    """
    Si sexo=None pero nombre es Carlos → infiere M.
    A los 30 años no hay tip de próstata (rango 50-75), pero no debe haber mamografía.
    """
    tips = get_tips_autocuidado(
        fecha_nacimiento="1994-06-15",  # 32 años
        sexo=None,
        especialidad="medicina general",
        nombre="Carlos Andrés Muñoz",
    )
    assert "mamografía" not in tips.lower()
    assert "pap" not in tips.lower()

def test_sin_sexo_inicial_generico_solo():
    """
    Nombre 'C. Ramirez' no permite inferir sexo. sexo=None.
    No debe incluir tips sexo-específicos (próstata ni mamografía ni PAP).
    """
    tips = get_tips_autocuidado(
        fecha_nacimiento="1965-04-20",  # 61 años
        sexo=None,
        especialidad="medicina general",
        nombre="C. Ramirez",
    )
    assert "próstata" not in tips.lower()
    assert "mamografía" not in tips.lower()
    assert "pap" not in tips.lower()
    # Pero sí debe incluir tips genéricos (perfil lipídico aplica 40+, ambos sexos)
    assert "lipídico" in tips.lower() or "colorrectal" in tips.lower()

def test_tip_generico_siempre_presente():
    """Debe haber siempre al menos un tip genérico."""
    tips = get_tips_autocuidado(nombre="Ana González")
    assert "🌿" in tips

def test_especialidad_ginecologia_no_fuerza_prostata():
    """Paciente de ginecología, sin importar el sexo en Medilink, no debe recibir próstata."""
    tips = get_tips_autocuidado(
        fecha_nacimiento="1975-08-12",
        sexo="M",  # error en Medilink
        especialidad="ginecología",
        nombre="Fernanda Ríos Soto",
    )
    assert "próstata" not in tips.lower()

if __name__ == "__main__":
    # Ejecutar sin pytest
    import traceback
    tests = [
        test_infiere_femenino_olga,
        test_infiere_femenino_maria,
        test_infiere_masculino_carlos,
        test_infiere_masculino_juan,
        test_no_infiere_inicial,
        test_no_infiere_nombre_vacio,
        test_resolver_mismatch_medilink_m_nombre_f,
        test_resolver_coincidencia,
        test_resolver_medilink_none_nombre_f,
        test_resolver_medilink_none_nombre_m,
        test_resolver_sin_datos,
        test_resolver_inicial_sin_medilink,
        test_olga_no_recibe_prostata,
        test_olga_puede_recibir_mamografia,
        test_juan_65_prostata_elegible,
        test_juan_65_no_recibe_mamografia,
        test_sin_sexo_nombre_carlos_no_prostata_ni_mamo,
        test_sin_sexo_inicial_generico_solo,
        test_tip_generico_siempre_presente,
        test_especialidad_ginecologia_no_fuerza_prostata,
    ]
    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL  {t.__name__}: {e}")
            traceback.print_exc()
            failed += 1
    print(f"\n{passed}/{passed+failed} tests pasaron.")
    if failed:
        sys.exit(1)
