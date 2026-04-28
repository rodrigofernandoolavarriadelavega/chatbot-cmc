#!/usr/bin/env bash
# Pre-deploy validation. Runs before pushing to prod to catch errors that
# `python3 -m py_compile` / `ast.parse` miss (NameError, missing imports,
# circular dependencies). Detects regressions like the 2026-04-27 bot
# crash where `re` wasn't imported at module level in messaging.py.

set -e

cd "$(dirname "$0")/.."

echo "==> AST parse"
python3 -c "
import ast, pathlib
for f in pathlib.Path('app').glob('*.py'):
    ast.parse(f.read_text())
    print(f'  ok {f.name}')
"

echo "==> Import every module (catches NameError / missing imports)"
python3 -c "
import sys, importlib
sys.path.insert(0, 'app')
for m in ['messaging','flows','claude_helper','medilink','jobs','session',
         'fidelizacion','reminders','doctor_alerts','pni','autocuidado',
         'resilience','config','admin_routes']:
    importlib.import_module(m)
    print(f'  ok {m}')
"

echo "==> Adversarial chat tests (53 conversaciones, 100% pass requerido)"
python3 scripts/adversarial_chat.py 2>&1 | tail -3 | head -2
if ! python3 scripts/adversarial_chat.py >/dev/null 2>&1; then
    echo "  ✗ Adversarial tests fallaron — abortando deploy"
    exit 1
fi
echo "  ok adversarial: 100%"

echo "==> Pre-deploy OK"
