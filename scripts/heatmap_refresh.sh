#!/bin/bash
# Refresh del heatmap CMC desde el BI (Postgres en Docker) + geocoding.
#
# El BI ya es el espejo nocturno de Medilink (ETL 04:30), así que poblar el
# heatmap desde ahí es instantáneo y NO toca Medilink. Antes esto re-bajaba día
# por día de Medilink (~1h); ahora son dos queries SQL. Por eso corre DIARIO
# (05:00, después del ETL) en vez de mensual — mantiene el mapa siempre fresco.
#
# El geocoding solo procesa direcciones NUEVAS (geocode_cache persiste), así que
# el hit a Nominatim es marginal día a día.
#
# Uso: bash scripts/heatmap_refresh.sh   (o vía /etc/cron.d/chatbot-cmc-heatmap)
set -euo pipefail
cd /opt/chatbot-cmc
export PYTHONPATH=app

venv/bin/python scripts/heatmap_from_bi.py
venv/bin/python scripts/geocode_direcciones.py

echo "[$(date -Iseconds)] heatmap refresh (BI) OK"
