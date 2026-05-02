# Backup completo del sitio WordPress — Centro Médico Carampangue

**Fecha de captura:** 2 de mayo de 2026 · 03:50 GMT-4
**Origen:** https://www.centromedicocarampangue.cl
**Método:** `curl` con User-Agent Mozilla/5.0
**Tamaño total:** ~6.3 MB (HTML + imágenes)

## Páginas HTML capturadas (16 + 1 post)

### Páginas principales
| Archivo | URL original |
|---|---|
| `home.html` | https://centromedicocarampangue.cl/ |
| `blog.html` | https://centromedicocarampangue.cl/blog/ |
| `contacto.html` | https://centromedicocarampangue.cl/contacto/ |
| `servicios.html` | https://centromedicocarampangue.cl/servicios/ |
| `profesionales.html` | https://centromedicocarampangue.cl/profesionales/ |
| `instalaciones.html` | https://centromedicocarampangue.cl/instalaciones/ |

### Landings SEO por especialidad
| Archivo | URL original |
|---|---|
| `medicina-general-arauco.html` | https://centromedicocarampangue.cl/medicina-general-arauco/ |
| `dentista-arauco.html` | https://centromedicocarampangue.cl/dentista-arauco/ |
| `kinesiologia-arauco.html` | https://centromedicocarampangue.cl/kinesiologia-arauco/ |
| `nutricionista-arauco.html` | https://centromedicocarampangue.cl/nutricionista-arauco/ |
| `ginecologo-arauco.html` | https://centromedicocarampangue.cl/ginecologo-arauco/ |
| `otorrino-arauco.html` | https://centromedicocarampangue.cl/otorrino-arauco/ |
| `traumatologo-arauco.html` | https://centromedicocarampangue.cl/traumatologo-arauco/ |
| `psicologia.html` | https://centromedicocarampangue.cl/psicologia/ |
| `centro-medico-curanilahue.html` | https://centromedicocarampangue.cl/centro-medico-curanilahue/ |
| `cuando-consultar-medico-general.html` | https://centromedicocarampangue.cl/cuando-consultar-medico-general/ |

### Posts
| Archivo | URL original |
|---|---|
| `post-2020-hello-world.html` | https://centromedicocarampangue.cl/2020/03/13/hello-world/ |

## Sitemaps y robots.txt

- `sitemap_index.xml` — índice general de Yoast
- `page-sitemap.xml` — sitemap de páginas (16 URLs)
- `post-sitemap.xml` — sitemap de posts (1 URL)
- `category-sitemap.xml` — sitemap de categorías
- `robots.txt` — directivas de crawler

## Assets

`/assets/` contiene **49 imágenes** (~3.4 MB) extraídas de los HTML:
fotos del centro, fotos profesionales, logos. Las imágenes están con sus
nombres originales (algunas en versión completa + thumbnail 400x284).

`_image_urls.txt` lista todas las URLs originales para referencia.

## Notas

- **Backup anterior**: `seo-backup-2026-04-20/` (~3.8 MB) contiene una versión
  más antigua del sitio, antes de que se publicaran las landings `*-arauco`.
  Conservar para referencia histórica.
- **El sitio WordPress sigue activo** en producción. Este backup es un snapshot
  estático para tener respaldo de:
  - Copy SEO original (descripciones, títulos, schema.org Yoast)
  - Estructura de landings actual
  - Imágenes profesionales y del centro
- Para restaurar contenido a WP, se puede copiar texto/imágenes desde estos
  HTML al editor de WordPress.

## Cómo ver el backup offline

```bash
cd /Users/rodrigoolavarria/chatbot-cmc/seo-backup-2026-05-02
open home.html  # macOS abre en navegador
```

Las imágenes y CSS apuntan a URLs absolutas del WordPress original, así que
para ver con estilos completos requieres conexión a Internet (mientras WP
siga online).
