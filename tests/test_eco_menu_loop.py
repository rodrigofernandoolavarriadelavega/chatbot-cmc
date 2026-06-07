"""
Regresión del menu-loop de ecografía (auditoría 2026-06-07).

Bug: el paciente escribe "eco abdominal" → el bot explica el tipo y ofrece
"✅ Sí, agendar" guardando especialidad_sugerida="ecografía" (genérico, el órgano
se perdía). Al aceptar, _iniciar_agendar recibía "ecografía" sin el órgano, y como
el texto del turno era el payload del botón ("agendar_sugerido"), route_ecografia
devolvía None → el bot VOLVÍA a preguntar el tipo indefinidamente.

Fix: al ofrecer agendar tras explicar un tipo de eco, se persiste el texto del
órgano en data["eco_tipo_text"]; _iniciar_agendar lo consume (pop) para resolver
el routing sin re-preguntar.

Ejecución:
    pytest tests/test_eco_menu_loop.py -v
    python tests/test_eco_menu_loop.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT))

_TMP_DB = Path(tempfile.mkdtemp(prefix="cmc_ecoloop_")) / "sessions.db"
os.environ["SESSIONS_DB"] = str(_TMP_DB)
os.environ.setdefault("SQLCIPHER_KEY", "")

import session as _session_mod
_session_mod.DB_PATH = _TMP_DB

_TODAY = date(2026, 6, 9)  # martes
_ECO_DATE = date(2026, 6, 10)

import medilink
import flows
import claude_helper
import resilience
import messaging


def _make_slot(prof, esp, fecha, hora, id_prof, duracion=15):
    hm = hora.split(":")
    end = int(hm[0]) * 60 + int(hm[1]) + duracion
    return {
        "profesional": prof, "especialidad": esp,
        "fecha": fecha.strftime("%Y-%m-%d"), "fecha_display": "mié 10 jun",
        "hora_inicio": hora, "hora_fin": f"{end // 60:02d}:{end % 60:02d}",
        "id_profesional": id_prof, "id_recurso": 1, "duracion": duracion,
    }


def _slots_for(esp: str, fecha: date) -> list[dict]:
    e = (esp or "").lower()
    if "cograf" in e or "cotomograf" in e or e == "eco":
        # David Pardo, Ecografía, ID 68
        return [_make_slot("David Pardo", "Ecografía", fecha, "10:00", 68, 15)]
    return [_make_slot("Dr. Andrés Abarca", "Medicina General", fecha, "09:00", 73, 15)]


async def _fake_bp(especialidad, dias_adelante=60, excluir=None,
                   intervalo_override=None, solo_ids=None):
    s = _slots_for(especialidad, _ECO_DATE)
    return s, s


async def _fake_bsd(especialidad, fecha, intervalo_override=None, **kw):
    try:
        f = date.fromisoformat(fecha)
    except (ValueError, TypeError):
        f = _ECO_DATE
    s = _slots_for(especialidad, f)
    return s, s


async def _fake_bsdpi(ids, fecha, intervalo_override=None, **kw):
    return _slots_for("ecografía", _ECO_DATE), _slots_for("ecografía", _ECO_DATE)


async def _fake_cpf(esp):
    return _ECO_DATE.strftime("%Y-%m-%d")


async def _fake_lcp(id_paciente=0, **kw):
    return []


async def _fake_bpac(rut):
    return None  # paciente nuevo → no bloquea ni dispara fast-track


async def _fake_send(*a, **kw):
    pass


for mod in (medilink, flows):
    mod.buscar_primer_dia = _fake_bp
    mod.buscar_slots_dia = _fake_bsd
    mod.buscar_slots_dia_por_ids = _fake_bsdpi
    mod.consultar_proxima_fecha = _fake_cpf
    mod.listar_citas_paciente = _fake_lcp
    mod.buscar_paciente = _fake_bpac

resilience.is_medilink_down = lambda: False
flows.is_medilink_down = lambda: False
messaging.send_whatsapp = _fake_send
flows.send_whatsapp = _fake_send

from session import get_session, reset_session


def _text(resp) -> str:
    if resp is None:
        return ""
    if isinstance(resp, str):
        return resp
    if isinstance(resp, dict):
        parts = []
        for k in ("text", "body"):
            v = resp.get(k)
            if isinstance(v, str):
                parts.append(v)
        # botones
        for b in resp.get("buttons", []) or []:
            if isinstance(b, dict):
                parts.append(str(b.get("title", "")))
        return "\n".join(parts)
    return str(resp)


_PREGUNTA_TIPO_MARK = "de qué tipo es la ecografía"


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_con_eco_tipo_text_no_repregunta():
    """CON el fix: eco_tipo_text presente → NO vuelve a preguntar el tipo."""
    phone = "56900000001"
    reset_session(phone)
    data = {"eco_tipo_text": "eco abdominal"}
    resp = _run(flows._iniciar_agendar(phone, data, "ecografía"))
    out = _text(resp).lower()
    assert _PREGUNTA_TIPO_MARK not in out, (
        f"Regresión del menu-loop: volvió a preguntar el tipo.\n{out[:400]}"
    )
    # No quedó esperando el tipo de eco
    st = get_session(phone)
    assert not st["data"].get("wait_eco_tipo"), "no debe quedar wait_eco_tipo activo"
    # eco_tipo_text fue consumido
    assert "eco_tipo_text" not in st["data"], "eco_tipo_text debe consumirse (pop)"


def test_sin_eco_tipo_text_si_repregunta():
    """Control negativo: SIN órgano y sin contexto → sí pregunta el tipo
    (comportamiento correcto cuando el paciente solo dijo 'ecografía')."""
    phone = "56900000002"
    reset_session(phone)
    data = {}
    resp = _run(flows._iniciar_agendar(phone, data, "ecografía"))
    out = _text(resp).lower()
    assert _PREGUNTA_TIPO_MARK in out, (
        f"Sin órgano debe preguntar el tipo, pero respondió:\n{out[:400]}"
    )
    st = get_session(phone)
    assert st["data"].get("wait_eco_tipo"), "debe quedar esperando el tipo de eco"


if __name__ == "__main__":
    test_con_eco_tipo_text_no_repregunta()
    test_sin_eco_tipo_text_si_repregunta()
    print("OK: 2/2 — menu-loop de ecografía cubierto")
