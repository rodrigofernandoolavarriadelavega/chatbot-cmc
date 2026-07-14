# El Libro de la Verdad del CMC

**Módulo:** `app/verdad.py` — una función por pregunta de negocio, cada una
documenta su fuente en el docstring y devuelve el dato + su procedencia
(`_fuente`, `_periodo`, `_n_filas`, `_advertencias`, `_no_responde`).

**Por qué existe este documento.** `sessions.db` acumuló 16 tablas donde vive
"atenciones" o "plata" (`bi_atenciones`, `bi_pagos_caja`, `pagos_cmc`,
`caja_diaria`, `olavarria_atenciones_cache`, `olavarria_bi_ingresos`,
`egresos_cmc`, …), más Medilink en vivo, el BI en Postgres
(`health-bi-project`) y `heatmap_cache.db`. Cada dashboard nuevo eligió su
propia fuente sobre la marcha — por eso los números "nunca cuadraban a la
primera". Esta página es la referencia única: para cada pregunta frecuente,
qué fuente usar y por qué, y qué NO usar nunca y por qué. Sirve para un
humano (el dueño, recepción) y para el próximo agente que toque este código.

Regla de oro: **si la pregunta ya tiene una función en `app/verdad.py`, se usa
esa función.** No se escribe un SELECT nuevo contra `bi_pagos_caja` /
`pagos_cmc` / `bi_atenciones` para responderla de nuevo.

---

## 1. Tabla: qué fuente usar para cada pregunta

| Pregunta | Función en `verdad.py` | Fuente | NO usar (y por qué) |
|---|---|---|---|
| ¿Cuánto vendió el CMC en un período? | `venta_total(desde, hasta)` | `bi_pagos_caja` (caja real, sync cada 30 min desde `/api/v5/pagos`) | `bi_atenciones.total` (facturado, no cobrado) · `pagos_cmc` (subcobertura, solo copago tecleado) · `olavarria_bi_ingresos`/BI Postgres (sobreestima ~15%) |
| ¿Cuánto produjo cada médico/profesional? | `plata_por_profesional(desde, hasta)` | `bi_pagos_caja.id_profesional` | BI Postgres `bi.fact_pagos` (**vacío**, `ingreso_id` NULL 100%) |
| ¿Cuántas consultas hubo de verdad? | `atenciones(desde, hasta, profesional=)` | `bi_pagos_caja`, `COUNT(*)` (cada pago = una visita) | `citas_bot.estado` / `bi_atenciones.finalizado` = "Atendido" (es un trámite, no un hecho; esconde las 569 atenciones nunca cerradas) |
| ¿Cuántos pacientes distintos pagaron? | `pacientes_distintos(desde, hasta, profesional=)` | `bi_pagos_caja`, `COUNT(DISTINCT id_paciente)` | — |
| ¿Cómo se distribuye el medio de pago (efectivo/tarjeta/transferencia)? | `mix_medios_de_pago(desde, hasta)` | `pagos_cmc.metodo_pago` | `bi_pagos_caja.metodo_pago` (99,7% dice "Efectivo" por default de Medilink, nadie lo cambia — no mide nada real) |
| ¿Cuánto copago se cobró, por previsión? | `copagos(desde, hasta, prevision=)` | `pagos_cmc` (copago, bonificacion, prevision) | — (recordar: cobertura parcial, no es venta total) |
| ¿Cuál fue el mejor mes histórico de un profesional? | `mejor_mes(profesional)` | `bi_pagos_caja`, agregado por mes | — |
| ¿Qué tan confiable es el vínculo pago↔atención? | `tasa_orfandad(desde, hasta, profesional=)` / `tasa_orfandad_evolucion()` | `bi_pagos_caja.atencion_id` | — (esta es la métrica de salud del propio dato) |
| ¿Cuánto gastó el CMC (operativo)? | `egresos(desde, hasta)` | `egresos_cmc` | — |
| ¿Está fresco el dato de hoy? | `sync_status()` | `bi_sync_log` | — |
| ¿Cuánto es el EBITDA / honorario líquido de un mes? | *(no está en verdad.py)* | `ebitda_routes._ebitda_mes` — ya usa `bi_pagos_caja` correctamente, aplica % de honorario (`equipo_cmc.pct_honorario`), retención de boleta 15,25%, contratos fijos y gastos (`egresos_cmc`) | No reimplementar esta lógica en otro lado — es una regla de negocio, no un hecho crudo |

---

## 2. Qué NO usar nunca, y por qué (detalle)

Esta lista vive también como `verdad.TABLAS_NO_USAR` en el código, para que
quien programe pueda importarla y no tenga que copiar texto de acá.

- **`bi_atenciones.total` / `abonado`** — es lo FACTURADO, no lo cobrado.
  Incluye atenciones con `total=0` que nunca se cerraron: **569 atenciones
  "nunca cerradas" en 180 días medidos (2026-07-12)**, `finalizado=0`,
  ≈$25,5M/año de fuga invisible (el ETL del BI las descarta en silencio, ver
  `health-bi-project/etl/transform.py:307`). `abonado` además llega en 0 en
  casi toda la respuesta JSON de Medilink — no es confiable.
- **`citas_bot.estado` / `bi_atenciones.finalizado` para contar "cuántos
  pacientes se atendieron"** — el estado de la cita es un TRÁMITE
  administrativo, no un hecho. Regla dura del dueño: *"si el paciente pagó,
  fue atendido"*. La caja es el hecho.
- **`pagos_cmc` para venta total** — solo cubre lo que recepción tecleó a
  mano en el módulo Pagos de Alma. 9.797 filas históricas contra 36.779 de
  `bi_pagos_caja` en el mismo rango — subcobertura estructural, no un error
  puntual. Sirve exclusivamente para **medio de pago** y **copago**.
- **`bi_pagos_caja.metodo_pago` para el mix de medios de pago** — 99,7% de
  36.779 filas dicen "Efectivo" porque nadie cambia el valor por defecto al
  cargar el pago en Medilink. No es que se cobre todo en efectivo: es que el
  campo no se usa. Solo 4 filas de débito y 0 de crédito en TODA la
  historia. Usar `pagos_cmc.metodo_pago`.
- **`olavarria_atenciones_cache`** — cache local vía
  `/citas?estado_cita=atendido&id_profesional=1`. Subestima ~22% porque
  filtrar por estado no captura todo lo que `/atenciones` sí ve. Solo usar
  como fallback si `bi_pagos_caja` no responde (Medilink caído).
- **`olavarria_bi_ingresos` / `bi.fact_ingresos` (BI Postgres)** — snapshot
  histórico (corte 2026-04) descargado del proyecto `health-bi-project` vía
  `/atenciones`. Sobreestima ~15% frente a la caja real (atenciones
  registradas pero no cobradas). **El factor de corrección 0.85 se aplica
  ÚNICAMENTE a esta tabla** (o a `bi.fact_ingresos` en Postgres) — NUNCA a
  `bi_pagos_caja`, que ya es caja real y no necesita corrección. Aplicar 0.85
  sobre `bi_pagos_caja` sería restar plata real que sí entró.
- **`bi.fact_pagos` (Postgres, `health-bi-project`)** — está **VACÍO**
  (`ingreso_id` NULL en el 100% de las filas). Invalida cualquier análisis
  de "brecha facturado vs. cobrado" o "desglose por profesional" que se
  hubiera basado en esta tabla (una nota de memoria vieja decía que el
  desglose por profesional "no se podía hacer" por esto — es falso: en
  `bi_pagos_caja`, tabla distinta en `sessions.db`, sí está poblado).
- **`caja_diaria`** — registro manual de efectivo físico a depositar (cuadre
  de caja chica del día a día), no venta. Sirve para conciliar depósitos
  bancarios, no para medir ingresos.

---

## 3. La anomalía real: orfandad pago↔atención (medida 2026-07-14)

**Pregunta:** ¿qué porcentaje de los pagos de `bi_pagos_caja` NO tiene un
`atencion_id` vinculado a una atención real de Medilink?

**No es un problema de un profesional. Es un salto CMC-completo, reciente y
que se está acelerando:**

| Mes | Pagos | Sin `atencion_id` | Tasa |
|---|---|---|---|
| 2026-01 | 797 | 0 | 0,0% |
| 2026-02 | 726 | 0 | 0,0% |
| 2026-03 | 1.058 | 0 | 0,0% |
| 2026-04 | 1.005 | 0 | 0,0% |
| 2026-05 | 1.144 | 101 | 8,8% |
| 2026-06 | 1.264 | 1.100 | **87,0%** |
| 2026-07 (parcial, al 14) | 531 | 493 | **92,8%** |

Histórico agregado completo (2020-2026): 36.779 pagos, 1.694 huérfanos =
**4,6%** — el promedio global esconde que la fuga es reciente y está
concentrada en los últimos 2-3 meses.

Por profesional en junio 2026 (los de mayor volumen, ninguno se salva):

| Profesional | Pagos | Huérfanos | Tasa |
|---|---|---|---|
| Dr. Rodrigo Olavarría (1) | 435 | 396 | 91,0% |
| Dr. Alonso Márquez (13) | 154 | 144 | 93,5% |
| Dr. Andrés Abarca (73) | 178 | 144 | 80,9% |
| Leonardo Etcheverry — kine (21) | 141 | 124 | 87,9% |
| David Pardo — ecografía (68) | 51 | 37 | 72,5% |
| Dra. Javiera Burgos — dental (55) | 48 | 37 | 77,1% |

Evolución específica de Abarca (el caso que motivó la medición original):

| Mes | Pacientes distintos | `atencion_id` distintos | Pagos | Tasa orfandad |
|---|---|---|---|---|
| 2025-11 (mejor mes histórico) | 331 | 352 (100%) | 352 | 0,0% |
| 2026-04 | 147 | 155 (100%) | 156 | 0,0% |
| 2026-05 | 155 | 130 | 159 | 18,2% |
| 2026-06 | 166 | 34 | 178 | 80,9% |
| 2026-07 (parcial) | 53 | 7 | 59 | 88,1% |

**El monto sigue siendo correcto** (`venta_total` y `plata_por_profesional`
no dependen de `atencion_id`, se basan en el pago). Lo que se pierde es la
trazabilidad pago → ficha clínica específica en Medilink. Si la tendencia
sigue, en unos meses no va a ser posible auditar cuánto produjo cada médico
CONTRA SU FICHA CLÍNICA — solo cuánto entró en caja a su nombre (que ya es
mucho, pero no es lo mismo).

### Causa raíz identificada (lectura de código — no arreglada, no confirmada verbalmente con el autor)

El commit `b090e9c0` (2026-06-12, *"fix cruce pago→profesional usa pagos_cmc
como nivel 0.5 + heurística bi_atenciones mejorada"*) agregó en
`app/bi_sync.py::_resolver_profesional_pago` un **NIVEL 0.5**: cuando el pago
matchea de forma inequívoca contra `pagos_cmc` (recepción registró un único
profesional para ese paciente+fecha), la función hace:

```python
if len(profs_cmc) == 1:
    return profs_cmc.pop(), None   # ← retorna YA, atencion_id = None
```

El propio comentario del código dice *"atencion_id lo deja None aquí; la
heurística existente en el bloque siguiente puede completarlo"* — pero el
`return` corta la ejecución ahí mismo. La cascada heurística que sí busca
`atencion_id` contra `bi_atenciones` (match por monto/fecha/deuda) vive en el
bloque de abajo y **nunca se alcanza** cuando el nivel 0.5 resuelve.

Ese commit **sí arregló el bug real que buscaba resolver** — pagos cruzados
al profesional equivocado cuando un paciente tenía atenciones con varios
profesionales el mismo día (ver memoria
`cmc-comparador-y-atribucion-2026-06-12`: caso de 4 pagos de $35k de Quijano
donde 2 quedaban mal atribuidos). El costo fue un efecto secundario no
buscado: **cuanto mejor registra recepción en `pagos_cmc` (que es la
tendencia, no una regresión), más pagos entran por el nivel 0.5 y más
orfandad de `atencion_id` se genera.** La curva de la tabla de arriba (0% →
9% → 87% → 93%) es consistente con esa mecánica y con la fecha del commit.

**Recomendación (no ejecutada por este agente — reportar y sugerir invocar
`cmc-bot-engineer`):** en el nivel 0.5, mantener el `id_profesional`
resuelto pero NO retornar de inmediato — seguir con la cascada heurística de
`atencion_id` ya existente, filtrada al profesional recién confirmado (para
no reabrir el bug que el commit arregló). Es un cambio acotado a una función
de `bi_sync.py`, pero toca el corazón del cruce pago→atención y merece los
mismos tests que se le hicieron al fix original.

**Medición reproducible:** `verdad.tasa_orfandad_evolucion()` y
`verdad.tasa_orfandad(desde, hasta, profesional=)`.

---

## 4. Dashboards migrados y deuda pendiente

### Migrado

- **`/cmc/comparador`** (`app/comparador_routes.py::_datos_rango`) — el
  desglose por profesional/área (antes un `SELECT ... GROUP BY
  id_profesional` propio) ahora llama a `verdad.plata_por_profesional()`.
  **Verificado**: se comparó la salida completa (`dict ==`) de la versión
  vieja contra la nueva para 4 rangos distintos (junio 2026 por área y por
  profesional, noviembre 2025 por profesional, y todo el histórico
  2020-2026 por área) contra la base de datos real de producción —
  **idéntica en los cuatro casos**. La serie diaria del gráfico (que no es
  una "pregunta de negocio" canónica) se dejó con su query propia.

### NO migrados (deuda pendiente, con riesgo)

| Dashboard/módulo | Por qué no se migró | Riesgo de dejarlo así |
|---|---|---|
| `ebitda_routes._ebitda_mes` (`/cmc/ebitda`, y consumido por `panel_dia_routes` y `remuneraciones_routes`) | Recibe un cursor `c` YA ABIERTO y lo reusa en un loop de hasta 12 iteraciones (`evolucion`, un mes por iteración) más `_comision_transbank`/`_gastos_mes`. Las funciones de `verdad.py` abren su propia conexión (`with db() as c:`) por diseño — migrar este call site multiplicaría por 12 las conexiones SQLite abiertas por request, sin necesidad. Migrar bien requiere primero extender `verdad.py` para aceptar un cursor externo opcional (`c=None`), y no se hizo en esta pasada para no tocar un módulo ya validado sin ese refuerzo. | Bajo-medio: la query que sí tiene (`SUM(monto)/COUNT(DISTINCT id_paciente) GROUP BY id_profesional AND id_profesional IS NOT NULL`) es idéntica en espíritu a `plata_por_profesional(..., incluir_sin_asignar=False)` — mientras ambas quedan sincronizadas a mano, coinciden; si alguien cambia una sin la otra, divergen en silencio. |
| `panel_dia_routes.py` (varios: `panel_financiero`, `panel_operativo`) | Mismo patrón de cursor compartido a través de un request con muchas queries secuenciales (agenda del día, ticket promedio 90d, capacidad por percentil 120d, conciliación medilink vs recepción). La conciliación específica (`SUM(bi_pagos_caja)` vs `SUM(pagos_cmc.copago)` por mes) es exactamente `venta_total()` + `mix_medios_de_pago()`, pero migrar solo esas dos líneas sin resolver el problema de conexión del resto del endpoint deja el archivo con dos patrones distintos a la vez — se prefirió no migrar a medias. | Medio: es el endpoint que más veces recalcula variantes de la misma agregación (ticket promedio, capacidad por percentil) — el que más se beneficiaría de una function única, y el que más riesgo tiene de quedar con una query vieja si `bi_pagos_caja` cambia de forma. |
| `roas_routes.py` (`_attribution`, paso 3: `id_paciente → SUM(monto)`) | Query angosta con `id_paciente IN (...)` sobre una lista dinámica de pacientes atribuidos a anuncios — no calza con la forma `(desde, hasta[, profesional])` de `venta_total`/`plata_por_profesional`. Necesitaría una función nueva (`venta_por_pacientes(ids, desde)`) que no se agregó para no ampliar la superficie sin un caso de uso adicional que la use. | Bajo: consulta acotada, ya bien documentada in situ ("SIEMPRE caja real, nunca bi_atenciones"), y de solo un call site. |
| `remuneraciones_routes.py` | Mismo patrón de cursor compartido que ebitda; además ya declara explícitamente que reusa `ebitda_routes._ebitda_mes` para la parte financiera — migrar acá sin migrar ebitda primero sería inconsistente. | Bajo: ya delega a un único punto (`_ebitda_mes`), no duplica el SELECT. |
| `panel_dia_jobs.py` (capacidad por percentil, `bi_pagos_caja` agregada por día) | Cálculo derivado (percentil de pacientes-distintos/día en ventana de 120 días) que no es una "pregunta de negocio" cruda sino un proxy estadístico — no es del tipo de las funciones de `verdad.py`. | Ninguno adicional — es un cálculo legítimamente distinto, no una fuente duplicada. |
| `caja_helper.py::caja_visitas` | Ya sigue el mismo principio ("bi_pagos_caja como fuente fresca", cada pago = una visita) y lo declara en su propio docstring; es usado por módulos de "pacientes en control" (kine, ortodoncia) que necesitan además identidad del paciente (nombre/teléfono/comuna) vía `bi.dim_paciente` — funcionalidad que `verdad.py` no cubre (es identidad, no un hecho de caja). Se dejó intacto por ser un helper ya alineado con la misma filosofía, aunque con una firma distinta (recibe lista de profesionales + ventana en meses). | Ninguno — ya es la fuente correcta, solo con una interfaz distinta orientada a otro consumidor. |

**Nota de diseño para el próximo agente que quiera migrar los pendientes de
cursor compartido:** la forma más segura de desbloquearlos es agregar un
parámetro `c=None` a cada función pública de `verdad.py` (si viene `None`,
abre su propia conexión con `with db() as c:`; si viene un cursor ya
abierto, lo usa directo sin abrir ni cerrar nada). Es un cambio mecánico
pero hay que re-correr la misma batería de verificación de equivalencia
(`dict ==` contra la versión vieja, con datos reales) para cada call site
que se migre.

---

## 5. Ningún dashboard mostraba un número equivocado

Se revisó cada módulo listado arriba comparando su query contra
`verdad.py` antes de decidir qué migrar. **Ninguno estaba usando la fuente
incorrecta** — todos (comparador, ebitda, roas, panel del día,
remuneraciones, caja_helper) ya seguían la regla aprendida "venta =
`bi_pagos_caja`, medio de pago = `pagos_cmc`" documentada en la memoria del
dueño (`cmc_ventas_fuente_fiel`, `cmc_fuentes_de_venta_y_pago`). El problema
real no era "fuente equivocada" sino **duplicación**: 5+ variantes del mismo
`SELECT ... GROUP BY id_profesional FROM bi_pagos_caja` viviendo en 5+
archivos, cada uno libre de divergir con el tiempo. `verdad.py` no corrige
ningún número — corrige que existan copias.

Lo único que si cambia una cifra visible es indirecto: si en el futuro
alguien arregla la orfandad de `atencion_id` (sección 3) tocando
`bi_sync.py`, **el total de venta y el desglose por profesional NO
cambian** (no dependen de `atencion_id`) — pero cualquier reporte que empiece
a cruzar pago↔atención (por ejemplo, para auditar clínicamente qué se
facturó en cada consulta) empezará a funcionar de nuevo donde hoy no puede.

---

## 6. Cómo se validó `app/verdad.py`

Contra datos reales de producción (solo lectura, vía SSH, módulo copiado a
un archivo temporal en el servidor y borrado después de cada corrida — nunca
importado desde `main.py` ni expuesto como endpoint):

- `venta_total("2026-06-01", "2026-06-30")` → **$26.039.530**, 1.264 pagos.
  Coincide con la cifra que el dueño confirmó explícitamente
  ("$26.004.530" en memoria — la reconciliación exacta a la fecha de esa
  nota fue con datos parciales; el corte completo del mes cerrado da
  $26.039.530, ver memoria `cmc-fuentes-de-venta-y-pago`).
- `mejor_mes(73)` (Dr. Abarca) → **2025-11, $5.620.090, 331 pacientes
  distintos, 352 pagos** — coincide exactamente con la cifra de referencia
  del enunciado de esta tarea.
- `mix_medios_de_pago`, `copagos`, `egresos`, `sync_status`, `atenciones`,
  `tasa_orfandad_evolucion` — corridos y revisados manualmente contra
  `pagos_cmc`/`egresos_cmc`/`bi_sync_log` reales (ver sección 3 para los
  números de orfandad).
- `comparador_routes._datos_rango` (antes/después de migrar) — comparación
  `dict == dict` byte a byte contra 4 rangos reales, **idéntica** en los
  cuatro.

`app/verdad.py::_self_check()` (ejecutable con
`SQLCIPHER_KEY=... venv/bin/python3 app/verdad.py` en el servidor, donde
vive la base real) automatiza los dos primeros chequeos y falla con
`SystemExit(1)` si alguna cifra deja de coincidir — pensado para correr
después de cualquier cambio futuro al módulo o a `bi_sync.py`.

---

## 7. Estado de despliegue

`app/verdad.py` y el cambio en `app/comparador_routes.py` están en el
working tree local (`~/chatbot-cmc`), **sin commit y sin deploy** —
siguiendo la regla de esta tarea de no hacer `git commit`. Antes de
desplegar: revisar el diff, `git add` selectivo (hay WIP de otra sesión en
`app/flows.py`/`app/main.py`/`app/pagos_routes.py`/`templates/alma_pagos.html`
— no tocar), commit, y `scripts/deploy.sh` normal (G1-G4 + rollback
automático).
