#!/usr/bin/env python3
"""Motor de auditoría con checks enchufables del chatbot CMC.

Cada check es una función que devuelve una lista de findings; todos escriben a
la misma tabla `audit_findings` (app/audit_store) y se ven en /admin/auditoria.

Checks deterministas (SQL/dicts, sin Claude — baratos y exactos):
  precio   — deriva de precios: profesionales sin tarifa, Márquez mal valuado
  consent  — fallo silencioso de opt-in (registros con RUT pero 0 consents)
  agenda   — doble-booking en citas_cache (mismo prof/fecha/hora)
  leak     — número personal del Dr. filtrado en mensajes salientes
  finanzas — atenciones finalizadas sin pago en caja (fuga de ingresos)

El check de conversación (Claude) vive en conversation_audit_swarm.py y también
escribe a la misma tabla con check_name='conversacion'.

Uso:
  python scripts/audit_runner.py --all                 # todos los deterministas
  python scripts/audit_runner.py --check precio,leak
  python scripts/audit_runner.py --all --dry-run       # no escribe, solo imprime
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "app"))

try:
    from dotenv import load_dotenv
    load_dotenv(str(ROOT / ".env"))
except Exception:
    pass

from app.audit_store import store_findings  # noqa: E402
from app.session import _conn  # noqa: E402

_TODAY = datetime.now(ZoneInfo("America/Santiago")).strftime("%Y-%m-%d")
_MONTH = _TODAY[:7]


# ── precio: deriva de tarifas ─────────────────────────────────────────────────
def check_precio() -> list[dict]:
    findings: list[dict] = []
    try:
        from medilink import PROFESIONALES
        from flows import PRECIOS_SLOT
    except Exception as e:
        return [{"severity": "low", "category": "precio", "fix_type": "logic_review",
                 "issue": f"No se pudo importar tablas de precio: {e}",
                 "dedup_key": "precio|import_error"}]

    # Cada profesional activo debe tener una tarifa para que el bot pueda cotizar.
    for pid, info in PROFESIONALES.items():
        esp = (info.get("especialidad") or "").strip()
        if not esp or esp == "Masoterapia":  # masoterapia se resuelve por duración
            continue
        if esp not in PRECIOS_SLOT:
            findings.append({
                "severity": "medium", "category": "precio", "fix_type": "data_safe",
                "issue": f"{info.get('nombre','?')} ({esp}) no tiene tarifa en PRECIOS_SLOT — el bot no puede cotizar su consulta.",
                "target_hint": "flows.py PRECIOS_SLOT",
                "dedup_key": f"precio|sin_tarifa|{esp}",
            })

    # Márquez (id 13): su valor CAPI / particular debe ser $30.000, no $25.000.
    try:
        from agenda_routes import _CAPI_VALUE_BY_PROF, DEFAULT_CAPI_VALUE
        mq = _CAPI_VALUE_BY_PROF.get(13, DEFAULT_CAPI_VALUE)
        if int(mq) != 30000:
            findings.append({
                "severity": "high", "category": "precio", "fix_type": "data_safe",
                "issue": f"Dr. Márquez (id 13) tiene valor CAPI ${int(mq):,}, pero su consulta particular es $30.000. Subvalúa el Purchase de Meta y puede cotizar mal.".replace(",", "."),
                "evidence": f"_CAPI_VALUE_BY_PROF.get(13)={_CAPI_VALUE_BY_PROF.get(13)} · default={DEFAULT_CAPI_VALUE}",
                "target_hint": "agenda_routes.py _CAPI_VALUE_BY_PROF",
                "dedup_key": "precio|marquez|capi",
            })
    except Exception as e:
        # No silenciar: si el guard se rompe (rename de la tabla), avisar.
        findings.append({
            "severity": "low", "category": "precio", "fix_type": "logic_review",
            "issue": f"El guard de precio de Márquez no pudo leer la tabla CAPI: {e}. Revisar que el check siga apuntando al dict correcto.",
            "target_hint": "scripts/audit_runner.py check_precio",
            "dedup_key": "precio|guard_roto",
        })
    return findings


# ── consent: fallo silencioso de opt-in (Ley 21.719) ──────────────────────────
def check_consent() -> list[dict]:
    con = _conn()
    try:
        c24 = con.execute(
            "SELECT COUNT(*) FROM privacy_consents WHERE consented_at >= datetime('now','-1 day')"
        ).fetchone()[0]
        reg24 = con.execute(
            "SELECT COUNT(*) FROM contact_profiles WHERE rut IS NOT NULL AND rut != '' "
            "AND updated_at >= datetime('now','-1 day')"
        ).fetchone()[0]
    finally:
        con.close()
    if reg24 >= 3 and c24 == 0:
        return [{
            "severity": "high", "category": "consentimiento", "fix_type": "logic_review",
            "issue": f"{reg24} registros con RUT en 24h pero 0 consentimientos guardados — posible fallo silencioso del opt-in (Ley 21.719). Ya pasó el 2026-05-29 (0/798).",
            "evidence": f"registros_24h={reg24} · consents_24h={c24}",
            "target_hint": "flows.py opt-in / session.save_privacy_consent",
            "dedup_key": f"consent|silencioso|{_TODAY}",
        }]
    return []


# ── agenda: doble-booking ─────────────────────────────────────────────────────
def check_agenda() -> list[dict]:
    con = _conn()
    try:
        rows = con.execute("""
            SELECT id_prof, fecha, hora_inicio, COUNT(*) n,
                   GROUP_CONCAT(paciente_nombre, ' / ') nombres
            FROM citas_cache
            WHERE fecha >= date('now')
            GROUP BY id_prof, fecha, hora_inicio
            HAVING COUNT(*) > 1
            LIMIT 50
        """).fetchall()
    finally:
        con.close()
    findings = []
    for r in rows:
        findings.append({
            "severity": "high", "category": "agenda", "fix_type": "logic_review",
            "issue": f"Doble-booking: {r['n']} citas en el mismo slot (prof {r['id_prof']}, {r['fecha']} {r['hora_inicio']}).",
            "evidence": (r["nombres"] or "")[:200],
            "target_hint": "medilink.py reserva / citas_cache",
            "dedup_key": f"agenda|{r['id_prof']}|{r['fecha']}|{r['hora_inicio']}",
        })
    return findings


# ── leak: número personal del Dr. en mensajes salientes ───────────────────────
_PERSONAL_RE = re.compile(r"(?:\+?56)?\s*9?\s*8\s*7\s*8\s*3\s*4\s*1\s*4\s*8|987834148")


def check_leak() -> list[dict]:
    con = _conn()
    try:
        rows = con.execute(
            "SELECT phone, text, ts, wamid FROM messages "
            "WHERE direction = 'out' AND ts >= datetime('now','-7 days') "
            "AND text LIKE '%8783%' LIMIT 200"
        ).fetchall()
    finally:
        con.close()
    findings = []
    for r in rows:
        txt = r["text"] or ""
        if _PERSONAL_RE.search(txt.replace(" ", "")):
            findings.append({
                "severity": "high", "category": "leak", "fix_type": "logic_review",
                "phone": r["phone"],
                "issue": "El número personal del Dr. (+56987834148) apareció en un mensaje saliente. Debe ser el bot +56966610737 o el fijo (44) 296 5226.",
                "evidence": txt[:160],
                "target_hint": "messaging.py _final_phone_guard / claude_helper _scrub_telefonos",
                "dedup_key": f"leak|{r['wamid'] or (r['phone'] + (r['ts'] or ''))}",
            })
    return findings


# ── finanzas: atenciones finalizadas sin pago en caja ─────────────────────────
def check_finanzas() -> list[dict]:
    con = _conn()
    try:
        # Excluir FONASA: el bono se paga por Imed, no por caja → no es fuga.
        # Quedan particulares/sin convenio que SÍ deberían registrar pago en caja.
        row = con.execute("""
            SELECT COUNT(*) n, COALESCE(SUM(a.total), 0) monto
            FROM bi_atenciones a
            LEFT JOIN bi_pagos_caja p ON p.atencion_id = a.atencion_id
            WHERE a.finalizado = 1
              AND a.fecha >= date('now','-30 days')
              AND p.pago_id IS NULL
              AND (a.nombre_convenio IS NULL OR a.nombre_convenio NOT LIKE '%FONASA%')
        """).fetchone()
    except Exception as e:
        con.close()
        return [{"severity": "low", "category": "finanzas", "fix_type": "logic_review",
                 "issue": f"No se pudo correr conciliación: {e}", "dedup_key": "finanzas|err"}]
    finally:
        try:
            con.close()
        except Exception:
            pass
    n, monto = row["n"], int(row["monto"] or 0)
    if n >= 5:  # umbral para no alarmar por casos sueltos (Fonasa bono, etc.)
        return [{
            "severity": "medium", "category": "finanzas", "fix_type": "logic_review",
            "issue": f"{n} atenciones particulares finalizadas en 30d sin pago en caja (monto bruto ${monto:,}). Excluye FONASA. Revisar posibles atenciones no cobradas (factor 0.85 BI vs Caja).".replace(",", "."),
            "evidence": f"particulares_sin_pago={n} · monto_bruto={monto}",
            "target_hint": "bi_atenciones vs bi_pagos_caja · auditor.py",
            "dedup_key": f"finanzas|particular_sin_pago|{_MONTH}",
        }]
    return []


CHECKS = {
    "precio": check_precio,
    "consent": check_consent,
    "agenda": check_agenda,
    "leak": check_leak,
    "finanzas": check_finanzas,
}


def main() -> int:
    ap = argparse.ArgumentParser(description="Motor de auditoría CMC (checks enchufables)")
    ap.add_argument("--all", action="store_true", help="corre todos los checks deterministas")
    ap.add_argument("--check", default="", help="lista separada por comas: " + ",".join(CHECKS))
    ap.add_argument("--dry-run", action="store_true", help="no escribe, solo imprime")
    args = ap.parse_args()

    if args.all:
        names = list(CHECKS)
    else:
        names = [n.strip() for n in args.check.split(",") if n.strip()]
    if not names:
        print("Nada que correr. Usa --all o --check precio,leak,...", file=sys.stderr)
        return 2

    total_found = total_new = 0
    for name in names:
        fn = CHECKS.get(name)
        if not fn:
            print(f"[runner] check desconocido: {name}", file=sys.stderr)
            continue
        try:
            findings = fn()
        except Exception as e:
            print(f"[runner] {name} ERROR: {e}", file=sys.stderr)
            continue
        total_found += len(findings)
        new = store_findings(name, findings, dry_run=args.dry_run)
        total_new += new
        tag = "DRY" if args.dry_run else f"+{new} nuevos"
        print(f"[runner] {name}: {len(findings)} hallazgos ({tag})")
        for f in findings:
            print(f"    [{f.get('severity','?')}] {f.get('issue','')[:140]}")

    print(f"[runner] total: {total_found} hallazgos · {total_new} nuevos guardados"
          + (" (dry-run)" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
