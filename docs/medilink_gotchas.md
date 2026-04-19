# Medilink — reglas contraintuitivas (LEER antes de tocar slots/horarios/citas)

Este archivo recopila las trampas no obvias de la API Medilink 2. Si razonas sobre slots, horarios, cupos o citas sin pasar por aquí, vas a re-descubrirlas a mano — ya nos pasó.

## 1. Intervalo: el bot IGNORA el intervalo de Medilink

Medilink devuelve slots de 5–10 min (bloques flexibles para recepcionistas). El bot debe agrupar esos slots en ventanas del intervalo definido en el dict `PROFESIONALES` de `app/medilink.py`.

- Ecografía (prof 68): Medilink intervalo=5 min, bot intervalo=15 min → agrupa 3 slots contiguos.
- Odontología Javiera (prof 55): Medilink intervalo=60, bot también 60.
- Siempre que consultes `/especialidades/{id}/proxima` para notificar/ofrecer, pasa por agrupación.

## 1b. El JSON de `/horarios` incluye `hora_inicio_break` y `hora_fin_break`

Cada día de trabajo viene como:
```json
{"dia":1,"nombre_dia":"Lunes","hora_inicio":"09:40:00","hora_fin":"18:00:00",
 "hora_inicio_break":"13:00:00","hora_fin_break":"14:00:00"}
```

Si `hora_inicio_break != hora_fin_break`, el profesional tiene **pausa/almuerzo**. Cualquier slot que se solape con esa ventana (total o parcial) es rechazado por Medilink al crear la cita con:

> `"Profesional no tiene horario para la fecha y duración de cita solicitadas"`

Caso real (2026-04-19): Leonardo Etcheverry lun 09:40-18:00 con break 13:00-14:00. El bot ofrecía 13:40 porque ignoraba el break. Paciente confirmaba → `crear_cita` fallaba 400 → bot mandaba "llama a recepción".

Fix ya aplicado en `_get_horario` y `_generar_slots_horario`: se respeta el break. **Si escribes scripts one-shot (waitlist, notificación manual)**: también agrupa ventanas excluyendo el break del profesional — consultar `/profesionales/{id}/horarios` y leer `hora_inicio_break`/`hora_fin_break`.

## 1c. Slot ofrecido debe NO solaparse con ninguna cita existente, no solo compartir hora_inicio

`_get_horas_ocupadas` expande cada cita en bloques de 5 min (una cita 19:10-19:50 añade 19:10, 19:15, ..., 19:45 al set). Al validar un slot, no alcanza con `hora_inicio ∈ ocupadas` — hay que chequear **todo el rango** del slot.

Caso real (2026-04-19): Luis Armijo tenía 18:00-18:40 (Deyanira) y 19:10-19:50 (Steeve). El bot ofreció 18:40-19:20 como libre (hora_inicio 18:40 no estaba ocupada), pero el slot se solapa con Steeve → Medilink 400 `"Profesional tiene tope con otra cita"`.

Fix aplicado: `_slot_libre_vs_ocupadas(hi, hf, ocupadas)` recorre cada bloque de 5 min entre hi y hf. Se usa en `_slots_para_fecha` antes de marcar slot como libre.

## 2. `/profesionales/{id}/horarios` puede estar vacío y NO significa "no trabaja"

El campo `hora_inicio == hora_fin` (p.ej. 08:00-08:00) en todos los `dias` = ventana cero. Esto no significa que el profesional no atienda — significa que la recepción no publicó horario base y crea citas "a mano". En ese caso:

- `buscar_slots_dia` devolverá vacío (depende del horario base).
- **Fallback**: `/especialidades/{id}/proxima` sí devuelve cupos reales, independiente del horario base. Úsalo para waitlist y disponibilidad.
- David Pardo (ecografía, prof 68) es el caso típico.

## 3. Cancelación de citas

`PUT /citas/{id}` con body **`{"id_estado": 1}`**.

NO uses `{"estado_anulacion": 1}` solo — retorna `Undefined index`.

## 4. Creación de citas requiere `duracion`

`POST /citas` necesita campo `duracion` (minutos) = `hora_fin - hora_inicio` en minutos. Sin ese campo, la creación falla silenciosa o devuelve error poco claro.

## 5. Formatos de fecha

- **Respuesta de Medilink**: `DD/MM/YYYY`.
- **Entrada del bot internamente / SQLite**: `YYYY-MM-DD`.
- No confundir ni asumir equivalencia — convertir explícitamente al cruzar la frontera.
- La fecha en la respuesta viene del API, no asumas que coincide con la fecha de consulta (edge cases de TZ).

## 6. Rate limit 429

- ~20 req/min duro. El bot tiene semáforo de 4 concurrentes + resilience.py (circuit breaker + cola).
- **No cachees "horario vacío" de un 429** — `medilink.py` hace esto bien hoy; cuidar en refactors.
- El fan-out en `/admin/api/agenda-dia` fue el que tiró el rate limit el 18-abr-2026 (20 profs × 1 req). Está desactivado en `admin_routes.py`.

## 7. Autenticación

Header `Authorization: Token <valor>` — **NO** `Bearer`. Error silencioso si te equivocas.

## 8. 404 en `/profesionales/{id}/horarios`

Algunos IDs válidos devuelven 404 ahí. No asumas "no existe". Para saber si un profesional tiene agenda, consulta `/agendas` o `/citas` con su ID.

## 9. Zona horaria

Servidor corre en UTC pero Chile es America/Santiago. `medilink.py` usa `ZoneInfo("America/Santiago")` para calcular "hoy". Si sumas TZ tú mismo, verifica DST (sept y abril).

## 10. Mapa `ESPECIALIDADES_ID` en `medilink.py`

Diccionario keyword → id_esp Medilink. Cuando sumes una especialidad nueva, actualízalo acá. Ecografía = 13, medicina general = 10, etc.

---

**Si tocas este archivo, actualiza también** `CLAUDE.md` si cambia la cita o referencia.
