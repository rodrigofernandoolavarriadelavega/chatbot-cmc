"""
Regresión — consolidado 2026-08-24, hallazgo #5: el bot confirmaba un slot
distinto del ofrecido/pedido sin avisar (ej. Dr. Abarca → Dr. Olavarría;
sábado 16:15 → lunes 13:30).

Root cause identificado: `data["prof_pedido_explicito"]` (fix previo, caso
56988694763) evita reservar con OTRO doctor cuando el paciente pidió uno
específico — pero solo validaba el PROFESIONAL, no la HORA. Si el paciente
pedía "Dr Rodrigo a las 13:00" y había un slot de Dr. Rodrigo pero a OTRA
hora, se confirmaba esa otra hora en silencio.

Fix:
- `_responder_pregunta_horario` ahora también extrae la hora explícita del
  texto (`time_parser.parse_hora`) y la guarda en
  `data["hora_pedida_explicita"]`.
- El consumidor de `prof_pedido_explicito` (en WAIT_SLOT, rama
  confirmar_sugerido/afirmaciones) ahora exige que el slot coincida en
  profesional Y hora si se pidió una hora explícita; si no hay coincidencia,
  avisa explícitamente en vez de confirmar otra cosa.

Uso:
    PYTHONPATH=app:. python tests/test_slot_pedido_explicito_hora_2026_08_24.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT))

TMP_DB = Path(tempfile.mkdtemp(prefix="cmc_test_slot_pedido_")) / "test_sessions.db"
os.environ["SESSIONS_DB"] = str(TMP_DB)
os.environ.setdefault("SQLCIPHER_KEY", "")

import session  # noqa: E402
session.DB_PATH = TMP_DB

import medilink as medilink_mod  # noqa: E402
medilink_mod.buscar_primer_dia = AsyncMock(return_value=([], []))
medilink_mod.buscar_slots_dia = AsyncMock(return_value=[])
medilink_mod.buscar_paciente = AsyncMock(return_value=None)
medilink_mod.crear_cita = AsyncMock(return_value={"id": 5555})
import messaging as messaging_mod  # noqa: E402
messaging_mod.send_whatsapp = AsyncMock(return_value="wamid.TEST")

import claude_helper as claude_helper_mod  # noqa: E402


async def _fake_classify(mensaje, state, session_data):
    return {"action": "continue"}


claude_helper_mod.classify_with_context = _fake_classify
import flows as flows_mod  # noqa: E402
flows_mod.classify_with_context = _fake_classify

_CHILE_TZ = ZoneInfo("America/Santiago")


def _fecha_futura(dias=3):
    return (datetime.now(_CHILE_TZ) + timedelta(days=dias)).strftime("%Y-%m-%d")


def _slot(hora, id_profesional, profesional, fecha=None):
    return {
        "fecha": fecha or _fecha_futura(),
        "fecha_display": fecha or _fecha_futura(),
        "hora_inicio": hora,
        "hora_fin": hora,
        "especialidad": "Medicina General",
        "profesional": profesional,
        "id_profesional": id_profesional,
        "id": 900,
    }


def _body_text(resp):
    if isinstance(resp, dict):
        inter = resp.get("interactive", {})
        return inter.get("body", {}).get("text", "")
    return resp


class TestHoraPedidaExplicita(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        with session.db() as conn:
            conn.execute("DELETE FROM sessions")
            conn.execute("DELETE FROM conversation_events")
            conn.commit()

    async def test_slot_exacto_disponible_confirma_directo(self):
        """Pidió Dr. Olavarría (id 1) a las 13:00 y SÍ hay ese slot exacto
        entre los mostrados -> confirma sin pedir nada más."""
        phone = "56900000801"
        slots = [_slot("13:00", 1, "Dr. Rodrigo Olavarría"),
                 _slot("15:00", 1, "Dr. Rodrigo Olavarría")]
        data = {
            "slots": slots, "todos_slots": slots,
            "especialidad": "medicina general",
            "prof_pedido_explicito": 1,
            "hora_pedida_explicita": "13:00",
        }
        resp = await flows_mod.handle_message(
            phone, "confirmar_sugerido", {"state": "WAIT_SLOT", "data": data})
        texto = _body_text(resp)
        self.assertIn("13:00", texto)
        self.assertIn("Olavarría", texto)

    async def test_hora_pedida_no_existe_avisa_no_confirma_silencio(self):
        """Pidió Dr. Olavarría a las 13:00 pero el único slot de Olavarría
        mostrado es a las 15:00 -> debe AVISAR, no confirmar 15:00 solo."""
        phone = "56900000802"
        slots = [_slot("15:00", 1, "Dr. Rodrigo Olavarría"),
                 _slot("16:00", 73, "Dr. Andrés Abarca")]
        data = {
            "slots": slots, "todos_slots": slots,
            "especialidad": "medicina general",
            "prof_pedido_explicito": 1,
            "hora_pedida_explicita": "13:00",
        }
        resp = await flows_mod.handle_message(
            phone, "confirmar_sugerido", {"state": "WAIT_SLOT", "data": data})
        texto = _body_text(resp)
        self.assertIn("No encontré cupo", texto)
        self.assertIn("13:00", texto)
        # NO debe haber confirmado (nunca dice "cancelada"/"reservada" acá,
        # sigue en WAIT_SLOT esperando que el paciente decida)
        sess = session.get_session(phone)
        self.assertEqual(sess.get("state"), "WAIT_SLOT")

    async def test_sin_hora_pedida_mantiene_comportamiento_anterior(self):
        """Sin hora_pedida_explicita (caso viejo, solo profesional pedido),
        el primer slot del profesional se confirma igual que antes."""
        phone = "56900000803"
        slots = [_slot("15:00", 1, "Dr. Rodrigo Olavarría")]
        data = {
            "slots": slots, "todos_slots": slots,
            "especialidad": "medicina general",
            "prof_pedido_explicito": 1,
        }
        resp = await flows_mod.handle_message(
            phone, "confirmar_sugerido", {"state": "WAIT_SLOT", "data": data})
        texto = _body_text(resp)
        self.assertIn("15:00", texto)
        self.assertIn("Olavarría", texto)


class TestExtraccionHoraPedida(unittest.TestCase):
    def test_parse_hora_extrae_13_00(self):
        from time_parser import parse_hora
        self.assertEqual(parse_hora("a las 13:00"), (13, 0))

    def test_parse_hora_none_sin_hora(self):
        from time_parser import parse_hora
        self.assertIsNone(parse_hora("qué días atiende"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
