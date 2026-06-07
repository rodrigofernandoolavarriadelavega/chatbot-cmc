#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# wship.sh — deploya desde DENTRO de un worktree de sesión (rama session/*).
# Como el worktree está aislado, no hace falta el baile de stash de ship.sh:
# acá el árbol es 100% tuyo. Solo hay que rebasar sobre lo último de origin/main
# (otra sesión pudo haber deployado mientras tanto) para que el push a main sea
# fast-forward, y deployar con las guardas de siempre.
#
# Uso:   bash scripts/wship.sh "feat(seo): X"            # commitea TODO el worktree
#        bash scripts/wship.sh "feat(seo): X" a.py b.py  # commitea solo esos
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail
ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"
BR="$(git rev-parse --abbrev-ref HEAD)"
SCRIPTS="${CMC_DIR:-$HOME/chatbot-cmc}/scripts"

case "$BR" in
  session/*) : ;;
  *) echo "⚠️  No estás en una rama session/* (estás en '$BR'). Si querés deploy normal usá ship.sh/deploy.sh."; exit 1 ;;
esac

MSG="${1:-}"; shift || true

# 1. Commit.
if [ -n "$MSG" ]; then
  if [ $# -gt 0 ]; then git add -- "$@"; else git add -A; fi
  if git diff --cached --quiet; then
    echo "Nada que commitear."
  else
    git commit -q -m "$MSG

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
    echo "  commit: $(git rev-parse --short HEAD) $MSG"
  fi
fi

# 2. Rebase sobre origin/main (para que main sea fast-forward).
git fetch origin --quiet
echo "→ rebase sobre origin/main…"
if ! git rebase origin/main; then
  echo "✘ rebase con conflicto. Resolvé (git status), luego: git rebase --continue && bash scripts/wship.sh"
  exit 1
fi

# 3. Deep-import local (G3 antes de tocar prod).
echo "→ deep-import…"
if ! python3 -c "import sys; sys.path.insert(0,'app'); from dotenv import load_dotenv; load_dotenv('.env'); \
import flows, main, claude_helper, jobs, messaging, session" 2>&1; then
  echo "✘ deep-import falló — no deployo."; exit 1
fi

# 4. Deploy guardado (deploy.sh pushea esta rama a main; tras el rebase es ff).
echo "→ deploy…"
bash "$SCRIPTS/deploy.sh"
