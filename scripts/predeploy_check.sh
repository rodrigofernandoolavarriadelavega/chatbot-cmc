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

echo "==> Pre-deploy OK"
