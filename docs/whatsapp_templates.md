# WhatsApp Message Templates — Centro Médico Carampangue

## Por qué se necesitan

WhatsApp Business API tiene una **ventana de 24 horas**: solo puedes enviar mensajes
free-form dentro de las 24h desde el último mensaje del paciente. Después de 24h,
SOLO puedes enviar **Message Templates** aprobados por Meta.

Todos los mensajes proactivos del bot (recordatorios, fidelización, lista de espera)
caen fuera de esta ventana.

**Excepción**: el reenganche (10-60 min después de que el paciente abandonó el flujo)
SÍ está dentro de la ventana, no necesita template.

## Categorías Meta

- **UTILITY**: transaccionales (recordatorios de cita, notificaciones de servicio)
- **MARKETING**: re-engagement, promociones, cross-sell

UTILITY tiene mejor tasa de entrega y no requiere opt-out.
MARKETING requiere mecanismo de opt-out (botón "No enviar más" o similar).

---

## Templates a registrar

### 1. `recordatorio_cita` (UTILITY)

Recordatorio 24h antes de la cita — con 3 botones de confirmación.
Incluye nombre del paciente y dirección (como el que enviaba Medilink).

**Body:**
```
Hola {{1}} 👋 Te recordamos tu cita en el *Centro Médico Carampangue*:

🏥 *{{2}}* — {{3}}
📅 *{{4}}* a las *{{5}}*
💳 {{6}}
📍 Monsalve esquina República, Carampangue

Recuerda llegar *15 minutos antes* con tu cédula de identidad.

¿Nos confirmas tu asistencia?
```

**Variables:**
| # | Descripción | Ejemplo |
|---|-------------|---------|
| {{1}} | Nombre paciente | Sergio Carrasco |
| {{2}} | Especialidad | Odontología General |
| {{3}} | Profesional | Dra. Javiera Burgos Godoy |
| {{4}} | Fecha display | Lunes 13 de abril |
| {{5}} | Hora | 10:00 |
| {{6}} | Modalidad | Particular |

**Buttons (QUICK_REPLY):**
| Index | Texto | Payload (dinámico) |
|-------|-------|--------------------|
| 0 | ✅ Confirmo | `cita_confirm:{id_cita}` |
| 1 | 🔄 Cambiar hora | `cita_reagendar:{id_cita}` |
| 2 | ❌ No podré ir | `cita_cancelar:{id_cita}` |

---

### 2. `recordatorio_cita_2h` (UTILITY)

Recordatorio corto 2 horas antes de la cita (sin botones).

**Body:**
```
Hola {{1}} ⏰ *En 2 horas* tienes tu cita en el *Centro Médico Carampangue*:

🏥 *{{2}}* — {{3}}
🕐 Hoy a las *{{4}}*
📍 Monsalve esquina República, Carampangue

Recuerda llegar *15 minutos antes* con tu cédula de identidad.
```

**Variables:**
| # | Descripción | Ejemplo |
|---|-------------|---------|
| {{1}} | Nombre paciente | Sergio Carrasco |
| {{2}} | Especialidad | Kinesiología |
| {{3}} | Profesional | Luis Armijo |
| {{4}} | Hora | 14:00 |

**Buttons:** ninguno

---

### 3. `postconsulta_seguimiento` (UTILITY)

Seguimiento 24h después de la cita — 3 botones de feedback.

**Body:**
```
Hola {{1}} 😊 ¿Cómo te sientes después de tu consulta de *{{2}}* con *{{3}}*?

Tu opinión nos ayuda a mejorar 🙏
```

**Variables:**
| # | Descripción | Ejemplo |
|---|-------------|---------|
| {{1}} | Nombre corto | *María* |
| {{2}} | Especialidad | Traumatología |
| {{3}} | Profesional | Dr. Claudio Barraza |

**Buttons (QUICK_REPLY):**
| Index | Texto | Payload |
|-------|-------|---------|
| 0 | Mejor 😊 | `seg_mejor` |
| 1 | Igual 😐 | `seg_igual` |
| 2 | Peor 😟 | `seg_peor` |

---

### 4. `reactivacion_paciente` (MARKETING)

Re-engagement para pacientes inactivos 30-90 días.

**Body:**
```
Hola {{1}} 👋 Hace un tiempo no te vemos en el *Centro Médico Carampangue* 🏥

¿Quieres retomar tu atención de *{{2}}*? Puedo ayudarte a agendar ahora mismo.
```

**Variables:**
| # | Descripción | Ejemplo |
|---|-------------|---------|
| {{1}} | Nombre corto | *Pedro* |
| {{2}} | Especialidad | Kinesiología |

**Buttons (QUICK_REPLY):**
| Index | Texto | Payload |
|-------|-------|---------|
| 0 | Sí, agendar | `reac_si` |
| 1 | No, gracias | `reac_luego` |

**Footer:** `Responde STOP para no recibir más mensajes`

---

### 5. `adherencia_kine` (UTILITY)

Recordatorio de continuidad para pacientes de kinesiología (4+ días sin sesión).

**Body:**
```
Hola {{1}} 💪 Para que tu tratamiento de kinesiología funcione bien, es importante mantener continuidad en las sesiones.

¿Quieres que te ayude a agendar la próxima?
```

**Variables:**
| # | Descripción | Ejemplo |
|---|-------------|---------|
| {{1}} | Nombre corto | *Juan* |

**Buttons (QUICK_REPLY):**
| Index | Texto | Payload |
|-------|-------|---------|
| 0 | Sí, agendar | `kine_adh_si` |
| 1 | Más adelante | `kine_adh_no` |

---

### 6. `control_especialidad` (UTILITY)

Recordatorio de control periódico por especialidad.

**Body:**
```
Hola {{1}} 😊 Ya va correspondiendo tu control de *{{2}}* 📅

Hacer el seguimiento a tiempo hace la diferencia. ¿Quieres ver horarios disponibles?
```

**Variables:**
| # | Descripción | Ejemplo |
|---|-------------|---------|
| {{1}} | Nombre corto | *Ana* |
| {{2}} | Especialidad | Nutrición |

**Buttons (QUICK_REPLY):**
| Index | Texto | Payload |
|-------|-------|---------|
| 0 | Sí, ver horarios | `ctrl_si` |
| 1 | No por ahora | `ctrl_no` |

---

### 7. `crosssell_kine` (MARKETING)

Sugerencia de kinesiología para pacientes de medicina/traumatología.

**Body:**
```
Hola {{1}} 😊 Muchas veces, tras una consulta de medicina o traumatología se recomienda continuar con kinesiología para avanzar mejor.

¿Te gustaría agendar con nuestros kinesiólogos?
```

**Variables:**
| # | Descripción | Ejemplo |
|---|-------------|---------|
| {{1}} | Nombre corto | *Carlos* |

**Buttons (QUICK_REPLY):**
| Index | Texto | Payload |
|-------|-------|---------|
| 0 | Sí, me interesa | `xkine_si` |
| 1 | No por ahora | `xkine_no` |

**Footer:** `Responde STOP para no recibir más mensajes`

---

### 8. `lista_espera_cupo` (UTILITY)

Notificación cuando se libera un cupo en la lista de espera.

**Body:**
```
Hola {{1}} 👋

¡Buenas noticias! Se liberó un cupo para *{{2}}*.

📅 Primera hora disponible: *{{3}} a las {{4}}*

Si quieres agendarla escribe *menu* y te ayudo al tiro 😊

_Te escribimos porque estás en nuestra lista de espera._
```

**Variables:**
| # | Descripción | Ejemplo |
|---|-------------|---------|
| {{1}} | Nombre corto | Pedro |
| {{2}} | Especialidad | Traumatología |
| {{3}} | Fecha | 2026-04-15 |
| {{4}} | Hora | 10:30 |

**Buttons:** ninguno (el paciente responde "menu")

---

### 9. `sistema_recuperado` (UTILITY)

Notificación al paciente cuando Medilink se recupera tras una caída.

**Body:**
```
✅ ¡Buenas noticias! Nuestro sistema de citas ya está operativo de nuevo 🎉

Si quieres retomar lo que estabas haciendo, escribe *menu* y te ayudo al tiro.

_Gracias por tu paciencia._
```

**Variables:** ninguna

**Buttons:** ninguno

---

### 10. `alerta_tecnica_admin` (UTILITY)

Alerta interna a recepción cuando Medilink está caído.

**Body:**
```
⚠️ *Alerta técnica CMC bot*

Medilink no responde desde las {{1}}.
Pacientes esperando: *{{2}}*

El bot avisó a cada paciente y les pedirá escribir cuando el sistema esté operativo.
```

**Variables:**
| # | Descripción | Ejemplo |
|---|-------------|---------|
| {{1}} | Hora caída | 14:30 UTC |
| {{2}} | Cantidad en cola | 3 |

**Buttons:** ninguno

---

### 11. `sistema_recuperado_admin` (UTILITY)

Aviso a recepción de que Medilink se recuperó.

**Body:**
```
✅ *Medilink recuperado*

El bot ya está operativo. Se avisó a {{1}} paciente(s) que estaban esperando.
```

**Variables:**
| # | Descripción | Ejemplo |
|---|-------------|---------|
| {{1}} | Cantidad notificados | 3 |

**Buttons:** ninguno

---

### 12. `informe_listo` (UTILITY)

Notificación al paciente de que su informe/resultado está disponible.
Se usa fuera de la ventana 24h para que el paciente responda y abra la ventana,
permitiendo después enviar el documento.

**Body:**
```
Hola {{1}} 👋 Tu informe de *{{2}}* ya está disponible.

Responde a este mensaje y te lo enviamos por aquí 📄
```

**Variables:**
| # | Descripción | Ejemplo |
|---|-------------|---------|
| {{1}} | Nombre paciente | Sergio |
| {{2}} | Tipo de examen | Ecografía |

**Buttons:** ninguno (el paciente responde libre)

---

### 13. `seguimiento_medico` (UTILITY)

Seguimiento médico personalizado — el doctor quiere saber cómo evoluciona
un paciente días después de la consulta (ej. hepatitis, lesión, post-operatorio).
Se envía desde el panel admin.

**Body:**
```
Hola {{1}} 👋 El *{{2}}* del Centro Médico Carampangue quiere saber cómo has evolucionado desde tu última consulta.

¿Cómo te has sentido? ¿Algún síntoma nuevo o cambio?

Responde a este mensaje y te orientamos 🙏
```

**Variables:**
| # | Descripción | Ejemplo |
|---|-------------|---------|
| {{1}} | Nombre paciente | Sergio |
| {{2}} | Nombre doctor | Dr. Rodrigo Olavarría |

**Buttons:** ninguno (el paciente responde libre, abre ventana 24h)

---

## Cómo registrar los templates

### Opción A — Meta Business Manager (UI)

1. Ir a [business.facebook.com](https://business.facebook.com)
2. WhatsApp Manager → Account Settings → Message Templates
3. Crear cada template con los datos de arriba
4. Esperar aprobación (24-48h)

### Opción B — Script automatizado (recomendado)

```bash
# Requiere WABA_ID en .env (ver instrucciones abajo)
PYTHONPATH=app:. python scripts/register_templates.py
```

Para obtener el WABA_ID:
1. Ir a Meta Business Manager → WhatsApp Manager → Phone Numbers
2. El WABA ID aparece en la URL: `business.facebook.com/wa/manage/phone-numbers/?waba_id=XXXXX`
3. Agregarlo al `.env`: `META_WABA_ID=XXXXX`

---

## Cambios en el código del bot

Una vez aprobados los templates, hay que modificar el bot para usar `send_whatsapp_template()`
en vez de `send_whatsapp()` / `send_whatsapp_interactive()` para todos los mensajes proactivos.

La función `send_whatsapp_template()` se agrega en `app/messaging.py` y recibe:
- `to`: teléfono destino
- `template_name`: nombre del template
- `language`: "es"
- `components`: variables + payloads de botones

Los payloads de los botones QUICK_REPLY se asignan dinámicamente al enviar
(ej. `cita_confirm:9001`), así que el flujo actual de handlers no cambia.

Ver `scripts/register_templates.py` para el detalle de la función de envío.
