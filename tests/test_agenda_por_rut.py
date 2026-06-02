"""
Test del bloqueo de cita repetida POR RUT (no por teléfono) — fix 3a86983 + hotfix.

Regla de negocio (caso real 2026-06-02: una mamá llegó con sus 4 hijos):
  - NO se limita por número de teléfono.
  - Solo se bloquea cuando el MISMO RUT ya tiene una hora con el MISMO
    profesional el MISMO día → se ofrece cambiar esa hora.
  - Una persona puede agendar con distintos profesionales el mismo día.
  - Un apoderado puede agendar a varios familiares (RUTs distintos) desde
    un mismo celular con el mismo doctor el mismo día.

Además pinea el bug que se coló en 3a86983: el bloque referenciaba
`es_tercero` (que se asigna recién más abajo en handle_message) → habría
dado UnboundLocalError al dispararse el bloqueo.

Enfoque: en vez de manejar todo el flujo conversacional (el harness de
agenda quedó desactualizado tras cambios del flujo de slots), sembramos la
sesión ya en CONFIRMING_CITA y mandamos "si". Eso ejercita exactamente el
código del fix.

Ejecución:
    PYTHONPATH=app:. python3 tests/test_agenda_por_rut.py
"""
from __future__ import annotations

import asyncio
import sys

# Importar el harness de agenda aplica TODOS los monkey-patches (Medilink,
# Claude, WhatsApp, DB temporal de sesiones).
import tests.harness_agenda_200 as H
from session import get_session, reset_session, save_session

# ── listar_citas_paciente fiel: scoped por id_paciente ───────────────────────
# El fake del harness ignora el id_paciente. Para probar la per-RUT-ness
# necesitamos que cada paciente vea SOLO sus citas (lo que Medilink hace).
CITAS_BY_ID: dict[int, list[dict]] = {}

async def fake_listar_citas_por_id(id_paciente: int = 0, **kwargs):
    return list(CITAS_BY_ID.get(int(id_paciente), []))

H.flows.listar_citas_paciente = fake_listar_citas_por_id

# El fake_crear_cita del harness quedó con firma vieja (sin `modalidad`).
# Lo reemplazamos por uno flexible que ADEMÁS registra cada reserva creada
# (señal fiel de "se agendó", porque la confirmación al paciente sale por
# send_whatsapp y el valor de retorno de handle_message puede ser un cross-sell).
CREATED: list[dict] = []

async def fake_crear_cita_flex(*args, **kwargs):
    CREATED.append(kwargs or {"args": args})
    return {"id": 5555}

H.flows.crear_cita = fake_crear_cita_flex

FAILURES: list[str] = []

ID_OLAVARRIA = 1   # Dr. Rodrigo Olavarría (Medicina General)
FECHA = "2026-06-10"

def _slot():
    return {
        "id_profesional": ID_OLAVARRIA,
        "profesional": "Dr. Rodrigo Olavarría",
        "especialidad": "Medicina General",
        "fecha": FECHA,
        "fecha_display": "miércoles 10 de junio",
        "hora_inicio": "09:00",
        "hora_fin": "09:15",
    }

def _seed_confirming(phone: str, paciente: dict, *, booking_for_other=False):
    """Deja una sesión lista para confirmar la cita (estado CONFIRMING_CITA)."""
    reset_session(phone)
    data = {
        "slot_elegido": _slot(),
        "paciente": paciente,
        "rut": paciente["rut"],
        "especialidad": "Medicina General",
        "modalidad": "fonasa",
        "dup_ok": True,            # saltar el soft-warn fecha+especialidad
        "booking_for_other": booking_for_other,
    }
    save_session(phone, "CONFIRMING_CITA", data)


def check(cond, label, got=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    if not cond:
        FAILURES.append(label)
        if got:
            print(f"         got: {got[:300]}")


async def confirm(phone: str) -> str:
    sess = get_session(phone)
    return H._normalize(await H.flows.handle_message(phone, "si", sess))


async def main() -> int:
    P_MAMA = {"id": 100, "nombre": "María Multi Citas", "rut": "11111111-1"}
    P_HIJO = {"id": 300, "nombre": "Pedro Agenda López", "rut": "33333333-3"}

    # ── A) MISMO RUT + MISMO profesional + MISMO día → BLOQUEA (sin crashear) ─
    print("\nA) Mismo RUT ya tiene hora con el mismo profesional el mismo día:")
    phone = "56930000001"
    CITAS_BY_ID.clear()
    CITAS_BY_ID[100] = [{
        "id_profesional": ID_OLAVARRIA, "profesional": "Dr. Rodrigo Olavarría",
        "fecha": FECHA, "hora_inicio": "10:00",
    }]
    _seed_confirming(phone, P_MAMA)
    CREATED.clear()
    resp = await confirm(phone)          # si esto crashea → UnboundLocalError vivo
    low = resp.lower()
    check("cambiar" in low and "no agendamos dos horas" in low,
          "bloquea y ofrece cambiar esa hora (sin UnboundLocalError)", resp)
    check(len(CREATED) == 0,
          "NO crea una segunda cita con el mismo prof", f"crear_cita llamado {len(CREATED)}x")

    # ── A2) Igual que A pero para tercero → mensaje en 3ª persona ─────────────
    print("\nA2) Mismo caso agendando para un tercero (nombre en 3ª persona):")
    phone = "56930000012"
    CITAS_BY_ID.clear()
    CITAS_BY_ID[300] = [{
        "id_profesional": ID_OLAVARRIA, "profesional": "Dr. Rodrigo Olavarría",
        "fecha": FECHA, "hora_inicio": "10:00",
    }]
    _seed_confirming(phone, P_HIJO, booking_for_other=True)
    resp = await confirm(phone)
    low = resp.lower()
    check("pedro ya tiene" in low,
          "usa el nombre del paciente ('Pedro ya tiene'), no 'Ya tienes'", resp)

    # ── B) MISMO RUT, OTRO profesional, mismo día → PERMITE ───────────────────
    print("\nB) Mismo RUT con OTRO profesional el mismo día:")
    phone = "56930000002"
    CITAS_BY_ID.clear()
    CITAS_BY_ID[100] = [{
        "id_profesional": 73, "profesional": "Dr. Andrés Abarca",  # otro prof
        "fecha": FECHA, "hora_inicio": "11:00",
    }]
    _seed_confirming(phone, P_MAMA)
    CREATED.clear()
    resp = await confirm(phone)
    low = resp.lower()
    check("no agendamos dos horas" not in low,
          "NO bloquea: otro profesional el mismo día es válido", resp)
    check(len(CREATED) == 1,
          "permite reservar con el otro profesional", f"crear_cita llamado {len(CREATED)}x")

    # ── C) MAMÁ con 2 hijos (RUTs distintos), mismo prof/día → PERMITE ────────
    print("\nC) Mismo teléfono, RUTs distintos, mismo profesional el mismo día:")
    phone = "56930000003"
    CITAS_BY_ID.clear()
    # La mamá (RUT 11111111-1) YA tiene la hora con Olavarría ese día...
    CITAS_BY_ID[100] = [{
        "id_profesional": ID_OLAVARRIA, "profesional": "Dr. Rodrigo Olavarría",
        "fecha": FECHA, "hora_inicio": "10:00",
    }]
    CITAS_BY_ID[300] = []   # ...el hijo (otro RUT) no tiene ninguna.
    # Se agenda al HIJO (id 300) con el mismo doctor, mismo día, mismo celular.
    _seed_confirming(phone, P_HIJO, booking_for_other=True)
    CREATED.clear()
    resp = await confirm(phone)
    low = resp.lower()
    check("no agendamos dos horas" not in low,
          "NO bloquea por compartir teléfono (es otro RUT)", resp)
    check(len(CREATED) == 1,
          "el 2º hijo (otro RUT) SÍ puede agendar el mismo doctor/día", f"crear_cita llamado {len(CREATED)}x")

    print()
    if FAILURES:
        print(f"RESULTADO: {len(FAILURES)} fallo(s) — {FAILURES}")
        return 1
    print("RESULTADO: 6/6 PASS — bloqueo per-RUT correcto, sin límite por teléfono, sin crash")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
