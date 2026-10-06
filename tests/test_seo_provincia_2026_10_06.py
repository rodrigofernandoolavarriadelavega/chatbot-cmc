"""SEO de la Provincia de Arauco (docs/SEO_PROVINCIA_2026-10.md), 2026-10-06.

Sin red, sin Medilink, sin Claude: TestClient sobre la app real con una DB temporal.
Cubre las páginas localidad x especialidad con contenido propio, la portada, los hubs
de comuna, el sitemap y las reglas duras del sitio (JSON-LD sin comentarios HTML,
una sola sede, nada de "certificados/habilitados", precios solo del código del bot).

    PYTHONPATH=app venv/bin/python -m pytest tests/test_seo_provincia_2026_10_06.py -q
"""
import itertools
import json
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
os.environ.setdefault("SESSIONS_DB", str(Path(tempfile.mkdtemp(prefix="cmc_seo_")) / "t.db"))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
import seo_provincia as sp  # noqa: E402
from seo_provincia_paginas import PAGINAS, URLS_NUEVAS  # noqa: E402

BASE = "https://centromedicocarampangue.cl"
# El contenido de otorrino se valida como si estuviera activo (para cuando vuelva);
# la redirección temporal mientras está apagado se prueba aparte.
main.OTORRINO_ACTIVO = True
client = TestClient(main.app)


def test_otorrino_apagado_redirige_temporal_a_fono():
    main.OTORRINO_ACTIVO = False
    try:
        for url, destino in (("/blog/otorrinolaringologia-curanilahue", "/blog/fonoaudiologia-curanilahue"),
                             ("/blog/otorrinolaringologia", "/blog/fonoaudiologia"),
                             ("/otorrino-curanilahue", "/blog/fonoaudiologia-curanilahue")):
            r = client.get(url, follow_redirects=False)
            assert r.status_code == 302 and r.headers["location"] == destino, (url, r.status_code)
    finally:
        main.OTORRINO_ACTIVO = True

# ── helpers ──────────────────────────────────────────────────────────────────
_JSONLD = re.compile(r'<script type="application/ld\+json">\s*(.*?)\s*</script>', re.S)


def _get(url: str) -> str:
    r = client.get(url)
    assert r.status_code == 200, f"{url} -> {r.status_code}"
    return r.text


def _jsonld(html: str) -> list:
    out = []
    for b in _JSONLD.findall(html):
        assert "<!--" not in b, "comentario HTML dentro de JSON-LD"
        out.append(json.loads(b))
    return out


def _article(html: str) -> str:
    m = re.search(r'<article class="blog-content">(.*?)</article>', html, re.S)
    return m.group(1) if m else ""


def _texto(html: str) -> str:
    t = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S)
    t = re.sub(r"<[^>]+>", " ", t)
    import html as _h
    return re.sub(r"\s+", " ", _h.unescape(t))


SLUGS = sorted(PAGINAS)
COMUNAS = [v["nombre"] for v in sp.LOCALIDADES.values()]

# Voseo (formas verbales rioplatenses) y chilenismos que no van en el sitio formal
_VOSEO = re.compile(
    r"\b(vos|tenés|querés|podés|sabés|venís|sos|decís|hacés|agendá|escribí|avisá|llamá|mirá|"
    r"confirmá|indicá|consultá|pedí|decile|indicale|fijate|andá|vení|esperá|contanos)\b", re.I)
_CHILENISMOS = re.compile(
    r"\b(al toque|altiro|al tiro|cachái|po\b|weon|huevón|bacán|fome|la raja|caleta de|pololo|polola|"
    r"luca|lucas|harto|guagua|cuático|ya po|sipo|nopo)\b", re.I)
_PROHIBIDAS = re.compile(r"certificad|habilitad|superintendencia|acreditad", re.I)


# ── 1. cada página nueva ─────────────────────────────────────────────────────
def test_hay_paginas_y_numero_razonable():
    assert 15 <= len(SLUGS) <= 40
    assert set(URLS_NUEVAS) <= set(SLUGS)


@pytest.mark.parametrize("slug", SLUGS)
def test_pagina_200_canonical_y_wa(slug):
    html = _get(f"/blog/{slug}")
    assert f'<link rel="canonical" href="{BASE}/blog/{slug}" />' in html
    assert "/static/cmc-wa.js" in html
    assert 'name="robots" content="index' in html
    assert "<h1" in html and "{{" not in html
    # el enlace de WhatsApp lleva el número del bot, la declaración (web: ...) y UTM
    wa = re.findall(r'href="(https://wa\.me/56966610737[^"]*)"', html)
    assert wa, "sin enlace de WhatsApp"
    assert all("utm_source=blog" in u and "utm_campaign=" in u for u in wa)
    assert all("%28web%3A%20blog%29" in u for u in wa)


@pytest.mark.parametrize("slug", SLUGS)
def test_jsonld_valido_y_direccion_unica(slug):
    html = _get(f"/blog/{slug}")
    bloques = _jsonld(html)
    tipos = [b.get("@type") for b in bloques]
    assert {"MedicalClinic", "MedicalWebPage", "BreadcrumbList", "FAQPage"} <= set(tipos)
    clinica = next(b for b in bloques if b["@type"] == "MedicalClinic")
    assert clinica["address"]["streetAddress"] == "Monsalve 102"
    assert clinica["address"]["addressLocality"] == "Carampangue"
    loc = sp.LOCALIDADES[PAGINAS[slug]["comuna"]]["nombre"]
    assert any(a["name"] == loc for a in clinica["areaServed"])
    faq = next(b for b in bloques if b["@type"] == "FAQPage")
    assert len(faq["mainEntity"]) >= 3
    # el schema declara la misma URL que el canonical
    pagina = next(b for b in bloques if b["@type"] == "MedicalWebPage")
    assert pagina["url"] == f"{BASE}/blog/{slug}"


@pytest.mark.parametrize("slug", SLUGS)
def test_tono_y_reglas_duras(slug):
    texto = _texto(_get(f"/blog/{slug}"))
    assert not _VOSEO.search(texto), _VOSEO.search(texto)
    assert not _CHILENISMOS.search(texto), _CHILENISMOS.search(texto)
    assert not _PROHIBIDAS.search(texto), _PROHIBIDAS.search(texto)
    assert not re.search(r"[\U0001F300-\U0001FAFF☀-➿]", texto), "emoji"


@pytest.mark.parametrize("slug", SLUGS)
def test_no_sugiere_sede_fuera_de_carampangue(slug):
    texto = _texto(_article(_get(f"/blog/{slug}")))
    # direcciones: solo Monsalve 102, esquina República / Monsalve (la esquina)
    calles = set(re.findall(r"\b(?:República|Monsalve|Conquista|Prat|Freire|O'Higgins|Arturo Prat)\s+\d+", texto))
    assert calles <= {"Monsalve 102"}, calles
    # toda frase que junte "sede/sucursal/consultorio" con otra localidad debe ser negación
    otras = [n for n in COMUNAS if n != "Carampangue"]
    for frase in re.split(r"(?<=[.!?])\s+", texto):
        if frase.strip().endswith("?"):
            continue  # la pregunta de la FAQ; su respuesta ("No...") sí se valida
        if re.search(r"\b(sede|sucursal(?:es)?|consultorio)\b", frase, re.I) and any(n in frase for n in otras):
            assert re.search(r"\b(no|ni|única|unica|solo)\b", frase, re.I), frase


@pytest.mark.parametrize("slug", SLUGS)
def test_precios_solo_del_codigo_del_bot(slug):
    """Todo monto en $ de la página tiene que existir en el código del bot."""
    import claude_helper
    import flows
    permitidos = set(re.findall(r"\$\s?(\d{1,3}(?:\.\d{3})+|\d{3,})", claude_helper.SYSTEM_PROMPT))
    permitidos = {p.replace(".", "") for p in permitidos}
    for v in flows.PRECIOS_SLOT.values():
        permitidos |= {str(x) for x in v if isinstance(x, int)}
    for pair in flows.PRECIO_PROF_SIN_BONO.values():
        permitidos |= {str(x) for x in pair}
    montos = {m.replace(".", "") for m in re.findall(r"\$\s?(\d{1,3}(?:\.\d{3})+)", _texto(_article(_get(f"/blog/{slug}"))))}
    assert montos <= permitidos, f"{slug}: montos que no están en el código: {sorted(montos - permitidos)}"


def test_enlaces_internos_responden_200():
    vistos = set()
    for slug in SLUGS:
        for href in re.findall(r'href="(/(?:blog|comuna)[^"#?]*)"', _get(f"/blog/{slug}")):
            if href in vistos:
                continue
            vistos.add(href)
            assert client.get(href).status_code == 200, f"{slug} enlaza a {href} (no responde 200)"


def test_cada_pagina_enlaza_pilar_y_especialidad():
    for slug, p in PAGINAS.items():
        html = _get(f"/blog/{slug}")
        assert f'href="{"/comuna/" + p["comuna"]}"' in html, f"{slug}: sin enlace al pilar de la comuna"
        assert len([e for e in p["enlaces"] if e[0].startswith("/blog/")]) >= 2, f"{slug}: pocos enlaces a blogs"


def test_contenido_no_duplicado_entre_paginas():
    """Doorway pages: el cuerpo de cualquier par de páginas debe diferir de verdad."""
    def sh(t, n=6):
        w = re.findall(r"\w+", t.lower())
        return {tuple(w[i:i + n]) for i in range(max(1, len(w) - n + 1))}

    cuerpos = {s: sh(_texto(_article(_get(f"/blog/{s}")))) for s in SLUGS}
    peor = (0.0, None)
    for a, b in itertools.combinations(SLUGS, 2):
        inter = len(cuerpos[a] & cuerpos[b])
        j = inter / max(1, len(cuerpos[a] | cuerpos[b]))
        if j > peor[0]:
            peor = (j, (a, b))
    assert peor[0] < 0.30, f"par demasiado parecido: {peor}"


def test_titulos_y_h1_unicos_con_la_consulta():
    titulos = [PAGINAS[s]["title"] for s in SLUGS]
    assert len(set(titulos)) == len(titulos)
    for s in SLUGS:
        loc = sp.LOCALIDADES[PAGINAS[s]["comuna"]]["nombre"]
        h1 = re.sub(r"<[^>]+>", "", PAGINAS[s]["h1"])
        assert loc in h1 or "Arauco" in h1, (s, h1)
        assert len(PAGINAS[s]["title"]) <= 78, (s, len(PAGINAS[s]["title"]))
        assert len(PAGINAS[s]["meta"]) <= 175, (s, len(PAGINAS[s]["meta"]))


# ── 2. sitemap ───────────────────────────────────────────────────────────────
def test_todas_las_urls_estan_en_el_sitemap():
    sm = _get("/sitemap.xml")
    for s in SLUGS:
        assert f"<loc>{BASE}/blog/{s}</loc>" in sm, f"{s} no está en sitemap.xml"
    assert sm.count("<loc>") == len(set(re.findall(r"<loc>([^<]+)</loc>", sm))), "URLs duplicadas en el sitemap"


def test_sitemap_blogs_estatico_incluye_la_url_nueva():
    x = (ROOT / "static" / "sitemap_blogs.xml").read_text(encoding="utf-8")
    for s in URLS_NUEVAS:
        assert f"/blog/{s}</loc>" in x


# ── 3. distancias y rutas ────────────────────────────────────────────────────
def test_distancias_consistentes_con_ruteo_vial():
    for slug, d in sp.LOCALIDADES.items():
        c = main.COMUNAS_ARAUCO[slug]
        assert (c["km"], c["min"]) == (d["km"], d["min"]), slug
    # los valores anteriores subestimaban estas tres
    assert main.COMUNAS_ARAUCO["lebu"]["km"] >= 75
    assert main.COMUNAS_ARAUCO["los-alamos"]["km"] >= 50
    assert main.COMUNAS_ARAUCO["arauco"]["km"] == 8


def test_texto_local_sin_lineas_de_bus_inventadas():
    for slug, d in main.COMUNA_LOCAL_DATA.items():
        t = " ".join(d.values())
        assert not re.search(r"Estuario|Lit Sur|Banco Estado|buses", t, re.I), slug


# ── 4. hubs de comuna ────────────────────────────────────────────────────────
@pytest.mark.parametrize("slug", list(sp.LOCALIDADES))
def test_hub_comuna(slug):
    html = _get(f"/comuna/{slug}")
    assert f'rel="canonical" href="{BASE}/comuna/{slug}"' in html
    _jsonld(html)
    assert "/static/cmc-wa.js" in html
    nombre = sp.LOCALIDADES[slug]["nombre"]
    if slug != "carampangue":
        assert f"Médico y Dentista en {nombre}" not in html, "el H1/título sugiere sede en la localidad"
        assert f"pacientes de" in html
    # el hub enlaza a las páginas con contenido propio de su comuna
    for s, p in PAGINAS.items():
        if p["comuna"] == slug:
            assert f'href="/blog/{s}"' in html, f"hub {slug} no enlaza a {s}"
    assert not _PROHIBIDAS.search(_texto(html))


def test_jsonld_valido_en_todo_lo_que_se_toco():
    urls = ["/canete", "/lebu", "/los-alamos", "/curanilahue", "/otorrino-curanilahue",
            "/dentista-curanilahue", "/ginecologo-curanilahue", "/comuna/", "/blog/ecografia-precio-arauco",
            "/blog/bono-fonasa-mle-arauco", "/blog/limpieza-dental-precio-arauco", "/blog/kinesiologia",
            "/blog/vacunas-pni-calendario-2026", "/blog/embarazo-controles-mensuales"]
    for u in urls:
        _jsonld(_get(u))


def test_landings_directas_con_precio_y_distancia_correctos():
    for slug in ("canete", "lebu", "los-alamos", "curanilahue"):
        html = _get(f"/{slug}")
        assert "Pediatría" not in html and "traumatología" not in html.lower()
        assert not re.search(r"\bbuses?\b", re.sub(r"<[^>]+>", " ", html), re.I), f"/{slug} nombra buses sin certeza"


# ── 5. portada (marca) ───────────────────────────────────────────────────────
def test_portada_marca_alineada():
    html = (ROOT / "templates" / "sitio.html").read_text(encoding="utf-8")
    assert "<title>Centro Médico Carampangue · Médico y Dentista en Carampangue y Arauco</title>" in html
    bloques = _jsonld(html)
    clinica = next(b for b in bloques if b.get("@type") == "MedicalClinic")
    assert clinica["address"]["streetAddress"] == "Monsalve 102"
    assert "Centro Médico Carampangue" == clinica["name"]
    faq = next(b for b in bloques if b.get("@type") == "FAQPage")
    preguntas = [q["name"] for q in faq["mainEntity"]]
    assert any("Dónde queda el Centro Médico Carampangue" in q for q in preguntas)
    assert any("Qué es Carampangue" in q for q in preguntas)
    eco = next(q for q in faq["mainEntity"] if q["name"] == "¿Atienden ecografías?")["acceptedAnswer"]["text"]
    assert "David Pardo: $40.000" in eco and "no se realiza en el centro" in eco
    assert "obstétrica también disponible" not in eco.lower()
    assert not _PROHIBIDAS.search(_texto(html)) or "Superintendencia" not in html


# ── 6. artículos existentes mejorados ────────────────────────────────────────
def test_articulos_mejorados():
    eco = _get("/blog/ecografia-precio-arauco")
    assert "$35.000</td><td>David Pardo" not in eco and "Ecografía obstétrica</strong></td>" not in eco
    assert "$40.000" in eco
    assert not _VOSEO.search(_texto(eco))
    for s in ("bono-fonasa-mle-arauco", "limpieza-dental-precio-arauco"):
        assert not _VOSEO.search(_texto(_get(f"/blog/{s}"))), s
    assert "<title>Calendario de vacunación 2026 en Chile (PNI)" in _get("/blog/vacunas-pni-calendario-2026")
    assert "Kinesiología con bono Fonasa en Arauco" in _get("/blog/kinesiologia")


def test_combos_sin_pagina_propia_siguen_funcionando():
    html = _get("/blog/medicina-general-tirua")
    assert f'rel="canonical" href="{BASE}/blog/medicina-general-tirua"' in html
    assert "/static/cmc-wa.js" in html
