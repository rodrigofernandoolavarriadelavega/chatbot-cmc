#!/usr/bin/env python3
"""
Dry-run de recordatorios para citas de recepción (piloto Márquez id=13).

Descarga las citas de los próximos 3 días desde Medilink para los profesionales
en RECORDATORIOS_RECEPCION_PROF_IDS, resuelve el celular de cada paciente y
muestra una tabla de a quién se le mandaría recordatorio, sin enviar nada.

Uso (desde la raíz del repo):
    python scripts/dry_run_recordatorios_recepcion.py
    python scripts/dry_run_recordatorios_recepcion.py --dias 5
    python scripts/dry_run_recordatorios_recepcion.py --prof 13 --dias 3

Requiere .env con MEDILINK_TOKEN, MEDILINK_BASE_URL, MEDILINK_SUCURSAL.
"""
import argparse
import asyncio
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

# Agregar app/ al path para importar módulos del bot
sys.path.insert(0, str(Path(__file__).parent.parent / "app"))

import httpx
from dotenv import load_dotenv

load_dotenv()

MEDILINK_BASE_URL = os.getenv("MEDILINK_BASE_URL", "https://api.medilink2.healthatom.com/api/v5")
MEDILINK_TOKEN    = os.getenv("MEDILINK_TOKEN", "")
MEDILINK_SUCURSAL = int(os.getenv("MEDILINK_SUCURSAL", "1"))

_HEADERS = {
    "Authorization": f"Bearer {MEDILINK_TOKEN}",
    "Content-Type":  "application/json",
    "Accept":        "application/json",
}


def _q(params: dict) -> str:
    return json.dumps(params)


def _normalizar_phone(cel_raw: str) -> str | None:
    """Normaliza un número de teléfono al formato WA (56XXXXXXXXX sin +)."""
    if not cel_raw:
        return None
    cel = str(cel_raw).strip().replace(" ", "").replace("-", "").replace("+", "")
    if cel.startswith("9") and len(cel) == 9:
        cel = "56" + cel
    if cel.startswith("56") and len(cel) == 11:
        return cel
    if len(cel) == 9:
        return "56" + cel
    return None


async def fetch_citas_prof(client: httpx.AsyncClient, id_prof: int, fecha: str) -> list[dict]:
    params = {
        "id_sucursal":      {"eq": MEDILINK_SUCURSAL},
        "id_profesional":   {"eq": id_prof},
        "fecha":            {"eq": fecha},
        "estado_anulacion": {"eq": 0},
    }
    r = await client.get(f"{MEDILINK_BASE_URL}/citas", params={"q": _q(params)}, headers=_HEADERS)
    if r.status_code != 200:
        print(f"  [warn] citas prof={id_prof} fecha={fecha}: HTTP {r.status_code}")
        return []
    return r.json().get("data", [])


async def fetch_celular(client: httpx.AsyncClient, id_pac: int) -> tuple[str | None, str]:
    """Retorna (phone_normalizado, fuente). Fuente: 'medilink_celular' o 'sin_celular'."""
    try:
        r = await client.get(f"{MEDILINK_BASE_URL}/pacientes/{id_pac}", headers=_HEADERS)
        if r.status_code == 200:
            p = r.json().get("data", {})
            if isinstance(p, list):
                p = p[0] if p else {}
            cel_raw = (
                p.get("celular")
                or p.get("telefono_movil")
                or p.get("telefono")
                or ""
            )
            phone = _normalizar_phone(str(cel_raw))
            if phone:
                return phone, "medilink_celular"
    except httpx.RequestError as e:
        print(f"  [warn] pac_id={id_pac} celular error: {e}")
    return None, "sin_celular"


async def check_en_citas_bot(id_cita: int) -> bool:
    """Verifica si la cita ya existe en citas_bot (fue agendada por el bot)."""
    try:
        from session import _conn
        with _conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM citas_bot WHERE id_cita=? LIMIT 1", (str(id_cita),)
            ).fetchone()
            return row is not None
    except Exception:
        return False


async def main():
    parser = argparse.ArgumentParser(description="Dry-run recordatorios recepción")
    parser.add_argument("--dias", type=int, default=3, help="Días a adelantar (default: 3)")
    parser.add_argument(
        "--prof", type=int, nargs="+", default=None,
        help="IDs de profesional (default: RECORDATORIOS_RECEPCION_PROF_IDS del .env)",
    )
    args = parser.parse_args()

    # Determinar prof_ids
    if args.prof:
        prof_ids = args.prof
    else:
        from config import RECORDATORIOS_RECEPCION_PROF_IDS
        prof_ids = RECORDATORIOS_RECEPCION_PROF_IDS

    from medilink import PROFESIONALES

    hoy = date.today()
    fechas = [(hoy + timedelta(days=d)).isoformat() for d in range(1, args.dias + 1)]

    print(f"\n{'='*70}")
    print(f"DRY-RUN recordatorios recepción — profesionales: {prof_ids}")
    print(f"Fechas: {fechas[0]} → {fechas[-1]}")
    print(f"{'='*70}\n")

    resultados = []

    async with httpx.AsyncClient(timeout=15) as client:
        for id_prof in prof_ids:
            prof_info = PROFESIONALES.get(id_prof, {})
            prof_nombre  = prof_info.get("nombre", f"Prof {id_prof}")
            especialidad = prof_info.get("especialidad", "?")

            print(f"--- Prof {id_prof}: {prof_nombre} ({especialidad}) ---")

            for fecha in fechas:
                citas_raw = await fetch_citas_prof(client, id_prof, fecha)
                print(f"  {fecha}: {len(citas_raw)} citas en Medilink")

                for c in citas_raw:
                    id_pac   = c.get("id_paciente")
                    id_cita  = c.get("id")
                    pac_nombre = (c.get("nombre_paciente") or "").strip()
                    hora_raw = (c.get("hora_inicio") or "")[:5]

                    if not id_pac or not id_cita:
                        continue

                    # Delay anti-429
                    await asyncio.sleep(0.4)

                    phone, phone_source = await fetch_celular(client, id_pac)
                    en_bot = await check_en_citas_bot(id_cita)

                    estado = "BOT (ya cubierta)" if en_bot else ("OK" if phone else "SIN CELULAR")

                    resultados.append({
                        "id_prof":       id_prof,
                        "prof_nombre":   prof_nombre,
                        "id_cita":       id_cita,
                        "id_paciente":   id_pac,
                        "paciente":      pac_nombre,
                        "fecha":         fecha,
                        "hora":          hora_raw,
                        "phone":         phone or "—",
                        "phone_source":  phone_source,
                        "estado":        estado,
                    })

                    print(
                        f"    {hora_raw} | {pac_nombre:<30} | "
                        f"{phone or '—':<14} | {phone_source:<18} | {estado}"
                    )

    # Resumen
    total       = len(resultados)
    enviaria    = [r for r in resultados if r["estado"] == "OK"]
    bot_ya      = [r for r in resultados if "BOT" in r["estado"]]
    sin_celular = [r for r in resultados if r["estado"] == "SIN CELULAR"]

    print(f"\n{'='*70}")
    print(f"RESUMEN")
    print(f"  Total citas encontradas: {total}")
    print(f"  Recibirían recordatorio:  {len(enviaria)}")
    print(f"  Ya cubiertas por el bot:  {len(bot_ya)}")
    print(f"  Sin celular en Medilink:  {len(sin_celular)}")
    print(f"\n{'='*70}")
    print("DETALLE — recibirían recordatorio:")
    print(f"  {'Nombre':<30} | {'Fecha':<12} | {'Hora':<6} | {'Phone':<14} | Fuente")
    print(f"  {'-'*30}-+-{'-'*12}-+-{'-'*6}-+-{'-'*14}-+-{'Fuente'}")
    for r in enviaria:
        print(
            f"  {r['paciente']:<30} | {r['fecha']:<12} | {r['hora']:<6} | "
            f"{r['phone']:<14} | {r['phone_source']}"
        )
    print(f"\n{'='*70}")
    print("NOTA: Este script NO envía mensajes. Es solo un preview.")
    print("Para activar: RECORDATORIOS_RECEPCION_ENABLED=true en .env del VPS.")
    print(f"{'='*70}\n")


if __name__ == "__main__":
    asyncio.run(main())
