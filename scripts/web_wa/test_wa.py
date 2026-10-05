#!/usr/bin/env python3
"""Prueba en Chromium real del marcador (web: pagina . articulo . boton).

Sirve cada fixture bajo su URL publica (para que location.pathname sea el real),
inyecta el static/cmc-wa.js local y vuelca el href resultante de cada link wa.me.
Uso: python3 scripts/web_wa/test_wa.py <dir_fixtures> [--mobile]
  fixtures: home.html (usa templates/sitio.html del repo), blog-<slug>.html -> /blog/<slug>,
            comuna-<slug>.html -> /comuna/<slug>, <slug>.html -> /<slug>, blog-index.html -> /blog
"""
import sys, pathlib, re, urllib.parse
from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parents[2]
JS = (ROOT / "static" / "cmc-wa.js").read_text(encoding="utf-8")
SITE = "https://centromedicocarampangue.cl"


def url_for(name):
    if name == "home": return "/"
    if name == "blog-index": return "/blog"
    if name.startswith("blog-"): return "/blog/" + name[5:]
    if name.startswith("comuna-"): return "/comuna/" + name[7:]
    return "/" + name


def main():
    d = pathlib.Path(sys.argv[1]); mobile = "--mobile" in sys.argv
    vp = {"width": 390, "height": 800} if mobile else {"width": 1280, "height": 800}
    with sync_playwright() as p:
        b = p.chromium.launch()
        for f in sorted(d.glob("*.html")):
            name = f.stem; path = url_for(name)
            html = (ROOT / "templates" / "sitio.html").read_text(encoding="utf-8") if name == "home" else f.read_text(encoding="utf-8")
            if "cmc-wa.js" not in html:  # fixture viva sin el tag: se agrega como lo hace el rewriter
                html = html.replace("</head>", '<script defer src="/static/cmc-wa.js?v=1"></script></head>', 1)
            pg = b.new_page(viewport=vp)
            errs = []; pg.on("pageerror", lambda e: errs.append(str(e)))
            def handle(route):
                u = route.request.url
                if u == SITE + path or u == SITE + path + "/": route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html)
                elif u.startswith(SITE + "/static/cmc-wa.js"): route.fulfill(status=200, content_type="text/javascript", body=JS)
                else: route.abort()
            pg.route("**/*", handle)
            pg.goto(SITE + path); pg.wait_for_load_state("domcontentloaded")
            rows = pg.evaluate("""()=>[...document.querySelectorAll('a[href*="wa.me/56966610737"]')].map(a=>{
              const u=new URL(a.href); return [ (a.className||'-').split(/\\s+/).slice(0,2).join('.'), u.searchParams.get('text'), u.search.includes('utm_')?'utm':'' , a.hasAttribute('data-wa-ok')]})""")
            print(f"\n=== {name}  ({SITE}{path})  viewport={vp['width']}  links={len(rows)}  errores_js={errs or 0}")
            for cls, text, utm, ok in rows:
                print(f"  {'OK ' if ok else 'NO '} [{cls:<22}] {text}  {utm}")
            if name == "home":  # botones de reserva que abren WhatsApp con window.open
                pg.evaluate("window.__opened=[]; window.open=(u)=>{window.__opened.push(u)}")
                for sel in ['.spec-card[data-spec="Ecografía"]', '.promo-card[data-spec="Ortodoncia"]', '.qb-search', 'header [data-open-booking]']:
                    el = pg.query_selector(sel)
                    if not el: print("  (sin elemento)", sel); continue
                    pg.evaluate("window.__opened=[]")
                    el.evaluate("e=>e.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true}))")
                    o = pg.evaluate("window.__opened[0]||null")
                    print(f"  CLICK {sel:<40} -> {urllib.parse.unquote(o.split('text=')[1]) if o else None}")
            pg.close()
        b.close()

main()
