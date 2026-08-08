"""
Regresión — bug de producción 2026-08-07 (Sergio Antonio Palma González,
WA 56950738587): el abono-gate general (`app/config.py::ABONO_REGLAS`, hoy
cubre Psiquiatría Y Gastroenterología) nació solo para Psiquiatría. Cuando el
slot original se pierde mientras el paciente espera (`procesar_imagen_abono`
en app/flows.py), el rebusque de una hora alternativa estaba CLAVADO en
"psiquiatría" sin importar la especialidad real del abono.

Caso real: paciente con abono pagado ($35.000) para Gastroenterología
(Dr. Quijano) perdió su cupo mientras esperaba. El bot le ofreció una hora de
Psiquiatría (Dra. Unibazo) en su lugar. Confirmó sin notar el cambio, y su
comprobante de $35.000 terminó evaluado contra los $60.000 de psiquiatría
("Vi que el monto en el comprobante es $35.000 y necesitamos $60.000...").

Este test fija que el rebusque SIEMPRE usa la especialidad del slot original
(`_area_pc`, derivado de `slot["especialidad"]`), nunca un valor fijo.

Ejecución:
    python tests/test_abono_gate_especialidad.py
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

TMP_DB_DIR = Path(tempfile.mkdtemp(prefix="cmc_test_abono_"))
TMP_DB = TMP_DB_DIR / "test_sessions.db"
os.environ["SESSIONS_DB"] = str(TMP_DB)

import session  # noqa: E402
session.DB_PATH = TMP_DB
TMP_DB.parent.mkdir(parents=True, exist_ok=True)

import medilink  # noqa: E402
import abono_comprobante  # noqa: E402
import flows  # noqa: E402
from session import save_session  # noqa: E402

PASS = 0
FAIL = 0


def check(label: str, got, expected):
    global PASS, FAIL
    ok = got == expected
    PASS += ok
    FAIL += not ok
    mark = "✅" if ok else "❌"
    print(f"{mark} {label}  got={got!r}  expected={expected!r}")


SLOT_GASTRO = {
    "especialidad": "Gastroenterología",
    "profesional": "Dr. Nicolás Quijano",
    "id_profesional": 65,
    "fecha": "2026-08-24",
    "fecha_display": "Lunes 24 de agosto",
    "hora_inicio": "10:20:00",
    "hora_fin": "10:40:00",
    "id_recurso": 1,
}

PACIENTE = {"id": 555, "nombre": "Sergio Antonio Palma González", "rut": "12830127-5"}


async def _fake_crear_cita_falla(**kwargs):
    """Simula que el cupo se lo llevaron mientras el paciente esperaba."""
    return None


def _fake_leer_comprobante_ok(img_bytes, content_type):
    return {"legible": True, "monto": 35000, "codigo_operacion": "OP123",
            "banco_origen": "BancoEstado"}


_ESPECIALIDADES_PEDIDAS: list[str] = []


async def _fake_buscar_primer_dia(especialidad, **kwargs):
    _ESPECIALIDADES_PEDIDAS.append(especialidad)
    return [], []  # sin alternativa — el test solo verifica QUÉ se pidió


async def _run():
    phone = "56950738587_test"
    save_session(phone, "WAIT_ABONO_COMPROBANTE", {
        "abono_gate_slot": SLOT_GASTRO,
        "abono_gate_paciente": PACIENTE,
        "rut": PACIENTE["rut"],
        "abono_gate_ts": "",
    })

    orig_crear_cita = medilink.crear_cita
    orig_buscar_primer_dia = medilink.buscar_primer_dia
    orig_leer_comprobante = abono_comprobante.leer_comprobante
    medilink.crear_cita = _fake_crear_cita_falla
    medilink.buscar_primer_dia = _fake_buscar_primer_dia
    abono_comprobante.leer_comprobante = _fake_leer_comprobante_ok
    try:
        await flows.procesar_imagen_abono(phone, b"fake-image-bytes", "image/jpeg")
    finally:
        medilink.crear_cita = orig_crear_cita
        medilink.buscar_primer_dia = orig_buscar_primer_dia
        abono_comprobante.leer_comprobante = orig_leer_comprobante

    check("rebusco pidió la MISMA especialidad del slot original",
          _ESPECIALIDADES_PEDIDAS, ["Gastroenterología"])


asyncio.run(_run())

print(f"\n── Total: {PASS}/{PASS + FAIL} passed, {FAIL} failed ──")
sys.exit(0 if FAIL == 0 else 1)
