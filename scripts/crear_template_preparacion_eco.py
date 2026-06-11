"""Registra en Meta el template UTILITY `preparacion_ecografia` (riel pre-examen).

Mensaje proactivo al paciente con ecografía agendada (David Pardo): cómo
prepararse según el tipo de eco. Texto clínico = mismo contenido conservador
de ecografias.prep_ayuno_eco() (fuente única de verdad del bot).

Uso:
    python scripts/crear_template_preparacion_eco.py
"""
import json
import os
import sys

import httpx
from dotenv import load_dotenv

load_dotenv()

ACCESS_TOKEN = os.getenv("META_ACCESS_TOKEN", "")
WABA_ID = os.getenv("META_WABA_ID", "")
if not ACCESS_TOKEN or not WABA_ID:
    print("ERROR: META_ACCESS_TOKEN y META_WABA_ID deben estar en .env")
    sys.exit(1)

API_URL = f"https://graph.facebook.com/v22.0/{WABA_ID}/message_templates"
HEADERS = {"Authorization": f"Bearer {ACCESS_TOKEN}"}

TEMPLATE = {
    "name": "preparacion_ecografia",
    "language": "es_CL",
    "category": "UTILITY",
    "components": [
        {
            "type": "BODY",
            "text": (
                "Hola {{1}} 👋 Te escribimos del *Centro Médico Carampangue* "
                "por tu *ecografía* del {{2}}.\n\n"
                "Para que el examen salga bien a la primera, la preparación "
                "depende del tipo:\n\n"
                "• *Abdominal / hígado / vesícula:* ayuno de 6 a 8 horas "
                "(puedes tomar agua y tus remedios).\n"
                "• *Renal / vejiga / próstata:* vejiga llena — toma harta agua "
                "1 hora antes y no orines hasta el examen.\n"
                "• *Tiroides / mama / partes blandas / testicular:* no requiere "
                "preparación.\n\n"
                "Si tienes dudas, responde este mensaje y te ayudamos 😊"
            ),
            "example": {"body_text": [["María", "viernes 12/06 a las 10:30"]]},
        },
    ],
}


def main() -> None:
    r = httpx.post(API_URL, headers=HEADERS, json=TEMPLATE, timeout=30)
    print(r.status_code)
    print(json.dumps(r.json(), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
