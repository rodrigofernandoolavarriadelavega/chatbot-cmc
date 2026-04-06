import os
from dotenv import load_dotenv

load_dotenv()

MEDILINK_BASE_URL  = os.getenv("MEDILINK_BASE_URL", "https://api.medilink2.healthatom.com/api/v5")
MEDILINK_TOKEN     = os.getenv("MEDILINK_TOKEN", "")
MEDILINK_SUCURSAL  = int(os.getenv("MEDILINK_SUCURSAL", "1"))

ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY", "")

META_ACCESS_TOKEN    = os.getenv("META_ACCESS_TOKEN", "")
META_PHONE_NUMBER_ID = os.getenv("META_PHONE_NUMBER_ID", "")
META_VERIFY_TOKEN    = os.getenv("META_VERIFY_TOKEN", "cmc_webhook_2026")

CMC_TELEFONO       = os.getenv("CMC_TELEFONO", "+56 XX XXX XXXX")
CMC_TELEFONO_FIJO  = os.getenv("CMC_TELEFONO_FIJO", "(41) 296 5226")

ADMIN_TOKEN        = os.getenv("ADMIN_TOKEN", "cmc_admin_2026")
