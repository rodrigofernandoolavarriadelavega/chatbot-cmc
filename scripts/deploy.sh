#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Deploy SEGURO del chatbot CMC — el ÚNICO camino sancionado a producción.
#
# Reemplaza el "git pull && systemctl restart" naive que el 2026-06-07 casi
# destruye trabajo de prod sin commitear (ver memory cmc_prod_divergencia).
#
# Cuatro guardas, todas con mensaje claro y salida ≠0 si fallan:
#   G1  Árbol de prod LIMPIO  → si hay trabajo sin commitear, ABORTA (no lo borra).
#   G2  Fast-forward posible  → si prod divergió de origin/main, ABORTA (hay que
#                               reconciliar, no pull — un pull a la fuerza rompe).
#   G3  Deep-import OK         → importa flows/main/claude_helper/jobs ANTES de
#                               reiniciar (el ast.parse NO detecta NameError).
#   G4  /health 200 + active   → si tras el restart no está sano, AUTO-ROLLBACK al
#                               commit anterior y reinicia (vuelve al estado bueno).
#
# Regla de oro: el VPS es DESTINO de deploy, nunca de edición. Todo cambio nace
# en local → push → este script. Nunca `git commit`/edición directa en el server.
#
# Uso:   bash scripts/deploy.sh
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

VPS="${CMC_VPS:-root@157.245.13.107}"
DIR="${CMC_DIR:-/opt/chatbot-cmc}"
HEALTH_URL="${CMC_HEALTH:-https://agentecmc.cl/health}"

c_red()  { printf '\033[31m%s\033[0m\n' "$*"; }
c_grn()  { printf '\033[32m%s\033[0m\n' "$*"; }
c_ylw()  { printf '\033[33m%s\033[0m\n' "$*"; }

echo "── Deploy seguro CMC ──────────────────────────────────────────"

# ── Pre-flight LOCAL ─────────────────────────────────────────────────────────
if [ -n "$(git status -s --untracked-files=no)" ]; then
  c_red "ABORT (local): tienes cambios sin commitear. Commitea (paths explícitos) y reintenta."
  git status -s --untracked-files=no
  exit 1
fi
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
if [ "$BRANCH" != "main" ]; then
  c_ylw "Aviso: estás en '$BRANCH', no en main. Continúo, pero el deploy usa origin/main."
fi
echo "→ push a origin/main…"
git push origin "$BRANCH":main

# ── Deploy REMOTO con guardas ────────────────────────────────────────────────
# set +e para capturar el código de salida del bloque remoto sin que set -e mate
# el script antes de imprimir el resumen / decidir el rollback.
set +e
ssh "$VPS" "DIR='$DIR' HEALTH_URL='$HEALTH_URL' bash -s" <<'REMOTE'
set -euo pipefail
cd "$DIR"

# G1 — árbol limpio (no tracked-modificados). Untracked (imágenes, venv) no bloquean.
if [ -n "$(git status -s --untracked-files=no)" ]; then
  echo "ABORT (G1): el árbol de PROD tiene cambios sin commitear — un pull los borraría."
  echo "Captúralos primero (git add <paths> && git commit) o respáldalos. NO se deploya."
  git status -s --untracked-files=no
  exit 2
fi

git fetch origin --quiet
LOCAL="$(git rev-parse HEAD)"
TARGET="$(git rev-parse origin/main)"
BASE="$(git merge-base HEAD origin/main)"

if [ "$LOCAL" = "$TARGET" ]; then
  echo "OK: prod ya está en origin/main ($(git rev-parse --short HEAD)). Nada que deployar."
  exit 0
fi

# G2 — debe ser fast-forward (prod no divergió).
if [ "$BASE" != "$LOCAL" ]; then
  echo "ABORT (G2): prod DIVERGIÓ de origin/main (no es fast-forward)."
  echo "Hay commits locales en el VPS que origin no tiene → reconciliar a mano (worktree),"
  echo "no forzar el pull. Ver dashboard plan_reconciliacion_cmc.html."
  exit 3
fi

OLD="$LOCAL"
echo "→ pull --ff-only  ($(git rev-parse --short $LOCAL) → $(git rev-parse --short $TARGET))"
git pull --ff-only --quiet

# G3 — deep-import (lo que el ast.parse no ve: NameError de imports faltantes).
echo "→ deep-import…"
if ! venv/bin/python -c "import sys; sys.path.insert(0,'app'); \
from dotenv import load_dotenv; load_dotenv('.env'); \
import flows, main, claude_helper, jobs, messaging, session" 2>&1; then
  echo "ABORT (G3): deep-import FALLÓ → rollback a $(git rev-parse --short $OLD) (sin reiniciar)."
  git reset --hard "$OLD" --quiet
  exit 4
fi

echo "→ restart…"
systemctl restart chatbot-cmc

# G4 — health + auto-rollback. PACIENTE: el app tarda en bootear (templates + pool BI),
# así que reintentamos /health hasta ~36s antes de declarar fracaso (evita falsos
# negativos por timing que dispararían un rollback innecesario).
HC=""
for i in 1 2 3 4 5 6 7 8 9 10 11 12; do
  sleep 3
  HC="$(curl -s -o /dev/null -w '%{http_code}' --max-time 12 "$HEALTH_URL" || true)"
  [ "$HC" = "200" ] && break
done
ACT="$(systemctl is-active chatbot-cmc || true)"
if [ "$ACT" = "active" ] && [ "$HC" = "200" ]; then
  echo "DEPLOY OK: prod en $(git rev-parse --short HEAD) · servicio=$ACT · health=$HC"
else
  echo "ABORT (G4): post-deploy NO sano (servicio=$ACT health=$HC) → AUTO-ROLLBACK."
  git reset --hard "$OLD" --quiet
  systemctl restart chatbot-cmc
  HC2=""
  for i in 1 2 3 4 5 6 7 8; do sleep 3; HC2="$(curl -s -o /dev/null -w '%{http_code}' --max-time 12 "$HEALTH_URL" || true)"; [ "$HC2" = "200" ] && break; done
  echo "Rollback a $(git rev-parse --short HEAD) · health=$HC2"
  exit 5
fi
REMOTE

rc=$?
set -e
echo "───────────────────────────────────────────────────────────────"
if [ $rc -eq 0 ]; then c_grn "✔ Deploy completado y verificado."; else c_red "✘ Deploy abortado (código $rc) — prod quedó en su estado seguro."; fi
exit $rc
