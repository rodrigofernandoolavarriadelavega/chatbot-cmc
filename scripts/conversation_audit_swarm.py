#!/usr/bin/env python3
"""
Portaviones v2 — auditoría de conversaciones del chatbot CMC.

Tres etapas:

  1. HORARIA (cron :00) — audita las conversaciones de la última hora. Cada
     transcript va ENRIQUECIDO: mensajes + eventos internos del bot
     (conversation_events: horas ofrecidas, lista de espera, sobrecupos,
     reservas…) + líneas de log WARNING/ERROR de ese teléfono + contexto previo
     de la conversación. Antes el auditor solo veía el texto y no podía
     distinguir un "no hay horas" verdadero de uno causado por un bug
     (caso eco 2026-09-24: 12 sobrecupos libres y el bot mandaba a lista de
     espera — invisible desde el chat).

  2. CONSOLIDAR (--consolidar) — agrupa los hallazgos crudos en ≤15 problemas
     raíz con tendencia (primera/última vez, por día), cruzados con los
     chequeos deterministas (audit_runner) y los commits recientes, para
     marcar lo que probablemente ya se arregló.

  3. VERIFICAR (--verificar, tras consolidar) — por cada problema arma un
     paquete de evidencia (conversaciones, eventos, log, fragmentos del código
     REAL, commits) y un verificador da veredicto: REAL / FALSO_POSITIVO /
     YA_RESUELTO / DISENO / INCIERTO, con causa raíz en archivo:función.
     Los descartes con confianza alta cierran sus hallazgos en la tabla y los
     falsos positivos alimentan `aprendido.md`, que el auditor horario lee:
     el portaviones deja de repetir el mismo error.

El conocimiento del negocio sale de `scripts/audit_conocimiento.md` (curado) +
una ficha generada DESDE EL CÓDIGO en cada corrida (profesionales, precios,
abonos) + `aprendido.md`. Salidas estructuradas (JSON Schema) → sin parse_error.

SEGURIDAD: NO toca el servicio en vivo ni hace git commit/deploy. Escribe la
tabla `audit_findings`, reportes en LOG_DIR y el staging de fixes seguros.

Uso:
  python scripts/conversation_audit_swarm.py                     # última hora
  python scripts/conversation_audit_swarm.py --since-min 180 --dry-run
  python scripts/conversation_audit_swarm.py --consolidar --since-days 7 --verificar
  python scripts/conversation_audit_swarm.py --verificar-json /var/log/cmc-audit/consolidado_X.json
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "app"))

try:
    from dotenv import load_dotenv
    load_dotenv(str(ROOT / ".env"))
except Exception:
    pass

from app.session import _conn  # noqa: E402

LOG_DIR = Path(os.getenv("CMC_AUDIT_LOG_DIR", "/var/log/cmc-audit"))
BOT_LOG = Path(os.getenv("CMC_BOT_LOG", "/var/log/cmc-bot.log"))
CONOCIMIENTO_MD = ROOT / "scripts" / "audit_conocimiento.md"
APRENDIDO_MD = LOG_DIR / "aprendido.md"

# Modelos: el horario corre ~24×/día y COMPARTE la cuenta API con el bot.
# 25-sep-2026: con claude-opus-5 cada hora la recarga de US$20 duró 3,6 días y
# el bot quedó sin saldo. Comparación 25-sep sobre las mismas 41 conversaciones:
# Haiku 12 hallazgos (~5 bugs reales, varias falsas alarmas) vs Sonnet 5 en
# esfuerzo bajo 16 (~13 reales). Elegido por el dueño: Sonnet 5 low
# (~US$0,6-0,8/día). No subir modelo ni esfuerzo sin preguntarle.
DEFAULT_MODEL = os.getenv("CMC_AUDIT_MODEL", "claude-sonnet-5")
DEFAULT_EFFORT = os.getenv("CMC_AUDIT_EFFORT", "low")
DEEP_MODEL = os.getenv("CMC_AUDIT_DEEP_MODEL", "claude-opus-5")
DEEP_EFFORT = os.getenv("CMC_AUDIT_DEEP_EFFORT", "high")
MAX_CONVERSATIONS = int(os.getenv("CMC_AUDIT_MAX_CONV", "150"))
CHUNK_SIZE = int(os.getenv("CMC_AUDIT_CHUNK", "10"))
CONTEXTO_PREVIO = 6          # mensajes anteriores a la ventana, por teléfono

# Eventos que solo meten ruido en el transcript (telemetría, no conversación).
_EVENTOS_RUIDO = {
    "capi_send_ok", "intent_context", "proactive_contact", "template_enviado",
    "medfam_filtra_marquez_call", "persistencia_skip_recepcion_activa",
}
_LOG_TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) +(\w+)")


# ══════════════════════════════════════════════════════════════════════════════
#  Conocimiento
# ══════════════════════════════════════════════════════════════════════════════

def _ficha_desde_codigo() -> str:
    """Profesionales, precios y abonos tal como están HOY en el código — el
    auditor juzga contra la verdad vigente, no contra un prompt que envejece."""
    out = ["## Ficha generada desde el código (vigente)"]
    try:
        from medilink import PROFESIONALES
        out.append("### Profesionales (id · nombre · especialidad · min/cita · extras)")
        for pid, p in sorted(PROFESIONALES.items()):
            extras = {k: v for k, v in p.items()
                      if k not in ("nombre", "especialidad", "intervalo") and not callable(v)}
            ex = f" · {json.dumps(extras, ensure_ascii=False, default=str)[:160]}" if extras else ""
            out.append(f"- {pid} · {p.get('nombre')} · {p.get('especialidad')} · "
                       f"{p.get('intervalo')} min{ex}")
    except Exception as e:  # noqa: BLE001
        out.append(f"(no se pudo leer PROFESIONALES: {e})")
    try:
        from flows import PRECIOS_SLOT
        out.append("### Precios por especialidad (PRECIOS_SLOT: modalidad, fonasa/base, sufijo, particular)")
        for esp, t in PRECIOS_SLOT.items():
            out.append(f"- {esp}: {t}")
    except Exception as e:  # noqa: BLE001
        out.append(f"(no se pudo leer PRECIOS_SLOT: {e})")
    try:
        from config import ABONO_REGLAS
        out.append("### Abonos obligatorios (config.ABONO_REGLAS)")
        def _clp(n):
            return f"${n:,}".replace(",", ".") if isinstance(n, int) else str(n)
        for k, r in ABONO_REGLAS.items():
            out.append(f"- {r.get('etiqueta', k)}: abono {_clp(r.get('monto'))} · precio "
                       f"{_clp(r.get('precio'))} · profesionales {r.get('profesionales')} · "
                       f"gate_bot={r.get('gate_bot')}")
    except Exception as e:  # noqa: BLE001
        out.append(f"(no se pudo leer ABONO_REGLAS: {e})")
    flags = {k: os.getenv(k) for k in ("SOBRECUPO_ENABLED", "SOBRECUPO_MAX_DIA",
                                       "ECO_ORDEN_OCR_ACTIVE", "ABONO_GATE_PSIQ_ACTIVE")}
    out.append(f"### Flags relevantes: {json.dumps(flags)}")
    return "\n".join(out)


def _conocimiento() -> str:
    partes = []
    try:
        partes.append(CONOCIMIENTO_MD.read_text(encoding="utf-8"))
    except Exception:
        partes.append("(falta scripts/audit_conocimiento.md)")
    partes.append(_ficha_desde_codigo())
    try:
        if APRENDIDO_MD.exists():
            partes.append("## Aprendido por el verificador (descartes confirmados)\n"
                          + APRENDIDO_MD.read_text(encoding="utf-8")[-12000:])
    except Exception:
        pass
    return "\n\n".join(partes)


# ══════════════════════════════════════════════════════════════════════════════
#  Llamada al modelo (salida estructurada)
# ══════════════════════════════════════════════════════════════════════════════

class LLMError(RuntimeError):
    pass


def _output_config(model: str, effort: str, schema: dict) -> dict:
    """Haiku 4.5 rechaza `effort` con un 400; solo los modelos que lo soportan lo llevan."""
    cfg = {"format": {"type": "json_schema", "schema": schema}}
    if "haiku" not in model:
        cfg["effort"] = effort
    return cfg


def _llm_json(system: str, user: str, schema: dict, *, model: str, effort: str,
              max_tokens: int = 16000) -> dict:
    """Una llamada con JSON Schema garantizado. El SDK instalado (0.28) no
    conoce output_config: va por extra_body (verificado 2026-09-24).
    El system (conocimiento, estable entre lotes) va cacheado."""
    from anthropic import Anthropic
    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"], timeout=600)
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
        extra_body={"output_config": _output_config(model, effort, schema)},
    )
    if resp.stop_reason == "refusal":
        raise LLMError("refusal")
    if resp.stop_reason == "max_tokens":
        raise LLMError(f"max_tokens ({max_tokens}) — salida truncada")
    text = "".join(getattr(b, "text", "") for b in resp.content
                   if getattr(b, "type", None) == "text")
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise LLMError(f"JSON inválido: {e}: {text[:200]}") from e


def _obj(props: dict, req: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props,
            "required": req if req is not None else list(props), "additionalProperties": False}


_STR = {"type": "string"}


# ══════════════════════════════════════════════════════════════════════════════
#  Datos: conversaciones enriquecidas
# ══════════════════════════════════════════════════════════════════════════════

def _fetch_conversations(since_min: int) -> dict[str, list[dict]]:
    """Mensajes de la ventana agrupados por phone, solo con actividad real
    del paciente. En `messages` la dirección es "in"/"out"."""
    con = _conn()
    try:
        rows = con.execute(
            "SELECT phone, direction, text, state, ts FROM messages "
            "WHERE ts >= datetime('now', ?) ORDER BY phone, id",
            (f"-{since_min} minutes",),
        ).fetchall()
    finally:
        con.close()
    convos: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        convos[r["phone"]].append(dict(r))
    return {ph: ms for ph, ms in convos.items() if any(m["direction"] == "in" for m in ms)}


def _contexto_y_eventos(phones: list[str], desde_ts: str, hasta_mas: int = 0
                        ) -> tuple[dict[str, list[dict]], dict[str, list[dict]]]:
    """Mensajes previos a la ventana (contexto) y eventos del bot por phone."""
    previos: dict[str, list[dict]] = {}
    eventos: dict[str, list[dict]] = defaultdict(list)
    con = _conn()
    try:
        for ph in phones:
            rows = con.execute(
                "SELECT direction, text, state, ts FROM messages WHERE phone=? AND ts < ? "
                "ORDER BY id DESC LIMIT ?", (ph, desde_ts, CONTEXTO_PREVIO)).fetchall()
            previos[ph] = [dict(r) for r in reversed(rows)]
            for r in con.execute(
                    "SELECT event, meta, ts FROM conversation_events WHERE phone=? "
                    "AND ts >= datetime(?, '-10 minutes') ORDER BY id", (ph, desde_ts)):
                if r["event"] not in _EVENTOS_RUIDO:
                    eventos[ph].append(dict(r))
    finally:
        con.close()
    return previos, eventos


def _leer_log(desde: datetime, incluir_rotados: bool = False) -> list[str]:
    """Líneas del log del bot (UTC) desde `desde`. Con incluir_rotados lee
    también .1 y .N.gz (para el verificador, ventana de días)."""
    archivos = [BOT_LOG]
    if incluir_rotados:
        archivos = sorted(BOT_LOG.parent.glob(BOT_LOG.name + ".*"), reverse=True) + [BOT_LOG]
    corte = desde.strftime("%Y-%m-%d %H:%M:%S")
    out: list[str] = []
    for f in archivos:
        try:
            op = gzip.open if f.suffix == ".gz" else open
            with op(f, "rt", encoding="utf-8", errors="replace") as fh:
                for ln in fh:
                    m = _LOG_TS.match(ln)
                    if m and m.group(1) >= corte:
                        out.append(ln.rstrip("\n"))
        except FileNotFoundError:
            continue
        except Exception as e:  # noqa: BLE001
            print(f"[log] no se pudo leer {f}: {e}", file=sys.stderr)
    return out


def _log_por_phone(lineas: list[str], phones: set[str], solo_problemas: bool) -> dict[str, list[str]]:
    res: dict[str, list[str]] = defaultdict(list)
    for ln in lineas:
        if "httpx" in ln:
            continue
        m = _LOG_TS.match(ln)
        if solo_problemas and (not m or m.group(2) not in ("WARNING", "ERROR", "CRITICAL")):
            continue
        for ph in phones:
            if ph in ln:
                res[ph].append(ln[:260])
    return res


def _errores_sistema(lineas: list[str]) -> str:
    """Firmas de WARNING/ERROR de la ventana (sin teléfonos), para que el
    auditor sepa si Medilink estaba saturado cuando el bot dijo 'no hay horas'."""
    firmas: Counter = Counter()
    for ln in lineas:
        m = _LOG_TS.match(ln)
        if not m or m.group(2) not in ("WARNING", "ERROR", "CRITICAL") or "httpx" in ln:
            continue
        cuerpo = ln[len(m.group(0)):]
        cuerpo = re.sub(r"\d{6,}", "<n>", cuerpo)
        cuerpo = re.sub(r"https?://\S+", "<url>", cuerpo)
        cuerpo = re.sub(r"\b\d+\b", "#", cuerpo)
        firmas[f"{m.group(2)} {cuerpo.strip()[:140]}"] += 1
    if not firmas:
        return "Sin WARNING/ERROR en el log de la ventana."
    return "\n".join(f"- ×{n} {f}" for f, n in firmas.most_common(15))


def _render(phone: str, msgs: list[dict], previos: list[dict], eventos: list[dict],
            log_ph: list[str]) -> str:
    lines = [f"### Conversación {phone}"]
    if previos:
        lines.append("(contexto previo, ya auditado antes — no reportar sobre él)")
        for m in previos:
            who = "PACIENTE" if m["direction"] == "in" else "BOT"
            lines.append(f"  [{m['ts']}] ({m.get('state') or ''}) {who}: "
                         f"{(m['text'] or '').strip().replace(chr(10), ' ')[:200]}")
        lines.append("(fin contexto)")
    items = [("m", m["ts"], m) for m in msgs] + [("e", e["ts"], e) for e in eventos]
    for kind, ts, x in sorted(items, key=lambda t: t[1] or ""):
        if kind == "m":
            who = "PACIENTE" if x["direction"] == "in" else "BOT"
            txt = (x["text"] or "").strip().replace("\n", " ")
            lines.append(f"[{ts}] ({x.get('state') or ''}) {who}: {txt[:500]}")
        else:
            meta = (x.get("meta") or "").replace("\n", " ")
            lines.append(f"[{ts}] ⚙ evento {x['event']} {meta[:180]}")
    if log_ph:
        lines.append("Log (WARNING/ERROR de este teléfono):")
        lines.extend(f"  {ln}" for ln in log_ph[-12:])
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
#  Etapa 1: auditoría horaria
# ══════════════════════════════════════════════════════════════════════════════

AUDITOR_INSTRUCCIONES = """\
Eres el auditor senior del chatbot WhatsApp del Centro Médico Carampangue.
Revisas conversaciones REALES de producción de la última hora. Cada una trae el
chat, los EVENTOS internos del bot (líneas "⚙ evento") y las líneas de log con
problemas de ese teléfono. Úsalos: muchos bugs solo se ven cruzando lo que el
bot dijo con lo que el sistema hizo por dentro (p.ej. "no hay horas" justo
después de un 429 de Medilink, o lista de espera con sobrecupos disponibles).

Reporta solo lo que tenga evidencia en el material. Antes de reportar, revisa
la sección "Diseños DELIBERADOS" y "Falsos positivos ya vistos" del
conocimiento: si calza, NO lo reportes. Precios, abonos y profesionales:
compáralos con la ficha generada desde el código, no con tu intuición.

Qué buscar: intent mal entendido · dato incorrecto (precio, abono, profesional,
horario, especialidad) · loop o menú repetido · derivación a humano innecesaria
o faltante · paciente con intención de agendar que se fue sin hora (y por qué) ·
disponibilidad mal informada · leak de datos o del número personal · tono
(incluido voseo) · mensaje confuso o contradictorio · fallas técnicas visibles
en eventos/log que afectaron al paciente.

fix_type: "data_safe" SOLO para datos de bajo riesgo (typo al normalizador,
frase al cache de intents, precio en una tabla); todo lo que toque lógica es
"logic_review". Ante la duda, logic_review.
target_hint: SOLO nombres del "Mapa REAL del código"; si no sabes, "".
confianza "baja" si depende de algo que no ves en el material.
Si no hay nada, findings vacío. Escribe en español de Chile, sin voseo."""

_FINDING_SCHEMA = _obj({
    "phone": _STR,
    "severity": {"type": "string", "enum": ["low", "medium", "high"]},
    "category": {"type": "string", "enum": ["intent", "precio", "disponibilidad", "derivacion",
                                             "loop", "comercial", "leak", "tono", "tecnico", "otro"]},
    "issue": _STR,
    "evidence": _STR,
    "evidence_source": {"type": "string", "enum": ["transcript", "eventos", "log", "mixta"]},
    "confianza": {"type": "string", "enum": ["alta", "media", "baja"]},
    "root_cause_hypothesis": _STR,
    "fix_type": {"type": "string", "enum": ["data_safe", "logic_review"]},
    "suggested_fix": _STR,
    "target_hint": _STR,
})
AUDIT_SCHEMA = _obj({"findings": {"type": "array", "items": _FINDING_SCHEMA}, "summary": _STR})


def _filtrar_nuevos(findings: list[dict]) -> list[dict]:
    """Solo hallazgos cuyo dedup_key NO existe ya en audit_findings (el staging
    llegó a 1,6 MB de repetidos cuando ignoraba el dedup)."""
    from app.audit_store import _dedup_key
    if not findings:
        return []
    con = _conn()
    try:
        return [f for f in findings if not con.execute(
            "SELECT 1 FROM audit_findings WHERE dedup_key = ? LIMIT 1",
            (_dedup_key("conversacion", f),)).fetchone()]
    finally:
        con.close()


def _registrar_salud(problema: str, detalle: str, dry_run: bool) -> None:
    """Una auditoría que no corre tiene que verse en /admin/auditoria (11 lotes
    se cayeron por saldo insuficiente de la API sin que nadie se enterara)."""
    print(f"[salud] {problema}: {detalle}", file=sys.stderr)
    if dry_run:
        return
    try:
        from app.audit_store import store_findings
        hora = datetime.now(timezone.utc).strftime("%Y-%m-%d %H")
        store_findings("portaviones_salud", [{
            "severity": "high", "category": "tecnico", "fix_type": "logic_review",
            "issue": f"Portaviones: {problema}", "evidence": detalle[:500],
            "suggested_fix": "Revisar saldo/credenciales de la API de Anthropic y cron.log",
            "dedup_key": f"{problema}|{hora}",
        }])
    except Exception as e:  # noqa: BLE001
        print(f"[salud] no se pudo registrar: {e}", file=sys.stderr)


def _write_report(findings: list[dict], summary: str, n_conv: int, ventana: str,
                  staged: list[dict]) -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M")
    report = LOG_DIR / f"audit_{stamp}.md"
    safe = [f for f in findings if f.get("fix_type") == "data_safe"]
    logic = [f for f in findings if f.get("fix_type") != "data_safe"]
    lines = [f"# Auditoría conversaciones — {stamp} UTC · ventana {ventana}",
             f"\nConversaciones: {n_conv} · Hallazgos: {len(findings)} "
             f"(seguros: {len(safe)}, revisión: {len(logic)})", f"\n**Resumen:** {summary}\n"]
    for titulo, grupo in (("Fixes seguros (estaged)", safe), ("Requieren revisión", logic)):
        lines.append(f"\n## {titulo} ({len(grupo)})")
        lines.extend(["_ninguno_"] if not grupo else [
            f"\n- **[{f.get('severity','?').upper()}·{f.get('confianza','?')}] {f.get('category','?')}**"
            f" ({f.get('phone','?')}) · fuente {f.get('evidence_source','?')}\n"
            f"  - Problema: {f.get('issue','')}\n  - Evidencia: _{f.get('evidence','')}_\n"
            f"  - Causa probable: {f.get('root_cause_hypothesis','')}\n"
            f"  - Fix: {f.get('suggested_fix','')}\n  - Dónde: `{f.get('target_hint') or '?'}`"
            for f in grupo])
    report.write_text("\n".join(lines), encoding="utf-8")
    staged_safe = [f for f in staged if f.get("fix_type") == "data_safe"]
    if staged_safe:
        with (LOG_DIR / "pending_safe_fixes.jsonl").open("a", encoding="utf-8") as fh:
            for f in staged_safe:
                fh.write(json.dumps({"run": stamp, **f}, ensure_ascii=False) + "\n")
    return report


def auditar(since_min: int, max_conv: int, model: str, effort: str, dry_run: bool) -> int:
    ventana = f"{since_min}min"
    convos = _fetch_conversations(since_min)
    if not convos:
        print(f"[audit] sin conversaciones con actividad en {ventana}")
        return 0
    items = list(convos.items())[:max_conv]
    if len(convos) > len(items):
        _registrar_salud("conversaciones sin auditar",
                         f"{len(convos)} en la ventana, auditadas {len(items)} (sube --max-conv)",
                         dry_run)

    desde_dt = datetime.now(timezone.utc) - timedelta(minutes=since_min)
    desde_ts = desde_dt.strftime("%Y-%m-%d %H:%M:%S")
    phones = [ph for ph, _ in items]
    previos, eventos = _contexto_y_eventos(phones, desde_ts)
    log_lineas = _leer_log(desde_dt - timedelta(minutes=10))
    log_ph = _log_por_phone(log_lineas, set(phones), solo_problemas=True)
    sistema = _errores_sistema(log_lineas)
    conocimiento = _conocimiento()
    system = f"{AUDITOR_INSTRUCCIONES}\n\n=== CONOCIMIENTO ===\n{conocimiento}"

    findings: list[dict] = []
    summaries: list[str] = []
    errores: list[str] = []
    for i in range(0, len(items), CHUNK_SIZE):
        chunk = items[i:i + CHUNK_SIZE]
        cuerpo = "\n\n".join(_render(ph, ms, previos.get(ph, []), eventos.get(ph, []),
                                     log_ph.get(ph, [])) for ph, ms in chunk)
        user = (f"Estado del sistema en la ventana (firmas de WARNING/ERROR del log):\n{sistema}\n\n"
                f"Conversaciones a auditar:\n\n{cuerpo}")
        try:
            result = _llm_json(system, user, AUDIT_SCHEMA, model=model, effort=effort)
        except Exception as e:  # noqa: BLE001 — un lote caído no bota la corrida
            errores.append(str(e)[:300])
            print(f"[audit] lote {i // CHUNK_SIZE + 1} falló: {e}", file=sys.stderr)
            continue
        lote = {ph for ph, _ in chunk}
        for f in result.get("findings") or []:
            if f.get("phone") in lote:        # anti-alucinación
                findings.append(f)
            else:
                print(f"[audit] descartado phone fuera del lote: {f.get('phone')!r}")
        if result.get("summary"):
            summaries.append(result["summary"])
    if errores:
        _registrar_salud("lotes de auditoría fallidos",
                         f"{len(errores)} lote(s): {errores[0]}", dry_run)
    summary = " · ".join(summaries)
    print(f"[audit] {len(items)} conversaciones · {len(findings)} hallazgos · "
          f"lotes fallidos: {len(errores)} · {summary}")
    for f in findings:
        print(f"  [{f.get('severity')}/{f.get('confianza')}] {f.get('fix_type')} "
              f"{f.get('category')} ({f.get('phone')}): {f.get('issue')}")
    if dry_run or not findings:
        if dry_run:
            print("[audit] dry-run: no se escribió nada")
        return 0
    nuevos = _filtrar_nuevos(findings)
    from app.audit_store import store_findings
    # La tabla no tiene columnas para fuente/confianza/causa: van en evidence.
    para_tabla = [{**f, "evidence": f"[{f.get('evidence_source')}·{f.get('confianza')}] "
                                    f"{f.get('evidence','')} | causa: {f.get('root_cause_hypothesis','')}"}
                  for f in findings]
    new = store_findings("conversacion", para_tabla)
    report = _write_report(findings, summary, len(items), ventana, staged=nuevos)
    print(f"[audit] reporte: {report} · {new} nuevos en tabla · {len(nuevos)} estageados")
    return 0


# ══════════════════════════════════════════════════════════════════════════════
#  Etapa 2: consolidar
# ══════════════════════════════════════════════════════════════════════════════

CONSOLIDADOR_INSTRUCCIONES = """\
Eres el triager senior del chatbot del Centro Médico Carampangue. Recibes
hallazgos CRUDOS de auditorías horarias, pre-agrupados (G1, G2…) con su
tendencia diaria, más los chequeos deterministas del sistema y los commits
desplegados en el período. Consolida en ≤15 problemas raíz distintos, ordenados
por impacto real en pacientes (frecuencia × severidad × si hace perder citas).

- Un problema raíz = una entrada, aunque junte muchos grupos. Lista sus ids en
  "grupos" (todos los que le pertenecen).
- No inventes problemas que no estén en los datos.
- Si un grupo dejó de aparecer después de un commit que lo ataca, pon ese
  commit en "posible_resuelto_por" (hash + fecha); si no, "".
- Marca "posible_falso_positivo" si calza con los diseños deliberados o
  falsos positivos del conocimiento.
- "palabras_clave_codigo": 3-6 identificadores o textos literales que un
  ingeniero buscaría con grep en el código para encontrar la causa (nombres de
  estados WAIT_*, funciones, frases exactas que el bot envía). Nada inventado.
- "target": SOLO archivos del mapa real del código, o "".
Español de Chile, sin voseo."""

_PROBLEMA_SCHEMA = _obj({
    "titulo": _STR,
    "prioridad": {"type": "integer", "enum": [1, 2, 3]},
    "n_hallazgos": {"type": "integer"},
    "grupos": {"type": "array", "items": _STR},
    "categorias": {"type": "array", "items": _STR},
    "fix_type": {"type": "string", "enum": ["data_safe", "logic_review"]},
    "descripcion": _STR,
    "fix_concreto": _STR,
    "target": _STR,
    "palabras_clave_codigo": {"type": "array", "items": _STR},
    "telefonos_ejemplo": {"type": "array", "items": _STR},
    "tendencia": {"type": "string", "enum": ["subiendo", "estable", "bajando", "desaparecio"]},
    "posible_resuelto_por": _STR,
    "posible_falso_positivo": {"type": "boolean"},
})
CONSOLIDAR_SCHEMA = _obj({"problemas": {"type": "array", "items": _PROBLEMA_SCHEMA},
                          "resumen": _STR})


def _fetch_findings(since_days: int, check: str = "conversacion") -> list[dict]:
    con = _conn()
    try:
        return [dict(r) for r in con.execute(
            "SELECT id, phone, severity, category, issue, evidence, fix_type, suggested_fix, "
            "target_hint, run_ts FROM audit_findings WHERE check_name = ? AND status = 'open' "
            "AND run_ts >= datetime('now', ?) ORDER BY run_ts", (check, f"-{since_days} days"))]
    finally:
        con.close()


def _agrupar(findings: list[dict]) -> dict[str, list[dict]]:
    grupos: dict[tuple, list[dict]] = defaultdict(list)
    for f in findings:
        norm = re.sub(r"[^a-záéíóúñü ]", "", (f.get("issue") or "").lower())
        grupos[(f.get("category"), f.get("fix_type"), " ".join(norm.split()[:8]))].append(f)
    ordenados = sorted(grupos.values(), key=len, reverse=True)
    return {f"G{i}": fs for i, fs in enumerate(ordenados, 1)}


def _compactar(grupos: dict[str, list[dict]]) -> str:
    bloques = []
    for gid, fs in grupos.items():
        tels = sorted({f.get("phone") or "?" for f in fs})
        dias = Counter((f.get("run_ts") or "")[:10] for f in fs)
        serie = " ".join(f"{d[5:]}:{n}" for d, n in sorted(dias.items()))
        ej = fs[0]
        b = [f"{gid} [{ej.get('category')}|{ej.get('fix_type')}] ×{len(fs)} · "
             f"{fs[0].get('run_ts','')[:16]} → {fs[-1].get('run_ts','')[:16]} · por día {serie}",
             f"  tels({len(tels)}): {', '.join(tels[:6])}",
             f"  issue: {ej.get('issue','')[:220]}"]
        if ej.get("evidence"):
            b.append(f"  evidencia: {ej['evidence'][:220]}")
        if ej.get("suggested_fix"):
            b.append(f"  fix sugerido: {ej['suggested_fix'][:160]}")
        if len(fs) > 1:
            b.append(f"  otro ej: {fs[len(fs) // 2].get('issue','')[:160]}")
        bloques.append("\n".join(b))
    return "\n\n".join(bloques)


def _commits(dias: int) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(ROOT), "log", f"--since={dias} days ago", "--date=short",
             "--pretty=%h %ad %s"], capture_output=True, text=True, timeout=20).stdout
        return out.strip() or "(sin commits en el período)"
    except Exception as e:  # noqa: BLE001
        return f"(git log no disponible: {e})"


def _deterministas(since_days: int) -> str:
    con = _conn()
    try:
        rows = con.execute(
            "SELECT check_name, severity, issue, COUNT(*) n, MAX(run_ts) ult FROM audit_findings "
            "WHERE check_name != 'conversacion' AND status = 'open' AND run_ts >= datetime('now', ?) "
            "GROUP BY check_name, issue ORDER BY n DESC LIMIT 40", (f"-{since_days} days",)).fetchall()
    finally:
        con.close()
    return "\n".join(f"- [{r['check_name']}|{r['severity']}] ×{r['n']} (última {r['ult'][:16]}) "
                     f"{(r['issue'] or '')[:200]}" for r in rows) or "(sin hallazgos deterministas)"


def consolidar(since_days: int, model: str, effort: str) -> dict:
    findings = _fetch_findings(since_days)
    if not findings:
        print(f"[consolidar] sin hallazgos abiertos en {since_days} días")
        return {}
    grupos = _agrupar(findings)
    compacto = _compactar(grupos)
    if len(compacto) > 300_000:
        print(f"[consolidar] ADVERTENCIA: {len(grupos)} grupos, se envían los más grandes")
        compacto = compacto[:300_000]
    user = (f"Período: últimos {since_days} días · {len(findings)} hallazgos · {len(grupos)} grupos.\n\n"
            f"== Commits desplegados (desde {since_days + 7} días) ==\n{_commits(since_days + 7)}\n\n"
            f"== Chequeos deterministas abiertos ==\n{_deterministas(since_days)}\n\n"
            f"== Grupos de hallazgos ==\n{compacto}")
    system = f"{CONSOLIDADOR_INSTRUCCIONES}\n\n=== CONOCIMIENTO ===\n{_conocimiento()}"
    result = _llm_json(system, user, CONSOLIDAR_SCHEMA, model=model, effort=effort, max_tokens=32000)
    # Mapa problema → ids de hallazgos, para poder cerrarlos tras verificar.
    for p in result.get("problemas", []):
        p["_finding_ids"] = sorted({f["id"] for g in p.get("grupos", []) for f in grupos.get(g, [])})
    result["_meta"] = {"since_days": since_days, "n_findings": len(findings),
                       "n_grupos": len(grupos), "ts": datetime.now(timezone.utc).isoformat()}
    _escribir_consolidado(result, since_days, verificado=False)
    return result


# ══════════════════════════════════════════════════════════════════════════════
#  Etapa 3: verificar
# ══════════════════════════════════════════════════════════════════════════════

VERIFICADOR_INSTRUCCIONES = """\
Eres un ingeniero senior que verifica UN problema reportado por el auditor del
chatbot del Centro Médico Carampangue, antes de que alguien toque código. El
auditor es un LLM: a veces inventa, confunde diseños deliberados con bugs o
reporta cosas ya arregladas. Tu trabajo es decidir con la evidencia adjunta
(conversaciones completas, eventos internos, log del bot, fragmentos del código
REAL y commits recientes):

- REAL: la evidencia muestra el defecto y el código actual lo sigue permitiendo.
- YA_RESUELTO: hubo un commit que lo ataca y los casos son anteriores a él, o
  el código actual ya no puede producirlo.
- DISENO: es comportamiento deliberado según el conocimiento.
- FALSO_POSITIVO: el material no muestra el defecto (el auditor malinterpretó).
- INCIERTO: falta evidencia para decidir; di exactamente qué falta.

confianza "alta" solo si la evidencia es directa. En "ubicacion" pon
archivo:función (o línea) del código adjunto; nunca inventes nombres. El
fix_propuesto debe ser el mínimo que mata la causa, y test_sugerido un caso
que falle hoy y pase con el fix. Español de Chile, sin voseo."""

VERIFICAR_SCHEMA = _obj({
    "veredicto": {"type": "string",
                  "enum": ["REAL", "YA_RESUELTO", "DISENO", "FALSO_POSITIVO", "INCIERTO"]},
    "confianza": {"type": "string", "enum": ["alta", "media", "baja"]},
    "evidencia": _STR,
    "causa_raiz": _STR,
    "ubicacion": _STR,
    "fix_propuesto": _STR,
    "test_sugerido": _STR,
    "que_falta": _STR,
    "impacto_pacientes": _STR,
})


def _grep_codigo(palabras: list[str], max_por_palabra: int = 6) -> str:
    """Fragmentos del código real donde aparece cada palabra clave (±3 líneas)."""
    archivos = sorted((ROOT / "app").glob("*.py"))
    textos = {}
    for f in archivos:
        try:
            textos[f] = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            continue
    bloques = []
    for p in [w for w in palabras if w and len(w) >= 4][:6]:
        pl = p.lower()
        hits = 0
        for f, lines in textos.items():
            for i, ln in enumerate(lines):
                if pl in ln.lower():
                    a, b = max(0, i - 3), min(len(lines), i + 4)
                    frag = "\n".join(f"{j + 1}: {lines[j]}" for j in range(a, b))
                    bloques.append(f"--- {f.relative_to(ROOT)} (busca: {p!r})\n{frag}")
                    hits += 1
                    if hits >= max_por_palabra:
                        break
            if hits >= max_por_palabra:
                break
        if not hits:
            bloques.append(f"--- (sin coincidencias en app/ para {p!r})")
    return "\n".join(bloques)[:40_000]


def _historial_phone(phone: str, dias: int) -> str:
    con = _conn()
    try:
        msgs = [dict(r) for r in con.execute(
            "SELECT direction, text, state, ts FROM messages WHERE phone=? AND ts >= datetime('now', ?) "
            "ORDER BY id DESC LIMIT 60", (phone, f"-{dias} days"))][::-1]
        evs = [dict(r) for r in con.execute(
            "SELECT event, meta, ts FROM conversation_events WHERE phone=? AND ts >= datetime('now', ?) "
            "ORDER BY id DESC LIMIT 80", (phone, f"-{dias} days"))][::-1]
    finally:
        con.close()
    evs = [e for e in evs if e["event"] not in _EVENTOS_RUIDO]
    return _render(phone, msgs, [], evs, [])


def verificar(consolidado: dict, since_days: int, model: str, effort: str,
              cerrar: bool, dry_run: bool) -> dict:
    problemas = consolidado.get("problemas", [])
    if not problemas:
        return consolidado
    phones = {ph for p in problemas for ph in p.get("telefonos_ejemplo", [])[:3]}
    desde = datetime.now(timezone.utc) - timedelta(days=since_days)
    log_ph = _log_por_phone(_leer_log(desde, incluir_rotados=True), phones, solo_problemas=False)
    commits = _commits(since_days + 14)
    system = f"{VERIFICADOR_INSTRUCCIONES}\n\n=== CONOCIMIENTO ===\n{_conocimiento()}"

    def _uno(p: dict) -> dict:
        casos = []
        for ph in p.get("telefonos_ejemplo", [])[:3]:
            casos.append(_historial_phone(ph, since_days))
            lg = [ln for ln in log_ph.get(ph, []) if "BOT to=" not in ln][-40:]
            if lg:
                casos.append("Log del bot para este teléfono:\n" + "\n".join(f"  {x}" for x in lg))
        user = (f"PROBLEMA REPORTADO:\n{json.dumps({k: v for k, v in p.items() if not k.startswith('_')}, ensure_ascii=False, indent=1)}\n\n"
                f"== Commits recientes ==\n{commits}\n\n"
                f"== Casos (conversación + eventos + log) ==\n" + "\n\n".join(casos)[:60_000] +
                f"\n\n== Código real (grep de palabras clave) ==\n{_grep_codigo(p.get('palabras_clave_codigo', []))}")
        try:
            return _llm_json(system, user, VERIFICAR_SCHEMA, model=model, effort=effort)
        except Exception as e:  # noqa: BLE001
            return {"veredicto": "INCIERTO", "confianza": "baja", "evidencia": "",
                    "causa_raiz": "", "ubicacion": "", "fix_propuesto": "", "test_sugerido": "",
                    "que_falta": f"verificación falló: {e}", "impacto_pacientes": ""}

    with ThreadPoolExecutor(max_workers=4) as ex:
        for p, v in zip(problemas, ex.map(_uno, problemas)):
            p["verificacion"] = v
            print(f"[verificar] {v['veredicto']}/{v['confianza']} — {p.get('titulo','')[:90]}")

    if cerrar and not dry_run:
        _cerrar_y_aprender(problemas)
    _escribir_consolidado(consolidado, since_days, verificado=True)
    return consolidado


_ESTADO_POR_VEREDICTO = {"YA_RESUELTO": "resolved", "DISENO": "descartado",
                         "FALSO_POSITIVO": "descartado"}


def _cerrar_y_aprender(problemas: list[dict]) -> None:
    """Descartes con confianza ALTA: cierran sus hallazgos (dejan de volver en
    cada consolidado) y, si eran falso positivo/diseño, se anotan en
    aprendido.md para que el auditor horario no los vuelva a reportar."""
    con = _conn()
    aprendido = []
    try:
        for p in problemas:
            v = p.get("verificacion") or {}
            estado = _ESTADO_POR_VEREDICTO.get(v.get("veredicto"))
            ids = p.get("_finding_ids") or []
            if not estado or v.get("confianza") != "alta" or not ids:
                continue
            con.executemany("UPDATE audit_findings SET status=? WHERE id=? AND status='open'",
                            [(estado, i) for i in ids])
            print(f"[cerrar] {len(ids)} hallazgos → {estado}: {p.get('titulo','')[:80]}")
            if estado == "descartado":
                aprendido.append(f"- {datetime.now(timezone.utc):%Y-%m-%d} · {v['veredicto']} · "
                                 f"{p.get('titulo','')}: {v.get('evidencia','')[:300]}")
        con.commit()
    finally:
        con.close()
    if aprendido:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        previo = APRENDIDO_MD.read_text(encoding="utf-8") if APRENDIDO_MD.exists() else ""
        nuevos = [a for a in aprendido if a.split(" · ", 2)[-1][:60] not in previo]
        if nuevos:
            with APRENDIDO_MD.open("a", encoding="utf-8") as fh:
                fh.write("\n".join(nuevos) + "\n")


def _escribir_consolidado(result: dict, since_days: int, verificado: bool) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    base = LOG_DIR / f"consolidado_{stamp}_{since_days}d"
    problemas = result.get("problemas", [])
    meta = result.get("_meta", {})
    lines = [f"# Portaviones — consolidado {stamp} · últimos {since_days} días"
             + (" · VERIFICADO" if verificado else ""),
             f"\nHallazgos crudos: {meta.get('n_findings','?')} → grupos {meta.get('n_grupos','?')}"
             f" → problemas raíz: {len(problemas)}"]
    if verificado:
        c = Counter((p.get("verificacion") or {}).get("veredicto") for p in problemas)
        lines.append("\n**Veredictos:** " + " · ".join(f"{k} {n}" for k, n in c.most_common()))
    lines.append(f"\n**Resumen:** {result.get('resumen','')}\n")
    orden = {"REAL": 0, "INCIERTO": 1, "YA_RESUELTO": 2, "DISENO": 3, "FALSO_POSITIVO": 4}
    lista = sorted(enumerate(problemas, 1), key=lambda t: (
        orden.get((t[1].get("verificacion") or {}).get("veredicto"), 0), t[1].get("prioridad", 3)))
    for i, p in lista:
        v = p.get("verificacion") or {}
        cab = f"\n## {i}. [P{p.get('prioridad','?')}] {p.get('titulo','')} — ×{p.get('n_hallazgos','?')}"
        if v:
            cab += f" · **{v.get('veredicto')}** ({v.get('confianza')})"
        lines.append(cab)
        lines.append(f"- **Qué pasa:** {p.get('descripcion','')}")
        lines.append(f"- **Tendencia:** {p.get('tendencia','?')}"
                     + (f" · posible resuelto por `{p['posible_resuelto_por']}`" if p.get("posible_resuelto_por") else ""))
        if v:
            lines.append(f"- **Evidencia:** {v.get('evidencia','')}")
            if v.get("impacto_pacientes"):
                lines.append(f"- **Impacto:** {v['impacto_pacientes']}")
            if v.get("veredicto") in ("REAL", "INCIERTO"):
                lines.append(f"- **Causa raíz:** {v.get('causa_raiz','')} · `{v.get('ubicacion','')}`")
                lines.append(f"- **Fix:** {v.get('fix_propuesto','')}")
                lines.append(f"- **Test:** {v.get('test_sugerido','')}")
            if v.get("que_falta"):
                lines.append(f"- **Falta para confirmar:** {v['que_falta']}")
        else:
            lines.append(f"- **Fix sugerido:** {p.get('fix_concreto','')} · `{p.get('target') or '?'}`")
        lines.append(f"- **Ejemplos:** {', '.join(p.get('telefonos_ejemplo', [])[:5])}")
    base.with_suffix(".md").write_text("\n".join(lines), encoding="utf-8")
    base.with_suffix(".json").write_text(json.dumps(result, ensure_ascii=False, indent=1),
                                         encoding="utf-8")
    print(f"[consolidar] {len(problemas)} problemas · {base.with_suffix('.md')}")


# ══════════════════════════════════════════════════════════════════════════════

def main() -> int:
    ap = argparse.ArgumentParser(description="Portaviones v2 — auditoría de conversaciones CMC")
    ap.add_argument("--since-min", type=int, default=65)
    ap.add_argument("--since-days", type=int, default=None)
    ap.add_argument("--max-conv", type=int, default=MAX_CONVERSATIONS)
    ap.add_argument("--consolidar", action="store_true",
                    help="consolida los hallazgos crudos en problemas raíz (default 7 días)")
    ap.add_argument("--verificar", action="store_true",
                    help="tras --consolidar, verifica cada problema con evidencia y código")
    ap.add_argument("--verificar-json", default=None,
                    help="verifica un consolidado .json ya existente")
    ap.add_argument("--no-cerrar", action="store_true",
                    help="no cierra hallazgos ni escribe aprendido.md tras verificar")
    ap.add_argument("--model", default=None)
    ap.add_argument("--effort", default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not os.getenv("ANTHROPIC_API_KEY"):
        print("ERROR: falta ANTHROPIC_API_KEY (¿cargaste el .env?)", file=sys.stderr)
        return 2

    if args.consolidar or args.verificar_json:
        dias = args.since_days or 7
        model, effort = args.model or DEEP_MODEL, args.effort or DEEP_EFFORT
        try:
            if args.verificar_json:
                cons = json.loads(Path(args.verificar_json).read_text(encoding="utf-8"))
                dias = (cons.get("_meta") or {}).get("since_days", dias)
            else:
                cons = consolidar(dias, model, effort)
            if cons and (args.verificar or args.verificar_json):
                verificar(cons, dias, model, effort, cerrar=not args.no_cerrar, dry_run=args.dry_run)
        except Exception as e:  # noqa: BLE001
            _registrar_salud("consolidación/verificación fallida", str(e)[:400], args.dry_run)
            return 1
        return 0

    since_min = (args.since_days * 24 * 60) if args.since_days else args.since_min
    return auditar(since_min, args.max_conv, args.model or DEFAULT_MODEL,
                   args.effort or DEFAULT_EFFORT, args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
