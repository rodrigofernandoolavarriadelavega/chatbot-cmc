# Evaluación de Impacto en Protección de Datos Personales (EIPD)

## Portal del Paciente — Centro Médico Carampangue

> **BORRADOR PARA REVISIÓN LEGAL — no constituye asesoría jurídica.**
> Este documento debe ser revisado por abogado/a con competencia en protección de datos antes de su aprobación formal.

| Campo | Valor |
|---|---|
| Sistema evaluado | Portal del Paciente (`agentecmc.cl/portal/v5`) |
| Responsable del tratamiento | Centro Médico Carampangue (CMC), Carampangue, comuna de Arauco, Región del Biobío, Chile |
| Naturaleza del responsable | Empresa de menor tamaño (microempresa/PYME, Ley N.º 20.416) — prestador de salud ambulatorio |
| Marco legal | Ley N.º 19.628 en el texto fijado por la **Ley N.º 21.719** (D.O. 13-12-2024; entrada en vigencia general 1 de diciembre de 2026, implementación gradual — Agencia de Protección de Datos Personales en instalación) |
| Fecha del documento | 2026-07-14 |
| Versión | 0.1 (borrador) |
| Elaborado por | Equipo técnico CMC (asistido por IA; requiere validación humana) |
| Próxima revisión | Antes del 2026-12-01 (entrada en vigencia) y luego anual, o ante cambio sustancial del tratamiento |

**Nota de vigencia.** A la fecha de este borrador la Ley 21.719 se encuentra en período de vacancia (vigencia general el 01-12-2026). Esta EIPD se elabora de forma **preparatoria y voluntaria**, para que el Portal del Paciente llegue conforme a la fecha de exigibilidad. Todas las citas a artículos corresponden a la Ley N.º 19.628 **en su texto reemplazado por la Ley 21.719**, salvo indicación en contrario.

---

## 2. Necesidad de la EIPD (por qué aplica)

El **artículo 15 ter** exige realizar una evaluación de impacto, **previa** al inicio de las operaciones, cuando sea probable que un tipo de tratamiento —por su naturaleza, alcance, contexto, tecnología utilizada o fines— produzca un **alto riesgo** para los derechos de los titulares, y enumera supuestos en que es en todo caso exigible:

| Supuesto del art. 15 ter | ¿Aplica al Portal? | Justificación |
|---|---|---|
| a) Evaluación sistemática y exhaustiva de aspectos personales basada en tratamiento automatizado, como perfiles | **Parcial / bajo** | Existen recordatorios y recomendaciones preventivas personalizadas por edad/diagnóstico. Es segmentación simple basada en reglas, **sin decisiones automatizadas con efectos jurídicos ni evaluación exhaustiva de la persona** (no gatilla por sí solo el art. 8 bis). Se evalúa igualmente por prudencia. |
| b) Tratamiento masivo o a gran escala | **No** | Un solo centro ambulatorio en Carampangue; universo de pacientes del orden de miles, ámbito comunal, un responsable, sin cruce con otras fuentes. No constituye gran escala. |
| c) Observación o monitoreo sistemático de zona de acceso público | **No** | No hay videovigilancia ni monitoreo de espacios públicos en el sistema evaluado. |
| d) Tratamiento de **datos sensibles** y especialmente protegidos, en las hipótesis de excepción del consentimiento | **Sí** | El portal trata **datos de salud** (art. 2 y arts. 16 y 16 bis): citas por especialidad, historial de atenciones, diagnósticos crónicos, mediciones clínicas. Aunque el diseño descansa principalmente en consentimiento expreso, parte del tratamiento (lectura de citas e historial desde la ficha clínica para la prestación de asistencia sanitaria) se ampara en las hipótesis del **art. 16 bis** (prestación de asistencia o tratamiento sanitario), es decir, en excepciones al consentimiento. |
| Cláusula general de alto riesgo | **Sí** | Concurren: datos sensibles de salud + canal de mensajería de un tercero (WhatsApp/Meta) + transferencia internacional (arts. 27-28) + datos de niños, niñas y adolescentes (art. 16 quáter) + acceso remoto por internet. La combinación configura riesgo alto probable en caso de vulneración. |

**Conclusión:** la EIPD **es procedente y exigible** por la letra d) del art. 15 ter y por la cláusula general de alto riesgo, aun cuando el tratamiento **no** es a gran escala. Realizarla antes de la vigencia de la ley es además coherente con los deberes de protección desde el diseño y por defecto (**art. 14 quáter**) y de responsabilidad (art. 3).

---

## 3. Descripción sistemática del tratamiento

### 3.1 Descripción del sistema

Portal web para pacientes del CMC (`agentecmc.cl/portal/v5`), que permite: ver citas; agendar y cambiar horas; consultar historial de atenciones; auto-registrar mediciones (presión arterial, glicemia, peso, temperatura); mantener diagnósticos crónicos (etiquetas); actualizar datos de contacto; administrar vínculos familiares; y acceder a guías educativas.

### 3.2 Titulares de los datos

| Titular | Particularidad |
|---|---|
| Pacientes adultos del CMC | Acceden con su propio número de WhatsApp previamente registrado. |
| Niños, niñas y adolescentes | Vinculados a la cuenta de un adulto mediante **declaración de tutor** (padre/madre/representante legal). Rige el art. 16 quáter: interés superior y autonomía progresiva; menores de 14 años requieren consentimiento del representante. |
| Familiares adultos vinculados | Solo con **autorización OTP enviada a su propio WhatsApp** (el titular adulto autoriza personalmente el vínculo). |
| Contactos de emergencia | Terceros cuyos datos de contacto entrega el paciente (dato mínimo: nombre y teléfono). |

### 3.3 Categorías de datos tratados

| Categoría | Datos | ¿Sensible? |
|---|---|---|
| Identificación | RUT, nombre, fecha de nacimiento, sexo | No (pero identificador único nacional: riesgo de suplantación) |
| Contacto | Teléfono/WhatsApp, email, comuna, dirección | No |
| Previsión de salud | Fonasa/Isapre | Vinculado a salud; tratamiento cauteloso |
| **Salud (sensibles, arts. 2, 16 y 16 bis)** | Citas por especialidad, historial de atenciones, diagnósticos crónicos (tags), mediciones auto-reportadas (presión, glicemia, peso, temperatura) | **Sí** |
| Vínculos | Familiares (incl. menores), contacto de emergencia | Puede revelar situación familiar |
| Técnicos | Cookie de sesión firmada, registros OTP, consentimientos (`privacy_consents`), logs | No, pero críticos para seguridad |

### 3.4 Flujo de datos (diagrama en texto)

```
Paciente (navegador móvil/desktop)
   │ 1. Ingresa RUT/teléfono registrado
   ▼
Portal v5 (uvicorn:8001, VPS DigitalOcean, EE.UU.)
   │ 2. Genera OTP 6 dígitos (validez ~10 min, máx 3/hora)
   │    o magic link firmado (30 min)
   ▼
WhatsApp Business Cloud API (Meta Platforms ── TRANSFERENCIA INTERNACIONAL)
   │ 3. Entrega OTP/link SOLO al número previamente registrado
   ▼
Paciente valida OTP ──► Cookie de sesión HMAC-SHA256, httpOnly, 24 h
   │
   ├── 4a. LECTURA de citas e historial ──► API Medilink (healthatom, SaaS chileno,
   │        con token; el HIS es la fuente; el portal NO duplica la ficha clínica)
   │
   ├── 4b. ESCRITURA local: mediciones, diagnósticos crónicos (tags), perfil,
   │        vínculos, consentimientos ──► SQLite `sessions.db` (VPS, acceso SSH
   │        solo por llave; SIN cifrado en reposo a la fecha)
   │
   └── 4c. Caché en el teléfono (localStorage): SOLO nombre y próximas citas,
            sin diagnósticos; se borra al cerrar sesión.

Respaldos: diarios, comprimidos, en el mismo VPS.
Supresión: borrado en cascada de 18 tablas con auditoría (derecho al olvido).
```

### 3.5 Ciclo de vida del dato

1. **Recolección**: registro previo del paciente (recepción/chatbot) y auto-reporte en el portal; opt-in explícito registrado en `privacy_consents`; checkbox obligatorio al reservar hora.
2. **Uso**: gestión de citas, continuidad de la atención, seguimiento de crónicos, educación en salud, recordatorios preventivos.
3. **Almacenamiento**: perfiles y mediciones en SQLite local del CMC; ficha clínica permanece en Medilink (el portal solo lee).
4. **Comunicación**: mensajes operativos por WhatsApp (OTP, links, recordatorios). Sin venta ni cesión de datos a terceros.
5. **Retención**: **indefinida a la fecha (brecha identificada, ver §7)**; respaldos diarios en el VPS.
6. **Supresión**: a solicitud del titular, borrado en cascada (18 tablas) con auditoría; la ficha clínica en Medilink queda sujeta a su régimen legal propio (Ley 20.584 y D.S. 41/2012 MINSAL: conservación mínima de 15 años, no suprimible a solicitud).

### 3.6 Encargados y terceros

| Entidad | Rol | Ubicación | Datos |
|---|---|---|---|
| Medilink (Healthatom) | Encargado del HIS/ficha clínica (art. 15 bis) | Chile (SaaS) | Ficha clínica, citas; el portal lee vía API con token |
| Meta Platforms (WhatsApp Business Cloud API) | Encargado del canal de mensajería | EE.UU. (transferencia internacional, arts. 27-28) | Número de teléfono, contenido de mensajes (OTP, links, recordatorios) |
| DigitalOcean | Encargado de infraestructura (hosting VPS) | EE.UU. (transferencia internacional) | Toda la base local del portal |

---

## 4. Base de licitud por finalidad

Fuentes de licitud: consentimiento (**art. 12**: libre, informado, específico, previo e inequívoco) y bases del **art. 13**; para datos sensibles rige el régimen reforzado de los **arts. 16 y 16 bis** (consentimiento expreso; excepciones tasadas, entre ellas la prestación de asistencia o tratamiento sanitario y las finalidades previstas en leyes especiales en materia de salud); para menores, el **art. 16 quáter**.

| # | Finalidad | Base de licitud | Artículo |
|---|---|---|---|
| 1 | Autenticación del paciente (OTP/magic link, cookie de sesión, límites anti-abuso) | Medidas necesarias para la prestación solicitada + interés legítimo en la seguridad del sistema | Art. 13 c) y d); deber de seguridad art. 14 quinquies |
| 2 | Agendamiento y cambio de horas | Ejecución de la relación asistencial solicitada por el titular (actos preparatorios/ejecución de contrato de prestación de salud) | Art. 13 c) |
| 3 | Visualización de citas e historial de atenciones (lectura desde Medilink) | Prestación de asistencia o tratamiento sanitario; ficha clínica regulada por ley especial (Ley 20.584; D.S. 41/2012) | Art. 16 bis (excepción sanitaria); art. 13 b) respecto de las obligaciones legales sobre ficha clínica |
| 4 | Auto-registro de mediciones y diagnósticos crónicos (tags) | **Consentimiento expreso** del titular (opt-in registrado en `privacy_consents`); finalidad sanitaria de seguimiento | Arts. 12 y 16; art. 16 bis (medicina preventiva/seguimiento) |
| 5 | Recordatorios y recomendaciones preventivas personalizadas (edad/diagnóstico) | **Consentimiento expreso** (opt-in). No hay decisiones automatizadas con efectos jurídicos; si se escalara a perfilamiento con efectos significativos, aplicaría el derecho de oposición del art. 8 bis | Arts. 12 y 16; referencia art. 8 bis |
| 6 | Datos de contacto y previsión | Ejecución de la relación asistencial + consentimiento | Art. 13 c); art. 12 |
| 7 | Vínculos familiares — menores de edad | Consentimiento del padre/madre/representante (declaración de tutor), interés superior del niño | Art. 16 quáter |
| 8 | Vínculos familiares — adultos | **Consentimiento propio** del familiar, acreditado por OTP a su propio WhatsApp | Arts. 12 y 16 |
| 9 | Contacto de emergencia (datos de un tercero) | Interés legítimo del responsable/titular en la seguridad del paciente; salvaguarda de vida o integridad en urgencia | Art. 13 d); art. 16 bis (urgencia) |
| 10 | Conservación de consentimientos, auditoría de supresiones y logs de seguridad | Cumplimiento de obligaciones legales de acreditación (accountability) e interés legítimo | Art. 13 b) y d); arts. 14 quinquies y 14 sexies |
| 11 | Respaldo y continuidad operativa | Interés legítimo + deber de seguridad | Art. 13 d); art. 14 quinquies |

**Regla de oro aplicada:** ninguna finalidad se ampara en consentimiento cuando en realidad existe obligación legal o necesidad asistencial (evita consentimientos "decorativos" que el titular no puede revocar en la práctica), y ningún dato sensible se trata fuera de consentimiento expreso o de las hipótesis tasadas del art. 16 bis.

---

## 5. Evaluación de necesidad y proporcionalidad

**Juicio de idoneidad.** El portal reduce llamadas y traslados para una población rural (Carampangue/Arauco), mejora adherencia de pacientes crónicos y continuidad asistencial. La finalidad es legítima y el medio es idóneo.

**Juicio de necesidad (minimización — art. 3).** Decisiones de diseño ya implementadas que acreditan minimización:

- **El portal no duplica la ficha clínica**: lee citas e historial desde Medilink vía API; en la base local solo viven perfiles, mediciones y consentimientos.
- **Caché del teléfono sin datos sensibles**: localStorage guarda SOLO nombre y próximas citas (sin diagnósticos) y se borra al cerrar sesión. Un teléfono perdido o compartido no expone diagnósticos desde la caché.
- **OTP únicamente al número ya registrado**: no se puede pedir el código hacia un número arbitrario; el factor de posesión queda anclado al canal validado previamente.
- **Vínculo de familiar adulto solo con autorización del propio familiar** (OTP a su WhatsApp): nadie agrega a un adulto sin su intervención.
- **Ventanas de validez cortas**: OTP ~10 minutos y máximo 3 códigos/hora (anti fuerza bruta); magic link 30 minutos; sesión 24 horas con cookie firmada HMAC-SHA256, httpOnly.
- **Datos pedidos = datos usados**: no se solicitan datos que el portal no ocupa (sin datos financieros, sin geolocalización, sin biometría).
- **Sin cesión ni venta**; sin decisiones automatizadas con efectos jurídicos.

**Juicio de proporcionalidad estricto.** El beneficio asistencial supera el riesgo residual **a condición de** cerrar las brechas del §7 (cifrado en reposo, plazos de retención, contratos con encargados). Con retención indefinida y base sin cifrar, la proporcionalidad queda comprometida en el tiempo: el riesgo crece con cada mes de datos acumulados sin plazo. Por eso el plan de acción (§10) trata esas dos brechas como prioritarias.

---

## 6. Identificación y evaluación de riesgos

Escala: Probabilidad (Baja / Media / Alta) × Impacto (Bajo / Medio / Alto / Muy alto) → Nivel (Bajo / Medio / Alto / Crítico). El impacto se evalúa sobre los derechos del titular (no sobre el negocio): los datos de salud pueden generar discriminación, estigmatización o daño familiar en una comunidad pequeña donde "todos se conocen".

| # | Riesgo | Escenario | Prob. | Impacto | Nivel |
|---|---|---|---|---|---|
| R1 | **Compromiso del VPS con base sin cifrar en reposo** | Intrusión, robo de snapshot o acceso del proveedor: `sessions.db` legible en claro con diagnósticos y mediciones de todos los usuarios del portal | Media | Muy alto | **Crítico** |
| R2 | **Retención indefinida** | Datos sensibles acumulados sin plazo amplifican cualquier brecha y vulneran el principio de proporcionalidad/calidad; hoy no hay política definida | Alta (situación actual cierta) | Alto | **Alto** |
| R3 | **Acceso por teléfono prestado o compartido** | El OTP llega al WhatsApp de un teléfono que usa otra persona (pareja, hijo, patrón); frecuente en contexto rural con equipos compartidos | Media | Alto | **Alto** |
| R4 | **Secuestro de la cuenta de WhatsApp (SIM swap / robo de sesión)** | Un tercero controla el número registrado y recibe OTP/magic links, accediendo a historial y diagnósticos | Baja-Media | Alto | **Alto** |
| R5 | **Suplantación por RUT conocido** | El RUT chileno es semi-público; combinado con ingeniería social o número registrado desactualizado (número reciclado por la compañía telefónica) permite intentar acceso o crear vínculos fraudulentos | Media | Alto | **Alto** |
| R6 | **Vínculo de menor con declaración de tutor no verificada** | Un adulto sin patria potestad/cuidado personal (p. ej. progenitor con medida cautelar, expareja) declara ser tutor y accede a datos de salud del menor | Media | Alto | **Alto** |
| R7 | **Transferencia internacional sin garantías documentadas** | Meta (WhatsApp) y DigitalOcean (EE.UU.) tratan datos sin cláusulas/garantías verificadas conforme arts. 27-28; incumplimiento formal aunque el riesgo material sea moderado | Alta (brecha formal actual) | Medio | **Alto** |
| R8 | **Respaldos diarios sin cifrar en el mismo VPS** | La copia comparte destino con el original: una intrusión compromete dato y respaldo; sin copia fuera de sitio tampoco hay resiliencia ante ransomware | Media | Alto | **Alto** |
| R9 | **Ausencia de contratos de encargo (DPA) con Medilink, Meta y DigitalOcean** | Sin el contrato del art. 15 bis, el CMC no puede acreditar instrucciones, confidencialidad ni deberes de seguridad de sus encargados | Alta (brecha formal actual) | Medio | **Alto** |
| R10 | **Reenvío o intercepción del magic link** | El paciente reenvía el link (o lo hace un tercero con acceso momentáneo al chat); 30 minutos de ventana con sesión de 24 h resultante | Media | Medio | **Medio** |
| R11 | **Interceptación del canal WhatsApp** | Cifrado de extremo a extremo de WhatsApp mitiga en tránsito, pero Meta procesa metadatos y el contenido es visible en el dispositivo y en respaldos de chat del paciente (Google Drive/iCloud sin E2E por defecto) | Baja | Medio | **Medio** |
| R12 | **Mediciones auto-reportadas erróneas usadas clínicamente** | Dato mal digitado (glicemia 500 por 50) induce decisión clínica o alarma indebida; riesgo de exactitud (art. 3, calidad del dato) | Media | Medio | **Medio** |
| R13 | **Vulneración sin procedimiento de respuesta** | Ocurrida una brecha, el CMC no tiene hoy protocolo escrito para reportar a la Agencia "sin dilaciones indebidas" ni para notificar a titulares cuando hay datos sensibles o de menores de 14 (art. 14 sexies) | Media | Medio | **Medio** |
| R14 | **localStorage en dispositivo compartido** | Nombre y próximas citas visibles para quien tome el teléfono con sesión abierta; sin diagnósticos y con borrado al cerrar sesión | Baja | Bajo | **Bajo** |
| R15 | **Exceso de acceso interno** | Personal del CMC o del desarrollo con acceso SSH puede leer toda la base; sin registro granular de accesos administrativos | Media | Medio | **Medio** |

**Riesgo residual agregado antes de mitigaciones pendientes: ALTO**, dominado por R1, R2 y el bloque formal R7/R9. Con el plan del §10 completado, el residual proyectado es **Medio-Bajo**.

---

## 7. Medidas de mitigación existentes y recomendadas (gap analysis)

### 7.1 Medidas ya implementadas

| Medida | Riesgos que mitiga |
|---|---|
| OTP 6 dígitos, validez ~10 min, máx 3/hora, solo al número registrado | R3, R4, R5 |
| Cookie de sesión HMAC-SHA256, httpOnly, expiración 24 h | R10, secuestro de sesión |
| Magic link firmado con ventana de 30 min | R10 |
| Acceso al VPS únicamente por llave SSH (sin password) | R1, R15 |
| Portal solo LEE la ficha desde Medilink (no la duplica) | R1, R2 |
| Caché local sin diagnósticos + borrado al cerrar sesión | R14 |
| Opt-in explícito registrado (`privacy_consents`) + checkbox al reservar + política en `/privacidad` | Licitud (arts. 12, 16, 14 ter) |
| Derecho al olvido implementado: borrado en cascada de 18 tablas con auditoría | R2, art. 7 |
| Vínculo de adulto solo con OTP a su propio WhatsApp | R5 |
| Sin venta/cesión de datos; sin decisiones automatizadas con efectos legales | Riesgos de finalidad |
| Respaldos diarios (continuidad) | Disponibilidad |

### 7.2 Brechas y medidas recomendadas (honesto: lo que falta)

| # | Brecha actual | Medida recomendada | Riesgo | Prioridad |
|---|---|---|---|---|
| G1 | **SQLite sin cifrar en reposo** (`sessions.db` en claro en el VPS) | Migrar a **SQLCipher** o cifrado a nivel de disco/volumen (LUKS) + gestión de llave fuera del mismo host; ver `docs/encryption_at_rest.md` | R1 | **P1** |
| G2 | **Retención indefinida** | Definir y aplicar **política de retención**: p. ej. mediciones y perfiles 5 años desde última actividad (alineado con seguimiento de crónicos), OTP/logs de autenticación 6-12 meses, consentimientos y auditoría de supresión mientras deba acreditarse; job de purga automática. La ficha clínica en Medilink conserva su plazo legal propio (15 años, D.S. 41/2012) | R2 | **P1** |
| G3 | **Sin contratos de encargo (art. 15 bis)** | Suscribir/archivar DPA con **Medilink (Healthatom)**; descargar y archivar los términos de tratamiento de datos de **Meta WhatsApp Business** y **DigitalOcean** (ambos ofrecen data processing addenda estándar con cláusulas contractuales); dejar evidencia en carpeta de cumplimiento | R7, R9 | **P1** |
| G4 | **Transferencias internacionales sin análisis documentado (arts. 27-28)** | Documentar la evaluación de garantías de Meta y DigitalOcean (cláusulas contractuales, certificaciones); incorporar la transferencia a la política de privacidad e informarla al titular (art. 14 ter) | R7 | **P1** |
| G5 | **Sin protocolo de vulneraciones (art. 14 sexies)** | Redactar procedimiento de respuesta a incidentes: detección → contención → evaluación de riesgo → reporte a la Agencia sin dilaciones indebidas → notificación a titulares si hay datos sensibles o de menores de 14 → registro interno del incidente | R13 | **P1** |
| G6 | **Respaldos sin cifrar y en el mismo VPS** | Cifrar respaldos (age/GPG) y copiar a destino independiente (bucket u otro proveedor, idealmente con evaluación art. 27); prueba de restauración semestral | R8 | **P2** |
| G7 | **Declaración de tutor sin verificación** | Verificación reforzada del vínculo con menores: exigir RUT del menor + coincidencia con registros del CMC (menor ya paciente), advertencia legal en la declaración, y revisión manual por recepción cuando el menor no sea paciente previo; registrar la declaración con timestamp y evidencia | R6 | **P2** |
| G8 | **Sin registro de actividades de tratamiento (RAT)** | Levantar RAT interno (finalidades, categorías, bases, encargados, transferencias, plazos): la ley no lo exige con el detalle del RGPD, pero es la forma práctica de acreditar cumplimiento (accountability) y de responder fiscalizaciones | Transversal | **P2** |
| G9 | **Sin responsable interno designado** | Designar **encargado de protección de datos interno** (en una EMT puede asumirlo el dueño, conforme al modelo de prevención de infracciones, arts. 49 y ss.); definir correo de contacto para titulares y para la Agencia | Transversal | **P2** |
| G10 | **Sin registro granular de accesos administrativos** | Log de accesos SSH/consultas administrativas a `sessions.db` (quién, cuándo, qué); revisión mensual | R15 | **P2** |
| G11 | **Número registrado puede quedar obsoleto (números reciclados)** | Re-validación periódica del número (p. ej. si pasa >12 meses sin uso, exigir re-verificación presencial o doble factor con dato adicional) antes de enviar OTP | R5, R3 | **P3** |
| G12 | **Mediciones sin validación de rango** | Validación de rangos plausibles al ingresar mediciones (glicemia, presión, temperatura) con confirmación ante valores extremos; etiqueta visible "dato auto-reportado" en toda vista clínica | R12 | **P3** |
| G13 | **Aviso ante sesiones en dispositivos compartidos** | Aviso al iniciar sesión: "Si este teléfono lo usan otras personas, cierra sesión al terminar"; opción "cerrar todas las sesiones" | R3, R14 | **P3** |
| G14 | **Política de privacidad previa a la Ley 21.719** | Actualizar `/privacidad` a los deberes de información del art. 14 ter (identidad del responsable, finalidades, bases, encargados, transferencias internacionales, plazos de retención, derechos y canal de ejercicio, derecho a reclamar ante la Agencia) | Transversal | **P2** |
| G15 | **Sin prueba de seguridad externa** | Pentest o revisión de seguridad enfocada en autenticación OTP, fijación de sesión y del endpoint API que lee Medilink, antes del 01-12-2026 | R1, R4, R5 | **P2** |

---

## 8. Derechos de los titulares y canales (ARCO-P)

Derechos reconocidos (art. 4 y siguientes), todos de ejercicio **gratuito** ante el CMC:

| Derecho | Artículo | Implementación en el CMC |
|---|---|---|
| Acceso | Art. 5 | El propio portal es el mecanismo primario de acceso (citas, historial, mediciones, perfil). Copia completa a solicitud. |
| Rectificación | Art. 6 | Datos de contacto y perfil editables en el portal; datos clínicos vía recepción/profesional (corrección en Medilink). |
| Supresión (cancelación) | Art. 7 | **Implementado**: borrado en cascada de 18 tablas con auditoría. **Límite legal**: la ficha clínica no se suprime a solicitud — conservación obligatoria (Ley 20.584; D.S. 41/2012 MINSAL, 15 años); esto se informa al titular al resolver. |
| Oposición | Art. 8 | Opt-out de recordatorios/recomendaciones preventivas en cualquier momento sin afectar la atención de salud. |
| Oposición a decisiones automatizadas | Art. 8 bis | No hay decisiones automatizadas con efectos jurídicos; si se implementaran, se habilitará intervención humana. |
| Bloqueo temporal | Art. 8 ter | Pendiente de proceso interno: resolver en **2 días hábiles** cuando acompañe una solicitud de rectificación, supresión u oposición. |
| Portabilidad | Art. 9 | Pendiente: export estructurado (JSON/CSV) de perfil, mediciones y consentimientos desde el portal. |

**Procedimiento y plazos (art. 11):** respuesta dentro de **30 días corridos**, prorrogable **una sola vez** por razones justificadas; respuesta escrita, clara y por el mismo medio de la solicitud salvo indicación del titular.

**Canales de ejercicio:**
- Portal: sección de privacidad (`agentecmc.cl/privacidad`) y opciones de cuenta.
- WhatsApp oficial del CMC: +56 9 6661 0737.
- Teléfono fijo: (44) 296 5226.
- Presencial: recepción del CMC, Carampangue.
- Correo del responsable interno de datos (definir en G9).

Si el titular no queda conforme, puede **reclamar ante la Agencia de Protección de Datos Personales** una vez operativa (se indicará en la respuesta y en la política de privacidad).

**Menores (art. 16 quáter):** las solicitudes sobre datos de menores de 14 años las ejercen sus padres o representantes; los adolescentes pueden ejercerlas atendida su autonomía progresiva, con resguardo reforzado en datos sensibles.

---

## 9. Consulta previa a la Agencia de Protección de Datos Personales

Conforme al **art. 15 ter**, si de la evaluación resulta que el tratamiento mantiene un **alto riesgo** (residual), el responsable **puede consultar a la Agencia antes de proceder**, la que emitirá recomendaciones. La consulta es facultativa, no un permiso previo.

**Criterio adoptado por el CMC:**
- **No se consultará** si al 01-12-2026 están cerradas las brechas P1 (G1-G5): el riesgo residual proyectado es Medio-Bajo y el tratamiento no es a gran escala.
- **Sí correspondería consultar** si a esa fecha persisten sin mitigar R1 (base sin cifrar) o R2 (retención indefinida), o si se decide incorporar funciones que cambien el perfil de riesgo: perfilamiento con efectos significativos, telemonitoreo continuo, integración de resultados de laboratorio/imágenes en la base local, o apertura del portal a otros centros (escala).
- Mientras la Agencia completa su instalación (implementación gradual de la ley), esta EIPD y su plan de acción quedan archivados como evidencia de cumplimiento proactivo.

---

## 10. Plan de acción

| # | Acción | Brecha | Responsable sugerido | Plazo sugerido |
|---|---|---|---|---|
| 1 | Cifrado en reposo de `sessions.db` (SQLCipher o volumen cifrado) + llave fuera del host | G1 | Desarrollo (R. Olavarría) | 2026-09-30 |
| 2 | Política de retención escrita + job de purga automática + actualización de `/privacidad` | G2, G14 | Dirección + Desarrollo | 2026-09-30 |
| 3 | DPA/términos de encargo archivados: Medilink, Meta, DigitalOcean | G3 | Dirección (con revisión legal) | 2026-09-30 |
| 4 | Análisis documentado de transferencias internacionales (arts. 27-28) | G4 | Asesor legal externo | 2026-10-31 |
| 5 | Protocolo de respuesta a vulneraciones (art. 14 sexies) + simulacro | G5 | Desarrollo + Dirección | 2026-10-31 |
| 6 | Respaldos cifrados fuera del VPS + prueba de restauración | G6 | Desarrollo | 2026-10-31 |
| 7 | Verificación reforzada de declaración de tutor (menores) | G7 | Desarrollo + Recepción | 2026-10-31 |
| 8 | RAT interno + designación de responsable interno de datos (EMT: puede asumir el dueño) | G8, G9 | Dirección | 2026-11-15 |
| 9 | Log de accesos administrativos + revisión mensual | G10 | Desarrollo | 2026-11-15 |
| 10 | Export de portabilidad (art. 9) + proceso de bloqueo en 2 días hábiles (art. 8 ter) | §8 | Desarrollo | 2026-11-30 |
| 11 | Pentest de autenticación y sesión | G15 | Externo | 2026-11-30 |
| 12 | Validación de rangos en mediciones + avisos de dispositivo compartido + re-validación de números inactivos | G11-G13 | Desarrollo | 2027-03-31 |
| 13 | Revisión legal integral de esta EIPD y aprobación formal | — | Asesor legal externo | Antes de 2026-12-01 |
| 14 | Revisión anual de la EIPD (o ante cambio sustancial del tratamiento) | — | Dirección | Anual desde 2027 |

**Nota EMT/PYME:** como empresa de menor tamaño (Ley 20.416), el CMC podría acceder durante los primeros 12 meses de vigencia a la sustitución de multa por **amonestación escrita** en una primera infracción (artículo sexto transitorio, Ley 21.719). Esto **no exime** de cumplir: la amonestación queda registrada y agrava fiscalizaciones futuras. El régimen sancionatorio general contempla multas de hasta 5.000 UTM (leves), 10.000 UTM (graves) y 20.000 UTM (gravísimas), con recargos por reincidencia (arts. 34 y 35). El tratamiento indebido de datos sensibles se sitúa en los tramos superiores.

---

## 11. Aprobación y firmas

| Rol | Nombre | Firma | Fecha |
|---|---|---|---|
| Responsable del tratamiento (representante legal, Centro Médico Carampangue) | ______________________ | ______________________ | ____ /____ /______ |
| Responsable interno de protección de datos | ______________________ | ______________________ | ____ /____ /______ |
| Responsable técnico del sistema | ______________________ | ______________________ | ____ /____ /______ |
| Revisión legal externa (abogado/a) | ______________________ | ______________________ | ____ /____ /______ |

**Historial de versiones**

| Versión | Fecha | Cambios | Autor |
|---|---|---|---|
| 0.1 | 2026-07-14 | Borrador inicial completo | Equipo técnico CMC (asistido por IA) |
|  |  |  |  |

---

*Referencias normativas: Ley N.º 21.719 (D.O. 13-12-2024), que sustituye el régimen de la Ley N.º 19.628 y crea la Agencia de Protección de Datos Personales — en particular arts. 2 (definiciones), 3 (principios), 4 a 11 (derechos y procedimiento), 12 y 13 (bases de licitud), 14 bis a 14 sexies (deberes de secreto, información, protección desde el diseño, seguridad y reporte de vulneraciones), 15 bis (encargado), 15 ter (EIPD y consulta a la Agencia), 16, 16 bis y 16 quáter (datos sensibles, datos de salud y menores), 27 y 28 (transferencias internacionales), 34 y 35 (infracciones y sanciones), 49 y ss. (modelo de prevención) y artículo sexto transitorio (EMT). Normativa sectorial: Ley N.º 20.584 (derechos y deberes de los pacientes) y D.S. N.º 41/2012 MINSAL (ficha clínica). Ley N.º 20.416 (empresas de menor tamaño).*
