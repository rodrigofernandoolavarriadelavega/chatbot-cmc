# Design Brief — Lead Magnets CMC
## Para maquetar en Canva, Figma o entregarse a diseñador

> Este documento reúne la especificación de marca y las instrucciones exactas para reproducir los 10 lead magnets del Centro Médico Carampangue en Canva, Figma o cualquier herramienta de diseño.

---

## 1. Sistema de marca

### Paleta de colores
| Token | Hex | Uso |
|---|---|---|
| Verde salud | `#2e7d57` | Acento principal, tablas, cards primarios |
| Verde oscuro | `#1f5a3d` | Títulos, portada principal |
| Verde claro | `#e8f3ee` | Fondo de callouts informativos |
| Azul confianza | `#2b6b8a` | H3, cards secundarios, chips |
| Azul claro | `#eaf1f6` | Fondo callout info |
| Oro cálido | `#c89a3a` | Acento warn, portada cálida |
| Rojo urgencias | `#b3332b` | SAMU, callouts danger, portada emergencias |
| Rojo claro | `#fbe9e7` | Fondo callout danger |
| Tinta | `#1c2a26` | Cuerpo de texto |
| Tinta suave | `#4a5854` | Secundario, subtítulos |
| Línea | `#d9e1de` | Bordes de tabla, divisores |
| Papel | `#fafaf7` | Fondo de página |

### Tipografía
- **Títulos (h1, h2, h3)**: Georgia · serif · 34 / 17 / 13 pt
- **Cuerpo**: Inter (o Helvetica Neue / Arial) · 11 pt · line-height 1.55
- **Kicker / labels**: sans-serif · 9 pt · UPPERCASE · letter-spacing 0.15em
- **Números grandes**: Georgia · 28–56 pt · weight 700

### Tamaño hoja
- A4 vertical · 210 × 297 mm
- Márgenes internos: 22 mm top (con header), 18 mm laterales, 18 mm bottom
- Sangrado de portada: 0 (fondo a borde)

### Header / footer estándar
- **Header**: "CENTRO MÉDICO CARAMPANGUE · MATERIAL EDUCATIVO" · 8 pt · uppercase · letter-spacing 0.14em · color tinta suave · borde inferior 1 px
- **Footer**: "agentecmc.cl · +56 9 4588 6628 · (41) 296 5226" · 8 pt · centrado · borde superior 1 px

### Logo mark
Círculo blanco 54 × 54 px · letra "C" en Georgia 22 pt weight 800 · color verde salud.
> Si tienes el logo oficial del CMC en SVG/PNG, úsalo en vez del circle-mark.

### Portadas (4 variantes)
Gradiente diagonal 160° de un tono oscuro a uno aún más oscuro del mismo color:
- **Verde** (default): `#2e7d57 → #1f5a3d`
- **Roja** (urgencias): `#b3332b → #7a1f18`
- **Azul** (informativa): `#2b6b8a → #14445b`
- **Oro** (formularios personales): `#c89a3a → #8a6818`
- **Tinta** (clínica/seriedad): `#2c3e37 → #0f1a16`

### Componentes reutilizables
1. **Callout**: recuadro con borde izquierdo 4 px + fondo claro + label-pill arriba a la izquierda. Variantes: info (azul), success (verde), warn (oro), danger (rojo).
2. **Card**: rectángulo blanco · radius 2 mm · border 1 px línea · padding 4 mm · shadow suave.
3. **Chip/pill**: cápsula pequeña · padding 1×3 mm · radius 10 mm · fondo color-lt · texto color-dk · 9 pt.
4. **Phone-box**: caja con borde rojo 2 px · número SAMU grande en Georgia 30 pt rojo · label uppercase arriba.
5. **TOC-line**: fila con número + título + puntos + página (borde inferior dotted).
6. **Fill line**: línea gris 1 px para escribir a mano (formularios).

---

## 2. Estructura estándar de cada documento

```
Portada (1 página)
 ├─ Header con nombre CMC pequeño
 ├─ Logo mark
 ├─ Kicker "Documento ##"
 ├─ H1 título grande (Georgia 34 pt)
 ├─ Subtitle
 └─ Footer con ubicación y ID (CMC · ##)

Contenido (2–4 páginas)
 ├─ Kicker por sección
 ├─ H2 con línea inferior verde
 ├─ Subsecciones con H3 azul
 ├─ Callouts para advertencias
 ├─ Tablas con header verde
 └─ Cards/grids para contenido modular

Cierre
 ├─ Señales de alarma (callout danger)
 ├─ Datos de contacto CMC
 └─ Disclaimer legal en cursiva 8.5 pt
```

---

## 3. Guía rápida para Canva

### Paso a paso
1. Abre Canva → **Crear diseño → Tamaño personalizado → 210 × 297 mm**
2. Sube los colores de marca a **Marca** (Brand Kit) usando los hex de arriba.
3. Sube las fuentes: Inter desde Google Fonts, Georgia ya viene incluida.
4. Crea la **portada** (1 página): rectángulo full-bleed con el gradiente correspondiente al tipo de documento + logo mark + texto.
5. Duplica la página para **contenido**: añade header y footer como elementos fijos, guárdalos como **elementos de Marca** para reusar.
6. Para cada documento usa el texto del archivo `textos_canva/XX_*.txt` (copy-paste directo).
7. Exporta como **PDF Estándar → Descargar con marcas de recorte: NO · Aplanar PDF: sí**.

### Plantillas Canva recomendadas para inspiración
Busca estos términos en Canva (no son templates exactos, son puntos de partida visuales):
- "Health brochure A4 printable" — base limpia
- "Medical patient guide" — estructura clínica
- "Healthcare infographic pastel" — paleta similar
- "Emergency contact list printable" — para documento 03
- "Vaccination schedule Spanish" — para documento 02

### Organización en Canva
Crea una **carpeta "CMC — Lead Magnets"** con subcarpetas por tipo:
- `Emergencias` (doc 03, 10)
- `Informativo` (doc 02, 04, 06, 07)
- `Formularios` (doc 01, 05, 09)
- `Educativo` (doc 08)

---

## 4. Guía rápida para Figma

### Paso a paso
1. Crea un nuevo archivo **CMC Lead Magnets**.
2. Crea un frame A4 (210 × 297 mm) llamado **Page Template**.
3. Define **Styles**:
   - Text: `Serif/H1`, `Serif/H2`, `Sans/Body`, `Sans/Kicker`
   - Color: `CMC/Green`, `CMC/Blue`, `CMC/Red`, `CMC/Gold`, etc.
   - Effects: `Card shadow`, `Cover gradient Green`, etc.
4. Crea **Components**:
   - `Cover` (con variants: green, blue, red, gold, ink)
   - `Header`, `Footer`
   - `Callout` (variants: info, success, warn, danger)
   - `Card`, `Phone Box`, `Chip`, `Fill Line`, `Checkbox`
5. Duplica el page template 1 vez por página de cada documento.
6. Usa el **plugin "Content Reel"** para auto-rellenar textos largos, o pega directo desde los archivos `textos_canva/*.txt`.
7. Exporta cada documento como **PDF**: selecciona todos los frames del documento → **Export → PDF**.

### Plugins útiles
- **Iconify** — iconos de emergencia, médicos
- **Content Reel** — texto de relleno
- **To PDF** — exportar múltiples frames a un solo PDF
- **Unsplash** — fotos de stock (úselas con criterio médico apropiado)

---

## 5. Iconografía sugerida

| Sección | Icono | Canva keyword |
|---|---|---|
| Documentos | 📋 | "clipboard checkmark" |
| Medicamentos | 💊 | "pill capsule" |
| Emergencia | 🚨 | "emergency siren" |
| Infarto | 🫀 | "heart anatomy" |
| ACV | 🧠 | "brain health" |
| Respiratorio | 🫁 | "lungs" |
| Pediatría | 👶 | "baby care" |
| Embarazo | 🤱 | "pregnancy" |
| Quemaduras | 🔥 | "fire warning" |
| Araña | 🕷️ | "spider warning" |
| Marea roja | 🌊 | "wave red" |
| Vacunas | 💉 | "syringe vaccine" |
| Cirugía | 🏥 | "hospital surgery" |
| IMC | ⚖️ | "scale balance" |

Prefiere iconos **line / outlined** para un look clínico y moderno. Evita iconos "cartoon" salvo en la guía pediátrica.

---

## 6. Checklist de revisión antes de publicar

- [ ] Datos de contacto actualizados: **(41) 296 5226** y **+56 9 4588 6628**
- [ ] Web correcta: `agentecmc.cl`
- [ ] Disclaimer legal al final de cada documento
- [ ] Fecha "Última revisión: [mes/año]" reemplazada por la fecha real
- [ ] Sin errores ortográficos (úsese el corrector de Canva/Figma)
- [ ] Teléfonos de emergencia verificados (SAMU 131, CITUC 22 635 3800)
- [ ] PDF bajo 2 MB por archivo para envío por WhatsApp
- [ ] Archivo de origen guardado en la carpeta compartida del CMC

---

## 7. Archivos disponibles en este brief

```
canva/
├── DESIGN_BRIEF.md            ← este archivo
├── brand_tokens.json          ← tokens exportables (Figma Variables)
├── textos_canva/
│   ├── 01_checklist.txt       ← texto plano listo para copiar
│   ├── 02_vacunas.txt
│   ├── 03_emergencias.txt
│   ├── 04_fonasa.txt
│   ├── 05_familia.txt
│   ├── 06_examenes.txt
│   ├── 07_prequirurgica.txt
│   ├── 08_imc.txt
│   ├── 09_postoperatorio.txt
│   └── 10_samu131.txt
└── covers.md                  ← mapa de qué gradiente usa cada documento
```

---

## 8. Uso recomendado de los HTML imprimibles

Además de Canva/Figma, en la carpeta `html/` hay versiones HTML A4 ya estilizadas que puedes:
- Abrir en Chrome → **Imprimir → Guardar como PDF** para obtener un PDF profesional en 10 segundos
- Editar el CSS (`style.css`) si quieres ajustar colores/tipografía antes de exportar
- Enviar directo por WhatsApp como PDF sin necesidad de Canva

> **Flujo sugerido**: HTML → PDF es lo más rápido para v1. Usa Canva/Figma cuando quieras diseño ilustrado con fotos, iconos personalizados o versiones para Instagram/RRSS.
