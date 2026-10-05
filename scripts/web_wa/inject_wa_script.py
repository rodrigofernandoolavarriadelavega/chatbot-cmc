#!/usr/bin/env python3
"""Inserta (idempotente) <script defer src="/static/cmc-wa.js?v=N"> antes de </head>
en las plantillas publicas del sitio que tienen links wa.me del CMC.

Uso:
  python3 scripts/web_wa/inject_wa_script.py            # aplica
  python3 scripts/web_wa/inject_wa_script.py --check    # no escribe; exit 1 si falta alguna
  python3 scripts/web_wa/inject_wa_script.py --bump     # sube la version ?v= (tras editar cmc-wa.js)
"""
import re, sys, pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
T = ROOT / "templates"
VERSION = "1"
# Paginas publicas de centromedicocarampangue.cl (sitemap + landings por comuna/especialidad)
FILES = (
    [T / n for n in (
        "sitio.html", "blog_index.html", "comuna_hub.html", "comuna_template.html",
        "landing_ortodoncia.html", "chequeos.html", "empresas.html",
        "dentista-curanilahue.html", "ginecologo-curanilahue.html",
        "otorrino-curanilahue.html", "traumatologo-curanilahue.html",
    )]
    + sorted((T / "blog").glob("*.html"))
)
TAG_RE = re.compile(r'<script defer src="/static/cmc-wa\.js\?v=[^"]*"></script>\n?')


def tag(v):
    return f'<script defer src="/static/cmc-wa.js?v={v}"></script>\n'


def main():
    check, bump = "--check" in sys.argv, "--bump" in sys.argv
    falta = []
    for p in FILES:
        if not p.exists():
            continue
        s = p.read_text(encoding="utf-8")
        if "wa.me/56966610737" not in s and "{{WA_" not in s:
            continue
        m = TAG_RE.search(s)
        if m and not bump:
            continue
        if check:
            falta.append(p.name)
            continue
        v = VERSION
        if m and bump:
            cur = re.search(r"v=([^\"]*)", m.group(0)).group(1)
            v = str(int(cur) + 1) if cur.isdigit() else VERSION
            s = TAG_RE.sub("", s, count=1)
        if "</head>" not in s:
            print("SIN </head>:", p.name)
            continue
        s = s.replace("</head>", tag(v) + "</head>", 1)
        p.write_text(s, encoding="utf-8")
        print("ok", p.name, "v=" + v)
    if check:
        print("faltan:", falta or "ninguna")
        sys.exit(1 if falta else 0)


if __name__ == "__main__":
    main()
