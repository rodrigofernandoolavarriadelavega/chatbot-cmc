"""
Regresión del bug P1: cancelar/reagendar ignora family_links (auditoría 2026-06-09).

Bug real (teléfono ...7534, 4-jun): mamá cancela cita de su hijo (RUT distinto),
el bot respondía "No encontré citas futuras para Maximo" aunque la cita de José
Bernal (hijo) existe y está vinculada via family_links. ~4 casos/semana → takeover.

Fix: antes de responder "no hay citas", buscar en family_links por los RUTs
vinculados y presentar sus citas si las hay. Si tampoco hay familiares con citas,
el mensaje sugiere enviar el RUT del familiar.

Casos cubiertos:
  1. Perfil conocido, sin citas propias, familiar con cita  → lista familiar, luego cancelar
  2. RUT manual sin citas propias, familiar con cita        → lista familiar, luego cancelar
  3. Sin citas propias ni familiares                        → mensaje con sugerencia de RUT
  4. Usuario manda RUT de familiar en estado FAMILIAR       → busca por ese RUT directo
  5. Error en _buscar_citas_familiares (family_links falla) → fallback al mensaje actual

Ejecución:
    pytest tests/test_cancelar_familiar.py -v
    python tests/test_cancelar_familiar.py
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

_TMP_DB = Path(tempfile.mkdtemp(prefix="cmc_familiar_")) / "sessions.db"
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

import session as _session_mod
_session_mod.DB_PATH = _TMP_DB

import medilink
import flows
import resilience
import messaging

# ── Mocks globales ────────────────────────────────────────────────────────────

PHONE_MAMA = "56987534000"
RUT_MAMA = "11111111-1"
NOMBRE_MAMA = "Maximo Bernal"

RUT_HIJO = "22222222-2"
NOMBRE_HIJO = "José Bernal"

CITA_HIJO = {
    "id": 9001,
    "especialidad": "Kinesiología",
    "profesional": "Luis Armijo",
    "fecha": "2026-06-15",
    "fecha_display": "dom 15-jun",
    "hora_inicio": "11:30:00",
    "hora_fin": "12:10:00",
}

PACIENTE_MAMA = {"id": 1001, "rut": RUT_MAMA, "nombre": NOMBRE_MAMA}
PACIENTE_HIJO = {"id": 1002, "rut": RUT_HIJO, "nombre": NOMBRE_HIJO}


def _noop(*a, **kw):
    pass


def _setup_mocks(monkeypatch_dict: dict):
    """Aplica monkeypatching sobre los módulos importados."""
    for target, value in monkeypatch_dict.items():
        module, attr = target.rsplit(".", 1)
        mod = sys.modules[module]
        setattr(mod, attr, value)


def _set_mocks_base():
    """Mocks mínimos: Medilink down desactivado, send_whatsapp sin-op."""
    resilience.is_medilink_down = lambda: False
    messaging.send_whatsapp = asyncio.coroutine(lambda *a, **kw: None) if False else (
        lambda *a, **kw: asyncio.coroutine(lambda: None)()
    )
    # send_whatsapp es async — monkeypatch con coroutine
    async def _fake_send(*a, **kw):
        pass
    messaging.send_whatsapp = _fake_send
    flows.send_whatsapp = _fake_send


# ═══════════════════════════════════════════════════════════════════════════════
# Caso 1: perfil conocido, sin citas propias, familiar con cita → lista familiar
# ═══════════════════════════════════════════════════════════════════════════════

def test_iniciar_cancelar_muestra_familiar_cuando_no_hay_citas_propias():
    """Perfil guardado → citas propias vacías → familiar con cita → bot lista el familiar."""
    _set_mocks_base()

    # Guardar perfil de la mamá
    _session_mod.save_profile(PHONE_MAMA, RUT_MAMA, NOMBRE_MAMA)

    # Vincular hijo como dependiente
    _session_mod.add_family_link(
        owner_rut=RUT_MAMA,
        dependent_rut=RUT_HIJO,
        dependent_nombre=NOMBRE_HIJO,
        relation="hijo",
        verification_method="bot_registro",
    )

    # buscar_paciente: mamá → PACIENTE_MAMA, hijo → PACIENTE_HIJO
    async def _fake_buscar_paciente(rut):
        if rut == RUT_MAMA:
            return PACIENTE_MAMA
        if rut == RUT_HIJO:
            return PACIENTE_HIJO
        return None

    # listar_citas_paciente: mamá sin citas, hijo con cita
    async def _fake_listar_citas(pac_id, rut=None):
        if pac_id == PACIENTE_MAMA["id"]:
            return []
        if pac_id == PACIENTE_HIJO["id"]:
            return [CITA_HIJO]
        return []

    flows.buscar_paciente = _fake_buscar_paciente
    flows.listar_citas_paciente = _fake_listar_citas
    flows.get_citas_bot_futuras = lambda *a, **kw: []

    resp = asyncio.run(flows._iniciar_cancelar(PHONE_MAMA, {}))

    assert "José" in resp or "jose" in resp.lower(), f"No menciona el familiar: {resp!r}"
    assert "Kinesiología" in resp or "kinesiolog" in resp.lower(), f"No menciona la especialidad: {resp!r}"
    sess = _session_mod.get_session(PHONE_MAMA)
    assert sess["state"] == "WAIT_CITA_CANCELAR_FAMILIAR", (
        f"Estado inesperado: {sess['state']}"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Caso 2: RUT manual (WAIT_RUT_CANCELAR) sin citas propias, familiar con cita
# ═══════════════════════════════════════════════════════════════════════════════

def test_wait_rut_cancelar_muestra_familiar():
    """Usuario ingresa RUT de mamá manualmente → citas propias vacías → familiar listado."""
    _set_mocks_base()

    # Sesión en WAIT_RUT_CANCELAR
    _session_mod.save_session(PHONE_MAMA, "WAIT_RUT_CANCELAR", {})

    async def _fake_buscar_paciente(rut):
        if rut == RUT_MAMA:
            return PACIENTE_MAMA
        if rut == RUT_HIJO:
            return PACIENTE_HIJO
        return None

    async def _fake_listar_citas(pac_id, rut=None):
        if pac_id == PACIENTE_MAMA["id"]:
            return []
        if pac_id == PACIENTE_HIJO["id"]:
            return [CITA_HIJO]
        return []

    flows.buscar_paciente = _fake_buscar_paciente
    flows.listar_citas_paciente = _fake_listar_citas
    flows.get_citas_bot_futuras = lambda *a, **kw: []

    # El usuario manda el RUT de la mamá (11.111.111-1 → clean_rut lo normaliza)
    sess = _session_mod.get_session(PHONE_MAMA)
    resp = asyncio.run(flows.handle_message(PHONE_MAMA, "11.111.111-1", sess))

    assert "José" in resp or "jose" in resp.lower() or "familiar" in resp.lower(), (
        f"Debería mencionar al familiar: {resp!r}"
    )
    sess = _session_mod.get_session(PHONE_MAMA)
    assert sess["state"] == "WAIT_CITA_CANCELAR_FAMILIAR", (
        f"Estado inesperado: {sess['state']}"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Caso 3: Sin citas propias ni familiares → mensaje con sugerencia de RUT
# ═══════════════════════════════════════════════════════════════════════════════

def test_iniciar_cancelar_sin_citas_ni_familiares_sugiere_rut():
    """Sin citas propias ni familiares → el mensaje menciona posibilidad de enviar RUT."""
    _set_mocks_base()

    _session_mod.save_profile(PHONE_MAMA, RUT_MAMA, NOMBRE_MAMA)
    # Sin family_links (ya están del test anterior, pero revocar para aislar)
    try:
        _session_mod.revoke_family_link(RUT_MAMA, RUT_HIJO)
    except Exception:
        pass

    async def _fake_buscar_paciente(rut):
        if rut == RUT_MAMA:
            return PACIENTE_MAMA
        return None

    async def _fake_listar_citas(pac_id, rut=None):
        return []

    flows.buscar_paciente = _fake_buscar_paciente
    flows.listar_citas_paciente = _fake_listar_citas
    flows.get_citas_bot_futuras = lambda *a, **kw: []

    resp = asyncio.run(flows._iniciar_cancelar(PHONE_MAMA, {}))

    # Debe mencionar la posibilidad de enviar RUT de familiar
    assert "RUT" in resp or "rut" in resp.lower(), (
        f"Debería sugerir enviar RUT del familiar: {resp!r}"
    )
    assert "familiar" in resp.lower() or "hijo" in resp.lower(), (
        f"Debería mencionar familiar: {resp!r}"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Caso 4: Usuario manda RUT del hijo en estado WAIT_CITA_CANCELAR_FAMILIAR
# ═══════════════════════════════════════════════════════════════════════════════

def test_wait_cita_cancelar_familiar_acepta_rut_directo():
    """En WAIT_CITA_CANCELAR_FAMILIAR el usuario manda el RUT del hijo → bot busca y lista."""
    _set_mocks_base()

    # Poner sesión en estado FAMILIAR con lista de citas planas
    cita_plana = dict(CITA_HIJO)
    cita_plana["_familiar_paciente"] = PACIENTE_HIJO
    _session_mod.save_session(PHONE_MAMA, "WAIT_CITA_CANCELAR_FAMILIAR", {
        "citas_familiares": [cita_plana],
        "rut": RUT_MAMA,
    })

    async def _fake_buscar_paciente_safe(rut):
        if rut == RUT_HIJO:
            return PACIENTE_HIJO, False
        return None, False

    async def _fake_listar_citas(pac_id, rut=None):
        if pac_id == PACIENTE_HIJO["id"]:
            return [CITA_HIJO]
        return []

    flows._buscar_paciente_safe = _fake_buscar_paciente_safe
    flows.listar_citas_paciente = _fake_listar_citas

    sess = _session_mod.get_session(PHONE_MAMA)
    resp = asyncio.run(flows.handle_message(PHONE_MAMA, "22.222.222-2", sess))

    # La respuesta puede ser str o dict interactivo; serializar para buscar
    resp_str = str(resp)
    assert "José" in resp_str or "Bernal" in resp_str or "Kinesiolog" in resp_str or "Armijo" in resp_str, (
        f"Debería listar citas del hijo: {resp_str!r}"
    )
    sess = _session_mod.get_session(PHONE_MAMA)
    assert sess["state"] == "WAIT_CITA_CANCELAR", (
        f"Estado inesperado: {sess['state']}"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Caso 5: Error en _buscar_citas_familiares → fallback al mensaje normal
# ═══════════════════════════════════════════════════════════════════════════════

def test_error_en_family_links_hace_fallback():
    """Si list_family_links lanza excepción, el bot responde con el mensaje normal (sin crash)."""
    _set_mocks_base()

    _session_mod.save_profile(PHONE_MAMA, RUT_MAMA, NOMBRE_MAMA)

    async def _fake_buscar_paciente(rut):
        return PACIENTE_MAMA

    async def _fake_listar_citas(pac_id, rut=None):
        return []

    flows.buscar_paciente = _fake_buscar_paciente
    flows.listar_citas_paciente = _fake_listar_citas
    flows.get_citas_bot_futuras = lambda *a, **kw: []

    # Forzar error en list_family_links directamente en _buscar_citas_familiares
    _orig = _session_mod.list_family_links

    def _raise(rut):
        raise RuntimeError("DB error simulado")

    _session_mod.list_family_links = _raise
    # flows importa list_family_links en tiempo de módulo desde session
    # Parchamos directamente en el namespace de flows
    flows.list_family_links = _raise

    try:
        resp = asyncio.run(flows._iniciar_cancelar(PHONE_MAMA, {}))
    finally:
        _session_mod.list_family_links = _orig
        flows.list_family_links = _orig

    # El bot no debe crashear y debe dar algún mensaje útil
    assert resp, "El bot no devolvió ningún mensaje"
    assert "cita" in resp.lower() or "agendar" in resp.lower() or "RUT" in resp, (
        f"Mensaje fallback inesperado: {resp!r}"
    )


# ── Runner standalone ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    tests = [
        ("Caso 1: perfil conocido + familiar con cita → lista familiar",
         test_iniciar_cancelar_muestra_familiar_cuando_no_hay_citas_propias),
        ("Caso 2: RUT manual + familiar con cita → lista familiar",
         test_wait_rut_cancelar_muestra_familiar),
        ("Caso 3: sin citas ni familiares → sugiere enviar RUT",
         test_iniciar_cancelar_sin_citas_ni_familiares_sugiere_rut),
        ("Caso 4: RUT enviado en estado FAMILIAR → busca directo",
         test_wait_cita_cancelar_familiar_acepta_rut_directo),
        ("Caso 5: error en family_links → fallback sin crash",
         test_error_en_family_links_hace_fallback),
    ]
    passed = 0
    for nombre, fn in tests:
        try:
            fn()
            print(f"  PASS  {nombre}")
            passed += 1
        except Exception as e:
            print(f"  FAIL  {nombre}: {e}")
    print(f"\n{passed}/{len(tests)} tests pasaron")
    if passed < len(tests):
        sys.exit(1)
