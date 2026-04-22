# Design Brief — Lead Magnets CMC (basado en Manual Oficial)
## Para maquetar en Canva Pro siguiendo las normas gráficas del CMC

> **Fuente única de verdad:** este documento traduce el `MANUALCMC.pdf` (Manual de Normas Gráficas) a una guía operativa para reproducir los lead magnets en Canva Pro al 100% on-brand.

---

## 1. Marca CMC — fundamentos

### Identidad
- **Nombre:** Centro Médico Carampangue
- **Lockup:** "CARAMPANGUE" arriba (azul, bold) + "CENTRO MÉDICO" abajo (turquesa, regular, tracking ancho)
- **Isotipo:** Cruz médica formada por una **C turquesa** que abraza una **cruz azul corporativa**, con sombra azul navy. Inspirado en la C de Carampangue + la cruz médica.

### Paleta oficial (**solo estos colores son la marca**)
| Color | Hex | RGB | CMYK | Uso |
|---|---|---|---|---|
| Turquesa | `#4FBECE` | 79, 190, 206 | 64/0/21/0 | Acento principal, franjas decorativas, "C" del isotipo |
| Azul corporativo | `#1172AB` | 17, 114, 171 | 86/48/12/1 | Texto del logotipo, titulares H2, links |
| Azul navy | `#1A3F75` | 26, 63, 117 | — | Cruz exterior del isotipo, cuerpo de títulos |

### Acentos funcionales (NO son parte de la paleta principal — usarlos solo cuando sea estrictamente necesario)
| Color | Hex | Uso |
|---|---|---|
| Rojo | `#c0392b` | **Solo emergencias** (SAMU 131, callouts danger) |
| Amarillo | `#d4a017` | Advertencias (warn) |
| Verde | `#2e7d57` | Success / información GES positiva |

### Tipografía oficial — Montserrat
**Una sola tipografía para todo.** El manual indica Montserrat Bold; usamos toda la familia (300/400/500/600/700/800) para jerarquía.

| Estilo | Tamaño | Peso | Tracking | Uso |
|---|---|---|---|---|
| H1 portada | 32pt | 700 | -0.5px | Título principal de portada (UPPERCASE) |
| H2 sección | 16pt | 700 | -0.2px | Títulos de sección con borde turquesa abajo (UPPERCASE) |
| H3 subsección | 12pt | 600 | normal | Sub-secciones (color navy) |
| H4 label | 10pt | 700 | 0.12em | Labels (UPPERCASE) |
| Body | 10.5pt | 400 | normal | Cuerpo de texto (line-height 1.55) |
| Kicker | 8.5pt | 700 | 0.22em | Antetítulos (UPPERCASE, color turquesa) |
| Big number | 26pt | 800 | -1px | KPIs y números destacados |
| SAMU 131 | 44–56pt | 800 | -1px | Solo en cajas de emergencia (color rojo) |

> Montserrat es **gratis en Google Fonts** y viene preinstalado en Canva. No hace falta subirla.

---

## 2. Sistema de página A4

### Dimensiones
- **Tamaño:** 210 × 297 mm (A4 vertical)
- **Márgenes internos:** 22 mm top + 18 mm laterales + 18 mm bottom

### Decoración fija (TODAS las páginas internas, no la portada)
**Franja superior + franja inferior — 4mm de alto cada una:**
```
[ TURQUESA #4FBECE 0–10% ][ AZUL CORPORATIVO #1172AB 10–100% ]
```
Esta es la firma visual del manual. Cada página interna debe llevar ambas franjas.

### Header / footer textuales (sobre las franjas)
- **Header:** "CENTRO MÉDICO CARAMPANGUE · MATERIAL EDUCATIVO" · 7.5pt · UPPERCASE · letter-spacing 0.18em · color azul corporativo · weight 600
- **Footer:** "agentecmc.cl · +56 9 4588 6628 · (44) 296 5226" · 7.5pt · centrado · color tinta suave · weight 500

### Portada (sin franjas decorativas — el gradiente cubre toda la hoja)
Gradiente diagonal 160° en una de las 5 variantes:
- **Azul** (default): `#1172AB → #1A3F75` · documentos institucionales
- **Navy** (premium / serio): `#1A3F75 → #0f2a52` · guía de mitos, postoperatorio
- **Turquesa** (educativo / wellness): `#4FBECE → #1172AB` · IMC, vacunas, antecedentes
- **Rojo** (solo emergencias): `#c0392b → #7a1f18` · SAMU, directorio emergencias
- **Verde** (GES / bienestar): `#2e7d57 → #1f5a3d` · uso opcional para contenido GES positivo

---

## 3. Componentes reutilizables (Brand Kit Canva)

### Logo mark
1. Si tiene el SVG/PNG oficial del isotipo CMC → úselo siempre.
2. Si no, en placeholder use: cuadrado blanco 64×64 px, esquinas redondeadas 8px, con un "+" gigante azul (Montserrat 38pt 800) y una "C" turquesa pequeña arriba a la izquierda.
3. **Espacio de respeto:** mínimo X = altura de la "C" del logotipo a cada lado.

### Componentes a guardar como "Elementos de Marca" en Canva Pro
1. **Header decorativo** (rectángulo 210mm × 4mm con gradiente turquesa/azul).
2. **Footer decorativo** (idem).
3. **Header textual** ("CENTRO MÉDICO CARAMPANGUE · MATERIAL EDUCATIVO").
4. **Footer textual** ("agentecmc.cl · +56 9 4588 6628 · (44) 296 5226").
5. **Logo mark** (en blanco sobre fondo color y en color sobre fondo blanco).
6. **Callout info** (fondo azul claro, borde izquierdo azul, label "INFO" en pill azul).
7. **Callout success** (verde claro + verde + label "GES" o "ÉXITO").
8. **Callout warn** (amarillo claro + amarillo + label "ATENCIÓN").
9. **Callout danger** (rojo claro + rojo + label "URGENCIA").
10. **Card** (fondo blanco + borde superior turquesa 3pt + radius 2mm + sombra suave).
11. **Phone box SAMU** (caja blanca con borde rojo 2pt, número 131 grande rojo).
12. **Chip / pill** (fondo turquesa claro, texto azul, radius 10mm, padding 1×3mm).

---

## 4. Estructura estándar de cada documento

```
Página 1 — PORTADA (full-bleed gradient, sin franjas)
  ├─ Tag "CENTRO MÉDICO CARAMPANGUE" pequeño arriba (uppercase, tracking ancho)
  ├─ Logo mark (cuadrado blanco con + azul)
  ├─ Kicker turquesa "Documento ##"
  ├─ H1 32pt UPPERCASE bold
  ├─ Subtítulo 12pt regular
  └─ Footer "Carampangue · Región del Biobío" + "CMC · ##"

Páginas 2–N — CONTENIDO (con franjas turquesa+azul arriba y abajo)
  ├─ Header textual minúsculo arriba
  ├─ Kicker turquesa por sección
  ├─ H2 con línea inferior turquesa
  ├─ Subsecciones con H3 navy
  ├─ Callouts según tipo (info/success/warn/danger)
  ├─ Tablas con header azul corporativo
  ├─ Cards con borde superior turquesa
  └─ Footer textual minúsculo abajo

Página Final — CIERRE
  ├─ Señales de alarma (callout danger)
  ├─ 3 canales CMC (WhatsApp / teléfono / web)
  └─ Disclaimer legal en cursiva 8pt
```

---

## 5. Paso a paso en Canva Pro

### Setup inicial
1. Canva Pro → **Crear diseño → Tamaño personalizado → 210 × 297 mm**.
2. Ir a **Marca → Brand Kit** y subir:
   - Logo CMC (SVG o PNG con fondo transparente)
   - Colores: pega los hex de la paleta oficial de arriba
   - Fuentes: Montserrat (ya viene en Canva)
3. Crear un **template "Página interna CMC"** con las franjas turquesa+azul + header + footer ya posicionados → Marcar como **Plantilla de marca** (icono de chincheta).
4. Crear un **template "Portada CMC"** con el gradiente full-bleed + logo + estructura de título → Plantilla de marca también.

### Para cada documento (1 → 11)
1. Duplicar la plantilla "Portada CMC" → cambiar gradiente según tipo (ver tabla en `covers.md`).
2. Pegar el título y subtítulo desde `textos_canva/NN_*.txt`.
3. Duplicar la plantilla "Página interna CMC" tantas veces como páginas tenga el documento.
4. Pegar el contenido de `textos_canva/NN_*.txt` respetando la jerarquía:
   - `▸` → H2
   - `•` → H3
   - `☐` → checkbox de checklist
   - texto plano → body
5. Aplicar callouts con un click (se llaman desde el panel "Elementos de marca").
6. Exportar como **PDF Estándar → Aplanar PDF: SÍ**.

### Importar PDFs ya generados a Canva Pro como editables
1. Canva Pro → **Subir → seleccionar archivo PDF** desde `pdfs/` de este repo.
2. Canva los importará como diseños editables (cada página queda como una página de Canva).
3. Reemplazar el placeholder del logo por el SVG real.
4. Ajustar tipografía si fue importada como otra fuente (forzar a Montserrat).
5. Re-exportar.

> **Tip Pro:** los PDFs generados via `bash build_pdfs.sh` ya están on-brand. Subirlos a Canva ahorra el trabajo de maquetar de cero.

---

## 6. Iconografía sugerida

Buscar en Canva con keyword **"line icon"** o **"outline"** para mantener un look clínico moderno.

| Sección | Icono | Canva keyword |
|---|---|---|
| Documentos | 📋 | "clipboard line" |
| Medicamentos | 💊 | "pill line" |
| Emergencia | 🚨 | "siren outline" |
| Infarto | 🫀 | "heart anatomy line" |
| ACV | 🧠 | "brain outline" |
| Respiratorio | 🫁 | "lungs line" |
| Pediatría | 👶 | "baby outline" |
| Embarazo | 🤱 | "pregnancy line" |
| Quemaduras | 🔥 | "fire warning outline" |
| Araña rincón | 🕷️ | "spider outline" |
| Marea roja | 🌊 | "wave warning" |
| Vacunas | 💉 | "syringe line" |
| Cirugía | 🏥 | "hospital cross" |
| IMC | ⚖️ | "scale balance line" |
| Hantavirus | 🐭 | "rodent outline" |
| Celulosa | 🏭 | "industry outline" |

---

## 7. Checklist de revisión antes de publicar

- [ ] Paleta = solo turquesa #4FBECE + azul #1172AB + navy #1A3F75 (acentos rojo/amarillo/verde solo cuando aplique)
- [ ] Tipografía = Montserrat únicamente
- [ ] Franjas decorativas turquesa/azul arriba y abajo de cada página interna
- [ ] Logo presente en portada (con espacio de respeto)
- [ ] Datos de contacto: **(44) 296 5226** y **+56 9 4588 6628**
- [ ] Web: `agentecmc.cl` o `centromedicocarampangue.cl`
- [ ] Disclaimer legal al final
- [ ] Fecha "Última revisión: [mes/año]" reemplazada por la fecha real
- [ ] Sin errores ortográficos
- [ ] Teléfonos de emergencia verificados (SAMU 131, CITUC 22 635 3800)
- [ ] PDF bajo 2 MB para envío por WhatsApp
- [ ] Archivo de origen guardado en la carpeta compartida del CMC

---

## 8. Archivos disponibles en este brief

```
canva/
├── DESIGN_BRIEF.md            ← este archivo (basado en Manual oficial CMC)
├── brand_tokens.json          ← tokens exportables (Figma Variables / Tokens Studio)
├── covers.md                  ← mapa de gradiente por documento
└── textos_canva/
    ├── 01_checklist.txt       ← texto plano listo para copiar
    ├── 02_vacunas.txt
    ├── 03_emergencias.txt
    ├── 04_fonasa.txt
    ├── 05_familia.txt
    ├── 06_examenes.txt
    ├── 07_prequirurgica.txt
    ├── 08_imc.txt
    ├── 09_postoperatorio.txt
    └── 10_samu131.txt
```

---

## 9. Flujo recomendado

1. **HTML → PDF** es lo más rápido para v1 (usar `bash build_pdfs.sh`).
2. **Subir esos PDFs a Canva Pro** y editarlos como diseños → ahorra todo el trabajo de maquetar.
3. **Reemplazar logo** placeholder por el SVG oficial del CMC.
4. **Exportar** desde Canva como PDF aplanado.
5. **Subir al sitio** `centromedicocarampangue.cl` o enviar por WhatsApp.

> Si el equipo de CMC tiene un diseñador o agencia, este brief + el `brand_tokens.json` + los HTMLs ya maquetados son suficientes para que reproduzcan todo en Canva o Figma sin preguntas.
