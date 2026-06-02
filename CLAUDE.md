# CLAUDE.md — Chatbot WhatsApp Centro Médico Carampangue (CMC)

> **Sprint activo: Win-back** — leé `docs/WINBACK_SPRINT_2026-05.md` antes de tocar `app/winback.py`, `app/custom_audiences_sync.py`, `app/jobs.py::_job_marketing_consent_blast`, handlers de `consent_marketing_v1`, cohortes BI, o flags `WINBACK_ACTIVE` / `MARKETING_CONSENT_BLAST_ACTIVE` / `CROSS_SELL_ACTIVE`. Dashboard live: `https://agentecmc.cl/winback?token=cmc_admin_2026`

> **Antes de tocar slots, horarios, cupos o citas de Medilink: lee `docs/medilink_gotchas.md`.** Contiene las reglas contraintuitivas (intervalo bot ≠ Medilink, horario base vacío, cancelación con id_estado=1, etc.) que si no recuerdas te hacen re-descubrir bugs ya resueltos.

## Instrucciones para Claude Code
- **Actualiza `## Sesión en curso`** cada vez que completes algo relevante (deploy, fix, feature, decisión importante). Hazlo antes de que el contexto se llene.
- **Al iniciar una sesión nueva**, lee esta sección primero para retomar desde donde quedaste.
- Mantén la sección concisa: qué se hizo, qué falta, qué decisiones se tomaron.

## Descripción del proyecto
Chatbot de WhatsApp para el Centro Médico Carampangue (Carampangue, Región del Biobío, Chile).
Permite a los pacientes agendar, cancelar y ver sus citas médicas directamente por WhatsApp.

## Stack tecnológico
- **Backend**: Python 3.11 + FastAPI + Uvicorn
- **IA**: Claude Haiku (`claude-haiku-4-5-20251001`) para detección de intención
- **Agendamiento**: API Medilink 2 (healthatom) — `https://api.medilink2.healthatom.com/api/v5`
- **Mensajería**: Meta Cloud API (WhatsApp Business) — webhook `POST /webhook`
- **Sesiones**: SQLite (`data/sessions.db`) con timeout de 30 minutos
- **Deploy**: DigitalOcean VPS (`157.245.13.107`), uvicorn directo (sin Docker), puerto 8001
- **HTTP client**: httpx (async)

## Estructura del proyecto
```
/
├── app/
│   ├── main.py          # FastAPI app, webhook Meta, rate limiter, scheduler, health (468 líneas)
│   ├── admin_routes.py  # 24 endpoints /admin/api/* (APIRouter) — auth, conversations, kine, ortodoncia
│   ├── messaging.py     # send_whatsapp, send_instagram, send_messenger, Whisper transcripción
│   ├── jobs.py          # 15 cron jobs: recordatorios, reenganche, watchdog, waitlist, fidelización, doctor alerts
│   ├── flows.py         # Máquina de estados (lógica conversacional + mensajes lista/botones + comando dx)
│   ├── claude_helper.py # detect_intent() y respuesta_faq() con Claude Haiku
│   ├── medilink.py      # Wrapper API Medilink (slots, pacientes, citas, agenda del día)
│   ├── session.py       # Sesiones SQLite + log_message, get_conversations, log_event, get_phone_by_rut
│   ├── fidelizacion.py  # Campañas: post-consulta, reactivación, adherencia kine, control, cross-sell
│   ├── reminders.py     # Recordatorios automáticos de citas (09:00 CLT + 2h antes)
│   ├── doctor_alerts.py # Alertas personales doctor: resumen pre-cita + reportes progreso + guías crónicas
│   ├── pni.py           # Programa Nacional de Inmunización: calendario vacunas por edad
│   ├── autocuidado.py   # Tips de autocuidado post-consulta por edad/sexo/especialidad
│   ├── resilience.py    # Modo degradado Medilink (circuit breaker + cola de intenciones)
│   └── config.py        # Variables de entorno (.env)
├── templates/
│   └── admin.html       # HTML del panel de recepción (~1.833 líneas)
├── tests/
│   ├── harness_50.py    # 81 tests offline del flujo conversacional
│   ├── test_normalizer.py # 52 tests del normalizador léxico
│   ├── test_foros_dental_estetica.py # 34 tests con frases reales de foros (requiere API key)
│   └── harness_stress_200.py # 200 casos de stress test
├── data/
│   └── sessions.db      # Base de datos SQLite (no se commitea)
├── requirements.txt
└── .env                 # No commitear — contiene tokens y API keys
```

## Variables de entorno requeridas (.env)
```
MEDILINK_BASE_URL=https://api.medilink2.healthatom.com/api/v5
MEDILINK_TOKEN=...
MEDILINK_SUCURSAL=1
ANTHROPIC_API_KEY=...
OPENAI_API_KEY=...            # Whisper — transcripción de audios WhatsApp
META_ACCESS_TOKEN=...         # Token permanente del System User "Chatbotcmc-systemuser"
META_PHONE_NUMBER_ID=...      # ID del número WhatsApp activo
META_VERIFY_TOKEN=cmc_webhook_2026
CMC_TELEFONO=+56987834148
ADMIN_TOKEN=cmc_admin_2026       # Token para endpoints /admin/*
```

## Cómo correr localmente
```bash
# Desarrollo
uvicorn app.main:app --port 8001 --reload
# En otra terminal:
ngrok http 8001
```

## Deploy en producción (DigitalOcean)

SSH ahora es **solo por llave pública** (Ed25519 en `~/.ssh/id_ed25519`, password deshabilitado el 2026-04-10). Conexión directa con `ssh root@157.245.13.107`.

### Deploy con systemd (recomendado)
```bash
git push origin main
ssh root@157.245.13.107 "cd /opt/chatbot-cmc && git pull && systemctl restart chatbot-cmc"
```

**Verificación post-deploy**:
```bash
curl -s -o /dev/null -w 'HTTP %{http_code}\n' https://agentecmc.cl/health   # → 200
ssh root@157.245.13.107 "systemctl is-active chatbot-cmc"                    # → active
```

Los logs viven en `/var/log/cmc-bot.log` en el servidor.

Ambos servicios corren como **systemd** con auto-restart:
- `chatbot-cmc.service` — uvicorn en `0.0.0.0:8001`, Restart=always, RestartSec=3s. Ruta: `/opt/chatbot-cmc/`
- `ges-assistant.service` — uvicorn en `127.0.0.1:8002`, auto-restart. Ruta: `/opt/ges-assistant/`

## Endpoints
- `GET /health` — health check
- `GET /webhook` — verificación de webhook Meta (hub.verify_token = META_VERIFY_TOKEN)
- `POST /webhook` — recibe mensajes de WhatsApp (Meta Cloud API)
- `GET /admin` — panel web de recepción (requiere `?token=ADMIN_TOKEN`)
- `GET /admin/api/metrics` — métricas JSON (requiere `?token=ADMIN_TOKEN`)
- `GET /admin/api/conversations` — conversaciones JSON (requiere `?token=ADMIN_TOKEN`)

## Flujo de la conversación (máquina de estados en flows.py)
```
IDLE → detect_intent (Claude Haiku)
  → agendar       → WAIT_ESPECIALIDAD → WAIT_SLOT → WAIT_MODALIDAD
                 → WAIT_BOOKING_FOR (¿para ti o para otra persona?)
                   → "Para mí" → WAIT_RUT_AGENDAR (usa perfil si existe)
                   → "Para otra persona" → WAIT_PHONE_OWNER_NAME (si no conocemos al dueño del cel)
                                         → WAIT_RUT_AGENDAR (RUT del paciente real)
                 → WAIT_NOMBRE_NUEVO (paciente nuevo en Medilink)
                 → WAIT_FECHA_NAC → WAIT_SEXO → WAIT_COMUNA → WAIT_EMAIL → WAIT_REFERRAL
                 → CONFIRMING_CITA → reserva creada
  → cancelar      → WAIT_RUT_CANCELAR → WAIT_CITA_CANCELAR → CONFIRMING_CANCEL
  → ver_reservas  → WAIT_RUT_VER
  → disponibilidad → responde con próxima fecha disponible
  → precio/info   → respuesta_faq() con Claude Haiku
  → humano        → derivar a recepción
```

### Comportamientos especiales
- Palabras de emergencia → siempre deriva a SAMU 131 (prioridad máxima)
- "menu/hola/inicio" → resetea sesión y muestra menú
- Menú y selecciones usan mensajes interactivos de WhatsApp (listas y botones)
- Atajos numéricos en IDLE: 1=agendar, 2=cancelar, 3=ver reservas, 4=humano
- "ver todos" en WAIT_SLOT → muestra todos los slots del día
- "otro día" en WAIT_SLOT → busca siguiente día con disponibilidad
- Paciente no encontrado por RUT → flujo de registro (WAIT_NOMBRE_NUEVO)
- Reenganche automático: si un paciente abandona un flujo activo entre 10-60 min, el bot envía un recordatorio (cron cada 5 min)

## Lógica de slots (medilink.py)
- `buscar_primer_dia(especialidad)` — primer día disponible vía `/especialidades/{id}/proxima`, con fallback día por día (60 días)
- `buscar_slots_dia(especialidad, fecha)` — slots para fecha específica
- `smart_select()` — elige 5 mejores slots priorizando compactar la agenda
- Cruza `/agendas` con `/citas` (`estado_anulacion=0`) para detectar slots libres reales
- Filtra con `/horariosbloqueados` por sucursal y fecha
- **IMPORTANTE**: la API Medilink devuelve fechas en `DD/MM/YYYY` en las respuestas; los slots usan la fecha real del API, no la fecha de consulta

## Profesionales habilitados (IDs Medilink)
El campo `intervalo` es la duración de cita por WhatsApp (en minutos). El bot **ignora el intervalo de Medilink** y siempre usa el del dict `PROFESIONALES` en `medilink.py`. Medilink tiene configuraciones de 5–10 min (bloques flexibles para recepcionistas) que no aplican al bot.

| ID | Nombre | Especialidad | Intervalo (min) |
|----|--------|-------------|----------------|
| 1 | Dr. Rodrigo Olavarría | Medicina General | 15 |
| 73 | Dr. Andrés Abarca | Medicina General | 15 |
| 13 | Dr. Alonso Márquez | Medicina General / Medicina Familiar | 20 |
| 23 | Dr. Manuel Borrego | Otorrinolaringología | 20 |
| 60 | Dr. Miguel Millán | Cardiología | 20 |
| 64 | Dr. Claudio Barraza | Traumatología | 15 |
| 61 | Dr. Tirso Rejón | Ginecología | 20 |
| 65 | Dr. Nicolás Quijano | Gastroenterología | 20 |
| 55 | Dra. Javiera Burgos | Odontología General | 60 |
| 72 | Dr. Carlos Jiménez | Odontología General | 30 |
| 66 | Dra. Daniela Castillo | Ortodoncia | 30 |
| 75 | Dr. Fernando Fredes | Endodoncia | 30 |
| 69 | Dra. Aurora Valdés | Implantología | 30 |
| 76 | Dra. Valentina Fuentealba | Estética Facial | 30 |
| 59 | Paola Acosta | Masoterapia | 20 o 40 (pregunta al paciente) |
| 77 | Luis Armijo | Kinesiología | 40 |
| 21 | Leonardo Etcheverry | Kinesiología | 40 |
| 52 | Gisela Pinto | Nutrición | 60 |
| 74 | Jorge Montalba | Psicología Adulto / Psicología Infantil | 45 |
| 49 | Juan Pablo Rodríguez | Psicología Adulto | 45 |
| 70 | Juana Arratia | Fonoaudiología | 30 |
| 67 | Sarai Gómez | Matrona | 30 |
| 56 | Andrea Guevara | Podología | 60 |
| 68 | David Pardo | Ecografía | 15 |

## Cancelación de citas en Medilink
Usar `PUT /citas/{id}` con body `{"id_estado": 1}` — esto pone la cita en estado "Anulado" con `estado_anulacion=1`.
**No usar** `{"estado_anulacion": 1}` solo (da error "Undefined index").

## Creación de citas en Medilink
Requiere el campo `duracion` (minutos). Se calcula como `_h_to_min(hora_fin) - _h_to_min(hora_inicio)`.

## Meta Cloud API
- App ID: 804421499380432
- System User: Chatbotcmc-systemuser (ID: 61576699507415) — token permanente
- Números de prueba: +1 555 641 7609 (Meta test number, sin aprobación requerida)
- **Bot activo en: +56966610737** (antiguo WhatsApp de secretarias, migrado a Cloud API)
- +56945886628 (prepago): quedó fuera de uso tras la migración

## Estado actual del proyecto
- [x] Webhook Meta Cloud API funcional
- [x] Detección de intención con Claude Haiku (AsyncAnthropic)
- [x] Flujo completo de agendamiento
- [x] Flujo de cancelación
- [x] Flujo de ver reservas
- [x] Registro de pacientes nuevos
- [x] Manejo de emergencias
- [x] Sesiones persistentes SQLite
- [x] Mensajes interactivos (listas y botones de WhatsApp)
- [x] Reenganche automático de pacientes que abandonan el flujo
- [x] Panel admin web (`/admin`) con métricas y conversaciones
- [x] Recordatorios automáticos de citas (09:00 CLT)
- [x] Deploy en VPS DigitalOcean (`157.245.13.107`) corriendo con uvicorn
- [x] Migración del número: +56945886628 (prepago inicial) → +56966610737 (número histórico CMC, ahora bot en Cloud API)
- [x] Fidelización completa: post-consulta · reactivación · adherencia kine · control por especialidad · cross-sell kine
- [x] Clasificación de respuesta libre al seguimiento (texto libre → mejor/igual/peor via Claude)
- [x] Panel admin: etiquetas de especialidad legibles, tiempo de espera en formato humano
- [x] Panel "Pacientes en Control": seguimiento de sesiones recurrentes (kine, ortodoncia, psicología, nutrición)
- [x] Instagram chatbot completo: auto-reply + flujo handle_message en texto plano
- [x] Facebook Messenger chatbot completo: misma lógica que IG
- [x] Almacenamiento de archivos de pacientes (fotos, PDFs, docs → `data/uploads/{phone}/`)
- [x] Nombres editables para contactos IG/FB en panel admin (click para editar)
- [x] Extracción de texto PDF/DOCX (PyMuPDF + python-docx)
- [x] Normalización de teléfono (sin prefijo `+`) para evitar sesiones duplicadas
- [x] Detección pasiva de Arauco: si paciente menciona "arauco", guarda tag automáticamente
- [x] Masoterapia con duración variable (20 o 40 min) antes de buscar slots
- [x] Fix timezone: servidor UTC → medilink.py usa `ZoneInfo("America/Santiago")` para no filtrar slots de Olavarría
- [x] Fix medicina general stage 0: slot más próximo entre Abarca (08-16) y Olavarría (16-21); Márquez solo como overflow
- [x] Caché incremental de citas en SQLite (`citas_cache`): primera carga sincroniza desde Medilink, luego instantáneo
- [x] Sync diario automático 23:50 CLT via APScheduler + endpoint `POST /admin/api/kine/sync`
- [x] Paralelización de requests en módulo Pacientes en Control (asyncio.gather, 18s → ~1s)
- [x] Filtros Mes / Año / Todos en modales Pacientes en Control y Ortodoncia
- [x] Módulo Ortodoncia (`🦷` en admin): tabla `ortodoncia_cache` con monto desde `/atenciones`
- [x] Auto-clasificación por monto: $120.000=Instalación, $30.000=Control, otro=Pendiente
- [x] Vista Matriz estilo Excel: filas=pacientes, columnas=fechas, círculos I/C/? con colores
- [x] Toggle Cards ▦ / Matriz ⊞ en modal Ortodoncia
- [x] `ORTODONCIA_TOKEN` separado (`cmc_ortodoncia_2026`) para acceso al módulo
- [x] SQLite WAL + busy_timeout=5000 para concurrencia
- [x] Rate limiter sliding window (30 msg/min) en webhook WA/IG/FB
- [x] Auth admin vía `Authorization: Bearer` (FastAPI `Depends`) + CORS restrictivo
- [x] `/health` con ping real a Medilink y latencia
- [x] ~~`purge_old_data`~~ desactivado — retención indefinida (~90 MB/año, manejable en SQLite)
- [x] `valid_rut` endurecido y masoterapia con matching estricto
- [x] Refactor main.py: 3,045 → 468 líneas; extraído messaging.py, jobs.py, admin_routes.py, templates/admin.html
- [x] Confirmación de audio Whisper: bot responde "Entendí: _{texto}_" al recibir nota de voz
- [x] Whisper deployado en producción (`OPENAI_API_KEY` en `.env` del VPS)
- [x] Pill "Confirman mañana" en topbar admin + modal con detalle (endpoint `/admin/api/confirmaciones`)
- [x] Fix "quiero tapadura" → glosario fuerza intent=info para cualquier mención de tratamiento (no solo preguntas)
- [x] Glosario dental expandido: tapadura caída, dientes chuecos, implante, encías sangrantes, dientes amarillos
- [x] Glosario estética expandido: 9 tratamientos con precios (hilos, lipopapada, exosomas, bioestimuladores, armonización, peeling)
- [x] Suite test_foros_dental_estetica.py: 34/34 casos con frases reales de foros de salud
- [x] Recordatorio 2 horas antes de la cita (cron cada 15 min 7:30-21:30)
- [x] Registro expandido paciente nuevo: fecha_nacimiento, sexo, comuna, email + celular auto-WA. Todo saltable.
- [x] Parser robusto de fecha nacimiento (DD/MM/YYYY, DD-MM-YYYY, DD/MM/YY, 8 dígitos, "15 de marzo de 1990", mes abreviado)
- [x] Abandonment tracking: log_event en cada paso del registro (inicio, skip, completo, abandono por timeout)
- [x] WhatsApp Business features: message status webhooks, BSUID prep, quality rating, document/image sending
- [x] Cross-sell ORL↔Fonoaudiología con prestaciones y precios reales
- [x] Templates `informe_listo` y `seguimiento_medico` registrados y aprobados por Meta
- [x] Panel admin: delivery status icons, envío documentos, botones notificar informe y seguimiento médico
- [x] Retry limit (3 intentos) en WAIT_CITA_CANCELAR/REAGENDAR → escalación a HUMAN_TAKEOVER
- [x] Filtro texto vacío en webhook (whitespace-only)
- [x] Stress test 200 casos (all specialties, professionals, colloquial, edge cases)
- [x] Panel admin: notas persistentes en SQLite (tabla `contact_notes`), autosave con debounce
- [x] Panel admin: notificaciones sonoras (Web Audio beep) + Browser Notification para mensajes nuevos, toggle mute
- [x] Panel admin: pulse animation en botón "Tomar control" cuando hay mensajes pendientes
- [x] Panel admin: atajos teclado Ctrl+K (búsqueda) + Escape (cerrar modales)
- [x] Panel admin: dropdown "Seguimiento" agrupa Pacientes en Control + Fidelización
- [x] Panel admin: tablet responsive (contexto como overlay en 768-1024px)
- [x] Panel admin: pills conversión agendamiento + registros completados/abandonados
- [x] Panel admin: contexto enriquecido (historial citas, lista espera, progreso registro)
- [x] Panel admin: badges canal WA/IG/FB visibles en lista de conversaciones
- [x] Fix celular registro: sin `+`, enviado en campos `celular` y `telefono` a Medilink
- [x] Recordatorio vacunas PNI al confirmar cita pediátrica (calendario completo, vacunas escolares condicionales)
- [x] Tips de autocuidado post-consulta personalizados por edad/sexo/especialidad
- [x] Descripciones de cada procedimiento en glosario de precios del SYSTEM_PROMPT
- [x] Fix ecografías: ginecológica/obstétrica/transvaginal/pélvica → Ginecología (Dr. Tirso Rejón). **Mamaria → David Pardo** (es partes blandas, no ginecológica) — fix 2026-05-22 commits `676239f`+`4906130`. Antes estaba mal asignada a Rejón.
- [x] Indicador de pensando (⏳ reacción WhatsApp) mientras el bot procesa
- [x] Alertas personales Dr. Olavarría: resumen pre-cita 10 min antes + reportes progreso 09/12/16/20
- [x] Exámenes preventivos por edad/sexo en resumen pre-cita (PAP, mamografía, PSA, EMPAM, PNI específico)
- [x] Detección pasiva de patologías crónicas (DM2, HTA, asma, EPOC, +7 más) por keywords en conversación
- [x] Guías clínicas por patología en resumen pre-cita (examen físico, exámenes, metas, recomendaciones)
- [x] Comando `dx` del doctor por WhatsApp: registrar/ver/borrar diagnósticos crónicos por RUT
- [x] Systemd service `chatbot-cmc.service`: auto-restart, arranque al boot, deploy limpio
- [x] Fix timezone: todos los CronTrigger con `timezone="America/Santiago"` (antes corrían en UTC)
- [x] Fix mensajes fidelización en panel admin: log_message en todos los envíos (post-consulta, recordatorios, etc.)
- [x] Referral tracking: pregunta "¿Cómo nos conociste?" en registro (WAIT_REFERRAL), tags referido:*, endpoint /admin/api/referral-stats
- [x] Upsell inteligente post-consulta: cross-sell contextual por especialidad al responder "Mejor" (traumato→kine, MG→chequeo, odonto→estética, kine→masoterapia, ORL↔fono)
- [x] Alerta automática al doctor cuando paciente reporta sentirse "Peor" en seguimiento post-consulta
- [x] Fix get_ultimo_seguimiento: se llama antes de save_fidelizacion_respuesta (antes devolvía None)
- [x] Dashboard métricas fidelización: 3 tabs (métricas trends, campañas estacionales, referidos) en modal Fidelización
- [x] Landing page SEO /landing: JSON-LD MedicalClinic, 16 especialidades, hero con CTA WhatsApp, Open Graph, responsive
- [x] Programa de referidos: código CMC-XXXX auto-generado al registrarse, WAIT_REFERRAL_CODE, validación + tags
- [x] Campañas estacionales: 8 campañas (invierno, vuelta a clases, corazón, diabetes, salud mental, dental, mujer), segmentación por tags, preview + envío manual desde panel
- [x] Cron cumpleaños diario 08:00 CLT + win-back mensual primer lunes 10:00 CLT
- [x] NPS dashboard: pill en topbar admin + modal con NPS por profesional (endpoint /admin/api/nps)
- [x] Campaña cumpleaños: cron diario 08:00 CLT con tips preventivos por edad
- [x] Campaña win-back >90 días: cron primer lunes del mes 10:00 CLT, personalizado por dx:* tags
- [x] Mensaje de bienvenida post-registro en WAIT_REFERRAL
- [x] fecha_nacimiento persistida en contact_profiles para campaña cumpleaños
- [x] Panel admin responsive completo: 6 breakpoints (desktop/tablet/mobile/small phone/landscape/notch)
- [x] Modals fullscreen en mobile, swipe gestures, safe-area padding, touch targets 44px
- [x] Doctor mode persistente con tags (sobrevive resets/timeouts), comando "cambiar modo"
- [x] Agendamiento para terceros: WAIT_BOOKING_FOR + WAIT_PHONE_OWNER_NAME, recordatorios personalizados dueño/paciente
- [x] Pill imágenes (📷) + modal media stats (historial completo)
- [x] Pill demanda (🔎) + tracking especialidades/exámenes no disponibles
- [x] Ley 19.628 compliance: opt-in explícito (`privacy_consents`), derecho al olvido (`DELETE /admin/api/patient`, cascade 18 tablas + audit `gdpr_deletions`), política formal `/privacidad`, playbook SQLCipher/LUKS

## Dashboard admin
- Ruta: `http://157.245.13.107:8001/admin?token=cmc_admin_2026`
- Incluido en el mismo proceso del bot (no es proyecto separado)
- Muestra métricas, conversaciones activas y estado del sistema

## Auditor financiero (`auditor.py`)
Script standalone de conciliación de pagos del CMC. Cruza CSVs de las 6 fuentes de cobro y genera informe de diferencias.

- Lee de los directorios raíz: `RECEPCION/`, `MEDILINK/`, `TRANSFERENCIA/`, `EFECTIVO/`, `TRANSBANK_DEBITO/`, `TRANSBANK_CREDITO/` (cada uno con los CSVs exportados de su fuente).
- Uso:
  - `python auditor.py` — audita todo lo disponible
  - `python auditor.py --desde 2026-03-01 --hasta 2026-03-31` — rango
  - `python auditor.py --output informe.html` — exporta HTML
- No toca el bot en ejecución; es una herramienta offline para el cierre mensual.

## Sesión en curso

### 2026-06-02 (madrugada) — Motor de Programas Clínicos + Tamizaje (carril aislado)
Sesión enfocada en programas por especialidad, 100% aislada a `programas.py`/`bi_helper.py`/templates propios para no colisionar con las sesiones paralelas (transversales + 18 agentes). **LOCAL OK + tests verdes + SQL validado vs BI viva. SIN commit, SIN deploy.**
- `app/programas.py` → `/alma/programas`: motor genérico parametrizado, **11 programas de tratamiento** (adherencia: Nutrición/Psico/Fono/Odonto · control: Cardio/Gastro/Gineco/Matrona/Podología/Traumato/Med.General[1,10]) + **3 cohortes de tamizaje** (PAP, EMPAM, Cardiovascular). Agregar especialidad = entrada de config.
- Agéntico: next-best-action WhatsApp, `build_digest` (lista de hoy unificada incl. Kine+Orto) → `/_/digest` + tool `get_worklist_hoy` del Copilot, `build_overview` → `/_/overview` (portfolio ranqueado por ingreso recuperable), KPI `valor_recuperable`, export con mensaje+wa por paciente.
- ETL `health-bi-project/etl/transform.py::_canon_especialidad`: General→Medicina General (+ UPDATE aplicado en BI viva, esp 1 y 10).
- `bi_helper.py` (bi_query compartido), `programas_report.py` (CLI), `docs/ALMA_PROGRAMAS.md`, `tests/test_alma_programas.py` (6 grupos). Tabla `programa_plan` en sessions.db.
- Verificado: import main OK, 19 rutas mías, 9 endpoints HTTP 200, tests verdes. Deploy: `git add` SELECTIVO de archivos propios + hunks de wiring (NO -A). Ver memory/cmc_alma_kine_ortodoncia_modules.md.

### 2026-06-02 (noche, ~4h autónomas) — Alma Agents: flota de 18 agentes autónomos
Rodrigo: "crea todo lo agentico autonomo que puedas, máximo riesgo, dejalos apagados". **18 agentes que operan la clínica sola. SIN commit, SIN deploy. TODOS APAGADOS (inerte por defecto).**
- Nuevo paquete `app/alma_agents/`: base (Agent+loop) + **guardrails** (authorize: gating cascada maestro→agente→execute→piso legal no-delegable: horas silencio, consent Ley 21.719, **presupuesto de contacto/paciente agregado anti-spam**, Medilink writes, riesgo extremo) + store + registry (autodescubre) + scheduler_hook (0 jobs si maestro off) + 18 agentes en `agents/`.
- Agentes (riesgo): briefing/sre/supervisor/conciliacion/demanda_estrategia (bajo), pricing/seo/creativos/postconsulta/abandono/reputacion (medio), yield_agenda/control_cronico/adherencia_kine/winback/ads_executor/inventario (alto), **cobranza (extremo)**. Reusan winback/fidelizacion/autopilot/inventario/kine/google_rating/conciliacion/alma_brain.
- UI `templates/alma_agents.html` (módulo "Flota de Agentes" /alma/agents, solo dueño, con **dry-run preview** por agente). Doc `docs/ALMA_AGENTS.md`. Tests `tests/test_alma_agents.py` (7 grupos OK).
- Modificados (aditivos): main.py (router + register_agent_jobs gateado), config.py (módulo agentes), alma.html (icono robot).
- Verificado: import main OK (con WIP paralelo), 3 rutas flota, hook 0 jobs con maestro off, dry-run bloquea todo con execute off.
- **OJO COMMIT**: hubo múltiples sesiones paralelas esta noche → `git add` SELECTIVO de app/alma_agents/ + sus archivos + hunks míos de main/config/alma.html. NO `git add -A`. Ver memory/alma_agents_flota.md.


### 2026-06-02 (noche) — Dos módulos Alma para reforzar especialidades: Kine + Ortodoncia
Construido autónomo (Rodrigo durmiendo, "haz el 1 y luego el 2, ponle buen"). Tras analizar la nómina real (BI), se eligió reforzar **Kinesiología** (base sólida, 3 profs, multi-sesión → lever = adherencia) y **Ortodoncia** (ticket alto, 1 profesional, tratamiento largo → lever = controles). **LOCAL OK + test verde + SQL validado contra BI viva. SIN commit, SIN deploy.**
- Nuevos `app/kine_routes.py` (`/alma/api/kine`) y `app/ortodoncia_routes.py` (`/alma/api/ortodoncia`): analítica BI-driven (bi.fact_atenciones esp=3 kine / esp=19 orto + fact_ingresos para monto + dim_paciente.telefono para wa.me). Detección de **episodios** (hueco>45d=nuevo), clasificación por días-sin-sesión (en_curso/riesgo/abandono), worklist accionable, plan opcional. Ortodoncia: meses de tratamiento, controles vencidos (>45d), **plan de pago** (saldo + cartera).
- Capa de gestión propia en sessions.db: tablas nuevas `kine_plan` y `ortodoncia_plan`, **llaveadas por bi.paciente_id** (NO reusan kine_tracking/ortodoncia_cache que usan id Medilink — keyspaces distintos).
- Templates premium `templates/alma_kine.html` + `templates/alma_ortodoncia.html` (clonan el sistema visual de alma_inventario.html: Montserrat + aqua/navy + cards 16px). KPIs, chips por estado, tabla, modal de plan, deep-link WhatsApp, export CSV.
- Registro: main.py (2 routers + 2 ensure_table al arrancar + 2 page handlers /alma/kine y /alma/ortodoncia), config.py (2 entries en ALMA_MODULE_REGISTRY + agregados al perfil recepción), alma.html (íconos activity + smile).
- Degradación elegante: si la BI no responde → listas vacías + source_status="bi_unavailable", nunca 500 (verificado local con BI caída: páginas HTTP 200, APIs JSON limpio).
- Verificado: `import main` OK, 10 rutas alma kine/orto registradas; `tests/test_alma_kine_ortodoncia.py` verde (episodios, estados, override por plan, saldo/cartera); analista corrió el SQL exacto contra BI viva (kine 1.663 filas/221 pacientes, orto 583 filas, LEFT JOIN no duplica).
- **Deploy pendiente (toca el shell que usa recepción)**: `git add app/kine_routes.py app/ortodoncia_routes.py templates/alma_kine.html templates/alma_ortodoncia.html tests/test_alma_kine_ortodoncia.py app/main.py app/config.py templates/alma.html` (add SELECTIVO — hay WIP de alma_brain/operativa sin commitear) → commit → push → ssh restart. Rutas live tras deploy: `/alma` (módulos Programa Kine + Ortodoncia en el sidebar de recepción).

### 2026-06-02 — Alma operativa (Fase 4): relleno de cupos liberados por evento
Cablea el pendiente "should_auto_confirm al waitlist". Cancelación → match candidatos en lista de espera → invitación → primero que acepta aparta (hold blando) → auto-confirma bajo riesgo o cae a recepción. **SIN commit, SIN deploy. Gateado OFF.**
- Nuevo `app/alma_brain/operativa.py`: `fill_freed_slot` (match FIFO + respeta prof preferido + excluye cancelador → fan-out top-N), `accept_offer` (claim atómico + política), `_confirmar_en_medilink`.
- `session.py`: tabla `waitlist_offers` + `claim_offer` ATÓMICO ("primero que acepta gana": SQLite single-writer serializa, el 2º ve el 'apartado' commiteado y pierde) + getters.
- `policy.py`: `should_auto_confirm(OfferContext)` — requiere paciente conocido + RUT válido + esp coincide + inscripción formal; margen escalonado (escasas cardio/gastro/otorrino/gineco → 48h, resto → 24h). Flag dedicado angosto `ALMA_OPERATIVA_AUTOCONFIRM` (NO usa ALMA_BRAIN_ALLOW_MEDILINK_WRITES).
- Enganches: `jobs.py` dispara fill SOLO en cancelación `anulada` (reasignada NO libera) + expiry TTL holds; `flows.py` handler de aceptación en IDLE (matcher angosto "TOMAR/sí"); `admin_routes.py` 3 endpoints recepción (`/admin/api/operativa/offers` + confirm + reject).
- Template Meta `oferta_cupo` (botón "Tomar la hora", entrega fuera de 24h): `scripts/register_oferta_cupo_template.py` (PENDIENTE registrar+aprobar en Meta). `_enviar_invitacion` usa template si USE_TEMPLATES, fallback texto libre.
- Tests: `tests/test_alma_operativa.py` 7/7 + `tests/test_alma_brain.py` 7/7. Import profundo de session/jobs/flows/admin_routes/operativa/policy OK.
- **Kill-switches OFF por defecto**: `ALMA_OPERATIVA_ENABLED=false` (no contacta a nadie) + `ALMA_OPERATIVA_AUTOCONFIRM=false` (todo a recepción). Con flags off el bot es idéntico a hoy.
- **Pendiente**: afinar regla bajo riesgo (Rodrigo); registrar+aprobar template Meta; UI del panel para los holds en recepción (endpoints listos, falta el front); encender flags solo tras Fase 0 (auth dura + auditoría).

### 2026-06-02 (madrugada) — Alma Brain: capa agéntica de Alma
Construido autónomo (Rodrigo durmiendo, "armalos todos"). **4 fases end-to-end, probadas con API real. SIN commit, SIN deploy. Gateado OFF.**
- Nuevo paquete `app/alma_brain/`: sensors (Medilink-free: bi.fact_citas/fact_pagos/sessions.db/snapshots) → state (alertas cross-domain) → copilot (Claude Sonnet con tool-use REAL) → tools (cola de propuestas + executors) → policy (HardLimits + Ley 21.719 + should_auto_confirm).
- UI `templates/alma_copilot.html` (módulo "Copilot Alma" en el shell, /alma/brain). Tests `tests/test_alma_brain.py` (6 OK). Doc `docs/ALMA_BRAIN.md`.
- Modificados: main.py (router + cron 06:00), config.py (módulo cerebro), alma.html (icono sparkles).
- Verificado: `import main` OK, 7 rutas registradas, copiloto encadenó tools y razonó cross-domain, policy bloqueó ejecución (EXECUTE=false → aprobada_manual).
- **Pendiente (requiere aprobación, toca prod)**: cablear should_auto_confirm al waitlist; executors reales por kind; sensor agenda fino. Ver docs/ALMA_BRAIN.md y memory/alma_brain_agentico.md.

---

**Fecha**: 2026-04-27 / 2026-04-28 (sesión maratónica que cubrió varios frentes)
**Historial completo**: ver claude-mem timeline o git log

---

### Resumen sesión 2026-04-27 / 2026-04-28

#### Deploys del día (en orden cronológico, todos en producción)
1. `7fc3af1` — feat(menu): grupo "Estratégicos · OLACORE" con SEO + Meta + Crecimiento + Horizonte
2. `884f251` — fix(geocoder): jitter determinístico en fallback dispersa clusters falsos (script `redistribute_fallback_jitter.py` ya ejecutado en server, 2741 entries re-distribuidas)
3. `dc72651` — feat(menu+meulen): card "App Meulen (POS+Admin)" + dashboard `/meulen/kpis` con 6 tabs
4. `1621c1a` — fix(admin/v2): contexto en mobile como overlay con backdrop (panel admin v2 era inservible en mobile)
5. `8cefcf7` — fix(admin): **NameError _hmac_admin → hmac** (Recepción tenía panel CAÍDO, HTTP 500 en `/admin/api/conversations` con token query)
6. `379a170` — feat(flows): **bloqueo duro de 1 cita activa por paciente y profesional** (caso Yesenia Reyes: agendó 3 horas con Dr. Márquez el mismo día). Estado: bloqueo retorna mensaje y resetea sesión, log_event `cita_bloqueada_mismo_profesional`
7. `637aa59` — feat(atribucion): tracking de referidos post-cita + dashboard `/atribucion` + endpoint `GET /api/atribucion/today` + agente `cmc-attribution-reporter`
8. `45907e4` — feat(flows): mensaje de derivación cuando especialidad no está en CMC (incluye CESFAM red SSC + clínicas privadas Concepción) + cache de typos kinesiología (kinesiologo, quinesiologo, quiniciologo, etc.). **Sin Doctoralia/Reservo por decisión del usuario**
9. `b7e2165` + `a1f121a` — fix(messaging): defense-in-depth `_final_phone_guard` + import `re` faltante. **Bot crashed 3 minutos** entre 02:29-02:32 UTC por NameError, hotfix recuperó.

#### Cambios en código clave (saber dónde están)
- `app/flows.py` línea ~4232 — bloqueo "1 cita por profesional" en `CONFIRMING_CITA`. Listar citas paciente, si hay misma `id_profesional` futura → reset_session + mensaje de bloqueo
- `app/flows.py` línea ~4915 — `data["is_paciente_nuevo_post_referral"] = True` flag
- `app/flows.py` línea ~4444 — post-confirmación, si flag activo, manda 2 mensajes: confirmación + pregunta referido (botones "Amigo / Redes-Google / Recurrente"), set state `WAIT_REFERRAL_POST`
- `app/flows.py` línea ~5054 — handler `WAIT_REFERRAL_POST` (mapea botón a tag, save_tag, log_event `registro_referral_post`)
- `app/flows.py` línea ~6433 — mensaje de derivación (CESFAM + 4 clínicas privadas Concepción)
- `app/messaging.py` líneas 140-160 — `_final_phone_guard` aplicado a `send_whatsapp`, `send_instagram`, `send_messenger`
- `app/admin_routes.py` líneas 126/134/295 — `_hmac_admin` → `hmac` (3 ocurrencias)
- `app/main.py` línea ~534 — endpoint `/api/atribucion/today` (Marketing API + sessions.db cruce)
- `templates/atribucion_dashboard.html` — dashboard auto-refresh 5min
- `scripts/redistribute_fallback_jitter.py` — re-jitter SHA-1 ±0.003° para entries fallback existentes
- `scripts/geocode_direcciones.py` — jitter ahora determinístico desde el inicio

#### Agentes Claude Code (en `~/.claude/agents/`)
**11 agentes totales** = 5 auditores read-only (cmc-bugs, cmc-medical, cmc-performance, cmc-security, cmc-ux) + 6 constructores nuevos:
- `cmc-bot-engineer` — implementa fixes/features en flows/medilink/jobs
- `cmc-data-analyst` — queries SQL sessions.db + heatmap_cache.db (sabe que sessions.db es SQLCipher, env var `SQLCIPHER_KEY`)
- `cmc-dashboard-builder` — patrón FastAPI+Tailwind+Chart.js+OLACORE
- `olacore-brand-designer` — brand boards estilo OLACORE/Olamar/Oris/Austra
- `clinic-strategist` — decisiones estratégicas (modelo opus)
- `cmc-conversation-auditor` — bugs de producción cruzando data real
- `cmc-attribution-reporter` — reportes diarios Meta×Bot×Pacientes (creado hoy)

**IMPORTANTE**: en sesión actual NO se pueden invocar (solo se cargan al iniciar Claude Code). En próxima sesión disponibles automáticamente.

#### Bug del leak histórico del número personal Dr.
- Auditoría completa: **60 mensajes outbound entre 2026-03-30 y 2026-04-20** leakearon `+56987834148` a 60 phones distintos
- Causa: Claude Haiku hallucinaba que ese era el WhatsApp del CMC
- Fix `_scrub_telefonos` en `claude_helper.py` se implementó el 20-21 abr (3 puntos: línea 1155, 1191, 1392)
- **Cero leaks en últimos 7 días** (verificado vía SQL)
- Defense-in-depth añadida hoy en `messaging.py` (`_final_phone_guard`) — última puerta antes de canal, loggea `WARNING PHONE_LEAK_GUARD personal_number_caught` si detecta regresión
- Riesgo residual: esos 60 phones pueden tener guardado el número personal del Dr.

#### Atribución diaria (datos reales del 27-abr)
- Meta spend: **CLP $4.930** (anómalamente bajo vs baseline ~CLP $200K/día) — **revisar Ads Manager**
- Phones nuevos: 65 · Citas creadas: 30 · **Conversión 32.3%**
- 0 tags de referido pre-fix (el flujo `WAIT_DATOS_NUEVO` saltaba `WAIT_REFERRAL`). Ahora con `WAIT_REFERRAL_POST` empezarán a llegar.
- Endpoint live: `https://agentecmc.cl/api/atribucion/today`
- Dashboard: `https://agentecmc.cl/atribucion`

#### Hallazgos de auditoría conversaciones 7d (no todos arreglados)
- 8 pacientes con múltiples reservas mismo profesional (caso Yesenia y otros 7) → **arreglado** con feature
- 7 casos "slot ya ocupado al confirmar" — race condition concentrada en Dr. Márquez/Olavarría → **NO arreglado** (idea: reserva tentativa 30s)
- Demanda no satisfecha 30d: ecografía 19, gastroenterología 14, implantología 10, cardiología 7 → typo capitalización en `sin_disponibilidad` event (NO arreglado)
- 94 intents "otro" en 7d (mal clasificados) — sample muestra 3 buckets: agradecimientos/cierre, status updates a recepción, saludos → no normalizado al cache aún
- **Bug crítico NO arreglado**: María 56968621918 pidió hora "para hoy", el bot le mostró slots de mañana sin avisar → revisar `WAIT_SLOT` cuando paciente especifica fecha y no hay slots ese día
- Otras 5 conversaciones llegaron a WAIT_SLOT y se cayeron sin convertir

#### Brand boards dental Concepción (en `~/Downloads/`)
3 brand boards completos creados (HTML+SVG+CSS+mockups) bajo paraguas OLACORE:
- `OLAMAR_brand/` — ola+mar, costa Concepción, agua marina #5B8B96
- `ORIS_brand/` — boca en latín, oficio dental clínico, esmeralda #2D5F4E (riesgo: marca relojera Oris)
- `AUSTRA_brand/` — sur en latín, escalable a red dental sur de Chile, pizarra #3D4F5C (riesgo: confusión "Austria")

**Decisión pendiente del Dr.**: cuál marca usar. Mi recomendación: Austra si hay ambición de expandir al sur (Talcahuano/Los Ángeles/Temuco), Oris si solo Concepción premium dental. Olamar perdió frente a esos dos.

**Contexto crítico de la decisión**: la ortodoncista Dra. Daniela Castillo (ID Medilink 66) vive en Concepción. Si se va full a la sub-marca dental, **CMC pierde ~10 pacientes recurrentes de Curanilahue** que iban a controles ortodónticos (5.1 sesiones/paciente promedio). Decidir si la dejás 1 día/semana en CMC o se va completa.

#### Otros dashboards/herramientas creadas
- `/horizonte` — roadmap estratégico CMC con escenarios A/B/C + pipeline contratación interactivo (CRUD endpoints `/api/hiring/pipeline`, tabla `hiring_pipeline` en heatmap_cache.db)
- `/meulen/kpis` — dashboard MVP Meulen (Fase 1 cerrada, 7 módulos backend, 122 tests 83% cobertura, riesgos)
- `/atribucion` — Meta×Bot×Pacientes diario
- App Meulen ya estaba desplegada en `/supermercadomeulen/menu` pero no enlazada — agregada al menú principal

#### Conversación importante con el Dr. (no técnica pero clave)
- Dr. estudió 1 año de Ing Civil UC (plan común, intención Mecatrónica/Industrial) en 2025
- Lo dejó porque prestó plata a sus padres (Meulen entró en crisis) y no podía sostener Stgo
- Reflexión brutal pero respetuosa: 60 mensajes históricos donde el bot dejó leakear su personal a pacientes — eso aplica el mismo patrón "subsidio familiar/personal sin límites" que con Meulen. Saber decir "sí ayudo, pero con reglas claras" aplica tanto a familia como a privacidad técnica.
- **NO retomar este tema sin que él lo abra**.

#### Bug del banco Itaú (no técnico del CMC pero relevante)
- Dr. tuvo problema accediendo a banco.itau.cl
- Memoria sugería Imperva bloqueando ISP Pacífico Cable, pero estaba en datos móviles → no era ISP
- Llamó al banco, era error del banco
- **Mi diagnóstico estaba errado**, lo reconocí explícitamente

---

### Pendientes técnicos priorizados (próxima sesión)

1. **Bug WAIT_SLOT "para hoy" sin avisar** — caso María. Cuando paciente pide hora hoy y no hay slots, debe decir "no hay slots para hoy, te muestro mañana" en vez de mostrar mañana en silencio. Es un fix en `_iniciar_agendar` o donde se llama `buscar_primer_dia` con preferencia de fecha.
2. **Normalizar capitalización en evento `sin_disponibilidad`** — typo "Cardiología" vs "cardiología" duplicaba conteos. `flows.py` líneas 2999 y 6457: `"especialidad": especialidad.lower()`.
3. **Auditar bucket "intent: otro"** (94/7d) y agregar al cache de `claude_helper.py` los patterns recurrentes (saludos, agradecimientos cortos).
4. **Reserva tentativa de slot por 30s** — para reducir race condition Dr. Márquez/Olavarría (5 casos de "slot ya ocupado" últimos 7d).
5. **Flujo de reimpresión de boletas** — caso real detectado, ortodoncia, requiere endpoint Medilink. Backlog.
6. **Test mock `fake_listar_citas_paciente` desactualizado** — no acepta kwarg `rut`. `tests/harness_50.py` falla en línea 4641 (no afecta producción).
7. **Validación pre-deploy más estricta** — el `python3 ast.parse()` no detectó `NameError: name 're'`. Mejor: `python3 -c "import sys; sys.path.insert(0,'app'); import messaging; import flows; import claude_helper"`.

### Pendientes no técnicos
- Decisión de marca dental (Olamar / Oris / Austra) → Dr. decide
- Validar disponibilidad dominios + INAPI antes de cualquier inversión en marca dental
- Revisar Meta Ads Manager: por qué spend del 27-abr fue solo $4.930 (vs ~$200K baseline)
- Conversación honesta sobre Meulen (¿es viable independiente del subsidio del CMC?)

---


- Bot en `+56 9 6661 0737` — status `CONNECTED` · quality `GREEN`
- Display Name "Centro Médico Carampangue" en `PENDING_REVIEW`
- **Payment method activo** (USD 20 cargados 2026-04-18) — desbloquea templates MARKETING sin restricción del free tier
- 14 templates APPROVED (9 UTILITY + 5 MARKETING): recordatorio_cita, recordatorio_cita_2h, postconsulta_seguimiento, lista_espera_cupo, informe_listo, seguimiento_medico, reactivacion_paciente, crosssell_kine, control_especialidad, adherencia_kine, sistema_recuperado, más administrativos

**Resumen (2026-04-18 PM)** — Panel Recepción v2 + anti-spam 429 Medilink:

*Panel v2 (`/admin/v2`, no reemplaza v1)*:
- Rediseño chat-first en `templates/admin_v2.html` (~1200 líneas). Layout 3 cols: bandeja / chat / contexto. Reutiliza endpoints v1 (conversations, takeover, reply, resume, unread-counts, mark-seen, notes, tags, profile, patient-context, patient-files, file, send-document). No agrega endpoints backend
- Paleta institucional CMC (Manual de Marca): aqua `#4FBECE`, azul `#1172AB`, navy `#0F3F68`. Tipografía Montserrat. Logo = isotipo recortado del PNG (`/static/isotipo.png`, 150×150, bbox auto-detectado con Pillow)
- Estética pro: sistema de sombras con tinte navy (xs/sm/md/lg), iconos SVG lucide-style, burbujas con tail en primer msg de cada grupo, empty state institucional con isotipo + kbd de shortcuts, scrollbars on-hover
- Timezone correcto: `parseServerTs()` trata timestamps server como UTC y renderiza en `America/Santiago` (helpers `fmtTime`/`fmtDay`/`fmtClock`). Antes hardcodeaba `-04:00` y rompía con DST
- Responsive mobile completo: <760px pasa a `display:flex column` (salir del grid evita columnas implícitas de `grid-template-areas`), safe-area inset para iOS, KPIs ocultos, btn-actions con icon-only, contexto overlay 100vw
- Auth: misma que `/admin` (token query o cookie HMAC)

*Anti-spam 429 Medilink*:
- Síntoma: mensaje "✅ Medilink recuperado" al admin cada minuto cuando la API oscilaba (429 intermitente)
- `resilience.should_notify_recovery()`: throttle 30 min + flap protection (ignora caídas <3 min)
- `resilience.mark_medilink_down()` ahora idempotente (no resetea `_KEY_MEDILINK_DOWN_AT` si ya estaba down) para medir duración real
- `jobs._job_medilink_watchdog` envuelve notifs (pacientes + admin) con el guard; `mark_medilink_up()` sigue ejecutando siempre para `/health`
- **Root cause matado**: `/admin/api/agenda-dia` desactivado — retorna `{profesionales: [], disabled: true}` sin consultar Medilink. Ese endpoint hacía fan-out de ~20 requests paralelos (uno por profesional) que saturaban rate limit. La recepción ya ve la agenda directamente en Medilink — era redundante. Implementación original preservada como `_admin_agenda_dia_DISABLED`

*Otros fixes*:
- `Dra. Javiera Burgos` intervalo 30→60 min en `PROFESIONALES`
- Isotipo recortado con script Pillow inline (bbox automático del logo horizontal 655×171 → cuadrado 150×150)

Commits: `2b45259` (v2 inicial), `2916c31` (fix filtro No leídas), `46b1bdf` (sección archivos), `fb9d9f1` (logo topbar), `a90379c` (logo cruz SVG), `b1933c9` (paleta CMC + tz + Javiera 60min), `487a281` (logo via CSS), `42b9b0d` (isotipo.png), `23ed4f4` (pulido estético), `029b6dd`+`e1c3a04` (responsive mobile), `19e3f3f` (anti-spam), `cfe53c6` (desactivar agenda-dia)

---

**Resumen (2026-04-18)** — UX + fixes basados en conversaciones reales:
- Modo chat-focus pantalla completa (botón flotante ⛶)
- Quick replies colapsables (+60px chat), chat-header compacto, takeover-banner fino
- Marcado visto (badges rojos + separador "↓ Mensajes nuevos ↓")
- Terceros sin fricción: RUT directo, no pide nombre del dueño del celular
- Fuzzy typos rurales (biene/horits/pars/medico geberal → correct)
- "Para mañana" = día siguiente + filtro estricto por fecha
- "médico familiar", "médico" aislado, "médico para hoy" → detectados
- Bono Fonasa MLE SE VENDE en el CMC (con huella), Matrona es preferencial (no MLE)
- HUMAN_TAKEOVER preservado con saludos y cuando recepcionista activa <10min
- Dedupe "Recibí tu imagen" en ráfagas <60s
- Audios largos en WAIT_RUT_* → humano automático con contexto
- Atajo "¿Se confirma mi hora para hoy?" → consulta Medilink directo
- "Marcar agendado manual" en panel (cita por teléfono/presencial)
- Fix crítico: `datetime` faltante hacía que imágenes se perdieran silenciosamente
- Staff Javiera Burgos agregada como profesional

**Resumen (2026-04-17)** — Friction Killer + seguridad:

**Resumen (2026-04-17)** — Friction Killer + seguridad:
- Fixes técnicos: `is_duplicate` atómico (INSERT OR IGNORE), índices `citas_bot(esp/phone)` + `demanda(phone)` + `events(event, ts)`, rate limit multi-clave (phone + `rut:{rut}`)
- Quick-book (`WAIT_QUICK_BOOK`): paciente conocido agenda como la última vez en 1 toque — reduce 4-6 pasos a 2
- Botón primer slot ahora "⚡ — Primero disp." (antes "⭐ recomendado")
- Reagendar 1-click tras cancelación doctor: endpoint `POST /admin/api/cita/{id}/cancel-doctor` pre-carga 3 alternativas en sesión paciente → WAIT_SLOT
- IG/FB celular opcional en registro (prompt suavizado)
- Conversion funnel por especialidad: pill 📊 conv + modal en topbar admin, endpoint `GET /admin/api/conversion-funnel`
- Fix bug preexistente: `_iniciar_ver_reservas` → `_iniciar_ver` (2 referencias)
- Tests: 100/100 harness + 200/200 stress + 52/52 normalizer

**TODOs documentados**:
- Detección automática de cancelaciones en Medilink (polling `GET /citas/{id}` + cron 30min) — agente dejó plan
- Botón "🔄 Reagendar cancelado-doctor" en tabla citas del panel admin HTML (endpoint ya existe)

**Resumen (2026-04-16)**:
- Ley 19.628 compliance: opt-in explícito, derecho al olvido (cascade 18 tablas), política `/privacidad`
- Registro paciente nuevo en 1 mensaje (WAIT_DATOS_NUEVO): nombre+sexo+fecha, IG/FB pide celular
- Reenganche agresivo con slot real + urgencia + botones interactivos
- Fidelización prescriptiva ("necesitas X" en vez de "¿te gustaría X?")
- Waitlist event-driven: notifica al cancelar (además del cron 07:00)
- Triage urgencia: mensaje empático antes de agendar
- Cumpleaños con botones interactivos + cross-sell/win-back prescriptivo
- SQLCipher para heatmap_cache.db + backup semanal encriptado
- Tests: 100/100 harness + 200/200 stress + 52/52 normalizer + 34/34 foros

**Sprint completado (19/19 del plan 2026-04-10)**: todos DONE excepto #12 (descartado: copago requiere huella) y #16 (pendiente: migración número WA).

**Estado servidor**: chatbot en `https://agentecmc.cl` (systemd). GES en `localhost:8002`. API GES en `https://api-ges.agentecmc.cl`. Frontend GES en `https://ges.agentecmc.cl`.

**Pendiente corto plazo**:
- SQLCipher sessions.db en VPS (playbook listo)
- ~~Migración número WhatsApp~~ ✅ bot ya corre en +56966610737 (Cloud API)
- Rotación PAT → SSH keys
- Recolección diferida de datos (comuna/email 2h antes de cita) — diseñado, no implementado

---

## Manejo de errores ortográficos (WhatsApp rural Arauco)

Los pacientes escriben con abreviaciones, sin tildes, con participios coloquiales (`sangrao`, `hinchao`), palabras pegadas y errores frecuentes. Estrategia en capas:

### Fase 1 — Normalización léxica (✅ DONE 2026-04-10)
`app/triage_ges.py::normalizar_texto_paciente()` aplica antes de enviar al motor GES:
- minúscula + sin tildes (preservando ñ)
- colapsa espacios
- diccionario `_ABREVIACIONES` (`q→que, xq→porque, dlr→dolor, kbza→cabeza, tngo→tengo, stoy→estoy, muxo→mucho, ke→que, kiero→quiero, ...`)
- diccionario `_TYPOS` (`feber→fiebre, diarea→diarrea, bomito→vomito, ...`)
- regex participios rurales: `\b([a-z]{3,})ao\b → \1ado` (`sangrao→sangrado, hinchao→hinchado`)
- **Limitación**: solo cubre lo que está en el diccionario. Ampliar con `triage_ges_nomatch` de producción.

### Fase 2 — Fuzzy matching en backend GES (pendiente)
Cambiar el matcher de substring a `rapidfuzz.token_set_ratio` con umbral ≥85. Captura errores de 1-2 letras que no están en el diccionario. **Costo**: 1-2 h en `ges-clinical-app/app/services/triage.py`. **Beneficio**: cubre el long tail de typos imposibles de diccionarizar.

### Fase 3 — Normalización con Claude Haiku (pendiente, solo si F1+F2 no alcanzan)
Agregar en `claude_helper.py::normalizar_sintomas(texto)`: Claude devuelve la versión canónica, se la pasamos a GES. **Costo**: +1 llamada Haiku por mensaje (~300 ms, ~$0.0001). **Beneficio**: captura regionalismos, frases incompletas y slang. **Descartar si**: la latencia p95 ya está al límite.

### Fase 4 — Embeddings semánticos (pendiente, roadmap largo)
Reemplazar substring matching por similitud coseno con embeddings multilingües (e.g. `intfloat/multilingual-e5`). `"dlr de kbza"` ≈ `"dolor de cabeza"` aunque el string sea distinto. **Costo**: refactor grande del backend GES (vector store, búsqueda ANN, warmup). **Beneficio**: máximo recall sin mantener diccionarios manualmente.

### Observabilidad
- Log `triage_ges_match` con `top`, `score`, `especialidad`, `urgency`, `elapsed_ms` (para p95)
- Log `triage_ges_nomatch` con `texto[:240]` cuando `_SENALES_SINTOMA` matchea pero GES retorna None — corpus de gaps para revisar semanalmente
- Heurística `_SENALES_SINTOMA` en `flows.py`: `me duele|dolor|molest|siento|fiebre|tos|flema|diarrea|vomit|sangr|hincha|no puedo|hace X que|desde hace|tengo un...`
- **Revisión recomendada**: cada lunes filtrar `conversation_events` por `event='triage_ges_nomatch'` de la semana anterior y ampliar `_ABREVIACIONES`/`_TYPOS` en base a los patrones recurrentes.

---

## Deuda técnica pendiente
1. ~~**Partir `main.py`**~~ — ✅ DONE (3,045 → 468 líneas, sesión 2026-04-12)
2. ~~**Mover HTML del panel a template externo**~~ — ✅ DONE (templates/admin.html)
3. **Auth real del panel** — token embebido en el HTML es visible en DOM; migrar a cookie httpOnly firmada + login
4. **Suite `pytest`** — cubrir `valid_rut`, `smart_select`, transiciones core de `flows.py`
5. Precios en `claude_helper.py` hardcodeados en SYSTEM_PROMPT — actualizar manualmente cuando cambien
6. Dr. Luis Armijo (ID 77) aparece como Medicina General en Medilink pero es Kinesiólogo — error de datos en Medilink, no en el bot
7. SQLite no escala bien con concurrencia alta — migrar a PostgreSQL o Redis si hay múltiples sucursales
8. Verificar IDs de profesionales menos frecuentes (Millán, Barraza, Rejón, etc.) directamente en API
