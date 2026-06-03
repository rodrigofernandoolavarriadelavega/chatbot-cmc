"""Tests offline de Alma operativa (Fase 4) — sin red ni Medilink real.

Fijan el contrato de:
  - claim_offer: carrera atómica "primero que acepta gana" (uno solo aparta el cupo).
  - _match_candidatos: compatibilidad (especialidad, profesional preferido, exclusión).
  - accept_offer: ruteo auto-confirma vs recepción según la política, con Medilink mockeado.

Correr: `python3 tests/test_alma_operativa.py`
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

# DB temporal ANTES de importar nada que toque session.
import session  # noqa: E402
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
session.DB_PATH = Path(_tmp.name)

from alma_brain import operativa, policy  # noqa: E402
import medilink  # noqa: E402


def _ok(cond, msg):
    print(("OK   " if cond else "FALLO ") + msg)
    assert cond, msg


def _seed_waitlist():
    # FIFO: Ana (más antigua) → Beto → Carla. Carla pide profesional 60.
    session.add_to_waitlist("56900000001", "11111111-1", "Ana Soto", "cardiología")
    session.add_to_waitlist("56900000002", "22222222-2", "Beto Díaz", "cardiología")
    session.add_to_waitlist("56900000003", "33333333-3", "Carla Ruiz", "cardiología",
                            id_prof_pref=99)  # quiere otro profesional, NO compatible con prof 60


def test_match_respeta_fifo_pref_y_exclusion():
    _seed_waitlist()
    cands = operativa._match_candidatos("Cardiología", 60, excluir_phone="56900000001")
    phones = [c["phone"] for c in cands]
    _ok("56900000001" not in phones, "excluye al que canceló (Ana)")
    _ok("56900000003" not in phones, "excluye a quien prefiere otro profesional (Carla)")
    _ok(phones == ["56900000002"], f"deja solo a Beto, compatible: {phones}")


def test_claim_atomico_primero_gana():
    slot_key = session.make_slot_key(60, "2030-01-10", "10:00")
    o1 = session.create_offer(slot_key, "56900000001", "cardiología", 60, "2030-01-10", "10:00")
    o2 = session.create_offer(slot_key, "56900000002", "cardiología", 60, "2030-01-10", "10:00")

    r1 = session.claim_offer(o1)
    r2 = session.claim_offer(o2)
    _ok(r1["ok"] and r1["reason"] == "claimed", "el primero aparta el cupo")
    _ok(not r2["ok"] and r2["reason"] == "slot_taken", "el segundo pierde la carrera")

    # Estado final: una sola oferta 'apartado', la otra 'perdida'.
    with session._conn() as c:
        ap = c.execute("SELECT COUNT(*) FROM waitlist_offers WHERE slot_key=? AND estado='apartado'",
                       (slot_key,)).fetchone()[0]
        pe = c.execute("SELECT COUNT(*) FROM waitlist_offers WHERE slot_key=? AND estado='perdida'",
                       (slot_key,)).fetchone()[0]
    _ok(ap == 1, f"exactamente 1 cupo apartado (got {ap})")
    _ok(pe == 1, f"exactamente 1 oferta perdida (got {pe})")


def test_accept_autoconfirma_bajo_riesgo():
    """Con el flag de auto-confirmación ON y señales de bajo riesgo, el sistema
    crea la cita en Medilink (mockeado) y la oferta queda 'confirmada'."""
    os.environ["ALMA_OPERATIVA_AUTOCONFIRM"] = "true"

    # Mocks de Medilink: paciente conocido + crear_cita exitoso.
    medilink.valid_rut = lambda r: True
    async def _fake_buscar(rut):  # paciente existe
        return {"id": 777, "nombre": "Ana Soto", "rut": rut}
    medilink.buscar_paciente = _fake_buscar
    async def _fake_crear(*a, **k):
        return {"id": "CITA-555", "confirmado": True}
    medilink.crear_cita = _fake_crear

    sent = []
    async def _send(phone, msg):
        sent.append((phone, msg))

    slot_key = session.make_slot_key(60, "2030-02-20", "09:00")  # >24h en el futuro
    oid = session.create_offer(slot_key, "56911111111", "cardiología", 60,
                               "2030-02-20", "09:00", waitlist_id=1,
                               rut="11111111-1", nombre="Ana")
    offer = session.get_open_offer_for_phone("56911111111")
    res = asyncio.run(operativa.accept_offer(offer, send_fn=_send))
    _ok(res["estado"] == "confirmada" and res["auto"], f"auto-confirmada: {res}")
    _ok(bool(sent), "se envió mensaje de confirmación al paciente")
    with session._conn() as c:
        row = c.execute("SELECT estado, id_cita FROM waitlist_offers WHERE id=?", (oid,)).fetchone()
    _ok(row["estado"] == "confirmada" and row["id_cita"] == "CITA-555",
        f"oferta confirmada con id_cita persistido: {dict(row)}")


def test_accept_cae_a_recepcion_si_flag_off():
    """Sin el flag de auto-confirmación, todo cupo aceptado va a recepción
    (hold blando), sin tocar Medilink."""
    os.environ.pop("ALMA_OPERATIVA_AUTOCONFIRM", None)
    sent = []
    async def _send(phone, msg):
        sent.append((phone, msg))

    slot_key = session.make_slot_key(60, "2030-03-15", "11:00")
    session.create_offer(slot_key, "56922222222", "cardiología", 60,
                         "2030-03-15", "11:00", rut="22222222-2", nombre="Beto")
    offer = session.get_open_offer_for_phone("56922222222")
    res = asyncio.run(operativa.accept_offer(offer, send_fn=_send))
    _ok(res["estado"] == "recepcion" and not res["auto"], f"cae a recepción: {res}")
    pend = session.get_offers_pendientes_recepcion()
    _ok(any(p["phone"] == "56922222222" for p in pend), "aparece en cola de recepción")


def test_fill_gateado_no_invita():
    """Con ALMA_OPERATIVA_ENABLED=false, una cancelación NO contacta a nadie."""
    os.environ.pop("ALMA_OPERATIVA_ENABLED", None)
    sent = []
    async def _send(phone, msg):
        sent.append((phone, msg))
    slot = {"especialidad": "cardiología", "id_prof": 60, "fecha": "2030-04-01",
            "hora": "08:00", "phone_cancelador": "56900000099"}
    res = asyncio.run(operativa.fill_freed_slot(slot, send_fn=_send))
    _ok(res["enabled"] is False and res["invitados"] == 0, f"fan-out gateado: {res}")
    _ok(not sent, "no se envió ninguna invitación con el flag apagado")


def test_fill_invita_top_n_cuando_enabled():
    os.environ["ALMA_OPERATIVA_ENABLED"] = "true"
    sent = []
    async def _send(phone, msg):
        sent.append((phone, msg))
    # Nueva especialidad con 2 candidatos compatibles.
    session.add_to_waitlist("56933333301", "44444444-4", "Dina", "nutrición")
    session.add_to_waitlist("56933333302", "55555555-5", "Edu", "nutrición")
    slot = {"especialidad": "nutrición", "id_prof": 52, "fecha": "2030-05-01",
            "hora": "15:00", "phone_cancelador": ""}
    res = asyncio.run(
        operativa.fill_freed_slot(slot, send_fn=_send, top_n=3))
    _ok(res["invitados"] == 2, f"invitó a los 2 candidatos: {res}")
    _ok(len(sent) == 2, "se enviaron 2 invitaciones")
    # Y se crearon 2 ofertas 'enviada' para ese slot.
    with session._conn() as c:
        n = c.execute("SELECT COUNT(*) FROM waitlist_offers WHERE slot_key=? AND estado='enviada'",
                      (res["slot_key"],)).fetchone()[0]
    _ok(n == 2, f"2 ofertas enviadas creadas (got {n})")


def test_fill_usa_template_cuando_flag():
    """Con USE_TEMPLATES=true y canal real (send_fn=None), la invitación sale por
    el template Meta `oferta_cupo` (entrega fuera de la ventana de 24h)."""
    os.environ["ALMA_OPERATIVA_ENABLED"] = "true"
    os.environ["USE_TEMPLATES"] = "true"
    import messaging
    llamadas = []
    async def _fake_tpl(to, name, body_params=None, **k):
        llamadas.append((to, name, body_params))
    messaging.send_whatsapp_template = _fake_tpl
    messaging.render_template_body = lambda name, params=None: f"[{name}] {params}"

    session.add_to_waitlist("56944444401", "66666666-6", "Fran", "podología")
    slot = {"especialidad": "podología", "id_prof": 56, "fecha": "2030-06-01",
            "hora": "16:00", "phone_cancelador": ""}
    res = asyncio.run(operativa.fill_freed_slot(slot))  # send_fn=None → canal real
    _ok(res["invitados"] == 1, f"invitó 1: {res}")
    _ok(len(llamadas) == 1 and llamadas[0][1] == "oferta_cupo",
        f"salió por template oferta_cupo: {llamadas}")
    _ok(llamadas[0][2][1] == "Podología", f"body_params trae especialidad: {llamadas[0][2]}")
    os.environ.pop("USE_TEMPLATES", None)


if __name__ == "__main__":
    tests = [
        test_match_respeta_fifo_pref_y_exclusion,
        test_claim_atomico_primero_gana,
        test_accept_autoconfirma_bajo_riesgo,
        test_accept_cae_a_recepcion_si_flag_off,
        test_fill_gateado_no_invita,
        test_fill_invita_top_n_cuando_enabled,
        test_fill_usa_template_cuando_flag,
    ]
    for t in tests:
        t()
    print(f"\n{len(tests)}/{len(tests)} tests OK — Alma operativa (Fase 4)")
    os.unlink(_tmp.name)
