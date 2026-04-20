# Política de Privacidad — Centro Médico Carampangue (CMC)

**Versión 1.0 · Vigente desde 2026-04-16**

Esta política cumple con la **Ley 19.628 sobre Protección de la Vida Privada** (Chile) y su reforma de 2024 (Ley 21.719) que entra en vigencia plena en diciembre 2026.

---

## 1. Responsable del tratamiento

**Centro Médico Carampangue SpA**
- Dirección: [DIRECCIÓN FÍSICA DEL CMC — completar]
- RUT: [RUT DE LA SOCIEDAD — completar]
- Representante legal: Dr. Rodrigo Olavarría de la Vega
- Email DPO: privacidad@centromedicocarampangue.cl *(pendiente activar buzón)*
- Teléfono: +56966610737

El CMC actúa como **responsable** del tratamiento de los datos personales que se recolectan a través de:
- Chatbot WhatsApp (+56945886628 — en migración a +56966610737)
- Panel admin interno (`/admin`)
- Portal del paciente (`/portal`)
- Sitio web (`centromedicocarampangue.cl` y `agentecmc.cl`)
- Landing page (`agentecmc.cl/landing`)

---

## 2. Datos que recolectamos

| Categoría | Datos específicos | Fuente |
|---|---|---|
| Identificación | Nombre, RUT, fecha de nacimiento, sexo, comuna, email | Registro del paciente |
| Contacto | Número de WhatsApp, dirección | Meta Cloud API + paciente |
| Conversación | Mensajes de texto, audios transcritos (Whisper), imágenes, PDFs | WhatsApp, Instagram, Messenger |
| Clínicos | Especialidad agendada, profesional, motivo de consulta, diagnósticos crónicos inferidos (HTA, DM2, asma, etc.) | Conversación + tags automáticos |
| Comportamiento | Historial de citas, asistencia, NPS, fecha de consulta, abandonos de flujo | Registro interno |
| Técnicos | ID de mensaje WhatsApp, timestamps, estado de entrega, tipo de dispositivo | Meta Cloud API |

**No recolectamos** (garantía expresa):
- Datos bancarios ni medios de pago
- Geolocalización en tiempo real (solo dirección declarada)
- Datos de otros servicios de salud fuera del CMC

---

## 3. Finalidad del tratamiento

Los datos se usan exclusivamente para:

1. **Coordinar atención médica**: agendar, cancelar, reagendar citas en el sistema clínico Medilink.
2. **Recordatorios y confirmaciones**: avisos 24 h y 2 h antes de la cita.
3. **Seguimiento post-consulta**: preguntas de evolución (mejor/igual/peor), recordatorios de control.
4. **Programas preventivos**: calendario de vacunas PNI, campañas estacionales (influenza, chequeo preventivo).
5. **Mejora del servicio**: análisis agregado (no individual) de flujos conversacionales y experiencia.
6. **Obligaciones legales**: ficha clínica (Ley 20.584), reportes ISP cuando corresponda.

**No usamos los datos para**:
- Venta o cesión a terceros con fines comerciales
- Publicidad de productos ajenos al CMC
- Perfilamiento automatizado con efectos jurídicos significativos

---

## 4. Base legal

Conforme al art. 12 de la Ley 19.628 (reformada), el tratamiento se realiza bajo las siguientes bases:

- **Consentimiento explícito**: aceptación registrada al primer mensaje ("¿Aceptas que procesemos tus datos?").
- **Relación contractual**: al agendar una consulta, el tratamiento es necesario para ejecutar la prestación de salud solicitada.
- **Obligación legal**: ficha clínica obligatoria (Ley 20.584), reporte sanitario al MINSAL/ISP.
- **Interés vital**: atención de emergencias (SAMU 131) — en este caso el tratamiento procede sin consentimiento previo.

---

## 5. Retención de datos

| Categoría | Plazo mínimo | Plazo máximo | Justificación |
|---|---|---|---|
| Ficha clínica | 15 años | Indefinido con anonimización | Ley 20.584 + requisitos sanitarios |
| Conversación WhatsApp | 24 meses | 60 meses | Continuidad del cuidado + cross-referencias médicas |
| Audios originales (pre-Whisper) | 7 días | 30 días | Solo se conserva la transcripción textual |
| Datos de marketing (NPS, campañas) | 12 meses | 24 meses | Análisis de retención del paciente |
| Consent records (`privacy_consents`) | Durante la relación + 6 años | — | Prueba de cumplimiento |
| Audit log de borrados (`gdpr_deletions`) | Indefinido | — | Prueba legal de derecho al olvido |

Los datos que excedan el plazo máximo son **anonimizados** (removiendo nombre, RUT, teléfono) y conservados solo para estadísticas agregadas.

---

## 6. Transferencias a terceros

Compartimos datos mínimos e indispensables con los siguientes proveedores. Todos han firmado (o deben firmar) un Acuerdo de Procesamiento de Datos (DPA):

| Proveedor | Datos compartidos | Finalidad | País | DPA |
|---|---|---|---|---|
| **Meta Platforms (WhatsApp)** | Mensajes, ID de usuario, teléfono | Entrega de mensajería | Irlanda/EEUU | Pendiente firma formal |
| **Anthropic** (Claude Haiku) | Texto del mensaje del paciente | Detección de intención + FAQ | EEUU | Pendiente firma formal |
| **OpenAI** (Whisper) | Audios de WhatsApp | Transcripción a texto | EEUU | Pendiente firma formal |
| **healthatom** (Medilink) | RUT, nombre, fecha, celular, motivo | Sistema clínico agendamiento | Chile | Firmado (al contratar Medilink) |
| **DigitalOcean** | Base de datos SQLite encriptada | Hosting del chatbot | EEUU (datacenter NYC3) | SOC 2 / ISO 27001 |
| **Cloudflare** | Headers HTTP, IP | DNS y CDN del sitio web | EEUU | SOC 2 |
| **Vercel** | Logs del frontend GES | Hosting del asistente clínico | EEUU | SOC 2 |
| **Google Analytics** | Eventos web anónimos | Analítica agregada (sitio web) | EEUU | Firmado en Console |

**No hay** transferencias a terceros con fines comerciales propios del tercero.

---

## 7. Derechos del titular (derechos ARCOP)

Conforme al art. 12 de la Ley 19.628 (reformada 2024), tienes los siguientes derechos:

- **Acceso**: solicitar copia de todos tus datos en 10 días hábiles.
- **Rectificación**: corregir datos inexactos o desactualizados.
- **Cancelación / Supresión** ("derecho al olvido"): eliminar tus datos cuando no sean necesarios o hayan sido tratados ilegítimamente.
- **Oposición**: rechazar el uso de tus datos para fines de marketing.
- **Portabilidad**: obtener tus datos en formato estructurado (JSON/CSV).

### Cómo ejercerlos

| Canal | Método |
|---|---|
| WhatsApp | Escribe *"borrar mis datos"* o *"quiero mi información"* al bot |
| Email | privacidad@centromedicocarampangue.cl |
| Teléfono | +56966610737 |
| Presencial | En recepción del CMC con tu cédula de identidad |

**Plazo de respuesta**: 10 días hábiles (prorrogables por 10 días más, notificando la prórroga).
**Costo**: gratuito.
**Validación de identidad**: obligatoria antes de ejecutar cualquier solicitud.

---

## 8. Seguridad

Medidas técnicas implementadas:

- **En tránsito**: HTTPS/TLS 1.3 en todos los endpoints (Let's Encrypt).
- **En reposo**:
  - Base de datos SQLite encriptada con **SQLCipher (AES-256)** desde 2026-04-16. La key vive en `/opt/chatbot-cmc/.env` con permisos 600. Detalles técnicos en `docs/encryption_at_rest.md`.
  - Backup semanal encriptado (AES-256) en `/opt/backups/`.
- **Autenticación**:
  - Panel admin: token Bearer + cookie firmada HMAC-SHA256 (7 días).
  - Portal paciente: OTP por SMS/WhatsApp.
  - SSH al servidor: solo llave pública Ed25519, password deshabilitado.
- **Rate limiting**: 30 mensajes/min por teléfono (anti-spam).
- **Aislamiento**: circuit breaker ante fallos de proveedores.
- **Audit logging**: eventos clave (agendamiento, borrado, consent) registrados con timestamp.

**Breach notification**: en caso de incidente de seguridad que afecte datos personales, notificaremos a la Agencia de Protección de Datos Personales dentro de 72 horas (art. 32 de la reforma 2024) y a los titulares afectados cuando el riesgo sea alto.

---

## 9. Menores de edad

Si eres menor de 14 años, necesitas que un adulto responsable consienta en tu nombre. El CMC atiende pacientes pediátricos y en esos casos el consentimiento lo otorga el padre, madre o tutor legal.

---

## 10. Cambios a esta política

Nos reservamos el derecho de actualizar esta política. Cuando haya cambios sustantivos:

1. Incrementaremos el campo `PRIVACY_POLICY_VERSION` en el código.
2. Solicitaremos **re-consentimiento explícito** a todos los pacientes activos.
3. Publicaremos el nuevo texto en `agentecmc.cl/privacidad` con la fecha.

---

## 11. Reclamos

Si consideras que tus derechos han sido vulnerados, puedes:

1. **Reclamar al CMC**: privacidad@centromedicocarampangue.cl
2. **Reclamar ante la Agencia de Protección de Datos Personales** (entra en funcionamiento pleno en diciembre 2026).
3. **Acudir a tribunales civiles** para indemnización conforme al art. 23 de la Ley 19.628.

---

## 12. Contacto DPO (Delegado de Protección de Datos)

**Dr. Rodrigo Olavarría de la Vega** (responsable interino hasta designación formal)
- Email: privacidad@centromedicocarampangue.cl
- Teléfono: +56966610737

---

*Esta política es un documento vivo. Última revisión: 2026-04-16.*
