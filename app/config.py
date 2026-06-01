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
# App Secret de la Meta App. Si está seteado, el webhook valida la firma
# X-Hub-Signature-256 de Meta. Si no está seteado, modo legacy (acepta todo).
# Para activar: agregar META_APP_SECRET en .env del server.
META_APP_SECRET      = os.getenv("META_APP_SECRET", "")
META_PAGE_ACCESS_TOKEN = os.getenv("META_PAGE_ACCESS_TOKEN", "") or os.getenv("META_ACCESS_TOKEN", "")
META_MESSENGER_TOKEN = os.getenv("META_MESSENGER_TOKEN", "")  # Page token para Messenger Send API
INSTAGRAM_USER_ID    = os.getenv("INSTAGRAM_USER_ID", "")   # ID del usuario de Instagram Business
META_PAGE_ID         = os.getenv("META_PAGE_ID", "")        # ID de la Página de Facebook
META_WABA_ID         = os.getenv("META_WABA_ID", "")        # WhatsApp Business Account ID (para consultar templates)

CMC_TELEFONO       = os.getenv("CMC_TELEFONO", "+56966610737")
CMC_TELEFONO_FIJO  = os.getenv("CMC_TELEFONO_FIJO", "(44) 296 5226")

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

ADMIN_TOKEN        = os.getenv("ADMIN_TOKEN", "cmc_admin_2026")
OLACORE_TOKEN      = os.getenv("OLACORE_TOKEN", "cmc_admin_olacore")

# ── Registro de módulos Alma ────────────────────────────────────────────────────
# Fuente de verdad de todos los módulos disponibles en el shell Alma.
# key → {label, icon, title, sub, src}.
# Los perfiles abajo referencian keys de este dict.
ALMA_MODULE_REGISTRY: dict[str, dict] = {
    "panel":       {"label": "Panel Recepción",  "icon": "inbox",     "title": "Panel de Recepción v2",          "sub": "Conversaciones · WhatsApp · Agenda",              "src": "/admin/v2"},
    "panel2":      {"label": "Panel Recepción 2","icon": "inbox",     "title": "Panel de Recepción v3 (beta)",   "sub": "Nuevo · cola de atención priorizada · en pruebas","src": "/admin/v3"},
    "agenda":      {"label": "Agenda",           "icon": "calendar",  "title": "Agenda",                         "sub": "Ver citas del dia · Agendar nueva hora",           "src": "/alma/agenda"},
    "pagos":       {"label": "Pagos",            "icon": "banknote",  "title": "Pagos del dia",                  "sub": "Registro de cobros · copagos · bonif. Imed",       "src": "/alma/pagos"},
    "conciliacion":{"label": "Conciliacion",     "icon": "layers",    "title": "Conciliacion Financiera",        "sub": "Cruce multi-fuente · Imed · hallazgos · cuadre",   "src": "/alma/conciliacion"},
    "boxes":       {"label": "Boxes",            "icon": "grid",      "title": "Boxes — Gemelo Digital",         "sub": "Ocupación y recaudación por box",                  "src": "/boxes"},
    "mensual":     {"label": "DB Mensual",       "icon": "chart",     "title": "Dashboard Mensual",              "sub": "Ingresos y honorarios por profesional",            "src": "/cmc/mensual"},
    "autopilot":   {"label": "Autopilot Ads",    "icon": "target",    "title": "Autopilot de Marketing",         "sub": "Meta Ads · decisiones por rentabilidad real",      "src": "/autopilot"},
    "demanda":     {"label": "Demanda",          "icon": "search",    "title": "Demanda capturada",              "sub": "Qué piden los pacientes que no capturamos",        "src": "/demanda"},
}

# ── Mapa de perfiles Alma: token → {variante, modulos, boxes_financiero} ────────
# `modulos=None` significa acceso total (todos los módulos del registry).
# `modulos=[...]` lista de keys que el perfil puede ver (sidebar filtra el resto).
# `boxes_financiero=True` habilita datos monetarios en /admin/api/boxes-state.
# Para agregar un perfil nuevo: añadir una entrada aquí.
# "variante" es la 3ª línea del lockup (debajo de "CARAMPANGUE" fija). "" = no muestra 3ª línea.
ALMA_PROFILES: dict[str, dict] = {
    "cmc_admin_olacore": {
        "variante": "OLACORE",
        "modulos": None,           # acceso total — dueño
        "boxes_financiero": True,
    },
    "cmc_admin_2026": {
        "variante": "",
        "modulos": ["panel", "panel2", "agenda", "pagos", "boxes"],  # recepción
        "boxes_financiero": False,  # sin valores monetarios en Boxes
    },
}

# Feature flags — se activan cuando Rodrigo apruebe condiciones comerciales
# (precios, % descuento, cuenta bancaria, templates Meta).
TELEMEDICINA_ENABLED   = os.getenv("TELEMEDICINA_ENABLED", "false").lower() == "true"
REFERRAL_BONOS_ENABLED = os.getenv("REFERRAL_BONOS_ENABLED", "false").lower() == "true"
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

# FIX-13: Validación pre-flight edad/género por especialidad ─────────────────
# Evita agendar menores en especialidades adultas o vice-versa. El check se
# hace en WAIT_RUT_AGENDAR cuando ya tenemos sexo y fecha_nacimiento del paciente.
EDAD_MIN_ESPECIALIDAD: dict[str, int] = {
    "psicologia adulto":  18,
    "gastroenterologia":  16,
    "cardiologia":        16,
    "implantologia":      18,
    "ginecologia":        12,
    "otorrinolaringologia": 5,
}

EDAD_MAX_ESPECIALIDAD: dict[str, int] = {
    "psicologia infantil": 17,
}

# BUG-3 FIX: Aviso informativo (no bloqueo) cuando el paciente es menor de
# la edad umbral en especialidades que el CMC atiende pero sin pediatría especializada.
# El bot muestra un aviso y pregunta si quiere continuar de todos modos.
EDAD_AVISO_PEDIATRIA: dict[str, int] = {
    "medicina general":  14,
    "medicina familiar": 14,
    "kinesiologia":      14,
    "fonoaudiologia":    14,
    "nutricion":         14,
    "psicologia adulto": 18,  # psicologia adulto ya tiene EDAD_MIN hard; este es el aviso suave
}

# "M" = masculino, "F" = femenino (según campo sexo de Medilink)
GENERO_REQUERIDO: dict[str, str] = {
    "ginecologia": "F",
    "matrona":     "F",
}

# Alternativa sugerida si no cumple restricción
ALTERNATIVA_ESPECIALIDAD: dict[str, str] = {
    "psicologia adulto":   "psicologia infantil",
    "psicologia infantil": "psicologia adulto",
}

# ── Aranceles CMC (CLP) — usados en CAPI Purchase value + winback ────────────
# Actualizar cuando cambien precios. Clave = especialidad normalizada (lowercase).
ARANCELES_CLP: dict[str, int] = {
    "medicina general":             25000,
    "medicina interna":             25000,
    "medicina familiar":            30000,
    "kinesiología":                 20000,
    "kinesiologia":                 20000,
    "otorrinolaringología":         35000,
    "otorrinolaringologia":         35000,
    "odontología general":          48000,
    "odontologia general":          48000,
    "nutrición":                    20000,
    "nutricion":                    20000,
    "podología":                    17000,
    "podologia":                    17000,
    "fonoaudiología":               26000,
    "fonoaudiologia":               26000,
    "cardiología":                  40000,
    "cardiologia":                  40000,
    "traumatología y ortopedia":    35000,
    "traumatologia y ortopedia":    35000,
    "gastroenterología":            35000,
    "gastroenterologia":            35000,
    "psicología":                   35000,
    "psicologia":                   35000,
    "ginecología":                  40000,
    "ginecologia":                  40000,
    "matrona":                      25000,
    "ecografía":                    34000,
    "ecografia":                    34000,
    "tecnólogo médico ecografista": 34000,
    "tecnolo medico ecografista":   34000,
    "ortodoncista":                 30000,
    "ortodoncia":                   30000,
    "implantología":                48000,
    "implantologia":                48000,
    "estética facial":              60000,
    "estetica facial":              60000,
    "endodoncia":                   110000,
    "masoterapia":                  17990,
}

def get_arancel_cpl(especialidad: str | None) -> int:
    """Retorna arancel estimado en CLP para una especialidad (fallback MG=25000)."""
    return ARANCELES_CLP.get((especialidad or "").lower().strip(), 25000)


# Meta Marketing API — cuenta publicitaria del CMC.
# Override en .env: META_AD_ACCOUNT_ID=act_XXXXXXXXXXXXX
META_AD_ACCOUNT_ID = os.getenv("META_AD_ACCOUNT_ID", "act_220608142267129")

# Meta Conversion API (CAPI) — server-side events.
# META_PIXEL_ID: vacío = CAPI deshabilitado (modo OFF seguro).
# META_CAPI_ACCESS_TOKEN: token del System User con permisos ads_management.
#   Fallback a META_ACCESS_TOKEN si no se define por separado.
# META_CAPI_TEST_EVENT_CODE: solo durante testing en Events Manager.
#   Eliminar después de 24-48h con eventos llegando bien.
META_PIXEL_ID             = os.getenv("META_PIXEL_ID", "")
META_CAPI_ACCESS_TOKEN    = os.getenv("META_CAPI_ACCESS_TOKEN", "") or os.getenv("META_ACCESS_TOKEN", "")
META_CAPI_TEST_EVENT_CODE = os.getenv("META_CAPI_TEST_EVENT_CODE", "")
