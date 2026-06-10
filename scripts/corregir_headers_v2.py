#!/usr/bin/env python3
"""Corrige el header IMAGE de las plantillas prof_*_v2 que quedaron con flyer corrido.

Contexto (2026-06-10): 13 v2 se crearon con la imagen de OTRO profesional por el
bug de lag del image-runner (la descarga traía la imagen del pedido anterior).
Meta NO permite editar plantillas en PENDING — este script revisa el estado y,
para cada una que ya salió de revisión (APPROVED/REJECTED/PAUSED), sube el flyer
canónico correcto y edita el componente HEADER vía POST /{template_id}.

Límite Meta: 1 edición por plantilla por 24 h (10/mes). Correr de nuevo si
alguna seguía PENDING. Idempotente: salta las ya corregidas (registro local).

Uso:  venv/bin/python scripts/corregir_headers_v2.py
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
APP_ID = "804421499380432"
CANON = Path.home() / "alma-image-runner" / "out" / "canonical"
GRAPH = "https://graph.facebook.com/v22.0"
HECHAS = Path(__file__).parent / ".headers_v2_corregidos.json"

# v2 con header corrido (creadas 2026-06-10 antes del remapeo de flyers)
CORRIDAS = [
    "prof_mg_abarca_v2", "prof_mg_olavarria_v2", "prof_mf_marquez_v2",
    "prof_cardio_millan_v2", "prof_gine_rejon_v2", "prof_orl_borrego_v2",
    "prof_eco_pardo_v2", "prof_odonto_jimenez_v2", "prof_endo_fredes_v2",
    "prof_estetica_fuentealba_v2", "prof_nutri_pinto_v2",
    "prof_psico_rodriguez_v2", "prof_matrona_gomez_v2",
]


def upload_handle(c: httpx.Client, png: Path) -> str:
    data = png.read_bytes()
    r = c.post(f"{GRAPH}/{APP_ID}/uploads",
               params={"file_length": len(data), "file_type": "image/png",
                       "access_token": META_ACCESS_TOKEN})
    r.raise_for_status()
    r = c.post(f"{GRAPH}/{r.json()['id']}",
               headers={"Authorization": f"OAuth {META_ACCESS_TOKEN}",
                        "file_offset": "0"}, content=data)
    r.raise_for_status()
    return r.json()["h"]


def main() -> None:
    hechas = set(json.loads(HECHAS.read_text())) if HECHAS.exists() else set()
    hdr = {"Authorization": f"Bearer {META_ACCESS_TOKEN}"}
    with httpx.Client(timeout=60) as c:
        r = c.get(f"{GRAPH}/{WABA}/message_templates",
                  params={"fields": "name,status,id,components", "limit": 250},
                  headers=hdr)
        por_nombre = {t["name"]: t for t in r.json().get("data", [])}
        for name in CORRIDAS:
            if name in hechas:
                continue
            t = por_nombre.get(name)
            if not t:
                print(f"? {name}: no existe en el WABA")
                continue
            if t["status"] == "PENDING":
                print(f"… {name}: aún PENDING — reintentar más tarde")
                continue
            key = name.removeprefix("prof_").removesuffix("_v2")
            png = CANON / f"prof_{key}.png"
            if not png.exists():
                print(f"✗ {name}: falta flyer canónico {png}")
                continue
            handle = upload_handle(c, png)
            comps = [comp for comp in t.get("components", [])
                     if comp.get("type") != "HEADER"]
            comps.insert(0, {"type": "HEADER", "format": "IMAGE",
                             "example": {"header_handle": [handle]}})
            r2 = c.post(f"{GRAPH}/{t['id']}", headers=hdr,
                        json={"components": comps})
            if r2.status_code == 200 and r2.json().get("success"):
                print(f"✓ {name}: header corregido (estaba {t['status']})")
                hechas.add(name)
                HECHAS.write_text(json.dumps(sorted(hechas), indent=1))
            else:
                print(f"✗ {name}: {r2.json().get('error', {}).get('message', r2.text)[:160]}")
            time.sleep(1.5)
    print(f"\ncorregidas {len(hechas)}/{len(CORRIDAS)}")


if __name__ == "__main__":
    main()
