"""Generador de páginas SEO especialidad × comuna (programmatic SEO del CMC).

El dashboard /seo identifica las celdas especialidad×comuna de mayor oportunidad
(coverage_matrix). Acá se CREAN esas páginas: estructura + schema SEO fijos (para que
Google las entienda) y el contenido único lo escribe Claude (para que NO sean thin/
duplicadas). Se guardan en data/seo_generated/ y se sirven en /seo/p/{slug} para poder
previsualizarlas embebidas en el panel y, si gustan, publicarlas al sitio.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from autopilot.seo_audit import ESPECIALIDADES, COMUNAS, coverage_matrix, demanda_seo

_DIR = Path(os.getenv("SEO_PAGES_DIR", "data/seo_generated"))
_BASE = "https://centromedicocarampangue.cl"
_WA = "56966610737"

_ESP = {s: n for s, n, *_ in ESPECIALIDADES}
_COM = {s: n for s, n, _ in COMUNAS}


def _slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9-]", "", s.lower().replace(" ", "-"))


def _wa_link(esp: str, comuna: str) -> str:
    txt = f"Hola, quiero agendar {esp} (vengo de {comuna})"
    from urllib.parse import quote
    return (f"https://wa.me/{_WA}?text={quote(txt)}"
            "&utm_source=seo&utm_medium=landing&utm_campaign=esp_comuna")


_TEMPLATE = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<meta name="description" content="{meta_desc}">
<link rel="canonical" href="{canonical}">
<meta name="robots" content="{robots}">
<meta property="og:title" content="{title}">
<meta property="og:description" content="{meta_desc}">
<meta property="og:type" content="website">
<meta property="og:url" content="{canonical}">
<script type="application/ld+json">
{schema_clinic}
</script>
<script type="application/ld+json">
{schema_faq}
</script>
<script type="application/ld+json">
{schema_breadcrumb}
</script>
<style>
 :root{{--aqua:#4FBECE;--navy:#0F3F68;--azul:#1172AB;--bg:#F5F8FA;--ink:#0f172a}}
 *{{box-sizing:border-box}} body{{margin:0;font-family:'Montserrat',system-ui,Arial,sans-serif;color:var(--ink);background:var(--bg);line-height:1.6}}
 .wrap{{max-width:820px;margin:0 auto;padding:0 20px}}
 header.hero{{background:linear-gradient(135deg,var(--navy),var(--azul));color:#fff;padding:48px 0 40px}}
 header.hero h1{{font-size:1.9rem;margin:0 0 8px}} header.hero .sub{{opacity:.9}}
 .cta{{display:inline-block;background:#25D366;color:#fff;text-decoration:none;font-weight:700;padding:14px 24px;border-radius:12px;margin-top:18px}}
 main{{padding:32px 0}} h2{{color:var(--navy);font-size:1.3rem;margin-top:28px}}
 .card{{background:#fff;border:1px solid #e2e8f0;border-radius:16px;padding:20px;margin:14px 0}}
 details{{background:#fff;border:1px solid #e2e8f0;border-radius:12px;padding:14px 18px;margin:10px 0}}
 details summary{{font-weight:700;cursor:pointer;color:var(--navy)}}
 footer{{background:var(--navy);color:#cbd5e1;padding:28px 0;font-size:.9rem}}
 footer a{{color:#fff}}
 .badge{{display:inline-block;background:rgba(255,255,255,.15);padding:4px 12px;border-radius:99px;font-size:.8rem;margin-bottom:12px}}
</style>
</head>
<body>
<header class="hero"><div class="wrap">
  <span class="badge">Atendemos a {comuna}</span>
  <h1>{h1}</h1>
  <div class="sub">Centro Médico Carampangue · {esp}</div>
  <a class="cta" href="{wa}">💬 Agendar por WhatsApp</a>
</div></header>
<main class="wrap">
  {intro}
  {body}
  <h2>Preguntas frecuentes</h2>
  {faq_html}
  <div class="card" style="text-align:center;margin-top:32px">
    <strong>¿List@ para agendar tu {esp_low} desde {comuna}?</strong><br>
    <a class="cta" href="{wa}">💬 Escríbenos por WhatsApp</a>
  </div>
</main>
<footer><div class="wrap">
  Centro Médico Carampangue — {esp} para pacientes de {comuna} y alrededores.
  WhatsApp <a href="{wa}">+56 9 6661 0737</a> ·
  <a href="https://centromedicocarampangue.cl">centromedicocarampangue.cl</a>
</div></footer>
</body>
</html>"""


async def _generar_contenido(esp: str, comuna: str) -> dict:
    """Pide a Claude el contenido ÚNICO de la página (JSON). Si falla, plantilla
    determinista de respaldo para nunca devolver vacío."""
    prompt = (
        f"Eres redactor SEO de un centro médico en Carampangue, Región del Biobío, Chile. "
        f"Escribe el contenido de una landing page para la keyword \"{esp} en {comuna}\". "
        f"Público: pacientes de {comuna} y alrededores que buscan {esp}. Tono cercano, "
        f"chileno, claro, sin exagerar ni prometer curas. NO inventes precios, distancias "
        f"exactas ni nombres de médicos. Devuelve SOLO un JSON con esta forma:\n"
        '{"meta_desc":"<155 chars>","intro_html":"<2 párrafos <p>…</p>>",'
        '"body_html":"<2-3 secciones con <h2> y <p>, por qué elegir el CMC para esta '
        'especialidad, qué incluye la atención, y mención a pacientes de la comuna>",'
        '"faqs":[{"q":"…","a":"…"},{"q":"…","a":"…"},{"q":"…","a":"…"}]}'
    )
    try:
        from claude_helper import client
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1600,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
        raw = re.sub(r"^```json|^```|```$", "", raw, flags=re.MULTILINE).strip()
        data = json.loads(raw)
        if not data.get("faqs"):
            raise ValueError("sin faqs")
        return data
    except Exception:  # noqa: BLE001 — respaldo determinista
        return {
            "meta_desc": f"{esp} en {comuna}: agenda en el Centro Médico Carampangue por WhatsApp. Atención cercana para pacientes de {comuna}.",
            "intro_html": (f"<p>¿Buscas <strong>{esp.lower()} en {comuna}</strong>? En el "
                           f"Centro Médico Carampangue atendemos a pacientes de {comuna} y "
                           f"alrededores con horarios flexibles y agendamiento por WhatsApp.</p>"),
            "body_html": (f"<h2>{esp} cerca de {comuna}</h2><p>Nuestro equipo atiende "
                          f"{esp.lower()} con un trato cercano. Coordina tu hora por WhatsApp "
                          f"y te mostramos la disponibilidad al instante.</p>"),
            "faqs": [
                {"q": f"¿Atienden {esp.lower()} a pacientes de {comuna}?",
                 "a": f"Sí, recibimos pacientes de {comuna} y comunas vecinas. Agenda por WhatsApp."},
                {"q": "¿Cómo agendo?",
                 "a": "Por WhatsApp al +56 9 6661 0737, 24/7, con nuestro asistente automático."},
            ],
        }


def _faq_html(faqs: list[dict]) -> str:
    return "\n".join(
        f"<details><summary>{f['q']}</summary><p>{f['a']}</p></details>" for f in faqs
    )


def _faq_schema(faqs: list[dict]) -> str:
    return json.dumps({
        "@context": "https://schema.org", "@type": "FAQPage",
        "mainEntity": [
            {"@type": "Question", "name": f["q"],
             "acceptedAnswer": {"@type": "Answer", "text": f["a"]}} for f in faqs
        ],
    }, ensure_ascii=False, indent=1)


async def generar(esp_slug: str, com_slug: str, *, publicar: bool = False) -> dict:
    esp = _ESP.get(esp_slug)
    comuna = _COM.get(com_slug)
    if not esp or not comuna:
        raise ValueError(f"especialidad/comuna desconocida: {esp_slug}/{com_slug}")
    slug = f"{esp_slug}-{com_slug}"
    canonical = f"{_BASE}/seo/p/{slug}"
    wa = _wa_link(esp, comuna)
    cont = await _generar_contenido(esp, comuna)
    title = f"{esp} en {comuna} | Centro Médico Carampangue"
    schema_clinic = json.dumps({
        "@context": "https://schema.org", "@type": "MedicalClinic",
        "name": "Centro Médico Carampangue",
        "url": canonical, "telephone": "+56966610737",
        "medicalSpecialty": esp,
        "areaServed": {"@type": "City", "name": comuna},
        "address": {"@type": "PostalAddress", "addressLocality": "Carampangue",
                    "addressRegion": "Biobío", "addressCountry": "CL"},
    }, ensure_ascii=False, indent=1)
    schema_breadcrumb = json.dumps({
        "@context": "https://schema.org", "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "Inicio", "item": _BASE},
            {"@type": "ListItem", "position": 2, "name": esp, "item": canonical},
            {"@type": "ListItem", "position": 3, "name": comuna, "item": canonical},
        ],
    }, ensure_ascii=False, indent=1)
    html = _TEMPLATE.format(
        title=title, meta_desc=cont["meta_desc"], canonical=canonical,
        robots=("index,follow" if publicar else "noindex,follow"),
        schema_clinic=schema_clinic, schema_faq=_faq_schema(cont["faqs"]),
        schema_breadcrumb=schema_breadcrumb,
        comuna=comuna, esp=esp, esp_low=esp.lower(),
        h1=f"{esp} en {comuna}", wa=wa,
        intro=cont["intro_html"], body=cont["body_html"],
        faq_html=_faq_html(cont["faqs"]),
    )
    _DIR.mkdir(parents=True, exist_ok=True)
    (_DIR / f"{slug}.html").write_text(html, encoding="utf-8")
    meta = {"slug": slug, "esp_slug": esp_slug, "com_slug": com_slug,
            "especialidad": esp, "comuna": comuna, "title": title,
            "publicado": publicar, "created": datetime.now(timezone.utc).isoformat(),
            "url": f"/seo/p/{slug}"}
    (_DIR / f"{slug}.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return {**meta, "html": html}


def get_html(slug: str) -> str | None:
    f = _DIR / f"{_slugify(slug)}.html"
    return f.read_text(encoding="utf-8") if f.exists() else None


def listar() -> list[dict]:
    if not _DIR.exists():
        return []
    out = []
    for jf in sorted(_DIR.glob("*.json")):
        try:
            out.append(json.loads(jf.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001
            pass
    out.sort(key=lambda m: m.get("created", ""), reverse=True)
    return out


def oportunidades(top_n: int = 24) -> list[dict]:
    """Celdas esp×comuna de mayor prioridad que AÚN no se generaron."""
    hechas = {m["slug"] for m in listar()}
    cells = coverage_matrix().get("top_opportunities", [])
    # top_opportunities trae solo 15; reconstruimos la grilla completa ordenada.
    full = []
    for esp_slug, esp, vol, intent in ESPECIALIDADES:
        dem = demanda_seo(vol, intent)
        for com_slug, com, conc in COMUNAS:
            full.append({"esp_slug": esp_slug, "especialidad": esp,
                         "com_slug": com_slug, "comuna": com,
                         "priority": dem * conc,
                         "slug": f"{esp_slug}-{com_slug}",
                         "keyword": f"{esp.lower()} en {com}"})
    full.sort(key=lambda c: -c["priority"])
    return [c for c in full if c["slug"] not in hechas][:top_n]
