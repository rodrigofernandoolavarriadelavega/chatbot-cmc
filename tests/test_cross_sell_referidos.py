"""
Tests unitarios para Feature A (cross-sell interactivo) y Feature B (referral bonos).
No requieren API Medilink ni variables de entorno externas.
"""
import sys
import os
import tempfile
import sqlite3
import pathlib
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))

# ── Fixtures: parchear DB_PATH para usar una DB temporal ─────────────────────

_DB_PATH = None
_session_module = None


def _setup_test_db():
    """Crea una DB temporal y parchea session.DB_PATH antes de usarla."""
    global _DB_PATH, _session_module
    tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    _DB_PATH = tmp.name
    tmp.close()
    import session as _session
    _session.DB_PATH = pathlib.Path(_DB_PATH)
    _session_module = _session
    # Forzar primera conexión para crear tablas
    with _session._conn():
        pass
    return _DB_PATH


# ══════════════════════════════════════════════════════════════════════════════
# Feature A — Cross-sell cooldown
# ══════════════════════════════════════════════════════════════════════════════

class TestCrossSellCooldown(unittest.TestCase):

    def setUp(self):
        _setup_test_db()
        import session
        self.session = session

    def tearDown(self):
        if _DB_PATH and os.path.exists(_DB_PATH):
            os.unlink(_DB_PATH)

    def test_primera_vez_permite_cross_sell(self):
        """Sin historial, puede_cross_sell debe retornar True."""
        ok = self.session.puede_cross_sell(
            "56912345678", "Ginecología", "Ecografía"
        )
        self.assertTrue(ok)

    def test_cooldown_mismo_dia(self):
        """Después de ofrecer un cross-sell hoy, no debe ofrecer otro el mismo día."""
        self.session.log_cross_sell("56912345678", "Ginecología", "Ecografía", "ofrecido")
        # Mismo paciente, diferente par — sigue bloqueado por el límite diario
        ok = self.session.puede_cross_sell(
            "56912345678", "Traumatología", "Kinesiología"
        )
        self.assertFalse(ok)

    def test_cooldown_no_afecta_otro_paciente(self):
        """El cooldown de un paciente no afecta a otro."""
        self.session.log_cross_sell("56912345678", "Ginecología", "Ecografía", "ofrecido")
        ok = self.session.puede_cross_sell(
            "56987654321", "Ginecología", "Ecografía"
        )
        self.assertTrue(ok)

    def test_cross_sell_rules_ginecologia(self):
        """_CROSS_SELL_RULES contiene regla para Ginecología."""
        # Import directo del dict (no requiere inicializar la app)
        import importlib.util, pathlib
        spec = importlib.util.spec_from_file_location(
            "flows_consts",
            pathlib.Path(__file__).parent.parent / "app" / "flows.py"
        )
        # Parsear el dict sin ejecutar el módulo completo (AST)
        import ast
        src = (pathlib.Path(__file__).parent.parent / "app" / "flows.py").read_text()
        tree = ast.parse(src)
        # Buscar _CROSS_SELL_RULES como AnnAssign o Assign
        found_keys = []
        for node in ast.walk(tree):
            if isinstance(node, ast.AnnAssign):
                if isinstance(node.target, ast.Name) and node.target.id == "_CROSS_SELL_RULES":
                    if isinstance(node.value, ast.Dict):
                        for k in node.value.keys:
                            if isinstance(k, ast.Constant):
                                found_keys.append(k.value)
        self.assertIn("Ginecología", found_keys)
        self.assertIn("Traumatología", found_keys)
        self.assertIn("Ortodoncia", found_keys)
        self.assertIn("Implantología", found_keys)


# ══════════════════════════════════════════════════════════════════════════════
# Feature B — Referral bonos
# ══════════════════════════════════════════════════════════════════════════════

class TestReferralBonos(unittest.TestCase):

    def setUp(self):
        _setup_test_db()
        import session
        self.session = session
        # Crear dos pacientes y código
        self.referrer = "56911111111"
        self.referred  = "56922222222"
        self.code = session.generate_referral_code(self.referrer)

    def tearDown(self):
        if _DB_PATH and os.path.exists(_DB_PATH):
            os.unlink(_DB_PATH)

    def test_registrar_bono_crea_registro(self):
        """registrar_bono_referral crea un registro pendiente."""
        bono_id = self.session.registrar_bono_referral(
            code=self.code,
            referrer_phone=self.referrer,
            referred_phone=self.referred,
            tipo_bono="medica_20",
        )
        self.assertIsNotNone(bono_id)
        bonos = self.session.get_bonos_referral(estado="todos")
        self.assertEqual(len(bonos), 1)
        self.assertEqual(bonos[0]["tipo_bono"], "medica_20")
        self.assertIsNone(bonos[0]["fecha_primera_cita"])

    def test_idempotente_doble_registro(self):
        """Registrar el mismo par dos veces no duplica el bono."""
        id1 = self.session.registrar_bono_referral(
            self.code, self.referrer, self.referred, "medica_20")
        id2 = self.session.registrar_bono_referral(
            self.code, self.referrer, self.referred, "medica_20")
        self.assertEqual(id1, id2)
        self.assertEqual(len(self.session.get_bonos_referral()), 1)

    def test_marcar_primera_cita_retorna_bono(self):
        """Cuando el referido completa primera cita, bono queda listo para notificar."""
        self.session.registrar_bono_referral(
            self.code, self.referrer, self.referred, "medica_20")
        pendientes = self.session.marcar_bono_primera_cita(self.referred)
        self.assertEqual(len(pendientes), 1)
        self.assertEqual(pendientes[0]["referrer_phone"], self.referrer)

    def test_marcar_primera_cita_sin_bono_retorna_vacio(self):
        """Si no hay bono registrado, marcar_primera_cita retorna lista vacía."""
        pendientes = self.session.marcar_bono_primera_cita("56933333333")
        self.assertEqual(pendientes, [])

    def test_marcar_bono_notificado(self):
        """Después de notificar, el bono no aparece en pendientes de notificación."""
        self.session.registrar_bono_referral(
            self.code, self.referrer, self.referred, "medica_20")
        pendientes = self.session.marcar_bono_primera_cita(self.referred)
        bono_id = pendientes[0]["id"]
        self.session.marcar_bono_notificado(bono_id)
        # Buscar pendientes de notificación nuevamente
        pendientes2 = self.session.marcar_bono_primera_cita(self.referred)
        self.assertEqual(len(pendientes2), 0)

    def test_conteo_referidos_mes(self):
        """conteo_referidos_mes retorna correctamente el conteo del mes actual."""
        self.session.registrar_bono_referral(
            self.code, self.referrer, self.referred, "medica_20")
        # Antes de primera cita: conteo = 0
        self.assertEqual(self.session.conteo_referidos_mes(self.referrer), 0)
        # Después de marcar primera cita: conteo = 1
        self.session.marcar_bono_primera_cita(self.referred)
        self.assertEqual(self.session.conteo_referidos_mes(self.referrer), 1)

    def test_get_bonos_filtro_estado(self):
        """get_bonos_referral filtra por estado correctamente."""
        self.session.registrar_bono_referral(
            self.code, self.referrer, self.referred, "medica_20")
        self.session.marcar_bono_primera_cita(self.referred)
        # Estado 'pendiente': tiene primera_cita pero no aplicado
        pendientes = self.session.get_bonos_referral(estado="pendiente")
        self.assertEqual(len(pendientes), 1)
        # Estado 'aplicado': aún vacío
        aplicados = self.session.get_bonos_referral(estado="aplicado")
        self.assertEqual(len(aplicados), 0)

    def test_generate_referral_code_formato(self):
        """El código generado cumple el formato CMC-XXXX."""
        import re
        code = self.session.generate_referral_code("56999999999")
        self.assertRegex(code, r'^CMC-[A-Z0-9]{4}$')

    def test_generate_referral_code_idempotente(self):
        """Llamar generate dos veces para el mismo phone retorna el mismo código."""
        code1 = self.session.generate_referral_code("56988888888")
        code2 = self.session.generate_referral_code("56988888888")
        self.assertEqual(code1, code2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
