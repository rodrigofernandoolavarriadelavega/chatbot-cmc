# Entregables SEO — listos para pegar

> Generado: 2026-04-23
> Sitio: https://centromedicocarampangue.cl (WordPress + Yoast + Elementor + Divi)
> Cada sección está lista para copiar/pegar sin modificación.

---

## ÍNDICE

1. [Title + meta description (Yoast)](#1-title--meta-description-yoast)
2. [Schema JSON-LD MedicalClinic](#2-schema-json-ld-medicalclinic)
3. [Landing /centro-medico-curanilahue/](#3-landing-centro-medico-curanilahue)
4. [Landings especialidad+ciudad (8)](#4-landings-especialidadciudad)
5. [Blog "Médico para niños en Arauco"](#5-blog-medico-para-ninos-en-arauco)
6. [Google Business Profile — paso a paso](#6-google-business-profile)
7. [Doctoralia centro médico — paso a paso](#7-doctoralia-centro-medico)
8. [Auditoría de 20 blog HTML locales](#8-auditoria-blog-html-locales)
9. [Tracking SEO en chatbot](#9-tracking-seo-en-chatbot)
10. [Plantillas de respuesta a reseñas](#10-plantillas-respuesta-resenas)

---

## 1. Title + meta description (Yoast)

### 🔴 Hallazgo crítico
El title actual de la home es **"Centro Médico Carampangue - Centro Médico Carampangue"** (literalmente repetido). Y **no existe meta description** — Google está extrayendo un fragmento auto-generado del og:description que dice "(41) 296 5226 +56 9 6661 0737 Carampangue..." (parece spam de NAP).

### Páginas a actualizar en Yoast

#### Home (`/`)

```
Title (≤60 chars):
Centro Médico Carampangue | Especialistas Arauco y Curanilahue

Meta description (≤155 chars):
Médicos, dentistas, kine y psicólogos en Carampangue. Atendemos pacientes de Arauco, Curanilahue y Los Álamos. Agenda por WhatsApp en 30 segundos.
```

#### Profesionales (`/profesionales/`)

```
Title:
Equipo Médico CMC | 24+ Profesionales en Arauco y Curanilahue

Meta description:
Conoce a nuestros médicos, dentistas, kinesiólogos y especialistas. Más de 24 profesionales atendiendo en Carampangue, Provincia de Arauco.
```

#### Contacto (`/contacto/`)

```
Title:
Contacto y Ubicación | Centro Médico Carampangue

Meta description:
República 102, Carampangue. Lun a Vie 08:00-21:00, Sáb 10:00-14:00. Agenda por WhatsApp +56 9 6661 0737 o llama al (41) 296 5226.
```

#### Psicología (`/psicologia/`)

```
Title:
Psicólogo en Arauco | Adulto e Infantil — CMC Carampangue

Meta description:
Psicología adulto e infantil en Carampangue. Atendemos pacientes de Arauco, Curanilahue, Los Álamos. Modalidad presencial. Agenda por WhatsApp.
```

#### (Futuras) Servicios (`/servicios/`)

```
Title:
Especialidades Médicas y Dentales | CMC Carampangue

Meta description:
Medicina general, otorrino, cardiología, traumatología, ginecología, kine, nutrición, odontología, ortodoncia, ecografías. Fonasa e Isapre.
```

### Cómo aplicar

1. WordPress admin → Páginas → editar cada página listada arriba.
2. Bajar a la sección "Yoast SEO" al final del editor.
3. Pestaña "SEO" → editar:
   - Slug SEO title → pegar `Title` de arriba
   - Meta description → pegar el texto correspondiente
4. Verificar barra verde de Yoast.
5. Actualizar página.
6. Una vez todas listas: Yoast → Tools → Optimize SEO data → Limpiar caché.

---

## 2. Schema JSON-LD MedicalClinic

### Cómo inyectar
**Opción A (recomendada)**: Yoast → Schema → tipo "MedicalClinic" + completar campos en cada página.
**Opción B (manual)**: Plugin "Insert Headers and Footers" → pegar el bloque completo en `<head>` global.

### Schema completo (pegar en `<head>` de la home)

```html
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "MedicalClinic",
  "@id": "https://centromedicocarampangue.cl/#clinic",
  "name": "Centro Médico Carampangue",
  "alternateName": "CMC",
  "description": "Centro médico privado en Carampangue, Provincia de Arauco. Especialidades médicas y dentales para Arauco, Curanilahue, Los Álamos y zonas aledañas.",
  "url": "https://centromedicocarampangue.cl",
  "logo": "https://centromedicocarampangue.cl/wp-content/uploads/2025/08/cropped-logo-carampangue.png",
  "image": "https://centromedicocarampangue.cl/wp-content/uploads/2025/08/cropped-logo-carampangue.png",
  "telephone": "+56412965226",
  "email": "contacto@centromedicocarampangue.cl",
  "priceRange": "$$",
  "currenciesAccepted": "CLP",
  "paymentAccepted": "Efectivo, Transbank Débito, Transbank Crédito, Transferencia, Fonasa, Isapre",
  "address": {
    "@type": "PostalAddress",
    "streetAddress": "República 102",
    "addressLocality": "Carampangue",
    "addressRegion": "Biobío",
    "postalCode": "4480000",
    "addressCountry": "CL"
  },
  "geo": {
    "@type": "GeoCoordinates",
    "latitude": -37.2667,
    "longitude": -73.2500
  },
  "areaServed": [
    { "@type": "City", "name": "Arauco" },
    { "@type": "City", "name": "Carampangue" },
    { "@type": "City", "name": "Laraquete" },
    { "@type": "City", "name": "Curanilahue" },
    { "@type": "City", "name": "Los Álamos" },
    { "@type": "City", "name": "Cañete" },
    { "@type": "AdministrativeArea", "name": "Provincia de Arauco" }
  ],
  "openingHoursSpecification": [
    {
      "@type": "OpeningHoursSpecification",
      "dayOfWeek": ["Monday","Tuesday","Wednesday","Thursday","Friday"],
      "opens": "08:00",
      "closes": "21:00"
    },
    {
      "@type": "OpeningHoursSpecification",
      "dayOfWeek": ["Saturday"],
      "opens": "10:00",
      "closes": "14:00"
    }
  ],
  "medicalSpecialty": [
    "GeneralMedicine",
    "FamilyPractice",
    "Dentistry",
    "Otolaryngology",
    "Cardiology",
    "Traumatologic",
    "Gynecology",
    "Gastroenterologic",
    "PhysicalTherapy",
    "Psychology",
    "Nutrition",
    "Podiatric",
    "Midwifery"
  ],
  "availableService": [
    {
      "@type": "MedicalProcedure",
      "name": "Consulta Medicina General",
      "procedureType": "https://schema.org/Diagnostic"
    },
    {
      "@type": "MedicalProcedure",
      "name": "Ortodoncia",
      "procedureType": "https://schema.org/Therapeutic"
    },
    {
      "@type": "MedicalProcedure",
      "name": "Implante Dental",
      "procedureType": "https://schema.org/Therapeutic"
    },
    {
      "@type": "MedicalProcedure",
      "name": "Ecografía Ginecológica y Obstétrica",
      "procedureType": "https://schema.org/Diagnostic"
    },
    {
      "@type": "MedicalProcedure",
      "name": "Kinesiología Musculoesquelética y Respiratoria",
      "procedureType": "https://schema.org/Therapeutic"
    }
  ],
  "sameAs": [
    "https://www.facebook.com/centromedicocarampangue",
    "https://www.instagram.com/centromedicocarampangue/"
  ],
  "potentialAction": {
    "@type": "ReserveAction",
    "target": {
      "@type": "EntryPoint",
      "urlTemplate": "https://wa.me/56966610737?text=Hola%2C%20quiero%20agendar",
      "actionPlatform": [
        "http://schema.org/DesktopWebPlatform",
        "http://schema.org/MobileWebPlatform"
      ]
    },
    "result": {
      "@type": "Reservation",
      "name": "Cita médica"
    }
  }
}
</script>
```

### ⚠️ Antes de pegar, confirmar:
- **Coordenadas geo**: -37.2667, -73.25 son aproximadas para Carampangue. Verificar en Google Maps con la dirección exacta y ajustar.
- **Dirección exacta**: "República 102" — confirmar número de calle.
- **Teléfonos**: chequear que +56412965226 (fijo) y wa.me/56966610737 sean los actuales.

---

## 3. Landing /centro-medico-curanilahue/

### Por qué esta landing
- 20 pacientes/mes de Curanilahue (6.7% del total)
- Cero clics SEO desde Curanilahue
- Curanilahue está a ~30 km de Carampangue (~30 min vía Ruta 160)

### Cómo crearla en WordPress
1. Páginas → Añadir nueva
2. Título: "Centro médico cercano para Curanilahue — CMC Carampangue"
3. Slug: `centro-medico-curanilahue`
4. Pegar el contenido de abajo (modo editor o Elementor)
5. Yoast → SEO title + meta description (más abajo)
6. Publicar

### Yoast settings

```
SEO title (≤60):
Centro Médico para Curanilahue | Especialistas en CMC Carampangue

Meta description (≤155):
¿Necesitas un especialista cerca de Curanilahue? El CMC Carampangue atiende pacientes de Curanilahue. Otorrino, traumatólogo, ginecólogo y más. Agenda por WhatsApp.

Focus keyword: centro medico curanilahue
```

### Contenido de la página (Markdown / pegar como bloques en editor)

```markdown
# Centro médico cercano para pacientes de Curanilahue

**Carampangue queda a 30 minutos de Curanilahue por la Ruta 160.** En el Centro Médico Carampangue atendemos cada mes a más de 20 pacientes de Curanilahue, San José de Cólico y Sargento Aldea, con especialistas y exámenes que no siempre están disponibles en la comuna.

## Especialidades para pacientes de Curanilahue

Si vienes desde Curanilahue, estos son los servicios más solicitados:

| Especialidad | Profesional | Disponibilidad |
|---|---|---|
| Otorrinolaringología | Dr. Manuel Borrego | Semanal |
| Traumatología | Dr. Claudio Barraza | Semanal |
| Cardiología | Dr. Miguel Millán | Semanal |
| Ginecología | Dr. Tirso Rejón | Semanal |
| Gastroenterología | Dr. Nicolás Quijano | Semanal |
| Ecografía obstétrica y ginecológica | Dr. Tirso Rejón | Semanal |
| Ecografía abdominal y musculoesquelética | David Pardo (TM) | Semanal |
| Ortodoncia e Implantes | Dra. Daniela Castillo / Dra. Aurora Valdés | Semanal |

## Cómo llegar desde Curanilahue

- **Auto particular**: Ruta 160 hacia Arauco, ~30 km, 30 min.
- **Bus rural**: línea Curanilahue–Arauco, paradero Carampangue.
- **Estacionamiento gratuito** en el centro.

> **Dirección**: República 102, Carampangue, Provincia de Arauco.

## Horarios y agendamiento

- **Lunes a viernes**: 08:00 – 21:00
- **Sábado**: 10:00 – 14:00

**Agenda directo por WhatsApp**: [+56 9 6661 0737](https://wa.me/56966610737?text=Hola%2C%20vengo%20desde%20Curanilahue%20y%20quiero%20agendar)

O llámanos: **(41) 296 5226**

## Preguntas frecuentes (Curanilahue)

### ¿Atienden Fonasa para pacientes de Curanilahue?
Sí. Atendemos Fonasa Modalidad Libre Elección (MLE) y particular, sin discriminar comuna de residencia.

### ¿Necesito interconsulta del CESFAM Curanilahue?
No. Atendemos directamente sin interconsulta del sistema público. Si traes una, podemos usarla como referencia.

### ¿Tienen ecografías que en Curanilahue no hacen?
Sí. Realizamos ecografías obstétricas, ginecológicas, abdominales y musculoesqueléticas. Resultados el mismo día.

### ¿Atienden niños?
Tenemos Medicina Familiar (Dr. Alonso Márquez) que atiende a familias completas, incluyendo niños. No tenemos pediatra; para casos complejos derivamos al hospital o pediatras de la red.

### ¿Cuánto cuesta una consulta?
Consulta Medicina General: $20.000 particular, copago Fonasa MLE.
Especialidades: entre $25.000 y $40.000 según profesional.

---

**¿Vienes desde Curanilahue?** Agenda tu hora ahora por WhatsApp y un asistente te confirma en minutos.

[💬 Agendar por WhatsApp](https://wa.me/56966610737?text=Hola%2C%20vengo%20desde%20Curanilahue%20y%20quiero%20agendar)
```

### Schema FAQ específico para esta landing (agregar al final)

```html
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "FAQPage",
  "mainEntity": [
    {
      "@type": "Question",
      "name": "¿Atienden Fonasa para pacientes de Curanilahue?",
      "acceptedAnswer": {
        "@type": "Answer",
        "text": "Sí. Atendemos Fonasa Modalidad Libre Elección (MLE) y particular, sin discriminar comuna de residencia."
      }
    },
    {
      "@type": "Question",
      "name": "¿Necesito interconsulta del CESFAM Curanilahue?",
      "acceptedAnswer": {
        "@type": "Answer",
        "text": "No. Atendemos directamente sin interconsulta del sistema público."
      }
    },
    {
      "@type": "Question",
      "name": "¿Atienden niños sin tener pediatra?",
      "acceptedAnswer": {
        "@type": "Answer",
        "text": "Tenemos Medicina Familiar que atiende a familias completas, incluyendo niños. Para casos complejos derivamos al hospital o pediatras de la red."
      }
    }
  ]
}
</script>
```

---

## 4. Landings especialidad+ciudad

Crear 8 páginas siguiendo el mismo patrón. Aquí el esqueleto base que se adapta a cada combinación.

### Lista priorizada
1. `/traumatologo-arauco/` — query con 4 clics/54 imp
2. `/otorrino-arauco/` — variantes "otorrino arauco" + "otorrinolaringologo arauco"
3. `/ginecologo-arauco/`
4. `/odontologo-arauco/`
5. `/traumatologo-curanilahue/`
6. `/otorrino-curanilahue/`
7. `/ginecologo-curanilahue/`
8. `/odontologo-curanilahue/`

### Plantilla (reemplazar `{ESPECIALIDAD}`, `{PROFESIONAL}`, `{CIUDAD}`, `{PRECIO}`, `{DESCRIPCION_ESP}`)

```markdown
# {ESPECIALIDAD} en {CIUDAD} — Centro Médico Carampangue

¿Buscas {ESPECIALIDAD_LOWER} en {CIUDAD}? En el Centro Médico Carampangue atiende **{PROFESIONAL}**, especialista en {ESPECIALIDAD_LOWER}, con disponibilidad semanal y atención presencial a pacientes de toda la Provincia de Arauco.

## ¿Qué tratamos?

{DESCRIPCION_ESP_DETALLADA}

## ¿Cuánto cuesta?

- Consulta particular: ${PRECIO}
- Fonasa MLE: bonificación según nivel
- Isapre: copago según convenio

## Cómo agendar

Por WhatsApp en 30 segundos:
[💬 Agendar con {PROFESIONAL}](https://wa.me/56966610737?text=Hola%2C%20quiero%20agendar%20con%20{PROFESIONAL_URL})

O llama al **(41) 296 5226**.

## Ubicación

República 102, Carampangue, a {DISTANCIA} de {CIUDAD}.

## Preguntas frecuentes

### ¿Necesito interconsulta?
No. Atendemos directamente sin interconsulta del sistema público.

### ¿Atienden Fonasa?
Sí, MLE (Modalidad Libre Elección).

### ¿Cuánto demora la consulta?
{DURACION_CITA} minutos en promedio.
```

### Datos para rellenar (extraídos de medilink.py / CLAUDE.md)

| Especialidad | Profesional | Precio ref | Duración |
|---|---|---|---|
| Traumatología | Dr. Claudio Barraza | $30.000 | 15 min |
| Otorrinolaringología | Dr. Manuel Borrego | $35.000 | 20 min |
| Ginecología | Dr. Tirso Rejón | $35.000 | 20 min |
| Odontología General | Dr. Carlos Jiménez / Dra. Javiera Burgos | $25.000 | 30-60 min |
| Cardiología | Dr. Miguel Millán | $40.000 | 20 min |
| Gastroenterología | Dr. Nicolás Quijano | $40.000 | 20 min |
| Ecografía | David Pardo | $40.000 | 15 min |

### Distancias desde CMC

- Arauco urbano: 8 km / 10 min
- Curanilahue: 30 km / 30 min
- Laraquete: 12 km / 15 min
- Los Álamos: 50 km / 45 min

---

## 5. Blog "Médico para niños en Arauco"

### Objetivo
Capturar las queries `pediatra arauco` (4 clics/29 imp) y `pediatra en arauco` SIN engañar (CMC no tiene pediatra). Se posiciona Medicina Familiar como alternativa.

### Yoast settings

```
SEO title:
¿Pediatra en Arauco? Médico para niños y familias — CMC

Meta description:
¿Buscas pediatra en Arauco? El CMC ofrece Medicina Familiar para niños desde los primeros meses. Atención cercana sin esperas. Agenda por WhatsApp.

Focus keyword: pediatra arauco
URL: /blog/medico-para-ninos-en-arauco/
```

### Contenido

```markdown
# ¿Pediatra en Arauco? Médico para niños y familias

Si buscas un **pediatra en Arauco** o un médico para tu hijo, este artículo te orienta sobre las opciones disponibles en la zona y cómo el Centro Médico Carampangue atiende a niños y familias completas.

## Diferencia entre pediatra y médico de familia

- **Pediatra**: especialista que atiende exclusivamente niños y adolescentes hasta los 15 años.
- **Médico de familia (Medicina Familiar)**: especialista que atiende a la familia completa, desde los primeros meses hasta adultos mayores. Capacitado en pediatría general, control sano, vacunas, problemas respiratorios, gastrointestinales y desarrollo infantil.

Ambos pueden hacer **control sano del niño**, **diagnóstico de patologías comunes** y **derivar a especialista** cuando es necesario.

## ¿Hay pediatra en Carampangue o Arauco?

Actualmente la oferta de pediatría privada en la Provincia de Arauco es limitada. La mayoría de los pediatras están en Concepción o el Hospital de Cañete (sistema público).

En el **Centro Médico Carampangue** no contamos con pediatra, pero sí con **Medicina Familiar** a cargo del **Dr. Alonso Márquez**, que atiende:

- Control sano del lactante y preescolar
- Vacunación y calendario PNI
- Consultas por fiebre, tos, diarrea, vómitos
- Problemas de piel
- Problemas de crecimiento y desarrollo
- Asma, rinitis y patología respiratoria recurrente

Para casos que requieren pediatra subespecialista (cardiología infantil, neurología, endocrinología pediátrica), derivamos a la red de Concepción.

## ¿Cuándo elegir Medicina Familiar para tu hijo?

✅ Control sano de rutina
✅ Cuadros respiratorios y gastrointestinales comunes
✅ Vacunas
✅ Problemas dermatológicos
✅ Cuando quieres un médico que también atienda a los padres y hermanos

## ¿Cuándo conviene un pediatra subespecialista?

🩺 Sospecha de patología cardíaca, neurológica o endocrina
🩺 Recién nacidos con complicaciones perinatales
🩺 Trastornos del desarrollo complejos

## Cómo agendar para tu hijo en CMC

[💬 Agendar Medicina Familiar por WhatsApp](https://wa.me/56966610737?text=Hola%2C%20quiero%20agendar%20Medicina%20Familiar%20para%20mi%20hijo)

O llama al (41) 296 5226. Atendemos pacientes de Arauco, Curanilahue, Laraquete y Los Álamos.

## Preguntas frecuentes

### ¿Atienden recién nacidos?
Sí, desde los primeros días. Para controles de las primeras semanas, recomendamos agendar con anticipación.

### ¿Pueden poner vacunas del PNI?
Sí, somos vacunatorio autorizado. Calendario PNI completo.

### ¿Cuánto cuesta la consulta?
$20.000 particular. Fonasa MLE con copago según nivel.
```

---

## 6. Google Business Profile

### ⚠️ CRÍTICO: posiblemente ya existe
Antes de crear, **buscar en Google Maps** "Centro Médico Carampangue" → si aparece una ficha sin verificar, **reclamarla** (no crear duplicado).

### Pasos para crear/reclamar

1. Ir a https://www.google.com/business
2. Iniciar sesión con cuenta Google del centro (recomendado: cuenta dedicada, no la personal del Dr. Olavarría).
3. Buscar "Centro Médico Carampangue" en el campo de empresa.
   - **Si aparece**: clic en la ficha → "Reclamar esta empresa".
   - **Si no aparece**: clic en "Añadir tu empresa".
4. Completar:
   - **Nombre**: Centro Médico Carampangue
   - **Categoría principal**: Centro médico
   - **Categorías adicionales** (importantísimo, agregar 3-5):
     - Clínica dental
     - Centro de fisioterapia
     - Psicólogo
     - Cardiólogo
     - Nutricionista
   - **Ubicación**: República 102, Carampangue
   - **Zonas de servicio** (CRÍTICO): Arauco, Carampangue, Laraquete, **Curanilahue**, **Los Álamos**, Cañete
   - **Teléfono**: (41) 296 5226
   - **Sitio web**: https://centromedicocarampangue.cl
   - **Horarios**: Lun-Vie 08:00-21:00, Sáb 10:00-14:00
5. **Verificación**: Google envía postal con código a República 102 (5-14 días). Sin esto el perfil no aparece públicamente.

### Una vez verificado

1. **Subir 10+ fotos**:
   - Fachada del edificio
   - Recepción / sala de espera
   - 3-5 boxes de atención (limpios y ordenados)
   - Equipo médico (con consentimiento)
   - Logo CMC
2. **Atributos** (activar):
   - Estacionamiento gratuito
   - Acceso para silla de ruedas
   - Acepta tarjetas
   - Acepta Fonasa
   - Cita por WhatsApp
3. **Servicios**: añadir cada especialidad como servicio individual con descripción y precio.
4. **Producto destacado**: "Agenda por WhatsApp" → link a wa.me.

### Reseñas (post-verificación)

**Meta**: 30 reseñas con 4.5★ promedio en 90 días.

Plan:
- Tras cada cita, el chatbot envía un mensaje pidiendo reseña con link directo a GBP.
- El link directo se obtiene desde GBP → "Pedir reseñas".
- Plantilla del mensaje (ya configurable en chatbot):

> "Hola {nombre}, gracias por confiar en CMC. ¿Nos ayudas con una reseña en Google? Es de gran ayuda para que más pacientes nos conozcan: {LINK_GBP}"

---

## 7. Doctoralia centro médico

### Pasos para crear el perfil de centro

1. Ir a https://pro.doctoralia.cl
2. Clic en "Regístrate gratis" → seleccionar "**Soy una clínica o centro médico**" (no "Soy profesional").
3. Completar formulario:
   - Nombre: Centro Médico Carampangue
   - País: Chile
   - Email: contacto@centromedicocarampangue.cl
   - Tipo: Centro médico privado
4. Verificar email.
5. Una vez dentro del panel:
   - **Información básica**: dirección, teléfonos, horarios, sitio web
   - **Especialidades**: marcar todas las que ofrece el centro
   - **Servicios y precios**: agregar prestaciones con precio referencial
   - **Fotos**: subir 5-10 fotos del centro
   - **Áreas atendidas**: Arauco, Curanilahue, Los Álamos, Cañete (importantísimo)
6. **NO contratar plan pago** (cobra por paciente nuevo, no compensa con el chatbot propio).

### A los doctores
NO crear perfil personal por ellos. Si algún doctor quiere su propio perfil, lo crea él y luego se vincula al centro.

### Beneficio inmediato
- Backlink de alta autoridad (DA ~75) hacia centromedicocarampangue.cl
- Aparición en listados "Centros médicos en Arauco" / "Centros médicos en Curanilahue"
- Reseñas de pacientes (canal alternativo a Google)

---

## 8. Auditoría blog HTML locales

20 archivos `blog-*.html` en `/Users/rodrigoolavarria/`. Estado y acción para cada uno:

| # | Archivo | Especialidad | Estado | Acción recomendada |
|---|---|---|---|---|
| 1 | blog-cardiologia.html | Cardiología | HTML standalone con paleta CMC | Migrar a WP como post |
| 2 | blog-endodoncia.html | Endodoncia | HTML standalone | Migrar a WP |
| 3 | blog-estetica-facial.html | Estética facial | HTML standalone | Migrar a WP |
| 4 | blog-fonoaudiologia.html | Fonoaudiología | HTML standalone | Migrar a WP |
| 5 | blog-ginecologia.html | Ginecología | HTML standalone | Migrar a WP |
| 6 | blog-implantologia.html | Implantología | HTML standalone | Migrar a WP |
| 7 | blog-kinesiologia-domicilio.html | Kine domicilio | HTML standalone | Migrar a WP (long-tail valioso) |
| 8 | blog-kinesiologia-musculoesqueletica.html | Kine ME | HTML standalone | Migrar a WP |
| 9 | blog-kinesiologia-respiratoria.html | Kine respi | HTML standalone | Migrar a WP |
| 10 | blog-kinesiologia.html | Kine general | HTML standalone | Migrar a WP (canónica) |
| 11 | blog-medicina-familiar.html | Med Familiar | HTML standalone | Migrar a WP (clave para query "pediatra") |
| 12 | blog-medicina-general.html | Medicina General | HTML standalone | Migrar a WP |
| 13 | blog-nutricion.html | Nutrición | HTML standalone | Migrar a WP |
| 14 | blog-odontologia-general.html | Odontología | HTML standalone | Migrar a WP |
| 15 | blog-ortodoncia.html | Ortodoncia | HTML standalone | Migrar a WP (alta demanda) |
| 16 | blog-otorrinolaringologia.html | ORL | HTML standalone | Migrar a WP |
| 17 | blog-podologia.html | Podología | HTML standalone | Migrar a WP |
| 18 | blog-psicologia-adulto.html | Psico adulto | HTML standalone | Migrar a WP |
| 19 | blog-psicologia-infantil.html | Psico infantil | HTML standalone | Migrar a WP |
| 20 | blog-traumatologia.html | Traumatología | HTML standalone | Migrar a WP (alta demanda GSC) |

### Cómo migrar (proceso por blog, ~10 min cada uno)

1. Abrir el HTML local en navegador → seleccionar contenido principal (no nav/footer).
2. WordPress → Posts → Añadir nuevo.
3. Pegar contenido (modo "Visual" no "HTML" para no traer estilos inline conflictivos).
4. Yoast: title (≤60), meta description (≤155), slug `kebab-case`, focus keyword.
5. Categoría: crear "Especialidades" si no existe.
6. Imagen destacada: subir/usar una de la mediateca.
7. CTA al final: "Agendar por WhatsApp" → wa.me/56966610737
8. Publicar.

### Atajo
Si los HTML tienen mucho CSS inline pesado, mejor extraer solo el `<main>`/`<article>` y reformatear con bloques Gutenberg. La estructura semántica importa más que mantener el diseño exacto.

### Bonus
Estos 20 blogs + el blog "Médico para niños en Arauco" + 12 posts SEO long-tail de la lista F2.1 = **33 posts publicados** en 90 días → más que suficiente para reactivar el `post-sitemap.xml` y mostrar a Google que el blog está vivo.

---

## 9. Tracking SEO en chatbot

### Objetivo
Medir cuántos pacientes nuevos llegan vía SEO orgánico (vs WhatsApp directo, redes, etc.) y desde qué comuna/landing.

### Dónde implementar
`/Users/rodrigoolavarria/chatbot-cmc/app/flows.py` — primer mensaje de cada nueva sesión.

### Lógica

```python
# En flows.py, función handle_message, al detectar primer mensaje de sesión:

def detect_referral_source(message_text: str, profile: dict) -> dict:
    """
    Detecta el origen del paciente para tracking SEO.
    Retorna dict con tags a agregar al perfil.
    """
    text = message_text.lower()
    tags = []

    # Geo origin (palabras clave que el paciente menciona)
    if any(k in text for k in ["curanilahue", "san josé de cólico", "san jose de colico"]):
        tags.append("geo:curanilahue")
    elif any(k in text for k in ["los álamos", "los alamos"]):
        tags.append("geo:los_alamos")
    elif any(k in text for k in ["cañete", "canete"]):
        tags.append("geo:canete")
    elif any(k in text for k in ["laraquete"]):
        tags.append("geo:laraquete")
    elif any(k in text for k in ["arauco"]):
        tags.append("geo:arauco")

    # Source landing (si el WhatsApp se abrió desde un wa.me con texto pre-cargado)
    # Patrón típico: "Hola, vengo desde Curanilahue y quiero agendar"
    if "vengo desde" in text:
        tags.append("source:landing_geo")
    if "quiero agendar" in text and not profile.get("contactado_antes"):
        tags.append("source:google_organico")

    return {"tags": tags}
```

### Endpoint admin para reportar

Agregar en `app/admin_routes.py`:

```python
@router.get("/admin/api/seo-conversion")
async def seo_conversion(token: str = Depends(verify_token)):
    """Cruza referral_source con citas confirmadas para medir ROI SEO."""
    conn = get_db()
    cursor = conn.execute("""
        SELECT
            json_extract(tags, '$') as tags,
            COUNT(DISTINCT phone) as pacientes,
            SUM(CASE WHEN cita_confirmada THEN 1 ELSE 0 END) as citas
        FROM contact_profiles
        WHERE created_at > date('now', '-30 days')
            AND tags LIKE '%geo:%'
        GROUP BY tags
        ORDER BY pacientes DESC
    """)
    return {"data": [dict(row) for row in cursor.fetchall()]}
```

Llamada de prueba:
```bash
curl "https://agentecmc.cl/admin/api/seo-conversion?token=cmc_admin_2026"
```

---

## 10. Plantillas respuesta reseñas

### Reseña 5 estrellas

> Muchas gracias {nombre} por tu reseña. Es un orgullo que confíes en el equipo del Centro Médico Carampangue. Te esperamos cuando lo necesites.
> — Equipo CMC

### Reseña 4 estrellas

> Gracias {nombre} por tu evaluación. Nos alegra saber que tu experiencia fue positiva y nos comprometemos a seguir mejorando. Si tienes algún comentario específico, escríbenos a contacto@centromedicocarampangue.cl
> — Equipo CMC

### Reseña 3 estrellas o menos

> Hola {nombre}, lamentamos que tu experiencia no haya sido la esperada. Tu opinión es muy importante para mejorar. ¿Podrías escribirnos a contacto@centromedicocarampangue.cl con más detalles? Queremos resolverlo personalmente.
> — Dr. Rodrigo Olavarría, Director CMC

### Reseña difamatoria o falsa

1. NO responder confrontacionalmente.
2. Reportar a Google → "Reportar como inapropiada" → categoría "Conflicto de intereses" o "Spam".
3. Si después de 7 días sigue, responder breve:
> Hola, no encontramos registros de tu visita al centro. Si fuiste atendido, escríbenos a contacto@centromedicocarampangue.cl para ayudarte.

---

## Próximas decisiones del usuario

Cuando vuelva, decidir:

1. ¿Aplicar title+description hoy mismo? (15 min, sin riesgo)
2. ¿Quién crea Doctoralia y GBP? (necesita verificación postal de 5-14 días)
3. ¿Empezamos por Curanilahue o por las 4 landings de Arauco?
4. ¿Confirma la dirección exacta y geo coordinates para el schema?
5. ¿Migra blogs HTML a WP él mismo o agendar dev/asistente?
