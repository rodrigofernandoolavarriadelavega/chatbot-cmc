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
import time
import uuid

log = logging.getLogger("bot")

# Estados del ciclo de vida de una acción pendiente.
ST_PENDING = "pending"
ST_APPROVED = "approved"       # aprobada y aplicada (o simulada) con éxito
ST_REJECTED = "rejected"
ST_FAILED = "failed"           # aprobada pero la aplicación falló
ST_EXPIRED = "expired"         # caducó sin decisión (no aplicar propuestas viejas)

_MONEY_ACTIONS = ("increase_budget", "decrease_budget", "pause")


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
    if os.getenv("AUTOPILOT_ENABLED", "false").lower() != "true":
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


def notify_owner(new_ids: list[str]) -> None:
    """Avisa al dueño por WhatsApp que hay decisiones de presupuesto esperando su OK.

    Una sola vía (notificación, no comando): el link lleva al dashboard donde aprueba.
    Gateado: solo con AUTOPILOT_ENABLED y si hay un teléfono de dueño configurado.
    """
    if not new_ids or os.getenv("AUTOPILOT_ENABLED", "false").lower() != "true":
        return
    phone = (os.getenv("AUTOPILOT_OWNER_PHONE")
             or os.getenv("ADMIN_ALERT_PHONE") or "").strip()
    if not phone:
        log.info("[autopilot] %d pendientes pero sin AUTOPILOT_OWNER_PHONE/ADMIN_ALERT_PHONE",
                 len(new_ids))
        return
    items = [get(i) for i in new_ids]
    items = [x for x in items if x]
    lines = ["🤖 *Autopilot — decisiones esperando tu OK*", ""]
    for x in items[:6]:
        verbo = {"increase_budget": "Subir", "decrease_budget": "Bajar",
                 "pause": "Pausar"}.get(x["action"], x["action"])
        cur = f"${x['current_budget']:,}" if x["current_budget"] else "—"
        prop = f"${x['proposed_budget']:,}" if x["proposed_budget"] else "—"
        lines.append(f"• *{verbo}* — {x['campaign_name'][:40]}")
        if x["action"] != "pause":
            lines.append(f"   {cur} → {prop}/día · {x['reason'][:80]}")
        else:
            lines.append(f"   {x['reason'][:80]}")
    if len(items) > 6:
        lines.append(f"… y {len(items) - 6} más.")
    base = os.getenv("PUBLIC_BASE_URL", "https://agentecmc.cl")
    lines += ["", f"Revisar y aprobar: {base}/autopilot"]
    msg = "\n".join(lines)

    # Resumen de la acción principal para el template (1 línea).
    top = items[0]
    _verbo = {"increase_budget": "Subir", "decrease_budget": "Bajar",
              "pause": "Pausar"}.get(top["action"], top["action"])
    _camp = (top.get("campaign_name") or "")[:34]
    if top["action"] == "pause":
        top_summary = f"{_verbo} {_camp}"
    else:
        _cur = f"${top['current_budget']:,}" if top.get("current_budget") else "—"
        _prop = f"${top['proposed_budget']:,}" if top.get("proposed_budget") else "—"
        top_summary = f"{_verbo} {_camp} {_cur} → {_prop}/día"

    # Proactivo fuera de ventana 24h → texto libre rebota con code 131047
    # ("Re-engagement message"). Se manda como template `autopilot_pendientes`
    # (UTILITY), con fallback a texto libre por si el template aún no está aprobado
    # (dentro de la ventana de 24h el texto sí entrega).
    try:
        import asyncio

        async def _send():
            try:
                from messaging import send_whatsapp_template
                await send_whatsapp_template(
                    phone, "autopilot_pendientes",
                    body_params=[str(len(items)), top_summary])
                log.info("[autopilot] aviso (template) de %d pendientes → %s", len(items), phone)
            except Exception as _e_tpl:  # noqa: BLE001 — fallback a texto libre
                log.warning("[autopilot] template aviso falló (%s); intento texto libre", _e_tpl)
                from messaging import send_whatsapp
                await send_whatsapp(phone, msg)
                log.info("[autopilot] aviso (texto) de %d pendientes → %s", len(items), phone)

        asyncio.create_task(_send())
    except Exception as e:  # noqa: BLE001
        log.warning("[autopilot] no se pudo avisar al dueño: %s", e)
