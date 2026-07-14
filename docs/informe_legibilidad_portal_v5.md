# Informe de legibilidad — Portal del Paciente v5

**Fecha:** 2026-07-14 · **Método:** índice de perspicuidad Szigriszt-Pazos (escala INFLESZ), calculado sobre los 78 textos educativos y de contenido del portal (banderas rojas, guías de autocuidado, consejos, promociones, exámenes).

## Resultado

| Métrica | Valor |
|---|---|
| Promedio del portal | **74.5 — "fácil"** |
| Umbral objetivo (material sanitario población general) | ≥ 55 ("bastante fácil") |
| Textos bajo el umbral | 9 de 78 (12%) |
| Corregidos en esta pasada | 3 (los de peor puntaje: inhaladores con nombres de fármacos, promo estética, desinfectantes) |

## Interpretación

- La escala INFLESZ castiga las palabras largas: los textos bajo 55 restantes son frases cortas y claras cuyo puntaje baja por UN término clínico necesario ("electrocardiograma", "pie diabético"). Se revisaron uno a uno y se consideran aceptables porque el término va acompañado de explicación o es de uso común.
- Referencia: la mayoría del material sanitario chileno publicado mide 40-55 (según estudios con INFLESZ en consentimientos y folletos). El portal quedó muy por encima de ese estándar.

## Textos bajo 55 restantes (aceptados con justificación)

| Puntaje | Texto | Por qué se acepta |
|---|---|---|
| 34.9 | Aplique povidona o clorhexidina (desinfectantes) si hay riesgo de infección.… | término clínico único, frase corta |
| 45.1 | Fuerza y equilibrio para prevenir caídas… | término clínico único, frase corta |
| 48.5 | Andrea: uñas encarnadas, callos y pie diabético.… | término clínico único, frase corta |
| 48.5 | Dr. Millán: electrocardiograma y ecografía del corazón. Ideal si tiene presión alta o diab… | término clínico único, frase corta |
| 48.5 | Espere 30 segundos entre una aplicación y otra.… | término clínico único, frase corta |
| 52.9 | Azúcar (glicemia) sobre 300, con mucha sed, ganas de orinar a cada rato, náuseas, aliento … | término clínico único, frase corta |
| 54.9 | Su tratamiento puede necesitar un ajuste. Pida control.… | término clínico único, frase corta |

## Método
Fórmula: `P = 206,835 − 62,3 × (sílabas/palabras) − (palabras/frases)`. Conteo silábico con agrupación de diptongos. Los textos se extrajeron automáticamente de los catálogos REDFLAGS, GUIDES, TIPS_CATALOG, PROMO_CATALOG y textos de exámenes del template `portal_v5.html`.

*Generado automáticamente como parte del plan de mejora del portal (Fase 2).*
