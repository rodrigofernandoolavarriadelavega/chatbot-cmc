#!/usr/bin/env python3
"""
upload_templates_to_meta.py — Sube templates de WhatsApp a Meta Business API.

Modo por defecto: dry-run (muestra qué se enviaría sin hacer requests).
Usa --apply para ejecutar los POSTs reales.

Requiere en .env (o variables de entorno):
  META_ACCESS_TOKEN              — token permanente del System User
  WHATSAPP_BUSINESS_ACCOUNT_ID   — WABA ID (no el Phone Number ID)

Uso:
  python scripts/upload_templates_to_meta.py
  python scripts/upload_templates_to_meta.py --apply
  python scripts/upload_templates_to_meta.py --apply --file crosssell_dx_dm2.json
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

try:
    import httpx
except ImportError:
    print("ERROR: httpx no instalado. Ejecuta: pip install httpx")
    sys.exit(1)

try:
    from dotenv import load_dotenv
    # Busca .env en la raíz del repo (dos niveles arriba de scripts/)
    _repo_root = Path(__file__).resolve().parent.parent
    load_dotenv(_repo_root / ".env")
except ImportError:
    pass  # dotenv opcional — variables deben estar en el entorno


TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates" / "whatsapp_templates"
META_API_VERSION = "v19.0"
META_GRAPH_BASE = "https://graph.facebook.com"

# Nombres que ya tienen template aprobado — se omiten por defecto para no
# crear duplicados. Edita esta lista si necesitas forzar re-submit.
ALREADY_APPROVED = {
    "recordatorio_cita",
    "recordatorio_cita_2h",
    "postconsulta_seguimiento",
    "lista_espera_cupo",
    "informe_listo",
    "seguimiento_medico",
    "reactivacion_paciente",
    "crosssell_kine",
    "control_especialidad",
    "adherencia_kine",
    "sistema_recuperado",
    # templates BI winback (módulo winback.py) — tienen su propio flujo
    "winback_generico_sensible_v1",
    "winback_kinesiologia_v1",
    "winback_medicina_general_v1",
    "winback_odontologia_v1",
    "winback_one_shot_general_v1",
    "winback_otorrino_v1",
    "consent_marketing_v1",
}


def load_env() -> tuple[str, str]:
    token = os.getenv("META_ACCESS_TOKEN", "")
    waba_id = os.getenv("WHATSAPP_BUSINESS_ACCOUNT_ID", "")
    if not token:
        print("ERROR: META_ACCESS_TOKEN no está definido.")
        sys.exit(1)
    if not waba_id:
        print("ERROR: WHATSAPP_BUSINESS_ACCOUNT_ID no está definido.")
        sys.exit(1)
    return token, waba_id


def load_templates(single_file: str | None = None) -> list[tuple[Path, dict]]:
    """Carga todos los JSON del directorio (o uno solo si se especifica --file)."""
    if single_file:
        path = TEMPLATES_DIR / single_file
        if not path.exists():
            print(f"ERROR: archivo no encontrado: {path}")
            sys.exit(1)
        files = [path]
    else:
        files = sorted(TEMPLATES_DIR.glob("*.json"))

    results = []
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            results.append((f, data))
        except json.JSONDecodeError as e:
            print(f"ERROR JSON en {f.name}: {e}")
            sys.exit(1)
    return results


def validate_template(data: dict, filename: str) -> list[str]:
    """Validaciones básicas antes de enviar a Meta. Retorna lista de errores."""
    errors = []
    required = {"name", "language", "category", "components"}
    for field in required:
        if field not in data:
            errors.append(f"Campo obligatorio ausente: {field!r}")

    body_components = [c for c in data.get("components", []) if c.get("type") == "BODY"]
    if not body_components:
        errors.append("Sin componente BODY")
    else:
        body_text = body_components[0].get("text", "")
        if len(body_text) > 1024:
            errors.append(f"BODY supera 1024 chars ({len(body_text)})")
        # Verificar consistencia de variables
        import re
        vars_in_text = re.findall(r"\{\{(\d+)\}\}", body_text)
        if vars_in_text:
            max_var = max(int(v) for v in vars_in_text)
            example = body_components[0].get("example", {}).get("body_text", [[]])
            if example and len(example[0]) < max_var:
                errors.append(
                    f"Variables en body: {{{{ {max_var} }}}} pero example solo tiene "
                    f"{len(example[0])} valor(es)"
                )

    button_components = [c for c in data.get("components", []) if c.get("type") == "BUTTONS"]
    if button_components:
        buttons = button_components[0].get("buttons", [])
        if len(buttons) > 3:
            errors.append(f"Más de 3 botones ({len(buttons)})")
        for btn in buttons:
            text = btn.get("text", "")
            if len(text) > 20:
                errors.append(f"Botón '{text}' supera 20 chars ({len(text)})")

    return errors


def submit_template(client: "httpx.Client", token: str, waba_id: str, data: dict) -> dict:
    """POST a /v19.0/{WABA_ID}/message_templates. Retorna respuesta JSON."""
    url = f"{META_GRAPH_BASE}/{META_API_VERSION}/{waba_id}/message_templates"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    resp = client.post(url, json=data, headers=headers, timeout=30)
    return {"status_code": resp.status_code, "body": resp.json()}


def main():
    parser = argparse.ArgumentParser(description="Sube templates WhatsApp a Meta.")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Ejecutar POSTs reales. Sin este flag corre en dry-run.",
    )
    parser.add_argument(
        "--file",
        metavar="FILENAME",
        help="Subir solo este archivo (ej: crosssell_dx_dm2.json).",
    )
    parser.add_argument(
        "--include-approved",
        action="store_true",
        help="Incluir templates ya aprobados (normalmente se omiten).",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1.5,
        metavar="SECS",
        help="Segundos de pausa entre requests (default: 1.5).",
    )
    args = parser.parse_args()

    templates = load_templates(args.file)
    if not templates:
        print("No se encontraron templates JSON.")
        sys.exit(0)

    # Validar todos antes de enviar cualquiera
    print(f"Validando {len(templates)} template(s)...\n")
    validation_ok = True
    for path, data in templates:
        errors = validate_template(data, path.name)
        if errors:
            validation_ok = False
            print(f"  [FAIL] {path.name}")
            for err in errors:
                print(f"         - {err}")
        else:
            print(f"  [OK]   {path.name}")
    print()

    if not validation_ok:
        print("Corrige los errores antes de continuar.")
        sys.exit(1)

    if not args.apply:
        print("--- DRY-RUN (sin cambios en Meta) ---")
        print("Templates que se enviarían:\n")
        for path, data in templates:
            name = data.get("name", path.stem)
            if name in ALREADY_APPROVED and not args.include_approved:
                print(f"  SKIP  {name}  (ya aprobado)")
                continue
            body = next(
                (c["text"] for c in data["components"] if c.get("type") == "BODY"), ""
            )
            print(f"  POST  {name}  [{data.get('category')}]")
            print(f"        Body preview: {body[:120].replace(chr(10), ' ')}")
            print()
        print("Ejecuta con --apply para subir a Meta.")
        return

    # --- APPLY ---
    token, waba_id = load_env()
    print(f"Subiendo a WABA {waba_id}...\n")

    enviados = 0
    omitidos = 0
    errores = 0

    with httpx.Client() as client:
        for path, data in templates:
            name = data.get("name", path.stem)
            if name in ALREADY_APPROVED and not args.include_approved:
                print(f"  SKIP  {name}  (ya aprobado)")
                omitidos += 1
                continue
            print(f"  POST  {name} ...", end=" ", flush=True)
            try:
                result = submit_template(client, token, waba_id, data)
                sc = result["status_code"]
                body = result["body"]
                if sc in (200, 201):
                    tid = body.get("id", "?")
                    print(f"OK (id={tid})")
                    enviados += 1
                else:
                    error_msg = body.get("error", {}).get("message", str(body))
                    print(f"ERROR {sc}: {error_msg}")
                    errores += 1
            except Exception as e:
                print(f"EXCEPTION: {e}")
                errores += 1
            time.sleep(args.delay)

    print(f"\nResultado: {enviados} enviados, {omitidos} omitidos, {errores} errores.")
    if errores:
        sys.exit(1)


if __name__ == "__main__":
    main()
