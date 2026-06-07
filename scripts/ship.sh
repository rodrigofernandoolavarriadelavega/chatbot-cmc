#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# ship.sh — deploya EN VIVO solo TUS archivos, sin chocar con la WIP de las otras
# ventanas. Pensado para el flujo de muchas sesiones en paralelo sobre el mismo
# checkout: cada ventana "embarca" su pedacito cuando está listo y lo ve en prod,
# sin esperar a las demás ni pisarlas.
#
# Qué hace, en orden, todo con red de seguridad:
#   1. SNAPSHOT del árbol completo (eod_snapshot) — nada se puede perder.
#   2. Aparta (stash) los archivos MODIFICADOS que NO son tuyos.
#   3. Commitea SOLO los tuyos (rutas explícitas) con tu mensaje.
#   4. Deploya por scripts/deploy.sh (G1-G4: deep-import + health + auto-rollback).
#   5. Restaura (pop) la WIP ajena, intacta.
# Si algo falla, restaura la WIP ajena igual (trap) y el snapshot queda de respaldo.
#
# Uso:
#   bash scripts/ship.sh "feat(seo): tooltip desglose" templates/autopilot_dashboard.html
#   bash scripts/ship.sh "fix(eco): persistir tipo" app/flows.py tests/test_eco_menu_loop.py
#
# Regla de oro del flujo paralelo: cada ventana toca archivos DISTINTOS. Si dos
# tocan el mismo archivo, el árbol ya los mezcló — ahí sí conviene coordinar.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail
cd "${CMC_DIR:-$HOME/chatbot-cmc}"
HERE="$(cd "$(dirname "$0")" && pwd)"

c_red(){ printf '\033[31m%s\033[0m\n' "$*"; }
c_grn(){ printf '\033[32m%s\033[0m\n' "$*"; }
c_ylw(){ printf '\033[33m%s\033[0m\n' "$*"; }

[ $# -ge 2 ] || { c_red "Uso: ship.sh \"<mensaje>\" <archivo> [archivo...]"; exit 1; }
MSG="$1"; shift
MINE=("$@")

# Validar que los archivos existen.
for f in "${MINE[@]}"; do
  [ -e "$f" ] || { c_red "No existe: $f"; exit 1; }
done

echo "── ship: $(printf '%s ' "${MINE[@]}")──"

# 1. SNAPSHOT (nada se pierde).
PUSH=0 bash "$HERE/eod_snapshot.sh" || true

# 2. ¿Qué archivos MODIFICADOS-tracked NO son míos? (los untracked ajenos no
#    bloquean el deploy, así que no hace falta tocarlos.)
#    Compatible bash 3.2 (macOS): sin arrays asociativos ni mapfile.
MINE_STR=" ${MINE[*]} "
OTHERS=""
for f in $(git diff --name-only; git diff --cached --name-only); do
  case "$MINE_STR" in
    *" $f "*) : ;;                                  # es mío → no apartar
    *) case " $OTHERS " in
         *" $f "*) : ;;                             # ya listado (dedup)
         *) OTHERS="$OTHERS $f" ;;
       esac ;;
  esac
done
OTHERS="$(echo "$OTHERS" | sed 's/^ *//;s/ *$//')"  # trim

STASHED=0
restore() {
  if [ "$STASHED" = "1" ]; then
    echo "→ restaurando WIP ajena…"
    git stash pop >/dev/null 2>&1 && c_grn "  WIP ajena restaurada." \
      || c_ylw "  ⚠️ pop con conflicto — tu WIP ajena está en 'git stash list' (stash@{0}), recuperable."
  fi
}
trap restore EXIT

if [ -n "$OTHERS" ]; then
  N=$(echo "$OTHERS" | wc -w | tr -d ' ')
  echo "→ aparto WIP ajena ($N arch.): $OTHERS"
  git stash push -q -- $OTHERS && STASHED=1
fi

# 3. Commit SOLO lo mío.
git add -- "${MINE[@]}"
if git diff --cached --quiet; then
  c_ylw "Nada que commitear en tus archivos (¿ya estaban commiteados?). Deploy igual del estado actual."
else
  git commit -q -m "$MSG

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
  c_grn "  commit: $(git rev-parse --short HEAD) $MSG"
fi

# 4. Deploy guardado (push + G1-G4 + auto-rollback).
echo "→ deploy…"
if bash "$HERE/deploy.sh"; then
  c_grn "✔ embarcado y en vivo."
else
  c_red "✘ deploy abortó (prod quedó en su estado seguro). Tu commit está local; revisá el motivo arriba."
fi
# trap restore corre acá: devuelve la WIP ajena.
