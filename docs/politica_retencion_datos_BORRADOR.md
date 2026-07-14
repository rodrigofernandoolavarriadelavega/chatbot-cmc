# Política de Retención de Datos Personales

## Chatbot WhatsApp + Portal del Paciente — Centro Médico Carampangue

> **BORRADOR — PROPUESTA — REQUIERE DECISIÓN DEL RESPONSABLE DEL TRATAMIENTO.**
> Ningún plazo de este documento está aprobado ni en ejecución (salvo lo marcado
> "IMPLEMENTADO"). Este documento implementa la medida **P1/G2 "Retención
> indefinida"** del plan de acción de la EIPD (`docs/EIPD_portal_paciente_BORRADOR.md`,
> §7.2 y §10 acción 2). Debe ser revisado por asesor legal y aprobado por el
> responsable antes de aplicar cualquier purga.

| Campo | Valor |
|---|---|
| Sistema | Chatbot WhatsApp + Portal del Paciente (`agentecmc.cl`), FastAPI + SQLite `data/sessions.db` (SQLCipher si `SQLCIPHER_KEY` está definida) |
| Responsable del tratamiento | Centro Médico Carampangue (CMC), Carampangue, comuna de Arauco |
| Marco legal | Ley N.º 19.628 en el texto fijado por la Ley N.º 21.719 (vigencia general 01-12-2026) — principios de finalidad, proporcionalidad y calidad (art. 3); Ley N.º 20.584 y D.S. N.º 41/2012 MINSAL (ficha clínica) |
| Fecha | 2026-07-14 |
| Versión | 0.1 (borrador) |
| Estado | **PROPUESTA — sin aprobar, sin ejecutar** |

---

## 1. Alcance y principio rector

Esta política cubre los datos personales almacenados **localmente** por el
chatbot y el portal del paciente en `data/sessions.db` (VPS DigitalOcean), sus
archivos adjuntos (`data/uploads/`) y sus respaldos.

**Deslinde crítico: lo local NO es la ficha clínica.** La ficha clínica del
paciente vive en Medilink (HIS, Healthatom) y está sujeta a su régimen legal
propio: conservación mínima de **15 años** conforme a la Ley 20.584 y el
D.S. 41/2012 MINSAL *(referencia citada de conocimiento general —
**VERIFICAR** artículo exacto con asesor legal antes de aprobar)*. El portal
**lee** la ficha vía API y no la duplica (EIPD §3.4 y §5). Lo que vive en la
base local es de naturaleza **conversacional y operacional**: sesiones de chat,
telemetría de embudo, perfiles de contacto, mediciones auto-reportadas por el
titular, consentimientos y registros de autenticación. Por eso sus plazos
pueden y deben ser mucho más cortos que los de la ficha.

Principio rector (art. 3, proporcionalidad y calidad): los datos se conservan
**solo mientras sirven a la finalidad que justificó su recolección**. La
retención indefinida actual es la brecha R2/G2 de la EIPD (riesgo ALTO): cada
mes de datos acumulados sin plazo amplifica el impacto de cualquier
vulneración.

---

## 2. Tabla de retención propuesta

> **TODO plazo de esta tabla es PROPUESTA — requiere decisión del responsable.**
> Columna "Método": *Borrado* = eliminación física (`DELETE` + `VACUUM`
> posterior); *Anonimización* = se conserva el registro sin identificadores
> (phone/RUT/nombre) para estadística agregada.

| # | Categoría de dato | Tabla(s) SQLite reales | Plazo propuesto | Justificación legal/clínica | Método |
|---|---|---|---|---|---|
| 1 | Códigos OTP del portal | `portal_otp` | **10 minutos** — **IMPLEMENTADO** (ver §3.1) | Dato de seguridad efímero; solo sirve durante la ventana de validez (5 min) + margen anti-abuso | Borrado |
| 2 | Eventos de conversación / telemetría de embudo | `conversation_events` | **24 meses** | Métricas de servicio y embudo; 2 ciclos anuales bastan para comparación interanual; pasado eso el dato identificado ya no aporta a la finalidad | Borrado (opcional: anonimizar agregados antes de purgar) |
| 3 | Mensajes del chat (WhatsApp/IG/FB, entrantes y salientes) | `messages`, `inbox_messages` | **24 meses** | Contenido conversacional/operacional, no clínico; ventana suficiente para continuidad del servicio, revisión de reclamos y auditoría de calidad | Borrado |
| 4 | Metadatos técnicos de mensajería | `message_statuses`, `processed_msgs`, `bsuid_map` | **12 meses** | Puramente técnico (entrega/dedupe/mapeo Meta); sin valor pasado el ciclo de soporte | Borrado (manual) |
| 5 | Mediciones auto-reportadas (presión, glicemia, peso, temperatura) | `patient_vitals` | **5 años desde cada registro** | Dato de salud sensible **aportado por el titular** para seguimiento de crónicos; 5 años cubre ciclos de control clínico. El titular puede borrarlas **antes** en cualquier momento desde el portal (`DELETE /portal/api/vitals/{id}`) | Borrado |
| 6 | Diagnósticos crónicos (tags `dx:*`) y demás etiquetas | `contact_tags` | Mientras el perfil esté vigente (ver fila 8) | Sirven al seguimiento del paciente activo; caen junto con el perfil | Borrado (cascade con perfil) |
| 7 | Vínculos familiares (tutor de menor / adulto con OTP) | `family_links` | **Hasta revocación** por el titular; la fila revocada (`revoked_at`) se conserva **24 meses** como evidencia de que el vínculo existió y fue revocado, luego se borra | Base = consentimiento (EIPD §4 filas 7-8); la evidencia post-revocación acredita cumplimiento ante disputa | Borrado |
| 8 | Perfiles y sesión del contacto | `contact_profiles`, `sessions`, `contact_tags`, `contact_notes` | **Mientras el paciente esté activo + 3 años desde la última actividad** | Relación asistencial vigente; 3 años de gracia cubren pacientes de control espaciado sin re-registro | Borrado |
| 9 | Registro operacional de citas del bot | `citas_bot`, `citas_recepcion_reminders`, `telemedicina_citas`, `waitlist`, `waitlist_offers` | **Activo + 3 años** (junto con el perfil) | Trazabilidad operacional del agendamiento (la cita clínica real vive en Medilink) | Borrado (manual) |
| 10 | Caches re-sincronizables desde Medilink / BI | `citas_cache`, `ortodoncia_cache`, `kine_tracking`, `abarca_atenciones_cache`, `olavarria_atenciones_cache`, `bi_atenciones`, `bi_pagos_caja` | Mientras se usen operativamente; purgables en cualquier momento | Son espejo de la fuente (Medilink/BI); borrarlos no pierde información — se re-sincronizan | Borrado (manual) |
| 11 | Fidelización, campañas, cross-sell y referidos | `fidelizacion_msgs`, `campanas_envios`, `cross_sell_log`, `pending_crosssell`, `template_sends`, `referral_codes`, `referral_uses`, `referral_bonos`, `demanda_no_disponible`, `meta_referrals` | **24 meses** | Marketing/operacional; mismo horizonte que la telemetría | Borrado (manual) |
| 12 | Archivos aportados por pacientes (fotos, PDFs) | `patient_files` + carpeta `data/uploads/{phone}/` | **Activo + 3 años** (junto con el perfil) | Documentos operacionales aportados por el titular; si un documento se incorporó a la ficha, la copia fiel vive en Medilink | Borrado (fila + archivo físico) |
| 13 | Consentimientos de privacidad | `privacy_consents` | **Mientras la relación esté vigente** (es la prueba de licitud del tratamiento en curso); al ejecutarse una supresión, la fila cae en el cascade y la evidencia queda en `gdpr_deletions` | Accountability (art. 3; deber de acreditación) | Borrado solo vía cascade de supresión |
| 14 | Auditoría de supresiones (derecho al olvido) | `gdpr_deletions` | **Permanente — NO SE BORRA** | Registro inmutable que prueba el cumplimiento del derecho de supresión; borrarlo destruiría la evidencia legal | Ninguno (inmutable) |
| 15 | Respaldos de la base | `/opt/backups/chatbot-cmc/sessions_*.db.gz` (VPS) | **90 días** | Ventana de recuperación operativa razonable; un respaldo viejo es una copia íntegra de TODOS los datos que ya fueron purgados del original — retenerlo más tiempo anula la purga | Borrado del archivo (rotación) |
| 16 | Ficha clínica | **No vive en esta base** — Medilink (HIS) | 15 años (D.S. 41/2012 MINSAL — **verificar**) | Régimen legal propio, Ley 20.584; no suprimible a solicitud del titular | Fuera del alcance de esta política |

Notas a la tabla:

- **Los cortes por "actividad"** (filas 8, 9, 12) se miden desde la última
  interacción del contacto (último mensaje en `messages` o última cita en
  `citas_bot`). Su purga es **MANUAL** y caso a caso; el script de purga
  automatizada NO las toca (ver §4).
- **Anonimización vs borrado**: para `conversation_events` el responsable puede
  optar por generar, antes de la purga, agregados estadísticos sin
  identificadores (conteos por evento/mes/especialidad) y conservarlos
  indefinidamente. La fila identificada se borra igual; el agregado no es dato
  personal.
- **Purga y VACUUM**: SQLite no devuelve espacio al filesystem con `DELETE`;
  tras purgas grandes debe correrse `VACUUM` (el script lo ofrece como flag
  explícito). Con SQLCipher el `VACUUM` re-escribe la base cifrada — hacerlo en
  horario de baja carga.

---

## 3. Estado actual (medido en el código, 2026-07-14)

### 3.1 Lo único que ya se purga hoy: OTPs — IMPLEMENTADO

`app/session.py::verify_portal_otp()` ejecuta en **cada verificación** de
código:

```sql
DELETE FROM portal_otp WHERE created_at < datetime('now', '-10 minutes')
```

Además el código solo es válido por **5 minutos** y hay rate limit de **3
códigos por hora por RUT** (`count_portal_otps`). Es una purga *oportunista*
(se dispara al verificar, no por cron): si nadie verifica OTPs por un período,
pueden quedar filas vencidas transitorias, ya inservibles (usadas o expiradas).
Se documenta como cumplimiento efectivo del plazo de la fila 1.
**PROPUESTA menor**: el script de purga también barre `portal_otp` residual
— *no incluido en la v1 del script por mantenerlo mínimo; los OTPs residuales
se limpian solos con el próximo login*.

### 3.2 Todo lo demás: retención indefinida (la brecha)

Existe `app/session.py::purge_old_data(msgs_days=90, events_days=180)` pero
está **desactivado deliberadamente** (decisión registrada: "retención
indefinida, ~90 MB/año"). Ninguna otra tabla tiene plazo. Esta política
propone reemplazar esa situación por los plazos de §2, ejecutados según §4.

### 3.3 Respaldos

`scripts/backup-cmc-db.sh` respalda `sessions.db` (export SQLCipher con la
misma llave) a `/opt/backups/chatbot-cmc/` y **retiene las últimas 8 copias**.
Además la EIPD registra respaldos diarios comprimidos en el mismo VPS.
**PROPUESTA**: unificar en retención máxima de **90 días** para toda copia de
`sessions.db`, y coordinar con la medida G6 de la EIPD (respaldos cifrados
fuera del VPS). Importante: mientras existan respaldos, los datos purgados del
original siguen existiendo en las copias — el plazo de respaldo es el plazo
real de eliminación efectiva.

---

## 4. Ejecución de la purga

> **PROPUESTA — requiere decisión del responsable.** Nada de esto corre solo.

| Qué | Cómo | Cuándo |
|---|---|---|
| `conversation_events` > 24 meses y `messages` > 24 meses | `scripts/purga_retencion.py` — **manual**, dry-run por defecto, solo borra con `--ejecutar --confirmo-que-hay-respaldo` | Tras aprobación de esta política; luego con la periodicidad que decida el responsable (sugerido: trimestral, siempre manual) |
| Todo lo demás de la tabla §2 (perfiles inactivos, citas, vitals > 5 años, vínculos revocados > 24 meses, metadatos técnicos, fidelización, archivos) | **MANUAL, caso a caso** — deliberadamente fuera del script. Tocan identidad, salud y consentimiento: exigen revisión humana | Revisión anual junto con la revisión de la EIPD |
| Rotación de respaldos a 90 días | Ajustar retención en `backup-cmc-db.sh` / cron de respaldo diario | Junto con la medida G6 (respaldos fuera del VPS) |

Reglas duras del script (ver header de `scripts/purga_retencion.py`):

1. **Dry-run SIEMPRE por defecto** — muestra conteos y fechas de corte sin tocar nada.
2. Solo ejecuta con `--ejecutar --confirmo-que-hay-respaldo` (ambos flags).
3. **NUNCA se agrega a cron ni a `main.py`** — es una decisión humana cada vez.
4. Antes de correr con `--ejecutar`: verificar que el respaldo más reciente existe y se abre con la llave (`backup-cmc-db.sh` ya hace ese smoke test).
5. No toca perfiles, citas, vitals, consents, `gdpr_deletions` ni `family_links`.

---

## 5. Interacción con los derechos de los titulares

La retención es el plazo **máximo**; los derechos del titular operan **antes**
y por encima de cualquier plazo:

- **Supresión (derecho al olvido) — ya implementado**: `DELETE /admin/api/patient`
  (`app/admin_routes.py`) → `app/session.py::delete_patient_data()`. Borrado en
  cascada, en transacción atómica, de las **18 tablas con PII por `phone`**
  (`_PII_TABLES_BY_PHONE`: sessions, contact_tags, citas_bot,
  conversation_events, contact_profiles, messages, fidelizacion_msgs,
  intent_queue, waitlist, message_statuses, bsuid_map, contact_notes,
  portal_otp, referral_codes, campanas_envios, patient_files,
  demanda_no_disponible, privacy_consents) + `referral_uses` (2 columnas de
  phone) + por RUT (`portal_otp`, `waitlist`, `family_links`,
  `patient_vitals`) + por id Medilink (`citas_cache`, `ortodoncia_cache`,
  `kine_tracking`) + carpeta física `data/uploads/{phone}/`. Cada ejecución
  queda auditada en `gdpr_deletions` (inmutable). La solicitud de supresión
  **no espera** al plazo de retención: se ejecuta al resolverse la solicitud
  (30 días corridos, art. 11).
- **Límite legal de la supresión**: la ficha clínica en Medilink NO se suprime
  a solicitud (conservación obligatoria, Ley 20.584 / D.S. 41/2012 — 15 años,
  **verificar**); esto se informa al titular al resolver, igual que en la EIPD §8.
- **Supresión parcial por el titular**: las mediciones auto-reportadas pueden
  ser borradas una a una por el propio paciente desde el portal
  (`DELETE /portal/api/vitals/{id}`), sin solicitud formal.
- **Revocación de vínculos**: los vínculos familiares duran hasta que el
  titular los revoca (`family_links.revoked_at`); la revocación surte efecto
  inmediato en el acceso.
- **Respaldos y supresión**: un dato suprimido del original persiste en los
  respaldos hasta que estos rotan (90 días propuestos). Esto se informa al
  titular al resolver su solicitud, y es una razón más para el plazo corto de
  la fila 15.
- La purga por retención **no sustituye ni degrada** ningún derecho: el titular
  conserva acceso, rectificación, supresión, oposición, bloqueo y (pendiente)
  portabilidad sobre todo dato aún retenido.

---

## 6. Aprobación

> Esta política entra en vigor SOLO con la firma del responsable del
> tratamiento. Hasta entonces, ninguna purga (fuera del OTP ya implementado)
> debe ejecutarse.

| Rol | Nombre | Firma | Fecha |
|---|---|---|---|
| Responsable del tratamiento (representante legal, CMC) | ______________________ | ______________________ | ____ /____ /______ |
| Responsable interno de protección de datos | ______________________ | ______________________ | ____ /____ /______ |
| Responsable técnico del sistema | ______________________ | ______________________ | ____ /____ /______ |
| Revisión legal externa | ______________________ | ______________________ | ____ /____ /______ |

## 7. Registro de cambios

| Versión | Fecha | Cambios | Autor |
|---|---|---|---|
| 0.1 | 2026-07-14 | Borrador inicial: tabla de retención completa contra el schema real de `sessions.db`, estado actual medido en código, reglas de ejecución del script de purga, interacción con derechos | Equipo técnico CMC (asistido por IA; requiere validación humana) |
|  |  |  |  |

---

*Documentos relacionados: `docs/EIPD_portal_paciente_BORRADOR.md` (brecha G2,
riesgo R2, plan de acción §10 acción 2) · `scripts/purga_retencion.py` (purga
manual de bajo riesgo) · `scripts/backup-cmc-db.sh` (respaldos) ·
`app/session.py` (schema, `delete_patient_data`, `verify_portal_otp`,
`purge_old_data` desactivado).*
