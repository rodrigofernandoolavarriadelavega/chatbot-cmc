"""
Regresión — bug de producción 2026-08-24 (portaviones consolidado #9, caso
real: WA 56939444138 canceló SIN pedir RUT la cita de "Jhon" perteneciente a
otro paciente/celular, WA 56939186484).

Root cause reconstruido con logs + sessions.db de prod:
- 2026-05-15, 56939444138 estaba agendando psicología PARA SÍ MISMO, escribió
  "Otro rut" en WAIT_MODALIDAD, el bot crasheó ("Tuve un problema técnico") y
  volvió a IDLE.
- El paciente pegó de una vez un bloque con los datos de un TERCERO (su
  hermano Jhon, para registrarlo/reservarle a él):
  "23208009-4\nJhon Muñoz Castillo\nArturo perez canto Ramadillas\nFonasa\n21-12-2009"
- `try_autocapture_rut_name` (app/session.py) escaneó el texto libre, encontró
  un RUT válido, y como el teléfono 56939444138 no tenía perfil aún, guardó
  el RUT+nombre de JHON como si fueran la identidad DUEÑA del celular
  56939444138 (contact_profiles).
- 3 meses después (2026-08-22), 56939444138 escribió "No podre ir" (su propia
  cita, id_cita 63287). El flujo de cancelar usó el perfil cacheado —el RUT
  de Jhon, no el suyo—, mostró las citas de Jhon SIN pedir RUT, y terminó
  cancelando la cita 63286 de Jhon (celular real en Medilink: 56939186484),
  no la propia.

Medilink SÍ tenía la señal para prevenir esto: `buscar_paciente('23.208.009-4')`
devuelve celular=+56939186484 — DISTINTO al celular que escribió el bloque
(56939444138). Fix: `try_autocapture_rut_name` ahora es async y verifica el
celular de ficha en Medilink contra el celular que está escribiendo; si
Medilink tiene un celular registrado y no coincide, NO persiste (evita que un
RUT de tercero quede cacheado como identidad del dueño del celular).

Ejecución:
    python tests/test_autocapture_rut_tercero_2026_08_24.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT))

TMP_DB_DIR = Path(tempfile.mkdtemp(prefix="cmc_test_autocap_"))
TMP_DB = TMP_DB_DIR / "test_sessions.db"
os.environ["SESSIONS_DB"] = str(TMP_DB)

import session  # noqa: E402
session.DB_PATH = TMP_DB
TMP_DB.parent.mkdir(parents=True, exist_ok=True)

from session import try_autocapture_rut_name, get_profile  # noqa: E402

PASS = 0
FAIL = 0


def check(label: str, got, expected):
    global PASS, FAIL
    ok = got == expected
    PASS += ok
    FAIL += not ok
    print(("OK  " if ok else "FAIL"), label, "->", got, "" if ok else f"(esperado {expected})")


async def _run():
    # ── Caso real: RUT de tercero (celular en Medilink NO coincide) ────────
    phone_giovanny = "56939444138"
    texto_tercero = (
        "23208009-4 \nJhon Muñoz Castillo \nArturo perez canto Ramadillas "
        "\nFonasa \n21-12-2009"
    )
    with patch("medilink.buscar_paciente", new=AsyncMock(return_value={
        "id": 855, "nombre": "Jhon Muñoz Castillo", "rut": "23208009-4",
        "celular": "+56939186484", "telefono": "+56939186484",
    })):
        resultado = await try_autocapture_rut_name(phone_giovanny, texto_tercero)
    check("no captura RUT de tercero (celular Medilink distinto)", resultado, None)
    check("contact_profiles NO quedó contaminado", get_profile(phone_giovanny), None)

    # ── Caso control: mismo escenario pero SIN celular en Medilink (paciente
    # nuevo, no puede verificarse) — debe mantener comportamiento anterior ──
    phone_nuevo = "56911112222"
    with patch("medilink.buscar_paciente", new=AsyncMock(return_value=None)):
        resultado2 = await try_autocapture_rut_name(phone_nuevo, "12.345.678-5 Pedro Soto")
    check("sin ficha Medilink -> sigue capturando (best-effort)",
          bool(resultado2 and resultado2.get("rut")), True)

    # ── Caso control: celular en Medilink SÍ coincide -> captura normal ────
    phone_dueno = "56933334444"
    with patch("medilink.buscar_paciente", new=AsyncMock(return_value={
        "id": 900, "nombre": "Ana Perez", "rut": "9.443.926-4",
        "celular": "+56933334444", "telefono": "",
    })):
        resultado3 = await try_autocapture_rut_name(phone_dueno, "9.443.926-4 maría Parra pedrero")
    check("celular Medilink coincide -> SÍ captura", bool(resultado3), True)
    perfil3 = get_profile(phone_dueno)
    check("perfil quedó guardado para el dueño real", bool(perfil3 and perfil3.get("rut")), True)

    # ── Caso control: Medilink cae/timeout -> no debe romper, best-effort ──
    phone_caida = "56955556666"
    with patch("medilink.buscar_paciente", new=AsyncMock(side_effect=Exception("timeout"))):
        resultado4 = await try_autocapture_rut_name(phone_caida, "7.654.321-6 Luis Rojas")
    check("Medilink caído no bloquea autocapture (best-effort)",
          bool(resultado4 and resultado4.get("rut")), True)


def main():
    asyncio.run(_run())
    print(f"\n{PASS} pasaron, {FAIL} fallaron")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
