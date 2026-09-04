"""
El bot creaba las fichas SIN comuna y SIN direccion (2026-09-03).

Hallazgo: sobre las 50 fichas mas recientes de Medilink (ids 16019-16064) la
correlacion era perfecta -20 de 20 de las fichas sin comuna tampoco tenian
direccion-: son exactamente las que crea el bot. El 40% de las fichas nuevas
nacia ciega, y un blanco despues no se distingue de nada.

La pregunta SI existia (`WAIT_COMUNA`, con el diccionario de localidades ya
enchufado) pero quedo INALCANZABLE al pasar al registro rapido de un mensaje:
nadie hace `save_session(phone, "WAIT_NOMBRE_NUEVO", ...)` en todo el repo, y
esa es la unica puerta a la cadena que termina en WAIT_COMUNA.

Fix: `WAIT_DATOS_NUEVO` acepta una localidad OPCIONAL como una parte mas del
mismo mensaje. Sin preguntas nuevas, sin idas y vueltas extra.

Lo que estos casos protegen:
  1. Localidad de otra comuna  -> se escribe esa comuna, no Arauco
  2. Localidad de Arauco       -> comuna Arauco + SECTOR (Laraquete != urbano)
  3. Sin localidad             -> no se inventa nada (no hay regresion)
  4. Basura en esa posicion    -> NO termina escrita como comuna
  5. Apellido que es toponimo  -> jamas le roba la parte del nombre
  6. Caleta de otra comuna     -> el caso real de la ficha 16054 (Quidico)
  7. Fuera de la provincia     -> va la ciudad, no la etiqueta interna
  8. Calle con nombre de pueblo-> "calle Los Alamos 53, Laraquete" es Laraquete

Ejecucion:
    python tests/test_registro_localidad.py
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

_TMP_DB = Path(tempfile.mkdtemp(prefix="cmc_localidad_")) / "sessions.db"
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

# El clasificador LLM no pinta nada en este flujo (WAIT_DATOS_NUEVO es
# deterministico) y sin mock sale a la red de verdad en cada caso.
async def _sin_llm(*a, **kw):
    return {}   # dict vacío: el pre-router lo lee con .get()
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


def registrar(texto: str, phone: str = "56900000123") -> dict:
    """Corre WAIT_DATOS_NUEVO y devuelve los kwargs con que se creo la ficha."""
    capturado: dict = {}

    async def fake_crear_paciente(rut, nombre, apellidos, **kw):
        capturado.update({"rut": rut, "nombre": nombre,
                          "apellidos": apellidos, "extra": kw})
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
    return capturado


print("── 1. Localidad de OTRA comuna: no puede quedar como Arauco ──")
r = registrar("María González López, F, 15/03/1990, Curanilahue")
check("comuna", r.get("extra", {}).get("comuna"), "Curanilahue")
check("nombre intacto", r.get("nombre"), "María")

print("\n── 2. Localidad de Arauco: la comuna es Arauco pero el SECTOR importa ──")
r = registrar("Juan Pérez Soto, M, 10/01/1985, Laraquete", "56900000124")
check("comuna", r.get("extra", {}).get("comuna"), "Arauco")
try:
    from session import get_profile  # noqa
    import session as _s
    with _s._conn() as c:
        row = c.execute("SELECT sector FROM contact_profiles WHERE phone=?",
                        ("56900000124",)).fetchone()
    check("sector persistido", row[0] if row else None, "Laraquete")
except Exception as e:  # pragma: no cover
    print(f"⚠️  no se pudo leer contact_profiles: {e}")

print("\n── 3. Sin localidad: no se inventa nada ──")
r = registrar("Ana Díaz Rojas, F, 20/05/1992", "56900000125")
check("no manda comuna", "comuna" in r.get("extra", {}), False)
check("fecha_nacimiento sí", r.get("extra", {}).get("fecha_nacimiento"), "1992-05-20")

print("\n── 4. Basura en esa posición: NO puede escribirse como comuna ──")
r = registrar("Pedro Soto Lara, M, 03/07/1980, xxqq zzz", "56900000126")
check("no manda comuna", "comuna" in r.get("extra", {}), False)
check("nombre intacto", r.get("nombre"), "Pedro")

print("\n── 5. Apellido que es topónimo: jamás le roba la parte del nombre ──")
r = registrar("Rosa Contulmo Vera, F, 19/08/1978", "56900000127")
check("nombre", r.get("nombre"), "Rosa")
check("apellidos", r.get("apellidos"), "Contulmo Vera")
check("no manda comuna", "comuna" in r.get("extra", {}), False)

print("\n── 6. Caleta de otra comuna (ficha real 16054: Quidico → decía Arauco) ──")
r = registrar("Luis Vera Muñoz, M, 01/01/1990, Quidico", "56900000128")
check("comuna", r.get("extra", {}).get("comuna"), "Tirúa")

print("\n── 7. Fuera de la provincia: va la ciudad, no la etiqueta interna ──")
r = registrar("Carla Ruiz Pino, F, 05/05/1995, Concepción", "56900000129")
check("comuna", r.get("extra", {}).get("comuna"), "Concepción")

print("\n── 8. Calle con nombre de pueblo: manda la localidad, no la calle ──")
r = registrar("Ema Soto Luna, F, 09/09/1970, calle Los Álamos 53, Laraquete",
              "56900000130")
check("comuna", r.get("extra", {}).get("comuna"), "Arauco")

print(f"\n── Total: {PASS}/{PASS+FAIL} passed, {FAIL} failed ──")
sys.exit(1 if FAIL else 0)
