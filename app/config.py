import os
from dotenv import load_dotenv

load_dotenv()

MEDILINK_BASE_URL  = os.getenv("MEDILINK_BASE_URL", "https://api.medilink2.healthatom.com/api/v5")
MEDILINK_TOKEN     = os.getenv("MEDILINK_TOKEN", "")
MEDILINK_SUCURSAL  = int(os.getenv("MEDILINK_SUCURSAL", "1"))

ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY", "")
OPENAI_API_KEY     = os.getenv("OPENAI_API_KEY", "")   # Whisper para transcripción de audios

META_ACCESS_TOKEN    = os.getenv("META_ACCESS_TOKEN", "")
META_PHONE_NUMBER_ID = os.getenv("META_PHONE_NUMBER_ID", "")
META_VERIFY_TOKEN    = os.getenv("META_VERIFY_TOKEN", "cmc_webhook_2026")
META_PAGE_ACCESS_TOKEN = os.getenv("META_PAGE_ACCESS_TOKEN", "") or os.getenv("META_ACCESS_TOKEN", "")
META_MESSENGER_TOKEN = os.getenv("META_MESSENGER_TOKEN", "")  # Page token para Messenger Send API
INSTAGRAM_USER_ID    = os.getenv("INSTAGRAM_USER_ID", "")   # ID del usuario de Instagram Business
META_PAGE_ID         = os.getenv("META_PAGE_ID", "")        # ID de la Página de Facebook

CMC_TELEFONO       = os.getenv("CMC_TELEFONO", "+56966610737")
CMC_TELEFONO_FIJO  = os.getenv("CMC_TELEFONO_FIJO", "(41) 296 5226")

# Validación crítica: el número personal del Dr. nunca debe ser CMC_TELEFONO.
# Bug detectado 2026-04-28 vía simulador adversarial: .env local tenía
# CMC_TELEFONO=+56987834148 → todas las respuestas con CMC_TELEFONO leakeaban
# el personal. En prod estaba bien, pero la falta de validación es riesgo.
if "987834148" in CMC_TELEFONO.replace(" ", ""):
    import logging as _log_cfg
    _log_cfg.getLogger(__name__).error(
        "CONFIG_ERROR: CMC_TELEFONO=%s es el número PERSONAL del Dr. Olavarría — "
        "NUNCA customer-facing. Forzando default +56966610737 (bot WA Cloud API).",
        CMC_TELEFONO,
    )
    CMC_TELEFONO = "+56966610737"
# Validación menor: código de área del fijo. Carampangue es VIII región → (41).
if "(44)" in CMC_TELEFONO_FIJO:
    import logging as _log_cfg2
    _log_cfg2.getLogger(__name__).error(
        "CONFIG_ERROR: CMC_TELEFONO_FIJO=%s tiene código (44) — Carampangue "
        "es código (41). Forzando default.",
        CMC_TELEFONO_FIJO,
    )
    CMC_TELEFONO_FIJO = "(41) 296 5226"

ADMIN_TOKEN        = os.getenv("ADMIN_TOKEN", "cmc_admin_2026")
ORTODONCIA_TOKEN   = os.getenv("ORTODONCIA_TOKEN", "cmc_ortodoncia_2026")

# Secreto para firmar cookies de sesión admin.
# Si no se define, se deriva automáticamente del ADMIN_TOKEN.
COOKIE_SECRET      = os.getenv("COOKIE_SECRET", "")

# Secreto para firmar cookies del portal del paciente.
PORTAL_SESSION_SECRET = os.getenv("PORTAL_SESSION_SECRET", "")

# Número WhatsApp al que se envían alertas técnicas (caída Medilink, etc.)
# Formato sin "+" ni espacios, ej: 56945886628
ADMIN_ALERT_PHONE  = os.getenv("ADMIN_ALERT_PHONE", "")

# GES Clinical Assistant — servicio interno de triage por síntomas.
# Apuntar al endpoint /triage del backend ges-clinical-app.
GES_ASSISTANT_URL  = os.getenv("GES_ASSISTANT_URL", "http://localhost:8002")

# Teléfonos de profesionales/staff del CMC.
# JSON: {"56912345678": "Dr. Olavarría", "56987654321": "Dra. Burgos", ...}
# Se muestra como badge en el panel admin para que recepción los identifique.
import json as _json
STAFF_PHONES: dict[str, str] = _json.loads(os.getenv("STAFF_PHONES", "{}"))

# Mensajes proactivos: usar Message Templates aprobados por Meta (fuera de ventana 24h).
# Poner en True SOLO cuando los templates estén aprobados en Meta Business Manager.
USE_TEMPLATES = os.getenv("USE_TEMPLATES", "false").lower() in ("true", "1", "yes")

# Google Analytics Data API — para mostrar métricas web en el panel admin.
# GA4_PROPERTY_ID: solo el número (ej: "529028500")
# GA4_CREDENTIALS_PATH: ruta al JSON de la cuenta de servicio
GA4_PROPERTY_ID      = os.getenv("GA4_PROPERTY_ID", "529028500")
GA4_CREDENTIALS_PATH = os.getenv("GA4_CREDENTIALS_PATH", "")
