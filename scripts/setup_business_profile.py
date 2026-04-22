"""
Configura el perfil de WhatsApp Business del Centro Médico Carampangue.

Uso:
    PYTHONPATH=app:. python scripts/setup_business_profile.py

Requiere en .env:
    META_ACCESS_TOKEN
    META_PHONE_NUMBER_ID
"""
import os
import sys

import httpx
from dotenv import load_dotenv

load_dotenv()

ACCESS_TOKEN = os.getenv("META_ACCESS_TOKEN", "")
PHONE_NUMBER_ID = os.getenv("META_PHONE_NUMBER_ID", "")

if not ACCESS_TOKEN or not PHONE_NUMBER_ID:
    print("ERROR: META_ACCESS_TOKEN y META_PHONE_NUMBER_ID deben estar en .env")
    sys.exit(1)

API_URL = f"https://graph.facebook.com/v22.0/{PHONE_NUMBER_ID}/whatsapp_business_profile"

PROFILE = {
    "messaging_product": "whatsapp",
    "about": "Asistente de citas del Centro Médico Carampangue 🏥",
    "address": "Monsalve 102 esq. República, Carampangue, Biobío, Chile",
    "description": (
        "Centro Médico Carampangue — Atención médica integral en Carampangue.\n\n"
        "Agenda, cancela o consulta tus citas médicas directamente por WhatsApp.\n\n"
        "Especialidades: Medicina General, Traumatología, Odontología, Kinesiología, "
        "Ginecología, Cardiología, Gastroenterología, ORL, Nutrición, Psicología, "
        "Fonoaudiología, Matrona, Podología, Ecografía, Masoterapia, Estética Facial.\n\n"
        "📞 (44) 296 5226\n"
        "📍 Monsalve esq. República, Carampangue"
    ),
    "email": "",
    "websites": ["https://agentecmc.cl"],
    "vertical": "HEALTH",
}


def main():
    print(f"Configurando perfil de WhatsApp Business para {PHONE_NUMBER_ID}...\n")

    try:
        r = httpx.post(
            API_URL,
            headers={"Authorization": f"Bearer {ACCESS_TOKEN}"},
            json=PROFILE,
            timeout=30,
        )
        print(f"Status: {r.status_code}")
        print(f"Response: {r.json()}")

        if r.status_code == 200:
            print("\n✅ Perfil actualizado correctamente")
        else:
            print(f"\n❌ Error: {r.json().get('error', {}).get('message', 'Unknown')}")
    except httpx.TimeoutException:
        print("❌ Timeout al conectar con Meta API")


if __name__ == "__main__":
    main()
