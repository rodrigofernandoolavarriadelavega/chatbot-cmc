"""Regresión: interactive.body.text de WhatsApp excedía el límite real de Meta
(1024 chars) y el mensaje NUNCA se entregaba (400 #131009, no-retry).

Caso real 2026-09-25 (56956326820): respuesta FAQ larga sobre diabetes +
oferta de hora armada con `_btn_msg` (flows.py) llegó a ~1300 chars de body.
`send_whatsapp` (texto plano) ya partía mensajes largos en chunks de 4000
(FIX-8), pero `send_whatsapp_interactive` no validaba nada — el payload salía
tal cual a Meta y moría en un 400 irreversible (ver `_post_meta`: 4xx != 429
no reintenta). El paciente nunca recibió el mensaje.

Ejecución:
    PYTHONPATH=app:. venv/bin/python3 tests/test_bugs_2026_09_25_messaging.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT))

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-used")

import messaging  # noqa: E402


class TestInteractiveBodyLimit(unittest.TestCase):
    """send_whatsapp_interactive debe recortar body.text a <=1024 chars antes
    de mandarlo a Meta, nunca dejar pasar un payload que Meta va a rechazar."""

    def setUp(self):
        self.sent_payloads = []

        async def _fake_post_meta(payload):
            self.sent_payloads.append(payload)
            return "wamid.fake"

        self._patcher = mock.patch.object(messaging, "_post_meta", _fake_post_meta)
        self._patcher.start()
        self.addCleanup(self._patcher.stop)

    def test_body_largo_se_recorta_bajo_el_limite(self):
        """Caso real: respuesta FAQ diabetes (~1300 chars) + botones."""
        # Reconstruye el mismo largo que produjo el 400 real (resp + preview +
        # pregunta), sin depender de Claude.
        resp = (
            "*Diabetes y prediabetes* — en el CMC tenemos al *Dr. Raúl Paz*, "
            "nutriólogo y diabetólogo, que evalúa y trata:\n\n"
            "✓ Diabetes tipo 1 y tipo 2 (desde 15 años)\n"
            "✓ Prediabetes y evaluación del riesgo\n"
            "✓ Dificultad para controlar el azúcar\n"
            "✓ Insulinoterapia y ajuste de medicamentos\n"
            "✓ Bombas de insulina y sensores de glucosa\n"
            "✓ Sobrepeso y obesidad asociada a diabetes\n"
            "✓ Colesterol y triglicéridos alterados\n"
            "✓ Hígado graso y síndrome metabólico\n"
            "✓ Diabetes en el embarazo\n"
            "✓ Seguimiento después de cirugía bariátrica\n\n"
            "💊 *Cómo funciona:* por videollamada (desde tu casa), 30 min, "
            "*$60.000* particular. El Dr. Paz evalúa tu caso, pide exámenes "
            "si necesita, y ajusta tu tratamiento o medicamentos.\n\n"
            "⏰ Atiende *miércoles 17:30–20:00* (5 cupos por semana). Los "
            "cupos se llenan rápido.\n\n"
            "⚠️ *Para agendar se paga el 100% por adelantado* ($60.000) — "
            "así reservamos tu cupo. El día de la atención no pagas nada más.\n\n"
            "🤔 Si lo que necesitas es un *plan alimentario* (qué comer y en "
            "qué cantidad), eso es *Nutrición con Gisela Pinto* — es más "
            "económico ($20.000 particular o bono Fonasa $4.770) y "
            "presencial aquí en el CMC.\n\n"
            "¿Te gustaría agendar con el Dr. Paz o prefieres empezar con "
            "nutrición? 😊"
        )
        preview = "📅 *Miércoles 30 de septiembre* · 🕐 *18:00* · Dr. Raúl Paz"
        body_text = f"{resp}\n\nPróxima hora disponible en *nutriología y diabetología*:\n{preview}\n\n¿Te la reservo?"
        self.assertGreater(len(body_text), 1024, "el fixture debe reproducir el caso real (>1024 chars)")

        interactive = {
            "type": "button",
            "body": {"text": body_text},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": "agendar_sugerido", "title": "✅ Sí, agendar"}},
                    {"type": "reply", "reply": {"id": "no_agendar", "title": "No por ahora"}},
                ]
            },
        }

        asyncio.run(messaging.send_whatsapp_interactive("56956326820", interactive))

        self.assertEqual(len(self.sent_payloads), 1)
        sent_body = self.sent_payloads[0]["interactive"]["body"]["text"]
        self.assertLessEqual(len(sent_body), 1024,
                              "el body enviado a Meta debe respetar el límite real de 1024 chars")
        # Botones intactos, no tocados por el recorte
        self.assertEqual(
            self.sent_payloads[0]["interactive"]["action"]["buttons"],
            interactive["action"]["buttons"],
        )

    def test_body_corto_no_se_toca(self):
        interactive = {
            "type": "button",
            "body": {"text": "¿Confirmas tu hora?"},
            "action": {"buttons": [{"type": "reply", "reply": {"id": "si", "title": "Sí"}}]},
        }
        asyncio.run(messaging.send_whatsapp_interactive("56900000000", interactive))
        self.assertEqual(self.sent_payloads[0]["interactive"]["body"]["text"], "¿Confirmas tu hora?")

    def test_recorte_no_deja_asterisco_colgando(self):
        """Si el corte cae justo dentro de una negrita abierta con *, el
        helper debe cerrar/quitar el asterisco suelto — nunca mandar markdown roto."""
        body_text = "normal " * 200 + "*negrita sin cerrar hasta el final"
        cut = messaging._safe_interactive_body(body_text)
        self.assertLessEqual(len(cut), 1024)
        self.assertEqual(cut.count("*") % 2, 0, "no debe quedar un * suelto tras recortar")


if __name__ == "__main__":
    unittest.main(verbosity=2)
