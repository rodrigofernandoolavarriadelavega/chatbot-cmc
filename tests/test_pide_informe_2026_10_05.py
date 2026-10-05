"""Regresión caso real 2-oct 21:42 (…8335): pedía el INFORME de una eco ya hecha.

1) "envíen informe de ecotomografía" → el bot entendía agendar eco.
2) "necesito el resultado, no agendar una nueva" → time_parser leía "una" = 13:00
   y reservaba el slot de las 12:45.
3) "Soy Jacquelinne Vergara, …" → la confundía con la Ps. Jacquelinne Salas.

Uso: PYTHONPATH=app:. python tests/test_pide_informe_2026_10_05.py
"""
import os, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import AsyncMock
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app")); sys.path.insert(0, str(ROOT))
TMP = Path(tempfile.mkdtemp(prefix="cmc_inf1005_")) / "s.db"
os.environ["SESSIONS_DB"] = str(TMP); os.environ.setdefault("SQLCIPHER_KEY", "")
os.environ["SOBRECUPO_ENABLED"] = "false"
import session  # noqa: E402
session.DB_PATH = TMP
import medilink as _ml  # noqa: E402
S = [{"especialidad": "Ecografía", "profesional": "David Pardo", "id_profesional": 68, "fecha": "2030-10-08",
      "fecha_display": "Martes 8 de octubre", "hora_inicio": h, "hora_fin": h}
     for h in ("10:00", "10:15", "12:30", "12:45")]
_ml.buscar_primer_dia = AsyncMock(return_value=(S, S))
_ml.buscar_slots_dia = AsyncMock(return_value=(S, S))
_ml.buscar_paciente = AsyncMock(return_value=None)
_ml.listar_citas_paciente = AsyncMock(return_value=[])
import messaging as _msg  # noqa: E402
_msg.send_whatsapp = AsyncMock(return_value="wamid.T")
import flows  # noqa: E402
from time_parser import parse_hora  # noqa: E402


async def _continue(m, s, d):
    return {"action": "continue"}

flows.classify_with_context = _continue
flows.detect_intent = AsyncMock(return_value={"intent": "otro", "especialidad": None, "respuesta_directa": None})
flows.respuesta_faq = AsyncMock(return_value="info")


class T(unittest.IsolatedAsyncioTestCase):
    async def _say(self, p, t):
        return await flows.handle_message(p, t, session.get_session(p))

    async def test_pedir_informe_en_idle_va_a_recepcion(self):
        p = "56900020001"
        r = await self._say(p, "Necesito que me envíen informe de ecotomografia realizada el lunes 28")
        self.assertEqual(session.get_session(p)["state"], "HUMAN_TAKEOVER")
        self.assertIn("recepción", str(r))

    async def test_no_agendar_en_wait_slot_no_reserva(self):
        p = "56900020002"
        await self._say(p, "quiero una ecografia")
        await self._say(p, "abdominal")
        self.assertEqual(session.get_session(p)["state"], "WAIT_SLOT")
        r = await self._say(p, "Necesito el resultado de mi ecotomografia,  no agendar una nueva")
        self.assertEqual(session.get_session(p)["state"], "HUMAN_TAKEOVER")
        self.assertNotIn("Para reservar", str(r))

    def test_una_articulo_no_es_hora(self):
        for t in ("no agendar una nueva", "dame una", "quiero una hora", "una y otra vez"):
            self.assertIsNone(parse_hora(t), t)
        for t, e in (("a la una", (13, 0)), ("una y media", (13, 30)), ("la una en punto", (13, 0)),
                     ("una", (13, 0)), ("una de la tarde", (13, 0)), ("a las dos", (14, 0))):
            self.assertEqual(parse_hora(t), e, t)

    def test_paciente_que_se_presenta_no_es_profesional(self):
        for t in ("Soy Jacquelinne Vergara, necesito que me envíe resultados de informe",
                  "soy leonardo perez quiero hora", "Me llamo Gisela Muñoz"):
            self.assertIsNone(flows._detectar_apellido_profesional(t), t)
        self.assertEqual(flows._detectar_apellido_profesional("hora con la psicóloga Jacquelinne Salas"),
                         "jacquelinne salas")
        self.assertEqual(flows._detectar_apellido_profesional("soy paciente de la dra salas"),
                         "jacquelinne salas")

    def test_pedir_hora_para_ver_resultados_sigue_agendando(self):
        for t in ("quiero hora para mostrar los resultados", "agendar control con resultados de examenes",
                  "necesito hora con el dr olavarria para ver resultados",
                  "el tratamiento me dio buen resultado, quiero otra hora", "quiero una eco abdominal"):
            self.assertFalse(flows._pide_informe(t), t)


    async def test_borrar_datos_y_resultados_es_derecho_al_olvido(self):
        p = "56900020009"
        r = await self._say(p, "quiero borrar mis datos y mis resultados")
        self.assertNotEqual(session.get_session(p)["state"], "HUMAN_TAKEOVER")
        self.assertNotIn("informe/resultado", str(r))

    async def test_doctor_no_cae_en_pide_informe(self):
        doc = getattr(flows, "ADMIN_ALERT_PHONE", None)
        if not doc:
            self.skipTest("sin _doctor_phone en entorno de test")
        r = await flows.handle_message(doc, "el resultado del examen de hoy", {"state": "IDLE", "data": {}})
        self.assertNotIn("informe/resultado", str(r))


if __name__ == "__main__":
    unittest.main(verbosity=2)
