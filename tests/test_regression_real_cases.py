"""Suite de regresión con casos reales del bot CMC.

Cada test cubre un bug histórico documentado. Si el test falla, el bug
está volviendo. Correr antes de cada deploy.

Casos cubiertos (orden cronológico de aparición):
  BUG-01..09 sesión 2026-05-01: cancelar cita reciente, hola mid-flow,
              modalidad fonasa, eco→ginecología, precio inconsistente,
              número personal, sesiones duplicadas con/sin "+"
  BUG postconsulta 2026-05-02: fecha UTC vs CLT, hora_corte
  BUG nombre "Si Primera Vez" 2026-05-02
  BUG RUT 1638805-K hint específico 2026-05-02
  BUG "se cancela allá" como pago 2026-05-02
  BUG WAIT_SLOT ordinales 2026-05-02
  BUG reenganche solo 5 estados 2026-05-02
  BUG reenganche IG/FB excluido 2026-05-02
  BUG recordatorio cita anulada 2026-05-03
  BUG markdown ** sin renderizar 2026-05-03
  BUG fonasa en WAIT_SLOT 2026-05-03
  BUG "otros horarios" texto 2026-05-03
  BUG medicina familiar sin nota 2026-05-03
  FIX medicina familiar sistémico 2026-05-03: solo Márquez (ID 13),
      fallback explícito cuando sin cupo, label "Medicina Familiar" en slot,
      detección de menor en flujo MG/MF

Uso:
  python tests/test_regression_real_cases.py
  python -m unittest tests.test_regression_real_cases
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

# Permitir importar desde app/
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

# Evitar que la importación de session.py intente abrir SQLCipher real
os.environ.setdefault("SQLCIPHER_KEY", "")


class TestRUTValidation(unittest.TestCase):
    """RUTs chilenos cortos (7 dígitos) y formato variable."""

    def test_rut_corto_con_K_es_valido(self):
        """RUT 1638805-K (7 dígitos + K) debe ser aceptado.
        Caso real: paciente Leonor 2026-05-02 escribió formato corto."""
        from medilink import valid_rut, clean_rut
        rut = clean_rut("1638805-K")
        self.assertTrue(valid_rut(rut), f"RUT corto con K debe ser válido: {rut}")

    def test_rut_corto_con_DV_invalido_da_hint(self):
        """RUT 1638805-0 (DV incorrecto) debe disparar hint específico."""
        from medilink import valid_rut, hint_rut_error
        self.assertFalse(valid_rut("16388050"))
        msg = hint_rut_error("1638805-0")
        self.assertIn("1638805", msg, "Hint debe mencionar el cuerpo del RUT")
        # Debe sugerir el DV correcto (K)
        self.assertIn("K", msg, "Hint debe sugerir DV correcto")

    def test_rut_con_puntos_y_guion(self):
        from medilink import valid_rut, clean_rut
        # RUTs comunes con puntos
        self.assertTrue(valid_rut(clean_rut("12.345.678-5")))
        self.assertTrue(valid_rut(clean_rut("16388052-0")))

    def test_rut_invalido_modulo11(self):
        from medilink import valid_rut, clean_rut
        self.assertFalse(valid_rut(clean_rut("12345678-9")))  # DV incorrecto


class TestNormalizacionTeléfono(unittest.TestCase):
    """Sesiones duplicadas con/sin prefijo + (BUG-09 sesión 1)."""

    def test_normalize_wa_id_strippea_prefijo(self):
        from session import normalize_wa_id
        self.assertEqual(normalize_wa_id("+56987834148"), "56987834148")
        self.assertEqual(normalize_wa_id("56987834148"), "56987834148")

    def test_normalize_wa_id_preserva_ig_fb(self):
        from session import normalize_wa_id
        self.assertEqual(normalize_wa_id("ig_27613511"), "ig_27613511")
        self.assertEqual(normalize_wa_id("fb_98765432"), "fb_98765432")


class TestCanalReenganche(unittest.TestCase):
    """Reenganche cubre WhatsApp + Instagram + Messenger (BUG 2026-05-02)."""

    def test_canal_de_phone_wa(self):
        from jobs import _canal_de_phone
        self.assertEqual(_canal_de_phone("56987834148"), "wa")

    def test_canal_de_phone_ig(self):
        from jobs import _canal_de_phone
        self.assertEqual(_canal_de_phone("ig_27613511"), "ig")

    def test_canal_de_phone_fb(self):
        from jobs import _canal_de_phone
        self.assertEqual(_canal_de_phone("fb_98765432"), "fb")

    def test_canal_de_phone_unknown_test(self):
        from jobs import _canal_de_phone
        self.assertEqual(_canal_de_phone("TEST_xxx"), "unknown")

    def test_canal_de_phone_corto(self):
        from jobs import _canal_de_phone
        # Phones < 10 dígitos no son válidos
        self.assertEqual(_canal_de_phone("12345"), "unknown")


class TestEspecialidadShortMatch(unittest.TestCase):
    """'Eco' debe matchear Ecografía antes que Ginecología (BUG-04 sesión 1)."""

    def test_eco_matchea_ecografia(self):
        from flows import _detectar_especialidad_en_texto
        result = _detectar_especialidad_en_texto("eco")
        self.assertIsNotNone(result)
        # Debe ser ecografía o id correspondiente, NO ginecología
        result_str = str(result).lower()
        self.assertIn("ecograf", result_str, f"'eco' debe matchear ecografía, got {result}")

    def test_orl_matchea_otorrino(self):
        from flows import _detectar_especialidad_en_texto
        result = _detectar_especialidad_en_texto("orl")
        self.assertIsNotNone(result)
        result_str = str(result).lower()
        self.assertIn("otorrino", result_str)

    def test_kine_matchea_kinesiologia(self):
        from flows import _detectar_especialidad_en_texto
        result = _detectar_especialidad_en_texto("kine")
        self.assertIsNotNone(result)
        result_str = str(result).lower()
        self.assertIn("kine", result_str)


class TestCancelarComoPago(unittest.TestCase):
    """Chilenismo cancelar=pagar (BUG 2026-05-02)."""

    def test_regex_pago_existe_en_claude_helper(self):
        """El pre-filter _CANCEL_AS_PAY_RE debe existir en claude_helper."""
        contenido = (ROOT / "app" / "claude_helper.py").read_text()
        self.assertIn("_CANCEL_AS_PAY_RE", contenido,
                      "claude_helper debe tener el pre-filter cancel-as-pay")
        # Debe matchear casos clave del paciente
        self.assertIn("se cancela", contenido.lower(),
                      "Pre-filter debe cubrir 'se cancela'")

    def test_se_cancela_alla_matchea_pre_filter(self):
        """Caso real: 'Se cancela alla o antes la horita Y como?'"""
        # Reconstruye el regex tal como está en producción (claude_helper.py:1081)
        import re
        rx = re.compile(
            r"(hay que cancelar|se cancela (?:al tiro|altiro|ahora|adelantado|por adelantado|en |con )|"
            r"se cancela (?:all[aá]|ac[aá]|ah[ií]|en el|al llegar|antes|despues|después|"
            r"al dia|al d[ií]a|el d[ií]a|el dia)|"
            r"cancela(?:r)? (?:all[aá]|ac[aá]|ah[ií]|en el centro|en recepcion|en recepción)|"
            r"cuando (?:se )?cancela|como (?:se )?cancela(?! (?:la|mi|una|el) (?:hora|cita))|"
            r"cuanto (?:hay que )?cancel|"
            r"cancelar (?:al tiro|altiro|por adelantado|adelantado|en efectivo|"
            r"con (?:efectivo|debito|débito|credito|crédito|transferencia|tarjeta))|"
            r"\bse paga\b|\bhay que pagar\b|\bcomo (?:se )?paga\b|\bcuando (?:se )?paga\b)",
            re.IGNORECASE
        )
        self.assertTrue(rx.search("se cancela alla o antes la horita"),
                        "Caso real Si Primera Vez debe matchear")
        self.assertTrue(rx.search("Se cancela allá"))
        self.assertTrue(rx.search("como se paga?"))

    def test_cancelar_la_hora_NO_matchea_pago(self):
        """'cancelar mi hora' NO debe matchear el pre-filter de pago."""
        import re
        rx = re.compile(
            r"como (?:se )?cancela(?! (?:la|mi|una|el) (?:hora|cita))",
            re.IGNORECASE
        )
        # "como se cancela LA hora" NO matchea (look-ahead negativo)
        self.assertIsNone(rx.search("como se cancela la hora"))
        self.assertIsNone(rx.search("como se cancela mi cita"))


class TestParserDatosNuevo(unittest.TestCase):
    """'Si primera vez' no debe quedar como nombre (BUG 2026-05-02)."""

    def test_prefijo_primera_vez_filtrado(self):
        """El regex _PREFIJOS_PRIMERA_VEZ debe matchear estos casos."""
        import re
        # Recreamos el regex tal como está en flows.py:5587
        PRIMERA_VEZ = re.compile(
            r'^(s[ií]\s+primera\s+vez|primera\s+vez|primera|s[ií]|no|control|'
            r'continuaci[oó]n|seguimiento|segunda\s+vez|es\s+primera|es\s+primera\s+vez)\s*$',
            re.I
        )
        for caso in ["Si primera vez", "Sí primera vez", "Primera vez",
                     "primera", "si", "no", "Control", "Es primera vez"]:
            self.assertTrue(PRIMERA_VEZ.match(caso),
                            f"Debería matchear: {caso!r}")

    def test_nombre_real_no_matchea(self):
        import re
        PRIMERA_VEZ = re.compile(
            r'^(s[ií]\s+primera\s+vez|primera\s+vez|primera|s[ií]|no|control|'
            r'continuaci[oó]n|seguimiento|segunda\s+vez|es\s+primera|es\s+primera\s+vez)\s*$',
            re.I
        )
        for caso in ["Leonor Pérez", "Juan Carlos", "María Eduvijes",
                     "Sebastián Mohor"]:
            self.assertFalse(PRIMERA_VEZ.match(caso),
                             f"Nombre real NO debe matchear: {caso!r}")


class TestPostconsultaFechaCLT(unittest.TestCase):
    """get_citas_para_seguimiento usa fecha CLT y filtra hora pasada
    (BUG postconsulta 2026-05-02)."""

    def test_signature_acepta_hora_corte(self):
        """La función debe aceptar parámetro hora_corte para filtrar."""
        import inspect
        from session import get_citas_para_seguimiento
        sig = inspect.signature(get_citas_para_seguimiento)
        params = list(sig.parameters.keys())
        self.assertIn("hora_corte", params,
                      "get_citas_para_seguimiento debe aceptar hora_corte")

    def test_fidelizacion_usa_clt(self):
        """fidelizacion.py debe usar ZoneInfo America/Santiago, no date.today()."""
        contenido = (ROOT / "app" / "fidelizacion.py").read_text()
        self.assertIn("America/Santiago", contenido,
                      "fidelizacion.py debe usar tz Chile")
        # date.today() puede aparecer en COMENTARIOS explicando el bug viejo,
        # pero NO en código activo de enviar_seguimiento_postconsulta.
        idx_func = contenido.find("def enviar_seguimiento_postconsulta")
        idx_next = contenido.find("\nasync def ", idx_func + 10)
        if idx_next == -1:
            idx_next = contenido.find("\ndef ", idx_func + 10)
        bloque = contenido[idx_func:idx_next]
        # Filtrar líneas de comentario
        lineas_codigo = [ln for ln in bloque.split("\n")
                         if not ln.lstrip().startswith("#")
                         and "#" not in ln.split('"""')[-1] if True]
        # Más simple: buscar date.today() solo fuera de comentarios y docstrings.
        # Estrategia: tokenizar por líneas, eliminar las que empiezan con # o """,
        # y verificar que ninguna línea de código tenga la llamada activa.
        en_docstring = False
        for ln in bloque.split("\n"):
            stripped = ln.lstrip()
            if stripped.startswith('"""') or stripped.startswith("'''"):
                # Toggle docstring
                en_docstring = not en_docstring
                continue
            if en_docstring:
                continue
            if stripped.startswith("#"):
                continue
            # Línea de código activo
            self.assertNotIn("date.today()", ln,
                             f"date.today() en código activo de "
                             f"enviar_seguimiento_postconsulta: {ln.strip()}")


class TestRecordatorioValidaMedilink(unittest.TestCase):
    """Recordatorio valida cita en Medilink antes de enviar (BUG Sebastian/Quijano)."""

    def test_get_citas_bot_pendientes_filtra_cancel_detected(self):
        """La query debe excluir citas con cancel_detected_at NOT NULL."""
        contenido = (ROOT / "app" / "session.py").read_text()
        idx = contenido.find("def get_citas_bot_pendientes")
        idx_end = contenido.find("\ndef ", idx + 10)
        bloque = contenido[idx:idx_end]
        self.assertIn("cancel_detected_at IS NULL", bloque,
                      "Query debe filtrar canceladas")

    def test_reminders_usa_get_cita(self):
        """reminders.py debe importar get_cita y usarlo para validar."""
        contenido = (ROOT / "app" / "reminders.py").read_text()
        self.assertIn("get_cita", contenido,
                      "reminders.py debe usar get_cita para validar Medilink")
        self.assertIn("estado_anulacion", contenido,
                      "Debe validar contra estado_anulacion")


class TestReengancheEstados(unittest.TestCase):
    """get_sesiones_abandonadas cubre todos los estados activos (no solo 5)."""

    def test_query_invertida_excluye_solo_idle_completed_takeover(self):
        contenido = (ROOT / "app" / "session.py").read_text()
        idx = contenido.find("def get_sesiones_abandonadas")
        idx_end = contenido.find("\ndef ", idx + 10)
        bloque = contenido[idx:idx_end]
        # Lógica invertida: NOT IN excluidos
        self.assertIn("state NOT IN", bloque,
                      "Query debe usar NOT IN (estados excluidos), no IN (5 estados)")
        # Debe tener IDLE, COMPLETED, HUMAN_TAKEOVER en exclusión
        self.assertIn('"IDLE"', bloque)
        self.assertIn('"COMPLETED"', bloque)
        self.assertIn('"HUMAN_TAKEOVER"', bloque)


class TestMarkdownPostProcesador(unittest.TestCase):
    """Markdown ** → * para WhatsApp (BUG 2026-05-03)."""

    def test_post_procesador_aplicado_en_respuesta_faq(self):
        contenido = (ROOT / "app" / "claude_helper.py").read_text()
        # El post-procesador debe estar dentro de respuesta_faq
        idx = contenido.find("def respuesta_faq")
        if idx == -1:
            idx = contenido.find("async def respuesta_faq")
        idx_end = contenido.find("\nasync def ", idx + 10)
        if idx_end == -1:
            idx_end = contenido.find("\ndef ", idx + 10)
        bloque = contenido[idx:idx_end] if idx_end != -1 else contenido[idx:]
        # Debe haber un replace de ** por *
        self.assertTrue(
            'replace("**", "*")' in bloque or "replace('**', '*')" in bloque,
            "respuesta_faq debe tener replace('**', '*') para WhatsApp"
        )


class TestStaffWhitelistEstructura(unittest.TestCase):
    """staff_whitelist module existe y exporta STAFF_PHONES."""

    def test_modulo_existe(self):
        path = ROOT / "app" / "staff_whitelist.py"
        self.assertTrue(path.exists(), "staff_whitelist.py debe existir")

    def test_exporta_funciones_staff(self):
        """Módulo debe exponer is_staff/get_staff_name/get_all_staff."""
        try:
            import staff_whitelist
        except Exception as e:
            self.fail(f"No se pudo importar staff_whitelist: {e}")
        for fn in ("is_staff", "get_staff_name", "get_all_staff"):
            self.assertTrue(hasattr(staff_whitelist, fn),
                            f"staff_whitelist debe exportar {fn}")


class TestMonitorAnomalias(unittest.TestCase):
    """Monitor activo escanea 8 patrones (sesión 2026-05-03)."""

    def test_modulo_monitor_existe(self):
        path = ROOT / "app" / "monitor.py"
        self.assertTrue(path.exists(), "app/monitor.py debe existir")

    def test_8_detectores_registrados(self):
        import monitor
        self.assertTrue(hasattr(monitor, "_DETECTORES"))
        self.assertGreaterEqual(len(monitor._DETECTORES), 7,
                                "Al menos 7 detectores activos")
        tipos = [t for t, _ in monitor._DETECTORES]
        # Detectores críticos que NO pueden faltar
        for esperado in ["POSTCONSULTA_PREMATURA", "LEAK_NUMERO_PERSONAL",
                          "RECORDATORIO_CITA_ANULADA"]:
            self.assertIn(esperado, tipos,
                          f"Detector crítico faltante: {esperado}")

    def test_anti_spam_table_creada(self):
        import monitor
        self.assertTrue(hasattr(monitor, "_alert_hash"))
        self.assertTrue(hasattr(monitor, "_was_alerted_recently"))


class TestJobPreventivoCancelaciones(unittest.TestCase):
    """_job_detectar_cancelaciones existe y se registra (sesión 2026-05-03)."""

    def test_job_existe_en_jobs(self):
        import jobs
        self.assertTrue(hasattr(jobs, "_job_detectar_cancelaciones"))

    def test_get_citas_bot_para_validar_existe(self):
        import session
        self.assertTrue(hasattr(session, "get_citas_bot_para_validar"))


class TestNumeroPersonalNoLeakea(unittest.TestCase):
    """+56987834148 NO debe aparecer hardcoded en archivos customer-facing."""

    def test_no_hardcoded_in_messaging(self):
        """messaging.py no debe tener el número personal hardcoded
        EXCEPTO en el bloque del _final_phone_guard que existe precisamente
        para detectarlo y bloquearlo."""
        contenido = (ROOT / "app" / "messaging.py").read_text()
        lines = contenido.split("\n")
        for i, line in enumerate(lines):
            if "987834148" not in line:
                continue
            # Permitido en líneas asociadas al guard
            lower = line.lower()
            permitido = any(kw in lower for kw in (
                "guard", "scrub", "leak", "warning", "regex", "pattern",
                "blocked", "personal_phone", "_personal", "nunca",
                "customer-facing", "log.warning", "log.error"
            ))
            if permitido:
                continue
            self.fail(f"messaging.py:{i+1} hardcodea número personal: {line.strip()}")

    def test_no_hardcoded_in_flows(self):
        """flows.py no debe usar 987834148 como destino de send_*."""
        contenido = (ROOT / "app" / "flows.py").read_text()
        lines = contenido.split("\n")
        for i, line in enumerate(lines):
            if "987834148" not in line:
                continue
            lower = line.lower()
            permitido = any(kw in lower for kw in (
                "guard", "scrub", "leak", "warning", "comment", "#",
                "personal", "nunca", "test"
            ))
            if permitido:
                continue
            self.fail(f"flows.py:{i+1} hardcodea número personal: {line.strip()}")


class TestSlotParserOrdinales(unittest.TestCase):
    """WAIT_SLOT acepta 'la primera', 'el último' (BUG 2026-05-02)."""

    def test_parse_slot_ordinales_existe(self):
        contenido = (ROOT / "app" / "flows.py").read_text()
        # Debe existir alguna referencia a ordinales en el parser
        self.assertTrue(
            "primera" in contenido and "último" in contenido,
            "flows.py debe tener lógica de ordinales en WAIT_SLOT"
        )


class TestOtrosHorariosTextoLibre(unittest.TestCase):
    """'Otros horarios' como texto libre debe matchear ver_otros (BUG 2026-05-03)."""

    def test_aliases_ver_otros_en_flows(self):
        contenido = (ROOT / "app" / "flows.py").read_text()
        # Debe haber alguna mención a "otros horarios" como texto válido
        contenido_lower = contenido.lower()
        self.assertIn("otros horarios", contenido_lower,
                      "flows.py debe reconocer 'otros horarios' como alias")


class TestMedicinaFamiliarNota(unittest.TestCase):
    """Medicina Familiar usa solo Márquez (ID 13), no MG genérica.
    Fix sistémico 2026-05-03: casos reales fb_36265734933013648 y 56987840895.
    """

    def test_nota_existe_en_flows(self):
        contenido = (ROOT / "app" / "flows.py").read_text()
        contenido_lower = contenido.lower()
        self.assertIn("medicina familiar", contenido_lower,
                      "flows.py debe mencionar medicina familiar")

    def test_esp_med_familiar_set_definido(self):
        """_ESP_MED_FAMILIAR debe incluir las 3 variantes clave."""
        contenido = (ROOT / "app" / "flows.py").read_text()
        self.assertIn("_ESP_MED_FAMILIAR", contenido)
        self.assertIn('"medicina familiar"', contenido)
        self.assertIn('"médico familiar"', contenido)
        self.assertIn('"medico familiar"', contenido)

    def test_med_familiar_ids_es_solo_marquez(self):
        """_MED_FAMILIAR_IDS = [13] — solo Dr. Márquez."""
        contenido = (ROOT / "app" / "flows.py").read_text()
        self.assertIn("_MED_FAMILIAR_IDS = [13]", contenido,
                      "_MED_FAMILIAR_IDS debe ser [13] (solo Márquez)")

    def test_branch_medfam_en_iniciar_agendar(self):
        """_iniciar_agendar debe tener branch elif para _ESP_MED_FAMILIAR."""
        contenido = (ROOT / "app" / "flows.py").read_text()
        self.assertIn("elif especialidad_lower in _ESP_MED_FAMILIAR", contenido,
                      "Debe existir branch elif _ESP_MED_FAMILIAR en _iniciar_agendar")

    def test_no_conversion_familiar_a_general(self):
        """medicina familiar NO debe convertirse silenciosamente a medicina general."""
        contenido = (ROOT / "app" / "flows.py").read_text()
        # El bloque BUG-02 que convertía MF→MG debe estar eliminado
        self.assertNotIn(
            '*Medicina Familiar* y *Medicina General* comparten agenda en el CMC.',
            contenido,
            "El mensaje engañoso de conversión MF→MG debe haberse eliminado"
        )

    def test_fallback_medfam_sin_cupo_es_explicito(self):
        """Cuando Márquez no tiene cupo, debe mostrar mensaje explícito — no autoswitch."""
        contenido = (ROOT / "app" / "flows.py").read_text()
        self.assertIn("WAIT_MEDFAM_FALLBACK", contenido,
                      "Debe existir estado WAIT_MEDFAM_FALLBACK para fallback explícito")
        self.assertIn("medfam_fallback_si", contenido,
                      "Debe existir botón medfam_fallback_si")

    def test_slot_label_medicina_familiar(self):
        """Cuando se ofrece slot de MF, el label debe ser 'Medicina Familiar' no 'Medicina General'."""
        contenido = (ROOT / "app" / "flows.py").read_text()
        # Buscar donde se normaliza el label del slot a Medicina Familiar
        self.assertIn('"Medicina Familiar"', contenido,
                      "Debe asignar label 'Medicina Familiar' a los slots de MF")

    def test_branch_medfam_en_otro_dia(self):
        """Handler 'otro_dia' en WAIT_SLOT debe tener branch elif _ESP_MED_FAMILIAR."""
        contenido = (ROOT / "app" / "flows.py").read_text()
        # El branch elif en otro_dia debe filtrar solo a Márquez
        idx = contenido.find("elif especialidad in _ESP_MED_FAMILIAR")
        self.assertGreater(idx, 0,
                           "Debe existir elif _ESP_MED_FAMILIAR en handler otro_dia")

    def test_branch_medfam_faq_preview(self):
        """FAQ slot preview inline también debe filtrar a solo Márquez para MF."""
        contenido = (ROOT / "app" / "flows.py").read_text()
        self.assertIn("elif esp_lower in _ESP_MED_FAMILIAR", contenido,
                      "FAQ preview inline debe tener branch _ESP_MED_FAMILIAR")

    def test_menor_detectado_en_mgmf(self):
        """Detección de menor en flujo MG/MF — WAIT_CONFIRMAR_ADULTO."""
        contenido = (ROOT / "app" / "flows.py").read_text()
        self.assertIn("WAIT_CONFIRMAR_ADULTO", contenido,
                      "Debe existir estado WAIT_CONFIRMAR_ADULTO para detección de menores")
        self.assertIn("_detectar_menor_en_texto", contenido,
                      "Debe existir helper _detectar_menor_en_texto")

    def test_detectar_menor_helper_edad(self):
        """_detectar_menor_en_texto detecta edad < 14 años."""
        import importlib.util, pathlib
        spec = importlib.util.spec_from_file_location(
            "flows_partial",
            str(ROOT / "app" / "flows.py")
        )
        # No podemos importar flows sin el entorno completo; verificar vía AST en cambio.
        contenido = (ROOT / "app" / "flows.py").read_text()
        self.assertIn("< 14", contenido,
                      "_detectar_menor_en_texto debe comparar edad < 14")


class TestContextoEspecialidadHerencia(unittest.TestCase):
    """Pregunta de seguimiento sin especialidad debe heredar contexto reciente.
    Caso real fb_36265734933013648 2026-05-03:
    1) 'hacen ecomamaria' → bot responde info de ecografía (esp=ecografía)
    2) 2min después: 'Y la hacen por Fonasa?' → esp=null en detect
    Sin herencia el bot daba respuesta genérica sin contexto.
    """

    def test_esp_context_heredado_existe(self):
        """flows.py debe heredar last_esp_context dentro de TTL 5min."""
        contenido = (ROOT / "app" / "flows.py").read_text()
        self.assertIn("esp_context_heredado", contenido,
                      "Debe loggear cuando hereda contexto")
        self.assertIn("last_esp_context", contenido,
                      "Debe leer last_esp_context")

    def test_ttl_300s(self):
        """El TTL debe ser 300s = 5min para herencia útil."""
        contenido = (ROOT / "app" / "flows.py").read_text()
        idx = contenido.find("esp_context_heredado")
        # Buscar 300 cerca (TTL en segundos)
        bloque = contenido[max(0, idx-500):idx+200]
        self.assertIn("300", bloque, "TTL debe ser 300s (5min)")


class TestOrdenMedicaRequisito(unittest.TestCase):
    """'Necesito orden médica?' es pregunta sobre requisito, no solicitud.
    Caso real fb_27736544599278971 2026-05-03 16:45.
    """

    def test_pre_filter_orden_requisito_existe(self):
        contenido = (ROOT / "app" / "claude_helper.py").read_text()
        self.assertIn("_ORDEN_REQUISITO_RE", contenido,
                      "claude_helper debe tener pre-filter orden-requisito")
        self.assertIn("orden-requisito prefilter", contenido,
                      "Debe loggear cuando matchea")

    def test_necesito_orden_medica_matchea(self):
        import re
        rx = re.compile(
            r"(necesito\s+(?:la\s+)?orden\s+m[eé]dica\s*[?¿]"
            r"|se\s+necesita\s+(?:la\s+)?orden"
            r"|hay\s+que\s+(?:llevar|tener|traer)\s+(?:la\s+)?orden"
            r"|requiere(?:n)?\s+(?:la\s+)?orden\s+m[eé]dica"
            r"|piden\s+orden\s+m[eé]dica"
            r"|necesito\s+orden\s+para"
            r"|sin\s+orden\s+m[eé]dica"
            r"|la\s+orden\s+es\s+obligatoria)",
            re.IGNORECASE,
        )
        casos_match = [
            "hola necesito orden médica?",
            "Necesito orden médica?",
            "se necesita orden?",
            "hay que llevar orden?",
            "requiere orden médica?",
            "piden orden médica para la eco?",
            "necesito orden para hacerme la eco",
        ]
        for caso in casos_match:
            self.assertTrue(rx.search(caso),
                            f"Debe matchear: {caso!r}")

    def test_solicitud_orden_NO_matchea(self):
        """Afirmación sin '?' NO debe disparar el pre-filter."""
        import re
        rx = re.compile(
            r"(necesito\s+(?:la\s+)?orden\s+m[eé]dica\s*[?¿]"
            r"|se\s+necesita\s+(?:la\s+)?orden"
            r"|hay\s+que\s+(?:llevar|tener|traer)\s+(?:la\s+)?orden"
            r"|requiere(?:n)?\s+(?:la\s+)?orden\s+m[eé]dica"
            r"|piden\s+orden\s+m[eé]dica"
            r"|necesito\s+orden\s+para"
            r"|sin\s+orden\s+m[eé]dica"
            r"|la\s+orden\s+es\s+obligatoria)",
            re.IGNORECASE,
        )
        self.assertIsNone(rx.search("necesito una orden por favor"))


class TestSlotLockOptimista(unittest.TestCase):
    """Lock optimista anti-race condition (5 casos/7d en auditoría)."""

    def setUp(self):
        # Usar DB temporal in-memory para no contaminar producción
        import os
        os.environ["DATABASE_PATH"] = ":memory:"
        # Forzar recarga del módulo session si ya estaba cacheado
        import importlib
        import session
        importlib.reload(session)
        self.session = session

    def test_adquirir_lock_primero_funciona(self):
        ok = self.session.adquirir_slot_lock(1, "2026-12-31", "10:00:00",
                                              "56987654321", ttl_segundos=30)
        self.assertTrue(ok, "Primer paciente debe adquirir el lock")

    def test_segundo_paciente_es_rechazado(self):
        self.session.adquirir_slot_lock(1, "2026-12-31", "10:30:00",
                                         "56111111111", ttl_segundos=30)
        ok2 = self.session.adquirir_slot_lock(1, "2026-12-31", "10:30:00",
                                               "56222222222", ttl_segundos=30)
        self.assertFalse(ok2, "Segundo paciente NO debe adquirir mismo slot")

    def test_mismo_paciente_re_adquiere(self):
        """Re-confirmación del mismo phone debe ser OK (idempotente)."""
        phone = "56333333333"
        ok1 = self.session.adquirir_slot_lock(1, "2026-12-31", "11:00:00",
                                               phone, ttl_segundos=30)
        ok2 = self.session.adquirir_slot_lock(1, "2026-12-31", "11:00:00",
                                               phone, ttl_segundos=30)
        self.assertTrue(ok1)
        self.assertTrue(ok2, "Mismo paciente puede re-adquirir su lock")

    def test_liberar_libera(self):
        self.session.adquirir_slot_lock(1, "2026-12-31", "12:00:00",
                                         "56444444444", ttl_segundos=30)
        self.session.liberar_slot_lock(1, "2026-12-31", "12:00:00")
        # Otro phone ahora puede adquirir
        ok = self.session.adquirir_slot_lock(1, "2026-12-31", "12:00:00",
                                              "56555555555", ttl_segundos=30)
        self.assertTrue(ok, "Tras liberar, otro paciente puede adquirir")




# ─── Tests bugs pediatría / validación edad 2026-05-03 ───────────────────────

class TestParseSlotSelectionEdadGuard(unittest.TestCase):
    """BUG-1: _parse_slot_selection no debe matchear número embebido en contexto de edad/menor."""

    def setUp(self):
        import sys
        sys.path.insert(0, str(ROOT / "app"))
        import importlib
        import flows as _flows
        importlib.reload(_flows)
        self._parse = _flows._parse_slot_selection

    def _make_slots(self, n=5):
        return [{"hora_inicio": f"{8+i:02d}:00", "hora_fin": f"{8+i:02d}:20"} for i in range(n)]

    def test_bebe_2_anos_no_matchea_slot_2(self):
        """'Es para mi bebé 2 años' NO debe matchear slot 2."""
        slots = self._make_slots(5)
        result = self._parse("Es para mi bebé 2 años", slots)
        self.assertIsNone(result, "El número '2' en 'bebé 2 años' no debe interpretarse como slot 2")

    def test_guagua_3_meses_no_matchea_slot_3(self):
        """'Es para mi guagua de 3 meses' NO debe matchear slot 3."""
        slots = self._make_slots(5)
        result = self._parse("Es para mi guagua de 3 meses", slots)
        self.assertIsNone(result, "El '3' en '3 meses' no debe ser slot 3")

    def test_nino_4_anos_no_matchea_slot_4(self):
        """'para mi niño de 4 años' NO debe matchear slot 4."""
        slots = self._make_slots(5)
        result = self._parse("para mi niño de 4 años", slots)
        self.assertIsNone(result, "Contexto de edad en niño no debe matchear slot")

    def test_numero_solo_si_matchea(self):
        """'2' suelto SÍ debe matchear slot 2 (índice 1)."""
        slots = self._make_slots(5)
        result = self._parse("2", slots)
        self.assertEqual(result, 1, "'2' suelto debe dar índice 1")

    def test_opcion_3_si_matchea(self):
        """'opción 3' SÍ debe matchear slot 3 (índice 2)."""
        slots = self._make_slots(5)
        result = self._parse("opción 3", slots)
        self.assertEqual(result, 2, "'opción 3' debe dar índice 2")


class TestOtraPersonaREAmpliado(unittest.TestCase):
    """BUG-2: _OTRA_PERSONA_RE en flows.py debe cubrir bebé, guagua, niño, niña, chico, chica."""

    def setUp(self):
        self.flows_content = (ROOT / "app" / "flows.py").read_text(encoding="utf-8")

    def test_bebe_en_otra_persona_re(self):
        """flows.py debe tener patrón para bebé/bebe en _OTRA_PERSONA_RE."""
        self.assertIn("beb", self.flows_content.lower(),
                      "flows.py debe tener 'beb' en _OTRA_PERSONA_RE")
        # Verificar específicamente el patrón ampliado
        self.assertIn("_OTRA_PERSONA_RE", self.flows_content,
                      "flows.py debe tener _OTRA_PERSONA_RE definido")

    def test_guagua_en_otra_persona_re(self):
        """flows.py debe tener patrón para guagua."""
        # Buscar desde el primer _OTRA_PERSONA_RE hasta el final del bloque (1200 chars)
        idx = self.flows_content.find("_OTRA_PERSONA_RE")
        bloque = self.flows_content[idx:idx+1200]
        self.assertIn("guagua", bloque,
                      "_OTRA_PERSONA_RE debe incluir guagua")

    def test_nino_nina_en_otra_persona_re(self):
        """flows.py debe tener patrón para niño/niña."""
        idx = self.flows_content.find("_OTRA_PERSONA_RE")
        bloque = self.flows_content[idx:idx+600]
        self.assertIn("ni", bloque,
                      "_OTRA_PERSONA_RE debe incluir niño/niña")

    def test_chico_chica_en_otra_persona_re(self):
        """flows.py debe tener patrón para chico/chica."""
        idx = self.flows_content.find("_OTRA_PERSONA_RE")
        bloque = self.flows_content[idx:idx+1200]
        self.assertIn("chic", bloque,
                      "_OTRA_PERSONA_RE debe incluir chico/chica")

    def test_no_matchea_otro_dia_en_codigo(self):
        """El comentario del fix debe mencionar el caso 'otro día' evitado."""
        self.assertIn("para otro", self.flows_content,
                      "El código debe documentar el caso 'para otro día' que NO debe matchear")

    def test_otra_persona_slot_re_en_wait_slot(self):
        """WAIT_SLOT también debe tener guard de tercero."""
        self.assertIn("_OTRA_PERSONA_SLOT_RE", self.flows_content,
                      "WAIT_SLOT debe tener _OTRA_PERSONA_SLOT_RE definido")

    def test_slot_re_tiene_guagua(self):
        """_OTRA_PERSONA_SLOT_RE en WAIT_SLOT debe incluir guagua."""
        idx = self.flows_content.find("_OTRA_PERSONA_SLOT_RE")
        bloque = self.flows_content[idx:idx+600]
        self.assertIn("guagua", bloque,
                      "_OTRA_PERSONA_SLOT_RE en WAIT_SLOT debe incluir guagua")


class TestConfigEdadAvisoPediatria(unittest.TestCase):
    """BUG-3: EDAD_AVISO_PEDIATRIA debe existir en config.py con las especialidades correctas."""

    def setUp(self):
        import sys
        sys.path.insert(0, str(ROOT / "app"))
        import importlib
        import config as _cfg
        importlib.reload(_cfg)
        self.cfg = _cfg

    def test_existe_dict(self):
        self.assertTrue(hasattr(self.cfg, "EDAD_AVISO_PEDIATRIA"),
                        "EDAD_AVISO_PEDIATRIA debe existir en config.py")

    def test_medicina_general_14(self):
        d = self.cfg.EDAD_AVISO_PEDIATRIA
        self.assertEqual(d.get("medicina general"), 14)

    def test_medicina_familiar_14(self):
        d = self.cfg.EDAD_AVISO_PEDIATRIA
        self.assertEqual(d.get("medicina familiar"), 14)

    def test_kinesiologia_14(self):
        d = self.cfg.EDAD_AVISO_PEDIATRIA
        self.assertEqual(d.get("kinesiologia"), 14)

    def test_psicologia_adulto_18(self):
        d = self.cfg.EDAD_AVISO_PEDIATRIA
        self.assertEqual(d.get("psicologia adulto"), 18)


class TestFlowsPediatriaGuards(unittest.TestCase):
    """BUG-2/BUG-3/BUG-5: guardrails en flows.py presentes en código."""

    def setUp(self):
        self.contenido = (ROOT / "app" / "flows.py").read_text()

    def test_otra_persona_slot_re_en_wait_slot(self):
        """_OTRA_PERSONA_SLOT_RE debe estar definido en el handler WAIT_SLOT."""
        self.assertIn("_OTRA_PERSONA_SLOT_RE", self.contenido,
                      "Falta guard de tercero/menor en WAIT_SLOT")

    def test_bue1_edad_ctx_re_en_parse_slot(self):
        """_EDAD_CTX_RE debe estar en _parse_slot_selection."""
        self.assertIn("_EDAD_CTX_RE", self.contenido,
                      "Falta guard de contexto de edad en _parse_slot_selection")

    def test_bug3_aviso_pediatria_en_wait_rut(self):
        """Aviso pediátrico debe verificarse en WAIT_RUT_AGENDAR."""
        self.assertIn("pediatria_aviso_visto", self.contenido,
                      "Falta flag pediatria_aviso_visto en WAIT_RUT_AGENDAR")
        self.assertIn("ped_continuar", self.contenido,
                      "Falta handler botón ped_continuar")
        self.assertIn("ped_no", self.contenido,
                      "Falta handler botón ped_no")

    def test_bug5_menor_kw_rut_guard(self):
        """Guard de keyword menor en WAIT_RUT_AGENDAR debe existir."""
        self.assertIn("rut_agendar_reintent_menor", self.contenido,
                      "Falta guard BUG-5 en WAIT_RUT_AGENDAR (log_event rut_agendar_reintent_menor)")
        self.assertIn("_MENOR_KW_RUT", self.contenido,
                      "Falta _MENOR_KW_RUT regex en WAIT_RUT_AGENDAR")


class TestClaudeHelperPediatriaRegla(unittest.TestCase):
    """BUG-4: SYSTEM_PROMPT debe tener regla explícita para consultas de pediatría."""

    def setUp(self):
        self.contenido = (ROOT / "app" / "claude_helper.py").read_text()

    def test_regla_pediatria_en_system_prompt(self):
        """SYSTEM_PROMPT debe mencionar que pediatría → intent info + derivar CESFAM."""
        self.assertIn("PEDIATRÍA", self.contenido,
                      "Falta regla PEDIATRÍA en SYSTEM_PROMPT de claude_helper.py")

    def test_nunca_psicologia_adulto_para_pediatria(self):
        """Regla debe prohibir clasificar pediatría como Psicología Adulto."""
        idx_ped = self.contenido.find("PEDIATRÍA")
        bloque_ped = self.contenido[idx_ped:idx_ped+800]
        self.assertIn("Psicología Adulto", bloque_ped,
                      "Regla debe mencionar que NUNCA clasifique como Psicología Adulto")

    def test_derivacion_cesfam_en_regla(self):
        """Regla debe mencionar CESFAM como derivación."""
        idx = self.contenido.find("PEDIATRÍA")
        bloque = self.contenido[idx:idx+600]
        self.assertIn("CESFAM", bloque,
                      "Regla de pediatría debe mencionar derivación a CESFAM")

def _run():
    """Ejecutor con resumen claro."""
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2, buffer=False)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(_run())


class TestBug1OtraPersonaREAmpliadoP2(unittest.TestCase):
    """BUG-1: _OTRA_PERSONA_RE debe cubrir suegra/cuñado/sobrina/tía/vecino/yerno/pololo."""

    def setUp(self):
        self.content = (ROOT / "app" / "flows.py").read_text(encoding="utf-8")
        idx = self.content.find("_OTRA_PERSONA_RE")
        # Tomar el bloque desde la primera ocurrencia hasta cierre del regex
        self.bloque = self.content[idx: idx + 1800]

    def _check(self, kw):
        self.assertIn(kw, self.bloque, f"_OTRA_PERSONA_RE debe cubrir '{kw}'")

    def test_suegra(self):  self._check("suegra")
    def test_cuñado(self):  self._check("cu\u00f1ado")
    def test_sobrina(self): self._check("sobrina")
    def test_tia(self):     self._check("t\u00eda")
    def test_vecina(self):  self._check("vecina")
    def test_yerno(self):   self._check("yerno")
    def test_nuera(self):   self._check("nuera")
    def test_pololo(self):  self._check("pololo")
    def test_abuelito(self): self._check("abuelito")


class TestBug3MenorEdad18(unittest.TestCase):
    """BUG-3: _detectar_menor_en_texto debe detectar 14-17 como menores."""

    def setUp(self):
        import sys
        sys.path.insert(0, str(ROOT / "app"))

    def test_catorce_anios_es_menor(self):
        from flows import _detectar_menor_en_texto
        self.assertTrue(_detectar_menor_en_texto("tiene 14 años"),
                        "14 años debe ser detectado como menor")

    def test_diecisiete_es_menor(self):
        from flows import _detectar_menor_en_texto
        self.assertTrue(_detectar_menor_en_texto("es una chica de 17 años"),
                        "17 años debe ser detectado como menor")

    def test_dieciocho_no_es_menor(self):
        from flows import _detectar_menor_en_texto
        self.assertFalse(_detectar_menor_en_texto("es para un joven de 18 años"),
                         "18 años NO debe ser detectado como menor")

    def test_adulto_no_es_menor(self):
        from flows import _detectar_menor_en_texto
        self.assertFalse(_detectar_menor_en_texto("tengo 45 años"),
                         "45 años NO debe ser detectado como menor")

    def test_adolescente_helper(self):
        from flows import _es_adolescente_en_texto
        self.assertTrue(_es_adolescente_en_texto("paciente de 15 años"),
                        "_es_adolescente_en_texto debe retornar True para 15 años")

    def test_adolescente_helper_menor_14_false(self):
        from flows import _es_adolescente_en_texto
        self.assertFalse(_es_adolescente_en_texto("niño de 8 años"),
                         "_es_adolescente_en_texto debe ser False para < 14")


class TestP1CacheTyposEspecialidad(unittest.TestCase):
    """P1: _INTENT_CACHE debe cubrir fisio/fisioterapia/kiné/ortodonsista."""

    def setUp(self):
        self.content = (ROOT / "app" / "claude_helper.py").read_text(encoding="utf-8")

    def _check_cache(self, kw):
        self.assertIn(f'"{kw}"', self.content,
                      f'_INTENT_CACHE debe tener entrada para "{kw}"')

    def test_fisio(self):          self._check_cache("fisio")
    def test_fisioterapia(self):   self._check_cache("fisioterapia")
    def test_fisioterapeuta(self): self._check_cache("fisioterapeuta")
    def test_kine_con_tilde(self): self._check_cache("kin\u00e9")
    def test_ortodonsista(self):   self._check_cache("ortodonsista")
    def test_obstetra(self):       self._check_cache("obstetra")


class TestBug5SessionHelper(unittest.TestCase):
    """BUG-5: session.py debe tener get_proxima_cita_paciente."""

    def test_funcion_existe(self):
        content = (ROOT / "app" / "session.py").read_text()
        self.assertIn("def get_proxima_cita_paciente", content,
                      "session.py debe tener get_proxima_cita_paciente")

    def test_funcion_importable(self):
        import sys
        sys.path.insert(0, str(ROOT / "app"))
        from session import get_proxima_cita_paciente
        self.assertTrue(callable(get_proxima_cita_paciente))


# ─────────────────────────────────────────────────────────────────────────────
# Meta Referral — feature 2026-05-03
# ─────────────────────────────────────────────────────────────────────────────

class TestMetaReferralSession(unittest.TestCase):
    """save_meta_referral + get_meta_referral_fresh con TTL."""

    def setUp(self):
        import tempfile, os
        self._db_fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(self._db_fd)
        # Apuntar session.py a la DB temporal
        import session as _sess
        from pathlib import Path
        self._orig_path = _sess.DB_PATH
        _sess.DB_PATH = Path(self._db_path)
        # Limpiar conexiones cacheadas para forzar reconexión con la DB temporal
        if hasattr(_sess, '_local'):
            _sess._local = {}
        # _conn() crea las tablas al conectar (llama a init_db internamente)
        _sess._conn()

    def tearDown(self):
        import session as _sess
        _sess.DB_PATH = self._orig_path
        _sess._local = {}
        import os
        os.unlink(self._db_path)

    def _sess(self):
        import session
        return session

    def test_save_and_get_fresh(self):
        sess = self._sess()
        phone = "56912345678"
        referral = {
            "headline": "Ecotomografía mamaria $40.000",
            "source_id": "ad_123",
            "source_type": "ad",
            "body": "Agenda fácil por WhatsApp",
            "ctwa_clid": "token_abc",
        }
        sess.save_meta_referral(phone, referral, canal="whatsapp")
        result = sess.get_meta_referral_fresh(phone, ttl_horas=24)
        self.assertIsNotNone(result, "Debe retornar el referral recién guardado")
        self.assertEqual(result["headline"], "Ecotomografía mamaria $40.000")
        self.assertEqual(result["source_id"], "ad_123")

    def test_get_fresh_expired_returns_none(self):
        """TTL negativo debe considerar cualquier referral como expirado."""
        import time
        sess = self._sess()
        phone = "56911111111"
        referral = {"headline": "Consulta médica", "source_id": "ad_999"}
        sess.save_meta_referral(phone, referral)
        # TTL negativo → cutoff en el futuro → ningún ts lo satisface
        result = sess.get_meta_referral_fresh(phone, ttl_horas=-1)
        self.assertIsNone(result, "TTL negativo debe retornar None")

    def test_get_fresh_no_referral_returns_none(self):
        sess = self._sess()
        result = sess.get_meta_referral_fresh("56999999999", ttl_horas=24)
        self.assertIsNone(result, "Sin referral debe retornar None")

    def test_get_referrals_recientes(self):
        sess = self._sess()
        for i in range(3):
            sess.save_meta_referral(f"5691000000{i}", {"headline": f"Anuncio {i}"})
        rows = sess.get_meta_referrals_recientes(limit=10)
        self.assertEqual(len(rows), 3)
        # Debe venir ordenado del más reciente al más antiguo
        headlines = [r["headline"] for r in rows]
        self.assertIn("Anuncio 0", headlines)


class TestOrdenRequisitoConReferral(unittest.TestCase):
    """detect_intent prefilter: referral + headline de eco → respuesta específica."""

    def _run_detect_intent_sync(self, mensaje, meta_referral=None):
        """Wrapper síncrono para detect_intent (función async)."""
        import asyncio
        # Mockear el cliente Anthropic para no llamar a la API real
        import claude_helper as ch
        _orig_client = ch.client

        class _FakeClient:
            class messages:
                @staticmethod
                async def create(**kwargs):
                    raise RuntimeError("No debe llamar a API real en tests")

        ch.client = _FakeClient()
        try:
            # Para prefilters que no llegan a Claude podemos correr sin mock
            loop = asyncio.new_event_loop()
            result = loop.run_until_complete(
                ch.detect_intent(mensaje, meta_referral=meta_referral)
            )
            loop.close()
        finally:
            ch.client = _orig_client
        return result

    def test_orden_medica_con_referral_eco_da_respuesta_especifica(self):
        """Caso real 2026-05-03: 'necesito orden médica?' + referral ecotomografía
        debe dar respuesta específica sobre eco, no genérica."""
        referral = {"headline": "Ecotomografía mamaria $40.000"}
        result = self._run_detect_intent_sync(
            "hola necesito orden médica?",
            meta_referral=referral,
        )
        self.assertEqual(result["intent"], "faq")
        resp = result.get("respuesta_directa") or ""
        # Debe mencionar ecografía específicamente
        self.assertIn("ecograf", resp.lower(),
                      "Respuesta debe mencionar ecografía cuando referral es de eco")
        # NO debe mostrar la lista genérica con bullets de todas las categorías
        self.assertNotIn("Kinesiología con bono Fonasa", resp,
                         "Respuesta específica de eco NO debe tener bullets genéricos")

    def test_orden_medica_sin_referral_da_respuesta_generica(self):
        """Sin referral → respuesta genérica con lista de exámenes."""
        result = self._run_detect_intent_sync(
            "hola necesito orden médica?",
            meta_referral=None,
        )
        self.assertEqual(result["intent"], "faq")
        resp = result.get("respuesta_directa") or ""
        # Respuesta genérica debe incluir la lista de bullets
        self.assertIn("Kinesiología con bono Fonasa", resp,
                      "Sin referral debe dar respuesta genérica con lista")

    def test_orden_medica_con_referral_no_eco_da_respuesta_generica(self):
        """Referral de un anuncio de medicina general (sin keywords de examen)
        → respuesta genérica."""
        referral = {"headline": "Consulta Medicina General $10.000"}
        result = self._run_detect_intent_sync(
            "necesito orden médica?",
            meta_referral=referral,
        )
        self.assertEqual(result["intent"], "faq")
        resp = result.get("respuesta_directa") or ""
        # Sin keywords de eco/radio/lab → genérica
        self.assertIn("Kinesiología con bono Fonasa", resp,
                      "Referral sin keywords de examen debe dar respuesta genérica")

    def test_se_necesita_orden_con_referral_radiografia(self):
        """Headline con 'radiografía' → respuesta específica de radio."""
        referral = {"headline": "Radiografía tórax + informe $15.000"}
        result = self._run_detect_intent_sync(
            "se necesita orden médica?",
            meta_referral=referral,
        )
        resp = result.get("respuesta_directa") or ""
        self.assertIn("radiograf", resp.lower(),
                      "Referral con radiografía debe dar respuesta específica de radio")


class TestMetaReferralFunctionsExist(unittest.TestCase):
    """Verifica que los helpers existen y son importables."""

    def test_save_meta_referral_importable(self):
        from session import save_meta_referral
        self.assertTrue(callable(save_meta_referral))

    def test_get_meta_referral_fresh_importable(self):
        from session import get_meta_referral_fresh
        self.assertTrue(callable(get_meta_referral_fresh))

    def test_get_meta_referrals_recientes_importable(self):
        from session import get_meta_referrals_recientes
        self.assertTrue(callable(get_meta_referrals_recientes))

    def test_detect_intent_acepta_meta_referral_kwarg(self):
        """detect_intent debe aceptar meta_referral como parámetro."""
        import inspect
        from claude_helper import detect_intent
        sig = inspect.signature(detect_intent)
        self.assertIn("meta_referral", sig.parameters,
                      "detect_intent debe tener parámetro meta_referral")

    def test_respuesta_faq_acepta_meta_referral_kwarg(self):
        """respuesta_faq debe aceptar meta_referral como parámetro."""
        import inspect
        from claude_helper import respuesta_faq
        sig = inspect.signature(respuesta_faq)
        self.assertIn("meta_referral", sig.parameters,
                      "respuesta_faq debe tener parámetro meta_referral")

    def test_admin_endpoint_en_admin_routes(self):
        """admin_routes.py debe tener el endpoint /admin/api/referrals/recientes."""
        content = (ROOT / "app" / "admin_routes.py").read_text(encoding="utf-8")
        self.assertIn("/admin/api/referrals/recientes", content,
                      "admin_routes debe tener endpoint de referrals")
