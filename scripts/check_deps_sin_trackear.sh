#!/usr/bin/env bash
#
# check_deps_sin_trackear.sh — cazador de dependencias que NO viajan en el deploy.
#
# POR QUÉ EXISTE (tres bugs reales en una sola noche, 2026-07-29):
#
#   1. `app/franja.py` sin trackear, importado por flows.py en 5 lugares. Los cinco
#      imports eran LOCALES a función, así que el bot arrancaba bien, pasaba el guard
#      G3 (deep-import de módulos top-level) y reventaba en caliente la primera vez
#      que un paciente escribía "en la mañana".
#
#   2. `main.py:4317` usaba `_cfg.AGENDADOR_V2_ENABLED`, definida SOLO en el config.py
#      local sin commitear. Deploy exitoso → AttributeError al entrar a /agendar/v2.
#
#   3. `reemplazo_ingreso_dashboard.html` embarcado sin su CSS: la página quedó en
#      producción sin estilos (404 en /static/tw/...).
#
# Las tres son la misma familia: algo que funciona en tu máquina porque el archivo
# está ahí, y se rompe en producción porque no viajó. El guard G3 no las ve.
#
# Sale 1 si encuentra un bloqueante (módulo o constante). Los templates y CSS
# faltantes solo avisan: están guardados con `if exists()` y degradan a 404.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

rojo()  { printf '\033[31m%s\033[0m\n' "$*"; }
ambar() { printf '\033[33m%s\033[0m\n' "$*"; }
verde() { printf '\033[32m%s\033[0m\n' "$*"; }

FALLO=0

# ── 1. Módulos importados (incluso dentro de funciones) que no estén en git ───
while read -r mod; do
  [ -z "$mod" ] && continue
  cand="app/${mod}.py"
  [ -f "$cand" ] || continue
  if ! git ls-files --error-unmatch "$cand" >/dev/null 2>&1; then
    rojo "BLOQUEANTE: se importa '${mod}' pero ${cand} NO está en git."
    grep -rn --include='*.py' -E "^[[:space:]]+(import|from)[[:space:]]+${mod}\b" app/ | head -3 | sed 's/^/    /'
    FALLO=1
  fi
done < <(grep -rhoE "^[[:space:]]+(import|from)[[:space:]]+[a-z_][a-z0-9_]*" app/*.py 2>/dev/null \
         | awk '{print $2}' | sort -u)

# ── 2. Constantes de config usadas pero definidas solo en el config sin commitear ──
if git show HEAD:app/config.py >/tmp/_cfg_head.py 2>/dev/null; then
  while read -r c; do
    [ -z "$c" ] && continue
    # ignora las que solo aparecen dentro de strings (metadatos tipo sonda="flag:X")
    usos=$(grep -rn --include='*.py' -E "(^|[^\"'])\b${c}\b" app/ 2>/dev/null \
           | grep -v '^app/config.py:' | grep -vE "\"flag:|'flag:" | head -3)
    if [ -n "$usos" ]; then
      rojo "BLOQUEANTE: ${c} se usa pero solo existe en el config.py SIN COMMITEAR."
      echo "$usos" | sed 's/^/    /'
      echo "    → app/config.py tiene que viajar en el mismo commit."
      FALLO=1
    fi
  done < <(comm -13 \
      <(grep -oE '^[A-Z][A-Z0-9_]{3,}[[:space:]]*=' /tmp/_cfg_head.py | tr -d ' =' | sort -u) \
      <(grep -oE '^[A-Z][A-Z0-9_]{3,}[[:space:]]*=' app/config.py  | tr -d ' =' | sort -u))
  rm -f /tmp/_cfg_head.py
fi

# ── 3. Templates que main.py carga y no están en git (degradan a 404, no crashean) ──
while read -r t; do
  [ -z "$t" ] && continue
  ruta="templates/${t}"
  [ -f "$ruta" ] || continue
  git ls-files --error-unmatch "$ruta" >/dev/null 2>&1 || \
    ambar "AVISO: main.py carga ${ruta} y no está en git → esa ruta dará 404."
done < <(grep -oE '_TEMPLATE_DIR / "[a-z0-9_]+\.html"' app/main.py 2>/dev/null \
         | grep -oE '[a-z0-9_]+\.html' | sort -u)

# ── 4. CSS/JS locales que un template servido referencia y no están en git ────
while read -r asset; do
  [ -z "$asset" ] && continue
  ruta="${asset#/}"
  [ -f "$ruta" ] || continue
  git ls-files --error-unmatch "$ruta" >/dev/null 2>&1 || \
    ambar "AVISO: un template referencia ${ruta} y no está en git → 404 (página sin estilos)."
done < <(grep -rhoE '(href|src)="/static/[a-zA-Z0-9._/-]+\.(css|js)"' templates/ 2>/dev/null \
         | grep -oE '/static/[a-zA-Z0-9._/-]+\.(css|js)' | sort -u)

[ "$FALLO" -eq 0 ] && verde "  ok deps: nada importado/usado queda fuera del commit"
exit "$FALLO"
