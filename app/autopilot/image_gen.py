"""Generación de imágenes de marketing con OpenAI gpt-image-1.

Integración server-side: el dashboard (botón "Generar") y el asistente (Claude,
vía el endpoint) piden una pieza describiendo qué quieren; aquí se arma un prompt
con las reglas de marca del CMC + buenas prácticas de salud y se llama a OpenAI.
La imagen vuelve en base64, se guarda como PNG en /static/ad_designs y entra a la
galería (`designs.add_design`).

Reutiliza `OPENAI_API_KEY` del .env (el mismo de Whisper). gpt-image-1 requiere
organización verificada en OpenAI (ya confirmado para esta cuenta).

gpt-image-1 solo acepta tamaños 1024x1024, 1024x1536 (vertical) y 1536x1024
(horizontal): se elige el más cercano al ratio del formato pedido.
"""
import base64
import logging
import os

import httpx

log = logging.getLogger("bot")

OPENAI_IMAGES_URL = "https://api.openai.com/v1/images/generations"
# Modelo de imagen. gpt-image-2 es el más nuevo (mejor render de texto y composición).
# Override por env si OpenAI saca uno superior. Disponibles en la org: gpt-image-1,
# gpt-image-1-mini, gpt-image-1.5, gpt-image-2, chatgpt-image-latest.
OPENAI_IMAGE_MODEL = os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-2")

# ── Perfil de marca CMC (reglas que se inyectan en cada prompt) ──────────────
# Equivalente al carampangue.json que sugería el flujo de diseño: una sola fuente
# para no re-explicar la identidad en cada pedido.
BRAND_CMC = {
    "name": "Centro Médico Carampangue (CMC)",
    "location": "Carampangue, comuna de Arauco, Región del Biobío, Chile",
    "palette": "azul navy profundo (#0F3F68), turquesa clínico (#4FBECE / #3BA9BA) y blanco azulado luminoso",
    "style": (
        "médico moderno, limpio, minimalista, confiable y cálido (humano, no estéril); "
        "mucho espacio en blanco, tipografía sans-serif legible estilo Montserrat, "
        "composición premium y profesional"
    ),
    "whatsapp": "+56 9 6661 0737",
    "phone": "(44) 296 5226",
    # Reglas duras (publicidad de salud + identidad). Ver memorias feedback_no_certificados, feedback_numero_personal.
    "rules": [
        "Estética institucional de salud, sobria y confiable; NO hospital genérico ni quirófano frío.",
        "Imágenes relatables (profesional real atendiendo, ambiente acogedor), no stock clínico estéril.",
        "Foco directo en el servicio o especialidad concreta; nada recargado ni demasiado alejado.",
        "Un solo llamado a la acción claro y visible.",
        "NO usar las palabras 'certificado', 'habilitado', 'acreditado' ni 'Superintendencia'.",
        "NO inventar promesas médicas ni claims sin respaldo.",
        "Texto SIEMPRE en español de Chile, ortografía y tildes perfectas.",
    ],
}


def gpt_size_for(fmt: dict) -> str:
    """Mapea el ratio del formato al tamaño soportado por gpt-image-1 más cercano."""
    w, h = fmt.get("width", 1024), fmt.get("height", 1024)
    if h > w * 1.1:
        return "1024x1536"   # vertical (4:5, 9:16, A-series)
    if w > h * 1.1:
        return "1536x1024"   # horizontal (1.91:1, email, portada)
    return "1024x1024"        # cuadrado


def build_prompt(fmt: dict, *, title: str, subtitle: str = "", specialty: str = "",
                 brief: str = "", cta: str = "") -> str:
    """Arma el prompt para gpt-image-1 combinando marca + formato + contenido."""
    b = BRAND_CMC
    cta = cta or fmt.get("cta", "Agenda tu hora por WhatsApp")
    orient = {"1024x1536": "vertical", "1536x1024": "horizontal", "1024x1024": "cuadrado"}[gpt_size_for(fmt)]
    safe = f" Mantén el texto y los elementos clave dentro de la zona segura ({fmt['safe_zone']})." if fmt.get("safe_zone") else ""

    lines = [
        f"Diseño de marketing {orient} ({fmt['label']}, ratio {fmt['ratio']}) para {b['name']}, {b['location']}.",
        f"Paleta de marca: {b['palette']}. Estilo: {b['style']}.",
    ]
    if specialty:
        lines.append(f"Especialidad o tema destacado: {specialty}.")
    if brief:
        lines.append(f"Mensaje / contexto: {brief}.")
    # Texto exacto a renderizar (gpt-image-1 escribe el texto que se le indica entre comillas).
    lines.append("Renderiza EXACTAMENTE este texto en español, sin errores ortográficos:")
    lines.append(f'- Título principal grande: "{title}".')
    if subtitle:
        lines.append(f'- Subtítulo: "{subtitle}".')
    lines.append(f'- Llamado a la acción destacado: "{cta} {b["whatsapp"]}".')
    lines.append(
        "Composición profesional con jerarquía visual clara, un solo CTA, mucho aire. "
        "Deja un área limpia para sobreponer el logotipo después." + safe
    )
    lines.append("Reglas obligatorias: " + " ".join(b["rules"]))
    return "\n".join(lines)


async def generate_png(prompt: str, size: str = "1024x1536", quality: str = "high") -> bytes:
    """Llama a gpt-image-1 y devuelve los bytes del PNG. Lanza en error."""
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY no configurada")
    payload = {"model": "gpt-image-1", "prompt": prompt, "size": size,
               "quality": quality, "output_format": "png", "n": 1}
    async with httpx.AsyncClient(timeout=180) as client:
        r = await client.post(
            OPENAI_IMAGES_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
        )
    if r.status_code != 200:
        detail = r.text[:300]
        log.error("gpt-image-1 falló (%s): %s", r.status_code, detail)
        raise RuntimeError(f"OpenAI {r.status_code}: {detail}")
    b64 = r.json()["data"][0]["b64_json"]
    return base64.b64decode(b64)
