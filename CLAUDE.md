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
│   ├── main.py          # FastAPI app, webhook Meta, mensajes interactivos, API admin, reenganche
│   ├── flows.py         # Máquina de estados (lógica conversacional + mensajes lista/botones)
│   ├── claude_helper.py # detect_intent() y respuesta_faq() con Claude Haiku
│   ├── medilink.py      # Wrapper API Medilink (slots, pacientes, citas)
│   ├── session.py       # Sesiones SQLite + log_message, get_conversations, log_event, get_sesiones_abandonadas
│   ├── reminders.py     # Recordatorios automáticos de citas (APScheduler, 09:00 CLT)
│   └── config.py        # Variables de entorno (.env)
├── auditor.py           # Script de cuadre contable (Medilink vs Transbank/efectivo/transferencias)
├── data/
│   └── sessions.db      # Base de datos SQLite de sesiones (no se commitea)
├── Dockerfile
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
```bash
# Desde el Mac — subir cambios
git push origin main

# En el servidor (157.245.13.107, usuario root)
ssh root@157.245.13.107   # contraseña: ver .env local
cd /opt/chatbot-cmc
git pull
kill $(ps aux | grep uvicorn | grep -v grep | awk '{print $2}')
nohup venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8001 > /var/log/cmc-bot.log 2>&1 &
```

Los logs viven en `/var/log/cmc-bot.log` en el servidor.

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
- [x] Job semanal `purge_old_data` (dom 04:00): messages>90d, events>180d + VACUUM
- [x] `valid_rut` endurecido y masoterapia con matching estricto

## Dashboard admin
- Ruta: `http://157.245.13.107:8001/admin?token=cmc_admin_2026`
- Incluido en el mismo proceso del bot (no es proyecto separado)
- Muestra métricas, conversaciones activas y estado del sistema

## Sesión en curso
**Fecha**: 2026-04-10

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
- **`purge_old_data()`** semanal (domingos 04:00 CLT): borra `messages` > 90d y `conversation_events` > 180d + `VACUUM`
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

**Estado del servidor**: ✅ corriendo en `https://agentecmc.cl`, deployado commit `e619125`. Verificaciones OK: `/health` reporta `medilink:ok`, `medilink_state:up`, `intent_queue_depth:0`, `waitlist_depth:0`; scheduler con 11 jobs registrados (incluye `waitlist_check` 07:00 CLT y `medilink_watchdog` cada 1 min).

**Pendiente (corto plazo)**:
- Agregar `OPENAI_API_KEY` al `.env` del servidor y deployar Fase 1 Whisper (commits locales listos, no pusheados)
- Verificar ortodoncia modal en producción (hard refresh Cmd+Shift+R, sync puede tardar hasta 10-15 min)
- Promover número +56945886628 a pacientes reales (redes sociales, recepción)
- Monitorear primeras conversaciones reales
- Tags clínicos automáticos (dolor lumbar, rehabilitación) — detectar con Claude y guardar en contact_tags

**Próximo sprint (plan aprobado 2026-04-10, pendiente arranque)**:
1. ✅ ~~Modo degradado Medilink~~ — DONE
2. ✅ ~~Reagendar en un paso~~ — DONE
3. ✅ ~~Lista de espera~~ — DONE
4. **Confirmación sí/no pre-cita** — botón `Confirmo` / `Cambiar hora` en el recordatorio de 09:00 AM del día anterior (reduce no-shows).
5. **Copagos Fonasa/Isapre al confirmar** — requiere verificar si Medilink expone previsión del paciente.
6. **Dashboard métricas fidelización** — tasa respuesta post-consulta, conversión reactivación, adherencia kine.
7. **Tests automatizados** — sprint dedicado, no intercalado con features.

---

## Deuda técnica pendiente
1. **Partir `main.py` (2.620 líneas)** — separar admin routes, HTML template, scheduler y webhook en módulos
2. **Mover HTML del panel** (~1.700 líneas) a template externo con Jinja2 + static JS
3. **Auth real del panel** — token embebido en el HTML es visible en DOM; migrar a cookie httpOnly firmada + login
4. **Suite `pytest`** — cubrir `valid_rut`, `smart_select`, transiciones core de `flows.py`
5. Precios en `claude_helper.py` hardcodeados en SYSTEM_PROMPT — actualizar manualmente cuando cambien
6. Dr. Luis Armijo (ID 77) aparece como Medicina General en Medilink pero es Kinesiólogo — error de datos en Medilink, no en el bot
7. SQLite no escala bien con concurrencia alta — migrar a PostgreSQL o Redis si hay múltiples sucursales
8. Verificar IDs de profesionales menos frecuentes (Millán, Barraza, Rejón, etc.) directamente en API para asegurar que sean correctos
