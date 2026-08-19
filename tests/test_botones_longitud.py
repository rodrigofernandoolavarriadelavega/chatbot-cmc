"""Regresión: títulos de botones/filas de WhatsApp no deben exceder el límite
de la plataforma.

WhatsApp Cloud API trunca SIN AVISO (no hay error, no hay warning en el
webhook) los títulos de:
  - Reply buttons (`_btn_msg` → `interactive.action.buttons[].reply.title`): 20 caracteres.
  - List rows (`_list_msg` → `sections[].rows[].title`): 24 caracteres.

Auditoría 2026-08-19 (#15): 'Agendar con Olavarrí', '👤 Es para otra perso',
'✏️ Mis datos (RUT/no', 'Sí, mostrar Medicina General', etc. — títulos
cortados a mitad de palabra, visibles al paciente.

Solo valida literales (`ast.Constant`) dentro de llamadas a `_btn_msg`/
`_list_msg` en `app/flows.py` — los títulos armados dinámicamente (f-strings
con nombre de profesional, etc.) ya tienen su propio guard de longitud en el
código (ver `_btn_agendar_title` en `flows.py`) y no son estáticamente
verificables acá.

Ejecución:
    python3 tests/test_botones_longitud.py
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FLOWS_PY = ROOT / "app" / "flows.py"

LIMITE_BOTON = 20
LIMITE_ROW = 24


def _literal_titles(call: ast.Call) -> list[str]:
    """Extrae todos los strings literales bajo la clave 'title' dentro de un
    ast.Call (recorre listas/dicts anidados en los argumentos)."""
    titles: list[str] = []

    def _walk(node):
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if (isinstance(k, ast.Constant) and k.value == "title"
                        and isinstance(v, ast.Constant) and isinstance(v.value, str)):
                    titles.append(v.value)
        for child in ast.iter_child_nodes(node):
            _walk(child)

    for arg in call.args:
        _walk(arg)
    for kw in call.keywords:
        if kw.value is not None:
            _walk(kw.value)
    return titles


def _run() -> int:
    tree = ast.parse(FLOWS_PY.read_text(encoding="utf-8"), filename=str(FLOWS_PY))
    fails: list[tuple[int, str, str, int, int]] = []
    total_checked = 0

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func_name = getattr(node.func, "id", None)
        if func_name == "_btn_msg":
            limite = LIMITE_BOTON
            kind = "boton"
        elif func_name == "_list_msg":
            limite = LIMITE_ROW
            kind = "row"
        else:
            continue
        for title in _literal_titles(node):
            total_checked += 1
            if len(title) > limite:
                fails.append((node.lineno, kind, title, len(title), limite))

    if fails:
        print("── Títulos que exceden el límite de WhatsApp ──")
        for lineno, kind, title, largo, limite in fails:
            print(f"  flows.py:{lineno}  [{kind}, límite {limite}]  "
                  f"len={largo}  {title!r}")

    print(f"\n── Total: {total_checked - len(fails)}/{total_checked} OK, "
          f"{len(fails)} exceden el límite ──")
    return len(fails)


if __name__ == "__main__":
    sys.exit(1 if _run() else 0)
