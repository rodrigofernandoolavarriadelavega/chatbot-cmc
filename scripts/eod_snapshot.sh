#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# eod_snapshot.sh — congela el árbol COMPLETO (tracked + untracked + staged) a
# una rama de respaldo, SIN tocar el working tree ni el índice. Las ventanas de
# Claude que estén editando en vivo no se enteran y no pierden nada.
#
# Por qué existe: se trabaja con muchas ventanas en paralelo sobre el MISMO
# checkout (~/chatbot-cmc). Eso es rápido pero deja WIP sin commitear que otra
# sesión puede pisar (git checkout / stash pop / reset). Este snapshot es la red:
# todo estado intermedio queda en un commit inmutable y recuperable.
#
# Cómo: un índice temporal descartable (GIT_INDEX_FILE) — git add solo LEE los
# archivos, así que poblar un índice falso captura el estado sin mover nada real.
#
# Los snapshots del día se ENCADENAN en la rama eod-backup/YYYY-MM-DD (cada corrida
# es un commit hijo del anterior), así queda el historial intra-día completo:
#   git log eod-backup/2026-06-07   → ve la evolución hora a hora
#   git checkout eod-backup/2026-06-07~3 -- <archivo>  → recupera una versión vieja
#
# Uso:   bash scripts/eod_snapshot.sh            (local, rápido, sin red)
#        PUSH=1 bash scripts/eod_snapshot.sh     (además empuja la rama del día a origin)
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail
cd "${CMC_DIR:-$HOME/chatbot-cmc}"

# Si no hay NADA pendiente (árbol limpio y == origin), no genera ruido.
if [ -z "$(git status --porcelain)" ]; then
  echo "$(date +%F_%H%M) · árbol limpio — sin snapshot."
  exit 0
fi

STAMP="$(date +%F_%H%M)"
DAY_BR="eod-backup/$(date +%F)"

# write-tree del estado actual usando un índice temporal (no toca el real).
TMPIDX="$(mktemp)"
trap 'rm -f "$TMPIDX"' EXIT
GIT_INDEX_FILE="$TMPIDX" git read-tree HEAD
GIT_INDEX_FILE="$TMPIDX" git add -A
TREE="$(GIT_INDEX_FILE="$TMPIDX" git write-tree)"

# Encadenar: parent = tip de la rama del día si existe, si no HEAD.
if git rev-parse --verify -q "$DAY_BR" >/dev/null; then
  PARENT="$(git rev-parse "$DAY_BR")"
else
  PARENT="$(git rev-parse HEAD)"
fi

# No duplicar si el árbol no cambió desde el último snapshot del día.
if [ "$(git rev-parse "$PARENT^{tree}" 2>/dev/null || true)" = "$TREE" ]; then
  echo "$STAMP · sin cambios desde el último snapshot — omito."
  exit 0
fi

SNAP="$(git commit-tree "$TREE" -p "$PARENT" -m "eod-snapshot $STAMP")"
git branch -f "$DAY_BR" "$SNAP"
echo "$STAMP · snapshot ${SNAP:0:10} → $DAY_BR ($(git rev-list --count "$DAY_BR" ^HEAD) en el día)"

if [ "${PUSH:-0}" = "1" ]; then
  git push -q origin "$DAY_BR" 2>/dev/null && echo "         empujado a origin/$DAY_BR" || echo "         (push falló — backup local OK igual)"
fi
