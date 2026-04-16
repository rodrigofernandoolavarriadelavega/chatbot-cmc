# Session Log — 2026-04-14/15 — Marketing Phase 1 + Responsive + Doctor Mode

## Conversación completa

---

### Bloque 1: Marketing Phase 1 (implementación)

**Lo que se pidió:** Implementar las 4 features de Fase 1 del plan de marketing:
1. Campaña de cumpleaños
2. Win-back >90 días
3. Mensaje de bienvenida post-registro
4. Dashboard NPS por profesional

**Lo que se hizo:**

#### 1. Campaña de cumpleaños
- `app/session.py`: migración `ALTER TABLE contact_profiles ADD COLUMN fecha_nacimiento TEXT`
- `app/session.py`: `get_cumpleanos_hoy()` — busca pacientes cuyo MM-DD es hoy, cooldown 330 días
- `app/fidelizacion.py`: `enviar_cumpleanos(send_fn)` — saludo + tips preventivos por edad:
  - 65+: EMPAM + vacuna influenza
  - 50+: chequeo preventivo anual
  - 40+: examen preventivo
- `app/jobs.py`: wrapper `_job_cumpleanos()`
- `app/main.py`: `CronTrigger(hour=8, minute=0, timezone=_CLT)` diario

#### 2. Win-back >90 días
- `app/session.py`: `get_pacientes_winback(dias_min=91, dias_max=365)` — excluye reactivados en 30d, sin cita futura
- `app/fidelizacion.py`: `enviar_winback(send_fn)` — personalizado para crónicos (lee dx:* tags)
- `app/fidelizacion.py`: `_msg_winback(paciente)` — mensaje interactivo con botón "Sí, agendar"
- `app/jobs.py`: wrapper `_job_winback()`
- `app/main.py`: `CronTrigger(day_of_week="mon", day="1-7", hour=10, minute=0, timezone=_CLT)`

#### 3. Mensaje de bienvenida
- `app/flows.py` en WAIT_REFERRAL: después de `crear_paciente()`, envía bienvenida con:
  - Qué puede hacer el bot (agendar, cancelar, consultar)
  - Dirección: Monsalve 102 esq. República, Carampangue
  - Teléfono CMC
- `save_profile()` ahora recibe `fecha_nacimiento` opcional

#### 4. NPS Dashboard
- `app/session.py`: `get_nps_por_profesional(dias=None)` — join fidelizacion_msgs + citas_bot
- `app/admin_routes.py`: `GET /admin/api/nps?dias=30`
- `templates/admin.html`:
  - Pill: `😊 NPS` con color coding (verde >=50, rojo <0)
  - Modal con tabla por profesional (mejor/igual/peor/total/NPS)
  - Fila roja si >20% peor
  - `loadNPS()` en init + `setInterval(loadNPS, 60000)`
  - Escape handler para modal-nps

**Archivos modificados:** session.py, fidelizacion.py, jobs.py, main.py, flows.py, admin_routes.py, admin.html
**Tests:** 90/90 harness + 52/52 normalizer ✅

---

### Bloque 2: Notion

**Lo que se pidió:** Guardar todas las fases del plan en Notion.

**Lo que se hizo:** Se actualizó la página "Plan Estratégico de Marketing y Fidelización — CMC (2026)" bajo "Documentación Técnica — Chatbot WhatsApp CMC". Plan completo de 5 fases con 10 secciones (activos, quick wins, referidos, estacionales, content marketing, SEO, RRSS, lifecycle, métricas, implementación).

---

### Bloque 3: Panel admin responsive

**Lo que se pidió:** "Como experto en UI y desarrollo frontend haz el panel responsive"

**Lo que se hizo:** Reescritura completa de los media queries en `templates/admin.html`:

| Breakpoint | Target | Qué hace |
|---|---|---|
| `>1024px` | Desktop | 3 columnas (filtros + conversaciones + chat/contexto) |
| `768-1024px` | Tablet | Columnas más angostas, contexto como overlay, actions wrap |
| `<768px` | Mobile | Columna única, overlays fullscreen, touch targets 44px |
| `<400px` | Celulares chicos | Spacing más apretado, filtros 85vw |
| Landscape `<500px` | Celular horizontal | Headers compactos, modals scrollables |
| `env(safe-area-inset-*)` | iPhone X+ | Padding para notch y home indicator |
| Print | Impresión | Solo chat visible |

**Detalles técnicos:**
- Todos los 7 modals se expanden a fullscreen en mobile (`width: 100vw !important; min-height: 100vh; border-radius: 0`)
- Swipe gestures: derecha → cerrar filtros o volver a lista, izquierda → cerrar contexto
- iOS zoom prevention: `font-size: 16px` en inputs, `maximum-scale=1`
- `viewport-fit=cover` para notch
- `-webkit-text-size-adjust: 100%`
- Topbar actions en fila compacta (order:10, width:100%)
- `kbd` hints ocultos en mobile
- Contexto panel: 85vw con max-width 320px
- Kine modal header: controles apilados verticalmente en mobile

**Primer deploy:** Se veía mal en celular — el topbar se rompía.

**Fix del topbar mobile:** Los botones de acción y el alert bar se superponían. Se reescribió:
- `.topbar-actions` con `display: flex !important` y `order: 10; width: 100%`
- Alert bar `#alert-bar` con `flex-wrap` para no cortar texto
- Pills con `display: none !important` por defecto, expandibles con "▼ Info"

---

### Bloque 4: Instagram y Facebook en el panel

**Lo que se preguntó:** "Donde esta ingresado en el panel los mensajes que vienen de instagram o facebook"

**Respuesta:** El flujo completo es:
1. `app/main.py` webhook: `obj == "instagram"` → `phone = f"ig_{sender_id}"`, canal="instagram". `obj == "page"` → `phone = f"fb_{sender_id}"`, canal="messenger"
2. `app/session.py` `log_message()`: guarda campo `canal` en tabla `messages`
3. `templates/admin.html` `canalIcon(canal)`: badges IG (rosa), FB (azul), WA (verde)
4. Aparecen en 3 lugares: lista conversaciones, header del chat, cada mensaje entrante
5. `app/admin_routes.py` `POST /admin/api/reply`: detecta canal por prefijo `ig_`/`fb_` y rutea a `send_instagram()`/`send_messenger()`

---

### Bloque 5: Doctor mode persistente

**El problema:** El doctor tiene 2 bots en su número (agente CMC + asistente clínico). Al escribir "Hola", el bot le pedía elegir modo cada vez, incluso si no habían pasado 30 min.

**Causa raíz:** `doctor_mode` se guardaba en `session.data` que se borra con cada `reset_session()`. Como "Hola" matchea en `_SALUDOS_SET` → ejecuta `reset_session()` → pierde el modo.

**Lo que se pidió:** "Que me mantenga en un modo hasta que ocupe la palabra clave cambiar modo, la única forma que vuelva a cambiar de modo"

**Solución implementada:** Mover `doctor_mode` de session data a **tags** (`contact_tags`):
- `_get_doctor_mode(phone)` — lee tag `doctor_mode:*`
- `_set_doctor_mode(phone, mode)` — borra tag anterior, guarda nuevo
- `_clear_doctor_mode(phone)` — elimina tag
- Tags sobreviven `reset_session()`, timeouts, cualquier cambio de estado
- "cambiar modo" / "cambiar_modo" = ÚNICA forma de volver al selector
- En modo agente: "hola/menu" muestra menú de pacientes (como cualquier teléfono)
- En modo asistente: "hola" responde "Asistente Clínico listo"
- Se revirtió la preservación en `session.py get_session()` (ya no necesaria)

**Archivos:** flows.py (nuevo bloque `_get/_set/_clear_doctor_mode` + rewrite de `_handle_doctor_command` y comandos globales), session.py (revertido)

---

### Bloque 6: Actualización de docs

**Lo que se pidió:** "Actualiza notion, md, etc"

**Lo que se hizo:**
- `CLAUDE.md`: 9 items nuevos en checklist + sesión 2026-04-15 completa
- Memory `test_suites.md`: fecha actualizada
- Notion "Plan Marketing": Fase 1 checkboxes ✅ con fechas
- Notion "Tareas Pendientes": título actualizado, sección completados reescrita con 8 items

---

### Commits de la sesión
| Commit | Descripción |
|---|---|
| `1f4d233` | feat: panel admin responsive completo — 6 breakpoints, modals fullscreen mobile, swipe gestures |
| `a72aaae` | fix: preservar doctor_mode + comando "modo" para cambiar (sesión data — approach intermedio) |
| `5fe1986` | fix: doctor_mode persistente con tags + topbar mobile arreglado |

**Estado del servidor:** ✅ `https://agentecmc.cl/health` → HTTP 200. Commit `5fe1986` deployado.

---
*Generado — sesión Claude Code 2026-04-15*
