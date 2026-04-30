# SEO Playbook CMC — 2026-04-20

Listo para ejecutar cuando entres a WordPress Admin. Todo en orden de prioridad.
Backup HTML ya guardado en esta misma carpeta.

---

## ✅ PARTE 1 — Limpieza de páginas duplicadas

**WP Admin → Páginas → Todas** → mover a Papelera:

- [ ] `home` (Inicio - Centro Médico Carampangue más cerca de tí)
- [ ] `centro-medico-carampangue`
- [ ] `centro-medico-carampangue-2`
- [ ] `centro-medico-carampangue-3`
- [ ] `elementor-26192`
- [ ] `elementor-26201`
- [ ] `kinesiologia-2`
- [ ] `medicina-general-2`
- [ ] `nutricionista-2`
- [ ] `equipo` (Instalaciones)
- [ ] `servicios`

**WP Admin → Entradas → Todas** → mover a Papelera:
- [ ] `Hello world!`

**Luego vaciar papelera**: WP → Páginas → Papelera → "Vaciar papelera".
(Si no se vacía, las URLs siguen existiendo en estado trash y Yoast/GSC las detecta).

Verificación: ninguna de estas URLs está enlazada desde la home — ya lo comprobé.
Safe to delete. Backup HTML está en `seo-backup-2026-04-20/`.

---

## ✅ PARTE 2 — Title / Meta / OG por página (Yoast)

Cada página: editor → panel Yoast SEO → pestaña **SEO** (Title + Meta desc) → pestaña **Social → Facebook** (OG title + OG desc + imagen).

### 🏠 Home — `/`

**SEO title:**
```
Centro Médico en Carampangue, Arauco | 15+ especialidades | CMC
```

**Meta description:**
```
Centro Médico Carampangue atiende en Arauco con medicina general, especialidades y dental. Fonasa y particular. Agenda online o por WhatsApp.
```

**OG title:**
```
Centro Médico Carampangue — Atención cercana en Arauco
```

**OG description:**
```
Medicina general, especialidades y dental en Carampangue. +12.000 pacientes, 15+ especialidades. Agenda hora online o por WhatsApp.
```

---

### 📞 Contacto — `/contacto/`

**SEO title:** `Contacto y ubicación | Centro Médico Carampangue, Arauco`

**Meta description:**
```
Visítanos en República 102, Carampangue, provincia de Arauco. Teléfono (41) 296 5226. WhatsApp +56 9 6661 0737. Lun-Vie 08-21h, Sáb 10-14h.
```

---

### 👩‍⚕️ Profesionales — `/profesionales/`

**SEO title:** `Médicos y especialistas | Centro Médico Carampangue`

**Meta description:**
```
Conoce a nuestro equipo médico y dental en Carampangue: medicina general, kinesiología, ginecología, psicología, nutrición, odontología y más.
```

---

### 🧠 Psicología — `/psicologia/`

**SEO title:** `Psicólogo en Carampangue y Arauco | CMC`

**Meta description:**
```
Atención psicológica adulto e infanto-juvenil en Carampangue, provincia de Arauco. Bono Fonasa y convenios. Agenda hora online.
```

---

### 📝 Blog — `/blog/`

**SEO title:** `Blog de salud | Centro Médico Carampangue`

**Meta description:**
```
Artículos sobre salud, prevención y especialidades médicas del equipo del Centro Médico Carampangue en Arauco.
```

---

### 📄 Cuándo consultar medicina general — `/cuando-consultar-medico-general/`

**SEO title:** `Cuándo consultar a un médico general | CMC Carampangue`

**Meta description:**
```
Guía práctica sobre cuándo acudir al médico general, señales de alerta y control preventivo. Atención en Carampangue, Arauco.
```

---

## ✅ PARTE 3 — Schema JSON-LD MedicalClinic (gran impacto)

Yoast solo genera schema genérico (WebSite/WebPage). Falta `MedicalClinic`, que es lo que Google usa para el **local pack médico** y rich snippets.

**Opción A (recomendada):** Plugin gratuito **"Insert Headers and Footers" (WPCode Lite)** → pegar el bloque del archivo `schema-medicalclinic.html` (en esta carpeta) dentro de `<head>` de todo el sitio. Se carga solo en la home si configuras condición "Only on Front Page".

**Opción B:** Yoast → SEO → Ajustes → Tipo de sitio → cambiar a **"Organización"** → Nombre: Centro Médico Carampangue. (Esto cubre LocalBusiness pero no MedicalClinic/horarios/especialidades).

Recomiendo A (más completo) + B en paralelo.

---

## ✅ PARTE 4 — Imagen OG (Facebook / WhatsApp previews)

Especificación:
- **Tamaño:** 1200 × 630 px (ratio 1.91:1)
- **Peso:** <300 KB (JPG o PNG optimizado)
- **Contenido sugerido:**
  - Logo CMC grande
  - Texto: "Centro Médico Carampangue"
  - Subtexto: "Atención médica y dental en Arauco"
  - Fondo: azul corporativo del sitio (#1172AB)

**Dónde subirla:** Yoast → SEO → Ajustes → Redes sociales → Facebook → "Imagen predeterminada".

*(Si quieres te la armo en Canva con MCP — dímelo).*

---

## ✅ PARTE 5 — Ajustes rápidos en Yoast

1. **Idioma OG:** Yoast → Ajustes → Sitio → Idioma → cambiar `es_ES` a `es_CL` (**es-CL** en algunas versiones).
2. **Separador de títulos:** Yoast → Apariencia en el buscador → Separador de títulos → elegir `|` (pipe) en lugar de `-` (más limpio).
3. **Desactivar archivos de autor:** Yoast → Ajustes → Tipos de contenido → Archivos de autor → **Desactivar** (evitas duplicados `?author=1`).
4. **Desactivar archivos de fecha:** mismo menú → Archivos basados en fecha → **Desactivar**.

---

## ✅ PARTE 6 — Arreglar typo en home

Elementor → Home → buscar texto "Equipo **frofesional**" → cambiar a "Equipo **profesional**".

---

## ✅ PARTE 7 — Google Search Console (al final)

1. Re-enviar sitemap: `https://centromedicocarampangue.cl/sitemap_index.xml`
2. Solicitar reindexación (Inspección de URL → Solicitar indexación) de:
   - `/`
   - `/contacto/`
   - `/profesionales/`
   - `/psicologia/`
   - `/blog/`
   - `/cuando-consultar-medico-general/`

---

## 📋 PARTE 8 — Blogs listos para publicar (20 artículos)

Tienes 20 HTMLs ya escritos en `~/blog-*.html`. Cada uno necesita:
1. Ser convertido a entrada de WordPress (copy-paste del contenido al editor)
2. Meta description (todas las tengo listas más abajo)
3. Categoría: crear **"Especialidades médicas"** y **"Especialidades dentales"**
4. Imagen destacada (sugerencia: generar con IA, 1200×630)

**Meta descriptions listas para cada blog:**

| Slug sugerido | Meta description |
|---|---|
| `cardiologia` | Especialistas en cardiología en Carampangue, Arauco. Consultas, ECG, hipertensión y prevención de enfermedades del corazón. Agenda online. |
| `endodoncia` | Tratamiento de conducto (endodoncia) en Carampangue sin dolor. Recupera tu diente con especialistas del CMC. Agenda online o por WhatsApp. |
| `estetica-facial` | Tratamientos de estética facial en Carampangue: rejuvenecimiento, hidratación y armonización facial con profesionales del CMC. |
| `fonoaudiologia` | Fonoaudiología infantil y adultos en Carampangue: trastornos del habla, voz y lenguaje. Atención Fonasa y particular. |
| `ginecologia` | Ginecólogo en Carampangue, provincia de Arauco: controles, PAP, ecografías y salud femenina integral. Reserva online. |
| `implantologia` | Implantes dentales en Carampangue: recupera piezas perdidas con implantología de calidad. Consulta evaluación con CMC. |
| `kinesiologia-domicilio` | Kinesiología a domicilio en Carampangue, Arauco: rehabilitación en tu hogar para adultos mayores y post-cirugía. |
| `kinesiologia-musculoesqueletica` | Kinesiología musculoesquelética en Carampangue: rehabilitación de lesiones, dolor lumbar, rodilla, hombro. Agenda online. |
| `kinesiologia-respiratoria` | Kinesiología respiratoria en Carampangue: tratamiento para bronquitis, asma, EPOC y post-COVID. Niños y adultos. |
| `kinesiologia` | Kinesiología en Carampangue: rehabilitación física, recuperación de lesiones y terapia funcional. Atención Fonasa. |
| `medicina-familiar` | Medicina familiar en Carampangue: atención integral y preventiva para toda la familia. Bono Fonasa o particular. |
| `medicina-general` | Médico general en Carampangue, Arauco. Consultas, licencias, órdenes de examen y control preventivo. Agenda online. |
| `nutricion` | Nutricionista en Carampangue: planes de alimentación, control de peso, diabetes e hipertensión. Atención Fonasa. |
| `odontologia-general` | Dentista en Carampangue: tapaduras, limpiezas, extracciones y salud bucal familiar. Consulta evaluación gratuita. |
| `ortodoncia` | Ortodoncia en Carampangue: brackets, alineadores y corrección dental para niños, adolescentes y adultos. |
| `otorrinolaringologia` | Otorrino en Carampangue: tratamiento de oídos, nariz y garganta. Audiometrías, sinusitis, vértigo y más. |
| `podologia` | Podólogo en Carampangue: cuidado profesional de pies, uñas encarnadas, callos y pie diabético. Fonasa y particular. |
| `psicologia-adulto` | Psicólogo para adultos en Carampangue: terapia para ansiedad, depresión y bienestar emocional. Bono Fonasa. |
| `psicologia-infantil` | Psicólogo infantil en Carampangue: señales, beneficios y apoyo emocional para niños y adolescentes. |
| `traumatologia` | Traumatólogo en Carampangue: lesiones óseas, articulares y musculares. Evaluación y tratamiento ortopédico. |

---

## Resumen de orden de ejecución

1. Parte 1 (papelera) — 10 min
2. Parte 2 (Yoast metas) — 30 min
3. Parte 5 (ajustes Yoast) — 5 min
4. Parte 6 (typo) — 2 min
5. Parte 3 (JSON-LD) — 15 min
6. Parte 4 (OG image) — aparte (dímelo y te la armo)
7. Parte 7 (Search Console) — 10 min
8. Parte 8 (blogs) — tarea larga, por etapas

Total mínimo viable (1 a 5, 7): **~1 hora** y se desbloquea la indexación.
