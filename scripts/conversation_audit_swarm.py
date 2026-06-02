#!/usr/bin/env python3
"""
Enjambre de auditoría horaria de conversaciones del chatbot CMC.

Corre por cron cada hora. Revisa las conversaciones de la última hora, las
audita con Claude buscando bugs de flujo, respuestas erróneas, fricción o
oportunidades comerciales perdidas, y clasifica cada hallazgo en:

  • data_safe    → fix de datos de bajo riesgo (typo al normalizador, intent al
                   cache, precio mal en un dict). Se ESTAGEA listo para aplicar.
  • logic_review → requiere criterio humano (cambio de flujo/lógica). Solo se
                   reporta.

REGLA DE SEGURIDAD: NO toca el servicio en vivo ni hace git commit/deploy
(ver memory/feedback_agentes_no_git_commit). Escribe:
  • tabla `audit_findings` en sessions.db (la lee el panel /admin)
  • reporte markdown en LOG_DIR (/var/log/cmc-audit por defecto)
  • staging de fixes seguros en LOG_DIR/pending_safe_fixes.jsonl (para tu revisión)

Uso:
  python scripts/conversation_audit_swarm.py                 # última hora (65 min)
  python scripts/conversation_audit_swarm.py --since-min 120
  python scripts/conversation_audit_swarm.py --dry-run       # no escribe ni notifica
  python scripts/conversation_audit_swarm.py --model claude-sonnet-4-6
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(str(ROOT / ".env"))
except Exception:
    pass

from app.session import _conn  # noqa: E402

LOG_DIR = Path(os.getenv("CMC_AUDIT_LOG_DIR", "/var/log/cmc-audit"))
DEFAULT_MODEL = os.getenv("CMC_AUDIT_MODEL", "claude-sonnet-4-6")
MAX_CONVERSATIONS = int(os.getenv("CMC_AUDIT_MAX_CONV", "40"))

# ── Conocimiento de dominio que el auditor DEBE tener presente ────────────────
# (Espejo del agente cmc-conversation-auditor; los gotchas que más bugs causan.)
AUDITOR_SYSTEM = """\
Eres un auditor senior del chatbot WhatsApp del Centro Médico Carampangue (CMC),
un centro médico rural en Carampangue/Arauco, Chile. Revisas conversaciones
REALES de producción para detectar fallas y proponer correcciones.

Contexto crítico del negocio (úsalo para juzgar):
- La mayoría de pacientes son Fonasa MLE N3, rurales, escriben con typos y
  chilenismos. "cancelar" a veces significa PAGAR, no anular una cita.
- El CMC tiene UNA sola sede. "Olavarría" es el apellido de un médico, NO un lugar.
- Precios de referencia: consulta MG particular $25.000 (Dr. Olavarría/Abarca),
  pero Dr. Alonso Márquez cobra $30.000 (medicina familiar). Bono Fonasa MG $7.880.
- El bot deriva a "recepción" (HUMAN_TAKEOVER) cuando no sabe responder.
- Teléfonos correctos: bot WA +56966610737, fijo (44) 296 5226. El número
  personal +56987834148 NUNCA debe aparecer en mensajes al paciente.
- NO se debe prometer sucursales, viajes, ni decir "certificados/habilitados
  /acreditados/Superintendencia" (publicidad engañosa).

Qué buscar (no exhaustivo):
- Intent mal clasificado (el bot entendió otra cosa).
- Respuestas con info incorrecta (precio, profesional, especialidad, horario).
- Loops / menú repetido (señal de frustración del paciente).
- Derivación a humano cuando el bot podía responder solo (o viceversa).
- Oportunidad comercial perdida (paciente con intención de agendar que se fue).
- Leaks de datos sensibles o del número personal.
- Errores de tono o mensajes confusos.

Para CADA hallazgo decides fix_type:
- "data_safe": corrección de DATOS de bajo riesgo y reversible — agregar un typo a
  un diccionario de normalización, agregar una frase al cache de intents, corregir
  un precio en una tabla. NUNCA cambios de flujo/máquina de estados.
- "logic_review": cualquier cosa que toque lógica conversacional, máquina de
  estados, condiciones, o que tenga ambigüedad. Requiere criterio humano.

Ante la duda, clasifica como "logic_review". Es un bot médico: prefiere reportar
antes que auto-aplicar.

Devuelve SOLO un objeto JSON válido, sin texto alrededor, con esta forma:
{
  "findings": [
    {
      "phone": "<phone de la conversación>",
      "severity": "low|medium|high",
      "category": "<intent|precio|derivacion|loop|comercial|leak|tono|otro>",
      "issue": "<qué salió mal, 1-2 frases>",
      "evidence": "<cita textual breve del mensaje problemático>",
      "fix_type": "data_safe|logic_review",
      "suggested_fix": "<qué cambiarías, concreto>",
      "target_hint": "<archivo/dict probable, ej: claude_helper.py _TYPOS — o null si no sabes>"
    }
  ],
  "summary": "<1-2 frases del estado general de la hora>"
}
Si no hay nada que reportar, devuelve {"findings": [], "summary": "sin hallazgos"}.
"""


def _fetch_conversations(since_min: int) -> dict[str, list[dict]]:
    """Mensajes de la última ventana, agrupados por phone. Solo conversaciones
    con al menos un mensaje inbound (actividad real del paciente)."""
    con = _conn()
    try:
        rows = con.execute(
            """
            SELECT phone, direction, text, state, ts
            FROM messages
            WHERE ts >= datetime('now', ?)
            ORDER BY phone, id
            """,
            (f"-{since_min} minutes",),
        ).fetchall()
    finally:
        con.close()

    convos: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        convos[r["phone"]].append(dict(r))

    # Filtrar: descartar conversaciones sin inbound (solo notificaciones del bot).
    # NOTA: en la tabla `messages` la dirección es "in"/"out", NO inbound/outbound.
    return {
        ph: msgs
        for ph, msgs in convos.items()
        if any(m["direction"] == "in" for m in msgs)
    }


def _render_transcript(phone: str, msgs: list[dict]) -> str:
    lines = [f"### Conversación {phone}"]
    for m in msgs:
        who = "PACIENTE" if m["direction"] == "in" else "BOT"
        txt = (m["text"] or "").strip().replace("\n", " ")
        if len(txt) > 400:
            txt = txt[:400] + "…"
        lines.append(f"[{m['ts']}] ({m.get('state','')}) {who}: {txt}")
    return "\n".join(lines)


def _call_auditor(transcripts: str, model: str) -> dict:
    from anthropic import Anthropic

    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    resp = client.messages.create(
        model=model,
        max_tokens=4000,
        system=AUDITOR_SYSTEM,
        messages=[{
            "role": "user",
            "content": (
                "Audita estas conversaciones de la última hora. "
                "Responde SOLO el JSON especificado.\n\n" + transcripts
            ),
        }],
    )
    text = "".join(
        block.text for block in resp.content if getattr(block, "type", None) == "text"
    )
    return _parse_json(text)


def _parse_json(text: str) -> dict:
    """Extrae el primer objeto JSON del texto, tolerante a fences."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
    return {"findings": [], "summary": "parse_error", "_raw": text[:500]}


def _ensure_table() -> None:
    con = _conn()
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS audit_findings (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                run_ts        TEXT DEFAULT (datetime('now')),
                phone         TEXT,
                severity      TEXT,
                category      TEXT,
                issue         TEXT,
                evidence      TEXT,
                fix_type      TEXT,
                suggested_fix TEXT,
                target_hint   TEXT,
                status        TEXT DEFAULT 'open'
            )
        """)
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_run ON audit_findings(run_ts)"
        )
        con.commit()
    finally:
        con.close()


def _store_findings(findings: list[dict]) -> None:
    con = _conn()
    try:
        for f in findings:
            con.execute(
                """INSERT INTO audit_findings
                   (phone, severity, category, issue, evidence, fix_type,
                    suggested_fix, target_hint)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    f.get("phone"), f.get("severity"), f.get("category"),
                    f.get("issue"), f.get("evidence"), f.get("fix_type"),
                    f.get("suggested_fix"), f.get("target_hint"),
                ),
            )
        con.commit()
    finally:
        con.close()


def _write_report(findings: list[dict], summary: str, n_conv: int) -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y-%m-%d_%H%M")
    report = LOG_DIR / f"audit_{stamp}.md"
    safe = [f for f in findings if f.get("fix_type") == "data_safe"]
    logic = [f for f in findings if f.get("fix_type") != "data_safe"]
    lines = [
        f"# Auditoría conversaciones — {stamp} UTC",
        f"\nConversaciones revisadas: {n_conv} · Hallazgos: {len(findings)} "
        f"(seguros: {len(safe)}, revisión: {len(logic)})",
        f"\n**Resumen:** {summary}\n",
    ]
    for titulo, grupo in (("Fixes seguros (estaged)", safe),
                          ("Requieren tu revisión", logic)):
        lines.append(f"\n## {titulo} ({len(grupo)})")
        if not grupo:
            lines.append("_ninguno_")
        for f in grupo:
            lines.append(
                f"\n- **[{f.get('severity','?').upper()}] {f.get('category','?')}** "
                f"({f.get('phone','?')})\n"
                f"  - Problema: {f.get('issue','')}\n"
                f"  - Evidencia: _{f.get('evidence','')}_\n"
                f"  - Fix: {f.get('suggested_fix','')}\n"
                f"  - Dónde: `{f.get('target_hint') or '?'}`"
            )
    report.write_text("\n".join(lines), encoding="utf-8")

    # Estagear fixes seguros para aplicación manual de un toque
    if safe:
        staging = LOG_DIR / "pending_safe_fixes.jsonl"
        with staging.open("a", encoding="utf-8") as fh:
            for f in safe:
                fh.write(json.dumps({"run": stamp, **f}, ensure_ascii=False) + "\n")
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description="Enjambre de auditoría horaria CMC")
    ap.add_argument("--since-min", type=int, default=65,
                    help="ventana en minutos (default 65)")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--dry-run", action="store_true",
                    help="no escribe a la DB ni estagea; solo imprime")
    args = ap.parse_args()

    if not os.getenv("ANTHROPIC_API_KEY"):
        print("ERROR: falta ANTHROPIC_API_KEY (¿cargaste el .env?)", file=sys.stderr)
        return 2

    convos = _fetch_conversations(args.since_min)
    if not convos:
        print(f"[audit] sin conversaciones con actividad en {args.since_min} min")
        return 0

    # Limitar volumen por corrida (coste/latencia acotados)
    items = list(convos.items())[:MAX_CONVERSATIONS]
    transcripts = "\n\n".join(_render_transcript(ph, msgs) for ph, msgs in items)

    result = _call_auditor(transcripts, args.model)
    findings = result.get("findings", []) or []
    summary = result.get("summary", "")

    print(f"[audit] {len(items)} conversaciones · {len(findings)} hallazgos · {summary}")
    for f in findings:
        print(f"  [{f.get('severity','?')}] {f.get('fix_type','?')} "
              f"{f.get('category','?')} ({f.get('phone','?')}): {f.get('issue','')}")

    if args.dry_run:
        print("[audit] dry-run: no se escribió nada")
        return 0

    if findings:
        _ensure_table()
        _store_findings(findings)
        report = _write_report(findings, summary, len(items))
        print(f"[audit] reporte: {report}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
