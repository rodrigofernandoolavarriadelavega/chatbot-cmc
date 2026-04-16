#!/bin/bash
# Genera PDFs A4 imprimibles de los 10 lead magnets desde los HTML.
# Usa Google Chrome en modo headless (ya viene instalado en macOS).
#
# Uso: bash build_pdfs.sh
# Salida: docs/lead_magnets/pdfs/*.pdf

set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
HTML_DIR="${DIR}/html"
PDF_DIR="${DIR}/pdfs"

mkdir -p "$PDF_DIR"

docs=(
  00_guia_mitos_realidades_arauco
  01_checklist_preconsulta
  02_calendario_vacunas_pni
  03_directorio_emergencias_arauco
  04_glosario_fonasa_ges
  05_antecedentes_familiares
  06_preparacion_examenes
  07_guia_prequirurgica
  08_calculadora_imc
  09_diario_postoperatorio
  10_cuando_llamar_131
)

for f in "${docs[@]}"; do
  "$CHROME" \
    --headless=new \
    --disable-gpu \
    --no-pdf-header-footer \
    --print-to-pdf-no-header \
    --virtual-time-budget=2000 \
    --print-to-pdf="${PDF_DIR}/${f}.pdf" \
    "file://${HTML_DIR}/${f}.html" 2>/dev/null
  printf "✓ %s.pdf\n" "$f"
done

echo ""
echo "PDFs generados en: $PDF_DIR"
ls -lh "$PDF_DIR" | tail -n +2
