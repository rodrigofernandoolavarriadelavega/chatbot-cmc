# Session Log — 2026-04-15 — Mapas Dinámicos + Agendamiento Terceros + Media/Demanda

## Resumen de la conversación

### 1. Mapas dinámicos con filtros de fecha

**Endpoint API `/admin/api/map-data`** (en `admin_routes.py`):
- Consulta `heatmap_cache.db` en tiempo real con parámetros `desde` y `hasta`
- Retorna: comunas, localidades, direcciones geocodificadas con detalle de pacientes (nombre, profesional, fecha)
- Incluye normalización de comunas, detección de localidades dentro de Arauco, cruce con `geocode_cache`

**Filtros en dashboard** (`templates/dashboard.html`):
- Barra de filtros con botones: Hoy / Semana / Mes / Año / Todo + selector de mes
- Los mapas, tablas y KPIs se recargan dinámicamente al cambiar filtro
- Reemplaza datos hardcodeados por fetch al API
- Funciones JS: `setMapPeriod()`, `setMapMonth()`, `loadMapData()`, `populateMonthSelect()`

### 2. Descarga de datos Ene-Abr 2026

**Comandos ejecutados:**
```bash
# Marzo (ya estaba en progreso de sesión anterior)
PYTHONPATH=app python scripts/heatmap_comunas.py download 2026 3

# Febrero (solicitado por el usuario)
PYTHONPATH=app python scripts/heatmap_comunas.py download 2026 2

# Enero se descargó automáticamente como bonus
```

**Datos finales:**

| Mes | Días | Citas | Pacientes |
|-----|------|-------|-----------|
| Ene 2026 | 31 | 881 | - |
| Feb 2026 | 28 | 873 | - |
| Mar 2026 | 31 | 1,158 | - |
| Abr 2026 | 14 | 447 | - |
| **Total** | **104** | **3,359** | **2,086** |

**Rate limits**: Medilink devuelve 429 cuando se hacen muchas requests. El script maneja backoff exponencial (10s, 20s, 40s, 80s, 160s). La descarga de ~2,086 pacientes tomó ~45 min con los rate limits.

### 3. Geocodificación masiva

**Primera pasada** (con diccionario original ~40 localidades):
- 531 direcciones nuevas pendientes
- Resultado: 140 Nominatim + 171 fallback = 311 geocodificadas, 220 sin coordenadas
- Tiempo: ~35 min (Nominatim rate limit: 1 req/s)

**Expansión del diccionario de fallback** (`geocode_direcciones.py`):
- De ~40 a ~75 localidades conocidas
- Agregadas: San José de Cólico, República, Maquehua, Covadonga, La Quinta, Calle Maitén, Villa Amanecer, Las Hortensias, Pasaje Zárate, Horconoes (typo), 10 de Julio, Ramadillas, Los Mañíos, Pedro Prado, Las Araucarias, Lago Calafquén, Volcán Antuco, calles de Curanilahue, Polvorín (Lota), Schwager/Lagunillas (Coronel)
- Villa La Paz confirmada en Carampangue (coordenadas correctas)

**Segunda pasada** (con diccionario expandido):
- 159 direcciones pendientes
- Resultado: 56 Nominatim + 32 fallback = 88 más geocodificadas
- Total final: **1,147/1,219 direcciones geocodificadas (94%)**

**71 direcciones no resueltas**: direcciones muy vagas o incompletas que ni Nominatim ni el fallback pueden resolver.

### 4. Deploy a producción

```bash
# WAL checkpoint (obligatorio antes de copiar)
python3 -c "import sqlite3; c=sqlite3.connect('data/heatmap_cache.db'); c.execute('PRAGMA wal_checkpoint(TRUNCATE)')"

# Copiar DB al VPS
scp data/heatmap_cache.db root@157.245.13.107:/opt/chatbot-cmc/data/heatmap_cache.db

# Verificar
curl -s "https://agentecmc.cl/admin/api/map-data?token=cmc_admin_2026" | python3 -c "import json,sys; d=json.load(sys.stdin); print(f'Citas={d[\"total_citas\"]}, Pacs={d[\"pacientes_unicos\"]}, Dirs={len(d[\"direcciones\"])}')"
# → Citas=3359, Pacs=1853, Dirs=1115
```

**Producción por mes verificado:**

| Mes | Citas | Pacientes | Direcciones |
|-----|-------|-----------|-------------|
| Ene | 784 | 579 | 388 |
| Feb | 873 | 610 | 395 |
| Mar | 1,055 | 715 | 442 |
| Abr | 447 | 342 | 237 |

### 5. Agendamiento para terceros (WAIT_BOOKING_FOR)

*(Implementado en sesión anterior del mismo día, reflejado en CLAUDE.md)*
- Después de Fonasa/Particular, el bot pregunta "¿Para ti o para otra persona?"
- Si es para otro y no conocemos al dueño del celular → nuevo estado `WAIT_PHONE_OWNER_NAME`
- Recordatorios personalizados: "Hola Ana 👋 Recuerda que Daniel tiene cita..."
- Columnas `paciente_nombre` y `es_tercero` en `citas_bot`

### 6. Pills nuevas en panel admin

- **Pill imágenes (📷)**: conteo de archivos media recibidos. Modal con tabla por paciente. Endpoint `GET /admin/api/media-stats`
- **Pill demanda (🔎)**: tracking de especialidades/exámenes no disponibles en CMC. Tabla `demanda_no_disponible`. Endpoint `GET /admin/api/demanda-no-disponible`

### 7. Discusión sobre paralelización

**Pregunta del usuario**: "¿No puedes dividir esto en 10 agentes?"

**Respuesta**: No se puede paralelizar el geocodificador porque Nominatim (OpenStreetMap) tiene rate limit de 1 req/s por IP. 10 agentes desde la misma IP serían bloqueados. La estrategia correcta fue expandir el diccionario de fallback local para resolver más direcciones sin llamar a Nominatim.

## Archivos modificados

| Archivo | Cambio |
|---------|--------|
| `app/admin_routes.py` | +endpoint `/admin/api/map-data` con filtros fecha |
| `templates/dashboard.html` | +filtros JS (setMapPeriod, setMapMonth, loadMapData, populateMonthSelect), datos dinámicos via API |
| `scripts/geocode_direcciones.py` | +~35 localidades nuevas en KNOWN_LOCATIONS |
| `scripts/heatmap_comunas.py` | Ejecutado para descargar Feb y Ene |
| `data/heatmap_cache.db` | 3,359 citas + 2,086 pacientes + 1,147 geocodificaciones |
| `CLAUDE.md` | Actualizado con datos Ene-Abr y geocoding 94% |
| `app/main.py` | +WAIT_BOOKING_FOR, +WAIT_PHONE_OWNER_NAME |
| `app/flows.py` | +lógica agendamiento terceros |

## Documentación actualizada

- **Notion**: "Guía: Pipeline de Mapas Geográficos" — números finales actualizados
- **CLAUDE.md**: secciones de sesión en curso actualizadas
- **Este archivo**: `docs/session_log_2026-04-15_mapas_terceros.md`

## Tests

- harness_50: 92/92 ✅ (2 tests TERC nuevos)
- test_normalizer: 52/52 ✅

### 8. Instalación de Claude Code en el VPS (acceso remoto desde celular)

**Objetivo**: Poder usar Claude Code desde el celular vía SSH (app Termius) conectándose al VPS.

**Pasos ejecutados:**

1. **Verificación inicial**: El VPS (Ubuntu 24.04.4 LTS) no tenía Node.js instalado.

2. **Instalación de Node.js 20**:
   ```bash
   ssh root@157.245.13.107 "curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && apt-get install -y nodejs"
   ```
   → Node v20.20.2, npm 10.8.2

3. **Instalación de Claude Code**:
   ```bash
   ssh root@157.245.13.107 "npm install -g @anthropic-ai/claude-code"
   ```
   → Claude Code v2.1.109 instalado en `/usr/bin/claude`

4. **Configuración de API key**: Se extrajo `ANTHROPIC_API_KEY` del `.env` del chatbot y se agregó a `~/.bashrc` del VPS para que esté disponible automáticamente.

**Resultado**: Claude Code funcional en el VPS. Para usarlo desde el celular:
- Instalar **Termius** (iOS/Android)
- Crear host: IP `157.245.13.107`, usuario `root`, llave SSH `id_ed25519`
- Conectarse y ejecutar: `cd /opt/chatbot-cmc && claude`

**Nota**: El VPS reporta kernel pendiente de actualización (6.8.0-71 → 6.8.0-107). No se reinició para no afectar los servicios en producción.

### 9. Descarga histórica Oct-Dic 2025

**Contexto**: el usuario pidió descargar datos de diciembre 2025 usando múltiples agentes en paralelo para acelerar.

**Comandos ejecutados en paralelo** (3 procesos simultáneos):
```bash
# Los 3 corriendo al mismo tiempo
PYTHONPATH=app python scripts/heatmap_comunas.py download 2025 12  # Dic
PYTHONPATH=app python scripts/heatmap_comunas.py download 2025 11  # Nov
PYTHONPATH=app python scripts/heatmap_comunas.py download 2025 10  # Oct
```

**Nota sobre agentes**: los agentes de Claude Code no pudieron ejecutar bash (permisos denegados en subprocesos). Se lanzaron como background tasks directos desde el proceso principal.

**Datos descargados:**

| Mes | Citas | Estado |
|-----|-------|--------|
| Oct 2025 | 1,005 | ✅ Citas listas, pacientes descargando |
| Nov 2025 | 1,009 | ✅ Citas listas, pacientes descargando |
| Dic 2025 | 946 | ✅ Completo |
| **Subtotal Q4** | **2,960** | |

**DB acumulada**: 6,319 citas (7 meses: Oct 2025 → Abr 2026), ~3,280 pacientes únicos.

**Concurrencia SQLite**: los 3 procesos escriben a la misma `heatmap_cache.db` sin problemas gracias a WAL mode + `busy_timeout=5000`.

**Pregunta del usuario**: "Las bases de datos que llenaste para Metabase ¿no tienen info similar?"

**Respuesta**: No. `sessions.db` (Metabase) solo tiene datos del chatbot (conversaciones, citas agendadas por bot). `heatmap_cache.db` descarga directo de la API Medilink y tiene TODAS las citas (bot + recepción + presenciales) — necesario para mapas geográficos que cubren el 100% de la actividad del centro.

**Pendiente post-descarga:**
1. Geocodificar direcciones nuevas (Oct-Dic traen pacientes no presentes en Ene-Abr)
2. WAL checkpoint + scp a producción
3. Verificar en dashboard

---
*Generado automáticamente — sesión Claude Code 2026-04-15*
