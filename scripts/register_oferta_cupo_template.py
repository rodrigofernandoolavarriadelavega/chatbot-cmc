"""Registra el template Meta `oferta_cupo` (Fase 4 — Alma operativa).

Invitación proactiva a un cupo liberado, con botón de respuesta rápida
"Tomar la hora". El botón permite aceptar incluso fuera de la ventana de 24h de
WhatsApp (donde el texto libre NO entrega). Al tocarlo, Meta envía `type=button`
con `button.text="Tomar la hora"`; el webhook lo convierte a texto y el handler
de aceptación de flows.py (operativa.maybe_accept_offer) lo resuelve.

Uso:
    PYTHONPATH=app:. python scripts/register_oferta_cupo_template.py

Requiere en .env: META_ACCESS_TOKEN (perm. whatsapp_business_management) + META_WABA_ID.
Queda en estado PENDING hasta que Meta lo apruebe (suele ser minutos para UTILITY).
Ref: https://developers.facebook.com/docs/whatsapp/business-management-api/message-templates
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
    "name": "oferta_cupo",
    "language": "es_CL",
    "category": "UTILITY",
    "components": [
        {
            "type": "BODY",
            # {{1}} nombre · {{2}} especialidad · {{3}} profesional · {{4}} fecha · {{5}} hora
            "text": (
                "Hola {{1}} 👋 ¡Se liberó una hora de *{{2}}* con {{3}} en el "
                "*Centro Médico Carampangue*!\n\n"
                "📅 *{{4}}* a las *{{5}}*\n\n"
                "Es por orden de llegada. Si la quieres, toca *Tomar la hora* y te "
                "la apartamos. Si no respondes o ya la tomó otra persona, sigues en "
                "tu lugar en la lista de espera."
            ),
            "example": {
                "body_text": [["María", "Cardiología", "Dr. Miguel Millán",
                               "lunes 9 de junio", "10:00"]]
            },
        },
        {
            "type": "BUTTONS",
            "buttons": [
                {"type": "QUICK_REPLY", "text": "Tomar la hora"},
            ],
        },
    ],
}


def main():
    print(f"Registrando template 'oferta_cupo' en WABA {WABA_ID}...")
    try:
        r = httpx.post(API_URL, headers=HEADERS, json=TEMPLATE, timeout=30)
        resp = r.json()
    except Exception as e:  # noqa: BLE001
        print(f"  ❌ error de red: {e}")
        sys.exit(1)
    if r.status_code == 200:
        print(f"  ✅ oferta_cupo → id={resp.get('id','?')} (status={resp.get('status','?')})")
        print("  Esperá la aprobación de Meta antes de poner USE_TEMPLATES=true en prod.")
    else:
        err = resp.get("error", {}).get("message", json.dumps(resp)[:200])
        print(f"  ❌ HTTP {r.status_code}: {err}")
        print("  Si ya existe, elimínalo en Meta Business Manager antes de re-registrar.")


if __name__ == "__main__":
    main()
