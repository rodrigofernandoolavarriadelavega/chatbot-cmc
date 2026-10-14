# Remuneraciones Pro — contrato backend ↔ frontend

Módulo Alma `/alma/remuneraciones` (key `remuneraciones`). Reemplaza en uso diario al
Nivel 4 del Panel del Día (que se mantiene intacto: no tocar sus endpoints
`/api/panel-dia/remuneracion*`).

## Objetivo del dueño
1. Pagar honorarios del mes a cada profesional.
2. Antes de pagar, ver que **tres fuentes calzan** y, si no, **dónde exactamente** no calza:
   - **Sistema** = espejo `bi_pagos_caja` (lo que hoy usa el cálculo, `ebitda_routes._ebitda_mes`).
   - **Medilink** = caja EN VIVO consultada día por día (`bi_sync._fetch_pagos_dia`), por `pago_id`.
   - **Módulo Pagos** = `pagos_cmc` (lo que teclea recepción; solo COPAGO; `id_profesional` existe).
3. Simulador (popup trabajable): cambiar %, sacar/meter profesionales, ajustes, ingresar
   saldo disponible → ¿me alcanza la plata?
4. Enviar a cada profesional su liquidación por WhatsApp (`wa.me` + copiar), con detalle.

## Reglas de negocio (no negociables)
- `equipo_cmc.pct_honorario` = lo que se lleva el PROFESIONAL. Margen centro = venta × (1−pct).
- Retención boleta 15,25% (`ebitda_routes.RETENCION_BOLETA`); `SIN_RETENCION` facturan.
- Abarca (73) y otros fijos: reusar exactamente la lógica de `_ebitda_mes` (no duplicar números).
- Medilink pagina de a 50 SIN ordenar; consultar SIEMPRE día por día, nunca por rango.
- 429 ≠ caída: reintentar con backoff; respetar intervalo entre llamadas (ver bi_sync).
- `pagos_cmc.bonificacion` está siempre en 0 → bonificación Fonasa se infiere por arancel
  (reusar `conciliacion_routes._bonif_desde_arancel` / `_ARANCEL_N3`).
- Nunca escribir en Medilink. Solo lectura de fuentes; escritura solo en tablas propias `remun_*`
  y en `remuneracion_estado` / `equipo_cmc` (pct/teléfono, como hoy).
- Auth igual que remuneraciones_routes (`_auth`: token admin o cookie).
- Front: sin CDN (fuentes/íconos locales o inline). Textos de la app en español formal;
  mensaje WhatsApp cordial y claro.

## Unidad de cruce: línea = (fecha, paciente, profesional)
Por línea, cada fuente aporta monto (o null). Estados (`tipo`):

| tipo | significa | afecta la base de pago |
|---|---|---|
| `ok` | las tres calzan (módulo: copago+bonif estimada ≈ caja, tolerancia por arancel) | — |
| `fantasma_sistema` | en espejo, NO en Medilink (anulado después del sync) | sí, baja |
| `falta_en_sistema` | en Medilink, no en espejo (sync atrasado) | sí, sube |
| `otro_profesional` | mismo pago_id, distinto id_profesional espejo vs Medilink | sí |
| `monto_distinto` | espejo ≠ Medilink en monto | sí |
| `sin_registro_modulo` | en caja, recepción no lo registró | no (alerta caja) |
| `solo_en_modulo` | recepción cobró, Medilink no tiene pago (atención no cerrada) | decisión del dueño (ver `_linea_cuenta_para_pago`) |
| `dif_modulo` | copago+bonif ≠ caja; agrupar patrones (p.ej. $630 Fonasa MG) | no (alerta) |
| `prof_distinto_modulo` | recepción lo cargó a otro profesional | no (alerta) |
| `ok_patron` | `dif_modulo` cuya diferencia (esperado, caja) se repite ≥5 veces idéntica en el mes (p.ej. +$630 Fonasa MG = recepción teclea la bonificación como copago; −$4.890 = posible tramo Fonasa). Se considera conciliada: NO enciende el badge, se lista agrupada en `patrones` | no |

Notas del motor (agregadas al construir el backend, 13-oct):
- **Placeholders**: filas de `pagos_cmc` con copago 0 y sin pago en caja (prellenadas, solo bonif. estimada) se excluyen del cruce en silencio. Filas con copago 0 y sin medio no entran nunca (igual que `_cruzar_caja`).
- **Badge rojo** (`conciliacion.n_dif`) = solo lo que mueve la base o es faltante real: `fantasma_sistema`, `falta_en_sistema`, `otro_profesional`, `monto_distinto`, `sin_registro_modulo`, `solo_en_modulo`, `prof_distinto_modulo`. `dif_modulo` puntual va en `conciliacion.n_alertas`; `ok_patron` en `conciliacion.n_ok_patron`.
- **`otro_profesional` mide deriva desde el sync**: Medilink /pagos NO trae profesional; el lado Medilink se atribuye con la misma regla del espejo (`bi_sync._resolver_profesional_pago`: corrección manual → recepción → atención). No es una atribución independiente y la explicación de la línea lo dice.
- Emparejamiento módulo↔caja: por `id_paciente` si ambos lo tienen; si no, `_sim_paciente` ≥0,66. Pasadas: mismo día y profesional → mismo día otro profesional (`prof_distinto_modulo`) → ±1 día mismo profesional.
- Una línea puede tener varios problemas: `tipo` = el de mayor prioridad; `tipos` = todos (orden de prioridad). Campos extra por línea: `id_profesional`, `tipos`, `verificado`, `dia_verificado`, `monto_pago` (lo que aporta a la base), `efecto_pago` (pago − sistema, como si el día verificado mandara), `otro_profesional_id`; `modulo` agrega `ids` y `prevision`.
- `GET /mes` agrega: `totales.{modulo_copago, modulo_bonif_est, total_ajustes, a_transferir, n_alertas, n_ok_patron, sin_profesional, n_pagados, n_profesionales}`, `patrones` del mes (`descripcion, causa, n, monto, diferencia_unitaria, esperado, caja, prevision`), por profesional `monto_fijo` y `conciliacion.{n_alertas, n_ok_patron, verificado_parcial}`; `verificado_medilink.{en_curso, dias_error}`. Estados de verificación: `sin_verificar | en_curso | parcial | ok` (solo `ok` cambia la base a Medilink).
- `GET /whatsapp` agrega `texto_resumen` (sin lista de atenciones, para cuando el texto completo es demasiado largo para wa.me), `largo` y `base_fuente`.
- `POST /verificar-medilink` responde 409 si ya hay un barrido corriendo (de cualquier mes); acepta `solo_pendientes: true` para reintentar solo los días fallidos.
- **Auth**: SOLO el token del dueño (OLACORE_TOKEN, query `token` o `Authorization: Bearer`), como Trayectoria/Campañas Meta. ADMIN_TOKEN (recepción) y la cookie `admin` reciben 403: este módulo muestra cuánto gana cada profesional.
- Ajustes: `monto` con signo (+ suma, − descuenta), se aplican sobre el líquido: `a_transferir = liquido + total_ajustes`. Motivo obligatorio.

Base de pago = Medilink en vivo si el mes está verificado; si no, sistema (y la UI lo dice).

## Endpoints (`app/remuneraciones_pro_routes.py`, prefijo `/api/remuneraciones`)
- `GET /mes?mes=YYYY-MM` → `{mes, verificado_medilink: {estado, at, dias_ok, dias_total},
  totales: {sistema, medilink, modulo_esperado, base_pago, bruto, liquido, pagado, por_pagar, n_dif, monto_dif_pago},
  profesionales: [{id, nombre, especialidad, pct, fijo, sin_retencion, telefono, pagado, pagado_at, monto_pagado,
    fuentes: {sistema:{monto,n}, medilink:{monto,n}|null, modulo:{copago,bonif_est,esperado,n}},
    conciliacion: {estado:'ok'|'diferencias'|'sin_verificar', n_ok, n_dif, monto_dif_pago, por_tipo:{tipo:{n,monto}}},
    liquidacion: {base, base_fuente, bruto, retencion, liquido, ajustes:[{id,monto,motivo}], total_ajustes, a_transferir}}]}`
- `GET /detalle?prof=&mes=` → `{lineas:[{fecha, paciente, id_paciente, sistema:{monto,pago_ids}|null,
  medilink:{monto,pago_ids,id_profesional}|null, modulo:{copago,bonif_est,esperado,metodo,id,id_profesional}|null,
  tipo, diferencia, explicacion, cuenta_para_pago}], patrones:[{descripcion,n,monto}]}`
- `POST /verificar-medilink {mes}` → arranca barrido en background; `GET /verificar-estado?mes=` → progreso.
  Cache en tabla `remun_medilink_pagos(mes, pago_id PK, fecha, id_profesional, id_paciente, nombre_paciente, monto, fetched_at)`.
- `POST /ajuste {prof, mes, monto, motivo}` / `DELETE /ajuste/{id}` → tabla `remun_ajustes`.
- `POST /pagado {prof, mes, pagado, monto_pagado}` → `remuneracion_estado` (+ columna monto_pagado).
- `POST /config {id, pct?, telefono?}` → igual que hoy.
- `GET /whatsapp?prof=&mes=` → `{telefono, texto}` (texto único fuente de verdad; el front lo previsualiza).
- `GET /alma/remuneraciones` → `templates/alma_remuneraciones.html`.

## Fixtures para desarrollo local
`/private/tmp/claude-501/-Users-rodrigoolavarria/e22b573e-da89-496a-8ed1-21ba6c381d37/scratchpad/remun_fixtures/`
- `mes_2026-09.json`, `detalle_<prof>_2026-09.json` con datos REALES de septiembre (fuera del repo).
- El front debe poder abrirse con `?fixtures=1` leyendo esos JSON vía un servidor local simple.

## Pedidos del front (13-oct, segunda ronda)
- `GET /mes` → por profesional `pct_actualizado_at` (= `equipo_cmc.updated_at`; ojo: se mueve con cualquier edición de la ficha, no solo del %) y `whatsapp_enviado_at` (último registro en `remun_envios`, o null).
- `patron_id` estable = `p<esperado>-<caja>` (p. ej. `p15760-15130`) en cada línea `ok_patron` (también como alias `patron`) y en cada item de `patrones` (también como alias `id`). `patrones[].monto` va CON signo (= `diferencia_unitaria × n`).
- `GET /whatsapp` → `texto` (completo), `texto_resumen` (sin lista; dice que el detalle va en el mensaje siguiente), `texto_detalle` (solo la lista: fecha · paciente · monto), `dos_mensajes` (largo > 1.800), `max_caracteres`, `whatsapp_enviado_at`.
- `POST /whatsapp-enviado {prof, mes}` → registra el envío (tabla `remun_envios(id, id_profesional, mes, canal, at)`); responde `{ok, whatsapp_enviado_at}`. No envía nada: el envío es en wa.me.
- `POST /linea-decision {prof, mes, clave_linea, incluir: true|false|null}` → tabla `remun_decisiones(mes, id_profesional, clave_linea, incluir, at)`. Solo líneas `solo_en_modulo` (400 si es otro tipo, 404 si la clave no existe). `null` borra la decisión. `_linea_cuenta_para_pago` la respeta ANTES de la regla default. Cada línea de `/detalle` trae `clave_linea` (`fecha|id_profesional|id:<id_paciente>` o `n:<nombre normalizado>`) y `decision` (null|true|false). Las decisiones con `id:<id_paciente>` entran al borrado por derecho al olvido.
- `POST /ajuste` → `{ok, id}`.

## FASE 2 — Atribución (14-oct): reporte de Medilink, repartos, detector, jefatura

**Por qué**: "Sistema = Medilink al peso" solo prueba que los pagos EXISTEN; la atribución usa la misma
regla en ambos lados (`bi_sync._resolver_profesional_pago`). Caso real Armijo (77) sep-2026: reporte UI
$935.250 (70 pagos) vs módulo $953.990 = pago 39968 sin aplicar ($30.130, atención 58037 editada a $0) +
pago 40457 partido ($26.520 = $15.130 Márquez + $11.390 Armijo, el espejo lo cargaba entero a Márquez).
La API no expone pago→tratamiento. La UI separa **caja verificada** (`verificado_medilink`) de
**atribución verificada** (`profesional.verificacion.atribucion`).

Tipos nuevos (todos afectan el pago, badge rojo): `sin_aplicar`, `partido`, `solo_en_reporte`,
`posible_exceso`; y `reporte_ok` (calza con el reporte, verde). Prioridad: atribución > caja > recepción.

Endpoints:
- `POST /reporte-medilink {prof, mes, texto, guardar?}` → parsea el texto copiado de "Pagos período de"
  (tabs o espacios, `$935.250`, fechas `30 Sep 2026` es/en, encabezados, línea Total). Valida suma de
  filas = Total (400 con mensaje claro), sin duplicados, todas las fechas en el mes. `guardar:false` =
  previsualizar (`filas, n, total, base_actual, diferencia_vs_base, advertencias`). Guardar responde
  `base_antes, base_ahora, por_tipo, repartos_propuestos`. Tablas `remun_reporte_cab` +
  `remun_reporte_medilink(mes, id_profesional, pago_id, id_atencion, fecha, monto, medio)` — SIN nombres
  ni id_paciente (no requiere cascada).
- `DELETE /reporte-medilink?prof=&mes=`.
- Con reporte cargado la base del profesional = total del reporte (`base_fuente='reporte_medilink'`);
  la regla `_linea_cuenta_para_pago` lo respeta después de la decisión por línea y antes del default.
  A los otros profesionales que el espejo cargaba con parte de un pago del reporte: `posible_exceso`
  ("pegue su reporte para confirmar"), con `efecto_pago` negativo.
- Repartos (solo remuneraciones; NO toca `bi_pagos_caja` ni `bi_pago_overrides`): tabla
  `remun_reparto(pago_id, id_profesional, monto, origen reporte|manual|detector, confirmado, at)`;
  `id_profesional 0` = sin asignar (saldo a favor). `GET /repartos?mes=` → `{propuestos, confirmados}`;
  `POST /reparto {pago_id, partes:[{id_profesional, monto}], origen?}` (Σ ≤ monto del pago; reemplaza);
  `DELETE /reparto/{pago_id}`. La liquidación usa el reparto confirmado por encima del espejo (el pago se
  parte en filas por profesional en sistema y Medilink).
- `GET /mes` agrega `repartos_propuestos`, por profesional `verificacion {caja, atribucion}`,
  `atribucion {estado, total_reporte, n_reporte, cargado_at, cuadra}`, `conciliacion.n_atribucion`,
  `liquidacion.{bruto_propio, jefatura[], total_jefatura}`; totales `n_atribucion, n_reportes,
  n_repartos_propuestos, n_repartos_confirmados`. `GET /detalle` agrega `reporte`, `verificacion`,
  `repartos_propuestos`, `repartos_confirmados`; cada línea `reporte {monto, pago_ids, id_atencion}|null`,
  `reporte_cargado`, `propuesta`, `reparto`, `pagos_linea [{pago_id, monto, atencion_id}]`.

Detector automático (en cada GET /mes, local):
- **Partido probable**: monto del pago = arancel de un área + arancel de otra (reusa
  `bi_sync._tabla_monto_area` y `_AREA_POR_PROF`, la lógica de "pago partido por suma de aranceles" de
  `auditar_atribucion_pagos`), el paciente tuvo cita Atendido / atención ese día con ambos profesionales,
  solución única, pago sin override manual → propone reparto (origen `detector`). Sep-2026: 2 casos
  (40457 y 39386).
- **Sin aplicar probable**: la atención que el espejo asocia al pago tiene total $0 EN VIVO
  (`remun_atencion_check`, llenada por el barrido con `GET /atenciones/{id}` SOLO para atenciones que
  `bi_atenciones` muestra en $0, ≥4 s y backoff 429, tope 250; `POST /verificar-medilink
  {mes, solo_atenciones:true}` corre solo esa parte). Es PROBABLE: el `atencion_id` del espejo es la
  elección del matcher (39799 y 39968 apuntan ambos a la atención 58037; solo 39968 quedó sin aplicar),
  por eso no propone reparto: lo resuelve el reporte pegado. `bi_atenciones.total` no sirve solo: es una
  foto del día siguiente a la atención (155 pagos de sep apuntan a atenciones en $0).

**Comisión de jefatura** (decisión del dueño 14-oct): `ebitda_routes.COMISION_JEFATURA = {21: [(77, 0.10,
"Jefatura kinesiología")]}` y `comision_jefatura()` = regla única usada por `_ebitda_mes` y Remuneraciones.
Etcheverry cobra su % + 10% de la venta de Luis; lo paga el centro (a Luis no se le descuenta); suma al
bruto de 21 con su retención normal. Base de Luis = la misma con que se le paga (reporte / reparto /
espejo; en EBITDA, su caja). Liquidación, impresión, WhatsApp y simulador muestran la línea "Jefatura
kinesiología: 10% de la venta de Luis Armijo ($base) = $X"; el simulador la recalcula si se saca a Luis.

**Pasos de deploy de la fase 2**:
1. Desplegar el código (rutas nuevas, `ebitda_routes` con jefatura).
2. EN EL MISMO deploy: `UPDATE equipo_cmc SET pct_honorario=50, updated_at=datetime('now') WHERE
   id_medilink=21` (hoy 52, aproximación vieja; con la jefatura pasa a 50). 77 ya está en 50.
3. Correr una vez `POST /verificar-medilink {mes:'2026-09', solo_atenciones:true}` fuera de horario punta.
4. Avisar: `/cmc/mensual` (cmc_mensual.html) calcula honorarios en JS por su cuenta (semillas ORIGINAL,
   "Kine Leo" 50%) y NO conoce la jefatura ni los repartos: su honorario de 21 no calzará con EBITDA.
