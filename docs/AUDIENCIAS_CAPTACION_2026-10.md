# Audiencias de captación en Meta (exclusión + semilla) — 2026-10-05

Estado: **código listo, sincronización APAGADA** (`META_AUDIENCIAS_CAPTACION_ACTIVE=false`).
No se creó nada en Meta. Código: `app/audiencias_captacion.py`. Job `audiencias_captacion_diario`
(04:30 CLT, `misfire_grace_time=3600`), inerte con el flag apagado. Tests:
`tests/test_audiencias_captacion_2026_10_05.py` (12).

## Qué hace

| Audiencia | Nombre en Meta | Contenido |
|---|---|---|
| Exclusión | `CMC Exclusion captacion 24m` | personas con pago en caja (`bi_pagos_caja`, monto>0) en 24 meses |
| Semilla | `CMC Semilla similar 18m` | cuartil superior de margen en 18 meses; margen = monto x (1 - pct_honorario/100), pct de `campanas_meta_routes._pct_honorarios` (0/ausente = 70) |
| Similar | `CMC Similar 1% Chile (semilla 18m)` | se crea solo si la semilla llega a 100 hashes |

- Teléfonos: `pacientes_heatmap.celular` (ficha Medilink), `citas_bot` (solo si `es_tercero=0`) y `contact_profiles` (cruce por RUT). Solo celulares chilenos válidos (569XXXXXXXX).
- Hash y mecanismo: `custom_audiences_sync` (`_sha256_phone`, `_meta_post`, `_buscar/_crear_audiencia`, `_crear_lookalike`, ahora con nombre/descripcion opcionales; el default no cambia). Solo viaja el SHA-256 del teléfono. Nombre y descripción no contienen especialidad, diagnóstico ni la palabra "paciente" (test).
- Reemplazo atómico por `/usersreplace` (quien sale de la lista sale de la audiencia).
- Una lista vacía o un BI caído no envía nada (nunca vacía una audiencia por una falla de lectura).

### Filtros (un teléfono entra solo si pasa todos)
1. Baja: `bi.opt_outs_marketing`, tag `marketing_opt_out` (incluye números reciclados), `bi.marketing_consent.declined`, `privacy_consents` revocado/rechazado, derecho al olvido (`gdpr_deletions`, por RUT y teléfono).
2. Teléfono compartido/tercero: mismo criterio de `winback._anotar_phone_compartido` (>=2 pacientes con el número, o el nombre que el bot conoce de ese WhatsApp no calza con la ficha). Citas agendadas a terceros no vinculan teléfono.
3. Menores de 18 (el teléfono es del apoderado).
4. Consentimiento: `bi.marketing_consent.status='accepted'`. **La semilla siempre lo exige.** La exclusión también por defecto (`META_AUDIENCIA_EXCLUSION_EXIGE_CONSENT=true`); en false queda solo con los filtros 1-3.

## Conteos reales (prod, 2026-10-05, solo números)

Pagos en caja: 8.726 pacientes en 24 meses (70 sin teléfono utilizable); 7.429 en 18 meses.

| Lista | Con consent exigido | Sin exigir consent |
|---|---|---|
| Exclusión (24m) | **213** teléfonos | **3.573** |
| Semilla top 25% (18m) | **52** (bajo el mínimo de 100) | 751 |
| Semilla top 50% | 103 | 1.502 |
| Semilla, todos los elegibles | 206 | 3.003 |

Descartes en la exclusión (por teléfono, en orden): 2.209 menores/olvidados, 903 compartidos, 118 bajas/declinados, 3.360 sin consent.

Lectura: con consentimiento exigido la **semilla NO alcanza el mínimo de Meta** (52 con el cuartil; 206 es todo el universo, y Meta empareja ~60-70%). Con flag encendido hoy, la semilla se omite (`bajo_minimo`) y solo subiría la exclusión de 213. Sin exigir consent hay tamaño de sobra, pero ver análisis legal.

## Análisis legal (no es asesoría jurídica: validar con abogado antes de encender)

Marco: Ley 19.628 vigente; Ley 21.719 publicada el 13-dic-2024, plena vigencia el 1-dic-2026 (confirmar fecha). El proyecto ya opera con el estándar de 21.719 (sprint win-back).

1. **Es dato de salud.** Un teléfono en una lista "personas atendidas en un centro médico" revela que la persona recibe atención de salud (dato sensible, 19.628 art. 2 g y art. 10; 21.719 lo trata como categoría especial). Con especialidad sería aún más grave: por eso los nombres son neutros. Pero el solo vínculo "cliente del CMC" ya es sensible; neutralizar el nombre reduce el riesgo, no lo elimina.
2. **El hash no anonimiza.** Meta lo empareja con los hashes de sus propios usuarios: es seudonimización, sigue siendo dato personal. Además Meta es un tercero que recibe el dato (transferencia internacional).
3. **¿Basta el consentimiento existente? No, a nuestro entender.**
   - `privacy_consents` (opt-in a la política del bot): cubre agendar y atender. El propio sprint win-back concluyó que ese opt-in NO cubre marketing saliente; con más razón no cubre entregar datos a una plataforma publicitaria (principio de finalidad).
   - `bi.marketing_consent.accepted` (591): el texto pide aceptar *recibir mensajes por WhatsApp*; no menciona compartir datos hasheados con Meta ni audiencias similares. Es el mejor consentimiento que tenemos, pero probablemente no es "específico" para este uso.
4. **¿La exclusión es interés legítimo?** Argumento plausible para dato común (evitar mostrar avisos de captación a quien ya es cliente, impacto mínimo para la persona). Débil acá: el dato es de salud, y en 21.719 el interés legítimo no es base válida para datos sensibles (verificar con abogado). Tampoco hay aviso previo de este uso en la política `/privacidad`.
5. **Condiciones de Meta.** Los términos de Business Tools prohíben enviar datos de salud/sensibles a Meta; un listado de clientes de un centro médico puede caer en eso y arriesgar la cuenta publicitaria. Revisar antes.
6. **Semilla = elaboración de perfil** (los mejores por margen): más invasiva que la exclusión. Por eso siempre exige consentimiento y excluye menores.

**Conclusión: el flag queda OFF.** Hay duda real en los tres puntos (dato sensible, alcance del consentimiento, términos de Meta). Para encenderlo, por orden de menor a mayor riesgo:
- Exclusión solo con consent aceptado (213 hoy; default del código): riesgo menor, pero igual falta que el texto del consent mencione audiencias publicitarias.
- Actualizar el texto de `consent_marketing` y `/privacidad` ("podemos usar tu teléfono cifrado con plataformas publicitarias para no mostrarte anuncios de captación o para encontrar personas con intereses parecidos"; con opción de baja), y recién ahí volver a medir los conteos: crecerán solo con respuestas nuevas.
- Alternativa sin entregar datos: excluir con señales propias de Meta (personas que ya escribieron al WhatsApp del CMC / interactuaron con la página) en vez de lista de pacientes.

## Hallazgos sobre `custom_audiences_sync` (existente, NO modificado salvo el nombre opcional del similar)

- El job diario 04:00 **no tiene flag** y exporta "CMC Atendidos 90d", "CMC No vuelven 365d" y "CMC Ortodoncia activos": solo filtra bajas (no exige consent) y **"Ortodoncia activos" revela especialidad** en el nombre. Contradice la regla de este trabajo; conviene revisarlo con el mismo criterio legal.
- `_replace_users` documenta REPLACE pero llama a `/users` (agrega): una baja posterior no saca al número de la audiencia. Las audiencias nuevas usan `/usersreplace`.
- Se verificó que ese job existe en `main.py` (`custom_audiences_sync_diario`) y se desconoce si Meta tiene hoy esas audiencias creadas: no se consultó la API.

## Qué hacer en Ads Manager (cuando el dueño apruebe y se encienda el flag)

1. Encender: `META_AUDIENCIAS_CAPTACION_ACTIVE=true` en `.env` del VPS + restart; revisar el log `job_audiencias_captacion` (al día siguiente 04:30 CLT, o ejecutar `sync_audiencias_captacion()` una vez a mano).
2. Audiencias > confirmar que aparecen `CMC Exclusion captacion 24m` y `CMC Semilla similar 18m` (estado "Lista"; puede tardar horas en poblarse).
3. En cada **ad set de captación**: Público > Excluir > Audiencias personalizadas > `CMC Exclusion captacion 24m`. No excluir en campañas de reactivación/fidelización.
4. Similar: si la semilla llegó a >=100 el job crea `CMC Similar 1% Chile (semilla 18m)`; si no, crearla a mano: Audiencias > Crear > Audiencia similar > origen `CMC Semilla similar 18m`, país Chile, 1%. Usarla como público en un ad set de captación nuevo (no tocar los existentes) y comparar costo por cita contra el actual.
5. Esperar que la exclusión rinda: con 213 teléfonos el efecto sobre la venta atribuida será pequeño; el valor real requiere ampliar el consentimiento.

## Pendiente
- Decisión legal (abogado) y de texto de consent.
- Decidir qué hacer con el job existente 04:00 (flag, nombre "Ortodoncia activos", consent).
