#!/bin/bash
# Refresh mensual del heatmap CMC: baja días nuevos de citas de Medilink al
# heatmap_cache.db y re-geocodifica direcciones nuevas.
#
# Por qué existe: nada re-descargaba citas al heatmap → se envejecía solo, y el
# incidente del symlink data/ (2026-06-10) lo dejó vacío sin que nadie lo notara
# hasta 3 semanas después. Este cron lo mantiene fresco y sirve de canario.
#
# Seguro para prod: el download es INCREMENTAL (salta días ya bajados vía la tabla
# dias_descargados), tiene retry ante 429 (comparte Medilink con el bot), y está
# throttled a ~40 req/min. Corre off-peak por cron (/etc/cron.d/chatbot-cmc-heatmap).
#
# Uso: bash scripts/heatmap_refresh.sh   (o vía cron)
set -euo pipefail
cd /opt/chatbot-cmc

Y=$(date +%Y)
M=$(date +%-m)
PY=$(date -d "1 month ago" +%Y)
PM=$(date -d "1 month ago" +%-m)

export PYTHONPATH=app

# Mes anterior (cierra días que se agregaron al final) + mes actual.
venv/bin/python scripts/heatmap_comunas.py download "${PY}" "${PM}"
venv/bin/python scripts/heatmap_comunas.py download "${Y}" "${M}"
venv/bin/python scripts/geocode_direcciones.py

echo "[$(date -Iseconds)] heatmap refresh OK (${PY}-${PM}, ${Y}-${M})"
