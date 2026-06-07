"""Email Marketing — renderizado HTML + envío provider-agnostic (GATED).

Renderiza el email de un segmento a HTML responsive listo para clientes de correo
(tablas + estilos inline, marca CMC/OLACORE), con preheader oculto, CTA único y
footer de baja obligatorio (Ley 21.719 + List-Unsubscribe).

Envío: provider-agnostic detrás de `send_email()`. Nace **gated**: si
`EMAIL_SENDING_ENABLED` no es "true", NO envía — devuelve estado 'gated'. Esto
respeta la decisión de diseño: el motor queda listo, pero ningún correo sale hasta
que se configure proveedor + se confirme el doble opt-in de email.

Adaptadores soportados (cuando se habilite): resend | smtp. Se elige con
`EMAIL_PROVIDER`. Las credenciales viven en .env, nunca en código.
"""
from __future__ import annotations

import html
import logging
import os

log = logging.getLogger("bot")

EMAIL_SENDING_ENABLED = os.getenv("EMAIL_SENDING_ENABLED", "false").lower() == "true"
EMAIL_PROVIDER = os.getenv("EMAIL_PROVIDER", "resend").lower()
EMAIL_FROM_ADDRESS = os.getenv("EMAIL_FROM_ADDRESS", "hola@centromedicocarampangue.cl")
UNSUBSCRIBE_BASE = os.getenv("EMAIL_UNSUBSCRIBE_BASE",
                             "https://agentecmc.cl/email/baja")

# Paleta institucional CMC (Manual de Marca) — coherente con admin_v2 / dashboards.
_NAVY = "#0F3F68"
_BLUE = "#1172AB"
_AQUA = "#4FBECE"
_INK = "#13202e"
_MUTED = "#64798c"
_BG = "#f4f7fa"


def merge(text: str, ctx: dict) -> str:
    """Sustituye merge tags {nombre}, {especialidad}, {comuna}. Defensivo:
    una llave faltante se reemplaza por un fallback neutro, nunca deja el {tag}."""
    if not text:
        return ""
    fallbacks = {"nombre": "", "especialidad": "tu especialidad", "comuna": "tu zona"}
    out = text
    for key, fb in fallbacks.items():
        val = (ctx.get(key) or "").strip() or fb
        out = out.replace("{" + key + "}", val)
    # Limpia un saludo que quedó sin nombre ("Hola , " → "Hola, ")
    out = out.replace(" ,", ",").replace("  ", " ")
    return out


def _esc(s: str) -> str:
    return html.escape(s or "")


def render_email(segment: dict, ctx: dict | None = None, *,
                 unsubscribe_url: str | None = None,
                 preview: bool = False) -> str:
    """Construye el HTML completo del email para un destinatario (ctx con merge data).

    En modo preview usa un ctx de ejemplo y un unsubscribe placeholder.
    """
    em = segment.get("email") or {}
    ctx = ctx or {"nombre": "María", "especialidad": "Kinesiología", "comuna": "Arauco"}
    nombre = (ctx.get("nombre") or "").strip()

    subject = merge(em.get("subject") or "", ctx)
    preheader = merge(em.get("preheader") or "", ctx)
    headline = merge(em.get("headline") or subject, ctx)
    body = merge(em.get("body") or "", ctx)
    cta_label = _esc(em.get("cta_label") or "Agendar por WhatsApp")
    cta_url = em.get("cta_url") or f"https://wa.me/56966610737"
    from_name = _esc(em.get("from_name") or "Centro Médico Carampangue")
    unsub = unsubscribe_url or (UNSUBSCRIBE_BASE + "?p=PREVIEW")

    # Cuerpo en párrafos
    paras = "".join(
        f'<p style="margin:0 0 16px;font-size:15px;line-height:1.6;color:{_INK}">{_esc(p)}</p>'
        for p in body.split("\n") if p.strip()
    )
    greeting = f"Hola {_esc(nombre)}," if nombre else "Hola,"

    return f"""<!DOCTYPE html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="x-apple-disable-message-reformatting">
<title>{_esc(subject)}</title></head>
<body style="margin:0;padding:0;background:{_BG};-webkit-text-size-adjust:100%">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;color:{_BG};font-size:1px;line-height:1px">
{_esc(preheader)}&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;
</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{_BG}">
<tr><td align="center" style="padding:24px 12px">
  <table role="presentation" width="600" cellpadding="0" cellspacing="0"
         style="width:600px;max-width:100%;background:#fff;border-radius:16px;overflow:hidden;
                font-family:Arial,Helvetica,sans-serif;box-shadow:0 4px 18px rgba(15,63,104,.08)">
    <!-- Header marca -->
    <tr><td style="background:linear-gradient(135deg,{_NAVY},{_BLUE});padding:26px 30px">
      <table role="presentation" cellpadding="0" cellspacing="0"><tr>
        <td style="vertical-align:middle">
          <div style="width:40px;height:40px;border-radius:11px;background:rgba(255,255,255,.16);
                      text-align:center;line-height:40px;color:#fff;font-size:20px;font-weight:bold">+</div>
        </td>
        <td style="vertical-align:middle;padding-left:12px">
          <div style="color:#fff;font-size:13px;font-weight:bold;letter-spacing:.5px">Centro Médico</div>
          <div style="color:{_AQUA};font-size:15px;font-weight:bold;letter-spacing:1px">CARAMPANGUE</div>
        </td>
      </tr></table>
    </td></tr>
    <!-- Cuerpo -->
    <tr><td style="padding:32px 30px 8px">
      <h1 style="margin:0 0 6px;font-size:22px;line-height:1.3;color:{_NAVY};font-weight:bold">{_esc(headline)}</h1>
      <p style="margin:0 0 18px;font-size:15px;color:{_MUTED}">{greeting}</p>
      {paras}
    </td></tr>
    <!-- CTA -->
    <tr><td align="center" style="padding:14px 30px 30px">
      <table role="presentation" cellpadding="0" cellspacing="0"><tr>
        <td style="border-radius:12px;background:{_AQUA}">
          <a href="{_esc(cta_url)}" target="_blank"
             style="display:inline-block;padding:15px 30px;font-size:16px;font-weight:bold;
                    color:{_NAVY};text-decoration:none;border-radius:12px">{cta_label} →</a>
        </td>
      </tr></table>
    </td></tr>
    <!-- Footer / baja -->
    <tr><td style="padding:22px 30px;background:#f0f5f9;border-top:1px solid #e6edf3">
      <p style="margin:0 0 8px;font-size:12px;line-height:1.5;color:{_MUTED}">
        Centro Médico Carampangue · Carampangue, Región del Biobío<br>
        WhatsApp: +56 9 6661 0737 · centromedicocarampangue.cl
      </p>
      <p style="margin:0;font-size:11px;line-height:1.5;color:{_MUTED}">
        Recibes este correo porque diste tu consentimiento para comunicaciones del CMC.
        Si no quieres recibir más, <a href="{_esc(unsub)}" style="color:{_BLUE}">date de baja aquí</a>.
      </p>
    </td></tr>
  </table>
  <p style="margin:14px 0 0;font-size:11px;color:{_MUTED};font-family:Arial,sans-serif">
    Enviado por {from_name}
  </p>
</td></tr></table>
</body></html>"""


def render_subject(segment: dict, ctx: dict | None = None) -> str:
    ctx = ctx or {"nombre": "María", "especialidad": "Kinesiología"}
    return merge((segment.get("email") or {}).get("subject") or "", ctx)


def render_confirmation_email(confirm_url: str, nombre: str = "") -> str:
    """Correo de confirmación del DOBLE opt-in (Ley 21.719). Es el segundo paso:
    el paciente dijo 'sí' en WhatsApp y debe confirmar haciendo clic aquí. Sobrio,
    un solo CTA, sin contenido promocional (es un correo transaccional de consent)."""
    greeting = f"Hola {_esc(nombre.split(' ')[0])}," if (nombre or "").strip() else "Hola,"
    return f"""<!DOCTYPE html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Confirma tu suscripción</title></head>
<body style="margin:0;padding:0;background:{_BG}">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{_BG}">
<tr><td align="center" style="padding:24px 12px">
  <table role="presentation" width="600" cellpadding="0" cellspacing="0"
         style="width:600px;max-width:100%;background:#fff;border-radius:16px;overflow:hidden;
                font-family:Arial,Helvetica,sans-serif;box-shadow:0 4px 18px rgba(15,63,104,.08)">
    <tr><td style="background:linear-gradient(135deg,{_NAVY},{_BLUE});padding:26px 30px">
      <div style="color:#fff;font-size:13px;font-weight:bold;letter-spacing:.5px">Centro Médico</div>
      <div style="color:{_AQUA};font-size:15px;font-weight:bold;letter-spacing:1px">CARAMPANGUE</div>
    </td></tr>
    <tr><td style="padding:32px 30px 8px">
      <h1 style="margin:0 0 12px;font-size:21px;color:{_NAVY}">Confirma tu suscripción</h1>
      <p style="margin:0 0 14px;font-size:15px;line-height:1.6;color:{_INK}">{greeting}</p>
      <p style="margin:0 0 14px;font-size:15px;line-height:1.6;color:{_INK}">
        Pediste recibir recordatorios y novedades del Centro Médico Carampangue por
        correo. Para activarlo, confirma con un clic:</p>
    </td></tr>
    <tr><td align="center" style="padding:8px 30px 28px">
      <table role="presentation" cellpadding="0" cellspacing="0"><tr>
        <td style="border-radius:12px;background:{_AQUA}">
          <a href="{_esc(confirm_url)}" target="_blank"
             style="display:inline-block;padding:15px 30px;font-size:16px;font-weight:bold;
                    color:{_NAVY};text-decoration:none;border-radius:12px">Sí, confirmar mi correo →</a>
        </td>
      </tr></table>
    </td></tr>
    <tr><td style="padding:18px 30px;background:#f0f5f9;border-top:1px solid #e6edf3">
      <p style="margin:0;font-size:11px;line-height:1.5;color:{_MUTED}">
        Si no fuiste tú, ignora este correo: sin tu confirmación no te enviaremos nada.
        Centro Médico Carampangue · centromedicocarampangue.cl</p>
    </td></tr>
  </table>
</td></tr></table>
</body></html>"""


# ─────────────────────────────────────────────────────────────────────────────
# Envío provider-agnostic — GATED
# ─────────────────────────────────────────────────────────────────────────────
def sending_status() -> dict:
    """Estado del canal de envío (para que el dashboard explique el gate)."""
    reasons = []
    if not EMAIL_SENDING_ENABLED:
        reasons.append("EMAIL_SENDING_ENABLED=false (envío bloqueado por diseño)")
    if EMAIL_PROVIDER == "resend" and not os.getenv("RESEND_API_KEY"):
        reasons.append("Falta RESEND_API_KEY")
    if EMAIL_PROVIDER == "smtp" and not os.getenv("SMTP_HOST"):
        reasons.append("Falta configuración SMTP")
    return {
        "enabled": EMAIL_SENDING_ENABLED and not reasons,
        "provider": EMAIL_PROVIDER,
        "from_address": EMAIL_FROM_ADDRESS,
        "reasons": reasons,
    }


async def send_email(to: str, subject: str, html_body: str, *,
                     unsubscribe_url: str | None = None) -> dict:
    """Envía un email vía el proveedor configurado. GATED por EMAIL_SENDING_ENABLED.

    Devuelve {status: 'gated'|'sent'|'error', ...}. Nunca lanza: el caller decide.
    """
    st = sending_status()
    if not st["enabled"]:
        return {"status": "gated", "reasons": st["reasons"]}
    try:
        if EMAIL_PROVIDER == "resend":
            return await _send_resend(to, subject, html_body, unsubscribe_url)
        if EMAIL_PROVIDER == "smtp":
            return _send_smtp(to, subject, html_body, unsubscribe_url)
        return {"status": "error", "error": f"proveedor desconocido: {EMAIL_PROVIDER}"}
    except Exception as e:  # noqa: BLE001
        log.error("send_email error a %s: %s", to[-12:], e)
        return {"status": "error", "error": str(e)}


async def _send_resend(to, subject, html_body, unsub) -> dict:
    """Adaptador Resend (https://resend.com). List-Unsubscribe para deliverability."""
    import httpx
    headers = {"Authorization": f"Bearer {os.getenv('RESEND_API_KEY')}",
               "Content-Type": "application/json"}
    payload = {"from": f"Centro Médico Carampangue <{EMAIL_FROM_ADDRESS}>",
               "to": [to], "subject": subject, "html": html_body}
    if unsub:
        payload["headers"] = {"List-Unsubscribe": f"<{unsub}>",
                              "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post("https://api.resend.com/emails", json=payload, headers=headers)
        if r.status_code in (200, 201):
            return {"status": "sent", "id": r.json().get("id")}
        return {"status": "error", "error": f"resend {r.status_code}: {r.text[:200]}"}


def _send_smtp(to, subject, html_body, unsub) -> dict:
    """Adaptador SMTP genérico (STARTTLS)."""
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"Centro Médico Carampangue <{EMAIL_FROM_ADDRESS}>"
    msg["To"] = to
    if unsub:
        msg["List-Unsubscribe"] = f"<{unsub}>"
        msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    with smtplib.SMTP(os.getenv("SMTP_HOST"), int(os.getenv("SMTP_PORT", "587")), timeout=30) as s:
        s.starttls()
        if os.getenv("SMTP_USER"):
            s.login(os.getenv("SMTP_USER"), os.getenv("SMTP_PASSWORD", ""))
        s.sendmail(EMAIL_FROM_ADDRESS, [to], msg.as_string())
    return {"status": "sent"}
