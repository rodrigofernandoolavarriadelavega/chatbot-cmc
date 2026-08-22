"""Regresión: confirmaciones con puntuación pegada (canal Facebook/Instagram).

Caso real fb_27793059056989936 (2026-08-13, psiquiatría — Dra. Unibazo):

    15:53:20 out CONFIRMING_CITA  '...¿La confirmo?'
    15:53:33 in  CONFIRMING_CITA  'Si, reservar'
    15:53:34 out CONFIRMING_CITA  'Responde *Sí* para confirmar, o toca ❌ Cambiar...'
    15:53:46 in  CONFIRMING_CITA  '✅'
    15:54:57 in  CONFIRMING_CITA  'Si, reservar'
    15:54:58 out CONFIRMING_CITA  'Responde *Sí* para confirmar...'   ← loop
    15:58:46 out HUMAN_TAKEOVER   '[Recepcionista] Necesitamos Ruth, nombre...'

Messenger/Instagram no tienen botones nativos con `id` (el bot los renderiza
como texto plano vía `_interactive_to_text`), así que el paciente re-escribe
el título del botón a mano, coma incluida: "✅ Sí, reservar". El check de
prefijo `tl_norm.startswith("si ")` fallaba porque el 3er carácter de
"si, reservar" es "," y no " ". La paciente terminó siendo atendida a mano por
recepción (pago, RUT, dirección, fecha de nacimiento — todo manual).

Fix: `flows._afirma()` / `flows._niega()` (helpers compartidos) toleran
puntuación pegada antes de decidir sí/no. Aplicado en CONFIRMING_CITA y
CONFIRMING_CANCEL (mismo patrón de botón "Sí, <verbo>").
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT))

import harness_50 as H  # noqa: E402 — trae todos los mocks de Medilink/Claude ya aplicados
import flows  # noqa: E402
from session import get_session, reset_session, save_session  # noqa: E402

PHONE_FB = "fb_27793059056989936"


def _slot():
    return {
        "profesional": "Dra. Cecilia Unibazo",
        "especialidad": "Psiquiatría",
        "fecha": "2026-08-13",
        "fecha_display": "Jueves 13 de agosto",
        "hora_inicio": "19:40",
        "hora_fin": "20:20",
        "id_profesional": 90,
        "id_recurso": 1,
        "duracion": 40,
    }


def _en_confirming(paciente=True, phone=PHONE_FB):
    reset_session(phone)
    data = {
        "especialidad": "psiquiatría",
        "slot_elegido": _slot(),
        "rut": "11111111-1",
        "modalidad": "particular",
    }
    if paciente:
        data["paciente"] = {"id": 100, "nombre": "Catherine Pérez Conus", "rut": "11111111-1"}
    save_session(phone, "CONFIRMING_CITA", data)
    return get_session(phone)


def _texto(resp: Any) -> str:
    return H._normalize(resp)


def _responder(mensaje: str, phone=PHONE_FB) -> str:
    ses = get_session(phone)
    return _texto(asyncio.run(flows.handle_message(phone, mensaje, ses)))


# ── El bug exacto ────────────────────────────────────────────────────────────

def test_si_coma_reservar_confirma_la_cita():
    _en_confirming()
    resp = _responder("Si, reservar")
    assert "Responde" not in resp or "confirmar" not in resp, resp
    # Confirmada de verdad: la sesión debe salir de CONFIRMING_CITA.
    assert get_session(PHONE_FB)["state"] != "CONFIRMING_CITA", resp


def test_si_tilde_coma_reservar_confirma():
    _en_confirming()
    resp = _responder("Sí, reservar")
    assert get_session(PHONE_FB)["state"] != "CONFIRMING_CITA", resp


@pytest.mark.parametrize("msg", [
    "Si, reservar", "Sí, reservar!", "si confirmo", "sí, por favor",
    "dale, reservalo", "confirmo, gracias",
])
def test_variantes_con_puntuacion_confirman(msg):
    _en_confirming()
    resp = _responder(msg)
    assert get_session(PHONE_FB)["state"] != "CONFIRMING_CITA", f"{msg!r} → {resp}"


def test_catchall_sin_sentido_sigue_intacto():
    """No hay que volverse permisivo: texto sin relación sigue pidiendo sí/no."""
    _en_confirming()
    resp = _responder("cuanto es la mitad de 10")
    assert "Responde" in resp and "confirmar" in resp, resp
    assert get_session(PHONE_FB)["state"] == "CONFIRMING_CITA"


# ── Reconstrucción cuando falta `paciente` pero el slot sigue ──────────────

def test_reconstruye_paciente_desde_perfil_si_falta():
    _en_confirming(paciente=False)
    resp = _responder("si")
    assert "Perdimos el hilo" not in resp, resp


def test_perdimos_el_hilo_si_falta_el_slot_tambien():
    phone = "fb_sin_slot_ni_paciente"
    reset_session(phone)
    save_session(phone, "CONFIRMING_CITA", {"modalidad": "particular"})
    resp = _responder("si", phone=phone)
    assert "Perdimos el hilo" in resp, resp


# ── Misma clase de bug en CONFIRMING_CANCEL ("✅ Sí, cancelar") ─────────────

def _en_confirming_cancel(phone="fb_cancel_test"):
    reset_session(phone)
    save_session(phone, "CONFIRMING_CANCEL", {
        "cita_cancelar": {"id": 999, "profesional": "Dr. Rodrigo Olavarría",
                          "especialidad": "Medicina General",
                          "fecha_display": "Lunes 17 de agosto", "hora_inicio": "10:00"},
    })
    return get_session(phone)


def test_si_coma_cancelar_ejecuta_la_cancelacion():
    phone = "fb_cancel_test"
    _en_confirming_cancel(phone)
    resp = _responder("Sí, cancelar", phone=phone)
    assert get_session(phone)["state"] != "CONFIRMING_CANCEL", resp


def test_no_coma_mantener_no_cancela():
    phone = "fb_cancel_test_no"
    _en_confirming_cancel(phone)
    resp = _responder("No, mantener", phone=phone)
    assert "mantiene" in resp.lower(), resp


if __name__ == "__main__":
    import subprocess
    raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-v"]))
