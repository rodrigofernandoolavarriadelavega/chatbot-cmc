# Session Log — 2026-04-14 — Marketing & Fidelización Phase 1

## Resumen de la conversación

### Plan estratégico de marketing y fidelización (5 fases)

Se diseñó un plan completo basado en el ecosistema CMC existente:

**Fase 1 — Quick Wins (implementada esta sesión):**
1. Campaña de cumpleaños — cron diario 08:00 CLT
2. Win-back >90 días — cron primer lunes del mes 10:00 CLT
3. Mensaje de bienvenida post-registro
4. Dashboard NPS — pill en admin con score por profesional

**Fase 2 — Programa de referidos:**
- Código único por paciente (ej: CMC-RODRIGO-4X)
- Descuento mutuo (referidor + referido)
- Tracking automático en registro (WAIT_REFERRAL ya captura "amigo/familiar")
- Dashboard de top referidores

**Fase 3 — Campañas estacionales:**
- Invierno (mayo-agosto): vacuna influenza, checkup respiratorio
- Vuelta a clases (febrero-marzo): control pediátrico, vacunas PNI, fono
- Mes del corazón (agosto): checkup cardio, Millán
- Segmentación por tags (dx:*, edad, última visita)

**Fase 4 — Contenido y educación:**
- Tips de salud semanales por WhatsApp (segmentados por perfil)
- FAQ bot expandido con contenido educativo
- Integración con sitio web SEO (blogs por especialidad)

**Fase 5 — Análisis y optimización:**
- Funnel completo: mensaje → apertura → respuesta → cita → asistencia
- A/B testing de mensajes de fidelización
- Reportes automáticos semanales al doctor
- Predicción de churn con ML (futuro)

### Implementación Phase 1 — Archivos modificados

| Archivo | Cambio |
|---------|--------|
| `app/session.py` | +migration fecha_nacimiento, +get_cumpleanos_hoy(), +get_pacientes_winback(), +get_nps_por_profesional() |
| `app/fidelizacion.py` | +enviar_cumpleanos(), +enviar_winback(), +_calcular_edad(), +_msg_winback() |
| `app/jobs.py` | +_job_cumpleanos(), +_job_winback() |
| `app/main.py` | +2 CronTrigger (cumpleaños 08:00, winback 1er lunes 10:00) |
| `app/flows.py` | +welcome message en WAIT_REFERRAL, +fecha_nacimiento en save_profile() |
| `app/admin_routes.py` | +GET /admin/api/nps endpoint |
| `templates/admin.html` | +NPS pill, +modal con tabla por profesional, +loadNPS() init+interval, +Escape handler |

### Tests
- harness_50: 90/90 ✅
- test_normalizer: 52/52 ✅

### Notion
- Plan guardado en página "Plan Estratégico de Marketing y Fidelización — CMC (2026)"
- Under "Documentación Técnica — Chatbot WhatsApp CMC"

---
*Generado automáticamente — sesión Claude Code 2026-04-14*
