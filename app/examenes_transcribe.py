"""
Transcripción de exámenes para la modalidad asistente del bot.

Cuando un número de STAFF manda la FOTO de un examen al WhatsApp del bot, esto
la transcribe a texto clínico limpio para pegar en la ficha de Medilink.

Reusa el cliente Anthropic y el wrapper de salud de claude_helper (no abre uno
nuevo ni vuelve a leer la API key). Meta entrega las imágenes ya en JPEG, así que
no necesitamos Pillow/HEIC acá.
"""

import base64
import logging

from claude_helper import _claude_create

log = logging.getLogger("examenes_transcribe")

# Modelo de visión. Sonnet da mejor exactitud de OCR que Haiku para un examen.
MODELO_EXAMENES = "claude-sonnet-4-6"
# Modelo de alta precisión para exámenes difíciles (manuscritos, letra chica,
# foto borrosa). Más caro (~5×) pero más exacto. Se elige con modelo="opus".
MODELO_EXAMENES_HD = "claude-opus-4-8"

# Formatos que acepta la API de visión. Meta normalmente manda image/jpeg.
_MIME_OK = {"image/jpeg", "image/png", "image/webp", "image/gif"}

# El prompt define CÓMO queda escrito el examen en la ficha. Mismo criterio que
# la herramienta ~/examen-a-ficha: fiel, no inventa, no diagnostica, marca ↑/↓.
PROMPT_FICHA = """Eres un asistente clínico que transcribe exámenes médicos a la ficha de un paciente en Chile.

Te paso la FOTO de un examen (laboratorio, imagenología, ECG, informe, etc.).
Tu tarea es transcribir su contenido a texto limpio, listo para pegar en la ficha clínica.

REGLAS ESTRICTAS:
- Transcribe FIELMENTE los valores. NO inventes, NO completes datos que no se vean.
- Si un valor o palabra es ilegible, escríbelo como [ilegible]. Nunca adivines un número.
- NO diagnostiques ni interpretes salvo que el propio examen ya traiga la interpretación/conclusión del laboratorio (en ese caso, transcríbela tal cual bajo "Conclusión").
- Mantén unidades y rangos de referencia tal como aparecen.
- Marca con (↑) o (↓) los resultados fuera del rango de referencia que muestra el mismo examen. Si no hay rango, no marques nada.
- Usa español clínico chileno, sobrio. Sin emojis. Sin frases de relleno.

FORMATO DE SALIDA (adáptalo a lo que realmente traiga el examen):
Examen: <nombre del examen>
Fecha: <fecha del examen si aparece>
Laboratorio/Centro: <si aparece>

Resultados:
- <parámetro>: <valor> <unidad> (ref: <rango>) <(↑)/(↓) si corresponde>
- ...

Conclusión/Observaciones: <solo si el examen la trae>

Devuelve SOLO el texto transcrito, sin comentarios tuyos, sin markdown, sin ```."""


async def transcribir_examen(img_bytes: bytes, mime: str | None = None,
                             modelo: str | None = None) -> str:
    """Foto de examen -> texto clínico. Devuelve '' si no se pudo transcribir.

    modelo: None/"sonnet" -> Sonnet (default). "opus" -> alta precisión (manuscritos).
    Solo se aceptan esos dos valores; cualquier otro cae a Sonnet (no se pasa
    texto arbitrario del cliente al campo model de la API).
    """
    model_id = MODELO_EXAMENES_HD if (modelo or "").lower() == "opus" else MODELO_EXAMENES
    media_type = (mime or "").lower()
    if media_type not in _MIME_OK:
        media_type = "image/jpeg"  # Meta entrega JPEG; mejor esfuerzo
    b64 = base64.standard_b64encode(img_bytes).decode()
    try:
        resp = await _claude_create(
            model=model_id,
            max_tokens=2000,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {
                        "type": "base64", "media_type": media_type, "data": b64,
                    }},
                    {"type": "text", "text": PROMPT_FICHA},
                ],
            }],
        )
        return "".join(b.text for b in resp.content if b.type == "text").strip()
    except Exception as e:
        log.error("Error transcribiendo examen: %s", e)
        return ""
