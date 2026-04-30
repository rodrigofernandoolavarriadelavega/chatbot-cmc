# Backup SEO — 2026-04-20 (completado 2026-04-24)

Copias HTML de TODAS las páginas de WordPress, listas para usar como respaldo offline antes de borrar el sitio actual y migrar a la landing del chatbot + blogs.

## Actualización 2026-04-24
Se agregaron los respaldos faltantes:
- raiz-home.html — copia exacta de la home pública
- centro-medico-principal.html — la home WP real (id 26285)
- contacto.html — página /contacto/ (indexada en Google)
- profesionales.html — página /profesionales/ (indexada)
- psicologia.html — página /psicologia/ (indexada)

Verificado en WP REST API: solo hay 1 post publicado ("Hello world!" 2020) — ya respaldado.
Los borradores no se pudieron extraer por timeouts 504/307 del servidor — pendiente de reintento.

## Páginas indexadas (NO se tocan destructivamente, solo se edita meta):
- /
- /contacto/
- /profesionales/
- /psicologia/

## Backup incluido (14 URLs):

### Páginas legítimas a conservar (solo agregar noindex si corresponde o fusionar contenido):
- servicios.html → /servicios/ (página real de servicios, evaluar si fusionar con home)
- equipo.html → /equipo/ (revisar vs /profesionales/)
- blog.html → /blog/ (listado del blog, se conserva)
- cuando-consultar-medico-general.html → único post real del blog

### Duplicados Elementor (candidatos a 301 o borrar):
- kinesiologia-2.html
- medicina-general-2.html
- nutricionista-2.html
- home.html (duplicado de /)
- centro-medico-carampangue.html
- centro-medico-carampangue-2.html
- centro-medico-carampangue-3.html
- elementor-26192.html (borrador Elementor)
- elementor-26201.html (borrador Elementor)

### Post fantasma WordPress default:
- 2020_03_13_hello-world.html → borrar

## Notas
- Todas descargadas con HTTP 200.
- User-Agent simuló Chrome desktop.
- urls.txt contiene el listado exacto.
