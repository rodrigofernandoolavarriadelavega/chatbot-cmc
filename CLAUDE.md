# CLAUDE.md — Chatbot WhatsApp Centro Médico Carampangue (CMC)

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
| 55 | Dra. Javiera Burgos | Odontología General | 30 |
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
- [x] Fix ecografías: ginecológica/obstétrica → Ginecología (Dr. Tirso Rejón), no Ecografía (David Pardo)
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

## Sesión en curso
**Fecha**: 2026-04-18
**Historial completo**: ver claude-mem timeline o git log

**Estado Meta/WhatsApp**:
- Bot en `+56 9 6661 0737` — status `CONNECTED` · quality `GREEN`
- Display Name "Centro Médico Carampangue" en `PENDING_REVIEW`
- **Payment method activo** (USD 20 cargados 2026-04-18) — desbloquea templates MARKETING sin restricción del free tier
- 14 templates APPROVED (9 UTILITY + 5 MARKETING): recordatorio_cita, recordatorio_cita_2h, postconsulta_seguimiento, lista_espera_cupo, informe_listo, seguimiento_medico, reactivacion_paciente, crosssell_kine, control_especialidad, adherencia_kine, sistema_recuperado, más administrativos

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
