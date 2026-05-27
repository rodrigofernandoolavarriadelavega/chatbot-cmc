# Centro Médico Carampangue — Visión 360°

> Documento maestro del proyecto CMC dentro del holding OLACORE. Última actualización: 2026-05-15.

---

## 1. Visión general

### El holding OLACORE
**OLACORE — Meridiano Sur** es el holding patrimonial del Dr. Rodrigo Olavarría. Tres sub-marcas activas:
- **Centro Médico Carampangue (CMC)** — salud ambulatoria en provincia de Arauco. Núcleo del grupo.
- **Supermercado Meulen** — Puerto Montt. Recuperación familiar post-crisis.
- **Olavega / Estación Olavarría** — strip comercial en Piedra Azul km 17 (Ruta 7, Puerto Montt). Primer activo inmobiliario OLACORE.

Identidad: paleta fiordo / cobre / lacre / neblina · fonts Fraunces + Space Grotesk + IBM Plex Mono · sello brújula con texto orbital. Tono: sureño, patrimonial, geográficamente concreto.

### Visión del CMC
Convertir el CMC en una **empresa de salud regional** (3 sucursales, 100+ profesionales, posiblemente imagenología y laboratorio propios). Decisión de vida documentada en `/horizonte`.

Hoy (2026-05-15):
- **1 sucursal** en Carampangue (Monsalve 102)
- **23+ profesionales activos**, 19 especialidades
- **14.677 pacientes únicos** en historial, **47.548 atenciones** totales (datos BI a abril 2026)
- **Captura de mercado**: 68% del distrito Carampangue, 65% Ramadillas, 34% Laraquete, 5% Arauco urbano
- **Pausa estratégica 2025** — Dr. Olavarría hizo año UC en Santiago. Curva real de salud actual: marzo 2026 (190 atend) → abril 2026 (220 atend)

---

## 2. El chatbot WhatsApp — núcleo operativo

### Qué es
Asistente automático que atiende a los pacientes 24/7 vía WhatsApp Business. Reemplaza el trabajo de recepción para los flujos repetitivos: agendar, cancelar, ver reservas, info de precios, derivación clínica básica.

### Acceso público
- **Número**: +56 9 6661 0737 (número histórico CMC, migrado de prepago a Cloud API)
- **App principal**: https://agentecmc.cl (FastAPI + uvicorn en DigitalOcean)
- **Sitio público SEO**: https://centromedicocarampangue.cl (WordPress)

### Stack
- **Backend**: Python 3.11 + FastAPI + Uvicorn, en VPS DigitalOcean (157.245.13.107, systemd)
- **IA**: Claude Haiku 4.5 para detección de intent y respuestas FAQ
- **HIS**: API Medilink 2 (healthatom.com) para slots, pacientes, citas
- **WhatsApp**: Meta Cloud API (System User permanente)
- **DB**: SQLite cifrado con SQLCipher (`sessions.db`, 48 tablas, 31MB)
- **Cron**: APScheduler con ~20 jobs (recordatorios, post-consulta, win-back, sync, watchdog, doctor alerts)

### Capacidades del bot
- **Agendar/cancelar/reagendar** citas con disponibilidad real cruzada de Medilink
- **Registro de pacientes nuevos** en 1 mensaje (RUT + nombre + sexo + fecha nac + comuna)
- **Detección de intent** con Claude Haiku para preguntas FAQ
- **Recordatorios** automáticos: 24h antes (09:00) y 2h antes
- **Post-consulta** a las 22:00 → encuesta 1-5 estrellas + tips de autocuidado
- **Win-back** automático para pacientes >90 días sin venir
- **Cross-sell contextual** (trauma → kine, ORL ↔ fono, MG → chequeo)
- **Tags clínicos** por keywords (dx:dm2, dx:hta, embarazo, etc.) para alertar al doctor en próxima consulta
- **Modo doctor** persistente para Dr. Olavarría: resumen pre-cita, reportes de progreso 9/12/16/20, comando `dx`
- **Multi-canal**: WhatsApp + Instagram DM + Facebook Messenger (mismo flujo)
- **Audio**: transcripción con OpenAI Whisper
- **Compliance Ley 19.628**: opt-in explícito, derecho al olvido (cascade 18 tablas)

### Panel de recepción
- **URL**: https://agentecmc.cl/admin?token=cmc_admin_2026
- **v1** (`/admin`): operativo, métricas
- **v2** (`/admin/v2`): rediseño chat-first (bandeja / chat / contexto)
- Funciones: tomar control de conversaciones, ver historial completo, marcar opt-in, gestionar pacientes en control, módulo ortodoncia, notificaciones sonoras

---

## 3. Dashboards estratégicos

Todos los dashboards viven en agentecmc.cl. Acceso desde el menú maestro: https://agentecmc.cl/menu

### 3.1 Operación clínica diaria

#### `/admin` y `/admin/v2`
Panel para recepción. Conversaciones en vivo, métricas, citas del día, módulos de control (kine, ortodoncia, psicología, nutrición).

#### `/admin/mapa-comunas` y `/admin/mapa-direcciones`
Heatmap geográfico de pacientes. Direcciones geocodificadas con jitter determinístico.

### 3.2 Marketing y Data

#### `/segmentacioncmc` ⭐ (nuevo 2026-05-13)
**Para qué**: identificar audiencias para campañas Meta Ads, lookalikes y mensajes diferenciados.

Vista global del centro médico + selector por profesional. Tabs:
- **Overview**: KPIs, evolución temporal (mensual/anual/personalizado), demografía
- **Especialidades**: distribución de pacientes por especialidad (16+, "General" y "Medicina General" unificadas)
- **Territorio**: localidades cruzadas con Censo INE 2017 (Arauco urbano 5.310 pac, Carampangue 797, Laraquete 333, Ramadillas 111, etc.)
- **RFM Lifecycle**: 8 segmentos (champion, leal, nuevo, en_riesgo, lapsed, hibernando, perdido_oneshot, otro)
- **Segmentaciones** (estilo Mailchimp/HubSpot Healthcare): sexo, generación, matriz sexo×edad, puerta de entrada (especialidad primera), mono vs multi-especialidad, loyalty 12m, velocidad de retorno, convenio, localidad×sexo, **buyer personas inferidas** (mujer cuidadora 25-50, adulto mayor crónico, joven preventivo, rehabilitación/trauma, dental exclusivo, multi-especialidad activo)
- **Audiencia · export**: CSV descargable por segmento listo para subir como Custom Audience de Meta

**Naturaleza**: snapshot estático refrescable desde el Mac. Filtros de fecha aplican en gráfico temporal (mensual/anual/personalizado).

#### `/seo/dashboard`
Plan SEO 12 semanas de centromedicocarampangue.cl: Google Search Console, queries, geo de captación cruzado con bot, OKRs y roadmap.

#### `/meta/dashboard`
Análisis Meta Marketing API: campañas, audiencias custom/lookalike, CAPI, CLP 14.3M lifetime, OKRs Q2 2026.

#### `/atribucion`
Cruce diario Meta Ads × Bot × Pacientes nuevos × Referidos. Funnel completo, distribución por canal (amigo / rrss / google / recurrente).

### 3.3 Estratégicos · OLACORE

#### `/horizonte`
Roadmap a 12 meses · 36 meses · 7 años. Escenarios A/B/C con pipeline de contratación. **Decisión de vida documentada**.

#### `/camino-50m`
Las 8 palancas para duplicar ingresos de 25M a 50M/mes.

#### `/crecimientopersonal`
Roadmap Dr. Olavarría: 33 cursos, 14 libros, 6 certificaciones. Transición médico → emprendedor.

### 3.4 BI Postgres (local Docker)

#### `/cmc` (local Mac, port 8000)
Dashboard unificado: Global CMC + selector por profesional. Mismo que `/segmentacioncmc` pero **con filtros de fecha live** (no snapshot). 5 tabs: Overview, Especialidades, Territorio, RFM, Audiencia.

Endpoints API:
- `/api/segmentacion/profesionales`
- `/api/segmentacion/global/{kpis,segmentos,segmentaciones,comunas,arauco-localidades,demografia,temporal,audiencia}`
- `/api/segmentacion/{prof_id}/{segmentos,demografia,territorio,cross-sell,temporal,audiencia}`

#### `/bi/mensual`, `/bi/dia`, `/bi/proyecto`
Dashboards mensuales con métricas detalladas + simulador de capacidad.

### 3.5 Médicos individuales
- `/olavarria` — Dr. Rodrigo. Ingresos reales (BI × 0.85 vs Caja Medilink). Pausa 2025 UC documentada
- `/abarca` — Dr. Andrés Abarca
- `/profesional/{id}` — dashboard genérico por id_profesional Medilink

### 3.6 Ecosistema
- `/ecosistema` — visualización Canvas 2D del sistema digital CMC
- `/portal`, `/portal/v2`, `/portal/informe` — portal paciente con OTP WhatsApp

---

## 4. Pipeline de datos

### Fuentes
1. **Medilink** (HIS, healthatom.com) — verdad clínica: citas, pacientes, atenciones, profesionales, agendas, bloqueos
2. **Bot sessions.db** (VPS, SQLCipher) — conversaciones, citas_bot, contact_profiles, fidelización, eventos
3. **heatmap_cache.db** (chatbot) — pacientes + direcciones para detección territorial
4. **BI Postgres** (local Mac Docker) — ETL de Medilink, fuente de verdad para análisis: `bi.fact_atenciones` (47k rows), `bi.fact_ingresos`, `bi.dim_paciente` (14.677 rows), `bi.dim_profesional`, `bi.dim_especialidad`, `bi.dim_convenio`

### Flujos
```
Medilink (API)
   │  ETL diario
   ▼
BI Postgres (local Mac)
   │  scripts/sync_localidad_from_heatmap.py
   │  (cruza dim_paciente con heatmap_cache.db
   │   y aplica LOCALIDAD_KEYWORDS para detectar
   │   Carampangue urbano/rural, Laraquete, Ramadillas, etc.)
   ▼
bi.dim_paciente.localidad  ◄── fuente única de verdad territorial
   │
   │  scripts/snapshot_cmc_to_vps.sh
   │  (genera JSONs y los sube al VPS)
   ▼
/opt/chatbot-cmc/data/cmc_snapshot/  →  agentecmc.cl/segmentacioncmc
   │
   │  scripts/sync_landings_comuna_to_wp.py
   │  (renderiza landings + las pushea como Pages a WP)
   ▼
centromedicocarampangue.cl/{curanilahue,los-alamos,canete,lebu}
```

### Reglas críticas
- **Ingresos**: usar `bi.fact_pagos` o módulo Caja Medilink. NUNCA `/atenciones.total` como "ventas" (sobreestima 15-20%).
- **Demografía territorial**: SIEMPRE usar `bi.dim_paciente.localidad` (ya enriquecido). NUNCA reinventar CASE de comuna.
- **Olavarría histórico**: BI × 0.85 = caja real (validado vs Caja Medilink).
- **"General" y "Medicina General"** son la misma especialidad. Unificar en queries.

---

## 5. Sistema de fidelización

Cron jobs activos (timezone Chile):
- **09:00** — recordatorios de citas del día siguiente
- **09:00** — postconsulta_morning (cubre citas tardías del día anterior)
- **Cada 15 min 7:30-21:30** — recordatorios 2h antes
- **10:00** — cumpleaños diario con tips preventivos
- **22:00** — post-consulta del mismo día (encuesta 1-5 estrellas + tips)
- **21:30** ⭐ — `_job_enrolar_atendidos_dia` (nuevo 2026-05-13): pull atendidos del día desde Medilink, enrola en `citas_bot` los que tienen perfil bot (Tier B), registra en `pacientes_sin_optin` los que no
- **Lunes 10:30** — reactivación pacientes inactivos
- **Lunes 10:05** — winback BI (gated, requiere consent marketing)
- **Lunes primer del mes 10:00** — win-back >90 días
- **Miércoles 10:30** — cross-sell kine
- **Cada hora :15** — detectar cancelaciones manuales en Medilink + reagendamiento 1-click
- **23:50** — sync caché de citas

### Templates Meta aprobados (14)
9 UTILITY + 5 MARKETING: recordatorio_cita, recordatorio_cita_2h, postconsulta_seguimiento, lista_espera_cupo, informe_listo, seguimiento_medico, reactivacion_paciente, crosssell_kine, control_especialidad, adherencia_kine, sistema_recuperado, winback_*

---

## 6. Compliance Ley 19.628 (datos personales)

### Opt-in del paciente
- **Vía bot**: cuando comparte RUT por primera vez → `save_privacy_consent(method="rut_provided")` automático
- **Vía QR de recepción** ⭐ (nuevo 2026-05-14): paciente atendido offline escanea el QR de recepción → WhatsApp abre con "Activar WhatsApp CMC" pre-llenado → bot pide opt-in formal con botones ✅/❌ → `save_privacy_consent(method="qr_recepcion")`
- **Página QR para imprimir/mostrar**: https://agentecmc.cl/qr-optin

### Otros
- Política de privacidad: https://centromedicocarampangue.cl/privacidad
- Derecho al olvido: `DELETE /admin/api/patient` (cascade 18 tablas + audit `gdpr_deletions`)
- Tabla `pacientes_sin_optin`: pacientes atendidos offline cuyo celular conocemos pero no dieron consent — recepción los enrola manual

---

## 7. Captación territorial (landings comuna)

Cuatro landings SEO local indexables en centromedicocarampangue.cl, generadas por el bot y pusheadas como WP Pages:

| URL | Comuna | km al CMC | Población (Censo 2017) | Captura actual |
|---|---|---:|---:|---:|
| /curanilahue | Curanilahue | 25 | 32.288 | 3.5% |
| /los-alamos | Los Álamos | 35 | 19.805 | ~1% |
| /canete | Cañete | 45 | 32.394 | <1% |
| /lebu | Lebu | 55 | 25.522 | <1% |

Diseño (versión C):
- Hero con rating dinámico Google Places (4.8 ★ · 14 reseñas)
- Trust strip (Fonasa MLE · horarios · parking · pago dental)
- 3 cards transaccionales (Agendar / Bono Fonasa / Chequeo preventivo)
- 16 especialidades con precio Fonasa + particular
- Distance block + tip por comuna
- 3 reseñas reales de Google
- FAQ con 6 preguntas localizadas
- Schema MedicalClinic + AggregateRating + Review + FAQPage + BreadcrumbList

Hosting:
- **Indexable en**: centromedicocarampangue.cl (vía WP Pages)
- **Preview interno**: agentecmc.cl/{slug} (noindex, evita duplicate content)
- **Snippet 8 Bridge** sirve el HTML desde agentecmc.cl?for_wp=1 con cache 6h

---

## 8. Cómo refrescar cada pieza

```bash
# 1. ETL Medilink → BI Postgres (data clínica reciente)
cd ~/health-bi-project && python -m etl.main

# 2. Localidad enriquecida en bi.dim_paciente
python3 ~/health-bi-project/scripts/sync_localidad_from_heatmap.py

# 3. Olavarría dashboard (CSV → SCP → SQLite VPS)
~/health-bi-project/scripts/sync_olavarria_to_vps.sh

# 4. Dashboard /segmentacioncmc en agentecmc.cl
~/health-bi-project/scripts/snapshot_cmc_to_vps.sh

# 5. Heatmap comunas (re-detección territorial)
ssh root@157.245.13.107 "cd /opt/chatbot-cmc && PYTHONPATH=app venv/bin/python3 scripts/heatmap_comunas.py all 2026 4"

# 6. Landings comuna en WP
ssh root@157.245.13.107 "cd /opt/chatbot-cmc && venv/bin/python3 scripts/sync_landings_comuna_to_wp.py all"

# 7. Deploy del bot
git push origin main && ssh root@157.245.13.107 "cd /opt/chatbot-cmc && git pull && systemctl restart chatbot-cmc"

# 8. Purgar cache WP
curl -s --user 'Adminweb:JObB yl8y 4QBF AZL3 mC96 Xj8U' "https://centromedicocarampangue.cl/?cmc_purge_now=yes-2026"
```

---

## 9. Proyección estratégica

### Escenario A (Conservador, 12 meses)
- 1 sucursal Carampangue
- 25 profesionales
- $25M/mes facturación → $30M
- Foco: optimizar lo que ya tenemos

### Escenario B (Ambicioso, 36 meses)
- 1-2 sucursales (segunda exploratoria en Curanilahue o Los Álamos)
- 50 profesionales
- $50M/mes facturación
- Foco: probar segunda sucursal sin perder calidad

### Escenario C (Decisión de vida, 7 años) ⭐ recomendado
- 3 sucursales: Carampangue (matriz, 20 boxes), Curanilahue (10 boxes), Los Álamos o Cañete (8 boxes)
- 100+ profesionales
- 4 jefes de sucursal
- Imagenología y laboratorio propios
- CapEx acumulado $300-500M
- Empresa de salud regional del sur

### Las 8 palancas (`/camino-50m`)
1. Captación nuevos pacientes (Meta Ads + SEO local)
2. Win-back lapsed (2.296 pacientes Olavarría sin venir 12-24m)
3. Cross-sell multi-especialidad
4. Chequeos preventivos como producto empaquetado
5. Convenio empresas (B2B)
6. Telemedicina
7. Farmacia magistral (validado pero pausado)
8. Segunda sucursal

---

## 10. Pendientes y decisiones abiertas

### Técnicos
- Bug WAIT_SLOT "para hoy" sin avisar (caso María 2026-04-27)
- Normalizar capitalización en evento `sin_disponibilidad`
- Auditar bucket "intent: otro" (94/7d) y agregar al cache
- Reserva tentativa de slot por 30s (race condition Dr. Márquez/Olavarría)
- Suite pytest formal (valid_rut, smart_select, flows core)

### Estratégicos
- Decisión marca dental Concepción (Olamar / Oris / Austra) — riesgo: ortodoncista Dra. Castillo vive en Concepción, si se va full = perdemos ~10 pacientes ortodoncia Curanilahue
- Validar Meulen viable independiente del subsidio CMC
- Cuándo activar winback BI (gated false hasta consent legal completo)
- Segunda sucursal: ¿Curanilahue o Los Álamos primero?

### Marketing
- Por qué Meta Ads spend del 27-abr fue $4.930 vs baseline $200K/día — revisar Ads Manager
- Activación de campañas segmentadas por buyer persona (data ya disponible en `/segmentacioncmc`)
- Onboarding de los 5 nuevos del 12-may sin perfil bot (vía QR opt-in)

---

## 11. Identidad y branding

**OLACORE — Meridiano Sur**
- Paleta oficial: fiordo #0E2530 · cobre #B98149 · lacre #9A3E2F · lenga #7C4A2A · verde-musgo #4A5D44 · neblina #E8E0D0
- Tipografía: Fraunces (display) + Space Grotesk (UI) + IBM Plex Mono (microdatos)
- Sello: brújula circular con texto orbital · meridiano vertical cobre con 2 bolitas
- Voz: sureño, patrimonial, geográficamente concreto

**Reglas críticas (no negociables)**
- Número +56987834148 NUNCA aparece en cara pública (es personal del Dr.)
- Métodos de pago: dental acepta tarjeta, médico solo efectivo/transferencia/Fonasa
- NO escribir "certificados/habilitados/Superintendencia/acreditados" en el sitio (publicidad engañosa)
- NO existe "Sucursal Olavarría" — Olavarría es apellido, no sede

---

## 12. Archivo de sesiones recientes

### 2026-05-13 al 15
- BI migrado a DO, sprint win-back, CAPI, Custom Audiences
- Dashboard `/segmentacioncmc` con 10 segmentaciones healthcare-marketing
- Selector "Personalizado" con date pickers en gráfico temporal
- QR opt-in `/qr-optin` para recepción (Ley 19.628)
- Job `_job_enrolar_atendidos_dia` 21:30 CLT (cierra el gap de pacientes offline al post-consulta)
- Rediseño landings comuna (versión C) + sync al WP via REST API
- Fotos del centro subidas a WP Media Library (dominio único)
- Limpieza sistemática de "Sucursal Olavarría" inventada

### 2026-04 al 2026-05-12
- Sprint SEO 2026-05-03 (27+ commits, 21 schemas, 247 URLs sitemap)
- Migración número WhatsApp: prepago → +56966610737 (Cloud API)
- Fidelización completa
- Panel admin v2 chat-first
- Templates Meta aprobados (14 total)
- Ley 19.628 compliance: opt-in + cascade delete
- Detección pasiva de patologías crónicas
- Doctor mode persistente con tags
- Agendamiento para terceros
- Heatmap geográfico + atribución diaria

---

*Documento generado para sincronización a Notion. Última edición humana: pendiente.*
