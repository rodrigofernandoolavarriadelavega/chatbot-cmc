#!/usr/bin/env bash
# health_watchdog.sh — Reinicia chatbot-cmc si /health deja de responder.
#
# POR QUÉ EXISTE: el deadlock GIL×SQLite (incidente 2026-06-10) deja el proceso
# "active" pero colgado: no responde y Restart=always NO lo cubre (solo reinicia
# si el proceso SALE, no si cuelga vivo). Este watchdog detecta el cuelgue
# (timeout de /health) y reinicia, acotando la caída a ~1-2 min.
#
# Instalación (FUERA del repo para que git no lo toque):
#   /opt/cmc-ops/health_watchdog.sh  + cron cada minuto.
#
# Anti-falsos-positivos: exige 2 fallos consecutivos antes de reiniciar.
# Anti-tormenta: tras reiniciar, no vuelve a reiniciar por 5 min (cooldown).
set -u

URL="http://127.0.0.1:8001/health"
SVC="chatbot-cmc"
STAMP="/tmp/cmc_health_fails"
COOLDOWN="/tmp/cmc_health_restart_cooldown"
LOG="/var/log/cmc-health-watchdog.log"
TIMEOUT=8
THRESHOLD=2          # fallos consecutivos para reiniciar
COOLDOWN_SECS=300    # no reiniciar de nuevo dentro de 5 min

ts() { date '+%Y-%m-%d %H:%M:%S'; }

if curl -fs -m "$TIMEOUT" -o /dev/null "$URL"; then
  # Sano: limpia el contador de fallos.
  [ -f "$STAMP" ] && rm -f "$STAMP"
  exit 0
fi

# /health no respondió.
fails=$(( $(cat "$STAMP" 2>/dev/null || echo 0) + 1 ))
echo "$fails" > "$STAMP"
echo "$(ts) health FAIL consecutivo #$fails (timeout ${TIMEOUT}s)" >> "$LOG"

[ "$fails" -lt "$THRESHOLD" ] && exit 0

# Cooldown: ¿reiniciamos hace poco?
now=$(date +%s)
last=$(cat "$COOLDOWN" 2>/dev/null || echo 0)
if [ $(( now - last )) -lt "$COOLDOWN_SECS" ]; then
  echo "$(ts) en cooldown (último restart hace $(( now - last ))s) — no reinicio" >> "$LOG"
  exit 0
fi

echo "$(ts) >=${THRESHOLD} fallos → systemctl restart $SVC" >> "$LOG"
echo "$now" > "$COOLDOWN"
systemctl restart "$SVC"
rm -f "$STAMP"
sleep 6
if curl -fs -m "$TIMEOUT" -o /dev/null "$URL"; then
  echo "$(ts) restart OK — /health responde de nuevo" >> "$LOG"
else
  echo "$(ts) restart NO recuperó /health — revisar manualmente" >> "$LOG"
fi
