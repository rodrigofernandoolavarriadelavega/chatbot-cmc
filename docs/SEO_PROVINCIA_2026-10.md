# SEO Provincia de Arauco, octubre 2026

Fuentes: Search Console (`gsc_diario`, `gsc_paginas_diario`, 3-may a 5-oct-2026, solo lectura en prod), fichas y citas de Medilink por localidad (`heatmap_cache.db`, localidad resuelta por dirección con `localidades_arauco.resolver`, citas de los últimos 12 meses), precios y profesionales del código del bot (`flows.PRECIOS_SLOT`, `claude_helper.SYSTEM_PROMPT`, `medilink.PROFESIONALES`) y días de la tabla de horarios de la portada. Distancias: ruteo vial OSRM/OpenStreetMap del 6-oct-2026, redondeadas.

## Hallazgo central

El sitio ya tenía 22 especialidades x 10 localidades = 220 URLs `/blog/{especialidad}-{localidad}`, todas con el mismo cuerpo y 3 párrafos de contexto. Varias rankean (pos 4-9) pero con CTR bajo. Lo que faltaba no eran URLs, era contenido propio. Por eso se reescribieron 28 de esas URLs (misma URL, mismo canonical, sin canibalización) y se creó 1 URL nueva.

Datos que mandan:
- Marca: "centro medico carampangue" 9.500 apariciones, pos 1,1-3,3, 630 clics: bien. "carampangue" solo: 6.000 apariciones, pos 8,2, 9 clics: es consulta informativa (mapa, clima), no de paciente.
- Demanda real 12 meses (citas / pacientes): Curanilahue 1.588 / ~400 (MG, ortodoncia, ORL, eco); Los Álamos 430 (ortodoncia 158 citas, MG, ORL); Lebu 96 (ORL primera); Cañete 129 (ortodoncia, ORL, psiquiatría tele, MG). Cañete tiene 3.800 apariciones y casi no tiene pacientes: se ve, no convierte (80 min de viaje).
- Traumatología, dermatología, urología y pediatría: ~700 apariciones y el centro NO las ofrece (traumatología deshabilitada en código). No se hacen páginas.

## Puntaje (0-12)

G: apariciones GSC (0-3). P: posición mejorable, 4 a 10 (0-2). D: pacientes 12m (0-3). M: ticket/valor de la especialidad (0-2). T: viaje corto o sin viaje por teleconsulta (0-2). Umbral para construir: 8.

| Localidad | Especialidad | Pts | Decisión |
|---|---|---|---|
| Curanilahue | Otorrinolaringología | 12 | Reescrita |
| Curanilahue | Ecografía | 12 | Reescrita |
| Arauco | Ecografía | 12 | Reescrita (ya rankea pos 5,4, 115 clics: se agrega contenido, título se mantiene en lo posible) |
| Laraquete | Gastroenterología | 12 | Reescrita, con Laraquete real (ranqueaba por consultas de Cañete) |
| Curanilahue | Cardiología | 11 | Reescrita |
| Curanilahue | Medicina general | 11 | Reescrita |
| Arauco | Otorrinolaringología | 11 | Reescrita |
| Arauco | Gastroenterología | 11 | Reescrita |
| Arauco | Medicina general | 11 | Reescrita |
| Arauco | Psiquiatría (tele) | 10 | Reescrita |
| Arauco | Oftalmología (examen de la vista) | 9 | Reescrita, aclara que es tecnóloga médica |
| Arauco | Odontología | 9 | Reescrita |
| Laraquete | Psicología adulto | 9 | Reescrita |
| Los Álamos | Odontología/ortodoncia | 9 | Reescrita |
| Los Álamos | Medicina general | 9 | Reescrita |
| Lebu | Odontología | 9 | Reescrita |
| Cañete | Odontología | 9 | Reescrita |
| Curanilahue | Ortodoncia | 8 | Reescrita |
| Arauco | Atención de niños (consulta "pediatra") | 8 | NUEVA `/blog/pediatra-arauco`, dice que no hay pediatra |
| Lebu | Medicina general | 8 | Reescrita |
| Lebu | Ecografía | 8 | Reescrita |
| Cañete | Ecografía / Cardiología / Medicina general / ORL / Psiquiatría | 8 | Reescritas (5) |
| Los Álamos | ORL | 5 | Reescrita igual: ORL es la 3a atención de la localidad y completa el grupo de ruta |
| Cañete | Gastroenterología | 7 | Reescrita: evita que "gastroenterólogo cañete" caiga en la página de Laraquete |
| Los Álamos | Ecografía | 7 | No: sin señal GSC |
| Cañete | Neurología (tele) | 7 | No: 0 pacientes, volumen mínimo |
| Cañete | Oftalmología | 6 | No: 1 paciente, demanda sin conversión |
| Curanilahue | Ginecología, Gastroenterología | 6 | No: sin señal GSC (se monitorea) |
| Ramadilla (cualquiera) | 9 | No: las impresiones son ruido de plantilla; el hub cubre |
| Tubul/Llico/Punta Lavapié, Coronel/Lota, Santa Juana | — | 5 | No: sin señal GSC y sin pacientes suficientes por localidad |
| Contulmo, Tirúa | — | 4 | No: 7 y 5 pacientes, 2-2,5 h de viaje |
| Traumatología, dermatología, urología | — | — | No: el centro no las ofrece. Oportunidad de negocio: ~700 apariciones por 5 meses y Barraza tuvo 826 citas |

Qué es pilar: el hub `/comuna/{slug}` (ya existía). Qué es combo: `/blog/{esp}-{slug}`. NO se hace: páginas por barrio o caleta, páginas por profesional (fuera de alcance), landings con "en {comuna}" que sugieran sede.

## Cambios

Páginas con contenido propio (28 reescritas + 1 nueva): ver `app/seo_provincia_c1.py`, `_c2.py`, `_c3.py`. Plantilla `templates/seo_localidad.html`. Se sirven desde `blog_post` antes del localizador genérico. Fallback: si falla, se usa la plantilla anterior.

Hubs `/comuna/{slug}`: título y H1 "para pacientes de X" (nunca "en X" fuera de Carampangue), sección que enlaza a las páginas propias de esa localidad, distancias corregidas.

Correcciones de datos falsos encontradas:
- Distancias: Lebu 50 km/60 min (real 77/80), Los Álamos 35/40 (53/55), Tirúa 110/120 (138/150), Arauco 15/20 (8/11), Laraquete 8 (12), Curanilahue 25 (30), Cañete 45 en landing (72). Rutas: Arauco-Carampangue es P-20, no P-22.
- Se quitaron nombres de líneas de bus (Estuario Reloncaví, Lit Sur) y "frente a Banco Estado": no verificables.
- Ecografía: el sitio decía $35.000 y "obstétrica disponible"; el bot dice $40.000 general, ginecológica $35.000, obstétrica NO disponible. Corregido en portada (FAQ y catálogo), `ecografia-precio-arauco`, tarjetas de hub. DECIDIR: confirmar con el dueño que $40.000 es el valor vigente.
- Landings `/otorrino-curanilahue` ($25-30k, es $35.000), `/ginecologo-curanilahue`, `/dentista-curanilahue` (brackets "$1.200.000", endodoncia $120-200k): corregidos al código.
- Landings `/canete`, `/lebu`, `/los-alamos`, `/curanilahue`: se quitó Traumatología y Pediatría (no se ofrecen) y "Bono Fonasa en ginecología/traumatología".
- Voseo ("tenés", "agendá", "pagás") en 3 artículos: corregido.
- "Bono Fonasa en sucursal": cambiado a "en el centro" (no sugerir sucursales).

Portada (marca): título "Centro Médico Carampangue · Médico y Dentista en Carampangue y Arauco", meta con dirección, FAQ nueva "¿Qué es Carampangue y a qué distancia queda?" y "¿Dónde queda el Centro Médico Carampangue?", alternateName real, enlaces a hubs de Laraquete y Ramadilla. Sin repetir palabras clave. La portada vive en el Snippet 5: SIN SYNC a WordPress todavía.

Mejoras a artículos existentes (título/meta/H1): vacunas PNI 2026 (27.000 apariciones, CTR 0,2%), ecografía precio Arauco, kinesiología, limpieza dental, embarazo. Pendiente en WordPress (no está en el repo): `/kinesiologia-2/` (465 apariciones, pos 3,0, 1 clic) y `/medicina-general-2/`: revisar título y meta ahí.

## Pendiente humano
1. Sincronizar al Snippet 5 la portada y revisar que /blog, /comuna y las rutas nuevas pasen por el puente de WordPress (`/blog/pediatra-arauco` usa el mismo puente que el resto de /blog).
2. Confirmar precio de ecografía ($40.000 vs $35.000).
3. `/traumatologo-curanilahue` sigue público ofreciendo un servicio deshabilitado: decidir noindex o retiro.
4. Dirección: el bot dice "Monsalve 102 esquina República"; el sitio, "República 102". Las páginas nuevas usan "República 102, esquina Monsalve". Unificar.
5. Rich Results Test (manual) sobre 3 páginas y la portada después del deploy.

## Qué medir en 4-8 semanas (panel Campañas Meta, canal Página web)
- Por página nueva: apariciones, posición y CTR en GSC; objetivo CTR > 3% en las de pos <6 (ORL Curanilahue, eco Arauco).
- Clics y escrituras por WhatsApp con marcador `(web: blog · {slug} · {botón})`, citas agendadas por página, y su tasa de conversión de escritura a cita.
- Cañete y Lebu: si suben apariciones pero no escrituras, el cuello es el viaje: probar enfatizar teleconsulta (psiquiatría, neurología) y agrupar atenciones.
- "carampangue" (6.000 apariciones): posición de la portada y clics tras la FAQ; no se espera conversión alta.
- Regresión: `centro medico carampangue` debe mantenerse en pos <3.
- `/blog/pediatra-arauco`: consultas "pediatra arauco" y escrituras (riesgo: frustración si buscan pediatra; ver rebote).
