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
  → agendar       → WAIT_ESPECIALIDAD → WAIT_SLOT → WAIT_RUT_AGENDAR
                                                   → WAIT_NOMBRE_NUEVO (paciente nuevo)
                                                   → WAIT_FECHA_NAC → WAIT_SEXO → WAIT_COMUNA → WAIT_EMAIL
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
- Número prepago CMC: +56945886628 (Display Name APROBADO ✅)
- **NO conectar** al +56966610737 (WhatsApp activo de secretarias del CMC)

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
- [x] Aprobación Display Name número prepago (+56945886628) ✅
- [x] Fidelización completa: post-consulta · reactivación · adherencia kine · control por especialidad · cross-sell kine
- [x] Clasificación de respuesta libre al seguimiento (texto libre → mejor/igual/peor via Claude)
- [x] Panel admin: etiquetas de especialidad legibles, tiempo de espera en formato humano
- [x] Panel "Pacientes en Control": seguimiento de sesiones recurrentes (kine, ortodoncia, psicología, nutrición)
- [x] Instagram y Facebook Messenger: webhook unificado, respuesta desde panel admin
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

## Dashboard admin
- Ruta: `http://157.245.13.107:8001/admin?token=cmc_admin_2026`
- Incluido en el mismo proceso del bot (no es proyecto separado)
- Muestra métricas, conversaciones activas y estado del sistema

## Sesión en curso
**Fecha**: 2026-04-14

**Hecho (sesión 2026-04-14 — alertas doctor + patologías crónicas + systemd + fixes)**:
- **PNI pediátrico**: recordatorio de vacunas del Programa Nacional de Inmunización al confirmar cita pediátrica. Calendario completo (RN→8°Básico), vacunas escolares con mensaje condicional. Módulo `app/pni.py`.
- **Tips autocuidado post-consulta**: módulo `app/autocuidado.py` con tips personalizados por edad/sexo/especialidad + exámenes preventivos. Se envía como segundo mensaje tras el seguimiento post-consulta.
- **Descripciones de procedimientos**: cada procedimiento del glosario de precios en `claude_helper.py` ahora tiene descripción para el paciente (qué es, si duele, cuánto dura).
- **Fix ecografías**: ginecológica/obstétrica redirigida a Ginecología (Dr. Tirso Rejón), no a Ecografía (David Pardo).
- **Indicador de pensando**: reacción ⏳ en WhatsApp mientras el bot procesa, se quita al responder.
- **Alertas personales Dr. Olavarría** (`app/doctor_alerts.py`): resumen del paciente 10 min antes de cada cita (nombre, RUT, edad, sexo + exámenes preventivos por edad/sexo + vacunas PNI específicas + guías clínicas por patología crónica). Reportes de progreso a las 09:00, 12:00, 16:00, 20:00 (agendados/atendidos/pendientes + próximos 3). `app/medilink.py::obtener_agenda_dia()` consulta citas + datos del paciente.
- **Detección pasiva de patologías crónicas**: 10 patologías (DM2, HTA, asma, EPOC, hipotiroidismo, dislipidemia, depresión, epilepsia, artrosis, IRC) detectadas por keywords en conversación → tag `dx:*` automático.
- **Guías clínicas por patología**: cada patología tiene examen físico, exámenes a pedir, metas terapéuticas y recomendaciones. Se muestran en el resumen pre-cita del doctor.
- **Comando `dx` por WhatsApp**: el doctor puede escribir `dx RUT dm2 hta` para registrar diagnósticos, `dx RUT` para ver, `dxborrar RUT dm2` para eliminar. Solo funciona desde su número. 24 patologías válidas.
- **Screening por edad**: siempre visible en resumen pre-cita (glicemia, PA, IMC según rango etario).
- **Systemd service** (`chatbot-cmc.service`): Restart=always, RestartSec=3s, arranque al boot. Deploy limpio con `systemctl restart chatbot-cmc`. Ya no hay problemas de setsid/nohup/SSH.
- **Fix timezone CronTrigger**: todos los jobs cron ahora usan `timezone="America/Santiago"`. Antes corrían en UTC del servidor (post-consulta llegaba a las 06:00 AM en vez de 10:00 AM).
- **Fix mensajes fidelización en panel**: `log_message()` agregado en los 5 flujos de fidelización + 2 recordatorios. Ahora todos los envíos automáticos del bot aparecen en el chat del panel admin.
- **`get_phone_by_rut()`**: búsqueda inversa RUT→teléfono en `session.py` para vincular tags del paciente con su historial de conversación.
- Commits: `d4ef903`→`58ea618`. Deployados todos. Tests 90/90.

**Hecho (sesión 2026-04-13 — WA Business features + registro expandido + stress tests)**:
- **WhatsApp Business features**: message status webhooks (delivered/read/failed), tabla `message_statuses` en SQLite, BSUID capture para migración junio 2026, quality rating endpoint, delivery status icons en panel admin.
- **Envío de documentos desde panel admin**: `POST /admin/api/send-document` (upload + send PDF/imagen), botón 📎 en chat del panel.
- **Templates Meta aprobados**: `informe_listo` (notificar informe listo) y `seguimiento_medico` (seguimiento médico personalizado). Botones en panel admin. Endpoint `POST /admin/api/send-template`.
- **Cross-sell ORL↔Fonoaudiología**: dict `CROSS_REFERENCE` en flows.py con prestaciones reales y precios de Fono (audiometría $25k, impedanciometría $20k, VPPB $50k, etc.) y mención de ORL desde Fono. Se agrega al confirmar cita.
- **Retry limit WAIT_CITA_CANCELAR/REAGENDAR**: 3 intentos → escalación a HUMAN_TAKEOVER (previene loops infinitos).
- **Filtro texto vacío**: whitespace-only messages retornan 200 sin procesar.
- **Stress test 200 casos** (`harness_stress_200.py`): 13 bloques cubriendo todas las especialidades, profesionales, variantes coloquiales, FAQ, flujos completos, emergencias, edge cases, masoterapia, cross-sell, fidelización.
- **Registro expandido paciente nuevo**: 5 estados nuevos (WAIT_FECHA_NAC → WAIT_SEXO → WAIT_COMUNA → WAIT_EMAIL). Parser robusto de fecha (DD/MM/YYYY, DD-MM-YYYY, DD/MM/YY, 8 dígitos pegados, "15 de marzo de 1990", mes abreviado). Todo saltable. Email no bloquea si es inválido. Celular auto-relleno desde WhatsApp. Abandonment tracking via log_event.
- **`crear_paciente()` expandido**: acepta `**kwargs` (fecha_nacimiento, sexo, celular, email, comuna, direccion, ciudad) → envía todos los campos a Medilink.
- **Tests**: 90/90 harness_50 + 200/200 stress = 290/290 ✅. Commit `943d2e9` deployado.

**Hecho (sesión 2026-04-13 — 11 mejoras panel admin + fix celular)**:
- **Notas persistentes**: tabla `contact_notes` en SQLite, endpoints GET/PUT `/admin/api/notes/{phone}`, autosave con debounce 1s. Eliminado `localNotes` en memoria.
- **Notificaciones mensajes nuevos**: beep via Web Audio API (880Hz, 0.15s) + Browser Notification al detectar mensaje entrante. Toggle mute con estado en localStorage.
- **Pulse animation**: botón "Tomar control" pulsa cuando hay mensajes sin responder. CSS `@keyframes pulse-glow`.
- **Atajos teclado**: `Ctrl+K`/`Cmd+K` → búsqueda global, `Escape` → cierra modal/overlay. Hint `<kbd>` en botón buscar.
- **Dropdown "Seguimiento"**: Pacientes en Control + Fidelización agrupados. Cierre automático al hacer click fuera.
- **Tablet responsive (768-1024px)**: panel de contexto como overlay (mismo mecanismo que mobile), botón ℹ️ visible.
- **Matrix table mobile**: `min-width: max-content` + `-webkit-overflow-scrolling: touch`.
- **Pill conversión**: `📈 X% (citas/intentos)` usando datos de `/admin/api/metrics`.
- **Pill registros**: `📝 X% (completados/total)` con nuevo endpoint `/admin/api/registration-stats`.
- **Contexto enriquecido**: sección "Historial" con última cita, total citas por bot, lista de espera activa. Endpoint `/admin/api/patient-context/{phone}`. Checklist de progreso expandido con pasos de registro.
- **Badges canal**: `WA`/`IG`/`FB` como badges coloreados en vez de emojis.
- **7 estados faltantes** agregados a STATE_LABELS/ACTIVE_STATES/STATE_GROUPS (registro, reagendar, masoterapia).
- **Grupo "Reagendando"** (cyan) en filtros de estado.
- **Fix `authHeaders()`**: reemplazado por `apiUrl()` en `seguimientoMedico()`.
- **Fix celular registro**: formato sin `+` (ej: `56912345678`), enviado en ambos campos `celular` y `telefono` a Medilink.
- Commit `f7e3d67` deployado. `/health` → 200.

**Hecho (sesión 2026-04-12 — features + fixes + deploy Whisper + docs sync)**:
- **Pill "Confirman mañana"** en topbar admin: CSS `.pill.green`, modal con detalle por paciente, JS con refresh 60s. Endpoint `/admin/api/confirmaciones?fecha=YYYY-MM-DD`. Commit `c483c78`.
- **Fix "quiero tapadura"**: glosario en `claude_helper.py` ahora fuerza `intent=info` para CUALQUIER mención de tratamiento del glosario (no solo preguntas "¿qué es...?"). Excepción: nombres de especialidad/profesional → siguen siendo `agendar`. Commit `c98bc93`.
- **Glosario dental expandido**: tapadura caída, dientes chuecos/torcidos, implante dental, encías sangrantes, dientes amarillos, duraciones y anestesia en descripciones.
- **Glosario estética expandido** (3→9 tratamientos): hilos tensores ($129.990), lipopapada ($139.990), exosomas ($349.900), bioestimuladores/hidroxiapatita ($450.000), armonización facial (eval $15.000), peeling + sinónimos. Commit `38291f9`.
- **Suite `test_foros_dental_estetica.py`**: 34 casos (18 dental + 16 estética) con frases reales de foros de salud chilenos. Verifica intent=info con especialidad y respuesta_directa.
- **Whisper deployado**: `OPENAI_API_KEY` agregada al `.env` del VPS. Bot transcribe audios en producción.
- **Audio feedback**: bot responde "Entendí: _{texto}_" antes de procesar el mensaje. Commit `e892fdb`.
- **Retención indefinida**: cron `purge_old_data` desactivado. ~90 MB/año, >100 años de capacidad. Commit `cbec378`.
- **Fix bug emergencias**: `reset_session(phone)` en handlers de emergencia. Commit `cdba1a8`.
- **Refactor main.py (3,045 → 468 líneas)**: extraídos `messaging.py`, `jobs.py`, `admin_routes.py`, `templates/admin.html`. Commit `6be5ae8`.
- **Custom domain `ges.agentecmc.cl` activo**: CNAME Cloudflare + Vercel.
- **Docs sync**: CLAUDE.md, memory files y 5 páginas Notion actualizadas con todo lo hecho en esta sesión.

**Hecho (sesión 2026-04-10/11 — git GES + swap + backup cron)**:
- **Repo privado `ges-clinical-app` creado en GitHub**: https://github.com/rodrigofernandoolavarriadelavega/ges-clinical-app
  - Monorepo con `backend/` (FastAPI) y `frontend/` (Next.js 14) en la raíz
  - `.gitignore` completo cubriendo venv, node_modules, .next, `*.db`, `.env*`, `.DS_Store`
  - Commit inicial `56983b0` con 77 archivos, cero secretos
  - `backend/data/ges.db` gitignoreada (se regenera con `scripts/seed*.py`)
  - Repo creado vía `POST /user/repos` de GitHub API, reutilizando el PAT que estaba embedded en el remote de `chatbot-cmc` por consistencia
  - **⚠️ Deuda**: el PAT queda en plaintext en el `.git/config` del Mac y del VPS (`/opt/chatbot-cmc/`). Plan acordado: rotar → SSH keys → actualizar remotes en **ambos** lados (si se rota sin tocar el VPS, el próximo `git pull` en deploy falla).
- **Swapfile 2 GB activo en el VPS**:
  - `fallocate -l 2G /swapfile`, `chmod 600`, `mkswap`, `swapon`, entrada en `/etc/fstab`
  - `vm.swappiness=10` (default era 60) persistida en `/etc/sysctl.d/99-swappiness.conf` — solo swapea en emergencia, mantiene RAM caliente
  - Estado post: `961 MB RAM + 2.0 GB swap`. El GES Assistant sigue activo, sin downtime
  - Esto desbloquea el deploy del frontend Next.js sin riesgo de OOM
- **Cron de backup semanal del `ges.db`**:
  - `sqlite3` CLI instalado en el VPS vía apt
  - Script `/usr/local/bin/backup-ges-db.sh` usa `sqlite3 .backup` (online, seguro con el service corriendo) + gzip + retención de los últimos 8
  - Cron en `/etc/cron.d/ges-assistant-backup` → **domingo 03:30 UTC** (00:30 hora Chile), ventana de baja actividad
  - Destino: `/opt/backups/ges-assistant/ges_YYYYMMDD_HHMMSS.db.gz`
  - Logs en `/var/log/ges-backup.log`
  - Backup de prueba ejecutado: **412 KB comprimido** (DB cruda 1.1 MB)
- **Docs sincronizados**: `docs/infra.md` agregó secciones de swap, backup y repo GitHub + plan de rotación de PAT. Memoria `vps_access.md` actualizada. Página Notion "8. Infraestructura VPS + GES Assistant" bajo "Documentación Técnica — Chatbot WhatsApp CMC".

**Hecho (sesión 2026-04-11 — Phase 2: frontend GES en Vercel + API pública)**:
- **Frontend Next.js deployado en Vercel**: `https://ges-clinical-app.vercel.app` (Production, build 45s, auto-deploy on push to `main`)
  - Proyecto Vercel `ges-clinical-app` conectado al repo GitHub `ges-clinical-app`
  - Root Directory = `frontend`, Framework Preset = Next.js
  - Env var `NEXT_PUBLIC_API_URL=https://api-ges.agentecmc.cl` en Vercel (build-time, se embebe en el bundle)
  - `vercel.json` simplificado: se eliminó referencia rota a Vercel Secret `@ges-api-url`
  - **Bug encontrado**: trailing space en la env var causaba `ERR_NAME_NOT_RESOLVED` (`api-ges.agentecmc.cl%20`). Detectado via DevTools Console. Fix: quitar espacio en Vercel UI + redeploy.
- **nginx subdomain `api-ges.agentecmc.cl`** activo con SSL:
  - Proxy selectivo a `127.0.0.1:8002` (GES backend systemd)
  - **Whitelist de endpoints**: `/health`, `/auth/*`, `/pathologies`, `/clinical/*`, `/symptoms`, `/calculators/*`, `/validation/*`
  - **`/triage` bloqueado** (404) — solo accesible desde localhost por el chatbot
  - Let's Encrypt cert via certbot, auto-renew, HTTP→HTTPS redirect
  - Headers de seguridad: `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`
  - Snippet compartido `/etc/nginx/snippets/ges-proxy-headers.conf` para headers de proxy
  - DNS: registro A `api-ges` → `157.245.13.107` en Cloudflare (DNS only, sin proxy)
- **CORS actualizado** en `/opt/ges-assistant/.env`: `CORS_ORIGINS` incluye `https://ges.agentecmc.cl,https://ges-clinical-app.vercel.app` + localhost dev
- **`GES_SECRET_KEY`** seteado a 64-char hex random en producción (JWT HS256, 12h expiry)
- **Verificación end-to-end**: `/health` → 200, `/pathologies` → lista GES, `/triage` → 404 (bloqueado). CORS preflight OK desde origen Vercel.
- Prototipos HTML del sitio web agrupados en `sitio-web/v2.html` y `sitio-web/v3.html`

**Hecho (sesión 2026-04-10 — expansión normalizador triage + deploy fix)**:
- **Diccionario de normalización expandido en `app/triage_ges.py`**: ~100 entradas nuevas repartidas entre `_ABREVIACIONES` y `_TYPOS`. Categorías:
  - Abreviaciones WhatsApp adicionales (`tngs`, `snto`, `hrs`, `dr`, `dra`, `mjer`, `hno`, `kmo`, `mnna`, etc.)
  - Modismos rurales chilenos confirmados en foros de salud: `guata`/`guatita`/`wata`→`estomago`, `cototo`→`hinchazon`, `empacho`→`indigestion`, `rasquiña`/`comeson`/`picason`→`picazon`, `escozor`/`escosor`→`ardor`
  - Typos frecuentes de partes del cuerpo (`cabesa`, `gargnta`, `stomago`, `rodia`, `peccho`, `naris`, etc.) y enfermedades (`gripa`, `pulmona`, `bronkitis`, `astma`, `preson`, `diabetis`, `alerjia`, `hemoragia`, `convulcion`, `infrto`, `inchao`, `inflamao`)
  - Regla aplicada: solo entradas que NO colisionan con palabras válidas del español (ej. se descartó `bota` por ambigüedad).
- **Suite unitaria nueva `tests/test_normalizer.py`**: 52 casos cubriendo tildes/mayúsculas, abreviaciones, typos, participios rurales (`-ao→-ado`), modismos CL, edge cases (empty, solo puntuación, números), preservación de IDs de botón (`cat_medico`, `cita_confirm:9001`) y no-colisión con español estándar. Corre standalone: `PYTHONPATH=app:. venv/bin/python tests/test_normalizer.py`. **52/52 ✅**
- **Harness principal intacto**: `harness_50.py` sigue en **68/68 ✅** post-cambios.
- Commits `27a9651`, `50efa8c`, `68ce043` pusheados y **deployados a prod**.
- **Fix de deploy procedure descubierto al vuelo**: el comando documentado (`nohup ... &` dentro de `ssh "..."`) deja el uvicorn asociado al pty remoto y muere al cerrarse la sesión SSH. Se corrigió a `setsid nohup ... & disown`. Docs y memoria actualizados:
  - `CLAUDE.md` → sección "Deploy en producción" reescrita con Opción A (one-liner desde Mac) y Opción B (sesión interactiva), más la advertencia sobre `setsid`.
  - `docs/infra.md` → mismo fix + warning.
  - `~/.claude/.../memory/vps_access.md` → gotcha documentado.
- **Verificación post-deploy**: `https://agentecmc.cl/health` → HTTP 200. Uvicorn PID activo. GES Assistant (systemd) intacto.

**Hecho (sesión 2026-04-10 — deploy GES Assistant + SSH hardening)**:
- **Fase 1 del GES Clinical Assistant deployada en producción**: servicio `ges-assistant.service` (systemd, auto-restart, arranque al boot) en `/opt/ges-assistant`, bindeado a `127.0.0.1:8002`, consumo ~70 MB RAM. El chatbot ya lo consume vía `GES_ASSISTANT_URL=http://localhost:8002` en `/opt/chatbot-cmc/.env`.
- **Motor de triage validado end-to-end en prod** con 8 casos: "dolor pecho al apretar" → Osteocondritis/Traumatología sin urgencia, "opresivo al caminar" → IAM/URGENCIAS, "tngo muxo dlr d kbza" (normalización rural) → Cefalea tensional, meningitis → URGENCIAS, etc.
- **Backend GES rsync** desde `/Users/rodrigoolavarria/ges-clinical-app/backend/` al VPS con venv dedicado. El repo local NO es git todavía (pendiente crear repo privado en GitHub).
- **SSH hardening completo** en el VPS (venía con password auth habilitado):
  - Ed25519 generada en Mac local (`~/.ssh/id_ed25519`, sin passphrase).
  - `ssh-copy-id` al VPS, key auth verificada.
  - `/etc/ssh/sshd_config.d/50-cloud-init.conf` → `PasswordAuthentication no`.
  - Contraseña root rotada a 24 chars random (openssl). Guardada en password manager del usuario.
  - Backup `sshd_config.bak.2026-04-10` por si acaso.
  - Verificado: password auth devuelve `Permission denied (publickey)`, key sigue funcionando.
- **Docs nuevos**: `docs/infra.md` con todo el detalle de VPS, servicios, redeploy procedures, memoria. Memoria persistente en `~/.claude/projects/.../memory/vps_access.md` como reference.
- **Fase 2 pospuesta** (frontend GES en Vercel + `ges.agentecmc.cl`): DNS en Cloudflare (subdomain trivial), repo privado pendiente, API key + CORS restrictivo para endpoints expuestos, `/triage` permanece localhost.

**Hecho (sesión 2026-04-10 — reagendar + lista de espera)**:
- **Reagendar en un paso** (menú opción 2): intent dedicado `reagendar` en Claude Haiku. Nuevos estados `WAIT_RUT_REAGENDAR` y `WAIT_CITA_REAGENDAR`. `_iniciar_reagendar` usa `contact_profiles` si existe (salta el paso del RUT). `CONFIRMING_CITA` crea la nueva cita PRIMERO y solo entonces cancela la vieja (si el rollback falla, se loggea `reagendar_cancel_old_fail` pero el paciente NUNCA queda sin cita).
- **Lista de espera** (menú opción 5 + intent `waitlist` + oferta automática): nueva tabla `waitlist` en `session.py` con upsert por `phone+especialidad` (índice parcial sobre `notified_at IS NULL AND canceled_at IS NULL`). Tres entradas: (1) oferta automática en `_iniciar_agendar` cuando `buscar_primer_dia` no encuentra cupo —capturando `id_prof_pref` cuando la especialidad resuelve a un único doctor, ej. "olavarria" → 1—, (2) opción 5 del menú (pide especialidad si no viene), (3) intent `waitlist` del LLM.
- **Cron `_job_waitlist_check` 07:00 CLT** en `main.py`: salta si Medilink está caído; recorre inscripciones FIFO; busca cupo a 14 días con `solo_ids=[id_prof_pref]` si aplica; notifica por WhatsApp con primer slot disponible y marca `notified_at`. Evento `waitlist_notificado` persiste en `conversation_events`.
- **Panel admin**: endpoints `GET /admin/api/waitlist` y `POST /admin/api/waitlist/{id}/cancel`. `/health` expone `waitlist_depth`.
- **`listar_citas_paciente` enriquecido** con `id_profesional` y `especialidad` (derivados de `PROFESIONALES`) → necesarios para que reagendar pueda reusar la especialidad sin preguntar.
- **Menú ampliado a 6 opciones**: Agendar / Reagendar / Cancelar / Ver / Lista espera / Recepción. Atajos numéricos 1..6. Fidelización buttons reaccomodados (id "1" → "2" para "Sí, reagendar") para no colisionar con la nueva opción del menú.
- Commit `e619125` deployado. Scheduler verificado con 11 jobs incluyendo `waitlist_check` y `medilink_watchdog`. `/health` reporta `waitlist_depth: 0` en verde.

**Hecho (sesión 2026-04-10 — modo degradado + UX FAQ-to-agendar)**:
- **Modo degradado Medilink**: tabla `intent_queue` + `resilience.py` (`is_medilink_down`, `mark_medilink_down/up`, throttle de alertas a recepción). Cuando el bot detecta fallo cascada, encola la intención del paciente, le responde "sistema temporalmente fuera de servicio, te escribo apenas vuelva" y dispara alerta a `ADMIN_ALERT_PHONE`. Cron `_job_medilink_watchdog` cada 1 min: si down, intenta `/sucursales`; al recuperar, avisa a cada paciente encolado y confirma a recepción.
- **UX FAQ-to-agendar**: cuando el usuario pregunta por un tratamiento (ej. "¿qué es una tapadura?"), después de la respuesta el bot pre-busca el slot más próximo y ofrece botón "✅ Sí, agendar" con horario inline. `IDLE` handler prioritario antes de atajos numéricos captura `especialidad_sugerida` en `data` para que "1"/"sí"/tap al botón rutee directo a `_iniciar_agendar(..., esp_sug_prev)`.

**Hecho (sesión 2026-04-10 — glosario FAQ + Fase 1 multimodal)**:
- **Panel conversaciones**: fix ordenamiento `get_conversations` usa `MAX(m.ts, s.updated_at)` para que FAQs en `WAIT_SLOT` (sin `save_session`) sigan burbujeando arriba; `save_session` forzado en el handler FAQ de `WAIT_SLOT`
- **Panel chat**: orden cronológico estilo WhatsApp (antiguo arriba → nuevo abajo con auto-scroll sticky-bottom, tolerancia 120px)
- **Scheduler fix**: `RuntimeError: no running event loop` resuelto — jobs pasados directamente como corutinas a `AsyncIOScheduler.add_job` (sin `lambda: asyncio.create_task(...)`); creados wrappers `_job_*` para los que requieren `send_whatsapp`
- **Glosario clínico FAQ** en `claude_helper.SYSTEM_PROMPT` (~110 términos coloquiales chilenos de Arauco/Biobío): digestivo (empacho, guatita, reflujo), cardiovascular (soplo, presión), respiratorio (gripazo, bronquitis), renal, piel (culebrilla, sarna, tiña), ojo (con nota: NO hay oftalmólogo), pediátrico/materno, dolor/cabeza, urgencias rurales (araña de rincón, marea roja), preguntas admin (Fonasa/GES/Isapre/licencia/orden kine/PAP/certificados)
- **`EMERGENCIAS` expandido** en `flows.py` con 30+ términos: respiratorio severo, cardiovascular, sangrado, trauma, quemaduras, **araña de rincón/loxosceles** (endémica Biobío), marea roja, neurológico, ocular urgente
- **Fase 1 multimodal — Whisper**: `openai==1.51.0` en `requirements.txt`; `OPENAI_API_KEY` en `config.py`; helpers `download_whatsapp_media()` (Graph API 2 pasos: URL firmada + blob) y `transcribe_audio()` (Whisper-1, idioma es) en `main.py`; webhook maneja `msg_type == "audio"`: descarga media → Whisper → texto procesa por pipeline normal `handle_message`; prefijo 🎤 en log para visibilidad en el panel

**Hecho (sesión 2026-04-10 — hardening / deuda técnica)**:
- **SQLite WAL + busy_timeout=5000** en `session.py::_conn()` → previene `database is locked` bajo concurrencia (verificado en prod: archivos `.db-wal`/`.db-shm` creados)
- **Rate limiter sliding window** en `main.py` (30 msg/min por teléfono) aplicado a WhatsApp, Instagram y Messenger en el webhook
- **Auth admin vía `Authorization: Bearer`**: 23 endpoints migrados a `Depends(require_admin)` / `Depends(require_ortodoncia)`; query param `?token=` sigue funcionando como fallback para el panel HTML
- **CORSMiddleware** restrictivo con whitelist (`agentecmc.cl`, VPS, localhost)
- **`/health` real**: ahora hace ping a Medilink `/sucursales` con timeout 3s y reporta `medilink_ms` (verificado: ~188ms en prod)
- **`purge_old_data()`** ~~semanal (domingos 04:00 CLT)~~ **desactivado** (2026-04-12): retención indefinida de datos. ~90 MB/año.
- **`valid_rut()` endurecido**: rechaza vacíos, longitudes fuera de 8–9 chars, caracteres no numéricos, DV inválido
- **Masoterapia validación estricta**: `re.findall(r"\b(20|40)\b", txt)` en `flows.py` (antes `"20" in txt` matcheaba "200", "2020")
- **Bare `except Exception: pass`** reemplazados por excepciones específicas (`sqlite3.OperationalError`, `json.JSONDecodeError`, `ValueError`) con logging
- **`print()` eliminado** en `medilink.crear_cita` → `log.error`
- **`twilio==9.2.3`** quitado de `requirements.txt` (no se usaba)
- **`sessions.db` huérfano** (0 bytes) eliminado de la raíz del repo
- Commit `8b901dc` pusheado y deployado; verificado con `curl` los 4 caminos de auth (sin token 401, query 200, bearer 200, bearer inválido 401)

**Hecho (sesión 2026-04-10 — anterior)**:
- **Fix timezone crítico**: servidor corre en UTC → Olavarría (16-21 CLT) era filtrado como pasado. Fix: `ZoneInfo("America/Santiago")` en 3 `datetime.now()` en `medilink.py`
- **Fix Medicina General**: `solo_ids=[73,1]` busca Abarca+Olavarría juntos (horarios complementarios 08-16 y 16-21); Márquez como overflow; slot más próximo entre ambos gana (`todos[0]`)
- **Caché SQLite `citas_cache`**: tabla en `session.py`; primera carga sync Medilink, luego lectura local instantánea
- **Sync incremental** en `get_citas_seguimiento_mes`: detecta días faltantes, sync paralelo con `asyncio.gather` (60 req secuenciales → paralelo, ~18s → ~1s); días vacíos marcados con fila sentinel `id_paciente=0`
- **Cron 23:50 CLT** (02:50 UTC) en APScheduler para sync diario automático
- **Endpoint `POST /admin/api/kine/sync`**: fuerza re-sync de una fecha específica
- **Módulo Ortodoncia** (`🦷` en admin panel):
  - Tabla `ortodoncia_cache` en SQLite con `total` desde `/atenciones/{id}` de Medilink
  - Auto-clasificación: $120.000=Instalación (I), $30.000=Control (C), otro=Pendiente (?)
  - `tipo_manual=1` protege overrides del usuario contra re-sync
  - Vista Cards y Vista Matriz (estilo Excel): filas=pacientes, columnas=fechas, círculos con colores
  - `ORTODONCIA_TOKEN` separado (`cmc_ortodoncia_2026`) para acceso independiente
- **Filtros Mes/Año/Todos** en modales Pacientes en Control y Ortodoncia
- **Sync histórico**: 108 combinaciones faltantes de Marzo 2026 re-sincronizadas; ortodoncia desde Ene 2025 en curso
- **Endpoint** `POST /admin/api/ortodoncia/sync`: fuerza re-sync de Ortodoncia (Dra. Castillo, ID 66)

**Hecho (sesiones 2026-04-08 y 2026-04-09)**:
- IDs Medilink corregidos: Márquez 18→**13**, Borrego 28→**23**, Etcheverry 26→**21**
- Masoterapia con duración variable: estado `WAIT_DURACION_MASOTERAPIA` antes de buscar slots
- Panel admin: etiquetas de especialidad legibles (`espLabel()`), tiempo de espera humano (`waitLabel()`)
- Panel "Pacientes en Control": seguimiento de sesiones kine/ortodoncia/psicología/nutrición
- Instagram y Facebook Messenger integrados: webhook unificado, íconos en panel
- Fidelización completa en `app/fidelizacion.py` (post-consulta, reactivación, adherencia kine, control esp., cross-sell)
- Detección pasiva de Arauco: cualquier mención guarda tag silenciosamente

**Hecho (sesión 2026-04-10)**:
- Harness `tests/harness_50.py`: 50 escenarios base + 8 regresión de bugs + 4 de confirmación pre-cita = 62/62 ✅
- 4 bugs arreglados (commit `5c55d80`):
  1. `WAIT_SLOT` acepta `si/sí/confirmo/dale/ok` libre → confirma el sugerido (antes solo botón)
  2. `WAIT_CITA_CANCELAR` aborta con `no/menu/cancelar` (antes quedaba atrapado)
  3. `WAIT_CITA_REAGENDAR` mismo fix
  4. `EMERGENCIAS` + regex flexible: captura `dolor fuerte en el pecho`, `mucho sangrado`, `me sangra mucho la nariz` (riesgo clínico)
- **Confirmación sí/no pre-cita** (feature #4 del sprint):
  - Columnas `confirmation_status` + `confirmation_at` en `citas_bot` (migración in-place)
  - Recordatorio 09:00 ahora es mensaje interactivo con 3 botones: ✅ Confirmo / 🔄 Cambiar hora / ❌ No podré ir
  - IDs de botón embebidos (`cita_confirm:<id>` etc.) → funcionan sin sesión activa
  - Handler `_handle_confirmacion_precita` en `flows.py` al inicio de `handle_message`
  - Reagendar desde botón: pre-rellena `cita_old` + perfil del paciente → cero pasos extra
  - Cancelar desde botón: salta directo a `CONFIRMING_CANCEL` con la cita cargada
  - Endpoint `GET /admin/api/confirmaciones?fecha=YYYY-MM-DD` con resumen {confirmed, reagendar, cancelar, pendiente}

**Estado del servidor**: ✅ Chatbot corriendo en `https://agentecmc.cl` (commit `58ea618`, systemd). GES Assistant en `127.0.0.1:8002` (systemd). GES API pública en `https://api-ges.agentecmc.cl` (nginx+SSL). Frontend GES en `https://ges.agentecmc.cl` (Vercel).

**Pendiente (corto plazo)**:
- Rotación de PAT → SSH keys en remotes de `chatbot-cmc` y `ges-clinical-app` (Mac + VPS)
- Promover número +56945886628 a pacientes reales (redes sociales, recepción)
- Monitorear primeras conversaciones reales
- Tags clínicos automáticos (dolor lumbar, rehabilitación) — detectar con Claude y guardar en contact_tags

**Próximo sprint (plan aprobado 2026-04-10, actualizado 2026-04-13)**:
1. ✅ ~~Modo degradado Medilink~~ — DONE
2. ✅ ~~Reagendar en un paso~~ — DONE
3. ✅ ~~Lista de espera~~ — DONE
4. ✅ ~~Confirmación sí/no pre-cita~~ — DONE (backend + pill admin + modal)
5. ✅ ~~Refactor main.py~~ — DONE (3,045 → 468 líneas)
6. ✅ ~~Whisper (transcripción audios)~~ — DONE y deployado con feedback al paciente
7. ✅ ~~Glosario dental + estética expandido~~ — DONE (34/34 tests foros)
8. ✅ ~~WhatsApp Business features~~ — DONE (status webhooks, BSUID, quality, docs, templates)
9. ✅ ~~Cross-sell ORL↔Fono~~ — DONE (prestaciones reales + precios)
10. ✅ ~~Registro expandido paciente nuevo~~ — DONE (fecha_nac, sexo, comuna, email, abandonment tracking)
11. ✅ ~~Stress test 200 casos~~ — DONE (290/290 total)
12. **Copagos Fonasa/Isapre al confirmar** — requiere verificar si Medilink expone previsión del paciente.
13. **Dashboard métricas fidelización** — tasa respuesta post-consulta, conversión reactivación, adherencia kine.
14. **Migración número WhatsApp** — backup conversaciones + delete WA Business + registrar en Cloud API.

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
