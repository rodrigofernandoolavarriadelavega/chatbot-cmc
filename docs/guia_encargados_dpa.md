# Guía práctica: contratos de encargo de tratamiento de datos (DPA) — Centro Médico Carampangue

> **BORRADOR DE APOYO — NO CONSTITUYE ASESORÍA LEGAL.**
> Este documento es una guía operativa preparada con información pública verificada al **14 de julio de 2026**. Los términos de los proveedores cambian; verificar la versión vigente antes de archivar. Ante cualquier duda sobre alcance o suficiencia jurídica, consultar a un abogado especialista en protección de datos.

**Objetivo:** cerrar la medida **P1 / G3-G4** de la EIPD del portal de pacientes (`docs/EIPD_portal_paciente_BORRADOR.md`, §3.6 y §7.2) y mitigar los riesgos **R7** (transferencias internacionales sin garantías documentadas, arts. 27-28 Ley 21.719) y **R9** (ausencia de contratos de encargo, art. 15 bis Ley 21.719).

---

## 0. Qué exige la ley y qué significa "formalizar" en la práctica

El **art. 15 bis de la Ley 21.719** exige que quien trate datos por cuenta del responsable (el CMC) lo haga en virtud de un **contrato o acto jurídico escrito** que fije: objeto y duración del encargo, finalidad, tipo de datos, instrucciones, deber de confidencialidad, medidas de seguridad, régimen de sub-encargados y destino de los datos al terminar.

**Punto clave para una microempresa:** con proveedores globales (DigitalOcean, Meta, Anthropic) e incluso con Healthatom, **no se negocia un contrato a medida**. Todos ofrecen un **DPA de adhesión** que ya viene incorporado a sus términos de servicio. "Formalizar" significa entonces:

1. **Identificar** el documento exacto que aplica.
2. **Verificar** que está aceptado/incorporado (casi siempre es automático al usar el servicio).
3. **Descargar y archivar** una copia fechada (PDF) en la carpeta de cumplimiento.
4. **Anotar** en el registro interno (RAT): documento, versión/fecha, mecanismo de aceptación, garantías de transferencia internacional, sub-encargados.
5. Solo en el caso de **Healthatom/Medilink** conviene además un correo formal (sección 3) para dejar constancia bilateral y cerrar los vacíos de su DPA público.

**Carpeta de cumplimiento sugerida:** crear `docs/cumplimiento/encargados/` en este repo (o una carpeta en Drive del CMC) con un subdirectorio por proveedor: `digitalocean/`, `meta-whatsapp/`, `healthatom-medilink/`, `anthropic/`. Cada PDF archivado se nombra `AAAA-MM-DD_nombre-documento.pdf` (fecha de descarga).

> Cómo guardar un PDF fechado: abrir la URL en el navegador → Imprimir → "Guardar como PDF". El pie de página del navegador imprime la URL y la fecha, lo que sirve como evidencia de la versión capturada.

---

## 1. DigitalOcean (hosting VPS — toda la base local del portal y del bot)

### Qué documento aplica

- **Data Processing Agreement (DPA)**: https://www.digitalocean.com/legal/data-processing-agreement — versión vigente verificada: **actualizada al 6 de febrero de 2026** (las versiones anteriores quedan archivadas en la misma sección legal, p. ej. octubre 2025, enero 2026).
- Es un **anexo de los Customer Terms of Service** (https://www.digitalocean.com/legal/terms-of-service-agreement).

### Cómo se acepta

**Automático.** El DPA forma parte de los Terms of Service que se aceptaron al crear la cuenta de DigitalOcean. **No hay nada que firmar ni ningún botón en el panel**: al mantener y usar la cuenta bajo los ToS vigentes, el DPA rige. Si se quisiera una copia con contrafirma (no es necesario para acreditar el encargo), se puede solicitar a **privacy@digitalocean.com**.

### Qué contiene (lo relevante para el CMC)

- Roles: el cliente (CMC) es **responsable** (controller); DigitalOcean es **encargado** (processor) respecto de los datos que el cliente aloja en sus servicios (el caso del VPS con `sessions.db`).
- **Cláusulas Contractuales Tipo (SCC) de la UE incorporadas** en el propio DPA, en tres módulos:
  - **Módulo 2 (responsable → encargado)**: el que aplica al CMC por los datos de pacientes almacenados en el droplet. Esta es la **garantía documentable** para el análisis de transferencia internacional (arts. 27-28 Ley 21.719), ya que los servidores y la empresa están en EE. UU.
  - Módulo 1 (responsable → responsable) y Módulo 3 (encargado → sub-encargado) para los demás escenarios.
- Cubre expresamente las leyes de protección de datos de la UE/EEE, Suiza, Reino Unido y California; la Ley 21.719 no se nombra, pero el contenido del DPA + SCC cumple materialmente lo que pide el art. 15 bis (instrucciones, confidencialidad, seguridad, sub-encargados, supresión al término).
- **Sub-encargados**: listado público en https://www.digitalocean.com/trust/subprocessors (el DPA autoriza su uso y remite a esa página).
- Preguntas de privacidad / Schrems II / medidas suplementarias: https://www.digitalocean.com/trust/schrems-ii-faq — contacto **privacy@digitalocean.com**.

### Pasos concretos

1. [ ] Entrar a https://www.digitalocean.com/legal/data-processing-agreement y **guardar como PDF fechado** en `cumplimiento/encargados/digitalocean/`.
2. [ ] Guardar también como PDF los **Terms of Service** y la página de **sub-encargados** (`/trust/subprocessors`).
3. [ ] Verificar en el panel (cloud.digitalocean.com → Settings) qué correo es el titular de la cuenta y anotarlo en el registro (es la "parte" que aceptó los ToS).
4. [ ] (Opcional) Enviar un correo breve a privacy@digitalocean.com pidiendo confirmación escrita de que el DPA vigente aplica a la cuenta del CMC — útil como evidencia bilateral, no indispensable.
5. [ ] Completar la fila de DigitalOcean en el registro interno (sección 6).

### Qué anotar en el registro interno (RAT)

| Campo | Valor |
|---|---|
| Encargado | DigitalOcean, LLC (EE. UU.) |
| Servicio | Hosting VPS (droplet 157.245.13.107) — base local del portal, bot y respaldos |
| Documento | Data Processing Agreement (anexo de los Customer ToS) |
| Versión archivada | 6 de febrero de 2026 *(verificar al descargar)* |
| Aceptación | Automática con los ToS al crear/usar la cuenta |
| Transferencia internacional | EE. UU. — garantía: SCC UE Módulo 2 incorporadas al DPA (arts. 27-28) |
| Sub-encargados | Según página pública /trust/subprocessors (copia archivada) |
| Fecha de archivo y responsable | *(completar por el responsable)* |

---

## 2. Meta / WhatsApp Business Cloud API (canal de mensajería del bot)

### Qué documentos aplican (son tres, encadenados)

1. **WhatsApp Business Platform — Cloud API Terms**: https://www.facebook.com/legal/WhatsApp-Business-Platform-Cloud-API
   Rigen el uso del Cloud API (el bot del CMC usa Cloud API con la app 804421499380432). Definen que Meta **hostea y procesa** los mensajes por cuenta de la empresa.
2. **Meta Global Processor Terms (MGPT)** — incorporados por referencia en el Exhibit A de los Cloud API Terms. Son el "contrato de encargo" propiamente tal: Meta actúa como **encargado (processor)** y la empresa como **responsable (controller)**.
3. **WhatsApp Business Data Processing Terms**: https://www.whatsapp.com/legal/business-data-processing-terms (versión vigente verificada: **22 de agosto de 2025**) — aplican al tratamiento que WhatsApp hace como encargado bajo los Business Terms, e incorporan los **Data Transfer Addenda** (la "carta de transferencia internacional").

### Rol de Meta y entidad contratante

- En los Cloud API Terms: *"Meta Platforms, Inc., if you are located in the United States, Canada, or Brazil, or (b) Meta Platforms Ireland Limited, if you are located elsewhere"* → para el CMC (Chile), la contraparte del Cloud API es **Meta Platforms Ireland Limited**, actuando como **encargado** del tratamiento de los mensajes.
- En los WhatsApp Business Data Processing Terms, para jurisdicciones fuera de UE/UK la entidad es **WhatsApp LLC** y el anexo de transferencia aplicable es el **Global Data Transfer Addendum** (incorporado por referencia; enlazado desde la misma página de los Data Processing Terms).

### Qué dicen (lo relevante para el CMC)

- Meta confirma que el objeto del encargo es **la provisión del Cloud API**; el CMC es el controller de los datos personales de sus pacientes que transitan por el canal (número de teléfono, contenido de mensajes: OTP, links, recordatorios).
- **Retención**: los mensajes se retienen **máximo 30 días** para operar el servicio; al dejar de usar Cloud API, Meta elimina los datos personales restantes **dentro de 90 días** (salvo obligación legal).
- **Sub-encargados**: listados en el **Exhibit B** de los Cloud API Terms (procesamiento principalmente en EE. UU., Irlanda, Dinamarca y Suecia); Meta puede sumar sub-encargados con aviso previo.
- **Seguridad**: remite a los **Data Security Terms** de WhatsApp (medidas técnicas y organizativas); compromiso de notificar violaciones de seguridad *"without undue delay"*.
- **Publicidad**: el Cloud API no usa los mensajes de WhatsApp para informar los anuncios que ve una persona.
- **Transferencia internacional (arts. 27-28 Ley 21.719)**: la garantía documentable es el conjunto **MGPT + Data Transfer Addendum** (Global Data Transfer Addendum para Chile), que replican el esquema de cláusulas contractuales tipo.

### Cómo se aceptan

**Automático.** Los Cloud API Terms (y con ellos los MGPT) se aceptan **al usar el Cloud API**: *"By continuing to access or use Cloud API after any update, you agree to be bound by it"*. Los Business Terms + Data Processing Terms se aceptaron al crear la cuenta de WhatsApp Business Platform (WABA). **No hay firma ni botón adicional en el Business Manager.**

### Pasos concretos

1. [ ] Guardar como PDF fechado los **Cloud API Terms** (facebook.com/legal/WhatsApp-Business-Platform-Cloud-API) — incluye Exhibit A (remisión a MGPT) y Exhibit B (sub-encargados).
2. [ ] Desde el Exhibit A, abrir y guardar como PDF los **Meta Global Processor Terms**.
3. [ ] Guardar como PDF los **WhatsApp Business Data Processing Terms** (whatsapp.com/legal/business-data-processing-terms) y, desde sus enlaces, el **Global Data Transfer Addendum** y los **Data Security Terms**.
4. [ ] Anotar los identificadores de la relación contractual: App ID 804421499380432, WABA del número +56 9 6661 0737, System User "Chatbotcmc-systemuser" (ID 61576699507415) — así el registro interno identifica exactamente QUÉ cuenta está amparada.
5. [ ] Todo a `cumplimiento/encargados/meta-whatsapp/` y completar la fila del registro (sección 6).

### Qué anotar en el registro interno (RAT)

| Campo | Valor |
|---|---|
| Encargado | Meta Platforms Ireland Limited (Cloud API) / WhatsApp LLC (Business Terms) |
| Servicio | WhatsApp Business Cloud API — mensajería del bot (+56 9 6661 0737) |
| Documentos | Cloud API Terms + Meta Global Processor Terms + WhatsApp Business Data Processing Terms (22-08-2025) + Global Data Transfer Addendum + Data Security Terms |
| Aceptación | Automática al usar Cloud API / al crear la WABA |
| Transferencia internacional | EE. UU. y UE — garantía: MGPT + Global Data Transfer Addendum (arts. 27-28) |
| Retención | Mensajes máx. 30 días en servicio; supresión dentro de 90 días al cesar el uso |
| Sub-encargados | Exhibit B de los Cloud API Terms (copia archivada) |
| Fecha de archivo y responsable | *(completar por el responsable)* |

---

## 3. Medilink / Healthatom (HIS — ficha clínica y citas)

### Hallazgo: SÍ existe un DPA público de Healthatom

Contra lo que se asumía en la EIPD, Healthatom **publica un "Acuerdo de Procesamiento de Datos"**:

- Página: https://www.healthatom.com/acuerdo-de-procesamiento-de-datos (el texto se carga desde https://dentalink-cdn.s3.amazonaws.com/billing/dpa.html).
- Versión vigente verificada: **actualizada al 2 de febrero de 2026**.
- Aceptación: *"Este Acuerdo de Procesamiento de Datos (…) forma parte integrante de los Términos y Condiciones"* → rige **automáticamente** con el contrato de servicio de Medilink.
- Partes y roles: cliente (CMC) = **responsable**; Healthatom (**Engenis SpA**, RUT 76.090.231-4, Av. Apoquindo 5400 of. 1801, Las Condes) = **encargado**. Contacto de privacidad: **privacy@healthatom.com**.
- Contenido relevante: infraestructura en **AWS** (RDS Aurora, ECS, S3 cifrado), cifrado TLS 1.2 en tránsito y AES-256 en reposo, respaldos diarios con retención 90 días, plan de respuesta a incidentes, derecho de auditoría del cliente, devolución/supresión de datos con retención máxima de **12 semanas** tras el término del contrato.
- Sub-encargados (autorización general, lista pública): https://dentalink-cdn.s3.us-east-1.amazonaws.com/billing/lista-subprocesadores.html — incluye **AWS (EE. UU.)**, Google, Twilio, AssemblyAI, **Anthropic, OpenAI, xAI** (modelos de IA), Zendesk, HubSpot y filiales Healthatom en Colombia/México/Ecuador/Perú/España, entre otros.

### Vacíos que igual conviene cerrar por correo

1. El DPA público cita el **RGPD europeo**; **no invoca expresamente la Ley 21.719** ni su art. 15 bis.
2. La notificación de brechas dice *"oportunamente"*, **sin plazo concreto** — el CMC necesita plazos para poder cumplir su propio deber de reporte a la Agencia (art. 14 sexies).
3. No identifica la **región/país de los servidores AWS** donde viven los datos de los pacientes del CMC (relevante para arts. 27-28).
4. Conviene una **constancia bilateral** (correo respondido) de que ese DPA rige para el contrato específico CMC-Medilink, ya que el documento público no nombra a las partes.

### Pasos concretos

1. [ ] Guardar como PDF fechado el **DPA de Healthatom** (dentalink-cdn.s3.amazonaws.com/billing/dpa.html), la **lista de sub-encargados**, la **Política General de Seguridad de la Información** (healthatom.com/politica-general-de-seguridad-de-la-informacion) y los **Términos del servicio** (healthatom.com/terminos). Todo a `cumplimiento/encargados/healthatom-medilink/`.
2. [ ] Enviar el correo de abajo a **privacy@healthatom.com** con copia a **soportemedilink@healthatom.com** (teléfono soporte Chile: +56 2 3210 1344, por si no responden).
3. [ ] Archivar la respuesta (correo completo, con encabezados) en la misma carpeta.
4. [ ] Si en 15 días hábiles no hay respuesta, reenviar y dejar constancia del reenvío (el esfuerzo de diligencia también se acredita).
5. [ ] Completar la fila del registro (sección 6).

### Correo listo para enviar

> **Para:** privacy@healthatom.com
> **CC:** soportemedilink@healthatom.com
> **Asunto:** Centro Médico Carampangue — Solicitud de formalización de encargo de tratamiento de datos (Ley 21.719, art. 15 bis)

Estimado equipo de Healthatom:

Les escribo en representación del **Centro Médico Carampangue** (Carampangue, comuna de Arauco, Región del Biobío), cliente de la plataforma **Medilink**, que utilizamos como sistema de gestión clínica (ficha clínica, agenda y citas de nuestros pacientes).

En el marco de la implementación de la **Ley N.º 21.719 de Protección de Datos Personales** y de la evaluación de impacto que estamos realizando como responsables del tratamiento, necesitamos dejar formalizada la relación de encargo de tratamiento con Healthatom conforme al **artículo 15 bis** de dicha ley. Hemos revisado el "Acuerdo de Procesamiento de Datos" publicado en su sitio web (versión del 2 de febrero de 2026) y, sobre esa base, les solicitamos lo siguiente:

1. **Contrato o anexo de encargo conforme a la Ley 21.719.** Confirmación escrita de que el Acuerdo de Procesamiento de Datos publicado rige para el contrato entre Healthatom (Engenis SpA) y el Centro Médico Carampangue, identificando a ambas partes. Dado que el acuerdo publicado se estructura en torno al RGPD europeo, agradeceremos indicar si cuentan (o tienen prevista) una versión o anexo que recoja expresamente las obligaciones del artículo 15 bis de la Ley 21.719 para clientes chilenos, considerando además que se trata de **datos de salud** (arts. 16 y 16 bis).

2. **Medidas de seguridad.** Descripción vigente de las medidas técnicas y organizativas aplicadas a los datos de nuestros pacientes (entendemos del anexo publicado: cifrado TLS 1.2 en tránsito y AES-256 en reposo, respaldos diarios, control de acceso y registro de actividad), junto con cualquier certificación o auditoría externa que las respalde.

3. **Sub-encargados y ubicación de servidores.** Confirmación de la lista vigente de sub-encargados que intervienen en el servicio Medilink para nuestros datos, y en particular **el país y la región de AWS donde se alojan y respaldan los datos de nuestros pacientes**, para documentar la eventual transferencia internacional conforme a los artículos 27 y 28 de la Ley 21.719 y las garantías que la amparan. Agradeceremos también aclarar si los datos clínicos de nuestros pacientes son tratados por los proveedores de modelos de IA que figuran en su lista pública de sub-procesadores y, de ser así, bajo qué condiciones.

4. **Protocolo de notificación de vulneraciones.** El acuerdo publicado indica que las brechas se notifican "oportunamente", sin plazo definido. Como responsables debemos reportar a la Agencia de Protección de Datos Personales "sin dilaciones indebidas" (art. 14 sexies), por lo que necesitamos que nos indiquen: **plazo máximo comprometido de notificación** desde que Healthatom detecta un incidente que afecte nuestros datos (idealmente no superior a 72 horas), canal de contacto de emergencia y contenido mínimo del reporte (naturaleza del incidente, datos y titulares afectados, medidas adoptadas).

5. **Plazo de respuesta.** Agradeceremos acusar recibo de esta solicitud y entregarnos una respuesta dentro de **15 días hábiles**. Quedamos disponibles para una reunión breve si les resulta más expedito.

Esta solicitud forma parte de nuestro proceso ordinario de cumplimiento; valoramos la plataforma y el objetivo es simplemente dejar la relación documentada como la ley lo exige a ambas partes.

Saludos cordiales,

**Dr. Rodrigo Olavarría**
Director — Centro Médico Carampangue
Carampangue, Arauco, Región del Biobío
*(completar teléfono y correo institucional del CMC)*

### Qué anotar en el registro interno (RAT)

| Campo | Valor |
|---|---|
| Encargado | Healthatom / Engenis SpA, RUT 76.090.231-4 (Chile) |
| Servicio | Medilink — HIS: ficha clínica, agenda, citas (el portal solo lee vía API) |
| Documento | Acuerdo de Procesamiento de Datos (parte de los T&C) — versión 02-02-2026 + respuesta al correo de formalización |
| Aceptación | Automática con los Términos y Condiciones del servicio; constancia bilateral por correo *(pendiente)* |
| Transferencia internacional | Infraestructura AWS (EE. UU. según lista de sub-encargados) — **región por confirmar por correo** |
| Sub-encargados | Lista pública archivada (AWS, Google, Twilio, Anthropic, OpenAI, xAI, filiales, etc.) |
| Brechas | "Oportunamente" — **plazo concreto por confirmar por correo** |
| Fecha de archivo y responsable | *(completar por el responsable)* |

---

## 4. Anthropic (API Claude — transcripción/estructuración de exámenes y detección de intención del bot)

### Qué documentos aplican

- **Commercial Terms of Service**: aceptados al crear la organización en Claude Console (platform.claude.com). Rigen todo uso de la API.
- **Data Processing Addendum (DPA)**: https://www.anthropic.com/legal/data-processing-addendum — **incorporado automáticamente** a los Commercial Terms. Según el propio centro de privacidad de Anthropic: *"Anthropic's DPA with Standard Contractual Clauses (SCCs) is automatically incorporated into our Commercial Terms of Service"*. **No hay proceso de firma separado.**
- Documentación de retención de la API: https://platform.claude.com/docs/en/manage-claude/api-and-data-retention y Trust Center: https://trust.anthropic.com

### Qué dicen (lo relevante para el CMC)

- **Rol**: Anthropic actúa como **encargado (processor)** de los datos enviados por la API; el CMC es el responsable.
- **Entrenamiento**: bajo los Commercial Terms, **los inputs/outputs de la API NO se usan para entrenar modelos** por defecto. **No hay que activar ningún opt-out: es la condición estándar de todo cliente comercial de la API** (a diferencia de las cuentas de consumidor de claude.ai, que tienen toggles propios).
- **Retención**: el contenido de las conversaciones (prompts y respuestas) **no se retiene por defecto** tras devolver la respuesta; los registros operativos siguen la política comercial de retención (acortada a un mínimo; ver el artículo "How long do you store my organization's data" en privacy.claude.com). Excepciones: contenido marcado por sistemas de confianza y seguridad (hasta 2 años) y **modelos "Covered"** (Claude Fable 5 / Mythos 5), que exigen retención de 30 días — los modelos Haiku/Sonnet usados por el bot y la transcripción no están en esa categoría.
- **Zero Data Retention (ZDR)**: existe como arreglo formal por organización, se solicita al equipo de ventas (claude.com/contact-sales). Para el volumen del CMC probablemente no lo concedan de inmediato, pero **pedirlo deja constancia de diligencia**; la alternativa práctica es documentar la retención mínima por defecto.
- **Transferencia internacional**: Anthropic PBC es de EE. UU.; la garantía documentable son las **SCC incluidas en el DPA** (arts. 27-28 Ley 21.719).
- Dato adicional: la API ofrece un régimen "HIPAA-ready" con BAA ejecutable desde Claude Console (Settings → Privacy). HIPAA es normativa de EE. UU. y no sustituye la Ley 21.719, pero si la organización del CMC es elegible, ejecutar el BAA estándar suma salvaguardas documentadas para datos de salud (cifrado, control de acceso, auditoría). Evaluar con calma: una vez activado es permanente para esa organización y bloquea funciones no elegibles.

### Pasos concretos

1. [ ] Guardar como PDF fechado el **DPA** (anthropic.com/legal/data-processing-addendum) y los **Commercial Terms of Service** (enlazados desde la misma sección legal).
2. [ ] Guardar como PDF la página de **retención de la API** (platform.claude.com/docs/en/manage-claude/api-and-data-retention) — es la evidencia de "no retención de contenido por defecto" y "no entrenamiento".
3. [ ] En **Claude Console → Settings** verificar y anotar: nombre de la organización, correo del administrador y qué API keys usa el CMC (bot + transcripción de exámenes). Revisar Settings → Privacy por los controles de retención disponibles para la organización.
4. [ ] (Opcional, recomendado como diligencia) Escribir a ventas (claude.com/contact-sales) solicitando **ZDR** para la organización del CMC, explicando que se procesan datos de salud; archivar la respuesta aunque sea negativa.
5. [ ] (Opcional) Evaluar la activación del **BAA/HIPAA-ready** desde Console si la organización es elegible — leer antes la HIPAA Implementation Guide del Trust Center y tener presente que es irreversible.
6. [ ] Todo a `cumplimiento/encargados/anthropic/` y completar la fila del registro (sección 6).

### Qué anotar en el registro interno (RAT)

| Campo | Valor |
|---|---|
| Encargado | Anthropic, PBC (EE. UU.) |
| Servicio | API Claude — detección de intención del bot y transcripción/estructuración de exámenes |
| Documento | Data Processing Addendum (incorporado a los Commercial Terms of Service) |
| Aceptación | Automática al aceptar los Commercial ToS (creación de la organización en Console) |
| Entrenamiento | Inputs/outputs de la API NO se usan para entrenamiento (condición estándar comercial) |
| Retención | Contenido no retenido por defecto; ZDR solicitado: sí/no *(completar)* |
| Transferencia internacional | EE. UU. — garantía: SCC incorporadas al DPA (arts. 27-28) |
| Fecha de archivo y responsable | *(completar por el responsable)* |

---

## 5. Nota sobre transferencias internacionales (cierra también G4)

Con los cuatro archivos anteriores queda documentado, para cada flujo hacia el extranjero, **qué garantía lo ampara** (SCC de DigitalOcean, MGPT + Global Data Transfer Addendum de Meta, SCC del DPA de Anthropic, y la confirmación de región AWS pendiente de Healthatom). Pasos finales:

1. [ ] Dejar en la carpeta de cumplimiento una hoja de una página ("Evaluación de transferencias internacionales") que liste proveedor → país → garantía → documento archivado. Puede ser literalmente la tabla de la sección 6 impresa.
2. [ ] Actualizar la política de privacidad (`/privacidad`) para **informar las transferencias internacionales al titular** (art. 14 ter) — ya está identificado como G14/P2 en la EIPD; al menos nombrar a Meta, DigitalOcean y Anthropic como destinatarios en el extranjero con garantías contractuales.

---

## 6. Tabla resumen — plan de cierre (completar por el responsable)

Todos los campos "Responsable" y "Fecha objetivo" quedan **por completar por el responsable** designado (en una EMT puede ser el propio dueño, cf. G9 de la EIPD).

| Proveedor | Documento que formaliza el encargo | Estado / acción | Responsable | Fecha objetivo | Hecho |
|---|---|---|---|---|---|
| DigitalOcean | DPA (anexo de los ToS) — SCC Módulo 2 incluidas | **Automático** — descargar PDF fechado + sub-encargados y archivar | *(completar)* | *(completar)* | ☐ |
| Meta / WhatsApp Cloud API | Cloud API Terms + Meta Global Processor Terms + WA Business Data Processing Terms + Global Data Transfer Addendum | **Automático** — descargar los 4 PDF fechados + anotar App ID/WABA | *(completar)* | *(completar)* | ☐ |
| Healthatom / Medilink | Acuerdo de Procesamiento de Datos (parte de los T&C, v. 02-02-2026) + respuesta al correo | **Automático + PEDIR** — archivar DPA público y **enviar correo** de la sección 3 (constancia bilateral, plazo de brechas, región AWS, anexo 21.719) | *(completar)* | *(completar)* | ☐ |
| Anthropic | Data Processing Addendum (incorporado a Commercial ToS) | **Automático** — descargar DPA + página de retención; **opcional**: solicitar ZDR / evaluar BAA | *(completar)* | *(completar)* | ☐ |
| Transversal | Hoja de evaluación de transferencias (arts. 27-28) + actualización de `/privacidad` (art. 14 ter) | **Redactar** con lo archivado arriba | *(completar)* | *(completar)* | ☐ |

**Criterio de "listo":** la medida P1/G3 se considera cerrada cuando (a) las cuatro carpetas de proveedor tienen sus PDF fechados, (b) Healthatom respondió (o hay constancia de dos intentos), y (c) la tabla de arriba está firmada/fechada por el responsable.

---

## Fuentes consultadas (14-07-2026)

- DigitalOcean DPA: https://www.digitalocean.com/legal/data-processing-agreement (act. 06-02-2026) · Schrems II FAQ: https://www.digitalocean.com/trust/schrems-ii-faq · Sub-encargados: https://www.digitalocean.com/trust/subprocessors
- Meta: Cloud API Terms: https://www.facebook.com/legal/WhatsApp-Business-Platform-Cloud-API · WhatsApp Business Data Processing Terms (22-08-2025): https://www.whatsapp.com/legal/business-data-processing-terms
- Healthatom: DPA (02-02-2026): https://www.healthatom.com/acuerdo-de-procesamiento-de-datos (contenido: https://dentalink-cdn.s3.amazonaws.com/billing/dpa.html) · Sub-encargados: https://dentalink-cdn.s3.us-east-1.amazonaws.com/billing/lista-subprocesadores.html · Contacto: privacy@healthatom.com / soportemedilink@healthatom.com
- Anthropic: DPA: https://www.anthropic.com/legal/data-processing-addendum · "How do I view and sign your DPA": https://privacy.claude.com/en/articles/7996862 · Retención API: https://platform.claude.com/docs/en/manage-claude/api-and-data-retention · Trust Center: https://trust.anthropic.com

> **Recordatorio final: este documento es un borrador de apoyo operativo y no constituye asesoría legal.** Verificar las versiones vigentes de cada documento al momento de archivarlas.
