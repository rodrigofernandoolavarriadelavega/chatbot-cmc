"""
Estética Facial (Dra. Fuentealba, id 76) — decisión del dueño 2026-10-07:

1. Antes de agendar, el bot pregunta QUÉ procedimiento y en QUÉ zona le interesa
   al paciente; lo guarda y lo pasa a la cita en Medilink (observaciones) para
   que la doctora lo vea. Si el paciente ya lo dijo (landing "Me interesa la
   zona: entrecejo", o "quiero botox en la frente"), no se vuelve a preguntar.
   "No sé / quiero evaluación" es respuesta válida.
2. Abono de $15.000 (la evaluación) para reservar la hora, por el sistema
   existente (config.ABONO_REGLAS + gate de flows + abono_transferencia).
3. Texto al paciente de EVALUACIÓN (no el de "consulta completa"): si ese mismo
   día se hace el tratamiento, la evaluación sale gratis y solo paga la
   diferencia. Sin la palabra "convenio".
4. Reagendar no vuelve a cobrar.

Todo mockeado: sin Claude ni Medilink reales.

Uso:
    PYTHONPATH=app:. python tests/test_estetica_abono_interes_2026_10_07.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT))

TMP_DB = Path(tempfile.mkdtemp(prefix="cmc_test_est_abono_")) / "sessions.db"
os.environ["SESSIONS_DB"] = str(TMP_DB)

import session  # noqa: E402
session.DB_PATH = TMP_DB

import config  # noqa: E402
import flows  # noqa: E402
import medilink  # noqa: E402
from session import get_session, save_session  # noqa: E402

PHONE = "56911112222_test_estetica"


def setUpModule():
    """Nada sale a la red: ni Medilink ni Claude ni WhatsApp."""
    import httpx

    async def _sin_red(self, *a, **k):
        raise RuntimeError("red bloqueada en test")

    global _PATCH_RED
    _PATCH_RED = patch.object(httpx.AsyncClient, "send", _sin_red)
    _PATCH_RED.start()


def tearDownModule():
    _PATCH_RED.stop()

SLOT_EST = {
    "especialidad": "Estética Facial",
    "profesional": "Dra. Valentina Fuentealba",
    "id_profesional": 76,
    "fecha": "2099-03-10",
    "fecha_display": "Martes 10 de marzo",
    "hora_inicio": "11:00:00",
    "hora_fin": "11:30:00",
    "id_recurso": 1,
}
PACIENTE = {"id": 4321, "nombre": "Camila Prueba Soto", "rut": "12345678-5"}


def run(coro):
    return asyncio.run(coro)


def _txt(resp) -> str:
    """Texto plano de una respuesta (str o botones)."""
    if isinstance(resp, str):
        return resp
    if isinstance(resp, dict):
        return json.dumps(resp, ensure_ascii=False)
    return str(resp)


def _handle(texto: str, state: str, data: dict):
    save_session(PHONE, state, data)
    sess = get_session(PHONE)
    return run(flows.handle_message(PHONE, texto, sess)), sess


class _Base(unittest.TestCase):
    """Estado limpio por test: sin perfil del teléfono (un perfil activaría el
    fast-track de _slot_confirmed, que consulta Medilink)."""

    def setUp(self):
        from session import reset_session
        reset_session(PHONE)
        self._p = patch.object(flows, "get_profile", lambda *a, **k: None)
        self._p.start()

    def tearDown(self):
        self._p.stop()


class TestRegla(unittest.TestCase):
    def test_regla_estetica_facial(self):
        r = config.abono_regla(especialidad="Estética Facial")
        self.assertIsNotNone(r)
        self.assertEqual(r["monto"], 15000)
        self.assertEqual(r["precio"], 15000)           # saldo de la evaluación = 0
        self.assertEqual(r["profesionales"], [76])
        self.assertTrue(r["gate_bot"])
        self.assertEqual(r["concepto"], "evaluacion")

    def test_match_por_nombres_y_profesional(self):
        for esp in ("estética facial", "estetica facial", "Estetica", "estética"):
            self.assertIsNotNone(config.abono_regla(especialidad=esp), esp)
        self.assertIsNotNone(config.abono_regla(id_profesional=76))

    def test_estetica_dental_y_otras_no_llevan_abono(self):
        for esp in ("estética dental", "estetica dental", "Odontología General",
                    "Medicina General", "Ortodoncia", "Kinesiología"):
            self.assertIsNone(config.abono_regla(especialidad=esp), esp)

    def test_las_otras_reglas_siguen_igual(self):
        self.assertEqual(config.abono_regla(especialidad="Psiquiatría")["monto"],
                         config.ABONO_PSIQUIATRIA_CLP)
        self.assertEqual(config.abono_regla(id_profesional=65)["monto"],
                         config.ABONO_GASTRO_CLP)
        self.assertNotEqual(config.abono_regla(id_profesional=78).get("concepto"),
                            "evaluacion")

    def test_el_slot_real_lo_nombra_como_la_regla(self):
        # Cómo nombra el bot a la profesional 76 en un slot.
        esp = medilink._especialidad_display(76)
        self.assertIsNotNone(config.abono_regla(especialidad=esp))

    def test_panel_de_abonos_deriva_la_entrada(self):
        import abonos_routes
        pol = abonos_routes._policy_desde_registro()
        self.assertEqual(pol["Estética Facial"]["abono_sugerido"], 15000)
        self.assertEqual(pol["Estética Facial"]["precio_sugerido"], 15000)
        self.assertEqual(pol["Estética Facial"]["id_profesional"], 76)


class TestExtraccionInteres(unittest.TestCase):
    def test_landing(self):
        t = ("Hola, quiero agendar una evaluación facial. Me interesa la zona: "
             "entrecejo, toxina botulínica (web: landing_estetica)")
        self.assertEqual(flows._extraer_interes_estetica(t),
                         "entrecejo, toxina botulínica")

    def test_texto_libre_con_procedimiento(self):
        self.assertEqual(flows._extraer_interes_estetica("quiero botox en la frente"),
                         "quiero botox en la frente")
        self.assertIsNotNone(flows._extraer_interes_estetica("relleno de labios"))
        self.assertIsNotNone(flows._extraer_interes_estetica("me molestan las ojeras"))

    def test_sin_procedimiento_no_hay_interes(self):
        for t in ("quiero hora de estética", "hola", "agendar estética facial",
                  "quiero una evaluación facial", "me duele una muela"):
            self.assertIsNone(flows._extraer_interes_estetica(t), t)

    def test_obs_para_la_doctora(self):
        obs = flows._obs_cita_estetica({"estetica_interes": "botox en la frente"}, SLOT_EST)
        self.assertIn("botox en la frente", obs)
        self.assertIn("EVALUACIÓN", obs)
        # slot de otra especialidad → nada
        self.assertEqual(flows._obs_cita_estetica(
            {"estetica_interes": "botox"}, {"especialidad": "Medicina General",
                                            "id_profesional": 1}), "")
        # sin interés ni obs de consentimiento → nada que decir
        self.assertEqual(flows._obs_cita_estetica({}, SLOT_EST), "")


class TestPreguntaInteres(_Base):
    def test_sin_interes_pregunta_antes_de_seguir(self):
        resp, _ = run_slot_confirmed({})
        t = _txt(resp)
        self.assertIn("procedimiento", t)
        self.assertIn("zona", t)
        self.assertEqual(get_session(PHONE)["state"], "WAIT_ESTETICA_INTERES")

    def test_con_interes_ya_dicho_no_pregunta(self):
        resp, _ = run_slot_confirmed({"estetica_interes": "entrecejo"})
        self.assertNotEqual(get_session(PHONE)["state"], "WAIT_ESTETICA_INTERES")
        self.assertNotIn("qué procedimiento", _txt(resp))

    def test_reagendar_no_pregunta(self):
        resp, _ = run_slot_confirmed({"reagendar_mode": True})
        self.assertNotEqual(get_session(PHONE)["state"], "WAIT_ESTETICA_INTERES")

    def test_otra_especialidad_no_pregunta(self):
        slot = {**SLOT_EST, "especialidad": "Medicina General", "id_profesional": 1}
        save_session(PHONE, "WAIT_SLOT", {})
        run(flows._slot_confirmed(PHONE, {}, slot))
        self.assertNotEqual(get_session(PHONE)["state"], "WAIT_ESTETICA_INTERES")

    def test_respuesta_se_guarda_y_continua(self):
        resp, sess = _handle("botox en la frente", "WAIT_ESTETICA_INTERES",
                             {"slot_elegido": SLOT_EST})
        d = get_session(PHONE)["data"]
        self.assertEqual(d.get("estetica_interes"), "botox en la frente")
        self.assertNotEqual(get_session(PHONE)["state"], "WAIT_ESTETICA_INTERES")

    def test_no_se_pero_quiero_evaluacion_es_valido(self):
        for entrada in ("est_int_nose", "no sé, quiero la evaluación", "no se"):
            _handle(entrada, "WAIT_ESTETICA_INTERES", {"slot_elegido": SLOT_EST})
            d = get_session(PHONE)["data"]
            self.assertIn("evaluación", d.get("estetica_interes", ""), entrada)
            self.assertNotEqual(get_session(PHONE)["state"], "WAIT_ESTETICA_INTERES", entrada)

    def test_texto_libre_sin_palabra_conocida_se_guarda_tal_cual(self):
        _handle("las arrugas de al lado de los ojos", "WAIT_ESTETICA_INTERES",
                {"slot_elegido": SLOT_EST})
        self.assertIn("arrugas", get_session(PHONE)["data"].get("estetica_interes", ""))

    def test_mensaje_de_entrada_recuerda_el_interes(self):
        # El primer mensaje (landing) queda guardado en el punto de entrada
        # aunque después el flujo siga por cualquier camino.
        async def _no_intent(*a, **k):
            return {"intent": "otro"}
        save_session(PHONE, "IDLE", {})
        sess = get_session(PHONE)
        with patch.object(flows, "detect_intent", _no_intent):
            try:
                run(flows.handle_message(
                    PHONE,
                    "Hola, quiero agendar una evaluación facial. Me interesa la zona: "
                    "entrecejo (web: landing_estetica)", sess))
            except Exception:
                pass  # el resto del pipeline no es lo que se prueba acá
        self.assertEqual(sess["data"].get("estetica_interes"), "entrecejo")


def run_slot_confirmed(data: dict):
    save_session(PHONE, "WAIT_SLOT", dict(data))
    d = get_session(PHONE)["data"]
    resp = run(flows._slot_confirmed(PHONE, d, dict(SLOT_EST)))
    return resp, d


async def _slot_libre(*a, **k):
    return True


async def _sin_citas(*a, **k):
    return []


async def _crear_cita_prohibida(**k):
    raise AssertionError("no debe crear la cita antes de validar el abono")


class TestGateYTexto(_Base):
    def _gate(self, auto: bool, data_extra: dict | None = None):
        data = {"slot_elegido": dict(SLOT_EST), "paciente": dict(PACIENTE),
                "rut": PACIENTE["rut"], "especialidad": "estética facial",
                "estetica_interes": "botox en la frente"}
        data.update(data_extra or {})
        save_session(PHONE, "CONFIRMING_CITA", data)
        sess = get_session(PHONE)
        with patch.object(flows, "_abono_gate_psiq_activo", return_value=True), \
             patch.object(flows, "verificar_slot_disponible", _slot_libre), \
             patch.object(flows, "listar_citas_paciente", _sin_citas), \
             patch.object(flows, "crear_cita", _crear_cita_prohibida), \
             patch.object(config, "ABONO_AUTO_ACTIVE", auto):
            return run(flows.handle_message(PHONE, "si", sess))

    def _checks_texto(self, t: str):
        self.assertIn("$15.000", t)
        self.assertIn("evaluación", t)
        self.assertIn("diferencia", t)
        self.assertIn("gratis", t)
        self.assertIn("24 horas", t)
        self.assertNotIn("convenio", t.lower())
        self.assertNotIn("valor total de la consulta", t)
        self.assertNotIn("nada adicional", t)
        self.assertNotIn("teleconsulta", t.lower())
        self.assertNotIn("videollamada", t.lower())

    def test_gate_pide_abono_15000_sin_link(self):
        resp = self._gate(auto=False)
        t = _txt(resp)
        self._checks_texto(t)
        self.assertIn("Datos para transferir", t)
        self.assertIn("apartada", t)
        sess = get_session(PHONE)
        self.assertEqual(sess["state"], "WAIT_ABONO_COMPROBANTE")
        slot = sess["data"]["abono_gate_slot"]
        self.assertIn("botox en la frente", slot["obs_cita"])

    def test_gate_con_link_y_registro_pendiente(self):
        resp = self._gate(auto=True)
        t = _txt(resp)
        self._checks_texto(t)
        self.assertIn("/abono/", t)
        from abono_transferencia import get_abono_pendiente_activo_por_phone
        ap = get_abono_pendiente_activo_por_phone(PHONE)
        self.assertEqual(ap["monto"], 15000)
        self.assertEqual(str(ap["id_profesional"]), "76")
        self.assertIn("obs_cita", json.loads(ap["slot_json"]))

    def test_los_otros_abonos_conservan_su_texto(self):
        slot = {**SLOT_EST, "especialidad": "Gastroenterología",
                "profesional": "Dr. Nicolás Quijano", "id_profesional": 65}
        data = {"slot_elegido": slot, "paciente": dict(PACIENTE), "rut": PACIENTE["rut"],
                "especialidad": "gastroenterología"}
        save_session(PHONE, "CONFIRMING_CITA", data)
        sess = get_session(PHONE)
        with patch.object(flows, "_abono_gate_psiq_activo", return_value=True), \
             patch.object(flows, "verificar_slot_disponible", _slot_libre), \
             patch.object(flows, "listar_citas_paciente", _sin_citas), \
             patch.object(flows, "crear_cita", _crear_cita_prohibida), \
             patch.object(config, "ABONO_AUTO_ACTIVE", False):
            t = _txt(run(flows.handle_message(PHONE, "si", sess)))
        self.assertIn("valor total de la consulta", t)
        self.assertIn("$35.000", t)
        self.assertNotIn("gratis", t)

    def test_reagendar_no_cobra(self):
        llamadas = []

        async def _crear(**k):
            llamadas.append(k)
            return {"id": 777}

        async def _cancelar(*a, **k):
            return True

        data = {"slot_elegido": dict(SLOT_EST), "paciente": dict(PACIENTE),
                "rut": PACIENTE["rut"], "especialidad": "estética facial",
                "estetica_interes": "ojeras", "reagendar_mode": True,
                "cita_old": {"id": 555, "profesional": "Dra. Valentina Fuentealba",
                             "fecha_display": "lunes", "hora_inicio": "10:00:00"}}
        save_session(PHONE, "CONFIRMING_CITA", data)
        sess = get_session(PHONE)
        with patch.object(flows, "_abono_gate_psiq_activo", return_value=True), \
             patch.object(flows, "verificar_slot_disponible", _slot_libre), \
             patch.object(flows, "listar_citas_paciente", _sin_citas), \
             patch.object(flows, "crear_cita", _crear), \
             patch.object(flows, "cancelar_cita", _cancelar), \
             patch.object(config, "ABONO_AUTO_ACTIVE", False):
            try:
                resp = run(flows.handle_message(PHONE, "si", sess))
            except Exception:
                resp = ""
        self.assertNotEqual(get_session(PHONE)["state"], "WAIT_ABONO_COMPROBANTE")
        self.assertNotIn("abono", _txt(resp).lower())
        self.assertEqual(len(llamadas), 1, "reagendar debe crear la cita directo")
        self.assertIn("ojeras", llamadas[0].get("observaciones_extra", ""))

    def test_gate_apagado_crea_directo_con_el_interes(self):
        llamadas = []

        async def _crear(**k):
            llamadas.append(k)
            return {"id": 888}

        data = {"slot_elegido": dict(SLOT_EST), "paciente": dict(PACIENTE),
                "rut": PACIENTE["rut"], "especialidad": "estética facial",
                "estetica_interes": "relleno de labios"}
        save_session(PHONE, "CONFIRMING_CITA", data)
        sess = get_session(PHONE)
        with patch.object(flows, "_abono_gate_psiq_activo", return_value=False), \
             patch.object(flows, "verificar_slot_disponible", _slot_libre), \
             patch.object(flows, "listar_citas_paciente", _sin_citas), \
             patch.object(flows, "crear_cita", _crear):
            try:
                run(flows.handle_message(PHONE, "si", sess))
            except Exception:
                pass
        self.assertEqual(len(llamadas), 1)
        self.assertIn("relleno de labios", llamadas[0].get("observaciones_extra", ""))
        self.assertEqual(llamadas[0].get("modalidad"), "PRESENCIAL")


class TestPrecioYPago(_Base):
    def test_precio_line_con_gate(self):
        with patch.object(flows, "_abono_gate_psiq_activo", return_value=True):
            linea = flows._precio_line("Estética Facial", id_profesional=76)
        self.assertIn("Abono previo requerido", linea)
        self.assertIn("$15.000", linea)
        self.assertIn("evaluación", linea)

    def test_respuesta_de_pago_no_dice_100_adelantado(self):
        data = {"slot_elegido": dict(SLOT_EST), "especialidad": "estética facial"}
        with patch.object(flows, "_abono_gate_psiq_activo", return_value=True):
            t = flows._preguntar_pago_respuesta(data, "como pago")
            t2 = flows._preguntar_precio_respuesta(data, "cuanto cuesta")
        for x in (t, t2):
            self.assertIn("diferencia", x)
            self.assertNotIn("100%", x)
            self.assertNotIn("nada adicional", x)


class TestConfirmacionAlPagar(_Base):
    """Foto del comprobante y correo del banco: ambos crean la cita presencial,
    con el interés en observaciones, y confirman con el texto de evaluación."""

    def test_foto_comprobante(self):
        llamadas = []

        async def _crear(**k):
            llamadas.append(k)
            return {"id": 999}

        slot = {**SLOT_EST, "obs_cita": "[EVALUACIÓN ESTÉTICA $15.000 (se descuenta)] [INTERÉS: botox en la frente]",
                "estetica_interes": "botox en la frente"}
        save_session(PHONE, "WAIT_ABONO_COMPROBANTE", {
            "abono_gate_slot": slot, "abono_gate_paciente": dict(PACIENTE),
            "rut": PACIENTE["rut"], "abono_gate_ts": ""})
        import abono_comprobante
        fake = lambda b, c: {"legible": True, "monto": 15000, "codigo_operacion": "OP1",
                             "banco_origen": "BancoEstado", "titular_origen": "X"}
        with patch.object(medilink, "crear_cita", _crear), \
             patch.object(flows, "listar_citas_paciente", _sin_citas), \
             patch.object(abono_comprobante, "leer_comprobante", fake):
            t = _txt(run(flows.procesar_imagen_abono(PHONE, b"x", "image/jpeg")))
        self.assertEqual(len(llamadas), 1)
        self.assertEqual(llamadas[0]["modalidad"], "PRESENCIAL")
        self.assertIn("botox en la frente", llamadas[0]["observaciones_extra"])
        self.assertIn("Estética Facial", t)
        self.assertNotIn("Psiquiatría", t)
        self.assertIn("diferencia", t)
        self.assertIn("gratis", t)
        self.assertIn("$15.000", t)
        self.assertNotIn("Saldo a pagar", t)
        self.assertNotIn("convenio", t.lower())

    def test_foto_con_monto_menor_no_crea_cita(self):
        async def _crear(**k):
            raise AssertionError("no debe crear la cita")

        save_session(PHONE, "WAIT_ABONO_COMPROBANTE", {
            "abono_gate_slot": dict(SLOT_EST), "abono_gate_paciente": dict(PACIENTE),
            "rut": PACIENTE["rut"], "abono_gate_ts": ""})
        import abono_comprobante
        fake = lambda b, c: {"legible": True, "monto": 10000, "codigo_operacion": "OP2",
                             "banco_origen": "BancoEstado"}
        with patch.object(medilink, "crear_cita", _crear), \
             patch.object(abono_comprobante, "leer_comprobante", fake):
            t = _txt(run(flows.procesar_imagen_abono(PHONE, b"x", "image/jpeg")))
        self.assertIn("$15.000", t)
        self.assertIn("Estética Facial", t)

    def test_correo_del_banco(self):
        import abono_transferencia as at
        llamadas, enviados = [], []

        async def _crear(**k):
            llamadas.append(k)
            return {"id": 1001}

        async def _enviar(phone, texto, *a, **k):
            enviados.append(texto)

        slot = {**SLOT_EST, "obs_cita": "[EVALUACIÓN ESTÉTICA $15.000 (se descuenta)] [INTERÉS: ojeras]",
                "estetica_interes": "ojeras"}
        link = at.crear_abono_pendiente(
            phone=PHONE, paciente_id=PACIENTE["id"], paciente_nombre=PACIENTE["nombre"],
            rut=PACIENTE["rut"], monto=15000, especialidad="Estética Facial",
            id_profesional=76, slot=slot)
        abono = at.get_abono_pendiente_activo_por_phone(PHONE)
        self.assertEqual(abono["monto"], 15000)
        import messaging
        with patch.object(medilink, "crear_cita", _crear), \
             patch.object(messaging, "send_whatsapp", _enviar):
            ok = run(at._crear_cita_y_confirmar(abono, 15000, "test", "OP3", "BancoEstado", "test"))
        self.assertTrue(ok)
        self.assertEqual(llamadas[0]["modalidad"], "PRESENCIAL")
        self.assertIn("ojeras", llamadas[0]["observaciones_extra"])
        t = " ".join(enviados)
        self.assertIn("Estética Facial", t)
        self.assertNotIn("Psiquiatría", t)
        self.assertIn("gratis", t)
        self.assertIn("diferencia", t)
        self.assertNotIn("Saldo a pagar", t)

    def test_psiquiatria_sigue_online_y_con_su_texto(self):
        import abono_transferencia as at
        llamadas, enviados = [], []

        async def _crear(**k):
            llamadas.append(k)
            return {"id": 1002}

        async def _enviar(phone, texto, *a, **k):
            enviados.append(texto)

        slot = {"especialidad": "Psiquiatría", "profesional": "Dra. Cecilia Unibazo",
                "id_profesional": 78, "fecha": "2099-03-12", "fecha_display": "Jueves 12",
                "hora_inicio": "10:00:00", "hora_fin": "10:40:00", "id_recurso": 1}
        at.crear_abono_pendiente(
            phone=PHONE + "p", paciente_id=1, paciente_nombre="Paciente Psiq", rut="11111111-1",
            monto=60000, especialidad="Psiquiatría", id_profesional=78, slot=slot)
        abono = at.get_abono_pendiente_activo_por_phone(PHONE + "p")
        import messaging
        with patch.object(medilink, "crear_cita", _crear), \
             patch.object(messaging, "send_whatsapp", _enviar):
            run(at._crear_cita_y_confirmar(abono, 60000, "test", "OP4", "Banco", "test"))
        self.assertEqual(llamadas[0]["modalidad"], "TELEMEDICINA")
        t = " ".join(enviados)
        self.assertIn("Psiquiatría", t)
        self.assertIn("Saldo a pagar el día de la atención: $0 CLP", t)


class TestWaitlistYCancelacion(unittest.TestCase):
    def test_nota_de_waitlist_usa_texto_de_evaluacion(self):
        # La nota sale de la misma regla: no debe prometer "valor total".
        r = config.abono_regla(especialidad="estética facial")
        self.assertTrue(flows._abono_es_evaluacion(r))
        self.assertIn("diferencia", flows._abono_frase_valor(r))
        self.assertIn("24 horas", flows._abono_aviso_cancelacion(r))
        r2 = config.abono_regla(especialidad="psiquiatría")
        self.assertIn("valor total de la consulta", flows._abono_frase_valor(r2))
        self.assertEqual(flows._abono_aviso_cancelacion(r2), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
