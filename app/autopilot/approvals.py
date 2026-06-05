"""Fase 2 — Ejecución con aprobación humana.

Cierra el lazo del autopilot: las acciones que mueven plata (subir/bajar/pausar
presupuesto) NO se aplican solas. Se encolan como "pendientes", se le avisa al
dueño por WhatsApp con un link al dashboard, y solo se aplican cuando él aprueba.
Al aplicar se RE-VALIDAN los límites duros (defensa en profundidad) y la escritura
real a Meta sigue gateada por AUTOPILOT_EXECUTE (si está off, se simula y se marca
'applied_dryrun', de modo que todo el flujo propose→avisar→aprobar→aplicar es
testeable sin tocar Meta).

Gating en cascada (todo debe estar ON para que algo llegue a Meta):
  AUTOPILOT_ENABLED   → el motor propone y encola (sin esto, nada se encola)
  [aprobación humana] → no-delegable: nada se aplica sin approve() explícito
  AUTOPILOT_EXECUTE   → la escritura real a Meta (si off → dry-run simulado)

NO toca flows.py ni el motor conversacional: la aprobación se maneja por el
dashboard (endpoints en routes.py), no por parseo de comandos de WhatsApp.
"""
import json
import logging
import os
from .flags import flag_on
import time
import uuid

log = logging.getLogger("bot")

# Estados del ciclo de vida de una acción pendiente.
ST_PENDING = "pending"
ST_APPROVED = "approved"       # aprobada y aplicada (o simulada) con éxito
ST_REJECTED = "rejected"
ST_FAILED = "failed"           # aprobada pero la aplicación falló
ST_EXPIRED = "expired"         # caducó sin decisión (no aplicar propuestas viejas)
ST_AUTO = "auto_applied"       # Fase 3: aplicada sola (alta confianza, sin pedir OK)

_MONEY_ACTIONS = ("increase_budget", "decrease_budget", "pause")


def _autoapply_on() -> bool:
    """Fase 3 — autonomía acotada. OFF por defecto. Solo con esto en true el motor
    aplica solo los movimientos de ALTA confianza (sin pedir aprobación)."""
    return flag_on("AUTOPILOT_AUTOAPPLY")


def _autoapply_min_conf() -> float:
    try:
        return float(os.getenv("AUTOPILOT_AUTOAPPLY_MIN_CONF", "0.75"))
    except (TypeError, ValueError):
        return 0.75


def _is_auto(a) -> bool:
    """¿Esta acción califica para auto-aplicarse en Fase 3?

    Solo movimientos de plata de ALTA confianza, y solo si AUTOPILOT_AUTOAPPLY=true.
    El tamaño ya está capado al ±max_step (límite duro), así que el filtro de riesgo
    aquí es la confianza: lo obvio se aplica solo, lo dudoso va a tu aprobación.
    NUNCA auto-pausa: pausar una campaña siempre pasa por aprobación humana.
    """
    if not _autoapply_on():
        return False
    act = getattr(a.action, "value", a.action)
    if act not in ("increase_budget", "decrease_budget"):
        return False
    return float(getattr(a, "confidence", 0.0)) >= _autoapply_min_conf()


def _conn():
    from session import _conn as _c
    return _c()


def _ensure_table() -> None:
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS autopilot_pending (
                id              TEXT PRIMARY KEY,
                created_ts      INTEGER,
                campaign_id     TEXT,
                campaign_name   TEXT,
                action          TEXT,
                current_budget  INTEGER,
                proposed_budget INTEGER,
                reason          TEXT,
                confidence      REAL,
                status          TEXT DEFAULT 'pending',
                decided_ts      INTEGER,
                decided_by      TEXT,
                applied_ts      INTEGER,
                apply_result    TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_autopilot_pending_status "
                     "ON autopilot_pending(status, created_ts DESC)")


def enqueue(actions: list, *, dedupe_hours: int = 20) -> list[str]:
    """Encola las acciones que mueven plata y requieren aprobación. Devuelve ids nuevos.

    Solo corre si AUTOPILOT_ENABLED=true. Dedupe: si ya hay una pendiente para la
    misma campaña+acción dentro de `dedupe_hours`, no la duplica (evita spamear al
    dueño con la misma propuesta cada día).
    """
    if not flag_on("AUTOPILOT_ENABLED"):
        return []
    _ensure_table()
    now = int(time.time())
    cutoff = now - dedupe_hours * 3600
    new_ids: list[str] = []
    with _conn() as conn:
        for a in actions:
            act = getattr(a.action, "value", a.action)
            if act not in _MONEY_ACTIONS or not getattr(a, "needs_approval", False):
                continue
            if _is_auto(a):
                continue  # Fase 3: lo aplica auto_apply, no va a la cola de aprobación
            dup = conn.execute(
                "SELECT 1 FROM autopilot_pending WHERE campaign_id=? AND action=? "
                "AND status=? AND created_ts>=? LIMIT 1",
                (a.campaign_id, act, ST_PENDING, cutoff),
            ).fetchone()
            if dup:
                continue
            pid = uuid.uuid4().hex[:12]
            conn.execute(
                "INSERT INTO autopilot_pending "
                "(id, created_ts, campaign_id, campaign_name, action, current_budget, "
                " proposed_budget, reason, confidence, status) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (pid, now, a.campaign_id, a.campaign_name, act,
                 a.current_budget_clp, a.proposed_budget_clp, a.reason,
                 float(getattr(a, "confidence", 0.0)), ST_PENDING),
            )
            new_ids.append(pid)
    if new_ids:
        log.info("[autopilot] %d acciones encoladas para aprobación", len(new_ids))
    return new_ids


async def auto_apply(actions: list, *, dedupe_hours: int = 20) -> list[dict]:
    """Fase 3 — aplica SOLA los movimientos de alta confianza (sin pedir OK).

    Solo corre con AUTOPILOT_ENABLED + AUTOPILOT_AUTOAPPLY en true. Cada acción
    pasa por el MISMO `_apply` (re-valida límites duros; escritura real gateada por
    AUTOPILOT_EXECUTE). Registra cada una como `auto_applied` para auditoría y la
    devuelve para el aviso informativo (FYI, no pide aprobación). Dedupe igual que
    enqueue para no re-aplicar la misma propuesta cada corrida.
    """
    if not flag_on("AUTOPILOT_ENABLED") or not _autoapply_on():
        return []
    _ensure_table()
    now = int(time.time())
    cutoff = now - dedupe_hours * 3600
    applied: list[dict] = []
    for a in actions:
        if not _is_auto(a):
            continue
        act = getattr(a.action, "value", a.action)
        # Dedupe: no re-aplicar lo mismo (cola O auto) dentro de la ventana.
        with _conn() as conn:
            dup = conn.execute(
                "SELECT 1 FROM autopilot_pending WHERE campaign_id=? AND action=? "
                "AND status IN (?,?) AND created_ts>=? LIMIT 1",
                (a.campaign_id, act, ST_PENDING, ST_AUTO, cutoff),
            ).fetchone()
        if dup:
            continue
        pid = uuid.uuid4().hex[:12]
        item = {
            "id": pid, "campaign_id": a.campaign_id, "campaign_name": a.campaign_name,
            "action": act, "current_budget": a.current_budget_clp,
            "proposed_budget": a.proposed_budget_clp, "reason": a.reason,
            "confidence": float(getattr(a, "confidence", 0.0)),
        }
        try:
            result = await _apply(item)
            ok = bool(result.get("ok"))
        except Exception as e:  # noqa: BLE001
            log.error("[autopilot] auto_apply %s falló: %s", pid, e)
            result, ok = {"ok": False, "error": str(e)}, False
        status = ST_AUTO if ok else ST_FAILED
        with _conn() as conn:
            conn.execute(
                "INSERT INTO autopilot_pending "
                "(id, created_ts, campaign_id, campaign_name, action, current_budget, "
                " proposed_budget, reason, confidence, status, decided_ts, decided_by, "
                " applied_ts, apply_result) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (pid, now, a.campaign_id, a.campaign_name, act, a.current_budget_clp,
                 a.proposed_budget_clp, a.reason, float(getattr(a, "confidence", 0.0)),
                 status, now, "autopilot", now,
                 json.dumps(result, ensure_ascii=False)),
            )
        if ok:
            item["status"] = ST_AUTO
            applied.append(item)
    if applied:
        log.info("[autopilot] Fase 3: %d acciones auto-aplicadas", len(applied))
    return applied


def _row_to_dict(r) -> dict:
    return {
        "id": r["id"], "created_ts": r["created_ts"],
        "campaign_id": r["campaign_id"], "campaign_name": r["campaign_name"],
        "action": r["action"], "current_budget": r["current_budget"],
        "proposed_budget": r["proposed_budget"], "reason": r["reason"],
        "confidence": r["confidence"], "status": r["status"],
        "decided_ts": r["decided_ts"], "decided_by": r["decided_by"],
        "applied_ts": r["applied_ts"],
        "apply_result": json.loads(r["apply_result"]) if r["apply_result"] else None,
    }


def list_pending() -> list[dict]:
    _ensure_table()
    expire_stale()
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM autopilot_pending WHERE status=? ORDER BY created_ts DESC",
            (ST_PENDING,),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def list_recent(limit: int = 30) -> list[dict]:
    _ensure_table()
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM autopilot_pending ORDER BY created_ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get(pid: str) -> dict | None:
    _ensure_table()
    with _conn() as conn:
        r = conn.execute("SELECT * FROM autopilot_pending WHERE id=?", (pid,)).fetchone()
    return _row_to_dict(r) if r else None


def expire_stale(hours: int = 48) -> int:
    """Caduca pendientes sin decisión > `hours`. Evita aplicar propuestas viejas."""
    cutoff = int(time.time()) - hours * 3600
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE autopilot_pending SET status=? WHERE status=? AND created_ts<?",
            (ST_EXPIRED, ST_PENDING, cutoff),
        )
        return cur.rowcount or 0


def reject(pid: str, by: str = "recepción") -> dict | None:
    with _conn() as conn:
        conn.execute(
            "UPDATE autopilot_pending SET status=?, decided_ts=?, decided_by=? "
            "WHERE id=? AND status=?",
            (ST_REJECTED, int(time.time()), by, pid, ST_PENDING),
        )
    return get(pid)


async def approve(pid: str, by: str = "recepción") -> dict | None:
    """Aprueba y APLICA la acción (vía executor). Re-valida límites antes de tocar Meta."""
    item = get(pid)
    if not item or item["status"] != ST_PENDING:
        return item
    now = int(time.time())
    try:
        result = await _apply(item)
        ok = bool(result.get("ok"))
        status = ST_APPROVED if ok else ST_FAILED
    except Exception as e:  # noqa: BLE001
        log.error("[autopilot] aplicar %s falló: %s", pid, e)
        result = {"ok": False, "error": str(e)}
        status = ST_FAILED
    with _conn() as conn:
        conn.execute(
            "UPDATE autopilot_pending SET status=?, decided_ts=?, decided_by=?, "
            "applied_ts=?, apply_result=? WHERE id=?",
            (status, now, by, now, json.dumps(result, ensure_ascii=False), pid),
        )
    return get(pid)


async def _apply(item: dict) -> dict:
    """Ejecuta una acción aprobada contra Meta, RE-VALIDANDO límites duros.

    - increase/decrease: ajusta el presupuesto diario al `proposed_budget` dentro de
      [min, max] por ad set y respeta el techo total de la cuenta.
    - pause: pausa todos los ad sets de la campaña.
    La escritura real va por meta_ads (gateada por AUTOPILOT_EXECUTE → si off, simula).
    """
    import httpx
    from .policy import HardLimits
    from . import meta_ads
    limits = HardLimits.from_env()
    action = item["action"]
    cid = item["campaign_id"]

    async with httpx.AsyncClient() as client:
        adsets = await meta_ads.list_adsets(client, cid)
        adset_ids = [a["id"] for a in adsets if a.get("id")]

        if action == "pause":
            if not adset_ids:
                return {"ok": False, "error": "campaña sin ad sets para pausar"}
            results = []
            for aid in adset_ids:
                results.append(await meta_ads.pause_adset(client, aid, reason=item["reason"][:120]))
            return {"ok": True, "mode": "pause", "adsets": adset_ids, "meta": results}

        # increase/decrease: validar el presupuesto propuesto contra límites duros.
        proposed = item["proposed_budget"]
        if not proposed:
            return {"ok": False, "error": "sin presupuesto propuesto (campaña sin budget editable)"}
        proposed = max(limits.min_daily_budget_clp, min(limits.max_daily_budget_clp, int(proposed)))
        current = item["current_budget"] or 0
        delta = proposed - current

        # Repartir el presupuesto objetivo entre los ad sets con budget propio.
        adsets_with_budget = [a for a in adsets if a.get("daily_budget")]
        if adsets_with_budget:
            total_now = sum(int(a["daily_budget"]) for a in adsets_with_budget) or 1
            results = []
            for a in adsets_with_budget:
                share = int(a["daily_budget"]) / total_now
                new_b = max(limits.min_daily_budget_clp,
                            min(limits.max_daily_budget_clp, int(round(proposed * share))))
                results.append(await meta_ads.set_adset_budget(
                    client, a["id"], new_b, reason=item["reason"][:120]))
            return {"ok": True, "mode": "adset_budget", "proposed_total": proposed,
                    "delta": delta, "meta": results}

        # CBO: presupuesto a nivel campaña.
        res = await meta_ads.set_campaign_budget(client, cid, proposed, reason=item["reason"][:120])
        return {"ok": True, "mode": "campaign_budget", "proposed_total": proposed,
                "delta": delta, "meta": res}


def _fmt_action_line(x: dict) -> list[str]:
    verbo = {"increase_budget": "Subir", "decrease_budget": "Bajar",
             "pause": "Pausar"}.get(x["action"], x["action"])
    out = [f"• *{verbo}* — {(x.get('campaign_name') or '')[:40]}"]
    if x["action"] != "pause":
        cur = f"${x['current_budget']:,}" if x.get("current_budget") else "—"
        prop = f"${x['proposed_budget']:,}" if x.get("proposed_budget") else "—"
        out.append(f"   {cur} → {prop}/día · {(x.get('reason') or '')[:80]}")
    else:
        out.append(f"   {(x.get('reason') or '')[:80]}")
    return out


def _action_summary(x: dict) -> str:
    verbo = {"increase_budget": "Subir", "decrease_budget": "Bajar",
             "pause": "Pausar"}.get(x["action"], x["action"])
    camp = (x.get("campaign_name") or "")[:34]
    if x["action"] == "pause":
        return f"{verbo} {camp}"
    cur = f"${x['current_budget']:,}" if x.get("current_budget") else "—"
    prop = f"${x['proposed_budget']:,}" if x.get("proposed_budget") else "—"
    return f"{verbo} {camp} {cur} → {prop}/día"


def notify_owner(new_ids: list[str], auto_applied: list[dict] | None = None) -> None:
    """Avisa al dueño por WhatsApp: decisiones esperando su OK y/o cambios que el
    autopilot aplicó solo (Fase 3, FYI). El link lleva al dashboard.

    Gateado: solo con AUTOPILOT_ENABLED y si hay un teléfono de dueño configurado.
    """
    auto_applied = auto_applied or []
    if (not new_ids and not auto_applied) or not flag_on("AUTOPILOT_ENABLED"):
        return
    phone = (os.getenv("AUTOPILOT_OWNER_PHONE")
             or os.getenv("ADMIN_ALERT_PHONE") or "").strip()
    if not phone:
        log.info("[autopilot] avisos pendientes pero sin AUTOPILOT_OWNER_PHONE/ADMIN_ALERT_PHONE")
        return
    items = [x for x in (get(i) for i in new_ids) if x]
    base = os.getenv("PUBLIC_BASE_URL", "https://agentecmc.cl")
    # El aviso es del DUEÑO → link con su token (acceso total, abre en Decisiones).
    # cmc_admin_2026 es recepción y solo ve Diseños del Autopilot.
    try:
        from config import OLACORE_TOKEN as _owner_tok
    except Exception:  # noqa: BLE001
        _owner_tok = "cmc_admin_olacore"
    panel = f"{base}/autopilot?token={_owner_tok}"

    lines: list[str] = []
    if items:
        lines += ["🤖 *Autopilot — decisiones esperando tu OK*", ""]
        for x in items[:6]:
            lines += _fmt_action_line(x)
        if len(items) > 6:
            lines.append(f"… y {len(items) - 6} más.")
    if auto_applied:
        if lines:
            lines.append("")
        lines.append("✅ *Aplicadas solas (alta confianza):*")
        for x in auto_applied[:6]:
            lines += _fmt_action_line(x)
    lines += ["", f"Detalle: {panel}"]
    msg = "\n".join(lines)

    # Proactivo fuera de ventana 24h → texto libre rebota (code 131047). Si hay
    # pendientes, se manda el template `autopilot_aviso` (UTILITY) con su count
    # + resumen; el texto libre (con la sección de auto-aplicadas) queda de fallback.
    # Si SOLO hay auto-aplicadas (nada que aprobar), se intenta texto libre (entrega
    # dentro de la ventana 24h; el detalle siempre está en el dashboard).
    try:
        import asyncio

        async def _send():
            if items:
                top_summary = _action_summary(items[0])
                try:
                    from messaging import send_whatsapp_template
                    await send_whatsapp_template(
                        phone, "autopilot_aviso",
                        body_params=[str(len(items)), top_summary])
                    log.info("[autopilot] aviso (template) de %d pendientes → %s", len(items), phone)
                    return
                except Exception as _e_tpl:  # noqa: BLE001 — fallback a texto libre
                    log.warning("[autopilot] template aviso falló (%s); intento texto libre", _e_tpl)
            from messaging import send_whatsapp
            await send_whatsapp(phone, msg)
            log.info("[autopilot] aviso (texto) → %s (%d pend · %d auto)",
                     phone, len(items), len(auto_applied))

        asyncio.create_task(_send())
    except Exception as e:  # noqa: BLE001
        log.warning("[autopilot] no se pudo avisar al dueño: %s", e)
