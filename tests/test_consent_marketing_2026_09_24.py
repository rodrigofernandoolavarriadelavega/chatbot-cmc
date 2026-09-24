"""Consentimiento de marketing (Ley 21.719) — auditoría 2026-09-24 sobre 706
plantillas consent_marketing_v2 en 30 días. Casos que se perdían:
  - "Sí, actívenlos" con la conversación tomada por recepción (main.py lo
    silenciaba antes de llegar al flujo);
  - "Sí, actívenlos" de quien antes dijo "no" (quedaba 'declined');
  - "Siii" no reconocido.
Y lo que NO debe pasar: un "No por ahora" a OTRO botón (cross-sell,
reenganche) tomado como rechazo de marketing (bug 2026-05-28, 98 bajas).
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402
import harness_50 as H  # noqa: E402,F401 — mocks de Medilink/Claude/WhatsApp
import consent_marketing as CM  # noqa: E402
import flows  # noqa: E402
import winback  # noqa: E402
import session as S  # noqa: E402
from session import reset_session, save_session, get_session  # noqa: E402


def log_message(phone, direction, text, state):
    """Escritura directa: otros tests de la suite reemplazan session.log_message
    por un doble, y estos casos dependen de que el mensaje quede en la tabla."""
    with S.db() as conn:
        conn.execute("INSERT INTO messages (phone, direction, text, state) VALUES (?,?,?,?)",
                     (phone, direction, text, state))
        conn.commit()

@pytest.fixture(autouse=True)
def _session_real(monkeypatch):
    """test_abono_transferencia reemplaza sys.modules["session"] por un doble al
    importarse; consent_marketing importa `session` en cada llamada, así que
    en la suite completa leía el doble (sin tabla messages). Lo mismo con
    `winback`, que otros tests también sustituyen."""
    monkeypatch.setitem(sys.modules, "session", S)
    monkeypatch.setitem(sys.modules, "winback", winback)


PLANTILLA = "[template: consent_marketing_v2]\nHola Ana, le saluda el Centro Médico Carampangue."
_n = [0]


def _phone():
    _n[0] += 1
    return f"5690024{os.getpid() % 1000:03d}{_n[0]:02d}"


class _BI:
    """Reemplaza las escrituras a Postgres BI (no hay BI en tests)."""
    def __init__(self):
        self.consent, self.opt_out, self.opt_in = [], [], []

    def __enter__(self):
        self._orig = (winback.registrar_consent_respuesta, winback.registrar_opt_out_marketing,
                      winback.remover_opt_out_marketing, winback.WINBACK_ACTIVE)
        winback.registrar_consent_respuesta = lambda ph, st, method: self.consent.append((ph, st))
        winback.registrar_opt_out_marketing = lambda ph, **k: self.opt_out.append(ph)
        winback.remover_opt_out_marketing = lambda ph: self.opt_in.append(ph)
        winback.WINBACK_ACTIVE = False
        return self

    def __exit__(self, *a):
        (winback.registrar_consent_respuesta, winback.registrar_opt_out_marketing,
         winback.remover_opt_out_marketing, winback.WINBACK_ACTIVE) = self._orig


def _responder(phone, texto, estado="IDLE", data=None):
    save_session(phone, estado, data or {})
    return H._normalize(asyncio.run(flows.handle_message(phone, texto, get_session(phone))))


# ── detectar ──────────────────────────────────────────────────────────────────

def test_boton_si_tras_plantilla():
    ph = _phone()
    log_message(ph, "out", PLANTILLA, "IDLE")
    assert CM.detectar(ph, "Sí, actívenlos") == "accepted"


def test_boton_si_aunque_haya_mensajes_despues_de_la_plantilla():
    ph = _phone()
    log_message(ph, "out", PLANTILLA, "IDLE")
    log_message(ph, "out", "Recordatorio: mañana tienes hora", "IDLE")
    assert CM.detectar(ph, "Sí, actívenlos") == "accepted"


def test_no_por_ahora_solo_si_contesta_la_plantilla():
    ph = _phone()
    log_message(ph, "out", PLANTILLA, "IDLE")
    assert CM.detectar(ph, "No por ahora") == "declined"
    log_message(ph, "out", "¿Te gustaría agendar kinesiología?", "WAIT_CROSS_SELL")
    assert CM.detectar(ph, "No por ahora") is None      # contesta al cross-sell


def test_si_escrito_con_vocales_repetidas():
    ph = _phone()
    log_message(ph, "out", PLANTILLA, "IDLE")
    for t in ("Siii", "sii", "Sí!", "si"):
        assert CM.detectar(ph, t) == "accepted", t


def test_si_acepto_de_otra_plantilla_no_cuenta():
    ph = _phone()
    log_message(ph, "out", PLANTILLA, "IDLE")
    log_message(ph, "out", "[template: consent_dental_v1]\nHola…", "IDLE")
    assert CM.detectar(ph, "Sí, acepto") is None


def test_sin_plantilla_no_detecta():
    ph = _phone()
    log_message(ph, "out", "Hola, ¿en qué te ayudo?", "IDLE")
    assert CM.detectar(ph, "Sí, actívenlos") is None
    assert CM.detectar(ph, "si") is None


# ── flujo ─────────────────────────────────────────────────────────────────────

def test_flujo_registra_aunque_la_sesion_este_en_otro_estado():
    ph = _phone()
    log_message(ph, "out", PLANTILLA, "WAIT_SLOT")
    with _BI() as bi:
        r = _responder(ph, "Sí, actívenlos", estado="WAIT_SLOT", data={"slots": []})
    assert bi.consent and bi.consent[-1][1] == "accepted", r
    assert bi.opt_in, "un sí debe sacarlo de opt_outs_marketing (re-opt-in)"
    assert "registrado" in r.lower(), r


def test_flujo_no_por_ahora_registra_declined_y_opt_out():
    ph = _phone()
    log_message(ph, "out", PLANTILLA, "IDLE")
    with _BI() as bi:
        r = _responder(ph, "No por ahora")
    assert bi.consent[-1][1] == "declined" and bi.opt_out, r


def test_flujo_no_por_ahora_de_cross_sell_no_toca_marketing():
    ph = _phone()
    log_message(ph, "out", PLANTILLA, "IDLE")
    log_message(ph, "out", "¿Te agendo kinesiología?", "WAIT_CROSS_SELL")
    with _BI() as bi:
        _responder(ph, "No por ahora", estado="WAIT_CROSS_SELL")
    assert not bi.consent and not bi.opt_out


# ── conversación tomada por recepción (main.py) ───────────────────────────────

def test_takeover_registra_el_boton_sin_sacar_a_recepcion():
    os.environ["META_APP_SECRET"] = "testsecret"
    import config
    config.META_APP_SECRET = "testsecret"
    import main
    from fastapi.testclient import TestClient
    ph = _phone()
    log_message(ph, "out", PLANTILLA, "HUMAN_TAKEOVER")
    save_session(ph, "HUMAN_TAKEOVER", {"human_replied": True})
    enviados = []

    async def _fake_send(to, body):
        enviados.append((to, body))
    orig_send = main.send_whatsapp
    main.send_whatsapp = _fake_send
    payload = {"object": "whatsapp_business_account", "entry": [{"id": "x", "changes": [{
        "field": "messages", "value": {"messaging_product": "whatsapp",
                                       "contacts": [{"wa_id": ph}],
                                       "messages": [{"id": f"wamid.cm{ph}", "from": ph,
                                                     "type": "button",
                                                     "button": {"text": "Sí, actívenlos",
                                                                "payload": "Sí, actívenlos"}}]}}]}]}
    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(b"testsecret", body, hashlib.sha256).hexdigest()
    try:
        with _BI() as bi:
            r = TestClient(main.app).post("/webhook", content=body, headers={
                "x-hub-signature-256": sig, "content-type": "application/json"})
    finally:
        main.send_whatsapp = orig_send
    assert r.status_code == 200
    assert bi.consent and bi.consent[-1][1] == "accepted", "el botón se perdió en takeover"
    assert enviados and "registrado" in enviados[-1][1]
    assert get_session(ph)["state"] == "HUMAN_TAKEOVER"      # recepción sigue a cargo
    reset_session(ph)
