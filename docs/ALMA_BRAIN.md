# Alma Brain — la capa agéntica de Alma

Generaliza el patrón probado del **Autopilot** (`app/autopilot/`) de "solo Meta Ads"
a TODOS los dominios del CMC. Convierte Alma de un conjunto de dashboards pasivos
+ crons ciegos en un sistema que **percibe, razona, propone y ejecuta con límites
duros y aprobación humana**.

Principio rector (heredado de `autopilot/advisor.py`): **la IA propone, las reglas
mandan.** El modelo nunca llama a un executor directo — deja propuestas que un
humano libera y los límites duros (`policy.check`) filtran.

## Arquitectura (4 fases, todas implementadas)

```
sensors.py  → PERCIBIR  : un sensor por dominio, lee fuentes YA existentes
state.py    → PERCIBIR  : compone los sensores + deriva alertas cross-domain
copilot.py  → RAZONAR   : Claude con tool-use real sobre el estado
tools.py    → ACTUAR    : registry de tools (lectura ejecuta; acción encola propuesta)
policy.py   → GOBERNAR  : HardLimits por dominio + regla de auto-confirmación
routes.py   → API + UI  : endpoints + módulo /alma/brain en el shell
```

Todo es **aditivo y degrada con gracia**: si una fuente no está, el dominio se
marca `available=false` y el resto opera. **Medilink-free**: ningún sensor golpea
la API de Medilink (cuello de rate-limit del bot); lee BI Postgres, sessions.db y
snapshots ya persistidos por crons.

## Fuentes por dominio (reusa lo existente, no reinventa)

| Dominio | Fuente | Fiel |
|---|---|---|
| agenda | `bi.fact_citas` (volumen + tendencia vs ventana previa) | sí |
| caja | `bi.fact_pagos` (ingreso real, NUNCA `/atenciones`) | sí |
| demanda | `sessions.db` (`sin_disponibilidad`, `demanda_no_disponible`) vía `api_demanda_data` | sí |
| ads | snapshot del Autopilot (`autopilot/world_state.load_snapshot`) | sí |
| fidelización | `bi.v_winback_cohortes_contactables` | sí |

## Endpoints

| Método | Ruta | Qué |
|---|---|---|
| GET | `/alma/brain` | UI del Copilot (módulo iframe en el shell) |
| GET | `/alma/brain/api/state` | snapshot global (cache; lo construye si falta) |
| POST | `/alma/brain/api/refresh` | reconstruye el estado ahora (read-only) |
| POST | `/alma/brain/api/chat` | turno del copiloto `{messages, allow_actions}` |
| GET | `/alma/brain/api/proposals` | cola de propuestas |
| POST | `/alma/brain/api/proposals/{id}/approve` | aprobar → policy + executor |
| POST | `/alma/brain/api/proposals/{id}/reject` | rechazar |

Cron: `alma_brain_snapshot` 06:00 CLT (corre siempre — es solo lectura).

## Acceso

- Módulo `cerebro` ("Copilot Alma") en `ALMA_MODULE_REGISTRY` (`config.py`).
- El estado lo ve cualquier token admin válido.
- El **Copilot y las acciones** requieren perfil con `modulos=None` (dueño) o
  `brain=True`. Recepción NO tiene acceso.

## Flags (.env) — todos OFF por defecto (seguro)

```bash
# Modelo del copiloto (default Sonnet, mejor razonamiento económico)
ALMA_BRAIN_MODEL=claude-sonnet-4-6
ALMA_BRAIN_MAX_ITERS=6

# Kill-switch GLOBAL de ejecución. Sin esto, TODO queda como propuesta/manual.
ALMA_BRAIN_EXECUTE=false

# Límites duros de contacto a pacientes (Ley 21.719)
ALMA_BRAIN_REQUIRE_CONSENT=true      # contacto masivo exige opt-in
ALMA_BRAIN_REQUIRE_TEMPLATES=true
ALMA_BRAIN_WINBACK_MAX_DAY=50

# Ads
ALMA_BRAIN_ADS_MAX_STEP=0.20         # paso máx de presupuesto por corrida

# Escrituras a Medilink desde el cerebro (agenda/citas) — OFF por defecto
ALMA_BRAIN_ALLOW_MEDILINK_WRITES=false

# Auto-confirmación de cupos liberados a lista de espera (kill-switch DEDICADO,
# angosto a propósito: NO abre las demás escrituras de Medilink)
ALMA_OPERATIVA_AUTOCONFIRM=false
```

## Pendiente (requiere tu aprobación — toca producción)

1. **Cablear `policy.should_auto_confirm` al flujo de waitlist.** La regla pura ya
   existe y está testeada (`tests/test_alma_brain.py`). Falta conectarla en el job
   de detección de cancelaciones (`jobs._job_detectar_cancelaciones`) → cuando un
   paciente de la lista de espera acepta un cupo liberado, decidir auto-confirmar
   (crear cita en Medilink) vs derivar a recepción. **Ajusta primero el umbral** en
   `should_auto_confirm` (¿48h para especialistas escasos? ¿historial de no-show?).
   Gateado por `ALMA_OPERATIVA_AUTOCONFIRM`.
2. **Executors reales por `kind`** en `tools._EXECUTORS`. Hoy solo
   `ads_dryrun_refresh` es auto-ejecutable; el resto queda "aprobada_manual". Sumar
   executors (ej: encolar pieza orgánica al Autopilot, disparar win-back gateado por
   consent) a medida que apruebes cada automatización.
3. **Sensor de agenda fino (ociosidad/yield)**: hoy da volumen + tendencia. El
   cálculo de ociosidad vive en `/boxes` (requiere modelo de capacidad + Medilink);
   integrarlo si se quiere que el copiloto razone sobre slots vacíos exactos.

## Tests

```bash
python3 tests/test_alma_brain.py   # 6 tests, sin red ni DB
```
