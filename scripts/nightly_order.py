#!/usr/bin/env python3
# ─────────────────────────────────────────────────────────────────────────────
# nightly_order.py — el "ordenador nocturno" del checkout compartido de chatbot-cmc.
#
# NO commitea ni borra trabajo. Su trabajo es REVISAR y REPORTAR para que en la
# mañana sepas, de un vistazo, qué quedó pendiente, de qué ventana, si choca, y
# cómo recuperar cualquier estado. La garantía "no se pierde nada" la da
# eod_snapshot.sh (commits inmutables); esto la hace LEGIBLE.
#
# Hace (todo seguro / read-only salvo la poda de respaldos viejos):
#   1. Agrupa el working tree pendiente por área (eco / autopilot / admin / ...).
#   2. Inventaría stashes y marca los que parecen viejos (base ya en origin/main).
#   3. Lista ramas de respaldo (eod-backup/*, safety/*) = puntos de recuperación.
#   4. Verifica divergencia local/origin y salud de prod.
#   5. Escribe informe HTML en ~/Desktop/cmc_orden_<fecha>.html.
#   6. Poda respaldos > RETAIN_DAYS (default 14) — único cambio destructivo, y solo
#      sobre ramas eod-backup/*+safety/* (nunca main ni feature/*).
#
# Uso:  python3 scripts/nightly_order.py            (informe + poda)
#       python3 scripts/nightly_order.py --no-prune (solo informe)
# ─────────────────────────────────────────────────────────────────────────────
import os, sys, subprocess, html, datetime, re
from pathlib import Path

REPO = Path(os.environ.get("CMC_DIR", str(Path.home() / "chatbot-cmc")))
RETAIN_DAYS = int(os.environ.get("RETAIN_DAYS", "14"))
HEALTH_URL = os.environ.get("CMC_HEALTH", "https://agentecmc.cl/health")
DESKTOP = Path.home() / "Desktop"


def git(*args, ok_fail=False):
    r = subprocess.run(["git", "-C", str(REPO), *args],
                       capture_output=True, text=True)
    if r.returncode != 0 and not ok_fail:
        return ""
    return r.stdout.strip()


# ── Clasificación por área (de qué "ventana/tema" es cada archivo) ───────────
AREAS = [
    ("Ecografía",      re.compile(r"eco|ecografia", re.I)),
    ("Autopilot/SEO",  re.compile(r"autopilot|seo_", re.I)),
    ("Admin/Panel",    re.compile(r"admin|templates/admin", re.I)),
    ("Winback/BI",     re.compile(r"winback|custom_audiences|bi_", re.I)),
    ("Flujo bot",      re.compile(r"app/flows|claude_helper|medilink|session", re.I)),
    ("Jobs/Cron",      re.compile(r"app/jobs|reminders|fidelizacion", re.I)),
    ("Docs/Config",    re.compile(r"CLAUDE\.md|docs/|\.env|config", re.I)),
]


def area_of(path):
    for name, rx in AREAS:
        if rx.search(path):
            return name
    return "Otros"


def pending_by_area():
    status = git("status", "--porcelain")
    buckets = {}
    for line in status.splitlines():
        if not line.strip():
            continue
        code, path = line[:2], line[3:].strip()
        # renames "a -> b"
        if " -> " in path:
            path = path.split(" -> ")[-1]
        kind = "nuevo" if "??" in code else ("borrado" if "D" in code else "modificado")
        buckets.setdefault(area_of(path), []).append((kind, path))
    return buckets


def stash_inventory():
    out = git("stash", "list")
    rows = []
    for i, line in enumerate(out.splitlines()):
        # ¿la base del stash ya está en origin/main? (señal de "viejo")
        base = git("rev-parse", "--short", f"stash@{{{i}}}^", ok_fail=True)
        merged = False
        if base:
            r = subprocess.run(["git", "-C", str(REPO), "merge-base",
                                "--is-ancestor", base, "origin/main"])
            merged = (r.returncode == 0)
        files = git("stash", "show", f"stash@{{{i}}}", "--stat", ok_fail=True)
        nfiles = len([l for l in files.splitlines() if "|" in l])
        rows.append({"i": i, "desc": line.split(":", 2)[-1].strip(),
                     "base": base, "viejo": merged, "nfiles": nfiles})
    return rows


def backup_branches():
    out = git("for-each-ref", "--sort=-committerdate",
              "--format=%(refname:short)|%(committerdate:short)",
              "refs/heads/eod-backup", "refs/heads/safety")
    rows = []
    for line in out.splitlines():
        if "|" in line:
            name, date = line.split("|", 1)
            rows.append((name, date))
    return rows


def divergence():
    git("fetch", "origin", "--quiet", ok_fail=True)
    behind_ahead = git("rev-list", "--left-right", "--count",
                       "origin/main...HEAD", ok_fail=True) or "0\t0"
    behind, ahead = (behind_ahead.split() + ["0", "0"])[:2]
    head = git("rev-parse", "--short", "HEAD")
    return behind, ahead, head


def prod_health():
    # curl usa los certs del sistema (urllib de Python en macOS suele fallar TLS).
    r = subprocess.run(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                        "--max-time", "12", HEALTH_URL], capture_output=True, text=True)
    return r.stdout.strip() or "sin respuesta"


def prune_old():
    """Borra eod-backup/* y safety/* con más de RETAIN_DAYS. NUNCA main/feature/*."""
    cutoff = datetime.date.today() - datetime.timedelta(days=RETAIN_DAYS)
    deleted = []
    out = git("for-each-ref", "--format=%(refname:short)|%(committerdate:short)",
              "refs/heads/eod-backup", "refs/heads/safety")
    for line in out.splitlines():
        if "|" not in line:
            continue
        name, date = line.split("|", 1)
        try:
            d = datetime.date.fromisoformat(date)
        except ValueError:
            continue
        if d < cutoff:
            git("branch", "-D", name, ok_fail=True)
            deleted.append(name)
    return deleted


def build_html(data):
    def esc(s): return html.escape(str(s))
    areas = "".join(
        f"<div class='area'><h3>{esc(a)} <span>{len(items)}</span></h3>"
        + "".join(f"<div class='f f-{k}'><b>{esc(k)}</b> {esc(p)}</div>" for k, p in items)
        + "</div>"
        for a, items in sorted(data["pending"].items(), key=lambda x: -len(x[1]))
    ) or "<p class='ok'>Árbol limpio — nada pendiente. 🎉</p>"

    stashes = "".join(
        f"<tr class='{'old' if s['viejo'] else ''}'><td>#{s['i']}</td><td>{esc(s['desc'])}</td>"
        f"<td>{s['nfiles']} arch.</td><td>{esc(s['base'])}</td>"
        f"<td>{'⚠️ base ya en main (revisar/descartar)' if s['viejo'] else 'única'}</td></tr>"
        for s in data["stashes"]
    ) or "<tr><td colspan=5 class='ok'>Sin stashes.</td></tr>"

    backups = "".join(
        f"<tr><td>{esc(n)}</td><td>{esc(d)}</td></tr>" for n, d in data["backups"][:30]
    ) or "<tr><td colspan=2>—</td></tr>"

    behind, ahead, head = data["div"]
    div_cls = "ok" if behind == "0" and ahead == "0" else "warn"
    return f"""<!doctype html><meta charset=utf8>
<title>Orden nocturno CMC · {data['date']}</title>
<style>
 body{{font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;background:#0f1720;color:#e6edf3;margin:0;padding:28px;max-width:980px}}
 h1{{font-size:22px;margin:0 0 4px}} .sub{{color:#8aa0b4;margin:0 0 22px}}
 .grid{{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin:18px 0}}
 .card{{background:#16212e;border:1px solid #243446;border-radius:12px;padding:16px}}
 .card h2{{font-size:13px;text-transform:uppercase;letter-spacing:.5px;color:#7fd4e0;margin:0 0 10px}}
 .big{{font-size:26px;font-weight:700}} .ok{{color:#5fd38d}} .warn{{color:#f0b35b}} .bad{{color:#f06b6b}}
 .area{{margin-bottom:12px}} .area h3{{font-size:13px;margin:0 0 4px;color:#cfe0ee}}
 .area h3 span{{background:#243446;border-radius:10px;padding:1px 8px;font-size:11px;margin-left:6px}}
 .f{{font-size:12px;color:#aebfce;padding:2px 0 2px 10px;border-left:2px solid #2a3a4c}}
 .f b{{font-size:10px;text-transform:uppercase;color:#6f8499;margin-right:6px}}
 .f-nuevo b{{color:#5fd38d}} .f-borrado b{{color:#f06b6b}}
 table{{width:100%;border-collapse:collapse;font-size:12px}} td,th{{text-align:left;padding:5px 8px;border-bottom:1px solid #243446}}
 tr.old{{opacity:.7}} tr.old td{{color:#f0b35b}}
 .note{{font-size:12px;color:#8aa0b4;margin-top:8px}}
</style>
<h1>🌙 Orden nocturno · chatbot-cmc</h1>
<p class=sub>{data['date']} · revisión read-only del checkout compartido por las ventanas en paralelo</p>

<div class=grid>
  <div class=card><h2>Sincronía git</h2>
    <div class='big {div_cls}'>{'✓ 0/0' if div_cls=='ok' else f'{behind}/{ahead}'}</div>
    <div class=note>HEAD {esc(head)} · {'local == origin/main' if div_cls=='ok' else f'{behind} atrás / {ahead} adelante de origin/main'}</div></div>
  <div class=card><h2>Salud de prod</h2>
    <div class='big {'ok' if str(data['health'])=='200' else 'bad'}'>{esc(data['health'])}</div>
    <div class=note>{esc(HEALTH_URL)}</div></div>
</div>

<div class=card><h2>Pendiente en el árbol — por tema/ventana</h2>{areas}</div>

<div class=card style=margin-top:14px><h2>Stashes ({len(data['stashes'])})</h2>
 <table><tr><th></th><th>descripción</th><th></th><th>base</th><th>estado</th></tr>{stashes}</table>
 <div class=note>Los marcados ⚠️ tienen su commit base ya en origin/main → probablemente WIP viejo. Revisá antes de <code>git stash drop</code>.</div></div>

<div class=card style=margin-top:14px><h2>Puntos de recuperación (respaldos)</h2>
 <table><tr><th>rama</th><th>fecha</th></tr>{backups}</table>
 <div class=note>Recuperar un archivo de un respaldo: <code>git checkout &lt;rama&gt; -- ruta/al/archivo</code>. Se podan automáticamente tras {RETAIN_DAYS} días.</div></div>

{f"<p class=note>Podados hoy: {', '.join(map(esc, data['pruned']))}</p>" if data.get('pruned') else ""}
"""


def main():
    no_prune = "--no-prune" in sys.argv
    data = {
        "date": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "pending": pending_by_area(),
        "stashes": stash_inventory(),
        "backups": backup_branches(),
        "div": divergence(),
        "health": prod_health(),
        "pruned": [] if no_prune else prune_old(),
    }
    DESKTOP.mkdir(exist_ok=True)
    out = DESKTOP / f"cmc_orden_{datetime.date.today().isoformat()}.html"
    out.write_text(build_html(data), encoding="utf-8")

    # Resumen de texto (para logs / terminal).
    npend = sum(len(v) for v in data["pending"].values())
    behind, ahead, head = data["div"]
    print(f"🌙 orden nocturno {data['date']}")
    print(f"   pendiente: {npend} archivo(s) en {len(data['pending'])} tema(s)")
    print(f"   stashes: {len(data['stashes'])} ({sum(1 for s in data['stashes'] if s['viejo'])} viejos)")
    print(f"   git: HEAD {head} · {behind}/{ahead} vs origin/main · prod {data['health']}")
    if data["pruned"]:
        print(f"   podados: {', '.join(data['pruned'])}")
    print(f"   informe: {out}")


if __name__ == "__main__":
    main()
