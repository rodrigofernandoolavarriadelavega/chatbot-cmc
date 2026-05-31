"""Catálogo de formatos publicitarios — fuente única de verdad.

Alimenta tanto el dashboard (`GET /autopilot/api/formats`) como el pipeline de
generación con Canva (skill `cmc-marketing-design` + `docs/marketing_design_playbook.md`).
Cada `key` es estable y se guarda en `design.format` (ver `designs.py`).

Specs basadas en investigación de tamaños 2026 (Buffer / Hootsuite / Sprout / Omnisend):
- Vertical 4:5 (1080×1350) supera al cuadrado en feed Meta.
- Historias / estados 9:16 (1080×1920), zona segura 1080×1610.
- Email: 600 px desktop / 320 px mobile, PNG liviano.

`canva_type` mapea al `design_type` de la tool Canva `generate-design`.
"""

# Cada formato: agrupado por canal para que el dashboard los muestre ordenados.
AD_FORMATS = [
    # ── Instagram ──────────────────────────────────────────────
    {
        "key": "instagram_post", "label": "Post Instagram (vertical)", "channel": "Instagram",
        "canva_type": "instagram_post", "width": 1080, "height": 1350, "ratio": "4:5",
        "safe_zone": None, "copy_max": 125, "cta": "Agenda tu hora por WhatsApp",
        "note": "Formato feed preferido por Meta en 2026. Texto principal grande + 1 CTA.",
    },
    {
        "key": "instagram_square", "label": "Post Instagram (cuadrado)", "channel": "Instagram",
        "canva_type": "instagram_post", "width": 1080, "height": 1080, "ratio": "1:1",
        "safe_zone": None, "copy_max": 125, "cta": "Agenda tu hora por WhatsApp",
        "note": "Cuadrado clásico. Útil para carruseles y grillas uniformes.",
    },
    {
        "key": "instagram_story", "label": "Historia Instagram", "channel": "Instagram",
        "canva_type": "your_story", "width": 1080, "height": 1920, "ratio": "9:16",
        "safe_zone": "1080×1610", "copy_max": 60, "cta": "Desliza / toca para agendar",
        "note": "Pantalla completa. Mantén texto y logo dentro de la zona segura (evita topes).",
    },
    # ── Facebook ───────────────────────────────────────────────
    {
        "key": "facebook_post", "label": "Post Facebook", "channel": "Facebook",
        "canva_type": "facebook_post", "width": 1080, "height": 1350, "ratio": "4:5",
        "safe_zone": None, "copy_max": 140, "cta": "Agenda tu hora por WhatsApp",
        "note": "Vertical rinde mejor que cuadrado. Audiencia 35-65 (núcleo decisor en salud).",
    },
    {
        "key": "facebook_link", "label": "Enlace Facebook (horizontal)", "channel": "Facebook",
        "canva_type": "facebook_post", "width": 1200, "height": 630, "ratio": "1.91:1",
        "safe_zone": None, "copy_max": 100, "cta": "Más información",
        "note": "Imagen estándar para compartir enlaces (al sitio o blog CMC).",
    },
    {
        "key": "facebook_story", "label": "Historia Facebook", "channel": "Facebook",
        "canva_type": "your_story", "width": 1080, "height": 1920, "ratio": "9:16",
        "safe_zone": "1080×1610", "copy_max": 60, "cta": "Toca para agendar",
        "note": "Misma pieza sirve para Instagram Story (cross-post).",
    },
    {
        "key": "facebook_cover", "label": "Portada Facebook", "channel": "Facebook",
        "canva_type": "facebook_cover", "width": 1640, "height": 624, "ratio": "2.63:1",
        "safe_zone": "centro 1080×624", "copy_max": 40, "cta": "—",
        "note": "Cabecera de la página. Branding + horario/teléfono, poco texto.",
    },
    # ── WhatsApp ───────────────────────────────────────────────
    {
        "key": "whatsapp_status", "label": "Estado de WhatsApp", "channel": "WhatsApp",
        "canva_type": "your_story", "width": 1080, "height": 1920, "ratio": "9:16",
        "safe_zone": "1080×1610", "copy_max": 60, "cta": "Responde este estado para agendar",
        "note": "Difusión directa a pacientes. Vertical, legible en miniatura.",
    },
    {
        "key": "whatsapp_image", "label": "Imagen para chat WhatsApp", "channel": "WhatsApp",
        "canva_type": "instagram_post", "width": 1080, "height": 1080, "ratio": "1:1",
        "safe_zone": None, "copy_max": 90, "cta": "Escríbenos para agendar",
        "note": "Cuadrado: se ve completo en la burbuja de chat sin recorte.",
    },
    # ── Email marketing ────────────────────────────────────────
    {
        "key": "email_header", "label": "Cabecera de email", "channel": "Email",
        "canva_type": "email", "width": 600, "height": 200, "ratio": "3:1",
        "safe_zone": None, "copy_max": 60, "cta": "—",
        "note": "600 px desktop / 320 px mobile. PNG liviano (<200 KB). Branding + título.",
    },
    {
        "key": "email_banner", "label": "Banner promocional de email", "channel": "Email",
        "canva_type": "email", "width": 600, "height": 400, "ratio": "3:2",
        "safe_zone": None, "copy_max": 90, "cta": "Agenda tu hora",
        "note": "Bloque hero del correo: oferta/servicio + CTA con botón.",
    },
    # ── Impreso / otros ────────────────────────────────────────
    {
        "key": "poster", "label": "Afiche", "channel": "Impreso",
        "canva_type": "poster", "width": 1080, "height": 1527, "ratio": "A-series",
        "safe_zone": None, "copy_max": 120, "cta": "Agenda tu hora · (44) 296 5226",
        "note": "Para mural de la sala de espera o vitrina. Precio + profesional + teléfono fijo.",
    },
    {
        "key": "flyer", "label": "Flyer / volante", "channel": "Impreso",
        "canva_type": "flyer", "width": 1080, "height": 1527, "ratio": "A-series",
        "safe_zone": None, "copy_max": 140, "cta": "Agenda tu hora por WhatsApp",
        "note": "Reparto en terreno (Carampangue, Arauco, Curanilahue).",
    },
]

FORMAT_BY_KEY = {f["key"]: f for f in AD_FORMATS}


def channels() -> list[str]:
    """Canales en orden de aparición, sin repetir."""
    seen, out = set(), []
    for f in AD_FORMATS:
        if f["channel"] not in seen:
            seen.add(f["channel"])
            out.append(f["channel"])
    return out
