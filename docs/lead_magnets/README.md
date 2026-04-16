# Lead Magnets CMC — Carpeta maestra

Material educativo descargable para el Centro Médico Carampangue.
**Una guía premium** (Mitos y Realidades de Salud en Arauco, 17 páginas) + **10 documentos operativos** (checklists, calendarios, formularios, emergencias).

## Estructura

```
lead_magnets/
├── README.md                       ← este archivo
├── 01–10_*.md                      ← fuente original en Markdown
├── html/                           ← versión HTML imprimible A4
│   ├── index.html                  ← portal de navegación
│   ├── style.css                   ← sistema de diseño (tokens + print)
│   └── 01–10_*.html
└── canva/                          ← brief + textos listos para Canva/Figma
    ├── DESIGN_BRIEF.md
    ├── brand_tokens.json
    ├── covers.md
    └── textos_canva/01–10_*.txt
```

## Dos formas de producir los PDFs finales

### A) Vía HTML (rápido · 10 segundos por documento)
1. Abre `html/index.html` en Chrome
2. Click en el documento que quieras imprimir
3. `Ctrl/Cmd + P` → Destino: **Guardar como PDF** → Tamaño **A4** → Márgenes **Ninguno**
4. Guarda con el nombre del documento

Resultado: PDF profesional con la marca CMC, listo para enviar por WhatsApp o subir a la landing.

### B) Vía Canva/Figma (1–2 horas · versión con fotos, iconos custom, más diseño)
1. Lee `canva/DESIGN_BRIEF.md` (paleta, tipografía, componentes)
2. Importa `canva/brand_tokens.json` en Figma (plugin "Tokens Studio") o crea el Brand Kit en Canva
3. Copia el contenido desde `canva/textos_canva/NN_*.txt` directo a tu lienzo
4. Sigue las portadas definidas en `canva/covers.md`
5. Exporta como PDF

## Lista de documentos

| # | Título | Uso | Riesgo médico-legal |
|---|---|---|---|
| **00** | **Mitos y Realidades Arauco (★ premium)** | **Lead magnet principal del sitio** | **Medio — citado con fuentes MINSAL/ISP/Scielo** |
| 01 | Checklist pre-consulta | Formulario útil a todo paciente | Muy bajo |
| 02 | Calendario de vacunas PNI | Familias con niños, adultos mayores | Muy bajo (es información pública MINSAL) |
| 03 | Directorio de emergencias Arauco | Imprimir para refrigerador | Bajo |
| 04 | Guía Fonasa y GES | Info previsional | Bajo |
| 05 | Antecedentes familiares | Formulario personal | Muy bajo |
| 06 | Preparación de exámenes | Pre-lab, ecografías, endoscopía | Bajo |
| 07 | Guía prequirúrgica | Pacientes quirúrgicos | Medio — enfoque logístico, no clínico |
| 08 | Calculadora de IMC | Educativo general | Bajo — no diagnostica |
| 09 | Diario postoperatorio | Pacientes quirúrgicos | Bajo |
| 10 | Cuándo llamar al SAMU 131 | Toda la familia | Muy bajo (criterio MINSAL) |

## Kits temáticos sugeridos

- **Kit Bienvenida** (todo paciente nuevo): 01 + 03 + 04
- **Kit Familias con niños**: 02 + 03 + 10
- **Kit Quirúrgico**: 07 + 09 + 10 + 03
- **Kit Crónicos / adultos mayores**: 05 + 08 + 02 + 10
- **Kit Emergencias rural Arauco**: 03 + 10 + guia_lead_magnet_arauco

## Antes de publicar

- [ ] Reemplaza `[mes/año]` por la fecha real en cada disclaimer
- [ ] Verifica teléfonos y web del CMC
- [ ] Exporta todos los PDFs y pésalos (objetivo: &lt; 2 MB para envío WA)
- [ ] Guarda copias en la carpeta compartida del CMC
- [ ] Sube los PDFs al sitio web (/sitio) como lead magnets reales
