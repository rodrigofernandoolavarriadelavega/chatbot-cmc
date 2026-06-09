"""
Reporte de conversión de la Promo Dental Junio (flyer dental_limpieza_junio_v2).

Mide, de los pacientes que recibieron el flyer (dental_consent='accepted'):
  - cuántos respondieron (inbound) tras el flyer,
  - cuántos agendaron (citas_bot) tras el flyer,
  - cuántos recibieron el flyer por el AUTO-TRIGGER (aceptaron y se les mandó solo).

Fuente única usada por (A) el tile en Autopilot → Win-back y (C) el cron de WhatsApp.
"""
import logging

log = logging.getLogger("dental_promo_report")

# Momento en que arrancó el envío del flyer a los consentidos (2026-06-08 22:20 CLT).
FLYER_START = "2026-06-08 22:00"


def report() -> dict:
    """Snapshot de conversión de la promo dental. Robusto: nunca lanza."""
    out = {
        "recipientes": 0,
        "respondieron": 0,
        "agendaron": 0,
        "auto_trigger": 0,
        "flyer_start": FLYER_START,
        "ok": True,
    }
    try:
        from winback import bi_conn
        from session import _conn, normalize_wa_id as _norm

        with bi_conn() as c:
            cur = c.cursor()
            cur.execute("SELECT phone FROM bi.dental_consent WHERE status='accepted'")
            aceptados = {_norm(r[0]) for r in cur.fetchall() if r[0]}
        out["recipientes"] = len(aceptados)

        with _conn() as s:
            ins = s.execute(
                "SELECT DISTINCT phone FROM messages WHERE direction='in' AND ts >= ?",
                (FLYER_START,),
            ).fetchall()
            out["respondieron"] = sum(1 for r in ins if _norm(r["phone"]) in aceptados)

            try:
                cb = s.execute(
                    "SELECT phone FROM citas_bot WHERE created_at >= ?", (FLYER_START,)
                ).fetchall()
                out["agendaron"] = sum(1 for r in cb if _norm(r["phone"]) in aceptados)
            except Exception as e_cb:
                log.debug("dental_promo_report citas_bot: %s", e_cb)

            try:
                ev = s.execute(
                    "SELECT COUNT(*) FROM conversation_events WHERE event='dental_promo_flyer_enviado'"
                ).fetchone()
                out["auto_trigger"] = int(ev[0] or 0)
            except Exception as e_ev:
                log.debug("dental_promo_report eventos: %s", e_ev)
    except Exception as e:
        log.warning("dental_promo_report fallo: %s", e)
        out["ok"] = False
    return out


def texto_whatsapp(d: dict | None = None) -> str:
    """Versión texto del reporte para enviar al dueño por WhatsApp."""
    d = d or report()
    rec = d.get("recipientes", 0)
    resp = d.get("respondieron", 0)
    ag = d.get("agendaron", 0)
    auto = d.get("auto_trigger", 0)
    conv = f"{(ag / rec * 100):.0f}%" if rec else "0%"
    return (
        "🦷 *Promo Dental Junio — conversión*\n\n"
        f"📨 Recibieron el flyer: *{rec}*\n"
        f"💬 Respondieron: *{resp}*\n"
        f"📅 Agendaron (bot): *{ag}*  ({conv})\n"
        f"🤖 Nuevos por auto-trigger: *{auto}*\n\n"
        "_Solo cuenta agendamientos por el bot; los que llaman a recepción no aparecen._"
    )
