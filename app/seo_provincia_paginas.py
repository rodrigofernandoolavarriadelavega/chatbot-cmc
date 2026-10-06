"""Registro de páginas localidad x especialidad (contenido en seo_provincia_c1/c2/c3).

Clave = slug de /blog/{slug}. Casi todas reemplazan el cuerpo generado por plantilla
de una URL que ya existía en el sitemap (misma URL, canonical propio); la única URL
nueva es pediatra-arauco.
"""
from seo_provincia_c1 import PAGINAS_1
from seo_provincia_c2 import PAGINAS_2
from seo_provincia_c3 import PAGINAS_3

PAGINAS: dict[str, dict] = {}
PAGINAS.update(PAGINAS_1)
PAGINAS.update(PAGINAS_2)
PAGINAS.update(PAGINAS_3)

# URLs que NO existían antes en el sitemap (el resto ya estaba como /blog/{esp}-{comuna})
URLS_NUEVAS = ("pediatra-arauco",)
