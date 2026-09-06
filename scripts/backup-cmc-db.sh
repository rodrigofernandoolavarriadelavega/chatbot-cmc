#!/bin/bash
# Backup online de sessions.db encriptada con SQLCipher.
# Usa sqlcipher_export() (el .backup tradicional no soporta DBs encriptadas).
# El backup resultante queda encriptado con la MISMA key.
# Retención: escalonada (7 diarios + 4 semanales + 3 mensuales), en
# scripts/purgar-respaldos.py. El cron corre TODOS LOS DÍAS, no semanal:
# la purga vieja conservaba 30 copias diarias y llegó a ocupar 8,3 GB.
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


SIZE=$(du -h "${DST}.gz" | cut -f1)
echo "[$(date -Iseconds)] OK: ${DST}.gz (${SIZE}, ${ROWS} sessions)"

# ── Backup de archivos subidos por pacientes (data/uploads) ───────────────────
# Añadido 2026-06-11: el incidente del symlink data/ (2026-06-10) borró uploads y
# NO había backup → imágenes/PDFs de pacientes previos se perdieron. Esto lo
# previene. Los uploads son chicos (~MB), tar.gz es barato.
UPLOADS_SRC=/opt/chatbot-cmc/data/uploads
if [ -d "${UPLOADS_SRC}" ]; then
    UPDST=${DST_DIR}/uploads_${TS}.tar.gz
    tar -czf "${UPDST}" -C /opt/chatbot-cmc/data uploads 2>/dev/null && chmod 600 "${UPDST}"
    USIZE=$(du -h "${UPDST}" 2>/dev/null | cut -f1)
    UFILES=$(find "${UPLOADS_SRC}" -type f 2>/dev/null | wc -l)
    echo "[$(date -Iseconds)] OK uploads: ${UPDST} (${USIZE}, ${UFILES} archivos)"
else
    echo "[$(date -Iseconds)] WARN: no existe ${UPLOADS_SRC}, skip uploads backup" >&2
fi

# ── Backup de heatmap_cache.db (geo-agregados, plaintext) ─────────────────────
# Añadido 2026-07-02: el incidente del symlink data/ (2026-06-10) borró las tablas
# del heatmap (citas_heatmap/pacientes_heatmap/geocode_cache) y NO había backup →
# hubo que reconstruir desde Medilink (~1h). Esto lo previene. Es plaintext (solo
# agregados geográficos, sin PII sensible), .backup online-safe + gzip.
HM_SRC=/opt/chatbot-cmc/data/heatmap_cache.db
if [ -f "${HM_SRC}" ]; then
    HMDST=${DST_DIR}/heatmap_cache_${TS}.db
    if sqlite3 "${HM_SRC}" ".backup '${HMDST}'" 2>/dev/null; then
        gzip -f "${HMDST}"
        chmod 600 "${HMDST}.gz"
        HMSIZE=$(du -h "${HMDST}.gz" 2>/dev/null | cut -f1)
        echo "[$(date -Iseconds)] OK heatmap: ${HMDST}.gz (${HMSIZE})"
    else
        echo "[$(date -Iseconds)] ERROR: falló .backup de ${HM_SRC}" >&2
    fi
else
    echo "[$(date -Iseconds)] WARN: no existe ${HM_SRC}, skip heatmap backup" >&2
fi

# ── Purga escalonada ─────────────────────────────────────────────────────────
# Al final y una sola vez para las tres familias. Antes cada bloque purgaba lo
# suyo con `tail -n +31`, que (a) conservaba 30 copias diarias —un mes de
# alcance por 8,3 GB— y (b) sólo miraba archivos .gz, así que los respaldos que
# quedaban a medias cuando el proceso se cortaba no los borraba nadie nunca.
python3 /opt/chatbot-cmc/scripts/purgar-respaldos.py \
    || echo "[$(date -Iseconds)] WARN: la purga de respaldos falló" >&2

# ── Copia FUERA del servidor ─────────────────────────────────────────────────
# Va al final, después de la purga local: primero se decide qué se conserva acá
# y recién entonces se sincroniza afuera. Un respaldo en el mismo disco que la
# base protege contra un DELETE mal escrito, no contra perder el droplet.
# El Space guarda MÁS historia que el disco local (30+12+12 contra 7+4+3):
# acá el espacio es el problema, allá sobran 250 GB ya pagados.
python3 /opt/chatbot-cmc/scripts/subir-respaldos.py \
    || echo "[$(date -Iseconds)] ERROR: la subida a Spaces falló — el respaldo de hoy sólo existe en este disco" >&2
