"""
Regresión de 2 bugs encontrados en producción el 2026-08-07 (caso Rosa Aguilar
Vasquez, RUT 13.384.668-9, WA 56988663604) alrededor del registro de paciente
nuevo (WAIT_DATOS_NUEVO):

1. `_RE_SPLIT_DATOS_NUEVO` (app/flows.py): el separador de campos incluía "/"
   como separador SIEMPRE-activo, junto a la coma. Como "DD/MM/YYYY" es el
   formato de ejemplo que el bot sugiere ("María González López, F,
   15/03/1990"), la fecha se partía en 3 tokens sueltos que
   `_parsear_fecha_nacimiento` no reconoce individualmente — el registro
   avanzaba SIN fecha de nacimiento, en silencio. Fix: "/" solo separa si
   viene con espacio a los dos lados (mismo trato que el guion).

2. `_buscar_paciente_safe` / `_crear_paciente_con_recuperacion` (app/flows.py):
   con Medilink en 429 sostenido, `buscar_paciente()` (strict=False, default)
   se tragaba el error y devolvía None — el bot trataba a una paciente YA
   REGISTRADA como paciente nueva. Al crear la ficha, Medilink respondía "Ya
   existe paciente con el rut ..." y el bot descartaba nombre/sexo/fecha ya
   capturados con "Hubo un problema al registrarte, llama a recepción".
   Fix: `_buscar_paciente_safe` usa `strict=True` y distingue la excepción
   puntual de esa request (no el estado agregado del circuit breaker, que a
   propósito NO se apaga con un 429 sostenido — ver medilink.py `_agotado`).
   Si aun así `crear_paciente` falla por RUT duplicado,
   `_crear_paciente_con_recuperacion` reintenta la búsqueda antes de rendirse.

Ejecución:
    python tests/test_registro_paciente.py
"""
from __future__ import annotations

import asyncio
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT))

import flows  # noqa: E402


PASS = 0
FAIL = 0


def check(label: str, got, expected):
    global PASS, FAIL
    ok = got == expected
    PASS += ok
    FAIL += not ok
    mark = "✅" if ok else "❌"
    print(f"{mark} {label}  got={got!r}  expected={expected!r}")


# ── 1. Split de campos: "/" no debe partir una fecha DD/MM/YYYY ─────────────
CASOS_SPLIT = [
    ("Rosa Aguilar Vasquez,F,19/08/1978",
     ["Rosa Aguilar Vasquez", "F", "19/08/1978"]),
    ("María González López, F, 15/03/1990",  # ejemplo que el bot sugiere
     ["María González López", "F", "15/03/1990"]),
    ("sergio antonio palma gonzález, M , 09/04/1975",
     ["sergio antonio palma gonzález", "M", "09/04/1975"]),
    ("Pedro Pérez González, M, 15/03/1990",
     ["Pedro Pérez González", "M", "15/03/1990"]),
    # Guion con espacios sigue separando campos (comportamiento previo intacto)
    ("Ruth - Femenino - 28/05/1939",
     ["Ruth", "Femenino", "28/05/1939"]),
    # Barra con espacios como separador explícito de campos sigue funcionando
    ("María González / F / 15-03-1990",
     ["María González", "F", "15-03-1990"]),
]

print("── Split de campos (WAIT_DATOS_NUEVO) ──")
for raw, expected in CASOS_SPLIT:
    parts = [p.strip() for p in flows._RE_SPLIT_DATOS_NUEVO.split(raw) if p.strip()]
    check(raw, parts, expected)

# ── 2. La fecha sobrevive el split Y se parsea correctamente ────────────────
print("\n── Fecha de nacimiento parseada tras el split ──")
for raw, expected_parts in CASOS_SPLIT:
    parts = [p.strip() for p in flows._RE_SPLIT_DATOS_NUEVO.split(raw) if p.strip()]
    fechas = [flows._parsear_fecha_nacimiento(p) for p in parts]
    fecha = next((f for f in fechas if f), None)
    check(f"fecha en {raw!r}", fecha is not None, True)


# ── 3. _buscar_paciente_safe distingue 429 sostenido de "no existe" ─────────
print("\n── _buscar_paciente_safe: 429 sostenido == transitorio, no 'no existe' ──")


async def _run_buscar_paciente_safe_tests():
    # Caso A: Medilink saturado (429 agotado) → buscar_paciente(strict=True)
    # lanza MedilinkRateLimited. Debe devolver (None, True) — NUNCA (None, False).
    async def fake_buscar_paciente_429(rut, strict=False):
        if strict:
            raise flows.MedilinkRateLimited("Medilink saturado (429) en /pacientes")
        return None

    orig = flows.buscar_paciente
    flows.buscar_paciente = fake_buscar_paciente_429
    try:
        paciente, transient = await flows._buscar_paciente_safe("13384668-9")
        check("429 sostenido -> paciente", paciente, None)
        check("429 sostenido -> transient", transient, True)
    finally:
        flows.buscar_paciente = orig

    # Caso B: RUT realmente no existe (Medilink responde 200, lista vacía) →
    # (None, False), el flujo de "paciente nuevo" es correcto acá.
    async def fake_buscar_paciente_no_existe(rut, strict=False):
        return None

    flows.buscar_paciente = fake_buscar_paciente_no_existe
    try:
        paciente, transient = await flows._buscar_paciente_safe("11111111-1")
        check("no existe -> paciente", paciente, None)
        check("no existe -> transient", transient, False)
    finally:
        flows.buscar_paciente = orig

    # Caso C: paciente existe -> se devuelve tal cual
    async def fake_buscar_paciente_existe(rut, strict=False):
        return {"id": 42, "nombre": "Rosa Aguilar Vasquez", "rut": "13384668-9"}

    flows.buscar_paciente = fake_buscar_paciente_existe
    try:
        paciente, transient = await flows._buscar_paciente_safe("13384668-9")
        check("existe -> paciente.id", paciente and paciente.get("id"), 42)
        check("existe -> transient", transient, False)
    finally:
        flows.buscar_paciente = orig


asyncio.run(_run_buscar_paciente_safe_tests())


# ── 4. _crear_paciente_con_recuperacion recupera cuando Medilink dice "ya existe" ──
print("\n── _crear_paciente_con_recuperacion: recupera en vez de perder los datos ──")


async def _run_recuperacion_tests():
    # Caso A: crear_paciente falla (duplicado) pero buscar_paciente ahora SÍ
    # encuentra al paciente (Medilink se recuperó del 429 puntual) -> se usa esa ficha.
    async def fake_crear_paciente_duplicado(rut, nombre, apellidos, **kw):
        return None  # simula 400 "Ya existe paciente con el rut ..."

    async def fake_buscar_paciente_recupera(rut, strict=False):
        return {"id": 42, "nombre": "Rosa Aguilar Vasquez", "rut": "13384668-9"}

    orig_crear = flows.crear_paciente
    orig_buscar = flows.buscar_paciente
    flows.crear_paciente = fake_crear_paciente_duplicado
    flows.buscar_paciente = fake_buscar_paciente_recupera
    try:
        paciente = await flows._crear_paciente_con_recuperacion(
            "13384668-9", "Rosa", "Aguilar Vasquez", {"sexo": "F"}, "56988663604")
        check("recupera duplicado -> paciente.id", paciente and paciente.get("id"), 42)
    finally:
        flows.crear_paciente = orig_crear
        flows.buscar_paciente = orig_buscar

    # Caso B: crear_paciente falla y buscar_paciente tampoco encuentra nada
    # (falla real, no duplicado) -> se propaga None (mensaje de error normal).
    async def fake_buscar_paciente_nada(rut, strict=False):
        return None

    flows.crear_paciente = fake_crear_paciente_duplicado
    flows.buscar_paciente = fake_buscar_paciente_nada
    try:
        paciente = await flows._crear_paciente_con_recuperacion(
            "99999999-9", "Nadie", "Existe", {}, "56900000000")
        check("sin recuperación posible -> None", paciente, None)
    finally:
        flows.crear_paciente = orig_crear
        flows.buscar_paciente = orig_buscar

    # Caso C: crear_paciente funciona a la primera -> no llama a buscar_paciente
    _llamadas = {"buscar": 0}

    async def fake_crear_paciente_ok(rut, nombre, apellidos, **kw):
        return {"id": 7, "nombre": f"{nombre} {apellidos}", "rut": rut}

    async def fake_buscar_paciente_no_deberia_llamarse(rut, strict=False):
        _llamadas["buscar"] += 1
        return None

    flows.crear_paciente = fake_crear_paciente_ok
    flows.buscar_paciente = fake_buscar_paciente_no_deberia_llamarse
    try:
        paciente = await flows._crear_paciente_con_recuperacion(
            "12345678-9", "Juan", "Perez", {}, "56911111111")
        check("crea OK -> paciente.id", paciente and paciente.get("id"), 7)
        check("crea OK -> no reintenta búsqueda", _llamadas["buscar"], 0)
    finally:
        flows.crear_paciente = orig_crear
        flows.buscar_paciente = orig_buscar


asyncio.run(_run_recuperacion_tests())


print(f"\n── Total: {PASS}/{PASS + FAIL} passed, {FAIL} failed ──")
sys.exit(0 if FAIL == 0 else 1)
