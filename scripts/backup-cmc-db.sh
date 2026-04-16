#!/bin/bash
# Backup online de sessions.db encriptada con SQLCipher.
# Usa sqlcipher_export() (el .backup tradicional no soporta DBs encriptadas).
# El backup resultante queda encriptado con la MISMA key.
# Retención: últimos 8 backups (~2 meses con cron semanal).
set -euo pipefail

SRC=/opt/chatbot-cmc/data/sessions.db
DST_DIR=/opt/backups/chatbot-cmc
TS=$(date +%Y%m%d_%H%M%S)
DST=${DST_DIR}/sessions_${TS}.db

mkdir -p "${DST_DIR}"
chmod 700 "${DST_DIR}"

if [ ! -f "${SRC}" ]; then
    echo "[$(date -Iseconds)] ERROR: no existe ${SRC}" >&2
    exit 1
fi

# Cargar SQLCIPHER_KEY desde .env
KEY=$(grep -E '^SQLCIPHER_KEY=' /opt/chatbot-cmc/.env | head -1 | cut -d= -f2- | tr -d "'\"")
if [ -z "${KEY}" ]; then
    echo "[$(date -Iseconds)] ERROR: SQLCIPHER_KEY vacío en .env" >&2
    exit 1
fi

# Borrar .db previo por si quedó de una corrida fallida
rm -f "${DST}"

# sqlcipher_export: abre la DB encriptada, attach target nuevo con misma key,
# copia schema+datos al target (todo dentro del engine SQLCipher, online-safe).
sqlcipher "${SRC}" <<EOF >/dev/null
PRAGMA key = "x'${KEY}'";
ATTACH DATABASE '${DST}' AS backup KEY "x'${KEY}'";
SELECT sqlcipher_export('backup');
DETACH DATABASE backup;
EOF

# Smoke test: verificar que la copia se abre con la key.
# Extraer solo el número (el PRAGMA escribe "ok" en stdout).
ROWS=$(sqlcipher "${DST}" "PRAGMA key = \"x'${KEY}'\"; SELECT COUNT(*) FROM sessions;" 2>/dev/null | tail -1 | tr -dc '0-9' || true)
if [ -z "${ROWS}" ]; then
    echo "[$(date -Iseconds)] ERROR: backup corrupto (no se lee con la key)" >&2
    rm -f "${DST}"
    exit 1
fi

gzip -f "${DST}"
chmod 600 "${DST}.gz"

# Purga: conservar solo los 8 más recientes
ls -1t "${DST_DIR}"/sessions_*.db.gz 2>/dev/null | tail -n +9 | xargs -r rm -f

SIZE=$(du -h "${DST}.gz" | cut -f1)
echo "[$(date -Iseconds)] OK: ${DST}.gz (${SIZE}, ${ROWS} sessions)"
