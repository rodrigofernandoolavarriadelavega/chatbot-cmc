"""Tests del candado anti-doble-mensaje compartido entre el cron de waitlist
(07:00) y el fan-out por evento de Fase 4 (Alma operativa).

Fija el contrato de la 'memoria común':
  - phones_with_open_offers(): un teléfono con oferta VIVA (enviada/apartado/recepcion)
    aparece; uno con oferta muerta (perdida/expirada/confirmada) NO.
  - expire_stale_offers(): vence invitaciones 'enviada' colgadas (slot pasado o >3d),
    devolviendo al paciente al pool — sin esto, ignorar una oferta = silent drop.
  - _match_candidatos(): no invita a quien ya tiene una oferta viva.

Correr: python3 tests/test_waitlist_doble_mensaje.py
"""
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

from alma_brain import operativa  # noqa: E402


def _ok(cond, msg):
    print(("OK   " if cond else "FALLO ") + msg)
    assert cond, msg


def _offer(phone, fecha, hora):
    sk = session.make_slot_key(60, fecha, hora)
    return session.create_offer(sk, phone, "cardiología", 60, fecha, hora)


def test_oferta_enviada_bloquea():
    _offer("56911111111", "2030-01-10", "10:00")
    _ok("56911111111" in session.phones_with_open_offers(),
        "una oferta 'enviada' bloquea al teléfono (no doble-mensaje)")


def test_apartado_y_recepcion_bloquean():
    oid = _offer("56933333333", "2030-03-10", "12:00")
    session.claim_offer(oid)  # enviada → apartado
    _ok("56933333333" in session.phones_with_open_offers(), "'apartado' bloquea")
    session.set_offer_estado(oid, "recepcion")
    _ok("56933333333" in session.phones_with_open_offers(), "'recepcion' bloquea")


def test_oferta_muerta_no_bloquea():
    oid = _offer("56922222222", "2030-02-10", "11:00")
    session.set_offer_estado(oid, "perdida")
    _ok("56922222222" not in session.phones_with_open_offers(),
        "una oferta 'perdida' NO bloquea — el paciente vuelve al pool")


def test_expire_libera_enviada_con_slot_pasado():
    # Slot en el pasado: expire_stale_offers debe vencerla y liberar al paciente.
    # Sin esto, una invitación ignorada lo dejaría bloqueado para siempre.
    _offer("56944444444", "2000-01-01", "09:00")
    _ok("56944444444" in session.phones_with_open_offers(), "antes de expirar, bloquea")
    venc = session.expire_stale_offers()
    _ok(venc >= 1, f"expire vence ≥1 oferta colgada (venció {venc})")
    _ok("56944444444" not in session.phones_with_open_offers(),
        "tras vencer el slot pasado, vuelve al pool (no silent drop)")


def test_match_excluye_oferta_viva():
    session.add_to_waitlist("56955555555", "55555555-5", "Eva Vera", "cardiología")
    _offer("56955555555", "2030-04-10", "08:00")
    busy = session.phones_with_open_offers()
    cands = operativa._match_candidatos("cardiología", 60, excluir_phones=busy)
    phones = [c["phone"] for c in cands]
    _ok("56955555555" not in phones,
        "Fase 4 no re-invita a quien ya tiene una oferta viva")


def test_match_sin_set_es_backward_compatible():
    # Llamada vieja (sin excluir_phones) sigue funcionando.
    session.add_to_waitlist("56966666666", "66666666-6", "Foe Fux", "dermatología")
    cands = operativa._match_candidatos("dermatología", None)
    _ok(any(c["phone"] == "56966666666" for c in cands),
        "_match_candidatos sin excluir_phones sigue devolviendo candidatos")


if __name__ == "__main__":
    _tests = [v for k, v in sorted(globals().items())
              if k.startswith("test_") and callable(v)]
    for fn in _tests:
        fn()
    print(f"\n✅ candado anti-doble-mensaje: {len(_tests)} tests pasaron")
