"""
Registra los 5 templates Meta de la campaña win-back en WhatsApp Business API.

Uso:
    cd ~/chatbot-cmc
    PYTHONPATH=app:. python3 scripts/register_winback_templates.py

Categoría: MARKETING (re-engagement de pacientes inactivos).
Variable: {{1}} = nombre del paciente.

Templates:
  1. winback_mg_v1            — cohorte general (Medicina General base)
  2. winback_podologia_v1     — podología
  3. winback_kinesiologia_v1  — kinesiología
  4. winback_nutricion_v1     — nutrición
  5. winback_mujer_v1         — matrona / ginecología

Tras registrar, Meta los pone en estado PENDING. Aprobación: 1-72 horas.
Una vez APPROVED, el bot puede usarlos vía send_whatsapp_template().

Reglas duras:
- NO usar "certificados/Superintendencia/acreditados/habilitados"
- Métodos pago médicos: efectivo o transferencia (sin tarjeta)
- Idioma: español chileno neutro, sin argentinismos ni emojis excesivos
- Número customer-facing: +56966610737 / fijo (44) 296 5226
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


TEMPLATES = [
    # 1. Cohorte general — Medicina General (la mayor: Abarca + Márquez)
    {
        "name": "winback_mg_v1",
        "language": "es",
        "category": "MARKETING",
        "components": [
            {
                "type": "BODY",
                "text": (
                    "Hola {{1}}, te saluda el Centro Médico Carampangue. "
                    "Hace varios meses no te visitamos. Si tienes algún tema de salud "
                    "pendiente, una revisión rutinaria o necesitas renovar recetas, "
                    "te invitamos a agendar una consulta con tu médico.\n\n"
                    "Atendemos Fonasa y particular en Carampangue y sucursal Olavarría.\n\n"
                    "Para agendar responde *AGENDAR*. Si no quieres recibir más "
                    "mensajes responde *BAJA*."
                ),
                "example": {"body_text": [["María"]]},
            },
        ],
    },

    # 2. Podología (Andrea Guevara)
    {
        "name": "winback_podologia_v1",
        "language": "es",
        "category": "MARKETING",
        "components": [
            {
                "type": "BODY",
                "text": (
                    "Hola {{1}}, te saluda el Centro Médico Carampangue. "
                    "Si tienes pendiente tu control de podología (uñas, callos, "
                    "hongos o cuidado preventivo), Andrea Guevara tiene horas "
                    "disponibles esta semana.\n\n"
                    "Tarifa $20.000 a $35.000 según tratamiento.\n\n"
                    "Para agendar responde *PODO*. Si no quieres recibir más "
                    "mensajes responde *BAJA*."
                ),
                "example": {"body_text": [["Carlos"]]},
            },
        ],
    },

    # 3. Kinesiología (Etcheverry)
    {
        "name": "winback_kinesiologia_v1",
        "language": "es",
        "category": "MARKETING",
        "components": [
            {
                "type": "BODY",
                "text": (
                    "Hola {{1}}, te saluda el Centro Médico Carampangue. "
                    "Si quedaste con sesiones pendientes de kinesiología o sentiste "
                    "un nuevo dolor (espalda, cuello, rodilla u hombro), Leonardo "
                    "Etcheverry tiene horas esta semana.\n\n"
                    "Bono Fonasa $7.830 o particular $20.000. Pack de 10 sesiones "
                    "con bono Fonasa $83.360.\n\n"
                    "Para agendar responde *KINE*. Si no quieres recibir más "
                    "mensajes responde *BAJA*."
                ),
                "example": {"body_text": [["José"]]},
            },
        ],
    },

    # 4. Nutrición (Burgos)
    {
        "name": "winback_nutricion_v1",
        "language": "es",
        "category": "MARKETING",
        "components": [
            {
                "type": "BODY",
                "text": (
                    "Hola {{1}}, te saluda el Centro Médico Carampangue. "
                    "Si te quedó pendiente tu control con la nutricionista, este "
                    "es buen momento para retomar. Javiera Burgos abre agenda "
                    "esta semana.\n\n"
                    "Bono Fonasa $4.770 o particular $20.000.\n\n"
                    "Para agendar responde *NUTRI*. Si no quieres recibir más "
                    "mensajes responde *BAJA*."
                ),
                "example": {"body_text": [["Patricia"]]},
            },
        ],
    },

    # 5. Mujer (matrona/ginecología) — incluye PAP y eco TV
    {
        "name": "winback_mujer_v1",
        "language": "es",
        "category": "MARKETING",
        "components": [
            {
                "type": "BODY",
                "text": (
                    "Hola {{1}}, te saluda el Centro Médico Carampangue. "
                    "¿Hace cuánto que no te haces tu PAP o tu control "
                    "ginecológico? Si pasaron más de 12 meses, agéndalo ahora — "
                    "toma 30 minutos.\n\n"
                    "También tenemos el Chequeo Mujer 30+ que incluye control con "
                    "matrona, PAP y ecografía transvaginal por $59.990 con Fonasa "
                    "o $69.990 particular.\n\n"
                    "Para agendar responde *MUJER*. Si no quieres recibir más "
                    "mensajes responde *BAJA*."
                ),
                "example": {"body_text": [["Carolina"]]},
            },
        ],
    },
]


def register_template(tpl: dict) -> dict:
    payload = {
        "name": tpl["name"],
        "language": tpl["language"],
        "category": tpl["category"],
        "components": tpl["components"],
    }
    try:
        r = httpx.post(API_URL, headers=HEADERS, json=payload, timeout=30)
        return {"name": tpl["name"], "status": r.status_code, "response": r.json()}
    except httpx.TimeoutException:
        return {"name": tpl["name"], "status": 0,
                "response": {"error": {"message": "Timeout"}}}


def main():
    print(f"Registrando {len(TEMPLATES)} templates win-back en WABA {WABA_ID}...\n")
    ok = 0
    fail = 0
    for tpl in TEMPLATES:
        result = register_template(tpl)
        status = result["status"]
        name = result["name"]
        resp = result["response"]
        if status == 200:
            tid = resp.get("id", "?")
            tpl_status = resp.get("status", "?")
            print(f"  OK  {name} → id={tid}  status={tpl_status}")
            ok += 1
        else:
            err = resp.get("error", {}).get("message", json.dumps(resp)[:160])
            print(f"  XX  {name} → HTTP {status}: {err}")
            fail += 1
    print(f"\nResultado: {ok} registrados, {fail} fallidos")
    print("Meta los pone en PENDING. Aprobación: 1-72 horas.")
    print("Verifica estado en Meta Business Suite > Templates.")


if __name__ == "__main__":
    main()
