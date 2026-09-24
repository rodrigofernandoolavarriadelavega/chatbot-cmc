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
import os
import re
import sys
from collections import Counter
from datetime import datetime, timedelta
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


# ══ Chequeos SISTÉMICOS (2026-09-24) ══════════════════════════════════════════
# Nacieron de dos bugs que el auditor de conversaciones no podía ver desde el
# chat: la eco mandando pacientes a lista de espera con sobrecupos libres, y el
# webhook que descartaba los estados de entrega de Meta en lote.
_BOT_LOG = Path(os.getenv("CMC_BOT_LOG", "/var/log/cmc-bot.log"))
_AUDIT_DIR = Path(os.getenv("CMC_AUDIT_LOG_DIR", "/var/log/cmc-audit"))
_HOY_UTC = datetime.now(ZoneInfo("UTC")).strftime("%Y-%m-%d")


def _log_desde(minutos: int) -> list[str]:
    corte = (datetime.now(ZoneInfo("UTC")) - timedelta(minutes=minutos)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(_BOT_LOG, encoding="utf-8", errors="replace") as fh:
            return [ln.rstrip("\n") for ln in fh if ln[:19] >= corte and ln[:2] == "20"]
    except FileNotFoundError:
        return []


def check_entrega() -> list[dict]:
    """Mensajes que figuran 'sent' aunque el paciente escribió DESPUÉS: el
    mensaje llegó, lo que se perdió es el estado → webhook descartando datos."""
    con = _conn()
    try:
        tot = con.execute("SELECT COUNT(*) FROM message_statuses "
                          "WHERE ts >= datetime('now','-24 hours')").fetchone()[0]
        perdidos = con.execute("""
            SELECT COUNT(*) FROM message_statuses s
            WHERE s.status = 'sent' AND s.ts >= datetime('now','-24 hours')
              AND EXISTS (SELECT 1 FROM messages m WHERE m.phone = s.phone
                          AND m.direction = 'in' AND m.ts > s.ts)""").fetchone()[0]
    finally:
        con.close()
    if tot >= 50 and perdidos / tot > 0.15:
        return [{"severity": "high", "category": "tecnico", "fix_type": "logic_review",
                 "issue": f"{perdidos} de {tot} mensajes (24h) quedaron en 'sent' aunque el paciente "
                          f"respondió después: se están perdiendo webhooks de estado de Meta.",
                 "evidence": f"perdidos={perdidos} total={tot} ({perdidos * 100 // tot}%)",
                 "target_hint": "app/main.py webhook (lotes de Meta) · session.upsert_message_status",
                 "dedup_key": f"entrega|estados_perdidos|{_HOY_UTC}"}]
    return []


def check_disponibilidad() -> list[dict]:
    """Especialidades mandando pacientes a 'no hay horas' en serie: agenda sin
    días abiertos en Medilink, o búsqueda rota. Operativamente urgente."""
    con = _conn()
    try:
        rows = con.execute("""
            SELECT lower(json_extract(meta, '$.especialidad')) esp, COUNT(*) n,
                   COUNT(DISTINCT phone) pacientes, MAX(ts) ult
            FROM conversation_events
            WHERE event = 'sin_disponibilidad' AND ts >= datetime('now','-24 hours')
            GROUP BY esp HAVING pacientes >= 3 ORDER BY pacientes DESC""").fetchall()
    finally:
        con.close()
    return [{"severity": "high" if r["pacientes"] >= 5 else "medium", "category": "disponibilidad",
             "fix_type": "logic_review",
             "issue": f"{r['pacientes']} pacientes recibieron 'no hay horas' de {r['esp'] or '?'} en 24h. "
                      f"Revisar si la agenda tiene días abiertos en Medilink y si hay sobrecupos que "
                      f"el bot no está ofreciendo.",
             "evidence": f"eventos={r['n']} pacientes={r['pacientes']} último={r['ult']}",
             "target_hint": "Medilink (agenda) · app/flows.py _iniciar_agendar · app/sobrecupo.py",
             "dedup_key": f"disponibilidad|{r['esp']}|{_HOY_UTC}"} for r in rows]


def check_log() -> list[dict]:
    """Errores del bot en la última hora agrupados por firma + búsquedas
    degradadas por 429 + crons que APScheduler descartó (misfire)."""
    lineas = _log_desde(65)
    firmas: Counter = Counter()
    degradadas = n429 = 0
    misfires: Counter = Counter()
    for ln in lineas:
        if "MEDILINK_429" in ln:
            n429 += 1
        if re.search(r"No se pudo (obtener|buscar)", ln):
            degradadas += 1
        m = re.search(r'Run time of job "([^"(]+)', ln)
        if m and "was missed" in ln:
            misfires[m.group(1).strip()] += 1
        if (" ERROR " in ln or " CRITICAL " in ln) and "429" not in ln and "saturado" not in ln.lower():
            cuerpo = re.sub(r"\d+", "#", ln[20:])[:150]
            firmas[cuerpo] += 1
    out = []
    for firma, n in firmas.most_common(5):
        if n >= 3:
            out.append({"severity": "medium", "category": "tecnico", "fix_type": "logic_review",
                        "issue": f"Error repetido ×{n} en la última hora: {firma}",
                        "evidence": firma, "target_hint": "",
                        "dedup_key": f"log|{firma[:100]}|{_HOY_UTC}"})
    if degradadas >= 10:
        out.append({"severity": "high", "category": "tecnico", "fix_type": "logic_review",
                    "issue": f"{degradadas} consultas a Medilink fallaron en la última hora "
                             f"({n429} respuestas 429): búsquedas de horas de pacientes quedan a medias.",
                    "evidence": f"degradadas={degradadas} 429={n429}",
                    "target_hint": "app/medilink.py _get · crons batch de pagos/atenciones",
                    "dedup_key": f"log|medilink_degradado|{_HOY_UTC}"})
    for job, n in misfires.items():
        out.append({"severity": "high", "category": "tecnico", "fix_type": "logic_review",
                    "issue": f"Cron '{job}' descartado por APScheduler ×{n} (misfire): no está corriendo.",
                    "evidence": job, "target_hint": "app/main.py add_job misfire_grace_time",
                    "dedup_key": f"log|misfire|{job}|{_HOY_UTC}"})
    return out


def check_portaviones() -> list[dict]:
    """La propia auditoría horaria dejó de correr (cron caído, script roto)."""
    cron_log = _AUDIT_DIR / "cron.log"
    try:
        edad_min = (datetime.now().timestamp() - cron_log.stat().st_mtime) / 60
    except FileNotFoundError:
        return []
    if edad_min > 130:
        return [{"severity": "high", "category": "tecnico", "fix_type": "logic_review",
                 "issue": f"La auditoría horaria de conversaciones no escribe hace {int(edad_min)} min.",
                 "evidence": str(cron_log), "target_hint": "crontab · scripts/conversation_audit_swarm.py",
                 "dedup_key": f"portaviones|detenido|{_HOY_UTC}"}]
    return []


CHECKS = {
    "precio": check_precio,
    "consent": check_consent,
    "agenda": check_agenda,
    "leak": check_leak,
    "finanzas": check_finanzas,
    "entrega": check_entrega,
    "disponibilidad": check_disponibilidad,
    "log": check_log,
    "portaviones": check_portaviones,
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
