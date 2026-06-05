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
    "control":     {"label": "Sala de Máquinas", "icon": "shield",   "title": "Sala de Máquinas — control agéntico", "sub": "Mapa y encendido de la flota y orquestadores", "src": "/alma/control"},
    "inicio":      {"label": "Inicio",           "icon": "home",      "title": "Inicio — Resumen del día",       "sub": "Alertas y KPIs de todos los módulos",              "src": "/alma/inicio"},
    "panel":       {"label": "Panel Recepción",  "icon": "inbox",     "title": "Panel de Recepción v2",          "sub": "Conversaciones · WhatsApp · Agenda",              "src": "/admin/v2"},
    "panel2":      {"label": "Panel Recepción 2","icon": "inbox",     "title": "Panel de Recepción v3 (beta)",   "sub": "Nuevo · cola de atención priorizada · en pruebas","src": "/admin/v3"},
    "agenda":      {"label": "Agenda",           "icon": "calendar",  "title": "Agenda",                         "sub": "Ver citas del dia · Agendar nueva hora",           "src": "/alma/agenda"},
    "pagos_olacore":{"label": "Pagos OLACORE",   "icon": "banknote",  "title": "Pagos del dia (completo)",       "sub": "Registro · copagos · bonif. Imed · Caja/Cierre",   "src": "/alma/pagos"},
    "pagos":       {"label": "Pagos",            "icon": "banknote",  "title": "Pagos del dia",                  "sub": "Registrar y editar pagos · export Excel",           "src": "/alma/pagos-simple"},
    "conciliacion":{"label": "Conciliacion",     "icon": "layers",    "title": "Conciliacion Financiera",        "sub": "Cruce multi-fuente · Imed · hallazgos · cuadre",   "src": "/alma/conciliacion"},
    "boxes":       {"label": "Boxes",            "icon": "grid",      "title": "Boxes — Gemelo Digital",         "sub": "Ocupación y recaudación por box",                  "src": "/boxes"},
    "mensual":     {"label": "DB Mensual",       "icon": "chart",     "title": "Dashboard Mensual",              "sub": "Ingresos y honorarios por profesional",            "src": "/cmc/mensual"},
    "autopilot":   {"label": "Autopilot Ads",    "icon": "target",    "title": "Autopilot de Marketing",         "sub": "Meta Ads · decisiones por rentabilidad real",      "src": "/autopilot"},
    "demanda":     {"label": "Demanda",          "icon": "search",    "title": "Demanda capturada",              "sub": "Qué piden los pacientes que no capturamos",        "src": "/demanda"},
    "inventario":  {"label": "Inventario Dental","icon": "package",   "title": "Inventario Dental",              "sub": "Stock · costos MayorDent · orden de compra",       "src": "/alma/inventario"},
    "proveedores": {"label": "Proveedores",     "icon": "truck",     "title": "Proveedores y compras",          "sub": "Directorio · órdenes de compra · estado",          "src": "/alma/proveedores"},
    # ── Módulos Profesionales (analítica clínica BI-driven por especialidad) ──
    "programas":   {"label": "Programas Clínicos","icon":"heartpulse", "title": "Programas Clínicos",             "sub": "Adherencia y recall por especialidad · lista de hoy","src": "/alma/programas", "grupo": "Módulos Profesionales"},
    "kine":        {"label": "Programa Kine",    "icon": "activity",  "title": "Programa Kinesiología",          "sub": "Adherencia · riesgo de abandono · plan de sesiones","src": "/alma/kine", "grupo": "Módulos Profesionales"},
    "ortodoncia":  {"label": "Ortodoncia",       "icon": "smile",     "title": "Seguimiento Ortodoncia",         "sub": "Controles vencidos · avance · plan de pago",       "src": "/alma/ortodoncia", "grupo": "Módulos Profesionales"},
    "pacientes":   {"label": "Pacientes",        "icon": "users",     "title": "Pacientes — Ficha 360",          "sub": "Buscar · historial · pagos · citas · etiquetas",   "src": "/alma/pacientes"},
    "interconsultas":{"label": "Interconsultas", "icon": "shuffle",   "title": "Interconsultas",                 "sub": "Derivaciones entre especialidades",                "src": "/alma/interconsultas"},
    "esterilizacion":{"label": "Esterilización", "icon": "shield",    "title": "Esterilización",                 "sub": "Trazabilidad de ciclos · indicadores · SEREMI",    "src": "/alma/esterilizacion"},
    "finanzas":    {"label": "Finanzas",         "icon": "wallet",    "title": "Finanzas",                       "sub": "Ingresos vs egresos · flujo · resultado",          "src": "/alma/finanzas"},
    "equipo":      {"label": "Equipo",           "icon": "badge",     "title": "Equipo / RRHH",                  "sub": "Staff · contratos · honorarios · licencias",       "src": "/alma/equipo"},
    "documentos":  {"label": "Documentos",       "icon": "file",      "title": "Documentos y cumplimiento",      "sub": "Consentimientos · protocolos · vencimientos",      "src": "/alma/documentos"},
    "habilitacion":{"label": "Habilitación SEREMI","icon":"clipcheck","title": "Habilitación Sanitaria SEREMI",  "sub": "Expediente · salas · checklist · avance",          "src": "/alma/habilitacion"},
    "mantencion":  {"label": "Mantención",       "icon": "wrench",    "title": "Mantención de equipos",          "sub": "Equipos · preventiva · validación · SEREMI",       "src": "/alma/mantencion"},
    "calidad":     {"label": "Calidad",          "icon": "star",      "title": "Calidad e incidentes",           "sub": "Incidentes · reclamos · seguridad del paciente",   "src": "/alma/calidad"},
    "examenes":    {"label": "Exámenes",         "icon": "beaker",    "title": "Exámenes y resultados",          "sub": "Órdenes · seguimiento · entrega de resultados",    "src": "/alma/examenes"},
    "tareas":      {"label": "Tareas",           "icon": "listcheck", "title": "Tareas del equipo",              "sub": "Pendientes · asignación · vencimientos",           "src": "/alma/tareas"},
    "liquidaciones":{"label": "Liquidaciones",  "icon": "coins",     "title": "Liquidaciones de honorarios",    "sub": "Producción × % · pagado/pendiente por profesional","src": "/alma/liquidaciones"},
    "branding":    {"label": "Brand Board",      "icon": "palette",   "title": "Brand Board — Alma",             "sub": "Identidad visual · sistema de diseño Alma",        "src": "/alma/branding"},
    # Módulo externo (app standalone alma-print). src configurable por env para no
    # incrustar la llave en git; por defecto sin token (se pega una vez en el PC).
    "impresion":   {"label": "Impresión",        "icon": "printer",   "title": "Impresión — imprimir en recepción", "sub": "Manda PDF/imágenes a la impresora del centro",  "src": os.getenv("ALMA_PRINT_URL", "https://print.agentecmc.cl/")},
}

# ── Mapa de perfiles Alma: token → {variante, modulos, boxes_financiero} ────────
# `modulos=None` significa acceso total (todos los módulos del registry).
# `modulos=[...]` lista de keys que el perfil puede ver (sidebar filtra el resto).
# `secciones={modulo: [tabs]}` limita las sub-secciones VISIBLES dentro de un
#   módulo (un nivel más adentro que `modulos`). Ausente o módulo no listado =
#   acceso total a sus secciones. Ej: {"autopilot": ["disenos"]} → solo Diseños.
# `boxes_financiero=True` habilita datos monetarios en /admin/api/boxes-state.
# Para agregar un perfil nuevo: añadir una entrada aquí.
# "variante" es la 3ª línea del lockup (debajo de "CARAMPANGUE" fija). "" = no muestra 3ª línea.
ALMA_PROFILES: dict[str, dict] = {
    "cmc_admin_olacore": {
        "variante": "Adkun",
        "modulos": None,           # acceso total — dueño
        "boxes_financiero": True,
        "panel_profesional": True,
    },
    "cmc_admin_2026": {
        "variante": "Recepción",
        "modulos": ["panel", "panel2", "agenda", "pagos", "impresion", "inventario", "proveedores", "pacientes", "interconsultas", "esterilizacion", "documentos", "examenes", "tareas", "calidad", "programas", "kine", "ortodoncia", "boxes", "autopilot"],  # recepción — "inicio" (vistazo del dueño) reservado a cmc_admin_olacore
        "secciones": {"autopilot": ["disenos"]},  # de Autopilot solo ve Diseños
        "boxes_financiero": False,  # sin valores monetarios en Boxes
        "panel_profesional": False,
    },
    # ── Perfiles por profesional (datos acotados a lo suyo) ──────────────────
    # `profesional_id` = id en Medilink (== bi.dim_profesional.profesional_id).
    # Su sola presencia activa el scoping: cada módulo filtra a ese profesional.
    # Kinesiólogos → Agenda + Kine (Programas no cubre kine, esp 3).
    "cmc_kine_armijo": {
        "variante": "Luis Armijo · Kinesiología",
        "modulos": ["agenda", "kine"],
        "profesional_id": 77,        # Luis Armijo (kine) — ve solo SUS pacientes
        "boxes_financiero": False,
        "panel_profesional": False,
    },
    "cmc_kine_etcheverry": {
        "variante": "Leonardo Etcheverry · Kinesiología",
        "modulos": ["agenda", "kine"],
        "profesional_id": 21,        # Leonardo Etcheverry (kine)
        "boxes_financiero": False,
        "panel_profesional": False,
    },
    # No-kine → módulo Programas (se acota a su programa + sus pacientes).
    "cmc_nutri_pinto": {
        "variante": "Gisela Pinto · Nutrición",
        "modulos": ["agenda", "programas"],
        "profesional_id": 52,        # Gisela Pinto (nutrición)
        "boxes_financiero": False,
        "panel_profesional": False,
    },
    "cmc_psico_montalba": {
        "variante": "Jorge Montalba · Psicología",
        "modulos": ["agenda", "programas"],
        "profesional_id": 74,        # Jorge Montalba (psicología)
        "boxes_financiero": False,
        "panel_profesional": False,
    },
    "cmc_psico_rodriguez": {
        "variante": "Juan Pablo Rodríguez · Psicología",
        "modulos": ["agenda", "programas"],
        "profesional_id": 49,        # Juan Pablo Rodríguez (psicología)
        "boxes_financiero": False,
        "panel_profesional": False,
    },
    "cmc_matrona_gomez": {
        "variante": "Sarai Gómez · Matrona",
        "modulos": ["agenda", "programas"],
        "profesional_id": 67,        # Sarai Gómez (matrona)
        "boxes_financiero": False,
        "panel_profesional": False,
    },
    "cmc_fono_arratia": {
        "variante": "Juana Arratia · Fonoaudiología",
        "modulos": ["agenda", "programas"],
        "profesional_id": 70,        # Juana Arratia (fonoaudiología)
        "boxes_financiero": False,
        "panel_profesional": False,
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

# Puerta de demo del portal: cuando es true, GET /portal/demo entra directo al
# panel con la sesion demo (RUT 50.000.000-7, datos FICTICIOS) sin pedir telefono
# ni codigo. NO toca el login OTP real de /portal. Apagar el flag mata la puerta.
PORTAL_DEMO_OPEN = os.getenv("PORTAL_DEMO_OPEN", "false").lower() in ("true", "1", "yes")

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

# Modalidad asistente de exámenes: si un número de STAFF_PHONES manda una FOTO al
# bot, se transcribe el examen y se le devuelve el texto (para pegar en la ficha de
# Medilink). NUNCA se activa para pacientes (gate por STAFF_PHONES). OFF por defecto.
ASISTENTE_EXAMENES_ENABLED = os.getenv("ASISTENTE_EXAMENES_ENABLED", "false").lower() in ("true", "1", "yes")

# ── Puente asistente Meulen ─────────────────────────────────────────────────
# Si un número de MEULEN_ASSISTANT_PHONES escribe al bot, su mensaje se reenvía
# al bot de Meulen (modo asistente del supermercado) en vez del flujo de
# pacientes. Gateado por número → la clínica/pacientes no se ven afectados.
MEULEN_ASSISTANT_ENABLED = os.getenv("MEULEN_ASSISTANT_ENABLED", "false").lower() in ("true", "1", "yes")
MEULEN_ASSISTANT_PHONES: set[str] = {
    p.strip().lstrip("+") for p in os.getenv("MEULEN_ASSISTANT_PHONES", "").split(",") if p.strip()
}
MEULEN_ASSISTANT_URL    = os.getenv("MEULEN_ASSISTANT_URL", "http://127.0.0.1:8004/assistant")
MEULEN_ASSISTANT_SECRET = os.getenv("MEULEN_ASSISTANT_SECRET", "")

# Gate de seguridad (fail-closed): el puente SOLO se activa si está bien
# configurado. Depende de META_APP_SECRET porque la firma del webhook es lo que
# hace confiable el número del remitente — sin ella, un webhook spoofeado podría
# operar precios/stock de Meulen. Si falta algo, el puente queda inerte (el bot
# médico NO se cae; ese número simplemente seguiría el flujo normal).
def _meulen_url_segura(u: str) -> bool:
    return u.startswith(("http://127.0.0.1", "http://localhost")) or u.startswith("https://")

MEULEN_ASSISTANT_ACTIVE = (
    MEULEN_ASSISTANT_ENABLED
    and bool(MEULEN_ASSISTANT_SECRET)
    and bool(os.getenv("META_APP_SECRET", "").strip())
    and _meulen_url_segura(MEULEN_ASSISTANT_URL)
)
if MEULEN_ASSISTANT_ENABLED and not MEULEN_ASSISTANT_ACTIVE:
    import logging as _lg_mln
    _lg_mln.getLogger("config").error(
        "Puente Meulen DESACTIVADO por seguridad — requiere MEULEN_ASSISTANT_SECRET + "
        "META_APP_SECRET (firma webhook) + URL https/localhost. Revisa el .env."
    )

# Mensajes proactivos: usar Message Templates aprobados por Meta (fuera de ventana 24h).
# Poner en True SOLO cuando los templates estén aprobados en Meta Business Manager.
USE_TEMPLATES = os.getenv("USE_TEMPLATES", "false").lower() in ("true", "1", "yes")

# ── Recordatorios para citas agendadas por recepción (piloto Márquez id=13) ────
# Con RECORDATORIOS_RECEPCION_ENABLED=false (default) el comportamiento es
# idéntico al actual — cero mensajes adicionales, cero cambio observable.
RECORDATORIOS_RECEPCION_ENABLED: bool = (
    os.getenv("RECORDATORIOS_RECEPCION_ENABLED", "false").lower() in ("true", "1", "yes")
)
# IDs de profesional habilitados. Formato env: "13" o "13,73".
_rr_ids_raw = os.getenv("RECORDATORIOS_RECEPCION_PROF_IDS", "13")
RECORDATORIOS_RECEPCION_PROF_IDS: list[int] = [
    int(x.strip()) for x in _rr_ids_raw.split(",") if x.strip().isdigit()
]

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

# ── Agendador público online (cara pública premium del agendamiento) ──────────
# Crea citas REALES en Medilink desde una página pública sin login.
# OFF por defecto: la página y los endpoints de escritura responden 404 hasta
# prenderlo. Mientras está OFF se puede previsualizar con ?preview=ADMIN_TOKEN.
AGENDADOR_PUBLICO_ENABLED = os.getenv("AGENDADOR_PUBLICO_ENABLED", "false").lower() in ("1", "true", "yes", "on")
# Enviar confirmación inmediata por WhatsApp al reservar (requiere template Meta
# aprobado). OFF por ahora; los recordatorios automáticos del cron igual salen
# porque la reserva se registra en citas_bot.
AGENDADOR_WA_CONFIRM = os.getenv("AGENDADOR_WA_CONFIRM", "false").lower() in ("1", "true", "yes", "on")
