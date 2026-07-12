#!/usr/bin/env bash
# healthcheck.sh — Watchdog HORARIO del chatbot CMC (cmc-healthcheck.service/.timer).
#
# HISTORIA: el original vivía en /opt/chatbot-cmc/scripts/healthcheck.sh SIN estar
# versionado en git. El 2026-06-10 (incidente deadlock GIL×SQLite) una limpieza del
# árbol de prod borró los archivos no rastreados y se lo llevó. El timer siguió
# disparando cada hora contra un archivo inexistente (203/EXEC) durante ~1 mes y
# nadie se enteró. Por eso ahora vive FUERA del repo, en /opt/cmc-ops/ — igual que
# health_watchdog.sh — donde ningún git checkout/clean lo puede borrar.
#
# DIVISIÓN DE TRABAJO (no duplicar reinicios):
#   /opt/cmc-ops/health_watchdog.sh  (cron 1 min) → caída DURA: /health no responde.
#                                     Reinicia rápido (2 fallos, cooldown 5 min).
#   este script                      (timer 1 h)  → red de seguridad + degradación
#                                     LENTA (circuit breaker pegado, servicio muerto
#                                     sin que el cron lo levantara) + alerta al dueño.
#
# CONSERVADOR A PROPÓSITO (ver bug histórico del watchdog con histéresis de "zona
# muerta" que mató un blast):  sin zonas muertas, sin heurísticas de carga.
#   - 3 reintentos espaciados antes de declarar caída (no reinicia por un timeout).
#   - No reinicia si el servicio arrancó hace <10 min (el cron acaba de actuar).
#   - Circuit breaker pegado: exige 2 corridas consecutivas (≥1 h sostenida).
#   - Si el bot responde 200, NUNCA reinicia.
set -u

URL="http://127.0.0.1:8001/health"
SVC="chatbot-cmc"
LOG="/var/log/cmc-healthcheck.log"
ALERTLOG="/var/log/cmc-watchdog-alerts.log"
STATE="/var/lib/cmc-ops/healthcheck_state"   # runs consecutivas con breaker pegado
FAILED_STATE="/var/lib/cmc-ops/failed_units" # "epoch|unidad1 unidad2 ..." ya alertadas
ENVFILE="/opt/chatbot-cmc/.env"

TIMEOUT=10
RETRIES=3
RETRY_SLEEP=15
MIN_UPTIME_SECS=600      # no reiniciar si arrancó hace menos de esto
BREAKER_RUNS=2           # corridas consecutivas con state=down antes de actuar
REALERT_SECS=86400       # si una unidad sigue caída, re-avisar 1×/día (no cada hora)

mkdir -p "$(dirname "$STATE")"
ts() { date -u '+%Y-%m-%dT%H:%M:%SZ'; }
log() { echo "$(ts) $*" >> "$LOG"; }

alert() {
  local msg="$1"
  echo "$(ts) [healthcheck] $msg" >> "$ALERTLOG"
  log "ALERT: $msg"
  # WhatsApp al dueño (best-effort; nunca hace fallar el script).
  # OJO: se leen las claves del .env con grep, NO con `source` (el .env tiene
  # valores con espacios/tildes y al sourcearlo tiraba "command not found").
  local tok pid phone
  tok=$(grep -m1 '^META_ACCESS_TOKEN=' "$ENVFILE" 2>/dev/null | cut -d= -f2- | tr -d "\"'")
  pid=$(grep -m1 '^META_PHONE_NUMBER_ID=' "$ENVFILE" 2>/dev/null | cut -d= -f2- | tr -d "\"'")
  phone=$(grep -m1 '^ADMIN_ALERT_PHONE=' "$ENVFILE" 2>/dev/null | cut -d= -f2- | tr -d "\"'")
  if [ -z "${tok:-}" ] || [ -z "${pid:-}" ] || [ -z "${phone:-}" ]; then
    return 0
  fi
  curl -s -m 12 -o /dev/null \
    -X POST "https://graph.facebook.com/v21.0/${pid}/messages" \
    -H "Authorization: Bearer ${tok}" -H "Content-Type: application/json" \
    -d "{\"messaging_product\":\"whatsapp\",\"to\":\"${phone}\",\"type\":\"text\",\"text\":{\"body\":\"[CMC watchdog] ${msg}\"}}" || true
}

svc_uptime_secs() {
  local t epoch now
  t=$(systemctl show "$SVC" -p ActiveEnterTimestamp --value 2>/dev/null)
  [ -z "$t" ] && { echo 999999; return; }
  epoch=$(date -d "$t" +%s 2>/dev/null || echo 0)
  now=$(date +%s)
  [ "$epoch" -eq 0 ] && { echo 999999; return; }
  echo $(( now - epoch ))
}

# Reinicia sólo si de verdad corresponde; respeta el arranque reciente del cron.
restart_guarded() {
  local reason="$1" up
  up=$(svc_uptime_secs)
  if [ "$up" -lt "$MIN_UPTIME_SECS" ]; then
    log "$reason — pero el servicio arrancó hace ${up}s (<${MIN_UPTIME_SECS}s): NO reinicio (lo cubre health_watchdog)"
    alert "$reason (NO reinicié: arranque reciente hace ${up}s, ya actuó el watchdog de 1 min)"
    return 0
  fi
  log "$reason — systemctl restart $SVC"
  systemctl restart "$SVC"
  sleep 10
  if curl -fs -m "$TIMEOUT" -o /dev/null "$URL"; then
    alert "$reason. Reiniciado OK, /health responde de nuevo."
  else
    alert "$reason. Reinicié y /health SIGUE sin responder — revisar a mano."
  fi
}

# ── 0) ¿Quién vigila a los vigilantes? ───────────────────────────────────────
# El propio cmc-healthcheck estuvo en `failed` 31 días (10-jun → 11-jul) sin que
# nadie se enterara: un servicio muerto no grita. Acá revisamos TODAS las unidades
# systemd y avisamos. Corre PRIMERO, porque los chequeos de abajo hacen `exit 0`.
#
# Anti-spam: solo alerta cuando el CONJUNTO de unidades caídas cambia (aparece una
# nueva). Si sigue igual, re-avisa 1×/día — ni silencio ni ruido horario.
check_failed_units() {
  local failed now prev last_ts
  failed=$(systemctl list-units --state=failed --no-legend --plain 2>/dev/null \
             | awk '{print $1}' | sort | tr '\n' ' ' | sed 's/ *$//')

  if [ -z "$failed" ]; then
    if [ -f "$FAILED_STATE" ]; then
      prev=$(cut -d'|' -f2- "$FAILED_STATE" 2>/dev/null)
      alert "Servicios recuperados, ya no hay unidades en failed (estaban: ${prev})"
      rm -f "$FAILED_STATE"
    fi
    return 0
  fi

  now=$(date +%s)
  prev=""; last_ts=0
  if [ -f "$FAILED_STATE" ]; then
    last_ts=$(cut -d'|' -f1 "$FAILED_STATE" 2>/dev/null || echo 0)
    prev=$(cut -d'|' -f2- "$FAILED_STATE" 2>/dev/null)
  fi

  if [ "$failed" != "$prev" ] || [ $(( now - last_ts )) -ge "$REALERT_SECS" ]; then
    alert "Servicios systemd en FAILED: ${failed}"
    echo "${now}|${failed}" > "$FAILED_STATE"
  else
    log "unidades en failed (ya alertado, sin cambios): ${failed}"
  fi
}
check_failed_units

# ── 1) ¿Responde /health? con reintentos ─────────────────────────────────────
body=""
ok=0
for i in $(seq 1 "$RETRIES"); do
  if body=$(curl -fs -m "$TIMEOUT" "$URL" 2>/dev/null); then ok=1; break; fi
  log "intento $i/$RETRIES: /health no responde (timeout ${TIMEOUT}s)"
  [ "$i" -lt "$RETRIES" ] && sleep "$RETRY_SLEEP"
done

if [ "$ok" -eq 0 ]; then
  rm -f "$STATE"
  active=$(systemctl is-active "$SVC" 2>/dev/null || true)
  restart_guarded "/health no responde tras $RETRIES intentos (servicio=$active)"
  exit 0
fi

# ── 2) Responde 200: NUNCA reiniciamos por métricas de carga ──────────────────
mstate=$(echo "$body" | grep -o '"medilink_state":"[^"]*"' | cut -d'"' -f4)
cstate=$(echo "$body" | grep -o '"claude_state":"[^"]*"' | cut -d'"' -f4)
mms=$(echo "$body"   | grep -o '"medilink_ms":[0-9]*'    | cut -d: -f2)
queue=$(echo "$body" | grep -o '"intent_queue_depth":[0-9]*' | cut -d: -f2)

if [ "${mstate:-up}" = "down" ]; then
  runs=$(( $(cat "$STATE" 2>/dev/null || echo 0) + 1 ))
  echo "$runs" > "$STATE"
  if [ "$runs" -lt "$BREAKER_RUNS" ]; then
    log "activo pero medilink_state=down (corrida $runs/$BREAKER_RUNS) — vigilando, sin actuar"
    exit 0
  fi
  # Breaker pegado ≥1 h con el bot respondiendo: degradación sostenida y clara.
  restart_guarded "circuit breaker Medilink pegado (state=down $runs corridas seguidas, ~${runs}h)"
  rm -f "$STATE"
  exit 0
fi

rm -f "$STATE"
log "activo - Medilink OK (${mms:-?}ms) - claude=${cstate:-?} - cola:${queue:-?}"
exit 0
