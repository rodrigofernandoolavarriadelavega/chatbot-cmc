#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# newsession.sh — abre una "sesión" AISLADA para una ventana de Claude usando un
# git worktree. Cada tema vive en su PROPIA carpeta y rama → dos ventanas NUNCA
# se pisan a nivel de archivo (es la versión fuerte de ship.sh).
#
# Uso:   bash scripts/newsession.sh eco        # crea ~/cmc-work/eco en rama session/eco
#        bash scripts/newsession.sh seo
# Luego: abrí una ventana de Claude en esa carpeta y trabajá normal.
# Deploy desde ahí:  bash scripts/wship.sh "<mensaje>"   (rebasa sobre main y deploya)
# Cerrar el tema:    bash scripts/newsession.sh --done eco
#
# Comparte el mismo .git (rápido, sin re-clonar). Enlaza .env y data/ del checkout
# principal porque son gitignored y un worktree no los hereda (los necesita para
# importar la app y para los tests).
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail
MAIN="${CMC_DIR:-$HOME/chatbot-cmc}"
WROOT="${CMC_WORKTREES:-$HOME/cmc-work}"
cd "$MAIN"

if [ "${1:-}" = "--done" ]; then
  TEMA="$(echo "${2:?Uso: newsession.sh --done <tema>}" | tr ' ' '-' | tr -cd 'a-zA-Z0-9-_')"
  git worktree remove --force "$WROOT/$TEMA" 2>/dev/null && echo "✔ sesión '$TEMA' cerrada (worktree removido)." \
    || echo "No encontré worktree para '$TEMA'."
  echo "  La rama session/$TEMA queda por si tenía commits sin mergear (git branch -D session/$TEMA para borrarla)."
  exit 0
fi
if [ "${1:-}" = "--list" ] || [ -z "${1:-}" ]; then
  echo "Sesiones (worktrees) activas:"
  git worktree list | sed 's/^/  /'
  [ -z "${1:-}" ] && { echo; echo "Uso: newsession.sh <tema> | --done <tema> | --list"; }
  exit 0
fi

TEMA="$(echo "$1" | tr ' ' '-' | tr -cd 'a-zA-Z0-9-_')"
DEST="$WROOT/$TEMA"
BR="session/$TEMA"

git fetch origin --quiet 2>/dev/null || true
mkdir -p "$WROOT"

if [ -d "$DEST" ]; then
  echo "Ya existe la sesión '$TEMA':"
else
  if git show-ref --verify --quiet "refs/heads/$BR"; then
    git worktree add "$DEST" "$BR"            # reusar rama existente
  else
    git worktree add "$DEST" -b "$BR" origin/main
  fi
  # gitignored que el worktree necesita: enlazar al checkout principal.
  for shared in .env data; do
    [ -e "$MAIN/$shared" ] && ln -sfn "$MAIN/$shared" "$DEST/$shared"
  done
  echo "✔ sesión '$TEMA' lista (rama $BR, basada en origin/main; .env y data/ enlazados)"
fi
echo
echo "  → Abrí una ventana de Claude AHÍ:    cd $DEST"
echo "  → Deployá tu cambio desde ahí:        bash scripts/wship.sh \"<mensaje>\""
echo "  → Cuando termines el tema:            bash scripts/newsession.sh --done $TEMA"
