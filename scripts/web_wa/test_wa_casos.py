#!/usr/bin/env python3
"""Casos limite del marcador + ida y vuelta con la regex REAL del bot (app/flows.py)."""
import pathlib, re, urllib.parse
from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parents[2]
JS = (ROOT / "static" / "cmc-wa.js").read_text(encoding="utf-8")
SITE = "https://centromedicocarampangue.cl"
# regex y split copiados tal cual de app/flows.py (handle_message)
BOT_END = re.compile(r"\(\s*web(?:\s*[:：]\s*([^()]{0,120}?))?\s*\)\s*$", re.I)
BOT_START = re.compile(r"^\s*\(\s*web(?:\s*[:：]\s*([^()]{0,120}?))?\s*\)\s*", re.I)

def bot_parse(txt):
    m = BOT_END.search(txt) or BOT_START.match(txt)
    if not m: return None
    partes = [p.strip().lower() for p in re.split(r"[·|/]", m.group(1) or "") if p.strip()]
    g = lambda i, n=80: re.sub(r"[^\w-]", "", partes[i])[:n] if len(partes) > i else ""
    limpio = (txt[:m.start()] + txt[m.end():]).strip(" .,-–—")
    return {"pagina": g(0), "articulo": g(1), "boton": g(2, 40), "texto": limpio}

A = "https://wa.me/56966610737"
CASOS = [  # (ruta, href de entrada, esperado pagina/boton o None)
  ("/blog/eco-abdominal", A+"?text=Hola%2C%20quiero%20agendar%20una%20Ecograf%C3%ADa.%20(web%3A%20blog_float)&utm_source=blog", ("blog","flotante")),
  ("/comuna/lebu", A+"?text=Hola%20desde%20Lebu%20(web%3A%20comuna_float)", ("comuna","flotante")),
  ("/comuna/lebu", A+"?text=Hola%20desde%20Lebu%20(web%3A%20comuna_cta)", ("comuna",None)),
  ("/lebu", A+"?text=Hola%2C%20soy%20de%20Lebu%20(web)", ("comuna",None)),
  ("/", A+"?text=%20(web)Hola%2C%20quiero%20agendar%20una%20hora%20de%20Podolog%C3%ADa.", ("home",None)),
  ("/", A+"?text=Quiero+ver+mis+citas", ("home",None)),
  ("/", A, ("home",None)),
  ("/blog/x", A+"/?utm_source=comuna_footer&utm_campaign=lebu", ("blog",None)),
  ("/empresas", A+"?text=Hola%20empresa%20(web%3A%20x)(web%3A%20y)", ("x",None)),
  ("/chequeos", A+"?text=Hola", ("chequeos",None)),
  ("/blog/eco/", A+"?text=Hola%20(web%3A%20blog)", ("blog",None)),  # slash final
]
ok = fail = 0
with sync_playwright() as p:
    b = p.chromium.launch()
    for ruta, href, esp in CASOS:
        pg = b.new_page()
        def h(r):
            u = r.request.url
            if u == SITE + ruta or u == SITE + ruta.rstrip("/"):
                r.fulfill(status=200, content_type="text/html", body=f'<html><head><script src="/static/cmc-wa.js"></script></head><body><main><a id="t" href="{href}">x</a></main></body></html>')
            elif "cmc-wa.js" in u: r.fulfill(status=200, content_type="text/javascript", body=JS)
            else: r.abort()
        pg.route("**/*", h); pg.goto(SITE + ruta)
        out = pg.evaluate("document.getElementById('t').href")
        text = urllib.parse.parse_qs(urllib.parse.urlparse(out).query).get("text", [""])[0]
        r = bot_parse(text)
        good = bool(r) and r["pagina"] == esp[0] and (esp[1] is None or r["boton"] == esp[1]) and bool(r["articulo"]) and bool(r["boton"]) and len(re.findall(r"\(web", text)) == 1 and text.endswith(")")
        ok += good; fail += (not good)
        print(("OK  " if good else "FALLA"), ruta, "|", text, "|", r, "| utm conservado" if "utm_" in href and "utm_" in out else "")
        pg.close()
    b.close()
print(f"\n{ok} ok, {fail} fallas")
