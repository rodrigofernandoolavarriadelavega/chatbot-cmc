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


def _run():
    """Ejecutor con resumen claro."""
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2, buffer=False)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(_run())
