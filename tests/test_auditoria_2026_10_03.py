"""Regresión — hallazgos del auditor 25-sep a 1-oct, verificados contra las
conversaciones reales. La IA se simula como "no entendió" (intent otro) para
probar que los caminos deterministas resuelven solos (el saldo API puede faltar).

Uso: PYTHONPATH=app:. python tests/test_auditoria_2026_10_03.py
"""
import os, sys, tempfile, unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app")); sys.path.insert(0, str(ROOT))
TMP = Path(tempfile.mkdtemp(prefix="cmc_aud1003_")) / "s.db"
os.environ["SESSIONS_DB"] = str(TMP); os.environ.setdefault("SQLCIPHER_KEY", "")
import session  # noqa: E402
session.DB_PATH = TMP
import medilink as _ml  # noqa: E402
_ml.buscar_primer_dia = AsyncMock(return_value=([], []))
_ml.buscar_slots_dia = AsyncMock(return_value=([], []))
_ml.buscar_paciente = AsyncMock(return_value=None)
_ml.listar_citas_paciente = AsyncMock(return_value=[])
import messaging as _msg  # noqa: E402
_msg.send_whatsapp = AsyncMock(return_value="wamid.T")
import flows  # noqa: E402
import claude_helper  # noqa: E402

_OTRO = {"intent": "otro", "especialidad": None, "respuesta_directa": None}


async def _continue(m, s, d):
    return {"action": "continue"}

flows.classify_with_context = _continue
flows.detect_intent = AsyncMock(return_value=_OTRO)
flows.respuesta_faq = AsyncMock(return_value="info")
F = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d")
SLOT = {"especialidad": "Medicina General", "profesional": "Dr. Andrés Abarca", "id_profesional": 73,
        "fecha": F, "fecha_display": "Lunes 5 de octubre", "hora_inicio": "10:00:00", "hora_fin": "10:15:00"}


class T(unittest.IsolatedAsyncioTestCase):
    async def test_asisto_confirma_recordatorio(self):
        fila = {"id_cita": "999001", "especialidad": "Medicina General",
                "profesional": "Dr. Andrés Abarca", "fecha": F, "hora": "18:15"}
        class _C:
            def fetchone(self): return fila
        class _Conn:
            def execute(self, *a, **k): return _C()
            def __enter__(self): return self
            def __exit__(self, *a): return False
        with patch("session.db", return_value=_Conn()), patch("flows.mark_cita_confirmation"), patch("flows.log_event"):
            r = await flows.handle_message("56900010001", "Asisto", {"state": "IDLE", "data": {}})
        self.assertIn("confirmada", str(r).lower())

    async def test_por_favor_acepta_la_hora(self):
        with patch("flows._slot_confirmed", AsyncMock(return_value="ok_slot")) as sc:
            r = await flows.handle_message("56900010002", "Por favor",
                                           {"state": "WAIT_SLOT", "data": {"especialidad": "medicina general",
                                            "slots": [SLOT], "todos_slots": [SLOT], "slot_sugerido": SLOT}})
        self.assertEqual(r, "ok_slot"); sc.assert_awaited()

    async def test_general_corto_lleva_a_medicina_general(self):
        self.assertEqual(flows._detectar_especialidad_en_texto("General"), "medicina general")

    async def test_linfatico_es_masoterapia(self):
        self.assertEqual(flows._detectar_especialidad_en_texto("De las gotitas linfaticss"), "masoterapia")

    async def test_anuncio_otra_especialidad_no_la_intercepta_el_prerouter(self):
        data = {"especialidad": "medicina general", "meta_slots_ofrecidos": [SLOT]}
        self.assertTrue(flows._es_respuesta_obvia_al_prompt(
            "Necesito saber si tienen Otorrino", "necesito saber si tienen otorrino", "WAIT_META_SLOT_CHOICE", data))
        self.assertTrue(flows._es_respuesta_obvia_al_prompt(
            "El diabetologo?", "el diabetologo?", "WAIT_META_SLOT_CHOICE", data))

    async def test_telemedicina_no_se_cae(self):
        with patch("flows.detect_intent", AsyncMock(return_value={"intent": "telemedicina", "especialidad": None,
                                                                  "respuesta_directa": None})):
            r = await flows.handle_message("56900010006", "Hora online", {"state": "IDLE", "data": {}})
        self.assertIn("videollamada", str(r).lower())
        self.assertNotIn("problema técnico", str(r).lower())

    async def test_reeng_si_retoma_la_hora(self):
        with patch("flows._slot_confirmed", AsyncMock(return_value="ok_slot")) as sc:
            r = await flows.handle_message("56900010007", "reeng_si",
                                           {"state": "WAIT_SLOT", "data": {"especialidad": "medicina general",
                                            "slots": [SLOT], "todos_slots": [SLOT], "slot_sugerido": SLOT}})
        self.assertEqual(r, "ok_slot"); sc.assert_awaited()

    def test_respuesta_informativa_no_propone_1(self):
        t = claude_helper._validar_respuesta_faq("Sí, tenemos otorrino. Escribe *1* o *agendar otorrinolaringología*.")
        self.assertNotIn("*1*", t); self.assertIn("*agendar otorrinolaringología*", t)

    async def test_general_tras_preguntar_especialidad(self):
        p = "56900010009"
        with patch("flows.detect_intent", AsyncMock(return_value={"intent": "disponibilidad", "especialidad": None,
                                                                  "respuesta_directa": None})):
            await flows.handle_message(p, "Buenas tardes tendrá horas medicas", {"state": "IDLE", "data": {}})
        self.assertEqual(session.get_session(p)["state"], "WAIT_ESPECIALIDAD")
        with patch("flows._iniciar_agendar", AsyncMock(return_value="AGENDAR")) as ia:
            r = await flows.handle_message(p, "General", session.get_session(p))
        self.assertEqual(r, "AGENDAR")
        self.assertEqual(_ml._ids_para_especialidad(ia.await_args.args[2]), [73, 1, 13])

    def test_formas_de_nombrar_especialidad(self):
        for e in ["médico general", "medica general", "doctor general", "medico familiar", "ginecologo",
                  "cardiologo", "traumatologo", "psicologo", "podóloga", "fonoaudiologo", "oculista", "ecografia"]:
            self.assertTrue(_ml._ids_para_especialidad(e), e)
        self.assertEqual(_ml.especialidad_canonica("médico general"), "medicina general")

    async def test_medico_general_no_dice_no_contamos(self):
        for e in ("médico general", "ginecologo"):
            with patch("flows.buscar_primer_dia", AsyncMock(return_value=([], []))):
                r = await flows._iniciar_agendar("56900010010", {}, e)
            self.assertNotIn("no contamos", str(r).lower(), e)

    def test_precio_fono_evaluacion(self):
        self.assertEqual(flows.PRECIOS_SLOT["Fonoaudiología"][1], 25000)  # dueño 2026-10-07

    def _lista(self):
        hs = ["09:00", "09:15", "09:30", "10:00", "10:30", "11:00", "11:30", "12:00", "12:30", "13:00",
              "13:30", "14:00", "14:15", "20:30", "20:45"]
        return [dict(SLOT, hora_inicio=h + ":00") for h in hs]

    async def test_numero_ambiguo_pregunta(self):
        l = self._lista()  # opción 14 = 20:30, pero hay hora a las 14:00
        r = await flows.handle_message("56900010011", "14",
                                       {"state": "WAIT_SLOT", "data": {"especialidad": "medicina general",
                                        "slots": l, "todos_slots": l}})
        t = str(r)
        self.assertIn("opc_idx:13", t); self.assertIn("hora_amb_si:14:00", t)

    async def test_boton_opcion_reserva_esa_opcion(self):
        l = self._lista()
        with patch("flows._slot_confirmed", AsyncMock(return_value="ok")) as sc:
            await flows.handle_message("56900010012", "opc_idx:13",
                                       {"state": "WAIT_SLOT", "data": {"especialidad": "medicina general",
                                        "slots": l, "todos_slots": l}})
        self.assertEqual(sc.await_args.args[2]["hora_inicio"][:5], "20:30")

    async def test_numero_sin_opcion_visible_es_hora(self):
        l = self._lista()
        mostrados = l[:5]  # solo 5 a la vista: "13" no es opción → hora 13:00/13:30
        r = await flows.handle_message("56900010013", "13",
                                       {"state": "WAIT_SLOT", "data": {"especialidad": "medicina general",
                                        "slots": mostrados, "todos_slots": l}})
        self.assertIn("13:00", str(r)); self.assertIn("13:30", str(r))

    async def test_numero_no_ambiguo_sigue_igual(self):
        l = self._lista()  # opción 3 = 09:30, no hay hora a las 03 → opción directa
        with patch("flows._slot_confirmed", AsyncMock(return_value="ok")) as sc:
            await flows.handle_message("56900010014", "3",
                                       {"state": "WAIT_SLOT", "data": {"especialidad": "medicina general",
                                        "slots": l, "todos_slots": l}})
        self.assertEqual(sc.await_args.args[2]["hora_inicio"][:5], "09:30")

    async def test_cancelar_con_abono_pendiente_pregunta_pagar_o_anular(self):
        with patch("abono_transferencia.get_abono_pendiente_activo_por_phone",
                   return_value={"id": 126, "estado": "pendiente"}):
            r = await flows.handle_message("56900010020", "Hoy es 3 y tengo que cancelar mi hora del día 6",
                                           {"state": "IDLE", "data": {}})
        t = str(r)
        self.assertIn("pagar_hora", t); self.assertNotIn("reagendar", t.lower())

    async def test_cancelar_con_plata_es_pagar(self):
        with patch("abono_transferencia.get_abono_pendiente_activo_por_phone", return_value=None):
            await flows.handle_message("56900010021", "Quiero cancelar los 60 mil de mi hora del día 6",
                                       {"state": "IDLE", "data": {}})
        self.assertEqual(session.get_session("56900010021")["state"], "HUMAN_TAKEOVER")

    async def test_pago_de_cita_que_ya_existe_no_busca_otra_hora(self):
        from datetime import datetime as _d
        from zoneinfo import ZoneInfo
        p = "56900010022"
        slot = {"especialidad": "Psiquiatría", "profesional": "Dra. Cecilia Unibazo", "id_profesional": 78,
                "fecha": F, "fecha_display": "Martes 6 de octubre", "hora_inicio": "16:40:00", "hora_fin": "17:20:00"}
        session.save_session(p, "WAIT_ABONO_COMPROBANTE", {
            "abono_gate_slot": slot, "abono_gate_paciente": {"id": 555, "nombre": "Juan P", "rut": "11111111-1"},
            "abono_gate_ts": _d.now(ZoneInfo("America/Santiago")).isoformat(), "rut": "11111111-1"})
        existente = [{"id": 65789, "id_profesional": 78, "fecha": F, "hora_inicio": "16:40"}]
        with patch("abono_comprobante.leer_comprobante", return_value={"legible": True, "monto": 60000,
                   "codigo": None, "banco_origen": "Itaú"}), \
             patch("flows.listar_citas_paciente", AsyncMock(return_value=existente)), \
             patch("medilink.crear_cita", AsyncMock(return_value=None)) as cc, \
             patch("medilink.buscar_primer_dia", AsyncMock(return_value=([dict(slot, fecha="2026-10-29")], []))):
            r = await flows.procesar_imagen_abono(p, b"img", "image/jpeg")
        cc.assert_not_awaited()
        self.assertNotIn("fue tomada", str(r))


if __name__ == "__main__":
    unittest.main(verbosity=2)
