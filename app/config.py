import os
from dotenv import load_dotenv

load_dotenv()

MEDILINK_BASE_URL  = os.getenv("MEDILINK_BASE_URL", "https://api.medilink2.healthatom.com/api/v5")
MEDILINK_TOKEN     = os.getenv("MEDILINK_TOKEN", "")
MEDILINK_SUCURSAL  = int(os.getenv("MEDILINK_SUCURSAL", "1"))

ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY", "")
OPENAI_API_KEY     = os.getenv("OPENAI_API_KEY", "")   # Whisper para transcripción de audios

# Lector de correos de Medilink (ver app/email_ticker.py) — buzón Gmail al que
# Medilink notifica cada agendamiento/anulación/reagendamiento, con hora exacta
# de creación en la cabecera Date (la API de Medilink NO la entrega). Vacío en
# dev local; solo debe estar seteado en el .env del servidor (permisos 600).
GMAIL_CMC_USER          = os.getenv("GMAIL_CMC_USER", "")
GMAIL_CMC_APP_PASSWORD  = os.getenv("GMAIL_CMC_APP_PASSWORD", "")

META_ACCESS_TOKEN    = os.getenv("META_ACCESS_TOKEN", "")
META_PHONE_NUMBER_ID = os.getenv("META_PHONE_NUMBER_ID", "")
META_VERIFY_TOKEN    = os.getenv("META_VERIFY_TOKEN", "cmc_webhook_2026")
# App Secret de la Meta App. Si está seteado, el webhook valida la firma
# X-Hub-Signature-256 de Meta. Si no está seteado, modo legacy (acepta todo).
# Para activar: agregar META_APP_SECRET en .env del server.
META_APP_SECRET      = os.getenv("META_APP_SECRET", "")
# Instagram App Secret: los webhooks del objeto `instagram` (IG con Instagram
# Login) se firman con ESTE secret, no con META_APP_SECRET. Sin él, las firmas
# de los DM de Instagram no validan y el webhook los rechaza con 403.
INSTAGRAM_APP_SECRET = os.getenv("INSTAGRAM_APP_SECRET", "")
META_PAGE_ACCESS_TOKEN = os.getenv("META_PAGE_ACCESS_TOKEN", "") or os.getenv("META_ACCESS_TOKEN", "")
META_MESSENGER_TOKEN = os.getenv("META_MESSENGER_TOKEN", "")  # Page token para Messenger Send API
INSTAGRAM_USER_ID    = os.getenv("INSTAGRAM_USER_ID", "")   # ID del usuario de Instagram Business
META_PAGE_ID         = os.getenv("META_PAGE_ID", "") or "111650363711290"  # Página FB "Centro Médico Carampangue"
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

ADMIN_TOKEN        = os.getenv("ADMIN_TOKEN", "")
OLACORE_TOKEN      = os.getenv("OLACORE_TOKEN", "")
# Token dedicado del Estudio de Marketing (/marketing) — acceso SOLO a ese panel,
# para dárselo a la encargada de contenido sin exponer el token admin/olacore.
MARKETING_TOKEN    = os.getenv("MARKETING_TOKEN", "cmc_marketing_8f3a2c")

# Tokens de perfiles profesionales (cada uno accede solo a sus datos)
KINE_ARMIJO_TOKEN      = os.getenv("KINE_ARMIJO_TOKEN", "")
KINE_ETCHEVERRY_TOKEN  = os.getenv("KINE_ETCHEVERRY_TOKEN", "")
NUTRI_PINTO_TOKEN      = os.getenv("NUTRI_PINTO_TOKEN", "")
PSICO_MONTALBA_TOKEN   = os.getenv("PSICO_MONTALBA_TOKEN", "")
PSICO_RODRIGUEZ_TOKEN  = os.getenv("PSICO_RODRIGUEZ_TOKEN", "")
MATRONA_GOMEZ_TOKEN    = os.getenv("MATRONA_GOMEZ_TOKEN", "")
FONO_ARRATIA_TOKEN     = os.getenv("FONO_ARRATIA_TOKEN", "")

# ── Registro de módulos Alma ────────────────────────────────────────────────────
# Fuente de verdad de todos los módulos disponibles en el shell Alma.
# key → {label, icon, title, sub, src}.
# Los perfiles abajo referencian keys de este dict.
ALMA_MODULE_REGISTRY: dict[str, dict] = {
    "control":     {"label": "Sala de Máquinas", "icon": "shield",   "title": "Sala de Máquinas — control agéntico", "sub": "Mapa y encendido de la flota y orquestadores", "src": "/alma/control"},
    "inicio":      {"label": "Inicio",           "icon": "home",      "title": "Inicio — Resumen del día",       "sub": "Alertas y KPIs de todos los módulos",              "src": "/alma/inicio"},
    "mapa":        {"label": "Mapa del Centro",  "icon": "map",       "title": "Mapa del Centro — todo lo que hay en marcha", "sub": "Etapa de cada frente · qué falta · qué espera tu decisión", "src": "/alma/mapa"},
    "dashboards":  {"label": "Accesos",          "icon": "shield",    "title": "Accesos — tokens y links por persona", "sub": "Todos los links de entrada a Alma · solo dueño",  "src": "/alma/dashboards"},
    "panel":       {"label": "Panel Recepción",  "icon": "inbox",     "title": "Panel de Recepción v2",          "sub": "Conversaciones · WhatsApp · Agenda",              "src": "/admin/v2"},
    "panel2":      {"label": "Panel Recepción 2","icon": "inbox",     "title": "Panel de Recepción v3 (beta)",   "sub": "Nuevo · cola de atención priorizada · en pruebas","src": "/admin/v3"},
    "recepcion_kanban":{"label":"Cola de Recepción","icon":"listcheck","title":"Cola de Recepción","sub":"A quién le toca responder ahora y por qué","src":"/alma/recepcion-kanban"},
    "roas":        {"label": "ROAS Campañas",    "icon": "trending-up","title": "ROAS por campaña · Meta Ads × Caja real", "sub": "Retorno de cada campaña vs ingreso real (caja)", "src": "/alma/roas"},
    "agenda_ticker":{"label": "Agendamientos en vivo","icon":"activity","title": "Monitor de Agendamientos en vivo", "sub": "Orden real de llegada · canal · citas pasadas sin cerrar", "src": "/alma/agenda-en-vivo"},
    "agenda":      {"label": "Agenda",           "icon": "calendar",  "title": "Agenda",                         "sub": "Ver citas del dia · Agendar nueva hora",           "src": "/alma/agenda"},
    "sala":        {"label": "En sala",          "icon": "users",     "title": "En sala de espera",              "sub": "Pacientes que avisaron que llegaron (check-in QR)", "src": "/alma/sala"},
    "pagos_olacore":{"label": "Pagos OLACORE",   "icon": "banknote",  "title": "Pagos del dia (completo)",       "sub": "Registro · copagos · bonif. Imed · Caja/Cierre",   "src": "/alma/pagos"},
    "pagos":       {"label": "Pagos",            "icon": "banknote",  "title": "Pagos del dia",                  "sub": "Registrar y editar pagos · export Excel",           "src": "/alma/pagos-simple"},
    "abonos":      {"label": "Abonos",           "icon": "banknote",  "title": "Abonos anticipados",             "sub": "Anticipos psiquiatría/fono/nutrición · saldo · no-show","src": "/alma/abonos"},
    "envios":      {"label": "Envíos / Campañas", "icon": "target",    "title": "Envíos / Campañas",              "sub": "Qué manda el bot: templates e imágenes · estado de entrega","src": "/alma/envios"},
    "pagos_medilink":{"label": "Pagos Medilink",  "icon": "banknote",  "title": "Pagos Medilink",                 "sub": "Caja real Medilink · misma fuente que DB Mensual · solo lectura","src": "/alma/pagos-medilink"},
    "caja_diaria_olacore": {"label": "Caja Diaria OLACORE","icon": "banknote", "title": "Caja Diaria (completo)",      "sub": "Libro de caja · depósitos · saldo · historia","src": "/alma/caja-diaria"},
    "caja_diaria": {"label": "Caja Diaria",      "icon": "banknote",  "title": "Caja Diaria",                    "sub": "Efectivo en el cajón ahora · registrar depósito","src": "/alma/caja-diaria-simple"},
    "conciliacion":{"label": "Conciliacion",     "icon": "layers",    "title": "Conciliacion Financiera",        "sub": "Cruce multi-fuente · Imed · hallazgos · cuadre",   "src": "/alma/conciliacion"},
    "boxes":       {"label": "Boxes",            "icon": "grid",      "title": "Boxes — Gemelo Digital",         "sub": "Ocupación y recaudación por box",                  "src": "/boxes"},
    "mensual":     {"label": "DB Mensual",       "icon": "chart",     "title": "Dashboard Mensual",              "sub": "Ingresos y honorarios por profesional",            "src": "/cmc/mensual"},
    "comparador":  {"label": "Comparador",       "icon": "chart",     "title": "Comparador BI",                  "sub": "Comparar rangos de fechas por área o profesional · columnas libres","src": "/cmc/comparador"},
    "ebitda":      {"label": "EBITDA / Resultado","icon": "wallet",    "title": "EBITDA / Resultado Operativo",   "sub": "Ingresos − honorarios − gastos · rentabilidad real del mes","src": "/cmc/ebitda"},
    "autopilot":   {"label": "Autopilot Ads",    "icon": "target",    "title": "Autopilot de Marketing",         "sub": "Meta Ads · decisiones por rentabilidad real",      "src": "/autopilot"},
    "demanda":     {"label": "Demanda",          "icon": "search",    "title": "Demanda capturada",              "sub": "Qué piden los pacientes que no capturamos",        "src": "/demanda"},
    "canal":       {"label": "Canal declarado",  "icon": "search",    "title": "Canal declarado",                "sub": "Cómo dicen los pacientes que nos conocieron · captura recepción","src": "/alma/canal"},
    "captacion":   {"label": "Captación",        "icon": "search",    "title": "Captación de pacientes",         "sub": "¿Cómo se enteran? · tendencia · fuentes · tasa de respuesta","src": "/captacion"},
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
    "checklist":   {"label": "Checklist",        "icon": "clipcheck", "title": "Checklist de recepción",         "sub": "Apertura · día · cierre · cumplimiento",           "src": "/alma/checklist"},
    "liquidaciones":{"label": "Liquidaciones",  "icon": "coins",     "title": "Liquidaciones de honorarios",    "sub": "Producción × % · pagado/pendiente por profesional","src": "/alma/liquidaciones"},
    "branding":    {"label": "Brand Board",      "icon": "palette",   "title": "Brand Board — Alma",             "sub": "Identidad visual · sistema de diseño Alma",        "src": "/alma/branding"},
    "mejoras":     {"label": "Mejoras",          "icon": "clipcheck", "title": "Plan de Mejoras",                "sub": "Auditoría 2026-06-09 · hallazgos · plan accionable", "src": "/alma/mejoras"},
    "grafo":       {"label": "Cerebro Alma",     "icon": "network",   "title": "Cerebro Alma — grafo del organismo", "sub": "Módulos, agentes y datos conectados",          "src": "/alma/grafo"},
    "brain":       {"label": "Copilot Alma",     "icon": "activity",  "title": "Copilot Alma (Brain)",           "sub": "Capa agéntica · sensores · propuestas",            "src": "/alma/brain"},
    "agents":      {"label": "Flota Agentes",    "icon": "grid" ,     "title": "Flota de agentes autónomos",     "sub": "18 agentes · gating en cascada · dry-run",         "src": "/alma/agents"},
    "orquestadores":{"label": "Orquestadores",   "icon": "layers",    "title": "Orquestadores clínicos",         "sub": "Orquestadores · propuestas · inbox",               "src": "/alma/orquestadores"},
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
    # Las claves son los tokens reales leídos de env (OLACORE_TOKEN / ADMIN_TOKEN).
    # Si el token está vacío (env no definida) no se registra la entrada — el dict
    # no tendrá claves vacías que permitan acceso sin token.
    **({OLACORE_TOKEN: {
        "variante": "Adkun",
        "modulos": None,           # acceso total — dueño
        "boxes_financiero": True,
        "panel_profesional": True,
    }} if OLACORE_TOKEN else {}),
    **({ADMIN_TOKEN: {
        "variante": "Recepción",
        "modulos": ["panel", "panel2", "recepcion_kanban", "agenda", "sala", "pagos", "caja_diaria", "abonos", "envios", "impresion", "inventario", "proveedores", "pacientes", "interconsultas", "esterilizacion", "documentos", "examenes", "tareas", "calidad", "programas", "kine", "ortodoncia", "boxes", "autopilot"],  # recepción — "inicio" (vistazo del dueño) reservado a OLACORE_TOKEN
        "secciones": {"autopilot": ["disenos"]},  # de Autopilot solo ve Diseños
        "boxes_financiero": False,  # sin valores monetarios en Boxes
        "panel_profesional": False,
    }} if ADMIN_TOKEN else {}),
    # ── Perfiles por profesional (datos acotados a lo suyo) ──────────────────
    # `profesional_id` = id en Medilink (== bi.dim_profesional.profesional_id).
    # Su sola presencia activa el scoping: cada módulo filtra a ese profesional.
    # Kinesiólogos → Agenda + Kine (Programas no cubre kine, esp 3).
    **({KINE_ARMIJO_TOKEN: {
        "variante": "Luis Armijo · Kinesiología",
        "modulos": ["agenda", "kine"],
        "profesional_id": 77,        # Luis Armijo (kine) — ve solo SUS pacientes
        "boxes_financiero": False,
        "panel_profesional": False,
    }} if KINE_ARMIJO_TOKEN else {}),
    **({KINE_ETCHEVERRY_TOKEN: {
        "variante": "Leonardo Etcheverry · Kinesiología",
        "modulos": ["agenda", "kine"],
        "profesional_id": 21,        # Leonardo Etcheverry (kine)
        "boxes_financiero": False,
        "panel_profesional": False,
    }} if KINE_ETCHEVERRY_TOKEN else {}),
    # No-kine → módulo Programas (se acota a su programa + sus pacientes).
    **({NUTRI_PINTO_TOKEN: {
        "variante": "Gisela Pinto · Nutrición",
        "modulos": ["agenda", "programas", "pagos", "abonos", "pagos_medilink"],
        "profesional_id": 52,        # Gisela Pinto (nutrición)
        "boxes_financiero": False,
        "panel_profesional": True,   # ve su propio panel (scopeado a ella)
        "ver_ingreso": True,         # ve "Ingreso recuperable" en Programas
        "pagos_readonly": True,      # Pagos/Abonos: solo lectura, filtrados a su especialidad
    }} if NUTRI_PINTO_TOKEN else {}),
    **({PSICO_MONTALBA_TOKEN: {
        "variante": "Jorge Montalba · Psicología",
        "modulos": ["agenda", "programas"],
        "profesional_id": 74,        # Jorge Montalba (psicología)
        "boxes_financiero": False,
        "panel_profesional": False,
    }} if PSICO_MONTALBA_TOKEN else {}),
    **({PSICO_RODRIGUEZ_TOKEN: {
        "variante": "Juan Pablo Rodríguez · Psicología",
        "modulos": ["agenda", "programas"],
        "profesional_id": 49,        # Juan Pablo Rodríguez (psicología)
        "boxes_financiero": False,
        "panel_profesional": False,
    }} if PSICO_RODRIGUEZ_TOKEN else {}),
    **({MATRONA_GOMEZ_TOKEN: {
        "variante": "Sarai Gómez · Matrona",
        "modulos": ["agenda", "programas"],
        "profesional_id": 67,        # Sarai Gómez (matrona)
        "boxes_financiero": False,
        "panel_profesional": False,
    }} if MATRONA_GOMEZ_TOKEN else {}),
    **({FONO_ARRATIA_TOKEN: {
        "variante": "Juana Arratia · Fonoaudiología",
        "modulos": ["agenda", "programas"],
        "profesional_id": 70,        # Juana Arratia (fonoaudiología)
        "boxes_financiero": False,
        "panel_profesional": False,
    }} if FONO_ARRATIA_TOKEN else {}),
}

# Feature flags — se activan cuando Rodrigo apruebe condiciones comerciales
# (precios, % descuento, cuenta bancaria, templates Meta).
TELEMEDICINA_ENABLED   = os.getenv("TELEMEDICINA_ENABLED", "false").lower() == "true"
REFERRAL_BONOS_ENABLED = os.getenv("REFERRAL_BONOS_ENABLED", "false").lower() == "true"
ORTODONCIA_TOKEN   = os.getenv("ORTODONCIA_TOKEN", "")

# Promo dental: flyer que se envía AUTOMÁTICAMENTE a quien recién acepta el
# consent dental (consent_dental_v1 → "Sí, acepto"). Gateado: apagar al terminar
# la promo (ej. fin de junio). Template MARKETING con header de imagen aprobado.
DENTAL_PROMO_FLYER_ACTIVE   = os.getenv("DENTAL_PROMO_FLYER_ACTIVE", "false").lower() in ("true", "1", "yes")
DENTAL_PROMO_FLYER_TEMPLATE = os.getenv("DENTAL_PROMO_FLYER_TEMPLATE", "dental_limpieza_junio_v2")
DENTAL_PROMO_FLYER_IMG      = os.getenv("DENTAL_PROMO_FLYER_IMG",
                                        "https://agentecmc.cl/static/promos/dental_limpieza_junio.jpg")

# Datos de transferencia bancaria del CMC (abonos / pagos anticipados).
# Fuente única: cualquier mensaje que pida transferencia debe leer de acá.
# (Dato entregado por el dueño 2026-06-12 para el abono de Psiquiatría.)
CMC_TRANSFERENCIA = {
    "banco":   "Banco Itaú",
    "tipo":    "Cuenta Corriente",
    "numero":  "0221708538",
    "titular": "Centro Médico Carampangue",
    "rut":     "77.140.898-2",
    "correo":  "centromedicocarampangue@gmail.com",
}
# Monto del abono anticipado de Psiquiatría: la CONSULTA COMPLETA ($60.000,
# dato dueño 2026-06-12) — no hay saldo el día de la atención.
ABONO_PSIQUIATRIA_CLP = int(os.getenv("ABONO_PSIQUIATRIA_CLP", "60000"))
ABONO_GASTRO_CLP = int(os.getenv("ABONO_GASTRO_CLP", "35000"))

# Horas que se le dan al paciente para transferir. Eran 90 MINUTOS y el primer
# caso real quedó fuera por 5: transfirió y mandó el comprobante a los 95 min.
# Se recorta al horario del centro (ver calcular_expira en abono_transferencia).
ABONO_VENTANA_HORAS = int(os.getenv("ABONO_VENTANA_HORAS", "4"))

# ── REGISTRO DE ABONOS ──────────────────────────────────────────────────────
# UNA sola fuente. Sumar una prestación al abono = agregar una entrada acá y
# nada más: el gate del bot, el mensaje al paciente, la ventana, el registro
# contable y la conciliación por correo leen todos de este diccionario.
#
# Antes esto estaba disperso: "psiquiatr" escrito a mano en flows.py, un
# _ABONO_POLICY aparte en abonos_routes.py, y la constante del monto en otro
# lado. Agregar gastroenterología obligaba a tocar los tres y era cuestión de
# tiempo que quedaran en desacuerdo.
#
# Lo GENERAL es igual para todas (pedir abono antes de crear la cita, apartar
# la hora, leer el comprobante con visión, confirmar por correo del banco,
# reconocer el ingreso recién con la atención). Acá van solo las
# PARTICULARIDADES de cada una.
#
#   claves:  monto  = lo que se pide por adelantado
#            precio = valor total de la prestación (saldo = precio - monto)
#            profesionales = ids Medilink a los que aplica ([] = toda el área)
#            ventana_horas = opcional; si falta usa ABONO_VENTANA_HORAS
#   gate_bot = True  → el BOT exige el abono ANTES de crear la cita
#              False → abono que registra recepción a mano (sin puerta)
ABONO_REGLAS: dict[str, dict] = {
    "psiquiatría": {
        "etiqueta":      "Psiquiatría",
        "monto":         ABONO_PSIQUIATRIA_CLP,   # consulta completa
        "precio":        ABONO_PSIQUIATRIA_CLP,   # → saldo del día = 0
        "profesionales": [78],                    # Dra. Cecilia Unibazo
        "gate_bot":      True,
    },
    "gastroenterología": {
        "etiqueta":      "Gastroenterología",
        "monto":         ABONO_GASTRO_CLP,        # consulta completa (dueño 29-jul)
        "precio":        ABONO_GASTRO_CLP,        # → saldo del día = 0
        "profesionales": [65],                    # Dr. Quijano
        "gate_bot":      True,
    },
    # Estas dos son abono PARCIAL y las registra recepción en el mesón: el bot
    # no las bloquea al agendar. Viven acá igual para que el monto sugerido, la
    # contabilidad y el ciclo pendiente→aplicado sean los mismos.
    "fonoaudiología": {
        "etiqueta":      "Fonoaudiología",
        "monto":         10_000,
        "precio":        20_000,
        "profesionales": [70],                    # Juana Arratia
        "gate_bot":      False,
    },
    "nutrición": {
        "etiqueta":      "Nutrición",
        "monto":         10_000,
        "precio":        20_000,
        "profesionales": [52],                    # Gisela Pinto
        "gate_bot":      False,
    },
}


def _sin_tilde(t: str) -> str:
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFD", (t or "").lower())
                   if unicodedata.category(c) != "Mn").strip()


def abono_regla(especialidad: str | None = None,
                id_profesional=None, solo_gate: bool = True) -> dict | None:
    """La regla de abono que aplica, o None si esa prestación no lleva abono.

    `solo_gate=True` (por defecto) devuelve SOLO las que el bot debe bloquear
    antes de agendar — que es el uso peligroso, el que le pide plata a un
    paciente. Para listar montos sugeridos en el panel de recepción se llama
    con solo_gate=False y aparecen también las de abono parcial.

    Busca por profesional primero (es exacto) y después por nombre de
    especialidad sin tildes y por prefijo, para que "Gastroenterologia",
    "gastroenterología" y "gastro" caigan todas en la misma entrada.
    """
    def _ok(cfg):
        return (not solo_gate) or bool(cfg.get("gate_bot"))

    if id_profesional is not None:
        try:
            pid = int(id_profesional)
            for clave, cfg in ABONO_REGLAS.items():
                if pid in (cfg.get("profesionales") or []) and _ok(cfg):
                    return {**cfg, "clave": clave}
        except (TypeError, ValueError):
            pass
    if especialidad:
        e = _sin_tilde(especialidad)
        for clave, cfg in ABONO_REGLAS.items():
            if not _ok(cfg):
                continue
            k = _sin_tilde(clave)
            if e == k or e.startswith(k[:8]) or k.startswith(e[:8]):
                return {**cfg, "clave": clave}
    return None

# Confirmación automática de abonos por transferencia (app/abono_transferencia.py,
# 2026-07-14). Lee el Gmail del centro (solo lectura IMAP) y empareja el correo
# del banco contra los abonos pendientes por monto+ventana+nombre. GATEADO —
# con el flag en false no se abre ninguna conexión IMAP ni se registra el cron;
# el flujo de siempre (WAIT_ABONO_COMPROBANTE con foto) sigue exactamente igual.
# Lo enciende el dueño cuando lo revise.
ABONO_AUTO_ACTIVE   = os.getenv("ABONO_AUTO_ACTIVE", "false").lower() in ("true", "1", "yes")
# Minutos que se espera el correo del banco antes de pedirle proactivamente
# la foto del comprobante (fallback — nunca deja al paciente sin salida).
# Calibrado con correos históricos reales (2026-07-14): Falabella y Banco de
# Chile notifican con p90 < 1 minuto tras la transacción — 12 min da margen
# de sobra para la latencia del correo + el ciclo del poller (60s).
ABONO_AUTO_WAIT_MIN = int(os.getenv("ABONO_AUTO_WAIT_MIN", "12"))
# Base pública para el link de la página de transferencia (/abono/{token}).
ABONO_BASE_URL = os.getenv("ABONO_BASE_URL", "https://agentecmc.cl")

# Conciliación de transferencias × correos del banco (app/conciliacion_transferencias.py).
# GATEADO igual que ABONO_AUTO_ACTIVE, y por la misma razón: con el flag en false
# NO se registra el cron y no se abre ninguna conexión IMAP.
#
# 2026-07-28: este flag NO existía. El cron `conciliacion_transferencias_poll` se
# registraba SIEMPRE, así que el solo hecho de deployar el bloque ponía al bot a
# leer el Gmail del centro cada 10 minutos sin ninguna decisión explícita — y sin
# forma de apagarlo que no fuera otro deploy. Su gemelo de abonos sí estaba
# gateado; la asimetría no era intencional, era un olvido.
#
# La diferencia importa: leer el correo del centro es una decisión de negocio y de
# datos (quedan almacenados nombre, banco y monto de gente que ni siquiera es
# paciente del CMC), no un detalle de despliegue.
CONCILIACION_TRANSFERENCIAS_ACTIVE = os.getenv(
    "CONCILIACION_TRANSFERENCIAS_ACTIVE", "false"
).lower() in ("true", "1", "yes", "on")

# Lectura automática de órdenes médicas de eco (foto → tipo → oferta de agenda).
# Práctica 2026-08-01: 15/15 órdenes reales leídas bien. El paciente SIEMPRE
# confirma antes de agendar; si la lectura falla, cae a recepción (flujo actual).
# Ver app/eco_orden_ocr.py.
ECO_ORDEN_OCR_ACTIVE = os.getenv(
    "ECO_ORDEN_OCR_ACTIVE", "false"
).lower() in ("true", "1", "yes", "on")

# Cola de comprobantes de transferencia recibidos por WhatsApp: el clasificador
# de imágenes extrae monto/N° operación/cuenta destino y los encola PRE-cruzados
# (cuenta CMC, duplicados, paciente+cita) en /alma/comprobantes. Recepción
# registra el pago con un click — la plata NUNCA se registra sola.
# Requiere ECO_ORDEN_OCR_ACTIVE (el clasificador). Ver app/comprobantes_pagos.py.
COMPROBANTES_WHATSAPP_ACTIVE = os.getenv(
    "COMPROBANTES_WHATSAPP_ACTIVE", "false"
).lower() in ("true", "1", "yes", "on")

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

# Meta Offline Conversions: enviar conversiones del canal NO-bot (fijo/walk-in) a Meta
# CAPI con identificadores hasheados para que Meta atribuya las llamadas al fijo a los ads
# (ver app/offline_match.py). Envía datos de pacientes a Meta → OFF hasta decisión del dueño.
OFFLINE_MATCH_ENABLED = os.getenv("OFFLINE_MATCH_ENABLED", "false").lower() in ("true", "1", "yes")
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

# ── Asistente Adkun (dueño) ──────────────────────────────────────────────────
# Si el DUEÑO escribe desde un número de ADKUN_ASSISTANT_PHONES, recibe reportes de
# la capa agéntica (P&L, win-back, Director, Autopilot, Optimizador) por WhatsApp en
# vez del flujo de pacientes. READ-ONLY: informa, no ejecuta. OFF por defecto; cualquier
# otro número sigue el flujo clínico normal (gate por número, fail-safe).
ADKUN_ASSISTANT_ENABLED = os.getenv("ADKUN_ASSISTANT_ENABLED", "false").lower() in ("true", "1", "yes")
ADKUN_ASSISTANT_PHONES: set[str] = {
    p.strip().lstrip("+") for p in os.getenv("ADKUN_ASSISTANT_PHONES", "").split(",") if p.strip()
}
ADKUN_ASSISTANT_ACTIVE = ADKUN_ASSISTANT_ENABLED and bool(ADKUN_ASSISTANT_PHONES)

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
    # data["especialidad"] viaja con tilde ("neurología") — ver flows.py:_precio_line
    # para el mismo detalle en PRECIOS_SLOT. Se agregan ambas formas por seguridad.
    "neurología":         15,
    "neurologia":         15,
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
    "bioimpedanciometría":          15000,   # examen aparte (Gisela Pinto), sin bono Fonasa
    "bioimpedanciometria":          15000,
    "bioimpedancia":                15000,
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
    "psiquiatría":                  60000,
    "psiquiatria":                  60000,
    "neurología":                   65000,
    "neurologia":                   65000,
    "tecnología médica oftalmológica": 15000,
    "tecnologia medica oftalmologica": 15000,
    "oftalmología":                 15000,
    "oftalmologia":                 15000,
    "optometría":                   15000,
    "optometria":                   15000,
    "ginecología":                  40000,
    "ginecologia":                  40000,
    "matrona":                      20000,  # F172: particular $20.000 (Fonasa $16.000); era 25000 incorrecto
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
# Agendador v2 (/agendar/v2, rediseño 2026-07-14). Llave PROPIA para que un
# deploy no lo exponga sin decisión del dueño: OFF = 404 salvo ?preview=ADMIN_TOKEN.
# La API es la misma de /agendar (sigue gateada por AGENDADOR_PUBLICO_ENABLED).
AGENDADOR_V2_ENABLED = os.getenv("AGENDADOR_V2_ENABLED", "false").lower() in ("1", "true", "yes", "on")
# Captura de doble opt-in de canal email (Ley 21.719) durante el registro: al
# tomar el correo, dispara el correo de confirmación. Nadie recibe marketing sin
# hacer clic en ese correo. OFF por defecto — el dueño decide cuándo encenderlo.
EMAIL_OPTIN_ENABLED = os.getenv("EMAIL_OPTIN_ENABLED", "false").lower() in ("1", "true", "yes", "on")

# ── Alma Print — impresion remota en recepcion ─────────────────────────────────
# ALMA_PRINT_URL: URL del servidor Alma Print (print.agentecmc.cl).
# ALMA_PRINT_USER_TOKEN: token del usuario en Alma Print (GET /api/printers + POST /api/jobs).
#   Obtenerlo con: grep USER_TOKEN /opt/alma-print/alma-print.env
ALMA_PRINT_URL        = os.getenv("ALMA_PRINT_URL", "https://print.agentecmc.cl").rstrip("/")
ALMA_PRINT_USER_TOKEN = os.getenv("ALMA_PRINT_USER_TOKEN", "")

# ── Alertas fuera de banda (Telegram) ─────────────────────────────────────────
# Rompe dependencia circular: si WhatsApp/Meta caen, las alertas críticas igual llegan.
# Activar pegando las claves en .env (sin ellas es no-op seguro):
#   TELEGRAM_ALERT_TOKEN=<bot token>
#   TELEGRAM_ALERT_CHAT_ID=<chat_id>
TELEGRAM_ALERT_TOKEN   = os.getenv("TELEGRAM_ALERT_TOKEN", "")
TELEGRAM_ALERT_CHAT_ID = os.getenv("TELEGRAM_ALERT_CHAT_ID", "")

# ── Dead-man's switch (healthchecks.io) ───────────────────────────────────────
# Cada cron crítico pingea su URL al terminar exitosamente.
# Si el cron deja de correr, healthchecks.io envía alerta por email/Slack.
# Activar pegando cada URL en .env (sin ella es no-op):
#   HEALTHCHECKS_RECORDATORIOS_URL=https://hc-ping.com/<uuid>
#   HEALTHCHECKS_WAITLIST_URL=https://hc-ping.com/<uuid>
# (crear un check gratis en healthchecks.io por cada cron que quieras vigilar)

# ── Synthetic check del agendamiento ──────────────────────────────────────────
# Cron cada 15 min que ejerce buscar_primer_dia("Medicina General") sin crear citas.
# Si falla N veces seguidas → alerta_oob. Read-only y barato.
# default true porque es completamente inocuo sin las env de Telegram/healthchecks.
SYNTHETIC_CHECK_ENABLED: bool = os.getenv("SYNTHETIC_CHECK_ENABLED", "true").lower() in ("true", "1", "yes")
