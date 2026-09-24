"""Bug 2026-09-24: con la agenda formal de eco (David Pardo) 100% llena, el bot
respondía "No hay horas disponibles" e inscribía en lista de espera aunque había
12 sobrecupos libres el lunes 28. Los sobrecupos solo se inyectaban DESPUÉS de
encontrar una hora formal (bloque "sobrecupo en la primera oferta"), así que con
0 slots formales nunca se ofrecían. Ahora se prueban antes de declarar vacío.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT))

import harness_50 as H  # noqa: E402,F401 — aplica los mocks de Medilink/Claude
import flows  # noqa: E402
import sobrecupo  # noqa: E402
from session import get_session, reset_session  # noqa: E402

PHONE = "56900002424"

_SOBRE = {
    "profesional": "David Pardo M.", "id_profesional": 68,
    "especialidad": "Ecografía", "fecha": "2026-09-28",
    "hora_inicio": "10:00", "hora_fin": "10:15", "sobrecupo": True, "capa": 2,
}


async def _vacio(*_a, **_k):
    return [], []


def _correr(sobres):
    async def _gen(_esp, dias_horizonte: int = 10):
        return [dict(s) for s in sobres]

    orig_bpd, orig_gen = flows.buscar_primer_dia, sobrecupo.generar_slots
    flows.buscar_primer_dia = _vacio
    sobrecupo.generar_slots = _gen
    try:
        reset_session(PHONE)
        return H._normalize(asyncio.run(flows._iniciar_agendar(PHONE, {"eco_tipo_text": "abdominal"}, "ecografía")))
    finally:
        flows.buscar_primer_dia, sobrecupo.generar_slots = orig_bpd, orig_gen


def test_agenda_llena_ofrece_sobrecupo_no_waitlist():
    r = _correr([_SOBRE])
    assert "No hay horas" not in r and "No encontré horas" not in r, r
    assert "10:00" in r, r
    assert "lista de espera" not in r.lower(), r


def test_agenda_llena_sin_sobrecupos_sigue_a_waitlist():
    r = _correr([])
    assert "No hay horas" in r or "No encontré horas" in r, r
