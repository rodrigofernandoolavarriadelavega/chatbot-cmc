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
MAX_CONVERSATIONS = int(os.getenv("CMC_AUDIT_MAX_CONV", "60"))
# Conversaciones por llamada a Claude. Bajo para que el JSON de hallazgos no
# trunque por max_tokens (bug real: 40 convos → 6KB+ de findings → truncado).
CHUNK_SIZE = int(os.getenv("CMC_AUDIT_CHUNK", "12"))

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
- Psiquiatría (Dra. Unibazo): TELEconsulta, martes y jueves 16:00-20:00, cada
  40 min; exige ABONO de $60.000 antes de crear la hora — que el bot lo pida
  NO es un error. Gastroenterología (Dr. Quijano) existe y exige abono $35.000.
- Neurología (Dra. González) es telemedicina y atiende SOLO ≥15 años.
- Resultado de examen ≠ orden de examen: son flujos distintos, no confundirlos.
- El bot puede ofrecer fechas de OTRO día si no hay cupo hoy — eso no es bug;
  sí lo es ofrecer fechas a meses de distancia sin que el paciente pidiera esa
  fecha (hubo un bug así con "adulto mayor"→mayo; ya corregido el 19-ago).

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
        max_tokens=8000,
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


def _filtrar_nuevos(findings: list[dict]) -> list[dict]:
    """Solo los hallazgos cuyo dedup_key NO existe ya en audit_findings.
    Evita re-estagear en pending_safe_fixes.jsonl lo mismo cada hora — el
    archivo llegó a 1,6 MB de repetidos porque el staging ignoraba el dedup
    que store_findings SÍ aplica en la tabla."""
    from app.audit_store import _dedup_key
    if not findings:
        return []
    con = _conn()
    try:
        nuevos = []
        for f in findings:
            dk = _dedup_key("conversacion", f)
            row = con.execute(
                "SELECT 1 FROM audit_findings WHERE dedup_key = ? LIMIT 1", (dk,)
            ).fetchone()
            if not row:
                nuevos.append(f)
        return nuevos
    finally:
        con.close()


def _write_report(findings: list[dict], summary: str, n_conv: int,
                  ventana: str, staged: list[dict]) -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y-%m-%d_%H%M")
    report = LOG_DIR / f"audit_{stamp}.md"
    safe = [f for f in findings if f.get("fix_type") == "data_safe"]
    logic = [f for f in findings if f.get("fix_type") != "data_safe"]
    lines = [
        f"# Auditoría conversaciones — {stamp} UTC · ventana {ventana}",
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

    # Estagear SOLO fixes seguros nuevos (dedup) — antes se apendeaba todo cada
    # hora y pending_safe_fixes.jsonl creció 1,6 MB de repetidos.
    staged_safe = [f for f in staged if f.get("fix_type") == "data_safe"]
    if staged_safe:
        staging = LOG_DIR / "pending_safe_fixes.jsonl"
        with staging.open("a", encoding="utf-8") as fh:
            for f in staged_safe:
                fh.write(json.dumps({"run": stamp, **f}, ensure_ascii=False) + "\n")
    return report


# ── Consolidación: de N hallazgos crudos a problemas raíz accionables ─────────

CONSOLIDATOR_SYSTEM = """\
Eres el triager senior del chatbot del Centro Médico Carampangue. Recibes
hallazgos CRUDOS de auditorías horarias (muchos son el mismo problema raíz
reformulado). Tu trabajo: consolidarlos en una lista CORTA de problemas
distintos, ordenada por (frecuencia × severidad), accionable.

Reglas:
- Un problema raíz = una entrada, aunque tenga 80 hallazgos crudos.
- MÁXIMO 15 problemas (los más importantes); descripcion ≤ 3 frases y
  fix_concreto ≤ 2 frases — la salida completa debe caber en el límite de tokens.
- No inventes problemas que no estén en los datos.
- "posible_falso_positivo": marca true si los hallazgos sugieren que el auditor
  horario está confundido (p.ej. reporta como bug el abono de psiquiatría, que
  es política real del centro).

Devuelve SOLO JSON:
{
  "problemas": [
    {
      "titulo": "<corto y concreto>",
      "prioridad": 1|2|3,
      "n_hallazgos": <int>,
      "categorias": ["..."],
      "fix_type": "data_safe|logic_review",
      "descripcion": "<qué pasa, 2-3 frases, con el patrón común>",
      "fix_concreto": "<qué cambiar exactamente>",
      "target": "<archivo/dict probable o null>",
      "telefonos_ejemplo": ["..."],
      "posible_falso_positivo": false
    }
  ],
  "resumen": "<3-4 frases del estado de la semana>"
}
"""


def _fetch_findings(since_days: int) -> list[dict]:
    con = _conn()
    try:
        rows = con.execute(
            """SELECT phone, severity, category, issue, evidence, fix_type,
                      suggested_fix, target_hint, run_ts
               FROM audit_findings
               WHERE check_name = 'conversacion' AND status = 'open'
                 AND run_ts >= datetime('now', ?)
               ORDER BY run_ts""",
            (f"-{since_days} days",),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def _compactar_findings(findings: list[dict]) -> str:
    """Pre-agrupa localmente para que 1.500 hallazgos entren en una llamada:
    agrupa por (category, fix_type, primeras 8 palabras normalizadas del issue)
    y emite count + teléfonos + hasta 2 ejemplos por grupo."""
    grupos: dict[tuple, list[dict]] = defaultdict(list)
    for f in findings:
        issue_norm = re.sub(r"[^a-záéíóúñü ]", "", (f.get("issue") or "").lower())
        clave_texto = " ".join(issue_norm.split()[:8])
        grupos[(f.get("category"), f.get("fix_type"), clave_texto)].append(f)

    bloques = []
    # Grupos grandes primero — son los problemas sistémicos.
    for (cat, ft, _k), fs in sorted(grupos.items(), key=lambda kv: -len(kv[1])):
        tels = sorted({f.get("phone") or "?" for f in fs})
        ej = fs[0]
        b = [f"[{cat}|{ft}] ×{len(fs)} · tels({len(tels)}): {', '.join(tels[:6])}"]
        b.append(f"  issue: {ej.get('issue','')}")
        if ej.get("evidence"):
            b.append(f"  evidencia: {ej['evidence'][:180]}")
        if ej.get("suggested_fix"):
            b.append(f"  fix sugerido: {ej['suggested_fix'][:180]}")
        if ej.get("target_hint"):
            b.append(f"  target: {ej['target_hint']}")
        if len(fs) > 1:
            e2 = fs[len(fs) // 2]
            b.append(f"  otro ej: {e2.get('issue','')[:160]}")
        bloques.append("\n".join(b))
    return "\n\n".join(bloques)


def consolidar(since_days: int, model: str) -> dict:
    findings = _fetch_findings(since_days)
    if not findings:
        print(f"[consolidar] sin hallazgos abiertos en {since_days} días")
        return {}
    compacto = _compactar_findings(findings)
    # Cota dura de tamaño del prompt (~200k chars ≈ margen amplio)
    if len(compacto) > 180_000:
        compacto = compacto[:180_000] + "\n[TRUNCADO]"

    from anthropic import Anthropic
    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    resp = client.messages.create(
        model=model,
        max_tokens=20000,  # 8000 truncaba el JSON con ~1.400 hallazgos (19-ago)
        system=CONSOLIDATOR_SYSTEM,
        messages=[{"role": "user", "content":
                   f"Hallazgos crudos de los últimos {since_days} días "
                   f"({len(findings)} en total), pre-agrupados:\n\n{compacto}"}],
    )
    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    result = _parse_json(text)
    problemas = result.get("problemas", []) or []

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out_md = LOG_DIR / f"consolidado_{stamp}_{since_days}d.md"
    lines = [
        f"# Consolidado semanal — {stamp} · últimos {since_days} días",
        f"\nHallazgos crudos: {len(findings)} → problemas raíz: {len(problemas)}",
        f"\n**Resumen:** {result.get('resumen','')}\n",
    ]
    for i, p in enumerate(problemas, 1):
        fp = " ⚠️ posible falso positivo del auditor" if p.get("posible_falso_positivo") else ""
        lines.append(
            f"\n## {i}. [P{p.get('prioridad','?')}] {p.get('titulo','')}"
            f" — ×{p.get('n_hallazgos','?')}{fp}\n"
            f"- **Qué pasa:** {p.get('descripcion','')}\n"
            f"- **Fix:** {p.get('fix_concreto','')}\n"
            f"- **Dónde:** `{p.get('target') or '?'}` · tipo `{p.get('fix_type','?')}`\n"
            f"- **Ejemplos:** {', '.join(p.get('telefonos_ejemplo', [])[:5])}"
        )
    out_md.write_text("\n".join(lines), encoding="utf-8")
    out_json = LOG_DIR / f"consolidado_{stamp}_{since_days}d.json"
    out_json.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[consolidar] {len(findings)} hallazgos → {len(problemas)} problemas · {out_md}")
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description="Enjambre de auditoría de conversaciones CMC")
    ap.add_argument("--since-min", type=int, default=65,
                    help="ventana en minutos (default 65)")
    ap.add_argument("--since-days", type=int, default=None,
                    help="ventana en días (pisa --since-min; para barridos largos)")
    ap.add_argument("--max-conv", type=int, default=MAX_CONVERSATIONS,
                    help=f"tope de conversaciones por corrida (default {MAX_CONVERSATIONS})")
    ap.add_argument("--consolidar", action="store_true",
                    help="NO audita: consolida los hallazgos crudos de la tabla "
                         "en problemas raíz (usa --since-days, default 7)")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--dry-run", action="store_true",
                    help="no escribe a la DB ni estagea; solo imprime")
    args = ap.parse_args()

    if not os.getenv("ANTHROPIC_API_KEY"):
        print("ERROR: falta ANTHROPIC_API_KEY (¿cargaste el .env?)", file=sys.stderr)
        return 2

    if args.consolidar:
        consolidar(args.since_days or 7, args.model)
        return 0

    since_min = (args.since_days * 24 * 60) if args.since_days else args.since_min
    ventana = f"{args.since_days}d" if args.since_days else f"{since_min}min"
    convos = _fetch_conversations(since_min)
    if not convos:
        print(f"[audit] sin conversaciones con actividad en {ventana}")
        return 0

    # Limitar volumen por corrida (coste/latencia acotados) — AVISANDO el corte.
    items = list(convos.items())[:args.max_conv]
    if len(convos) > len(items):
        print(f"[audit] ADVERTENCIA: {len(convos)} conversaciones en la ventana, "
              f"solo se auditan {len(items)} (sube --max-conv para cubrir todo)")

    # Procesar en lotes para que el JSON de hallazgos no trunque por max_tokens.
    findings: list[dict] = []
    summaries: list[str] = []
    errores = 0
    for i in range(0, len(items), CHUNK_SIZE):
        chunk = items[i:i + CHUNK_SIZE]
        transcripts = "\n\n".join(_render_transcript(ph, msgs) for ph, msgs in chunk)
        try:
            result = _call_auditor(transcripts, args.model)
        except Exception as e:  # un lote caído no bota la corrida completa
            errores += 1
            print(f"[audit] lote {i // CHUNK_SIZE + 1} falló: {e}", file=sys.stderr)
            continue
        # Anti-alucinación: descartar hallazgos cuyo phone no está en el lote.
        phones_lote = {ph for ph, _ in chunk}
        for f in result.get("findings", []) or []:
            if f.get("phone") in phones_lote:
                findings.append(f)
            else:
                print(f"[audit] descartado hallazgo con phone fuera del lote: "
                      f"{f.get('phone')!r} — {f.get('issue','')[:80]}")
        if result.get("summary"):
            summaries.append(result["summary"])
    summary = " · ".join(summaries)

    print(f"[audit] {len(items)} conversaciones · {len(findings)} hallazgos "
          f"· lotes fallidos: {errores} · {summary}")
    for f in findings:
        print(f"  [{f.get('severity','?')}] {f.get('fix_type','?')} "
              f"{f.get('category','?')} ({f.get('phone','?')}): {f.get('issue','')}")

    if args.dry_run:
        print("[audit] dry-run: no se escribió nada")
        return 0

    if findings:
        # Staging: calcular los realmente-nuevos ANTES de insertarlos.
        nuevos = _filtrar_nuevos(findings)
        from app.audit_store import store_findings
        new = store_findings("conversacion", findings)
        report = _write_report(findings, summary, len(items), ventana, staged=nuevos)
        print(f"[audit] reporte: {report} · {new} nuevos en tabla · {len(nuevos)} estageados")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
