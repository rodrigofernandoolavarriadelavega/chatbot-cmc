#!/usr/bin/env bash
# Regression gate para el sprint de robustez de intención.
# Corre las suites sensibles y compara contra los techos de baseline.
# Baseline (2026-05-31, antes del sprint):
#   test_normalizer            : 52/52  (techo de fallos = 0)
#   test_regression_real_cases : 2 fail (pediatría, ajenos)  (techo = 2)
#   harness_friction_500       : 19 fail (traumato/quickbook) (techo = 19)
set -u
cd "$(dirname "$0")/.." || exit 2
P="venv/bin/python"
export PYTHONPATH="app:."
fail=0

run() { # name cmd techo_de_fallos parser
  local name="$1"; shift
  echo "── $name ──"
}

# normalizer: exige 0 fallos
$P tests/test_normalizer.py >/tmp/g_norm.log 2>&1
nf=$(grep -oE '[0-9]+ failed' /tmp/g_norm.log | grep -oE '^[0-9]+' | tail -1); nf=${nf:-0}
echo "normalizer: $nf failed (techo 0)"; [ "$nf" -gt 0 ] && { echo "  REGRESION"; fail=1; }

# regression_real_cases: techo 2
$P tests/test_regression_real_cases.py >/tmp/g_reg.log 2>&1
rf=$(grep -oE 'failures=[0-9]+' /tmp/g_reg.log | grep -oE '[0-9]+' | tail -1); rf=${rf:-0}
echo "regression_real_cases: $rf failed (techo 2)"; [ "$rf" -gt 2 ] && { echo "  REGRESION"; fail=1; }

# friction_500: techo 19
$P tests/harness_friction_500.py >/tmp/g_fr.log 2>&1
ff=$(grep -oE '[0-9]+ failed' /tmp/g_fr.log | grep -oE '^[0-9]+' | tail -1); ff=${ff:-0}
echo "friction_500: $ff failed (techo 19)"; [ "$ff" -gt 19 ] && { echo "  REGRESION"; fail=1; }

if [ "$fail" -eq 0 ]; then echo "GATE: OK (sin regresiones)"; else echo "GATE: FALLA"; fi
exit $fail
