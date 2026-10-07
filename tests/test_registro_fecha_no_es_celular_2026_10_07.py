"""
El bot escribía la FECHA DE NACIMIENTO en el campo CELULAR de Medilink (2026-10-07).

En el registro rápido (WAIT_DATOS_NUEVO) cada parte del mensaje se clasificaba
probando primero "¿es celular?" y recién después "¿es fecha?". El patrón de
celular aceptaba dígitos con guiones o espacios, de 8 a 12 caracteres, así que
"15-03-1990" (8 dígitos) se tomaba como teléfono y el `continue` impedía que
llegara al parser de fechas. Tres daños a la vez:

  1. Medilink quedaba con celular = 15031990
  2. La ficha quedaba SIN fecha de nacimiento
  3. No se guardaba el WhatsApp real (el autorrelleno solo corre sin celular)

Medido en prod (solo lectura, 17-abr → 7-oct-2026): 80 registros de 73
teléfonos; 75 con guiones, 4 con espacios, 1 pegada.

Fix: la fecha se prueba ANTES que el celular, y un celular chileno exige 9
dígitos (o 56 + 9).

Ejecución:
    python tests/test_registro_fecha_no_es_celular_2026_10_07.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT))

_TMP_DB = Path(tempfile.mkdtemp(prefix="cmc_fecha_cel_")) / "sessions.db"
os.environ["SESSIONS_DB"] = str(_TMP_DB)
os.environ.setdefault("SQLCIPHER_KEY", "")
os.environ.setdefault("MEDILINK_BASE_URL", "https://fake")
os.environ.setdefault("MEDILINK_TOKEN", "fake")
os.environ.setdefault("MEDILINK_SUCURSAL", "1")
os.environ.setdefault("ANTHROPIC_API_KEY", "fake")
os.environ.setdefault("META_ACCESS_TOKEN", "fake")
os.environ.setdefault("META_PHONE_NUMBER_ID", "fake")
os.environ.setdefault("META_VERIFY_TOKEN", "fake")
os.environ.setdefault("OPENAI_API_KEY", "fake")

import flows  # noqa: E402


async def _sin_llm(*a, **kw):
    return {}
if hasattr(flows, "classify_with_context"):
    flows.classify_with_context = _sin_llm

PASS = 0
FAIL = 0


def check(label, got, expected):
    global PASS, FAIL
    ok = got == expected
    PASS += ok
    FAIL += not ok
    print(f"{'✅' if ok else '❌'} {label}  got={got!r}  expected={expected!r}")


_SLOT = {"especialidad": "Kinesiología", "profesional": "Leonardo Vidal",
         "fecha_display": "lunes 8 de septiembre", "hora_inicio": "10:30:00",
         "id_profesional": 1, "fecha": "2026-09-08"}


def registrar(texto: str, phone: str) -> dict:
    """Corre WAIT_DATOS_NUEVO y devuelve los kwargs con que se creó la ficha."""
    capturado: dict = {}

    async def fake_crear_paciente(rut, nombre, apellidos, **kw):
        capturado.update({"nombre": nombre, "apellidos": apellidos, "extra": kw})
        return {"id": 999, "nombre": f"{nombre} {apellidos}", "rut": rut,
                "sexo": kw.get("sexo", "")}

    orig = flows.crear_paciente
    flows.crear_paciente = fake_crear_paciente
    try:
        sess = {"state": "WAIT_DATOS_NUEVO",
                "data": {"rut": "11111111-1", "slot_elegido": dict(_SLOT),
                         "modalidad": "particular"}}
        asyncio.run(flows.handle_message(phone, texto, sess))
    finally:
        flows.crear_paciente = orig
    return capturado.get("extra", {})


print("── 1. Fecha con guiones (75 de los 80 casos reales) ──")
e = registrar("María González López, F, 15-03-1990", "56912340001")
check("fecha_nacimiento", e.get("fecha_nacimiento"), "1990-03-15")
check("celular = WhatsApp, no la fecha", e.get("celular"), "912340001")

print("\n── 2. Fecha con espacios ──")
e = registrar("Juan Pérez Soto, M, 10 01 1985", "56912340002")
check("fecha_nacimiento", e.get("fecha_nacimiento"), "1985-01-10")
check("celular = WhatsApp", e.get("celular"), "912340002")

print("\n── 3. Fecha pegada DDMMYYYY ──")
e = registrar("Ana Díaz Rojas, F, 20051992", "56912340003")
check("fecha_nacimiento", e.get("fecha_nacimiento"), "1992-05-20")
check("celular = WhatsApp", e.get("celular"), "912340003")

print("\n── 4. Con barras sigue igual (no hay regresión) ──")
e = registrar("Rosa Vera Muñoz, F, 19/08/1978", "56912340004")
check("fecha_nacimiento", e.get("fecha_nacimiento"), "1978-08-19")
check("celular = WhatsApp", e.get("celular"), "912340004")

print("\n── 5. Celular real escrito por el paciente se respeta ──")
e = registrar("Luis Soto Lara, M, 01-01-1990, +56 9 8765 4321", "56912340005")
check("fecha_nacimiento", e.get("fecha_nacimiento"), "1990-01-01")
check("celular escrito", e.get("celular"), "987654321")

print("\n── 6. Celular con guiones y sin código país ──")
e = registrar("Pedro Rojas Vera, M, 03/07/1980, 9-8765-4322", "56912340006")
check("celular escrito", e.get("celular"), "987654322")
check("fecha_nacimiento", e.get("fecha_nacimiento"), "1980-07-03")

print(f"\n{PASS} OK · {FAIL} FALLAS")
sys.exit(1 if FAIL else 0)
