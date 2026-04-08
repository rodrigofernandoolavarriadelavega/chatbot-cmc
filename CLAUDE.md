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
| ID | Nombre | Especialidad |
|----|--------|-------------|
| 1 | Dr. Rodrigo Olavarría | Medicina General |
| 73 | Dr. Andrés Abarca | Medicina General |
| 18 | Dr. Alonso Márquez | Medicina General / Medicina Familiar |
| 28 | Dr. Manuel Borrego | Otorrinolaringología |
| 60 | Dr. Miguel Millán | Cardiología |
| 64 | Dr. Claudio Barraza | Traumatología |
| 61 | Dr. Tirso Rejón | Ginecología |
| 65 | Dr. Nicolás Quijano | Gastroenterología |
| 55 | Dra. Javiera Burgos | Odontología General |
| 72 | Dr. Carlos Jiménez | Odontología General |
| 66 | Dra. Daniela Castillo | Ortodoncia |
| 75 | Dr. Fernando Fredes | Endodoncia |
| 69 | Dra. Aurora Valdés | Implantología |
| 76 | Dra. Valentina Fuentealba | Estética Facial |
| 59 | Paola Acosta | Kinesiología |
| 77 | Luis Armijo | Kinesiología |
| 26 | Leonardo Etcheverry | Kinesiología |
| 52 | Gisela Pinto | Nutrición |
| 74 | Jorge Montalba | Psicología Adulto / Psicología Infantil |
| 49 | Juan Pablo Rodríguez | Psicología Adulto |
| 70 | Juana Arratia | Fonoaudiología |
| 67 | Sarai Gómez | Matrona |
| 56 | Andrea Guevara | Podología |
| 68 | David Pardo | Ecografía |

## Cancelación de citas en Medilink
Usar `PUT /citas/{id}` con body `{"id_estado": 1}` — esto pone la cita en estado "Anulado" con `estado_anulacion=1`.
**No usar** `{"estado_anulacion": 1}` solo (da error "Undefined index").

## Creación de citas en Medilink
Requiere el campo `duracion` (minutos). Se calcula como `_h_to_min(hora_fin) - _h_to_min(hora_inicio)`.

## Meta Cloud API
- App ID: 804421499380432
- System User: Chatbotcmc-systemuser (ID: 61576699507415) — token permanente
- Números de prueba: +1 555 641 7609 (Meta test number, sin aprobación requerida)
- Número prepago CMC: +56945886628 (Display Name en revisión)
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

## Dashboard admin
- Ruta: `http://157.245.13.107:8001/admin?token=cmc_admin_2026`
- Incluido en el mismo proceso del bot (no es proyecto separado)
- Muestra métricas, conversaciones activas y estado del sistema

## Sesión en curso
**Fecha**: 2026-04-07

**Hecho hoy**:
- Deploy completo en DigitalOcean con HTTPS — dominio `agentecmc.cl` con SSL Let's Encrypt, nginx como proxy, ngrok eliminado
- Webhook Meta actualizado a `https://agentecmc.cl/webhook`
- Agendamiento directo desde panel de recepción (`/admin`) — modal "Nueva Cita" con búsqueda por RUT, slots y confirmación
- Lista de profesionales completamente actualizada: 24 profesionales, nombres con Dr./Dra. correctos, especialidades corregidas
- Nuevos profesionales: Dr. Alonso Márquez (ID 18), Leonardo Etcheverry (ID 26), Dr. Manuel Borrego (ID 28)
- Correcciones: Luis Armijo → Kinesiología, Valentina Fuentealba → Estética Facial, Fernando Fredes → Endodoncia, David Pardo Muñoz eliminado (duplicado de ID 68)
- Psicología: Jorge Montalba = Adulto e Infantil; Juan Pablo Rodríguez = solo Adulto
- Medicina Familiar agregada (Dr. Márquez atiende con bono medicina general $7.880)
- **Precios revisados uno a uno con Dr. Olavarría y actualizados en `claude_helper.py`** — todas las especialidades corregidas y desplegadas

**Precios confirmados (resumen)**:
- Medicina General: $25.000 particular / $7.880 Fonasa
- Medicina Familiar (Márquez): $30.000 particular / $7.880 Fonasa
- Kinesiología Luis/Leo: $7.830 Fonasa / $20.000 particular
- Kinesiología Paola: masoterapia 20 min $17.990 / 40 min $26.990
- Nutrición: $4.770 Fonasa / $20.000 particular + bioimpedanciometría $20.000
- Psicología: $14.420 Fonasa / $20.000 particular
- ORL: solo consulta $35.000 y control $8.000 (todo particular)
- Odontología: evaluación $15.000, exodoncia $40.000–$60.000, blanqueamiento $75.000, destartraje+profilaxis $30.000, resina desde $35.000
- Endodoncia: anterior $110.000 / premolar $150.000 / molar $220.000
- Implantología: corona+tornillo desde $650.000
- Ecografía: ecotomografías $40.000 / doppler $90.000

**Estado del servidor**: corriendo en `https://agentecmc.cl` (`/opt/chatbot-cmc`), sin errores

**Pendiente**:
- Promover número +56945886628 a pacientes reales (redes sociales, recepción, etc.)
- Monitorear primeras conversaciones reales y corregir lo que falle

---

## Deuda técnica pendiente
1. Precios en `claude_helper.py` hardcodeados en SYSTEM_PROMPT — actualizar manualmente cuando cambien
2. Dr. Luis Armijo (ID 77) aparece como Medicina General en Medilink pero es Kinesiólogo — error de datos en Medilink, no en el bot
3. SQLite no escala bien con concurrencia alta — migrar a PostgreSQL o Redis si hay múltiples sucursales
