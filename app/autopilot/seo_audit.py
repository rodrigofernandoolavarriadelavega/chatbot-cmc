"""SEO — auditor on-page + técnico + local para el Autopilot del CMC.

Encaja en la filosofía del autopilot (medir → decidir → proponer → re-medir):
audita las URLs canónicas del sitio en vivo, las puntúa contra las mejores
prácticas de SEO médico-local y propone acciones priorizadas. **Read-only**:
solo hace GET a las páginas públicas, nunca escribe en el sitio.

Se renderiza en la pestaña "SEO" del dashboard `/autopilot`.

Arquitectura (espejo de email_segments.py):
  1. OPPORTUNITY_TEMPLATES  — librería best-practice clonable: los targets de
     palabra clave local de mayor valor ("{especialidad} en {comuna}"), con
     title/meta/H1/schema recomendados e intención de búsqueda. Solo lectura.
  2. audit_page(html, url)  — corre los checks on-page/técnico/local/confianza
     sobre el HTML real y devuelve score 0-100 + grade + checks (igual forma
     que email_segments.score_segment).
  3. fetch_and_audit(urls)  — baja las URLs en vivo (httpx, concurrente) y arma
     un snapshot persistido en data/seo_snapshot.json para que el dashboard no
     golpee el sitio en cada vista.
  4. local_seo_checklist()  — checklist NAP / GBP / geo-schema (SEO local es el
     canal #1 de una clínica de barrio).
  5. coverage_matrix()      — grilla especialidad × comuna: qué combinaciones de
     alta intención están cubiertas y cuáles son oportunidad.

Los checks "críticos" salen de bugs reales del CMC (memoria del proyecto):
  - comentarios HTML dentro de JSON-LD → rompió la home (4.8 → 36.6 por 2 días).
  - canonical a agentecmc.cl en vez del dominio canónico → 0 indexación (host-split).
  - fuga del número personal del Dr. (+56987834148) en contenido público.
  - palabras prohibidas ("certificados/habilitados/Superintendencia/acreditados")
    → publicidad engañosa (no todos los profesionales están registrados).

Diseñado defensivo: si una URL no responde, su tarjeta degrada a "no se pudo
auditar" y el resto del snapshot se arma igual. Nunca tumba el dashboard.
"""
from __future__ import annotations

import asyncio
import html as _html
import json
import logging
import os
import re
from datetime import datetime, timezone

import httpx

log = logging.getLogger("bot")

CANONICAL_HOST = "centromedicocarampangue.cl"
CANONICAL_BASE = f"https://{CANONICAL_HOST}"
SNAPSHOT_PATH = os.getenv("SEO_SNAPSHOT_PATH", "data/seo_snapshot.json")
TARGETS_PATH = os.getenv("SEO_TARGETS_PATH", "data/seo_targets.json")

# Memoria: nunca exponer el personal del Dr.; el público es el bot o el fijo.
PERSONAL_PHONE_RE = re.compile(r"\+?56\s*9\s*8783\s*4148|987834148")
# Memoria: publicidad engañosa — no todos los profesionales están registrados.
FORBIDDEN_TRUST_WORDS = ["certificad", "habilitad", "superintendencia", "acreditad"]


# ─────────────────────────────────────────────────────────────────────────────
# Comunas de la Provincia de Arauco con peso de prioridad SEO.
# concentracion (1-5) = DERIVADO de pacientes reales en data/heatmap_cache.db
# (pacientes_heatmap, conteo 2026-06-02): Arauco 2056 · Curanilahue 313 · Los
# Álamos 58 · Lebu/Cañete 19 · resto cola larga. Carampangue se rola a comuna
# "Arauco" en el registro pero es la sede física + keyword de marca → 5.
# Tunear acá cambia el ranking del mapa de oportunidad. Decoplado de
# COMUNAS_ARAUCO (main.py) a propósito para no crear import circular.
# ─────────────────────────────────────────────────────────────────────────────
COMUNAS = [
    # slug          nombre          concentracion(1-5)  · pacientes reales
    ("carampangue", "Carampangue",  5),   # sede física + marca (registro→Arauco)
    ("arauco",      "Arauco",       5),   # 2056
    ("curanilahue", "Curanilahue",  4),   # 313
    ("los-alamos",  "Los Álamos",   3),   # 58
    ("lebu",        "Lebu",         2),   # 19
    ("canete",      "Cañete",       2),   # 19
    ("laraquete",   "Laraquete",    2),   # cercana a la sede (8 km)
    ("ramadilla",   "Ramadilla",    1),   # 2 · rural cercana
    ("tirua",       "Tirúa",        1),   # 2
    ("contulmo",    "Contulmo",     1),   # 0 registrados
]
_COMUNA_NOMBRE = {s: n for s, n, _ in COMUNAS}

# Especialidades con landing/blog y su demanda relativa.
# demanda (1-5) = DERIVADO del volumen real de citas en data/heatmap_cache.db
# (citas_heatmap agregado por profesional → especialidad, 2026-06-02):
#   MG 2985 · Kine 817 · Odonto 507 · Otorrino 379 · Ortodoncia 298 ·
#   Gineco 288 · Trauma 173 · Nutrición 161 · Ecografía 157 · Cardio 74 · Gastro 27.
# Excepciones marcadas: implantología no aparece en el top de citas (oferta
# restringida) pero es ticket alto + demanda no satisfecha (memoria) → 3.
ESPECIALIDADES = [
    # slug                    nombre                  demanda(1-5)  · citas reales
    ("medicina-general",     "Medicina General",     5),   # 2985 (volumen, baja intención SEO)
    ("kinesiologia",         "Kinesiología",         5),   # 817
    ("odontologia-general",  "Odontología",          4),   # 507
    ("otorrinolaringologia", "Otorrinolaringología", 4),   # 379
    ("ortodoncia",           "Ortodoncia",           4),   # 298 · ticket alto
    ("ginecologia",          "Ginecología",          4),   # 288
    ("traumatologia",        "Traumatología",        3),   # 173
    ("nutricion",            "Nutrición",            3),   # 161
    ("ecografia",            "Ecografía",            3),   # 157 (demanda > oferta)
    ("implantologia",        "Implantología",        3),   # <30 · ticket alto + demanda no satisfecha
    ("cardiologia",          "Cardiología",          2),   # 74
    ("gastroenterologia",    "Gastroenterología",    1),   # 27
]


# ─────────────────────────────────────────────────────────────────────────────
# URLs a auditar — el set por defecto cubre las páginas de mayor valor (no las
# 223 del sitemap: sería lento y la mayoría comparte template). El usuario puede
# sumar targets propios vía POST /autopilot/api/seo/targets.
# ─────────────────────────────────────────────────────────────────────────────
def default_targets() -> list[dict]:
    """Páginas prioritarias del sitio canónico, con su rol SEO."""
    t = [
        {"url": f"{CANONICAL_BASE}/",         "role": "home",     "label": "Home"},
        {"url": f"{CANONICAL_BASE}/blog",     "role": "hub",      "label": "Blog (índice)"},
        {"url": f"{CANONICAL_BASE}/comuna/",  "role": "hub",      "label": "Comunas (índice)"},
    ]
    # Hubs por comuna de alta concentración
    for slug, nombre, conc in COMUNAS:
        if conc >= 4:
            t.append({"url": f"{CANONICAL_BASE}/comuna/{slug}", "role": "comuna",
                      "label": f"Comuna · {nombre}", "comuna": slug})
    # Landings directas de especialidad-comuna (alta intención comercial)
    for direct in ("ortodoncia", "curanilahue", "lebu", "los-alamos", "canete", "chequeos"):
        t.append({"url": f"{CANONICAL_BASE}/{direct}", "role": "landing",
                  "label": f"Landing · {direct}"})
    return t


# ─────────────────────────────────────────────────────────────────────────────
# Librería de oportunidades best-practice (clonable, orientada a ranking local).
# Cada plantilla = target de palabra clave con el title/meta/H1/schema YA
# optimizados. El usuario las clona a un target editable. Mismo espíritu que
# SEGMENT_TEMPLATES de email.
# ─────────────────────────────────────────────────────────────────────────────
OPPORTUNITY_TEMPLATES = [
    {
        "id": "opp_esp_comuna",
        "name": "{Especialidad} en {Comuna}",
        "intent": "comercial-local",
        "why": "La búsqueda '[especialidad] en [comuna]' tiene intención de agendar y baja "
               "competencia en la Provincia de Arauco. Es el patrón de mayor conversión para "
               "una clínica de barrio: el paciente ya decidió, solo busca dónde.",
        "recipe": {
            "title": "{Especialidad} en {Comuna} | Centro Médico Carampangue",
            "meta": "{Especialidad} en {Comuna}: agenda por WhatsApp, sin lista de espera. "
                    "Atención cercana en la Provincia de Arauco. Reserva hoy.",
            "h1": "{Especialidad} en {Comuna} y la Provincia de Arauco",
            "schema": "MedicalWebPage + MedicalClinic (con geo + areaServed {Comuna})",
            "url": f"{CANONICAL_BASE}/blog/{{esp_slug}}-{{comuna_slug}}",
        },
    },
    {
        "id": "opp_precio",
        "name": "Precio de {tratamiento} en Arauco",
        "intent": "comercial-precio",
        "why": "Las búsquedas con 'precio' tienen altísima intención y casi nadie las responde "
               "con transparencia. Un artículo con rango de precio + CTA captura tráfico de "
               "fondo de embudo (implante, ortodoncia, ecografía, limpieza).",
        "recipe": {
            "title": "Precio de {tratamiento} en Arauco 2026 | Centro Médico Carampangue",
            "meta": "¿Cuánto cuesta {tratamiento} en la Provincia de Arauco? Rango de precios, "
                    "qué incluye y cómo agendar por WhatsApp.",
            "h1": "Precio de {tratamiento} en Arauco: guía 2026",
            "schema": "Article + FAQPage (preguntas de precio) + Offer con priceRange",
            "url": f"{CANONICAL_BASE}/blog/precio-{{tratamiento_slug}}-arauco",
        },
    },
    {
        "id": "opp_sintoma",
        "name": "Síntoma → cuándo consultar ({condición})",
        "intent": "informacional-salud",
        "why": "Contenido E-E-A-T de salud (síntomas, cuándo consultar) construye autoridad de "
               "dominio y captura tope de embudo. Bien enlazado a la landing de especialidad, "
               "alimenta conversión y mejora el ranking de todo el clúster.",
        "recipe": {
            "title": "{Condición}: síntomas y cuándo consultar | Centro Médico Carampangue",
            "meta": "{Condición}: síntomas, señales de alarma y cuándo ver a un especialista. "
                    "Información médica del Centro Médico Carampangue.",
            "h1": "{Condición}: síntomas y cuándo consultar a un especialista",
            "schema": "MedicalWebPage + MedicalCondition (con código ICD-10)",
            "url": f"{CANONICAL_BASE}/blog/{{condicion_slug}}",
        },
    },
    {
        "id": "opp_gbp",
        "name": "Ficha de Google (Perfil de Empresa)",
        "intent": "local-map-pack",
        "why": "Para una clínica, el Perfil de Empresa de Google (mapa + reseñas) suele traer "
               "más pacientes que el sitio. Categoría correcta, NAP consistente, fotos, horario "
               "y respuesta a reseñas pesan más que cualquier on-page.",
        "recipe": {
            "title": "(no aplica — se gestiona en business.google.com)",
            "meta": "Categoría primaria 'Centro médico'; categorías secundarias por especialidad; "
                    "NAP idéntico al del sitio; publicar posts semanales; pedir y responder reseñas.",
            "h1": "—",
            "schema": "MedicalClinic con sameAs al Perfil de Empresa + aggregateRating real",
            "url": "https://business.google.com",
        },
    },
]


# ─────────────────────────────────────────────────────────────────────────────
# Parser HTML minimalista (sin dependencias: regex + stdlib, como _localize_blog
# en main.py). Suficiente para los elementos SEO-relevantes de páginas propias.
# ─────────────────────────────────────────────────────────────────────────────
def _strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", " ", s)


def _meta(html: str, name: str, attr: str = "name") -> str | None:
    m = re.search(
        rf'<meta\s+[^>]*{attr}\s*=\s*["\']{re.escape(name)}["\'][^>]*>',
        html, re.I)
    if not m:
        return None
    c = re.search(r'content\s*=\s*["\']([^"\']*)["\']', m.group(0), re.I)
    return _html.unescape(c.group(1).strip()) if c else None


def parse_seo(html: str) -> dict:
    """Extrae los elementos SEO-relevantes de una página HTML."""
    head = (re.search(r"<head[^>]*>(.*?)</head>", html, re.I | re.S) or [None, html])
    head = head[1] if isinstance(head, list) else head.group(1)

    title_m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    title = _html.unescape(_strip_tags(title_m.group(1)).strip()) if title_m else None

    h1s = [_html.unescape(_strip_tags(m).strip())
           for m in re.findall(r"<h1[^>]*>(.*?)</h1>", html, re.I | re.S)]
    h2s = re.findall(r"<h2[^>]*>(.*?)</h2>", html, re.I | re.S)

    canonical_m = re.search(
        r'<link\s+[^>]*rel\s*=\s*["\']canonical["\'][^>]*>', head, re.I)
    canonical = None
    if canonical_m:
        href = re.search(r'href\s*=\s*["\']([^"\']*)["\']', canonical_m.group(0), re.I)
        canonical = href.group(1).strip() if href else None

    lang_m = re.search(r'<html[^>]*\blang\s*=\s*["\']([^"\']+)["\']', html, re.I)

    jsonld_blocks = re.findall(
        r'<script[^>]*type\s*=\s*["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.I | re.S)

    imgs = re.findall(r"<img\b[^>]*>", html, re.I)
    imgs_with_alt = [i for i in imgs
                     if re.search(r'\balt\s*=\s*["\'][^"\']+["\']', i, re.I)]

    links = re.findall(r'<a\b[^>]*href\s*=\s*["\']([^"\']+)["\']', html, re.I)
    internal = [l for l in links
                if l.startswith("/") or CANONICAL_HOST in l]

    text = _strip_tags(re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html,
                              flags=re.I | re.S))
    words = len(re.findall(r"\w+", text))

    return {
        "title": title,
        "meta_description": _meta(html, "description"),
        "h1s": h1s,
        "h2_count": len(h2s),
        "canonical": canonical,
        "lang": lang_m.group(1) if lang_m else None,
        "viewport": _meta(html, "viewport") is not None,
        "og_title": _meta(html, "og:title", attr="property"),
        "og_description": _meta(html, "og:description", attr="property"),
        "og_image": _meta(html, "og:image", attr="property"),
        "jsonld_blocks": jsonld_blocks,
        "img_total": len(imgs),
        "img_with_alt": len(imgs_with_alt),
        "internal_links": len(internal),
        "word_count": words,
        "raw_text_lower": text.lower(),
        "raw_html": html,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Scoring best-practice — "salud SEO" de la página (0-100), misma forma que
# email_segments.score_segment: checks {key,label,level,msg,weight,category}.
# ─────────────────────────────────────────────────────────────────────────────
def audit_page(html: str, url: str, role: str = "") -> dict:
    """Audita una página contra las mejores prácticas de SEO médico-local."""
    p = parse_seo(html)
    checks: list[dict] = []

    def add(key, label, cat, ok, warn_if, ok_msg, bad_msg, weight=1, critical=False):
        level = "ok" if ok else ("warn" if warn_if and not critical else "fail")
        checks.append({"key": key, "label": label, "category": cat, "level": level,
                       "msg": ok_msg if ok else bad_msg, "weight": weight,
                       "critical": critical})

    # ── On-page ──────────────────────────────────────────────────────────────
    title = p["title"] or ""
    tlen = len(title)
    add("title_len", "Title (30-60 caracteres)", "on_page",
        30 <= tlen <= 60, 0 < tlen < 30 or 60 < tlen <= 70,
        f"Title de {tlen} caracteres: buen largo para el SERP.",
        f"Title de {tlen} caracteres: apunta a 30-60 (se corta o desperdicia en Google).",
        weight=2)

    add("title_brand", "Title con marca / lugar", "on_page",
        bool(re.search(r"carampangue|arauco|curanilahue|lebu|cmc", title, re.I)), False,
        "El title incluye marca o localidad: refuerza relevancia local.",
        "Suma la marca o la comuna al title para ganar relevancia local.")

    desc = p["meta_description"] or ""
    dlen = len(desc)
    add("meta_desc", "Meta description (120-160)", "on_page",
        120 <= dlen <= 160, 0 < dlen < 120 or 160 < dlen <= 200,
        f"Meta description de {dlen} caracteres con largo óptimo.",
        f"Meta description de {dlen} caracteres: apunta a 120-160 con un CTA.",
        weight=2)

    n_h1 = len(p["h1s"])
    add("h1_unique", "Un único H1", "on_page",
        n_h1 == 1, n_h1 == 0,
        "Exactamente un H1: jerarquía correcta.",
        "Sin H1." if n_h1 == 0 else f"{n_h1} etiquetas H1: debe haber solo una.",
        weight=2)

    add("headings", "Subtítulos H2", "on_page",
        p["h2_count"] >= 2, p["h2_count"] == 1,
        f"{p['h2_count']} H2: buena estructura de contenido.",
        "Pocos H2: estructura el contenido en secciones escaneables.")

    add("word_count", "Contenido suficiente (≥300)", "on_page",
        p["word_count"] >= 300, 120 <= p["word_count"] < 300,
        f"{p['word_count']} palabras: contenido con cuerpo.",
        f"{p['word_count']} palabras: contenido delgado, Google lo subvalora.")

    if p["img_total"]:
        cov = p["img_with_alt"] / p["img_total"]
        add("img_alt", "Imágenes con alt", "on_page",
            cov >= 0.8, cov >= 0.4,
            f"{p['img_with_alt']}/{p['img_total']} imágenes con alt.",
            f"Solo {p['img_with_alt']}/{p['img_total']} con alt: añade texto alternativo (accesibilidad + SEO de imágenes).")

    add("internal_links", "Enlaces internos", "on_page",
        p["internal_links"] >= 3, p["internal_links"] >= 1,
        f"{p['internal_links']} enlaces internos: reparte autoridad al clúster.",
        "Pocos enlaces internos: enlaza a especialidades y comunas relacionadas.")

    # ── Open Graph ─────────────────────────────────────────────────────────────
    add("og", "Open Graph (compartir)", "on_page",
        bool(p["og_title"] and p["og_description"] and p["og_image"]), bool(p["og_title"]),
        "OG completo: se ve bien al compartir en WhatsApp/redes.",
        "Falta og:title/description/image: el preview al compartir queda pobre.")

    if p["og_image"]:
        add("og_image_host", "og:image servible", "on_page",
            p["og_image"].startswith("http"), False,
            "og:image con URL absoluta.",
            "og:image debe ser URL absoluta (las relativas dan 404 fuera del sitio).")

    # ── Técnico ────────────────────────────────────────────────────────────────
    add("viewport", "Viewport móvil", "tecnico",
        p["viewport"], False,
        "Meta viewport presente: mobile-first OK.",
        "Falta <meta name=viewport>: Google indexa mobile-first.", weight=2)

    add("lang", "Atributo lang", "tecnico",
        (p["lang"] or "").lower().startswith("es"), bool(p["lang"]),
        f"lang=\"{p['lang']}\".",
        "Falta lang=\"es\" en <html>.")

    can = p["canonical"] or ""
    add("canonical_present", "Canonical presente", "tecnico",
        bool(can), False,
        "Canonical declarado.",
        "Sin <link rel=canonical>: riesgo de contenido duplicado.", weight=2)

    # CRÍTICO (memoria): canonical al host correcto, no agentecmc.cl
    if can:
        add("canonical_host", "Canonical al dominio canónico", "tecnico",
            CANONICAL_HOST in can, False,
            f"Canonical apunta a {CANONICAL_HOST}.",
            f"Canonical apunta a otro host ({can[:60]}…). Debe ser {CANONICAL_HOST} "
            "(el bug host-split dejó 0 indexación).",
            weight=3, critical=True)

    # JSON-LD: presente, válido, y SIN comentarios HTML (memoria: tumbó la home)
    blocks = p["jsonld_blocks"]
    add("jsonld_present", "Datos estructurados (JSON-LD)", "tecnico",
        len(blocks) >= 1, False,
        f"{len(blocks)} bloque(s) JSON-LD: rich results habilitados.",
        "Sin JSON-LD: pierdes rich results (MedicalClinic, FAQ, breadcrumb).",
        weight=2)

    if blocks:
        has_comment = any("<!--" in b for b in blocks)
        add("jsonld_no_comment", "JSON-LD sin comentarios HTML", "tecnico",
            not has_comment, False,
            "JSON-LD limpio (sin comentarios HTML).",
            "Hay comentarios HTML dentro del JSON-LD: Google lo ignora en silencio "
            "(esto hundió la home 4.8→36.6 por 2 días).",
            weight=3, critical=True)

        valid = True
        for b in blocks:
            try:
                json.loads(b.strip())
            except Exception:
                valid = False
                break
        add("jsonld_valid", "JSON-LD parsea como JSON válido", "tecnico",
            valid, False,
            "Todos los bloques JSON-LD son JSON válido.",
            "Algún bloque JSON-LD no parsea: Google lo descarta.",
            weight=2, critical=True)

    # ── Local SEO ──────────────────────────────────────────────────────────────
    jsonld_all = " ".join(blocks).lower()
    if role in ("home", "comuna", "landing"):
        add("geo_schema", "Schema con geo / MedicalClinic", "local",
            "geocoordinates" in jsonld_all or "medicalclinic" in jsonld_all
            or "medicalbusiness" in jsonld_all, "localbusiness" in jsonld_all,
            "Schema de negocio local con geo: alimenta el map-pack.",
            "Sin MedicalClinic/geo en el schema: el SEO local pierde fuerza.")

        add("area_served", "Comunas mencionadas (areaServed)", "local",
            sum(1 for _, n, _ in COMUNAS if n.lower() in p["raw_text_lower"]) >= 2, True,
            "El contenido nombra varias comunas: cubre el área de servicio.",
            "Menciona explícitamente las comunas que atiendes (Arauco, Curanilahue, Lebu…).")

    # ── Confianza / cumplimiento (memoria, no negociable) ───────────────────────
    add("no_personal_phone", "Sin fuga del teléfono personal", "confianza",
        not PERSONAL_PHONE_RE.search(p["raw_html"]), False,
        "No expone el número personal del Dr.",
        "Aparece el número personal del Dr. (+56987834148): usar el bot o el fijo.",
        weight=2, critical=True)

    found_words = [w for w in FORBIDDEN_TRUST_WORDS if w in p["raw_text_lower"]]
    add("no_misleading", "Sin claims engañosos", "confianza",
        not found_words, False,
        "Sin 'certificados/habilitados/Superintendencia/acreditados'.",
        f"Usa términos potencialmente engañosos ({', '.join(found_words)}): "
        "no todos los profesionales están registrados.",
        weight=2, critical=True)

    # ── Score ────────────────────────────────────────────────────────────────
    total_w = sum(c["weight"] for c in checks)
    got = sum(c["weight"] * (1 if c["level"] == "ok" else 0.5 if c["level"] == "warn" else 0)
              for c in checks)
    score = round(100 * got / total_w) if total_w else 0
    # Un solo check crítico fallado capa el score a 59 (no puede ser "verde").
    has_critical_fail = any(c["critical"] and c["level"] == "fail" for c in checks)
    if has_critical_fail:
        score = min(score, 59)
    grade = "A" if score >= 90 else "B" if score >= 75 else "C" if score >= 60 else "D"

    by_cat: dict[str, dict] = {}
    for c in checks:
        cat = by_cat.setdefault(c["category"], {"ok": 0, "warn": 0, "fail": 0})
        cat[c["level"]] += 1

    return {
        "url": url, "role": role,
        "score": score, "grade": grade,
        "title": title, "has_critical_fail": has_critical_fail,
        "checks": checks, "by_category": by_cat,
        "fails": [c for c in checks if c["level"] == "fail"],
    }


# ─────────────────────────────────────────────────────────────────────────────
# Fetch en vivo + snapshot (read-only, defensivo, concurrente)
# ─────────────────────────────────────────────────────────────────────────────
async def _fetch_one(client: httpx.AsyncClient, target: dict) -> dict:
    url = target["url"]
    try:
        r = await client.get(url, follow_redirects=True,
                             headers={"User-Agent": "CMC-SEO-Autopilot/1.0"})
        if r.status_code >= 400:
            return {"url": url, "role": target.get("role", ""),
                    "label": target.get("label", url), "error": f"HTTP {r.status_code}",
                    "score": 0, "grade": "D", "checks": [], "fails": []}
        result = audit_page(r.text, url, target.get("role", ""))
        result["label"] = target.get("label", url)
        result["status_code"] = r.status_code
        return result
    except Exception as e:  # noqa: BLE001 — una URL caída no tumba el snapshot
        log.warning("seo_audit: no pude auditar %s: %s", url, e)
        return {"url": url, "role": target.get("role", ""),
                "label": target.get("label", url), "error": str(e)[:120],
                "score": 0, "grade": "D", "checks": [], "fails": []}


async def fetch_and_audit(targets: list[dict] | None = None) -> dict:
    """Audita los targets en vivo y arma el snapshot. Read-only sobre el sitio."""
    targets = targets or (default_targets() + load_targets())
    # dedup por URL preservando orden
    seen, uniq = set(), []
    for t in targets:
        if t["url"] not in seen:
            seen.add(t["url"]); uniq.append(t)

    sem = asyncio.Semaphore(6)  # no martillar el sitio

    async def _guarded(client, t):
        async with sem:
            return await _fetch_one(client, t)

    async with httpx.AsyncClient(timeout=15.0) as client:
        pages = await asyncio.gather(*[_guarded(client, t) for t in uniq])

    audited = [p for p in pages if not p.get("error")]
    scores = [p["score"] for p in audited]
    critical = [p for p in audited if p.get("has_critical_fail")]
    snapshot = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "pages": pages,
        "kpis": {
            "audited": len(audited),
            "errored": len(pages) - len(audited),
            "avg_score": round(sum(scores) / len(scores)) if scores else 0,
            "critical_issues": len(critical),
            "grade_a": sum(1 for p in audited if p["grade"] == "A"),
            "needs_work": sum(1 for p in audited if p["grade"] in ("C", "D")),
        },
        "coverage": coverage_matrix(),
        "local_checklist": local_seo_checklist(),
        "opportunities": OPPORTUNITY_TEMPLATES,
    }
    save_snapshot(snapshot)
    return snapshot


# ─────────────────────────────────────────────────────────────────────────────
# Coverage matrix — especialidad × comuna priorizada por demanda × concentración
# ─────────────────────────────────────────────────────────────────────────────
def coverage_matrix() -> dict:
    """Grilla de oportunidad: cada celda esp×comuna con su prioridad (demanda ×
    concentración de pacientes). El sitemap ya genera la URL base-{comuna}; lo
    valioso es priorizar dónde invertir contenido único."""
    cells = []
    for esp_slug, esp_nombre, dem in ESPECIALIDADES:
        for com_slug, com_nombre, conc in COMUNAS:
            priority = dem * conc  # 1-25
            cells.append({
                "esp_slug": esp_slug, "especialidad": esp_nombre,
                "comuna_slug": com_slug, "comuna": com_nombre,
                "priority": priority,
                "url": f"{CANONICAL_BASE}/blog/{esp_slug}-{com_slug}",
                "keyword": f"{esp_nombre.lower()} en {com_nombre}",
            })
    cells.sort(key=lambda c: -c["priority"])
    return {
        "especialidades": [{"slug": s, "nombre": n, "demanda": d} for s, n, d in ESPECIALIDADES],
        "comunas": [{"slug": s, "nombre": n, "concentracion": c} for s, n, c in COMUNAS],
        "top_opportunities": cells[:15],
    }


# ─────────────────────────────────────────────────────────────────────────────
# Checklist de SEO local (NAP / GBP / geo) — el canal #1 de una clínica
# ─────────────────────────────────────────────────────────────────────────────
def local_seo_checklist() -> list[dict]:
    """Checklist accionable de SEO local. 'manual' = requiere acción humana fuera
    del sitio (Google Business Profile, directorios). Estado por defecto 'todo'
    porque no podemos verificar GBP por API sin credenciales — es una guía."""
    return [
        {"key": "gbp_claimed", "label": "Perfil de Empresa de Google reclamado y verificado",
         "why": "El map-pack trae más pacientes que el sitio para una clínica local.",
         "status": "todo", "manual": True},
        {"key": "gbp_category", "label": "Categoría primaria 'Centro médico' + secundarias por especialidad",
         "why": "La categoría es el factor #1 de ranking local en Google Maps.",
         "status": "todo", "manual": True},
        {"key": "nap_consistent", "label": "NAP idéntico en sitio, GBP y directorios",
         "why": "Nombre/Dirección/Teléfono inconsistentes confunden a Google y diluyen señales.",
         "status": "todo", "manual": True},
        {"key": "nap_phone", "label": "Teléfono público = bot (+56966610737) o fijo, nunca el personal",
         "why": "Consistencia + memoria del proyecto: el personal del Dr. no es customer-facing.",
         "status": "todo", "manual": True},
        {"key": "reviews", "label": "Pedir reseñas activamente y responder todas",
         "why": "Volumen y frescura de reseñas + respuestas suben el ranking y la conversión.",
         "status": "todo", "manual": True},
        {"key": "gbp_posts", "label": "Publicar posts semanales en el Perfil de Empresa",
         "why": "Señal de actividad; mantiene la ficha fresca y promociona servicios.",
         "status": "todo", "manual": True},
        {"key": "photos", "label": "Fotos reales del centro, equipo y box",
         "why": "Las fichas con fotos reciben más clics a la web y a 'cómo llegar'.",
         "status": "todo", "manual": True},
        {"key": "directories", "label": "Alta en directorios médicos chilenos (NAP consistente)",
         "why": "Citaciones locales refuerzan la autoridad geográfica.",
         "status": "todo", "manual": True},
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Persistencia — snapshot + targets de usuario (JSON atómico, igual que designs)
# ─────────────────────────────────────────────────────────────────────────────
def save_snapshot(payload: dict) -> None:
    try:
        os.makedirs(os.path.dirname(SNAPSHOT_PATH) or ".", exist_ok=True)
        tmp = SNAPSHOT_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, default=str)
        os.replace(tmp, SNAPSHOT_PATH)
    except Exception as e:  # noqa: BLE001
        log.warning("seo_audit: no pude guardar snapshot: %s", e)


def load_snapshot() -> dict | None:
    try:
        with open(SNAPSHOT_PATH, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:  # noqa: BLE001
        log.warning("seo_audit: no pude leer snapshot: %s", e)
        return None


def load_targets() -> list[dict]:
    """Targets extra definidos por el usuario (además de los default)."""
    try:
        with open(TARGETS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("targets", []) if isinstance(data, dict) else data
    except FileNotFoundError:
        return []
    except Exception as e:  # noqa: BLE001
        log.error("seo_audit.load_targets falló: %s", e)
        return []


def _save_targets(targets: list[dict]) -> None:
    os.makedirs(os.path.dirname(TARGETS_PATH) or ".", exist_ok=True)
    tmp = TARGETS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"targets": targets,
                   "updated_at": datetime.now(timezone.utc).isoformat()},
                  f, ensure_ascii=False, indent=2, default=str)
    os.replace(tmp, TARGETS_PATH)


def add_target(record: dict) -> dict:
    url = (record.get("url") or "").strip()
    if not url.startswith("http"):
        url = f"{CANONICAL_BASE}/{url.lstrip('/')}"
    record["url"] = url
    record.setdefault("role", "landing")
    record.setdefault("label", url.replace(CANONICAL_BASE, "").strip("/") or "Home")
    targets = [t for t in load_targets() if t.get("url") != url]
    targets.insert(0, record)
    _save_targets(targets)
    return record


def delete_target(url: str) -> bool:
    targets = load_targets()
    kept = [t for t in targets if t.get("url") != url]
    if len(kept) == len(targets):
        return False
    _save_targets(kept)
    return True
