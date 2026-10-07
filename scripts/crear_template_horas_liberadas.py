"""Registra en Meta el template UTILITY `horas_liberadas_v1` (aviso de horas
liberadas a quien PIDIÓ hora de esa especialidad y no la consiguió; lo usa
jobs._job_horas_vacias_dia_siguiente fuera de la ventana de 24 h).

Fuente única: templates/whatsapp_templates/horas_liberadas_v1.json

Uso (en el VPS, con el .env de prod):
    python scripts/crear_template_horas_liberadas.py
"""
import json
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv()

ACCESS_TOKEN = os.getenv("META_ACCESS_TOKEN", "")
WABA_ID = os.getenv("META_WABA_ID", "")
if not ACCESS_TOKEN or not WABA_ID:
    print("ERROR: META_ACCESS_TOKEN y META_WABA_ID deben estar en .env")
    sys.exit(1)

TPL = Path(__file__).resolve().parents[1] / "templates" / "whatsapp_templates" / "horas_liberadas_v1.json"


def main() -> None:
    template = json.loads(TPL.read_text(encoding="utf-8"))
    r = httpx.post(f"https://graph.facebook.com/v22.0/{WABA_ID}/message_templates",
                   headers={"Authorization": f"Bearer {ACCESS_TOKEN}"},
                   json=template, timeout=30)
    print(r.status_code)
    print(json.dumps(r.json(), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
