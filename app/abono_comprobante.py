"""
Módulo de lectura de comprobantes de transferencia bancaria.

Recibe bytes de una imagen y devuelve JSON estructurado con los datos del
comprobante. Patrón calcado de ~/meulen-facturas/facturas.py: normalización
de imagen → Claude vision → JSON estricto + manejo de errores.

Modelo: claude-haiku-4-5-20251001.
Haiku tiene visión completa y maneja comprobantes simples perfectamente;
reservamos sonnet solo si la precisión en campo resulta insuficiente tras
pruebas con comprobantes reales (los comprobantes Itaú son nítidos y muy
estandarizados → haiku es suficiente y ~8× más barato).
"""

import base64
import io
import json
import logging
import re

log = logging.getLogger(__name__)

# Mismo modelo que el resto del bot (claude_helper.py)
MODELO = "claude-haiku-4-5-20251001"

_client = None

def _cliente():
    global _client
    if _client is None:
        import anthropic
        _client = anthropic.Anthropic()  # usa ANTHROPIC_API_KEY del entorno
    return _client


_MIME_OK = {"image/jpeg", "image/png", "image/webp", "image/gif"}


def _normalizar_imagen(data: bytes, content_type: str | None) -> tuple[bytes, str]:
    """
    Re-codifica a JPEG (corrige HEIC de iPhone, orientación EXIF, escala).
    Si Pillow no está disponible usa el original si el mime es aceptable.
    Patrón idéntico a meulen-facturas/facturas.py::_normalizar_imagen.
    """
    try:
        try:
            import pillow_heif
            pillow_heif.register_heif_opener()
        except Exception:
            pass
        from PIL import Image, ImageOps
        img = Image.open(io.BytesIO(data))
        img = ImageOps.exif_transpose(img)
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        # 2600px es suficiente para leer texto; más es ruido de red
        if max(img.size) > 2600:
            r = 2600 / max(img.size)
            img = img.resize((int(img.width * r), int(img.height * r)))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=90)
        return buf.getvalue(), "image/jpeg"
    except Exception:
        ct = (content_type or "").lower()
        if ct in _MIME_OK:
            return data, ct
        return data, "image/jpeg"


PROMPT_COMPROBANTE = """Eres un asistente que lee comprobantes de transferencia bancaria de Chile.

Te paso la foto de un comprobante. Extrae los datos y devuelve SOLO el siguiente JSON, sin texto antes ni después, sin ```:

{
  "monto": <número entero en pesos chilenos, sin separador de miles, sin símbolo>,
  "fecha": "<fecha de la transacción en formato YYYY-MM-DD, o null si no aparece>",
  "codigo_operacion": "<número de operación / folio / comprobante, o null>",
  "banco_origen": "<nombre del banco del que sale el dinero, o null>",
  "titular_origen": "<nombre del titular que transfiere, o null>",
  "legible": <true si pudiste leer con certeza al menos el monto y el código de operación, false si la imagen es borrosa, cortada, ilegible o no es un comprobante de transferencia>
}

REGLAS:
- "monto" DEBE ser entero (ej: 20000, no "20.000" ni "$20.000"). Si no puedes leerlo con certeza, usa null.
- Si la imagen no es un comprobante de transferencia bancaria (ej: foto de una persona, receta, examen médico), pon legible: false y el resto en null.
- NO inventes datos. Si un campo no aparece, usa null.
- "legible": true solo si el monto es legible con certeza."""


def _parse_json(texto: str) -> dict:
    """Extrae el JSON del texto de Claude, limpiando posibles prefijos/sufijos."""
    t = texto.strip()
    t = re.sub(r"^```(?:json)?", "", t).strip()
    t = re.sub(r"```$", "", t).strip()
    # Recorta al primer { ... } por si Claude agrega texto
    m = re.search(r"\{.*\}", t, re.DOTALL)
    if m:
        t = m.group(0)
    d = json.loads(t)
    if not isinstance(d, dict):
        raise ValueError("respuesta no es un dict")
    return {
        "monto":            _entero_o_none(d.get("monto")),
        "fecha":            _str_o_none(d.get("fecha")),
        "codigo_operacion": _str_o_none(d.get("codigo_operacion")),
        "banco_origen":     _str_o_none(d.get("banco_origen")),
        "titular_origen":   _str_o_none(d.get("titular_origen")),
        "legible":          bool(d.get("legible", False)),
    }


def _entero_o_none(v) -> int | None:
    if v is None:
        return None
    try:
        # Elimina separadores de miles y símbolo $ por si Claude los incluye
        clean = str(v).replace(".", "").replace(",", "").replace("$", "").strip()
        return int(float(clean))
    except (TypeError, ValueError):
        return None


def _str_o_none(v) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s if s and s.lower() != "null" else None


def leer_comprobante(data: bytes, content_type: str | None = None) -> dict:
    """
    Bytes de imagen → dict con campos del comprobante.

    Retorna siempre un dict con las claves:
        monto, fecha, codigo_operacion, banco_origen, titular_origen, legible

    Nunca lanza excepciones al llamador: en caso de falla retorna
    legible=False con todos los campos en None y 'error' con el mensaje.
    """
    try:
        img, media_type = _normalizar_imagen(data, content_type)
        b64 = base64.standard_b64encode(img).decode()
        resp = _cliente().messages.create(
            model=MODELO,
            max_tokens=512,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": b64,
                }},
                {"type": "text", "text": PROMPT_COMPROBANTE},
            ]}],
        )
        texto = "".join(b.text for b in resp.content if b.type == "text")
        log.debug("leer_comprobante raw: %s", texto[:300])
        return _parse_json(texto)
    except Exception as e:
        log.warning("leer_comprobante error: %s", e)
        return {
            "monto": None,
            "fecha": None,
            "codigo_operacion": None,
            "banco_origen": None,
            "titular_origen": None,
            "legible": False,
            "error": str(e),
        }
