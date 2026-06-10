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

# ── Tripwire (incidente 2026-06-10): 'data' JAMÁS debe trackearse. Un symlink
#    'data' se coló a git y borró la sessions.db de prod. Abortar si reaparece.
if [ -n "$(git ls-files data)" ] || [ -L data ]; then
  c_red "ABORT: 'data' está trackeado o es un symlink — NUNCA se commitea (borra la DB de prod)."
  c_red "  Arreglar:  [ -L data ] && rm data && mkdir -p data ; git rm -r --cached data 2>/dev/null || true"
  exit 1
fi
for f in "${MINE[@]}"; do
  case "$f" in data|data/*) c_red "ABORT: no se puede shippear bajo data/ ($f)."; exit 1;; esac
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
# Temp file con nombre impredecible y exclusivo (mktemp, O_EXCL) — evita symlink
# TOCTOU y que dos ventanas con ship.sh en paralelo se pisen el archivo de error.
ERR="$(mktemp "${TMPDIR:-/tmp}/ship_stash_err.XXXXXX")" || { c_red "mktemp falló"; exit 1; }
restore() {
  rm -f "$ERR"
  if [ "$STASHED" = "1" ]; then
    echo "→ restaurando WIP ajena…"
    git stash pop >/dev/null 2>&1 && c_grn "  WIP ajena restaurada." \
      || c_ylw "  ⚠️ pop con conflicto — tu WIP ajena está en 'git stash list' (stash@{0}), recuperable."
  fi
}
trap restore EXIT

if [ -n "$OTHERS" ]; then
  N=$(echo "$OTHERS" | wc -w | tr -d ' ')
  if [ "$N" -ge 8 ]; then
    c_ylw "⚠️  $N archivos de otras ventanas en vuelo. Bajo esta concurrencia conviene un"
    c_ylw "    worktree aislado (scripts/newsession.sh <tema>) — ver incidente de índice 2026-06-10."
  fi
  echo "→ aparto WIP ajena ($N arch.): $OTHERS"
  # Reintento ante carrera de índice con otra ventana ('could not write index').
  ok=0
  for try in 1 2 3 4 5; do
    if git stash push -q -- $OTHERS 2>"$ERR"; then ok=1; break; fi
    c_ylw "  índice ocupado por otra ventana (intento $try/5) — espero…"; sleep 2
  done
  if [ "$ok" != "1" ]; then
    c_red "ABORT: no pude apartar la WIP ajena (índice en disputa con otra ventana)."
    c_red "  NO commiteo, para no mezclar trabajo ajeno. Reintentá o usá un worktree (newsession.sh)."
    sed 's/^/    /' "$ERR" 2>/dev/null
    exit 1
  fi
  STASHED=1
fi

# 3. Commit SOLO lo mío.
git add -- "${MINE[@]}"
# ── Guardia anti-incidente #2 (2026-06-10): si una carrera de índice dejó staged
#    archivos que NO son tuyos, ABORTAR antes de commitear mezclado (fue justo lo
#    que pasó: 5 archivos ajenos se bundlearon bajo un commit equivocado).
for f in $(git diff --cached --name-only); do
  case "$MINE_STR" in
    *" $f "*) : ;;
    *) c_red "ABORT: '$f' quedó staged y NO es tuyo — índice contaminado por una carrera."
       c_red "  NO commiteo para no bundlear trabajo ajeno. Reintentá o usá un worktree."
       exit 1 ;;
  esac
done
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
