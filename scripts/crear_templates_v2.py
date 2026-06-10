#!/usr/bin/env python3
"""Crea las plantillas prof_*_v2 en Meta: header IMAGE (flyer) + body de la v1.

Flujo por plantilla:
  1. Sube el flyer canónico vía Resumable Upload API → header_handle
  2. POST message_templates con HEADER IMAGE + BODY/FOOTER/BUTTONS de la biblioteca

Solo crea las v2 cuyo flyer canónico exista en
~/alma-image-runner/out/canonical/prof_<key>.png — correr de nuevo cuando el
batch de imágenes complete más piezas (se salta las ya creadas).

Uso:  venv/bin/python scripts/crear_templates_v2.py
"""
import json
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

from config import META_ACCESS_TOKEN  # noqa: E402
import os  # noqa: E402

WABA = os.getenv("META_WABA_ID", "")
APP_ID = "804421499380432"  # App Meta del CMC (CLAUDE.md)
CANON = Path.home() / "alma-image-runner" / "out" / "canonical"
GRAPH = "https://graph.facebook.com/v22.0"


def upload_handle(c: httpx.Client, png: Path) -> str:
    """Resumable Upload API → handle para example.header_handle."""
    data = png.read_bytes()
    r = c.post(f"{GRAPH}/{APP_ID}/uploads",
               params={"file_length": len(data), "file_type": "image/png",
                       "access_token": META_ACCESS_TOKEN})
    r.raise_for_status()
    session_id = r.json()["id"]  # "upload:XYZ"
    r = c.post(f"{GRAPH}/{session_id}",
               headers={"Authorization": f"OAuth {META_ACCESS_TOKEN}",
                        "file_offset": "0"},
               content=data)
    r.raise_for_status()
    return r.json()["h"]


def main() -> None:
    from autopilot.plantillas_profesionales import BIBLIOTECA, FOOTER, _BTN_DEFAULT

    hdr = {"Authorization": f"Bearer {META_ACCESS_TOKEN}"}
    with httpx.Client(timeout=60) as c:
        # v2 ya existentes en el WABA (para re-correr sin duplicar)
        r = c.get(f"{GRAPH}/{WABA}/message_templates",
                  params={"fields": "name,status", "limit": 250}, headers=hdr)
        existentes = {t["name"] for t in r.json().get("data", [])}

        ok = fail = skip = 0
        for t in BIBLIOTECA:
            v1 = t["template_name"]                      # prof_<key>_v1
            key = v1.removeprefix("prof_").removesuffix("_v1")
            v2 = f"prof_{key}_v2"
            png = CANON / f"prof_{key}.png"
            if v2 in existentes:
                skip += 1
                continue
            if not png.exists():
                print(f"… {v2:38s} sin flyer canónico aún — pendiente")
                continue
            try:
                handle = upload_handle(c, png)
            except Exception as e:  # noqa: BLE001
                print(f"✗ {v2:38s} upload falló: {e}")
                fail += 1
                continue
            payload = {
                "name": v2, "category": "MARKETING", "language": "es",
                "components": [
                    {"type": "HEADER", "format": "IMAGE",
                     "example": {"header_handle": [handle]}},
                    {"type": "BODY", "text": t["body"],
                     "example": {"body_text": [["María"]]}},
                    {"type": "FOOTER", "text": t.get("footer", FOOTER)},
                    {"type": "BUTTONS", "buttons": [
                        {"type": "QUICK_REPLY", "text": b}
                        for b in t.get("buttons", _BTN_DEFAULT)]},
                ],
            }
            r = c.post(f"{GRAPH}/{WABA}/message_templates", headers=hdr, json=payload)
            d = r.json()
            if r.status_code == 200 and d.get("id"):
                print(f"✓ {v2:38s} → {d.get('status')}")
                ok += 1
            else:
                err = d.get("error", {})
                print(f"✗ {v2:38s} {(err.get('error_user_msg') or err.get('message', ''))[:160]}")
                fail += 1
            time.sleep(1.5)
        print(f"\nRESUMEN v2: {ok} creadas · {skip} ya existían · {fail} fallidas")


if __name__ == "__main__":
    main()
