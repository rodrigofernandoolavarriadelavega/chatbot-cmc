"""FIX D (tablero julio): "hola" en estados WAIT_META_* debe resetear al menú
normal, igual que en el resto de los estados no-retomables.

Diagnóstico (2026-08-22): el bloque global de saludo/reset en flows.py
(`_es_comando_reset`, ejecutado ANTES del dispatcher por estado) ya cubre
WAIT_META_SLOT_CHOICE y WAIT_META_WAITLIST porque ninguno de los dos está en
`_FLUJO_RETOMABLE` — cae al reset genérico + menú de bienvenida, igual que
cualquier otro estado no listado ahí. Verificado con reproducción directa
(handle_message real, sin parche) antes de tocar código. Este test lo deja
como regresión — si algún refactor mueve WAIT_META_* a `_FLUJO_RETOMABLE` sin
querer, o intercepta el saludo antes de este bloque, el test debe fallar.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT))

import harness_50 as H  # noqa: E402
import flows  # noqa: E402
from session import get_session, reset_session, save_session  # noqa: E402

PHONE = "56900002222"


@pytest.mark.parametrize("state", ["WAIT_META_SLOT_CHOICE", "WAIT_META_WAITLIST"])
@pytest.mark.parametrize("saludo", ["hola", "Hola!", "buenas", "buenos días"])
def test_saludo_resetea_a_menu_normal(state, saludo):
    reset_session(PHONE)
    save_session(PHONE, state, {"especialidad": "kinesiología"})
    resp = asyncio.run(flows.handle_message(PHONE, saludo, get_session(PHONE)))
    texto = H._normalize(resp)
    assert get_session(PHONE)["state"] == "IDLE", (state, saludo, texto)
    assert "¿Qué necesitas hoy?" in texto or "asistente" in texto.lower(), texto


if __name__ == "__main__":
    import subprocess
    raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-v"]))
