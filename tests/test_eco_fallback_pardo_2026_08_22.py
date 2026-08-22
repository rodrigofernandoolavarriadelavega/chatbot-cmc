"""FIX C (auditoría 2026-08-22): tras 2 intentos sin resolver el tipo de
ecografía en wait_eco_tipo, ofrecer directo la eco general de partes blandas
con David Pardo en vez de re-preguntar infinito / escalar a ciegas a recepción.

Antes: al 2° intento fallido, `save_session(phone, "HUMAN_TAKEOVER", {})` — el
paciente quedaba 100% de las veces en manos de recepción, incluso cuando lo que
necesitaba (abdominal/renal/tiroides/partes blandas) era exactamente lo que
hace Pardo. Ahora se le ofrece directo con botones "Sí, agendar" / "Es otra eco".
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT))

import harness_50 as H  # noqa: E402
import flows  # noqa: E402
from session import get_session, reset_session, save_session  # noqa: E402

PHONE = "56900001111"


def _texto(resp: Any) -> str:
    return H._normalize(resp)


def _iniciar():
    reset_session(PHONE)
    save_session(PHONE, "WAIT_ESPECIALIDAD", {"wait_eco_tipo": True})


def _responder(mensaje: str) -> str:
    ses = get_session(PHONE)
    return _texto(asyncio.run(flows.handle_message(PHONE, mensaje, ses)))


def test_dos_intentos_fallidos_ofrece_pardo_no_takeover():
    _iniciar()
    r1 = _responder("no se")
    assert get_session(PHONE)["state"] == "WAIT_ESPECIALIDAD"
    r2 = _responder("algo asi como una ecografia")
    assert get_session(PHONE)["state"] == "WAIT_ECO_FALLBACK_PARDO", r2
    assert get_session(PHONE)["state"] != "HUMAN_TAKEOVER"
    assert "David Pardo" in r2, r2


def test_acepta_pardo_confirma_agendamiento_partes_blandas():
    _iniciar()
    _responder("no se")
    _responder("algo asi como una ecografia")
    r3 = _responder("Sí, agendar")
    # _iniciar_agendar debería avanzar (no quedarse pidiendo el tipo de nuevo)
    assert get_session(PHONE)["state"] != "WAIT_ECO_FALLBACK_PARDO"
    assert "David Pardo" in r3 or "Pardo" in r3, r3


def test_es_otra_eco_deriva_a_recepcion():
    _iniciar()
    _responder("no se")
    _responder("algo asi como una ecografia")
    r3 = _responder("Es otra eco")
    assert get_session(PHONE)["state"] == "HUMAN_TAKEOVER"
    assert "recepcionista" in r3.lower(), r3


def test_zona_dorsal_reconocida_sin_llegar_al_fallback():
    _iniciar()
    r = _responder("realizan Ecotomografia Dorsal?")
    # matcheó al primer intento — no debe quedar pidiendo el tipo de nuevo
    assert get_session(PHONE)["state"] != "WAIT_ECO_FALLBACK_PARDO"


def test_pelviana_masculina_no_va_a_ginecologia():
    _iniciar()
    r = _responder("ecografia pelviana masculina")
    assert "Rejón" not in r and "Ginecolog" not in r, r


if __name__ == "__main__":
    import subprocess
    raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-v"]))
