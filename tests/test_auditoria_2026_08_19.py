"""Regresión de los fixes del consolidado de auditoría 2026-08-19.

Cubre, con tests unitarios sin red (mocks/DB temporal donde hace falta):
  - #9 leak: guarda de meta-prompt en detect_intent/respuesta_faq.
  - #4/#9: señal de resolución de la recepcionista fuerza HUMAN_TAKEOVER
    aunque el paciente esté en un estado transaccional.
  - #1 Márquez: relabel a "Medicina Familiar" al guardar la cita para el pool MG.
  - #2 modalidad: especialidades Solo Particular nunca deben mostrar Fonasa.
  - #5 nombre centinela: '_nombre_corto' filtra 'Otra'/'paciente'/'None'/etc.
  - #12 seguimiento viejo: 'get_ultimo_seguimiento' ignora respuestas
    pendientes de hace más de 48h.
  - #15 botones: cubierto por test_botones_longitud.py (test separado).

Ejecución:
    python3 tests/test_auditoria_2026_08_19.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import pathlib
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))


# ══════════════════════════════════════════════════════════════════════════
# #9 — Guarda de fuga de meta-prompt
# ══════════════════════════════════════════════════════════════════════════

class TestLeakGuard(unittest.TestCase):

    def setUp(self):
        import claude_helper
        self.rx = claude_helper._RX_LEAK_METAPROMPT

    def test_detecta_el_leak_real(self):
        texto = (
            "Entendido. Estoy listo para procesar mensajes de pacientes. "
            "Responderé exclusivamente en formato JSON válido con las claves: "
            "intent, especialidad y respuesta_directa. ¿Cuál es el mensaje del paciente?"
        )
        self.assertTrue(self.rx.search(texto))

    def test_no_falso_positivo_en_respuesta_normal(self):
        textos_ok = [
            "El Dr. Alonso Márquez (Medicina Familiar) atiende martes y jueves. "
            "La consulta particular es $30.000.",
            "La ecografía es solo particular, $40.000. ¿Te ayudo a agendar?",
            "Puedes pagar con transferencia, efectivo o tarjeta (solo dental).",
            "El JSON de tu comprobante no es necesario, solo la foto del voucher.",
        ]
        for t in textos_ok:
            self.assertIsNone(self.rx.search(t), f"falso positivo en: {t!r}")


# ══════════════════════════════════════════════════════════════════════════
# #4/#9 — Señal de resolución de recepcionista fuerza HUMAN_TAKEOVER
# ══════════════════════════════════════════════════════════════════════════

class TestResolucionRecepcionista(unittest.TestCase):

    def setUp(self):
        import re as _re
        # Misma regex que admin_routes.py::admin_reply — reconstruida acá para
        # no depender de arrancar FastAPI/Request en el test. Si se edita la
        # regex en admin_routes.py, este test debe actualizarse a mano (o
        # mejor: extraerla a una función importable en un refactor futuro).
        self.rx = _re.compile(
            r"ya\s+(?:est[aá]|qued[oó]|te\s+la|te\s+lo|la|lo)\s*"
            r"(?:anul|cancel|agend|reserv|confirm|cambi|resolv|list)"
            r"|\blisto,?\s+ya\b"
            r"|\bresuelto\b"
            r"|\bya\s+la\s+anul|ya\s+lo\s+anul"
            r"|\byo\s+(?:ya\s+)?(?:la\s+|lo\s+)?(?:anul[eé]|agend[eé]|cancel[eé])\b",
            _re.IGNORECASE,
        )

    def test_casos_reales_detectados(self):
        casos = [
            "ok, ya está anulada",
            "listo, ya quedó agendada para el jueves",
            "Ya la anulé recién",
            "resuelto, gracias",
        ]
        for c in casos:
            self.assertTrue(self.rx.search(c), f"no detectó resolución en: {c!r}")

    def test_preguntas_normales_no_disparan(self):
        casos = [
            "su nombre?",
            "¿me confirmas tu RUT?",
            "dame un momento por favor",
            "¿cuál era la hora que querías?",
        ]
        for c in casos:
            self.assertIsNone(self.rx.search(c), f"falso positivo en: {c!r}")


# ══════════════════════════════════════════════════════════════════════════
# #1 — Precio/etiqueta de Dr. Márquez (Medicina Familiar, no MG)
# ══════════════════════════════════════════════════════════════════════════

class TestMarquezRelabel(unittest.TestCase):

    def test_precio_line_fuerza_medicina_familiar_por_id(self):
        import flows
        slot = {"id_profesional": 13, "hora_inicio": "10:00", "hora_fin": "10:20"}
        linea = flows._precio_line("Medicina General", slot)
        self.assertIn("30.000", linea)
        self.assertNotIn("25.000", linea)

    def test_otro_profesional_mg_no_se_toca(self):
        import flows
        slot = {"id_profesional": 1, "hora_inicio": "10:00", "hora_fin": "10:15"}
        linea = flows._precio_line("Medicina General", slot)
        self.assertIn("25.000", linea)


# ══════════════════════════════════════════════════════════════════════════
# #2 — Solo Particular nunca debe ofrecer Fonasa
# ══════════════════════════════════════════════════════════════════════════

class TestSoloParticular(unittest.TestCase):

    def test_especialidades_solo_particular_fuera_de_fonasa_set(self):
        import flows
        for esp in ("Ecografía", "Odontología General", "Podología", "Psiquiatría"):
            self.assertNotIn(esp, flows._FONASA_SPECIALTIES,
                              f"{esp} no debería tener opción Fonasa")

    def test_especialidades_ambas_estan_en_fonasa_set(self):
        import flows
        for esp in ("Medicina General", "Medicina Familiar", "Kinesiología"):
            self.assertIn(esp, flows._FONASA_SPECIALTIES)


# ══════════════════════════════════════════════════════════════════════════
# #5 — Nombres centinela no se muestran al paciente
# ══════════════════════════════════════════════════════════════════════════

class TestNombreCentinela(unittest.TestCase):

    def test_fidelizacion_filtra_centinelas(self):
        import fidelizacion
        for centinela in ("Otra persona", "paciente", "None", "null", "Otro"):
            self.assertEqual(fidelizacion._nombre_corto(centinela), "",
                              f"{centinela!r} debería filtrarse")
        self.assertEqual(fidelizacion._nombre_corto("Maria Jose Soto"), "Maria")

    def test_reminders_filtra_centinelas(self):
        import reminders
        for centinela in ("Otra persona", "paciente", "None", "null"):
            self.assertEqual(reminders._nombre_corto(centinela), "")
        self.assertEqual(reminders._nombre_corto("Sergio Carrasco"), "Sergio")


# ══════════════════════════════════════════════════════════════════════════
# #12 — get_ultimo_seguimiento ignora seguimientos viejos (>48h)
# ══════════════════════════════════════════════════════════════════════════

class TestSeguimientoViejo(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._db_path = tmp.name
        tmp.close()
        import session as _session
        self._orig_db_path = _session.DB_PATH
        _session.DB_PATH = pathlib.Path(self._db_path)
        self.session = _session
        with _session._conn():
            pass

    def tearDown(self):
        self.session.DB_PATH = self._orig_db_path
        if os.path.exists(self._db_path):
            os.unlink(self._db_path)

    def _insert_seguimiento(self, phone: str, hace_horas: int):
        with self.session._conn() as conn:
            conn.execute(
                "INSERT INTO fidelizacion_msgs (phone, tipo, respuesta, cita_id, enviado_en) "
                "VALUES (?, 'postconsulta', NULL, '1', datetime('now', ?))",
                (phone, f"-{hace_horas} hours"),
            )
            conn.commit()

    def test_seguimiento_reciente_se_retorna(self):
        self._insert_seguimiento("56900000001", hace_horas=2)
        seg = self.session.get_ultimo_seguimiento("56900000001")
        self.assertIsNotNone(seg)

    def test_seguimiento_viejo_se_ignora(self):
        # Caso real: seguimiento de hace 3 semanas nunca respondido — un
        # "Mejor" cualquiera semanas después NO debe interpretarse como
        # respuesta a esa pregunta de salud vieja.
        self._insert_seguimiento("56900000002", hace_horas=24 * 21)
        seg = self.session.get_ultimo_seguimiento("56900000002")
        self.assertIsNone(seg)

    def test_limite_justo_48h(self):
        self._insert_seguimiento("56900000003", hace_horas=47)
        self.assertIsNotNone(self.session.get_ultimo_seguimiento("56900000003"))
        self._insert_seguimiento("56900000004", hace_horas=49)
        self.assertIsNone(self.session.get_ultimo_seguimiento("56900000004"))


def _run() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for cls in (TestLeakGuard, TestResolucionRecepcionista, TestMarquezRelabel,
                TestSoloParticular, TestNombreCentinela, TestSeguimientoViejo):
        suite.addTests(loader.loadTestsFromTestCase(cls))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(_run())
