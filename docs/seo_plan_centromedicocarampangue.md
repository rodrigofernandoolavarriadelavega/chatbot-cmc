# Plan SEO — centromedicocarampangue.cl

**Fecha**: 2026-04-16
**Responsable**: Dr. Rodrigo Olavarría (Claude asistente)
**Dominio**: https://centromedicocarampangue.cl (WordPress + Yoast + Elementor)
**Plazo objetivo**: 12 semanas para triplicar tráfico orgánico y conversiones desde el sitio al chatbot.

---

## Contexto y baseline (2026-04-15)

### Estado actual del sitio
- **Tráfico**: 162 usuarios/mes, **51.8% rebote**, ~2.1 páginas/sesión.
- **Dependencia Google**: 75% del tráfico orgánico viene de búsquedas directas de "centro médico carampangue".
- **Conversión**: 80% del tráfico **muere en la home** (no avanza a agendar, servicios ni contacto).
- **Dispositivos**: **65% móvil**, 30% desktop, 5% tablet.
- **RRSS como fuente**: 5% del tráfico total.
- **Search Console**: 197 clics en 30 días, 11 páginas indexadas.
- **Costo hosting actual**: ~$70.000 CLP/año (ya pagado hasta ~2027-04).

### Problemas críticos detectados
1. **🔴 Número WhatsApp incorrecto en todo el sitio**: usa `+56966610737` (secretarias) en vez de `+56945886628` (chatbot oficial). Implica que el tráfico web **NO llega al bot**.
2. **🔴 Placeholder "+5691234567"** en al menos 1 post del blog.
3. **🔴 Links "Agendar" apuntan a `n9.cl/xxxx`** (acortador) en vez de directamente al chatbot.
4. **🟠 0 meta descriptions** en home + páginas principales.
5. **🟠 1 solo post publicado** (default "Hello world" de WordPress, 2020).
6. **🟠 19 borradores** sin publicar (hay material pero no se subió).
7. **🟠 URLs duplicadas**: `/home/`, `/centro-medico-carampangue-{,2,3}/`, `/elementor-*/` son rutas internas de Elementor expuestas e indexables.
8. **🟠 Páginas desactualizadas desde 2020**: `/profesionales/`, `/equipo/`, `/servicios/`.
9. **🟡 0 schema markup** (MedicalClinic, Physician, FAQPage).
10. **🟡 Imágenes sin optimizar** (`.jpg` de 2020, no hay WebP).
11. **🟡 3 números WhatsApp distintos coexisten** en el sitio, ninguno es el chatbot.

---

## Objetivos medibles (12 semanas)

| KPI                        | Baseline | Meta 4 sem | Meta 12 sem |
|----------------------------|----------|-----------|-------------|
| Tráfico orgánico (usuarios/mes) | 162      | 250       | 500         |
| Rebote                     | 51.8%    | 45%       | 35%         |
| Páginas/sesión             | 2.1      | 2.5       | 3.2         |
| Clics WhatsApp → chatbot   | ~0       | 30        | 100         |
| Páginas indexadas          | 11       | 20        | 40          |
| Blogs publicados           | 1        | 6         | 16          |
| Backlinks                  | ?        | 5         | 20          |
| Google Business Profile    | —        | Creado    | 4.5★ / 30 reseñas |

---

## Plan en 4 fases

---

## 🔴 FASE 1 — Fixes críticos (2 horas, ESTA SEMANA)

### F1.1 — Unificar número WhatsApp al chatbot
- Buscar **todas** las ocurrencias de `+56966610737`, `+569 6661 0737`, `966610737` en WordPress y reemplazar por `+56945886628`.
- Ídem con placeholder `+5691234567`.
- Cambiar todos los links `wa.me/…` al formato `https://wa.me/56945886628?text=Hola%2C%20quiero%20agendar`.
- Plugin recomendado: "Better Search Replace" (WP) para hacerlo en 1 clic.

### F1.2 — Meta descriptions (home + 5 páginas principales)
Agregar vía Yoast SEO las siguientes meta descriptions (≤155 caracteres):

| Página            | Meta description |
|-------------------|------------------|
| Home              | Centro Médico Carampangue: médicos, dentistas, kine, psicólogo. Agenda tu cita por WhatsApp en 30 segundos. Atención Fonasa e Isapre. |
| Profesionales     | Conoce a nuestros médicos, dentistas, kinesiólogos y especialistas del Centro Médico Carampangue. 20+ profesionales disponibles. |
| Servicios         | Medicina general, odontología, kinesiología, psicología, ortodoncia, ecografías y más. Agenda online por WhatsApp. |
| Odontología       | Ortodoncia, implantes, endodoncia, estética dental y odontología general en Carampangue. Primera evaluación gratis con descuento. |
| Kinesiología      | Rehabilitación post-trauma, dolor lumbar, ciática y kinesiología respiratoria con Leonardo Etcheverry y Luis Armijo. |
| Contacto          | Carampangue, Región del Biobío. Agenda por WhatsApp, llama al +56 9 4588 6628 o visítanos en Av. Nahuelbuta. |

### F1.3 — Canonical y redirects
- **301 redirects** para las URLs duplicadas:
  - `/home/` → `/`
  - `/centro-medico-carampangue-2/`, `/centro-medico-carampangue-3/` → `/`
  - Cualquier `/elementor-*` → `/`
- Plugin: "Redirection" (WP, gratis).
- Agregar `<link rel="canonical">` a cada página principal (Yoast lo hace automático si se configura).

### F1.4 — Noindex a páginas internas de Elementor
- En Yoast → marcar "noindex" para `/?elementor_library=…` y `/elementor-*/`.
- Agregar `Disallow: /?elementor_library=` en `robots.txt`.

### F1.5 — Sitemap limpio
- Regenerar sitemap con Yoast (automático al limpiar las URLs duplicadas).
- Resubir a **Google Search Console** + **Bing Webmaster Tools**.

**Entregable F1**: sitio con WA correcto, 6 páginas con meta description, sin URLs duplicadas indexables, sitemap limpio en GSC.

---

## 🟠 FASE 2 — Contenido y blog (4-6 horas, semanas 1-3)

### F2.1 — Publicar 12 blog posts SEO (palabras clave reales)

**Lista priorizada** (volumen Chile estimado vía Google Ads):

| # | Título sugerido                                                        | Palabra clave principal              | Especialidad       |
|---|------------------------------------------------------------------------|--------------------------------------|--------------------|
| 1 | ¿Cuánto cuesta una ortodoncia en Chile? Guía de precios 2026           | precio ortodoncia chile              | Odontología        |
| 2 | Implante dental: qué es, cuánto dura y precios reales en Biobío        | implante dental biobío               | Odontología        |
| 3 | Dolor lumbar: cuándo ir al kine y cuándo al traumatólogo               | dolor lumbar kinesiología            | Kine / Trauma      |
| 4 | Cefalea tensional vs migraña: cuál es cuál y qué hacer                 | cefalea tensional tratamiento        | Medicina general   |
| 5 | Control del embarazo paso a paso en Chile                              | control embarazo fonasa              | Matrona            |
| 6 | Otorrino en Carampangue: ¿cuándo consultar?                            | otorrino arauco                      | ORL                |
| 7 | Rinoplastia funcional vs estética: diferencias                         | rinoplastia funcional precio         | ORL                |
| 8 | Diabetes tipo 2: primeros pasos tras el diagnóstico                    | diabetes tipo 2 control              | Medicina general   |
| 9 | Hipertensión: metas terapéuticas y exámenes que debes pedir            | hipertensión control                 | Cardiología        |
| 10| Psicología infantil: cuándo llevar a tu hijo al psicólogo              | psicólogo infantil arauco            | Psicología         |
| 11| Nutrición Fonasa: cómo agendar tu primera consulta                     | nutricionista fonasa chile           | Nutrición          |
| 12| Vacunas PNI 2026: calendario completo por edad                         | calendario vacunas PNI 2026          | Pediatría          |

### F2.2 — Estructura de cada post (checklist)
- **H1**: exactamente la palabra clave principal.
- **Meta title**: ≤60 caracteres, con palabra clave al inicio.
- **Meta description**: ≤155 caracteres con CTA.
- **URL slug**: kebab-case corto (ej: `/blog/precio-ortodoncia-chile/`).
- **Contenido**: 800-1.500 palabras, H2/H3 jerárquicos, tabla de contenidos.
- **Imagen destacada**: WebP, ≤150 KB, con `alt` descriptivo.
- **Internal linking**: 2-3 links a otras páginas/posts del sitio.
- **CTA final**: botón "Agendar por WhatsApp" con `wa.me/56945886628`.
- **Schema**: Article + BreadcrumbList.
- **Última revisión**: fecha visible ("Actualizado: 2026-04-16").

### F2.3 — Publicar los 19 borradores existentes
- Revisar uno a uno en el dashboard WP.
- Si el contenido está bien, agregar: featured image, meta, categoría, tags, CTA WA → publicar.
- Si el contenido está mal, archivar.

**Entregable F2**: 16 posts publicados (12 nuevos + 4 rescatados de borradores), todos con Yoast verde.

---

## 🟡 FASE 3 — Optimización técnica (2 semanas)

### F3.1 — Schema markup (JSON-LD)

**MedicalClinic** (inyectar en home + contacto):
```json
{
  "@context": "https://schema.org",
  "@type": "MedicalClinic",
  "name": "Centro Médico Carampangue",
  "image": "https://centromedicocarampangue.cl/logo.webp",
  "url": "https://centromedicocarampangue.cl",
  "telephone": "+56945886628",
  "priceRange": "$$",
  "address": {
    "@type": "PostalAddress",
    "streetAddress": "Av. Nahuelbuta [número]",
    "addressLocality": "Carampangue",
    "addressRegion": "Biobío",
    "postalCode": "4480000",
    "addressCountry": "CL"
  },
  "openingHoursSpecification": [...],
  "medicalSpecialty": [
    "GeneralMedicine","Dentistry","Otolaryngology","Cardiology",
    "Traumatology","Gynecology","Physiotherapy","Psychology","Nutrition"
  ],
  "availableService": [...]
}
```

**Physician** (una por profesional):
- Dr. Rodrigo Olavarría, Medicina General
- Dr. Andrés Abarca, Medicina General
- Dr. Alonso Márquez, Medicina General/Familiar
- Dra. Javiera Burgos, Odontología
- ... (24 en total, ver tabla CLAUDE.md)

**FAQPage** (en páginas de servicios):
- ¿Atienden Fonasa?
- ¿Cuánto cuesta la consulta?
- ¿Hacen licencias médicas?
- ¿Tienen estacionamiento?
- ¿Cómo agendo?

### F3.2 — Performance
- **Convertir imágenes a WebP** (plugin "Imagify" o "ShortPixel").
- **Lazy loading** para imágenes bajo el fold.
- **Cache**: WP Rocket o LiteSpeed Cache.
- **CDN**: Cloudflare (gratis) en modo proxy.
- **Meta Core Web Vitals** objetivo: LCP <2.5s, CLS <0.1, INP <200ms.

### F3.3 — Landing por especialidad
Crear landing dedicada para cada especialidad top con:
- Hero con profesional y precio destacado.
- Lista de prestaciones.
- CTA WhatsApp.
- FAQ con schema.
- Testimonios (cuando los haya).

Landings priorizadas:
1. `/ortodoncia/` — $120k instalación + $30k control
2. `/kinesiologia/` — lumbar, post-trauma, respiratorio
3. `/otorrinolaringologia/` — Dr. Borrego
4. `/cardiologia/` — Dr. Millán
5. `/psicologia/` — adulto e infantil
6. `/medicina-general/` — 3 médicos
7. `/nutricion/` — Fonasa $4.680
8. `/estetica-facial/` — Dra. Fuentealba

### F3.4 — Mobile-first audit
- Probar en Lighthouse móvil (objetivo: 90+ en SEO y accesibilidad).
- Arreglar textos pequeños, botones cercanos, viewport.
- **Tap target ≥48px** en botones Agendar/Llamar.

**Entregable F3**: sitio con schema en 10+ páginas, WebP + cache + CDN activos, 8 landings por especialidad, Lighthouse 90+ en mobile.

---

## 🟢 FASE 4 — Presencia local y backlinks (ongoing)

### F4.1 — Google Business Profile (GBP)
- Crear o reclamar perfil en maps.google.com/business.
- Subir fotos reales (fachada, box, equipo, instalaciones).
- Completar horarios, servicios, atributos (estacionamiento, accesible, Fonasa).
- **Pedir reseñas** a pacientes satisfechos (meta: 30 reseñas con 4.5★ promedio).
- Responder el 100% de las reseñas en <24h.

### F4.2 — Directorios y listados
- **Doctoralia.cl** — crear perfiles individuales de los 4 médicos más buscados.
- **ChileAtiende / Fonasa** — verificar que aparece como prestador.
- **Páginas Amarillas CL** — listado gratuito.
- **Wikidata + OpenStreetMap** — ficha del centro.
- **Bing Places** — equivalente a GBP.

### F4.3 — Backlinks locales
- **Municipalidad de Arauco** — pedir link desde "servicios de salud privados" (si existe).
- **Diario Regional / BiobíoChile** — pitch nota sobre inauguración chatbot IA.
- **Colegio Médico CL, Regional Biobío** — directorio de socios.
- **Universidades del área de salud** (UdeC, UCSC) — si hay convenios de prácticas.
- **Bloggers locales** de maternidad/salud del Biobío.

### F4.4 — Redes sociales (soporte al SEO)
- **Instagram**: 2 posts/semana (tips de salud + casos).
- **Facebook**: replicar IG + eventos (campañas de vacunación, etc.).
- **TikTok**: 1 video/semana del chatbot en acción (gancho: "agenda en 30 segundos").
- **YouTube Shorts**: entrevistas cortas con profesionales (≤60s).

**Cada post debe linkear al sitio**, no solo a WhatsApp, para alimentar el canal "social" de GA4.

**Entregable F4**: GBP con 30 reseñas, 5 directorios verificados, 10+ backlinks locales, 3 canales sociales activos.

---

## Cronograma resumido

| Semana | Foco                                             |
|--------|--------------------------------------------------|
| 1      | F1 completo (fixes críticos) + 2 blogs nuevos    |
| 2      | 4 blogs nuevos + schema MedicalClinic            |
| 3      | 4 blogs nuevos + schema Physician (24)           |
| 4      | 2 blogs nuevos + WebP + cache + Cloudflare       |
| 5-6    | Landings por especialidad (4 primeras)           |
| 7-8    | Landings por especialidad (4 últimas) + GBP      |
| 9-10   | Directorios + backlinks + pedido de reseñas      |
| 11     | Revisión métricas + ajustes + corner cases SEO   |
| 12     | Medición final vs baseline + plan siguiente fase |

---

## Seguimiento

Dashboard visual de progreso en: **https://agentecmc.cl/seo/dashboard**
Ver archivo: `templates/seo_dashboard.html`

El dashboard se actualiza editando el objeto `DATA` en el `<script>` al final del HTML.

---

## Referencias
- Credenciales WordPress: memoria `wordpress_centromedicocarampangue.md`
- Ecosistema digital: https://agentecmc.cl/ecosistema
- Plan de crecimiento general: memoria `growth_plan.md` (Notion #9)
- Landing actual del chatbot: https://agentecmc.cl/landing

---

# ADENDA 2026-04-26 — Cross-sell Med General → Especialistas (Arauco urbano)

## Contexto (datos reales de heatmap_cache.db)
- 6 años de histórico cargado (2020-2026)
- Arauco urbano: 3.364 pacientes únicos
- **Solo ~10-12% de pacientes de MG usaron alguna otra especialidad** (vs 37.9% global)
- Los pares más cruzados (excluyendo rotación intra-MG):
  - MG → Otorrino (Borrego): 116 pacientes (3.4%)
  - MG → Ginecología (Rejón): 116 (3.4%)
  - MG → Kinesiología (Etcheverry): 108 (3.2%)
  - MG → Odontología (Burgos): 62 (1.8%)

## Acciones para reforzar flujos MG → Especialistas en las landings

### En `/medicina-general-arauco/`
Agregar sección "**Si necesitas especialista**":
- "El equipo de medicina general deriva habitualmente a:"
- Cards con link a las 4 landings esp+arauco creadas:
  - 🦴 Traumatólogo (lesiones, dolor)
  - 👂 Otorrino (oídos, garganta, sinusitis)
  - 👶 Ginecóloga (control PAP, embarazo)
  - 🦷 Dentista (limpieza, ortodoncia)
- Mensaje: "Atención integral en un solo lugar — sin viajar a Concepción"

### En `/traumatologo-arauco/`, `/otorrino-arauco/`, `/ginecologo-arauco/`, `/dentista-arauco/`
Agregar sección "**Antes de tu cita con el especialista**":
- "Si no sabes con qué especialista consultar, agenda primero con Medicina General. El equipo te orienta."
- Link a `/medicina-general-arauco/`
- Botón "Consulta primero con MG" además del botón principal "Agendar [especialista]"

### En `/centro-medico-curanilahue/`
- Mismo concepto: "Atendemos contigo desde Medicina General hasta especialista, todo en una visita"
- Replicar para reducir fricción de viaje desde Curanilahue

## Programa "Chequeo + 1" (operacional, no SEO)
Cuando un paciente de Arauco urbano consulta con MG, ofrecer en la misma jornada un servicio adicional:
- Ecografía (TM David Pardo)
- Limpieza dental (Javiera Burgos)
- Sesión kine de evaluación
- Consulta con matrona

Esto requiere:
1. Que recepción tenga visibilidad de slots disponibles del día
2. Que el chatbot sugiera "+1" al confirmar la cita de MG
3. Pricing especial paquete (ej. $35.000 MG + $15.000 ecografía vs $40.000 sueltos)

## KPI a trackear (dashboard SEO)
Agregar a la pestaña Cruces del dashboard:
- **% cross-sell inter-especialidad por comuna** — distinguir de rotación intra-MG
- **Meta**: subir Arauco urbano de 10-12% → 25% en 12 meses

## Profesionales antiguos (excluir del análisis cross)
- Dr. Tomás Araneda Muñoz — MG (no atiende)
- Dr. Gabriel Díaz — MG (no atiende)
- Dra. Natalia Torres Concha — MG (no atiende)

Sus 570+ "cruces" en la data eran rotación intra-MG, no revenue cross-sell.
