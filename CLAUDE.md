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

SSH ahora es **solo por llave pública** (Ed25519 en `~/.ssh/id_ed25519`, password deshabilitado el 2026-04-10). Conexión directa con `ssh root@157.245.13.107`.

### Opción A — one-liner desde el Mac (recomendado)
```bash
git push origin main
ssh root@157.245.13.107 "cd /opt/chatbot-cmc && git pull && pkill -f 'chatbot-cmc.*uvicorn'; sleep 2; cd /opt/chatbot-cmc && setsid nohup venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8001 </dev/null >/var/log/cmc-bot.log 2>&1 & disown"
```

### Opción B — sesión SSH interactiva
```bash
ssh root@157.245.13.107
cd /opt/chatbot-cmc
git pull
pkill -f 'chatbot-cmc.*uvicorn'
sleep 2
setsid nohup venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8001 </dev/null >/var/log/cmc-bot.log 2>&1 &
disown
```

**⚠️ Importante — usar `setsid`, no solo `nohup`**: cuando el comando se ejecuta dentro de `ssh "..."` (one-liner), un `nohup ... &` pelado deja el proceso asociado al pty remoto y éste se muere al cerrarse la sesión SSH — bot caído. `setsid` fuerza un nuevo process group, desligando el uvicorn del SSH. Verificado en prod 2026-04-10.

**Verificación post-deploy**:
```bash
curl -s -o /dev/null -w 'HTTP %{http_code}\n' https://agentecmc.cl/health   # → 200
ssh root@157.245.13.107 "ps aux | grep 'chatbot-cmc.*uvicorn' | grep -v grep"
```

Los logs viven en `/var/log/cmc-bot.log` en el servidor.

El GES Assistant corre por separado como **systemd** (`ges-assistant.service`, bind `127.0.0.1:8002`). Para sus propios restarts usar `systemctl restart ges-assistant` — no se ve afectado por deploys del chatbot.

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
**Fecha**: 2026-04-11

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

**Estado del servidor**: ✅ Chatbot corriendo en `https://agentecmc.cl`. GES Assistant en `127.0.0.1:8002` (systemd). GES API pública en `https://api-ges.agentecmc.cl` (nginx+SSL). Frontend GES en `https://ges-clinical-app.vercel.app` (Vercel).

**Pendiente (corto plazo)**:
- Fix trailing space en `NEXT_PUBLIC_API_URL` en Vercel → redeploy (causa `%20` en URLs del API)
- Custom domain `ges.agentecmc.cl` para el frontend Vercel (CNAME en Cloudflare + add domain en Vercel)
- Deploy del feature confirmación pre-cita
- Agregar columna "Confirmados mañana" al dashboard admin (frontend) — endpoint ya expuesto
- Agregar `OPENAI_API_KEY` al `.env` del servidor y deployar Fase 1 Whisper (commits locales listos, no pusheados)
- Rotación de PAT → SSH keys en remotes de `chatbot-cmc` y `ges-clinical-app` (Mac + VPS)
- Promover número +56945886628 a pacientes reales (redes sociales, recepción)
- Tags clínicos automáticos (dolor lumbar, rehabilitación) — detectar con Claude y guardar en contact_tags

**Próximo sprint (plan aprobado 2026-04-10)**:
1. ✅ ~~Modo degradado Medilink~~ — DONE
2. ✅ ~~Reagendar en un paso~~ — DONE
3. ✅ ~~Lista de espera~~ — DONE
4. ✅ ~~Confirmación sí/no pre-cita~~ — DONE (backend + tests; falta columna frontend panel)
5. **Copagos Fonasa/Isapre al confirmar** — requiere verificar si Medilink expone previsión del paciente.
6. **Dashboard métricas fidelización** — tasa respuesta post-consulta, conversión reactivación, adherencia kine.
7. **Tests automatizados** — sprint dedicado, no intercalado con features.

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
1. **Partir `main.py` (2.620 líneas)** — separar admin routes, HTML template, scheduler y webhook en módulos
2. **Mover HTML del panel** (~1.700 líneas) a template externo con Jinja2 + static JS
3. **Auth real del panel** — token embebido en el HTML es visible en DOM; migrar a cookie httpOnly firmada + login
4. **Suite `pytest`** — cubrir `valid_rut`, `smart_select`, transiciones core de `flows.py`
5. Precios en `claude_helper.py` hardcodeados en SYSTEM_PROMPT — actualizar manualmente cuando cambien
6. Dr. Luis Armijo (ID 77) aparece como Medicina General en Medilink pero es Kinesiólogo — error de datos en Medilink, no en el bot
7. SQLite no escala bien con concurrencia alta — migrar a PostgreSQL o Redis si hay múltiples sucursales
8. Verificar IDs de profesionales menos frecuentes (Millán, Barraza, Rejón, etc.) directamente en API para asegurar que sean correctos
