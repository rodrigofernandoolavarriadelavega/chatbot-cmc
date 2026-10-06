"""SEO de la Provincia de Arauco: páginas localidad × especialidad con contenido propio.

Contexto (docs/SEO_PROVINCIA_2026-10.md): el sitio ya tenía 22 especialidades x 10
localidades = 220 URLs /blog/{especialidad}-{localidad} generadas por plantilla
(mismo cuerpo con 3 párrafos de contexto). Search Console mostró que varias rankean
(pos 4-9) pero con CTR bajo, porque ni el título ni el cuerpo responden a lo que la
gente escribe ("otorrinolaringólogo en Curanilahue", "dentista Cañete"). Este módulo
reemplaza, SOLO para las celdas con evidencia, el cuerpo genérico por una página
escrita para esa localidad, sobre la MISMA URL (no hay URLs paralelas ni canibalización).

Reglas duras que este módulo hace cumplir:
  - Una sola sede: Monsalve 102, esquina República, Carampangue. Nunca se sugiere sede en otra comuna.
  - Precios y profesionales salen de OFERTA, que replica el código del bot
    (flows.PRECIOS_SLOT y el bloque PRECIOS de claude_helper.SYSTEM_PROMPT).
  - Distancias: ruteo vial (OSRM sobre OpenStreetMap, consultado el 2026-10-06),
    redondeadas. No se nombran líneas ni frecuencias de buses: solo la ruta.
  - JSON-LD siempre se arma con json.dumps (jamás comentarios HTML dentro).
  - Español formal de Chile (usted), sin voseo ni chilenismos.
"""
from __future__ import annotations

import html as _html
import json
import re
from pathlib import Path
from urllib.parse import quote

BASE = "https://centromedicocarampangue.cl"
WA = "56966610737"
TPL_PATH = Path(__file__).resolve().parent.parent / "templates" / "seo_localidad.html"

DIRECCION = {
    "@type": "PostalAddress",
    "streetAddress": "Monsalve 102",
    "addressLocality": "Carampangue",
    "addressRegion": "Biobío",
    "postalCode": "4250000",
    "addressCountry": "CL",
}
GEO = {"@type": "GeoCoordinates", "latitude": -37.254995, "longitude": -73.236079}

# ── Localidades ──────────────────────────────────────────────────────────────
# km y min: ruteo vial hasta Carampangue (OSRM/OpenStreetMap, 2026-10-06), redondeados.
LOCALIDADES = {
    "carampangue": {"nombre": "Carampangue", "km": 0, "min": 0, "ruta": ""},
    "arauco": {"nombre": "Arauco", "km": 8, "min": 11, "ruta": "el camino que une Arauco con Carampangue (Ruta P-20)"},
    "laraquete": {"nombre": "Laraquete", "km": 12, "min": 12, "ruta": "la Ruta 160"},
    "ramadilla": {"nombre": "Ramadilla", "km": 7, "min": 7, "ruta": "la Ruta 160"},
    "curanilahue": {"nombre": "Curanilahue", "km": 30, "min": 30, "ruta": "la Ruta 160"},
    "los-alamos": {"nombre": "Los Álamos", "km": 53, "min": 55, "ruta": "la Ruta 160"},
    "canete": {"nombre": "Cañete", "km": 72, "min": 80, "ruta": "la Ruta P-60 y luego la Ruta 160"},
    "lebu": {"nombre": "Lebu", "km": 77, "min": 80, "ruta": "la Ruta 160"},
    "contulmo": {"nombre": "Contulmo", "km": 106, "min": 120, "ruta": "la Ruta P-60 y luego la Ruta 160"},
    "tirua": {"nombre": "Tirúa", "km": 138, "min": 150, "ruta": "las rutas P-72, P-60 y 160"},
}

# ── Oferta real (precios y profesionales: código del bot) ───────────────────
# clave -> (prestación, valor, quién y cuándo)
OFERTA = {
    "orl_consulta": ("Consulta de otorrinolaringología", "$35.000", "Dr. Manuel Borrego. Lunes a miércoles, 16:00 a 20:00. Solo particular"),
    "orl_control": ("Control de otorrinolaringología", "$8.000", "Seguimiento después de la consulta"),
    "orl_lavado": ("Lavado de oídos", "$10.000", "Se realiza en la consulta; valor adicional"),
    "audiometria": ("Audiometría", "$25.000", "Fonoaudiología, Juana Arratia. Examen de unos 20 minutos"),
    "impedanciometria": ("Impedanciometría", "$20.000", "Fonoaudiología. Mide la movilidad del tímpano"),
    "eco_general": ("Ecotomografía abdominal, renal, tiroidea, partes blandas, musculoesquelética, pelviana, testicular o mamaria", "$40.000", "David Pardo. Fechas según la agenda del especialista; se coordinan por WhatsApp. Solo particular"),
    "eco_doppler": ("Ecotomografía doppler (arterias y venas)", "$90.000", "David Pardo. Solo particular"),
    "eco_gineco": ("Ecografía ginecológica (transvaginal)", "$35.000", "Dr. Tirso Rejón, en ginecología. Solo particular"),
    "gastro_consulta": ("Consulta de gastroenterología", "$35.000", "Dr. Nicolás Quijano. Fechas según agenda; se coordinan por WhatsApp. Solo particular"),
    "gastro_revision": ("Revisión de exámenes (endoscopías, ecografías)", "$17.500", "Dr. Nicolás Quijano"),
    "cardio_consulta": ("Consulta de cardiología", "$40.000", "Dr. Miguel Millán. Fechas según agenda; se coordinan por WhatsApp. Solo particular"),
    "cardio_ecg": ("Electrocardiograma informado por cardiólogo", "$20.000", "Dr. Miguel Millán. Dura unos 10 minutos"),
    "cardio_eco": ("Ecocardiograma", "$110.000", "Dr. Miguel Millán. Se realiza una vez al mes; el paciente queda en lista de espera"),
    "mg_fonasa": ("Consulta de medicina general con bono Fonasa", "$7.880", "Copago Fonasa MLE nivel 3. El bono se emite en el centro con huella"),
    "mg_part": ("Consulta de medicina general particular", "$25.000", "Dr. Rodrigo Olavarría o Dr. Andrés Abarca. Con el Dr. Alonso Márquez (medicina familiar): $30.000"),
    "mg_control": ("Revisión de exámenes", "$0", "Sin costo para el paciente"),
    "psiq": ("Consulta de psiquiatría por teleconsulta", "$60.000", "Dra. Cecilia Unibazo. Martes y jueves por la tarde. Se reserva abonando el valor por adelantado. Solo particular"),
    "neuro": ("Consulta de neurología por telemedicina", "$65.000", "Dra. Franca González. Desde los 15 años. Solo particular"),
    "oft": ("Examen de la vista (optometría, fondo de ojo preventivo y presión intraocular)", "$15.000", "TM Ana Celedón, tecnóloga médica. Mismo valor para todos los pacientes; sin bono Fonasa"),
    "odo_eval": ("Evaluación dental", "$15.000", "Dra. Javiera Burgos o Dr. Carlos Jiménez. Incluye diagnóstico y plan de tratamiento"),
    "odo_limpieza": ("Limpieza dental (destartraje y profilaxis)", "$30.000", "Odontología general"),
    "odo_resina": ("Restauración con resina (tapadura)", "desde $35.000", "Odontología general"),
    "odo_exo": ("Extracción simple", "$40.000", "Odontología general"),
    "orto_brackets": ("Instalación de brackets, boca completa", "$120.000", "Dra. Daniela Castillo. Se parte con la evaluación dental"),
    "orto_control": ("Control de ortodoncia", "$30.000", "Ajuste periódico de arcos y elásticos"),
    "kine_bono": ("Sesión de kinesiología con bono Fonasa", "$7.830", "Luis Armijo o Leonardo Etcheverry. Primera y última sesión con bono: $10.360"),
    "kine_pack": ("Pack de 10 sesiones con bono Fonasa", "$83.360", "Habitualmente indicado por médico o traumatólogo"),
    "kine_part": ("Sesión de kinesiología particular", "$20.000", "Sin bono Fonasa"),
    "psico_bono": ("Sesión de psicología con bono Fonasa (45 minutos)", "$14.420", "Jorge Montalba o Juan Pablo Rodríguez"),
    "psico_part": ("Sesión de psicología particular (45 minutos)", "$25.000", "Con la Ps. Jacquelinne Salas, los pacientes Fonasa pagan $20.000 directo, sin bono"),
    "gineco": ("Consulta de ginecología", "$30.000", "Dr. Tirso Rejón. Solo particular"),
    "mf_ninos": ("Medicina familiar (atiende niños)", "$7.880 con bono Fonasa · $30.000 particular", "Dr. Alonso Márquez"),
}


def tabla_oferta(claves: list[str]) -> str:
    filas = []
    for k in claves:
        pres, valor, quien = OFERTA[k]
        filas.append(
            f"<tr><td>{_html.escape(pres)}</td><td><strong>{_html.escape(valor)}</strong></td>"
            f"<td>{_html.escape(quien)}</td></tr>"
        )
    return (
        '<table class="price-table"><thead><tr><th>Atención</th><th>Valor</th><th>Quién y cuándo</th></tr></thead>'
        "<tbody>" + "".join(filas) + "</tbody></table>"
    )


def bloque_viaje(slug_loc: str) -> str:
    """Tres cifras de viaje a Carampangue (ruteo vial redondeado). Sin buses."""
    d = LOCALIDADES[slug_loc]
    return (
        '<div class="trip-grid">'
        f'<div><b>{d["km"]} km</b><span>por carretera hasta Carampangue</span></div>'
        f'<div><b>{_dur(d["min"])}</b><span>en auto, sin contar paradas</span></div>'
        "<div><b>Monsalve 102, esquina República</b><span>esquina Monsalve, Carampangue</span></div>"
        "</div>"
    )


def _dur(minutos: int) -> str:
    if minutos < 60:
        return f"{minutos} min"
    h, m = divmod(minutos, 60)
    return f"{h} h" if m == 0 else f"{h} h {m:02d} min"


# ── Utilidades de armado ─────────────────────────────────────────────────────

def wa_url(texto: str, slug: str) -> str:
    """Link de WhatsApp con UTM. static/cmc-wa.js agrega el marcador
    "(web: página · artículo · botón)" al cargar; acá se declara la página."""
    return (
        f"https://wa.me/{WA}?text={quote(texto + ' (web: blog)')}"
        f"&utm_source=blog&utm_medium=organic&utm_campaign={quote(slug)}"
    )


def _json_script(obj: dict) -> str:
    return '<script type="application/ld+json">\n' + json.dumps(obj, ensure_ascii=False, indent=2) + "\n</script>"


def _faq_html(faqs: list[tuple[str, str]]) -> str:
    out = ['<h2>Preguntas <em>frecuentes</em></h2>', '<div class="faq-list">']
    chev = ('<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" '
            'stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg>')
    for q, a in faqs:
        out.append(f'<details><summary>{_html.escape(q)} {chev}</summary><div class="answer">{a}</div></details>')
    out.append("</div>")
    return "\n".join(out)


def _plain(s: str) -> str:
    return _html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def jsonld_pagina(p: dict, slug: str) -> str:
    loc = LOCALIDADES[p["comuna"]]
    url = f"{BASE}/blog/{slug}"
    clinica = {
        "@context": "https://schema.org",
        "@type": "MedicalClinic",
        "@id": f"{BASE}/#clinic",
        "name": "Centro Médico Carampangue",
        "url": BASE + "/",
        "telephone": "+56442965226",
        "address": DIRECCION,
        "geo": GEO,
        "areaServed": [{"@type": "City", "name": loc["nombre"]}] if loc["nombre"] != "Carampangue"
        else [{"@type": "City", "name": "Carampangue"}],
        "medicalSpecialty": p["schema_specialty"],
        "openingHoursSpecification": [
            {"@type": "OpeningHoursSpecification", "dayOfWeek": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"], "opens": "08:00", "closes": "21:00"},
            {"@type": "OpeningHoursSpecification", "dayOfWeek": ["Saturday"], "opens": "09:00", "closes": "14:00"},
        ],
    }
    pagina = {
        "@context": "https://schema.org",
        "@type": "MedicalWebPage",
        "name": p["title"],
        "headline": p["h1"],
        "description": p["meta"],
        "url": url,
        "inLanguage": "es-CL",
        "about": {"@type": "MedicalSpecialty", "name": p["schema_specialty"]},
        "dateModified": "2026-10-06",
        "publisher": {"@id": f"{BASE}/#clinic"},
        "mainEntityOfPage": {"@type": "WebPage", "@id": url},
    }
    migas = {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "Inicio", "item": BASE + "/"},
            {"@type": "ListItem", "position": 2, "name": p["crumb_mid"], "item": BASE + p["crumb_mid_url"]},
            {"@type": "ListItem", "position": 3, "name": p["crumb_last"], "item": url},
        ],
    }
    faq = {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": [
            {"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": _plain(a)}}
            for q, a in p["faqs"]
        ],
    }
    return "\n".join(_json_script(o) for o in (clinica, pagina, migas, faq))


def render_pagina(slug: str) -> str | None:
    from seo_provincia_paginas import PAGINAS  # import perezoso: archivo grande de contenido
    p = PAGINAS.get(slug)
    if not p or not TPL_PATH.exists():
        return None
    tpl = TPL_PATH.read_text(encoding="utf-8")
    loc = LOCALIDADES[p["comuna"]]
    wa = wa_url(p["wa_texto"], slug)
    body = []
    for h2, cuerpo in p["secciones"]:
        body.append(f"<h2>{h2}</h2>\n{cuerpo}")
    body.append(_faq_html(p["faqs"]))
    links = "\n".join(
        f'            <li><a href="{_html.escape(h)}">{_html.escape(t)}</a></li>' for h, t, _d in p["enlaces"]
    )
    mini = "".join(
        f'<li><a href="{_html.escape(h)}">{_html.escape(t)}</a><span>{_html.escape(d)}</span></li>'
        for h, t, d in p["enlaces"]
    )
    body.append('<h2>Más información <em>útil</em></h2>\n<ul class="mini-links">' + mini + "</ul>")
    reemp = {
        "{{TITLE}}": _html.escape(p["title"]),
        "{{META}}": _html.escape(p["meta"]),
        "{{CANONICAL}}": f"{BASE}/blog/{slug}",
        "{{JSONLD}}": jsonld_pagina(p, slug),
        "{{CRUMB_MID_URL}}": p["crumb_mid_url"],
        "{{CRUMB_MID}}": _html.escape(p["crumb_mid"]),
        "{{CRUMB_LAST}}": _html.escape(p["crumb_last"]),
        "{{EYEBROW}}": _html.escape(p["eyebrow"]),
        "{{H1}}": p["h1"],
        "{{LEAD}}": p["lead"],
        "{{BODY}}": "\n".join(body),
        "{{SIDE_CTA}}": _html.escape(p["side_cta"]),
        "{{SIDE_LINKS_TITLE}}": "Para este viaje",
        "{{SIDE_LINKS}}": links,
        "{{CTA_H2}}": p["cta_h2"],
        "{{CTA_P}}": _html.escape(p["cta_p"]),
        "{{WA_URL}}": wa,
    }
    for k, v in reemp.items():
        tpl = tpl.replace(k, v)
    return tpl


# Qué piden más los pacientes de cada localidad (citas de los últimos 12 meses, ficha de
# Medilink con la localidad resuelta por dirección; ver docs/SEO_PROVINCIA_2026-10.md).
# Solo se publica el orden, nunca cifras de pacientes.
PERFIL_HUB = {
    "arauco": "En la comuna de Arauco, las atenciones más usadas son medicina general, kinesiología, odontología y ortodoncia, nutrición y ecografía.",
    "curanilahue": "Desde Curanilahue, las atenciones más usadas son medicina general, los controles de ortodoncia, otorrinolaringología, odontología y ecografía.",
    "los-alamos": "Desde Los Álamos, las atenciones más usadas son los controles de ortodoncia, medicina general, otorrinolaringología, ecografía y odontología.",
    "lebu": "Desde Lebu, la atención más usada es otorrinolaringología, seguida de ecografía y cardiología.",
    "canete": "Desde Cañete, las atenciones más usadas son ortodoncia, otorrinolaringología, psiquiatría por teleconsulta, medicina general, odontología y gastroenterología.",
}


def seccion_hub(slug: str) -> str:
    """Sección 'atenciones con información específica' para el hub /comuna/{slug}:
    enlaza a las páginas localidad x especialidad con contenido propio."""
    from seo_provincia_paginas import PAGINAS
    nombre = LOCALIDADES[slug]["nombre"]
    items = [(s, p) for s, p in PAGINAS.items() if p["comuna"] == slug]
    perfil = PERFIL_HUB.get(slug, "")
    if not items and not perfil:
        return ""
    cards = []
    for s, p in items:
        cards.append(
            f'<a class="spec-card" href="/blog/{s}"><span class="pill">{_html.escape(p["esp"])}</span>'
            f'<h3>{_html.escape(_plain(p["h1"]))}</h3><p>{_html.escape(_plain(p["lead"]))}</p>'
            '<span class="read">Ver detalle</span></a>'
        )
    cuerpo = f'<p class="section-sub">{_html.escape(perfil)}</p>' if perfil else ""
    grid = f'<div class="spec-grid">{"".join(cards)}</div>' if cards else ""
    return (
        '<section class="section"><div class="container">'
        f'<h2 class="section-title">Atenciones con información específica para pacientes de {_html.escape(nombre)}</h2>'
        f"{cuerpo}{grid}</div></section>"
    )


def slugs_servidos() -> list[str]:
    """Slugs servidos por este módulo (para sitemap y tests)."""
    from seo_provincia_paginas import PAGINAS
    return sorted(PAGINAS)


# ── Constructor de páginas (defaults comunes) ────────────────────────────────

def pag(*, comuna: str, base: str, esp: str, spec: str, title: str, meta: str, h1: str,
        lead: str, secciones: list, faqs: list, enlaces: list, wa_texto: str,
        eyebrow: str | None = None, cta_h2: str | None = None, cta_p: str | None = None,
        side_cta: str | None = None) -> dict:
    """Arma el dict de una página. `base` = slug del artículo de la especialidad
    (para la miga de pan); `esp` = nombre legible; `spec` = especialidad schema.org."""
    nombre = LOCALIDADES[comuna]["nombre"]
    return {
        "comuna": comuna, "base": base, "esp": esp, "schema_specialty": spec,
        "title": title, "meta": meta, "h1": h1, "lead": lead,
        "secciones": secciones, "faqs": faqs, "enlaces": enlaces, "wa_texto": wa_texto,
        "crumb_mid": esp, "crumb_mid_url": f"/blog/{base}", "crumb_last": f"{esp} · {nombre}",
        "eyebrow": eyebrow or f"{esp} · Pacientes de {nombre}",
        "cta_h2": cta_h2 or f"Su hora de {esp.lower()}, <em>sin viajar de más</em>",
        "cta_p": cta_p or "Escriba por WhatsApp, indique desde dónde viene y confirmaremos fecha y hora antes de que salga de su casa.",
        "side_cta": side_cta or "Confirmamos fecha y hora por WhatsApp antes de que viaje.",
    }
