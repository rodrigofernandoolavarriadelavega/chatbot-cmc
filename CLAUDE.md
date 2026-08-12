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

### ⭐ DEFAULT con varias ventanas: un worktree por ventana (`scripts/newsession.sh`)
El dueño trabaja con **muchas sesiones de Claude en paralelo sobre este repo**.
Tras DOS incidentes el 2026-06-10 (symlink `data/` que borró la DB de prod, y una
carrera del índice compartido que bundleó trabajo ajeno — ver memory
`cmc_sistema_orden_multisesion`), el flujo **recomendado** es un worktree por
ventana: cada una su carpeta + rama + **índice propio** → cero choque de archivos
Y de índice.
```bash
bash scripts/newsession.sh eco      # crea ~/cmc-work/eco en rama session/eco (enlaza SOLO .env)
# …abrís Claude en ~/cmc-work/eco y trabajás 100% aislado…
bash scripts/wship.sh "fix(eco): X" # commitea, rebasa sobre origin/main y deploya con guardas
bash scripts/newsession.sh --done eco
```
El worktree usa su propio `data/` aislado (vacío) — NUNCA se symlinkea `data/`.

### ⚡ Alternativa rápida (pocas ventanas / cambio puntual): `scripts/ship.sh`
Cuando hay poca concurrencia y querés embarcar **tu pedazo** sin el setup del
worktree:
```bash
bash scripts/ship.sh "<mensaje de commit>" <archivo> [archivo...]
```
Hace: snapshot → aparta (stash) los archivos modificados que NO nombraste →
**verifica que lo staged sea SOLO lo tuyo** (aborta si una carrera contaminó el
índice) → commitea SOLO los tuyos → `deploy.sh` (G1-G4) → restaura la WIP ajena.
Si hay ≥8 archivos ajenos en vuelo te avisa que mejor uses un worktree.
**Regla de oro: tocá archivos DISTINTOS de las otras ventanas.** Nada se pierde:
`eod_snapshot.sh` (launchd horario) respalda a `eod-backup/*` y `nightly_order.py`
(03:00) deja informe en `~/Desktop/cmc_orden_*.html`.

### Deploy directo — `scripts/deploy.sh` (cuando el árbol es tuyo solo / una sola ventana)
```bash
bash scripts/deploy.sh
```
El script tiene 4 guardas y auto-rollback: **G1** aborta si el árbol de prod está
sucio (no borra trabajo sin commitear), **G2** aborta si prod divergió de origin/main
(exige reconciliar, no forzar pull), **G3** deep-import antes de reiniciar (el
`ast.parse` NO ve un `NameError` de import faltante — ya tumbó el bot 3 min una vez),
**G4** verifica `/health` 200 + servicio active y hace **rollback automático** si no.

> ⚠️ **REGLA DE ORO (incidente 2026-06-07):** el VPS es DESTINO de deploy, **nunca**
> de edición. Jamás `git commit` ni editar archivos directo en `/opt/chatbot-cmc`.
> Todo cambio nace en local → `git push` → `scripts/deploy.sh`. Editar en el server
> dejó prod 21 commits atrás + ~20 archivos sin commitear que un pull habría borrado.
> Ver memory `cmc_prod_divergencia_2026_06_07`.

**Deploy manual (solo emergencia, conociendo los riesgos de arriba):**
```bash
git push origin main
ssh root@157.245.13.107 "cd /opt/chatbot-cmc && git pull --ff-only && systemctl restart chatbot-cmc"
```

**Verificación post-deploy** (el script ya la hace; manual):
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
| 78 | Dra. Cecilia Unibazo | Psiquiatría | **40** (TELECONSULTA, $60.000 particular, abono) — **martes 16-20** (6 cupos) y **jueves 15-20** (7 cupos), verificado en Medilink 2026-07-29. ⚠️ Pendiente en Medilink: mover el jueves a **15:20**-20:00 para que el último paciente sea 19:20 y cierre a las 20:00 (hoy termina 19:40 con 20 min muertos) |
| 79 | Dra. Franca González | Neurología | 30 (TELEMEDICINA, $65.000 particular, solo desde 15 años) |
| 80 | TM Ana Celedón | Tecnología Médica Oftalmológica | 20 (PRESENCIAL, $15.000 particular a todos, sin Fonasa) |

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
**Última actualización**: 2026-08-12

### 2026-08-12 — Auditoría harness (27 fallas explicadas → 103/103) + bug real "otra persona" + rango cerrado
- **Las 27 fallas "baseline" del harness, clasificadas una a una** (exigencia
  del dueño): CERO fallos reales de producción. 21 = fixture con fecha
  congelada `2026-04-15` (quedó en el pasado el 2026-04-28 cuando entró la
  defensa de slots expirados 4dadedd — el harness ofrecía horas del pasado y
  el bot correctamente las rechazaba); 4 = mock `fake_crear_cita` sin kwarg
  `modalidad`; 2 = tests del flujo viejo de "cambiar_datos" (rediseñado a
  sub-menú cd_* en 616d3b4). Arreglado TODO en `tests/harness_50.py`: fechas
  dinámicas (`_fecha_futura`), mocks con `**kwargs`, TERC-02/FT-02 reescritos.
- **Hallazgo grave del harness**: `classify_with_context` NO estaba mockeado —
  la suite "offline" llamaba a la API REAL de Claude en cada mensaje que
  pasaba el fast-path (costo + lentitud + flakiness por sampling). Mockeado a
  `{"action": "continue"}`. **Suite ahora 103/103 × 5 corridas, determinista.**
- **BUG REAL DE PRODUCCIÓN cazado gracias al flaky** (TERC-01 fallaba ~25%):
  en WAIT_RUT_AGENDAR, "otra persona" pasaba por el pre-router LLM y Claude a
  veces lo clasificaba `escape: cambiar_profesional` → reset + re-oferta → el
  paciente perdía el flujo de terceros. Fix doble en `flows.py`: (1)
  `_es_respuesta_obvia_al_prompt` ahora marca obvio `_OTRA_PERSONA_RE` en
  WAIT_RUT_AGENDAR/WAIT_MODALIDAD (el LLM no lo ve); (2) cinturón en el escape
  `cambiar_profesional`: si el texto matchea "otra persona", continue.
  Verificado 6/6 con Claude real.
- **Rango de fechas CERRADO** (revisión del dueño 2026-08-11): "la próxima
  semana" = lunes-domingo (antes solo mínimo → podía ofrecer 3 semanas
  después sin aviso). `buscar_primer_dia` ganó `fecha_desde/fecha_hasta`
  NATIVOS (ya no se fabrica lista `excluir` con tope escondido); sin cupo en
  rango cerrado → aviso explícito "No encontré horas para la próxima semana,
  te muestro la primera después". Fix "+15": "en 15 días"=+15 ≠ "en dos
  semanas"=+14. Nuevo "esta semana" (hoy→domingo). Captura de preferencia
  UNIFICADA en `_stash_preferencia_fecha` (antes triplicada en 3 call sites);
  la preferencia sobrevive al cambio de especialidad (no se hace pop) y una
  nueva sobreescribe la anterior. Negación "no mañana/mañana no puedo" ya no
  devuelve mañana. "mañana en la mañana" (día+franja) ya no pierde el día.
- Tests: extractores 18/18 + combinaciones adversariales, rango nativo con
  Medilink simulado 4/4, sobreescritura de preferencias 4/4, harness 103/103
  ×5, normalizer 52/52.

### 2026-08-12 — Modo-caída Medilink 403 plataforma-inactiva + ventana contexto 24h (DEPLOYADO commit bac21ea)
- **Incidente real esta mañana**: Medilink devolvió 403 "no se encuentra
  activa" (plataforma suspendida, distinto de un 403 de permisos puntual y
  distinto del 429 de saturación). El breaker viejo (`probe_up()`) leía
  `status_code<500` como "vivo" — un 403 pasaba el filtro y el circuito NUNCA
  se abría, así que el bot seguía intentando agendar contra una plataforma
  caída sin avisarle a nadie.
- **`app/medilink_outage.py`** (nuevo): `MedilinkInactiva` (excepción
  específica, no `MedilinkRateLimited`); modo caída persistido en sqlite —
  abre con 2 fallos consecutivos, cierra con 2 sondeos OK consecutivos
  (anti-flapping), tope duro de 24h. Captura el contexto de TODO mensaje
  entrante mientras está abierto (incluidos los que quedan en
  `HUMAN_TAKEOVER`), sin tocar `reset_session` — el paciente no pierde su
  lugar en el flujo.
- **`app/jobs.py`**: watcher nuevo `_job_medilink_outage_watcher`, cada 3 min,
  solo actúa si hay contexto pendiente (ahorra requests). Al recuperar, arma
  un mensaje dirigido con horas reales (`buscar_slots_dia`/`buscar_primer_dia`)
  — regla barata primero (especialidad ya capturada), Claude Haiku
  (`detect_intent`) solo si el contexto quedó ambiguo. Skip si el rut ya tiene
  cita futura, si pasaron >24h, o si quedó en `HUMAN_TAKEOVER` (recepción
  lo lleva).
- **`app/main.py`**: catch de `MedilinkInactiva` en los dos webhooks (WhatsApp
  + IG/FB), mensaje distinto al de saturación ("problema técnico… apenas se
  recupere te escribo"), captura forzada del mensaje que disparó el fallo.
  Job registrado en el scheduler `id="medilink_outage_watcher"`.
- **Tests**: `tests/test_medilink_outage.py` (nuevo, 57 casos) +
  `tests/test_429_no_es_caida.py` — ambos 100%. **OJO**: `harness_50.py`
  76/103 y `harness_stress_200.py` 169/200 son baseline preexistente en HEAD
  (WIP de otra sesión en `flows.py`/`harness_50.py`, sin commitear) — verificado
  por aislamiento (mismo conteo de fallas con y sin este fix staged). No
  relacionado a este deploy.
- **Verificado en PROD**: `/health` 200, `systemctl is-active` → active, 0
  tracebacks/ERROR post-restart (fuera de `MEDILINK_429`, guardrail conocido
  — cola de reintentos de la caída de esta mañana, no señal de deploy roto).
- **PENDIENTE**: observar el próximo ciclo real de caída→recuperación en prod
  para confirmar que el recontacto dirigido sale con las horas correctas.

### 2026-08-06 — Resultados de examen enviados por el paciente (DEPLOYADO commit 115ef84)
- **Caso Manuel Yaupe (56998901932, 17:15 CL)**: mandó el PDF de sus exámenes
  de Inmunomédica. El bot **SÍ lo leyó** (`extract_text_from_pdf` devolvió el
  texto completo) pero no lo reconoció, así que el texto entró al pipeline de
  agendamiento y le respondió *"¡Gracias por enviarme tus datos! Para agendar
  necesito saber qué especialidad…"*. **El problema nunca fue leer, fue
  clasificar**: `_CLINICAL_DOC_KEYS` solo cubría ficha/entrevista/formulario/
  consentimiento. No volvió a escribir → abandono silencioso.
- **`app/examenes_lab.py`** (nuevo): `parece_examen()` por PUNTAJE (fuerte=2:
  laboratorio conocido, "toma de muestra", "valores de referencia" · media=1:
  analitos, tipo de muestra, "nº orden" · umbral=3), no lista de keywords
  sueltas — así "me dijeron que tengo el colesterol alto" no dispara.
  Informes de imagen puntúan aparte (modalidad + lenguaje de informe).
  `nombre_en_examen()` extrae el nombre impreso (informativo, para el aviso).
- **VETO CLAVE — resultado ≠ orden**: una *orden* ("se solicita hemograma")
  es indicación para HACERSE el examen; ese paciente quiere agendar y de eso
  se encarga `eco_orden_ocr.py`. Mandar una orden por el carril del resultado
  le cuesta la hora. `_es_orden_sin_resultados()` es veto duro, no puntaje.
  Segundo veto: comprobante bancario (tiene su carril en `abono_transferencia`).
- **`app/main.py`** (~10653): la rama corre ANTES del truncado a 200 chars
  (defensivo: en otros laboratorios las marcas pueden venir más abajo) y antes
  del clasificador de documentos clínicos, que se dejó **intacto sobre el texto
  truncado** a propósito — sus keywords son genéricas y ampliarle el alcance
  dispararía takeovers de más. Respuesta al paciente + `log_event`
  `examen_recibido` + `HUMAN_TAKEOVER` (motivo `examen_recibido`) + aviso a
  `ADMIN_ALERT_PHONE` envuelto en try/except (la ventana de 24 h del admin
  puede estar cerrada; eso no puede tumbar el webhook — lección del 2026-08-04).
- **Tests** `tests/test_examenes_lab.py` 14/14, incluido el texto real del PDF
  de Yaupe tal como sale de PyMuPDF (columnas mezcladas, etiqueta y valor
  separados por salto de línea). Verificado en PROD contra el PDF real:
  `es_examen=True`, nombre `MANUEL ARTURO YAUPE RIVAS`. harness_50 76/103
  (=baseline), normalizer 52/52, `/health` 200, logs sin errores.
- **Corpus continuo**: revisar el evento `examen_recibido` (trae las señales
  que dispararon) para ampliar `_LABORATORIOS`/`_ANALITOS`, mismo patrón que
  `eco_tipo_nomatch`.
- **PENDIENTE HUMANO**: Yaupe quedó sin respuesta útil y su examen sin avisar.
  Decidir si se le escribe.

### 2026-08-11 — Preferencia de fecha en la PRIMERA oferta (cierra backlog #1)
- Antes: "para la próxima semana necesito hora con X" ofrecía HOY (la cañería
  fecha_preferida solo entendía hoy/mañana/pasado mañana). `flows.py`:
  - `_detectar_fecha_pedida_idle` ahora también parsea día de semana nombrado
    ("el viernes" → próximo viernes) reusando `_DIAS_SEMANA`+`_proxima_fecha_dia`
    de WAIT_SLOT, y el combo "el viernes de la próxima semana" (empuja dentro
    de esa semana).
  - Nueva `_detectar_fecha_min_idle` para RANGOS ("próxima semana"/"otra
    semana"/"semana que viene" → próximo lunes; "en 15 días"/"en dos semanas"
    → hoy+14). Se stashea como `data["fecha_min_pedida"]` en los 3 call sites
    (IDLE top, branch agendar, WAIT_ESPECIALIDAD).
  - `_iniciar_agendar` convierte el mínimo en `excluir=[hoy..min-1]` (máx 21
    días) para `buscar_primer_dia` en las 3 rutas default (MG dual, MF,
    genérica) → la primera oferta cae directo dentro del rango pedido. La
    fecha exacta pedida sigue teniendo prioridad sobre el rango.
- Tests: extractores 11/11 (relativos a hoy), harness 76/103 (=baseline),
  normalizer 52/52, deep-import OK.

### 2026-08-06 — Vocabulario especialidades + números reciclados
- **Caso "siquiatra" (56991927216)**: "necesito una hora con siquiatra" caía a
  Medicina General porque la frase genérica `("necesito una hora","medicina
  general")` de `_FRASES_ESPECIALIDAD` ganaba por primer-match y "siquiatra"
  (sin p) no estaba en ningún vocabulario. Fix doble en `flows.py`: (a) ~35
  variantes/typos nuevos de especialidades (siquiatr/sicolog cubren también
  psiquiatr/psicolog por substring; oftamolog, oculista, otorino, tramatolog,
  quinesiolog, nuerolog, matron, etc.) + espejo en `_INTENT_CACHE` de
  `claude_helper.py`; (b) "necesito una hora" salió de la lista y ahora es un
  check guardado al FINAL de `_detectar_especialidad_en_texto`: si nombra a
  alguien no reconocido ("hora con urologo") devuelve None → Claude decide.
  PENDIENTE conocido: la preferencia de fecha ("próxima semana") se ignora en
  la primera oferta (misma clase que el caso María "para hoy" del backlog).
- **Números reciclados** (`app/numero_equivocado.py` nuevo, decisión del
  dueño): detector conservador de "número equivocado" (patrones fuertes +
  débiles con contexto) en `handle_message` → responde, tag
  `posible_numero_equivocado`, HUMAN_TAKEOVER motivo `numero_equivocado`.
  Recepción confirma con botón "☎️ Nº equivocado" del panel v2 →
  `POST /admin/api/numero-equivocado/{phone}/limpiar` ejecuta la receta 4
  capas (Medilink PUT celular/telefono="" en TODAS las fichas con ese número,
  BI telefono=NULL + opt_outs_marketing 2 formatos, consent→declined, tag
  local + evento). NUNCA auto-opt-out por texto libre. Ver memoria
  `cmc_numeros_reciclados_receta.md`. Tests: detector 14/14, vocab 18/18,
  harness 76/103 (=baseline), normalizer 52/52, JS panel OK, endpoint 403 OK.

### 2026-08-05 — Módulo Alma "Ausentismo" — ranking pacientes que no asisten
- **`app/ausentismo.py`** (nuevo): tabla local `ausentismo_citas` + recolector
  nocturno 04:50 CLT que camina `/citas` DÍA POR DÍA con `fecha eq` (carril
  batch, backoff de `_get`). **Gotcha nuevo medido en prod**: una consulta por
  RANGO de fechas corta la paginación en ~25 páginas (~1.250 filas) sin error
  — un rango de 12 meses devuelve solo ~5 semanas; `fecha eq` no sufre eso.
  Backfill 12 meses automático la primera noche; días caídos por 429 se
  reintentan al final tras pausa. Metodología validada 2026-08-05: no-show =
  id_estado=8 & anulacion=0 · excluye id_estado=14 (reagenda) · dedup por
  (paciente, día, profesional) con precedencia atendida > no_show > anulada.
- **`app/ausentismo_routes.py`** (nuevo): `/alma/ausentismo` (HTML) +
  `/api/ausentismo/ranking` (filtros dias/prof/minimo) + `/api/ausentismo/
  paciente/{id}` (historial) + `POST /api/ausentismo/recolectar` (gated dueño,
  barrido manual en background). Auth patrón agenda_ticker_routes.
- **`templates/alma_ausentismo.html`** (nuevo): premium Alma (paleta kine,
  Montserrat local, cero CDN), KPIs, chips período/mínimo, select profesional,
  ranking con severidad (Crítico ≥4 / Reincidente ≥2), pill "próxima cita"
  para confirmar por teléfono, historial expandible, tarjetas en móvil.
- **`app/config.py`**: key `ausentismo` en registry + visible para perfil
  Recepción (ADMIN_TOKEN). **`app/main.py`**: registro de rutas + cron 04:50.
- Validado: deep-import, test funcional de la metodología (dedup intradía,
  exclusión reagenda, próxima cita), node --check del JS, TestClient 200/403.

### 2026-08-05 — Pestaña "Pacientes con seguro complementario" en Alma Kine (DEPLOYADO commit cd1911a)
- **`templates/alma_kine.html`**: nueva pestaña junto a "Programa" (toggle JS
  `tab()`/`.view.on`), snapshot estático de pacientes de kine con probable
  seguro complementario a Fonasa (detectado por patrón de copago residual
  bajo en caja, histórico 09-sep-2021→04-ago-2026). Datos embebidos en
  `SC_DATA` (const JS, no vive query — snapshot fijo, "no se actualiza en
  vivo" explícito en la UI). KPIs, tabla filtrable/buscable por nombre/RUT/
  teléfono, 3 tarjetas de acción (activos con pocas sesiones → asegurar
  programa; dormidos con historial largo → reevaluación clínica documentada,
  no solo por el patrón de pago; post-alta funcional → plan particular,
  bono Fonasa Res. 49/2009 Grupo 06 no cubre mantención).
- Sin cambios en `main.py`/`kine_routes.py` (la ruta `/alma/kine` ya servía
  el HTML vía placeholder replace `__TOKEN__`/`__KINE_FINANCIERO__`/
  `__PROF_DASHBOARD_URL__`, sin tocar).
- Validado: imports profundos OK, JS embebido `node --check` OK, `TestClient`
  200 local, harness_50/normalizer/stress_200 sin regresión (fallas
  preexistentes en `main` antes del cambio, verificado con `git stash`).
  Post-deploy: `/health` 200, `/alma/kine?token=...` 200 en prod con la
  pestaña nueva presente, logs limpios.


### 2026-08-01 — OCR de órdenes de eco (DEPLOYADO commit cf91da1, GATED OFF)
- **`app/eco_orden_ocr.py`** (nuevo): foto de orden médica en `wait_eco_tipo` →
  Claude **Sonnet** visión (Haiku leyó mal 3/3 manuscritas de hospital) clasifica
  el documento + transcribe exámenes → `decidir_accion()` (pura, testeada) cruza
  con `route_ecografia` → botones "Sí, agendar" vía mecanismo
  `especialidad_sugerida`+`eco_tipo_text` existente. El paciente SIEMPRE confirma.
- **Guard clave**: solo se rutea un examen con raíz ecográfica (`_parece_eco`,
  regex + fuzzy) — sin él, "Escoliosis lumbar (radiografía)" ofrecía eco por el
  keyword suelto "lumbar" (pasó en la validación e2e).
- **Validación con 19 imágenes reales de prod (90d)**: 13 acciones automáticas
  correctas (incl. 2 obstétricas respondidas al tiro), 5 caídas seguras a
  recepción (2 letra ilegible con confianza baja, multi-examen, comprobante de
  pago, Holter), 1 parcial, **0 falsos positivos**.
- **Flag `ECO_ORDEN_OCR_ACTIVE`** (config.py, default false) → encender:
  `.env` del VPS + restart. Sin flag el flujo actual (recepción) no cambia.
- Tests: `tests/test_eco_orden_ocr.py` 16/16. Evento nuevo `eco_orden_ocr`
  (decision/motivo/examenes/confianza) para auditar antes de encender.

### 2026-08-01 — Fix confirmación recordatorio 3ª persona + RUT conocido en ver-reservas + previsión declarativa (DEPLOYADO, commit 8429dc9)
- Caso real Dayan 56988538373: la mamá confirmó en 3ª persona/gerundio
  ("Si asistira", "Confirmando la hora de dayan") → caía al menú genérico →
  flujo ver-reservas pedía el RUT que el bot ya tenía → el texto se parseó
  como RUT inválido ("no reconozco ese RUT"). Recepción rescató a mano.
- **`app/flows.py`**: `_RE_CONFIRM_RECOD`/`_RE_NO_RECOD` (regex 3ª persona/
  gerundio) junto a los sets exactos de confirmación/negativa de recordatorio
  (~línea 2892). Gate intacto: solo dispara si hay cita futura con
  `reminder_sent=1`/`reminder_2h_sent=1` y sin `confirmation_status` en
  `citas_bot` — si no hay fila, cae al flujo normal (verificado que "quiero
  confirmar una hora para mañana" en IDLE sin cita recordada NO se roba por
  el gate). La confirmación ahora hace eco de especialidad/fecha/hora/
  profesional. `_iniciar_ver`: si `get_profile(phone)` tiene RUT, re-despacha
  directo a `WAIT_RUT_VER` con el RUT conocido (mismo patrón que
  `_iniciar_reagendar`). `WAIT_RUT_VER`: texto sin dígitos (<4) que matchea
  verbo de asistencia/confirmación → `reset_session` + re-dispatch a IDLE
  (patrón BUG-5 de `WAIT_RUT_AGENDAR`), evento `rut_ver_era_respuesta_recordatorio`
  — verificado sin loop (IDLE reprocesa con el bloque de confirmación, que
  responde directo sin volver a `WAIT_RUT_VER`).
- **`app/claude_helper.py`**: SYSTEM_PROMPT — pregunta declarativa de
  previsión/cobertura ("El cardiólogo atiende por Fonasa", caso 56989975963,
  verificado `intent="otro"` en `conversation_events`) → intent `info` con
  `respuesta_directa`, nunca menú genérico.
- **Tests**: deep-import 13 módulos OK · harness_50 76/103 (=baseline) ·
  normalizer 52/52 · stress_200 169/200 (=baseline, comparado contra
  2462b72 con revert temporal de los 2 archivos) · suites eco (92+15+2+27) y
  recordatorios_recepcion (11+2) OK. 0 regresiones.
- **Deploy**: `scripts/deploy.sh` (G0-G4 + auto-rollback), sin intervención
  manual en VPS. `/health` 200 · `systemctl is-active` active · logs limpios
  (sin ERROR/Traceback) en los primeros minutos post-restart.

### 2026-08-01 — Fix comprensión de ecografías (DEPLOYADO, commit fddc0e5)
- **Diagnóstico con data real** (60d de prod): 213 `ecografia_sin_tipo` vs 126
  `ecografia_tipo_matched`. Los fallos: (a) vocabulario/typos ~12% ("Ginecóloga"
  sola, "Dopler", "unguinal", "obstretica", "Lumbrosaca", "pélvica" sola),
  (b) respuestas laterales ~12% (precio/Fonasa/"no tengo la orden") que quemaban
  reintentos, (c) foto de la orden médica que perdía contexto.
- **`app/ecografias.py`**: keywords nuevos del corpus + **capa fuzzy difflib**
  (fallback del substring exacto; tokens ≥5 letras, ratio ≥0.85, solo keywords
  de 1 palabra, misma prioridad de grupos) — mata la clase entera de typos de
  1-2 letras. Gate ampliado: `ecotom\w*`, `dopler`.
- **`app/flows.py` (wait_eco_tipo)**: preguntas laterales (precio/fonasa/bono,
  "orden", hora/cuándo) se responden y se re-pregunta el tipo SIN incrementar
  `eco_tipo_reintentos`. Evento nuevo **`eco_tipo_nomatch`** (txt crudo 200c) =
  corpus continuo → revisar mensual para ampliar vocabulario.
- **`app/main.py` (media handler)**: imagen/documento con `wait_eco_tipo` activo
  → `handoff_reason="media:orden_eco"`, evento `eco_orden_foto_recepcion`, ack
  específico "si es tu orden médica, recepción te agenda la eco".
- **Tests**: `tests/test_eco_vocab_fuzzy.py` 27/27 (corpus real + anti-falso-
  positivo + regresión). Suites eco existentes 92+15+2 OK. harness_50 76/103
  (= baseline). Smoke en prod post-deploy: 6/6 OK.

### 2026-07-14 — Agendar v2 + dashboard de mejora (SIN DEPLOY, sin commit)
- **Investigación**: 10 agentes (3 auditoría del agendador actual + 6 evidencia
  internacional/nacional + síntesis) → 107 hallazgos, 25 intervenciones.
  JSON completo embebido en el dashboard.
- **Dashboard**: `static/mejora-agendador.html` (4 tabs: Diagnóstico / Plan v2 /
  Evidencia / Guardrails). Se sirve solo por /static/.
- **Bug crítico de la v1 CONFIRMADO en navegador**: en desktop ≥981px NO existe
  botón para avanzar del paso "Día y hora" (la .mbar está display:none) —
  conversión desktop 0%. La v1 además: reagendar cancela ANTES de reservar la
  nueva, cancelar sin confirmación, RUT inputmode=numeric sin tecla K (~9% de
  RUT terminan en K), Google Fonts (viola regla cero-CDN).
- **Agendar v2**: `templates/agendador_v2.html` (nuevo, ~74KB autocontenido,
  cero CDN, system fonts) + ruta `/agendar/v2` en main.py. 3 pasos, "primera
  hora disponible" multi-profesional, escasez honesta (conteo real, se suprime
  si falló parte del pool), WhatsApp de rescate en todo error/vacío, WCAG AA
  (≥16px funcional, taps ≥48px, foco gestionado, aria-live), timeout 20s con
  reintento preservando datos (sessionStorage), reagendar en orden seguro
  (nueva→cancelar vieja), confirmación antes de cancelar, RUT con K + módulo 11,
  tarjetas WhatsApp para Psiquiatría/Neurología/Oftalmología TM (no agendables
  online), Google Calendar link en el éxito.
- **Flag NUEVA**: `AGENDADOR_V2_ENABLED` (config.py, default false) — la v2 es
  404 salvo `?preview=ADMIN_TOKEN` hasta que el dueño la encienda (necesita
  además AGENDADOR_PUBLICO_ENABLED, ya ON en prod).
- **Backend endurecido** (afecta también v1): `medilink.buscar_paciente(strict=)`
  — con strict=True (solo agendador_routes, 4 call sites) un 429/caída de
  Medilink ya NO se confunde con "paciente no existe" (evitaba fichas DUPLICADAS
  en el HIS al reservar durante un rate-limit). Bot sin cambios (strict=False
  default). Nota de Ortodoncia del catálogo pasada a "usted".
- **Verificación**: 2 rondas adversariales (guardrails 7/7 OK, contrato API,
  a11y con ratios calculados, navegador real móvil+desktop con guard anti-POST).
  Ronda 1 encontró 1 crítico (renderSlotsArea inexistente rompía todo camino
  de vuelta) + 3 altos — TODOS corregidos y re-verificados en ronda 2.
- **Tests**: test_agendador_e2e 9 salvaguardas PASS (el fallo "el HTML
  referencia la API real" es PRE-existente del harness, no de este cambio).
- **NO deployado, NO commiteado.** Archivos tocados: templates/agendador_v2.html
  (nuevo), static/mejora-agendador.html (nuevo), app/main.py (+18 líneas: loader
  + ruta), app/config.py (+4: flag), app/medilink.py (+10: strict),
  app/agendador_routes.py (strict=True x4 + nota usted). OJO: main.py/config.py
  tienen además WIP de otra ventana — hacer `git add` selectivo por hunks o
  coordinar antes de commitear.


### 2026-07-13 — Carril de persistencia: medición del embudo + fix de raíz (SIN DEPLOY, sin commit)
- **Medición estaba rota**: `intent_agendar` se logueaba en solo 1 de ~84 sitios
  que llaman a `_iniciar_agendar` → 424 "intents" vs 992 citas creadas en 30d
  (>100% conversión, imposible). Fix sistémico: `_iniciar_agendar` (chokepoint
  único, todas las rutas de entrada convergen ahí) ahora logea
  `funnel_intent_agendar` UNA vez por intento real, con un `_funnel_id` (uuid)
  que viaja en `data` y se propaga a `funnel_especialidad`/`funnel_slot_ofrecido`
  (ya existían)/`funnel_slot_elegido` (nuevo, en `_slot_confirmed`)/
  `funnel_confirmacion`/`cita_creada` — permite reconstruir el recorrido
  completo de una persona con un solo query por `funnel_id`.
- **Embudo real reconstruido** (30 días, prod, vía `messages.state` — funciona
  para cualquier rango histórico sin depender de la instrumentación rota):
  1051 entraron → 960 vieron slots → 579 llegaron a confirmar → 582 citas
  (conversión punta a punta 50.8%). El agujero más grande: 394 vieron un
  horario y nunca confirmaron; de esos, 230 en silencio total (nunca volvieron
  a escribir) — la "lista de espera del hoy" (disclaimer "no tengo para HOY")
  es un modo real pero chico (45 casos/30d, 84% no agenda después).
- **BUG DE RAÍZ encontrado y corregido**: `session.py::phone_tiene_solo_citas_canceladas`
  decía "o ninguna" en el docstring y lo cumplía literal — un paciente SIN
  ninguna cita (el caso más común, abandonó antes de crear una) también daba
  `True`. El reenganche (`jobs._enviar_reenganche`, cron cada 5 min) toma esa
  rama como "tenía cita y se canceló, no insistir" y, sin cancelación que
  reinvitar, solo loguea `reenganche_skip_cita_cancelada` sin enviar nada —
  para siempre. Medido: 214 de los 230 silencios puros (93%) caían acá. Fix
  de una línea: exigir que el phone tenga ≥1 fila histórica en `citas_bot`
  antes de considerar "solo canceladas". Sin historial → `False` → sigue el
  reenganche normal. Probablemente el mayor agujero único de conversión del bot.
- **Carril nuevo** `app/persistencia.py` + `app/persistencia_routes.py`
  (`GET/POST /api/persistencia*`, mismo patrón auth que `mg_abandono_routes.py`):
  máquina de estados por CONSULTA (no por sesión) `ABIERTA→CONTACTADA→
  AGENDADA|NO_EXPLICITO|EXPIRADA` en tabla `consultas_persistencia`. Segundo
  toque único (2-26h desde apertura, después de que el reenganche ya tuvo su
  oportunidad), reusa `contact_budget.py` (no crea presupuesto nuevo),
  `phones_with_open_offers()`, detecta "no"/stop explícito → cierre inmediato
  sin más contacto. Fuera de ventana 24h NO envía nada (falta template
  `seguimiento_consulta_pendiente`, borrador en
  `templates/whatsapp_templates/seguimiento_consulta_pendiente.DRAFT.json`,
  NO subido a Meta). **GATED OFF por `PERSISTENCIA_ACTIVE`** (default false) —
  cron registrado cada 15 min en `main.py` pero inerte hasta que se encienda.
- **Tests**: harness_50 76/103, stress_200 169/200, normalizer 52/52 — idéntico
  al baseline (comparado con `git stash` antes/después). 0 regresiones.
- **NO deployado.** `app/flows.py`/`app/session.py`/`app/main.py` modificados
  localmente (instrumentación + fix + registro del carril, todo aditivo/gated).
  Falta: revisar, `git add` selectivo (hay WIP ajeno de otra sesión en
  `app/pagos_routes.py`/`templates/alma_pagos.html` — NO tocar), deploy con
  `scripts/deploy.sh`, y decidir si subir el template borrador a Meta.
- Script de medición reusable: `scripts/embudo_persistencia.py` (solo lectura,
  corre con `venv/bin/python3` en el VPS).

### 2026-06-09 — Deploy 9 bugfixes críticos (commit 5c59702)

### 2026-06-09 — Deploy 9 bugfixes críticos (commit 5c59702)
- **B1** `flows.py:6981/6988` — NameError en tiempo de ejecución: `PROFESIONALES` bare → `_PROFS_AP` (causa de 66 resets confirmados en logs)
- **B2** `flows.py:1947` — alias `PROFESIONALES as _PROFS_HQ` en `_responder_pregunta_horario` (elimina riesgo UnboundLocalError, 20 warnings previos)
- **B3** `flows.py:6165` — eliminado bloque que negaba psiquiatría (fósil pre-Unibazo). Conflicto con d65314a resuelto manteniendo la versión más completa (con `_iniciar_agendar`)
- **B4** `flows.py:107` — `_first_name` devuelve `""` (no `"paciente"`) cuando nombre vacío; guards en callers ya existentes
- **B5** `flows.py:PRECIOS_SLOT` — Psiquiatría $60.000 particular; Matrona $16.000/$30.000 ambas modalidades
- **B6** `flows.py:2802` — `WAIT_META_SLOT_CHOICE` y `WAIT_META_WAITLIST` agregados a `_FLOW_STATES` (consent no interceptaba CTWA)
- **B7** `flows.py:10829` — cross-sell loop: máx 1 reprompt, al 2° intento fallido escapa con reset
- **B8** `pagos_routes.py:2114` — `meta_new` completo (fuente/match_confianza/copago/metodo_pago/creado_por/monto_medilink/prevision/profesional), eliminados 6 KeyError en logs
- **B9** `ecografias.py` — keywords obstétricas → grupo `obstetrica_no_disponible`; flows.py maneja flujo en 2 call sites
- **Conflicto resuelto**: 948283b creado sobre base anterior a d65314a (WAIT_ESPECIALIDAD psiquiatra). Cherry-pick con resolución conservadora: se mantuvo HEAD que era más completo.
- **Tests post-deploy**: harness_50 76/103 (=baseline), harness_stress_200 169/200 (=baseline), normalizer 52/52, WRONG=0 (=baseline). 0 regresiones nuevas.
- **Deploy**: /health 200, service active, logs limpios.

**Fecha**: 2026-04-27 / 2026-04-28 (sesión maratónica que cubrió varios frentes)
**Historial completo**: ver claude-mem timeline o git log

### 2026-06-07 — Fix menu-loop ecografía (auditoría portavión)
- **Bug**: paciente dice "eco abdominal" → bot explica + ofrece "✅ Sí, agendar"
  guardando `especialidad_sugerida="ecografía"` (genérico, el órgano se perdía).
  Al aceptar, `_iniciar_agendar` recibía "ecografía" sin órgano y el texto del
  turno era el payload del botón → `route_ecografia()`→None → re-preguntaba el
  tipo → **menu-loop** (patrón de fricción dominante de eco, auditoría 2026-06-07).
- **Fix** (`app/flows.py`): al ofrecer agendar tras explicar un tipo de eco se
  persiste `data["eco_tipo_text"]=txt`; `_iniciar_agendar` lo consume (pop) y
  resuelve el routing sin re-preguntar. Cubre path info/precio y disponibilidad.
- **Tests**: `tests/test_eco_menu_loop.py` (2/2, falla sin el fix). Corregidos 4
  tests obsoletos de `test_ecografia_routing.py` que afirmaban mamaria→Rejón
  (la verdad es mamaria→Pardo, partes blandas, fix 2026-05-22). Suite eco 78/78.
- **OJO**: las 9 fallas de test_no_silent_date_jump/test_meta_ctwa/test_takeover
  son PRE-EXISTENTES (idénticas con/sin este cambio). Y los docs ECO_FRICCION_*
  /ECO_VARIANTES_DAVID que una sesión previa dijo escribir NO están en el repo.

---
**Fecha**: 2026-05-29
**Historial completo**: ver claude-mem timeline o git log

### Deploy 2026-05-29 — pool PG + teléfono fijo

- `baf7f3c` — fix(estabilidad,contacto): pool PG boxes endurecido + teléfono fijo corregido a (44) 296 5226
  - `app/main.py`: conn=None guard, PoolError→HTTP 503, rollback antes putconn, cursor en finally, 3 fases para no retener PG durante llamadas Medilink
  - `app/config.py` + `claude_helper.py`, `flows.py`, `fidelizacion.py`, `winback.py`, `dental_winback.py`, `messaging.py`: (41)→(44) en todos los mensajes outbound
  - `scripts/medir_conversion_postfix.py`: nuevo script read-only de medición conversión
  - Servidor: `/opt/chatbot-cmc/.env` actualizado (`CMC_TELEFONO_FIJO=(44) 296 5226`)
  - /health post-deploy: **200** · service: **active** · logs: limpios

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

### ⚠️ INCIDENTE 2026-06-10 04:17 UTC — symlink data/ commiteado borró sessions.db de prod (RESUELTO)
- Una carrera de `newsession.sh` dejó `~/chatbot-cmc/data` como symlink a sí mismo; el commit `4988a7c` lo arrastró trackeado; el pull en prod reemplazó `/opt/chatbot-cmc/data/` real → bot crash-loop → DB vacía.
- RESTAURADO desde `/opt/backups/chatbot-cmc/sessions_20260610_033002.db.gz` (backup diario 03:30). Pérdida: solo 03:30→04:17 UTC. La DB vacía del gap quedó en `data/sessions.db.POST-INCIDENTE-0417`.
- REGLAS: (1) `data` está en .gitignore y NUNCA debe trackearse — si `git status` muestra `data` (symlink o dir), NO commitear, avisar. (2) Antes de ship/deploy: `git ls-files data` debe estar VACÍO. (3) `~/chatbot-cmc/data` debe ser un directorio real, no symlink.
